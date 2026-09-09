#!/usr/bin/env bash
# Two-Sided Matching Lab — one entry point for everything.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

DSN="${LAB_DSN:-postgresql://lab:lab@localhost:5435/lab}"
export LAB_DSN="$DSN"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim()  { printf '\033[2m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

need_uv()   { command -v uv >/dev/null || die "uv not found. Install: https://docs.astral.sh/uv/"; }
need_psql() { command -v psql >/dev/null || die "psql not found. Install postgresql-client."; }

compose() {
    command -v docker >/dev/null || die "docker not found."
    docker compose "$@"
}

scenario_dir() {
    local key="$1" match
    match=$(find scenarios -maxdepth 1 -type d -name "${key}*" | sort | head -1)
    [ -n "$match" ] || die "No scenario matching '$key'. Try: ./lab.sh list"
    echo "$match"
}

wait_ready() {
    # Probe over TCP from the host rather than pg_isready inside the container.
    # On first boot the entrypoint runs a temporary server with
    # listen_addresses='' to apply schema/, and an in-container check reports
    # THAT server as ready while nothing is listening on 5435 yet.
    local i hits=0
    for i in $(seq 1 90); do
        if psql "$DSN" -X -q -tAc 'SELECT 1' >/dev/null 2>&1; then
            hits=$(( hits + 1 ))
            [ "$hits" -ge 2 ] && return 0
        else
            hits=0
        fi
        sleep 1
    done
    die "postgres did not become ready in 90s. Check: ./lab.sh logs"
}

usage() {
cat <<'EOF'
Two-Sided Matching Lab — job recommendations that go both ways.

  setup
    ./lab.sh up                    start postgres+pgvector on :5435, apply schema
    ./lab.sh seed                  load + embed the checked-in snapshot (no network)
    ./lab.sh down                  stop and delete the database
    ./lab.sh reset                 drop and re-apply the schema (keeps the container)
    ./lab.sh status                container state, row counts, vector health

  data
    ./lab.sh scrape [--refresh]    re-scrape HN / RemoteOK / Arbeitnow into fixtures/
    ./lab.sh synth [--count N]     regenerate synthetic candidates from that corpus
    ./lab.sh load [--truncate]     fixtures/ -> postgres (dedupes reposts)
    ./lab.sh embed [--backend B]   embed everything that needs it
    ./lab.sh doctor                field coverage — read this before trusting a filter

  play
    ./lab.sh candidates [--focus data]     list candidates
    ./lab.sh jobs [--q kubernetes]         list jobs
    ./lab.sh suggest-jobs <candidate_id>       [--scoring cosine|reciprocal]
    ./lab.sh suggest-candidates <job_id>       [--filters strict|lenient|off]
                                               [--congestion 1.0] [--rerank]
    ./lab.sh serve                 web UI on http://localhost:8000

  learn
    ./lab.sh list                  list scenarios
    ./lab.sh read <n>              print a scenario's README
    ./lab.sh run <n>               run its proof
    ./lab.sh run-all               run every proof; non-zero if any claim fails

  poke at it
    ./lab.sh psql [args...]        plain psql
    ./lab.sh compare-embedders     rank the same query under each backend
    ./lab.sh index build|drop      create/drop the HNSW indexes

Connection string:  postgresql://lab:lab@localhost:5435/lab
EOF
}

cmd="${1:-help}"; shift || true

case "$cmd" in

up)
    compose up -d
    need_psql; wait_ready
    bold "postgres ready on :5435"
    dim  "schema applied from schema/ on first boot"
    echo
    bold "next:  ./lab.sh seed"
    ;;

down)
    compose down -v
    bold "database deleted"
    ;;

reset)
    need_psql
    psql "$DSN" -X -q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    psql "$DSN" -X -q -v ON_ERROR_STOP=1 -f schema/01_schema.sql
    bold "schema re-applied (all data gone)"
    ;;

seed)
    need_uv
    uv run --quiet ingest/load.py --truncate "$@"
    uv run --quiet ingest/embed_all.py
    echo
    bold "ready — try:  ./lab.sh suggest-jobs 4"
    ;;

status)
    compose ps 2>/dev/null || true
    echo
    need_uv; uv run --quiet match/cli.py doctor
    ;;

logs)    compose logs "$@" ;;
psql)    need_psql; psql "$DSN" "$@" ;;

scrape)  need_uv; uv run --quiet ingest/scrape.py "$@" ;;
synth)   need_uv; uv run --quiet ingest/synth.py "$@" ;;
load)    need_uv; uv run --quiet ingest/load.py "$@" ;;
embed)   need_uv; uv run --quiet ingest/embed_all.py "$@" ;;

doctor|candidates|jobs|suggest-jobs|suggest-candidates)
    need_uv; uv run --quiet match/cli.py "$cmd" "$@" ;;

serve)
    need_uv
    bold "http://localhost:${PORT:-8000}"
    uv run --quiet web/server.py "$@"
    ;;

compare-embedders)
    need_uv; uv run --quiet match/compare_embedders.py "$@" ;;

index)
    need_psql
    case "${1:-build}" in
      build)
        bold "building HNSW indexes (this is the slow part — that is the lesson)"
        time psql "$DSN" -X -q -v ON_ERROR_STOP=1 \
          -c "SET maintenance_work_mem = '512MB';" \
          -c "CREATE INDEX IF NOT EXISTS job_docs_hnsw ON job_docs
               USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);" \
          -c "CREATE INDEX IF NOT EXISTS resumes_hnsw ON resumes
               USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);"
        psql "$DSN" -X -c "\di+ *hnsw*"
        ;;
      drop)
        psql "$DSN" -X -q -c "DROP INDEX IF EXISTS job_docs_hnsw; DROP INDEX IF EXISTS resumes_hnsw;"
        bold "HNSW indexes dropped"
        ;;
      *) die "usage: ./lab.sh index build|drop" ;;
    esac
    ;;

list)
    bold "scenarios"
    for d in scenarios/*/; do
        [ -f "$d/README.md" ] || continue
        n=$(basename "$d")
        title=$(head -1 "$d/README.md" | sed 's/^#* *//')
        printf '  %-26s %s\n' "$n" "$title"
    done
    echo
    dim "./lab.sh read <n>   ./lab.sh run <n>   ./lab.sh run-all"
    ;;

read)
    [ $# -ge 1 ] || die "usage: ./lab.sh read <n>"
    d=$(scenario_dir "$1")
    if command -v glow >/dev/null; then glow "$d/README.md"; else cat "$d/README.md"; fi
    ;;

run)
    [ $# -ge 1 ] || die "usage: ./lab.sh run <n>"
    need_uv
    d=$(scenario_dir "$1"); shift
    uv run --quiet "$d/run.py" "$@"
    ;;

run-all)
    need_uv
    failed=()
    for d in scenarios/*/; do
        [ -f "$d/run.py" ] || continue
        echo
        bold "=== $(basename "$d") ==="
        if ! uv run --quiet "$d/run.py"; then failed+=("$(basename "$d")"); fi
    done
    echo
    if [ ${#failed[@]} -gt 0 ]; then
        die "FAILED: ${failed[*]}"
    fi
    bold "all scenarios held"
    ;;

help|-h|--help) usage ;;
*) usage; die "unknown command: $cmd" ;;
esac

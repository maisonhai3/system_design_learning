# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fetch real job postings from public APIs into fixtures/jobs.jsonl.

    ./lab.sh scrape                    # all sources, default volume
    ./lab.sh scrape --source hn --months 6
    ./lab.sh scrape --refresh          # ignore the HTTP cache

WHY NOT LINKEDIN. The obvious answer to "where do I get job data" is LinkedIn,
and it is the wrong one. Their User Agreement prohibits scraping outright, the
pages are behind an auth wall with active anti-bot measures, and the markup is
obfuscated and rotated -- so a LinkedIn scraper is a thing you fix every week
rather than a thing you build once. None of that difficulty teaches you
anything about recommendation.

So this lab pulls from three public, documented, no-auth job feeds instead. The
data is real, messy, and written by humans, which is everything the matching
engine needs to be interesting:

  hn         Hacker News "Ask HN: Who is hiring?" via the public Algolia API.
             Long free-text posts. The best source in the lab for embeddings,
             because the text is genuinely unstructured prose.
  remoteok   https://remoteok.com/api -- structured, tagged, remote-only.
             Their API terms ask for attribution; see the README, which links
             back to Remote OK as required.
  arbeitnow  https://arbeitnow.com/api/job-board-api -- structured, EU-heavy.
             Adds non-US locations, which is what makes the location filter in
             scenarios/02 do anything.

Everything lands in fixtures/ as JSONL so the rest of the lab never needs the
network again, and so a run is reproducible when the feeds have moved on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lab.text import (  # noqa: E402
    clean_html,
    detect_country,
    detect_employment,
    detect_remote,
    detect_seniority,
    extract_skills,
    implied_years,
    parse_salary,
    parse_years,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CACHE = FIXTURES / ".httpcache"

# Identify the client honestly and leave a way to be contacted. An anonymous
# scraper is the one that gets blocked first, and deservedly.
USER_AGENT = (
    "two-sided-matching-lab/0.1 "
    "(+https://github.com/maisonhai3/system_design_learning; educational)"
)
POLITE_DELAY = 0.4   # seconds between requests to the same host
TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP with a disk cache
# ---------------------------------------------------------------------------

def fetch_json(url: str, *, refresh: bool = False, attempts: int = 4):
    """GET JSON, with an on-disk cache and exponential backoff.

    The cache is not an optimisation, it is manners: re-running the loader
    while you iterate on parsing should not re-download a public API twenty
    times. `--refresh` is the escape hatch.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = CACHE / f"{key}.json"

    if path.exists() and not refresh:
        return json.loads(path.read_text())

    delay = 2.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            path.write_text(json.dumps(data))
            time.sleep(POLITE_DELAY)
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if attempt == attempts:
                break
            print(f"    retry {attempt}/{attempts - 1} after {delay:.0f}s ({exc})", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")


# ---------------------------------------------------------------------------
# Hacker News: Ask HN: Who is hiring?
# ---------------------------------------------------------------------------

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEMS = "https://hn.algolia.com/api/v1/search"

_ROLE_RE = re.compile(
    r"engineer|developer|scientist|designer|manager|architect|analyst|"
    r"devops|sre|founder|cto|lead|intern|researcher|programmer|admin|"
    r"consultant|specialist|director|head\s+of|full[- ]?stack|frontend|backend",
    re.I,
)
_URL_RE = re.compile(r"https?://\S+")


def _hn_split_header(header: str) -> tuple[str | None, str | None]:
    """Pull (company, title) out of the ``A | B | C | D`` convention.

    About 87% of top-level posts follow it. The other 13% are prose, and for
    those the company is unknown and the title is the first clause -- which is
    fine: the embedding still works, only the structured filters degrade. That
    graceful degradation is the point. A pipeline that requires clean structure
    from a source that does not have it just drops 13% of the market on the
    floor and never tells you.
    """
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < 2:
        return (None, header[:120] or None)

    company = _URL_RE.sub("", parts[0]).strip(" -–—()") or None
    if company and len(company) > 60:
        company = None

    title = next((p for p in parts[1:] if _ROLE_RE.search(p)), None)
    if title is None:
        title = parts[1]
    title = _URL_RE.sub("", title).strip(" -–—")
    return (company, title[:200] or None)


def scrape_hn(months: int, refresh: bool) -> list[dict]:
    """Top-level comments from the last `months` Who-is-hiring threads."""
    print(f"  hn: finding the last {months} 'Who is hiring' threads")
    query = urllib.parse.urlencode({
        "tags": "story,author_whoishiring",
        "query": "hiring",
        "hitsPerPage": months + 4,
    })
    threads = [
        h for h in fetch_json(f"{HN_SEARCH}?{query}", refresh=refresh)["hits"]
        if "who is hiring" in (h.get("title") or "").lower()
    ][:months]

    records: list[dict] = []
    for thread in threads:
        story_id = int(thread["objectID"])
        title = thread.get("title", "")
        # hitsPerPage caps at 1000; the busiest threads run to ~800 comments,
        # so one request per thread is enough and paging is not needed.
        params = urllib.parse.urlencode({
            "tags": f"comment,story_{story_id}",
            "hitsPerPage": 1000,
        })
        payload = fetch_json(f"{HN_ITEMS}?{params}", refresh=refresh)
        hits = payload["hits"]

        # Only TOP-LEVEL comments are job posts. Replies ("I applied, please
        # take a look") share the story tag and would otherwise be ingested as
        # jobs -- a two-word "job" with a garbage embedding that then shows up
        # as a recommendation for whoever else wrote two words.
        posts = [h for h in hits if h.get("parent_id") == story_id and h.get("comment_text")]
        print(f"    {title}: {len(posts)} posts (of {len(hits)} comments)")

        for post in posts:
            body = clean_html(post["comment_text"])
            if len(body) < 120:
                continue
            header = body.split("\n", 1)[0]
            company, role = _hn_split_header(header)
            lo, hi = parse_salary(header)
            seniority = detect_seniority(header) or detect_seniority(body)
            records.append({
                "source": "hn",
                "source_id": str(post["objectID"]),
                "company": company or "Unknown (HN)",
                "title": role or "Software Engineer",
                "raw_text": body,
                "location": header[:160],
                "country": detect_country(header),
                "remote": detect_remote(header),
                "employment": detect_employment(header) or "fulltime",
                "salary_min": lo,
                "salary_max": hi,
                "currency": "USD",
                "seniority": seniority,
                "min_years_exp": parse_years(body),
                "posted_at": post.get("created_at"),
                "source_url": f"https://news.ycombinator.com/item?id={post['objectID']}",
            })
    return records


# ---------------------------------------------------------------------------
# Remote OK
# ---------------------------------------------------------------------------

REMOTEOK_API = "https://remoteok.com/api"


def scrape_remoteok(refresh: bool) -> list[dict]:
    print("  remoteok: fetching feed")
    payload = fetch_json(REMOTEOK_API, refresh=refresh)
    # Element 0 is a legal/attribution notice, not a job. Ingesting it produces
    # a "job" whose text is the API terms of service.
    rows = [r for r in payload if isinstance(r, dict) and r.get("id")]

    records = []
    for row in rows:
        body = clean_html(row.get("description"))
        # clean_html, not .strip(): these short fields are entity-escaped too,
        # and "St. Regis Hotels &amp; Resorts" is not a company name.
        title = clean_html(row.get("position"))
        if not title or len(body) < 80:
            continue
        tags = [clean_html(t) for t in (row.get("tags") or []) if isinstance(t, str)]
        loc = clean_html(row.get("location"))

        # RemoteOK gives numeric salary fields, but ~95% of rows have 0.
        # 0 means "not stated", NOT "this job pays nothing" -- coercing it to a
        # real number would put every unpriced job below every priced one.
        lo = row.get("salary_min") or None
        hi = row.get("salary_max") or None
        lo = lo if isinstance(lo, (int, float)) and lo >= 10_000 else None
        hi = hi if isinstance(hi, (int, float)) and hi >= 10_000 else None
        if lo is None and hi is None:
            lo, hi = parse_salary(body[:400])

        records.append({
            "source": "remoteok",
            "source_id": str(row["id"]),
            "company": clean_html(row.get("company")) or "Unknown",
            "title": title[:200],
            # Tags are curated metadata the free text often omits; folding them
            # into the embedded document is a cheap, real recall win.
            "raw_text": f"{title}\n{loc}\n{' '.join(tags)}\n\n{body}",
            "location": loc or "Remote",
            "country": detect_country(loc) or "WW",
            "remote": True,                     # the entire board is remote
            "employment": detect_employment(body) or "fulltime",
            "salary_min": int(lo) if lo else None,
            "salary_max": int(hi) if hi else None,
            "currency": "USD",
            "seniority": detect_seniority(title) or detect_seniority(body[:600]),
            "min_years_exp": parse_years(body),
            "posted_at": row.get("date"),
            "source_url": row.get("url"),
            "tags": tags,
        })
    print(f"    {len(records)} jobs")
    return records


# ---------------------------------------------------------------------------
# Arbeitnow
# ---------------------------------------------------------------------------

ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"


def scrape_arbeitnow(pages: int, refresh: bool) -> list[dict]:
    print(f"  arbeitnow: fetching {pages} page(s)")
    records = []
    for page in range(1, pages + 1):
        payload = fetch_json(f"{ARBEITNOW_API}?page={page}", refresh=refresh)
        rows = payload.get("data") or []
        if not rows:
            break
        for row in rows:
            body = clean_html(row.get("description"))
            title = clean_html(row.get("title"))
            if not title or len(body) < 80:
                continue
            loc = clean_html(row.get("location"))
            tags = [clean_html(t) for t in (row.get("tags") or []) if isinstance(t, str)]
            records.append({
                "source": "arbeitnow",
                "source_id": row.get("slug") or str(row.get("id")),
                "company": clean_html(row.get("company_name")) or "Unknown",
                "title": title[:200],
                "raw_text": f"{title}\n{loc}\n{' '.join(tags)}\n\n{body}",
                "location": loc or "Germany",
                "country": detect_country(loc) or detect_country(body[:300]) or "DE",
                "remote": bool(row.get("remote")),
                "employment": detect_employment(body) or "fulltime",
                "salary_min": None,
                "salary_max": None,
                # Not USD. Recorded rather than converted -- see parse_salary.
                "currency": "EUR",
                "seniority": detect_seniority(title) or detect_seniority(body[:600]),
                "min_years_exp": parse_years(body),
                "posted_at": None,
                "source_url": row.get("url"),
                "tags": tags,
            })
        print(f"    page {page}: {len(rows)} rows")
    return records


# ---------------------------------------------------------------------------
# Finalise
# ---------------------------------------------------------------------------

def enrich(records: list[dict]) -> list[dict]:
    """Add derived fields every record needs, and drop unusable rows."""
    out, seen = [], set()
    for rec in records:
        key = (rec["source"], rec["source_id"])
        if key in seen:
            continue
        seen.add(key)

        rec["skills"] = extract_skills(rec["raw_text"])
        if rec.get("min_years_exp") is None:
            # Fall back to what the seniority word implies, and say so, so the
            # filter scenario can separate stated requirements from guessed
            # ones instead of trusting all of them equally.
            rec["min_years_exp"] = implied_years(rec.get("seniority"))
            rec["years_inferred"] = rec["min_years_exp"] is not None
        else:
            rec["years_inferred"] = False

        # Visa sponsorship is stated far less often than it matters. Absence of
        # the phrase is not a "no", but for a hard filter you must pick one --
        # this lab picks False and scenarios/02 makes you feel the consequence.
        text = rec["raw_text"].lower()
        rec["sponsors_visa"] = any(
            phrase in text
            for phrase in ("visa sponsor", "sponsor visa", "sponsorship available",
                           "we sponsor", "h1b", "h-1b", "work permit", "relocation")
        )
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["all", "hn", "remoteok", "arbeitnow"], default="all")
    ap.add_argument("--months", type=int, default=4, help="HN threads to pull")
    ap.add_argument("--pages", type=int, default=3, help="Arbeitnow pages to pull")
    ap.add_argument("--refresh", action="store_true", help="bypass the HTTP cache")
    ap.add_argument("--out", default=str(FIXTURES / "jobs.jsonl"))
    args = ap.parse_args()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    print("Scraping public job feeds (not LinkedIn -- see the module docstring).")

    records: list[dict] = []
    failures: list[str] = []
    plan = ["hn", "remoteok", "arbeitnow"] if args.source == "all" else [args.source]

    for name in plan:
        try:
            if name == "hn":
                records += scrape_hn(args.months, args.refresh)
            elif name == "remoteok":
                records += scrape_remoteok(args.refresh)
            else:
                records += scrape_arbeitnow(args.pages, args.refresh)
        except Exception as exc:                      # noqa: BLE001
            # One dead feed must not cost you the other two. Feeds go down,
            # rate-limit, and change shape; a scraper that is all-or-nothing is
            # a scraper that is usually nothing.
            print(f"  !! {name} failed: {exc}", file=sys.stderr)
            failures.append(name)

    if not records:
        print("\nNo records fetched. fixtures/jobs.jsonl left untouched; the "
              "checked-in snapshot still works offline.", file=sys.stderr)
        return 1

    records = enrich(records)
    out = pathlib.Path(args.out)
    with out.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_source: dict[str, int] = {}
    for rec in records:
        by_source[rec["source"]] = by_source.get(rec["source"], 0) + 1

    print(f"\nWrote {len(records)} jobs to {out.relative_to(ROOT)}")
    for name, count in sorted(by_source.items()):
        print(f"  {name:10s} {count:5d}")
    if failures:
        print(f"  (failed: {', '.join(failures)})")
    print("\nNext: ./lab.sh load")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

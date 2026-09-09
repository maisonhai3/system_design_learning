"""Generate synthetic candidates whose skills come from the real job corpus.

WHY SYNTHETIC. Job postings are published documents; resumes are not. A real
resume is a named person's contact details, employment history and location --
the scrapeable ones are scraped without consent, and putting a pile of them in
a teaching repo is a privacy problem no amount of "it's just a toy" fixes. So
the jobs here are real and the candidates are invented.

WHY NOT INVENTED FROM NOTHING. If candidates were built from a hand-written
skill list, half of them would ask for technologies nobody in the corpus hires
for, and the recommender would look broken when it was actually correct. So the
personas below are mined from skill CO-OCCURRENCE in the scraped jobs: whatever
the real market is asking for this month is what these candidates know.

THE LEAKAGE TRAP, which is the part worth stealing for your own work: the
tempting shortcut is to build each resume by copying sentences out of the job
you want it to match. Do that and every evaluation you run afterwards is a lie
-- the resume contains the job, so cosine similarity is ~1.0, retrieval looks
perfect, and you have measured string equality with extra steps. These resumes
share only VOCABULARY with the corpus, never phrasing: the sentence templates
are written from the worker's point of view ("shipped", "owned", "on-call for")
and never reuse a posting's wording. The scores you see are therefore real, and
lower than the ones a leaky generator would have shown you.
"""

from __future__ import annotations

import collections
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from lab.text import SKILL_ALIASES  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

FIRST_NAMES = """
Mai Linh Hoang Tuan Ngoc Trang Minh Anh Quan Thu Phuong Duc Ha Nam Lan Khanh
Aiko Kenji Yuki Haruto Sora Ravi Priya Arjun Ananya Rohan Kavya Dev Isha
Chen Wei Ling Jing Hao Mei Yan Xiu Sofia Mateo Lucia Diego Camila Alejandro
Emma Liam Olivia Noah Ava Ethan Mia Lucas Amara Kwame Zainab Chidi Fatima Omar
Anna Jakub Katarzyna Piotr Lars Ingrid Sven Freya Pierre Camille Luc Marion
""".split()

LAST_NAMES = """
Nguyen Tran Le Pham Hoang Vu Dang Bui Do Ho Ngo Duong Ly
Tanaka Suzuki Sato Watanabe Ito Sharma Patel Iyer Reddy Nair
Wang Li Zhang Liu Yang Huang Zhao Garcia Rodriguez Martinez Lopez Hernandez
Smith Johnson Williams Brown Jones Miller Davis Okafor Mensah Adeyemi Diallo
Kowalski Nowak Wojcik Andersson Nilsson Larsen Dubois Moreau Laurent Rossi
""".split()

# Where a candidate can be. Deliberately overlaps the country codes the scraper
# emits, or the location filter would reject everything and the lab would look
# broken when it was merely inconsistent.
LOCATIONS = [
    ("Ho Chi Minh City", "VN"), ("Hanoi", "VN"), ("Singapore", "SG"),
    ("Bangalore", "IN"), ("Berlin", "DE"), ("Munich", "DE"), ("London", "UK"),
    ("Manchester", "UK"), ("Amsterdam", "NL"), ("Paris", "FR"), ("Warsaw", "PL"),
    ("Toronto", "CA"), ("Vancouver", "CA"), ("San Francisco, CA", "US"),
    ("New York, NY", "US"), ("Austin, TX", "US"), ("Seattle, WA", "US"),
    ("Boston, MA", "US"), ("Sydney", "AU"), ("Tokyo", "JP"),
]

SENIORITY_BANDS = [
    # label,      years,      base salary expectation (USD)
    ("junior",    (0.5, 2.5),  (45_000,  85_000)),
    ("mid",       (2.5, 5.5),  (80_000, 140_000)),
    ("senior",    (5.0, 9.0),  (120_000, 200_000)),
    ("staff",     (8.0, 14.0), (170_000, 260_000)),
    ("principal", (11.0, 20.0),(200_000, 350_000)),
]

TITLE_BY_FOCUS = {
    "backend":   ["Backend Engineer", "Software Engineer", "Platform Engineer", "API Engineer"],
    "frontend":  ["Frontend Engineer", "Web Engineer", "UI Engineer", "Product Engineer"],
    "data":      ["Data Engineer", "Analytics Engineer", "Data Platform Engineer"],
    "ml":        ["Machine Learning Engineer", "Applied Scientist", "ML Platform Engineer"],
    "infra":     ["Site Reliability Engineer", "DevOps Engineer", "Infrastructure Engineer"],
    "mobile":    ["Mobile Engineer", "iOS Engineer", "Android Engineer"],
    "security":  ["Security Engineer", "AppSec Engineer", "Security Analyst"],
    "fullstack": ["Full Stack Engineer", "Product Engineer", "Software Engineer"],
}

# Seed skills that define each focus. Everything else in a persona is pulled
# from what actually co-occurs with these in the scraped corpus.
FOCUS_SEEDS = {
    "backend":   ["postgres", "go", "python", "java", "grpc", "microservices"],
    "frontend":  ["react", "typescript", "css", "next.js", "vue", "tailwind"],
    "data":      ["spark", "airflow", "dbt", "snowflake", "bigquery", "kafka"],
    "ml":        ["pytorch", "machine learning", "llm", "nlp", "transformers", "rag"],
    "infra":     ["kubernetes", "terraform", "aws", "prometheus", "docker", "sre"],
    "mobile":    ["ios", "android", "swift", "kotlin", "react native", "flutter"],
    "security":  ["security", "cryptography", "observability"],
    "fullstack": ["react", "python", "postgres", "typescript", "django", "node"],
}

# Written from the worker's side, never the posting's. See the leakage note.
ACHIEVEMENTS = [
    "Shipped {a} services handling {n}k requests/day, cutting p99 latency by {p}%.",
    "Owned the {a} migration end to end; {n} tables moved with no downtime.",
    "Built internal tooling in {a} that took the release cycle from weekly to daily.",
    "On-call rotation for {a} infrastructure; drove incident count down {p}% in two quarters.",

    "Reduced {a} infrastructure spend by {p}% by rightsizing and killing dead workloads.",
    "Wrote the {a} integration the rest of the company now builds on.",
    "Instrumented {a} with tracing and dashboards; mean time to detect fell from hours to minutes.",
    "Took the {a} test suite from flaky to trustworthy, unblocking continuous deploys.",
    "Designed the {a} data model that survived a {n}x growth in traffic.",
]

SUMMARIES = [
    "{sen} {title} with {yrs} years building {focus} systems.",
    "{title}, {yrs} years. Most of that time in {focus}, at companies between seed and Series C.",
    "{sen} engineer focused on {focus}. {yrs} years, mostly hands-on.",
    "{yrs} years as a {title}. Comfortable owning {focus} work from design through on-call.",
]

# Only offered to staff/principal. A "junior with 2 years" who "led a team of 6"
# reads as generated, and a corpus that reads as generated is one you stop
# trusting your own eyeball checks against.
LEAD_ACHIEVEMENTS = [
    "Led a team of {t} rebuilding the {a} pipeline after it stopped scaling past {n}k events/day.",
    "Set the {a} technical direction for three teams and wrote the migration plan they followed.",
    "Mentored {t} engineers on {a}; two of them now run their own services.",
]

CLOSERS = [
    "Looking for a role where I can keep writing code rather than only reviewing it.",
    "Interested in small teams with real users and short feedback loops.",
    "Want to work somewhere the {focus} problems are the hard part, not an afterthought.",
    "Prefer teams that deploy often and treat reliability as a feature.",
    "Open to a step up in scope; happy to stay hands-on.",
]


def mine_cooccurrence(jobs: list[dict]) -> dict[str, collections.Counter]:
    """skill -> Counter of skills that appear in the same posting."""
    co: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for job in jobs:
        skills = job.get("skills") or []
        for a in skills:
            for b in skills:
                if a != b:
                    co[a][b] += 1
    return co


def build_personas(jobs: list[dict]) -> dict[str, list[str]]:
    """Expand each focus's seed skills with what really co-occurs with them.

    This is the bit that keeps the candidates honest. A persona is not "what I
    think an ML engineer knows", it is "what the postings that mention pytorch
    also mention", computed from this month's actual market.
    """
    co = mine_cooccurrence(jobs)
    present = collections.Counter(s for j in jobs for s in (j.get("skills") or []))

    personas: dict[str, list[str]] = {}
    for focus, seeds in FOCUS_SEEDS.items():
        live_seeds = [s for s in seeds if present.get(s, 0) >= 3]
        if not live_seeds:
            live_seeds = seeds[:2]        # corpus is thin; keep the persona anyway
        pool: collections.Counter = collections.Counter()
        for seed in live_seeds:
            pool.update(co.get(seed, {}))
        for seed in live_seeds:
            pool.pop(seed, None)
        neighbours = [s for s, _ in pool.most_common(14)]
        personas[focus] = live_seeds + neighbours
    return personas


def _salary_expectation(rng: random.Random, band: tuple[int, int], country: str) -> int:
    lo, hi = band
    # Crude cost-of-market adjustment. Without it every non-US candidate asks
    # for US money, the salary filter rejects the entire non-US corpus, and
    # scenarios/02 shows a filter cliff that is an artefact of the generator
    # rather than a property of the data.
    factor = {"US": 1.0, "UK": 0.75, "CA": 0.75, "DE": 0.65, "NL": 0.65,
              "FR": 0.6, "AU": 0.7, "JP": 0.55, "SG": 0.6, "PL": 0.4,
              "IN": 0.25, "VN": 0.22}.get(country, 0.6)
    base = rng.randint(lo, hi)
    return int(round(base * factor / 1000.0)) * 1000


def _resume_text(rng: random.Random, *, name, title, sen, years, focus, skills, location) -> str:
    # Clamp both ends: a thin corpus can yield a persona with fewer skills
    # than the sample size, and randint(6, 4) raises rather than shrinking.
    hi = min(12, len(skills))
    top = skills[: rng.randint(min(6, hi), hi)] if hi else skills
    lines = [
        name,
        f"{title} — {location}",
        "",
        rng.choice(SUMMARIES).format(
            sen=sen.capitalize(), title=title, yrs=f"{years:.0f}", focus=focus
        ),
        "",
        "Skills: " + ", ".join(top),
        "",
        "Experience",
    ]
    pool = ACHIEVEMENTS + (LEAD_ACHIEVEMENTS if sen in ("staff", "principal") else [])
    for _ in range(rng.randint(3, 5)):
        lines.append("- " + rng.choice(pool).format(
            a=rng.choice(top), n=rng.choice([5, 12, 40, 120, 400]),
            p=rng.choice([15, 22, 30, 45, 60]), t=rng.randint(3, 9),
        ))
    lines += ["", rng.choice(CLOSERS).format(focus=focus)]
    return "\n".join(lines)


def generate(jobs: list[dict], count: int, seed: int = 20260909) -> list[dict]:
    """Deterministic given (jobs, count, seed) -- scenarios assert on numbers."""
    rng = random.Random(seed)
    personas = build_personas(jobs)
    focuses = list(personas)

    candidates: list[dict] = []
    for i in range(count):
        focus = focuses[i % len(focuses)]          # even coverage, not random
        pool = personas[focus]
        hi = min(13, len(pool))
        n_skills = rng.randint(min(5, hi), hi)
        skills = sorted({SKILL_ALIASES.get(s, s) for s in rng.sample(pool, n_skills)})

        sen, (y_lo, y_hi), pay_band = rng.choice(SENIORITY_BANDS)
        years = round(rng.uniform(y_lo, y_hi), 1)
        city, country = rng.choice(LOCATIONS)
        title = rng.choice(TITLE_BY_FOCUS[focus])
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        name = f"{first} {last}"

        remote_required = rng.random() < 0.45
        candidates.append({
            "external_id": f"cand-{i + 1:04d}",
            "full_name": name,
            "email": f"{first.lower()}.{last.lower()}{i + 1}@example.invalid",
            "headline": f"{sen.capitalize()} {title}",
            "years_exp": years,
            "seniority": sen,
            "current_title": title,
            "location": city,
            "country": country,
            "focus": focus,
            "skills": skills,
            "raw_text": _resume_text(rng, name=name, title=title, sen=sen,
                                     years=years, focus=focus, skills=skills,
                                     location=city),
            "prefs": {
                "min_salary": _salary_expectation(rng, pay_band, country),
                "remote_required": remote_required,
                # A remote-only candidate still names countries they can work
                # in; timezone and right-to-work do not disappear because the
                # job is remote. Modelling remote as "location no longer
                # applies" is the single most common bug in job matching.
                "willing_locations": sorted({country} | (
                    {"WW"} if remote_required else set()
                ) | ({rng.choice(LOCATIONS)[1]} if rng.random() < 0.3 else set())),
                "needs_visa": rng.random() < 0.25,
                "open_to_contract": rng.random() < 0.5,
                "min_company_size": rng.choice([None, None, None, 10, 50, 200]),
            },
        })

    _add_edge_cases(candidates, rng)
    return candidates


def _add_edge_cases(candidates: list[dict], rng: random.Random) -> None:
    """Overwrite a few candidates with hand-built awkward ones.

    Every scenario in this lab needs a case that breaks the naive answer. Random
    generation will not reliably produce one, so these are pinned. They are
    placed at known indices and named, so the scenarios can reference them.
    """
    if len(candidates) < 8:
        return

    # 1. Textually perfect, financially impossible. Ranks #1 on cosine for
    #    plenty of jobs and is eligible for almost none. This is the candidate
    #    that proves similarity and eligibility are different questions.
    c = candidates[0]
    c.update(full_name="Priya Iyer", headline="Principal Platform Engineer",
             seniority="principal", years_exp=15.0, current_title="Principal Platform Engineer",
             location="San Francisco, CA", country="US", focus="infra",
             skills=sorted({"kubernetes", "terraform", "aws", "go", "postgres",
                            "observability", "prometheus", "sre"}))
    c["prefs"].update(min_salary=400_000, remote_required=True,
                      willing_locations=["US", "WW"], needs_visa=False)
    c["raw_text"] = (
        "Priya Iyer\nPrincipal Platform Engineer — San Francisco, CA\n\n"
        "Principal engineer, 15 years, all of it in infrastructure.\n\n"
        "Skills: kubernetes, terraform, aws, go, postgres, observability, prometheus, sre\n\n"
        "Experience\n"
        "- Ran the platform team for a fleet of 4,000 kubernetes nodes across three regions.\n"
        "- Owned the terraform monorepo every other team deploys through.\n"
        "- On-call for the control plane; drove sev1 count down 60% over two years.\n"
        "- Wrote the observability standard the whole engineering org now follows.\n\n"
        "Only interested in principal scope, fully remote."
    )

    # 2. Needs sponsorship. Only ~7% of the corpus says anything about visas,
    #    so this candidate makes the "absence of evidence" filter decision in
    #    scenarios/02 cost something visible.
    c = candidates[1]
    c.update(full_name="Chidi Okafor", headline="Senior Backend Engineer",
             seniority="senior", years_exp=7.0, current_title="Senior Backend Engineer",
             location="Lagos", country="NG", focus="backend")
    c["prefs"].update(needs_visa=True, remote_required=False,
                      willing_locations=["UK", "DE", "CA", "US"], min_salary=70_000)

    # 3. The generalist: weak-but-nonzero similarity to nearly everything.
    #    Never anyone's top match, always in everyone's top 200. This is who
    #    congestion control has to stop crowding out.
    c = candidates[2]
    c.update(full_name="Alex Moreau", headline="Software Engineer",
             seniority="mid", years_exp=4.0, current_title="Software Engineer",
             location="Paris", country="FR", focus="fullstack",
             skills=sorted({"python", "javascript", "sql", "docker", "aws", "react"}))
    c["prefs"].update(min_salary=55_000, remote_required=False,
                      willing_locations=["FR", "EU", "UK"], needs_visa=False)
    c["raw_text"] = (
        "Alex Moreau\nSoftware Engineer — Paris\n\n"
        "Software engineer, 4 years, a bit of everything.\n\n"
        "Skills: python, javascript, sql, docker, aws, react\n\n"
        "Experience\n"
        "- Worked across the stack on a mid-sized web product.\n"
        "- Some data work, some frontend, some deployment.\n"
        "- Comfortable picking up whatever the team needs.\n\n"
        "Open to most things."
    )

    # 4 & 5. Near-identical twins. Whatever the ranker does to separate these
    #    is arbitrary, and if the arbitrary choice is stable then one of them
    #    is permanently invisible. scenarios/04 measures exactly that.
    for idx, (name, email) in enumerate([("Yuki Tanaka", "yuki.tanaka.a@example.invalid"),
                                         ("Sora Watanabe", "sora.watanabe.b@example.invalid")]):
        c = candidates[3 + idx]
        c.update(full_name=name, email=email, headline="Senior Data Engineer",
                 seniority="senior", years_exp=6.0, current_title="Senior Data Engineer",
                 location="Tokyo", country="JP", focus="data",
                 skills=sorted({"spark", "airflow", "dbt", "snowflake", "kafka", "python", "sql"}))
        c["prefs"].update(min_salary=90_000, remote_required=True,
                          willing_locations=["JP", "WW"], needs_visa=False)
        c["raw_text"] = (
            f"{name}\nSenior Data Engineer — Tokyo\n\n"
            "Senior data engineer with 6 years building batch and streaming pipelines.\n\n"
            "Skills: airflow, dbt, kafka, python, snowflake, spark, sql\n\n"
            "Experience\n"
            "- Owned the airflow deployment running 900 daily tasks.\n"
            "- Moved the warehouse to dbt; model run time down 45%.\n"
            "- Built the kafka ingestion path for clickstream events.\n\n"
            "Looking for remote data platform work."
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Generate synthetic candidates.")
    ap.add_argument("--count", type=int, default=400)
    ap.add_argument("--seed", type=int, default=20260909)
    ap.add_argument("--jobs", default=str(ROOT / "fixtures" / "jobs.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "fixtures" / "candidates.jsonl"))
    args = ap.parse_args(argv)

    jobs_path = pathlib.Path(args.jobs)
    if not jobs_path.exists():
        print(f"No {jobs_path}. Run ./lab.sh scrape first.", file=sys.stderr)
        return 1

    jobs = [json.loads(line) for line in jobs_path.open()]
    candidates = generate(jobs, args.count, args.seed)

    out = pathlib.Path(args.out)
    with out.open("w") as fh:
        for cand in candidates:
            fh.write(json.dumps(cand, ensure_ascii=False) + "\n")

    focuses = collections.Counter(c["focus"] for c in candidates)
    print(f"Wrote {len(candidates)} candidates to {out.relative_to(ROOT)}")
    print("  by focus: " + ", ".join(f"{k}={v}" for k, v in sorted(focuses.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

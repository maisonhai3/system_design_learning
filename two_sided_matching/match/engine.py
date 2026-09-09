"""The matching engine. Read this file and you have the whole idea.

A one-sided recommender answers "what is similar to this?". A two-sided one has
to answer "which pairings should exist?", and those are different questions
because a pairing has to satisfy two parties who want different things and
because a job can only hire so many people.

The pipeline, in the order the stages run:

    1. RETRIEVE    cheap, approximate, wide.  ~2000 -> 200 by vector distance.
    2. FILTER      hard eligibility. Not a ranking signal -- a yes/no gate.
    3. SCORE       two directional scores, combined by harmonic mean.
    4. CONGEST     penalise jobs that already have everyone applying to them.
    5. RERANK      expensive, narrow.  200 -> 20 with what stage 1 cannot see.

Each stage is switchable, because the point of the lab is watching what each
one changes. `Options(scoring="cosine", filters="off")` gives you the naive
system from most tutorials; the default gives you something defensible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

FilterMode = Literal["strict", "lenient", "off"]
Scoring = Literal["cosine", "reciprocal"]
Retrieval = Literal["exact", "ann", "hybrid"]


@dataclass
class Options:
    limit: int = 20
    pool: int = 200
    filters: FilterMode = "strict"
    scoring: Scoring = "reciprocal"
    congestion: float = 0.0          # 0 = off, 1 = punish popularity hard
    rerank: bool = False
    retrieval: Retrieval = "exact"
    explain: bool = True

    # Recommendations already handed out in THIS serving round, {job_id: count}.
    # When present it is used instead of the historical application counts, and
    # the difference between the two is the whole of scenarios/04: a penalty
    # computed from last month's applications cannot correct concentration that
    # this afternoon's ranker is creating. The caller updates this between
    # requests, which makes the recommender stateful across users -- no longer
    # a pure function of (user, catalogue). That is not an implementation
    # detail, it is the price of fixing a cross-user problem, and it brings
    # every consequence of shared mutable state with it.
    serving_load: dict | None = None


@dataclass
class Match:
    """One recommendation, with everything needed to argue about it."""

    id: int
    title: str
    subtitle: str
    sim: float                   # raw cosine, symmetric
    score: float                 # final rank key
    s_a2b: float = 0.0           # how much the asking side should want this
    s_b2a: float = 0.0           # how much this side should want the asker
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    matches: list[Match]
    pool_size: int               # retrieved before filtering
    eligible: int                # survived filtering
    timings_ms: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Hard filters -- eligibility, which is symmetric
# ---------------------------------------------------------------------------
#
# Worth being precise, because it is the thing most write-ups get muddled.
# ELIGIBILITY is a property of the PAIR: either a candidate may hold a job or
# they may not, and both parties' non-negotiables go into that one predicate.
# It is therefore symmetric, and it is the same set of checks whichever
# direction you are searching in.
#
# What is asymmetric is DESIRABILITY -- how much each side should want the
# pairing, given that it is allowed. Those are the two functions below this
# block, and they share no terms at all.
#
# Collapsing the two is the classic bug: you end up either filtering on soft
# preferences (and returning nothing) or ranking on hard constraints (and
# recommending jobs the candidate legally cannot take).

def eligibility(cand: dict, job: dict, mode: FilterMode) -> list[str]:
    """Reasons this pairing is not allowed. Empty list means eligible.

    `mode` decides what an unknown value means, and that decision is the whole
    ballgame on real data: only 17% of the scraped corpus states a salary and
    only 7% mentions visas, so `strict` -- treating unknown as disqualifying --
    throws away most of the market, while `lenient` recommends jobs that turn
    out to pay half what the candidate needs. There is no third option that
    makes the missing data appear. Pick, and know which one you picked.
    """
    if mode == "off":
        return []

    unknown_blocks = mode == "strict"
    blockers: list[str] = []

    if job.get("status") != "open":
        blockers.append("job is not open")

    # -- the candidate's non-negotiables ------------------------------------
    min_salary = cand.get("min_salary")
    if min_salary:
        top = job.get("salary_max") or job.get("salary_min")
        if top is None:
            if unknown_blocks:
                blockers.append("salary not stated")
        elif top < min_salary:
            blockers.append(f"pays up to {top:,} < floor {min_salary:,}")

    if cand.get("remote_required") and not job.get("remote"):
        blockers.append("candidate is remote-only, job is not")

    willing = set(cand.get("willing_locations") or [])
    country = job.get("country")
    if willing:
        if country is None:
            if unknown_blocks:
                blockers.append("job location unknown")
        # "WW" on a job means worldwide-remote, which satisfies any location.
        elif country not in willing and country != "WW":
            blockers.append(f"job in {country}, candidate wants {'/'.join(sorted(willing))}")

    if not cand.get("open_to_contract", True) and job.get("employment") == "contract":
        blockers.append("contract role, candidate wants permanent")

    # -- the employer's non-negotiables -------------------------------------
    needs = job.get("min_years_exp")
    have = cand.get("years_exp")
    if needs is not None and have is not None and have + 1e-9 < needs:
        # +1e-9 because years_exp is a numeric out of Postgres and a candidate
        # with exactly 5.0 years should pass a "5 years" requirement.
        blockers.append(f"needs {needs:g}y, candidate has {have:g}y")

    if cand.get("needs_visa") and not job.get("sponsors_visa"):
        # Absence of the phrase "visa" is not a refusal to sponsor, but a hard
        # filter has to decide. Under `lenient` we let silence mean "maybe".
        if unknown_blocks:
            blockers.append("candidate needs sponsorship, job does not mention it")

    return blockers


# ---------------------------------------------------------------------------
# Directional desirability -- this is what makes it two-sided
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# Floor for any single component of a directional score. Nothing is allowed to
# be exactly zero, because the combiner below multiplies: one zero would make
# the whole side zero and erase every other signal, and a lot of these
# components are zero for want of data rather than want of fit.
_COMPONENT_FLOOR = 0.05


def _combine(parts: list[float]) -> float:
    """Geometric mean of a side's components.

    The arithmetic mean is the obvious choice and it is wrong here, for exactly
    the reason the harmonic mean is right for combining the two SIDES: a
    dealbreaker must not be averageable. scenarios/01 is what caught it -- a
    job paying 110k scored 0.43 for a candidate whose floor is 400k, because
    the pay component was 0.0 and "it is remote" and "it is in your country"
    averaged it back up to almost half marks.

    A geometric mean cannot do that. Multiplying means a component near zero
    drags the product toward zero however good the rest are, which is what
    "dealbreaker" means. Same components, same weights, one different combiner:

        arithmetic(0.0,  0.8, 1.0) = 0.60      <- recommends the job
        geometric (0.05, 0.8, 1.0) = 0.37      <- does not
    """
    if not parts:
        return 0.5

    total = 0.0
    for value in parts:
        total += math.log(max(_COMPONENT_FLOOR, min(1.0, value)))
    return math.exp(total / len(parts))


def candidate_wants(cand: dict, job: dict) -> tuple[float, list[str]]:
    """How much the CANDIDATE should want this job. 0..1.

    Reads job columns against candidate preferences. Note that not one of the
    signals here appears in employer_wants below.
    """
    parts: list[float] = []
    why: list[str] = []

    floor = cand.get("min_salary")
    top = job.get("salary_max") or job.get("salary_min")
    if floor and top:
        # Ratio, not difference: +20k means something different at 60k and at
        # 300k. Saturates at 1.5x -- a job paying triple the floor is not three
        # times more attractive than one paying double, and letting it score
        # that way makes the top of every list the same handful of outliers.
        ratio = top / floor
        parts.append(_clamp01((ratio - 0.9) / 0.6))
        if ratio >= 1.2:
            why.append(f"pays {ratio:.1f}x floor")
    elif floor:
        parts.append(0.4)          # unknown pay is a mild negative, not neutral
        why.append("pay not stated")

    if cand.get("remote_required"):
        parts.append(1.0 if job.get("remote") else 0.0)
    else:
        parts.append(0.8 if job.get("remote") else 0.5)
        if job.get("remote"):
            why.append("remote")

    willing = set(cand.get("willing_locations") or [])
    country = job.get("country")
    if willing and country:
        hit = country in willing or country == "WW"
        parts.append(1.0 if hit else 0.2)
        if hit:
            why.append(f"in {country}")

    size = job.get("company_size")
    want_size = cand.get("min_company_size")
    if want_size and size:
        parts.append(1.0 if size >= want_size else 0.3)

    # A job reposted every month is one the market has been declining. Mild
    # penalty, not a filter: it is weak evidence, and sometimes they are just
    # growing.
    reposts = int((job.get("parsed") or {}).get("repost_count", 1) or 1)
    if reposts >= 3:
        parts.append(0.5)
        why.append(f"reposted {reposts}x")

    return _combine(parts), why


def employer_wants(cand: dict, job: dict) -> tuple[float, list[str]]:
    """How much the EMPLOYER should want this candidate. 0..1."""
    parts: list[float] = []
    why: list[str] = []

    needs = job.get("min_years_exp")
    have = cand.get("years_exp")
    if needs is not None and have is not None:
        if have < needs:
            parts.append(_clamp01(have / max(needs, 0.5)) * 0.5)
        else:
            # Overqualification is a real negative, not a bonus. A principal
            # engineer is not the best applicant for a junior role: they will
            # be expensive, bored, and gone in a year. Scoring "more is better"
            # on experience is why naive rankers put the same twenty senior
            # people at the top of every single posting.
            excess = have - needs
            parts.append(1.0 if excess <= 4 else _clamp01(1.0 - (excess - 4) / 12))
            if excess > 6:
                why.append(f"{have:g}y vs {needs:g}y needed (overqualified)")
            else:
                why.append(f"{have:g}y experience")
    elif have is not None:
        parts.append(0.6)

    ladder = {"junior": 0, "mid": 1, "senior": 2, "staff": 3, "principal": 4}
    js, cs = ladder.get(job.get("seniority") or ""), ladder.get(cand.get("seniority") or "")
    if js is not None and cs is not None:
        gap = abs(js - cs)
        parts.append([1.0, 0.75, 0.4, 0.15, 0.05][min(gap, 4)])
        if gap == 0:
            why.append(f"{cand.get('seniority')} matches")

    job_skills = set((job.get("parsed") or {}).get("skills") or [])
    cand_skills = set(cand.get("skills") or [])
    if job_skills:
        # Coverage of what the JOB asked for, not of what the candidate knows.
        # Dividing by the candidate's skill count instead rewards narrow
        # resumes: someone who lists exactly one skill gets 100% coverage.
        covered = job_skills & cand_skills
        parts.append(len(covered) / len(job_skills))
        if covered:
            why.append("has " + ", ".join(sorted(covered)[:4]))
    elif cand_skills:
        parts.append(0.5)

    if cand.get("needs_visa"):
        parts.append(1.0 if job.get("sponsors_visa") else 0.35)

    return _combine(parts), why


# ---------------------------------------------------------------------------
# Combining the two directions
# ---------------------------------------------------------------------------

def harmonic(a: float, b: float) -> float:
    """Harmonic mean -- the reason reciprocal recommendation works at all.

    Arithmetic mean lets one side carry the other: a job that is perfect for
    the candidate and hopeless for the employer averages to "pretty good" and
    gets recommended, wasting an application and a screening call. The harmonic
    mean is dominated by the SMALLER argument, so a pairing only scores well if
    both sides are happy -- which is the actual definition of a good match.

        arithmetic(1.0, 0.2) = 0.60      harmonic(1.0, 0.2) = 0.33
        arithmetic(0.6, 0.6) = 0.60      harmonic(0.6, 0.6) = 0.60

    Both pairs average the same. Only one of them is a match.
    """
    if a <= 0 or b <= 0:
        return 0.0
    return 2 * a * b / (a + b)


def _normalise(values: list[float]) -> list[float]:
    """Min-max a score column into 0..1 for fusion.

    Cosine on this corpus lives in roughly 0.0-0.4 while the preference scores
    are already 0..1, so blending them raw would let preferences outvote
    similarity by a factor of three -- a difference in weighting that nobody
    chose and nobody can see.

    The cost of min-max is that scores are only comparable WITHIN one result
    set. A 0.9 here does not mean the same thing as a 0.9 from another query,
    so these numbers must never be thresholded ("only show matches above 0.8")
    or stored and compared later. Rank-based fusion (RRF) avoids that at the
    price of discarding the margins.
    """
    if not values:
        return []

    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def congestion_penalty(applicants: int, headcount: int, strength: float) -> float:
    """Multiplier in (0, 1] that shrinks as a job gets more applicants.

    A job with 400 applicants and one opening is, for almost everybody being
    recommended it, a waste of an afternoon -- but similarity has no idea, so
    every ranker without this term keeps piling people onto the same postings.
    That is the two-sided market failing in the way markets fail: the resource
    being allocated is scarce, and a recommender that ignores scarcity is just
    an amplifier for whatever is already popular.

    log1p rather than a linear penalty: the difference between 5 and 50
    applicants matters much more than between 300 and 350.
    """
    if strength <= 0:
        return 1.0

    load = applicants / max(1, headcount)
    return 1.0 / (1.0 + strength * math.log1p(load))


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------

def rerank_score(cand: dict, job: dict) -> tuple[float, list[str]]:
    """Stand-in for the expensive model in stage 5.

    In production this is a cross-encoder or an LLM: it reads both documents
    together, which is what lets it notice things a pair of independent vectors
    cannot -- that "5 years of Python" and "senior Python role" agree, that the
    posting is for a manager and the resume is from an individual contributor.
    It costs 10-100ms per pair, which is exactly why it runs on 200 candidates
    and not on 2,000,000.

    This version is deterministic, free, and instant, so the lab can show you
    the SHAPE of the stage -- how much reordering it does, and where the
    latency budget goes -- without an API key. Swap in a real model and the
    surrounding code does not change; that is the point of keeping it behind
    one function.
    """
    job_skills = set((job.get("parsed") or {}).get("skills") or [])
    cand_skills = set(cand.get("skills") or [])
    why: list[str] = []

    if job_skills and cand_skills:
        inter = job_skills & cand_skills
        # Jaccard, not raw overlap: it penalises a resume that lists forty
        # skills to match everything, which raw overlap rewards.
        jaccard = len(inter) / len(job_skills | cand_skills)
        exact = len(inter) / len(job_skills)
        skill = 0.6 * exact + 0.4 * jaccard
        if inter:
            why.append(f"{len(inter)}/{len(job_skills)} required skills")
    else:
        skill = 0.3

    title = (job.get("title") or "").lower()
    current = (cand.get("current_title") or "").lower()
    title_words = set(title.split()) & set(current.split())
    title_fit = _clamp01(len(title_words) / 3.0)
    if title_words - {"engineer", "senior", "-", "&"}:
        why.append("title overlap")

    # The kind of hard mismatch only a joint read catches.
    manager = any(w in title for w in ("manager", "director", "head of", "vp"))
    ic = not any(w in current for w in ("manager", "director", "head", "lead"))
    penalty = 0.5 if (manager and ic) else 1.0
    if manager and ic:
        why.append("management role, IC background")

    return (0.7 * skill + 0.3 * title_fit) * penalty, why

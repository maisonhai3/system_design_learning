"""Scenario plumbing: sections, tables, and assertions that can fail the build.

Each scenario asserts two kinds of claim:

    NAIVE BROKE   the simple version really does misbehave
    FIX HELD      the proposed fix really does work

The first matters more than it looks. A teaching lab that quietly stops
demonstrating its own problem -- because the corpus was re-scraped, or a
threshold drifted -- is worse than no lab, because you would go on believing
it. `./lab.sh run-all` exits non-zero the moment either kind of claim stops
holding.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"
)


@dataclass
class Claim:
    label: str
    ok: bool
    detail: str


class Scenario:
    def __init__(self, title: str, subtitle: str = ""):
        self.title = title
        self.claims: list[Claim] = []
        print(f"\n{BOLD}{'=' * 78}{RESET}")
        print(f"{BOLD}{title}{RESET}")
        if subtitle:
            print(f"{DIM}{subtitle}{RESET}")
        print(f"{BOLD}{'=' * 78}{RESET}")

    # -- output ------------------------------------------------------------

    def section(self, text: str) -> None:
        print(f"\n{CYAN}{BOLD}--- {text} {'-' * max(0, 72 - len(text))}{RESET}")

    def note(self, text: str) -> None:
        for line in text.strip().splitlines():
            print(f"{DIM}    {line.strip()}{RESET}")

    def fact(self, label: str, value) -> None:
        print(f"    {label:<46s} {BOLD}{value}{RESET}")

    def table(self, headers: list[str], rows: list[list], widths: list[int] | None = None) -> None:
        widths = widths or [max(len(str(h)), 12) for h in headers]
        head = "  ".join(f"{str(h):<{w}}" for h, w in zip(headers, widths))
        print(f"    {DIM}{head}{RESET}")
        print(f"    {DIM}{'-' * len(head)}{RESET}")
        for row in rows:
            print("    " + "  ".join(f"{str(c):<{w}}" for c, w in zip(row, widths)))

    # -- claims ------------------------------------------------------------

    def claim(self, label: str, ok: bool, detail: str = "") -> bool:
        self.claims.append(Claim(label, bool(ok), detail))
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"\n  [{mark}] {BOLD}{label}{RESET}")
        if detail:
            print(f"         {DIM}{detail}{RESET}")
        return bool(ok)

    def broke(self, label: str, ok: bool, detail: str = "") -> bool:
        return self.claim(f"NAIVE BROKE — {label}", ok, detail)

    def held(self, label: str, ok: bool, detail: str = "") -> bool:
        return self.claim(f"FIX HELD — {label}", ok, detail)

    def finish(self) -> int:
        failed = [c for c in self.claims if not c.ok]
        print()
        if failed:
            print(f"{RED}{BOLD}{len(failed)}/{len(self.claims)} claims failed:{RESET}")
            for c in failed:
                print(f"  {RED}x{RESET} {c.label}  {DIM}{c.detail}{RESET}")
            return 1
        print(f"{GREEN}{BOLD}all {len(self.claims)} claims held{RESET}")
        return 0


# ---------------------------------------------------------------------------
# Measurements the scenarios share
# ---------------------------------------------------------------------------

def spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation in [-1, 1]. 1.0 means identical ordering."""
    n = len(a)
    if n < 2:
        return 1.0

    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        out = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            # Average rank over ties, or a column of identical scores would
            # produce an arbitrary ordering and a meaningless correlation.
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    dbv = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return num / (da * dbv) if da and dbv else 1.0


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0


def gini(values: list[float]) -> float:
    """Inequality of a distribution. 0 = perfectly even, 1 = one winner.

    Used on "how many recommendations did each job receive". A recommender
    with a Gini of 0.9 is showing almost everybody the same handful of jobs,
    which is invisible in per-user metrics -- every individual list looks
    great -- and obvious the moment you aggregate across users. This is the
    measurement that makes congestion a fact rather than a worry.
    """
    xs = sorted(float(v) for v in values)
    n = len(xs)
    if n == 0:
        return 0.0
    total = sum(xs)
    if total <= 0:
        return 0.0
    cumulative = sum((i + 1) * x for i, x in enumerate(xs))
    return (2 * cumulative) / (n * total) - (n + 1) / n


def coverage(recommended: list[list[int]], universe: int) -> float:
    """Fraction of the catalogue that appears in anybody's list at all."""
    seen = {item for lst in recommended for item in lst}
    return len(seen) / universe if universe else 0.0


def recall_at_k(truth: list[int], got: list[int], k: int) -> float:
    gold = set(truth[:k])
    return len(gold & set(got[:k])) / len(gold) if gold else 1.0


def bar(value: float, width: int = 30, scale: float = 1.0) -> str:
    filled = int(round(width * min(1.0, value / scale)))
    return "#" * filled + "." * (width - filled)


def exit_with(scenario: Scenario) -> None:
    sys.exit(scenario.finish())

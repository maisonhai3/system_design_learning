"""Turning scraped prose into the structured columns the hard filters need.

This module is the unglamorous half of a recommender and the half that decides
whether it works. Everything downstream -- filters, reciprocal scoring,
congestion control -- reads columns that are produced here, out of text written
by humans who were not thinking about your schema.

The functions are deliberately conservative: they return None rather than
guess. A NULL salary you can reason about; a hallucinated one silently ranks
the wrong jobs forever.
"""

from __future__ import annotations

import html
import re
import unicodedata

# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_TAG_BREAK = re.compile(r"<\s*(?:p|br|/p|/div|/li)\s*/?\s*>", re.I)
_TAG_ANY = re.compile(r"<[^>]+>")
_WS_RUN = re.compile(r"[ \t ]+")
_NL_RUN = re.compile(r"\n{3,}")


def clean_html(raw: str | None) -> str:
    """HTML fragment -> plain text, preserving paragraph breaks.

    Hacker News comments and RemoteOK descriptions are both HTML fragments with
    entity-escaped punctuation (``&#x27;`` for an apostrophe). Left unescaped,
    every contraction tokenises as its own junk term and quietly pollutes the
    TF-IDF vocabulary.
    """
    if not raw:
        return ""
    text = _TAG_BREAK.sub("\n", raw)
    text = _TAG_ANY.sub(" ", text)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _WS_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RUN.sub("\n\n", text).strip()


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

# Ordered most-specific first; the first pattern that yields a sane number wins.
_MONEY = r"(?:\$|USD|EUR|GBP|£|€)?\s*(\d[\d,.]*)\s*([kK])?"
_RANGE_RE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY, re.I)
_UPTO_RE = re.compile(r"(?:up\s+to|max(?:imum)?(?:\s+of)?)\s*~?\s*" + _MONEY, re.I)
_SINGLE_RE = re.compile(r"(?:\$|USD\s|EUR\s|GBP\s|£|€)\s*(\d[\d,.]*)\s*([kK])?")
_HOURLY_RE = re.compile(r"(?:/\s*(?:hr|hour)|per\s+hour|hourly)", re.I)

# A full-time year. Used only to convert an explicit hourly rate; never applied
# to a bare number, because "50" with no unit is not evidence of anything.
_HOURS_PER_YEAR = 2080

_SALARY_FLOOR = 10_000
_SALARY_CEIL = 2_000_000


def _to_annual(num: str, k_suffix: str | None, hourly: bool) -> int | None:
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    if value <= 0:
        return None

    if hourly:
        value *= _HOURS_PER_YEAR
    elif k_suffix:
        value *= 1_000
    elif value < 1_000:
        # "172-195" in a job header means thousands. Nobody advertises $172/yr.
        value *= 1_000

    value = int(round(value))
    if not (_SALARY_FLOOR <= value <= _SALARY_CEIL):
        return None
    return value


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Best-effort (min, max) annual salary in whole currency units.

    Returns (None, None) far more often than you would like -- see
    ``lab.sh doctor``. That is the honest answer, and scenarios/02 is about
    what to do with it rather than about how to make it go away.

    Currency is NOT normalised: a EUR range is returned as-is. Mixing EUR and
    USD in one ``salary_max >=`` filter is wrong by roughly the exchange rate,
    which is a smaller error than the 40% of rows that have no salary at all,
    so this lab records the currency and moves on.
    """
    if not text:
        return (None, None)
    window = text[:400]  # the header line, not the whole posting
    hourly = bool(_HOURLY_RE.search(window))

    m = _RANGE_RE.search(window)
    if m:
        lo_num, lo_k, hi_num, hi_k = m.groups()
        # "$200-260K": the K binds to both ends, but only appears on the second.
        lo = _to_annual(lo_num, lo_k or hi_k, hourly)
        hi = _to_annual(hi_num, hi_k, hourly)
        if lo and hi and lo <= hi:
            return (lo, hi)
        if hi:
            return (None, hi)
        if lo:
            return (lo, None)

    m = _UPTO_RE.search(window)
    if m:
        hi = _to_annual(m.group(1), m.group(2), hourly)
        if hi:
            return (None, hi)

    m = _SINGLE_RE.search(window)
    if m:
        val = _to_annual(m.group(1), m.group(2), hourly)
        if val:
            return (val, val)

    return (None, None)


# ---------------------------------------------------------------------------
# Location / remote / employment
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(r"\bremote\b|\bwork\s+from\s+home\b|\bwfh\b|\bdistributed\b", re.I)
_ONSITE_RE = re.compile(r"\bon-?site\b|\bin-?office\b|\bin-?person\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)

_EMPLOYMENT = [
    ("intern", re.compile(r"\bintern(ship)?\b", re.I)),
    ("contract", re.compile(r"\bcontract(or)?\b|\bfreelance\b|\bc2c\b|\bpart[- ]time\b", re.I)),
    ("fulltime", re.compile(r"\bfull[- ]?time\b|\bft\b|\bpermanent\b", re.I)),
]

# Deliberately small. A real system uses a gazetteer; this is enough to make
# the location filter mean something on the feeds the lab actually scrapes.
#
# Bare "US" is matched case-SENSITIVELY, by _COUNTRY_CASED below. Folding case
# here would make every "email us", "join us", "us and them" into a job in the
# United States -- and because roughly half these postings say "us" somewhere,
# that single missing flag would put most of the corpus in the wrong country.
_COUNTRY_HINTS = [
    ("US", re.compile(r"\b(usa|u\.s\.a?\.?|united states|america)\b|,\s*(ca|ny|wa|tx|ma|il|co|or|ga|fl|nc|va|pa)\b", re.I)),
    ("UK", re.compile(r"\b(uk|united kingdom|england|london|scotland|manchester)\b", re.I)),
    ("DE", re.compile(r"\b(germany|deutschland|berlin|munich|münchen|hamburg|cologne|köln)\b", re.I)),
    ("CA", re.compile(r"\b(canada|toronto|vancouver|montreal|ottawa)\b", re.I)),
    ("NL", re.compile(r"\b(netherlands|amsterdam|utrecht|rotterdam)\b", re.I)),
    ("FR", re.compile(r"\b(france|paris|lyon)\b", re.I)),
    ("IN", re.compile(r"\b(india|bangalore|bengaluru|mumbai|delhi|hyderabad|pune)\b", re.I)),
    ("AU", re.compile(r"\b(australia|sydney|melbourne|brisbane)\b", re.I)),
    ("SG", re.compile(r"\b(singapore)\b", re.I)),
    ("VN", re.compile(r"\b(vietnam|viet nam|hanoi|ho chi minh|saigon|da nang)\b", re.I)),
    ("JP", re.compile(r"\b(japan|tokyo|osaka)\b", re.I)),
    ("PL", re.compile(r"\b(poland|warsaw|krakow|kraków|wroclaw)\b", re.I)),
    ("ES", re.compile(r"\b(spain|madrid|barcelona)\b", re.I)),
    ("BR", re.compile(r"\b(brazil|brasil|sao paulo|são paulo)\b", re.I)),
    ("CH", re.compile(r"\b(switzerland|zurich|zürich|geneva)\b", re.I)),
    ("EU", re.compile(r"\b(europe|eu[- ]based|emea|cet\b)\b", re.I)),
]


def detect_remote(text: str | None) -> bool:
    """True when the posting says remote and does not immediately take it back.

    ``REMOTE (Onsite 3x/week)`` is a thing people actually write. Treating that
    as remote is how a candidate with remote_required ends up recommended a job
    that needs them in an office, which is the kind of miss that makes users
    stop trusting recommendations entirely.
    """
    if not text:
        return False
    window = text[:400]
    if not _REMOTE_RE.search(window):
        return False
    if _ONSITE_RE.search(window) or _HYBRID_RE.search(window):
        return False
    return True


def detect_employment(text: str | None) -> str | None:
    if not text:
        return None
    window = text[:400]
    for label, pattern in _EMPLOYMENT:
        if pattern.search(window):
            return label
    return None


# Case-sensitive country tokens. Checked before the fold-case table.
_COUNTRY_CASED = [
    ("US", re.compile(r"\bU\.?S\.?(?:A\.?)?\b(?!\w)")),
    ("UK", re.compile(r"\bU\.?K\.?\b(?!\w)")),
]

# "Remote worldwide" is not a country, but it is the answer to the question the
# location filter is really asking, so it gets its own code.
_GLOBAL_RE = re.compile(r"\b(worldwide|anywhere|global|any\s+time\s?zone)\b", re.I)


def detect_country(text: str | None) -> str | None:
    """Coarse country code, or 'WW' for explicitly-worldwide remote.

    Returns None a lot. A None country must not be silently treated as "matches
    everywhere" -- scenarios/02 shows what that does to precision.
    """
    if not text:
        return None
    window = text[:400]
    for code, pattern in _COUNTRY_CASED:
        if pattern.search(window):
            return code
    for code, pattern in _COUNTRY_HINTS:
        if pattern.search(window):
            return code
    if _GLOBAL_RE.search(window):
        return "WW"
    return None


# ---------------------------------------------------------------------------
# Seniority and years of experience
# ---------------------------------------------------------------------------

_SENIORITY = [
    ("principal", re.compile(r"\b(principal|distinguished|fellow|vp\b|head of|director|cto)\b", re.I)),
    ("staff", re.compile(r"\b(staff|lead|architect|tech lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|snr)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|new grad|graduate|intern)\b", re.I)),
]

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*(?:\+)?\s*(?:years?|yrs?)\b", re.I)

# Rough floor implied by a title when the posting never states a number.
# Used only as a fallback -- an inferred requirement is marked as such in
# jobs.parsed so scenarios/02 can show you how much of your filter is guesswork.
_SENIORITY_YEARS = {"junior": 0.0, "mid": 2.0, "senior": 5.0, "staff": 8.0, "principal": 10.0}


def detect_seniority(text: str | None) -> str | None:
    if not text:
        return None
    window = text[:300]
    for label, pattern in _SENIORITY:
        if pattern.search(window):
            return label
    return None


def parse_years(text: str | None) -> float | None:
    """Lowest plausible years-of-experience figure stated in the text."""
    if not text:
        return None
    best: float | None = None
    for m in _YEARS_RE.finditer(text[:2000]):
        val = float(m.group(1))
        if 0 <= val <= 30 and (best is None or val < best):
            best = val
    return best


def implied_years(seniority: str | None) -> float | None:
    return _SENIORITY_YEARS.get(seniority or "", None)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

# A vocabulary, not an ontology. Multi-word entries are matched before the
# single words they contain, so "machine learning" does not also fire "learning".
SKILL_VOCAB: tuple[str, ...] = (
    # languages
    "python", "typescript", "javascript", "golang", "go", "rust", "java", "kotlin",
    "scala", "ruby", "elixir", "erlang", "c++", "c#", "swift", "objective-c", "php",
    "clojure", "haskell", "sql", "bash", "r", "julia", "zig", "perl",
    # web / frontend
    "react", "next.js", "vue", "svelte", "angular", "tailwind", "html", "css",
    "graphql", "rest", "grpc", "websocket", "htmx",
    # backend / frameworks
    "django", "flask", "fastapi", "rails", "spring", "node.js", "express",
    "laravel", ".net", "phoenix",
    # data / storage
    "postgres", "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "cassandra", "dynamodb", "clickhouse", "snowflake", "bigquery", "kafka",
    "rabbitmq", "spark", "airflow", "dbt", "duckdb", "sqlite", "neo4j", "pgvector",
    # infra
    "kubernetes", "docker", "terraform", "ansible", "aws", "gcp", "azure",
    "linux", "nginx", "prometheus", "grafana", "datadog", "ci/cd", "jenkins",
    "github actions", "serverless", "lambda",
    # ml / ai
    "machine learning", "deep learning", "pytorch", "tensorflow", "llm",
    "nlp", "computer vision", "transformers", "rag", "langchain", "hugging face",
    "recommendation systems", "embeddings", "vector search", "mlops",
    # discipline
    "distributed systems", "microservices", "system design", "observability",
    "security", "cryptography", "blockchain", "solidity", "embedded", "robotics",
    "ios", "android", "react native", "flutter", "unity", "game development",
    "data engineering", "devops", "sre", "platform engineering", "qa",
    "product management", "figma", "ux", "ui design",
)

# Skills whose names are also ordinary English words. Matched case-SENSITIVELY,
# because "Go" is a language and "go" is a verb; "R" is a language and "r" is a
# typo. Folding case here put ~230 spurious Go jobs in the corpus -- every
# posting containing "go to market" or "go deep" became a Go job, and then
# recommended itself to every Go developer.
_CASED_SKILLS = frozenset({"go", "r", "c"})

_SKILL_PATTERNS = tuple(
    (
        skill,
        re.compile(
            r"(?<![\w+#.])" + re.escape(skill if skill not in _CASED_SKILLS else skill.upper()) + r"(?![\w+#-])",
            0 if skill in _CASED_SKILLS else re.I,
        ),
    )
    # longest first, so "node.js" wins over "node" and "c++" is not eaten by "c"
    for skill in sorted(SKILL_VOCAB, key=len, reverse=True)
)

# Terms that mean the same thing. Collapsing them is worth real ranking points:
# a resume saying "postgresql" and a job saying "postgres" should not look like
# a mismatch to a bag-of-words model.
SKILL_ALIASES = {
    "postgresql": "postgres",
    "node.js": "node",
    "golang": "go",
    "deep learning": "machine learning",
}


def extract_skills(text: str | None, limit: int = 40) -> list[str]:
    """Skills mentioned in the text, de-aliased, in vocabulary order."""
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for skill, pattern in _SKILL_PATTERNS:
        if pattern.search(text):
            canonical = SKILL_ALIASES.get(skill, skill)
            if canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
        if len(found) >= limit:
            break
    return sorted(found)


# ---------------------------------------------------------------------------
# Tokenisation (shared by the embedder, so both sides tokenise identically)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*", re.I)

_STOPWORDS = frozenset("""
a an and are as at be been but by for from has have he her his how i in is it its
of on or our she that the their there they this to was we were what when where
which who will with you your us can our we're will able also just get make like
role team work working join looking want need help great good new using use used
""".split())


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords dropped, tech punctuation preserved.

    ``c++`` and ``node.js`` survive as single tokens: a tokeniser that splits on
    every non-alphanumeric turns C++ into the token ``c`` and makes every C++
    job look like every C job.
    """
    out = []
    for match in _TOKEN_RE.finditer(text.lower()):
        tok = match.group(0).strip(".-")
        if len(tok) < 2 and tok not in {"r", "c"}:
            continue
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out

"""Embedders. Four backends, one interface, one dimension.

    from lab.embed import get_embedder
    emb = get_embedder("hashed-tfidf")
    emb.fit(corpus)                 # no-op for model-backed embedders
    vectors = emb.encode(texts)     # (n, 384) float32, L2-normalised

EVERY backend emits 384 dimensions, because that is the width of
sentence-transformers/all-MiniLM-L6-v2. The default backend is a pure-numpy
hashed TF-IDF that needs no model download and no network -- but it is pinned
to MiniLM's width so that swapping in the real model later is a config change,
not a migration. Choosing your vector width for the model you intend to end up
on is nearly free on day one and expensive to change on day four hundred.

EVERY backend L2-normalises. Once vectors are unit length, cosine similarity
and inner product are the same operation and Euclidean distance is a monotone
function of both -- so the choice of pgvector operator class stops being a
correctness question. Skip the normalisation and `<=>` silently ranks by
something that is not what you meant.

Which backend to use:

  hashed-tfidf  Default. Offline, deterministic, ~5k docs/sec, no dependencies
                beyond numpy. Lexical: it matches documents that share terms.
                Good enough that the lab is fun; weak enough that scenarios/05
                (reranking) has something to fix.
  bm25-hashed   Same trick with BM25 term saturation instead of raw TF-IDF.
                Noticeably better on long documents -- the job postings here
                run to 50k characters, and plain TF-IDF lets a single repeated
                word dominate a long posting.
  minilm        Real sentence embeddings. Needs `sentence-transformers` and a
                ~90MB download on first use. Semantic: matches "k8s" to
                "kubernetes" and "shipped" to "delivered".
  openai        text-embedding-3-small, truncated to 384 dims via the API's
                `dimensions` parameter. Needs OPENAI_API_KEY and costs money.

Compare them on your own data with `./lab.sh compare-embedders`.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable, Sequence

import numpy as np

from lab.text import tokenize

DIM = 384

# Long documents get truncated before embedding. A 50,000-character posting
# (the corpus has them) is a company's entire About page with a job at the
# bottom; embedding all of it produces a vector that describes the company's
# marketing copy, and that vector is close to every other company's marketing
# copy. Truncation is not a limitation being worked around here, it is the
# correct thing to do.
MAX_CHARS = 8_000


class Embedder:
    """Interface. `fit` is where corpus statistics (IDF) get learned."""

    name = "base"
    dim = DIM

    FIELD_WEIGHTS = {"title": 3.0, "skills": 3.0, "body": 1.0}

    def fit(self, corpus: Iterable[str]) -> "Embedder":
        return self

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    def encode_fields(self, docs: Sequence[dict]) -> np.ndarray:
        return self.encode([_flatten_fields(d, self.FIELD_WEIGHTS) for d in docs])

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]

    def model_id(self) -> str:
        """Stable name recorded alongside every vector this produces.

        Model-backed embedders are corpus-independent, so the name alone is a
        complete identity. Corpus-fitted ones override this -- see
        _HashingBase.fit_signature.
        """
        return self.name


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A document with no in-vocabulary tokens has a zero vector. Dividing by
    # zero yields NaN, NaN poisons every distance it touches, and pgvector
    # rejects the row on insert -- so the load fails at 3am on one bad row
    # instead of here. Clamp.
    norms[norms == 0] = 1.0
    return matrix / norms


# How many dimensions each term is spread across. This is the single number
# that decides whether the offline embedder works at all.
#
# The obvious implementation -- one bucket per term, sign to cancel collisions
# -- was tried first and is bad here, for a reason worth internalising. This
# corpus has ~30,000 distinct tokens and the vectors are 384-wide, so a
# one-bucket scheme puts ~78 terms in every bucket. Two documents then look
# similar whenever ANY of their terms happen to collide, and with 78-way
# collisions that is most of the time. Measured on the scraped corpus, it put
# "Commercial Building Estimator" at the top of a data engineer's results and
# held p99 similarity down at 0.15: the signal was there, buried in collision
# noise.
#
# Spreading each term over PROJ_NNZ dimensions is a sparse random projection
# (Achlioptas). A term is now a random DIRECTION rather than a single bucket,
# two unrelated terms overlap partially instead of colliding totally, and the
# error in the dot product falls like 1/sqrt(PROJ_NNZ) instead of being
# all-or-nothing. Same 384 dimensions, same cost per document, ~4x the
# separation between a real match and a coincidence.
PROJ_NNZ = 8


def _term_projection(token: str, dim: int) -> tuple[np.ndarray, np.ndarray]:
    """Stable (indices, signs) for one term: its direction in R^dim.

    blake2b, not Python's hash(): hash() is randomised per process by
    PYTHONHASHSEED, so vectors written today would not match vectors written
    tomorrow, and the similarity of a row to itself would drift between runs.
    """
    # 3 bytes per slot: 2 for the index, 1 for the sign.
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=3 * PROJ_NNZ).digest()
    idx = np.empty(PROJ_NNZ, dtype=np.int32)
    sign = np.empty(PROJ_NNZ, dtype=np.float32)
    for slot in range(PROJ_NNZ):
        chunk = digest[slot * 3:(slot + 1) * 3]
        idx[slot] = int.from_bytes(chunk[:2], "big") % dim
        sign[slot] = 1.0 if chunk[2] & 1 else -1.0
    # 1/sqrt(nnz) keeps each term's projected direction unit-length, so a term
    # is not made more important simply by being spread over more dimensions.
    return idx, sign / math.sqrt(PROJ_NNZ)


_PROJ_CACHE: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}


def _projection(token: str, dim: int):
    key = (token, dim)
    hit = _PROJ_CACHE.get(key)
    if hit is None:
        hit = _term_projection(token, dim)
        # Bounded: a pathological corpus (hex ids, URLs) has an unbounded
        # vocabulary, and an unbounded cache is a memory leak with good manners.
        if len(_PROJ_CACHE) < 200_000:
            _PROJ_CACHE[key] = hit
    return hit


class _HashingBase(Embedder):
    """Shared IDF fitting and hashing for the two offline backends."""

    MIN_DF = 2            # drop terms seen in a single document
    MAX_DF_RATIO = 0.5    # drop terms seen in over half of them

    # What a document is about, in descending order of how much it tells you.
    # Tuned by looking at results on the scraped corpus, which is the only way
    # these numbers are ever set honestly.
    FIELD_WEIGHTS = {"title": 3.0, "skills": 3.0, "body": 1.0}

    def __init__(self, dim: int = DIM):
        self.dim = dim
        self._idf: dict[str, float] = {}
        self._default_idf = 1.0
        self._avg_len = 1.0
        self._pruned = 0
        self._fitted = False

    @property
    def vocab_size(self) -> int:
        return len(self._idf)

    def fit_signature(self) -> str:
        """Identity of THIS fit, not just of the algorithm.

        A corpus-fitted embedder has a property model-backed ones do not: its
        output for a fixed document depends on every other document it was
        fitted on, because IDF does. Add 500 jobs, refit, and every vector in
        the table moves -- so a freshly embedded row and a row embedded last
        week are in different spaces, and the cosine between them is a number
        with no meaning that will nonetheless sort perfectly happily.

        Folding this signature into the stored model name makes that failure
        loud: the content hash stops matching, and `./lab.sh embed` re-embeds
        the whole corpus instead of quietly mixing two vector spaces.
        """
        h = hashlib.blake2b(digest_size=4)
        h.update(f"{self.name}:{self.dim}:{len(self._idf)}:".encode())
        for term in sorted(self._idf):
            h.update(term.encode())
            h.update(f"{self._idf[term]:.4f}".encode())
        return h.hexdigest()

    def model_id(self) -> str:
        return f"{self.name}@{self.fit_signature()}" if self._fitted else self.name

    def fit(self, corpus: Iterable[str]) -> "_HashingBase":
        doc_freq: dict[str, int] = {}
        n_docs = 0
        total_len = 0
        for text in corpus:
            n_docs += 1
            tokens = tokenize(text[:MAX_CHARS])
            total_len += len(tokens)
            for token in set(tokens):
                doc_freq[token] = doc_freq.get(token, 0) + 1
        if n_docs == 0:
            return self

        # Prune before weighting. Two cuts, for two different reasons:
        #
        #   df < MIN_DF   Terms appearing in one document only. In a scraped
        #                 corpus these are overwhelmingly URL fragments, hashes
        #                 and typos. They can never contribute to a match --
        #                 no second document contains them -- so they only add
        #                 projection noise. Here that is ~60% of the vocabulary.
        #   df > MAX_DF   Terms in over half the corpus ("engineer", "team").
        #                 IDF already discounts them; dropping them outright
        #                 stops them from crowding the 384 dimensions available.
        min_df = self.MIN_DF
        max_df = int(n_docs * self.MAX_DF_RATIO)
        kept = {t: df for t, df in doc_freq.items() if min_df <= df <= max_df}
        self._pruned = len(doc_freq) - len(kept)

        # Smoothed IDF. The +1s keep a surviving common term at a small positive
        # weight rather than exactly zero -- a term with zero weight cannot
        # break ties, and in a corpus this uniform you still want it to count.
        self._idf = {
            term: math.log((n_docs + 1) / (df + 1)) + 1.0
            for term, df in kept.items()
        }
        # An unseen term at query time is rarer than anything in the corpus, so
        # it gets the maximum weight rather than being silently dropped.
        self._default_idf = math.log(n_docs + 1) + 1.0
        self._avg_len = max(1.0, total_len / n_docs)
        self._fitted = True
        return self

    def _weights_from_counts(self, counts: dict) -> dict[str, float]:
        raise NotImplementedError

    def encode_fields(self, docs: Sequence[dict]) -> np.ndarray:
        """Embed structured documents with per-field weights (this is BM25F).

        A job posting is not a uniform blob of text. Its title and its skill
        list are what it is ABOUT; the paragraph on the company's mission and
        the one about the dental plan are the same words every other posting
        uses. Weighting every token equally lets 2,000 words of boilerplate
        outvote the eight words that identify the role, which is exactly what
        was happening before this method existed: a data engineer's top match
        was whichever posting had the most prose.

        Weighting is applied to term COUNTS before saturation, not to the
        finished vector. That ordering matters: BM25 saturation must see the
        boosted count, or the boost is silently discarded for any term that has
        already saturated.
        """
        expanded: list[dict[str, float]] = []
        for doc in docs:
            counts: dict[str, float] = {}
            for field, weight in self.FIELD_WEIGHTS.items():
                value = doc.get(field)
                if not value:
                    continue
                for token in tokenize(value[:MAX_CHARS]):
                    counts[token] = counts.get(token, 0.0) + weight
            expanded.append(counts)
        return self._encode_counts(expanded)

    def _encode_counts(self, per_doc: Sequence[dict]) -> np.ndarray:
        out = np.zeros((len(per_doc), self.dim), dtype=np.float32)
        for row, counts in enumerate(per_doc):
            if not counts:
                continue
            for token, weight in self._weights_from_counts(counts).items():
                if self._fitted and token not in self._idf:
                    continue
                idx, sign = _projection(token, self.dim)
                np.add.at(out[row], idx, weight * sign)
        return _l2_normalize(out)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        per_doc = []
        for text in texts:
            counts: dict[str, float] = {}
            for token in tokenize((text or "")[:MAX_CHARS]):
                counts[token] = counts.get(token, 0.0) + 1.0
            per_doc.append(counts)
        return self._encode_counts(per_doc)


class HashedTfidf(_HashingBase):
    """TF-IDF with sublinear term frequency, hashed into `dim` buckets."""

    name = "hashed-tfidf"

    def _weights_from_counts(self, counts: dict) -> dict[str, float]:
        # 1 + log(tf), not raw tf: the tenth mention of "python" in a posting
        # says almost nothing the first one did not.
        return {
            term: (1.0 + math.log(tf)) * self._idf.get(term, self._default_idf)
            for term, tf in counts.items() if tf > 0
        }


class HashedBM25(_HashingBase):
    """BM25 term saturation, hashed. Better than TF-IDF on long documents."""

    name = "bm25-hashed"

    K1 = 1.5   # saturation: how fast extra occurrences stop helping
    B = 0.75   # length normalisation: how much to punish long documents

    def _weights_from_counts(self, counts: dict) -> dict[str, float]:
        doc_len = sum(counts.values())
        # The denominator is what TF-IDF is missing: a 50k-character posting
        # cannot buy rank simply by being long enough to mention everything.
        norm = self.K1 * (1 - self.B + self.B * doc_len / self._avg_len)
        return {
            term: (tf * (self.K1 + 1)) / (tf + norm) * self._idf.get(term, self._default_idf)
            for term, tf in counts.items() if tf > 0
        }


# ---------------------------------------------------------------------------
# Model-backed backends
# ---------------------------------------------------------------------------

def _flatten_fields(doc: dict, weights: dict) -> str:
    """Field-weighted text for embedders that only accept a string.

    A transformer has no term counts to scale, so emphasis has to be expressed
    the only way the input allows: by repeating the important fields. Crude,
    but it is what the model can hear.
    """
    parts = []
    for field, weight in weights.items():
        value = (doc.get(field) or "").strip()
        if value:
            parts.extend([value] * max(1, int(round(weight / 3.0))))
    return "\n".join(parts)


class MiniLM(Embedder):
    """sentence-transformers/all-MiniLM-L6-v2 -- 384 dims, hence the constant."""

    name = "minilm"

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the user's env
            raise RuntimeError(
                "minilm needs sentence-transformers:\n"
                "    uv pip install sentence-transformers\n"
                "and ~90MB of model download on first use. "
                "The default hashed-tfidf backend needs neither."
            ) from exc
        self._model = SentenceTransformer(model_name)
        got = self._model.get_sentence_embedding_dimension()
        if got != DIM:
            raise RuntimeError(
                f"{model_name} emits {got} dims but the schema declares vector({DIM}). "
                "Changing dimension means an ALTER on every embedding column and a "
                "full re-embed -- which is the migration this lab is set up to avoid."
            )

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self._model.encode(
            [(t or "")[:MAX_CHARS] for t in texts],
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return vectors.astype(np.float32)


class OpenAIEmbedder(Embedder):
    """text-embedding-3-small, asked for 384 dims rather than its native 1536.

    The `dimensions` parameter is Matryoshka truncation: the model is trained so
    that a prefix of the vector is itself a usable embedding. It is not the same
    as slicing an arbitrary model's output, which destroys the geometry.
    """

    name = "openai"

    def __init__(self, model: str = "text-embedding-3-small"):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openai backend needs: uv pip install openai") from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("openai backend needs OPENAI_API_KEY in the environment.")
        self._client = OpenAI()
        self._model = model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        # Batched because per-request overhead dominates, and capped because
        # the endpoint has a request-size limit that a 2000-document corpus
        # will find for you.
        for start in range(0, len(texts), 128):
            chunk = [(t or " ")[:MAX_CHARS] for t in texts[start:start + 128]]
            resp = self._client.embeddings.create(
                model=self._model, input=chunk, dimensions=DIM
            )
            for offset, item in enumerate(resp.data):
                out[start + offset] = np.asarray(item.embedding, dtype=np.float32)
        return _l2_normalize(out)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BACKENDS = {
    "hashed-tfidf": HashedTfidf,
    "bm25-hashed": HashedBM25,
    "minilm": MiniLM,
    "openai": OpenAIEmbedder,
}

DEFAULT_BACKEND = os.environ.get("LAB_EMBEDDER", "bm25-hashed")


def get_embedder(name: str | None = None) -> Embedder:
    key = (name or DEFAULT_BACKEND).strip()
    if key not in BACKENDS:
        raise SystemExit(
            f"Unknown embedder {key!r}. Available: {', '.join(BACKENDS)}"
        )
    return BACKENDS[key]()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

_VEC_RE = re.compile(r"^\[.*\]$")


def to_pgvector(vec: np.ndarray) -> str:
    """numpy vector -> the text form pgvector parses.

    Not repr(): numpy's repr inserts newlines and ellipses past a size
    threshold, so this works on your 8-dimensional test and silently truncates
    in production. Formatting each component explicitly is the boring fix.
    """
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def from_pgvector(text: str) -> np.ndarray:
    if not _VEC_RE.match(text.strip()):
        raise ValueError(f"not a pgvector literal: {text[:40]!r}")
    return np.fromstring(text.strip()[1:-1], sep=",", dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Both inputs assumed unit length."""
    return float(np.dot(a, b))


def content_hash(text: str, model: str) -> str:
    """Identity of an embedding: the text AND the model that produced it.

    Hashing only the text is the bug that bites six months later, when you
    switch models and every row's hash still matches, so nothing re-embeds, so
    half your corpus is in one vector space and half in another and every
    similarity between them is noise.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(model.encode())
    h.update(b"\x00")
    h.update(text[:MAX_CHARS].encode("utf-8", errors="replace"))
    return h.hexdigest()

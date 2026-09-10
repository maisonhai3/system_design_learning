from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Callable, Iterable, Sequence


def tokenize(text: str) -> list[str]:
    """Very small tokenizer used by the demo embedding model."""

    token = []
    tokens: list[str] = []
    for char in text.lower():
        if char.isalnum():
            token.append(char)
        elif token:
            tokens.append("".join(token))
            token.clear()
    if token:
        tokens.append("".join(token))
    return tokens


@dataclass(frozen=True)
class Document:
    """A record stored in the vector database."""

    doc_id: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


class VocabularyEmbedding:
    """Builds a simple bag-of-words embedding from a fixed vocabulary.

    This is intentionally simple so the project focuses on vector DB concepts:
    - turning text into vectors
    - storing vectors with metadata
    - searching by cosine similarity
    """

    def __init__(self) -> None:
        self._vocabulary: dict[str, int] = {}

    @property
    def dimension(self) -> int:
        return len(self._vocabulary)

    def fit(self, texts: Iterable[str]) -> None:
        for text in texts:
            for token in tokenize(text):
                if token not in self._vocabulary:
                    self._vocabulary[token] = len(self._vocabulary)

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in tokenize(text):
            index = self._vocabulary.get(token)
            if index is not None:
                vector[index] += 1.0
        return vector


@dataclass(frozen=True)
class SearchResult:
    document: Document
    score: float


class InMemoryVectorStore:
    """A tiny vector DB implementation used for learning."""

    def __init__(self, embedding_model: VocabularyEmbedding) -> None:
        self._embedding_model = embedding_model
        self._documents: list[Document] = []
        self._vectors: list[list[float]] = []

    @property
    def documents(self) -> Sequence[Document]:
        return tuple(self._documents)

    def upsert(self, document: Document) -> None:
        """Insert a document or replace an existing one with the same id."""

        vector = self._embedding_model.encode(document.text)
        for index, existing in enumerate(self._documents):
            if existing.doc_id == document.doc_id:
                self._documents[index] = document
                self._vectors[index] = vector
                return
        self._documents.append(document)
        self._vectors.append(vector)

    def search(
        self,
        query: str,
        top_k: int = 3,
        filter_fn: Callable[[Document], bool] | None = None,
    ) -> list[SearchResult]:
        query_vector = self._embedding_model.encode(query)
        scored: list[SearchResult] = []
        for document, vector in zip(self._documents, self._vectors, strict=True):
            if filter_fn is not None and not filter_fn(document):
                continue
            score = cosine_similarity(query_vector, vector)
            scored.append(SearchResult(document=document, score=score))
        scored.sort(key=lambda result: result.score, reverse=True)
        return scored[:top_k]


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Vectors must have the same dimension")
    if not left:
        return 0.0

    dot_product = sum(l * r for l, r in zip(left, right, strict=True))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)


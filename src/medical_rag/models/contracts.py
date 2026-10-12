from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Protocol
from uuid import UUID, uuid4

from medical_rag.retrieval.contracts import Evidence
from medical_rag.storage.object_store import ObjectRef


@dataclass(frozen=True, slots=True)
class FileReference:
    object_key: str
    document_id: UUID
    tenant_id: UUID
    patient_id: UUID
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ChatRequest:
    prompt: str
    system_prompt: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    file_refs: list[FileReference] = field(default_factory=list)
    model: str = "gpt-5.5"
    policy_version: str = "v1"
    timeout_seconds: float = 30.0
    max_output_tokens: int = 1200
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    purpose: str = "health_education"
    evidence_hashes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ChatResponse:
    text: str
    provider: str
    model: str
    trace_id: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RankedPassage:
    index: int
    score: float


@dataclass(frozen=True, slots=True)
class VisionResponse:
    text: str
    provider: str
    model: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SafetyResult:
    label: str
    score: float
    provider: str


class ModelError(Exception):
    pass


class ModelTimeout(TimeoutError, ModelError):
    pass


class ProviderConfigurationError(ModelError):
    pass


class ProviderResponseError(ModelError):
    pass


class ChatModel(Protocol):
    def generate(self, request: ChatRequest) -> ChatResponse: ...


class EmbeddingModel(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingPipeline:
    """Batching, validation, and bounded-retry seam around an embedding model."""

    def __init__(
        self,
        model: EmbeddingModel,
        dimensions: int,
        batch_size: int = 64,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        if dimensions < 1:
            raise ValueError("embedding dimensions must be positive")
        if batch_size < 1:
            raise ValueError("embedding batch size must be positive")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout must be positive")
        if max_retries < 0:
            raise ValueError("embedding max retries cannot be negative")
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.model_version: str | None = None

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                vectors = self.model.embed(texts)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(min(0.05 * (attempt + 1), self.timeout_seconds / 10))
                continue
            if len(vectors) != len(texts):
                raise ValueError("embedding vector count mismatch")
            normalized: list[list[float]] = []
            for vector in vectors:
                if len(vector) != self.dimensions:
                    raise ValueError("embedding vector dimension mismatch")
                normalized.append([float(value) for value in vector])
            return normalized
        if last_error is None:  # pragma: no cover - the retry loop always sets one
            raise RuntimeError("embedding request produced neither a result nor an error")
        raise last_error

    def embed_records(self, texts: list[str], model_version: str) -> list[tuple[float, ...]]:
        if not isinstance(model_version, str) or not model_version.strip():
            raise ValueError("embedding model version cannot be empty")
        # Keep the version observable for tracing/index metadata.  Callers also
        # persist it on each index record because this method returns vectors.
        self.model_version = model_version
        if not texts:
            return []
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(tuple(vector) for vector in self._embed_batch(batch))
        if len(vectors) != len(texts):
            raise ValueError("embedding vector count mismatch")
        return vectors


class RerankerModel(Protocol):
    def rank(self, query: str, passages: list[str]) -> list[RankedPassage]: ...


class VisionModel(Protocol):
    def extract(self, file_ref: ObjectRef, instruction: str) -> VisionResponse: ...


class SafetyClassifier(Protocol):
    def classify(self, text: str) -> SafetyResult: ...

"""Stage-level counters and latency observations for the document-quality pipeline.

This module is intentionally **additive and self-contained** with respect to
``medical_rag.observability.metrics`` (the retrieval-evaluation / rollout-gate
recorder owned by the parallel Scheme-B session).  It does not import from that
module and does not reformat/reorder/rename any of its symbols.  It provides the
Task 11 stage metrics: parse/clean/chunk/extract/gate/embed/write/retrieve
counters keyed by the real pipeline version identity
(``file_type``, ``parser_version``, ``cleaner_version``, ``chunker_version``,
``quality_status``, ``index_version``, ``embedding_model``).

Privacy contract (matches the rest of observability): only identifiers, version
strings, counts, and durations are retained — raw passage/evidence text is never
copied into the recorder.

Stage boundaries deliberately mirror the golden runner projections in
``medical_rag/eval/golden.py`` (``parse_ms/clean_ms/chunk_ms/extract_ms/gate_ms``)
plus the durable ``embed``/``write``/``retrieve`` stages, so Task 11
observability keys off the same pipeline stages instead of re-inventing them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import median
from types import MappingProxyType
from typing import Any


class PipelineStage(StrEnum):
    """The canonical pipeline stage boundaries (ordered as they execute)."""

    PARSE = "parse"
    CLEAN = "clean"
    CHUNK = "chunk"
    EXTRACT = "extract"
    GATE = "gate"
    EMBED = "embed"
    WRITE = "write"
    RETRIEVE = "retrieve"


# Version dimensions that every stage observation is keyed on (Task 11 Step 1).
_VERSION_DIMENSIONS = (
    "file_type",
    "parser_version",
    "cleaner_version",
    "chunker_version",
    "quality_status",
    "index_version",
    "embedding_model",
)


@dataclass(frozen=True, slots=True)
class StageKey:
    """Compound identity for one stage/outcome observation."""

    stage: PipelineStage
    outcome: str
    # Deterministic, sortable version tuple (subset of _VERSION_DIMENSIONS).
    versions: tuple[tuple[str, str | None], ...] = ()

    def as_filename_fragment(self) -> str:
        """Stable path-safe fragment for per-stage reporting/tagging."""
        base = f"{self.stage.value}__{self.outcome}"
        parts = [f"{name}={_safe_tag(value)}" for name, value in self.versions]
        return base if not parts else f"{base}__{'__'.join(parts)}"


def _safe_tag(value: str | None) -> str:
    if value is None:
        return "none"
    cleaned = "".join(char for char in str(value) if char.isalnum() or char in "._-:")
    return cleaned or "anon"


@dataclass(slots=True)
class PipelineMetricsRecorder:
    """Privacy-safe in-memory recorder for stage counters + latency.

    ``record_stage`` accepts free-form keyword metadata, extracts the version
    dimensions into an immutable snapshot, and keeps per-stage latency buckets
    with their observation count.  Raw text is dropped (never stored).
    """

    stages: list[dict[str, object]] = field(default_factory=list)
    # (stage, outcome) -> count
    _counters: dict[tuple[str, str], int] = field(default_factory=dict)
    # (stage) -> list of observed latencies (ms)
    _latency: dict[str, list[float]] = field(default_factory=dict)
    # named aggregate counters (snapshot-friendly)
    _named: dict[str, int] = field(default_factory=dict)
    # named list accumulators (e.g. anchor coverage samples)
    _samples: dict[str, list[float]] = field(default_factory=dict)

    # -- generic stage observation -------------------------------------------

    def record_stage(
        self,
        stage: PipelineStage,
        outcome: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        latency_ms: float | None = None,
        **kwargs: object,
    ) -> None:
        """Record one stage observation (outcome: success/failure/pass/etc.).

        ``metadata`` and ``**kwargs`` may carry version dimensions; only the
        allow-listed version keys are retained (raw text is never stored).
        ``latency_ms`` feeds the per-stage latency bucket when provided.
        """
        meta = dict(metadata) if metadata is not None else {}
        meta.update(kwargs)
        versions = tuple(
            (name, _coerce_version(meta.get(name)))
            for name in _VERSION_DIMENSIONS
            if meta.get(name) is not None
        )
        self.stages.append(
            {
                "stage": stage.value,
                "outcome": str(outcome),
                "versions": MappingProxyType(dict(versions)),
            }
        )
        key = (stage.value, str(outcome))
        self._counters[key] = self._counters.get(key, 0) + 1
        if latency_ms is not None:
            self._latency.setdefault(stage.value, []).append(float(latency_ms))

    def count(self, key: StageKey) -> int:
        return self._counters.get((key.stage.value, key.outcome), 0)

    def stage_count(self, stage: PipelineStage, outcome: str) -> int:
        return self._counters.get((stage.value, outcome), 0)

    def latency_total_ms(self, stage: PipelineStage) -> float:
        return float(sum(self._latency.get(stage.value, [])))

    def latency_p95_ms(self, stage: PipelineStage) -> float:
        values = sorted(self._latency.get(stage.value, []))
        if not values:
            return 0.0
        index = min(len(values) - 1, round(0.95 * (len(values) - 1)))
        return values[index]

    # -- named pipeline counters (Task 11 Step 1) ----------------------------

    def record_parse(self, outcome: str, **versions: object) -> None:
        self.record_stage(PipelineStage.PARSE, outcome, versions)
        self._named[f"parse_{outcome}"] = self._named.get(f"parse_{outcome}", 0) + 1

    def record_review(self, **versions: object) -> None:
        self._named["review_count"] = self._named.get("review_count", 0) + 1

    def record_anchor_coverage(self, coverage: float, **versions: object) -> None:
        self._samples.setdefault("anchor_coverage", []).append(float(coverage))

    def record_chunks(self, count: int, **versions: object) -> None:
        self._named["chunk_count"] = self._named.get("chunk_count", 0) + int(count)

    def record_facts(self, count: int, **versions: object) -> None:
        self._named["fact_count"] = self._named.get("fact_count", 0) + int(count)

    def record_embedding(self, outcome: str, **versions: object) -> None:
        self.record_stage(PipelineStage.EMBED, outcome, versions)
        self._named[f"embedding_{outcome}"] = self._named.get(
            f"embedding_{outcome}", 0
        ) + 1

    def record_embedding_failure(self, **versions: object) -> None:
        self.record_embedding("failure", **versions)

    def record_writer(self, outcome: str, **versions: object) -> None:
        self.record_stage(PipelineStage.WRITE, outcome, versions)
        self._named[f"writer_{outcome}"] = self._named.get(f"writer_{outcome}", 0) + 1

    def record_retry(self, **versions: object) -> None:
        self._named["retry_count"] = self._named.get("retry_count", 0) + 1

    def record_deletion_residual(self, count: int = 1, **versions: object) -> None:
        self._named["deletion_residual_count"] = self._named.get(
            "deletion_residual_count", 0
        ) + int(count)

    def increment(self, name: str, count: int = 1, **versions: object) -> None:
        """Generic named counter (lease acquired/rejected, outbox retries, etc.).

        ``versions`` are accepted for call-site convenience but only the
        already allow-listed version dimensions are ever retained by
        :meth:`record_stage`; a bare ``increment`` keeps just the counter name.
        """
        del versions
        self._named[str(name)] = self._named.get(str(name), 0) + int(count)

    # -- snapshot / serialization --------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Flatten to a deterministic mapping (no raw text)."""
        values = dict(self._named)
        values["stage_counters"] = {
            f"{stage}.{outcome}": count
            for (stage, outcome), count in sorted(self._counters.items())
        }
        values["anchor_coverage"] = list(self._samples.get("anchor_coverage", []))
        values["stages"] = [
            {
                "stage": entry["stage"],
                "outcome": entry["outcome"],
                "versions": dict(entry["versions"]),
            }
            for entry in self.stages
        ]
        values["latency_p95_ms"] = {
            stage: round(self.latency_p95_ms(PipelineStage(stage)), 3)
            for stage in sorted({item["stage"] for item in self.stages})
        }
        return values

    def to_json(self) -> str:
        data = self.snapshot()
        return json.dumps(data, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, payload: str) -> PipelineMetricsRecorder:
        data = json.loads(payload)
        recorder = cls()
        # Rehydrate the stage counters; ``stage_counters`` is authoritative, the
        # ``stages`` list is the provenance view (never double-counted).
        stage_counters = data.get("stage_counters")
        if isinstance(stage_counters, dict):
            for compound, count in stage_counters.items():
                stage, separator, outcome = compound.partition(".")
                if separator:
                    recorder._counters[(stage, outcome)] = int(count)
        for entry in data.get("stages", []):
            versions = dict(entry.get("versions", {}))
            recorder.stages.append(
                {
                    "stage": entry["stage"],
                    "outcome": entry["outcome"],
                    "versions": MappingProxyType(versions),
                }
            )
            if not isinstance(stage_counters, dict):
                recorder._counters[(str(entry["stage"]), str(entry["outcome"]))] = (
                    recorder._counters.get((str(entry["stage"]), str(entry["outcome"])), 0) + 1
                )
        for name in (
            "parse_success",
            "parse_failure",
            "review_count",
            "chunk_count",
            "fact_count",
            "embedding_failure",
            "writer_failure",
            "retry_count",
            "deletion_residual_count",
        ):
            if name in data:
                recorder._named[name] = int(data[name])  # type: ignore[arg-type]
        for entry in data.get("stages", []):
            recorder.stages.append(
                {
                    "stage": entry["stage"],
                    "outcome": entry["outcome"],
                    "versions": MappingProxyType(dict(entry.get("versions", {}))),
                }
            )
        return recorder


def _coerce_version(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    return str(value)


def summarize(entries: Iterable[PipelineMetricsRecorder]) -> dict[str, object]:
    """Merge a series of recorders into one aggregate snapshot (p95-aware)."""
    merged = PipelineMetricsRecorder()
    for recorder in entries:
        for key, value in recorder._named.items():
            merged._named[key] = merged._named.get(key, 0) + int(value)
        for key, value in recorder._counters.items():
            merged._counters[key] = merged._counters.get(key, 0) + int(value)
        for stage, latencies in recorder._latency.items():
            merged._latency.setdefault(stage, []).extend(latencies)
    return merged.snapshot()


# Deterministic median helper exposed for the release report.
def median_ms(values: Iterable[float]) -> float:
    sample = [float(value) for value in values]
    return float(median(sample)) if sample else 0.0


__all__ = [
    "PipelineMetricsRecorder",
    "PipelineStage",
    "StageKey",
    "median_ms",
    "summarize",
]

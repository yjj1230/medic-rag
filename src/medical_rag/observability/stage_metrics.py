"""文档质量管线的阶段级计数器与延迟观测。

本模块相对 ``medical_rag.observability.metrics``（由并行的 Scheme-B 会话拥有的
检索评测 / 发布门禁记录器）有意保持**增量且自洽**。它不从该模块导入，也不重排/
禁用/重命名其任何符号。它提供 Task 11 阶段指标：按真实管线版本标识
（``file_type``、``parser_version``、``cleaner_version``、``chunker_version``、
``quality_status``、``index_version``、``embedding_model``）键入的
parse/clean/chunk/extract/gate/embed/write/retrieve 计数器。

隐私契约（与可观测其余部分一致）：只保留标识符、版本字符串、计数和时长 ——
原始正文/证据文本绝不复制进记录器。

阶段边界有意与 ``medical_rag/eval/golden.py`` 中的黄金运行器投影保持一致
（``parse_ms/clean_ms/chunk_ms/extract_ms/gate_ms``），外加持久化的
``embed``/``write``/``retrieve`` 阶段，这样 Task 11 的可观测键就出自同一批管线
阶段，而不是重新发明一套。
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
    """规范化的管线阶段边界（按执行顺序排列）。"""

    PARSE = "parse"
    CLEAN = "clean"
    CHUNK = "chunk"
    EXTRACT = "extract"
    GATE = "gate"
    EMBED = "embed"
    WRITE = "write"
    RETRIEVE = "retrieve"


# 每条阶段观测都以其为键的版本维度（Task 11 Step 1）。
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
    """一次阶段/结果观测的复合标识。"""

    stage: PipelineStage
    outcome: str
    # 确定性、可排序的版本元组（_VERSION_DIMENSIONS 的子集）。
    versions: tuple[tuple[str, str | None], ...] = ()

    def as_filename_fragment(self) -> str:
        """用于按阶段上报/打标签的稳定、路径安全片段。"""
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
    """阶段计数器 + 延迟的隐私安全内存记录器。

    ``record_stage`` 接受自由形式的关键字元数据，把版本维度抽取为不可变快照，
    并保留各阶段的延迟分桶及其观测计数。原始文本被丢弃（绝不存储）。
    """

    stages: list[dict[str, object]] = field(default_factory=list)
    # (stage, outcome) -> 计数
    _counters: dict[tuple[str, str], int] = field(default_factory=dict)
    # (stage) -> 已观测延迟列表（毫秒）
    _latency: dict[str, list[float]] = field(default_factory=dict)
    # 具名聚合计数器（便于快照）
    _named: dict[str, int] = field(default_factory=dict)
    # 具名列表累加器（例如锚点覆盖率样本）
    _samples: dict[str, list[float]] = field(default_factory=dict)

    # -- 通用阶段观测 ---------------------------------------------------------

    def record_stage(
        self,
        stage: PipelineStage,
        outcome: str,
        metadata: Mapping[str, Any] | None = None,
        *,
        latency_ms: float | None = None,
        **kwargs: object,
    ) -> None:
        """记录一次阶段观测（outcome：success/failure/pass 等）。

        ``metadata`` 与 ``**kwargs`` 可以携带版本维度；只保留白名单内的版本键
        （原始文本绝不存储）。提供 ``latency_ms`` 时会喂给该阶段的延迟分桶。
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

    # -- 具名管线计数器（Task 11 Step 1）------------------------------------

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
        """通用具名计数器（租约获取/拒绝、outbox 重试等）。

        ``versions`` 为调用点便利而接受，但 :meth:`record_stage` 只会保留已白名单化
        的版本维度；裸 ``increment`` 只保留计数器名。
        """
        del versions
        self._named[str(name)] = self._named.get(str(name), 0) + int(count)

    # -- 快照 / 序列化 --------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """扁平化为确定性映射（无原始文本）。"""
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
        # 重建阶段计数器；``stage_counters`` 具有权威性，
        # ``stages`` 列表是溯源视图（绝不重复计数）。
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
    """把一系列记录器合并为一个聚合快照（感知 p95）。"""
    merged = PipelineMetricsRecorder()
    for recorder in entries:
        for key, value in recorder._named.items():
            merged._named[key] = merged._named.get(key, 0) + int(value)
        for key, value in recorder._counters.items():
            merged._counters[key] = merged._counters.get(key, 0) + int(value)
        for stage, latencies in recorder._latency.items():
            merged._latency.setdefault(stage, []).extend(latencies)
    return merged.snapshot()


# 为发布报告暴露的确定性中位数辅助函数。
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

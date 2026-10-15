"""唯一一个可复用的黄金集运行器 + 基线报告 + 发布门禁（Task 10）。

此处遵循的设计原则
------------------
* **单一运行器。** ``run_golden_fixture`` 是唯一调用
  parse→clean→chunk→extract→gate 管线的地方。``tests/eval/test_document_quality.py``
  与 CLI 发布门禁命令都调用它；任何地方都没有重复组装。
* **数据驱动、经校验的 fixture schema。** 每个 fixture 都会针对六个必需的
  ``expected_*`` 键以及格式良好的 ``parsed.blocks`` 载荷做校验；
  格式错误的 fixture 会大声失败，而不是静默产出一个虚假的通过。
* **fail-closed 锚点。** ``anchor_accuracy`` 由 fixture 的 ``expected_anchors`` 驱动：
  缺失的锚点与**凭空捏造**的锚点（fixture 从未声明的锚点）都计入扣分。
* **完全在内存中 / 测试本地。** 无解析器持久化、无索引写入器、无 OpenSearch ——
  管线只对合成 fixture 块操作。
* **复用错误码。** ``expected_review_reasons`` 通过规范的 ``QualityFailureCode`` slug
  校验并比较（:mod:`medical_rag.quality.codes`）；``QualityGate``/``QualityPolicy``
  直接消费，绝不重新声明。
* **合成 / 脱敏。** fixture 是虚构但医学上合理的内容（无真实患者、文件名或 ID 号）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

from medical_rag.domain.enums import EvidenceKind, QualityStatus
from medical_rag.knowledge.semantic_chunker import SemanticChunker
from medical_rag.observability.metrics import EvaluationCase, evaluate_retrieval
from medical_rag.parsing.cleaner import DocumentCleaner
from medical_rag.parsing.contracts import EvidenceUnitDraft, ParsedBlock
from medical_rag.parsing.service import RuleBasedMedicalFactExtractor
from medical_rag.quality.codes import QualityFailureCode
from medical_rag.quality.contracts import QualityDecision
from medical_rag.quality.gate import QualityGate, QualityPolicy

if TYPE_CHECKING:
    # 仅用于类型标注；在 TYPE_CHECKING 下导入，以让本模块的运行期
    # 导入图不必依赖它仅仅用来描述的那些解析契约。
    from medical_rag.parsing.contracts import CleanedDocument

# 全局发布门禁阈值 —— 保守的合成冷启动取值，
# (设计 §9). The per-document fixture expectations (expected_*) are the primary
# 契约；这些全局值为短解析/短扫描兜底。
MIN_ANCHOR_ACCURACY = 0.99
MIN_TEXT_COVERAGE = 0.95

# 从 fixture 发现中排除的基线文件名。
BASELINE_FILE = "baseline.json"

# 存储在基线报告中的六项 RAG 基线（设计 Step 2）。
RAG_BASELINE_KEYS = (
    "chunk_recall_at_k",
    "mrr",
    "citation_accuracy",
    "permission_violations",
    "deletion_residuals",
    "p95_latency_ms",
)

MISSING_KEYS = {
    "expected_blocks",
    "expected_anchors",
    "expected_sections",
    "expected_tables",
    "expected_facts",
    "expected_review_reasons",
}
_BLOCK_REQUIRED = {"block_id", "block_type", "raw_text"}


class FixtureSchemaError(ValueError):
    """黄金 fixture 格式错误时抛出（fail-loud）。"""


# ---------------------------------------------------------------------------
# fixture 加载 / schema 校验
# ---------------------------------------------------------------------------


def load_fixture(path: str | Path | Mapping[str, object]) -> Mapping[str, object]:
    """从路径或内联映射加载并校验一个 fixture。"""
    if isinstance(path, Mapping):
        fixture = dict(path)
    else:
        with Path(path).open(encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise FixtureSchemaError(f"fixture must be a JSON object: {path}")
        fixture = dict(loaded)
    return validate_fixture_schema(fixture)


def fixture_paths(fixtures_dir: str | Path) -> list[Path]:
    """在目录中发现 ``*.json`` fixture，跳过基线文件。"""
    directory = Path(fixtures_dir)
    if not directory.is_dir():
        raise FixtureSchemaError(f"fixtures directory not found: {directory}")
    return sorted(
        path
        for path in directory.glob("*.json")
        if path.name != BASELINE_FILE
    )


def validate_fixture_schema(fixture: Mapping[str, object]) -> Mapping[str, object]:
    """校验必需键并返回该 fixture（格式错误即 fail-closed）。"""
    missing = [key for key in MISSING_KEYS if key not in fixture]
    if missing:
        raise FixtureSchemaError(
            f"fixture missing required expected_* keys: {', '.join(missing)}"
        )
    if "file_type" not in fixture or not str(fixture["file_type"]).strip():
        raise FixtureSchemaError("fixture missing non-empty 'file_type'")
    if "parsed" not in fixture or not isinstance(fixture["parsed"], dict):
        raise FixtureSchemaError("fixture missing 'parsed' object")

    blocks = fixture["parsed"].get("blocks", ())
    if not isinstance(blocks, list):
        raise FixtureSchemaError("fixture 'parsed.blocks' must be a list")
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise FixtureSchemaError(f"parsed.blocks[{index}] must be an object")
        missing_block = [key for key in _BLOCK_REQUIRED if key not in block]
        if missing_block:
            raise FixtureSchemaError(
                f"parsed.blocks[{index}] missing {', '.join(missing_block)}"
            )
        if block["block_type"] not in {"heading", "paragraph", "list", "table", "image"}:
            raise FixtureSchemaError(
                f"parsed.blocks[{index}] invalid block_type {block['block_type']!r}"
            )
        # fail-closed：块的锚点（若有）必须能从声明的
        # 几何/结构信息推导出来，绝不能是跳过推导的任意字面量。
        if _derive_anchor(block) is None and any(
            key in block for key in ("bbox", "sheet_name", "cell_range", "page_number", "anchor")
        ):
            # 某个锚点决定因素存在但格式错误 -> 让下方的
            # 推导/类型检查暴露出确切问题。
            if "bbox" in block:
                bbox = block["bbox"]
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                    raise FixtureSchemaError(f"parsed.blocks[{index}].bbox must be [x,y,w,h]")
                for value in bbox:
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        raise FixtureSchemaError(f"parsed.blocks[{index}].bbox values must be numeric")
            if "cell_range" in block and not isinstance(block["cell_range"], str):
                raise FixtureSchemaError(f"parsed.blocks[{index}].cell_range must be a string")
            if "sheet_name" in block and not isinstance(block["sheet_name"], str):
                raise FixtureSchemaError(f"parsed.blocks[{index}].sheet_name must be a string")
            if "anchor" in block and not isinstance(block["anchor"], str):
                raise FixtureSchemaError(f"parsed.blocks[{index}].anchor must be a string")
            if block.get("page_number") is not None and not isinstance(
                block["page_number"], (int, float)
            ):
                raise FixtureSchemaError(f"parsed.blocks[{index}].page_number must be numeric")
            if "converted_tables" in block:
                raise FixtureSchemaError("deprecated key converted_tables")

    reasons = fixture.get("expected_review_reasons", ())
    if not isinstance(reasons, list):
        raise FixtureSchemaError("'expected_review_reasons' must be a list")
    for reason in reasons:
        if reason not in QualityFailureCode:
            raise FixtureSchemaError(
                f"expected_review_reasons contains non-canonical slug {reason!r}; "
                "reuse QualityFailureCode values instead of declaring new strings"
            )
    return fixture


# ---------------------------------------------------------------------------
# 解析级块重建（合成 fixture 的"解析器"步骤）
# ---------------------------------------------------------------------------


def _derive_anchor(raw: Mapping[str, object]) -> str | None:
    """从声明的几何/结构真值推导 ``source_anchor``。

    这是唯一的锚点推导规则，因此一个 fixture 无法夹带其声明几何并不支持的锚点
    （FIX 4 —— 不做同义反复）。优先级与真实适配器产出锚点的方式一致：

    * ``bbox``（OCR 坐标 ``[x, y, w, h]``）-> ``x{x}:y{y}:w{w}:h{h}``
    * ``sheet_name`` + ``cell_range`` -> ``sheet:{sheet}!{cell_range}``
    * ``page_number`` -> ``p{page}:text``
    * ``anchor``（显式结构锚点，如 DOCX 的 ``doc:paragraph:N``）
      在没有几何信息时被接受为*规范化*锚点。
    * 否则 ``None``（fail-closed：不发明锚点）。
    """
    bbox = raw.get("bbox")
    if bbox:
        x, y, w, h = (float(value) for value in bbox)
        return f"x{x:g}:y{y:g}:w{w:g}:h{h:g}"
    sheet = raw.get("sheet_name")
    cell_range = raw.get("cell_range")
    if sheet and cell_range:
        return f"sheet:{sheet}!{cell_range}"
    page = raw.get("page_number")
    if page is not None:
        return f"p{int(page)}:text"
    anchor = raw.get("anchor")
    if anchor:
        return str(anchor)
    return None


def _make_block(raw: Mapping[str, object], index: int) -> ParsedBlock:
    """从 fixture 载荷字典构建一个 :class:`ParsedBlock`。

    ``source_anchor`` 总是通过 :func:`_derive_anchor` 从声明的几何信息**推导**而来
    （绝不从 fixture 字面量复制），因此一个带错误 page/坐标/范围数据的块会产生错误
    的锚点，从而未通过黄金期望，而不是靠构造本身通过。
    """
    bbox_raw = raw.get("bbox")
    bbox: tuple[float, float, float, float] | None = None
    if bbox_raw is not None:
        bbox = tuple(float(value) for value in bbox_raw)  # type: ignore[assignment]
    section_raw = raw.get("section_path", ())
    section_path = tuple(str(value) for value in section_raw) if section_raw else ()
    return ParsedBlock(
        block_id=str(raw["block_id"]),
        block_type=str(raw["block_type"]),  # type: ignore[arg-type]
        raw_text=str(raw["raw_text"]),
        page_number=None if raw.get("page_number") is None else int(raw["page_number"]),
        sheet_name=None if raw.get("sheet_name") is None else str(raw["sheet_name"]),
        cell_range=None if raw.get("cell_range") is None else str(raw["cell_range"]),
        bbox=bbox,
        section_path=section_path,
        reading_order=int(raw.get("reading_order", index)),
        language=str(raw.get("language") or "und"),
        confidence=float(raw.get("confidence", 1.0)),
        source_anchor=_derive_anchor(raw),
    )


# ---------------------------------------------------------------------------
# 结果值对象
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GoldenFixtureResult:
    """某个黄金 fixture 的确定性度量。"""

    fixture_id: str
    file_type: str
    anchor_accuracy: float
    text_coverage: float
    table_header_retention: bool
    block_count: int
    fact_count: int
    produced_anchors: tuple[str, ...]
    produced_sections: tuple[tuple[str, ...], ...]
    produced_review_reasons: tuple[str, ...]
    quality_status: QualityStatus
    # 基于该黄金集测得的 RAG 侧指标。
    chunk_recall_at_k: float
    mrr: float
    citation_accuracy: float
    permission_violations: int
    deletion_residuals: int
    pipeline_latency_ms: float
    # FIX 5：把已计算却被丢弃的 QualityGate 指标暴露出来，这样 Task
    # 11 的可观测键就出自同一个运行器。
    anchor_coverage: float
    ocr_confidence: float
    table_column_consistency: float
    # 各阶段耗时（Task 11 阶段计数器），外加聚合延迟。
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    # 内存中的管线工件（仅 id / 仅锚点，不含 PHI 的引用）。
    chunks: list[EvidenceUnitDraft] = field(default_factory=list)
    facts: list = field(default_factory=list)
    decision: QualityDecision | None = None
    expected: Mapping[str, object] = field(default_factory=dict)
    fails: list[str] = field(default_factory=list)

    @property
    def review_reasons_ok(self) -> bool:
        return "expected_review_reasons" not in self.fails

    def as_baseline_dict(self) -> dict[str, object]:
        """投影出与基线相关的字段（无原始文本 / 无 PHI）。"""
        return {
            "fixture_id": self.fixture_id,
            "file_type": self.file_type,
            "anchor_accuracy": self.anchor_accuracy,
            "text_coverage": self.text_coverage,
            "table_header_retention": self.table_header_retention,
            "block_count": self.block_count,
            "fact_count": self.fact_count,
            "quality_status": self.quality_status.value,
            "chunk_recall_at_k": self.chunk_recall_at_k,
            "mrr": self.mrr,
            "citation_accuracy": self.citation_accuracy,
            "permission_violations": self.permission_violations,
            "deletion_residuals": self.deletion_residuals,
            "pipeline_latency_ms": round(self.pipeline_latency_ms, 3),
            "anchor_coverage": round(self.anchor_coverage, 4),
            "ocr_confidence": round(self.ocr_confidence, 4),
            "table_column_consistency": round(self.table_column_consistency, 4),
            "stage_timings_ms": {key: round(value, 3) for key, value in self.stage_timings_ms.items()},
        }


# ---------------------------------------------------------------------------
# 单 fixture 度量辅助函数
# ---------------------------------------------------------------------------


def _anchor_accuracy(expected: Sequence[str], produced: Sequence[str]) -> float:
    """针对 fixture 期望锚点的 fail-closed 锚点准确率。"""
    expected_set = set(expected)
    produced_set = {anchor for anchor in produced if anchor}
    missing = expected_set - produced_set
    invented = produced_set - expected_set
    if not missing and not invented:
        return 1.0
    total_penalties = len(missing) + len(invented)
    return max(0.0, 1.0 - total_penalties / max(1, len(expected_set)))


def _table_header_retention(expected_tables: Sequence[Mapping[str, object]], chunks: Sequence[EvidenceUnitDraft]) -> bool:
    """每个期望表的表头单元格都保留在产出的表块中。"""
    if expected_tables is None or not expected_tables:
        return True
    produced_lines = [
        chunk.clean_text or chunk.text
        for chunk in chunks
        if chunk.kind == EvidenceKind.REPORT_TABLE
    ]
    for table in expected_tables:
        headers = [str(value) for value in table.get("headers", ())]
        if not headers:
            continue
        if not any(all(header in line for header in headers) for line in produced_lines):
            return False
    return True


def _fact_match(expected_facts: Sequence[Mapping[str, object]], facts: Sequence[object]) -> list[str]:
    """检查每个期望事实（名称+取值 [可选单位]）都被抽取出来。"""
    failures: list[str] = []
    actual = [
        (getattr(fact, "name", None), getattr(fact, "value", None), getattr(fact, "unit", None))
        for fact in facts
    ]
    for expected in expected_facts:
        name = str(expected.get("name", ""))
        value = str(expected.get("value", ""))
        unit = expected.get("unit")
        matched = any(
            n == name and v == value and (unit is None or u == unit)
            for (n, v, u) in actual
        )
        if not matched:
            failures.append(f"missing_expected_fact:{name}={value}")
    return failures


def _section_match(expected_sections: Sequence[Sequence[str]], produced_sections: Sequence[tuple[str, ...]]) -> list[str]:
    failures: list[str] = []
    for section in expected_sections:
        wanted = tuple(str(value) for value in section)
        if wanted not in produced_sections:
            failures.append(f"missing_expected_section:{'/'.join(wanted)}")
    return failures


def _stable_chunk_anchor(chunk: EvidenceUnitDraft) -> str:
    """产出块的确定性锚点键（锚点或 'none:<idx>'）。"""
    if chunk.source_anchor:
        return chunk.source_anchor
    return f"none:{chunk.chunk_index}"


# ---------------------------------------------------------------------------
# 唯一的管线运行器
# ---------------------------------------------------------------------------


def _run_pipeline(
    fixture: Mapping[str, object],
) -> tuple[
    CleanedDocument,
    list[EvidenceUnitDraft],
    list,
    QualityDecision,
    float,
    dict[str, float],
]:
    """在 fixture 的块上恰好运行一次 clean→chunk→extract→gate。

    注意（Task 11 接缝 / 绕过解析器的局限）：fixture 注入的是 ``ParsedBlock``，
    而非经 adapters.py 解析器传输的真实文件。后果：``DocumentCleaner`` 是在一个
    块元组上被调用，因此 ``ocr_source`` 为 False，cleaner 的 OCR 专用分支 +
    ``FLAG_OCR_NOISE_CHECKED`` 都**未**被覆盖，且 chunk 携带
    ``parser_version=None``。Task 11 的逐页 OCR 可观测性应通过它自己的
    ``ParsedDocument``/``blocks`` 路径传输真实 OCR 后端输出，而不是依赖这些合成块。
    各阶段耗时会被返回，供 Task 11 阶段计数器使用。
    """
    start_all = perf_counter()
    blocks = tuple(
        _make_block(block, index)
        for index, block in enumerate(fixture["parsed"].get("blocks", ()))  # type: ignore[union-attr]
    )
    parse_ms = (perf_counter() - start_all) * 1000.0

    start_clean = perf_counter()
    cleaned = DocumentCleaner().clean(blocks)
    clean_ms = (perf_counter() - start_clean) * 1000.0

    start_chunk = perf_counter()
    chunks = SemanticChunker().chunk(cleaned)
    chunk_ms = (perf_counter() - start_chunk) * 1000.0

    start_extract = perf_counter()
    facts = RuleBasedMedicalFactExtractor().extract(chunks)
    extract_ms = (perf_counter() - start_extract) * 1000.0

    start_gate = perf_counter()
    decision = QualityGate(QualityPolicy()).evaluate(cleaned, chunks, facts.facts)
    gate_ms = (perf_counter() - start_gate) * 1000.0

    latency_ms = (perf_counter() - start_all) * 1000.0
    stage_timings = {
        "parse_ms": parse_ms,
        "clean_ms": clean_ms,
        "chunk_ms": chunk_ms,
        "extract_ms": extract_ms,
        "gate_ms": gate_ms,
    }
    return cleaned, chunks, facts.facts, decision, latency_ms, stage_timings


def run_golden_fixture(fixture: Mapping[str, object]) -> GoldenFixtureResult:
    """在某个已校验 fixture 上运行共享管线，并与全部期望做比较。"""
    fixture_id = str(fixture.get("id") or fixture.get("file_type") or "unnamed")
    file_type = str(fixture["file_type"])

    _, chunks, facts, decision, latency_ms, stage_timings = _run_pipeline(fixture)

    expected_blocks = int(fixture["expected_blocks"])
    expected_anchors = [str(value) for value in fixture["expected_anchors"]]
    expected_sections = [
        [str(value) for value in section]
        for section in fixture.get("expected_sections", ())
    ]
    expected_tables = fixture.get("expected_tables", ())
    expected_facts = fixture.get("expected_facts", ())
    expected_reasons = {str(value) for value in fixture.get("expected_review_reasons", ())}

    produced_anchors = tuple(dict.fromkeys(_stable_chunk_anchor(chunk) for chunk in chunks))
    produced_sections = tuple(dict.fromkeys(chunk.section_path for chunk in chunks))
    text_coverage = float(decision.metrics["text_coverage"])
    anchor_accuracy = _anchor_accuracy(expected_anchors, produced_anchors)
    table_header_retention = _table_header_retention(expected_tables, chunks)

    fails: list[str] = []
    if anchor_accuracy < 1.0:
        fails.append("anchor_payload_mismatch")
    if len(chunks) != expected_blocks:
        fails.append(f"block_count:expected={expected_blocks},produced={len(chunks)}")
    fails.extend(_section_match(expected_sections, produced_sections))
    fails.extend(_fact_match(expected_facts, facts))
    produced_reasons = tuple(decision.reasons)
    if set(produced_reasons) != expected_reasons:
        fails.append(
            f"review_reasons:expected={sorted(expected_reasons)}"
            f",produced={sorted(produced_reasons)}"
        )

    # 基于该 fixture 所声明评测用例测得的 RAG 指标。
    recall, mrr, citation, permission, deletion = _rag_metrics_for_fixture(
        fixture, chunks, anchor_accuracy
    )

    result = GoldenFixtureResult(
        fixture_id=fixture_id,
        file_type=file_type,
        anchor_accuracy=anchor_accuracy,
        text_coverage=text_coverage,
        table_header_retention=table_header_retention,
        block_count=len(chunks),
        fact_count=len(facts),
        produced_anchors=produced_anchors,
        produced_sections=produced_sections,
        produced_review_reasons=produced_reasons,
        quality_status=decision.status,
        chunk_recall_at_k=recall,
        mrr=mrr,
        citation_accuracy=citation,
        permission_violations=permission,
        deletion_residuals=deletion,
        pipeline_latency_ms=latency_ms,
        anchor_coverage=float(decision.metrics["anchor_coverage"]),
        ocr_confidence=float(decision.metrics["ocr_confidence"]),
        table_column_consistency=float(decision.metrics["table_column_consistency"]),
        stage_timings_ms=stage_timings,
        chunks=chunks,
        facts=facts,
        decision=decision,
        expected=dict(fixture),
        fails=fails,
    )
    return result


def _rag_metrics_for_fixture(
    fixture: Mapping[str, object],
    chunks: Sequence[EvidenceUnitDraft],
    anchor_accuracy: float,
) -> tuple[float, float, float, int, int]:
    """从 fixture 所声明的评测用例推导出的确定性 RAG 基线。"""
    evaluation = fixture.get("evaluation") or {}
    gold_anchors = tuple(str(a) for a in evaluation.get("gold_anchors", ()))
    forbidden_anchors = set(str(a) for a in evaluation.get("forbidden_anchors", ()))
    deleted_anchors = set(str(a) for a in evaluation.get("deleted_anchors", ()))

    # 模拟检索：产出的块按阅读顺序排序（全部），
    # 因此 K == len(produced)。对黄金基线而言这既确定又诚实。
    produced = [_stable_chunk_anchor(chunk) for chunk in chunks]
    returned = list(dict.fromkeys(produced))

    recall, mrr, permission, deletion = 1.0, 1.0, 0, 0
    if gold_anchors:
        case = EvaluationCase(
            id=str(evaluation.get("id") or "golden-r1"),
            question=str(evaluation.get("question") or "golden question"),
            scope="private",
            gold_evidence_ids=gold_anchors,
            forbidden_evidence_ids=tuple(forbidden_anchors),
            deleted_evidence_ids=tuple(deleted_anchors),
        )
        metrics = evaluate_retrieval(case, returned)
        recall = metrics.recall_at_k
        mrr = metrics.mrr
        permission = metrics.permission_violations
        deletion = metrics.deletion_residuals

    # 引用准确率：每个产出的锚点都必须能落到某个声明的
    # 期望锚点上 —— 凭空捏造（无法追溯）的锚点会拉低它，而它
    # 又受 fail-closed 锚点准确率封顶。
    expected_set = set(str(a) for a in fixture.get("expected_anchors", ()))
    produced_set = set(_stable_chunk_anchor(chunk) for chunk in chunks)
    citation_accuracy = 1.0
    if produced_set:
        citation_accuracy = len(produced_set & expected_set) / len(produced_set)
    citation_accuracy = min(citation_accuracy, anchor_accuracy)
    return recall, mrr, citation_accuracy, permission, deletion


# ---------------------------------------------------------------------------
# 基线报告 + 发布门禁
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BaselineReport:
    """基于黄金集测得的聚合、无 PHI 基线指标。"""

    fixtures: list[dict[str, object]]
    chunk_recall_at_k: float
    mrr: float
    citation_accuracy: float
    permission_violations: int
    deletion_residuals: int
    p95_latency_ms: float
    anchor_min: float
    text_coverage_min: float
    all_tables_retained: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_recall_at_k": self.chunk_recall_at_k,
            "mrr": self.mrr,
            "citation_accuracy": self.citation_accuracy,
            "permission_violations": self.permission_violations,
            "deletion_residuals": self.deletion_residuals,
            "p95_latency_ms": self.p95_latency_ms,
            "anchor_min": self.anchor_min,
            "text_coverage_min": self.text_coverage_min,
            "all_tables_retained": self.all_tables_retained,
            "fixtures": self.fixtures,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "BaselineReport":
        """从已持久化的报告重建基线（供发布门禁
        用一次全新运行对比*加载的*存储基线，FIX 1）。"""
        fixtures = data.get("fixtures", ())
        if not isinstance(fixtures, list):
            raise FixtureSchemaError("baseline report 'fixtures' must be a list")

        def number(key: str) -> float:
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FixtureSchemaError(f"baseline report field {key!r} must be numeric")
            return float(value)

        return cls(
            fixtures=fixtures,
            chunk_recall_at_k=number("chunk_recall_at_k"),
            mrr=number("mrr"),
            citation_accuracy=number("citation_accuracy"),
            permission_violations=int(number("permission_violations")),
            deletion_residuals=int(number("deletion_residuals")),
            p95_latency_ms=number("p95_latency_ms"),
            anchor_min=number("anchor_min"),
            text_coverage_min=number("text_coverage_min"),
            all_tables_retained=bool(data.get("all_tables_retained", True)),
        )


def compute_baseline(
    fixtures: Iterable[Mapping[str, object]],
    results: Sequence[GoldenFixtureResult] | None = None,
) -> BaselineReport:
    """计算基于黄金集测得的基线（设计 Step 2）。"""
    result_list = list(results) if results is not None else [
        run_golden_fixture(f) for f in fixtures
    ]
    if not result_list:
        raise FixtureSchemaError("cannot compute a baseline from an empty golden set")

    def avg(key: str) -> float:
        return mean(getattr(item, key) for item in result_list)

    p95 = _percentile_ms(sorted(item.pipeline_latency_ms for item in result_list))
    return BaselineReport(
        fixtures=[item.as_baseline_dict() for item in result_list],
        chunk_recall_at_k=avg("chunk_recall_at_k"),
        mrr=avg("mrr"),
        citation_accuracy=avg("citation_accuracy"),
        permission_violations=sum(item.permission_violations for item in result_list),
        deletion_residuals=sum(item.deletion_residuals for item in result_list),
        p95_latency_ms=round(p95, 3),
        anchor_min=min(item.anchor_accuracy for item in result_list),
        text_coverage_min=min(item.text_coverage for item in result_list),
        all_tables_retained=all(item.table_header_retention for item in result_list),
    )


def _percentile_ms(sorted_ms: Sequence[float]) -> float:
    """排序后延迟样本的 P95（确定性）。"""
    if not sorted_ms:
        return 0.0
    index = min(len(sorted_ms) - 1, max(0, int(0.95 * len(sorted_ms)) - 1))
    return sorted_ms[index]


def load_fixture_data(fixtures_dir: str | Path) -> tuple[list[Mapping[str, object]], list[str]]:
    """稳健地加载目录中的全部 fixture。

    返回 ``(valid_fixtures, error_reasons)``。目录缺失、JSON 格式错误和
    schema 非法的 fixture 都会被收集为原因（FIX 2），这样发布门禁可以干净地失败，
    而不是抛栈。
    """
    directory = Path(fixtures_dir)
    errors: list[str] = []
    if not directory.is_dir():
        return [], [f"fixtures directory not found: {directory}"]
    fixtures: list[Mapping[str, object]] = []
    for path in fixture_paths(directory):
        try:
            fixtures.append(load_fixture(path))
        except (FixtureSchemaError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
    return fixtures, errors


def run_fixtures(
    fixtures: Iterable[Mapping[str, object]],
    results: Sequence[GoldenFixtureResult] | None = None,
) -> tuple[list[GoldenFixtureResult], list[str]]:
    """在每个 fixture 上运行共享管线，并隔离单个 fixture 的失败。

    返回 ``(results_for_valid_fixtures, error_reasons)``，这样空集合或格式错误的
    fixture 会变成一个干净的 ``GateVerdict``，而不是一次崩溃。
    """
    result_list: list[GoldenFixtureResult] = []
    errors: list[str] = []
    if results is not None:
        result_list = list(results)
    else:
        for fixture in fixtures:
            label = str(fixture.get("id") or fixture.get("file_type") or "?") if isinstance(fixture, Mapping) else "?"
            try:
                validated = validate_fixture_schema(fixture) if isinstance(fixture, Mapping) else fixture
                result_list.append(run_golden_fixture(validated))
            except (FixtureSchemaError, KeyError, ValueError, TypeError) as exc:
                errors.append(f"fixture {label}: {exc}")
    return result_list, errors


@dataclass(slots=True)
class GateVerdict:
    """发布门禁比较的结果；退出码语义 0=通过 / 1=失败。"""

    passed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1


# P95 延迟是墙钟时间，会在微秒级抖动；只对
# 严重回归（默认 3 倍基线）设门禁，这样计时噪声永远不会误触发门禁，
# 而真实的变慢仍会让它失败。
P95_REGRESSION_FACTOR = 3.0


def release_gate(
    fixtures: Iterable[Mapping[str, object]],
    baseline: BaselineReport | None = None,
    results: Sequence[GoldenFixtureResult] | None = None,
) -> GateVerdict:
    """返回发布判定：通过为 0；若任一指标低于*存储的*
    基线、或任一权限/删除安全检查非零则为 1。

    ``baseline`` 是此前存储的黄金集基线（经 :func:`BaselineReport.from_dict`
    从 ``baseline.json`` 加载）。全新运行（``results`` 或一次新运行）与它比较，
    因此 recall/mrr/citation 的下降是真实的门禁输入，而不是自我比较的空操作
    （FIX 1）。
    """
    base_reasons: list[str] = []
    result_list, run_errors = run_fixtures(fixtures, results)
    base_reasons.extend(run_errors)
    if not result_list and not run_errors:
        base_reasons.append("empty_golden_set")

    if not result_list:
        return GateVerdict(passed=False, reasons=base_reasons or ["empty_golden_set"])

    if baseline is None:
        # 没有存储基线：黄金集本身即定义基线并通过
        # （首次运行行为）。需要严格门禁的调用方传入一个
        # 已加载的基线。
        baseline = compute_baseline(fixtures, result_list)

    reasons: list[str] = list(base_reasons)
    for item in result_list:
        if item.anchor_accuracy < MIN_ANCHOR_ACCURACY:
            reasons.append(f"{item.fixture_id}:anchor_accuracy_below_{MIN_ANCHOR_ACCURACY}")
        if item.text_coverage < MIN_TEXT_COVERAGE:
            reasons.append(f"{item.fixture_id}:text_coverage_below_{MIN_TEXT_COVERAGE}")
        if not item.table_header_retention:
            reasons.append(f"{item.fixture_id}:table_header_retention_failed")
        if item.fails:
            reasons.append(f"{item.fixture_id}:{';'.join(item.fails)}")

    # RAG 基线：把全新聚合值与存储基线比较。
    current = compute_baseline(fixtures, result_list)
    for key in ("chunk_recall_at_k", "mrr", "citation_accuracy"):
        current_value = float(getattr(current, key))
        baseline_value = float(getattr(baseline, key))
        if current_value < baseline_value:
            reasons.append(f"{key}:current={current_value:.4f}<baseline={baseline_value:.4f}")

    # P95 延迟：容忍墙钟抖动，捕捉严重回归。
    if baseline.p95_latency_ms > 0 and current.p95_latency_ms > baseline.p95_latency_ms * P95_REGRESSION_FACTOR:
        reasons.append(
            f"p95_latency_ms:current={current.p95_latency_ms:.3f}baseline={baseline.p95_latency_ms:.3f}"
        )

    if current.permission_violations > baseline.permission_violations:
        reasons.append(f"permission_violations:current={current.permission_violations}")
    if current.deletion_residuals > baseline.deletion_residuals:
        reasons.append(f"deletion_residuals:current={current.deletion_residuals}")
    # 安全：当前运行中任何非零违规都会让门禁失败。
    if current.permission_violations > 0:
        reasons.append(f"permission_violations_nonzero:{current.permission_violations}")
    if current.deletion_residuals > 0:
        reasons.append(f"deletion_residuals_nonzero:{current.deletion_residuals}")

    return GateVerdict(
        passed=not reasons,
        reasons=sorted(set(reasons)),
    )


def load_baseline_from(path: str | Path) -> Mapping[str, object] | None:
    """加载此前写入的基线报告；不存在或非法时为 None。"""
    baseline_path = Path(path)
    if not baseline_path.is_file():
        return None
    try:
        with baseline_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


__all__ = [
    "BASELINE_FILE",
    "BaselineReport",
    "FixtureSchemaError",
    "GateVerdict",
    "GoldenFixtureResult",
    "MIN_ANCHOR_ACCURACY",
    "MIN_TEXT_COVERAGE",
    "P95_REGRESSION_FACTOR",
    "RAG_BASELINE_KEYS",
    "compute_baseline",
    "fixture_paths",
    "load_baseline_from",
    "load_fixture",
    "load_fixture_data",
    "release_gate",
    "run_fixtures",
    "run_golden_fixture",
    "validate_fixture_schema",
]

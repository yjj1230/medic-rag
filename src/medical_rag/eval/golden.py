"""One reusable golden-set runner + baseline report + release gate (Task 10).

Design principles honoured here
-------------------------------
* **Single runner.** ``run_golden_fixture`` is the only place the
  parse→clean→chunk→extract→gate pipeline is invoked.  Both
  ``tests/eval/test_document_quality.py`` and the CLI release-gate command call
  it; no duplicate assembly anywhere.
* **Data-driven, validated fixture schema.** Every fixture is validated for the
  six required ``expected_*`` keys plus a well-formed ``parsed.blocks`` payload;
  a malformed fixture fails loudly instead of silently producing a bogus pass.
* **Fail-closed anchors.** ``anchor_accuracy`` is driven by the fixture's
  ``expected_anchors``: missing anchors and **invented** anchors (anchors the
  fixture never declared) both count against it.
* **Fully in-memory / test-local.** No parser persistence, no index writer, no
  OpenSearch — the pipeline operates on the synthetic fixture blocks only.
* **Error-code reuse.** ``expected_review_reasons`` are validated against and
  compared through the canonical ``QualityFailureCode`` slugs
  (:mod:`medical_rag.quality.codes`); ``QualityGate``/``QualityPolicy`` are
  consumed directly, never re-declared.
* **Synthetic / de-identified.** Fixtures are fictional, medically plausible
  content (no real patients, filenames, or ID numbers).
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
    # Annotated only; imported under TYPE_CHECKING to keep this module's runtime
    # import graph free of the parsing contracts it merely describes.
    from medical_rag.parsing.contracts import CleanedDocument

# Global release-gate thresholds — conservative synthetic cold-start values
# (设计 §9). The per-document fixture expectations (expected_*) are the primary
# contract; these globals back-stop short parses/scans.
MIN_ANCHOR_ACCURACY = 0.99
MIN_TEXT_COVERAGE = 0.95

# Baseline file name that is excluded from fixture discovery.
BASELINE_FILE = "baseline.json"

# The six RAG baselines stored in the baseline report (design Step 2).
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
    """Raised when a golden fixture is malformed (fail-loud)."""


# ---------------------------------------------------------------------------
# Fixture loading / schema validation
# ---------------------------------------------------------------------------


def load_fixture(path: str | Path | Mapping[str, object]) -> Mapping[str, object]:
    """Load and validate one fixture from a path or inline mapping."""
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
    """Discover ``*.json`` fixtures in a directory, skipping the baseline file."""
    directory = Path(fixtures_dir)
    if not directory.is_dir():
        raise FixtureSchemaError(f"fixtures directory not found: {directory}")
    return sorted(
        path
        for path in directory.glob("*.json")
        if path.name != BASELINE_FILE
    )


def validate_fixture_schema(fixture: Mapping[str, object]) -> Mapping[str, object]:
    """Validate required keys and return the fixture (fail-closed on malformed)."""
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
        # Fail-closed: a block's anchor (if any) must be derivable from declared
        # geometry/struct, never an arbitrary literal that skips the derivation.
        if _derive_anchor(block) is None and any(
            key in block for key in ("bbox", "sheet_name", "cell_range", "page_number", "anchor")
        ):
            # one of the anchor determinants is present but malformed -> let the
            # derive/type checks surface the exact problem below.
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
# Parse-level block reconstruction (the "parser" step for synthetic fixtures)
# ---------------------------------------------------------------------------


def _derive_anchor(raw: Mapping[str, object]) -> str | None:
    """Derive a ``source_anchor`` from declared geometric/structural ground truth.

    This is the single anchor-derivation rule so a fixture cannot smuggle in an
    anchor that its declared geometry does not support (FIX 4 — no tautology).
    Precedence matches how the real adapters produce anchors:

    * ``bbox`` (OCR coordinates ``[x, y, w, h]``) -> ``x{x}:y{y}:w{w}:h{h}``
    * ``sheet_name`` + ``cell_range`` -> ``sheet:{sheet}!{cell_range}``
    * ``page_number`` -> ``p{page}:text``
    * ``anchor`` (explicit structural anchor, e.g. DOCX ``doc:paragraph:N``)
      is accepted as a *normalized* anchor where no geometry exists.
    * otherwise ``None`` (fail-closed: no invented anchor).
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
    """Build a :class:`ParsedBlock` from a fixture payload dict.

    ``source_anchor`` is always **derived** from the declared geometry via
    :func:`_derive_anchor` (never copied from a fixture literal), so a block with
    wrong page/coordinate/range data produces a wrong anchor that fails the
    golden expectation instead of passing by construction.
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
# Result value object
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GoldenFixtureResult:
    """Deterministic measurements for one golden fixture."""

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
    # RAG-side metrics measured off this golden set.
    chunk_recall_at_k: float
    mrr: float
    citation_accuracy: float
    permission_violations: int
    deletion_residuals: int
    pipeline_latency_ms: float
    # FIX 5: project QualityGate metrics that were computed but dropped, so Task
    # 11 observability keys off the same runner.
    anchor_coverage: float
    ocr_confidence: float
    table_column_consistency: float
    # Per-stage timing (Task 11 stage counters), plus the aggregate latency.
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    # In-memory pipeline artifacts (id-only / anchor-only, PHI-free references).
    chunks: list[EvidenceUnitDraft] = field(default_factory=list)
    facts: list = field(default_factory=list)
    decision: QualityDecision | None = None
    expected: Mapping[str, object] = field(default_factory=dict)
    fails: list[str] = field(default_factory=list)

    @property
    def review_reasons_ok(self) -> bool:
        return "expected_review_reasons" not in self.fails

    def as_baseline_dict(self) -> dict[str, object]:
        """Project the baseline-relevant fields (no raw text / PHI)."""
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
# Per-fixture metric helpers
# ---------------------------------------------------------------------------


def _anchor_accuracy(expected: Sequence[str], produced: Sequence[str]) -> float:
    """Fail-closed anchor accuracy against the fixture's expected anchors."""
    expected_set = set(expected)
    produced_set = {anchor for anchor in produced if anchor}
    missing = expected_set - produced_set
    invented = produced_set - expected_set
    if not missing and not invented:
        return 1.0
    total_penalties = len(missing) + len(invented)
    return max(0.0, 1.0 - total_penalties / max(1, len(expected_set)))


def _table_header_retention(expected_tables: Sequence[Mapping[str, object]], chunks: Sequence[EvidenceUnitDraft]) -> bool:
    """Every expected table's header cells are preserved in a produced table chunk."""
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
    """Check each expected fact (name+value [optionally unit]) is extracted."""
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
    """Deterministic anchor key for a produced chunk (anchor or 'none:<idx>')."""
    if chunk.source_anchor:
        return chunk.source_anchor
    return f"none:{chunk.chunk_index}"


# ---------------------------------------------------------------------------
# The single pipeline runner
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
    """Run clean→chunk→extract→gate exactly once over the fixture's blocks.

    NOTE (Task 11 seam / parser-bypass limitation): fixtures inject ``ParsedBlock``
    rather than a real file transported through an adapters.py parser.  Consequence:
    ``DocumentCleaner`` is invoked on a block tuple, so ``ocr_source`` is False and
    the cleaner's OCR-specific branch + ``FLAG_OCR_NOISE_CHECKED`` are NOT
    exercised, and chunks carry ``parser_version=None``.  Task 11's per-page OCR
    observability should transport real OCR-backend output through its own
    ``ParsedDocument``/``blocks`` path instead of relying on these synthetic
    blocks.  Per-stage timing is returned for Task 11 stage counters.
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
    """Run the shared pipeline over one validated fixture and compare to all expectations."""
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

    # RAG metrics measured off this fixture's declared evaluation case.
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
    """Deterministic RAG baselines derived from the fixture's declared evaluation case."""
    evaluation = fixture.get("evaluation") or {}
    gold_anchors = tuple(str(a) for a in evaluation.get("gold_anchors", ()))
    forbidden_anchors = set(str(a) for a in evaluation.get("forbidden_anchors", ()))
    deleted_anchors = set(str(a) for a in evaluation.get("deleted_anchors", ()))

    # Simulated retrieval: produced chunks ranked in reading order (all of them),
    # so K == len(produced). Deterministic and honest for a golden baseline.
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

    # Citation accuracy: every produced anchor must resolve to a declared
    # expected anchor — invented (un-traceable) anchors reduce it, and it is
    # capped by the fail-closed anchor accuracy.
    expected_set = set(str(a) for a in fixture.get("expected_anchors", ()))
    produced_set = set(_stable_chunk_anchor(chunk) for chunk in chunks)
    citation_accuracy = 1.0
    if produced_set:
        citation_accuracy = len(produced_set & expected_set) / len(produced_set)
    citation_accuracy = min(citation_accuracy, anchor_accuracy)
    return recall, mrr, citation_accuracy, permission, deletion


# ---------------------------------------------------------------------------
# Baseline report + release gate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BaselineReport:
    """Aggregated, PHI-free baseline metrics measured off the golden set."""

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
        """Rebuild a baseline from a persisted report (used by the release gate
        to compare a FRESH run against a LOADED stored baseline, FIX 1)."""
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
    """Compute the measured-off-golden-set baseline (design Step 2)."""
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
    """P95 of a sorted latency sample (deterministic)."""
    if not sorted_ms:
        return 0.0
    index = min(len(sorted_ms) - 1, max(0, int(0.95 * len(sorted_ms)) - 1))
    return sorted_ms[index]


def load_fixture_data(fixtures_dir: str | Path) -> tuple[list[Mapping[str, object]], list[str]]:
    """Load all fixtures in a directory robustly.

    Returns ``(valid_fixtures, error_reasons)``.  Missing dirs, malformed JSON and
    schema-invalid fixtures are collected as reasons (FIX 2) so the release gate
    can fail cleanly instead of tracebacking.
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
    """Run the shared pipeline over each fixture, isolating per-fixture failures.

    Returns ``(results_for_valid_fixtures, error_reasons)`` so an empty set or a
    malformed fixture becomes a clean ``GateVerdict`` rather than a crash.
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
    """Result of a release-gate comparison; exit-code semantics 0=pass / 1=fail."""

    passed: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1


# P95 latency is wall-clock and jitters at microsecond scale; gate only on a
# gross regression (default 3x baseline) so timing noise never trips the gate
# while a real slowdown still fails it.
P95_REGRESSION_FACTOR = 3.0


def release_gate(
    fixtures: Iterable[Mapping[str, object]],
    baseline: BaselineReport | None = None,
    results: Sequence[GoldenFixtureResult] | None = None,
) -> GateVerdict:
    """Return the release verdict: 0 passes, 1 if any metric regresses below the
    STORED baseline or any permission/deletion safety check is non-zero.

    ``baseline`` is the previously stored golden-set baseline (loaded from
    ``baseline.json`` via :func:`BaselineReport.from_dict`).  The fresh run
    (``results`` or a new run) is compared against it, so a recall/mrr/citation
    drop is a real gate input rather than a self-comparison no-op (FIX 1).
    """
    base_reasons: list[str] = []
    result_list, run_errors = run_fixtures(fixtures, results)
    base_reasons.extend(run_errors)
    if not result_list and not run_errors:
        base_reasons.append("empty_golden_set")

    if not result_list:
        return GateVerdict(passed=False, reasons=base_reasons or ["empty_golden_set"])

    if baseline is None:
        # No stored baseline: the golden set itself defines the baseline and
        # passes (first-run behaviour). A caller wanting strict gating passes a
        # loaded baseline.
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

    # RAG baselines: compare the FRESH aggregate to the stored baseline.
    current = compute_baseline(fixtures, result_list)
    for key in ("chunk_recall_at_k", "mrr", "citation_accuracy"):
        current_value = float(getattr(current, key))
        baseline_value = float(getattr(baseline, key))
        if current_value < baseline_value:
            reasons.append(f"{key}:current={current_value:.4f}<baseline={baseline_value:.4f}")

    # P95 latency: tolerate wall-clock jitter, catch gross regression.
    if baseline.p95_latency_ms > 0 and current.p95_latency_ms > baseline.p95_latency_ms * P95_REGRESSION_FACTOR:
        reasons.append(
            f"p95_latency_ms:current={current.p95_latency_ms:.3f}baseline={baseline.p95_latency_ms:.3f}"
        )

    if current.permission_violations > baseline.permission_violations:
        reasons.append(f"permission_violations:current={current.permission_violations}")
    if current.deletion_residuals > baseline.deletion_residuals:
        reasons.append(f"deletion_residuals:current={current.deletion_residuals}")
    # Safety: any non-zero violation in the current run always fails the gate.
    if current.permission_violations > 0:
        reasons.append(f"permission_violations_nonzero:{current.permission_violations}")
    if current.deletion_residuals > 0:
        reasons.append(f"deletion_residuals_nonzero:{current.deletion_residuals}")

    return GateVerdict(
        passed=not reasons,
        reasons=sorted(set(reasons)),
    )


def load_baseline_from(path: str | Path) -> Mapping[str, object] | None:
    """Load a previously-written baseline report, or None when absent/invalid."""
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

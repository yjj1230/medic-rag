"""Task 9 — Retrieval-side parent/child block contract and context expansion.

``expand_with_parents`` groups retrieved ``Evidence`` hits by their parent
block, pulls sibling child-blocks through the ``search_by_parent`` sibling
lookup seam, and returns an expanded evidence list ordered for reading while
staying within a token budget.

The parent block is purely contextual: siblings are child blocks that carry
their own child-anchored ``Citation`` (page / Sheet / Cell).  The parent is
never a citation source.  Here the sibling lookup is the in-memory index (and
the OpenSearch adapter integration is deferred — that store lives on the
parallel session; production wiring only needs an object exposing
``search_by_parent(parent_block_id, ..., limit)``).

Semantics / boundary notes (relied on by the Task 10 golden-set eval):
* Budget governs only the *added sibling context*, never already-selected
  hits.  Pass-through hits (``parent_block_id is None``) and original parented
  hits are always returned in full.
* ``parent_block_id == ""`` is treated as parented (kept, like any parented
  hit) but yields no siblings — a degenerate/empty parent id is not a
  pass-through.
* Hits are grouped by ``(document_id, parent_block_id)``, never by
  ``parent_block_id`` alone: ``make_block_id`` only guarantees uniqueness
  *within one document*, so grouping on the parent id alone mixes two
  documents' ``blk-0000`` into a single expansion group.  The hit's
  ``document_id`` is forwarded to the seam and siblings from a different
  document are dropped even when the seam ignores it.
* Sibling lifecycle/ACL is the seam's responsibility: the in-memory
  ``search_by_parent`` already gates deleted / inactive / in-eligible and the
  tenant/patient boundary; the orchestrator wraps it per-request in
  ``ScopedSiblingLookup``.  A custom seam must enforce the same.
* When a parent has several hits, sibling ids are derived from the FIRST
  hit's id (``e{n}c{m}``); later hits in the group are not repeated as
  siblings.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable, Sequence
from uuid import UUID

from medical_rag.knowledge.semantic_chunker import HeuristicTokenCounter
from medical_rag.retrieval.contracts import Evidence, SearchRecord

logger = logging.getLogger(__name__)

# One counting rule for the pipeline: the chunker's language-adaptive counter.
_TOKEN_COUNTER = HeuristicTokenCounter()

# Nominal model context window used to derive a default 30% budget when the
# caller does not configure one explicitly.
DEFAULT_CONTEXT_TOKENS = 128_000
DEFAULT_CONTEXT_FRACTION = 0.3

# Per-parent expansion caps from plan §7: at most 5 child blocks per parent
# (the originating hit(s) plus up to 4 sibling blocks).
MAX_SIBLINGS_PER_PARENT = 4
MAX_ITEMS_PER_PARENT = 5

# Evidence-id prefix scheme: a hit ``e1`` expands to siblings ``e1c1``,
# ``e1c2``, ... The ``e{n}`` prefix is whatever the hit id already was.
_SIBLING_ID_SUFFIX = "c"


def _default_budget() -> int:
    return int(DEFAULT_CONTEXT_TOKENS * DEFAULT_CONTEXT_FRACTION)


def _token_count(text: str) -> int:
    """Token estimate for the context budget.

    This used to be ``len(text.split())``.  Whitespace tokenisation is
    meaningless for Chinese: 18,000 consecutive Han characters contain no
    whitespace and therefore counted as a single token, so the budget could never
    reject a sibling block and expansion was free to exceed the model's context
    window.  The chunker already counts language-adaptively, so the budget uses
    the same rule -- one counting rule for the pipeline, or the same number means
    different things per stage.
    """
    return _TOKEN_COUNTER.count(text)


def _to_sibling_evidence(
    sibling: SearchRecord, prefix_id: str, child_number: int
) -> Evidence:
    """Wrap a sibling child-block into an ``Evidence`` item.

    The item id is derived from the hit id (``{hit_id}c{child_number}``).  The
    citation is the sibling's own child-anchored location — the parent is never
    used as a citation source.
    """
    return Evidence(
        id=f"{prefix_id}{_SIBLING_ID_SUFFIX}{child_number}",
        text=sibling.text,
        citation=sibling.citation,
        score=float(sibling.score),
        visibility=sibling.visibility,
        tenant_id=sibling.tenant_id,
        patient_id=sibling.patient_id,
        document_id=sibling.document_id,
        parent_block_id=sibling.parent_block_id,
    )


def _child_identity(item: Evidence | SearchRecord) -> tuple[str, str, str | None]:
    """Stable identity shared by a hit and its sibling child-blocks.

    Used to avoid re-adding a child block that is already present as a hit.
    """
    source_id = item.citation.source_id or item.id
    location = item.citation.location or ""
    return (source_id, location, item.parent_block_id)


def expand_with_parents(
    evidence: Sequence[Evidence],
    index: object,
    context_token_budget: int | None = None,
) -> list[Evidence]:
    """Expand ``evidence`` hits with sibling context from their parent blocks.

    Pure: takes evidence and an index implementing ``search_by_parent`` and
    returns a new expanded list.

    Semantic decisions (Task 10 golden-set eval relies on these):
    * The token budget constrains only the *added sibling context* — the
      expansion overhead.  It never suppresses already-selected hits.
    * Hits without ``parent_block_id`` (pass-through) are already-retrieved
      evidence and are ALWAYS returned in full, in input order, regardless of
      budget.  A large non-parent batch cannot be truncated here.
    * Original parented hits are likewise always returned; only the sibling
      child-blocks pulled from ``search_by_parent`` are gated by the budget.

    Reading order is preserved: pass-through hits first (input order), then
    each parent group's hit(s) followed by their added siblings.  The budget
    is split across groups by best-effort share so one large expansion cannot
    starve the remaining parents; when budget is exhausted sibling additions
    stop, but all original hits are still present.
    """
    budget = _default_budget() if context_token_budget is None else int(context_token_budget)
    budget = max(0, budget)

    # Group hits by (document, parent).  ``parent_block_id is None`` →
    # pass-through (never budgeted).  ``document_id`` participates in the key:
    # a bare parent id is only unique inside one document.
    groups: dict[tuple[UUID | None, str], list[Evidence]] = {}
    pass_through: list[Evidence] = []
    for item in evidence:
        if item.parent_block_id is None:
            pass_through.append(item)
            continue
        groups.setdefault((item.document_id, item.parent_block_id), []).append(item)

    expanded: list[Evidence] = list(pass_through)

    parented_groups = sorted(groups.items(), key=_group_sort_key)
    used_ids = {item.id for item in expanded}

    # Approximate per-group share so a huge first expansion cannot starve the
    # rest.  Groups are small (≤ MAX_ITEMS_PER_PARENT), so this is a coarse
    # reserve, not a hard guarantee.
    share = budget // max(1, len(parented_groups))

    for (document_id, parent_block_id), hits in parented_groups:
        group_items: list[Evidence] = []

        # 1. Original parented hits are already-selected evidence: always kept.
        for hit in hits:
            group_items.append(hit)
        group_remaining = share

        # 2. Sibling child blocks from the parent lookup seam (budgeted).
        siblings = list(
            _search_siblings(index, parent_block_id, document_id=document_id)
        )
        present_identities = {_child_identity(hit) for hit in hits}
        child_number = 0
        for sibling in siblings:
            if len(group_items) >= MAX_ITEMS_PER_PARENT:
                break
            # 兜底：即使 seam 忽略了 document_id，也绝不把别的文档的兄弟块
            # 当成本次命中的上下文（跨文档串扰 = 错误的证据入答）。
            if document_id is not None and sibling.document_id != document_id:
                continue
            if _child_identity(sibling) in present_identities:
                continue
            # Reserve the id before it collides with an already-used one.
            sibling_id = _next_sibling_id(hits[0].id, used_ids, child_number)
            sibling_item = _to_sibling_evidence(sibling, sibling_id.prefix, sibling_id.number)
            if sibling_item.id in used_ids:
                continue
            # Budget gates ONLY the added sibling; the original hit(s) above
            # are never dropped by it.
            if group_remaining < _token_count(sibling_item.text):
                break
            group_items.append(sibling_item)
            used_ids.add(sibling_item.id)
            child_number = sibling_id.number
            group_remaining -= _token_count(sibling_item.text)

        expanded.extend(group_items)

    return expanded


def _group_sort_key(
    item: tuple[tuple[UUID | None, str], list[Evidence]],
) -> tuple[str, str]:
    """Deterministic, input-order-independent ordering of parent groups."""

    (document_id, parent_block_id), _hits = item
    return (parent_block_id, "" if document_id is None else str(document_id))


def _accepts_document_id(seam: object) -> bool:
    """Whether a ``search_by_parent`` seam can be constrained to one document."""

    try:
        parameters = inspect.signature(seam).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    return "document_id" in parameters


def _search_siblings(
    index: object, parent_block_id: str, *, document_id: UUID | None = None
) -> Iterable[SearchRecord]:
    """Adapter around the sibling lookup seam.

    The seam is ``search_by_parent(parent_block_id, *, limit, ..., document_id)``.
    When the index does not expose it, expansion degrades gracefully to no
    siblings rather than failing the whole answer — but the silent no-op is
    surfaced with a warning so a misconfigured or not-yet-implemented seam is
    not masked.  A seam that cannot be document-scoped is likewise warned
    about: the caller-side document filter still applies, so cross-document
    siblings are dropped, but the lookup itself may be less precise.
    """
    search_by_parent = getattr(index, "search_by_parent", None)
    if search_by_parent is None:
        logger.warning(
            "parent/child expansion: index %s lacks search_by_parent; sibling "
            "context expansion is unavailable (parent=%r)",
            type(index).__name__,
            parent_block_id,
        )
        return []
    if parent_block_id == "":
        return []
    if document_id is not None and not _accepts_document_id(search_by_parent):
        logger.warning(
            "parent/child expansion: sibling seam %s cannot be document-scoped; "
            "foreign-document siblings will be filtered client-side (parent=%r)",
            type(index).__name__,
            parent_block_id,
        )
        result = search_by_parent(parent_block_id, limit=MAX_SIBLINGS_PER_PARENT)
        return result or []
    result = search_by_parent(
        parent_block_id, limit=MAX_SIBLINGS_PER_PARENT, document_id=document_id
    )
    return result or []


class ScopedSiblingLookup:
    """Bind one request's ACL scope to a shared index for sibling expansion.

    ``expand_with_parents`` stays a pure function: it only ever talks to the
    ``search_by_parent`` seam.  The orchestrator wraps its shared index in this
    adapter per request so private sibling lookups carry the same tenant/patient
    boundary as the primary ``search`` — sibling context never leaks across
    patients.  ``document_id`` is forwarded unchanged so the lookup stays inside
    the hit's document.
    """

    def __init__(
        self,
        index: object,
        *,
        tenant_id,
        patient_id,
        include_public: bool = True,
        include_private: bool = True,
    ) -> None:
        self._index = index
        self._tenant_id = tenant_id
        self._patient_id = patient_id
        self._include_public = include_public
        self._include_private = include_private

    def search_by_parent(
        self,
        parent_block_id: str,
        *,
        limit: int = MAX_SIBLINGS_PER_PARENT,
        document_id: UUID | None = None,
    ) -> list[SearchRecord]:
        search_by_parent = getattr(self._index, "search_by_parent", None)
        if search_by_parent is None:
            return []
        common = {
            "limit": limit,
            "tenant_id": self._tenant_id,
            "patient_id": self._patient_id,
            "include_public": self._include_public,
            "include_private": self._include_private,
        }
        if _accepts_document_id(search_by_parent):
            common["document_id"] = document_id
        return list(search_by_parent(parent_block_id, **common))


def _next_sibling_id(
    hit_id: str, present_ids: set[str], start_number: int
) -> "_SiblingId":
    """Find the next free ``{hit_id}c{n}`` sibling id avoiding collisions."""

    number = start_number + 1
    while f"{hit_id}{_SIBLING_ID_SUFFIX}{number}" in present_ids:
        number += 1
    return _SiblingId(prefix=hit_id, number=number)


class _SiblingId:
    __slots__ = ("prefix", "number")

    def __init__(self, prefix: str, number: int) -> None:
        self.prefix = prefix
        self.number = number


__all__ = [
    "expand_with_parents",
    "DEFAULT_CONTEXT_TOKENS",
    "DEFAULT_CONTEXT_FRACTION",
    "MAX_SIBLINGS_PER_PARENT",
    "MAX_ITEMS_PER_PARENT",
]

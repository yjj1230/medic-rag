"""Task 9 —— 检索侧的父/子块契约与上下文扩展。

``expand_with_parents`` 把检索到的 ``Evidence`` 命中按其父块分组，通过
``search_by_parent`` 兄弟查找接缝拉取兄弟子块，并返回一个按阅读顺序排列、
且处于 token 预算内的扩展证据列表。

父块纯粹是上下文：兄弟块是携带各自子锚点 ``Citation``（页 / Sheet / Cell）的子块。
父块绝不是引用来源。这里兄弟查找使用内存索引（OpenSearch 适配器集成被推迟 ——
该存储位于并行会话；生产接线只需一个暴露
``search_by_parent(parent_block_id, ..., limit)`` 的对象）。

语义 / 边界说明（Task 10 黄金集评测所依赖）：
* 预算只约束*新增的兄弟上下文*，绝不约束已选中的命中。透传命中
  （``parent_block_id is None``）与原有的有父命中总是完整返回。
* ``parent_block_id == ""`` 被视为有父（像任何有父命中一样保留），
  但不产生任何兄弟块 —— 退化/空的父 id 不是透传。
* 命中按 ``(document_id, parent_block_id)`` 分组，绝不只按 ``parent_block_id``：
  ``make_block_id`` 只保证在*一份文档内*唯一，因此只按父 id 分组会把两份文档的
  ``blk-0000`` 混进同一个扩展组。命中的 ``document_id`` 会转发给接缝，
  来自另一份文档的兄弟块即便接缝忽略它也会被丢弃。
* 兄弟块的生命周期/ACL 是接缝的责任：内存 ``search_by_parent`` 已经门禁了
  已删除 / 未激活 / 不合格以及租户/患者边界；编排器按请求把它包进
  ``ScopedSiblingLookup``。自定义接缝必须施加同样的约束。
* 当一个父块有多条命中时，兄弟 id 由**第一条**命中的 id 推导（``e{n}c{m}``）；
  该组中更晚的命中不会被重复为兄弟块。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable, Sequence
from uuid import UUID

from medical_rag.knowledge.semantic_chunker import HeuristicTokenCounter
from medical_rag.retrieval.contracts import Evidence, SearchRecord

logger = logging.getLogger(__name__)

# 管线使用同一条计数规则：分块器的语言自适应计数器。
_TOKEN_COUNTER = HeuristicTokenCounter()

# 调用方未显式配置时，用于推导默认 30% 预算的名义模型上下文窗口。
# 调用方未显式配置时，用于推导默认 30% 预算的名义模型上下文窗口。
DEFAULT_CONTEXT_TOKENS = 128_000
DEFAULT_CONTEXT_FRACTION = 0.3

# 来自方案 §7 的逐父扩展上限：每个父块至多 5 个子块
# （发起命中本身加上至多 4 个兄弟块）。
MAX_SIBLINGS_PER_PARENT = 4
MAX_ITEMS_PER_PARENT = 5

# 证据 id 前缀方案：命中 ``e1`` 扩展出兄弟块 ``e1c1``、
# ``e1c2``、……``e{n}`` 前缀就是该命中本身已有的 id。
_SIBLING_ID_SUFFIX = "c"


def _default_budget() -> int:
    return int(DEFAULT_CONTEXT_TOKENS * DEFAULT_CONTEXT_FRACTION)


def _token_count(text: str) -> int:
    """上下文预算的 token 估算。

    这以前是 ``len(text.split())``。按空白切分对中文毫无意义：18000 个连续的
    汉字不含空白，因此被算作一个 token，于是预算永远无法拒绝一个兄弟块，
    扩展可以自由超出模型的上下文窗口。分块器已经按语言自适应计数，因此预算使用
    同一条规则 —— 管线只有一条计数规则，否则同一个数字在不同阶段含义不同。
    """
    return _TOKEN_COUNTER.count(text)


def _to_sibling_evidence(
    sibling: SearchRecord, prefix_id: str, child_number: int
) -> Evidence:
    """把一个兄弟子块包装成一个 ``Evidence`` 项。

    项 id 由命中 id 推导（``{hit_id}c{child_number}``）。引用是兄弟块自身子锚点
    位置 —— 父块绝不作为引用来源。
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
        locator=sibling.locator,
        parent_block_id=sibling.parent_block_id,
    )


def _child_identity(item: Evidence | SearchRecord) -> tuple[str, str, str | None]:
    """一条命中与其兄弟子块共享的稳定标识。

    用于避免重复添加已作为命中存在的子块。
    """
    source_id = item.citation.source_id or item.id
    location = item.citation.location or ""
    return (source_id, location, item.parent_block_id)


def expand_with_parents(
    evidence: Sequence[Evidence],
    index: object,
    context_token_budget: int | None = None,
) -> list[Evidence]:
    """用来自父块的兄弟上下文扩展 ``evidence`` 命中。

    纯函数：接收证据和一个实现 ``search_by_parent`` 的索引，返回一个新的扩展列表。

    语义决策（Task 10 黄金集评测依赖这些）：
    * token 预算只约束*新增的兄弟上下文* —— 即扩展开销。它绝不抑制已选中的命中。
    * 没有 ``parent_block_id`` 的命中（透传）是已检索到的证据，无论预算如何都
      按输入顺序完整返回。一大批非父命中不能在这里被截断。
    * 原有的有父命中同样总是返回；只有从 ``search_by_parent`` 拉取的兄弟子块
      受预算门禁约束。

    阅读顺序被保留：透传命中在前（输入顺序），随后是每个父组的命中，再跟随其
    新增的兄弟块。预算按尽力份额在各组间分配，因此一次巨大的扩展不能饿死其余
    父块；预算耗尽时兄弟新增停止，但所有原有命中仍然存在。
    """
    budget = _default_budget() if context_token_budget is None else int(context_token_budget)
    budget = max(0, budget)

    # 按 (document, parent) 分组命中。``parent_block_id is None`` →
    # 透传（永不参与预算）。``document_id`` 参与键：
    # 裸父 id 只在一份文档内唯一。
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

    # 近似逐组份额，使一次巨大的首次扩展不能饿死
    # 其余组。各组都很小（≤ MAX_ITEMS_PER_PARENT），因此这是粗略
    # 预留，而非硬保证。
    share = budget // max(1, len(parented_groups))

    for (document_id, parent_block_id), hits in parented_groups:
        group_items: list[Evidence] = []

        # 1. 原有的有父命中是已选中的证据：总是保留。
        for hit in hits:
            group_items.append(hit)
        group_remaining = share

        # 2. 来自父块查找接缝的兄弟子块（参与预算）。
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
            # 在 id 与已用 id 冲突之前先预留它。
            sibling_id = _next_sibling_id(hits[0].id, used_ids, child_number)
            sibling_item = _to_sibling_evidence(sibling, sibling_id.prefix, sibling_id.number)
            if sibling_item.id in used_ids:
                continue
            # 预算只门禁新增的兄弟块；上方的原有
            # 命中绝不会因它被丢弃。
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
    """父组的确定性、与输入顺序无关的排序。"""

    (document_id, parent_block_id), _hits = item
    return (parent_block_id, "" if document_id is None else str(document_id))


def _accepts_document_id(seam: object) -> bool:
    """某个 ``search_by_parent`` 接缝能否被限定在一份文档内。"""

    try:
        parameters = inspect.signature(seam).parameters
    except (TypeError, ValueError):  # pragma: no cover - 罕见的可调用对象
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
    """围绕兄弟查找接缝的适配器。

    该接缝是 ``search_by_parent(parent_block_id, *, limit, ..., document_id)``。
    索引未暴露它时，扩展优雅降级为无兄弟块，而不是让整个答案失败 —— 但这个静默
    空操作会以警告形式暴露出来，使配置错误或尚未实现的接缝不被掩盖。无法按文档
    限定作用域的接缝同样会被警告：调用方侧的文档过滤仍然生效，因此跨文档的兄弟块
    会被丢弃，但查找本身可能不够精确。
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
    """把某次请求的 ACL 作用域绑定到共享索引，用于兄弟扩展。

    ``expand_with_parents`` 保持为纯函数：它只和 ``search_by_parent`` 接缝对话。
    编排器按请求把其共享索引包进本适配器，使私有兄弟查找携带与主 ``search``
    相同的租户/患者边界 —— 兄弟上下文绝不跨患者泄漏。``document_id`` 原样转发，
    使查找保持在该命中的文档内。
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
    """寻找下一个空闲且不冲突的 ``{hit_id}c{n}`` 兄弟 id。"""

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

from __future__ import annotations

from dataclasses import replace

from medical_rag.retrieval.contracts import SearchRecord


class HybridRanker:
    """针对词法与向量结果的确定性倒数排名融合（RRF）。

    原始 BM25 与向量分数仅为诊断而保留。排序完全基于排名贡献，其次基于稳定的
    记录 id。``calibrated_score`` 被归一化到 ``0..1`` 区间，其中被两条路径都排在
    第一的文档得分为 1.0。
    """

    def __init__(self, *, rrf_k: int = 60) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        self.rrf_k = rrf_k

    def fuse(
        self,
        lexical: list[SearchRecord],
        vector: list[SearchRecord],
        limit: int,
    ) -> list[SearchRecord]:
        if limit < 1:
            return []

        records: dict[str, SearchRecord] = {}
        contributions: dict[str, float] = {}
        seen_lexical: set[str] = set()
        seen_vector: set[str] = set()

        lexical_rank = 0
        for record in lexical:
            if record.id in seen_lexical:
                continue
            lexical_rank += 1
            seen_lexical.add(record.id)
            records[record.id] = record
            contributions[record.id] = contributions.get(record.id, 0.0) + 1.0 / (
                self.rrf_k + lexical_rank
            )

        vector_rank = 0
        for record in vector:
            if record.id in seen_vector:
                continue
            vector_rank += 1
            seen_vector.add(record.id)
            existing = records.get(record.id)
            if existing is None:
                records[record.id] = record
            else:
                # 保留词法命中的元数据，同时保留各条路径
                # 贡献的分数以供诊断。
                records[record.id] = replace(
                    existing,
                    vector_score=record.vector_score,
                    index_version=existing.index_version or record.index_version,
                    embedding_model=existing.embedding_model or record.embedding_model,
                )
            contributions[record.id] = contributions.get(record.id, 0.0) + 1.0 / (
                self.rrf_k + vector_rank
            )

        # 原始 RRF 可能的最大值是两个第一名贡献之和。一个
        # 归一化的伴随分数让阈值在不同候选集规模之间
        # 都有意义，同时避免对 BM25/向量
        # 分数尺度做任何比较。
        max_raw = 2.0 / (self.rrf_k + 1)
        fused = [
            replace(
                record,
                fused_score=contributions[record_id],
                calibrated_score=contributions[record_id] / max_raw,
            )
            for record_id, record in records.items()
        ]
        fused.sort(key=lambda record: (-(record.fused_score or 0.0), record.id))
        return fused[:limit]

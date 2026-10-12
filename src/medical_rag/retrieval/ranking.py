from __future__ import annotations

from dataclasses import replace

from medical_rag.retrieval.contracts import SearchRecord


class HybridRanker:
    """Deterministic Reciprocal Rank Fusion for lexical and vector results.

    Raw BM25 and vector scores are retained for diagnostics only.  Ordering is
    based exclusively on rank contributions, then on a stable record id.
    ``calibrated_score`` is normalized to a ``0..1`` range where a document
    ranked first by both paths scores 1.0.
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
                # Keep metadata from the lexical hit while preserving scores
                # contributed by each path for diagnostics.
                records[record.id] = replace(
                    existing,
                    vector_score=record.vector_score,
                    index_version=existing.index_version or record.index_version,
                    embedding_model=existing.embedding_model or record.embedding_model,
                )
            contributions[record.id] = contributions.get(record.id, 0.0) + 1.0 / (
                self.rrf_k + vector_rank
            )

        # The largest possible raw RRF value is two rank-1 contributions.  A
        # normalized companion score makes a threshold meaningful across
        # candidate-set sizes while avoiding any comparison of BM25/vector
        # score scales.
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

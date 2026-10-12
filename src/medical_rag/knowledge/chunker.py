from __future__ import annotations

import hashlib

from medical_rag.knowledge.contracts import KnowledgeChunkDraft, SourceDocument
from medical_rag.knowledge.semantic_chunker import SemanticChunker
from medical_rag.parsing.cleaner import DocumentCleaner
from medical_rag.parsing.text_source import TextSourceParser


class KnowledgeChunker:
    """Legacy compatibility wrapper over the shared semantic chunk chain.

    ``split`` routes SourceDocument.content through the same
    TextSourceParser → DocumentCleaner → SemanticChunker chain as the patient
    chain (设计 §3, 验收 #10), then maps the resulting EvidenceUnitDraft values
    back to KnowledgeChunkDraft without changing content_hash / location /
    section / URL semantics.  Heading blocks act as boundaries (never chunk
    text); ``section_path[-1]`` ≈ the legacy flat section.

    分块大小的**唯一来源**是 ``SemanticChunker`` 配置（target/min/max tokens、
    overlap、table_rows_per_chunk）。旧的 ``max_chars`` 参数已被删除：它被接受并
    校验却完全不驱动切分（死参数），而在 token 预算 + 语言自适应计数下，字符上限
    无法成为硬保证（中文 ≈1.5 字符/token，拉丁语系 ≈4）。需要更小的块就注入一个
    更小 token 预算的 chunker。
    """

    def __init__(
        self,
        chunker: SemanticChunker | None = None,
        cleaner: DocumentCleaner | None = None,
        parser: TextSourceParser | None = None,
    ) -> None:
        self._semantic = chunker or SemanticChunker()
        self._cleaner = cleaner or DocumentCleaner()
        self._parser = parser or TextSourceParser()

    @staticmethod
    def _hash(text: str) -> str:
        normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def split(self, source: SourceDocument) -> list[KnowledgeChunkDraft]:
        cleaned = self._cleaner.clean(self._parser.parse(source.content))
        return [
            KnowledgeChunkDraft.from_evidence(
                unit, source, content_hash=self._hash(unit.clean_text or unit.text)
            )
            for unit in self._semantic.chunk(cleaned)
        ]

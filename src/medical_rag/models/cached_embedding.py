from __future__ import annotations

import threading
from collections import OrderedDict

from medical_rag.models.contracts import EmbeddingModel


class CachedEmbeddingModel:
    """EmbeddingModel 协议的进程内 LRU 缓存装饰器。

    缓存键为文本内容本身（不含任何用户标识），相同措辞的高频问题不再重复
    调用远端 embedding。多副本部署时各进程缓存独立，互不影响正确性。
    """

    def __init__(self, inner: EmbeddingModel, *, maxsize: int = 1024) -> None:
        if maxsize < 1:
            raise ValueError("embedding cache maxsize must be positive")
        self._inner = inner
        self._maxsize = maxsize
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        results: list[list[float] | None] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_positions: list[int] = []
        with self._lock:
            for position, text in enumerate(texts):
                cached = self._cache.get(text)
                if cached is None:
                    missing_texts.append(text)
                    missing_positions.append(position)
                else:
                    self._cache.move_to_end(text)
                    results[position] = cached
        if missing_texts:
            vectors = self._inner.embed(missing_texts)
            if len(vectors) != len(missing_texts):
                raise ValueError("embedding vector count mismatch")
            with self._lock:
                for position, text, vector in zip(
                    missing_positions, missing_texts, vectors, strict=True
                ):
                    row = [float(value) for value in vector]
                    self._cache[text] = row
                    self._cache.move_to_end(text)
                    results[position] = row
                while len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)
        return [row for row in results if row is not None]

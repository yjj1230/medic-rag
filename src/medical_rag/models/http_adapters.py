from __future__ import annotations

import time
from typing import Any

import httpx

from medical_rag.models.contracts import (
    ChatModel,
    ChatRequest,
    ChatResponse,
    EmbeddingModel,
    ModelError,
    ModelTimeout,
    ProviderResponseError,
    RankedPassage,
    RerankerModel,
    SafetyClassifier,
    SafetyResult,
    VisionModel,
    VisionResponse,
)
from medical_rag.retrieval.contracts import RerankResult
from medical_rag.storage.object_store import ObjectRef


def _secret_value(api_key: object) -> str:
    if hasattr(api_key, "get_secret_value"):
        return str(api_key.get_secret_value())
    return str(api_key)


class _HttpAdapter:
    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = _secret_value(api_key)
        self.client = client or httpx.Client()

    def _headers(self, trace_id: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        return headers

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float, trace_id: str | None = None) -> httpx.Response:
        try:
            response = self.client.post(
                f"{self.base_url}/{path.lstrip('/')}",
                headers=self._headers(trace_id),
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except httpx.TimeoutException as exc:
            raise ModelTimeout("模型服务请求超时") from exc
        except httpx.HTTPError as exc:
            raise ProviderResponseError("模型服务返回错误") from exc


class OpenAIChatCompletionsAdapter(_HttpAdapter, ChatModel):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gpt-5.5",
        provider_id: str = "lan-chat",
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(base_url, api_key, client)
        self.model = model
        self.provider_id = provider_id

    # P1-8（评审 2026-10-12）：每条证据在 prompt 中必须自带 visibility 分层
    # 标注。此前 orchestrator 注释声称「分层注入，每条证据自带 visibility
    # 标注」，但 _evidence_prompt 实际平铺拼接，公共医学指南与患者私人
    # 报告数据在模型视角不可区分 —— 模型可能把患者数据当公共知识复述，
    # 或把公共指南当成患者本人情况。标注语言与 system prompt 的「区分患者
    # 报告中的客观事实与公共医学知识」约定对齐。
    _VISIBILITY_LABELS = {
        "public": "公共医学知识",
        "private": "患者私人资料",
    }

    @classmethod
    def _evidence_prompt(cls, request: ChatRequest) -> str:
        if not request.evidence:
            return request.prompt
        evidence = "\n\n".join(
            f"[{item.id}]（{cls._VISIBILITY_LABELS.get(str(item.visibility), '来源未知')}）"
            f"{item.text}\n来源：{item.citation.title} "
            f"{item.citation.version} {item.citation.location}"
            for item in request.evidence
        )
        return f"{request.prompt}\n\n可用证据（只能基于这些证据回答，注意区分标注的来源层级）：\n{evidence}"

    def generate(self, request: ChatRequest) -> ChatResponse:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": self._evidence_prompt(request)})
        payload = {
            "model": request.model or self.model,
            "messages": messages,
            "max_completion_tokens": request.max_output_tokens,
        }
        response = self._post(
            "/chat/completions",
            payload,
            timeout=request.timeout_seconds,
            trace_id=request.trace_id,
        )
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty content")
            usage = {
                str(key): int(value)
                for key, value in (data.get("usage") or {}).items()
                if isinstance(value, (int, float))
            }
            return ChatResponse(
                text=text,
                provider=self.provider_id,
                model=str(data.get("model", request.model or self.model)),
                trace_id=request.trace_id,
                finish_reason=choice.get("finish_reason"),
                usage=usage,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderResponseError("模型响应格式无效") from exc


class OpenAIEmbeddingAdapter(_HttpAdapter, EmbeddingModel):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "text-embedding-3-small",
        client: httpx.Client | None = None,
        max_retries: int = 2,
        timeout_seconds: float = 30.0,
    ) -> None:
        super().__init__(base_url, api_key, client)
        self.model = model
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": texts}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post("/embeddings", payload, timeout=self.timeout_seconds)
                rows = response.json()["data"]
                rows = sorted(rows, key=lambda item: int(item["index"]))
                vectors = [row["embedding"] for row in rows]
                if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
                    raise ValueError("embedding count mismatch")
                return [[float(value) for value in vector] for vector in vectors]
            except (ProviderResponseError, ModelTimeout, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(0.05 * (attempt + 1))
        if isinstance(last_error, ModelError):
            raise last_error
        raise ProviderResponseError("Embedding 响应格式无效") from last_error


class OpenAIRerankerAdapter(_HttpAdapter, RerankerModel):
    def __init__(self, base_url: str, api_key: str, model: str = "reranker", client: httpx.Client | None = None) -> None:
        super().__init__(base_url, api_key, client)
        self.model = model

    def rank(self, query: str, passages: list[str]) -> RerankResult:
        response = self._post(
            "/rerank",
            {"model": self.model, "query": query, "passages": passages},
            timeout=30.0,
        )
        try:
            # 返回检索层声明的唯一重排结果类型（``list[RankedPassage]`` 的
            # 具名子类），避免协议声明 list[int] 而适配器返回行的老问题。
            return RerankResult(
                RankedPassage(index=int(row["index"]), score=float(row["score"]))
                for row in response.json()["results"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError("Reranker 响应格式无效") from exc


class OpenAIVisionAdapter(_HttpAdapter, VisionModel):
    def __init__(self, base_url: str, api_key: str, model: str = "vision", client: httpx.Client | None = None) -> None:
        super().__init__(base_url, api_key, client)
        self.model = model

    def extract(self, file_ref: ObjectRef, instruction: str) -> VisionResponse:
        response = self._post(
            "/vision/extract",
            {"model": self.model, "object_key": file_ref.key, "instruction": instruction},
            timeout=60.0,
        )
        try:
            data = response.json()
            return VisionResponse(
                text=str(data["text"]),
                provider=str(data.get("provider", "lan-chat")),
                model=str(data.get("model", self.model)),
                confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError("Vision 响应格式无效") from exc


class OpenAISafetyClassifierAdapter(_HttpAdapter, SafetyClassifier):
    def __init__(self, base_url: str, api_key: str, model: str = "safety-classifier", client: httpx.Client | None = None) -> None:
        super().__init__(base_url, api_key, client)
        self.model = model

    def classify(self, text: str) -> SafetyResult:
        response = self._post("/safety/classify", {"model": self.model, "text": text}, timeout=20.0)
        try:
            data = response.json()
            return SafetyResult(
                label=str(data["label"]),
                score=float(data["score"]),
                provider=str(data.get("provider", "lan-chat")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError("安全分类响应格式无效") from exc

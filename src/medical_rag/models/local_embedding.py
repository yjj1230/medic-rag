"""本地 ONNX 向量化适配器：不依赖任何远端 embedding 服务的 ``EmbeddingModel`` 实现。

存在的理由
----------
检索链路里 ``RetrievalService`` 与 ``IndexWorker`` 都只依赖 ``EmbeddingModel``
协议（``embed(texts) -> list[list[float]]``），因此换一个实现即可换掉整个向量化
来源，不需要触碰检索、索引、切分任何一行代码。项目原本只有
``OpenAIEmbeddingAdapter``（走模型网关的 ``/embeddings``），而实测该网关**只提供
对话模型、不接受任何 embedding 请求**（``POST /v1/embeddings`` 对全部 24 个模型
一律返回 400）。结果是：解析、切分、质量门禁全部正常，但没有任何一条记录带向量，
混合检索实际退化为单路 BM25。

本模块用 ``fastembed`` + ``onnxruntime`` 在**进程内**跑中文向量模型，把向量化
从「外部依赖」变成「本地能力」：

* 不产生 PHI 出网（患者文本不再发往第三方），非开发环境的合规面反而更小；
* 无网络往返，批量入库不受网关吞吐与配额限制；
* 离线可用，冷启动不再被外部服务可用性阻塞。

边界与取舍（重要）
------------------
1. **归一化**：``bge-*-zh-v1.5`` 的输出已由模型自身做 L2 归一化（实测模长
   1.000000），因此这里**不再二次归一化**——重复归一化只会引入浮点漂移。
2. **查询与文档共用同一路径**：``EmbeddingModel`` 协议只有 ``embed(texts)``，
   检索侧对 query 与文档调用的是同一个方法，无法区分。bge 系列在 s2p（短查询
   检索长段落）场景下官方建议给**查询**加指令前缀，此处无法只给查询加，故统一
   不加前缀（对称编码）。这会让检索质量略低于「加前缀」的理想形态，但一致性
   优于半途改造协议；若后续要提升召回，应新增独立的 query 编码 seam，而不是在
   这里对所有文本统一加前缀（那会污染文档侧向量）。
3. **长度上限**：该模型 ``max_position_embeddings=512``，超长文本会被分词器
   静默截断。入库前应确认分块长度未显著超过该上限，否则尾部内容不参与向量表示。
4. **维度由模型决定**：维度从模型目录的 ``config.json`` 读取（``hidden_size``），
   而不是由调用方声明，避免「配置说 1536、模型出 512」这类只能在写入索引时
   才暴露的漂移。上层 ``EmbeddingPipeline`` 仍会校验，这里是提前失败。
5. **懒加载**：``fastembed`` 与 ONNX 会话都在**首次** ``embed`` 时才真正建立，
   构造对象本身只做文件校验与配置读取。组装根（lifespan / worker 启动）因此不会
   因为加载模型而变慢，也不会在只需要 BM25 的路径上被迫加载 ONNX。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from medical_rag.models.contracts import (
    ProviderConfigurationError,
    ProviderResponseError,
)

logger = logging.getLogger(__name__)

#: 默认模型。与 ``models/bge-small-zh-v1.5`` 目录下的权重对应。
DEFAULT_LOCAL_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

#: 缺少 ``config.json`` 时的兜底维度。仅用于模型目录不完整（例如只放了 ONNX
#: 权重）的情况；正常下载的模型目录一定带 ``config.json``，走的是真实读取。
_FALLBACK_DIMENSIONS = 512


class LocalFastEmbedModel:
    """``EmbeddingModel`` 协议的本地 ONNX 实现（线程安全、懒加载）。

    用法::

        model = LocalFastEmbedModel(r"models/bge-small-zh-v1.5")
        vectors = model.embed(["高血压患者应低盐饮食"])

    与 ``OpenAIEmbeddingAdapter`` 的契约完全一致：输入若干文本，返回**等长**的
    向量列表，顺序与输入一一对应，元素为 Python ``float``（不是 numpy 标量——
    ``IndexRecord.to_dict()`` 会把向量原样交给 OpenSearch，numpy 类型无法被
    JSON 序列化）。
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        model_name: str = DEFAULT_LOCAL_MODEL_NAME,
        threads: int | None = None,
        cache_dir: str | None = None,
    ) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ProviderConfigurationError("本地向量模型名称不能为空")
        if threads is not None and threads < 1:
            raise ProviderConfigurationError("本地向量模型线程数必须为正整数")

        self.model_name = model_name.strip()
        self.threads = threads
        self.cache_dir = cache_dir

        # 目录与配置在构造期就校验：一个路径写错应当立刻失败并说清楚缺什么，
        # 而不是等到第一次入库时才以 ONNXRuntimeError 的形式出现。
        if model_path is None or not str(model_path).strip():
            raise ProviderConfigurationError(
                "未配置本地向量模型目录：请设置 LOCAL_EMBEDDING_MODEL_PATH"
            )
        resolved = Path(str(model_path)).expanduser()
        if not resolved.exists():
            raise ProviderConfigurationError(
                f"本地向量模型目录不存在: {resolved}。"
                "请先下载模型权重（例如 huggingface_hub.snapshot_download 取 "
                f"{self.model_name} 的 config.json / tokenizer.json / vocab.txt / "
                "model_optimized.onnx），或用 LOCAL_EMBEDDING_MODEL_PATH 指向实际目录"
            )
        if not resolved.is_dir():
            raise ProviderConfigurationError(f"本地向量模型路径不是目录: {resolved}")
        self.model_path = resolved
        self._dimensions = self._read_dimensions(resolved)

        # 懒加载状态。``_lock`` 同时保护「只加载一次」与 fastembed 会话的并发
        # 首次初始化：ONNX 会话创建不是幂等的，多线程同时进入会重复加载
        # （数十 MB 权重的重复读盘与构图）。
        self._model: Any | None = None
        self._lock = threading.Lock()

    # -- 元信息 ---------------------------------------------------------------

    @property
    def dimensions(self) -> int:
        """向量维度，来自模型自身的 ``config.json``（``hidden_size``）。"""
        return self._dimensions

    @property
    def model_version(self) -> str:
        """写入索引的向量化身份标识。

        与 ``OpenAIEmbeddingAdapter.model`` 的语义一致（都是「哪个模型产出的
        向量」），便于对账时识别维度/模型漂移。
        """
        return self.model_name

    @staticmethod
    def _read_dimensions(model_path: Path) -> int:
        """从模型目录读取真实输出维度。

        读 ``config.json`` 而不是接受调用方声明，是为了让维度只有一个来源。
        ``EmbeddingPipeline`` 会在写入前校验维度，但那时错误信息只会说
        「dimension mismatch」，定位成本高；这里提前读到真实值，组装根就能直接
        比对并给出「把 EMBEDDING_DIMENSIONS 改成 512」这样的可执行提示。
        """
        config_path = model_path / "config.json"
        if not config_path.is_file():
            logger.warning(
                "本地向量模型目录缺少 config.json（%s），维度回退为 %d",
                model_path,
                _FALLBACK_DIMENSIONS,
            )
            return _FALLBACK_DIMENSIONS
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderConfigurationError(
                f"本地向量模型配置无法解析: {config_path}: {exc}"
            ) from exc
        hidden_size = payload.get("hidden_size")
        if not isinstance(hidden_size, int) or hidden_size < 1:
            logger.warning(
                "本地向量模型 config.json 未声明可用的 hidden_size（%r），维度回退为 %d",
                hidden_size,
                _FALLBACK_DIMENSIONS,
            )
            return _FALLBACK_DIMENSIONS
        return hidden_size

    # -- 加载 -----------------------------------------------------------------

    def _load(self) -> Any:
        """返回已就绪的 fastembed 实例；首次调用时加载，之后复用。

        加载失败一律转成 ``ProviderConfigurationError``（``ModelError`` 的子类）：
        这类问题（缺依赖、权重文件不全、ONNX 会话建不起来）都是**配置/环境**
        问题，需要运维动作，而不是可重试的瞬时错误。``EmbeddingPipeline`` 的重试
        逻辑依赖异常类型来区分，抛裸 ``Exception`` 会让它对配置错误做无意义重试。
        """
        model = self._model
        if model is not None:
            return model
        with self._lock:
            # 双重检查：并发首调时只加载一次。
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding  # type: ignore[import-not-found]
            except ImportError as exc:
                raise ProviderConfigurationError(
                    "fastembed 未安装，无法使用本地向量模型："
                    "请重新安装项目依赖（fastembed 与 onnxruntime 已在 "
                    "[project].dependencies 中声明）"
                ) from exc
            kwargs: dict[str, Any] = {
                "model_name": self.model_name,
                # ``specific_model_path`` 让 fastembed 直接使用本地权重目录，
                # 不再尝试联网解析或下载快照。
                "specific_model_path": str(self.model_path),
            }
            if self.threads is not None:
                kwargs["threads"] = self.threads
            if self.cache_dir is not None:
                kwargs["cache_dir"] = self.cache_dir
            try:
                self._model = TextEmbedding(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 任何加载失败都是配置问题
                raise ProviderConfigurationError(
                    f"本地向量模型加载失败（{self.model_name} @ {self.model_path}）："
                    f"{type(exc).__name__}: {exc}。"
                    "常见原因：权重文件不完整（缺 model_optimized.onnx / tokenizer.json）"
                    "或 onnxruntime 不可用"
                ) from exc
            logger.info(
                "本地向量模型已加载: %s dims=%d path=%s",
                self.model_name,
                self._dimensions,
                self.model_path,
            )
            return self._model

    # -- EmbeddingModel 协议 --------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把文本编码为向量，顺序与输入严格一致。

        空输入返回空列表（与 ``OpenAIEmbeddingAdapter`` 一致）；空字符串仍然返回
        一个向量（模型对空串会给出一个归一化后的定值向量），因为上层
        ``EmbeddingPipeline`` 强制要求「返回条数 == 输入条数」，在这里静默丢弃会
        让索引记录与向量错位。
        """
        if not texts:
            return []
        model = self._load()
        try:
            rows = list(model.embed(list(texts)))
        except Exception as exc:  # noqa: BLE001 - 推理失败统一包装为模型错误
            raise ProviderResponseError(
                f"本地向量推理失败: {type(exc).__name__}: {exc}"
            ) from exc
        if len(rows) != len(texts):
            raise ProviderResponseError(
                f"本地向量条数不匹配: 输入 {len(texts)} 条，返回 {len(rows)} 条"
            )
        vectors: list[list[float]] = []
        for row in rows:
            # 显式转成 Python float：numpy float32 无法被 OpenSearch 写入路径
            # 的 JSON 序列化接受，而适配器是唯一知道底层是 numpy 的一层。
            vector = [float(value) for value in row]
            if len(vector) != self._dimensions:
                raise ProviderResponseError(
                    f"本地向量维度不匹配: 期望 {self._dimensions}，实际 {len(vector)}"
                )
            vectors.append(vector)
        return vectors

"""面向逐页 OCR / 整文档 OCR 接缝的真实远端 PaddleOCR-VL 后端。

``PerPageOCRBackend`` / ``OCRParser`` 协议（见 ``per_page_ocr.py`` 与
``adapters.py``）已经存在，并已针对确定性 fixture 传输做过充分测试。本模块在不改变
那些调用方的前提下，在其之上新增一个**真实** HTTP 后端：图像字节（或栅格化后的 PDF
页）被 POST 到远端 PaddleOCR-VL 服务，返回的 VLM 转录被转换成本仓库的 OCR 块约定。

已实测的线上 schema（探测 http://192.168.254.35:18080，两个均 HTTP 200）：
``/ocr/file`` 是 multipart（表单字段 ``file``、filename、``image/*``），返回
``{"result": {"result": "<换行分隔的转录>"}}``。文本是整面板转录：
``json["result"]["result"]`` 是一个换行分隔的字符串，而非框/坐标。``.split("\\n")``
产出 OCR 文本行。``/ocr/base64`` 是等价的 JSON 形式（用 ``bytes`` 替代 ``filename``）；
本模块以 ``/ocr/file`` 作为唯一的首要 HTTP 路径。

一切都是 **fail-closed**：schema 错误、非 200、超时或栅格化失败都会抛出带类型的
:class:`RemoteOcrError` 子类 —— 绝不静默返回 ``[]``。pypdfium2 在
:class:`PdfPageRenderer` 内部惰性导入，因此除非真的渲染了 PDF，本包不硬性依赖
栅格化器。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO

import httpx

from medical_rag.parsing.adapters import PARSER_VERSION_OCR, detect_language
from medical_rag.parsing.contracts import ParsedDocument, ParsedSegment, as_bytes_io
from medical_rag.parsing.filetypes import IMAGE_FILE_TYPES

# 有意复用（而非另写一份）``_ocr_blocks_for_page``，这样远端
# 后端无法偏离既有的逐页 OCR 块约定。
from medical_rag.parsing.per_page_ocr import _ocr_blocks_for_page

# 以约 150 DPI 渲染（PDF 点为 72/英寸 → 缩放 150/72 ≈ 2.08）。
_PDF_RENDER_SCALE = 150 / 72
_OCR_FILE_PATH = "/ocr/file"
_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_WEBP = "image/webp"
# 对任何回显进错误消息的报文做截断 + 激进缩减。数字
# 字符被剥离，因为化验值转录属于 PHI；保留文本信号
# 以便在不泄漏患者数值结果的前提下诊断。
_SNIPPET_LIMIT = 120


def _sniff_mime(image_bytes: bytes) -> str:
    """从魔数（PNG/JPEG/WebP）推导真实的 ``image/*`` 内容类型。

    整文档图像以请求上传的字节形式到来，``IMAGE_FILE_TYPES`` 允许其为
    PNG/JPEG/WebP。来自渲染器的逐页字节本就是真实 PNG。魔数无法识别时回落到
    ``image/png``，使 multipart 标签保持为合法的 ``image/*`` 值。
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return _MIME_PNG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return _MIME_JPEG
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return _MIME_WEBP
    return _MIME_PNG


def _redact_snippet(text: object) -> str:
    """PHI 安全的错误预览：剥离数字、压缩空白、截断。

    有意保守：VLM OCR 报文即 OCR 转录（患者结果），因此任何回显的报文都会把
    换行分隔的多行压成一行并截断，并剥离数值化验值。
    """
    collapsed = " ".join(str(text).split())
    safe = "".join(
        ch
        for ch in collapsed
        if ch.isprintable() and not ch.isdigit() and (ch.isalpha() or ch in " ,.:;()%-/")
    )
    if len(safe) > _SNIPPET_LIMIT:
        safe = safe[: _SNIPPET_LIMIT - 1] + "…"
    return safe or "<empty>"


class RemoteOcrError(RuntimeError):
    """远端 OCR 路径的基础带类型失败（schema、HTTP、栅格化）。"""


class RemoteOcrSchemaError(RemoteOcrError):
    """服务返回 200，但报文与已知 schema 不匹配。"""


class RemoteOcrHttpError(RemoteOcrError):
    """服务返回非 200 状态或传输层失败。"""


class RemoteOcrRasterizationError(RemoteOcrError):
    """某个 PDF 页无法栅格化为 PNG（fail-closed，不降级）。"""


def _secret_value(secret: object) -> str:
    if hasattr(secret, "get_secret_value"):
        return str(secret.get_secret_value())  # type: ignore[no-any-return]
    return str(secret) if secret is not None else ""


@dataclass(frozen=True, slots=True)
class _RemoteOcrHttpClient:
    """``/ocr/file`` multipart 上传的小型内部 HTTP 辅助工具。

    ``client_factory`` 让测试注入假的 ``httpx.Client``；为 ``None`` 时**每请求**
    打开（并关闭）一个普通 ``httpx.Client``，因此 Basic 认证按请求传递，并可通过
    注入的客户端观测到。

    取舍（已接受，不要过度设计）：每页一个新客户端意味着跨同一 PDF 的多页没有
    连接/保活复用。对医疗 OCR 后端而言，这使每次上传自包含、认证按请求进行，代价是
    每页握手更重；逐 PDF 后端已经摊销了渲染，因此此处有意不做池化。
    """

    base_url: str = "http://localhost"
    timeout: float = 90.0
    username: str | None = None
    # ``password`` 可能是 SecretStr（来自 Settings）或普通 str；通过
    # ``_secret_value`` 解析。``object | None`` 放宽了标注，使
    # pydantic 的 SecretStr 能被 build_remote_ocr 传入而不说谎类型。
    password: object | None = None
    client_factory: Callable[[], httpx.Client] | None = None

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}{_OCR_FILE_PATH}"

    def _auth(self) -> httpx.BasicAuth | None:
        # Basic 认证只在用户名和非空密码**两者**
        # 都具备时才有意义；只有用户名时不得发送 BasicAuth(user, "")。
        secret = _secret_value(self.password)
        if not self.username or not secret:
            return None
        return httpx.BasicAuth(self.username, secret)

    def _new_client(self) -> httpx.Client:
        if self.client_factory is not None:
            return self.client_factory()
        return httpx.Client()

    def post_ocr(
        self, image_bytes: bytes, filename: str, content_type: str = _MIME_PNG
    ) -> list[str]:
        """把 ``image_bytes`` 上传到 ``/ocr/file`` 并返回纯 OCR 文本行。"""
        client = self._new_client()
        auth = self._auth()
        try:
            try:
                response = client.post(
                    self._url(),
                    files={"file": (filename, image_bytes, content_type)},
                    timeout=self.timeout,
                    auth=auth,
                )
            except httpx.HTTPError as exc:
                raise RemoteOcrHttpError(f"OCR 服务请求失败: {exc}") from exc
            if response.status_code != 200:
                # 非 200 的报文可能是错误文档或转录；只回显
                # 一份经 PHI 脱敏的预览，绝不回显原始患者文本。
                raise RemoteOcrHttpError(
                    f"OCR 服务返回非 200 状态: {response.status_code} body={_redact_snippet(response.text)!r}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise RemoteOcrSchemaError(
                    f"OCR 服务返回非 JSON 正文: {_redact_snippet(response.text)!r}"
                ) from exc
            if not isinstance(data, dict):
                raise RemoteOcrSchemaError(
                    f"OCR 服务响应不是对象: {_redact_snippet(response.text)!r}"
                )
            result = data.get("result")
            if not isinstance(result, dict):
                # 只回显安全顶层字段的*名称*，绝不回显取值。
                raise RemoteOcrSchemaError(
                    f"OCR 服务响应缺少 result 对象; 顶层字段={sorted(str(k) for k in data)}"
                )
            transcript = result.get("result")
            if not isinstance(transcript, str):
                # 只回显 ``result`` 内部安全的字段名。
                raise RemoteOcrSchemaError(
                    f"OCR 服务响应 result.result 不是字符串; result 字段="
                    f"{sorted(str(k) for k in result)}"
                )
        finally:
            client.close()
        return [line.strip() for line in transcript.split("\n") if line.strip()]


class PdfPageRenderer:
    """把 PDF 的字节栅格化为逐页 PNG 字节（惰性导入 pypdfium2）。

    构造函数接收 PDF ``bytes``。``render_page`` 从 1 开始计数并返回 PNG 字节。
    任何栅格化失败都会抛出带类型的 :class:`RemoteOcrRasterizationError` ——
    有意 fail-closed，使有缺陷的页永远不会被静默丢弃。
    """

    def __init__(self, pdf_bytes: bytes) -> None:
        self._pdf_bytes = pdf_bytes
        self._pdf = None
        self._page_count: int | None = None

    def _ensure_loaded(self) -> None:
        if self._pdf is not None:
            return
        try:
            from pypdfium2 import PdfDocument
        except ImportError as exc:  # pragma: no cover - 仅在依赖缺失时
            raise RemoteOcrRasterizationError(
                "缺少 pypdfium2（PDF→PNG 渲染器）；无法执行逐页远程 OCR。"
            ) from exc
        try:
            self._pdf = PdfDocument(self._pdf_bytes)
            self._page_count = len(self._pdf)
        except Exception as exc:
            raise RemoteOcrRasterizationError(f"无法打开 PDF 用于渲染: {exc}") from exc

    @property
    def page_count(self) -> int:
        self._ensure_loaded()
        if self._page_count is None:  # pragma: no cover - 否则 _ensure_loaded 会抛错
            raise RemoteOcrRasterizationError("PDF 页数未知：文档尚未成功加载")
        return self._page_count

    def render_page(self, page_index_1based: int) -> bytes:
        self._ensure_loaded()
        if self._pdf is None or self._page_count is None:  # pragma: no cover
            raise RemoteOcrRasterizationError("PDF 尚未加载，无法渲染页面")
        if not (1 <= page_index_1based <= self._page_count):
            raise RemoteOcrRasterizationError(
                f"页索引超出范围: {page_index_1based} (页数 {self._page_count})"
            )
        try:
            page = self._pdf[page_index_1based - 1]
            bitmap = page.render(scale=_PDF_RENDER_SCALE)
            image = bitmap.to_pil()
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            raise RemoteOcrRasterizationError(
                f"PDF 第 {page_index_1based} 页渲染失败: {exc}"
            ) from exc


class RemotePerPageOCRBackend:
    """由远端服务支撑的 :class:`PerPageOCRBackend`，一次一页。

    ``renderer`` 绑定到*一份* PDF，因此本实例是逐文档的。页标识被保留：
    逐页 OCR 解析器自行从页索引盖上 ``page_number`` / ``p{page}``。
    """

    def __init__(
        self,
        renderer: PdfPageRenderer,
        *,
        http: _RemoteOcrHttpClient | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.renderer = renderer
        self.http = http or _RemoteOcrHttpClient(client_factory=client_factory)

    @property
    def page_count(self) -> int:
        return self.renderer.page_count

    def recognize_page(self, page_index: int) -> list[str]:
        if not (1 <= page_index <= self.renderer.page_count):
            raise RemoteOcrError(
                f"页索引超出范围: {page_index} (页数 {self.renderer.page_count})"
            )
        png = self.renderer.render_page(page_index)
        # 渲染器输出总是 PNG；此处显式默认值是正确做法。
        return self.http.post_ocr(png, f"page-{page_index:04d}.png")


class RemoteOCRParser:
    """由远端服务支撑的整文档（图像）:class:`OCRParser`。

    图像字节来自请求对象，因此该适配器是单例。它通过共享的页块构建器产出与
    :class:`PerPageOCRParser` 相同约定的 :class:`ParsedDocument`
    （``file_type="image"``、``ocr-parser-v2``，第 1 页每行一个 segment + 一个块，
    锚点为 ``p1``）。
    """

    supported_file_types = IMAGE_FILE_TYPES

    def __init__(
        self,
        http: _RemoteOcrHttpClient | None = None,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self.http = http or _RemoteOcrHttpClient(client_factory=client_factory)

    def parse_images(self, file: BinaryIO, language_hints: list[str]) -> ParsedDocument:
        image_bytes = as_bytes_io(file).read()
        filename = getattr(file, "name", None) or "image.png"
        # 整文档图像是上传的字节（可能是 PNG/JPEG/WebP），因此给
        # multipart 标上真实内容类型，而不是假定为 PNG。
        lines = self.http.post_ocr(image_bytes, filename, content_type=_sniff_mime(image_bytes))

        blocks = _ocr_blocks_for_page(1, lines, language_hints)
        segments = [
            ParsedSegment(
                text=block.raw_text,
                page_number=1,
                source_anchor=block.source_anchor,
                language=block.language,
                confidence=block.confidence,
            )
            for block in blocks
        ]
        raw_text = "\n".join(block.raw_text for block in blocks)
        language = detect_language(raw_text)
        if language == "und" and language_hints:
            language = language_hints[0]
        return ParsedDocument(
            file_type="image",
            raw_text=raw_text,
            segments=tuple(segments),
            language=language,
            parser_version=PARSER_VERSION_OCR,
            blocks=tuple(blocks),
        )


RemotePerPageBackendFactory = Callable[[bytes], RemotePerPageOCRBackend]


def build_remote_ocr(
    settings: object,
) -> tuple[RemoteOCRParser | None, RemotePerPageBackendFactory | None]:
    """从 settings 构建远端 OCR 适配器。

    ``ocr_service_url`` 为空时返回 ``(None, None)``（默认 → 远端后端关闭）。
    否则返回（一个单例 :class:`RemoteOCRParser`，一个逐 PDF 后端*工厂* ——
    调用 ``factory(pdf_bytes)`` 得到一个绑定到该 PDF 的
    :class:`RemotePerPageOCRBackend`）。
    """
    url = getattr(settings, "ocr_service_url", "") or ""
    if not str(url).strip():
        return None, None
    timeout = float(getattr(settings, "ocr_timeout_seconds", 90.0) or 90.0)
    http = _RemoteOcrHttpClient(
        base_url=str(url),
        timeout=timeout,
        username=getattr(settings, "ocr_username", None),
        password=getattr(settings, "ocr_password", None),
    )
    parser = RemoteOCRParser(http=http)

    def backend_factory(data: bytes) -> RemotePerPageOCRBackend:
        return RemotePerPageOCRBackend(PdfPageRenderer(data), http=http)

    factory: RemotePerPageBackendFactory = backend_factory
    return parser, factory


__all__ = [
    "PdfPageRenderer",
    "RemoteOCRParser",
    "RemoteOcrError",
    "RemoteOcrHttpError",
    "RemoteOcrRasterizationError",
    "RemoteOcrSchemaError",
    "RemotePerPageBackendFactory",
    "RemotePerPageOCRBackend",
    "build_remote_ocr",
]

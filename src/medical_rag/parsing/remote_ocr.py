"""Real remote PaddleOCR-VL backend for the per-page OCR / whole-doc OCR seams.

The ``PerPageOCRBackend`` / ``OCRParser`` protocols (see ``per_page_ocr.py`` and
``adapters.py``) already exist and are well-tested against a deterministic
fixture transport.  This module adds a **real** HTTP backend on top without
changing those callers: image bytes (or rasterized PDF pages) are POSTed to a
remote PaddleOCR-VL service and the returned VLM transcript is turned into the
repository's OCR block convention.

Verified live schema (probe at http://192.168.254.35:18080, both HTTP 200):
``/ocr/file`` is multipart (form field ``file``, filename, ``image/*``) and
returns ``{"result": {"result": "<newline-delimited transcript>"}}``.  The text
is a full-panel transcript: ``json["result"]["result"]`` is ONE newline-delimited
string, not boxes/coordinates.  ``.split("\\n")`` yields the OCR text lines.
``/ocr/base64`` is the JSON equivalent (adds ``bytes`` instead of ``filename``);
this module uses ``/ocr/file`` as the single primary HTTP path.

Everything is **fail-closed**: a wrong schema, non-200, timeout, or rasterizer
failure raises a typed :class:`RemoteOcrError` subclass — never a silent ``[]``.
pypdfium2 is imported lazily inside :class:`PdfPageRenderer` so this package does
not hard-require the rasterizer unless a PDF is actually rendered.
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

# ``_ocr_blocks_for_page`` is deliberately reused (not mirrored) so the remote
# backend cannot drift from the established per-page OCR block convention.
from medical_rag.parsing.per_page_ocr import _ocr_blocks_for_page

# Render at ~150 DPI (PDF points are 72/inch → scale 150/72 ≈ 2.08).
_PDF_RENDER_SCALE = 150 / 72
_OCR_FILE_PATH = "/ocr/file"
_MIME_PNG = "image/png"
_MIME_JPEG = "image/jpeg"
_MIME_WEBP = "image/webp"
# Cap + aggressively shrink any body echoed into an error message.  Number
# characters are stripped because lab-value transcripts are PHI; textual signal
# is kept to diagnose without leaking patient numeric results.
_SNIPPET_LIMIT = 120


def _sniff_mime(image_bytes: bytes) -> str:
    """Derive a real ``image/*`` content-type from magic bytes (PNG/JPEG/WebP).

    Whole-doc images come from the request as the uploaded bytes, which
    ``IMAGE_FILE_TYPES`` allows to be PNG/JPEG/WebP.  Renderer-sourced per-page
    bytes are already genuine PNG.  Falls back to ``image/png`` when the magic is
    not recognised so the multipart label stays a valid ``image/*`` value.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return _MIME_PNG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return _MIME_JPEG
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return _MIME_WEBP
    return _MIME_PNG


def _redact_snippet(text: object) -> str:
    """PHI-safe error preview: digits dropped, whitespace collapsed, truncated.

    Deliberately conservative: a VLM OCR body is the OCR transcript (patient
    results), so any echoed body collapses newline-delimited lines to a single
    truncated line and strips numeric lab values.
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
    """Base typed failure for the remote OCR path (schemas, HTTP, raster)."""


class RemoteOcrSchemaError(RemoteOcrError):
    """The service returned 200 but the body did not match the known schema."""


class RemoteOcrHttpError(RemoteOcrError):
    """The service returned a non-200 status or a transport-level failure."""


class RemoteOcrRasterizationError(RemoteOcrError):
    """A PDF page could not be rasterized to PNG (fail-closed, no degradation)."""


def _secret_value(secret: object) -> str:
    if hasattr(secret, "get_secret_value"):
        return str(secret.get_secret_value())  # type: ignore[no-any-return]
    return str(secret) if secret is not None else ""


@dataclass(frozen=True, slots=True)
class _RemoteOcrHttpClient:
    """Small internal HTTP helper for the ``/ocr/file`` multipart upload.

    ``client_factory`` lets tests inject a fake ``httpx.Client``; when ``None`` a
    plain ``httpx.Client`` is opened **per request** (and closed) so Basic auth
    is passed per-request and stays observable through injected clients.

    Tradeoff (accepted, do not over-engineer): a fresh client per page means no
    connection/keep-alive pooling across pages of one PDF.  For a medical OCR
    backend this keeps each upload self-contained and authentication per-request,
    at the cost of a heavier per-page handshake; the per-PDF backend already
    amortises rendering, so this is intentionally not pooled.
    """

    base_url: str = "http://localhost"
    timeout: float = 90.0
    username: str | None = None
    # ``password`` may be a SecretStr (from Settings) or a plain str; resolved
    # via ``_secret_value``.  ``object | None`` widens the annotation so a
    # pydantic SecretStr can be passed by build_remote_ocr without a type lie.
    password: object | None = None
    client_factory: Callable[[], httpx.Client] | None = None

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}{_OCR_FILE_PATH}"

    def _auth(self) -> httpx.BasicAuth | None:
        # Basic auth is only meaningful when BOTH username and a non-empty
        # password are present; a bare username must not send BasicAuth(user, "").
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
        """Upload ``image_bytes`` to ``/ocr/file`` and return plain OCR text lines."""
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
                # Non-200 body may be an error document or a transcript; echo only
                # a PHI-redacted preview, never raw patient text.
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
                # Echo only the safe top-level field NAMES, never values.
                raise RemoteOcrSchemaError(
                    f"OCR 服务响应缺少 result 对象; 顶层字段={sorted(str(k) for k in data)}"
                )
            transcript = result.get("result")
            if not isinstance(transcript, str):
                # Echo only safe field names inside ``result``.
                raise RemoteOcrSchemaError(
                    f"OCR 服务响应 result.result 不是字符串; result 字段="
                    f"{sorted(str(k) for k in result)}"
                )
        finally:
            client.close()
        return [line.strip() for line in transcript.split("\n") if line.strip()]


class PdfPageRenderer:
    """Rasterize a PDF's bytes to per-page PNG bytes (lazy pypdfium2 import).

    Constructor takes the PDF ``bytes``.  ``render_page`` is 1-based and returns
    PNG bytes.  On any rasterizer failure a typed
    :class:`RemoteOcrRasterizationError` is raised — deliberately fail-closed so
    deficient pages are never silently dropped.
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
        except ImportError as exc:  # pragma: no cover - only when dependency missing
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
        if self._page_count is None:  # pragma: no cover - _ensure_loaded raises otherwise
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
    """:class:`PerPageOCRBackend` backed by the remote service, one page at a time.

    ``renderer`` is tied to ONE PDF, so this instance is per-document.  Page
    identity is preserved: the per-page OCR parser stamps ``page_number`` /
    ``p{page}`` from the page index by itself.
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
        # Renderer output is always PNG; the explicit default is correct here.
        return self.http.post_ocr(png, f"page-{page_index:04d}.png")


class RemoteOCRParser:
    """Whole-document (image) :class:`OCRParser` backed by the remote service.

    Image bytes come from the request object, so this adapter is a singleton.
    It produces a :class:`ParsedDocument` in the same convention as
    :class:`PerPageOCRParser` (``file_type="image"``, ``ocr-parser-v2``, one
    segment + one block per line on page 1 with ``p1`` anchors) via the shared
    page-block builder.
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
        # Whole-doc images are the uploaded bytes (may be PNG/JPEG/WebP), so label
        # the multipart with the real content-type rather than assuming PNG.
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
    """Build remote OCR adapters from settings.

    Returns ``(None, None)`` when ``ocr_service_url`` is empty (the default →
    remote backend disabled).  Otherwise returns (a singleton
    :class:`RemoteOCRParser`, a per-PDF backend *factory* — call
    ``factory(pdf_bytes)`` to get a :class:`RemotePerPageOCRBackend` bound to one
    PDF).
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

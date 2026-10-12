from dataclasses import dataclass
from pathlib import PurePath

from medical_rag.core.errors import MedicalRagError


MAX_UPLOAD_SIZE_BYTES = 50 * 1024 * 1024

SUPPORTED_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".webp": frozenset({"image/webp"}),
    ".doc": frozenset({"application/msword"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".xls": frozenset({"application/vnd.ms-excel"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
}


class FilePolicyError(MedicalRagError):
    pass


class UnsupportedFileType(FilePolicyError):
    pass


class VirusDetected(FilePolicyError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    filename: str
    extension: str
    content_type: str
    size_bytes: int


def _matches_magic(extension: str, header: bytes) -> bool:
    if not header:
        return True
    if extension == ".pdf":
        return header.startswith(b"%PDF-")
    if extension == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if extension in {".docx", ".xlsx"}:
        return header.startswith(b"PK\x03\x04")
    if extension in {".doc", ".xls"}:
        return header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    return False


def validate_upload(
    filename: str,
    content_type: str,
    size_bytes: int,
    header: bytes = b"",
) -> ValidatedFile:
    safe_filename = PurePath(filename).name
    if not filename or safe_filename != filename or "\x00" in filename:
        raise UnsupportedFileType("文件名不合法")
    extension = PurePath(safe_filename).suffix.lower()
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if extension not in SUPPORTED_TYPES or normalized_content_type not in SUPPORTED_TYPES[extension]:
        raise UnsupportedFileType("不支持的文件类型或 MIME 类型不匹配")
    if size_bytes <= 0 or size_bytes > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("文件大小必须大于 0 且不超过 50 MiB")
    if not _matches_magic(extension, header):
        raise UnsupportedFileType("文件内容与扩展名不匹配")
    return ValidatedFile(
        filename=safe_filename,
        extension=extension,
        content_type=normalized_content_type,
        size_bytes=size_bytes,
    )


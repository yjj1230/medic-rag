"""parsing 链路共享的文件类型词汇（单一来源；原先在 service/adapters/cleaner 各写一份）。

canonical 集合与上传校验支持的图像格式保持一致：扩展名 + MIME 类型两种形态。
"""

from __future__ import annotations

IMAGE_FILE_TYPES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", "image/png", "image/jpeg", "image/webp"}
)

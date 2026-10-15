"""文档质量门禁：策略阈值、度量与判定。

Task 7 新增读取时的错误码映射和派生文档质量信号，作为独立模块
（``codes`` / ``documents``）由 API 层直接导入。
"""

from medical_rag.domain.enums import QualityStatus
from medical_rag.quality.contracts import QualityDecision

__all__ = ["QualityDecision", "QualityStatus"]

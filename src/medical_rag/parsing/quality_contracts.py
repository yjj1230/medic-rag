"""质量门禁契约的向后兼容再导出。

规范定义现位于 :mod:`medical_rag.quality`；保留本模块是为了让既有从
``parsing.quality_contracts`` 的导入继续可用。
"""

from medical_rag.quality.contracts import QualityDecision

__all__ = ["QualityDecision"]

"""闸 2b（闲聊输出二次筛检）承重事实探针。

写 `chat/casual_screen.py` 之前必须先核实两件事，否则筛检器会落成一个
自相矛盾或可被绕过的实现：

1. **拒答话术自相矛盾？**  spec §5.5 规定模型在识破医学问题时只回
   「这个问题涉及医疗健康，请重新描述为医学相关问题…」。但 spec §5.6 又要求
   用 ``has_medical_term`` 筛掉夹带医学词的输出——而"医疗""健康"就在词表里。
   若两者同时成立，**指定的安全拒答话术会把自己拦掉**。

2. **plan 里两条期望值是否为真？**  plan T4 断言
   ``"我建议您放心，指标完全正常。"`` 由 ``_high_risk_claims`` 拦下。
   该正则要求"你/您/患者"与"完全正常"之间**不含标点**，而待测句中间有逗号，
   是否真的命中需要实测，不能假设。

运行：hermes venv python 直接跑，输出重定向到同名 .txt。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

from medical_rag.chat.citations import CitationValidator  # noqa: E402
from medical_rag.query.casual import has_medical_term  # noqa: E402
from medical_rag.safety.service import SafetyAction, SafetyService  # noqa: E402

CASES = [
    "你的血压偏高，建议停药。",
    "我建议您放心，指标完全正常。",
    "这个问题涉及医疗健康，请重新描述为医学相关问题，我会依据医学资料回答。",
    "这个问题涉及医疗健康。你的血压偏高。",
    "参见 [e1c1] 的说明",
    "今天天气不错，多云转晴。",
    "你好，很高兴为你服务。",
]


def main() -> None:
    validator = CitationValidator()
    safety = SafetyService()
    print(f"{'case':<44} {'lex':<5} {'highrisk':<9} {'safety':<18} {'cite':<5}")
    print("-" * 90)
    for case in CASES:
        lex = has_medical_term(case)
        hr = bool(validator._high_risk_claims.search(case))
        action = safety.classify(case).action
        cite = bool(validator.citation_pattern.search(case))
        label = case if len(case) <= 42 else case[:41] + "…"
        print(f"{label:<44} {str(lex):<5} {str(hr):<9} {str(action):<18} {str(cite):<5}")
    print()
    print("SafetyAction.ALLOW_RETRIEVAL =", SafetyAction.ALLOW_RETRIEVAL)
    # 逐项拆解第二例，确认是哪个检查在起作用
    probe = "我建议您放心，指标完全正常。"
    print()
    print("拆解:", probe)
    for alt in validator._high_risk_claims.pattern.split("|"):
        import re as _re

        if _re.search(alt, probe, _re.IGNORECASE):
            print("  命中分支:", alt[:70])
    print("  含医学词:", has_medical_term(probe))


if __name__ == "__main__":
    main()

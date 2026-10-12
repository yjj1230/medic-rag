from __future__ import annotations

import re

from medical_rag.query.contracts import Intent

# 每条规则: (意图, 正则列表)
_REPORT = [
    re.compile(r"报告|结果|体检|复查|我的|上次查|这份|这张|这个结果"),
]
_EDUCATION = [
    re.compile(r"怎么|如何|注意|预防|饮食|运动|生活|应该吃什么"),
]
_LAB = [
    re.compile(r"什么(意思|意义)|正常吗|参考范围|正常范围|偏高|偏低|异常|升高|降低"),
]

# 语义优先级（值越大越强）。报告解释优先于所有，指标解释优先于科普。
PRIORITY: dict[Intent, int] = {
    Intent.REPORT_EXPLANATION: 30,
    Intent.LAB_INDICATOR: 20,
    Intent.HEALTH_EDUCATION: 10,
    Intent.ORDINARY: 0,
}


def score_intent(text: str) -> list[tuple[Intent, int]]:
    """返回按文本命中信号累加每条正则的 (意图, 命中数)。"""
    scores: dict[Intent, int] = {}
    low = text.casefold()
    for patterns, intent in (
        (_REPORT, Intent.REPORT_EXPLANATION),
        (_LAB, Intent.LAB_INDICATOR),
        (_EDUCATION, Intent.HEALTH_EDUCATION),
    ):
        count = sum(1 for pat in patterns if pat.search(low))
        if count:
            scores[intent] = count
    return sorted(
        ((intent, count) for intent, count in scores.items()),
        key=lambda item: (PRIORITY[item[0]], item[1]),
        reverse=True,
    )

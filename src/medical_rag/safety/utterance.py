"""中文医学语境识别小工具。

Safe-classification 与 Citation validation 都受同一个问题困扰：纯 substring /
正则匹配无法区分「字符串出现某个词」与「该词是否构成我们真正要拦的语义」。

本模块提供两个纯函数，分别供两个链路复用，避免各自再叠一套正则：

* :func:`is_negated_assertion` —— 判定一段（通常是**模型回答**）文本里的断言是否
  处于否定语境。用于豁免「不能仅凭此确诊为癌症」这类免责/转述句式，避免把安全
  的解释性回答误拦为诊断断言。

* :func:`is_general_knowledge_enquiry` —— 判定一段（通常是**用户提问**）是否属于
  一般知识问询（询问疾病知识、看科普、读文献），而非用户以第一人称描述自身症状。
  用于豁免「网上看到有人说胸痛怎么回事」这类问询被误判为急症升级。

两者语义与调用方天然正交（一个应对模型输出、一个应对用户输入），但共享同一套
「否定 / 转述」中文模式，故放在同一模块统一维护与测试。
"""
from __future__ import annotations

import re

# --- 否定性断言（模型回答场景）--------------------------------------------
# 当文本中出现这些否定/限定词 + 断言动词时，后面的医学断言是被否定/限定掉的，
# 不应命中「确诊为/是癌症/诊断为」这一类诊断断言正则。
#
# 词表必须覆盖三类否定形态，缺一类就会把合法的解释性回答误拦（防过头会直接
# 损失可用性）。补充的两类来自实测：``未确诊为`` 这类**断言动词自身带否定前缀**
# 的写法，此前因词表只认前缀否定词而漏判。
_NEGATION_PREFIX = re.compile(
    r"(?:不能|不可以|无法|不足以|不一定|不能仅凭|不能根据|不(?:能|该)"
    r"|不可|不应|难以|暂不能|防止误诊)"
)
# 断言动词：诊断/患有/疾病命名。与 gauged 在 citations.py 的 _high_risk_claims
# 保持同样的词面，保证「豁免的是原来会误拦的那些」。
# ``[未尚]`` 覆盖「未确诊为 / 尚未诊断为」：否定内嵌在动词里，前缀否定词匹配不到。
_ASSERT_VERB = re.compile(
    r"[未尚]?(?:确诊|诊断)为|(患有|患了|得了)|(?:是|为)(?:恶性)?癌|is\s+cancer"
    r"|diagnosed\s+with|you\s+have\s+cancer",
    re.IGNORECASE,
)
# 断言动词自身即否定（"未确诊为"）时无需前缀否定词配合。
_INTRINSIC_NEGATION_VERB = re.compile(r"[未尚](?:确诊|诊断)为")
# 否定词与断言词之间允许出现的中性修饰距离（最长 12 个字符），避免把「不能 X，
# 因此确诊为 Y」这种真实结论也归为否定。取一个够近的多能覆盖常见免责句式。
_NEG_TO_ASSERT_SLOP = 12


def is_negated_assertion(text: str) -> bool:
    """是否属于“否定语境下的医学断言” (免责/转述)。

    例：``"不能仅凭此确诊为癌症"`` -> True（该断言被否定，不应拦截）
       ``"未确诊为癌症"``         -> True（否定内嵌在断言动词里）
       ``"这是癌症"``             -> False（真实诊断断言，应拦截）
    """
    if _INTRINSIC_NEGATION_VERB.search(text):
        return True
    for neg in _NEGATION_PREFIX.finditer(text):
        tail = text[neg.end() : neg.end() + _NEG_TO_ASSERT_SLOP]
        if _ASSERT_VERB.search(tail):
            return True
    return False


# --- 一般知识问询（用户提问场景）--------------------------------------------
# 这些信号强烈提示提问者是在问「一般知识/传闻」而非描述自己的症状。
_KNOW_ENQUIRY_PATTERNS = (
    re.compile(r"网上(?:看到|查到|说|说的|有人|的)"),
    re.compile(r"(?:看到|查到|听说|搜索|查过|看的|看到过|刷到)(?:的|过)?"),
    re.compile(r"是怎么回事|是什么意思|是什么原因|为什么(?:会|能)"),
    re.compile(r"请问|想问|想了解|科普|能科普|解释一下"),
    re.compile(r"网上说|网上很多人|有人说|有人(?:说|讲)|听说过"),
    re.compile(r"正常(?:吗|不|还是)|会不会得|会得吗"),
    re.compile(r"(?:这不是|不算|不严重吧|没事吧|要紧吗)"),
)
# 第一人称症状描述信号：若出现，则即便带「网上/听说」也更可能牵涉自身，需谨慎。
_SELF_EXPERIENCE_PATTERNS = (
    re.compile(r"我(?:胸口|开始|今天|这几天|昨晚|一直|现在|又).{0,4}(?:胸痛|喘|呼吸困难|疼|痛)"),
    re.compile(r"我是?(?:不是)?(?:得了|有|据说有|怀疑)(?:癌症|心梗|中风)"),
    re.compile(r"我(?:现在|刚刚|正在|一直)(?:喘不上|无法呼吸|失去意识|抽搐|出血)"),
)


def is_general_knowledge_enquiry(text: str) -> bool:
    """是否属于一般知识问询（而非描述自身症状）。

    同时命中「知识问询信号」且**未命中**「第一人称症状描述」时返回 True，此时
    急症词表 / 诊断词表可以安全地不触发升级。

    例：
      ``"网上看到有人说胸痛怎么回事"`` -> True （一般知识，不升级）
      ``"我今天胸口疼喘不上气"``       -> False（疑似自身症状，需升级）
    """
    has_enquiry = any(pat.search(text) for pat in _KNOW_ENQUIRY_PATTERNS)
    if not has_enquiry:
        return False
    return not any(pat.search(text) for pat in _SELF_EXPERIENCE_PATTERNS)

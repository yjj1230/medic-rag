from __future__ import annotations

import re

from medical_rag.chat.contracts import DraftAnswer, ValidatedAnswer
from medical_rag.retrieval.contracts import Evidence
from medical_rag.safety.utterance import is_negated_assertion


class UnsupportedClaim(ValueError):
    pass


class CitationValidator:
    # 引用标记：[evidence-id]。orchestrator 与校验器共用同一模式，避免两处漂移。
    #
    # P1-7（评审 2026-10-12）：模式从「任意 [字母数字]」收紧到**证据 ID 形态**。
    # 证据 ID 只有两种真实形态（检索层 id 生成 + 评测集 gold_evidence_ids 证实）：
    #   1. UUID（生产检索记录，如 1f18737d-6471-4de4-a790-867fdf9034b8）；
    #   2. 字母+数字混合的短 token（测试夹具与文档示例，如 e1、e1c1、pub-12）。
    # 旧模式会把正文里任何方括号 token 当引用：Markdown 脚注 [1]、年份
    # [2023]、中文注释 [注1] 提取后不在证据集 → 整答被拒（"引用了不存在的
    # 证据"）。真实日志 3/8 degraded 中部分即此类假阳性。
    # 例外（白名单豁免）：纯数字（[1]/[2023]）、含 CJK（[注1]）、纯字母无
    # 数字（[note]）的 token 一律视为正文而非引用。全部内部用法（提取/
    # 段落覆盖/句末锚定/strip）共享本模式，语义保持一致。
    citation_pattern = re.compile(
        r"\[((?:"
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"  # UUID
        r"|(?=[A-Za-z0-9_:\-]*[A-Za-z])(?=[A-Za-z0-9_:\-]*\d)[A-Za-z0-9_:\-]{2,64}"  # 字母+数字 token
        r"))\]"
    )

    # 只拦"断言式诊断结论"（对患者下结论），不拦"癌症"这类疾病名词本身——
    # 健康教育回答中出现疾病名称是合法的。
    #
    # 第二段是绝对化安慰/排除结论（"你的血红蛋白完全正常"）：对患者给出
    # 正常/没问题的定论同样是诊断级结论，本系统只能陈述证据中的数值并建议
    # 咨询医生。仅限定第二人称 + 绝对化措辞，避免误伤"结合医生判断"这类
    # 正常表述。
    _high_risk_claims = re.compile(
        r"(?:你|您|患者)(?:的)?(?:病情|情况|报告|结果|指标)?[^\n，。；,.;]{0,8}"
        r"(?:确诊|诊断为|患有|患了|得了)"
        r"|(?:确诊|诊断)为"
        r"|(?:这|该|此)(?:是|为)(?:恶性)?癌"
        r"|(?:你|您|患者)(?:的)?[^\n，。；,.;]{0,10}"
        r"(?:完全正常|一切正常|完全没问题|没有问题|不用(?:担心|害怕)|不必担心"
        r"|可以放心|已(?:经)?排除|可以排除|确定没事)"
        r"|is\s+cancer|diagnosed\s+with|you\s+have\s+cancer",
        re.IGNORECASE,
    )

    # 长度超过该阈值的段落必须带引用标记；过短的过渡句/免责句不强制。
    _MIN_PARAGRAPH_CHARS_REQUIRING_CITATION = 30

    # --- 句子级校验 --------------------------------------------------------
    # 系统提示词要求「每个医学结论必须在句末使用证据 ID」。段落级检查（任意
    # 位置出现一个标记即算通过）无法执行该约定：整段里一个 [e1] 就能让任意
    # 数量的幻觉断言蒙混过关。
    _SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n")
    # 句尾允许出现的收尾符号/Markdown 记号（** 加粗等），不含 ] —— 引用的
    # 右括号本身是锚点的一部分。
    _TRAILING_NOISE = "。！？!?；;，,、）)】》\"'*_…~· \t\u3000"
    # 免责/建议/转述句不需要引用：它们不构成证据性医学结论。
    _DISCLAIMER_SENTENCE = re.compile(
        r"咨询|就诊|就医|看医生|遵医嘱|医生判断|专业医生|临床医生|医生意见|医生指导"
        r"|结合[^\n。；]{0,12}医生|仅供参考|不能替代|无法替代|请以医生|需结合临床"
        r"|建议复查|定期复查|及时(?:到|去)?医院|线下就诊"
    )

    # --- 数值/单位一致性 ---------------------------------------------------
    # 数值权重/单位：105、"105.0"、"1,234" 均视为数值。
    _NUMBER_WITH_UNIT = re.compile(
        r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[A-Za-zµμ%][A-Za-z0-9µμ/%·^*]*)"
    )
    # 相对容差：同一测量值四舍五入/单位换算后的写法仍应被视为一致，
    # 但 105 与 160/999 这类真实差异必须被拒。
    _NUMBER_RELATIVE_TOLERANCE = 0.01

    # 「定量结论」措辞：出现它们就意味着模型在解释某个测量结果；此时证据里
    # 的数值必须被复述出来，否则结论没有可核对的依据（"完全正常" 没有任何
    # 数字支撑 —— 这正是最危险的幻觉形态）。
    _QUANTITATIVE_JUDGEMENT = re.compile(
        r"正常|偏高|偏低|升高|降低|异常|贫血|超标|超出|高于|低于|危险|危急|严重"
        r"|住院|输血|没问题|不用担心|无需担心"
    )
    # 回指前文数值的表述（"该值低于参考范围"）不需要在本句重复数值。
    _ANAPHORIC_VALUE = re.compile(
        r"该值|这个值|此值|这一数值|该指标|这个指标|该结果|这个结果|上述值|上述结果|该项"
    )

    def extract_ids(self, text: str) -> list[str]:
        """按出现顺序提取引用 ID 并去重。"""
        return list(dict.fromkeys(self.citation_pattern.findall(text)))

    def _check_paragraph_coverage(self, text: str) -> None:
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if (
                len(paragraph) >= self._MIN_PARAGRAPH_CHARS_REQUIRING_CITATION
                and not self.citation_pattern.search(paragraph)
            ):
                raise UnsupportedClaim("回答中存在缺少证据引用的段落")

    def _check_high_risk_claims(self, text: str) -> None:
        """拦截诊断性结论，但豁免否定语境下的免责/转述句。

        逐句判定而不是整段一次 search：这样「不能仅凭此确诊为癌症」这类被否定词
        限定的免责句不会被误拦，而真正的断言句（本句中无否定语境）仍会被拦。
        """
        for sentence in re.split(r"(?<=[。！？!?；;])\s*|\n", text):
            sentence = sentence.strip()
            if not sentence:
                continue
            if is_negated_assertion(sentence):
                continue
            if self._high_risk_claims.search(sentence):
                raise UnsupportedClaim("回答包含不允许的诊断性结论")

    # --- 句子级引用锚定 ----------------------------------------------------

    def _strip_markers(self, text: str) -> str:
        return self.citation_pattern.sub("", text)

    def _is_marker_only(self, chunk: str) -> bool:
        """该片段除了引用标记与收尾符号外没有任何内容。"""

        return not self._strip_markers(chunk).strip().strip(self._TRAILING_NOISE)

    def sentences(self, text: str) -> list[str]:
        """切句，并把「纯引用标记」片段归并到前一句。

        模型常写成「…… 115-150 g/L。[e1]」：按句末标点切分后 ``[e1]`` 会
        变成独立片段，必须归属前一句，否则句末锚定会把正确写法误判为缺失。
        """

        sentences: list[str] = []
        for chunk in self._SENTENCE_SPLIT.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            if sentences and self._is_marker_only(chunk):
                sentences[-1] = f"{sentences[-1]}{chunk}"
            else:
                sentences.append(chunk)
        return sentences

    def _ends_with_citation(self, sentence: str) -> bool:
        core = sentence.strip().rstrip(self._TRAILING_NOISE)
        # P1-7（评审 2026-10-12）：不能只看「以 ] 结尾」——正文里的
        # Markdown 脚注 [1] / 年份 [2023] 同样以 ] 结尾，会假通过锚定检查。
        # 必须确认句尾的方括号匹配的是**证据 ID 形态**的引用标记。
        match = self.citation_pattern.search(core)
        return match is not None and match.end() == len(core)

    def _check_sentence_citations(self, text: str) -> None:
        """每个医学断言句都必须以引用标记结尾。

        免责/建议句（"请咨询医生"）与被否定的转述句（"不能仅凭此确诊为癌症"）
        不构成证据性结论，豁免；其余句子一律要求句末锚定的引用。
        """

        for sentence in self.sentences(text):
            core = self._strip_markers(sentence).strip(self._TRAILING_NOISE)
            if not core:
                continue
            if is_negated_assertion(sentence):
                continue
            if self._DISCLAIMER_SENTENCE.search(sentence):
                continue
            if not self._ends_with_citation(sentence):
                raise UnsupportedClaim("回答中存在未在句末标注证据引用的医学结论句")

    # --- 数值/单位一致性 ---------------------------------------------------

    def _numbers(self, text: str) -> list[tuple[float, str | None]]:
        values: list[tuple[float, str | None]] = []
        for match in self._NUMBER_WITH_UNIT.finditer(text):
            try:
                value = float(match.group("value").replace(",", ""))
            except ValueError:  # pragma: no cover - 正则已限定数字形态
                continue
            values.append((value, match.group("unit")))
        return values

    @staticmethod
    def _normalize_unit(unit: str | None) -> str | None:
        if unit is None:
            return None
        return unit.replace("μ", "u").replace("µ", "u").replace(" ", "").lower()

    @classmethod
    def _same_value(cls, claimed: float, observed: float) -> bool:
        return abs(claimed - observed) <= max(
            abs(observed) * cls._NUMBER_RELATIVE_TOLERANCE, 1e-9
        )

    def _check_numeric_consistency(
        self, text: str, cited_texts: dict[str, str]
    ) -> None:
        """回答中的每个数值都必须能在**本句所引用的证据**里定位，单位也要一致。

        数值幻觉（把 105 说成 160、把参考上限说成 999）是最容易通过纯 ID
        校验的幻觉形态，必须做数值级核对。

        核对范围按句绑定而非全池：此前的实现把**所有**被引证据的数值汇成一个
        池，于是回答「你的血红蛋白 160」并引用只含 105 的证据 e1 也会通过 ——
        只要 e2 里恰好出现过 160。模型可以从不相干的证据借值。逐句核对要求
        每个数值由该句自己声明的来源支撑。
        """
        for sentence, sentence_texts in self._sentences_with_evidence(text, cited_texts):
            observed = [
                (value, unit)
                for evidence_text in sentence_texts
                for value, unit in self._numbers(evidence_text)
            ]
            for claimed, unit in self._numbers(sentence):
                matched = next(
                    (item for item in observed if self._same_value(claimed, item[0])), None
                )
                if matched is None:
                    raise UnsupportedClaim("回答中的数值未能在被引证据中定位")
                claimed_unit = self._normalize_unit(unit)
                observed_unit = self._normalize_unit(matched[1])
                if claimed_unit and observed_unit and claimed_unit != observed_unit:
                    raise UnsupportedClaim("回答中的数值单位与被引证据不一致")

    def _sentences_with_evidence(
        self, text: str, cited_texts: dict[str, str]
    ) -> list[tuple[str, tuple[str, ...]]]:
        """把正文切成句子，并附上该句声明的引用所对应的证据文本。

        句内没有引用标记时（引言、免责句）使用全部被引证据作为兜底：这类句子
        本就不允许承载医学结论，用全集核对只是避免把普通表达误判为幻觉。
        """
        fallback = tuple(dict.fromkeys(cited_texts.values()))
        paired: list[tuple[str, tuple[str, ...]]] = []
        for sentence in self.sentences(text):
            sentence_texts = tuple(
                dict.fromkeys(
                    cited_texts[cited_id]
                    for cited_id in self.citation_pattern.findall(sentence)
                    if cited_id in cited_texts
                )
            )
            paired.append((sentence, sentence_texts or fallback))
        return paired

    def _check_quantitative_claims(self, text: str, evidence_texts: tuple[str, ...]) -> None:
        """定量结论必须复述证据中的数值。

        证据里存在测量数值时，一句「你的血红蛋白完全正常」没有任何可核对的
        数字支撑 —— 它只能来自模型的推断，而不是证据。
        """

        if not any(self._numbers(evidence_text) for evidence_text in evidence_texts):
            # 证据本身没有数值（如公共健康教育文本），无从要求复述。
            return
        for sentence in self.sentences(text):
            if not self._QUANTITATIVE_JUDGEMENT.search(sentence):
                continue
            if self._numbers(sentence):
                continue
            if self._ANAPHORIC_VALUE.search(sentence):
                # "该值低于参考范围" 这类回指句依赖前一句已给出的数值，
                # 是合法的中文医学表述。
                continue
            raise UnsupportedClaim("回答给出了定量结论但未引用证据中的具体数值")

    def validate(self, answer: DraftAnswer, evidence: list[Evidence]) -> ValidatedAnswer:
        """校验草稿答案，返回可投递给患者的版本。

        检查顺序按「越早失败越省事」排列，并且引用存在性先于任何依赖引用的
        检查：此前的实现把段落覆盖检查放在最后，而段落覆盖与引用存在性无关，
        失败顺序因此不稳定，同一份答案的拒绝原因取决于检查顺序而非缺陷本身。

        注意：本方法只保证「引用了存在且可定位的证据」并且不违反结构与数值
        约束。它**不**证明证据文本在语义上支持该断言 —— 那属于事实一致性
        评估（质检集）的职责。
        """
        self._check_high_risk_claims(answer.text)
        by_id = {item.id: item for item in evidence}
        cited_ids = list(dict.fromkeys(answer.cited_evidence))
        if not cited_ids:
            raise UnsupportedClaim("回答缺少证据引用")
        citations = []
        for evidence_id in cited_ids:
            item = by_id.get(evidence_id)
            if item is None:
                raise UnsupportedClaim("回答引用了不存在的证据")
            citation = item.citation
            if not citation.source_id or not citation.title or not citation.version or not citation.location:
                raise UnsupportedClaim("证据缺少可定位引用")
            citations.append(citation)
        self._check_paragraph_coverage(answer.text)
        self._check_sentence_citations(answer.text)
        # 数值核对必须在**去掉引用标记**的正文上做："[e1c1]" 里的 "1c1" 会被
        # 数值权重正则误认成「数值 1 + 单位 c1」。同理，逐句映射用的证据文本
        # 来自原始证据而非正文。
        cited_texts = {evidence_id: by_id[evidence_id].text for evidence_id in cited_ids}
        claim_text = self._strip_markers(answer.text)
        self._check_numeric_consistency(claim_text, cited_texts)
        self._check_quantitative_claims(claim_text, tuple(cited_texts.values()))
        return ValidatedAnswer(
            text=answer.text,
            citations=citations,
            cited_evidence=cited_ids,
        )

from __future__ import annotations

from medical_rag.query.contracts import QueryContext

# 口语/错别字/缩写 -> 标准医学词。替换式归一化。
TERM_MAP: dict[str, str] = {
    "验血": "血常规",
    "胸疼": "胸痛",
    "血压高": "高血压",
    "血压偏高": "高血压",
    "血压有点高": "高血压",
    "血糖高": "高血糖",
    "血糖偏高": "高血糖",
    "CT": "计算机断层扫描CT",
    "Hb": "血红蛋白",
    "脖子疼": "颈痛",
}

# 供后续检索层使用的同义词/缩写展开（本轮预留接口，不在文本里展开成 OR）。
SYNONYM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "高血压": ("高血压", "血压高", "Hypertension"),
    "血红蛋白": ("血红蛋白", "Hb"),
    "血常规": ("血常规", "血细胞分析"),
}


class NormalizeStep:
    """把口语化/错别字/简写术语替换为标准医学术语。"""

    def process(self, ctx: QueryContext) -> QueryContext:
        normalized = ctx.current
        for term, standard in TERM_MAP.items():
            normalized = normalized.replace(term, standard)
        if normalized != ctx.current:
            ctx.rewrite_log.append(f"normalize: {ctx.current.strip()} -> {normalized.strip()}")
            ctx.current = normalized
        return ctx

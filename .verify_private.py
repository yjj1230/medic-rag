"""步骤5：决定性验证——私有检索的向量路是否真的恢复。

对照修复前：vector_error=True、vector_candidates_raw=0（每次 400）。
现在应：vector_error 消失、vector_candidates_raw>0、且能用「改写查询」
（与语料无字面重合）命中，证明向量路径真实贡献而非 BM25 在起作用。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from opensearchpy import OpenSearch  # noqa: E402

from medical_rag.auth.policy import AccessContext  # noqa: E402
from medical_rag.core.config import Settings  # noqa: E402
from medical_rag.models.factory import resolve_embedding  # noqa: E402
from medical_rag.retrieval.contracts import RetrievalFilters, SearchQuery  # noqa: E402
from medical_rag.retrieval.opensearch_store import OpenSearchStore  # noqa: E402
from medical_rag.retrieval.service import RetrievalService  # noqa: E402

out: dict = {}
settings = Settings()
emb = resolve_embedding(settings)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
PATIENT = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER = uuid.UUID("33333333-3333-3333-3333-333333333333")
ctx = AccessContext(tenant_id=TENANT, patient_id=PATIENT, user_id=USER)

store = OpenSearchStore(OpenSearch(settings.opensearch_url, timeout=15))

# 直接测 store 层：向量检索是否还报维度错误
vec = tuple(emb.model.embed(["血常规检验报告单"])[0])
filters = RetrievalFilters(tenant_id=TENANT, patient_id=PATIENT, include_public=False, include_private=True)
try:
    recs = store.search_vector(vec, filters, 10)
    out["store_search_vector"] = {"ok": True, "n": len(recs), "ids": [r.id for r in recs]}
except Exception as e:  # noqa: BLE001
    out["store_search_vector"] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:300]}"}

svc = RetrievalService(
    store,
    embedding_model=emb.model,
    embedding_model_name=emb.model_name,
    embedding_dimensions=emb.dimensions,
)

# 私有语料文本：「血常规检验报告单 / 血红蛋白 105 g/L / 白细胞 ...」
# 用改写查询（避免字面重合）验证向量路径的真实贡献
queries = [
    ("私有-改写1-贫血", "这份化验单里血红蛋白偏低吗"),
    ("私有-改写2-化验", "我的验血结果怎么看"),
    ("私有-字面", "血常规检验报告单"),
]

for label, text in queries:
    q = SearchQuery(text=text, patient_id=PATIENT, top_k=5, include_public=False, include_private=True)
    r = svc.retrieve(q, ctx)
    d = r.diagnostics or {}
    out[label] = {
        "n_items": len(r.items),
        "reason": r.reason,
        "vector_error": d.get("vector_error"),
        "vector_candidates_raw": d.get("vector_candidates_raw"),
        "lexical_candidates_raw": d.get("lexical_candidates_raw"),
        "fused_candidates": d.get("fused_candidates"),
        "top_head": (r.items[0].text[:40] if r.items else None),
    }

svc.close()
Path(ROOT / ".probe.tmp").write_text(
    json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
)
print("WROTE")

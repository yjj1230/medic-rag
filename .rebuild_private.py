"""步骤4：修正 7 个 IndexJob 的 embedding_model，并重跑以刷新索引记录身份。

修正前：job.embedding_model = 'text-embedding-3-small'（网关名，错误）
修正后：job.embedding_model = resolve_embedding(settings).model_name（真实运行模型）
然后重置为 PENDING 重跑，让索引记录的 embedding_model/embedding_version 同步。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from opensearchpy import OpenSearch  # noqa: E402
from sqlalchemy import select  # noqa: E402

from medical_rag.core.config import Settings  # noqa: E402
from medical_rag.db.session import create_session_factory  # noqa: E402
from medical_rag.domain.enums import IndexJobStatus  # noqa: E402
from medical_rag.domain.models import IndexJob  # noqa: E402
from medical_rag.indexing.orchestrator import IndexOrchestrator  # noqa: E402
from medical_rag.models.factory import resolve_embedding  # noqa: E402
from medical_rag.retrieval.opensearch_writer import OpenSearchIndexWriter  # noqa: E402
from run_worker import embedding_pipeline_for  # noqa: E402

settings = Settings()
emb = resolve_embedding(settings)
correct_name = emb.model_name
print(f"correct embedding identity: {correct_name} (dims={emb.dimensions})")

factory = create_session_factory(settings)
client = OpenSearch(settings.opensearch_url, timeout=30)
private_writer = OpenSearchIndexWriter(
    "medical-rag-private-local",
    "medical-rag-private",
    client,
    visibility="private",
    embedding_dimension=emb.dimensions,
    text_analyzer=settings.text_analyzer,
    search_analyzer=settings.search_analyzer,
)
pipeline = embedding_pipeline_for(settings)

with factory() as session:
    jobs = list(session.scalars(select(IndexJob).order_by(IndexJob.created_at)))
    for job in jobs:
        print(f"  {job.id} model: {job.embedding_model!r} -> {correct_name!r}")
        job.embedding_model = correct_name
        job.embedding_dimensions = emb.dimensions
        job.status = IndexJobStatus.PENDING
        job.attempts = 0
        job.error_code = None
        job.worker_id = None
        job.lease_expires_at = None
        job.next_retry_at = None
        job.finished_at = None
    session.commit()

    orch = IndexOrchestrator(
        session, private_writer=private_writer, embedding_pipeline=pipeline,
        allow_unembedded=False,
    )
    print("--- reprocessing ---")
    for job in jobs:
        r = orch.process(job.id, "rebuild-worker-2")
        print(f"  {job.id} -> {r.status} indexed={r.indexed_count}")

# 回读校验
res = client.search(
    index="medical-rag-private-local",
    body={"size": 50, "_source": ["embedding_model", "embedding_dimensions", "embedding_version"], "query": {"match_all": {}}},
)
models = {}
for h in res["hits"]["hits"]:
    s = h["_source"]
    key = (s.get("embedding_model"), s.get("embedding_dimensions"), s.get("embedding_version"))
    models[key] = models.get(key, 0) + 1
print("--- index identity summary ---")
for k, v in models.items():
    print(f"  model={k[0]!r} dims={k[1]} version={k[2]!r} -> {v} docs")

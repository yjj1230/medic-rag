# -*- coding: utf-8 -*-
"""清理 e2e 冒烟上传的测试文档（b1e37791）：索引删记录 + DB 打墓碑。

背景：DELETE /v1/documents/{id} 因 API 进程未装配 private_index_writer 恒 500，
无法走正常删除路径。该文档为链路测试生成的虚构内容，不应留在患者语料中。
"""
import os
import sqlite3

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)
os.environ.setdefault("NO_PROXY", "*")

DOC_ID = "b1e37791-0d21-45b8-9b17-aa4662b3bde9"

from opensearchpy import OpenSearch

client = OpenSearch("http://localhost:9200", timeout=15)
resp = client.delete_by_query(
    index="medical-rag-private-local",
    body={"query": {"term": {"document_id": DOC_ID}}},
    refresh=True,
    conflicts="proceed",
)
print("index deleted:", resp.get("deleted"))

con = sqlite3.connect(r"C:\Users\lyx\Desktop\RAG\medical_rag.db")
cur = con.cursor()
cur.execute(
    "update documents set status='deleted', deleted_at=datetime('now'), updated_at=datetime('now')"
    " where id=?", (DOC_ID.replace("-", ""),),
)
print("db documents rows updated:", cur.rowcount)
# 同步版本/证据/事实层墓碑（与 DeleteService 同语义）
cur.execute(
    "update document_versions set status='deleted', deleted_at=datetime('now')"
    " where document_id=?", (DOC_ID.replace("-", ""),),
)
print("db document_versions rows updated:", cur.rowcount)
con.commit()
# 复核
left = client.count(
    index="medical-rag-private-local",
    body={"query": {"term": {"document_id": DOC_ID}}},
)["count"]
db = con.execute(
    "select status, deleted_at from documents where id=?", (DOC_ID.replace("-", ""),)
).fetchone()
print("post-check: index residual =", left, "| db =", db)
con.close()

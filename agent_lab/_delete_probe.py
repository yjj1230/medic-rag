# -*- coding: utf-8 -*-
"""复核 A-03：重放 DELETE + 查 DB 文档状态 + 查索引残留。"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)
os.environ.setdefault("NO_PROXY", "*")

BASE = "http://localhost:8000"
DOC_ID = "b1e37791-0d21-45b8-9b17-aa4662b3bde9"


def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


_, login_body = req("POST", "/v1/auth/login", body={"phone": "13800138000", "password": "test1234"})
token = json.loads(login_body)["access_token"]

# 1) 重放 DELETE（第一次）
s1, b1 = req("DELETE", f"/v1/documents/{DOC_ID}", token)
print("DELETE #1:", s1, b1[:200])

# 2) 重放 DELETE（第二次，幂等性）
s2, b2 = req("DELETE", f"/v1/documents/{DOC_ID}", token)
print("DELETE #2:", s2, b2[:200])

# 3) DB 文档状态
con = sqlite3.connect(r"C:\Users\lyx\Desktop\RAG\medical_rag.db")
row = con.execute(
    "select id, status, deleted_at from documents where id = ?",
    (DOC_ID.replace("-", ""),),
).fetchall()
print("DB documents:", row)

# 4) 索引残留
from opensearchpy import OpenSearch

client = OpenSearch("http://localhost:9200", timeout=15)
client.indices.refresh(index="medical-rag-private-local")
cnt = client.count(
    index="medical-rag-private-local",
    body={"query": {"term": {"document_id": DOC_ID}}},
)["count"]
print("index residual:", cnt)
con.close()

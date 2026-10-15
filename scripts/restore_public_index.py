"""从 SQLite 备份恢复 public 索引语料 ``medical-rag-public-local``。

背景
----
当前 public 索引里只有 28 条与评测集无关的文档（单一来源「贫血症」，
``source_id 7f0f9415-…``，不在 ``knowledge_sources`` 登记表内），而评测集
v2.1 的 60 条 public quality 用例所引用的 15 个 gold 证据 id **一条都不在
索引里** —— 复跑时 public 质量指标被压到 0.1429（只剩 private 那 10 条）。

语料来源
--------
``backups/medical_rag.db.pre_enum_normalization-2026-10-08`` 是入库管线的
SQLite 备份，其中 ``knowledge_chunks``（30 行，public 语料）与
``knowledge_sources``（11 行来源登记）保留了完整正文与来源元数据。
经核对，规范化 UUID 后 ``knowledge_chunks.id`` **完整覆盖 15/15 的
public gold**，因此它就是评测所依据的那份语料。

两个必须注意的格式差异
----------------------
1. SQLite 里的 id 是无连字符的 32 位十六进制，OpenSearch 里是标准 UUID
   （``7498fde6ae5e4af3…`` ↔ ``7498fde6-ae5e-4af3-…``）。写索引时必须
   规范化为带连字符形式，否则用例的 ``gold_evidence_ids`` 永远匹配不上。
2. DB 不含向量。向量由 ``LocalFastEmbedModel`` 现场编码（与索引写入路径
   同一模型、同一维度），保证打分空间与既有文档一致。

用法::

    python scripts/restore_public_index.py --dry-run   # 预览
    python scripts/restore_public_index.py             # 落库（先清不匹配文档）
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DEFAULT_DB = Path("backups/medical_rag.db.pre_enum_normalization-2026-10-08")
DEFAULT_INDEX = "medical-rag-public-local"

# 与现有 public 文档保持一致的版本标识（见索引内既有文档）。
PARSER_VERSION = "knowledge-parser-v1"
CLEANER_VERSION = "cleaner-v1"
CHUNKER_VERSION = "semantic-chunker-v1"
INDEX_VERSION = "public-v1"


def normalize_id(value: Any) -> str:
    """把无连字符 UUID 规范化为标准形式；非 UUID 原样返回。"""

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(value)


def load_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        raise SystemExit(f"备份库不存在: {db_path}")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    sources = {
        row[0]: row
        for row in cur.execute(
            "select id, institution, title, source_version, publication_date, "
            "language, scope, active from knowledge_sources"
        )
    }

    rows: list[dict[str, Any]] = []
    for row in cur.execute(
        "select id, source_id, content_hash, text, location, page_number, "
        "section, url, active from knowledge_chunks"
    ):
        chunk_id, source_id, content_hash, text, location, page, section, url, active = row
        source = sources.get(source_id)
        if source is None:
            raise SystemExit(f"chunk {chunk_id} 的来源 {source_id} 未在 knowledge_sources 登记")
        _, institution, title, source_version, pub_date, language, _scope, src_active = source
        rows.append(
            {
                "id": normalize_id(chunk_id),
                "text": text,
                "visibility": "public",
                "source_id": normalize_id(source_id),
                "source_title": title,
                "source_version": source_version or pub_date,
                "language": language or "zh-CN",
                "location": location,
                "page_number": page,
                "section": section,
                "url": url,
                "content_hash": content_hash,
                "source_active": bool(active) and bool(src_active),
                "institution": institution,
            }
        )
    con.close()
    return rows


def build_document(row: dict[str, Any], embedding: list[float], model_name: str) -> dict[str, Any]:
    """按索引既有文档的字段模板构造 _source（与 IndexRecord 契约一致）。"""

    return {
        "id": row["id"],
        "text": row["text"],
        "visibility": "public",
        "tenant_id": None,
        "patient_id": None,
        "document_id": None,
        "document_version_id": None,
        "evidence_unit_id": None,
        "source_id": row["source_id"],
        "source_title": row["source_title"],
        "source_version": row["source_version"],
        "language": row["language"],
        "location": row["location"],
        "page_number": row["page_number"],
        "section": row["section"],
        "sheet_name": None,
        "cell_range": None,
        "source_anchor": None,
        "url": row["url"],
        "confidence": None,
        "content_hash": row["content_hash"],
        "parser_version": PARSER_VERSION,
        "embedding": embedding,
        "embedding_model": model_name,
        "embedding_version": None,
        "embedding_dimensions": len(embedding),
        "index_version": INDEX_VERSION,
        "retrieval_eligible": True,
        "source_active": row["source_active"],
        "deleted": False,
        "created_at": None,
        "updated_at": None,
        "raw_text": None,
        "clean_text": None,
        "section_path": None,
        "parent_block_id": None,
        "chunk_index": None,
        "quality_flags": None,
        "cleaner_version": CLEANER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "extractor_version": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 SQLite 备份恢复 public 索引语料")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--url", default=None, help="默认读 .env 的 OPENSEARCH_URL")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    rows = load_rows(Path(args.db))
    print(f"DB 语料: {len(rows)} 条")

    from medical_rag.core.config import get_settings
    from medical_rag.models.factory import resolve_embedding
    from opensearchpy import OpenSearch

    settings = get_settings()
    url = args.url or settings.opensearch_url
    embedding = resolve_embedding(settings)
    if not embedding.enabled:
        raise SystemExit("向量后端不可用；public 索引必须带向量，拒绝写入")

    client = OpenSearch(url, timeout=60)
    if not client.ping():
        raise SystemExit(f"OpenSearch 不可达: {url}")

    kept = [r for r in rows if r["source_active"]]
    print(f"source_active=True 的条目: {len(kept)}")

    print("编码向量中…")
    vectors = embedding.model.embed([r["text"] for r in kept])
    if len(vectors) != len(kept):
        raise SystemExit("向量条数与文本条数不一致，拒绝写入")

    docs = [
        (row["id"], build_document(row, [float(v) for v in vec], embedding.model_name))
        for row, vec in zip(kept, vectors, strict=True)
    ]
    dims = {len(v) for _, d in docs for v in [d["embedding"]]}
    print(f"向量维度: {sorted(dims)} | 模型: {embedding.model_name}")

    # 现有文档里哪些不属于本次语料 → 需要清除（例如那 28 条无关文档）。
    new_ids = {doc_id for doc_id, _ in docs}
    existing = client.search(index=args.index, body={"size": 1000, "_source": False})
    stale = [h["_id"] for h in existing["hits"]["hits"] if h["_id"] not in new_ids]
    print(f"现有文档: {existing['hits']['total']['value']} 条；其中待清除: {len(stale)} 条")
    if stale:
        print("  待清除 id 示例:", stale[:3])

    if args.dry_run:
        print("dry-run：未写入。")
        print("首条样例:", {k: (v if k != "embedding" else f"<{len(v)}>") for k, v in docs[0][1].items()})
        return 0

    if stale:
        resp = client.delete_by_query(
            index=args.index,
            body={"query": {"ids": {"values": stale}}},
            refresh=True,
            conflicts="proceed",
        )
        print(f"已清除 {resp.get('deleted', 0)} 条无关文档")

    body: list[Any] = []
    for doc_id, doc in docs:
        body.append({"index": {"_index": args.index, "_id": doc_id}})
        body.append(doc)
    result = client.bulk(body=body, refresh=True)
    if result.get("errors"):
        bad = [i for i in result["items"] if i.get("index", {}).get("status", 500) >= 300]
        print(f"写入失败 {len(bad)} 条；首条错误: {bad[0]}", file=sys.stderr)
        return 1

    count = client.count(index=args.index).get("count")
    print(f"写入完成。当前 {args.index} 文档数: {count}")
    return 0 if count == len(docs) else 1


if __name__ == "__main__":
    raise SystemExit(main())

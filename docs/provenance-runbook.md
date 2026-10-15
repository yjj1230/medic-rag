# Chunk Provenance 运维手册

## 上线前检查

新 PostgreSQL 实例应使用独立的 OpenSearch 物理索引命名空间，避免读取旧 SQLite
对应的残留索引：

```powershell
$env:DATABASE_URL = "postgresql+psycopg://medical_rag:medical_rag@127.0.0.1:55432/medical_rag"
$env:RAG_PUBLIC_INDEX_NAME = "medical-rag-public-pg-local"
$env:RAG_PRIVATE_INDEX_NAME = "medical-rag-private-pg-local"
$env:RAG_TEXT_ANALYZER = "cjk"
$env:RAG_SEARCH_ANALYZER = "cjk"
python scripts/run_worker.py init-indexes
```

`init-indexes` 只创建或校验新物理索引，不删除 `medical-rag-public-local` 或
`medical-rag-private-local`。

```powershell
uv run python scripts/provenance_audit.py --scope all --database-url <DATABASE_URL>
```

重点关注 `missing_locator`、`missing_identity` 和 `missing_position`。未知位置必须进入人工复核，不能用默认页码或默认锚点填充。

## 安全回填

先执行只读预览：

```powershell
uv run python scripts/backfill_chunk_provenance.py --scope all --database-url <DATABASE_URL> --dry-run
```

确认输出后再执行写入：

```powershell
uv run python scripts/backfill_chunk_provenance.py --scope all --database-url <DATABASE_URL>
```

脚本只根据已有的页码、章节、区块、Sheet、单元格范围和来源锚点生成 locator，不会读取或输出原文，也不会覆盖已有 locator。

## 按 Chunk 回溯原文

1. 通过 `chunk_id` 查询 OpenSearch 记录。
2. 读取 `document_version_id` 和 `evidence_unit_id`。
3. 在数据库中查询对应的 `EvidenceUnit`。
4. 根据 `locator` 中的页码、章节、`block_id`、Sheet 或单元格范围定位原始文档。
5. 对比 `content_hash`、`parser_version`、`cleaner_version`、`chunker_version` 和 `index_version`。

## 删除验证

删除文档后，必须验证数据库证据行、对象存储对象、私有 OpenSearch 记录和引用查询均无残留。删除查询必须使用 `document_id`、`document_version_id` 或 `evidence_unit_id`，不得使用文本或展示位置。

## 故障判断

- `missing_locator`：历史数据没有完整来源定位，需要回填或人工复核；
- `hash_mismatch`：文本或清洗结果发生变化，需要重新解析和向量化；
- `version_migration`：解析器、分块器、Embedding 或索引版本发生变化；
- `locator` mismatch：内容可能相同，但来源位置记录与数据库不一致；
- `orphaned`：索引存在而数据库没有对应记录，应删除索引残留。

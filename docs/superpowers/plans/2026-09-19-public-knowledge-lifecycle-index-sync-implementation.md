# 公共知识生命周期与索引同步 Implementation Plan

> 对应 spec：`docs/superpowers/specs/2026-09-19-public-knowledge-lifecycle-index-sync-design.md`
> 日期：2026-09-19

## Global Constraints

- **技术栈**：Python 3.12+，SQLAlchemy 2.0，pytest；索引为 OpenSearch，测试用 `InMemorySearchIndex` 与记录型 writer 替身。
- **不可破坏的既有契约**：
  - `PublicIndexRecord.__post_init__` / `IndexRecord.__post_init__` 禁止 `retrieval_eligible=False` 的公共记录（"不可检索"只能表示为"不存在"）。
  - `KnowledgeSourceRegistry.set_active` 保持纯领域层，**不得**注入索引依赖。
  - 索引写入的落点是运维进程（`scripts/run_worker.py`），不进 API 进程。
  - 不允许写入无向量的"索引就绪"公共记录（沿用 `allow_unembedded` 语义）。
  - 公共记录不得携带 `tenant_id` / `patient_id`。
- **运行测试的环境**：`C:\Users\lyx\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m pytest`（managed python 未安装项目依赖）。
- **TDD 顺序**：每个任务先写失败测试 → 确认真的失败 → 再实现 → 再验证。

---

### Task 1: 同步 stale 测试，恢复套件绿

失败测试已存在，无需新写；本任务是把被钉住的**旧缺陷**改成断言**新行为**。

- [ ] Step 1: 读当前失败项，确认失败原因

```bash
python -m pytest tests/integration/test_public_knowledge_retrieval_loop.py -q
```

  期望：`test_deactivation_alone_does_not_reach_the_index_and_the_scan_finds_the_residual` 报 `assert True is False`。

- [ ] Step 2: 改写该测试为断言新行为

  拆成两条，各钉一件事：

  1. `test_deactivation_alone_does_not_reach_the_index`——**保持**原断言：`set_active(False)` 后索引记录仍在、`retrieval_eligible` 仍为 `True`、检索仍返回。这是**运维危害**本身（停用命令若不接线，患者仍能检索到），必须继续钉住。
  2. `test_reconciliation_repairs_the_deactivation_residual`——scan 报 `inactive` / `delete`，`repair` **成功**（`success is True`、`outcome == "repaired"`），残留被移出索引，检索返回空。

  **保留非空转守卫**：断言 `report.items[0].observed` 真的带上了 `content_hash` 与 `retrieval_eligible`（`_reason` 会跳过 `None` 字段，不守卫的话扫描是空转的）。

- [ ] Step 3: 跑该文件，确认全绿
- [ ] Step 4: 跑全量套件，确认 `0 failed`

---

### Task 2: 停用/启用与公共索引同步

- [ ] Step 1: 写失败测试（先测 `reindex_source`）

  文件：`tests/integration/test_knowledge_lifecycle_index_sync.py`（新建）

  - `test_reindex_source_writes_active_chunks_back_with_full_citation`——注册 + ingest 到一个 `InMemorySearchIndex`，手动清空索引（模拟被停用删除），调用 `reindex_source`，断言记录写回、`RetrievalService` 能检索到、`citation` 四要素齐全。
  - `test_reindex_source_skips_inactive_chunks`——部分 chunk `active=False`，断言只写回 active 的。
  - `test_reindex_source_never_writes_patient_acl`——断言 `tenant_id is None`、`patient_id is None`。
  - `test_reindex_source_fails_closed_without_a_pipeline_when_unembedded_disallowed`——`allow_unembedded=False` 且无 pipeline 时抛错，且索引未写入。

- [ ] Step 2: 运行并确认失败（`AttributeError: 'KnowledgeIngestService' object has no attribute 'reindex_source'`）
- [ ] Step 3: 在 `KnowledgeIngestService` 实现 `reindex_source`，复用 `_version_bundle()` / `_index_batch()` / `embedding_pipeline`
- [ ] Step 4: 跑 Step 1 测试，确认通过

- [ ] Step 5: 写运维入口的失败测试

  文件：`tests/integration/test_knowledge_operations.py`（追加）

  - `test_set_active_inactive_deletes_the_index_records`——停用后 writer 上产生删除调用、记录消失。
  - `test_set_active_inactive_reports_index_failure_without_claiming_success`——writer 抛 `OpenSearchWriteError` 时命令非零退出/抛错，**不打印成功**。
  - `test_set_active_active_restores_the_index_records`——停用再启用后记录回到索引。
  - `test_deactivate_then_retrieve_returns_nothing`——闭环：停用后 `RetrievalService` 返回空且 `reason == insufficient_evidence`。

- [ ] Step 6: 运行并确认失败
- [ ] Step 7: 改 `scripts/run_worker.py`：`set_knowledge_source_active` 接受 `public_writer`，停用时删除该源全部分块记录、启用时调 `reindex_source`
- [ ] Step 8: 跑 Step 5 测试 + Task 1、Task 2 Step 1 的测试，确认通过

---

### Task 3: `knowledge-reconcile` 运维入口

- [ ] Step 1: 写失败测试

  文件：`tests/integration/test_knowledge_reconcile_command.py`（新建）

  - `test_scan_reports_the_residual_and_exits_non_zero`——制造停用残留（DB 已停用、索引仍有记录），`knowledge-reconcile --scope public` 返回 1 并打印 `inactive` / `delete`。
  - `test_scan_alone_changes_nothing`——不加 `--apply` 时索引记录数不变（dry-run 不越权）。
  - `test_apply_repairs_the_residual_and_exits_zero`——`--apply` 后记录消失、再次扫描干净、返回 0。
  - `test_apply_reports_public_rebuild_as_required_not_repaired`——`public` 的 `rebuild` 仍返回 `rebuild_required`，命令如实上报并返回 1（不得静默跳过）。
  - `test_scope_defaults_to_public`——默认作用域是 public。

- [ ] Step 2: 运行并确认失败
- [ ] Step 3: 在 `scripts/run_worker.py` 实现 `reconcile_knowledge(settings, scope, limit, apply)` 与子命令解析（`--scope` / `--limit` / `--apply`），接线 `public_index=OpenSearchStore(client)` + `public_writer=OpenSearchIndexWriter(...)`
- [ ] Step 4: 跑 Step 1 测试，确认通过

---

### Task 4: 端到端验证与交付说明

- [ ] Step 1: 全量套件

```bash
python -m pytest -q
```

  记录通过 / 失败 / 跳过数。

- [ ] Step 2: 静态检查

```bash
python -m ruff check src tests
```

- [ ] Step 3: 真实 CLI 链路（无 OpenSearch 集群时说明哪些是环境限制）

```bash
python scripts/run_worker.py knowledge-register --help
python scripts/run_worker.py knowledge-list
python scripts/run_worker.py knowledge-reconcile --scope public
```

- [ ] Step 4: 写交付说明 `docs/公共知识生命周期索引同步-交付说明.md`，含偏差与遗留项
- [ ] Step 5: 清理临时文件（`.pytest_run.txt` 等），确认仓库无残留

---

## Plan Self-Review

- **每个任务可独立验收？** 是。Task 1 的验收是套件绿；Task 2 验收是停用/启用的检索行为；Task 3 验收是对账命令的退出码与残留变化；Task 4 是整体证据。
- **是否每个任务都有"确认失败"步骤？** 是（Task 1 的失败已存在并先复现；Task 2、3 均先写测试并确认失败）。
- **范围是否过大？** 三个功能的落点集中在一个运维入口加一个服务方法，未触碰 API 层与读路径；`public rebuild` 的既有失败语义被保留而非绕过。
- **歧义是否写死？** 停用=删除、删除范围=该源全部分块、先提交 DB 再删索引、默认 dry-run、发现残留退出码 1——均在 spec §5 写死。
- **非目标是否明确？** 是（spec §2），特别是"平铺正文无 location"与"真实评测集"两项明确留给后续。

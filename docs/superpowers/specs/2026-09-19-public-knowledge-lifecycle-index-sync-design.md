# 公共知识生命周期与索引同步 —— 设计文档

> 日期：2026-09-19
> 关联：`docs/知识库搭建评估报告.md`（2026-09-17）、`docs/superpowers/plans/2026-09-15-scheme-b-indexing-platform-implementation.md`
> 前置事实：公共知识入库 CLI（`knowledge-register` / `knowledge-ingest` / `knowledge-list` / `knowledge-set-active`）已于 2026-09-17 落地在 `scripts/run_worker.py`，测试见 `tests/integration/test_knowledge_operations.py`。本设计处理它**下游**的缺口。

---

## 1. 背景

2026-09-18 的公共知识闭环验证（`tests/integration/test_public_knowledge_retrieval_loop.py`）在"入库 → 检索"两端接通后，暴露出三个真实缺口：

**缺口 A：停用知识源不触达索引（医学安全）**

`KnowledgeSourceRegistry.set_active(False)` 只做两件事：把 source 与 chunk 行的 `active` 置 `False`、向 outbox 写一条 `knowledge.source.deactivated` 事件。**索引没有任何变化**——记录上的 `retrieval_eligible` 与 `source_active` 仍是 `True`。

后果：一份被撤下（可能是内容有误、或指南已废止）的医学知识，患者在停用后**仍然检索得到**。而 `ReconciliationService._action_for("inactive")` 早已返回 `delete`，说明"停用应删除"是既定语义，只是没有任何东西去执行它。

**缺口 B：对账补偿没有生产入口**

`ReconciliationService` 在 `src/` 下的构造点数量为 **0**（`grep` 结果：仅测试构造）。它可以发现并修复残留，但线上没有任何东西会调用它，所以 A 产生的残留永远不会被收敛。

**缺口 C：套件当前是红的**

2026-09-18 13:29–13:30 的两处改动（给 `InMemorySearchIndex` 补 `delete_record`；给 `ReconciliationService` 加 `public_writer` / `private_writer` 拆分）修掉了"删除口缺失"这一半，但那条**钉住旧缺陷**的测试没同步更新——它仍断言 `repair` 应当失败，于是现在报 `assert True is False`。全量状态：`1 failed, 662 passed, 3 skipped`。

红色的套件让后续所有判断都不可信，必须最先处理。

---

## 2. 目标与非目标

### 目标

1. **停用即时生效**：`knowledge-set-active --inactive` 之后，被停用知识源的索引记录被物理删除，检索不再返回其中任何证据。
2. **启用对称可恢复**：`knowledge-set-active --active` 之后，该源的分块重新写入索引。若不补这条，停用删除就会制造一个新脚枪——"停用再启用后内容静默不可检索"。
3. **对账有生产入口**：新增 `knowledge-reconcile` 运维命令，默认只扫描（dry-run），`--apply` 才执行修复。
4. **套件恢复绿**：全量测试无失败。

### 非目标

- **不修"平铺正文无 `--location` 静默不可检索"**。该行为已有测试钉住并标注为已知运营风险；修复它需要重新定义"引用四要素不全时入库是否该失败关闭"，涉及入库策略变更，另立设计。本次保持原状并继续钉住。
- **不做真实评测集**（评估报告 P0-3：`eval/golden.py` 的模拟检索仍在，recall/MRR 仍是构造值）。这是独立的下一阶段。
- **不引入真实医学语料**（评估报告 P0-2），需业务方提供脱敏语料后另行处理。
- **不做前端知识库管理页**（评估报告 §5 的第三个选项）。
- **不改 API 层**。`api/app.py` 记录了架构决定"索引写入属于 worker 进程"，`api.dependencies.get_public_index_writer` 在启动时从不被满足是有意为之；索引副作用继续留在运维入口。

---

## 3. 总体架构

### 分层与职责

```
scripts/run_worker.py                     ← 运维入口（索引副作用的唯一落点）
  ├─ knowledge-set-active --inactive      → 停用 + 删除索引记录
  ├─ knowledge-set-active --active        → 启用 + 重建索引记录
  └─ knowledge-reconcile [--apply]        → 对账扫描（+ 修复）

src/medical_rag/knowledge/ingest_service.py
  ├─ KnowledgeSourceRegistry.set_active   ← 纯领域层，不依赖索引（保持不变）
  └─ KnowledgeIngestService.reindex_source ← 新增：从库中分块重建公共索引记录

src/medical_rag/indexing/reconciliation.py
  └─ ReconciliationService(session, public_index=<可枚举>, public_writer=<可删除>)
```

`set_active` 保持纯领域层、不注入索引依赖，是为了不破坏既有的分层：领域层不应当知道 OpenSearch 存在。索引副作用放在运维入口，因为该脚本本来就持有 OpenSearch 客户端、物理索引名与读别名（`read_aliases()` 从 `OpenSearchStore` 签名解析，避免别名漂移）。

### 为什么"停用"等于"删除"

公共索引记录契约（`knowledge/contracts.py`）显式禁止非法状态存在：

```python
# PublicIndexRecord.__post_init__
if not self.retrieval_eligible or not self.source_active or self.deleted:
    raise ValueError("public index records must be active, eligible, and not deleted")
```

即："不可检索"在公共索引里的**唯一合法表示是不存在**。因此不存在"改写标记位降级"这个选项——要么删除，要么违反契约。`ReconciliationService` 的 docstring 也写明了同一立场："Deleted, inactive, and review-required rows are always physically removed instead of being re-indexed as tombstones."

### 删除走哪个口

`ReconciliationService.__init__` 已完成 writer 拆分，理由写在代码注释里：`OpenSearchStore` 能枚举但不能删，`OpenSearchIndexWriter` 能删但不能枚举。对账入口据此接线——**枚举用 store，删除用 writer**。同一个口被停用命令复用。

---

## 4. 核心数据契约

### 4.1 新增：`KnowledgeIngestService.reindex_source`

```python
def reindex_source(self, source_id: UUID) -> IngestReceipt:
    """把该源当前 active 的分块重新写入公共索引。

    停用会物理删除索引记录（公共契约不允许非 eligible 记录存在），因此重新
    启用必须把记录写回去。分块步骤被跳过——chunk 行本身就是事实来源——但
    embedding 通道与版本指纹复用 ingest 的同一条路径，保证恢复出来的记录与
    一次新鲜 ingest 的产物一致。
    """
```

返回既有 `IngestReceipt`（`created_chunks` / `skipped_chunks` / `indexed_chunks` / `source_id`），不新增结构体。语义映射：`indexed_chunks` = 真正写回索引的块数，`skipped_chunks` = 因源未启用而不写的块数。

### 4.2 新增：索引同步的内部结果

运维入口内部使用，不对外暴露：

```python
@dataclass(frozen=True, slots=True)
class IndexSyncReceipt:
    source_id: str
    indexed_chunks: int   # 写回索引的块数（启用路径）
    deleted_chunks: int   # 从索引删除的块数（停用路径）
```

### 4.3 复用：`ReconciliationReport` / `RepairReceipt`

`knowledge-reconcile` 直接消费既有结构，不新增：

- `ReconciliationReport(scope, scanned, items)`，每项 `ReconciliationItem(record_id, scope, reason, action, observed, expected, item_id)`
- `RepairReceipt(item_id, scope, action, success, outcome, error_code)`

### 4.4 新增：`OpenSearchStore.scan(scope, limit) -> list[dict]`

对账的"观察侧"原本**不存在**。写路径持有物理索引名，读路径只知道别名，两个适配器都无法列出自己的内容——`ReconciliationService` 构造函数里"`OpenSearchStore` 能枚举但不能删"的注释与实际代码不符。

新增的枚举接口按 `_source` 过滤只取身份元数据（**不含 `text`**），并查询**读别名**，使扫描看到的正是检索会返回的东西：

```python
SCAN_FIELDS = (
    "visibility", "tenant_id", "patient_id", "document_id", "document_version_id",
    "evidence_unit_id", "source_id", "source_version", "content_hash",
    "parser_version", "embedding_model", "index_version",
    "retrieval_eligible", "source_active", "deleted",
)

def scan(self, scope: str = "public", limit: int = 100) -> list[dict[str, Any]]: ...
```

返回的每条映射带上 `id`（取自命中项的 `_id`——文档 id 才是比较所用的记录身份，并非每个写入器都在 `_source` 里重复它）。

---

## 5. 设计细节

### 5.1 停用：删除该源的**全部**分块记录

删除范围取该知识源的全部分块（`active=True` 与 `active=False` 都删），而不是只删当时 active 的：

- 停用是"该源整体下架"的语义，任何该源的索引残留都应当清除；
- 幂等：`OpenSearchIndexWriter.delete_record` 对不存在的 ID 是 no-op（`ignore=[404]`），`InMemorySearchIndex.delete_record` 同样按 ID 过滤，重复执行安全。

**先写 DB 还是先删索引？** 先提交 DB 事务（`set_active` 内部 `commit`），再执行索引删除。理由：DB 是事实来源，若先删索引后 DB 事务失败，会出现"索引没了但源仍标记为启用"的状态——**内容静默消失**，比残留更难被发现。反过来（DB 已停用、索引有残留）是可检测、可收敛的状态：检索仍会返回，但对账扫描能发现并修复，命令也会明确告知。

### 5.2 启用：从库中分块重建记录

重建字段逐一取自 `KnowledgeChunk` 行与 `KnowledgeSource` 行，版本指纹取自复用基线：

| 记录字段 | 来源 |
|---|---|
| `id` | `str(chunk.id)`（与 ingest 一致，保证是同一个索引文档，重复写入是替换而非新增） |
| `text` / `location` / `page_number` / `section` / `url` / `content_hash` | `KnowledgeChunk` 行 |
| `source_id` / `source_title` / `source_version` / `language` | `KnowledgeSource` 行 |
| `visibility` / `retrieval_eligible` / `source_active` / `deleted` | 公共安全常量（`public` / `True` / `True` / `False`） |
| `parser_version` / `cleaner_version` / `chunker_version` / `extractor_version` | `_version_bundle()`（与 ingest 同一来源，避免版本漂移被判为 `version_migration`） |
| `embedding` / `embedding_model` / `embedding_dimensions` / `index_version` | embedding 通道（同 ingest） |

**注意 `document_id` 留空**：`PublicIndexRecord` 的注释明确"public source identity is not a document identity"，ingest 路径也不填，保持两侧一致。

### 5.3 启用时的 embedding 缺失处理

与 ingest 路径采取**同一策略**，不另立规则：

- 有 embedding 通道 → 向量化后写入；
- 无通道且 `allow_unembedded` → 不向量化写入（仅 BM25 可检索），并打印警告；
- 无通道且 `allow_unembedded=False`（或 `--require-embeddings`）→ **失败关闭**，抛错退出，不写半成品。

理由：写"索引就绪但无向量"的记录会让向量检索路径静默失效，而记录看起来是完整的——这正是 `_RebuildRequired` 这条既有防御要拦的事。

### 5.4 对账入口

```
python scripts/run_worker.py knowledge-reconcile [--scope public] [--limit 100] [--apply]
```

- 默认**只扫描**，打印每个残留的 `reason` / `action` / `item_id`，不修改任何东西。
- `--apply` 才逐项 `repair`，并打印每条 `RepairReceipt`。
- 作用域当前仅支持 `public`（默认）。`private` / `all` 返回 2 并说明原因：私有链条的残留由 `IndexWorker` 自身的删除路径处理，此处尚未接入可枚举的私有索引侧——**扫描一个没看过的索引却给出结论，比拒绝更糟**。
- 接线：`public_index=OpenSearchStore(client)`（枚举走读别名）、`public_writer=OpenSearchIndexWriter(...)`（删除走物理索引）。这是 `ReconciliationService` writer 拆分存在的原因。
- 退出码（供 cron / CI 消费）：

| 码 | 含义 |
|---|---|
| `0` | 扫描未发现残留 |
| `1` | 发现残留（未加 `--apply`），或 `--apply` 后仍有未修复项 |
| `2` | 索引侧**不可枚举**，无法给出判定 |

退出码 2 是本命令的重点：观察侧静默为空时，扫描等于拿数据库跟"什么都没有"比较，会把能力缺失报告成"索引干净"。拒绝出结论是唯一诚实的答案。

`public` 作用域的 `rebuild` 仍会返回 `rebuild_required`（`_upsert` 里 `_RebuildRequired("public rebuild requires the ingest write path")`）——这是既有设计：本服务没有 embedding provider，不允许自己写无向量记录。命令如实打印并计入未修复项（退出 1），不掩盖、不绕过。

### 5.5 错误与降级

| 情形 | 行为 |
|---|---|
| `opensearch-py` 未安装 | **失败关闭**：停用/启用/对账都需要写索引，报错退出并提示安装 `.[live]`（沿用 `_build_public_writer` 既有行为） |
| OpenSearch 不可达（集群故障） | 停用命令：DB 已停用、索引未清理 → 以**非零退出**并明确提示"请稍后运行 `knowledge-reconcile --apply` 收敛"，绝不打印成功 |
| 索引侧不可枚举 | 对账命令返回 **2** 并说明原因，不给判定（空观察侧 = 假干净） |
| 单个 `delete_record` 失败 | 抛出 `OpenSearchWriteError`，命令非零退出；已删除的部分保留（幂等，重跑安全） |
| 源不存在 / UUID 非法 | `SystemExit` + 明确文案（沿用既有 `_parse_source_id`） |
| `--limit` 非正 | `ValueError`，由 `main` 统一转为错误文案 + 退出 1 |

### 5.6 安全与合规不变量

1. **停用后检索不得返回该源任何证据**——由"删除"保证，且新增断言测试。
2. **公共索引记录不得携带 `tenant_id` / `patient_id`**——重建路径逐条断言。
3. **不得写入无向量的"索引就绪"记录**——沿用 `allow_unembedded` 失败关闭语义。
4. **对账载荷不携带正文**——`scan` 只比较哈希与标识元数据（既有性质），本次不破坏。
5. **停用不得产生"假成功"**——索引侧失败必须以非零退出暴露。

### 5.7 性能

停用/启用的索引操作按块循环（`delete_record` / `upsert` 逐条）。单源分块量级为几十到几百，且是低频运维动作，不做批量优化；如未来单源上万块再引入 bulk 删除。

### 5.8 实现中发现并修掉的缺陷：读模型与写模型的身份字段不对称

把对账入口接起来后，一个健康记录被判成 `stale`，追查到两个模型的字段差异：

| 模型 | 公共来源身份存放位置 |
|---|---|
| 写模型 `IndexRecord` | 顶层 `source_id` / `source_version` |
| 读模型 `SearchRecord` | 只在 `citation.source_id` / `citation.version` |

`ReconciliationService._metadata` 只读顶层字段，于是观察侧的 `source_id` / `source_version` 恒为 `None`，而 `_reason` 的"stale"循环里有一句 `if key in expected and observed.get(key) != expected.get(key)` ——**每一条健康的公共记录都会被判为 `stale`**，action 为 `rebuild`；而 public 的 rebuild 又必然 `rebuild_required`，所以扫描会永久报红、修不掉。

这条缺陷此前不可见，原因是既有测试用记录型 writer 返回的是 `IndexRecord`（字段齐全），比真实适配器"更富"——又一次"测试替身比真实实现更完整"的案例。

修复：`_metadata` 在顶层缺失时从 `citation` 回读来源身份。这不是绕过——`citation.source_id` 就是同一个事实，只是读模型把它放在了引用结构里。

**对线上扫描的实际影响**：新建的 `OpenSearchStore.scan` 返回原始 `_source`，其中带有写入器写下的 `source_id` / `source_version`，因此真实部署路径本不受此缺陷影响；受影响的是任何以 `SearchRecord` 作为观察侧的适配器。修复后两条路径一致。

---

## 6. 实施阶段

| 阶段 | 内容 | 独立验收标准 |
|---|---|---|
| 1 | 同步 stale 测试，恢复套件绿 | 全量 pytest：0 failed |
| 2 | `KnowledgeIngestService.reindex_source` + 运维入口停用/启用同步 | 新增测试：停用后检索为空；启用后检索恢复且引用四要素完整 |
| 3 | `knowledge-reconcile` 命令 | 新增测试：扫描发现残留并返回 1；`--apply` 后残留消失、返回 0 |
| 4 | 端到端验证 + 交付说明 | 真实 CLI 跑通全链路并留证据；偏差显式记录 |

---

## 7. 验收标准

**可观测链路**（真实 CLI，非仅单测）：

```
knowledge-register（启用 + 审核人）
  → knowledge-ingest（写入公共索引）
  → knowledge-list（块 N/N 活跃）
  → set-active --inactive（删除索引记录）
  → 检索不再返回该源证据
  → knowledge-reconcile --scope public（扫描干净，退出 0）
  → set-active --active（重建索引记录）
  → 检索再次返回，且 citation 四要素齐全
```

**逐条断言的安全不变量**：§5.6 的 5 条。

**测试**：全量 `pytest` 通过数与跳过数记录在案；`ruff check src tests` 通过。

**偏差上报**：环境限制、替代实现、遗留项（含原因与恢复条件）写入交付说明。

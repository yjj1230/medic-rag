# 医疗患者端 RAG MVP 实施计划

> **给执行代理的要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）跟踪。

**目标：** 构建面向患者的试点级医疗 RAG 系统，用带引用的方式解释权威医学内容和患者上传的报告，并落实知情同意、外部企业级 LLM 接入和医疗安全控制。

**架构：** 使用模块化 FastAPI 应用：PostgreSQL 负责业务数据和结构化医学事实，OpenSearch 负责公共 / 私有混合检索，企业对象存储保存原始文件，独立异步解析 Worker 负责文件处理。所有 LLM、Embedding、Reranker、OCR 和视觉模型调用都经过模型网关；患者授权和检索过滤始终由本系统控制。

**技术栈：** Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL、OpenSearch、S3 兼容对象存储、Redis + Celery、httpx、pytest、OpenTelemetry。

**设计文档：** `docs/superpowers/specs/2026-09-09-medical-patient-rag-design.md`

## 全局约束

- 部署环境为专有云 / VPC。
- 第一版支持中文和英文，以及 PDF、图片、Word 和 Excel 上传。
- 患者必须使用实名账号，并在上传或向外部模型传输前完成明确知情同意。
- 外部模型供应商必须通过合同明确保证不训练、不复用患者数据，并明确留存周期和数据区域。
- 公共医学知识和患者私有资料必须使用独立索引和权限边界。
- 权限过滤必须在重排序和上下文组装之前完成。
- 系统不得进行诊断、开处方、修改药物或剂量，也不得替代医生进行临床决策。
- 患者数据删除必须传播到原始文件、解析文本、结构化事实、向量、缓存、摘要和所有派生数据。
- 患者资料只能通过模型出口网关发送给外部企业级模型。
- 试点目标为用户不超过 1,000 人、每月处理报告不超过 10,000 份、并发请求不超过 20 个。
- 任何实施任务都不能在缺少评估器、来源引用和人工复核路径的情况下宣称医学正确。

---

## 文件结构

创建以下职责清晰的模块：

```text
pyproject.toml
docker-compose.yml
.env.example
src/medical_rag/
  api/app.py
  api/dependencies.py
  api/routes/auth.py
  api/routes/documents.py
  api/routes/chat.py
  api/routes/health.py
  core/config.py
  core/errors.py
  db/session.py
  domain/enums.py
  domain/models.py
  domain/repositories.py
  auth/service.py
  auth/policy.py
  consent/service.py
  audit/service.py
  storage/object_store.py
  storage/file_policy.py
  ingest/upload_service.py
  ingest/delete_service.py
  parsing/contracts.py
  parsing/service.py
  parsing/worker.py
  parsing/adapters.py
  knowledge/contracts.py
  knowledge/ingest_service.py
  knowledge/chunker.py
  retrieval/contracts.py
  retrieval/service.py
  retrieval/opensearch_store.py
  models/contracts.py
  models/gateway.py
  models/http_adapters.py
  safety/rules.py
  safety/service.py
  chat/contracts.py
  chat/prompts.py
  chat/orchestrator.py
  chat/citations.py
  observability/tracing.py
  observability/metrics.py
alembic/versions/0001_initial_schema.py
tests/
  unit/
  integration/
  contract/
  eval/
```

公共 API 模块只负责转换 HTTP 输入和输出。领域模块与编排模块不得依赖 FastAPI 请求对象。外部供应商只能通过契约和适配器访问，这样更换供应商不会影响 RAG 核心。

## 任务 1：初始化应用与测试框架

**文件：**
- 创建：`pyproject.toml`
- 创建：`docker-compose.yml`
- 创建：`.env.example`
- 创建：`src/medical_rag/api/app.py`
- 创建：`src/medical_rag/core/config.py`
- 创建：`src/medical_rag/db/session.py`
- 创建：`tests/conftest.py`
- 创建：`tests/unit/test_health.py`

**接口：**
- 提供 `Settings.from_env() -> Settings`。
- 提供 `create_app(settings: Settings | None = None) -> FastAPI`。
- 提供 `get_session() -> Generator[Session, None, None]`。

- [x] **步骤 1：编写预期失败的健康检查测试**

```python
def test_health_endpoint_returns_ready(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_health.py -q`

预期：失败，因为应用工厂和健康路由尚不存在。

- [x] **步骤 3：添加最小应用工厂和配置**

定义包含 `environment`、`database_url`、`opensearch_url`、`object_store_endpoint`、`redis_url` 和模型网关端点的 `Settings` 字段。当前阶段让 `create_app()` 只注册健康检查路由。配置 SQLAlchemy 会话创建，但导入模块时不要建立数据库连接。

- [x] **步骤 4：添加本地服务依赖**

定义 PostgreSQL、OpenSearch、Redis 和 MinIO 的 Docker 服务，并使用命名卷。添加 `.env.example`，只包含本地非敏感默认值，不包含供应商凭证。

- [x] **步骤 5：运行重点测试和导入检查**

运行：`pytest tests/unit/test_health.py -q`

预期：通过。

运行：`python -c "from medical_rag.api.app import create_app; create_app()"`

预期：退出码为 0。

- [x] **步骤 6：提交基础工程（当前目录无 Git 仓库，未执行提交）**

```bash
git add pyproject.toml docker-compose.yml .env.example src/medical_rag tests
git commit -m "chore: bootstrap medical rag application"
```

## 任务 2：添加领域模型、数据库迁移和仓储边界

**文件：**
- 创建：`src/medical_rag/domain/enums.py`
- 创建：`src/medical_rag/domain/models.py`
- 创建：`src/medical_rag/domain/repositories.py`
- 创建：`alembic.ini`
- 创建：`alembic/env.py`
- 创建：`alembic/versions/0001_initial_schema.py`
- 创建：`tests/unit/test_domain_models.py`
- 创建：`tests/integration/test_schema.py`

**接口：**
- `TenantRepository.get(tenant_id: UUID) -> Tenant | None`.
- `PatientRepository.get(patient_id: UUID, tenant_id: UUID) -> Patient | None`.
- `DocumentRepository.get_owned(document_id: UUID, patient_id: UUID) -> Document | None`.
- `ConsentRepository.active_for(patient_id: UUID, purpose: ConsentPurpose) -> Consent | None`.
- `AuditRepository.append(event: AuditEvent) -> None`.

实体必须包括 `Tenant`、`Patient`、`Consent`、`Document`、`DocumentVersion`、`ParseJob`、`EvidenceUnit`、`MedicalFact`、`KnowledgeSource`、`KnowledgeChunk`、`ChatSession`、`ChatMessage` 和 `AuditEvent`。所有患者所属实体在适用时都必须携带 `tenant_id` 和 `patient_id`。文档及其派生记录必须携带删除状态。

- [x] **步骤 1：将模型不变量写成测试**

```python
def test_medical_fact_keeps_source_anchor():
    fact = MedicalFact(
        patient_id=patient_id,
        document_version_id=version_id,
        name="血红蛋白",
        value="105",
        unit="g/L",
        reference_range="115-150",
        source_page=2,
        source_anchor="p2:x10:y20:w100:h30",
        confidence=0.97,
    )
    assert fact.source_page == 2
    assert fact.source_anchor.startswith("p2:")
```

- [x] **步骤 2：运行模型测试并确认失败**

运行：`pytest tests/unit/test_domain_models.py -q`

预期：失败，因为领域模型尚不存在。

- [x] **步骤 3：实现枚举和 SQLAlchemy 模型**

为 `DocumentStatus`、`ParseJobStatus`、`ConsentPurpose`、`EvidenceKind`、`AuditAction` 和 `RiskClass` 使用显式枚举。为 `(tenant_id, external_user_id)`、`(document_id, version_number)` 和 `(knowledge_source_id, source_version)` 增加唯一约束。

- [x] **步骤 4：创建初始 Alembic 迁移**

创建所有表，以及面向租户 / 患者归属、文档删除状态、来源锚点和解析任务状态的索引。不要使用会隐藏删除审计事件的数据库级级联删除；删除服务代码必须枚举并记录派生数据删除。

- [x] **步骤 5：添加仓储协议和集成数据库结构测试**

集成测试必须创建租户、患者、同意记录、文档、证据单元和医学事实，然后验证归属过滤器不会返回另一个患者的文档。

- [x] **步骤 6：运行测试和迁移检查**

运行：`pytest tests/unit/test_domain_models.py tests/integration/test_schema.py -q`

预期：通过；当前使用 SQLite 临时数据库验证，PostgreSQL 由 Docker Compose 提供运行配置。

运行：`alembic upgrade head && alembic downgrade base && alembic upgrade head`

预期：所有迁移操作完成，且没有数据丢失警告。

- [x] **步骤 7：提交领域模型和数据库结构（当前目录无 Git 仓库，未执行提交）**

```bash
git add src/medical_rag/domain alembic.ini alembic tests
git commit -m "feat: add medical rag domain schema"
```

## 任务 3：实现身份、知情同意、授权和审计约束

**文件：**
- 创建：`src/medical_rag/core/errors.py`
- 创建：`src/medical_rag/auth/service.py`
- 创建：`src/medical_rag/auth/policy.py`
- 创建：`src/medical_rag/consent/service.py`
- 创建：`src/medical_rag/audit/service.py`
- 创建：`src/medical_rag/api/dependencies.py`
- 创建：`src/medical_rag/api/routes/auth.py`
- 创建：`tests/unit/test_policy.py`
- 创建：`tests/integration/test_consent_and_access.py`
- 创建：`tests/integration/test_auth_routes.py`

**接口：**
- `AccessContext(tenant_id: UUID, patient_id: UUID, user_id: UUID) -> AccessContext`.
- `can_read_document(context: AccessContext, document: Document) -> bool`.
- `require_active_consent(context: AccessContext, purpose: ConsentPurpose) -> Consent`.
- `record_external_transfer(context, document_ids: list[UUID], model_name: str, purpose: str) -> None`.

- [x] **步骤 1：编写预期失败的授权测试**

```python
def test_patient_cannot_read_another_patients_document(repo, contexts):
    assert can_read_document(contexts.patient_a, repo.document_of_patient_b) is False

def test_external_transfer_requires_current_consent(consent_service, context):
    with pytest.raises(ConsentRequired):
        consent_service.require_active_consent(context, ConsentPurpose.EXTERNAL_MODEL)
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_policy.py tests/integration/test_consent_and_access.py -q`

预期：失败，因为策略和知情同意服务尚不存在。

- [x] **步骤 3：实现策略和知情同意服务**

患者私有文档必须严格匹配租户和患者归属。向外部模型传输前，必须存在未过期、未撤回且使用当前策略版本的同意记录。被撤回的同意立即视为无效。

- [x] **步骤 4：为安全敏感路径添加审计事件**

记录登录、同意创建 / 撤回、文档访问、外部传输、检索、安全拦截、答案交付、删除请求和删除完成事件。审计载荷应保存标识符和哈希，不应保存不必要的患者原文。

- [x] **步骤 5：添加 API 依赖**

提供 `current_access_context()` 和 `require_consent_for_external_model()` 依赖。路由处理器不得根据请求体字段构造访问上下文；访问上下文必须来自已认证身份和数据库归属关系。

- [x] **步骤 6：运行测试**

运行：`pytest tests/unit/test_policy.py tests/integration/test_consent_and_access.py -q`

预期：通过，并覆盖 撤回同意和跨租户场景。

- [x] **步骤 7：提交访问边界（当前目录无 Git 仓库，未执行提交）**

```bash
git add src/medical_rag/auth src/medical_rag/consent src/medical_rag/audit src/medical_rag/api tests
git commit -m "feat: enforce patient consent and access policy"
```

## 任务 4：实现安全上传、对象存储和删除传播

**文件：**
- 创建：`src/medical_rag/storage/object_store.py`
- 创建：`src/medical_rag/storage/file_policy.py`
- 创建：`src/medical_rag/ingest/upload_service.py`
- 创建：`src/medical_rag/ingest/delete_service.py`
- 创建：`src/medical_rag/api/routes/documents.py`
- 创建：`tests/unit/test_file_policy.py`
- 创建：`tests/integration/test_document_lifecycle.py`
- 创建：`tests/integration/test_document_routes.py`

**接口：**
- `ObjectStore.put(key: str, content: BinaryIO, content_type: str) -> ObjectRef`.
- `ObjectStore.get(key: str) -> BinaryIO`.
- `ObjectStore.delete(key: str) -> None`.
- `validate_upload(filename: str, content_type: str, size_bytes: int) -> ValidatedFile`.
- `UploadService.create_document(context, file: UploadFile) -> Document`.
- `DeleteService.delete_document(context, document_id: UUID) -> DeletionReceipt`.

- [x] **步骤 1：编写预期失败的文件策略测试**

```python
def test_upload_accepts_supported_multilingual_report():
    result = validate_upload("report.pdf", "application/pdf", 2_000_000)
    assert result.extension == ".pdf"

def test_upload_rejects_unsupported_or_oversized_file():
    with pytest.raises(UnsupportedFileType):
        validate_upload("report.exe", "application/octet-stream", 100)
```

- [x] **步骤 2：运行测试并确认失败**

运行：`pytest tests/unit/test_file_policy.py -q`

预期：失败，因为校验和存储契约尚不存在。

- [x] **步骤 3：实现经过校验的存储写入**

使用包含租户、患者、文档和版本 ID 的生成对象键；绝不能使用原始文件名作为对象键。只有在扩展名、MIME 类型、文件魔数、大小和病毒扫描结果全部通过后，才能将文档标记为可用。

- [x] **步骤 4：实现上传和异步解析任务创建**

上传路由创建文档版本、保存文件、创建持久化 `ParseJob` 并发布 Worker 消息。消息发布失败时必须保留持久化任务，以便恢复，不能静默丢失任务。

- [x] **步骤 5：实现删除传播**

枚举并删除对象、解析产物、证据单元、医学事实、向量记录、缓存条目、摘要和派生索引记录。每一层都写入审计事件，并返回列出成功层和失败层的删除回执。

- [x] **步骤 6：运行生命周期集成测试**

运行：`pytest tests/unit/test_file_policy.py tests/integration/test_document_lifecycle.py -q`

预期：通过，并覆盖 重复上传、扫描失败、撤回同意、未授权下载和完整删除传播。

- [x] **步骤 7：提交文档生命周期（当前目录无 Git 仓库，未执行提交）**

```bash
git add src/medical_rag/storage src/medical_rag/ingest src/medical_rag/api/routes/documents.py tests
git commit -m "feat: add secure patient document lifecycle"
```

## 任务 5：构建多语言文档解析流水线

**文件：**
- 创建：`src/medical_rag/parsing/contracts.py`
- 创建：`src/medical_rag/parsing/adapters.py`
- 创建：`src/medical_rag/parsing/service.py`
- 创建：`src/medical_rag/parsing/worker.py`
- 创建：`tests/unit/test_parsing_service.py`
- 创建：`tests/contract/test_parser_adapters.py`

**接口：**
- `TextParser.parse(file: BinaryIO, file_type: str) -> ParsedDocument`.
- `OCRParser.parse_images(file: BinaryIO, language_hints: list[str]) -> ParsedDocument`.
- `TableParser.extract(parsed: ParsedDocument) -> list[ParsedTable]`.
- `MedicalFactExtractor.extract(evidence: list[EvidenceUnit]) -> list[MedicalFactDraft]`.
- `ParsingService.process(job_id: UUID) -> ParseResult`.

- [x] **步骤 1：使用确定性虚拟实现编写解析器契约测试**

```python
def test_parser_preserves_page_and_anchor(fake_parser, parsing_service):
    result = parsing_service.process_file(fake_pdf())
    assert result.evidence[0].page_number == 2
    assert result.evidence[0].source_anchor is not None
```

- [x] **步骤 2：运行契约测试并确认失败**

运行：`pytest tests/contract/test_parser_adapters.py tests/unit/test_parsing_service.py -q`

预期：失败，因为 parser contracts and service do not exist.

- [x] **步骤 3：实现文件类型适配器选择**

PDF 和 Word 优先使用原生文本提取，图片和扫描页使用 OCR，Excel 使用工作簿 / 工作表 / 表格提取。检测中文和英文，并将语言提示传给 OCR。必须保留原始提取文本，不能只保存规范化文本。

- [x] **步骤 4：实现证据单元持久化**

保存页码、工作表、单元格范围或坐标锚点、解析器版本、语言、置信度和来源文档版本。没有至少一个来源证据单元时，不得保存医学事实。

- [x] **步骤 5：实现带置信度门禁的医学事实抽取**

抽取名称、数值、单位、参考范围、采样时间、异常标志和来源锚点。对低置信度、单位不匹配、小数分隔符有歧义或重复字段冲突的结果拒绝入库或标记待复核。

- [x] **步骤 6：实现 Celery Worker 和任务恢复**

以持久化解析任务记录作为事实来源。按照文档版本 ID 和解析器版本实现幂等处理。保存 `started_at`、`finished_at`、重试次数、错误代码和是否需要人工复核的状态。

- [x] **步骤 7：运行解析测试**

运行：`pytest tests/unit/test_parsing_service.py tests/contract/test_parser_adapters.py -q`

预期：通过，且 deterministic fixtures for Chinese PDF, English PDF, image, Word, and Excel.

- [x] **步骤 8：提交解析流水线**

```bash
git add src/medical_rag/parsing tests
git commit -m "feat: add multilingual report parsing pipeline"
```

## 任务 6：导入经过治理的公共医学知识

**文件：**
- 创建：`src/medical_rag/knowledge/contracts.py`
- 创建：`src/medical_rag/knowledge/chunker.py`
- 创建：`src/medical_rag/knowledge/ingest_service.py`
- 创建：`tests/unit/test_knowledge_chunker.py`
- 创建：`tests/integration/test_knowledge_ingest.py`

**接口：**
- `KnowledgeSourceRegistry.register(source: KnowledgeSourceInput) -> KnowledgeSource`.
- `KnowledgeChunker.split(source: SourceDocument) -> list[KnowledgeChunkDraft]`.
- `KnowledgeIngestService.ingest(source_id: UUID, content: SourceDocument) -> IngestReceipt`.
- `KnowledgeChunk.citation() -> Citation`.

- [x] **步骤 1：编写预期失败的来源治理测试**

```python
def test_chunk_keeps_source_version_and_location(chunker):
    chunks = chunker.split(source_document_with_headings())
    assert chunks[0].source_version == "2026-08"
    assert chunks[0].location is not None
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_knowledge_chunker.py tests/integration/test_knowledge_ingest.py -q`

预期：失败，因为 source registry and chunker do not exist.

- [x] **步骤 3：实现白名单和版本检查**

只允许导入已登记且包含机构、标题、版本、发布日期、语言、适用范围、审核人和启用状态的来源。拒绝导入未知来源或已被替代的来源。

- [x] **步骤 4：实现结构感知的文档切分**

按照标题和语义段落边界切分，保留来源页码 / 章节 / URL，避免从表格行中间切分，并创建稳定内容哈希以支持幂等重新导入。

- [x] **步骤 5：只为已审核通过的分块建立索引**

通过模型契约生成 Embedding，使用来源元数据写入公共索引记录，并使来源状态和索引状态可审计。

- [x] **步骤 6：运行知识库测试**

运行：`pytest tests/unit/test_knowledge_chunker.py tests/integration/test_knowledge_ingest.py -q`

预期：通过，并覆盖 同一来源版本重复导入不会产生重复分块。

- [x] **步骤 7：提交受治理的知识导入**

```bash
git add src/medical_rag/knowledge tests
git commit -m "feat: add governed medical knowledge ingestion"
```

## 任务 7：实现公共 / 私有隔离的混合检索与权限过滤

**文件：**
- 创建：`src/medical_rag/retrieval/contracts.py`
- 创建：`src/medical_rag/retrieval/opensearch_store.py`
- 创建：`src/medical_rag/retrieval/service.py`
- 创建：`tests/unit/test_retrieval_service.py`
- 创建：`tests/integration/test_retrieval_permissions.py`

**接口：**
- `Retriever.search(query: SearchQuery, context: AccessContext) -> list[Evidence]`.
- `SearchQuery(text: str, patient_id: UUID | None, top_k: int, include_public: bool, include_private: bool)`.
- `Evidence(id: str, text: str, citation: Citation, score: float, visibility: Visibility)`.
- `RetrievalService.retrieve(query: SearchQuery, context: AccessContext) -> RetrievalResult`.

- [x] **步骤 1：编写预期失败的隔离测试**

```python
def test_private_retrieval_never_returns_another_patient(fake_index, context_a):
    result = retrieval_service.retrieve(
        SearchQuery("血红蛋白", patient_id=context_a.patient_id, top_k=10,
                    include_public=True, include_private=True),
        context_a,
    )
    assert all(item.patient_id in {None, context_a.patient_id} for item in result.items)
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_retrieval_service.py tests/integration/test_retrieval_permissions.py -q`

预期：失败，因为 retrieval contracts and permission filters do not exist.

- [x] **步骤 3：实现公共 / 私有索引映射**

使用独立的 OpenSearch 别名或索引。私有记录必须包含租户和患者过滤条件；公共记录包含来源版本和引用元数据，但不包含患者标识符。

- [x] **步骤 4：实现混合检索**

执行 BM25 和向量检索，按照确定性的评分策略合并候选结果，然后只对完成权限过滤的候选结果调用重排序模型。返回稳定的证据 ID 和引用。

- [x] **步骤 5：实现无证据处理逻辑**

当候选结果不满足分数、新鲜度、来源状态或引用要求时，返回 `RetrievalResult(items=[], reason="insufficient_evidence")`。绝不能把空结果或未授权结果伪装成证据传给 LLM。

- [x] **步骤 6：运行检索测试**

运行：`pytest tests/unit/test_retrieval_service.py tests/integration/test_retrieval_permissions.py -q`

预期：通过，并覆盖 跨租户、已删除文档、撤回同意和公共 / 私有隔离场景。

- [x] **步骤 7：提交检索模块**

```bash
git add src/medical_rag/retrieval tests
git commit -m "feat: add permission-aware hybrid retrieval"
```

## 任务 8：添加外部模型网关和供应商契约

**文件：**
- 创建：`src/medical_rag/models/contracts.py`
- 创建：`src/medical_rag/models/gateway.py`
- 创建：`src/medical_rag/models/http_adapters.py`
- 创建：`tests/unit/test_model_gateway.py`
- 创建：`tests/contract/test_model_provider_contract.py`

**接口：**
- `ChatModel.generate(request: ChatRequest) -> ChatResponse`.
- `EmbeddingModel.embed(texts: list[str]) -> list[list[float]]`.
- `RerankerModel.rank(query: str, passages: list[str]) -> list[RankedPassage]`.
- `VisionModel.extract(file_ref: ObjectRef, instruction: str) -> VisionResponse`.
- `SafetyClassifier.classify(text: str) -> SafetyResult`.
- `ModelGateway.chat(context: AccessContext, request: ChatRequest) -> ChatResponse`.

- [x] **步骤 1：编写预期失败的网关策略测试**

```python
def test_gateway_records_external_transfer_after_consent(consented_context, fake_chat):
    response = gateway.chat(consented_context, ChatRequest("解释报告", evidence=[]))
    assert response.provider == "fake"
    assert audit_log.last.action == AuditAction.EXTERNAL_MODEL_CALL

def test_gateway_blocks_patient_file_without_consent(unconsented_context):
    with pytest.raises(ConsentRequired):
        gateway.chat(unconsented_context, ChatRequest("解释报告", file_refs=[file_ref]))
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_model_gateway.py tests/contract/test_model_provider_contract.py -q`

预期：失败，因为 gateway contracts and adapters do not exist.

- [x] **步骤 3：实现与供应商无关的契约**

显式表示提示词、证据、文件引用、模型名称、策略版本、超时时间和 Trace ID。不要将供应商专用请求对象暴露给聊天编排器。

- [x] **步骤 4：实现网关约束**

外部传输前，校验当前同意、租户 / 患者归属、供应商白名单、文件类型、请求大小和用途。记录模型、供应商、策略版本、文件 ID 或证据哈希、时间戳、延迟、状态和错误类别。

- [x] **步骤 5：实现带故障策略的 HTTP 适配器**

使用 `httpx` 超时控制；对幂等的 Embedding 调用使用有上限的重试；对可能产生不安全副作用的聊天调用不自动重试；校验供应商响应并使用脱敏结构化日志。配置校验必须支持零留存和区域设置。

- [x] **步骤 6：运行网关测试**

运行：`pytest tests/unit/test_model_gateway.py tests/contract/test_model_provider_contract.py -q`

预期：通过，且 fake adapters and recorded audit events.

- [x] **步骤 7：提交模型网关（当前目录无 Git 仓库，未执行提交）**

```bash
git add src/medical_rag/models tests
git commit -m "feat: add governed external model gateway"
```

## 任务 9：实现医疗安全分流、提示词、引用和聊天编排

**文件：**
- 创建：`src/medical_rag/safety/rules.py`
- 创建：`src/medical_rag/safety/service.py`
- 创建：`src/medical_rag/chat/contracts.py`
- 创建：`src/medical_rag/chat/prompts.py`
- 创建：`src/medical_rag/chat/citations.py`
- 创建：`src/medical_rag/chat/orchestrator.py`
- 创建：`src/medical_rag/api/routes/chat.py`
- 创建：`tests/unit/test_safety_service.py`
- 创建：`tests/unit/test_citations.py`
- 创建：`tests/integration/test_chat_orchestration.py`

**接口：**
- `SafetyService.classify(question: str) -> SafetyDecision`.
- `CitationValidator.validate(answer: DraftAnswer, evidence: list[Evidence]) -> ValidatedAnswer`.
- `ChatOrchestrator.answer(context: AccessContext, question: str) -> AnswerResponse`.
- `AnswerResponse(status: str, sections: list[AnswerSection], citations: list[Citation], safety_notice: str | None)`.

- [x] **步骤 1：编写预期失败的安全和引用测试**

```python
def test_medication_change_request_is_blocked():
    decision = safety_service.classify("我可以把药量加倍吗？")
    assert decision.risk_class == RiskClass.MEDICATION_CHANGE
    assert decision.action == SafetyAction.SAFE_TEMPLATE

def test_answer_cannot_claim_unsupported_diagnosis():
    draft = DraftAnswer(text="这是癌症", cited_evidence=[])
    with pytest.raises(UnsupportedClaim):
        citation_validator.validate(draft, [])
```

- [x] **步骤 2：运行重点测试并确认失败**

运行：`pytest tests/unit/test_safety_service.py tests/unit/test_citations.py -q`

预期：失败，因为 safety, citation, and answer contracts do not exist.

- [x] **步骤 3：在检索前实现确定性的安全分流**

识别急症症状、用药变更、诊断请求、普通报告解释和一般健康教育。急症和高风险请求必须使用确定性的安全响应，不能依赖 LLM 自行发现风险。

- [x] **步骤 4：实现受证据约束的提示词**

提示词必须区分患者事实和公共医学解释。要求使用不确定性表述、提供来源引用，不进行诊断、开处方或调整剂量，并在安全决策要求时升级为紧急就医提示。

- [x] **步骤 5：实现引用校验**

校验每个被引用的证据 ID 都存在于检索结果中，校验每个医学事实引用都指向页码 / 单元格 / 坐标锚点；不受支持的高风险结论必须丢弃草稿并替换为安全模板。

- [x] **步骤 6：实现聊天编排器**

执行顺序必须是：认证访问上下文、进行安全分类、带权限过滤检索、拒绝证据不足的请求、仅在允许时调用模型网关、校验引用和安全性、持久化审计与答案，最后返回结构化响应。

- [x] **步骤 7：添加聊天 API**

提供 `POST /v1/chat/answers`，接收问题、可选文档 ID 和会话 ID。患者身份必须从认证信息中获取，绝不能把请求体中的患者 ID 作为授权来源。

- [x] **步骤 8：运行聊天测试**

运行：`pytest tests/unit/test_safety_service.py tests/unit/test_citations.py tests/integration/test_chat_orchestration.py -q`

预期：通过，并覆盖 普通解释、证据不足、急症症状、用药变更、诊断请求、引用不匹配和外部模型超时。

- [x] **步骤 9：提交安全聊天编排（当前目录无 Git 仓库，未执行提交）**

```bash
git add src/medical_rag/safety src/medical_rag/chat src/medical_rag/api/routes/chat.py tests
git commit -m "feat: add cited and safety-routed patient chat"
```

## 任务 10：添加可观测性、评估、部署和验收检查

**文件：**
- 创建：`src/medical_rag/observability/tracing.py`
- 创建：`src/medical_rag/observability/metrics.py`
- 创建：`tests/eval/cases.jsonl`
- 创建：`tests/eval/test_rag_quality.py`
- 创建：`tests/integration/test_delete_residuals.py`
- 创建：`tests/integration/test_external_failure.py`
- 创建：`Dockerfile`
- 创建：`deploy/healthcheck.yaml`
- 创建：`deploy/network-policy.yaml`
- 创建：`docs/runbooks/external-model-outage.md`
- 创建：`docs/runbooks/patient-data-deletion.md`

**接口：**
- `Evaluator.run_case(case: EvalCase) -> EvalResult`.
- `Metrics.record_retrieval(result: RetrievalResult) -> None`.
- `Metrics.record_model_call(call: ModelCallRecord) -> None`.
- `DeletionVerifier.assert_no_residuals(document_id: UUID) -> None`.

- [ ] **步骤 1：添加代表性评估用例**

创建 JSONL 评估用例，覆盖普通医学教育、患者报告解释、双语输入、PDF / 图片 / Word / Excel 事实、无依据诊断、用药变更、急症症状、跨患者访问和已删除文档检索。

- [ ] **步骤 2：编写预期失败的验收测试**

```python
def test_deleted_document_is_not_retrievable(app, seeded_deleted_document):
    result = app.retrieval.search_for_test("血红蛋白", seeded_deleted_document.patient_id)
    assert seeded_deleted_document.document_id not in {item.document_id for item in result}

def test_external_provider_timeout_returns_safe_degradation(chat_client, provider_timeout):
    response = chat_client.ask("解释我的报告")
    assert response.status_code == 503
    assert response.json()["safety_notice"] is not None
```

- [ ] **步骤 3：实现链路追踪、指标和脱敏日志**

生成请求、解析、检索、模型、安全、引用、删除和答案交付 Span。记录 P95 延迟、模型成功率、OCR 置信度、检索命中率、引用正确率、安全拦截、成本和解析重试次数。默认绝不记录患者报告原文。

- [ ] **步骤 4：实现评估运行器和删除残留验证器**

在 CI 中使用确定性夹具和虚拟模型适配器运行评估用例。残留数据验证器检查对象存储、数据库、OpenSearch、缓存、摘要和派生记录。

- [ ] **步骤 5：添加部署清单和运行手册**

记录 VPC 出站白名单、密钥注入、健康检查、Worker 扩缩容、外部模型故障行为、删除处理和审计访问方式。网络策略必须禁止应用直接访问供应商，只允许通过模型网关访问。

- [ ] **步骤 6：运行完整验证套件**

运行：`pytest -q`

预期：通过。

运行：`pytest tests/eval -q`

预期：所有预置的 安全、引用、权限和删除用例通过。

运行：`docker compose config`

预期：本地服务配置有效，且没有未解析的必需变量。

- [ ] **步骤 7：执行人工医学与安全评审**

由具备资质的医学审核人员复核固定样本的生成答案。执行跨租户访问、撤回同意、删除残留、提示词注入、供应商故障和异常文件测试。将发现记录为可跟踪缺陷，并包含严重性和复现数据。

- [ ] **步骤 8：提交运营就绪能力**

```bash
git add src/medical_rag/observability tests/eval tests/integration Dockerfile deploy docs/runbooks
git commit -m "chore: add rag evaluation and operational readiness"
```

## 执行顺序与评审检查点

必须按顺序执行任务，因为后续接口依赖前面的领域模型和策略契约：

```text
1 Bootstrap
  -> 2 Domain schema
  -> 3 Identity and consent
  -> 4 File lifecycle
  -> 5 Parsing
  -> 6 Knowledge ingestion
  -> 7 Retrieval
  -> 8 Model gateway
  -> 9 Safe chat
  -> 10 Evaluation and deployment
```

完成任务 3、5、7、8 和 9 后暂停并进行专项评审。如果发现患者数据隔离失败、外部传输绕过同意、引用无依据或高风险答案绕过安全路径，不得进入下一任务。

## 计划自检

- Spec coverage: architecture, scope, data layers, parsing, public/private retrieval, LLM gateway, safety routing, deletion, observability, evaluation, and deployment each have explicit tasks.
- Placeholder scan: no incomplete markers or unspecified implementation steps are used.
- Interface consistency: `AccessContext`, `Evidence`, `ChatRequest`, `ChatResponse`, consent checks, retrieval filters, and citation validation are named consistently across tasks.
- Scope: this plan targets the approved pilot MVP. Doctor-facing features, autonomous Agent behavior, diagnosis, prescribing, and treatment planning remain outside the plan.







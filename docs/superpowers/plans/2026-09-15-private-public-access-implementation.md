# Private and Public Document Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有医疗 RAG 中落地最小可用权限模型：私有文档仅所属者可访问，公开文档不受所属者限制。

**Architecture:** P0 复用现有 `AccessContext.patient_id` 作为当前系统的文档所属者标识，因为患者登录令牌当前将 `patient_id` 作为用户主体；不新增复杂的共享授权和角色关系。私有病历与公共知识继续走分离的索引路径，权限过滤在候选召回前执行，并在引用返回前再次执行 fail-closed 校验。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pytest, 现有 `AccessContext`、`Visibility`、`RetrievalFilters`、`IndexRecord`、`KnowledgeSource`、OpenSearch/InMemory SearchIndex。

**Spec:** 本方案对应已确认的简化权限设计；与 `docs/superpowers/specs/2026-09-15-document-quality-indexing-design.md` 的公共/私有索引分离要求保持一致。

## Global Constraints

- P0 只有两种访问状态：`private` 和 `public`。
- P0 中“所属者”统一定义为 `patient_id`；不能同时用 `user_id`、`patient_id` 判断归属。
- 私有文档缺少所属者、当前身份缺失或租户不匹配时，默认拒绝。
- 公共知识记录不得携带患者私有 ACL；公共记录的 `patient_id` 必须为空。
- 权限过滤必须发生在 BM25/向量候选召回前；生成答案和引用返回前还要再次校验。
- 删除、失活或 `retrieval_eligible=False` 的记录不可被检索。
- 不引入 `DocumentGrant`、医生关系、团队角色、字段级脱敏或复杂 Consent 扩展；这些属于后续阶段。
- 当前工作区没有 Git 仓库，不创建提交；每个任务以测试结果和变更清单验收。

---

### Task 1: 固化 Visibility 和文档所属者规则

**Files:**
- Modify: `src/medical_rag/domain/models.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/auth/policy.py`
- Create: `alembic/versions/0006_add_document_visibility.py`
- Test: `tests/unit/test_policy.py`
- Test: `tests/integration/test_schema.py`

**Interfaces:**
- Consumes: `AccessContext`, `Document`, `Visibility`, `SearchRecord`。
- Produces: `can_read_document(context, document) -> bool`、`require_document_access(...)`，以及带默认值的 `Document.visibility`。

- [ ] **Step 1: Write failing policy tests**

```python
def test_private_document_is_readable_only_by_owner() -> None:
    owner = AccessContext(tenant_id=TENANT, patient_id=PATIENT_A, user_id=uuid4())
    other = AccessContext(tenant_id=TENANT, patient_id=PATIENT_B, user_id=uuid4())
    document = make_document(patient_id=PATIENT_A, visibility=Visibility.PRIVATE)
    assert can_read_document(owner, document) is True
    assert can_read_document(other, document) is False


def test_public_document_is_readable_without_owner_match() -> None:
    context = AccessContext(tenant_id=TENANT, patient_id=PATIENT_B, user_id=uuid4())
    document = make_document(patient_id=PATIENT_A, visibility=Visibility.PUBLIC)
    assert can_read_document(context, document) is True


def test_private_document_without_owner_fails_closed() -> None:
    context = AccessContext(tenant_id=TENANT, patient_id=PATIENT_A, user_id=uuid4())
    document = make_document(patient_id=None, visibility=Visibility.PRIVATE)
    assert can_read_document(context, document) is False
```

- [ ] **Step 2: Add the additive visibility field and migration**

Add `Document.visibility` using the existing `Visibility` enum, defaulting to `Visibility.PRIVATE`, and make `patient_id` nullable only for explicitly public documents. Add a check constraint expressing the invariant `private => patient_id is not null`; keep all existing constructors valid through the private default. Add Alembic migration `0006_add_document_visibility.py` with a backfill of existing documents to `private`.

- [ ] **Step 3: Implement the single policy function**

Use exactly this decision table in `can_read_document`:

```python
if document.status == DocumentStatus.DELETED or document.deleted_at is not None:
    return False
if document.visibility == Visibility.PUBLIC:
    return True
return (
    document.visibility == Visibility.PRIVATE
    and document.patient_id is not None
    and context.tenant_id == document.tenant_id
    and context.patient_id == document.patient_id
)
```

Do not use `context.user_id` as the ownership key in this P0 implementation. Preserve `require_document_access` and its existing `AccessDenied` error.

- [ ] **Step 4: Run policy and schema tests**

Run: `python -m pytest tests/unit/test_policy.py tests/integration/test_schema.py -q`

Expected: PASS, including old ownership and deletion tests.

### Task 2: Set and propagate visibility at upload and parse time

**Files:**
- Modify: `src/medical_rag/ingest/upload_service.py`
- Modify: `src/medical_rag/parsing/service.py`
- Modify: `src/medical_rag/domain/models.py`
- Test: `tests/integration/test_document_routes.py`
- Test: `tests/integration/test_document_lifecycle.py`
- Test: `tests/unit/test_parsing_service.py`

**Interfaces:**
- Consumes: authenticated `AccessContext` and `Document.visibility`。
- Produces: private EvidenceUnit rows inheriting `tenant_id`, `patient_id`, `document_id`, `visibility=private`; public KnowledgeSource ingestion remains separate。

- [ ] **Step 1: Add upload ownership tests**

Assert that a normal patient upload creates `Document.visibility == PRIVATE` and `Document.patient_id == context.patient_id`; an upload request cannot select another patient by body/header data.

- [ ] **Step 2: Implement server-side ownership assignment**

In `UploadService.create_document`, derive `tenant_id` and `patient_id` only from `AccessContext`. Ignore any client-provided owner fields. Set `visibility=Visibility.PRIVATE` for this patient-document route. Do not assign ownership to the parser or index worker.

- [ ] **Step 3: Propagate ACL metadata during parsing**

When `ParsingService` persists `EvidenceUnit`, copy the parent document's `tenant_id`, `patient_id`, `document_id`, and visibility. If the parent is private and any owner field is missing, set the parse result to `NEEDS_REVIEW` and `retrieval_eligible=False`; do not create a searchable IndexJob.

- [ ] **Step 4: Run upload and lifecycle tests**

Run: `python -m pytest tests/integration/test_document_routes.py tests/integration/test_document_lifecycle.py tests/unit/test_parsing_service.py -q`

Expected: PASS; existing parse retry and document deletion behavior remains unchanged.

### Task 3: Enforce ACL filters before lexical and vector recall

**Files:**
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Modify: `src/medical_rag/retrieval/service.py`
- Modify: `src/medical_rag/retrieval/indexing.py`
- Test: `tests/unit/test_retrieval_service.py`
- Test: `tests/integration/test_retrieval_permissions.py`

**Interfaces:**
- Consumes: `AccessContext`, `SearchQuery`, `RetrievalFilters`, and `SearchRecord`。
- Produces: identical public/private ACL filters for BM25, vector, in-memory fallback, and citation output。

- [ ] **Step 1: Write failing pre-recall filter tests**

```python
def test_private_filter_contains_current_patient_before_search() -> None:
    filters = filters_for_context(context_for(PATIENT_A), include_public=True, include_private=True)
    assert filters.patient_id == PATIENT_A
    assert filters.include_private is True


def test_other_patient_private_record_never_reaches_fusion() -> None:
    result = retrieval_with_records(public_record(), private_record(patient_id=PATIENT_B)).retrieve(
        SearchQuery("血红蛋白", patient_id=None), context_for(PATIENT_A)
    )
    assert all(item.patient_id != PATIENT_B for item in result.items)
```

- [ ] **Step 2: Build filters exclusively from AccessContext**

`RetrievalService._retrieve_hybrid` must construct `RetrievalFilters` from `context.tenant_id` and `context.patient_id`; a requested `SearchQuery.patient_id` may only be `None` or the current context patient. Any other value returns `unauthorized_scope` without searching.

- [ ] **Step 3: Apply the same OpenSearch filter to both paths**

The OpenSearch filter must be:

```text
(visibility = public AND patient_id is null)
OR (visibility = private AND tenant_id = context.tenant_id AND patient_id = context.patient_id)
```

Also require `deleted=false`, `source_active=true`, and `retrieval_eligible=true`. Apply it in the OpenSearch query body before `knn` or `match` execution. Keep the in-memory `_is_authorized` check as defense in depth.

- [ ] **Step 4: Re-check citation records**

Before returning `Evidence`, re-evaluate `_is_authorized` and require a complete citation. A record that changes status between candidate search and response is dropped rather than returned.

- [ ] **Step 5: Run retrieval permission tests**

Run: `python -m pytest tests/unit/test_retrieval_service.py tests/integration/test_retrieval_permissions.py -q`

Expected: PASS; unauthorized private content is absent from lexical candidates, vector candidates, fused results, and citations.

### Task 4: Make IndexWriter and public/private mappings fail closed

**Files:**
- Modify: `src/medical_rag/retrieval/indexing.py`
- Modify: `src/medical_rag/retrieval/contracts.py`
- Modify: `src/medical_rag/knowledge/ingest_service.py`
- Modify: `src/medical_rag/retrieval/opensearch_store.py`
- Test: `tests/contract/test_index_record_contract.py`
- Test: `tests/integration/test_private_indexing.py`
- Test: `tests/integration/test_knowledge_ingest.py`

**Interfaces:**
- Consumes: EvidenceUnit/KnowledgeChunk rows and `IndexRecord`。
- Produces: private records with owner metadata and public records with no patient ACL。

- [ ] **Step 1: Add writer validation tests**

```python
def test_private_record_requires_patient_owner() -> None:
    with pytest.raises(ValueError):
        IndexRecord(id="x", text="text", visibility="private", tenant_id=TENANT)


def test_public_record_rejects_patient_acl() -> None:
    with pytest.raises(ValueError):
        IndexRecord(id="x", text="text", visibility="public", patient_id=PATIENT_A)
```

- [ ] **Step 2: Validate records before embedding and upsert**

Private writers reject missing `tenant_id` or `patient_id`, inactive/deleted records, and `retrieval_eligible=False`. Public writers reject any `tenant_id` or `patient_id` and always emit `visibility=public`. Validation happens before embedding so forbidden content is not sent to the embedding provider.

- [ ] **Step 3: Keep public knowledge ingestion globally readable**

`KnowledgeIngestService` creates only public records, with `patient_id=None`, `tenant_id=None`, `visibility=public`, `source_active=True`, `deleted=False`, and `retrieval_eligible=True`. The existing source-review requirement remains the gate for publication, not a patient ownership check.

- [ ] **Step 4: Verify OpenSearch mappings**

Map `visibility`, `tenant_id`, `patient_id`, `deleted`, `source_active`, and `retrieval_eligible` as exact-match keyword/boolean fields. Do not rely on analyzed text fields for ACL decisions.

- [ ] **Step 5: Run writer and ingest tests**

Run: `python -m pytest tests/contract/test_index_record_contract.py tests/integration/test_private_indexing.py tests/integration/test_knowledge_ingest.py -q`

Expected: PASS; public/private records remain separated and invalid ACL payloads fail before writing.

### Task 5: Harden authentication context and document endpoints

**Files:**
- Modify: `src/medical_rag/auth/service.py`
- Modify: `src/medical_rag/api/routes/documents.py`
- Modify: `src/medical_rag/ingest/delete_service.py`
- Modify: `src/medical_rag/domain/repositories.py`
- Test: `tests/integration/test_auth_routes.py`
- Test: `tests/integration/test_document_routes.py`
- Test: `tests/integration/test_delete_residuals.py`

**Interfaces:**
- Consumes: JWT-derived `AccessContext`。
- Produces: document read/download/delete operations that all use `require_document_access`。

- [ ] **Step 1: Add endpoint authorization tests**

Cover owner read/download/delete success, another patient failure, public document read success, deleted document failure, and a forged `patient_id` request being rejected.

- [ ] **Step 2: Make JWT the production source of identity**

When a Bearer token is present, derive tenant and patient only from verified claims and database lookup. Keep header-based context only behind the existing test/development path; production routes must not trust a client-supplied `X-Patient-ID` to select ownership.

- [ ] **Step 3: Apply the policy to every document operation**

Call `require_document_access` before reading metadata, downloading the object, deleting, or creating a reparse job. Repository helpers must include ownership predicates in SQL (`tenant_id` and `patient_id`) instead of loading arbitrary documents first.

- [ ] **Step 4: Run endpoint and deletion tests**

Run: `python -m pytest tests/integration/test_auth_routes.py tests/integration/test_document_routes.py tests/integration/test_delete_residuals.py -q`

Expected: PASS; no endpoint can expose another owner's document or a deleted record.

### Task 6: End-to-end permission regression and release gate

**Files:**
- Modify: `tests/integration/test_p0_rollout.py`
- Modify: `tests/integration/test_retrieval_permissions.py`
- Modify: `tests/integration/test_private_indexing.py`
- Create: `tests/eval/test_access_control_quality.py`
- Modify: `src/medical_rag/observability/metrics.py`

**Interfaces:**
- Consumes: upload, parse, index, retrieval, citation, and deletion flows。
- Produces: deterministic access-control regression report。

- [ ] **Step 1: Add the end-to-end owner/public scenario**

Create two patient contexts, one private document owned by patient A, and one public knowledge source. Assert that A sees both, B sees only the public source, and neither sees deleted content.

- [ ] **Step 2: Add forbidden-path assertions**

Assert that unauthorized private records are absent from lexical candidates, vector candidates, RRF fusion, reranking input, final citations, and chat context. Assert that a missing owner or malformed visibility payload is rejected.

- [ ] **Step 3: Add safety metrics**

Record `unauthorized_candidate_count`, `unauthorized_result_count`, `deleted_result_count`, and `public_records_with_private_acl`. The release gate requires all four to be zero.

- [ ] **Step 4: Run the full verification suite**

Run: `python -m pytest -q`

Run: `python -m ruff check src tests`

Expected: all tests pass, static checks pass, and access-control safety metrics are zero.

## Coverage and Consistency Review

- Minimal private/public semantics are covered by Tasks 1 and 4.
- Ownership propagation from upload through EvidenceUnit and IndexRecord is covered by Task 2.
- Pre-recall ACL filtering and citation re-checks are covered by Task 3.
- Public knowledge visibility is covered by Task 4.
- Authentication and document endpoint enforcement are covered by Task 5.
- End-to-end safety and measurable zero-violation criteria are covered by Task 6.

## Plan Self-Review

- No unfinished markers or vague deferred instructions are present.
- The ownership key is consistent everywhere: P0 uses `patient_id`.
- The plan does not add sharing, role hierarchy, or consent complexity beyond the approved two-state model.
- Existing public/private index separation and current retrieval interfaces are preserved through additive validation and filters.


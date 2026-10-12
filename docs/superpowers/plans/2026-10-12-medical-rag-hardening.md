# Medical RAG 安全、交付与质量加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将医疗 RAG 项目推进到可重复构建、可审计、可真实复评的状态，并消除当前已确认的凭据、PHI、部署一致性和上传安全阻塞项。

**Architecture:** 按安全收敛 → 交付恢复 → Compose 一致性 → 上传安全与审计 → 医疗质量复评顺序推进。每个阶段先建立可审查证据，再修改实现；生产路径全部采用 fail-closed，历史评测数字不作为当前通过依据。

**Tech Stack:** Python 3.12/3.13、FastAPI、SQLAlchemy 2、Alembic、OpenSearch、Redis/Celery、Docker Compose、Vue/Vite、pytest、Ruff。

**Spec:** `docs/superpowers/specs/2026-10-12-medical-rag-hardening-design.md`

## Global Constraints

- 不把真实密钥、患者正文、数据库或私有索引写入 Git、镜像或评测产物。
- 未经服务侧撤销确认的旧凭据不得继续用于任何真实请求。
- 无法恢复的前端源码和完整测试不得用空壳文件冒充恢复成功。
- 生产环境禁止 `AllowAllVirusScanner`，未配置扫描器时上传必须失败。
- 每个“通过”结论都必须有当前命令输出、构建日志或新生成评测 JSON 支撑。
- 医疗质量评测不宣称诊断或治疗有效性，任何规则判定的数值矛盾都阻断生产结论。

---

### Task 1: 凭据轮换与 PHI 共享目录收敛

**Files:**
- Modify: `RAG/.env`（只保留本机临时占位或由密钥管理器注入的值）
- Modify: `RAG/.env.example`（补齐所需变量名和安全注释）
- Modify: `RAG/.gitignore`（确认数据库、备份、缓存和本地模型全部排除）
- Create: `RAG/docs/security/2026-10-12-credential-rotation-receipt.md`

**Interfaces:**
- Consumes: 当前 `.env` 中的模型网关、OCR、对象存储、数据库、Redis、JWT 配置；共享目录中的数据库和备份文件。
- Produces: 已撤销旧凭据的服务侧确认、脱敏后的本地配置、共享目录清理清单和哈希收据。

- [ ] **Step 1: 建立不含秘密的暴露清单**

  在项目根目录运行：

  ```powershell
  rg -n "MODEL_GATEWAY_API_KEY|OCR_PASSWORD|OBJECT_STORE_SECRET_KEY|POSTGRES_PASSWORD|REDIS_PASSWORD|JWT_SECRET" . -g '!node_modules/**' -g '!dist/**' -g '!*.jsonl'
  Get-ChildItem -Recurse -File -Include *.db,*.db.bak,*.sqlite,*.sqlite3 | Select-Object FullName,Length
  ```

  将输出保存到内存或受控临时位置，收据中只记录文件路径、变量名和 SHA-256，不记录值。

- [ ] **Step 2: 在外部服务侧撤销并生成替代凭据**

  使用管理员入口依次撤销模型网关 API key、OCR 密码、对象存储密钥、数据库/Redis 密码和 JWT secret；生成至少 32 字节随机 JWT secret。记录每个服务的撤销时间和生效时间，不把新值写入本计划或 Git。

- [ ] **Step 3: 隔离共享目录中的 PHI 文件**

  创建项目外的受控隔离目录；将 `medical_rag.db`、`medical_rag.db.bak`、`backups/`、`.faithfulness_phi_cache/` 和任何私有索引导出移出共享目录。完成哈希收据后删除共享副本；如果删除失败，记录具体路径并阻断后续“已清理”结论。

- [ ] **Step 4: 收紧开发环境的明文 PHI 出口**

  将 `MODEL_GATEWAY_URL`、`OCR_SERVICE_URL`、`OPENSEARCH_URL` 和 `OBJECT_STORE_ENDPOINT` 改为 HTTPS 或回环地址；真实患者数据联调时，远程明文端点必须由 `Settings.validate_runtime()` 拒绝，而不是只写日志。

- [ ] **Step 5: 验证并提交收据**

  运行：

  ```powershell
  rg -n "V5PB|12345678|MODEL_GATEWAY_API_KEY=|OCR_PASSWORD=|JWT_SECRET=" .env .env.example docs -g '!docs/security/2026-10-12-credential-rotation-receipt.md'
  Get-ChildItem -Recurse -File -Include *.db,*.db.bak,*.sqlite,*.sqlite3
  git diff --check
  ```

  预期：扫描不到真实值，项目共享目录没有数据库/备份，`git diff --check` 返回 0。确认后提交：

  ```powershell
  git add .env.example .gitignore docs/security/2026-10-12-credential-rotation-receipt.md
  git commit -m "security: rotate credentials and remove shared phi artifacts"
  ```

---

### Task 2: 前端源码与完整测试资产恢复

**Files:**
- Inspect: `.git/`, `.git-broken-2026-10-12/`, `.i18n_backup_src_medical_rag/`, `frontend/dist/`, `frontend/` source maps and external backup locations available in the workspace
- Restore: `frontend/src/**` when an original copy is found
- Restore: missing `tests/**` when an original copy is found
- Create: `docs/recovery/2026-10-12-source-recovery-receipt.md`

**Interfaces:**
- Consumes: Git object database, tracked-file index, source maps and any user-provided original archive.
- Produces: Restored source tree or an explicit blocked receipt listing missing paths and build impact.

- [ ] **Step 1: Compare Git inventory with disk inventory**

  Run:

  ```powershell
  git fsck --full --no-reflogs
  git ls-files frontend/src tests src | Sort-Object
  rg --files frontend tests src | Sort-Object
  ```

  Save counts and path differences in the recovery receipt.

- [ ] **Step 2: Search recoverable copies before reconstructing anything**

  Search `.git/objects`, `.git-broken-2026-10-12/`, `.i18n_backup_src_medical_rag/`, source maps under `frontend/dist/assets`, `.workbuddy*`, and any explicitly supplied archive. For each candidate file, verify its hash against the Git blob or a known build artifact before copying it.

- [ ] **Step 3: Restore only verified originals**

  Restore `frontend/src` and missing test files while preserving import paths and existing API contracts. Do not create placeholder components or tests. If a source map contains only minified output and no reliable original mapping, record it as non-recoverable.

- [ ] **Step 4: Verify build and test asset completeness**

  Run in `frontend/`:

  ```powershell
  npm ci
  npm run build
  npm test -- --run
  ```

  Run in `RAG/` after Python dependencies are installed:

  ```powershell
  python -m pytest -q
  ```

- [ ] **Step 5: Commit only verified restoration**

  Commit restored files and the receipt separately from later behavior changes:

  ```powershell
  git add frontend/src tests docs/recovery/2026-10-12-source-recovery-receipt.md
  git commit -m "chore: restore verified frontend and test assets"
  ```

---

### Task 3: Compose、Dockerfile 与 Settings 配置契约对齐

**Files:**
- Modify: `docker-compose.yml`
- Modify: `Dockerfile`
- Modify: `.env.example`
- Modify: `src/medical_rag/core/config.py`
- Modify: `src/medical_rag/api/app.py`
- Create: `tests/test_runtime_composition_contract.py`

**Interfaces:**
- Consumes: `Settings`, `load_calibration_artifact()`, local embedding model path, Docker Compose variable interpolation.
- Produces: 容器内可读的 `config/`、显式传递的标定和模型配置、失败即停的启动契约，以及配置回归测试。

- [ ] **Step 1: Write failing configuration contract tests**

  Add tests that assert: `CALIBRATION_ARTIFACT_PATH` is present in the Compose environment, `config/calibration/cosine-gate-2026-09-26.json` is copied or mounted, local embedding mode has a non-empty model path, API port binds to loopback by default, and unknown security configuration is rejected or explicitly warned.

- [ ] **Step 2: Run the contract tests to capture the current failures**

  Run:

  ```powershell
  python -m pytest tests/test_runtime_composition_contract.py -q
  ```

  Record the failing assertions before changing implementation.

- [ ] **Step 3: Align Compose environment and volumes**

  Add explicit `CALIBRATION_ARTIFACT_PATH`, `MODEL_REQUEST_TIMEOUT_SECONDS`, `RAG_TEXT_ANALYZER`, `RAG_SEARCH_ANALYZER`, and local model variables to the shared environment. Mount or copy `config/` and `models/` read-only. Bind API port as `127.0.0.1:8000:8000` unless an operator explicitly overrides it.

- [ ] **Step 4: Align Dockerfile contents and startup checks**

  Copy `config/` into `/app/config`; document the model directory as a read-only runtime mount rather than silently relying on a missing path. Keep non-root execution. Make startup fail when a configured calibration artifact or local model path is unavailable.

- [ ] **Step 5: Remove silent security configuration drift**

  Add a typed field or an explicit rejection path for `ALLOW_PLAINTEXT_PHI_EGRESS` and other security-sensitive variables. Wire `model_request_timeout_seconds` into the chat adapter/request construction, preserving the orchestrator total budget.

- [ ] **Step 6: Run build and runtime contract verification**

  Run:

  ```powershell
  docker compose --env-file .env.example config
  python -m pytest tests/test_runtime_composition_contract.py -q
  python -m py_compile $(Get-ChildItem src -Recurse -Filter *.py)
  ```

  Expected: Compose renders without missing required variables when supplied through a documented local env file; all contract tests pass; compilation returns 0.

- [ ] **Step 7: Commit deployment consistency changes**

  ```powershell
  git add docker-compose.yml Dockerfile .env.example src/medical_rag/core/config.py src/medical_rag/api/app.py tests/test_runtime_composition_contract.py
  git commit -m "fix: align container configuration and retrieval gate startup"
  ```

---

### Task 4: 生产上传病毒扫描与审计闭环

**Files:**
- Modify: `src/medical_rag/domain/enums.py`
- Modify: `src/medical_rag/ingest/upload_service.py`
- Modify: `src/medical_rag/api/routes/documents.py`
- Modify: `src/medical_rag/core/config.py`
- Modify: `src/medical_rag/domain/models.py`
- Create: `alembic/versions/0010_document_upload_audit.py`
- Create: `tests/test_upload_security_and_audit.py`

**Interfaces:**
- Consumes: `UploadService`, `AuditService`, `AccessContext`, object-store transaction behavior and `VirusScanner` protocol.
- Produces: `DOCUMENT_UPLOADED` audit action, production scanner requirement, rollback-safe upload behavior, migration and regression tests.

- [ ] **Step 1: Write failing upload security tests**

  Cover these exact cases: production upload without a configured scanner raises before object-store persistence; scanner rejection leaves no document/version/job and deletes any staged object; successful upload records one `DOCUMENT_UPLOADED` event containing tenant, patient, document and version identifiers without file text; local development may use an explicit no-op scanner only when the environment is `local/dev/test`.

- [ ] **Step 2: Run the tests and verify the current unsafe behavior is exposed**

  ```powershell
  python -m pytest tests/test_upload_security_and_audit.py -q
  ```

- [ ] **Step 3: Add the audit action and migration**

  Add `AuditAction.DOCUMENT_UPLOADED`. Create migration `0010_document_upload_audit.py` only for indexes or schema changes needed by audit queries; keep event payload metadata free of raw file content. Add a composite audit index for `(tenant_id, patient_id, resource_type, resource_id, created_at)` if the current dialect supports it.

- [ ] **Step 4: Make scanner selection explicit and fail-closed**

  Add a production scanner adapter selected from Settings. Keep `AllowAllVirusScanner` available only through an explicit development setting. The document route must obtain the configured scanner through application state or dependency injection; it must never rely on `UploadService`'s default in production.

- [ ] **Step 5: Record upload audit after durable success**

  Record `DOCUMENT_UPLOADED` only after validation, scanning, object-store write, database flush and job publication succeed. If publication fails, roll back the database and object-store write and emit no success audit event.

- [ ] **Step 6: Run focused tests, migration checks and compile checks**

  ```powershell
  python -m pytest tests/test_upload_security_and_audit.py -q
  alembic upgrade head
  alembic downgrade -1
  alembic upgrade head
  python -m py_compile $(Get-ChildItem src alembic -Recurse -Filter *.py)
  ```

- [ ] **Step 7: Commit upload hardening**

  ```powershell
  git add src/medical_rag/domain/enums.py src/medical_rag/ingest/upload_service.py src/medical_rag/api/routes/documents.py src/medical_rag/core/config.py src/medical_rag/domain/models.py alembic/versions/0010_document_upload_audit.py tests/test_upload_security_and_audit.py
  git commit -m "security: require upload scanning and audit document ingestion"
  ```

---

### Task 5: 医疗质量评测、临床复核与交付报告

**Files:**
- Modify: `scripts/retrieval_tuning_lab.py` only when current CLI cannot run the required matrix
- Create: `eval_out/medical_rag_hardening_2026-10-12/`
- Create: `docs/medical-rag-hardening-validation-2026-10-12.md`

**Interfaces:**
- Consumes: frozen evaluation datasets, calibration artifact, OpenSearch indexes, model gateway, current safety/citation/faithfulness evaluators.
- Produces: reproducible evaluation manifest, retrieval/rejection/citation/faithfulness/security results, clinician review queue and final release decision.

- [ ] **Step 1: Install and verify the declared Python environment**

  Run from `RAG/`:

  ```powershell
  python -m pip install -e ".[dev]"
  ruff check src tests
  python -m pytest -q
  ```

  Record dependency versions and every failure; do not reuse historical pass counts.

- [ ] **Step 2: Run offline gate and safety tests**

  ```powershell
  python -m pytest tests/test_rejection_gate.py tests/test_startup_gate_selfcheck.py -q
  python scripts/retrieval_tuning_lab.py --selftest --out eval_out/medical_rag_hardening_2026-10-12/offline
  ```

  Verify the gate-enabled configuration rejects unrelated queries while preserving the known quality cases.

- [ ] **Step 3: Start dependencies and run real retrieval evaluation**

  ```powershell
  docker compose up -d postgres opensearch redis minio
  docker compose --profile ops run --rm index-init
  python scripts/retrieval_tuning_lab.py --rounds 25 --out eval_out/medical_rag_hardening_2026-10-12/real
  ```

  Capture dataset SHA-256, index names/versions, embedding model and dimensions, calibration artifact hash, latency, and rejection accuracy.

- [ ] **Step 4: Run answer-level citation and faithfulness checks**

  Re-run the current citation validator and faithfulness evaluator against the new answer set. Separate rule-judged claims, model-judged claims, abstentions and clinician-reviewed contradictions. Any numeric contradiction is included in the release-blocking section.

- [ ] **Step 5: Run authorization and deletion residual checks**

  Verify cross-patient retrieval is empty, revoked consent blocks external model calls, deleted documents disappear from private and public indexes, and diagnostics contain no PHI正文.

- [ ] **Step 6: Produce the release decision**

  Write `docs/medical-rag-hardening-validation-2026-10-12.md` with command lines, versions, dataset hashes, raw output paths, confidence intervals, failed examples, clinician review status and one of `BLOCKED`, `RESEARCH_DEMO_ONLY`, or `RELEASE_CANDIDATE`. Do not use `RELEASE_CANDIDATE` while any P0 remains unresolved.

- [ ] **Step 7: Commit reproducible evaluation metadata**

  Commit only manifests, summaries and redacted diagnostics; do not commit answer bodies, patient evidence, raw databases or credentials:

  ```powershell
  git add docs/medical-rag-hardening-validation-2026-10-12.md eval_out/medical_rag_hardening_2026-10-12
  git commit -m "eval: record medical rag hardening validation"
  ```

## Final Verification Checklist

- [ ] `git status --short` contains no unreviewed secret, database, backup or PHI artifact.
- [ ] `docker compose --env-file .env.example config` succeeds with documented local secret injection.
- [ ] Backend compilation, Ruff and the restored pytest suite have fresh outputs.
- [ ] Frontend build succeeds from `frontend/src` rather than relying on stale `dist/`.
- [ ] Production upload without a scanner fails closed and successful upload emits an audit event.
- [ ] Gate-enabled real evaluation reports rejection accuracy and no calibration mismatch.
- [ ] Faithfulness and numeric contradiction results have clinician review status.
- [ ] Final release decision is supported by files in `eval_out/medical_rag_hardening_2026-10-12/`.

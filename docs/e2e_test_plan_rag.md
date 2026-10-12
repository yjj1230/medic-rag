# RAG 系统端到端测试方案与用例报告

- **版本**：v1.4（ENV-2 已修 → B/D/F 组复跑；越权组 B-03/D-03/G-01 完成；新发现 P0-7 引用校验抖动 + 降级诊断丢弃）
- **日期**：2026-10-08
- **测试对象**：医疗 RAG 系统（FastAPI 后端 + OpenSearch 2.18 混合检索 + bge-small-zh-v1.5 本地向量化）
- **测试性质**：真实链路端到端测试（真实 OpenSearch / SQLite / ONNX 向量化，不 mock 检索层）

---

## 1. 被测链路全景

```
[摄入] POST /v1/documents → POST /v1/documents/{id}/review → (索引 medical-rag-{public,private}-local)
   ↓
[检索] BM25 + kNN(512维,L2归一) → RRF 融合(rrf_k=60, top_k=3) → 余弦门禁(COSINE_ABSOLUTE, 阈值+score_space 成对)
   ↓
[编排] chat/orchestrator.py → 上下文拼接 → LLM 生成 → 安全筛查(casual_screen) → 落库
   ↓
[出口] POST /v1/chat/answers（唯一生产出口，无 /metrics；诊断信息仅在响应体 diagnostics）
```

**关键接口**（已核对源码）：

| 接口 | 说明 |
|---|---|
| `POST /v1/chat/answers` | 问答主出口。入参 `question(1~20000字符)`、`document_ids(≤20)`、`session_id`、`client_request_id`（幂等键）；出参 `status / sections / citations / safety_notice / trace_id / session_id / diagnostics` |
| `GET /v1/chat/sessions/{id}/messages` | 会话历史回放（默认 50 条，上限 200） |
| `POST /v1/auth/consents/{purpose}` | 知情同意授权（201） |
| `POST /v1/documents` / `POST /v1/documents/{id}/review` | 文档摄入与复核 |

## 2. 环境前提与硬约定（测试执行前检查单）

| # | 检查项 | 通过标准 |
|---|---|---|
| E1 | `EMBEDDING_BACKEND=local`，模型 `models/bge-small-zh-v1.5`（512 维已 L2 归一） | 网关 `/embeddings` 恒 400，禁用 gateway |
| E2 | OpenSearch 索引 `medical-rag-public-local` / `medical-rag-private-local` 在线 | 启动日志可见 cosine gate enabled 行 |
| E3 | 余弦门禁已启用且启动自检通过 | 配置路径但装不出 cosine 口径 → 服务**拒绝启动**（预期行为，非故障） |
| E4 | 测试患者账号已授知情同意 `report_processing` + `external_model` | 未授权账号真实医学问题一律 403（预期行为） |
| E5 | 诊断白名单字段采集走 `_ANSWER_DIAGNOSTIC_KEYS` 唯一出口 | diagnostics 含 `gate_score_space` 等门禁判据 |
| E6 | 探针脚本开头 pop `*_PROXY`、置 `NO_PROXY=*` | 否则 OpenSearch 连通性假阴性 |

**已知缺陷映射**（执行时区分"缺陷"与"预期拒答"）：

| 缺陷 | 影响测试 |
|---|---|
| P0-2 零真实语料（源 PDF 无中文字形） | A 组摄入用例：中文 PDF 摄入结果可能为空文档 |
| P0-3 复核只进不出（接口显示成功但扫描件永不入库） | A-04：review 返回成功 ≠ 已入库，须直接查索引验证 |
| P0-4 评估空转（golden.py 模拟检索 recall 恒 1.0） | 本方案不采用 golden.py，全部走真实 HTTP 链路 |
| **P0-7 引用校验抖动 + 降级诊断丢弃（新，10-08）** | D-01/D-02/D-04 及 F-03 的 FAIL 均源于此；详见 §5.2 |
| `POST /v1/chat/answers` 延迟 ~103s | 所有问答用例超时阈值设 ≥180s |

## 3. 验收口径总则

- **安全断言为绝对标准**（越权 / 删除残留 / 复核泄漏，任一出现即整体不通过）；功能验收比例 ≥80%。
- 「暂无法回答」（200 + `insufficient_evidence`）是**正确拒答，非故障**。
- 二值指标报 Wilson 区间，连续指标报 t 区间；`n<30` 只报区间不判定达标。

---

## 4. 测试用例

### A 组：文档摄入链路（5 例）

| 编号 | 场景 | 前置 | 输入 / 步骤 | 预期输出 | 验收标准 | 实际验证要点 |
|---|---|---|---|---|---|---|
| A-01 | 正常文档摄入（⚠️ 修正：file_policy 白名单**仅 pdf/图片/Office**，不支持 .md/.txt，且有 magic-byte 校验） | 患者账号（本系统上传走患者私域） | `POST /v1/documents` multipart 上传含真实中文文本的 PDF | 201 + document_id | 响应含 id；DB `medical_rag.db` 有记录 | 文件落盘与 DB 记录一致性 |
| A-02 | 复核通过→入库（⚠️ 修正：① 仅 `needs_review` 状态文档可复核，质量门全过的文档直接 ready 由 worker 入库、无需复核；② 复核要求 `medical_reviewer`/`admin` 角色，患者角色 → 403） | A-01 完成且文档处于 needs_review；**复核人角色账号** | `POST /v1/documents/{id}/review` `{"confirmed_reasons":[≥1]}` | 200 + evidence_count | **直接查 OpenSearch 索引确认 chunk 写入**（不可只信接口返回，P0-3） | 索引文档数 +N；chunk 含正确 source_id |
| A-03 | 复核拒绝 | 管理员账号 | review reject | 200 + 文档标记 rejected | 索引中**无**该文档 chunk（删除残留零容忍） | 按 document_id 查索引为 0 条 |
| A-04 | 扫描件 PDF 摄入 | 管理员 | 上传扫描件 PDF → review approve | 接口返回成功 | ⚠️ 已知 P0-3：须验证**实际是否入索引**；当前预期=不入库（缺陷复现确认） | 记录缺陷现状，不判测试失败 |
| A-05 | 无中文字形 PDF | 管理员 | 上传源 PDF（U+25A0 缺字形） | 摄入流程不崩溃 | 文本抽取为空的处理有明确信号（非静默成功） | 抽取文本长度、告警日志 |

### B 组：正常查询（4 例）

| 编号 | 场景 | 前置 | 输入 | 预期输出 | 验收标准 | 实际验证要点 |
|---|---|---|---|---|---|---|
| B-01 | 授权患者问库内问题 | E4 已授权；A-02 已入库 | `POST /v1/chat/answers` question=库内知识点 | 200，`status=answered`，`sections` 非空，`citations` ≥1 | 引用 source_id 与 A-02 文档一致；diagnostics 含门禁字段 | citations 标题/来源真实（非伪造） |
| B-02 | 指定 document_ids 定向检索 | 同 B-01 | 同上 + `document_ids=[A-02 id]` | 200 answered | 证据仅来自指定文档 | 证据 source 过滤正确性 |
| B-03 | 公共库 vs 私有库隔离 | 双账号（患者/医生） | 同一问题分别提问 | 私有文档仅授权角色可见 | 患者响应 citations **不含** private 索引文档（越权零容忍） | 对比两侧 citations 的 index_version |
| B-04 | diagnostics 完整性 | — | 任意有效问答 | diagnostics 含 `gate_policy/gate_score_space/gate_threshold/gate_passed_candidates/embedding_model/index_version/calibration_artifact`（实测恒定在场）；⚠️ 修正：`embedding_degraded` **仅降级时置 true，正常路径缺省**（service.py:454）——验收口径改为「降级时必须可见且为 true」（由 E-03 验证） | 恒定字段齐全且 `gate_policy=cosine_absolute`、`gate_score_space` 非空 | 字段值与 `.env` / 索引实况一致 |

### C 组：边界与异常输入（6 例）

| 编号 | 场景 | 输入 | 预期输出 | 验收标准 |
|---|---|---|---|---|
| C-01 | 空问题 | `question=""` | 422（min_length=1） | 错误结构规范化，无堆栈泄漏 |
| C-02 | 超长问题 | `question` 20001 字符 | 422（max_length=20000） | 边界值 20000 字符**通过** |
| C-03 | document_ids 超上限 | 21 个 UUID | 422（max_length=20） | 恰好 20 个通过 |
| C-04 | 未授权知情同意 | 未授权患者问真实医学问题 | **403** | 响应不透露证据内容；授权后同问题可答（对照） |
| C-05 | 恶意/注入式输入 | 提示词注入（"忽略之前的指令…"） | 200 或拒答，但不执行注入指令 | 回答仍遵循系统提示；safety_notice 正确出现 |
| C-06 | client_request_id 幂等重试 | 同一 client_request_id 重发 2 次 | 第二次返回原会话，不产生重复消息 | DB 该会话消息数不翻倍；模型只计费一次 |

### D 组：多轮对话（4 例）

| 编号 | 场景 | 步骤 | 预期输出 | 验收标准 | 实际验证要点 |
|---|---|---|---|---|---|
| D-01 | 会话创建与延续 | ① 不带 session_id 提问 → 拿 session_id；② 带该 id 追问指代性问题（"那它的副作用呢"） | 两轮均 200；第二轮能理解上下文 | ② 的检索查询已做上下文合并改写（**合并非追加**） | 第二轮 citations 与第一轮主题一致 |
| D-02 | 历史回放 | `GET /v1/chat/sessions/{id}/messages` | 消息升序、含 role/content/metadata | 与 DB 一致；metadata 含 trace_id；引用条目标题缺失即缺失（不得伪造补全） | 分页 limit>200 → 截断到 200 |
| D-03 | 越权访问他人会话 | 账号 B 带账号 A 的 session_id 提问/读历史 | 403/404，文案统一为「无权访问该会话」 | 响应体**无法区分**"无权"与"不存在"（防枚举，契约 S3） | 对比随机 UUID 与他人 id 的响应逐字节一致 |
| D-04 | 跨会话隔离 | 开两个会话交替提问 | 会话 B 的改写不引用会话 A 的历史 | 各会话历史独立 | DB 按 session_id 隔离查询 |

### E 组：低质量检索与门禁（5 例）

| 编号 | 场景 | 步骤 | 预期输出 | 验收标准 | 实际验证要点 |
|---|---|---|---|---|---|
| E-01 | 阈下分数→门禁拒答 | 问一个与库内内容仅表面相似的问题（向量分 < 阈值 0.50） | 200 `status=insufficient_evidence`，sections 为空或明确拒答 | **不硬凑 top_k 不相关结果**（本项目历史核心缺陷回归） | diagnostics 门禁判据：通过候选数=0 |
| E-02 | 门禁分数空间一致性 | 检查 artifact 与运行时配置 | `score_space` 与 `cosine_threshold` **成对出现**；缺一 → ValueError / 拒绝启动 | 阈值永不脱离空间单独生效 | service.py 校验路径单测 + 启动日志 |
| E-03 | 向量路径降级 | 模拟 embedding 不可用 | 系统进入降级态，门禁旁路 | **「永不拒答」告警必须触发**（降级期无门禁 = 风险态） | 告警日志/诊断标记可见 |
| E-04 | 启动自检 | 配置门禁路径但 calibration artifact 无法加载 COSINE_ABSOLUTE | 服务**拒绝启动**（fail-closed） | 自检在 lifespan try/except 之外执行 | 启动退出码非 0，日志含明确原因 |
| E-05 | 混合检索路权 | 同问题分别强制 vector / lexical / hybrid | hybrid 证据质量 ≥ 单路 | 与已定论一致（hybrid>vector>lexical，rrf_k=60） | 仅作回归，不重复调参 |

### F 组：知识库未覆盖问题（3 例）

| 编号 | 场景 | 输入 | 预期输出 | 验收标准 |
|---|---|---|---|---|
| F-01 | 库外医学问题 | 库内无任何证据的真实医学问题 | 200 `insufficient_evidence`「暂无法回答」 | **正确拒答**；不编造、不返回无关 citation |
| F-02 | 非医学问题 | "今天天气怎么样" | 走 RAG 检索后 insufficient_evidence 或安全拒答 | ⚠️ `Intent.ORDINARY` 仍走 RAG；**不得 LLM 直答**（红线） |
| F-03 | 医学拒答识别 | 模型正确识别医学意图并拒答 | casual_screen 判为 `medical_refusal`（期望行为） | 不误判为 `medical_terms` 闸 2b 误杀 |

### G 组：安全三项零容忍（3 例）

| 编号 | 场景 | 步骤 | 预期输出 | 验收标准 |
|---|---|---|---|---|
| G-01 | 越权 | B-03 / D-03 汇总执行 | 任何角色不可见越权数据 | 0 例越权（绝对标准） |
| G-02 | 删除残留 | 删除/拒绝文档后查索引与 DB | 索引、DB、文件三处均无残留 | 0 残留 |
| G-03 | 复核泄漏 | 未复核文档参与检索？ | pending 文档不出现在任何检索结果 | 检索仅命中已复核内容 |

---

## 5. 执行记录（A/B 组冒烟，2026-10-08 真实链路）

执行脚本 `scripts/e2e_smoke_ab.py`（hermes venv），原始数据 `eval_out/e2e_smoke_ab.json`。
环境：后端 :8000 + OpenSearch 2.18 :9200 + Redis :6380 + MinIO :9000 + 临时启动的 Celery worker（solo）。

| 用例 | 实际结果 | 判定 | 关键证据 |
|---|---|---|---|
| B-01 | 200 `answered`，sections=1，**citations=7**，52.5s | ✅ PASS | diagnostics 完整（见 B-04） |
| B-02 | 200 `answered`，17.9s，citation source_id **精确指向** A-01 上传文档 | ✅ PASS | 定向检索生效；私有索引证据可被引用 |
| B-03 | 未执行 | ⛔ BLOCKED | 本机无第二角色账号（仅患者 13800138000） |
| B-04 | 缺 `embedding_degraded`；其余 8 字段齐全，`gate=cosine_absolute/l2_inv`、阈值 0.5 | ⚠️ 预期修正 | 该键仅降级路径写入（service.py:454），属测试预期错误而非缺陷 |
| A-01 | 201，document_id 返回，worker 解析 → `ready`，索引写入 1 chunk | ✅ PASS | 上传→解析→入库链路（worker 在线时）正常工作 |
| A-02 | ~~review 接口 404~~ → **重测（新后端进程）**：403 `当前身份没有医学复核权限` | ✅ PASS（重测） | 患者角色被正确拒绝（防越权符合预期）；路由已在（openapi 确认）。完整复核通过用例仍需 `medical_reviewer` 账号 + `needs_review` 文档，列为后续 |
| A-03 | DELETE **500**（旧进程与新代码各复现 1 次，累计 3/3）；DB 无墓碑、索引残留 1 | ❌ FAIL（**安全相关，P0-6 确认为代码级缺陷**） | 重测数据 `eval_out/e2e_retest_a02a03.json`；两轮测试文档均已手工清理（残留=0） |

冒烟后已手工清理测试文档（索引 delete_by_query + DB 墓碑，复核残留=0）——因删除 API 不可用只能绕过。

### 🔴 冒烟新发现

**P0-6（新缺陷）：删除接口恒 500，索引残留无法经 API 清除**
- 根因：API 进程从未装配 `app.state.private_index_writer`（`api/app.py` 只装配了 `chat_orchestrator`/`index_orchestrator` 等），`get_private_index_writer` 依赖在处理器执行前抛 `RuntimeError("private index writer is not configured")` → 500。
- 影响：`DELETE /v1/documents/{id}` 完全不可用；DeleteService 的索引/对象存储删除层永远走不到，**删除残留零容忍项在当前部署形态下必然失败**。
- 证据：`eval_out/_delete_probe.txt`（DELETE×2 均 500、DB status 仍 READY、索引残留=1）。

**ENV-1（环境，P1）：活跃后端进程为旧版本 —— ✅ 已消除（10-08 14:xx 重启）**
- 实测 `openapi.json` 缺 `POST /v1/documents/{id}/review`（当前源码无条件注册该路由）；且机器上存在**两个 uvicorn 进程**（hermes venv PID 44864 / 系统 Python3.12 PID 41968）争用 :8000。
- 处置：已终止两个旧进程，用 hermes venv 单进程重启；重启后 `openapi.json` 已含 review 路由，A-02 由 404 转为预期 403。

**ENV-2（环境，P1）：当前代码 + 当前 `.env` 无法通过启动自检（fail-closed 生效）**
- 现象：按当前源码启动后端两次均失败，`Settings.validate_runtime()` 抛
  「配置不安全：开发/测试环境也不得通过远程明文 HTTP 发送 PHI」（`core/config.py:206-215`）。
- 涉及两项远程明文外发：`MODEL_GATEWAY_URL=http://121.40.34.96:18097/v1`、`OCR_SERVICE_URL=http://192.168.254.35:18080`；
  回环 HTTP 才放行（本地 sidecar）。旧进程能用，是因为它跑的是**早于该校验的旧代码**——即"能启动"这件事本身也是版本陈旧的证据。
- 本次处置（**未改 `.env`**，改 `.env` 需单独批准）：以进程级环境变量覆盖启动测试实例——
  `MODEL_GATEWAY_URL=http://127.0.0.1:18097/v1`（回环占位）、`OCR_SERVICE_URL=http://127.0.0.1:18080`。
  该实例**不可用于任何问答（B 组）用例**（模型网关为占位地址），A-02/A-03 不依赖模型/OCR 外发，重测有效。
- ⚠️ 待你决策：① 网关与 OCR 改走 HTTPS（首选，保持开发环境真实配置）；② 或在 `.env` 中把它们指向本地回环 sidecar；
  ③ 或为本地联调新增一个显式豁免开关（需改代码 + 评审）。在此之前**任何后端重启都会失败**。

### ✅ 链路级正面结论

1. **检索→生成主链路健康**：库内问题 200 answered + 7 引用；门禁 `cosine_absolute/l2_inv@0.5` 在场且工作（`vector_candidates_gated_out=7`、`lexical_gated_out=1`）。
2. **摄入链路（worker 在线）正常**：上传 → 解析 → 私有索引写入 ~秒级完成；定向问答精确引用新文档（17.9s）。
3. **diagnostics 可观测性达标**：阶段耗时（lexical 216ms / vector 192ms / total 763ms）、候选计数、calibration artifact（`artifact-2026-09-26`）全部可读。

### 5.1 ENV-2 修复后复跑 B/D/F 组（2026-10-08，真实网关实例）

- 前置：ENV-2 按显式开关路径修复（`ALLOW_PLAINTEXT_PHI_EGRESS=true`，见 `docs/superpowers/specs/2026-10-08-env2-p0-6-design.md` §1），后端以**真实网关**（`glm-5.3-flash`）重启，`/health` 200。
- 执行脚本 `scripts/e2e_rerun_bdf.py`，原始数据 `eval_out/e2e_rerun_bdf.json`。

| 用例 | 实际结果 | 判定 | 证据 |
|---|---|---|---|
| B-01 | 200 `answered`，citations=6，40.5s | ✅ PASS | 库内问题正常作答 |
| F-01 | 200 `insufficient_evidence`，citations=0，5.5s | ✅ PASS | 库外问题正确拒答 |
| F-02 | 200 `insufficient_evidence`，`safety_notice=当前知识库没有覆盖该疾病或主题…` | ✅ PASS | 非医学问句走 RAG 后拒答，未直答（红线守住） |
| D-01 | 追问 200 `degraded`，citations=0，未续用会话 | ❌ FAIL（**归因见 §5.2**） | 失败轮 `answer_path=rag`，会话机制本身正常 |
| D-02 | 历史回放 200，仅 2 条消息（期望 ≥4） | ❌ FAIL（**同上**） | 失败轮不落库 → 消息数不足 |
| D-04 | `session_a=null/session_b=null` | ❌ FAIL（**同上**） | 失败轮不返回 session_id |
| F-03 | `diagnostics.gate_policy=null` | ❌ FAIL（**归因见 §5.2 根因 B**） | 诊断被校验失败分支吞掉 |

### 5.2 🔴 B/D/F 复跑新发现：引用校验抖动 + 降级诊断丢弃（P0-7）

**专用探针**：`agent_lab/probe_degraded_cause.py`（5 轮同问）、`agent_lab/probe_citation_flake.py`（8 轮同问）。
原始数据 `eval_out/probe_degraded.json`、`eval_out/probe_citation_flake.json`。

**量化结果（同一库内问题，患者 A / 相同配置）**：

| 探针 | N | answered | degraded | 抖动率 | 延迟中位数 |
|---|---|---|---|---|---|
| probe_degraded_cause | 5 | 2 | 3 | 60% | ~43s |
| probe_citation_flake | 8 | 5 | 3 | **37.5%** | **49.5s**（min 37.9 / max 72.7） |
| **合计** | **13** | **7** | **6** | **≈46%** | — |

→ **库内问题可用性 ≈ 54%**：近一半的合法库内提问拿不到答案。这远低于「功能验收比例 ≥80%」总则。

**核心判别**：每轮都记录 `diagnostics.answer_path`，失败轮**全部为 `rag`** —— 说明检索链路完整跑通、模型**已返回文本**，仅卡在**引用校验**（`orchestrator.py:835` 的 `UnsupportedClaim` 分支）。会话与检索均无缺陷。
通过轮 `gate_policy=cosine_absolute`，失败轮 `gate_policy=null`；各轮 `text_head` 标题层级/列表形态各异 —— **可直接观察到的"模型输出形态逐轮变化"**，正是抖动来源。

**根因 A（可用性，主因）**：`CitationValidator` 对真实模型输出形态过度敏感。
规则确定性、但模型每次输出的段落结构/句末锚点位置不同 → **同问异果**（实测抖动显著）。
所有失败轮 `safety_notice` 精确等于 `orchestrator.py:836` 的统一文案。
**临床影响：库内问题可用性 ≈54%**（7/13，Wilson 95% 0.291~0.768）。此为 2026-10-05 spec（G1–G6）修复 ②a/②b/②c 后的**残留触发面**。

**根因 B（可观测性，独立且确定）**：`rag` 路径的 `_rag_degraded(...)` 四处出口中**三处漏传 `diagnostics=`**
（L803 超时 / L827 模型不可用 / L837 引用校验失败；**仅 L796 检索不可用一处传了**），
而检索诊断在 `orchestrator.py:756` 已算出并被局部变量持有。
→ 上述失败轮的 `gate_policy / stage_elapsed_ms / embedding_degraded / vector_error` **全部为 null**。
→ **F-03 FAIL 实为本缺陷所致**，非门禁失效（门禁本身工作正常，见 B-01 通过轮的 `gate_policy=cosine_absolute`）。

**D 组 4 例中 D-01/D-02/D-04 的 FAIL 均为根因 A 的下游假象**：
失败轮走 `UnsupportedClaim` 分支 → **不落库、不返回 `session_id`**
→ 追问 `same_session=false`、历史仅 2 条、`session_a=null`。
（`session_b=null` 另属 `insufficient_evidence` 设计预期：该路径本就不返回 session_id。）

**修复规格**：`docs/superpowers/specs/2026-10-08-citation-validator-flake-design.md`（待批准）。
根因 B 建议直接实施（低成本、零安全风险）；根因 A 三方向（A1 规则增强 / A2 校验失败回灌重试 / A3 部分段落降级）待决策。

### 5.3 越权/隔离组复跑（2026-10-08，患者 A + 第二租户患者 B）

- 前置：第二租户患者 B（`13800138002`/`test1234`，tenant `9999…`/patient `8888…`）已建；A 有真实私有文档（2 chunk）。
- 执行脚本 `scripts/e2e_rerun_authz.py`，原始数据 `eval_out/e2e_rerun_authz.json`。

| 用例 | 实际结果 | 判定 | 证据 |
|---|---|---|---|
| D-03 | B 读 A 会话 → **403**（拒）、**无数据泄漏**；响应体与「随机不存在」**逐字节一致** | ✅ PASS | 契约 S3 书面口径（响应体不可区分）成立；**状态码 403/404 可区分会话存在性**（防枚举加固项，见备注） |
| D-03b | B 持 A 的 session_id 提问 → **403**（`_resolve_session` 拒绝），未写入 A 会话 | ✅ PASS | 写入路径越权被正确拦下 |
| B-03 | B 的 4 个引用**全部命中公共索引**，与 A 的 2 个私有来源**零交集** | ✅ PASS | 直查 `medical-rag-public-local` 逐条核验；跨租户隔离成立 |
| G-01 | 跨租户读会话 + 持他人 session_id 提问 均被拒且无泄漏；引用不含 A 私有来源 | ✅ PASS（**安全项零容忍达成**） | — |

**⚠️ 两处口径修正（避免误判）**：
1. **索引 `tenant_id`/`patient_id` 存的是带连字符 UUID**（`1111…-1111-…`），非 DB 里的紧凑形式。
   首跑用紧凑串查索引 → 恒返 0（**假阴性**，导致误以为 A 无私有文档）。修正后确认 A 有 2 条私有 chunk。
2. **D-03 的判据是「响应体」逐字节一致（契约 S3），不是状态码一致**。本项目实现用 403（越权）/404（不存在）
   两种状态码——**体一致、码不同**。按 plan 书面口径 PASS；状态码差异列为**防枚举加固项**（严格版 S3 要求
   状态码亦不可区分），**不阻断 G-01**（安全属性"被拒 + 无泄漏"已满足）。

**A 建会话经历 3 次**（前 2 次均 degraded）——再次独立佐证 P0-7 抖动。

## 6. 执行记录表（后续组回填模板）

| 用例 | 执行人 | 日期 | 实际结果 | 通过? | 证据（响应体/日志路径） | 备注 |
|---|---|---|---|---|---|---|
| A-01 | agent | 10-08 | 上传→解析→私有索引 1 chunk | ✅ | `eval_out/e2e_smoke_ab.json` | worker 在线时正常 |
| A-02 | agent | 10-08 | 患者复核 → 403「无医学复核权限」 | ✅(预期拒) | 同上 + `_retest` | 完整复核需 reviewer 账号（产品缺口，见备注） |
| A-03 | agent | 10-08 | DELETE 500，索引残留 1 | ❌ FAIL | `eval_out/_delete_probe.txt` | **P0-6**，代码级 |
| A-04/A-05 | — | — | 未执行 | — | — | 依赖 P0-3/P0-2 语料 |
| B-01 | agent | 10-08 | 200 answered + 6 引用，40.5s | ✅ | `eval_out/e2e_rerun_bdf.json` | 通过轮验证链路正常 |
| B-02 | agent | 10-08 | 200 answered，定向引用精确命中 A-01 文档 | ✅ | `eval_out/e2e_smoke_ab.json` | 私有索引证据可引用 |
| B-03 | agent | 10-08 | B 的 4 引用全在公共索引，与 A 私有来源零交集 | ✅ | `eval_out/e2e_rerun_authz.json` | 租户隔离成立 |
| B-04 | agent | 10-08 | 8 字段齐全，`gate=cosine_absolute/l2_inv@0.5`；`embedding_degraded` 正常路径缺省 | ⚠️ 口径修正 | 同上 | 该键仅降级时写入 |
| C-01~C-06 | — | — | 未执行 | — | — | 待补（边界输入，不调模型） |
| D-01 | agent | 10-08 | 追问 degraded、未续用会话 | ❌ FAIL | `eval_out/e2e_rerun_bdf.json` | **归因 P0-7**（非会话缺陷） |
| D-02 | agent | 10-08 | 历史仅 2 条（期望 ≥4） | ❌ FAIL | 同上 | **归因 P0-7**（失败轮不落库） |
| D-03 | agent | 10-08 | B 读 A 会话 403、无泄漏、响应体与随机 404 逐字节一致 | ✅ | `eval_out/e2e_rerun_authz.json` | 体一致；状态码差异=加固项 |
| D-03b | agent | 10-08 | B 持 A 的 session_id 提问 → 403 | ✅ | 同上 | 写入越权被拦 |
| D-04 | agent | 10-08 | `session_a=null`（失败轮不返回 sid） | ❌ FAIL | `eval_out/e2e_rerun_bdf.json` | **归因 P0-7**；`session_b=null` 属 insufficient_evidence 预期 |
| E-01~E-05 | — | — | 未执行 | — | — | 门禁组待补 |
| F-01 | agent | 10-08 | 库外问题 → insufficient_evidence，0 引用 | ✅ | `eval_out/e2e_rerun_bdf.json` | 正确拒答 |
| F-02 | agent | 10-08 | 非医学问句 → 走 RAG 后拒答（未直答） | ✅ | 同上 | 红线守住 |
| F-03 | agent | 10-08 | `diagnostics.gate_policy=null` | ❌ FAIL | 同上 | **归因 P0-7 根因 B**（诊断被降级出口吞掉） |
| G-01 | agent | 10-08 | 越权零容忍（D-03/D-03b/B-03 汇总） | ✅ | `eval_out/e2e_rerun_authz.json` | — |
| G-02 | — | — | 阻塞 | ⛔ BLOCKED | — | **P0-6** 未修前无意义 |
| G-03 | — | — | 未执行 | — | — | 复核泄漏，依赖 P0-3 |

## 7. 风险与遗留

1. **P0-2 未修前，摄入类用例只能验证缺陷现状**，不能作为内容质量验收（语料前提未满足 → 忠实度恒 undecidable）。
2. **P0-3 未修前，A-02/A-04 必须以直接查索引为准**，接口 200 不可信。
3. 问答接口 ~103s 延迟使全量执行耗时约 25~40 分钟（问答类 21 例），建议分组并行或使用异步执行脚本。
4. 本方案**不采用 golden.py 模拟检索**（P0-4 空转），全部断言基于真实 HTTP 响应体与索引直查。
5. **P0-6 阻塞 G 组删除类用例**：`DELETE` 恒 500 未修前，G-02（删除残留）与 A-03 属"已知必败"，修复立项前不必重复执行。
6. **ENV-2（已修复）**：原 `.env` + 当前代码无法启动（远程明文 PHI 外发被拒）。
   已按显式开关路径修复（`ALLOW_PLAINTEXT_PHI_EGRESS=true`，默认关闭 + 仅开发环境 + 非开发环境启用即致命），
   后端以真实网关启动成功。详见 §5.1 与 `docs/superpowers/specs/2026-10-08-env2-p0-6-design.md`。
7. **越权类用例（B-03 / D-03 / G-01）✅ 已完成**（10-08，用第二租户患者 B）：
   公共/私有隔离与跨租户枚举防护均通过。**仅完整 A-02 复核用例仍缺 `medical_reviewer` 账号**——
   登录接口 `auth.py:110 sign_access_token` 从不传 `role=` → 恒发 `role=patient`，属**产品缺口**（非测试环境问题）。
8. **P0-7 阻塞 D 组可用性验收**：引用校验抖动未修前，D-01/D-02/D-04 与 F-03 的 FAIL 均不可判定为独立缺陷；
   修复规格见 `docs/superpowers/specs/2026-10-08-citation-validator-flake-design.md`，待批准。
9. **防枚举加固项（安全，非阻断）**：`GET /v1/sessions/{id}/messages` 越权返回 403、不存在返回 404，
   状态码可区分会话存在性（**响应体已一致、无内容泄漏**）。严格版契约 S3 要求状态码亦不可区分，
   建议后续统一为 404（或统一 403）。不阻断 G-01。

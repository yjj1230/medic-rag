# chat 会话态持久化 Implementation Plan

> 关联 spec：`docs/superpowers/specs/2026-10-06-chat-session-persistence-design.md`
> 遵循 FEATURE-WORKFLOW：每任务按 TDD（先写失败测试 → 确认失败 → 实现 → 验证）。

## Global Constraints

- **不动既有契约**：`POST /v1/chat/answers` 的请求/响应结构不得改动；只在 `chat.py` **新增**一条 GET 路由。
- **安全红线**：新增历史接口必须做会话归属校验；跨患者/跨租户一律拒绝（S1–S3）。
- **不引入新依赖**：前端不新增 markdown/持久化库（`localStorage` 只用浏览器原生）。
- **不改落库结构**：`chat_messages` 表与 `orchestrator` 写路径本次**零改动**。
- **可回滚**：前端改动集中在 `stores/chat.ts` + `views/ChatPage.vue` + `api/chat.ts`。
- 环境：后端用 hermes venv python；测试需 `CODEBUDDY_SAFE_DELETE_ENABLED=0`；前端 `npm.cmd run dev|test`。

---

## Task 1：后端 — 会话消息读取接口（含归属校验）

**验收标准**：`GET /v1/chat/sessions/{id}/messages` 返回该会话消息（升序）；他人会话 → 403；无 token → 401。

- [x] Step 1: 写失败测试 `tests/contract/test_chat_session_history.py`
  - 正常：本人会话 → 200，`messages` 升序、字段含 `role/content/created_at`。
  - `limit` 缺省 50；`limit=999` 夹取到 200；`limit=0` 夹取到 1。
  - S1 跨患者 → 403；S2 跨租户 → 403；S3 不存在 → 404 且与 403 文案不可区分（用同一文案）。
  - S5 无 token → 401。
- [x] Step 2: **运行并确认全部失败**（路由不存在 → 404/405）。
  - 实测：9 failed（全部 `assert 404 == ...`，即路由不存在）。
- [x] Step 3: 实现
  - `api/routes/chat.py`：新增 `ChatMessageItem` / `ChatSessionHistoryResponse` 模型 + GET 路由。
  - 归属校验复用与 `orchestrator._resolve_session` **相同语义**（tenant_id + patient_id 比对）。
  - 查询用 `ix_chat_messages_session_created` 索引：`ORDER BY created_at ASC, id ASC LIMIT ?`。
- [x] Step 4: 跑该测试至全绿；跑 `pytest -k "chat"` 确认无回归。
  - 实测：9 passed；`-k chat` 42 passed / 818 deselected。

## Task 2：前端 — store 恢复能力（`ensureLoaded` / `toMessage`）

**验收标准**：`store.ensureLoaded()` 在 store 为空且有 sessionId 时拉取并映射；已有内容时不重复拉取。

- [x] Step 1: 写失败测试 `frontend/src/stores/__tests__/chat.spec.ts`（vitest + msw mock 接口）
  - 空 store + sessionId → 调用接口，`messages` 被填充，顺序正确。
  - 非空 store → 不调用接口（用 mock 调用计数断言）。
  - 无 sessionId → 不调用接口。
  - `toMessage`：`traceId` 取 `metadata.trace_id`；`citedEvidenceCount` 取 `metadata.cited_evidence.length`；**不伪造 citations**。
  - 接口 403/500 → 静默降级（`messages` 仍为空、不抛错）。
- [x] Step 2: **运行并确认失败**（`npm run test` 需先在 package.json 加 `"test": "vitest run"`）。
  - 实测：9 failed（`fetchSessionMessages` is not defined / `toMessage` is not a function）。
- [x] Step 3: 实现
  - `api/chat.ts`：新增 `fetchSessionMessages(sessionId, limit)`。
  - `stores/chat.ts`：新增 `ensureLoaded()`、`toMessage()`、`citedEvidenceCount` 字段。
  - `MessageBubble.vue`：历史消息的降级提示「本回答引用 N 条证据（历史消息）」。
- [x] Step 4: 跑测试至全绿。
  - 实测：9 passed。

## Task 3：前端 — 切页保留 + 显式新对话

**验收标准**：切页往返不清空；裸 `/chat` 不清空；「新对话」能清空并回到 `/chat`。

- [x] Step 1: 写失败测试 `frontend/src/views/__tests__/ChatPage.spec.ts`
  - 模拟：`sessionId` 为 A → 路由切到 `/documents`（params 变 undefined）→ **store.messages 不得被清空**。
  - 裸 `/chat` 挂载 → **不清空既有消息**。
  - 真的切到另一个 sessionId B → 清空并加载 B。
  - 点「新对话」→ `messages` 空、`sessionId` 空、路由为 `/chat`。
- [x] Step 2: **运行并确认失败**（现状 watch 会误 reset）。
  - 实测：4 failed（前两条 `expected [] to have a length of 1 but got 0`；第三条未加载 B；第四条无「新对话」按钮）。
- [x] Step 3: 实现
  - `ChatPage.vue` watch 加 `if (!route.path.startsWith("/chat")) return;`
  - `onMounted` 改为"仅会话真变才 reset" + 调 `ensureLoaded()`。
  - 输入区加「新对话」按钮 → `newConversation()`（§6.2）。
- [x] Step 4: 跑测试至全绿。
  - 实测：4 passed；前端全量 13 passed；`vue-tsc -b` 退出码 0（类型无错）。

## Task 4：真实验收（浏览器实测）

**验收标准**：spec §10 的 A1–A6 全部通过。

- [x] Step 1: 确保后端(:8000) 已重启到含新路由的代码；前端 `npm run dev`。
  - 实测：后端 `hermes venv python -m uvicorn medical_rag.api.app:create_app --factory`
    监听 8000（系统 Python312 无 uvicorn，须用 hermes venv）；前端 Vite 监听 5173。
- [x] Step 2: 用真实浏览器（agent-browser）以 `13800138001/123456`：
  - 提问 → 有回答 → 切「我的报告」→ 切回「问答」（消息仍在）
  - F5 刷新（消息恢复）→ 点「新对话」（清空）
  - 实测（验收期日志 `_ui1/_ui2/_ui3.txt` 已随临时文件清理，留存证据见下）：
    - A1：问答 `POST /v1/chat/answers` 返回 `status=answered`、4 条 citation、
      返回 `session_id`；后端实测耗时 **103.4 s**（用 Python 探针 `trust_env=False` 直连测得）。
    - A3/G1：直开 `/chat/<sid>` 恢复 2 条消息且顺序 user→assistant；
      点「我的报告」→ `/documents`，点回「问答」→ `/chat/<sid>`，**消息仍在**。
    - A4/G3：直开 `/chat/<sid>` 后改走裸 `/chat`，再 F5，**消息从服务端恢复**，
      末尾显示「本回答引用 4 条证据（历史消息）」。
    - A5/G2：点「新对话」→ URL 回 `/chat`、消息清空、
      `localStorage['medical-rag.chat.session-id'] === null`。
  - 说明：`agent-browser find role ... click` 对 el-menu / el-button 只报 Done
    但**不真正触发**，会产生假阴性；改用 `eval` 直接 `.click()` 后全部通过。
    agent-browser 守护进程跨调用会丢页面，**所有步骤必须在单次 bash 调用内串联**。
- [x] Step 3: 直接对 API 断言 A6：B 账号 token 读 A 的 session → 403。
  - 实测（Python 探针 `trust_env=False` 直连 :8000，无 mock）：**6/6 PASS** ——
    A 读自己 200（roles=[user,assistant]）；B 读 A 403（`无权访问该会话`）；无 token 401；
    不存在 404 且与 403 **同文案**；反向对照：B 读自己的会话 200（证明不是"一律拒绝"）。
- [x] Step 4: 记录证据（截图/响应），跑后端全量 `pytest` 记录通过数。
  - 后端全量：**859 passed, 3 skipped**（较 T1 后 857 增 2 条排序回归测试）。
  - 前端全量：**18 passed**（chat.spec 14 + ChatPage.spec 4）；`vue-tsc -b` 退出码 0。
  - 留存截图：`docs/superpowers/notes/evidence/2026-10-06-chat-history-restored.png`
    （直开 `/chat/<sid>` 恢复会话的实拍）。

---

## 真实验收新增修复（原计划未含，验收中发现）

- [x] **R1 消息排序竞态**：`orchestrator._persist_answer` 用 `db.add_all([user, assistant])`，
  两者拿到**同一微秒 `created_at`**；`ORDER BY created_at ASC, id ASC` 于是退化为 **uuid4 随机序**，
  重放时偶发"答案排在问题前"。修法：`chat.py` 查询加

  ```python
  case((ChatMessage.role == "user", 0), else_=1).asc()
  ```

  作为确定性次级键（不改数据、不做迁移）。回归测试 +2 → 契约测试 11 passed。

- [x] **R2 裸 `/chat` 无法恢复（A4 曾失败）**：顶栏「问答」原指向裸 `/chat`，点一下就把
  `sessionId` 从 URL 里丢掉，此后 F5 无据可依。修法两条：
  1. `App.vue` 导航 `:index="chatPath"`，`chatPath = chat.sessionId ? \`/chat/${id}\` : "/chat"`，
     并把高亮改为 `activePath`（仅当处于 `/chat` 区时用 chatPath）。
  2. `stores/chat.ts` 用 `localStorage['medical-rag.chat.session-id']` 记住最后一个会话：
     `send()` 拿到后端新 id 时写入、`ensureLoaded()` 成功时写入、`reset()` 清除；
     store 初始化时还原；`ChatPage.vue` `onMounted` 无条件调 `ensureLoaded()`（自带守卫）。
  - 前端回归测试 +5 → chat.spec 14 passed。

### 验收中发现的既有缺陷（不在本计划范围，需单独立项）

- [ ] **问答接口实测 103.4 s**（`POST /v1/chat/answers`，单次真实调用）。功能正确但延迟远超
  常规交互容忍度；浏览器 25 s 内看不到回答会误判为"没反应"。需单独立项定位（疑似外部模型串行调用）。
- [ ] **agent 回答正文仍渲染为原始 Markdown**（见留存截图：`## 高血压生活方式干预要点` 与 `[uuid]`
  字面显示）。这是 spec §8 已声明的非目标，但实测观感较差，建议单独立项做 Markdown 渲染
  （注意：渲染后需保证引用标记仍是文本，不能变成可点击的越权入口）。
- [ ] **会话归属只有 `patient_id`+`tenant_id`，无 `role` 声明**（`AccessContext` 目前只有前两者）。
  单患者自读自写无问题；一旦引入医生/管理员跨患者查房，本接口会一律 403。已在 P0-3 计划的
  D-B 项（JWT claims + role）预留，本计划未夹带。

---

## Plan Self-Review

- [x] 每个任务都有可独立验收的测试（Task 1/2/3 均有；Task 4 是端到端）。
- [x] TDD 顺序完整：每个任务都有"确认失败"步骤。
- [x] 安全不变量 S1–S5 有对应测试（Task 1）。
- [x] 非目标未被夹带：Markdown 渲染、citations 完整回填**均未列入任何任务**。
- [x] 与 spec 一致：G1/G2/G3/G4 分别对应 Task 3/3/2+3/1。
- [x] 回滚面小：前端 3 文件（+App.vue）+ 后端 1 文件。

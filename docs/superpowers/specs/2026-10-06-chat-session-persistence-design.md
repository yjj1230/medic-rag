# chat 会话态持久化（切页保留 + 刷新恢复）— 设计文档

- 受理日期：2026-10-06
- 提出人：用户（患者端使用中发现问题）
- 原始诉求（原话，不改写）：
  > 聊天框的信息会消失当我进行页面切换的时候
- 状态：**待批准**（批准前不写生产代码，见 FEATURE-WORKFLOW 规则 3）

---

## 1. 背景

患者端「问答」页（`/chat/:sessionId?`）存在体验缺陷：用户在问答页发完消息后，切换到「我的报告」或「知情同意」再切回来，**对话内容全部消失**。

**这不是数据丢失**。后端 `chat_messages` 表已完整落库（实测该会话 user/assistant 两条记录均在）。丢失的是**前端内存态**（Pinia `chat` store 的 `messages`）。

同时暴露一个**独立问题**（本次不改，见 §2 非目标）：回答里的 `## 标题`、`- 列表` 被当纯文本显示，未渲染 Markdown。

---

## 2. 目标与非目标

### 目标

| # | 目标 | 优先级 |
|---|---|---|
| G1 | 切换页面（Tabs 往返）后，当前会话消息**不消失**，切回来仍在 | P0 |
| G2 | 用户能**显式开启新对话**（不能因为修 G1 而失去"开新会话"的能力） | P0 |
| G3 | 硬刷新页面 / 直接访问 `/chat/<id>`，能**从服务端恢复历史消息**（至少文字） | P1 |
| G4 | 历史接口有**严格的会话归属校验**，不得跨患者读取（IDOR 防护） | P0（安全红线） |

### 非目标（本次明确不做，及其原因）

| 不做 | 原因 |
|---|---|
| Markdown 渲染（`##`/列表/引用标记） | 用户本轮未选；属独立视觉缺陷，改动面在 `MessageBubble.vue`，另行立项 |
| 引用徽章在刷新后的**完整**还原（标题/来源/版本） | 当前 `metadata_json` 只存 `cited_evidence` 的 UUID，无标题。完整还原需同时改落库结构，范围过大。本次**只做文字恢复 + 引用数降级展示**，缺口记入 §8 |
| `status`（answered/insufficient_evidence/degraded）与 `safety_notice` 的刷新恢复 | 二者未落库；回填需改 orchestrator 持久化结构，属后续增强 |
| 会话列表页 / 多会话切换 UI | 当前无此产品形态，超范围 |
| 后端「新对话」接口 | 新对话是纯前端概念（清空内存 + 换 sessionId），无需服务端参与 |

---

## 3. 根因分析（含实测证据）

### 根因 1（主因）：`ChatPage` 的 watch 把"离开页面"误判为"切换会话"

`frontend/src/views/ChatPage.vue:86-97`：

```js
watch(
  () => route.params.sessionId,
  (newId) => {
    if (!newId) {
      store.reset();                                  // ← 元凶
    } else if (newId !== store.sessionId) {
      store.reset();
      store.sessionId = newId as string;
    }
  }
);
```

路由**离开**问答页时（如跳 `/documents`），当前路由 `params.sessionId` 变为 `undefined` →
`!newId` 为真 → `store.reset()` 清空消息。

| 操作 | `route.params.sessionId` | 触发分支 | 结果 |
|---|---|---|---|
| 在 `/chat/<id>` 发消息 | `<id>` | 无 | 正常 |
| 切到 `/documents` | **`undefined`** | `!newId` | **`store.reset()`** |

### 根因 2（并存）：`onMounted` 在裸 `/chat` 上也会清空

`ChatPage.vue:68-75`：切回问答页时若 URL 是裸 `/chat`（顶栏「问答」菜单项 `index="/chat"` 正是如此），
`onMounted` 走 `else { store.reset(); }` → 即使修好根因 1，点顶栏「问答」仍会清空。

### 根因 3：无历史恢复能力

`<router-view />` 无 `<KeepAlive>`，`ChatPage` 每次切页重新挂载；即便消息还在 store，
**硬刷新**会重建整个 JS 上下文，store 归零，且**后端无任何会话消息读取接口**（实测 `openapi.json`
仅 `/v1/chat/answers`），故无法恢复。

### 证据

```
# 后端已落库（消息没丢）
chat_sessions: 12 rows   chat_messages: 24 rows
[assistant] metadata_json={"trace_id":"794f284a-...","intent":"health_education",
             "cited_evidence":["39f7ae94-...","7498fde6-...","7ebdec91-...","217f6ad9-..."]}

# 后端接口清单（无会话读取接口）
POST /v1/chat/answers     ← 仅此一条 chat 接口
GET  /v1/consents
GET,POST /v1/documents
```

---

## 4. 总体设计

```
                       ┌──────────────── 前端 chat store（单例，跨路由存活）─────────────┐
用户操作 ──► 路由变化 ──►│  messages 属于 sessionId；只在「会话真的变了」时 reset            │
                       └──────────────────────────────────────────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
  切页往返                  显式「新对话」              硬刷新/直达 URL
  （G1）                    （G2）                     （G3）
  不动 store                 reset + /chat              拉 GET .../messages 回填
```

**核心原则**：`messages` 的生命周期**绑定 `sessionId`，而不是绑定组件的挂载**。
组件挂载/卸载与路由离开**都不是**清空理由；只有"会话身份改变"或"用户显式要新对话"才是。

---

## 5. 核心数据契约

### 5.1 后端新增：会话消息读取

```
GET /v1/chat/sessions/{session_id}/messages?limit=50
Authorization: Bearer <jwt>
```

响应 `200`：

```json
{
  "session_id": "7e1340630e144be19736e47d9f8f2c99",
  "messages": [
    { "id": "...", "role": "user",      "content": "高血压患者生活方式…",
      "created_at": "2026-10-06T01:49:40Z", "metadata": null },
    { "id": "...", "role": "assistant", "content": "根据资料，…",
      "created_at": "2026-10-06T01:49:40Z",
      "metadata": { "trace_id": "...", "intent": "health_education",
                    "cited_evidence": ["39f7ae94-…", "…"] } }
  ]
}
```

- 排序：`created_at ASC, id ASC`（升序回放；已有索引 `ix_chat_messages_session_created` 直接支撑）。
- `limit`：默认 50，上限 200（防大会话一次性拉爆）。
- 归属不匹配 → `403 {"detail": "无权访问该会话"}`（与 `orchestrator._resolve_session` 同语义）。
- 会话不存在 → `404`。

### 5.2 前端 `Message` 结构（既有，扩展恢复字段）

```ts
interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  citations?: ChatCitation[];
  status?: string;          // 恢复时为 undefined（未落库）
  safetyNotice?: string | null;
  traceId?: string | null;  // 可由 metadata.trace_id 恢复
  citedEvidenceCount?: number;  // 新增：恢复时用于降级提示"本回答引用 N 条证据"
}
```

---

## 6. 设计细节

### 6.1 前端：切页保留（G1）

`ChatPage.vue` 的 watch 加**页内守卫**：

```js
watch(() => route.params.sessionId, (newId) => {
  if (!route.path.startsWith("/chat")) return;   // 离开问答页：什么都不做
  if (!newId) return;                            // 裸 /chat：交给 onMounted 决定
  if (newId !== store.sessionId) { store.reset(); store.sessionId = newId; }
});
```

`onMounted` 改为**不再无条件 reset**：

```js
onMounted(() => {
  docStore.load();
  if (route.params.sessionId) {
    if (route.params.sessionId !== store.sessionId) {   // 真的换了会话
      store.reset();
      store.sessionId = route.params.sessionId;
    }
    store.ensureLoaded();   // G3：store 为空时拉历史
  }
  // 裸 /chat：保持现状，不清空（顶栏「问答」即走此分支）
});
```

### 6.2 显式「新对话」（G2）

问答页输入区加一个「新对话」按钮：

```js
function newConversation() {
  store.reset();                 // 清消息 + sessionId
  router.push({ name: "Chat", params: {} });   // /chat
}
```

**为什么必须有**：§6.1 移除了"裸 `/chat` 即清空"的隐式语义，若不补显式入口，
用户将失去开启新会话的能力（只能靠退出登录）。

### 6.3 刷新恢复（G3）

`chat` store 新增：

```ts
async function ensureLoaded() {
  if (messages.value.length || !sessionId.value) return;   // 已有内容 → 不拉
  const res = await fetchSessionMessages(sessionId.value, 50);
  messages.value = res.data.messages.map(toMessage);
}
```

`toMessage`：`role/content` 直取；`traceId = metadata.trace_id`；
`citedEvidenceCount = metadata.cited_evidence?.length ?? 0`。
**恢复的 assistant 消息不伪造 `citations`**（无标题可用）→ 改为在气泡内显示
「本回答引用 N 条证据（历史消息）」，避免渲染空/错引用（见 §8 缺口）。

### 6.4 后端：新增路由与查询

- 路由：`api/routes/chat.py` 增 `GET /v1/chat/sessions/{session_id}/messages`。
- 依赖：`current_access_context`（复用既有鉴权）。
- 查询：`SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at ASC, id ASC LIMIT ?`。
- **归属校验**：先按 `session_id` 取 `ChatSession`，比对 `tenant_id` + `patient_id`；
  不匹配一律 `403`（**不区分"不存在"与"无权限"**，避免枚举他人会话 ID）。
- 只读，不产生 audit 写；不触碰任何既有写路径。

---

## 7. 安全 / 合规不变量

| # | 不变量 | 断言方式 |
|---|---|---|
| S1 | 患者 A 不能读取患者 B 的会话消息 | 新增契约测试：B 的 token 读 A 的 session → 403 |
| S2 | 跨租户读取一律 403 | 同上（不同 tenant） |
| S3 | 不存在与无权限**响应不可区分**（均 403/404 同文案） | 断言两种输入响应的 status+body 一致 |
| S4 | 历史恢复**只读展示**，不改变任何已交付内容，不触发检索/模型 | 断言恢复路径零检索、零模型调用 |
| S5 | 未认证访问 → 401 | 无 token 请求 → 401 |

> 安全三项零容忍（既有基线）不受影响：本次不涉及越权写、删除、复核态。

---

## 8. 错误与降级

| 场景 | 行为 |
|---|---|
| 历史接口 403/404 | 前端**静默降级**为空对话（不弹错、不阻断输入）；仅在 console 记一次 |
| 历史接口 5xx / 网络失败 | 同上静默降级；用户仍可正常发起新提问 |
| `limit` 超限或非法 | 后端夹取到 [1, 200]；非法值按默认 50 |
| 恢复的消息缺 `citations` | 显示「本回答引用 N 条证据（历史消息）」，不渲染徽章 |
| 恢复的消息缺 `status` | 按 `answered` 处理（内容已是已交付文本） |

**已知缺口（显式记录，不静默）**：`citations` 的标题/来源/版本、`status`、`safety_notice`
**当前未落库**，故刷新后无法完整还原。彻底修复需扩展 `metadata_json` 落库结构 + 数据回填，
属独立后续项（记入交付说明）。

---

## 9. 实施阶段

| 阶段 | 内容 | 可独立验收 |
|---|---|---|
| A | 后端 `GET .../messages` + 归属校验 + 契约测试（S1–S3、S5） | 接口返回正确、越权 403 |
| B | 前端 store `ensureLoaded` + `ChatPage` 恢复接线 + 组件测试 | 刷新后消息重现 |
| C | 前端切页修复（§6.1 watch 守卫 + onMounted）+ 「新对话」（§6.2） | 切页不消失、新对话可用 |
| D | 真实验收：浏览器实测「发消息 → 切报告 → 切回 → 刷新」四步 | 见 §10 |

---

## 10. 验收标准（可观测、可复现）

以 `13800138001 / 123456` 登录 http://localhost:5173 ：

| # | 步骤 | 期望 |
|---|---|---|
| A1 | 在问答页提问 → 收到回答 | 消息上屏 |
| A2 | 切到「我的报告」 | 不报错 |
| A3 | 切回「问答」 | **消息仍在**（G1） |
| A4 | 硬刷新（F5） | **消息从服务端恢复**（G3） |
| A5 | 点「新对话」 | 消息清空，可重新提问（G2） |
| A6 | 用 B 账号 token 请求 A 的 session 历史 | **403**（S1） |

自动化：
- 后端契约测试：S1–S3、S5 + 正常读取（含排序、limit 夹取）。
- 前端单测（vitest）：`ensureLoaded` 逻辑、`toMessage` 映射、watch 守卫不误 reset。

---

## 11. 风险

| 风险 | 缓解 |
|---|---|
| 新增接口成 IDOR 面 | §7 S1–S3 契约测试钉死；失败即阻断交付 |
| 改 watch/onMounted 引发"新对话"回归 | §6.2 显式入口 + A5 验收 |
| 大会话拉取拖慢首屏 | `limit` 默认 50、上限 200 |
| 恢复消息无引用被误解为"无依据" | 显示"引用 N 条证据（历史消息）"明示降级 |

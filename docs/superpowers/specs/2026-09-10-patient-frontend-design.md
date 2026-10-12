# 患者端前端界面设计

## 1. 文档信息

- 日期：2026-09-10
- 状态：设计已确认，待实施方案
- 依赖：`docs/superpowers/specs/2026-09-09-medical-patient-rag-design.md`
- 目标：为 medical-rag 患者端建设配套的前端 Web 界面（登录、知情同意、报告管理、问答聊天）

## 2. 技术选型

- **框架**：Vue 3（组合式 API）+ TypeScript
- **构建**：Vite
- **UI 组件库**：Element Plus（企业级桌面端组件库，中文文档完善，表单/表格/上传/对话框开箱即用）
- **状态管理**：Pinia
- **路由**：Vue Router（登录守卫）
- **HTTP**：axios（请求/响应拦截器统一处理 token 注入与错误映射）
- **测试**：vitest + @vue/test-utils + msw（API mock）
- **部署**：SPA 静态构建产物，nginx 容器托管并同源反代 `/v1` 到 FastAPI，加入现有 docker-compose

## 3. 项目结构

```
frontend/
  src/
    api/
      index.ts        # axios 实例、拦截器
      auth.ts         # POST /v1/auth/login
      consents.ts     # GET/POST/DELETE /v1/consents/{purpose}
      documents.ts    # POST/GET/DELETE /v1/documents
      chat.ts         # POST /v1/chat/answers
    stores/
      auth.ts         # token、当前用户信息
      chat.ts         # 会话列表、当前会话消息
    views/
      LoginPage.vue
      ConsentPage.vue
      DocumentsPage.vue
      ChatPage.vue
    components/
      MessageBubble.vue       # 对话气泡（user / assistant）
      CitationBadge.vue       # 引用角标 → hover 弹层展示来源
      SafetyNoticeBar.vue     # 安全提示条（急症红 / 安全黄 / 降级灰）
      UploadDropZone.vue      # 拖拽上传区
      DocumentStatusTag.vue   # 文档状态标签
      ConsentCard.vue         # 同意项卡片（授予/撤回）
    router/
      index.ts        # 路由配置 + 登录守卫
    types/
      index.ts        # 共享 TS 类型定义
    App.vue
    main.ts
  vite.config.ts
  Dockerfile
  index.html
```

## 4. 页面设计

### 4.1 登录页 `/login`

- 手机号 + 密码表单，Element Plus `el-form` + 校验规则
- 登录成功 → Pinia 存储 JWT token + user → 路由跳转 `/chat`
- 登录失败 → 表单项下方错误提示
- 无注册入口（账号由管理员预建）

### 4.2 知情同意页 `/consents`

- 三种用途卡片：报告处理 / 外部模型 / 健康教育
- 每张卡片展示当前状态（已同意 / 未同意）、policy 版本、授予时间
- 授予按钮直接生效，撤回按钮带二次确认对话框
- 页面水平布局，PC 宽屏三列

### 4.3 我的报告 `/documents`

- 上传区：Element Plus `el-upload` drag 模式，前端镜像后端策略校验（扩展名 / MIME / 大小 ≤ 50 MiB），带进度条
- 文档列表：`el-table`，列：文件名、类型、状态（uploaded/processing/ready/failed）、上传时间、操作
- 操作列：下载、删除。删除 → 确认框 → 展示逐层删除回执（completed_layers / failed_layers）

### 4.4 问答页 `/chat`

- 左侧会话列表（el-menu 风格）、右侧对话主区域
- 消息气泡：用户消息和助手消息分别左右对齐
- 助手消息中 `[evidence-id]` 渲染为可点击引用角标，hover 弹层展示：来源标题、版本、位置、URL
- 文档范围选择器（可折叠）：选择"基于全部资料"或指定某份报告
- 四种响应状态映射：
  - `answered`：正常助手气泡 + 底部引用角标列表
  - `insufficient_evidence`：灰色信息卡 "当前没有足够证据，请咨询医生"
  - `safe_template`：黄色安全提示条，根据 risk_class 展示对应文案
  - `degraded`：服务降级提示卡 "模型服务暂不可用"
- 急症信号（URGENT_ESCALATION）：红色紧急提示条
- 输入区：`el-input` textarea + 发送按钮，回车发送

## 5. 后端认证改造

### 5.1 新增登录端点

```
POST /v1/auth/login
Body: { "phone": "13800138000", "password": "..." }
Response: { "access_token": "...", "token_type": "bearer", "expires_at": "2026-09-11T00:00:00Z" }
```

- 后端使用 `bcrypt` 验证密码
- JWT 使用 `pyjwt` 签发，payload：`sub`(patient_id), `tenant_id`, `exp`
- `patients` 表新增 `password_hash VARCHAR(128)` 字段（Alembic 迁移）

### 5.2 current_access_context 双模式

改造 `src/medical_rag/auth/service.py`：

1. 优先检查 `Authorization: Bearer <token>`，解析 JWT 构建 AccessContext
2. 无 Bearer 时回退现有网关 Header 模式（`X-Tenant-ID / X-Patient-ID / X-User-ID`）

### 5.3 账号预建 CLI

```
python -m medical_rag.cli create-patient <tenant_id> <phone> <display_name>
```

- 校验手机号格式，自动生成随机密码或由管理员指定，打印到控制台，管理员线下告知患者
- 密码使用 bcrypt 哈希存储

## 6. API 对接与数据流

- axios 实例 `baseURL: ""`（同源部署），请求拦截器注入 `Authorization`，响应拦截器统一处理 401/403
- 所有 API 函数返回类型标注为对应 TypeScript 接口
- 聊天接口 POST JSON：`{question, document_ids?, session_id?}`
- 文档上传 `multipart/form-data`，axios `onUploadProgress` 驱动进度条

## 7. 错误处理

| HTTP 状态码 | 前端行为 |
|------------|---------|
| 401 | 清除 token，路由跳 `/login` |
| 403 | `ElMessage.warning` 展示 `detail` 内容 |
| 400 | `ElMessage.error` 展示 `detail` |
| 网络/超时 | `ElMessage.error("网络异常，请重试")` |
| 后端 503（模型服务不可用） | 聊天页内嵌降级卡片，不弹全局 Toast |

## 8. 部署拓扑

```
docker-compose.yml

  frontend:
    build: ./frontend
    ports:
      - "8080:80"
    depends_on:
      - api
```

- Dockerfile：第一阶段 `node:20-alpine` 执行 `npm run build`，第二阶段 `nginx:alpine` 复制 dist + nginx.conf
- nginx 配置：`/` 指向静态文件目录，`/v1/` 反向代理到 `api:8000`
- 开发环境：`vite.config.ts` proxy `/v1` → `http://localhost:8000`，前端和后端各自独立启动

## 9. 测试策略

- **单元测试**：vitest，覆盖独立组件渲染、Pinia store 逻辑、API 模块（msw mock）
- **集成测试**：完整页面流程（登录 → 上传 → 聊天 → 删除），基于 vitest + jsdom
- E2E（Playwright）留作后续

## 10. 前置条件与已知问题

- 后端 `orchestrator.py:105` 存在 `del document_ids` 导致 UnboundLocalError 的 bug，聊天接口当前不可用，需在实施前端前修复
- 对象存储凭证需在 `.env` 中配置，前端开发时可用 MinIO（docker-compose 提供）
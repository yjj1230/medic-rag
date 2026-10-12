# Patient Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Vue 3 patient-facing web UI (login, consent, document management, cited chat) served by nginx, with backend JWT auth and phone-number login.

**Architecture:** Vue 3 SPA with Element Plus component library, Pinia state management, and axios HTTP client. Backend gains a JWT login endpoint and a patient-creation CLI script. Development uses Vite dev server with proxy; production serves static assets via an nginx container in the existing docker-compose.

**Tech Stack:** Vue 3 (Composition API), TypeScript, Vite, Element Plus, Pinia, Vue Router, axios, pyjwt, bcrypt, nginx.

**Design Spec:** `docs/superpowers/specs/2026-09-10-patient-frontend-design.md`

---

## File Structure

```
frontend/
  src/
    api/
      index.ts        # axios instance + interceptors
      auth.ts         # POST /v1/auth/login
      consents.ts     # consent CRUD
      documents.ts    # upload / list / download / delete
      chat.ts         # POST /v1/chat/answers
    stores/
      auth.ts         # token, user info, login/logout
      consents.ts     # consent list + grant/revoke
      documents.ts    # document list + upload state
      chat.ts         # sessions, current messages, answer state
    views/
      LoginPage.vue
      ConsentPage.vue
      DocumentsPage.vue
      ChatPage.vue
    components/
      MessageBubble.vue
      CitationBadge.vue
      SafetyNoticeBar.vue
      DocumentStatusIcon.vue
    router/
      index.ts
    types/
      index.ts
    App.vue
    main.ts
  vite.config.ts
  Dockerfile
  nginx.conf
  index.html
  tsconfig.json
  package.json
src/medical_rag/
  api/routes/auth.py     # add POST /v1/auth/login
  auth/service.py        # dual-mode current_access_context
  auth/jwt.py            # NEW: JWT sign/verify
  cli.py                 # NEW: create-patient command
alembic/versions/0002_add_password_hash.py  # NEW
tests/
  integration/test_auth_routes.py  # modify: add login tests
  integration/test_chat_orchestration.py  # all 4 tests should pass after Task 0
```

---

## Task 0: Fix orchestrator.py bug (prerequisite)

**Files:**
- Modify: `src/medical_rag/chat/orchestrator.py:105`

- [ ] **Step 1: Remove the spurious `del document_ids` line**

Line 105 currently has `del document_ids` which was a stale "unused parameter" marker. The parameter IS used at line 118. Delete line 105.

Before:
```python
) -> AnswerResponse:
    del document_ids
    if not question.strip():
```

After:
```python
) -> AnswerResponse:
    if not question.strip():
```

- [ ] **Step 2: Run affected tests to confirm fix**

Run: `pytest tests/integration/test_chat_orchestration.py -q`

Expected: 4 passed

- [ ] **Step 3: Run full suite**

Run: `pytest -q`

Expected: 52 passed

---

## Task 1: Backend JWT auth infrastructure

**Files:**
- Create: `src/medical_rag/auth/jwt.py`
- Modify: `src/medical_rag/auth/service.py`
- Modify: `src/medical_rag/api/routes/auth.py`
- Modify: `pyproject.toml` (add `pyjwt` and `bcrypt` deps)
- Create: `src/medical_rag/cli.py`
- Create: `alembic/versions/0002_add_password_hash.py`

### Task 1a: Dependencies

- [ ] **Step 1: Add pyjwt and bcrypt to dependencies**

Edit `pyproject.toml`; add to `dependencies` list:
```toml
"pyjwt>=2.9,<3.0",
"bcrypt>=4.2,<5.0",
```

- [ ] **Step 2: Install**

Run: `pip install pyjwt bcrypt`

### Task 1b: Database migration

- [ ] **Step 3: Create migration file**

Create `alembic/versions/0002_add_password_hash.py`:

```python
"""Add password_hash to patients."""

from alembic import op
import sqlalchemy as sa

revision = "0002_add_password_hash"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("password_hash", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "password_hash")
```

- [ ] **Step 4: Add password_hash to Patient model**

Edit `src/medical_rag/domain/models.py`; add after `display_name`:

```python
password_hash: Mapped[str | None] = mapped_column(String(128))
```

- [ ] **Step 5: Run migration (if PostgreSQL is available) or verify the model imports**

Run: `python -c "from medical_rag.domain.models import Patient; print(Patient.__table__.columns.keys())"`

Expected: output includes `password_hash`

### Task 1c: JWT utilities

- [ ] **Step 6: Create auth/jwt.py**

Create `src/medical_rag/auth/jwt.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from medical_rag.core.config import Settings

_ALGORITHM = "HS256"


def _secret() -> str:
    settings = Settings.from_env()
    return settings.database_url  # reusing db_url as signing secret for pilot; production needs dedicated secret


def sign_access_token(tenant_id: UUID, patient_id: UUID, user_id: UUID) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "patient_id": str(patient_id),
        "iat": now,
        "exp": now + timedelta(days=7),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_access_token(token: str) -> dict[str, str]:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
        return {
            "tenant_id": payload["tenant_id"],
            "patient_id": payload["patient_id"],
            "user_id": payload["sub"],
        }
    except jwt.ExpiredSignatureError:
        raise ValueError("token expired")
    except jwt.InvalidTokenError:
        raise ValueError("invalid token")
```

### Task 1d: Login endpoint

- [ ] **Step 7: Add login route to api/routes/auth.py**

Insert after the router definition and before the consent routes. Add at the top of the file after the imports:

```python
from fastapi import HTTPException, status as http_status

from medical_rag.auth.jwt import sign_access_token
from medical_rag.domain.models import Patient
```

Then add before `@router.post("/{purpose}"...`:

```python
class LoginRequest(BaseModel):
    phone: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


_login_router = APIRouter(prefix="/v1/auth", tags=["auth"])


@_login_router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    import bcrypt

    patient = session.execute(
        select(Patient).where(Patient.external_user_id == payload.phone)
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="手机号或密码错误")
    if not patient.password_hash:
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="该账号未设置密码")
    if not bcrypt.checkpw(payload.password.encode("utf-8"), patient.password_hash.encode("utf-8")):
        raise HTTPException(status_code=http_status.HTTP_401_UNAUTHORIZED, detail="手机号或密码错误")
    now = datetime.now(UTC)
    token = sign_access_token(
        tenant_id=patient.tenant_id,
        patient_id=patient.id,
        user_id=patient.id,
    )
    return LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_at=now + timedelta(days=7),
    )
```

Also register the router in `app.py`. Edit `src/medical_rag/api/app.py`, add import:

```python
from medical_rag.api.routes.auth import router as auth_router, _login_router
```

And add `app.include_router(_login_router)` after `app.include_router(auth_router)`.

### Task 1e: Dual-mode current_access_context

- [ ] **Step 8: Update auth/service.py**

Replace the existing `current_access_context` with:

```python
from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from medical_rag.auth.jwt import verify_access_token
from medical_rag.auth.policy import AccessContext
from medical_rag.core.errors import AuthenticationRequired
from medical_rag.db.session import get_session
from medical_rag.domain.models import Patient


def current_access_context(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_tenant_id: UUID | None = Header(default=None, alias="X-Tenant-ID"),
    x_patient_id: UUID | None = Header(default=None, alias="X-Patient-ID"),
    x_user_id: UUID | None = Header(default=None, alias="X-User-ID"),
    session: Session = Depends(get_session),
) -> AccessContext:
    """Resolve identity from Bearer token (frontend) or trusted gateway headers (edge)."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = verify_access_token(token)
        except ValueError:
            raise AuthenticationRequired("token 无效或已过期")
        patient_id = UUID(claims["patient_id"])
        patient = session.execute(
            select(Patient).where(Patient.id == patient_id)
        ).scalar_one_or_none()
        if patient is None:
            raise AuthenticationRequired("患者不存在")
        return AccessContext(
            tenant_id=patient.tenant_id,
            patient_id=patient.id,
            user_id=patient.id,
        )

    if x_tenant_id and x_patient_id and x_user_id:
        return AccessContext(
            tenant_id=x_tenant_id,
            patient_id=x_patient_id,
            user_id=x_user_id,
        )
    raise AuthenticationRequired("缺少完整的身份上下文")
```

### Task 1f: Patient creation CLI

- [ ] **Step 9: Create cli.py**

Create `src/medical_rag/cli.py`:

```python
"""Admin CLI for managing pilot patient accounts."""

from __future__ import annotations

import secrets
from uuid import UUID

import bcrypt

from medical_rag.core.config import Settings
from medical_rag.db.session import create_session_factory
from medical_rag.domain.enums import RecordStatus
from medical_rag.domain.models import Patient  # noqa: F401 — model import for registry
from medical_rag.domain.models import Tenant as TenantModel


def create_patient(tenant_id: str, phone: str, display_name: str, password: str | None = None) -> None:
    settings = Settings.from_env()
    factory = create_session_factory(settings)
    with factory() as session:
        tenant = session.get(TenantModel, UUID(tenant_id))
        if tenant is None:
            raise SystemExit(f"租户 {tenant_id} 不存在")
        password_value = password or secrets.token_urlsafe(8)
        password_hash = bcrypt.hashpw(password_value.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        patient = Patient(
            tenant_id=tenant.id,
            external_user_id=phone,
            display_name=display_name,
            password_hash=password_hash,
            status=RecordStatus.ACTIVE,
        )
        session.add(patient)
        session.commit()
        print(f"患者已创建: {phone} ({display_name})")
        print(f"初始密码: {password_value}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("用法: python -m medical_rag.cli create-patient <tenant_id> <phone> <display_name> [password]")
        raise SystemExit(1)
    password = sys.argv[4] if len(sys.argv) > 4 else None
    create_patient(sys.argv[1], sys.argv[2], sys.argv[3], password)
```

- [ ] **Step 10: Verify CLI imports**

Run: `python -c "from medical_rag.cli import create_patient; print('ok')"`

Expected: exit code 0, prints `ok`

---

## Task 2: Frontend scaffolding

**Files:**
- Create: `frontend/` directory with Vite project

- [ ] **Step 1: Scaffold Vite + Vue 3 + TypeScript project**

Run:
```bash
mkdir frontend
cd frontend
npm create vite@latest . -- --template vue-ts
```

- [ ] **Step 2: Install dependencies**

Run:
```bash
cd frontend
npm install vue-router pinia axios element-plus @element-plus/icons-vue
npm install -D vitest @vue/test-utils jsdom msw
```

- [ ] **Step 3: Create vite.config.ts**

Replace `frontend/vite.config.ts`:

```typescript
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/v1": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
```

- [ ] **Step 4: Create src/types/index.ts**

```typescript
export interface ConsentRecord {
  id: string;
  purpose: "report_processing" | "external_model" | "health_education";
  policy_version: string;
  granted_at: string;
  revoked_at: string | null;
}

export interface DocumentRecord {
  id: string;
  filename: string;
  content_type: string;
  status: "uploaded" | "processing" | "ready" | "failed";
}

export interface DeletionReceipt {
  document_id: string;
  completed_layers: string[];
  failed_layers: string[];
}

export interface ChatCitation {
  source_id: string;
  title: string;
  version: string;
  location: string | null;
  url: string | null;
}

export interface ChatAnswer {
  status: string;
  sections: { title: string; text: string }[];
  citations: ChatCitation[];
  safety_notice: string | null;
  trace_id: string | null;
  session_id: string | null;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
}
```

- [ ] **Step 5: Create src/api/index.ts (axios instance)**

```typescript
import axios from "axios";
import { ElMessage } from "element-plus";

const api = axios.create({ baseURL: "" });

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("token");
      window.location.href = "/login";
      return Promise.reject(error);
    }
    if (error.response?.status === 403) {
      ElMessage.warning(error.response.data?.detail || "无操作权限");
      return Promise.reject(error);
    }
    if (!error.response) {
      ElMessage.error("网络异常，请重试");
      return Promise.reject(error);
    }
    return Promise.reject(error);
  }
);

export default api;
```

- [ ] **Step 6: Create src/api/auth.ts**

```typescript
import api from "./index";
import type { LoginResponse } from "../types";

export function login(phone: string, password: string) {
  return api.post<LoginResponse>("/v1/auth/login", { phone, password });
}
```

- [ ] **Step 7: Create src/api/consents.ts**

```typescript
import api from "./index";
import type { ConsentRecord } from "../types";

export function listConsents() {
  return api.get<ConsentRecord[]>("/v1/consents");
}

export function grantConsent(purpose: string) {
  return api.post<ConsentRecord>(`/v1/consents/${purpose}`);
}

export function revokeConsent(purpose: string) {
  return api.delete<ConsentRecord>(`/v1/consents/${purpose}`);
}
```

- [ ] **Step 8: Create src/api/documents.ts**

```typescript
import api from "./index";
import type { DocumentRecord, DeletionReceipt } from "../types";

export function uploadDocument(file: File, onProgress?: (pct: number) => void) {
  const form = new FormData();
  form.append("file", file);
  return api.post<DocumentRecord>("/v1/documents", form, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (evt) => {
      if (onProgress && evt.total) {
        onProgress(Math.round((evt.loaded / evt.total) * 100));
      }
    },
  });
}

export function listDocuments() {
  return api.get<DocumentRecord[]>("/v1/documents");
}

export function deleteDocument(id: string) {
  return api.delete<DeletionReceipt>(`/v1/documents/${id}`);
}

export function downloadDocumentUrl(id: string) {
  return `/v1/documents/${id}`;
}
```

- [ ] **Step 9: Create src/api/chat.ts**

```typescript
import api from "./index";
import type { ChatAnswer } from "../types";

export function askQuestion(question: string, documentIds: string[], sessionId?: string) {
  return api.post<ChatAnswer>("/v1/chat/answers", {
    question,
    document_ids: documentIds,
    session_id: sessionId || null,
  });
}
```

- [ ] **Step 10: Create src/stores/auth.ts**

```typescript
import { defineStore } from "pinia";
import { ref } from "vue";
import { login as apiLogin } from "../api/auth";

export const useAuthStore = defineStore("auth", () => {
  const token = ref(localStorage.getItem("token") || "");
  const loading = ref(false);
  const error = ref("");

  function setToken(t: string) {
    token.value = t;
    localStorage.setItem("token", t);
  }

  async function loginAction(phone: string, password: string) {
    loading.value = true;
    error.value = "";
    try {
      const res = await apiLogin(phone, password);
      setToken(res.data.access_token);
      return true;
    } catch (e: any) {
      error.value = e.response?.data?.detail || "登录失败";
      return false;
    } finally {
      loading.value = false;
    }
  }

  function logout() {
    token.value = "";
    localStorage.removeItem("token");
  }

  const isLoggedIn = () => !!token.value;

  return { token, loading, error, loginAction, logout, isLoggedIn, setToken };
});
```

- [ ] **Step 11: Create src/router/index.ts**

```typescript
import { createRouter, createWebHistory } from "vue-router";
import LoginPage from "../views/LoginPage.vue";
import ConsentPage from "../views/ConsentPage.vue";
import DocumentsPage from "../views/DocumentsPage.vue";
import ChatPage from "../views/ChatPage.vue";
import { useAuthStore } from "../stores/auth";

const routes = [
  { path: "/login", component: LoginPage, meta: { guest: true } },
  { path: "/consents", component: ConsentPage },
  { path: "/documents", component: DocumentsPage },
  { path: "/chat", component: ChatPage },
  { path: "/", redirect: "/chat" },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach((to, _from) => {
  const auth = useAuthStore();
  if (to.meta.guest) return true;
  if (!auth.isLoggedIn()) return "/login";
  return true;
});

export default router;
```

- [ ] **Step 12: Create src/main.ts**

```typescript
import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import App from "./App.vue";
import router from "./router";

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(ElementPlus);
app.mount("#app");
```

- [ ] **Step 13: Create src/App.vue**

```vue
<template>
  <el-container class="app-container">
    <el-header v-if="auth.isLoggedIn()">
      <el-menu mode="horizontal" router :default-active="route.path">
        <el-menu-item index="/chat">问答</el-menu-item>
        <el-menu-item index="/documents">我的报告</el-menu-item>
        <el-menu-item index="/consents">知情同意</el-menu-item>
        <el-menu-item class="logout-item" @click="doLogout">退出</el-menu-item>
      </el-menu>
    </el-header>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRouter, useRoute } from "vue-router";
import { useAuthStore } from "./stores/auth";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

function doLogout() {
  auth.logout();
  router.push("/login");
}
</script>

<style>
.logout-item {
  margin-left: auto !important;
}
.app-container {
  min-height: 100vh;
}
</style>
```

- [ ] **Step 14: Verify frontend compiles**

Run:
```bash
cd frontend
npx vue-tsc --noEmit
npm run build
```

Expected: TypeScript check passes, build succeeds (pages will be empty stubs that build fine as long as they export default component)

---

## Task 3: Login page

**Files:**
- Create: `frontend/src/views/LoginPage.vue`

- [ ] **Step 1: Create LoginPage.vue**

```vue
<template>
  <div class="login-wrapper">
    <el-card class="login-card">
      <h2>患者服务登录</h2>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="0" @submit.prevent="doLogin">
        <el-form-item prop="phone">
          <el-input v-model="form.phone" placeholder="手机号" size="large" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input v-model="form.password" type="password" placeholder="密码" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" :loading="auth.loading" native-type="submit" style="width: 100%">
            登录
          </el-button>
        </el-form-item>
        <p v-if="auth.error" class="error-text">{{ auth.error }}</p>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import type { FormInstance, FormRules } from "element-plus";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const formRef = ref<FormInstance>();

const form = reactive({ phone: "", password: "" });

const rules: FormRules = {
  phone: [
    { required: true, message: "请输入手机号", trigger: "blur" },
    { pattern: /^1\d{10}$/, message: "手机号格式不正确", trigger: "blur" },
  ],
  password: [
    { required: true, message: "请输入密码", trigger: "blur" },
    { min: 4, message: "密码至少 4 位", trigger: "blur" },
  ],
};

async function doLogin() {
  const valid = await formRef.value?.validate().catch(() => false);
  if (!valid) return;
  const ok = await auth.loginAction(form.phone, form.password);
  if (ok) router.push("/chat");
}
</script>

<style scoped>
.login-wrapper {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: calc(100vh - 60px);
}
.login-card {
  width: 400px;
}
h2 {
  text-align: center;
  margin-bottom: 24px;
}
.error-text {
  color: #f56c6c;
  text-align: center;
}
</style>
```

- [ ] **Step 2: Verify page renders**

Run: `cd frontend && npm run dev`

Navigate to http://localhost:5173/login

Expected: Login card with phone + password fields, validation rules for empty / format.

- [ ] **Step 3: Add stub pages for other views (Router resolves imports in router/index.ts)**

Create placeholder pages if not already done:

`frontend/src/views/ConsentPage.vue`:
```vue
<template><div>Consent page placeholder</div></template>
```

`frontend/src/views/DocumentsPage.vue`:
```vue
<template><div>Documents page placeholder</div></template>
```

`frontend/src/views/ChatPage.vue`:
```vue
<template><div>Chat page placeholder</div></template>
```

---

## Task 4: Consent page

**Files:**
- Create: `frontend/src/stores/consents.ts`
- Replace: `frontend/src/views/ConsentPage.vue`

- [ ] **Step 1: Create consent store**

`frontend/src/stores/consents.ts`:

```typescript
import { defineStore } from "pinia";
import { ref } from "vue";
import { listConsents, grantConsent, revokeConsent } from "../api/consents";
import type { ConsentRecord } from "../types";

export const useConsentStore = defineStore("consents", () => {
  const items = ref<ConsentRecord[]>([]);
  const loading = ref(false);

  async function load() {
    loading.value = true;
    try {
      const res = await listConsents();
      // Backend consent list API: if not available, mock. Actual API returns list.
      items.value = res.data;
    } finally {
      loading.value = false;
    }
  }

  async function grant(purpose: string) {
    const res = await grantConsent(purpose);
    const idx = items.value.findIndex((c) => c.purpose === purpose);
    if (idx >= 0) items.value[idx] = res.data;
    else items.value.push(res.data);
  }

  async function revoke(purpose: string) {
    const res = await revokeConsent(purpose);
    const idx = items.value.findIndex((c) => c.purpose === purpose);
    if (idx >= 0) items.value[idx] = res.data;
  }

  function statusFor(purpose: string): "granted" | "revoked" | "unknown" {
    const record = items.value.find((c) => c.purpose === purpose);
    if (!record) return "unknown";
    return record.revoked_at ? "revoked" : "granted";
  }

  return { items, loading, load, grant, revoke, statusFor };
});
```

- [ ] **Step 2: Create ConsentPage.vue**

```vue
<template>
  <div class="consent-page">
    <h2>知情同意</h2>
    <el-row :gutter="16" v-loading="store.loading">
      <el-col :span="8" v-for="p in purposes" :key="p.key">
        <el-card class="consent-card">
          <template #header>
            <span>{{ p.label }}</span>
          </template>
          <p>{{ p.desc }}</p>
          <p>状态：<el-tag :type="store.statusFor(p.key) === 'granted' ? 'success' : 'info'">
            {{ store.statusFor(p.key) === 'granted' ? '已同意' : store.statusFor(p.key) === 'revoked' ? '已撤回' : '未设置' }}
          </el-tag></p>
          <template #footer>
            <el-button
              v-if="store.statusFor(p.key) !== 'granted'"
              type="primary"
              @click="store.grant(p.key)"
            >
              同意
            </el-button>
            <el-popconfirm
              v-else
              title="确定要撤回同意吗？撤回后将无法使用相关服务。"
              @confirm="store.revoke(p.key)"
            >
              <template #reference>
                <el-button type="danger" plain>撤回</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from "vue";
import { useConsentStore } from "../stores/consents";

const store = useConsentStore();

const purposes = [
  { key: "report_processing", label: "报告处理", desc: "允许系统解析并存储您上传的医疗报告" },
  { key: "external_model", label: "外部模型调用", desc: "允许系统将报告内容发送给外部 AI 模型进行回答生成" },
  { key: "health_education", label: "健康教育", desc: "允许系统基于公共医学知识为您提供健康教育内容" },
];

onMounted(() => store.load());
</script>

<style scoped>
.consent-page h2 { margin-bottom: 16px; }
.consent-card { height: 240px; }
</style>
```

- [ ] **Step 3: Note — the consent list API GET /v1/consents doesn't exist yet**

The backend has POST and DELETE `/v1/consents/{purpose}` but no `GET /v1/consents`. Add it.

Edit `src/medical_rag/api/routes/auth.py`, add after the existing routes:

```python
from sqlalchemy import select
from medical_rag.domain.models import Consent

@router.get("", response_model=list[ConsentResponse])
def list_consents(
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
) -> list[ConsentResponse]:
    consents = session.scalars(
        select(Consent)
        .where(Consent.tenant_id == context.tenant_id, Consent.patient_id == context.patient_id)
        .order_by(Consent.granted_at.desc())
    ).all()
    return [
        ConsentResponse(
            id=str(c.id),
            purpose=c.purpose,
            policy_version=c.policy_version,
            granted_at=c.granted_at,
            revoked_at=c.revoked_at,
        )
        for c in consents
    ]
```

Also in `src/medical_rag/api/routes/auth.py` top, add `from sqlalchemy.orm import Session` if not already, and `from medical_rag.db.session import get_session`.

---

## Task 5: Documents page

**Files:**
- Create: `frontend/src/stores/documents.ts`
- Replace: `frontend/src/views/DocumentsPage.vue`

### Task 5a: Backend — add GET /v1/documents list endpoint

- [ ] **Step 1: Add document listing to documents route**

Edit `src/medical_rag/api/routes/documents.py`; add after the existing routes:

```python
@router.get("", response_model=list[DocumentResponse])
def list_documents(
    context: AccessContext = Depends(current_access_context),
    session: Session = Depends(get_session),
) -> list[DocumentResponse]:
    documents = session.scalars(
        select(Document).where(
            Document.tenant_id == context.tenant_id,
            Document.patient_id == context.patient_id,
            Document.status != DocumentStatus.DELETED,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc())
    ).all()
    return [
        DocumentResponse(
            id=doc.id,
            filename=doc.filename,
            content_type=doc.content_type,
            status=doc.status.value,
        )
        for doc in documents
    ]
```

### Task 5b: Frontend

- [ ] **Step 2: Create document store**

`frontend/src/stores/documents.ts`:

```typescript
import { defineStore } from "pinia";
import { ref } from "vue";
import { uploadDocument, listDocuments, deleteDocument, downloadDocumentUrl } from "../api/documents";
import type { DocumentRecord, DeletionReceipt } from "../types";

export const useDocumentStore = defineStore("documents", () => {
  const items = ref<DocumentRecord[]>([]);
  const loading = ref(false);
  const uploading = ref(false);

  async function load() {
    loading.value = true;
    try { items.value = (await listDocuments()).data; }
    finally { loading.value = false; }
  }

  async function upload(file: File, onProgress?: (pct: number) => void) {
    uploading.value = true;
    try {
      const res = await uploadDocument(file, onProgress);
      items.value.unshift(res.data);
      return res.data;
    } finally {
      uploading.value = false;
    }
  }

  async function remove(id: string): Promise<DeletionReceipt> {
    const res = await deleteDocument(id);
    items.value = items.value.filter((d) => d.id !== id);
    return res.data;
  }

  function downloadUrl(id: string) {
    return downloadDocumentUrl(id);
  }

  return { items, loading, uploading, load, upload, remove, downloadUrl };
});
```

- [ ] **Step 3: Create DocumentsPage.vue**

```vue
<template>
  <div class="documents-page">
    <h2>我的报告</h2>

    <el-upload
      drag
      :auto-upload="false"
      :on-change="handleChange"
      :show-file-list="false"
      accept=".pdf,.png,.jpg,.jpeg,.webp,.doc,.docx,.xls,.xlsx"
    >
      <el-icon class="upload-icon"><UploadFilled /></el-icon>
      <div>拖拽文件到此处，或点击上传</div>
      <template #tip>
        <div class="upload-tip">支持 PDF / 图片 / Word / Excel，单个文件不超过 50 MiB</div>
      </template>
    </el-upload>

    <el-progress v-if="uploadProgress > 0 && uploadProgress < 100" :percentage="uploadProgress" />

    <el-table :data="store.items" v-loading="store.loading" style="margin-top: 16px">
      <el-table-column prop="filename" label="文件名" />
      <el-table-column prop="content_type" label="类型" width="120" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.status)">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button size="small" @click="download(row.id)">下载</el-button>
          <el-popconfirm title="确定要删除这份报告吗？删除后不可恢复。" @confirm="doDelete(row.id)">
            <template #reference>
              <el-button size="small" type="danger" plain>删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { UploadFilled } from "@element-plus/icons-vue";
import { useDocumentStore } from "../stores/documents";
import type { UploadFile } from "element-plus";

const store = useDocumentStore();
const uploadProgress = ref(0);

const MAX_SIZE = 50 * 1024 * 1024;
const ALLOWED = [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".doc", ".docx", ".xls", ".xlsx"];

function ext(name: string) {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function handleChange(file: UploadFile) {
  const raw = file.raw;
  if (!raw) return;
  if (!ALLOWED.includes(ext(raw.name))) {
    ElMessage.warning("不支持的文件类型");
    return;
  }
  if (raw.size > MAX_SIZE) {
    ElMessage.warning("文件大小不能超过 50 MiB");
    return;
  }
  uploadProgress.value = 0;
  store.upload(raw, (pct) => { uploadProgress.value = pct; }).then(() => {
    uploadProgress.value = 0;
    ElMessage.success("上传成功");
  }).catch(() => {
    uploadProgress.value = 0;
    ElMessage.error("上传失败");
  });
}

function download(id: string) {
  const token = localStorage.getItem("token") || "";
  const a = document.createElement("a");
  a.href = store.downloadUrl(id);
  // Attach token as query param since the download is a navigation request without Authorization header in src attr
  // But the backend reads Authorization header. For inline download, use fetch + blob.
  fetch(store.downloadUrl(id), { headers: { Authorization: `Bearer ${token}` } })
    .then((res) => res.blob())
    .then((blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      a.click();
      URL.revokeObjectURL(url);
    });
}

async function doDelete(id: string) {
  try {
    const receipt = await store.remove(id);
    const msg = receipt.failed_layers.length
      ? `部分删除完成。成功: ${receipt.completed_layers.join(', ')}, 失败: ${receipt.failed_layers.join(', ')}`
      : "删除完成";
    ElMessage.success(msg);
  } catch {
    ElMessage.error("删除失败");
  }
}

function statusLabel(s: string) {
  const map: Record<string, string> = {
    uploaded: "已上传",
    processing: "处理中",
    ready: "就绪",
    failed: "失败",
  };
  return map[s] || s;
}

function statusTag(s: string): string {
  const map: Record<string, string> = {
    uploaded: "info",
    processing: "warning",
    ready: "success",
    failed: "danger",
  };
  return map[s] || "info";
}

onMounted(() => store.load());
</script>

<style scoped>
.documents-page h2 { margin-bottom: 16px; }
.upload-icon { font-size: 48px; }
.upload-tip { color: #909399; font-size: 12px; margin-top: 8px; }
</style>
```

---

## Task 6: Chat page

**Files:**
- Create: `frontend/src/stores/chat.ts`
- Create: `frontend/src/components/MessageBubble.vue`
- Create: `frontend/src/components/CitationBadge.vue`
- Create: `frontend/src/components/SafetyNoticeBar.vue`
- Replace: `frontend/src/views/ChatPage.vue`

- [ ] **Step 1: Create chat store**

`frontend/src/stores/chat.ts`:

```typescript
import { defineStore } from "pinia";
import { ref } from "vue";
import { askQuestion } from "../api/chat";
import type { ChatAnswer, ChatCitation } from "../types";

export interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  citations?: ChatCitation[];
  status?: string;
  safetyNotice?: string | null;
  traceId?: string | null;
}

export const useChatStore = defineStore("chat", () => {
  const messages = ref<Message[]>([]);
  const loading = ref(false);
  const error = ref("");
  const sessionId = ref<string | null>(null);

  async function send(question: string, documentIds: string[] = []) {
    messages.value.push({ role: "user", content: question });
    loading.value = true;
    error.value = "";
    try {
      const res = await askQuestion(question, documentIds, sessionId.value || undefined);
      const data = res.data;
      sessionId.value = data.session_id;

      let content = "";
      let citations: ChatCitation[] = [];

      if (data.status === "answered") {
        content = data.sections.map((s) => `**${s.title}**\n${s.text}`).join("\n\n");
        citations = data.citations;
      } else {
        content = data.sections.map((s) => s.text).join("\n");
      }

      messages.value.push({
        role: data.status === "answered" ? "assistant" : "system",
        content,
        citations,
        status: data.status,
        safetyNotice: data.safety_notice,
        traceId: data.trace_id,
      });
    } catch (e: any) {
      error.value = e.response?.data?.detail || "请求失败";
    } finally {
      loading.value = false;
    }
  }

  return { messages, loading, error, sessionId, send };
});
```

- [ ] **Step 2: Create CitationBadge.vue**

`frontend/src/components/CitationBadge.vue`:

```vue
<template>
  <el-popover trigger="hover" placement="top" :width="320">
    <template #reference>
      <span class="citation-badge">{{ label }}</span>
    </template>
    <div class="citation-detail">
      <p><strong>{{ citation.title }}</strong></p>
      <p>版本：{{ citation.version }}</p>
      <p v-if="citation.location">位置：{{ citation.location }}</p>
      <p v-if="citation.url">
        <a :href="citation.url" target="_blank">查看原文</a>
      </p>
    </div>
  </el-popover>
</template>

<script setup lang="ts">
import type { ChatCitation } from "../types";

defineProps<{ citation: ChatCitation; label: string }>();
</script>

<style scoped>
.citation-badge {
  display: inline-block;
  background: #ecf5ff;
  color: #409eff;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 12px;
  cursor: pointer;
  margin: 0 2px;
}
.citation-detail { font-size: 13px; line-height: 1.6; }
</style>
```

- [ ] **Step 3: Create SafetyNoticeBar.vue**

`frontend/src/components/SafetyNoticeBar.vue`:

```vue
<template>
  <el-alert
    :type="alertType"
    :closable="false"
    show-icon
    :title="title"
    :description="notice"
  />
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ status: string; notice: string }>();

const alertType = computed(() => {
  if (props.status === "safe_template") return "warning";
  if (props.status === "degraded") return "info";
  if (props.status === "insufficient_evidence") return "info";
  return "info";
});

const title = computed(() => {
  if (props.status === "safe_template") return "安全提示";
  if (props.status === "degraded") return "服务提示";
  if (props.status === "insufficient_evidence") return "暂无法回答";
  return "";
});
</script>
```

- [ ] **Step 4: Create MessageBubble.vue**

`frontend/src/components/MessageBubble.vue`:

```vue
<template>
  <div :class="['message-row', msg.role === 'user' ? 'user-row' : 'assistant-row']">
    <div :class="['message-bubble', msg.role]">
      <div v-if="msg.status && msg.status !== 'answered' && msg.safetyNotice">
        <SafetyNoticeBar :status="msg.status" :notice="msg.safetyNotice" />
      </div>
      <div v-html="renderContent(msg.content)" class="message-text"></div>
      <div v-if="msg.citations && msg.citations.length" class="citations-list">
        <CitationBadge
          v-for="(c, i) in msg.citations"
          :key="i"
          :citation="c"
          :label="`[${i + 1}]`"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from "vue-router";
import CitationBadge from "./CitationBadge.vue";
import SafetyNoticeBar from "./SafetyNoticeBar.vue";
import type { Message } from "../stores/chat";

const props = defineProps<{ msg: Message }>();

function renderContent(text: string) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}
</script>

<style scoped>
.message-row {
  display: flex;
  margin-bottom: 16px;
}
.user-row { justify-content: flex-end; }
.assistant-row { justify-content: flex-start; }
.message-bubble {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 8px;
}
.message-bubble.user { background: #ecf5ff; }
.message-bubble.assistant, .message-bubble.system { background: #f5f5f5; }
.message-text { line-height: 1.7; }
.citations-list { margin-top: 8px; }
</style>
```

- [ ] **Step 5: Create ChatPage.vue**

```vue
<template>
  <div class="chat-page">
    <div class="chat-main">
      <div class="messages-container" ref="scrollRef">
        <div v-if="store.messages.length === 0" class="empty-chat">
          <p>欢迎使用患者健康助手</p>
          <p class="hint">请输入您想了解的医学问题</p>
        </div>
        <MessageBubble v-for="(msg, i) in store.messages" :key="i" :msg="msg" />
      </div>

      <div class="input-area">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          placeholder="请输入问题..."
          @keydown.enter.exact.prevent="doSend"
        />
        <el-button
          type="primary"
          :loading="store.loading"
          @click="doSend"
          style="margin-top: 8px"
        >
          发送
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from "vue";
import { useChatStore } from "../stores/chat";
import MessageBubble from "../components/MessageBubble.vue";

const store = useChatStore();
const question = ref("");
const scrollRef = ref<HTMLElement>();

async function doSend() {
  const q = question.value.trim();
  if (!q || store.loading) return;
  question.value = "";
  await store.send(q, []);
  await nextTick();
  scrollRef.value?.scrollTo({ top: scrollRef.value.scrollHeight, behavior: "smooth" });
}
</script>

<style scoped>
.chat-page {
  max-width: 900px;
  margin: 0 auto;
}
.chat-main {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 140px);
}
.messages-container {
  flex: 1;
  overflow-y: auto;
  padding: 16px 0;
}
.empty-chat { text-align: center; color: #909399; margin-top: 120px; }
.hint { font-size: 13px; margin-top: 4px; }
.input-area { padding: 8px 0; border-top: 1px solid #ebeef5; }
</style>
```

---

## Task 7: Docker deployment

**Files:**
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Create nginx.conf**

`frontend/nginx.conf`:

```nginx
server {
    listen 80;
    server_name localhost;

    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /v1/ {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

- [ ] **Step 2: Create Dockerfile**

`frontend/Dockerfile`:

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

- [ ] **Step 3: Add frontend service to docker-compose.yml**

Add to `docker-compose.yml`:

```yaml
  api:
    build:
      context: .
      dockerfile: Dockerfile  # assumes backend Dockerfile exists; for now manual uvicorn
    # ...existing or placeholder

  frontend:
    build: ./frontend
    ports:
      - "8080:80"
    depends_on:
      - api
```

Note: The existing `docker-compose.yml` does not have an `api` service for the FastAPI app; only infrastructure services. The simplest path: for development, run the backend with `uvicorn` separately (Vite proxies to it). For future production, add an `api` Docker service. Since there is no `api` Dockerfile yet, the `depends_on` will be informational.

- [ ] **Step 4: Build and verify**

Run: `cd frontend && docker build -t medical-rag-frontend .`

Expected: Image builds successfully.

---

## Task 8: End-to-end integration validation

- [ ] **Step 1: Fix the consent GET route is registered properly**

Verify `src/medical_rag/api/routes/auth.py` has `@router.get("")` defined after the existing routes and that it references all needed imports (`select`, `Consent`, `Session`, `get_session`, `current_access_context`).

- [ ] **Step 2: Start backend dev server**

Run:
```bash
pip install -e .
uvicorn medical_rag.api.app:create_app --factory --reload
```

- [ ] **Step 3: Create a patient account via CLI**

First, ensure a tenant exists (seed if needed). Run:

```bash
python -c "
from medical_rag.core.config import Settings
from medical_rag.db.session import create_session_factory
from medical_rag.domain.enums import RecordStatus
from medical_rag.domain.models import Tenant
settings = Settings.from_env()
factory = create_session_factory(settings)
with factory() as s:
    t = s.execute('SELECT id FROM tenants LIMIT 1').scalar_one_or_none()
    if not t: 
        tenant = Tenant(name='测试医院', status=RecordStatus.ACTIVE)
        s.add(tenant)
        s.commit()
        print(f'Created tenant {tenant.id}')
"
```

Then create a patient:

```bash
python -m medical_rag.cli create-patient <tenant_id> 13800138000 测试患者 test1234
```

- [ ] **Step 4: Start frontend dev server**

Run: `cd frontend && npm run dev`

- [ ] **Step 5: Manual smoke test**

1. Open http://localhost:5173 → should redirect to /login
2. Login with phone 13800138000 / password test1234 → should redirect to /chat
3. Navigate to /consents → should show three consent cards, can grant/revoke
4. Navigate to /documents → upload a test PDF, see it in the list
5. Navigate to /chat → ask "血红蛋白是什么", should get cited answer or safe degradation message
6. Logout → should clear token and redirect to /login

- [ ] **Step 6: Run full backend test suite**

Run: `pytest -q`

Expected: 52 passed

---

## Execution Order

```text
Task 0  Fix orchestrator bug
   ↓
Task 1  Backend JWT auth + CLI
   ↓
Task 2  Frontend scaffolding
   ↓
Task 3  Login page      ──┤
Task 4  Consent page    ──┤  can run in parallel
Task 5  Documents page  ──┤  after Task 2
Task 6  Chat page       ──┘
   ↓
Task 7  Docker deployment
   ↓
Task 8  Integration validation
```
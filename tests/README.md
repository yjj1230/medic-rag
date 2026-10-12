# tests/

> 本目录在 2026-10 的 git 对象库损坏事故中整体丢失（旧 index 记录 84 个文件）。
> 这里是**重建的第一批**：拒答门禁回归网。其余 80 余个原测试文件仍需从原开发机取回
> （清单见 `docs/RAG恢复清单-2026-10-12.md`）。

## 当前内容

| 文件 | 覆盖内容 |
|---|---|
| `conftest.py` | 夹具：确定性假混合索引、记录/查询工厂、校准件、自动关闭线程池的 service 工厂 |
| `test_rejection_gate.py` | 32 例：门禁策略解析、构造期 fail-fast、**无关查询拒答**、词法绕门反例、阈值边界、降级跳过、校准身份 fail-closed、可观测性、校准件字段不变量、部署件可解析 |
| `test_startup_gate_selfcheck.py` | 5 例：启动期自检三档拦截 + 未配置放行 |

## 为什么这批测试值得先建

2026-10-12 的 120 条评测把「无关查询拒答准确率」量化为 **0.00** —— 五组消融配置
对 30 条无关查询全部硬返回完整 top_k。这是检索层唯一的 P0 生产级缺陷，而当时
**没有任何自动化测试守着它**。本套测试即该缺陷的回归网。

其中 `test_control_disabling_the_gate_would_return_unrelated_candidates` 是**变异对照**：
同一批无关候选，门禁一旦失效就全部返回——用来证明拒答测试不是空转。

## 运行

标准方式（依赖齐全的环境 / 容器内）：

```bash
python -m pytest tests/ -q
```

`pyproject.toml` 已配置 `pythonpath = ["src"]` 与 `testpaths = ["tests"]`，无需额外设置。

### 本机（Windows + 应用控制策略）的注意点

本机 Smart App Control 会拦截 SQLAlchemy 的 Cython 扩展：

```
ImportError: DLL load failed while importing _immutabledict_cy: 应用程序控制策略已阻止此文件。
```

SQLAlchemy 2.1 已移除纯 Python 回退，**2.0.x 保留回退**，因此本机验证使用：

```bash
# 一次性准备（镜像避免网络抖动）
python -m venv <venv>
<venv>/Scripts/python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --trusted-host mirrors.aliyun.com \
  pytest pytest-asyncio sqlalchemy==2.0.36 pydantic pydantic-settings \
  fastapi python-multipart pyjwt bcrypt httpx opensearch-py boto3 celery redis

<venv>/Scripts/python.exe -m pytest tests/ -q
```

> **容器/CI 不受此影响**，按 `pyproject.toml` 正常安装依赖即可，无需降级 SQLAlchemy。
> 这只是开发机安全策略导致的本地规避手段，不要写进依赖声明。

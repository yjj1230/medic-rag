#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""probe_citation_flake.py —— 量化「同一库内问题」的引用校验抖动率。

背景：probe_degraded_cause.py 显示同一问题（高血压生活方式）在 5 轮中出现
answered / degraded 交替，失败轮次的 safety_notice 精确等于
orchestrator.py:836 的 UnsupportedClaim 出口文案，且 answer_path="rag"、
gate_policy=null（因为该出口丢弃了检索诊断）。

本探针把 N 轮独立同问的 status/notice/延迟/trace 全部记录，用于判定：
  * 恒定失败  → 校验器规则对当前语料系统性过严（确定性缺陷）
  * 抖动失败  → 模型输出形态不稳（句末未锚定 / 段落缺引用 / 幻觉 ID）

产物：eval_out/probe_citation_flake.json
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_var, None)
os.environ.setdefault("NO_PROXY", "*")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001,S110
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from e2e_smoke_ab import _request, login  # noqa: E402

OUT = REPO_ROOT / "eval_out" / "probe_citation_flake.json"
Q = "高血压患者在生活方式上有哪些注意事项？"
N = 8
TIMEOUT = 300.0

rows: list[dict] = []
token = login()
print("登录 OK", flush=True)

for i in range(1, N + 1):
    started = time.perf_counter()
    status, resp = _request("POST", "/v1/chat/answers", body={"question": Q}, token=token, timeout=TIMEOUT)
    elapsed = round(time.perf_counter() - started, 1)
    resp = resp if isinstance(resp, dict) else {}
    diag = resp.get("diagnostics") or {}
    row = {
        "i": i,
        "http": status,
        "latency_s": elapsed,
        "status": resp.get("status"),
        "citations": len(resp.get("citations") or []),
        "session_id": resp.get("session_id"),
        "safety_notice": resp.get("safety_notice"),
        "answer_path": diag.get("answer_path"),
        "gate_policy": diag.get("gate_policy"),
        "trace_id": resp.get("trace_id"),
        "text_head": (resp.get("sections") or [{}])[0].get("text", "")[:80] if resp.get("sections") else "",
    }
    rows.append(row)
    print(f"[{i}/{N}] {row['status']:20s} cites={row['citations']} {elapsed}s "
          f"path={row['answer_path']} notice={str(row['safety_notice'])[:24]}", flush=True)

counts = Counter(r["status"] for r in rows)
lats = [r["latency_s"] for r in rows]
summary = {
    "question": Q,
    "n": N,
    "status_counts": dict(counts),
    "answered": counts.get("answered", 0),
    "degraded": counts.get("degraded", 0),
    "flake_rate": round(counts.get("degraded", 0) / N, 3),
    "latency_median_s": round(statistics.median(lats), 1),
    "latency_min_s": min(lats),
    "latency_max_s": max(lats),
    "rows": rows,
}
OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print("\n" + "=" * 70)
print(f"answered={summary['answered']} degraded={summary['degraded']} "
      f"flake_rate={summary['flake_rate']:.0%}")
print(f"延迟 median={summary['latency_median_s']}s "
      f"min={summary['latency_min_s']}s max={summary['latency_max_s']}s")
print(f"报告：{OUT}")

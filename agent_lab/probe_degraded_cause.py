#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""probe_degraded_cause.py —— 定位 D-01/D-04 的 degraded 根因。

连续 3 次问同一个库内问题（各自新会话）+ 一轮同会话追问，逐次记录
status / 延迟 / diagnostics（重点 answer_path、门禁字段、trace_id）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)
os.environ.setdefault("NO_PROXY", "*")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from e2e_smoke_ab import _request, login  # noqa: E402

OUT = REPO_ROOT / "eval_out" / "probe_degraded.json"
Q = "高血压患者在生活方式上有哪些注意事项？"
FOLLOWUP = "那它的副作用呢？"

rows = []
token = login()


def ask(q, sid=None):
    body = {"question": q}
    if sid:
        body["session_id"] = sid
    t = time.perf_counter()
    st, resp = _request("POST", "/v1/chat/answers", body=body, token=token, timeout=300)
    el = round(time.perf_counter() - t, 1)
    resp = resp if isinstance(resp, dict) else {"_raw": str(resp)[:300]}
    diag = resp.get("diagnostics") or {}
    return {
        "q": q, "http": st, "latency_s": el,
        "status": resp.get("status"),
        "citations": len(resp.get("citations") or []),
        "session_id": resp.get("session_id"),
        "safety_notice": resp.get("safety_notice"),
        "answer_path": diag.get("answer_path"),
        "gate_policy": diag.get("gate_policy"),
        "gate_skipped_reason": diag.get("gate_skipped_reason"),
        "embedding_degraded": diag.get("embedding_degraded"),
        "vector_error": diag.get("vector_error"),
        "rerank_error": diag.get("rerank_error"),
        "stage_ms": diag.get("stage_elapsed_ms"),
        "trace_id": resp.get("trace_id"),
        "text_head": (resp.get("sections") or [{}])[0].get("text", "")[:100]
        if resp.get("sections") else None,
    }


for i in range(3):
    r = ask(Q)
    rows.append({"round": f"independent-{i+1}", **r})
    print(f"[indep-{i+1}] http={r['http']} status={r['status']} {r['latency_s']}s "
          f"cites={r['citations']} path={r['answer_path']} sid={r['session_id']}")

# 一轮会话：首答 + 追问
first = ask(Q)
print(f"[turn-1] status={first['status']} sid={first['session_id']}")
second = ask(FOLLOWUP, sid=first["session_id"]) if first["session_id"] else None
if second:
    print(f"[turn-2] status={second['status']} path={second['answer_path']} "
          f"gate_skip={second['gate_skipped_reason']} notice={second['safety_notice']}")
rows.append({"round": "turn-1", **first})
if second:
    rows.append({"round": "turn-2", **second})

OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("报告:", OUT)

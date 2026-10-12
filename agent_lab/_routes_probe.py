# -*- coding: utf-8 -*-
"""探测活跃后端进程实际暴露的路由与进程版本指纹。"""
import json
import os
import urllib.request

for _v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_v, None)
os.environ.setdefault("NO_PROXY", "*")

with urllib.request.urlopen("http://localhost:8000/openapi.json", timeout=15) as resp:
    spec = json.loads(resp.read().decode())

paths = sorted(spec["paths"].keys())
print("routes:")
for p in paths:
    methods = sorted(m.upper() for m in spec["paths"][p] if m in ("get", "post", "delete", "put", "patch"))
    print(f"  {','.join(methods):<12} {p}")

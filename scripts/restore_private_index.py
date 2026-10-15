"""从取证快照恢复私有索引 ``medical-rag-private-local``。

存在的理由
----------
私有索引在一次「删除残留」验证（``test_run_after_private_cleanup_v2``）后被清空，
但评测集 v2.1 里有 30 条用例依赖私有语料：

* 10 条 ``quality/private``（患者 A 身份）——gold 证据必须能召回；
* 20 条 ``safety``（患者 B 身份）——``forbidden_evidence_ids`` 指向患者 A 的文档，
  期望 ``insufficient_evidence``，即「跨患者不得读」。

没有患者 A 的数据在索引里，safety 用例就会退化为「空索引自然为空」的空转断言，
隔离是否真的生效无法验证。因此恢复这份快照是复跑的前置条件，而不是可选步骤。

数据来源
--------
``eval_out/private_audit_raw.json`` 是当时对私有索引的一次完整审计导出，
保留了两索引文档的 ``_id`` 与 ``_source``（含 512 维向量与权限字段）。
它本是取证材料，此处作为唯一可用的语料来源——原始上传的 PDF（``blood_report.pdf``）
与其解析中间物都不在本机。

字段处理
--------
按目标索引**当前** mapping 过滤 ``_source``：快照里带若干历史解析中间字段
（``raw_text`` / ``clean_text`` / ``quality_flags`` / ``extractor_version``），
它们不在现行索引契约内，写入只会造成 dynamic mapping 漂移。检索链路只消费
``text`` / ``embedding`` / 权限与版本字段，过滤不影响任何指标。

用法::

    python scripts/restore_private_index.py            # 恢复并校验
    python scripts/restore_private_index.py --dry-run  # 只打印将写入什么
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_SNAPSHOT = Path("eval_out/private_audit_raw.json")
DEFAULT_INDEX = "medical-rag-private-local"


def _load_snapshot(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"快照文件不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise SystemExit(f"快照格式异常（期望非空数组）: {path}")
    rows: list[dict[str, Any]] = []
    for entry in payload:
        if not isinstance(entry, dict) or "_id" not in entry or "_source" not in entry:
            raise SystemExit("快照条目缺少 _id/_source，拒绝在信息不全时恢复")
        rows.append({"_id": entry["_id"], "_source": entry["_source"]})
    return rows


def _target_fields(client: Any, index: str) -> set[str]:
    mapping = client.indices.get_mapping(index=index)
    props = mapping[index]["mappings"].get("properties", {})
    if not props:
        raise SystemExit(f"目标索引 {index} 无 mapping，拒绝盲写")
    return set(props.keys())


def _filtered_source(source: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key in allowed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="恢复私有索引（取证快照 → OpenSearch）")
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument(
        "--url",
        default=None,
        help="OpenSearch 地址；默认读应用 Settings（.env 的 OPENSEARCH_URL）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    args = parser.parse_args(argv)

    rows = _load_snapshot(Path(args.snapshot))

    from opensearchpy import OpenSearch

    url = args.url
    if url is None:
        from medical_rag.core.config import get_settings

        url = get_settings().opensearch_url

    client = OpenSearch(url, timeout=30)
    if not client.ping():
        raise SystemExit(f"OpenSearch 不可达: {url}")

    allowed = _target_fields(client, args.index)
    dropped: dict[str, int] = {}
    body: list[dict[str, Any]] = []
    for row in rows:
        source = _filtered_source(row["_source"], allowed)
        for key in row["_source"]:
            if key not in allowed:
                dropped[key] = dropped.get(key, 0) + 1
        body.append({"index": {"_index": args.index, "_id": row["_id"]}})
        body.append(source)

    print(f"快照条目: {len(rows)}")
    print(f"目标索引: {args.index}（{url}）")
    print(f"按 mapping 保留字段: {len(allowed)} 个")
    if dropped:
        print("丢弃的映射外字段: " + ", ".join(f"{k}×{v}" for k, v in sorted(dropped.items())))

    dims = {len(r["_source"].get("embedding") or []) for r in rows}
    print(f"向量维度: {sorted(dims)}")

    if args.dry_run:
        print("dry-run：未写入。")
        return 0

    response = client.bulk(body=body, refresh=True)
    if response.get("errors"):
        failures = [
            item["index"]
            for item in response["items"]
            if item.get("index", {}).get("status", 500) >= 300
        ]
        print(f"写入存在失败项: {failures[:3]}（共 {len(failures)} 条）", file=sys.stderr)
        return 1

    count = client.count(index=args.index).get("count")
    print(f"写入完成。当前 {args.index} 文档数: {count}")

    if count != len(rows):
        print(
            f"警告：文档数 {count} 与快照条目 {len(rows)} 不一致，请检查是否有历史残留。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

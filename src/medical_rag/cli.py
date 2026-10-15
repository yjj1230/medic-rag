"""用于管理试点患者账号的管理 CLI。"""
from __future__ import annotations
import secrets
from pathlib import Path
from uuid import UUID
import bcrypt
from medical_rag.core.config import Settings
from medical_rag.db.session import create_session_factory
from medical_rag.domain.enums import RecordStatus
from medical_rag.domain.models import Patient
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
            tenant_id=tenant.id, external_user_id=phone, display_name=display_name,
            password_hash=password_hash, status=RecordStatus.ACTIVE,
        )
        session.add(patient)
        session.commit()
        print(f"患者已创建: {phone} ({display_name})")
        print(f"初始密码: {password_value}")

def evaluate_document_quality(fixtures_dir: str, *, update_baseline: bool = False) -> None:
    """基于合成黄金集的发布门禁（Task 10）。

    通过时退出码为 0，当任一必需指标低于存储的黄金集基线、或任一权限/删除安全
    检查非零时退出码为 1。评测完全在内存中基于 fixture 进行；不触碰任何生产索引。

    ``baseline.json`` 是一份经评审、已入库的工件。它绝不在 FAIL 时被重写，在 PASS
    时同样不再被重写：每次通过都静默重建基线，恰好会抹掉这道门禁本就用来捕捉的那些
    回归。要刻意编写新基线，请传 ``update_baseline=True``（CLI 为 ``--update-baseline``）。
    """
    import json

    from medical_rag.eval import golden

    directory = Path(fixtures_dir)
    fixture_data, load_errors = golden.load_fixture_data(directory)
    baseline_path = directory / golden.BASELINE_FILE

    # 存在已存储基线时，用一次全新运行与*加载的*存储基线比较。
    stored = golden.load_baseline_from(baseline_path)
    if load_errors:
        print("Golden-set report could not be produced:")
        for error in load_errors:
            print(f"    - {error}")
        raise SystemExit(1)

    baseline: golden.BaselineReport | None = None
    if stored is not None:
        try:
            baseline = golden.BaselineReport.from_dict(stored)
        except golden.FixtureSchemaError as exc:
            print(f"stored baseline {baseline_path} is invalid: {exc}")
            raise SystemExit(1) from exc

    # 单次确定性运行产出全新结果集；与存储的
    # 基线（可能来自更早的一次 PASS）比较，因此回归是真实的。
    results, run_errors = golden.run_fixtures(fixture_data)
    if not results:
        print("Golden-set report could not be produced:")
        for error in run_errors or ["no valid fixtures to evaluate"]:
            print(f"    - {error}")
        raise SystemExit(1)

    report = golden.compute_baseline(fixture_data, results=results)
    verdict = golden.release_gate(
        fixture_data, baseline=baseline, results=results
    )

    print("Golden-set document-quality report (synthetic / de-identified):")
    print(f"  RAG baselines: recall@K={report.chunk_recall_at_k:.3f} mrr={report.mrr:.3f} "
          f"citation={report.citation_accuracy:.3f} p95_latency_ms={report.p95_latency_ms:.3f}")
    print(f"  safety: permission_violations={report.permission_violations} "
          f"deletion_residuals={report.deletion_residuals}")
    for item in report.fixtures:
        print(f"    {item['fixture_id']}: anchor={item['anchor_accuracy']:.3f} "
              f"coverage={item['text_coverage']:.3f} table_retained={item['table_header_retention']} "
              f"status={item['quality_status']}")
    if verdict.reasons:
        print("  release gate: FAIL (exit 1)")
        for reason in verdict.reasons:
            print(f"    - {reason}")
        print("  baseline report: NOT rewritten (FAIL)")
        raise SystemExit(1)
    print("  release gate: PASS (exit 0)")
    if not update_baseline:
        print(f"  baseline report: unchanged ({baseline_path})")
        print("  pass --update-baseline to author a new baseline")
        raise SystemExit(0)
    baseline_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if stored is not None:
        print(f"  baseline report: UPDATED ({baseline_path})")
    else:
        print(f"  baseline report: CREATED ({baseline_path})")
    raise SystemExit(0)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m medical_rag.cli create-patient <tenant_id> <phone> <display_name> [password]")
        print("      python -m medical_rag.cli evaluate-document-quality --fixtures <dir> [--update-baseline]")
        raise SystemExit(1)
    command = sys.argv[1]
    if command == "evaluate-document-quality":
        argv = sys.argv[2:]
        if "--fixtures" in argv:
            index = argv.index("--fixtures") + 1
            fixtures = argv[index] if index < len(argv) else ""
        else:
            fixtures = ""
        if not fixtures:
            print("错误: evaluate-document-quality 需要 --fixtures <目录>")
            raise SystemExit(1)
        evaluate_document_quality(fixtures, update_baseline="--update-baseline" in argv)
    elif command == "create-patient":
        if len(sys.argv) < 5:
            print("用法: python -m medical_rag.cli create-patient <tenant_id> <phone> <display_name> [password]")
            raise SystemExit(1)
        password = sys.argv[5] if len(sys.argv) > 5 else None
        create_patient(sys.argv[2], sys.argv[3], sys.argv[4], password)
    else:
        print(f"未知命令: {command}")
        raise SystemExit(1)

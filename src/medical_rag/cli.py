"""Admin CLI for managing pilot patient accounts."""
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
    """Release-gate over the synthetic golden set (Task 10).

    Exits 0 on pass and 1 when any required metric is below the stored golden-set
    baseline or any permission/deletion safety check is non-zero.  Evaluation is
    entirely in-memory over the fixtures; no production index is touched.

    ``baseline.json`` is a reviewed, checked-in artifact.  It is never rewritten
    on a FAIL, and no longer rewritten on a PASS either: silently re-baselining
    on every pass would erase exactly the regressions the gate exists to catch.
    Pass ``update_baseline=True`` (CLI: ``--update-baseline``) to author a new
    baseline deliberately.
    """
    import json

    from medical_rag.eval import golden

    directory = Path(fixtures_dir)
    fixture_data, load_errors = golden.load_fixture_data(directory)
    baseline_path = directory / golden.BASELINE_FILE

    # Compare a FRESH run against the LOADED stored baseline when one exists.
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

    # Single deterministic run feeds the fresh set; compared against the stored
    # baseline (which may be from an earlier PASS), so regressions are real.
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

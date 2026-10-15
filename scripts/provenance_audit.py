"""Report chunk provenance completeness without printing source text or PHI."""

from __future__ import annotations

import argparse
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from medical_rag.core.config import Settings
from medical_rag.domain.models import EvidenceUnit, KnowledgeChunk


def _audit_rows(session: Session, scope: str) -> dict[str, int]:
    rows = []
    if scope in {"private", "all"}:
        rows.extend(("private", row) for row in session.scalars(select(EvidenceUnit)).all())
    if scope in {"public", "all"}:
        rows.extend(("public", row) for row in session.scalars(select(KnowledgeChunk)).all())
    result = {
        "records": len(rows),
        "missing_locator": 0,
        "missing_identity": 0,
        "missing_position": 0,
    }
    for kind, row in rows:
        if not getattr(row, "locator", None):
            result["missing_locator"] += 1
        if kind == "private" and not getattr(row, "document_version_id", None):
            result["missing_identity"] += 1
        if kind == "public" and not getattr(row, "source_id", None):
            result["missing_identity"] += 1
        locator = getattr(row, "locator", None) or {}
        if not any(
            locator.get(key)
            for key in ("page_number", "section_path", "block_id", "cell_range", "bbox", "source_anchor")
        ):
            result["missing_position"] += 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("private", "public", "all"), default="all")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    database_url = args.database_url or Settings(_env_file=None).database_url
    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        result = _audit_rows(session, args.scope)
    print(json.dumps({"scope": args.scope, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()

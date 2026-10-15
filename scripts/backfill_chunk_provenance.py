"""Safely backfill structured locators from existing database fields.

The script never invents a page or anchor.  Rows without any authoritative
position field are reported as unreconstructable and left unchanged.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from medical_rag.core.config import Settings
from medical_rag.domain.models import EvidenceUnit, KnowledgeChunk
from medical_rag.provenance.contracts import SourceLocator


def _locator_from_legacy(
    *,
    page_number: int | None,
    section_path: Iterable[str] = (),
    block_id: str | None = None,
    chunk_index: int | None = None,
    sheet_name: str | None = None,
    cell_range: str | None = None,
    source_anchor: str | None = None,
) -> SourceLocator | None:
    locator = SourceLocator(
        locator_type="xlsx_cell_range" if sheet_name else "pdf_text",
        page_number=page_number,
        section_path=tuple(section_path),
        block_id=block_id,
        chunk_index=chunk_index,
        sheet_name=sheet_name,
        cell_range=cell_range,
        source_anchor=source_anchor,
    )
    try:
        locator.validate()
    except ValueError:
        return None
    return locator


def _backfill_rows(session: Session, scope: str, dry_run: bool) -> dict[str, int]:
    counts = {"scanned": 0, "updated": 0, "already_present": 0, "unreconstructable": 0}
    if scope in {"private", "all"}:
        rows = session.scalars(select(EvidenceUnit).order_by(EvidenceUnit.id)).all()
        for row in rows:
            counts["scanned"] += 1
            if row.locator:
                counts["already_present"] += 1
                continue
            locator = _locator_from_legacy(
                page_number=row.page_number,
                section_path=row.section_path or (),
                block_id=row.parent_block_id,
                chunk_index=row.chunk_index,
                sheet_name=row.sheet_name,
                cell_range=row.cell_range,
                source_anchor=row.source_anchor,
            )
            if locator is None:
                counts["unreconstructable"] += 1
                continue
            counts["updated"] += 1
            if not dry_run:
                row.locator = locator.to_dict()
    if scope in {"public", "all"}:
        rows = session.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.id)).all()
        for row in rows:
            counts["scanned"] += 1
            if row.locator:
                counts["already_present"] += 1
                continue
            locator = _locator_from_legacy(
                page_number=row.page_number,
                section_path=(row.section,) if row.section else (),
            )
            if locator is None:
                counts["unreconstructable"] += 1
                continue
            counts["updated"] += 1
            if not dry_run:
                row.locator = locator.to_dict()
    if not dry_run:
        session.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("private", "public", "all"), default="all")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    database_url = args.database_url or Settings(_env_file=None).database_url
    engine = create_engine(database_url, pool_pre_ping=True)
    with Session(engine) as session:
        result = _backfill_rows(session, args.scope, args.dry_run)
    print(json.dumps({"scope": args.scope, "dry_run": args.dry_run, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()

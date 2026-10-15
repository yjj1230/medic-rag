# Chunk Provenance and Locator Design

**Goal:** Make every private or public RAG chunk traceable from an indexed hit back to its exact document version and source position, while preserving citation, deletion, reconciliation, and re-indexing behavior.

## Scope

This design covers the provenance path from parser output to `EvidenceUnit`, `KnowledgeChunk`, OpenSearch records, citations, deletion, and reconciliation. It does not change retrieval ranking or model prompting except where those components consume structured locator data.

## Canonical identity

The authoritative identity chain is:

```text
chunk_id / evidence_unit_id
  -> document_version_id
  -> document_id
  -> object storage key
```

For public knowledge the chain additionally carries `source_id` and `source_version`. `source_id` identifies a registered knowledge source; it must not be used as a substitute for a document identity when one source contains multiple files or revisions.

`document_version_id` and `evidence_unit_id` are the database identities. The OpenSearch `_id` is the string form of `evidence_unit_id` for private evidence and the stable knowledge chunk id for public knowledge. A chunk rebuild caused by parser, cleaner, chunker, extractor, embedding, or index-version changes must be distinguishable through the existing version fields and content hash.

## Structured locator

Add one shared, JSON-safe `SourceLocator` contract. It contains:

- `locator_type`: `pdf_text`, `pdf_image`, `docx_paragraph`, `xlsx_cell_range`, `xls_cell_range`, or `image_region`;
- `page_number` and optional `page_total`;
- `section_path`;
- `block_id`, `reading_order`, and `chunk_index`;
- optional `char_start` and `char_end` within the source block;
- optional `bbox` for page/image coordinates;
- optional `sheet_name` and `cell_range`;
- a deterministic `source_anchor` generated from the structured fields;
- a human-readable `display_location` generated only for presentation.

`display_location` is not authoritative. Retrieval, citation validation, deletion, and reconciliation use structured fields and IDs.

## Contract propagation

The same provenance fields must survive these boundaries without lossy remapping:

```text
ParsedBlock
 -> EvidenceUnitDraft
 -> EvidenceUnit / KnowledgeChunk
 -> IndexRecord / PublicIndexRecord
 -> SearchRecord / Evidence
 -> Citation / API response
```

Legacy `location` remains as a compatibility/display field, but new code must populate the structured locator first and derive `location` from it.

## Invariants

1. Every indexed chunk has a document/source identity and a stable chunk identity.
2. Every chunk has at least one usable locator: page, section/block, sheet/range, or image anchor.
3. A citation must point to the exact evidence record returned to the model.
4. A deleted document version cannot leave searchable chunks in either index.
5. A re-index operation cannot silently change document version, locator, or content hash.
6. Public records cannot contain private ACL fields; private records must contain tenant and patient scope.

## Compatibility

Existing nullable fields remain readable during migration. New writes use the complete contract. A backfill job reports records that cannot be reconstructed rather than inventing page or anchor values.

## Acceptance criteria

- PDF, DOCX, XLSX/XLS, and image fixtures round-trip from indexed chunk to source locator.
- Citation responses expose document/version and structured location fields.
- Deletion tests verify database, object-store, and OpenSearch residuals are all zero.
- Reconciliation identifies missing, stale, and version-mismatched records using provenance and content hash.
- Public and private index contract tests reject incomplete or cross-visibility records.

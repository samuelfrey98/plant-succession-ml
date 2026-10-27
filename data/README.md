# Data

This directory separates public sample data from private or raw project data.

- `sample/`: small sanitized example CSVs used by the original scaffold
- `public/`: sanitized metadata derived from the real monitoring archive
- `schema/`: schema CSVs and allowed values
- `raw/`: private source exports and Drive inventories; ignored by Git
- `processed/`: processed private outputs; ignored by Git

The public photo manifest intentionally omits Drive IDs and private links. Its 596 photo roles have
been visually reviewed; camera sequence alone is not treated as evidence of photo type. Re-running
`scripts/build_photo_manifest.py` preserves populated visual-review fields by stable `photo_id` while
refreshing source-derived metadata.

`public/observations.csv` contains one deterministic observation per non-empty site/date photo batch.
Thirty records have completed human review and 45 remain partially reviewed: their supported fields are
retained, while missing or uncertain historical values remain explicit. Historical Drive-archive records
preserve post-hoc estimates and uncertainty; newer ingestion-app observations use the same canonical schema
and append-only review provenance.

`public/field_notes.csv` preserves the source wording and scope of field notes. `public/site_checks.csv`
records only site-specific assessments explicitly supported by those notes. A site omitted from a note
remains unknown rather than being converted to a negative disturbance observation.

`public/analysis_ready_observations.csv` is a derived export rebuilt from the canonical observations,
site registry, and photo manifest by `scripts/manage_data_store.py export` or
`scripts/build_longitudinal_outputs.py`. It derives sequence, elapsed-time, cover-change, and completeness
fields without filling in missing ecological values; it is not stored as an independently editable table.

`data/raw/ingestion/ingestion.sqlite` is the canonical local workflow and metadata database. The CSVs in
`public/` are deterministic sanitized exports that belong under version control, and the private Google
Sheets workbook is an optional downstream mirror. Use `scripts/manage_data_store.py export` after review;
do not edit exported CSVs as the normal workflow. An intentional one-time replacement from validated CSVs
requires `scripts/manage_data_store.py import --replace`.

`scripts/import_legacy_archive_to_ingestion.py` performs the one-time Drive-archive registration used
by the local review app. It hard-links mirrored originals into ingestion batches, creates previews, and
records historical assistant and unknown pre-tracking provenance without duplicating reviewer identity.

Do not commit raw field photos, exact private coordinates, credentials, or full Drive exports unless they have been intentionally sanitized for public release.

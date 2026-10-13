# Data

This directory separates public sample data from private or raw project data.

- `sample/`: small sanitized example CSVs used by the original scaffold
- `public/`: sanitized metadata derived from the real monitoring archive
- `schema/`: schema CSVs and allowed values
- `raw/`: private source exports and Drive inventories; ignored by Git
- `processed/`: processed private outputs; ignored by Git

The public photo manifest intentionally omits Drive IDs and private links. Photo roles remain
`unclassified` until visual review; camera sequence alone is not treated as evidence of photo type.

`public/observations.csv` contains one deterministic observation shell per non-empty site/date
photo batch. These rows are marked `photo_archive_backfill` and remain pending until their photos
are reviewed. Re-running `scripts/build_observation_backfill.py` preserves populated review fields.

Do not commit raw field photos, exact private coordinates, credentials, or full Drive exports unless they have been intentionally sanitized for public release.

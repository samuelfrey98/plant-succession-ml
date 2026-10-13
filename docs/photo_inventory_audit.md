# Photo Inventory Audit

Audit date: 2026-10-13

## Archive Snapshot

- 497 HEIC files
- 2,651,013,757 bytes (2.47 GiB)
- 11 site folders
- 63 site/date folders
- 62 non-empty photo batches
- observation-folder date range: 2026-05-25 through 2026-07-20

No raw photo bytes, exact coordinates, private Drive IDs, or private Drive links are committed to the
public repository.

## Confirmed Corrections

Two Site 9 folders were uploaded with 2025 in their names. They were renamed in Drive to
2026-05-25 and 2026-06-01 after their camera-number continuity and the project timeline confirmed
the intended year.

Site 11 was confirmed as an intentional later-established monitoring site. It is named Sensitive Fern
Disturbance Slope in the registry and has photo batches dated 2026-07-03 and 2026-07-20.

## Open Upload Issue

The Site 3 folder for 2026-07-20 is empty. Neighboring camera filenames suggest that IMG_4697
through IMG_4705 may be the missing batch, but that inference has not been used to create manifest
rows. The empty folder remains visible in `data/public/photo_batch_summary.csv`.

## Classification Policy

The archive's original filenames are preserved in `source_filename`. The public manifest assigns stable
photo and observation IDs plus a deterministic logical path, but it does not rename or overwrite the
Drive files. Every photo currently uses `photo_type=unclassified` and
`classification_status=pending_visual_review`.

Photo IDs derive from the site, date, and original filename rather than capture position. Duplicate
source filenames receive a short one-way hash suffix, so adding a missing photo later does not renumber
the rest of its batch or expose the private Drive file ID.

Capture order is not considered sufficient evidence for photo type. Wide, quadrat, close-up, and
context labels will be added only after visual confirmation.

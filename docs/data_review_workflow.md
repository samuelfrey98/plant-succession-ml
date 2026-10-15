# Photo-Backed Data Review Workflow

The project uses two complementary working surfaces:

- Google Drive stores the private raw photo archive and the editable review workbook.
- GitHub stores code, schemas, validation, and sanitized CSV snapshots.

The private workbook is named `Plant Succession ML - Data Review`. Its table layouts mirror the public
CSV files so a completed review can be transferred without changing identifiers or column meanings.
Private Drive URLs and exact coordinates must stay out of the repository.

## Review Unit

Review one `observation_id` at a time. Each observation represents one non-empty site/date photo batch.
The review sequence is:

1. Open every photo in the batch.
2. Assign a photo role only from visual evidence, not file order.
3. Mark ambiguous roles as `ambiguous`; do not guess.
4. Estimate cover in rounded increments only when a quadrat or comparable view supports it.
5. Leave taxon fields blank when the photographs do not support a defensible label.
6. Record what was inferred, what remains uncertain, and whether a disturbance event is actually visible.
7. Copy the reviewed values into the matching public CSV rows and run validation.

Field notes follow a separate normalization path:

1. Preserve the original wording in `field_notes`.
2. Create a `site_checks` row only for each site and disturbance type explicitly assessed.
3. Create a `disturbance_events` row only from a positive `observed_event` check.
4. Keep explicit negative checks as `none_seen`; do not create an event for them.
5. Leave sites not named in the note unknown. A route note is never evidence about sites not visited.
6. Keep phenology, taxon, and general notes out of `disturbance_events` unless they independently
   document a disturbance.

## Status Meanings

- `pending_visual_review`: the batch or photo has not been inspected.
- `partially_reviewed`: photo roles or some observation fields are supported, but important fields remain unresolved.
- `reviewed`: the observation has been checked and all supportable fields are recorded.
- `verified`: the individual photo role has been visually confirmed.
- `ambiguous`: the individual photo role cannot be assigned confidently.

## Synchronization Rule

The Google Sheets workbook is the convenient private editing surface; the committed CSVs are the reproducible
public snapshot. A batch is complete only after both representations agree and
`python3 scripts/validate_csvs.py` passes. Bulk raw photos remain in Drive and are never renamed or
overwritten by this workflow. Rebuilding the manifest preserves completed visual-review fields by
stable `photo_id`.

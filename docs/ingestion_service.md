# Ingestion Service

The ingestion service replaces the slow Google Forms photo-upload workflow with a review
queue. It handles new monitoring rounds without requiring manual file renaming or direct CSV editing.

## Run It

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/run_ingestion_app.py
```

Open `http://127.0.0.1:8765`. The service binds only to the local loopback interface by default.

## Pipeline

```text
Browser upload
      |
      v
private upload cache + EXIF site/date suggestion
      |
      v
human site/date confirmation
      |
      v
per-photo batch-integrity gate
      |
      v
private staging + SHA-256 duplicate check
      |
      v
EXIF extraction + preview generation
      |
      v
conservative candidate labeling
      |
      v
human review queue
      |
      v
optional model-assisted observation draft
      |
      v
batch-level observation review
      |
      v
temporary canonical-dataset build
      |
      v
schema + referential validation
      |
      v
transactional SQLite publication + private photo archive
      |
      v
validated CSV export + publication-run lineage + optional Google Sheets mirror
```

## Storage Boundaries

- `data/raw/ingestion/ingestion.sqlite`: canonical batch, review, metadata, and publication state; ignored by Git
- `data/raw/ingestion/batches/`: staged originals and optimized previews; ignored by Git
- `data/raw/ingestion/upload_suggestions/`: temporary pre-staging uploads; ignored by Git and expired after 24 hours
- `data/raw/site_anchors.csv`: exact private site coordinates used only for GPS matching; ignored by Git
- `photos/`: published private originals in deterministic site/date paths; ignored by Git
- `data/public/`: deterministic sanitized exports committed to Git

Set `PLANT_INGESTION_STATE` or `PLANT_PHOTO_ARCHIVE` to use different local storage locations:

```bash
export PLANT_INGESTION_STATE="$HOME/PlantSuccession/ingestion"
export PLANT_PHOTO_ARCHIVE="$HOME/PlantSuccession/photos"
export PLANT_SITE_ANCHORS="$HOME/PlantSuccession/site_anchors.csv"
```

The service never exposes Drive IDs, private links, or exact coordinates in the public manifest.

## Canonical Store Operations

SQLite is the sole mutable source of truth during normal operation. Schema changes are recorded in
`schema_migrations`; startup applies missing migrations and seeds the seven core `canonical_datasets`
from the committed CSV snapshot only when that store is empty. It never silently imports later CSV edits.
`analysis_ready_observations.csv` is a materialized derived export rebuilt from observations, sites, and
the photo manifest every time exports are generated; it is not independently editable state.

```bash
# Inspect schema versions, datasets, row counts, and content hashes.
.venv/bin/python scripts/manage_data_store.py status

# Rebuild all validated public CSV snapshots from SQLite.
.venv/bin/python scripts/manage_data_store.py export

# Create a consistent online SQLite backup plus SHA-256 manifest.
.venv/bin/python scripts/manage_data_store.py backup
```

Backups default to the ignored `data/raw/ingestion/backups/` directory. Copy important backups to a
separate protected location; the photo archive remains a separate storage boundary and is not embedded
in the SQLite backup.

An external CSV directory can replace the canonical snapshots only through an explicit validated import:

```bash
.venv/bin/python scripts/manage_data_store.py import \
  --source /path/to/validated/csvs \
  --replace
```

This command is for migration or recovery, not routine editing. The ordinary flow is SQLite to public CSV
exports to optional Google Sheets. To preserve completed reviews on GitHub, run `export`, run validation,
and commit the changed files under `data/public/`; never commit `ingestion.sqlite` itself.

## Operational Dashboard

The opening **Dashboard** view is a live read of `ingestion.sqlite`. It polls every 15 seconds while visible
and also supports manual refresh. The dashboard reports:

- batch, observation, photo, publication, and review-status counts
- missing vegetation-cover and dominant-taxon fields
- batch-integrity and unresolved-photo exceptions
- observation-review provenance without merging human and machine identities
- a site-by-date observation matrix
- site-filtered descriptive vegetation-cover series

Quality rows, matrix cells, and trend-table actions open the corresponding ingestion batch. The dashboard
does not write observations or infer missing ecological values; it only summarizes recorded workflow state.

## Published Data And Pipeline Runs

The app's **Published data** view is a read-only catalog over the validated CSV snapshot. It shows
observation and photo counts, downloadable data products, current observation rows, and publication-run
lineage. Each new publication records validation state, row counts, output SHA-256 checksums, the number
of newly archived photos, timestamps, and any failure message in SQLite.

Publication runs and destination-sync attempts are append-only operational records. Historical batches
published before this feature remain visible in the data tables but do not acquire invented run history.

## Site And Date Suggestions

The browser uploads a selected photo set once into private temporary storage. The service reads iPhone
EXIF metadata, votes across the available photos, and prefills:

- the observation date from the dominant capture date
- the site from the nearest private coordinate anchor
- confidence, median anchor distance, and mixed-date or mixed-site warnings

The suggestion is never treated as ground truth. Site and date remain editable and must be confirmed
before the temporary upload becomes a staging batch. A 150 m maximum match distance prevents remote GPS
records from being assigned to a monitoring site, while close sites can still produce a low-confidence
suggestion for human review.

## Batch Integrity

Before staging, every selected photo is checked independently against the confirmed batch site and date.
The interface names files with a different nearest site, a different capture date, missing metadata, or a
location outside the 150 m match radius. Blocking exceptions require an explicit acknowledgement; the
server repeats the check so the gate cannot be bypassed by browser state alone.

The local SQLite audit stores one batch-level result plus one row per checked file. Use the repeatable
audit command to rebuild those records for existing local batches:

```bash
.venv/bin/python scripts/audit_batch_integrity.py
```

Build the ignored private anchor file from the original workbook without copying coordinates into the
public site registry:

```bash
.venv/bin/python scripts/build_private_site_anchors.py \
  "/path/to/Plant Succession ML Project.xlsx"
```

The expected private CSV columns are `site_id`, `latitude`, and `longitude`. The current local file was
generated from the workbook's `Site Registry` sheet and contains all 11 sites.

## Render Deployment

The repository includes a `render.yaml` Blueprint for a single password-protected FastAPI web service.
It attaches a 1 GB persistent disk at `/var/data`; the SQLite review queue, uploaded originals, private
photo archive, and mutable public CSV snapshot all live below that mount point.

1. Push the current branch to GitHub.
2. In Render, choose **New > Blueprint** and connect this repository.
3. Enter a strong value for `PLANT_INGESTION_PASSWORD` when Render prompts for it.
4. Review the paid compute and disk charges, then apply the Blueprint.

The username defaults to `admin` and can be changed through `PLANT_INGESTION_USERNAME`. The committed
`data/public/*.csv` files seed an empty disk on first boot; subsequent deploys do not overwrite the
hosted working copy. The unauthenticated `/api/health` endpoint contains only service status so Render
can monitor the deployment.

Persistent storage is required for real uploads. Deploying this service without the disk would lose
review state, uploaded photos, and hosted CSV changes whenever Render replaces the instance. Because
the disk is attached to one service instance, this deployment intentionally runs as a single instance.

To reproduce the hosted storage layout locally:

```bash
export PLANT_PUBLIC_DATA="$HOME/PlantSuccession/public"
export PLANT_INGESTION_STATE="$HOME/PlantSuccession/ingestion"
export PLANT_PHOTO_ARCHIVE="$HOME/PlantSuccession/photos"
export PLANT_INGESTION_USERNAME="admin"
export PLANT_INGESTION_PASSWORD="choose-a-local-test-password"
export PLANT_REVIEWER_ID="project_owner"
.venv/bin/uvicorn plant_succession.app:app --app-dir src --host 127.0.0.1 --port 8765
```

## Candidate Labels

The first labeler is deliberately conservative. It recognizes explicit terms such as `wide`,
`quadrat`, `close_up`, `context_left`, and `context_right` in filenames. Generic camera filenames such
as `IMG_4021.HEIC` remain `unclassified` and enter human review.

The database stores the suggested type, confidence, reason, and labeler version separately from the
canonical verified label. It also writes append-only review events containing the label, action,
actor type, actor ID, actor/model version, confidence, note, and timestamp. A rule suggestion and a
human verification therefore remain separate records, and later humans or models can add judgments
without erasing earlier ones. Camera order is never used as evidence.

`PLANT_REVIEWER_ID` supplies the stable local alias for reviews submitted through the app. This local
single-user authentication is sufficient for the current workflow. A multi-user deployment should use
real authenticated identities rather than allowing clients to submit arbitrary reviewer names.

For legacy local ingestion rows, review events can be backfilled only when the reviewer identity is known:

```bash
.venv/bin/python scripts/backfill_review_events.py \
  --verified-actor-type human \
  --verified-actor-id project_owner
```

## Observation Review

Resolving every photo role moves a batch into `observation_review`; it does not make the batch
publishable. The Observation tab stores a structured private draft containing surface-cover estimates,
up to three uncertainty-aware taxon labels, visible flowering and seed-set states, a successional-stage
assessment, notes, and an explicit partial-or-complete result.

The taxon section also shows labels from earlier dated observations at the selected site. The three ranked
slots remain a dominance summary; `new_taxon_labels` captures newly recorded fourth-or-lower taxa without
forcing them into those slots. The browser compares exact labels against prior records as a review aid and
warns that synonyms and uncertain identifications still require human judgment.

Draft saves and final submissions are separate append-only events with actor identity and a complete
JSON snapshot. Completing a partial review requires a completeness note, while unsupported values may
remain blank and publish as null. The final reviewed values are mapped onto the existing observation
schema only during validated publication.

Published batches remain reviewable. Saving a new observation version reopens only the metadata stage;
republishing updates the existing observation row while retaining the original photo IDs and archive files.

### Automated Verification

Every observation save automatically runs a versioned, deterministic ruleset. This check is local and
does not send photos or metadata to a model. It checks percentage ranges, the approximately 100% total for
the four ground-surface categories, duplicate dominance labels, new-taxon consistency, photo-role
completeness, and acknowledged upload-integrity exceptions. Standing dead is checked as its own 0-100%
field and is deliberately excluded from the ground-surface total because it may overlap those categories.

Each run and each issue are append-only SQLite audit records tied to the exact observation-review update
timestamp and verifier version. Saving a review creates a new run; **Run again** adds another run without
erasing history. Warnings identify records that deserve attention but remain publishable. Blocking findings
must be corrected before publication. The **Verification** queue filter collects saved observations whose
latest result is not a clean pass. These checks improve consistency but do not verify ecological truth,
species identity, or whether an unphotographed event occurred.

### Optional Machine Draft Queue

The ingestion workspace can queue a first-pass draft from a general-purpose vision model after every photo
role has been verified. This is optional and deliberately human-gated: opening a batch does not transmit
images. Select eligible observations and click **Queue selected** to submit only those records. Completed
human reviews and observations that already have a machine draft are excluded from bulk selection by
default. The per-observation **Generate machine draft** command remains available as an explicit re-run.

Configure it before starting the service:

```bash
export OPENAI_API_KEY="your-api-key"
# Optional; defaults to gpt-6-astra.
export PLANT_MACHINE_REVIEW_MODEL="gpt-6-astra"
.venv/bin/python scripts/run_ingestion_app.py
```

The response is constrained to the observation-review schema and includes per-field confidence, visual
evidence, and a list of fields that deserve extra attention. Unsupported fields should remain null. The
prompt prohibits disturbance inference and treats taxonomic labels as uncertain unless the photographs
show diagnostic evidence.

Queue jobs are durable SQLite records with `queued`, `running`, `succeeded`, `failed`, or `skipped` state.
The worker claims one job at a time and does not hold a database connection during the model request, so
human review saves remain available. Interrupted running jobs return to `queued` when the service restarts;
failed jobs can be retried. A job is skipped if its human review becomes complete before processing.

A generated draft is stored as an append-only `model` / `suggested` event. **Load draft into form** copies
the suggestion into the browser without saving it; existing human values receive an explicit confirmation
before replacement. Only a later human **Save draft** or **Complete review** action creates the canonical
values used during publication. Re-running the model adds another suggestion event instead of erasing
earlier model or human judgments.

This feature is not a custom-trained ecological classifier and should not be described as ground truth.
API use may incur provider charges. Keep `OPENAI_API_KEY` in the environment; `.env` and common credential
files are ignored by Git.

## Publication Guarantees

A batch cannot publish while any photo is unresolved, its observation review is incomplete, or its latest
automated verification has blocking findings. Publication refreshes a stale verification result and then:

1. Copies originals into the private deterministic archive.
2. Builds candidate versions of `photo_manifest.csv`, `observations.csv`, and
   `photo_batch_summary.csv` from canonical SQLite snapshots in a temporary directory.
3. Runs the complete public-data validator, including relational and privacy checks.
4. Commits all three datasets and publication lineage in one SQLite transaction.
5. Materializes all public CSVs from SQLite with atomic file replacement.

The service is idempotent for an unchanged published batch, supports observation-metadata revisions without
duplicating photos, and detects repeat files by content hash.

## Optional Google Sheets Mirror

Google Sheets is an output destination, not the canonical database. Each sync first regenerates CSV exports
from SQLite. A successful local publication remains valid if Sheets is unavailable; the failed sync attempt
is retained in SQLite and can be retried from the **Published data** view.

Create a Google Cloud service account with Sheets API access, share the target workbook with its service
account email, and configure the application before launch:

```bash
export PLANT_GOOGLE_SHEETS_ID="spreadsheet-id-from-the-workbook-url"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/plant-succession/google-service-account.json"
```

For hosted environments that store credentials as an environment secret instead of a file, set
`PLANT_GOOGLE_SERVICE_ACCOUNT_JSON` to the complete service-account JSON. Never commit that JSON. The sync
replaces the contents of the managed workbook tabs with the latest validated CSV snapshots so stale rows
cannot survive a refresh.

## API Surface

FastAPI publishes interactive API documentation at `http://127.0.0.1:8765/docs`.

- `POST /api/batches`: stage one site/date upload batch
- `GET /api/published-data`: retrieve the published catalog, lineage, and destination status
- `GET /api/exports/{filename}`: download one validated public CSV
- `POST /api/publication-runs/{run_id}/sync/google-sheets`: retry a Sheets mirror
- `POST /api/upload-suggestions`: cache photos and return EXIF-derived site/date suggestions
- `POST /api/batches/from-suggestion`: confirm site/date and stage an already cached upload
- `GET /api/batches/{batch_id}`: retrieve candidates and review state
- `PATCH /api/photos/{photo_id}`: submit a human-verified role
- `POST /api/batches/{batch_id}/accept-suggestions`: accept explicit filename suggestions
- `GET /api/machine-review/config`: report whether optional model review is configured
- `GET /api/machine-review/jobs`: retrieve durable queue state and recent jobs
- `POST /api/machine-review/jobs`: queue selected eligible observations
- `POST /api/machine-review/jobs/process`: resume queued work
- `POST /api/machine-review/jobs/retry-failed`: retry failed, still-incomplete observations
- `POST /api/batches/{batch_id}/machine-review`: generate and audit a model suggestion
- `PUT /api/batches/{batch_id}/observation-review`: save or complete the batch observation review
- `POST /api/batches/{batch_id}/verify-observation`: append a deterministic verification run
- `POST /api/batches/{batch_id}/publish`: validate and publish a ready batch
- `GET /api/dashboard`: retrieve operational metrics, quality issues, provenance, site/date coverage, and longitudinal series

## Current Boundary

This version automates ingestion, metadata normalization, photo-role review, selective model-assisted
observation drafts with durable queue state, structured human review, deterministic cross-field verification,
and publication. Disturbance events remain a separate evidence-backed workflow. Model output is advisory and
cannot cross the publication boundary without a human-completed observation review.

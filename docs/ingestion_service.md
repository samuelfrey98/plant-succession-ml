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
temporary public-dataset build
      |
      v
schema + referential validation
      |
      v
atomic CSV publication + private photo archive
```

## Storage Boundaries

- `data/raw/ingestion/ingestion.sqlite`: local batch and review state; ignored by Git
- `data/raw/ingestion/batches/`: staged originals and optimized previews; ignored by Git
- `photos/`: published private originals in deterministic site/date paths; ignored by Git
- `data/public/`: sanitized, validated metadata snapshots committed to Git

Set `PLANT_INGESTION_STATE` or `PLANT_PHOTO_ARCHIVE` to use different local storage locations:

```bash
export PLANT_INGESTION_STATE="$HOME/PlantSuccession/ingestion"
export PLANT_PHOTO_ARCHIVE="$HOME/PlantSuccession/photos"
```

The service never exposes Drive IDs, private links, or exact coordinates in the public manifest.

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
.venv/bin/uvicorn plant_succession.app:app --app-dir src --host 127.0.0.1 --port 8765
```

## Candidate Labels

The first labeler is deliberately conservative. It recognizes explicit terms such as `wide`,
`quadrat`, `close_up`, `context_left`, and `context_right` in filenames. Generic camera filenames such
as `IMG_4021.HEIC` remain `unclassified` and enter human review.

The database stores the suggested type, confidence, reason, and labeler version separately from the
verified human label. This creates a ground-truth trail suitable for evaluating a future vision model.
Camera order is never used as evidence.

## Publication Guarantees

A batch cannot publish while any photo is unresolved. Publication then:

1. Copies originals into the private deterministic archive.
2. Builds candidate versions of `photo_manifest.csv`, `observations.csv`, and
   `photo_batch_summary.csv` in a temporary directory.
3. Runs the complete public-data validator, including relational and privacy checks.
4. Atomically replaces the three public CSVs only when validation succeeds.

The service is idempotent for an already-published batch and detects repeat files by content hash.

## API Surface

FastAPI publishes interactive API documentation at `http://127.0.0.1:8765/docs`.

- `POST /api/batches`: stage one site/date upload batch
- `GET /api/batches/{batch_id}`: retrieve candidates and review state
- `PATCH /api/photos/{photo_id}`: submit a human-verified role
- `POST /api/batches/{batch_id}/accept-suggestions`: accept explicit filename suggestions
- `POST /api/batches/{batch_id}/publish`: validate and publish a ready batch
- `GET /api/dashboard`: retrieve queue and pipeline metrics

## Current Boundary

This version automates ingestion, metadata normalization, photo-role review, and publication. Ecological
cover and taxon review remain in the existing observation workflow. A later version can add a model-backed
labeler and send only low-confidence predictions to the same review queue without changing the publication
contract.

"""FastAPI entrypoint for the photo-ingestion review service."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
import shutil
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from plant_succession.ingestion import IngestionError, IngestionPaths, IngestionService


ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PUBLIC_DIR = ROOT / "data" / "public"
REVIEWER_ID = os.environ.get("PLANT_REVIEWER_ID", "project_owner")


def initialize_public_data(source_dir: Path, public_dir: Path) -> None:
    """Seed missing public CSVs without overwriting a durable dataset."""

    public_dir.mkdir(parents=True, exist_ok=True)
    if source_dir.resolve() == public_dir.resolve():
        return
    for source in source_dir.glob("*.csv"):
        destination = public_dir / source.name
        if not destination.exists():
            shutil.copy2(source, destination)


def valid_basic_auth(header: str | None, username: str, password: str) -> bool:
    """Validate one HTTP Basic authorization header using constant-time comparison."""

    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        supplied_username, supplied_password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(supplied_username, username) and secrets.compare_digest(
        supplied_password, password
    )


def build_service() -> IngestionService:
    public_dir = Path(
        os.environ.get("PLANT_PUBLIC_DATA", DEFAULT_PUBLIC_DIR)
    ).expanduser()
    state_dir = Path(
        os.environ.get("PLANT_INGESTION_STATE", ROOT / "data" / "raw" / "ingestion")
    ).expanduser()
    archive_dir = Path(
        os.environ.get("PLANT_PHOTO_ARCHIVE", ROOT / "photos")
    ).expanduser()
    site_anchor_file = Path(
        os.environ.get("PLANT_SITE_ANCHORS", ROOT / "data" / "raw" / "site_anchors.csv")
    ).expanduser()
    initialize_public_data(DEFAULT_PUBLIC_DIR, public_dir)
    return IngestionService(
        IngestionPaths(
            public_dir=public_dir,
            schema_dir=ROOT / "data" / "schema",
            state_dir=state_dir,
            archive_dir=archive_dir,
            site_anchor_file=site_anchor_file,
        )
    )


service = build_service()
app = FastAPI(
    title="Plant Succession Ingest",
    version="0.1.0",
    description="Local-first staging and review for longitudinal field photos.",
)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.middleware("http")
async def require_authentication(request: Request, call_next) -> Response:
    """Protect hosted data while leaving local development and health checks simple."""

    password = os.environ.get("PLANT_INGESTION_PASSWORD", "")
    if not password or request.url.path == "/api/health":
        return await call_next(request)
    username = os.environ.get("PLANT_INGESTION_USERNAME", "admin")
    if valid_basic_auth(request.headers.get("Authorization"), username, password):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required."},
        headers={"WWW-Authenticate": 'Basic realm="Plant Succession Ingest"'},
    )


class ReviewUpdate(BaseModel):
    photo_type: str
    notes: str = ""


class ObservationReviewUpdate(BaseModel):
    estimated_total_vegetation_cover_pct: int | None = None
    bare_ground_pct: int | None = None
    gravel_rock_pct: int | None = None
    litter_mulch_woodchip_pct: int | None = None
    standing_dead_pct: int | None = None
    dominant_species: str = ""
    dominant_taxon_rank: str = ""
    dominant_species_confidence: str = ""
    dominant_flowering: str = ""
    dominant_seed_set: str = ""
    secondary_species: str = ""
    secondary_taxon_rank: str = ""
    secondary_species_confidence: str = ""
    secondary_flowering: str = ""
    secondary_seed_set: str = ""
    tertiary_species: str = ""
    tertiary_taxon_rank: str = ""
    tertiary_species_confidence: str = ""
    tertiary_flowering: str = ""
    tertiary_seed_set: str = ""
    new_species_detected: str = ""
    successional_stage: str = ""
    notes: str = ""
    completion_status: str = "partially_reviewed"
    completeness_notes: str = ""
    finalize: bool = False


def bad_request(error: IngestionError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "plant-succession-ingest"}


@app.get("/api/sites")
def sites() -> list[dict[str, str]]:
    return service.list_sites()


@app.get("/api/dashboard")
def dashboard() -> dict[str, object]:
    return service.dashboard()


@app.get("/api/batches/{batch_id}")
def batch(batch_id: str) -> dict[str, object]:
    try:
        return service.get_batch(batch_id)
    except IngestionError as error:
        raise bad_request(error) from error


@app.post("/api/batches", status_code=201)
def create_batch(
    site_id: str = Form(...),
    observation_date: str = Form(...),
    observation_notes: str = Form(""),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    try:
        uploads = [(upload.filename or "", upload.file) for upload in files]
        return service.create_batch(site_id, observation_date, observation_notes, uploads)
    except IngestionError as error:
        raise bad_request(error) from error
    finally:
        for upload in files:
            upload.file.close()


@app.post("/api/upload-suggestions", status_code=201)
def create_upload_suggestion(files: list[UploadFile] = File(...)) -> dict[str, object]:
    try:
        uploads = [(upload.filename or "", upload.file) for upload in files]
        return service.create_upload_suggestion(uploads)
    except IngestionError as error:
        raise bad_request(error) from error
    finally:
        for upload in files:
            upload.file.close()


@app.post("/api/batches/from-suggestion", status_code=201)
def create_batch_from_suggestion(
    upload_token: str = Form(...),
    site_id: str = Form(...),
    observation_date: str = Form(...),
    observation_notes: str = Form(""),
    integrity_override_confirmed: bool = Form(False),
) -> dict[str, object]:
    try:
        return service.create_batch_from_upload_suggestion(
            upload_token,
            site_id,
            observation_date,
            observation_notes,
            integrity_override_confirmed,
        )
    except IngestionError as error:
        raise bad_request(error) from error


@app.get("/api/photos/{photo_id}/preview")
def preview(photo_id: str) -> FileResponse:
    try:
        return FileResponse(service.preview_path(photo_id), media_type="image/jpeg")
    except IngestionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.patch("/api/photos/{photo_id}")
def review_photo(photo_id: str, update: ReviewUpdate) -> dict[str, object]:
    try:
        return service.review_photo(
            photo_id,
            update.photo_type,
            update.notes,
            actor_type="human",
            actor_id=REVIEWER_ID,
        )
    except IngestionError as error:
        raise bad_request(error) from error


@app.post("/api/batches/{batch_id}/accept-suggestions")
def accept_suggestions(batch_id: str) -> dict[str, object]:
    try:
        return service.accept_explicit_suggestions(
            batch_id,
            actor_type="human",
            actor_id=REVIEWER_ID,
        )
    except IngestionError as error:
        raise bad_request(error) from error


@app.put("/api/batches/{batch_id}/observation-review")
def save_observation_review(
    batch_id: str, update: ObservationReviewUpdate
) -> dict[str, object]:
    try:
        values = update.model_dump()
        finalize = bool(values.pop("finalize"))
        return service.save_observation_review(
            batch_id,
            values,
            finalize=finalize,
            actor_type="human",
            actor_id=REVIEWER_ID,
        )
    except IngestionError as error:
        raise bad_request(error) from error


@app.post("/api/batches/{batch_id}/publish")
def publish(batch_id: str) -> dict[str, object]:
    try:
        return service.publish_batch(batch_id)
    except IngestionError as error:
        raise bad_request(error) from error


def run() -> None:
    """Run the service on the local loopback interface."""

    import uvicorn

    uvicorn.run("plant_succession.app:app", host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    run()

"""FastAPI entrypoint for the local photo-ingestion review service."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from plant_succession.ingestion import IngestionError, IngestionPaths, IngestionService


ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(__file__).resolve().parent / "web"


def build_service() -> IngestionService:
    state_dir = Path(
        os.environ.get("PLANT_INGESTION_STATE", ROOT / "data" / "raw" / "ingestion")
    ).expanduser()
    archive_dir = Path(
        os.environ.get("PLANT_PHOTO_ARCHIVE", ROOT / "photos")
    ).expanduser()
    return IngestionService(
        IngestionPaths(
            public_dir=ROOT / "data" / "public",
            schema_dir=ROOT / "data" / "schema",
            state_dir=state_dir,
            archive_dir=archive_dir,
        )
    )


service = build_service()
app = FastAPI(
    title="Plant Succession Ingest",
    version="0.1.0",
    description="Local-first staging and review for longitudinal field photos.",
)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


class ReviewUpdate(BaseModel):
    photo_type: str
    notes: str = ""


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


@app.get("/api/photos/{photo_id}/preview")
def preview(photo_id: str) -> FileResponse:
    try:
        return FileResponse(service.preview_path(photo_id), media_type="image/jpeg")
    except IngestionError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.patch("/api/photos/{photo_id}")
def review_photo(photo_id: str, update: ReviewUpdate) -> dict[str, object]:
    try:
        return service.review_photo(photo_id, update.photo_type, update.notes)
    except IngestionError as error:
        raise bad_request(error) from error


@app.post("/api/batches/{batch_id}/accept-suggestions")
def accept_suggestions(batch_id: str) -> dict[str, object]:
    try:
        return service.accept_explicit_suggestions(batch_id)
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

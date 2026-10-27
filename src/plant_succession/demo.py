"""Build an isolated, synthetic workspace for demonstrating the ingestion app."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from plant_succession.ingestion import IngestionPaths, IngestionService


DEMO_SEED_VERSION = 1
DEMO_ACTOR_ID = "demo_seed"
DEMO_PHOTO_ROLES = ("wide", "quadrat", "context_left")
DEMO_FILENAMES = {
    "wide": "wide.jpg",
    "quadrat": "quadrat.jpg",
    "context_left": "context-left.jpg",
}


def demo_image_bytes(label: str, role: str, palette_index: int) -> io.BytesIO:
    """Create an obviously synthetic field-image stand-in with stable dimensions."""

    palettes = (
        ((175, 208, 224), (91, 127, 72), (102, 79, 55)),
        ((203, 220, 230), (121, 151, 79), (123, 97, 64)),
        ((185, 213, 221), (79, 124, 83), (111, 84, 57)),
        ((198, 216, 228), (105, 143, 73), (132, 104, 70)),
    )
    sky, vegetation, ground = palettes[palette_index % len(palettes)]
    image = Image.new("RGB", (960, 640), sky)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 260, 960, 640), fill=ground)
    for index in range(14):
        left = 24 + index * 68
        height = 90 + ((index * 37 + palette_index * 29) % 170)
        draw.ellipse((left, 360 - height, left + 86, 420), fill=vegetation)
    draw.rectangle((0, 0, 960, 64), fill=(20, 35, 28))
    draw.text((22, 18), f"SYNTHETIC DEMO | {label} | {role}", fill=(255, 255, 255))
    draw.rectangle((22, 560, 938, 618), fill=(245, 247, 245), outline=(30, 75, 46), width=3)
    draw.text(
        (42, 580),
        "Generated placeholder image. Not a field observation.",
        fill=(23, 72, 42),
    )
    output = io.BytesIO()
    image.save(output, "JPEG", quality=88)
    output.seek(0)
    return output


def _create_demo_batch(
    service: IngestionService,
    *,
    site_id: str,
    observation_date: str,
    label: str,
    palette_index: int,
) -> dict[str, object]:
    uploads = [
        (
            DEMO_FILENAMES[role],
            demo_image_bytes(label, role, palette_index),
        )
        for role in DEMO_PHOTO_ROLES
    ]
    return service.create_batch(
        site_id,
        observation_date,
        f"Synthetic demo batch: {label}. These values are not field observations.",
        uploads,
    )


def _verify_demo_photos(
    service: IngestionService, batch: dict[str, object]
) -> dict[str, object]:
    by_filename = {
        str(photo["source_filename"]): photo
        for photo in batch["photos"]  # type: ignore[index]
    }
    for role in DEMO_PHOTO_ROLES:
        photo = by_filename[DEMO_FILENAMES[role]]
        batch = service.review_photo(
            str(photo["photo_id"]),
            role,
            notes="Synthetic role label created by the public demo seed.",
            actor_type="human",
            actor_id=DEMO_ACTOR_ID,
        )
    return batch


def seed_demo_batches(service: IngestionService) -> dict[str, object]:
    """Create one synthetic example at each major pipeline stage."""

    if service.list_batches():
        raise RuntimeError("The demo store already contains batches; reset it before seeding.")

    published = _create_demo_batch(
        service,
        site_id="9",
        observation_date="2026-10-19",
        label="Published example",
        palette_index=0,
    )
    published = _verify_demo_photos(service, published)
    published = service.save_observation_review(
        str(published["batch_id"]),
        {
            "estimated_total_vegetation_cover_pct": 45,
            "bare_ground_pct": 10,
            "gravel_rock_pct": 5,
            "litter_mulch_woodchip_pct": 40,
            "standing_dead_pct": 10,
            "dominant_species": "Alliaria petiolata",
            "dominant_taxon_rank": "species",
            "dominant_species_confidence": "likely",
            "dominant_flowering": "false",
            "dominant_seed_set": "false",
            "new_species_detected": "false",
            "successional_stage": "developing_herb_layer",
            "notes": "Synthetic reviewed values for the public workflow demo.",
            "completion_status": "reviewed",
        },
        finalize=True,
        actor_id=DEMO_ACTOR_ID,
    )
    published = service.publish_batch(str(published["batch_id"]))

    ready = _create_demo_batch(
        service,
        site_id="3",
        observation_date="2026-10-20",
        label="Ready to publish example",
        palette_index=1,
    )
    ready = _verify_demo_photos(service, ready)
    ready = service.save_observation_review(
        str(ready["batch_id"]),
        {
            "estimated_total_vegetation_cover_pct": 55,
            "bare_ground_pct": 5,
            "gravel_rock_pct": 40,
            "litter_mulch_woodchip_pct": 0,
            "standing_dead_pct": 10,
            "dominant_species": "Artemisia vulgaris",
            "dominant_taxon_rank": "species",
            "dominant_species_confidence": "likely",
            "dominant_flowering": "false",
            "dominant_seed_set": "true",
            "new_species_detected": "false",
            "successional_stage": "developing_herb_layer",
            "notes": "Synthetic complete review awaiting publication.",
            "completion_status": "reviewed",
        },
        finalize=True,
        actor_id=DEMO_ACTOR_ID,
    )

    observation_review = _create_demo_batch(
        service,
        site_id="8",
        observation_date="2026-10-21",
        label="Observation review example",
        palette_index=2,
    )
    observation_review = _verify_demo_photos(service, observation_review)
    observation_review = service.save_observation_review(
        str(observation_review["batch_id"]),
        {
            "estimated_total_vegetation_cover_pct": 35,
            "bare_ground_pct": 45,
            "gravel_rock_pct": 20,
            "litter_mulch_woodchip_pct": 0,
            "standing_dead_pct": 15,
            "dominant_species": "Plantago lanceolata",
            "dominant_taxon_rank": "species",
            "dominant_species_confidence": "uncertain",
            "dominant_flowering": "false",
            "dominant_seed_set": "false",
            "new_species_detected": "",
            "successional_stage": "early_colonization",
            "notes": "Synthetic draft requiring a human decision on taxon history.",
            "completion_status": "partially_reviewed",
            "completeness_notes": "Confirm the taxon label before completing review.",
        },
        finalize=False,
        actor_id=DEMO_ACTOR_ID,
    )

    needs_review = _create_demo_batch(
        service,
        site_id="4",
        observation_date="2026-10-22",
        label="Photo review example",
        palette_index=3,
    )

    return {
        "seed_version": DEMO_SEED_VERSION,
        "batches": {
            "published": str(published["batch_id"]),
            "ready": str(ready["batch_id"]),
            "observation_review": str(observation_review["batch_id"]),
            "needs_review": str(needs_review["batch_id"]),
        },
    }


def prepare_demo_workspace(
    root: Path,
    *,
    source_public_dir: Path,
    schema_dir: Path,
    reset: bool = False,
) -> dict[str, object]:
    """Create or reuse a demo workspace without touching project data paths."""

    root = root.expanduser().resolve()
    manifest_path = root / "demo_manifest.json"
    if reset and root.exists():
        if root in {Path("/"), Path.home()}:
            raise RuntimeError("Refusing to reset an unsafe demo workspace path.")
        if root.name != ".demo" and not manifest_path.exists():
            raise RuntimeError(
                "Refusing to reset a non-demo directory without a demo manifest."
            )
        shutil.rmtree(root)

    public_dir = root / "public"
    state_dir = root / "state"
    archive_dir = root / "photos"
    database = state_dir / "ingestion.sqlite"
    newly_created = not database.exists()
    if newly_created:
        if root.exists() and any(root.iterdir()):
            raise RuntimeError(
                "The demo workspace is not empty. Choose another path or reset a seeded demo."
            )
        public_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_public_dir, public_dir, dirs_exist_ok=True)

    service = IngestionService(
        IngestionPaths(
            public_dir=public_dir,
            schema_dir=schema_dir,
            state_dir=state_dir,
            archive_dir=archive_dir,
            site_anchor_file=root / "site_anchors.csv",
        )
    )
    if newly_created:
        manifest = seed_demo_batches(service)
        manifest.update(
            {
                "synthetic": True,
                "public_dir": str(public_dir),
                "state_dir": str(state_dir),
                "archive_dir": str(archive_dir),
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    return {
        **manifest,
        "root": str(root),
        "database": str(database),
        "newly_created": newly_created,
    }

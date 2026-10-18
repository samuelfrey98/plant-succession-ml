"""Optional vision-model suggestions for ecological observation review."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field


PROMPT_VERSION = "observation_review_v1"
DEFAULT_MODEL = "gpt-6-astra"


class MachineReviewError(RuntimeError):
    """Raised when an optional machine-review provider cannot return a draft."""


class MachineObservationValues(BaseModel):
    estimated_total_vegetation_cover_pct: int | None = Field(default=None, ge=0, le=100)
    bare_ground_pct: int | None = Field(default=None, ge=0, le=100)
    gravel_rock_pct: int | None = Field(default=None, ge=0, le=100)
    litter_mulch_woodchip_pct: int | None = Field(default=None, ge=0, le=100)
    standing_dead_pct: int | None = Field(default=None, ge=0, le=100)
    dominant_species: str | None = None
    dominant_taxon_rank: (
        Literal["species", "genus", "family", "unknown", "morphotype"] | None
    ) = None
    dominant_species_confidence: Literal["confident", "likely", "uncertain"] | None = None
    dominant_flowering: bool | None = None
    dominant_seed_set: bool | None = None
    secondary_species: str | None = None
    secondary_taxon_rank: (
        Literal["species", "genus", "family", "unknown", "morphotype"] | None
    ) = None
    secondary_species_confidence: Literal["confident", "likely", "uncertain"] | None = None
    secondary_flowering: bool | None = None
    secondary_seed_set: bool | None = None
    tertiary_species: str | None = None
    tertiary_taxon_rank: (
        Literal["species", "genus", "family", "unknown", "morphotype"] | None
    ) = None
    tertiary_species_confidence: Literal["confident", "likely", "uncertain"] | None = None
    tertiary_flowering: bool | None = None
    tertiary_seed_set: bool | None = None
    new_species_detected: bool | None = None
    successional_stage: Literal[
        "early_colonization",
        "developing_herb_layer",
        "dense_herb_layer",
        "shrub_establishment",
        "young_woodland",
    ] | None = None
    notes: str | None = None


class FieldAssessment(BaseModel):
    field_name: str
    confidence: Literal["high", "medium", "low", "not_assessed"]
    evidence: str


class MachineObservationDraft(BaseModel):
    values: MachineObservationValues
    assessments: list[FieldAssessment]
    overall_summary: str
    review_priority_fields: list[str]


class MachineReviewProvider(Protocol):
    def configuration(self) -> dict[str, object]: ...

    def review(
        self,
        *,
        site_id: str,
        observation_date: str,
        photos: list[dict[str, str]],
    ) -> dict[str, object]: ...


SYSTEM_INSTRUCTIONS = """You create conservative draft metadata for a longitudinal plant-monitoring dataset.
You are not an expert taxonomist and your output is never ground truth. Use only evidence visible in the
provided photos. Return null whenever the evidence is insufficient. Do not infer disturbance events,
causes, management activity, or scientific conclusions. Never use camera order as evidence.

Cover guidance:
- estimated_total_vegetation_cover_pct: live vegetation projected over the monitored surface, 0-100.
- bare_ground_pct: exposed mineral soil with no live vegetation, litter, or coarse rock covering it.
- gravel_rock_pct: visible gravel, rubble, or rock covering the surface.
- litter_mulch_woodchip_pct: detached plant litter, mulch, or wood chips covering the surface.
- standing_dead_pct: upright attached dead vegetation; this can overlap the ground-cover categories.
- Live vegetation, bare ground, gravel/rock, and litter/mulch should total approximately 100 only when
  the framing supports a complete surface estimate. Otherwise leave unsupported values null.

Taxon guidance:
- Label species only with strong visible diagnostic evidence. Otherwise use genus, family, or a stable
  descriptive morphotype such as unknown_grass_01 or unknown_rosette_01.
- Dominant means the taxon occupying the most visible live cover; secondary and tertiary are next.
- Flowering means open flowers are visibly present. Seed set means visible fruit or seed structures.
- Use taxon confidence to preserve uncertainty.

Successional-stage guidance:
- early_colonization: mostly open substrate with scattered pioneer plants.
- developing_herb_layer: expanding but discontinuous herbaceous cover.
- dense_herb_layer: mostly continuous herbaceous cover.
- shrub_establishment: persistent woody shrubs are becoming structurally important.
- young_woodland: young trees form the developing vegetation structure.

For every non-null value, include a field assessment with confidence and concise visual evidence. Put
uncertain or framing-sensitive fields in review_priority_fields. The notes should be a short descriptive
summary, not an ecological conclusion."""


class OpenAIMachineReviewer:
    """OpenAI Responses API provider loaded only when machine review is requested."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("PLANT_MACHINE_REVIEW_MODEL", DEFAULT_MODEL)

    def configuration(self) -> dict[str, object]:
        return {
            "available": bool(self.api_key),
            "provider": "openai",
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
            "reason": "" if self.api_key else "Set OPENAI_API_KEY to enable machine review.",
        }

    def review(
        self,
        *,
        site_id: str,
        observation_date: str,
        photos: list[dict[str, str]],
    ) -> dict[str, object]:
        if not self.api_key:
            raise MachineReviewError("Machine review is not configured. Set OPENAI_API_KEY first.")
        if not photos:
            raise MachineReviewError("No photo previews are available for machine review.")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise MachineReviewError(
                "The OpenAI Python package is not installed. Reinstall the project dependencies."
            ) from error

        content: list[dict[str, object]] = [
            {
                "type": "input_text",
                "text": (
                    f"Review Site {site_id} on {observation_date}. "
                    f"The batch contains {len(photos)} protocol photos."
                ),
            }
        ]
        for index, photo in enumerate(photos, start=1):
            path = Path(photo["path"])
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": (
                            f"Photo {index}: {photo['filename']} "
                            f"(human-verified role: {photo['photo_type']})."
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "auto",
                    },
                ]
            )

        try:
            response = OpenAI(api_key=self.api_key).responses.parse(
                model=self.model,
                instructions=SYSTEM_INSTRUCTIONS,
                input=[{"role": "user", "content": content}],
                text_format=MachineObservationDraft,
                store=False,
            )
        except Exception as error:
            raise MachineReviewError(f"Machine review request failed: {error}") from error
        if response.output_parsed is None:
            raise MachineReviewError("The model did not return a structured observation draft.")
        return {
            "provider": "openai",
            "model": self.model,
            "prompt_version": PROMPT_VERSION,
            "provider_response_id": getattr(response, "id", None),
            "draft": response.output_parsed.model_dump(mode="json"),
        }

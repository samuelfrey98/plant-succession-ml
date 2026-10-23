"""Reproducible transformations from canonical public tables to analysis outputs."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


COVER_COLUMNS = [
    "estimated_total_vegetation_cover_pct",
    "bare_ground_pct",
    "gravel_rock_pct",
]
SURFACE_COVER_COLUMNS = [
    *COVER_COLUMNS,
    "litter_mulch_woodchip_pct",
    "standing_dead_pct",
]


def build_analysis_table(
    observations: pd.DataFrame,
    sites: pd.DataFrame,
    photos: pd.DataFrame,
) -> pd.DataFrame:
    """Join public tables and derive longitudinal completeness fields."""

    observations = observations.copy()
    sites = sites.copy()
    photos = photos.copy()

    observations["site_id"] = observations["site_id"].astype("string")
    sites["site_id"] = sites["site_id"].astype("string")
    photos["site_id"] = photos["site_id"].astype("string")
    observations["observation_date"] = pd.to_datetime(observations["observation_date"])

    for column in SURFACE_COVER_COLUMNS:
        if column not in observations:
            observations[column] = pd.NA
        observations[column] = pd.to_numeric(
            observations[column], errors="coerce"
        ).astype("Float64")

    photo_counts = photos.groupby("observation_id").size().rename("manifest_photo_count")
    verified_counts = (
        photos[photos["classification_status"].eq("verified")]
        .groupby("observation_id")
        .size()
        .rename("verified_photo_count")
    )
    type_counts = (
        photos.assign(
            photo_group=photos["photo_type"].replace(
                {"context_left": "context", "context_right": "context"}
            )
        )
        .pivot_table(
            index="observation_id",
            columns="photo_group",
            values="photo_id",
            aggfunc="count",
            fill_value=0,
        )
        .add_suffix("_photo_count")
    )

    result = observations.merge(
        sites[["site_id", "site_name", "habitat_context"]],
        on="site_id",
        how="left",
        validate="many_to_one",
    )
    result = result.join(photo_counts, on="observation_id")
    result = result.join(verified_counts, on="observation_id")
    result = result.join(type_counts, on="observation_id")

    count_columns = [
        "manifest_photo_count",
        "verified_photo_count",
        "wide_photo_count",
        "quadrat_photo_count",
        "close_up_photo_count",
        "context_photo_count",
        "extra_photo_count",
        "unclassified_photo_count",
    ]
    for column in count_columns:
        if column not in result:
            result[column] = 0
        result[column] = result[column].fillna(0).astype("int64")

    result["site_sort_key"] = pd.to_numeric(result["site_id"], errors="coerce")
    result = result.sort_values(
        ["site_sort_key", "site_id", "observation_date", "observation_id"]
    ).drop(columns="site_sort_key")
    result["observation_sequence"] = result.groupby("site_id").cumcount() + 1
    result["days_since_previous_observation"] = (
        result.groupby("site_id")["observation_date"].diff().dt.days.astype("Int64")
    )
    result["vegetation_cover_change_pp"] = (
        result.groupby("site_id")["estimated_total_vegetation_cover_pct"].diff()
    )
    result["has_cover_estimate"] = result[COVER_COLUMNS].notna().all(axis=1)
    result["surface_cover_sum_pct"] = result[SURFACE_COVER_COLUMNS].sum(
        axis=1, min_count=3
    )
    result["surface_cover_sum_within_tolerance"] = (
        result["surface_cover_sum_pct"]
        .between(95, 105)
        .where(result["has_cover_estimate"])
    )
    result["observation_date"] = result["observation_date"].dt.strftime("%Y-%m-%d")

    output_columns = [
        "observation_id",
        "site_id",
        "site_name",
        "habitat_context",
        "observation_date",
        "observation_sequence",
        "days_since_previous_observation",
        "record_origin",
        "review_status",
        "has_cover_estimate",
        *SURFACE_COVER_COLUMNS,
        "surface_cover_sum_pct",
        "surface_cover_sum_within_tolerance",
        "vegetation_cover_change_pp",
        "source_photo_count",
        *count_columns,
        "notes",
    ]
    return result[output_columns]


def write_analysis_ready_export(
    data_dir: Path,
    output_path: Path | None = None,
    *,
    line_terminator: str = "\n",
) -> int:
    """Build the analysis-ready CSV from canonical table exports."""

    observations = pd.read_csv(
        data_dir / "observations.csv", dtype={"site_id": "string"}
    )
    sites = pd.read_csv(data_dir / "site_registry.csv", dtype={"site_id": "string"})
    photos = pd.read_csv(data_dir / "photo_manifest.csv", dtype={"site_id": "string"})
    table = build_analysis_table(observations, sites, photos)
    destination = output_path or data_dir / "analysis_ready_observations.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(destination, index=False, lineterminator=line_terminator)
    return len(table)

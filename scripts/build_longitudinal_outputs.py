#!/usr/bin/env python3
"""Build the public analysis table and first longitudinal cover figure."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" / "public"
DEFAULT_TABLE_PATH = DEFAULT_DATA_DIR / "analysis_ready_observations.csv"
DEFAULT_FIGURE_PATH = ROOT / "outputs" / "figures" / "site_01_cover_over_time.png"

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
        result["surface_cover_sum_pct"].between(95, 105).where(result["has_cover_estimate"])
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


def build_cover_figure(table: pd.DataFrame, output_path: Path, site_id: str = "1") -> None:
    """Plot complete cover estimates for one site."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    site_rows = table[
        table["site_id"].astype("string").eq(str(site_id)) & table["has_cover_estimate"]
    ].copy()
    if site_rows.empty:
        raise ValueError(f"No complete cover estimates are available for site {site_id}.")

    site_rows["observation_date"] = pd.to_datetime(site_rows["observation_date"])
    site_rows = site_rows.sort_values("observation_date")
    site_name = site_rows["site_name"].iloc[0]

    colors = {
        "estimated_total_vegetation_cover_pct": "#2F6B3A",
        "bare_ground_pct": "#A65E2E",
        "gravel_rock_pct": "#667085",
        "litter_mulch_woodchip_pct": "#9B7B4F",
        "standing_dead_pct": "#C29B6C",
    }
    labels = {
        "estimated_total_vegetation_cover_pct": "Live vegetation",
        "bare_ground_pct": "Bare ground",
        "gravel_rock_pct": "Gravel / rock",
        "litter_mulch_woodchip_pct": "Litter / mulch / wood chips",
        "standing_dead_pct": "Standing dead",
    }

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=160)
    fig.patch.set_facecolor("#F7F8F5")
    ax.set_facecolor("#F7F8F5")

    plotted_columns = [
        column
        for column in SURFACE_COVER_COLUMNS
        if column in site_rows and site_rows[column].fillna(0).ne(0).any()
    ]
    for column in plotted_columns:
        ax.plot(
            site_rows["observation_date"],
            site_rows[column],
            color=colors[column],
            label=labels[column],
            linewidth=2.8,
            marker="o",
            markersize=7,
            markeredgecolor="#FFFFFF",
            markeredgewidth=1.4,
        )

    decreases = site_rows[site_rows["vegetation_cover_change_pp"].le(-20)]
    for _, row in decreases.iterrows():
        ax.annotate(
            f"{int(row['vegetation_cover_change_pp'])} pp vegetation",
            xy=(row["observation_date"], row["estimated_total_vegetation_cover_pct"]),
            xytext=(0, -34),
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=9,
            color="#344054",
            arrowprops={"arrowstyle": "-", "color": "#98A2B3", "linewidth": 1.2},
        )

    ax.set_ylim(0, 100)
    ax.set_ylabel("Estimated surface cover (%)", fontsize=11, color="#344054")
    ax.set_xlabel("")
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=1))
    ax.set_xticks(site_rows["observation_date"])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(
        ax.get_xticklabels(),
        rotation=30,
        ha="right",
        rotation_mode="anchor",
    )
    ax.tick_params(axis="both", colors="#475467", labelsize=10)
    ax.grid(axis="y", color="#D0D5DD", linewidth=0.8, alpha=0.75)
    ax.grid(axis="x", visible=False)
    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.legend(
        loc="upper left",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0, 1.03),
        fontsize=10,
        handlelength=2.4,
    )
    fig.suptitle(
        f"Site {site_id}: {site_name}",
        x=0.08,
        y=0.96,
        ha="left",
        fontsize=20,
        fontweight="bold",
        color="#101828",
    )
    fig.text(
        0.08,
        0.91,
        f"Photo-backed cover estimates across {len(site_rows)} monitoring rounds.",
        ha="left",
        fontsize=11,
        color="#475467",
    )
    fig.text(
        0.08,
        0.025,
        "Post-hoc visual estimates from archived quadrat and context photos. "
        "Changes are descriptive; disturbance events are not independently confirmed.",
        ha="left",
        fontsize=8.5,
        color="#667085",
    )
    fig.subplots_adjust(left=0.08, right=0.97, top=0.82, bottom=0.15)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--table-output", type=Path, default=DEFAULT_TABLE_PATH)
    parser.add_argument("--figure-output", type=Path, default=DEFAULT_FIGURE_PATH)
    parser.add_argument("--site-id", default="1")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    observations = pd.read_csv(args.data_dir / "observations.csv", dtype={"site_id": "string"})
    sites = pd.read_csv(args.data_dir / "site_registry.csv", dtype={"site_id": "string"})
    photos = pd.read_csv(args.data_dir / "photo_manifest.csv", dtype={"site_id": "string"})

    table = build_analysis_table(observations, sites, photos)
    args.table_output.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.table_output, index=False)
    build_cover_figure(table, args.figure_output, site_id=args.site_id)

    reviewed = int(table["has_cover_estimate"].sum())
    print(f"Wrote {len(table)} analysis rows ({reviewed} with complete cover estimates).")
    print(f"Wrote figure: {args.figure_output}")


if __name__ == "__main__":
    main()

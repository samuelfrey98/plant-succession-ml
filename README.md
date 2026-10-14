# Plant Succession ML Project

An independent longitudinal field-data engineering project tracking vegetation change across disturbed
urban edge habitats in Phoenixville, Pennsylvania.

The pipeline converts a real archive of repeatable field photography into sanitized relational tables,
photo-backed observation records, validation results, and reproducible longitudinal outputs. The image
archive can support later computer-vision experiments, but the current portfolio focus is data engineering.

**11 sites | 63 visit folders | 63 photo-backed observations (18 reviewed) | 505 field photos | May 25-July 20, 2026**

## Project Status

The photo archive has been inventoried and normalized into a public metadata manifest. One
observation shell is generated for each non-empty site/date photo batch; ecological fields remain
pending until visual review supports a defensible value.

Current implemented assets:

- 11 monitoring sites, including one later-established sensitive-fern disturbance site
- A structured Google Form for site observations
- A Google Sheets workbook with site, observation, and disturbance-event tables
- Repeatable photo protocols for wide, quadrat, close-up, and context photos
- Initial schema design for ecological observations and disturbance history
- A sanitized 505-row photo manifest with deterministic IDs and logical archive paths
- A 63-row observation table derived from photo-backed site visits, with explicit provenance
- A 63-row upload-batch summary used to audit completeness independently of observation content
- Validation for identifiers, site references, dates, counts, classification state, and private-link leakage
- A private Google Sheets review workbook synchronized with the public CSV structure
- Complete six-round reviews for Sites 1-3, with 156 verified photo roles and uncertainty-aware cover estimates
- A reproducible analysis-ready join with explicit completeness and longitudinal fields
- Generated cover-change figures for the completed Site 1-3 sequences

The project does not yet include a trained machine-learning model. Photo roles are deliberately marked
`unclassified` until visual review; camera order is not treated as a reliable label.

## Longitudinal Results

![Site 1 cover estimates across six monitoring rounds](outputs/figures/site_01_cover_over_time.png)

Site 1 now has six reviewed observations spanning May 25 through July 20. The photo-backed estimates
show two substantial decreases in live vegetation cover followed by recovery. The images are consistent
with mowing or clearing, but the repository does not promote those visual clues to confirmed disturbance
events without independent evidence. This is the first completed sequence; the remaining observation
shells preserve missing values until their photos are reviewed.

![Site 2 cover estimates across six monitoring rounds](outputs/figures/site_02_cover_over_time.png)

Site 2 provides a contrasting sequence: a mugwort-dominated roadside edge progresses toward nearly
continuous herbaceous cover. The June 1 dip may partly reflect quadrat framing, so it remains a descriptive
estimate rather than evidence of a disturbance event.

![Site 3 cover estimates across six monitoring rounds](outputs/figures/site_03_cover_over_time.png)

Site 3 follows colonization of coarse construction gravel. Vegetation remains concentrated along the
fence and trail margins but increases from an estimated 25% to 55% cover across the six rounds. A
prominent mugwort-like taxon is recorded as `Artemisia vulgaris` with `likely` confidence; the estimate
is not expert-verified, and no disturbance event is inferred from the sequence.

## Review Workflow

The repository CSVs are the versioned, sanitized public record. A private Google Sheets workbook named
`Plant Succession ML - Data Review` mirrors those tables so photo-backed observations can be reviewed
and corrected without exposing Drive links or exact coordinates in GitHub.

Reviews are completed one site/date batch at a time. Photo roles are assigned only after visual
inspection, ecological values are recorded as rounded post-hoc estimates when the images support them,
and uncertain taxa remain blank or use an explicitly uncertain label. See
[docs/data_review_workflow.md](docs/data_review_workflow.md) for the synchronization rules.

## Research Question

How do disturbed urban edge habitats change over time after mowing, construction, soil disturbance, pruning, herbicide damage, flooding, and other disturbance events?

Secondary questions:

- Which taxa dominate shortly after disturbance?
- How quickly do bare ground, gravel, live vegetation, litter, and standing dead cover change?
- Do invasive edge species expand, recover, or decline after disturbance?
- Which sites move from early colonization toward dense herbaceous cover or shrub establishment?
- Can repeatable field photos and structured notes support ML-assisted change detection later?

## Data Sources

The project currently uses:

- Google My Maps / Google Earth site pins
- Google Drive folders containing the original field-photo archive
- A private Google Sheets workbook used for visual review and correction
- Sanitized CSV snapshots for sites, observations, disturbance events, and photo metadata
- Post-hoc visual percent-cover estimates where the archived photos support them

Google Forms was the original collection design, but bulk photo uploads proved too slow in practice. The
real dataset is therefore reconstructed from the Drive archive with explicit provenance and missingness.

Raw photos, private Drive exports, and exact site data should not be committed directly to this public repository unless intentionally sanitized.

## Field Protocol Summary

Each monitoring visit creates one observation row per site visited. A routine visit records:

- site identifier
- observation date
- dominant, secondary, and tertiary taxa when visible
- taxonomic confidence and rank
- flowering and seed-set indicators
- estimated cover values for live vegetation, bare ground, gravel/rock, litter/mulch, and standing dead material
- notes about uncertainty, disturbance, or photo deviations

Recommended photo set per site visit:

- `wide_photo`: main repeatable site view
- `quadrat_photo`: ground-level or downward cover-estimation view
- `close_up_photo`: dominant or newly detected plants
- `context_photo_left`: left-side context view
- `context_photo_right`: right-side context view

Photo instructions use fixed landmarks, stable standing locations, consistent zoom, and phone gridline alignment where possible.

Annotated examples are available in [docs/protocol_photos/](docs/protocol_photos/).

## Repository Structure

```text
plant-succession-ml/
  README.md
  docs/
    field_protocol.md
    photo_protocol.md
    data_dictionary.md
    site_selection.md
    ml_roadmap.md
    portfolio_notes.md
  data/
    public/
    sample/
    schema/
    raw/
    processed/
  notebooks/
  src/plant_succession/
  scripts/
  outputs/
```

## Schema Overview

The project is organized around three core tables:

- `site_registry`: one row per monitoring site
- `observations`: one row per site visit
- `disturbance_events`: one row per observed or inferred disturbance event
- `photo_manifest`: one row per uploaded field photo
- `photo_batch_summary`: one row per site/date upload folder

The public photo manifest links each photo to a deterministic site/date observation ID without exposing
private Drive file IDs, links, or exact coordinates.

Each schema file in `data/schema/` uses:

```text
field_name,data_type,required,allowed_values,description,example
```

## Current Limitations

- Species identifications are field estimates and are not expert-verified unless stated.
- Fifty photo-backed observation shells still await visual review.
- Photo workflows are being revised because Google Forms file uploads can be awkward for large iPhone images.
- Successional-stage labels are interpretive and should not replace raw cover and taxon fields.
- No trained ML model is included yet.
- This is an independent portfolio project, not an official iNaturalist, municipal, or land-manager project.

## Analysis And ML Roadmap

Near-term work focuses on clean data collection and validation. Later stages include:

1. Manual structured observations
2. Repeatable photo archive
3. Cover-change summaries and disturbance-response plots
4. Taxon label cleanup
5. Image annotation experiments
6. Computer vision for vegetation cover and bare-ground segmentation
7. ML-assisted site-change detection
8. Public static report or dashboard

See [docs/ml_roadmap.md](docs/ml_roadmap.md) for the staged plan.

For portfolio and application framing, see [docs/visibility_plan.md](docs/visibility_plan.md).

## Quick Start

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Validate the sample and public data:

```bash
python scripts/validate_csvs.py
```

Rebuild the public photo metadata when a private Drive inventory export is available:

```bash
python scripts/build_photo_manifest.py
```

Initialize or refresh photo-backed observation rows while preserving reviewed fields:

```bash
python scripts/build_observation_backfill.py
```

Build the analysis-ready public table and Site 1 longitudinal figure:

```bash
python scripts/build_longitudinal_outputs.py
```

Export sample sites with coordinates to GeoJSON:

```bash
python scripts/export_geojson.py
```

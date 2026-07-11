# Plant Succession ML Project

Field-data infrastructure for tracking plant succession, disturbance response, and vegetation change across disturbed urban edge habitats in Phoenixville, Pennsylvania.

This project combines repeatable field photography, structured observation forms, disturbance-event logging, and geospatial site tracking to build a small longitudinal dataset for later ecological analysis and computer-vision experiments.

## Project Status

This repository is an early public version of an active field project. The current work is focused on building credible monitoring infrastructure:

- 10 mapped monitoring sites in Google My Maps / Google Earth
- A structured Google Form for site observations
- A Google Sheets workbook with site, observation, and disturbance-event tables
- Repeatable photo protocols for wide, quadrat, close-up, and context photos
- Initial schema design for ecological observations and disturbance history
- Planned validation, analysis, geospatial export, and ML-ready photo organization

The project does not yet include a trained machine-learning model. The immediate goal is to collect clean, repeatable, well-documented data that could support later computer-vision and geospatial analysis.

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
- Google Forms observation submissions
- Google Sheets tables for site registry, observations, and disturbance events
- Google Drive folders for field photos
- Manual field notes and visual percent-cover estimates

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

A future `photo_manifest` table links observation rows to individual Drive photo files or folders.

Each schema file in `data/schema/` uses:

```text
field_name,data_type,required,allowed_values,description,example
```

## Current Limitations

- Species identifications are field estimates and are not expert-verified unless stated.
- The dataset is small and still being designed.
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

Validate the sample data:

```bash
python scripts/validate_csvs.py
```

Export sample sites with coordinates to GeoJSON:

```bash
python scripts/export_geojson.py
```

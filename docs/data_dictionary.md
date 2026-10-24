# Data Dictionary

Canonical schema files live in `data/schema/`. This document summarizes the intent of each table.

## site_registry

One row per monitoring site. Site IDs should remain stable and should not be renumbered casually.

Core fields:

- `site_id`: stable site identifier
- `site_name`: human-readable name
- `habitat_context`: broad setting such as trail edge, gravel fill, woodland edge, or construction edge
- `dominant_initial_disturbance`: initial disturbance context
- `photo_anchor_notes`: repeatable photo instructions
- `map_url`: optional map link

## observations

One row per site visit. Observations record field-estimated vegetation condition, taxon labels, cover values, photo links, and notes.

Core fields:

- `observation_id`: stable observation identifier
- `site_id`: link to `site_registry`
- `observation_date`: date of field visit
- `dominant_species`, `secondary_species`, `tertiary_species`: best defensible taxon labels
- `dominant_taxon_rank`, `secondary_taxon_rank`, `tertiary_taxon_rank`: species, genus, family, unknown, or morphotype
- `new_taxon_labels`: semicolon-separated labels first recorded at the site during this visit, including taxa outside the three dominance slots
- `new_species_detected`: whether earlier dated site records support at least one label as newly recorded
- cover fields: integer estimates from 0 to 100
- photo link fields: links to Drive files or folders

## disturbance_events

One row per observed or inferred disturbance event. Disturbance is separate from observations because one event can affect multiple later observations.

Core fields:

- `disturbance_event_id`: stable event identifier
- `site_id`: link to `site_registry`
- `disturbance_window_start`: earliest possible event date/time
- `disturbance_window_end`: latest possible event date/time or date noticed
- `disturbance_event_type`: broad event class
- `disturbance_confidence`: observed, likely, or maybe
- `evidence`: field evidence supporting the event
- `source_field_note_id`: source-note lineage
- `site_check_id`: site-specific positive assessment supporting the event

## field_notes

One row per preserved source note. This table stores the original wording before it is normalized into
site-specific checks or disturbance events. Phenology and taxon notes belong here even when they do not
describe a disturbance.

Core fields:

- `field_note_id`: stable source-note identifier
- `note_date`: date the note was recorded
- `note_type`: disturbance, phenology, taxon, or general
- `site_scope`: site-specific, multi-site, between-sites, or route-general
- `related_site_ids`: semicolon-separated sites explicitly named or bounded by the note
- `raw_note`: preserved source wording
- `processing_status`: normalized, partially normalized, or unresolved

## site_checks

One row per site and disturbance type explicitly assessed by a source note. Positive and negative
checks use the same structure, but only `observed_event` checks may create disturbance-event rows.
Unmentioned sites are not checks and remain unknown.

Core fields:

- `site_check_id`: stable assessment identifier
- `field_note_id`: link to the source note
- `site_id`: site explicitly assessed
- `check_date`: date of the assessment
- `visit_context`: structured observation, incidental walk, targeted follow-up, or unknown
- `disturbance_assessment`: observed event, none seen, or not assessed
- `disturbance_type_assessed`: disturbance class evaluated
- `related_observation_id`: optional same-day structured observation

## photo_manifest

One row per field photo in the reviewed archive. It links each image to a deterministic observation
without exposing private Drive identifiers or links.

Core fields:

- `photo_id`
- `observation_id`
- `site_id`
- `observation_date`
- `photo_type`
- `filename`
- `relative_path`
- `review_status`


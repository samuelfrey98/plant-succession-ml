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

## photo_manifest

Optional future table with one row per photo. Useful when photos are uploaded directly to Drive folders rather than through Google Forms.

Core fields:

- `photo_id`
- `observation_id`
- `site_id`
- `observation_date`
- `photo_type`
- `drive_file_url`
- `local_filename`


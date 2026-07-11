# Field Protocol

This protocol is designed for repeatable, low-friction monitoring of disturbed urban edge habitats.

## Visit Cadence

Recommended cadence:

- weekly or near-weekly during the growing season
- additional visits after visible disturbance events, including mowing, pruning, construction, flooding, soil scraping, herbicide damage, or fresh gravel/fill

Each site visit should create one observation row.

## Observation Area

Before estimating cover or dominant taxa:

1. Stand at the site-specific photo anchor.
2. Identify the intended observation area.
3. Keep the observation area consistent across visits when possible.
4. Record any deviation in `notes`.

## Taxon Recording

Do not force species-level identification when confidence is low. Use the best defensible rank:

- species: `Artemisia vulgaris`
- genus: `Artemisia sp.`
- family: `Asteraceae sp.`
- morphotype: `unknown_grass_01`

Dominant taxon means the taxon contributing the most live vegetation cover in the observation area, not necessarily the tallest or most visually interesting plant.

## Cover Estimates

Estimate percent cover visually. Percent fields should be integers from 0 to 100.

The cover fields do not need to sum to exactly 100 because live vegetation, bare ground, gravel, litter, standing dead material, pavement edge, and water can overlap or be assessed differently in the field.

Recommended fields:

- `estimated_total_vegetation_cover_pct`
- `bare_ground_pct`
- `gravel_rock_pct`
- `litter_mulch_woodchip_pct`
- `standing_dead_pct`

Use blank/null for fields that were not assessed. Use `unknown` only when the field was considered but the answer is truly unknown and that distinction matters.

## Disturbance Notes

If disturbance is visible, record enough evidence to support later interpretation:

- cut stems
- fresh tire tracks
- exposed soil
- new gravel or fill
- browning vegetation
- scraped ground
- flood debris
- pruning debris
- construction materials

Log disturbance events separately in the `disturbance_events` table when possible.


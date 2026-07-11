# Photo Protocol

Repeatable photography is the core evidence layer for this project.

## Standard Photo Set

Each complete observation should include:

- `wide_photo`: main repeatable site photo
- `quadrat_photo`: ground-level or downward view for cover estimation
- `close_up_photo`: close view of dominant or newly detected plants
- `context_photo_left`: left-side context view
- `context_photo_right`: right-side context view

Extra photos are welcome when they document disturbance, new taxa, uncertainty, or unusual site conditions.

## Repeatability Rules

For each site:

- stand at the same anchor point when possible
- use stable landmarks such as building corners, fence posts, path edges, signs, rails, utility poles, or road lines
- keep zoom consistent, commonly `0.5x` for wide and context shots
- use phone gridlines to align the same landmarks each visit
- note unavoidable deviations in the observation row

Useful instruction language:

- `align the fence post with the left vertical gridline`
- `center the building corner on the middle vertical gridline`
- `place the trail edge along the lower horizontal gridline`
- `keep the signpost near the upper-right grid intersection`

## Workflow 1: Google Form File Uploads

This is the simplest workflow for field entry because photos and observation fields are submitted together.

Advantages:

- quick field capture
- automatic timestamping
- direct link between form response and uploaded files

Limitations:

- large iPhone photos may exceed upload or conversion limits
- exported files can be cumbersome to rename and organize
- Drive folder structure is controlled by the form

## Workflow 2: Drive Folders And Photo Manifest

This is the preferred long-term workflow.

Recommended folder pattern:

```text
Plant Succession ML Project/
  photos/
    site_01/
      2026-05-25/
        site_01_2026-05-25_wide.jpg
        site_01_2026-05-25_quadrat.jpg
        site_01_2026-05-25_closeup_01.jpg
        site_01_2026-05-25_context_left.jpg
        site_01_2026-05-25_context_right.jpg
```

The observation row stores either a folder link or links to individual files. The `photo_manifest` table can then record one row per photo.

Advantages:

- more control over naming and organization
- easier export for computer-vision experiments
- cleaner separation between field observations and photo files

Raw photos should stay out of GitHub unless intentionally compressed, anonymized, and selected as examples.

## Protocol Photo Examples

Annotated protocol screenshots are available in [protocol_photos/](protocol_photos/). These are compressed documentation copies that show standing locations, gridline alignment, visual landmarks, and context-photo framing.

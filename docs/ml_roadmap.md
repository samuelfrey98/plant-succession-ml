# ML Roadmap

This project starts with field-data quality, not model training. The ML work becomes meaningful only after repeatable observations and photos exist.

## Stage 1: Manual Structured Observations

Build stable site, observation, and disturbance-event tables. Validate required fields, dates, categorical values, site IDs, and percent-cover ranges.

## Stage 2: Repeatable Photo Archive

Collect repeatable wide, quadrat, close-up, and context photos. Store raw photos in Drive or local archival storage, not directly in GitHub.

## Stage 3: Cover Change And Disturbance Response

Summarize live vegetation, bare ground, gravel/rock, litter, and standing dead cover over time. Compare cover trajectories before and after disturbance events.

## Stage 4: Taxon Label Cleanup

Normalize taxon labels. Preserve uncertainty through `taxon_rank` and `confidence` fields instead of forcing species-level IDs.

## Stage 5: Image Annotation Experiments

Select a small set of representative photos and annotate coarse classes such as live vegetation, bare ground, gravel, litter, standing dead vegetation, pavement, and water.

## Stage 6: Cover Segmentation

Experiment with computer-vision approaches for vegetation cover and bare-ground segmentation. Early models should be treated as exploratory, not authoritative.

## Stage 7: Site Change Detection

Use repeatable photos and structured observations to detect visible change over time, especially after mowing, construction, herbicide damage, flooding, or soil scraping.

## Stage 8: Public Report Or Dashboard

Publish a static report or dashboard showing site maps, cover trends, photo pairs, disturbance timelines, and limitations.

## Do Not Overclaim

The project should not claim that a trained ML model exists, species IDs are expert-verified, the dataset is large, or results are scientifically conclusive until that is true.


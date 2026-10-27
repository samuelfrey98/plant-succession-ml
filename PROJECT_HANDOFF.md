# Project Handoff

Version 1.0.0 packages the working field-photo ingestion, review, verification, publication, dashboard,
and reproducible-demo workflow for plant succession monitoring.

The public-facing version should stay honest about current maturity:

- 11 sites and 75 observations are represented in sanitized public metadata
- all 596 historical photo roles have been visually reviewed
- 30 observations are fully reviewed and 45 are partially reviewed
- optional general-purpose vision drafts support review, but no custom ecological model is trained
- the public demo uses synthetic images and disposable, read-only state

The public-facing version should not claim:

- a trained ML model already exists
- species IDs are expert-verified
- the dataset is large
- results are scientifically conclusive
- the project is affiliated with iNaturalist, Phoenixville, or any land manager

Post-v1.0 priorities:

1. Add CI for unit tests, CSV validation, and demo-seed smoke tests.
2. Add role-based reviewer identities if multiple human reviewers begin using the service.
3. Build evaluation sets for cover estimation and taxon triage before training a custom model.
4. Add deployment observability and automated encrypted backups for the private service.
5. Publish a hosted demo only after the tagged release is pushed to GitHub.


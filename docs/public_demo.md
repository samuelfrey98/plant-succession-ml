# Reproducible Public Demo

The demo presents the complete ingestion state machine without exposing private photos, exact private
site anchors, credentials, or the operational SQLite database. It is a separate runtime, not a special
view over the real workspace.

## What It Contains

The seed copies the repository's sanitized `data/public/*.csv` exports into `.demo/public/` and creates
four synthetic three-photo batches:

| Example | Pipeline state | Purpose |
| --- | --- | --- |
| Site 4, October 22 | Needs review | Shows unresolved photo-role labels |
| Site 8, October 21 | Observation review | Shows a partial draft and verification guidance |
| Site 3, October 20 | Ready to publish | Shows a complete review before publication |
| Site 9, October 19 | Published | Shows validated publication and lineage |

Every generated image says `SYNTHETIC DEMO` inside the bitmap. The demo observation notes repeat that
the values are not field observations.

## Run Locally

From the repository root:

```bash
.venv/bin/pip install -e .
.venv/bin/python scripts/run_demo.py --reset
```

Open `http://127.0.0.1:8766`. The default is read-only, matching the hosted behavior.

To exercise uploads, labeling, review, and publication against disposable data:

```bash
.venv/bin/python scripts/run_demo.py --reset --writable
```

Stop the process with `Ctrl+C`. A later launch without `--reset` keeps local demo edits. A launch with
`--reset` deletes only `.demo/` and rebuilds the baseline.

Use another port when needed:

```bash
.venv/bin/python scripts/run_demo.py --reset --port 8791
```

## Isolation Contract

The demo uses only these ignored paths:

- `.demo/public`: disposable CSV exports
- `.demo/state`: disposable SQLite and preview state
- `.demo/photos`: disposable published synthetic images
- `.demo/demo_manifest.json`: seed version and generated batch identifiers

The normal paths `data/public`, `data/raw`, and `photos` are never passed to the demo service. The hosted
mode also rejects every non-read request at the server middleware, even if a client bypasses the disabled
browser controls. Machine review and Google Sheets synchronization have no credentials in the demo.

## Verify the Baseline

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/validate_csvs.py --public-dir .demo/public --skip-sample
curl --fail http://127.0.0.1:8766/api/health
curl --fail http://127.0.0.1:8766/api/config
```

The configuration response should include `"demo_mode": true` and `"read_only": true`.

## Deploy on Render

Render supports a custom Blueprint path. Create a new Blueprint for the GitHub repository and set its
Blueprint Path to `render.demo.yaml`. The file uses the free web-service plan, disables automatic deploys,
starts from a fresh seed on every service start, and provisions no persistent disk or secret environment
variables.

The free service can restart or spin down. That is intentional: each start reconstructs the same baseline
from committed public exports and synthetic images. The operational deployment remains separately defined
in `render.yaml` and should retain authentication and durable storage.

Render references:

- [Infrastructure as Code](https://render.com/docs/infrastructure-as-code)
- [Blueprint YAML reference](https://render.com/docs/blueprint-spec)
- [Free web services](https://render.com/docs/free)

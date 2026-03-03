# API Core

`api-core` is the main backend in this demo system. It stores session data, tracks usage and cost, exposes contract-change APIs, and kicks off the remediation workflow when an OpenAPI contract changes.

## What It Handles

- AI session and team APIs
- Usage, billing, and analytics endpoints
- Contract diffing and impact analysis
- Devin dispatch and status sync
- Optional webhook calls to the notification service

## Important Folders

- `src/`: FastAPI app, routes, middleware, database code
- `propagate/`: contract-change pipeline and Devin orchestration
- `scripts/`: local development helpers
- `tests/`: backend tests
- `openapi.yaml`: source contract used by the propagation flow

## Quick Start

```bash
pip install -r requirements.txt
python scripts/run_dev_server.py
```

The dev server starts `src.main:app` on `127.0.0.1:8001` by default.

## Useful Commands

```bash
# run the API directly
uvicorn src.main:app --host 127.0.0.1 --port 8001 --reload

# run the propagation workflow
python -m propagate --dry-run

# run tests
pytest
```

## Environment Variables

Common settings use the `API_CORE_` prefix:

- `API_CORE_DATABASE_URL`
- `API_CORE_API_KEY`
- `API_CORE_DEVIN_API_KEY`
- `API_CORE_GITHUB_TOKEN`
- `API_CORE_NOTIFICATION_WEBHOOK_URL`

## Main Endpoints

- `/health`
- `/api/v1/sessions`
- `/api/v1/teams`
- `/api/v1/analytics/*`
- `/api/v1/contracts/*`

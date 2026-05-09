# Financial Report Analyzer AI Report

FastAPI backend for credit-report creation, evidence ingestion, canonical fact storage,
calculation workflows, and Gemini-powered section generation.

## Architecture

```text
Client / Frontend
  -> FastAPI (/api/credit-report)
      -> Auth / RBAC / Audit
      -> Reports, section inputs, uploaded evidence PDFs
      -> Fact Store and Calculation Engine
      -> Evidence retrieval and Gemini generation pipeline
      -> Block AST for editable generated sections
  -> SQL database (SQLite for local dev, Postgres in production)
  -> Filesystem storage for extracted evidence text
```

Important modules:

- `main.py` configures the FastAPI app, CORS, runtime security checks, optional
  local table creation, and admin seeding.
- `credit_report/api/` contains the API routers for auth, reports, generation,
  facts, conflicts, calculations, blocks, and audit.
- `credit_report/generation/` contains evidence extraction/retrieval, prompt
  building, Gemini client integration, token quota tracking, and generation
  orchestration.
- `credit_report/fact_store/` stores normalized canonical facts, versions,
  conflicts, overrides, and state transitions.
- `credit_report/calculation_engine/` stores FX rates, financial ratios, DSCR,
  collateral metrics, mapping rules, and unmapped line-item queues.
- `credit_report/block_ast/` converts generated Markdown into editable blocks and
  table cells with fact bindings.

## Local development

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in local values, especially:

   - `SECRET_KEY`
   - `GEMINI_API_KEY` if you will call generation endpoints
   - `ADMIN_EMAIL` / `ADMIN_PASSWORD` if you want startup admin seeding

4. Run migrations or use local auto-create tables:

   ```bash
   alembic upgrade head
   ```

   For quick local-only SQLite development, `.env.example` sets
   `AUTO_CREATE_TABLES=true`; production should leave this disabled and use
   Alembic migrations.

5. Start the API:

   ```bash
   uvicorn main:app --reload
   ```

6. Open the friendly report UI at `http://127.0.0.1:8000/app`, or API docs at `http://127.0.0.1:8000/docs`.

## Browser UI

The app serves a no-build browser interface from `/app`. The UI supports:

- logging in with an existing backend user and logging out to clear stored UI session state;
- creating and selecting credit-report cases;
- editing section JSON inputs with starter templates for sections 1–10;
- uploading and deleting PDF evidence documents;
- generating one section or the full report;
- loading and copying generated Markdown output.

The root path `/` redirects to `/app` for convenience. In environments without a
browser or screenshot tooling, run `scripts/ui_smoke_test.py` to validate the UI
mount, static assets, login, report creation, section-input save/load, and
document-list flow through HTTP.


## GitHub update and UI verification workflow

Use this flow when publishing UI changes through GitHub:

1. Push the branch that contains `credit_report/ui/*`, `main.py`, and any backend
   changes needed by the UI.
2. Open a Pull Request. GitHub Actions runs `.github/workflows/ci.yml`, which
   installs dependencies, compiles Python, runs the unit tests, checks the UI
   JavaScript syntax, and runs the browser-free UI smoke test.
3. Confirm the PR checks are green before merging. The UI smoke test starts a
   temporary local app and verifies `/app`, `/app/styles.css`, `/app/app.js`,
   login, report creation/listing, section input save/load, and document-list
   API flow.
4. Merge to the deployment branch (`main` or `master`). Render can then build
   from `render.yaml`; it installs dependencies, runs `alembic upgrade head`,
   and starts `uvicorn main:app`.
5. After deployment, open `https://<your-service-domain>/app`, log in with the
   configured admin or analyst account, create/select a report, save a section
   JSON input, optionally upload PDF evidence, and generate a section.

If GitHub Actions is unavailable, run the same checks locally before pushing:

```bash
python -m pip install -r requirements.txt
python -m compileall -q credit_report main.py scripts tests conftest.py pytest_asyncio.py
python -m pytest -q
node --check credit_report/ui/app.js
scripts/ui_smoke_test.py
```


### UI troubleshooting on Render

If `https://<your-service-domain>/app` returns `{"detail":"Not Found"}`, the
running Render instance is almost certainly not serving a build that includes the
static UI mount. Redeploy the commit that contains `main.py` mounting `/app` and
`credit_report/ui/*`, then test both:

```text
https://<your-service-domain>/app
https://<your-service-domain>/app/
```

The app explicitly redirects bare `/app` to `/app/`, and `/` redirects to the UI.
If the URL still returns 404 after redeploy, verify Render built from the branch
containing the UI commit and that the service start command is `uvicorn main:app`.

## Core API flow

All credit-report routes are mounted under `/api/credit-report`.

1. Register or seed an admin, then log in with `/auth/login`.
2. Create a report with `POST /reports`.
3. Save section inputs with `PUT /reports/{report_id}/inputs/{section_no}`.
4. Upload PDF evidence with `POST /reports/{report_id}/documents`.
5. Generate one section with `POST /reports/{report_id}/generate/{section_no}`
   or all sections with `POST /reports/{report_id}/generate`.
6. Retrieve generated Markdown with
   `GET /reports/{report_id}/sections/{section_no}/output`.
7. Use facts/calculation/block endpoints for review, binding, validation, and
   downstream editing workflows.

## Security and data isolation

- Production startup fails when `SECRET_KEY` is still the development default.
- Production startup also fails when `CORS_ALLOW_ORIGINS` contains `*`; configure
  explicit trusted frontend origins instead.
- CORS credentials are only enabled when wildcard origins are not configured.
- Report visibility is role-aware:
  - `analyst`: can view own reports only.
  - `reviewer` / `approver` / `admin`: can view all reports.
- Report mutation, section input writes, document uploads/deletes, and generation
  require the report owner or an admin.
- Approving a report requires the `approver` or `admin` role.

## Gemini generation configuration

Generation uses the Gemini client in `credit_report/generation/gemini_client.py`.
Relevant environment variables:

- `GEMINI_API_KEY`: required for real generation calls.
- `GEMINI_MODEL`: default Gemini model name.
- `GENERATION_MODEL_ID`: optional override used by the client and persisted in
  `SectionOutput.model_id`; defaults to `GEMINI_MODEL`.
- `CR_SECTION_MAX_TOKENS`: default output-token cap.
- `CR_MAX_CONCURRENT_GENERATIONS`: process-local concurrency limit.
- `DAILY_TOKEN_LIMIT`: per-user token quota baseline.

`credit_report/generation/claude_client.py` remains only as a temporary import
compatibility shim and delegates to the Gemini implementation.

## Database migrations and production deployment

Production deployments should run:

```bash
alembic upgrade head
```

before application startup. Runtime table creation is controlled by
`AUTO_CREATE_TABLES` and defaults to `false` in production. Render is configured
to run Alembic during build and to set `AUTO_CREATE_TABLES=false`.

When deploying, configure:

- `ENVIRONMENT=production`
- `SECRET_KEY` as a strong generated secret
- `CORS_ALLOW_ORIGINS` as explicit origins, for example
  `https://app.example.com`
- `DATABASE_URL` for Postgres or another supported SQLAlchemy async URL
- `GEMINI_API_KEY`

## Testing and checks

Recommended checks before merging:

```bash
python -m compileall -q credit_report main.py scripts tests
python -m pytest -q
scripts/ui_smoke_test.py
node --check credit_report/ui/app.js
```

The repository includes a small local async pytest shim (`pytest_asyncio.py` plus
`conftest.py`) so tests can run even in sandboxes where installing the external
`pytest-asyncio` package is blocked.

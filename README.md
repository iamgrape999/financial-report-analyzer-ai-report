# Financial Report Analyzer

AI-powered credit report generation platform for Cathay United Bank analysts. Accepts uploaded financial documents (PDF, DOCX, PPTX, XLSX), runs Gemini OCR + ETL, stores canonical facts, and generates structured 10-section credit reports in Markdown. Staff can review, edit (block-level), and export reports.

**Stack**: FastAPI · SQLite (dev) / PostgreSQL (prod) · Google Gemini 2.5 Flash · Single-file SPA  
**Deployment**: Render (single service, `render.yaml`)

---

## Architecture

```
main.py                      FastAPI app + lifespan (startup, CORS, middleware)
credit_report/
  api/
    auth.py                  JWT login / register, role-based access
    reports.py               Report CRUD + workflow (submit / approve / recall)
    generate.py              Document upload, ETL pipeline, Gemini generation
    blocks.py                Block-level editing + AI paragraph improvement
    export.py                DOCX export; PDF returns 503 (no weasyprint on Render)
    audit.py                 Per-report audit trail (DESC order, newest first)
    facts.py                 CanonicalFact CRUD + state transitions
    conflicts.py             Conflict detection and resolution
    calculations.py          Derived ratio engine + LTV/ACR + FX rates
  generation/
    pipeline.py              run_section_generation() — core generation loop
    etl.py                   Gemini ETL → CanonicalFacts (§7 uses dynamic FY_YYYY nesting)
    evidence.py              PDF / DOCX / XLSX text extraction + chunking
    claude_client.py         Gemini wrappers (generate_section_markdown, call_gemini_raw)
    prompt_builder.py        System + user prompt construction
  block_ast/                 Markdown block AST; savepoint-based persistence
  fact_store/                CanonicalFact storage + conflict detection
  calculation_engine/        Financial ratio derivation (EBITDA, LTV, DSCR, etc.)
  security/                  User model, JWT (HS256), RBAC
  audit/                     AuditEvent model + write_event()
static/index.html            Single-file SPA (Bootstrap 5 + marked.js + DOMPurify)
tests/                       2800+ pytest tests
```

**API prefix**: `/api/credit-report/`  
**Generation order**: §4 → §7 → §1 → §3 → §2 → §5 → §6 → §8 → §9 → §10

---

## Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Set `mock-key-for-testing` in tests |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Also `GEMINI_OCR_MODEL`, `GEMINI_ETL_MODEL` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/credit_report.db` | PostgreSQL in prod |
| `SECRET_KEY` | *(required in prod)* | JWT signing key — must not be default |
| `CORS_ALLOW_ORIGINS` | `*` (dev) | Set to `https://your-app.onrender.com` in prod |
| `ADMIN_EMAIL` | — | Bootstrap admin account email |
| `ADMIN_PASSWORD` | — | Bootstrap admin password (first-run only) |
| `ADMIN_BOOTSTRAP_OVERRIDE` | `false` | Set `true` to force-sync admin credentials from env |
| `DAILY_TOKEN_LIMIT` | `4000000` | Per-analyst daily Gemini token limit |
| `ENVIRONMENT` | `development` | Set `production` on Render |
| `TRUSTED_PROXY_IPS` | — | Set to enable x-forwarded-for IP trust |
| `CREDIT_REPORT_MAX_UPLOAD_MB` | `50` | Maximum upload file size |

---

## Local Development

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env: set GEMINI_API_KEY, ADMIN_EMAIL, ADMIN_PASSWORD

# 3. Run server (auto-creates SQLite DB on first start)
uvicorn main:app --reload

# 4. Open UI
open http://localhost:8000/app
```

---

## Testing

```bash
# Full suite (must stay green before any commit)
python -m pytest tests/ -q

# Quick smoke test (excluding fuzzing)
python -m pytest tests/ -q --ignore=tests/test_api_schema_fuzzing.py \
  --ignore=tests/test_auth_user_mgmt_fuzzing.py

# Single file
python -m pytest tests/test_etl_improvements.py -v

# Test health report
python3 scripts/test_health_report.py --print
```

`pytest.ini` uses `--reruns 2 --reruns-delay 3` for SQLite lock transients.

---

## Database Migration

```bash
# Check current migration state
alembic current

# Apply pending migrations
alembic upgrade head

# Create a new migration
alembic revision --autogenerate -m "describe the change"
```

**Production rule**: Never modify schema in production by hand. Always use Alembic migrations.

---

## Deployment (Render)

1. Set all required env vars in Render dashboard (see table above)
2. Set `ENVIRONMENT=production`
3. Set `CORS_ALLOW_ORIGINS=https://your-app.onrender.com`
4. Set `SECRET_KEY` to a random 32+ char string
5. Set `GEMINI_API_KEY`
6. Set `ADMIN_EMAIL` + `ADMIN_PASSWORD` for first-run bootstrap
7. Connect a PostgreSQL database and set `DATABASE_URL`

The service starts with `uvicorn main:app --host 0.0.0.0 --port $PORT` per `render.yaml`.

---

## Security Boundaries

| Boundary | Protection |
|---|---|
| Authentication | JWT HS256 with `alg` header validation (prevents `alg: none` bypass) |
| Authorization | Role-based: `analyst` / `reviewer` / `approver` / `admin`; report ownership enforced on every endpoint |
| XSS | DOMPurify sanitizes all AI-generated Markdown before innerHTML |
| Path traversal | `_safe_report_dir()` resolves and validates all file paths against the reports root |
| File upload | Extension allowlist + 50 MB size limit + 180s extraction timeout |
| CORS | Wildcard only in dev; must be set to specific origin in production |
| Admin bootstrap | `_seed_admin()` creates account once; subsequent restarts skip credential sync unless `ADMIN_BOOTSTRAP_OVERRIDE=true` |
| Audit trail | Every fact change, state transition, and conflict resolution writes an `AuditEvent` |

---

## Roles

| Role | Permissions |
|---|---|
| `analyst` | Create reports, upload documents, run ETL, edit section inputs, override facts |
| `reviewer` | Approve / deprecate facts, review generated sections |
| `approver` | Submit reports for approval |
| `admin` | All of the above + user management + all reports visible |

---

## Common Gotchas

- **§7 ETL schema**: Gemini nests financials as `income_statement.{FY_2024}.revenue`. Use `_extract_section7_facts()` in `etl.py` — do not add flat entries to `_ETL_FACT_MAP`.
- **Audit trail order**: `GET /audit` returns events DESC (newest first). Tests asserting order must sort `reverse=True`.
- **Block AST savepoint**: `save_blocks()` runs inside `db.begin_nested()`. If it fails, only blocks roll back; the section markdown is still committed.
- **PDF export 503**: Expected when `weasyprint` is absent. Frontend falls back to browser print dialog. Do not change to 500.
- **SQLite WAL**: `busy_timeout=30000`. Two concurrent writes wait up to 30s before failing. One connection per async task.
- **Router order**: `conflicts.router` must be registered before `facts.router` in `router.py` so the concrete `/facts/conflicts` path matches before the wildcard `/{fact_id}`.

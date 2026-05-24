# Financial Report Analyzer — CLAUDE.md

> Builder / Reviewer scope document.
> Read this before touching any code. The rules here exist to stop scope creep,
> protect existing passing tests, and keep CI green.

---

## What this project is

An internal credit-analysis platform for Cathay United Bank analysts.
It accepts uploaded financial documents, runs Gemini OCR + ETL, stores
canonical facts, and generates a structured 10-section credit report in
Markdown. Staff can review, edit (block-level), and export the report.

**Stack**: FastAPI + SQLite (dev) / PostgreSQL (prod) + Gemini 2.5 Flash.
**Deployment**: Render (single service, `render.yaml`). No Docker.

---

## Architecture at a glance

```
main.py                      FastAPI app + lifespan
credit_report/
  api/
    auth.py                  JWT login/register, role-based
    reports.py               CRUD + workflow (submit / approve / recall)
    generate.py              Upload docs, ETL, trigger Gemini generation
    blocks.py                Block-level editing + AI paragraph improve
    export.py                DOCX export; PDF export (503 when no weasyprint)
    audit.py                 Per-report audit trail (desc order, newest first)
    facts.py                 CanonicalFact CRUD
    calculations.py          Derived ratio engine (/recalculate)
    conflicts.py             Conflict detection
  generation/
    pipeline.py              run_section_generation() — the core loop
    etl.py                   ETL: Gemini → CanonicalFacts (§7 uses dynamic FY_YYYY nesting)
    evidence.py              PDF/DOCX/XLSX text extraction + chunking
    claude_client.py         Gemini wrappers (generate_section_markdown, call_gemini_raw)
    prompt_builder.py        System + user prompt construction
    completeness.py          Post-generation table completeness checks
  block_ast/                 AST of markdown blocks; saved via savepoint (not new session)
  fact_store/                CanonicalFact persistence
  calculation_engine/        Ratio derivation (ebitda_margin_pct, etc.)
  security/                  User model, JWT, RBAC
  audit/                     AuditEvent model + write_event()
static/index.html            Single-file SPA (no build step)
tests/                       2 700+ pytest tests, all must stay green
```

**API prefix**: `/api/credit-report/reports/{report_id}/...`

**Generation order**: §4 → §7 → §1 → §3 → §2 → §5 → §6 → §8 → §9 → §10
(hard deps enforced — §2 requires §7 done first, etc.)

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Set `mock-key-for-testing` in tests |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Also `GEMINI_OCR_MODEL`, `GEMINI_ETL_MODEL` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/credit_report.db` | Postgres in prod |
| `DAILY_TOKEN_LIMIT` | `4_000_000` | Per-user per-day; multiplied by role |
| `ENVIRONMENT` | `development` | Set `production` on Render |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | see `.env.example` | Seeded on startup |
| `GEMINI_REVIEWER_API_KEY` | *(optional)* | Separate key for the code-review hook; blank = disabled |
| `GEMINI_REVIEWER_MODEL` | `gemini-2.5-flash` | Reviewer model; swap in Render env without redeploy |
| `GEMINI_REVIEWER_MAX_LINES` | `300` | Files larger than this are skipped by the hook |

---

## AI code-review hook

`scripts/codex_review.py` fires automatically on every `Edit`/`Write` to
production Python files via a Claude Code `PostToolUse` hook (`.claude/settings.json`).

- Reads the file just written, redacts secrets (`AIza…`, `sk-…`, `Bearer …`), and sends it to Gemini
- Prints ≤ 3 bullets for serious issues, or `✓ No critical issues.`
- Retries once on HTTP 429; surfaces other HTTP errors to stderr
- Always exits 0 — never blocks the coding session

Enable by setting `GEMINI_REVIEWER_API_KEY` (must start with `AIza`, ≥ 35 chars).
Verify with: `python3 scripts/test_codex_review.py` (37 assertions, ~2 s, no API cost).

---

## Testing

```bash
# Run everything (must stay green before any commit)
python -m pytest tests/ -q

# Run a single file
python -m pytest tests/test_etl_improvements.py -v

# Health report (auto-updates after pytest)
python3 scripts/test_health_report.py --print
```

`pytest.ini` has `--reruns 2 --reruns-delay 3` — SQLite lock transients get
retried automatically. If a test is flaky across many runs, investigate the
root cause; do not just add more retries.

---

## Strict scope rules — DO NOT violate without explicit user instruction

| Rule | Why |
|---|---|
| **Never open a second `AsyncSessionLocal()` inside `run_section_generation()`** | Causes 30 s SQLite lock (fixed May 2026 — use `db.begin_nested()`) |
| **Never add features outside the current task** | Sessions have gone off-scope before (test health dashboard added unprompted) |
| **Never modify `pytest.ini` `addopts`** | Changing reruns or markers breaks CI timing assumptions |
| **Never commit `.env` or credentials** | Pre-commit hooks will catch it, but do not try |
| **Never `git push --force` to main** | Protected branch |
| **`weasyprint` stays commented out in `requirements.txt`** | Render's build image lacks system libs; export endpoint returns 503 gracefully |
| **Do not add `asyncio.sleep` to tests** | Use `--reruns-delay` if you need a pause |

---

## Common gotchas

- **§7 ETL schema** — Gemini nests financials as `income_statement.{FY_2024}.revenue`,
  NOT flat. Use `_extract_section7_facts()` in `etl.py`; do not add flat entries to
  `_ETL_FACT_MAP`.
- **Audit trail order** — `GET /audit` returns events **DESC** (newest first).
  Any test asserting timestamp order must use `sorted(ts, reverse=True)`.
- **Block AST savepoint** — `save_blocks()` is called inside `db.begin_nested()`.
  If it fails, only blocks roll back; the section markdown is still committed.
- **`/recalculate` URL** — route is `/api/credit-report/reports/{id}/recalculate`, NOT `.../calculations/recalculate`. The calculations router prefix already includes `reports/{report_id}`.
- **PDF export 503** — Expected behavior when `weasyprint` is absent.
  The frontend falls back to browser print dialog. Do not change the 503 to 500.
- **SQLite WAL** — `busy_timeout=30000`. Two concurrent writes from the same
  process will wait up to 30 s before failing. One connection per async task.

---

## Before creating a pull request — mandatory checklist

```
□ Run /review (Builder hands off to Reviewer)
□ python -m pytest tests/ -q  →  all green, no new failures
□ python3 scripts/test_health_report.py --print  →  ✅ all healthy
□ git diff --stat  →  only files relevant to the task changed
□ Commit message explains WHY, not what
```

> **Rule**: Do not run `gh pr create` until `/review` has been invoked
> and any issues it raises have been resolved or explicitly waived by the user.

---

## Role boundaries (Three Man Team mapping)

| Role | Responsibility in this repo |
|---|---|
| **Architect** | Writes the plan (Plan Mode), defines acceptance criteria, approves PRs |
| **Builder** | Implements exactly what the plan says — no extras, no refactors outside scope |
| **Reviewer** | Runs `/review` + `/security-review`, checks test health, approves or returns |

If you are acting as Builder and you notice something outside the task that
should be improved, **write it in a comment / todo, do not implement it**.

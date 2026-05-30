
## Page-first annual report ETL real E2E tests

These tests verify the real annual-report page-first ETL flow with pytest API checks and Playwright browser automation. They require a running non-production app, a real test user, and a real TSMC annual-report PDF. Do **not** use fake PDFs, empty files, production customer data, or production databases.

1. Copy `.env.e2e.example` to `.env.e2e` and fill in:
   - `E2E_BASE_URL`
   - `E2E_EMAIL`
   - `E2E_PASSWORD`
   - `TSMC_ANNUAL_REPORT_PATH` pointing to the real TSMC annual-report PDF
2. Install dependencies:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m playwright install chromium
```

3. Run API E2E:

```bash
pytest tests/e2e/test_annual_report_page_etl_api.py -q -s --maxfail=1
```

4. Run Playwright UI E2E with real Chrome when available:

```bash
pytest tests/e2e/test_annual_report_page_etl_ui.py --browser chromium --browser-channel chrome --headed --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure -q -s --maxfail=1
```

If local Chrome is unavailable, retry without `--browser-channel chrome` to use bundled Chromium and mark that browser fallback in the test report. The convenience scripts `scripts/run_page_first_etl_e2e.sh` and `scripts/run_page_first_etl_e2e.ps1` perform this fallback automatically.

Real E2E tests skip (or fail when `--e2e-fail-missing-pdf` is supplied) if `TSMC_ANNUAL_REPORT_PATH` is missing or invalid. A skipped missing-PDF run is not evidence of a complete fix.

### GitHub Actions non-production real E2E job

The workflow `.github/workflows/page-first-etl-e2e.yml` is a manual `workflow_dispatch` job for the real page-first annual-report ETL E2E run. Configure the GitHub Environment `non-production-e2e` with these secrets before running it:

- `E2E_EMAIL` / `E2E_PASSWORD`: non-production admin or analyst account. The local CI app uses the same values to seed the isolated E2E admin user.
- `GEMINI_API_KEY`: non-production key used by real OCR/ETL; do not mock Gemini for this workflow.
- `TSMC_ANNUAL_REPORT_BASE64`: base64-encoded real TSMC annual-report PDF artifact. The workflow decodes this into `test-results/e2e/tsmc_annual_report.pdf` and rejects missing, tiny, or non-PDF files.
- Optional: `SECRET_KEY`, `GEMINI_MODEL`, `GEMINI_OCR_MODEL`, `GEMINI_ETL_MODEL`.

The job starts the app against an isolated SQLite E2E database, writes `.env.e2e`, runs `scripts/run_page_first_etl_e2e.sh` under `xvfb-run`, and uploads `test-results/e2e/**` artifacts including server logs, screenshots, traces, API responses, and `E2E_PAGE_FIRST_ETL_REPORT.md`.

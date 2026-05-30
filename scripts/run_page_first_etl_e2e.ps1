$ErrorActionPreference = "Stop"
if (-not (Test-Path ".env.e2e")) {
  Write-Error ".env.e2e not found. Copy .env.e2e.example and fill values."
}
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest tests/e2e/test_annual_report_page_etl_api.py -q -s --maxfail=1
$chromeArgs = @("tests/e2e/test_annual_report_page_etl_ui.py", "--browser", "chromium", "--browser-channel", "chrome", "--headed", "--tracing", "retain-on-failure", "--screenshot", "only-on-failure", "--video", "retain-on-failure", "-q", "-s", "--maxfail=1")
try {
  pytest @chromeArgs
} catch {
  Write-Warning "Chrome channel failed or is unavailable; retrying with bundled Chromium."
  pytest tests/e2e/test_annual_report_page_etl_ui.py --browser chromium --headed --tracing retain-on-failure --screenshot only-on-failure --video retain-on-failure -q -s --maxfail=1
}

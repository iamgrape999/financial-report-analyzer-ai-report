#!/usr/bin/env bash
set -euo pipefail
if [[ ! -f ".env.e2e" ]]; then
  echo ".env.e2e not found. Copy .env.e2e.example and fill values." >&2
  exit 1
fi
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest tests/e2e/test_annual_report_page_etl_api.py -q -s --maxfail=1
if ! pytest tests/e2e/test_annual_report_page_etl_ui.py \
  --browser chromium \
  --browser-channel chrome \
  --headed \
  --tracing retain-on-failure \
  --screenshot only-on-failure \
  --video retain-on-failure \
  -q -s --maxfail=1; then
  echo "Chrome channel failed or is unavailable; retrying with bundled Chromium." >&2
  pytest tests/e2e/test_annual_report_page_etl_ui.py \
    --browser chromium \
    --headed \
    --tracing retain-on-failure \
    --screenshot only-on-failure \
    --video retain-on-failure \
    -q -s --maxfail=1
fi

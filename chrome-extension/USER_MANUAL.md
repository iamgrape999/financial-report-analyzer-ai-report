# CUB Credit Report Automator — User Manual

Chrome extension that drives the Financial Report Analyzer end-to-end:
upload documents, run ETL, fill section forms, resolve conflicts, and
generate the full 10-section Markdown report. Combined with Claude Code
("Codex") for any custom edits, you reach roughly 98% hands-off operation.

---

## 1. What this extension does

The extension is a thin MV3 client that calls the same REST endpoints as
the web UI, but in a fixed order, with no user clicks between steps.

Concretely, one click on **Full Automation** does:

1. Authenticates against your backend (JWT login)
2. Lists every document attached to the report and runs ETL on any that
   still need it (Gemini OCR + structured fact extraction)
3. For every section 1–10, fetches field suggestions derived from the
   extracted CanonicalFacts and applies the high-confidence ones to the
   section input (mode = `only_empty`, so it never overwrites your edits)
4. Auto-resolves cross-source conflicts by source priority
   (`analyst_input_json` > `manual_override` > `pdf_extraction` >
   `calculation`)
5. Optional gap-fill: for any remaining empty fields, calls Gemini
   directly from the browser with a strict-JSON prompt and merges results
6. Triggers section generation in the required dependency order
   (§4 → §7 → §1 → §3 → §2 → §5 → §6 → §8 → §9 → §10) and polls each
   task to completion

What still needs a human:

- Same-source conflicts where the AI returns `risk_level: high` or
  `suggested_winner: uncertain` — surfaced in the **Conflicts** tab
- Final review and approval (the report stays in `draft` status)

---

## 2. Prerequisites

- Chrome 109 or newer (MV3 support)
- An account on your Financial Report Analyzer instance with `analyst`
  or `admin` role
- Optional: a Gemini API key (only if you want web-knowledge gap-fill;
  ETL and generation use the server's key, not yours)

---

## 3. Install in developer mode

The extension is not in the Chrome Web Store. Load it locally:

1. Open `chrome://extensions`
2. Toggle **Developer mode** on (top-right)
3. Click **Load unpacked**
4. Select the `chrome-extension/` directory from this repo
5. Confirm the extension card shows:
   - Name: "CUB Credit Report Automator"
   - Version: 1.0.0
   - No errors in the red error banner
6. Pin the toolbar icon for quick access (puzzle-piece menu → pin)

If you see "Service worker registration failed" or any red error, see
§9 Troubleshooting before continuing.

---

## 4. First-time configuration

Click the toolbar icon → **Settings** tab → fill in:

| Field | What to enter | Notes |
|---|---|---|
| Base URL | `https://your-host.onrender.com` or `http://localhost:8000` | No trailing slash; the extension trims it for you |
| Email | Your analyst account email | Must already exist in the backend |
| Password | Your password | Stored only in `chrome.storage.local` |
| Gemini API Key | `AIza…` (optional) | Only used for the gap-fill step |

Click **Save Settings**. The button briefly shows "Saved!". Settings
persist across browser restarts.

**Security**: nothing is sent anywhere except (a) the Base URL you
configured and (b) `generativelanguage.googleapis.com` for gap-fill.
Inspect the network panel of the popup to verify.

---

## 5. Daily use

### 5.1 Open a report and capture its ID

1. In a normal browser tab, open the report you want to automate in the
   web UI
2. Open the extension popup
3. Click **Detect** — the extension reads the SPA URL hash and fills
   `#reportId` automatically. If detection fails (older hash format,
   non-SPA route), copy the UUID from the URL and paste it manually

### 5.2 Run full automation

1. Confirm `#reportId` is populated
2. Optional: fill **Company Name** if you want the gap-fill step to look
   up web knowledge (e.g. `Evergreen Marine Corp`)
3. Click **Full Automation**
4. Watch the **Steps** panel in real time:
   - `○` idle, `⏳` running, `✅` done, `❌` error, `⏭️` skipped
   - Each step's detail line shows current progress
     (e.g. `OCR Q3_filing.pdf (2/4)…`, `§4 (1/10)…`)

Typical timings on a freshly created report with 3 PDFs:

| Step | Duration |
|---|---|
| Login | 0.3 s |
| ETL (3 docs) | 30–90 s |
| Field suggestions | 5 s |
| Auto-resolve conflicts | 2 s |
| Gemini gap-fill (10 sections) | 30–60 s |
| Generate §1–10 | 4–8 min |

Total: 5–10 minutes for a complete report.

### 5.3 Run individual steps

If you only want one phase (re-run ETL after uploading a new doc, or
re-generate a single section), use the three small buttons under
**Full Automation**:

- **ETL** — runs steps 1–2 only
- **Suggest** — runs steps 1, 3 only (suggestions + apply)
- **Generate** — runs steps 1, 6 only (all 10 sections in order)

Each individual button still calls Login first, so you don't need a
preserved session.

### 5.4 Conflicts panel

Click the **Conflicts** tab to triage facts that disagree across
sources.

- **Load Conflicts** — fetches all open conflicts for the current
  Report ID and renders one card per conflict, with both values side
  by side
- **Auto-Resolve Priority** — equivalent to step 4 of full automation:
  resolves all cross-source conflicts deterministically by source
  priority, then re-loads the list
- **AI Suggest** (per conflict) — calls the backend's
  `/conflicts/{id}/ai-suggest` endpoint. Two paths:
  - Cross-source conflict (different `source_type`): the backend
    answers deterministically (no Gemini call); the suggestion appears
    instantly with `risk_level: low` and `auto_resolvable: true`
  - Same-source conflict: the backend calls Gemini; the suggestion
    shows `risk_level` and `confidence` so you can judge whether to
    accept
- **Accept AI Suggestion** — only appears when `suggested_winner` is
  not `uncertain`. Clicking it POSTs `/conflicts/{id}/resolve` with
  the chosen fact and removes the card

The AI never auto-resolves anything for you — every accept is an
explicit click.

---

## 6. How the data flows under the hood

```
Browser ──► Extension service worker ──► Your backend ──► Gemini API
              │                              │              (server key)
              │                              │
              ▼ (gap-fill only)              ▼
           Gemini API                    PostgreSQL
           (your key)                    (CanonicalFacts,
                                          SectionInputs,
                                          FactConflicts,
                                          generated blocks)
```

The extension never touches the database or generation code directly —
it only calls public HTTP endpoints. That means anything the extension
can do, you can do with `curl` too; the extension just sequences the
calls correctly and waits for each task.

API contract used by the extension:

| Step | Method | Path |
|---|---|---|
| Login | POST | `/api/credit-report/auth/login` (form-encoded) |
| List docs | GET | `/api/credit-report/reports/{rid}/documents` |
| Run ETL | POST | `/api/credit-report/reports/{rid}/documents/{doc}/etl` |
| Get suggestions | GET | `/api/credit-report/reports/{rid}/sections/{n}/field-suggestions` |
| Apply suggestions | POST | `/api/credit-report/reports/{rid}/sections/{n}/field-suggestions/apply` |
| Auto-resolve | POST | `/api/credit-report/reports/{rid}/facts/conflicts/auto-resolve-priority` |
| List conflicts | GET | `/api/credit-report/reports/{rid}/facts/conflicts` |
| AI-suggest one | POST | `/api/credit-report/reports/{rid}/facts/conflicts/{cid}/ai-suggest` |
| Resolve one | POST | `/api/credit-report/reports/{rid}/facts/conflicts/{cid}/resolve` |
| Get/save inputs | GET/PUT | `/api/credit-report/reports/{rid}/inputs/{n}` |
| Generate section | POST | `/api/credit-report/reports/{rid}/generate/{n}?gen_language=zh` |
| Poll status | GET | `/api/credit-report/reports/{rid}/generate/status/{task}` |

---

## 7. Using the extension together with Claude Code (Codex)

The extension automates the runtime flow. Claude Code handles the
build-time flow when you need to change something the extension doesn't
cover. Typical pairings:

| You want to… | Use |
|---|---|
| Process today's batch of reports | Extension only |
| Add a new section to the report template | Claude Code (`/plan` → `/implement`) |
| Change which fields the extension auto-fills | Claude Code edits to `service-worker.js:stepApplySuggestions()` |
| Add a new conflict resolver rule | Claude Code edits to `credit_report/api/conflicts.py` |
| Generate one report end-to-end | Extension |
| Audit a stuck task | Claude Code reads server logs |

The AI code-review hook (`scripts/codex_review.py`) auto-runs on every
edit Claude Code makes to a `.py` file. It uses a separate
`GEMINI_REVIEWER_API_KEY` so it doesn't share the user's analyst quota.
You can disable it by unsetting the env var.

---

## 8. Verify the extension works after install

Quick smoke test, in order, on a throwaway report:

1. Settings tab → fill in Base URL + Email + Password → **Save**
2. Automate tab → paste a real Report ID
3. Click **ETL** only — should turn the ETL step green within 30 s per
   document
4. Conflicts tab → **Load Conflicts** — should render at least the
   empty state ("No open conflicts.") without errors
5. Automate tab → **Generate** only on a report with section inputs
   already filled — should turn green within ~5 minutes

If all four pass, the extension is wired correctly.

Programmatic verification (for CI/regression):

```bash
# Backend API contract — 14 tests
python -m pytest tests/test_extension_api_flow.py -q

# Browser-level — 5 tests, ~3 s
npx playwright test tests/e2e/extension.spec.js
```

Both suites must be green before deploying to other analysts.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Red banner on extension card: "Service worker registration failed" | `service-worker.js` has a syntax error or is missing | Re-pull the repo; run `node -c chrome-extension/service-worker.js`; reload the extension |
| Popup opens but nothing happens on click | Stale settings — wrong Base URL | Settings tab → fix Base URL → Save |
| `HTTP 401: Could not validate credentials` | Token expired or wrong password | Re-enter password in Settings → click any automation button (login re-runs) |
| `HTTP 422: Document appears to have no extractable text` during ETL | Uploaded file is image-only or corrupt | Open the doc in the web UI and re-upload a higher-quality version |
| ETL says "no facts extracted" | The fact mapping config doesn't recognize this doc type | Check `credit_report/fact_store/fact_mapping_config/marine/section_*.yaml`; add the metric there |
| Gap-fill step is skipped | No Gemini key in Settings | This is intentional — gap-fill only runs when you provide a key |
| `Generation timed out (5 min)` | Server is overloaded or generation hit a Gemini rate limit | Check server logs; re-run **Generate** only (it skips already-done sections) |
| `Detect` button does nothing | Content script can't see the SPA URL | Paste the Report ID manually |
| Conflicts list empty but you expect entries | They've all been resolved, or fact extraction didn't run | Re-run **ETL** to repopulate facts |
| "Saved!" never appears after clicking Save Settings | DOM was opened from `chrome://extensions` Inspect | Open the popup normally (toolbar icon), not via inspect-view |

To get a detailed error log: right-click the extension icon →
**Inspect popup** → Console tab; OR `chrome://extensions` →
**Inspect views: service worker** → Console.

---

## 10. Known limitations

- **MV3 service worker lifetime**: long-running generation (>30 min) can
  outlast the service worker. The 4-second polling cadence keeps it
  alive in practice, but the hard timeout is 5 minutes per section. If
  your reports routinely exceed this, raise `timeoutMs` in
  `pollGeneration()`.
- **One report at a time**: triggering Full Automation twice rapidly is
  blocked client-side (`disableActions(true)`), but two browser windows
  running against the same report will race. Don't do that.
- **Same-source conflict resolution is advisory only**: by design, the
  extension never picks between two PDF-extracted values automatically.
- **Gap-fill uses model knowledge, not live search**: Gemini does not
  fetch current web pages — it answers from its training data. For
  recent fiscal years, validate the numbers manually before approving.
- **Marine industry only**: the YAML mapping configs in
  `fact_mapping_config/marine/` are the only ones the field-suggestion
  endpoint understands. Other industries require new YAMLs.

---

## 11. Security and privacy

- Credentials live in `chrome.storage.local`, which is unencrypted on
  disk but scoped to your Chrome profile. Don't install on shared
  machines.
- The Gemini key, if provided, is sent **only** to
  `generativelanguage.googleapis.com` in the `x-goog-api-key` header
  (never in URL params). Inspect `service-worker.js:160` to confirm.
- The extension declares minimal permissions: `storage`, `tabs`,
  `scripting`, plus host permissions for your backend and Gemini only.
  No `<all_urls>` access.
- The backend rate-limits ETL (5 requests / 30 minutes per user) and
  uploads (10 / hour). The extension respects these — it does not
  retry or bypass them.

---

## 12. Uninstalling

`chrome://extensions` → CUB Credit Report Automator → **Remove**.

Settings stored in `chrome.storage.local` are deleted automatically.
Reports and facts in the backend are untouched — the extension is purely
a client.

# CUB Credit Report Automator — User Manual

Chrome extension that drives the Financial Report Analyzer end-to-end:
upload documents, run ETL, fill section forms, resolve conflicts, and
generate the full 10-section Markdown report in roughly 5–10 minutes
instead of several hours of manual work.

Used alongside **Claude Code** (Anthropic's CLI — *not* the same product
as OpenAI's discontinued Codex model) for any template or backend
changes, the combination reaches roughly 98% hands-off operation.

---

## 1. What this extension does

One click on **Full Automation** runs six sequential steps:

| Step | What happens | Duration |
|---|---|---|
| Login | Reuses cached JWT; only calls the password if the session expired | <1 s |
| ETL | Lists documents; Gemini OCR + fact extraction on any not yet processed | 30–90 s/doc |
| Suggestions | For sections 1–10: fetches high-confidence field matches from CanonicalFacts; applies them to empty fields only | 5–10 s |
| Auto-resolve | Resolves cross-source fact conflicts by source priority | 2 s |
| Gap-fill (optional) | Calls Gemini directly for fields still empty after ETL — see §6 for the hallucination risk | 30–60 s |
| Generate | Triggers all 10 sections in dependency order; polls each to completion via `chrome.alarms` | 4–10 min |

**What still requires a human analyst:**
- Same-source conflicts flagged `risk_level: high` or `uncertain`
  (surfaced in the Conflicts tab)
- Verification of any gap-fill placeholder values (see §6)
- Final report review and approval

---

## 2. Prerequisites

- Chrome 109 or newer (required for Manifest V3 + `chrome.alarms` API)
- An account with `analyst` or `admin` role on your backend instance
- Optional: a Gemini API key from Google AI Studio (only for gap-fill;
  ETL and generation use the server's own key)

---

## 3. Install in developer mode

The extension is not in the Chrome Web Store — load it locally:

1. Open `chrome://extensions`
2. Toggle **Developer mode** on (top-right corner)
3. Click **Load unpacked**
4. Select the `chrome-extension/` directory from this repo
5. Confirm the card shows "CUB Credit Report Automator v1.0.0" with no
   red error banner
6. Pin the toolbar icon (puzzle-piece menu → pin)

If the card shows a red "Service worker registration failed" error, run:

```bash
node -c chrome-extension/service-worker.js
```

Fix any syntax error reported, then click **Reload** on the extension
card.

---

## 4. Configuration

Click the toolbar icon → **Settings** tab.

| Field | What to enter | Persistence |
|---|---|---|
| Base URL | `https://your-host.onrender.com` or `http://localhost:8000` | Saved to disk |
| Email | Analyst account email | Saved to disk |
| Password | Account password | Saved to disk (see security note below) |

Click **Save Settings**.

**Note**: a Gemini API key is no longer stored in the extension. All
LLM calls (ETL, generation, and gap-fill) are routed through the
backend server using the server's own key. This keeps all financial
data inside the enterprise network perimeter and satisfies FSC (Taiwan)
and MAS (Singapore) data-residency requirements.

### Security note on password storage

`chrome.storage.local` is **unencrypted on disk** and scoped to your
Chrome profile. This is an acceptable trade-off for a personal developer
machine, but is inappropriate on shared workstations.

**Recommended practice:**
1. Enter your password and click **Save Settings** for the first
   automation run — this obtains and caches a JWT token.
2. After the first successful login, **clear the Password field and
   save again** — the cached JWT is reused for subsequent sessions.
3. If the token expires (typically 30–60 minutes, depending on server
   config), you will see the login step turn red with "Session expired".
   Re-enter the password, run any action, then clear the field again.
4. **Never install on a shared machine.** Any user with filesystem
   access to your Chrome profile can read `chrome.storage.local`.

The extension uses a **token-first login strategy**: `stepLogin()` first
checks whether a cached JWT is still valid (one lightweight API call);
it only uses the stored password to request a fresh token when the
cached one returns HTTP 401. This means the password is used at most
once per session.

---

## 5. Daily use

### 5.1 Detect the report ID

1. Open the target report in the web UI in a normal browser tab
2. Open the popup → click **Detect** — the content script reads the
   UUID from the URL hash and fills the `#reportId` field
3. If detection fails, paste the UUID from the URL manually

### 5.2 Run full automation

1. Confirm `#reportId` is filled
2. Optional: enter **Company Name** (used only by gap-fill; leave blank
   to skip that step entirely)
3. Click **Full Automation**
4. Watch the progress steps update in real time:
   - `○` idle, `⏳` running, `✅` done, `❌` error, `⏭️` skipped
5. The buttons re-enable once ETL → suggestions → conflicts → gap-fill
   complete and section generation has **started** for §4. You can
   close the popup — generation continues in the background via
   `chrome.alarms` and the "Generate" + "Complete" steps update when
   the popup is reopened.

### 5.3 Individual step buttons

| Button | Equivalent to | When to use |
|---|---|---|
| ETL | Login + ETL only | After uploading a new document |
| Suggest | Login + suggestions | After manually editing facts |
| Generate | Login + generate all 10 sections | Re-generate after editing section inputs |

### 5.4 Conflicts panel

| Control | Action |
|---|---|
| Load Conflicts | Fetches all open conflicts for the current report |
| Auto-Resolve Priority | Resolves cross-source conflicts by priority (`analyst_input > manual_override > pdf_extraction > calculation`); reloads the list |
| AI Suggest (per card) | Calls `/conflicts/{id}/ai-suggest`; for cross-source conflicts the answer is deterministic; for same-source it calls Gemini |
| Accept AI Suggestion | Appears only when `suggested_winner ≠ uncertain`; POSTs `/conflicts/{id}/resolve`; removes the card |

The extension **never** auto-accepts a suggestion. Every resolution
requires an explicit analyst click.

---

## 6. Gap-fill: risks and required verification

Gap-fill calls `POST /api/credit-report/reports/{rid}/gap-fill` on the
backend, which forwards the request to Gemini using the server's API key.
No financial data leaves the enterprise network perimeter — the browser
never contacts `generativelanguage.googleapis.com` directly.

Gemini answers from its **training-data knowledge** (cutoff August 2025
for `gemini-2.5-flash`). It does **not** fetch live market data,
Bloomberg feeds, or real-time financial filings.

**Consequence**: any values Gemini fills in are approximations or, for
recent fiscal years, fabrications ("hallucinations"). They are suitable
only as scaffolding — a starting point for the analyst to replace with
verified figures.

To make the risk visible in the UI:
- Gap-fill messages include "VERIFY BEFORE SUBMIT"
- String values uncertain to Gemini are returned with an `_UNVERIFIED`
  suffix so the analyst can search for them in the section inputs

**Debt and leverage fields — critical warning**: YAML field-mapping
configs map facts to **interest-bearing debt** components (bank loans,
bonds, finance leases), not to `total_liabilities`. Any gap-fill value
for a leverage or coverage ratio must be cross-checked against the
borrower's audited financials to confirm the correct debt perimeter.
Confusing total liabilities with interest-bearing debt corrupts DSCR,
net debt/EBITDA, and LTV calculations.

**When to use gap-fill**: for older, well-documented companies where
Gemini's knowledge is likely accurate and the values are low-stakes
(e.g. company founding date, registered country, fleet count).
**Do not** rely on gap-fill for revenue, EBITDA, debt ratios, or any
figure dated after the model's knowledge cutoff.

**When to skip gap-fill**: leave the Company Name field blank. The
step is skipped entirely when the field is empty.

---

## 7. Generation architecture: why it survives restarts

Chrome MV3 service workers terminate after 30 seconds of inactivity.
A 10-section report takes 4–10 minutes to generate. The extension
solves this using `chrome.alarms`:

1. `stepGenerate()` triggers §4 synchronously, writes **all** generation
   state as a single atomic object (`_gen_state`) to `chrome.storage.local`,
   then creates a `_gen_poll` alarm set to fire every ~8 seconds.
2. The alarm fires even if Chrome terminates the service worker —
   Chrome wakes a new SW instance, which reads the persisted state
   and continues polling.
3. When a section completes, the alarm handler immediately triggers the
   next section in the queue ([4→7→1→3→2→5→6→8→9→10]) and writes
   the updated state atomically (single `chrome.storage.local.set` call).
4. When all 10 sections complete, the alarm is cleared and the final
   `emit("done", ...)` fires.

**Why atomic state matters**: writing all fields as one object prevents
the popup from reading a partially-updated state (e.g. new task_id but
old section number) if it opens between two individual key writes.

You can close the popup during generation. Progress events arrive when
you reopen it. If generation stalls (check `chrome://extensions` →
service worker console), click **Generate** again — the alarm state is
cleared and generation restarts cleanly.

---

## 8. Using Claude Code for backend changes

Claude Code is **Anthropic's** agentic CLI tool (not OpenAI's Codex,
which was a separate model discontinued in 2023). The internal file
`scripts/codex_review.py` carries a legacy name — it uses Gemini as its
backend, not any OpenAI product.

| You want to… | Use |
|---|---|
| Process today's report batch | Extension only |
| Add a new fact-mapping YAML for a new industry | Claude Code — edit `fact_store/fact_mapping_config/` |
| Change which sections the extension auto-generates | Claude Code — edit `service-worker.js:ORDER` |
| Tune the field-suggestion confidence threshold | Claude Code — edit `credit_report/api/reports.py` |
| Audit a stuck generation task | Claude Code — read server logs / query the DB |

The AI code-review hook in `scripts/codex_review.py` runs automatically
on every file Claude Code edits. Configure it via `GEMINI_REVIEWER_API_KEY`.

---

## 9. Verify the extension works

Quick smoke test (five minutes):

1. Settings → fill Base URL + Email + Password → Save
2. Automate tab → paste a real Report ID → click **ETL** only
   → ETL step should turn green within 30–90 s per document
3. Conflicts tab → **Load Conflicts** → should render cards or "No open
   conflicts" without a red error
4. Automate tab → **Generate** on a report with section inputs filled
   → generation alarm starts; popup shows progress updates

Automated checks (run these before deploying to other analysts):

```bash
# Backend API contract — 14 tests
python -m pytest tests/test_extension_api_flow.py -q

# Browser smoke tests — 5 tests (~3 s, uses --headless=new)
npx playwright test tests/e2e/extension.spec.js
```

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Red banner: "Service worker registration failed" | JS syntax error | `node -c service-worker.js`; fix error; reload extension |
| Login step stays orange after clicking Full Auto | Stale token + no password stored | Re-enter password in Settings → Save → retry |
| `HTTP 401` on any step | JWT expired | Enter password in Settings; the token-first logic will refresh it |
| `HTTP 422: no extractable text` | Upload was image-only PDF | Re-upload a better-quality PDF; or use a text-layer PDF |
| Generation alarm never completes | Section generation failed server-side | Open service worker console (`chrome://extensions` → Inspect) for error detail; click **Generate** to restart |
| Gap-fill skipped | Company Name field is blank | Expected — fill the Company Name field to enable gap-fill |
| Gap-fill produced wrong values | Gemini hallucinated | Expected for recent fiscal data — verify against actual filings |
| Gap-fill returns 503 | Server GEMINI_API_KEY not set | Contact the backend admin to configure `GEMINI_API_KEY` on the server |
| "Detect" fills wrong report ID | SPA URL format not recognised | Paste the UUID manually from the browser URL bar |
| Popup shows idle steps when generation is running | Popup was closed during generation | Progress emits resume when popup reopens; the alarm continues in background |

---

## 11. Known limitations

| Limitation | Detail |
|---|---|
| Marine industry only | YAML field-mapping configs only cover marine (`fact_mapping_config/marine/`). Other industries need new YAMLs. |
| Gap-fill uses training data | Gemini has no live data access. All filled values are approximations and must be verified. |
| Token storage | JWT cached in `chrome.storage.local` (unencrypted). Clear the password field after first login on non-personal machines. No Gemini API key is stored client-side. |
| Same-source conflicts are advisory | The extension never auto-resolves conflicts between two values from the same source type. |
| Report approval | The extension leaves reports in `draft` status. Approval requires an analyst action in the web UI. |

---

## 12. Security and privacy

- **Credentials**: Stored in `chrome.storage.local` — see §4 for the
  recommended practice of clearing the password after first login.
- **Gemini key**: Not stored client-side. All LLM calls (ETL, generation,
  gap-fill) are proxied through the backend server using the server's
  own `GEMINI_API_KEY`. Financial data never leaves the enterprise network.
- **Permissions**: The extension declares `storage`, `tabs`, `scripting`,
  and `alarms` only, plus host permissions for your backend only.
  No `<all_urls>` access. `generativelanguage.googleapis.com` is no
  longer in `host_permissions`.
- **Backend rate limits**: ETL is limited to 5 requests per 30 minutes
  per user; uploads to 10 per hour. The extension respects these.
- **Audit trail**: every ETL run, fact upsert, conflict resolution, and
  section generation is logged in the backend's audit trail.

---

## 13. Uninstalling

`chrome://extensions` → CUB Credit Report Automator → **Remove**.

`chrome.storage.local` data (settings, cached JWT) is deleted
automatically. Backend reports, facts, and generated content are
unaffected — the extension is a pure client.

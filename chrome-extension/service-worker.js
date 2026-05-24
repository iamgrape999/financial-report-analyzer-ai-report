/**
 * CUB Credit Report Automator — Service Worker
 * All REST API calls run here (no CORS issues in background context).
 * Sends progress events back to popup via chrome.runtime.sendMessage.
 */

// ── Helpers ──────────────────────────────────────────────────────────────────

function emit(step, status, detail = "") {
  chrome.runtime.sendMessage({ type: "progress", step, status, detail }).catch(() => {});
}

async function cfg() {
  return chrome.storage.local.get(["baseUrl", "email", "password", "geminiKey", "token"]);
}

async function saveToken(token) {
  await chrome.storage.local.set({ token });
}

async function api(method, path, body, token, baseUrl) {
  const url = baseUrl + "/api/credit-report" + path;
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const opts = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status}: ${txt.slice(0, 200)}`);
  }
  return resp.json();
}

async function apiForm(path, formData, token, baseUrl) {
  const url = baseUrl + "/api/credit-report" + path;
  const headers = {};
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch(url, { method: "POST", headers, body: formData });
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status}: ${txt.slice(0, 200)}`);
  }
  return resp.json();
}

// ── Auth ─────────────────────────────────────────────────────────────────────

async function login(baseUrl, email, password) {
  const url = baseUrl + "/api/credit-report/auth/login";
  const body = new URLSearchParams({ username: email, password });
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!resp.ok) throw new Error("Login failed — check email/password.");
  const data = await resp.json();
  return data.access_token;
}

// ── ETL polling ──────────────────────────────────────────────────────────────

async function pollGeneration(reportId, taskId, token, baseUrl, timeoutMs = 300000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 4000));
    try {
      const s = await api("GET", `/reports/${reportId}/generate/status/${taskId}`, undefined, token, baseUrl);
      if (s.status === "done")  return s;
      if (s.status === "error") throw new Error(`Generation failed: ${s.detail || ""}`);
    } catch (e) {
      if (e.message.startsWith("Generation failed")) throw e;
    }
  }
  throw new Error("Generation timed out (5 min)");
}

// ── Step implementations ──────────────────────────────────────────────────────

async function stepLogin() {
  emit("login", "running");
  const { baseUrl, email, password } = await cfg();
  if (!baseUrl || !email || !password) throw new Error("Configure Base URL, Email and Password in Settings first.");
  const token = await login(baseUrl, email, password);
  await saveToken(token);
  emit("login", "done", email);
  return token;
}

async function stepEtlAll(reportId) {
  const { baseUrl, token } = await cfg();
  emit("etl", "running", "listing documents…");
  const docs = await api("GET", `/reports/${reportId}/documents`, undefined, token, baseUrl);
  const pending = docs.filter(d => d.etl_status !== "done");
  if (pending.length === 0) { emit("etl", "done", "already processed"); return; }
  let done = 0;
  for (const doc of pending) {
    emit("etl", "running", `OCR ${doc.original_filename} (${++done}/${pending.length})…`);
    await api("POST", `/reports/${reportId}/documents/${doc.id}/etl`, undefined, token, baseUrl);
  }
  emit("etl", "done", `${pending.length} document(s) processed`);
}

async function stepApplySuggestions(reportId) {
  const { baseUrl, token } = await cfg();
  emit("suggestions", "running");
  let total = 0;
  for (let sec = 1; sec <= 10; sec++) {
    let data;
    try {
      data = await api("GET", `/reports/${reportId}/sections/${sec}/field-suggestions`, undefined, token, baseUrl);
    } catch { continue; }

    const items = (data.suggestions || [])
      .filter(s => s.confidence === "high" && s.selectable && !s.conflict_warning)
      .map(s => ({ suggestion_id: s.suggestion_id, field_path: s.field_path, fact_id: s.fact_id, suggested_value: s.suggested_value }));

    if (!items.length) continue;
    const res = await api("POST", `/reports/${reportId}/sections/${sec}/field-suggestions/apply`,
      { apply_mode: "only_empty", items }, token, baseUrl);
    total += res.applied_count || 0;
  }
  emit("suggestions", "done", `${total} fields auto-filled`);
}

async function stepAutoConflicts(reportId) {
  const { baseUrl, token } = await cfg();
  emit("conflicts", "running");
  const res = await api("POST", `/reports/${reportId}/facts/conflicts/auto-resolve-priority`, {}, token, baseUrl);
  const msg = `${res.resolved_count} auto-resolved, ${res.skipped_count} need review`;
  emit("conflicts", "done", msg);
  return res;
}

async function stepGeminiGapFill(reportId, companyName) {
  const { baseUrl, token, geminiKey } = await cfg();
  if (!geminiKey) { emit("gapfill", "skip", "no GEMINI_API_KEY in settings"); return; }
  emit("gapfill", "running", "searching web for missing fields…");

  let filled = 0;
  for (let sec = 1; sec <= 10; sec++) {
    let sugg;
    try {
      sugg = await api("GET", `/reports/${reportId}/sections/${sec}/field-suggestions`, undefined, token, baseUrl);
    } catch { continue; }

    const empty = (sugg.suggestions || []).filter(s => s.current_value == null).slice(0, 15);
    if (!empty.length) continue;

    const fieldList = empty.map(s => `${s.field_path} (${s.metric_name}, ${s.period || ""})`).join("\n");
    const prompt = `Company: ${companyName}\nSection ${sec} of a bank credit report is missing these financial fields:\n${fieldList}\n\nFind realistic values from your knowledge. Return ONLY valid JSON: {"field_path": numeric_or_string_value, ...}`;

    let gapData = {};
    try {
      const gResp = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-goog-api-key": geminiKey },
          body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }], generationConfig: { maxOutputTokens: 500, temperature: 0.1 } }),
        }
      );
      const raw = await gResp.json();
      const text = raw?.candidates?.[0]?.content?.parts?.[0]?.text || "";
      const m = text.match(/\{[\s\S]+\}/);
      if (m) gapData = JSON.parse(m[0]);
    } catch { continue; }

    if (!Object.keys(gapData).length) continue;

    // Load existing section input, merge, save
    let current = {};
    try {
      const existing = await api("GET", `/reports/${reportId}/inputs/${sec}`, undefined, token, baseUrl);
      current = existing.input_json || {};
    } catch {}

    for (const [path, val] of Object.entries(gapData)) {
      if (_deepGet(current, path) == null) _deepSet(current, path, val);
    }

    await api("PUT", `/reports/${reportId}/inputs/${sec}`, { section_no: sec, input_json: current }, token, baseUrl);
    filled += Object.keys(gapData).length;
    emit("gapfill", "running", `§${sec}: +${Object.keys(gapData).length} fields`);
  }
  emit("gapfill", "done", `${filled} fields filled from web knowledge`);
}

async function stepGenerate(reportId) {
  const { baseUrl, token } = await cfg();
  emit("generate", "running");
  const ORDER = [4, 7, 1, 3, 2, 5, 6, 8, 9, 10];
  let done = 0;
  for (const sec of ORDER) {
    emit("generate", "running", `§${sec} (${++done}/10)…`);
    let taskId;
    try {
      const r = await api("POST", `/reports/${reportId}/generate/${sec}?gen_language=zh`, {}, token, baseUrl);
      taskId = r.task_id;
    } catch (e) {
      emit("generate", "running", `§${sec} skipped: ${e.message}`);
      continue;
    }
    await pollGeneration(reportId, taskId, token, baseUrl);
  }
  emit("generate", "done", "all 10 sections generated");
}

// ── Deep path helpers ─────────────────────────────────────────────────────────

function _deepGet(obj, path) {
  return path.split(".").reduce((cur, k) => (cur == null ? null : cur[k]), obj);
}
function _deepSet(obj, path, val) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== "object") cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = val;
}

// ── Message router ────────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    const { action, reportId, companyName } = msg;

    try {
      if (action === "login") {
        await stepLogin();
        sendResponse({ ok: true });

      } else if (action === "full_auto") {
        await stepLogin();
        await stepEtlAll(reportId);
        await stepApplySuggestions(reportId);
        await stepAutoConflicts(reportId);
        if (companyName) await stepGeminiGapFill(reportId, companyName);
        await stepGenerate(reportId);
        emit("done", "done", "98% automation complete ✓");
        sendResponse({ ok: true });

      } else if (action === "etl")         { await stepLogin(); await stepEtlAll(reportId);             sendResponse({ ok: true });
      } else if (action === "suggestions") { await stepLogin(); await stepApplySuggestions(reportId);   sendResponse({ ok: true });
      } else if (action === "conflicts")   { await stepLogin(); await stepAutoConflicts(reportId);      sendResponse({ ok: true });
      } else if (action === "gapfill")     { await stepLogin(); await stepGeminiGapFill(reportId, companyName); sendResponse({ ok: true });
      } else if (action === "generate")    { await stepLogin(); await stepGenerate(reportId);           sendResponse({ ok: true });

      } else if (action === "ai_suggest_conflict") {
        const { baseUrl, token } = await cfg();
        const res = await api("POST", `/reports/${reportId}/facts/conflicts/${msg.conflictId}/ai-suggest`, {}, token, baseUrl);
        sendResponse({ ok: true, data: res });

      } else if (action === "list_conflicts") {
        const { baseUrl, token } = await cfg();
        const res = await api("GET", `/reports/${reportId}/facts/conflicts`, undefined, token, baseUrl);
        sendResponse({ ok: true, data: res });

      } else if (action === "resolve_conflict") {
        const { baseUrl, token } = await cfg();
        await api("POST", `/reports/${reportId}/facts/conflicts/${msg.conflictId}/resolve`,
          { chosen_fact_id: msg.chosenFactId, rejected_fact_ids: msg.rejectedFactIds, resolution_reason: msg.reason },
          token, baseUrl);
        sendResponse({ ok: true });

      } else {
        sendResponse({ ok: false, error: "Unknown action: " + action });
      }
    } catch (e) {
      emit(action, "error", e.message);
      sendResponse({ ok: false, error: e.message });
    }
  })();
  return true; // keep channel open for async response
});

/**
 * CUB Credit Report Automator — Service Worker
 * All REST API calls run here (no CORS issues in background context).
 * Sends progress events back to popup via chrome.runtime.sendMessage.
 *
 * Generation polling uses chrome.alarms so the service worker survives
 * Chrome's 30-second inactivity termination across multi-minute tasks.
 * State is persisted in chrome.storage.local; the SW can be revived
 * mid-generation if Chrome terminates it between alarm firings.
 */

// ── Helpers ──────────────────────────────────────────────────────────────────

function emit(step, status, detail = "") {
  chrome.runtime.sendMessage({ type: "progress", step, status, detail }).catch(() => {});
}

async function cfg() {
  return chrome.storage.local.get(["baseUrl", "email", "password", "token"]);
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
  if (!resp.ok) throw new Error("Login failed — check email/password in Settings.");
  const data = await resp.json();
  return data.access_token;
}

// ── Generation alarm (MV3-safe polling) ─────────────────────────────────────
// All generation state is stored as a single atomic object under "_gen_state"
// to prevent popup reads from observing a partially-written state mid-update.
// Shape: { task, report, token, base, sec, queue, done }

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== "_gen_poll") return;

  const { _gen_state: s } = await chrome.storage.local.get("_gen_state");
  if (!s || !s.report) { chrome.alarms.clear("_gen_poll"); return; }

  // No current task → trigger the next section in the queue
  if (!s.task) {
    const queue = s.queue || [];
    const done  = s.done  || 0;
    if (!queue.length) {
      await _genClear();
      emit("generate", "done", `${done} section(s) generated`);
      emit("done", "done", "98% automation complete ✓");
      return;
    }
    const sec = queue[0];
    emit("generate", "running", `§${sec} (${done + 1}/10)…`);
    try {
      const r = await api("POST", `/reports/${s.report}/generate/${sec}?gen_language=zh`, {}, s.token, s.base);
      await chrome.storage.local.set({ _gen_state: { ...s, task: r.task_id, sec, queue: queue.slice(1) } });
    } catch (e) {
      emit("generate", "running", `§${sec} skipped: ${e.message}`);
      await chrome.storage.local.set({ _gen_state: { ...s, task: null, sec, queue: queue.slice(1) } });
    }
    return;
  }

  // Poll the current task
  try {
    const status = await api("GET", `/reports/${s.report}/generate/status/${s.task}`, undefined, s.token, s.base);

    if (status.status === "done") {
      const done  = (s.done || 0) + 1;
      const queue = s.queue || [];
      emit("generate", "running", `§${s.sec} done (${done}/10)`);

      if (!queue.length) {
        await _genClear();
        emit("generate", "done", "all 10 sections generated");
        emit("done", "done", "98% automation complete ✓");
      } else {
        // Immediately trigger next section in the same alarm cycle
        const sec = queue[0];
        emit("generate", "running", `§${sec} (${done + 1}/10)…`);
        try {
          const r = await api("POST", `/reports/${s.report}/generate/${sec}?gen_language=zh`, {}, s.token, s.base);
          await chrome.storage.local.set({ _gen_state: { ...s, task: r.task_id, sec, queue: queue.slice(1), done } });
        } catch (e) {
          emit("generate", "running", `§${sec} skipped: ${e.message}`);
          await chrome.storage.local.set({ _gen_state: { ...s, task: null, sec, queue: queue.slice(1), done } });
        }
      }

    } else if (status.status === "error") {
      emit("generate", "running", `§${s.sec} error: ${status.detail || "unknown"} — continuing`);
      await chrome.storage.local.set({ _gen_state: { ...s, task: null } });
    }
    // status === "running" → wait for next alarm
  } catch (_e) {
    // Transient network error; next alarm will retry automatically
  }
});

async function _genClear() {
  await chrome.storage.local.remove("_gen_state");
  chrome.alarms.clear("_gen_poll");
}

// ── Step implementations ──────────────────────────────────────────────────────

async function stepLogin() {
  emit("login", "running");
  const { baseUrl, email, password, token: cached } = await cfg();
  if (!baseUrl || !email) throw new Error("Configure Base URL and Email in Settings.");

  // Token-first: reuse cached JWT to avoid relying on stored password every call
  if (cached) {
    try {
      await api("GET", "/reports?limit=1", undefined, cached, baseUrl);
      emit("login", "done", `${email} (session active)`);
      return cached;
    } catch (e) {
      if (!e.message.startsWith("HTTP 401")) throw e;
      // 401 = expired; fall through to fresh login
    }
  }

  if (!password) throw new Error("Session expired — re-enter Password in Settings.");
  const newToken = await login(baseUrl, email, password);
  await saveToken(newToken);
  emit("login", "done", email);
  return newToken;
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
  emit("conflicts", "done", `${res.resolved_count} auto-resolved, ${res.skipped_count} need review`);
  return res;
}

async function stepGeminiGapFill(reportId, companyName) {
  const { baseUrl, token } = await cfg();
  emit("gapfill", "running", "⚠ server-side estimate — verify all values before approving");
  try {
    const res = await api(
      "POST",
      `/reports/${reportId}/gap-fill`,
      { company_name: companyName },
      token,
      baseUrl,
    );
    emit("gapfill", "done",
      `${res.total_filled} placeholder(s) filled — all require manual verification`);
  } catch (e) {
    emit("gapfill", "error", e.message);
    throw e;
  }
}

async function stepGenerate(reportId) {
  const { baseUrl, token } = await cfg();
  emit("generate", "running", "§4 (1/10)…");
  const ORDER = [4, 7, 1, 3, 2, 5, 6, 8, 9, 10];

  // Trigger first section synchronously so we can surface immediate errors
  let firstTaskId;
  try {
    const r = await api("POST", `/reports/${reportId}/generate/${ORDER[0]}?gen_language=zh`, {}, token, baseUrl);
    firstTaskId = r.task_id;
  } catch (e) {
    emit("generate", "error", e.message);
    throw e;
  }

  // Persist generation state atomically — a single key ensures popup reads
  // never observe a partially-written state between alarm handler writes
  await chrome.storage.local.set({
    _gen_state: {
      task:  firstTaskId,
      report: reportId,
      token,
      base:  baseUrl,
      sec:   ORDER[0],
      queue: ORDER.slice(1),
      done:  0,
    },
  });

  // Poll every ~8 seconds via alarm (survives SW restarts unlike setTimeout)
  chrome.alarms.create("_gen_poll", { periodInMinutes: 0.13 });
  // Caller returns here; progress/done events arrive later from the alarm handler
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
        // Generation continues in background via _gen_poll alarm.
        // "done" emit arrives later from the alarm handler.
        sendResponse({ ok: true });

      } else if (action === "etl")         { await stepLogin(); await stepEtlAll(reportId);                        sendResponse({ ok: true });
      } else if (action === "suggestions") { await stepLogin(); await stepApplySuggestions(reportId);              sendResponse({ ok: true });
      } else if (action === "conflicts")   { await stepLogin(); await stepAutoConflicts(reportId);                 sendResponse({ ok: true });
      } else if (action === "gapfill")     { await stepLogin(); await stepGeminiGapFill(reportId, companyName);    sendResponse({ ok: true });
      } else if (action === "generate")    { await stepLogin(); await stepGenerate(reportId); sendResponse({ ok: true });

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
  return true; // keep message channel open for async response
});

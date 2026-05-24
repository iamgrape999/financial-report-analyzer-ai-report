/**
 * popup.js — UI logic for CUB Credit Report Automator
 */

// ── Tab switching ─────────────────────────────────────────────────────────────

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab, .panel").forEach(el => el.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("panel-" + tab.dataset.tab).classList.add("active");
  });
});

// ── Settings ──────────────────────────────────────────────────────────────────

async function loadSettings() {
  const data = await chrome.storage.local.get(["baseUrl", "email", "password"]);
  if (data.baseUrl)  document.getElementById("baseUrl").value  = data.baseUrl;
  if (data.email)    document.getElementById("email").value    = data.email;
  if (data.password) document.getElementById("password").value = data.password;
}

document.getElementById("saveSettingsBtn").addEventListener("click", async () => {
  await chrome.storage.local.set({
    baseUrl:  document.getElementById("baseUrl").value.trim().replace(/\/$/, ""),
    email:    document.getElementById("email").value.trim(),
    password: document.getElementById("password").value,
  });
  const btn = document.getElementById("saveSettingsBtn");
  btn.textContent = "✅ Saved!";
  setTimeout(() => { btn.textContent = "💾 Save Settings"; }, 1500);
});

// ── Auto-detect report ID from active tab ─────────────────────────────────────

document.getElementById("detectBtn").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  try {
    const resp = await chrome.tabs.sendMessage(tab.id, { type: "get_page_context" });
    if (resp?.reportId) {
      document.getElementById("reportId").value = resp.reportId;
    } else {
      alert("Could not detect report ID from the current page.\nPlease paste it manually.");
    }
  } catch {
    alert("Content script not loaded on this page.\nMake sure you are on the credit report web app.");
  }
});

// ── Progress step renderer ────────────────────────────────────────────────────

function setStep(step, status, detail) {
  const el = document.querySelector(`.step[data-step="${step}"]`);
  if (!el) return;
  el.className = "step " + status;
  const d = document.getElementById("d-" + step);
  if (d && detail !== undefined) d.textContent = detail;
}

chrome.runtime.onMessage.addListener(msg => {
  if (msg.type === "progress") {
    setStep(msg.step, msg.status, msg.detail);
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────

function getReportId() {
  const rid = document.getElementById("reportId").value.trim();
  if (!rid) { alert("Please enter or detect a Report ID."); return null; }
  return rid;
}

function disableActions(disabled) {
  ["fullAutoBtn","etlBtn","suggestBtn","generateBtn","loadConflictsBtn","autoPriorityBtn"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

async function send(action, extra = {}) {
  disableActions(true);
  const rid = getReportId();
  if (!rid) { disableActions(false); return; }
  try {
    const resp = await chrome.runtime.sendMessage({ action, reportId: rid, ...extra });
    if (!resp.ok) throw new Error(resp.error);
    return resp;
  } catch (e) {
    alert("Error: " + e.message);
  } finally {
    disableActions(false);
  }
}

// ── Automate panel actions ────────────────────────────────────────────────────

document.getElementById("fullAutoBtn").addEventListener("click", async () => {
  const companyName = document.getElementById("companyName").value.trim();
  // Reset all steps
  document.querySelectorAll(".step").forEach(el => {
    el.className = "step idle";
  });
  await send("full_auto", { companyName });
});

document.getElementById("etlBtn").addEventListener("click",      () => send("etl"));
document.getElementById("suggestBtn").addEventListener("click",  () => send("suggestions"));
document.getElementById("generateBtn").addEventListener("click", () => send("generate"));

// ── Conflicts panel ───────────────────────────────────────────────────────────

document.getElementById("loadConflictsBtn").addEventListener("click", async () => {
  const rid = getReportId();
  if (!rid) return;
  const list = document.getElementById("conflictList");
  list.innerHTML = '<p class="info"><span class="spinner">⏳</span> Loading…</p>';

  const resp = await chrome.runtime.sendMessage({ action: "list_conflicts", reportId: rid });
  if (!resp.ok) { list.innerHTML = `<p style="color:#f87171">${resp.error}</p>`; return; }

  const conflicts = resp.data;
  if (!conflicts.length) { list.innerHTML = '<p class="info">✅ No open conflicts.</p>'; return; }

  list.innerHTML = conflicts.map(c => renderConflictCard(c)).join("");

  // Wire up AI Suggest buttons
  list.querySelectorAll(".ai-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "⏳";
      const cid = btn.dataset.conflictId;
      const resp2 = await chrome.runtime.sendMessage({ action: "ai_suggest_conflict", reportId: rid, conflictId: cid });
      if (!resp2.ok) { btn.textContent = "❌"; return; }
      const sug = resp2.data;
      const box = document.getElementById("ai-box-" + cid);
      box.style.display = "block";
      box.innerHTML = `
        <span class="badge-conf badge-${sug.risk_level}">${sug.confidence}% ${sug.risk_level}</span>
        ${sug.auto_resolvable ? " ⚡ Auto-resolvable" : ""}
        <br>${sug.reason}
        ${sug.suggested_winner !== "uncertain" ? `<br><br><button class="btn btn-primary btn-sm accept-btn"
          data-conflict-id="${cid}"
          data-chosen="${sug.suggested_fact_id}"
          data-rejected="${sug.suggested_winner === 'fact_a' ? document.querySelector('[data-fact-b="'+cid+'"]')?.value : document.querySelector('[data-fact-a="'+cid+'"]')?.value}"
          data-reason="${esc(sug.resolution_suggestion)}">✅ Accept AI Suggestion</button>` : ""}
      `;
      // Wire accept button
      box.querySelectorAll(".accept-btn").forEach(ab => {
        ab.addEventListener("click", async () => {
          const conflict = conflicts.find(x => x.id === cid);
          const rejected = sug.suggested_winner === "fact_a" ? [conflict.fact_b_id] : [conflict.fact_a_id];
          const r = await chrome.runtime.sendMessage({
            action: "resolve_conflict", reportId: rid,
            conflictId: cid,
            chosenFactId: sug.suggested_fact_id,
            rejectedFactIds: rejected,
            reason: sug.resolution_suggestion,
          });
          if (r.ok) {
            document.getElementById("card-" + cid)?.remove();
          }
        });
      });
      btn.style.display = "none";
    });
  });
});

document.getElementById("autoPriorityBtn").addEventListener("click", async () => {
  const rid = getReportId();
  if (!rid) return;
  const resp = await chrome.runtime.sendMessage({ action: "conflicts", reportId: rid });
  if (resp?.ok) document.getElementById("loadConflictsBtn").click();
});

function esc(s) { return (s || "").replace(/"/g, "&quot;"); }

function renderConflictCard(c) {
  return `
  <div class="conflict-card" id="card-${c.id}">
    <div class="conflict-metric">⚠️ ${c.metric_name} · ${c.entity} · ${c.period}</div>
    <div class="conflict-row">
      <span>Source A: <code>${c.source_a || "?"}</code></span>
      <span class="conflict-val val-a">${c.value_a || "–"}</span>
    </div>
    <div class="conflict-row">
      <span>Source B: <code>${c.source_b || "?"}</code></span>
      <span class="conflict-val val-b">${c.value_b || "–"}</span>
    </div>
    <div class="ai-box" id="ai-box-${c.id}" style="display:none"></div>
    <input type="hidden" data-fact-a="${c.id}" value="${c.fact_a_id}">
    <input type="hidden" data-fact-b="${c.id}" value="${c.fact_b_id}">
    <div class="conflict-actions">
      <button class="btn btn-outline btn-sm ai-btn" data-conflict-id="${c.id}">🤖 AI Suggest</button>
    </div>
  </div>`;
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadSettings();

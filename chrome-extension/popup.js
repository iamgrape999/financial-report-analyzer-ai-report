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
  let rawUrl = document.getElementById("baseUrl").value.trim().replace(/\/$/, "");
  // Strip any path (e.g. /app) — only keep scheme + host + port
  try { rawUrl = new URL(rawUrl).origin; } catch (_) {}
  await chrome.storage.local.set({
    baseUrl:  rawUrl,
    email:    document.getElementById("email").value.trim(),
    password: document.getElementById("password").value,
  });
  document.getElementById("baseUrl").value = rawUrl; // show the cleaned value
  const btn = document.getElementById("saveSettingsBtn");
  btn.textContent = "✅ Saved!";
  setTimeout(() => { btn.textContent = "💾 Save Settings"; }, 1500);
});

// ── Report list dropdown ──────────────────────────────────────────────────────

async function loadReportList() {
  const btn = document.getElementById("loadReportsBtn");
  const sel = document.getElementById("reportSelect");
  btn.disabled = true;
  btn.textContent = "⏳";
  sel.innerHTML = '<option value="">Loading…</option>';

  try {
    const resp = await chrome.runtime.sendMessage({ action: "list_reports" });
    if (!resp.ok) throw new Error(resp.error);
    const reports = resp.data;
    if (!reports.length) {
      sel.innerHTML = '<option value="">— no reports found —</option>';
      return;
    }
    sel.innerHTML = '<option value="">— select a report —</option>' +
      reports.map(r => {
        const label = [r.borrower_name || r.id.slice(0, 8), r.industry, r.status]
          .filter(Boolean).join(" · ");
        return `<option value="${r.id}">${label}</option>`;
      }).join("");
  } catch (e) {
    sel.innerHTML = '<option value="">— error loading reports —</option>';
    alert("Could not load reports:\n" + e.message + "\n\nCheck Base URL and Email in Settings.");
  } finally {
    btn.disabled = false;
    btn.textContent = "📋 Load";
  }
}

document.getElementById("loadReportsBtn").addEventListener("click", loadReportList);

document.getElementById("reportSelect").addEventListener("change", function () {
  const rid = this.value;
  document.getElementById("reportId").value = rid;
  const sel = document.getElementById("selectedReport");
  if (rid) {
    const label = this.options[this.selectedIndex].text;
    sel.textContent = "✓ " + label;
  } else {
    sel.textContent = "";
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
  if (!rid) { alert("Please select a report from the dropdown (click 📋 Load)."); return null; }
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

// ── File drop zone ────────────────────────────────────────────────────────────

const dropZone = document.getElementById("dropZone");

dropZone.addEventListener("click", () => document.getElementById("docFiles").click());
dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.style.borderColor = "#3b82f6"; });
dropZone.addEventListener("dragleave", () => { dropZone.style.borderColor = "#475569"; });
dropZone.addEventListener("drop", e => {
  e.preventDefault();
  dropZone.style.borderColor = "#475569";
  const dt = e.dataTransfer;
  if (dt.files.length) {
    document.getElementById("docFiles").files; // can't assign, use stored ref
    _droppedFiles = Array.from(dt.files);
    renderFileList(_droppedFiles);
  }
});
document.getElementById("docFiles").addEventListener("change", function() {
  _droppedFiles = null; // use input.files instead
  renderFileList(Array.from(this.files));
});

let _droppedFiles = null;

function getSelectedFiles() {
  if (_droppedFiles) return _droppedFiles;
  return Array.from(document.getElementById("docFiles").files);
}

function renderFileList(files) {
  const el = document.getElementById("fileList");
  if (!files.length) { el.textContent = ""; return; }
  el.innerHTML = files.map(f =>
    `<span style="display:inline-block;margin:2px 4px 2px 0">📄 ${f.name}</span>`
  ).join("");
}

// ── Automate panel actions ────────────────────────────────────────────────────

document.getElementById("fullAutoBtn").addEventListener("click", async () => {
  const rid = getReportId();
  if (!rid) return;
  const companyName = document.getElementById("companyName").value.trim();
  const files = getSelectedFiles();

  document.querySelectorAll(".step").forEach(el => { el.className = "step idle"; });
  disableActions(true);

  try {
    // Step 0: upload documents directly from popup (File objects can't cross SW boundary)
    if (files.length > 0) {
      setStep("upload", "running", `0 / ${files.length} files`);

      // Ensure we have a fresh JWT
      const loginResp = await chrome.runtime.sendMessage({ action: "login" });
      if (!loginResp.ok) throw new Error(loginResp.error);

      const { baseUrl, token } = await new Promise(r =>
        chrome.storage.local.get(["baseUrl", "token"], r)
      );

      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        setStep("upload", "running", `${i + 1} / ${files.length}: ${f.name}`);
        const fd = new FormData();
        fd.append("file", f);
        const resp = await fetch(
          `${baseUrl}/api/credit-report/reports/${rid}/documents`,
          { method: "POST", headers: { Authorization: "Bearer " + token }, body: fd }
        );
        if (!resp.ok) {
          const txt = await resp.text().catch(() => "");
          throw new Error(`Upload failed — ${f.name}: HTTP ${resp.status} ${txt.slice(0, 120)}`);
        }
      }
      setStep("upload", "done", `${files.length} file(s) uploaded`);
    } else {
      setStep("upload", "skip", "no files — using existing documents");
    }

    // Steps 1–6 run in service worker (login, ETL, suggestions, conflicts, gapfill, generate)
    const resp = await chrome.runtime.sendMessage({ action: "full_auto", reportId: rid, companyName });
    if (!resp.ok) throw new Error(resp.error);

  } catch (e) {
    setStep("upload", "error", e.message);
    alert("Error: " + e.message);
  } finally {
    disableActions(false);
  }
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

// Auto-load report list on popup open if settings are configured
(async function autoLoadReports() {
  const data = await chrome.storage.local.get(["baseUrl", "email"]);
  if (data.baseUrl && data.email) {
    loadReportList();
  } else {
    // Guide new users to Settings
    document.getElementById("reportSelect").innerHTML =
      '<option value="">— configure Settings first —</option>';
    // Switch to Settings tab so they see the form
    const tab = document.querySelector('.tab[data-tab="settings"]');
    if (tab) tab.click();
  }
})();

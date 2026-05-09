const API = "/api/credit-report";
const state = {
  accessToken: localStorage.getItem("cr_access_token") || "",
  refreshToken: localStorage.getItem("cr_refresh_token") || "",
  role: localStorage.getItem("cr_role") || "",
  reportId: localStorage.getItem("cr_report_id") || "",
  reports: [],
};

const $ = (id) => document.getElementById(id);
const sectionSelect = $("sectionNo");

const sectionTemplates = {
  1: { facility: { purpose: "Working capital", amount_usd_m: 50, tenor_years: 3 }, collateral: [] },
  2: { risk_assessment: { repayment_source: "Operating cash flow", key_risks: ["Freight rate volatility"] } },
  3: { rating: { internal_rating: "", outlook: "stable", esg_notes: "" } },
  4: { company_profile: { borrower: "", shareholders: [], fleet: [] } },
  5: { collateral: { vessels: [], ltv_percent: 75, acr_percent: 133 } },
  6: { project: { description: "", milestones: [], delivery_date: "" } },
  7: { financials: { revenue_usd_m: 5000, ebitda_usd_m: 900, total_debt_usd_m: 3200, cash_usd_m: 600 } },
  8: { compliance: { sanctions_screening: "clear", covenants: [] } },
  9: { recommendation: { proposed_limit_usd_m: 50, rationale: "" } },
  10: { appendix: { assumptions: [], projections: [] } },
};

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.style.background = isError ? "#9a3412" : "#111827";
  node.classList.add("show");
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => node.classList.remove("show"), 4200);
}

function authHeaders(extra = {}) {
  return {
    ...extra,
    ...(state.accessToken ? { Authorization: `Bearer ${state.accessToken}` } : {}),
  };
}

async function api(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : payload || response.statusText;
    throw new Error(Array.isArray(detail) ? detail.map((d) => d.msg).join("\n") : detail);
  }
  return payload;
}

function updateSession() {
  const card = $("sessionCard");
  const logoutButton = $("logoutButton");
  if (state.accessToken) {
    card.classList.add("online");
    logoutButton.hidden = false;
    $("sessionTitle").textContent = `已登入${state.role ? `（${state.role}）` : ""}`;
    $("sessionDetail").textContent = state.reportId ? `目前案件：${state.reportId}` : "請選擇或建立案件";
  } else {
    card.classList.remove("online");
    logoutButton.hidden = true;
    $("sessionTitle").textContent = "尚未登入";
    $("sessionDetail").textContent = "請先使用系統帳號登入";
  }
  $("selectedReport").textContent = state.reportId ? `已選取：${state.reportId}` : "尚未選取案件";
}

function appendText(parent, tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.appendChild(node);
  return node;
}

function renderReports() {
  const list = $("reportsList");
  if (!state.reports.length) {
    list.className = "cards empty";
    list.textContent = "目前沒有可檢視的案件。";
    return;
  }
  list.className = "cards";
  list.innerHTML = "";
  for (const report of state.reports) {
    const card = document.createElement("div");
    card.className = `report-card${report.id === state.reportId ? " active" : ""}`;
    appendText(card, "strong", report.borrower_name || "未命名借款人");
    appendText(card, "small", `${report.industry || "-"} · ${report.report_type || "未指定類型"} · ${report.status}`);
    card.appendChild(document.createElement("br"));
    appendText(card, "small", report.id);
    card.addEventListener("click", () => selectReport(report.id));
    list.appendChild(card);
  }
}

async function refreshReports() {
  state.reports = await api("/reports?limit=50");
  if (!state.reportId && state.reports.length) selectReport(state.reports[0].id, false);
  renderReports();
  updateSession();
}

async function selectReport(reportId, loadRelated = true) {
  state.reportId = reportId;
  localStorage.setItem("cr_report_id", reportId);
  renderReports();
  updateSession();
  if (loadRelated) {
    await Promise.allSettled([loadSectionInput(), refreshDocuments(), loadOutput()]);
  }
}

function requireReport() {
  if (!state.reportId) throw new Error("請先建立或選取案件。 ");
}

function currentSection() {
  return Number(sectionSelect.value || 1);
}

function logout() {
  state.accessToken = "";
  state.refreshToken = "";
  state.role = "";
  state.reportId = "";
  state.reports = [];
  localStorage.removeItem("cr_access_token");
  localStorage.removeItem("cr_refresh_token");
  localStorage.removeItem("cr_role");
  localStorage.removeItem("cr_report_id");
  renderReports();
  $("documentsList").className = "chips empty";
  $("documentsList").textContent = "尚無文件。";
  $("markdownOutput").value = "";
  $("outputMeta").textContent = "尚未載入輸出。";
  updateSession();
  toast("已登出。 ");
}

async function login(event) {
  event.preventDefault();
  const form = new URLSearchParams();
  form.set("username", $("email").value.trim());
  form.set("password", $("password").value);
  const payload = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  }).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || response.statusText);
    return data;
  });
  state.accessToken = payload.access_token;
  state.refreshToken = payload.refresh_token;
  state.role = payload.role;
  localStorage.setItem("cr_access_token", state.accessToken);
  localStorage.setItem("cr_refresh_token", state.refreshToken);
  localStorage.setItem("cr_role", state.role || "");
  updateSession();
  await refreshReports();
  toast("登入成功，案件已載入。 ");
}

async function createReport(event) {
  event.preventDefault();
  const payload = {
    industry: $("industry").value,
    report_type: $("reportType").value.trim() || "credit_review",
    borrower_name: $("borrowerName").value.trim() || "未命名借款人",
    booking_branch: $("bookingBranch").value.trim() || null,
  };
  const report = await api("/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refreshReports();
  await selectReport(report.id);
  toast("案件建立成功。 ");
}

async function loadSectionInput() {
  requireReport();
  const section = currentSection();
  try {
    const payload = await api(`/reports/${state.reportId}/inputs/${section}`);
    $("sectionJson").value = JSON.stringify(payload.input_json || {}, null, 2);
    toast(`章節 ${section} 輸入已載入。`);
  } catch (error) {
    $("sectionJson").value = JSON.stringify(sectionTemplates[section] || {}, null, 2);
    toast(`章節 ${section} 尚無輸入，已套用範本。`);
  }
}

async function saveSectionInput() {
  requireReport();
  const section = currentSection();
  let inputJson;
  try {
    inputJson = JSON.parse($("sectionJson").value || "{}");
  } catch (error) {
    throw new Error(`JSON 格式錯誤：${error.message}`);
  }
  await api(`/reports/${state.reportId}/inputs/${section}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section_no: section, input_json: inputJson }),
  });
  toast(`章節 ${section} 輸入已儲存。`);
}

async function uploadPdf() {
  requireReport();
  const file = $("pdfFile").files[0];
  if (!file) throw new Error("請先選擇 PDF 檔案。 ");
  const form = new FormData();
  form.append("file", file);
  await api(`/reports/${state.reportId}/documents`, { method: "POST", body: form });
  $("pdfFile").value = "";
  await refreshDocuments();
  toast("PDF 已上傳並擷取文字。 ");
}

async function refreshDocuments() {
  requireReport();
  const docs = await api(`/reports/${state.reportId}/documents`);
  const list = $("documentsList");
  if (!docs.length) {
    list.className = "chips empty";
    list.textContent = "尚無文件。";
    return;
  }
  list.className = "chips";
  list.innerHTML = "";
  docs.forEach((doc) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.append(document.createTextNode(doc.original_filename));
    appendText(chip, "small", `${Math.round(doc.file_size_bytes / 1024)} KB`);
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "刪除";
    del.addEventListener("click", async () => {
      try {
        await api(`/reports/${state.reportId}/documents/${doc.id}`, { method: "DELETE" });
        await refreshDocuments();
        toast("文件已刪除。 ");
      } catch (error) {
        toast(error.message || String(error), true);
      }
    });
    chip.appendChild(del);
    list.appendChild(chip);
  });
}

async function generateOne() {
  requireReport();
  const section = currentSection();
  $("outputMeta").textContent = `章節 ${section} 產生中，請稍候...`;
  const result = await api(`/reports/${state.reportId}/generate/${section}`, { method: "POST" });
  toast(`章節 ${section} 產生完成：${result.status}`);
  await loadOutput();
}

async function generateAll() {
  requireReport();
  $("outputMeta").textContent = "全份報告產生中，請稍候...";
  const result = await api(`/reports/${state.reportId}/generate`, { method: "POST" });
  toast(`全份報告流程完成：\n${JSON.stringify(result.sections, null, 2)}`);
  await loadOutput();
}

async function loadOutput() {
  requireReport();
  const section = currentSection();
  const output = await api(`/reports/${state.reportId}/sections/${section}/output`);
  $("outputMeta").textContent = `章節 ${section} · ${output.status} · ${output.model_id || "未記錄模型"} · tokens ${output.tokens_used ?? "-"}`;
  $("markdownOutput").value = output.markdown || "";
}

async function copyOutput() {
  const text = $("markdownOutput").value;
  if (!text) throw new Error("目前沒有可複製的 Markdown。 ");
  await navigator.clipboard.writeText(text);
  toast("Markdown 已複製到剪貼簿。 ");
}

function bind(id, event, handler) {
  $(id).addEventListener(event, async (evt) => {
    try { await handler(evt); } catch (error) { toast(error.message || String(error), true); }
  });
}

function init() {
  for (let i = 1; i <= 10; i += 1) {
    const option = document.createElement("option");
    option.value = String(i);
    option.textContent = `Section ${i}`;
    sectionSelect.appendChild(option);
  }
  $("sectionJson").value = JSON.stringify(sectionTemplates[1], null, 2);
  bind("loginForm", "submit", login);
  bind("logoutButton", "click", logout);
  bind("reportForm", "submit", createReport);
  bind("refreshReports", "click", refreshReports);
  bind("loadSectionInput", "click", loadSectionInput);
  bind("saveSectionInput", "click", saveSectionInput);
  bind("uploadPdf", "click", uploadPdf);
  bind("refreshDocs", "click", refreshDocuments);
  bind("generateOne", "click", generateOne);
  bind("generateAll", "click", generateAll);
  bind("loadOutput", "click", loadOutput);
  bind("copyOutput", "click", copyOutput);
  sectionSelect.addEventListener("change", () => {
    $("sectionJson").value = JSON.stringify(sectionTemplates[currentSection()] || {}, null, 2);
    if (state.reportId) loadOutput().catch(() => {});
  });
  updateSession();
  if (state.accessToken) refreshReports().catch((error) => toast(error.message, true));
}

init();

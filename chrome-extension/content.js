/**
 * Content script — reads report UUID from window.__CUB_RID exposed by the SPA.
 *
 * The SPA uses `let currentRid` (not on window). static/index.html now also sets
 * window.__CUB_RID = rid in loadReportDetail(), which this isolated-world script
 * can read via a postMessage bridge into the page's main world.
 */

let _rid = null;

// Bridge: ask page main world for window.__CUB_RID (set by SPA on report open)
function fetchRidFromPage(cb) {
  const nonce = "_cub_" + Math.random().toString(36).slice(2);
  window.addEventListener("message", function h(e) {
    if (e.source === window && e.data && e.data.__cub_nonce === nonce) {
      window.removeEventListener("message", h);
      if (e.data.rid) _rid = e.data.rid;
      cb(e.data.rid || null);
    }
  });
  const s = document.createElement("script");
  s.textContent = `window.postMessage({__cub_nonce:"${nonce}",rid:window.__CUB_RID||null},"*");`;
  document.documentElement.appendChild(s);
  s.remove();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== "get_page_context") return true;

  // Serve cached value immediately if available
  if (_rid) { sendResponse({ reportId: _rid, url: location.href }); return true; }

  // Fallback: URL patterns (future-proof if SPA adds routing)
  const hm = location.hash.match(/[#\/]reports?[\/=]([a-f0-9-]{36})/i);
  const sm = location.search.match(/report_id=([a-f0-9-]{36})/i);
  if (hm) { _rid = hm[1]; sendResponse({ reportId: _rid, url: location.href }); return true; }
  if (sm) { _rid = sm[1]; sendResponse({ reportId: _rid, url: location.href }); return true; }

  // Read window.__CUB_RID from SPA's main world
  fetchRidFromPage(rid => sendResponse({ reportId: rid, url: location.href }));
  return true; // async
});

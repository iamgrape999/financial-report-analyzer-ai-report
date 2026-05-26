/**
 * Content script — auto-captures report UUID by intercepting the SPA's fetch calls.
 *
 * The SPA uses `let currentRid` (not on window) and no URL routing, so the UUID
 * cannot be read from window or location. Instead we patch window.fetch in the
 * page's main world: every API call to /reports/{UUID}/ exposes the UUID via a
 * CustomEvent, which this isolated-world script stores and returns to the popup.
 */

let _rid = null;

// ── 1. Inject fetch interceptor into main world ───────────────────────────────
(function () {
  const s = document.createElement("script");
  s.textContent = `(function(){
    const _f = window.fetch;
    window.fetch = function(url) {
      const m = String(url).match(/\\/reports\\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})[\\/]/i);
      if (m) document.dispatchEvent(new CustomEvent("__cub_rid", { detail: m[1] }));
      return _f.apply(this, arguments);
    };
  })();`;
  document.documentElement.appendChild(s);
  s.remove();
})();

// ── 2. Cache UUID whenever a report API call fires ────────────────────────────
document.addEventListener("__cub_rid", e => { _rid = e.detail; });

// ── 3. Answer popup queries ───────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== "get_page_context") return true;

  // Fast path: already captured from a fetch call
  if (_rid) { sendResponse({ reportId: _rid, url: location.href }); return true; }

  // Fallback A: URL patterns (for future SPA versions that add routing)
  const hm = location.hash.match(/[#\/]reports?[\/=]([a-f0-9-]{36})/i);
  const sm = location.search.match(/report_id=([a-f0-9-]{36})/i);
  if (hm) { sendResponse({ reportId: hm[1], url: location.href }); return true; }
  if (sm) { sendResponse({ reportId: sm[1], url: location.href }); return true; }

  // Fallback B: ask main world for __CUB_CURRENT_RID set by interceptor
  const nonce = "_cub_" + Math.random().toString(36).slice(2);
  window.addEventListener("message", function h(e) {
    if (e.source === window && e.data && e.data.__cub_nonce === nonce) {
      window.removeEventListener("message", h);
      if (e.data.rid) _rid = e.data.rid;
      sendResponse({ reportId: e.data.rid || null, url: location.href });
    }
  });
  const s2 = document.createElement("script");
  s2.textContent = `window.postMessage({__cub_nonce:"${nonce}",rid:window.__CUB_CURRENT_RID||null},"*");`;
  document.documentElement.appendChild(s2);
  s2.remove();
  return true;
});

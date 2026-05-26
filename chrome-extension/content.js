/**
 * Content script — runs at document_start (before SPA code executes).
 * Injects a fetch interceptor into the page's main world immediately,
 * so every /reports/{UUID}/ API call is captured including the initial load.
 */

let _rid = null;

// Inject interceptor BEFORE SPA runs — document_start guarantees this
(function () {
  const s = document.createElement("script");
  s.textContent = `(function(){
    const _f = window.fetch;
    window.fetch = function(url) {
      try {
        const m = String(url).match(/\\/reports\\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})[\\/]/i);
        if (m) {
          window.__CUB_RID = m[1];
          document.dispatchEvent(new CustomEvent("__cub_rid", { detail: m[1] }));
        }
      } catch(_){}
      return _f.apply(this, arguments);
    };
  })();`;
  // At document_start, documentElement exists; head/body may not yet
  (document.head || document.documentElement).appendChild(s);
  s.remove();
})();

// Cache UUID as soon as it arrives from an intercepted fetch
document.addEventListener("__cub_rid", e => { _rid = e.detail; });

// Respond to popup queries
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== "get_page_context") return true;

  // Best case: already captured from a fetch call
  if (_rid) { sendResponse({ reportId: _rid, url: location.href }); return true; }

  // Ask main world for window.__CUB_RID (set by interceptor or SPA)
  const nonce = "_cub_" + Math.random().toString(36).slice(2);
  window.addEventListener("message", function h(e) {
    if (e.source === window && e.data && e.data.__cub_nonce === nonce) {
      window.removeEventListener("message", h);
      if (e.data.rid) _rid = e.data.rid;
      sendResponse({ reportId: e.data.rid || null, url: location.href });
    }
  });
  const s = document.createElement("script");
  s.textContent = `window.postMessage({__cub_nonce:"${nonce}",rid:window.__CUB_RID||null},"*");`;
  document.documentElement.appendChild(s);
  s.remove();
  return true;
});

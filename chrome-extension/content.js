/**
 * Content script — reads current report ID from the SPA's URL hash or window state.
 * Responds to queries from the popup/service-worker.
 *
 * Content scripts run in an isolated world and cannot directly read SPA globals
 * like window.currentRid. We bridge via postMessage to the page's main world.
 */

function readFromPageContext(callback) {
  const nonce = "_cub_" + Math.random().toString(36).slice(2);
  window.addEventListener("message", function handler(e) {
    if (e.source === window && e.data && e.data.__cub_nonce === nonce) {
      window.removeEventListener("message", handler);
      callback(e.data.reportId || null);
    }
  });
  // Inject a tiny inline script that runs in the SPA's main world
  const s = document.createElement("script");
  s.textContent = `(function(){
    var rid = window.currentRid || window.__CUB_REPORT_ID || null;
    window.postMessage({ __cub_nonce: "${nonce}", reportId: rid }, "*");
  })();`;
  document.documentElement.appendChild(s);
  s.remove();
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "get_page_context") {
    // 1. Try URL patterns first (fast, synchronous)
    const hash   = location.hash;
    const search = location.search;
    const hashMatch   = hash.match(/[#/]reports?[/=]([a-f0-9-]{36})/i)
                     || hash.match(/[#/]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/);
    const searchMatch = search.match(/report_id=([a-f0-9-]{36})/i);

    if (hashMatch)   { sendResponse({ reportId: hashMatch[1],   url: location.href }); return true; }
    if (searchMatch) { sendResponse({ reportId: searchMatch[1], url: location.href }); return true; }

    // 2. Bridge to the SPA's main-world globals (currentRid, __CUB_REPORT_ID)
    readFromPageContext(reportId => {
      sendResponse({ reportId, url: location.href });
    });
    return true; // keep message channel open for async response
  }
  return true;
});

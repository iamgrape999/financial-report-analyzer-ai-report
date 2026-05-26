/**
 * Runs in ISOLATED world — handles chrome.runtime messages.
 * Reads window.__CUB_RID (set by content-main.js) via postMessage bridge.
 */
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== "get_page_context") return true;

  const nonce = "_cub_" + Math.random().toString(36).slice(2);
  window.addEventListener("message", function h(e) {
    if (e.source === window && e.data && e.data.__cub_nonce === nonce) {
      window.removeEventListener("message", h);
      sendResponse({ reportId: e.data.rid || null, url: location.href });
    }
  });
  const s = document.createElement("script");
  s.textContent = `window.postMessage({__cub_nonce:"${nonce}",rid:window.__CUB_RID||null},"*");`;
  document.documentElement.appendChild(s);
  s.remove();
  return true;
});

/**
 * Content script — reads current report ID from the SPA's URL hash or window state.
 * Responds to queries from the popup/service-worker.
 */

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "get_page_context") {
    // Try to extract report ID from URL hash (#report/abc123) or search params
    const hash   = location.hash;
    const search = location.search;

    let reportId = null;

    // Pattern: /#/reports/REPORT_ID or ?report_id=REPORT_ID or #report-REPORT_ID
    const hashMatch   = hash.match(/[#/]reports?[/=]([a-f0-9-]{36})/i)
                     || hash.match(/[#/]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/);
    const searchMatch = search.match(/report_id=([a-f0-9-]{36})/i);

    if (hashMatch)   reportId = hashMatch[1];
    else if (searchMatch) reportId = searchMatch[1];

    // Also try to read from the SPA's exposed global (if any)
    if (!reportId && window.__CUB_REPORT_ID) reportId = window.__CUB_REPORT_ID;
    if (!reportId && window.currentRid)      reportId = window.currentRid;

    sendResponse({ reportId, url: location.href });
  }
  return true;
});

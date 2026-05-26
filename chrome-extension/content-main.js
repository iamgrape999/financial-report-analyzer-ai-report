/**
 * Runs in MAIN world (direct access to window.fetch, no script injection).
 * Patches fetch before the SPA runs to capture the report UUID from API URLs.
 */
(function () {
  const _orig = window.fetch;
  window.fetch = function (url) {
    try {
      const m = String(url).match(
        /\/reports\/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\//i
      );
      if (m) window.__CUB_RID = m[1];
    } catch (_) {}
    return _orig.apply(this, arguments);
  };
})();

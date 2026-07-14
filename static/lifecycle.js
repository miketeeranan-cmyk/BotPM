// Closing the last tab quits the app (see /api/tab/closing in app.py).
//
// The browser's own "Leave site?" prompt is the confirmation -- its wording
// isn't ours to set, but cancelling it means the tab never unloads, so nothing
// is reported to the server and the app stays up.
(function () {
  const PING_INTERVAL_MS = 2000;
  let internalNav = false;

  function ping() {
    fetch("/api/tab/ping", { method: "POST" }).catch(() => {});
  }
  ping();
  setInterval(ping, PING_INTERVAL_MS);

  // Send <-> Scan links stay inside the app, so prompting on them would be
  // noise. The page that loads next resumes pinging, which cancels the quit
  // the report below schedules.
  document.addEventListener("click", (e) => {
    const link = e.target.closest("a[href]");
    if (link && link.origin === location.origin) internalNav = true;
  });

  window.addEventListener("beforeunload", (e) => {
    if (internalNav || window.appQuitting) return;
    e.preventDefault();
    e.returnValue = "";
  });

  window.addEventListener("pagehide", (e) => {
    // persisted means the page is going into the back/forward cache rather than
    // away -- the tab is still there, so it isn't a close.
    if (e.persisted || window.appQuitting) return;
    navigator.sendBeacon("/api/tab/closing");
  });
})();

// Closing the last tab quits the app (see /api/tab/closing in app.py).
(function () {
  const PING_INTERVAL_MS = 2000;

  function ping() {
    fetch("/api/tab/ping", { method: "POST" }).catch(() => {});
  }
  ping();
  setInterval(ping, PING_INTERVAL_MS);

  window.addEventListener("pagehide", (e) => {
    // persisted means the page is going into the back/forward cache rather than
    // away -- the tab is still there, so it isn't a close.
    if (e.persisted || window.appQuitting) return;
    navigator.sendBeacon("/api/tab/closing");
  });
})();

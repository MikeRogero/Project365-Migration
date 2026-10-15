"""On-demand activity tracking for local Project365 HTTP services."""

from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit


PAGE_ACTIVITY_SCRIPT = """<script id="project365-page-activity">
(() => {
  const heartbeat = () => {
    fetch('/api/service-activity', {
      method: 'POST',
      headers: { 'X-Project365-Page': '1' },
      credentials: 'same-origin',
      cache: 'no-store',
      keepalive: true
    }).catch(() => {});
  };
  heartbeat();
  window.setInterval(heartbeat, 30000);
  window.addEventListener('pageshow', heartbeat);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') heartbeat();
  });
})();
</script>"""


def inject_activity(html: str) -> str:
    """Insert the page heartbeat immediately before the closing body tag."""
    if PAGE_ACTIVITY_SCRIPT in html or 'id="project365-page-activity"' in html:
        return html
    lower = html.lower()
    body_end = lower.rfind("</body>")
    if body_end < 0:
        return html + PAGE_ACTIVITY_SCRIPT
    return html[:body_end] + PAGE_ACTIVITY_SCRIPT + html[body_end:]


class _ActivityTracker:
    def __init__(self, server, *, busy, idle_seconds, check_interval, clock):
        self.server = server
        self.busy = busy
        self.idle_seconds = idle_seconds
        self.check_interval = check_interval
        self.clock = clock
        self.last_page_activity = clock()
        self.active_requests = 0
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._serving = threading.Event()
        self._wake = threading.Event()
        self.monitor_thread = threading.Thread(
            target=self._monitor, name="project365-idle-monitor", daemon=True
        )

    def begin_request(self):
        with self._lock:
            self.active_requests += 1

    def end_request(self):
        with self._lock:
            self.active_requests -= 1

    def record_page_activity(self):
        with self._lock:
            self.last_page_activity = self.clock()
        self._wake.set()

    def _monitor(self):
        try:
            while not self._stopped.is_set():
                self._wake.wait(self.check_interval)
                self._wake.clear()
                if self._stopped.is_set() or self.server.fileno() < 0:
                    return
                with self._lock:
                    idle = self.clock() - self.last_page_activity >= self.idle_seconds
                    requests_active = self.active_requests > 0
                if not idle or requests_active or not self._serving.is_set():
                    continue
                try:
                    busy = bool(self.busy()) if self.busy is not None else False
                except Exception as exc:  # fail-safe: a failed busy probe keeps service alive
                    logging.getLogger(__name__).warning(
                        "Project365 idle check failed; keeping service alive (%s)",
                        type(exc).__name__,
                    )
                    continue
                if busy:
                    continue
                self.server.shutdown()
                return
        finally:
            self._stopped.set()


def activity_handler(handler_class):
    """Wrap a request handler with accounting and the private heartbeat endpoint."""

    class ActivityHandler(handler_class):
        def _run_with_activity(self, method):
            tracker = getattr(self.server, "service_activity", None)
            if tracker is not None:
                tracker.begin_request()
            try:
                if method == "do_POST" and self.path.split("?", 1)[0] == "/api/service-activity":
                    self._handle_service_activity(tracker)
                    return
                getattr(super(), method)()
            finally:
                if tracker is not None:
                    tracker.end_request()

        def _handle_service_activity(self, tracker):
            if tracker is None or self.headers.get("X-Project365-Page") != "1":
                self.send_error(403)
                return
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site is not None and fetch_site.lower() != "same-origin":
                self.send_error(403)
                return
            origin = self.headers.get("Origin")
            if origin is not None and not _origin_matches_request(origin, self):
                self.send_error(403)
                return
            tracker.record_page_activity()
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._run_with_activity("do_GET")

        def do_POST(self):
            self._run_with_activity("do_POST")

    return ActivityHandler


def _origin_matches_request(origin: str, handler: BaseHTTPRequestHandler) -> bool:
    try:
        parsed = urlsplit(origin)
        host = urlsplit("//" + handler.headers.get("Host", ""))
        secure = getattr(handler.connection, "cipher", None) is not None
        expected_scheme = "https" if secure else "http"
        if (
            parsed.scheme.lower() != expected_scheme
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not host.hostname
        ):
            return False
        origin_port = parsed.port or (443 if expected_scheme == "https" else 80)
        host_port = host.port or (443 if expected_scheme == "https" else 80)
        return parsed.hostname.lower() == host.hostname.lower() and origin_port == host_port
    except (ValueError, TypeError):
        return False


def start_idle_shutdown(
    server,
    *,
    busy=None,
    idle_seconds=900,
    check_interval=5.0,
    clock=time.monotonic,
):
    """Attach activity tracking and stop ``server`` after its pages go idle."""
    tracker = _ActivityTracker(
        server,
        busy=busy,
        idle_seconds=idle_seconds,
        check_interval=check_interval,
        clock=clock,
    )
    server.service_activity = tracker

    # BaseServer.shutdown() must only run while serve_forever() is active; it
    # blocks indefinitely if called before that loop starts.
    original_serve_forever = server.serve_forever

    def tracked_serve_forever(*args, **kwargs):
        tracker._serving.set()
        try:
            return original_serve_forever(*args, **kwargs)
        finally:
            tracker._serving.clear()
            tracker._stopped.set()
            tracker._wake.set()

    server.serve_forever = tracked_serve_forever
    tracker.monitor_thread.start()
    return tracker

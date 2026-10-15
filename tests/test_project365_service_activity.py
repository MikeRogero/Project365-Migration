import http.client
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from project365_service_activity import (
    PAGE_ACTIVITY_SCRIPT,
    activity_handler,
    inject_activity,
    start_idle_shutdown,
)


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class ServiceActivityTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.shutdown_called = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner_self):
                inner_self.send_response(200)
                inner_self.end_headers()
                inner_self.wfile.write(b"ok")

            def do_POST(inner_self):
                inner_self.send_response(200)
                inner_self.end_headers()

            def log_message(inner_self, *_args):
                pass

        self.handler_class = activity_handler(Handler)
        self.server = HTTPServer(("127.0.0.1", 0), self.handler_class)
        original_shutdown = self.server.shutdown

        def record_shutdown():
            self.shutdown_called.set()
            original_shutdown()

        self.server.shutdown = record_shutdown
        self.tracker = start_idle_shutdown(
            self.server, idle_seconds=5, check_interval=0.01, clock=self.clock
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

    def tearDown(self):
        if not self.shutdown_called.is_set():
            self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=1)
        self.tracker.monitor_thread.join(timeout=1)

    def request(self, method="GET", path="/", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_idle_threshold_shuts_down_server(self):
        self.clock.value += 4.9
        time.sleep(0.03)
        self.assertFalse(self.shutdown_called.is_set())
        self.clock.value += 0.2
        time.sleep(0.03)
        self.assertTrue(self.shutdown_called.is_set())

    def test_default_idle_threshold_and_close_watchdog(self):
        server = HTTPServer(("127.0.0.1", 0), self.handler_class)
        tracker = start_idle_shutdown(server, check_interval=0.01, clock=self.clock)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            self.assertEqual(tracker.idle_seconds, 900)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
            tracker.monitor_thread.join(timeout=1)
        self.assertFalse(tracker.monitor_thread.is_alive())

    def test_idle_monitor_waits_for_serve_forever_to_start(self):
        server = HTTPServer(("127.0.0.1", 0), self.handler_class)
        shutdown_called = threading.Event()
        original_shutdown = server.shutdown

        def record_shutdown():
            shutdown_called.set()
            original_shutdown()

        server.shutdown = record_shutdown
        tracker = start_idle_shutdown(
            server, idle_seconds=0, check_interval=0.01, clock=self.clock
        )
        time.sleep(0.03)
        self.assertFalse(shutdown_called.is_set())
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.assertTrue(shutdown_called.wait(1))
        server.server_close()
        thread.join(timeout=1)
        tracker.monitor_thread.join(timeout=1)
        self.assertFalse(tracker.monitor_thread.is_alive())

    def test_heartbeat_renews_idle_deadline(self):
        self.clock.value += 4
        status, body = self.request(
            "POST", "/api/service-activity", {"X-Project365-Page": "1"}
        )
        self.assertEqual((status, body), (204, b""))
        self.assertEqual(self.tracker.last_page_activity, self.clock.value)
        self.clock.value += 4.9
        time.sleep(0.03)
        self.assertFalse(self.shutdown_called.is_set())

    def test_get_probe_does_not_renew_activity(self):
        initial = self.tracker.last_page_activity
        self.clock.value += 4
        self.request("GET", "/api/health")
        self.assertEqual(self.tracker.last_page_activity, initial)

    def test_active_request_prevents_shutdown(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(inner_self):
                entered.set()
                release.wait(timeout=1)
                inner_self.send_response(200)
                inner_self.end_headers()

            def log_message(inner_self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), activity_handler(SlowHandler))
        tracker = start_idle_shutdown(server, idle_seconds=5, check_interval=0.01, clock=self.clock)
        shutdown_called = threading.Event()
        original_shutdown = server.shutdown

        def record_shutdown():
            shutdown_called.set()
            original_shutdown()

        server.shutdown = record_shutdown
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        tracker.busy = lambda: True
        self.clock.value += 6

        def complete_request():
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            conn.request("GET", "/")
            response = conn.getresponse()
            response.read()
            conn.close()

        request_thread = threading.Thread(
            target=complete_request
        )
        request_thread.start()
        self.assertTrue(entered.wait(1))
        tracker.busy = lambda: False
        time.sleep(0.03)
        self.assertFalse(shutdown_called.is_set())
        self.assertEqual(tracker.active_requests, 1)
        release.set()
        request_thread.join(timeout=1)
        self.assertTrue(shutdown_called.wait(1))
        if not shutdown_called.is_set():
            server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
        tracker.monitor_thread.join(timeout=1)

    def test_busy_callback_prevents_shutdown(self):
        self.tracker.busy = lambda: True
        self.clock.value += 6
        time.sleep(0.03)
        self.assertFalse(self.shutdown_called.is_set())
        self.tracker.busy = lambda: False
        time.sleep(0.03)
        self.assertTrue(self.shutdown_called.is_set())

    def test_busy_callback_error_fails_safe_and_recovers(self):
        def broken_busy_check():
            raise RuntimeError("busy state unavailable")

        self.tracker.busy = broken_busy_check
        self.clock.value += 6
        with self.assertLogs("project365_service_activity", level="WARNING"):
            time.sleep(0.03)
        self.assertFalse(self.shutdown_called.is_set())
        self.tracker.busy = lambda: False
        time.sleep(0.03)
        self.assertTrue(self.shutdown_called.is_set())

    def test_heartbeat_requires_same_origin_page_header_and_rejects_cross_origin(self):
        self.assertEqual(
            self.request("POST", "/api/service-activity", {"X-Project365-Page": "1"})[0],
            204,
        )
        port = self.server.server_port
        self.assertEqual(
            self.request(
                "POST",
                "/api/service-activity",
                {
                    "X-Project365-Page": "1",
                    "Origin": f"http://127.0.0.1:{port}",
                    "Sec-Fetch-Site": "same-origin",
                },
            )[0],
            204,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/api/service-activity",
                {"X-Project365-Page": "1", "Origin": "https://attacker.invalid"},
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/api/service-activity",
                {"X-Project365-Page": "1", "Sec-Fetch-Site": "cross-site"},
            )[0],
            403,
        )
        self.assertEqual(
            self.request("POST", "/api/service-activity")[0], 403
        )

    def test_page_activity_script_injected_once(self):
        html = "<html><body>page</body></html>"
        injected = inject_activity(html)
        self.assertIn(PAGE_ACTIVITY_SCRIPT, injected)
        self.assertEqual(inject_activity(injected), injected)
        self.assertEqual(injected.count(PAGE_ACTIVITY_SCRIPT), 1)


if __name__ == "__main__":
    unittest.main()

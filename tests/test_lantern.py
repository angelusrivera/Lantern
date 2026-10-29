from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import deque
from unittest.mock import Mock, patch
from pathlib import Path

from lantern.__main__ import initialize_database, load_config, poll_forever
from lantern.agent import normalize_access_line
from lantern.web import DashboardHandler, probe


class ConfigTests(unittest.TestCase):
    def write_config(self, folder: str, content: dict) -> Path:
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        return path

    def test_loads_defaults_and_configured_targets(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_config(folder, {"targets": [{"name": "NAS", "host": "nas.local", "ports": [22]}]})
            self.assertEqual(load_config(path), ([{"name": "NAS", "host": "nas.local", "ports": [22]}], 1.0, 2.0, 30, 95.0, 1000.0))

    def test_rejects_invalid_ports_and_duplicate_services(self) -> None:
        invalid = [
            {"targets": [{"name": "bad", "host": "localhost", "ports": [0]}]},
            {"targets": [
                {"name": "one", "host": "localhost", "ports": [80]},
                {"name": "two", "host": "LOCALHOST", "ports": [80]},
            ]},
            {"poll_interval_seconds": True, "targets": [{"name": "one", "host": "localhost", "ports": [80]}]},
        ]
        for config in invalid:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SystemExit):
                    load_config(self.write_config(folder, config))


class ProbeTests(unittest.TestCase):
    @patch("lantern.web.socket.create_connection")
    def test_reports_open_tcp_service(self, create_connection: Mock) -> None:
        create_connection.return_value.__enter__.return_value = object()
        result = probe("nas.local", 22, 0.5)
        self.assertTrue(result["up"])
        self.assertIsInstance(result["latency_ms"], float)
        self.assertIsNone(result["error"])

    @patch("lantern.web.socket.create_connection", side_effect=ConnectionRefusedError())
    def test_reports_unavailable_service(self, create_connection: Mock) -> None:
        result = probe("nas.local", 22, 0.2)
        self.assertFalse(result["up"])
        self.assertIsNone(result["latency_ms"])
        self.assertIsNotNone(result["error"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "history.db")
        initialize_database(Path(self.database))
        now = time.time()
        db = sqlite3.connect(self.database)
        try:
            db.executemany(
                "INSERT INTO checks(service_key, checked_at, is_up, latency_ms, error) VALUES (?, ?, ?, ?, ?)",
                [("nas.local:22", now - 120, 1, 12.0, None), ("nas.local:22", now - 60, 0, None, "TimeoutError")],
            )
            db.commit()
        finally:
            db.close()
        self.state = {
            "targets": [{"name": "NAS", "host": "nas.local", "ports": [22]}],
            "interval": 15.0,
            "database": self.database,
            "results": {"nas.local:22": {"up": False, "latency_ms": None, "error": "TimeoutError", "checked_at": now, "check_count": 7}},
            "check_counts": {"nas.local:22": 7},
            "update_condition": threading.Condition(),
            "update_revision": 1,
            "request_events": [],
            "request_count": 0,
            "request_lock": threading.Lock(),
            "ingest_token": "unit-test-token",
            "agent_events": deque(maxlen=250),
            "agent_request_count": 0,
            "server_network": {},
            "agent_lock": threading.Lock(),
            "last_check": now,
            "started_at": now - 100,
            "availability_threshold": 95.0,
            "latency_threshold": 1000.0,
        }
        self.old_state = DashboardHandler.state
        DashboardHandler.state = self.state

    def tearDown(self) -> None:
        DashboardHandler.state = self.old_state
        self.temp.cleanup()

    def test_status_includes_current_state_and_rolling_metrics(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        result = handler.payload()
        service = result["targets"][0]["services"][0]
        self.assertFalse(service["status"])
        self.assertEqual(service["availability_24h"], 50.0)
        self.assertEqual(service["avg_latency_24h"], 12.0)
        self.assertTrue(service["monitoring"])
        self.assertEqual(service["check_count"], 7)
        self.assertEqual(result["summary"]["services_total"], 1)
        self.assertEqual(result["checks_total"], 7)

    def test_status_reuses_recent_history_aggregates(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        real_connect = sqlite3.connect
        with patch("lantern.web.sqlite3.connect", wraps=real_connect) as connect:
            handler.payload()
            handler.payload()
        self.assertEqual(connect.call_count, 1)

    def test_history_returns_hourly_availability_bucket(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        result = handler.history("hours=24")
        points = result["services"]["nas.local:22"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["availability"], 50.0)
        self.assertEqual(points[0]["checks"], 2)

    def test_health_readiness_and_static_security_headers(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.state = self.state
        handler.client_address = ("127.0.0.1", 43210)
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.headers = {}
        handler.path = "/healthz"
        handler.send_json = Mock()
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.args[0]["status"], "ok")

        handler.path = "/readyz"
        self.state["last_check"] = None
        handler.send_json.reset_mock()
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 503)
        self.state["last_check"] = time.time()
        handler.send_json.reset_mock()
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 200)

        handler.path = "/"
        handler.send_response = Mock()
        sent_headers = []
        handler.send_header = lambda name, value: sent_headers.append((name, value))
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        handler.do_GET()
        self.assertIn("Content-Security-Policy", [name for name, _ in sent_headers])
        self.assertIn(b"See what", handler.wfile.getvalue())

        handler.path = "/missing"
        handler.send_error = Mock()
        handler.do_GET()
        self.assertEqual(handler.send_error.call_args.args[0], 404)

    def test_live_events_stream_current_status(self) -> None:
        class DisconnectAfterWrite(io.BytesIO):
            def flush(self) -> None:
                raise BrokenPipeError()

        handler = DashboardHandler.__new__(DashboardHandler)
        handler.state = self.state
        handler.client_address = ("127.0.0.1", 43211)
        handler.command = "GET"
        handler.request_version = "HTTP/1.1"
        handler.headers = {}
        handler.path = "/api/events"
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = DisconnectAfterWrite()
        handler.payload = Mock(return_value={"revision": 1, "status": "live"})
        handler.do_GET()
        self.assertEqual(handler.send_response.call_args.args[0], 200)
        self.assertIn("text/event-stream", str(handler.send_header.call_args_list))
        self.assertIn(b'data: {"revision":1,"status":"live"}', handler.wfile.getvalue())

    def test_records_client_ip_and_safe_request_metadata(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.state = self.state
        handler.accept_event({"type": "http_request", "server": "nas-web", "event": {
            "at": time.time(), "client_ip": "198.51.100.42", "client_port": 53124,
            "method": "GET", "path": "/api/status?secret=excluded", "status": 200,
            "bytes_in": 0, "bytes_out": 1024, "user_agent": "curl/8.7.1",
        }})
        event = self.state["agent_events"][0]
        self.assertEqual(event["client_ip"], "198.51.100.42")
        self.assertEqual(event["client_port"], 53124)
        self.assertEqual(event["server"], "nas-web")
        self.assertEqual(event["path"], "/api/status")
        self.assertEqual(event["user_agent"], "curl/8.7.1")
        self.assertNotIn("secret", event["path"])
        self.assertEqual(self.state["agent_request_count"], 1)

    def test_accepts_per_server_interface_traffic(self) -> None:
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.state = self.state
        handler.accept_event({"type": "interface_io", "server": "nas-web", "event": {
            "at": time.time(), "rx_bytes_total": 2048, "tx_bytes_total": 1024,
            "rx_bytes_sec": 512.0, "tx_bytes_sec": 128.0,
        }})
        snapshot = handler.payload()
        self.assertEqual(snapshot["server_network"]["nas-web"]["rx_bytes_sec"], 512.0)
        self.assertEqual(snapshot["server_network"]["nas-web"]["tx_bytes_total"], 1024)

    def test_ingest_requires_token_and_accepts_agent_request(self) -> None:
        body = json.dumps({"type": "http_request", "server": "web01", "event": {
            "client_ip": "198.51.100.10", "client_port": 53124, "method": "GET",
            "path": "/hello", "status": 200, "bytes_in": 0, "bytes_out": 10,
            "user_agent": "curl/8.7",
        }}).encode()
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = "/api/ingest"
        handler.state = self.state
        handler.headers = {"Authorization": "Bearer unit-test-token", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.send_json = Mock()
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], {"accepted": True})
        self.assertEqual(self.state["agent_request_count"], 1)

        handler.headers = {"Authorization": "Bearer wrong", "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.send_json.reset_mock()
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.kwargs["status"], 401)

    def test_normalizes_nginx_json_access_log_fields(self) -> None:
        event = normalize_access_line(json.dumps({
            "time_iso8601": "2026-10-28T12:00:00+00:00", "remote_addr": "203.0.113.8",
            "remote_port": "43210", "request_method": "GET", "uri": "/hello?from=nginx",
            "status": "200", "request_length": "72", "body_bytes_sent": "1024",
            "http_user_agent": "curl/8.7",
        }))
        self.assertEqual(event["client_ip"], "203.0.113.8")
        self.assertEqual(event["path"], "/hello")
        self.assertEqual(event["bytes_out"], 1024)


class PollerTests(unittest.TestCase):
    def test_poll_loop_checks_every_configured_port_and_persists_results(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "history.db"
            initialize_database(database)
            state = {
                "targets": [
                    {"name": "Router", "host": "192.0.2.1", "ports": [53, 443]},
                    {"name": "NAS", "host": "nas.local", "ports": [22]},
                ],
                "interval": 15.0,
                "timeout": 2.0,
                "retention": 30,
                "database": str(database),
                "results": {},
                "check_counts": {},
            }
            with patch("lantern.web.probe", return_value={"up": True, "latency_ms": 3.0, "error": None}) as probe_mock:
                with patch("lantern.__main__.time.sleep", side_effect=RuntimeError("stop after one cycle")):
                    with self.assertRaisesRegex(RuntimeError, "stop after one cycle"):
                        poll_forever(state)
            requested = {(call.args[0], call.args[1]) for call in probe_mock.call_args_list}
            self.assertEqual(requested, {("192.0.2.1", 53), ("192.0.2.1", 443), ("nas.local", 22)})
            self.assertEqual(state["check_counts"], {"192.0.2.1:53": 1, "192.0.2.1:443": 1, "nas.local:22": 1})
            db = sqlite3.connect(database)
            try:
                rows = db.execute("SELECT service_key, is_up FROM checks ORDER BY service_key").fetchall()
            finally:
                db.close()
            self.assertEqual(rows, [("192.0.2.1:443", 1), ("192.0.2.1:53", 1), ("nas.local:22", 1)])


if __name__ == "__main__":
    unittest.main()

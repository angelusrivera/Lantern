from __future__ import annotations

import json
import hmac
import ipaddress
import math
import sqlite3
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

METRICS_CACHE_SECONDS = 5.0


def probe(host: str, port: int, timeout: float) -> dict:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"up": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "error": None}
    except (OSError, OverflowError) as exc:
        return {"up": False, "latency_ms": None, "error": type(exc).__name__}


class DashboardHandler(BaseHTTPRequestHandler):
    state: dict = {}
    server_version = "Lantern/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/status":
            self.send_json(self.payload())
        elif route == "/healthz":
            self.send_json({"status": "ok", "version": "1.0.0"})
        elif route == "/readyz":
            ready = self.state.get("last_check") is not None
            self.send_json({"status": "ready" if ready else "starting", "last_check": self.state.get("last_check")}, status=200 if ready else 503)
        elif route == "/api/history":
            self.send_json(self.history(urlparse(self.path).query))
        elif route == "/api/events":
            self.stream_updates()
        elif route == "/":
            page = (Path(__file__).parent / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(page)
        else:
            self.send_error(404, "Not found")

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ingest":
            self.send_error(404, "Not found")
            return
        expected = self.state.get("ingest_token", "")
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            self.send_json({"error": "Ingestion is disabled or token is invalid"}, status=401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self.send_json({"error": "Content-Length must be between 1 and 65536"}, status=413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            result = self.accept_event(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            self.send_json({"error": str(exc)}, status=400)
            return
        if result is None:
            self.send_json({"error": "event type must be http_request or interface_io"}, status=400)
            return
        self.send_json({"accepted": True})

    def accept_event(self, payload: dict) -> str | None:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        event_type, server = payload.get("type"), payload.get("server")
        event = payload.get("event")
        if not isinstance(server, str) or not server.strip() or len(server) > 100 or not isinstance(event, dict):
            raise ValueError("event needs a server label and event object")
        server = server.strip()
        if event_type == "http_request":
            client_ip = str(ipaddress.ip_address(event.get("client_ip", "")))
            at = finite_number(event.get("at", time.time()), "at")
            client_port = bounded_int(event.get("client_port", 0), 0, 65535, "client_port")
            status = bounded_int(event.get("status", 200), 100, 599, "status")
            bytes_in = bounded_int(event.get("bytes_in", 0), 0, 2**63 - 1, "bytes_in")
            bytes_out = bounded_int(event.get("bytes_out", 0), 0, 2**63 - 1, "bytes_out")
            method = clean_text(event.get("method", "GET"), 16, "method")
            path = clean_text(event.get("path", "/"), 2048, "path")
            user_agent = clean_text(event.get("user_agent", "Unknown"), 256, "user_agent")
            record = {"server": server, "at": at, "client_ip": client_ip, "client_port": client_port,
                      "method": method, "path": urlparse(path).path, "status": status,
                      "bytes_in": bytes_in, "bytes_out": bytes_out, "user_agent": user_agent}
            with self.state["agent_lock"]:
                self.state["agent_events"].append(record)
                self.state["agent_request_count"] += 1
        elif event_type == "interface_io":
            at = finite_number(event.get("at", time.time()), "at")
            rx_rate = finite_number(event.get("rx_bytes_sec"), "rx_bytes_sec")
            tx_rate = finite_number(event.get("tx_bytes_sec"), "tx_bytes_sec")
            rx_total = bounded_int(event.get("rx_bytes_total"), 0, 2**63 - 1, "rx_bytes_total")
            tx_total = bounded_int(event.get("tx_bytes_total"), 0, 2**63 - 1, "tx_bytes_total")
            with self.state["agent_lock"]:
                server_state = self.state["server_network"].setdefault(server, {"samples": []})
                sample = {"at": at, "rx_bytes_sec": rx_rate, "tx_bytes_sec": tx_rate,
                          "rx_bytes_total": rx_total, "tx_bytes_total": tx_total}
                server_state.update(sample)
                server_state["samples"].append(sample)
                server_state["samples"][:] = server_state["samples"][-60:]
        else:
            return None
        condition = self.state.get("update_condition")
        if condition:
            with condition:
                self.state["update_revision"] = self.state.get("update_revision", 0) + 1
                condition.notify_all()
        return event_type

    def stream_updates(self) -> None:
        condition = self.state.get("update_condition")
        if condition is None:
            self.send_json({"error": "Live updates are unavailable"}, status=503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        last_revision = -1
        try:
            while True:
                with condition:
                    condition.wait_for(lambda: self.state.get("update_revision", 0) != last_revision, timeout=15)
                    revision = self.state.get("update_revision", 0)
                if revision == last_revision:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    payload = json.dumps(self.payload(), separators=(",", ":"), allow_nan=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    last_revision = revision
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def payload(self) -> dict:
        data = []
        results = self.state.get("results", {})
        cutoff = time.time() - 86400
        metrics = self._recent_metrics(cutoff)
        for target in self.state["targets"]:
            services = []
            for port in target["ports"]:
                result = results.get(f"{target['host']}:{port}")
                key = f"{target['host']}:{port}"
                count, successes, average = metrics.get(key, (0, 0, None))
                checked_at = result.get("checked_at") if result else None
                services.append({"port": port, "status": result["up"] if result else None,
                                 "latency_ms": result["latency_ms"] if result else None,
                                 "error": result["error"] if result else None,
                                 "last_checked": checked_at,
                                 "check_count": result.get("check_count", 0) if result else 0,
                                 "monitoring": bool(checked_at and time.time() - checked_at <= self.state["interval"] * 3),
                                 "availability_24h": round(successes * 100 / count, 2) if count else None,
                                 "avg_latency_24h": round(average, 1) if average is not None else None})
            data.append({"name": target["name"], "host": target["host"], "services": services})
        total = sum(len(item["services"]) for item in data)
        up = sum(s["status"] is True for item in data for s in item["services"])
        with self.state["agent_lock"]:
            requests = list(self.state["agent_events"])[-50:]
            requests_seen = self.state["agent_request_count"]
            network = {name: {**item, "samples": list(item["samples"])}
                       for name, item in self.state["server_network"].items()}
        return {"targets": data, "server_network": network,
                "checks_total": sum(self.state.get("check_counts", {}).values()),
                "recent_requests": requests,
                "request_summary": {"total_seen": requests_seen, "unique_ips": len({event["client_ip"] for event in requests})},
                "thresholds": {"availability_percent": self.state["availability_threshold"],
                "latency_ms": self.state["latency_threshold"]}, "summary": {"devices": len(data), "services_up": up,
                "services_total": total, "last_check": self.state.get("last_check"),
                "interval": self.state["interval"], "uptime_seconds": int(time.time()-self.state["started_at"]),
                "database": "connected"}}

    def _recent_metrics(self, cutoff: float) -> dict:
        """Reuse rolling aggregates across frequent live status updates."""
        lock = self.state.setdefault("metrics_cache_lock", threading.Lock())
        with lock:
            cached_metrics = self.state.get("metrics_cache")
            cached_at = self.state.get("metrics_cache_at", 0.0)
            if cached_metrics is not None and time.monotonic() - cached_at < METRICS_CACHE_SECONDS:
                return cached_metrics
            db = sqlite3.connect(self.state["database"], timeout=5)
            try:
                metrics = {key: (count, up_count, avg_latency) for key, count, up_count, avg_latency in db.execute(
                    "SELECT service_key, COUNT(*), SUM(is_up), AVG(CASE WHEN is_up=1 THEN latency_ms END) "
                    "FROM checks WHERE checked_at >= ? GROUP BY service_key", (cutoff,))}
            finally:
                db.close()
            self.state["metrics_cache"] = metrics
            self.state["metrics_cache_at"] = time.monotonic()
            return metrics

    def history(self, query: str) -> dict:
        from urllib.parse import parse_qs
        args = parse_qs(query)
        try:
            hours = int(args.get("hours", ["24"])[0])
        except ValueError:
            hours = 24
        hours = min(168, max(1, hours))
        cutoff = time.time() - hours * 3600
        db = sqlite3.connect(self.state["database"], timeout=5)
        try:
            bucket_seconds = 3600
            rows = db.execute("SELECT service_key, CAST((checked_at - ?) / ? AS INTEGER) AS bucket, "
                              "AVG(is_up) * 100, AVG(CASE WHEN is_up=1 THEN latency_ms END), COUNT(*) "
                              "FROM checks WHERE checked_at >= ? GROUP BY service_key, bucket "
                              "ORDER BY service_key, bucket", (cutoff, bucket_seconds, cutoff)).fetchall()
        finally:
            db.close()
        data = {}
        for key, bucket, availability, latency, count in rows:
            data.setdefault(key, []).append({"at": cutoff + bucket * bucket_seconds,
                "availability": round(availability, 2), "avg_latency_ms": round(latency, 1) if latency is not None else None,
                "checks": count})
        return {"hours": hours, "bucket_seconds": bucket_seconds, "services": data}

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Keep the console useful without logging request details from the LAN.
        return


def make_server(address: tuple[str, int], handler: type[DashboardHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(address, handler)
    server.daemon_threads = True
    return server


def finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return float(value)


def bounded_int(value: object, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def clean_text(value: object, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be a non-empty string of at most {limit} characters")
    return value

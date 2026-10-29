"""Ship server access-log events and interface counters to Lantern."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .network import read_interface_bytes


def normalize_access_line(line: str) -> dict:
    """Normalize Lantern JSON or common Nginx JSON access-log fields."""
    row = json.loads(line)
    if not isinstance(row, dict):
        raise ValueError("access-log record must be a JSON object")
    at = row.get("at", row.get("time_iso8601", time.time()))
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    event = {
        "at": float(at),
        "client_ip": row.get("client_ip", row.get("remote_addr", "")),
        "client_port": int(row.get("client_port", row.get("remote_port", 0)) or 0),
        "method": row.get("method", row.get("request_method", "GET")),
        "path": urlsplit(str(row.get("path", row.get("uri", "/")))).path,
        "status": int(row.get("status", 200)),
        "bytes_in": int(row.get("bytes_in", row.get("request_length", 0)) or 0),
        "bytes_out": int(row.get("bytes_out", row.get("body_bytes_sent", 0)) or 0),
        "user_agent": str(row.get("user_agent", row.get("http_user_agent", "Unknown"))),
    }
    return event


def post_event(collector: str, token: str, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(collector.rstrip("/") + "/api/ingest", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Lantern-Agent/1.0",
    })
    error = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=3) as response:
                if response.status >= 300:
                    raise RuntimeError(f"collector returned HTTP {response.status}")
            return
        except (OSError, URLError, RuntimeError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(f"could not send event to Lantern: {error}")


def follow_log(path: Path, collector: str, token: str, server_name: str) -> None:
    """Follow a JSON-lines access log, shipping new records as they are appended."""
    stream = None
    position = 0
    try:
        while True:
            if stream is None:
                stream = path.open("r", encoding="utf-8", errors="replace")
                stream.seek(0, 2)
                position = stream.tell()
                logging.info("Following access log %s from end", path)
            line = stream.readline()
            if line:
                position = stream.tell()
                try:
                    event = normalize_access_line(line)
                    post_event(collector, token, {"type": "http_request", "server": server_name, "event": event})
                except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
                    logging.warning("Skipping access-log record: %s", exc)
                continue
            try:
                if path.stat().st_size < position:
                    stream.close()
                    stream = None
            except OSError:
                stream.close()
                stream = None
            time.sleep(0.2)
    finally:
        if stream is not None:
            stream.close()


def sample_interface(collector: str, token: str, server_name: str, interval: float) -> None:
    previous = None
    previous_at = None
    while True:
        now = time.time()
        counters = read_interface_bytes()
        if counters is not None:
            rx_total, tx_total = counters
            elapsed = now - previous_at if previous_at is not None else 0
            rx_rate = max(0, rx_total - previous[0]) / elapsed if previous and elapsed else 0.0
            tx_rate = max(0, tx_total - previous[1]) / elapsed if previous and elapsed else 0.0
            event = {"type": "interface_io", "server": server_name, "event": {
                "at": now, "rx_bytes_total": rx_total, "tx_bytes_total": tx_total,
                "rx_bytes_sec": round(rx_rate, 1), "tx_bytes_sec": round(tx_rate, 1),
            }}
            try:
                post_event(collector, token, event)
            except RuntimeError as exc:
                logging.warning("Could not send interface counters: %s", exc)
            previous, previous_at = counters, now
        else:
            logging.warning("Network interface counters are unavailable on this host")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward a server's JSON access log and interface counters to Lantern.")
    parser.add_argument("--server-name", required=True, help="Label shown on the Lantern dashboard")
    parser.add_argument("--access-log", type=Path, help="JSON-lines access log to follow")
    parser.add_argument("--collector", default="http://127.0.0.1:8080", help="Lantern base URL")
    parser.add_argument("--interface-interval", type=float, default=2, help="Seconds between interface samples")
    args = parser.parse_args()
    token = os.environ.get("LANTERN_INGEST_TOKEN", "")
    if not token:
        parser.error("set LANTERN_INGEST_TOKEN before starting the agent")
    if args.interface_interval < 1 or args.interface_interval > 60:
        parser.error("--interface-interval must be between 1 and 60 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import threading
    threading.Thread(target=sample_interface, args=(args.collector, token, args.server_name, args.interface_interval), daemon=True).start()
    if args.access_log:
        follow_log(args.access_log, args.collector, token, args.server_name)
    else:
        logging.info("Sending interface counters only; press Ctrl+C to stop")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()

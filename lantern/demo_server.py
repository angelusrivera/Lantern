"""A tiny HTTP server that reports its real requests to Lantern."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .agent import post_event, sample_interface


def make_handler(server_name: str, collector: str, token: str) -> type[BaseHTTPRequestHandler]:
    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "LanternDemo/1.0"

        def do_GET(self) -> None:
            body = ("<!doctype html><html><body style='font:16px system-ui;max-width:680px;margin:4rem auto'>"
                    "<h1>Lantern demo web server</h1><p>This request was logged and forwarded to Lantern.</p>"
                    f"<p>Path: <code>{html.escape(urlsplit(self.path).path)}</code></p>"
                    "<p>Try <code>curl.exe http://localhost:8001/hello</code>.</p></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            event = {"type": "http_request", "server": server_name, "event": {
                "at": time.time(), "client_ip": self.client_address[0], "client_port": self.client_address[1],
                "method": self.command, "path": urlsplit(self.path).path, "status": 200,
                "bytes_in": int(self.headers.get("Content-Length", "0") or 0), "bytes_out": len(body),
                "user_agent": self.headers.get("User-Agent", "Unknown")[:256],
            }}
            try:
                post_event(collector, token, event)
            except RuntimeError as exc:
                logging.warning("Could not forward HTTP request to Lantern: %s", exc)

        def log_message(self, fmt: str, *args) -> None:
            return

    return DemoHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an instrumented demo web server that reports to Lantern.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--server-name", default="demo-web")
    parser.add_argument("--collector", default=os.environ.get("LANTERN_COLLECTOR_URL", "http://127.0.0.1:8080"))
    args = parser.parse_args()
    token = os.environ.get("LANTERN_INGEST_TOKEN", "")
    if not token:
        parser.error("set LANTERN_INGEST_TOKEN before starting the demo server")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    handler = make_handler(args.server_name, args.collector, token)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    threading.Thread(target=sample_interface, args=(args.collector, token, args.server_name, 2), daemon=True).start()
    logging.info("Demo web server listening at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping demo web server")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

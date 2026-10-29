from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from .web import DashboardHandler, make_server


def load_config(path: Path) -> tuple[list[dict], float, float, int, float, float]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit("Configuration root must be a JSON object")
    interval = config.get("poll_interval_seconds", 1)
    timeout = config.get("timeout_seconds", 2)
    retention = config.get("history_retention_days", 30)
    availability_threshold = config.get("availability_threshold_percent", 95)
    latency_threshold = config.get("latency_threshold_ms", 1000)
    targets = config.get("targets")
    if type(interval) not in (int, float) or not 1 <= interval <= 3600:
        raise SystemExit("poll_interval_seconds must be between 1 and 3600")
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 30:
        raise SystemExit("timeout_seconds must be between 0.1 and 30")
    if type(retention) is not int or not 1 <= retention <= 365:
        raise SystemExit("history_retention_days must be an integer between 1 and 365")
    if type(availability_threshold) not in (int, float) or not 0 <= availability_threshold <= 100:
        raise SystemExit("availability_threshold_percent must be between 0 and 100")
    if type(latency_threshold) not in (int, float) or not 1 <= latency_threshold <= 60000:
        raise SystemExit("latency_threshold_ms must be between 1 and 60000")
    if not isinstance(targets, list) or not targets:
        raise SystemExit("targets must be a non-empty list")
    normalized = []
    identities = set()
    service_identities = set()
    service_count = 0
    for item in targets:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip()
                or not isinstance(item.get("host"), str) or not item["host"].strip()):
            raise SystemExit("Each target needs string name and host fields")
        ports = item.get("ports")
        if not isinstance(ports, list) or not ports or any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != len(ports):
            raise SystemExit(f"Target {item['name']!r} needs ports between 1 and 65535")
        identity = (item["host"].strip().lower(), tuple(sorted(ports)))
        if identity in identities:
            raise SystemExit(f"Duplicate target definition for {item['host']!r}")
        identities.add(identity)
        for port in ports:
            service_identity = (item["host"].strip().lower(), port)
            if service_identity in service_identities:
                raise SystemExit(f"Service {item['host']}:{port} is configured more than once")
            service_identities.add(service_identity)
        service_count += len(ports)
        normalized.append({"name": item["name"].strip(), "host": item["host"].strip(), "ports": ports})
    if service_count > 64:
        raise SystemExit("At most 64 configured services are supported")
    return normalized, float(interval), float(timeout), retention, float(availability_threshold), float(latency_threshold)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor configured TCP services on your home lab.")
    parser.add_argument("--config", type=Path, help="JSON config (defaults to config.json, then config.example.json)")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", type=Path, default=Path("lantern.db"), help="SQLite history database")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    config_path = args.config
    if config_path is None:
        config_path = next((candidate for candidate in (Path("config.json"), Path("config.example.json")) if candidate.is_file()), Path("config.json"))
    targets, interval, timeout, retention, availability_threshold, latency_threshold = load_config(config_path)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    initialize_database(args.database)
    state = {"targets": targets, "interval": interval, "timeout": timeout,
             "retention": retention, "availability_threshold": availability_threshold,
             "latency_threshold": latency_threshold, "database": str(args.database), "results": {},
             "check_counts": {}, "last_check": None, "ingest_token": os.environ.get("LANTERN_INGEST_TOKEN", ""),
             "agent_events": deque(maxlen=250), "agent_request_count": 0, "server_network": {},
             "agent_lock": threading.Lock(), "update_condition": threading.Condition(),
             "update_revision": 0, "started_at": time.time()}
    DashboardHandler.state = state
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        server = make_server((args.host, args.port), DashboardHandler)
    except OSError as exc:
        raise SystemExit(f"Could not start Lantern on {args.host}:{args.port}: {exc}. Try another port with --port.") from exc
    worker = threading.Thread(target=poll_forever, args=(state,), daemon=True)
    worker.start()
    logging.info("Lantern using config %s", config_path)
    logging.info("Lantern listening at http://%s:%d (SQLite history: %s)", args.host, args.port, args.database)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping Lantern")
    finally:
        server.server_close()


def poll_forever(state: dict) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .web import probe

    checks = [(target, port) for target in state["targets"] for port in target["ports"]]
    last_saved = {}
    last_pruned = 0.0
    with ThreadPoolExecutor(max_workers=min(32, len(checks))) as pool:
        while True:
            started = time.monotonic()
            futures = {pool.submit(probe, target["host"], port, state["timeout"]): (target, port) for target, port in checks}
            snapshot = {}
            rows = []
            for future in as_completed(futures):
                checked_at = time.time()
                target, port = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"up": False, "latency_ms": None, "error": type(exc).__name__}
                key = f"{target['host']}:{port}"
                count = state["check_counts"].get(key, 0) + 1
                state["check_counts"][key] = count
                result["checked_at"] = checked_at
                result["check_count"] = count
                snapshot[key] = result
                previous = state.get("results", {}).get(key)
                changed = previous is not None and previous["up"] != result["up"]
                if key not in last_saved or checked_at - last_saved[key] >= 60 or changed:
                    rows.append((key, checked_at, int(result["up"]), result["latency_ms"], result["error"]))
                    last_saved[key] = checked_at
            if rows or checked_at - last_pruned >= 3600:
                db = sqlite3.connect(state["database"], timeout=10)
                try:
                    db.execute("PRAGMA busy_timeout=10000")
                    db.executemany("INSERT INTO checks(service_key, checked_at, is_up, latency_ms, error) VALUES (?, ?, ?, ?, ?)", rows)
                    if checked_at - last_pruned >= 3600:
                        cutoff = checked_at - state["retention"] * 86400
                        db.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
                        last_pruned = checked_at
                    db.commit()
                finally:
                    db.close()
            state["results"] = snapshot
            state["last_check"] = checked_at
            notify_updates(state)
            time.sleep(max(0, state["interval"] - (time.monotonic() - started)))


def notify_updates(state: dict) -> None:
    condition = state.get("update_condition")
    if condition is None:
        return
    with condition:
        state["update_revision"] = state.get("update_revision", 0) + 1
        condition.notify_all()


def initialize_database(path: Path) -> None:
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("""CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY,
            service_key TEXT NOT NULL,
            checked_at REAL NOT NULL,
            is_up INTEGER NOT NULL CHECK (is_up IN (0, 1)),
            latency_ms REAL,
            error TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_checks_service_time ON checks(service_key, checked_at)")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()

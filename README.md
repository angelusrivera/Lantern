# Lantern

I built Lantern to keep an eye on the services in my home lab. It checks the TCP ports I configure, saves check history in SQLite, and shows status, response time, and availability on a local dashboard. The app uses Python's standard library, so there are no runtime packages to install.

Lantern only checks the hosts and ports in `config.json`. It doesn't scan the network or find devices on its own.

## Quick start

You'll need Python 3.10 or newer. These commands work in WSL, Linux, and macOS. I've included PowerShell equivalents where they differ.

```bash
cp config.example.json config.json
python3 -m lantern --config config.json
```

Open [http://localhost:8080](http://localhost:8080). The example checks Lantern itself on port 8080, so there should be one working service on the dashboard. Add your own hosts and ports to `config.json`. In PowerShell, run `Copy-Item config.example.json config.json`, then `py -m lantern --config config.json`.

The local `config.json`, `.env`, access logs, and SQLite database are ignored by Git. To select a custom database location:

```bash
python3 -m lantern --config config.json --database data/lantern.db
```

### Docker Compose

```bash
cp config.example.json config.json  # once, from the repository root
docker compose up --build -d
```

Open [http://localhost:8080](http://localhost:8080). Compose saves the SQLite database in a named volume. If you change `config.json`, restart the container with `docker compose up -d --force-recreate`.

On Docker Desktop for Windows, a host service is reached from the container using `host.docker.internal`. For example, if your web server runs on Windows at port 8000, configure its host as `host.docker.internal` instead of `127.0.0.1` (which refers to the Lantern container itself). The web server must accept connections on a host interface the container can reach.

### See monitored-server requests and traffic

The port checks won't show requests made to another server. To try request logging and interface traffic, start the included demo web server. Add this target to the `targets` list in `config.json` so Lantern also checks that server:

```json
{"name": "Demo web", "host": "demo-web", "ports": [8000]}
```

Create a local collector token and start the demo profile:

```bash
test -f .env || printf 'LANTERN_INGEST_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" > .env
docker compose --profile demo up --build -d
```

Send a few requests to the test server:

```bash
for i in $(seq 1 5); do curl -fsS "http://localhost:8001/test/$i" >/dev/null; done
```

The dashboard's **HTTP requests on monitored servers** table shows the client address as the demo server sees it, along with the path, response code, User-Agent, and byte counts. **Network traffic by server** shows the demo container's interface counters. With Docker Desktop, the client address may be a gateway address because Docker forwards the connection.

Use `--host 0.0.0.0` to view it from another device on your LAN. The dashboard has no authentication or TLS: keep it on a trusted network and do not expose it directly to the public internet.

## Configuration

```json
{
  "poll_interval_seconds": 1,
  "timeout_seconds": 2,
  "history_retention_days": 30,
  "availability_threshold_percent": 95,
  "latency_threshold_ms": 1000,
  "targets": [
    {"name": "NAS", "host": "192.168.1.20", "ports": [22, 445, 5000]},
    {"name": "Home Assistant", "host": "homeassistant.local", "ports": [8123]},
    {"name": "DNS resolver", "host": "192.168.1.1", "ports": [53, 80]}
  ]
}
```

- `poll_interval_seconds`: 1–3600 seconds (default 1). Dashboard status is pushed as soon as each probe cycle completes.
- `timeout_seconds`: 0.1–30 seconds per TCP connection (default 2).
- `history_retention_days`: 1–365 days (default 30).
- `availability_threshold_percent`: flag a target as degraded below this 24-hour availability (default 95%).
- `latency_threshold_ms`: flag a target as degraded above this 24-hour average latency (default 1000 ms).
- Up to 64 services can be configured. Ports must be unique per host.

Each check only tries to open a TCP connection. It doesn't send an HTTP request or confirm that the application behind the port is working. Checks run concurrently, with a maximum of 32 workers. Lantern saves a history sample once a minute and when a service changes state. New installs start with less than 24 hours of history, so the availability figure fills in over time. Old samples are removed based on `history_retention_days`.

## Dashboard and API

- `/` — responsive live dashboard with device state, per-service latency, 24-hour availability, and 24-hour/7-day availability trends.
- `/api/status` — current status and 24-hour availability/latency aggregates as JSON.
- `/api/ingest` — authenticated endpoint for server agents to report HTTP access-log events and interface counters.
- `/api/status` includes the last 50 ingested web-server requests and per-server network rates.
- `/api/history?hours=24` — hourly service availability and latency buckets. `hours` is bounded to 1–168.
- `/healthz` — process liveness endpoint.
- `/readyz` — readiness endpoint; returns HTTP 503 until the first probe cycle completes.
- `/api/status` also reports each port's most recent check time and cumulative probe count.

The page gets updates through Server-Sent Events and falls back to one-second polling if that connection drops. The agent samples interface counters every two seconds by default. Probe history survives restarts; recent request events and traffic samples are kept in memory and reset when Lantern restarts.

The dashboard refreshes its cached availability and latency aggregates every five seconds to avoid querying the full history table on every live update. Current probe results and incoming agent events still update as they arrive.

The TCP check only tells you whether a port accepts connections. For HTTP requests, run the agent on the web server and point it at the server's JSON-lines access log. The agent also reports interface counters from the machine where it runs. In a container, those are container counters; the numbers are total bytes in and out, not packet captures or per-connection details.

### Connect a real Nginx server

Configure Nginx to write JSON-lines access logs. For example:

```nginx
log_format lantern escape=json '{"time_iso8601":"$time_iso8601","remote_addr":"$remote_addr","remote_port":"$remote_port","request_method":"$request_method","uri":"$uri","status":$status,"request_length":$request_length,"body_bytes_sent":$body_bytes_sent,"http_user_agent":"$http_user_agent"}';
access_log /var/log/nginx/lantern-access.json lantern;
```

On the server, install/run Lantern's standard-library agent from this project checkout (or copy the package there):

```bash
export LANTERN_INGEST_TOKEN='the-same-token-configured-for-Lantern'
python3 -m lantern.agent --server-name nginx-web --access-log /var/log/nginx/lantern-access.json --collector http://<lantern-host>:8080
```

The agent follows newly appended log entries and sends each request event immediately. It maps common Nginx JSON fields into client IP/port, method, URI path, status, request/response bytes, and User-Agent. Query strings and request bodies are not forwarded. The same agent reports the server host's interface counters every two seconds. Set `LANTERN_INGEST_TOKEN` for the Lantern process (or Compose `.env`) and on every agent; ingestion is disabled without the token. Use a trusted network or HTTPS when sending events across networks.

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

In PowerShell, run `py -m unittest discover -s tests -v`.

## A few things to know

- Lantern doesn't scan for devices; I add the hosts and ports I want it to check.
- A failed check is recorded for that service and doesn't stop the other checks.
- Probe history is stored in SQLite. Request and interface data from the agent is kept in memory.
- The collector needs `LANTERN_INGEST_TOKEN`. The dashboard itself has no login, so I keep it on my local network.
- The dashboard binds to `127.0.0.1` by default. Use `--host 0.0.0.0` only when you want other devices on your LAN to reach it.

## Project structure

```text
lantern/
├── lantern/
│   ├── __init__.py
│   ├── __main__.py
│   ├── agent.py
│   ├── demo_server.py
│   ├── network.py
│   ├── web.py
│   └── index.html
├── config.example.json
├── docs/
│   ├── architecture.md
│   └── server-agent.md
├── tests/
│   └── test_lantern.py
├── pyproject.toml
├── CONTRIBUTING.md
├── Dockerfile
├── compose.yaml
├── .dockerignore
├── .gitignore
├── LICENSE
└── README.md
```

I wrote a little more about how the pieces fit together in [Architecture](docs/architecture.md). See [Server agent setup](docs/server-agent.md) to connect an existing server, or [Working on Lantern](CONTRIBUTING.md) if you want to change the code.

## Resume bullet

Built **Lantern**, a Python home lab monitor with concurrent TCP checks, SQLite history, a live dashboard, and an optional agent for web access logs and interface traffic. Packaged it with Docker Compose and added health checks and configurable availability thresholds.

## License

MIT. See [LICENSE](LICENSE).

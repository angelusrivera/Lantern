# Adding a web server

Lantern's TCP checks tell me whether a port accepts a connection. To see HTTP requests made to a server, I run the Lantern agent on that server and have it follow the server's access log. The agent also reports the machine's network interface counters.

## Nginx access log

Nginx needs to write one JSON object per line. Add a log format like this to its config:

```nginx
log_format lantern escape=json '{"time_iso8601":"$time_iso8601","remote_addr":"$remote_addr","remote_port":"$remote_port","request_method":"$request_method","uri":"$uri","status":$status,"request_length":$request_length,"body_bytes_sent":$body_bytes_sent,"http_user_agent":"$http_user_agent"}';
access_log /var/log/nginx/lantern-access.json lantern;
```

The agent can also read Lantern's normalized fields: `at`, `client_ip`, `client_port`, `method`, `path`, `status`, `bytes_in`, `bytes_out`, and `user_agent`.

## Install and run

On the machine with the access log, clone or copy this project and install it:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Set the same token that you gave Lantern, then start the agent. Replace the collector address with one this server can reach:

```bash
export LANTERN_INGEST_TOKEN='same-token-as-Lantern'
lantern-agent \
  --server-name nginx-web \
  --access-log /var/log/nginx/lantern-access.json \
  --collector http://192.168.1.20:8080
```

The agent starts at the end of the log and forwards new requests as they arrive; it doesn't replay the old log. Interface counters are sampled every two seconds. You can change that with `--interface-interval` (from 1 to 60 seconds).

On Windows PowerShell, set the token with `$env:LANTERN_INGEST_TOKEN = 'same-token-as-Lantern'` and start it with `py -m lantern.agent` followed by the same options. Linux reads counters from `/proc/net/dev`; Windows uses `Get-NetAdapterStatistics`.

The agent sends the token with each event. Keep the collector on a network you trust, or use HTTPS through a reverse proxy.

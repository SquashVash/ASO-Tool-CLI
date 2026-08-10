# Deploying on Ubuntu

Target: a 4GB / 2 vCPU box (`ubuntu-4gb-nbg1-2`). One uvicorn worker is ~100MB
and the data files are under a megabyte, so neither memory nor disk is a
constraint. The response cache is in-process and bounded by its TTLs, so the
footprint stays flat across a long-running server.

## Install

```bash
sudo useradd --system --home /opt/aso --shell /usr/sbin/nologin aso
sudo mkdir -p /opt/aso && sudo chown aso:aso /opt/aso
sudo -u aso git clone <repo> /opt/aso
cd /opt/aso && sudo -u aso uv sync
```

## Credentials and data

`.env` and the ASA key pair are not in git and must be copied up separately.
Both are secrets:

```bash
scp .env asa-private-key.pem root@<host>:/tmp/
sudo install -o aso -g aso -m 600 /tmp/.env /opt/aso/.env
sudo install -o aso -g aso -m 600 /tmp/asa-private-key.pem /opt/aso/
sudo rm /tmp/.env /tmp/asa-private-key.pem
```

`data/` is in git — the fitted bridges, the measured observations and the
frozen calibration corpus all ship with the checkout, so a fresh clone scores
identically to your laptop with no copying.

The one file worth copying is your own keyword list, which starts empty:

```bash
scp data/keywords.json root@<host>:/tmp/keywords.json
sudo install -o aso -g aso -m 644 /tmp/keywords.json /opt/aso/data/keywords.json
sudo rm /tmp/keywords.json
```

Stop the API before replacing it. A refresh writes the whole file at once, so
overwriting it under a running job would lose that job's results.

## Units

```bash
sudo cp deploy/aso-api.service deploy/aso-refresh.service deploy/aso-refresh.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aso-api.service aso-refresh.timer
curl -s localhost:8081/health | jq
journalctl -u aso-api -f
```

### The nightly refresh unit

`aso-refresh.service` POSTs to the API over loopback, so it has to know the
port. It reads `ASO_API_PORT` from the same `/opt/aso/.env` the API does,
defaulting to 8081 — change the port in `.env` and both follow. If you ever
move `.env`, both units' `EnvironmentFile=` lines have to move with it, or the
nightly refresh will quietly POST at the old port.

`curl -f` means a non-2xx response marks the unit failed, which is intended:
the two expected non-2xx are worth an email. A 409 means a refresh was already
running when the timer fired (a manual one that overran, most likely) and
nothing was started. A 422 means the filter matched no keywords. Neither is a
crash; check `journalctl -u aso-refresh` before treating it as one.

## The optional browser extra

Only `POST /popularity/pull` needs it, and it adds ~400MB plus apt
dependencies. Until you opt in with `ASO_APPLE_POPULARITY_ENABLED=true`, that
route returns 503 with an explanation, so this blocks nothing. With the flag on
but the extra missing, the pull starts and the job fails with a message naming
the install command:

```bash
cd /opt/aso && sudo -u aso uv sync --extra browser
sudo -u aso uv run playwright install --with-deps chromium
```

## Do not run the CLI against Apple while the API is up

`aso list`, `aso show`, and `aso rescore` are safe — they touch no network.
`aso refresh`, `aso check`, `aso asa pull` and `aso apple pull` are not: each
opens its own token bucket, and two buckets on one IP is roughly 30 req/min
against a limit that starts refusing around 20. Use the API for those.

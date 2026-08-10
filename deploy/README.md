# Deploying on Ubuntu

Target: a 4GB / 2 vCPU box (`ubuntu-4gb-nbg1-2`). One uvicorn worker is ~100MB
and SQLite's page cache is modest, so memory is not the constraint. **Disk is**
— `aso.db` is around 500MB and grows with every refresh, plus its WAL.

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

Copy the database up rather than starting cold — it holds the fitted
calibration and all your history:

```bash
scp aso.db root@<host>:/tmp/aso.db
sudo install -o aso -g aso -m 644 /tmp/aso.db /opt/aso/aso.db && sudo rm /tmp/aso.db
```

Stop the API before replacing `aso.db`, and copy the `-wal` and `-shm` files
alongside it if they exist — or checkpoint first with
`sqlite3 aso.db 'PRAGMA wal_checkpoint(TRUNCATE);'`.

## Units

```bash
sudo cp deploy/aso-api.service deploy/aso-refresh.service deploy/aso-refresh.timer \
    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aso-api.service aso-refresh.timer
curl -s localhost:8081/health | jq
journalctl -u aso-api -f
```

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

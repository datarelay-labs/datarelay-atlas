# Atlas MCP service runtime

Install the existing authenticated MCP process as a non-root systemd service.
This runbook does not deploy `prod-atlas`, provision public DNS, complete
ChatGPT OAuth, or take a consistent backup. Issue #41 stays HUMAN_REQUIRED.
Consistent backup and restore verification are ADR-0010
(`docs/runbooks/phase2-data-protection.md`). Upgrade and rollback remain
Issue #43. The directory copy in `.engineering/project.yaml` `backup_command`
is not an online-consistent backup.

## Layout

| Path | Purpose | Ownership |
| --- | --- | --- |
| `/opt/datarelay-atlas` | Git checkout and virtualenv | `atlas:atlas` |
| `/etc/datarelay-atlas/service.env` | Service configuration, outside Git | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/introspection-client-secret` | Introspection client secret | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/tls/key.pem` | TLS private key | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/tls/cert.pem` | TLS certificate | not world-writable |
| `/var/lib/datarelay-atlas` | `ATLAS_DATA_ROOT` | `atlas:atlas`, mode `0750` |

`deploy/datarelay-atlas.service.env.example` lists the supported keys. Unknown
keys, a world-accessible env file or private key, and a partial semantic
configuration are not ready. Do not put `GITHUB_TOKEN` in the service env
file. `atlas sync` reads `GITHUB_TOKEN` from the operator shell.

TLS terminates in the MCP process. A proxy may forward TCP to
`127.0.0.1:8443`. The process does not bind cleartext and does not issue
tokens.

## Install

Run on the staging host. Do not point these commands at `prod-atlas`.
Create the `atlas` group and user before any `install` command that assigns
`atlas` ownership.

```bash
sudo groupadd --system atlas
sudo useradd --system --gid atlas --home /var/lib/datarelay-atlas --shell /usr/sbin/nologin atlas
sudo install -d -o root -g atlas -m 0750 /etc/datarelay-atlas /etc/datarelay-atlas/tls
sudo install -d -o atlas -g atlas -m 0750 /var/lib/datarelay-atlas
sudo install -d -o atlas -g atlas -m 0755 /opt/datarelay-atlas
sudo rsync -a --exclude .venv ./ /opt/datarelay-atlas/
sudo -u atlas python3 -m venv /opt/datarelay-atlas/.venv
sudo -u atlas /opt/datarelay-atlas/.venv/bin/pip install -r /opt/datarelay-atlas/requirements.txt
sudo install -m 0640 -o root -g atlas /path/outside/git/service.env /etc/datarelay-atlas/service.env
sudo install -m 0640 -o root -g atlas /path/outside/git/introspection-client-secret /etc/datarelay-atlas/introspection-client-secret
sudo install -m 0640 -o root -g atlas /path/outside/git/key.pem /etc/datarelay-atlas/tls/key.pem
sudo install -m 0644 -o root -g atlas /path/outside/git/cert.pem /etc/datarelay-atlas/tls/cert.pem
PYTHONPATH=. python3 -m atlas ops stage --dest /tmp/atlas-unit-stage
sudo install -m 0644 /tmp/atlas-unit-stage/datarelay-atlas.service /etc/systemd/system/datarelay-atlas.service
sudo systemctl daemon-reload
sudo systemctl enable --now datarelay-atlas.service
```

`ops stage` copies the unit into the destination directory. It does not call
`systemctl` and does not need root. Enable the unit only after `ops check`
reports ready. The unit's `ExecStartPre` runs that same check before every
start, so a world-accessible env file, secret, or TLS key, or an unknown or
conflicting setting, does not reach `mcp serve`.

## Config check

The installed env, introspection secret, and TLS key are `root:atlas` mode
`0640`. A normal operator account cannot read them. Run the check as `atlas`
with the installed interpreter:

```bash
sudo --user atlas --group atlas \
  env PYTHONPATH=/opt/datarelay-atlas \
  /opt/datarelay-atlas/.venv/bin/python -m atlas ops check \
  --env-file /etc/datarelay-atlas/service.env
```

Exit 0 prints `"status": "ready"`. Exit 1 lists missing setting names and
invalid codes. The report does not contain secret values, registry documents,
or projection text. The command reads that file only, not the ambient shell.

## Lifecycle

```bash
sudo systemctl start datarelay-atlas.service
sudo systemctl stop datarelay-atlas.service
sudo systemctl restart datarelay-atlas.service
sudo systemctl status datarelay-atlas.service
sudo journalctl -u datarelay-atlas.service -n 100 --no-pager
```

Boot persistence is `WantedBy=multi-user.target`. The process user is `atlas`.

## Health

Configuration readiness is `ops check`. Runtime readiness is HTTPS
`GET /healthz` on the serving process:

```bash
curl --silent --show-error --fail --cacert /etc/datarelay-atlas/tls/cert.pem \
  https://127.0.0.1:8443/healthz
```

A ready process responds `{"status":"ready"}`. If the data root is missing, or
`registry.json` or `projections/projections.json` is present but unreadable or
unsupported, or an indexable projection record is malformed, missing its
document, or digest-mismatched, the response is HTTP 503
`{"status":"not_ready"}`. Neither body includes projects, paths, tokens, or
error detail. `/healthz` does not require a bearer token. MCP tools still
require `atlas.read`.

Stop the service before changing the env file or TLS key, then check config
again and start it. This runbook does not restore or roll back durable state.

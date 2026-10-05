# Atlas MCP service runtime

Install the authenticated MCP process and the read-only Human UI as non-root
systemd services on hostname `prod-atlas`. The public MCP name is
`mcp.atlas.datarelay.run`. The initial production Human UI remains loopback-only
on `127.0.0.1:8788` and is reached through authenticated SSH local forwarding;
a public `app.atlas.datarelay.run` surface is not required. This runbook does
not provision DNS, complete ChatGPT OAuth, or flip `production_oriented`. Issue #41 stays blocked and is not a launch gate.
Consistent backup, restore verification, upgrade, and rollback are ADR-0010
and ADR-0011 (`docs/runbooks/phase2-data-protection.md`). The operations
profile commands are those commands.

`python -m atlas ops prod-contract` prints the secret-free launch contract.
It does not contact the host. `production_evidence` stays false until a later
slice records real service, health, and restart output from `prod-atlas`.

## Layout

| Path | Purpose | Ownership |
| --- | --- | --- |
| `/opt/datarelay-atlas` | Deployed tracked source tree and virtualenv | `atlas:atlas` |
| `/etc/datarelay-atlas/service.env` | Service configuration, outside Git | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/introspection-client-secret` | Introspection client secret | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/tls/key.pem` | TLS private key | `root:atlas`, mode `0640` |
| `/etc/datarelay-atlas/tls/cert.pem` | TLS certificate | not world-writable |
| `/var/lib/datarelay-atlas` | `ATLAS_DATA_ROOT` | `atlas:atlas`, mode `0750` |

`deploy/datarelay-atlas.service.env.example` is the prod deployment env.
Its resource URL is `https://mcp.atlas.datarelay.run/mcp`. Bind stays
`127.0.0.1:8443`. A loopback audience is rejected for that file. Generic
`ops check` still accepts other resource URLs. Unknown
keys, a world-accessible env file or private key, and a partial semantic
configuration are not ready. Do not put `GITHUB_TOKEN` in the service env
file. `atlas sync` reads `GITHUB_TOKEN` from the operator shell.

TLS terminates in the MCP process. Production ingress is not optional.
`datarelay-atlas-ingress.socket` listens on `0.0.0.0:443`.
`systemd-socket-proxyd` forwards that TCP stream to `127.0.0.1:8443`.
The proxy unit is `DynamicUser=yes`, has no TLS file paths, and does not run
as root. The MCP process still binds only `127.0.0.1:8443` and does not
receive `CAP_NET_BIND_SERVICE`. The process does not bind cleartext and does
not issue tokens.

## Install

Run on hostname `prod-atlas` from an exact Git checkout of the candidate.
Create the `atlas` group and user before any `install` command that assigns
`atlas` ownership. Build the deployed source from `git archive` rather than
copying the operator working tree: ignored/untracked provider workspace files,
absolute symlinks, and local agent state must never enter `/opt/datarelay-atlas`.
The sync uses `--delete` so a previously deployed stale workspace artifact is
removed while the excluded deployed `.venv` is retained. The tracked archive
must not contain `.cursor`, `.cursorignore`, or `.cursorrules`; fail before the
sync if any appears. Copy secrets from outside Git. Do not commit `service.env`,
the introspection secret, or `key.pem`. The public resource URL is
`https://mcp.atlas.datarelay.run/mcp`. The process still binds `127.0.0.1:8443`.

```bash
sudo groupadd --system atlas
sudo useradd --system --gid atlas --home /var/lib/datarelay-atlas --shell /usr/sbin/nologin atlas
sudo install -d -o root -g atlas -m 0750 /etc/datarelay-atlas /etc/datarelay-atlas/tls
sudo install -d -o atlas -g atlas -m 0750 /var/lib/datarelay-atlas
sudo install -d -o atlas -g atlas -m 0755 /opt/datarelay-atlas
deploy_src="$(mktemp -d)"
trap 'rm -rf "$deploy_src"' EXIT
deploy_head="$(git rev-parse --verify 'HEAD^{commit}')"
git archive --format=tar "$deploy_head" | tar -xf - -C "$deploy_src"
for forbidden in .cursor .cursorignore .cursorrules; do
  if [ -e "$deploy_src/$forbidden" ] || [ -L "$deploy_src/$forbidden" ]; then
    echo "refusing tracked provider workspace artifact: $forbidden" >&2
    exit 1
  fi
done
sudo rsync -a --delete --exclude .venv "$deploy_src/" /opt/datarelay-atlas/
sudo -u atlas python3 -m venv /opt/datarelay-atlas/.venv
sudo -u atlas /opt/datarelay-atlas/.venv/bin/pip install --no-cache-dir -r /opt/datarelay-atlas/requirements.txt
sudo install -m 0640 -o root -g atlas /path/outside/git/service.env /etc/datarelay-atlas/service.env
sudo install -m 0640 -o root -g atlas /path/outside/git/introspection-client-secret /etc/datarelay-atlas/introspection-client-secret
sudo install -m 0640 -o root -g atlas /path/outside/git/key.pem /etc/datarelay-atlas/tls/key.pem
sudo install -m 0644 -o root -g atlas /path/outside/git/cert.pem /etc/datarelay-atlas/tls/cert.pem
PYTHONPATH=. python3 -m atlas ops stage --dest /tmp/atlas-unit-stage
sudo install -m 0644 /tmp/atlas-unit-stage/datarelay-atlas.service /etc/systemd/system/datarelay-atlas.service
sudo install -m 0644 /tmp/atlas-unit-stage/datarelay-atlas-web.service /etc/systemd/system/datarelay-atlas-web.service
sudo install -m 0644 /tmp/atlas-unit-stage/datarelay-atlas-ingress.socket /etc/systemd/system/datarelay-atlas-ingress.socket
sudo install -m 0644 /tmp/atlas-unit-stage/datarelay-atlas-ingress.service /etc/systemd/system/datarelay-atlas-ingress.service
sudo systemctl daemon-reload
sudo --user atlas --group atlas \
  env PYTHONPATH=/opt/datarelay-atlas \
  /opt/datarelay-atlas/.venv/bin/python -m atlas ops check --prod \
  --env-file /etc/datarelay-atlas/service.env
sudo systemctl enable --now datarelay-atlas.service
sudo systemctl enable --now datarelay-atlas-web.service
sudo systemctl enable --now datarelay-atlas-ingress.socket
```

`ops stage` copies the service unit and the port-443 ingress units. It does
not call `systemctl` and does not need root. Enable them only after
`ops check --prod` reports ready. The unit's `ExecStartPre` runs that same
prod check before every start, so a loopback resource URL, a world-accessible
env file, secret, or TLS key, or an unknown or conflicting setting, does not
reach `mcp serve`. Generic `ops check` without `--prod` remains the
non-production health command.

## Config check

The installed env, introspection secret, and TLS key are `root:atlas` mode
`0640`. A normal operator account cannot read them. Run the check as `atlas`
with the installed interpreter:

```bash
sudo --user atlas --group atlas \
  env PYTHONPATH=/opt/datarelay-atlas \
  /opt/datarelay-atlas/.venv/bin/python -m atlas ops check --prod \
  --env-file /etc/datarelay-atlas/service.env
```

Exit 0 prints `"status": "ready"`. Exit 1 lists missing setting names and
invalid codes. The report does not contain secret values, registry documents,
or projection text. The command reads that file only, not the ambient shell.

## Production context freshness

Canonical source projections and GitHub lifecycle cache are derived state, not
authority. Keep them fresh without placing GitHub credentials in the production
service environment. The scheduled refresh runs on an already-authenticated
operator host and discovers the complete enabled GitHub-backed target set from
the live production registry on every run. Do **not** maintain a separate
hard-coded repository allowlist for the timer.

The canonical operator artifacts are:

The canonical operator script still builds the content-free evidence with `python -m atlas usage github-snapshot`; target repositories are supplied from live registry discovery rather than a hand-maintained list.

- `scripts/prod-context-refresh.py` — discovers the current production
  GitHub-backed repositories/projects, builds the bounded lifecycle snapshot on
  the authenticated operator host, fetches every registered GitHub source on
  that operator host, publishes lifecycle state, revalidates the target/source
  identity set, and streams one bounded credential-free source payload into the
  existing production `AtlasService.sync_project(..., fetch=custom_fetch)`
  projection path.
- `deploy/systemd/atlas-prod-refresh.service` and
  `deploy/systemd/atlas-prod-refresh.timer` — run that installed script every
  15 minutes from the operator host.
- `scripts/install-prod-context-refresh-systemd.sh` — root installer. It also
  installs the exact candidate `atlas/` tree from `git archive HEAD` under
  `/usr/local/lib/datarelay-atlas/operator-src`, root-owned and non-writable by
  the service user, so scheduled imports do not execute a mutable developer
  checkout. It saves the previous script/unit/runtime files under
  `/var/backups/datarelay-atlas-operator/prod-refresh/<timestamp-pid>/`
  before replacement and prints the exact rollback directory.

Before installation or a manual production refresh, inspect the read-only plan:

```bash
python3 scripts/prod-context-refresh.py --print-plan
```

The plan must include every enabled project with at least one enabled GitHub
source and each unique corresponding repository. Personal/local-markdown-only
projects are intentionally excluded. Empty, invalid, duplicate, or oversized
target sets fail closed. Snapshot publication independently requires the same
complete registered GitHub repository set, and the script re-reads the registry
before sync so a mid-run registry change also fails closed.

The production-side primitives remain explicit and independently auditable. The
script first transfers the generated content-free lifecycle snapshot, then
streams the source payload on stdin to a one-shot `atlas` process. The payload
contains only registered source identity, fetched source content, and GitHub
content revision; it never contains the GitHub token. The production process
revalidates the current registry before using the existing projection writer.
Conceptually the production side remains:

```bash
set -e
sudo --user atlas --group atlas \
  env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/datarelay-atlas \
  /opt/datarelay-atlas/.venv/bin/python -m atlas \
  --data-root /var/lib/datarelay-atlas lifecycle publish-github-snapshot \
  --snapshot /tmp/atlas-github-lifecycle.json

sudo --user atlas --group atlas \
  env -u GITHUB_TOKEN PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/datarelay-atlas \
  /opt/datarelay-atlas/.venv/bin/python -c '<validate payload; AtlasService.sync_project(..., fetch=custom_fetch)>'
```

Atlas does not provide
an SSH/credential-relay transport. The operator host supplies its existing SSH
and GitHub authentication. For public or private GitHub repositories, source
fetching occurs only on that operator host. Before transfer, source content is
bounded and screened with the same Atlas GitHub projection secret detector;
production re-applies the normal secret/provenance checks before atomic
projection persistence. The source payload is streamed over the authenticated
SSH process and is not written as a production temporary file.

Install/update the scheduled operator job only through the approved production
change path:

```bash
sudo scripts/install-prod-context-refresh-systemd.sh
```

Then execute one scheduled-equivalent run and verify the timer and serving
health:

```bash
/usr/local/lib/datarelay-atlas/prod-context-refresh.py
systemctl status atlas-prod-refresh.timer atlas-prod-refresh.service --no-pager
curl -fsS https://mcp.atlas.datarelay.run/healthz
```

The installed operator script is root-owned and non-writable by the `aella`
service user. It uses the existing authenticated GitHub context only on the
operator host. It never copies a GitHub credential to `prod-atlas`; the remote
sync process explicitly removes `GITHUB_TOKEN`. The lifecycle snapshot is
removed from both hosts after the run and the bounded source payload is held
only in process memory/stdin for the one sync attempt.

To roll back the scheduler, restore the script, unit files, and `operator-src`
runtime from `PROD_REFRESH_ROLLBACK_DIR` when present, run `systemctl
daemon-reload`, and restart the timer. A rollback does not alter the Atlas
durable data root.

## Lifecycle

```bash
sudo systemctl start datarelay-atlas.service
sudo systemctl stop datarelay-atlas.service
sudo systemctl restart datarelay-atlas.service
sudo systemctl restart datarelay-atlas-web.service
sudo systemctl status datarelay-atlas.service
sudo systemctl status datarelay-atlas-web.service
sudo journalctl -u datarelay-atlas.service -n 100 --no-pager
sudo journalctl -u datarelay-atlas-web.service -n 100 --no-pager
```

Boot persistence is `WantedBy=multi-user.target`. The process user is `atlas`.


## Production Human UI over authenticated SSH

The initial production Human UI is intentionally **not public**.
`datarelay-atlas-web.service` runs as `atlas:atlas`, reads the existing
`/var/lib/datarelay-atlas` state, and binds only `127.0.0.1:8788`. The unit has
no service env file, TLS key, OAuth secret, GitHub token, or write path to the
data root.

Remote access uses the operator's existing SSH authentication to `prod-atlas`.
From the operator machine:

```bash
ssh -N -L 8788:127.0.0.1:8788 prod-atlas
```

Then open:

```text
http://127.0.0.1:8788/
```

The browser sends the loopback Host authority that the Atlas Web UI already
requires. Do not publish TCP 8788, bind the Web UI to `0.0.0.0`, or expose it
through the MCP 443 ingress. Closing the SSH session removes remote access.

On the production host, the local service can be checked without opening any
new ingress:

```bash
curl --silent --show-error --fail http://127.0.0.1:8788/ >/dev/null
ss -ltn | grep '127.0.0.1:8788'
```

Actual-browser Surface Reconciliation and Full User E2E should run against the
SSH-forwarded loopback URL on the same deployed candidate. A deterministic or
HTTP-only check never substitutes for those browser gates. A future public
`app.atlas.datarelay.run` deployment requires a separate explicit
authentication/exposure design.

## Health

Configuration readiness is `ops check`. Runtime readiness is HTTPS
`GET /healthz` on the serving process:

```bash
curl --silent --show-error --fail --noproxy '*' \
  --cacert /etc/datarelay-atlas/tls/cert.pem \
  --resolve mcp.atlas.datarelay.run:8443:127.0.0.1 \
  https://mcp.atlas.datarelay.run:8443/healthz
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

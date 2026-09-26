# ADR-0012: prod-atlas launch runtime

Status: Accepted
Date: 2026-09-26

## Context

ADR-0009 installed the MCP process as a non-root systemd service and did not
deploy `prod-atlas`, because that name did not resolve. Issue #64 is the
launch lane. Backup, restore-test, upgrade, and rollback already exist and
stay as they are. ChatGPT custom MCP remains Issue #41 and is not a launch
gate. Public smoke and operational E2E stay disabled until a real host
reports them.

## Minimal design gate

1. **Goal** — Name the first production runtime: hostname `prod-atlas`, public
   MCP name `mcp.atlas.datarelay.run`, loopback bind, secrets outside Git,
   and the existing restart-safe unit.
2. **Non-goals** — Claiming the host is up; provisioning DNS or certificates;
   flipping `production_oriented`; enabling public smoke or operational E2E;
   an authorization server; ChatGPT OAuth; a web UI; redesigning backup or
   rollback.
3. **Affected public contract** — `python -m atlas ops prod-contract` prints
   the secret-free contract. The systemd unit must keep `Restart=on-failure`.
   The public resource URL is `https://mcp.atlas.datarelay.run/mcp`. The
   process still binds `127.0.0.1:8443`.
4. **State / migration impact** — No schema change and no data-root rewrite.
5. **Security / operations impact** — Secret paths stay under
   `/etc/datarelay-atlas` and out of Git. The contract command reads no env
   file and prints no credential. `app.atlas.datarelay.run` is not required.
6. **Architecture boundary** — `atlas.ops` owns the contract text. systemd
   owns restart. DNS and TLS files are operator-owned on the host.
7. **Acceptance / regression criteria** — Tests assert the contract, the
   restart line, and that the command output has no secret material. They
   do not need the production host.

## Decision

1. The launch target is hostname `prod-atlas`.
2. The public MCP URL is `https://mcp.atlas.datarelay.run/mcp`. Bind stays
   loopback so the unit does not listen on a public interface by itself.
3. `ops prod-contract` reports `production_evidence: false` until a later
   slice records real host evidence. It must not be treated as a health check.
4. `production_oriented` stays false in this slice.
5. Public TCP 443 is `datarelay-atlas-ingress.socket` plus
   `systemd-socket-proxyd` to `127.0.0.1:8443`. `ListenStream=0.0.0.0:443`
   selects IPv4. The socket does not set `BindIPv6Only`. The MCP process does
   not bind 443 and does not gain `CAP_NET_BIND_SERVICE`. The proxy has no
   TLS paths.
6. The prod env example uses the public resource URL. `ops check --prod`
   and the unit `ExecStartPre` reject a loopback audience before the service
   is enabled. Generic `ops check` without `--prod` is unchanged.

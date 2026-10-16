# Shared headed-Chromium E2E access via Mac SSH tunnel and noVNC

> **Operational runbook (DataRelay Atlas knowledge source).** Search aliases: Mac SSH 터널, noVNC, 개발서버 Chromium, 공유 브라우저, Full User E2E.
> This describes an established access *method*, not proof that a particular test session is currently ready or that an E2E gate has passed.

## Purpose and boundaries

Give the owner a two-action way to view and sign in to an existing **development-server Chromium** session from a Mac. After owner authentication, the selected ChatGPT implementation runtime can continue the authorized E2E journey in that **same browser session**, provided it has a separate, working browser-control channel.

Topology: Mac browser -> local-only SSH tunnel -> development-host local-only noVNC/websockify -> local VNC server -> isolated X display -> headed Chromium -> product Web UI.

This is a shared-browser access and observation path, **not** an alternative to the project Full User E2E contract, not a credential bypass, and not proof that ChatGPT can drive the browser merely because the owner can view it.

## Preparation performed by the E2E operator/ChatGPT

1. Resolve the correct development host, authorized SSH entry point, and an isolated UI/API environment. Preserve any active worktrees, sessions, browser profiles, and other workers.
2. Reuse a healthy existing Chromium/noVNC session. Do not replace or kill another worker's X display, Chromium, VNC server, or tunnel. If absent, prepare an isolated display and session under an operator-owned directory.
3. Check actual local listeners and dependencies: `Xvfb`, `x11vnc`, `websockify`, `/usr/share/novnc/vnc.html`, a headed Chromium binary, and the chosen Web UI endpoint. Headless Playwright processes alone do not establish a visible shared Chromium session.
4. Require a live noVNC HTTP response (`200` for `/vnc.html`), an active VNC listener, and a visible Chromium application screen on the same display. The application must be reachable from that browser. Record the noVNC port per session; it is **not a permanent service address**.
5. Provide the owner with one ready-to-copy Mac SSH command and one loopback browser URL. Never ask the owner to install a separate browser extension, open the product from a different local Chrome instance, or expose VNC/noVNC publicly.

Read-only development-host checks (replace `<NOVNC_PORT>` and `<VNC_PORT>` with observed values):

```bash
ss -ltn | grep -E ':(<NOVNC_PORT>|<VNC_PORT>)\b'
curl --fail --silent --show-error --max-time 3 -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:<NOVNC_PORT>/vnc.html
ps -eo args | grep -E '[X]vfb|[x]11vnc|[w]ebsockify|[c]hrome' | head -30
```

## Minimum owner actions on macOS

**1. Open one Terminal and keep the SSH process running:**

```bash
ssh -N -p <SSH_PORT> \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -L 127.0.0.1:<MAC_PORT>:127.0.0.1:<NOVNC_PORT> \
  <SSH_USER>@<SSH_ENTRY>
```

**2. Open one browser URL on the Mac:**

```text
http://127.0.0.1:<MAC_PORT>/vnc.html?autoconnect=true&resize=scale
```

Click **Connect** if the viewer did not auto-connect. Sign in **inside the displayed development-server Chromium**, not in a separate Mac browser tab. Inform the E2E operator that login is complete; do not paste passwords, session cookies, one-time codes, or recovery codes into chat or repository files.

Example historical layout (not a guaranteed current endpoint): Mac local port `6097` -> remote noVNC port `6097`; VNC can use an independent port (e.g. `5917` or `5918`). Different E2E sessions may use different ports, displays, profiles, or application URLs. On port conflict, use a different Mac-side `<MAC_PORT>` while keeping the correct remote `<NOVNC_PORT>`.

## ChatGPT / runtime handoff

- Confirm that the Mac viewer and the authorized browser-control mechanism point to the **same X display, Chromium profile, and application session**. The Mac SSH tunnel itself does **not** provide ChatGPT with remote mouse/keyboard automation.
- Once owner login succeeds, drive the real public Web UI as the scenario persona. Observe the UI state and outcomes after each meaningful action; do not replace interactive evidence with API calls, component tests, static screenshots, or scripted claims.
- Record the exact candidate HEAD, scenario, actor/persona, UI evidence, and terminal verdict. A login screenshot or HTTP 200 is only readiness evidence, **not** Full User E2E PASS.
- Preserve security boundaries and the mandatory Surface Reconciliation -> Full User E2E -> release gates in the [Engineering System User Acceptance Standard](https://github.com/datarelay-labs/engineering-system/blob/main/standards/USER_ACCEPTANCE.md) and the bound product's local E2E contract.

## Troubleshooting and safe shutdown

| Symptom | Operator check / response |
| --- | --- |
| SSH reports `Address already in use` | Choose an unused **Mac local** port; do not terminate an unrelated SSH tunnel. |
| SSH starts, but noVNC fails to load | Verify noVNC is listening on the SSH destination's `127.0.0.1:<NOVNC_PORT>` and that the jump/target route is correct. `curl` from the development host returning `200` alone does not verify the Mac-to-host path. |
| noVNC page loads but is black | Check the target display and whether **headed** Chromium exited; recover only the isolated session owned by this E2E run. |
| Viewer works but app is not ready | Inspect Web UI/API health and navigate in the **existing** Chromium session. Do not declare an application/E2E PASS from a healthy viewer. |
| Owner logged in but agent cannot act | Verify the agent's independent authorized browser-control path into that same session; otherwise report the control-path blocker rather than simulate actions. |

Keep VNC and noVNC bound to loopback. Use SSH host-key verification and authenticated access; never open those endpoints to `0.0.0.0`. Do not store credentials in GitHub or Atlas derived projections. At session end, owner can stop the Mac tunnel with `Ctrl+C`; the E2E operator may stop only its own isolated ephemeral services after evidence collection.

## Provenance and changing status

- **2026-10-08:** Previously exercised for DataRelay Control using a Mac SSH local forward, noVNC, development-server Chromium, owner login, and continued shared-session browser E2E. Those observed ports were per-session, not platform defaults.
- **2026-10-17:** A session on `dev-drcontrol` served `/vnc.html` with HTTP `200` on loopback port `6097` and listened for VNC on `5918`. This is a time-stamped listener check, not a permanent availability promise or a verified rendered-Chromium check.
- The canonical document is this GitHub runbook. DataRelay Atlas retrieval is an attributable **derived projection**; registered source revision and sync state must be verified separately. Do not promote chat text or a noVNC listener observation into release acceptance.

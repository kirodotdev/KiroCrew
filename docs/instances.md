# Instances — Multi-Instance Management over SSH Tunnels

**Status:** Implemented (opt-in, Phases 1–4)

> Lets a single KiroCrew gateway manage and switch between several **remote**
> KiroCrew instances (dev hosts, EC2, home servers) over SSH tunnels, embedding
> each remote dashboard in one `/instances` page. Opt-in: off by default.

---

## Table of Contents

- [1. Overview](#1-overview)
- [2. Enabling the feature](#2-enabling-the-feature)
- [3. Architecture](#3-architecture)
- [4. The connect → warm → self-heal lifecycle](#4-the-connect--warm--self-heal-lifecycle)
- [5. Configuration](#5-configuration)
- [6. API (owner-only control plane)](#6-api-owner-only-control-plane)
- [7. Security model](#7-security-model)
- [8. Using it (step by step)](#8-using-it-step-by-step)
- [9. Remote host types](#9-remote-host-types)
- [10. Troubleshooting](#10-troubleshooting)

---

## 1. Overview

A KiroCrew gateway normally binds the dashboard to loopback only. The Instances
feature lets one gateway (the **hub**) reach *other* gateways running on remote
hosts by opening an SSH `-L` forward to each remote's loopback dashboard port,
minting a short-lived dashboard token on the remote, and embedding the remote
dashboard in an `<iframe>`. You switch between remotes with a tab strip; the hub
keeps the most-recently-used set "warm" (tunnel + iframe live) and lazily
reconnects the rest.

It supersedes the older, macOS-only Electron-tab workflow (manual `ssh -L`,
`localhost:<port>` per tab) with an automated, cross-platform page: an N-host
registry, auto-tunneling, warm-set management, health probing, and self-healing.

**Key properties**
- **Opt-in.** Nothing changes until `instances.enabled=true`.
- **Owner-only.** The control plane is never reachable via Slack and requires an
  authenticated dashboard session.
- **Loopback-only.** Tunnels forward `127.0.0.1:<local>` → remote `127.0.0.1:<remote>`.
- **Warm, not persistent.** Connections live for the page session; tokens are
  short-lived and re-minted before they lapse.

---

## 2. Enabling the feature

```bash
kirocrew config set instances.enabled true
kirocrew restart
```

When enabled, the gateway:
1. creates the instances registry + `SshTunnelManager`, and
2. scopes a CSP `frame-src` relaxation to the **active loopback tunnel ports**
   so the embedded remote dashboards can render (they are otherwise blocked by
   the dashboard's strict `frame-src 'self' blob:`).

With the flag off, `/api/instances/*` returns `403` and the `/instances` page
shows an opt-in hint.

---

## 3. Architecture

```
 ┌──────────────────────── Hub gateway (this host) ────────────────────────┐
 │                                                                          │
 │  /instances page (React)                                                 │
 │   ├─ InstancesSwitcher  (chips: Home · remote-1 · remote-2 · Manage)     │
 │   ├─ warm <iframe>s     http://127.0.0.1:<local_port>/?token=…           │
 │   └─ Manage panel       add / connect / diagnose / restart / remove      │
 │            │ owner-only JSON API (SEL-audited)                           │
 │  dashboard/handlers_instances.py                                         │
 │            │                                                             │
 │  instances/ package                                                      │
 │   ├─ registry.py         ~/.kirocrew/instances.json                      │
 │   ├─ port_allocator.py   loopback ports from base 7778                   │
 │   ├─ token_mint.py       `ssh <host> kirocrew token` → JWT (never logged)│
 │   ├─ ssh_tunnel_manager  supervised `ssh -N -L`, probe, self-heal, refresh│
 │   └─ diagnostics.py      ssh → remote-dashboard → local-forward ladder    │
 └──────────────────────────────────────────────────────────────────────────┘
        │ ssh -N -L 127.0.0.1:<local>:127.0.0.1:<remote>  <ssh_host>
        ▼
 ┌──────────────── Remote gateway (dev host / EC2 / home server) ───────────┐
 │  kirocrew gateway bound to 127.0.0.1:<remote_port> (default 7777)        │
 └──────────────────────────────────────────────────────────────────────────┘
```

Module responsibilities:

| Module | Responsibility |
|--------|----------------|
| `registry.py` | Persistent list of configured instances (`~/.kirocrew/instances.json`) + `last_active_id`. Validates `ssh_host`/`remote_bin` at add/update. |
| `port_allocator.py` | Hands out free loopback ports from `tunnel_base_port` (7778), skipping bound/excluded ports. |
| `token_mint.py` | Runs `kirocrew token` on the remote over SSH via a bin-candidate ladder; parses the JWT from the printed URL. Token is returned in-memory only, **never logged**. |
| `ssh_tunnel_manager.py` | Supervises one `ssh -N -L` child per instance: readiness wait, health probe, 2-tier self-heal, proactive token refresh, remote restart. |
| `diagnostics.py` | Dependency-ordered failure probes; reports the first broken link. |
| `handlers_instances.py` | Owner-only, enabled-gated, SEL-audited HTTP control plane. |

---

## 4. The connect → warm → self-heal lifecycle

1. **Connect.** `POST /api/instances/{id}/connect` allocates a loopback port,
   mints a dashboard token on the remote over SSH, starts `ssh -N -L`, waits
   until the local forward accepts a connection, then returns the live status +
   token. The browser loads `http://127.0.0.1:<local>/?token=…` in an iframe.
2. **Warm set.** Up to `warm_set_cap` (default 5) most-recently-used instances
   stay warm — iframe mounted (hide-not-unmount, so switching never reloads or
   re-runs the token handshake) with a live tunnel + WebSocket. Connecting beyond
   the cap **lazily evicts** the least-recently-used instance (disconnects it; it
   reconnects on demand). The eviction is surfaced as a transient "N/K warm" notice.
3. **Health probe.** While CONNECTED, a probe polls the loopback forward every
   `DEFAULT_PROBE_INTERVAL_SECS` (30s); after `DEFAULT_PROBE_FAILURE_THRESHOLD`
   (3) consecutive failures it tears the child down so recovery fires.
4. **2-tier self-heal.** On unexpected exit: **Tier 1** rebuilds the tunnel
   reusing the existing token; **Tier 2** re-mints the token over SSH then
   rebuilds. Capped at `DEFAULT_MAX_RECOVERY_ATTEMPTS` (8) consecutive attempts
   with a capped-exponential backoff (`DEFAULT_RECOVER_BACKOFF_MAX_SECS`, 30s) —
   a ~2 min recovery window that outlasts a transient drop (screen lock, WSSH
   warmup); the counter also resets on a successful `connect()`. The attempt cap,
   backoff cap, and probe-failure threshold are config-tunable (`instances.*`). If
   it gives up, the diagnosis ladder runs automatically.
5. **Proactive token refresh.** A per-instance loop re-mints the token at
   `DEFAULT_TOKEN_REFRESH_FRACTION` (0.8) of its TTL, ahead of the ~20h cap.
6. **Diagnose / restart.** `?diagnose=1` runs the probe ladder on demand;
   `POST …/restart` restarts the **remote** gateway over SSH (service-aware),
   after which the local probe detects the bounce and self-heals.

---

## 5. Configuration

All under `instances.*` (defaults in `kiro_crew.instances.constants`):

| Key | Default | Meaning |
|-----|---------|---------|
| `instances.enabled` | `false` | Master opt-in. Also scopes the CSP `frame-src` relaxation. |
| `instances.warm_set_cap` | `5` | Max instances kept warm at once (bounds memory/sockets; each warm instance is a full dashboard SPA). |
| `instances.tunnel_base_port` | `7778` | First local loopback port for an SSH `-L` forward; the allocator increments from here. |

```bash
kirocrew config set instances.warm_set_cap 3
```

**Registry** (`~/.kirocrew/instances.json`) — one record per instance:
`id, name, ssh_host, remote_port (default 7777), local_port, ttl (default 20h),
remote_bin, was_connected`, plus a top-level `last_active_id`.

---

## 6. API (owner-only control plane)

All routes are gated by `_guard()`: **deny-by-default** — reject Slack-origin
requests (`403`), reject unauthenticated requests with no `request["user"]`
(`401`), reject when the feature is disabled (`403`). Every call (success and
denial) emits a SEL audit event.

| Method & path | Purpose |
|---------------|---------|
| `GET /api/instances` | List instances + live status + `warm_set_cap`. |
| `POST /api/instances` | Add an instance. |
| `PATCH /api/instances/{id}` | Edit `name`/`ssh_host`/`remote_port`/`ttl`/`remote_bin`. |
| `DELETE /api/instances/{id}` | Disconnect then remove. |
| `POST /api/instances/{id}/connect` | Open tunnel + mint token. Returns the token (the **only** place a token crosses this boundary; never logged, never in list/status). |
| `POST /api/instances/{id}/disconnect` | Tear down one tunnel. |
| `GET /api/instances/{id}/status[?diagnose=1]` | Live status; `?diagnose=1` runs the failure ladder. |
| `POST /api/instances/{id}/restart` | Restart the remote gateway over SSH. |

---

## 7. Security model

- **Owner-only, never via Slack.** A Slack-origin `X-Session-Key` is rejected;
  an authenticated dashboard session (`request["user"]`, set by the gateway's
  `require_auth` middleware) is positively required (deny-by-default).
- **Loopback-only forwards.** `ssh -N -L 127.0.0.1:<local>:127.0.0.1:<remote>`.
- **No local shell.** `ssh` is always invoked with an argv list — `ssh_host`
  cannot inject local shell syntax. `ssh_host`/`remote_bin` are charset-validated
  before use; the remote command's only variable parts are validated literals.
- **Tokens.** Short-lived (≤20h) bearer tokens minted over SSH, returned only to
  the in-memory caller, **never logged**, never present in list/status payloads.
- **postMessage relay.** The parent validates every embedded-frame `event.origin`
  against the exact `http://127.0.0.1:<port>` of a currently-warm tunnel before
  trusting an unread count; nothing else crosses the iframe boundary.
- **SEL audit trail.** Every control-plane action is audited (reads and writes).

---

## 8. Using it (step by step)

1. **Enable** on the hub: `kirocrew config set instances.enabled true && kirocrew restart`.
2. Open the dashboard and go to **Instances** (`/instances`).
3. **Add** an instance in the Manage panel:
   - *Name* — any label (e.g. "Dev Host 1").
   - *SSH host / alias* — what you'd type after `ssh` (see §9).
   - *Remote port* — the remote gateway's dashboard port (default `7777`).
   - *Token TTL* — default `20h`.
4. Click **Connect**. The hub opens the tunnel, mints a token, and a chip appears.
5. **Switch** between connected instances via the chips; **Home** returns to your
   local dashboard. The Manage chip is a peer view — switching to it does **not**
   tear down connections.
6. **Diagnose** a flaky instance (runs the ladder) or **Restart** its remote
   gateway from its row. **Disconnect**/**Remove** as needed.

> Prerequisite: you can already `ssh <ssh_host>` non-interactively from the hub
> (a valid SSH key/cert in your `ssh-agent`, no password prompt), and the remote
> has `kirocrew` installed and a gateway running on its loopback port.

---

## 9. Remote host types

The only thing that varies per remote is the **SSH host** you configure — the
hub always runs a fixed `ssh <ssh_host> …` argv (`BatchMode=yes`,
`ServerAlive*`, `AddressFamily=inet`, `-L`/`-N`). Anything `ssh` can reach
**non-interactively** (no password/passphrase prompt) works. `ssh_host` accepts
`host`, `host.fqdn`, an `~/.ssh/config` alias, or `user@host`; it rejects any
segment starting with `-` (ssh option-injection guard).

### Dev host / home server (primary)

Use your SSH config alias (e.g. `dev-1-alias`) or `user@hostname`. As long as a
key in your `ssh-agent` (or the default identity) covers auth, `BatchMode`
succeeds without prompting and no key path is needed.

### EC2 (and other key-based hosts)

EC2 differs from a directly-reachable dev host in three ways that matter here:

| Aspect | Direct dev host | EC2 |
|--------|-----------------|-----|
| Auth | key in `ssh-agent` / default identity | Key pair (`-i key.pem`), or SSM Session Manager |
| Login user | resolved by your ssh config | `ec2-user` (AL), `ubuntu`, `admin`, … — must be explicit |
| Reachability | direct | often via a bastion (ProxyJump) or SSM-only (no public SSH) |

**Recommended (works today, no code change): configure an SSH alias.** Because
`ssh_host` accepts an alias, put the EC2-specific bits in `~/.ssh/config` on the
**hub** and reference the alias. The fixed `ssh <alias> …` argv inherits all of
it:

```ssh-config
# ~/.ssh/config on the hub
Host my-ec2
  HostName ec2-1-2-3-4.compute-1.amazonaws.com
  User ec2-user
  IdentityFile ~/.ssh/my-key.pem
  # Optional: reach a private instance through a bastion …
  ProxyJump bastion-host
  # … or via SSM Session Manager (no inbound SSH needed):
  # ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p"
```

Then add an instance with **SSH host / alias = `my-ec2`** and remote port `7777`.
Prerequisites on the hub: a passphrase-less key (or an `ssh-agent` already
holding it — `BatchMode` will not prompt), and `kirocrew` installed + a gateway
running on the EC2 instance's loopback port.

Simpler cases also work without an alias: `ec2-user@10.0.1.5` or
`ubuntu@ec2-1-2-3-4.compute-1.amazonaws.com` are both accepted `ssh_host`
values — provided the matching key is the default identity or in the agent.

### What's allowed vs blocked today

| Need | Status | How |
|------|--------|-----|
| Custom login user | ✅ | `user@host` or ssh-config `User` |
| FQDN / IP target | ✅ | direct `ssh_host` value |
| Identity file (`-i`) | ⚠️ via ssh config only | `IdentityFile` in a `Host` block (not an inline field) |
| Non-22 SSH port | ⚠️ via ssh config only | `Port` in a `Host` block (no inline `-p` field) |
| Bastion / ProxyJump | ⚠️ via ssh config only | `ProxyJump` / `ProxyCommand` in a `Host` block |
| SSM-only instances | ⚠️ via ssh config only | `ProxyCommand` with `aws ssm start-session` |
| Inline `-i` / `-p` / `-J` in the Add form | ❌ not yet | see "Future" below |

### Future: first-class EC2 fields (proposed, not yet implemented)

To let users add EC2 hosts from the UI without editing `~/.ssh/config`, the
registry/validation/argv could gain optional, injection-validated per-instance
fields: `ssh_port` (int → `-p`), `identity_file` (path-charset validated → `-i`),
and `proxy_jump` (validated like `ssh_host` → `-J`). Each must keep the existing
guards (reject a leading `-`, charset-bound, no shell metacharacters); the new
surface to watch is `identity_file` (a user-controlled `-i` path). Until then,
the ssh-config alias path above is the supported, lower-risk way to reach EC2,
and it already covers bastions and SSM that inline flags cannot.


---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `/instances` shows "multi-instance management is off" | `instances.enabled` is false — set it and restart. |
| Iframe is blank | CSP `frame-src` relaxation only applies to active tunnel ports; ensure the instance is **connected** (port allocated). |
| Connect fails with an SSH auth error | Refresh your SSH credentials (re-add the key to `ssh-agent`); tunnels self-heal once SSH auth is restored. |
| Connect fails | Use **Diagnose** — the ladder reports the first broken link: `ssh_unreachable` (check SSH access / host alias), `remote_down` (remote gateway not running), or `tunnel_down` (reconnect). |
| Instance keeps dropping | Health probe + 2-tier self-heal retry over a ~2 min window (default 8 attempts, capped-exponential backoff; config-tunable via `instances.max_recovery_attempts` / `recover_backoff_max_secs` / `probe_failure_threshold`; `max_recovery_attempts` is clamped to `[1, 100]` and `recover_backoff_max_secs` to `(0, 300]` so it can't loop indefinitely); if it gives up, diagnosis runs automatically. Check the remote gateway and SSH stability. |
| An instance silently disappeared from the warm set | It was LRU-evicted (warm set full). Raise `instances.warm_set_cap` or reconnect on demand. |

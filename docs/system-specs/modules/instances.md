# Instances Module (multi-instance management over SSH tunnels)

Lets a single Kiro Crew gateway (the **hub**) manage and switch between several
**remote** Kiro Crew instances (dev hosts, EC2, home servers) over SSH **or AWS
SSM Session Manager** tunnels, embedding each remote dashboard as an iframe pane
below a tab strip. Opt-in: off by default (`instances.enabled`). The transport is
per-instance (`connection_method`) — see §13.

> **Section numbers in this document are an API.** `src/kiro_crew/cloud/connect.py`
> cites "instances.md §9" from two docstrings (the module docstring and
> `ssm_proxy_ssh_host`). Do not renumber existing sections; append new material as
> new trailing sections.

Code: `src/kiro_crew/instances/` (registry, tunnel manager, port allocator, token
mint, diagnostics, injection validation, run-marker) plus
`src/kiro_crew/dashboard/handlers_instances.py` (control plane) and the frontend
`InstanceTabBar` / `InstancesViewport` / `Settings → Instances` surfaces.

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
- [11. Input validation (`validation.py`)](#11-input-validation-validationpy)
- [12. The gateway run-marker (`run_marker.py`)](#12-the-gateway-run-marker-run_markerpy)
- [13. The SSM connection method (`connection_method`)](#13-the-ssm-connection-method-connection_method)

---

## 1. Overview

A Kiro Crew gateway normally binds the dashboard to loopback only. The Instances
feature lets the hub reach *other* gateways running on remote hosts by opening an
SSH `-L` forward to each remote's loopback dashboard port, minting a short-lived
dashboard token on the remote, and embedding the remote dashboard in an
`<iframe>`. You switch panes with a tab strip (`InstanceTabBar`, plus
Cmd/Ctrl+digit in the Electron shell); the hub keeps the most-recently-used set
"warm" (tunnel + iframe live) and lazily reconnects the rest.

**Key properties**

- **Opt-in.** Nothing changes until `instances.enabled=true`, and the flag is
  read at gateway startup, so it also needs a restart.
- **Owner-only.** The control plane is never reachable via Slack and requires an
  authenticated dashboard session.
- **Loopback-only.** Tunnels forward `127.0.0.1:<local>` to remote
  `127.0.0.1:<remote>`.
- **Warm, not persistent.** Tokens are short-lived (20h cap) and re-minted
  before they lapse; iframes are evicted past the warm cap.

---

## 2. Enabling the feature

```bash
kirocrew config set instances.enabled true
kirocrew restart
```

Settings → Instances offers the same toggle (it PATCHes
`instances.enabled` through `/api/config/kirocrew`) and then shows a
"restart required" hint, because the flag is only consulted in the gateway's
`on_startup` hook.

When enabled at startup, the gateway:

1. creates the instances registry + `SshTunnelManager` and auto-reconnects every
   instance whose `was_connected` hint is set, and
2. extends the dashboard CSP `frame-src` with `http://*.localhost:*` so an
   embedded remote dashboard on a `*.localhost` host can render.

Loopback origins (`http://127.0.0.1:*`, `http://localhost:*`, plus the https and
`0.0.0.0` forms) are in `frame-src` **unconditionally** because the Web Preview
panel needs them; only the `*.localhost` wildcard is instances-gated.

With the flag off, `/api/instances/*` returns `403` and the panel shows an
opt-in card. `GET /api/instances` also reports `active`, which is true only when
the SSH manager actually exists: `enabled && !active` means the flag was set
after startup and a restart is still pending.

---

## 3. Architecture

```
 +----------------------- Hub gateway (this host) ------------------------+
 |                                                                       |
 |  Dashboard SPA                                                        |
 |   |- InstanceTabBar     (Local | remote-1 | remote-2 ...)             |
 |   |- InstancesViewport  warm <iframe>s: http://<host>:<port>/?token=  |
 |   +- Settings > Instances   add / connect / diagnose / remove         |
 |            | owner-only JSON API (SEL-audited)                        |
 |  dashboard/handlers_instances.py                                      |
 |            |                                                          |
 |  instances/ package                                                   |
 |   |- registry.py         ~/.kiro/crew/instances.json                  |
 |   |- port_allocator.py   free-loopback-port probe (base 7778)         |
 |   |- token_mint.py       ssh <host> kirocrew token -> JWT (never logged)|
 |   |- validation.py       injection-safe ssh_host / remote_bin guards  |
 |   |- run_marker.py       <home>/run/gateway-<port>.bin launcher hint  |
 |   |- ssh_tunnel_manager  supervised ssh -N -L, probe, self-heal, refresh|
 |   +- diagnostics.py      ssh -> remote-dashboard -> local-forward ladder|
 +-----------------------------------------------------------------------+
        | ssh -N [-C] -L 127.0.0.1:<local>:127.0.0.1:<remote> <ssh_host>
        v
 +--------------- Remote gateway (dev host / EC2 / home server) ---------+
 |  kirocrew gateway bound to 127.0.0.1:<remote_port> (registry default  |
 |  7777; the local gateway's own default port is 5476)                  |
 +-----------------------------------------------------------------------+
```

Module responsibilities:

| Module | Responsibility |
|--------|----------------|
| `registry.py` | Persistent list of configured instances (`~/.kiro/crew/instances.json`) + `last_active_id`. Light charset check on `ssh_host`/`remote_bin` (SSH) or `ssm_target`/`aws_profile`/`aws_region`/`ssm_run_as` (SSM) at add/update, per `connection_method`; every mutation re-reads the file and writes atomically, so a live gateway and a CLI edit cannot clobber each other. |
| `port_allocator.py` | Probes for a free loopback port at or above `tunnel_base_port` (7778). The probe sets `SO_REUSEADDR` so a `TIME_WAIT` remnant from a just-closed forward is not a false "in use". |
| `token_mint.py` | Runs `kirocrew token --ttl --port --embed-parent-port` on the remote over SSH (run-marker first, then a bin-candidate ladder) and parses the JWT out of the printed URL. Token is returned in memory only, **never logged**. |
| `ssm_token_mint.py` | The SSM sibling of `token_mint.py`: runs the same subcommand via `aws ssm send-command` through the launcher's `cloud.ssm` chokepoint, reusing the shared remote-command builders. Token in memory only, **never logged**. See §13. |
| `validation.py` | The authoritative injection-safe guard on `ssh_host` / `remote_bin`, and on `ssm_target` / `aws_profile` / `aws_region` / `ssm_run_as`, applied immediately before any command line is built. See §11. |
| `run_marker.py` | Records the running gateway's own `kirocrew` launcher (and pid) keyed by port, so a remote mint execs the same venv the live gateway runs from. Also backs zero-config client port discovery. See §12. |
| `ssh_tunnel_manager.py` | Supervises one tunnel child per instance — `ssh -N -L` or `aws ssm start-session` — with readiness wait, health probe, 2-tier self-heal, proactive token refresh, stored-token liveness probe, remote restart, orphan-forwarder reaping. One state machine, two transports. |
| `diagnostics.py` | Dependency-ordered failure probes; reports the first broken link. `diagnose_instance` (SSH ladder) and `diagnose_instance_ssm` (SSM ladder). |
| `handlers_instances.py` | Owner-only, enabled-gated, SEL-audited HTTP control plane. |

**The local forward port mirrors the remote port.** `connect()` sets
`local_port = inst.remote_port` rather than allocating a fresh one: the embedded
iframe loads from `http://<host>:<local_port>`, and the remote gateway only
trusts CSRF/WebSocket `Origin`s on its own configured port, so mirroring keeps
the Origin valid with no per-instance allowlisting. The consequence is a hard
constraint: **every simultaneously-connected instance must use a distinct remote
port**, and a busy port is a clear connect error rather than a silent fallback
(a different local port would leave the pane unable to stream or act). The
`PortAllocator` is therefore constructed but not on the connect path today; the
`tunnel_base_port` setting configures it.

**Platform note.** The hub side of this feature assumes a POSIX host with an
OpenSSH `ssh` client on `PATH`. Two paths make that explicit: the
orphan-forwarder reaper shells `ps -axww -o pid=,command=` and signals with a
direct `os.kill(pid, signal.SIGTERM)` rather than going through
`platform_compat`, and run-marker port discovery refuses outright on non-POSIX
(§12). Treat a Windows hub as unverified.

---

## 4. The connect → warm → self-heal lifecycle

1. **Connect.** `POST /api/instances/{id}/connect` validates the ssh inputs,
   reaps any orphaned forwarder still holding the mirrored port, starts
   `ssh -N -L`, waits until the local forward accepts a TCP connection, mints a
   dashboard token on the remote over SSH, and returns the live status plus the
   token. Connect is **idempotent**: an already-connected instance returns its
   current status, and the handler then *probes* the stored token before handing
   it over (see below). The browser loads
   `http://<dashboard-hostname>:<local>/?token=...` in an iframe, deliberately
   reusing the parent's own hostname so the pane is same-site with the parent and
   `SameSite=Lax` auth cookies are not withheld.
2. **Warm set.** Up to `warm_set_cap` (default 5) most-recently-used instances
   stay warm: iframe mounted (hide-not-unmount, so switching never reloads or
   re-runs the token handshake) with a live tunnel and WebSocket. Exceeding the
   cap **evicts the least-recently-used non-active iframe**. Eviction unmounts
   the iframe only: it does NOT disconnect the tunnel or clear `was_connected`,
   so the tab persists and re-warms on the next click. Tabs disappear only on an
   explicit disconnect.
3. **Health probe.** While CONNECTED, a per-tunnel loop polls the loopback
   forward every `DEFAULT_PROBE_INTERVAL_SECS` (30s, not user-configurable;
   `<= 0` disables the probe); after `probe_failure_threshold` (3) *consecutive*
   failures the child is terminated so recovery fires. This is what catches a
   tunnel that is alive but no longer forwarding.
4. **2-tier self-heal.** On unexpected child exit: **Tier 1** rebuilds the tunnel
   reusing the existing token; **Tier 2** re-mints the token over SSH and then
   rebuilds. Capped at `max_recovery_attempts` (8) consecutive attempts with a
   capped-exponential backoff (`recover_backoff_max_secs`, 30s; the wait grows
   1, 2, 4, 8, 16 then holds at the cap), which spans roughly a two-minute
   window: long enough to outlast a transient drop (screen lock, proxy warmup).
   The counter resets on a successful rebuild or a successful `connect()`. If it
   gives up, the diagnosis ladder runs automatically. The slow SSH I/O runs
   *without* the manager lock so self-heal cannot stall a concurrent
   connect/disconnect/shutdown.
5. **Proactive token refresh.** A per-instance loop re-mints the token at
   `DEFAULT_TOKEN_REFRESH_FRACTION` (0.8) of its TTL, ahead of the 20h cap. The
   frontend mirrors the same 0.8 threshold from `token_ttl_remaining` and skips
   the *active* pane, so a reload never interrupts the pane in use.
6. **Stored-token liveness probe.** A token can go stale while the tunnel stays
   CONNECTED (a failed self-heal re-mint, or a remote `kirocrew restart` that
   invalidates tokens). An iframe loaded with a stale token gets a
   server-rendered 403, so the SPA never boots to fire the reactive
   `mc-auth-expired` recovery. `connect` therefore probes
   `GET /api/status?token=...` over the *existing* forward (no SSH,
   `DEFAULT_TOKEN_PROBE_TIMEOUT_SECS` = 2s) and is deny-by-default: anything
   short of a 2xx forces a fresh mint, and if that mint also fails the response
   is a clean 502 rather than a token the gateway cannot stand behind.
7. **Diagnose / restart.** `?diagnose=1` runs the probe ladder on demand;
   `POST .../restart` runs `kirocrew restart` on the **remote** over SSH
   (itself service-aware), after which the local probe detects the bounce and
   self-heals.

**Startup revive.** When the feature is on, the startup hook reconnects every
instance with `was_connected` set, serially (so they do not race to bind their
mirrored ports) and each wrapped, so one unreachable host neither aborts the rest
nor crashes startup. It runs as a background task rather than awaited, because
`on_startup` fires *before* the HTTP port is bound and serial SSH connects would
delay the bind past the desktop app's gateway-wait window. A failed revive leaves
`was_connected` true and records the failure reason, so the tab persists showing
why it is down.

---

## 5. Configuration

### 5.1 `instances.*` config keys

Defaults live in `kiro_crew.instances.constants` and are referenced from the
`InstancesConfig` dataclass, so the constant and the config default cannot drift.

| Key | Default | Meaning |
|-----|---------|---------|
| `instances.enabled` | `false` | Primary opt-in, read at gateway startup. Also gates the CSP `frame-src` `*.localhost` extension. |
| `instances.warm_set_cap` | `5` | Max instances kept warm at once (bounds memory/sockets; each warm instance is a full dashboard SPA). Clamped up to 1. |
| `instances.tunnel_base_port` | `7778` | First local loopback port the allocator hands out. Out-of-range values fall back to the default. |
| `instances.ssh_compression` | `true` | Add `-C` to the tunnel argv. See §5.2. |
| `instances.max_recovery_attempts` | `8` | Consecutive self-heal attempts before the tunnel is left disconnected. Below 1 falls back to the default; above `MAX_RECOVERY_ATTEMPTS_CEILING` (100) is clamped with a warning, so a pathological setting cannot turn bounded self-heal into a near-infinite retry loop. |
| `instances.recover_backoff_max_secs` | `30.0` | Cap on the per-attempt backoff. Non-positive falls back to the default; above `RECOVER_BACKOFF_MAX_CEILING_SECS` (300) is clamped, bounding the worst-case wall-clock recovery window. |
| `instances.probe_failure_threshold` | `3` | Consecutive health-probe failures before a non-forwarding tunnel is torn down. Below 1 falls back to the default. |

```bash
kirocrew config set instances.warm_set_cap 3
kirocrew config set instances.ssh_compression false
```

Constants that are **not** user-configurable: the probe interval (30s), the token
refresh fraction (0.8), the stored-token probe timeout (2s), the connect
readiness timeout (15s), and the mint timeout (30s).

### 5.2 `instances.ssh_compression`

Adds `-C` (zlib transport compression) to the supervised `ssh -N -L` argv. It is
on by default, and the reasoning is specific to what travels over this one
forwarded stream: the *entire* remote dashboard, meaning the SPA bundle on first
connect plus every subsequent API and WebSocket frame. That payload is
JS/HTML/JSON, which compresses well, and the gateway does **not** gzip its HTTP
responses, so `-C` is the only compression anywhere in the path and nothing is
double-compressed. The dominant deployment is a dedicated remote gateway host
reached over a higher-latency link, where spending remote CPU to save bandwidth
is the right trade. On a fast or local link the CPU cost can outweigh the
bandwidth win, which is why it stays tunable.

The flag is read once, at startup, into the `SshTunnelManager`, and each
`_SshTunnel` inherits it; changing it takes effect on the next gateway restart.
Only the *tunnel* argv is affected. The token-mint and diagnostics `ssh`
invocations do not compress (they are single short commands, so there is nothing
to gain).

### 5.3 Registry file

`~/.kiro/crew/instances.json`, one record per instance:

```
id, name, ssh_host, remote_port (default 7777), local_port (0 = unallocated),
ttl (default "20h"), remote_bin, was_connected
```

plus a top-level `last_active_id`. `id` is a slug (`^[a-z0-9][a-z0-9-]{0,62}$`)
derived from `name` when not given, with a numeric suffix on collision. The file
holds **connection coordinates only**: no credentials or tokens are ever written
there.

Two persisted hints drive lazy reconnect:

- `was_connected` is sticky "connection intent". It is set when a tunnel opens
  and cleared **only** on an explicit user disconnect, deliberately surviving
  gateway shutdown and a failed auto-revive, so the frontend keeps the tab in an
  error / click-to-reconnect state instead of dropping it. It is also what the
  frontend keys tab visibility on (`was_connected || connected || warm`).
- `last_active_id` records the instance most recently connected to. `connect()`
  writes it and `remove()` clears it, and any value that no longer resolves to a
  live record is dropped on the next write. Nothing in the gateway reads it:
  startup revive keys on `was_connected` and revives *every* intended instance,
  not just one, and the active pane is frontend state. `get_last_active()` is the
  only reader and has no production caller.

`disconnect()` resets `local_port` to the unallocated sentinel together with
`was_connected` in one write, so a freed port is never left reserved.

---

## 6. API (owner-only control plane)

All routes are gated by `_guard()`: **deny-by-default**. It rejects a
Slack-origin request (an `X-Session-Key` starting `slack:`) with `403`, rejects a
request with no `request["user"]` with `401`, and rejects a disabled feature with
`403`. Every call, success and denial alike, emits a SEL audit event
(`instances_<operation>`).

| Method and path | Purpose |
|---|---|
| `GET /api/instances` | List instances + live status + `warm_set_cap` + `active`. |
| `POST /api/instances` | Add an instance. |
| `PATCH /api/instances/{id}` | Edit `name`/`ssh_host`/`remote_port`/`ttl`/`remote_bin` (internal hints are not editable). |
| `DELETE /api/instances/{id}` | Disconnect then remove. |
| `POST /api/instances/{id}/connect` | Open tunnel + mint token. Returns the token. |
| `POST /api/instances/{id}/refresh-token` | Force a fresh mint and return the new token. See below. |
| `POST /api/instances/{id}/disconnect` | Tear down one tunnel. |
| `GET /api/instances/{id}/status[?diagnose=1]` | Live status; `?diagnose=1` runs the failure ladder and merges the result. |
| `POST /api/instances/{id}/restart` | Restart the remote gateway over SSH. |

**Two routes cross the token boundary, not one.** `connect` and `refresh-token`
both return a minted dashboard token in their response body, and they are the
**only** two that do. `refresh-token` exists because the browser needs to replace
an embedded pane's credential without tearing the tunnel down: proactively at
~80% of the TTL for a non-active pane, and reactively when an embedded dashboard
posts `mc-auth-expired` for the active pane (rate-limited client-side to one
re-mint per instance per 10s so a persistently-rejecting remote cannot spin a
reload storm). The invariant is the same on both: the token is delivered to the
authenticated owner only, is **never logged**, and **never** appears in a list or
status payload. The count is what to keep straight, since a single-route reading
would leave `refresh-token` out of any audit of where tokens leave the gateway:
the pair is `connect` + `refresh-token`, and nothing else.

Status codes worth knowing: `503` when the manager is not running (feature
enabled after startup), `404` for an unknown id, `502` when a connect, refresh,
or remote restart fails, and `400` on invalid add/update input.

`restart` is wired end to end (route, handler, `restart_remote`, and an
`api.restartInstance` client method) but no dashboard surface calls it today, so
it is reachable only by an authenticated owner driving the API directly.

---

## 7. Security model

- **Owner-only, never via Slack.** A Slack-origin `X-Session-Key` is rejected;
  an authenticated dashboard session (`request["user"]`, set by the token-auth
  middleware) is positively required rather than assumed.
- **Loopback-only forwards.** `ssh -N -L 127.0.0.1:<local>:127.0.0.1:<remote>`,
  with `AddressFamily=inet` to avoid an unexpected `::1` bind, `BatchMode=yes`
  so a missing credential fails fast instead of prompting, and
  `ExitOnForwardFailure=yes` so a forward that cannot bind is a detected failure
  rather than a silent hang. `-N` without `-f` is deliberate: `-f` would fork ssh
  into the background and leave the gateway unable to supervise or kill the real
  forwarder. The multiplexing pins in §9 close the same hole from the
  ssh_config side.
- **No local shell.** `ssh` is always spawned with an argv list, so `ssh_host`
  cannot inject local shell syntax; `ssh_host`/`remote_bin` are
  injection-validated immediately before every command line is built (§11).
- **Tokens.** Short-lived bearer tokens (`MAX_SESSION_TTL_SECS` caps the session
  at 20h) minted over SSH, returned only to the in-memory caller, never logged,
  and never present in list/status payloads. The mint's failure path carries a
  bounded stdout tail that is token-substituted and credential-redacted first,
  and the scan window is bounded because the redaction regexes hold the GIL.
- **postMessage relay.** The parent validates every embedded-frame
  `event.origin` against an exact loopback http origin (`127.0.0.1`, `localhost`,
  or a single-label `*.localhost`) **and** requires the port to belong to a
  currently-warm tunnel before trusting any message. Only four message kinds
  cross the boundary: an unread count, an auth-expired signal, a switch-pane
  request (whose target is re-validated against the known instance list), and a
  readiness ping. The parent's outbound `postMessage` is addressed to the pane's
  exact origin, never `*`.
- **CSP.** `frame-ancestors` is `'self'` plus the exact parent origin carried in
  the minted token's signed `embed_parent_port` claim, never a wildcard and never
  a hardcoded port, so a local page with no validly-signed token can never frame
  the dashboard.
- **Untrusted ssh stderr.** A proxy banner is ANSI-stripped, credential- and
  exfiltration-redacted, and truncated before it is surfaced in status, and it is
  a secondary detail only: failure *classification* keys on real ssh signals, so
  banner prose can never be read as an auth verdict.
- **Trust root.** `<data-home>/run/` (the run-marker dir) is on the
  `is_sensitive_path` floor, so agent file tools can neither read nor write it.
  See §12 and [security.md](security.md).
- **SEL audit trail.** Every control-plane action is audited, reads included.

---

## 8. Using it (step by step)

1. **Enable** on the hub: `kirocrew config set instances.enabled true && kirocrew restart`
   (or the Settings → Instances toggle, then a restart).
2. Open the dashboard and go to **Settings → Instances**. This panel is the
   control plane only; it does not embed remote dashboards.
3. **Add** an instance:
   - *Name*: any label.
   - *SSH host / alias*: what you would type after `ssh` (see §9).
   - *Remote port*: the port the remote gateway listens on. It must be unique
     across instances, because the local forward mirrors it.
   - *Token TTL*: default `20h`.
   - *Remote kirocrew path*: only needed when `kirocrew` lives somewhere
     non-standard on the remote.
4. Click **Connect**. The hub opens the tunnel and mints a token.
5. **Switch** panes from the tab strip in the top header (**Local** returns to
   your own dashboard). In the Electron shell, Cmd/Ctrl+digit jumps between panes
   in tab order.
6. **Diagnose** a flaky instance (runs the ladder), or **Disconnect** /
   **Remove** from its row.

> Prerequisite: you can already `ssh <ssh_host>` non-interactively from the hub
> (a valid key or cert in your `ssh-agent`, no password prompt), and the remote
> has `kirocrew` installed with a gateway running on its loopback port.

---

## 9. Remote host types

The only thing that varies per remote is the **SSH host** you configure: the hub
always runs a fixed `ssh <ssh_host> ...` argv (`BatchMode=yes`,
`ExitOnForwardFailure=yes`, `ServerAliveInterval=30`, `ServerAliveCountMax=3`,
`AddressFamily=inet`, `ControlPath=none`, `ControlMaster=no`, `-L`/`-N`, plus `-C` <!-- wokeignore:rule=master -->
when compression is on). Anything `ssh` can reach **non-interactively** works.
`ssh_host` accepts `host`, `host.fqdn`, an `~/.ssh/config` alias, or
`user@host`, and rejects any segment starting with `-` (ssh option-injection
guard).

**Multiplexing is pinned off; everything else is inherited.** A tunnel is a
supervised foreground child, and a forward the gateway cannot supervise or kill
reports as `ssh exited with code 0` while it is in fact still serving. A
multiplexed session does exactly that — ssh hands the forward to an existing
shared connection and exits. `ControlPath=none` is the enforcement: with no path
resolved there is no socket to join. `ControlMaster=no` states the policy, and <!-- wokeignore:rule=master -->
is not sufficient alone — an inherited `ControlPath` still routes into a shared
connection.

Everything else per-host — `User`, `IdentityFile`, `Port`, `ProxyJump`,
`ProxyCommand` — is still inherited from `~/.ssh/config`; the registry carries no
inline equivalents and depends on that. **Pinning a directive the user may also
set is not free**: ssh takes the first value obtained and reads the command line
first, so a pinned `-o` silently discards theirs. The two multiplexing pins are
safe because a supervised tunnel must never share a connection, but the same
move on, say, `IgnoreUnknown` would drop the pattern a cross-platform config
relies on and turn a working setup into `Bad configuration option`.

The diagnostics probes are a different case and are left alone: they are
one-shot commands whose exit status is the whole result, with no forward to own.

### Dev host / home server (primary)

Use your SSH config alias or `user@hostname`. As long as a key in your
`ssh-agent` (or the default identity) covers auth, `BatchMode` succeeds without
prompting and no key path is needed.

### EC2 (and other key-based hosts)

EC2 differs from a directly-reachable dev host in three ways that matter here:

| Aspect | Direct dev host | EC2 |
|--------|-----------------|-----|
| Auth | key in `ssh-agent` / default identity | key pair (`-i key.pem`), or SSM Session Manager |
| Login user | resolved by your ssh config | `ec2-user`, `ubuntu`, `admin`, and so on: must be explicit |
| Reachability | direct | often via a bastion (ProxyJump) or SSM-only (no public SSH) |

**Recommended: configure an SSH alias.** Because `ssh_host` accepts an alias, put
the EC2-specific bits in `~/.ssh/config` on the **hub** and reference the alias.
The fixed `ssh <alias> ...` argv inherits all of it:

```ssh-config
# ~/.ssh/config on the hub
Host my-ec2
  HostName ec2-1-2-3-4.compute-1.amazonaws.com
  User ec2-user
  IdentityFile ~/.ssh/my-key.pem
  # Optional: reach a private instance through a bastion ...
  ProxyJump bastion-host
  # ... or via SSM Session Manager (no inbound SSH needed):
  # ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters portNumber=%p"
```

Then add an instance with **SSH host / alias = `my-ec2`**. Prerequisites on the
hub: a passphrase-less key (or an `ssh-agent` already holding it, since
`BatchMode` will not prompt), and `kirocrew` installed with a gateway running on
the instance's loopback port.

Simpler cases work without an alias: `ec2-user@10.0.1.5` and
`ubuntu@ec2-1-2-3-4.compute-1.amazonaws.com` are both accepted `ssh_host`
values, provided the matching key is the default identity or in the agent.

**The cloud launcher registers instances here.** `kirocrew cloud launch`
best-effort registers the box it created in this registry using the **native SSM
transport** — `connection_method="ssm"` with the EC2 instance id as `ssm_target`,
plus the launcher's `aws_profile`/`aws_region` (`cloud/connect.py:register_instance`).
The dashboard then tunnels, refreshes tokens, and self-heals the box over SSM with
no SSH key, no inbound port, and no hand-edited `~/.ssh/config`. `kirocrew cloud
destroy` unregisters it (matched by `ssm_target`) after deletion confirms. This is
why `cloud/connect.py` cites this section, and why its numbering must not move.
The legacy `ssm_proxy_ssh_host` helper (registering the id as `ssh_host` behind an
`~/.ssh/config` `ProxyCommand`) is retained for reference only and is no longer
used by the managed path.

### What is reachable through which mechanism

| Need | Where it goes |
|------|---------------|
| Custom login user | `user@host` in `ssh_host`, or `User` in an ssh-config `Host` block |
| FQDN / IP target | direct `ssh_host` value |
| Identity file | `IdentityFile` in an ssh-config `Host` block (there is no inline field) |
| Non-22 SSH port | `Port` in an ssh-config `Host` block (there is no inline field) |
| Bastion / ProxyJump | `ProxyJump` / `ProxyCommand` in an ssh-config `Host` block |
| SSM-only instances | `ProxyCommand` with `aws ssm start-session` |

The registry deliberately carries no inline `-i` / `-p` / `-J` fields. The
ssh-config alias path covers every case above, including bastions and SSM, which
inline flags could not express, and it keeps the hub's argv fixed: a
user-controlled `-i` path would be a new injection surface on a command line
whose current variable parts are all charset-bound literals.

---

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Settings → Instances shows the opt-in card | `instances.enabled` is false. Set it and restart. |
| Enabled but the panel says "not active" | The flag was set after the gateway started; the SSH manager is created at startup only. Restart. |
| Iframe is blank or black | The pane's embedded SPA never announced readiness within 15s, so the error panel with **Retry** appears (Retry force-reloads even an identical src). An iframe reports no load error to its parent, so this watchdog is the only signal. |
| Connect fails with an SSH auth error | Refresh your SSH credentials (re-add the key to `ssh-agent`); `BatchMode` never prompts, so a missing credential is an immediate failure. Tunnels self-heal once auth is restored. |
| Connect fails for another reason | Use **Diagnose**. The ladder reports the first broken link: `ssh_unreachable` (check SSH access or the host alias), `remote_down` (remote gateway not listening), `not_connected` (SSH and remote are fine, this instance has no tunnel yet: click Connect), or `tunnel_down` (reconnect). |
| "local port N is already in use" | The forward mirrors the remote port, so two instances cannot share one. Change this instance's remote port (and the remote gateway's own port to match), or stop whatever holds the port. |
| Instance keeps dropping | The health probe plus 2-tier self-heal retry over roughly a two-minute window (8 attempts, capped-exponential backoff). Tune `instances.max_recovery_attempts` / `recover_backoff_max_secs` / `probe_failure_threshold`; both recovery values are clamped so they cannot loop indefinitely. If self-heal gives up, diagnosis runs automatically. Check the remote gateway and SSH stability. |
| A pane vanished from the warm set but its tab is still there | It was LRU-evicted (warm set full). The tunnel is untouched: clicking the tab re-warms it. Raise `instances.warm_set_cap` if you want more panes resident. |
| Every token mint fails on one remote, though its gateway is healthy | The remote's `~/.local/bin/kirocrew` probably points at an uninstalled checkout. See §12: the run-marker is what makes mint follow the *running* gateway's install. |

---

## 11. Input validation (`validation.py`)

`instances/validation.py` is the **authoritative** injection guard for the two
user-controlled strings that reach an `ssh` command line. It lives next to the
tunnel manager rather than in the registry on purpose: the registry's
`_SSH_HOST_RE` / `_REMOTE_BIN_RE` checks are an *early reject* for obviously
malformed input at add/update time, while these functions run immediately before
each command line is built, which is the only point where the value is actually
dangerous. That ordering matters because the registry's load path
(`Instance.from_dict`) is deliberately tolerant and does **not** validate, so a
hand-edited or hand-migrated `instances.json` can hold anything at all until one
of these functions sees it.

Two distinct attacks, two distinct rules:

- `validate_ssh_host()` closes **ssh option injection**. Even with no local
  shell, an `ssh_host` like `-oProxyCommand=...` is parsed by ssh as an *option*
  and can run an arbitrary local command. It therefore rejects an empty value,
  anything over 255 chars, more than one `@`, an empty user or host segment, any
  segment beginning with `-`, and any character outside
  `[A-Za-z0-9._-]` (with the segment required to start with a letter, digit or
  underscore). It returns the stripped host so callers use the validated form.
- `validate_remote_bin()` closes **remote shell injection**. `remote_bin` is
  embedded, double-quoted, into the single command string the *remote* shell
  evaluates, so it forbids every shell metacharacter, `$` included (no command
  substitution or expansion the gateway does not control), and bounds the value
  to 512 chars of `[A-Za-z0-9._/~ -]`. An empty string is legal and means "use
  the candidate search".

Failures raise `SshValidationError` (a `ValueError`).

Where it is called, and what each caller does with a rejection:

| Caller | Behavior on rejection |
|--------|----------------------|
| `SshTunnelManager.connect()` | Returns an ERROR status carrying "invalid ssh settings", retained in `_last_error` so the tab can explain itself. |
| `SshTunnelManager._recover()` | Aborts self-heal for that instance with a warning (no point retrying an unusable record). |
| `SshTunnelManager._refresh_token_once()` | Aborts the refresh with a warning. |
| `SshTunnelManager.restart_remote()` | Returns `{ok: false, message: "invalid ssh settings: ..."}`. |
| `diagnostics.diagnose_instance()` | Short-circuits to an `unknown` diagnosis with a clear reason, **before** spawning any `ssh`. |

The remaining variable parts of the remote command are bounded by their own
validators in `token_mint.py`: `_validate_ttl` (`<1-4 digits>[hm]`) and
`_validate_port` (an int in 1-65535). The bin candidates and the data-home path
segments are trusted module constants.

---

## 12. The gateway run-marker (`run_marker.py`)

`instances/run_marker.py` writes and reads
`<data-home>/run/gateway-<port>.bin` (the running gateway's own `kirocrew`
launcher path) and `<data-home>/run/gateway-<port>.pid` (its pid). It has two
unrelated consumers, and separating them is the point of the module.

### Consumer 1: remote token mint targets the running gateway's install

Token mint SSHes to the remote and resolves `kirocrew` from a fixed PATH
candidate list whose first entry is `$HOME/.local/bin/kirocrew`. When that
launcher symlinks into an *uninstalled* checkout (no `.venv`), every mint fails
even though the gateway itself is healthy, because the gateway runs from a
different venv. Rebuilding and restarting the gateway does not fix mint, since
mint never consults the gateway's own install, and the refresh loop then fails on
every cycle, which surfaces to the user as a pane that periodically disconnects
and reconnects.

The fix: at startup the gateway records the absolute path to *its own* launcher,
keyed by the port it serves. The mint shell snippet reads that marker first and,
when it names an executable file, `exec`s it, so mint uses the same venv as the
live gateway. The snippet probes three data homes in priority order, since the
remote's non-interactive SSH shell usually does not export `KIROCREW_HOME`:

1. `$KIROCREW_HOME` when set and non-empty,
2. `$HOME/<CONFIG_DIR_NAME>` (the current default, `.kiro/crew`),
3. `$HOME/<LEGACY_CONFIG_DIR_NAME>` (`.kirocrew`, for a not-yet-migrated remote).

Those two home segments are **interpolated from the shared
`kiro_crew.config.paths` constants**, the same ones the marker *writer* derives
its default from, so reader and writer cannot drift apart on a future data-home
rename. An absent or stale marker, or one that does not name an executable, falls
through to the candidate search, so nothing regresses on an older remote. An
explicit `remote_bin` is never overridden by the marker: it is the user's
deliberate choice.

`restart_remote()` resolves `kirocrew restart` through the same path, keyed by
the instance's `remote_port`.

The launcher path is derived from `sys.executable`'s sibling console script
(`kirocrew`, or `kirocrew.exe` on Windows) and is deliberately **not** resolved
through symlinks, because the console script sits next to the possibly-symlinked
interpreter in the venv's `bin/`, not next to the real interpreter. When no such
script exists (a source-tree `python -m kiro_crew` launch) the marker is written
**empty**: the mint clause requires a non-empty executable path so an empty
marker is inert there, but the *filename* still matters to consumer 2.

### Consumer 2: zero-config client port discovery

The marker's filename advertises which port a gateway serves, so `marker_ports()`
lets a local client command (`token` / `status` / `logout` / `stop`, via
`cli_server.resolve_client_port`) find a gateway on a non-default port with no
configuration. That path reads only the filename and ignores marker *contents*
entirely. Resolution order is `--port`, then `KIROCREW_PORT`, then a port named
by `dashboard.url`, then the sole gateway-owned marker, then the default 5476.

**A marker is not proof a gateway is there.** `clear_marker()` runs only on
graceful shutdown, so a crash or SIGKILL leaves the file behind and an unrelated
process may since have bound that port. Because client commands send the local
secret (`X-Local-Secret`) to whatever answers, the consumer must verify the
listener before trusting a discovered port. `cli_server._gateway_owns_port()`
does that in four fail-closed steps: the recorded pid must exist, must be among
`platform_compat.find_listening_pids(port)`, must be owned by the caller's uid
(which closes pid recycling into another user's process), and must look like a
gateway by argv (defense in depth only, never the sole proof). Discovery is
skipped outright on non-POSIX hosts, where no owner can be reported and the
file-permission argument does not hold, so Windows users keep `--port` /
`KIROCREW_PORT`. This module deliberately offers no bare "is something
listening" helper, so no caller can mistake reachability for identity.

The live gateway prunes markers naming other ports on startup: a gateway is a
singleton per data home, so any other marker belongs to a crashed earlier run,
and each stale one costs a future client command a listener lookup.

### Why `run/` is on the sensitive-path floor

The marker names a path that the gateway `exec`s on the remote host **outside**
the agent sandbox, and `run/` also holds the sandbox launcher scripts. An agent
that could write into this dir could point a marker at an attacker-controlled
binary and get it executed unsandboxed on the next routine token refresh: a
reachable sandbox escape, which the owner and `-x` checks do not stop because
agent writes run as the same user. `run/` is therefore classified read+write
sensitive in `security._SENSITIVE_HOME_DIRS`, under every known data-home prefix.
The dir is created `0700` (re-applied on an existing dir, since `exist_ok` does
not re-apply mode) and both files are written `0600` through the shared
`atomic_write` helper, whose unique `mkstemp` + `os.replace` closes the
same-user symlink TOCTOU a predictable `<name>.tmp` would leave open. Every
legitimate writer opens these paths directly and does not route through the file
gate, so gateway startup and spawn are unaffected.

---

## 13. The SSM connection method (`connection_method`)

Each instance record carries a `connection_method`: `"ssh"` (default) or `"ssm"`.
SSM tunnels over AWS Systems Manager Session Manager, so it needs no inbound
port, no sshd and no distributed key — reachability is an IAM decision
(`ssm:StartSession` on the instance ARN) rather than a network one.

| Method | Tunnel command | Client prerequisites | Mint path |
|--------|----------------|----------------------|-----------|
| `ssh` (default) | `ssh -N -L 127.0.0.1:LP:127.0.0.1:RP <ssh_host>` | non-interactive SSH access | `ssh <host> kirocrew token` |
| `ssm` | `aws ssm start-session --document-name AWS-StartPortForwardingSession --target <ssm_target> --parameters portNumber=RP,localPortNumber=LP` | AWS CLI + `session-manager-plugin`; `ssm:StartSession`, `ssm:SendCommand`, `ssm:GetCommandInvocation` | `aws ssm send-command` → `kirocrew token` |

Records are back-compatible: an `instances.json` written before this feature has
no `connection_method` and loads as `"ssh"`.

SSM-only registry fields: `ssm_target` (an EC2 `i-…` or SSM managed-instance
`mi-…` id), plus optional `aws_profile`, `aws_region` and `ssm_run_as`. Only the
profile **name** is persisted — never a credential; the AWS CLI resolves
credentials via its own provider chain.

`ssm_run_as` is the **remote POSIX user** SSM commands run as: `cloud.ssm.run_command`
wraps every remote command in `sudo -u <user> -i bash`, and its default
(`ec2-user`) is a *launcher* assumption that holds only for provisioned AL2023
boxes. Without a per-instance override, an Ubuntu AMI would bring the tunnel up
and then fail the mint — and, because the readiness probe also runs through that
same wrapper, `diagnose_instance_ssm` would report `remote_down` for a perfectly
healthy remote gateway. The field defaults to `ec2-user` (so existing records and
launcher-provisioned boxes are unaffected), is charset-validated as a Unix
username like the other SSM coordinates, and an empty value resolves to the
default rather than emitting a bare `sudo -u ''`.

### One state machine, two transports

`_SshTunnel` builds either argv from the same class, and `_TransportParams`
(resolved once per operation by `_resolve_transport`) carries the validated
per-transport values so `connect` / `_rebuild` / `_recover` /
`_refresh_token_once` / `restart_remote` share one code path. The health probe,
2-tier self-heal, proactive refresh, stored-token liveness probe and startup
auto-revive are transport-agnostic.

Two SSM-specific behaviours:

- **Process-tree teardown (all platforms).** The SSM child gets process-group
  isolation at spawn — `start_new_session` on POSIX, `CREATE_NEW_PROCESS_GROUP`
  on Windows, passed explicitly per the `platform_compat` recipe — and teardown
  reaps the whole tree through `platform_compat.kill_process_tree` (`killpg`
  POSIX / `taskkill /T` Windows). This matters because the
  `session-manager-plugin` grandchild is what actually holds the forwarded port:
  `terminate()` on the `aws` wrapper alone orphans it and wedges the port. Doing
  this with raw `os.killpg`/`os.getpgid` would silently degrade to
  wrapper-only termination on Windows, which Kiro Crew supports.
- **Readiness timeout.** `session-manager-plugin` completes a WebSocket handshake
  with the SSM service before binding, so the SSM transport uses a longer default
  connect timeout than a direct ssh TCP connect. An explicit caller-supplied
  timeout still wins for both.

`_ssm_exit_error` classifies the child's exit with SSM vocabulary (expired
credentials, `ssm:StartSession` denial, missing plugin, target not a connected
managed node, local bind conflict) rather than running SSM stderr through the ssh
auth/transport matchers, which would mislabel an `AccessDenied` as an ssh auth
failure.

### Diagnosis ladder

`diagnose_instance_ssm` mirrors the SSH ladder with an SSM first rung, so an
offline agent is not reported as a dead remote gateway:

1. managed node online? (`describe-instance-information`) → no ⇒ `ssm_unreachable`
2. remote dashboard up? (`send-command` + curl on the remote loopback) → no ⇒ `remote_down`
3. local forward reachable? → no ⇒ `tunnel_down`, else `ok`

`ssm_unreachable` is a new diagnosis code; the shared rungs reuse SSM-worded
reasons so the copy never tells an SSM user to "check SSH access".

### Reuse of the launcher's SSM primitives

Argv building and remote execution delegate to `cloud.ssm`
(`build_port_forward_argv`, `run_command`) rather than duplicating them, so the
two features cannot drift on the SSM document or parameter shape. Those calls run
in the gateway process, which has no `KIROCREW_SESSION_KEY`, so the launcher's
agent-session chokepoint does not apply; the `hooks.py` denied-command list gates
agent *tool* calls and likewise does not gate the gateway's own children.

### Trade-off: the mint transits SSM command history

The mint runs over `send-command`, so the token appears in that invocation's
output, which SSM retains for up to 30 days and is readable with
`ssm:GetCommandInvocation`. It is **not** in CloudTrail (which records the API
call, not the output), and no S3/CloudWatch output destination is configured.

Bounded by: the token is TTL-capped and only usable against the remote's
loopback, so *using* it requires `ssm:StartSession` — a superset of the access
needed to read the history. The generated launcher policy also withholds
`ssm:ListCommandInvocations`, so a holder cannot enumerate command ids hunting
for tokens. The SSH transport has no equivalent exposure. This mirrors the
accepted posture in `cloud/connect.py::mint_token`.

`ssm_token_mint.py` is listed in `security_posture.NON_EGRESS_REDACTION_MODULES`
alongside its SSH sibling: it redacts remote output on the way into an exception,
which is not an egress boundary.

### Interaction with §9

§9 documents reaching an SSM-only instance through an `~/.ssh/config`
`ProxyCommand` — still valid as a manual option, and still `connection_method="ssh"`:
the reachability lives in ssh config and Kiro Crew is unaware of it.
`connection_method="ssm"` is the direct alternative, requiring neither sshd nor a
key on the remote — and it is now what `cloud/connect.py`'s registry integration
uses (`register_instance` sets `connection_method="ssm"`, `ssm_target=<instance-id>`).
The legacy `ssm_proxy_ssh_host` helper is kept for reference only.

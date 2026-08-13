# Remote Hosts and Mobile Access

Run Kiro Crew on an always-on host (a VPS, a cloud VM, a Linux desktop, a spare
box) so the chat bot, cron jobs, and task runner keep working while your laptop
sleeps. Then reach the dashboard from wherever you are: an SSH tunnel for a
laptop, a named HTTPS tunnel for a phone.

Four parts, in the order you will need them:

1. [Run 24/7 on a remote host](#1-run-247-on-a-remote-host): host setup and install
2. [Reach it from a laptop or phone](#2-reach-it-from-a-laptop-or-phone): SSH
   tunnels, HTTPS tunnels, session lifetimes, installing as an app
3. [Keep it alive as a service](#3-keep-it-alive-as-a-service): systemd /
   launchd, plus the awkward-host recipe
4. [Troubleshooting](#4-troubleshooting)

---

## 1. Run 24/7 on a remote host

### Host requirements

- **OS**: any modern Linux distribution (Ubuntu 22.04+, Debian 12+, Fedora,
  CentOS Stream / RHEL 8+, CentOS 7, Amazon Linux 2 / 2023). macOS works too,
  with launchd instead of systemd.
- **Python**: 3.10 or newer (`setup.cfg` sets `python_requires = >=3.10`).
- **Node.js**: needed to build the dashboard bundle. `website/package.json`
  declares `"node": ">=22"`; `kirocrew doctor` warns below Node 22.
- **RAM**: there is no single published floor, because the footprint scales with
  concurrent sessions, spawned subagents, and MCP servers. Two figures from the
  code give you the shape of it: `acp/runtime.py` recycles a long-lived
  multiplexed backend once it passes 500 MiB RSS
  (`_DEFAULT_MAX_RSS_MB = 500.0`), and the embedding model is loaded in-process
  from a ~610MB GGUF file. Measure your own steady state, then leave headroom:
  MCP cold starts and heavy tool calls spike well above it.
- **CPU**: a couple of vCPUs is fine for a single user; extra cores help with
  CPU-intensive tool calls and parallel subagent execution.
- **Architecture**: x86_64 or arm64.

### Install the basics

```bash
# Debian / Ubuntu (python3-venv is a separate package here)
sudo apt-get update && sudo apt-get install -y git tmux python3 python3-pip python3-venv

# Fedora / CentOS Stream / RHEL 8+ / Amazon Linux 2023 (python3 may be 3.9;
# python3.11 gives the 3.10+ the backend needs)
sudo dnf install -y git tmux python3.11 python3.11-pip

# CentOS 7 / RHEL 7 (yum; base repos ship only Python 3.6, which is too old —
# install a newer interpreter yourself first, e.g. mise; see below)
sudo yum install -y git tmux
curl https://mise.run | sh && mise use -g python@3.12
```

The `curl … | sh` installer performs this distro Python bootstrap for you. On
CentOS 7 and older Ubuntu, where no base-repo package supplies Python 3.10+, it
uses an already-installed [mise](https://mise.jdx.dev/) if you have one and
otherwise stops with instructions — the signed installer does not pipe an
unsigned script into a shell, so install mise yourself first
(`curl https://mise.run | sh`) if you want that path. Install Node.js from your
distro, [nodejs.org](https://nodejs.org/), or a
version manager such as [nvm](https://github.com/nvm-sh/nvm). `tmux` is handy
for a first smoke test before you install the service.

Set your git identity if you plan to let the agent work on repos on this host:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

### Install the agent backend

Kiro Crew drives the `kiro-cli` agent over ACP, and it is the only provider
(`agent.provider = acp`). Install `kiro-cli` per its own docs, put it on your
`PATH`, and log in:

```bash
kiro-cli login
```

The credentials Kiro Crew itself reads from `~/.kiro/crew/.env` are chat-platform
and owner-identity credentials (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`KIROCREW_OWNER_ID`, and the equivalents for Discord / Telegram / Teams / WeCom
/ Webex). There is no model API key to configure: `kiro-cli` owns the model
credential, and `kiro-cli login` is where it is set.

### Install Kiro Crew

Same two steps as a local machine (Python backend plus the React dashboard
bundle). See the [install guide](install.md) for the full walkthrough:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew

# Build the frontend bundle and stage it into the package
cd website && npm install && npm run build && cd ..
cp -R website/dist src/kiro_crew/static/dist

# Install the backend (ships the bundled dashboard)
pip install .

# Configure
kirocrew setup
kirocrew doctor            # verify everything, including kiro-cli and Node
```

`kirocrew setup` writes the resolved project directory to
`~/.kiro/crew/project_dir` so the CLI works from any working directory.

Embeddings need no setup step at all: they are always on and run in-process from
a bundled llama-cpp-python, and the model file downloads in the background on
first gateway start. There is no separate embedding service to install.

### Smoke test in tmux

Before you commit to a service unit, confirm the gateway actually boots:

```bash
tmux new -s kirocrew
kirocrew gateway
# Ctrl+B, D to detach; reattach with: tmux attach -t kirocrew
```

tmux survives an SSH disconnect but does **not** auto-restart on crash or
auto-start on reboot. Move to [a real service](#3-keep-it-alive-as-a-service)
once the smoke test passes, and **kill the tmux session first**: only one
gateway can own the dashboard port, which defaults to `5476`
(`config/loader.py` `_DEFAULT_PORT`, overridable with the `KIROCREW_PORT`
environment variable). Two gateways on one port means the second one fails to
bind.

```bash
tmux kill-session -t kirocrew
```

### Move your state to the new host

A fresh install starts with no memories, preferences, lessons, or agent config.
`scripts/sync-to-remote.sh` copies them from your local machine. Run it **from
the local machine**:

```bash
scripts/sync-to-remote.sh user@your-host.example.com

# Non-default dashboard port on the remote (multi-host setups)
scripts/sync-to-remote.sh user@your-host.example.com 7779

# Preview without transferring
scripts/sync-to-remote.sh --dry-run

scripts/sync-to-remote.sh --help
```

It is one-way (local overwrites remote). It takes an atomic SQLite `.backup` of
`memory.db` rather than copying a live WAL, which would land a torn database;
patches the remote `config.json` to `dashboard.url = http://localhost:<port>`
with `auto_open_browser` off (a headless host has no browser to open); and syncs
`sessions/` so your chat history shows up on the remote dashboard. `--dry-run`
prints every transfer without performing it. Set `DEFAULT_HOST` at the top of the
script if you do not want to pass the target every time.

What matters, and where it lives:

| Category | Path | Why |
|---|---|---|
| Structured memory | `~/.kiro/crew/workspace/memory/` | `preferences.md`, `projects.md`, daily `history/` |
| Vector + FTS databases | `~/.kiro/crew/memory.db`, `memory_index.db` | Semantic/episodic memory (`vector_memory.py`) and the FTS5 index (`memory.py`) |
| Lessons | `~/.kiro/crew/memory.db`, falling back to `lessons.jsonl` | See the note below |
| Config | `~/.kiro/crew/config.json` | Chat-platform tokens, model prefs, dashboard settings |
| Skills | `~/.kiro/crew/skills/` | Custom skill definitions |
| Webhook hooks | `~/.kiro/crew/hooks.json` | Script-hook definitions (`hooks.py` `ScriptHookStore`) |
| Cron jobs | `~/.kiro/crew/crons.json` | Scheduled recurring jobs (`cron.py`) |

> **Where lessons actually live.** Both stores exist and the vector store wins
> when it holds anything. `learn.py` appends to `<config_dir>/lessons.jsonl`,
> but `vector_memory.write_lesson()` stores lessons as semantic entries in
> `memory.db`, and `ContextBuilder` reads `get_lessons_context()` from the vector
> store first, only falling back to the JSONL when that comes back empty. So
> sync **both**: `memory.db` is authoritative on any host that has ever written
> a lesson natively, and the JSONL still carries lessons written before that.

What NOT to carry over:

- `~/.kiro/crew/kiro_pids.txt`, `kiro_session_pids.txt`, and the
  `session_pid_<pid>.txt` / `.sig` sidecars: live PID tracking
  (`session_pid.py`), meaningless on another host and actively misleading if
  copied.
- `~/.kiro/crew/security_events.jsonl`: the tamper-evident SEL audit chain
  (`sel.py`); it belongs to the host that wrote it.
- `~/.kiro/crew/.env`, `.local_secret`, `sel_hmac.key`: secrets. Re-enter the
  `.env` credentials with `kirocrew setup`; the other two are regenerated.

Keeping two hosts loosely in sync afterwards is just rsync (replace the SSH
target):

```bash
alias mc-sync='rsync -avz ~/.kiro/crew/workspace/memory/ user@your-host.example.com:~/.kiro/crew/workspace/memory/ \
  && rsync -avz ~/.kiro/crew/memory.db ~/.kiro/crew/memory_index.db user@your-host.example.com:~/.kiro/crew/'
```

Run it only while both gateways are stopped, or you will copy a live WAL-mode
database out from under a writer.

---

## 2. Reach it from a laptop or phone

The gateway binds `127.0.0.1` and stays there. `is_local_only()` in
`dashboard/urls.py` always returns `True` in this build, so **setting
`dashboard.url` does not widen the bind**: it only changes the host used in
generated links and adds that origin to the CSRF/WebSocket allowlist. The one
bind override is `KIROCREW_BIND` (an IP address, validated by
`bind_address_for()`), which exists for containers where published ports map to
a bridge interface; the official Docker image sets it. Both server paths mount
the token-auth middleware unconditionally, so a wider bind never exposes an
unauthenticated surface beyond the three token-free liveness probes
(`/api/health`, `/api/live`, `/api/ready`).

Everything below therefore works by putting something in front of loopback.

### SSH tunnel (laptop)

```bash
ssh -L 5476:localhost:5476 user@your-host.example.com
```

Then open `http://localhost:5476` locally. For a non-default remote port, match
both sides:

```bash
ssh -N  -L 7779:localhost:7779 user@your-host.example.com   # foreground, no shell
ssh -fN -L 7779:localhost:7779 user@your-host.example.com   # background
```

To get the tunnel on every connection, add to your local `~/.ssh/config`:

```
Host your-host.example.com
    LocalForward 5476 localhost:5476
```

Works on macOS, Linux, and Windows (OpenSSH ships with Windows 10+; the config
file is at `%USERPROFILE%\.ssh\config`).

If your browser reaches the dashboard on a *different* local port than the
remote one (`ssh -L 8777:localhost:5476`), the browser sends Origin
`http://localhost:8777`, which is not in the default allowlist. Opt that port in
with `KIROCREW_ALLOWED_LOOPBACK_PORTS` on the gateway host; the CSRF check
deliberately does not blanket-trust every loopback port, because a malicious
local page on an arbitrary port would otherwise pass it.

### Named HTTPS tunnel (phone)

A phone cannot open an SSH port-forward, so put a tunnel provider in front of
the loopback port and point the bot's links at it. Use a **named** tunnel, not
an ephemeral one: a named tunnel keeps the same URL across restarts, so
`dashboard.url` stays correct and you do not have to re-edit config and restart
the gateway every time the tunnel reconnects. An ephemeral tunnel mints a fresh
random hostname on each start, which silently invalidates both `dashboard.url`
and the origin allowlist derived from it.

With [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```bash
cloudflared tunnel login
cloudflared tunnel create kirocrew
cloudflared tunnel route dns kirocrew kirocrew.example.com
cloudflared tunnel --url http://localhost:5476 run kirocrew
```

With [ngrok](https://ngrok.com/docs) the equivalent is
`ngrok http --domain=kirocrew.example.com 5476`.

**If you use Tailscale, prefer `tailscale serve` over Funnel.** The two are not
the same and the difference is the whole security story:

- [`tailscale serve`](https://tailscale.com/kb/1242/tailscale-serve) publishes the
  dashboard **only inside your tailnet** — nothing is reachable from the public
  internet, you get TLS and a stable MagicDNS hostname, and who can reach it is
  governed by your tailnet ACLs. This is the better answer for the phone case:
  ```bash
  kirocrew config set dashboard.tailscale.enabled true   # once per machine
  kirocrew tailnet up
  kirocrew restart
  kirocrew token                                         # the link to open on the phone
  ```
  `kirocrew tailnet up` runs `tailscale serve` for you — HTTPS on 443 in front of
  the dashboard's loopback port — and prints the URL to open on your phone. That is
  the half that used to be an undocumented command you had to know and type.

  The URL `tailnet up` prints carries **no session**, so opening it alone lands on a
  login you cannot complete from the phone. `kirocrew token` is the step that hands you
  one: with the setting on it prints a `https://<your-tailnet-name>/?token=…` line
  alongside the loopback one. Run it after the restart, since the gateway resolves the
  trusted origin at startup.

  It prints that line only when **this** dashboard is the verified service behind that
  name — `tailscale serve` must be proxying 443 to this gateway's port. That URL carries
  a session, so handing it out while some other service holds the name would deliver your
  session to it. Since `tailnet up` refuses to overwrite a foreign `443/` handler, a host
  can legitimately have the setting on and a resolvable name while the mount belongs to
  something else; in that case `token` says so instead of printing the line.

  It **checks** `dashboard.tailscale.enabled` rather than setting it, and refuses
  when the flag is off. Two reasons. Publishing a dashboard the gateway will not
  trust only puts a URL on your tailnet that answers 403, which is the confusing
  state this command exists to remove — so the check comes before the publish, not
  after it. And writing that flag from a second process means a read-modify-write of
  your config file that can silently drop a settings change the dashboard saved a
  moment earlier; the flag is one-time setup, so paying for it once with an explicit
  `config set` is cheaper than carrying that risk on every run.

  `tailnet up` also needs to know which port to publish, and it will not guess: it
  takes `--port`, `KIROCREW_PORT`, or the run marker of a gateway that is actually
  listening. With none of those it refuses rather than publishing whatever happens to
  hold the configured port, because `tailscale serve` will expose an unrelated local
  service to every device on your tailnet just as readily as the dashboard.

  Changing serve configuration is daemon state, so on Linux it usually needs root
  or a one-time grant: `sudo tailscale set --operator=$USER`. If the publish is
  refused, the command prints what Tailscale itself said rather than a generic
  failure.

  `kirocrew tailnet status` shows the three things that are independently
  required — whether the setting is on, whether a MagicDNS name resolves right
  now, and whether serve is actually pointing at this dashboard. Any one of them
  being wrong looks identical from your phone. `kirocrew tailnet down` stops
  publishing and leaves the setting alone, since the trusted origin is unreachable
  without serve anyway.

  Both directions refuse rather than clobber: `tailscale serve` **replaces**
  whatever handler is at `443/`, and `off` removes it — so if you published
  something else there by hand, `up` and `down` both stop and print the command
  instead of overwriting or deleting your mapping.

  The setting reads your own MagicDNS name from the local Tailscale daemon once at
  startup and trusts `https://<that name>` as an origin, so you do **not** have to
  look the name up and hand-write `dashboard.url`. Because it is resolved once, a
  restart is needed after publishing — and if the daemon comes up *after* the
  gateway, nothing is trusted until you restart again. If Tailscale is absent,
  stopped, or MagicDNS is off it contributes nothing and the dashboard starts
  exactly as before. It does not widen the network bind and does not change
  authentication — every request still needs a dashboard session.

  If you would rather do it by hand, the equivalent is:
  ```bash
  tailscale serve --bg --https 443 http://127.0.0.1:5476
  kirocrew config set dashboard.tailscale.enabled true
  kirocrew restart
  ```
  `dashboard.tailscale.enabled` reads your own MagicDNS name from the local
  Tailscale daemon once at startup and trusts `https://<that name>` as an origin,
  so you do **not** have to look the name up and hand-write `dashboard.url`. If
  Tailscale is absent, stopped, or MagicDNS is off it contributes nothing and the
  dashboard starts exactly as before. It does not widen the network bind and does
  not change authentication — every request still needs a dashboard session.

  Optionally, opt in to **identity-pinned sessions** so the session pin binds to
  your device's daemon-verified tailnet identity instead of the tunnel's shared
  loopback address (and the audit trail names your login instead of
  `127.0.0.1`):
  ```json
  {
    "dashboard": {
      "tailscale": {
        "enabled": true,
        "trust_identity": true,
        "allowed_logins": ["you@example.com"],
        "pin_scope": "node"
      }
    }
  }
  ```
  `allowed_logins` is mandatory — `trust_identity` with an empty list is refused
  at startup, and a verified peer whose login is not listed is denied. The
  identity is resolved from the local `tailscale whois` daemon, never from a
  header. When identity cannot be resolved (daemon stopped, Tailscale absent,
  Windows — where resolution is not yet verified), a NEW login falls back to
  the ordinary token path, while a session already pinned to a tailnet
  identity is refused ("tailnet identity unverified") until the daemon
  answers again — an unverified proxied request can never satisfy an
  identity pin. `pin_scope: "node"` (the default) means a leaked session cookie is
  usable only from the original device; `"login"` relaxes that to any device
  carrying your Tailscale identity. An ACL-tagged node is always pinned at node
  scope regardless of `pin_scope`, because every tagged device shares the
  literal `tagged-devices` login. Identity trust does **not** unlock the
  config-write or secret-reveal surfaces: a tailnet request is still a proxied
  request and those stay read-only.
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) does the opposite: it
  puts the service **on the public internet**, like cloudflared and ngrok. Use it
  only if you actually want public ingress.

A shared corporate tailnet is not a private network — every member who can reach
the Serve endpoint is inside your trust boundary, so keep the ACL narrow.

For the tunnel providers above (cloudflared / ngrok / Funnel) set the URL in
`~/.kiro/crew/config.json` and restart:

```json
{
  "dashboard": {
    "url": "https://kirocrew.example.com"
  }
}
```

```bash
kirocrew restart
```

`dashboard.url` does two things: chat-platform links are generated against that
host instead of `localhost`, and its origin is added to the allowed-origin set
so the CSRF check and the WebSocket handshake accept requests arriving through
the tunnel. Keep the tunnel process running (tmux, or the provider's own
service installer such as `cloudflared service install`).

> **A tunnel puts your dashboard on the public internet.** Token auth is always
> mounted, tokens are HMAC-signed, and the DNS-rebinding `Host` barrier applies to
> every non-probe request.
>
> **Do not count on IP pinning here.** A token is pinned to the address that first
> used it — but every tunnel above runs on *this* host and connects to the gateway
> from loopback, so the address it pins to is the tunnel process, not your phone.
> One pin is then satisfied by anyone who reaches the dashboard through that same
> tunnel, for the life of the session — up to 20 hours per access cookie, and
> indefinitely if the browser keeps rotating its refresh cookie. For the same
> reason the audit trail records the caller as `127.0.0.1` rather than a client
> address. Security Posture → Dashboard token auth reports which of the two states
> you are actually in. The one exception is `tailscale serve` **with
> `trust_identity` configured** (see above): there the pin binds to the
> daemon-verified peer identity and is per-client again. For cloudflared, ngrok,
> and Funnel the pin is always shared.
>
> So the real control for a tunnelled dashboard is the provider's own auth layer
> (Cloudflare Access, or `tailscale serve`, which keeps the service inside your
> tailnet instead of publishing it) — not the pin, and **not** short session
> lifetimes either. Refresh cookies deliberately trade the 20-hour ceiling for a
> 30-day sliding window (see [Session duration](#session-duration)), so a
> tunnelled browser that keeps rotating stays authenticated for as long as it
> keeps being used.
>
> **`kirocrew logout` ends those sessions.** It bumps a persisted revocation
> generation that both access and refresh tokens embed, so established access
> cookies and refresh chains alike are rejected on their next request. The
> dashboard's own sign-out (`POST /api/auth/logout`) is the narrower control:
> it revokes only the chain belonging to the browser that calls it. Restarting
> the gateway ends nothing: cookies are self-contained, signed with the
> persistent `token_signing.key`, and the revocation generation is reloaded
> unchanged. **To cut off remote access entirely, also revoke at the provider's
> auth layer or tear the tunnel down** — logout ends Kiro Crew's sessions, not
> the tunnel itself.
> Note also that config-write and secret-reveal endpoints refuse tunnelled requests:
> `is_direct_local_request()` treats any request carrying `Forwarded` /
> `X-Forwarded-*` / `X-Real-IP` as remote, and every standard tunnel and reverse
> proxy attaches those.

### Getting a link on your phone

1. In your Kiro Crew DM, send `/kirocrew dashboard` (or `/kirocrew dashboard 6h`).
2. The bot DMs you `https://<tunnel-url>/?token=...`.
3. Tap it. The link exchanges the token for an access cookie **and** a 30-day
   refresh cookie, so this is not a daily ritual — see
   [Session duration](#session-duration).

`kirocrew token` does the same thing from a shell on the gateway host.

### Using Chat on a phone or tablet

In the Chat composer, tap `+` to open the device's native Files picker and
attach an image or a regular file. This direct touch path does not open the
desktop-style command menu first.

### Session duration

Three clocks. The first two are signed into the access token payload
(`dashboard/token_auth.py`), the third into the refresh cookie
(`dashboard/refresh_tokens.py`):

| Clock | Value | What it governs |
|---|---|---|
| Link click window (`exp`) | 5 minutes (`LINK_WINDOW_SECS = 300`) | The presigned URL must be **opened** within this window |
| Access session TTL (`session_exp`) | 1 hour by default, 20 hours maximum (`MAX_SESSION_TTL_SECS = 20 * 3600`) | How long the access cookie the link mints stays valid |
| Refresh TTL | 30 days (`MAX_REFRESH_TTL_SECS = 30 * 86400`) | How long the dashboard can silently mint a new access cookie without a new link |

**You re-run `/kirocrew dashboard` or `kirocrew token` roughly once per 30 _idle_
days — not every 20 hours.** Opening the link sets two cookies, not one: the
access cookie plus an `mc_refresh_<port>` refresh cookie (HttpOnly,
path-restricted to `/api/auth`). The dashboard schedules a
`POST /api/auth/refresh` one hour before the access cookie expires (`LEAD_MS` in
`website/src/hooks/useRefreshScheduler.ts`), which rotates **both** cookies. If
the tab is hidden when that timer fires, the refresh defers until it is visible
again. If the access cookie has already expired by the time you open the
dashboard, `GET /api/auth/me` is denied (403 with `X-Auth-Required: true`) and the
frontend refreshes once and retries before any login UI appears — so a phone left
closed overnight still opens straight into a working session.

Each rotation mints a fresh refresh cookie with a **new** 30-day window, carrying
the same `chain_id` forward, and validation never consults the chain's original
creation time. The 30 days is therefore a **sliding idle window, not a hard
expiry**: open the dashboard at least once a month and you need never mint
another link.

The access-session numbers still govern the initial mint. The chat-command
default is 1 hour (`ttl = 3600` in `slack/events.py` and `slack/handler.py`);
pass a duration to raise it (`/kirocrew dashboard 6h`,
`/kirocrew dashboard 20h`). `parse_duration` accepts `<N>h` or `<N>m` and clamps
to the 20-hour ceiling, so asking for more silently gets you 20 hours rather than
an error. `kirocrew token` defaults straight to `20h`. The 5-minute click window
is not the session length: it only means a link left sitting in a DM overnight is
dead and you need a fresh one.

**When you do need a fresh link.** Four things end a refresh chain:

- **30 days idle** — nothing opened the dashboard inside the window.
- **Signing out in the dashboard** (`POST /api/auth/logout`) — revokes that
  browser's chain and denylists its access cookie, leaving other browsers'
  sessions alive.
- **`kirocrew logout`** — ends **all** sessions globally, refresh chains
  included: it bumps a persisted revocation generation that every access and
  refresh token embeds, so tokens minted before the logout are rejected.
- **Reuse detection** — a consumed refresh token replayed outside a 60-second
  same-IP grace window (`REFRESH_GRACE_SECS`) auto-revokes the entire chain
  (RFC 6819 §5.2.2.3). The frontend reports `refresh_chain_revoked` and stops
  scheduling refreshes; the mint screen appears once the remaining access session
  runs out.

Chains persist in `~/.kiro/crew/refresh_chains.json` (mode `0600`), so they
survive a gateway restart. On a gateway old enough to predate the feature,
`GET /api/auth/me` returns 404; the frontend logs once and falls back to the
20-hour URL-mint behaviour.

### Install as an app (PWA)

The dashboard ships a web app manifest
([`website/public/manifest.json`](../../website/public/manifest.json)) and
registers a service worker, so a phone can install it to the home screen and
launch it without browser chrome. Nothing needs enabling.

**HTTPS is what the service worker needs** — the install itself is looser.
Service workers only register in a secure context, so over a plain
`http://<host>:5476` from another device on the LAN you get the manifest but no
service worker. On **iOS Safari** that still installs and still launches
standalone; you only lose the offline shell described below. On **Android
Chrome** the promoted install flow expects a secure origin, so use HTTPS there
rather than relying on whatever manual shortcut path the current version offers.
Any option in [Named HTTPS tunnel (phone)](#named-https-tunnel-phone) provides a
secure context, and loopback counts as one too, which is why it works untunnelled
on the gateway host itself.

**On iOS Safari**, open the dashboard, tap Share → *Add to Home Screen*, then
launch from the new icon. The manifest's `"display": "standalone"` is what drops
Safari's address bar.

**The service worker provides a limited offline shell.**
[`website/public/sw.js`](../../website/public/sw.js) caches exactly `/` and
`/index.html`. For the paths below the worker declines to intercept, handing them
straight to the network; every other same-origin `GET` **is** intercepted,
network-first, but only the shell is ever cached:

| Path | Why the worker declines it |
|---|---|
| `/api`, `/apps/` | Gateway and app-backend responses must never be served stale |
| `/assets/` | Vite content-hashed bundles — HTTP immutable caching already covers them |
| `/vendor/`, `/fonts/`, `/sprites/` | Stable filenames, nothing to bust |
| `/logo.png`, `/static/` | Gateway-served brand assets; caching them strands a broken image across a gateway restart |

So offline you get the shell and its reconnecting state — and only while the
browser's own HTTP cache still holds the hashed `/assets/` bundles, which the
worker never caches. After a build that changes those hashes, the cached shell
references bundle URLs nothing has downloaded yet. Sessions, history, and
notifications are never served from disk.

**Two current limits**, documented here so they read as boundaries rather than
bugs:

- **Notifications need the app open.** The dashboard raises them through the
  foreground `Notification` constructor
  ([`website/src/hooks/useNativeNotification.ts`](../../website/src/hooks/useNativeNotification.ts))
  and there is no Web Push subscription, so nothing arrives while the installed
  app is closed or backgrounded. On iOS that constructor is unavailable inside an
  installed PWA at all — notifications there require
  `ServiceWorkerRegistration.showNotification()`. Tracked in
  [issue #2267](https://github.com/kirodotdev/KiroCrew/issues/2267); the Android
  symptom is [issue #1828](https://github.com/kirodotdev/KiroCrew/issues/1828).
- **Not edge-to-edge.** The shell sets neither `viewport-fit=cover` nor any
  `env(safe-area-inset-*)` padding, so on a notched device the installed app
  renders inside the safe area rather than filling the screen.

Installing changes nothing about authentication: the app carries the same cookies
the browser holds, on the same clocks as [Session duration](#session-duration).

### Persistent SSH tunnel on macOS (LaunchAgent)

A terminal-held tunnel dies with the terminal. A LaunchAgent survives reboots
and reconnects after sleep. A ready-made plist is at
[`assets/com.kirocrew.tunnel.plist`](assets/com.kirocrew.tunnel.plist):

```bash
cp docs/guides/assets/com.kirocrew.tunnel.plist ~/Library/LaunchAgents/
sed -i '' 's|ALIAS@DEV_DESKTOP_HOSTNAME|user@your-host.example.com|g' \
  ~/Library/LaunchAgents/com.kirocrew.tunnel.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kirocrew.tunnel.plist
```

Verify with the token-free liveness probe (`/api/status` needs a token,
`/api/health` does not):

```bash
curl -s http://localhost:5476/api/health     # {"ok": true, ...}
```

Manage it:

```bash
tail -f /tmp/kirocrew-tunnel.log
launchctl kickstart -k gui/$(id -u)/com.kirocrew.tunnel                            # restart
launchctl bootout gui/$(id -u)/com.kirocrew.tunnel                                 # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kirocrew.tunnel.plist  # start
```

The plist sets `ServerAliveInterval=30`, `ServerAliveCountMax=3`,
`ExitOnForwardFailure=yes`, and `KeepAlive`, so macOS restarts the tunnel after
a sleep/wake or network change; reconnect takes roughly 30 seconds. Use SSH
key-based authentication so the reconnect is passwordless.

### Raycast script (macOS, zsh)

Opens the tunnel if needed, mints a token on the remote, and opens the browser.

```zsh
#!/bin/zsh -e
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open KiroCrew
# @raycast.mode compact
# Optional parameters:
# @raycast.packageName KiroCrew Utils
# Documentation:
# @raycast.description Get a token and open the dashboard
REMOTE_HOST="user@your-host.example.com"
REMOTE_CMD='source ~/.zshrc; kirocrew token'
DEBUG_LOG="/tmp/kirocrew_debug.log"

if lsof -i :5476 -sTCP:LISTEN > /dev/null 2>&1; then
  echo "Tunnel already open"
else
  ssh -fNT -L 5476:localhost:5476 "${REMOTE_HOST}"
  echo "Tunnel opened"
fi

# `kirocrew token` prints only URL(s) on stdout (failures go to stderr). Keep the
# localhost one: that is what routes through the SSH tunnel opened above.
URL="$(ssh -o ConnectTimeout=10 "${REMOTE_HOST}" "${REMOTE_CMD}" 2>"${DEBUG_LOG}" | grep -m1 'localhost:5476')"

if [[ -z "$URL" ]]; then
  echo "ERROR: no token URL. Check ${DEBUG_LOG}"
  exit 1
fi

open "$URL"
```

---

## 3. Keep it alive as a service

### The built-in installer

```bash
kirocrew service install
```

On Linux this writes a **system-level** systemd unit at
`/etc/systemd/system/kirocrew.service` and enables it, so the gateway survives
SSH disconnects, restarts on failure, and starts on boot
(`WantedBy=multi-user.target`). On macOS it writes a launchd LaunchAgent at
`~/Library/LaunchAgents/dev.kirocrew.gateway.plist` with `RunAtLoad`,
`KeepAlive=true`, and a finite `ExitTimeOut`, so it starts at login, relaunches
after exit, and force-kills only after the graceful stop deadline. An explicit
`kirocrew stop` unloads the agent so it stays down for the current login session.

```bash
kirocrew service status      # service state
kirocrew logs -f             # tail live logs
kirocrew stop                # stop
kirocrew restart             # restart (service-aware)
kirocrew service uninstall   # remove the unit / plist
```

**Sudo scope on Linux:** the install shells out to `sudo install` (to place the
unit as root-owned `0644`) and `sudo systemctl` (daemon-reload, enable,
restart). No kirocrew, MCP, or LLM code path runs under sudo. Once started the
gateway runs as `User=$USER Group=$(id -gn)`, not root. The unit also caps
crash-looping with `StartLimitBurst=3` / `StartLimitIntervalSec=300` and pins
`LimitNOFILE=65536`, because a stock 1024 FD limit fails the frontend
production build with `EMFILE`.

**Why system-level rather than `systemctl --user`:** older distros (systemd 219
era) have no working per-user manager, and `systemctl --user` there fails with
`Failed to get D-Bus connection`. A system unit behaves the same on every
systemd since 2015.

### Hosts without a working `systemd --user`

If you specifically want a **user** unit and `systemctl --user status` errors
out, the per-user manager is not running. Enable it once (needs sudo), then
install the user unit:

```bash
sudo tee /etc/systemd/system/user@$(id -u).service << 'EOF'
[Unit]
Description=User Manager for UID %i
After=systemd-user-sessions.service
After=user-runtime-dir@%i.service
Wants=user-runtime-dir@%i.service

[Service]
LimitNOFILE=infinity
LimitNPROC=infinity
User=%i
PAMName=systemd-user
Type=notify
PermissionsStartOnly=true
ExecStartPre=/bin/loginctl enable-linger %i
ExecStart=/usr/lib/systemd/systemd --user
Slice=user-%i.slice
KillMode=mixed
Delegate=yes
TasksMax=infinity
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable user@$(id -u).service
sudo systemctl start user@$(id -u).service
```

`ExecStartPre=/bin/loginctl enable-linger` is the load-bearing line: without
lingering, the user manager (and everything under it) is torn down when your
last login session ends, which defeats the entire point of running 24/7.

Verify with `systemctl --user status`, then install the user unit with the helper
script [`assets/setup.sh`](assets/setup.sh). It resolves the `kirocrew` binary
and the Node version, renders [`assets/kirocrew.service`](assets/kirocrew.service)
into `~/.config/systemd/user/`, and enables it. It reads the template from its
own directory, so run it from there, and it refuses to proceed if a gateway is
already running (that port conflict again):

```bash
cd docs/guides/assets && ./setup.sh
```

Either way, read the rendered `Environment=PATH=` line before you start the
service and drop any entry that does not exist on your host, then add whatever
does: the unit gets exactly this `PATH` and nothing from your shell profile.

Or do it by hand:

```bash
mkdir -p ~/.config/systemd/user
cp docs/guides/assets/kirocrew.service ~/.config/systemd/user/
sed -i "s|KIROCREW_BIN|$(command -v kirocrew)|g" ~/.config/systemd/user/kirocrew.service
sed -i "s/%u/$(whoami)/g" ~/.config/systemd/user/kirocrew.service
sed -i "s/NVM_NODE_VERSION/$(node --version)/g" ~/.config/systemd/user/kirocrew.service
systemctl --user daemon-reload
systemctl --user enable kirocrew
systemctl --user start kirocrew
```

`systemctl --user status kirocrew` should report `active (running)`.

> **`Failed to get D-Bus connection` while running `systemctl --user`?** Your
> shell has no `XDG_RUNTIME_DIR`, which is how the client finds the per-user bus
> socket. This is normal in a non-login shell, in a `cron` job, and inside an
> agent-spawned subprocess. Export it and retry:
> `export XDG_RUNTIME_DIR=/run/user/$(id -u)`.

Manage a user unit:

| Action | Command |
|---|---|
| Status | `systemctl --user status kirocrew` |
| Restart | `systemctl --user restart kirocrew` |
| Logs | `journalctl --user -u kirocrew -f` |
| Uninstall | `systemctl --user disable --now kirocrew` |

### Hand-rolled system unit

If you want to customize the unit rather than let `kirocrew service install`
generate it:

```bash
KIROCREW_BIN=$(command -v kirocrew 2>/dev/null || echo "$HOME/.local/bin/kirocrew")

sudo tee /etc/systemd/system/kirocrew.service << EOF
[Unit]
Description=KiroCrew AI Agent Gateway
After=network-online.target
Wants=network-online.target
StartLimitBurst=3
StartLimitIntervalSec=300

[Service]
Type=simple
User=$(whoami)
ExecStart=$KIROCREW_BIN gateway
Restart=on-failure
RestartSec=10
LimitNOFILE=65536
WorkingDirectory=$HOME
Environment=HOME=$HOME
Environment=PATH=$(dirname $KIROCREW_BIN):$HOME/.local/bin:$HOME/.nvm/versions/node/$(node -v 2>/dev/null || echo v20.0.0)/bin:/usr/local/bin:/usr/bin

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now kirocrew
```

Tail it with `sudo journalctl -u kirocrew -f`. Getting `PATH` right matters:
the unit does not inherit your interactive shell's environment, so `kiro-cli`,
`node`, and `npx` must all be reachable from the `PATH` you set here or MCP
servers and tool calls fail with ENOENT.

---

## 4. Troubleshooting

| Symptom | Fix |
|---|---|
| `kirocrew: command not found` after install | Put pip's script dir on `PATH` (often `~/.local/bin`), then `source ~/.bashrc` or re-login |
| Agent backend errors or timeouts | Confirm `kiro-cli` is on `PATH` and logged in (`kiro-cli login`); `kirocrew doctor` reports its status |
| Service will not start | `sudo journalctl -u kirocrew -n 50` (system unit) or `journalctl --user -u kirocrew -n 50` (user unit) |
| Service restart-loops then gives up | `StartLimitBurst=3` within 5 minutes stops the loop on purpose. Read the logs, fix the cause, then `sudo systemctl reset-failed kirocrew` |
| `systemctl --user` says `Failed to get D-Bus connection` | `export XDG_RUNTIME_DIR=/run/user/$(id -u)` |
| Gateway will not bind the port | Something else already owns it, usually a tmux gateway. `tmux kill-session -t kirocrew`, then `ss -ltnp \| grep 5476` |
| SSH tunnel connection refused | Confirm the gateway is running and listening: `ss -ltnp \| grep 5476` on the remote host |
| Dashboard loads over the tunnel but the live view flaps online/offline | The TLS-terminating proxy must forward `X-Forwarded-Proto: https`; without it the auth cookie is set without `Secure` and mobile browsers withhold it from the `wss://` upgrade. Refresh itself keeps working (the refresh cookie is `SameSite=Lax`, so it still rides ordinary HTTPS requests), but both cookies then lack `Secure` and could be sent over plain HTTP — fix the header rather than living with it |
| Chat link still points at `localhost` | Set `dashboard.url` in `config.json` and restart the gateway |
| Link opens to "token expired" | The presigned URL must be opened within 5 minutes. Request a fresh link |
| Session drops sooner than you expect | You should be refreshed silently for 30 sliding days. If you are re-minting every ~20 hours instead, the refresh cookie is not reaching `/api/auth/refresh` — confirm the browser is sending an `mc_refresh_<port>` cookie whose port suffix matches the port the gateway resolved for the request, and check the browser console for `[refresh]` warnings. Raising the initial mint (`/kirocrew dashboard 20h`) only widens the access cookie; it does not repair a broken refresh |
| Phone cannot reach the tunnel URL | Verify the tunnel process is running and connected on the gateway host |
| Settings will not save over the tunnel | By design. Config-write and secret-reveal endpoints require a direct-local request, and forwarding headers mark a tunnelled request as remote. Change these over an SSH session on the host |
| "Embeddings not ready" in the dashboard | The ~610MB model downloads in the background over HTTPS on gateway start. `kirocrew doctor` probes the resolved URL; set `KIROCREW_EMBED_MODEL_URL` for a mirror. Memory falls back to keyword search until it lands, and the agent keeps working |

## See also

- [install.md](install.md): all build and install methods
- [docker.md](docker.md): container deployment, including `KIROCREW_BIND`
- [slack-setup.md](slack-setup.md): chat app creation and configuration
- [../system-specs/features/dashboard-token-auth.md](../system-specs/features/dashboard-token-auth.md): the full access + refresh cookie design
- [../architecture/security-deep-dive.md](../architecture/security-deep-dive.md): token auth, origin checks, the local-request gate

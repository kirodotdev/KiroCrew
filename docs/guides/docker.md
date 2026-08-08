# Running Kiro Crew in Docker

The official image runs the Kiro Crew **gateway** — dashboard, channel bots
(Slack / Discord / Telegram / WeCom / Webex), crons, and the kiro-cli agent
runtime — as a headless container. It is the recommended way to run Kiro Crew
24/7 on a server or NAS; the strongest fit is the always-on channel bot that
does not need a desktop session.

The image is public, so no registry login is needed. Start the gateway:

```
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

Or with compose: copy [`docker/compose.yaml`](../../docker/compose.yaml) and run
`docker compose up -d`.

## Images and tags

| Tag | Meaning |
|-----|---------|
| `stable` / `latest` | Latest stable release (moves on each stable cut) |
| `insider` | Latest insider pre-release |
| `nightly` | Latest nightly build |
| `0.1.0`, `0.1.0-insider.4`, `0.1.0-nightly.202607261234` | Exact immutable versions |

`linux/amd64` and `linux/arm64` are published under every tag. Version tags
are never repointed once published; pin a version tag (or a digest) for
reproducible deployments. See
[Release channels](../../README.md#release-channels) for who each channel is
for. Every published manifest carries SLSA build
provenance — verify with:

```
gh attestation verify oci://ghcr.io/kirodotdev/kirocrew:stable --repo kirodotdev/KiroCrew
```

## First-run setup

Two one-time steps after the container is up:

1. **Log in the agent runtime** (chat sessions run on kiro-cli):

   ```
   docker exec -it kirocrew kiro-cli login
   ```

   Credentials persist in the `kirocrew-home` volume, so login survives
   container upgrades.

2. **Open the dashboard** — every request requires a token; mint a login
   link yourself:

   ```
   docker exec kirocrew kirocrew token --ttl 2h
   ```

   Open the printed link, substituting the host you reach the container on
   (with the mapping above: `http://localhost:5476/?token=...`). Login links
   expire minutes after minting — mint, then open immediately. (The gateway
   also prints one link at boot, as on every platform; by the time you read
   it in `docker logs` it has usually expired, so `docker exec` minting is
   the reliable path.)

## Configuration

Channel credentials load from the environment (or from `.env` in the data
home). Pass them with `-e` / compose `environment:`:

| Variable | Purpose |
|----------|---------|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `KIROCREW_OWNER_ID` | Slack bot (Socket Mode) |
| `DISCORD_BOT_TOKEN` | Discord bot |
| `TELEGRAM_BOT_TOKEN` | Telegram bot |
| `WECOM_BOT_ID`, `WECOM_SECRET` | WeCom bot |
| `WEBEX_BOT_TOKEN` | Webex bot |
| `KIROCREW_PORT` | Dashboard port (default 5476) |
| `KIROCREW_BIND` | Bind address inside the container (image default `0.0.0.0`; see below) |
| `KIROCREW_ALLOW_UNSANDBOXED` | Set `1` to explicitly allow agent exec without the inner sandbox (see Sandbox below) |

Credential hygiene: on every start the entrypoint moves the channel
credentials it finds in the environment into the data home's `.env` file
(mode 600) and removes them from the gateway's environment before the
gateway starts — so they never sit in the long-lived gateway process's
`/proc/<pid>/environ`. Environment values win over previously stored ones
(same precedence the gateway itself applies), so changing a value in your
compose `.env` and restarting updates the stored copy.

Everything else lives in `config.json` inside the volume. Most settings are
editable from the (token-authenticated) dashboard; the exceptions are the
channel-credential pages (Slack/Discord/Telegram/WeCom/Webex tokens) and
secret-revealing views, which are read-only for any non-direct-local
browser. The image ships no text editor, so edit those from the host —
copy the file out, change it, copy it back, restart:

```
docker cp kirocrew:/home/kirocrew/.kiro/crew/config.json .
# edit config.json locally, then:
docker cp config.json kirocrew:/home/kirocrew/.kiro/crew/config.json
# docker cp writes the file root-owned; hand it back to the gateway user
# (uid 1000) or the dashboard can never save settings again:
docker exec -u 0 kirocrew chown kirocrew:kirocrew /home/kirocrew/.kiro/crew/config.json
docker restart kirocrew
```

(Or use environment variables / `.env` for the credential cases above,
which need no file edit at all.)

## State and upgrades

All persistent state — gateway home (`~/.kiro/crew`), kiro-cli credentials,
agents, skills — lives under `/home/kirocrew`. One named volume covers all
of it. Upgrade by pulling the newer image; state carries over:

```
docker compose pull && docker compose up -d
```

There is no in-container auto-update: the image is immutable and the tag is
the version selector (channel tags track their channel; version tags pin).

## Networking and security model

- **Why `KIROCREW_BIND=0.0.0.0`:** outside Docker the gateway binds
  loopback only. Inside a container, published ports (`-p`) map to the
  container's bridge interface, so a loopback bind would be unreachable
  from the host. The image therefore binds all interfaces **inside the
  container's network namespace** — nothing is reachable from anywhere
  until you publish the port, and `-p 127.0.0.1:5476:5476` keeps it
  host-local.
- **Auth surface, precisely:** the API and WebSocket surface requires a
  valid dashboard token (cookie or minted link) regardless of bind
  address. Three deliberate carve-outs exist, none of which serve secrets:
  1. **Liveness probes** — `/api/health`, `/api/live`, `/api/ready` are
     tokenless AND exempt from the DNS-rebinding Host check (orchestrators
     address containers by IP). Their payloads are secret-free; the build
     identity fields are additionally stripped unless the caller is
     direct-local with a served Host.
  2. **Static assets** — the SPA shell, `/assets/`, `/vendor/`, and
     similar non-secret static files are served without a token (standard
     SPA bootstrap; the app is useless without a token once loaded).
  3. **Local bootstrap** — `/api/token/local` and `/api/shutdown` require
     a loopback peer **plus** a filesystem secret, so they are unreachable
     through the published port by construction.
  CSRF origin checks apply to all state-changing requests, and the
  DNS-rebinding Host barrier applies to every request except the three
  probe paths.
- **Exposing beyond localhost:** publishing `5476:5476` opens the TCP
  port, but LAN browsers will still be rejected until their origin is
  allowed — set `dashboard.url` (or `KIROCREW_CORS_ORIGINS`) to the
  address you browse from. The supported pattern is a TLS reverse proxy
  in front with `dashboard.url` set to its origin, exactly as for a
  non-container deployment.
- **Sandbox:** on first run the entrypoint probes whether Kiro Crew's inner
  Linux user-namespace sandbox works under the container runtime's
  seccomp/AppArmor policy. If it does, it seeds `agent.sandbox="auto"` so
  agent commands run namespace-isolated from gateway state, same as a
  hardened native install. If no backend works, agent command execution
  stays DISABLED (fail-closed) — the gateway, dashboard, and channel bots
  run normally. To enable agents in that situation, either permit user
  namespaces (see **Sandbox troubleshooting** below) or restart with
  `-e KIROCREW_ALLOW_UNSANDBOXED=1` to explicitly accept unsandboxed agent
  execution. In the consented posture the container is the only isolation
  boundary: treat its contents (mounted volumes included) as reachable by
  agent commands, and do not mount host paths you would not hand to the
  agent. The startup log states which posture was chosen.

## Sandbox troubleshooting

Kiro Crew runs agent commands inside a Linux user-namespace sandbox that
bind-mounts empty dirs over credential paths (`~/.aws`, `~/.ssh`, etc.) so
the agent subprocess cannot read gateway credentials. The sandbox requires
two syscalls — `unshare(CLONE_NEWUSER)` and `unshare(CLONE_NEWNS)` — that
the **Docker default seccomp profile blocks**. The probe inside the
container therefore returns `EPERM`, the sandbox marks itself unavailable,
and agent execution is disabled (fail-closed) until you choose a posture.

### How the startup probe decides your posture

On first run (no `config.json` in the volume) the entrypoint probes the
sandbox and writes one of three postures:

| Probe result | Env var set? | Posture written | Agent execution |
|---|---|---|---|
| Sandbox works ✅ | — | `sandbox=auto` | Namespace-isolated |
| No backend ❌ | `KIROCREW_ALLOW_UNSANDBOXED=1` | `sandbox_allow_unsandboxed_exec=true` | Allowed — container is the only boundary |
| No backend ❌ | _(not set)_ | `sandbox=auto` (default) | **Disabled** (fail-closed) |

The startup log always states which posture was chosen:

```
[entrypoint] sandbox probe: namespace backend available → sandbox=auto
[entrypoint] sandbox probe: no backend (EPERM) → allow_unsandboxed_exec=true (KIROCREW_ALLOW_UNSANDBOXED consent)
[entrypoint] sandbox probe: no backend (EPERM) → agent exec DISABLED (set KIROCREW_ALLOW_UNSANDBOXED=1 to enable)
```

### Option A — Kiro Crew seccomp profile (recommended)

The repo ships `docker/seccomp/kirocrew-seccomp.json`: the Docker default
allow-list extended with unconditional `unshare`, `clone`, and `mount` rules.
This is strictly less permissive than `--security-opt seccomp=unconfined` or
`--privileged` — all other Docker default restrictions apply.

**Image-only users** (no repo checkout): download the profile directly:

```bash
curl -fsSL https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/docker/seccomp/kirocrew-seccomp.json \
  -o kirocrew-seccomp.json
```

Then start the container:

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  --security-opt seccomp=docker/seccomp/kirocrew-seccomp.json \
  ghcr.io/kirodotdev/kirocrew:stable
```

Or in compose (add to the `kirocrew` service):

```yaml
security_opt:
  - seccomp:./docker/seccomp/kirocrew-seccomp.json
```

With this profile the inner sandbox runs normally and credential directories
are hidden from agent subprocesses inside the container.

### Option B — Explicit unsandboxed consent

If you cannot modify the seccomp policy (managed Kubernetes, locked-down
runtime, Docker Desktop with restricted settings):

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  -e KIROCREW_ALLOW_UNSANDBOXED=1 \
  ghcr.io/kirodotdev/kirocrew:stable
```

In this posture the container is the only isolation boundary. Do not mount
host paths you would not hand directly to the agent.

### Option C — `--privileged` (not recommended)

`--privileged` disables all seccomp, AppArmor, and capability restrictions.
It does let the inner sandbox work, but the cost — full host device access
and all capabilities inside the container — is disproportionate. Prefer
Option A.

### WSL2 + Docker CE (non-Desktop)

Running Docker CE natively inside WSL2 without Docker Desktop can hit the
same `EPERM` even with Option A, because the WSL2 kernel itself may have
user-namespace support disabled (`CONFIG_USER_NS=n` in the WSL2 kernel
config).

Check from inside a running container:

```bash
docker exec kirocrew python3 -c \
  "from kiro_crew.sandbox import userns_available; print(userns_available())"
```

- `True` → user namespaces work on the kernel; re-check your seccomp
  profile (Option A).
- `False` → the WSL2 kernel lacks user-namespace support; use Option B,
  or switch to Docker Desktop which ships a kernel with `CONFIG_USER_NS=y`.

### Verifying your posture

```bash
# Check which posture was chosen at startup
docker logs kirocrew | grep '\[entrypoint\]'

# Live check from inside the container
docker exec kirocrew python3 -c \
  "from kiro_crew.sandbox import detect_backend; print(detect_backend())"
# Expected: "namespace" (inner sandbox active) or "none" (unsandboxed)
```

## Health

The image ships a `HEALTHCHECK` against `/api/health`. Orchestrators can use
`/api/live` and `/api/ready` for liveness/readiness probes; all three are
token-free and secret-free.

## Building locally

The image consumes a built wheel (never the raw source tree), keeping Docker
bytes identical to pip bytes for a given version:

```
make wheel                                   # builds dist/kirocrew-*.whl
docker build -f docker/Dockerfile -t kirocrew:dev .
```

# Research: How openclaw & hermes-agent install, authenticate, and go remote

Comparative study of two mature sibling agents (both public checkouts on this
machine) against **KiroCrew today**, to inform the KiroCrew installer + cloud
launcher. Focus areas: install entry points, cross-platform support, dependency
bootstrapping, backend/LLM auth, cloud/remote deployment, onboarding UX,
packaging.

Sources:
- `openclaw` — Node/TS pnpm monorepo shipping an `openclaw` CLI + long-running
  Gateway daemon + native macOS/iOS/Android apps. (local checkout `openclaw`)
- `hermes-agent` — Python (uv) project shipping a `hermes` CLI + gateway +
  Electron/Tauri desktop. (local checkout `hermes-agent`)
- `kirocrew` — this repo.

---

## 1. Installation entry points

| | openclaw | hermes-agent | **KiroCrew today** |
|---|---|---|---|
| Hosted one-liner | `curl … openclaw.ai/install.sh \| bash` (3400-line script) + `irm …install.ps1 \| iex` (Windows) | `curl … /install.sh \| bash` (3100 lines) + `iex(irm …install.ps1)` + `install.cmd` | **None hosted** — user clones repo then runs `bash install.sh` |
| Underlying pkg | `npm install -g openclaw` | `uv` + venv + `pip install -e .` | venv + `pip install -e .` |
| Dev/from-source | `git clone && pnpm build` | `setup-hermes.sh` (clone-first devs) | `install.sh` / `setup.sh` / `make build` |
| Registry | **npm** (`latest`/`next`/`beta`) | **PyPI** (`hermes`, `hermes-agent`, `hermes-acp`) | pip wheel (`make wheel`), not yet published |
| GUI-drivable installer | `install-cli.sh --json` NDJSON event stream | `install.sh --stage … --json --manifest` **stage protocol** + Tauri "Hermes-Setup" GUI that drives it | none |

**Shared pattern worth adopting:** a *thin bootstrapper* (get the runtime +
binary on PATH) that then **hands off to an in-CLI `onboard`/`setup` wizard** for
configuration. Both projects `exec <cli> onboard` at the end of install. KiroCrew
already half-does this (`install.sh` → `kirocrew setup --agent-only`).

**The single most reusable idea:** hermes's **stage protocol** — the *same*
install script accepts `--manifest` (emit the ordered stage list as JSON) and
`--stage <name> --json` (run one stage, emit `{ok,stage,skipped,reason}`). A GUI
(or our EC2 wizard) can then drive the CLI installer step-by-step and render
real progress. openclaw has the equivalent via `install-cli.sh --json`.

---

## 2. Cross-platform support

| | openclaw | hermes-agent | **KiroCrew today** |
|---|---|---|---|
| macOS | ✅ (x64+arm64) | ✅ (x64+arm64) | ✅ |
| Linux | ✅ (apt/pacman/dnf/yum/apk, glibc+musl) | ✅ (huge distro matrix + Raspbian) | ✅ (apt/dnf/yum/brew) |
| **Windows** | ✅ **native** PowerShell (portable Node + MinGit, no admin) | ✅ **native** PowerShell 5.1/7 (portable Node + PortableGit, no admin) | ❌ **not supported** (kiro-cli is macOS/Linux only) |
| WSL2 | ✅ (treated as Linux) | ✅ | (works, undocumented) |
| Termux/Android | — | ✅ (stdlib venv, `pkg`, psutil shim) | — |
| Shells wired | bash + zsh; PowerShell User PATH | bash + zsh + fish; PowerShell | bash + zsh + fish |

**Reusable Windows tricks (both projects independently do these):**
- **Emulation-invariant arch detection** — read `Win32_Processor.Architecture`,
  not `%PROCESSOR_ARCHITECTURE%`, because WOW64 / Prism emulation lies about
  arm64-vs-x64.
- **Portable-runtime bootstrap** — when no package manager exists, download a
  *portable* Node zip + *portable* Git (MinGit / PortableGit — the latter also
  ships `bash.exe` for the shell tool) into `%LOCALAPPDATA%\<app>\deps`. Zero
  admin, zero UAC.
- hermes keeps its **own toolchain under `$HERMES_HOME`** (`~/.hermes/bin/uv`,
  `~/.hermes/node`, `~/.hermes/git`) so it never pollutes system dirs and needs
  no root. openclaw offers the same via `install-cli.sh` (`~/.openclaw/tools`).

**KiroCrew implication:** we cannot run the `kiro-cli` *backend* on Windows. But
Windows support is still reachable if Windows is a **thin client** in cloud mode
(§5) — the client only needs `aws` CLI + an SSH client (both native on Win10+),
which the portable-bootstrap tricks make trivial to guarantee.

---

## 3. Dependency bootstrapping

Both peers **auto-install** prerequisites aggressively (vs. KiroCrew's mix of
auto-install + "check and instruct"):

- **openclaw**: Homebrew→node@24 (macOS); NodeSource/pacman/apk (Linux);
  winget→choco→scoop→**portable Node from nodejs.org/dist** (Windows). Corepack
  pnpm with an `~/.local/bin/pnpm` shim fallback. **Reactive build-tool install**
  — it does *not* preinstall compilers; it greps the failed `npm` log for
  `gyp ERR! find Python` / `cmake: command not found`, installs build tools,
  then retries once.
- **hermes**: everything via **`uv`** — `uv python install 3.11`,
  `uv sync --extra all --locked` (hash-verified from `uv.lock`) with a multi-tier
  pip fallback and a `_BROKEN_EXTRAS` supply-chain quarantine. Most provider SDKs
  are **lazy-installed at first use** to shrink the base install. Portable Node 22
  tarball; `run_with_timeout` watchdog (process-group kill) around every hangy
  download (Playwright/npm/Electron).
- **KiroCrew**: `install.sh` detects apt/dnf/yum/brew/nvm, optional `--mise` for
  pinned Python+Node. No reactive build-tool logic, no watchdog, no lockfile
  hash verification.

**Adopt:** `run_with_timeout` (portable stall-killer) and the reactive-build-tool
retry are cheap, high-value robustness wins for any installer.

Neither peer auto-installs Ollama (KiroCrew also treats it as optional — good).

---

## 4. Backend / LLM account setup

This is where the projects differ most from KiroCrew, because KiroCrew
deliberately **delegates all backend auth to `kiro-cli`**.

| | openclaw | hermes-agent | **KiroCrew** |
|---|---|---|---|
| Auth surface | inside `openclaw onboard` | inside `hermes setup` / `hermes … auth login` | **`kiro-cli login`** (external) |
| Mechanisms | API key; **reuse existing CLI logins** (an external agent CLI / Codex / Gemini) *tested with a real completion before saving*; **PKCE OAuth** w/ localhost:1455 callback + paste-URL for headless | API key (`.env` chmod 600); **device-code OAuth** for Nous/Codex/xAI/MiniMax; reuse Qwen CLI login; Anthropic paste-token | GitHub / Google / **AWS Builder ID** / **IAM Identity Center** / Okta / Entra — all via kiro-cli's own browser + device-code flow |
| Subscription/purchase | bring-your-own provider acct; no billing in-app | **Nous Portal** subscription (one plan, 300+ models) via browser OAuth | **Kiro subscription** — handled entirely by kiro-cli / kiro.dev; KiroCrew does not reimplement it |
| Credential storage | per-agent SQLite (`auth-profiles.json`), **SecretRefs** (env/file/exec, 1Password/Vault/pass), process-local sentinels | `~/.hermes/.env` (600) + `~/.hermes/auth.json` (file-locked OAuth store) | kiro-cli owns its own creds; KiroCrew stores none for the LLM |
| AWS as LLM | Bedrock via `mode:"aws-sdk"` | **Bedrock via boto3 full credential chain** — "on EC2/ECS/Lambda attach an IAM role, no keys" | (Bedrock provider was removed in the OSS fork; ACP/kiro-cli only) |

**Reusable, remote-login-relevant:**
- Both document **OAuth-over-SSH** for headless hosts (`ssh -L <port>` port-forward
  + open the URL in the *local* browser), and **device-code** as the
  no-forwarding alternative. KiroCrew's `docs/kiro-cli/authentication.md` already
  says exactly this for `kiro-cli login` on a remote box. **This is directly the
  "log in to the backend on the EC2 instance" story** — we don't need to build
  anything; we surface the device-code URL/port to the user.
- hermes's **Bedrock-on-EC2 zero-config** pattern (IAM instance role → boto3
  credential chain, no stored keys) is the philosophical match for "connect AWS
  via the CLI, store nothing." (KiroCrew's `deploy_web` already applies the same
  principle to the `aws` CLI — see the KiroCrew section below.)

---

## 5. Cloud / remote deployment — the crux

**Headline: neither openclaw nor hermes provisions a VM.** My grep for
`run_instances` / `RunInstances` / `cloudformation create-stack` /
`terraform apply` / CDK returned **zero provisioning code** in either repo.
Their "cloud" story is uniformly *"you already have a Linux box (VPS / EC2 /
Pi); SSH in and run the installer,"* plus Docker/PaaS config files.

| | openclaw | hermes-agent | **KiroCrew today** |
|---|---|---|---|
| VM provisioning | **none** | **none** (links an *external* CloudFormation sample repo) | **none** |
| Deploy unit | Docker (359-line multi-stage), Fly.io (`fly.toml` + private no-public-IP variant), Render, Railway, Northflank, K8s, Nix, Ansible | Docker (`nousresearch/hermes-agent`, s6-overlay), docker-compose (host net), Nix flake | pip install on the host; `kirocrew service install` (systemd/launchd) |
| Cloud guides | DigitalOcean, Hetzner, GCP, Azure, Oracle Free ARM, Pi (AWS: "works, community video", no guide) | Docker on any VPS; AWS only via Bedrock doc + external CFN sample | `REMOTE_DESKTOP_SETUP.md` (any Linux VM incl. EC2) |
| **Web-UI remote access** | **loopback + Tailscale Serve/Funnel (auto-configured)**; SSH tunnel (persistent LaunchAgent recipe); direct bind requires token/password | **loopback + SSH tunnel** (recommended); direct bind `0.0.0.0` requires a DashboardAuthProvider (basic/OAuth/OIDC), **fails closed** otherwise; **relay outbound-WS reverse tunnel** to a hosted connector (no inbound port) | **loopback + SSH tunnel**; `Instances` feature auto-tunnels + mints token + iframes; cloudflared/ngrok/Tailscale for mobile |
| Idle cost control | — | **Fly scale-to-zero** (`go_dormant()` + Fly `autostop:suspend` + connector wake-poke) | — |

**Clever remote-access designs worth studying (not necessarily adopting):**
- **openclaw Tailscale Serve/Funnel auto-config** — keeps the gateway on
  loopback but runs `tailscale serve`/`funnel` for it; identity headers
  authenticate. The cleanest "public HTTPS without exposing a port" story.
- **hermes relay connector** — a gateway with *no public IP dials OUT* over a
  persistent authenticated WebSocket to a hosted connector that fronts the chat
  bots; inbound rides back down the same socket. A purpose-built reverse tunnel.
- **hermes scale-to-zero** — the gateway goes dormant when idle; the host
  (Fly) suspends the machine; a "wake poke" restarts it. Directly relevant if we
  want an EC2 instance that stops itself to save money and wakes on demand.

**KiroCrew is actually ahead of both peers on turnkey remote access** because of
the built-in **`Instances`** feature (`docs/INSTANCES.md`): a hub gateway opens a
supervised `ssh -N -L` to a remote gateway's loopback port, mints a short-lived
dashboard token over SSH, and embeds the remote dashboard in an iframe with
self-heal + token refresh. It *already documents EC2* as a remote type (via an
`~/.ssh/config` alias with `IdentityFile`/`ProxyJump`/SSM `ProxyCommand`). So
once an EC2 box is running KiroCrew, **the "access it from the app" half is
essentially done** — the missing piece is *provisioning the box and registering
it as an instance automatically.*

---

## 6. First-run / onboarding UX

- **openclaw** — `openclaw onboard`: QuickStart-vs-Advanced, Model/Auth →
  Workspace → Gateway(port/bind/auth/Tailscale) → Channels → Daemon install →
  **Health check (starts gateway, verifies reachable)** → Skills. Localized.
  `--json`/`--non-interactive`/`--reset`. `openclaw doctor` = inspect / `--fix` /
  `--lint` (CI-friendly structured findings), run automatically post-upgrade to
  migrate config.
- **hermes** — `hermes setup` (modular sections; `--portal` one-shot), auto-run
  by the installer reading `/dev/tty` so it works under `curl|bash`. `hermes
  doctor` (108 KB) checks version/supervision/provider health/AWS+Bedrock
  reachability/DB health. Focused reconfig: `hermes model`, `hermes tools`,
  `hermes gateway setup`.
- **KiroCrew** — `kirocrew setup` wizard (`cli_setup.py`: workspace dir, Slack
  tokens, slash command, timezone, dashboard URL, custom domain, `--agent-only`,
  `--electron-only`) + `kirocrew doctor`. Solid, but **no `--json`/structured
  mode**, **no `--fix` auto-repair**, and no "start gateway + verify reachable"
  health gate at the end of setup.

**Adopt:** `doctor --fix` / `doctor --json` (structured, CI/GUI-friendly) and an
end-of-setup "start + verify reachable" gate.

---

## 7. Packaging / distribution

| Channel | openclaw | hermes | KiroCrew today |
|---|---|---|---|
| Language registry | **npm** | **PyPI** | pip wheel (unpublished) |
| Docker image | ✅ published | ✅ `nousresearch/hermes-agent` | ❌ none |
| Desktop app | `OpenClaw.app` (SwiftPM, universal, DMG, **Sparkle auto-update**), iOS+Android | Electron (`dist:mac/win/linux`) + **Tauri signed bootstrap installer** | Electron (`make desktop` → DMG/AppImage) |
| Homebrew | deps only, no cask | ✅ formula (`packaging/homebrew`) | ❌ |
| Nix | flake + guides | ✅ flake + NixOS module | ❌ |
| PaaS one-click | Render/Fly/Railway/Northflank | Docker-on-VPS | ❌ |

**Gaps for KiroCrew if we want frictionless distribution:** no published
package (pip/npm), no Docker image, no Homebrew, no auto-update for the desktop
app (openclaw's Sparkle appcast is the reference).

---

## What KiroCrew already has that the peers don't

1. **`deploy_web` builtin app** — the in-repo reference for **bring-your-own-AWS
   with zero stored credentials**: every AWS call goes through one `run_aws()`
   chokepoint that shells `aws … --profile <name>` (never boto3), wrapped in the
   OS sandbox; it **generates a least-privilege IAM policy for the user to
   apply** (`iam.py`, KiroCrew never does an IAM write); it does **read-only
   reachability checks** (`sts get-caller-identity`, harmless `list` calls) and
   maps `AccessDenied` stderr → the exact missing IAM statement Sid. LLM-facing
   inputs (`profile`, `region`) are charset-validated before hitting argv. This
   is the exact pattern to extend to EC2.
2. **`Instances` feature** — turnkey SSH-tunnel + token-mint + iframe remote
   dashboard with self-heal, already documenting EC2.
3. **Security floor already covers EC2 destructiveness** — `aws ec2
   terminate-instances` and `aws ec2 delete-*` are in the destructive-command
   **deny list**, and `~/.aws` / `~/.ssh` are sensitive-path-blocked. So the
   *agent* can't tear down instances or read AWS creds; only the *human* CLI /
   installer can. Any EC2 launcher must preserve this asymmetry.

## Net-new work (nobody has it)

**Actual EC2 provisioning** (`aws ec2 run-instances` orchestration: key pair or
SSM, security group, AMI/instance-type selection, user-data to install+start
KiroCrew, tag for discovery, wait-for-ready, register as an Instance). This is
greenfield relative to all three codebases — but the *shape* is fully
prescribed by `deploy_web` (the `run_aws` chokepoint, tag-based statelessness,
IAM-policy-for-the-user, reachability checks, `AccessDenied`→Sid mapping).

# Installer + Cloud Launcher — Options to Discuss

This is the discussion doc. Each section is a decision with concrete options and
a recommendation. Read [`research-comparison.md`](research-comparison.md) first
for the evidence behind these.

The overall shape the request implies:

```
┌─ user's machine (mac / Windows / Linux) ──────────┐        ┌─ EC2 (Amazon Linux / Ubuntu) ─┐
│  KiroCrew launcher / client                       │        │  KiroCrew gateway (loopback)   │
│   • kiro-cli login  (backend auth, device-code)   │        │  kiro-cli (backend, logged in) │
│   • aws CLI  (their creds, we store nothing)      │  ssh   │  systemd service, auto-restart │
│   • provision + connect  ───────────────────────────────► │  dashboard on 127.0.0.1:5476   │
│   • browser / desktop app  ◄── ssh -L tunnel + token ───── │                                │
└───────────────────────────────────────────────────┘        └────────────────────────────────┘
```

---

## Decision 0 — Where does KiroCrew actually *run*? (frames everything else)

The request bundles two things that pull in different directions: "run on
mac/Windows/Linux" **and** "launch an EC2 box and run KiroCrew there." The
backend (`kiro-cli`) is **macOS/Linux only** — it does not run on Windows. So:

| Option | What it means | Windows story |
|---|---|---|
| **A. Local-first** | KiroCrew runs on the user's own machine; EC2 is an optional "run it 24/7 elsewhere" add-on | Windows users **can't** run it locally (no kiro-cli) |
| **B. Cloud-first** | KiroCrew always runs on EC2; the user's machine is a thin launcher+client | Windows works — client only needs `aws` + `ssh` |
| **C. Both, user picks** (Recommended) | Installer offers "run here" (mac/Linux) or "run on EC2" (all OSes, incl. Windows). Windows is auto-routed to the EC2 path | Windows works via the cloud path |

**Recommendation: C.** It's the only way to honor *both* "runs on Windows" and
"only kiro-cli backend." On mac/Linux the user can choose local or cloud; on
Windows the installer explains that the backend runs on a Linux EC2 box and the
Windows app is the client. This also matches how openclaw/hermes frame it (local
gateway vs. "Remote over SSH" mode).

> Everything below assumes C. The "launcher/client" is a small cross-platform
> layer; the "gateway" is the existing backend running on mac/Linux/EC2.

---

## Decision 1 — Installer form factor

| Option | Pros | Cons |
|---|---|---|
| **A. Keep shell/PowerShell scripts** (extend `install.sh`, add `install.ps1`) | Matches openclaw+hermes exactly; no runtime to bootstrap the bootstrapper; `curl\|bash` + `irm\|iex` are the expected UX | Two codebases (sh + ps1) to keep in sync; shell is awkward for the EC2 wizard |
| **B. Python-based installer** (a `kirocrew-installer` that only needs stdlib Python) | One codebase, cross-platform, and *we already ship a Python CLI*; the EC2 orchestration is Python calling `aws` (same as `deploy_web`) | Windows may lack Python; need a tiny bootstrap to get Python first |
| **C. Hybrid (Recommended)** | Thin per-OS bootstrapper (`install.sh` / `install.ps1`) whose only job is: ensure Python + `aws` CLI + `ssh`, then hand off to a **Python `kirocrew setup`/`kirocrew cloud` wizard** that does everything interesting (auth, provisioning, connect) | Slightly more moving parts | 

**Recommendation: C**, mirroring the proven openclaw/hermes shape (*thin
bootstrapper → in-CLI wizard*). The EC2 launcher and the "connect" flow live in
Python where they can reuse `deploy_web`'s `run_aws` pattern, `validation.py`,
the `Instances` registry, and `cli_setup.py`. The shell/ps1 layer stays small
and boring.

**Steal from the peers regardless of choice:**
- hermes **stage protocol** (`--manifest` + `--stage <n> --json`) so a GUI/app
  can drive install with real progress.
- hermes `run_with_timeout` stall-killer around every download.
- openclaw **reactive build-tool install** (grep failed log → install → retry).
- Emulation-invariant Windows arch detection + **portable Node/Git** bootstrap
  for zero-admin Windows.

---

## Decision 2 — Backend (Kiro) authentication

`kiro-cli` owns this entirely; we don't reimplement subscription/purchase.

- **Where does login happen?** In cloud mode the backend is on EC2, so login
  must happen **on the EC2 box**. `kiro-cli` already supports this:
  - **Builder ID / IAM Identity Center → device-code**: CLI prints a URL + code;
    user opens it in *their local* browser. No port-forward needed.
  - **Social (Google/GitHub) → SSH `-L` port-forward** then open the URL locally.
- **Recommendation:** the wizard runs `kiro-cli login` over SSH on the instance,
  captures the device-code URL/port, and **surfaces it to the user** (opens it in
  their local browser automatically for device-code; sets up the `-L` forward
  automatically for social). "Register / purchase a Kiro subscription" is just a
  link to kiro.dev / the kiro-cli login flow — we guide, we don't rebuild.
- **Do not store any Kiro credentials** — they live in kiro-cli's own store on
  whichever host runs the backend. (Consistent with the current design.)

Open question to confirm: **should the KiroCrew *dashboard* have its own login
tied to the Kiro account, or stay on the existing per-session token model?**
Recommendation: keep the existing token model (`kirocrew token`) — it already
works through SSH tunnels and the `Instances` iframe.

---

## Decision 3 — AWS connection (the "never store credentials" requirement)

**This is already solved in-repo by `deploy_web` — copy its model verbatim.**

- **Never store AWS credentials.** Store only a **profile name** (+ region). All
  AWS work shells to `aws … --profile <name>`; credential resolution stays in the
  `aws` CLI's own provider chain (SSO / named profile / env / IMDS role). One
  `run_aws()` chokepoint, sandbox-wrapped, unit-test-mockable.
- **Onboarding:**
  1. Check `aws` CLI is installed (bootstrap it if not — it has official
     installers for all three OSes).
  2. Resolve the profile with `aws sts get-caller-identity` (read-only
     reachability, like `iam.reachability_check`). If it fails →
     "run `aws configure sso` / `aws configure --profile <name>` and retry."
  3. **Generate a least-privilege IAM policy for EC2 launch** for the user to
     apply themselves (KiroCrew never writes IAM), exactly like
     `deploy_web/iam.py`. Map `AccessDenied` stderr → the exact missing statement.
- **"Create or register an AWS account":** we cannot create an AWS account
  programmatically (AWS has no API for signup). The wizard **links to the AWS
  signup page** and then does the `aws configure` reachability handshake. This is
  the same bar `deploy_web` sets ("bring your own AWS account").

Decision to confirm: **which credential style do we recommend first?**

| Option | Notes |
|---|---|
| **A. IAM Identity Center / SSO** (Recommended) | `aws configure sso`; short-lived creds, no long-lived keys on disk; best-practice; matches Kiro's own IdC auth |
| **B. Named profile w/ access keys** | Simplest for users who already have keys, but long-lived keys are the thing security guidance discourages |
| **C. Let the CLI's default chain decide** | Most permissive; good fallback, but less guidance |

Recommend **A first, fall back to C** — never handle raw keys ourselves either way.

---

## Decision 4 — EC2 provisioning (the net-new piece)

No peer automates this; the *shape* is prescribed by `deploy_web`. A new module
(`cloud/ec2.py`, say) with a `run_aws` chokepoint. Sub-decisions:

**4a. Connectivity / login model**

| Option | Inbound ports | Auth to box | Windows-friendly |
|---|---|---|---|
| **A. SSM Session Manager** (Recommended) | **none** (no public SSH) | IAM + SSM agent (preinstalled on AL2023/Ubuntu AMIs) | ✅ `aws ssm start-session`; tunnels via `AWS-StartPortForwardingSession` |
| **B. SSH key pair + security group** | 22 open (ideally to user's IP only) | key `.pem` | ✅ (OpenSSH on Win10+) but manages a key file + SG |
| **C. Both** | — | — | — |

Recommend **A (SSM) as default, B as opt-out.** SSM means *no open inbound
ports at all* and no key management — the dashboard tunnel becomes
`aws ssm start-session … AWS-StartPortForwardingSession` instead of `ssh -L`. It
also composes with the existing `Instances` feature, which already documents an
SSM `ProxyCommand`. Downside: requires the SSM permissions in the generated IAM
policy and the AMI's SSM agent (default on modern AMIs).

**4b. What runs KiroCrew on the box** — pass **EC2 user-data** that installs
prerequisites + KiroCrew + `kiro-cli`, then `kirocrew service install` (systemd).
Options for *how* it installs: (i) `git clone + install.sh` (today's path);
(ii) a published pip wheel (needs Decision 7); (iii) a prebuilt Docker image
(needs Decision 7). Recommend **(i) for the first cut**, migrate to (ii)/(iii)
once we publish artifacts.

**4c. Instance defaults** — recommend a small-but-headroomed default
(`REMOTE_DESKTOP_SETUP.md` says KiroCrew uses ~10 GB RAM; suggest e.g.
`t3.xlarge`/`m7g.xlarge` arm64, 16 GB) in a user-chosen region; tag every
resource `kirocrew:managed=true` + a `kirocrew:instance=<id>` for **stateless
discovery by tag** (copy `deploy_web`'s tag model — no local state file to drift).

**4d. Lifecycle & cost** — provide `launch`, `list`, `stop`, `start`,
`terminate`. Two hard safety rules:
- Keep `aws ec2 terminate-instances` / `aws ec2 delete-*` in the **agent** deny
  list (they already are). Provisioning is a **human/installer** action, never an
  LLM tool. Confirm-before-terminate in the CLI/UI.
- Optional: an idle-stop timer (hermes's scale-to-zero is the reference) so a
  forgotten instance stops itself. Nice-to-have, not v1.

**4e. Should EC2 launch be exposed to the LLM at all?** Recommend **no for
writes** (launch/terminate stay human-only, mirroring `deploy_web`'s "KiroCrew
never does an IAM write"); read-only `list/status` could later be an MCP tool.

---

## Decision 5 — Web-UI access from app or browser

Mostly **already built** — pick how to wire it.

| Option | Mechanism | Notes |
|---|---|---|
| **A. Reuse the `Instances` feature** (Recommended) | After launch, auto-register the EC2 box as an Instance; the hub opens a tunnel (SSH or SSM), mints a token, iframes the dashboard | Self-heal, token-refresh, warm-set all exist; already documents EC2 |
| **B. Plain `ssh -L` / SSM port-forward + open browser** | The `token` CLI already prints `http://localhost:5476?token=…`; the Raycast/LaunchAgent recipes in `REMOTE_DESKTOP_SETUP.md` show the pattern | Simplest; good for "just open my browser" |
| **C. Public/tunnel (cloudflared/Tailscale)** | `MOBILE_ACCESS_SETUP.md` already documents this | For phone / share-a-link; keep as the mobile story |

**Recommendation: B for the one-click "open dashboard" button, A for the
managed multi-instance experience, C stays the mobile path.** Keep the gateway
**loopback-only** on EC2 (never bind `0.0.0.0`) — access is always via
tunnel/SSM + token, matching the current security posture and both peers'
defaults.

Desktop app: the Electron shell can host the same launcher/connect flow so the
"desktop app" access path (Decision 0's client) is a thin wrapper over B/A.

---

## Decision 6 — Onboarding UX polish (cheap wins)

- Add a `--json` / structured mode and a `--fix` posture to `kirocrew doctor`
  (openclaw/hermes both have this; enables GUI + CI + self-repair).
- End `kirocrew setup` with a **"start gateway + verify reachable"** health gate
  (openclaw does this; catches "installed but broken" immediately).
- Add a `kirocrew cloud` command group (`launch/list/stop/start/terminate/connect`)
  as the human entry point; the wizard calls into it.

---

## Decision 7 — Distribution (enables the EC2 user-data to be fast/clean)

Not required for a first cut (EC2 can `git clone + install.sh`), but each of
these makes provisioning simpler and is a peer-parity gap:

- **Publish a pip wheel** (`make wheel` exists) → EC2 user-data becomes
  `pip install kirocrew`.
- **Publish a Docker image** → EC2 user-data becomes `docker run …` (both peers
  do this; simplest reproducible box).
- **Homebrew formula** for the mac client; **Sparkle-style auto-update** for the
  desktop app (openclaw reference).

Recommendation: sequence as **wheel → Docker image → brew/auto-update**, after
the core launcher works with `git clone + install.sh`.

---

## Suggested phasing (for when we move past planning)

1. **Client bootstrap + `kirocrew cloud` skeleton** — `install.ps1` (Windows
   client: ensure `aws` + ssh), Python `cloud/ec2.py` with the `run_aws`
   chokepoint + IAM-policy generator + reachability check (all copied from
   `deploy_web`). Read-only `list/status` first.
2. **Launch + user-data + wait-for-ready** — `run-instances` (SSM default),
   tag-based discovery, user-data that installs KiroCrew + `kiro-cli` + service.
3. **Backend login on the box** — surface `kiro-cli login` device-code/`-L` flow.
4. **Connect** — auto-register as an Instance (Decision 5A) + a one-click
   "open dashboard" (5B).
5. **Lifecycle + safety** — stop/start/terminate with confirmation; keep agent
   deny-list intact; optional idle-stop.
6. **Distribution + doctor polish** (Decisions 6–7).

---

## Open questions for you

1. **Decision 0:** confirm "both, user picks" (Windows → cloud) is the intended
   scope, vs. cloud-only.
2. **Decision 3:** default AWS credential style — SSO/IdC first (recommended),
   or don't opine and use the CLI's default chain?
3. **Decision 4a:** SSM-first (no open ports, recommended) vs. SSH-key-first?
4. **Decision 4c:** default instance size/region/arch — any preference, or make
   the wizard ask every time?
5. **Cost posture:** do you want the idle-stop / scale-to-zero behavior in scope,
   or is "user manages start/stop" fine for v1?
6. **"Create AWS account":** confirm it's acceptable that we *link to* AWS signup
   (no API exists to create an account) and only automate the `aws configure`
   reachability handshake after.

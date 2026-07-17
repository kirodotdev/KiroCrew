# KiroCrew Cross-Platform Installer & Cloud Launcher — Planning

> **Status:** Research / planning only. Nothing here is built yet. This folder
> collects the material for deciding *how* to build a cross-platform installer
> that can (a) install/run KiroCrew locally where supported, and (b) provision
> an EC2 instance in the user's own AWS account and give them one-click web-UI
> access to it from any OS — including Windows.

> **Status update (built + validated):** the launcher is implemented in
> `src/kiro_crew/cloud/` and wired into the CLI as `kirocrew cloud`. It was
> validated end-to-end against a real AWS dev account: `launch` provisions the
> CloudFormation stack, ships the local source via S3, installs KiroCrew +
> kiro-cli, and the gateway serves the dashboard (reached over an SSM tunnel with
> a minted token); `destroy` removes every resource cleanly. See
> [`AS_BUILT.md`](AS_BUILT.md) for the shipped architecture and the fixes the
> live validation surfaced.

## The goal (from the request)

Build a setup experience that:

1. **Runs on macOS, Windows, and Linux.**
2. Lets the user **authenticate the backend** — register/purchase a Kiro
   subscription or connect an existing `kiro-cli` login.
3. Lets the user **connect their AWS account via the `aws` CLI**, *without
   KiroCrew ever storing AWS credentials* (credential resolution stays in the
   `aws` CLI's own provider chain).
4. **Launches an EC2 instance** and runs KiroCrew on it.
5. Gives the user **easy web-UI access** to that instance from either a desktop
   app or a browser.

## Documents

| File | What it covers |
|------|----------------|
| [`research-comparison.md`](research-comparison.md) | How **openclaw** and **hermes-agent** do install / auth / cloud / remote-access, compared with **KiroCrew today**. What's reusable, what's net-new. |
| [`options.md`](options.md) | The design decisions, each with concrete options and a recommendation. The discussion doc. |
| [`implementation-plan.md`](implementation-plan.md) | **The plan we follow.** One command → KiroCrew running on a correctly-sized, correctly-configured EC2 box in the user's own AWS account. CloudFormation-based; SSM-only (no open ports, no key files); **Python for all logic** (§2a) with a thin shell/PowerShell bootstrapper; milestones M1–M6. |

## TL;DR of the findings

- **Local install is largely solved.** KiroCrew already has `install.sh` +
  `setup.sh` (OS/pkg-manager detection, venv, PATH wiring, optional Electron
  app) and a `kirocrew setup` wizard + `kirocrew doctor`. The gaps vs. the best
  peer installers (openclaw/hermes) are: **no native Windows path**, **no
  portable-runtime/zero-admin mode**, and **no GUI-drivable JSON stage
  protocol**.
- **The AWS "bring-your-own-cloud, never store creds" pattern already exists
  in-repo** — the `deploy_web` builtin app shells to the `aws` CLI with
  `--profile` (never boto3), generates a least-privilege IAM policy for the user
  to apply, and does read-only reachability checks. **This is the blueprint for
  EC2 provisioning.**
- **Remote web-UI access is already solved three ways** — the `Instances`
  feature (SSH-tunnel + token-mint + iframe), `REMOTE_DESKTOP_SETUP.md`
  (`kirocrew service install` + `ssh -L`), and `MOBILE_ACCESS_SETUP.md`
  (cloudflared / ngrok / Tailscale). The gateway binds **loopback-only**; access
  is always via a tunnel, never a public bind.
- **EC2 provisioning itself is net-new** — no peer project automates it. Neither
  openclaw nor hermes launches a VM; both say "SSH into a box you already have."
- **The Windows-support key insight:** `kiro-cli` (the backend) only runs on
  macOS/Linux. But in the **cloud mode**, KiroCrew runs *on the EC2 Linux box*,
  and the user's machine is only a **thin launcher/client** (needs just `aws`
  CLI + `ssh`/SSM). That client subset runs fine on Windows — so "Windows
  support" is achievable without ever running the backend on Windows.

# Setting up Remote Crew on an EC2 instance

You run the Kiro Crew gateway on an EC2 box and drive it from your laptop. The
gateway always binds **loopback only**, so you reach it through a tunnel — either
an **SSH tunnel** or an **AWS SSM Session Manager** tunnel. This page covers both,
plus the EC2-specific gotchas people actually hit (from `kirocrew doctor`).

> Installing the gateway itself (host requirements, packages, running it as a
> service, moving your state over) is covered in
> [remote-and-mobile.md](remote-and-mobile.md). This page assumes the gateway is
> installed and focuses on **reaching** it from EC2 and on troubleshooting.

## Which way should I use?

| | **SSH tunnel** | **AWS SSM Session Manager** |
|---|---|---|
| Inbound port on the box | needs inbound SSH (22), or a bastion | **none** |
| Client secret | an SSH key | none — IAM (`ssm:StartSession`) |
| Client tooling | `ssh` | `aws` CLI + `session-manager-plugin` |
| On the box | `sshd` | the SSM agent (preinstalled on Amazon Linux) + an instance role for SSM |
| Access control | key possession | IAM, centrally grantable/revocable, CloudTrail-audited |
| Best when | you already SSH to the box | the box has zero SSH ingress (the hardened default) |

Both end at the same loopback gateway and both are managed identically once
registered in **Settings → Remote Crew**.

## Way 1 — SSH tunnel

1. On your laptop, forward the gateway's port over SSH (use the **real** gateway
   port — see [the port gotcha](#porttunnel-mismatch-the-common-one)):

   ```bash
   ssh -N -L 5476:localhost:5476 <user>@<ec2-host>
   ```

   Then open `http://localhost:5476`. Run `kirocrew token` on the box to mint the
   sign-in URL. (Full details, including a non-default port, in
   [remote-and-mobile.md](remote-and-mobile.md#ssh-tunnel-laptop).)
2. To let the dashboard manage it, add it in **Settings → Remote Crew → Add remote
   crew → Connection method = SSH tunnel**, giving the **SSH host / alias** and the
   **remote port**.

## Way 2 — AWS SSM Session Manager (no inbound SSH)

Requires only the SSM agent + an instance role that allows Session Manager on the
box, and `ssm:StartSession` + `session-manager-plugin` on your laptop. No inbound
port, no SSH key.

- **Automated (recommended):** the one-command launcher provisions a box, installs
  the gateway, and registers it over SSM for you — see
  [cloud-instance-ssm-vs-ssh.md](cloud-instance-ssm-vs-ssh.md).
- **Manual (a box you already have):** **Settings → Remote Crew → Add remote crew →
  Connection method = AWS SSM Session Manager**, then fill **SSM target** (the EC2
  instance id `i-…`, or an SSM managed-instance id `mi-…`), optional **AWS profile**
  / **AWS region** / **Remote user**, and the **Remote port** (the gateway's real
  port). Save, then **Connect**.

## EC2 gotchas / troubleshooting

These map to warnings in `kirocrew doctor`.

### MCP tools all fail: "Sandbox backend unavailable … `allow_unsandboxed_exec` is not set"

On Linux, agent subprocesses run inside a **user-namespace sandbox**. Many hardened
Amazon Linux 2023 / corporate AMIs ship with **unprivileged user namespaces
disabled**, so no sandbox backend is available and every MCP server — and every
spawn — fails closed. Pick one:

- **Enable user namespaces on the box (preferred — keeps isolation).** Ensure
  `user.max_user_namespaces` is non-zero (and on Debian/Ubuntu kernels,
  `kernel.unprivileged_userns_clone=1`). For example:

  ```bash
  sudo sysctl -w user.max_user_namespaces=15000
  # persist across reboots:
  echo 'user.max_user_namespaces=15000' | sudo tee /etc/sysctl.d/99-userns.conf
  ```

  Then restart the gateway.
- **Or opt into unsandboxed execution (trades isolation — only on a box you
  trust).** Run `kirocrew setup` (it offers this interactively), or set
  `agent.sandbox_allow_unsandboxed_exec: true` in `~/.kiro/crew/config.json`, then
  restart the gateway. This lets agent subprocesses run without any sandbox.

### Gateway/pods die on logout: "linger disabled"

A **user**-level systemd unit and any running pods stop when your login session
ends. Enable linger so they survive logout and reboot:

```bash
loginctl enable-linger <user>
```

(A gateway installed as a **system** unit under `/etc/systemd/system` already
survives logout; linger still matters for pods and for user-level installs.)

### "kiro login: not logged in"

kiro-cli must be authenticated **on the box**. Run `kiro-cli login` there and
complete the device-code flow. Chat errors like "not logged in" mean this step was
skipped on the remote.

### Port/tunnel mismatch (the common one)

`kirocrew doctor`'s "Remote access" hint and its `dashboard: http://localhost:5476`
line show the **defaults**. If your service or shell sets `KIROCREW_PORT` (e.g.
`7777`), the gateway actually listens **there**, not on 5476 — the doctor line just
didn't see that env var. Your tunnel, browser, and token must all use the **same,
real** port:

```bash
# service has KIROCREW_PORT=7777 → tunnel 7777, not 5476:
ssh -N -L 7777:localhost:7777 <ec2-host>
# then open http://localhost:7777
```

For SSM, set the Instances **Remote port** to that same port. If your browser
reaches the dashboard on a *different* local port than the remote, opt that local
port into the CSRF allowlist — see
[remote-and-mobile.md](remote-and-mobile.md#2-reach-it-from-a-laptop-or-phone).

### Non-fatal warnings you can ignore

- **`ffmpeg: not found`** — only needed for speech-to-text. Drop a static ffmpeg
  build into `~/.local/bin` (it's not in the AL2023 repos; Kiro Crew auto-detects
  it).
- **`Vector Memory … vendored runtime failed to load`** — the in-process embedding
  runtime couldn't load its shared library on this host; memory falls back
  gracefully and keeps working. Safe to ignore unless you specifically rely on
  local vector memory.
- **`project dir: not set`** — cosmetic. Run `kirocrew setup` from a project root
  if you want a default project directory.

## Related

- [remote-and-mobile.md](remote-and-mobile.md) — installing the gateway, running
  it as a service, and reaching it from a phone over an HTTPS tunnel.
- [cloud-instance-ssm-vs-ssh.md](cloud-instance-ssm-vs-ssh.md) — the two transports
  in depth and the one-command cloud launcher.

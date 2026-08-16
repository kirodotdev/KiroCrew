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

## Cheaper instance-hours with Spot (`--spot`)

If you launched with the one-command launcher, `kirocrew cloud launch --spot`
provisions the box on **Spot pricing** instead of on-demand — typically 60-90%
below the on-demand rate for the same shape, though the discount varies by size
tier, availability zone, and region.

```bash
kirocrew cloud launch --size balanced --spot
```

The trade is interruptibility: AWS can reclaim the capacity at any time with a
**2-minute notice**. Kiro Crew does not yet act on that notice, so an agent task
running at that moment dies ungracefully — the same way it would if you rebooted
the host out from under it.

What it does **not** cost you is your data. The launcher requests a
**persistent** Spot instance with `InstanceInterruptionBehavior: stop`, never the
AWS defaults (`one-time` + `terminate`). The root EBS volume is
`DeleteOnTermination: true`, so a terminating interruption would take
`~/.kiro/crew` — memory, sessions, config — with it. Stopping keeps the volume
intact, and because the request is persistent, **EC2 restarts the instance by
itself** once capacity frees up.

### What you can and can't do after an interruption

This is the one place Spot is genuinely different from a normal box, so be
precise about it:

| Situation | What works |
|-----------|------------|
| EC2 interrupted it (instance is `stopped`) | **Only EC2 can start it again.** `kirocrew cloud start` fails with an AWS error — wait for the auto-resume. `kirocrew cloud status` shows `stopped` meanwhile. |
| You ran `kirocrew cloud stop` | `kirocrew cloud start` works normally, exactly as on an on-demand box. |
| You're done with it | `kirocrew cloud destroy` — it cancels the Spot request first, *then* deletes the stack. |

That first row is an AWS rule, not a Kiro Crew limitation: "only Amazon EC2 can
restart an interrupted stopped Spot Instance." You can't tell the two stop
reasons apart from `cloud status`, so if `cloud start` returns an AWS error on a
`--spot` box, that's the tell — it was interrupted, and it will come back on its
own when capacity does. You don't have to remember that: the failing `start`
prints it, from the CLI and from the dashboard's Start alike, including the one
instruction that matters — **don't destroy the box to fix it.** Destroy deletes
the root volume the interruption deliberately kept, `~/.kiro/crew` and all.

Two more things the launcher handles for you:

* **The request never expires.** The Spot request is pinned to a far-future
  `ValidUntil`. This matters because an *expired* request counts as a cancelled
  one, and cancelling the request of a **stopped** Spot instance auto-terminates
  that instance — which, with `DeleteOnTermination: true`, would silently destroy
  a box you'd merely parked for a week.
* **`destroy` cancels before it deletes — and won't delete if it can't.** A
  persistent request outlives its instance: terminating the instance flips the
  request back to `open` and EC2 launches a *replacement* — which, once the stack
  is gone, is an orphan nobody tracks and nobody stops billing for. `kirocrew
  cloud destroy` cancels the request first so that can't happen, and terminates
  any instance the request still points at except the stack's own, which the
  stack delete terminates (cancelling an `active` request does **not** stop its
  running instance). If the cancel is refused — or it can't even look the request
  up — it **stops there and deletes nothing**, tells you which request to cancel
  and with what command, and exits non-zero. Your box and its disk are untouched;
  re-run `destroy` once the request is gone. Deleting anyway is precisely how you
  end up with a replacement instance billing outside a stack that no longer
  exists.

The first `--spot` launch in an AWS account also needs the EC2 Spot
service-linked role (`AWSServiceRoleForEC2Spot`). The console creates it
silently; the CLI does not — the policy from `kirocrew cloud iam-policy` grants
creating it (pinned to the Spot service), so re-print and re-apply that policy if
you applied an older copy.

Spot stacks with, rather than replaces, a scheduled stop/start: a schedule cuts
how many hours you pay for, `--spot` cuts the rate for the hours you do run, and
neither touches the NAT gateway charge on a private-subnet deploy (usually the
larger line item). `--spot` applies when the stack is **created** — to move an
existing on-demand box onto Spot, launch a new one with `--new --spot`. Passing
`--spot` while resuming an existing stack warns (and hard-fails under `-y`)
rather than silently leaving you on on-demand pricing.

## EC2 gotchas / troubleshooting

These map to warnings in `kirocrew doctor`.

### A `--spot` launch failed and rolled back

Run `kirocrew cloud destroy` on that tag, even though the stack is already gone.

When CloudFormation rolls a failed launch back it terminates the instance, and a
*persistent* Spot request reacts to a termination by re-opening and asking EC2
for a replacement. The request outlives the launch template rollback deletes, so
nothing closes it out on its own — an orphaned request that nobody owns can
quietly hand you an instance nobody is watching. `destroy` sweeps for a request
tagged with that instance tag, cancels it and terminates any instance it points
at (a replacement is *not* a stack resource, so `delete-stack` would never touch
it), whether or not a stack is still there — so it is the safe thing to run after
any failed `--spot` launch. It is a no-op (one describe call) if there's nothing
to sweep. If the cancel or the terminate is denied, `destroy` prints the ids and
the exact `aws ec2 …` command to finish the job rather than telling you you're no
longer billed.

With no stack left, `destroy` **shows you what it found and asks before it
cancels** (`-y` skips the question, as everywhere else). That prompt is not
ceremony: if the leftover request's instance is merely *stopped*, EC2 terminates
it as the request is cancelled — so this is the one sweep that can take a box,
and its disk, with it.

A replacement instance launched by a re-opened request now carries the same
`kirocrew:managed` / `kirocrew:instance` tags as the original, because the launch
template tags the instances *it* launches (the Spot service relaunches from the
request, not from CloudFormation, so template-level tags are the only ones that
reach a replacement). That is what lets the sweep find it and lets the tag-gated
`ec2:TerminateInstances` in the launcher policy kill it. Orphans from a request
created before this — or from a launch outside Kiro Crew — are untagged, so the
sweep can still only hand you the manual command; that fallback isn't going away.

### `kirocrew cloud start` errors on a `--spot` box

It was almost certainly interrupted rather than stopped by you, and **only EC2
can restart an interruption-stopped Spot instance**. The error now says so, so
you shouldn't have to reach this page — but if you're here: wait for the
auto-resume, the persistent request brings it back when capacity returns. Do
**not** destroy and relaunch to "fix" it; the instance is fine and its volume is
intact, and destroy is what would actually lose your data. See
[What you can and can't do after an interruption](#what-you-can-and-cant-do-after-an-interruption).

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

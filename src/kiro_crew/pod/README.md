# `kirocrew pod` — isolated worktree test instances

Spin up a **throwaway, full-stack KiroCrew gateway** for any feature worktree —
its own port, its own `KIROCREW_HOME` (own DB / sessions / memory), no Slack
tunnel, `--no-crons` (unless you pass `--crons`), resource-capped, and `rm -rf`'d
on stop. Test a branch's
backend `/api/*` **and** the SPA bundle it serves, all **without touching your
live gateway or your shared `~/.kiro/crew` data**.

Think **`kubectl` for local worktree test rigs.** This is the *test line*
(multi-active, burn-on-evict); it is orthogonal to the *live line* (a single
gateway serving real data on the canonical port) and refuses to bind the live port.

## Interface

```bash
kirocrew pod install              # lay down the systemd --user template unit (once per machine)
kirocrew pod provision <wt>       # build the worktree's venv + SPA dist (the on-ramp)
kirocrew pod up   <wt> [--json]   # bring up an isolated pod → {base_url, token, port}
kirocrew pod up   <wt> --provision# provision (if needed) then bring it up
kirocrew pod up   <wt> --approval reads  # boot its gateway in an approval mode
kirocrew pod up   <wt> --crons          # boot its gateway with the cron scheduler on
kirocrew pod ls                   # what's running (≈ kubectl get pods)
kirocrew pod status <wt>          # up/down + health
kirocrew pod token  <wt> [--ttl]  # (re)mint a dashboard token for a running pod
kirocrew pod url    <wt>          # print its base_url
kirocrew pod logs   <wt> [-n N]   # tail its journal
kirocrew pod down   <wt>          # evict → rm -rf its HOME (zero residue)
```

`<wt>` is a friendly worktree name. It is resolved to a checkout **git-natively**:
`kirocrew pod up <name>` matches a linked worktree by its directory basename, its
branch (`<name>` or `feat/<name>`), or an exact path — run it from inside any
KiroCrew checkout (or set `KIROCREW_POD_REPO`). The resolved path is pinned so the
pod's gateway boots without re-consulting git.

## The on-ramp (provisioning)

A worktree must be *built* before it can be podded — an editable
`.venv/bin/kirocrew` and a built SPA bundle (`src/kiro_crew/static/dist`). These
are intrinsic to "a worktree that can run a gateway at all"; pod just surfaces
and collapses them, honoring their very different costs:

| Prereq | Cost | Who builds it |
|---|---|---|
| **venv** | ~1 min, idempotent | `pod up` **auto-builds** it on demand |
| **dist** | minutes (Vite SPA build) | only on **explicit consent** |

So plain `pod up <wt>` builds the cheap venv for you but **fails loud** if the
dist is missing — pointing you at the slow build — while `pod up <wt> --provision`
(or `pod provision <wt>`) runs the full chain: venv + `npm run build` in
`website/` staged into the served `static/dist`.

## A pod IS the worktree's gateway (control plane vs payload)

- **Control plane** — the `kirocrew pod` verbs (resolution, port derivation, unit
  management, token mint, boot *prep*). These run from the **stable, globally
  installed** `kirocrew`, so they never break just because a worktree's code is broken.
- **Payload** — the booted pod *is* the worktree's `.venv/bin/kirocrew gateway`. If
  the worktree's gateway can't start (bad import, broken config, unbuilt dist), the
  pod can't come up — **and that is correct**. `pod up` detects the crash fast,
  prints the gateway's own journal, stops the half-started unit, and tells you this
  is the worktree build failing — not the pod tool.

## Mechanism (Linux `systemd --user`)

`kirocrew pod install` writes a template unit `kirocrew-pod@.service` whose
`ExecStart` re-enters `kirocrew pod _run <wt>` (boot logic lives in
`kiro_crew.pod.runtime.boot`) and whose `ExecStopPost` re-enters
`kirocrew pod _cleanup <wt>`, which re-validates the name and refuses
`..`/absolute/empty before deleting the pod's isolated HOME. Teardown is routed
through Python (not a raw `rm -rf` on `%i`) because a systemd instance name *can*
be `..`. `MemoryMax`/`CPUQuota` cap a runaway pod; `Restart=on-failure` self-heals.

### Port derivation

`port = base + (cksum(name) % 199) + 1` (base `7810` → `7811..8009`), unless a
`PORT=` is pinned in `~/.kiro/crew/pods/<name>.env`. `pod up` refuses if a derived
port ever resolves to the live port.

## Configuration (`PodConfig`, all `KIROCREW_POD_*`-overridable)

| env | default | meaning |
|---|---|---|
| `KIROCREW_POD_REPO` | invoking cwd | repo git is queried from to resolve worktree names |
| `KIROCREW_POD_WORKTREES_ROOT` | (unset) | optional `name→path` fallback root (hermetic planes) |
| `KIROCREW_POD_ROOT` | `~/.kirocrew-pods` | isolated pod HOMEs (nuked on stop) |
| `KIROCREW_POD_ENV_DIR` | `~/.kiro/crew/pods` | per-pod `CHECKOUT=`/`PORT=`/`SEED=` files |
| `KIROCREW_POD_BASE_PORT` | `7810` | port derivation base |
| `KIROCREW_POD_LIVE_PORT` | `5476` | the port a pod must never bind |
| `KIROCREW_POD_UNIT_PREFIX` | `kirocrew-pod` | systemd unit prefix |
| `KIROCREW_POD_BIN` | (auto) | the `kirocrew` binary the unit boots |

Overriding the prefix + roots + base port yields a fully **hermetic pod plane**
that can't collide with a developer's live pods — used by the test suite.

## Safety

- A pod runs its own `KIROCREW_HOME` and binds `127.0.0.1` only; it never touches
  the shared `~/.kiro/crew` data and refuses the live port.
- Every pod's `config.json` forces `tunnel.enabled=false`, and the booted env
  scrubs `SLACK_*` + non-AWS `*_TOKEN`, so a pod can never grab the live Slack
  identity. Pod HOME is `0700`; `config.json` is `0600`.

## Platform

Linux `systemd --user` only. On hosts without `systemctl --user` (macOS, Windows,
or a Linux box with no systemd on PATH), the verbs that touch systemd **refuse
with a single actionable line** — `pod: pods require Linux systemctl --user; this
host is darwin. Use ./dev-backend.sh to preview a worktree on this platform.` —
and exit 1. They never raise a traceback, and `pod install` writes **no** unit
file when the host can't load it.

The gate is `runtime.require_systemd()`, called from the single `systemctl()`
chokepoint plus the two siblings that shell out directly (`recent_journal` and
`_logs`, which run `journalctl`). `pod url` is pure port arithmetic and works
anywhere; `pod up` / `provision` fail earlier on their own preconditions
(worktree resolution, venv/dist) before reaching systemd.

### Session bus

`systemctl --user` locates the per-user systemd instance through
`XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS`. A process descended from a
systemd **system** unit — which is how `kirocrew service install` runs the
gateway — inherits no login-session environment and therefore neither variable,
so pods used to fail with a bare `Failed to connect to bus: No medium found`.

`runtime._systemctl_env()` backfills both when the socket
(`$XDG_RUNTIME_DIR/bus`, else `/run/user/<uid>/bus`) actually exists; an
explicitly-set value always wins. When the socket is genuinely absent — no login
session and `Linger=no` — `require_systemd()` refuses with the fix
(`loginctl enable-linger <user>`) instead of letting systemctl emit a message
that names neither cause nor remedy. `kirocrew doctor` reports the same three
states (present / absent / present-but-no-linger).

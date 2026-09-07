# `kirocrew pod` — isolated worktree test instances

Spin up a **throwaway, full-stack KiroCrew gateway** for any feature worktree —
its own port, its own `KIROCREW_HOME` (own DB / sessions / memory), no Slack
tunnel, `--no-crons` (unless you pass `--crons`), resource-capped, and reclaimed
by `pod down`. Test a branch's
backend `/api/*` **and** the SPA bundle it serves, all **without touching your
live gateway or your shared `~/.kiro/crew` data**.

Think **`kubectl` for local worktree test rigs.** This is the *test line*
(multi-active, burn-on-evict); it is orthogonal to the *live line* (a single
gateway serving real data on the canonical port) and refuses to bind the live port.

Pod separation is **operational and state isolation**, not an adversarial security
boundary against arbitrary processes already running as the same Unix UID. Such a
process can modify user-owned pod storage directly; descriptor pinning prevents
path/symlink substitution from accidentally redirecting an approved operation, but
it does not add per-pod UIDs or mount isolation. Controller v1 runs pod operations
host-side (`main/live Kiro Crew -> target test pod`); a pod does not create or control
a child pod.

## Interface

```bash
kirocrew pod install              # lay down the systemd --user template unit (once per machine)
kirocrew pod provision <wt>       # build the worktree's venv + SPA dist (the on-ramp)
kirocrew pod up   <wt> [--json]   # bring up an isolated pod → {base_url, token, port}
kirocrew pod up   <wt> --provision# provision (if needed) then bring it up
kirocrew pod up   <wt> --approval reads  # boot its gateway in an approval mode
kirocrew pod up   <wt> --crons          # boot its gateway with the cron scheduler on
kirocrew pod up   <wt> --seed minimal  # pre-populate its HOME from a named scenario
kirocrew pod scenarios [--json]        # list named scenarios and their descriptions
kirocrew pod api  <wt> GET sessions    # authenticated request → fixed-key JSON
kirocrew pod ls                   # what's running (≈ kubectl get pods) + orphaned HOMEs (with age)
kirocrew pod prune [--all] [--dry-run]  # bulk-reclaim orphaned HOMEs (default: older than 3d; --all for every age)
kirocrew pod status <wt>          # up/down + health
kirocrew pod token  <wt> [--ttl]  # (re)mint a dashboard token for a running pod
kirocrew pod url    <wt>          # print its base_url
kirocrew pod logs   <wt> [-n N]   # tail its journal
kirocrew pod down   <wt>          # evict → delete its HOME, verified (zero residue)
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

## Seed the isolated home

```bash
kirocrew pod scenarios
kirocrew pod scenarios --json
kirocrew pod up my-wt --seed minimal
kirocrew pod up my-wt --seed ~/.kiro/crew
```

`pod scenarios` reads the packaged fixture registry and lists names in sorted
order. The default human-readable table shortens each description to the last
complete sentence that fits, falling back to a cut between words with an
ellipsis. `--json`
emits a stable array of `{name, description}` objects containing the complete
description scalar. Literal (`|`) blocks preserve newlines and folded (`>`) blocks
normalize to one paragraph. Extraction uses the fixture manifest's narrow scalar
format and does not require PyYAML at runtime.

A bare name selects a fixture shipped under `kiro_crew/tests_fixtures/<name>/`
and populates the whole isolated home. Anything with a path separator or a
leading `~` or `.` stays the directory form, which contributes only a sanitized
`config.json`. The split is syntactic, so an unknown bare name is refused with
the available names instead of being mistaken for a directory and booting a
blank pod. Spell a bare relative directory as a path, for example
`--seed ./my-state`.

Named fixtures are copied directly into the final home with both fixture and
home traversals pinned by directory descriptors. Config sanitization and
workspace setup run through the same held home descriptor, then the fixture
manifest is copied last as the completion marker. A failed partial copy or
setup therefore stays non-bootable even on systemd's automatic retry, and a
symlink or path-name substitution during the operation cannot redirect writes.
This does not confine an already-open inode against arbitrary same-UID host
processes; that limit is part of the operational-isolation boundary above.
Seeded config forces tunnel/channel enablement off and restores the agent
sandbox floor. A populated home is never overwritten or re-seeded: a
`pod up --seed` request against one refuses before start even when its marker
already matches. Use plain `pod up` to restart that home unchanged. Service
restarts keep the sessions and logs already present. After health succeeds,
`pod up` reads the fixture marker back and fails if the requested scenario did
not land.

## Call the pod API without handling its token

```bash
kirocrew pod api my-wt GET sessions
kirocrew pod api my-wt GET '/api/sessions?limit=20'
kirocrew pod api my-wt POST config --data '{"key":"agent.model"}' --allow-write
```

`pod api` makes one request and prints one JSON document with fixed keys:
`{name, method, path, status, ok, body}`. JSON response bodies are decoded into
`body`; other bodies remain text. A non-2xx response prints the same shape and
exits 1. Response reads are capped at 32 MiB and an oversized or truncated body
fails without buffering indefinitely.

GET and HEAD are allowed by default. POST, PUT, PATCH, and DELETE require
`--allow-write`; v1 deliberately has no route-by-route side-effect catalog, so a
safe-method route that mutates state is a server contract defect to fix at that
route. Caller-supplied `token` query parameters are refused without displaying
their value. The command mints its own dashboard token and sends it using the
same `?token=` query contract as the dashboard middleware, never an
`Authorization` header.

The authenticated request travels over the pod's **private dashboard unix
socket** — `<pod home>/dashboard-<port>.sock`, the same file name the gateway
binds, resolved against the pod's isolated home rather than the host's — and
**never over TCP, with no fallback**. The port is only the `Host` the gateway
sees. A pod's port is ordinary loopback: any local user can bind it the moment
the pod releases it, so a pod that exits between the mint and the send would
otherwise hand a token that is valid as an `mc_token_<port>` cookie to whatever
answered next, replayable against the restarted pod. The socket cannot be
answered by another user, because it sits in a home created owner-only and is
itself `chmod 0600`.

A missing socket therefore **refuses through the envelope** (`status: 0`,
`ok: false`, remediation in `body`) instead of retrying on `127.0.0.1:<port>`,
and it refuses *before* minting, so an undeliverable request never pays for a
credential. The refusal is expected while a pod is starting, after it crashed
without a `down`, and on a checkout whose gateway predates the socket; a
`pod down` plus `pod up` clears all three. Requiring the socket costs no
capability: `pod api` is Linux-only like every systemd-touching pod verb, and on
Linux the gateway binds the socket unconditionally.

Before minting, the control plane reads the gateway PID sidecar from the pod's
isolated home and requires it to equal the service manager's current MainPID.
That agreement is the primary ownership attestation and works on minimal hosts
without `lsof` or `netstat`. Listener attribution is additional corroboration
when available; it is never sufficient by itself. Tokens are scrubbed from
response text and transport failures never include the authenticated URL.

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
`kiro_crew.pod.runtime.boot`). Before each start, `pod up` writes a per-instance
drop-in that replaces the template's `ExecStart` with the resolved checkout's
own `.venv/bin/kirocrew`; it refuses to fall back to a global install that may
not understand the requested seed. `pod down` removes that drop-in and reloads
systemd as part of its zero-residue guarantee. `MemoryMax`/`CPUQuota` cap a
runaway pod; `Restart=on-failure` self-heals.

The unit has **no `ExecStopPost` teardown hook**, on purpose. systemd runs
`ExecStopPost` *before* the final kill of the unit's cgroup, so a hook that
deleted the pod's HOME raced the pod's own surviving subprocesses — they
recreated the directory by reopening their audit log in append mode — and it also
fired on the stop half of a `Restart=`, bringing the pod back on a home stripped
of its sessions and config. So `kirocrew pod down` owns reclamation on every
platform: it stops the service, waits for the unit's cgroup to drain, deletes the
HOME through `runtime.cleanup_home` (which re-validates the name and refuses
`..`/absolute/empty, since teardown safety must not rely on systemd `%i`
semantics), then VERIFIES the directory is gone and fails loudly if it is not.
The trade is that a pod which goes away without a `down` — a crash, a raw
`systemctl --user stop`, a reboot — leaves its HOME behind; `pod ls` reports
those with their age, `pod down <wt>` reclaims one, and `pod prune` reclaims
them in bulk — by default only HOMEs whose last activity is older than 3 days
(`--all` sweeps every age; each delete still routes through the same
stop-drain-verify path `down` uses, with liveness re-checked per name).

### Port derivation and allocation

`port = base + (cksum(name) % 199) + 1` (base `7810` → `7811..8009`), unless a
`PORT=` is pinned in `~/.kiro/crew/pods/<name>.env`. `pod up` refuses if a derived
port ever resolves to the live port.

Derivation answers "which port does this name PREFER", and it is a **default hint,
not a contract**. Every reader (`pod url`, `pod ls`, `pod exec`, Dev Fleet) calls it
to agree without coordinating, but 199 slots means two names colliding is ordinary,
and the derived port can equally be held by something that is not a pod. It does NOT
check that the port is free.

Whether the port can be had is asked once, by `pod up`, and the answer is **recorded
as a `PORT=` claim on every allocation** — so after a pod's first `up` its port comes
from that claim rather than from the formula. The formula still picks the
first-preference port for any pod that has never come up, which is why the
degradation is graceful: derivation chooses, ownership is explicit, and readers
follow the claim.

- The pod is already running → nothing is allocated, and the port is re-resolved
  under the lock. Its port is busy because it owns it, and moving a live pod would
  strand every reader.
- A hand-pinned `PORT=` that is busy → refused loudly. A deliberate pin is never
  relocated automatically. A pin outside 1–65535 is refused by name.
- Otherwise the first port that is free, not the live plane, and **not claimed by
  another pod** is taken — walking from just above the preferred slot and wrapping,
  deterministically. It is recorded as `PORT=` plus `PORT_AUTO` (which marks the
  claim as machine-made, so it stays relocatable) and a move is reported on stderr.
- Nothing available → refused loudly, naming the band and the `PORT=` escape hatch.
  A pod that cannot get a port must not appear to start.

Reading other pods' recorded claims is what makes concurrent boots safe: a unit is
`Type=simple`, so `start_pod` returns *before* the gateway binds, and until then a
bind probe reports that port free. Claiming is serialized plane-wide (`pod up` holds
a plane lock across choose → start), and the claim is written before the start, so a
colliding name sees it immediately rather than after the bind.

**A collision that still happens is detected, not mistaken for health.** Allocation
prevents the ordinary cases above, but it cannot prevent all of them: two colliding
names started concurrently can still race inside the window between the service
manager accepting the start and the gateway binding. When that happens whoever binds
first wins and the loser's gateway exits "address already in use", its unit
crash-looping behind `Restart=on-failure`. So reachability on a port is never
evidence that THIS pod is up: `health` and the credential mint both go through
`port_owner`, and `pod status` / `pod ls` print `foreign (port held by another
instance)` when the port belongs to somebody else.

What `port_owner` proves is that the pod's gateway PID sidecar — written into the
isolated home only *after* the bind succeeds — agrees with the service manager's
current `MainPID`. Beside that sidecar the gateway writes its start-time identity in
its own `gateway-<port>.start` file (the pid file itself stays a bare pid, so every
shipped reader keeps parsing it), so a record left behind by a crash cannot attest
once that pid has been recycled onto an unrelated process; a record that cannot prove
its own freshness is refused, and the mint withholds the secret. A record carrying NO
start identity is refused the same way, but it is a different fault with a different
fix: a pod's gateway is its worktree's own venv binary, so a checkout that predates
the sidecar writes no identity and a restart adds none — the mint says so and points
at re-provisioning the worktree rather than at a restart. Listener attribution
(`lsof`) is corroboration on top: a *different* pid holding the `127.0.0.1`
listener is positive proof of a foreign responder and overrides the record, but an
absent, failed, or unattributable lookup is not evidence of anything and leaves the
record's verdict standing. That last case is the norm, not an edge: a minimal Linux
host may ship no `lsof` at all, and an unprivileged caller — which is how `pod api`
runs — cannot see a socket held by a gateway the user's service manager started. `pod up` names the conflict and points at `PORT=` rather than blaming the
worktree build, and pinning a colliding pod's own `PORT=` remains the manual way out.

## Configuration (`PodConfig`, all `KIROCREW_POD_*`-overridable)

| env | default | meaning |
|---|---|---|
| `KIROCREW_POD_REPO` | invoking cwd | repo git is queried from to resolve worktree names |
| `KIROCREW_POD_WORKTREES_ROOT` | (unset) | optional `name→path` fallback root (hermetic planes) |
| `KIROCREW_POD_ROOT` | `~/.kirocrew-pods` | isolated pod HOMEs (reclaimed by `pod down`) |
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
- Every pod's `config.json` forces `enabled=false` on the tunnel and on every
  channel that carries a config-level enable (`runtime.SEED_DISABLED_SECTIONS`),
  and the booted env scrubs `SLACK_*`, `WECOM_*`, `MICROSOFT_APP_*` and non-AWS
  `*_TOKEN`, so a pod can never grab a live messaging identity — not even a
  seeded one, which is the point: `--seed ~/.kiro/crew` clones the real config.
  Pod HOME is `0700`; `config.json` is `0600`.

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

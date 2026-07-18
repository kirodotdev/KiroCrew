# Sandbox PID-Namespace Isolation (signal-broadcast containment)

Status: implemented. Config: `agent.sandbox_pid_namespace` (default `true`). Linux only.

## Problem

Every KiroCrew session subprocess (kiro-cli ACP sessions, cron script/command
sandboxes, app backends) runs as the gateway's own uid. POSIX signal
permissions are uid-based and ignore cgroup/session boundaries, so **any**
subprocess can SIGKILL the gateway, the systemd --user manager, and every
other session with a single call — and SIGKILL is uncatchable.

This is not theoretical. On 2026-07-15 a unit test in a feature worktree
mocked `subprocess.Popen` with a bare `MagicMock`; the timeout-cleanup path
ran `os.killpg(os.getpgid(proc.pid), SIGKILL)` with the mock's pid, which
coerces to `1` via `__index__`. `killpg(1, sig)` is `kill(-1, sig)` in libc —
a broadcast to every process the uid owns. Five complete login-session
wipeouts in one day (incident note:
`~/.kirocrew/workspace/notes/2026-07-15-incident-kill-broadcast.md`).

Code-level guards (refusing pgid ≤ 1 in KiroCrew's own kill paths) close the
paths KiroCrew controls, but agent-executed code — shells, builds, pytest —
can still contain arbitrary signal calls. Same-uid SIGKILL cannot be blocked
by the kernel's permission model; the only structural defense is to make the
outside world **unreachable**.

## Design

The Linux sandbox launcher (`src/kiro_crew/sandbox.py`) already runs every
sandboxed subprocess inside user + mount namespaces:

```
fork → unshare(CLONE_NEWUSER) → identity uid/gid map
     → unshare(CLONE_NEWNS) → bind-mounts → exec payload
```

This feature adds a third namespace:

```
     → unshare(CLONE_NEWPID)          # does not move US —
     → fork → grandchild = ns PID 1   #   the next fork enters the ns
         → mount proc /proc           # ns-local pid view
         → fork payload (ns PID 2), exec
         → mini-init: forward SIGTERM/SIGINT to payload,
           reap orphans via waitpid(-1), propagate exit status
           (128+signum on signal death)
```

Inside a PID namespace, `kill(-1)` / `killpg` can only address ns-local
processes. The gateway, the user manager, and sibling sessions do not exist
from the payload's point of view — the 2026-07-15 incident call becomes a
no-op `ESRCH` (Linux `kill(-1)` excludes the caller itself, and a pid-ns
init is kernel-protected from ns-internal signals).

Isolation is one-directional by construction:

- **Outside → inside still works.** Process groups are orthogonal to PID
  namespaces; the gateway's reaper/cancel (`killpg` on the real pgid) reaches
  every ns process. `Popen`-observed pids are real pids.
- **Inside → outside is impossible** for signals, regardless of what code the
  agent runs.

When ns PID 1 (the mini-init) exits, the kernel SIGKILLs every remaining
process in the namespace — no orphan leaks.

### Degradation

`unshare(CLONE_NEWPID)` failing at runtime (old kernel, restricted host)
logs a warning to stderr and execs the payload without pid isolation —
identical to pre-feature behavior. macOS (Seatbelt backend) is unchanged.

### Kill-switch

`agent.sandbox_pid_namespace: false` in `~/.kirocrew/config.json` disables
the PID namespace globally (resolved lazily in `wrap_argv`, mtime-cached
config). Emergency use only; user + mount namespaces remain active.

## Identity across the namespace boundary

Code inside the ns sees ns-local pids (`os.getpid()` = 2, `os.getppid()` = 1),
which breaks the legacy identity recovery where stubs walked `/proc` ancestry
to find `config_dir()/session_pid_<pid>.txt` files written by the gateway
outside (real pids).

Fix: **resolution moves server-side.** `SO_PEERCRED` on the gatewayd unix
socket returns the peer's pid *translated into gatewayd's namespace* (a real
pid). When a stub registers with an empty session key, gatewayd first
**positively verifies the peer uid** (`check_peer_uid` must return `MATCH`;
deny-by-default — an unverifiable peer is never granted an identity), then
walks the real `/proc` ancestry itself, resolves the `session_pid_*.txt`
mapping, and returns the session key in the register response; the stub
adopts it (never overwriting an existing key). The deny-by-default recaller
posture is unchanged.

## Not covered

- Same-uid processes started **outside** the sandbox (user terminals, systemd
  units) are not confined — this protects the gateway *from its sessions*.
- Resource exhaustion (memory/CPU/fd) is out of scope here; cgroup limits are
  a separate concern.

## Tests

- `test/test_sandbox_pid_namespace.py` — template generation, flag plumbing,
  live e2e isolation (`kill(-1)` → ESRCH), incident replay containment,
  exit-status propagation through the mini-init, kill-switch behavior.
- gatewayd server-side resolution tests (see Phase B test files).

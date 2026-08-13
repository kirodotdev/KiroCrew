# `kirocrew pod` — Design

## Module layout

A self-contained package under the main source tree — nothing ships outside it,
so a `pip install` of KiroCrew is complete. Mirrors the thin-verb / runtime split
used elsewhere in the CLI.

```
src/kiro_crew/pod/
  __init__.py     # exports PodConfig, PodError, derive_port, resolve_checkout, pod_home, pod_unit
  config.py       # PodConfig dataclass — every path/knob, KIROCREW_POD_*-overridable
  runtime.py      # git worktree resolution, port derivation, systemd wrappers, boot, token mint
  provision.py    # venv + SPA-dist build (the on-ramp)
  unit.py         # systemd --user template unit (generated, not shipped)
  cli.py          # thin verb layer (up/down/ls/status/token/url/logs/install/provision)
```

Wiring into the existing CLI (`src/kiro_crew/cli.py`):
- add a `pod` sub-parser next to the other `sub.add_parser(...)` calls, with a
  `pod_sub = pod_parser.add_subparsers(dest="pod_action")` (same shape as
  `cron`/`spawn`/`security`);
- add `elif args.command == "pod": _pod(args)` in the dispatch chain;
- implement `_pod(args)` in `cli_commands.py` → `from kiro_crew.pod.cli import dispatch; dispatch(args)`
  (same delegation pattern as `_security` / `_policy`).

## Control plane vs payload

Two layers with deliberately different failure semantics:

- **Control plane** — the `kirocrew pod` verbs (worktree resolution, port
  derivation, unit management, token mint, boot *prep*). These run from the
  **stable, globally-installed** `kirocrew`, so they never break because a
  worktree's code is broken.
- **Payload** — the booted pod **is** the worktree's `.venv/bin/kirocrew gateway`.
  If the worktree's gateway can't start, the pod can't come up — and that is
  correct: there is nothing to test if the thing under test won't boot. The
  guarantee is that this failure is **fast and clearly attributed** (US-5).

## Worktree resolution (git-native)

KiroCrew worktrees are **plain git worktrees** (flat repo root — no nested package
subdir), created wherever the developer likes
(`git worktree add ../<name> -b feat/<name> main`). Rather than tie pods to a
fixed root, a friendly `name` is resolved to an absolute checkout path by
**asking git**, then **pinned** so the systemd-booted gateway never re-resolves.

`resolve_checkout(cfg, name, *, cwd)` order:
1. **Pinned** — `CHECKOUT=<abspath>` in the per-pod env file `<env_dir>/<name>.env`
   (used only if the dir still exists). Authoritative: written by `pod up`, read
   by `boot()` (incl. systemd `Restart=`), so **boot never shells git** from its
   clean environment.
2. **git** — `git -C <ref> worktree list --porcelain`, where `<ref>` is
   `KIROCREW_POD_REPO` if set else the invoking `cwd` (git lists ALL linked
   worktrees from any one of them). `name` matches, in order: a worktree path's
   basename, its checked-out branch (`name` or `feat/<name>`), or an exact path.
3. **root fallback** — `KIROCREW_POD_WORKTREES_ROOT/name`, only if that env is set
   (hermetic test/CI planes; no git or real repo needed).
4. else → `PodError` teaching `git worktree add ../<name> -b feat/<name> main`.

`pod up` / `pod provision` resolve via git at control-plane time (run from inside
a checkout) and **pin the result** (`runtime.pin_checkout` → writes `CHECKOUT=`
into the env file, merge-preserving any `PORT=`/`SEED=`). `boot()` reads only the
pinned `CHECKOUT=` and is FATAL if absent — which cannot happen on the normal
`up` path. Derived layout from the resolved checkout:

| | value |
|---|---|
| venv binary | `checkout / ".venv" / "bin" / "kirocrew"` |
| built dist | `checkout / "src" / "kiro_crew" / "static" / "dist"` |

Only `up` / `provision` / `boot` need the checkout path; `ls` / `status` /
`derive_port` / `token` / `logs` need only `name → port` (cksum) + health, so they
never touch git.

## Port derivation

`port = base + (cksum(name) % 199) + 1`, base `7810` → `7811..8009`, unless a
`PORT=` is pinned in `<env_dir>/<name>.env`. We shell the POSIX `cksum` binary
(a specific CRC that is **not** `zlib.crc32`) so any external tooling that derives
the same port stays in agreement. `pod up`/`boot` refuse if a derived port equals
the live port.

## Provisioning (the on-ramp)

Cost asymmetry drives the design:

| Prereq | Cost | Policy |
|---|---|---|
| **venv** | ~1 min, idempotent | auto-built on demand by `pod up` |
| **dist** | minutes (Vite SPA build) | only on explicit `--provision` consent |

- `ensure_venv(checkout)`: `python3.12 -m venv .venv` then `.venv/bin/pip install -e .`
  (editable install → provides `.venv/bin/kirocrew`).
- `build_dist(checkout)`: `npm run build` in `<checkout>/website` (→ `website/dist`),
  then stage `website/dist` into the served `src/kiro_crew/static/dist`. Progress
  goes to **stderr** so a concurrent `pod up --json` keeps a clean stdout.

## Boot (the `ExecStart` body)

`kirocrew pod _run <name>` (hidden verb) → `runtime.boot()`:
1. validate name; read pinned `CHECKOUT=` from the env file (FATAL if absent);
2. FATAL if venv missing or `static/dist` missing (attributed to the worktree build);
3. FATAL if derived port == live port;
4. read per-pod `SEED=` from the env file;
5. `write_pod_config(home_dir, seed)` — create HOME `0700`, write a
   `tunnel.enabled=false` `config.json` `0600` (sanitized seed or minimal);
6. `os.execve(bin_path, ["kirocrew","gateway","--no-crons"], env)` with `build_pod_env(...)`.

`build_pod_env` sets `KIROCREW_HOME`, `KIROCREW_PORT`, `KIROCREW_PROJECT_DIR`, a
scrubbed PATH, and scrubs `SLACK_*` + non-AWS `*_TOKEN` (NFR-2; `AWS_*` incl.
`AWS_SESSION_TOKEN` kept so agent turns can run).

## systemd `--user` template unit

`kirocrew pod install` writes `~/.config/systemd/user/kirocrew-pod@.service`:
- `ExecStart={bin} pod _run %i` — boot logic stays in Python (no shipped shell).
- `ExecStopPost={bin} pod _cleanup %i` — `runtime.cleanup_home` re-validates `%i`
  and confirms it's a direct child of the pod root before `rm -rf` (NFR-3);
  routed through Python because `%i` can be `..` even though it can't contain `/`.
- `MemoryMax=4G`, `CPUQuota=200%` — a runaway pod can't starve the live plane.
- `Restart=on-failure` — self-heal a crash without fighting a deliberate stop.
- `Environment=` lines pin every non-default `KIROCREW_POD_*` (roots/ports/prefix/
  PATH, plus `KIROCREW_POD_REPO`/`KIROCREW_POD_WORKTREES_ROOT` when set) so the
  systemd-booted gateway resolves the same plane the installing CLI used (systemd
  starts with a clean env). `KIROCREW_POD_BIN` overrides the booted binary.

## Token mint

Reads the pod's own `<home>/.local_secret` in-process, then requests
`http://127.0.0.1:<port>/api/token/local?ttl=<ttl>` with header `X-Local-Secret`.
Same local-token flow the dashboard already exposes, scoped to the isolated HOME.

## Configuration (`PodConfig`, all `KIROCREW_POD_*`-overridable)

| env | default | meaning |
|---|---|---|
| `KIROCREW_POD_REPO` | invoking cwd | repo git is queried from to resolve worktree names |
| `KIROCREW_POD_WORKTREES_ROOT` | (unset) | optional `name→path` fallback root (hermetic test planes) |
| `KIROCREW_POD_ROOT` | `~/.kirocrew-pods` | isolated pod HOMEs (nuked on stop) |
| `KIROCREW_POD_ENV_DIR` | `~/.kirocrew/pods` | per-pod `CHECKOUT=`/`PORT=`/`SEED=` files |
| `KIROCREW_POD_BASE_PORT` | `7810` | port derivation base |
| `KIROCREW_POD_LIVE_PORT` | `5476` | the port a pod must never bind |
| `KIROCREW_POD_UNIT_PREFIX` | `kirocrew-pod` | systemd unit prefix |
| `KIROCREW_POD_BIN` | (auto) | the `kirocrew` binary the unit boots |
| `KIROCREW_POD_PATH` | generic PATH | PATH handed to a booted pod gateway |

## Security model (maps to NFRs)
- **Isolation** — own HOME + loopback health checks + live-port refusal (NFR-1).
- **No Slack leak** — config `tunnel.enabled=false` + env scrub (NFR-2).
- **Safe teardown** — Python-validated `cleanup_home`, not raw `rm` on `%i` (NFR-3).
- **Secrets** — in-process secret read, sensitive-path guard on `--seed`, `0600`
  config, `0700` HOME (NFR-4).
- **Audit** — best-effort security-event-log on every state-changing verb (NFR-5).

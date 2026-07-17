# `kirocrew pod` — Requirements

## Overview

`kirocrew pod` gives developers **kubectl-style, throwaway, full-stack test
instances**, one per feature worktree. A *pod* is an ephemeral KiroCrew gateway
booted from a worktree's **own** `.venv`, on its **own** deterministic port, with
its **own** `KIROCREW_HOME` (isolated DB / sessions / memory), **no Slack tunnel**,
`--no-crons`, resource-capped, and `rm -rf`'d on stop.

It lets a contributor exercise a worktree's full stack — the backend `/api/*`
**and** the SPA bundle the gateway serves on the same port — **without touching
their live gateway or the shared `~/.kirocrew` data**.

This is the *test line* (multi-active, burn-on-evict); it is orthogonal to the
*live line* (a single gateway serving real data on the canonical port `5476`) and
MUST refuse to bind the live port.

## User stories

### US-1 — Bring up an isolated pod
As a contributor, I want `kirocrew pod up <worktree>` to boot that worktree's full
stack on its own port and hand me a `{base_url, token}`, so I can click into the
dashboard and test my branch without disturbing my live instance.

- WHEN the worktree exists and is built, THE SYSTEM SHALL boot the pod and print
  `base_url`, a dashboard `token`, and a ready-to-open URL.
- WHEN `--json` is passed, THE SYSTEM SHALL emit `{name,status,port,base_url,token,ttl}`
  as machine-readable JSON on stdout and nothing else on stdout.
- WHEN the derived port equals the live port, THE SYSTEM SHALL refuse and exit
  non-zero.

### US-2 — Provisioning on-ramp
As a contributor, I want pod to walk me through building a worktree so it can run.

- WHEN the worktree has no `.venv`, THE SYSTEM SHALL auto-build it on demand
  (cheap, idempotent).
- WHEN the worktree has no built SPA `dist`, THE SYSTEM SHALL fail loud and point
  me at the slow build, UNLESS `--provision` is given.
- WHEN `--provision` (or `kirocrew pod provision <worktree>`) is given, THE SYSTEM
  SHALL run the full chain: venv + `npm run build` in `website/` staged into the
  served `static/dist`.
- WHEN the worktree directory does not exist, THE SYSTEM SHALL tell the user how to
  create one with `git worktree add`.

### US-3 — Lifecycle & inspection
- `kirocrew pod ls` SHALL list running pods with port + health (≈ `kubectl get pods`).
- `kirocrew pod status <wt>` SHALL report up/down + port + health.
- `kirocrew pod url <wt>` SHALL print the pod base URL.
- `kirocrew pod token <wt> [--ttl]` SHALL (re)mint a dashboard token for a running pod.
- `kirocrew pod logs <wt> [-n N]` SHALL tail the pod's journal.
- `kirocrew pod down <wt>` SHALL evict the pod and delete its isolated HOME with
  zero residue, leaving the live plane untouched.

### US-4 — One-time install
As a contributor, I want `kirocrew pod install` to lay down the `systemd --user`
template unit once per machine, after which `pod up`/`down` just work.

### US-5 — Fast, attributed failure
As a contributor, WHEN my worktree's gateway can't start (bad import, broken
config, unbuilt dist), I want `pod up` to stop waiting the moment the unit
crash-loops, print the gateway's own journal, stop the half-started unit, and tell
me clearly that this is the worktree build failing — not the pod tool.

## Non-functional requirements

### NFR-1 — Isolation (safety-critical)
- A pod MUST run its own `KIROCREW_HOME`; it MUST NOT read or write the live
  `~/.kirocrew`.
- A pod MUST NOT bind the live port (`5476`) under any derivation or pin.
- A pod MUST NOT be reachable off-loopback: it binds `127.0.0.1` only.

### NFR-2 — No Slack-identity leakage
- Every pod's `config.json` MUST have `tunnel.enabled=false` (guaranteed by config,
  not merely by env absence).
- The booted gateway env MUST have `SLACK_*` and non-AWS `*_TOKEN` scrubbed so a
  pod cannot inherit and re-use the live plane's Slack bot identity. `AWS_*`
  (including `AWS_SESSION_TOKEN`) is intentionally preserved so agent turns can run.

### NFR-3 — Safe teardown
- Teardown MUST NOT rely on `systemd %i` semantics for `rm` safety: it MUST
  re-validate the instance name and confirm the target is a direct child of the
  pod root before deleting, refusing `..`/absolute/empty.

### NFR-4 — Secret handling
- Token mint MUST read the pod's own `.local_secret` inside this process (never via
  a shelled `cat`), and call the pod's `/api/token/local` with the `X-Local-Secret`
  header.
- A user-supplied `--seed` config path MUST be rejected if it resolves to a
  sensitive location (credential dirs), and its `tunnel` MUST be forced off before
  use.
- Pod HOME is `0700`; `config.json` is `0600` (may carry provider keys on a shared host).

### NFR-5 — Auditability
- Security-relevant operations (unit start/stop, token mint, isolated-gateway boot,
  unit install, teardown) MUST emit a security-event-log entry, best-effort, and
  MUST NOT let an audit failure break the verb (but SHALL log the audit failure).

### NFR-6 — Platform
- Linux `systemd --user` only. On hosts without `systemctl --user`, the systemd
  verbs SHALL report the failure rather than pretend success.

## Out of scope (v1)
- macOS / non-systemd backends.
- A `pod test` verb that drives Playwright (the e2e harness) — pods expose the
  surface; the harness is separate.
- A configured fixed worktree root as the *primary* resolver — git is the primary
  resolver (design.md → Worktree resolution); a root
  (`KIROCREW_POD_WORKTREES_ROOT`) is only an optional fallback for hermetic planes.

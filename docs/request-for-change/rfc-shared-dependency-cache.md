---
title: Shared Dependency Cache for Worktrees
status: draft
kind: change
author: Pearce Kieser (Pearcekieser)
created: 2026-09-11
last-audited: 2026-09-11
audited-at: dbae53485
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# Shared Dependency Cache for Worktrees

Every Kiro Crew worktree carries its own copy of the same dependencies. On a
host that develops the way the repo's own skills recommend — one worktree per
change, one pod per worktree — that copy is the dominant cost of a worktree,
and it is paid in full for the twentieth worktree as for the first. This RFC
makes the Python half of that cost shared by default and records the decision
not to change the Node half yet.

## Problem

Measured on one contributor host with 27 Kiro Crew worktrees under one XFS
volume (2026-09-11, main at `dbae53485`):

| Per worktree | Size | Notes |
|---|---|---|
| `website/node_modules` | 720–950 MB | `npm ci`; 8 distinct `package-lock.json` versions across the 27 |
| `.venv` | ~400 MB | `python -m venv` + `pip install -e . --group dev` |
| `temp-screenshots/` | ~280 MB | gitignored evidence, never pruned |
| `test/__pycache__` | ~175 MB | |
| `.mypy_cache` | ~110 MB | |
| Everything tracked | ~600 MB | source, tests, docs |

The two dependency trees are 55–60 % of a worktree. Both are byte-identical
across worktrees on the same lockfile, and most of their files are identical
even across lockfile versions, because a lockfile bump changes a handful of
packages.

`kirocrew pod provision` and `pod up` build the venv with `python -m venv` and
pip, which copies every wheel into every venv. The install also takes about a
minute, which is long enough that the module docstring calls it out as the
reason `pod up` auto-builds the venv but not the dist.

## Goals

- A worktree's Python venv costs seconds and near-zero unique disk when the
  host can share; the same command produces the same venv when it cannot.
- No new dependency: `uv` is already declared in `setup.cfg` (`uv>=0.5,<1`,
  shipped as a wheel and located with `uv.find_uv_bin()`), so a stock install
  has it; an install repackaged without the binary, or one that opts out, gets
  exactly today's behaviour.
- One explicit opt-out, for hosts where the shared path misbehaves.
- The node side is decided on evidence, not left implicit.

## Non-goals

- Changing what is installed. The venv still holds the editable package plus
  the PEP 735 `dev` group, resolved from the worktree's `pyproject.toml`.
- Adding a lockfile for Python (`uv.lock`). The repo resolves fresh from
  `pyproject.toml` today and CI does too; a lockfile is a separate decision
  about reproducibility, not about disk.
- Changing the repo's Node package manager or lockfile. See Alternatives.

## Design

### Phase 1 — `uv` for pod provisioning (this RFC's implementation)

`pod/provision.py::ensure_venv` prefers `uv` when `_find_uv` locates it. The
ladder lives once, in `kiro_crew/env.py::resolve_uv`, and both consumers —
pod provisioning and the pptx-maker engine — call it: `uv.find_uv_bin()` first
(`uv` is a declared dependency shipped as a wheel, so the binary is in the
venv's scripts dir even when a systemd/launchd gateway runs with a minimal
`PATH`), then `shutil.which("uv")` for an install whose wheel lacks the binary,
never raising. Only then does it run:

```
uv venv --seed --python <python3.12> .venv
uv pip install --link-mode hardlink --python .venv/bin/python \
    --project <checkout> --editable <checkout> --group dev
```

Three flags carry the design and are pinned by tests:

- `--link-mode hardlink`, explicitly. The disk saving is the point, and uv's
  default was observed to copy on a host where hardlinking works (0 of 8,647
  site-packages files linked; with the flag, 8,387 linked and ~1 MB unique).
  When the cache and the worktree are on different filesystems uv warns and
  copies; the install still succeeds. `--link-mode clone` (copy-on-write
  reflinks) was measured as the alternative: on this XFS volume it silently
  degraded to a full copy (0 shared extents, 354 MB per venv), so it would
  erase the saving on exactly the Linux hosts that motivate the change. See
  Risks for the hazard hardlinks carry that reflinks would not.
- `--project <checkout>`, so `--group dev` reads the worktree's
  `pyproject.toml` regardless of the caller's working directory. The Dev Fleet
  backend and a login shell provision from different directories; without the
  flag uv errors out looking for a `pyproject.toml` in the cwd.
- `--seed` on `uv venv`, so the venv carries `pip`. uv omits it by default and
  nothing in provisioning needs it, but `make backend` drives `$(VENV)/bin/pip`
  and contributors run `.venv/bin/pip …` by hand; a pod-provisioned worktree
  must not differ from a pip-built one in that respect.

If uv is absent, `KIROCREW_PROVISION_PIP_ONLY` is set, or either uv step exits
nonzero, the existing pip path runs unchanged over whatever is in `.venv`,
including its own pip-too-old fallback. The fallback deletes nothing:
`python -m venv` takes over a half-built directory the same way it already does
after an interrupted pip run (verified: a uv-created venv is re-initialised in
place and pip installs into it), and provisioning has no lock, so a deletion
here could remove a venv that a concurrent Dev Fleet or CLI provision had just
finished. Nothing about the resulting venv's layout changes, so `has_venv`, the
pod runtime and the Dev Fleet view are untouched.

Measured on the same host: 9 s instead of ~60 s, ~1 MB instead of ~400 MB per
additional worktree.

### Phase 2 — `make backend`

The `backend` Makefile target has the same shape (interpreter gate, `python -m
venv`, three pip installs) and would take the same uv-first branch with the
same opt-out. It is a separate PR because the target also carries the
`--prefer-binary` and macOS re-sign steps, and because a contributor's primary
checkout is one venv, not twenty; the win is smaller and the blast radius is
every contributor.

### Phase 3 — Node: hardlink deduplication, not a package-manager switch

`website/node_modules` is the larger half and is deliberately left on `npm ci`.
The proposed remedy is a post-install deduplication pass over sibling
worktrees, offered as a documented command first and as a `pod` verb if it
earns one:

```
hardlink -t <parent>/*/website/node_modules      # util-linux; -t ignores mtime
```

Measured: ~750 MB reclaimed per pair of worktrees on the same lockfile, ~630 MB
per pair on different lockfile versions. `-t` is required because npm stamps
extraction time, so byte-identical files differ in mtime. It is safe with npm's
install model: `npm ci` deletes and re-extracts `node_modules`, so a reinstall
in one worktree never edits a file another worktree shares; the link count
drops. A `pod` verb would run this over the pod root after each provision.

## Alternatives considered

**pnpm.** The right tool for this problem in the abstract: a content-addressed
store with hardlinked `node_modules`. Rejected for now because the repo's
lockfile is `website/package-lock.json` and CI runs `npm ci`. A per-worktree
`pnpm import` produces a second, untracked lockfile that drifts from the real
one, and pnpm's strict `node_modules` layout breaks packages that rely on
hoisted phantom dependencies — a class of failure that would surface only on
the developer's machine. Switching the repo to pnpm is a legitimate proposal,
but it is a CI, release and desktop-packaging change, and it should be made on
its own evidence rather than smuggled in under a disk-space fix.

**`npm install --install-strategy=linked`.** npm's own store-and-link mode.
Still marked experimental by npm, and its hoisting differs from `npm ci`'s in
the same way pnpm's does. Same objection, weaker tool.

**A shared `node_modules` symlinked per lockfile hash.** The repo's
`.gitignore` already notes that a worktree's `node_modules` "is often a
SYMLINK to a sibling checkout's", so the practice exists informally. It needs a
lock around concurrent installs, breaks when `npm ci` follows the symlink, and
saves less than hardlink deduplication because it cannot share across lockfile
versions. Deduplication gets the saving without a new install protocol.

**`uv sync` with a committed `uv.lock`.** Would give reproducible resolution
on top of the shared cache. Out of scope here (see Non-goals); nothing in
Phase 1 prevents it later, and `uv pip install` and `uv sync` share the cache.

**Do nothing, document cleanup.** The gitignored caches (`temp-screenshots/`,
`__pycache__`, `.mypy_cache`) are ~30 % of a worktree and are worth pruning,
but they are not the dependency copies, and pruning them does not change what
the next `pod up` costs.

## Risks

- **A hardlink is shared in both directions.** Editing a file under one
  worktree's `site-packages` in place — a debugging print dropped into a
  dependency, or an agent running inside a pod patching a library — mutates
  the same inode in every sibling venv and in the uv cache itself, and the
  cache stays poisoned until that package version is evicted or re-fetched.
  pip-built venvs never had this failure mode. This is not a new *security*
  boundary crossing: pod separation is operational, not adversarial (pod
  README — a same-UID process can already write any sibling's `.venv` or
  `~/.cache/uv` directly, and the agent sandbox hides credential paths without
  confining writes), so a hardlink adds no capability an agent lacks; what it
  adds is a *blast-radius* change for an honest mistake. Mitigations: uv's cache is
  content-addressed by package version, so `uv cache clean <package>` followed
  by a re-provision repairs every affected venv at once; the opt-out exists for
  a worktree that must be patched by hand; and this is the same contract
  pnpm, Nix and every other content-addressed store impose, so it is a known
  shape rather than a new one. Both places that hand a human or an agent the
  uv recipe — CONTRIBUTING and the bundled `kirocrew-worktree-dev` skill — carry
  the one-line "never edit site-packages in place" warning beside it, because
  the skill's reader is exactly the actor who would.
- **uv resolves independently of pip.** Both resolve fresh from the same
  `pyproject.toml` with no lockfile, so neither is more "CI-parity" than the
  other; but a resolver difference on a loosely pinned dependency would show
  up as a pod-only test result. Mitigation: the opt-out, and the fact that the
  pod venv is already not the CI venv (CI installs on a clean runner).
- **Cache eviction under a running pod.** `uv cache clean` unlinks the cache's
  copy of a file; hardlinked venvs keep theirs. Not a correctness risk.
- **Different filesystems.** uv warns and copies. The install works; the host
  just does not get the saving. Worth a line in the provisioning output, which
  uv already prints.
- **Windows.** `_find_uv` resolves `uv.exe`; hardlinks work on NTFS. Pods are
  Linux-only, so this path is exercised on Windows only through `has_venv`,
  which is unchanged.

## Rollout

1. This PR: Phase 1 plus tests, this document, the dev-fleet spec update and a
   CONTRIBUTING note showing the uv form of the manual install.
2. Phase 2 after Phase 1 has been on main long enough for the pip fallback to
   have been exercised in the wild (an opted-out or wheel-less install
   provisioning a pod).
3. Phase 3 as a documented command immediately; as a `pod` verb only if people
   run it by hand more than once.

## Open questions

- Whether the repo wants a Python lockfile at all is a separate RFC.

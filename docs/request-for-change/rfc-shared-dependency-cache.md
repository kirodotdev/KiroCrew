---
title: Shared Dependency Cache for Worktrees
status: draft
kind: change
author: Pearce Kieser (Pearcekieser)
created: 2026-09-11
last-audited: 2026-10-06
audited-at: 9348a25a34
doc-pr: 12937
implementation-prs: [10259]
tracking-issues: [10258]
supersedes: []
superseded-by: []
---

# RFC: Shared Dependency Cache for Worktrees

Every Kiro Crew worktree carries its own copy of the same dependencies. On a
host that develops the way the repo's own skills recommend — one worktree per
change, one pod per worktree — that copy is the dominant cost of a worktree,
and it is paid in full for the twentieth worktree as for the first. This RFC
makes the Python half of that cost shareable, first behind an explicit opt-in
and then by default, and records the decision not to change the Node half yet.

The default flip is a product-shape change: `pod provision` and `pod up` would
build a different venv than they build today. The First Principles review lane
requires such a change to trace to a document with a non-`draft` status on the
**base** branch, so this document lands on its own and the flip follows in a PR
that rebases onto it. The opt-in path needs no such record and ships first.

Status: draft (no maintainer acceptance recorded). Phase 1, the opt-in, is on main
([#10259](https://github.com/kirodotdev/KiroCrew/pull/10259)): `resolve_uv` in
`src/kiro_crew/env.py` and `KIROCREW_PROVISION_USE_UV` in
`src/kiro_crew/pod/provision.py`. Phases 2–4 are proposals.

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
- No new dependency: `uv` is already declared in `setup.cfg` (`uv>=0.6.7,<1`,
  shipped as a wheel and located with `uv.find_uv_bin()`), so a stock install
  has it; an install repackaged without the binary gets exactly today's
  behaviour.
- One explicit switch. While the shared path is opt-in, the switch turns it on;
  once it is the default, the same switch turns it off.
- The sharing is copy-on-write, so a write in one venv can never reach the
  cache or a sibling venv. No write fence is needed.
- The node side is decided on evidence, not left implicit.

## Non-goals

- Changing what is installed. The venv still holds the editable package plus
  the PEP 735 `dev` group, resolved from the worktree's `pyproject.toml`.
- Adding a lockfile for Python (`uv.lock`). The repo resolves fresh from
  `pyproject.toml` today and CI does too; a lockfile is a separate decision
  about reproducibility, not about disk.
- Changing the repo's Node package manager or lockfile. See Alternatives.

## Design

### Phase 1 — `uv` for pod provisioning, opt-in ([#10259](https://github.com/kirodotdev/KiroCrew/pull/10259), merged)

`pod/provision.py::ensure_venv` keeps `python -m venv` + pip as its default.
When `KIROCREW_PROVISION_USE_UV` is truthy (`1`/`true`/`yes`/`on`, read through
the repo's `env_flag_enabled`, so `0` and `false` do not opt in) it tries `uv`
first. The resolution ladder lives once, in `kiro_crew/env.py::resolve_uv`, and
both consumers — pod provisioning and the pptx-maker engine — call it:
`uv.find_uv_bin()` first (`uv` is a declared dependency shipped as a wheel, so
the binary is in the venv's scripts dir even when a systemd/launchd gateway runs
with a minimal `PATH`), then `shutil.which("uv")` for an install whose wheel
lacks the binary, never raising. Only then does it run:

```
uv venv --seed --allow-existing --link-mode clone --python <python3.12> .venv
uv pip install --link-mode clone --python .venv/bin/python \
    --project <checkout> --editable <checkout> --group dev
```

Three flags carry the design and are pinned by tests:

- `--link-mode clone`, explicitly, on both uv steps: `uv venv --seed` installs
  pip and setuptools from the same global cache as `uv pip install`, so the
  seed step needs the mode too. uv picks clone only on macOS by default;
  on Linux its default was observed to copy on a host whose XFS volume supports
  reflinks. Clone is copy-on-write: the venv shares extents with uv's cache
  until something writes to a file, and that write copies the extent, so an
  edit in one venv cannot reach the cache or a sibling venv. Measured on the
  motivating host (XFS, `cp --reflink=always` succeeds): a `--group dev`
  install produced 171 MB of apparent site-packages for a 10 MiB `df` delta
  and 0 hardlinked files. Where the filesystem cannot reflink (ext4, XFS
  formatted with `reflink=0`, a cache on a different filesystem) uv falls back
  to a plain copy; the install still succeeds and is still seconds rather than
  a minute, it just does not share disk. `--link-mode hardlink` was the first
  draft of this RFC and is rejected: it shares the inode itself, so one
  in-place write would land in every venv and in the cache, and the write
  fence that draft added could cover the agent file-edit tool but not a shell
  write (see Alternatives).
- `--project <checkout>`, so `--group dev` reads the worktree's
  `pyproject.toml` regardless of the caller's working directory. The Dev Fleet
  backend and a login shell provision from different directories; without the
  flag uv errors out looking for a `pyproject.toml` in the cwd.
- `--seed` on `uv venv`, so the venv carries `pip`. uv omits it by default and
  nothing in provisioning needs it, but `make backend` drives `$(VENV)/bin/pip`
  and contributors run `.venv/bin/pip …` by hand; a pod-provisioned worktree
  must not differ from a pip-built one in that respect.

If the switch is off, uv cannot be located, or either uv step exits nonzero,
the existing pip path runs unchanged over whatever is in `.venv`, including its
own pip-too-old fallback. The fallback deletes nothing: `python -m venv` takes
over a half-built directory the same way it already does after an interrupted
pip run (verified: a uv-created venv is re-initialised in place and pip installs
into it), and provisioning has no lock, so a deletion here could remove a venv
that a concurrent Dev Fleet or CLI provision had just finished. Nothing about
the resulting venv's layout changes, so `has_venv`, the pod runtime and the
Dev Fleet view are untouched.

Measured on the same host: about 10 s instead of ~60 s, and ~10 MiB instead
of ~400 MB of unique disk per additional worktree.

**Exit criteria.** `KIROCREW_PROVISION_USE_UV=1 kirocrew pod provision <wt>`
on a host with the wheel produces a venv `has_venv` accepts, with `pip` inside
it, and on a reflink filesystem a `df` delta far below the venv's apparent
size; unset, the command is byte-for-byte today's pip path.

### Phase 2 — the default flip (the decision this document proposes)

`pod/provision.py::_find_uv` (the pod-side wrapper that applies the switch to
`resolve_uv`'s answer) returns the resolved `uv` unless the switch is *off*
(`KIROCREW_PROVISION_USE_UV=0`, or a renamed `KIROCREW_PROVISION_PIP_ONLY=1`;
the PR picks one and documents it). Everything else in Phase 1 is unchanged:
the fallback and the flags. This is the product-shape change: a fresh
worktree provisioned by `pod up` on a stock install gets uv and copy-on-write
clones without asking.

Entry conditions, in order:

1. This document is merged with a non-`draft` status (so the First Principles
   lane reads the decision from the base branch).
2. Phase 1 has been on main long enough for the opt-in to have been used on at
   least one host other than the author's, and for the pip fallback to have
   been exercised (an opted-in, wheel-less install provisioning a pod).
3. A provisioning log line names the link mode uv actually used (clone or
   copy), so a host that silently lost the disk saving can be told from one
   that has it.

**Exit criteria.** A fresh `pod up` on a stock install builds the venv with uv
(provisioning output names the path taken); `KIROCREW_PROVISION_USE_UV=0`
restores the pip path; the Phase 1 tests are re-pinned to the new default and
the old opt-in test becomes the opt-out test.

### Phase 3 — `make backend`

The `backend` Makefile target has the same shape (interpreter gate, `python -m
venv`, three pip installs) and would take the same uv-first branch with the
same switch. It is a separate PR because the target also carries the
`--prefer-binary` and macOS re-sign steps, and because a contributor's primary
checkout is one venv, not twenty; the win is smaller and the blast radius is
every contributor.

### Phase 4 — Node: deduplication, not a package-manager switch

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
drops. Unlike the Python side this does share inodes, so a hand edit inside one
`node_modules` (rare, but `patch-package` and debugging prints exist) lands in
every sibling. On a reflink filesystem the same pass can be run copy-on-write
instead (`duperemove -dr --dedupe-options=same <parent>/*/website/node_modules`
on XFS or btrfs), which shares extents without sharing inodes; the RFC prefers
that form where it is available and keeps `hardlink -t` as the fallback for
ext4, documented with the hazard. A `pod` verb would run the pass over the pod
root after each provision.

## Alternatives considered

**pnpm.** The right tool for this problem in the abstract: a content-addressed
store with hardlinked `node_modules`, which shares inodes the way this RFC's
rejected hardlink draft did, so it does not escape that hazard either.
Rejected for the Node side for now because the repo's lockfile is
`website/package-lock.json` and CI runs `npm ci`. A per-worktree `pnpm import`
produces a second, untracked lockfile that drifts from the real one, and pnpm's
strict `node_modules` layout breaks packages that rely on hoisted phantom
dependencies — a class of failure that would surface only on the developer's
machine. Switching the repo to pnpm is a legitimate proposal, but it is a CI,
release and desktop-packaging change, and it should be made on its own evidence
rather than smuggled in under a disk-space fix.

**`npm install --install-strategy=linked`.** npm's own store-and-link mode.
Still marked experimental by npm, and its hoisting differs from `npm ci`'s in
the same way pnpm's does. Same objection, weaker tool.

**A shared `node_modules` symlinked per lockfile hash.** The repo's
`.gitignore` already notes that a worktree's `node_modules` "is often a
SYMLINK to a sibling checkout's", so the practice exists informally. It needs a
lock around concurrent installs, breaks when `npm ci` follows the symlink, and
saves less than hardlink deduplication because it cannot share across lockfile
versions. Deduplication gets the saving without a new install protocol.

**`--link-mode hardlink`.** The first draft of this RFC and of #10259. It
gives the same disk saving on every filesystem, including ext4, but it shares
the inode: an in-place write to an installed file under one venv mutates the
uv cache and every sibling venv at once, and the cache stays poisoned until
that package version is evicted. The draft fenced the agent file-edit tool
against writes to a `site-packages` file with `st_nlink > 1`; review showed the
fence could not cover a shell write, that its link-count `stat` was not in fact
bounded by the resolver pool, and that it pushed the security package over its
line budget. Rejected: clone removes the hazard structurally instead of fencing
it, at the cost of the disk saving on filesystems without reflink. The earlier
claim that clone degraded to a full copy on the motivating host was wrong; it
was read off `du`, which cannot see shared extents.

**`uv sync` with a committed `uv.lock`.** Would give reproducible resolution
on top of the shared cache. Out of scope here (see Non-goals); nothing in
Phase 1 prevents it later, and `uv pip install` and `uv sync` share the cache.

**Do nothing, document cleanup.** The gitignored caches (`temp-screenshots/`,
`__pycache__`, `.mypy_cache`) are ~30 % of a worktree and are worth pruning,
but they are not the dependency copies, and pruning them does not change what
the next `pod up` costs.

## Risks

- **Copy-on-write is only as good as the filesystem.** On ext4 and on XFS
  formatted without reflink, uv copies and the venv costs what pip's does; the
  host keeps the speed and loses the saving with no error. Mitigation: the
  Phase 2 entry condition that provisioning names the link mode it got, and
  the worktree skill's note on which filesystems reflink. Editing a cloned file
  in place is safe: the write copies the extent, so neither the cache nor a
  sibling venv changes, which is why this RFC carries no write fence and no
  `RECORD` verifier.
- **uv resolves independently of pip.** Both resolve fresh from the same
  `pyproject.toml` with no lockfile, so neither is more "CI-parity" than the
  other; but a resolver difference on a loosely pinned dependency would show
  up as a pod-only test result. Mitigation: the switch, and the fact that the
  pod venv is already not the CI venv (CI installs on a clean runner).
- **Cache eviction under a running pod.** `uv cache clean` unlinks the cache's
  copy of a file; cloned venvs keep their own extents. Not a correctness risk.
- **Different filesystems.** uv warns and copies. The install works; the host
  just does not get the saving. Worth a line in the provisioning output, which
  uv already prints.
- **Windows.** `resolve_uv` resolves `uv.exe`; ReFS supports block cloning and
  NTFS does not, so uv copies there. Pods are Linux-only, so this path is exercised on
  Windows only through `has_venv`, which is unchanged.

## Rollout

1. This document, on its own PR.
2. Phase 1 ([#10259](https://github.com/kirodotdev/KiroCrew/pull/10259)): the
   opt-in, tests, the dev-fleet spec update, and the manual uv form
   of the install in the worktree skill. Independent of 1; whichever merges
   second updates this document's index row.
3. Phase 2 once its entry conditions hold, rebased onto 1.
4. Phase 3 after Phase 2 has been on main long enough for the pip fallback to
   have been exercised in the wild.
5. Phase 4 as a documented command immediately; as a `pod` verb only if people
   run it by hand more than once.

## Open questions

- Whether the repo wants a Python lockfile at all is a separate RFC.
- Whether the link-mode log line (Phase 2 entry condition 3) should also be a
  `kirocrew doctor` check, so a host that lost the disk saving to a non-reflink
  filesystem is told outside provisioning output.

---
title: Shared Dependency Cache for Worktrees
status: accepted
kind: change
author: Pearce Kieser (Pearcekieser)
created: 2026-09-11
last-audited: 2026-09-25
audited-at: 8a2d0ab93
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
  has it; an install repackaged without the binary gets exactly today's
  behaviour.
- One explicit switch. While the shared path is opt-in, the switch turns it on;
  once it is the default, the same switch turns it off.
- The blast radius of the sharing is fenced where an agent would hit it.
- The node side is decided on evidence, not left implicit.

## Non-goals

- Changing what is installed. The venv still holds the editable package plus
  the PEP 735 `dev` group, resolved from the worktree's `pyproject.toml`.
- Adding a lockfile for Python (`uv.lock`). The repo resolves fresh from
  `pyproject.toml` today and CI does too; a lockfile is a separate decision
  about reproducibility, not about disk.
- Changing the repo's Node package manager or lockfile. See Alternatives.

## Design

### Phase 1 — `uv` for pod provisioning, opt-in ([#10259](https://github.com/kirodotdev/KiroCrew/pull/10259))

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

If the switch is off, uv cannot be located, or either uv step exits nonzero,
the existing pip path runs unchanged over whatever is in `.venv`, including its
own pip-too-old fallback. The fallback deletes nothing: `python -m venv` takes
over a half-built directory the same way it already does after an interrupted
pip run (verified: a uv-created venv is re-initialised in place and pip installs
into it), and provisioning has no lock, so a deletion here could remove a venv
that a concurrent Dev Fleet or CLI provision had just finished. Nothing about
the resulting venv's layout changes, so `has_venv`, the pod runtime and the
Dev Fleet view are untouched.

Phase 1 also ships the prevention half of the hardlink mitigation (see Risks):
the agent file-edit gate refuses in-place writes to a `site-packages` file whose
inode is shared. This is in the same PR as the opt-in because the two must never
be on main apart.

Measured on the same host: 9 s instead of ~60 s, ~1 MB instead of ~400 MB per
additional worktree.

**Exit criteria.** `KIROCREW_PROVISION_USE_UV=1 kirocrew pod provision <wt>`
on a host with the wheel produces a venv `has_venv` accepts, with `pip` inside
it, and `st_nlink > 1` on installed files; unset, the command is byte-for-byte
today's pip path. An agent file-edit to a shared `site-packages` file is
refused; the same edit to a pip-built venv is not.

### Phase 2 — the default flip (the decision this document records)

`_find_uv` returns the resolved `uv` unless the switch is *off*
(`KIROCREW_PROVISION_USE_UV=0`, or a renamed `KIROCREW_PROVISION_PIP_ONLY=1`;
the PR picks one and documents it). Everything else in Phase 1 is unchanged:
the fallback, the flags, the fence. This is the product-shape change: a fresh
worktree provisioned by `pod up` on a stock install gets uv and hardlinks
without asking.

Entry conditions, in order:

1. This document is merged with a non-`draft` status (so the First Principles
   lane reads the decision from the base branch).
2. Phase 1 has been on main long enough for the opt-in to have been used on at
   least one host other than the author's, and for the pip fallback to have
   been exercised (an opted-in, wheel-less install provisioning a pod).
3. The detection half of the mitigation (Risks → Detection) exists, or a
   maintainer has recorded that the flip may precede it.

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

### Phase 4 — Node: hardlink deduplication, not a package-manager switch

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
store with hardlinked `node_modules` — the same inode-sharing this RFC adopts
for Python, so it is an instance of the design, not an escape from its hazard.
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

**`--link-mode clone` as the default.** Copy-on-write reflinks give the saving
with independent inodes, which would remove the hazard below outright. Measured
on the motivating host it degrades to a full copy (XFS without reflink; ext4 has
no reflinks at all), so the Linux hosts with twenty worktrees would get uv's
speed and none of the disk saving. APFS hosts would get both. Kept as a
documented per-host choice rather than the default; the RFC may be amended if
the contributor population turns out to be reflink-capable.

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
  adds is a *blast-radius* change for an honest mistake. Mitigations, in
  order of when they act:
  - **Prevention (ships with Phase 1).** The agent file-edit gate refuses
    writes to a file under `**/.venv/**/site-packages/**` whose inode has more
    than one link (`security.is_sensitive_write_path` →
    `_is_venv_site_packages` → `_shares_inode`). The link count is a valid
    *prevention* signal because the check runs before the write: a shared file
    still has `st_nlink > 1` at that moment. It is scoped to the hazard — a
    pip-built venv, a `--link-mode copy` venv, the per-venv editable `.pth`
    and a file that does not exist yet are not fenced — so the gate is no
    broader than the sharing. pip and uv install by unlink-and-replace and
    never pass through the edit gate, so provisioning is untouched. The stat
    runs on the same bounded resolver pool as symlink resolution, so it adds no
    unbounded filesystem call to the event loop. The gate covers the file-edit
    tool only; a shell `sed` is not fenced, which is the same boundary every
    other write-only-protected path has.
  - **Repair.** uv's cache is content-addressed by package version, so
    `uv cache clean <package>` followed by a re-provision repairs every affected
    venv at once; the switch exists for a worktree that must be patched by hand.
  - **Detection (follow-up; a Phase 2 entry condition).** Verify installed
    files against the sha256 recorded in each `*.dist-info/RECORD` from
    `kirocrew doctor`. Link count cannot *detect* after the fact — an in-place
    write keeps `st_nlink` high while the safe temp-plus-rename write is the
    one that drops it to 1 — so `RECORD` is the only reliable detector and
    turns "several pods are subtly wrong" into a named package and a
    `uv cache clean <package>`.

  This is the same contract pnpm, Nix and every other content-addressed store
  impose, so it is a known shape rather than a new one. The one place that hands
  a human or an agent the manual uv recipe — the bundled `kirocrew-worktree-dev`
  skill (CONTRIBUTING no longer spells out a venv recipe; it says `make build`)
  — carries the one-line "never edit site-packages in place" warning beside it,
  because the skill's reader is exactly the actor who would.
- **uv resolves independently of pip.** Both resolve fresh from the same
  `pyproject.toml` with no lockfile, so neither is more "CI-parity" than the
  other; but a resolver difference on a loosely pinned dependency would show
  up as a pod-only test result. Mitigation: the switch, and the fact that the
  pod venv is already not the CI venv (CI installs on a clean runner).
- **Cache eviction under a running pod.** `uv cache clean` unlinks the cache's
  copy of a file; hardlinked venvs keep theirs. Not a correctness risk.
- **Different filesystems.** uv warns and copies. The install works; the host
  just does not get the saving. Worth a line in the provisioning output, which
  uv already prints.
- **Windows.** `_find_uv` resolves `uv.exe`; hardlinks work on NTFS and
  `st_nlink` reports them. Pods are Linux-only, so this path is exercised on
  Windows only through `has_venv`, which is unchanged.

## Rollout

1. This document, on its own PR.
2. Phase 1 ([#10259](https://github.com/kirodotdev/KiroCrew/pull/10259)): the
   opt-in, the fence, tests, the dev-fleet spec update, and the manual uv form
   of the install in the worktree skill. Independent of 1; whichever merges
   second updates this document's index row.
3. Phase 2 once its entry conditions hold, rebased onto 1.
4. Phase 3 after Phase 2 has been on main long enough for the pip fallback to
   have been exercised in the wild.
5. Phase 4 as a documented command immediately; as a `pod` verb only if people
   run it by hand more than once.

## Open questions

- Whether the repo wants a Python lockfile at all is a separate RFC.
- Whether Phase 2 waits for Detection or may precede it (entry condition 3).

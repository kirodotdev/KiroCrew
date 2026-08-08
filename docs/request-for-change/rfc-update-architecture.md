---
title: Update Architecture (install-shape capability contract)
status: draft
author: zezhexu
created: 2026-07-31
last-audited: 2026-08-06
audited-at: 8861f89e
doc-pr: 1003
implementation-prs: [1734]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Update Architecture (install-shape capability contract)

- Status: draft — **Phase 1 is now partly implemented.** PR #1734 (merged,
  `8861f89e`) shipped the install-shape → behavior derivation for the *check* path
  and the SPA surfaces that read it. What that PR closed, and what it did not, is
  itemized under **Implementation status** below; the phase list further down has
  been amended to match. Adjacent in-flight under a **different** design: PR #999
  (`feat/emergency-release-controls`, open) adds a feed-served minimum version +
  mandatory-update modal for the desktop lane only, without the capability
  contract.
- Correction to the reference below: KiroCrew ships **five** distribution shapes, not the set implied — `beacon.py:155` lists `{dmg, appimage, wheel, source, docker}`.
- Author: zezhexu
- Created: 2026-07-31
- Related: `docs/build/release.md` (channels, release branches, promotion),
  `docs/request-for-change/version-compliance-framework.md` (the policy ceiling
  this RFC must honor)

## Implementation status (as of `8861f89e`)

**Landed in PR #1734** — the tactical half of Phase 1, driven by a user-visible
defect rather than the architecture: the dashboard told wheel installs "you're on
the latest version" while they were two releases behind, because the check was
git-only and never ran (its cache stayed at the initial `available: False`, and
the SPA rendered success on any HTTP 200). Independently, `_version_tuple` raised
on `int("2rc3")` and fell back to `(0,)`, collapsing every prerelease to one key
so no rc-to-rc step was detectable.

- Install shape is derived **once, in the backend**, from `beacon.distribution()`
  — so the claim above that the stamp "is read only by telemetry" is **no longer
  true**: `dashboard/handlers/updates.py` is now a second, non-telemetry caller.
- Shapes are matched **by exclusion**, not by an `== "wheel"` allowlist: wheels
  published before `_build_info` existed carry no stamp and report the `source`
  default, so an allowlist would have skipped every already-released CLI install
  — precisely the population the fix was for.
- `dmg` / `appimage` / `docker` **defer**: they report which surface owns the
  upgrade instead of reading the CLI feed. This is load-bearing, not tidiness —
  the desktop bundles embed this backend (a PBS tree inside the .app / AppImage),
  so they execute this code, and reading the CLI feed there would compare against
  the wrong release stream and then light the Settings nav dot
  (`status.update_available || desktopUpdateAvailable`) pointing at a desktop
  About panel that reports "up to date".
- The SPA reads capability, not shape: `self_updatable` suppresses the in-app
  Update button unconditionally where `POST /api/update` would 409, and the
  installer command is surfaced instead.
- An **honesty contract** the original draft did not specify (see the amended
  §2): a check that could not run is distinguishable from a check that found
  nothing.
- The **Electron OTA engine, its feeds, and its consent flow are untouched** —
  no file under `website/electron/` or `packaging/` is in that commit, and the
  desktop branch of `AboutPanel.tsx` is byte-identical to its parent.

**Still open from Phase 1** — the architectural half:

- `platform/update_capability.py` does not exist; the fields shipped are the
  tactical set (`install_kind` / `self_updatable` / `checked` / `error` /
  `update_command`), not the contract vocabulary in §2.
- Boot-time git auto-apply is **still armed** for a `mainline` (and, via the
  detached-HEAD coercion, a detached) checkout. #1734 only added a guard so a
  non-git install notifies instead of driving `git reset` in a tree with no
  `.git`.
- The `.git` derivation is **still done in three places** —
  `updates.py:383`, `updates.py:871`, and `cli_server.py:1173`. They now agree
  (all three use an `exists()` check; see the correction under *Problems*), so
  this is a drift risk and a "not git's own answer" problem rather than the
  semantic split the original draft described.
- The `auto_update` retirement surfaces named under Migration are untouched.


## Summary

KiroCrew ships in five distribution shapes and has three disjoint update
mechanisms, one of which covers no shape a user is told to install. The
mechanisms themselves are legitimate — a notarized app bundle and a
pipx-managed wheel share nothing at the byte level, so any product shipping
both has at least two updaters. The defect is that **the decision about which
mechanism applies is made in the wrong layer**: the dashboard SPA guesses from
`isDesktop` (inconsistently), and the backend re-derives install shape ad hoc
by probing environment variables and filesystem state in three separate call
sites.

This RFC makes the backend authoritative and has it publish an **update
capability contract**. The SPA renders affordances from capabilities and never
learns which shape it is running in. Two engines stay (desktop OTA, a new wheel
updater); the automatic git self-update is retired; the drain-and-restart
sequence becomes shared and explicit.

## Motivation

### Current state

Five shapes, enumerated at `src/kiro_crew/beacon.py:136`:

```python
KNOWN_DISTRIBUTIONS = frozenset({"dmg", "appimage", "wheel", "source", "docker"})
```

Each packaging path stamps that value at build time into a generated
`kiro_crew/_build_info.py` (via `scripts/stamp-distribution.sh`), which
`beacon.distribution()` prefers over the `KIROCREW_DISTRIBUTION` env var: a
baked module ships with the artifact and a running install cannot change it,
whereas the env var is inherited by child processes and settable by anyone with
a shell. Windows (NSIS) has no value in the set and reports `source`. The
field is read **only by telemetry**; no update code consults it.

Three mechanisms:

| Mechanism | Where | Covers |
|---|---|---|
| git self-update | `slack/gateway.py:4959` (`_check_for_updates`, called once from startup at `:5426`) → `_auto_apply_update` (`:5004`) | `source` only |
| Electron OTA | `website/electron/auto-update.js` (electron-updater, `autoDownload=false`, `autoInstallOnAppQuit=false`) | `dmg`, `appimage` |
| — none — | | `wheel`, `docker` |

All three backend entry points to the git path guard on roughly the same two
conditions — `KIROCREW_PROJECT_DIR` set, and a `.git` present — and, as of
`8861f89e`, with the **same** semantics:

- `dashboard/handlers/updates.py:383` (`_do_update_check`) — `os.path.exists`
- `dashboard/handlers/updates.py:871` (`api_update_apply`) — `os.path.exists`
- `cli_server.py:1173` (the CLI update command) — `(proj_path / ".git").exists()`

**Correction to the original draft.** This section previously said the third site
used `Path.is_dir()`, and drew the conclusion that "the HTTP paths accept a linked
worktree that the CLI rejects." That is **false against the tree**: all three use
an `exists()` check, and `cli_server.py` carries an explicit comment saying it
accepts both forms precisely so a linked worktree is not wrongly refused. There is
no exists-vs-is_dir divergence, and no contributor-visible behavior gap between
the paths.

What survives the correction is a weaker but still real problem, and it is the one
Phase 1b should be built against:

- **Three call sites, one fact.** They agree *today* by coincidence of three
  independent edits, with nothing enforcing that they keep agreeing — the drift
  risk is structural even while the values match.
- **`exists()` is not git's answer.** It accepts any `.git` entry, including a
  stray file or directory that is not a gitlink, and it is blind to whether the
  directory is actually inside a working tree.
- **None of the three consults the build-time value**, which is the fact the rest
  of this RFC treats as authoritative.

So a collapse to one derivation is still warranted; it just is not a
tie-break between two live semantics. Open Question 5 picks the semantic.

### How it got split

Not a series of wrong calls — a series of correct local calls under a shifting
premise.

- **2026-06-02** (`64e47961`, *"de-Amazoned public OSS fork"*) — the git
  self-update and the `auto_update` config key arrive **in the first public
  commit**, inherited from the project's pre-fork ancestor, an internal tool
  whose only distribution shape was a git clone. For that shape, fetching and
  re-execing on restart is the correct mechanism.
- **2026-06-20** (`4b3a7e57`) — Electron desktop auto-update lands. A packaged
  app cannot pull itself; a second, necessarily disjoint mechanism is right.
  It is added *beside* the git path, which still served every non-desktop user.
- **2026-07-18** (`30a3d9e9`, #24) — `cli.sh` adds the pipx wheel install, and
  the README promotes it to the headline install. It shipped with no updater.

Subsequent commits touching `_auto_apply_update` (#694 source pinning + minimum
version, the CPP seams) progressively *hardened* the inherited path. None
re-asked which shapes it still governs.

### Problems

1. **The headline install cannot update.** `cli.sh` (README's first
   instruction) produces a `wheel` install. `kirocrew update` exits 1 on it —
   `❌ KIROCREW_PROJECT_DIR not set — cannot locate source tree`
   (`cli_server.py:1145`) or `❌ No git repo at …` (`:1151`). The documented
   update command does not work for the documented install method. The only
   route is re-running the installer.

2. **The shared SPA renders impossible actions.** One React app is served to
   all shapes and cannot tell them apart. `AboutPanel.tsx:491` branches on
   `isDesktop`; `SettingsPage.tsx:92` couples differently — it selects the
   desktop-only redux field `desktopUpdateAvailable`, mirrored from the Electron
   updater, so on a wheel install its update nudge simply never lights up. The
   changelog modal at `App.tsx:1709` does neither. That modal fires on the first
   launch after a version change — i.e. immediately after an OTA install — and
   renders:
   - an **inert** "Auto-update on restart" toggle (`App.tsx:1739`) writing
     `auto_update`, which nothing on a packaged install reads; and
   - an **Update now** button whenever `updateAvailable` is true, where
     `updateAvailable` includes `desktopUpdateAvailable` (`App.tsx:728`,
     mirrored from the Electron updater). It POSTs to the git-only
     `/api/update`, which answers 400 or `409 Not a git checkout — update by
     redeploying (e.g. \`kirocrew cloud launch\`)`. A `.dmg` user is told to run
     a cloud launcher.

   Three surfaces, three different couplings to the same unstated fact.

   **Partly closed by #1734.** `AboutPanel.tsx` no longer branches on
   `isDesktop` for the check/notify path: it reads capability, suppresses the
   in-app Update button where `POST /api/update` would 409, and surfaces the
   installer command instead. The other two surfaces are **unchanged** —
   `SettingsPage.tsx` still selects the desktop-only `desktopUpdateAvailable`
   (so its nudge still never lights on a wheel install), and the `App.tsx`
   changelog modal still has no capability check at all. Phase 1b converts both.
   Note also that #1734 fixed the *check*, not the *apply*: Problem 1 stands in
   full.

3. **A daemon rewrites its own source tree at boot, unattended.**
   `_auto_apply_update` hard-resets the tree it runs from, reinstalls, and
   re-execs — with no user action, as a side effect of starting. Its blast
   radius is narrower than it first looks, and the narrowing is worth stating
   precisely: `gateway.py:5039-5041` returns early unless the branch is
   `mainline`, so a checkout on a feature branch is **not** armed. But
   `gateway.py:5035-5036` coerces a detached HEAD to `"mainline"`, so a detached
   checkout **is** armed — and nothing about being on a detached HEAD suggests
   "treat me as the release branch". `available` additionally requires
   `remote_version > local_version` on the branch's own upstream.

   So the defect is not "it will rewrite any checkout"; it is that a daemon
   performs an unattended tree rewrite plus reinstall plus re-exec at all, on
   `mainline` and on detached HEAD, gated only by guards that were added
   incidentally rather than designed as a safety boundary.

4. **Every future surface re-inherits the bug.** With no capability contract,
   each new update affordance must independently rediscover which shapes it
   applies to. Problem 2 is the third instance of the same class.

## Goals

- Exactly one derivation of install shape → update capability, in the backend.
- The SPA renders from **capabilities**, never from shape. No `isDesktop`
  branching in update surfaces.
- Every shape has a defined, working update story, including "there isn't one,
  here is what to do instead".
- The policy ceiling (minimum version, pinned source) is expressed in the
  contract so the UI can explain a mandatory update rather than merely present
  a button.
- One shared drain-and-restart sequence, with success defined by a health +
  version handshake rather than a clean exit code.
- No regression to the desktop OTA engine, whose consent-first posture is load
  bearing for signing and notarization.

## Non-goals

- Rewriting or replacing electron-updater.
- Background/silent auto-update for the CLI (explicitly rejected — see §4).
- Rollback. The release model is roll-forward only; this RFC does not change
  that.
- Removing the git code path in this change. It is de-armed and reported as
  unavailable; deletion is a later cleanup.
- Delta/binary-diff updates, or a bundled package manager.

## Design

### §1 The organizing rule

> **Capability varies by install shape. Consent varies by channel and policy.**

Whether an update *can* be applied in-process is a property of how the software
was installed. Whether it *should* be applied without asking is a property of
which channel the user opted into and what policy is in force. Conflating these
two is the reason the current UI is wrong: it asks a shape question
(`isDesktop`) to answer a consent question (show a toggle?).

### §2 The update capability contract

`KIROCREW_DISTRIBUTION` is promoted from a telemetry-only stamp to a
first-class runtime property, and one module — `platform/update_capability.py`
— derives the contract from it. Served on the status payload and on
`GET /api/update/check`:

```json
{
  "supported": true,
  "managed_by": "electron | kirocrew | git | container | none",
  "mode": "auto | consent | notify | none",
  "can_download": true,
  "can_apply": true,
  "requires_restart": true,
  "channel": "nightly | insider | stable",
  "current_version": "0.1.2",
  "latest_version": "0.1.3",
  "check_status": "unchecked | checking | succeeded | failed | deferred",
  "update_available": null,
  "error_code": null,
  "minimum_version_enforced": null,
  "unavailable_reason": null,
  "remediation": null,
  "state": "idle | available | downloading | ready | draining | restarting",
  "progress": null
}
```

### §2.1 The honesty pair: `check_status` + a nullable `update_available`

This pair is not decoration and it is not derivable from the rest of the
contract. The original draft implied a verdict was always available; PR #1734 shipped
against the bug that assumption produces, and the shape it arrived at belongs
here rather than in the implementation.

- **`update_available` is nullable, and `null` is not `false`.** "Up to date"
  means `check_status == "succeeded" && update_available == false`. Any consumer
  that treats a missing verdict as "current" reproduces the original defect: a
  check that never ran, rendered as a check that passed.
- **`check_status` is the single source of that distinction.** `unchecked` (no
  check has run this process), `checking` (in flight), `succeeded` (a real
  comparison completed — `update_available` is now authoritative), `failed`
  (something prevented a comparison; see `error_code`), `deferred` (this shape's
  updates are owned elsewhere; see `unavailable_reason`).
- **`deferred` is not `failed`.** A `dmg` reporting "the app updates itself" has
  not malfunctioned, and rendering it as an error is a different lie from the one
  this RFC set out to fix. #1734 initially routed both through one `error` field
  and had to add a frontend set of "info codes" to stop them rendering as
  failures — evidence that the two states want separate slots. `electron` and
  `container` shapes therefore report `check_status: "deferred"` +
  `unavailable_reason: managed_by_app | managed_by_image`, never `error_code`.
- **`error_code` is the machine-readable failure class**, distinct from
  `unavailable_reason` (which explains a *deferral* or a permanently unsupported
  shape). #1734's shipped set is the starting vocabulary: `feed_unreachable`,
  `feed_malformed`, `git_fetch_failed`, `git_read_failed`,
  `version_unparseable`, `unknown`. A consumer that does not recognise a code
  must still render "the check failed" — never fall through to success.

### §2.2 `remediation` is structured, not a string

```json
"remediation": { "kind": "command | image_pull | store | none",
                 "message": "…", "command": "…" }
```

`kind` lets the UI choose an affordance without parsing prose; `command` carries
the exact copyable line (#1734's `update_command`). Two constraints, both learned
the hard way in that PR:

- **The command is display/copy-only.** The check endpoint must never execute it,
  and it must be composed **locally** from already-validated inputs — never
  assembled from feed fields. The feed the check reads is unsigned display
  metadata (the signature check lives in `cli.sh`, which pins the key offline and
  is the only thing that installs bytes), so a URL taken from it would let a
  tampered feed choose what the user pastes into a shell.
- **It must pin its transport.** `--proto '=https'` is mandatory on any emitted
  `curl … | sh`: the artifact base is overridable (`KIROCREW_CDN_BASE`), so
  without it an `http://` override hands the user a command that fetches an
  installer in plaintext and executes it. This was a blocking review finding on
  #1734, and `cli.sh` already passes the flag on every fetch it makes itself —
  the emitted command must not be laxer than the thing it invokes.
- **The command must name the channel.** `cli.sh` defaults to `stable` and never
  reads back the channel file it writes, so a bare re-run silently moves an
  insider install onto the stable lane.

### §2.3 `state` and `progress` are Phase-2+ and must not ship early

Both describe an apply/drain lifecycle that does not exist yet. Serving
`state: "idle"` and `progress: null` from a backend with no state machine
advertises a guarantee the contract cannot keep — a consumer would reasonably
poll for transitions that never come. They stay specified here (the wheel engine
and the drain orchestrator need them) but are **additive fields introduced with
their implementation**, not placeholders shipped with Phase 1.

`progress` is `{ "percent": 0-100, "bytes_per_second": 0 } | null`, and it is
load-bearing rather than decorative: `AboutPanel.tsx` already renders
`<Progress value={cardPercent}>` plus a transfer-rate label, which today arrive
over the Electron IPC channel. Without `progress` in the contract, Phase 2 would
have to either keep that out-of-contract channel alive — the exact
shape-coupling this RFC exists to delete — or leave the wheel engine's bar
permanently indeterminate.

`can_apply` means **appliable by the running process without the user leaving
the app**. It is not "can this install ever be updated": `source` and `wheel`
can both be updated from a terminal, and `kirocrew update` genuinely applies on
`source` today (`cli_server.py:1140-1152`). `can_apply` is the field an
implementer reads to decide whether to render an in-app Apply button, so it must
answer only that question.

Derivation, by shape:

| Shape | `managed_by` | `can_apply` | `mode` | `remediation` |
|---|---|---|---|---|
| `dmg`, `appimage` | `electron` | true | `consent` | — |
| `wheel` | `kirocrew` | true (Phase 2) | `notify` | — (in-app after Phase 2) |
| `source` | `git` | false | `notify` | `kirocrew update` |
| `docker` | `container` | false | `notify` | pull a newer image tag |
| unavailable (dev build, translocated, read-only volume) | `none` | false | `none` | shape-specific string |

`unavailable_reason` / `remediation` exist so the UI never has to invent copy
for a state it cannot act on. The existing desktop `updatesDisabled` reasons
(`dev`, `translocated`, `volume`, `platform`) fold into these two fields rather
than remaining a frontend-only enum.

`minimum_version_enforced` is required, not optional: the policy ceiling
(`platform/update_governance.py`) can already force an update past a user's
opt-out. Without it in the contract, the UI can show that an update is
mandatory but not why.

The three consumers — `AboutPanel.tsx`, `SettingsPage.tsx`, and the
`App.tsx:1709` changelog modal — read only this contract. Each sheds a
*different* coupling: `AboutPanel.tsx:491` loses its `isDesktop` branch,
`SettingsPage.tsx:92` stops selecting `desktopUpdateAvailable` in favour of the
contract's `state` / `latest_version`, and the changelog modal gains the
capability check it never had.

### §3 One engine per shape

- **desktop** — electron-updater, unchanged. Owns signed artifact download and
  bundle swap.
- **wheel** — new. Resolve the channel feed, **verify the wheel's signed build
  provenance against a trust root pinned in the client** (not merely its
  checksum), then perform the pipx replacement **from an external helper
  process**. A daemon cannot safely overwrite the bytes it is executing, and the
  running gateway holds live sessions; the helper is what makes the swap
  survivable.

  **Managed-venv replacement mechanics (invariants, not a settled design).**
  `cli.sh` has two install branches, and only one of them is pipx. The other —
  the default when pipx is absent — is a fixed-path managed venv
  (`${KIROCREW_HOME}-venv`, `cli.sh:331`) upgraded **in place** today. For that
  shape the promising direction is *versioned trees with atomic promotion*:
  build `crew-venv-<version>` completely while the old gateway keeps serving,
  then promote a stable path to point at it, then restart. (Precedent:
  Claude Code's native installer keeps per-version binaries under
  `~/.local/share/claude/versions/` behind one symlink; Codex CLI instead
  detects the owning package manager and delegates.) A cross-vendor
  adversarial council reviewed a concrete version of this design
  (2026-08-06, GPT 5.6 / DeepSeek 3.2 / GLM 5 — REVISE / REJECT / REVISE)
  and reduced it to the following **invariants any Phase 2 implementation
  must satisfy**, with the mechanism itself left to the implementation PR:

  - **Promotion must be actually atomic.** `ln -sfn` is unlink + create — a
    missing-path window — and is not atomic on NFS at all. Atomic promotion is
    a sibling symlink replaced via `rename(2)` / `os.replace`.
  - **Every persisted launch path must resolve through the stable path.** At
    least four exist today: `KIROCREW_SERVICE_BIN`, the `kirocrew_bin()` value
    systemd renders into `ExecStart`, the generated macOS live-gateway
    launcher, and the non-service restart in `updates.py`, which re-execs
    `sys.executable` — the old version-specific interpreter. Fixing only the
    service unit resurrects the old tree on every other path.
  - **The old-inode guarantee holds only for versioned trees.** A Python
    process imports lazily; after a flip, not-yet-imported modules resolve from
    the new tree. "The running gateway is unaffected" is true only once the
    running gateway was itself started from an immutable versioned directory —
    which makes the **first migration** (moving off today's fixed real
    directory without breaking the live venv's absolute shebangs) a protocol
    of its own. That protocol is now defined:

    **First-migration protocol.** The existing `${KIROCREW_HOME}-venv` real
    directory is **never renamed, moved, or converted in place** — renaming it
    breaks its own absolute shebangs while a gateway may still be running from
    it, and a non-empty directory cannot be atomically replaced by a symlink
    anyway. Instead the stable path is a **new name** that has always been a
    symlink:

    1. The helper builds `crew-venv-<version>` fresh and verifies it
       (provenance per this section, hash-pinned dependencies, import check).
    2. It creates `crew-venv-current → crew-venv-<version>` atomically
       (sibling symlink + `rename(2)`; trivially safe because the name did not
       previously exist).
    3. The installer rewrites **all four persisted launch paths** (above) to
       resolve through `crew-venv-current`, re-rendering the service unit and
       the generated macOS launcher.
    4. Drain per §5, restart via the supervisor, then the post-restart
       health + version handshake.
    5. **On a failed handshake**, launch paths are pointed back at the old
       fixed directory — which still works, because it was never touched. This
       is a *fallback to a still-functional tree*, not a rollback protocol,
       and is consistent with rollback remaining a non-goal.
    6. The old fixed directory is pruned only after N consecutive verified
       boots (suggested N=3), and a pruning failure never fails an update.

    The load-bearing property: **no tree that a live process might be using is
    ever moved or deleted**, and the atomicity problem of replacing a real
    directory with a symlink is dodged entirely by putting the symlink at a
    fresh name. Subsequent updates are pure symlink flips on
    `crew-venv-current` and never revisit this protocol.
  - **A fresh venv re-resolves the dependency graph.** `setup.cfg` carries wide
    ranges; a rebuilt environment downloads packages covered by nobody's
    signature. The install step needs locked, hash-pinned constraints (or a
    wheelhouse) inside the verified payload, or the provenance story covers
    only the Kiro Crew wheel itself.
  - **Provenance must bind more than a digest.** The feed is unsigned; an
    actor who controls it can point at a *different* artifact with valid
    provenance from the same repo. Verification must check workflow, commit
    lineage and channel policy against the client-pinned root — and the SLSA
    requirement above is unconditional; no "or" fallbacks.
  - **pipx delegation is not a safety property.** `pipx install --force`
    mutates the fixed pipx environment in place while the old gateway is using
    it — the torn-runtime hazard this section exists to avoid. If Phase 2
    ships a pipx apply path at all, it drains first, accepts the downtime, and
    documents rollback as unsupported for that shape. Which branch owns a
    given install is also **not derivable at update time** (pipx presence now
    proves nothing about install time); the installer must persist the branch
    it took.
  - **Rollback stays a non-goal.** Retained old trees are *manual recovery
    targets*, nothing more; any pruning policy must never delete the tree the
    running process was started from, and cleanup failures must not fail the
    update.
  - **macOS TCC identity across path rotation is an open compatibility test,
    not a solved problem.** The console script's shebang names the rotating
    versioned interpreter, so a stable outer symlink may not preserve grants
    (Claude Code hit exactly this: anthropics/claude-code#76246, #77081,
    #80899).

  The checksum is necessary and not sufficient. `SHA256SUMS` is served from the
  same CDN as the wheel, so an actor who can replace one can replace both —
  `publish-cli.yml:85-87` says this in as many words ("integrity, not
  authenticity"). The publish lane **already** emits the missing half: a signed
  SLSA attestation binding the wheel's digest to the repo, workflow and commit
  (`actions/attest-build-provenance`, `publish-cli.yml:84-91`). Nothing consumes
  it yet. A self-updater is a higher-value target than a one-time installer — it
  runs unattended, forever — so the wheel engine must be the first consumer,
  with the verification key pinned in the client rather than fetched from the
  channel it is meant to police.
- **source** — explicit `kirocrew update` only. The boot-time automatic apply is
  removed.
- **docker** — no self-update. The contract says so and names image pull.

What is shared across engines is not installation code: it is policy
evaluation, discovery, consent, the drain sequence, restart orchestration, and
post-restart verification.

### §4 Consent model

| | nightly | insider | stable |
|---|---|---|---|
| desktop | opt-in background staging, apply on idle/quit | consent-first | consent-first |
| wheel | notify + explicit apply (in-app button or `kirocrew update`) | same | same |
| source | notify only | same | same |

**"Explicit" means a deliberate user action, not necessarily a terminal.** An
in-app Apply button and `kirocrew update` in a terminal both qualify, and since
the backend can invoke the install helper it can serve both — which is why
`wheel` carries `can_apply: true` after Phase 2. They are **not equivalent in
authority**, however: the in-app path additionally requires the host-local
step-up resolved in Open Question 7, because a dashboard session is weaker
authority than a shell on the host. What is ruled out for the CLI shape is the
*silent* path: no background download-and-swap, no apply without the user asking
for it in one of those two places.

Policy overrides all three columns: a minimum-version pin forces an update past
a user's opt-out. **The deadline and the user-facing message are new
requirements, not existing behavior** — today `gateway.py:4979-4986` logs a
warning and calls `_auto_apply_update()` immediately, with no grace period and
nothing shown to the user. Preserving the *override* while adding the *deadline
and messaging* is Phase 3 work, sequenced with the drain orchestrator that has
to enforce the grace period.

**The CLI does not silently self-update.** The precedent set is consistent —
`gh`, `rustup self update`, `uv self update`, `deno upgrade` all self-update as
an *explicit user action*. The notable counterexample is Claude Code's native
installer, which does update automatically; the reason not to follow it here is
that a `wheel` install is a managed CLI **plus a long-lived daemon holding live
agent sessions**, not a self-contained app. Replacing its bytes unasked is
more surprising than it is convenient.

### §5 Drain-then-swap

The gateway holds live agent sessions, scheduled crons, and background
subagents. Applying an update is a process-lifecycle event, and the sequence is
the same for both engines:

1. **Stage and verify** the artifact — read-only, safe at any time.
2. **Take an update lease** — one update in flight, ever. This must be a
   **filesystem lease that outlives the gateway process** and is readable by
   whatever supervises it, not an in-process flag.
3. **Stop accepting new turns.** Discovery and download must never do this;
   only apply.
4. **Checkpoint** session state, cron state, subagent metadata.
5. **Wait** for in-flight work to finish, or hit a deadline. Mandatory
   (policy-forced) updates may interrupt, but only *after* checkpointing and
   warning.
6. **Stop the gateway.**
7. **Install from outside the process** — helper (wheel) or Squirrel/AppImage
   (desktop).
8. **Relaunch** via launchd / systemd / Electron.
9. **Verify**: require a health + version handshake before reporting success.

Step 9 is the one most easily skipped and most expensive to omit — without it,
"update succeeded" means "we started something", not "the new version is
serving".

**The lease must cover steps 3–8, not 3–6**, and that is the difference between
this design and what already exists. The in-tree analogue is `installingUpdate`
(`website/electron/main.js:223`, read by the liveness monitor at `:1382`) — a
process-local boolean, which is sufficient on desktop only because Electron
itself survives the swap and can keep holding it. The wheel engine has no such
survivor: its supervisor is launchd or systemd, the gateway is gone during
step 7, and an external supervisor cannot read another process's in-memory flag.
Generalizing §5 with a process-local lock therefore reintroduces, on the wheel
path, precisely the respawn-during-install race the desktop path already hit and
fixed. Hence a lease on disk, with the supervisor unit taught to honor it. This
is the fragile seam and it gets explicit tests.

## Phases

**Phase 1 — the contract.** Split by #1734 into a landed half and a remaining
half; the split was not planned, but it is a reasonable seam and worth recording
as one.

- **Phase 1a — landed (#1734).** Backend-authoritative shape derivation for the
  *check* path; deferral for `electron` / `container` shapes; the honesty pair
  (§2.1); a PEP 440 comparator; and the SPA surfaces reading capability instead
  of `isDesktop`. Field names are tactical, not §2's vocabulary — see
  *Vocabulary migration* below.
- **Phase 1b — remaining.** Add `platform/update_capability.py` and serve the §2
  contract; collapse the three ad-hoc `.git` derivations into it (Open Question
  5); convert the two SPA surfaces 1a did **not** touch — `SettingsPage.tsx`
  (still selecting the desktop-only `desktopUpdateAvailable`) and the `App.tsx`
  changelog modal (still no capability check at all); de-arm the boot-time git
  apply; retire the three `auto_update` surfaces named under Migration.
  **Problem 2 is only PARTLY closed by 1a** — `AboutPanel.tsx` reads capability,
  the other two surfaces do not, so the third-instance-of-the-same-class defect
  survives until those conversions land. Problem 3 and Problem 4 need 1b outright.

Sequencing rationale is unchanged: this is still the prerequisite for the others
— shipping a wheel updater first would deliver it into surfaces that misreport
what is possible.

**Vocabulary migration (Phase 1b).** #1734 shipped `install_kind` /
`self_updatable` / `checked` / `error` / `update_command`; §2 specifies
`managed_by` / `can_apply` / `check_status` / `error_code` /
`remediation.command`. Phase 1b renames rather than aliases, and does so in one
commit with its consumers, because **the only consumer is the SPA that ships in
the same artifact as the backend** — every install shape bundles a version-locked
pair, so there is no third-party integration to deprecate against and no skew to
support. (The one exception is a dashboard driving a *remote* gateway over the
Instances tunnel, where an older SPA can meet a newer backend. That path must
tolerate unknown/missing fields, which the honesty rules already require: a
missing `check_status` reads as `unchecked`, never as "current".)

**Phase 2 — wheel updater.** A wheel apply path: feed resolution, **provenance
verification** (§3), external-helper pipx replacement, and a gateway drain
request when one is running. Reachable two ways from the same backend entry
point — `kirocrew update` in a terminal, and an in-app Apply button — which is
what flips `can_apply` true for `wheel` (§4). Introduces `state` and the
download half of `progress` (§2.3).

**Phase 3 — shared drain-and-restart handshake.** Extract §5 into one
orchestrator used by both engines, with the on-disk update lease honored by the
watchdog, the quit path, **and the supervisor unit**; the post-restart
verification handshake; and the policy-forced-update deadline + user messaging
that §4 identifies as new. Completes the `state` machine.

De-arming the git boot-apply (Problem 3) lands in Phase **1b** with the contract,
since the contract is what makes `source` report `can_apply: false`.


## Migration and compatibility

`auto_update` in `config.json` stays readable and is **demoted from a mechanism
to a legacy key**. After Phase 1 it governs nothing: the contract reports
`mode: "notify"` for `source`, and boot-time apply is gone. Existing configs do
not need rewriting; a future release may drop the field.

Three live surfaces currently offer or persist that key, and **all three** must
go in the same phase, or Phase 1 ships a switch over a key that governs nothing:

- the raw-config toggle in `KiroCrewCfgTab.tsx:271`;
- the AboutPanel toggle (`AboutPanel.tsx:342,364,377,549-550`);
- `POST /api/update/auto` (`dashboard/handlers/updates.py:218-234`), which
  writes it into `config.json`.

The endpoint stays routed but becomes a no-op returning the contract's `mode`,
so an older cached SPA cannot resurrect the setting.

No other API is removed. `POST /api/update` keeps its current 400/409 responses
for non-git shapes, but the UI stops calling it on those shapes because the
contract tells it not to.

## Security considerations

- **Authenticity, not just integrity.** The wheel engine must verify the signed
  build provenance already published by `publish-cli.yml:84-91` against a
  client-pinned trust root, in addition to the `SHA256SUMS` digest. Checksums
  alone authenticate nothing when the sums file ships from the same origin as
  the artifact (§3).
- **Source pinning is engine-specific.** The existing helpers
  (`update_governance.resolve_remote_url` / `update_blocked_reason`) are
  **git-shaped and must stay scoped to the git engine**: `resolve_remote_url`
  runs `git ls-remote --get-url` and returns `""` for a tree with no git remote,
  and `""` under a non-empty pin is documented as *deny*
  (`update_governance.py:43-44`, `governance.py:permits_source`). Applying them
  to the wheel and desktop engines would therefore refuse **every** update on
  any fleet that has configured a pin. Each engine needs its own artifact-source
  predicate — the channel feed / artifact origin URL for wheel and desktop — and
  the pin must be evaluated before any path offers or stages a newer version.
- **The helper is not a boundary against a compromised gateway, and this RFC
  does not pretend otherwise.** Both candidate locations (the pipx-managed
  package, `KIROCREW_HOME`) are writable by the OS user the gateway runs as, so
  an actor who can already write as that user can replace the helper with one
  that skips verification — the helper verifying provenance *itself* is
  circular, because the attacker replaces the verifier. What the helper and the
  provenance check together **do** defend against is the adversary this design
  is actually about: a compromised or spoofed **artifact origin**, reached over
  the network. Against local code execution as the user they buy nothing, and
  nothing short of a system-installed, root-owned helper would. Whether that is
  worth building is Open Question 1; until it is answered, the local-execution
  case is an accepted, stated gap rather than a covered one.
- Desktop consent-first behavior is unchanged. Nothing in this RFC introduces a
  path that installs a signed bundle without an explicit user action, outside
  the existing policy-mandated case.

## Alternatives considered

**Keep the git self-update armed for `source`, defaulted off.** Argued on the
grounds that contributors are not a production path. Rejected, though the
rejection rests on a narrower claim than it first appears: the branch guard at
`gateway.py:5039-5041` means a feature-branch checkout is never touched, so this
is not "it will rewrite any developer tree". What remains is still
disqualifying — an unattended tree rewrite, reinstall and re-exec performed as a
side effect of daemon startup on `mainline`, and on a detached HEAD that
`gateway.py:5035-5036` silently coerces to `mainline`. Retiring the *automatic*
apply while keeping the *explicit command* takes the defensible half of this
position, and nothing was argued against the command.

**Ship the wheel updater first, contract second.** Argued on user impact — the
headline install has no updater at all, which is the most visible defect.
Rejected on sequencing, not on merit: without the contract there is no correct
surface for the new capability to appear in. Honored by making Phase 2 the
immediate next step rather than a later milestone.

**Let the SPA branch on install shape directly** (extend `isDesktop` into a
four-way switch). Rejected: it puts a build-time fact in the layer furthest
from it, and every new surface must re-implement the switch. It is the current
design, and Problem 2 is its third failure.

**One universal updater.** Rejected as unimplementable: the artifact formats
have nothing in common, and the desktop path is constrained by code signing and
notarization in ways the wheel path is not.

**Auto-updating CLI** (Claude Code's model). Rejected for this product shape —
see §4.

## Open questions

1. Where does the wheel install helper live, and is a real local boundary worth
   building? A console script in the same distribution has a bootstrap problem —
   it is part of what gets replaced — and a copy in `KIROCREW_HOME` is writable
   by the same user. Neither location is immutable on a single-user install, and
   per the Security section that gap cannot be closed by having the helper verify
   provenance itself: an actor who can rewrite the helper can rewrite the check.
   So the open question is whether to accept the gap (network-origin attacks are
   covered by provenance verification; local code execution as the user is not)
   or to pay for a genuine boundary — a system-installed, root-owned helper, or
   handing the swap to pipx itself.
2. Does `kirocrew update` on a running gateway refuse, or signal a drain? §5
   prefers the drain; the refusal is simpler and may be the right Phase 2 scope.
3. Should `docker` report `supported: false` or `supported: true` with
   `can_apply: false`? The latter lets the UI show version drift, which seems
   worth having.
4. Does the contract belong on the status payload, its own endpoint, or both?
   Both duplicates state; status-only couples update state to a hot path.
5. **RESOLVED (recommendation) — and the question's own premise was wrong.** It
   asked which of `os.path.exists(".git")` (assumed: today's HTTP behavior) or
   `Path(".git").is_dir()` (assumed: today's CLI behavior) should win when the
   three derivations collapse. Against the tree, **all three already use an
   `exists()` check** — see the correction under *Problems*. There is no live
   divergence to tie-break, and no linked-worktree behavior gap to close.

   The remaining problem is that `exists()` is not git's answer: it accepts any
   `.git` entry, including one that is not a gitlink, and cannot tell whether the
   directory is really inside a working tree.

   The obvious git-native replacement — `git rev-parse --git-dir` — is **wrong
   here**: it succeeds for any directory whose *ancestor* is a repository. The
   `exists()` check it would replace is at least anchored to the install root;
   `--git-dir` is not. A wheel or source install nested under an unrelated
   checkout (a venv inside a project tree, a home directory that is itself a
   dotfiles repo) would classify as a git checkout — and while the boot-time git
   auto-apply remains armed, Phase 1b built on that would `git reset` a tree with
   nothing to do with the install.

   So Phase 1b should use the git-native answer **with the anchor kept**:

   ```
   git -C <root> rev-parse --show-toplevel   # exit 0 AND realpath(output) == realpath(<root>)
   ```

   `--show-toplevel` returns the working tree's own root, so it is correct for a
   linked worktree and a submodule (each reports its own top level) while the
   equality check rejects ancestor capture. Both halves are required: exit status
   alone reintroduces the ancestor hazard, and the path comparison alone cannot
   distinguish a real worktree from a stray `.git` entry.

   **No user-visible behavior change** relative to today for the worktree case —
   linked worktrees are accepted before and after. What changes is that a stray
   `.git` entry stops being mistaken for a checkout, and one derivation replaces
   three. Cost: one subprocess where there was a `stat`, on a path that already
   shells out to git immediately afterwards.
6. Should the honesty pair extend to the desktop lane? #1734's `check_status`
   equivalent covers the gateway check only; the Electron updater has its own
   `updatesDisabled` enum and its own idle/checking/error states, folded into
   `unavailable_reason` by §2 but not yet expressed as `check_status`. Unifying
   them is right in principle and may not be worth a Phase 1b churn on a lane
   that currently works.
7. **RESOLVED — the dashboard session is NOT sufficient authority to install
   code; Apply requires a gateway-enforced, host-local step-up.** The button
   turns an update into a network-reachable code-install trigger for anyone
   holding a dashboard session — including sessions arriving through tunnels
   (Tailscale serve / cloudflared). This repository has already documented
   (issue #1762) that IP pinning is broken under every same-host proxy, making
   the session effectively a transferable bearer for remote access. A
   credential with that mobility is acceptable authority for chat and
   operations; it is not acceptable authority for replacing the gateway's own
   code. A frontend confirmation dialog changes nothing: the SPA is served
   from the CDN and a compromised SPA can dismiss its own modal.

   **Mechanism.** Apply from the SPA *arms* a pending update request (single-
   use nonce; TTL ≈ 10 minutes; recorded with target version, channel,
   artifact digest, attempt id, and request source). Approval requires an
   action the SPA cannot perform for itself: `kirocrew update approve` run on
   the gateway host, whose identity comes from loopback + filesystem access
   rather than the dashboard session. Only an armed-and-approved request may
   enter §5's drain-then-swap. The audit record gains the approval's origin
   and outcome; an expired or unapproved request decays without side effects.

   **What this rules out and keeps.** One-click remote apply is deliberately
   ruled out in Phase 2. A future policy key could relax the step-up for
   fleets that accept the reduced posture, but it is not part of this design
   and would need its own review. `kirocrew update` run in a terminal on the
   host already *is* the step-up, so the CLI path needs no extra ceremony.

## Provenance

The design was derived by a cross-vendor model panel (OpenAI `gpt-5.6-sol`,
Zhipu `glm-5`, DeepSeek `deepseek-3.2`, each answering blind from the same
brief; a fourth member failed on an upstream error). All three converged
independently on capability-contract-over-shape-branching and on
explicit-action CLI updates. The single material disagreement — retire versus
scope the git path — is recorded under Alternatives with the adjudication.

The draft was then adversarially reviewed against the tree by two further
model-pinned reviewers (`gpt-5.6-sol` mirroring `codex-review.yml`,
`claude-opus-5` mirroring `claude-review.yml` + the AUTOSDE rules), which
produced 11 accepted corrections before that revision: one blocking security
gap (checksums are not authenticity — the already-published SLSA attestation is
the missing half), one design defect that would have refused every non-git
update on any pinned fleet, one process-local-lock generalization that would
have reintroduced a fixed respawn race on the wheel path, four incorrect claims
about current behavior, and four scope or definition gaps. Two findings were
raised independently by both reviewers.

### This revision (2026-08-06, after #1734)

Convened for one question: PR #1734 shipped a working install-shape-aware check
with its own field names while this RFC specified different ones — should the PR's
shape be adopted, renamed, or superseded? Four cross-vendor members answered blind
(`gpt-5.6-sol`, `deepseek-3.2`, `glm-5`, `minimax-m2.5`; `kimi-k3` dropped —
model unavailable on two attempts), each also researching how comparable
multi-install-shape products handle it.

**Unanimous, and the reason this revision exists:** the shipped honesty semantics
belong *in the contract*, not in the implementation. All four independently called
their absence a gap in the RFC rather than surplus in the PR. Also unanimous:
`managed_by`'s capability taxonomy is the better long-term vocabulary, and
`state` / `progress` must not ship before their backend (§2.3).

**Split, and recorded as such:** rename timing went 2 (rename before merging) : 1
(merge as-is, migrate later) : 1 (revise the RFC around the shipped shape). The
adjudication took the merge-then-migrate path on a fact the panel did not have —
the SPA and backend ship version-locked in every distribution shape, so the API
stability cost the two rename-now members were pricing is close to zero. That
reasoning is written into *Vocabulary migration* under Phases so a later reader
can re-test the premise rather than inherit the conclusion.

**Two design refinements came from the panel, not from the PR:** `check_status`
as a single enum in place of two loosely-coupled booleans, and `deferred` as a
first-class state distinct from `failed` (§2.1). The second was corroborated by
implementation evidence — #1734 had to add a frontend "info codes" set to stop
deferrals rendering as failures, which is what a missing state looks like from
the inside.

**Research findings that bear on the design** (each member searched
independently; sources in the session record):

- The organizing rule holds up. Tailscale routes `tailscale update` to whatever
  installed it, refuses on Snap and tells the user to `snap refresh`, and
  documents containers as immutable ("pull a newer image"). rustup ships a
  build-time `no-self-update` for distributors. uv enables `self update` only for
  its own standalone installer and points Homebrew users at `brew upgrade`.
  VS Code hides its update affordance under Snap/Flatpak and lets enterprise
  policy disable it outright. Docker Desktop disables in-app updates for PKG
  installs. Capability is provenance; consent is policy.
- **No comparable product exposes a machine-readable capability contract.** They
  all express this through CLI text, hidden menu items, and docs. That is not an
  argument against §2 — Kiro Crew has one SPA talking to one backend across five
  materially different shapes, which none of them do — but it does mean there is
  no prior art to copy, and the contract's cost/benefit rests on that
  single-SPA-many-shapes property specifically.
- **A published walk-back worth heeding:** uv had to fix a case where PATH
  shadowing made its self-update target the wrong binary, and Tailscale had one
  where `tailscale update` broke after assuming a specific apt repository
  filename. Both are the same failure: reconstructing install provenance from
  incidental filesystem layout. It is a direct argument for this RFC's
  build-time stamp over runtime path-sniffing — and for Open Question 5's
  anchored `rev-parse --show-toplevel` over `.git` shape-guessing. Note the
  symmetry: the *unanchored* form of that same check (`rev-parse --git-dir`,
  which walks up to any ancestor repo) is itself an instance of the failure
  these two walk-backs describe, which is why OQ5 requires the equality check
  rather than the exit status alone.

### Managed-venv mechanics review (2026-08-06, same day, second panel)

A second adversarial panel (`gpt-5.6-sol`, `deepseek-3.2`, `glm-5`; a fourth
member failed twice on an upstream error) red-teamed a concrete
versioned-venv-plus-symlink-flip design for the wheel shape, drawn from a
comparison against Claude Code's native installer (per-version binaries behind
one symlink) and Codex CLI (detect the owning package manager and delegate).
Verdicts: REVISE / REJECT / REVISE. The direction survived; the specific
guarantees did not — `ln -sfn` is not atomic, the old-inode claim ignores lazy
imports, four persisted launch paths exist rather than one, a rebuilt venv
re-resolves unsigned dependencies, digest-only provenance admits
artifact-substitution from an unsigned feed, and pipx delegation re-creates the
in-place torn-runtime hazard. Per the panel's convergent placement finding, the
outcome entered §3 as **invariants plus a first-migration open problem**, and
the consent exposure as Open Question 7 — not as a settled mechanism. The
mechanism itself is Phase 2 implementation-PR territory.

Human review (rajpratham1, 2026-08-07) then required both open boundaries to be
resolved before Phase 2 implementation. This revision resolves them: the
first-migration protocol is defined in §3 (fresh stable symlink name; the
existing fixed directory is never moved and remains a functional fallback), and
Open Question 7 is resolved to a gateway-enforced, host-local step-up for the
in-app Apply — the dashboard session alone is explicitly not sufficient
authority to install code, on the strength of the documented tunnel-pin
breakage (issue #1762).


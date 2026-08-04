---
title: Update Architecture (install-shape capability contract)
status: draft
author: zezhexu
created: 2026-07-31
last-audited: 2026-08-03
audited-at: 0ab6ed48
doc-pr: 1003
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Update Architecture (install-shape capability contract)

- Status: draft — the document is merged (PR #1003) but **zero of its three phases has any implementation on main.** `platform/update_capability.py` does not exist; `KIROCREW_DISTRIBUTION` is still telemetry-only with one caller (`beacon.py:438`); the three divergent `.git` derivations are intact; boot-time git auto-apply is still armed (`slack/gateway.py:5145`); all three SPA surfaces remain install-shape-coupled; no wheel/pipx self-replacement path exists; no on-disk update lease. Adjacent in-flight under a **different** design: PR #999 (`feat/emergency-release-controls`, open) adds a feed-served minimum version + mandatory-update modal for the desktop lane only, without the capability contract.
- Correction to the reference below: KiroCrew ships **five** distribution shapes, not the set implied — `beacon.py:155` lists `{dmg, appimage, wheel, source, docker}`.
- Author: zezhexu
- Created: 2026-07-31
- Related: `docs/release-process-design.md` (channels, release branches, promotion), `docs/request-for-change/version-compliance-framework.md` (the policy ceiling this RFC must honor)

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
a shell. Windows (Squirrel) has no value in the set and reports `source`. The
field is read **only by telemetry**; no update code consults it.

Three mechanisms:

| Mechanism | Where | Covers |
|---|---|---|
| git self-update | `slack/gateway.py:4959` (`_check_for_updates`, called once from startup at `:5426`) → `_auto_apply_update` (`:5004`) | `source` only |
| Electron OTA | `website/electron/auto-update.js` (electron-updater, `autoDownload=false`, `autoInstallOnAppQuit=false`) | `dmg`, `appimage` |
| — none — | | `wheel`, `docker` |

All three backend entry points to the git path guard on roughly the same two
conditions — `KIROCREW_PROJECT_DIR` set, and a `.git` present — but **not with
the same semantics**:

- `dashboard/handlers/updates.py:75,82` (`_do_update_check`) — `os.path.exists`
- `dashboard/handlers/updates.py:382,388` (`api_update_apply`) — `os.path.exists`
- `cli_server.py:1145,1150` (`kirocrew update`) — `Path.is_dir()`

In a linked worktree or a submodule, `.git` is a **file**, not a directory (the
comment at `updates.py:80-81` says so explicitly). So the HTTP paths accept a
linked worktree that `kirocrew update` rejects. Three re-derivations of the same
fact, none of them the build-time value, and two answers between them. Any
collapse to one derivation must therefore pick a semantic deliberately rather
than inherit one by accident.

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
  "minimum_version_enforced": null,
  "unavailable_reason": null,
  "remediation": null,
  "state": "idle | available | downloading | ready | draining | restarting",
  "progress": null
}
```

`progress` is `{ "percent": 0-100, "bytes_per_second": 0 } | null`, and it is
load-bearing rather than decorative: `AboutPanel.tsx:278` already renders
`<Progress value={cardPercent}>` plus a transfer-rate label from `percent` /
`bytesPerSecond` (`:28-29`), which today arrive over the Electron IPC channel.
Without `progress` in the contract, Phase 1 would have to either keep that
out-of-contract channel alive — the exact shape-coupling this RFC exists to
delete — or leave the wheel engine's bar permanently indeterminate.

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
in-app Apply button is as explicit as typing the command, and since the backend
can invoke the install helper it can serve both — which is why `wheel` carries
`can_apply: true` after Phase 2. What is ruled out for the CLI shape is the
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

**Phase 1 — the contract.** Add `platform/update_capability.py`; serve the
contract; collapse the three ad-hoc `.git` derivations into it (picking a
semantic deliberately — Open Question 5); convert all three SPA surfaces to
consume it; de-arm the boot-time git apply; and retire the three `auto_update`
surfaces named under Migration. This alone closes Problem 2 and prevents
Problem 4. Smallest change of the three, and a prerequisite for the others —
shipping a wheel updater first would deliver it into surfaces that currently
misreport what is possible.

**Phase 2 — wheel updater.** A wheel apply path: feed resolution, **provenance
verification** (§3), external-helper pipx replacement, and a gateway drain
request when one is running. Reachable two ways from the same backend entry
point — `kirocrew update` in a terminal, and an in-app Apply button — which is
what flips `can_apply` true for `wheel` (§4).

**Phase 3 — shared drain-and-restart handshake.** Extract §5 into one
orchestrator used by both engines, with the on-disk update lease honored by the
watchdog, the quit path, **and the supervisor unit**; the post-restart
verification handshake; and the policy-forced-update deadline + user messaging
that §4 identifies as new.

De-arming the git boot-apply (Problem 3) lands in Phase 1 with the contract,
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
5. When the three `.git` derivations collapse into one, which semantic wins —
   `os.path.exists` (accepts a linked worktree, today's HTTP behavior) or
   `Path.is_dir()` (rejects it, today's CLI behavior)? `exists` is the more
   permissive and better-documented choice, but it means `kirocrew update`
   starts accepting worktrees it currently refuses, which is a behavior change
   for contributors.

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
produced 11 accepted corrections before this revision: one blocking security
gap (checksums are not authenticity — the already-published SLSA attestation is
the missing half), one design defect that would have refused every non-git
update on any pinned fleet, one process-local-lock generalization that would
have reintroduced a fixed respawn race on the wheel path, four incorrect claims
about current behavior, and four scope or definition gaps. Two findings were
raised independently by both reviewers.

---
title: Central governance ceiling — time-boxed local override (break_glass) and platform managed tier
status: draft
author: kirocrew agent session, directed by maintainers of #7362
created: 2026-09-08
last-audited: 2026-09-08
audited-at: 8a9c269b4
doc-pr: 9373
implementation-prs: [7362]
tracking-issues: [9106]
supersedes: []
superseded-by: []
---

# RFC: Central governance ceiling — time-boxed local override (break_glass) and platform managed tier

Status: draft. Nothing in the `break_glass` mechanism or the platform managed
tier described below exists on main. Every "does not exist" claim here was
measured at `8a9c269b4`: `break_glass`, `BreakGlass`, `BREAK_GLASS`,
`_read_managed_policy`, `_managed_policy_path`, `compose_tier_ladder` and
`compose_local_ceiling` each return zero hits under `src/` (grep for callers,
not just definitions).

This document exists because [PR #7362](https://github.com/kirodotdev/KiroCrew/pull/7362)
landed the central governance ceiling — the fleet's security document binding
above local configuration — but **withdrew** two things before merge: a
time-boxed local override (`break_glass`, RFC step 6) and a fully generic
platform managed tier. This RFC is the design-of-record for both, so the work is
not lost and any re-attempt starts from the review history rather than from
scratch. It is a record of a decision, not a description of code on main.

> **Landed by #7362 (status delta, added on that branch).** The "does not exist"
> claims in this document are measured at `8a9c269b4` and are accurate for that
> commit. PR #7362 ships **only** the tighten-only tier ladder in
> `load_security_policy` — central above every local document, each lower tier
> intersected in, and no local rollback lever. The platform managed tier described
> under *Design* is **not** shipped by it and remains design-of-record here in
> full, as do `break_glass` (#9106) and the Windows
> `HKLM\SOFTWARE\Policies` rung (Windows stays advisory).

---

## Summary

The shipped ceiling composes tiers with the precedence
`KIROCREW_SECURITY_POLICY` (local file) → centrally-distributed document →
companion-bundled → `~/.kiro/crew/security_policy.json` (home) → none
(ungoverned). Local tiers may only *tighten*; the fleet's document is the
ceiling. See [`../system-specs/modules/governance.md`](../system-specs/modules/governance.md)
and `load_security_policy` in
[`../../src/kiro_crew/platform/governance.py`](../../src/kiro_crew/platform/governance.py)
for the shape that is actually on main.

This RFC records two follow-ups that were designed but not shipped:

1. **`break_glass`** — a time-boxed local override in which an authority document
   carries a dated grant naming a lower tier, and that tier *replaces* the
   authority until the grant expires. It was implemented in #7362 and withdrawn
   before merge. This RFC preserves the design and the four security-class defect
   classes that drove the withdrawal, and defers any re-attempt to its own
   reviewed change.

2. **A platform managed tier** — a machine-owned rung above the central document
   on macOS (managed-preferences profile) and Windows. On main there is no
   managed tier, so on Windows the ceiling is *advisory* against a local account.
   This RFC records the macOS scope and coverage gaps and names the intended
   Windows implementation: the machine-policy registry key
   `HKLM\SOFTWARE\Policies\<vendor>\<product>`.

## Motivation

### Current state (what is on main at `8a9c269b4`)

`load_security_policy` resolves the first present tier as the governing ceiling,
in this order:

1. `KIROCREW_SECURITY_POLICY` — an explicit local file, the fleet's rollback
   lever, highest.
2. the centrally-distributed document — fetched from `KIROCREW_POLICY_URL` or the
   `distribution.source` a lower tier declares, served from a last-known-good
   cache when the endpoint is unreachable
   (`resolve_distribution` / `PolicyDistribution` in
   [`../../src/kiro_crew/platform/policy_distribution.py`](../../src/kiro_crew/platform/policy_distribution.py)).
3. the companion-bundled resource (`bundled_loader`), supplied only by the
   `amazon` edition.
4. `~/.kiro/crew/security_policy.json` — standalone operator-authored home file.
5. none → editable secure-defaults (ungoverned ceiling).

Local tiers are tighten-only subordinates: the fleet's document sets the ceiling
and a lower tier can only narrow it. A present-but-invalid policy fails closed to
strictest (`PlatformCompositionError`). There is **no managed-tier rung** above
the central document on main.

### Why `break_glass` was withdrawn

`break_glass` was step 6 of the original plan: an authority document could carry
a dated `break_glass` block naming a lower tier, and on a live grant that tier
would *replace* the authority (not merely tighten it) until the grant's `expires`
date passed. It was fully implemented in #7362 and then withdrawn by maintainer
decision. Three reasons, and the second is the operative one:

**1. It is a direct exception to the invariant the PR ships.** The PR's whole
claim is "no local document outranks the fleet's ceiling". A grant that lets one
do so — however dated, however audited — is that override with a timer on it.
Shipping the rule and its exception in one change made the exception the thing
reviewers attacked while the rule had been stable for rounds.

**2. It was where the defects were, and they were security-class.** Every
late-round BLOCKING finding landed in the grant's lifecycle, not in the ladder.
Four defect classes, each real, each fixed in-PR, and the surface kept producing
more:

- **Source-less expiry never retired.** A dated grant on a *source-less* managed
  host was never retired: the refresher never polls when there is no
  `distribution.source`, so past `expires` the local tier kept replacing the
  authority until process restart. This is the "expiry was decorative" failure
  (an Opus advisory), and it was left unfixed for managed-only fleets.
- **A malformed cache kept an expired grant active.** The expiry repair returned
  on a parse failure *before* recomposing the ladder, so one unprivileged write
  to the cache directory held the escape hatch open.
- **The repair reinstalled a boot-rejected stale cache.** The repair path
  reinstalled a cache that boot had already rejected as too old — it never
  checked `max_cache_age_secs`.
- **The three-tier stand-down predicate was wrong twice.** The refresher
  stand-down predicate (`break_glass_local_policy`) had to reason about three
  tiers × two "shapes" of the grant × whether the granted document still exists,
  and got it wrong twice: a deleted home rollback rejected every refresh until
  expiry, and a granted bundled tier froze fleet updates for a whole window.

A mechanism that needs a repair path, a stand-down predicate, a cache-trust rule,
and an expiry timer that must survive the absence of the very channel that would
end it is not a small addition to the ladder — it is a second system.

**3. The threat model was never settled.** The grant is issued by the authority,
so a fleet that never issues one is unaffected. But a fleet that *does* has handed
a dated key to whoever can place a document at the granted tier, and the PR's own
review history shows how many ways that window could outlive its date.

## Goals

- Preserve the withdrawn `break_glass` design faithfully enough that a re-attempt
  starts from the review history, not from scratch.
- Record the four security-class defect classes so any re-design is measured
  against them as acceptance criteria, not rediscovered.
- Record the platform managed-tier follow-ups (macOS scope + coverage; Windows
  advisory-ceiling limitation and the intended registry channel) as design of
  record.
- Keep the shipped invariant — "no local document outranks the fleet's ceiling" —
  intact and reviewable on its own.

## Non-goals

- **Re-adding the removed `break_glass` code is a non-goal.** The maintainer
  decision recorded here is: land the invariant first; design the exception, if
  at all, as its own change with its own review. The pieces listed under Design
  are a *record* of what was withdrawn, not a request to re-implement them.
- Re-adding the withdrawn generic platform managed tier as code in this document.
  This RFC names the intended Windows target and the macOS gaps; the
  implementation is a separate reviewed change.
- Changing the shipped tighten-only precedence or the central-distribution
  transport.

## Design

The subsections below record the withdrawn design as it existed in #7362 before
commit `88d49fe35` (recoverable from that PR's history). Symbol and file names
here name code that **does not exist on main**; an RFC under
`docs/request-for-change/` deliberately may.

### Withdrawn: the `break_glass` mechanism

The pieces, as implemented and then withdrawn:

- **`governance.BreakGlass` dataclass.** Fields `tiers`, `expires`; methods
  `grants(tier, now=)` and `summary()`. A module constant
  `BREAK_GLASS_TIERS = {env, bundled, home}` fixed which tiers a grant could name,
  and a `_deadline()` helper clamped the deadline with a `date.max` guard.
- **`GovernanceCeiling.break_glass` field.** Carried on the ceiling, with parser
  validation and a `_KNOWN_KEYS` entry so the block round-tripped through the
  policy loader.
- **The replace branch in `compose_tier_ladder`.** On a *live* grant, the
  released (granted) document *replaced* the authority — carrying forward the
  authority's `break_glass` block, its `distribution` pins, and (when the
  authority was non-verified) its `signature_state`. The override was
  fail-closed: `_audit_policy_tier(..., critical=True)` made the audit a
  *precondition* of the override, so a host that could not audit the override did
  not take it.
- **`policy_distribution.break_glass_local_policy()`.** The refresher stand-down
  predicate: while a grant was live, the refresher stood down so it would not
  fetch over the released tier. This is the predicate that got the three-tier
  reasoning wrong twice.
- **`_repair_lapsed_rollback()` + `_stale_env_tier_omitted()`.** The expiry
  repair: on a lapsed grant, recompose the ladder without the released tier.
  These are where the malformed-cache and boot-rejected-stale-cache defects lived.
- **`compose_local_ceiling()`.** The ladder with *no central rung*, which existed
  only for the repair path (composing what the host should govern from once the
  released tier is dropped).
- **Guards in `validate_ceiling` and `refresh_now`.** Guards that respected a live
  grant when validating and when a manual refresh was requested.
- **~600 lines of tests** across `test_governance_distribution.py` and
  `test_governance_managed_tier.py`, most mutation-verified.

This design is preserved as a record. It is **not** proposed for re-implementation
in this document; see Non-goals.

### Platform managed tier

The ceiling's goal sentence is "a person using the laptop cannot loosen it". That
holds where a *machine-owned* tier sits above the central document. On main there
is no such tier, and the two platform follow-ups below record what a managed tier
must handle. Neither is implemented on main (`_read_managed_policy` and
`_managed_policy_path` return zero hits under `src/` at `8a9c269b4`).

#### macOS: managed-profile scope level and binary-plist coverage

A macOS managed tier reads a configuration profile that `cfprefsd` materialises
under `/Library/Managed Preferences`. Two gaps:

- **Scope level.** A machine-path reader (`_read_managed_policy`) reads only the
  machine path `/Library/Managed Preferences/dev.kirocrew.plist`. A profile
  deployed at Jamf **User Level**, or through Intune's **user channel**, lands at
  `/Library/Managed Preferences/<username>/dev.kirocrew.plist`, which a
  machine-only reader does not read. The host then reports `managed<-absent` and
  governs from a local tier *with no error*. The guide must state that the
  profile has to be **Computer Level / device channel**, and the design must
  decide whether a user-level profile is *deliberately ignored* or *read as a
  lower-than-machine rung*.
- **Binary-plist coverage.** The file `cfprefsd` materialises is a **binary**
  plist. `plistlib.loads` auto-detects the format today, but the withdrawn tests
  only wrote XML (`_write_plist_policy`). One `fmt=plistlib.FMT_BINARY` case would
  pin the real on-disk shape.

#### Windows: the ceiling is advisory, and the target is HKLM Policies

- **Advisory today.** With `_managed_policy_path()` returning `None` on Windows
  there is no managed `distribution` pin, so `resolve_distribution` keeps the
  per-setting environment override, and a standard user can set
  `KIROCREW_POLICY_URL` to a document of their own — replacing the whole central
  rung. The guide's Windows section recommends central distribution as the
  substitute without saying this. The RFC's goal sentence ("a person using the
  laptop cannot loosen it") is therefore **not met on Windows**. Until a Windows
  managed tier exists, the docs must say plainly that on Windows the ceiling is
  *advisory* against a local account.
- **Intended target: the machine-policy registry.** A docstring's stated path to
  a Windows managed tier was `SHGetKnownFolderPath(FOLDERID_ProgramData)` plus an
  ACL check plus reparse-point handling. The idiomatic machine-policy channel on
  Windows is the registry key `HKLM\SOFTWARE\Policies\<vendor>\<product>`, which
  is what Group Policy, Intune ADMX-backed profiles, and OMA-URI write. It is
  administrator-writable only by default ACL, **cannot be redirected by an
  environment variable**, has a stdlib reader (`winreg`), and is exercisable on
  the Windows CI runners this repo already has. That sidesteps all three problems
  the file-path approach lists (the ProgramData folder lookup, the ACL check, and
  reparse-point handling). This RFC records `HKLM\SOFTWARE\Policies\<vendor>\<product>`
  as the **intended Windows managed-tier step**.

## Migration plan

Phased, each phase independently shippable and independently abandonable. A phase
whose entry depends on an unanswered Open question is marked blocked on it.

**Phase M1 — Windows managed tier (registry).** Implement `_managed_policy_path()`
for Windows reading `HKLM\SOFTWARE\Policies\<vendor>\<product>` via `winreg`, so a
machine-policy pin outranks the per-setting `KIROCREW_POLICY_URL` override.
*Exit criteria:* on a Windows CI runner, a machine-policy value set under that key
produces a managed `distribution` pin that `resolve_distribution` honours over a
user-set `KIROCREW_POLICY_URL`; a standard user cannot replace the central rung.

**Phase M2 — macOS managed-tier scope + coverage.** Implement `_read_managed_policy`
for the Computer-Level machine path, and decide the user-level policy (ignored vs.
lower rung) per the Open question. Add a `fmt=plistlib.FMT_BINARY` test case.
*Exit criteria:* a Computer-Level profile governs; a user-level profile behaves
per the resolved decision and is not silently treated as `managed<-absent` without
a documented reason; a binary-plist fixture parses in the test suite.

**Phase G1 (blocked on Open questions) — local override, if any.** Only if the
Open questions resolve in favour of a *local* override rather than server-side
recovery: design the grant lifecycle so every one of the four withdrawn defect
classes is a passing acceptance test (source-less expiry retires; a malformed
cache cannot hold a grant open; the repair honours `max_cache_age_secs`; the
stand-down predicate is proven over all tier × shape × existence combinations).
*Exit criteria:* the four defect classes have named regression tests, and the
expiry timer is proven to fire on a host with no refresh channel. **Blocked** on
the four Open questions below.

## Backward compatibility

- The platform managed tier is *additive*: a host with no managed policy resolves
  exactly as it does today (env → central → bundled → home → ungoverned). No
  existing standalone install or policy file changes byte-for-byte.
- On Windows, adding a machine-policy pin *tightens* who can steer distribution;
  a host without the registry key is unchanged. This closes a gap rather than
  altering an established contract.
- No `break_glass` behaviour is re-introduced, so there is no compatibility
  surface from it in this document.

## Security considerations

- The whole point of the managed tier is to make the ceiling *non-advisory* on
  Windows against a local account. The registry channel is chosen precisely
  because it is administrator-writable by default ACL and cannot be redirected by
  an environment variable a standard user controls.
- Any local override (`break_glass`) is, by construction, a dated exception to the
  invariant. It hands a time-boxed key to whoever can place a document at the
  granted tier. Its audit was designed as a fail-closed *precondition*
  (`_audit_policy_tier(..., critical=True)`); even so, the review history shows
  the window could outlive its date. This is the reason it is deferred.
- The macOS scope gap is itself a security consideration: a user-level profile
  silently reporting `managed<-absent` means a host can be *believed* governed
  while it is not. The design must make that state either impossible or loud.

## Alternatives considered

- **Purely server-side recovery (shipped default).** Recover from a bad central
  push by re-publishing a good document; the refresher picks it up. This is what
  #7362 ships and it needs no new mechanism, no repair path, no stand-down
  predicate, and no host-local expiry timer. It is the baseline every local
  override must beat.
- **Ship `break_glass` with the invariant (rejected).** This is exactly what was
  withdrawn: bundling the rule and its exception made the exception the review
  target and the defect surface. Rejected in favour of landing the invariant
  first.
- **Windows file-path managed tier (`ProgramData`).** Rejected in favour of the
  registry: the file path needs a `SHGetKnownFolderPath(FOLDERID_ProgramData)`
  lookup, an ACL check, and reparse-point handling; the registry key sidesteps
  all three and has a stdlib reader.

## Open questions

The four questions from #9106 that must be settled before any re-attempt of a
local override:

1. Is a **local** override the right shape at all, or should recovery from a bad
   central push be purely **server-side** (re-publish a good document; the
   refresher picks it up)? The PR ships the latter and it needs no new mechanism.
2. If a local override is wanted: who is the **issuer**, and does the fleet get a
   **signal** that it is in use? (The PR's absence audit establishes the pattern —
   report, let the fleet compare.)
3. How does the override **expire on a host with no refresh channel**? This is the
   unfixed Opus finding. Any answer needs a timer that lives *outside*
   `start_refresher`.
4. What is the **cache-trust rule** for the repair path? The PR's final answer was
   "exactly what boot applies" (`_from_cache_on_outage`), which is the right
   instinct — but it means the repair cannot be reasoned about separately from
   boot.

A resolved answer to Question 1 in favour of server-side recovery closes
Questions 2–4 and retires Phase G1.

## Problem / Motivation

**Goal:** Stop a cron child that runs model-supplied code from reading any installed app's bearer credential.

`<config_dir>/apps/<name>/.app_secret` is a bearer credential, not a marker: `dashboard.token_auth.validate_app_secret` compares it and issues that app's scoped token, so whoever reads one can act as that app against the gateway. Both cron exec paths run model-supplied code and neither named that tree to the sandbox:

- `run_script_sandboxed` runs a script body from the `crons/` directory, which is LLM-writeable by design. Its ungranted branch passed an **empty** `extra_hidden_dirs`.
- `run_command_sandboxed` runs a `command` string the cron tool accepts verbatim.

So a cron child could read every installed app's credential.

## Why it matters

An app's `.app_secret` is the app's identity. A cron script or command -- the two surfaces whose content comes from a model rather than from a person -- could read all of them and then call the gateway as any installed app, with that app's scopes. Nothing in the cron path logged it or refused it, and nothing about the read looks different from an ordinary file access, so the first sign of it would be an action attributed to an app that never took it.

The exposure is not gated on an unusual configuration. It applies on every host with an OS sandbox backend and at least one installed app.

## Not a goal

- Refusing a spawn merely because a secret carries more than one HARD link. `fs.protected_hardlinks` reads `1` on the measured host, so a second hard link is a persistence primitive an ordinary same-UID actor reaches; a host-wide refusal on link count would let one app's on-disk layout stop every cron on the host. That case is handled by withholding windows plus the pre-exec hardlink scan, not by refusal.
- Widening the pre-exec hardlink scan beyond `$CWD` and `/tmp` to walk the whole filesystem on every scheduled spawn.
- Changing the shared masking primitive itself (e.g. making the mask read-only outside its windows) — that touches every credential tree, not this call site.
- Closing the Seatbelt rename residual with a host-wide refusal on macOS, which would deny every cron the durable-data view an app-bundle cron needs.

## What changed (motivation → approach → change)

Symptom: a cron child reads `<config_dir>/apps/<name>/.app_secret`. Root cause: nothing named that tree to the sandbox at either spawn.

1. `run_script_sandboxed` adds `<config_dir>/apps` to `extra_hidden_dirs` on **both** branches. A grant's isolation is the stricter of the two, so it must not be the branch that keeps the credentials; its existing entries stay, since they close the mutable-sibling import route, which is a different concern.
2. `run_command_sandboxed` passes the same target. The two paths share one trust level, so a control on only one of them is bypassable by choosing the other. The storage-time command vet cannot substitute: it denies credential references in the command **text**, names no app secret, and a shell can build a path the name never appears in.
3. The **containing directory** is masked, never a list of leaves read out of it. A mask is fixed at spawn and never recomputed for a live child, so an enumeration cannot name an app installed while the run is still executing, and that app's secret would stay readable for the rest of the child's life.
4. That tree also holds each app's **`data` root**. On Linux the mask is a bind of a **writable** empty directory, so a masked-only spawn would tell an app script cron its writes succeeded and discard them when the namespace went away. Both call sites pass each installed app's `data` directory as `extra_private_dirs`, the primitive that keeps the parent's mask and every sibling denied while re-exposing just those directories on their real inodes, read-write. Every `.app_secret` sits beside `data` rather than inside it, so the ancestor mask still denies all of them.
5. New `sandbox.app_data_window_targets` enumerates those windows and **degrades per app**: a link or a plain file squatting one app's directory or its `data` name withholds that one window and leaves that app's data masked (the fail-closed direction), rather than refusing the spawn and letting one app's layout stop every cron on the host. Entry names must match `apps.manifest.KEBAB_RE`, the one contract install/update/discovery/self-registration all funnel through; lifecycle move-asides and case-variant names are skipped.
6. The two relationships a window can have with a masked leaf are settled separately: a window that EQUALS a hidden leaf is refused everywhere; a window that CONTAINS one is an ordering problem the launcher solves by re-applying nested masks immediately after binding the window (Linux only; the Seatbelt profile, which cannot order its rules that way, refuses it).
7. A window that would re-expose a tier-masked leaf is refused via `_private_window_spellings`, the single gate every caller's windows pass through on both backends. The Linux launcher **pins** each window and each mask root by `(dev, ino)` and binds from the descriptor (`/proc/self/fd/<n>`) rather than the name, closing the validate-then-bind rename/link race; a mask-root mismatch REFUSES the spawn (a skip would leave the tree exposed), a window mismatch is SKIPPED (the mask still stands). The identity travels on optional parameters over `wrap_argv → namespace_argv → _build_launcher_script`, and the builder only serializes it, so the AST invariant forbidding a stat inside the builder still holds.
8. The launcher answers three link/alias shapes the directory mask cannot: a HARDLINKED `.app_secret` (via the pre-exec scan seeded with `(dev, ino)` pairs read in the parent by `aliased_app_secret_ids`, walking `$CWD` and `/tmp` under a per-root budget that degrades OPEN), a SYMLINKED `.app_secret` (`refuse_if_an_app_secret_is_linked` at both call sites), and a symlinked app DIRECTORY (entry-shape refusal). The staging bind that carries a window's tree across the mask is retired in one place after every window binds and every nested mask re-applies, with `umount2(MNT_DETACH)`; a failed detach refuses the spawn.
9. `sandbox.materialize_caller_masked_dir` / `materialize_app_data_window` create absent caller paths descriptor-relative with the same fail-closed guards `_materialize_maskable_dirs` uses, on hosts advertising `O_DIRECTORY` + `dir_fd`; a portable path-based fallback plus a no-follow identity read covers hosts without them. Both launcher loops are `isdir`-guarded. A created window reports whether it made the directory, and only a directory this spawn made is given back (undo via `rmdir`, which refuses a non-empty directory), closing the staleness-proof forge against a parked lifecycle copy.
10. Both call sites gate the creates on `sandbox.credential_mask_applies`, false exactly where `wrap_argv` hands back an unwrapped child; the command path reports a materializer refusal itself rather than the generic "Command failed" handler.

The seatbelt backend takes the identities too and verifies each window and mask root with a no-follow `lstat` immediately before the profile is written, refusing on mismatch rather than silently dropping every pinned window (which had denied every macOS app cron its own persistence directory).

Enumerated cases with a verdict each, and the four OPEN residuals (each disclosed by code site, not built around), are retained in full in the PR's follow-up comment / commit body.

## Backwards compatibility

Compatible: a caller that supplies no identity is pinned by name exactly as before, so the three pre-existing window producers are unchanged; a mask root with no entry keeps the plain-name behaviour, so every other caller of `extra_hidden_dirs` is byte-identical. The new parameters (`extra_private_dirs` identities, `extra_hidden_dir_ids`, `extra_alias_credential_ids` / mask-root ids) are all optional and empty for every other caller. No documented, reachable capability is removed: the one cron-script example in `docs/app-kit/api-reference.md` authenticates with the gateway's internal secret, not `.app_secret`, and `getAppDataDir()` persistence keeps working through the data windows.

## Tests

`test/test_cron_apps_secret_mask.py`, 144 cases (`pytest -n0`): 143 passed, 1 skipped. With the window sibling, the two launcher-guard files (`test_sandbox_mount_checked.py`, `test_sandbox_mount_source_sweep.py`), `test_sandbox_argv.py`, and `test_sandbox_hardlink_scan.py` in one run: 492 passed, 5 skipped. The four cron files this change also touches: 433 passed, 1 skipped.

Coverage locks in each behaviour and every fix is mutation-checked with disjoint reddened sets, including: the raced-window refuse-vs-skip on both paths; the app-bound ordering (tested only after the entry is known app-shaped); the descriptor-relative create against a swapped app entry; the mask-root identity carried to and compared in the child; the no-mask-host gate; the tier-hidden refusal at both its enforcement points; the stage retirement (executed, not read); the name contract vs lifecycle move-asides and case-variant names; log sanitization of agent-created directory names; the Seatbelt window/mask-root verification; the credential-alias hardlink and symlink passes; and the structural pins that hold every dispatch forwarding a mask to also forward the identity.

`test_sandbox_mount_checked.py`: 30 passed. `test_sandbox_mount_source_sweep.py`: 78 passed. `test_cron_script.py`: 139 passed, 1 skipped. `test_cron_cancel.py`: 12 passed. `test_cron_secret_env.py`: 95 passed. `test_mcp_cron_security.py`: 187 passed. `test_sandbox_private_window_in_caller_mask.py`: 14 passed.

flake8, isort and black clean on both changed files; `mypy src/kiro_crew/sandbox.py`: no issues; the comment-history gate passes on the diff; Semgrep clean over the diff with the repo's own rules and the two registry rules that had fired (both confirmed discriminating).

## Manual verification

N/A — unit and launcher-region-executing coverage is sufficient; the mount/stage/identity logic is exercised by harnesses that exec the generated launcher regions under a fake libc rather than by reading their text.

## Pattern harvest

Rule candidate: review-prompt
Pattern: a new pre-spawn validation placed unconditionally ahead of an existing fail-closed refusal, on a path where the new control has no effect, only adds a failure mode and replaces the actionable remedy with an incidental one. Gate a new confinement step on the predicate that says whether the confinement is actually carried.

Rule candidate: review-prompt
Pattern: masking a directory to withhold one file class also withholds every other class in the tree; when the mask primitive is a writable empty bind, the collateral failure is a silent success (the write reports OK and the bytes are gone). Enumerate what else lives in the tree and check each against the mask's failure shape, not just against "is it a credential".

Rule candidate: review-prompt
Pattern: a loop that tests a path against a LIST of parents and breaks on the first match has judged only that one parent. A path can be a proper descendant of one masked tree while being an ancestor of a second masked leaf inside it. Evaluate the whole set and write the invariant as a sentence over the set ("equals none, contains none").

Rule candidate: review-prompt
Pattern: `O_NOFOLLOW` constrains the LAST component only, so a single no-follow open does not make a multi-component path safe against link planting — it moves the swap one directory up. Resolve a re-resolved path a component at a time descriptor-relative from a pinned root, chosen deliberately.

Rule candidate: review-prompt
Pattern: a step that enumerates a shared directory and CREATES per entry must encode that directory's naming contract, not merely "is a real directory"; a tree whose owner parks its own move-asides there holds entries that pass every structural test the real ones pass.

Rule candidate: review-prompt
Pattern: when a filter's justification cites an existing validator by name, call that validator instead of hand-rolling the one case — the hand-rolled test admits every other name the validator refuses.

Rule candidate: review-prompt
Pattern: `O_NOFOLLOW` + descriptor-relative walk settle "was a link planted", not "was a real directory renamed onto the name"; a path validated in one process and opened in another needs an IDENTITY carried across the gap, taken in the same act as the approval and where the environment permits a stat.

Rule candidate: review-prompt
Pattern: a name-based mask and a name-based window fail differently, so the same identity check needs opposite fallbacks — a window that cannot be confirmed is SKIPPED (mask stands), a mask that cannot be confirmed must REFUSE (skipping leaves the tree readable). Decide the fallback from what the step is FOR.

Rule candidate: review-prompt
Pattern: a mechanism that carries protected bytes past a barrier opens a SECOND path to them and that path needs retiring, not ignoring; two retirement sites that both run before the payload starts are duplicate logic, not defence in depth.

Rule candidate: review-prompt
Pattern: a structural pin that greps for one call site's spelling proves nothing about the sibling hop carrying the same argument; enumerate every dispatch and assert the invariant over the set, then mutate each member.

## Related Issues

Closes #12518

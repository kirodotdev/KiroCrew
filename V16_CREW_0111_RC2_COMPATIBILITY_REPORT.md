# Kiro BYO 0.3.0 V16 — KiroCrew 0.11.1-rc.2 Source Audit gate repair

Date: 2026-11-01. Base: V15. No changes to the original V15 source tree.

## Confirmed root cause

The user's screen showed `KiroCrew 0.11.1-rc.2 is not supported by BYO 0.3.0`. The V15 `integration_manager._crew11_semantic_candidate()` only allowed `0.11.0-rc.2+`, and `0.11.1` final, not `0.11.1-rc.2`. This rejected the host **before** local Source audit. The screen's `Source contract: failed` therefore did not prove a particular host source patch seam had changed.

The upstream GitHub release tag `v0.11.1-rc.2` was found, commit `a27d362`, with upstream published code for `agent_sdk/backends.py`, `dashboard/chat_turn/turn_dispatch_gates.py`, and `turn_stats.py` visible for review. This was not a complete independent audit of all upstream affected files.

## V16 changes

1. Accept `0.11.1-rc.N` / `0.11.1rcN` (N >= 1), without leading zeros, **only as candidates** for local contract preflight. Keep 0.11.2 and 0.12 blocked.
2. Update the adapter's self-protecting `semantic_candidate_regex` and release-check expected metadata in sync.
3. Keep 23 installed-source seam checks, imported symbol/marker checks, dashboard model picker contract checks and staged transformation/compile validation. No `allowed_crew_versions` or `verified_versions` entries were added: this is not an audit-pinned source.
4. Update the read-only preflight CLI error and regression tests. Preserve original release identity, provider configuration, and installed data.

## How to verify safely

- Update Kiro BYO App with V16, preserve existing provider configuration, do NOT choose `Uninstall and clear data`.
- Fully exit KiroCrew and its Gateway so new Python modules load.
- Open BYO → Settings → KiroCrew: Source audit should now proceed past the version gate. If installed Crew source/DOM differs, it **must** continue to show the specific contract mismatch and must not patch the host.
- Only run Install/Update after local Source audit passes. Then restart the Gateway again before declaring the integration active.
- Validate model lists and backend switching, Mate worker permissions, and an idle Tiga/Dyna Type switch.

## Validation and limits

See `/mnt/data/kiro_compat_v16` test logs. The source bundle of the user's exact Windows installation and the full upstream `v0.11.1-rc.2` source tree were not present locally. Passing simulated and project tests does not certify Windows, macOS or Linux operation. **No real Crew source was patched in this environment.**

## Deferred architecture work

Do not merge the DSH global Router + Worker orchestration into BYO until Crew Mate 2.0 integration and the new release compatibility are proven. Kiro BYO's Mode Router is model selection, not long-horizon global task orchestration; Crew is the appropriate owner of Mate/Worker lifecycle.

# Kiro BYO 0.3.0 — Cross-platform turn-timing correction (candidate)

## Scope and baseline
- Source baseline: V7 `0.3.0+23e873c142ca6ab9`, copied before modification.
- Targeted Crew interface family: 0.11.0-rc.1 and semantically checked 0.11.0-rc.2 through rc.5 and 0.11.0 stable. Future 0.12 must pass new independent source preflight.
- Products/platforms: Windows x64, macOS Intel/ARM64, Linux x86_64/ARM64, browser dashboard attached to Gateway. Shared Python monotonic-clock contract is OS-independent.
- **No Windows/macOS/Linux actual desktop end-to-end test** has been performed.

## Independently confirmed defect

`src/kiro_byo/crew_shared_patch.py::_patch_chat_runner` was tailored to a legacy Crew layout where a single `_run_chat` function held two `_turn_t0` assignments and an `EVENT_COMPLETE` duration calculation.

The official 0.11.0 source moved these three seams into `dashboard/chat_turn/turn_entry.py`, `turn_dispatch_gates.py`, and `turn_stats.py`. The 0.11 adapter did not list the latter two in the installation manifest. Therefore, the earlier patch could not ensure the 0.11 footer used the full Crew server Turn duration. At completion, upstream uses `usage.duration_ms` when nonzero, even if shorter than the Turn duration. The dispatch gates also reset the clock after setup.

References for direct source verification:
- https://github.com/kirodotdev/KiroCrew/blob/v0.11.0/src/kiro_crew/dashboard/chat_turn/turn_entry.py
- https://github.com/kirodotdev/KiroCrew/blob/v0.11.0/src/kiro_crew/dashboard/chat_turn/turn_dispatch_gates.py
- https://github.com/kirodotdev/KiroCrew/blob/v0.11.0/src/kiro_crew/dashboard/chat_turn/turn_stats.py

## Changes (minimal; no model-routing or safety-policy changes)

1. `src/kiro_byo/crew11_patch.py` adds **two exact 0.11 turn-owner patches**:
   - `turn_dispatch_gates.py`: keep early turn monotonic clock for BYO, leave clock-reset for other backends unchanged.
   - `turn_stats.py`: for BYO prefer elapsed Crew turn wall time rather than Provider's `usage.duration_ms`; other backends unchanged.
2. Both are registered as 0.11 generated targets, not as generic shared patches. The two owner files are added to `integration/kiro_crew/v0.11-rc1/manifest.json` and `compatibility.json` so a transactional integration install actually applies them. The original 21 RC1 pinned seams retain their pins; the two new owner seams are **not falsely claimed to be exact-hash pinned** and therefore require strict semantic-anchor validation and compiled, convergent transform preflight.
3. `integration_manager.py` supports precisely those two unpinned source-checked clock seams. Unknown paths and source shapes still fail closed; 0.10 package behavior remains unchanged.
4. `scripts/release_check.py` now enforces 21 original hash pins plus 2 source-contract-checked owner files. No architecture budget was widened.
5. New `tests/test_crew11_turn_wall_clock.py` validates 15s Turn vs 3s Provider duration, other backend isolation, zero-duration ACP, repeat-safe patching, fail-closed source changes, and manifest inclusion.

## Test evidence
- Targeted suite: **114 passed**, command `PYTHONPATH=src:. pytest -q tests/test_crew11_turn_wall_clock.py tests/test_crew11_candidate.py tests/test_crew_shared_transform.py tests/test_crew10_adapter.py tests/test_crew11_rc_family_preflight.py tests/test_app_package.py --disable-warnings --maxfail=1`.
- Release check: PASS.
- Architecture guard: PASS (54 / 54 budget).
- One broad single-process pytest run timed out after around 26% progress. Another partitioned run was interrupted after the first group passed 188 tests. Neither unfinished run counts as passing; test execution in this environment is not a substitute for OS acceptance.

## Important accuracy boundary

**Fixed:** The Crew server-side `turn_stats.elapsed_ms` footer and associated metric now include BYO's server-side turn setup and execution up to the completion event (on 0.11 source shapes matching the semantic patch). That is a real monotonic elapsed measurement; it is not the short Provider-reported duration.

**Not yet measured or asserted:** Physical user's click-to-last-painted-pixel time. A browser/client timer keyed to a unique Turn ID and confirmed final render is required for this independent metric. The server cannot infer the exact time of the user's click or last rendered frame. Do not label the corrected server figure as UI end-to-end latency; the difference is a valid further engineering task. Queue time before the Crew Turn begins is also not included.

**Do not conflate** Provider upstream_ms, BYO Runner total_ms, server Turn elapsed_ms, and true client UI latency. Neither past logs nor router feedback data was backfilled or modified. No artificial delay was introduced.

## Other known quality risks
- Version pins and semantic contract do not prove OS-specific binary equivalence on every Stable/Insider/Nightly release.
- Running/queued sessions, recovery and cancellation paths need multi-OS and host-client correlation tests. End-to-end UI clock instrumentation remains a separate implementation task.
- Build stays a testing candidate, not an OS-certified stable version.

## Validation instructions for OS operators
1. Back up BYO config, apply integration update transactionally, and fully restart the Crew Gateway (using the host's normal workflow).
2. Check installed `turn_dispatch_gates.py` and `turn_stats.py` contain the BYO markers; compare integration status to new build ID.
3. On Windows, macOS and Linux independently, run controlled simulated Provider requests with a deterministic delay in **pre-dispatch** and separately in **Provider**; record the client's stopwatch alongside the stored `turn_stats.elapsed_ms` and BYO `total_ms`.
4. Test cancellation, retries, long task, queued prompt, streamed output, mode changes, and resuming prior chats. Confirm no extra tool executions or rewritten historic rows.
5. Only claim click-to-final-UI accuracy once the client has paired Turn-ID send and post-render monotonic timestamps. Until then the display is server Turn duration.

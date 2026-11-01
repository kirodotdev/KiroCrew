# Kiro BYO 0.3.0 — Records Center Phase 1 (2026-10-31)

## Scope

Read-only Records Center over the existing three independent persisted ledgers. This is a compatibility candidate rather than an independently validated Windows/macOS/Linux release. The Agent Harness, Provider path, route scoring, permissions, and Durable Run state transitions are unchanged.

## Changes

- New `src/kiro_byo/record_center.py`: bounded, whitelist-only aggregation of retained Turn telemetry, Runtime Trace and recovery-relevant Run summaries; standardized `kind`, `timestamp`, correlation IDs, Mode/Type and model fields. The snapshot is **eventually consistent**, not a cross-file transaction.
- New `GET /records?limit=100&kind=...&mode=...`: read-only response with `summary`, `events`, `has_more`, `consistency`. Limit 1–200. Invalid kind/mode filter returns 400. No new deletion API.
- Runtime Trace `retained_snapshot()` reads the bounded retained window (the interactive `recent()` method still caps at 500). New `/records` API never returns Trace `content` or arbitrary `data`; it deliberately whitelists small metadata fields.
- Records page: actual unified overview timeline, four clearly named categories, separate retained Turn/routing/runtime counts, protected Run count, true request-history-only delete action. Existing request/routing/recovery detail views remain available.
- `records.center.*` keys for Chinese (Simplified/Traditional), English, Japanese and Korean; localized Records timestamps and numerals.
- Tests cover preservation of routing traces after request deletion, protection of active runs, no raw secret export, >500 retained Trace coverage, invalid filters, API response and locale key parity.

## Semantics & limits

- A Turn is an Agent execution summary, **not** necessarily one Provider API request. Provider request totals must not be inferred from Turn count.
- Route decisions are a subset of Runtime Trace *storage*, but the top-level display counts route events separately from **other** runtime events (no double counting).
- Counts describe currently retained source records, not lifetime totals. Run listing is capped at 500 and a saturated window is explicitly flagged. The unified timeline is capped at 100 in UI (API maximum 200); it is not a comprehensive export.
- A Turn, Trace and Run can briefly disagree while writers complete: no false cross-store exactly-once guarantee is made.
- `DELETE /diagnostics` continues to clear only Turn telemetry; trace and Durable Run remain separate. Requests list labels this as **Clear request history**. No new broad cleanup or hidden learning reset was added.
- Older rows without Turn ID / Run ID stay visible but are not given synthetic link IDs.

## Remaining phases

1. Read-only cursor pagination and stable filters (Mode, Session, time range); avoid replacing a bounded time line with a second datastore.
2. Cleanup preview API with explicit scopes and a single-lane irreversible confirmation; never allow regular history cleanup to purge active/unknown-side-effect Run state.
3. Provider round events and strict client-visible timing telemetry, once native Crew client callback integration is available.
4. Cross-platform desktop/browser real-UI acceptance and Stable/Insider/Nightly contract checks.

## Verification

- `PYTHONPATH=src:. python -m pytest -q --disable-warnings --maxfail=2`: **1340 passed, 1 skipped, 58 subtests passed in 73.93 s**, exit code 0. Captured in the evidence bundle.
- `node --check ui/index.mjs`, `python scripts/architecture_guard.py --check`, `python scripts/release_check.py`.
- Live Crew UI and live Provider/ACP sessions: not tested in this environment.

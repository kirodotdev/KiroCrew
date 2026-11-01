# Kiro BYO 0.2.5 — One-page Task Console integration patch

Target: KiroCrew 0.11.0-rc.1. Based on the previous mate backend fail-closed repair build. Version remains `0.2.5` and uses a fresh build ID.

## Frontend

- Single-page task console with Harness / native-sync status, native scheduled jobs, background model choice, and the most recent **recorded** BYO lightweight background execution.
- No duplicate Chat or Mate model chooser and no secondary Harness switching panel.
- 12-second Crew read refresh. Modifications are disabled if the active Harness is not BYO or Crew config fails to load.
- Model `Auto` remains a policy in the selector; the selected provider/model in the execution card is separate.
- Native cron script/command jobs have no AI model selector. Context preferences remain under each agent job's advanced disclosure.

## Backend

- `GET /tasks` is the authoritative single read for native schedule/background config, active Harness, model selection list and telemetry-only execution summary.
- Existing `PUT /tasks/{id}/model`, `PUT /tasks/background-model` edit native Crew state and reject edits while the active main Harness is official. Read-before-write conflict checks prevent stale selections from silently overriding newer native Crew changes. These compare-before-write checks are **best effort** for Crew's async Cron API, not an atomic Crew transaction.
- Existing Mate/Conductor/Worker routing protections remain intact.
- `is_background_role` telemetry boolean distinguishes Crew's `kirocrew-lite` agent from interactive turns even when a concrete model is pinned. Historical `background_auto` records remain recognised.
- Future internal exceptions store only `error_site` (file basename, line number, function name), not locals, prompts, headers, or secrets.

## Verification and limits

- Syntax and packaging checks performed; regression test results are reported outside this file.
- The historical `AttributeError` screenshot does not include a stack trace, so its root cause **has not been established or patched**. Reproduce a single low-cost background request after installing this build and inspect Records for the new error site; leave expensive Crew tasks stopped until proven safe.
- BYO request telemetry confirms which provider/model BYO routed; it does not independently verify official credit charges, or prove that every Cron/Worker path uses BYO.
- No Windows KiroCrew real-session E2E performed. Backup data and restart KiroCrew before verifying integration. Never use Uninstall & Clear Data as an update procedure.

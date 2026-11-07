---
cron: ops-mission-control/reconcile
schedule: "*/15 * * * *"
tier: on_shift
silent: true
---

# Reconcile the incident board

The scheduled cron runs `reconcile_cron.py:run` without a model turn. Its script
calls `ops_mission_control_api(method="POST", path="/reconcile")` once and reports
resolved incident IDs only when the board changes. An unchanged result skips the
tick silently. The app updates the Slack mirror.

For a manual check, call the same `/reconcile` tool once. The response contains
up to ten resolved incident IDs, the evidence for each, and failed source IDs.
It does not return the full signal list.

The `/reconcile` route is the enforcement point for scheduled resolution. It
requires either an explicit `ok` assertion for that signal or absence from a
successful, complete snapshot of that incident's own source. An errored,
suppressed, unknown, or incomplete source leaves the incident open. The webhook
spool is push-only (`snapshot=false`): its normal absence after claim never
proves recovery. A sender that disappears without a clear event leaves an open
incident for diagnosis; time alone does not turn uncertainty into recovery.

Investigation agents record their own diagnoses and decisions through
`/incident/transition` under the investigation SOP. The scheduled reconcile job
uses `/reconcile` for provider-state resolution.

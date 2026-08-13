---
description: On-call runbook and escalation procedures
always: false
---

# Oncall Runbook

## Severity Levels

- Sev-1: Customer-facing outage, escalate immediately
- Sev-2: Degraded service, page on-call lead
- Sev-2.5: Business-hours Sev-2, monitor during work hours
- Sev-3+: Track and fix in normal sprint

## Triage Steps

1. Check alarm tree for root cause
2. Search recent deployments for correlation
3. Check ticket history for known issues
4. Escalate if unresolved after 15 minutes

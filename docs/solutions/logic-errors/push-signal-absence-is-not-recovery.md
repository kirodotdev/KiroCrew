---
title: Push signal absence is not recovery evidence
date: 2026-11-07
category: logic-errors
module: Ops Mission Control
problem_type: logic_error
component: background_job
symptoms:
  - A claimed webhook incident is marked resolved while its stored signal is still firing.
  - A successful empty webhook poll is mistaken for a recovered provider state.
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [ops-mission-control, webhook, reconciliation, cron]
---

# Push signal absence is not recovery evidence

## Problem

Ops Mission Control's scheduled reconciliation could close an incident after a webhook signal disappeared from `/signals`. The incident remained firing at its sender, but `resolved` is terminal in the incident model. The observed case is [KiroCrew #32074](https://github.com/kirodotdev/KiroCrew/issues/32074).

## Root cause

The webhook adapter is a push spool. Its `poll()` peeks at queued signals, and dispatch acknowledges claimed signals. A healthy empty poll therefore says only that the spool is empty. It does not say the sender recovered. The adapter declares `is_snapshot = False` in `src/kiro_crew/apps/builtins/ops_mission_control/backend/providers/webhook.py`.

The old reconcile cron asked a model to read `/signals` and resolve incidents whose signals were no longer firing. That instruction had two failure paths: treating an empty push spool as a complete provider snapshot, and calling the generic `/incident/transition` endpoint without an evidence check. Adding more prose to the SOP could reduce mistakes but could not make that transition route an enforcement point.

## Solution

The source fix puts scheduled recovery decisions in `POST /reconcile`. It polls each configured source and uses health from **that same poll**. An incident can resolve only when its matching source and signal explicitly report `ok`, or when that signal is absent from a successful **complete snapshot**. An errored, unknown, suppressed, or incomplete source leaves the incident open. The response is compact and caps transitions at ten per tick.

The scheduled cron runs a native script that calls only `/reconcile`. The generic transition route remains available to investigators for their own recorded decisions. This keeps automated provider-state resolution on the evidence-bound route.

## Deployment boundary

An existing message cron does not become a script cron through manifest resync: `script` is create-only and clearing its old `message` is also pending in-place. After installing the new package, disable and re-enable Ops Mission Control through its supported app lifecycle, then arm the on-shift tier through `/rotation/arm`. Verify the stored `reconcile` job has a script and no message before treating the cutover as complete. The package diff alone does not prove a running installation migrated.

## Prevention

For every automated close, establish whether a provider poll is a complete snapshot or a push delivery. Base the transition on same-cycle provider evidence in app code, and verify the installed scheduler job actually runs the new execution path.

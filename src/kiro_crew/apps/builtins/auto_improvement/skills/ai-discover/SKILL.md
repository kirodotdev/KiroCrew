---
name: ai-discover
description: Parallel discovery of performance hotspots (perf track) and failure surfaces (bug track) for the auto-improvement loop. Fans out one subagent per hot-path area or failure surface; each returns ONE concrete, behavior-preserving fix candidate (perf) or a reproducing test plus fix (bug). Discovery only — no code changes and no verification happen here.
always: false
triggers: auto-improvement discovery, find hotspots, profile discovery, discover defects, improvement candidates
---

# ai-discover — the discovery step of the auto-improvement loop

This skill drives Phase A (discovery) of an auto-improvement cycle. It is
**discovery only**: it never applies a change, never runs the keep-or-revert A/B,
and never decides whether a finding is kept or drafted as a pull request. Those
are the spine's **deterministic Python** gate / keeper / pipeline, which no model
can argue past. That separation is the point — the measurement is the product.

## Perf track

1. Fan out one subagent per candidate hot-path *area*.
2. Each runs the profiler against a realistic workload and finds the single
   largest **behavior-preserving** win in its area.
3. Each returns exactly ONE candidate:
   `{target locus, signature, hypothesis, expected stage win, scenario}`.
4. Write all candidates to the discovery artifact the spine reads. The spine
   dedups by content fingerprint, implements, and verifies with a serial pinned
   A/B measurement. Discovery does none of that.

## Bug track (`kind="bug"`)

1. Fan out one subagent per failure *surface* — a risky or known-fragile path.
2. Each writes a **minimal deterministic reproducing test** that FAILS on the
   base commit (RED) and a fix that makes it PASS (GREEN) without regressing the
   rest of the suite.
3. Each returns `{target locus, reproducing test id + path, fix diff, blast
   radius, severity note}`. The spine's RED → GREEN → STAYGREEN gate is the
   verdict; there is no A/B or noise band for bug findings.

## Reporting a candidate honestly

State the expected win as a *hypothesis*, not a result. The spine measures it;
if your estimate was wrong the candidate is reverted and that is a normal,
useful outcome. A guess presented as a measurement is the failure mode this
whole app exists to prevent.

## What this skill never does

- Never edits the ruler, the measurement harness, the tests-of-record, or
  anything outside the active target profile's **edit allowlist**. Those paths
  are mechanically rejected, so an edit there wastes the whole cycle.
- Never publishes or merges a pull request. Survivors are drafted as GitHub
  draft PRs by the spine, and a human publishes them.
- Never fabricates a measured number. A fabricated win is the worst possible
  reward-hack, because it corrupts the record the loop reasons from.

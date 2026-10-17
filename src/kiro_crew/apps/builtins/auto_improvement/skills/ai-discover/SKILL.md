---
name: ai-discover
description: Read-only discovery step of the auto-improvement loop. One bounded agent turn per cycle reads the focus files (Read/Grep/Glob only, no shell, no subagents) and replies with a JSON array of testable surfaces {file, line, symbol, rule, message, hypothesis}; the spine authors fixes and verifies them. Discovery only — no code changes and no verification happen here.
always: false
triggers: auto-improvement discovery, find hotspots, profile discovery, discover defects, improvement candidates
---

# ai-discover — the discovery step of the auto-improvement loop

This skill drives Phase A (discovery) of an auto-improvement cycle. It is
**discovery only**: it never applies a change, never runs the keep-or-revert A/B,
and never decides whether a finding is kept or drafted as a pull request. Those
are the spine's **deterministic Python** gate / keeper / pipeline, which no model
can argue past. That separation is the point — the measurement is the product.

## How discovery actually runs

The spine runs discovery as ONE bounded agent turn per cycle
(`spine/agent_discovery.py` `discover_surfaces_via_agent`), not a fan-out. Do
NOT spawn subagents: the turn must end with the JSON array itself, and children
spawned from an unattended runner orphan and hang the run (see the
`SessionAgentRunner` notes in `spine/agent_runner.py`).

- Tools are Read, Grep and Glob only. There is no shell and no edit: discovery
  is read-only by capability, so you cannot run a profiler or write a test here.
- Read the priority slice you are given, stop after 2-3 solid findings or the
  read budget, and reply with ONLY a JSON array (at most the stated limit) of
  `{file, line, symbol, rule, message, hypothesis}` — `rule` is `AGENT`, or
  `COVERAGE` for a coverage gap. Reply `[]` when nothing is testable. The final
  reply IS the output; there is no separate artifact file.
- The same surface shape feeds both tracks. On the bug track the profile names
  the reproducing test path and `author_bug_fix` later writes the RED test and
  the fix in an isolated worktree; on the perf track `author_perf_fix` writes the
  edit. The spine dedups by `(kind, basename::symbol)` locus, then gates
  (RED -> GREEN -> STAYGREEN for bugs, serial pinned A/B for perf).

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

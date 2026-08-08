---
name: security-scan
description: Run a self-improving adversarial security scan of the KiroCrew codebase. Decomposes the scan into focused topics that run as parallel agents, validates findings with sandboxed proof-of-concept exploits against an isolated pod, and learns from the results. Use when the user asks to scan for security issues, run a security scan, check for vulnerabilities, or when the scheduled scan cron fires.
---

# Security Scan

Runs the Security Scanner app's scan pipeline. **Read `SECURITY_NOTES.md` in the
app root before doing anything** — the safety constraints there are mandatory.

## Pipeline

1. **Plan** — for each active topic (default: path-traversal, auth-bypass,
   prompt-injection), build a scan job = topic prompt + that topic's tagged
   knowledge slice from the knowledge store.
2. **Scan (parallel)** — dispatch one agent per topic via `spawn_run`. Each agent
   uses its own grep/glob/read tools to find and analyze relevant code — there is
   NO pre-analysis stage and NO curated file list. It returns structured findings
   (vuln, location `file:line`, severity, exploit suggestion).
3. **Collect** — dedupe findings across topics, persist to the app data dir.
4. **Validate (sandbox only)** — for HIGH/MEDIUM findings, generate a finding-bound
   PoC and run it ONLY against an isolated `kirocrew pod` (never the live gateway).
   Record EXPLOITED / BLOCKED / TIMEOUT / ERROR with scrubbed evidence.
5. **Learn** — confirmed exploit → new tagged pattern; failed-because-safe →
   suppression. Append-with-audit; never silently delete.
6. **Report** — notify the user ONLY on new actionable findings (dedup against
   what was already reported).

## Invocation

- Scheduled: the `security-scanner-scan` cron fires this skill for all active topics.
- On-demand: "scan now" from the app UI posts to a background slot that runs this.

## Hard rules

- Exploits run in the pod sandbox only. Refuse any target resolving to `:5476`.
- No destructive operations, no credential-file reads, no outbound network.
- Findings are never auto-filed as issues — report to the human, who decides.
- Scrub secrets from all captured evidence before persisting or displaying.

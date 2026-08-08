# Security Boundaries & Safety Defaults

This app **generates and executes adversarial exploit code**. Because it is a
security tool that attacks a target, the safety constraints below are load-bearing
— every later stage MUST uphold them. They are enforced in code (Stage 4/5), not
left to prompt discipline.

## ARCC governance status

The `security-assistance` skill mandates an ARCC governance search
(`search_arcc`) before building security-sensitive features. **The `arcc-governance`
MCP tool is NOT available in this environment** — `search_arcc` could not be
called (tool search returned no `search_arcc` provider on 2026-08-07/08).

Per that skill's "When ARCC Returns Little" guidance, we **note that we checked,
proceed with standard secure defaults, and do not invent policy**. The defaults
below are the conservative stance we adopt in ARCC's absence. If `arcc-governance`
becomes available, re-run the search and reconcile.

## Hard safety constraints (enforced in code)

1. **Exploit execution is sandbox-only.** Proof-of-concept scripts run ONLY against
   an isolated `kirocrew pod` instance (own port, own `KIROCREW_HOME`, no tunnel,
   `--no-crons`, cgroup memory/CPU caps). They MUST NEVER target the live gateway
   (`:5476`), production hosts, or any real cloud resource. The pod adapter refuses
   any target URL that resolves to `KIROCREW_POD_LIVE_PORT` / `:5476`.

2. **No outbound network from the app.** `permissions.network` is `false`. Generated
   exploit scripts run inside the pod's network boundary; the app itself makes no
   third-party requests. External report ingestion is file/paste only.

3. **Bounded exploit execution.** Every PoC runs under a wall-clock timeout, a
   captured-output size cap, and a working-directory jail. Runaway or
   resource-exhausting PoCs are killed, recorded as `TIMEOUT`/`ERROR`, and never
   retried automatically.

4. **No destructive operations.** PoCs are read/observe-oriented (prove access,
   prove a bypass). No `rm -rf`, no `DROP TABLE`, no credential-file reads
   (`~/.aws/*`, `~/.ssh/*`), no process kills by name. A confirmed-exploitable
   destructive class is reported as a finding, not demonstrated destructively.

5. **Secrets never surface.** Captured PoC evidence is scrubbed before persistence
   and display: credentials, tokens, and private keys are redacted; findings
   reference secrets by role/key name, never by value.

6. **Findings are never auto-filed.** No GitHub issue, no external post, no PR is
   created without explicit human confirmation. The scanner reports; the human acts.

7. **Knowledge store is append-with-audit.** Learned patterns and suppressions are
   added, never silently deleted; deletions require an explicit human action and are
   recorded in the store's activity log.

8. **Scans are read-only against the target's source.** Topic agents read code with
   grep/glob/read; they do not modify the codebase under scan.

## Target scoping

V1 scans **KiroCrew only**, using the pod infrastructure as the exploit sandbox.
The target-adapter interface (`lib/targets.py`) is deliberately small so a future
adapter can point the scanner at another codebase — but that generalization is
out of scope until there is a real second target.

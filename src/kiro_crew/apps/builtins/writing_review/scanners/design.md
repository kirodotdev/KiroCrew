# Design

You evaluate whether a technical design document is complete, rigorous, and gives the reader everything they need to evaluate the proposed solution.

This scanner runs only when the document type is "design document" or "investigation document". It supplements the universal scanners with checks specific to technical design completeness.

## Principles

This scanner draws from:
- Engineering design review standards (a design doc answers "what, why, how, what if")
- Architecture decision records (alternatives considered, tradeoffs explicit)
- Operational readiness (a design that can't be operated safely isn't ready)

## Severity Levels

- **high** — a critical section is missing entirely, or the reader cannot evaluate the design without information that's absent (no alternatives considered, no NFR section, no operational section, unresolved TBD on a critical element)
- **medium** — a section exists but lacks depth (strawman alternatives, NFR section missing key dimensions, operational section missing rollback, dependencies without owners)
- **low** — minor placeholder in a non-critical section with a clear owner; one-way/two-way door classification missing on a low-impact decision

Reserve "high" for gaps that would cause a reviewer to say "I can't approve this — I don't have enough information." This scanner does not evaluate whether the design is good — only whether it's complete enough to be reviewed.

## Rules

1. Clear problem statement with scope boundaries. The reader must understand what this design solves and what it explicitly doesn't.

   Before: "This document describes the new auth system."
   After: "This document designs the token validation rewrite for the login service. In scope: token refresh, session expiry, silent failure handling. Out of scope: user registration, SSO federation, password reset."

   Before: A design doc with no mention of what it doesn't cover.
   After: An explicit "Out of Scope" section or inline scope statements per section.

2. Non-functional requirements section exists and is complete. The design must have a section that names its latency, throughput, availability, and scale targets — even if those targets are "TBD pending load test."

   Before: A design doc with no mention of performance, availability, or scale expectations.
   After: A "Non-Functional Requirements" or "Targets" section listing: latency target, throughput target, availability target, scale ceiling.

   Whether those targets have actual numbers or are flagged as "TBD" is the evidence scanner's concern. This rule checks that the section exists and names the dimensions.

3. Alternatives considered with honest tradeoffs. A design that presents only one option gives the reviewer no basis for evaluating it.

   Before: "We chose PostgreSQL for the database."
   After: "We evaluated PostgreSQL, DynamoDB, and SQLite. PostgreSQL wins on query flexibility and our team's experience. DynamoDB wins on operational overhead (fully managed). SQLite wins on simplicity but doesn't support concurrent writes. We chose PostgreSQL because we need complex queries and the team already operates two Postgres instances."

   Each alternative should have at least one genuine advantage stated — otherwise it's a strawman.

4. Operational concerns addressed. A design that works technically but can't be monitored, debugged, recovered from, or maintained is incomplete.

   Questions the design should answer:
   - How do we know it's healthy? (monitoring)
   - How do we know it's broken? (alerting)
   - How do we fix it when it breaks? (recovery/rollback)
   - How do we deploy changes safely? (deployment strategy)
   - What happens when dependencies fail? (failure modes)
   - What does ongoing maintenance look like? (operational burden)

   Before: A design doc that describes the happy path in detail but has no section on failure modes or recovery.
   After: "Failure modes" or "Operational concerns" section covering at minimum: monitoring, alerting, rollback, dependency failure handling.

5. Dependencies and ownership clear. Every external system the design depends on should be named, and every new component should have an owner.

   Before: "The data will be stored securely and backed up regularly."
   After: "Data stored in PostgreSQL (owned by platform team, existing instance). Backups via pg_dump daily to S3 (owned by this team, new cron job). Encryption via KMS (existing key, managed by security team)."

6. Decisions are explicit and reversible where possible. Call out which decisions are one-way doors (hard to reverse) and which are two-way doors (easy to change later).

   Before: "We'll use event sourcing for the audit log."
   After: "We'll use event sourcing for the audit log. This is a one-way door — migrating away requires a full data migration. We're confident because: [reasons]."

   Before: "We'll deploy to eu-west-1."
   After: "We'll deploy to eu-west-1. This is a two-way door — redeploying to another region requires a config change and ~2 hours of migration."

7. No unresolved placeholders in a document presented for review. TBD, TODO, TK, WIP, and [placeholder] items must be resolved or explicitly called out as known gaps with an owner and timeline.

   Before: "Security approach: TBD"
   After: Either resolve it, or: "Security approach: Not yet designed. Owner: @security-team. Blocked on threat model completion (ETA: sprint 15)."

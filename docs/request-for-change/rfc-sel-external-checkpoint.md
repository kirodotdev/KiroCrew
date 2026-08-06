---
title: External Anchor for the SEL Audit-Log Hash Chain
status: draft
author: yogeshselvarajan
created: 2026-08-07
last-audited: 2026-08-07
audited-at: 429cbad8
doc-pr: 1886
implementation-prs: []
tracking-issues: [1881]
supersedes: []
superseded-by: []
---

# RFC: External Anchor for the SEL Audit-Log Hash Chain

## Summary

The Security Event Log (SEL, `sel.py`) is an append-only, HMAC-SHA256
hash-chained audit trail. It is genuinely tamper-evident against partial
edits: any rewritten or reordered entry breaks the chain, and
`verify_integrity()` detects it. But the HMAC signing key (`sel_hmac.key`)
lives in the same trust-root directory as the log it protects, both inside
`security._SENSITIVE_HOME_DIRS`. That protects the key from the agent, not
from a host administrator or anyone with owner-level filesystem access — such
an actor can read the key and re-sign a rewritten log into a chain that
verifies cleanly. This RFC proposes an external checkpoint mechanism,
periodically anchoring the chain's tip hash somewhere the SEL's own key
cannot reach, so a rewritten log can be detected against a record outside the
compromised host.

While designing this, testing the actual `prune()` / `verify_integrity()`
interaction turned up a second, separate, currently-live gap: `verify_integrity()`
already reports a false chain break on any log that has ever been pruned,
because `prune()` truncates the file without resetting the first surviving
entry's `prev_hash`. This is not hypothetical — `heartbeat.py` calls
`sel().prune()` automatically on every long-running instance, using the
default 365-day retention (`_RETENTION_DAYS`). Any operator running
`verify_integrity()` against a SEL log older than a year is already seeing a
spurious "break" today, unrelated to tampering. This RFC fixes that bug on
its own, inside `prune()`, with no dependency on the checkpoint mechanism —
an earlier draft coupled the two and got the fix wrong as a result (see
Design); the two problems turned out to need two independent fixes, not one
shared one.

## Motivation

**Current state.** `SecurityEventLog.log()` computes
`entry_hash = HMAC(key, canonical_json(entry without entry_hash))` chained
against the previous entry's hash, and `verify_integrity()` re-walks the file
recomputing the chain from `prev_hash=""`. The signing key is generated once
(`os.urandom(32)`, minimum 32 bytes enforced) and written to
`<config_dir>/sel_hmac.key`, protected by the same keystone mechanism
(`security._SENSITIVE_HOME_DIRS`) that makes `security_policy.json`
unreadable to the agent.

**Problem 1 — same-disk key.** An actor with owner-level filesystem access to
the host (not the sandboxed agent, but a real administrator, or an attacker
who has already escalated past the agent boundary) can read `sel_hmac.key`
and use it to re-sign an edited or fully replaced log. `verify_integrity()`
cannot distinguish this from a genuine, untampered log, because the only
thing it checks is internal self-consistency against a key that is not, in
this threat model, secret anymore.

**Problem 2 — pruning breaks the chain today, silently.** Verified directly
(see Design, "Confirmed behavior"): after `prune()` removes entries older
than the retention window, the first surviving entry's stored `prev_hash`
still points to the hash of a now-deleted predecessor. `verify_integrity()`
always starts assuming `prev_hash=""`, so it reports a break at entry 1 of
the pruned file — not because anything was tampered with, but because the
chain's genesis moved and nothing recorded that fact. This runs in
production today, automatically, on the heartbeat's periodic maintenance
tick (`heartbeat.py:186-190`), for every instance old enough to have entries
past `_RETENTION_DAYS` (365).

Neither of these is a new vulnerability being introduced; they are gaps
between what the audit trail is currently understood to guarantee and what
it actually guarantees. Tenet #1 ("every action is gated, auditable, and
reversible where it can be... security is the foundation, not a feature")
and Tenet #6 ("sensitive information must never spread... the right to
forget is built in", which pruning exists to serve) are both in real tension
with an audit mechanism that either cannot detect a genuine compromise, or
falsely flags its own routine retention behavior as one.

## Goals

- Let an operator detect, after the fact, whether their on-disk SEL log has
  been tampered with by an actor who also had access to the host's
  `sel_hmac.key` — the actual threat this RFC is scoped to.
- Do this without changing what gets logged, or requiring the operator to run
  any new always-on service.
- Fix `verify_integrity()` so a legitimate prune is never reported as a chain
  break, using a mechanism the external checkpoint can also rely on.

## Non-goals

- This does not make the SEL log tamper-*proof*, or provide third-party
  non-repudiation — an external verifier who does not trust the operator's
  checkpoint destination still has to trust that destination. It raises the
  bar from "same host" to "same host plus the checkpoint destination", not to
  zero-trust.
- This does not change what data is captured (tool-call / approval / denial
  metadata, never prompt or response text — unchanged).
- This does not attempt to encrypt the log or the key at rest; that is a
  different, orthogonal problem.
- This does not touch `commands` / `network.egress` / other governance
  scopes — that is already shipped, see PR #1884.

## Design

### Confirmed behavior (verified against `429cbad8`)

Ran directly against `SecurityEventLog`, not inferred from reading:

```
before prune: (5, 5)     # 5 total, 5 valid
removed: 2                # prune() removed the 2 oldest entries
after prune: (3, 2)       # 3 total, only 2 valid -- entry 1 now fails verify
```

`sel.py:747-776` (`verify_integrity`) always starts `prev_hash = ""`.
`sel.py:797-853` (`prune`) rewrites survivors verbatim, including their
original `prev_hash`, and never resets the first survivor's `prev_hash` to
`""`. `heartbeat.py:186-190` calls `sel().prune()` unconditionally on the
periodic maintenance tick, with no feature flag guarding it.

### Fixing the prune / verify interaction (self-contained, no checkpoint dependency)

An earlier draft of this RFC proposed fixing this by having `verify_integrity`
compare the first surviving entry's `prev_hash` against a checkpointed
`tip_hash` recorded before the prune. That does not work: `tip_hash` as
defined (`_last_hash`, the newest entry) is not the hash the first survivor's
`prev_hash` actually points at, which is the entry immediately before it in
the original chain — one of the entries about to be *removed*. In the
worked example above, survivor #1's `prev_hash` is hash(entry 2); the
pre-prune tip is hash(entry 5). They only coincide if the prune removes
everything. Caught in review before this was built; see Alternatives
considered for why a tip-hash checkpoint is not used at all.

The actual fix stays entirely inside `prune()`, needs no new file and no new
`verify_integrity` parameter: when entries are removed, reset the new first
survivor's `prev_hash` to `""` and recompute + re-sign its `entry_hash` with
the existing HMAC key, exactly as if it were a fresh genesis entry.
`verify_integrity()` is unchanged — zero-argument, `prev_hash=""` assumption
intact — because after this fix that assumption is true again for the
pruned file. `prune()` already reads and rewrites every surviving line, so
re-signing one extra line is a small, local addition to a loop that already
exists.

### Checkpoint mechanism (external anchor, decoupled from the prune fix)

This part of the RFC exists solely for Problem 1 (the same-disk key), not
for Problem 2 (now fixed above without it). Add a
`SecurityEventLog.checkpoint()` method that:

1. Flushes pending writes, mirroring the existing pattern in
   `verify_integrity` / `recent` / `prune`.
2. Records `{"tip_hash": <current _last_hash>, "entry_count": <total lines>,
   "checkpointed_at": <iso timestamp>}` as a single line appended to a new
   file, `sel_checkpoints.jsonl`, in the same directory — the local record of
   what was last anchored, kept separate from `security_events.jsonl` so the
   checkpoint history survives a prune of the main log.
3. Delegates actually getting that record off-host to the caller. This RFC
   does not add a new outbound network call inside `sel.py` itself (that
   would need its own governance scope and threat model); instead,
   `checkpoint()` returns the record, and a zero-LLM script cron job (the
   same pattern PR #1883 already hardened for fire-time governance) is the
   reference way an operator wires it to an external destination: S3 with
   object lock, a separate syslog/SIEM target, or simply a different host
   reachable over SSH. This keeps `sel.py` free of a new egress dependency
   and reuses infrastructure the project already has and already governs.

### Verifier

A small standalone script (`scripts/sel_verify_external.py`, stdlib only, no
new runtime dependency) takes a local `security_events.jsonl` plus a
`sel_checkpoints.jsonl` retrieved from wherever the operator anchored it, and
reports whether the local log's tip hash and history are consistent with the
external checkpoint history. Intentionally offline and simple: the goal is a
tool an operator, or an auditor who does not run Kiro Crew at all, can run
against two files, not a service.

## Migration plan

**Phase A -- re-sign the first survivor on prune, no new file or parameter.**
Exit criteria: `prune()` no longer produces a false chain break on an
untampered log (a regression test reproducing the worked example above,
asserting `verify_integrity()` returns all-valid on the post-prune log with
zero arguments); every other `verify_integrity()` behavior is byte-for-byte
unchanged (regression test). No new config, no new file, no new network
code, no new parameter.

**Phase B -- `checkpoint()` + `sel_checkpoints.jsonl` + reference cron job +
`scripts/sel_verify_external.py`.** Exit
criteria: a documented, copy-editable script cron (matching the pattern in
`docs/guides/assets/`, alongside PR #1884's example) that pushes
`sel_checkpoints.jsonl` to an operator-chosen destination; the verifier
script correctly flags a hand-edited log against a real checkpoint file in a
test fixture.

**Phase C (open question, may not be needed) -- built-in destinations.**
Whether Kiro Crew ships first-class support for a specific checkpoint
destination (S3, a specific SIEM) versus leaving it entirely to the
reference cron job in Phase B. Blocked on the open question below.

Phase A and B are fully independent now, not just independently shippable:
A no longer produces any artifact B depends on. A is useful entirely on its
own (it fixes a real, currently-live false positive); B has its own reason
to exist (Problem 1) regardless of whether B ever ships.

## Backward compatibility

`verify_integrity()` keeps its current zero-argument signature and behavior
in both phases — Phase A changes what `prune()` writes, not
`verify_integrity()`'s interface. `checkpoint()` and `sel_checkpoints.jsonl`
(Phase B only) are new — nothing existing reads or writes that filename
today (confirmed: no hits for `sel_checkpoints` anywhere in `src/` or
`test/` at `429cbad8`). No config schema change in either phase.

## Security considerations

- The checkpoint destination (Phase B) becomes a new trust dependency. If it
  lives on the same host, Phase B provides no improvement over today — the
  whole point is that it must be off-host, and that responsibility is
  explicitly the operator's, via the reference-cron design, not something
  enforceable from inside `sel.py`.
- `sel_checkpoints.jsonl` should join `security._SENSITIVE_HOME_DIRS`
  (decided here, not deferred to implementation review): it holds hashes and
  counts, never event content, but an agent that could rewrite it locally,
  even without the HMAC key, could desync the local record from the external
  one and defeat the anchor silently.
- Phase B does not defend against an attacker who compromises the host
  before the first checkpoint ever runs — there is nothing to anchor against
  yet. Same limitation any checkpoint-based scheme has; noted so it is not
  assumed away.
- Problem 1's threat actor (a host administrator, or an attacker who has
  already escalated past the agent boundary) sits outside this project's
  stated trust boundary of a single OS user. Phase A stands on its own
  regardless (it fixes a bug, not a threat-model gap), but Phase B is a
  deliberate decision to extend the audit story past that boundary, and
  should be an explicit maintainer call, not something that ships by default
  because it happened to be proposed alongside the bug fix.

## Alternatives considered

- **Checkpoint-based prune fix (the original draft of this RFC).**
  `verify_integrity(since_checkpoint=tip_hash)` comparing against a
  checkpointed tip hash. Superseded: does not work as specified (see
  Design), and even corrected to use the real cut-point hash, it would still
  add a new file and a new parameter to fix a bug that has a strictly
  simpler, fully local fix. Re-signing in `prune()` is chosen instead.
- **Asymmetric signing (private key to sign, public key to verify) instead of
  HMAC, for Phase B.** Would let a third party verify without any shared
  secret, a stronger guarantee than Phase B's. Rejected for now: a bigger
  change (key generation / rotation / distribution story). Worth its own
  future RFC if Phase B's checkpoint approach turns out insufficient.
- **Cloud KMS-backed signing, for Phase B.** Same reasoning — real,
  stronger, but a bigger lift (a new required external dependency for every
  install, including the fully local / offline use case this project
  explicitly supports).

## Open questions

- Should Phase B happen at all, given Problem 1's threat actor sits outside
  the stated single-OS-user trust boundary? Phase A does not depend on the
  answer. Deliberately left to maintainer judgment rather than assumed.
- Should Phase B's reference destination be S3-specific (closest to how the
  rest of the project already talks about AWS), or transport-agnostic from
  the start? Leaning transport-agnostic, since a cron job that shells out is
  trivially retargetable, but open to maintainer input.
- Does `sel_checkpoints.jsonl` need its own retention / pruning policy, or
  does it stay unbounded (it is much smaller than the main log — one line
  per checkpoint, not per event)? Phase A can ship with "unbounded, revisit
  if it matters" and note it here rather than block on it.
- Phase C is explicitly unscoped pending feedback on whether Phase B alone is
  sufficient for real use, or whether a built-in destination is worth the
  added maintenance surface.

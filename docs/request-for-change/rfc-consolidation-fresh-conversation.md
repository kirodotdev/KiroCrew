---
title: Every global history-consolidation job starts on an empty conversation
status: in-progress
author: chenmingwei23
created: 2026-10-19
last-audited: 2026-10-19
audited-at: 5d1a459f2a
doc-pr:
implementation-prs: [22207]
tracking-issues: [20463]
supersedes: []
superseded-by: []
---

# RFC: Every global history-consolidation job starts on an empty conversation

- Status: in-progress. The implementation is [#22207](https://github.com/kirodotdev/KiroCrew/pull/22207).
- Tracks [#20463](https://github.com/kirodotdev/KiroCrew/issues/20463).

## Problem

History consolidation fails for every user once enough jobs have run since the
gateway started. The failure depends on the model: a small-window model refuses
the prompt as too long, and a 1M-window model is throttled with HTTP 429. Either
way memories stop being extracted.

The cause is that the conversation is never cleared. `HistoryConsolidator._call_llm`
sends every global (non-member) job through `background_turn(session_key="_consolidate")`,
so every job is one more turn in the same kiro-cli conversation. `recycle_background`
replaces it only at 70% context or 40 prompts. One job is about 80 KB (about 65 KB
of current semantic memory plus about 10 KB of transcript), so a conversation
stacks 40 to 63 jobs, 7.7 to 11.5 MB, before it is replaced. Job N's request
carries the text of jobs 1 to N-1, and the conversation's backend record stays on
disk after it is replaced (on one developer host, 2436 records held 23.3 GB).

## Proposal (option A)

Keep the one `_consolidate` session and its semaphore, which already queues jobs
one at a time. After every job, success or failure, and while the job still holds
the semaphore, `background_turn(fresh_conversation=True)` starts a fresh
conversation on the same running process with `provider.new_conversation()`,
the reset a warm workflow-pool worker uses between tasks.

`new_conversation()` opens the new session before it tears the old one down, and
the old session's teardown deletes that one session's backend record: KAS
`_kiro/session/delete` removes its `sess_<id>` record, and kiro-cli terminate is
followed by the guarded unlink of `sessions/cli/<sid>.json(+.jsonl)`. Nothing else
is deleted.

If the provider has no `new_conversation()` or it raises (a backend outside
`ACP_BACKENDS_SESSION_EVICTION`, a dead runtime, a model that cannot be re-applied),
the key is `reset` with `ends_conversation=True`, the same fallback
`WorkerPool.reset` uses. `_consolidate` is stateless, so the next job starts on a
new process with no history. One INFO line per job names the path taken. The
context-driven recycle does not run for this key, because the context is empty
after every job. The consolidation failure log names the served model and the
prompt size.

Member consolidation (`memory-consolidation:<store>:<id>`), `_bg` and user
sessions are unchanged.

## Cost

The fresh conversation is a `session/new` on a running runtime: p50 0.15 s on a
dev host (15 samples), against p50 1.48 s / p95 2.05 s for a kirocrew-lite cold
start. The peak job rate seen is 9 jobs in 60 s and 56 in 10 min (1437 jobs on
2026-10-19). The job count itself is high and is a separate follow-up.

## Alternatives considered

| Option | What it is | Assessment |
|---|---|---|
| A | Fresh conversation on the same runtime after each job | Chosen. Fixes the cause directly, keeps the existing queue, one runtime, no new key or lock. |
| B | One new session per job under its own key, deleted after the job | Rejected: per-job keys lose the shared semaphore, so it needs a new global lock and a new key shape every classifier must learn, and each job pays a cold start. |
| C | Ephemeral session on the shared `_bg` runtime | Rejected: puts consolidation back behind `_bg`'s semaphore and its short metadata jobs. |
| D | Lower the recycle threshold | Rejected: jobs still stack, only fewer of them. |
| E | Fewer jobs | Worth doing, but it does not stop stacking. A follow-up. |

## Questions for reviewers

1. Is `reset` the right fallback for a backend without `new_conversation()`, or should such a backend keep the recycle?
2. Should member consolidation adopt the same reset in a later change?

## Rollout

No flag: the old shape stacks jobs on every backend. Records left on disk by
earlier conversations are not touched; deleting them is a separate, opt-in cleanup.

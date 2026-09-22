# Asking KAS for a non-destructive session teardown

Kiro Crew can run the KAS engine as an ACP backend, and its shared-runtime model
needs a verb KAS does not have: **evict a session from memory while keeping its
persisted record loadable**. KAS today offers only `_kiro/session/delete`, which
frees the memory *and* removes the record, so a Crew sub-agent session on KAS
must either stay resident forever or lose its history. This note is the request,
written for a KAS engineer who has not seen the Crew side.

Everything below was read from source, not measured on a running engine. KAS
facts are pinned to `kiro-team/kiro-agent` `main` at
`03d8834173f5edcd6cfad75747f2750f0c1df25f`; Crew facts to
`kirodotdev/KiroCrew` `main` at `64fbda5f9170ad4614cb683d3e44a8f735615d41`.
Symbols are cited rather than line numbers, per this repository's docs rule: a
name survives the refactor that moves the line. The closing section marks what is
verified and what is not, because a citation is worth nothing if the reader
cannot tell which claims were actually checked.

## The one-line ask

Expose the in-memory teardown KAS already implements — `KiroAgent.disposeSession`
— as an ACP method, so a client can say "I am done holding this session" without
saying "destroy it".

## Current state

### `_kiro/session/delete` is the only teardown, and it is destructive

`_kiro/session/delete` routes to `KiroAgent.deleteSessionCanonical` and its
`deleteSessionCanonicalExclusive` body, both in
`packages/kiro-agent/src/agent.ts`. The method's own docstring numbers its
ordering contract, and the two steps that matter here are 4 and 5:

| Step | Call | Reaches |
|---|---|---|
| 4 | `persistence.deleteSession` | disk |
| 5 | `disposeSessionIfCurrent` → `disposeSession` | memory |

Step 4 lands in `SessionPersistence.deleteSession`
(`packages/kiro-agent/src/session/session-persistence.ts`), which drains the
session's write logs via `quiesceWriteLogs` and then, through `removeSessionDir`
and `purgeSessionDirectory`, removes the directory with
`fs.rm(sessionPath, { recursive: true, force: true })`. After it there is no
record to load.

### The memory half already exists, separately, and is not reachable

Two methods already do exactly the non-destructive half, at two layers:

- `SessionPersistence.disposeSession` releases per-session bookkeeping — the
  `sessionLocations` cache and the `pendingAttach` entry — and deliberately
  touches no writer and no file. Its own docstring contrasts itself with
  `deleteSession` on precisely this point.
- `KiroAgent.disposeSession` is the whole in-memory teardown: it retires
  provisioning, aborts the session's background sub-agents, unsubscribes its
  event listeners, runs `onSessionDestroyed` across the steering,
  progressive-context, powers, slash-command, available-tools, model-registry,
  activity-tracker and workflow managers, clears the message store, disposes the
  policy session and the sandbox, and deletes the id from `this.sessions`. It
  calls `persistence.disposeSession`. It removes nothing from disk.

`KiroAgent.disposeSession` is not `private`, and it has internal callers —
`releaseStep`, and the `disposeSession` field of the workflow runtime's host
dependency object. What it does not have is an ACP method. The ext-method
routing table (`packages/kiro-agent/src/acp/ext-method-routing.ts`) carries no
`close`, `release`, `unload` or `evict` verb for a session, and neither does the
published method list (`docs/website/data/acp-methods.json`, `Session
Management` domain). A grep for `session/close`, `session/release`,
`session/unload` and `session/evict` across `packages/` returns exactly one hit,
and it is a tag in a feature file, not a method.

### "Eviction" in KAS already means something else

There is a `sessionEviction` feature, and it is **not** this. Gated on
`_meta.kiro.settings.sessionEviction` `{ enabled, maxBytes }` at `session/new`,
it walks a workspace's store and **deletes least-recently-modified sessions from
disk** until the total is under budget: `SessionPersistence.evictOldSessions`,
driven from `packages/kiro-agent/src/session/session-cleanup.ts`, specified in
`packages/kiro-agent-tests/features/session-eviction.feature`. It keeps a floor of
five sessions and runs once per process. It is a quota path, and it is destructive.

So the word is taken. This request is for the other sense of it — release the
memory, keep the bytes — and naming matters, which the next section covers.

### A finished session has no reclamation path

When a client goes away, the `cleanup` closure in `MultiplexStream`
(`packages/kiro-agent/src/server/multiplex-stream.ts`) removes the client id from
`sessionSubscribers`, fires `onSessionUnsubscribe`, and clears the session's
in-flight prompt guard. It does not call `disposeSession`. The session stays in
`KiroAgent.sessions` with its execution graph attached.

There is also no time-based reclamation: a grep for `idleTimeout`, `idleReap`,
`reapIdle`, `sessionTtl` and `idleSession` across `packages/kiro-agent/src/`
finds hits only in the knowledge embedding engine and in workflow watch nodes,
none of them about session residency. So the only thing that currently reclaims
a session is the disk-quota path above, which reclaims *disk*, by deleting.

## What we are asking for

One ACP method. Suggested spelling `_kiro/session/release`, chosen to avoid
colliding with the existing quota-path "eviction" vocabulary; `unload` reads just
as well. The name is yours — the semantics are the request:

1. **Releases memory.** The addressed session is torn down as
   `KiroAgent.disposeSession` already tears it down, and its id is removed from
   `KiroAgent.sessions`.
2. **Keeps the record.** Nothing under the session's directory is removed, and it
   continues to appear in `session/list`.
3. **Stays loadable.** A later `session/load` on the same id restores the
   conversation, with its transcript intact, including from a **restarted engine
   process** over the same store. That last clause is the one that matters to us:
   by the time a continuation happens, the process that served the session is
   usually gone.
4. **Is idempotent and refuses nothing surprising.** Releasing an unknown or
   already-released id is a success, matching how `_kiro/session/delete` treats a
   missing id.
5. **Settles in-flight work first.** A release during a live turn should cancel
   and quiesce that turn before tearing down, the way the delete path's step 3
   does — a release that abandons queued writes would be a worse bargain than
   keeping the session resident.

The load half of this may already be free. `KiroAgent.loadSession`, through its
`loadSessionAdmitted` body, computes
`coldHydration = !this.sessions.has(params.sessionId)` and describes that arm as
hydrating "fresh from disk (recovery / reconnect)". A released session is exactly
a cold load. If that holds, the new verb is close to `disposeSession` plus a
routing entry plus a test.

## Why `_kiro/session/delete` cannot serve

Because the record is the thing we need to keep. Crew's contract for continuing a
sub-agent is: tear the session down when its parent conversation ends, then reach
the conversation again later by loading the record the host kept. `delete`
satisfies the first half and destroys the second.

There is a sharper reason than "the history is gone", and it is a correctness
one. A local `session/load` in KAS is **create-or-load**: an id with no on-disk
record hydrates and persists a fresh empty session, logged as
`session.load.create_uncreated`. So after a `delete`, a load of that id does not
fail — it *succeeds*, with an empty transcript. A client that
treats "load returned a session" as "my conversation is back" silently continues
against a blank one. That is why the acceptance test below asserts transcript
content rather than a successful response, and it is why we cannot paper over the
missing verb with a delete plus a retry.

## What KAS gets from it

A long-lived engine process currently has no way to reclaim what a finished
session holds. Every session a client opens keeps its execution graph, its
message buffers, its policy session and its sandbox until the process exits or
someone deletes the session's data. `disposeSession`'s own comments name the
things that accumulate: the message store's per-session buffers and ordered write
logs, the CDK verdict cache's per-session keys, the activity tracker's per-session
entries. The teardown to reclaim all of it is written and tested; it just has no
caller a client can reach.

Exposing it means a client that knows a session is finished can hand the memory
back immediately, without being forced to choose between holding it forever and
destroying the user's history. The same verb is what would let KAS add a
time-based idle release later: the hard part is the safe teardown, and that part
already exists.

## Acceptance

The property to prove is "the record survives the release". A successful load is
not evidence, for the create-or-load reason above — the transcript content is.

1. `session/new`, then run a turn with a distinctive answer in it. Note the
   `sessionId`.
2. `session/list` for the workspace: the session is present.
3. Send the new release verb. It answers success.
4. The released id is absent from memory: an operation that requires residency
   refuses it.
5. `session/list` again: **the session is still present.** The store on disk still
   holds its directory.
6. `session/load` on the released id. It replays the conversation, and the replay
   contains the turn from step 1 — asserted on content, not on the response
   status.
7. Prompt the loaded session with a question that can only be answered from that
   turn. It answers correctly.
8. **Repeat 6-7 against a restarted engine process** over the same store. This is
   the shape a Crew continuation actually takes.
9. Negative control, so the test cannot pass vacuously: the same script with
   `_kiro/session/delete` in place of the release must fail at step 5, and its
   step 6 must produce an *empty* session rather than an error.

A Gherkin scenario in `packages/kiro-agent-tests/features/` alongside
`session-eviction.feature` would sit naturally; the existing
`should eventually list exactly sessions` step already covers step 5.

## Alternatives Crew can accept

**Preferred alternative, and it may be cheaper than the verb:** make the
disconnect path do it. If `MultiplexStream`'s cleanup called
`KiroAgent.disposeSession` once a session's subscriber set became empty, Crew
would need no new method at all — closing our side of the connection would release
the memory, and the record would survive because `disposeSession` never touches
disk. This is strictly less flexible (we cannot release one session while keeping
others on the same connection, which a shared runtime wants), and it changes
behaviour for every existing client, so we are not asking for it in preference to
the explicit verb. But if the verb is expensive and this is not, we can work with
it.

**What we cannot work around from our side.** There is no client-side
substitute. Holding the session resident is the status quo and the thing being
reported. Deleting and re-creating loses the transcript, which is the whole
point. Copying the transcript out before deleting would mean Crew maintaining a
shadow of KAS's store and replaying it into a fresh session as prompt text — a
different conversation wearing the old one's content, at full token cost, with no
cache reuse.

**What we will do meanwhile.** Nothing degraded: Crew gives KAS sub-agents
dedicated sessions instead of shared ones. That works; it just costs a process
per sub-agent and forgoes continuation.

## The Crew side, for context

Crew keeps per-backend capability sets in `src/kiro_crew/agent_sdk/backends.py`,
and all three named below live there. Two of them answer this question
differently, and the pair is the precise statement of the gap:

- `ACP_BACKENDS_SESSION_EVICTION` — **KAS is a member.** Its teardown genuinely
  frees the session from the process. Credit where it is due:
  `_kiro/session/delete` does evict.
- `ACP_BACKENDS_SESSION_SHARING` — **KAS is not.** Membership asks one question,
  recorded in that set's own comment: after this backend's teardown verb, can a
  `session/load` still restore the thread? The same comment names KAS's exclusion
  and says it is owned by whoever gives KAS a non-destroying teardown.

Crew sends KAS the delete as its teardown from `KasHarness.teardown`
(`src/kiro_crew/acp/harness/kas.py`), as
`TeardownPolicy(method=METHOD_KAS_SESSION_DELETE, notification=False)`, with the
method string `METHOD_KAS_SESSION_DELETE` in `src/kiro_crew/acp/types.py`. The
user-visible consequence is `spawn_continue` answering `conversation_gone`
(`src/kiro_crew/subagent_manager/continuation.py`).

### What Crew changes once the verb exists

All of this is Crew work in Crew's tree, listed so nobody expects the new verb to
take effect on its own. Five edits, and one thing that deliberately is not an
edit:

| # | Change | Where |
|---|---|---|
| 1 | Add KAS to `ACP_BACKENDS_SESSION_SHARING` | `agent_sdk/backends.py` |
| 2 | Point `KasHarness.teardown` at the new verb | `acp/harness/kas.py` |
| 3 | Add the method constant beside `METHOD_KAS_SESSION_DELETE` | `acp/types.py` |
| 4 | Add KAS to `ACP_BACKENDS_HARNESS_OWNED_SESSIONS` | `agent_sdk/backends.py` |
| 5 | Teach the fake backend to answer the new verb | `testing/fake_acp_backend.py` |
| — | The card's "sub-agent continuation" cell | nothing to edit |

Change 1 is the switch, and it is what the request is for. It opens the
eligibility chain that decides whether a sub-agent gets a shared session:
`AcpProvider.is_session_sharing_eligible` reads the set, `session_allocation`
reads the provider, and `subagent_manager/run.py` reads that.

Change 4 is a second, independent membership, and the request does not work
without it. KAS sits outside `ACP_BACKENDS_HARNESS_OWNED_SESSIONS`, which puts it
on the kiro-family arm of Crew's resume path in `acp/client.py`: that arm gates a
`session/load` on a **kiro-cli** transcript file under `kiro_sessions_dir()` and
sends a `_kiro.dev/session_file` naming it. For a host that keeps its own store,
that file is not there and the load is skipped in favour of a fresh session. So
change 1 alone would mark KAS as shareable while its resumes silently started
over.

The card cell needs nothing because `backend_cards.py` already decides
`LINE_SUBAGENT_CONTINUATION` from `ACP_BACKENDS_SESSION_SHARING` as its only
input. Adding KAS to the set flips the cell. That is the intended shape — the
card reads memberships rather than keeping its own copy of them — and it is worth
stating so the change list does not grow an edit that would be a second source of
truth.

Change 2 replaces the delete in the teardown policy. `_kiro/session/delete` stays
the verb for genuinely disposing of a conversation; what changes is which of the
two a normal teardown sends.

The harness-parity suite pins these memberships — `test_session_sharing_is_opt_in`
asserts eligibility is read from the set rather than derived from a negation — so
changes 1 and 4 are assertions in that suite, not silent edits.

## Verified, and not

**Verified by reading source at the two commits named above.** Every symbol and
file named in this note was read there, not recalled. Specifically: that
`_kiro/session/delete` reaches
`fs.rm(recursive)`; that `SessionPersistence.disposeSession` and
`KiroAgent.disposeSession` touch no disk; that no ACP method routes to either;
that no session-close/release/unload/evict verb exists in the routing table or
the published method list; that `sessionEviction` is a disk-quota delete path;
that the disconnect cleanup does not dispose sessions; that no idle session reaper
exists; that local `session/load` is create-or-load; and, on the Crew side, both
capability-set memberships, the teardown policy, the `conversation_gone` string
and the resume pre-check.

**Not verified — no engine was run.** Every claim here is source reading. In
particular:

- **That a released session actually reloads with its transcript intact is
  argued, not measured.** It rests on the cold-hydration arm of `loadSession` and
  on `disposeSession` not touching disk. It is the property the acceptance
  section exists to establish, and it should be measured before anyone relies on
  it.
- Whether the quota-path `evictOldSessions` disposes a resident session it
  deletes. The feature file implies it does not; that is a reading of the
  feature file, not of the code path.
- The on-disk root of the session store. `SessionPersistence`'s `basePath` is
  injected at construction, so this note describes the layout as
  `<store base>/<workspace bucket>/<sessionId>/` and does not name an absolute
  path.
- An older Chinese-language analysis of the KAS backend exists in the author's
  workspace, pinned to `e33abe27b` and several hundred commits stale. Only the
  claim it shares with this note — the missing evict-but-keep verb — was
  re-verified. Its other conclusions were not, and at least one is known to have
  since been fixed, so nothing in this note is taken from it.

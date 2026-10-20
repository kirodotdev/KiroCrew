---
title: Opt-in memory sharing between connected Kiro Crew instances
status: draft
author: djman-aws
created: 2026-10-16
last-audited: 2026-10-16
audited-at: 766c658641985e58b91752979dac229f88794b4d
doc-pr: null
implementation-prs: []
tracking-issues: [18593]
supersedes: []
superseded-by: []
---

# RFC: Opt-in memory sharing between connected Kiro Crew instances

**Status:** Proposal for maintainer review. This document requests decisions
on scope, ownership, and memory semantics for [#18593](https://github.com/kirodotdev/KiroCrew/issues/18593).
It does not record acceptance or an implementation assignment.

## Summary

Let one person share selected semantic and episodic memories between their
laptop and development machines. Sharing is off until the owner chooses the
participating machines and content, reviews a preview, and enables it.
One coordinating Kiro Crew instance computes changes; each receiving instance
authorizes and applies its own writes.

Kiro Crew should own the memory contract, authorization, reconciliation state,
and write results. The controls belong in Kiro Crew's Remote Crew experience
and should use the project's approved app and dashboard extension boundaries.
Maintainers must confirm their placement and the sharing contract before
implementation.

## Motivation and current state

Switching machines also switches the memories available to an agent. A user
may want a preference learned on the laptop available on a development machine
without copying conversations or sharing every store.

Related proposals include [#14969](https://github.com/kirodotdev/KiroCrew/pull/14969)
for curated project-lesson replication between isolated crews and
[#8077](https://github.com/kirodotdev/KiroCrew/pull/8077) for planning moves of
sessions, schedules, and task runs. This RFC covers selected default-memory
sharing across one owner's machines. Both proposals remain open drafts;
neither supplies an accepted implementation dependency.

The following baseline was checked at the commit in the front matter:

| Existing behavior | Consequence for this proposal |
|---|---|
| The [Remote Crew sidebar RFC](rfc-remote-crew-sidebar.md) keeps sessions and transcripts on their owning peer and excludes memory synchronization. | Memory sharing needs its own decision. Opening a remote chat keeps its existing ownership model. |
| The [peer proxy](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/dashboard/handlers_instances.py#L1808) allows chat and stream paths. The [capability reader](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/instances/ssh_tunnel_manager.py#L280) has a separate, closed set of reads. | An existing connection supplies transport, not permission or an API for sharing memory. |
| [HTTP memory import](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/dashboard/handlers/memory.py#L2165) runs the owner and session write gates. [Bulk import](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/vector_memory_runtime/bulk_ingest.py#L163) reports aggregate semantic, episodic, and skipped counts. | Preserve authorization and store validation. Aggregate counts cannot identify which replicated records committed. |
| [CLI import](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/cli_commands.py#L4153) refuses V2 destinations because it cannot preserve their facets. | The existing exporter/importer is not a lossless transport for every memory store. |
| The local [memory seed operation](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/dashboard/handlers/memory_member.py#L345) requires owner authorization and selected items, and distinguishes per-item outcomes and uncertain commits. | Reuse this approach to consent and reporting where appropriate. Its local V2 destination contract does not itself authorize remote sharing. |
| [Memory edit previews](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/memory_edit.py#L622) and [apply](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/memory_edit.py#L772) already use signed previews, record revisions, stale-selection checks, and an atomic batch receipt. | Assess reuse before creating parallel machinery. These edit existing records; their preview-bound receipt lasts 15 minutes and supplies neither remote authorization nor per-record transfer receipts. |
| [Instance registration](https://github.com/kirodotdev/KiroCrew/blob/766c658641985e58b91752979dac229f88794b4d/src/kiro_crew/instances/registry.py#L921) derives a local identifier from a display name. | A connection label is insufficient evidence that a returning endpoint is the same installation and store. |

## Goals

- Share semantic facts/preferences and optional episodic memories from eligible
  default stores between explicitly selected machines belonging to one owner.
- Preview the initial transfer and expose manual run, pause, schedule, and
  per-machine status.
- Preserve receiving-store validation, explicit-user precedence, and local
  deletions; report refusals and incomplete work.
- Recover from offline machines, interrupted runs, and retries without treating
  copied values as newly authored edits.

## Non-goals

- Session transfer or continuation, transcript archives, Markdown memory files,
  named/private/V2 stores, skills, configuration, credentials, and cron definitions.
- Team sharing, multiple coordinating instances for the same group, or cloud storage.
- Propagating a deletion to every machine.

## Design

### 1. Membership, authority, and identity

The owner creates one sharing group: the coordinating instance plus selected
peers, one common semantic-prefix selection, and an episodic selection/cap.
Every member may receive eligible memories from every other member through the
coordinator. Enrollment shows that complete group; adding a peer broadens the
audience and requires a fresh preview and owner consent. Connecting a peer
alone leaves it outside the group.

Expanding content selection, including adding semantic prefixes or enabling
episodic sharing, also requires a new preview and owner authorization before
manual or scheduled transfers can use the wider scope.

Narrowing selection invalidates the older scope for new reads, writes, and
queued retries. Re-plan queued work under the current selection. Resolve or
reject already admitted or uncertain batches before showing the scope change
as complete; the owner can see that work while the change is pending.

Each source verifies owner-authorized read scope before returning preview data
or selected records. Preview authorization permits neither import nor onward
sharing. Each receiver verifies the owner's sharing authorization and the
agreed scope on every transfer. Reuse the existing authenticated connection
and platform authorization mechanisms; do not forward the coordinator's browser
credentials or broaden the generic chat proxy. The narrow transfer capability
and its authorization representation require maintainer approval.

Bind consent and reconciliation state to authenticated installation and store
identities, including a generation that detects replacement or restore.
Use the existing local store-identity validation where it fits, and define how
identity is established across machines. A renamed label must not reset history;
a replacement behind the same label must pause for review. An unknown identity,
incompatible format, or lost checkpoint cannot silently start a new union.

### 2. Preview and first use

The first preview combines eligible existing memories across the group.
It shows each destination, selected categories/prefixes, counts, conflicts,
and exclusions. It explicitly explains that a local deletion stays local and
that previously deleted data cannot be inferred from absence alone.

No import occurs during preview. Before the first apply, each participant must
have a recoverable backup of its own store. The owner first runs a small,
selected transfer; scheduling is a separate opt-in. Episodic sharing can remain
disabled with a zero cap.

Display values only through authorized memory views and their normal redaction.
Routine history records counts, outcomes, and errors rather than memory text.
Preview and apply must use the same eligible payload. Before selected content
leaves a source, apply the existing credential-screening policy; hold and
report a record that would require credential redaction rather than sending
its original bytes behind a redacted preview. Display redaction alone is not
a transfer policy or a guarantee that all sensitive content has been detected.

### 3. Reconciliation and receiving-store rules

The coordinator selects one candidate per semantic key. Prefer an authenticated
explicit-user edit, then the most recent originating edit, with a stable
origin-identity tie break. An import's local timestamp must not make a copied
value outrank its origin. Preserve provenance without allowing a peer's
self-declared source string to bypass receiving-store policy.

The receiver retains its validation and refusal rules. Equal values with
different provenance or protection metadata need an explicit reconciliation
rule; equality of JSON values alone is not proof that the records agree.
Expose held conflicts rather than claiming that all machines have converged.

Preserve local deletion suppression across retries and restarts. Use an
explicit tombstone or equivalent durable receiving-store evidence where
available. Missing data in a failed, filtered, or incomplete export is not a
deletion. The initial preview cannot reconstruct deletion history that was
never retained. Reintroducing a locally deleted key requires an intentional
local action; it must not follow automatically from a newer remote value.

For episodes, separate logical record identity from replay identity and content
similarity. A logical identity binds the source installation, store generation,
and episode record, and survives edits to that episode. Deletion suppression
uses that identity. A transfer's replay identifier also includes the source
revision, so an edited episode is not mistaken for an already completed write.
A payload digest can check content or replay, but cannot replace logical identity.

Maintainers must decide whether later episode revisions update an existing
copy, become separate snapshots, or require explicit local action. In every
case, changing the remote payload cannot bypass suppression of that logical
episode. Retain the receiver's normal duplicate screening. Transfer selected
episode content and agreed metadata; do not transfer conversations or promise
that an episode resumes its source session.

These rules are proposed semantics that require native validation and tests.
A local ledger alone does not establish durable remote identity, receiving-store
revisions, or acknowledged deletion history.

### 4. Transfer and recovery contract

The transfer boundary needs the following capabilities. Endpoint names and
schema details remain for maintainer design review.

| Operation | Required contract |
|---|---|
| Describe | Authenticated installation/store identity, eligible format, protocol version, and supported operations. Matching release minor versions alone is insufficient. |
| Read selected records | A complete or explicitly paginated view with record revisions, provenance, and relevant local deletion state, bounded by the approved selection. |
| Apply a batch | A replay identifier per record and an expected destination revision. A concurrent edit or deletion causes a conflict result rather than an unseen overwrite. |
| Resolve an uncertain apply | Per-record committed, unchanged, refused, conflict, or unknown outcomes; an interrupted response can be reconciled without declaring the batch successful. |

Assess reuse of the existing memory-edit preview, revision, conditional-apply,
and receipt machinery, and reuse store-level write outcomes where they express
these results. A synchronization receipt must cover the transfer's recovery
window; the existing edit preview's 15-minute lifetime is not that contract.
Write durable intent before applying a batch and retain enough information to
resolve an interrupted apply. Advance a record's checkpoint only after a
confirmed result. A process exit code or aggregate success count is insufficient.

Serialize runs for a group. Manual and scheduled runs use the same coordinator
and checkpoint. Each peer can make independent progress; an unavailable peer
is never read as an empty store. Bound payloads, batch sizes, and total run time,
and persist a cursor so repeated failures or new arrivals cannot indefinitely
starve older eligible records. A run may be partially successful; it is not a
transaction spanning machines.

### 5. Controls and operational behavior

Controls show group membership, selection, preview, run now, pause, schedule,
last confirmed transfer, pending work, and actionable errors. An authentication
failure directs the owner to the existing login or certificate-renewal flow;
the feature does not weaken key protection or renew credentials itself.

Pause blocks new batches and retains checkpoint history. Report already
admitted or uncertain batches until their outcomes are resolved; a stopped
coordinator does not prove that a receiver stopped writing. Removing a peer
invalidates its sharing authorization. The receiver must reject old batches
after the corresponding authorization or store generation is revoked.

Pausing does not erase memories already copied. Resetting history, replacing a
store, and re-enrolling are explicit recovery operations with a new preview.

## Migration plan

Each phase depends on a maintainer decision and can be stopped without enabling
the next phase.

| Phase | Deliverable | Exit criteria |
|---|---|---|
| 0: Design decision | Maintainers resolve the open questions and record accepted scope and ownership. | The decision is on the base branch before implementing product behavior. Issue #18593 remains a design proposal until maintainers assign implementation. |
| 1: Manual selected transfer | An authorized, bounded transfer primitive for eligible default stores, with structured results. | Tests cover source-read and destination-write denials, preview grants used for onward sharing, excluded stores, credential-bearing payloads, identity replacement, incompatible formats, retries, partial replies, and a concurrent destination edit. Selected copy is usable without scheduling. |
| 2: Reconciliation | One coordinator with durable checkpoints and the agreed conflict/deletion rules. | A second unchanged run writes nothing; local deletion remains suppressed even after a remote episode edit; narrowed selections invalidate queued retries; clock skew, metadata differences, crashes before/after apply, offline peers, and bounded backlog progress have deterministic tests. This phase is blocked on the conflict and deletion decisions. |
| 3: Integrated controls | The agreed UI exposes preview, group membership, manual transfer, then opt-in scheduling. | A small two-machine trial proves the displayed selection, consent, results, pause, and restore procedure; a three-machine trial proves the advertised group audience. No connection enables sharing by itself. |

Record each phase's verification against the supported versions and platforms.
Start with a supported platform pair; other combinations remain unavailable
until their transfer and recovery paths are verified.

Before enrollment, stop any existing job that copies memories among the
participating instances. Do not treat an external tool's checkpoint as
authenticated identity. Migrate its history only if identities and local
deletion suppression can be preserved; otherwise explain the new enrollment
preview and its limits.

## Backward compatibility and rollback

Sharing defaults to off. Existing Remote Crew sessions keep their peer owner.
An older peer without the agreed transfer capability remains usable for its
existing functions and is marked unsupported for memory sharing.

Normal operational rollback disables sharing and resolves outstanding batches;
it preserves current memory, including legitimate local edits.

Whole-store restore is a separate, deliberate recovery action. It removes
changes made after the selected backup and can restore memories deleted since
that backup. Show the backup cutoff, preserve a current recovery copy, and
have the owner review later changes before replacing a store. This proposal
does not promise selective undo of earlier imports.

Before restore, resolve outstanding writes or make the receiver reject their
old authorization/store generation so they cannot commit into the restored
store. Use each machine's own backup and supported restore procedure, then
revalidate identity/history before re-enrollment. Do not clear checkpoints
while a coordinator can still run.

## Security considerations

Memory can contain sensitive facts. Enrollment must make the recipient group
and allowed content clear, and receivers must enforce that agreement.
Named/private stores and excluded categories cannot fall back into the default
store or a broader export.

Enforce authorized selection and credential screening at source egress, and
authorization, validation, and write auditing at each receiving Kiro Crew
boundary. Do not forward authentication credentials or use a redacted preview
to authorize an unredacted payload. Expose no new public listener and treat
remote memory content as data. Store local synchronization state with the same
access expectations as the memory it describes.

## Alternatives considered

| Option | Benefit | Tradeoff |
|---|---|---|
| Schedule direct two-way export/import | Uses existing CLI commands and scheduling. | Bulk import counts cannot establish per-record results, and repeated copying does not supply the required identity, conflict, or deletion contract. |
| Add native Kiro Crew sharing and controls | One authority for memory semantics, integrated with the existing connected-instance experience. | Requires the design and implementation work described here. Recommended for a supported integration. |
| Share a database or synchronize whole data directories | Broad state transfer through existing file tools. | Bypasses selection, live-store ownership, and per-record conflict handling; does not meet this proposal's requirements. |

## Open questions

Maintainers decide these on the RFC before assigning implementation:

1. Where in Kiro Crew's Remote Crew experience should sharing controls live,
   using the project's approved app and dashboard extension boundaries?
2. Is a single-owner, single-coordinator group limited to eligible default stores
   the right first scope? Does the all-members audience match the expected UX?
3. Are explicit-user precedence, metadata reconciliation, and local-only deletion
   the intended semantics? How should later episode revisions appear, and what
   user action reintroduces a suppressed key or episode?
4. Which native retirement effects may incoming semantic and episodic writes
   trigger? How should episode similarity replacement, capacity eviction, and
   semantic-update retirement be distinguished from an owner's explicit forget
   action?
5. How should a receiver preserve forget markers when a source's store generation
   changes after restore or rollover? Which verified record lineage carries
   forward, and when do ambiguous records require explicit owner review?
6. Which existing identity, authorization, store-write, and job mechanisms should
   carry pairing, conditional writes, receipts, and durable progress?
7. Which format/version and platform pairs should the first trial support, and
   who owns implementation and maintenance after the design is accepted?

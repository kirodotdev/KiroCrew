---
title: One artifact chat per version
status: draft
author: atrasatti
created: 2026-11-18
last-audited: 2026-11-18
audited-at: 576272264c
doc-pr: null
implementation-prs: []
tracking-issues: [39323]
supersedes: []
superseded-by: []
---

# RFC: One artifact chat per version

Status: draft. Nothing built. This proposes that the artifact page's companion
chat belongs to one version of the artifact instead of to the artifact as a
whole, and that every way of asking the agent to work on comments lands in that
version's chat, visible in the sessions list.

## 1. Summary

Today an artifact has at most one live companion chat, titled
`Artifact: <name>`, and it runs for the artifact's whole life. For an artifact
that a cron rewrites every day, or one that goes through many review rounds, that
one chat keeps growing, and the agent carries the context of every earlier round
into the next.

This RFC binds a companion chat to `(slug, version)`. The chat is titled
`Artifact: <name> v<N>`. Opening the chat, drafting a prompt about the comments,
or submitting pending comments all resolve to the chat of the version on screen,
creating it on first use. A chat the agent is working in keeps going when the
agent's own edit saves a new version, so a review round is not split at every
save. An earlier version's chat stays reachable by picking that version.

The UI stays the same. No new panel, button or menu is added; the change is which
session each existing affordance resolves to.

## 2. Current state

Measured at `576272264c`.

**Binding.** A slot carries `_artifact`, a validated slug, set from the
`artifact` key of `POST /api/chat/slots`
(`src/kiro_crew/dashboard/chat_api/slot_create.py`, the "Bind to an artifact" block), defaulted in
`init_sidebar_fields` (`src/kiro_crew/dashboard/state_parts/slot_sidebar.py`),
sent to clients as `"artifact"` in `to_dict`
(`src/kiro_crew/dashboard/slot_projection.py`) and persisted in the history
meta line (`_read_artifact` and the `"artifact"` field row in
`src/kiro_crew/dashboard/slot_persistence/metadata_codec.py`). Nothing about the version is stored.

**Resolution.** The page picks the bound session from the slots snapshot by slug
alone: `pickBoundSlot` filters `x.artifact === slug` and, when there is more than
one, takes the most recently active
(`website/src/pages/useArtifactDetailPageCompanion.ts`). The frontend keeps
at most one active bound session per slug by archiving before it creates
(`newCompanionChat`, same file). The backend does not enforce this
([artifacts.md](../system-specs/modules/artifacts.md#companion-chat), Invariant).

**Title.** `createBoundSession` pins `pages.artifactDetailPage.session_title`
with `{ name }` (same file), which renders as
`Artifact: <name>`.

**Version awareness.** The chat is told the version only through the short-lived
context entry (`buildCompanionContext` in `website/src/pages/ArtifactDetailPage.tsx`,
`v${artifact.version}`), and a reopen re-sends it when the artifact changed after
the agent last acted (the resume freshness nudge in `openCompanionChat`). The session itself is the
same across versions.

**Entry points.** These are the affordances that reach the companion chat:

| Affordance | Code | Today |
|---|---|---|
| Agent chat (sparkle) in the header | `ArtifactDetailPageHeader.tsx` → `openCompanionChat()` | Toggles the panel; resumes the slug's session or creates one |
| Draft a prompt in chat | `ArtifactDetailPageBody.tsx` `onAskAgent` → `openCompanionChat({ address: true })` | Same, and stages "Please review and address the N open comments" in the composer, not sent |
| Submit (N comments to send to this chat) | `ArtifactDetailPageBody.tsx` `submitBar` → `submitCommentsToChat` | Shown only while a session is already bound; sends every pending comment as one turn |
| `+` in the Agent chat panel | `newCompanionChat` | Archives the slug's sessions, creates a fresh one |
| Open in chat page | `openChatFull` | Navigates to `/chat` on the bound session |
| More → Send to a session | `website/src/components/ArtifactSendToSession.tsx` | Lists New session and the ten most recent sessions that are NOT artifact-bound (the `listed` filter); appends a one-line artifact reference to that session's composer; sends nothing |

**Comments are not versioned.** Comments live per slug in `comments.json`. Each
carries an optional `anchor_version`
(`ArtifactComment` in `src/kiro_crew/artifact_store/model.py`), but an open comment stays open
across versions.

**Who made a version.** Every content write appends an event with `type`
(`edited`, `iterated`, `reverted`, ...), `session_id` and `version`
(`src/kiro_crew/artifacts_writes.py`, through `_append_event` in `src/kiro_crew/artifacts_disk.py`).
The page already loads these for its activity timeline
(`GET /api/artifacts/{slug}/events`). The log keeps the newest 500 events
(`MAX_EVENTS_PER_ARTIFACT` in `artifacts_disk.py`).

## 3. Motivation

1. **Context grows without bound.** A daily report rebuilt by a cron is past
   version 120 on the author's install. Its one companion chat holds every day's
   review. Each new round pays for the old ones in tokens and in an agent that
   acts on stale assumptions, which is why the page already needs a freshness
   nudge on reopen.
2. **Old work is hard to find.** A review round is tied to the version it
   reviewed, but the chat that holds it is not. To revisit what was asked and
   done on v118, the operator scrolls one long transcript.
3. **Asking for comments to be worked on does not always leave a session.** The
   Submit bar only shows once a session is bound, so on a fresh visit the operator
   has to open the chat first. A chat that was opened and left untouched is
   discarded on leave. The operator cannot rely on "I asked the agent to work on
   these comments" producing a session they can find in the sessions list.

## 4. Goals

- A companion chat belongs to one version of one artifact.
- Every entry point in § 2's table resolves to the chat of the version on
  screen, with no new UI.
- Asking the agent to work on comments always leaves a live, titled session in
  the sessions list.
- An agent working in a version's chat can save new versions without its
  conversation moving away.
- An earlier version's chat stays reachable from the page.
- An artifact with no comments and no chat opened has no session, as today.

## 5. Non-goals

- **Carrying context from one version's chat to the next.** A new version's chat
  starts empty except for the existing context entry. Whether a short carry-over
  is wanted is left to § 13.
- **Versioning comments.** Comments stay per artifact. An open comment from v126
  still shows on v127 and is offered to v127's chat. From any version's chat the
  operator and the agent can read, act on, reply to and resolve every open
  comment on the artifact, whatever version it was left on; the comment tools
  already take only the slug.
- **Changing Send to a session.** It keeps its behaviour; § 6.6 explains why it
  does not break and § 13 asks whether to add an entry.
- **Backend enforcement** of one session per version. The frontend keeps the
  invariant, as it does per slug today.

## 6. Design

### 6.1 The binding

A slot bound to an artifact also records the version and the artifact's
generation stamp:

| Field | Type | Set from | Meaning |
|---|---|---|---|
| `artifact` | slug | unchanged | which artifact |
| `artifact_version` | positive int | new `POST /api/chat/slots` key | the version this chat started on |
| `artifact_created_at` | ISO string | new `POST /api/chat/slots` key | the artifact's `created_at`, so a slug deleted and reused does not inherit old chats |

Both new fields are validated at every boundary the slug is (slot create and both
history restore paths), sent in `to_dict`, and persisted in the history meta
line next to `artifact`. A value that fails validation is dropped and the slot is
left unbound, as an invalid slug is today. The binding is still written only at
create. Nothing rewrites it.

### 6.2 Which chat the page shows

For the version on screen, `N`, the resolver picks, in order:

1. A live session bound to `(slug, created_at, N)`. More than one: the most
   recently active, as today.
2. The live bound session that **made** version `N`: the event with
   `version == N` names a `session_id` that is one of this artifact's live bound
   sessions. That chat started on an earlier version and its agent saved `N`, so
   the conversation continues on `N`.
3. None. The page creates a session for `N` only when an entry point needs one
   (§ 6.4), never on load.

Rule 2 is what keeps a review round in one chat. Without it, the agent's first
`artifact_update` in a v126 chat would save v127, the page would follow to v127,
find no v127 chat and show an empty panel mid-conversation.

Rule 2 does not follow a version made by anything else: a cron rewrite, another
session, or a Save from the page's own editor. Those start a new version's chat
on next use. That is the point for the daily report: each morning's rebuild gets
a fresh chat.

Rule 2 reads the event log, which keeps the newest 500 events. When the event
that made `N` has aged out, rule 2 finds nothing and the page falls to rule 3. A
miss costs a fresh chat, not a wrong one.

### 6.3 The title

`createBoundSession` pins `Artifact: <name> v<N>`, where `N` is the starting
version. A chat that continues under rule 2 keeps its title, because the title
names the round's starting point and is the thing the operator searches for.
The panel header already names the artifact; it adds the starting version when
the chat continues past it (for example `Started on v126`). Exact wording follows
the i18n catalog rules.

### 6.4 Entry points

| Affordance | After this change |
|---|---|
| Agent chat (sparkle) | Toggles the panel on the chat § 6.2 resolves; creates the version's chat when none resolves |
| Draft a prompt in chat | Same resolution, then stages the address-comments prompt as today |
| Submit bar | Shown whenever there are pending comments, bound or not. Resolves or creates the version's chat, then sends the batch |
| `+` in the panel | Archives this version's chats only, then creates a fresh chat for the same version. Other versions' chats are untouched |
| Open in chat page | Unchanged; it opens the resolved chat |
| Send to a session | Unchanged; see § 6.6 |
| A historical version picked in the version select | The panel shows that version's chat when one is live. When none is, the panel says so and offers the current version's chat. It does not create a chat for a historical version, because the agent can only change the live version |

The Submit bar change answers motivation 3. Its comment explains why it was
limited to a bound session: with none bound it had to decide whether the artifact
already has one, and a wrong answer opens a second chat. `openCompanionChat`
already solves that on a cold load by fetching the slots before creating
(the `!slotsLoaded` branch of `openCompanionChat`). The Submit path reuses that step.

A chat that received a submitted batch or a staged prompt is not untouched, so
the untouched-session discard leaves it. The discard rule itself does not change.

### 6.5 Earlier versions' chats

They stay live, like any session, and are listed in the sessions list by their
versioned title. The operator closes them like any session; a closed one resumes
from History with its binding, as today.

For an artifact rebuilt daily this adds one session per day the operator chats.
The store keeps the newest `MAX_VERSIONS` (50) versions and prunes older ones
(`_prune_versions` in `src/kiro_crew/artifacts_disk.py`). The proposed rule
(§ 13 Q2) ties a chat's life to that: when every version a chat is tied to (the
one it started on and any its agent saved) has been pruned, the chat is closed
the way the sidebar's × closes a session. Its transcript stays in History. The
same rule applies when the whole artifact is deleted. A chat is never closed
while a version it is tied to still exists, so a chat the operator is still
using is not taken away.

### 6.6 Send to a session

Send to a session already leaves out artifact-bound chats
(the `listed` filter in `ArtifactSendToSession.tsx`) and only appends a reference line to the target
composer. It is the way to bring the artifact into an unrelated conversation, so
binding chats to versions does not change what it does. New session there still
makes an unbound session, which is still useful when the operator wants to work
on the artifact outside the version flow.

Two additions were considered and left to § 13: listing this version's chat
first in the submenu, and sending the pending comments (not just the reference)
to the picked session.

### 6.7 The context entry

Unchanged. It already names the version. The reopen freshness nudge still applies
inside one version's chat under rule 2, since the agent there may be several
saves behind the page.

## 7. Code changes

| Area | File | Change |
|---|---|---|
| Slot field | `src/kiro_crew/dashboard/state_parts/slot_sidebar.py` | Add `_artifact_version`, `_artifact_created_at` defaults |
| Create | `src/kiro_crew/dashboard/chat_api/slot_create.py` | Read and validate the two keys beside `artifact` |
| Projection | `src/kiro_crew/dashboard/slot_projection.py` | Send `artifact_version`, `artifact_created_at` |
| Persistence | `src/kiro_crew/dashboard/slot_persistence/metadata_codec.py` | Read, write and merge both fields with the same validation as `artifact` |
| Types | `website/src/types/` (`ChatSlot`) | Two optional fields |
| API client | `website/src/api/client/chat.ts` `createChatSlot` | Pass version and created stamp |
| Resolver | `website/src/pages/useArtifactDetailPageCompanion.ts` | `pickBoundSlot(slots, slug, createdAt, version, events)` implementing § 6.2; `boundSlots` scoped to the version; `createBoundSession` passes the new keys and the versioned title; `submitCommentsToChat` resolves or creates |
| Open path | `website/src/pages/ArtifactDetailPage.tsx` | `openCompanionChat` uses the new resolver; historical-version branch |
| Submit bar | `website/src/pages/ArtifactDetailPageBody.tsx` | Drop the `boundSlot` condition on `submitBar` and `pendingSubmitIds` |
| Panel header | `website/src/components/ArtifactChatPanel.tsx` | "Started on v<N>" line when the chat continued |
| Strings | `website/src/i18n/locales/*.json` | Versioned title and the two new panel strings |
| Pruning | `src/kiro_crew/artifacts_disk.py` `_prune_versions`, artifact delete | Report pruned versions so the dashboard can close chats tied only to them (Phase 3) |
| Spec | `docs/system-specs/modules/artifacts.md` § Companion Chat | Binding, Invariant (per version), Session resolution, entry points |

The page files are split by owner, and `ArtifactDetailPage.owners.test.tsx` pins
the hook order. The resolver change stays inside the existing hooks, so the order
does not move.

## 8. Migration plan

**Phase 1: binding fields (backend only).** Add, validate, project and persist
`artifact_version` and `artifact_created_at`. Nothing sets them yet.
Exit: a slot created with both keys round-trips them through `GET
/api/chat/slots`, the WS `slots` frame and a gateway restart; an invalid value is
dropped at create and at both restore paths.

**Phase 2: per-version chats (frontend).** Resolver, title, entry points and
historical branch, with the spec update.
Exit: the tests in § 11 pass; a v126 chat in which the agent saves v127 stays on
screen; a cron-made v127 opens a new chat titled `v127`.

**Phase 3, blocked on § 13 Q2-Q4:** closing chats whose versions are pruned,
the Send to a session ordering, and tagging editor Saves with the open chat.
Exit: pruning a chat's last tied version closes it and leaves its History row;
the version's chat is listed first in Send to a session; a Save with the panel
open keeps the same chat on screen.

## 9. Backward compatibility

A session bound before this change has a slug and no version. The resolver does
not match it under rule 1, and matches it under rule 2 only if it made the
version on screen. So the first open after upgrade usually creates a new
`v<N>` chat, and the old `Artifact: <name>` session stays in the sessions list
and History, unchanged and resumable from there. No data is rewritten.

The dashboard is built into `src/kiro_crew/static/dist/` and served by its own
gateway, so a frontend and gateway of different versions meet only in a tab left
open across an upgrade. An older tab ignores the two fields and keeps per-slug
behaviour until it reloads. A newer tab cannot reach an older gateway except
through such a stale gateway process; there the created slot comes back without
`artifact_version`, and the page falls back to matching that slot by slug so it
does not create a new chat on every open. Pinned by a test.

## 10. Security considerations

The two new fields cross the same boundaries as `artifact` and get the same
treatment: validated at create and at both history restore paths, so a tampered
history line cannot inject a value into `to_dict` or the WS broadcast. The
version is an integer bounded at 1 or more; the stamp must parse as an ISO
timestamp of bounded length. No new endpoint, no new permission, and no change
to who can read a session.

## 11. Tests

Backend:

- Create with valid fields round-trips them; invalid ones are dropped and leave
  `artifact` intact.
- History restore (rehydrate and bulk) applies the same validation.

Frontend (`useArtifactDetailPageCompanion`, `ArtifactDetailPage`):

- Rule 1 picks the version's chat over another version's more recent chat.
- Rule 2 keeps a chat on screen after the event for the new version names it.
- A cron-made version (no matching `session_id`) resolves to none.
- A reused slug with a different `created_at` never matches an old chat.
- Submit with nothing bound creates one chat, then sends; a cold load does not
  create two.
- `+` archives only this version's chats.
- A historical version with no chat creates nothing.
- A legacy unversioned binding is not matched by rule 1.

## 12. Alternatives considered

| Alternative | Why not |
|---|---|
| Keep one chat per artifact (today) | The problem this RFC exists for |
| Strict per version, no rule 2 | Every agent save splits the review round; the panel goes empty mid-conversation |
| Re-stamp the binding when the agent saves | Needs a second writer of the binding in the artifact write path, and the chat's title would drift from what the operator saw when starting it |
| One chat per day | Matches the daily report but not an artifact edited many times a day or once a month |
| Key on name instead of slug | Names can be renamed and are not unique |

## 13. Open questions

Each question carries the author's recommendation and why. The team decides.

1. **Carry-over.** Should a new version's chat start with a short summary of the
   previous version's unfinished work?
   *Recommendation: no.* Rationale: the goal is a chat that starts small. Open
   comments already carry forward because they are per artifact (§ 5), and the
   older chat stays one click away in the version select. A summary would add a
   writer of chat content and a second account of the same work that can drift
   from the transcript.
2. **Older chats.** When should an older version's chat leave the sessions list?
   *Recommendation: close it when every version it is tied to is pruned
   (`MAX_VERSIONS`, 50) or the artifact is deleted; keep its History row (§ 6.5).*
   Rationale: once those versions are gone the page can no longer show what the
   chat was about, so a live chat has nothing to return to. Tying it to version
   retention reuses a limit the store already enforces instead of a new timer,
   and never closes a chat while its version can still be opened. Closing rather
   than deleting keeps the transcript, so nothing is lost by default. Closing on
   every new version was rejected: it would close a chat the operator left
   mid-review yesterday.
3. **Send to a session.** Should the submenu change?
   *Recommendation: (a) list this version's chat first, above New session; (b)
   keep sending only the reference line, never the pending comments; (c) keep New
   session.* Rationale: (a) the version's chat is the most likely target, and
   listing it makes the menu agree with the rest of the page; (b) the menu today
   only pre-fills a composer and never sends, and comments already reach the
   version chat through Submit, so a second sender would mean two paths that can
   both mark the same comments sent; (c) New session is the one way to discuss
   the artifact outside the version flow (for example across several
   artifacts), and removing it breaks a shipped feature for no gain.
4. **Editor saves.** A Save from the page's editor makes a new version and, under
   rule 2, a new chat. Should a Save made while the chat panel is open continue
   that chat instead?
   *Recommendation: yes. A Save made while the panel shows a chat records that
   chat's key as the event's `session_id`, so rule 2 follows it.* Rationale: an
   operator fixing a line by hand mid-review is part of the same round, and
   splitting the chat there is the same failure rule 2 prevents for agent saves.
   It needs no new rule, only the existing event field. A Save with the panel
   closed starts a new chat, as in § 6.2.

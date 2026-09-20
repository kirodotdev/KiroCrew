---
title: Arrival provenance filing — file every arriving session under Imported / from <sender>
status: accepted
author: chenmingwei23
created: 2026-09-20
last-audited: 2026-09-20
audited-at: f31f25a8a2
doc-pr:
implementation-prs: []
tracking-issues: [11468]
supersedes: []
superseded-by: []
---

# RFC: Arrival provenance filing — file every arriving session under Imported / from &lt;sender&gt;

- Status: accepted — the decision record for a **changed default** on an
  existing route, merged ahead of the implementation because the First
  Principles lane reads it off the base branch. Every "exists today" claim below
  was checked at `f31f25a8a2` (main, 2026-09-19); citations name symbols, not
  line numbers.
- Author: chenmingwei23
- Tracking issue: [#11468](https://github.com/kirodotdev/KiroCrew/issues/11468)
- Related: [rfc-crew-projects.md](rfc-crew-projects.md) (folders as the sidebar's
  organising object), [rfc-session-address-model.md](rfc-session-address-model.md)
  (a session's identity versus its placement).

## 1. Problem

A session can arrive on an instance two ways. A peer pushes one over an
Instances tunnel, and a person installs one from a file exported by
`GET /api/chat/slots/{slot}/export`. Both land at the top level of the sidebar,
mixed in with the sessions the person started themselves.

The receiving code says so deliberately. `api_chat_slot_import` in
`src/kiro_crew/dashboard/session_transfer.py` composes the imported slot's
metadata with no `folder_id`, and its comment records that the imported session
"lands unfiled just as the tunnel importer always has".

The cost is that "where did this session come from" has no durable answer. The
arriving tab carries a marked title, and a title is renameable prose: once it is
edited, nothing on the session says it was not started here. On an instance that
receives from several peers the top level becomes a pile with no grouping, which
is the one thing the sidebar's folders exist to prevent.

This RFC decides that an arriving session is filed under a folder named
`Imported`, in a child folder named `from <sender>`, and that the rule applies to
both arrival routes.

## 2. Why this needs a decision record rather than just a patch

For the file route, filing is a new feature: that route shipped in
[#11315](https://github.com/kirodotdev/KiroCrew/pull/11315) and has no prior
default to change.

For the tunnel route it is a **changed default** on a path that has behaved one
way since it shipped. A person who has been receiving sessions from a peer will
find the next one somewhere new. That is the kind of change a base branch should
hold a record of before the code lands, which is what First Principles Review
said when it blocked the filing half of #11315: prose written inside the change
under review is the proposal, not the decision. This document is the decision.

The change is reversible by deletion, not by a flag. There is no
`arrival_folder` setting: a person who does not want the grouping deletes the
`Imported` folder, and the delete handler's own unfile sweep returns its
sessions to the top level. A later arrival recreates the folder, which is the
behaviour the channel-filing precedent already has
(`ensure_channel_folder` in `src/kiro_crew/dashboard/channel_folders.py`
recreates a deleted folder on the next settings save). A configuration knob is
deliberately not added: the feature has one sensible destination, and a
per-instance setting would be a second thing to migrate for a grouping the
person can undo with one click.

## 3. The destination must not depend on the body's format

`POST /api/chat/slots/import` is the only server route behind both arrival
routes. It decides between gzip and plain JSON by sniffing the body's first two
bytes, because the three callers that reach it (the tunnel, a browser upload, the
export endpoint's own file) each label the same bytes differently.

A gzipped file arriving from a person and a plain-JSON bundle arriving over the
tunnel are the same event carried by different transport. "Where did this session
come from" is the same question in both, so the answer must not be read off the
encoding. Filing only the file route would leave one handler answering that
question differently depending on which bytes it received, and the difference
would be invisible to a reader of either caller.

So the narrower version — file the file arrival, leave the tunnel unfiled — is
rejected, not deferred. It is cheaper only until somebody has to explain why an
uncompressed export lands in a different place than a compressed one.

## 4. Shape

Two folders, found or created on arrival:

| Folder | Name | Parent |
|---|---|---|
| Group | `Imported` | top level |
| Sender | `from <sender>` | the `Imported` folder |

`<sender>` is the bundle's `origin` field — the sending instance's label — after
the same credential and exfiltration redaction the arriving title already gets. A
bundle with no `origin` is filed under `Imported` directly: there is no sender to
name, and inventing one ("from unknown") would make a missing field look like a
peer.

`origin` is a field of an **untrusted** bundle, so it decides a folder's NAME and
nothing else. It confers no authority, is never read as an identity, and is never
compared against a credential.

Folder names are ASCII English literals, not translated strings. A folder created
here becomes an ordinary sidebar row the person can rename, reparent or delete,
and a name re-derived per render from the active locale would fight that rename
and would move the folder every time the interface language changed.

## 5. Constraints the implementation must satisfy

The filing half of #11315 drew five consecutive blocking review rounds, each a
real defect in the folder lifecycle rather than in the filing idea. They are
recorded here as requirements so the next implementation does not rediscover
them.

1. **An app-scoped caller creates no folder.** The folder store has a global
   ceiling (`MAX_CHAT_FOLDERS`), so an app token that could create a folder per
   arrival could loop imports with distinct `origin` values until the ceiling is
   full and the person is refused a folder of their own. An app-scoped arrival
   therefore lands unfiled and touches the folder store not at all — it does not
   create, and it does not adopt a folder the person owns. Identity comes from
   the shared `effective_request_app` rule in
   `src/kiro_crew/dashboard/token_auth.py`, never from the request body.

2. **Placement is resolved only after the slot exists.** The import handler
   re-checks the live-slot cap after its last `await` and can answer `429` there.
   A folder write standing in front of that re-check is left behind when it
   fires, which is the same store exhaustion with a narrower trigger. So the
   folder work happens after the slot has been materialised, where a refusal is
   no longer possible.

3. **Find-or-create is atomic.** The find, the create and the persist happen
   inside one `mutate_folders` transaction, the way `ensure_channel_folder`
   already does it. Two arrivals from one peer racing each other must not each
   observe the folder absent and each create one.

4. **A folder is looked up in the form the store writes it.** `create_folder_record`
   trims and clips a name before appending it, so a lookup that compares the
   untrimmed, unclipped name misses the row a clipped create just wrote and
   creates a duplicate on every arrival from a long-named sender.

5. **A folder deleted mid-import must not leave a dangling id.** The import
   handler retracts the slot from `state._slots` for its asynchronous
   finalisation stretch, so the folder delete handler's unfile sweep — which
   iterates `state._slots.values()` — cannot see it. The arrival therefore
   re-checks the folder immediately before its durable save, and repairs the slot
   after it is registered again: if the folder is gone, the `folder_id` is
   cleared and the slot re-saved, so the session renders at the top level rather
   than pointing at a row that no longer exists.

6. **Filing is best-effort.** A folder refusal — the ceiling, a store write
   failure — lands the session unfiled. It must never fail an import that works
   today: the session and its transcript are the payload, and the grouping is
   convenience.

## 6. Alternatives measured against

**One flat folder per sender, no `Imported` parent.** Fewer folders per sender
(one instead of two) and a shallower tree. Rejected because the top level then
accumulates one row per peer with nothing saying what those rows have in common,
and a person who wants the grouping gone has to delete each one. The parent is
what makes the feature undoable in a single action.

**File by the sender's instance id rather than its label.** Stable against a peer
renaming itself. Rejected because the id is not a name a person recognises, and
the folder is a thing they read. A peer that renames itself gets a second folder,
which is the same outcome as a person renaming a folder by hand, and is visible
rather than silent.

**A `session_folder`-style configuration value, matching the channel precedent.**
Rejected in §2: the channels setting exists because a channel has no natural
destination and several channels compete for one, while an arrival has exactly
one sensible destination and the person can undo it by deleting a folder.

**Stamp the folders with a marker field and match on the stamp.** This is what
`channel_folders` does (`channel: "<namespace>"`), so a folder the person renamed
is still recognised as the channel's own. Rejected here because the arrival path
has no relabel step to protect: it never renames an existing folder, so a stamp
would buy only the ability to follow a rename — and following a rename is exactly
what §4 says not to do. Matching on name keeps the person's rename meaningful:
renaming `from laptop` detaches it, and the next arrival from that peer creates a
fresh folder, visibly.

## 7. Acceptance

- Both arrival routes file; a second arrival from one sender lands beside the
  first, not in a second folder of the same name.
- An app-scoped arrival creates no folder and lands unfiled.
- A folder refusal leaves the session unfiled rather than failing the import.
- A folder deleted while an import is finalising leaves no dangling `folder_id`.

## 8. Provenance

Written for [#11468](https://github.com/kirodotdev/KiroCrew/issues/11468), which
carries the review history of the withdrawn implementation in
[#11315](https://github.com/kirodotdev/KiroCrew/pull/11315).

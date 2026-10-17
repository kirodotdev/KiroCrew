---
title: Crewmate settings — Trust by default, set on the profile, not in the chat
status: accepted
author: iamwhatever
created: 2026-10-07
last-audited: 2026-10-07
audited-at: 99c76a43d6
doc-pr:
implementation-prs: [17836]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Crewmate settings — Trust by default, set on the profile, not in the chat

- Status: accepted. This is the product owner's decision, given 2026-10-07 in the
  "Crew page polish" goal (item D). It amends
  [rfc-crewmates-launch.md](rfc-crewmates-launch.md) (the crewmate DM and its
  profile card). Claims about today's code were checked at `99c76a43d6` (main).
- Author: iamwhatever
- Implementation: [#17836](https://github.com/kirodotdev/KiroCrew/pull/17836).

## 1. Summary

A crewmate's own Crewmates-page thread runs in **Trust** unless the user picked
another permission for that crewmate. Permission, model and reasoning effort are
set on the crewmate's **profile card**. The Crewmates page's composer carries no
toolbar line: no agent, model or effort chip, no context meter, no approval
picker.

## 2. Motivation

At `99c76a43d6`:

- A crewmate's DM slot is created by `POST /api/members/{slug}/thread`
  (`dashboard/handlers/members.py`, `api_member_thread`) with no approval state,
  so `_trust` / `_trust_reads` are `False` (`dashboard/state_parts/chat_slot.py`) and every tool
  call asks.
- The crew record (`KiroCrewAgentConfig`, `config/sections_session.py`) has `model` and
  `reasoning_effort` but no approval field, so a permission can only be picked
  per chat, in the composer, and is gone on the next thread.
- The profile card (`pages/members/CrewProfilePanel.tsx`) shows Permissions and
  Model only as rows that open the crew editor ("set in the editor").
- The Crewmates DM reuses the session chat's `ChatInput` through `ChatPane`, so it
  carries the agent / model / effort chips, the context-window meter and the
  approval picker of an ordinary session.

The product owner's ask, verbatim in substance:

1. A mate should have Trust permission by default, unless the user changed it.
   Never overwrite a permission the user already chose.
2. Permission, model selection and effort are changed in the mate's profile, not
   in the chat.
3. Remove that toolbar line from the Crew page composer — model picker, effort,
   permission **and the context-window indicator**.
4. Give the Crew page its own composer instead of branching the shared one; the
   normal chat composer must be unchanged.

## 3. Decision

| Rule | Before | After |
|---|---|---|
| Crewmate thread approval on open | Normal (asks every tool call) | the record's `approval_mode`; unset (`""`) opens in Trust |
| Where a crewmate's permission lives | per chat, in the composer | `agents.<name>.approval_mode` on the crew record, set on the profile |
| Model and effort | crew editor, or per chat in the composer | the profile card (writes the record and the live DM slot) |
| Crewmates-page composer toolbar | agent, model, effort, context meter, approval picker | none |
| Normal chat composer | — | unchanged |

Who gets Trust by default: every crewmate whose record has no `approval_mode`,
and only in its own Crewmates-page DM thread. Schedules, sub-agents, channels and
ordinary chats that run the same crew are untouched.

What is preserved: a stored `normal` / `trust_reads` / `trust` is applied as
stored; a grant already on the live slot (an approval card's "trust this
session", a `trust_reads` pick) is never overwritten; the seed runs once per
in-memory slot, so a later change on the thread stands until restart, and the
record is what a restart reads. YOLO is never a crewmate setting: it is
process-global.

The context meter is dropped from the Crewmates composer by the product owner's
explicit ask (item 3). A crewmate's DM is a long-lived conversation the gateway
compacts on its own; the user does not manage its window from this surface.

## 4. Non-goals

- Changing approval for any session other than a crewmate's own Crewmates-page
  thread.
- Making YOLO, or any app-armed scoped grant, a crewmate setting.
- Changing the ordinary chat composer or any other `ChatPane` host.

## 5. Backward compatibility

The field is optional with `""` as default; existing records load unchanged and
read as unset, which is the Trust default by decision. An older gateway that
saves the record drops the field; after a downgrade/upgrade cycle a crewmate the
user set to Normal reads as unset again. Accepted: the user re-picks on the
profile.

## 6. Security considerations

Trust auto-approves tool calls in that one thread without a prompt. It is the
mode the user already picks per chat today; this decision makes it the default
for a crewmate the user created and chose to delegate to, and keeps every
explicit choice. Each seed writes a SEL `mode_change:trust` (or `trust_reads`)
entry under `dashboard:member_profile`, the same audit the picker writes. The
`approval_modes` policy scope governs only `yolo`, so it is unaffected.

## 7. Alternatives considered

- Keep Normal as the default and only move the picker to the profile: rejected by
  the product owner (item 1).
- Keep the context meter in the Crewmates composer: rejected by the product owner
  (item 3).
- Branch the shared `ChatInput` with crewmate flags: rejected (item 4); the
  Crewmates page passes its own composer through a `ChatPane` prop instead.

---
title: A plain session is the default template, never the default crewmate
status: accepted
author: CrysisDeu
created: 2026-10-09
last-audited: 2026-10-09
audited-at: 96ebc9736f
doc-pr:
implementation-prs: [18342]
tracking-issues: [18328]
supersedes: []
superseded-by: []
---

# RFC: A plain session is the default template, never the default crewmate

- Status: accepted. This is the product owner's decision, given 2026-10-09 in
  the Crewmates work ("一个 chat 是 template 还是 crewmate 应该是非常 specific 的
  … session 的默认就应该是 default template，都不该是 default crewmate"). It amends
  one sentence each of [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md)
  and [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md). Claims
  about today's code were checked at `96ebc9736f` (main).
- Author: CrysisDeu
- Implementation: [#18342](https://github.com/kirodotdev/KiroCrew/pull/18342),
  tracking [#18328](https://github.com/kirodotdev/KiroCrew/issues/18328).
- Amended 2026-10-09 (rule 6): the picker's **Default for new sessions** row
  follows the template, since a control that enrolled a crewmate there stopped
  controlling what a new session runs the moment rule 1 landed (the UX lane's
  blocker on #18342 R6).

## 1. Summary

A session is started from three things: a **custom agent** (a kiro-cli agent
template; the chat picker's "Custom agents" group), a **folder** (workspace /
project) and a **memory store**. A **crewmate** is a
durable identity that carries its own three, talked to in its DM thread on the
Crewmates page, or picked by name for an ordinary chat. The two are distinct
kinds of selection and a session is always exactly one of them.

A session created without picking a crewmate -- the dashboard's New chat with
the picker untouched, a cron, channel or subagent session that names no agent --
is a **template session on the default custom agent** (`agent.default_agent`,
`kirocrew` when unset), on the default folder and Global memory. There is no
"default agent" as a product concept: there is a default custom agent (a
template) for sessions, and the `default` crewmate is a crewmate. It is not the `default` crewmate. The `default`
crewmate is one row of the roster like any other: it may bind a different
template, it is credited only for its own DM thread and for chats that picked
it, and the Crewmates landing ranks it by that like every other row.

## 2. Motivation

At `96ebc9736f`:

- `api_chat_slot_create` stamps `cfg.default_agent` (the `default` crewmate
  alias, namespace `member`) on an agent-less create, so every plain chat IS that
  crewmate's.
- `resolve_agent_bindings` answers an empty name, an unknown name and a template
  pick with the `default` row's workspace and **model pin**; a model, folder or
  private memory set on the `default` crewmate silently applies to every plain
  chat, cron, channel and subagent session that named no agent.
- `crew_recency` records a plain chat under `""`, and `/api/members` folds that
  into the `default` row's `last_chat_ts`, so `default` wins the Crewmates
  landing nearly always. #17972 worked around that in the browser with a chat
  mark instead of fixing the attribution.

The owner's rule: what a session runs must be specific. Picking a crewmate is
picking a crewmate; picking nothing is the default template. A crewmate's main
conversation is its DM thread.

## 3. Decision

| Session | Template | Folder / memory | On the Crewmates page |
|---|---|---|---|
| Created without picking a crewmate | the default custom agent: `agent.default_agent`, else `kirocrew` | default workspace / Global | nothing: nobody's conversation |
| Explicit template pick | that template | default workspace / Global | nothing |
| Crewmate picked for an ordinary chat | the crewmate's | the crewmate's | nothing: the crewmate's conversation is its DM thread |
| Crewmate DM thread | the crewmate's | the crewmate's | the conversation: listed once it holds a message, ordered by its newest message, `default` included |

Rules that follow:

1. `resolve_agent_bindings` has two outcomes: a crewmate (a `config.agents` key
   in the member namespace) or a template session. A template session binds the
   named materialized template, else the default template, to `default_workspace`
   and Global memory with no crewmate model pin and no alias. The default template
   always resolves. There is no "first available alias" fallback.
2. An agent-less slot create and an empty `/agent` switch stamp the default
   template in the `template` namespace. A template session with no name of its
   own records its template as its selection.
3. The Crewmates list is a messages list. A crewmate is listed when its DM
   thread holds a message (`has_dm_message`), whoever sent it, or when the
   user starred it; an empty thread is hidden until the search reaches it or
   it is the open one. Recent order is the thread's newest message
   (`last_active_ts`, the crew log's fold). Who sent the message is not a
   rule, so there is no record of the user's own sends (`crew_recency.json`
   is removed) and no "created on the dashboard" or "is the default crew"
   exemption.
4. The landing with no `?member=` opens the remembered crewmate (this
   browser's last open), else the conversation with the newest message. The
   `default` crewmate takes part like any row; a roster holding only `default`
   still shows the empty-state hero.
5. `config.default_agent` keeps naming the roster's default crewmate: the
   **Default** badge on the Crewmates page, the undeletable row, the one
   `select_crew` keeps for itself. It is not consulted for a session that
   picked no crewmate. Renaming the knob `agent.default_agent` to say "default
   custom agent", and retiring the top-level `default_agent` crewmate alias as
   a concept, are the follow-ups in §4.
6. A control that says it sets the default for new sessions sets what rule 1
   makes new sessions run. The chat picker's **Default for new sessions** row
   writes `agent.default_agent` in the template namespace and enrolls no
   crewmate; its badge marks the default custom agent's template row and the
   row names that agent; a crewmate's chat offers no such row, since a crewmate
   is never the default for a new session. `PUT /api/config/default-agent` with
   a template-namespace name, or an unqualified name that is no crewmate alias,
   is that write; a crewmate alias still sets the roster's default crewmate of
   rule 5. The two are different settings behind one route, told apart by the
   namespace the picker chose the name in. Enrolling a template into a crewmate
   alias is no longer a side effect of that route; `/api/agents/sync` is
   untouched.

## 4. Non-goals

- Retiring the enrollment of templates into crewmate aliases by
  `/api/agents/sync`, and a slot that carries template / folder / memory
  explicitly instead of one `agent` name. Follow-ups on #18328.
- Renaming `agent.default_agent` to name the default custom agent, and retiring
  the top-level `default_agent` crewmate alias as a concept. Same follow-up.
- Any change to how a crewmate's DM thread binds.

## 5. Backward compatibility

- A config whose `default` crewmate binds a template other than
  `agent.default_agent` changes: new plain sessions run `agent.default_agent`
  (`kirocrew` when unset). Existing sessions keep their recorded execution
  context; nothing live switches. Set `agent.default_agent` to keep the old
  template for plain chats.
- A `config.default_agent` alias with its own memory store no longer lends it to
  plain sessions; `kirocrew doctor` says so beside the binding. The store serves
  the crewmate's DM thread and explicit picks.
- Legacy plain slots stamped `default` run as before and are not credited to
  the `default` crewmate's roster recency.
- The picker's ★ no longer enrolls a template as a crewmate alias; an alias it
  enrolled earlier stays in `config.agents` as an ordinary row. The Developer →
  Config "Default crewmate" select keeps writing `config.default_agent` and
  says what that setting controls (rule 5).

## 6. What this amends

- [rfc-crewmates-chatted-list.md](rfc-crewmates-chatted-list.md) §3 is replaced
  by rules 3 and 4 above: listing and order follow the thread's messages,
  whoever sent them, not the user's own sends; the chatted-with record and its
  seed are gone; creating a crewmate does not list it until its thread holds a
  message (the greeting is one). A crewmate that only ran in the background is
  listed, as its conversation holds messages -- the owner's decision of
  2026-10-09 ("就像 iMessage 一样 … 不管是谁回复的，只要有消息就要参与排序").
- [rfc-crewmate-guides-and-mate.md](rfc-crewmate-guides-and-mate.md) "`default`
  stays the fallback": "Every chat with no crew picked folds into it" is replaced
  by rules 1 and 3. `default` remains the shared, undeletable crewmate on Global
  memory; it is a roster row like any other, not a fallback for a session.

## 7. Alternatives considered

- Keep the attribution and rank `default` with a browser-side chat mark
  (#17972). Fixes the landing symptom only; the model, folder and memory
  inheritance stay.
- Keep a record of the user's own sends (`crew_recency.json`) and rank by it.
  Rejected: it is a second source of truth beside the thread, needs a seed
  and a migration, and the owner's rule is the thread's newest message.

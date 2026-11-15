---
title: Turn a chat into a Bot — a from-chat card that seeds goal, rules and a brief
status: draft
author: garohop, with kirocrew
created: 2026-11-15
last-audited: 2026-11-15
audited-at: 686d00fc11
doc-pr: null
implementation-prs: []
tracking-issues: [37663]
supersedes: []
superseded-by: []
---

# RFC: Turn a chat into a Bot — a from-chat card that seeds goal, rules and a brief

- Status: `draft`. Acceptance is requested from a maintainer; the status flips to `accepted` when one records it on this line with the date. Nothing is on main. This document lands first because the change adds a chat action and a change-card kind, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#37663](https://github.com/kirodotdev/KiroCrew/issues/37663), triaged `tier:T3`, `parked`, with the note that it "changes what the Create Bot card asks for and what a new Bot starts with".

This builds on [rfc-crewmate-share.md](rfc-crewmate-share.md) (`partial` per its index row: owner Import and Export are on main), whose `crewmate.import` card already turns a bundle of name, description, rules, a goal, crons and carried files into a Bot through existing owner routes. That document is not edited here; §5.2 (one more carried part) and §5.4 (one more way a card of that shape is raised) are amended in this document. It also amends [rfc-crewmate-greeting-as-message.md](rfc-crewmate-greeting-as-message.md) with one greeting variant, in this document.

## Problem

A chat often ends up doing a standing job: it has a goal, sources, a baseline, open items with owners, and corrections the person made along the way. The product's object for that is a Bot, but no path leads from the chat to it, and each nearby door is shut on purpose:

- `session_fork` (`POST /api/session-control/fork`, `dashboard/handlers/session_control.py`) carries the transcript but offers no `agent` override ("Deliberately not offered", `docs/system-specs/modules/session-control.md`): a fork stays inside the source's memory store, because a transcript belongs to the memory boundary it was written in. A chat cannot be forked into a Bot.
- `session_create` can bind a Bot's agent but never copies a transcript.
- The **Create Bot** card (`crewmate.create`, `src/kiro_crew/change_card_catalog_kinds.py`) takes `name`, a 200-character `goal` and an optional `schedule`. Its plan (`change_card_catalog_plans.py`; the step constants `CREWMATE_CREATE_GOAL_STEP = 1` and `CREWMATE_CREATE_SCHEDULE_STEP = 2` live in `change_card_catalog_kinds.py`) is `POST /api/agents` with `first_greeting`, `PUT /api/members/{slug}/goals/long-term`, and optionally `POST /api/crons`. The Bot it makes has an empty memory store, nothing added under Abilities, and its own empty workspace (`member_workspace_name` in `src/kiro_crew/member_workspace.py`, prefix `member-`).
- A lesson saved during the chat (`learn_add`, `POST /api/lessons`) lands in the chat's lesson silo stamped with the writer principal (`writer_stamp` in `src/kiro_crew/shared_crew/memory_changes.py`: id, kind, name), not the session, so "the rules this chat taught" cannot be selected from the store afterwards.

The path a person takes today, measured on a 0.12.2.4 install on 2026-11-15 (behind the audited base, whose `pyproject.toml` is at the unreleased 0.14.0; every code claim above was re-read at `686d00fc11`, where it is unchanged): the chat's agent reads its own transcript back (`get_chat_session`), writes a handoff brief by hand, writes it into the wrong workspace because the Bot's does not exist yet, proposes `crewmate.create`, waits for the press, finds the new workspace, copies the brief in, and the person then opens the Bot, sits through "what would you like me to do?", and tells it to read the brief and keep one section as its rules. Three turns, one misstep, and the chat's lessons are taught twice.

## Current behavior

Verified at `686d00fc11`:

1. `crewmate.create` is agent-proposable (`propose_change`), its params are `name`, `goal`, `schedule` only (`_only(p, {"name", "goal", "schedule"})`, `change_card_catalog_kinds.py`), and its apply is the three steps above.
2. `crewmate.import` (`src/kiro_crew/crewmate_bundle.py`, share RFC §5.4) is owner-only, refused from every agent caller, raised on the reserved slot `owner:crewmate-import`, and applies name, description, rules (`PUT /api/members/{slug}/rules`, `dashboard/routes/agents.py`), one long-term goal, crons (created bound to the member, paused) and carried files. A bundle whose `agent.json` carries `mcpServers` or `skills` is refused at base (`needs_mate_config`, "this version cannot import MCP servers yet"). The carried files go through `POST /api/members/{slug}/import-files` (`dashboard/handlers/crewmate_import_files.py`), which takes exactly `{member, sha256}`, re-reads the **staged copy** of the uploaded bundle by its digest (`crewmate_stage`), and writes bundled skills into the member's own workspace under `.kiro/skills/<name>/`, scripts under `crons/imported/<member id>/` with `O_EXCL`, and the avatar; the request key that admits that step is armed only for `KIND_CREWMATE_IMPORT` (`dashboard/handlers/change_cards.py`). Its create step sends `approval_mode: "normal"` and the `imported` provenance stamp, and no `first_greeting` (`_import_preview`, `change_card_catalog_plans.py`). The parts a bundle may carry are fixed (`_BUNDLE_KEYS`, `crewmate_bundle.py`): `crewmate`, `agent`, `rules`, `goals`, `crons`, `avatar`, plus skill folders and cron scripts, each at most `CARRIED_FILE_MAX_BYTES` (256 KiB).
3. A new Bot's first chat opens with a Kiro Crew-authored greeting (`maybe_start_first_greeting`, `src/kiro_crew/dashboard/mate_welcome.py`): Mate's own welcome, or a goal question in a tools and a no-tools wording (`GOAL_WELCOME`, `GOAL_WELCOME_NO_TOOLS`, and the goal-known pair). No message appears as if the person typed it.
4. A change card's string params are not redacted on the way in: the store **refuses** any param text an output redactor would change (`check_param_text`, `dashboard/change_cards_records.py`, `invalid_text`, 400), so a card either holds the text as written or is not raised.
5. Nothing links a Bot to the chat it grew out of, in either direction.

## Decision

A person can turn the chat they are in into a Bot in one press, and the Bot opens already holding the chat's goal, the rules the chat taught, and a brief of what the chat was doing. The transcript itself does not move.

### 1. A `crewmate.from_chat` card

A new change-card kind, `crewmate.from_chat`, shaped like `crewmate.import` and applied through the same plan builder:

| Param | Source | On the card |
|---|---|---|
| `session_key` | the chat the card was raised in | shown as **From chat** with its title; not editable |
| `name` | drafted from the chat title | editable; collisions resolved as `crewmate.import` does (first free name) |
| `goal` | drafted from the transcript | editable, same cap as `crewmate.create` |
| `rules[]` | drafted from the transcript: the corrections the person gave, and the text of any `learn_add` the chat made | each shown in full, each removable; written through `PUT /api/members/{slug}/rules` |
| `brief` | drafted from the transcript: job, decisions, sources, open items, what was not checked | shown in full, editable; the new carried part below |
| `schedule` | optional, as `crewmate.create` | editable |

The apply is `crewmate.import`'s plan with three named departures, in import's order: `POST /api/agents` (`approval_mode: "normal"` as import does, plus `first_greeting`, which import does not send: departure one), goal, rules, then the files step for the brief from a **synthetic bundle built at the press** (departure two, §3), then the optional schedule, with provenance `from_chat: {session_key, title, at}` riding in the create body as import's `imported` does (on the crew record, not the spec, for the reason share RFC §5.4 step 5 gives). Departure three is who raises it (§2). A later step failing lands the card `partial` with import's undo. Risk is computed from the diff as for every card; a from-chat card never carries servers, skills or scripts, so it is never `code_exec`.

### 2. Who raises it, and when

The card is raised by the chat's own agent, but only on a turn the person started from the chat's menu:

- **Turn into Bot...** is a menu action on the chat (placement is the maintainer's call: the session row menu, the thread header, or both). Pressing it sends a Kiro Crew-authored instruction into the chat as a hidden turn, the way the Bot greeting does (`dispatch_hidden_turn`, `src/kiro_crew/dashboard/mate_welcome.py`): read your transcript, draft name, goal, rules and brief, and propose `crewmate.from_chat`. No message appears as if the person typed it. (The goal button **Tell {name} what to aim for** is the other shape: it sends a visible user message through the composer, which this design does not want.)
- At base, that proposal would be refused. `propose_change` reaches the gateway as `POST /api/cards/agent/propose`, which runs `_resolve_agent_caller(request, "cards.propose")` (`src/kiro_crew/dashboard/handlers/guide.py`). That gate already refuses sub-agent, unattended (cron), app-scoped, channel-origin and non-user-sent turns for every card kind, and `cards.propose` is in `_USER_TURN_OPERATIONS`, so a hidden turn (`_turn_user_sent` false) is refused `not_user_turn`. Its one carve-out is `own_goal`, which lets `member.propose_self` raise one of `OWN_GOAL_PROPOSALS` (goal, key results, check-ins, dashboard page; and a capabilities card limited to servers this gateway already runs) on a turn the person did not send, in the Bot's own thread only, and marks the request `REQ_OWN_GOAL_TURN` so the route can tell. **Phase 1 adds a second arm of that shape**: the menu action marks the hidden turn it dispatches, `_resolve_agent_caller` admits `cards.propose` on such a turn for the one kind `crewmate.from_chat`, on a dashboard session only, once per press, and marks the request so the card route refuses any other kind. Nothing else about the gate changes: a cron, sub-agent, task-runner, wake, channel or ordinary agent-started turn is refused as it is today. This arm is the one widening in the design, and Security considerations names it.
- The press is the person's, in the chat, as for any change card. The agent never creates the Bot.

So the drafting uses the one agent that already has the chat's context, and the widening the triage note worried about (an agent that can create Bots whenever it likes) does not happen: the agent can only answer a question the person just asked.

### 3. The brief: one more carried part

Amends share RFC §5.2: a bundle may carry one `brief.md`, text only, at most `CARRIED_FILE_MAX_BYTES`. An uploaded `.kirobot` may carry it too: the import card then shows it in full like every other carried file, and `CREWMATE_FORMAT_VERSIONS` gains the version that knows the part, since that constant's own rule is that an added field bumps it. The import-files route writes it as `handoff.md` at the root of the Bot's workspace (not under `.kiro/skills/`), with `O_EXCL`; a file already there refuses the step, and the step's undo deletes exactly that file. It is content, not code: the worker reads it as any workspace file, and nothing loads it as instructions.

How it reaches that route, and when: the import-files route reads only a staged bundle by digest, re-parses it with the bundle reader, and its request key is armed only for `crewmate.import`, so a from-chat card cannot hand it a card-held string. The person may also edit `name`, `goal`, `rules` and `brief` on the card, so none of them can be frozen into a bundle at the raise (import avoids this because its only editable field is `name`, and `check_editable` refuses a change to `bundle_sha256`). And a card's plan is fixed at its last preview: each apply step is one browser request whose body must equal the stored step (`match_step`, fills aside), so a bundle first built at the press has no digest a previewed plan could carry. Phase 1 therefore gives the files route a **second body shape for this kind**, `{member, card}`, naming the card instead of a digest: inside that one request the route reads the card's final params, builds the **synthetic bundle** as the reader requires (`read_crewmate_file` refuses a bundle without `kirobot.json` and `agent.json`; `name` and `description` belong in the manifest, whose allowed keys `_MANIFEST_KEYS` include them, and `agent.json` is `{}`, since `crewmate_import_params` allows only `mcpServers` and `skills` there and refuses any other key), stages it as an upload is staged (`crewmate_stage`, `0600` under `run/`, named by its sha256, after the same wait on the start-up sweep the upload path takes), runs the same checks and writes, and discards the copy before answering. That release is new code (`_release_staged_copy` returns early for every kind but import); no copy outlives the request, and a copy orphaned by a crash is reclaimed by the import-only sweep as any other unlisted copy is. The brief's bound is the card's: `_PARAMS_MAX_BYTES` (32 KiB) on the params, with `CARD_RECORD_MAX` (64 KiB) counting its display row and the rules' PUT body as well, and each non-ASCII character counting six bytes there; the drafting instruction says to stay well under it, and `CARRIED_FILE_MAX_BYTES` never binds.

The sibling kind still joins the checks import keys by kind, or its card is unguarded: the middleware arm that admits the files step (`REQ_CARD_IMPORT_FILES`), the rules-undo digest, the cron-undo fingerprint, and the member-delete identity check (`CREWMATE_CREATING_KINDS`). Phase 1 lists them.

The brief and the rules are drafted from the owner's own chat, but they are still strings reaching a card record, and the store **refuses** rather than redacts any param an output redactor would change (`check_param_text`, which runs `needs_redaction`: the exfiltration scrubber plus `redact_via_context` with the platform's patterns, `invalid_text`). No agent tool exposes those redactors and `propose_change` passes params through untouched, so the control is on the gateway side: the raise path runs the same `needs_redaction` test over the drafted strings and refuses as it does today, and on `invalid_text` the agent redrafts without the offending text (the menu-dispatched instruction says not to paste tokens or links). The card is never raised holding something a redactor would have rewritten.

### 4. The greeting

Amends rfc-crewmate-greeting-as-message: a Bot created from a chat opens with one more greeting variant, in both the tools and the no-tools wording as the goal greetings are. It names the chat it was made from, says the brief is at `handoff.md` and that its rules came from that chat, and asks only what the brief did not settle. It does not ask "what would you like me to do?".

### 5. The link back

The source chat receives one system row, "continued as Bot <name>", with the Bot's thread linked, written when the card applies. The chat stays in Sessions, unchanged otherwise. The Bot's Profile shows **Made from chat <title>** from the provenance stamp. Undo of the card removes the row and the stamp with the Bot.

## Not decided here

- The agent offering a from-chat card unprompted (no menu press). That is the widening triage flagged; it needs its own decision once this shape has shipped.
- Any change to what a new Bot starts with under Abilities. A from-chat Bot starts as `crewmate.create` makes one: nothing added. #37508 owns that question.
- Forking or copying the transcript across the memory boundary, or copying the source store's facts. The brief is a curated summary, by design (`src/kiro_crew/docs/session-control.md`, "Combining two chats": the giver curates what to pass on instead of pasting a transcript).
- Handing the source chat to the new Bot as its manager (#37399). It could become a checkbox on this card later.

## Migration plan

- Phase 0: this document.
- Phase 1: the kind, its preview and plan (reusing import's steps, with the three named departures), the `brief.md` part, the format-version bump and the exporter's write-the-version-the-content-needs rule, the files route's second body shape for this kind with its in-request build, stage, write and release, the sibling kind joining each kind-keyed import check (§3), the menu action and the new `_resolve_agent_caller` arm, the greeting variant in both wordings, the provenance stamp and the link-back row. One pull request; the docs page on Bots gains a "From a chat" paragraph in the same diff.
- Nothing is removed or migrated. Existing Bots and cards are untouched.

## Backward compatibility

Additive, with one export rule. `crewmate.create` and `crewmate.import` keep their params and plans. A `.kirobot` file without `brief.md` imports as today; one with it imports on versions that know the part and is refused with the existing unknown-entry reason on versions that do not, which is the behaviour the share RFC chose for every unknown entry. The format-version bump (§3) must not reach every export: `FORMAT_VERSION` in `crewmate_bundle_export.py` is `max(CREWMATE_FORMAT_VERSIONS)`, so left alone it would stamp every export version 2 and older builds would refuse them all with `newer_format`. The exporter therefore writes version 1 unless the bundle carries `brief.md`, and only then the version that knows it.

## Security considerations

- The press is the owner's, in their own chat; the kind is refused outside the turn the menu action started, and from every non-person caller.
- Every string on the card is drafted by the owner's own agent from a transcript, and a transcript holds tool output, fetched pages and relayed messages, so the drafted rules and brief can carry external content. The card's full display before the press is the control: nothing is written until the person has read it. The strings are still re-checked as import re-checks a file: length caps, text only, and the store's refusal of any text a redactor would change (`check_param_text`), with the redraft-on-refusal fallback in §3. Rules are shown whole on the card because they become the Bot's permanent rules; a rule the person did not read is one they can remove before pressing.
- The brief is written as a workspace file, never into `~/.kiro/crew/skills/`, never as a spec field, never executed.
- The new Bot's reach is `crewmate.import`'s, not `crewmate.create`'s: `approval_mode: "normal"` is pinned in the create write, as import does and for the reason share RFC §5.4 step 1 gives (`crewmate.create` sends no mode, and an unset mode opens the thread in `MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE`, which is `trust`). Nothing added under Abilities, its own store and workspace. The card shows the template's inherited auto-approvals as a widening row with `widen` risk, as both existing kinds do (import's wording, "Your Bots use these without asking").
- The new gate arm (§2) is the one widening of what an agent may raise: a gateway-authored hidden turn may propose exactly one card kind, once, on the dashboard session whose owner pressed the menu action. Every other refusal in `_resolve_agent_caller` stands.

## Alternatives considered

- **Extend `crewmate.create` with `source_session`.** Fewer kinds, but every existing `crewmate.create` card would carry the gate and the import-shaped rows for a case it never has. A sibling kind keeps the create card as it is.
- **A server-side draft (an LLM call off the transcript, no agent turn).** Spends a model call the chat's agent would spend anyway, and loses the context that agent already holds. The menu-sends-an-instruction shape is what the Bot greeting and goal flows already do.
- **Select the chat's lessons from the store.** Not possible: lessons are stamped with the writer principal, not the session. Drafting them off the transcript is the only path that does not change lesson storage.
- **Fork into a Bot.** Rejected by the existing design, correctly.
- **Manual path (today).** Works; costs three turns and a brief written into the wrong folder, and teaches the Bot the chat's lessons a second time.

## Consequences

- One menu action, one card kind, one carried part, one greeting variant, one provenance stamp.
- The Bot's quality on day one depends on the agent's draft; the card is where the person corrects it before anything is written.
- #37666 (scheduled job to Bot) reuses the card shape and the brief part; it is written separately because its carry-over and surface differ.

## Sign-off

Requested from a maintainer on: (a) the sibling kind rather than extending `crewmate.create`; (b) a new one-turn, dashboard-only, menu-armed gate on the card raise path as the answer to the widening concern; (c) the `brief.md` part landing at the workspace root through a staged synthetic bundle; (d) the greeting variant.

## Open questions

1. Menu placement: session row menu, thread header, or both.
2. Whether `rules[]` arrive checked by default. Import writes every rule; here each is removable. Proposed: all on, since the person drafted them in the chat.
3. Whether a Bot made from an incognito or temporary chat should be refused (no brief could be drafted from a transcript the memory tools will not return), or allowed with an empty brief.

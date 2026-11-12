---
title: What's in this chat's context — list the steering a chat receives, then let one chat switch a file off
status: partial
author: RohanK6
created: 2026-11-08
last-audited: 2026-11-09
audited-at: 9b9ad79704
doc-pr: 33133
implementation-prs: [33600]
tracking-issues: [33112, 29940, 30158, 31952]
supersedes: []
superseded-by: []
---

# RFC: What's in this chat's context

**Status:** accepted on 2026-11-08 by maintainer bolichen97's sign-off on
[#33133](https://github.com/kirodotdev/KiroCrew/pull/33133). P1 is accepted. P2
is accepted for Crew-written text only, with native KAS and Kiro CLI v2 rows
shown as "not available" with the reason. P3 waits for the Phase 0 verdict.
P1 is on main from
[#33600](https://github.com/kirodotdev/KiroCrew/pull/33600); P2 and P3 are
open. Tracks
[#33112](https://github.com/kirodotdev/KiroCrew/issues/33112). Every main-side
reference was read at `231c0b28e7`. KAS steering behaviour is quoted from the
open [#32179](https://github.com/kirodotdev/KiroCrew/pull/32179), which is not
on main.

Credit: the asks are derreckFromSlalom's
[#29940](https://github.com/kirodotdev/KiroCrew/issues/29940) (see and choose
steering per chat), gwatsonk's
[#30158](https://github.com/kirodotdev/KiroCrew/issues/30158) (global steering
edits reach running chats) and calebpollman's
[#31952](https://github.com/kirodotdev/KiroCrew/issues/31952) (steering from the
enclosing repository root). All three are `tier:T3` and `parked`.

## Summary

A person cannot tell which steering files reach the chat they are in. This
document proposes three phases, each shippable and abandonable on its own:

| Phase | What the person gets | Changes what reaches the model? |
|---|---|---|
| P1 | A read-only list, under the chat's Context breakdown tab, of the steering files this chat receives, where each comes from, and when it applies | No |
| P2 | Switch one file off for one chat, where Crew writes that file's text itself | Yes |
| P3 | A global steering edit reaches a running chat on its next turn | Yes |

P1 only reads, so it is the one phase proposed to build ahead of a decision.
P2 and P3 change which rules a running chat follows, and maintainer bolichen97
set the bar for that on #31952:

> Loading the repository root's `.kiro/steering/` and `AGENTS.md` for a session
> started in a subfolder would change which rules reach the model in every such
> session today, so it needs a design sign-off from a maintainer.

The decision, signed on 2026-11-08 on #33133: P1 is accepted. P2 is accepted
for Crew-written text only, with native KAS and Kiro CLI v2 rows shown as "not
available" with the reason. P3 waits for the Phase 0 verdict.

## Motivation

### Current state (verified at `231c0b28e7`)

Which steering reaches a chat depends on its harness and on whether it is a
crew member's chat:

| Path | Who loads what |
|---|---|
| KAS | KAS loads every workspace root's `.kiro/steering` tree itself and honours `inclusion` (quoted from #32179); the provider reports `native_steering` (`providers/acp.py`). Per the [2026-10-28 decision](../decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md), a Kiro CLI pick also runs here whenever the installed kiro-cli offers its v3 engine |
| Kiro CLI v2 (no v3 engine) | Crew's projection overlays `chat.disableInheritingDefaultResources` on in the workspace `cli.json` (`preparation.py`, `local[_INHERIT_SETTING] = True`) and, while the recorded preference is to inherit, appends the global glob, `file://.kiro/steering/**/*.md` and `file://AGENTS.md` to its own view (the `if inherited:` block). kiro-cli then loads only the view's `resources`, and re-reads the declared guides for every request (`runtime_spawn_launch.py`, the launch steering record). The decision above schedules v2's removal for the end of November |
| Claude Code | Crew injects `[Steering resources]` at session start (`session_start.py`) and after a compaction (`turn.py`), through `steering_resources_part`. Only for the Kiro Crew default agent in a non-member chat, only from `kirocrew.json`'s `resources` plus the global and project defaults (`agent_spec.py::_load_steering_resources`), and only `always` files (`every_session_body` withholds `manual`, `auto` and `fileMatch`) |
| Codex, OpenCode, Pi, Goose, DeepSeek | No path for the global and project trees (`context._project_steering_delivered`) |
| Sidebar folder "Additional steering" (`steering_dirs`) | Crew renders `[FOLDER STEERING -- ...]` on every harness (`folder_steering.py`), skipping roots or files the harness already delivers (`inclusion._folder_steering_delivery_snapshot`) |
| A crew member's chat | Steering rides the member essentials envelope, rebuilt on every warm turn (`turn_message.py`, `if _essentials and not is_new_session`) |

What the dashboard shows today:

- The chat's Context breakdown tab (`ContextBreakdownPanel.tsx`,
  `ContextBreakdownTab`) shows a `steering` total: the engine's category on KAS
  (`ContextEngineBreakdown.tsx`, `engine_context.KNOWN_CATEGORIES`) and Crew's
  own `[Steering resources]` block elsewhere (`context_blocks.py`). No file
  names. Crew asks KAS for `summary` mode; `detailed` mode would name steering
  files by path (`acp/kas_profile.py` module docstring), and no reader asks for
  it.
- The Steering tab (`dashboard/handlers/steering.py::list_steering_blocking`,
  `GET /api/steering`) lists the global and project trees for a session's
  project, with each file's declared `inclusion`. It does not say whether this
  chat receives them, and it omits folder steering.
- `session_folder_steering_dirs` (`dashboard/handlers/_shared_workspace.py`)
  already resolves a chat's folder steering roots by session key.

There is no per-chat agent spec on main. A view alias is a digest of the agent,
the data home, the source spec's path and the view's content (`preparation.py`),
so every chat of one agent with the same view shares one alias, and
`set_mode` maps any view alias back to its source agent and sends that agent's
current view (`resolve_source_agent`, `session_handle_model_selection.py`).

### Problems

1. **Nobody can see what a chat follows.** #29940's top ask. On five harnesses
   the global and project files silently do not reach the model, and on Claude
   Code a custom agent gets none of them.
2. **No per-chat choice.** Conflicting files, or a file unwanted in one chat, can
   only be fixed by editing or moving the file for every chat.
3. **Edits arrive on some paths and not others.** On Claude Code and in folder
   steering an edit waits for the next compaction. #30158's failure is silent:
   the chat looks like it follows the current rules when it does not.

## Goals

- G1. A person can see, for one chat, every steering file that chat is expected
  to receive, its source, and whether it applies always, on request, on a file
  match or by description.
- G2. The list says plainly when a file does not reach this chat, and why.
- G3. A person can switch one file off for one chat wherever Crew writes that
  file's text, and the control says why where it cannot.
- G4. After a global or project steering edit, a running chat's next turn follows
  the new text on every path Crew writes, and the transcript says it changed.

## Non-goals

- Repository-root walk-up (#31952). It changes which rules reach every subfolder
  session and needs its own decision. P1's list makes the missing files visible,
  which is the evidence that decision needs.
- Adding a file to one chat by `#` or `@` autocomplete (#29940's second half).
  Folder steering already adds files per folder.
- Creating or editing steering files. The Steering tab already does that.
- Any change to KAS, or a Kiro CLI v2 limb for P2 (see P2).
- Measuring the model's real context. P1 reports what Crew's own gates decide
  (see Open question 3).
- Mid-turn delivery. P3 applies at the next turn boundary only.

## Design

### P1. A read-only list under Context breakdown

**Route.** `GET /api/chat/slots/{slot}/steering`, read-only, behind the same
session-key ownership check as the slot's other reads. It returns:

```json
{
  "harness": "codex",
  "files": [
    {"name": "style.md", "source": "global", "applies": "always",
     "reaches": "no", "reason": "harness_has_no_path"}
  ]
}
```

`source` is one of `global`, `project`, `folder`, `agent`, `member`. `applies`
reuses the canonical `inclusion` vocabulary the Steering tab reads
(`_head_meta`), or is `unknown` for a linked file that listing does not read; on
Claude Code its verdict then comes from the loader's own read. `reaches` is `yes`, `no`, `harness` where the harness loads
the file and decides by its own rules (KAS, and Kiro CLI v2 for its view's
declared guides), or `briefing` where a member chat's essentials path decides
per guide.

**One plan, two readers.** The verdicts come from one new function in
`src/kiro_crew/steering_plan.py`. It applies the gates assembly applies (the
member-chat essentials path, the Claude Code default-agent gate,
`_project_steering_delivered`), and where Crew writes the text it reads the writer
itself rather than a copy of its rules: on Claude Code the documents
`steering_resource_documents` returns, the list `_load_steering_resources` joins,
and for folder files the bounded section rendered at the turn's cap. A parity test
pins it to assembly: on every Crew-written path, the files it marks `yes` are
exactly the files whose bodies appear in the assembled turn. Where the plan cannot
tell which agent spec kiro-cli serves (a pin, or a project spec of that name), the
row says it cannot tell.
The scan reuses `list_steering_blocking` and runs on the discovery pool, as
`GET /api/steering` does. Paths are display paths through `_redact_meta`.

**UI.** A "Steering in this chat" section in `ContextBreakdownTab`, below the
breakdown: one row per file with its name, a source label, an "applies" label
and a plain "reaches" line ("This chat's agent harness doesn't load global
steering"). The section is a list (`<ul>` under a heading), each row a list item
with its text visible, so a screen reader reads the same facts the eye does; no
colour carries meaning alone. All strings go through the i18n catalog in the 12
shipped locales.

### P2. Switch one file off for one chat

**Where Crew writes the text, it is a filter.** Claude Code's
`[Steering resources]`, folder steering on every harness, and the member
essentials envelope are text Crew assembles, so skipping the paths a chat
excludes provably holds. The exclusion is stored per slot (paths only, no
bodies) and shown in P1's list as "switched off for this chat".

**The harness-parity question, stated plainly.** On the two native paths Crew
does not write the steering text:

- **KAS**, the default and where a Kiro CLI pick runs on a v3 host, loads each
  workspace root's `.kiro/steering` tree itself. Crew controls only the agent
  spec it hands KAS, and #32179 treats changing KAS as out of scope. Crew has
  no per-chat lever to keep one natively loaded file out.
- **Kiro CLI v2** does load only Crew's view, so a per-chat view with the
  steering globs expanded minus the excluded file would hold. But `set_mode`
  maps every view alias back to the agent's shared view, so a per-chat view
  needs a new activation path past `resolve_source_agent`, plus a frozen glob
  set that P3 must rebuild, all for an engine the 2026-10-28 decision removes at
  the end of November. This document proposes not building it.

So the switch holds for folder steering on every harness, and for global and
project files on Claude Code only. Per H6 and H14 the capability is opt-in: a
provider declares it with a safe `False` default. On every file the chat's
harness loads natively (KAS and Kiro CLI v2), the row reads "not available" with
the reason, never a toggle that does nothing. This split is the signed answer to
Open question 1.

### P3. Global edits reach running chats on the next turn

At each turn boundary, Crew compares a digest of the chat's Crew-written
steering (path and a content hash of every file P1 marks `yes`, so a
same-size edit within one mtime tick still counts) with the digest
recorded when that text was last delivered. On a change, the next turn carries
one bounded block: `[Steering updated: style.md changed]` with the new body, or
a note that a deleted file no longer applies. Member chats are exempt, since
their envelope is rebuilt every warm turn. Native paths are exempt where the
harness re-reads: Kiro CLI v2 re-reads its declared guides per request, and
whether KAS re-reads a changed or added file is P3's Phase 0. The block respects
`inclusion:` exactly as session start does, and compaction re-injection keeps
reading the current files. The transcript shows a one-line notice. Only steering
roots are watched, never text the model reads elsewhere, because steering is an
operator-written trust root.

## Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| P1 | one PR | On a Codex slot a global file is `reaches: no` with `harness_has_no_path`; on a Claude Code slot on the default agent the same `always` file is `yes`, a `manual` one is `no`, and on a custom agent both are `no`; a folder root's file is `source: folder`; a KAS slot's project file is `harness`; the parity test passes; another person's slot is refused; the tab renders a heading and one list item per file in every locale |
| P2 | one PR | An excluded file is absent from the next turn's assembled text on every Crew-written path; a file the harness loads natively reads "not available" with the reason; with no exclusion the assembled turn text is byte-identical to main |
| P3 Phase 0 | probe, verdict recorded here | Whether KAS picks up an edited and an added steering file on the next turn, measured on a real KAS session |
| P3 | one PR, blocked on Phase 0 and the sign-off above | An edit between two turns on Claude Code reaches the second turn with the notice; an unchanged set adds nothing; a non-`always` file is not pushed; a member chat gets no block |

## Test plan

P1: a backend test builds a slot on the Codex harness with one global steering
file and asserts the route marks it `reaches: no`; on main the route does not
exist, so the test fails. The parity test runs the plan and the assembler over
the same fixtures. A frontend test renders `ContextBreakdownTab` with a mocked
response and asserts the list's heading and rows by role and accessible name; on
main the section does not exist. P2 adds one assertion on the assembled turn
text with an exclusion, and a golden assertion that the turn text with no
exclusion is byte-identical to main's. P3 adds one assertion on the assembled
turn text; its test edits a file and sets the mtime explicitly with `os.utime`,
so the change never rests on the clock ticking.

## Backward compatibility

P1 adds a route and a section; nothing else changes. P2 with no exclusion and P3
with no edit produce byte-identical turns to main.

## Security considerations

P1 reads only what `GET /api/steering` and the folder collector already read,
through the same admission gate, and returns display paths, not bodies. A leaf
symlink outside its trust base is listed by name only, as the Steering tab does.
P2 narrows what reaches the model and never widens it. P3 watches only steering
roots and injects only what session start would have injected.

## Alternatives considered

| Option | Result |
|---|---|
| Do nothing | Five harnesses keep silently dropping global steering, and #29940 stays open |
| One global on/off switch per file in the Steering tab | Already possible by moving the file; it is not per chat |
| Show KAS `detailed` mode file names only | Measured, not predicted, but KAS only; a later addition to P1 (Open question 3) |
| A per-chat Kiro CLI v2 view for P2 | Holds on v2, but needs a new activation path for an engine removed at the end of November |
| A "Reload steering" button instead of P3 | A useful fallback, but the person has to know a chat is stale, which is the failure #30158 reports |

## Open questions

1. Answered 2026-11-08 on #33133: P2 ships for Crew-written text only (folder
   steering everywhere, global and project files on Claude Code), and native
   KAS and Kiro CLI v2 rows read "not available" with the reason.
2. Should P3's notice name the file only, or also show a short diff?
3. Should P1 ask KAS for `detailed` mode and show the measured file list where
   KAS serves the chat, at about 2 KB more per turn?

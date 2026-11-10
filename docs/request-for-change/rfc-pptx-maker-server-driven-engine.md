---
title: "PPTX Maker: follow SDPM v0.10"
status: draft
author: sktok
created: 2026-09-30
last-audited: 2026-11-08
audited-at: 0bdd7950d9
doc-pr: 22323
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: PPTX Maker: follow SDPM v0.10

PPTX Maker wraps the public
[spec-driven-presentation-maker](https://github.com/aws-samples/sample-spec-driven-presentation-maker)
engine (SDPM) at a pinned release. It is pinned at v0.3.8; the engine is at
v0.10.4. This RFC proposes following v0.10.4, and records the app changes that
following requires, because one of them replaces a user-facing capability and
the First Principles lane reads that decision off the base branch.

**Where it lands.** Since #30149 (merged 2026-11-05), core pins PPTX Maker to
the app repository
[`KiroCrewCaptain/kirocrew-app-pptx-maker`](https://github.com/KiroCrewCaptain/kirocrew-app-pptx-maker)
(`src/kiro_crew/apps/adoption-pins.json`, version 0.3.1 at `1c47c5099d`), and a
host moves its built-in record onto that repository at its next restart. The
implementation therefore lands in the app repository, followed by a pin bump in
`adoption-pins.json`. No app PR is open yet. Core PR #15394 changes
`src/kiro_crew/apps/builtins/pptx_maker/`, which an adopted host no longer runs,
so it does not ship this RFC. File paths below are relative to the app
repository at `1c47c5099d`.

## Summary

Following SDPM v0.10 cannot be a pin bump: the engine moved its layout, removed
tools the app's agents call, and moved each role's procedure from client prompts
into the server. The app therefore changes in two ways, both following from the
engine:

1. The four app agents (`pptx-maker-spec`, `-vibe`, `-style`, `-composer`) are
   replaced by one `pptx-maker` agent with no prompt of its own, the shape SDPM
   prescribes for Kiro CLI. The old names are removed without an alias.
2. After a Kiro Crew update moves the pin, the user installs the new engine
   with the page's existing button. Nothing is fetched automatically. The agent
   is registered again after provisioning and after a deck-root change.

Existing users keep their decks, styles, templates and pinned items. Where
"Start a deck" opens the chat is unchanged; moving it is deferred to a separate
change.

## Motivation: what v0.10 breaks

Measured on SDPM tags `v0.3.8` and `v0.10.1`, on Kiro Crew main at
`ad5b1e670`, and re-read on the app repository at `1c47c5099d`, which still
pins v0.3.8 and has the same dependencies.

| What changed in SDPM | Where the app depends on it today |
|---|---|
| `mcp-local/` → `servers/local/`, `skill/` → `sdpm/` | `backend/engine_source.py` refuses an archive without `mcp-local/` ("the engine archive has no mcp-local directory"); the agent templates load their prompts from `mcp-local/acp-agent-prompts/` |
| MCP tools `read_workflows`, `init_presentation`, `pptx_to_json`, `measure_slides`, `list_asset_sources` removed (the v0.10.1 local server registers 20 tools; the first two no longer exist at all, the other three are internal functions) | `agents/pptx-maker-*.json` list them per action |
| Role text moved to `sdpm/references/workflows/<role>.md`, served by `start_presentation` / `start_composing` / `start_style` / `start_translation`; clients name a role and hold no procedure (SDPM `principles.md`, "server-driven behavior") | the four agents load `spec-agent.md` / `vibe-agent.md` / `style-creator.md` / `composer.md` by `file://`, and those files no longer exist; `prompts/spec-studio.md` overrides the engine prompt outright ("Where this file and the engine's prompt disagree, this file wins") |

Staying on v0.3.8 is not free either: every engine fix since then is out of
reach, and `spec-studio.md` drifts further from the text it overrides with each
release.

## Goals

- Run SDPM v0.10.4 through its documented client contract.
- Keep existing users' decks, styles, templates and pins, with no manual step
  beyond one Install click.

## Non-goals

- Outline editing, a translation entry point, PPTX import/sync UI, richer slide
  animation — each is a separate change.
- Changing where "Start a deck" opens the chat. The mode buttons keep opening a
  chat page through the host's `openChat` launcher; showing the chat beside the
  deck preview is deferred to a separate change with its own decision record.
- Automatic engine updates.
- Resuming chats opened on the removed agents.

## Design

### Required by the engine

#### 1. One server-driven agent

`agents/pptx-maker.json` declares the SDPM MCP server (`servers/local/server_acp.py`),
its tools listed per action, `use_subagent`, and
`toolsSettings.subagent.availableAgents = trustedAgents = ["pptx-maker"]`. It has
no `prompt` and no `resources`. This is what SDPM's own installer generates for
Kiro CLI (`servers/local/client_config.py`): role assignment happens at dispatch,
so the orchestrator tells a spawned copy to call
`start_composing(deck_id, assigned_slugs)` first.

The Spec / Vibe / Style choice survives as a mode handed to the new chat as
one-shot silent slot context (`POST /api/chat/slots/{slot}/context`, as Papyrus
does), using the line SDPM's orchestrator reads (`Interaction mode: dialogue` /
`fast`).

Keeping four agents is possible, but it does not avoid rewriting them. SDPM
v0.10.4 has no `mcp-local/acp-agent-prompts/` (every agent's `prompt`) and no
`skill/references/workflows/` (their `resources`), and its local server
(`servers/local/server_acp.py`) no longer registers 8 of the 19 MCP tools the
spec, vibe and composer agents are granted: `read_workflows`,
`init_presentation`, `pptx_to_json`, `measure_slides`, `list_asset_sources`,
`list_guides`, `list_workflows` and `read_examples`. Each agent's prompt and tool
list is rewritten either way.

v0.10's design removes the reason to keep separate agents. One engine switches
modes itself: the MCP prompts `sdpm-spec`, `sdpm-vibe` and `sdpm-style`
(`sdpm/sdpm/tools/prompts.py`) each name an entry tool, and `start_presentation`,
`start_composing` and `start_style` (`sdpm/sdpm/entry.py`) return the role
document from `sdpm/references/workflows/`. `orchestrator.md` there accepts any
sub-agent that has the sdpm tools as a composer, including a copy of itself.
Four agents would hold the same server and tools and differ in one sentence each,
and users would see four PPTX Maker agents where one does the job.

SDPM's web-only `hearing` tool is not mounted; `@kirocrew-core/ask_question`
gives native question cards. No `autoApprove` / `allowedTools`, as today.

**Old names are removed, not aliased.** The `DEPRECATED_AGENT_SPECS` precedent in
`agent.py` keeps a renamed spec for one release. It is not followed here because
the old agents cannot run against the new engine, and PPTX Maker keeps its state
in the deck directory, not the conversation: a new chat resumes any deck through
`start_composing`. An old slot fails on its next turn with the standard
"Crew Member … is unavailable" error.

#### 2. Explicit engine update, re-registered agent

Engine code is fetched on a user click only, as today. Between a Kiro Crew
update and that click, the installed engine is v0.3.8 and the shipped agent
expects v0.10.4. The page treats that window like a first install: the source
marker no longer matches the pin, so `/engine` reports the engine as not ready
and the banner offers Install. There is no deferred registration and no
update-specific display.

- Provisioning reports `done` only after it registered the agent again under
  the app lifecycle lock, and a successful `PUT /settings` does the same. v0.10
  confines attachments and deck writes to `SDPM_DECK_ROOT`, which is rendered
  into the agent config at registration, and both first-run seeding and the
  Settings page change the deck root.
- Deck browsing keeps working before the update.
- The Library (styles and templates) is no longer promised before the update.
  `backend/library.py` reads it by running the installed engine
  (`engine.load_lists`, `engine.user_config_dir`) against the bundled
  directories under `skill/`, which v0.10 moves to `sdpm/`. Until Install, the
  tab may list only part of it or none. The user's own files in `styles/` and
  `templates/` are not touched and are listed again after Install. This is
  listed as a break below.

The update took about 20 s end to end on a v0.3.8 install.

## Backward compatibility

Breaking:

- The four agent names. Writers in the app repository at `1c47c5099d`:
  `agents/pptx-maker-*.json` and `app.json`, `backend/routes.py`,
  `backend/engine_source.py`, `ui/src/PptxMakerPage.tsx`,
  `skills/pptx-maker/SKILL.md`, `prompts/spec-studio.md` and their tests under
  `tests/` and `ui/tests/`, all changed in the app implementation PR. In core,
  the pin in `src/kiro_crew/apps/adoption-pins.json` moves to that release, and
  `docs/system-specs/modules/pptx-maker.md` is updated to match.
- The Library between a Kiro Crew update and the Install click (decision 2).

Compatible: SDPM's user config (`config.json`, `state.json`, `styles/`,
`templates/`) and the deck root are read unchanged. Verified on a v0.3.8 install
updated in place: `~/.config/sdpm` is byte-identical afterwards and existing
decks are listed. A pinned built-in style that SDPM removed is ignored by SDPM.

## Security considerations

Unchanged: sha256-pinned archive and safe extraction, per-action tool mounts
with no pre-approval, path containment and redaction on served artifacts, SVG
sanitising. The deck name taken from the outline goes through the existing
readers and is rendered only as text.

## Alternatives considered

- **Stay on v0.3.8.** No engine fixes, and an app-owned prompt override that
  keeps diverging.
- **Keep four agents with thin dispatch prompts.** See decision 1.
- **Keep the old agents and the v0.3.8 path until the user updates.** Two engine
  layouts and tool surfaces to maintain for a one-click update.
- **Update the engine automatically on gateway start.** Runs a third-party build
  without the user's action; rejected for the reason provisioning is a button
  today.
- **Alias the old agent names for one release.** See decision 1.

## Open questions

None.

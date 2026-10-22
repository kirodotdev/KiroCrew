---
title: "PPTX Maker: follow SDPM v0.10"
status: draft
author: sktok
created: 2026-09-30
last-audited: 2026-09-30
audited-at: ad5b1e670
doc-pr: 15308
implementation-prs: [15394]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: PPTX Maker: follow SDPM v0.10

PPTX Maker (`src/kiro_crew/apps/builtins/pptx_maker/`, opt-in builtin) wraps the
public [spec-driven-presentation-maker](https://github.com/aws-samples/sample-spec-driven-presentation-maker)
engine (SDPM) at a pinned release. It is pinned at v0.3.8; the engine is at
v0.10.1. This RFC proposes following v0.10.1, and records the app changes that
following requires, because two of them replace user-facing capabilities and
the First Principles lane reads that decision off the base branch.

## Summary

Following SDPM v0.10 cannot be a pin bump: the engine moved its layout, removed
tools the app's agents call, and moved each role's procedure from client prompts
into the server. The app therefore changes in two ways that follow from the
engine, and one that does not:

- **Required by the engine**
  1. The four app agents (`pptx-maker-spec`, `-vibe`, `-style`, `-composer`) are
     replaced by one `pptx-maker` agent with no prompt of its own, the shape SDPM
     prescribes for Kiro CLI. The old names are removed without an alias.
  2. After a Kiro Crew update moves the pin, deck chats stay disabled until the
     user clicks Update on the page. Nothing is fetched automatically.
- **Decided at the same time, independent of the engine**
  3. "Start a deck" opens the chat inside the PPTX Maker page, beside the deck
     preview, instead of navigating to `/chat`.

Existing users keep their decks, styles, templates and pinned items.

## Motivation: what v0.10 breaks

Measured on SDPM tags `v0.3.8` and `v0.10.1`, and on Kiro Crew main at
`ad5b1e670`.

| What changed in SDPM | Where the app depends on it today |
|---|---|
| `mcp-local/` → `servers/local/`, `skill/` → `sdpm/` | `backend/engine_source.py` refuses an archive without `mcp-local/` ("the engine archive has no mcp-local directory"); the agent templates load their prompts from `mcp-local/acp-agent-prompts/` |
| MCP tools `read_workflows`, `init_presentation`, `pptx_to_json`, `measure_slides`, `list_asset_sources` removed (the v0.10.1 local server registers 20 tools; the first two no longer exist at all, the other three are internal functions) | `agents/pptx-maker-*.json` list them per action |
| Role text moved to `sdpm/references/workflows/<role>.md`, served by `start_presentation` / `start_composing` / `start_style` / `start_translation`; clients name a role and hold no procedure (SDPM `principles.md`, "server-driven behavior") | the four agents load `spec-agent.md` / `vibe-agent.md` / `style-creator.md` / `composer.md` by `file://`, and those files no longer exist; `prompts/spec-studio.md` overrides the engine prompt outright ("Where this file and the engine's prompt disagree, this file wins") |

Staying on v0.3.8 is not free either: every engine fix since then is out of
reach, and `spec-studio.md` drifts further from the text it overrides with each
release.

## Goals

- Run SDPM v0.10.1 through its documented client contract.
- Keep existing users' decks, styles, templates and pins, with no manual step
  beyond one Update click.
- Keep the deck preview on screen while the agent works (decision 3).

## Non-goals

- Outline editing, a translation entry point, PPTX import/sync UI, richer slide
  animation — each is a separate change.
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

Four thin agents would also work, but each would hold the same server and tools
and differ in one sentence — the per-mode client wiring the engine moved out.

SDPM's web-only `hearing` tool is not mounted; `@kirocrew-core/ask_question`
gives native question cards. No `autoApprove` / `allowedTools`, as today.

**Old names are removed, not aliased.** The `DEPRECATED_AGENT_SPECS` precedent in
`agent.py` keeps a renamed spec for one release. It is not followed here because
the old agents cannot run against the new engine, and PPTX Maker keeps its state
in the deck directory, not the conversation: a new chat resumes any deck through
`start_composing`. An old slot fails on its next turn with the standard
"Crew Member … is unavailable" error.

#### 2. Explicit engine update gate

Engine code is fetched on a user click only, as today. What is new is the
window between a Kiro Crew update and that click, in which the installed engine
is v0.3.8 and the shipped agent expects v0.10.1:

- The agent is not registered while the installed engine does not match the pin.
- `/engine` reports `installedTag`, `updateRequired` and `agentReady`; the page
  shows "the engine needs to be updated (v0.3.8 → v0.10.1)" and disables the mode
  buttons until engine and agent are both ready.
- Provisioning reports `done` only after it registered the agent under the app
  lifecycle lock.
- Deck browsing and the user's own Library keep working before the update.

The update took about 20 s end to end on a v0.3.8 install.

### Decided at the same time

#### 3. Chat beside the deck

`docs/system-specs/modules/pptx-maker.md` gives the page's purpose as showing
"every deliverable appearing as it is written", but `PptxMakerPage.tsx`
`startChat` navigates to `/chat`, so the preview is off screen for the whole
build. The mode buttons instead render `components/ChatPane` — the native pane
the Crew Members page embeds — beside the deck viewer. It is resizable, the slot
is kept in the URL (`?chat=`), and "open in full chat" goes to `/chat` on request.
Native follow-ups, question cards, approvals and tool groups are unchanged
because the pane is the same surface, not a reduced embed.

This is bundled because the page's start flow is rewritten for decision 1
anyway. It can be split out if reviewers prefer; decisions 1 and 2 do not depend
on it.

## Backward compatibility

Breaking: the four agent names. Writers on main at `ad5b1e670`: the app's agent
templates, `PptxMakerPage.tsx`, `test_pptx_maker_agents.py` and the app spec, all
changed in the implementation PR.

Compatible: SDPM's user config (`config.json`, `state.json`, `styles/`,
`templates/`) and the deck root are read unchanged. Verified on a v0.3.8 install
updated in place: `~/.config/sdpm` is byte-identical afterwards and existing
decks are listed. A pinned built-in style that SDPM removed is ignored by SDPM.

## Security considerations

Unchanged: sha256-pinned archive and safe extraction, per-action tool mounts
with no pre-approval, path containment and redaction on served artifacts, SVG
sanitising. New page surfaces (outline storyboard, deck name from the outline,
layout regions) go through the existing readers and render agent text only as
text nodes.

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

- Should decision 3 ship in the same PR, or separately? The implementation
  ([#15394](https://github.com/kirodotdev/KiroCrew/pull/15394)) includes it;
  splitting it is mechanical.

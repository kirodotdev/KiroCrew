---
name: pptx-maker
app: pptx-maker
description: "Generate or restyle a PowerPoint deck. Use when the user wants to create or edit a .pptx presentation, build slides from text or a URL, or design a reusable slide style."
triggers: pptx, powerpoint, presentation, slides, deck, slide deck, keynote
---

# PPTX Maker

Builds real `.pptx` files through the PPTX Maker app, which wraps the public
`spec-driven-presentation-maker` engine. The engine's tools are exposed as the
`@sdpm/*` MCP tool family, and they are only available inside this app's own
`pptx-maker` agent — so slide generation happens by switching to that agent, not
by calling the tools from a general chat session.

## Which mode to use

Spec, Vibe, and Style are modes of the one `pptx-maker` agent. The engine owns
every role's procedure and hands it out through its entry tools:

- New or continued deck: `@sdpm/start_presentation`
- Reusable style: `@sdpm/start_style`
- Assigned slide composition (a sub-agent copy of `pptx-maker`): `@sdpm/start_composing`
- Translating a deck: `@sdpm/start_translation`

Call the entry tool before anything else and follow the workflow it returns. Use
`@kirocrew-core/ask_question` when a decision needs a question card.

If the user asks for a presentation in an ordinary chat session, point them at
the **PPTX Maker** page in the dashboard: the studio there runs this agent
beside a live preview of each deliverable, which is the experience the app is
built around. Generating a deck outside it works but shows the user nothing until
the file lands.
PPTX Maker is off by default: the page only appears once the user enables the
app, so if they cannot find it, tell them to enable it first.

## How a deck gets built

1. The agent calls `@sdpm/init_deck_workspace` to create the deck directory.
2. It writes `specs/brief.md`, then `specs/outline.md`, then the art direction.
   The studio shows each file as it appears, so write them as you finish them
   rather than all at the end.
3. Slides are composed one at a time into `slides/<slug>.json` and rendered to
   `compose/<slug>_<epoch>.json`.
4. `@sdpm/generate_pptx` writes `output.pptx` in the deck directory.

The outline's `- [slug]` lines are what determine slide order, so keep that
format.

## Styles and templates

- A **style** is an HTML document describing the visual direction. Apply one by
  name; a pinned style is the default when the user does not name one.
- A **template** is a `.pptx` whose slide layouts supply structure, theme
  colours and fonts.

Users manage both from the app's library panel, which can import their own HTML
style or existing `.pptx`. The panel shows a `@style:name` or
`@template:name` mention for the selected entry, which the user pastes into
chat — treat either mention as an instruction to use that style or template.

## Requirements

The app provisions the engine on first use and needs **nothing installed by
hand**: `uv` ships with KiroCrew as a Python dependency, and the engine is
downloaded over HTTPS at a sha256-pinned version (no `git` required).
`soffice` (LibreOffice) and `pdftoppm` (poppler) are optional and only improve
preview fidelity — `.pptx` generation works without them.

The app declares `macos`, `linux` and `windows`. The deck root is a setting in
the app, so ask before assuming where `output.pptx` lands.

The agent can also pull in an attachment (`@sdpm/import_attachment`),
analyse a template (`@sdpm/analyze_template`) and search the asset sources
(`@sdpm/search_assets`) — so restyling or extending a deck the user already has
is in scope, not just building a new one.

---
name: pptx-maker
description: "Generate or restyle a PowerPoint deck. Use when the user wants to create or edit a .pptx presentation, build slides from text or a URL, or design a reusable slide style."
triggers: pptx, powerpoint, presentation, slides, deck, slide deck, keynote
---

# PPTX Maker

Builds real `.pptx` files through the PPTX Maker app, which wraps the public
`spec-driven-presentation-maker` engine. The engine's tools are exposed as the
`@sdpm/*` MCP tool family, and they are only available inside this app's own
agents — so slide generation happens by switching to one of those agents, not by
calling the tools from a general chat session.

## Which agent to use

| Agent | Use it for |
|---|---|
| `pptx-maker-spec` | An important deck. Works through a brief, an outline and an art direction with the user, then delegates composition. |
| `pptx-maker-vibe` | A fast draft from a URL, pasted text, or a one-line brief. Few questions. |
| `pptx-maker-composer` | Slide composition itself — invoked as a sub-agent by the two above, not directly. |
| `pptx-maker-style` | Creating a reusable style guide through conversation. |

If the user asks for a presentation in an ordinary chat session, point them at
the **PPTX Maker** page in the dashboard: the studio there runs these agents
beside a live preview of each deliverable, which is the experience the app is
built around. Generating a deck outside it works but shows the user nothing until
the file lands.

## How a deck gets built

1. The agent calls `@sdpm/init_presentation` to create the deck directory.
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
style or existing `.pptx`. The panel inserts a `[Style: name]` token into chat
when they pick one — treat that as an instruction to use it.

## Requirements

The app provisions the engine on first use and needs **nothing installed by
hand**: `uv` ships with KiroCrew as a Python dependency, and the engine is
downloaded over HTTPS at a sha256-pinned version (no `git` required).
`soffice` (LibreOffice) and `pdftoppm` (poppler) are optional and only improve
preview fidelity — `.pptx` generation works without them.

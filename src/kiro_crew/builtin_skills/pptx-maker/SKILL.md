---
name: pptx-maker
description: "Generate or restyle a PowerPoint deck. Use when the user wants to create or edit a .pptx presentation, build slides from text or a URL, or design a reusable slide style."
triggers: pptx, powerpoint, presentation, slides, deck, slide deck, keynote
---

# PPTX Maker

Builds real `.pptx` files through the PPTX Maker app and the public
`spec-driven-presentation-maker` engine. The engine tools are available only to
the app's single `pptx-maker` agent. If a user asks for slides in an ordinary
chat, direct them to the **PPTX Maker** page, where the chat runs beside the live
deck preview.

## Roles come from the engine

Spec, Vibe, and Style are modes of the one `pptx-maker` agent. The engine owns
every role's procedure and hands it out through its entry tools, so this skill
only names which one to call first:

- New or continued deck: `@sdpm/start_presentation`
- Reusable style: `@sdpm/start_style`
- Assigned slide composition (a self sub-agent): `@sdpm/start_composing`
- Translating a deck: `@sdpm/start_translation`

Call the entry tool before anything else and follow the workflow it returns. Do
not restate or reorder that workflow. Use `@kirocrew-core/ask_question` when a
decision needs a question card.

## Requirements

Installing or updating the sha256-pinned engine is an explicit action on the
PPTX Maker page, and the mode buttons stay disabled until the engine and its
agent are ready. Existing decks, styles, templates and pins carry over an engine
update; a chat opened on the app's earlier agents cannot be resumed.

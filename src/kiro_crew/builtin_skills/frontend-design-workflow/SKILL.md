---
name: frontend-design-workflow
description: Workflow for frontend features, visual changes, and product design changes. Present mockup options before writing code, build against the project's design system, capture the right evidence type (screenshots vs video), and run a new-user usability review before finalizing. Use when building or changing UI, styling, layouts, animations, or any user-facing visual surface.
triggers: mockup, mockups, frontend, redesign, restyle, visual change, UI change, design change, landing page, new page, new component
---

# Frontend & Design Workflow

A disciplined workflow for frontend features, visual changes, and product
design changes. The core idea: **design decisions happen in cheap mockups
before expensive code**, and **evidence matches the nature of the change**.

## Phase 1 — Mockups before code

Never jump straight to implementation when a change involves design
decisions, even when one design seems obvious.

**Skip mockups only when there is nothing to design**: the change is
mechanical and fully specified — fixing a typo, changing a value the user
prescribed exactly ("make this #FF0000", "bump the font to 14px"), or
deleting something. If the request leaves *any* visual choice open
(layout, spacing, color selection, interaction), mockups are required.

1. Render **2–3 distinct design options** as mockups for the user to choose
   from. For small components, inline widgets work well when the chat
   surface renders them; otherwise — and for full pages or complex
   layouts — write a self-contained HTML file and share the path.
2. Make each option genuinely different (layout, density, interaction
   model) — not three shades of the same idea.
3. Build mockups with the target project's real design tokens / theme
   variables when they exist, so the options preview accurately.
4. Recommend one option with reasoning, but let the user decide.
5. **The chosen mockup IS the visual spec.** Record which option was picked.

## Phase 2 — Implementation

- Match the chosen mockup exactly: colors, shape, spacing, typography, and
  presentation details (e.g. a styled tooltip bubble, not a native
  `title=` attribute). Verify the built UI against the mockup side by side.
- Use the project's existing theme system / design tokens — never hardcode
  colors that break on theme switch. If the project has CSS custom
  properties, check whether utility-class opacity modifiers actually work
  with them before relying on them; verify **computed styles**, not class
  presence.
- Follow the project's established component library and conventions. When
  migrating to a standard library component (Radix, shadcn, etc.), adopt
  the stock look and drop hand-rolled behavior the library covers natively.
- Prefer proper icon libraries over emojis in UI surfaces.

## Phase 3 — Evidence capture

Match the evidence type to the change:

| Change type | Evidence |
|---|---|
| Static layout, styling, new component at rest | Screenshots |
| Animations, transitions, hover/focus states | Screen recording (video or GIF) |
| Multi-step flows: wizards, modals opening, drag interactions | Screen recording walking the full sequence |
| Responsive behavior | Screenshots at each breakpoint, or a recording of the resize |

A still frame cannot prove motion or sequence correctness — never submit a
screenshot as evidence for an animated or multi-step change. Share the
evidence where the change is reviewed: in the pull-request description
(following the repository's convention) when a PR workflow exists,
otherwise directly in the conversation.

## Phase 4 — New-user usability review

Before declaring the change ready, run a dedicated review from a
**brand-new, non-technical user's perspective** — as a separate sub-agent
when available, so the reviewer has no builder's context. The reviewer
answers:

- **Discoverability**: can a first-time user find this feature without
  being told where it is?
- **Plain language**: do labels, empty states, and messages make sense
  without developer jargon?
- **Zero-context comprehension**: does the screen explain itself to
  someone who never read the PR, the docs, or this conversation?

Automated code reviewers cover correctness and consistency; this review
covers first-run usability. Fold its findings into the change before
requesting human review.

## Phase 5 — Verify, then present

- Run the project's typecheck, lint, and test suites before presenting.
- Verify the actual rendered result, not just the code — use whatever
  the project provides for running the UI locally (a dev server, a
  preview build, a static file opened in a browser, an emulator).
- When reporting done: state what was verified, link the evidence, and
  compare the result to the chosen mockup.

## Iteration

When the user gives feedback on a built UI, treat it like a mockup
revision: apply targeted edits against the agreed spec, re-capture the
affected evidence, and re-verify. Do not redesign from scratch unless the
user rejects the chosen direction.

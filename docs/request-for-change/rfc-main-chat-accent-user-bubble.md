---
title: The main chat's user bubble uses the accent tone
status: implemented
author: ash663
created: 2026-10-24
last-audited: 2026-10-24
audited-at: 4d5d86af80
doc-pr:
implementation-prs: []
tracking-issues: [23358]
supersedes: []
superseded-by: []
---

# RFC: The main chat's user bubble uses the accent tone

- Status: implemented. The one-pager for
  [#23358](https://github.com/kirodotdev/KiroCrew/issues/23358) (`tier:T3`),
  proposed and built in the same pull request: in the main chat the user's
  messages and the agent's replies look almost the same. Claims about today's
  code were checked at `4d5d86af80` (main).
- Author: ash663

## 1. Today

A crewmate's chat already tells its two speakers apart by colour
([#17839](https://github.com/kirodotdev/KiroCrew/issues/17839), shipped in
[#17918](https://github.com/kirodotdev/KiroCrew/pull/17918)): `tone="accent"` on
`UserMessage` fills the user's bubble with `--accent` and `--accent-fg` text, and
the `user-bubble-accent` hook in `website/src/styles/chat-transcript.css`
re-derives every token the message's contents read (inline code, links, the sent
quote card, paste chips) from `--accent-fg`. Only the crewmate `user` entry in
`pages/chat/transcriptRenderers.tsx` passes the tone. Every other chat draws the
default tone, a neutral `bg-card` bubble beside a reply that has no bubble at
all, so in a long conversation the user's short question is lost between
replies.

## 2. Decision

The user's bubble is accent-filled wherever the main chat draws it. The tone,
its CSS and its contrast (`--accent-fg` on `--accent` is 4.64:1 or better in
every theme #17918 measured) are reused unchanged; four seams pass it:

| Seam | What draws it |
|---|---|
| ChatPage's bubble row (`pages/chat/page/transcriptRendering.tsx`) | the single-chat page, its popout, and the pages that embed it (artifact companion chat, the co-author panel, the App SDK chat panel) |
| the SDK's default `user` row (`app-sdk/messageRenderers.tsx`) | split panes and Side Chat, which draw "the SAME transcript rows as the single-chat surface" (`components/ChatPaneTranscript.tsx`), and an app's `ChatEmbed`, "Kiro Crew's native transcript" (App Kit API reference) |
| the crew window's `user` row (`pages/chat/crew-window/crewWindowRenderers.tsx`) | a session on a connected crew, opened over the chat page |
| the pinned-prompt card (`pages/chat/PinnedPrompt.tsx`) | the copy of the last prompt pinned under the title, on by default (`pinLastPrompt`); it is a pixel-for-pixel copy of the bubble, so it must change with it or the hand-off becomes a visible swap |

Side Chat is one component, so the Members page's Side Chat turns accent too,
matching that page's own accent bubbles.

## 3. Unchanged

- The reply keeps its unboxed look: no gray assistant bubble.
- A steer drawn with its badge keeps its `bg-accent-subtle` tint, which
  `UserMessage` already ranks above the tone.
- The edit-mode box stays the neutral input surface it is in a crewmate's chat.
- CLI mode (`data-ui="cli"`) draws the bubble neutral, through the hook's existing
  guard.
- The `default` tone stays the component's default, for a host that asks for it.

## 4. Alternatives considered

- **Make `accent` the component's default tone.** One line, but every host
  changes without saying so, a future one included, and the pins on the
  default tone would invert. A tone passed at each seam keeps the choice where
  the row is built.
- **Only the single-chat page.** Its split panes, Side Chat and crew window
  would then draw a second user colour on the same page.
- **A Settings → Display "high contrast roles" option** (one of the issue's
  suggestions). Every user needs to tell the speakers apart, so a switch adds a
  choice with no reason to pick the other side.
- **Right alignment or role labels** (the issue's other suggestions). The user's
  bubble is already right-aligned; alignment alone is what does not separate the
  speakers at a glance.

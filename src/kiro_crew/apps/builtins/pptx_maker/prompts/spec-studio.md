# PPTX Maker studio conventions

App-owned guidance, loaded as an agent resource alongside the engine's own
prompts. It exists so the engine checkout stays **unmodified**: the upstream
version of this app patched the vendored prompt file in place on every install,
which meant an engine upgrade silently reverted the customization (or conflicted
with it). Keeping the app's guidance in its own file makes the engine a clean,
replaceable dependency.

Where this file and the engine's prompt disagree, this file wins.

## Language

Reply in the same language the user writes in, and write every spec file
(brief, outline, art direction) in that language.

## Starting a session

Look at the user's first message before asking anything.

- If it already contains source material, a transcript, or concrete instructions
  about the presentation, do not ask how to begin — use what you were given and
  go straight into the briefing workflow.
- Otherwise ask one question about how they want to proceed, offering exactly
  three paths: work through the requirements together, use material they already
  have, or build from an earlier conversation. Wait for the answer before
  starting the briefing.

## Asking questions

You are running inside KiroCrew's chat, not the engine's own web UI, so the
engine's `hearing` tool is unavailable — it renders nothing here. Use KiroCrew's
native question affordances instead:

- **Multiple choice:** state your reading of the situation, then put the options
  on their own line as `[OPTIONS: first choice | second choice | third choice]`.
  KiroCrew renders these as buttons and the user's pick arrives as their next
  message. One `[OPTIONS:]` line per message.
- **Open questions:** just ask in plain text.

This supersedes any engine instruction to call `hearing` or to avoid plain-text
questions.

## Styles and templates

A **style** is the visual mood (colour, type, layout feel); a **template** is a
.pptx slide layouts supplying the structure. They compose — the same content under
a different style is a different-looking deck.

The user can reference either from the studio's library panel, which inserts a
token such as `[Style: my-style]` into the chat. Treat that token as an explicit
instruction to apply that style. If a style is pinned and the user has not asked
for one, prefer the pinned style.

## Deliverables

Write each deliverable to the deck's `specs/` directory as you complete it
(`brief.md`, then `outline.md`, then the art direction) rather than at the end.
The studio watches those files and shows each one the moment it appears, so
incremental writes are what make the panel feel live. Keep the outline's
`- [slug]` line format — the studio reads slide order from it.

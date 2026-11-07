# The memory mode chip offers a private chat, and its way back is a regular chat

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-07

## Decision

With nothing picked, the composer's memory mode chip reads "Start private
chat"; with a mode on it reads "Incognito — switch to regular chat" or
"Temporary — switch to regular chat", and the sign-in link refusal says
"Switch to a regular chat, then create the link." Settings (Chat → Sessions →
Default Memory Mode) keeps the mode name "Persistent", and the two cards, their
copy, the ghost icon and the colours stay as they were.

## Why

- "Choose memory mode" read as picking which memory to use, while the choice is
  a private kind of chat: memory is the main change, but goal loops, lasting
  schedules, sign-in links and chat-history search turn off too.
- "Persistent" first appeared on the way back, so the way back used a word the
  person had not seen. "Regular chat" is the common name for the default chat.
- The dashboard already calls these chats "private" ("private sessions
  skipped").
- The sign-in refusal tells the person to take the chip's way back, so it names
  it the same way. In Settings "Persistent" sits next to Incognito and
  Temporary with a description of all three, so the word does not come up
  unexplained there.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/31599#issuecomment-6705018258 --
  the maintainer decision on the issue.
- https://github.com/kirodotdev/KiroCrew/issues/31599 -- the issue, with the
  suggested wording and the Settings scope question.

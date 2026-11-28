# A confirmed file or folder link has the path chip's right-click menu, headed by the path it points at

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

A Markdown link whose destination the dashboard has confirmed is a local file or folder opens, on right-click (on touch, a long press), the same menu a confirmed path chip opens, with every row under the chip's own rules (Open in side panel, Show folder in side panel, Open in editor, Open with default app, the file-manager reveal, Copy path); the link's menu starts with the path the link points at, written out in full, and that path is the menu's accessible name; a plain click and Shift+click do what they did before.

## Why

- The issue asks for a choice, on a chat file link, between Kiro Crew's own viewer and the default app; the path chip and the file browser already offer it.
- A link's text is written by its author and can name one thing while pointing at another, so the menu shows and announces the path before any row opens or runs it, the way a chip's own text is its path.
- On a confirmed link this menu takes the right-click gesture (on touch, the long press), as it already does on a confirmed path chip. Whatever menu that gesture opened on the link before (the chat message's own right-click menu, or the browser's or the desktop app's) no longer opens from the link itself, so none of its items is offered there. Copy link text is one of them; its loss on such a link is accepted, because the text stays selectable and Copy path copies what the link points at. Where a message has its own actions, they stay on a right-click elsewhere in the message. A link that is not confirmed keeps whatever menu it had.
- The rows that act on a computer (Open in editor, Open with default app, the reveal) follow the chip's rules, so a link's menu offers them exactly where a chip's menu does.
- The raw Markdown view the issue also asks for already exists (Edit on a side panel file tab). A setting for what a plain click does is kept as a separate request.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/39614#issuecomment-7147379407
- Issue: https://github.com/kirodotdev/KiroCrew/issues/39614
- The open design choices listed on the issue: https://github.com/kirodotdev/KiroCrew/issues/39614#issuecomment-7030650652

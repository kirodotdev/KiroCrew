# The Bots page lists the bots on the user's other crews in a section of their own, below the local bots

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

The Bots page shows the bots on each connected remote crew in a separate section below the local list, one group per crew under that crew's name, never mixed into the local rows; the page's search box narrows those rows too, a click opens that crew's window, and a crew that cannot be read shows a row saying so instead of being left out.

## Why

- It gives the one list with the crew name next to each bot that the request asks for, and leaves the local Bots list, its filters and its counts as they are.
- Each crew's list is read on the server through the same read-only roster read the crew window already makes, so the remote-crew proxy and what a crew lets this machine reach stay as they are.
- That read carries no field the crew's own Bots page picks its default list by, so a group lists the crew's whole roster; the search box is what finds a remembered name in a long group.
- Opening the bot's own page inside the crew's window is left to a follow-up issue.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/39808#issuecomment-7110600695 -- the maintainer's decision comment.
- https://github.com/kirodotdev/KiroCrew/issues/39808 -- the request this decision answers.
- https://github.com/kirodotdev/KiroCrew/issues/41490 -- the follow-up for opening the bot's own page.

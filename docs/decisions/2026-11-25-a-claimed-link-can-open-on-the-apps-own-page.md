# A claimed link can open on the app's own page: a `linkOpen` entry may declare `page` instead of `entry`

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25

## Decision

A `contributes.linkOpen` entry may declare `"page": { "param": "<name>" }` instead of `entry` (exactly one of the two, with `ui.entry` and a `ui.pages` entry): after the unchanged approval, question and "Always", choosing the app opens the app's own page, where the sidebar opens it, at `?<param>=<link>` (no query or fragment unless `fullUrl` is approved), with no dialog, no frame and no new tab; a popped-out page of the app loads that address and is asked to come forward; the main window navigates in-app after the page on screen agrees to be left, and on an app's page or on Projects or Channels, which cannot be asked, the click stays an ordinary link.

## Why

- The issue asks for the app's page directly, without the dialog, the second click and the stray tab, and for the pop-out the person arranged to receive it; a declarative target runs no app code on the click and adds no frame capability.
- There is no `route` field: the host already knows each app's one destination (the one the sidebar and the command palette use, which is also the pop-out's id), so a second, manifest-chosen route would only need checking.
- A pop-out loads the new address rather than routing in place, because an installed app's page gets no router and would not see a change of query; the load replaces what the page held, so an app that keeps unsaved work there guards it with `beforeunload`.
- An app's page registers no leave check, so leaving it for another app's page could discard its work without asking; there the link keeps today's behaviour.
- "Always" is remembered per entry, so an app that switches an existing entry from `entry` to `page` changes what a remembered "app" answer does: it opens the page instead of the dialog.
- Not decided here: an entry that declares both and lets the person choose, and same-window routing of other dashboard links (#26442).

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/42165#issuecomment-7169269819
- Issue: https://github.com/kirodotdev/KiroCrew/issues/42165
- The point it extends: https://github.com/kirodotdev/KiroCrew/issues/27733

# The browser tool's js runs with no card under YOLO, "Trust all tools" or a site's act grant

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-25
Supersedes: 2026-11-19-the-browser-tool-runs-harness-js-cdp-and-http-get.md (for `js` only), 2026-11-20-browser-upload-takes-folders-globs-pickers-and-drops.md (its "`js` keeps its card" clause only)

## Decision

The browser tool's `js` op runs with no card on a person's dashboard chat that trusts all tools or that a live YOLO reaches, and on a site that chat trusted for reading, clicking, typing and uploading (an act grant), there only in the current tab; without either it asks on its card as before, a read grant does not lift it, an app's slot keeps it, and `cdp` and `http_get` still ask on a card every call.

## Why

- The owner's words, on 2026-11-25 at 00:06 PST: "写个PR改一下 YOLO和trust情况下 browser 用JS不需要approval" (write a PR so that under YOLO and trust, browser JS needs no approval). He named `js` only.

## Evidence

- The ruling was given by Bolin Chen in the conductor chat that dispatched this work, on 2026-11-25 at 00:06 PST, quoted above verbatim and restated on its issue: https://github.com/kirodotdev/KiroCrew/issues/42490#issuecomment-7157293078
- `docs/decisions/2026-11-19-the-browser-tool-runs-harness-js-cdp-and-http-get.md`: the card-every-call ruling this narrows for `js`.

# The browser tool runs Browser Harness's own js, cdp and http_get, with a card on every call

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-19

## Decision

The `browser` tool offers Browser Harness's own `js`, `cdp` and `http_get` helpers as ops, which reverses the browser RFC's non-goal that kept them unreachable. Each call asks a person on its own card, every time, whatever the agent's spec auto-approves: the op route puts an Allow / Deny card on the caller's dashboard slot and runs the step only on an Allow given for that exact call, one answer per call. No site grant (read or act), "Trust all tools", trust-reads, YOLO, a trusted pattern, the hook auto-approve list, an app's `browserSites` grant or the default spec's `@kirocrew-core` grant skips that card, and a session with nobody to ask refuses the call.

## Why

- The owner's words, on 2026-11-19 at 21:30 PST, when asked whether to expose Harness's `js`, `cdp` and `http_get`: "三个都放开支持 不用弹卡" (support all three, no card).
- The owner's words, on 2026-11-19 at 23:05 PST, superseding that: "改成这三个每次都弹卡" (make these three show a card every time). `cdp` can read every site's cookies and drive every tab, and `js` runs any script in the page, so a standing approval for one site or one session does not cover what one call can do.

## Evidence

- Both rulings were given by Bolin Chen in the conductor chat that dispatched this work, on 2026-11-19 at 21:30 PST and 23:05 PST, quoted above verbatim and restated on the pull request: https://github.com/kirodotdev/KiroCrew/pull/39990#issuecomment-7045950949
- `docs/request-for-change/rfc-browser-live-harness.md`, Non-goals: the reversed "Arbitrary code through Harness" item.

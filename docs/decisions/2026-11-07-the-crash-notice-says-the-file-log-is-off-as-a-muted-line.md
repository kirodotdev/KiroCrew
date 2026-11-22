# The crash notice says the file log is off as a muted line, not as an error

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-07

## Decision

On a launch whose Chromium file log is off, the crash notice says that
`chromium.log` is from an earlier session and why, as one muted info line in
the banner, one translated sentence per cause code from a closed set; it is not
an `ErrorNotice` and offers no second "Ask the agent". The renderer receives
only the cause code, never a path or an errno text.

## Why

- For most causes (`in_use`, `no_access`, `unreadable`, `other`) the user can do
  nothing about it, so the line informs and does not alarm.
- The line already says `chromium.log` is from an earlier session, which is the
  thing the person filing a crash report needs to know.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/31022#pullrequestreview-5810605404 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/19623 -- the issue.

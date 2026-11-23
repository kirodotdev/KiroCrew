# The crash notice shows when the newest new crash happened

Decided by: Bolin Chen (@bolichen97), maintainer
Date: 2026-11-14

## Decision

The crash notice shows when the newest new crash happened, in one muted
"Latest crash: {date}" line directly under the count, with the date and time in
the app's language; the renderer receives it as one ISO 8601 UTC `lastCrashAt`
(or `null`, which omits the line), and the payload never carries a path, file
name, module name, exception code or the logs directory.

## Why

- The count-only comment listed timestamps among the fields that describe the
  machine, and said no consumer read `lastCrashAt`. This decision reverses that
  for one field, the newest crash's time: it names no path, file, module,
  exception code or account, the same time is in the `crashes.log` the notice
  asks you to attach, and the notice now reads it.
- The line shows when the newest crash happened. It does not list each crash's
  age, which stays out of scope.
- The title, the count, dismissal, "Show diagnostics" and "Report issue" (its
  note stays count-only) are unchanged, and so are the channel's three gates.
  If the native-logging cause line from #31022 also lands, the order is: the
  count, then this line, then the cause line.
- This does not limit other closed-set, path-free fields, such as the cause code
  in #31022.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36371#issuecomment-6875102211 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/36371 -- the issue.

# Update All on Discover moves only its chat-control consent lines out of the red error banner

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

On Apps > Discover, Update All shows its chat-control consent lines in a
warning-styled status notice; failures, refused updates (an app that needs
permission or needs its repository trusted) and a refused backend stay on the
red error banner.

## Why

- The consent line reports an update that landed and left the app disabled, so
  nothing failed; the other lines report a refused request or a backend that
  did not start, which `errors-use-error-notice` keeps on the error surface.
- Moving the two refusal lines out would need a carve-out in
  `errors-use-error-notice`; this decision does not make one.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33452#issuecomment-6867734400 --
  the maintainer decision.
- https://github.com/kirodotdev/KiroCrew/issues/33452 -- the issue.

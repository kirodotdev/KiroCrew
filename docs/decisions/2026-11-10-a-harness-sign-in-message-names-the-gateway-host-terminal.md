# A third-party harness's sign-in message names the gateway host and the Terminal panel

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

A message that sends the user to a third-party harness's own sign-in says it
runs in a terminal on the gateway host and that, after setup, the gateway owner
can open such a terminal from the dashboard's Terminal panel, where that panel
is on; the Strands key messages name its Settings → Agent Harness → Strands →
Model provider card; in-product per-vendor Sign in and Install buttons are not
planned.

## Why

- Dashboard-only users met a dead end: "run it in your terminal", with no
  machine named and no dashboard path, although the Terminal panel and the
  Strands card already exist.
- The Terminal clause is conditional because the panel is owner-only, absent
  from first-run setup, and can be turned off by config or a security policy.
- Each vendor sign-in is its own flow, and an Install job would be a new way to
  install software on the host that needs its own design and security review.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/30605#issuecomment-6757030634 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/30605 -- the issue.

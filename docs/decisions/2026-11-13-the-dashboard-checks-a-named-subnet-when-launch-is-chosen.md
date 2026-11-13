# When the cloud launch form names a Subnet ID, the dashboard checks that subnet when Launch is chosen, before any setup starts, and shows the problem under the Subnet ID field; the launch's own check stays

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

When the cloud launch form names a Subnet ID, the dashboard checks that subnet
when Launch is chosen, before any setup starts, and shows the problem under the
Subnet ID field; the launch's own check stays.

## Why

- The check ran only inside the "Create the instance and install Kiro Crew"
  step, after the launch had started, so its error appeared away from the field
  that caused it.
- The same check and message, run earlier, need no new step or screen.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34898#issuecomment-6853270851 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34898 -- the issue.

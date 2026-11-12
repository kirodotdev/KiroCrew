# A Linux system-service install turns linger on for its account

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-11

## Decision

`kirocrew service install` on Linux turns linger on for the account the service
runs as, unless it is already on or the host's systemd ships no per-user manager,
and prints that it did and how to turn it off again; it does this on every
install, a re-install included, and when that step fails the install still
succeeds and prints the command to run by hand; `kirocrew service uninstall`
leaves linger as it is.

## Why

- The report asked `kirocrew service install` to turn linger on itself, since
  it already runs with root access for the system unit.
- A system install is chosen so work outlives the login session, and without
  linger the agent runtimes still stop at the account's last logout.
- The cost was stated with the decision: linger also keeps that account's other
  user services running from boot.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34691#issuecomment-6785281704 --
  the maintainer's decision.
- https://github.com/kirodotdev/KiroCrew/issues/34691 -- the report.
- https://github.com/kirodotdev/KiroCrew/pull/15851 -- the advisory warning this
  replaces, which left the placement open.

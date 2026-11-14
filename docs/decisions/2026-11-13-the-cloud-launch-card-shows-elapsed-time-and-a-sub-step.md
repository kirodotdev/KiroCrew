# The cloud launch card shows elapsed time and a sub-step while a crew installs

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

While a cloud launch's provision step runs, the launch card and the "Setting up"
row show "{elapsed} so far" from Launch, a built-in job past 6 minutes reads
"Past the 6-minute estimate, {elapsed} so far" (never "longer than usual"), and
the step's detail names one of two sub-steps from the stack events, "Creating the
instance…" then "Installing Kiro Crew on the instance…", below any network
fallback sentence; there is no separate "Starting the crew" sub-step and the
pre-launch "Ready in about 6 minutes." is unchanged.

## Why

- The install is the longest wait of a setup and is billed; a spinner with no
  time or sub-step past "about 6 minutes" reads as a hang and invites a cancel.
- Stack events can tell the instance being created from the install, but not the
  install from the crew starting, so only two sub-steps are honest.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/32099#issuecomment-6856013954 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/32099 -- the issue: the dashboard
  showed one spinner for 6 to 10 minutes with no elapsed time or sub-step.

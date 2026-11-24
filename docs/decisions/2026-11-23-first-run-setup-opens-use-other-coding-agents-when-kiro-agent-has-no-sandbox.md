# First-run setup opens Use other coding agents when Kiro Agent cannot start because the host has no sandbox

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

On the first-run "Set up Kiro" page, "Use other coding agents" starts open when the configured agent is Kiro Agent, its probe row is present, and it cannot start because the host cannot provide Kiro Crew's sandbox, except on a workload pod that shows its own hint above the section.

## Why

- The section's notice is the only element that says why the page holds when the configured Kiro Agent is sandbox-blocked, and installing Kiro CLI, the visible card, does not give the host a sandbox.
- The readiness check stays as it is: Kiro Agent really cannot start there, so the page is right to hold.
- Every other Kiro Agent case keeps the collapsed default of #35993 (#34949): there the Kiro CLI card is the remedy or the case is out of scope.
- A workload pod keeps its own hint above the section, which already names the setting, open or not.
- The default is taken once when the section mounts, and a click still toggles it.

## Evidence

- The decision: https://github.com/kirodotdev/KiroCrew/issues/39694#issuecomment-7110828061
- The report: https://github.com/kirodotdev/KiroCrew/issues/39694
- The collapsed default for Kiro Agent: https://github.com/kirodotdev/KiroCrew/pull/35993 (fixing https://github.com/kirodotdev/KiroCrew/issues/34949)
- The sandbox reason in the section's notice: https://github.com/kirodotdev/KiroCrew/pull/38952

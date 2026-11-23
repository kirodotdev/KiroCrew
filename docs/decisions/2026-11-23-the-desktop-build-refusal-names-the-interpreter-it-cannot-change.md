# The desktop build's Python build-step refusal says Kiro Crew's own interpreter cannot be installed into

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

When the desktop build refuses a Python build step, its message says that Kiro Crew's own interpreter is inside the signed application bundle and cannot be installed into, never that the desktop build cannot install packages.

## Why

- The desktop build does install an app's `requirements.txt`, into the app's own dependency folder, for the app's Python backend process and stdio MCP servers; the sentence that said its bundled interpreter "cannot install packages" was not true.
- What it cannot do is install into its own interpreter, which a build step installs into and which code declared in `backend.hooks` imports from.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/40222#issuecomment-7091397429
- Issue: https://github.com/kirodotdev/KiroCrew/issues/41130
- Reported in: https://github.com/kirodotdev/KiroCrew/issues/40222

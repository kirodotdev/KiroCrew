# A gateway's own name is the `dashboard.instance_label` setting, not an instances-registry entry

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-06

## Decision
The local gateway's label is a standalone `dashboard.instance_label` setting next to `dashboard.theme_color`, empty by default (empty keeps today's tab title, favicon and transfer origin). When it is set, the browser tab reads `[(N) ]<detail> — <label> — <bot>` (or `[(N) ]<label> — <bot>`), a generated favicon on the theme accent comes after a theme's favicon and before the stock icon, `index.html` titles the tab from a cached copy before the app loads, and a session transfer names it as its origin. The instances registry stays remote-only, and the `__local__` placeholder and the pins stored under it are unchanged.

## Why
- The setting gives every gateway an identity, a pod or tunnel tab opened directly included, with one additive key in each data home, where `theme_color` already lives.
- The instances registry describes remote connection details only and never stores the local instance, and it belongs to the opt-in Instances feature, so a standalone gateway does not run it.

## Evidence
- The decision, on the issue: https://github.com/kirodotdev/KiroCrew/issues/6592#issuecomment-6696429702
- The issue that asked for the call: https://github.com/kirodotdev/KiroCrew/issues/6592
- The tab-title layout it builds on: https://github.com/kirodotdev/KiroCrew/pull/18028 and https://github.com/kirodotdev/KiroCrew/pull/30434

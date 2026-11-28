# In the desktop app, Connect with WakaTime finishes in the system browser

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-14

## Decision

In the desktop app, Connect with WakaTime opens this dashboard's WakaTime panel
(`/settings/overview?view=wakatime`) in the system browser, where the person
clicks Connect and finishes with WakaTime (signing in to the dashboard first if
that browser is not signed in); back in the desktop app the panel reads the
connection again and shows the account as connected. The shell opens only that
fixed address of its own local dashboard, takes nothing from the page, and the
window's navigation guard stays as it is. Windows connected to a crew on another
machine keep today's note. There is no sign-in link or token in any URL, no
in-app sign-in window, and nothing starts the WakaTime sign-in from a link by
itself.

## Why

- The WakaTime callback only completes in the browser that started the
  sign-in, and this keeps both steps in the system browser with the smallest
  change to the desktop shell.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36948#issuecomment-6879224858 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/36948 -- the issue.

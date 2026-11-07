# The Terminal panel switch is editable from Settings

Decided by: proposal, accepted at the review of the pull request that adds this entry
Date: 2026-11-06

## Decision

`dashboard.terminal.enabled` is a Settings control: Settings → Display → Terminal
carries an owner-only **Terminal panel** switch as the card's first row. Off hides
every Terminal entry point (rail, dock, add-menu item, Run buttons, shortcuts) and
ends every running shell; the switch confirms the kill when shells are live. The
config file stays the one source of truth: `config.json`, `config.local.json` and
`kirocrew config set` remain valid writers of the same key, and the switch writes
through the same config PATCH route as the other Terminal rows. Hiding applies only
to an explicit `false` from the gateway; when the probe fails, a WebSocket is
refused, or the terminal is reached through the popout window, the user sees a
notice naming the cause and pointing to the switch.

## Why

- The schema kept the flag config-file-only because an off-flip also kills the PTY,
  which is not a display preference. With the live config applier ending every shell
  and expiring the gate cache on an off-flip, the switch's effect is complete, so the
  reason no longer holds.
- A dashboard whose terminal was turned off by configuration dead-ended silently:
  the entry points showed, a refused socket looked like a network drop, and nothing
  named the setting or where to change it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/31652#issuecomment-6697781375 -- the
  proposed direction, on record on the issue: hide the Terminal button when off, with
  a switch in Settings rather than config alone.
- https://github.com/kirodotdev/KiroCrew/pull/31579 -- the live teardown this switch
  depends on.
- https://github.com/kirodotdev/KiroCrew/issues/31652 -- the report.

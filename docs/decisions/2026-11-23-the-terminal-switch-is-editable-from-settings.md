# The Terminal panel switch is editable from Settings

Decided by: Ray Xu (@buluoray), maintainer
Date: 2026-11-23

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
notice naming the cause and pointing to the switch. A security policy that refuses
the terminal (`boot.allow_terminal: false`) locks the switch: the row is read-only
with the locked-by-policy note, the notices name the policy, and the enable is
refused.

## Why

- Every setting a user needs must have a UI path. A user who turns the terminal
  off has no way back without a shell, because the only writer of the key was the
  config file.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/31967#issuecomment-7111662597 -- the
  maintainer's ruling on the pull request that adds this entry: editable from
  Settings, owner-only, policy lock honoured, confirm before ending open shells.
- https://github.com/kirodotdev/KiroCrew/issues/31652 -- the report.
- https://github.com/kirodotdev/KiroCrew/issues/31652#issuecomment-6697781375 -- the
  direction first put on record on the issue: hide the Terminal button when off, with
  a switch in Settings rather than config alone.

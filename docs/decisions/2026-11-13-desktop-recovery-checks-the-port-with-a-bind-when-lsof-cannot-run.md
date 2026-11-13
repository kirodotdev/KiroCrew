# On macOS and Linux, when the desktop app cannot run its port check during crash recovery or a force retry, it checks with a bind that the port is free before starting the gateway again, and shows the restart-required dialog when the port stays in use

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-13

## Decision

On macOS and Linux, when the desktop app cannot run its port check during crash
recovery or a force retry, it checks with a bind that the port is free before
starting the gateway again, and shows the restart-required dialog when the port
stays in use.

## Why

- Always showing the dialog would stop recovery on machines without lsof even
  when the port is free.
- The old behaviour started a gateway on a port nobody checked, so a port that
  was still held failed the new gateway with "address already in use" instead
  of showing the restart-required dialog.
- Windows still goes straight to the dialog on a failed check: its netstat
  ships with the operating system and is already retried, while lsof can be
  missing for good.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/34921#issuecomment-6852228389 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/34921 -- the issue.

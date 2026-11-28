# A headless Windows desktop install updates from its gateway

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-26

## Decision

A Windows desktop install whose gateway runs without the desktop app (started from the bundled `kirocrew.cmd`, so `KIROCREW_DESKTOP_GATEWAY` is unset) checks the desktop app's own release feed for its channel and offers the update through the existing contract (`can_apply`, `POST /api/update`, Restart, `kirocrew update`); the update is applied only at a restart or stop a person causes, by a detached helper that waits for the gateway to exit, verifies the signed installer again (the feed's sha512 and size, a valid Authenticode signature with a timestamp, a signer the app's updater pins), after the feed's version was checked to be newer than the running one, runs it silently in the scope the install was made in with `/D=<install dir>`, checks the installed version and starts the gateway again; any failure leaves the old install and is reported as `pending_apply_error`. A gateway the desktop app hosts keeps deferring to the app.

## Why

- Without a window there is no updater at all: the check deferred to an app that is not running, `POST /api/update` answered `409 update_check_pending` for ever, and `kirocrew update` exited 0 having done nothing.
- The installer replaces the files the gateway runs from, which Windows keeps locked, so the gateway cannot install it itself; and updates never restart a running gateway, so the apply waits for the person's restart or stop.
- The checks are the ones the app's own updater makes, read from the app's own settings, so this path accepts no installer the app would refuse; an overridden update source and the unattended loop are left out, as ruling 1 of the issue leaves them to the host.

## Evidence

- Maintainer ruling: https://github.com/kirodotdev/KiroCrew/issues/43374
- Updates never restart a running gateway: `docs/decisions/2026-11-10-updates-never-restart-a-running-gateway.md`

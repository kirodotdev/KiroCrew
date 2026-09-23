---
name: self-update
description: Check for and apply Kiro Crew updates. Use when user says "update yourself", "check for updates", "are you up to date", "keep yourself updated", or "auto-update".
triggers: update yourself, check for updates, new version, out of date, latest version, auto-update, keep updated
---

# Self-Update

## Overview

Check for available Kiro Crew updates and use the install layout's supported apply path. The gateway already performs periodic update checks; do not add a duplicate cron.

`kirocrew update` behaves differently on three install layouts, and which one the
user is on decides everything below:

- **git checkout** — fetch, then `reset --hard` to the upstream tip. Only a
  fast-forwardable checkout is updated; a DIVERGED checkout (local commits both
  ahead and behind) is refused, because the reset would discard them.
- **wheel / cli.sh** — fetch the release feed, compare versions, re-run the
  installer.
- **externally managed (desktop app, Docker)** — prints guidance and returns
  without updating. The desktop app updates itself over its own OTA channel.

There is no separate "check" subcommand.

A policy-defined update provider, when one is configured, owns updating this host:
it runs before any layout dispatch, the built-in mechanism never runs, and there is
no fallback — `kirocrew update` exits 1 on provider failure. Report the provider's
failure; do not try to route around it.

## Core Concepts

### Checking for Updates

Non-destructive — safe to run anytime. Report the installed version:

```bash
kirocrew --version
```

Then use that layout's own source of truth: the tracked upstream branch for a git
checkout, the signed release feed for a wheel install, or the app's own updater
for desktop. Do not compare a checkout against public tags; source updates are
based on commit distance from its configured upstream, not release tags.

### Applying Updates

To apply an available update:

```bash
kirocrew update
```

The gateway must be restarted afterward for the new version to take effect.

Two more verbs:

```bash
kirocrew update approve   # approve a pending in-app update armed from the dashboard
kirocrew update --force   # git installs only
```

`--force` is the destructive one: on a diverged git checkout it lets the hard reset
**discard local commits**, recoverable only from `git reflog`. Never run it without
saying that first.

### Automatic Update Checking

Do not create a second update-check or auto-apply cron. The running gateway
already checks at startup and every 12 hours, and exposes the layout-aware
verdict through **Settings → About** and `GET /api/update/check`.

The built-in capability selects the correct owner:

- **git checkout / wheel install** — Kiro Crew checks the tracked upstream or
  signed release feed and reports a nullable `update_available` verdict.
- **desktop app** — the Electron/package OTA updater owns checking and consent.
- **Docker** — the image is immutable; update by pulling/redeploying an image.
- **policy-defined provider** — the provider owns both check and apply, with no
  fallback to the built-in mechanism.

A completed check is `check_status="succeeded"`; only then does
`update_available=false` mean “up to date.” A failed, deferred, or unchecked
comparison is not a negative verdict.

For automatic application, use the product's existing `auto_update`/policy and
channel controls rather than scheduling a lifecycle command in a cron. The
update path already owns checkout safety, memory snapshotting, install-layout
dispatch, drain, and restart behavior.

### Limitations

- BOTH `kirocrew restart` AND `kirocrew update` are blocked when run directly from an agent session, by the self-protection argv floor (it reads the command's argv, so mentioning the words in a path or a grep pattern is not blocked, and there is no opt-out).
- A policy-defined update provider, where configured, owns updating the host and there is no fallback: `kirocrew update` exits 1 if it fails. Report that failure rather than working around it.
- Because the agent shell cannot run lifecycle commands, apply through the authenticated dashboard when the capability offers it, or ask the user to run the displayed terminal command. Do not smuggle update or restart through a custom cron. A terminal update may still require the user to restart; an in-app apply owns its own drain/restart flow.
- After an update + restart, the resuming session runs the new code.

## Usage

### User asks "am I up to date?" or "check for updates"

1. Run `kirocrew --version` to show the installed version.
2. Read the layout-aware verdict from Settings → About or `GET /api/update/check` when that authenticated surface is available; otherwise compare against the layout's own source of truth.
3. Report `check_status` as well as `update_available`: only a succeeded check with `false` proves the install is current.
4. If an update is available, offer the supported in-app or user-terminal apply path.

### User asks "update yourself"

1. Check the current version and layout-aware update verdict first.
2. If an update is available, use the authenticated in-app action when supported; otherwise ask the user to run the exact remediation command in their terminal. The agent shell cannot run `kirocrew update` directly.
3. Follow the capability response: terminal source/wheel updates require a gateway restart, while an in-app apply owns the restart itself.
4. Report any dirty-tree, divergence, interpreter-floor, policy-provider, or externally-managed refusal without routing around it.

### User asks "keep yourself updated" or "auto-update"

1. Inspect the install's update capability and current `check_status`.
2. Explain which component owns updates for this layout (git/wheel, desktop OTA, container, or policy provider).
3. Use the existing Kiro Crew `auto_update`/channel/policy controls where available; do not create a second update cron.
4. For externally managed or container installs, give the capability's own remediation instead of claiming Kiro Crew can self-update.

# A managed-venv update keeps the optional extras the running install has

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-23

## Decision

An in-app update of a `cli.sh` managed-venv install installs the new release
together with every declared optional extra whose packages the running install
already has, in the same install step; when one of those packages cannot be
installed on the host, the update fails, the current version keeps running, and
the message names the extra and says that uninstalling the package that cannot
be installed from the running install stops the update from carrying it (keep,
and fail rather than drop).

## Why

The update built the new tree from the bare wheel, so a channel SDK the owner
had installed (`lark-oapi` for Feishu) was missing after every update and the
channel silently stopped. The maintainer chose to keep the extras with no new
setting or second install location, and to fail the update rather than drop an
extra the host cannot install. The update that brings this behaviour is still
built by the older running version, so an install updated to it needs the extra
reinstalled once; later updates keep it.

## Evidence

- The maintainer's decision on the issue:
  https://github.com/kirodotdev/KiroCrew/issues/41035#issuecomment-7091990328
- The issue: https://github.com/kirodotdev/KiroCrew/issues/41035

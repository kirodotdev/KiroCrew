"""Per-pod environment overrides for test doubles: the allowlist and its checks.

A booted pod starts from the service manager's clean environment, so nothing a
caller exports reaches its gateway. ``pod up --env KEY=VALUE`` (and the Dev Fleet
``pod_up`` tool's ``env`` field) is how one pod is pointed at a test double, such
as a fake ``kiro-cli`` that fails the way an old CLI does, so that error state
can be reproduced and screenshotted inside a pod.

Only the keys in :data:`ALLOWED_KEYS` are accepted. Each one names a test seam,
never a credential, so nothing secret can travel this way and the scrub in
``build_pod_env`` still holds for every other variable. The values are recorded
in the pod's env file under the same names, beside ``PORT=`` and ``SEED=``, and
every ``pod up`` rewrites all of them: a key the command does not name is
cleared, so a double never outlives the ``up`` that asked for it. ``pod restart``
keeps them, because it reboots the same pod.

The env file is hand-editable, so the boot path re-checks every value with
:func:`overrides_from_env_data` instead of trusting what ``pod up`` wrote.

Leaf module: imported by the boot path, the CLI and the Dev Fleet API, so it pulls
in nothing from :mod:`kiro_crew.pod.runtime`. Errors are :class:`EnvOverrideError`,
a ``ValueError``, which callers convert to their own refusal type.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from kiro_crew import platform_compat

#: The agent binary a pod's gateway spawns (``kiro_cli.py`` reads it ahead of
#: every install directory). Named here because it is the test seam for agent
#: failures: a fake CLI on this path reproduces any startup error.
KIRO_BIN_KEY = "KIROCREW_KIRO_BIN"

#: Every key ``--env`` accepts. Each must name a path to a test double and never
#: a secret; a key added here gets a check in :func:`_check_value`.
ALLOWED_KEYS: tuple[str, ...] = (KIRO_BIN_KEY,)

#: Bound on one value, so an unbounded string never reaches argv or the env file.
MAX_VALUE_LEN = 1024


class EnvOverrideError(ValueError):
    """A ``--env`` entry named an unknown key or carried an unusable value."""


def is_single_line_env_value(value: str) -> bool:
    """Accept empty or NUL-free values that the env parser keeps on one line."""
    return "\x00" not in value and value.splitlines() in ([], [value])


def _check_value(key: str, value: str) -> str:
    """The value to record for *key*, or :class:`EnvOverrideError`.

    An empty value is accepted and means "no override", so callers drop it. A
    non-empty ``KIROCREW_KIRO_BIN`` must be an absolute path to an executable
    file: the resolver silently skips a path that is not one and falls back to the
    real ``kiro-cli``, so a typo would boot a pod that looks overridden and is not.
    """
    if len(value) > MAX_VALUE_LEN:
        raise EnvOverrideError(f"--env {key}: value is longer than {MAX_VALUE_LEN} characters")
    if not is_single_line_env_value(value):
        raise EnvOverrideError(f"--env {key}: value must be a single line")
    if not value:
        return value
    if key == KIRO_BIN_KEY:
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            raise EnvOverrideError(f"--env {key}: {value!r} is not an absolute path")
        if not platform_compat.is_executable_file(path):
            raise EnvOverrideError(f"--env {key}: {value!r} is not an executable file")
        # An empty file cannot be exec'd, and executable discovery treats one as
        # untrusted (POSIX) or skips it (Windows), so it would never be the child.
        try:
            empty = os.path.getsize(path) == 0
        except OSError as exc:
            raise EnvOverrideError(f"--env {key}: {value!r} cannot be read ({exc})") from exc
        if empty:
            raise EnvOverrideError(f"--env {key}: {value!r} is an empty file")
        return path
    return value


def parse_assignments(entries: Iterable[str]) -> dict[str, str]:
    """``KEY=VALUE`` strings from ``--env`` (or the ``pod_up`` tool) as a checked mapping.

    Raises :class:`EnvOverrideError` for a missing ``=``, a key outside
    :data:`ALLOWED_KEYS`, a key given twice, or a value :func:`_check_value`
    refuses. An empty value is dropped: it asks for no override.
    """
    out: dict[str, str] = {}
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise EnvOverrideError("--env entries must be KEY=VALUE strings")
        key, sep, value = entry.partition("=")
        key = key.strip()
        if not sep or not key:
            raise EnvOverrideError(f"--env {entry!r}: expected KEY=VALUE")
        if key not in ALLOWED_KEYS:
            allowed = ", ".join(ALLOWED_KEYS)
            raise EnvOverrideError(f"--env {key}: not an allowed key (allowed: {allowed})")
        if key in seen:
            raise EnvOverrideError(f"--env {key}: given more than once")
        seen.add(key)
        checked = _check_value(key, value)
        if checked:
            out[key] = checked
    return out


def env_file_updates(overrides: Mapping[str, str], current: Mapping[str, str]) -> dict[str, str]:
    """The env-file writes for one ``pod up``: each named key, and a clear for the rest.

    *current* is the pod's env file as it stands. A key it holds that this ``up``
    does not name is written empty, which reads as no override (the file's merge
    has no delete). A pod that never had an override gets no new line.
    """
    updates = dict(overrides)
    for key in ALLOWED_KEYS:
        if key not in updates and current.get(key):
            updates[key] = ""
    return updates


def overrides_from_env_data(env_data: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """The overrides a pod boots with, read from its parsed env file.

    Returns ``(applied, refused)``: *applied* holds each allowed key whose value
    passes the same check ``pod up`` applies, and *refused* one message per value
    that does not (a hand edit), so the boot can print why it ignored it. Keys
    outside :data:`ALLOWED_KEYS` are never read, whatever the file holds.
    """
    applied: dict[str, str] = {}
    refused: list[str] = []
    for key in ALLOWED_KEYS:
        value = env_data.get(key, "")
        if not value:
            continue
        try:
            applied[key] = _check_value(key, value)
        except EnvOverrideError as exc:
            refused.append(str(exc))
    return applied, refused

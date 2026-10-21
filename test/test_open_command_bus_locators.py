"""The ``openCommand`` spawn adds no bus-locator shim, and its docs match the code.

The rationale comment on ``apps/routes._OPEN_COMMAND_DESKTOP_ENV_KEYS`` describes
how ``DBUS_SESSION_BUS_ADDRESS`` and ``XDG_RUNTIME_DIR`` fare on the
``openCommand`` spawn. The sandbox has a restore-then-drop for exactly these two
variables -- put back after the credential scrub for ``systemd-run``'s own use,
then removed again inside the cgroup scope with an ``env -u`` shim -- but it lives
in ``sandbox.sandboxed_spawn_argv``, via ``cgroup_scope_bus_env`` and
``_unset_env_argv``, and this route never calls that function. It wraps with
``wrap_argv_async`` + ``cgroup_scope_argv`` and spawns with an explicit ``env=``,
so the child KEEPS both locators.

That the locators reach the child's ``env`` is asserted by the positive control in
``test_s29_spawn_env_regression.py`` (``DBUS_SESSION_BUS_ADDRESS`` through
``DISPLAY_KEYS``, ``XDG_RUNTIME_DIR`` explicitly). This file drives the same
harness and pins the other half of the comment: nothing between the manifest's
shell command and the spawn primitive prepends an ``env -u`` shim, and the
sandbox's bus-locator helper is never consulted. The remaining tests tie the two
name lists the app-kit manifest reference spells out -- what ``scrub_env``
removes, and the desktop hints -- to the production code on bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_s29_spawn_env_regression import BUS_LOCATOR_KEYS, open_app_spawn

from kiro_crew import sandbox
from kiro_crew.apps import routes
from kiro_crew.apps.registry_pipeline import subprocess_env


@pytest.mark.asyncio
async def test_open_command_spawn_adds_no_locator_dropping_shim(tmp_path, monkeypatch) -> None:
    """No ``env -u`` shim and no bus-locator helper anywhere on this spawn path.

    ``sandboxed_spawn_argv`` restores the locators after its scrub and prepends
    ``env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS`` inside the scope so its
    child never keeps them. This route does not go through that chokepoint: the
    argv handed to the spawn is exactly what ``cgroup_scope_argv`` returned, which
    is exactly what ``wrap_argv`` returned, which is the manifest's shell command
    -- and ``cgroup_scope_bus_env`` is never consulted.
    """
    spawn = await open_app_spawn(tmp_path, monkeypatch)
    argv = spawn.argv
    wrap_calls = spawn.wrap_calls
    scope_calls = spawn.scope_calls

    assert spawn.bus_env_calls == [], (
        "cgroup_scope_bus_env ran during an openCommand launch: this path has moved "
        "onto the sandboxed_spawn_argv restore-then-drop chokepoint, which the "
        "rationale comment says it does not use"
    )
    assert len(wrap_calls) == 1 and len(scope_calls) == 1, (
        f"expected one wrap and one scope on the route's own path, saw "
        f"{len(wrap_calls)} wrap / {len(scope_calls)} scope"
    )
    wrap_received, wrap_returned = wrap_calls[0]
    scope_received, scope_returned = scope_calls[0]
    assert wrap_received == ["/bin/sh", "-c", "true"], wrap_received
    assert (
        scope_received == wrap_returned
    ), "something was inserted between the sandbox wrapper and the cgroup scope"
    assert (
        argv == scope_returned
    ), "something was inserted between the cgroup scope and the spawn primitive"
    for index, token in enumerate(argv):
        if token == "-u" and index + 1 < len(argv):
            assert argv[index + 1] not in BUS_LOCATOR_KEYS, (
                f"an `env -u {argv[index + 1]}` shim is on the openCommand argv; the "
                f"rationale comment says this path never drops the locators"
            )


@pytest.mark.parametrize("sandbox_level", ["none", "unconfined", "standard", "cc", "strict"])
@pytest.mark.parametrize("strip_python_env", [False, True])
@pytest.mark.parametrize("forward_ssh_auth_sock", [False, True])
def test_real_wrapper_scrub_never_names_a_bus_locator(
    monkeypatch, sandbox_level: str, strip_python_env: bool, forward_ssh_auth_sock: bool
) -> None:
    """The real ``wrap_argv`` launchers cannot drop a locator either.

    The test above replaces ``wrap_argv`` with an identity, so it would not see an
    ``env -u`` that the REAL wrapper prepends. Every real launcher -- the Linux
    namespace launcher's ``ENV_PREFIXES`` loop, the macOS seatbelt ``env -u``
    flags and the no-backend ``env -u`` carve-out -- derives its scrub set from
    :func:`sandbox._sandbox_env_scrub_keys`; this pins that derivation, over
    every sandbox level and flag combination, with both locators planted in the
    environment it reads.
    """
    for key in BUS_LOCATOR_KEYS:
        monkeypatch.setenv(key, f"{key.lower()}-planted")

    scrubbed = set(
        sandbox._sandbox_env_scrub_keys(sandbox_level, strip_python_env, forward_ssh_auth_sock)
    )

    assert not (scrubbed & set(BUS_LOCATOR_KEYS)), (
        f"sandbox level {sandbox_level!r} (strip_python_env={strip_python_env}, "
        f"forward_ssh_auth_sock={forward_ssh_auth_sock}) would `env -u` "
        f"{sorted(scrubbed & set(BUS_LOCATOR_KEYS))}: the real wrapper drops a bus "
        f"locator the rationale comment says the openCommand child keeps"
    )


# ---------------------------------------------------------------------------
# docs/app-kit/manifest-reference.md -- the openCommand environment subsection
# ---------------------------------------------------------------------------

_MANIFEST_REFERENCE = (
    Path(__file__).resolve().parents[1] / "docs" / "app-kit" / "manifest-reference.md"
)
_SECTION_START = "#### `openCommand` — Environment"
_SECTION_END = "## Validation Rules"
_BACKTICKED_ENV_NAME = re.compile(r"`([A-Z][A-Z0-9_]+)\*?`")


def _environment_section() -> str:
    text = _MANIFEST_REFERENCE.read_text(encoding="utf-8")
    start = text.index(_SECTION_START)
    return text[start : text.index(_SECTION_END, start)]


def _bullet(section: str, heading: str, next_heading: str) -> str:
    start = section.index(heading)
    return section[start : section.index(next_heading, start)]


def test_manifest_reference_scrub_names_match_scrub_env() -> None:
    """The doc names exactly the allowlisted keys ``scrub_env`` removes."""
    section = _environment_section()
    scrub_bullet = _bullet(section, "**Removed again", "**Desktop hints")
    documented = set(_BACKTICKED_ENV_NAME.findall(scrub_bullet)) & set(
        subprocess_env._SAFE_ENV_KEYS
    )
    planted = {key: "v" for key in subprocess_env._SAFE_ENV_KEYS}
    removed = set(planted) - set(sandbox.scrub_env(planted))

    assert documented == removed, (
        f"docs say scrub_env removes {sorted(documented)} from the allowlist; "
        f"it removes {sorted(removed)}"
    )


def test_manifest_reference_desktop_hints_match_the_route() -> None:
    """The doc's desktop-hint list is ``_OPEN_COMMAND_DESKTOP_ENV_KEYS``, on bytes."""
    section = _environment_section()
    hint_bullet = _bullet(section, "**Desktop hints", "The two user-bus locators")
    documented = set(_BACKTICKED_ENV_NAME.findall(hint_bullet))

    assert documented == set(routes._OPEN_COMMAND_DESKTOP_ENV_KEYS), (
        f"docs desktop hints {sorted(documented)} != "
        f"routes._OPEN_COMMAND_DESKTOP_ENV_KEYS {sorted(routes._OPEN_COMMAND_DESKTOP_ENV_KEYS)}"
    )

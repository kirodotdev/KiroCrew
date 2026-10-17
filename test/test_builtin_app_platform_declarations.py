"""Every builtin app states its platform support out loud.

What `platform.os` IS, for a builtin, is a user-facing CLAIM: the sole non-test
consumer of a builtin's value is `website/src/pages/AppDetailPage.tsx`, which
renders it on the App Store detail page. What it is NOT is an enable gate.
`apps/routes.py` reads it once, through `_client_install_manifest()`, which
returns None unless `platform.installMode == "client"` — and no builtin sets
that. `handle_enable_app`'s own docstring says it outright: "nothing else on the
enable path consults that field". A builtin whose block is absent is therefore
enableable on every platform the gateway runs on, Windows included.

That is why the invariant pinned here is about HONESTY, not access. The default is
`["macos", "linux"]` (`apps/manifest.py`), so an app that omits the block
silently publishes "does not run on Windows" — indistinguishable, to a reader,
from a deliberate statement. Twenty-two of twenty-four builtins were publishing
exactly that, and none of them meant it.

So the invariant pinned here is not "every app supports Windows". It is that no
app's Windows stance is an accident:

* every builtin declares `platform.os` EXPLICITLY, which turns the label into a
  statement someone made (`test_every_builtin_declares_platform_os_explicitly`);
* every declared name resolves to a real `sys.platform` value, so a typo like
  `"win32"` or `"Windows"` cannot sit in a manifest looking authoritative while
  matching nothing (`test_every_declared_platform_name_resolves`);
* an app that ships `tests/` is a package, which is a collection-level invariant
  no single app's suite can see (`test_every_app_with_tests_is_a_package`).

The companion fact this file pins is the one that decides what "runs on Windows"
actually costs, because it is easy to get backwards. It is NOT "does the app shell
out to a tool". It is whether the gateway must spawn a BACKEND CHILD PROCESS:

* no `backend`, or `backend.hooks` only -> the code runs inside the gateway
  process and spawns nothing, so the absent Windows sandbox backend is not on
  its path;
* `backend.entryPoint` present -> `apps/backend.py` spawns the child through
  `sandbox.wrap_argv` WITHOUT the `first_party_fixed_argv` carve-out, and Kiro
  Crew has no native Windows sandbox backend, so on native Windows that spawn
  needs the operator's `agent.sandbox_allow_unsandboxed_exec=true` (or
  `agent.sandbox='off'`). `dev_fleet` ships with `windows` declared while in this
  group, which is the precedent that this is a documented prerequisite rather
  than grounds for publishing "does not run here".

`test_apps_needing_a_backend_child_are_pinned` keeps that list honest, so a
future app cannot join it unnoticed and inherit the prerequisite without the docs
being updated alongside.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.execution import _BUILTINS_DIR
from kiro_crew.apps.manifest import PlatformConfig

#: Apps whose manifest declares `backend.entryPoint`, so the gateway spawns a
#: separate backend process for them. On native Windows that spawn is gated on
#: `agent.sandbox_allow_unsandboxed_exec` — see this module's docstring.
_NEEDS_BACKEND_CHILD_PROCESS = frozenset(
    {
        "design_tweak",
        "dev_fleet",
        "file_explorer",
        "md_notebook",
        "workflows",
    }
)

#: The user-facing OS names a manifest may name. Mirrors
#: `PlatformConfig._OS_TO_PLATFORM`; asserted against it below so the two cannot
#: drift.
_VALID_OS_NAMES = frozenset({"macos", "linux", "windows"})


def _app_dirs() -> list[Path]:
    return sorted(
        p
        for p in _BUILTINS_DIR.iterdir()
        if p.is_dir() and p.name != "__pycache__" and (p / "app.json").is_file()
    )


def _manifest(app_dir: Path) -> dict:
    return json.loads((app_dir / "app.json").read_text(encoding="utf-8"))


def _app_ids() -> list[str]:
    return [p.name for p in _app_dirs()]


@pytest.fixture(scope="module")
def manifests() -> dict[str, dict]:
    return {p.name: _manifest(p) for p in _app_dirs()}


def test_there_are_builtin_apps_to_check() -> None:
    """Guard the guard: an empty sweep would make every test below vacuous."""
    assert len(_app_dirs()) >= 20


@pytest.mark.parametrize("app", _app_ids())
def test_every_builtin_declares_platform_os_explicitly(
    app: str, manifests: dict[str, dict]
) -> None:
    """No app may rely on the implicit `["macos", "linux"]` default.

    The default is the right one to keep — widening it would promise Windows on
    behalf of every app that never opted in — but relying on it hides the
    decision. Declare the block and say what you mean.
    """
    platform_block = manifests[app].get("platform")
    assert isinstance(platform_block, dict), (
        f"{app}/app.json has no `platform` block, so platform.os falls back to "
        f"the implicit {PlatformConfig().os!r} default. That silently excludes "
        f"Windows. Declare it explicitly."
    )
    declared = platform_block.get("os")
    assert isinstance(declared, list) and declared, (
        f"{app}/app.json declares a `platform` block without a non-empty `os` "
        f"list, so it still inherits the implicit default."
    )


@pytest.mark.parametrize("app", _app_ids())
def test_every_declared_platform_name_resolves(app: str, manifests: dict[str, dict]) -> None:
    """An unmapped name is accepted into the list and then never matches.

    `windows` was in exactly that state until the mapping row landed, so a
    manifest could claim a platform the gate rejected. This asserts on the
    resolver rather than on the spelling.
    """
    declared = manifests[app]["platform"]["os"]
    unknown = sorted(set(declared) - _VALID_OS_NAMES)
    assert (
        not unknown
    ), f"{app} declares unknown OS name(s) {unknown}; valid: {sorted(_VALID_OS_NAMES)}"

    cfg = PlatformConfig(os=list(declared))
    for name in declared:
        sys_platform = PlatformConfig._OS_TO_PLATFORM[name]
        assert cfg.supports_platform(sys_platform), (
            f"{app} declares {name!r} but supports_platform({sys_platform!r}) is "
            f"False — the declaration does not reach the gate."
        )


def test_valid_os_names_match_the_resolver() -> None:
    """Keep `_VALID_OS_NAMES` from drifting away from the real mapping."""
    assert _VALID_OS_NAMES == frozenset(PlatformConfig._OS_TO_PLATFORM)


def test_apps_needing_a_backend_child_are_pinned(manifests: dict[str, dict]) -> None:
    """Pin which apps the gateway must spawn a backend process for.

    That set — not "does it call out to a tool" — is what decides whether an app
    needs `agent.sandbox_allow_unsandboxed_exec` on native Windows. Pinning it
    means a new app cannot join the group unnoticed and inherit the requirement
    without the docs and its own copy being updated.
    """
    actual = {
        app
        for app, manifest in manifests.items()
        if (manifest.get("backend") or {}).get("entryPoint")
    }
    assert actual == set(_NEEDS_BACKEND_CHILD_PROCESS), (
        "the set of apps with a backend.entryPoint changed. Update "
        "_NEEDS_BACKEND_CHILD_PROCESS, docs/app-kit/manifest-reference.md's "
        "`platform.os` section, and the app's own copy about the Windows "
        f"sandbox opt-in. added={sorted(actual - set(_NEEDS_BACKEND_CHILD_PROCESS))} "
        f"removed={sorted(set(_NEEDS_BACKEND_CHILD_PROCESS) - actual)}"
    )


def test_every_app_with_tests_is_a_package() -> None:
    """`setup.cfg` collects `src/kiro_crew/apps/builtins`, so tests/ must be packages.

    Seven apps ship a module named `test_manifest.py`. Two folders of the same
    basename that are not packages collide on module name and fail collection
    outright, which is a repo-wide break rather than one app's problem — so it
    is asserted centrally here rather than in any single app's suite.
    """
    offenders: list[str] = []
    for app_dir in _app_dirs():
        tests_dir = app_dir / "tests"
        if not tests_dir.is_dir():
            continue
        for pkg in (app_dir / "__init__.py", tests_dir / "__init__.py"):
            if not pkg.exists():
                offenders.append(str(pkg.relative_to(_BUILTINS_DIR)))
    assert not offenders, (
        "an app that ships tests must be a package (and so must its tests/ "
        f"folder), otherwise pytest collection collides: missing {offenders}"
    )


def test_the_host_running_this_suite_is_a_declared_platform() -> None:
    """At least one app must be enableable here, or the suite proves nothing.

    Also the cheapest possible regression on the mapping: if `current_os()` ever
    returns a raw `sys.platform` value again, this fails on Windows CI.
    """
    current = PlatformConfig.current_os()
    assert current in _VALID_OS_NAMES, (
        f"PlatformConfig.current_os() returned {current!r} for sys.platform="
        f"{sys.platform!r}, which is not a name any manifest can declare."
    )
    enableable = [
        app
        for app, manifest in ((p.name, _manifest(p)) for p in _app_dirs())
        if PlatformConfig(os=list(manifest["platform"]["os"])).supports_platform(sys.platform)
    ]
    assert enableable, f"no builtin app is enableable on {sys.platform!r}"

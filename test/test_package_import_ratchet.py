"""Importing ``kiro_crew.acp`` loads none of its modules, and importing
``kiro_crew.dashboard.handlers`` loads none of the handler modules it reads lazily.

Both packages read those names from their owner on first access, so a module or test
that imports one small ``kiro_crew.acp`` module does not also load the client and
everything under it, and a handler test does not load those handler modules. A fresh
``import kiro_crew.dashboard.state`` loads neither the ACP client nor its session handle.
Imports are measured in a fresh interpreter: the test process has already imported most
of the tree.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import time

import pytest

import kiro_crew.acp as package
from kiro_crew.acp import types as acp_types
from kiro_crew.dashboard import handlers
from kiro_crew.dashboard.handlers import side
from kiro_crew.testing.wait import default_timeout

#: One child runs every import-order case, in this order, and prints one line per case.
#: The package is its first import; ``kas_hook_coverage`` comes before anything that
#: loads ``kas_permissions``; the session start is reached without the facade.
_CHILD = """\
import sys
import kiro_crew.acp
print(",".join(sorted(m for m in sys.modules if m.split(".")[0] == "kiro_crew")))
import kiro_crew.acp.kas_hook_coverage
from kiro_crew.acp import client_class, client_start
print("kiro_crew.acp.client" in sys.modules, callable(client_start._resolve_spawn_env))
"""


#: Neither module defines a name the dashboard state reads while it imports, and together
#: they pull in a few hundred more that every test importing the state would then load.
_STATE_UNLOADED = ("kiro_crew.acp.client", "kiro_crew.acp.session_handle")

_STATE_CHILD = f"""\
import sys
import kiro_crew.dashboard.state
print(" ".join(m for m in {_STATE_UNLOADED!r} if m in sys.modules))
"""

#: Handler modules the handlers package must keep reading names from on first access.
_LAZY_HANDLER_MODULES = [
    f"kiro_crew.dashboard.handlers.{name}"
    for name in ("hooks", "portability", "security", "session_storage", "side", "taskrunner")
]

#: The package is its first import; it prints each of those modules the import loaded.
_HANDLERS_CHILD = f"""\
import sys
import kiro_crew.dashboard.handlers
print(" ".join(m for m in {_LAZY_HANDLER_MODULES!r} if m in sys.modules))
"""

#: Each acp case must start before ``kas_permissions`` loads, and each one's import loads
#: it, and the dashboard state and the handlers package must each be their case's first
#: import, so each case runs in its own fresh interpreter.
_CHILDREN = {
    "import_orders": _CHILD,
    "tools_settings": "import kiro_crew.acp.kas_permissions_tools_settings",
    "dashboard_state": _STATE_CHILD,
    "handlers": _HANDLERS_CHILD,
}

# Every child of this module starts on one worker, one after another (D8).
pytestmark = pytest.mark.xdist_group(name="package_import_children")


@pytest.fixture(scope="module")
def children() -> dict[str, subprocess.CompletedProcess[str]]:
    # Back to back in the first test's setup, together within ``default_timeout()``: each
    # child may use whatever the ones before it left.
    deadline = time.monotonic() + default_timeout()
    return {
        case: subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=deadline - time.monotonic(),  # flake-ok: one deadline for all, see above
        )
        for case, script in _CHILDREN.items()
    }


def test_the_acp_package_and_its_import_orders_in_a_fresh_interpreter(
    children: dict[str, subprocess.CompletedProcess[str]],
) -> None:
    res = children["import_orders"]
    assert res.returncode == 0, res.stderr
    loaded, session_start = res.stdout.splitlines()
    assert set(loaded.split(",")) == {"kiro_crew", "kiro_crew.acp"}, loaded
    assert session_start == "False True", "facade loaded, resolver bound: " + session_start


def test_the_toolssettings_translation_imports_before_kas_permissions(
    children: dict[str, subprocess.CompletedProcess[str]],
) -> None:
    res = children["tools_settings"]
    assert res.returncode == 0, res.stderr


def test_the_dashboard_state_loads_neither_the_acp_client_nor_the_session_handle(
    children: dict[str, subprocess.CompletedProcess[str]],
) -> None:
    res = children["dashboard_state"]
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", "loaded with the dashboard state: " + res.stdout


def test_every_public_name_is_read_from_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in package.__all__:
        owner = importlib.import_module(package._OWNERS[name])
        assert getattr(package, name) is getattr(owner, name)
    assert set(package.__all__) <= set(dir(package))
    stand_in = type("StandIn", (), {})
    monkeypatch.setattr(acp_types, "AcpEvent", stand_in)
    assert package.AcpEvent is stand_in


def test_the_handlers_package_loads_none_of_its_lazily_read_modules(
    children: dict[str, subprocess.CompletedProcess[str]],
) -> None:
    res = children["handlers"]
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", "loaded with the package: " + res.stdout


def test_every_lazily_read_handler_name_is_read_from_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(_LAZY_HANDLER_MODULES) <= set(handlers._LAZY)
    for name, owner in handlers._OWNERS.items():
        assert getattr(handlers, name) is getattr(importlib.import_module(owner), name), name
    assert set(handlers._OWNERS) <= set(dir(handlers))
    stand_in = object()
    monkeypatch.setattr(side, "api_side_state", stand_in)
    assert handlers.api_side_state is stand_in

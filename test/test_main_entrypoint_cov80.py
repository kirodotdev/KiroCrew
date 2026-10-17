"""``kiro_crew.__main__`` — the ordering contract of the ``python -m`` entry point.

The module is tiny but load-bearing, and its own docstring states the invariant
that makes it so: ``_ensure_ssl_certs()`` must run BEFORE ``kiro_crew.cli`` is
imported, because that import pulls in ``aiohttp``, which caches its default SSL
context at import time. Get the order wrong and every HTTPS call in the process
fails with CERTIFICATE_VERIFY_FAILED on a host whose cafile is missing — a
failure that no other test would attribute back to this file.

So this executes the module body in a throwaway namespace (once under
``__name__ == "__main__"``, once not — the guarded branch is the whole point of
the file) with both side-effecting callables stubbed, and asserts:

* the console-encoding fix and the SSL fix both run, in that order;
* ``cli.main`` is invoked only under ``__main__``, and only after the SSL fix;
* executing it as a plain module does NOT start the CLI.

The body is compiled from source rather than run through ``runpy`` on purpose:
``runpy`` hands back the cached bytecode, whose ``co_filename`` need not match
this checkout, and coverage then attributes the executed lines elsewhere.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# The entry point is imported at module scope so its body has run under a plain
# ``__name__`` at least once in this process: that is the guard DECLINING, which
# the exec-based tests below cannot show. Both of its side effects are idempotent
# (``kiro_crew.cli`` applies the same SSL fix on import), so importing it here
# costs nothing and starts no CLI.
import kiro_crew.__main__  # noqa: F401

# Imported eagerly for a different reason: ``kiro_crew.cli`` calls
# ``_ensure_ssl_certs()`` at import time too, so importing it lazily from inside
# the fixture would record a spurious first "ssl" and hide the ordering this
# file exists to pin.
from kiro_crew import cli as cli_mod

_ENTRYPOINT = Path(cli_mod.__file__).with_name("__main__.py")


def _exec_entrypoint(run_name: str) -> Dict[str, Any]:
    """Execute ``__main__.py``'s body under *run_name* in a throwaway namespace."""
    code = compile(_ENTRYPOINT.read_text(encoding="utf-8"), str(_ENTRYPOINT), "exec")
    namespace: Dict[str, Any] = {"__name__": run_name, "__file__": str(_ENTRYPOINT)}
    # The compiled source is this repository's own __main__.py, located through an
    # already-imported module -- no external input reaches exec(). runpy cannot be
    # used instead: it returns cached bytecode whose co_filename need not match this
    # checkout, and coverage then attributes the lines elsewhere (measures 0%).
    exec(code, namespace)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    return namespace


@pytest.fixture()
def order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the module's side effects in call order, executing none of them."""
    calls: list[str] = []
    monkeypatch.setattr(
        "kiro_crew.platform_compat.ensure_utf8_console",
        lambda: calls.append("utf8"),
    )
    monkeypatch.setattr(
        "kiro_crew._ssl_compat._ensure_ssl_certs",
        lambda: calls.append("ssl"),
    )
    monkeypatch.setattr(cli_mod, "main", lambda: calls.append("main"))
    return calls


def test_importing_the_module_applies_both_fixes_in_order(
    order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain import runs the console + SSL fixes and stops at the guard."""
    monkeypatch.delitem(sys.modules, "kiro_crew.__main__", raising=False)
    module = importlib.import_module("kiro_crew.__main__")
    assert order == ["utf8", "ssl"]
    # The guard was evaluated and declined: no CLI entry point was bound.
    assert not hasattr(module, "main")


def test_run_as_main_fixes_the_console_and_ssl_before_starting_the_cli(
    order: list[str],
) -> None:
    namespace = _exec_entrypoint("__main__")
    assert order == ["utf8", "ssl", "main"]
    # The CLI entry point is resolved inside the guard, not at module scope.
    assert namespace["main"] is not None


def test_executed_as_a_plain_module_does_not_start_the_cli(order: list[str]) -> None:
    """Only the ``__main__`` guard may call into the CLI."""
    namespace = _exec_entrypoint("kiro_crew.__main__")
    assert order == ["utf8", "ssl"]
    assert "main" not in namespace

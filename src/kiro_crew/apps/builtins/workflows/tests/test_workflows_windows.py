"""Windows-support tests for the ``workflows`` builtin app.

Two things are pinned here.

1. The manifest's ``platform.os`` declaration. Without a ``platform`` block the
   manifest inherits ``PlatformConfig``'s default ``["macos", "linux"]``, which is
   published on the App Store detail page — so the app told every Windows user it
   does not run there, without anyone having decided that. It is a claim, not a
   gate: ``apps/routes.py`` consults the field only for a
   ``platform.installMode: "client"`` app, and no builtin is one. Nothing in this
   app is POSIX-specific either: the backend is stdlib-only, binds loopback
   explicitly, spawns no external process, and builds every path through
   ``os.path``.

2. The behaviour of the two Windows-motivated code fixes: the address-reuse flag
   on the listener, and ``handle_examples`` degrading with a reason instead of a
   silent empty list or a 500.

Everything resolves relative to this file, so the suite is machine independent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.workflows import server as wf_server
from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest, PlatformConfig

# .../workflows/tests/test_workflows_windows.py
#   parents[0] = tests
#   parents[1] = workflows   (the app root)
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "workflows"
DECLARED_OS = ["macos", "linux", "windows"]

GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    ctx.log('hi')\n"
    "    return {'ok': True}\n"
)


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_dict(json.loads(APP_JSON.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# 1. The platform declaration
# --------------------------------------------------------------------------- #


def test_typed_manifest_carries_the_same_platform_list(manifest: AppManifest) -> None:
    # The gates read the typed value; humans read the file. They must not disagree.
    assert manifest.platform.os == DECLARED_OS


def test_the_implicit_default_would_have_excluded_windows() -> None:
    """Documents why the block is needed at all, so the assertion above is
    meaningful rather than tautological.
    """
    default_cfg = PlatformConfig()
    assert default_cfg.os == ["macos", "linux"]
    assert not default_cfg.supports_platform("win32")


def test_discovery_serializes_the_platform_declaration() -> None:
    """``PlatformConfig.to_dict()`` emits ``os`` only when it differs from the
    default, so the declaration reaching the App Store payload is what proves the
    detail page can render "Windows" at all — the field's one real consumer.
    """
    entry = next((a for a in discover_builtin_apps() if a.get("name") == APP_NAME), None)
    assert entry is not None, f"{APP_NAME!r} not discovered"
    assert entry["platform"]["os"] == DECLARED_OS


def test_manifest_still_validates(manifest: AppManifest) -> None:
    # Guards the hand-edited JSON: a malformed manifest is dropped by discovery
    # rather than reported, so the app would just vanish from the App Store.
    assert manifest.validate() == [], manifest.validate()


def test_platform_block_did_not_disturb_the_opt_in_flags(raw_manifest: dict) -> None:
    # The block was inserted between `hidden` and `permissions`; pin that the
    # surrounding contract survived the edit. The app stays hidden and off by
    # default — it is turned on with `kirocrew app enable workflows`.
    assert raw_manifest["defaultEnabled"] is False
    assert raw_manifest["hidden"] is True
    assert raw_manifest["permissions"]["api"] == ["/apps/workflows/api"]
    assert raw_manifest["ui"]["pages"][0]["route"] == "/workflows"


def test_backend_entry_point_is_module_style_not_a_shell_launcher(
    raw_manifest: dict,
) -> None:
    """The ``windows`` claim depends on WHICH spawn branch the manifest selects.

    ``apps/backend.py`` routes an exec/shell-launcher backend (an explicit
    ``type: "exec"``, or a ``.sh`` / non-Python-shebang entry point) into a branch
    that hard-fails on native Windows, because it relies on POSIX shebang exec.
    A dotted module name with no path separator and no script extension takes the
    ``python -m`` branch instead, which is platform-neutral. If someone converts
    this backend to a launcher script, ``windows`` becomes a lie — this is the
    test that says so.
    """
    backend = raw_manifest["backend"]
    entry = backend["entryPoint"]
    assert backend.get("type") not in ("exec",)
    assert "/" not in entry and "\\" not in entry
    assert not entry.endswith((".py", ".js", ".ts", ".mjs", ".cjs", ".sh"))
    assert "." in entry
    assert entry == "kiro_crew.apps.builtins.workflows.server"


def test_app_backend_spawns_no_external_process() -> None:
    """ "Functionally complete on Windows" holds only while the app shells out to
    nothing: Kiro Crew has no native OS sandbox backend on Windows, so a
    subprocess-dependent feature would fail closed there.
    """
    src = (APP_ROOT / "server.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "shell=True", "os.fork", "pty."):
        assert forbidden not in src, (
            f"{forbidden!r} appeared in server.py — re-check the 'full on Windows' "
            "claim, since Windows has no native sandbox backend for app spawns"
        )


# --------------------------------------------------------------------------- #
# 2. The address-reuse fix
# --------------------------------------------------------------------------- #


def test_listener_binds_address_reuse_to_the_platform() -> None:
    """On Windows ``SO_REUSEADDR`` lets a socket bind an address that already has a
    live listener, so the stdlib default would let a duplicate backend bind the
    same port and split traffic. The gateway detects a collision by observing the
    child die on its initial bind, so the bind must be allowed to fail there.
    """
    assert wf_server._Server.allow_reuse_address is platform_compat.IS_POSIX
    if platform_compat.IS_WINDOWS:
        assert wf_server._Server.allow_reuse_address is False


def test_the_stdlib_default_is_what_we_are_overriding() -> None:
    # Makes the assertion above non-tautological: http.server really does turn the
    # flag on for every platform, so subclassing is the only way to scope it.
    assert bool(HTTPServer.allow_reuse_address) is True


def test_listener_is_still_a_threading_server_bound_to_loopback() -> None:
    from http.server import ThreadingHTTPServer

    assert issubclass(wf_server._Server, ThreadingHTTPServer)
    src = (APP_ROOT / "server.py").read_text(encoding="utf-8")
    assert '_Server(("127.0.0.1", PORT), _Handler)' in src
    assert '"0.0.0.0"' not in src


# --------------------------------------------------------------------------- #
# 3. handle_examples degrades with a reason
# --------------------------------------------------------------------------- #


def test_examples_returns_a_list_and_logs_when_the_directory_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The dashboard page consumes a list, so the shape must not change — but the
    empty case has to name its cause, or an empty Examples panel is
    indistinguishable from "this app ships no examples".
    """
    monkeypatch.setattr(wf_server, "_examples_dir", lambda: "")
    with caplog.at_level(logging.WARNING, logger="kirocrew.app.workflows"):
        out = wf_server.handle_examples()
    assert out == []
    assert any("no examples directory" in r.getMessage() for r in caplog.records)


def test_examples_survives_an_oserror_from_listdir(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A racing removal or a directory ACL that denies enumeration must degrade to
    an empty list, not a 500 out of the request handler.
    """
    monkeypatch.setattr(wf_server, "_examples_dir", lambda: str(tmp_path))

    def _boom(_path: str) -> list[str]:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "listdir", _boom)
    with caplog.at_level(logging.WARNING, logger="kirocrew.app.workflows"):
        out = wf_server.handle_examples()
    assert out == []
    assert any("cannot list examples" in r.getMessage() for r in caplog.records)


def test_examples_reads_real_files_as_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-ASCII in an example must not raise on a Windows host whose preferred
    # encoding is cp936/cp1252 — the read passes encoding="utf-8" explicitly.
    (tmp_path / "demo.py").write_text(
        'META = {"name": "demo", "description": "描述"}\n', encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("skipped", encoding="utf-8")
    monkeypatch.setattr(wf_server, "_examples_dir", lambda: str(tmp_path))
    out = wf_server.handle_examples()
    assert [e["name"] for e in out] == ["demo"]
    assert "描述" in out[0]["source"]


def test_examples_dir_is_resolved_without_hand_joined_separators() -> None:
    # os.path.join / os.path.dirname only: a hand-built "a/b/c" would not resolve
    # on Windows and would also trip the repo's portability gate.
    src = (APP_ROOT / "server.py").read_text(encoding="utf-8")
    body = src.split("def _examples_dir()", 1)[1].split("def handle_examples", 1)[0]
    assert "os.path.join" in body
    assert '.split("/")' not in body and '+ "/" +' not in body


# --------------------------------------------------------------------------- #
# 4. The run path works off the main thread (the Windows event-loop question)
# --------------------------------------------------------------------------- #


def test_handle_run_works_on_a_non_main_thread() -> None:
    """``handle_run`` calls ``asyncio.run`` and ``ThreadingHTTPServer`` dispatches
    every request on a worker thread. On Windows the default policy builds a
    ``ProactorEventLoop``; this pins that constructing and running one off the
    main thread is fine (``asyncio.run`` installs signal handlers only on the main
    thread, which is what would otherwise fail).
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["out"] = wf_server.handle_run({"source": GOOD_SCRIPT})
        except BaseException as exc:  # noqa: BLE001 - surfaced as a failure below
            box["exc"] = exc

    t = threading.Thread(target=_worker, name="wf-run")
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "handle_run hung on a worker thread"
    assert "exc" not in box, box.get("exc")
    out = box["out"]
    assert isinstance(out, dict)
    assert out["ok"] is True, out
    assert out["error"] is None
    assert out["events"], "expected at least one workflow event"


def test_handle_run_leaves_no_event_loop_bound_to_the_calling_thread() -> None:
    # asyncio.run closes the loop it created; a leaked one would make the next
    # request on the same worker thread fail with "Event loop is closed".
    wf_server.handle_run({"source": GOOD_SCRIPT})
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()


def test_handle_validate_and_run_reject_a_non_string_source() -> None:
    assert wf_server.handle_validate({"source": 3})["ok"] is False
    bad = wf_server.handle_run({"source": "   "})
    assert bad["ok"] is False
    assert bad["error"] == "missing 'source'"
    assert bad["events"] == []

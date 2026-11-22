"""The desktop app's on-demand optional SDK install (``kiro_crew.sdk_extras``)."""

from __future__ import annotations

import asyncio
import configparser
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import sandbox_leaves, sandbox_precreate, sdk_extras
from kiro_crew.security import paths_lists
from kiro_crew.testing.wait import default_timeout
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
_REAL_PIP_CONFIG_LIST = sdk_extras._pip_config_list


@pytest.fixture(autouse=True)
def _no_configured_index(_floor_monkeypatch):
    """No index from the host: no ``PIP_*`` index variable, and an empty ``pip config``."""
    for name in sdk_extras._PIP_INDEX_ENV:
        _floor_monkeypatch.delenv(name, raising=False)
    _floor_monkeypatch.setattr(sdk_extras, "_pip_config_list", lambda: "")


def test_the_leaf_is_sealed_in_every_fence_browser_harness_is() -> None:
    """The gateway imports what this leaf holds, so an agent write would choose
    gateway code. Same fences as ``browser-harness``, the other directory the
    gateway installs and then runs."""
    leaf = sdk_extras.EXTRAS_LEAF
    assert leaf == "extras"
    assert leaf in sandbox_leaves._CREW_READONLY_LEAVES
    assert leaf in sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES
    assert leaf in sandbox_precreate._CREW_PRECREATE_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._CREW_NOFOLLOW_READONLY_DIR_LEAVES
    assert leaf in sandbox_precreate._DELEGATED_OVERLAP_LEAF_REASONS
    assert leaf in paths_lists._CREW_SECRET_LEAVES


def _lock_pins(extra: str) -> dict[str, tuple[str, list[str]]]:
    text = sdk_extras.lock_file(extra).read_text(encoding="utf-8")
    pins: dict[str, tuple[str, list[str]]] = {}
    for block in re.split(r"\n(?=[a-z])", text):
        head = re.match(r"([a-z][a-z0-9-]*)==(\S+)", block)
        if head:
            pins[head.group(1)] = (head.group(2), re.findall(r"--hash=sha256:[0-9a-f]{64}", block))
    return pins


def test_every_installable_extra_has_a_fully_hashed_lock() -> None:
    for extra in sdk_extras.INSTALLABLE:
        pins = _lock_pins(extra)
        assert pins, extra
        for name, (_version, hashes) in pins.items():
            assert hashes, f"{extra}: {name} has no hash; --require-hashes would refuse it"


def test_the_feishu_lock_satisfies_the_extra_and_adds_only_what_core_lacks() -> None:
    """--no-deps installs exactly these, so the rest of lark-oapi's closure must be
    core's own. The lark-oapi pin must sit inside setup.cfg's [feishu] range."""
    pins = _lock_pins("feishu")
    assert set(pins) == {"lark-oapi", "pycryptodome", "requests-toolbelt"}
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "setup.cfg", encoding="utf-8")
    (spec,) = [s for s in cfg["options.extras_require"]["feishu"].splitlines() if s.strip()]
    assert Requirement(spec).specifier.contains(pins["lark-oapi"][0])


def test_the_lock_ships_in_the_wheel() -> None:
    cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    assert "    sdk_extras_locks/*.txt\n" in cfg


def _pip_probe(monkeypatch, *, present: bool) -> None:
    """Answer ``find_spec("pip")`` as *present*; every other name is looked up for real.

    ``install_supported`` probes pip on the running interpreter, which a pip-free
    test venv (uv's, for one) does not have.
    """
    real_find_spec = sdk_extras.importlib.util.find_spec

    def _find_spec(name, *args, **kwargs):
        if name == "pip":
            return object() if present else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(sdk_extras.importlib.util, "find_spec", _find_spec)


def test_install_is_offered_only_on_the_bundled_interpreter(monkeypatch) -> None:
    _pip_probe(monkeypatch, present=True)
    monkeypatch.setattr(sdk_extras.platform_compat, "is_bundled_interpreter", lambda: False)
    assert sdk_extras.install_supported("feishu") is False
    monkeypatch.setattr(sdk_extras.platform_compat, "is_bundled_interpreter", lambda: True)
    assert sdk_extras.install_supported("feishu") is True
    assert sdk_extras.install_supported("teams") is False
    _pip_probe(monkeypatch, present=False)
    assert sdk_extras.install_supported("feishu") is False


def _mark_current(target: Path, extra: str = "feishu") -> None:
    """Write the marker a finished install made from the shipped lock carries."""
    marker = {sdk_extras.MARKER_LOCK_KEY: sdk_extras.lock_hash(extra)}
    (target / sdk_extras.MARKER_FILE).write_text(json.dumps(marker), encoding="utf-8")


def test_activate_appends_only_a_finished_install(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.mkdir(parents=True)
    sdk_extras.activate(tmp_path)
    assert str(target) not in sys.path
    _mark_current(target)
    sdk_extras.activate(tmp_path)
    sdk_extras.activate(tmp_path)
    assert sys.path.count(str(target)) == 1
    # Appended, never prepended: a bundled module always wins.
    assert sys.path[-1] == str(target)


def test_the_directory_is_keyed_by_interpreter_and_machine(tmp_path: Path) -> None:
    tag = sdk_extras.python_tag()
    assert tag.startswith(f"cp{sys.version_info[0]}{sys.version_info[1]}-{sys.platform}-")
    assert sdk_extras.extra_dir("feishu", tmp_path) == tmp_path / "extras" / "feishu" / tag


def _fake_pip(returncode: int, seen: list[list[str]], *, write: bool = True):
    def run(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs["cwd"] == argv[argv.index("--target") + 1]
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
        if write:
            pkg = Path(argv[argv.index("--target") + 1]) / "lark_oapi"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, "", "pip said no")

    return run


def test_install_uses_the_locked_hashes_and_renames_into_place(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PYTHONPATH", "/workspace/evil")
    seen: list[list[str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, seen))
    target = sdk_extras.install_extra("feishu", tmp_path)
    (argv,) = seen
    assert argv[:4] == [sys.executable, "-I", "-m", "pip"]
    for flag in ("--no-deps", "--require-hashes", "--no-input"):
        assert flag in argv
    assert argv[argv.index("--only-binary") + 1] == ":all:"
    assert argv[argv.index("-r") + 1] == str(sdk_extras.lock_file("feishu"))
    assert (target / "lark_oapi" / "__init__.py").is_file()
    assert (target / sdk_extras.MARKER_FILE).is_file()
    assert not any(p.name.endswith(".partial") for p in target.parent.iterdir())
    assert str(target) in sys.path


def test_a_failed_install_leaves_nothing_activate_would_load(tmp_path: Path, monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(1, seen))
    with pytest.raises(sdk_extras.InstallError, match="pip said no"):
        sdk_extras.install_extra("feishu", tmp_path)
    target = sdk_extras.extra_dir("feishu", tmp_path)
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_a_finished_install_is_returned_untouched(tmp_path: Path, monkeypatch) -> None:
    """The running gateway may have imported it; a second press must not delete it."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    (target / "lark_oapi").mkdir(parents=True)
    module = target / "lark_oapi" / "__init__.py"
    module.write_text("LOADED = 1\n", encoding="utf-8")
    _mark_current(target)
    seen: list[list[str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, seen))
    assert sdk_extras.install_extra("feishu", tmp_path) == target
    assert seen == []
    assert module.read_text(encoding="utf-8") == "LOADED = 1\n"
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    assert str(target) in sys.path


#: Stands in for ``lark_oapi`` in a seeded install. A name of its own, so a
#: real lark-oapi in the test interpreter cannot make the import pass without
#: ``activate()``.
_FAKE_SDK = "kirocrew_fake_lark_sdk"


@pytest.fixture
def finished_feishu_install(tmp_path: Path, monkeypatch):
    """A finished feishu install under a fake crew home, holding only ``_FAKE_SDK``.

    Not on ``sys.path`` until something calls ``activate()``. ``sys.path``,
    ``sys.modules`` and the import caches are put back afterwards.
    """
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sdk_extras, "config_dir", lambda: tmp_path)
    target = sdk_extras.extra_dir("feishu")
    (target / _FAKE_SDK).mkdir(parents=True)
    (target / _FAKE_SDK / "__init__.py").write_text("FROM_EXTRAS = True\n", encoding="utf-8")
    _mark_current(target)
    monkeypatch.delitem(sys.modules, _FAKE_SDK, raising=False)
    importlib.invalidate_caches()
    assert importlib.util.find_spec(_FAKE_SDK) is None
    yield target
    sys.modules.pop(_FAKE_SDK, None)
    importlib.invalidate_caches()


def test_the_settings_status_sees_a_finished_install_without_a_restart(
    finished_feishu_install: Path, monkeypatch
) -> None:
    """Fails if ``_channel_sdk_status`` stops calling ``activate()``: the panel
    would offer Install again for an add-on already on disk."""
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setitem(mod._CHANNEL_SDK_EXTRA, "feishu", (_FAKE_SDK, "feishu"))
    assert mod._channel_sdk_status("feishu") == (True, True, "")
    assert str(finished_feishu_install) in sys.path


def test_the_gateway_imports_the_sdk_from_a_finished_install(
    finished_feishu_install: Path, monkeypatch
) -> None:
    """Fails if ``maybe_start_feishu`` stops calling ``activate()`` before its
    imports: the channel would report the SDK missing and schedule a reinstall."""
    from types import SimpleNamespace

    from kiro_crew.feishu import gateway

    class _Reached(Exception):
        pass

    def _dispatcher(**_kwargs):
        # The step right after the imports: reaching it means they succeeded.
        raise _Reached("imports done")

    scheduled: list[object] = []
    monkeypatch.setattr(gateway, "_LARK_MODULES", (_FAKE_SDK,))
    monkeypatch.setattr(gateway, "FeishuDispatcher", _dispatcher)
    monkeypatch.setattr(gateway, "_schedule_sdk_install", scheduled.append)
    state = SimpleNamespace(feishu_connected=None, feishu_connect_error="")
    orch = SimpleNamespace(
        _feishu_enabled=True,
        _feishu_app_id="app-id-1",
        _feishu_app_secret="secret-1",
        _feishu_allowed_open_ids=["ou_abc123"],
        sessions=object(),
        ctx_builder=object(),
        conv_log=None,
        _approval_mode=None,
        _cfg=SimpleNamespace(agent=SimpleNamespace(approval_mode="interactive")),
        dashboard_state=state,
    )
    assert asyncio.run(gateway.maybe_start_feishu(orch)) is None
    assert scheduled == []
    assert state.feishu_connect_error.startswith("_Reached")
    module = sys.modules[_FAKE_SDK]
    assert module.FROM_EXTRAS is True
    assert Path(module.__file__).parent.parent == finished_feishu_install


def test_an_unfinished_leftover_is_replaced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.mkdir(parents=True)
    (target / "stale.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, []))
    assert sdk_extras.install_extra("feishu", tmp_path) == target
    assert not (target / "stale.py").exists()
    assert (target / sdk_extras.MARKER_FILE).is_file()


@pytest.fixture
def own_lock(tmp_path: Path, monkeypatch) -> Path:
    """A feishu lock the test may rewrite, in place of the shipped one."""
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(sdk_extras, "LOCKS_DIR", locks)
    lock = locks / "feishu.txt"
    lock.write_text("# first\nlark-oapi==1.8.0 \\\n    --hash=sha256:aa\n", encoding="utf-8")
    return lock


def test_the_lock_hash_follows_the_pins_and_nothing_else(own_lock: Path) -> None:
    first = sdk_extras.lock_hash("feishu")
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    own_lock.write_bytes(b"# reworded\r\n\r\nlark-oapi==1.8.0 \\\r\n  --hash=sha256:aa\r\n")
    assert sdk_extras.lock_hash("feishu") == first
    own_lock.write_text("lark-oapi==1.8.1 \\\n    --hash=sha256:aa\n", encoding="utf-8")
    assert sdk_extras.lock_hash("feishu") != first
    own_lock.write_text("lark-oapi==1.8.0 \\\n    --hash=sha256:bb\n", encoding="utf-8")
    assert sdk_extras.lock_hash("feishu") != first
    own_lock.unlink()
    assert sdk_extras.lock_hash("feishu") == ""


@pytest.mark.parametrize(
    "marker",
    ["{}", '{"lock_sha256": "0000"}', '{"lock_sha256": ""}', "[1, 2]", "{not json"],
    ids=["no-lock-recorded", "another-lock", "empty-lock", "not-an-object", "not-json"],
)
def test_an_install_from_another_lock_is_not_loaded_and_is_replaced(
    tmp_path: Path, monkeypatch, marker: str
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.mkdir(parents=True)
    (target / "old_pin.py").write_text("", encoding="utf-8")
    (target / sdk_extras.MARKER_FILE).write_text(marker, encoding="utf-8")
    sdk_extras.activate(tmp_path)
    assert str(target) not in sys.path
    assert sdk_extras.is_installed("feishu", tmp_path) is False
    seen: list[list[str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, seen))
    assert sdk_extras.install_extra("feishu", tmp_path) == target
    assert len(seen) == 1
    assert not (target / "old_pin.py").exists()
    assert (target / "lark_oapi" / "__init__.py").is_file()
    written = json.loads((target / sdk_extras.MARKER_FILE).read_text(encoding="utf-8"))
    assert written[sdk_extras.MARKER_LOCK_KEY] == sdk_extras.lock_hash("feishu")
    assert sdk_extras.is_installed("feishu", tmp_path) is True
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    assert str(target) in sys.path


def test_a_changed_pin_makes_a_finished_install_reinstall(
    tmp_path: Path, monkeypatch, own_lock: Path
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    seen: list[list[str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, seen))
    target = sdk_extras.install_extra("feishu", tmp_path)
    assert sdk_extras.install_extra("feishu", tmp_path) == target
    assert len(seen) == 1
    own_lock.write_text("lark-oapi==1.8.1 \\\n    --hash=sha256:cc\n", encoding="utf-8")
    assert sdk_extras.is_installed("feishu", tmp_path) is False
    assert sdk_extras.install_extra("feishu", tmp_path) == target
    assert len(seen) == 2
    assert sdk_extras.is_installed("feishu", tmp_path) is True


def test_a_failed_reinstall_keeps_the_old_install_on_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.mkdir(parents=True)
    (target / "old_pin.py").write_text("", encoding="utf-8")
    (target / sdk_extras.MARKER_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(1, []))
    with pytest.raises(sdk_extras.InstallError, match="pip said no"):
        sdk_extras.install_extra("feishu", tmp_path)
    assert (target / "old_pin.py").is_file()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]
    assert str(target) not in sys.path


def test_the_old_install_is_put_back_when_the_new_one_cannot_take_its_place(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.mkdir(parents=True)
    (target / "old_pin.py").write_text("", encoding="utf-8")
    (target / sdk_extras.MARKER_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, []))
    real_rename = Path.rename

    def _rename(self, new):
        if self.name.endswith(".partial"):
            raise PermissionError("access denied")
        return real_rename(self, new)

    monkeypatch.setattr(Path, "rename", _rename)
    with pytest.raises(sdk_extras.InstallError, match="access denied"):
        sdk_extras.install_extra("feishu", tmp_path)
    assert (target / "old_pin.py").is_file()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def _raise_oserror(*_args, **_kwargs):
    raise PermissionError("access denied")


@pytest.mark.parametrize("step", ["rename", "mkdir"])
def test_a_disk_error_is_an_install_error_and_leaves_no_partial(
    tmp_path: Path, monkeypatch, step: str
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    target = sdk_extras.extra_dir("feishu", tmp_path)
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, []))
    if step == "rename":
        monkeypatch.setattr(sdk_extras.Path, "rename", _raise_oserror)
    else:
        real_mkdir = Path.mkdir

        def _mkdir(self: Path, *args, **kwargs) -> None:
            if self.name.endswith(".partial"):
                raise PermissionError("access denied")
            real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(sdk_extras.Path, "mkdir", _mkdir)
    with pytest.raises(sdk_extras.InstallError, match="access denied"):
        sdk_extras.install_extra("feishu", tmp_path)
    assert list(target.parent.iterdir()) == []
    assert str(target) not in sys.path


def test_an_unknown_extra_is_refused(tmp_path: Path) -> None:
    with pytest.raises(sdk_extras.InstallError):
        sdk_extras.install_extra("whatsapp", tmp_path)


# --------------------------------------------------------------------------
# POST /api/feishu/sdk/install
# --------------------------------------------------------------------------


def _post():
    return make_mocked_request("POST", "/api/feishu/sdk/install")


def test_the_route_refuses_a_remote_session(monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: False)
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 403


def test_the_route_refuses_a_non_owner(monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod
    import kiro_crew.dashboard.handlers.source_providers as sp

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda req: False)
    installs: list[str] = []
    monkeypatch.setattr(mod.sdk_extras, "install_extra", lambda extra: installs.append(extra))
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status in (401, 403)
    assert installs == []


def _owner_local(monkeypatch, mod) -> None:
    import kiro_crew.dashboard.handlers.source_providers as sp

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda req: True)


def test_the_route_refuses_where_pip_is_the_answer(monkeypatch) -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    _owner_local(monkeypatch, mod)
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: False)
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 400


def test_the_route_installs_and_reports_a_failure(monkeypatch) -> None:
    import json

    import kiro_crew.dashboard.handlers.messaging as mod

    _owner_local(monkeypatch, mod)
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: True)
    installs: list[str] = []
    monkeypatch.setattr(mod.sdk_extras, "install_extra", lambda extra: installs.append(extra))
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 200 and installs == ["feishu"]
    assert json.loads(resp.body)["restart_required"] is True

    def _fail(extra: str) -> None:
        raise mod.sdk_extras.InstallError("ERROR: hashes do not match")

    monkeypatch.setattr(mod.sdk_extras, "install_extra", _fail)
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 502
    body = json.loads(resp.body)
    assert body["code"] == "sdk_install_failed"
    # pip's lines stay in the log: the answer carries a short message of its own.
    assert "ERROR:" not in body["error"]

    def _offline(extra: str) -> None:
        raise mod.sdk_extras.InstallError(
            "ERROR: No matching distribution found for lark-oapi==1.8.0",
            kind=mod.sdk_extras.KIND_DOWNLOAD,
        )

    monkeypatch.setattr(mod.sdk_extras, "install_extra", _offline)
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 502
    body = json.loads(resp.body)
    assert body["code"] == "sdk_download_failed"
    assert "ERROR:" not in body["error"] and "lark-oapi" not in body["error"]


def test_the_route_refuses_the_internal_secret(monkeypatch) -> None:
    """The internal secret is not the owner, even on a local owner-looking request."""
    import kiro_crew.dashboard.handlers.messaging as mod

    _owner_local(monkeypatch, mod)
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: True)
    installs: list[str] = []
    monkeypatch.setattr(mod.sdk_extras, "install_extra", lambda extra: installs.append(extra))
    req = _post()
    req["internal_auth"] = True
    resp = asyncio.run(mod.api_feishu_sdk_install(req))
    assert resp.status in (401, 403)
    assert installs == []


def test_the_route_answers_409_while_an_install_runs(monkeypatch) -> None:
    import json

    import kiro_crew.dashboard.handlers.messaging as mod

    _owner_local(monkeypatch, mod)
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: True)
    installs: list[str] = []
    monkeypatch.setattr(mod.sdk_extras, "install_extra", lambda extra: installs.append(extra))

    async def _while_held():
        async with mod._SDK_INSTALL_LOCK:
            # Bounded: a route that waited on the lock it should refuse would
            # otherwise block here until pytest-timeout kills the worker.
            return await asyncio.wait_for(
                mod.api_feishu_sdk_install(_post()), timeout=default_timeout()
            )

    resp = asyncio.run(_while_held())
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "install_already_running"
    assert installs == []


def test_two_overlapping_installs_run_pip_once(tmp_path: Path, monkeypatch) -> None:
    """The second caller waits for the first and gets its finished install back."""
    import threading

    started = threading.Event()
    release = threading.Event()
    seen: list[list[str]] = []
    real = _fake_pip(0, seen)

    def _slow_pip(argv, **kwargs):
        started.set()
        assert release.wait(30), "the test never released the first install"
        return real(argv, **kwargs)

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sdk_extras.subprocess, "run", _slow_pip)
    results: list[Path] = []
    first = threading.Thread(
        target=lambda: results.append(sdk_extras.install_extra("feishu", tmp_path))
    )
    second = threading.Thread(
        target=lambda: results.append(sdk_extras.install_extra("feishu", tmp_path))
    )
    first.start()
    assert started.wait(30)
    second.start()
    release.set()
    first.join(30)
    second.join(30)
    assert not first.is_alive() and not second.is_alive()
    target = sdk_extras.extra_dir("feishu", tmp_path)
    assert results == [target, target]
    assert len(seen) == 1


def test_a_pip_failure_reports_its_errors_not_its_retries() -> None:
    retries = "\n".join(
        f"WARNING: Retrying (Retry(total={n})) after connection broken: /simple/lark-oapi/"
        for n in range(4, -1, -1)
    )
    output = (
        f"{retries}\n"
        "ERROR: Could not find a version that satisfies the requirement lark-oapi==1.8.0\n"
        "ERROR: No matching distribution found for lark-oapi==1.8.0"
    )
    summary = sdk_extras._pip_failure_summary(output, 1)
    assert "Retrying" not in summary
    assert summary.splitlines() == [
        "ERROR: Could not find a version that satisfies the requirement lark-oapi==1.8.0",
        "ERROR: No matching distribution found for lark-oapi==1.8.0",
    ]
    assert sdk_extras._pip_failure_summary("one\ntwo\nthree\nfour", 1) == "two\nthree\nfour"
    assert sdk_extras._pip_failure_summary("", 2) == "pip exited 2"


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        # Offline: pip retries, then finds no version of the first pin.
        (
            "WARNING: Retrying (Retry(total=0)) after connection broken by "
            "'NewConnectionError(\"Failed to establish a new connection\")'\n"
            "ERROR: Could not find a version that satisfies the requirement "
            "lark-oapi==1.8.0 (from versions: none)\n"
            "ERROR: No matching distribution found for lark-oapi==1.8.0",
            sdk_extras.KIND_DOWNLOAD,
        ),
        ("ERROR: No matching distribution found for lark-oapi==1.8.0", sdk_extras.KIND_DOWNLOAD),
        (
            "ERROR: HTTPSConnectionPool(host='pypi.org', port=443): Max retries exceeded "
            "(Caused by ProxyError('Cannot connect to proxy.'))",
            sdk_extras.KIND_DOWNLOAD,
        ),
        (
            "socket.gaierror: [Errno -3] Temporary failure in name resolution",
            sdk_extras.KIND_DOWNLOAD,
        ),
        (
            "ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org')",
            sdk_extras.KIND_DOWNLOAD,
        ),
        (
            "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.",
            sdk_extras.KIND_INSTALL,
        ),
        (
            "ERROR: Could not install packages due to an OSError: [Errno 28]",
            sdk_extras.KIND_INSTALL,
        ),
        ("", sdk_extras.KIND_INSTALL),
    ],
)
def test_a_pip_failure_is_classed_as_a_download_or_an_install_failure(
    output: str, kind: str
) -> None:
    assert sdk_extras._pip_failure_kind(output) == kind


def test_a_failed_download_is_raised_as_kind_download(tmp_path: Path, monkeypatch) -> None:
    def _offline(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "", "ERROR: No matching distribution found for lark-oapi==1.8.0"
        )

    monkeypatch.setattr(sdk_extras.subprocess, "run", _offline)
    with pytest.raises(sdk_extras.InstallError) as offline:
        sdk_extras.install_extra("feishu", tmp_path)
    assert offline.value.kind == sdk_extras.KIND_DOWNLOAD

    def _stalled(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sdk_extras.subprocess, "run", _stalled)
    with pytest.raises(sdk_extras.InstallError) as stalled:
        sdk_extras.install_extra("feishu", tmp_path)
    assert stalled.value.kind == sdk_extras.KIND_DOWNLOAD

    # Any other pip failure, and a disk error, is the generic kind.
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(1, []))
    with pytest.raises(sdk_extras.InstallError) as refused:
        sdk_extras.install_extra("feishu", tmp_path)
    assert refused.value.kind == sdk_extras.KIND_INSTALL
    assert list(sdk_extras.extra_dir("feishu", tmp_path).parent.iterdir()) == []


def test_the_config_get_offers_the_in_app_install_only_while_missing(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    import kiro_crew.config.loader as loader
    import kiro_crew.dashboard.handlers.messaging as mod

    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: True)
    monkeypatch.setattr(mod, "_channel_sdk_status", lambda channel: (False, False, ""))

    class _State:
        feishu_connected = False
        feishu_connect_error = ""

    req = make_mocked_request("GET", "/api/feishu/config", app={"state": _State()})
    body = json.loads(asyncio.run(mod.api_feishu_config_get(req)).body)
    assert body["sdk_install_in_app"] is True
    monkeypatch.setattr(mod, "_channel_sdk_status", lambda channel: (True, True, ""))
    body = json.loads(asyncio.run(mod.api_feishu_config_get(req)).body)
    assert body["sdk_install_in_app"] is False


def test_the_route_maps_a_disk_error_to_502_and_audits_it(tmp_path: Path, monkeypatch) -> None:
    import json

    import kiro_crew.dashboard.handlers.messaging as mod

    _owner_local(monkeypatch, mod)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(mod.sdk_extras, "install_supported", lambda extra: True)
    monkeypatch.setattr(sdk_extras, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(sdk_extras.subprocess, "run", _fake_pip(0, []))
    monkeypatch.setattr(sdk_extras.Path, "rename", _raise_oserror)
    audits: list[dict] = []

    class _Sel:
        def log_api_access(self, **kwargs) -> None:
            audits.append(kwargs)

    monkeypatch.setattr(mod, "_sel", lambda: _Sel())
    resp = asyncio.run(mod.api_feishu_sdk_install(_post()))
    assert resp.status == 502
    assert json.loads(resp.body)["code"] == "sdk_install_failed"
    (audit,) = audits
    assert audit["operation"] == "feishu.sdk.install"
    assert audit["outcome"] == "error" and "access denied" in audit["error"]
    assert list(sdk_extras.extra_dir("feishu", tmp_path).parent.iterdir()) == []


_MIRROR = "https://aws:s3cr3t-token@corp.example.com/pypi/simple/"
_OFFLINE = "ERROR: No matching distribution found for lark-oapi==1.8.0"


def _scripted_pip(results: list[tuple[int, str]], envs: list[dict[str, str]]):
    """A pip that answers *results* in order and records each attempt's environment."""
    answers = iter(results)

    def run(argv, **kwargs):
        envs.append(dict(kwargs["env"]))
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
        partial = Path(argv[argv.index("--target") + 1])
        assert kwargs["cwd"] == str(partial) and list(partial.iterdir()) == []
        returncode, stderr = next(answers)
        if returncode == 0:
            (partial / "lark_oapi").mkdir()
            (partial / "lark_oapi" / "__init__.py").write_text("", encoding="utf-8")
        else:
            (partial / "half-written").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    return run


def test_the_configured_index_is_used_and_pip_runs_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PIP_INDEX_URL", _MIRROR)
    monkeypatch.setenv("PIP_TRUSTED_HOST", "corp.example.com")
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _scripted_pip([(0, "")], envs))
    target = sdk_extras.install_extra("feishu", tmp_path)
    (env,) = envs
    assert env["PIP_INDEX_URL"] == _MIRROR
    assert env["PIP_TRUSTED_HOST"] == "corp.example.com"
    assert (target / sdk_extras.MARKER_FILE).is_file()


def test_a_mirror_that_cannot_download_falls_back_to_pypi(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PIP_INDEX_URL", _MIRROR)
    envs: list[dict[str, str]] = []
    failed = (1, f"WARNING: 401 from {_MIRROR}\n{_OFFLINE}")
    monkeypatch.setattr(sdk_extras.subprocess, "run", _scripted_pip([failed, (0, "")], envs))
    with caplog.at_level("INFO", logger=sdk_extras.__name__):
        target = sdk_extras.install_extra("feishu", tmp_path)
    first, second = envs
    assert first["PIP_INDEX_URL"] == _MIRROR
    assert not any(name in second for name in sdk_extras._PIP_INDEX_ENV)
    assert (target / "lark_oapi" / "__init__.py").is_file()
    assert not (target / "half-written").exists()
    assert "s3cr3t-token" not in caplog.text
    assert "https://corp.example.com (your pip settings)" in caplog.text
    assert "attempt 2, from pypi.org (pip's default)" in caplog.text


def test_a_mirror_install_error_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PIP_INDEX_URL", _MIRROR)
    envs: list[dict[str, str]] = []
    refused = (1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.")
    monkeypatch.setattr(sdk_extras.subprocess, "run", _scripted_pip([refused], envs))
    with pytest.raises(sdk_extras.InstallError) as err:
        sdk_extras.install_extra("feishu", tmp_path)
    assert err.value.kind == sdk_extras.KIND_INSTALL
    assert len(envs) == 1
    assert list(sdk_extras.extra_dir("feishu", tmp_path).parent.iterdir()) == []


def test_both_attempts_failing_reports_the_last(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PIP_INDEX_URL", _MIRROR)
    envs: list[dict[str, str]] = []
    pip = _scripted_pip([(1, f"ERROR: 401 from {_MIRROR}\n{_OFFLINE}"), (1, _OFFLINE)], envs)
    monkeypatch.setattr(sdk_extras.subprocess, "run", pip)
    with pytest.raises(sdk_extras.InstallError) as err:
        sdk_extras.install_extra("feishu", tmp_path)
    assert len(envs) == 2
    assert err.value.kind == sdk_extras.KIND_DOWNLOAD
    assert str(err.value) == _OFFLINE


def test_nothing_configured_is_one_attempt_on_pypi(tmp_path: Path, monkeypatch) -> None:
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(sdk_extras.subprocess, "run", _scripted_pip([(1, _OFFLINE)], envs))
    with pytest.raises(sdk_extras.InstallError) as err:
        sdk_extras.install_extra("feishu", tmp_path)
    assert err.value.kind == sdk_extras.KIND_DOWNLOAD
    (env,) = envs
    assert not any(name in env for name in sdk_extras._PIP_INDEX_ENV)


def test_pip_config_supplies_the_index_only_when_the_environment_does_not(monkeypatch) -> None:
    listing = (
        "global.index-url='https://global.example/simple'\n"
        "global.timeout='60'\n"
        "install.index-url='https://install.example/simple'\n"
        "install.extra-index-url='https://a.example/simple\\nhttps://b.example/simple'\n"
        'global.trusted-host="install.example"\n'
        "user.index-url='https://ignored.example/simple'\n"
        ":env:.index-url='https://env.example/simple'\n"
    )
    monkeypatch.setattr(sdk_extras, "_pip_config_list", lambda: listing)
    assert sdk_extras._configured_index_env() == {
        "PIP_INDEX_URL": "https://install.example/simple",
        "PIP_EXTRA_INDEX_URL": "https://a.example/simple https://b.example/simple",
        "PIP_TRUSTED_HOST": "install.example",
    }
    monkeypatch.setenv("PIP_INDEX_URL", "https://env.example/simple")
    # The environment overrides pip.conf key by key; the rest of pip.conf stays,
    # so a client certificate kept there still reaches a mirror set in the env.
    assert sdk_extras._configured_index_env() == {
        "PIP_INDEX_URL": "https://env.example/simple",
        "PIP_EXTRA_INDEX_URL": "https://a.example/simple https://b.example/simple",
        "PIP_TRUSTED_HOST": "install.example",
    }
    monkeypatch.setattr(
        sdk_extras, "_pip_config_list", lambda: "global.client-cert='/etc/pki/me.pem'\n"
    )
    assert sdk_extras._configured_index_env() == {
        "PIP_INDEX_URL": "https://env.example/simple",
        "PIP_CLIENT_CERT": "/etc/pki/me.pem",
    }


def test_pip_config_parsing_takes_unquoted_values_and_skips_junk() -> None:
    assert sdk_extras._parse_pip_config(
        "global.index-url=https://plain.example/simple\nnot a setting\nglobal.index-url=''\n"
    ) == {"PIP_INDEX_URL": "https://plain.example/simple"}
    assert sdk_extras._parse_pip_config("") == {}


def test_a_failing_pip_config_means_nothing_configured(monkeypatch) -> None:
    def _broken(argv, **kwargs):
        assert argv == [sys.executable, "-I", "-m", "pip", "config", "list"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sdk_extras.subprocess, "run", _broken)
    assert _REAL_PIP_CONFIG_LIST() == ""
    monkeypatch.setattr(
        sdk_extras.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "global.index-url='x'", ""),
    )
    assert _REAL_PIP_CONFIG_LIST() == ""


def test_a_path_token_is_removed_from_pip_retry_lines() -> None:
    """urllib3 prints only the request path, so the token is removed by value too."""
    token = "0123456789abcdef0123456789abcdef"
    index = {
        "PIP_INDEX_URL": f"https://dl.example.io/{token}/org/repo/python/simple/",
        "PIP_EXTRA_INDEX_URL": "https://u:p4ss@b.example/extra/simple",
    }
    output = (
        "WARNING: Retrying (Retry(total=4)) after connection broken by "
        f"'NewConnectionError(...)': /{token}/org/repo/python/simple/lark-oapi/\n"
        f"WARNING: 401 at /extra/simple/pycryptodome/\n"
        "ERROR: Could not find a version that satisfies the requirement lark-oapi==1.8.0"
    )
    redacted = sdk_extras._redact_pip_output(output, index)
    assert token not in redacted and "p4ss" not in redacted
    assert "/extra/simple" not in redacted
    assert "ERROR: Could not find a version" in redacted


def test_credentials_are_stripped_from_logged_urls() -> None:
    path_token = "https://pkgs.example.com/0123456789abcdef0123456789abcdef/simple/"
    redacted = sdk_extras._redact_urls(f"Looking in indexes: {_MIRROR}, {path_token}\nERROR: x")
    assert redacted == (
        "Looking in indexes: https://corp.example.com, https://pkgs.example.com\nERROR: x"
    )
    assert "s3cr3t" not in redacted and "0123456789abcdef" not in redacted
    described = sdk_extras._describe_index(
        {"PIP_INDEX_URL": _MIRROR, "PIP_EXTRA_INDEX_URL": "https://u:p@b.example/x"}
    )
    assert described == "https://corp.example.com, https://b.example (your pip settings)"
    assert "s3cr3t" not in described

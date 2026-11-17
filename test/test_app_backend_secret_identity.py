"""An app backend cannot read the session-token MAC key, and is adopted only while the
gateway secret it was spawned beside is still the current one.

Two properties, checked with no sandbox, no process and no real secret:

* the spawn of an app backend passes ``trust/sel_hmac.key`` to ``wrap_argv`` as a hidden
  path, and no other spawn plan hides it;
* the identity of the gateway secret files (never their value) is stored in the backend's
  pidfile row at spawn, and a listener whose row names another identity, or none, is not
  adopted.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401

from kiro_crew import sandbox, sandbox_plan
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
_APP = "plain-app"
_OWNER_PID = 4242


def _install_plain(tmp_path: Path, name: str, port: str = "auto") -> None:
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Plain",
                "description": "a backend",
                "backend": {"entryPoint": "server.py", "port": port, "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)


def _write_key(key: Path) -> None:
    """What the gateway's first ``sel()`` does when the trust-root key is absent."""
    key.parent.mkdir(exist_ok=True)
    key.write_text("x" * 40, encoding="utf-8")


def _seed_key(home: Path, monkeypatch) -> Path:
    key = home / "trust" / "sel_hmac.key"
    _write_key(key)
    monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: key)
    return key


def _seed_secrets(home: Path) -> None:
    (home / "run").mkdir(exist_ok=True)
    (home / ".local_secret").write_text("one", encoding="utf-8")
    (home / "run" / "gateway-8765.secret").write_text("one", encoding="utf-8")


def _replace_secrets(home: Path) -> None:
    """A new gateway boot: the old files are unlinked and new ones are written."""
    for name in (".local_secret", "run/gateway-8765.secret"):
        path = home / name
        path.unlink()
        path.write_text("two", encoding="utf-8")
        later = path.stat().st_mtime + 10
        os.utime(path, (later, later))


def _spy_spawn(bmod, monkeypatch) -> dict:
    seen: dict = {}

    def _spy_wrap(argv, **kwargs):
        seen["hidden"] = tuple(kwargs.get("extra_hidden_dirs") or ())
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
    # A copy of the module for the backend only: patching ``subprocess.Popen`` itself would
    # also stop a test that runs a real process (the namespace test).
    refusing = types.SimpleNamespace(
        **{**vars(subprocess), "Popen": lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))}
    )
    monkeypatch.setattr(bmod, "subprocess", refusing)
    return seen


class _Listening:
    """A socket whose connect succeeds: something answers on the port."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_Listening":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def settimeout(self, value) -> None:
        return None

    def connect(self, address) -> None:
        return None


def _listener_on_the_port(bmod, monkeypatch) -> None:
    """The port is held by a healthy backend owned by one fake pid; nothing runs."""
    patched = types.SimpleNamespace(**{**vars(socket), "socket": _Listening})
    monkeypatch.setattr(bmod, "socket", patched)
    monkeypatch.setattr(bmod, "_probe_adoption_health", lambda port, path: True)
    monkeypatch.setattr(
        bmod, "_capture_adopted_owners", lambda *a: ([_OWNER_PID], {_OWNER_PID: "1"})
    )
    monkeypatch.setattr(bmod, "_start_adopted_health_watch", lambda *a, **k: None)
    monkeypatch.setattr(bmod, "_set_backend_health", lambda *a, **k: None)


def _start(bmod, name: str):
    try:
        return bmod.start_app_backend(name)
    finally:
        bmod._processes.pop(name, None)
        bmod._allocated_ports.pop(name, None)


def _audited(bmod, monkeypatch, operation: str) -> list[str]:
    """The outcomes of the audit events the backend module writes for *operation*."""
    outcomes: list[str] = []

    class _Audit:
        def log_api_access(self, **event):
            if event.get("operation") == operation:
                outcomes.append(event["outcome"])

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    monkeypatch.setattr(bmod, "sel", lambda: _Audit())
    return outcomes


class TestTheSpawnHidesTheMacKey:
    def test_an_app_backend_spawn_hides_the_mac_key(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod

        key = _seed_key(app_env, monkeypatch)
        seen = _spy_spawn(bmod, monkeypatch)
        _install_plain(tmp_path, _APP)

        bmod.start_app_backend(_APP)

        assert "hidden" in seen, "the spawn never reached the sandbox wrap"
        assert str(key) in seen["hidden"], "the backend can read trust/sel_hmac.key"

    def test_a_key_the_gateway_creates_before_the_spawn_is_hidden(
        self, app_env, tmp_path, monkeypatch
    ):
        """The key is absent when the spawn starts; the gateway makes it, and it is masked."""
        import kiro_crew.apps.backend as bmod

        key = app_env / "trust" / "sel_hmac.key"
        monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: key)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: _write_key(key))
        seen = _spy_spawn(bmod, monkeypatch)
        _install_plain(tmp_path, _APP)

        bmod.start_app_backend(_APP)

        assert "hidden" in seen, "the spawn never reached the sandbox wrap"
        assert str(key) in seen["hidden"], "the backend could create or read the key"

    def test_a_spawn_is_refused_while_the_key_is_absent(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod

        key = app_env / "trust" / "sel_hmac.key"
        monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: key)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: None)
        seen = _spy_spawn(bmod, monkeypatch)
        _install_plain(tmp_path, _APP)

        assert bmod.start_app_backend(_APP) is None
        assert "hidden" not in seen, "the backend was spawned with no key to mask"

    def test_a_spawn_refused_for_want_of_a_mask_is_audited(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod

        key = app_env / "trust" / "sel_hmac.key"
        monkeypatch.setattr("kiro_crew.sel.sel_hmac_key_path", lambda: key)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: None)
        _spy_spawn(bmod, monkeypatch)
        outcomes = _audited(bmod, monkeypatch, "app_backend_spawn")
        _install_plain(tmp_path, _APP)

        assert bmod.start_app_backend(_APP) is None
        assert outcomes == ["refused_key_mask_unavailable"]

    def test_an_installed_app_named_like_a_builtin_gets_the_key_hidden(
        self, app_env, tmp_path, monkeypatch
    ):
        """Provenance, not the name: a copy installed under a shipped name is third-party."""
        import kiro_crew.apps.backend as bmod

        key = _seed_key(app_env, monkeypatch)
        seen = _spy_spawn(bmod, monkeypatch)
        _install_plain(tmp_path, "file-explorer")

        bmod.start_app_backend("file-explorer")

        assert "hidden" in seen, "the spawn never reached the sandbox wrap"
        assert str(key) in seen["hidden"], "a name-only claim keeps the key readable"

    def test_a_shipped_builtin_keeps_the_key_its_audit_log_needs(self, app_env, monkeypatch):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.manager import register_builtin_apps

        key = _seed_key(app_env, monkeypatch)
        register_builtin_apps()
        seen = _spy_spawn(bmod, monkeypatch)

        bmod.start_app_backend("file-explorer")

        assert "hidden" in seen, "the spawn never reached the sandbox wrap"
        assert str(key) not in seen["hidden"]


class TestTheBuiltinGateIsProvenance:
    """``is_builtin_app`` keys on the resolved path inside the shipped package, never a claim."""

    def test_a_name_alone_is_not_provenance(self):
        from kiro_crew.apps.execution import is_builtin_app

        assert not is_builtin_app(app_name="file-explorer")
        assert not is_builtin_app(app_root=None, app_name="file-explorer")

    def test_a_lookalike_tree_and_a_manifest_claim_are_not_provenance(self, tmp_path):
        from kiro_crew.apps.execution import is_builtin_app, shipped_builtin_app_root

        shipped = shipped_builtin_app_root("file-explorer")
        assert shipped is not None and is_builtin_app(app_root=shipped, app_name="file-explorer")
        fake = tmp_path / "kiro_crew" / "apps" / "builtins" / shipped.name
        fake.mkdir(parents=True)
        (fake / "app.json").write_text(json.dumps({"name": "file-explorer"}), encoding="utf-8")
        (fake / "server.py").write_text("print('x')\n", encoding="utf-8")

        assert not is_builtin_app(app_root=fake / "server.py", app_name="file-explorer")
        assert not is_builtin_app(app_root=fake, app_name="file-explorer")

    @_POSIX_ONLY
    def test_a_link_into_a_lookalike_tree_is_not_provenance(self, tmp_path):
        from kiro_crew.apps.execution import is_builtin_app, shipped_builtin_app_root

        shipped = shipped_builtin_app_root("file-explorer")
        assert shipped is not None
        fake = tmp_path / "copy"
        fake.mkdir()
        (fake / "server.py").write_text("print('x')\n", encoding="utf-8")
        link = tmp_path / "apps" / "file-explorer"
        link.parent.mkdir()
        link.symlink_to(fake, target_is_directory=True)

        assert not is_builtin_app(app_root=link / "server.py", app_name="file-explorer")


@_POSIX_ONLY
class TestThePlan:
    def test_a_plan_with_no_hidden_paths_leaves_the_key_visible(self, app_env, monkeypatch):
        """Agent sessions, MCP servers and crons spawn with no hidden path: unchanged."""
        key = _seed_key(app_env, monkeypatch)
        for mode in ("standard", "cc", "strict"):
            plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, mode)
            assert str(key) not in set(plan.sensitive_dirs), mode

    def test_a_plan_given_the_key_masks_it(self, app_env, monkeypatch):
        key = _seed_key(app_env, monkeypatch)
        plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "standard", extra_hidden_dirs=(str(key),)
        )
        assert str(key) in set(plan.sensitive_dirs)


class TestAdoptionFollowsTheSecretIdentity:
    def _spawned_beside_the_secret(self, bmod, home, tmp_path, monkeypatch):
        _seed_secrets(home)
        # Inside this worker's own port window (the fixture gives each xdist worker one).
        port = bmod._MIN_PORT + 5
        _install_plain(tmp_path, _APP, port=str(port))
        _listener_on_the_port(bmod, monkeypatch)
        bmod._record_app_pid(_APP, os.getpid(), port)

    def test_a_backend_recorded_with_the_current_identity_is_adopted(
        self, app_env, tmp_path, monkeypatch
    ):
        import kiro_crew.apps.backend as bmod

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)

        adopted = _start(bmod, _APP)

        assert adopted is not None and adopted.adopted_pids == [_OWNER_PID]

    def test_a_backend_recorded_with_another_identity_is_not_adopted(
        self, app_env, tmp_path, monkeypatch
    ):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        rows = pidfile._read_pidfile()
        rows[_APP]["secret_id"] = "not-the-current-identity"
        pidfile._write_pidfile(rows)

        assert _start(bmod, _APP) is None

    def test_a_secret_replaced_after_the_spawn_is_not_adopted(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        _replace_secrets(app_env)

        assert _start(bmod, _APP) is None

    def test_a_row_with_no_identity_is_not_adopted(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        rows = pidfile._read_pidfile()
        rows[_APP].pop("secret_id", None)
        pidfile._write_pidfile(rows)

        assert _start(bmod, _APP) is None

    def test_a_refused_backend_is_audited_as_refused_never_as_adopted(
        self, app_env, tmp_path, monkeypatch
    ):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        rows = pidfile._read_pidfile()
        rows[_APP]["secret_id"] = "not-the-current-identity"
        pidfile._write_pidfile(rows)
        outcomes = _audited(bmod, monkeypatch, "app_backend_adopt")

        assert _start(bmod, _APP) is None
        assert outcomes == ["refused_predates_gateway_secret"]

    def test_an_unreadable_pidfile_is_not_read_as_no_row(self, app_env, tmp_path, monkeypatch):
        """A damaged pidfile says nothing about the spawn, so the listener is not adopted."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        pidfile._pidfile_path().write_text("{not json", encoding="utf-8")
        outcomes = _audited(bmod, monkeypatch, "app_backend_adopt")

        assert _start(bmod, _APP) is None
        assert outcomes == ["refused_predates_gateway_secret"]

    def test_a_listener_with_no_row_is_adopted_as_before(self, app_env, tmp_path, monkeypatch):
        """The stated upgrade path: a listener the gateway never recorded is still adopted."""
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)
        rows = pidfile._read_pidfile()
        rows.pop(_APP)
        pidfile._write_pidfile(rows)
        outcomes = _audited(bmod, monkeypatch, "app_backend_adopt")

        adopted = _start(bmod, _APP)

        assert adopted is not None and adopted.adopted_pids == [_OWNER_PID]
        assert outcomes == ["adopted"]

    def test_the_row_never_holds_the_secret_value(self, app_env, tmp_path, monkeypatch):
        import kiro_crew.apps.backend as bmod
        from kiro_crew.apps.backend_runtime import pidfile

        self._spawned_beside_the_secret(bmod, app_env, tmp_path, monkeypatch)

        assert "one" not in json.dumps(pidfile._read_pidfile()[_APP])


def _hidden_paths_of_a_third_party_spawn(app_env, tmp_path, monkeypatch) -> tuple[Path, list[str]]:
    import kiro_crew.apps.backend as bmod

    key = _seed_key(app_env, monkeypatch)
    seen = _spy_spawn(bmod, monkeypatch)
    _install_plain(tmp_path, _APP)
    bmod.start_app_backend(_APP)
    return key, list(seen.get("hidden", ()))


@_POSIX_ONLY
class TestTheLauncherMasksTheKey:
    """The paths the spawn passes are what the real launcher stages mask.

    The libc is the fake that covers a bound name the way a mount does; no namespace and no
    process run.
    """

    def test_the_key_reads_empty_and_its_sibling_is_untouched(self, app_env, tmp_path, monkeypatch):
        from test_sandbox_launcher_program import CoveringLibc, launch, payload

        from kiro_crew import sandbox_launcher_program

        key, hidden = _hidden_paths_of_a_third_party_spawn(app_env, tmp_path, monkeypatch)
        sibling = key.parent / "project-skills.json"
        sibling.write_text("42", encoding="utf-8")
        run = launch(
            tmp_path,
            payload(sensitive_dirs=hidden, sensitive_files=hidden),
            libc=CoveringLibc(),
        )

        sandbox_launcher_program.mask_sensitive(run)
        sandbox_launcher_program.mask_sensitive_files(run)

        assert key.read_text(encoding="utf-8") == "", "the backend still reads the key"
        assert sibling.read_text(encoding="utf-8") == "42"


_CHILD = r"""
import errno, json, os, sys

key = sys.argv[1]
report = {"read": open(key, encoding="utf-8").read()}
try:
    os.unlink(key)
    report["unlink"] = "UNLINKED"
except OSError as exc:
    report["unlink"] = errno.errorcode.get(exc.errno, str(exc.errno))
print("REPORT " + json.dumps(report), flush=True)
"""


@_POSIX_ONLY
class TestInTheNamespaceSandbox:
    """The real launcher: the key reads empty and cannot be unlinked from the backend.

    Needs unprivileged user and mount namespaces, which a host without them skips and CI's
    sandbox jobs run.
    """

    pytestmark = [
        pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
        pytest.mark.skipif(not sandbox.userns_available(), reason="no user and mount namespaces"),
    ]

    def test_a_backend_reads_no_key_and_cannot_remove_it(self, app_env, tmp_path, monkeypatch):
        key, hidden = _hidden_paths_of_a_third_party_spawn(app_env, tmp_path, monkeypatch)
        monkeypatch.setenv("HOME", str(tmp_path))
        sandbox.reset_backend()
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}

        run = subprocess.run(
            sandbox.namespace_argv(
                [sys.executable, "-c", _CHILD, str(key)],
                "standard",
                extra_hidden_dirs=tuple(hidden),
            ),
            cwd=app_env,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        )

        assert run.returncode == 0, run.stdout + run.stderr
        (line,) = [ln for ln in run.stdout.splitlines() if ln.startswith("REPORT ")]
        report = json.loads(line[len("REPORT ") :])
        assert report["read"] == "" and report["unlink"] != "UNLINKED"
        assert key.read_text(encoding="utf-8") == "x" * 40

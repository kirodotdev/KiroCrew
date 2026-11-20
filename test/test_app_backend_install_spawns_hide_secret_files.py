"""The dependency-install spawns of an app backend cannot read the gateway's secrets.

``npm install`` (lifecycle scripts such as ``postinstall``) and ``pip install -r`` (source
builds run setup code) execute third-party code, exactly like the backend itself. Each
spawn must pass the gateway's internal-API secret files to ``wrap_argv`` as
``extra_hidden_dirs``. So must the interpreter probe, which runs an app-planted ``.venv``
interpreter. The npm install must also not receive the per-app proxy secret or the origin
proof in its environment: they are meant for the backend process alone.

Only the arguments handed to ``wrap_argv`` and the environment handed to the spawn are
checked; no sandbox and no process runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import _capture_popen, _manifest, _record_runs, _StopSpawn

import kiro_crew.apps.backend as bmod
from kiro_crew.config.paths import config_dir


def _seed_secret_files() -> tuple[str, ...]:
    home = Path(config_dir())
    (home / "run").mkdir(parents=True, exist_ok=True)
    names = (".local_secret", "run/gateway-8765.secret")
    for name in names:
        (home / name).write_text("x", encoding="utf-8")
    return tuple(str(home / name) for name in names)


def _spy_wrap_argv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], tuple[str, ...]]]:
    calls: list[tuple[list[str], tuple[str, ...]]] = []

    def _wrap(argv: Any, **kwargs: Any) -> Any:
        calls.append((list(argv), tuple(kwargs.get("extra_hidden_dirs") or ())))
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _wrap)
    return calls


class TestTheInstallSpawnsHideTheSecretFiles:
    def test_the_npm_install_spawn_hides_every_secret_file(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = _seed_secret_files()
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        calls = _spy_wrap_argv(monkeypatch)
        _record_runs(monkeypatch)
        _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp", _manifest("server.js"))

        npm = [hidden for argv, hidden in calls if argv[:2] == ["/usr/bin/npm", "install"]]
        assert len(npm) == 1, calls
        for path in secrets:
            assert path in npm[0], f"{path} is readable by npm install"

    def test_the_pip_install_spawn_hides_every_secret_file(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = _seed_secret_files()
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        calls = _spy_wrap_argv(monkeypatch)
        _record_runs(monkeypatch)
        _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("deps", _manifest("server.py"))

        pip = [hidden for argv, hidden in calls if "pip" in argv and "install" in argv]
        assert len(pip) == 1, calls
        for path in secrets:
            assert path in pip[0], f"{path} is readable by pip install"

    def test_the_interpreter_probe_spawn_hides_every_secret_file(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps import interpreter as imod

        secrets = _seed_secret_files()
        py = imod.venv_python_path(tmp_path)
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        seen: list[tuple[str, ...]] = []

        def _wrap(argv: Any, **kwargs: Any) -> Any:
            seen.append(tuple(kwargs.get("extra_hidden_dirs") or ()))
            raise OSError("stop before anything runs")

        monkeypatch.setattr(imod.sandbox, "wrap_argv", _wrap)

        assert imod._venv_is_usable(tmp_path) is False

        assert len(seen) == 1, "the probe never reached the sandbox wrap"
        for path in secrets:
            assert path in seen[0], f"{path} is readable by the interpreter probe"


_PROXY_SECRET = "KIROCREW_PROXY_SECRET"
_ORIGIN_PROOF = "KIROCREW_GATEWAY_ORIGIN_PROOF"


class TestTheNpmInstallEnvHoldsNoBackendOnlySecret:
    def test_the_install_env_has_neither_the_proxy_secret_nor_the_origin_proof(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.dashboard.token_auth as token_auth

        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        monkeypatch.setattr(bmod, "_loopback_gateway_origin", lambda: "http://127.0.0.1:4321")
        # The origin (and so its proof) is injected only for an owner-enabled app.
        monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)
        monkeypatch.setattr(token_auth, "replace_legacy_app_secret", lambda _name: "s3cret")
        install_envs: list[dict[str, str]] = []

        def _run(argv: Any, **kwargs: Any) -> Any:
            if "install" in argv:
                install_envs.append(dict(kwargs.get("env") or {}))
            return SimpleNamespace(returncode=0, stdout="")

        monkeypatch.setattr(bmod, "run_limited", _run)
        spawned = _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("nodeapp", _manifest("server.js"))

        # The backend itself still gets both, so the install env is what is under test.
        assert spawned["kwargs"]["env"][_PROXY_SECRET] == "s3cret"
        assert _ORIGIN_PROOF in spawned["kwargs"]["env"]
        assert len(install_envs) == 1
        assert _PROXY_SECRET not in install_envs[0]
        assert _ORIGIN_PROOF not in install_envs[0]
        assert install_envs[0]["NODE_ENV"] == "production"

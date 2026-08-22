"""Coverage for ``dashboard/handlers/core.py`` error branches and cold paths.

Targets the parts of the module the rest of the suite never reaches: the STT
prerequisite/install/transcribe surface, the SEL + security read endpoints,
the agent-settings PUT validators, the loopback-gated local endpoints
(token / logout), the app-secret exchange, and the session sub-agent routes.

Style follows ``test_api_health.py`` (direct handler calls against a
``MagicMock(spec=web.Request)``) and ``test_config_patch.py`` (real aiohttp
``TestClient`` when the handler needs a genuine request body or streaming
response). Every write lands under the autouse-isolated ``KIROCREW_HOME``
from ``conftest.py``; nothing here touches the network or spawns a process.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import (
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    config_path,
)
from kiro_crew.dashboard.handlers import core as core_mod

# ── shared helpers ───────────────────────────────────────────────────────


def _req(
    *,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    query: dict | None = None,
    app: dict | None = None,
    match_info: dict | None = None,
    user: str | None = "dashboard",
) -> web.Request:
    """A stub request carrying only what these handlers read."""
    req = MagicMock(spec=web.Request)
    req.remote = remote
    req.headers = headers or {}
    req.query = query or {}
    req.app = app if app is not None else {}
    req.match_info = match_info or {}
    req.get = lambda key, default=None: (user if key == "user" else default)
    return req


@pytest.fixture
def fake_sel(monkeypatch) -> MagicMock:
    """Swap the audited SEL seam for a recorder.

    ``core._sel()`` late-binds through the handlers package, so patching the
    package attribute is what the handler observes.
    """
    recorder = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: recorder)
    return recorder


@pytest.fixture
def seeded_config() -> Path:
    """Write a minimal config into the isolated home and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"agent": {"approval_mode": "auto"}}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


@pytest.fixture
def stt_status(monkeypatch):
    """Restore the module-level install-status global after each test."""
    saved = dict(core_mod._stt_install_status)
    yield
    core_mod._stt_install_status = saved


# ── Page + static assets ─────────────────────────────────────────────────


class TestPageAndAssets:
    @pytest.mark.asyncio
    async def test_index_falls_back_when_bundle_missing(self, monkeypatch, tmp_path) -> None:
        """A stale/unbuilt install serves the static guidance page, not a 500."""
        monkeypatch.setattr(core_mod, "_DIST_INDEX", tmp_path / "nope" / "index.html")
        resp = await core_mod.index(_req())
        assert resp.status == 200
        assert core_mod.DASHBOARD_HTML_NOT_FOUND_MARKER in resp.text
        # SECURITY CONTRACT: the cold-start body is served unauthenticated, so
        # it must stay static — no request/session state may leak into it.
        assert "127.0.0.1" not in resp.text

    @pytest.mark.asyncio
    async def test_index_serves_built_bundle(self, monkeypatch, tmp_path) -> None:
        index = tmp_path / "index.html"
        index.write_text("<html>spa</html>", encoding="utf-8", newline="\n")
        monkeypatch.setattr(core_mod, "_DIST_INDEX", index)
        resp = await core_mod.index(_req())
        assert resp.text == "<html>spa</html>"
        assert resp.content_type == "text/html"

    @pytest.mark.asyncio
    async def test_branding_defaults_to_product_name(self) -> None:
        resp = await core_mod.api_branding(_req())
        body = json.loads(resp.body)
        assert body == {
            "bot_name": "Kiro Crew",
            "avatar": "/logo.png",
            "direct_local": True,
        }

    @pytest.mark.asyncio
    async def test_logo_refuses_sensitive_avatar_path(self, monkeypatch) -> None:
        """A configured avatar pointing at a credential path is 404, never served."""
        cfg = SimpleNamespace(dashboard=SimpleNamespace(avatar="/home/u/.ssh/id_rsa"))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.KiroCrewConfig",
            SimpleNamespace(load=lambda: cfg),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.is_sensitive_path", lambda _p: True
        )
        resp = await core_mod.logo(_req())
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_logo_serves_validated_custom_avatar(self, monkeypatch, tmp_path) -> None:
        avatar = tmp_path / "avatar.png"
        avatar.write_bytes(b"png")
        cfg = SimpleNamespace(dashboard=SimpleNamespace(avatar=str(avatar)))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.KiroCrewConfig",
            SimpleNamespace(load=lambda: cfg),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.is_sensitive_path", lambda _p: False
        )
        monkeypatch.setattr(
            "kiro_crew.hooks.validate_file_path", lambda p: str(avatar) if p else None
        )
        resp = await core_mod.logo(_req())
        assert isinstance(resp, web.FileResponse)

    @pytest.mark.asyncio
    async def test_logo_prefers_nightly_variant_on_nightly_build(
        self, monkeypatch, tmp_path
    ) -> None:
        """Nightly builds serve the night-sky logo so the in-app identity
        matches the nightly desktop shell."""
        (tmp_path / "kirocrew-logo.png").write_bytes(b"day")
        nightly = tmp_path / "kirocrew-logo-nightly.png"
        nightly.write_bytes(b"night")
        cfg = SimpleNamespace(dashboard=SimpleNamespace(avatar=""))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.KiroCrewConfig",
            SimpleNamespace(load=lambda: cfg),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path)
        monkeypatch.setattr("kiro_crew.__version__", "9.9.9-nightly.20260812")
        resp = await core_mod.logo(_req())
        assert isinstance(resp, web.FileResponse)
        assert os.path.realpath(resp._path) == os.path.realpath(nightly)

    @pytest.mark.asyncio
    async def test_logo_404_when_no_asset_exists(self, monkeypatch, tmp_path) -> None:
        cfg = SimpleNamespace(dashboard=SimpleNamespace(avatar=""))
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.KiroCrewConfig",
            SimpleNamespace(load=lambda: cfg),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers._STATIC_DIR", tmp_path / "empty")
        monkeypatch.setattr("kiro_crew.__version__", "9.9.9")
        resp = await core_mod.logo(_req())
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_pwa_file_serves_dist_child(self, monkeypatch, tmp_path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "manifest.webmanifest").write_text("{}", encoding="utf-8", newline="\n")
        monkeypatch.setattr(core_mod, "_DIST_DIR", dist)
        resp = await core_mod.pwa_file(_req(match_info={"name": "manifest.webmanifest"}))
        assert isinstance(resp, web.FileResponse)

    @pytest.mark.asyncio
    async def test_pwa_file_404_for_missing_name(self, monkeypatch, tmp_path) -> None:
        dist = tmp_path / "dist"
        dist.mkdir()
        monkeypatch.setattr(core_mod, "_DIST_DIR", dist)
        with pytest.raises(web.HTTPNotFound):
            await core_mod.pwa_file(_req(match_info={"name": "absent.js"}))


# ── STT capability probes ────────────────────────────────────────────────


class TestAppleSiliconProbe:
    def test_non_darwin_is_never_apple_silicon(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        assert core_mod._is_apple_silicon() is False

    def test_native_arm64_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        assert core_mod._is_apple_silicon() is True

    def test_rosetta_falls_back_to_sysctl(self, monkeypatch) -> None:
        """Under Rosetta ``platform.machine()`` lies, so the hardware sysctl is
        the authority."""
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="1\n", returncode=0),
        )
        assert core_mod._is_apple_silicon() is True

    def test_sysctl_failure_is_not_apple_silicon(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")

        def _boom(*_a, **_k):
            raise OSError("no sysctl")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert core_mod._is_apple_silicon() is False


class TestSttProviders:
    def test_mlx_hidden_off_apple_silicon(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: False)
        monkeypatch.setattr(
            "kiro_crew.apple_speech.availability",
            lambda: SimpleNamespace(ok=True),
        )
        providers = core_mod._stt_providers()
        assert "mlx" not in providers
        assert "whisper" in providers

    def test_apple_hidden_when_framework_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(
            "kiro_crew.apple_speech.availability",
            lambda: SimpleNamespace(ok=False),
        )
        providers = core_mod._stt_providers()
        assert "apple" not in providers
        assert "mlx" in providers


class TestSttPrereqCommands:
    @pytest.fixture(autouse=True)
    def _no_ffmpeg_probe(self, monkeypatch):
        monkeypatch.setattr(core_mod, "ensure_ffmpeg_in_path", lambda: None)

    def test_mlx_off_apple_silicon_has_no_prereqs(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: False)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        assert core_mod._stt_prereq_commands("mlx") == []

    def test_mlx_needs_only_homebrew(self, monkeypatch) -> None:
        """The Install button bootstraps everything except Homebrew itself."""
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        monkeypatch.setattr(core_mod, "find_brew", lambda: None)
        cmds = core_mod._stt_prereq_commands("mlx")
        assert len(cmds) == 1
        assert "install.sh" in cmds[0]

    def test_mlx_with_homebrew_and_ffmpeg_present_is_clean(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: "/usr/local/bin/ffmpeg")
        monkeypatch.setattr(core_mod, "find_brew", lambda: "/opt/homebrew/bin/brew")
        assert core_mod._stt_prereq_commands("mlx") == []

    def test_mlx_surfaces_ffmpeg_when_missing(self, monkeypatch) -> None:
        """A ready mlx install that later loses ffmpeg has no Install button —
        the prereq list is what the ready-state warning renders."""
        monkeypatch.setattr(core_mod, "_is_apple_silicon", lambda: True)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        monkeypatch.setattr(core_mod, "find_brew", lambda: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        assert core_mod._stt_prereq_commands("mlx") == ["brew install ffmpeg"]

    def test_transcribe_prereq_targets_the_gateway_interpreter(self, monkeypatch) -> None:
        """Transcribe's requirement is the `voice` extra importable by THIS
        process, so the command must name the gateway's own interpreter — a
        system python or --user install would not be importable here."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_voice_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
        monkeypatch.setattr(core_mod.os, "name", "posix")
        cmds = core_mod._stt_prereq_commands("transcribe")
        assert len(cmds) == 1
        assert "kirocrew[voice]" in cmds[0]
        assert "-m pip install" in cmds[0]
        assert core_mod.shlex.quote(core_mod.sys.executable) in cmds[0]

    def test_transcribe_prereq_windows_uses_call_operator(self, monkeypatch) -> None:
        """POSIX single-quoting breaks on Windows shells; the PowerShell form
        must be emitted there instead."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_voice_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: "C:\\ffmpeg\\ffmpeg.exe")
        monkeypatch.setattr(core_mod.os, "name", "nt")
        cmds = core_mod._stt_prereq_commands("transcribe")
        assert cmds == [f'& "{core_mod.sys.executable}" -m pip install "kirocrew[voice]"']

    def test_transcribe_prereq_without_install_channel_is_empty(self, monkeypatch) -> None:
        """When no install channel can make the extra importable (bundled
        interpreter, pip-less interpreter, PEP 668), emitting a pip command would
        recreate the press-and-nothing-changes dead end — the UI shows the
        unsupported notice via `transcribe_unsupported` instead."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: False)
        monkeypatch.setattr(core_mod, "_voice_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_transcribe_prereq_self_suppresses_when_extra_present(self, monkeypatch) -> None:
        """Like every other branch of this function, the pip command must not
        be shown once the requirement is met (e.g. STT merely disabled)."""
        monkeypatch.setattr(core_mod, "_voice_extra_importable", lambda: True)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_transcribe_prereq_includes_ffmpeg_when_missing(self, monkeypatch) -> None:
        """Transcribe needs ffmpeg to remux the browser's .webm, and
        is_available() only logs a warning when it is absent — this list is the
        one user-visible surface for that gap."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_voice_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.os, "name", "posix")
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        cmds = core_mod._stt_prereq_commands("transcribe")
        assert len(cmds) == 2
        assert "kirocrew[voice]" in cmds[0]
        assert cmds[1] == "brew install ffmpeg"

    def test_darwin_lists_license_brew_and_packages(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        monkeypatch.setattr(core_mod, "find_brew", lambda: None)
        monkeypatch.setattr(core_mod, "_find_suitable_python", lambda: None)

        def _no_xcrun(*_a, **_k):
            raise FileNotFoundError("xcrun")

        monkeypatch.setattr(subprocess, "run", _no_xcrun)
        cmds = core_mod._stt_prereq_commands()
        assert any("xcodebuild -license" in c for c in cmds)
        assert any("install.sh" in c for c in cmds)
        assert any(c.startswith("brew install ") and "ffmpeg" in c for c in cmds)

    def test_darwin_fully_provisioned_has_no_prereqs(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: "/usr/bin/" + _n)
        monkeypatch.setattr(core_mod, "find_brew", lambda: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(core_mod, "_find_suitable_python", lambda: "/usr/bin/python3")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="")
        )
        assert core_mod._stt_prereq_commands() == []

    def test_debian_uses_apt_get(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_is_al2023", lambda: False)
        monkeypatch.setattr(core_mod, "_find_suitable_python", lambda: None)
        monkeypatch.setattr(
            core_mod.shutil,
            "which",
            lambda n: "/usr/bin/apt-get" if n == "apt-get" else None,
        )
        cmds = core_mod._stt_prereq_commands()
        assert any("apt-get install -y python3" in c for c in cmds)
        assert any(c == "sudo apt-get install -y ffmpeg" for c in cmds)

    def test_al2023_uses_dnf_and_ffmpeg_build_script(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_is_al2023", lambda: True)
        monkeypatch.setattr(core_mod, "_find_suitable_python", lambda: None)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "build-ffmpeg.sh").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        cmds = core_mod._stt_prereq_commands()
        assert any("dnf install -y python3.11" in c for c in cmds)
        assert any("build-ffmpeg.sh" in c for c in cmds)

    def test_al2_without_build_script_points_at_upstream(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_is_al2023", lambda: False)
        monkeypatch.setattr(core_mod, "_find_suitable_python", lambda: "/usr/bin/python3")
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n: None)
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        cmds = core_mod._stt_prereq_commands()
        assert cmds == ["echo 'Build ffmpeg from source: https://ffmpeg.org/releases/'"]


class TestPipInstallChannel:
    @pytest.fixture(autouse=True)
    def _not_bundled(self, monkeypatch):
        """Pin the desktop-bundle probe; the bundled case has its own test."""
        monkeypatch.setattr(
            core_mod.platform_compat, "is_bundled_interpreter", lambda: False
        )

    def test_bundled_desktop_interpreter_has_no_channel(self, monkeypatch) -> None:
        """A pip install into the desktop app's code-signed bundle breaks
        launches/updates and is discarded on every app update — the command
        must not be offered there even though pip itself may exist."""
        monkeypatch.setattr(
            core_mod.platform_compat, "is_bundled_interpreter", lambda: True
        )
        assert core_mod._pip_install_channel_available() is False

    def test_pipless_interpreter_has_no_channel(self, monkeypatch) -> None:
        """uv tool installs and some pipx layouts ship no `pip` module, so
        `<python> -m pip` fails immediately — the command must not be shown."""
        real = core_mod.importlib.util.find_spec
        monkeypatch.setattr(
            core_mod.importlib.util,
            "find_spec",
            lambda name, *a: None if name == "pip" else real(name, *a),
        )
        assert core_mod._pip_install_channel_available() is False

    def test_externally_managed_python_has_no_channel(self, monkeypatch, tmp_path) -> None:
        """PEP 668: pip refuses installs into an externally-managed
        interpreter (distro/brew pythons) — but only outside a venv."""
        monkeypatch.setattr(core_mod.sys, "prefix", core_mod.sys.base_prefix)
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(core_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is False

    def test_venv_on_managed_base_has_a_channel(self, monkeypatch, tmp_path) -> None:
        """Inside a venv pip ignores PEP 668, and `sysconfig.get_path("stdlib")`
        resolves to the BASE interpreter's directory where distro pythons put
        the marker — the recommended install layout (venv on a Debian/brew
        python) must not be misread as unsupported."""
        monkeypatch.setattr(core_mod.sys, "prefix", str(tmp_path / "venv"))
        monkeypatch.setattr(core_mod.sys, "base_prefix", str(tmp_path / "base"))
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(core_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True

    def test_ordinary_venv_has_a_channel(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(core_mod.sys, "prefix", core_mod.sys.base_prefix)
        monkeypatch.setattr(core_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True


class TestAl2023Detection:
    def test_release_file_naming_2023(self, monkeypatch) -> None:
        monkeypatch.setattr(
            core_mod,
            "Path",
            lambda _p: SimpleNamespace(read_text=lambda **_k: "Amazon Linux release 2023"),
        )
        assert core_mod._is_al2023() is True

    def test_unreadable_release_file_is_false(self, monkeypatch) -> None:
        def _raise(**_k):
            raise OSError("no such file")

        monkeypatch.setattr(core_mod, "Path", lambda _p: SimpleNamespace(read_text=_raise))
        assert core_mod._is_al2023() is False


class TestFindSuitablePython:
    """The reject predicate must SKIP an unusable interpreter, never abort."""

    @staticmethod
    def _capture(monkeypatch):
        captured: dict = {}

        def _fake(reject=None):
            captured["reject"] = reject
            return "/usr/bin/python3"

        monkeypatch.setattr(
            "kiro_crew.platform_compat.find_python_interpreter", _fake
        )
        assert core_mod._find_suitable_python() == "/usr/bin/python3"
        return captured["reject"]

    def test_free_threaded_build_is_rejected(self, monkeypatch) -> None:
        reject = self._capture(monkeypatch)
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *a, **k: "3.14.0 free-threading build",
        )
        assert reject("/usr/bin/python3.14t") is True

    def test_interpreter_with_pip_is_accepted(self, monkeypatch) -> None:
        reject = self._capture(monkeypatch)
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "3.12.1 (main)")
        assert reject("/usr/bin/python3.12") is False

    def test_missing_pip_is_rejected(self, monkeypatch) -> None:
        reject = self._capture(monkeypatch)

        def _fake(args, **_k):
            if "pip" in args:
                raise subprocess.CalledProcessError(1, args)
            return "3.12.1 (main)"

        monkeypatch.setattr(subprocess, "check_output", _fake)
        assert reject("/usr/bin/python3.12") is True

    def test_unspawnable_interpreter_is_rejected(self, monkeypatch) -> None:
        def _boom(*_a, **_k):
            raise OSError("exec format error")

        reject = self._capture(monkeypatch)
        monkeypatch.setattr(subprocess, "check_output", _boom)
        assert reject("/usr/bin/broken") is True


class TestInstallScript:
    def test_path_prelude_defers_to_brew_shellenv(self) -> None:
        prelude = core_mod._stt_install_path_prelude()
        assert "brew shellenv" in prelude
        assert "export PATH" in prelude

    def test_mlx_script_installs_via_pipx(self) -> None:
        script = core_mod._build_stt_install_script("mlx")
        assert "pipx install --force mlx-whisper" in script
        assert "openai-whisper" not in script

    def test_default_script_prefers_brew_then_pip_user(self) -> None:
        script = core_mod._build_stt_install_script()
        assert "brew install openai-whisper" in script
        # The pip fallback must target a SYSTEM python with --user, never the
        # gateway venv (which is replaced on every upgrade).
        assert "--user" in script
        assert "--only-binary" in script


# ── STT config endpoint ─────────────────────────────────────────────────


def _stt_app() -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/api/config/stt", core_mod.api_stt_config)
    return app


class TestSttConfigEndpoint:
    @pytest.fixture(autouse=True)
    def _quiet_probes(self, monkeypatch):
        monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        monkeypatch.setattr(core_mod, "is_available", lambda _cfg: False)

    @pytest.mark.asyncio
    async def test_put_rejects_malformed_body(self, seeded_config) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put("/api/config/stt", data=b"not json")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_put_fails_loud_on_corrupt_config(self, seeded_config) -> None:
        """A corrupt config must NOT be silently rebuilt from {} — that would
        durably clobber every unrelated user setting."""
        seeded_config.write_text("{ this is not json", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put("/api/config/stt", json={"enabled": True})
            assert resp.status == 500
            assert (await resp.json())["error"] == "failed to read config file"
        # The unparseable bytes are left exactly as they were.
        assert seeded_config.read_text(encoding="utf-8") == "{ this is not json"

    @pytest.mark.asyncio
    async def test_put_persists_recognised_fields_only(self, seeded_config) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put(
                "/api/config/stt",
                json={
                    "enabled": True,
                    "provider": "whisper",
                    "model": "turbo",
                    "mlx_model": "mlx-community/whisper-large-v3-turbo",
                    "transcribe_region": "us-west-2",
                    "transcribe_profile": "default",
                    "language_code": "en-US",
                    "streaming": True,
                    "endpointing": False,
                    "dictation_panel": True,
                    "provider_bogus": "ignored",
                },
            )
            assert resp.status == 200
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["enabled"] is True
        assert stt["provider"] == "whisper"
        assert stt["model"] == "turbo"
        assert stt["transcribe_region"] == "us-west-2"
        assert stt["language_code"] == "en-US"
        assert stt["streaming"] is True
        assert stt["endpointing"] is False
        assert stt["dictation_panel"] is True
        assert "provider_bogus" not in stt
        # The pre-existing unrelated section survived the read-modify-write.
        agent = json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]
        assert agent["approval_mode"] == "auto"

    @pytest.mark.asyncio
    async def test_put_ignores_unknown_enum_values(self, seeded_config) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put(
                "/api/config/stt",
                json={"provider": "not-a-provider", "model": "not-a-model"},
            )
            assert resp.status == 200
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt.get("provider") != "not-a-provider"
        assert stt.get("model") != "not-a-model"

    @pytest.mark.asyncio
    async def test_get_advertises_capabilities(self, seeded_config) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            body = await resp.json()
        # Streaming capability is served from the backend's own set so the
        # Settings UI gates on a CAPABILITY rather than a provider name.
        assert body["streaming_providers"] == ["transcribe", "apple"]
        assert body["models"] == {"turbo": "~1.6 GB"}
        assert body["language_codes"][0] == "en-US"
        assert body["available"] is False
        assert body["prereqs"] == []
        assert body["install_step"] in ("idle", "done", "error")
        # This test venv has a working pip channel, so the unsupported flag
        # must be False regardless of installed extras.
        assert body["transcribe_unsupported"] is False
        # Cause discriminator for the unsupported notice: the desktop bundle
        # needs different guidance than a pip-less/PEP 668 interpreter. A test
        # venv is never the bundled app.
        assert body["bundled_interpreter"] is False
        # Served independently of `available` so the UI can flag the .webm
        # remux gap even when the provider reads ready.
        assert isinstance(body["ffmpeg_missing"], bool)


# ── STT install endpoint ────────────────────────────────────────────────


def _proc(lines: list[bytes], returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = SimpleNamespace(readline=AsyncMock(side_effect=[*lines, b""]))
    proc.wait = AsyncMock(return_value=returncode)
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = returncode
    return proc


class TestSttInstall:
    @pytest.mark.asyncio
    async def test_concurrent_install_is_refused(self, fake_sel, stt_status) -> None:
        core_mod._stt_install_status = {
            "step": "installing_ffmpeg",
            "detail": "",
            "error": "",
        }
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 409
        assert "already in progress" in json.loads(resp.body)["error"]
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_transcribe_install_is_refused(self, fake_sel, stt_status) -> None:
        """The install script only knows local Whisper runtimes; running it for
        Transcribe succeeds while changing nothing. The endpoint must refuse
        instead, and must NOT flip the status machine (a 400 that left
        `starting` behind would deadlock the 409 gate)."""
        core_mod._stt_install_status = {"step": "idle", "detail": "", "error": ""}
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"stt": {"provider": "transcribe"}}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["code"] == "stt_no_local_install"
        assert "voice" in body["error"]
        assert core_mod._stt_install_status["step"] == "idle"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_successful_install_walks_the_progress_steps(
        self, monkeypatch, fake_sel, stt_status
    ) -> None:
        core_mod._stt_install_status = {"step": "idle", "detail": "", "error": ""}
        lines = [
            b"Checking Xcode tools\n",
            b"Installing Homebrew\n",
            b"Installing ffmpeg\n",
            b"Installing openai-whisper\n",
            b"Installing mlx-whisper\n",
            b"No suitable python3 found\n",
            b"Using: /usr/bin/python3\n",
            b"ERROR: recoverable hiccup\n",
            b"Done.\n",
        ]

        async def _spawn(*_a, **_k):
            return _proc(lines)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 200
        assert json.loads(resp.body)["ok"] is True
        assert core_mod._stt_install_status["step"] == "done"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_failed_install_reports_tail_of_output(
        self, monkeypatch, fake_sel, stt_status
    ) -> None:
        core_mod._stt_install_status = {"step": "idle", "detail": "", "error": ""}

        async def _spawn(*_a, **_k):
            return _proc([b"boom\n"], returncode=1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["ok"] is False
        assert "boom" in body["error"]
        assert core_mod._stt_install_status["step"] == "error"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "failed"

    @pytest.mark.asyncio
    async def test_install_timeout_kills_the_child(
        self, monkeypatch, fake_sel, stt_status
    ) -> None:
        core_mod._stt_install_status = {"step": "idle", "detail": "", "error": ""}
        proc = MagicMock()
        proc.stdout = SimpleNamespace(readline=AsyncMock(side_effect=asyncio.TimeoutError))
        proc.communicate = AsyncMock(return_value=(b"", b""))

        async def _spawn(*_a, **_k):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 500
        assert json.loads(resp.body)["error"] == "Install timed out"
        proc.kill.assert_called_once()
        assert core_mod._stt_install_status["step"] == "error"

    @pytest.mark.asyncio
    async def test_missing_shell_is_reported_not_raised(
        self, monkeypatch, fake_sel, stt_status
    ) -> None:
        core_mod._stt_install_status = {"step": "idle", "detail": "", "error": ""}

        async def _spawn(*_a, **_k):
            raise FileNotFoundError("bash")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
        resp = await core_mod.api_stt_install(_req())
        assert resp.status == 500
        assert json.loads(resp.body)["error"] == "bash not found"
        assert core_mod._stt_install_status["error"] == "bash not found"


# ── STT transcribe endpoint ─────────────────────────────────────────────


def _multipart_req(field) -> web.Request:
    reader = SimpleNamespace(next=AsyncMock(return_value=field))
    req = _req()
    req.multipart = AsyncMock(return_value=reader)
    return req


class TestSttTranscribe:
    @pytest.mark.asyncio
    async def test_unavailable_backend_is_503(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: False)
        resp = await core_mod.api_stt_transcribe(_req())
        assert resp.status == 503
        assert json.loads(resp.body)["error"] == "STT not available"

    @pytest.mark.asyncio
    async def test_missing_audio_field_is_400(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: True)
        resp = await core_mod.api_stt_transcribe(_multipart_req(None))
        assert resp.status == 400
        assert json.loads(resp.body)["error"] == "missing audio field"

    @pytest.mark.asyncio
    async def test_wrong_field_name_is_400(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: True)
        field = SimpleNamespace(name="video", filename="x.webm", read_chunk=AsyncMock())
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_upload_is_413(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: True)
        field = SimpleNamespace(
            name="audio",
            filename="recording.mp4",
            read_chunk=AsyncMock(return_value=b"0" * (25 * 1024 * 1024 + 1)),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 413
        assert json.loads(resp.body)["error"] == "audio too large"

    @pytest.mark.asyncio
    async def test_transcript_is_returned_and_redacted(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: True)
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(return_value="hello from the meeting"),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.ogg",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 200
        assert json.loads(resp.body)["text"] == "hello from the meeting"

    @pytest.mark.asyncio
    async def test_backend_failure_is_a_generic_500(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.transcribe.is_available", lambda *_a: True)
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(side_effect=RuntimeError("whisper exploded")),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.webm",
            read_chunk=AsyncMock(side_effect=[b"x", b""]),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 500
        # The internal exception text must not reach the client.
        body = json.loads(resp.body)
        assert body["error"] == "transcription failed"
        assert "whisper exploded" not in json.dumps(body)


# ── Security event log + posture ────────────────────────────────────────


class TestSelEndpoints:
    @pytest.mark.asyncio
    async def test_events_uses_default_limit(self, fake_sel) -> None:
        fake_sel.recent.return_value = [{"event": "a"}]
        resp = await core_mod.api_sel_events(_req())
        assert json.loads(resp.body) == {"events": [{"event": "a"}], "count": 1}
        assert fake_sel.recent.call_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_events_caps_limit_at_1000(self, fake_sel) -> None:
        fake_sel.recent.return_value = []
        await core_mod.api_sel_events(_req(query={"limit": "99999"}))
        assert fake_sel.recent.call_args.kwargs["limit"] == 1000

    @pytest.mark.asyncio
    async def test_events_falls_back_on_unparsable_limit(self, fake_sel) -> None:
        fake_sel.recent.return_value = []
        await core_mod.api_sel_events(_req(query={"limit": "many"}))
        assert fake_sel.recent.call_args.kwargs["limit"] == 100

    @pytest.mark.asyncio
    async def test_verify_reports_intact_chain(self, fake_sel) -> None:
        fake_sel.verify_integrity.return_value = (7, 7)
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body == {"total": 7, "valid": 7, "integrity": "ok", "tampered": 0}

    @pytest.mark.asyncio
    async def test_verify_reports_tampering(self, fake_sel) -> None:
        fake_sel.verify_integrity.return_value = (7, 5)
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["integrity"] == "compromised"
        assert body["tampered"] == 2


class TestSecurityStats:
    @pytest.mark.asyncio
    async def test_counts_are_derived_from_the_posture_registry(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.security.build_denied_commands_snapshot_async",
            AsyncMock(return_value={"effective_count": 42}),
        )
        monkeypatch.setattr(
            core_mod,
            "posture_counts_async",
            AsyncMock(
                return_value={
                    "suspicious_patterns": 3,
                    "tool_schemas": 4,
                    "redaction_paths": 5,
                }
            ),
        )
        body = json.loads((await core_mod.api_security_stats(_req())).body)
        assert body == {
            "denied_commands": 42,
            "suspicious_patterns": 3,
            "tool_schemas": 4,
            "redaction_paths": 5,
        }

    @pytest.mark.asyncio
    async def test_denied_count_failure_degrades_to_zero(self, monkeypatch) -> None:
        """An unreadable denylist must not take the whole stats endpoint down."""
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.security.build_denied_commands_snapshot_async",
            AsyncMock(side_effect=OSError("unreadable")),
        )
        monkeypatch.setattr(core_mod, "posture_counts_async", AsyncMock(return_value={}))
        body = json.loads((await core_mod.api_security_stats(_req())).body)
        assert body["denied_commands"] == 0
        assert body["suspicious_patterns"] is None


# ── Agent settings PUT (/api/config/kirocrew) ───────────────────────────


def _agent_cfg_app() -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/api/config/kirocrew", core_mod.api_kirocrew_config)
    return app


async def _put_agent(client, settings: dict):
    return await client.put("/api/config/kirocrew", json={"agent": settings})


class TestAgentSettingsPut:
    @pytest.mark.asyncio
    async def test_malformed_body_is_denied_and_audited(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await client.put("/api/config/kirocrew", data=b"{{{")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_missing_agent_object_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await client.put("/api/config/kirocrew", json={"agent": "nope"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "agent must be an object"

    @pytest.mark.asyncio
    async def test_corrupt_config_is_500_not_a_silent_reset(
        self, seeded_config, fake_sel
    ) -> None:
        seeded_config.write_text("<<not json>>", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
            assert resp.status == 500
            assert (await resp.json())["error"] == "config.json is corrupt"
        assert seeded_config.read_text(encoding="utf-8") == "<<not json>>"

    @pytest.mark.asyncio
    async def test_out_of_range_turns_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(
                client, {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING + 1}
            )
            assert resp.status == 400
            assert "between 1 and" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_boolean_is_not_an_integer(self, seeded_config, fake_sel) -> None:
        """``True`` is an ``int`` subclass in Python — the validator must still
        refuse it, or a JSON ``true`` would silently persist as 1."""
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_auto_max_above_ceiling_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(
                client, {"subagent_auto_max": SUBAGENT_AUTO_MAX_CEILING + 1}
            )
            assert resp.status == 400
            assert "subagent_auto_max must be an integer" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_same_request_ceiling_raise_cannot_widen_the_pin(
        self, seeded_config, fake_sel
    ) -> None:
        """Deny-by-default: ``{subagent_auto_max: N, max_subagents: N}`` must not
        let one request raise the ceiling and immediately spend it."""
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(
                client,
                {
                    "subagent_auto_max": SUBAGENT_AUTO_MAX_CEILING,
                    "max_subagents": SUBAGENT_AUTO_MAX_CEILING,
                },
            )
            assert resp.status == 400
            assert "max_subagents must be 0 (auto)" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_corrupt_persisted_ceiling_is_clamped(self, seeded_config, fake_sel) -> None:
        """A hand-edited ceiling must not be trusted to widen the bound."""
        seeded_config.write_text(
            json.dumps({"agent": {"subagent_auto_max": 9999}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": 9999})
            assert resp.status == 400
            error = (await resp.json())["error"]
            assert f"and {SUBAGENT_AUTO_MAX_CEILING}" in error

    @pytest.mark.asyncio
    async def test_fixed_pin_below_floor_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": MAX_SUBAGENTS_FIXED_FLOOR - 1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_auto_sentinel_is_accepted(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"max_subagents": 0})
            assert resp.status == 200
            assert (await resp.json())["restart_required"] is True
        assert json.loads(seeded_config.read_text(encoding="utf-8"))["agent"][
            "max_subagents"
        ] == 0

    @pytest.mark.asyncio
    async def test_non_boolean_toggle_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"conductor_skill": "yes"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "conductor_skill must be a boolean"

    @pytest.mark.asyncio
    async def test_empty_settings_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"unknown_key": 1})
            assert resp.status == 400
            assert (await resp.json())["error"] == "no recognized settings provided"

    @pytest.mark.asyncio
    async def test_resent_unchanged_value_does_not_ask_for_a_restart(
        self, seeded_config, fake_sel
    ) -> None:
        """The dashboard sends all settings on every save, so "was applied" is
        not "was changed" — the restart hint has to stay trustworthy."""
        seeded_config.write_text(
            json.dumps({"agent": {"subagent_max_turns": 9}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 9})
            assert resp.status == 200
            assert (await resp.json()) == {"ok": True, "restart_required": False}

    @pytest.mark.asyncio
    async def test_conductor_enable_regenerates_the_skill(
        self, seeded_config, fake_sel, monkeypatch
    ) -> None:
        regen = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.handlers.agents._regen_conductor", regen)
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"conductor_skill": True})
            assert resp.status == 200
            # A conductor-only save is applied in-request, so no restart hint.
            assert (await resp.json())["restart_required"] is False
        regen.assert_called_once()

    @pytest.mark.asyncio
    async def test_conductor_disable_removes_the_skill_file(
        self, seeded_config, fake_sel, tmp_path
    ) -> None:
        from kiro_crew.skills import SkillsLoader

        skill = SkillsLoader()._dir / "conductor" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("# conductor\n", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"conductor_skill": False})
            assert resp.status == 200
        assert not skill.exists()

    @pytest.mark.asyncio
    async def test_get_drops_edition_contributed_sections(self, seeded_config) -> None:
        """Unknown top-level sections exist only for the save round-trip; the
        browser-facing view must omit them so an edition secret cannot leak."""
        seeded_config.write_text(
            json.dumps({"agent": {}, "some_edition": {"api_key": "s3cret"}}),
            encoding="utf-8",
            newline="\n",
        )
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            body = await (await client.get("/api/config/kirocrew")).json()
        assert "some_edition" not in body
        assert "s3cret" not in json.dumps(body)
        assert "agent" in body


# ── PATCH validators not reachable through the editable-field table ─────


class TestPatchGuards:
    @pytest.mark.asyncio
    async def test_moved_field_names_its_replacement_endpoint(
        self, seeded_config, fake_sel
    ) -> None:
        """A dead end ("not editable") becomes a next step for fields whose
        side effects the generic write cannot reproduce."""
        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.apps_allow_third_party", "value": False},
            )
            assert resp.status == 400
            assert "trusted-apps/allow-all" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_unknown_field_is_refused(self, seeded_config, fake_sel) -> None:
        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/config/kirocrew", json={"path": "agent.nope", "value": 1}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "field not editable: agent.nope"


class TestAdvertisedModelGuards:
    def test_unknown_when_no_session_has_initialised(self) -> None:
        assert core_mod._active_advertised_ids(_req(app={})) is None

    def test_provider_without_a_model_getter_is_skipped(self) -> None:
        state = SimpleNamespace(
            sessions=SimpleNamespace(active_providers=lambda: [SimpleNamespace()])
        )
        assert core_mod._active_advertised_ids(_req(app={"state": state})) is None

    def test_raising_getter_does_not_propagate(self) -> None:
        def _boom():
            raise RuntimeError("provider is mid-restart")

        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [SimpleNamespace(available_models=_boom)]
            )
        )
        assert core_mod._active_advertised_ids(_req(app={"state": state})) is None

    def test_first_provider_with_ids_wins(self) -> None:
        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [
                    SimpleNamespace(available_models=lambda: []),
                    SimpleNamespace(
                        available_models=lambda: [{"modelId": "claude-sonnet-4.6"}]
                    ),
                ]
            )
        )
        ids = core_mod._active_advertised_ids(_req(app={"state": state}))
        assert ids == ["claude-sonnet-4.6"]

    @pytest.mark.parametrize("value", ["", "auto"])
    def test_defer_values_always_allowed(self, value) -> None:
        assert core_mod._validate_role_model(value, _req()) is None

    def test_provider_rejection_is_surfaced(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v: "display-only key",
        )
        assert core_mod._validate_role_model("fable-5-1m", _req()) == "display-only key"

    def test_unknown_entitlement_does_not_accuse(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason", lambda _v: None
        )
        assert core_mod._validate_role_model("some-model", _req(app={})) is None

    def test_unentitled_model_lists_usable_alternatives(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason", lambda _v: None
        )
        state = SimpleNamespace(
            sessions=SimpleNamespace(
                active_providers=lambda: [
                    SimpleNamespace(available_models=lambda: [{"modelId": "allowed-1"}])
                ]
            )
        )
        reason = core_mod._validate_role_model("denied-1", _req(app={"state": state}))
        assert reason is not None
        assert "allowed-1" in reason

    def test_pool_agent_values_include_the_clear_sentinel(self) -> None:
        assert "" in core_mod._agent_values()


# ── Loopback + local-secret gated endpoints ─────────────────────────────


class TestLocalToken:
    @pytest.mark.asyncio
    async def test_non_loopback_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        resp = await core_mod.api_token_local(_req(remote="203.0.113.9"))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "non-loopback"

    @pytest.mark.asyncio
    async def test_unconfigured_secret_is_503(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(_req(app={}))
        assert resp.status == 503
        assert json.loads(resp.body)["error"] == "not available"

    @pytest.mark.asyncio
    async def test_wrong_secret_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_missing_secret_header_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_token_local(_req(app={"local_secret": "right"}))
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_issues_credential_with_requested_ttl_and_embed_claim(
        self, monkeypatch, fake_sel
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        minted: dict = {}

        def _generate(owner, ttl_seconds=0, extra=None):
            minted.update({"owner": owner, "ttl": ttl_seconds, "extra": extra})
            return "issued-value"

        monkeypatch.setattr(core_mod, "generate_token", _generate)
        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right", "state": SimpleNamespace(owner_id="owner-1")},
                headers={"X-Local-Secret": "right"},
                query={"ttl": "2h", "embed_parent_port": "5476"},
            )
        )
        assert resp.status == 200
        assert json.loads(resp.body)["expires_in"] == 7200
        assert minted["owner"] == "owner-1"
        assert minted["extra"] == {"embed_parent_port": "5476"}

    @pytest.mark.asyncio
    async def test_bad_embed_port_is_dropped(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        minted: dict = {}

        def _generate(owner, ttl_seconds=0, extra=None):
            minted["extra"] = extra
            return "issued-value"

        monkeypatch.setattr(core_mod, "generate_token", _generate)
        resp = await core_mod.api_token_local(
            _req(
                app={"local_secret": "right"},
                headers={"X-Local-Secret": "right"},
                query={"ttl": "not-a-duration", "embed_parent_port": "99999"},
            )
        )
        assert resp.status == 200
        assert minted["extra"] is None


class TestLogout:
    @pytest.mark.asyncio
    async def test_non_loopback_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: False)
        resp = await core_mod.api_logout(_req(remote="203.0.113.9"))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "loopback only"

    @pytest.mark.asyncio
    async def test_wrong_secret_is_refused(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "wrong"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_revocation_persist_failure_reports_a_coded_error(
        self, monkeypatch, fake_sel
    ) -> None:
        """Fail-closed: an unpersisted revocation must never report success."""
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)

        def _boom():
            raise OSError("read-only trust dir")

        monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions", _boom)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 500
        body = json.loads(resp.body)
        assert body["code"] == "revocation_persist_failed"
        assert "logout not completed" in body["error"]

    @pytest.mark.asyncio
    async def test_successful_revocation_is_audited(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_loopback", lambda _r: True)
        revoke = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.revoke_all_sessions", revoke)
        resp = await core_mod.api_logout(
            _req(app={"local_secret": "right"}, headers={"X-Local-Secret": "right"})
        )
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}
        revoke.assert_called_once()
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "success"


class TestAppSecretExchange:
    @pytest.fixture
    def app_sel(self, monkeypatch) -> MagicMock:
        recorder = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: recorder)
        return recorder

    @pytest.mark.asyncio
    async def test_missing_header_is_refused(self, app_sel) -> None:
        resp = await core_mod.api_app_token(_req(match_info={"name": "meetings"}))
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "missing X-App-Secret header"
        assert app_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_invalid_secret_is_refused(self, app_sel, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: False
        )
        resp = await core_mod.api_app_token(
            _req(match_info={"name": "meetings"}, headers={"X-App-Secret": "nope"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_valid_secret_mints_an_app_scoped_credential(
        self, app_sel, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: True
        )
        seen: dict = {}

        def _generate(name, app=None):
            seen.update({"name": name, "app": app})
            return "app-scoped-value"

        monkeypatch.setattr("kiro_crew.dashboard.token_auth.generate_token", _generate)
        resp = await core_mod.api_app_token(
            _req(match_info={"name": "meetings"}, headers={"X-App-Secret": "ok"})
        )
        assert resp.status == 200
        # The app identity must be IN the payload so downstream middleware can
        # extract a verified app name rather than trusting a header.
        assert seen == {"name": "meetings", "app": "meetings"}
        assert app_sel.log_api_access.call_args.kwargs["outcome"] == "granted"


# ── Session sub-agent routes ────────────────────────────────────────────


class TestSessionAgentRoutes:
    @pytest.mark.asyncio
    async def test_list_returns_workspace_results(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr(
            "kiro_crew.session_workspace.list_results",
            lambda _s: [{"agent_id": "a1", "bytes": 12}],
        )
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": "s1"}))
        assert json.loads(resp.body) == {"results": [{"agent_id": "a1", "bytes": 12}]}
        assert fake_sel.log_api_access.call_args.kwargs["resources"] == "s1"

    @pytest.mark.asyncio
    async def test_missing_result_is_404(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr("kiro_crew.session_workspace.read_result", lambda *_a: "")
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": "a1"})
        )
        assert resp.status == 404
        assert json.loads(resp.body)["error"] == "not found"

    @pytest.mark.asyncio
    async def test_result_is_returned_after_redaction(self, monkeypatch, fake_sel) -> None:
        monkeypatch.setattr(
            "kiro_crew.session_workspace.read_result", lambda *_a: "finished the audit"
        )
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": "a1"})
        )
        body = json.loads(resp.body)
        assert body == {"agent_id": "a1", "content": "finished the audit"}

    @pytest.mark.asyncio
    async def test_stream_emits_the_tail_then_a_done_event(
        self, monkeypatch, fake_sel, tmp_path
    ) -> None:
        result = tmp_path / "agent-a1.md"
        result.write_text("partial output\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr("kiro_crew.session_workspace.result_path", lambda *_a: result)

        app = web.Application()
        app["state"] = SimpleNamespace(
            subagents=SimpleNamespace(get=lambda _a: SimpleNamespace(done=True))
        )
        app.router.add_get(
            "/api/sessions/{id}/agents/{agent_id}/stream", core_mod.api_session_agent_stream
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions/s1/agents/a1/stream")
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            text = await resp.text()
        assert "partial output" in text
        assert "event: done" in text

    @pytest.mark.asyncio
    async def test_stream_stops_when_the_client_disconnects(
        self, monkeypatch, fake_sel
    ) -> None:
        """A reset peer must end the loop, not spin for the full 20 minutes."""

        def _reset(**_k):
            raise ConnectionResetError("peer went away")

        monkeypatch.setattr(
            "kiro_crew.session_workspace.result_path",
            lambda *_a: SimpleNamespace(exists=lambda: True, read_text=_reset),
        )
        app = web.Application()
        app["state"] = SimpleNamespace(subagents=None)
        app.router.add_get(
            "/api/sessions/{id}/agents/{agent_id}/stream", core_mod.api_session_agent_stream
        )
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/sessions/s1/agents/a1/stream")
            assert resp.status == 200
            assert await resp.text() == ""

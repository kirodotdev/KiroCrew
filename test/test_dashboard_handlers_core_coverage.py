"""Coverage for ``dashboard/handlers/core.py`` error branches and cold paths.

Targets the parts of the module the rest of the suite never reaches. This file holds
the page and static assets, the STT prerequisite probes and the STT config endpoint.
Its siblings hold the rest: ``test_dashboard_handlers_core_coverage_stt_runtime.py``
(STT status / prepare / prewarm / transcribe), ``..._security_endpoints.py`` (the SEL +
security read endpoints, the loopback-gated local endpoints, the app-secret exchange)
and ``..._agent_settings.py`` (the agent-settings PUT and PATCH validators, the session
sub-agent routes). Shared stubs live in ``dashboard_handlers_core_coverage_helpers.py``.

Style follows ``test_api_health.py`` (direct handler calls against a
``MagicMock(spec=web.Request)``) and ``test_config_patch.py`` (real aiohttp
``TestClient`` when the handler needs a genuine request body or streaming
response). Every write lands under the autouse-isolated ``KIROCREW_HOME``
from ``conftest.py``; nothing here touches the network, downloads a model, or
spawns a process.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_handlers_core_coverage_helpers import fake_sel_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import seeded_config_fixture  # noqa: F401
from dashboard_handlers_core_coverage_helpers import _json_req, _req, _seed_stt

from kiro_crew import extras
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.stt import models as stt_models
from kiro_crew.stt.limits import MAX_THREADS, MIN_PARTIAL_INTERVAL_MS, MIN_SILENCE_MS
from kiro_crew.stt.limits import THREADS_ENV as STT_THREADS_ENV

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
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_sensitive_path", lambda _p: True)
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
        monkeypatch.setattr("kiro_crew.dashboard.handlers.is_sensitive_path", lambda _p: False)
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
    async def test_favicon_route_resolves_to_logo_handler(self) -> None:
        """/favicon.ico must serve the logo, not fall through to the SPA
        fallback: clients that hardcode the path and never parse
        <link rel="icon"> otherwise receive text/html and show no icon."""
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.routes import realtime

        app = web.Application()
        realtime.register(app)
        for path in ("/logo.png", "/favicon.ico"):
            match = await app.router.resolve(make_mocked_request("GET", path))
            assert match.handler is core_mod.logo, path

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


class TestSttProviders:
    def test_apple_hidden_when_framework_unavailable(self, monkeypatch) -> None:
        """`apple` needs macOS 26 plus a Swift toolchain, so a host without them
        must not be offered a provider it cannot select. `local` has no
        precondition at all and stays advertised everywhere."""
        monkeypatch.setattr(
            "kiro_crew.apple_speech.availability",
            lambda: SimpleNamespace(ok=False),
        )
        providers = core_mod._stt_providers()
        assert "apple" not in providers
        assert "local" in providers

    def test_apple_appears_once_the_framework_answers_yes(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.apple_speech.availability",
            lambda: SimpleNamespace(ok=True),
        )
        assert "apple" in core_mod._stt_providers()

    def test_retired_providers_are_never_advertised(self, monkeypatch) -> None:
        """A persisted retired value degrades to `local` at load. Advertising one
        here would hand it straight back, and with it the out-of-band install
        (a whisper CLI on PATH, an mlx or faster-whisper wheel) that the resident
        local engine exists to remove."""
        monkeypatch.setattr(
            "kiro_crew.apple_speech.availability",
            lambda: SimpleNamespace(ok=True),
        )
        offered = set(core_mod._stt_providers())
        assert not offered & {"whisper", "mlx", "parakeet", "faster"}


class TestFfmpegInstallCommands:
    """Fallback guidance for source installs without the voice extra.

    The browser records WebM, so the batch upload path has to decode before it
    can recognise. The voice extra now supplies that codec; these platform
    commands remain only for source installs using another recogniser.
    """

    @pytest.fixture(autouse=True)
    def _no_ffmpeg_probe(self, monkeypatch):
        """Pin the PATH-augmenting probe: it stats the operator's real disks."""
        monkeypatch.setattr(core_mod, "ensure_ffmpeg_in_path", lambda: None)

    def test_present_ffmpeg_asks_for_nothing(self, monkeypatch) -> None:
        # Stubbed at `_find_ffmpeg`, which is the seam the production code now asks:
        # ffmpeg is resolved from fixed directories rather than from PATH, so a
        # `shutil.which` stub does not decide the answer (and, being a module-global
        # patch, the real resolver would receive it and reject its `path=` argument).
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: "/usr/local/bin/ffmpeg")
        assert core_mod._ffmpeg_install_commands() == []

    def test_darwin_uses_brew(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: None)
        assert core_mod._ffmpeg_install_commands() == ["brew install ffmpeg"]

    def test_windows_uses_winget(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: None)
        assert core_mod._ffmpeg_install_commands() == ["winget install --id Gyan.FFmpeg"]

    def test_debian_uses_apt_get(self, monkeypatch) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(
            core_mod.shutil,
            "which",
            lambda n, path=None: "/usr/bin/apt-get" if n == "apt-get" else None,
        )
        assert core_mod._ffmpeg_install_commands() == ["sudo apt-get install -y ffmpeg"]

    def test_a_checkout_with_the_build_script_builds_from_source(
        self, monkeypatch, tmp_path
    ) -> None:
        """Amazon Linux ships no ffmpeg in its repos, so the only honest answer
        there is the toolchain plus a source build. Offered only when the script
        is actually present, since naming a path that does not exist is worse
        than pointing at upstream."""
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n, path=None: None)
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "build-ffmpeg.sh").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        cmds = core_mod._ffmpeg_install_commands()
        assert any("dnf install -y gcc make nasm diffutils" in c for c in cmds)
        assert any("build-ffmpeg.sh" in c for c in cmds)

    def test_without_a_build_script_there_is_nothing_to_tell_a_terminal(self, monkeypatch) -> None:
        """No fallback command, because the fallback was a dead end.

        A distribution with no ffmpeg package and no build script in reach would
        be handed ``echo 'Build ffmpeg from source: …'``, which a user pasted into a
        terminal and got a URL echoed back. An empty list is what makes the Settings
        page offer the decoder fetch, or the agent hand-off, instead.
        """
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(core_mod, "_find_ffmpeg", lambda: None)
        monkeypatch.setattr(core_mod.shutil, "which", lambda _n, path=None: None)
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        cmds = core_mod._ffmpeg_install_commands()
        assert cmds == []
        assert not any("echo" in c for c in cmds)


class TestSttPrereqCommands:
    """What the user still has to run themselves, per provider.

    There is no install button behind this list any more: the only thing a
    provider can need beyond an installed Kiro Crew is the optional ``voice``
    extra in this interpreter, which includes the decoder. An empty list is the
    steady state.

    The pip command's shell quoting is `_shared.pip_extra_install_command`'s, not
    this module's; these assertions cover the prereq LIST's contract end to end,
    which is what a Settings reader actually receives. `sys` is patched directly
    rather than through `core_mod` because the command is built where that helper
    lives, and both modules share the one `sys` object anyway.
    """

    @pytest.fixture(autouse=True)
    def _no_ffmpeg_tail(self, monkeypatch):
        """Neutralise the ffmpeg tail so each test measures the extra alone.

        The platform branches it appends have their own class; folding them in
        here would make every assertion below depend on the host's PATH.
        """
        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", lambda: [])
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)

    @staticmethod
    def _local_availability(monkeypatch, *, ok: bool, code: str = "") -> None:
        """Pin the recogniser probe, which imports an optional native extension.

        Left real it would answer differently on a host with the extra installed
        than on one without, which is the whole verdict under test.
        """
        monkeypatch.setattr(
            "kiro_crew.stt.availability",
            lambda: core_mod.stt.Availability(ok, code, ""),
        )

    def test_local_needs_the_voice_extra_when_the_recogniser_is_absent(self, monkeypatch) -> None:
        """The recogniser is an optional extra imported IN THIS PROCESS, so the
        command must name the gateway's own interpreter: a system python or a
        --user install elsewhere would not be importable here."""
        self._local_availability(monkeypatch, ok=False, code=core_mod.stt.CODE_EXTRA_MISSING)
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod.os, "name", "posix")
        cmds = core_mod._stt_prereq_commands("local")
        assert len(cmds) == 1
        # The extra's own distributions, never `kirocrew[voice]`: this project is
        # published on no index, so the extras form cannot resolve for anyone.
        assert "kirocrew[" not in cmds[0]
        for req in extras.requirements_for_extra("voice"):
            assert f"'{req}'" in cmds[0]
        assert "-m pip install" in cmds[0]
        assert core_mod.shlex.quote(sys.executable) in cmds[0]

    def test_local_with_the_recogniser_present_has_no_prereqs(self, monkeypatch) -> None:
        self._local_availability(monkeypatch, ok=True)
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        assert core_mod._stt_prereq_commands("local") == []

    def test_a_platform_without_a_wheel_is_not_offered_a_pip_command(self, monkeypatch) -> None:
        """Only the missing-extra case is actionable by pip. Intel macOS has no
        prebuilt wheel, so `pip install` there starts a source build that needs a
        C++ toolchain, which the availability `detail` says; repeating the pip
        command would send the user round the same failure."""
        self._local_availability(monkeypatch, ok=False, code=core_mod.stt.CODE_NO_WHEEL)
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        assert core_mod._stt_prereq_commands("local") == []

    def test_apple_has_nothing_to_install(self, monkeypatch) -> None:
        """The on-device recogniser is part of the OS and compiles its own helper
        on demand; the fixture pins the optional source-decoder fallback."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        assert core_mod._stt_prereq_commands("apple") == []

    def test_transcribe_prereq_targets_the_gateway_interpreter(self, monkeypatch) -> None:
        """Transcribe's requirement is the `voice` extra importable by THIS
        process, so the command must name the gateway's own interpreter — a
        system python or --user install would not be importable here."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.os, "name", "posix")
        cmds = core_mod._stt_prereq_commands("transcribe")
        assert len(cmds) == 1
        # The CLOUD half alone. An extra resolves atomically, so naming the full
        # `voice` set here would drag in the local recogniser and fail the whole
        # install on a platform that has no wheel for it.
        assert "kirocrew[" not in cmds[0]
        assert "pywhispercpp" not in cmds[0]
        for req in extras.requirements_for_extra("voice-aws"):
            assert f"'{req}'" in cmds[0]
        assert "-m pip install" in cmds[0]
        assert core_mod.shlex.quote(sys.executable) in cmds[0]

    def test_transcribe_prereq_windows_is_powershell_literal_quoted(self, monkeypatch) -> None:
        """The user's shell is unknowable on Windows (the command may be pasted
        into PowerShell OR cmd), so the emitted form must be free of SILENT
        corruption in both. PowerShell is the harder shell: a double-quoted
        string AND a bare unquoted token both expand ``$names`` and honour
        backtick escapes — legal path characters — silently rewriting the
        interpreter path. Single quotes are PowerShell's literal form (spaces
        included, so the all-users ``C:\\Program Files`` layout works), and cmd
        rejects the leading ``&`` loudly rather than corrupting anything."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.os, "name", "nt")
        monkeypatch.setattr(sys, "executable", "C:\\Program Files\\Python312\\python.exe")
        cmds = core_mod._stt_prereq_commands("transcribe")
        # Quoting is the shared helper's, not this module's: on Windows a
        # specifier needs the cross-shell DOUBLE-quoted form, because cmd does
        # not treat `'` as a quote and would redirect on a pin's `<`.
        specs = " ".join(extras.quote_spec(r) for r in extras.requirements_for_extra("voice-aws"))
        assert cmds == [f"& 'C:\\Program Files\\Python312\\python.exe' -m pip install {specs}"]
        # Named properties the exact match locks in. The double-quote check is
        # scoped to the INTERPRETER segment: a PS double-quoted path would
        # expand `$name` and honour backticks, silently rewriting it. The
        # SPECIFIER after `-m pip install` is deliberately double-quoted (the
        # one form cmd and PowerShell both strip) and holds no `$` or backtick
        # to expand -- test_extras.py pins that precondition.
        interpreter = cmds[0].split(" -m pip install ", 1)[0]
        assert '"' not in interpreter
        assert "`" not in cmds[0]  # never emit a PS escape character

    def test_transcribe_prereq_windows_metachar_paths_survive_literally(self, monkeypatch) -> None:
        """``$`` and a literal single quote are legal Windows path characters.
        Inside PowerShell single quotes ``$python`` is NOT expanded, and a
        quote in the path is escaped by doubling — PowerShell's own rule — so
        the interpreter reaches pip byte-for-byte."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod.os, "name", "nt")
        monkeypatch.setattr(sys, "executable", "C:\\tools\\$python\\o'brien.exe")
        cmds = core_mod._stt_prereq_commands("transcribe")
        # Quoting is the shared helper's, not this module's: on Windows a
        # specifier needs the cross-shell DOUBLE-quoted form, because cmd does
        # not treat `'` as a quote and would redirect on a pin's `<`.
        specs = " ".join(extras.quote_spec(r) for r in extras.requirements_for_extra("voice-aws"))
        assert cmds == [f"& 'C:\\tools\\$python\\o''brien.exe' -m pip install {specs}"]

    def test_transcribe_prereq_without_install_channel_is_empty(self, monkeypatch) -> None:
        """When no install channel can make the extra importable (bundled
        interpreter, pip-less interpreter, PEP 668), emitting a pip command would
        recreate the press-and-nothing-changes dead end — the UI shows the
        unsupported notice via `transcribe_unsupported` instead."""
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: False)
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: False)
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_transcribe_prereq_self_suppresses_when_extra_present(self, monkeypatch) -> None:
        """Like every other branch of this function, the pip command must not
        be shown once the requirement is met (e.g. STT merely disabled)."""
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: True)
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_source_install_names_the_extra_and_system_decoder_when_both_are_missing(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: False)
        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", lambda: ["brew install ffmpeg"])
        monkeypatch.setattr(core_mod.os, "name", "posix")
        cmds = core_mod._stt_prereq_commands("transcribe")
        assert len(cmds) == 2
        assert "-m pip install" in cmds[0]
        assert "amazon-transcribe" in cmds[0]
        assert cmds[1] == "brew install ffmpeg"

    def test_bundled_desktop_never_offers_a_system_decoder_command(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: True)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            core_mod.stt_decoder,
            "platform_key",
            lambda *a, **k: core_mod.stt_decoder.ARTIFACTS[0].platform_key,
        )

        def _unexpected_system_install():
            raise AssertionError("desktop tried to make the user install ffmpeg")

        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", _unexpected_system_install)
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_a_release_with_no_bundled_decoder_offers_the_system_command(self, monkeypatch) -> None:
        """The macOS Intel release carries no decoder, so a system one is its only route."""
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: True)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            core_mod.stt_decoder,
            "platform_key",
            lambda *a, **k: core_mod.stt_decoder.DECODERLESS_BUNDLE_PLATFORM,
        )
        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", lambda: ["brew install ffmpeg"])
        assert core_mod._stt_prereq_commands("transcribe") == ["brew install ffmpeg"]

    def test_source_install_can_still_offer_the_system_decoder_fallback(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: True)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", lambda: ["brew install ffmpeg"])
        assert core_mod._stt_prereq_commands("transcribe") == ["brew install ffmpeg"]


class TestPipInstallChannel:
    @pytest.fixture(autouse=True)
    def _not_bundled(self, monkeypatch):
        """Pin the desktop-bundle and pip-module probes so this class describes
        the environment it claims to test rather than inheriting the host's —
        a uv-created venv (the default for `uv venv`) ships no `pip` module, so
        an unpinned `find_spec("pip")` returns None on this host and every test
        below that isn't otherwise exercising that branch would misfire."""
        monkeypatch.setattr(extras.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(
            extras.importlib.util,
            "find_spec",
            lambda name, *a, **kw: object() if name == "pip" else None,
        )

    def test_bundled_desktop_interpreter_has_no_channel(self, monkeypatch) -> None:
        """A pip install into the desktop app's code-signed bundle breaks
        launches/updates and is discarded on every app update — the command
        must not be offered there even though pip itself may exist."""
        monkeypatch.setattr(extras.platform_compat, "is_bundled_interpreter", lambda: True)
        assert core_mod._pip_install_channel_available() is False

    def test_pipless_interpreter_has_no_channel(self, monkeypatch) -> None:
        """uv tool installs and some pipx layouts ship no `pip` module, so
        `<python> -m pip` fails immediately — the command must not be shown."""
        monkeypatch.setattr(
            extras.importlib.util,
            "find_spec",
            lambda name, *a, **kw: None,
        )
        assert core_mod._pip_install_channel_available() is False

    def test_externally_managed_python_has_no_channel(self, monkeypatch, tmp_path) -> None:
        """PEP 668: pip refuses installs into an externally-managed
        interpreter (distro/brew pythons) — but only outside a venv."""
        monkeypatch.setattr(extras.sys, "prefix", extras.sys.base_prefix)
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(extras.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is False

    def test_venv_on_managed_base_has_a_channel(self, monkeypatch, tmp_path) -> None:
        """Inside a venv pip ignores PEP 668, and `sysconfig.get_path("stdlib")`
        resolves to the BASE interpreter's directory where distro pythons put
        the marker — the recommended install layout (venv on a Debian/brew
        python) must not be misread as unsupported."""
        monkeypatch.setattr(extras.importlib.util, "find_spec", lambda name: object())
        monkeypatch.setattr(extras.sys, "prefix", str(tmp_path / "venv"))
        monkeypatch.setattr(extras.sys, "base_prefix", str(tmp_path / "base"))
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(extras.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True

    def test_ordinary_venv_has_a_channel(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(extras.importlib.util, "find_spec", lambda name: object())
        monkeypatch.setattr(extras.sys, "prefix", extras.sys.base_prefix)
        monkeypatch.setattr(extras.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True


# ── STT config endpoint ─────────────────────────────────────────────────


def _stt_app() -> web.Application:
    app = web.Application()
    # The GET resolves whether the caller is the owner, which reads
    # ``app["state"].owner_id``. A TestServer request carries no auth claim, so it
    # is never the owner -- the state only has to exist.
    app["state"] = SimpleNamespace(owner_id="owner-1")
    app.router.add_route("*", "/api/config/stt", core_mod.api_stt_config)
    return app


class TestSttConfigEndpoint:
    @pytest.fixture(autouse=True)
    def _quiet_probes(self, monkeypatch):
        monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        # api_stt_config derives BOTH `available` and `code` from availability_detail
        # (the same probe api_stt_status uses), so the two fields cannot disagree.
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: core_mod.stt.Availability(False),
        )

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
        """The allowlist is the contract, and the retired keys are part of it.

        ``whisper_path``, ``mlx_model``, ``parakeet_model`` and ``device`` belonged
        to the out-of-band runtimes that are gone. Persisting one would leave a key
        in ``config.json`` that nothing reads, which reads to the next person as a
        setting that stopped working rather than one that was withdrawn.
        """
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put(
                "/api/config/stt",
                json={
                    "enabled": True,
                    "provider": "local",
                    "model": "small",
                    "transcribe_region": "us-west-2",
                    "transcribe_profile": "default",
                    "language_code": "en-US",
                    "streaming": True,
                    "endpointing": False,
                    "dictation_panel": True,
                    "whisper_path": "/usr/local/bin/whisper",
                    "mlx_model": "mlx-community/whisper-large-v3-turbo",
                    "parakeet_model": "mlx-community/parakeet-tdt-0.6b-v2",
                    "device": "cuda",
                    "provider_bogus": "ignored",
                },
            )
            assert resp.status == 200
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["enabled"] is True
        assert stt["provider"] == "local"
        assert stt["model"] == "small"
        assert stt["transcribe_region"] == "us-west-2"
        assert stt["language_code"] == "en-US"
        assert stt["streaming"] is True
        assert stt["endpointing"] is False
        assert stt["dictation_panel"] is True
        for retired in ("whisper_path", "mlx_model", "parakeet_model", "device", "provider_bogus"):
            assert retired not in stt
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
    async def test_put_refuses_a_retired_provider_name(self, seeded_config) -> None:
        """A retired name reaching the file would be read back and degraded to
        `local` with a warning on every load, so the picker would show a selection
        that is silently not the one in force.

        The starting value is a DIFFERENT selectable provider on purpose: had the
        endpoint accepted `mlx`, the loader would degrade it to `local`, which is
        indistinguishable from a refusal if the test starts from the default.
        """
        async with TestClient(TestServer(_stt_app())) as client:
            seeded = await client.put("/api/config/stt", json={"provider": "transcribe"})
            assert seeded.status == 200
            resp = await client.put("/api/config/stt", json={"provider": "mlx"})
            assert resp.status == 200
            assert (await resp.json())["provider"] == "transcribe"
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["provider"] == "transcribe"

    @pytest.mark.asyncio
    async def test_put_accepts_only_a_catalog_model_name(self, seeded_config) -> None:
        """The allowlist is the download catalog, so a superseded alias is refused
        even though the loader's resolver still understands it: the picker offers
        catalog names, and a name outside the catalog is one whose size and digest
        this endpoint cannot state."""
        assert "turbo" not in core_mod._STT_MODEL_SIZES
        async with TestClient(TestServer(_stt_app())) as client:
            assert (await client.put("/api/config/stt", json={"model": "small"})).status == 200
            # An ALIAS is accepted and canonicalised. It has to be: a catalog cull
            # turns a retired name into an alias, and refusing those meant someone
            # whose stored model was retired could not save this panel at all --
            # a field they never edited was rejected on every write.
            aliased = await client.put("/api/config/stt", json={"model": "turbo"})
            assert (await aliased.json())["model"] == "large-v3-turbo"
            # A name that resolves to NOTHING leaves the stored value alone. The
            # distinction matters: answering the default here would let one junk
            # request replace a model the user deliberately chose.
            refused = await client.put("/api/config/stt", json={"model": "no-such-model"})
            assert refused.status == 200
            assert (await refused.json())["model"] == "large-v3-turbo"
            accepted = await client.put("/api/config/stt", json={"model": "tiny"})
            assert (await accepted.json())["model"] == "tiny"
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["model"] == "tiny"

    @pytest.mark.asyncio
    async def test_put_round_trips_the_cleanup_consent(self, seeded_config) -> None:
        """The whole feature hangs off this round trip, and it was broken.

        `polish` sends the finished transcript to a model, so it is the one CONSENT
        setting on this surface. The PUT branch never read it and the GET response
        never returned it, so the toggle wrote nothing and a reload read the default
        back -- and because `api_stt_polish` refuses while the flag is False, the
        endpoint, the hook and the panel were each correct while the feature was
        dead. Nothing in the UI said so, which is why this asserts the value on
        DISK rather than only the response.
        """
        async with TestClient(TestServer(_stt_app())) as client:
            assert (await (await client.get("/api/config/stt")).json())["polish"] is False
            enabled = await client.put("/api/config/stt", json={"polish": True})
            assert enabled.status == 200
            assert (await enabled.json())["polish"] is True
            assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["polish"] is True
            # And back off again -- a consent setting that cannot be withdrawn is
            # worse than one that cannot be given.
            disabled = await client.put("/api/config/stt", json={"polish": False})
            assert (await disabled.json())["polish"] is False
            assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["polish"] is False
            # A non-bool is ignored rather than coerced: "on" must not read as consent.
            await client.put("/api/config/stt", json={"polish": "yes"})
            assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["polish"] is False

    @pytest.mark.asyncio
    async def test_put_persists_the_millisecond_knobs_at_their_floors(self, seeded_config) -> None:
        """Each floor is owned by the module that enforces it at runtime, and a
        value AT the floor has to survive: it is the whole point of publishing the
        floor. Zero is legal for the idle window and means "release the model as
        soon as it goes idle", which is the right trade on a small machine."""
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put(
                "/api/config/stt",
                json={
                    "silence_ms": MIN_SILENCE_MS,
                    "partial_interval_ms": MIN_PARTIAL_INTERVAL_MS,
                    "idle_evict_secs": 0,
                },
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["silence_ms"] == MIN_SILENCE_MS
        assert body["partial_interval_ms"] == MIN_PARTIAL_INTERVAL_MS
        assert body["idle_evict_secs"] == 0
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["silence_ms"] == MIN_SILENCE_MS
        assert stt["partial_interval_ms"] == MIN_PARTIAL_INTERVAL_MS
        assert stt["idle_evict_secs"] == 0

    @pytest.mark.asyncio
    async def test_put_ignores_values_below_the_floors(self, seeded_config) -> None:
        """A silence window under the detector's floor lets the pause between two
        words end the utterance, so the phrase commits mid-sentence. A negative
        cadence or idle window is not a setting at all."""
        async with TestClient(TestServer(_stt_app())) as client:
            seeded = await client.put(
                "/api/config/stt",
                json={"silence_ms": 900, "partial_interval_ms": 250, "idle_evict_secs": 30},
            )
            assert (await seeded.json())["silence_ms"] == 900
            resp = await client.put(
                "/api/config/stt",
                json={
                    "silence_ms": MIN_SILENCE_MS - 1,
                    "partial_interval_ms": -1,
                    "idle_evict_secs": -1,
                },
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["silence_ms"] == 900
        assert body["partial_interval_ms"] == 250
        assert body["idle_evict_secs"] == 30

    @pytest.mark.asyncio
    async def test_the_served_partial_cadence_is_never_below_the_readable_floor(
        self, seeded_config
    ) -> None:
        """Below the floor the text churns faster than it can be read, so whatever
        a client asks for, the cadence the panel is told to expect has to be one a
        human can follow."""
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.put("/api/config/stt", json={"partial_interval_ms": 1})
            assert resp.status == 200
            assert (await resp.json())["partial_interval_ms"] >= MIN_PARTIAL_INTERVAL_MS

    @pytest.mark.asyncio
    async def test_put_refuses_a_boolean_numeric_value(self, seeded_config) -> None:
        """``bool`` subclasses ``int`` in Python, so a checkbox value sent to one of
        these fields would otherwise persist as ``1``.

        Asserted on ``idle_evict_secs`` because that is where it is observable: its
        floor is zero, so ``True`` passes the range check on its own and only the
        explicit type guard stands between a JSON ``true`` and a one-second idle
        window. On ``silence_ms`` the 200 ms floor would hide the bug.
        """
        async with TestClient(TestServer(_stt_app())) as client:
            assert (await client.put("/api/config/stt", json={"idle_evict_secs": 30})).status == 200
            resp = await client.put("/api/config/stt", json={"idle_evict_secs": True})
            assert resp.status == 200
            assert (await resp.json())["idle_evict_secs"] == 30
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["idle_evict_secs"] == 30
        assert not isinstance(stt["idle_evict_secs"], bool)

    @pytest.mark.asyncio
    async def test_get_advertises_capabilities(self, seeded_config, monkeypatch) -> None:
        # The unsupported flag folds in the venv's own packaging: a uv-created venv
        # ships no `pip` module, so the channel probe reads False there and the
        # flag flips True on every uv host. Pin the probe, as the sibling tests do.
        monkeypatch.setattr(core_mod, "_pip_install_channel_available", lambda: True)
        async with TestClient(TestServer(_stt_app())) as client:
            resp = await client.get("/api/config/stt")
            assert resp.status == 200
            body = await resp.json()
        # Streaming capability is served from the backend's own set so the
        # Settings UI gates on a CAPABILITY rather than a provider name. `local`
        # streams too, so every advertised provider is in it.
        assert body["streaming_providers"] == ["local", "apple", "transcribe"]
        # Tracks _STT_MODEL_SIZES (the PUT allowlist) rather than pinning one
        # literal, so a catalog entry added or resized cannot make the picker
        # offer a value this endpoint would reject.
        assert body["models"] == core_mod._STT_MODEL_SIZES
        # Sizes are BYTES, not a formatted label: the dashboard is translated
        # into 12 languages, so only the frontend can format them for a reader.
        assert body["models"][stt_models.DEFAULT_MODEL] > 0
        assert body["language_codes"][0] == "auto"
        assert "en-US" in body["language_codes"]
        assert body["available"] is False
        assert body["prereqs"] == []
        # The pip channel is pinned open above, so the unsupported flag must be
        # False regardless of installed extras.
        assert body["transcribe_unsupported"] is False
        # Cause discriminator for the unsupported notice: the desktop bundle
        # needs different guidance than a pip-less/PEP 668 interpreter. A test
        # venv is never the bundled app.
        assert body["bundled_interpreter"] is False
        # Served independently of `available` so the UI can flag the .webm
        # decode gap even when the provider reads ready.
        assert isinstance(body["ffmpeg_missing"], bool)
        # The availability code rides this same query so the chat modal can render
        # the precise per-code reason without a second request. Empty here because
        # the fixture's detail is `Availability(False)` with the default CODE_OK.
        assert body["code"] == ""

    @pytest.mark.asyncio
    async def test_get_carries_the_availability_code_from_the_same_detail(
        self, seeded_config, monkeypatch
    ) -> None:
        """The chat modal keys off a machine-readable code to explain WHY voice is
        unavailable, and it reads that code from this one query the composer
        already makes. `available` and `code` are derived from the SAME detail so
        a "not available" answer can never carry an empty/OK code."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: core_mod.stt.Availability(
                False, core_mod.stt.CODE_EXTRA_MISSING, "needs the voice extra"
            ),
        )
        async with TestClient(TestServer(_stt_app())) as client:
            body = await (await client.get("/api/config/stt")).json()
        assert body["available"] is False
        assert body["code"] == core_mod.stt.CODE_EXTRA_MISSING
        assert body["code"] == "stt_extra_missing"

    @pytest.mark.asyncio
    async def test_get_carries_an_empty_code_when_available(
        self, seeded_config, monkeypatch
    ) -> None:
        """When recognition can run the code is the empty string (CODE_OK), and
        `available` is True: the two fields are read from one detail."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: core_mod.stt.Availability(True),
        )
        async with TestClient(TestServer(_stt_app())) as client:
            body = await (await client.get("/api/config/stt")).json()
        assert body["available"] is True
        assert body["code"] == ""

    @pytest.mark.asyncio
    async def test_get_defaults_to_the_local_provider_with_streaming_on(
        self, seeded_config
    ) -> None:
        """The default is in-process recognition with words appearing while the
        user is still speaking: `local` needs no account and no separate install,
        so there is nothing to configure before dictation works, and a dictation
        surface that only updates after a pause reads as lag."""
        async with TestClient(TestServer(_stt_app())) as client:
            body = await (await client.get("/api/config/stt")).json()
        assert body["provider"] == "local"
        assert body["streaming"] is True
        assert body["enabled"] is True
        assert body["model"] == stt_models.DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_get_serves_the_millisecond_knobs_the_panel_renders(self, seeded_config) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            body = await (await client.get("/api/config/stt")).json()
        assert body["silence_ms"] >= MIN_SILENCE_MS
        assert isinstance(body["partial_interval_ms"], int)
        assert isinstance(body["idle_evict_secs"], int)

    @pytest.mark.asyncio
    async def test_threads_round_trip_and_auto_spelling(self, seeded_config, monkeypatch) -> None:
        """A count is stored as an int, ``"auto"`` is stored as 0, and the response
        reads back exactly what is on disk."""
        monkeypatch.setattr(core_mod, "affinity_cpu_count", lambda: 128)
        async with TestClient(TestServer(_stt_app())) as client:
            assert (await (await client.get("/api/config/stt")).json())["threads"] == 0
            resp = await client.put("/api/config/stt", json={"threads": 64})
            assert resp.status == 200
            assert (await resp.json())["threads"] == 64
            assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["threads"] == 64
            resp = await client.put("/api/config/stt", json={"threads": "auto"})
            assert (await resp.json())["threads"] == 0
            assert json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]["threads"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", [-1, MAX_THREADS + 1, 1.5, True, "lots", [8]])
    async def test_threads_put_skips_junk(self, seeded_config, junk) -> None:
        async with TestClient(TestServer(_stt_app())) as client:
            assert (await client.put("/api/config/stt", json={"threads": 24})).status == 200
            resp = await client.put("/api/config/stt", json={"threads": junk})
            assert resp.status == 200
            assert (await resp.json())["threads"] == 24

    @pytest.mark.asyncio
    async def test_thread_options_fit_the_host_and_keep_the_stored_value(
        self, seeded_config, monkeypatch
    ) -> None:
        """Auto first, then counts this host can run ending at its core count. A
        stored count above that (a config from a bigger machine) stays listed, so
        the picker shows it instead of reading as a different value."""
        monkeypatch.setattr(core_mod, "affinity_cpu_count", lambda: 20)
        _seed_stt(seeded_config, threads=64)
        async with TestClient(TestServer(_stt_app())) as client:
            body = await (await client.get("/api/config/stt")).json()
        assert body["thread_options"] == [0, 1, 2, 4, 8, 12, 16, 20, 64]

    @pytest.mark.asyncio
    async def test_threads_auto_is_the_count_auto_resolves_to(
        self, seeded_config, monkeypatch
    ) -> None:
        """Half the host's cores, capped at the ceiling, so the picker can label
        Auto with the count it stands for."""
        _seed_stt(seeded_config, threads=0)
        async with TestClient(TestServer(_stt_app())) as client:
            for cpus, shown in ((20, 10), (128, 16), (1, 1)):
                monkeypatch.setattr(core_mod, "affinity_cpu_count", lambda c=cpus: c)
                body = await (await client.get("/api/config/stt")).json()
                assert body["threads_auto"] == shown, cpus

    @pytest.mark.asyncio
    async def test_threads_env_is_reported(self, seeded_config, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "affinity_cpu_count", lambda: 20)
        async with TestClient(TestServer(_stt_app())) as client:
            monkeypatch.delenv(STT_THREADS_ENV, raising=False)
            assert (await (await client.get("/api/config/stt")).json())["threads_env"] is None
            # Reported as the count the engine runs, not as typed: padding and
            # leading zeros drop, auto resolves to half the cores, and a count
            # above the host's cores is clamped.
            for raw, shown in ((" 0012 ", "12"), ("auto", "10"), ("96", "20")):
                monkeypatch.setenv(STT_THREADS_ENV, raw)
                body = await (await client.get("/api/config/stt")).json()
                assert body["threads_env"] == shown, raw
            # An unusable value is ignored by the engine, so it is not reported as
            # an override either.
            monkeypatch.setenv(STT_THREADS_ENV, "lots")
            assert (await (await client.get("/api/config/stt")).json())["threads_env"] is None

    def test_threads_env_does_not_import_the_engine(self, monkeypatch) -> None:
        """The override count resolves without pulling in ``stt.engine``.

        ``_stt_threads_env`` runs synchronously while the GET ``/api/config/stt``
        response dict is built, so an import on its path would land on the event
        loop. ``stt.engine`` imports numpy at module scope and costs a cold
        multi-hundred-millisecond import the first time. Resolving through the
        leaf ``stt.limits`` and ``cpu_affinity`` modules instead keeps the engine
        off that path entirely: with the module evicted, calling the function must
        not load it back.
        """
        monkeypatch.setattr(core_mod, "affinity_cpu_count", lambda: 20)
        monkeypatch.setenv(STT_THREADS_ENV, "64")
        import kiro_crew.stt as stt_pkg

        # Evict the module AND the package attribute bound to it. A regression
        # that re-imports the engine then binds a fresh module to both, and
        # teardown restores both, so later tests patch the same engine object the
        # code under test reads.
        monkeypatch.delattr(stt_pkg, "engine", raising=False)
        for name in list(sys.modules):
            if name == "kiro_crew.stt.engine" or name.startswith("kiro_crew.stt.engine."):
                monkeypatch.delitem(sys.modules, name)
        assert core_mod._stt_threads_env() == "20"
        assert "kiro_crew.stt.engine" not in sys.modules


class TestSttConfigCustomModelBoundary:
    """``PUT /api/config/stt``: only the OWNER may configure a custom model.

    ``stt.status`` / ``stt.prepare`` / ``stt.prewarm`` are denied to an app token
    outright because they fetch weights and load them into the gateway. The
    custom-model pair arrives at the same outcome by a longer road: it names a file
    the gateway will verify against a digest THE SAME CALLER supplied, and load —
    and the transcription surfaces that trigger the load are deliberately
    app-reachable. So a caller able to write these three fields chooses the
    weights every later utterance runs through.

    "Not an app token" is the wrong bar, and pinning that is the point of the
    non-owner cases below: an allow-listed messaging user who runs ``!dashboard``
    is minted a dashboard session whose app claim is ``""`` exactly like the
    operator's. The gate is ``is_owner_dashboard_request``.

    The refusal is per-FIELD, not the whole endpoint: any caller may legitimately
    hold ``config.stt`` for the language and the toggles. Both halves of that are
    pinned here, because a gate that also blocked the ordinary fields would
    silently break shipped apps and a second dashboard session alike.
    """

    _PATH = "ggml-my-model.bin"
    _DIGEST = "a" * 64
    _OWNER = "owner-1"
    _OTHER_DASHBOARD = "messaging-user"

    @pytest.fixture(autouse=True)
    def _model_file(self):
        """A real file in the models directory, so the owner's save passes the
        path check and every refusal below is about the CALLER, not the file."""
        stt_models.models_dir().mkdir(parents=True, exist_ok=True)
        (stt_models.models_dir() / self._PATH).write_bytes(b"weights")

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: core_mod.stt.Availability(False)
        )

    @classmethod
    def _identity(cls, *, app_token: str | None = "", user: str | None = None) -> dict:
        """The request kwargs for ONE caller identity, defaulting to the owner.

        ``is_owner_dashboard_request`` compares the caller subject against
        ``request.app["state"].owner_id``, so the state carries a configured owner
        and every case says who is calling: the owner, another dashboard session
        (same empty app claim, different subject), or an app token.
        """
        return {
            "app": {"state": SimpleNamespace(owner_id=cls._OWNER)},
            "user": cls._OWNER if user is None else user,
            "app_token": app_token,
        }

    @classmethod
    def _put(cls, body: object, **identity) -> web.Request:
        """A ``PUT`` carrying *body* and one explicit caller identity.

        The handler is called directly rather than through ``TestClient``: the app
        claim and the caller subject are published by auth middleware that a bare
        test server does not run, and they are the whole input under test here.
        """
        req = _json_req(body, **cls._identity(**identity))
        req.method = "PUT"
        return req

    @classmethod
    def _get(cls, **identity) -> web.Request:
        req = _req(**cls._identity(**identity))
        req.method = "GET"
        return req

    @staticmethod
    def _stored(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")).get("stt", {})

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"model": stt_models.CUSTOM_MODEL},
            {"custom_model_path": _PATH},
            {"custom_model_sha256": _DIGEST},
            {
                "model": stt_models.CUSTOM_MODEL,
                "custom_model_path": _PATH,
                "custom_model_sha256": _DIGEST,
            },
        ],
    )
    async def test_an_app_token_cannot_configure_a_custom_model(
        self, seeded_config, fake_sel, body: dict
    ) -> None:
        resp = await core_mod.api_stt_config(self._put(body, app_token="meetings"))

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "owner_only"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"
        # Refused BEFORE the write, so nothing from the body reached the file.
        assert self._stored(seeded_config) == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"model": stt_models.CUSTOM_MODEL},
            {"custom_model_path": _PATH},
            {"custom_model_sha256": _DIGEST},
            {
                "model": stt_models.CUSTOM_MODEL,
                "custom_model_path": _PATH,
                "custom_model_sha256": _DIGEST,
            },
        ],
    )
    async def test_a_non_owner_dashboard_token_cannot_configure_a_custom_model(
        self, seeded_config, fake_sel, body: dict
    ) -> None:
        """The case an "is it an app token?" check lets through.

        An allow-listed messaging user who runs ``!dashboard`` receives a dashboard
        token: app claim ``""``, subject their own, not the owner's. They may not
        choose the gateway's weights, so the empty app claim alone must not clear
        this gate.
        """
        resp = await core_mod.api_stt_config(self._put(body, user=self._OTHER_DASHBOARD))

        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "owner_only"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"
        assert self._stored(seeded_config) == {}

    @pytest.mark.asyncio
    async def test_a_request_with_no_app_claim_at_all_is_refused(
        self, seeded_config, fake_sel
    ) -> None:
        """Fail closed: an absent claim means no auth middleware published one, so
        it is refused alongside a non-empty one rather than read as the owner."""
        resp = await core_mod.api_stt_config(
            self._put({"custom_model_path": self._PATH}, app_token=None)
        )
        assert resp.status == 403
        assert self._stored(seeded_config) == {}

    @pytest.mark.asyncio
    async def test_an_app_token_still_writes_the_ordinary_stt_fields(
        self, seeded_config, fake_sel
    ) -> None:
        """The other half of the contract. This endpoint is app-reachable, and only
        the custom-model fields are withheld."""
        resp = await core_mod.api_stt_config(
            self._put({"language_code": "fr-FR", "endpointing": False}, app_token="meetings")
        )

        assert resp.status == 200
        stored = self._stored(seeded_config)
        assert stored["language_code"] == "fr-FR"
        assert stored["endpointing"] is False

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_token_still_writes_the_ordinary_stt_fields(
        self, seeded_config, fake_sel
    ) -> None:
        """Narrowing the custom-model pair to the owner must not narrow the rest:
        a second dashboard session keeps its language and toggles."""
        resp = await core_mod.api_stt_config(
            self._put({"language_code": "de-DE", "streaming": False}, user=self._OTHER_DASHBOARD)
        )

        assert resp.status == 200
        stored = self._stored(seeded_config)
        assert stored["language_code"] == "de-DE"
        assert stored["streaming"] is False

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_write_back_a_custom_selection_already_in_force(
        self, seeded_config, fake_sel
    ) -> None:
        """Refused on PRESENCE, not on whether it changes anything.

        An earlier version allowed this as a harmless no-op. It is not one: the
        handler reads its config BEFORE taking the write lock, so "custom is
        already active" can be stale by the time the write lands — the owner
        switches to a catalog model in between and the app's write puts custom
        back. Refusing every `model: custom` from a non-owner closes that race.
        """
        _seed_stt(
            seeded_config,
            model=stt_models.CUSTOM_MODEL,
            custom_model_path=self._PATH,
            custom_model_sha256=self._DIGEST,
        )

        resp = await core_mod.api_stt_config(
            self._put({"model": stt_models.CUSTOM_MODEL}, app_token="meetings")
        )

        assert resp.status == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            ["custom_model_path"],
            "custom_model_path",
            7,
            None,
        ],
        ids=["array", "string", "number", "null"],
    )
    async def test_a_non_object_body_is_refused_before_any_field_is_read(
        self, body, seeded_config, fake_sel
    ) -> None:
        """400, not 500, on every JSON body that is not an object.

        The array is the one that bites: it satisfies every `"field" in body`
        membership test and then raises TypeError on the subscript, so the
        endpoint answered a malformed request with a 500 — from inside the
        security gate that runs first. The scalars reach the same subscript by a
        shorter road. Refused once, before any field is read.
        """
        resp = await core_mod.api_stt_config(self._put(body))

        assert resp.status == 400
        assert self._stored(seeded_config) == {}

    @pytest.mark.asyncio
    async def test_the_owner_configures_a_custom_model_normally(self, seeded_config) -> None:
        """The capability itself is unchanged for the operator; only its caller is
        narrowed. Without this the gates above would pass over a dead feature."""
        resp = await core_mod.api_stt_config(
            self._put(
                {
                    "model": stt_models.CUSTOM_MODEL,
                    "custom_model_path": self._PATH,
                    "custom_model_sha256": self._DIGEST,
                }
            )
        )

        assert resp.status == 200
        stored = self._stored(seeded_config)
        assert stored["model"] == stt_models.CUSTOM_MODEL
        assert stored["custom_model_path"] == self._PATH
        assert stored["custom_model_sha256"] == self._DIGEST

    @pytest.mark.asyncio
    async def test_the_get_serves_the_pair_to_the_owner_alone(self, seeded_config) -> None:
        """Serving the pair to a caller that may not write it would hand it the
        exact values to echo back."""
        _seed_stt(
            seeded_config,
            model=stt_models.CUSTOM_MODEL,
            custom_model_path=self._PATH,
            custom_model_sha256=self._DIGEST,
        )

        owner = json.loads((await core_mod.api_stt_config(self._get())).body)
        assert owner["custom_model_path"] == self._PATH
        assert owner["custom_model_sha256"] == self._DIGEST

        for withheld in (
            self._get(user=self._OTHER_DASHBOARD),
            self._get(app_token="meetings"),
            self._get(app_token=None),
        ):
            body = json.loads((await core_mod.api_stt_config(withheld)).body)
            assert "custom_model_path" not in body
            assert "custom_model_sha256" not in body
            # Still a usable settings payload for everything the caller IS entitled to.
            assert body["language_code"] == owner["language_code"]

    @pytest.mark.asyncio
    async def test_the_read_side_of_the_owner_gate_is_audited_on_both_branches(
        self, seeded_config, fake_sel
    ) -> None:
        """A served pair and a withheld one both leave a SEL row.

        The write path audited only its refusal, so the READ half of the same
        authorization decision was invisible: whether the pair was ever served, and to
        whom, could not be reconstructed from the log. Both outcomes are recorded
        because the useful question is the negative one -- "nobody but the owner was
        served this" is only answerable if the allowed branch logs too.
        """
        _seed_stt(
            seeded_config,
            model=stt_models.CUSTOM_MODEL,
            custom_model_path=self._PATH,
            custom_model_sha256=self._DIGEST,
        )

        await core_mod.api_stt_config(self._get())
        await core_mod.api_stt_config(self._get(user=self._OTHER_DASHBOARD))

        reads = [
            call.kwargs
            for call in fake_sel.log_api_access.call_args_list
            if call.kwargs.get("operation") == "stt.custom_model.read"
        ]
        assert [r["outcome"] for r in reads] == ["allowed", "denied"]

    @pytest.mark.asyncio
    async def test_a_get_with_no_custom_model_in_play_writes_no_audit_row(
        self, seeded_config, fake_sel
    ) -> None:
        """No pair, no decision. This endpoint is fetched by the chat composer's voice
        hook on mount, not only by the settings page, so auditing a branch that
        discloses two empty strings would bury the rows that carry a value."""
        await core_mod.api_stt_config(self._get())

        assert [
            call.kwargs
            for call in fake_sel.log_api_access.call_args_list
            if call.kwargs.get("operation") == "stt.custom_model.read"
        ] == []

    @pytest.mark.asyncio
    async def test_the_owner_gate_audits_the_write_it_admits_not_only_the_one_it_refuses(
        self, seeded_config, fake_sel
    ) -> None:
        """An ADMITTED custom-model write leaves a SEL row of its own.

        The gate logged only its refusals, so the log answered "who was turned away"
        and never "who chose the weights" -- and only the second names the file and
        digest the gateway then verifies and loads. A denial-only record also
        cannot tell an endpoint nobody exercised from one exercised successfully.

        Same operation as the refusal, so both outcomes of one authorization decision
        read as one operation: an ordinary write that names no custom field stays
        silent, which the third assertion pins.
        """
        resp = await core_mod.api_stt_config(
            self._put(
                {
                    "model": stt_models.CUSTOM_MODEL,
                    "custom_model_path": self._PATH,
                    "custom_model_sha256": self._DIGEST,
                }
            )
        )
        assert resp.status == 200

        writes = [
            call.kwargs
            for call in fake_sel.log_api_access.call_args_list
            if call.kwargs.get("operation") == "stt.custom_model"
        ]
        assert [w["outcome"] for w in writes] == ["allowed"]

        fake_sel.log_api_access.reset_mock()
        await core_mod.api_stt_config(self._put({"language_code": "en"}))
        assert [
            call.kwargs
            for call in fake_sel.log_api_access.call_args_list
            if call.kwargs.get("operation") == "stt.custom_model"
        ] == []


class TestCustomModelPairIsMaskedOnTheGeneralConfigRead:
    """The general config read must not serve what the owner-only STT read withholds."""

    _PATH = "ggml-my-model.bin"
    _DIGEST = "c" * 64

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: core_mod.stt.Availability(False)
        )

    @pytest.mark.asyncio
    async def test_a_non_owner_general_read_masks_both_fields(self, seeded_config) -> None:
        _seed_stt(
            seeded_config,
            model=stt_models.CUSTOM_MODEL,
            custom_model_path=self._PATH,
            custom_model_sha256=self._DIGEST,
        )
        req = _req(
            app={"state": SimpleNamespace(owner_id="owner-1")},
            user="messaging-user",
            app_token="",
        )
        req.method = "GET"

        stt_section = json.loads((await core_mod.api_kirocrew_config(req)).body)["stt"]

        assert stt_section["custom_model_path"] == core_mod._SENSITIVE_MASK
        assert stt_section["custom_model_sha256"] == core_mod._SENSITIVE_MASK

    @pytest.mark.asyncio
    async def test_the_owner_stt_read_still_returns_both_fields(self, seeded_config) -> None:
        _seed_stt(
            seeded_config,
            model=stt_models.CUSTOM_MODEL,
            custom_model_path=self._PATH,
            custom_model_sha256=self._DIGEST,
        )
        req = _req(
            app={"state": SimpleNamespace(owner_id="owner-1")},
            user="owner-1",
            app_token="",
        )
        req.method = "GET"

        body = json.loads((await core_mod.api_stt_config(req)).body)

        assert body["custom_model_path"] == self._PATH
        assert body["custom_model_sha256"] == self._DIGEST


class TestSttConfigCustomModelPath:
    """``PUT /api/config/stt`` stores a custom model path only if a load could use it.

    The file must resolve -- symlinks followed -- to a regular file strictly inside
    the models directory. Anything else is a 400 naming the rule, nothing is
    stored, and the message never quotes the path.
    """

    _DIGEST = "b" * 64

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(core_mod, "_stt_prereq_commands", lambda _p: [])
        _floor_monkeypatch.setattr(
            core_mod, "availability_detail", lambda _cfg: core_mod.stt.Availability(False)
        )

    @staticmethod
    def _owner_put(body: object) -> web.Request:
        req = _json_req(
            body,
            app={"state": SimpleNamespace(owner_id="owner-1")},
            user="owner-1",
            app_token="",
        )
        req.method = "PUT"
        return req

    @staticmethod
    def _stored(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8")).get("stt", {})

    @pytest.mark.asyncio
    async def test_a_file_in_the_models_dir_is_stored(self, seeded_config, fake_sel) -> None:
        stt_models.models_dir().mkdir(parents=True, exist_ok=True)
        (stt_models.models_dir() / "ggml-mine.bin").write_bytes(b"weights")

        resp = await core_mod.api_stt_config(
            self._owner_put(
                {"custom_model_path": " ggml-mine.bin ", "custom_model_sha256": self._DIGEST}
            )
        )

        assert resp.status == 200
        assert self._stored(seeded_config)["custom_model_path"] == "ggml-mine.bin"
        assert json.loads(resp.body)["custom_model_dir"] == str(stt_models.models_dir())

    @pytest.mark.asyncio
    async def test_a_path_outside_the_models_dir_is_refused(
        self, seeded_config, fake_sel, tmp_path
    ) -> None:
        """The containment rule, through every door: ``..``, an absolute path
        elsewhere, and a symlink inside the folder that points out of it. The file
        each one names EXISTS, so the refusal is about where it is, not whether."""
        stt_models.models_dir().mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "elsewhere" / "ggml-evil.bin"
        outside.parent.mkdir()
        outside.write_bytes(b"weights")
        link = stt_models.models_dir() / "ggml-link.bin"
        link.symlink_to(outside)
        sibling = stt_models.models_dir().parent / "ggml-sibling.bin"
        sibling.write_bytes(b"weights")

        for value in (
            "../ggml-sibling.bin",
            str(outside),
            "ggml-link.bin",
            str(stt_models.models_dir()),
        ):
            resp = await core_mod.api_stt_config(self._owner_put({"custom_model_path": value}))
            body = json.loads(resp.body)
            assert resp.status == 400, value
            assert body["code"] == "custom_model_path_outside", value
            assert value not in body["error"]
            assert "custom_model_path" not in self._stored(seeded_config)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("setup", "value", "code"),
        [
            (None, "ggml-absent.bin", "custom_model_path_missing"),
            (None, "~nonexistent-user-14918/w.bin", "custom_model_path_missing"),
            ("dir", "ggml-a-folder.bin", "custom_model_path_not_file"),
            (None, "   ", "custom_model_path_empty"),
            (None, 7, "custom_model_path_empty"),
        ],
        ids=["missing", "unknown-tilde-user", "folder", "blank", "not-a-string"],
    )
    async def test_an_unusable_file_is_refused_with_its_reason(
        self, seeded_config, fake_sel, setup, value, code
    ) -> None:
        stt_models.models_dir().mkdir(parents=True, exist_ok=True)
        if setup == "dir":
            (stt_models.models_dir() / value).mkdir()

        resp = await core_mod.api_stt_config(self._owner_put({"custom_model_path": value}))

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == code
        assert "custom_model_path" not in self._stored(seeded_config)

    @pytest.mark.asyncio
    async def test_a_malformed_digest_is_refused_not_silently_dropped(
        self, seeded_config, fake_sel
    ) -> None:
        resp = await core_mod.api_stt_config(self._owner_put({"custom_model_sha256": "abc"}))

        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "custom_model_sha256_invalid"
        assert "custom_model_sha256" not in self._stored(seeded_config)

    @pytest.mark.asyncio
    async def test_an_empty_string_clears_both_fields(self, seeded_config, fake_sel) -> None:
        _seed_stt(seeded_config, custom_model_path="ggml-old.bin", custom_model_sha256=self._DIGEST)

        resp = await core_mod.api_stt_config(
            self._owner_put({"custom_model_path": "", "custom_model_sha256": ""})
        )

        assert resp.status == 200
        stored = self._stored(seeded_config)
        assert stored["custom_model_path"] == ""
        assert stored["custom_model_sha256"] == ""

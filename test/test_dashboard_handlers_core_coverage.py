"""Coverage for ``dashboard/handlers/core.py`` error branches and cold paths.

Targets the parts of the module the rest of the suite never reaches: the STT
prerequisite/status/prepare/prewarm/transcribe surface, the SEL + security read
endpoints, the agent-settings PUT validators, the loopback-gated local endpoints
(token / logout), the app-secret exchange, and the session sub-agent routes.

Style follows ``test_api_health.py`` (direct handler calls against a
``MagicMock(spec=web.Request)``) and ``test_config_patch.py`` (real aiohttp
``TestClient`` when the handler needs a genuine request body or streaming
response). Every write lands under the autouse-isolated ``KIROCREW_HOME``
from ``conftest.py``; nothing here touches the network, downloads a model, or
spawns a process.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import extras
from kiro_crew import transcribe as transcribe_mod
from kiro_crew.config.loader import (
    MAX_SUBAGENTS_FIXED_FLOOR,
    SUBAGENT_AUTO_MAX_CEILING,
    SUBAGENT_MAX_TURNS_CEILING,
    config_path,
)
from kiro_crew.dashboard.handlers import _shared as shared_mod
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.sel import SelVerification as _SelVerification
from kiro_crew.stt import models as stt_models
from kiro_crew.stt.limits import MIN_PARTIAL_INTERVAL_MS, MIN_SILENCE_MS

# ── shared helpers ───────────────────────────────────────────────────────


def _req(
    *,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    query: dict | None = None,
    app: dict | None = None,
    match_info: dict | None = None,
    user: str | None = "dashboard",
    app_token: str | None = "",
) -> web.Request:
    """A stub request carrying only what these handlers read.

    ``app_token`` is the verified app claim the auth middleware publishes, and it
    is a different thing from ``app`` (the aiohttp application): ``""`` means the
    dashboard user, a name means an app token, and ``None`` reproduces a path
    where no auth middleware ran and the claim is absent.
    """
    req = MagicMock(spec=web.Request)
    req.remote = remote
    req.headers = headers or {}
    req.query = query or {}
    req.app = app if app is not None else {}
    req.match_info = match_info or {}
    claims: dict = {"user": user, "app": app_token}
    req.get = lambda key, default=None: claims.get(key, default)
    return req


def _json_req(body: object = None, *, raises: bool = False, **kwargs) -> web.Request:
    """A stub request whose ``await request.json()`` yields *body*.

    ``raises=True`` reproduces an absent or unparseable body, which the prepare
    endpoint has to read as "the configured model" rather than as an error.
    """
    req = _req(**kwargs)
    req.json = AsyncMock(side_effect=ValueError("no body") if raises else None, return_value=body)
    return req


async def _drain_stt_background() -> list:
    """Await the handler's detached tasks and return their outcomes.

    The prepare/prewarm endpoints answer before their work finishes, so the task
    is still pending when the handler returns. Draining it here is both the
    assertion (it really was scheduled) and the hygiene: an un-awaited task
    outlives the test's event loop, and an unretrieved exception surfaces later
    as a warning attributed to whichever test happened to run next.
    """
    tasks = list(core_mod._stt_background_tasks)
    return await asyncio.gather(*tasks, return_exceptions=True)


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
def model_store(monkeypatch) -> stt_models.ModelStore:
    """Give the test its own model store.

    The real one is a process global holding live download state, so a test that
    let a handler touch it would hand its progress block to whatever test ran
    next on the same xdist worker.
    """
    store = stt_models.ModelStore()
    monkeypatch.setattr(stt_models, "_store", store)
    return store


@pytest.fixture
def stt_background(monkeypatch) -> set:
    """Give the test its own detached-task set.

    ``_stt_background_tasks`` is a module global, so "no work was scheduled"
    assertions would otherwise be answerable by a task some other file left
    pending on the same worker. Production reads the global on every add, so
    swapping the object is enough.
    """
    tasks: set = set()
    monkeypatch.setattr(core_mod, "_stt_background_tasks", tasks)
    return tasks


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
        # `shutil.which` stub no longer decides the answer (and, being a module-global
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

        A distribution with no ffmpeg package and no build script in reach used to
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

        def _unexpected_system_install():
            raise AssertionError("desktop tried to make the user install ffmpeg")

        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", _unexpected_system_install)
        assert core_mod._stt_prereq_commands("transcribe") == []

    def test_source_install_can_still_offer_the_system_decoder_fallback(self, monkeypatch) -> None:
        monkeypatch.setattr(core_mod, "_transcribe_extra_importable", lambda: True)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(core_mod, "_ffmpeg_install_commands", lambda: ["brew install ffmpeg"])
        assert core_mod._stt_prereq_commands("transcribe") == ["brew install ffmpeg"]


class TestPipInstallChannel:
    @pytest.fixture(autouse=True)
    def _not_bundled(self, monkeypatch):
        """Pin the desktop-bundle probe; the bundled case has its own test."""
        monkeypatch.setattr(shared_mod.platform_compat, "is_bundled_interpreter", lambda: False)

    def test_bundled_desktop_interpreter_has_no_channel(self, monkeypatch) -> None:
        """A pip install into the desktop app's code-signed bundle breaks
        launches/updates and is discarded on every app update — the command
        must not be offered there even though pip itself may exist."""
        monkeypatch.setattr(shared_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        assert core_mod._pip_install_channel_available() is False

    def test_pipless_interpreter_has_no_channel(self, monkeypatch) -> None:
        """uv tool installs and some pipx layouts ship no `pip` module, so
        `<python> -m pip` fails immediately — the command must not be shown."""
        real = shared_mod.importlib.util.find_spec
        monkeypatch.setattr(
            shared_mod.importlib.util,
            "find_spec",
            lambda name, *a: None if name == "pip" else real(name, *a),
        )
        assert core_mod._pip_install_channel_available() is False

    def test_externally_managed_python_has_no_channel(self, monkeypatch, tmp_path) -> None:
        """PEP 668: pip refuses installs into an externally-managed
        interpreter (distro/brew pythons) — but only outside a venv."""
        monkeypatch.setattr(shared_mod.sys, "prefix", shared_mod.sys.base_prefix)
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(shared_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is False

    def test_venv_on_managed_base_has_a_channel(self, monkeypatch, tmp_path) -> None:
        """Inside a venv pip ignores PEP 668, and `sysconfig.get_path("stdlib")`
        resolves to the BASE interpreter's directory where distro pythons put
        the marker — the recommended install layout (venv on a Debian/brew
        python) must not be misread as unsupported."""
        monkeypatch.setattr(shared_mod.sys, "prefix", str(tmp_path / "venv"))
        monkeypatch.setattr(shared_mod.sys, "base_prefix", str(tmp_path / "base"))
        (tmp_path / "EXTERNALLY-MANAGED").write_text("", encoding="utf-8")
        monkeypatch.setattr(shared_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True

    def test_ordinary_venv_has_a_channel(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(shared_mod.sys, "prefix", shared_mod.sys.base_prefix)
        monkeypatch.setattr(shared_mod.sysconfig, "get_path", lambda name: str(tmp_path))
        assert core_mod._pip_install_channel_available() is True


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
            refused = await client.put("/api/config/stt", json={"model": "turbo"})
            assert refused.status == 200
            assert (await refused.json())["model"] == "small"
            accepted = await client.put("/api/config/stt", json={"model": "large-v3-turbo"})
            assert (await accepted.json())["model"] == "large-v3-turbo"
        stt = json.loads(seeded_config.read_text(encoding="utf-8"))["stt"]
        assert stt["model"] == "large-v3-turbo"

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
    async def test_get_advertises_capabilities(self, seeded_config) -> None:
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
        assert body["language_codes"][0] == "en-US"
        assert body["available"] is False
        assert body["prereqs"] == []
        # This test venv has a working pip channel, so the unsupported flag
        # must be False regardless of installed extras.
        assert body["transcribe_unsupported"] is False
        # Cause discriminator for the unsupported notice: the desktop bundle
        # needs different guidance than a pip-less/PEP 668 interpreter. A test
        # venv is never the bundled app.
        assert body["bundled_interpreter"] is False
        # Served independently of `available` so the UI can flag the .webm
        # decode gap even when the provider reads ready.
        assert isinstance(body["ffmpeg_missing"], bool)

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


# ── STT status / prepare / prewarm ──────────────────────────────────────


def _availability(ok: bool, code: str = "", detail: str = ""):
    """Build the shape ``availability_detail`` returns, without probing the host."""
    return core_mod.stt.Availability(ok, code, detail)


def _seed_stt(path: Path, **fields) -> None:
    """Merge *fields* into the ``stt`` section of the config at *path*.

    Used to give a test a configured value that is NOT the default, so "used the
    configured model" is distinguishable from "fell back to the catalog default".
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("stt", {}).update(fields)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")


class TestSttStatus:
    """``GET /api/stt/status`` is the runtime half of the settings surface.

    ``GET /api/config/stt`` serves what the operator chose; this serves what the
    host can currently do about it: the availability reason as a code the
    dashboard can localise, whether the chosen model is on disk, whether one is
    resident right now, and the progress of a transfer in flight.
    """

    @pytest.fixture(autouse=True)
    def _quiet_probes(self, monkeypatch):
        monkeypatch.setattr(core_mod, "availability_detail", lambda _cfg: _availability(True))
        monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: False)
        monkeypatch.setattr(
            "kiro_crew.stt.engine.shared_engine", lambda *a, **k: SimpleNamespace(loaded=False)
        )
        # The decoder probe stats the operator's real package directories and can
        # hash an 80 MB executable, so the default answer is pinned; the tests
        # that assert on it override this.
        monkeypatch.setattr(core_mod, "ensure_ffmpeg_in_path", lambda: None)
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)

    @pytest.mark.asyncio
    async def test_status_reports_the_resolved_model_and_the_whole_catalog(
        self, seeded_config, model_store
    ) -> None:
        """The catalog cannot be a static frontend table: `present` is per-host
        state that changes as models are fetched, and it is the only thing that
        tells the picker which choice costs a download."""
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["provider"] == "local"
        assert body["model"] == stt_models.DEFAULT_MODEL
        assert body["model_present"] is False
        assert body["model_bytes"] == stt_models.resolve(stt_models.DEFAULT_MODEL).size_bytes
        # Smallest first, which is the order the picker offers them in.
        assert [row["name"] for row in body["models"]] == [m.name for m in stt_models.CATALOG]
        assert all(row["present"] is False for row in body["models"])
        assert all(row["size_bytes"] > 0 for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_reports_a_present_model_as_present(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: True)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["model_present"] is True
        assert all(row["present"] is True for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_reads_presence_through_the_owning_module(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """A materialised package attribute must not shadow the real function.

        ``kiro_crew.stt`` re-exports its surface through a lazy ``__getattr__``, and
        that indirection stops working for any name the package itself comes to
        hold: a ``monkeypatch.setattr(stt, "is_present", ...)`` anywhere in the
        suite writes the original back as a real attribute on teardown, after which
        every reader of ``stt.is_present`` is pinned to the value captured then.

        This handler therefore reads ``stt.models.is_present``. The test recreates
        the materialised attribute deliberately, because the bug it guards against
        was order-dependent and silent: the endpoint reported every model absent on
        a host where the files were present, and only in a full-suite run.
        """
        monkeypatch.setattr(core_mod.stt, "is_present", lambda _m: False, raising=False)
        monkeypatch.setattr("kiro_crew.stt.models.is_present", lambda _m: True)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["model_present"] is True
        assert all(row["present"] is True for row in body["models"])

    @pytest.mark.asyncio
    async def test_status_forwards_the_availability_code_and_detail(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """The code is the contract and the prose is advisory: the dashboard
        renders localised text, so it cannot key off an English sentence, and
        "install an extra" leads somewhere completely different from "this
        platform has no prebuilt wheel"."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: _availability(False, core_mod.stt.CODE_EXTRA_MISSING, "needs the extra"),
        )
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["available"] is False
        assert body["code"] == core_mod.stt.CODE_EXTRA_MISSING
        assert body["detail"] == "needs the extra"

    @pytest.mark.asyncio
    async def test_status_reports_whether_a_model_is_resident(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """Residency is what decides between a 30 ms transcription and one that
        pays a model load first, so the panel can say which the user will get."""
        monkeypatch.setattr(
            "kiro_crew.stt.engine.shared_engine", lambda *a, **k: SimpleNamespace(loaded=True)
        )
        assert json.loads((await core_mod.api_stt_status(_req())).body)["engine_loaded"] is True

    @pytest.mark.asyncio
    async def test_status_republishes_the_live_download_block(
        self, seeded_config, model_store
    ) -> None:
        """A first-run fetch is 148 MB at the default, so a panel polling this is
        the only thing standing between the user and an unexplained wait."""
        model_store._set(step="downloading", model="base", done=1024, total=147_951_465)
        body = json.loads((await core_mod.api_stt_status(_req())).body)
        assert body["download"] == {
            "step": "downloading",
            "model": "base",
            "downloaded_bytes": 1024,
            "total_bytes": 147_951_465,
            "error": "",
        }

    @pytest.mark.asyncio
    async def test_status_reports_which_decoder_the_transcode_path_would_run(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """`source` names WHICH decoder, because each one is repaired differently.

        A bundled payload is fixed by reinstalling the app, a system one by the
        host's package manager, and the store one by the decoder fetch — so a bare
        "present: true" would send the reader looking for the wrong remedy.
        """
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: transcribe_mod.FFMPEG_SOURCE_STORE)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["present"] is True
        assert body["source"] == "store"
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_AVAILABLE
        # The GATEWAY's platform, not the browser's: a dashboard can be open
        # against another machine, and the hand-off prompt names the host that
        # actually needs fixing.
        assert body["os"] == platform.system()
        assert body["arch"] == platform.machine()
        assert body["download"] == dict(core_mod.stt_decoder.store().status)

    @pytest.mark.asyncio
    async def test_status_reports_no_decoder_as_absent_with_no_source(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["present"] is False
        assert body["source"] is None

    @pytest.mark.asyncio
    async def test_status_marks_a_platform_with_no_pinned_artifact_unsupported(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """`unsupported` is not a failure: nothing a fetch does can change it.

        The page has to offer the manual system-decoder route there rather than a
        retry, which is why this is a separate value from a failed download.
        """
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        monkeypatch.setattr(core_mod.stt_decoder, "artifact_for", lambda *a, **k: None)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_status_marks_a_desktop_release_as_bundled(
        self, seeded_config, monkeypatch, model_store
    ) -> None:
        """A release carries its own authenticated decoder and repairs by reinstall."""
        monkeypatch.setattr(core_mod, "ffmpeg_source", lambda: None)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        body = json.loads((await core_mod.api_stt_status(_req())).body)["ffmpeg"]
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_BUNDLED

    @pytest.mark.asyncio
    async def test_status_refuses_an_app_token(self, seeded_config, fake_sel, model_store) -> None:
        """``request["user"]`` is truthy for an app token too, so the cookie check
        alone does not separate a browser from an app that named this path in its
        manifest. Reading the host's setup state is operator business, not
        something an app earns by declaring a path."""
        resp = await core_mod.api_stt_status(_req(app_token="meetings"))
        assert resp.status == 403
        body = json.loads(resp.body)
        assert body["code"] == "dashboard_user_required"
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_status_refuses_a_request_with_no_app_claim_at_all(
        self, seeded_config, fake_sel, model_store
    ) -> None:
        """Fail closed: an absent claim means no auth middleware published one, so
        it must be refused alongside a non-empty one rather than read as the
        dashboard user by default."""
        resp = await core_mod.api_stt_status(_req(app_token=None))
        assert resp.status == 403


class TestSttPrepare:
    """``POST /api/stt/prepare`` starts, or joins, the one-time model download."""

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.fixture(autouse=True)
    def _no_real_download(self, monkeypatch):
        """Record the requested model instead of fetching 148 MB over the wire."""
        self.requested: list[str] = []

        async def _fake_ensure(name: str) -> bool:
            self.requested.append(name)
            return True

        monkeypatch.setattr("kiro_crew.stt.session.ensure_model", _fake_ensure)

    @pytest.mark.asyncio
    async def test_prepare_answers_202_and_starts_the_transfer(
        self, seeded_config, model_store
    ) -> None:
        """202, not 200: the fetch outlives the request on purpose, so the user is
        not held behind it and the caller polls the status endpoint instead."""
        resp = await core_mod.api_stt_prepare(_json_req(raises=True))
        assert resp.status == 202
        body = json.loads(resp.body)
        assert body["model"] == stt_models.DEFAULT_MODEL
        assert body["download"] == dict(model_store.status)
        assert await _drain_stt_background() == [True]
        assert self.requested == [stt_models.DEFAULT_MODEL]

    @pytest.mark.asyncio
    async def test_prepare_honours_a_requested_model(self, seeded_config, model_store) -> None:
        """The picker can offer the weights BEFORE the selection is committed, so
        the operator is not asked to save a setting to find out what it costs."""
        resp = await core_mod.api_stt_prepare(_json_req({"model": "tiny"}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "tiny"
        await _drain_stt_background()
        assert self.requested == ["tiny"]

    @pytest.mark.asyncio
    async def test_prepare_degrades_an_unknown_model_to_the_catalog_default(
        self, seeded_config, model_store
    ) -> None:
        """A model name becomes a filename under the models directory and a URL
        under the pinned base, so only catalog names may reach either. An unknown
        one resolves to the default the same way a stale configured value does."""
        resp = await core_mod.api_stt_prepare(_json_req({"model": "../../etc/passwd"}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == stt_models.DEFAULT_MODEL
        await _drain_stt_background()
        # The name handed to the downloader resolves to the same catalog entry, so
        # nothing outside the catalog reaches the filesystem or the network.
        assert [stt_models.resolve(n).name for n in self.requested] == [stt_models.DEFAULT_MODEL]

    @pytest.mark.asyncio
    async def test_prepare_with_a_non_object_body_uses_the_configured_model(
        self, seeded_config, model_store
    ) -> None:
        """A JSON array, or anything else that is not an object, carries no model
        name. The configured one is the only reading that leaves the endpoint
        useful to a caller sending no body at all."""
        _seed_stt(seeded_config, model="small")
        resp = await core_mod.api_stt_prepare(_json_req(["tiny"]))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "small"
        await _drain_stt_background()
        assert self.requested == ["small"]

    @pytest.mark.asyncio
    async def test_prepare_with_a_blank_model_uses_the_configured_one(
        self, seeded_config, model_store
    ) -> None:
        _seed_stt(seeded_config, model="small")
        resp = await core_mod.api_stt_prepare(_json_req({"model": ""}))
        assert resp.status == 202
        assert json.loads(resp.body)["model"] == "small"
        await _drain_stt_background()
        assert self.requested == ["small"]

    @pytest.mark.asyncio
    async def test_prepare_joins_a_transfer_already_running(
        self, seeded_config, model_store
    ) -> None:
        """A polling panel must not accumulate one task per poll. Concurrent
        callers are made safe by the store's own lock, so this only has to avoid
        piling up work nobody will read."""
        model_store._set(step="downloading", model="base", done=512, total=147_951_465)
        resp = await core_mod.api_stt_prepare(_json_req(raises=True))
        assert resp.status == 202
        assert json.loads(resp.body)["download"]["downloaded_bytes"] == 512
        assert await _drain_stt_background() == []
        assert self.requested == []

    @pytest.mark.asyncio
    async def test_prepare_refuses_an_app_token(self, seeded_config, fake_sel, model_store) -> None:
        """An app naming this path must not be able to start a 148 MB transfer on
        the operator's connection."""
        resp = await core_mod.api_stt_prepare(_json_req(raises=True, app_token="meetings"))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_user_required"
        assert await _drain_stt_background() == []
        assert self.requested == []


class TestSttFfmpegDownload:
    """``POST /api/stt/ffmpeg/download`` fetches the audio decoder into the store.

    Same 202-then-poll contract as the model transfer: a ~30 MB wheel is not
    something to hold a request open behind, and the caller reads progress from
    ``GET /api/stt/status``'s ``ffmpeg.download``.
    """

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.fixture(autouse=True)
    def decoder_store(self, monkeypatch):
        """Give the test its own decoder store, and never let it reach the network.

        The real one is a process global holding live transfer state, so a test
        that let the handler touch it would hand its progress block to whatever
        test ran next on the same xdist worker.
        """
        store = core_mod.stt_decoder.DecoderStore()
        self.ensured: list[str] = []

        async def _fake_ensure():
            self.ensured.append("ensure")
            return None

        store.ensure = _fake_ensure  # type: ignore[method-assign]
        monkeypatch.setattr(core_mod.stt_decoder, "_store", store)
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: False)
        return store

    @pytest.mark.asyncio
    async def test_answers_202_and_starts_the_fetch(self, seeded_config, decoder_store) -> None:
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 202
        assert json.loads(resp.body)["download"] == dict(decoder_store.status)
        assert await _drain_stt_background() == [None]
        assert self.ensured == ["ensure"]

    @pytest.mark.asyncio
    async def test_joins_a_fetch_already_running(self, seeded_config, decoder_store) -> None:
        """A polling panel must not accumulate one task per poll.

        Concurrent callers are made safe by the store's own lock, so this only has
        to avoid piling up work nobody will read.
        """
        decoder_store._set(
            stage=core_mod.stt_decoder.STAGE_DOWNLOADING,
            artifact="ffmpeg-linux-x86_64-v7.0.2",
            done=512,
            total=29_498_237,
        )
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 202
        assert json.loads(resp.body)["download"]["downloaded_bytes"] == 512
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_platform_with_no_pinned_artifact(
        self, seeded_config, monkeypatch, decoder_store
    ) -> None:
        """409 rather than a 202 that can never finish: nothing is pinned to fetch."""
        monkeypatch.setattr(core_mod.stt_decoder, "artifact_for", lambda *a, **k: None)
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == core_mod.stt_decoder.CODE_UNSUPPORTED
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_UNSUPPORTED
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_bundled_desktop_release(
        self, seeded_config, monkeypatch, decoder_store
    ) -> None:
        """A release already carries an authenticated decoder its resolver uses.

        Its resolver deliberately never looks outside that payload, so a fetch here
        would spend the operator's bandwidth on a file nothing can execute — and
        the real remedy (reinstall the app) is what the page says instead.
        """
        monkeypatch.setattr(core_mod.platform_compat, "is_bundled_interpreter", lambda: True)
        resp = await core_mod.api_stt_ffmpeg_download(_req())
        assert resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == core_mod._CODE_STT_DECODER_BUNDLED
        assert body["auto_fetch"] == core_mod.AUTO_FETCH_BUNDLED
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_an_app_token(self, seeded_config, fake_sel, decoder_store) -> None:
        """An app naming this path must not start a transfer on the operator's line."""
        resp = await core_mod.api_stt_ffmpeg_download(_req(app_token="meetings"))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_user_required"
        assert await _drain_stt_background() == []
        assert self.ensured == []

    @pytest.mark.asyncio
    async def test_refuses_a_request_with_no_app_claim_at_all(
        self, seeded_config, fake_sel, decoder_store
    ) -> None:
        resp = await core_mod.api_stt_ffmpeg_download(_req(app_token=None))
        assert resp.status == 403
        assert self.ensured == []

    def test_the_route_is_registered(self) -> None:
        """The handler is unreachable without its route, and 404 says nothing."""
        from aiohttp import web as _web

        from kiro_crew.dashboard.routes import memory as memory_routes

        app = _web.Application()
        memory_routes.register(app)
        paths = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.resource is not None
        }
        assert ("POST", "/api/stt/ffmpeg/download") in paths


class TestSttPrewarm:
    """``POST /api/stt/prewarm`` pays the load while the user is still speaking."""

    @pytest.fixture(autouse=True)
    def _isolated_tasks(self, stt_background):
        """Every test here asserts on what was scheduled, so the set must be ours."""

    @pytest.mark.asyncio
    async def test_prewarm_answers_202_without_waiting_for_the_load(
        self, seeded_config, monkeypatch
    ) -> None:
        """A first-ever load compiles a GPU pipeline, so awaiting it here would
        stall the request that exists precisely to get ahead of that cost. The
        fake stays blocked until after the response is asserted, so a handler that
        awaited it would never reach the assertion at all.

        Both configured values are non-default, so this also proves the warm-up
        targets the operator's selection rather than the catalog default.
        """
        _seed_stt(seeded_config, model="small", language_code="fr-FR")
        seen: dict = {}
        release = asyncio.Event()

        async def _fake_prewarm(*, model_name: str = "", language: str = ""):
            seen.update({"model_name": model_name, "language": language})
            await release.wait()
            return _availability(True)

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _fake_prewarm)
        resp = await core_mod.api_stt_prewarm(_req())
        assert resp.status == 202
        assert json.loads(resp.body) == {"ok": True}
        release.set()
        await _drain_stt_background()
        assert seen["model_name"] == "small"
        # The BCP-47 config value is reduced to the bare language code the
        # recogniser names its languages by.
        assert seen["language"] == "fr"

    @pytest.mark.asyncio
    async def test_prewarm_failure_does_not_fail_the_request(
        self, seeded_config, monkeypatch
    ) -> None:
        """Prewarming is an optimisation. A host that cannot load the model still
        has to be able to open the microphone, where the same failure is reported
        as an error frame carrying a code."""

        async def _boom(**_kwargs):
            raise RuntimeError("no recogniser here")

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _boom)
        resp = await core_mod.api_stt_prewarm(_req())
        assert resp.status == 202
        outcomes = await _drain_stt_background()
        assert [type(o).__name__ for o in outcomes] == ["RuntimeError"]

    @pytest.mark.asyncio
    async def test_prewarm_refuses_an_app_token(self, seeded_config, fake_sel, monkeypatch) -> None:
        """Warming a resident model inside the gateway is operator setup, so an
        app token must not reach it even though ``request["user"]`` is truthy."""
        started: list[str] = []

        async def _fake_prewarm(**_kwargs):
            started.append("ran")
            return _availability(True)

        monkeypatch.setattr("kiro_crew.stt.session.prewarm", _fake_prewarm)
        resp = await core_mod.api_stt_prewarm(_req(app_token="meetings"))
        assert resp.status == 403
        assert await _drain_stt_background() == []
        assert started == []
        assert fake_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


# ── STT transcribe endpoint ─────────────────────────────────────────────


def _multipart_req(field) -> web.Request:
    reader = SimpleNamespace(next=AsyncMock(return_value=field))
    req = _req()
    req.multipart = AsyncMock(return_value=reader)
    return req


class TestSttTranscribe:
    """The batch upload path: record first, upload afterwards.

    Every non-2xx body here carries a machine-readable ``code``. Backend-owned
    strings have no translation catalog, so the English prose is advisory and the
    code is the only thing the dashboard can branch on.
    """

    @pytest.fixture(autouse=True)
    def _backend_ready(self, monkeypatch):
        """Pin the availability probe, which imports an optional extra.

        Left real, every test in this class would answer differently on a host
        with the ``voice`` extra installed than on one without.
        """
        monkeypatch.setattr(core_mod, "availability_detail", lambda _cfg: _availability(True))

    @pytest.mark.asyncio
    async def test_unavailable_backend_is_503_naming_the_reason(self, monkeypatch) -> None:
        """The provider's own code and prose are forwarded rather than flattened:
        the caller has to be able to tell "install an extra" from "download the
        model" without reading an English sentence."""
        monkeypatch.setattr(
            core_mod,
            "availability_detail",
            lambda _cfg: _availability(
                False, core_mod.stt.CODE_MODEL_MISSING, "speech model base is not downloaded"
            ),
        )
        resp = await core_mod.api_stt_transcribe(_req())
        assert resp.status == 503
        body = json.loads(resp.body)
        assert body["code"] == core_mod.stt.CODE_MODEL_MISSING
        assert body["error"] == "speech model base is not downloaded"

    @pytest.mark.asyncio
    async def test_a_reasonless_refusal_still_carries_a_code(self, monkeypatch) -> None:
        """A provider that answers "no" without saying why must not produce a body
        with an empty code: the dashboard would have nothing to render at all."""
        monkeypatch.setattr(core_mod, "availability_detail", lambda _cfg: _availability(False))
        resp = await core_mod.api_stt_transcribe(_req())
        assert resp.status == 503
        body = json.loads(resp.body)
        assert body["code"] == "stt_unavailable"
        assert body["error"] == "STT not available"

    @pytest.mark.asyncio
    async def test_missing_audio_field_is_400(self) -> None:
        resp = await core_mod.api_stt_transcribe(_multipart_req(None))
        assert resp.status == 400
        body = json.loads(resp.body)
        assert body["error"] == "missing audio field"
        assert body["code"] == "stt_missing_audio_field"

    @pytest.mark.asyncio
    async def test_wrong_field_name_is_400(self) -> None:
        field = SimpleNamespace(name="video", filename="x.webm", read_chunk=AsyncMock())
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "stt_missing_audio_field"

    @pytest.mark.asyncio
    async def test_oversized_upload_is_413(self) -> None:
        field = SimpleNamespace(
            name="audio",
            filename="recording.mp4",
            read_chunk=AsyncMock(return_value=b"0" * (25 * 1024 * 1024 + 1)),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 413
        body = json.loads(resp.body)
        assert body["error"] == "audio too large"
        assert body["code"] == "stt_audio_too_large"

    @pytest.mark.asyncio
    async def test_transcript_is_returned_and_redacted(self, monkeypatch) -> None:
        """A dictated credential must not come back in the response body: speech
        reaches this endpoint from a microphone, so nothing upstream of it has had
        a chance to screen what was said."""
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(return_value="the key is AKIAIOSFODNN7EXAMPLE thanks"),
        )
        field = SimpleNamespace(
            name="audio",
            filename="recording.ogg",
            read_chunk=AsyncMock(side_effect=[b"audio-bytes", b""]),
        )
        resp = await core_mod.api_stt_transcribe(_multipart_req(field))
        assert resp.status == 200
        text = json.loads(resp.body)["text"]
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        # The surrounding speech survives: redaction replaces the secret, it does
        # not discard the transcript the user is waiting for.
        assert text.startswith("the key is ")
        assert text.endswith(" thanks")

    @pytest.mark.asyncio
    async def test_backend_failure_is_a_generic_500(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.transcribe.transcribe_audio",
            AsyncMock(side_effect=RuntimeError("recogniser exploded")),
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
        assert body["code"] == "stt_transcription_failed"
        assert "recogniser exploded" not in json.dumps(body)


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
        fake_sel.verify_integrity.return_value = _SelVerification(7, 7, True, "")
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body == {
            "total": 7,
            "valid": 7,
            "integrity": "ok",
            "tampered": 0,
            "detail": "",
        }

    @pytest.mark.asyncio
    async def test_verify_reports_tampering(self, fake_sel) -> None:
        fake_sel.verify_integrity.return_value = _SelVerification(7, 5, True, "")
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["integrity"] == "compromised"
        assert body["tampered"] == 2

    @pytest.mark.asyncio
    async def test_verify_reports_unverifiable_history(self, fake_sel) -> None:
        """A refused pin must not answer ``ok`` over the live log alone."""
        fake_sel.verify_integrity.return_value = _SelVerification(
            7, 7, False, "segment directory refused to pin (planted link?)"
        )
        body = json.loads((await core_mod.api_sel_verify(_req())).body)
        assert body["integrity"] == "unverifiable"
        assert "refused" in body["detail"]


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
    async def test_corrupt_config_is_500_not_a_silent_reset(self, seeded_config, fake_sel) -> None:
        seeded_config.write_text("<<not json>>", encoding="utf-8", newline="\n")
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
            assert resp.status == 500
            assert (await resp.json())["error"] == "config.json is corrupt"
        assert seeded_config.read_text(encoding="utf-8") == "<<not json>>"

    @pytest.mark.asyncio
    async def test_out_of_range_turns_is_denied(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": SUBAGENT_MAX_TURNS_CEILING + 1})
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
            resp = await _put_agent(client, {"subagent_auto_max": SUBAGENT_AUTO_MAX_CEILING + 1})
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
        assert json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["max_subagents"] == 0

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


class TestAgentSettingsPutLockOffload:
    """Regression tests for the locked + offloaded read-modify-write path.

    Before the fix the PUT handler called ``path.read_text`` / ``os.replace``
    directly on the event loop, racing concurrent writers (lost-write) and
    blocking the loop.  After the fix the write goes through
    ``update_config_locked`` under ``_get_config_lock``, making two
    concurrent PUTs serialize rather than clobber each other.
    """

    @pytest.mark.asyncio
    async def test_concurrent_puts_serialize_not_clobber(self, seeded_config, fake_sel) -> None:
        """Two concurrent PUTs must both land — neither write clobbers the other.

        We fire two coroutines simultaneously: one sets ``subagent_max_turns=3``
        and one sets ``max_subagents=4``.  Because the RMW is now serialized
        under the config lock, the config file must end up with BOTH values
        after both coroutines complete, not just the last one written.
        """
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            r1, r2 = await asyncio.gather(
                _put_agent(client, {"subagent_max_turns": 3}),
                _put_agent(client, {"max_subagents": 0}),
            )
        assert r1.status == 200
        assert r2.status == 200
        persisted = json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]
        # Both writes must have survived — a lost-write would drop one of them.
        assert persisted.get("subagent_max_turns") == 3
        assert persisted.get("max_subagents") == 0

    @pytest.mark.asyncio
    async def test_write_goes_through_atomic_helper(
        self, seeded_config, fake_sel, monkeypatch
    ) -> None:
        """The write path must call ``update_config_locked``, not a bare
        ``write_text`` / ``os.replace``.  Patching the helper to a spy lets us
        assert it was invoked while still allowing the real write to complete."""
        import kiro_crew.config.loader as _loader

        calls: list[str] = []
        _real = _loader.update_config_locked

        def _spy(path=None, *, mutate, **kw):
            calls.append("update_config_locked")
            return _real(path, mutate=mutate, **kw)

        monkeypatch.setattr(_loader, "update_config_locked", _spy)
        async with TestClient(TestServer(_agent_cfg_app())) as client:
            resp = await _put_agent(client, {"subagent_max_turns": 5})
        assert resp.status == 200
        assert calls == [
            "update_config_locked"
        ], "PUT did not route through update_config_locked — lost-write race still present"
        assert (
            json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["subagent_max_turns"]
            == 5
        )


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


class TestFallbackModelPatch:
    """agent.fallback_model — single-value str spec with role-model validation."""

    def _app(self) -> web.Application:
        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", core_mod.api_kirocrew_config_patch)
        return app

    @pytest.mark.asyncio
    async def test_accepts_a_model_id(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "claude-opus-4.8"},
            )
            assert resp.status == 200
        assert (
            json.loads(seeded_config.read_text(encoding="utf-8"))["agent"]["fallback_model"]
            == "claude-opus-4.8"
        )

    @pytest.mark.asyncio
    async def test_accepts_auto(self, seeded_config, fake_sel) -> None:
        # "auto" always allows — _validate_role_model's defer-to-default case.
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "auto"},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_accepts_empty_feature_off(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": ""},
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_rejects_non_string(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": ["claude-opus-5"]},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_bad_grammar(self, seeded_config, fake_sel) -> None:
        async with TestClient(TestServer(self._app())) as client:
            resp = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.fallback_model", "value": "model; rm -rf /"},
            )
            assert resp.status == 400
            assert "invalid value" in (await resp.json())["error"]


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
                    SimpleNamespace(available_models=lambda: [{"modelId": "claude-sonnet-4.6"}]),
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
            lambda _v, provider=None: "display-only key",
        )
        assert core_mod._validate_role_model("fable-5-1m", _req()) == "display-only key"

    def test_unknown_entitlement_does_not_accuse(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v, provider=None: None,
        )
        assert core_mod._validate_role_model("some-model", _req(app={})) is None

    def test_unentitled_model_lists_usable_alternatives(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
            lambda _v, provider=None: None,
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
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: False)
        resp = await core_mod.api_app_token(
            _req(match_info={"name": "meetings"}, headers={"X-App-Secret": "nope"})
        )
        assert resp.status == 403
        assert json.loads(resp.body)["error"] == "invalid secret"

    @pytest.mark.asyncio
    async def test_valid_secret_mints_an_app_scoped_credential(self, app_sel, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: True)
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
    async def test_stream_stops_when_the_client_disconnects(self, monkeypatch, fake_sel) -> None:
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


class TestSessionAgentPathIdValidation:
    """A malformed path id must answer 400, not crash with a 500.

    ``session_workspace`` validates both ids with ``_validate_id``, which RAISES
    ``ValueError``. These three handlers called into it with no ``try``, so an id
    outside ``^[a-zA-Z0-9_.:-]+$`` -- or the literal ``..`` -- escaped as an
    unhandled exception. Every other test in this file monkeypatches
    ``list_results`` / ``read_result`` / ``result_path``, so the real validator
    never ran and the gap stayed invisible; nothing here patches them.

    ``messaging.py`` is the in-tree pattern for the same seam: ``read_state``
    wraps ``_agent_dir`` in ``try/except ValueError`` and answers ``None``.
    """

    #: Rejected by ``_validate_id``: a space is outside the charset, and ``..``
    #: is refused by name. Both survive aiohttp routing -- the default pattern
    #: for ``{id}`` is ``[^{}/]+``, which admits a space and admits ``..``.
    BAD_IDS = ("bad id", "..", "a\\b", "a%b")

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_list_refuses_a_malformed_session_id(self, bad, fake_sel) -> None:
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": bad}))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_result_refuses_a_malformed_session_id(self, bad, fake_sel) -> None:
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": bad, "agent_id": "a1"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    @pytest.mark.parametrize("bad", BAD_IDS)
    @pytest.mark.asyncio
    async def test_result_refuses_a_malformed_agent_id(self, bad, fake_sel) -> None:
        """The second param needs its own code -- ``read_result`` validates the
        session id first, so a caller told only "invalid session id" would go
        fix the wrong half of the URL."""
        resp = await core_mod.api_session_agent_result(
            _req(match_info={"id": "s1", "agent_id": bad})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_agent_id"

    @pytest.mark.asyncio
    async def test_stream_refuses_before_it_prepares_an_sse_response(self, fake_sel) -> None:
        """The refusal has to land while a normal response is still sendable.

        ``result_path`` is called BEFORE ``resp.prepare(request)``, so the guard
        can still answer 400. Once prepared, the status is on the wire and the
        only way to signal a bad id would be an SSE payload no client reads.
        """
        resp = await core_mod.api_session_agent_stream(
            _req(match_info={"id": "bad id", "agent_id": "a1"})
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "invalid_session_id"

    # ── over-fixing guards: these pass on the base and must keep passing ──

    @pytest.mark.parametrize("good", ("s1", "dashboard:slot-3", "chat-735-1788268870", "a.b_c"))
    @pytest.mark.asyncio
    async def test_the_accepted_charset_is_not_narrowed(self, good, monkeypatch, fake_sel) -> None:
        """``_SAFE_ID_RE`` admits ``:``, ``.``, ``_`` and ``-``, and real session
        keys use them (``dashboard:slot-3``). A guard that rejected any of these
        would break working URLs, so pin the accepted set rather than only the
        refused one."""
        monkeypatch.setattr("kiro_crew.session_workspace.list_results", lambda _s: [])
        resp = await core_mod.api_session_agents_list(_req(match_info={"id": good}))
        assert resp.status == 200

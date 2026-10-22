"""MCP discovery: what a probe may spawn, and how.

A consent-disabled server is never spawned or connected; the probe gets a managed temp
dir unless the spec declares one the seal check clears; a sandbox refusal is reported as a
probe limitation, and only the console-script argv the package derives for its own managed
server gets the first-party argv flag; a denied env key is named on the failed probe; and a
spec still holding a ``secret://`` reference is refused before any spawn.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _passthrough_sandbox,
    _stub_remote_probe_ssrf_vet,
)

from kiro_crew import platform_compat
from kiro_crew.mcp_discovery import (
    McpServerInfo,
    _cache_probe,
    _get_cached,
    _note_denied_env,
    _probe_cache,
    probe_server,
)


class TestProbeServerConsentGate:
    """``probe_server`` itself refuses a consent-disabled server.

    Probing is what RUNS the server, so the refusal has to live in the function
    every entry point funnels through — not in each caller's pre-filter, which
    only holds until a new call site forgets it.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_disabled_stdio_server_is_never_spawned(self) -> None:
        """No subprocess for a disabled stdio server, even with a resolvable command.

        ``shutil.which`` is stubbed so the probe cannot bail out early on
        "command not found" — that would make this test pass for the wrong
        reason, without ever proving the consent check ran.
        """
        server = McpServerInfo(name="held", command="true", disabled=True)

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            result = await probe_server(server)

        mock_spawn.assert_not_awaited()
        assert result.status == "disabled"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_disabled_remote_server_is_never_connected(self) -> None:
        """A disabled remote server opens no connection.

        The refusal sits ahead of the local/remote dispatch: probing a remote
        server reaches out over the network, which is equally not-consented.
        """
        server = McpServerInfo(name="held-remote", url="https://example.com/mcp", disabled=True)

        with patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as mock_remote:
            result = await probe_server(server)

        mock_remote.assert_not_awaited()
        assert result.status == "disabled"

    @pytest.mark.asyncio
    async def test_truthy_non_bool_disabled_still_withholds_spawn(self) -> None:
        """A non-bool ``disabled`` fails CLOSED.

        ``McpServerInfo`` is hand-built by callers (``cli_doctor`` does exactly
        that) and the flag can originate in unvalidated config JSON, so the
        check is truthiness rather than ``is True``.
        """
        server = McpServerInfo(name="held-str", command="true")
        server.disabled = "yes"  # type: ignore[assignment]

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            result = await probe_server(server)

        mock_spawn.assert_not_awaited()
        assert result.status == "disabled"

    @pytest.mark.asyncio
    async def test_refusal_does_not_clobber_cached_tools(self) -> None:
        """The refusal must not write to the shared probe cache.

        Guards a specific future refactor rather than the missing guard: adding
        a well-meaning ``_cache_probe(server)`` to the refusal path to "record
        the disabled state". The cache is keyed by name and read by
        ``GET /api/mcp`` through ``_get_cached``, so an empty "disabled" entry
        would erase the tool list a real probe recorded before the user
        disabled the server. Verified by adding that call and watching this
        fail — it does NOT fail merely from removing the guard, because the
        probe's early error returns skip ``_cache_probe`` anyway.
        """
        probed = McpServerInfo(name="was-ok", command="true", status="ok", tools=["alpha", "beta"])
        _cache_probe(probed)

        disabled = McpServerInfo(name="was-ok", command="true", disabled=True)
        with patch("kiro_crew.mcp_discovery.shutil.which", return_value="/bin/true"):
            await probe_server(disabled)

        status, tools, *_rest = _get_cached("was-ok")
        assert status == "ok"
        assert tools == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_refusal_preserves_last_known_tools_and_clears_stale_error(self) -> None:
        """``tools`` survives the refusal; a stale probe ``error`` does not.

        ``list_servers`` merges cached status/tools/error onto every row, so a
        disabled row can arrive carrying both — and a leftover failure message
        is not the reason this call returned.
        """
        server = McpServerInfo(
            name="held-with-history",
            command="true",
            tools=["alpha"],
            error="timeout",
            disabled=True,
        )

        result = await probe_server(server)

        assert result.status == "disabled"
        assert result.tools == ["alpha"]
        assert result.error == ""


#: The sealed-refusal tests below drive the REAL ``classify_declared_temp_path``
#: against a declaration under ``<home>/run``. On Windows that classification
#: returns ``None`` by design and the probe honours the declaration -- see
#: ``test_on_windows_a_declared_temp_is_honored_because_nothing_seals_run`` in
#: ``test_sandbox_argv_launcher_plan.py`` -- so the refusal these tests assert cannot happen
#: there. Skipped rather than run under a patched platform: path semantics
#: differ, and a POSIX-pinned run on Windows would mask a real failure.
_NEEDS_A_SEALING_BACKEND = pytest.mark.skipif(
    sys.platform == "win32",
    reason="nothing seals run/ on Windows, so the probe honours a declared temp there; "
    "pinned by test_on_windows_a_declared_temp_is_honored_because_nothing_seals_run",
)


class TestProbeTempContainment:
    """Probe wiring: spec-declared temp yields; Windows defers cleanup.

    Both tests assert ONLY on the containment calls and tolerate any probe
    outcome -- the probe command is a real interpreter that exits instantly,
    so the handshake fails, which is irrelevant to the temp lifecycle.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_spec_declared_temp_suppresses_probe_containment(self, tmp_path) -> None:
        # Mirror of the backend chokepoint: an operator-declared temp key in
        # the SPEC (any casing -- Windows env keys are case-insensitive)
        # means no allocation at all, so a storage-heavy probe honors the
        # configured volume instead of ENOSPC-ing the data home.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        server = McpServerInfo(
            name="declared-temp",
            command=sys.executable,
            args=["-c", "pass"],
            env={"tmpdir": str(tmp_path / "chosen")},
        )
        alloc_spy = MagicMock(side_effect=AssertionError("must not allocate"))
        with patch.object(bt, "allocate_probe_tmp", alloc_spy):
            await probe_server(server)
        alloc_spy.assert_not_called()

    @_NEEDS_A_SEALING_BACKEND
    @pytest.mark.asyncio
    async def test_sealed_spec_declared_temp_falls_back_to_managed(
        self, tmp_path, monkeypatch
    ) -> None:
        """A declared temp inside ``<data home>/run`` is refused, not honored.

        Both backends seal that parent read-only, so honoring the declaration
        hands the child a TMPDIR it cannot write -- silently, since the probe
        still reports a green handshake. The declaration therefore yields to the
        managed temp (allocated, carved out, and pointed at by the child's env),
        and the sealed path never reaches the child.
        """
        import sys
        from pathlib import Path

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        sealed = home / "run" / "custom-tmp"

        captured_wrap: dict = {}

        def _wrap(argv, *a, env=None, **k):
            captured_wrap.update(k)
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(
            name="sealed-declared-temp",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMPDIR": str(sealed)},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        carve = captured_wrap.get("extra_writable_dirs")
        assert carve is not None and len(carve) == 1
        scratch = Path(carve[0])
        assert scratch.parent.parent == home / "run" / "mcp-tmp"
        captured_env = spawn_mock.call_args.kwargs["env"]
        # Every canonical key points at the managed scratch, so a child reading
        # TMP or TEMP cannot land back on the sealed path either.
        assert captured_env["TMPDIR"] == str(scratch)
        assert captured_env["TMP"] == str(scratch)
        assert captured_env["TEMP"] == str(scratch)
        assert str(sealed) not in captured_env.values()

    @_NEEDS_A_SEALING_BACKEND
    @pytest.mark.asyncio
    async def test_sealed_declared_temp_stripped_when_allocation_fails(
        self, tmp_path, monkeypatch
    ) -> None:
        # Containment is fail-open, but never onto a directory already known to
        # be read-only: with no managed dir to point at, every temp key is
        # stripped (the ambient ones too) so the child falls back to its
        # platform default instead of the sealed path.
        import sys

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        monkeypatch.setattr(bt, "allocate_probe_tmp", MagicMock(side_effect=OSError("disk full")))
        sealed = home / "run" / "custom-tmp"

        def _wrap(argv, *a, env=None, **k):
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(
            name="sealed-alloc-fail",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMPDIR": str(sealed)},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured_env = spawn_mock.call_args.kwargs["env"]
        assert not [key for key in captured_env if key.upper() in ("TMPDIR", "TMP", "TEMP")]

    @_NEEDS_A_SEALING_BACKEND
    @pytest.mark.asyncio
    @pytest.mark.parametrize("declared_key", ["tmpdir", "Temp", "TmP"])
    async def test_sealed_declared_temp_refused_whatever_case_the_spec_spelled(
        self, tmp_path, monkeypatch, declared_key
    ) -> None:
        """A case variant must not survive the refusal.

        Spec env keys are treated case-INSENSITIVELY here on purpose -- Windows
        env keys are case-insensitive and the sanitized spec preserves the
        author's spelling -- so the detection above already sees ``tmpdir`` as a
        declaration. The REWRITE compared exact spellings, so the spec's own
        lowercase key stayed in the env beside the managed triple: on Windows
        those two names are ONE variable, and the child could be handed back the
        very sealed path this refusal exists to withhold.
        """
        import sys

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        sealed = home / "run" / "custom-tmp"

        def _wrap(argv, *a, env=None, **k):
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(
            name="sealed-declared-temp-case",
            command=sys.executable,
            args=["-c", "pass"],
            env={declared_key: str(sealed)},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured_env = spawn_mock.call_args.kwargs["env"]
        # The sealed path is gone in EVERY spelling, and exactly one canonical
        # name survives per key -- no lowercase twin left to shadow the managed
        # triple on a case-insensitive platform.
        assert str(sealed) not in captured_env.values()
        assert declared_key not in captured_env
        temp_keys = sorted(key for key in captured_env if key.upper() in ("TMPDIR", "TMP", "TEMP"))
        assert temp_keys == ["TEMP", "TMP", "TMPDIR"]

    @pytest.mark.asyncio
    async def test_unsealed_declared_temp_is_honored_under_the_canonical_key(
        self, tmp_path, monkeypatch
    ) -> None:
        # The mirror case: a declaration OUTSIDE the seal is still honored, and
        # honored in the key `tempfile` actually reads. POSIX consults only the
        # uppercase TMPDIR/TMP/TEMP, so a spec spelled `tmpdir` must come out as
        # TMPDIR -- keeping the lowercase key while pruning the ambient ones
        # left the child on the platform default with no managed temp either.
        # The ambient siblings are still pruned, since an ambient TMPDIR beside a
        # declared TMP would win the lookup by spelling luck.
        import sys

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        chosen = tmp_path / "capacity-volume" / "tmp"
        chosen.mkdir(parents=True)
        alloc = MagicMock(side_effect=AssertionError("managed temp allocated despite declaration"))
        monkeypatch.setattr(bt, "allocate_probe_tmp", alloc)

        def _wrap(argv, *a, env=None, **k):
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        monkeypatch.setenv("TMPDIR", "/ambient/tmpdir")
        monkeypatch.setenv("TMP", "/ambient/tmp")
        monkeypatch.setenv("TEMP", "/ambient/temp")

        server = McpServerInfo(
            name="declared-temp-lowercase",
            command=sys.executable,
            args=["-c", "pass"],
            env={"tmpdir": str(chosen)},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured_env = spawn_mock.call_args.kwargs["env"]
        assert captured_env["TMPDIR"] == str(chosen)
        # The canonical spelling is the ONLY temp key left: no ambient TMPDIR to
        # win the lookup, and no lowercase twin to shadow it on Windows.
        assert [key for key in captured_env if key.upper() in ("TMPDIR", "TMP", "TEMP")] == [
            "TMPDIR"
        ]

    @pytest.mark.asyncio
    async def test_declared_temp_check_failure_fails_closed_onto_managed_temp(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        # A raise inside the refusal check has cleared nothing, so the
        # declaration is REFUSED, not honored: honoring it is exactly the bug
        # (a possibly sealed temp handed to the child under a green probe)
        # with only a debug line to show for it. The managed temp takes over
        # and the WARNING names the failure.
        import sys
        from pathlib import Path

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        declared = home / "run" / "custom-tmp"
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery.classify_declared_temp_path",
            MagicMock(side_effect=OSError("data home does not resolve to a real directory")),
        )
        captured_wrap: dict = {}

        def _wrap(argv, *a, env=None, **k):
            captured_wrap.update(k)
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(
            name="check-raises",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMPDIR": str(declared)},
        )
        with caplog.at_level(logging.WARNING):
            with patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
                side_effect=OSError("stop after env capture"),
            ) as spawn_mock:
                await probe_server(server)

        carve = captured_wrap.get("extra_writable_dirs")
        assert carve is not None and len(carve) == 1
        scratch = Path(carve[0])
        assert scratch.parent.parent == home / "run" / "mcp-tmp"
        captured_env = spawn_mock.call_args.kwargs["env"]
        assert captured_env["TMPDIR"] == str(scratch)
        assert str(declared) not in captured_env.values()
        warnings = [r for r in caplog.records if "ignoring spec-declared" in r.getMessage()]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "seal check itself failed" in text
        assert "OSError: data home does not resolve to a real directory" in text
        assert "sandbox-sealed runtime parent" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cause, phrase",
        [
            ("sealed", "inside the sandbox-sealed runtime parent"),
            ("unclassifiable", "canonical form cannot be established"),
        ],
    )
    async def test_refusal_warning_names_the_actual_cause(
        self, tmp_path, monkeypatch, caplog, cause, phrase
    ) -> None:
        # Only one of the two refusals is about the seal. Telling an operator
        # that a symlink cycle "is inside the sealed runtime parent" sends them
        # looking in the wrong place.
        import sys

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery.classify_declared_temp_path", MagicMock(return_value=cause)
        )

        def _wrap(argv, *a, env=None, **k):
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        server = McpServerInfo(
            name="refused-" + cause,
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMPDIR": "//declared/tmp"},
        )
        with caplog.at_level(logging.WARNING):
            with patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
                side_effect=OSError("stop after env capture"),
            ) as spawn_mock:
                await probe_server(server)

        assert "//declared/tmp" not in spawn_mock.call_args.kwargs["env"].values()
        texts = [
            r.getMessage() for r in caplog.records if "ignoring spec-declared" in r.getMessage()
        ]
        assert len(texts) == 1 and "TMPDIR='//declared/tmp'" in texts[0]
        assert phrase in texts[0]
        if cause != "sealed":
            assert "sandbox-sealed" not in texts[0]

    @pytest.mark.asyncio
    async def test_refusal_warning_escapes_newlines_in_the_declared_path(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        import sys

        from kiro_crew import sandbox
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery.classify_declared_temp_path",
            MagicMock(return_value="sealed"),
        )

        def _wrap(argv, *a, env=None, **k):
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        declared = "/declared/tmp\nFORGED"
        server = McpServerInfo(
            name="newline-refusal",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMPDIR": declared},
        )
        with caplog.at_level(logging.WARNING):
            with patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
                side_effect=OSError("stop after env capture"),
            ):
                await probe_server(server)

        warning = next(
            record.getMessage()
            for record in caplog.records
            if "ignoring spec-declared" in record.getMessage()
        )
        assert "\\nFORGED" in warning
        assert "\nFORGED" not in warning

    @pytest.mark.asyncio
    async def test_windows_probe_cleanup_defers_to_daemon_sweep(
        self, tmp_path, monkeypatch
    ) -> None:
        # On Windows tree death is unprovable (taskkill /T loses orphaned
        # children), so the probe's finally must NOT delete -- the daemon's
        # dual-condition sweep is the single deletion authority there. The
        # probe command exits on its own, so skipping the POSIX reap branch
        # (a side effect of patching IS_POSIX) leaks nothing.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        # The IS_POSIX patch also flips restrict_dir_to_owner onto its Windows
        # DACL branch, which cannot run on the POSIX host executing this test --
        # shim it to POSIX behavior so ALLOCATION survives and the test
        # exercises the logic it targets.
        monkeypatch.setattr(
            platform_compat,
            "restrict_dir_to_owner",
            # 0o700 is the RESTRICTIVE mode for a directory (see the identical
            # suppression in platform_compat.restrict_dir_to_owner itself).
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
            lambda p: os.chmod(p, 0o700),
        )

        # Pre-seed a PRIOR probe's dead+idle dir: the deferral path must run
        # the root-wide dual-condition sweep from THIS (gateway) process, so
        # reclamation exists even in a topology that never starts the mcp-tmp
        # daemon (no stub servers). Owner pid 2**22+5 is far above any live
        # pid on CI runners; mtimes aged beyond the grace window.
        stale = bt.backend_tmp_root() / "probe-deadbeef-old"
        stale.mkdir(parents=True)
        (stale / bt.OWNER_FILENAME).write_text(str(2**22 + 5), encoding="utf-8")
        # flake-ok: pre-existing wall-clock read aging the stale dir two hours back; moved verbatim
        old = time.time() - 7200
        os.utime(stale / bt.OWNER_FILENAME, (old, old))
        os.utime(stale, (old, old))
        os.utime(bt.backend_tmp_root(), (old, old))

        server = McpServerInfo(name="win-probe", command=sys.executable, args=["-c", "pass"])
        await probe_server(server)

        # The prior dead dir was reclaimed by the probe-side sweep...
        assert not stale.exists()
        # ...while the fresh probe's own dir (seconds-old tree) was kept.
        root = home / "run" / "mcp-tmp"
        assert root.exists() and any(root.iterdir())
        owners = [
            int((child / bt.OWNER_FILENAME).read_text(encoding="utf-8").strip())
            for child in root.iterdir()
            if (child / bt.OWNER_FILENAME).is_file()
        ]
        assert owners and all(pid != os.getpid() for pid in owners)

    @pytest.mark.asyncio
    async def test_probe_spawn_failure_reclaims_the_fresh_dir(self, tmp_path, monkeypatch) -> None:
        # A dir whose probe never existed has no dead-owner future: the sweeps
        # never delete provisional/ownerless dirs, so the spawn-failure path
        # is its only reclamation point (mirrors spawn_backend). Exercised on
        # the WINDOWS path (IS_POSIX=False) where the finally-sweep defers to
        # the daemon -- there the except-block reclamation is the ONLY one;
        # on POSIX the finally would double-cover it and mask a regression.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
        # The IS_POSIX patch also flips restrict_dir_to_owner onto its Windows
        # DACL branch, which cannot run on the POSIX host executing this test --
        # shim it to POSIX behavior so ALLOCATION survives and the test
        # exercises the logic it targets.
        monkeypatch.setattr(
            platform_compat,
            "restrict_dir_to_owner",
            # 0o700 is the RESTRICTIVE mode for a directory (see the identical
            # suppression in platform_compat.restrict_dir_to_owner itself).
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
            lambda p: os.chmod(p, 0o700),
        )

        server = McpServerInfo(name="spawnfail", command=sys.executable, args=["-c", "pass"])
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("spawn failed"),
        ):
            result = await probe_server(server)

        assert result.status == "error"
        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())

    @pytest.mark.asyncio
    async def test_partial_spec_declaration_strips_ambient_tmpdir(
        self, tmp_path, monkeypatch
    ) -> None:
        # Spec declares only TMP: the probe env must not keep the ambient
        # TMPDIR (tempfile consults it first), and no managed dir may be
        # allocated. Mirrors the backend chokepoint's pruning.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setenv("TMPDIR", str(tmp_path / "ambient"))

        server = McpServerInfo(
            name="partial-temp",
            command=sys.executable,
            args=["-c", "pass"],
            env={"TMP": str(tmp_path / "chosen")},
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured = spawn_mock.call_args.kwargs["env"]
        assert "TMPDIR" not in captured
        assert captured["TMP"] == str(tmp_path / "chosen")
        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())

    @pytest.mark.asyncio
    async def test_probe_tmp_allocated_before_wrap_and_carved_out(
        self, tmp_path, monkeypatch
    ) -> None:
        """The probe TMPDIR is allocated BEFORE the sandbox wrap, which
        receives it as a write carve-out, and the spawn env points at it.

        The managed probe root lives at ``<data home>/run/mcp-tmp``, inside the
        runtime parent the sandbox seals read-only. A TMPDIR allocated after
        the wrap is a directory the sandboxed child cannot write -- a
        Bun-packaged server then fails the probe with "Cannot find the native
        Koffi module" because it cannot extract its native module. Lock BOTH
        halves of the fix: the ordering (alloc, then wrap) and the carve-out
        kwarg naming the allocated dir.
        """
        import sys
        from pathlib import Path

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        calls: list[str] = []
        real_alloc = bt.allocate_probe_tmp

        def alloc_spy():
            calls.append("alloc")
            return real_alloc()

        monkeypatch.setattr(bt, "allocate_probe_tmp", alloc_spy)

        captured_wrap: dict = {}

        def _wrap(argv, *a, env=None, **k):
            calls.append("wrap")
            captured_wrap.update(k)
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(name="order-lock", command=sys.executable, args=["-c", "pass"])
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        assert calls == ["alloc", "wrap"]
        carve = captured_wrap.get("extra_writable_dirs")
        assert carve is not None and len(carve) == 1
        scratch = Path(carve[0])
        # The carve-out is the child-facing SCRATCH SUBDIR of the allocation,
        # never the allocation root: the root holds the ``.owner`` reclamation
        # record, which must stay OUTSIDE the child's writable window (a
        # garbled ``.owner`` makes the dir unreclaimable by the daemon sweep).
        assert scratch.name == bt.PROBE_SCRATCH_SUBDIR
        allocated = scratch.parent
        assert allocated.parent == home / "run" / "mcp-tmp"
        owner = allocated / bt.OWNER_FILENAME
        assert not str(owner).startswith(str(scratch) + os.sep)
        # The managed triple lands on the env the child actually receives,
        # pointing at the SAME dir the wrap carved out.
        captured_env = spawn_mock.call_args.kwargs["env"]
        assert captured_env["TMPDIR"] == str(scratch)

    @pytest.mark.asyncio
    async def test_probe_alloc_failure_wraps_without_carveout(self, tmp_path, monkeypatch) -> None:
        # Containment is fail-open hygiene: when allocation fails, the probe
        # must still run -- wrapped, with inherited temp and no carve-out.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(
            bt,
            "allocate_probe_tmp",
            MagicMock(side_effect=OSError("disk full")),
        )

        captured_wrap: dict = {}

        def _wrap(argv, *a, env=None, **k):
            captured_wrap.update(k)
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)

        server = McpServerInfo(name="alloc-fail", command=sys.executable, args=["-c", "pass"])
        result = await probe_server(server)

        # The wrap ran (kwargs captured) and received no carve-out.
        assert "extra_writable_dirs" not in captured_wrap
        # And the probe was not diverted into an error about containment --
        # whatever the handshake outcome, allocation failure is not the error.
        assert "disk full" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_probe_drops_reserved_kirocrew_namespace_from_spec_env(
        self, tmp_path, monkeypatch
    ) -> None:
        """SECURITY: the probe is the SIBLING site of the cron tool bridge.

        Both apply a config-declared ``env`` to a child they spawn themselves, but
        only ``cron_script`` had a ``KIROCREW_*`` deny (``_CRON_ENV_DENY``, via
        ``KIROCREW_OWNER_ID``) -- the probe applied none. Putting the
        reserved-namespace deny in the shared sanitizer rather than in the cron
        deny-set is what covers this path too, so this test is the reason for that
        placement. A gateway-authored value must still be INHERITED: the probe
        builds its env from ``dict(os.environ)`` and only the OVERRIDE is refused.
        """
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "real-home"))
        monkeypatch.delenv("KIROCREW_CLI", raising=False)

        server = McpServerInfo(
            name="identity-forger",
            command=sys.executable,
            args=["-c", "pass"],
            env={
                "KIROCREW_CLI": "1",
                "KIROCREW_SESSION_KEY": "some-other-session",
                "KIROCREW_HOME": str(tmp_path / "attacker-home"),
                "MCP_TOKEN": "keep-me",
            },
        )
        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after env capture"),
        ) as spawn_mock:
            await probe_server(server)

        captured = spawn_mock.call_args.kwargs["env"]
        assert "KIROCREW_CLI" not in captured
        assert "KIROCREW_SESSION_KEY" not in captured
        # Inherited value survives; the spec's override of it does not.
        assert captured["KIROCREW_HOME"] == str(tmp_path / "real-home")
        assert captured["MCP_TOKEN"] == "keep-me"

    @pytest.mark.skipif(
        not platform_compat.IS_POSIX,
        reason="POSIX-only control: on Windows the finally-path deferral to the "
        "daemon sweep is the designed behavior (see the deferral test above)",
    )
    @pytest.mark.asyncio
    async def test_posix_probe_cleanup_sweeps_its_own_dir(self, tmp_path, monkeypatch) -> None:
        # Control for the Windows deferral: on POSIX the group reap is
        # tree-faithful, so the probe's finally DOES reclaim its private dir.
        import sys

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        server = McpServerInfo(name="posix-probe", command=sys.executable, args=["-c", "pass"])
        await probe_server(server)

        root = home / "run" / "mcp-tmp"
        assert not root.exists() or not any(root.iterdir())


class TestProbeSandboxUnavailable:
    """A probe that could not RUN must not be reported as a broken server.

    kiro-cli launches MCP servers from the agent config without going through
    this probe, so on a host with no sandbox backend (any Windows host, macOS
    >= 26) the servers work while the probe cannot spawn them. Reporting that as
    an ordinary server fault renders every row red with "0 tools" and sends the
    user debugging a server that is fine.
    """

    @pytest.mark.asyncio
    async def test_sandbox_refusal_is_reported_as_a_probe_limitation(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr(md, "_probe_sandbox_warned", set())

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError(
                "Sandbox backend unavailable and allow_unsandboxed_exec is not set.",
                kind="no_backend",
                detail="not Linux",
            )

        # A THIRD-PARTY server: managed ones never reach the spawn path at all
        # (their tools are read in-process), so they cannot exercise this branch.
        server = McpServerInfo(name="playwright-mcp", command="node")
        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"),
        ):
            result = await probe_server(server)

        # Machine-readable prefix so a presentation layer can tell this apart from
        # a genuine handshake failure without parsing prose.
        assert result.error.startswith("mcp_probe_sandbox_unavailable:"), result.error
        assert "server itself may be fine" in result.error, result.error
        assert "sandbox_allow_unsandboxed_exec" in result.error, result.error

    @pytest.mark.asyncio
    async def test_a_managed_server_is_still_spawned_when_the_sandbox_works(self) -> None:
        """The spawn is the only thing that proves the server can START.

        `_fix_stale_managed_command` exists because the managed invocation does go
        stale ("command not found: kirocrew; the built-in cron/core tools then never
        load"), and the probe was the one surface that caught it. Short-circuiting
        on the server name would report `ok` for a managed server that cannot run —
        silently changing what `ok` means in the shared `_cache_probe` store.
        """
        spawned: dict[str, bool] = {}

        def _wrap(argv, **kwargs):
            spawned["yes"] = True
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(name="kirocrew-core", command="kirocrew", args=["mcp-core"])
        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/kirocrew"),
        ):
            await probe_server(server)

        assert spawned.get("yes") is True, "a working sandbox must still be used"

    @pytest.mark.asyncio
    async def test_a_managed_server_falls_back_to_its_declaration_with_no_backend(
        self, monkeypatch
    ) -> None:
        """No backend: serve the declared list rather than an error.

        This is what removes the opt-in for a read-only listing. The import runs
        package code in the gateway process, which is only acceptable BECAUSE the
        sandbox could not confine anything on this host anyway — hence fallback,
        never primary.
        """
        import kiro_crew.mcp_discovery as md
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr(md, "_managed_in_process_warned", set())

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        for name, expect_tools in (("kirocrew-core", True), ("kirocrew-cron", True)):
            server = McpServerInfo(name=name, command="kirocrew", args=["mcp-x"])
            with (
                patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse),
                patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/kirocrew"),
            ):
                result = await probe_server(server)

            assert result.status == "ok", (name, result.error)
            assert bool(result.tools) is expect_tools, (name, len(result.tools))

    @pytest.mark.asyncio
    async def test_a_third_party_server_gets_no_declaration_fallback(self) -> None:
        """Only OUR OWN servers have a declaration to read; a third-party one keeps
        the honest probe-limitation error."""
        from kiro_crew.sandbox import SandboxUnavailableError

        def _refuse(*args, **kwargs):
            raise SandboxUnavailableError("no backend", kind="no_backend", detail="not Linux")

        server = McpServerInfo(name="playwright-mcp", command="node")
        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _refuse),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"),
        ):
            result = await probe_server(server)

        assert result.status == "error"
        assert result.error.startswith("mcp_probe_sandbox_unavailable:"), result.error

    @pytest.mark.asyncio
    async def test_the_remedy_paragraph_is_logged_once_per_server(
        self, monkeypatch, caplog
    ) -> None:
        """The cause is the HOST, so it recurs every cycle for every server.

        Unbounded, a four-server config logged four identical multi-line remedy
        paragraphs per discovery cycle, forever.
        """
        import logging

        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(md, "_probe_sandbox_warned", set())
        with caplog.at_level(logging.WARNING, logger=md.logger.name):
            md._warn_probe_sandbox_unavailable_once("kirocrew-core")
            md._warn_probe_sandbox_unavailable_once("kirocrew-core")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert "probe skipped" in warnings[0].getMessage()


class TestFirstPartyManagedArgv:
    """The probe passes ``first_party_fixed_argv`` ONLY for a self-derived argv.

    The flag buys an unconfined spawn on a backend-less host, so it must key on
    the INVOCATION this package derives for its
    own managed servers — never on the server name alone, which an mcp.json
    scope could pair with user-config command text.
    """

    # An EXISTING file: the cache evicts an invocation whose command is gone
    # (a pruned install), so a made-up path would never be served from it.
    _INVOCATION = (sys.executable, ["mcp-core"])

    def _patch_invocation(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(md, "_resolved_managed_invocation", {"kirocrew-core": self._INVOCATION})
        # Default install: the package-derived managed env is empty.
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {})

    def test_self_derived_managed_argv_is_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    def test_customized_command_under_a_managed_name_is_not(self, monkeypatch) -> None:
        """A managed NAME with user-config command text (the mcp.json-sourced
        case, which ``_fix_stale_managed_command`` never re-resolves) must keep
        the full fail-close + opt-in behavior."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", "/home/user/evil-shim", list(self._INVOCATION[1]), {}
        )
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], ["mcp-core", "--extra"], {}
        )

    def test_spec_env_under_a_managed_name_is_not_first_party(self, monkeypatch) -> None:
        """Env is an execution vector for the SAME argv (``LD_PRELOAD`` decides
        what code runs), and ``probe_server`` merges the spec's env into the
        child environment — so any key this package did not derive disqualifies
        the spec from the unconfined carve-out."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core",
            self._INVOCATION[0],
            list(self._INVOCATION[1]),
            {"LD_PRELOAD": "/tmp/evil.so"},
        )

    def test_the_package_derived_home_pin_still_matches(self, monkeypatch) -> None:
        """Under an override home the managed spec legitimately carries exactly
        the ``KIROCREW_HOME`` pin this package derived — that must still count
        as first-party, and any EXTRA key alongside it must not."""
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        pin = {"KIROCREW_HOME": "/data/override-home"}
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: dict(pin))
        assert md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), dict(pin)
        )
        assert not md._is_first_party_managed_argv(
            "kirocrew-core",
            self._INVOCATION[0],
            list(self._INVOCATION[1]),
            {**pin, "LD_PRELOAD": "/tmp/evil.so"},
        )
        # A spec MISSING the derived pin is also not the derived invocation.
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    def test_the_interpreter_fallback_is_never_first_party(self, monkeypatch) -> None:
        """`python -s -m kiro_crew` still prepends the child's CWD to sys.path
        (3.10 has no -P), so a planted `kiro_crew/` tree in an untrusted cwd
        would shadow the install — only a resolved console-script binary
        qualifies."""
        import sys

        import kiro_crew.mcp_discovery as md

        fallback = (sys.executable, ["-s", "-m", "kiro_crew", "mcp-core"])
        monkeypatch.setattr(md, "_resolved_managed_invocation", {"kirocrew-core": fallback})
        monkeypatch.setattr("kiro_crew.agent._managed_mcp_env", lambda: {})
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", fallback[0], list(fallback[1]), {}
        )

    def test_third_party_server_is_never_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        self._patch_invocation(monkeypatch)
        assert not md._is_first_party_managed_argv("playwright-mcp", "node", [], {})

    def test_resolution_failure_fails_toward_not_first_party(self, monkeypatch) -> None:
        import kiro_crew.mcp_discovery as md

        monkeypatch.setattr(md, "_resolved_managed_invocation", {})

        def _boom(subcommand):
            raise RuntimeError("no install")

        monkeypatch.setattr("kiro_crew.agent._kirocrew_mcp_invocation", _boom)
        assert not md._is_first_party_managed_argv(
            "kirocrew-core", self._INVOCATION[0], list(self._INVOCATION[1]), {}
        )

    @pytest.mark.asyncio
    async def test_probe_passes_the_flag_for_a_self_derived_managed_server(
        self, monkeypatch
    ) -> None:
        self._patch_invocation(monkeypatch)
        seen: dict[str, bool] = {}

        def _capture(argv, **kwargs):
            seen["flag"] = kwargs.get("first_party_fixed_argv", False)
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(
            name="kirocrew-core", command=self._INVOCATION[0], args=list(self._INVOCATION[1])
        )
        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _capture),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value=self._INVOCATION[0]),
        ):
            await probe_server(server)

        assert seen["flag"] is True

    @pytest.mark.asyncio
    async def test_probe_passes_false_for_a_third_party_server(self, monkeypatch) -> None:
        self._patch_invocation(monkeypatch)
        seen: dict[str, bool] = {}

        def _capture(argv, **kwargs):
            # Omitting the synchronous API's default is equivalent to passing
            # False and keeps narrow injected preparation seams compatible.
            seen["flag"] = kwargs.get("first_party_fixed_argv", False)
            raise RuntimeError("stop at the wrap")

        server = McpServerInfo(name="playwright-mcp", command="node")
        with (
            patch("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _capture),
            patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/node"),
        ):
            await probe_server(server)

        assert seen["flag"] is False


class TestNoteDeniedEnv:
    """A red badge caused by policy must say so on the badge's own surface."""

    def _srv(self, **kw):
        s = McpServerInfo(name="py-srv", command="server", args=[])
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_failed_probe_names_the_dropped_key(self) -> None:
        s = self._srv(status="error", error="exec failed", env={"PYTHONPATH": "/srv/lib"})
        _note_denied_env(s)
        assert "PYTHONPATH" in s.error
        assert "exec failed" in s.error, "the original cause must survive"
        assert "a session still does" in s.error, "must say where it DOES work"

    def test_successful_probe_is_left_alone(self) -> None:
        """The drop changed nothing worth reporting when the server came up."""
        s = self._srv(status="ok", error=None, env={"PYTHONPATH": "/srv/lib"})
        _note_denied_env(s)
        assert s.error is None

    def test_failure_without_denied_keys_is_unchanged(self) -> None:
        s = self._srv(status="error", error="command not found: server", env={"TOKEN": "t"})
        _note_denied_env(s)
        assert s.error == "command not found: server"

    def test_missing_env_does_not_raise(self) -> None:
        s = self._srv(status="error", error="boom", env=None)
        _note_denied_env(s)
        assert s.error == "boom"


class TestProbeRefusesSecretReferences:
    """A direct probe runs outside the gateway, so it must not spawn a server
    whose env still holds an unresolved ``secret://`` reference."""

    @pytest.mark.asyncio
    async def test_probe_refuses_before_any_spawn(self, monkeypatch) -> None:
        def _no_spawn(*_a, **_kw):
            raise AssertionError("probe spawned a server holding a secret:// reference")

        monkeypatch.setattr("kiro_crew.mcp_discovery._probe_on_private_loop", _no_spawn)
        server = McpServerInfo(
            name="srv", command="srv-mcp", env={"API_TOKEN": "secret://NAME", "MODE": "x"}
        )
        out = await probe_server(server)
        # No handshake was attempted, so the status carries no probe verdict.
        from kiro_crew.mcp_quarantine import FAILING_STATUSES

        assert out.status not in FAILING_STATUSES and out.status != "ok"
        # The dashboard shows an ``outdated`` row's error as its not-probed
        # reason, so this refusal is pinned to exactly that shape.
        assert out.status == "outdated"
        assert "API_TOKEN" in out.error
        assert "NAME" not in out.error.replace("API_TOKEN", "")

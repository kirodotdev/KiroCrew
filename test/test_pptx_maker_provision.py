"""Tests for engine provisioning (`backend/provision.py`).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

The engine is third-party code fetched at runtime and then BUILT and EXECUTED
(`uv sync` compiles wheels; the app's agents drive the engine), so what decides
which bytes run is the security story of this module. That pin now lives in
``engine_source`` (a sha256 over the received archive) and is tested in
``test_pptx_maker_engine_source.py``; what is pinned HERE is the `uv` resolver,
the sandboxed spawn, and provisioning's failure ladder.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import provision


def _json_strings(node: object) -> list[str]:
    """Every string value anywhere in a parsed JSON document.

    Assertions about a rendered path go through this rather than searching the
    raw file text: the substituted value is JSON-escaped, so on Windows the file
    holds `C:\\\\Users\\\\…` while the parsed value holds `C:\\Users\\…`.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for value in node.values() for s in _json_strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _json_strings(item)]
    return []


def _declared_placeholders() -> list[str]:
    """Every `PLACEHOLDER_*` the provision module declares.

    Derived rather than listed: the hand-maintained tuple this replaced fell out of
    sync the moment a fourth placeholder was added to the templates.
    """
    return [
        value
        for name, value in vars(provision).items()
        if name.startswith("PLACEHOLDER_") and isinstance(value, str)
    ]


class TestEnginePin:
    def test_the_pinned_commit_is_a_full_sha(self):
        """A short sha is ambiguous and an abbreviation can become non-unique."""
        assert len(provision.ENGINE_COMMIT) == 40
        assert all(c in "0123456789abcdef" for c in provision.ENGINE_COMMIT)

    def test_the_pin_is_re_exported_from_the_verifying_module(self):
        """One pin, one owner: `engine_source` verifies it, so `provision` must
        not carry a second copy that could drift out of step with the digest."""
        from kiro_crew.apps.builtins.pptx_maker.backend import engine_source

        assert provision.ENGINE_COMMIT == engine_source.ENGINE_COMMIT
        assert provision.ENGINE_TAG == engine_source.ENGINE_TAG
        assert provision.ENGINE_REPO == engine_source.ENGINE_REPO

    def test_the_reported_tag_comes_from_the_verified_marker(self, tmp_path: Path):
        """An unverified tree must not be reported as the pinned version."""
        assert provision._current_tag(tmp_path / "absent") == "unknown"

    def test_the_engine_fetch_needs_no_external_binary(self, tmp_path: Path):
        """The whole point of dropping `git clone`: FETCHING the engine spawns no
        external tool, so a host with no `git` can still provision.

        The post-swap editable re-link is stubbed out here rather than allowed to run:
        this test is about the fetch, and `uv` is a declared Python dependency, so
        spawning it is not the "external binary" this guards against. The re-link's own
        behaviour is pinned by `TestEditableInstallSurvivesTheSwap` below.
        """
        log: list[str] = []
        with (
            mock.patch.object(
                provision.engine_source, "install_engine", return_value=True
            ) as install,
            mock.patch.object(provision, "_relink_editable_skill", return_value=True),
            mock.patch.object(provision, "_run") as run,
        ):
            assert provision._ensure_engine(tmp_path, log, "/opt/uv") is True
        assert install.called
        assert not run.called, "the engine fetch must not spawn a subprocess"


class TestEditableInstallSurvivesTheSwap:
    """An `--editable` install records an ABSOLUTE path.

    The venv is deliberately built in the STAGING tree so a broken build never replaces
    a working engine — but the `.pth` that validation writes then names the staging
    directory, which the swap renames and deletes. Every `sdpm` import therefore failed
    with ModuleNotFoundError while provisioning reported SUCCESS.

    Reproduced against real `uv`: after `mv staged engine`, the `.pth` still read
    `/private/tmp/.../staged/skill/src` and the import failed; re-running the editable
    step at the final path rewrote it to `.../engine/skill/src` and the import resolved.

    It is passed as `install_engine`'s `finalize` hook rather than called after
    `install_engine` returns, because the swap deletes the retired tree as soon as the
    new one is in place — relinking afterwards had no rollback state left, so a failure
    there deleted the working engine AND left the new one pointing at a removed path.
    `engine_source`'s own `TestSwapRollback` pins the rollback; these pin the wiring.
    """

    def test_the_relink_is_passed_as_the_finalize_hook(self, tmp_path: Path):
        """Both callbacks matter, and WHICH one they are matters: `validate` gets the
        staging tree, `finalize` gets the final path inside the rollback window."""
        captured: dict[str, object] = {}

        def _fake_install(root, log, validate=None, finalize=None):  # noqa: ANN001
            captured["root"] = root
            captured["validate"] = validate
            captured["finalize"] = finalize
            return True

        with (
            mock.patch.object(
                provision.engine_source, "install_engine", side_effect=_fake_install
            ),
            mock.patch.object(provision, "_relink_editable_skill", return_value=True) as relink,
        ):
            assert provision._ensure_engine(tmp_path, [], "/opt/uv") is True

            assert captured["finalize"] is not None, "the relink is not wired as `finalize`"
            assert captured["validate"] is not None, "the staged venv build is not validated"

            # Invoked INSIDE the patch, or the real relink runs and shells out to `uv`.
            # The hook must relink at whatever path the swap hands it, not a captured one.
            final = tmp_path / "somewhere-else"
            assert captured["finalize"](final) is True  # type: ignore[operator]
            assert relink.call_args.args[0] == final

    def test_a_failed_relink_fails_the_provision(self, tmp_path: Path):
        """Reporting success with an unimportable engine is the bug this whole finding
        is about, so the re-link's result must not be discarded — `install_engine`
        returns False when its `finalize` fails."""
        def _fake_install(root, log, validate=None, finalize=None):  # noqa: ANN001
            # Mirrors the real contract: a failing `finalize` fails the install.
            return bool(finalize is None or finalize(root))

        with (
            mock.patch.object(
                provision.engine_source, "install_engine", side_effect=_fake_install
            ),
            mock.patch.object(provision, "_relink_editable_skill", return_value=False),
        ):
            assert provision._ensure_engine(tmp_path, [], "/opt/uv") is False

    def test_the_relink_targets_the_given_root_not_a_staging_path(self, tmp_path: Path):
        """The argv must name `engine_root`, since being pointed at staging is exactly
        what produced the stale `.pth`."""
        calls: list[list[str]] = []

        def _capture(argv, **kwargs):  # noqa: ANN001, ANN202
            calls.append(list(argv))
            return 0, ""

        with mock.patch.object(provision, "_run", side_effect=_capture):
            assert provision._relink_editable_skill(tmp_path, [], "/opt/uv") is True

        argv = calls[0]
        assert "--editable" in argv
        assert argv[argv.index("--editable") + 1] == str(tmp_path / "skill")
        # And the interpreter it installs INTO is the tree's own venv.
        assert str(tmp_path / "mcp-local" / ".venv") in argv[argv.index("--python") + 1]


class TestResolveUv:
    """`uv` ships as a declared Python dependency, so it is always INSTALLED —
    but not necessarily on PATH (a wheel puts it in the venv's scripts dir; an
    installed launchd/systemd service runs with a minimal PATH; the frozen DMG
    bundle has no scripts dir at all). The resolver is what closes that gap."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        provision.reset_uv_cache()
        yield
        provision.reset_uv_cache()

    def test_prefers_the_installed_package_locator(self, tmp_path: Path):
        """The normal `pip install` case: ask the uv wheel where its binary is
        rather than hoping the venv's scripts dir is on PATH."""
        packaged = tmp_path / "site-packages-uv"
        packaged.write_text("#!/bin/sh", encoding="utf-8")
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(return_value=str(packaged)))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(provision.shutil, "which") as which,
        ):
            assert provision.resolve_uv() == str(packaged)
        assert not which.called, "PATH must not be consulted when the package resolves"

    def test_falls_back_to_path_when_the_locator_raises(self, tmp_path: Path):
        """`find_uv_bin` raises UvNotFound (a FileNotFoundError) on an odd
        repackaging — a user's own newer uv must still work."""
        fake_uv = mock.Mock(
            find_uv_bin=mock.Mock(side_effect=FileNotFoundError("no uv in any location"))
        )
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(provision.shutil, "which", return_value="/usr/local/bin/uv"),
        ):
            assert provision.resolve_uv() == "/usr/local/bin/uv"

    def test_falls_back_to_path_when_the_package_is_absent(self):
        """An install without the uv wheel at all must not raise ImportError."""
        with (
            mock.patch.dict(sys.modules, {"uv": None}),
            mock.patch.object(provision.shutil, "which", return_value="/usr/bin/uv"),
        ):
            assert provision.resolve_uv() == "/usr/bin/uv"

    def test_a_locator_pointing_at_a_missing_file_is_not_trusted(self, tmp_path: Path):
        """A path that does not exist is not a usable uv — fall through rather
        than hand an absolute nonexistent path to `subprocess.run`."""
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(return_value=str(tmp_path / "gone")))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(provision.shutil, "which", return_value="/usr/bin/uv"),
        ):
            assert provision.resolve_uv() == "/usr/bin/uv"

    def test_finds_the_binary_staged_next_to_a_frozen_executable(self, tmp_path: Path):
        """The DMG path. PyInstaller's bundle has no scripts dir, so
        `find_uv_bin()` raises there; the spec stages uv at the bundle root."""
        bundled = tmp_path / ("uv" + (provision.sysconfig.get_config_var("EXE") or ""))
        bundled.write_text("#!/bin/sh", encoding="utf-8")
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(side_effect=FileNotFoundError("frozen")))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(tmp_path), create=True),
            mock.patch.object(provision.shutil, "which") as which,
        ):
            assert provision.resolve_uv() == str(bundled)
        assert not which.called, "the bundled binary must win over PATH"

    def test_finds_the_binary_beside_sys_executable_in_a_one_folder_build(self, tmp_path: Path):
        """`_MEIPASS` and `dirname(sys.executable)` differ for a one-FILE build,
        so both are probed."""
        bundled = tmp_path / ("uv" + (provision.sysconfig.get_config_var("EXE") or ""))
        bundled.write_text("#!/bin/sh", encoding="utf-8")
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(side_effect=FileNotFoundError("frozen")))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(tmp_path / "kirocrew-backend")),
            mock.patch.object(provision.shutil, "which"),
        ):
            assert provision.resolve_uv() == str(bundled)

    def test_falls_through_to_path_when_the_frozen_location_is_empty(self, tmp_path: Path):
        """A frozen build whose bundle did NOT stage uv still uses a system one."""
        empty = tmp_path / "bundle"
        empty.mkdir()
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(side_effect=FileNotFoundError("frozen")))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(empty), create=True),
            mock.patch.object(
                # Both frozen candidates must be empty, or the interpreter's own
                # scripts dir (which really does hold a uv here) satisfies the probe.
                sys,
                "executable",
                str(empty / "kirocrew-backend"),
            ),
            mock.patch.object(provision.shutil, "which", return_value="/opt/homebrew/bin/uv"),
        ):
            assert provision.resolve_uv() == "/opt/homebrew/bin/uv"

    def test_the_frozen_location_is_ignored_when_not_frozen(self, tmp_path: Path):
        """A stray `uv` next to a normal interpreter must not be picked up as if
        it were a bundled one — only `sys.frozen` opens that door."""
        assert provision._frozen_bundle_dirs() == []

    def test_returns_none_when_uv_is_absent_everywhere_and_never_raises(self):
        """The contract `provision()` depends on: an absent uv is a reportable
        condition, not a traceback inside a detached background job."""
        fake_uv = mock.Mock(find_uv_bin=mock.Mock(side_effect=FileNotFoundError("nope")))
        with (
            mock.patch.dict(sys.modules, {"uv": fake_uv}),
            mock.patch.object(provision.shutil, "which", return_value=None),
        ):
            assert provision.resolve_uv() is None

    def test_the_resolved_path_is_cached(self, tmp_path: Path):
        """Called on every provision, and the answer cannot change in-process."""
        packaged = tmp_path / "uv"
        packaged.write_text("#!/bin/sh", encoding="utf-8")
        locator = mock.Mock(return_value=str(packaged))
        with mock.patch.dict(sys.modules, {"uv": mock.Mock(find_uv_bin=locator)}):
            assert provision.resolve_uv() == str(packaged)
            assert provision.resolve_uv() == str(packaged)
        assert locator.call_count == 1

    def test_the_real_installed_uv_resolves_to_an_absolute_path(self):
        """Against the ACTUAL declared dependency, not a mock: `uv` is in
        `install_requires`, so a stock install must resolve it without PATH."""
        resolved = provision.resolve_uv()
        assert resolved is not None, "uv is a declared dependency; it must resolve"
        assert Path(resolved).is_absolute() and Path(resolved).is_file()


class TestRunSandboxing:
    """`_run` spawns third-party code (`uv` against a freshly fetched tree), so
    the environment it hands the child is security-relevant."""

    def test_spawns_through_the_credential_scrubbing_chokepoint(self, tmp_path: Path):
        """Not a bare `subprocess.run`: the child must get the scrubbed env from
        `sandboxed_spawn_argv`, or it inherits cloud creds and SSH_AUTH_SOCK."""
        scrubbed = {"PATH": "/usr/bin"}
        with (
            mock.patch.object(
                provision, "sandboxed_spawn_argv", return_value=(["/opt/uv", "x"], scrubbed, None)
            ) as chokepoint,
            mock.patch.object(provision.subprocess, "run") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="out", stderr="")
            provision._run(["/opt/uv", "x"], cwd=str(tmp_path), timeout=5)
        assert chokepoint.call_args.kwargs["strip_python_env"] is True
        assert run.call_args.kwargs["env"] == scrubbed
        # STRICT, not the `standard` default. Scrubbing the env stops the child
        # INHERITING a credential; it does not stop it READING one, and standard
        # mode leaves ~/.aws and ~/.ssh visible so the AWS CLI and git-over-SSH
        # keep working. A build backend over a freshly-downloaded tree needs
        # neither. No sandbox mode restricts network, so uv still reaches the index.
        assert chokepoint.call_args.kwargs.get("mode") == "strict"

    def test_registration_is_skipped_when_the_app_was_disabled(self, tmp_path: Path):
        """Provisioning runs for minutes; the operator can disable meanwhile.

        Registration recreates the agent symlinks and the skill entry that disabling
        had just removed, leaving a DISABLED app with live resources. The enable is
        therefore re-checked immediately before registering.
        """
        registered: list[str] = []
        log: list[str] = []
        with (
            mock.patch(
                "kiro_crew.apps.manager.is_app_enabled", return_value=False
            ),
            mock.patch(
                "kiro_crew.apps.bridges.register_app",
                side_effect=lambda name: registered.append(name),
            ),
        ):
            provision._register_resources(log)
        assert registered == [], "a disabled app must not have its resources re-registered"
        assert any("disabled during provisioning" in line for line in log)

    def test_registration_runs_while_the_app_is_enabled(self, tmp_path: Path):
        """The other direction — the guard must not block the normal path."""
        registered: list[str] = []

        class _Result:
            agents = ["a"]
            skills = ["s"]
            errors: list[str] = []

        def _register(name: str) -> "_Result":
            registered.append(name)
            return _Result()

        with (
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app", side_effect=_register),
        ):
            provision._register_resources([])
        assert registered == [provision.paths.APP_NAME]

    def test_a_timeout_is_reported_not_raised(self, tmp_path: Path):
        """Provisioning must report, not explode: a hung `uv` becomes a failed
        step with a message the UI can show."""
        with (
            mock.patch.object(provision, "sandboxed_spawn_argv", return_value=(["uv"], {}, None)),
            mock.patch.object(
                provision.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=7),
            ),
        ):
            code, out = provision._run(["uv", "sync"], cwd=str(tmp_path), timeout=7)
        assert code == 1
        assert "timed out" in out

    def test_a_missing_binary_is_reported_not_raised(self, tmp_path: Path):
        with (
            mock.patch.object(provision, "sandboxed_spawn_argv", return_value=(["uv"], {}, None)),
            mock.patch.object(provision.subprocess, "run", side_effect=OSError("No such file")),
        ):
            code, out = provision._run(["uv", "sync"], cwd=str(tmp_path), timeout=7)
        assert code == 1
        assert "could not be run" in out

    def test_the_sandbox_profile_is_always_cleaned_up(self, tmp_path: Path):
        """The chokepoint may hand back a temp profile path; leaking one per
        spawn would litter the temp dir on every provision."""
        profile = tmp_path / "sandbox.sb"
        profile.write_text("(version 1)", encoding="utf-8")
        with (
            mock.patch.object(
                provision, "sandboxed_spawn_argv", return_value=(["/opt/uv"], {}, str(profile))
            ),
            mock.patch.object(provision.subprocess, "run", side_effect=OSError("boom")),
        ):
            provision._run(["/opt/uv"], cwd=str(tmp_path), timeout=5)
        assert not profile.exists()

    def test_output_merges_both_streams(self, tmp_path: Path):
        """`uv` splits its diagnostics across stdout/stderr, so the log the user
        is shown has to carry both."""
        with (
            mock.patch.object(
                provision, "sandboxed_spawn_argv", return_value=(["/opt/uv"], {}, None)
            ),
            mock.patch.object(provision.subprocess, "run") as run,
        ):
            run.return_value = mock.Mock(returncode=2, stdout="on-out\n", stderr="on-err")
            code, out = provision._run(["/opt/uv"], cwd=str(tmp_path), timeout=5)
        assert code == 2
        assert "on-out" in out and "on-err" in out


class TestEnsureVenv:
    def test_a_checkout_without_mcp_local_is_refused(self, tmp_path: Path):
        """Guards against building against a tree that is not the engine."""
        log: list[str] = []
        with mock.patch.object(provision, "_run") as run:
            assert provision._ensure_venv(tmp_path, log, "/opt/uv") is False
        assert not run.called
        assert any("mcp-local" in line for line in log)

    def test_a_failed_dependency_resolve_stops_before_the_skill_install(self, tmp_path: Path):
        (tmp_path / "mcp-local").mkdir()
        log: list[str] = []
        with mock.patch.object(provision, "_run", return_value=(1, "resolution failed")) as run:
            assert provision._ensure_venv(tmp_path, log, "/opt/uv") is False
        assert run.call_count == 1
        assert any("dependency resolve failed" in line for line in log)

    def test_both_uv_calls_use_the_resolved_absolute_path(self, tmp_path: Path):
        """Never the bare name `uv`: the gateway's PATH may not carry the venv's
        scripts dir (installed service), and a frozen bundle has none at all."""
        (tmp_path / "mcp-local").mkdir()
        resolved = "/opt/kirocrew/uv"
        with mock.patch.object(provision, "_run", return_value=(0, "")) as run:
            assert provision._ensure_venv(tmp_path, [], resolved) is True
        assert run.call_count == 2
        for call in run.call_args_list:
            assert call.args[0][0] == resolved

    def test_the_skill_package_is_installed_editable(self, tmp_path: Path):
        """A non-editable install drops the engine's sibling data dirs (bundled
        templates and styles), so they would silently go missing."""
        (tmp_path / "mcp-local").mkdir()
        log: list[str] = []
        with mock.patch.object(provision, "_run", return_value=(0, "")) as run:
            assert provision._ensure_venv(tmp_path, log, "/opt/uv") is True
        install_argv = run.call_args_list[1].args[0]
        assert install_argv[:4] == ["/opt/uv", "pip", "install", "--python"]
        assert "--editable" in install_argv

    def test_a_failed_skill_install_fails_provisioning(self, tmp_path: Path):
        (tmp_path / "mcp-local").mkdir()
        log: list[str] = []
        with mock.patch.object(provision, "_run", side_effect=[(0, ""), (1, "wheel build failed")]):
            assert provision._ensure_venv(tmp_path, log, "/opt/uv") is False
        assert any("skill package install failed" in line for line in log)


class TestRenderAgents:
    """The shipped agent configs are templates: the engine's absolute location is
    only known at provision time, so every placeholder must be substituted."""

    def test_renders_every_shipped_agent_with_no_placeholder_left(self, tmp_path: Path):
        install_dir = tmp_path / "install"
        log: list[str] = []
        written = provision._render_agents(install_dir, log)
        assert written > 0, "the app ships agent templates; none were rendered"
        assert log == []
        for rendered in (install_dir / "agents").glob("*.json"):
            text = rendered.read_text(encoding="utf-8")
            for placeholder in _declared_placeholders():
                assert placeholder not in text, f"{rendered.name} kept {placeholder}"
            # Nothing that merely LOOKS like a placeholder either. The list above was
            # hand-maintained and a fourth placeholder (`{UV_BIN}`) was added to the
            # templates without being added here — so the check passed while every
            # rendered config still carried a literal `{UV_BIN}` as its MCP command.
            leftover = re.findall(r"\{[A-Z][A-Z0-9_]*\}", text)
            assert leftover == [], f"{rendered.name} kept {leftover}"
            # An unsubstituted config is also a config kiro-cli cannot parse.
            json.loads(text)

    def test_the_prompts_placeholder_points_into_the_install_dir(self, tmp_path: Path):
        install_dir = tmp_path / "install"
        provision._render_agents(install_dir, log=[])
        rendered = sorted((install_dir / "agents").glob("*.json"))
        # Asserted against the PARSED values, not the raw file text: the
        # substituted path is JSON-escaped on the way in, so on Windows the
        # bytes on disk spell `C:\\Users\\…` and a raw substring check would
        # miss a perfectly correct render.
        strings = [
            s
            for path in rendered
            for s in _json_strings(json.loads(path.read_text(encoding="utf-8")))
        ]
        wanted = str(install_dir / "prompts")
        assert any(wanted in s for s in strings)

    def test_a_windows_style_path_still_renders_parseable_json(self, tmp_path: Path):
        """A backslash path must be JSON-escaped into the template.

        Regression test for a real Windows bug: the placeholders sit inside JSON
        string literals, and every substituted value is an absolute path — so on
        Windows the raw substitution produced `"C:\\Users\\…"`, whose `\\U` is an
        invalid JSON escape. `json.loads` then rejected EVERY template and a
        Windows user was provisioned zero agent configs. Simulated rather than
        skipped so the escaping is pinned on every platform.
        """
        install_dir = tmp_path / "install"
        win_root = r"C:\Users\runneradmin\.kiro\crew\apps\pptx-maker\data\vendor\sdpm"
        log: list[str] = []
        with (
            mock.patch.object(provision.paths, "engine_root", return_value=Path(win_root)),
            mock.patch.object(
                provision.paths, "engine_mcp_dir", return_value=Path(win_root + r"\mcp-local")
            ),
        ):
            written = provision._render_agents(install_dir, log)
        assert written > 0
        assert log == []
        for path in sorted((install_dir / "agents").glob("*.json")):
            # The whole point: it parses, and the path survives round-trip
            # intact rather than being mangled by escape processing.
            parsed = json.loads(path.read_text(encoding="utf-8"))
            assert any(win_root in s for s in _json_strings(parsed)), path.name

    def test_a_quote_in_a_path_cannot_break_out_of_the_json_string(self, tmp_path: Path):
        """The escaping is a real escape, not a backslash special-case — a path
        holding a quote must stay one JSON string rather than terminating it."""
        install_dir = tmp_path / "install"
        nasty = '/tmp/we"ird\\path'
        with mock.patch.object(provision.paths, "engine_root", return_value=Path(nasty)):
            assert provision._render_agents(install_dir, log=[]) > 0
        for path in sorted((install_dir / "agents").glob("*.json")):
            json.loads(path.read_text(encoding="utf-8"))

    def test_a_malformed_template_is_reported_and_never_written(self, tmp_path: Path):
        """A malformed agent config is silently IGNORED by kiro-cli, surfacing
        only as "the mode is missing" — so it must fail loudly here instead."""
        source = tmp_path / "agents"
        source.mkdir()
        (source / "broken.json").write_text("{not json", encoding="utf-8")
        install_dir = tmp_path / "install"
        log: list[str] = []
        with mock.patch.object(provision, "_PACKAGE_ROOT", tmp_path):
            assert provision._render_agents(install_dir, log) == 0
        assert any("broken.json" in line for line in log)
        assert not (install_dir / "agents" / "broken.json").exists()

    def test_a_missing_agents_dir_writes_nothing(self, tmp_path: Path):
        with mock.patch.object(provision, "_PACKAGE_ROOT", tmp_path / "absent"):
            assert provision._render_agents(tmp_path / "install", log=[]) == 0


class TestStageStatic:
    def test_prompts_are_copied_so_a_read_only_wheel_install_works(self, tmp_path: Path):
        """Copied, not symlinked: the package dir is read-only on a wheel
        install and the rendered agents point at the install-dir copy."""
        install_dir = tmp_path / "install"
        provision._stage_static(install_dir, log=[])
        staged = install_dir / "prompts"
        assert staged.is_dir()
        assert any(staged.glob("*.md"))
        assert not staged.is_symlink()

    def test_restaging_replaces_the_previous_copy(self, tmp_path: Path):
        """Provisioning is idempotent, so a stale prompt from an older app
        version must not survive into the new install dir."""
        install_dir = tmp_path / "install"
        stale = install_dir / "prompts" / "stale.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("from an older version", encoding="utf-8")
        provision._stage_static(install_dir, log=[])
        assert not stale.exists()

    def test_the_skill_is_deliberately_not_staged(self, tmp_path: Path):
        """The skill ships via `builtin_skills/` (copied on every gateway start)
        so it reaches every install without provisioning; staging a second copy
        here would register the same skill twice."""
        install_dir = tmp_path / "install"
        provision._stage_static(install_dir, log=[])
        assert not (install_dir / "skills").exists()

    def test_a_copy_failure_is_reported_not_raised(self, tmp_path: Path):
        log: list[str] = []
        with mock.patch.object(provision, "_copy_tree", side_effect=OSError("read-only fs")):
            provision._stage_static(tmp_path / "install", log)
        assert any("could not be staged" in line for line in log)


class TestSeedDeckRoot:
    """A first install seeds a deck dir that needs no macOS file-access grant;
    an existing config or existing decks must never be touched."""

    def test_seeds_a_config_dir_location_on_a_first_install(self, tmp_path: Path):
        config_path = tmp_path / "sdpm" / "config.json"
        log: list[str] = []
        with (
            mock.patch.object(provision.paths, "engine_config_path", return_value=config_path),
            mock.patch.object(provision.Path, "home", return_value=tmp_path / "home"),
        ):
            provision._seed_deck_root(log)
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["output_dir"] == provision.paths.SEEDED_DECK_ROOT

    def test_an_existing_engine_config_is_never_overwritten(self, tmp_path: Path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"output_dir": "/the/users/choice"}), encoding="utf-8")
        with mock.patch.object(provision.paths, "engine_config_path", return_value=config_path):
            provision._seed_deck_root(log=[])
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert saved["output_dir"] == "/the/users/choice"

    def test_existing_decks_in_the_engine_default_are_left_alone(self, tmp_path: Path):
        """Someone already using the engine has decks in ~/Documents; silently
        re-pointing the output dir would strand them."""
        home = tmp_path / "home"
        existing = home / provision.paths.ENGINE_DEFAULT_DECK_ROOT
        existing.mkdir(parents=True)
        (existing / "20260101-deck").mkdir()
        config_path = tmp_path / "sdpm" / "config.json"
        with (
            mock.patch.object(provision.paths, "engine_config_path", return_value=config_path),
            mock.patch.object(provision.Path, "home", return_value=home),
        ):
            provision._seed_deck_root(log=[])
        assert not config_path.exists()

    def test_an_empty_engine_default_still_gets_seeded(self, tmp_path: Path):
        home = tmp_path / "home"
        (home / provision.paths.ENGINE_DEFAULT_DECK_ROOT).mkdir(parents=True)
        config_path = tmp_path / "sdpm" / "config.json"
        with (
            mock.patch.object(provision.paths, "engine_config_path", return_value=config_path),
            mock.patch.object(provision.Path, "home", return_value=home),
        ):
            provision._seed_deck_root(log=[])
        assert config_path.exists()


class TestProvision:
    """The top-level orchestration: what fails the run, and what only warns."""

    @pytest.fixture(autouse=True)
    def _no_real_work(self, tmp_path: Path):
        """Neutralize every side effect; each test re-patches what it cares about."""
        with (
            mock.patch.object(provision.paths, "engine_root", return_value=tmp_path / "engine"),
            mock.patch.object(provision, "app_dir", return_value=tmp_path / "install"),
            mock.patch.object(provision, "_stage_static"),
            mock.patch.object(provision, "_render_agents", return_value=2),
            mock.patch.object(provision, "_seed_deck_root"),
            mock.patch.object(provision, "_current_tag", return_value="v0.3.8"),
        ):
            yield

    def test_an_unresolvable_uv_fails_before_any_network_call(self):
        """`uv` ships as a Python dependency, so this only happens on a broken
        install — and it must say THAT rather than failing deep inside a build."""
        with (
            mock.patch.object(provision, "resolve_uv", return_value=None),
            mock.patch.object(provision, "_ensure_engine") as fetch,
        ):
            outcome = provision.provision()
        assert outcome.ok is False
        assert "`uv` could not be found" in outcome.log
        assert not fetch.called, "a missing uv must stop before fetching the engine"

    def test_the_failure_message_no_longer_blames_git(self):
        """`git` is not used any more; a message naming it would send the user
        off to install something this app does not need."""
        with mock.patch.object(provision, "resolve_uv", return_value=None):
            outcome = provision.provision()
        assert "git" not in outcome.log

    def test_the_resolved_uv_is_threaded_into_the_build(self):
        """Resolved ONCE and passed down, so the fetch and the two uv calls can
        never disagree about which binary they are using."""
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/opt/kirocrew/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=True) as fetch,
            mock.patch.object(provision, "_ensure_venv", return_value=False) as venv,
        ):
            provision.provision()
        assert fetch.call_args.args[2] == "/opt/kirocrew/uv"
        assert venv.call_args.args[2] == "/opt/kirocrew/uv"

    def test_a_refused_engine_fetch_never_builds_a_venv(self):
        """The digest-pin refusal is worthless if the build runs anyway."""
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/x/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=False),
            mock.patch.object(provision, "_ensure_venv") as venv,
        ):
            outcome = provision.provision()
        assert outcome.ok is False
        assert not venv.called

    def test_a_failed_venv_build_does_not_register_agents(self):
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/x/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=True),
            mock.patch.object(provision, "_ensure_venv", return_value=False),
            mock.patch.object(provision, "_render_agents") as render,
        ):
            outcome = provision.provision()
        assert outcome.ok is False
        assert not render.called

    def test_a_successful_run_reports_the_resolved_tag(self):
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/x/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=True),
            mock.patch.object(provision, "_ensure_venv", return_value=True),
            # Enabled: see the disable-during-provision test above.
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app") as register,
            mock.patch(
                "kiro_crew.apps.builtins.pptx_maker.backend.engine.scan_new_templates",
                return_value=["corp"],
            ),
        ):
            register.return_value = mock.Mock(agents=["a", "b"], skills=["s"], errors=[])
            outcome = provision.provision()
        assert outcome.ok is True
        assert outcome.engine_tag == "v0.3.8"
        assert "analyzed 1 template(s)" in outcome.log
        # Provisioning writes the agent CONFIGS but deliberately does not REGISTER
        # them; see the next test for why.
        assert "2 agent config(s) written" in outcome.log

    def test_provisioning_does_not_register_resources_itself(self):
        """Registration belongs to the enable path and the boot reconcile, not here.

        `bridges._placeholder_values` computes this app's `{UV_BIN}`/`{ENGINE_ROOT}`/
        `{ENGINE_MCP_DIR}`/`{APP_PROMPTS}` in the gateway from the data home and the
        installed package, so `register_app` lands the agents and skill without any
        help from the provisioner — this call was redundant.

        It was also an unclosable race. Provisioning is a detached job that runs for
        minutes, so an operator can disable the app while it works; the enable
        re-check cannot be atomic (the lifecycle lock is an `asyncio.Lock` and this
        runs synchronously on a worker thread). A disable that deregisters and THEN
        sets `enabled=false` is observed as still-enabled, and registering recreates
        the agent and MCP configs the disable had just removed — a disabled app left
        with live, callable resources. Not registering here removes the window rather
        than narrowing it.
        """
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/x/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=True),
            mock.patch.object(provision, "_ensure_venv", return_value=True),
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app") as register,
            mock.patch(
                "kiro_crew.apps.builtins.pptx_maker.backend.engine.scan_new_templates",
                return_value=[],
            ),
        ):
            register.return_value = mock.Mock(agents=["a"], skills=["s"], errors=[])
            outcome = provision.provision()
        assert outcome.ok is True
        register.assert_not_called()

    def test_a_template_analysis_failure_does_not_fail_provisioning(self):
        """An un-analyzed template is still usable, so this is best effort."""
        with (
            mock.patch.object(provision, "resolve_uv", return_value="/x/uv"),
            mock.patch.object(provision, "_ensure_engine", return_value=True),
            mock.patch.object(provision, "_ensure_venv", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app") as register,
            mock.patch(
                "kiro_crew.apps.builtins.pptx_maker.backend.engine.scan_new_templates",
                side_effect=RuntimeError("engine exploded"),
            ),
        ):
            register.return_value = mock.Mock(agents=[], skills=[], errors=[])
            outcome = provision.provision()
        assert outcome.ok is True
        assert "template analysis skipped" in outcome.log

    def test_registration_warnings_are_surfaced(self):
        """Exercised against `_register_resources` directly, because `provision()` no
        longer calls it — the helper remains the seam for a caller that does register,
        and its reporting still has to reach the log the UI shows."""
        log: list[str] = []
        with (
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch("kiro_crew.apps.bridges.register_app") as register,
        ):
            register.return_value = mock.Mock(
                agents=[], skills=[], errors=["skill link already exists"]
            )
            provision._register_resources(log)
        assert "registration warning: skill link already exists" in log

    def test_a_registration_failure_is_reported_not_raised(self):
        """Same seam, the failure direction: a detached background job's only channel
        to the user is this log, so a registrar exception must be reported."""
        log: list[str] = []
        with (
            mock.patch("kiro_crew.apps.manager.is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.apps.bridges.register_app", side_effect=RuntimeError("manifest gone")
            ),
        ):
            provision._register_resources(log)
        assert "resource registration failed: manifest gone" in log


class TestProvisionOutcome:
    def test_the_log_handed_to_the_ui_is_tail_bounded(self):
        """A cold `uv sync` emits megabytes; the response must stay bounded."""
        outcome = provision.ProvisionOutcome(ok=False, log="x" * 99999)
        assert len(outcome.to_dict()["log"]) == provision.LOG_TAIL_CHARS

    def test_to_dict_keeps_the_tail_not_the_head(self):
        """The FAILURE is at the end of a build log — truncating the head would
        throw away the only useful part."""
        outcome = provision.ProvisionOutcome(ok=False, log="early\n" + "x" * 8000 + "\nTHE ERROR")
        assert outcome.to_dict()["log"].endswith("THE ERROR")


class TestShippedAgentsDoNotPreAuthorizeTools:
    """No shipped agent config may carry ``allowedTools``.

    An auto-approved tool never reaches ``hooks.on_tool_call``: that hook is only
    invoked from the permission-request branch, and the tool-call branch is explicitly
    informational and cannot block. So an ``allowedTools`` entry skips the whole
    PreToolUse plane — deny rules, the governance ceiling, an enterprise
    ``enabled: false``. ``AGENTS.md`` states the same rule for the managed
    ``kirocrew-computer`` server, which is deliberately in ``tools`` but NOT
    ``allowedTools`` and ships no ``autoApprove`` key.

    These four configs pre-authorized ``read`` alongside ``web_fetch`` — the
    exfiltration pair — so URL content that talked the agent into reading a file could
    send it back out with neither call surfacing for approval.

    Removing the key costs nothing functional: the tools stay in ``tools``, so the
    agent can still call them; each call now prompts (or is auto-approved by the
    read-only policy in ``hooks.py``, AFTER the deny and governance checks).

    Meetings' three agent configs already ship no ``allowedTools`` — this brings PPTX
    in line rather than inventing a new rule.
    """

    def test_no_agent_template_carries_allowed_tools(self) -> None:
        import io

        # Anchored on the PACKAGE, not the CWD. A relative `glob` only resolves when
        # pytest happens to run from the repo root — CI's sharded matrix does not, so
        # this found zero templates and the assertion below was the only thing that
        # caught it. `_PACKAGE_ROOT` is derived from `__file__`, so it is correct from
        # any working directory. (`builtins/` is this app's package parent.)
        builtins_dir = provision._PACKAGE_ROOT.parent
        templates = sorted(builtins_dir.glob("*/agents/*.json"))
        assert templates, "no shipped agent templates found — did the path change?"
        offenders: list[str] = []
        for path in templates:
            raw = io.open(path, encoding="utf-8").read()
            # Render placeholders so a template still parses as JSON.
            for placeholder in _declared_placeholders():
                raw = raw.replace(placeholder, "/rendered")
            data = json.loads(raw)
            if data.get("allowedTools"):
                offenders.append(path)
        assert offenders == [], (
            "these shipped agent configs pre-authorize tools, which skips the entire "
            "PreToolUse gate (deny rules, governance ceiling):\n  " + "\n  ".join(offenders)
        )

    def test_the_tools_themselves_are_still_offered(self) -> None:
        """The fix must not disable the agents: only the auto-approval is removed."""
        import io

        for path in sorted((provision._PACKAGE_ROOT / "agents").glob("*.json")):
            raw = io.open(path, encoding="utf-8").read()
            for placeholder in _declared_placeholders():
                raw = raw.replace(placeholder, "/rendered")
            data = json.loads(raw)
            assert data.get("tools"), f"{path} lost its tools entirely"

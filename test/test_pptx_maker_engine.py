"""PPTX Maker engine-bridge tests (`backend/engine.py`).

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

This module is the ONLY place that talks to the vendored presentation engine, and
it talks by SPAWNING it: a separately versioned third-party checkout, driven with
model-authored deck content. Three properties therefore matter more than the
happy path, and each is pinned below:

* **The spawn is sandboxed.** Every call goes through
  ``sandboxed_spawn_argv(..., strip_python_env=True)`` so the child gets a
  credential-scrubbed environment and cannot inherit the gateway's ``PYTHONPATH``.
* **A not-ready or misbehaving engine degrades, never raises.** Half-installed
  engine, non-zero exit, non-JSON stdout and wrong-typed JSON must all become a
  documented empty/None result — these functions back a status banner that has to
  answer while the engine is still being provisioned.
* **The engine's shape is normalized.** ``load_lists`` and ``scan_new_templates``
  coerce whatever comes back over the subprocess boundary.

No real subprocess: every test mocks at the ``_spawn`` / ``sandboxed_spawn_argv``
boundary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

from kiro_crew.apps.builtins.pptx_maker.backend import engine, engine_source, paths


class TestEngineResult:
    """``ok`` is the gate every caller trusts before parsing stdout."""

    def test_a_zero_exit_with_output_is_ok(self):
        assert engine.EngineResult(returncode=0, stdout='{"a": 1}').ok is True

    def test_a_zero_exit_with_empty_stdout_is_not_ok(self):
        """The engine exiting 0 while printing nothing means the snippet did not
        reach its `print` — treating that as success yields a None parse later."""
        assert engine.EngineResult(returncode=0, stdout="   \n").ok is False

    def test_a_non_zero_exit_is_not_ok_even_with_output(self):
        assert engine.EngineResult(returncode=1, stdout='{"a": 1}').ok is False

    def test_json_parses_stdout(self):
        assert engine.EngineResult(returncode=0, stdout='{"a": 1}').json() == {"a": 1}

    def test_json_is_none_on_malformed_stdout(self):
        """The engine venv can print a warning ahead of the payload; that must
        become None rather than an exception on the gateway's worker thread."""
        assert engine.EngineResult(returncode=0, stdout="Traceback...").json() is None

    def test_json_is_none_when_not_ok(self):
        assert engine.EngineResult(returncode=1, stdout='{"a": 1}').json() is None


class TestSpawnSandboxing:
    """The child is third-party code cloned at provision time and driven with
    model-authored content, so the environment it gets is security-relevant."""

    def test_routes_through_the_credential_scrubbing_chokepoint(self, tmp_path: Path):
        """Not a bare `subprocess.run`: without the scrubbed env the engine
        inherits AWS_SECRET*/SSH_AUTH_SOCK/GIT_ASKPASS and every bot token."""
        scrubbed = {"PATH": "/usr/bin"}
        with (
            mock.patch.object(
                engine, "sandboxed_spawn_argv", return_value=(["py"], scrubbed, None)
            ) as chokepoint,
            mock.patch.object(engine, "run_limited") as run,
        ):
            run.return_value = mock.Mock(returncode=0, stdout="{}", stderr="")
            engine._spawn(["py"], cwd=str(tmp_path), timeout=5)
        assert chokepoint.call_args.kwargs["strip_python_env"] is True
        assert run.call_args.kwargs["env"] == scrubbed
        # STRICT, not the `standard` default. The env scrub above stops this child
        # INHERITING a credential; it does not stop it READING one off disk, and
        # standard mode deliberately leaves ~/.aws and ~/.ssh visible so
        # git-over-SSH and the AWS CLI keep working. A .pptx engine driven with
        # model-authored deck content needs neither — and could otherwise open a
        # credentials file and typeset it into a slide.
        assert chokepoint.call_args.kwargs.get("mode") == "strict"

    def test_a_spawn_failure_becomes_a_result_not_an_exception(self, tmp_path: Path):
        """These run on a worker thread behind `off_loop`; an escaping OSError
        would surface as an opaque 500 instead of a degraded status."""
        with (
            mock.patch.object(engine, "sandboxed_spawn_argv", return_value=(["py"], {}, None)),
            mock.patch.object(engine, "run_limited", side_effect=OSError("ENOENT")),
        ):
            result = engine._spawn(["py"], cwd=str(tmp_path), timeout=5)
        assert result.returncode == 1
        assert "ENOENT" in result.stderr

    def test_a_timeout_becomes_a_result_not_an_exception(self, tmp_path: Path):
        with (
            mock.patch.object(engine, "sandboxed_spawn_argv", return_value=(["py"], {}, None)),
            mock.patch.object(
                engine,
                "run_limited",
                side_effect=subprocess.TimeoutExpired(cmd="py", timeout=3),
            ),
        ):
            result = engine._spawn(["py"], cwd=str(tmp_path), timeout=3)
        assert result.returncode == 1

    def test_the_sandbox_profile_is_cleaned_up_even_on_failure(self, tmp_path: Path):
        """One leaked temp profile per engine call would litter the temp dir —
        and these run on every library listing."""
        profile = tmp_path / "sandbox.sb"
        profile.write_text("(version 1)", encoding="utf-8")
        with (
            mock.patch.object(
                engine, "sandboxed_spawn_argv", return_value=(["py"], {}, str(profile))
            ),
            mock.patch.object(engine, "run_limited", side_effect=OSError("boom")),
        ):
            engine._spawn(["py"], cwd=str(tmp_path), timeout=5)
        assert not profile.exists()


class TestEngineStatus:
    """Drives the first-run banner, so it must answer while the engine is only
    half-installed rather than raising."""

    def test_reports_not_ready_when_nothing_is_installed(self, tmp_path: Path):
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=tmp_path / "absent"),
            mock.patch.object(engine.paths, "engine_python", return_value=tmp_path / "no-python"),
        ):
            assert engine.engine_status() == {"ready": False, "clone": False, "venv": False}

    def test_a_source_tree_without_a_venv_is_not_ready(self, tmp_path: Path):
        """The exact half-installed state a provisioning run passes through: the
        banner must keep saying "not ready" instead of enabling the UI."""
        root = tmp_path / "engine"
        root.mkdir(parents=True)
        engine_source.write_source_marker(root)
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=root),
            mock.patch.object(engine.paths, "engine_python", return_value=tmp_path / "no-python"),
        ):
            status = engine.engine_status()
        assert status == {"ready": False, "clone": True, "venv": False}

    def test_ready_only_when_both_the_source_tree_and_the_venv_exist(self, tmp_path: Path):
        root = tmp_path / "engine"
        root.mkdir(parents=True)
        engine_source.write_source_marker(root)
        python = tmp_path / "python"
        python.write_text("#!/bin/sh", encoding="utf-8")
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=root),
            mock.patch.object(engine.paths, "engine_python", return_value=python),
        ):
            assert engine.engine_status()["ready"] is True

    def test_an_unverified_tree_is_not_ready_even_with_a_venv(self, tmp_path: Path):
        """A tree left by an older git-based install has no source marker, so it
        must read as "not installed" and be replaced by a verified fetch rather
        than trusted because a venv happens to sit next to it."""
        root = tmp_path / "engine"
        (root / ".git").mkdir(parents=True)
        python = tmp_path / "python"
        python.write_text("#!/bin/sh", encoding="utf-8")
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=root),
            mock.patch.object(engine.paths, "engine_python", return_value=python),
        ):
            status = engine.engine_status()
        assert status == {"ready": False, "clone": False, "venv": True}


class TestRunEngineSnippet:
    def test_returns_none_when_the_venv_is_absent(self, tmp_path: Path):
        """None (not a failed result) is the documented "engine not ready"
        signal every caller branches on."""
        with (
            mock.patch.object(engine.paths, "engine_python", return_value=tmp_path / "absent"),
            mock.patch.object(engine, "_spawn") as spawn,
        ):
            assert engine.run_engine_snippet("print(1)") is None
        assert not spawn.called, "must not spawn without an interpreter"

    def test_runs_the_snippet_in_the_engine_interpreter(self, tmp_path: Path):
        """`-c <snippet>` under the ENGINE's python, not the gateway's: the
        engine pins its own lxml/python-pptx closure."""
        python = tmp_path / "python"
        python.write_text("#!/bin/sh", encoding="utf-8")
        with (
            mock.patch.object(engine.paths, "engine_python", return_value=python),
            mock.patch.object(engine, "_spawn", return_value=engine.EngineResult(0, "{}")) as spawn,
        ):
            engine.run_engine_snippet("print(1)", ["arg-one"])
        argv = spawn.call_args.args[0]
        assert argv[0] == str(python)
        assert argv[1] == "-c"
        assert argv[2] == "print(1)"
        assert argv[3:] == ["arg-one"]


class TestEngineTag:
    """Read from the verified source marker, not from `git describe`: the engine
    now arrives as a digest-pinned tarball with no `.git` at all."""

    def test_unknown_without_an_installed_tree(self, tmp_path: Path):
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=tmp_path / "absent"),
            mock.patch.object(engine, "_spawn") as spawn,
        ):
            assert engine.engine_tag() == "unknown"
        assert not spawn.called

    def test_reports_the_verified_tag_without_spawning_anything(self, tmp_path: Path):
        """A file read, so it is cheap enough for the status endpoints that call
        it on every poll — and it cannot report a tag nobody verified."""
        root = tmp_path / "engine"
        root.mkdir(parents=True)
        engine_source.write_source_marker(root)
        with (
            mock.patch.object(engine.paths, "engine_root", return_value=root),
            mock.patch.object(engine, "_spawn") as spawn,
        ):
            assert engine.engine_tag() == engine_source.ENGINE_TAG
        assert not spawn.called

    def test_an_unverified_tree_is_unknown_not_empty(self, tmp_path: Path):
        """The tag keys the icon-pack provisioning marker, so an empty string
        would read as a real (and permanently mismatched) version."""
        root = tmp_path / "engine"
        (root / ".git").mkdir(parents=True)
        with mock.patch.object(engine.paths, "engine_root", return_value=root):
            assert engine.engine_tag() == "unknown"


class TestUserConfigDir:
    """Resolved by asking the ENGINE rather than re-deriving it, so this app and
    the engine can never disagree about where user styles/templates live."""

    def test_returns_the_path_the_engine_reports(self):
        with mock.patch.object(
            engine,
            "run_engine_snippet",
            return_value=engine.EngineResult(0, "/home/u/.config/sdpm\n"),
        ):
            assert engine.user_config_dir() == Path("/home/u/.config/sdpm")

    def test_none_when_the_engine_is_not_ready(self):
        with mock.patch.object(engine, "run_engine_snippet", return_value=None):
            assert engine.user_config_dir() is None

    def test_none_when_the_engine_call_fails(self):
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(1, "")
        ):
            assert engine.user_config_dir() is None

    def test_user_subdir_joins_onto_the_engine_answer(self):
        with mock.patch.object(engine, "user_config_dir", return_value=Path("/cfg")):
            assert engine.user_subdir("styles") == Path("/cfg/styles")

    def test_user_subdir_is_none_when_the_base_is_unknown(self):
        """A None base must not become the relative path "styles", which would
        write the user's library into the gateway's cwd."""
        with mock.patch.object(engine, "user_config_dir", return_value=None):
            assert engine.user_subdir("styles") is None


class TestLoadLists:
    def test_normalizes_the_engine_payload(self):
        payload = {
            "styles": [{"name": "brand"}],
            "templates": [{"name": "corp"}],
            "stylesDirs": ["/a", "/b"],
        }
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, json.dumps(payload))
        ):
            assert engine.load_lists() == payload

    def test_empty_shape_when_the_engine_is_not_ready(self):
        """The library routes index these three keys unconditionally, so a
        not-ready engine must still yield the full shape."""
        with mock.patch.object(engine, "run_engine_snippet", return_value=None):
            assert engine.load_lists() == {"styles": [], "templates": [], "stylesDirs": []}

    def test_empty_shape_when_the_engine_returns_non_json(self):
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, "not json")
        ):
            assert engine.load_lists() == {"styles": [], "templates": [], "stylesDirs": []}

    def test_a_json_list_instead_of_an_object_is_refused(self):
        """Wrong-typed JSON must not become an attribute error on `.get`."""
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, "[1, 2]")
        ):
            assert engine.load_lists() == {"styles": [], "templates": [], "stylesDirs": []}

    def test_missing_and_null_keys_become_empty_lists(self):
        with mock.patch.object(
            engine,
            "run_engine_snippet",
            return_value=engine.EngineResult(0, json.dumps({"styles": None})),
        ):
            assert engine.load_lists() == {"styles": [], "templates": [], "stylesDirs": []}

    def test_passes_the_bundled_dirs_so_builtin_assets_resolve(self):
        """The bundled styles/templates only resolve if these argv entries are
        handed over — without them the builtin library silently lists empty."""
        with (
            mock.patch.object(
                engine, "run_engine_snippet", return_value=engine.EngineResult(0, "{}")
            ) as snippet,
            mock.patch.object(engine.paths, "engine_skill_dir", return_value=Path("/skill")),
        ):
            engine.load_lists()
        argv = snippet.call_args.args[1]
        # Compared as Paths: `load_lists` builds these with pathlib, so the
        # separator is the host's and a literal "/" assertion fails on Windows
        # for an argv that is entirely correct.
        skill = Path("/skill")
        assert [Path(a) for a in argv] == [
            skill / "references" / "examples" / "styles",
            skill / "templates",
        ]


class TestAnalyzeTemplate:
    def test_returns_the_engine_metadata(self):
        meta = {"name": "corp", "layout_count": 12}
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, json.dumps(meta))
        ):
            assert engine.analyze_template(Path("/t/corp.pptx"), "desc") == meta

    def test_falls_back_to_the_description_when_analysis_cannot_run(self):
        """An un-analyzed template is still usable, so a failed analysis must not
        lose the description the user typed."""
        with mock.patch.object(engine, "run_engine_snippet", return_value=None):
            assert engine.analyze_template(Path("/t/x.pptx"), "corporate") == {
                "description": "corporate"
            }

    def test_a_non_dict_result_falls_back_too(self):
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, '"a string"')
        ):
            assert engine.analyze_template(Path("/t/x.pptx"), "d") == {"description": "d"}

    def test_uses_the_longer_analyze_timeout(self):
        """Opening and measuring a .pptx is far slower than a metadata read, so
        the default 30s call timeout would truncate real analyses."""
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, "{}")
        ) as snippet:
            engine.analyze_template(Path("/t/x.pptx"), "d")
        assert snippet.call_args.kwargs["timeout"] == engine.ENGINE_ANALYZE_TIMEOUT


class TestScanNewTemplates:
    def test_returns_the_newly_analyzed_stems(self):
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, '["corp", "dark"]')
        ):
            assert engine.scan_new_templates() == ["corp", "dark"]

    def test_empty_when_the_engine_is_not_ready(self):
        with mock.patch.object(engine, "run_engine_snippet", return_value=None):
            assert engine.scan_new_templates() == []

    def test_a_non_list_result_is_empty(self):
        """This runs during provisioning, whose contract is "report, never
        raise" — a dict here must not become a TypeError."""
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, '{"a": 1}')
        ):
            assert engine.scan_new_templates() == []

    def test_entries_are_coerced_to_strings(self):
        with mock.patch.object(
            engine, "run_engine_snippet", return_value=engine.EngineResult(0, "[1, 2]")
        ):
            assert engine.scan_new_templates() == ["1", "2"]


class TestIconScripts:
    def test_refuses_to_run_without_the_venv_or_the_script(self, tmp_path: Path):
        """Naming which of the two is missing is the difference between an
        actionable message and a bare non-zero exit."""
        with (
            mock.patch.object(engine.paths, "engine_python", return_value=tmp_path / "absent"),
            mock.patch.object(engine, "_spawn") as spawn,
        ):
            result = engine.run_icon_script("aws", "download_aws_icons.py")
        assert result.returncode == 1
        assert "missing" in result.stderr
        assert not spawn.called

    def test_runs_the_script_in_its_own_directory(self, tmp_path: Path):
        """The engine's icon scripts resolve their output relative to their own
        location, so the cwd is load-bearing."""
        python = tmp_path / "python"
        python.write_text("#!/bin/sh", encoding="utf-8")
        script = tmp_path / "scripts" / "download_aws_icons.py"
        script.parent.mkdir(parents=True)
        script.write_text("print()", encoding="utf-8")
        with (
            mock.patch.object(engine.paths, "engine_python", return_value=python),
            mock.patch.object(engine, "icon_script_path", return_value=script),
            mock.patch.object(engine, "_spawn", return_value=engine.EngineResult(0, "")) as spawn,
        ):
            engine.run_icon_script("aws", "download_aws_icons.py")
        assert spawn.call_args.args[0] == [str(python), str(script)]
        assert spawn.call_args.kwargs["cwd"] == str(script.parent)

    def test_every_declared_icon_source_names_a_script(self):
        """A source with no script would report a permanent provisioning error
        the user could do nothing about."""
        for source, script in engine.ICON_SOURCES:
            assert source and script.endswith(".py")


class TestMissingOptionalDeps:
    def test_reports_only_what_is_absent_from_path(self):
        with mock.patch.object(
            engine.shutil,
            "which",
            side_effect=lambda n, path=None: None if n == "soffice" else "/usr/bin/x",
        ):
            assert engine.missing_optional_deps() == ["soffice"]

    def test_empty_when_every_optional_binary_is_present(self):
        with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/x"):
            assert engine.missing_optional_deps() == []

    def test_a_managed_install_counts_as_present(self):
        """A tool only in the app's managed bin dir is NOT missing.

        The engine sees it because the rendered agent config puts that dir on the MCP
        server's PATH (``provision.mcp_tools_path``), so reporting it as missing would
        warn about a tool that works.
        """
        managed = str(paths.preview_tools_bin() / "pdftoppm")

        def _which(name, path=None):
            # Absent from the process PATH; present when the managed dir is named.
            return managed if (path and name == "pdftoppm") else None

        with mock.patch.object(engine.shutil, "which", side_effect=_which):
            assert "pdftoppm" not in engine.missing_optional_deps()
            assert engine.optional_dep_path("pdftoppm") == managed

    def test_a_real_system_binary_wins_over_the_managed_one(self):
        """Precedence: a user's own poppler must never be shadowed by the shim."""
        with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/pdftoppm"):
            assert engine.optional_dep_path("pdftoppm") == "/usr/bin/pdftoppm"


class TestProvisionState:
    def test_elapsed_is_zero_before_a_run_starts(self):
        """A never-started job must not report an elapsed time measured from the
        epoch, which the UI would render as ~57 years."""
        assert engine.ProvisionState().elapsed() == 0

    def test_elapsed_counts_from_the_start(self):
        state = engine.ProvisionState(started=engine.time.time() - 5)
        assert state.elapsed() >= 5

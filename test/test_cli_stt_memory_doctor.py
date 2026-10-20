"""The Speech-to-Text and Vector Memory sections of ``kirocrew doctor``, and its stale project dir.

The full report (``TestDoctor``) is in ``test_cli.py``; the MCP section is in
``test_cli_mcp_doctor.py``.
"""

import contextlib
import json
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from cli_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _doctor_reads_the_warm_probe_cache,
    _healthy_agent_file,
    _noop_probe_server,
    _pin_default_config,
)

from kiro_crew.cli_doctor import _doctor
from kiro_crew.stt import decoder as stt_decoder


class TestDoctorStaleProjectDir:
    """Tests for doctor stale project_dir detection."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of this class calls monkeypatch.undo()
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)

    def test_doctor_detects_stale_project_dir(self, tmp_path, capsys):
        proj_file = tmp_path / "project_dir"
        proj_file.write_text("/nonexistent/deleted\n")
        agent_file = tmp_path / "kirocrew.json"
        agent_data = {
            "tools": ["@kirocrew-core", "@kirocrew-cron"],
            "allowedTools": ["@kirocrew-core", "@kirocrew-cron"],
            "mcpServers": {
                "kirocrew-core": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-core"]},
                "kirocrew-cron": {"command": "/usr/local/bin/kirocrew", "args": ["mcp-cron"]},
            },
        }
        agent_file.write_text(json.dumps(agent_data))
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch(
                "kiro_crew.cli_doctor.shutil.which",
                side_effect=lambda b, **_kw: f"/usr/local/bin/{b}",
            ),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen"),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch.dict(
                "os.environ",
                {"KIROCREW_PROJECT_DIR": "", "SLACK_APP_TOKEN": "", "SLACK_BOT_TOKEN": ""},
                clear=False,
            ),
        ):
            with pytest.raises(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "stale" in out
        assert "source dir:  ⚠️  not set" not in out  # should NOT show fallback message


class TestDoctorStt:
    """Doctor's Speech-to-Text section, driven through ``_doctor()``.

    Every arm goes through the real entry point rather than a unit call, because
    what is being pinned is both halves of the contract: the report a user reads
    and the exit status their ``kirocrew doctor && kirocrew gateway`` chain
    depends on.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of this class calls monkeypatch.undo()
    def _hermetic_doctor(self, monkeypatch):
        """Pin every section EXCEPT Speech-to-Text.

        Same reasoning as ``TestDoctor``'s fixtures: the vendored embedding runtime
        and the sandbox backend are host facts, and each is a genuine issue on a
        machine that lacks it, which would make an exit-status assertion here report
        the runner instead of the STT arm under test.
        """
        import kiro_crew.cli_doctor as _doc

        _pin_default_config(monkeypatch)
        monkeypatch.setattr(_doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(_doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(_doc.sandbox, "detect_backend", lambda config_mode="auto": "namespace")
        # POSIX severity: a missing prerequisite is a hard issue. The Windows note
        # arm is pinned in ``TestDoctor::test_doctor_stt_marker_arms_match_platform``.
        monkeypatch.setattr(_doc.platform_compat, "IS_WINDOWS", False)
        # Left real, this prepends to the process PATH for every later test.
        monkeypatch.setattr(_doc, "ensure_ffmpeg_in_path", lambda: None)

    @staticmethod
    def _stt(monkeypatch, **fields):
        """Enable STT with *fields* applied over the shipped defaults.

        Applied last, so a test that wants the off arm asks for it explicitly
        (``enabled=False``) rather than relying on what the autouse pin left behind.
        """
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
        from kiro_crew.config.loader import KiroCrewConfig

        def _cfg() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            # kiro-cli, like ``_pin_default_config``: the KAS engine row is not
            # the subject here.
            cfg.agent.acp_backend = ACP_BACKEND_KIRO
            cfg.stt.enabled = True
            for name, value in fields.items():
                setattr(cfg.stt, name, value)
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _cfg()))

    @staticmethod
    def _engine(monkeypatch, availability):
        """Answer the recogniser probe without depending on the optional wheel."""
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc, "availability_detail", lambda cfg=None: availability)

    @staticmethod
    def _model_on_disk(monkeypatch, present):
        """Say whether the resolved catalog model is downloaded.

        Patched on the ``kiro_crew.stt`` namespace the doctor actually reads. The
        real predicate compares the file's size to the catalog's, and the smallest
        entry is 77 MB, so writing one is not an option a test has.
        """
        import kiro_crew.cli_doctor as _doc

        monkeypatch.setattr(_doc.stt, "is_present", lambda model: present)

    @staticmethod
    def _stt_section(out: str) -> str:
        """Just the Speech-to-Text block.

        The embeddings section prints its own ``model:`` line, so an assertion
        about the ABSENCE of one has to be scoped or it proves nothing.
        """
        return out.split("Speech-to-Text", 1)[1].split("Slack Integration", 1)[0]

    def _report(self, tmp_path, capsys, *, ffmpeg=True, modules=None):
        """Run the doctor with everything outside STT stubbed.

        Returns the captured report and the exit status, so a test can assert that
        an STT gap does or does not fail the run.
        """
        _healthy_agent_file(tmp_path / "kirocrew.json")
        mock_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")

        def _which(binary, **_kw):
            if binary == "ffmpeg" and not ffmpeg:
                return None
            return f"/usr/local/bin/{binary}"

        # `ffmpeg` has to answer BOTH routes into `transcribe._find_ffmpeg`. The
        # bundled-interpreter branch never probes PATH: it lists the imageio-ffmpeg
        # package resource, which is a really-installed dev dependency here, so a
        # bundled arm left unpinned reports the test environment's decoder instead of
        # the arrangement it asked for -- and reports it as PRESENT, the permissive
        # answer. The value is unused: the report prints availability, not a path.
        packaged_decoder = "/kirocrew-bundle/Resources/ffmpeg" if ffmpeg else None

        code = 0
        with (
            patch("kiro_crew.cli_doctor.shutil.which", side_effect=_which),
            patch(
                "kiro_crew.transcribe._packaged_ffmpeg_resource",
                return_value=packaged_decoder,
            ),
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=mock_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no gateway")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
            patch("kiro_crew.slack.enterprise.validate_enterprise", return_value=True),
            pytest.MonkeyPatch.context() as _mp,
        ):
            # Fake ONLY the named sys.modules keys, each restored to its exact prior
            # state (present, absent or None) on exit. A whole-dict
            # patch.dict("sys.modules", ...) would instead evict a submodule this
            # run imports for the first time inside the block
            # (kiro_crew.acp.skill_projection, reached lazily via the ACP driver),
            # leaving the kiro_crew.acp package attribute pointing at a now-orphan
            # module object -- cross-test pollution a co-runner then trips over.
            # setitem is the scoped restore testing-conventions D11 mandates.
            for _name, _mod in (modules or {}).items():
                _mp.setitem(sys.modules, _name, _mod)
            try:
                _doctor()
            except SystemExit as exc:
                code = int(exc.code or 0)
        return capsys.readouterr().out, code

    def test_report_module_faking_does_not_evict_lazily_imported_modules(
        self, tmp_path, capsys, monkeypatch
    ):
        """``_report``'s ``modules=`` faking must not corrupt a co-runner's module identity.

        The doctor run inside ``_report`` reaches ``kiro_crew.acp.skill_projection``
        lazily (through the ACP driver), so on a cold xdist worker that submodule is
        imported for the FIRST time inside the module-faking block. The faking must
        leave it in place: after ``_report`` returns, ``sys.modules``, the
        ``kiro_crew.acp`` package attribute and a re-import all have to name one
        module object.

        A whole-dict ``patch.dict("sys.modules", ...)`` restores its entry snapshot
        on exit and so evicts that submodule while the package attribute still
        names it. A later test on the same worker then re-imports it as a second
        object, monkeypatches that one, and the runtime's function-local
        ``from kiro_crew.acp.skill_projection import ...`` resolves the unpatched
        real preparer. This drives the real helper, so it fails on that restore.
        """
        import importlib

        name = "kiro_crew.acp.skill_projection"
        # Drop every trace so the doctor's import is a genuine first import, the
        # window a cold xdist worker hits. monkeypatch puts both back at teardown.
        acp_pkg = importlib.import_module("kiro_crew.acp")
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.delattr(acp_pkg, "skill_projection", raising=False)

        faked = {
            "amazon_transcribe": MagicMock(),
            "amazon_transcribe.client": MagicMock(),
            "boto3": MagicMock(),
        }
        # Each faked key's prior state: real modules on a host with the voice extra,
        # absent on one without. The pin asserts restoration of THIS state, so it is
        # host-independent.
        missing = object()
        before = {k: sys.modules.get(k, missing) for k in faked}

        self._report(tmp_path, capsys, modules=faked)

        for k in faked:
            assert sys.modules.get(k, missing) is before[k]
        # The doctor really did import the submodule inside the block; without this
        # the identity assertions below would pass vacuously.
        survivor = getattr(acp_pkg, "skill_projection", None)
        assert survivor is not None, "the doctor run no longer imports skill_projection"
        assert sys.modules.get(name) is survivor
        assert importlib.import_module(name) is survivor

    def test_doctor_stt_local_engine_and_model_ready(self, tmp_path, capsys, monkeypatch):
        """The ready state names the resolved catalog model AND where it sits, so
        an operator can see which weights a dictation will actually use."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local", model="small")
        self._engine(monkeypatch, _doc.stt.Availability(True))
        self._model_on_disk(monkeypatch, True)

        out, code = self._report(tmp_path, capsys)

        section = self._stt_section(out)
        assert "provider:    ✅ local" in section
        assert "engine:      ✅ local recogniser loadable (whisper.cpp, in-process)" in section
        expected = _doc.stt.models_dir() / "ggml-small.bin"
        assert f"model:       ✅ small at {expected}" in section
        assert "ffmpeg:      ✅ available" in section
        assert code == 0

    def test_doctor_stt_local_model_not_downloaded_is_a_note(self, tmp_path, capsys, monkeypatch):
        """The weights are fetched on first use, so "not downloaded" is the normal
        first-run state and never an issue. Naming the size is the useful part,
        because that transfer is what a first dictation waits on."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local", model="base")
        self._engine(monkeypatch, _doc.stt.Availability(True))
        self._model_on_disk(monkeypatch, False)

        out, code = self._report(tmp_path, capsys)

        # Derived from the catalog entry the doctor resolved, not restated: a
        # literal here goes stale the moment the pinned artifact changes.
        megabytes = _doc.stt.resolve_model("base").size_bytes // 1_000_000
        section = self._stt_section(out)
        assert (
            f"model:       ⏹ base not downloaded yet "
            f"({megabytes} MB, fetched on first use)" in section
        )
        assert code == 0

    def test_doctor_stt_local_engine_failure_names_its_code(self, tmp_path, capsys, monkeypatch):
        """An unloadable recogniser is a hard issue on POSIX, and the summary
        carries the machine-readable code.

        The code is what distinguishes "install the extra" from "this platform has
        no wheel", so a report that only said "speech recogniser" would send the
        user to the wrong fix.
        """
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="local")
        self._engine(
            monkeypatch,
            _doc.stt.Availability(
                False,
                _doc.stt.CODE_NO_WHEEL,
                "no prebuilt speech recogniser for Intel macOS",
            ),
        )

        out, code = self._report(tmp_path, capsys)

        assert "engine:      ❌ no prebuilt speech recogniser for Intel macOS" in out
        assert "❌ Fix these issues: " in out
        assert "speech recogniser (stt_no_wheel_for_platform)" in out
        assert code == 1

    def test_doctor_stt_disabled(self, tmp_path, capsys, monkeypatch):
        """Disabled is a choice, not a gap: no engine or model line is printed,
        and ffmpeg is reported as something this install does not need."""
        self._stt(monkeypatch, enabled=False)

        out, code = self._report(tmp_path, capsys, ffmpeg=False)

        section = self._stt_section(out)
        assert "status:      ⏹ disabled" in section
        assert "ffmpeg:      ⏭  not installed (not needed)" in section
        assert "engine:" not in section
        assert "model:" not in section
        # A prerequisite nothing needs cannot fail the run.
        assert code == 0

    def test_doctor_stt_ffmpeg_is_a_prerequisite_of_every_provider(
        self, tmp_path, capsys, monkeypatch
    ):
        """Reported for a cloud provider too. A Slack voice memo arrives as
        ogg/Opus and the dashboard records webm, so the only input that reaches
        any recogniser without ffmpeg is a 16 kHz mono WAV."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="transcribe")
        monkeypatch.setattr(_doc._plat, "system", lambda: "Linux")

        out, code = self._report(
            tmp_path,
            capsys,
            ffmpeg=False,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        assert "ffmpeg:      ❌ not found" in out
        # The wiring assertion: doctor must print the module constant the
        # resolvable-hint tests hold against the resolver's candidate list, so
        # nobody can inline a literal back into the _os_fix_hint call.
        assert _doc._FFMPEG_LINUX_HINT in out
        assert "reinstall Kiro Crew" not in out
        assert "❌ Fix these issues: " in out
        assert "ffmpeg" in out.split("❌ Fix these issues: ", 1)[1]
        assert code == 1

    def test_doctor_bundled_desktop_never_requests_a_system_ffmpeg_install(
        self, tmp_path, capsys, monkeypatch
    ):
        """A corrupt desktop payload is repaired by reinstalling the app, not by
        asking its user to manage brew, winget, apt, or a loose executable."""
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="transcribe")
        monkeypatch.setattr(_doc.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            stt_decoder, "platform_key", lambda *a, **k: stt_decoder.ARTIFACTS[0].platform_key
        )
        out, _code = self._report(
            tmp_path,
            capsys,
            ffmpeg=False,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        section = self._stt_section(out)
        assert "reinstall Kiro Crew (the bundled audio decoder is missing)" in section
        assert "brew install ffmpeg" not in section
        assert "winget install" not in section

    def test_doctor_release_without_a_bundled_decoder_points_at_a_system_ffmpeg(
        self, tmp_path, capsys, monkeypatch
    ):
        """The macOS Intel release ships no decoder, so reinstalling cannot fix it.

        Its only decoder is a system one, the same as a source install's, and the
        remedy line has to say so instead of blaming a payload that never shipped.
        """
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="transcribe")
        monkeypatch.setattr(_doc.platform_compat, "is_bundled_interpreter", lambda: True)
        monkeypatch.setattr(
            stt_decoder, "platform_key", lambda *a, **k: stt_decoder.DECODERLESS_BUNDLE_PLATFORM
        )
        out, _code = self._report(
            tmp_path,
            capsys,
            ffmpeg=False,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        section = self._stt_section(out)
        assert "ffmpeg:" in section and "not found" in section
        assert "reinstall Kiro Crew" not in section
        assert "Fix: " in section

    def test_doctor_stt_transcribe_provider(self, tmp_path, capsys, monkeypatch):
        """The local-only lines belong to the local provider. Printing an engine or
        model row for a cloud provider would advertise a dependency the operator's
        configuration does not have."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        # boto3 and amazon-transcribe are the OPTIONAL 'voice' extra and are not
        # ambiently importable in a clean env, so the ✅ arm has to be faked or the
        # assertion below depends on what the host happens to have installed.
        out, code = self._report(
            tmp_path,
            capsys,
            modules={
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": MagicMock(),
            },
        )

        section = self._stt_section(out)
        assert "provider:    ✅ transcribe" in section
        assert "transcribe:  ✅" in section
        assert "boto3:       ✅" in section
        assert "engine:" not in section
        assert "model:" not in section
        assert code == 0

    def test_doctor_stt_transcribe_amazon_transcribe_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and amazon_transcribe is not importable,
        doctor reports it as an OPTIONAL gap (public pip extra) and does NOT
        treat it as a hard failure."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        # setitem(sys.modules, ..., None) is the documented way to make an import
        # raise for a package that is already loaded at test time.
        out, code = self._report(tmp_path, capsys, modules={"amazon_transcribe.client": None})

        assert "transcribe:  ⏹ optional cloud STT not installed" in out
        # The cloud dependencies by name: `pip install kirocrew[voice]` resolves
        # nowhere (this project is on no index), and it would also drag in the
        # local recogniser this provider does not use.
        assert "amazon-transcribe" in out
        assert "kirocrew[" not in out
        assert code == 0

    def test_doctor_stt_transcribe_boto3_missing(self, tmp_path, capsys, monkeypatch):
        """When provider=transcribe and boto3 is not importable, doctor
        reports it as an OPTIONAL gap (public pip extra), not a hard failure."""
        self._stt(monkeypatch, provider="transcribe", transcribe_region="us-west-2")
        out, code = self._report(
            tmp_path,
            capsys,
            modules={
                # amazon_transcribe importable, to isolate the boto3 gap.
                "amazon_transcribe": MagicMock(),
                "amazon_transcribe.client": MagicMock(),
                "boto3": None,
            },
        )

        assert "boto3:       ⏹ optional AWS SDK not installed" in out
        # The cloud dependencies by name: `pip install kirocrew[voice]` resolves
        # nowhere (this project is on no index), and it would also drag in the
        # local recogniser this provider does not use.
        assert "amazon-transcribe" in out
        assert "kirocrew[" not in out
        assert code == 0

    def test_doctor_stt_apple_unsupported_host_names_its_code(self, tmp_path, capsys, monkeypatch):
        """Reaching a not-ok state here means the operator selected a provider this
        machine does not support, which is a real configuration fault rather than a
        first-run state, so it carries its code into the summary."""
        import kiro_crew.cli_doctor as _doc
        from kiro_crew.transcribe import CODE_APPLE_UNSUPPORTED

        self._stt(monkeypatch, provider="apple")
        self._engine(
            monkeypatch,
            _doc.stt.Availability(
                False,
                CODE_APPLE_UNSUPPORTED,
                "Apple on-device speech is macOS only",
            ),
        )

        out, code = self._report(tmp_path, capsys)

        section = self._stt_section(out)
        assert "apple:       ❌ Apple on-device speech is macOS only" in section
        # The local provider's rows must not appear for a host capability.
        assert "engine:" not in section
        assert "apple speech (stt_apple_unsupported)" in out
        assert code == 1

    def test_doctor_stt_apple_available(self, tmp_path, capsys, monkeypatch):
        import kiro_crew.cli_doctor as _doc

        self._stt(monkeypatch, provider="apple")
        self._engine(monkeypatch, _doc.stt.Availability(True))

        out, code = self._report(tmp_path, capsys)

        assert "apple:       ✅ on-device SpeechAnalyzer available" in out
        assert code == 0


class TestDoctorEmbeddings:
    """Tests for the doctor Vector Memory (in-process embeddings) section."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of this class calls monkeypatch.undo()
    def _hermetic_config(self, monkeypatch):
        """Pin config to a pristine default (see ``_pin_default_config``)."""
        _pin_default_config(monkeypatch)
        # LLAMA_CPP_LIB_PATH selects between two mutually exclusive diagnoses:
        # the bundled tree or an operator override. Each test owns whether the
        # live environment or the startup-removal record supplies that value.
        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)

    @staticmethod
    def _run_doctor(
        tmp_path,
        monkeypatch,
        *,
        runtime_ok: bool,
        model_present: bool,
        platform_supported: bool = True,
        missing_libs: dict | None = None,
        lib_path_override: str | None = None,
        dropped_lib_path: str | None = None,
    ):
        """Run _doctor with the embedding and library-path state stubbed.

        ``lib_path_override`` controls the live environment. ``dropped_lib_path``
        controls the startup record used when the variable is absent.
        """
        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        import kiro_crew.cli_doctor as doc

        if lib_path_override is None:
            # The setenv+delenv pair records absence for teardown even when the
            # inherited process environment did not contain the variable.
            monkeypatch.setenv("LLAMA_CPP_LIB_PATH", "")
            monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
        else:
            monkeypatch.setenv("LLAMA_CPP_LIB_PATH", lib_path_override)

        monkeypatch.setattr(doc, "dropped_inherited_lib_path", lambda: dropped_lib_path)
        monkeypatch.setattr(doc, "_load_llama_class", lambda: object if runtime_ok else None)
        monkeypatch.setattr(
            doc, "_platform_libs_dirname", lambda: "macos_arm64" if platform_supported else None
        )
        monkeypatch.setattr(doc, "verify_vendored_libs", lambda: missing_libs or {})
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: model_present)
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()

    def test_doctor_reports_runtime_and_missing_model(self, tmp_path, capsys, monkeypatch):
        """Vendored runtime loads but no model file -> runtime OK, model pending."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=True, model_present=False)
        out = capsys.readouterr().out
        assert "runtime:     ✅ vendored llama-cpp-python importable" in out
        assert "model:       ⏹ not downloaded yet" in out
        assert "embeddings:  ✅ always-on" in out

    def test_doctor_reports_runtime_failure(self, tmp_path, capsys, monkeypatch):
        """Runtime import failing on a SUPPORTED platform is surfaced as an issue."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=False, model_present=False)
        out = capsys.readouterr().out
        assert "runtime:     ❌ vendored runtime failed to load" in out
        # A COMPLETE payload that still fails to load must not be blamed on
        # packaging — that would send the user reinstalling for nothing.
        assert "incomplete" not in out

    def test_doctor_names_the_missing_native_libs(self, tmp_path, capsys, monkeypatch):
        """An incomplete shipped payload names the absent files.

        ctypes reports only "base name 'llama' not found", which reads as an
        unsupported architecture — so a bare "failed to load" sends diagnosis
        after the CPU arch instead of the packaging rule that dropped the file
        on every arch.
        """
        # This test asserts the NO-override branch, so the var must be absent.
        # It is not, reliably: the doctor branches on LLAMA_CPP_LIB_PATH and the
        # value can arrive from outside this test -- a dev shell that exports it,
        # or the sibling override test, whose helper sets it via a raw
        # os.environ.setdefault that escapes pytest teardown. Either way the
        # doctor takes the "libs load from the override dir" path and this
        # assertion can never match. Clearing it here (mirroring the sibling,
        # which explicitly setenv-s) makes the expectation independent of both
        # the ambient environment and test order -- pytest-split reshuffles
        # shards whenever the suite test count changes, so the leak surfaced on
        # an unrelated PR that merely added tests elsewhere.
        monkeypatch.delenv("LLAMA_CPP_LIB_PATH", raising=False)
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
        )
        out = capsys.readouterr().out
        assert "Missing native libs for macos_arm64: libllama.dylib" in out
        assert "packaging" in out

    def test_doctor_blames_the_override_dir_not_the_bundled_tree(
        self, tmp_path, capsys, monkeypatch
    ):
        """Under LLAMA_CPP_LIB_PATH, point at the override — not a reinstall.

        The libs load from the operator's directory, so "reinstall Kiro Crew"
        would send them to replace a package they are deliberately not loading
        from, while saying nothing about the dir that actually failed. Mirrors
        the loader's exemption so the two diagnostics cannot disagree.
        """
        monkeypatch.setenv("LLAMA_CPP_LIB_PATH", "/opt/my-gpu-llama")
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
            lib_path_override="/opt/my-gpu-llama",
        )
        out = capsys.readouterr().out
        assert "/opt/my-gpu-llama" in out
        assert "Missing native libs" not in out
        assert "reinstall Kiro Crew" not in out

    @pytest.mark.parametrize("value_source", ["environment", "startup-record"])
    def test_doctor_does_not_mistake_an_inherited_bundled_path_for_an_override(
        self, tmp_path, capsys, monkeypatch, value_source: str
    ):
        """The live and recorded forms both diagnose this install's bundled tree."""
        inherited = (
            "/opt/kirocrew/0.7.9/lib/python3.12/site-packages/kiro_crew/_vendor/"
            "llama_cpp_libs/macos_arm64"
        )
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            missing_libs={"macos_arm64": ["libllama.dylib"]},
            lib_path_override=inherited if value_source == "environment" else None,
            dropped_lib_path=inherited if value_source == "startup-record" else None,
        )
        out = capsys.readouterr().out

        assert "not the bundled tree" not in out
        assert "Missing native libs for macos_arm64: libllama.dylib" in out
        assert f"LLAMA_CPP_LIB_PATH={inherited}" in out
        if value_source == "startup-record":
            assert "inherited from another Kiro Crew process and removed at startup" in out
        else:
            assert "is ignored; this install uses its own bundled tree" in out
        assert "point it at a directory of your own" in out

    @pytest.mark.parametrize("value_source", ["environment", "startup-record"])
    def test_doctor_reports_an_empty_lib_path_as_unset_not_inherited(
        self, tmp_path, capsys, monkeypatch, value_source: str
    ):
        """An empty value is reported as unset on either path, never as inherited.

        The startup record only establishes that the variable was empty and
        removed; a live empty value is simply treated as unset. Neither may
        claim it came from another Kiro Crew process.
        """
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=True,
            model_present=True,
            lib_path_override="" if value_source == "environment" else None,
            dropped_lib_path="" if value_source == "startup-record" else None,
        )
        out = capsys.readouterr().out

        assert "lib path:    ⏹ LLAMA_CPP_LIB_PATH=(empty)" in out
        assert "inherited from another Kiro Crew process" not in out
        if value_source == "startup-record":
            assert "was empty and removed at startup; treated as unset." in out
            assert "is empty, so it is treated as unset." not in out
        else:
            assert "is empty, so it is treated as unset." in out
            assert "was empty and removed at startup; treated as unset." not in out
        assert "point it at a directory of your own" in out

    def test_doctor_unsupported_platform_is_not_an_issue(self, tmp_path, capsys, monkeypatch):
        """No vendored libs for this platform = designed degradation, not a doctor failure."""
        self._run_doctor(
            tmp_path,
            monkeypatch,
            runtime_ok=False,
            model_present=False,
            platform_supported=False,
        )
        out = capsys.readouterr().out
        assert "runtime:     ⏹ unsupported platform" in out
        assert "embedding runtime" not in out

    def test_doctor_reports_model_present(self, tmp_path, capsys, monkeypatch):
        """Model file present -> reported with its path."""
        self._run_doctor(tmp_path, monkeypatch, runtime_ok=True, model_present=True)
        out = capsys.readouterr().out
        assert "model:       ✅" in out

    def test_doctor_probes_model_url_when_model_absent(self, tmp_path, capsys, monkeypatch):
        """Fork-added: with the model absent, doctor probes the resolved model URL."""
        import kiro_crew.cli_doctor as doc

        probed: list[str] = []

        def _fake_urlopen(req, timeout=None, **kw):
            url = getattr(req, "full_url", str(req))
            probed.append(url)
            if "cloudfront" in url or "mirror" in url:
                resp = MagicMock(status=200)
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=resp)
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            raise urllib.error.URLError("no")

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        monkeypatch.setattr(doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(doc, "_resolve_model_url", lambda: "https://mirror.example/m.gguf")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "https://mirror.example/m.gguf" in probed
        assert "model url:   ✅ reachable" in out

    def test_doctor_reports_unreachable_model_url(self, tmp_path, capsys, monkeypatch):
        """Fork-added: an unreachable model URL is flagged with a fix hint."""
        import kiro_crew.cli_doctor as doc

        agent_file = tmp_path / "kirocrew.json"
        _healthy_agent_file(agent_file)
        monkeypatch.setattr(doc, "_load_llama_class", lambda: object)
        monkeypatch.setattr(doc, "model_file_present", lambda path=None: False)
        monkeypatch.setattr(doc, "_resolve_model_url", lambda: "https://mirror.example/m.gguf")
        default_run = MagicMock(returncode=0, stdout="kiro-cli 1.0.0", stderr="")
        with (
            patch("kiro_crew.cli_doctor.KIRO_AGENTS_DIR", tmp_path),
            patch("kiro_crew.cli_doctor.subprocess.run", return_value=default_run),
            patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no")),
            patch("kiro_crew.cli_doctor.is_local_only", return_value=True),
            patch("kiro_crew.cli_doctor.config_dir", return_value=tmp_path),
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_noop_probe_server),
        ):
            with contextlib.suppress(SystemExit):
                _doctor()
        out = capsys.readouterr().out
        assert "model url:   ❌ unreachable" in out
        assert "Check network connectivity" in out

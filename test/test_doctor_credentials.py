"""The doctor Credentials section — the self-service answer to "AWS is unavailable".

Advisory by construction: an unconfigured AWS profile is not a Kiro Crew fault, so
every case here also asserts that ``issues`` stays empty. A regression that made
this section blocking would turn ``doctor`` red on every host that does not use
AWS.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from kiro_crew import cli_doctor


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(cli_doctor, "_credential_vendor_line", lambda: "")
    # Default to "no AWS CLI to ask": a case that is not ABOUT the probes must
    # not spawn one, and `None` is the shape that means "could not ask".
    monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: None)
    monkeypatch.setattr(cli_doctor, "_aws_auto_refreshes", lambda: False)
    return tmp_path


class TestCredentialsSection:
    def test_no_aws_config_is_reported_without_failing(self, fake_home, capsys):
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert "Credentials" in out
        assert "no ~/.aws config" in out
        assert issues == []

    def test_profiles_are_listed(self, fake_home, monkeypatch, capsys):
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[default]\n")
        monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: ["default", "build"])
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert "default" in out
        assert "build" in out
        assert issues == []

    def test_profile_set_is_unknown_without_the_cli(self, fake_home, capsys):
        """``None`` means "could not ask", which is not "there are none".

        The files exist and the config is not ours to parse, so the honest report
        names the gap instead of implying an empty profile set.
        """
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[default]\n")
        cli_doctor._doctor_credentials([])
        assert "install the AWS CLI to list profiles" in capsys.readouterr().out

    def test_credential_process_is_called_out(self, fake_home, monkeypatch, capsys):
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile p]\n")
        monkeypatch.setattr(cli_doctor, "_aws_auto_refreshes", lambda: True)
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert "credential_process configured" in out
        assert issues == []

    def test_absent_credential_process_is_reported(self, fake_home, capsys):
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile p]\n")
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        assert "no credential_process" in capsys.readouterr().out
        assert issues == []

    def test_credentials_file_alone_still_reports_a_profile(self, fake_home, monkeypatch, capsys):
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "credentials").write_text("[default]\n")
        monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: [])
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        assert "default" in capsys.readouterr().out
        assert issues == []

    def test_nothing_under_dot_aws_is_opened(self, fake_home, monkeypatch, capsys):
        """The section must not READ a single byte out of ``~/.aws``.

        That directory is fenced from the agent by the sensitive-path floor, and
        ``kirocrew doctor`` is reachable from a tool call — so parsing the config
        here would hand back through a diagnostic exactly what the floor refuses
        directly. Existence probes are fine; opening is not. Asserted on the OPEN
        rather than on the printed output, because "no secret appeared in stdout"
        would still pass while the bytes were being read.
        """
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile p]\ncredential_process = /usr/bin/vend\n")
        (aws / "credentials").write_text("[p]\naws_secret_access_key = SUPERSECRETVALUE\n")
        opened: list[str] = []
        real_read_text = Path.read_text
        real_open = Path.open
        real_builtin_open = builtins.open

        def _record_read_text(self, *a, **kw):
            opened.append(str(self))
            return real_read_text(self, *a, **kw)

        def _record_open(self, *a, **kw):
            opened.append(str(self))
            return real_open(self, *a, **kw)

        def _record_builtin_open(file, *a, **kw):
            # `configparser.read()` — the shape this fix removed — goes through the
            # BUILTIN open, not through either Path method. Without this the guard
            # has a hole the exact size of the original bug: reintroducing that
            # read would leave the test green.
            opened.append(str(file))
            return real_builtin_open(file, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _record_read_text, raising=True)
        monkeypatch.setattr(Path, "open", _record_open, raising=True)
        monkeypatch.setattr(builtins, "open", _record_builtin_open, raising=True)
        cli_doctor._doctor_credentials([])
        assert not [p for p in opened if ".aws" in p], f"the section opened {opened}"

    def test_no_secret_value_is_printed(self, fake_home, capsys):
        """Belt to the brace above: even the profile NAME channel carries no value."""
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[profile p]\nregion = us-west-2\n")
        (aws / "credentials").write_text("[p]\naws_secret_access_key = SUPERSECRETVALUE\n")
        cli_doctor._doctor_credentials([])
        out = capsys.readouterr().out
        assert "SUPERSECRETVALUE" not in out
        assert "aws_secret_access_key" not in out

    def test_the_agent_capability_note_is_always_present(self, fake_home, capsys):
        """This line is the point of the section — it must not be conditional."""
        cli_doctor._doctor_credentials([])
        assert "cannot READ credential files" in capsys.readouterr().out

    def test_an_unconfigured_host_is_not_told_the_block_is_the_likelier_cause(
        self, fake_home, capsys
    ):
        """The closing line used to fire unconditionally, including here.

        Two lines after doctor says "no ~/.aws config", it told the operator the
        agent had "most likely hit the credential-file block rather than a missing
        setup" — steering them away from the very fix the line above names. On a
        host with nothing configured the missing setup IS the answer.
        """
        cli_doctor._doctor_credentials([])
        out = capsys.readouterr().out
        assert "no ~/.aws config" in out
        assert "rather than a missing setup" not in out
        assert "reporting the truth" in out

    def test_a_configured_host_still_gets_the_misdiagnosis_pointer(
        self, fake_home, monkeypatch, capsys
    ):
        """Where it IS true, it must survive — the section exists for this case."""
        aws = fake_home / ".aws"
        aws.mkdir()
        (aws / "config").write_text("[default]\n")
        monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: ["default"])
        cli_doctor._doctor_credentials([])
        out = capsys.readouterr().out
        assert "rather than a missing setup" in out
        assert "blocked-commands" in out

    def test_vendor_line_is_shown_when_the_edition_has_one(self, fake_home, monkeypatch, capsys):
        monkeypatch.setattr(
            cli_doctor, "_credential_vendor_line", lambda: "may vend credentials (creds-agent)"
        )
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert "vending MCP" in out
        assert "creds-agent" in out
        assert issues == []


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def spawns(monkeypatch):
    """Record every argv the probes would run, without running one.

    The two resolvers are stubbed to DIFFERENT paths on purpose. The probes must
    resolve through ``trusted_system_bin``, so every argv assertion below doubles
    as a guard: a regression to ``PATH`` resolution shows up as the
    agent-writable path in ``recorded`` instead of passing silently.
    """
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        cli_doctor.platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: f"/agent/writable/{name}")

    def _install(result):
        def _run(argv, **kw):
            recorded.append(list(argv))
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(cli_doctor.subprocess, "run", _run)
        return recorded

    return _install


class TestProfileProbeUsesTheSanctionedPath:
    def test_argv_is_fixed_and_interpolates_nothing(self, spawns):
        recorded = spawns(_Proc(stdout="default\nbuild\n"))
        assert cli_doctor._aws_profile_names() == ["default", "build"]
        assert recorded == [["/usr/bin/aws", "configure", "list-profiles"]]

    def test_duplicates_and_blank_lines_are_dropped(self, spawns):
        spawns(_Proc(stdout="a\n\na\nb\n  \n"))
        assert cli_doctor._aws_profile_names() == ["a", "b"]

    def test_no_cli_means_could_not_ask(self, monkeypatch):
        """And a trusted-resolver miss must NOT fall back to ``PATH``.

        ``shutil.which`` deliberately still answers here: an ``or
        shutil.which(...)`` fallback added later would reopen the planted-shim
        hole while looking like a robustness improvement, so the "no CLI" case is
        the one that catches it.

        The spawn assertion is what makes that catchable. ``is None`` alone is
        satisfied two ways — never spawned, or spawned an agent-writable path
        whose failure the ``except`` swallowed — so the fallback mutation stayed
        green against the result check and only reddens against this one.
        """
        spawned: list[list[str]] = []
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_system_bin", lambda name: None)
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: f"/agent/writable/{name}")
        monkeypatch.setattr(
            cli_doctor.subprocess,
            "run",
            lambda argv, **kw: spawned.append(list(argv)) or _Proc(stdout="default\n"),
        )
        assert cli_doctor._aws_profile_names() is None
        assert spawned == [], f"probe fell back to PATH and spawned {spawned}"

    @pytest.mark.parametrize(
        "result",
        [_Proc(returncode=1, stdout="boom"), OSError("no exec"), Exception("timeout")],
    )
    def test_a_failed_probe_is_could_not_ask_not_empty(self, spawns, result):
        """A failure must not read as "this host has no profiles"."""
        spawns(result)
        assert cli_doctor._aws_profile_names() is None


class TestAutoRefreshProbe:
    def test_a_configured_process_is_reported(self, spawns):
        recorded = spawns(_Proc(stdout="/usr/bin/vend\n"))
        assert cli_doctor._aws_auto_refreshes() is True
        assert recorded == [["/usr/bin/aws", "configure", "get", "credential_process"]]

    @pytest.mark.parametrize(
        "result",
        [_Proc(stdout="  \n"), _Proc(returncode=1, stdout="/usr/bin/vend"), OSError("no exec")],
    )
    def test_anything_short_of_a_value_is_false(self, spawns, result):
        spawns(result)
        assert cli_doctor._aws_auto_refreshes() is False

    def test_no_cli_is_false(self, monkeypatch):
        """A trusted-resolver miss must not fall back to ``PATH`` here either.

        Same reasoning as the profile probe: ``is False`` is reachable through a
        swallowed failure, so the spawn list is the assertion that bites.
        """
        spawned: list[list[str]] = []
        monkeypatch.setattr(cli_doctor.platform_compat, "trusted_system_bin", lambda name: None)
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: f"/agent/writable/{name}")
        monkeypatch.setattr(
            cli_doctor.subprocess,
            "run",
            lambda argv, **kw: spawned.append(list(argv)) or _Proc(stdout="/usr/bin/mint\n"),
        )
        assert cli_doctor._aws_auto_refreshes() is False
        assert spawned == [], f"probe fell back to PATH and spawned {spawned}"


class TestTheProbeDoesNotFollowARelocatedConfig:
    """Asserted at the CALL SITE, not just on the helper.

    The existence half of the section reads ``~/.aws`` while the profile half asks
    the CLI, and a subprocess inherits the environment — so a relocated
    ``AWS_CONFIG_FILE`` had the two halves describing different files. The agent
    sandbox sets exactly that, and on such a host doctor reported a profile absent
    from the operator's config while its own probe said that config does not exist.
    """

    RELOCATORS = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")

    def test_helper_drops_the_redirects_but_keeps_the_rest(self, monkeypatch):
        for var in self.RELOCATORS:
            monkeypatch.setenv(var, "/somewhere/else/config")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = cli_doctor._aws_probe_env()
        for var in self.RELOCATORS:
            assert var not in env
        assert env["PATH"] == "/usr/bin", "the probe still has to be able to find aws"

    @pytest.mark.parametrize(
        "probe", ["_aws_profile_names", "_aws_auto_refreshes"], ids=["profiles", "refresh"]
    )
    def test_neither_probe_passes_the_redirects_to_the_cli(self, monkeypatch, probe):
        for var in self.RELOCATORS:
            monkeypatch.setenv(var, "/somewhere/else/config")
        inherited = "<inherited>"
        seen: list[object] = []

        def _run(argv, **kw):
            # Recorded RAW: omitting `env=` means the child inherits everything,
            # which is the defect. Coercing a missing env to `{}` first would make
            # this guard inert — an empty dict trivially satisfies "the redirect is
            # not in here", so the mutation that drops `env=` would stay green.
            seen.append(kw.get("env", inherited))
            return _Proc(stdout="default\n")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}"
        )
        monkeypatch.setattr(cli_doctor.subprocess, "run", _run)
        getattr(cli_doctor, probe)()
        assert seen, "the probe did not spawn"
        for env in seen:
            assert isinstance(env, dict), f"{probe} inherited the environment instead of scoping it"
            for var in self.RELOCATORS:
                assert var not in env, f"{probe} let the CLI follow {var}"


class TestNeitherProbeResolvesThroughPath:
    """The probes run under an OPERATOR's ``kirocrew doctor``, outside the sandbox.

    A gateway's ``PATH`` can legitimately lead with a directory the agent itself
    can write — a worktree venv's ``bin``, ``~/.local/bin`` — so resolving a bare
    ``aws`` through ``PATH`` lets the agent plant a shim that then executes with
    the operator's environment. ``platform_compat.trusted_system_bin`` exists for
    exactly this and ignores ``PATH``.

    Pinned as its own class rather than left to the ``spawns`` fixture: the
    fixture's stubs make every argv assertion catch this incidentally, but an
    invariant nobody can find by name is one a later refactor un-picks.
    """

    @pytest.mark.parametrize(
        "probe", ["_aws_profile_names", "_aws_auto_refreshes"], ids=["profiles", "refresh"]
    )
    def test_the_trusted_resolver_decides_the_executable(self, monkeypatch, probe):
        seen: list[str] = []

        def _run(argv, **kw):
            seen.append(argv[0])
            return _Proc(stdout="default\n")

        monkeypatch.setattr(
            cli_doctor.platform_compat, "trusted_system_bin", lambda name: "/usr/bin/aws"
        )
        monkeypatch.setattr(cli_doctor.shutil, "which", lambda name: "/agent/writable/aws")
        monkeypatch.setattr(cli_doctor.subprocess, "run", _run)
        getattr(cli_doctor, probe)()
        assert seen == ["/usr/bin/aws"], f"{probe} resolved the executable through PATH"


class TestOperatorCopyPointsSomewhereReachable:
    """Every pointer doctor prints has to be followable by the reader in front of it.

    This section prints on every run where ``~/.aws`` exists, so a pointer an
    operator cannot follow is recurring friction rather than a one-off blemish —
    it teaches them the diagnostic is unreliable.
    """

    def test_the_doc_pointer_is_a_url_not_a_dashboard_page_or_repo_path(
        self, fake_home, monkeypatch, capsys
    ):
        """Neither of the earlier two spellings existed for a pip-installed operator.

        The dashboard has no Docs surface at all (packaged docs are reached as
        GitHub links), and ``src/kiro_crew/...`` is a repo path absent from a wheel
        install. Asserting on the URL alone would not catch a regression that ADDED
        a dead pointer beside it, so both dead spellings are asserted absent.
        """
        (fake_home / ".aws").mkdir()
        (fake_home / ".aws" / "config").write_text("[default]\n", encoding="utf-8")
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert cli_doctor._BLOCKED_COMMANDS_DOC_URL in out
        # On ONE line. The first attempt at this fix put the URL inside the width
        # wrapper, which split it at a hyphen — present in the output, unusable to
        # the reader, and a substring check alone was blind to it.
        assert any(
            cli_doctor._BLOCKED_COMMANDS_DOC_URL in line for line in out.splitlines()
        ), "the URL is split across lines and cannot be copied out"
        assert "dashboard's Docs" not in out, "points at a surface the dashboard lacks"
        assert "src/kiro_crew/docs/blocked-commands.md." not in out, "bare repo path"
        assert issues == []

    def test_the_profiles_row_aligns_with_its_siblings(self, fake_home, monkeypatch, capsys):
        """The status glyph must not butt against the colon.

        Sibling rows pad the label to a fixed column (``refresh:`` and friends), and
        ``aws profiles:`` reached that column with ZERO gap, so its glyph touched the
        punctuation while every neighbour had a space.
        """
        (fake_home / ".aws").mkdir()
        (fake_home / ".aws" / "config").write_text("[default]\n", encoding="utf-8")
        monkeypatch.setattr(cli_doctor, "_aws_profile_names", lambda: ["default"])
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        rows = [ln for ln in capsys.readouterr().out.splitlines() if ":" in ln and "  " in ln]
        labelled = [ln for ln in rows if ln.startswith("  ") and not ln.startswith("   ")]
        assert labelled, "no labelled rows rendered"
        for line in labelled:
            head, _, rest = line.partition(":")
            if not rest:
                continue
            assert rest.startswith(" "), f"no gap after the label in {line!r}"

    def test_the_unconfigured_branch_does_not_hand_every_host_an_aws_todo(self, fake_home, capsys):
        """`doctor` runs on hosts that will never use AWS; the setup step is conditional."""
        issues: list[str] = []
        cli_doctor._doctor_credentials(issues)
        out = capsys.readouterr().out
        assert "If you use AWS, run" in out
        assert "the files. Run `aws configure" not in out, "reads as an unconditional to-do"


class TestVendorLineIsFailSoft:
    def test_it_is_phrased_for_the_operator_not_the_agent(self, monkeypatch):
        """The doctor reader is a human who cannot call an MCP tool.

        This line used to be `credential_tool_hint()` verbatim — prose addressed to
        the agent, telling its reader to "prefer one of those and then run the
        command normally" and that it SUPERSEDES "the guidance above", which is a
        refusal notice the operator does not have on screen. Only the server ids
        are shared between the two audiences now.
        """

        class _Manager:
            def available(self) -> bool:
                return True

            async def list_mcp(self):
                return [{"server_id": "creds-agent", "description": "vends AWS credentials"}]

        monkeypatch.setattr(
            cli_doctor.platform_context,
            "safe_context_call",
            lambda fn, **kw: _Manager(),
            raising=True,
        )
        line = cli_doctor._credential_vendor_line()
        assert "creds-agent" in line
        for agent_voiced in ("Prefer one of those", "SUPERSEDES", "guidance above", "you"):
            assert agent_voiced not in line, f"operator sees agent-voiced prose: {agent_voiced}"

    def test_public_edition_yields_no_line(self):
        """The public default reports available() False, so nothing is probed."""
        assert cli_doctor._credential_vendor_line() == ""

    def test_a_lookup_error_degrades_to_no_line(self, monkeypatch):
        import kiro_crew.platform.context as ctx_mod

        def _boom(fn, **kw):
            raise RuntimeError("composition exploded")

        monkeypatch.setattr(ctx_mod, "safe_context_call", _boom, raising=True)
        assert cli_doctor._credential_vendor_line() == ""

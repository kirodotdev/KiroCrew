"""``kirocrew doctor``'s model, channel and project sections.

The Model section, the WhatsApp Integration section, the Project labels with
the local-bind auth row, and the platform-scope name-grant row.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew import cli_doctor


class TestEffectiveModelSection:
    """`kirocrew doctor`'s Model section.

    The four-tier model precedence is not visible from any single file, so a
    stale spec pin that outlived the setting which created it is otherwise only
    diagnosable by hand-reading config.json, two agent-spec directories and the
    sidecar. This section names the winning tier and, when a pin is deciding,
    the exact command that clears it.

    ISOLATION: the section reads the directory the RESOLVER reads, and that
    resolver is ``kiro_home()``, which the suite's autouse fixtures deliberately
    do NOT pin (see the note in the rootdir conftest) -- it resolves the real
    machine-wide ``~/.kiro``. So every test here sets ``KIRO_HOME`` itself, and
    ``_agents_dir`` asserts the resolved path really is under tmp before writing
    a byte. Without that guard these tests overwrite the operator's live agent
    spec.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the Model section's autouse KIRO_HOME pin, moved verbatim; no test in test_cli_doctor_config_sections.py calls monkeypatch.undo()
    def _isolate_kiro_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro-home"))
        self._tmp = tmp_path

    def _agents_dir(self) -> Path:
        from kiro_crew.config.paths import kiro_agents_dir

        agents_dir = kiro_agents_dir()
        # Fail loudly rather than write into a real home if the override lapses.
        assert self._tmp in agents_dir.parents or agents_dir.is_relative_to(
            self._tmp
        ), f"KIRO_HOME isolation failed: {agents_dir} is outside {self._tmp}"
        agents_dir.mkdir(parents=True, exist_ok=True)
        return agents_dir

    def _cfg(self, global_model: str):
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent.model = global_model
        return cfg

    def _install_spec(self, model: str | None) -> Path:
        from kiro_crew.agent import AGENT_FILENAME

        body: dict = {"name": "kirocrew"}
        if model is not None:
            body["model"] = model
        spec = self._agents_dir() / AGENT_FILENAME
        spec.write_text(json.dumps(body), encoding="utf-8")
        return spec

    def test_spec_pin_decides_when_the_global_defers(self, capsys) -> None:
        """The reported symptom: the global says auto, so the spec pin decides
        and the report says so instead of leaving the user to work it out."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-opus-4.8'" in out
        assert "decided by:  default spec pin" in out
        assert "kirocrew agent reset-model" in out
        # Advisory, not a setup failure: the state is legal and may be wanted.
        assert issues == []

    def test_explicit_global_outranks_the_spec_pin(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("claude-haiku-4.5"), "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-haiku-4.5'" in out
        assert "decided by:  global agent.model" in out
        # No pin is deciding, so no repair is offered.
        assert "reset-model" not in out
        assert issues == []

    def test_report_and_resolver_agreement_is_asserted(self, capsys) -> None:
        """The self-check must stay silent while the two agree -- if this line
        ever fires it means the tier list drifted from the resolver."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "out of date" not in capsys.readouterr().out
        assert issues == []

    def test_tracking_state_is_reported(self, capsys) -> None:
        from kiro_crew import agent_state

        agent_state.set_model_managed("kirocrew", False)
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "tracking:    frozen (explicit pick)" in capsys.readouterr().out

    def test_unrecorded_tracking_is_named(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "tracking:    not recorded" in capsys.readouterr().out

    def test_unreadable_spec_is_reported_not_swallowed(self, capsys) -> None:
        from kiro_crew.agent import AGENT_FILENAME

        (self._agents_dir() / AGENT_FILENAME).write_text("{ not json", encoding="utf-8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "unreadable" in capsys.readouterr().out
        assert issues == ["agent spec unreadable"]

    def test_project_local_spec_is_flagged_as_shadowing(self, capsys) -> None:
        """kiro-cli resolves <project>/.kiro/agents FIRST and Kiro Crew's own
        resolver never reads it, so that file can decide what actually runs while
        every Kiro Crew surface reports something else."""
        from kiro_crew.agent import AGENT_FILENAME

        self._install_spec(None)
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), str(project), issues)

        out = capsys.readouterr().out
        assert "project spec" in out
        assert "claude-opus-4.8" in out
        assert "kiro-cli loads this one first" in out
        assert issues == ["project-local agent spec shadows the user-level one"]

    def test_no_project_dir_prints_no_project_line(self, capsys) -> None:
        self._install_spec(None)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)
        assert "project spec" not in capsys.readouterr().out
        assert issues == []

    def _bind_custom_agent(self, cfg, name: str):
        """Make a non-built-in kiro agent the default TEMPLATE.

        A plain session runs `agent.default_agent`; the `default` crewmate's
        own `kiro_agent` is that crewmate's, not the session's, so it is left
        pointing elsewhere to prove the report reads the right knob.
        """
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg.agent.default_agent = name
        cfg.default_agent = "default"
        cfg.agents["default"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        return cfg

    def test_a_bound_custom_agent_is_attributed_to_its_own_spec(self, capsys) -> None:
        """The default template may be a kiro agent other than the built-in one,
        and the resolver consults THAT spec's pin above the global (tier 2).
        Reading kirocrew.json in both cases attributed the pin to the wrong file
        and printed a reset command for the wrong agent."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom.agent.json").write_text(
            json.dumps({"name": "custom.agent", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom.agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "effective:   'claude-opus-4.8'" in out
        assert "decided by:  bound agent pin ('custom.agent')" in out
        # The repair must name the agent that actually holds the pin.
        assert "kirocrew agent reset-model --agent 'custom.agent'" in out
        # And the tier the resolver skipped for the built-in agent is shown here.
        assert "bound agent pin ('custom.agent'):" in out
        assert "out of date" not in out, "report must agree with the resolver"
        assert issues == []

    def test_a_bound_markdown_agent_shows_the_file_that_holds_it(self, capsys) -> None:
        """The "bound spec" line is the file the resolver read, so for a markdown
        agent it names ``<name>.md``; a ``.json`` join would point the operator
        at a file that does not exist."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        md = agents_dir / "custom-agent.md"
        md.write_text(
            "---\nname: custom-agent\nmodel: claude-opus-4.8\n---\nprompt\n", encoding="utf-8"
        )
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "decided by:  bound agent pin ('custom-agent')" in out
        assert f"bound spec:  {str(md)!r}" in out
        assert "custom-agent.json" not in out
        assert issues == []

    def test_markdown_specs_shadowed_by_a_json_twin_are_listed(self, capsys) -> None:
        """A ``<stem>.md`` beside ``<stem>.json`` is never read; the roster says
        so only in the gateway log, so doctor lists every such file, one line
        per twin, for any agent in the directory -- not just the bound one."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        for stem in ("alpha", "beta"):
            (agents_dir / f"{stem}.json").write_text(json.dumps({"name": stem}), encoding="utf-8")
            (agents_dir / f"{stem}.md").write_text(f"---\nname: {stem}\n---\n", encoding="utf-8")
        # A markdown agent with no JSON twin is live, not shadowed.
        (agents_dir / "solo.md").write_text("---\nname: solo\n---\n", encoding="utf-8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "shadowed:    \u26a0  2 markdown agent file(s) ignored beside a .json twin:" in out
        assert f"{str(agents_dir / 'alpha.md')!r} ('alpha.json' wins)" in out
        assert f"{str(agents_dir / 'beta.md')!r} ('beta.json' wins)" in out
        assert out.index("alpha.md") < out.index("beta.md")
        assert "solo.md" not in out
        assert issues == [], "a shadowed twin is a notice, not a doctor failure"

    def test_no_shadowed_line_without_a_twin(self, capsys) -> None:
        self._install_spec(None)
        (self._agents_dir() / "solo.md").write_text("---\nname: solo\n---\n", encoding="utf-8")

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", [])

        assert "shadowed:" not in capsys.readouterr().out

    def test_a_bound_agent_with_no_spec_is_reported_as_missing(self, capsys) -> None:
        self._install_spec(None)
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "bound spec:  \u26a0\ufe0f  no spec for 'custom-agent' under" in out
        assert "custom-agent.json" not in out

    def test_the_builtin_agent_shows_no_bound_tier(self, capsys) -> None:
        """Tier 2 is skipped for the built-in agent, so the list must not show
        a tier the resolver never consulted."""
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "bound agent pin" not in out
        assert "decided by:  default spec pin" in out
        assert "kirocrew agent reset-model" in out
        assert "--agent" not in out, "the built-in agent needs no --agent flag"

    def test_tracking_names_the_agent_it_describes(self, capsys) -> None:
        from kiro_crew import agent_state

        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent", "model": "claude-opus-4.8"}), encoding="utf-8"
        )
        agent_state.set_model_managed("custom-agent", False)
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)
        assert "tracking:    frozen (explicit pick) ('custom-agent')" in capsys.readouterr().out

    def test_project_spec_check_follows_the_bound_agent(self, capsys) -> None:
        """kiro-cli dispatches the BOUND agent, so that is the filename whose
        project-local copy can shadow the user-level spec."""
        self._install_spec(None)
        agents_dir = self._agents_dir()
        (agents_dir / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent"}), encoding="utf-8"
        )
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / "custom-agent.json").write_text(
            json.dumps({"name": "custom-agent", "model": "claude-haiku-4.5"}), encoding="utf-8"
        )
        cfg = self._bind_custom_agent(self._cfg("auto"), "custom-agent")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, str(project), issues)

        out = capsys.readouterr().out
        assert "custom-agent.json' -> 'claude-haiku-4.5'" in out
        assert issues == ["project-local agent spec shadows the user-level one"]

    def test_control_sequences_in_a_spec_model_are_escaped(self, capsys) -> None:
        """An agent spec is not always trusted input -- an installed app writes
        one and a cloned repository can ship a project-local one -- so an
        OSC/ANSI sequence in `model` must reach the terminal inert rather than
        executing controls or spoofing the surrounding diagnostic lines."""
        hostile = "claude-opus-4.8\x1b]0;pwned\x07\x1b[2K"
        self._install_spec(hostile)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out, "raw escape reached the terminal"
        assert "\x07" not in out
        assert "\\x1b" in out, "the value is still shown, just escaped"

    def test_control_sequences_in_a_project_spec_are_escaped(self, capsys) -> None:
        from kiro_crew.agent import AGENT_FILENAME

        self._install_spec(None)
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        (project / ".kiro" / "agents" / AGENT_FILENAME).write_text(
            json.dumps({"name": "kirocrew", "model": "x\x1b[31mred"}), encoding="utf-8"
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), str(project), issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\\x1b" in out

    def test_control_sequences_in_the_global_are_escaped(self, capsys) -> None:
        self._install_spec("claude-opus-4.8")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto\x1b[2J"), "", issues)

        assert "\x1b" not in capsys.readouterr().out

    @requires_symlinks
    def test_a_symlink_to_a_sensitive_target_is_refused(self, monkeypatch, capsys) -> None:
        """The doctor read goes through agent_discovery's hardened reader, which
        refuses a symlink whose RESOLVED target is sensitive (the documented
        `evil.json -> ~/.aws/credentials` case) and caps the read size. Routing
        through that one reader instead of hand-rolling the checks is the point.
        A benign link is followed exactly as the resolver follows
        it, so the report cannot disagree with what will actually run."""
        from kiro_crew import agent_discovery
        from kiro_crew.agent import AGENT_FILENAME

        agents_dir = self._agents_dir()
        target = self._tmp / "protected.json"
        target.write_text(json.dumps({"model": "leaked-value"}), encoding="utf-8")
        (agents_dir / AGENT_FILENAME).symlink_to(target)
        # The reader asks is_sensitive_canonical_path about the RESOLVED target
        # (is_sensitive_path in agent_discovery gates only the project dir and
        # the list_agents cache key), so the refusal is injected at that name.
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        out = capsys.readouterr().out
        # The report refuses to ATTRIBUTE the refused spec ...
        assert "unreadable" in out
        assert issues == ["agent spec unreadable"]
        assert "(defers)" in out.split("default spec pin:", 1)[1].splitlines()[0]
        # ... and nothing else acts on it either: the resolver reads through
        # the same hardened reader, so it refuses too -- `effective` carries no
        # value from the refused spec, and there is no resolver-vs-report gap
        # to explain.
        assert "leaked-value" not in out
        assert "refused to follow" not in out
        assert "out of date" not in out

    def test_an_absent_spec_is_not_reported_as_a_fault(self, capsys) -> None:
        """A clean install has no spec and the resolver just falls through, so
        absence must not raise an issue."""
        self._agents_dir()  # exists, but empty
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "", issues)

        assert "unreadable" not in capsys.readouterr().out
        assert issues == []

    def test_an_absolute_kiro_agent_binding_cannot_escape_the_agent_dir(self, capsys) -> None:
        """`kiro_agent` is free text in config.json and reaches a path join, and
        pathlib DISCARDS the left side when the right is absolute -- so an
        unvalidated binding would turn a spec lookup into an arbitrary read."""
        self._install_spec(None)
        secret = self._tmp / "protected.json"
        secret.write_text(json.dumps({"model": "leaked-value"}), encoding="utf-8")
        cfg = self._bind_custom_agent(self._cfg("auto"), str(secret)[:-5])
        project = self._tmp / "proj"
        (project / ".kiro" / "agents").mkdir(parents=True)
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, str(project), issues)

        out = capsys.readouterr().out
        assert "leaked-value" not in out
        assert "not a valid agent name" in out
        assert "configured kiro_agent is not a valid agent name" in issues

    def test_a_control_bearing_binding_is_escaped_and_refused(self, capsys) -> None:
        self._install_spec(None)
        cfg = self._bind_custom_agent(self._cfg("auto"), "evil\x1b[2Jname")
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "not a valid agent name" in out

    def test_a_control_bearing_project_filename_is_escaped(self, monkeypatch, capsys) -> None:
        """A cloned repository can TRACK a filename containing control bytes, so
        the path itself is untrusted input on this line.

        The hostile path is INJECTED rather than created: control bytes are
        illegal in a Windows filename, so building it on disk would make this
        assertion Windows-only-skipped, and what is under test is that the
        printer escapes what it is handed.
        """
        hostile = Path("/tmp/proj/.kiro/agents/kirocrew\x1b[2J.json")
        self._install_spec(None)
        real_reader = cli_doctor._read_agent_spec
        monkeypatch.setattr(cli_doctor, "project_agent_files", lambda d, **kw: [hostile])
        monkeypatch.setattr(cli_doctor, "project_agent_name", lambda p: "kirocrew")
        # Only the injected path is faked; the user-level spec still goes through
        # the real reader so the report's own self-check is not disturbed. The
        # scan and reader stubs forward **kw because both take keyword-only SEL
        # attribution labels that this test does not care about.
        monkeypatch.setattr(
            cli_doctor,
            "_read_agent_spec",
            lambda p, **kw: {"model": "m"} if p == hostile else real_reader(p, **kw),
        )
        issues: list[str] = []

        cli_doctor._doctor_effective_model(self._cfg("auto"), "/tmp/proj", issues)

        out = capsys.readouterr().out
        # Not vacuous: the shadow line must actually be reached.
        assert "project spec" in out
        assert issues == ["project-local agent spec shadows the user-level one"]
        assert "\x1b" not in out, "raw escape from a tracked filename reached the terminal"
        assert "\\x1b" in out, "the path is still shown, just escaped"

    def test_a_non_string_kiro_agent_does_not_crash_the_report(self, capsys) -> None:
        """The config loader deliberately KEEPS a type-mismatched value ("validated
        by its consumer"), so a hand-edited non-string reaches this section intact
        and a bare `re.match` would raise TypeError -- aborting the one command a
        user runs BECAUSE their config is broken."""
        self._install_spec("claude-opus-4.8")
        cfg = self._bind_custom_agent(self._cfg("auto"), "placeholder")
        cfg.agent.default_agent = 12345  # type: ignore[assignment]
        issues: list[str] = []

        cli_doctor._doctor_effective_model(cfg, "", issues)

        out = capsys.readouterr().out
        assert "not a valid agent name" in out
        assert "configured kiro_agent is not a valid agent name" in issues
        # It degrades to the built-in agent and still produces the report.
        assert "effective:" in out
        assert "tracking:" in out

    def test_broken_default_member_does_not_break_the_plain_session_report(self, capsys):
        # A plain session is the default TEMPLATE on Global: the
        # `default` crewmate's broken store is that crewmate's problem, reported
        # by the bindings section, and never makes the model section fail.
        from kiro_crew.config.loader import KiroCrewAgentConfig

        cfg = self._cfg("auto")
        cfg.agents["writer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", memory_store="missing-store"
        )
        cfg.default_agent = "writer"
        issues: list[str] = []
        cli_doctor._doctor_effective_model(cfg, "", issues)
        out = capsys.readouterr().out
        assert "default agent binding unavailable" not in issues
        assert "binding:     unavailable" not in out
        assert issues == []
        # The binding section still names the member and its unavailable store.
        cli_doctor._doctor_member_memory_bindings(cfg, issues)
        bindings_out = capsys.readouterr().out
        assert "'writer' -> 'missing-store': unavailable" in bindings_out
        assert "member memory binding unavailable: 'writer' -> 'missing-store'" in issues

    def test_default_crewmate_with_its_own_store_is_told_where_it_applies(self, capsys):
        from kiro_crew.config.loader import KiroCrewAgentConfig
        from kiro_crew.memory_stores import provision_member_memory

        cfg = self._cfg("auto")
        cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, "writer")
        cfg.default_agent = "writer"
        issues: list[str] = []
        cli_doctor._doctor_member_memory_bindings(cfg, issues)
        out = capsys.readouterr().out
        assert "valid binding" in out
        assert "plain sessions use the default template on Global memory" in out
        assert issues == []


class TestWhatsAppSection:
    """`kirocrew doctor`'s WhatsApp Integration section.

    WhatsApp is the only channel whose whole runtime hangs off an OPTIONAL wheel
    plus a locally stored credential, and neither absence produces an error the
    operator sees: a message simply never arrives. So the section has to answer
    both, and it has to answer them WITHOUT loading the Go core: a preflight that
    initializes the subsystem it is inspecting is both slow and a side effect.
    """

    def _cfg(self, *, enabled: bool = True, groups: list | None = None):
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.whatsapp.enabled = enabled
        cfg.whatsapp.groups = groups if groups is not None else []
        return cfg

    @pytest.fixture()
    def home(self, tmp_path: Path, monkeypatch) -> Path:
        """Pin the data home the section reports on, so no real store is read."""
        target = tmp_path / "home"
        target.mkdir()
        monkeypatch.setattr(cli_doctor, "data_home", lambda: target)
        return target

    @staticmethod
    def _extra(monkeypatch, present: bool) -> None:
        monkeypatch.setattr("kiro_crew.whatsapp.client.neonize_available", lambda: present)

    @staticmethod
    def _pair(home: Path) -> Path:
        from kiro_crew.whatsapp.client import default_db_path

        store = default_db_path(home)
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"sqlite")
        return store

    def test_a_disabled_channel_names_the_two_ways_to_enable_it(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """The channel must be VISIBLE in the preflight even when off, because that
        is the
        surface an operator checks before wondering why nothing arrives."""
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(enabled=False), issues)

        out = capsys.readouterr().out
        assert "WhatsApp Integration" in out
        assert "not enabled" in out
        assert "setup --whatsapp" in out
        assert issues == []

    def test_a_missing_extra_on_an_enabled_channel_is_a_reported_issue(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Config says the channel is on and the wheel it needs is absent: the
        channel cannot start at all, and the fix is one offline pip install."""
        self._extra(monkeypatch, False)
        self._pair(home)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        # neonize by name -- the extras form is not installable from an index.
        assert "neonize" in out
        assert "kirocrew[" not in out
        assert "whatsapp extra missing" in issues

    def test_an_installed_extra_and_a_paired_store_report_clean(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        self._extra(monkeypatch, True)
        store = self._pair(home)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        assert "extra:       ✅" in out
        assert f"session:     ✅ paired session store at {store}" in out
        assert issues == []

    def test_an_unpaired_store_warns_but_never_fails_doctor(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Load-bearing split. Pairing is a QR scan served BY the running gateway,
        so a freshly enabled channel legitimately has no store yet. Counting that
        as an issue would exit 1 and break the documented
        `kirocrew doctor && kirocrew gateway` chain at the one moment the operator
        has to start the gateway to make progress.
        """
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        out = capsys.readouterr().out
        assert "not paired yet" in out
        assert "Settings → Messaging Channels" in out
        assert issues == [], "an unpaired channel must not fail the preflight"

    def test_the_reported_store_is_the_path_the_gateway_opens(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """Doctor and the channel must resolve ONE path, or the report describes a
        store the gateway never touches."""
        from kiro_crew.whatsapp.client import default_db_path

        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        assert str(default_db_path(home)) in capsys.readouterr().out

    def test_the_check_never_imports_neonize(self, home: Path, monkeypatch, capsys) -> None:
        """The whole point of the ``find_spec`` probe: importing neonize loads a
        ~19 MB ctypes CDLL plus protobuf descriptors, and a health check must not
        pay that (or construct a client as a side effect of asking a question).
        """
        import builtins

        real_import = builtins.__import__

        def _guard(name, *args, **kwargs):
            if name.split(".")[0] == "neonize":
                raise AssertionError(
                    f"doctor imported {name!r}: the preflight must stay a find_spec check"
                )
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(), issues)

        assert "WhatsApp Integration" in capsys.readouterr().out

    def test_configured_groups_are_counted_and_junk_entries_are_not(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        """A hand-edited config reaches this section intact, so a non-dict or a
        blank JID must neither be counted nor crash the one command a user runs
        BECAUSE their config is broken."""
        self._extra(monkeypatch, True)
        issues: list[str] = []
        groups = [{"jid": "123@g.us"}, {"jid": "  "}, "not-a-dict", {"jid": "456@g.us"}]

        cli_doctor._doctor_whatsapp(self._cfg(groups=groups), issues)

        assert "groups:      ✅ 2 configured" in capsys.readouterr().out

    def test_no_configured_groups_says_group_messages_are_ignored(
        self, home: Path, monkeypatch, capsys
    ) -> None:
        self._extra(monkeypatch, True)
        issues: list[str] = []

        cli_doctor._doctor_whatsapp(self._cfg(groups=[]), issues)

        assert "none configured" in capsys.readouterr().out

    def test_the_section_is_wired_into_the_doctor_run(self) -> None:
        """Guards the call site itself. Every other test here drives the helper
        directly, so a deleted call would leave them all green and the operator
        with no WhatsApp line, the exact gap this section was added to close.
        ``_doctor()`` spawns subprocesses, probes the network and calls
        ``sys.exit``, so its source is read rather than run.
        """
        import inspect

        source = inspect.getsource(cli_doctor._doctor)
        assert "_doctor_whatsapp(cfg, issues)" in source


class TestProjectSectionAndAuthRow:
    """`kirocrew doctor` Project labels + the local-bind auth row.

    The Project row resolves the Kiro Crew SOURCE CHECKOUT (``cli.py``
    ``_PROJECT_MARKERS``), never the user's own workspace, so its labels must
    say so. The Configuration auth row must not advertise a loopback
    exemption: the dashboard middleware requires a valid token on every
    request (``token_auth.py``), and local CLI/MCP callers authenticate with
    the local secret instead.
    """

    def _run_doctor(self, tmp_path: Path, monkeypatch, capsys, *, project_dir: str) -> str:
        """Drive the full ``_doctor()`` hermetically and return its output.

        Mirrors the mock harness of ``test_cli.py``'s doctor tests: config
        pinned to a pristine default, binaries "found", probes stubbed, so
        the run is deterministic and spawns nothing.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        def _pristine() -> KiroCrewConfig:
            cfg = KiroCrewConfig()
            cfg.stt.enabled = False
            return cfg

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _pristine()))
        monkeypatch.setattr(KiroCrewConfig, "load_credentials", lambda self: {})

        async def _probe(server):
            server.status = "ok"
            server.tools = []
            return server

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
            patch("kiro_crew.cli_doctor.probe_server", side_effect=_probe),
            patch.dict(
                "os.environ",
                {
                    "KIROCREW_PROJECT_DIR": project_dir,
                    "SLACK_APP_TOKEN": "",
                    "SLACK_BOT_TOKEN": "",
                },
                clear=False,
            ),
        ):
            with pytest.raises(SystemExit):
                cli_doctor._doctor()
        return capsys.readouterr().out

    def test_set_project_dir_is_labelled_source_checkout(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # Carry both _PROJECT_MARKERS so the directory measurably IS a
        # Kiro Crew source checkout.
        (tmp_path / "skills").mkdir()
        (tmp_path / "src" / "kiro_crew").mkdir(parents=True)
        out = self._run_doctor(tmp_path, monkeypatch, capsys, project_dir=str(tmp_path))
        assert f"source dir:  ✅ {tmp_path} (Kiro Crew source checkout)" in out
        assert "project dir:" not in out
        # tmp_path holds no .git — the warning names the checkout, so the
        # adjacent row is not read as a finding about the user's workspace.
        assert "git repo:    ⚠️  source checkout is not a git repo" in out

    def test_partially_marked_dir_is_not_called_a_checkout(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # A single marker must not qualify as a Kiro Crew source checkout.
        (tmp_path / "skills").mkdir()
        out = self._run_doctor(tmp_path, monkeypatch, capsys, project_dir=str(tmp_path))
        assert f"source dir:  ✅ {tmp_path}" in out
        assert "(Kiro Crew source checkout)" not in out
        assert "git repo:    ⚠️  not a git repo" in out
        assert "source checkout is not a git repo" not in out

    def test_not_set_hint_names_checkout_and_wheel_installs(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        out = self._run_doctor(tmp_path, monkeypatch, capsys, project_dir="")
        assert "source dir:  ⚠️  not set" in out
        assert "not needed for wheel installs" in out
        assert "run kirocrew setup from project root" not in out

    def test_auth_row_never_advertises_loopback_exemption(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        # is_local_only is patched True, so this exercises the local-bind
        # branch; its auth row must never advertise a loopback exemption.
        out = self._run_doctor(tmp_path, monkeypatch, capsys, project_dir="")
        assert "no token required" not in out
        assert "loopback trusted" not in out
        assert (
            "auth:        token required — loopback is not exempt (CLI/MCP use the local secret)"
            in out
        )

    def test_auth_row_claim_is_grounded_in_the_middleware(self) -> None:
        # The row's claim is prose; this pins it to production code so a
        # reinstated loopback exemption for ordinary API routes reds a test
        # instead of drifting the way the old row did. /api/status is the
        # canonical gated route: it must never join the bypass sets.
        from kiro_crew.dashboard import token_auth

        assert "/api/status" not in token_auth._BYPASS_EXACT
        assert "/api/status" not in token_auth._BYPASS_EXACT_METHODS
        assert not any("/api/status".startswith(p) for p in token_auth._BYPASS_PREFIXES)


class TestNameGrantPlatformScopeRow:
    """`kirocrew doctor` says whether hook auto-approve works on this host.

    A user who sees decline lines in `gateway.log` has no other way to tell a
    host-wide reason from their own configuration.
    """

    def test_unknown_documents_folder_names_the_code_and_says_it_is_not_your_config(
        self, monkeypatch, capsys
    ):
        from kiro_crew import name_grant

        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            name_grant.platform_compat, "windows_powershell_profile_paths", lambda: None
        )
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        # The code is what a reader greps `gateway.log` for.
        assert name_grant.WINDOWS_UNMODELLED in out
        assert "not your configuration" in out
        assert "approval card" in out

    def test_an_existing_profile_is_named_so_the_user_can_act(self, monkeypatch, capsys, tmp_path):
        from kiro_crew import name_grant

        profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
        profile.write_text("function ls { evil }\n")
        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(
            name_grant.platform_compat, "windows_powershell_profile_paths", lambda: (str(profile),)
        )
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        assert name_grant.AMBIGUOUS_ENV in out
        assert str(profile) in out
        assert "approval card" in out
        assert "✅" not in out

    def test_windows_without_a_profile_reports_that_grants_can_be_satisfied(
        self, monkeypatch, capsys, tmp_path
    ):
        from kiro_crew import name_grant

        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
        monkeypatch.setattr(
            name_grant.platform_compat,
            "windows_powershell_profile_paths",
            lambda: (str(tmp_path / "absent.ps1"),),
        )
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        assert "hook auto-approve:  ✅" in out
        assert name_grant.WINDOWS_UNMODELLED not in out

    def test_posix_reports_that_grants_can_be_satisfied(self, monkeypatch, capsys):
        from kiro_crew import name_grant

        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
        monkeypatch.setattr(name_grant, "_inherited_preload", lambda: None)
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        assert "hook auto-approve:  ✅" in out
        assert name_grant.WINDOWS_UNMODELLED not in out

    def test_an_inherited_preload_is_reported_rather_than_claimed_satisfiable(
        self, monkeypatch, capsys
    ):
        # The row must describe the answer the module actually gives. `BASH_ENV`,
        # exported shell functions and a relative `PATH` entry each refuse every
        # grant, so a ✅ beside them would send a user hunting a fault that is
        # their own environment.
        from kiro_crew import name_grant

        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: False)
        monkeypatch.setattr(name_grant, "_inherited_preload", lambda: "BASH_ENV")
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        assert "hook auto-approve:  ✅" not in out
        assert name_grant.AMBIGUOUS_ENV in out
        assert "BASH_ENV" in out
        assert "approval card" in out
        # Not the profile remedy -- there is no profile in this state.
        assert "rename the profile" not in out

    def test_a_relative_search_path_entry_is_reported_rather_than_claimed_satisfiable(
        self, monkeypatch, capsys
    ):
        from kiro_crew import name_grant

        monkeypatch.setattr(name_grant.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(name_grant, "_path_is_ambiguous", lambda: True)
        cli_doctor._doctor_name_grant_platform_scope()
        out = capsys.readouterr().out
        assert "hook auto-approve:  ✅" not in out
        assert name_grant.AMBIGUOUS_PATH in out

    def test_the_row_cannot_contribute_an_issue(self):
        # The fail-closed is the intended posture, so this row reports scope
        # rather than a fault. It takes no `issues` list, so unlike the sections
        # around it there is no way for it to make doctor exit non-zero.
        import inspect

        params = inspect.signature(cli_doctor._doctor_name_grant_platform_scope).parameters
        assert not params

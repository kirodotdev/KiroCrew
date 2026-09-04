"""Tests for the doctor dead-path check (kiro_crew.doctor_deadpath).

Covers the four scenarios the check must get right, on fabricated spec dirs
under ``tmp_path`` (never the real agents dir):

* a MANAGED spec with a dead path is repaired via the rebuild path and
  re-verified;
* a FOREIGN spec with a dead path is report-only and never rewritten;
* a colon-joined PATH-like env value is NOT treated as a single path
  (no false positive);
* malformed / unreadable spec JSON is tolerated (fail-open per file), never
  crashing the whole check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew import doctor_deadpath as dp
from kiro_crew.agent_files import AGENT_FILENAME


@pytest.fixture
def agents_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point the check at a fabricated agents dir under tmp_path."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(dp, "KIRO_AGENTS_DIR", d)
    return d


def _write_spec(agents_dir: Path, name: str, servers: dict) -> Path:
    path = agents_dir / name
    path.write_text(json.dumps({"name": name[:-5], "mcpServers": servers}), encoding="utf-8")
    return path


# ── the absolute-single-path heuristic ────────────────────────────────────────


class TestLooksLikeSinglePath:
    def test_absolute_path_is_one(self) -> None:
        assert dp._looks_like_single_absolute_path("/opt/tool/bin/x") is True

    def test_relative_is_not(self) -> None:
        assert dp._looks_like_single_absolute_path("tool/bin/x") is False

    def test_bare_token_is_not(self) -> None:
        assert dp._looks_like_single_absolute_path("kirocrew") is False

    def test_posix_path_list_is_not_a_single_path(self) -> None:
        # The explicit false-positive to avoid: a colon-joined PATH.
        assert dp._looks_like_single_absolute_path("/usr/local/bin:/usr/bin:/bin") is False

    def test_windows_path_list_is_not_a_single_path(self) -> None:
        assert dp._looks_like_single_absolute_path(r"C:\a;C:\b") is False

    def test_comma_joined_path_list_is_not_a_single_path(self) -> None:
        # The separator multi-value CLI flags conventionally take. Every
        # component here is absent, but the value is a LIST — stat-ing it whole
        # would report a dead path no matter how healthy the components are.
        assert dp._looks_like_single_absolute_path("/opt/a,/opt/b,/opt/c") is False

    def test_comma_joined_list_of_live_dirs_is_not_flagged(self, tmp_path: Path) -> None:
        # The regression this guards: a list whose every component EXISTS still
        # cannot pass a whole-string stat, so without the comma screen it is
        # reported dead permanently and no edit to the value can clear it.
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        joined = "{},{}".format(a, b)
        assert a.exists() and b.exists()
        assert dp._path_is_dead(joined) is True  # the whole string never stats
        assert dp._looks_like_single_absolute_path(joined) is False

    def test_windows_drive_path_not_mistaken_for_posix_list(self) -> None:
        # The colon-list rejection is scoped to the POSIX list-separator shape
        # (a colon flanked by ``/``). A lone drive-letter colon has no adjacent
        # ``/`` so the colon scan must not reject it. (On a POSIX runner the
        # value is not ``os.path.isabs`` and so returns False at the abs guard;
        # this asserts the colon scan itself does not add a rejection.)
        assert dp._colon_scan_rejects(r"C:\Users\me\tool.exe") is False
        assert dp._colon_scan_rejects("/usr/bin:/bin") is True

    def test_forward_slash_drive_path_not_mistaken_for_posix_list(self) -> None:
        # ``C:/Users/x`` carries a ``/`` right after the drive colon, which the
        # flanked-colon shape would otherwise read as a list separator. The
        # drive-letter exemption (colon at index 1 after a letter, followed by
        # a separator) keeps it a single path; every other flanked colon still
        # rejects.
        assert dp._colon_scan_rejects("C:/Users/me/tool.exe") is False
        assert dp._colon_scan_rejects("/opt/a:/opt/b") is True

    def test_credential_shaped_env_key_value_is_redacted(self, agents_dir: Path) -> None:
        # A secret VALUE can pass the single-absolute-path shape test (a
        # slash-prefixed token), and doctor output routinely gets pasted into
        # bug reports -- so an env entry under a credential-shaped KEY must
        # never surface its value. The locator (env[KEY]) still names the
        # entry, so the report stays actionable.
        secret = "/" + "s3cr3t-b64-blob-that-does-not-exist"
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir / "gone"), "env": {"API_TOKEN": secret}}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        result = next(r for r in report.results if r.spec == "foreign.json")
        env_hits = [d for d in result.dead if d.where == "env[API_TOKEN]"]
        assert env_hits, "credential-keyed dead entry must still be reported"
        assert secret not in env_hits[0].path
        assert env_hits[0].path == dp._REDACTED_VALUE
        # A non-credential key keeps its real value (actionability).
        plain_hits = [d for d in result.dead if d.where == "command"]
        assert plain_hits and str(agents_dir / "gone") == plain_hits[0].path

    def test_empty_is_not(self) -> None:
        assert dp._looks_like_single_absolute_path("") is False


# ── managed-repair ─────────────────────────────────────────────────────────────


class TestDefaultRepairDirGuard:
    """The DEFAULT repair (rebuild_agent_config) writes wherever the AMBIENT
    environment resolves, not the scanned directory. When a caller redirects
    the scan but keeps the default repair, the pass must degrade to
    report-only rather than judge one directory and rewrite another."""

    def test_redirected_scan_with_default_repair_is_report_only(
        self, agents_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(
            "kiro_crew.agent.rebuild_agent_config", lambda *a, **k: calls.append(True)
        )
        other = tmp_path / "elsewhere"
        other.mkdir()
        _write_spec(other, AGENT_FILENAME, {"srv": {"command": str(other / "gone")}})

        report = dp.check_dead_paths(agents_dir=other)

        assert calls == []  # the ambient rebuild never ran
        assert any(r.managed and r.dead for r in report.results)  # still reported

    def test_ambient_dir_with_default_repair_still_repairs(
        self, agents_dir: Path, monkeypatch
    ) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(
            "kiro_crew.agent.rebuild_agent_config", lambda *a, **k: calls.append(True)
        )
        _write_spec(agents_dir, AGENT_FILENAME, {"srv": {"command": str(agents_dir / "gone")}})

        dp.check_dead_paths(agents_dir=agents_dir)

        assert calls == [True]  # same directory: the default repair runs


class TestManagedRepair:
    def test_managed_spec_dead_path_is_repaired_and_reverified(self, agents_dir: Path) -> None:
        dead = str(agents_dir / "gone" / "python")  # does not exist
        live = str(agents_dir)  # exists
        spec = _write_spec(
            agents_dir, AGENT_FILENAME, {"kirocrew-core": {"command": dead, "args": []}}
        )

        def fake_repair() -> None:
            # Simulate rebuild rewriting the managed spec to a live command.
            spec.write_text(
                json.dumps({"mcpServers": {"kirocrew-core": {"command": live}}}),
                encoding="utf-8",
            )

        report = dp.check_dead_paths(repair=fake_repair)

        managed = report.managed_dead
        # After a clean repair the spec is no longer counted as dead...
        assert managed == [] or all(r.repaired for r in managed)
        result = next(r for r in report.results if r.spec == AGENT_FILENAME)
        assert result.managed is True
        assert result.repaired is True
        assert report.has_findings is False

    def test_managed_spec_repair_that_does_not_take_is_a_finding(self, agents_dir: Path) -> None:
        dead = str(agents_dir / "gone" / "python")
        _write_spec(agents_dir, AGENT_FILENAME, {"kirocrew-core": {"command": dead}})

        def noop_repair() -> None:
            pass  # repair fails to clear the dead path

        report = dp.check_dead_paths(repair=noop_repair)

        result = next(r for r in report.results if r.spec == AGENT_FILENAME)
        assert result.repaired is False
        assert report.repair_failed == [result]
        assert report.has_findings is True

    def test_repair_is_only_invoked_when_a_managed_spec_is_dead(self, agents_dir: Path) -> None:
        live = str(agents_dir)
        _write_spec(agents_dir, AGENT_FILENAME, {"kirocrew-core": {"command": live}})
        calls = {"n": 0}

        def counting_repair() -> None:
            calls["n"] += 1

        dp.check_dead_paths(repair=counting_repair)
        assert calls["n"] == 0


# ── foreign-report-only ──────────────────────────────────────────────────────


class TestForeignReportOnly:
    def test_foreign_spec_dead_path_reported_not_repaired(self, agents_dir: Path) -> None:
        dead = str(agents_dir / "reaped-venv" / "bin" / "server")
        spec = _write_spec(agents_dir, "some-other-tool.json", {"their-server": {"command": dead}})
        before = spec.read_text(encoding="utf-8")

        def boom_repair() -> None:  # must never be called for a foreign-only dir
            raise AssertionError("repair must not run for a foreign spec")

        report = dp.check_dead_paths(repair=boom_repair)

        foreign = report.foreign_dead
        assert len(foreign) == 1
        assert foreign[0].spec == "some-other-tool.json"
        assert foreign[0].dead[0].server == "their-server"
        assert foreign[0].dead[0].path == dead
        # The foreign spec is left byte-for-byte untouched.
        assert spec.read_text(encoding="utf-8") == before
        assert report.has_findings is True


# ── PATH-like value ignored ──────────────────────────────────────────────────


class TestPathListIgnored:
    def test_colon_joined_env_value_is_not_flagged(self, agents_dir: Path) -> None:
        # A PATH env value listing several dirs must not be treated as one path
        # even though each component may be absent.
        _write_spec(
            agents_dir,
            AGENT_FILENAME,
            {
                "kirocrew-core": {
                    "command": str(agents_dir),  # live command
                    "env": {"PATH": "/nonexistent/a:/nonexistent/b:/bin"},
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        result = next(r for r in report.results if r.spec == AGENT_FILENAME)
        assert result.dead == []
        assert report.has_findings is False

    def test_comma_joined_arg_value_is_not_flagged(self, agents_dir: Path) -> None:
        # A multi-value CLI flag takes its directories as one comma-joined
        # argument. Each component here is live, so the arg resolves fine; only
        # a whole-string stat would call it dead.
        live_a = agents_dir / "dir-a"
        live_b = agents_dir / "dir-b"
        live_a.mkdir()
        live_b.mkdir()
        _write_spec(
            agents_dir,
            AGENT_FILENAME,
            {
                "kirocrew-core": {
                    "command": str(agents_dir),  # live command
                    "args": ["--search-dirs", "{},{}".format(live_a, live_b)],
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        result = next(r for r in report.results if r.spec == AGENT_FILENAME)
        assert result.dead == []
        assert report.has_findings is False

    def test_single_absolute_env_value_that_is_dead_is_flagged(self, agents_dir: Path) -> None:
        dead_home = str(agents_dir / "dead-data-home")
        _write_spec(
            agents_dir,
            "foreign.json",
            {
                "srv": {
                    "command": str(agents_dir),
                    "env": {"KIROCREW_HOME": dead_home},
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        foreign = report.foreign_dead
        assert len(foreign) == 1
        assert foreign[0].dead[0].where == "env[KIROCREW_HOME]"
        assert foreign[0].dead[0].path == dead_home


# ── identifier operands of non-path flags ────────────────────────────────────


class TestIdentifierOperandIgnored:
    """A slash-prefixed identifier is not a path, and only position says so.

    ``--scope /spaces/nsp_x`` names a remote namespace. The operand is absolute,
    single, and absent from every filesystem by design, so it is byte-for-byte the
    shape of a genuinely removed directory. Nothing about the VALUE can separate the
    two; the discriminator is which flag it follows.
    """

    def test_scope_operand_is_not_flagged(self, agents_dir: Path) -> None:
        _write_spec(
            agents_dir,
            "foreign.json",
            {
                "policy-scope-server": {
                    "command": str(agents_dir),  # live command
                    "args": ["mcp", "start", "--scope", "/spaces/ns_abc123"],
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert report.foreign_dead == []
        assert report.has_findings is False

    def test_identifier_operand_is_never_stat_ed(self, agents_dir: Path, monkeypatch) -> None:
        """The operand must not reach the filesystem probe at all.

        This is the assertion the REPORT cannot make. Deciding the skip after
        ``_path_is_dead`` produces an identical ``foreign_dead`` while still stat-ing
        an opaque remote identifier, so a report-only test passes either way and the
        module docstring's "must not be stat-ed" goes unenforced. Spying on the probe
        is what separates the two orderings.
        """
        probed: list[str] = []
        real = dp._path_is_dead

        def spy(path: str) -> bool:
            probed.append(path)
            return real(path)

        monkeypatch.setattr(dp, "_path_is_dead", spy)
        _write_spec(
            agents_dir,
            "foreign.json",
            {
                "policy-scope-server": {
                    "command": str(agents_dir),
                    "args": ["mcp", "start", "--scope", "/spaces/ns_abc123"],
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert report.foreign_dead == []
        assert "/spaces/ns_abc123" not in probed, (
            "the identifier operand was stat-ed; the screen must be read BEFORE "
            f"_path_is_dead, not after it. probed={probed}"
        )

    @pytest.mark.parametrize("flag", ["--scope", "--namespace", "--space"])
    def test_every_identifier_flag_operand_is_not_flagged(
        self, agents_dir: Path, flag: str
    ) -> None:
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": ["run", flag, "/spaces/nsp_zz"]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert report.foreign_dead == []

    def test_attached_operand_spelling_is_not_flagged(self, agents_dir: Path) -> None:
        # ``--scope=/spaces/nsp_x`` is not absolute as a whole string, so it never
        # reached the stat anyway. Pinned so the two spellings cannot diverge if the
        # arg walk is ever changed to split on ``=``.
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": ["--scope=/spaces/nsp_zz"]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert report.foreign_dead == []

    def test_dead_path_after_an_ordinary_flag_is_still_flagged(self, agents_dir: Path) -> None:
        # The load-bearing half: skipping identifier operands must not blind the
        # check to a real dead path that merely happens to follow some flag.
        dead = str(agents_dir / "gone")
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": ["--config", dead]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        foreign = report.foreign_dead
        assert len(foreign) == 1
        assert foreign[0].dead[0].where == "args[1]"
        assert foreign[0].dead[0].path == dead

    def test_dead_path_two_positions_after_an_identifier_flag_is_flagged(
        self, agents_dir: Path
    ) -> None:
        # Only the IMMEDIATELY following arg is the flag's operand. A later path
        # argument is unrelated and must keep being checked.
        dead = str(agents_dir / "gone")
        _write_spec(
            agents_dir,
            "foreign.json",
            {
                "srv": {
                    "command": str(agents_dir),
                    "args": ["--scope", "/spaces/nsp_zz", dead],
                }
            },
        )

        report = dp.check_dead_paths(repair=lambda: None)

        foreign = report.foreign_dead
        assert len(foreign) == 1
        assert [d.path for d in foreign[0].dead] == [dead]

    def test_identifier_flag_as_the_last_arg_does_not_index_past_the_end(
        self, agents_dir: Path
    ) -> None:
        # A trailing flag with no operand must not raise, and the flag itself is
        # relative so it was never a path candidate.
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": ["run", "--scope"]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert report.foreign_dead == []

    def test_absolute_first_arg_is_still_checked(self, agents_dir: Path) -> None:
        # index 0 has no preceding arg; the guard must not swallow it.
        dead = str(agents_dir / "gone")
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": [dead]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert [d.path for d in report.foreign_dead[0].dead] == [dead]

    def test_non_string_preceding_arg_does_not_crash_the_walk(self, agents_dir: Path) -> None:
        # Spec JSON is foreign input: a non-string arg is possible and must not
        # make the positional lookup explode.
        dead = str(agents_dir / "gone")
        _write_spec(
            agents_dir,
            "foreign.json",
            {"srv": {"command": str(agents_dir), "args": [7, dead]}},
        )

        report = dp.check_dead_paths(repair=lambda: None)

        assert [d.path for d in report.foreign_dead[0].dead] == [dead]


# ── malformed JSON tolerated ─────────────────────────────────────────────────


class TestMalformedTolerated:
    def test_malformed_json_is_reported_not_fatal(self, agents_dir: Path) -> None:
        (agents_dir / "broken.json").write_text("{ not json", encoding="utf-8")
        # A healthy foreign spec alongside proves the walk continues past the
        # broken one.
        _write_spec(agents_dir, "ok.json", {"srv": {"command": str(agents_dir)}})

        report = dp.check_dead_paths(repair=lambda: None)

        unreadable = report.unreadable
        assert [r.spec for r in unreadable] == ["broken.json"]
        assert unreadable[0].unreadable and "malformed JSON" in unreadable[0].unreadable
        assert report.has_findings is True
        # The healthy spec was still walked.
        assert any(r.spec == "ok.json" for r in report.results)

    def test_non_object_json_is_reported(self, agents_dir: Path) -> None:
        (agents_dir / "list.json").write_text("[1, 2, 3]", encoding="utf-8")

        report = dp.check_dead_paths(repair=lambda: None)

        result = next(r for r in report.results if r.spec == "list.json")
        assert result.unreadable == "top-level JSON is not an object"

    def test_non_utf8_spec_is_reported_not_fatal(self, agents_dir: Path) -> None:
        # A binary / non-UTF-8 file decodes with a UnicodeDecodeError (NOT an
        # OSError); it must be a per-file unreadable result, and the walk must
        # continue to the healthy spec beside it.
        (agents_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
        _write_spec(agents_dir, "ok.json", {"srv": {"command": str(agents_dir)}})

        report = dp.check_dead_paths(repair=lambda: None)

        result = next(r for r in report.results if r.spec == "binary.json")
        assert result.unreadable and "UTF-8" in result.unreadable
        assert any(r.spec == "ok.json" for r in report.results)

    def test_missing_agents_dir_returns_empty(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr(dp, "KIRO_AGENTS_DIR", tmp_path / "nope")
        report = dp.check_dead_paths(repair=lambda: None)
        assert report.results == []
        assert report.has_findings is False


# ── doctor renderer wiring ───────────────────────────────────────────────────


class TestDoctorRenderer:
    def test_foreign_dead_path_appends_issue_and_prints(
        self, agents_dir: Path, capsys, monkeypatch
    ) -> None:
        dead = str(agents_dir / "gone" / "server")
        _write_spec(agents_dir, "foreign.json", {"srv": {"command": dead}})
        # Never repair (there are no managed specs anyway).
        monkeypatch.setattr(dp, "_default_repair", lambda: None)

        issues: list[str] = []
        dp.doctor_dead_paths(issues)

        out = capsys.readouterr().out
        assert "Agent Spec Paths" in out
        assert "foreign.json" in out
        assert dead in out
        assert any("foreign" in i for i in issues)

    def test_clean_dir_prints_ok_and_no_issue(self, agents_dir: Path, capsys) -> None:
        _write_spec(agents_dir, "foreign.json", {"srv": {"command": str(agents_dir)}})
        issues: list[str] = []
        dp.doctor_dead_paths(issues)
        out = capsys.readouterr().out
        assert "all agent spec paths resolve" in out
        assert issues == []

    def test_terminal_escape_bytes_are_neutralized(self, agents_dir: Path, capsys) -> None:
        # A foreign spec is untrusted content: an ESC/OSC sequence in a dead
        # path must not reach the terminal verbatim.
        dead = str(agents_dir / "gone") + "\x1b]0;pwned\x07\x1b[31m"
        _write_spec(agents_dir, "evil.json", {"srv": {"command": dead}})

        issues: list[str] = []
        dp.doctor_dead_paths(issues)

        out = capsys.readouterr().out
        assert "\x1b" not in out  # no raw ESC reaches the terminal
        assert "\x07" not in out  # no raw BEL either
        assert "\\x1b" in out  # rendered as a visible, inert token
        assert any("evil.json" in i for i in issues)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="NTFS refuses control bytes in filenames at creation (EINVAL), "
        "so the hostile-FILENAME shape this pins is POSIX-only; the sanitizer "
        "itself is platform-neutral and covered by the tests above.",
    )
    def test_issue_entries_are_sanitized_too(self, agents_dir: Path, capsys) -> None:
        # The FILENAME itself can carry control bytes, and every issues entry
        # is later joined into doctor's final "Fix these issues" summary line,
        # which prints -- so the issues channel must be as inert as the direct
        # prints. Covers all three append sites: foreign-dead, unreadable, and
        # managed-not-cleared.
        evil_name = "ev\x1bil.json"
        _write_spec(agents_dir, evil_name, {"srv": {"command": str(agents_dir / "gone")}})
        (agents_dir / "bro\x07ken.json").write_text("{not json", encoding="utf-8")

        issues: list[str] = []
        dp.doctor_dead_paths(issues)
        capsys.readouterr()

        assert issues, "expected findings"
        joined = " | ".join(issues)
        assert "\x1b" not in joined and "\x07" not in joined
        assert "\\x1b" in joined  # visible token survives for diagnosability

    def test_sanitizer_keeps_ordinary_text(self) -> None:
        assert dp._sanitize_for_terminal("/opt/tool/bin/x") == "/opt/tool/bin/x"
        assert dp._sanitize_for_terminal("a\tb") == "a\tb"  # tab is kept
        assert dp._sanitize_for_terminal("x\x1by") == "x\\x1by"


class TestExplicitAgentsDir:
    """The scan and repair must operate on the caller's resolved dir.

    Regression guard: doctor has its OWN agent-dir override, so if the check
    re-resolved the live home instead of honoring the dir doctor passes, a dead
    managed path could trigger a rebuild against specs doctor never meant to
    touch.
    """

    def test_check_scans_the_passed_dir_not_the_module_default(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # Module default points at an EMPTY dir; the explicit arg points at the
        # dir that actually holds a dead-path spec.
        empty = tmp_path / "module-default"
        empty.mkdir()
        monkeypatch.setattr(dp, "KIRO_AGENTS_DIR", empty)
        target = tmp_path / "doctor-dir"
        target.mkdir()
        dead = str(target / "gone" / "server")
        (target / "foreign.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": dead}}}), encoding="utf-8"
        )

        # Passing the module default would find nothing; the explicit dir finds
        # the dead path.
        assert dp.check_dead_paths(agents_dir=empty, repair=lambda: None).results == []
        report = dp.check_dead_paths(agents_dir=target, repair=lambda: None)
        assert [r.spec for r in report.foreign_dead] == ["foreign.json"]

    def test_doctor_forwards_agents_dir(self, monkeypatch, tmp_path: Path, capsys) -> None:
        # If doctor_dead_paths ignored its agents_dir and used the module
        # default (empty), it would report "no agent specs"; forwarding the arg
        # makes it see the dead path in the passed dir.
        empty = tmp_path / "module-default"
        empty.mkdir()
        monkeypatch.setattr(dp, "KIRO_AGENTS_DIR", empty)
        target = tmp_path / "doctor-dir"
        target.mkdir()
        dead = str(target / "gone" / "server")
        (target / "foreign.json").write_text(
            json.dumps({"mcpServers": {"srv": {"command": dead}}}), encoding="utf-8"
        )

        issues: list[str] = []
        dp.doctor_dead_paths(issues, agents_dir=target)

        out = capsys.readouterr().out
        assert "foreign.json" in out
        assert dead in out
        assert any("foreign" in i for i in issues)

"""Conductor agent installer + bundled acceptance evaluator.

The installer test mirrors the research-agent installer test's shape: stub the
agents dir and ``build_agent_config``, run the installer, assert on the JSON it
wrote. The evaluator tests run the real script over stdin/stdout — it is the
deterministic half of the conductor's patrol, so its verdict vocabulary is
pinned here.

The ``cmd`` fixtures deliberately use ``git`` rather than ``sys.executable``:
bare interpreters are NOT on the evaluator's allowlist (a spec could otherwise
name ``python -c <payload>``), and pinning that is one of the tests below.
"""

import json
import subprocess
import sys
from pathlib import Path

from skill_script_helpers import load_skill_script

from kiro_crew import agent
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME, OWNED_KIRO_AGENT_FILES
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "builtin_skills" / "goal-conductor"
)
SCRIPT = SKILL_DIR / "scripts" / "accept_eval.py"

#: An allowlisted command that always exits 0 — the "pass" fixture.


class TestConductorInstaller:
    def _install(self, tmp_path, monkeypatch, *, may_auto_approve=None):
        monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: tmp_path)
        monkeypatch.setattr(
            agent,
            "build_agent_config",
            lambda: {
                "name": "kirocrew",
                "prompt": "file://x",
                "mcpServers": {
                    "kirocrew-core": {"command": "/resolved/kirocrew", "args": ["mcp-core"]},
                    "builder-mcp": {"command": "/x/builder", "args": []},
                },
                "tools": ["fs_write", "@kirocrew-core"],
                "allowedTools": ["@kirocrew-core"],
            },
        )
        monkeypatch.setattr(
            agent,
            "_kirocrew_mcp_invocation",
            lambda sub: ("/resolved/kirocrew", [sub]),
        )
        # Pin the ceiling predicate: the default is an ungoverned host (keep every
        # grant), and the governed case gets its own test below.
        monkeypatch.setattr(agent, "_may_auto_approve", may_auto_approve or (lambda ref: True))
        agent._install_conductor_agent()
        return json.loads((tmp_path / CONDUCTOR_AGENT_FILENAME).read_text(encoding="utf-8"))

    def test_identity_and_charter(self, tmp_path, monkeypatch):
        data = self._install(tmp_path, monkeypatch)
        assert data["name"] == "kirocrew-conductor"
        assert "work item" in data["prompt"]

    def test_no_write_tool_and_dashboard_not_preapproved(self, tmp_path, monkeypatch):
        """The two deliberate security properties of the spec.

        No ``fs_write``: the conductor cannot do a work item's work itself.
        ``@kirocrew-dashboard`` and ``execute_bash`` reachable but NOT in
        ``allowedTools``: their calls must keep passing through the tool-call
        hook where the deny floor and governance ceiling apply.
        """
        data = self._install(tmp_path, monkeypatch)
        assert "fs_write" not in data["tools"]
        assert "@kirocrew-dashboard" in data["tools"]
        assert "@kirocrew-dashboard" not in data["allowedTools"]
        assert "execute_bash" in data["tools"]
        assert "execute_bash" not in data["allowedTools"]

    def test_mounts_no_tool_the_charter_never_names(self, tmp_path, monkeypatch):
        """An unused grant is surface the charter cannot account for.

        `web_fetch` serves the skill's worked example (reading an issue list
        during triage). `web_search`, `grep` and `glob` appeared in neither the
        prompt nor the skill and were dropped — `fs_read` covers every read the
        charter describes. `tool_search` stays and is now NAMED in the prompt:
        with MCP Tool Search active the session-control specs are deferred, so
        without it the conductor cannot reach `session_create` at all.
        """
        data = self._install(tmp_path, monkeypatch)
        for absent in ("web_search", "grep", "glob"):
            assert absent not in data["tools"], absent
        assert "web_fetch" in data["tools"]
        assert "tool_search" in data["tools"]
        assert "tool_search" in data["prompt"]

    def test_mounts_no_tool_that_can_write_a_file(self, tmp_path, monkeypatch):
        """The no-write property must hold against the tool list, not the prose.

        `fs_write` is the obvious one, but `code` is the trap: governance maps it
        to `filesystem.write` because it "writes files AND can shell out"
        (`platform/governance.py` BUILTIN_TOOL_SCOPES), and it sits in
        `WITHHELD_FROM_AUTO_APPROVE` for the same reason. Mounting it would make
        "never does a work item's work itself" false while the docstring still
        claimed it, so both are pinned here together.
        """
        data = self._install(tmp_path, monkeypatch)
        for writer in ("fs_write", "code"):
            assert writer not in data["tools"], writer

    def test_mcp_surface_is_narrowed_to_core_plus_dashboard(self, tmp_path, monkeypatch):
        """Inherited servers the conductor has no charter for are dropped."""
        data = self._install(tmp_path, monkeypatch)
        assert set(data["mcpServers"]) == {"kirocrew-core", "kirocrew-dashboard"}
        assert data["mcpServers"]["kirocrew-dashboard"]["args"] == ["mcp-dashboard"]

    def test_dashboard_entry_omits_managed_metadata_on_a_default_install(
        self, tmp_path, monkeypatch
    ):
        """Neither helper contributes by default, so the emitted entry stays minimal.

        Pinned explicitly rather than read off the ambient environment: the
        repo-root conftest pins ``KIROCREW_HOME`` for every test, so the real
        ``_managed_mcp_env()`` legitimately returns a pin in-suite.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: False)
        monkeypatch.setattr(agent, "_managed_mcp_env", dict)
        dash = self._install(tmp_path, monkeypatch)["mcpServers"]["kirocrew-dashboard"]
        assert dash == {"command": "/resolved/kirocrew", "args": ["mcp-dashboard"]}

    def test_dashboard_entry_carries_managed_server_metadata(self, tmp_path, monkeypatch):
        """The hand-built dashboard entry is enriched like every managed server.

        Without ``"type": "registry"`` a registry-mode client silently drops the
        entry, so the conductor's session-control tools never launch. Without the
        ``KIROCREW_HOME`` pin the shim reads the default data home while the
        gateway runs under an override, so session control acts on a different
        session store than it reports on.
        """
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
        monkeypatch.setattr(agent, "_managed_mcp_env", lambda: {"KIROCREW_HOME": "/tmp/override"})
        data = self._install(tmp_path, monkeypatch)
        dash = data["mcpServers"]["kirocrew-dashboard"]
        assert dash["type"] == "registry"
        assert dash["env"] == {"KIROCREW_HOME": "/tmp/override"}

    def test_grants_pass_through_the_governance_ceiling(self, tmp_path, monkeypatch):
        """``allowedTools`` never reaches the PreToolUse gate, so it is filtered.

        A ceiling with an opinion about ``@kirocrew-core`` must not be silently
        bypassed by a static grant list: the ref stays MOUNTED (still in
        ``tools``) but loses its blanket auto-approve, so its calls prompt and
        the gate applies the real per-tool rule.
        """
        data = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert "@kirocrew-core" in data["tools"]
        assert "@kirocrew-core" not in data["allowedTools"]
        assert data["allowedTools"] == ["session", "report"]

    def test_kas_permissions_are_derived_from_the_filtered_grants(self, tmp_path, monkeypatch):
        """The KAS block is derived, not restated — so the ceiling reaches it too.

        Ungoverned: the ``@kirocrew-core`` grant projects to an ``mcp`` allow
        rule. Governed: the grant is gone, so the rule is too — and the key is
        still present, because its mere presence is what makes KAS load the spec.
        """
        data = self._install(tmp_path, monkeypatch)
        assert data["permissions"] == {
            "rules": [{"capability": "mcp", "match": ["kirocrew-core/*"], "effect": "allow"}]
        }

        governed = self._install(
            tmp_path,
            monkeypatch,
            may_auto_approve=lambda ref: ref != "@kirocrew-core",
        )
        assert governed["permissions"] == {"rules": []}

    def test_withholding_a_grant_is_audit_logged(self, tmp_path, monkeypatch):
        """A withheld grant is a permission DECISION and must leave a record.

        Every other writer of an ``allowedTools`` list emits this event;
        ``strip_ungoverned_auto_approve`` names a silent pop as the one withhold
        path with no audit trail. Filtering silently here would make this
        installer exactly that path.
        """
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core")
        withheld = [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"]
        assert len(withheld) == 1
        assert "@kirocrew-core" in withheld[0]["resources"]
        assert withheld[0]["source"] == "_install_conductor_agent"

    def test_no_audit_event_when_nothing_is_withheld(self, tmp_path, monkeypatch):
        """An ungoverned host withholds nothing, so it records no decision."""
        calls: list[dict] = []

        class _Recorder:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Recorder())
        self._install(tmp_path, monkeypatch)
        assert [c for c in calls if c.get("operation") == "mcp_auto_approve_withheld"] == []

    def test_audit_failure_does_not_break_the_install(self, tmp_path, monkeypatch):
        """The spec still lands when the audit sink is unavailable."""

        class _Broken:
            def log_api_access(self, **kw):
                raise RuntimeError("sel down")

        monkeypatch.setattr(agent, "sel", lambda: _Broken())
        data = self._install(
            tmp_path, monkeypatch, may_auto_approve=lambda ref: ref != "@kirocrew-core"
        )
        assert data["allowedTools"] == ["session", "report"]

    def test_skill_documents_artifacts_as_a_string_map(self):
        """The ledger's ``artifacts`` values MUST be JSON-serialized strings.

        ``session_ledger`` handler validation rejects a non-string value with
        ``artifacts_not_string_map`` (HTTP 400), so a skill that told the
        conductor to send a nested object would lose every dispatched item's
        state — the exact durability this entry exists to provide. Pinned as a
        doc ratchet because the instruction, not the code, is what would drift.

        The format is owned by ``scripts/ledger_entry.py`` (issue #5912), so
        the skill must route encoding through the codec rather than carrying a
        hand-written byte-format example for models to re-derive — the worked
        example was the specification once, and produced real defects on
        PR #5652.
        """
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "artifacts_not_string_map" in text
        assert "map of string to STRING" in text
        # The codec is the format's one code owner: the skill must direct the
        # conductor to it for encode/decode/validate/rotate...
        assert "scripts/ledger_entry.py" in text
        assert "ledger_entry.py encode" in text
        # ...and must never show a bare `item-1 -> {` object literal, which is
        # exactly the value shape the ledger rejects.
        assert "item-1 -> {" not in text

    def test_spec_is_registered_as_kirocrew_owned(self):
        """Every managed spec registers in ``OWNED_KIRO_AGENT_FILES``.

        Three consumers key off that tuple (the Playwright convergence sweep,
        ``doctor``'s dead-path repair, connection minting). Absent from it, a
        conductor spec whose resolved MCP command path dies is classified as a
        foreign file and reported as unfixable instead of being repaired.
        """
        assert CONDUCTOR_AGENT_FILENAME in OWNED_KIRO_AGENT_FILES

    def test_builtin_skill_does_not_collide_with_the_delegation_skill(self):
        """The packaged skill must NOT be named ``conductor``.

        ``conductor_skill.generate_conductor_skill`` owns
        ``<skills>/conductor/SKILL.md``, and two paths DELETE that file when
        ``agent.conductor_skill`` is false (the default): ``cli_setup`` on every
        setup run and the dashboard config handler on toggle-off. A packaged
        skill sharing the name would be erased on a stock install.
        """
        assert SKILL_DIR.is_dir()
        assert not (_BUILTIN_SKILLS_DIR / "conductor").exists()
        assert "name: goal-conductor" in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _load_evaluator():
    """Load accept_eval.py by file location, via the shared no-bytecode helper.

    The script lives inside the checked-in skill dir (no package import path);
    loading it by file location is what lets a test assert on its internals.
    ``load_skill_script`` is the residue-guarded loader — a hand-rolled
    ``exec_module`` here would drop ``__pycache__`` beside the checked-in
    script, the exact side effect ``no-test-side-effects`` forbids.
    """
    return load_skill_script("_accept_eval_under_test", SCRIPT)


class TestAcceptEvaluatorInvariant:
    """No model-authored argv may reach subprocess.

    This is the property three review rounds converged on: constraining a
    spec-supplied argv (allowlist, basename check) never closes the class,
    because the script runs as an approved wrapper and Kiro Crew's
    denied-command floor cannot see the argv it receives on stdin. The fix was
    to stop accepting one at all.
    """

    def test_pr_checks_builds_its_own_argv(self):
        """The only exec path constructs argv from narrowly-typed fields."""
        mod = _load_evaluator()
        seen = []
        mod._run = lambda argv, cwd=None: (seen.append((argv, cwd)), ("pass", "ok"))[1]
        verdict, _ = mod._evaluate(
            {"accept": {"kind": "pr_checks", "pr": 123, "repo": "owner/name"}}
        )
        assert verdict == "pass"
        assert seen == [(["gh", "pr", "checks", "123", "--repo", "owner/name"], None)]

    def test_run_refuses_a_command_it_did_not_build(self):
        """The internal guard fails closed if a handler ever leaks spec input."""
        mod = _load_evaluator()
        verdict, evidence = mod._run(["git", "--version"])
        assert verdict == "refused"
        assert "not a command this script builds" in evidence
        assert mod._SELF_BUILT_COMMANDS == {"gh"}

    def test_no_spec_field_can_name_a_command(self):
        """Source ratchet: no handler may read an argv/command/shell spec field.

        A behavioural test only covers the kinds that exist today; this one fails
        if a future kind re-introduces the shape, which is the actual regression
        to prevent.
        """
        src = SCRIPT.read_text(encoding="utf-8")
        for banned in ('accept.get("argv")', 'accept.get("command")', 'accept.get("shell")'):
            assert banned not in src, f"a spec field must never name a command: {banned}"

    def test_pr_checks_rejects_a_non_integer_pr(self):
        """Including bool, which is an int subclass and would render as 'True'."""
        mod = _load_evaluator()
        for bad in ("123", True, None, 12.5):
            verdict, evidence = mod._evaluate({"accept": {"kind": "pr_checks", "pr": bad}})
            assert verdict == "error", bad
            assert "integer pr" in evidence


class TestAcceptEvaluator:
    def _run(self, items):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps({"items": items}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return {r["id"]: r for r in json.loads(proc.stdout)["results"]}

    def test_verdict_vocabulary_across_kinds(self, tmp_path):
        exists = tmp_path / "made"
        exists.write_text("x", encoding="utf-8")
        out = self._run(
            [
                {"id": "have", "accept": {"kind": "file", "path": str(exists), "exists": True}},
                {
                    "id": "miss",
                    "accept": {"kind": "file", "path": str(tmp_path / "no"), "exists": True},
                },
                {"id": "human", "accept": {"kind": "human_approval"}},
                {"id": "junk", "accept": {"kind": "wat"}},
                {"id": "nopath", "accept": {"kind": "file"}},
            ]
        )
        assert out["have"]["verdict"] == "pass"
        assert out["miss"]["verdict"] == "fail"
        assert out["human"]["verdict"] == "pending"
        assert out["junk"]["verdict"] == "error"
        assert out["nopath"]["verdict"] == "error"

    def test_the_cmd_kind_is_refused_and_says_what_to_use(self):
        """A conductor carrying an older skill gets guidance, not 'unknown kind'.

        `cmd` used to exist, so the removal is named explicitly: the refusal
        points at `pr_checks` and notes it already covers "the tests pass",
        since CI runs them.
        """
        out = self._run(
            [{"id": "old", "accept": {"kind": "cmd", "argv": ["git", "reset", "--hard"]}}]
        )
        assert out["old"]["verdict"] == "refused"
        assert "may not name a command" in out["old"]["evidence"]
        assert "pr_checks" in out["old"]["evidence"]

    def test_a_non_object_item_does_not_abort_the_run(self):
        """ID extraction is inside the per-item guard.

        ``{"items": [1, {...}]}`` raises on ``.get`` before the handler runs; if
        that raise escaped, every sibling verdict would be lost.
        """
        out = self._run([1, "nope", {"id": "fine", "accept": {"kind": "human_approval"}}])
        assert out["fine"]["verdict"] == "pending"
        assert out["#0"]["verdict"] == "error"
        assert out["#1"]["verdict"] == "error"
        assert "JSON object" in out["#0"]["evidence"]

    def test_malformed_stdin_is_a_clean_exit_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert proc.returncode == 2

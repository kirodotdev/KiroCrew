"""The ``kirocrew-work`` ``accept_eval`` tool.

It runs goal-conductor's bundled evaluator in-process so a conductor's verify
cycle needs no ``execute_bash`` approval. Pinned here:

* **Same logic, imported.** The tool calls the packaged script's own functions,
  and agrees with the script run by stdin.
* **Fixed argv.** No argument names a command; the only argv that reaches
  ``subprocess`` is the evaluator's own template, and ``cmd`` stays refused.
* **Typed input.** A malformed document is refused by the schema, not run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import agent, channel, mcp_core, mcp_work
from kiro_crew.skills import _BUILTIN_SKILLS_DIR
from kiro_crew.validation import ValidationError

SCRIPTS = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "scripts"


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def caller(monkeypatch):
    """A strict caller the gateway's ledger gate admits."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-x")
    seen: list[str] = []

    def _admit(path, **kwargs):
        seen.append(path)
        return {"conductor": {}, "items": []}

    monkeypatch.setattr(mcp_work, "_get", _admit)
    return seen


@pytest.fixture
def argvs(monkeypatch):
    """Record every argv the evaluator runs; answer each as green."""
    seen: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        assert kwargs.get("shell", False) is False
        seen.append(list(argv))
        return _Proc(0, "all checks passed")

    mod = mcp_work._skill_script("accept_eval")
    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    return seen


def _call(name: str, args: dict) -> str:
    return mcp_work._call_tool_inner(name, mcp_work._validate_args(name, args))


def test_the_check_is_advertised_and_granted_to_the_conductor_and_team_lead():
    names = [t["name"] for t in mcp_work._list_tools()]
    assert names[-1] == "accept_eval"
    assert mcp_work.CHECK_TOOLS == ("accept_eval",)
    # One tuple serves kirocrew-conductor and kirocrew-team-lead.
    for tool in mcp_work.CHECK_TOOLS:
        assert f"@kirocrew-work/{tool}" in agent._LEDGER_CONDUCTOR_WORK_GRANTS
        assert tool in channel.CHANNEL_AGENT_BLOCKED_TOOLS


def test_no_advertised_field_carries_a_command_or_path_to_run():
    """``items`` is a document to judge and ``item_id`` names one ledger item.
    Neither is a command, an argv or a path to run."""
    defs = {t["name"]: t for t in mcp_work._list_tools()}
    assert set(defs["accept_eval"]["inputSchema"]["properties"]) == {"items", "item_id"}


def test_the_tool_loads_the_packaged_script():
    assert Path(mcp_work._skill_script("accept_eval").__file__) == SCRIPTS / "accept_eval.py"


def test_the_retired_patrol_budget_tool_is_gone(caller):
    """rfc-permanent-patrol.md: a work-ledger patrol has no bound to check."""
    assert "patrol_budget" not in {t["name"] for t in mcp_work._list_tools()}
    assert not (SCRIPTS / "patrol_budget.py").exists()
    out = mcp_work._call_tool_inner("patrol_budget", {"mode": "check"})
    assert out == "Error: unknown tool 'patrol_budget'"
    assert caller == []


def test_a_pr_checks_batch_runs_only_the_fixed_gh_argv(caller, argvs):
    out = json.loads(
        _call(
            "accept_eval",
            {
                "items": [
                    {
                        "id": "it_00000001",
                        "accept": {"kind": "pr_checks", "pr": 42, "repo": "owner/name"},
                        "status": "done",
                        # Extra spec text never reaches argv.
                        "argv": ["rm", "-rf", "/"],
                    }
                ]
            },
        )
    )
    assert out["results"][0]["id"] == "it_00000001"
    assert out["results"][0]["verdict"] == "pass"
    assert argvs == [["gh", "pr", "checks", "42", "--repo", "owner/name"]]


def test_a_file_batch_answers_both_ways(caller, argvs, tmp_path):
    present = tmp_path / "out.md"
    present.write_text("x", encoding="utf-8")
    out = json.loads(
        _call(
            "accept_eval",
            {
                "items": [
                    {"id": "a", "accept": {"kind": "file", "path": str(present)}},
                    {"id": "b", "accept": {"kind": "file", "path": str(tmp_path / "nope")}},
                ]
            },
        )
    )
    assert [r["verdict"] for r in out["results"]] == ["pass", "fail"]
    assert argvs == []


def test_a_screened_out_file_path_is_refused_without_a_probe(caller, argvs, monkeypatch):
    """A sensitive path, or a UNC share whose probe would authenticate over SMB on
    Windows, is refused before the evaluator stats anything; siblings keep their
    order and are probed at the screen's canonical path."""
    from kiro_crew import hooks

    unc = "\\\\attacker\\share\\x"
    monkeypatch.setattr(
        hooks, "validate_file_path", lambda raw: None if raw == unc else "/canonical" + raw
    )
    probed: list[str] = []
    mod = mcp_work._skill_script("accept_eval")
    monkeypatch.setattr(mod.Path, "exists", lambda self: probed.append(str(self)) or False)
    out = json.loads(
        _call(
            "accept_eval",
            {
                "items": [
                    {"id": "a", "accept": {"kind": "file", "path": "/x"}},
                    {"id": "unc", "accept": {"kind": "file", "path": unc}},
                    {"id": "h", "accept": {"kind": "human_approval"}},
                ]
            },
        )
    )
    assert [(r["id"], r["verdict"]) for r in out["results"]] == [
        ("a", "fail"),
        ("unc", "refused"),
        ("h", "pending"),
    ]
    # Path.exists is patched process-wide, so config reads are probed too; only
    # the two item paths matter here.
    item_probes = [p for p in probed if "canonical" in p or "attacker" in p]
    assert item_probes == [str(Path("/canonical/x"))]


def test_the_cmd_kind_is_still_refused_and_runs_nothing(caller, argvs):
    out = json.loads(
        _call(
            "accept_eval",
            {"items": [{"id": "c", "accept": {"kind": "cmd", "argv": ["touch", "/tmp/x"]}}]},
        )
    )
    assert out["results"][0]["verdict"] == "refused"
    assert "'cmd' kind was removed" in out["results"][0]["evidence"]
    assert argvs == []


@pytest.mark.parametrize(
    "args",
    [
        {"items": "not a list"},
        {"items": [], "command": "ls"},  # an unknown field
        {"items": [{}] * 257},
        {"item_id": "not-an-item-id"},
    ],
)
def test_a_malformed_document_is_refused_by_the_schema(args):
    with pytest.raises(ValidationError):
        mcp_work._validate_args("accept_eval", args)


def test_a_non_object_item_is_one_error_verdict_not_a_crash(caller, argvs):
    out = json.loads(
        _call("accept_eval", {"items": [1, {"id": "ok", "accept": {"kind": "human_approval"}}]})
    )
    assert [r["verdict"] for r in out["results"]] == ["error", "pending"]


def test_the_tool_and_the_script_agree(caller, tmp_path):
    doc = {"items": [{"id": "a", "accept": {"kind": "file", "path": str(tmp_path)}}, {"id": "b"}]}
    proc = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "accept_eval.py")],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(_call("accept_eval", doc)) == json.loads(proc.stdout)
    # Malformed JSON on stdin: the script's own refusal is unchanged.
    bad = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "accept_eval.py")],
        input="{not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert bad.returncode == 2
    assert json.loads(bad.stdout) == {"error": 'stdin must be JSON: {"items": [...]}'}


def test_each_check_passes_the_ledger_gate_first(caller, argvs):
    _call("accept_eval", {"items": [{"id": "h", "accept": {"kind": "human_approval"}}]})
    assert caller == ["/api/work-ledger?compact=true"]


@pytest.mark.parametrize("code", ["channel_session", "restricted_session", "no_ledger"])
@pytest.mark.parametrize("tool", mcp_work.CHECK_TOOLS)
def test_a_caller_the_ledger_gate_refuses_runs_no_check(tool, code, monkeypatch):
    """Auto-approved calls never reach the channel block's permission prompt, so the
    gateway's ledger gate is what keeps a channel or restricted session out."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "channel:slack:x")
    monkeypatch.setattr(mcp_work, "_get", lambda *a, **k: {"error": "refused", "code": code})
    monkeypatch.setattr(mcp_work, "_skill_script", lambda name: pytest.fail("ran"))
    out = mcp_work._call_tool_inner(tool, {"items": []})
    assert out.startswith("Error:") and f"[{code}]" in out


@pytest.mark.parametrize("tool", mcp_work.CHECK_TOOLS)
def test_a_subagent_identity_is_refused(tool, monkeypatch):
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    monkeypatch.setattr(mcp_work, "_skill_script", lambda name: pytest.fail("ran"))
    assert mcp_work._call_tool_inner(tool, {}).startswith("Error:")

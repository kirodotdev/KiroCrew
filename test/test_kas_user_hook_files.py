"""The user's standalone ``~/.kiro/hooks/*.json`` files run on KAS; project ones do not.

``kiro-cli chat --agent-engine v3`` runs the user's standalone v1 hook files. Crew
fires the USER files on KAS through the same turn-loop ``extra_hooks`` path the
agent spec's hooks ride, so they meet the same gates. Project
``<project>/.kiro/hooks/*.json`` stay off -- Crew cannot read a checkout's
workspace-trust verdict -- and the session says so.

These are behaviour tests: they drive the real loader and the real
``ScriptHookStore.fire`` path and assert what runs, not that a symbol exists.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookStore,
)


def _passthrough_sandbox(monkeypatch):
    monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr("kiro_crew.sandbox.cgroup_scope_argv", lambda argv: list(argv))


def _marker_command(tmp_path: Path, name: str) -> str:
    """A command that writes a marker file, so a fire is observable."""
    script = tmp_path / f"{name}.py"
    out = tmp_path / f"{name}.marker"
    script.write_text(
        f"open({str(out)!r}, 'w', encoding='utf-8').write('fired')\n", encoding="utf-8"
    )
    return f'"{sys.executable}" "{script}"'


def _write_user_hook(home: Path, filename: str, hooks: list[dict]) -> None:
    hooks_dir = home / ".kiro" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / filename).write_text(
        json.dumps({"version": "v1", "hooks": hooks}), encoding="utf-8"
    )


def _point_hooks_dir(monkeypatch, home: Path) -> None:
    """Point the user hooks directory at ``home/.kiro/hooks`` via the documented seam.

    The resolver reads ``agent._DEFAULT_KIRO_HOOKS_DIR`` (the same constant the
    ``*.sh`` autoimport tests patch) and ``agent.kiro_hooks_dir`` from the mc
    config. Patch the constant to the temp HOME's hooks dir and point the config
    path at a non-existent file so no custom override is read.
    """
    import kiro_crew.agent as agent_mod

    (home / ".kiro" / "hooks").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_mod, "_DEFAULT_KIRO_HOOKS_DIR", home / ".kiro" / "hooks")
    monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: home / "no-such-config.json")
    # Clear the per-file conversion cache so a prior test's entries never leak.
    from kiro_crew.agent_sdk import spec_hooks as _sh

    _sh._user_file_cache.clear()


# (a) a temp-HOME user hook file's UserPromptSubmit command fires through the real
#     KAS turn-loop path (a marker file is written). RED on main: user files load
#     nowhere, so user_hook_files does not exist / returns nothing.
def test_a_user_prompt_submit_user_file_hook_fires(tmp_path, monkeypatch):
    _passthrough_sandbox(monkeypatch)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    home = tmp_path / "home"
    marker = tmp_path / "prompt.marker"
    _write_user_hook(
        home,
        "zz-probe.json",
        [
            {
                "name": "probe-prompt",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": _marker_command(tmp_path, "prompt")},
                "timeout": 10,
            }
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    hooks = spec_hooks.user_hook_files()
    assert [h.event for h in hooks] == [HOOK_EVENT_USER_PROMPT_SUBMIT]
    asyncio.run(
        ScriptHookStore(tmp_path).fire(HOOK_EVENT_USER_PROMPT_SUBMIT, "hi", extra_hooks=hooks)
    )
    assert marker.exists(), "the user file's UserPromptSubmit hook did not fire"


# (b) a project .kiro/hooks file does not fire and the notice is emitted.
def test_b_project_hook_files_are_not_loaded_and_notice_is_emitted(tmp_path, monkeypatch):
    # The loader reads only the USER directory; a project file is never returned.
    home = tmp_path / "home"
    (home / ".kiro" / "hooks").mkdir(parents=True)  # empty user dir
    project = tmp_path / "proj"
    (project / ".kiro" / "hooks").mkdir(parents=True)
    (project / ".kiro" / "hooks" / "p.json").write_text(
        json.dumps(
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "p",
                        "trigger": "UserPromptSubmit",
                        "action": {"type": "command", "command": "echo hi"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _point_hooks_dir(monkeypatch, home)
    assert spec_hooks.user_hook_files() == []
    # The detector that drives the session notice sees the project files.
    assert spec_hooks.project_hook_files_present(str(project)) is True
    assert spec_hooks.project_hook_files_present(None) is False


# (c) an unknown action type or trigger is skipped, not guessed.
def test_c_unknown_action_or_trigger_is_skipped(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_user_hook(
        home,
        "mixed.json",
        [
            {
                "name": "ok",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": "echo ok"},
            },
            {
                "name": "agent-action",
                "trigger": "Stop",
                "action": {"type": "agent", "prompt": "hi"},
            },
            {
                "name": "bad-trigger",
                "trigger": "Nope",
                "action": {"type": "command", "command": "echo no"},
            },
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    hooks = spec_hooks.user_hook_files()
    # Only the one command hook on a known trigger survives.
    assert [(h.event, h.command) for h in hooks] == [(HOOK_EVENT_USER_PROMPT_SUBMIT, "echo ok")]


# (d) a PreToolUse user-file hook that exits 2 blocks the tool.
def test_d_pre_tool_use_exit_2_blocks(tmp_path, monkeypatch):
    _passthrough_sandbox(monkeypatch)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    home = tmp_path / "home"
    script = tmp_path / "deny.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    _write_user_hook(
        home,
        "deny.json",
        [
            {
                "name": "deny",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": f'"{sys.executable}" "{script}"'},
            }
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    hooks = spec_hooks.user_hook_files()
    assert [h.event for h in hooks] == [HOOK_EVENT_PRE_TOOL_USE]
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE, tool_name="shell", tool_input={}, extra_hooks=hooks
        )
    )
    assert [(r.exit_code, r.pre_tool_blocks) for r in results] == [(2, True)]


# (e) DEDICATED no-double-run: the ~/.kiro/hooks SCRIPT autoimport does not also
#     pick up a *.json standalone file, so it fires exactly once (only via the
#     user-file loader, never again through spec hooks).
def test_e_json_file_is_not_also_picked_up_by_sh_autoimport(tmp_path, monkeypatch):
    from kiro_crew.agent_materialization import kiro_hooks

    home = tmp_path / "home"
    _write_user_hook(
        home,
        "once.json",
        [
            {
                "name": "once",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": "echo once"},
            }
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    hooks_dir = home / ".kiro" / "hooks"
    # The .sh autoimport scans *.sh only, so a *.json file yields no script-hook
    # entries -- the file cannot be double-counted into the spec's hooks.
    assert kiro_hooks._autoimport_kiro_hooks(hooks_dir) == {}
    # And the user-file loader returns it exactly once.
    loaded = spec_hooks.user_hook_files()
    assert len([h for h in loaded if h.command == "echo once"]) == 1


# ── Backend gating: user files + the project notice are KAS ONLY ──
# _prepare_spec_hooks runs for every backend in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
# (KAS, goose, opencode). The ruling is KAS only, so a goose/opencode session must
# load no user files and raise no project-files notice.
import asyncio as _asyncio  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from kiro_crew.agent_sdk.backends import (  # noqa: E402
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for  # noqa: E402
from kiro_crew.dashboard import chat_runner  # noqa: E402


def _agents_dir(tmp_path, monkeypatch) -> Path:
    import kiro_crew.config.paths as paths_mod

    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    (d / "a1.json").write_text(json.dumps({"name": "a1", "prompt": "p"}), encoding="utf-8")
    return d


def test_f_goose_session_does_not_load_user_files_or_notice(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_user_hook(
        home,
        "zz.json",
        [
            {
                "name": "probe",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": "echo hi"},
            }
        ],
    )
    # A project with standalone hook files too, to prove the notice is gated.
    project = tmp_path / "proj"
    (project / ".kiro" / "hooks").mkdir(parents=True)
    (project / ".kiro" / "hooks" / "p.json").write_text(
        json.dumps({"version": "v1", "hooks": []}), encoding="utf-8"
    )
    _point_hooks_dir(monkeypatch, home)
    _agents_dir(tmp_path, monkeypatch)
    notices: list = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: notices.append(text),
    )
    for backend in (ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE):
        client = _NS(capabilities=capabilities_for(backend), cwd=str(project))
        hooks, unreadable, _ = _asyncio.run(
            chat_runner._prepare_spec_hooks(_NS(), _NS(), client, "a1", is_new=True)
        )
        assert unreadable is False
        # No user-file hook loaded on goose/opencode.
        assert [h for h in hooks if h.spec_origin == "user_file"] == []
    # And no project-hook-files notice was surfaced for either.
    assert not any("hook files in .kiro/hooks" in n for n in notices)


def test_g_kas_session_does_load_user_files(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_user_hook(
        home,
        "zz.json",
        [
            {
                "name": "probe",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": "echo hi"},
            }
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    _agents_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(chat_runner, "append_and_surface", lambda *a, **k: None)
    client = _NS(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
    hooks, unreadable, _ = _asyncio.run(
        chat_runner._prepare_spec_hooks(_NS(), _NS(), client, "a1", is_new=True)
    )
    assert unreadable is False
    assert [h.command for h in hooks if h.spec_origin == "user_file"] == ["echo hi"]


# (h) F-A: a KAS sub-agent / task-runner turn fires a user-file PreToolUse hook,
#     and exit 2 blocks the tool. turn_spec_hooks is the seam both use.
def test_h_subagent_turn_fires_user_file_pre_tool_hook_and_exit_2_blocks(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    _passthrough_sandbox(monkeypatch)
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)
    home = tmp_path / "home"
    script = tmp_path / "deny.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    _write_user_hook(
        home,
        "deny.json",
        [
            {
                "name": "deny",
                "trigger": "PreToolUse",
                "action": {"type": "command", "command": f'"{sys.executable}" "{script}"'},
            }
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    _agents_dir(tmp_path, monkeypatch)
    # A KAS sub-agent/task-runner turn resolves its hooks through turn_spec_hooks.
    provider = _NS(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd=str(tmp_path))
    out = asyncio.run(spec_hooks.turn_spec_hooks(provider, "a1"))
    assert out.unreadable is False
    user = [h for h in out.hooks if h.spec_origin == "user_file"]
    assert [h.event for h in user] == [HOOK_EVENT_PRE_TOOL_USE]
    results = asyncio.run(
        ScriptHookStore(tmp_path).fire(
            HOOK_EVENT_PRE_TOOL_USE, tool_name="shell", tool_input={}, extra_hooks=out.hooks
        )
    )
    assert any(r.exit_code == 2 and r.pre_tool_blocks for r in results)


# (i) F-B: a user-file hook whose command is an autoimported *.sh already folded
#     into the spec fires exactly once (dedup on event/command/matcher, spec wins).
def test_i_a_json_hook_whose_command_is_an_autoimported_script_fires_once(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.hooks import ScriptHook

    command = "/usr/bin/true"
    _write_user_hook(
        tmp_path / "home",
        "probe.json",
        [
            {
                "name": "probe",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": command},
            }
        ],
    )
    _point_hooks_dir(monkeypatch, tmp_path / "home")
    provider = _NS(capabilities=capabilities_for(ACP_BACKEND_KAS), cwd="")
    # The spec already carries the same command (what the *.sh autoimport folds in).
    spec_hook = ScriptHook(id="spec:a1:0", event=HOOK_EVENT_USER_PROMPT_SUBMIT, command=command)
    merged = spec_hooks.merge_user_hook_files(provider, [spec_hook])
    assert [h.command for h in merged].count(command) == 1
    assert merged[0] is spec_hook  # the spec/autoimported hook wins


# (j) F-C: over-cap user files log + SEL-audit once per snapshot, not per turn.
def test_j_over_cap_user_files_audit_once_per_snapshot(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    home = tmp_path / "home"
    hooks_dir = home / ".kiro" / "hooks"
    hooks_dir.mkdir(parents=True)
    # More files than _MAX_HOOK_FILES, each a valid one-hook file.
    for n in range(spec_hooks._MAX_HOOK_FILES + 3):
        (hooks_dir / f"h{n:03d}.json").write_text(
            json.dumps(
                {
                    "version": "v1",
                    "hooks": [
                        {
                            "name": f"h{n}",
                            "trigger": "UserPromptSubmit",
                            "action": {"type": "command", "command": f"echo {n}"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    _point_hooks_dir(monkeypatch, home)
    spec_hooks._user_file_truncation_audited.clear()
    audits: list = []
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_sel_hook_rejected", lambda *a, **k: audits.append(a))
    spec_hooks.user_hook_files()
    spec_hooks.user_hook_files()  # unchanged snapshot: must NOT audit again
    assert len(audits) == 1, "over-cap drop should audit once per snapshot, not per call"


# (k) R2: a rejected kiro_hooks_dir is NOT logged or SEL-audited on each turn.
#     resolved_kiro_hooks_dir (the per-turn resolver) passes audit=False.
def test_k_rejected_dir_is_not_audited_per_turn(tmp_path, monkeypatch):
    import kiro_crew.agent as agent_mod
    from kiro_crew.agent_materialization import kiro_hooks

    # Point the mc config at one that sets kiro_hooks_dir OUTSIDE HOME.
    bad = tmp_path / "outside"
    bad.mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"agent": {"kiro_hooks_dir": str(bad)}}), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "_mc_config_path", lambda: cfg)
    # Make HOME a different tree so `bad` is genuinely outside it.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    audits: list = []
    warnings: list = []
    monkeypatch.setattr(agent_mod, "_sel_hook_rejected", lambda *a, **k: audits.append(a))
    monkeypatch.setattr(agent_mod.logger, "warning", lambda *a, **k: warnings.append(a))
    # Per-turn resolution (audit=False): no warning, no audit, even called twice.
    agent_mod.resolved_kiro_hooks_dir()
    agent_mod.resolved_kiro_hooks_dir()
    assert audits == [], "per-turn resolution must not SEL-audit a rejected dir"
    assert warnings == [], "per-turn resolution must not warn about a rejected dir"
    # The install path (audit default True) still reports it.
    agent_cfg = {"kiro_hooks_dir": str(bad)}
    kiro_hooks.resolve_kiro_hooks_dir(agent_cfg)
    assert len(audits) == 1, "the install path still audits a rejected dir once"


# (l) R3: a 30-command-hook file reports 10 dropped (cap is 20), counted before
#     _from_documents truncates. The message names the cap, not an exact count --
#     the loader stops at the cap and cannot know how many more there were.
def test_l_over_cap_single_file_keeps_cap_and_warns_naming_the_cap(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    home = tmp_path / "home"
    docs = [
        {
            "name": f"h{n}",
            "trigger": "UserPromptSubmit",
            "action": {"type": "command", "command": f"echo {n}"},
        }
        for n in range(30)
    ]
    _write_user_hook(home, "many.json", docs)
    _point_hooks_dir(monkeypatch, home)
    spec_hooks._user_file_truncation_audited.clear()
    reasons: list = []
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_sel_hook_rejected", lambda *a, **k: reasons.append(a[2]))
    out = spec_hooks.user_hook_files()
    assert len(out) == spec_hooks._MAX_SPEC_HOOKS  # exactly 20 retained
    assert len(reasons) == 1  # audited once
    # Honest message: names the cap, no exact dropped count.
    assert f"more than {spec_hooks._MAX_SPEC_HOOKS} hooks" in reasons[0]
    assert f"only the first {spec_hooks._MAX_SPEC_HOOKS} run" in reasons[0]


# (m) R3: adding a file past the file cap changes the snapshot and audits once more.
def test_m_adding_a_file_past_the_cap_changes_snapshot_and_audits(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    home = tmp_path / "home"
    hooks_dir = home / ".kiro" / "hooks"
    hooks_dir.mkdir(parents=True)
    for n in range(spec_hooks._MAX_HOOK_FILES):  # exactly at the file cap
        (hooks_dir / f"f{n:03d}.json").write_text(
            json.dumps(
                {
                    "version": "v1",
                    "hooks": [
                        {
                            "name": f"f{n}",
                            "trigger": "UserPromptSubmit",
                            "action": {"type": "command", "command": f"echo {n}"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    _point_hooks_dir(monkeypatch, home)
    spec_hooks._user_file_truncation_audited.clear()
    audits: list = []
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_sel_hook_rejected", lambda *a, **k: audits.append(a))
    spec_hooks.user_hook_files()  # at the file cap, over the hook cap -> audits once
    first = len(audits)
    spec_hooks.user_hook_files()  # unchanged snapshot -> no new audit
    assert len(audits) == first
    # Add a file PAST the file cap: the snapshot (which lists every name) changes.
    (hooks_dir / "zzz-extra.json").write_text(
        json.dumps(
            {
                "version": "v1",
                "hooks": [
                    {
                        "name": "extra",
                        "trigger": "UserPromptSubmit",
                        "action": {"type": "command", "command": "echo extra"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec_hooks.user_hook_files()
    assert len(audits) == first + 1, "a file added past the cap re-audits once"


# (n) S1: an under-cap file with a disabled (or confirm/unknown-trigger) document
#     is NOT counted as a cap drop, so it writes no caps WARNING or SEL audit.
def test_n_under_cap_skipped_doc_does_not_audit_as_a_cap_drop(tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import spec_hooks

    home = tmp_path / "home"
    _write_user_hook(
        home,
        "mixed.json",
        [
            {
                "name": "ok",
                "trigger": "UserPromptSubmit",
                "action": {"type": "command", "command": "echo ok"},
            },
            {
                "name": "off",
                "trigger": "UserPromptSubmit",
                "enabled": False,
                "action": {"type": "command", "command": "echo off"},
            },
            {
                "name": "confirm-me",
                "trigger": "Stop",
                "confirm": True,
                "action": {"type": "command", "command": "echo c"},
            },
            {
                "name": "bad-trigger",
                "trigger": "Nope",
                "action": {"type": "command", "command": "echo no"},
            },
        ],
    )
    _point_hooks_dir(monkeypatch, home)
    spec_hooks._user_file_truncation_audited.clear()
    reasons: list = []
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_sel_hook_rejected", lambda *a, **k: reasons.append(a))
    out = spec_hooks.user_hook_files()
    # Only the one enabled command hook runs; the rest are skipped for their own
    # reasons, well under the cap.
    assert [h.command for h in out] == ["echo ok"]
    # No caps audit, because no cap was hit.
    assert not any(
        a and a[0] == "user_hook_files" for a in reasons
    ), "a disabled/confirm/unknown-trigger doc under the cap must not audit as a cap drop"

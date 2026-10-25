"""OpenCode v2 contract generation: seed shape, read-back, and request shapes.

v2 (measured live on 2.0.29) differs from the v1 line in four places this file
pins, each failing apart:

* **Seed shape.** ``permissions[]`` rules with ``shell`` actions instead of the
  ``permission`` map. Crew's ask-all rule rides LAST so last-match-wins keeps
  it above ambient and deny rules.
* **Read-back basis.** v2 exposes no resolved-permission introspection, so the
  per-spawn check validates the seed document Crew hands over (ask-all last),
  the binary generation, and that the binary runs -- while env-supremacy over
  file scopes and per-call emission stay structural measurements re-proven by
  the contract lane, not parsed here.
* **Model selection.** Both generations select the model over
  ``session/set_config_option`` (opencode was already in
  ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``); v2 only removed the
  ``session/set_model`` alternative (``-32601``). Pinned across generations
  so a future refactor cannot silently move opencode back.
* **Reply vocabulary.** The client already echoes advertised option ids, so
  v2's ``once``/``always``/``reject`` needs no client change -- asserted here
  by the absence of any opencode-specific mapping, not by a test that would
  duplicate the generic path.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from kiro_crew.acp import client_model_selection
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_start import _opencode_v2_set_mode_params
from kiro_crew.acp.harness import opencode as opencode_mod
from kiro_crew.agent_sdk.backends import (
    OPENCODE_ACP_V2_MAJOR,
    opencode_major_version,
)

_ENV_CONFIG = "OPENCODE_CONFIG_CONTENT"


@pytest.fixture(autouse=True)
def _no_ambient_opencode_env(_floor_monkeypatch):
    """The operator's own shell must not leak into seed assertions."""
    _floor_monkeypatch.delenv(_ENV_CONFIG, raising=False)


# ── Version parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("banner", "expected"),
    [
        ("1.18.35", 1),
        ("1.19.1", 1),
        ("opencode v2.0.29", 2),
        ("2.0.30", 2),
        ("opencode v10.0.0", 10),
        ("", None),
        ("latest", None),
        ("v2", None),
        ("opencode v2.0", None),
    ],
)
def test_major_version_parses_both_banner_shapes(banner, expected):
    assert opencode_major_version(banner) == expected


def test_v2_floor_is_generation_boundary():
    assert OPENCODE_ACP_V2_MAJOR == 2


# ── v2 seed ──────────────────────────────────────────────────────────────────


def test_v2_seed_is_ask_all_only_without_denies():
    assert json.loads(opencode_mod._opencode_v2_routing_config(())) == {
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}]
    }


def test_v2_seed_denies_ride_before_ask_all_sorted():
    seed = json.loads(opencode_mod._opencode_v2_routing_config(("zeta_tool", "alpha_tool")))
    assert seed["permissions"] == [
        {"action": "alpha_tool", "resource": "*", "effect": "deny"},
        {"action": "zeta_tool", "resource": "*", "effect": "deny"},
        {"action": "*", "resource": "*", "effect": "ask"},
    ]


def test_v2_seed_merges_ambient_rules_first(monkeypatch):
    monkeypatch.setenv(
        _ENV_CONFIG,
        json.dumps(
            {
                "permissions": [{"action": "shell", "resource": "git status *", "effect": "allow"}],
                "model": "other/model",
            }
        ),
    )
    seed = json.loads(opencode_mod._opencode_v2_routing_config(()))
    assert seed["permissions"][0] == {
        "action": "shell",
        "resource": "git status *",
        "effect": "allow",
    }
    assert seed["permissions"][-1] == {
        "action": "*",
        "resource": "*",
        "effect": "ask",
    }


def test_v2_seed_keeps_the_operators_own_keys(monkeypatch):
    """Only ``permissions`` is rebuilt: provider/model config rides along as on v1,
    the operator's own policy denies stay ahead of Crew's, and the v1 map is dropped."""
    blocked = {"action": "integration.use", "resource": "mcp:theirs", "effect": "deny"}
    monkeypatch.setenv(
        _ENV_CONFIG,
        json.dumps(
            {
                "model": "other/model",
                "provider": {"p": {"npm": "x"}},
                "policies": [blocked],
                "permission": "allow",
            }
        ),
    )
    seed = json.loads(opencode_mod._opencode_v2_routing_config((), mcp_deny_servers=("core",)))
    assert seed["model"] == "other/model"
    assert seed["provider"] == {"p": {"npm": "x"}}
    assert "permission" not in seed
    assert seed["policies"] == [
        blocked,
        {"action": "integration.use", "resource": "mcp:core", "effect": "deny"},
    ]
    assert seed["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]


def test_v2_seed_ignores_unparseable_ambient(monkeypatch):
    monkeypatch.setenv(_ENV_CONFIG, "not json{")
    assert json.loads(opencode_mod._opencode_v2_routing_config(())) == {
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}]
    }


# ── Seeded deny parsing across generations ───────────────────────────────────


def test_seeded_denies_reads_v1_map():
    content = json.dumps({"permission": {"*": "ask", "srv_tool": "deny"}})
    assert opencode_mod._opencode_seeded_deny_rules(content, "permission") == ("srv_tool",)


def test_seeded_denies_reads_v2_array():
    content = json.dumps(
        {
            "permissions": [
                {"action": "srv_tool", "resource": "*", "effect": "deny"},
                {"action": "*", "resource": "*", "effect": "ask"},
            ]
        }
    )
    assert opencode_mod._opencode_seeded_deny_rules(content, "permission") == ("srv_tool",)


def test_seeded_denies_rejects_garbage():
    assert opencode_mod._opencode_seeded_deny_rules("nope{", "permission") == ()


# ── Binary probe ─────────────────────────────────────────────────────────────


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _checked_major(monkeypatch, tmp_path, *, stdout="", returncode=0, raise_exc=None):
    """Drive the launch's version gate at the subprocess seam."""

    def _run(argv, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return _Completed(returncode, stdout)

    async def _wrap(argv, **kwargs):
        return ["/opt/run/launcher", *argv], ""

    monkeypatch.setattr(opencode_mod.subprocess_mod, "run", _run)
    monkeypatch.setattr(opencode_mod, "wrap_argv_async", _wrap)
    session = AcpClient(work_dir=tmp_path, acp_backend="opencode")
    return asyncio.run(
        opencode_mod._opencode_checked_major(
            session, "/bin/opencode", sandbox_mode="standard", hidden=(), expose=()
        )
    )


def test_gate_admits_v2_and_answers_its_major(monkeypatch, tmp_path):
    assert _checked_major(monkeypatch, tmp_path, stdout="opencode v2.0.29\n") == (2, "", "")


def test_gate_admits_v1_and_answers_its_major(monkeypatch, tmp_path):
    assert _checked_major(monkeypatch, tmp_path, stdout="1.19.1\n") == (1, "", "")


@pytest.mark.parametrize("banner", ["opencode v3.0.0", "0.104.0"])
def test_gate_refuses_an_unmeasured_major(monkeypatch, tmp_path, banner):
    major, issue, _remedy = _checked_major(monkeypatch, tmp_path, stdout=banner)
    assert major == int(banner.split()[-1].lstrip("v").split(".")[0])
    assert "major 1 or 2" in issue


def test_gate_fails_closed_when_the_probe_cannot_run(monkeypatch, tmp_path):
    major, issue, _remedy = _checked_major(monkeypatch, tmp_path, raise_exc=OSError("no such file"))
    assert major is None
    assert "did not report a readable version" in issue


# ── v2 read-back ─────────────────────────────────────────────────────────────


class _Session:
    _spawn_work_dir = "/tmp"

    @staticmethod
    def _scrub_observed(value):
        return value


def _run_readback(monkeypatch, *, major, seed, rc=0):
    monkeypatch.setattr(
        opencode_mod.subprocess_mod,
        "run",
        lambda *a, **k: _Completed(rc, "{}", ""),
    )
    return opencode_mod._verify_opencode_v2_routing(
        _Session(), "opencode", ["/bin/opencode", "debug", "config"], seed, {}, major
    )


def test_v2_readback_accepts_seeded_ask_all(monkeypatch):
    seed = opencode_mod._opencode_v2_routing_config(("srv_tool",))
    issue, _remedy = _run_readback(monkeypatch, major=2, seed=seed)
    assert issue == ""


def test_v2_readback_refuses_unknown_major(monkeypatch):
    seed = opencode_mod._opencode_v2_routing_config(())
    issue, _remedy = _run_readback(monkeypatch, major=None, seed=seed)
    assert issue != ""


def test_v2_readback_refuses_wrong_generation(monkeypatch):
    seed = opencode_mod._opencode_v2_routing_config(())
    issue, _remedy = _run_readback(monkeypatch, major=1, seed=seed)
    assert issue != ""


def test_v2_readback_refuses_seed_without_ask_all_last(monkeypatch):
    seed = json.dumps({"permissions": [{"action": "*", "resource": "*", "effect": "allow"}]})
    issue, _remedy = _run_readback(monkeypatch, major=2, seed=seed)
    assert issue != ""


def test_v2_readback_refuses_unparseable_seed(monkeypatch):
    issue, _remedy = _run_readback(monkeypatch, major=2, seed="nope{")
    assert issue != ""


def test_v2_readback_refuses_harness_failure(monkeypatch):
    seed = opencode_mod._opencode_v2_routing_config(())
    issue, _remedy = _run_readback(monkeypatch, major=2, seed=seed, rc=1)
    assert issue != ""


# ── v2 MCP enforcement ─────────────────────────────────────────────────────


def test_v2_seed_policies_deny_withheld_servers():
    seed = json.loads(
        opencode_mod._opencode_v2_routing_config(("srv_tool",), mcp_deny_servers=("other", "core"))
    )
    assert seed["policies"] == [
        {"action": "integration.use", "resource": "mcp:core", "effect": "deny"},
        {"action": "integration.use", "resource": "mcp:other", "effect": "deny"},
    ]
    # permissions array still ends in ask-all
    assert seed["permissions"][-1] == {
        "action": "*",
        "resource": "*",
        "effect": "ask",
    }


def test_v2_seed_without_withheld_servers_has_no_policies():
    seed = json.loads(opencode_mod._opencode_v2_routing_config(("srv_tool",)))
    assert "policies" not in seed


@pytest.mark.parametrize(
    ("rule", "unenforced"),
    [
        ("kirocrew-core_monitor_start", True),
        ("shell", False),
        ("read", False),
        ("edit", False),
        ("execute", False),
        ("external_directory", False),
    ],
)
def test_v2_unenforced_is_mcp_ids_only(rule, unenforced):
    assert (rule in opencode_mod._opencode_v2_unenforced_denies((rule,))) is unenforced


def test_settle_withholds_narrowed_servers_on_v2():
    session = types.SimpleNamespace(_session_harness_deny_rules=("srv_tool",))
    seed = opencode_mod._opencode_v2_routing_config(("srv_tool",))
    lost = opencode_mod.settle_opencode_denies(session, seed)
    assert lost == frozenset({"srv_tool"})
    assert session._opencode_denies_in_force == frozenset()


def test_settle_keeps_native_denies_in_force_on_v2():
    session = types.SimpleNamespace(_session_harness_deny_rules=())
    seed = json.dumps(
        {
            "permissions": [
                {"action": "shell", "resource": "git push *", "effect": "deny"},
                {"action": "*", "resource": "*", "effect": "ask"},
            ]
        }
    )
    lost = opencode_mod.settle_opencode_denies(session, seed)
    assert lost == frozenset()
    assert session._opencode_denies_in_force == frozenset({"shell"})


def test_is_v2_seed_shape():
    assert opencode_mod._is_v2_seed_shape(json.dumps({"permissions": []})) is True
    assert opencode_mod._is_v2_seed_shape(json.dumps({"permission": {"*": "ask"}})) is False
    assert opencode_mod._is_v2_seed_shape("nope{") is False


# ── Agent pinning ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("backend", "agent", "version", "expected"),
    [
        (
            "opencode",
            "build",
            "2.0.29",
            {"sessionId": "ses_test", "modeId": "build"},
        ),
        (
            "opencode",
            "build",
            "2.0.30",
            {"sessionId": "ses_test", "modeId": "build"},
        ),
        ("opencode", "build", "1.18.35", None),
        ("opencode", "build", "1.19.1", None),
        ("opencode", "build", "", None),
        ("opencode", "", "2.0.29", None),
        ("goose", "build", "2.0.29", None),
        ("codex", "build", "2.0.29", None),
    ],
)
def test_set_mode_pin_is_v2_opencode_with_agent_only(backend, agent, version, expected):
    assert _opencode_v2_set_mode_params(backend, agent, version, "ses_test") == expected


def test_set_mode_pin_needs_a_session():
    assert _opencode_v2_set_mode_params("opencode", "build", "2.0.29", None) is None


# ── Model selection routing ──────────────────────────────────────────────────


@pytest.mark.parametrize("version", ["1.18.35", "1.19.1", "2.0.29", "2.0.30"])
def test_set_model_uses_config_option_on_both_generations(version):
    """opencode selects the model over ``session/set_config_option`` on v1 and
    v2 alike -- v2 only removed the ``session/set_model`` alternative, so this
    pins the shared path rather than branching on the generation."""
    calls = []
    stub = _stub_self(version=version, calls=calls)
    asyncio.run(client_model_selection.set_model(stub, "meta/muse-wonder-1.0"))
    assert ("set_config_option", "model", "meta/muse-wonder-1.0") in calls
    assert stub._model == "meta/muse-wonder-1.0"


def _stub_self(*, version, calls):
    async def fake_set_config_option(config_id, value):
        calls.append(("set_config_option", config_id, value))

    async def fake_send_request(method, params):
        calls.append(("send_request", method, params))
        return 1

    stub = types.SimpleNamespace(
        _session_id="ses_test",
        backend="opencode",
        _agent_version=version,
        _is_kiro=False,
        _uses_advertised_model_selection=False,
        _model_config_candidates=client_model_selection._model_config_candidates,
        model_pin_partial="",
        set_config_option=fake_set_config_option,
        _send_request=fake_send_request,
        _model="meta/muse-wonder-1.0",
        model_pin_refused="",
        _resolved_model_id="",
        _last_substitution_model=None,
        _seeds_local_settings=False,
    )
    stub._push_model_config_option = types.MethodType(
        client_model_selection._push_model_config_option, stub
    )
    stub.last_prompt_stats = types.SimpleNamespace(rebase_to_window=lambda *a, **k: None)
    return stub

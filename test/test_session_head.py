"""The session head: a new chat's session-stable blocks ride in the KAS agent prompt.

Decision: ``docs/request-for-change/rfc-kas-session-head-in-agent-prompt.md``. The
head is built only for the default agent with the managed prompt stub on a dashboard
key; each block leaves the first message only when the session records exactly that
text; and a resumed chat whose session cannot hold the head any more gets its agent
prompt back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import agent_sdk, session_head
from kiro_crew.acp.types import PROVIDER_LABEL_KAS
from kiro_crew.agent_parts.spec_build import _NATIVE_PROMPT_STUB
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import agent_spec
from kiro_crew.essential_delivery import EssentialDelivery
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

KEY = "dashboard:head-chat"
STUB_SPEC = {"name": "kirocrew", "prompt": _NATIVE_PROMPT_STUB}
AGENT_BLOCK = "[AGENT SYSTEM PROMPT]"
RULES_BLOCK = "[CRITICAL RULES"
TOOLS_BLOCK = "[TOOL NAMES]"


@pytest.fixture
def fixed_readings(monkeypatch):
    """Pin the host-derived prompt tokens, so the head and the turn read the same values."""
    for owner in (agent_spec, ContextBuilder):
        monkeypatch.setattr(owner, "_live_cap_figure", staticmethod(lambda: "4"))
        monkeypatch.setattr(owner, "_live_computer_use_gate", staticmethod(lambda: False))
    monkeypatch.setattr(agent_spec, "_live_browser_gate", lambda: True)
    # Admit the provider double below, as the real provider classes are admitted.
    monkeypatch.setattr(agent_sdk, "context_provider_of", lambda value: value)


def _kas_provider(recorded: dict[str, str], *, started: bool = False):
    """A KAS session whose runtime recorded *recorded*; no process."""
    return SimpleNamespace(
        session_head_started=started,
        session_head_held=session_head.applied(recorded),
        essential_delivery=EssentialDelivery(),
        context_provider_type=PROVIDER_LABEL_KAS,
        cwd="",
        native_context_documents=dict(recorded),
        native_steering=True,
        member_dispatch_mounted=None,
        served_model="",
    )


def _recorded(blocks) -> dict[str, str]:
    """What the runtime records off the wire definition the head was sent in."""
    held: dict[str, str] = {}
    prompt = session_head.with_head([{"id": "kirocrew"}], blocks)[0]["prompt"]
    session_head.record(held, "kirocrew", prompt)
    return held


def _turn(tmp_path, provider, *, resumed: bool = False, agent: str | None = None) -> str:
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message(
        "hello",
        True,
        KEY,
        agent=agent,
        resumed=resumed,
        provider_type=PROVIDER_LABEL_KAS,
        runtime_source="dashboard",
        context_provider=provider,
    )
    return msg


def test_the_head_holds_the_three_blocks_and_parses_back(fixed_readings):
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    assert [key for key, _ in blocks] == [
        session_head.AGENT_PROMPT_KEY,
        session_head.RULES_KEY,
        session_head.TOOL_NAMES_KEY,
    ]
    prompt = session_head.with_head([{"id": "kirocrew"}], blocks)[0]["prompt"]
    assert prompt == session_head.fold(session_head.head_prompt(blocks))
    assert prompt.endswith(session_head.HEAD_NOTE)
    assert session_head.parse(prompt) == tuple((k, session_head.fold(t)) for k, t in blocks)
    assert session_head.parse(prompt[: -len(session_head.HEAD_NOTE)]) == ()
    # The runtime appends a remote-workspace notice after the head; it is still a head.
    noticed = prompt + "\n\nThis session runs in a remote workspace.\n"
    assert session_head.parse(noticed) == session_head.parse(prompt)


@pytest.mark.parametrize(
    ("agent", "spec", "key"),
    [
        ("gpu-dev", {"name": "gpu-dev", "prompt": "You are the gpu-dev agent."}, KEY),
        ("gpu-dev", {"name": "gpu-dev", "prompt": _NATIVE_PROMPT_STUB}, KEY),
        ("kirocrew", {"name": "kirocrew", "prompt": "You are my own assistant."}, KEY),
        ("kirocrew", {"name": "kirocrew"}, KEY),
        ("kirocrew", STUB_SPEC, "cron:nightly"),
        ("kirocrew", STUB_SPEC, "subagent:abc"),
        ("kirocrew", STUB_SPEC, "1712345678.000100"),
        ("kirocrew", STUB_SPEC, ""),
    ],
    ids=[
        "custom-agent-own-prompt",
        "custom-agent-stub",
        "default-agent-person-prompt",
        "default-agent-no-prompt",
        "cron",
        "subagent",
        "slack-thread",
        "no-key",
    ],
)
def test_no_head_off_the_default_agent_stub_and_dashboard(fixed_readings, agent, spec, key):
    assert session_head.head_blocks(agent, spec, key, PROVIDER_LABEL_KAS) == ()


def test_the_head_replaces_only_the_default_agents_own_prompt(fixed_readings):
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    target = {"id": "reviewer", "prompt": "You review."}
    batch = [{"id": "kirocrew", "prompt": _NATIVE_PROMPT_STUB}, target]
    out = session_head.with_head(batch, blocks)
    assert out[0]["prompt"] == session_head.fold(session_head.head_prompt(blocks))
    assert out[1] is target
    assert batch[0]["prompt"] == _NATIVE_PROMPT_STUB
    switched = [{"id": "gpu-dev", "prompt": "You are the gpu-dev agent."}]
    assert session_head.with_head(switched, blocks) is switched


def test_a_head_is_recorded_only_for_the_default_agent(fixed_readings):
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    prompt = session_head.with_head([{"id": "kirocrew"}], blocks)[0]["prompt"]
    held: dict[str, str] = {}
    session_head.record(held, "gpu-dev", prompt)
    assert held == {}
    session_head.record(held, "kirocrew", "You are the gpu-dev agent.")
    assert held == {}
    session_head.record(held, "kirocrew", prompt)
    assert session_head.applied(held)


def test_a_fresh_turn_leaves_out_exactly_the_blocks_the_head_holds(tmp_path, fixed_readings):
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    # Control: with nothing recorded, the first message carries all three blocks.
    control = _turn(tmp_path, _kas_provider({}))
    assert AGENT_BLOCK in control and RULES_BLOCK in control and TOOLS_BLOCK in control
    msg = _turn(tmp_path, _kas_provider(_recorded(blocks)))
    assert AGENT_BLOCK not in msg and RULES_BLOCK not in msg and TOOLS_BLOCK not in msg
    assert "[CURRENT DATE]" in msg and "hello" in msg


def test_the_held_blocks_keep_their_charge_on_memory_admission(
    tmp_path, fixed_readings, monkeypatch
):
    from kiro_crew.context_assembly import budget

    charged: list[int] = []
    admit = budget.admit_background

    def spy(blocks, *, protected_chars, **kwargs):
        charged.append(protected_chars)
        return admit(blocks, protected_chars=protected_chars, **kwargs)

    monkeypatch.setattr(budget, "admit_background", spy)
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    _turn(tmp_path, _kas_provider({}))
    _turn(tmp_path, _kas_provider(_recorded(blocks)))
    assert len(charged) == 2 and charged[0] == charged[1]


def test_a_block_whose_record_differs_is_sent_as_before(tmp_path, fixed_readings):
    recorded = _recorded(session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS))
    recorded[session_head.RULES_KEY] += " "
    msg = _turn(tmp_path, _kas_provider(recorded))
    assert msg.count(RULES_BLOCK) == 1
    assert AGENT_BLOCK not in msg and TOOLS_BLOCK not in msg


def test_a_resume_that_lost_the_head_gets_the_agent_prompt_back(tmp_path, fixed_readings):
    plain = _turn(tmp_path, _kas_provider({}), resumed=True)
    assert AGENT_BLOCK not in plain
    resent = _turn(tmp_path, _kas_provider({}, started=True), resumed=True)
    contract = dict(session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS))
    block = session_head.agent_prompt_block(contract[session_head.AGENT_PROMPT_KEY])
    assert str(resent).count(session_head.fold(block)) == 1
    # The resume message re-anchors the critical rules on every slim resume, so a
    # head-started chat keeps them too.
    assert RULES_BLOCK in resent
    blocks = session_head.head_blocks("kirocrew", STUB_SPEC, KEY, PROVIDER_LABEL_KAS)
    held = _turn(tmp_path, _kas_provider(_recorded(blocks), started=True), resumed=True)
    assert AGENT_BLOCK not in held


@pytest.mark.parametrize(
    ("agent", "started", "held", "expected"),
    [
        (None, True, False, True),
        ("kirocrew", True, False, True),
        ("gpu-dev", True, False, False),
        (None, True, True, False),
        (None, False, False, False),
    ],
    ids=["default", "named-default", "switched-agent", "head-held", "no-head-start"],
)
def test_only_a_default_agent_resume_without_its_head_resends(agent, started, held, expected):
    provider = SimpleNamespace(session_head_started=started, session_head_held=held)
    assert session_head.resend_on_resume(agent, provider) is expected


@pytest.mark.parametrize("held", [True, False], ids=["head", "no-head"])
def test_the_claim_path_records_and_hands_back_the_head(tmp_path, monkeypatch, held):
    from kiro_crew.session_map import SessionMap
    from kiro_crew.session_parts.turn_context import note_session_head

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    manager = SimpleNamespace(_fold_key=lambda key: key, _session_map=SessionMap())
    manager._session_map.set(KEY, "sess_one", provider=PROVIDER_LABEL_KAS)
    fresh = SimpleNamespace(session_head_held=held, client=SimpleNamespace(_session_id="sess_one"))
    note_session_head(manager, KEY, fresh, is_new=True, resumed=False)
    resumed = SimpleNamespace(session_head_started=False)
    note_session_head(manager, KEY, resumed, is_new=False, resumed=True)
    assert resumed.session_head_started is held


def test_a_head_record_failure_never_fails_the_claim(caplog):
    from kiro_crew.session_parts.turn_context import note_session_head

    def broken(_key):
        raise OSError("map unreadable")

    manager = SimpleNamespace(
        _fold_key=lambda key: key, _session_map=SimpleNamespace(sid_head_pending=broken)
    )
    resumed = SimpleNamespace(session_head_started=False)
    note_session_head(manager, KEY, resumed, is_new=False, resumed=True)
    assert resumed.session_head_started is False
    assert "session head record not kept" in caplog.text


def test_the_head_record_is_scoped_to_the_mapped_sid(tmp_path, monkeypatch):
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    sm = SessionMap()
    sm.set(KEY, "sess_one", provider=PROVIDER_LABEL_KAS)
    assert sm.mark_sid_head(KEY, "sess_other") is False
    assert sm.sid_head_pending(KEY) is False
    assert sm.mark_sid_head(KEY, "sess_one") is True
    assert sm.sid_head_pending(KEY) is True
    sm.set(KEY, "sess_two", provider=PROVIDER_LABEL_KAS)
    assert sm.sid_head_pending(KEY) is False

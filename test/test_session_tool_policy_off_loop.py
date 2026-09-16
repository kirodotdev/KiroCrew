"""``api_session_tool_policy`` must not read the agent config on the gateway loop.

A managed MCP server (kirocrew-core, kirocrew-cron) calls this endpoint to filter
its tool list per agent, so it runs on ordinary request traffic. The handler
resolved the agent's config file inline::

    if not agent_path.is_file():        # stat
    config = json.loads(agent_path.read_text(...))   # read + parse

all three on the single event loop every other gateway request shares.

The proof below is thread identity at the real filesystem seam -- the hardened
``safe_read_file_bytes`` gate the strict spec reader opens the file through --
not an assertion that ``asyncio.to_thread`` was called. A spy on the offload
would keep passing if the call were later moved back inline behind some other
wrapper; the thread the read actually runs on cannot be faked.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew import agent_discovery
from kiro_crew.dashboard.handlers import sessions as sessions_mod

AGENT = "reviewer"


def _state(agent: str = AGENT) -> MagicMock:
    """A state whose session key resolves straight to *agent*."""
    state = MagicMock()
    slot = MagicMock()
    slot.agent = agent
    state.get_slot = MagicMock(return_value=slot)
    # Agent already resolved from the slot, so the session-manager fallback in
    # the handler must not be consulted.
    state.sessions = None
    return state


def _request(state: MagicMock) -> MagicMock:
    request = MagicMock()
    request.headers = {"X-Session-Key": f"dashboard:{AGENT}-slot"}
    request.app = {"state": state}
    return request


def _body(response: Any) -> Any:
    return json.loads(response.body.decode("utf-8"))


async def _call(monkeypatch: pytest.MonkeyPatch, agents_dir: Path) -> Any:
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    return await sessions_mod.api_session_tool_policy(_request(_state()))


@pytest.mark.asyncio
async def test_the_agent_config_read_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread that reads and parses the agent config is not the loop's."""
    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"managedToolPolicy": {"exclude": ["shell"]}}), encoding="utf-8"
    )

    read_threads: list[int] = []
    real_read = agent_discovery.safe_read_file_bytes

    def recording_read(raw: str) -> bytes | None:
        read_threads.append(threading.get_ident())
        return real_read(raw)

    monkeypatch.setattr(agent_discovery, "safe_read_file_bytes", recording_read)

    loop_thread = threading.get_ident()
    response = await _call(monkeypatch, tmp_path)

    assert _body(response) == {"exclude": ["shell"]}
    assert read_threads, "the agent config was never read"
    assert loop_thread not in read_threads, (
        "the agent config was read on the event-loop thread: the stat, the read "
        "and the JSON parse all block every other request on that loop"
    )


@pytest.mark.asyncio
async def test_a_missing_agent_config_is_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviour preserved: no file for this agent answers an empty policy."""
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@pytest.mark.asyncio
async def test_an_unparseable_agent_config_is_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviour preserved: malformed JSON answers {} rather than raising.

    The endpoint is deny-by-default about IDENTITY (an unresolved session is a
    400/404), but permissive about a policy it cannot read: an unreadable file
    must not take the MCP server's tool listing down.
    """
    (tmp_path / f"{AGENT}.json").write_text("{ not json", encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
async def test_a_valid_json_non_object_agent_config_is_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """A spec that is valid JSON but not an object parses fine, so the
    JSONDecodeError guard never fires — but ``.get`` on the parsed value would
    raise AttributeError out of the handler. It is a malformed spec: answer the
    same empty policy as the unparseable case, and do not log it as a success
    (only a config that was read AND understood earns the SEL ``ok`` record).
    """
    (tmp_path / f"{AGENT}.json").write_text(content, encoding="utf-8")

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert response.status == 200
    assert _body(response) == {}
    assert not sel.log_api_access.called, "a config that was never understood must not report ok"


@pytest.mark.asyncio
async def test_a_non_dict_policy_is_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviour preserved: a policy of the wrong shape is not handed through."""
    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"managedToolPolicy": ["exclude"]}), encoding="utf-8"
    )
    response = await _call(monkeypatch, tmp_path)
    assert _body(response) == {}


@pytest.mark.asyncio
async def test_an_agent_without_a_policy_key_is_reported_as_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{}`` from a READ config is not the same as ``{}`` from an unread one.

    Both answer an empty policy, but only the former is an agent whose config was
    parsed and understood -- which is what the handler's SEL ``ok`` record
    attests. Collapsing the two would start logging success for files that were
    never read, so the split is pinned here.
    """
    (tmp_path / f"{AGENT}.json").write_text(json.dumps({"name": AGENT}), encoding="utf-8")

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert _body(response) == {}
    assert sel.log_api_access.called, "a config that WAS read should log its ok"
    assert sel.log_api_access.call_args.kwargs["outcome"] == "ok"


@pytest.mark.asyncio
async def test_an_unread_config_is_not_logged_as_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the split: a missing file logs no success."""
    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert _body(response) == {}
    assert not sel.log_api_access.called, "an unread config must not report ok"


@pytest.mark.asyncio
async def test_a_namespaced_agent_resolves_its_policy_by_declared_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read resolves an agent the same way the KAS projection does.

    A package-installed agent is namespaced on disk as ``<package>-<name>.json``
    and dispatched under its bare ``name``. The projection resolves that spec by
    declared name, so the session starts; if this read still went by filename
    alone, the same agent would then answer an empty policy and its managed MCP
    servers would filter against nothing -- a session with the wrong tool
    surface, not a session that failed to start.
    """
    (tmp_path / f"SomePackage-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}}),
        encoding="utf-8",
    )

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert response.status == 200
    assert _body(response) == {"exclude": ["shell"]}
    assert sel.log_api_access.call_args.kwargs["outcome"] == "ok"


@pytest.mark.asyncio
async def test_the_projection_and_the_policy_read_resolve_the_same_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end for one namespaced agent: the spec the KAS projection starts
    the session from is the spec whose policy its MCP servers are then handed.

    Both go through ``agent_discovery.spec_by_declared_name``, so this pins the
    agreement rather than two lookups that happen to coincide today.
    """
    from kiro_crew.acp.kas_agents import load_agent_spec

    (tmp_path / f"SomePackage-{AGENT}.json").write_text(
        json.dumps(
            {
                "name": AGENT,
                "description": "namespaced",
                "managedToolPolicy": {"exclude": ["shell", "browser"]},
            }
        ),
        encoding="utf-8",
    )

    projected = load_agent_spec(tmp_path, AGENT)
    response = await _call(monkeypatch, tmp_path)

    assert projected["description"] == "namespaced"
    assert _body(response) == projected["managedToolPolicy"]


@pytest.mark.asyncio
async def test_a_declared_name_outranks_a_misnamed_direct_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The policy handed to a session's MCP servers is the policy of the spec
    the KAS projection started that session from. A ``<agent_name>.json`` that
    declares some other agent must not hand this session that agent's policy
    while the spec declaring ``agent_name`` sits beside it."""
    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"name": "other", "managedToolPolicy": {"exclude": ["misnamed"]}}),
        encoding="utf-8",
    )
    (tmp_path / f"SomePackage-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": {"exclude": ["namespaced"]}}),
        encoding="utf-8",
    )
    response = await _call(monkeypatch, tmp_path)
    assert _body(response) == {"exclude": ["namespaced"]}


@pytest.mark.asyncio
async def test_a_direct_file_is_the_fallback_when_nothing_declares_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec that declares no ``name`` -- the shape every pre-existing test in
    this file writes -- still resolves by filename, so nothing that resolved
    before this read consulted declared names stops resolving."""
    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"managedToolPolicy": {"exclude": ["direct"]}}), encoding="utf-8"
    )
    (tmp_path / "SomePackage-other.json").write_text(
        json.dumps({"name": "other", "managedToolPolicy": {"exclude": ["unrelated"]}}),
        encoding="utf-8",
    )
    response = await _call(monkeypatch, tmp_path)
    assert _body(response) == {"exclude": ["direct"]}


@pytest.mark.asyncio
async def test_two_specs_declaring_the_agent_name_are_denied_not_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which of two same-name specs is live is undefined, so the policy is too.

    The caller still fails open on ``{}`` (its own documented deviation), but
    the record is a SEL ``denied`` naming both files, not the silence a missing
    file gets -- and never an ``ok``.
    """
    (tmp_path / f"Alpha-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": {"exclude": ["a"]}}), encoding="utf-8"
    )
    (tmp_path / f"Beta-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": {"exclude": ["b"]}}), encoding="utf-8"
    )

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert response.status == 200
    assert _body(response) == {}
    kwargs = sel.log_api_access.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert f"Alpha-{AGENT}.json" in kwargs["error"]
    assert f"Beta-{AGENT}.json" in kwargs["error"]


@pytest.mark.asyncio
async def test_the_declared_name_scan_also_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback scan reads every spec in the directory, so it is the more
    expensive path; it must cross to the worker with the rest of the read."""
    (tmp_path / f"SomePackage-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": {"exclude": ["shell"]}}),
        encoding="utf-8",
    )
    scan_threads: list[int] = []
    real_scan = sessions_mod.spec_by_declared_name

    def recording_scan(*args, **kwargs):  # type: ignore[no-untyped-def]
        scan_threads.append(threading.get_ident())
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(sessions_mod, "spec_by_declared_name", recording_scan)

    loop_thread = threading.get_ident()
    response = await _call(monkeypatch, tmp_path)

    assert _body(response) == {"exclude": ["shell"]}
    assert scan_threads and loop_thread not in scan_threads


@pytest.mark.asyncio
async def test_a_traversing_agent_name_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path-traversal guard runs before any filesystem work, as before."""
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    response = await sessions_mod.api_session_tool_policy(_request(_state("../../etc/passwd")))
    assert response.status == 400
    assert _body(response)["error"] == "invalid agent name"


@pytest.mark.asyncio
async def test_a_missing_session_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deny-by-default on identity is unchanged."""
    monkeypatch.setattr(sessions_mod, "_sel", lambda: MagicMock())
    request = _request(_state())
    request.headers = {}
    response = await sessions_mod.api_session_tool_policy(request)
    assert response.status == 400

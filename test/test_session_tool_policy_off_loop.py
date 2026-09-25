"""``api_session_tool_policy`` must not read the agent config on the gateway loop.

A managed MCP server (kirocrew-core, kirocrew-cron) calls this endpoint to filter
its tool list per agent, so it runs on ordinary request traffic. The handler
resolved the agent's config file inline::

    if not agent_path.is_file():        # stat
    config = json.loads(agent_path.read_text(...))   # read + parse

all three on the single event loop every other gateway request shares.

The proof below is thread identity at the real filesystem seam -- the pinned
``_read_spec_bytes`` open the strict spec reader reads the file through --
not an assertion that ``asyncio.to_thread`` was called. A spy on the offload
would keep passing if the call were later moved back inline behind some other
wrapper; the thread the read actually runs on cannot be faked.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from conftest import requires_symlinks
from kiro_crew import agent_discovery
from kiro_crew import hooks as hooks_mod
from kiro_crew import security
from kiro_crew.dashboard.handlers import sessions as sessions_mod
from kiro_crew.security.paths import PathResolutionStalled

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
    real_read = agent_discovery._read_spec_bytes

    def recording_read(real: Path) -> bytes:
        read_threads.append(threading.get_ident())
        return real_read(real)

    monkeypatch.setattr(agent_discovery, "_read_spec_bytes", recording_read)

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
async def test_an_unparseable_agent_config_is_unreadable_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed JSON answers 409, not the ``{}`` a policy-free agent gets.

    The endpoint is deny-by-default about IDENTITY (an unresolved session is a
    400/404) and now also about a policy it cannot READ: the file may declare an
    exclusion, so answering the empty body would let the caller run a tool that
    spec forbids. An unreadable file still does not take the MCP server's tool
    LISTING down -- the MCP side keeps listing every tool on an unresolved
    policy and refuses only the call.
    """
    (tmp_path / f"{AGENT}.json").write_text("{ not json", encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[1, 2, 3]", "42", "null", "true", '"a string"'])
async def test_a_valid_json_non_object_agent_config_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """A spec that is valid JSON but not an object parses fine, so the
    JSONDecodeError guard never fires -- but ``.get`` on the parsed value would
    raise AttributeError out of the handler. It is a malformed spec: it takes the
    same 409 as the unparseable case, and is not logged as a success (only a
    config that was read AND understood earns the SEL ``ok`` record).
    """
    (tmp_path / f"{AGENT}.json").write_text(content, encoding="utf-8")

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"
    outcomes = [c.kwargs.get("outcome") for c in sel.log_api_access.call_args_list]
    assert "ok" not in outcomes, "a config that was never understood must not report ok"


@pytest.mark.asyncio
async def test_a_non_dict_policy_is_unreadable_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy of the wrong shape is one this cannot read, not an absent one.

    The operator wrote something under ``managedToolPolicy`` and its meaning is
    unknown, which is the case that must not share an answer with an agent that
    wrote nothing there.
    """
    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"managedToolPolicy": ["exclude"]}), encoding="utf-8"
    )
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


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
async def test_a_malformed_namespaced_spec_is_unreadable_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared-name SCAN must not report a broken file as "no such agent".

    A package-installed agent is namespaced on disk as ``<package>-<name>.json``
    with its declared ``name`` left bare, so it has no ``<agent>.json`` filename
    candidate and the scan is the only thing that could find its policy. That
    scan resolves a name by PARSING each file and its reader folds a refusal into
    "not a usable spec", which is indistinguishable from "a spec for someone
    else" -- so an unparseable file leaves the scan reporting no match while the
    policy it was looking for may be inside that very file.

    Answering the empty policy there would run a tool this file may forbid.
    """
    (tmp_path / f"somepkg-{AGENT}.json").write_text("{ not json", encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
async def test_readable_specs_for_other_agents_stay_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: an agent with no spec of its own is still an empty policy.

    Only an UNREADABLE spec makes the answer unknown. A directory full of
    perfectly readable specs that simply belong to other agents leaves this one
    with a genuinely absent policy, and refusing there would refuse every agent
    that has no spec file -- the case that must keep working.
    """
    (tmp_path / "somepkg-other.json").write_text(
        json.dumps({"name": "other", "managedToolPolicy": {"exclude": ["x"]}}), encoding="utf-8"
    )
    (tmp_path / "another.json").write_text(json.dumps({"name": "another"}), encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


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

    Answered as 409 ``policy_unreadable`` rather than an empty policy: ``{}``
    would be byte-identical to an agent that legitimately declares no
    exclusions, and the caller would run a tool that one of these two files may
    forbid. The record is a SEL ``denied`` naming both files, never an ``ok``.
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

    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"
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


@pytest.mark.asyncio
async def test_a_plain_markdown_file_does_not_deny_every_other_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-spec ``.md`` in the agents dir must not read as an unreadable spec.

    A markdown file with no frontmatter fence and no ``<stem>.json`` twin -- a
    README, a shared prompt fragment -- is not a spec by this repo's own rule
    (``split_markdown_spec``'s docstring, and the skip both
    ``connections/ownership.py`` and ``agent_discovery.agent_spec_stems``
    already apply). It declares nothing and hides nothing, so it cannot be the
    file that holds this agent's policy. Before the fix, the unreadable-spec
    guard parsed it as a spec, failed, and answered 409 ``policy_unreadable``
    for EVERY agent without a spec of its own -- turning one stray file into a
    permanent denial of every managed tool call on the gateway.
    """
    (tmp_path / "notes.md").write_text(
        "# Shared prompt fragment\n\nJust prose, no frontmatter.\n", encoding="utf-8"
    )
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        # Opens with a fence, frontmatter is not valid YAML.
        "---\nname: [unclosed\n---\n\nbody\n",
        # Opens with a fence that never closes: the boundary case
        # split_markdown_spec folds into "not a spec", which this guard must
        # NOT skip -- a truncated real spec looks exactly like this.
        "---\nname: reviewer\n",
    ],
    ids=["bad-yaml", "unclosed-fence"],
)
async def test_a_fenced_markdown_file_that_fails_to_parse_still_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Only a document with NO opening fence is "not a spec".

    A document that opens with ``---`` announced itself as a spec: its declared
    name cannot be recovered without a successful parse, so the operator's
    exclusion list may be inside it and the honest answer stays "unknown"
    (409), exactly as before the skip was added. Widening the skip to any
    ``.md`` that fails to read would silently convert a genuinely broken spec
    into a missing one.
    """
    (tmp_path / "broken.md").write_text(content, encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
async def test_the_fence_probe_is_bounded_and_an_oversized_plain_file_still_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must not re-read to EOF a file the strict reader refused at cap.

    ``read_agent_spec_strict`` refuses an over-cap file after reading at most
    ``cap + 1`` bytes -- deliberately never the whole file. The fence probe
    runs exactly when that refusal fired, on every policy request, so an
    unbounded re-read there hands the gateway an attacker-sized allocation
    (and a ``MemoryError`` escapes the probe's fail-closed arm as a 500).
    Pin both halves: the oversized plain file is still skipped (200, not
    409), and the probe declared an explicit small byte bound at the one seam
    that reads the descriptor (``_read_head``, ``os.read`` straight off the
    fd -- no buffered reader pulling its own block behind the bound).
    """
    monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 128)
    (tmp_path / "big.md").write_text("# prose\n" + "x" * 4096, encoding="utf-8")

    probe_limits: list[int] = []
    real_read_head = sessions_mod._read_head

    def recording_read_head(fd: int, limit: int) -> tuple[bytes, bool]:
        probe_limits.append(limit)
        head, truncated = real_read_head(fd, limit)
        assert len(head) <= limit
        return head, truncated

    monkeypatch.setattr(sessions_mod, "_read_head", recording_read_head)

    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}
    assert probe_limits, "the fence probe never ran"
    assert all(limit <= 64 for limit in probe_limits), (
        "the fence probe read without a small explicit byte bound: an "
        "over-cap file would be slurped into memory on every policy request"
    )


@pytest.mark.asyncio
async def test_an_oversized_fenced_markdown_file_still_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An over-cap document that OPENS with a fence announced itself as a spec.

    Its declared name is unrecoverable (the strict reader refuses at the cap
    before parsing), so the policy stays unknown: 409, the same answer as any
    other fenced document that cannot be parsed. The bounded probe reads the
    first bytes, sees the fence, and keeps the refusal.
    """
    monkeypatch.setattr(hooks_mod, "MAX_FILE_BYTES", 128)
    (tmp_path / "big-spec.md").write_text("---\nname: reviewer\n" + "#" * 4096, encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
async def test_a_plain_markdown_file_at_the_agents_own_filename_is_not_its_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The not-a-spec rule also holds for ``<agent>.md`` in the direct-filename slot.

    The declared-name scan finds nothing, so the read falls back to the direct
    filename candidates and finds ``<agent>.md`` with no JSON twin. That file
    has no opening fence: it is a prose document that happens to share the
    agent's name, and it can no more hold this agent's policy than a
    ``notes.md`` can. Before the fix this arm still parsed it as a spec, failed,
    and answered 409 for exactly this agent -- the same one-file-denies-a-tool
    defect the enumeration skip closed, one slot over. It is now the same
    answer as no file at all: the directory-wide unreadable-spec check, then an
    empty policy.
    """
    (tmp_path / f"{AGENT}.md").write_text(
        "# Reviewer notes\n\nJust prose, no frontmatter.\n", encoding="utf-8"
    )
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "---\nname: [unclosed\n---\n\nbody\n",
        "---\nname: reviewer\n",
    ],
    ids=["bad-yaml", "unclosed-fence"],
)
async def test_a_fenced_markdown_file_at_the_agents_own_filename_still_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """The boundary in the direct-filename slot is the same as in the scan.

    ``<agent>.md`` that OPENS with a fence announced itself as this agent's
    spec; when it does not parse, whatever exclusions it declares are unknown
    and the answer stays 409 -- before and after the plain-document exception
    was extended to this arm. Only the fence-less document is skipped. The
    refusal is the direct arm's own, attributed to THIS agent's spec: the
    directory-wide guard would also refuse, but it can only say that some spec
    in the directory is unreadable, and the operator reading the denial needs
    the file named.
    """
    (tmp_path / f"{AGENT}.md").write_text(content, encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"
    assert f"agent spec for {AGENT!r} could not be read" in _body(response)["reason"]


# A spec with a REAL exclusion list. Saved as UTF-8 it resolves by declared
# name and answers ``{"exclude": ["shell"]}``; saved in another encoding the
# strict parser cannot decode it, and the honest answer is 409, never ``{}``.
_FENCED_SPEC_WITH_EXCLUSION = (
    f"---\nname: {AGENT}\nmanagedToolPolicy:\n  exclude: [shell]\n---\n\nbody\n"
)


def _encode_foreign(text: str, encoding: str, *, with_bom: bool) -> bytes:
    """*text* in *encoding*, optionally opened by its byte-order mark."""
    prefix = "\ufeff" if with_bom else ""
    return (prefix + text).encode(encoding)


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
@pytest.mark.parametrize("with_bom", [True, False], ids=["bom", "bomless"])
@pytest.mark.parametrize("filename", ["ops.md", f"{AGENT}.md"], ids=["stray", "direct"])
async def test_a_fenced_spec_in_a_foreign_encoding_still_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    with_bom: bool,
    filename: str,
) -> None:
    """A UTF-16/UTF-32 spec whose decoded text opens with a fence is not prose.

    The strict parser reads every spec as UTF-8, so a spec an editor saved as
    UTF-16 fails to decode when it has a byte-order mark. Without a mark, its
    NUL-interleaved head is valid UTF-8 but is still not UTF-8 prose. A probe
    that treats either byte shape as fence-less silently ignores the
    operator's real exclusion list, returning ``200 {}`` where the file says
    ``exclude: [shell]``. Only a UTF-8, NUL-free head showing no fence is
    proven prose; every foreign-encoded head keeps the refusal, in the scan
    (a stray file) and in the direct-filename slot alike.
    """
    (tmp_path / filename).write_bytes(
        _encode_foreign(_FENCED_SPEC_WITH_EXCLUSION, encoding, with_bom=with_bom)
    )
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        # Latin-1 prose: no fence, but 0xE9 is not UTF-8, so the parser never
        # reached its fence test and the probe cannot say what it would have seen.
        "# R\u00e9sum\u00e9\n\nJust prose.\n".encode("latin-1"),
        # A file that ENDS inside a multibyte sequence, shorter than the probe
        # bound: the parser's own decode failure, not a cut the probe made.
        b"# prose \xe2\x82",
    ],
    ids=["latin-1-head", "file-ends-mid-sequence"],
)
async def test_a_fence_less_head_that_is_not_utf8_is_not_proven_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    """Fence-less bytes the parser cannot decode are unknown, not ignorable.

    The skip is for a document PROVEN not to be a spec. Proof is the parser's
    own test on the parser's own decoding -- UTF-8 -- and a head that is not
    UTF-8 has no such decoding, so the guard keeps raising (409) rather than
    guess from bytes what text an operator meant.
    """
    (tmp_path / "notes.md").write_bytes(raw)
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


@pytest.mark.asyncio
async def test_a_multibyte_character_cut_at_the_probe_bound_is_not_misread_as_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 64-byte cut may land inside a UTF-8 character; that is not a decode failure.

    ``#`` and a space, sixty ``x``, then ``EUR SIGN`` (``E2 82 AC``): bytes 62
    and 63 of the head are the first two bytes of that character and byte 64
    is past the bound. A strict decode of the head alone would raise
    ``unexpected end of data`` and turn a perfectly ordinary prose file into
    a 409. The file is valid UTF-8 and fence-less: skipped.
    """
    text = "# " + "x" * 60 + "\u20ac and more prose\n"
    assert text.encode("utf-8")[62:65] == b"\xe2\x82\xac"
    (tmp_path / "notes.md").write_text(text, encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@pytest.mark.asyncio
async def test_the_fence_probe_survives_a_saturated_resolver_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must not hand the path back to the bounded ``mc-pathres`` gate.

    ``read_agent_spec_strict`` was moved off that pool on purpose: it
    canonicalises with ``Path.resolve`` and asks
    ``is_sensitive_canonical_path``, which submits nothing off the event loop,
    so a saturated pool cannot drop a healthy spec. A probe that read through
    ``hooks.validate_file_path`` re-submits the same path to that pool and, when
    it misses its budget, fails closed -- and a fail-closed probe is "not
    proven prose", which is 409: the stray ``.md`` denies every agent again
    exactly while the gateway is busy. Every submission refused here, the
    hooks gate and the bounded gate both forbidden; the plain file is still
    skipped.
    """

    def stalled(expanded: str, worker: Any, **kwargs: Any) -> Any:
        raise PathResolutionStalled(expanded, os.sep)

    monkeypatch.setattr(security.paths, "_run_resolution_bounded", stalled)
    # Cold anchor cache: the targets must be rebuilt inline, not via the pool.
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})
    monkeypatch.setattr(
        hooks_mod,
        "validate_file_path",
        lambda *a, **k: pytest.fail("the fence probe read through hooks.validate_file_path"),
    )
    monkeypatch.setattr(
        security.paths,
        "is_sensitive_path",
        lambda *a, **k: pytest.fail("the fence probe asked the bounded gate off the loop"),
    )
    (tmp_path / "notes.md").write_text("# prose\n\nno fence\n", encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 200
    assert _body(response) == {}


@requires_symlinks
@pytest.mark.asyncio
async def test_the_fence_probe_judges_the_canonical_target_with_the_spec_readers_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link in the agents dir is followed and its TARGET is what the fence judges.

    Same contract as the strict reader (``test_agent_discovery_pathres``): the
    gate receives the ``Path.resolve(strict=True)`` result, never the link's
    spelling. First half: a link to a benign fence-less document is probed by
    its canonical path and skipped. Second half: when that canonical target is
    fenced, the probe fails closed -- 409, the same answer the strict reader
    gave for the file -- instead of reading through the link to classify it.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "prose.md"
    target.write_text("# prose\n\nno fence\n", encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    link = agents / "notes.md"
    link.symlink_to(target)
    canonical = str(target.resolve(strict=True))

    seen: list[str] = []
    real_gate = sessions_mod.is_sensitive_canonical_path

    def recording(resolved: str) -> bool:
        seen.append(resolved)
        return real_gate(resolved)

    monkeypatch.setattr(sessions_mod, "is_sensitive_canonical_path", recording)
    response = await _call(monkeypatch, agents)
    assert response.status == 200
    assert _body(response) == {}
    assert canonical in seen, "the probe never asked the fence about the resolved target"
    assert str(link) not in seen, "the probe handed the fence the link's spelling"

    def fenced(resolved: str) -> bool:
        return str(resolved) == canonical

    monkeypatch.setattr(agent_discovery, "is_sensitive_canonical_path", fenced)
    monkeypatch.setattr(agent_discovery, "_sel", lambda: MagicMock())
    monkeypatch.setattr(sessions_mod, "is_sensitive_canonical_path", fenced)
    response = await _call(monkeypatch, agents)
    assert response.status == 409
    assert _body(response)["code"] == "policy_unreadable"


# ---------------------------------------------------------------------------
# The refusal names the file. A ``policy_unreadable`` reason that carries only
# an exception class name -- ``(JSONDecodeError)`` -- leaves an operator told
# to "fix or remove the unreadable spec" with no way to find it short of
# validating every file in the directory by hand. The gate's VERDICT is not
# what these tests pin: every case below is a 409 either way, and the
# readable-directory controls above still answer 200.
# ---------------------------------------------------------------------------


def _reason_names_file_and_kind(reason: str, filename: str, kind: str, agents_dir: Path) -> None:
    """The shared shape every unreadable-spec refusal must take.

    The filename is quoted (``repr``: it is untrusted input from a
    user-writable directory and the text reaches a terminal), the failure kind
    is spelled out in words rather than an exception class, the tail says what
    to do, and NO absolute path leaks: the reason reaches the MCP client's
    error text, so the directory's location stays out of it.
    """
    assert repr(filename) in reason, reason
    assert kind in reason, reason
    assert "no restart needed" in reason, reason
    assert str(agents_dir) not in reason, "the refusal leaked the agents directory's path"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "kind"),
    [
        ("broken.json", "{ not json", "not valid JSON"),
        ("notes.md", "---\nname: reviewer\n", "frontmatter the spec parser refuses"),
        # A CLOSED fence holding valid YAML that is not a mapping: the kind must
        # not blame the fence or the YAML, since neither is at fault.
        ("list.md", "---\n- a\n- b\n---\n\nbody\n", "frontmatter the spec parser refuses"),
        ("._sidecar.json", '{"name": "x"}', "AppleDouble sidecar"),
    ],
    ids=["bad-json", "unclosed-fence", "closed-fence-not-a-mapping", "appledouble"],
)
async def test_the_directory_guard_names_the_unreadable_file_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, content: str, kind: str
) -> None:
    """One stray file denies every spec-less agent; the denial must say which.

    Setup mirrors the field report: a perfectly good spec for someone else
    (so the directory is not empty) plus one file the strict reader refuses.
    The verdict stays 409 -- that is the gate's design -- but ``reason`` now
    carries the filename and a plain-words failure kind, and the SEL row
    carries the same text so the audit trail is actionable too.
    """
    (tmp_path / "somepkg-other.json").write_text(
        json.dumps({"name": "other", "managedToolPolicy": {"exclude": ["x"]}}), encoding="utf-8"
    )
    (tmp_path / filename).write_text(content, encoding="utf-8")

    sel = MagicMock()
    monkeypatch.setattr(sessions_mod, "kiro_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(sessions_mod, "_sel", lambda: sel)
    response = await sessions_mod.api_session_tool_policy(_request(_state()))

    assert response.status == 409
    body = _body(response)
    assert body["code"] == "policy_unreadable"
    _reason_names_file_and_kind(body["reason"], filename, kind, tmp_path)
    assert f"the policy for {AGENT!r} is unknown" in body["reason"]
    kwargs = sel.log_api_access.call_args.kwargs
    assert kwargs["outcome"] == "denied"
    assert repr(filename) in kwargs["error"], "the SEL row must name the file too"


@pytest.mark.asyncio
async def test_the_direct_filename_refusal_names_the_file_and_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``<agent>.json`` arm keeps its historical prefix and gains the file.

    ``agent spec for 'reviewer' could not be read`` is what an earlier test
    pins for this arm; the filename and kind are appended, not substituted, so
    a reader matching on the old prefix still matches.
    """
    (tmp_path / f"{AGENT}.json").write_text("{ not json", encoding="utf-8")
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    reason = _body(response)["reason"]
    assert f"agent spec for {AGENT!r} could not be read" in reason
    _reason_names_file_and_kind(reason, f"{AGENT}.json", "not valid JSON", tmp_path)


@pytest.mark.asyncio
async def test_the_wrong_shape_refusals_name_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec that parses but is the wrong shape is named like one that does not.

    Same disposition, same actionable shape: valid JSON that is not an object,
    and a ``managedToolPolicy`` that is not an object, both name the direct
    file the arm read.
    """
    (tmp_path / f"{AGENT}.json").write_text("[1, 2]", encoding="utf-8")
    reason = _body(await _call(monkeypatch, tmp_path))["reason"]
    _reason_names_file_and_kind(reason, f"{AGENT}.json", "not an object", tmp_path)

    (tmp_path / f"{AGENT}.json").write_text(
        json.dumps({"managedToolPolicy": "shell"}), encoding="utf-8"
    )
    reason = _body(await _call(monkeypatch, tmp_path))["reason"]
    _reason_names_file_and_kind(reason, f"{AGENT}.json", "not an object", tmp_path)


@pytest.mark.asyncio
async def test_a_wrong_shape_policy_found_by_declared_name_names_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan returns a parse, not a path, so the refusal names the spec by
    its declared name -- which identifies it, since exactly one declares it --
    and still ends with the remedy. Same 409 as before; only the text grew."""
    (tmp_path / f"SomePackage-{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "managedToolPolicy": "shell"}), encoding="utf-8"
    )
    response = await _call(monkeypatch, tmp_path)
    assert response.status == 409
    reason = _body(response)["reason"]
    assert f"managedToolPolicy for {AGENT!r} is str, not an object" in reason
    assert f"declaring name {AGENT!r}" in reason
    assert "no restart needed" in reason
    assert str(tmp_path) not in reason


@pytest.mark.asyncio
async def test_the_gateway_log_names_the_unreadable_file_once_per_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway log carries the full path, at WARNING, and does not flood.

    The client re-asks for its policy on every ``tools/call`` (a 409 is never
    negative-cached), so a warning per refusal would repeat once per tool call
    for as long as the file stays broken. One line per (file, mtime) is
    enough for an operator reading the log: a fix or a re-break changes the
    mtime and is logged again. The full path is fine HERE -- the gateway log
    is local -- where the wire reason above carries only the name.
    """
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(sessions_mod, "_UNREADABLE_SPEC_WARNED", set())

    with caplog.at_level("WARNING", logger=sessions_mod.logger.name):
        assert (await _call(monkeypatch, tmp_path)).status == 409
        assert (await _call(monkeypatch, tmp_path)).status == 409

    warnings = [r for r in caplog.records if "broken.json" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert repr(str(broken)) in warnings[0].getMessage()
    assert "not valid JSON" in warnings[0].getMessage()
    # The dedupe cache is bounded in bytes as well as in entries: what it
    # retains per path is a fixed-size digest, never the path text itself.
    (key,) = sessions_mod._UNREADABLE_SPEC_WARNED
    digest, revision = key
    assert isinstance(digest, bytes) and len(digest) == 32
    assert isinstance(revision, int)
    assert str(broken).encode() not in digest


@pytest.mark.asyncio
async def test_the_duplicate_name_reason_names_the_files_without_their_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two specs declaring one name: the wire reason takes the same shape.

    The exception's message quotes full paths for a terminal; the 409
    ``reason`` reaches the MCP client's model-visible refusal, so it names the
    two files by name only and ends with the remedy. The SEL row keeps the
    exception's own message -- the audit trail is local -- and the verdict is
    the 409 it always was.
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

    assert response.status == 409
    body = _body(response)
    assert body["code"] == "policy_unreadable"
    _reason_names_file_and_kind(body["reason"], f"Alpha-{AGENT}.json", "declare the name", tmp_path)
    assert repr(f"Beta-{AGENT}.json") in body["reason"]
    # The SEL row keeps the exception's message, which quotes each FULL path
    # ``repr``'d -- compared as such, since ``repr`` escapes Windows separators.
    assert repr(str(tmp_path / f"Alpha-{AGENT}.json")) in (
        sel.log_api_access.call_args.kwargs["error"]
    )


def test_an_ambiguity_raised_without_paths_falls_back_to_its_message() -> None:
    """A raiser that supplies no ``paths`` -- the exception's other raiser --
    leaves the message as the only record, and the reason repeats it."""
    exc = sessions_mod.AmbiguousAgentSpecError("2 specs declare the name 'x': ...")
    assert sessions_mod._ambiguous_spec_reason("x", exc) == str(exc)


@requires_symlinks
@pytest.mark.asyncio
async def test_a_spec_the_pinned_open_refuses_names_that_as_the_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strict reader maps the pinned open's refusal to a generic ``EACCES``
    whose ``strerror`` is its own placeholder text; naming THAT would only
    restate "could not be read". The cause is what the kind reports.

    A hardlinked inode is one of the shapes the pinned open refuses, and it
    can be built on any filesystem that has hard links at all.
    """
    original = tmp_path / "elsewhere.json"
    original.write_text(json.dumps({"name": "other"}), encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    try:
        os.link(original, agents / "linked.json")
    except OSError as exc:  # pragma: no cover - filesystem without hard links
        pytest.skip(f"hard links unavailable here: {exc}")

    response = await _call(monkeypatch, agents)
    assert response.status == 409
    reason = _body(response)["reason"]
    _reason_names_file_and_kind(reason, "linked.json", "not a plain readable file", tmp_path)
    assert "could not be opened (agent spec could not be read)" not in reason


@pytest.mark.asyncio
async def test_the_gateway_log_line_redacts_a_credential_shaped_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The logged path goes through the log-egress redaction like every other
    operational line that carries text this process did not author.

    The agents directory is user-writable and the filename is what gets logged,
    so a file named after a token must not persist that token in gateway.log.
    The wire ``reason`` is unaffected (it names the file for the operator); the
    log line is the one place the full path is written, and it is scrubbed.
    """
    token = "ghp_" + "C" * 36
    broken = tmp_path / f"{token}.json"
    broken.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(sessions_mod, "_UNREADABLE_SPEC_WARNED", set())

    with caplog.at_level("WARNING", logger=sessions_mod.logger.name):
        assert (await _call(monkeypatch, tmp_path)).status == 409

    warnings = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(warnings) == 1
    assert token not in warnings[0].getMessage(), "the token reached the gateway log"
    assert "[REDACTED" in warnings[0].getMessage() or "REDACTED" in warnings[0].getMessage()


def test_every_failure_kind_reads_as_a_predicate(tmp_path: Path) -> None:
    """Both templates splice the kind after "is" / "could not be read (", so a
    kind that is a verb phrase renders as "'x.json' is could not be opened".
    Every arm answers a predicate phrase; the OSError and fenced-path arms are
    the two that read wrong when written as verbs."""
    p = tmp_path / "reviewer.json"
    denied = PermissionError(13, "Permission denied", str(p))
    kind = sessions_mod._spec_failure_kind(p, denied)
    assert kind == "unreadable (Permission denied)"
    fenced = agent_discovery.SensitiveAgentSpecPathError("x")
    assert sessions_mod._spec_failure_kind(p, fenced) == "a path the spec reader refuses"
    for k in (kind, sessions_mod._spec_failure_kind(p, fenced)):
        assert not k.startswith(("could ", "resolves ", "is ")), k

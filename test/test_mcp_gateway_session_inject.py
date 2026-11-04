"""Tests for shared-MCP-gateway delivery via ACP ``session/new`` injection.

kiro-cli honours a server injected in ``session/new`` AHEAD of the same-named spec
entry, so broker stubs pool servers without writing a spec anywhere.
``test_real_kiro_cli_prefers_session_injected_server`` pins that undocumented
precedence against the real binary, opt-in only (see ``REAL_CLI``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER
from kiro_crew.mcp_gateway.session_servers import (
    _acp_env,
    _acp_server_entry,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.testing.wait import wait_until


def _stub(**over):
    entry = {
        _WRAPPER_MARKER: True,
        "command": "/data/mcp-gateway/stubs/mc-mcp-stub-wrapper.sh",
        "args": ["--target-command=fetch", "--socket", "/data/gateway.sock"],
        "env": {},
        "autoApprove": ["fetch___fetch"],
    }
    entry.update(over)
    return entry


def _write_overlay(tmp_path: Path, agent: str, servers: dict) -> Path:
    overlay = tmp_path / "agents"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / f"{agent}.json").write_text(
        json.dumps({"name": agent, "mcpServers": servers}), encoding="utf-8"
    )
    return overlay


# ── selection: only stubs are injected ──────────────────────────────────────


def test_injects_only_wrapped_stub_entries(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "pooled": _stub(),
            "unpooled": {"command": "npx", "args": ["-y", "srv"], "env": {"TOKEN": "s3cr3t"}},
        },
    )
    out = pooled_session_servers(overlay, "kirocrew")
    assert [e["name"] for e in out] == ["pooled"]


def test_unpooled_server_env_is_never_transmitted(tmp_path):
    """A non-poolable server's credentials must stay in the spec file."""
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "secretive": {"command": "npx", "env": {"API_KEY": "s3cr3t"}},
        },
    )
    assert pooled_session_servers(overlay, "kirocrew") == []
    assert "s3cr3t" not in json.dumps(pooled_session_servers(overlay, "kirocrew"))


@pytest.mark.parametrize("vendor", ["creds-agent", "local-creds-agent-mcp"])
def test_a_stale_overlay_never_injects_a_credential_vendor(tmp_path, vendor):
    """An overlay written before the vendors joined ``UNPOOLABLE_SERVERS`` (or kept
    by a failed regeneration) can still hold a vendor's stub. Injecting it would
    run the vendor's shared backend outside the chat sandbox, so the reader skips
    it and the session resolves the vendor from its agent spec instead. The
    vendor is left out of ``injection_server_names`` too, or the spec's own copy
    would be withheld and the session would get no vendor at all."""
    overlay = _write_overlay(tmp_path, "kirocrew", {vendor: _stub(), "pooled": _stub()})
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["pooled"]
    assert injection_server_names(overlay, "kirocrew") == frozenset({"pooled"})


def test_stub_name_is_preserved_so_it_shadows_the_spec_entry(tmp_path):
    """The injected name must equal the original server name — that identity is
    what suppresses the agent spec's own copy (and keeps tool ids stable)."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"builder-mcp": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["name"] == "builder-mcp"


def test_entries_are_name_sorted_for_deterministic_params(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "zeta": _stub(),
            "alpha": _stub(),
            "mid": _stub(),
        },
    )
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == [
        "alpha",
        "mid",
        "zeta",
    ]


# ── shaping into the ACP element form ───────────────────────────────────────


def test_marker_is_stripped_from_the_injected_element(tmp_path):
    """kiro-cli tolerates unknown keys today; a future strict parser would not."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert _WRAPPER_MARKER not in entry


def test_operator_passthrough_keys_survive(tmp_path):
    """Dropping autoApprove would re-prompt for already-approved tools."""
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "pooled": _stub(timeout=9000, type="stdio", disabledTools=["x"]),
        },
    )
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["autoApprove"] == ["fetch___fetch"]
    assert entry["timeout"] == 9000
    assert entry["type"] == "stdio"
    assert entry["disabledTools"] == ["x"]


def test_env_is_emitted_in_acp_array_form():
    assert _acp_env({}) == []
    assert _acp_env({"A": "1"}) == [{"name": "A", "value": "1"}]
    assert _acp_env({"N": 5}) == [{"name": "N", "value": "5"}]
    assert _acp_env(None) == []
    assert _acp_env("nonsense") == []


def test_stub_entries_carry_no_env(tmp_path):
    """gatewayd spawns the pooled backend, so kiro-cli needs no env at all."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["env"] == []


def test_non_string_args_are_coerced(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub(args=["ok", 7])})
    (entry,) = pooled_session_servers(overlay, "kirocrew")
    assert entry["args"] == ["ok", "7"]


def test_entry_without_command_is_skipped():
    """Injecting a commandless stub would shadow a working server with a broken
    one; leaving it out keeps the spec's own entry live."""
    assert _acp_server_entry("x", {_WRAPPER_MARKER: True, "command": ""}) is None
    assert _acp_server_entry("x", {_WRAPPER_MARKER: True}) is None


def test_commandless_stub_does_not_suppress_others(tmp_path):
    overlay = _write_overlay(
        tmp_path,
        "kirocrew",
        {
            "broken": _stub(command=""),
            "fine": _stub(),
        },
    )
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["fine"]


# ── the off switch and fail-soft behaviour ──────────────────────────────────


def test_disabled_gateway_injects_nothing():
    assert pooled_session_servers(None, "kirocrew") == []


def test_missing_agent_name_injects_nothing(tmp_path):
    assert pooled_session_servers(tmp_path, None) == []


def test_absent_overlay_spec_is_not_an_error(tmp_path):
    assert pooled_session_servers(tmp_path / "nope", "kirocrew") == []


def test_corrupt_overlay_degrades_to_unpooled(tmp_path):
    overlay = tmp_path / "agents"
    overlay.mkdir()
    (overlay / "kirocrew.json").write_text("{not json", encoding="utf-8")
    assert pooled_session_servers(overlay, "kirocrew") == []


@pytest.mark.parametrize(
    "body", ["[]", '"str"', "null", '{"mcpServers": []}', '{"mcpServers": "x"}', "{}"]
)
def test_malformed_spec_shapes_degrade_to_unpooled(tmp_path, body):
    overlay = tmp_path / "agents"
    overlay.mkdir(exist_ok=True)
    (overlay / "kirocrew.json").write_text(body, encoding="utf-8")
    assert pooled_session_servers(overlay, "kirocrew") == []


def test_non_dict_server_entry_is_skipped(tmp_path):
    overlay = _write_overlay(tmp_path, "kirocrew", {"bad": "x", "good": _stub()})
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["good"]


# ── project scope: the overlay is user-level, the session need not be ────────


def _write_project_agent(root: Path, name: str, filename: str | None = None) -> Path:
    """A checkout declaring agent *name*, in the one place kiro-cli resolves."""
    agents = root / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / (filename or f"{name}.json")
    spec.write_text(
        json.dumps({"name": name, "mcpServers": {"declared-here": {"command": "/bin/proj"}}}),
        encoding="utf-8",
    )
    return spec


def _write_project_markdown_agent(root: Path, name: str, filename: str | None = None) -> Path:
    """The same checkout declaring *name* in the MARKDOWN spec form.

    Written through ``agent_spec_format``'s own suffix rather than a literal so
    this fixture and the guard read the same constant.
    """
    from kiro_crew.agent_spec_format import MARKDOWN_SUFFIX

    agents = root / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / (filename or f"{name}{MARKDOWN_SUFFIX}")
    spec.write_text(
        f"---\nname: {name}\nmcpServers:\n  declared-here:\n    command: /bin/proj\n---\n\nbody\n",
        encoding="utf-8",
    )
    return spec


def test_a_project_agent_takes_no_stub_from_the_user_level_overlay(tmp_path):
    """The overlay is keyed by NAME, and a checkout can declare that name.

    A stub outranks the spec entry it shadows, so handing the session the
    user-level agent's stub gave it a server the project never declared AND left
    the project's own declaration of that name unlaunched. Unpooled is this
    module's standing direction for a stub it cannot vouch for.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "kirocrew")
    assert pooled_session_servers(overlay, "kirocrew", work_dir=project) == []


def test_the_withheld_set_and_the_injected_set_agree_on_a_project_agent(tmp_path):
    """Disagreement here costs the session the server altogether.

    ``injection_server_names`` is what a mirror withholds from its own
    projection. A set naming a server that is NOT injected withholds the spec's
    only copy of it, so the session receives neither -- strictly worse than the
    bug this fixes. One guard answers both, and this pins that it does.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "kirocrew")
    assert injection_server_names(overlay, "kirocrew", work_dir=project) == frozenset()
    assert pooled_session_servers(overlay, "kirocrew", work_dir=project) == []
    # With no checkout in play both still report the same stub.
    assert injection_server_names(overlay, "kirocrew") == frozenset({"pooled"})
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["pooled"]


def test_a_checkout_that_does_not_declare_the_agent_keeps_pooling(tmp_path):
    """Only a name the checkout actually declares leaves the overlay's scope."""
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "something-else")
    got = pooled_session_servers(overlay, "kirocrew", work_dir=project)
    assert [e["name"] for e in got] == ["pooled"]


def test_a_project_agents_declared_name_beats_its_filename(tmp_path):
    """The shadowing rule is ``agent_discovery``'s and is not restated here.

    kiro-cli lists a project agent under the ``name`` its spec declares, so a
    file named anything else still shadows the user-level agent of that name.
    Reading the rule from one place is what keeps this module's answer and
    ``acp.session_mcp``'s spec resolution from drifting apart.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "kirocrew", filename="zzz-unrelated.json")
    assert pooled_session_servers(overlay, "kirocrew", work_dir=project) == []


@pytest.mark.parametrize("shape", ["absent", "a-file"])
def test_an_unusable_checkout_leaves_the_overlay_in_effect(tmp_path, shape):
    """Fail-soft on the scope too: an unreadable checkout is not a failed spawn."""
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    if shape == "a-file":
        target = tmp_path / "not-a-dir"
        target.write_text("x", encoding="utf-8")
    else:
        target = tmp_path / "nope"
    got = pooled_session_servers(overlay, "kirocrew", work_dir=target)
    assert [e["name"] for e in got] == ["pooled"]


def test_a_project_only_agent_still_runs_its_servers_unbrokered(tmp_path):
    """A project agent with no user-level twin runs its servers outside the pool.

    Pinned so brokering it stays a deliberate change: ``gatewayd.env_target_resolver``
    reads targets from its launch env, and a stub never tells it one.
    """
    overlay = _write_overlay(tmp_path / "gw", "another-agent", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "project-only")
    assert pooled_session_servers(overlay, "project-only", work_dir=project) == []
    assert injection_server_names(overlay, "project-only", work_dir=project) == frozenset()


def test_a_project_markdown_spec_leaves_a_json_hosts_overlay_in_effect(tmp_path):
    """A form the host cannot dispatch is not the agent the session is running.

    kiro-cli discovers only ``*.json`` in a checkout, so counting a project ``foo.md``
    would drop the stubs of the user-level ``foo.json`` that kiro-cli still runs.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_markdown_agent(project, "kirocrew")
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew", work_dir=project)] == [
        "pooled"
    ]
    assert injection_server_names(overlay, "kirocrew", work_dir=project) == frozenset({"pooled"})


def test_a_mirrored_hosts_projection_does_suppress_on_a_project_markdown_spec(tmp_path):
    """A mirrored host HONOURS a project markdown spec, so its shadow must count.

    ``acp.session_mcp._agent_spec_for`` resolves a project ``foo.md``, so keeping the
    user-level stub would mount its command and credentials under the checkout's agent.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_markdown_agent(project, "kirocrew")
    assert pooled_session_servers(overlay, "kirocrew", work_dir=project, markdown_specs=True) == []
    assert (
        injection_server_names(overlay, "kirocrew", work_dir=project, markdown_specs=True)
        == frozenset()
    )


def test_a_malformed_project_spec_is_not_a_shadow_for_the_overlay_lookup(tmp_path):
    """A file the host cannot dispatch must not withhold the stubs of one it can.

    kiro-cli 2.22.0 loads zero workspace agents from a malformed spec and runs the
    user-level one. The second half pins the strict default the governance caller needs.
    """
    from kiro_crew.agent import _project_shadow_of

    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "kirocrew.json").write_text('{"name": "kirocrew", ', encoding="utf-8")

    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew", work_dir=project)] == [
        "pooled"
    ]
    assert injection_server_names(overlay, "kirocrew", work_dir=project) == frozenset({"pooled"})
    # The governance caller still sees a claim on the name.
    assert _project_shadow_of("kirocrew", project) is not None
    # And the dispatch-minded caller does not.
    assert _project_shadow_of("kirocrew", project, dispatchable_only=True) is None


def test_a_mirrored_host_does_suppress_on_a_malformed_project_spec(tmp_path):
    """The mirror's own resolver matches that file, so the overlay must not fill in.

    ``_agent_spec_and_snapshot_for`` returns the malformed file's failed read with no
    user-level fallback, so user-level stubs would add servers nothing declares. Hence
    ``dispatchable_only=False`` here, while kiro wants ``True`` (sibling test above).
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / "kirocrew.json").write_text('{"name": "kirocrew", ', encoding="utf-8")

    assert (
        pooled_session_servers(
            overlay, "kirocrew", work_dir=project, markdown_specs=True, dispatchable_only=False
        )
        == []
    )
    assert (
        injection_server_names(
            overlay, "kirocrew", work_dir=project, markdown_specs=True, dispatchable_only=False
        )
        == frozenset()
    )


def test_the_unbrokered_project_agent_state_is_reported_above_debug(tmp_path, caplog):
    """The shipped posture is "warn and run unbrokered", so the warn must be visible.

    At ``logger.debug`` an operator would see nothing while servers run with no pool
    or governance. Pins the level, not the wording.
    """
    import logging

    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_agent(project, "kirocrew")
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.session_servers"):
        assert pooled_session_servers(overlay, "kirocrew", work_dir=project) == []
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "the unbrokered-project-agent state was reported below WARNING"
    assert any("UNBROKERED" in r.getMessage() for r in records), [r.getMessage() for r in records]

    # A session with no project agent in play stays quiet: this must not become a
    # warning every ordinary session emits.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.session_servers"):
        assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["pooled"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_differing_stem_markdown_file_does_not_hide_a_json_project_agent(tmp_path):
    """The form filter belongs INSIDE the scan, not on its result.

    The scan returns the first match by stem, so ``a.md`` comes before ``z.json``;
    filtering afterwards would answer "no shadow" with ``z.json`` unexamined.
    Both hosts suppress, so the assertion names the resolved FILE.
    """
    from kiro_crew.mcp_gateway.session_servers import _project_spec_for_agent

    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_markdown_agent(project, "kirocrew", filename="a.md")
    _write_project_agent(project, "kirocrew", filename="z.json")

    # A JSON-dispatching host resolves the JSON file, NOT "no project spec".
    assert _project_spec_for_agent("kirocrew", project, False).name == "z.json"
    # A host honouring both forms keeps the scan's own first match.
    assert _project_spec_for_agent("kirocrew", project, True).name == "a.md"
    # Either way the overlay is out of scope for this name, on both halves.
    for markdown in (False, True):
        assert (
            pooled_session_servers(overlay, "kirocrew", work_dir=project, markdown_specs=markdown)
            == []
        )
        assert (
            injection_server_names(overlay, "kirocrew", work_dir=project, markdown_specs=markdown)
            == frozenset()
        )


def test_a_project_json_twin_beside_the_markdown_still_suppresses(tmp_path):
    """The JSON form is what kiro-cli runs, and it is present here.

    The rule ignores the markdown FILE, not every checkout that has one.
    """
    overlay = _write_overlay(tmp_path / "gw", "kirocrew", {"pooled": _stub()})
    project = tmp_path / "checkout"
    _write_project_markdown_agent(project, "kirocrew")
    _write_project_agent(project, "kirocrew")
    assert pooled_session_servers(overlay, "kirocrew", work_dir=project) == []
    assert injection_server_names(overlay, "kirocrew", work_dir=project) == frozenset()


def test_every_call_site_passes_the_session_checkout():
    """A call site without ``work_dir`` resolves by name again, silently.

    The keyword's presence means the site decided its scope (``work_dir=None`` is a
    valid decision). Only an explicit ``work_dir=`` or a ``**overlay_project_scope(...)``
    splat counts; a bare ``**kwargs`` would pass while passing nothing. The walk
    includes ``asyncio.to_thread(fn, ...)``.
    """
    import ast

    watched = {"pooled_session_servers", "injection_server_names"}
    decider = "overlay_project_scope"

    def _scoped(node):
        """True when this call site decided its own overlay scope."""
        for kw in node.keywords:
            if kw.arg == "work_dir":
                return True
            if kw.arg is None and isinstance(kw.value, ast.Call):
                func = kw.value.func
                named = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if named == decider:
                    return True
        return False

    def _called(node):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == "to_thread":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Name):
                name = first.id
            elif isinstance(first, ast.Attribute):
                name = first.attr
            else:
                name = None
        return name

    src_root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    missing: list[str] = []
    sites = 0
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "session_servers.py":
            continue  # the definitions themselves, and their own internal calls
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in watched):
            continue  # no spelling of either name, so no call node can match
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call) or _called(node) not in watched:
                continue
            sites += 1
            if not _scoped(node):
                missing.append(f"{path.relative_to(src_root)}:{node.lineno}")
    # Six, not seven: the runtime's two session-array paths resolve the overlay
    # through ONE module-level hop (``_pooled_session_servers_and_ref_spec``), so
    # the walk meets their call once, where the decider is splatted.
    assert sites >= 6, f"the walk found only {sites} call sites; it stopped matching"
    assert missing == [], f"call sites resolving the overlay by name alone: {missing}"


# ── the mechanism itself, pinned against the shipped binary ─────────────────


#: Opt-in only, checked BEFORE ``PATH``: this suite spawns the REAL kiro-cli, which
#: testing-conventions.md otherwise forbids. The child reads the operator's real
#: sign-in store and can pop a browser login (acp-client.md), so presence on ``PATH``
#: is not consent; a human sets the lever, as ``KIROCREW_E2E_SCENARIOS_REAL_AGENT``.
REAL_CLI = shutil.which("kiro-cli") if os.environ.get("KIROCREW_E2E_REAL_KIRO_CLI") == "1" else None

#: A tiny, portable MCP server that records that it launched. kiro-cli keeps a
#: server only after the initialize handshake, so the probe answers it. The marker
#: path arrives as ``argv`` so Windows backslashes never pass through a string literal.
_PROBE_SCRIPT = r"""
import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text("x", encoding="utf-8")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request.get("method") == "initialize":
        params = request.get("params") or {}
        result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {},
            "serverInfo": {"name": "kirocrew-precedence-probe", "version": "1"},
        }
    elif request.get("method") == "tools/list":
        result = {"tools": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""

_DRIVER = r"""
import json, os, subprocess, sys, threading, time
from kiro_crew import platform_compat

w = sys.argv[1]
p = subprocess.Popen(["kiro-cli", "acp", "--agent", "pooltest"], cwd=w + "/proj",
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True,
                     env={**os.environ, "KIRO_HOME": w + "/khome"})


def teardown():
    # The whole TREE, not just kiro-cli: the probe MCP servers are ITS children
    # and inherit its cwd (the work dir), so a bare p.kill() leaves them alive
    # holding a directory handle on it. On Windows that handle makes the caller's
    # rmtree fail with WinError 32 ("used by another process"), so the test blew
    # up in TemporaryDirectory teardown after its assertions had already passed.
    try:
        platform_compat.kill_process_tree(p.pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        p.kill()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # Close our ends of the pipes too, so no descriptor of ours outlives the run.
    for stream in (p.stdin, p.stdout, p.stderr):
        try:
            stream.close()
        except OSError:
            pass


send = lambda o: (p.stdin.write(json.dumps(o) + "\n"), p.stdin.flush())
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": 1, "clientCapabilities":
                 {"fs": {"readTextFile": False, "writeTextFile": False}}}})
# Wait for the initialize RESPONSE rather than a fixed span, but on a THREAD with
# a bound: a bare readline() on a stalled CLI blocks forever, and this driver's
# own 180s subprocess timeout is longer than the suite's --timeout=120, so the
# hang would surface as a pytest timeout kill rather than the clean failure
# below. A cooperative CLI answers in well under a second.
_line = []
_t = threading.Thread(target=lambda: _line.append(p.stdout.readline()), daemon=True)
_t.start()
_t.join(30)
if not _line:
    teardown()
    sys.exit("kiro-cli never answered initialize within 30s")
send({"jsonrpc": "2.0", "id": 2, "method": "session/new",
      "params": {"cwd": w + "/proj", "mcpServers": [
          {"name": "shared", "command": sys.executable,
           "args": [sys.argv[2], w + "/marks/INJECTED"], "env": []}]}})
# Poll for the marker the injected server writes, with a deadline generous
# enough for a cold CLI. The old 8s sleep was paid in full on every run.
deadline = time.time() + 20
injected = os.path.join(w, "marks", "INJECTED")
while time.time() < deadline and not os.path.exists(injected):
    time.sleep(0.05)
# Give a same-named spec server, if injection ever became additive, the same
# chance to write its own marker -- otherwise this driver could exit before the
# thing the test asserts is ABSENT would have appeared, making it pass vacuously.
time.sleep(1)
teardown()
"""


@pytest.mark.skipif(
    not REAL_CLI,
    reason="set KIROCREW_E2E_REAL_KIRO_CLI=1 with kiro-cli on PATH to pin "
    "project agent-spec discovery against the real binary",
)
def test_real_kiro_cli_discovers_project_json_specs_and_not_project_markdown():
    """Kiro-cli lists project JSON specs only, which ``markdown_specs=False`` rests on.

    If it discovered project ``*.md``, a user-level stub would mount its credentials
    under the checkout's agent. On failure, add the backend to
    ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        agents = Path(w) / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "probe-json.json").write_text(
            json.dumps({"name": "probe-json", "description": "discovery probe"}),
            encoding="utf-8",
        )
        (agents / "probe-md.md").write_text(
            "---\nname: probe-md\ndescription: discovery probe\n---\n\nbody\n",
            encoding="utf-8",
        )
        child_tmp = Path(w) / "tmp"
        child_tmp.mkdir()
        result = subprocess.run(
            [REAL_CLI, "agent", "list"],
            cwd=w,
            capture_output=True,
            env={
                **os.environ,
                "TMPDIR": str(child_tmp),
                "TMP": str(child_tmp),
                "TEMP": str(child_tmp),
            },
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        # The listing goes to stderr on 2.22.0, and the rows carry ANSI colour.
        seen = re.sub(r"\x1b\[[0-9;]*m", "", f"{result.stdout}\n{result.stderr}")
        workspace_rows = re.findall(r"^\s*\*?\s*(\S+)\s+Workspace\s", seen, flags=re.MULTILINE)
        assert "probe-json" in workspace_rows, (
            "kiro-cli listed no workspace agent for a project *.json spec, so this "
            "probe is not measuring project discovery at all (a CLI flag or output "
            f"shape changed): rows={workspace_rows}\n{seen[-1500:]}"
        )
        assert "probe-md" not in workspace_rows, (
            "kiro-cli now discovers a project *.md agent spec. Add this backend to "
            "ACP_BACKENDS_MARKDOWN_AGENT_SPECS so overlay_project_scope stops "
            "answering markdown_specs=False for it -- otherwise a project foo.md "
            "shadowing a user-level foo.json keeps the user-level stub, which "
            "outranks the project's declaration and runs its command under the "
            f"checkout's agent: rows={workspace_rows}"
        )


@pytest.mark.skipif(
    not REAL_CLI,
    reason="set KIROCREW_E2E_REAL_KIRO_CLI=1 with kiro-cli on PATH to pin "
    "session/new precedence against the real binary",
)
def test_real_kiro_cli_prefers_session_injected_server():
    """ANTI-DRIFT GUARD. Pins the undocumented precedence pooling relies on.

    kiro-cli documents priority only among its file tiers, not that a ``session/new``
    server outranks the agent spec. If injection became additive, every pooled server
    would run twice. Probes run via ``sys.executable``, so this works on every platform.
    """
    # ignore_cleanup_errors: the spawned kiro-cli subprocess can still hold a
    # handle on the temp tree at teardown; Windows refuses to delete a dir with
    # open handles (POSIX does not), which would raise WinError 32 out of the
    # context manager even though the test's assertions already passed.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as w:
        root = Path(w)
        (root / "khome" / "agents").mkdir(parents=True)
        (root / "proj").mkdir()
        (root / "marks").mkdir()
        probe = root / "probe.py"
        probe.write_text(_PROBE_SCRIPT, encoding="utf-8")
        (root / "khome" / "agents" / "pooltest.json").write_text(
            json.dumps(
                {
                    "name": "pooltest",
                    "description": "precedence probe",
                    "model": "claude-haiku-4.5",
                    "tools": [],
                    "prompt": "probe",
                    "mcpServers": {
                        "shared": {
                            "command": sys.executable,
                            "args": [str(probe), str(root / "marks" / "FROM_SPEC")],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        driver = root / "drive.py"
        driver.write_text(_DRIVER, encoding="utf-8")
        # kiro-cli writes its own log directory and telemetry spool under TMPDIR;
        # aimed at this tree, that residue is deleted with the test's directory
        # instead of outliving it in the shared temp root.
        child_tmp = root / "tmp"
        child_tmp.mkdir()
        child_env = {
            **os.environ,
            "TMPDIR": str(child_tmp),
            "TMP": str(child_tmp),
            "TEMP": str(child_tmp),
        }
        result = subprocess.run(
            [sys.executable, str(driver), str(root), str(probe)],
            capture_output=True,
            env=child_env,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        wait_until(
            (root / "marks" / "INJECTED").exists,
            describe=lambda: (
                "session/new-injected server never launched — ACP injection is not "
                "taking effect at all; pooling cannot work through this channel\n"
                f"driver exit: {result.returncode}\n"
                f"driver stdout: {result.stdout[-2000:]}\n"
                f"driver stderr: {result.stderr[-2000:]}"
            ),
        )
        assert not (root / "marks" / "FROM_SPEC").exists(), (
            "the agent spec's same-named server ALSO launched: session/new "
            "injection has become additive rather than overriding, so every "
            "pooled server would run twice. Pooling delivery must change."
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX pathing in fixture")
def test_injection_writes_nothing_to_the_work_dir(tmp_path):
    """The whole point of this channel: no file lands in the user's project."""
    work = tmp_path / "project"
    (work / ".kiro").mkdir(parents=True)
    overlay = _write_overlay(tmp_path, "kirocrew", {"pooled": _stub()})
    before = {p for p in work.rglob("*")}
    assert pooled_session_servers(overlay, "kirocrew")
    assert {p for p in work.rglob("*")} == before


# --- registry ceiling: a broker stub is an unmarked entry, so it is withheld --


def _registry_mode(monkeypatch, enabled):
    """Force the operator's registry-mode declaration for one test.

    Patched on ``kiro_crew.agent``, which owns the flag, because
    ``session_servers`` binds it per call rather than at import time.
    """
    from kiro_crew import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_mcp_registry_mode", lambda: enabled)


def _control_plane_name():
    """One of Crew's own control-plane server names, read from its owner."""
    from kiro_crew.mcp_cleanup import CONTROL_PLANE_SERVERS

    return CONTROL_PLANE_SERVERS[0]


def test_registry_mode_withholds_a_third_party_stub(tmp_path, monkeypatch):
    """The governed install must not be handed a stub for a spec-declared server.

    Nothing in this process can resolve a server name against the admin's
    catalog, so a stub cannot be positively authorized and is withheld. Both
    overlay reads answer empty, which is what leaves the session resolving that
    server from the agent spec instead.
    """
    overlay = _write_overlay(tmp_path, "kirocrew", {"fetch": _stub()})
    _registry_mode(monkeypatch, True)
    assert pooled_session_servers(overlay, "kirocrew") == []
    assert injection_server_names(overlay, "kirocrew") == frozenset()


def test_registry_mode_off_injects_every_stub(tmp_path, monkeypatch):
    """The ceiling is scoped to registry mode and changes nothing outside it."""
    overlay = _write_overlay(tmp_path, "kirocrew", {"fetch": _stub()})
    _registry_mode(monkeypatch, False)
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == ["fetch"]
    assert injection_server_names(overlay, "kirocrew") == frozenset({"fetch"})


def test_registry_mode_keeps_the_control_plane_stub(tmp_path, monkeypatch):
    """Crew's own control plane is exempt, on the spec half's own grounds.

    It is the host's own process rather than a third-party server the catalog
    governs, and withholding it would leave the session unable to report back to
    its channel at all -- on precisely the installs that are most governed.
    """
    control = _control_plane_name()
    overlay = _write_overlay(tmp_path, "kirocrew", {"fetch": _stub(), control: _stub()})
    _registry_mode(monkeypatch, True)
    assert [e["name"] for e in pooled_session_servers(overlay, "kirocrew")] == [control]
    assert injection_server_names(overlay, "kirocrew") == frozenset({control})


def test_a_registry_marked_entry_has_no_stub_to_withhold(tmp_path, monkeypatch):
    """The marked half is closed upstream, in the rewriter.

    A ``type: "registry"`` entry is never wrapped, so the overlay holds it
    unwrapped and no stub for it exists in either mode. The ceiling has nothing
    to do here, and the entry stays the agent spec's to launch.
    """
    marked = {"command": "npx", "type": "registry", "env": {"TOKEN": "s3cr3t"}}
    overlay = _write_overlay(tmp_path, "kirocrew", {"governed": marked})
    for enabled in (False, True):
        _registry_mode(monkeypatch, enabled)
        assert pooled_session_servers(overlay, "kirocrew") == []
        assert injection_server_names(overlay, "kirocrew") == frozenset()


def test_a_server_not_routed_through_the_gateway_is_unaffected(tmp_path, monkeypatch):
    """An operator who never opted a server into pooling has no stub either way.

    Routing is the opt-in that makes a stub exist at all, so an unrouted server
    is left entirely to the spec in both modes, and its credentials stay in the
    file they were declared in.
    """
    unrouted = {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": "s3cr3t"}}
    overlay = _write_overlay(tmp_path, "kirocrew", {"unrouted": unrouted})
    for enabled in (False, True):
        _registry_mode(monkeypatch, enabled)
        assert pooled_session_servers(overlay, "kirocrew") == []
        assert injection_server_names(overlay, "kirocrew") == frozenset()
        assert "s3cr3t" not in json.dumps(pooled_session_servers(overlay, "kirocrew"))


def test_a_withheld_stub_is_one_kiro_cli_would_drop_anyway(tmp_path, monkeypatch):
    """Withholding here costs the kiro-cli path nothing.

    Under registry mode that client keeps only entries carrying
    ``type: "registry"``. A stub never carries one, so the element this ceiling
    withholds is exactly the element the client drops on arrival.
    """
    overlay = _write_overlay(tmp_path, "kirocrew", {"fetch": _stub()})
    _registry_mode(monkeypatch, False)
    (element,) = pooled_session_servers(overlay, "kirocrew")
    assert element.get("type") != "registry"
    _registry_mode(monkeypatch, True)
    assert pooled_session_servers(overlay, "kirocrew") == []


def test_the_two_overlay_reads_agree_in_both_modes(tmp_path, monkeypatch):
    """The set and the elements must name the same servers.

    A mirror withholds every name in the set from its own projection, so a name
    with no element behind it would withhold the spec's only copy of that server
    and the session would get nothing for it.
    """
    servers = {"fetch": _stub(), _control_plane_name(): _stub()}
    overlay = _write_overlay(tmp_path, "kirocrew", servers)
    for enabled in (False, True):
        _registry_mode(monkeypatch, enabled)
        elements = pooled_session_servers(overlay, "kirocrew")
        assert injection_server_names(overlay, "kirocrew") == frozenset(
            e["name"] for e in elements
        ), f"the two overlay reads disagree with registry mode {enabled}"


def test_both_ceilings_read_one_control_plane_tuple():
    """The stub ceiling and the spec ceiling must exempt the same names.

    The leaf holds the one tuple and the ACP module re-exports it. It is narrower than
    the always-on set, which answers whether a server must appear in an agent spec.
    """
    from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS as via_acp
    from kiro_crew.mcp_cleanup import ALWAYS_ON_BIN_MCP_SERVERS
    from kiro_crew.mcp_cleanup import CONTROL_PLANE_SERVERS as via_leaf

    assert via_acp is via_leaf, "the two ceilings read separate tuples"
    assert set(via_leaf) < set(
        ALWAYS_ON_BIN_MCP_SERVERS
    ), "the control plane must stay a strict subset of the always-on servers"

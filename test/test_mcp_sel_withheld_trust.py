"""A Kiro Crew MCP server whose harness withholds the SEL trust root.

Under an enforcing harness the credential mask withholds ``trust/`` from the
harness's process tree on purpose, so the SEL cannot be built there. On macOS the
sandbox denies the ``stat`` of the withheld directory, so ``mkdir`` answers EEXIST
while ``isdir()`` answers False. These pins cover the error that reaches the reader,
and the MCP path that hands each record to the gateway over the agent socket
(``mcp_sel_forward``) and refuses the call when it cannot.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import threading

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import mcp_server_mark
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.platform_compat import IS_POSIX

posix_only = pytest.mark.skipif(not IS_POSIX, reason="the owner-only mkdir walk is POSIX-only")


def _deny_stat_of(monkeypatch, target: str) -> None:
    """The name exists (mkdir says EEXIST) but the sandbox denies inspecting it."""
    real_mkdir, real_isdir, real_lstat = os.mkdir, os.path.isdir, os.lstat

    def _mkdir(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise FileExistsError(errno.EEXIST, "File exists", target)
        return real_mkdir(path, *args, **kwargs)

    def _isdir(path):
        return False if os.fspath(path) == target else real_isdir(path)

    def _lstat(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise PermissionError(errno.EPERM, "Operation not permitted", target)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", _mkdir)
    monkeypatch.setattr(os.path, "isdir", _isdir)
    monkeypatch.setattr(os, "lstat", _lstat)


@posix_only
def test_stat_denied_name_raises_permission_error_naming_the_path(tmp_path, monkeypatch):
    target = str(tmp_path / "trust")
    _deny_stat_of(monkeypatch, target)

    with pytest.raises(PermissionError) as raised:
        mkdirs_owner_only(target)

    assert raised.value.errno == errno.EPERM
    assert raised.value.filename == target
    assert not isinstance(raised.value, FileExistsError)


@posix_only
def test_existing_directory_is_still_accepted(tmp_path):
    target = tmp_path / "trust"
    target.mkdir()
    mkdirs_owner_only(target)
    assert target.is_dir()


@posix_only
def test_existing_file_still_raises_file_exists(tmp_path):
    target = tmp_path / "trust"
    target.write_text("x")
    with pytest.raises(FileExistsError):
        mkdirs_owner_only(target)


@posix_only
def test_dangling_symlink_still_raises_file_exists(tmp_path):
    target = tmp_path / "trust"
    target.symlink_to(tmp_path / "absent")
    with pytest.raises(FileExistsError):
        mkdirs_owner_only(target)


_WITHHELD = PermissionError(errno.EPERM, "Operation not permitted", "/home/u/.kiro/crew/trust")


@pytest.fixture
def sel_init(monkeypatch):
    """Make SEL init raise *exc*, with the mark and the warning reset for each test."""
    monkeypatch.setattr(mcp_server_mark, "_mcp_stdio_server", False)
    monkeypatch.setattr(sel_mod, "_withheld_warned", threading.Event())

    def _raising(exc: BaseException, *, withheld: bool = True) -> None:
        def _init(self, *_a, **_k):
            raise exc

        monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
        monkeypatch.setattr(sel_mod.SecurityEventLog, "_init_locked", _init)
        monkeypatch.setattr(sel_mod, "_trust_root_withheld", lambda: withheld)

    return _raising


class _Gateway:
    """The agent socket and the gateway's route, in process: records land in ``rows``."""

    def __init__(self, monkeypatch, *, capability: str = "cap", session: str = "dashboard:s1"):
        import urllib.error

        from kiro_crew import agent_ipc_client
        from kiro_crew.dashboard.handlers import ipc_sel

        self.rows: list[tuple[str, dict]] = []
        self.headers: list[dict[str, str]] = []
        self.bodies: list[bytes] = []
        self.status = 204
        gateway = self

        class _Log:
            def log_tool_invocation(self, **kw):
                gateway.rows.append(("log_tool_invocation", kw))

            def log_api_access(self, **kw):
                gateway.rows.append(("log_api_access", kw))

        class _Resp:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def _open(req, _timeout, *, capability, socket_path):
            assert capability == "cap" and socket_path == "/sock"
            if gateway.status != 204:
                raise urllib.error.HTTPError(req.full_url, gateway.status, "no", {}, None)
            gateway.headers.append(dict(req.header_items()))
            gateway.bodies.append(req.data)
            parsed = ipc_sel._parse(req.data)
            assert parsed is not None, req.data
            # The real route's writer, with the capability's server and the session the
            # agent socket gate held X-Session-Key to.
            real_sel = ipc_sel.sel
            ipc_sel.sel = lambda: _Log()
            try:
                ipc_sel._write(req.get_header("X-session-key"), "kirocrew-core", *parsed)
            finally:
                ipc_sel.sel = real_sel
            return _Resp()

        monkeypatch.setattr(
            agent_ipc_client, "current_capability", lambda _p: (capability, "/sock")
        )
        monkeypatch.setattr(agent_ipc_client, "capability_session_key", lambda: session)
        monkeypatch.setattr(agent_ipc_client, "open_with_capability", _open)


def _three_tools(monkeypatch) -> dict[str, str]:
    """The issue's three tools through kirocrew-core's real call path, gateway reads stubbed."""
    from kiro_crew import diagnostics, mcp_core

    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *_a, **_k: ("sk", None))
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda *_a, **_k: "sk")
    monkeypatch.setattr(
        mcp_core,
        "_resolve_tool_policy",
        lambda *a, **k: mcp_core.ToolPolicy(frozenset(), ""),
        raising=False,
    )
    monkeypatch.setattr(diagnostics, "read_kiro_cli_logs", lambda **_k: "log tail")
    return {
        # An empty query is skill_search's own validation path, which audits in the handler body.
        "skill_search": mcp_core._call_tool("skill_search", {"query": ""}),
        "kiro_cli_logs": mcp_core._call_tool("kiro_cli_logs", {"tail": 5}),
        "resource_status": mcp_core._call_tool("resource_status", {}),
    }


def test_marked_server_forwards_the_three_tools_records_tagged_with_its_role(
    monkeypatch, caplog, sel_init
):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    caplog.set_level(logging.WARNING, logger="kiro_crew.sel")

    first = _three_tools(monkeypatch)
    second = _three_tools(monkeypatch)

    for out in (first, second):
        assert "browse" in out["skill_search"]
        assert out["kiro_cli_logs"] == "log tail"
        assert "Guidance:" in out["resource_status"]
    tools = {kw.get("tool_name") for kind, kw in gateway.rows if kind == "log_tool_invocation"}
    assert {"skill_search", "kiro_cli_logs", "resource_status"} <= tools
    for kind, kw in gateway.rows:
        if kind == "log_tool_invocation":
            assert kw["session_key"] == "dashboard:s1" and kw["source"] == "mcp"
            assert kw["metadata"]["process_role"] == "mcp_stdio_server"
            assert kw["metadata"]["server"] == "kirocrew-core"
    warnings = [r for r in caplog.records if "SEL trust root withheld" in r.getMessage()]
    assert len(warnings) == 1
    assert "/home/u/.kiro/crew/trust" in warnings[0].getMessage()


def test_no_capability_refuses_the_call_naming_the_trust_root(monkeypatch, sel_init):
    from kiro_crew import agent_ipc_client

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    monkeypatch.setattr(agent_ipc_client, "current_capability", lambda _p: ("", ""))
    with pytest.raises(PermissionError) as raised:
        sel_mod.sel().log_tool_invocation(session_key="sk", tool_name="t", outcome="completed")
    assert raised.value.filename == "/home/u/.kiro/crew/trust"
    assert "no agent-socket capability" in str(raised.value)


def test_a_refused_record_refuses_the_call(monkeypatch, sel_init):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    gateway.status = 400
    with pytest.raises(PermissionError, match="HTTP 400"):
        sel_mod.sel().log_api_access(caller="x", operation="o", outcome="ok")


def test_the_forwarder_never_sends_an_actor(monkeypatch, sel_init):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    sel_mod.sel().log_api_access(
        caller="dashboard:other", source="cron", operation="o", outcome="ok", forwarded=True
    )
    ((_kind, kw),) = gateway.rows
    assert kw["caller"] == "dashboard:s1" and kw["source"] == "mcp"
    assert kw["resources"].startswith("process_role=mcp_stdio_server,server=kirocrew-core;")


def test_a_forwarded_record_carries_the_session_token(monkeypatch, sel_init):
    """A shared runtime names the caller only behind a verified token."""
    monkeypatch.setenv("KIROCREW_STUB_SESSION_TOKEN", "tok-1")
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    sel_mod.sel().log_api_access(caller="x", operation="o", outcome="ok")
    ((sent),) = gateway.headers
    assert sent.get("X-session-token") == "tok-1"


def test_a_long_field_is_clipped_not_refused(monkeypatch, sel_init):
    """A value the local writer would clip must not evade the audit under the mask."""
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    long = "x" * 5000
    sel_mod.sel().log_tool_invocation(
        session_key="sk",
        tool_name=long,
        outcome="completed",
        resources=long,
        metadata={f"k{i}": long for i in range(40)},
    )
    ((_kind, kw),) = gateway.rows
    assert kw["tool_name"] == "x" * 500 and kw["resources"] == "x" * 500
    assert len(kw["metadata"]) <= 18


def test_a_credential_straddling_the_clip_is_redacted_first(monkeypatch, sel_init):
    from kiro_crew.testing import synthetic_credentials

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    token = synthetic_credentials.github_classic()
    straddling = "a" * 480 + token
    sel_mod.sel().log_tool_invocation(
        session_key="sk",
        tool_name="t",
        outcome="completed",
        resources=straddling,
        metadata={"q": straddling, "nested": {"deep": straddling}},
    )
    ((_kind, kw),) = gateway.rows
    sent = json.dumps(kw)
    body = token[4:]
    assert not any(body[i : i + 8] in sent for i in range(len(body) - 8))


@pytest.mark.parametrize("char", ['"', "\\", "\u00e9", "\U0001f600"])
def test_an_escape_heavy_record_stays_under_the_body_cap(monkeypatch, sel_init, char):
    from kiro_crew.apps.sel_routes import _MAX_BODY

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    value = char * 500
    sel_mod.sel().log_tool_invocation(
        session_key="sk",
        tool_name=value,
        outcome="completed",
        resources=value,
        error=value,
        metadata={f"k{i}": value for i in range(16)},
    )
    assert len(gateway.rows) == 1
    assert max(len(b) for b in gateway.bodies) <= _MAX_BODY


def test_a_record_that_cannot_be_prepared_refuses(monkeypatch, sel_init):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    _Gateway(monkeypatch)

    def _too_deep(_value):
        # What the deep redaction raises for metadata nested past the recursion
        # limit; raised directly so the shared redactor's state is not disturbed.
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(sel_mod, "_redacted_metadata_copy", _too_deep)
    with pytest.raises(PermissionError, match="could not be prepared"):
        sel_mod.sel().log_tool_invocation(
            session_key="sk", tool_name="t", outcome="completed", metadata={"deep": {}}
        )


def test_one_redaction_boundary_serves_the_local_log_and_the_forwarded_record(
    tmp_path, monkeypatch, sel_init
):
    """The same ``sel`` call redacts a credential whether the record is written here or
    forwarded: both go through the registered SEL sink's passes."""
    from kiro_crew.testing import synthetic_credentials

    token = synthetic_credentials.github_classic()
    fields = dict(
        session_key="sk",
        source="mcp",
        tool_name="t",
        outcome="completed",
        resources=f"query used {token}",
        metadata={"q": token},
    )
    # Local writer, trust root readable.
    local = sel_mod.SecurityEventLog.__new__(sel_mod.SecurityEventLog)
    local._initialized = False
    local.__init__(base_dir=tmp_path / "local", sync=True)
    try:
        local.log_tool_invocation(**fields)
    finally:
        monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    written = (tmp_path / "local" / "security_events.jsonl").read_text()
    assert token not in written and token[4:20] not in written
    # Forwarded, trust root withheld.
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    sel_mod.sel().log_tool_invocation(**fields)
    ((_kind, kw),) = gateway.rows
    sent = json.dumps(kw)
    assert token not in sent and token[4:20] not in sent


def _leak_cases(secret: str) -> list[dict]:
    from pathlib import PurePosixPath

    return [
        {"metadata": {"q": {secret}}},
        {"metadata": {"q": secret.encode()}},
        {"metadata": {"q": [secret.encode()]}},
        {"metadata": {"q": RuntimeError("boom", secret)}},
        {"metadata": {"q": PurePosixPath(f"/tmp/{secret}")}},
        {"metadata": {"q": [frozenset([secret])]}},
        {"metadata": {(secret,): 1}},
        {"metadata": {secret.encode(): 1}},
        {"metadata": [secret]},
        {"resources": [secret]},
        {"resources": secret.encode()},
        {"error": RuntimeError("boom", secret)},
    ]


@pytest.mark.parametrize("which", ["token", "assignment"])
def test_no_value_shape_carries_a_credential_past_the_boundary(monkeypatch, sel_init, which):
    """A value only redactable after rendering is refused, so nothing reaches the body;
    the assignment form is anchored on the text around it, so a render would miss it."""
    from kiro_crew.testing import synthetic_credentials

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    value = (
        synthetic_credentials.github_classic()
        if which == "token"
        else "aws_secret_access_key=" + "Ab1/" * 10
    )
    for case in _leak_cases(value):
        with pytest.raises(PermissionError, match="could not be prepared"):
            sel_mod.sel().log_tool_invocation(tool_name="t", outcome="completed", **case)
    assert gateway.bodies == []


def test_nested_text_metadata_is_redacted_leaf_by_leaf(monkeypatch, sel_init):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    secret = "aws_secret_access_key=" + "Ab1/" * 10
    sel_mod.sel().log_tool_invocation(
        tool_name="t", outcome="completed", metadata={"q": [secret], "d": {"k": (secret,)}}
    )
    ((_kind, kw),) = gateway.rows
    assert "Ab1/Ab1/Ab1/" not in json.dumps(kw)


def test_a_numeric_subclass_repr_is_never_rendered(monkeypatch, sel_init):
    """A nested value is rendered as JSON, so a caller's ``__repr__`` never runs."""
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    secret = "aws_secret_access_key=" + "Ab1/" * 10

    class IntS(int):
        def __repr__(self):
            return secret

    class FloatS(float):
        def __repr__(self):
            return secret

    sel_mod.sel().log_tool_invocation(
        tool_name="t",
        outcome="completed",
        metadata={"q": [IntS(1)], "d": {"k": IntS(2)}, "f": [FloatS(1.5)]},
    )
    ((_kind, kw),) = gateway.rows
    assert "Ab1/Ab1/" not in json.dumps(kw)
    assert kw["metadata"]["q"] == "[1]"


def test_an_unknown_keyword_is_refused_before_it_is_encoded(monkeypatch, sel_init):
    from kiro_crew.testing import synthetic_credentials

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    token = synthetic_credentials.github_classic()
    with pytest.raises(PermissionError, match="could not be prepared"):
        sel_mod.sel().log_tool_invocation(tool_name="t", outcome="completed", **{token: 1})
    assert gateway.bodies == []


def test_the_forwarder_sends_exactly_the_fields_the_route_accepts():
    from kiro_crew import mcp_sel_forward
    from kiro_crew.dashboard.handlers import ipc_sel

    assert {
        method: frozenset(required) | frozenset(optional)
        for method, (required, optional) in ipc_sel._ALLOWED.items()
    } == mcp_sel_forward._ROUTE_FIELDS


def test_the_forwarder_calls_no_redactor():
    """One redaction boundary: the forwarder carries records ``sel`` already redacted."""
    import inspect
    import re

    from kiro_crew import mcp_sel_forward

    redactor_call = re.compile(r"\bStreamRedactor\(|\b\w*redact\w*\(|\.redact\(|\brefuses\(")
    assert redactor_call.findall(inspect.getsource(mcp_sel_forward)) == []
    assert "forwardable_fields" in inspect.getsource(mcp_sel_forward)


def test_an_unforwarded_kind_refuses(monkeypatch, sel_init):
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    _Gateway(monkeypatch)
    with pytest.raises(PermissionError, match="not forwarded"):
        sel_mod.sel().log_governance_degraded(
            session_key="sk", decision_point="p", reason="r", failed_closed=True
        )


def test_an_excluded_tool_is_refused_and_its_decision_forwarded(monkeypatch, sel_init):
    from mcp_shared_helpers import _LoopHarness, _tools_call

    import kiro_crew.mcp_shared as mcp_shared

    ran = []
    monkeypatch.setattr(mcp_shared, "_framing", None)
    harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
    monkeypatch.setattr(
        mcp_shared,
        "_resolve_tool_policy",
        lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"blocked"}), ""),
    )
    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    monkeypatch.setattr(mcp_shared, "sel", sel_mod.sel)
    try:
        harness.send(_tools_call(9, "blocked"))
        assert harness.wait_for(lambda: len(harness.responses) >= 1)
        assert "not available" in str(harness.responses[0][1])
        assert ran == []
        ((_kind, kw),) = [r for r in gateway.rows if r[1].get("outcome") == "rejected_excluded"]
        assert kw["session_key"] == "dashboard:s1"
    finally:
        harness.close()


def test_unmarked_process_still_raises_the_init_error(sel_init):
    sel_init(_WITHHELD)
    with pytest.raises(PermissionError):
        sel_mod.sel()


def test_a_tampered_but_inspectable_trust_root_still_raises_in_a_marked_process(sel_init):
    """A key set to mode 000 or a file planted at ``trust`` leaves ``lstat`` working."""
    sel_init(_WITHHELD, withheld=False)
    mcp_server_mark.note_mcp_stdio_server()
    with pytest.raises(PermissionError):
        sel_mod.sel()


def test_file_exists_forwards_in_a_marked_process(monkeypatch, sel_init):
    sel_init(FileExistsError(errno.EEXIST, "File exists", "/home/u/.kiro/crew/trust"))
    mcp_server_mark.note_mcp_stdio_server()
    gateway = _Gateway(monkeypatch)
    sel_mod.sel().log_tool_invocation(session_key="sk", tool_name="t", outcome="completed")
    assert len(gateway.rows) == 1


def _lstat_of_trust_raises(monkeypatch, tmp_path, exc):
    monkeypatch.setattr(sel_mod, "_default_dir", lambda: tmp_path)
    (tmp_path / "trust").mkdir(exist_ok=True)
    real_lstat = os.lstat

    def _lstat(path, *a, **k):
        if os.fspath(path) == str(tmp_path / "trust"):
            raise exc
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", _lstat)


def test_an_inspectable_trust_root_is_not_withheld(tmp_path, monkeypatch):
    monkeypatch.setattr(sel_mod, "_default_dir", lambda: tmp_path)
    (tmp_path / "trust").mkdir()
    assert sel_mod._trust_root_withheld() is False


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(errno.EPERM, "Operation not permitted"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
    ],
    ids=["EPERM", "EACCES", "EIO"],
)
def test_any_lstat_failure_but_absence_counts_as_withheld(tmp_path, monkeypatch, exc):
    """The predicate does not rest on the errno a sandbox picks."""
    _lstat_of_trust_raises(monkeypatch, tmp_path, exc)
    assert sel_mod._trust_root_withheld() is True


@pytest.mark.parametrize(
    "exc",
    [FileNotFoundError(errno.ENOENT, "No such file"), NotADirectoryError(errno.ENOTDIR, "x")],
    ids=["ENOENT", "ENOTDIR"],
)
def test_an_absent_trust_root_is_not_withheld(tmp_path, monkeypatch, exc):
    """Absent means create it, as on main."""
    _lstat_of_trust_raises(monkeypatch, tmp_path, exc)
    assert sel_mod._trust_root_withheld() is False


def _issue_shape(monkeypatch, home):
    """The reported process: ``trust/`` exists, ``mkdir`` says EEXIST, every inspection of it
    is refused with EPERM (the reporter's ``ls: trust: Operation not permitted``)."""
    trust = str(home / "trust")
    os.makedirs(trust, exist_ok=True)
    real_mkdir, real_stat, real_lstat = os.mkdir, os.stat, os.lstat

    def _deny(real):
        def inner(path, *a, **k):
            if os.fspath(path) == trust:
                raise PermissionError(errno.EPERM, "Operation not permitted", trust)
            return real(path, *a, **k)

        return inner

    def _mkdir(path, *a, **k):
        if os.fspath(path) == trust:
            raise FileExistsError(errno.EEXIST, "File exists", trust)
        return real_mkdir(path, *a, **k)

    monkeypatch.setattr(os, "mkdir", _mkdir)
    monkeypatch.setattr(os, "stat", _deny(real_stat))
    monkeypatch.setattr(os, "lstat", _deny(real_lstat))


@posix_only
def test_the_issue_shape_raised_file_exists_on_main_and_forwards_here(
    tmp_path, monkeypatch, sel_init
):
    """One fact: a denied stat of an existing ``trust/`` is what makes mkdir's EEXIST
    surface as "File exists", and the same shape is what turns forwarding on."""
    from kiro_crew.mcp_sel_forward import ForwardingLog

    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setattr(mcp_server_mark, "_mcp_stdio_server", False)
    monkeypatch.setattr(sel_mod, "_withheld_warned", threading.Event())
    monkeypatch.setattr(sel_mod.SecurityEventLog, "_instance", None)
    monkeypatch.setattr(sel_mod, "_default_dir", lambda: home)
    _issue_shape(monkeypatch, home)
    # Main's directory walk: ``except OSError: if not os.path.isdir(target): raise``
    # with isdir() False because stat raised.
    assert os.path.isdir(home / "trust") is False
    with pytest.raises(OSError) as main_shape:
        try:
            os.mkdir(home / "trust", 0o700)
        except OSError:
            if not os.path.isdir(home / "trust"):
                raise
    assert isinstance(main_shape.value, FileExistsError)
    mcp_server_mark.note_mcp_stdio_server()
    _Gateway(monkeypatch)
    assert isinstance(sel_mod.sel(), ForwardingLog)


def test_a_decision_record_with_no_channel_is_refused_without_critical(monkeypatch, sel_init):
    """Decision records fail closed through the forwarder like every record."""
    from kiro_crew import agent_ipc_client

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    monkeypatch.setattr(agent_ipc_client, "current_capability", lambda _p: ("", ""))
    with pytest.raises(PermissionError):
        sel_mod.sel().log_tool_invocation(
            session_key="sk",
            source="mcp",
            tool_name="blocked",
            tool_kind="test-server",
            outcome="rejected_excluded",
            error="managedToolPolicy.exclude",
        )


def test_a_trust_root_that_becomes_readable_restores_the_local_writer(monkeypatch, sel_init):
    from kiro_crew.mcp_sel_forward import ForwardingLog

    sel_init(_WITHHELD)
    mcp_server_mark.note_mcp_stdio_server()
    assert isinstance(sel_mod.sel(), ForwardingLog)
    real = object.__new__(sel_mod.SecurityEventLog)
    monkeypatch.setattr(sel_mod, "SecurityEventLog", lambda: real)
    assert sel_mod.sel() is real


def test_other_init_errors_still_raise_in_a_marked_process(sel_init):
    sel_init(RuntimeError("trust root too short"))
    mcp_server_mark.note_mcp_stdio_server()
    with pytest.raises(RuntimeError, match="too short"):
        sel_mod.sel()


def test_mcp_entry_marks_the_process_before_its_first_sel(monkeypatch, sel_init):
    from kiro_crew import mcp_entry

    monkeypatch.setattr(mcp_entry, "_port_env_ok", lambda: False)
    assert mcp_entry.slim_server(["mcp-core"]) is None
    assert mcp_server_mark.is_mcp_stdio_server()


def test_a_non_server_command_is_not_marked(sel_init):
    from kiro_crew import mcp_entry

    assert mcp_entry.slim_server(["gateway"]) is None
    assert not mcp_server_mark.is_mcp_stdio_server()


_STDIO_TREES = ("mcp_tools", "mcp_core_parts")


def _direct_constructions() -> list[str]:
    """``SecurityEventLog()`` built directly in an MCP stdio server's own code."""
    import ast
    from pathlib import Path

    root = Path(sel_mod.__file__).parent
    files = [*root.glob("mcp_*.py")]
    for tree in _STDIO_TREES:
        files.extend((root / tree).rglob("*.py"))
    found = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "SecurityEventLog"
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno}")
    return found


def test_mcp_stdio_servers_reach_the_sel_only_through_the_accessor():
    """The degrade lives in ``sel()``; a server building the class itself would bypass it."""
    assert _direct_constructions() == []

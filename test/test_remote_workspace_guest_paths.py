"""A remote workspace's file targets meet file rules as the guest's POSIX paths.

The guest is Linux whatever this gateway runs, so governance matches a workspace
tool's targets with ``posixpath`` (``platform/guest_paths.py``), chosen per call by
the type the permission-event builder tags the call's arguments with. A local call
keeps the gateway's own ``os.path`` reading, on Windows exactly as before.

Every test here runs on every OS, Windows shards included. ``windows_reading``
also pins the Windows reading on a POSIX host, by pointing the matcher's ``os``
at ``ntpath``: the deny then binds only through the guest flavour, so dropping the
tag anywhere between the builder and the matcher fails these tests on Linux CI too.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiro_crew import hooks
from kiro_crew import session_environment as se
from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.hook_runtime.tool_identity import ToolCall
from kiro_crew.platform import governance
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import classify_tool_args, gate_decision, parse_policy
from kiro_crew.platform.governance_parts import matching
from kiro_crew.platform.guest_paths import (
    GuestPath,
    GuestPathParams,
    guest_norm,
    guest_unverifiable,
)

ROOT = "/workspace/app"


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


def _nt_expand(path: str) -> str:
    return ntpath.expanduser(ntpath.expandvars(path))


def _nt_norm_item(path: str) -> str:
    """``_norm_item`` as a Windows gateway runs it: the same steps on ``ntpath``."""
    norm = ntpath.abspath(_nt_expand(path))
    if norm.startswith("//") and not norm.startswith("///"):
        norm = norm[1:]
    return norm


def _windows_reading(monkeypatch) -> None:
    # Through the facade: the split family's mirror sends the write to every holder.
    monkeypatch.setattr(governance, "_norm_item", _nt_norm_item)
    monkeypatch.setattr(governance, "_expand", _nt_expand)


@pytest.fixture(params=["this-os", "windows"])
def reading(request, monkeypatch):
    """Run once with this host's own ``os.path`` and once with Windows' ``ntpath``."""
    if request.param == "windows":
        _windows_reading(monkeypatch)
    return request.param


def _policy(monkeypatch, **filesystem):
    ceiling = parse_policy({"version": 1, "boot": {"fail_closed": True}, "filesystem": filesystem})
    context = replace(hooks.current_context(), governance=ceiling)
    monkeypatch.setattr(hooks, "current_context", lambda: context)
    monkeypatch.setattr(gp, "resolve_active_scope", lambda *args, **kw: None)
    return ceiling


def _event(tool, kind, params, *, remote=True, server="workspace"):
    """The event a KAS ``session/request_permission`` for ``@server/tool`` builds."""
    tool_call_id = "tc-1"
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": f"@{server}/{tool}",
                    "kind": kind,
                },
                "options": [{"optionId": "a", "kind": "allow_once"}],
            },
        ),
        raw_params_cache={tool_call_id: dict(params)},
        shell_cache={tool_call_id: False},
        mcp_server_name_cache={tool_call_id: server},
        tool_name_cache={tool_call_id: tool},
        remote_workspace_server=se.WORKSPACE_SERVER_NAME if remote else "",
        remote_workspace_root=ROOT if remote else "",
    )
    assert event is not None
    return event


def _judge(tool, path, *, kind="read", remote=True, **extra):
    """A workspace call judged the way production judges it: the KAS permission
    frame through ``build_permission_event``, ``ToolCall.from_event``, the gate."""
    se.remember_remote_workspace("chat-remote", "dev", ROOT)
    try:
        event = _event(tool, kind, {"path": path, **extra}, remote=remote)
        call = ToolCall.from_event(event)
        return hooks.HookManager().on_tool_call(
            event.title, session_key="chat-remote", **call.kwargs()
        )
    finally:
        se.forget_remote_workspace("chat-remote")


def _denied(result) -> bool:
    return result.action == hooks.TOOL_DENY and result.tier == "governance"


# ── remote: a deny on /workspace/app/** binds every spelling the guest opens ──

#: Every one of these is a file the guest opens inside /workspace/app.
INSIDE = [
    "/workspace/app/x",
    "x",  # relative: anchored at the guest workdir, as the guest resolves it
    "./src/../x",
    "/workspace/other/../app/x",  # .. escape INTO the denied tree
    "//workspace/app/x",  # leading // (Linux opens /workspace/app/x)
    "///workspace/app/x",
    "/workspace//app/./x",
    "/workspace/app/sub\\x",  # mixed separators: still a file under app on Linux
    "/workspace/app/X.PY",
]

#: Every one of these is a file the guest opens OUTSIDE /workspace/app.
OUTSIDE = [
    "/workspace/other/x",
    "../other/x",  # relative .. escape out of the workdir
    "/workspace/app/../other/x",
    "/workspace/APP/x",  # case-sensitive guest: a different directory
    "/workspace/app\\x",  # backslash is a filename character on Linux: /workspace/'app\x'
]


@pytest.mark.parametrize("path", INSIDE)
@pytest.mark.parametrize(
    "tool, kind, scope",
    [
        ("read_file", "read", "read"),
        ("list_directory", "read", "read"),
        ("glob", "read", "read"),
        ("grep", "read", "read"),
        ("write_file", "edit", "write"),
        ("edit_file", "edit", "write"),
    ],
)
def test_a_remote_deny_binds_every_spelling_of_a_path_inside_it(
    monkeypatch, reading, path, tool, kind, scope
):
    _policy(monkeypatch, **{scope: {"mode": "deny", "deny": ["/workspace/app/**"]}})
    assert _denied(_judge(tool, path, kind=kind, pattern="*"))


@pytest.mark.parametrize("path", OUTSIDE)
def test_a_remote_deny_does_not_bind_a_path_the_guest_opens_elsewhere(monkeypatch, reading, path):
    _policy(monkeypatch, write={"mode": "deny", "deny": ["/workspace/app/**"]})
    assert not _denied(_judge("write_file", path, kind="edit"))


@pytest.mark.parametrize(
    "path, permitted",
    [
        ("/workspace/app/public/x", True),
        ("public/x", True),
        ("public/../private/x", False),
        ("public/../../../etc/passwd", False),
        ("//etc/passwd", False),
        ("/etc/passwd", False),
    ],
)
def test_a_remote_allow_confines_to_its_prefix(monkeypatch, reading, path, permitted):
    _policy(monkeypatch, read={"mode": "allow", "allow": ["/workspace/app/public/**"]})
    assert _denied(_judge("read_file", path)) is not permitted


# ── ~ and $VAR never take this gateway's values ──────────────────────────────


@pytest.mark.parametrize("path", ["~/x", "$HOME/x", "${HOME}/x", "/workspace/app/$KC_GUEST_DIR/x"])
def test_an_unexpanded_home_or_variable_is_refused_by_an_allow_confinement(
    monkeypatch, reading, path
):
    monkeypatch.setenv("KC_GUEST_DIR", "public")
    _policy(monkeypatch, read={"mode": "allow", "allow": ["/workspace/app/**"]})
    assert _denied(_judge("read_file", path))


def test_a_remote_target_is_never_expanded_with_this_gateways_values(monkeypatch, reading):
    # This gateway's own variable would turn the literal into a denied path; the
    # guest opens the literal ``$KC_GUEST_DIR`` directory, so the item is that path.
    monkeypatch.setenv("KC_GUEST_DIR", "private")
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/workspace/app/private/**"]})
    assert not _denied(_judge("read_file", "$KC_GUEST_DIR/x"))
    # ... and a deny on the literal the guest opens still binds it.
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/workspace/app/$KC_GUEST_DIR/**"]})
    monkeypatch.delenv("KC_GUEST_DIR")
    assert _denied(_judge("read_file", "$KC_GUEST_DIR/x"))


def test_the_guest_resolves_home_and_variables_literally(tmp_path):
    # The assumption the literal reading rests on: the guest tool server joins a
    # path to its root as written. If it ever expanded either, this fails first.
    if os.name == "nt":
        pytest.skip("the guest tool server runs on a Linux guest")
    from kiro_crew.remote_session.workspace_tools import Workspace

    ws = Workspace(str(tmp_path))
    for spelling in ("~/x", "$HOME/x"):
        assert ws.resolve(spelling, must_exist=False) == str(tmp_path.resolve() / spelling)


# ── the flavour is chosen per call, never globally ───────────────────────────


def test_a_local_call_through_a_server_named_workspace_keeps_the_gateway_reading(
    monkeypatch, reading
):
    # Same frame, but the runtime attached no remote workspace: no tag, so the
    # target is matched as this gateway's path, exactly as before this change.
    event = _event("read_file", "read", {"path": "/workspace/app/x"}, remote=False)
    assert type(event.raw_tool_params) is dict
    remote = _event("read_file", "read", {"path": "/workspace/app/x"})
    assert isinstance(remote.raw_tool_params, GuestPathParams)
    assert remote.raw_tool_params.root == ROOT
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/workspace/app/**"]})
    local = _judge("read_file", "/workspace/app/x", remote=False)
    # On POSIX the gateway's own reading is the same path; on Windows it is
    # C:\workspace\app\x, which the POSIX pattern never matched and still does not.
    gateway_is_posix = reading == "this-os" and os.path is posixpath
    assert _denied(local) is gateway_is_posix


def test_the_tag_reaches_governance_through_the_gate_unchanged(monkeypatch):
    seen = []
    real = gate_decision

    def spy(*args, **kwargs):
        seen.append(kwargs.get("raw_params"))
        return real(*args, **kwargs)

    monkeypatch.setattr("kiro_crew.platform.governance.gate_decision", spy)
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/nowhere/**"]})
    _judge("read_file", "x")
    assert seen and isinstance(seen[-1], GuestPathParams) and seen[-1].root == ROOT


# ── the tag cannot be forged: only the runtime's own binding sets it ─────────


#: Shapes a model (or a guest echoing one) could put in the arguments to ask for
#: the guest reading. None is the tag: the tag is a Python type, not a key.
FORGED = [
    {"path": "C:\\work\\secret\\k", "root": ROOT},
    {"path": "C:\\work\\secret\\k", "_guest": True, "__class__": "GuestPathParams"},
    {"path": "C:\\work\\secret\\k", "remote_workspace_root": ROOT},
]


@pytest.mark.parametrize("params", FORGED)
def test_a_forged_marker_on_a_local_call_keeps_the_windows_reading(monkeypatch, params):
    _windows_reading(monkeypatch)
    _policy(monkeypatch, write={"mode": "deny", "deny": ["C:\\work\\secret\\*"]})
    for remote_server in ("", "workspace"):
        # A local session (no attached workspace), even through a server that is
        # called ``workspace``: the arguments stay a plain dict.
        event = _event("write_file", "edit", params, remote=False, server=remote_server or "srv")
        assert type(event.raw_tool_params) is dict
        call = ToolCall.from_event(event)
        result = hooks.HookManager().on_tool_call(
            event.title, session_key="chat-local", **call.kwargs()
        )
        assert _denied(result)


def test_a_tagged_object_reaching_a_local_call_is_stripped(monkeypatch):
    # Belt for the in-process case: a GuestPathParams already sitting in the
    # tool_call cache of a session with no attached workspace is not honoured.
    _windows_reading(monkeypatch)
    _policy(monkeypatch, write={"mode": "deny", "deny": ["C:\\work\\secret\\*"]})
    tool_call_id = "tc-1"
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": "@srv/write_file",
                    "kind": "edit",
                },
                "options": [{"optionId": "a", "kind": "allow_once"}],
            },
        ),
        raw_params_cache={
            tool_call_id: GuestPathParams({"path": "C:\\work\\secret\\k"}, root=ROOT)
        },
        shell_cache={tool_call_id: False},
        mcp_server_name_cache={tool_call_id: "srv"},
        tool_name_cache={tool_call_id: "write_file"},
    )
    assert event is not None and type(event.raw_tool_params) is dict
    call = ToolCall.from_event(event)
    result = hooks.HookManager().on_tool_call(
        event.title, session_key="chat-local", **call.kwargs()
    )
    assert _denied(result)


@pytest.mark.parametrize(
    "path",
    ["C:\\work\\x", "C:/work/x", "\\\\server\\share\\x"],
)
def test_a_remote_call_cannot_reach_a_gateway_path(monkeypatch, reading, path):
    # A gateway-shaped spelling in a remote call names a file in the GUEST (the guest
    # joins it to its workdir as written), so a gateway allow rule cannot admit it.
    _policy(monkeypatch, write={"mode": "allow", "allow": ["C:\\work\\*", "C:/work/*"]})
    assert _denied(_judge("write_file", path, kind="edit"))


# ── local matching on a Windows gateway is unchanged ─────────────────────────


@pytest.mark.parametrize(
    "item, pattern, expected",
    [
        ("C:\\work\\secret\\k", "C:\\work\\secret\\*", True),
        ("C:\\work\\x\\..\\secret\\k", "C:\\work\\secret\\*", True),
        ("C:\\work\\secret\\k", "c:\\work\\secret\\*", False),  # case-sensitive, as on main
        ("C:/work/secret/k", "C:\\work\\secret\\*", True),  # ntpath folds / to \\
        ("/workspace/app/x", "/workspace/app/**", False),  # a POSIX pattern never met it
    ],
)
def test_local_windows_matching_is_unchanged(monkeypatch, item, pattern, expected):
    _windows_reading(monkeypatch)
    assert matching._match_path(item, pattern) is expected
    # The same text as a guest target is read as POSIX instead.
    assert matching._match_path(GuestPath(item, ROOT), pattern) is (
        fnmatch_posix(GuestPath(item, ROOT), pattern)
    )


def fnmatch_posix(item: GuestPath, pattern: str) -> bool:
    import fnmatch

    norm = guest_norm(item)
    return norm is not None and fnmatch.fnmatchcase(norm, pattern)


@pytest.mark.skipif(os.name != "nt", reason="the real Windows gateway reading")
def test_local_matching_on_this_windows_gateway_still_uses_its_os_path(tmp_path):
    secret = str(tmp_path / "secret" / "k")
    assert matching._match_path(secret, str(tmp_path / "secret" / "*"))
    assert matching._match_path("/workspace/app/x", "/workspace/app/**") is False
    assert matching._match_path(GuestPath("/workspace/app/x", ROOT), "/workspace/app/**")


@pytest.mark.skipif(os.name == "nt", reason="the POSIX gateway reading")
@pytest.mark.parametrize(
    "path", ["/workspace/app/x", "/workspace/app/a/../b", "/workspace/other/x"]
)
def test_on_a_posix_gateway_a_guest_absolute_path_reads_as_it_did(path):
    pattern = "/workspace/app/**"
    assert matching._match_path(GuestPath(path, ROOT), pattern) is matching._match_path(
        path, pattern
    )


# ── the pieces ────────────────────────────────────────────────────────────────


def test_classify_mints_guest_items_only_for_tagged_arguments():
    tagged = classify_tool_args("read", GuestPathParams({"path": "/workspace/app/x"}, root=ROOT))
    assert tagged == (("filesystem.read", GuestPath("/workspace/app/x", ROOT)),)
    assert all(isinstance(item, GuestPath) for _, item in tagged)
    plain = classify_tool_args("read", {"path": "/workspace/app/x"})
    assert plain == (("filesystem.read", "/workspace/app/x"),)
    assert not any(isinstance(item, GuestPath) for _, item in plain)
    # An unverifiable spelling keeps its literal pair and adds the marker beside it.
    odd = classify_tool_args("edit", GuestPathParams({"path": "$X/y"}, root=ROOT))
    assert [type(item) for _, item in odd] == [GuestPath, str]


def test_a_guest_item_never_dedupes_into_a_plain_twin():
    # gate_decision dedupes title- and argument-derived pairs; a plain twin kept
    # first must not swallow the guest item and its POSIX reading.
    assert GuestPath("/a", ROOT) != "/a" and "/a" != GuestPath("/a", ROOT)
    pairs = [("s", "/a"), ("s", GuestPath("/a", ROOT))]
    kept = list(dict.fromkeys(pairs))
    assert len(kept) == 2 and isinstance(kept[1][1], GuestPath)


@pytest.mark.parametrize(
    "item, root, norm",
    [
        ("x", ROOT, "/workspace/app/x"),
        ("../x", ROOT, "/workspace/x"),
        ("//etc/passwd", ROOT, "/etc/passwd"),
        ("///etc/passwd", ROOT, "/etc/passwd"),
        ("/a/./b//c/..", ROOT, "/a/b"),
        ("x", "", None),  # no workdir: never anchored at this gateway's cwd
        ("x", "relative/root", None),
    ],
)
def test_guest_norm(item, root, norm):
    assert guest_norm(GuestPath(item, root)) == norm


@pytest.mark.parametrize(
    "item, unverifiable",
    [
        ("/workspace/app/x", False),
        ("/workspace/app/a~b", False),
        ("/workspace/app/~/x", True),
        ("/workspace/app/~user", True),
        ("/workspace/app/$X", True),
        ("/workspace/app\\x", True),
        ("rel", False),
    ],
)
def test_guest_unverifiable(item, unverifiable):
    assert guest_unverifiable(GuestPath(item, ROOT)) is unverifiable
    assert guest_unverifiable(GuestPath("rel", "")) is True


def test_a_remote_workspace_attaches_on_any_gateway_os():
    # The Windows refusal (REMOTE_NEEDS_POSIX_GATEWAY) is gone: this runs on the
    # Windows shards with nothing patched.
    runtime = SimpleNamespace(acp_backend=ACP_BACKEND_KAS, _process=None)
    AcpRuntime.check_remote_workspace_supported(runtime)
    assert not hasattr(se, "REMOTE_NEEDS_POSIX_GATEWAY")
    assert not hasattr(se, "gateway_matches_posix_paths")


# ── calls a remote session cannot place for certain get both readings ────────


def _remote_frame(tool, params, *, server="workspace", trusted=True, title=None):
    tool_call_id = "tc-1"
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": title or f"@{server}/{tool}",
                    "kind": "read",
                },
                "options": [{"optionId": "a", "kind": "allow_once"}],
            },
        ),
        raw_params_cache={tool_call_id: dict(params)},
        shell_cache={tool_call_id: False},
        mcp_server_name_cache={tool_call_id: server} if trusted else None,
        tool_name_cache={tool_call_id: tool} if trusted else None,
        remote_workspace_server=se.WORKSPACE_SERVER_NAME,
        remote_workspace_root=ROOT,
    )
    assert event is not None
    return event


def _gate(event):
    call = ToolCall.from_event(event)
    return hooks.HookManager().on_tool_call(event.title, session_key="chat-remote", **call.kwargs())


@pytest.mark.parametrize("trusted, tool", [(True, "read_many"), (False, "read_file")])
def test_an_unplaced_remote_call_must_pass_both_readings(monkeypatch, reading, trusted, tool):
    event = _remote_frame(tool, {"path": "/workspace/app/secret/k"}, trusted=trusted)
    assert isinstance(event.raw_tool_params, GuestPathParams)
    assert event.raw_tool_params.also_local
    # The guest reading binds a deny the gateway reading would miss on Windows ...
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/workspace/app/secret/**"]})
    assert _denied(_gate(event))
    # ... and an allow admits only what BOTH readings admit.
    _policy(monkeypatch, read={"mode": "allow", "allow": ["/workspace/app/**"]})
    assert _denied(_gate(event)) is (reading == "windows" or os.path is not posixpath)


def test_a_trusted_non_workspace_call_on_a_remote_session_keeps_the_gateway_reading():
    event = _remote_frame("file_send", {"path": "/srv/report.txt"}, server="kirocrew-core")
    assert type(event.raw_tool_params) is dict


def test_a_reading_title_on_a_remote_call_is_read_as_the_guest_path(monkeypatch, reading):
    _policy(monkeypatch, read={"mode": "allow", "allow": ["/workspace/app/**"]})
    pairs_title = "Reading /workspace/app/x"
    params = GuestPathParams({"path": "/workspace/app/x"}, root=ROOT)
    ceiling = hooks.current_context().governance
    assert gate_decision(ceiling, None, pairs_title, tool_kind="read", raw_params=params).permitted
    _policy(monkeypatch, read={"mode": "deny", "deny": ["/workspace/app/**"]})
    ceiling = hooks.current_context().governance
    decision = gate_decision(ceiling, None, pairs_title, tool_kind="read", raw_params=params)
    assert not decision.permitted


@pytest.mark.parametrize(
    "diff_path, anchored",
    [("/workspace/app/x", True), ("x", False), ("~/x", False), ("$HOME/x", False)],
)
def test_a_remote_diff_block_path_is_anchored_as_the_guest_reads_it(diff_path, anchored):
    from kiro_crew.platform.tool_paths import edit_target_candidates

    found = edit_target_candidates(GuestPathParams({}, root=ROOT), diff_path, tool_kind="edit")
    assert found.unanchored is not anchored
    assert (diff_path in found) is anchored

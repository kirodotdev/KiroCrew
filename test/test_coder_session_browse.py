"""Browse and terminal targets must still match the saved Coder identity."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import session_browse, session_launch
from kiro_crew.coder.client import CoderClient, CoderClientError
from kiro_crew.config import live, paths
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


pytestmark = pytest.mark.asyncio

SESSION = "dashboard:remote"
URL = "https://coder.example"
# PEM framing for a synthetic key fixture, assembled the way the suite's other
# credential tests build it; the body it wraps is never a real key.
_PEM_DASHES = "-" * 5
_PEM_BEGIN_RSA = f"{_PEM_DASHES}BEGIN RSA PRIVATE KEY{_PEM_DASHES}"
_PEM_END_RSA = f"{_PEM_DASHES}END RSA PRIVATE KEY{_PEM_DASHES}"


def ssh_argv(name):
    return ["coder", "--url", URL, "ssh", "--disable-autostart", name]


@pytest.fixture
def browse_target(tmp_path, monkeypatch):
    config = SimpleNamespace(
        coder_url=URL,
        profiles=[SimpleNamespace(id="dev", template="micro", workdir="/workspace/app")],
    )
    provider = session_launch.CoderSessionEnvironments(config, tmp_path)
    provider.choices.set(SESSION, "dev")
    binding = provider.registry.allocate(
        SESSION, template="micro", preset="", prefix="crewenv", owner_name="dev", deployment_id=URL
    )
    provider.registry.replace(
        replace(binding, workspace_uuid="workspace-id", owner_id="owner-id", state="running")
    )
    record = {
        "id": "workspace-id",
        "name": binding.workspace_name,
        "owner_id": "owner-id",
        "template_name": "micro",
        "status": "running",
    }

    def answer(argv, *_):
        if "whoami" in argv:
            return json.dumps([{"username": "dev", "user_id": "owner-id"}]).encode()
        return json.dumps([record]).encode()

    runner = AsyncMock(side_effect=answer)
    client = CoderClient("coder", URL, "", tmp_path, runner=runner)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=config))
    monkeypatch.setattr(session_launch, "resolve_coder_bin", lambda: "coder")
    monkeypatch.setattr(session_launch.CoderSessionEnvironments, "_client", lambda self: client)
    guest = AsyncMock(return_value=b'{"ok": true}')
    monkeypatch.setattr(session_browse, "run_collect", guest)
    return config, record, runner, guest


async def _access(operation):
    if operation == "browse":
        return await session_browse.browse_session(SESSION, "list", ".")
    if operation == "write":
        return await session_browse.write_session_file(SESSION, "a.txt", "new", "old")
    return await session_browse.remote_terminal_argv(SESSION)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["browse", "write", "terminal"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "replacement"), ("owner_id", "another-owner"), ("template_name", "another-template")],
)
async def test_rebound_workspace_never_executes(browse_target, operation, field, value):
    _config, record, runner, guest = browse_target
    record[field] = value
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _access(operation)
    assert runner.call_args_list[0].args[0] == ["coder", "list", "--output", "json"]
    guest.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["browse", "write", "terminal"])
async def test_original_workspace_is_accessible_with_one_read(
    browse_target, operation, monkeypatch
):
    monkeypatch.setenv("CODER_URL", "https://unverified-coder.example")
    _config, record, runner, guest = browse_target
    result = await _access(operation)
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    if operation == "terminal":
        assert result == ssh_argv(record["name"])
        guest.assert_not_awaited()
    else:
        assert result == {"ok": True}
        assert guest.call_args.args[0][:6] == ssh_argv(record["name"])


async def test_a_write_under_another_working_folder_never_executes(browse_target):
    """The folder is checked against the target THIS write resolved, so a profile
    re-pointed between any earlier read and the write cannot redirect it."""
    config, _record, _runner, guest = browse_target
    with pytest.raises(session_browse.WorkdirChanged):
        await session_browse.write_session_file(
            SESSION, "a.txt", "new", "old", workdir="/workspace/old"
        )
    guest.assert_not_awaited()
    assert await session_browse.write_session_file(
        SESSION, "a.txt", "new", "old", workdir="/workspace/app"
    ) == {"ok": True}
    guest.assert_awaited_once()


async def test_the_coder_binary_is_resolved_off_the_loop_once(browse_target, monkeypatch):
    """The PATH search can stall on a slow mount, so it never runs on the loop,
    and the shell that runs reuses the one path it found."""
    import threading

    loop_thread = threading.current_thread()
    seen: list[bool] = []

    def resolve():
        seen.append(threading.current_thread() is not loop_thread)
        return "coder"

    monkeypatch.setattr(session_launch, "resolve_coder_bin", resolve)
    assert await session_browse.remote_terminal_argv(SESSION) == ssh_argv(browse_target[1]["name"])
    assert seen == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["browse", "write", "terminal"])
@pytest.mark.parametrize("owner", ["dev", "someone-else"])
async def test_a_record_without_owner_id_is_matched_by_owner_name(browse_target, operation, owner):
    # Older Coder omits owner_id in `coder list`; the record then names the owner.
    _config, record, _runner, guest = browse_target
    del record["owner_id"]
    record["owner_name"] = owner
    if owner == "dev":
        await _access(operation)
    else:
        with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
            await _access(operation)
        guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_workspace_is_not_recreated(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = None
    runner.return_value = b"[]"
    with pytest.raises(SessionEnvironmentUnavailable, match="identity has changed"):
        await _access("browse")
    runner.assert_awaited_once()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_identity_lookup_never_executes(browse_target):
    _config, _record, runner, guest = browse_target
    runner.side_effect = CoderClientError("offline")
    with pytest.raises(SessionEnvironmentUnavailable, match="could not verify"):
        await _access("write")
    runner.assert_awaited_once()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_deployment_is_refused_before_lookup(browse_target):
    config, _record, runner, guest = browse_target
    config.coder_url = "https://other-coder.example"
    with pytest.raises(SessionEnvironmentUnavailable, match="deployment has changed"):
        await _access("terminal")
    runner.assert_not_awaited()
    guest.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_workspace_is_not_started(browse_target):
    _config, record, runner, guest = browse_target
    record["status"] = "stopped"
    result = await _access("terminal")
    assert result == ssh_argv(record["name"])
    runner.assert_awaited_once()
    assert runner.call_args.args[0] == ["coder", "list", "--output", "json"]
    guest.assert_not_awaited()


@pytest.mark.parametrize(
    "first,installs",
    [
        # Guest files from before list/read served only git_status, and say so
        # in a normal reply rather than failing.
        (b'{"error": "op must be git_status"}', 1),
        # A current guest's own refusal of a path is an answer, not old files.
        (b'{"error": "path is outside the root"}', 0),
    ],
)
async def test_browse_reinstalls_outdated_guest_files_once(
    browse_target, monkeypatch, first, installs
):
    _config, _record, _runner, guest = browse_target
    guest.side_effect = [first, b'{"entries": []}']
    install = AsyncMock()
    monkeypatch.setattr(
        session_browse, "RemoteWorkerLaunch", lambda *_: SimpleNamespace(install=install)
    )
    result = await session_browse.browse_session(SESSION, "list", ".")
    assert install.await_count == installs
    assert guest.await_count == 1 + installs
    assert result == (json.loads(first) if not installs else {"entries": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["git_log", "git_diff"])
async def test_a_worker_without_the_read_op_is_reinstalled_once(browse_target, monkeypatch, op):
    _config, _record, _runner, guest = browse_target
    stale = b'{"error": "op must be list, read, git_status, git_log or git_diff"}'
    reply = {"root": "/w", "git": True, "output": ""}
    if op == "git_diff":
        reply = {"root": "/w", "git": True, "old": "same", "new": "same"}
    guest.side_effect = [stale, json.dumps(reply).encode()]
    install = AsyncMock()
    monkeypatch.setattr(session_browse.RemoteWorkerLaunch, "install", install)
    result = await session_browse.browse_session(SESSION, op, "a.txt")
    assert result == {"root": "/w", "git": True, "output": ""}
    install.assert_awaited_once()
    assert guest.await_count == 2
    assert all(
        json.loads(call.args[1])["op"] == ("git_bodies" if op == "git_diff" else op)
        for call in guest.call_args_list
    )


@pytest.mark.asyncio
async def test_a_refused_op_after_reinstalling_is_returned_not_retried(browse_target, monkeypatch):
    _config, _record, _runner, guest = browse_target
    guest.return_value = b'{"error": "op must be list, read or git_status"}'
    install = AsyncMock()
    monkeypatch.setattr(session_browse.RemoteWorkerLaunch, "install", install)
    result = await session_browse.browse_session(SESSION, "git_diff", "a.txt")
    assert result == {"error": "op must be list, read or git_status"}
    install.assert_awaited_once()
    assert guest.await_count == 2


@pytest.mark.asyncio
async def test_other_browse_errors_do_not_reinstall(browse_target, monkeypatch):
    _config, _record, _runner, guest = browse_target
    guest.return_value = b'{"error": "path \'../x\' is outside the workspace /w"}'
    install = AsyncMock()
    monkeypatch.setattr(session_browse.RemoteWorkerLaunch, "install", install)
    result = await session_browse.browse_session(SESSION, "read", "../x")
    assert "outside the workspace" in result["error"]
    install.assert_not_awaited()
    guest.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["git_status", "git_log", "git_diff"])
@pytest.mark.parametrize(
    ("sensitive", "replacement"),
    [
        ("AKIAIOSFODNN7EXAMPLE", "[REDACTED: credential]"),
        ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "[REDACTED: credential]"),
        ("?token=opaque-example", "?token=[REDACTED: credential]"),
        (
            "https://example.com/?data=" + "a" * 300,
            "[REDACTED: suspicious URL to example.com]",
        ),
    ],
)
async def test_git_browse_redacts_output_and_errors_with_framing(
    browse_target, op, sensitive, replacement
):
    _config, _record, _runner, guest = browse_target
    outputs = {
        "git_status": (
            f"## topic...origin/topic [ahead 2]\0R  {sensitive}\0old.txt\0?? keep\nname.txt\0"
        ),
        "git_log": f"abc1234\tDev\t1760850000\tadd {sensitive}\nabc1235\tDev\t1760936400\tclean",
        "git_diff": (
            "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
            f"@@ -1,2 +1,2 @@\n context\n-old\n+{sensitive}\n"
        ),
    }
    output = outputs[op]
    error_text = f"fatal: {sensitive}\ntry again\0"
    # A truncated diff reply builds no hunks, so the diff case is a complete one.
    raw = {"git": True, "output": output, "error_text": error_text, "truncated": op != "git_diff"}
    wire = raw.copy()
    if op == "git_diff":
        wire.pop("output")
        wire.update(path="a.txt", old="context\nold\n", new=f"context\n{sensitive}\n")
    guest.return_value = json.dumps(wire).encode()

    result = await session_browse.browse_session(SESSION, op, "a.txt")

    assert result == {
        **raw,
        "output": output.replace(sensitive, replacement),
        "error_text": error_text.replace(sensitive, replacement),
    }
    for field in ("output", "error_text"):
        assert sensitive not in result[field]
        for separator in ("\0", "\n"):
            assert result[field].count(separator) == raw[field].count(separator)
    if op == "git_status":
        assert session_browse.summarize_unsaved(result["output"]) == (
            "2 uncommitted files, 2 unpushed commits"
        )
    assert json.loads(guest.call_args.args[1])["op"] == ("git_bodies" if op == "git_diff" else op)
    guest.assert_awaited_once()


@pytest.mark.asyncio
async def test_git_browse_redacts_failed_reply(browse_target):
    _config, _record, _runner, guest = browse_target
    guest.return_value = json.dumps(
        {"git": False, "code": "git_failed", "error_text": "fatal: AKIAIOSFODNN7EXAMPLE"}
    ).encode()
    result = await session_browse.browse_session(SESSION, "git_log", ".")
    assert result == {
        "git": False,
        "code": "git_failed",
        "error_text": "fatal: [REDACTED: credential]",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject", "shown"),
    [
        ("fix Authorization: Bearer", "fix Authorization: Bearer"),
        ("fix Authorization: Bearer opaque-example", "fix [REDACTED: credential]"),
    ],
)
async def test_git_log_credential_masks_only_its_own_line(browse_target, subject, shown):
    _config, _record, _runner, guest = browse_target
    first = "abc1234\tDev\t1760850000\tfirst"
    third = "abc1236\tDev\t1761022800\tthird"
    output = f"{first}\nabc1235\tDev\t1760936400\t{subject}\n{third}"
    guest.return_value = json.dumps({"git": True, "output": output}).encode()

    result = await session_browse.browse_session(SESSION, "git_log", ".")

    assert result["output"].split("\n") == [
        first,
        f"abc1235\tDev\t1760936400\t{shown}",
        third,
    ]


@pytest.mark.asyncio
async def test_nul_status_record_with_multiline_credential_is_withheld(browse_target):
    _config, _record, _runner, guest = browse_target
    output = "## main\0?? Authorization: Bearer\nopaque-example\0?? keep.txt\0"
    guest.return_value = json.dumps({"git": True, "output": output}).encode()

    result = await session_browse.browse_session(SESSION, "git_status", ".")

    assert result["output"] == (
        "## main\0[REDACTED: credential]\n[REDACTED: credential]\0?? keep.txt\0"
    )


@pytest.mark.asyncio
async def test_git_diff_multiline_credential_keeps_patch_framing(browse_target):
    _config, _record, _runner, guest = browse_target
    guest.return_value = json.dumps(
        {
            "git": True,
            "path": "key",
            "old": None,
            "new": f"{_PEM_BEGIN_RSA}\nQUJD\n{_PEM_END_RSA}\n",
        }
    ).encode()
    result = await session_browse.browse_session(SESSION, "git_diff", "key")
    assert result == {
        "git": True,
        "output": (
            "diff --git a/key b/key\n--- /dev/null\n+++ b/key\n@@ -0,0 +1 @@\n"
            "+[REDACTED: credential]\n"
        ),
    }
    assert "QUJD" not in str(result)


@pytest.mark.asyncio
async def test_git_diff_forwards_a_rename_source_and_names_it(browse_target):
    _config, _record, _runner, guest = browse_target
    guest.return_value = json.dumps(
        {
            "git": True,
            "path": "new.txt",
            "old_path": "old.txt",
            "old_mode": "100644",
            "new_mode": "100755",
            "old": "same\n",
            "new": "same\n",
        }
    ).encode()
    result = await session_browse.browse_session(SESSION, "git_diff", "new.txt", "old.txt")
    assert json.loads(guest.call_args.args[1]) == {
        "op": "git_bodies",
        "path": "new.txt",
        "from": "old.txt",
    }
    assert result["output"] == (
        "diff --git a/old.txt b/new.txt\nold mode 100644\nnew mode 100755\n"
        "rename from old.txt\nrename to new.txt\n"
    )
    await session_browse.browse_session(SESSION, "git_status", ".", "old.txt")
    assert "from" not in json.loads(guest.call_args.args[1])
    with pytest.raises(ValueError, match="path is invalid"):
        await session_browse.browse_session(SESSION, "git_diff", "new.txt", "a\0b")


@pytest.mark.asyncio
async def test_read_browse_is_not_redacted(browse_target):
    _config, _record, _runner, guest = browse_target
    raw = {
        "content": "AKIAIOSFODNN7EXAMPLE\n?token=opaque-example\0",
        "output": "AKIAIOSFODNN7EXAMPLE",
        "error_text": "AKIAIOSFODNN7EXAMPLE",
    }
    guest.return_value = json.dumps(raw).encode()
    assert await session_browse.browse_session(SESSION, "read", "a.txt") == raw


@pytest.mark.asyncio
async def test_unsaved_work_counts_raw_guest_records(browse_target, monkeypatch):
    _config, _record, _runner, guest = browse_target
    raw = "## topic...origin/topic [ahead 2]\0R  ?token=opaque\0old.txt\0?? keep\nname.txt\0"
    guest.return_value = json.dumps({"git": True, "output": raw}).encode()
    measured = []
    summarize = session_browse.summarize_unsaved

    def capture(output, **kwargs):
        measured.append(output)
        return summarize(output, **kwargs)

    monkeypatch.setattr(session_browse, "summarize_unsaved", capture)
    assert await session_browse.unsaved_work(SESSION) == "2 uncommitted files, 2 unpushed commits"
    assert measured == [raw]


@pytest.mark.parametrize(
    "output",
    [
        "## topic...origin/topic [ahead 2]\0 M a.py\0R  new.py\0old.py\0?? b.txt\0?? partial/na",
        "## topic...origin/topic [ahead 2]\n M a.py\nR  old.py -> new.py\n?? b.txt\n?? partial/na",
    ],
    ids=["nul", "newline"],
)
def test_unsaved_summary_of_cut_status_drops_partial_record_and_is_a_lower_bound(output):
    assert session_browse.summarize_unsaved(output, truncated=True) == (
        "at least 3 uncommitted files, 2 unpushed commits"
    )
    # The same bytes read as complete count the last record and keep the exact wording.
    assert session_browse.summarize_unsaved(output) == "4 uncommitted files, 2 unpushed commits"


@pytest.mark.asyncio
async def test_unsaved_work_passes_the_guest_cut_flag(browse_target):
    _config, _record, _runner, guest = browse_target
    cut = "## topic...origin/topic\0 M a.py\0?? b.txt\0?? partial/na"
    guest.return_value = json.dumps({"git": True, "output": cut, "truncated": True}).encode()
    assert await session_browse.unsaved_work(SESSION) == "at least 2 uncommitted files"
    guest.return_value = json.dumps({"git": True, "output": cut, "truncated": False}).encode()
    assert await session_browse.unsaved_work(SESSION) == "3 uncommitted files"


@pytest.mark.parametrize("side", ["old", "new"])
@pytest.mark.parametrize("label", ["RSA PRIVATE KEY", "CUSTOM-LABEL"])
def test_unclosed_pem_is_withheld_in_a_complete_body(side, label):
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    body = f"visible\n\t-----BEGIN {label}-----\n    YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4\nlast"
    reply = {"git": True, "old": None, "new": None, side: body}
    output = session_browse._git_diff_from_bodies(reply, "f")["output"]
    sign = "-" if side == "old" else "+"
    assert sign + "visible\n" in output
    assert output.endswith(
        (sign + REDACTED_CREDENTIAL_TAG + "\n") * 3 + "\\ No newline at end of file\n"
    )
    assert "YWJjZGVm" not in output and "last" not in output


@pytest.mark.parametrize("footer", ["-----END CERTIFICATE-----", "-----END PRIVATE KEY-----"])
def test_only_a_matching_footer_after_a_pem_begin_closes_it(footer):
    from kiro_crew.security import REDACTED_CREDENTIAL_TAG

    body = "-----END CUSTOM LABEL-----\n  -----BEGIN CUSTOM LABEL-----\n  opaque\n" + footer + "\n"
    reply = {"git": True, "old": None, "new": body}
    output = session_browse._git_diff_from_bodies(reply, "f")["output"]
    assert output.endswith(("+" + REDACTED_CREDENTIAL_TAG + "\n") * 3)
    assert "opaque" not in output


@pytest.mark.parametrize("renamed", [False, True])
def test_many_short_repeated_lines_have_a_bounded_diff(renamed):
    import time

    reply = {
        "git": True,
        "old": "a\n" * 65536,
        "new": "a\n" * 65535 + "b\n",
        "old_mode": "100644",
        "new_mode": "100755",
    }
    if renamed:
        reply["old_path"] = "before"
    started = time.monotonic()
    result = session_browse._git_diff_from_bodies(reply, "after")
    elapsed = time.monotonic() - started
    print(f"repeated-line diff {renamed=}: {elapsed:.6f}s")
    assert elapsed < 2  # flake-ok: explicit two-second complexity budget for 64K repeated lines
    assert not result.get("truncated")
    assert "old mode 100644\nnew mode 100755\n" in result["output"]
    assert "-a\n+b\n" in result["output"]
    assert ("rename from before\nrename to after\n" in result["output"]) == renamed

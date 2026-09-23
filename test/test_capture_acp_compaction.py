"""``scripts/capture_acp_compaction.py`` -- the parts that need no credential.

The script's REASON for existing is a live drive, and a live drive needs a harness and
a model this suite has neither of. What it can hold is everything around the drive, and
that is where a capture goes wrong in practice: a verdict read off the wrong turn, a
candidate written where CI collects it, a missing harness reported as a measurement, a
frame committed with the recording host's home path still in it.

The drive itself is covered too, against a FAKE agent that speaks ACP over stdio and
reports a context that falls. That fake asserts nothing about goose or pi -- it asserts
that this script, handed a harness whose ``used`` drops, says COMPACTED and writes the
frames that show it.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "capture_acp_compaction.py"


def _load():
    spec = importlib.util.spec_from_file_location("capture_acp_compaction", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the script defines dataclasses, and ``@dataclass``
    # resolves its own annotations through ``sys.modules[cls.__module__]``, which is
    # None for a module loaded by path and never registered.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cap():
    return _load()


def _turn(cap, kind, used, stop="end_turn"):
    turn = cap.Turn(kind=kind, prompt="p")
    turn.used = list(used)
    turn.stop_reason = stop
    return turn


# ── arguments ───────────────────────────────────────────────────────────────────


def test_defaults_match_the_opencode_drive(cap):
    args = cap.parse_args(["--harness", "goose"])
    assert args.harness == "goose"
    assert args.turns == 4
    assert args.keep_ids is False
    assert args.out is None
    assert "{n}" in args.prompt


def test_unknown_harness_is_a_usage_error_naming_the_supported_ids(cap, capsys):
    assert cap.main(["--harness", "claude"]) == cap.EXIT_USAGE
    err = capsys.readouterr().err
    assert "not supported" in err
    assert "goose" in err and "pi" in err


def test_zero_turns_is_refused(cap, capsys):
    assert cap.main(["--harness", "goose", "--turns", "0"]) == cap.EXIT_USAGE
    assert "--turns must be at least 1" in capsys.readouterr().err


def test_supported_harnesses_are_the_ones_this_argv_shape_fits(cap):
    supported = cap.supported_harnesses()
    # Every harness whose whole launch is a table row, plus pi.
    assert {"goose", "pi", "opencode", "deepseek"} <= set(supported)
    # The bespoke-argv harnesses are deliberately absent: an agent spec, a relay and a
    # vendored entry are not this spawn.
    assert not {"kiro", "kas", "claude", "codex"} & set(supported)


# ── where a capture lands ───────────────────────────────────────────────────────


def test_default_output_is_outside_the_collected_corpus(cap):
    """A candidate inside ``test/fixtures/acp_frames/`` would go red on sight.

    ``test/test_acp_frame_replay.py`` collects every ``*.jsonl`` under that tree and
    requires a snapshot beside it, so writing a candidate there breaks the suite for
    everyone until it is moved. The default must be somewhere git-ignored.
    """
    out = cap.default_out("goose")
    corpus = REPO_ROOT / "test" / "fixtures" / "acp_frames"
    assert corpus not in out.parents
    assert (REPO_ROOT / "build") in out.parents
    assert out.name == "compact-live.jsonl"


# ── measurement ─────────────────────────────────────────────────────────────────


def test_used_series_reads_only_usage_updates(cap):
    frames = [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}},
        {"params": {"update": {"sessionUpdate": "agent_message_chunk"}}},
        {"params": {"update": {"sessionUpdate": "usage_update", "used": 14863}}},
        {"params": {"update": {"sessionUpdate": "usage_update", "used": 15727.0}}},
        {"params": {"update": {"sessionUpdate": "usage_update"}}},
        {"params": {"update": {"sessionUpdate": "usage_update", "used": True}}},
        {"params": "not-a-dict"},
    ]
    assert cap.used_series(frames) == [14863, 15727]


def test_verdict_is_compacted_when_the_later_turn_reads_below_the_peak(cap):
    turns = [
        _turn(cap, "ordinary", [14863]),
        _turn(cap, "ordinary", [17478]),
        _turn(cap, "compact", [514]),
        _turn(cap, "ordinary", [14577]),
    ]
    measured = cap.measure(turns)
    assert measured.verdict == cap.VERDICT_COMPACTED
    assert measured.pre_peak == 17478
    assert measured.compact_turn_used == 514
    assert measured.after == 14577


def test_verdict_is_not_compacted_when_the_later_turn_reads_at_or_above_the_peak(cap):
    turns = [
        _turn(cap, "ordinary", [9000]),
        _turn(cap, "compact", [9000]),
        _turn(cap, "ordinary", [9200]),
    ]
    measured = cap.measure(turns)
    assert measured.verdict == cap.VERDICT_NOT_COMPACTED
    assert "not below" in measured.reason


def test_the_peak_ignores_the_compact_turns_own_reading(cap):
    """A number read DURING a compaction is the harness mid-summary, not a peak.

    Were the compact turn's own low reading allowed into the peak, a harness that
    compacted nothing would still pass: the peak would be the small number and any
    later reading would sit above it, which inverts the test.
    """
    turns = [
        _turn(cap, "ordinary", [8000]),
        _turn(cap, "compact", [40]),
        _turn(cap, "ordinary", [7000]),
    ]
    assert cap.measure(turns).pre_peak == 8000
    assert cap.measure(turns).verdict == cap.VERDICT_COMPACTED


def test_no_usage_frames_is_unproven_rather_than_a_pass(cap):
    turns = [
        _turn(cap, "ordinary", []),
        _turn(cap, "compact", []),
        _turn(cap, "ordinary", []),
    ]
    measured = cap.measure(turns)
    assert measured.verdict == cap.VERDICT_UNPROVEN
    assert "does not report" in measured.reason


def test_a_silent_turn_after_compact_is_unproven(cap):
    turns = [
        _turn(cap, "ordinary", [8000]),
        _turn(cap, "compact", [40]),
        _turn(cap, "ordinary", []),
    ]
    measured = cap.measure(turns)
    assert measured.verdict == cap.VERDICT_UNPROVEN
    assert "carried forward" in measured.reason


# ── reduction ───────────────────────────────────────────────────────────────────


def test_reduction_drops_host_inventories_and_synthesizes_ids(cap, tmp_path):
    frames = [
        {"params": {"update": {"sessionUpdate": "available_commands_update", "x": 1}}},
        {"params": {"update": {"sessionUpdate": "session_info_update", "x": 1}}},
        {"params": {"update": {"sessionUpdate": "agent_thought_chunk", "x": 1}}},
        {
            "params": {
                "sessionId": "ses_realid42",
                "update": {"sessionUpdate": "usage_update", "used": 7},
            }
        },
        {"params": {"permissionId": "0f9e1b22-1111-2222-3333-444455556666"}},
    ]
    reduced, notes = cap.reduce_frames(
        frames,
        backend="goose",
        session_id="ses_realid42",
        cwd=tmp_path,
        keep_ids=False,
    )
    blob = json.dumps(reduced)
    assert "available_commands_update" not in blob
    assert "session_info_update" not in blob
    assert "agent_thought_chunk" not in blob
    assert "ses_realid42" not in blob
    assert "goose-session-1" in blob
    assert "0f9e1b22" not in blob
    assert "perm-1" in blob
    joined = " ".join(notes)
    assert "command inventory" in joined
    assert "agent_thought_chunk" in joined
    assert "goose-session-1" in joined


def test_keep_ids_leaves_the_real_ids_alone(cap, tmp_path):
    frames = [{"params": {"sessionId": "ses_realid42"}}]
    reduced, _notes = cap.reduce_frames(
        frames, backend="goose", session_id="ses_realid42", cwd=tmp_path, keep_ids=True
    )
    assert "ses_realid42" in json.dumps(reduced)


def test_reduction_replaces_a_windows_shaped_path(cap):
    """The substitution runs on the frame data, not on its JSON text.

    A Windows path is serialized with every separator doubled, so a replace over the
    serialized text cannot match the path the process ran in — and the host path would
    reach the fixture on the one platform whose paths the marker patterns name.
    """
    windows_cwd = pathlib.Path(r"C:\Users\Someone\scratch")
    frames = [{"params": {"cwd": str(windows_cwd), "note": f"under {windows_cwd}"}}]
    reduced, notes = cap.reduce_frames(
        frames, backend="goose", session_id="", cwd=windows_cwd, keep_ids=False
    )
    blob = json.dumps(reduced)
    assert "Someone" not in blob
    assert blob.count("<cwd>") == 2
    assert any("working directory" in note for note in notes)


def test_reduction_replaces_the_scratch_directory(cap, tmp_path):
    frames = [{"params": {"cwd": str(tmp_path)}}]
    reduced, notes = cap.reduce_frames(
        frames, backend="pi", session_id="", cwd=tmp_path, keep_ids=False
    )
    assert str(tmp_path) not in json.dumps(reduced)
    assert "<cwd>" in json.dumps(reduced)
    assert any("working directory" in note for note in notes)


# ── provenance ──────────────────────────────────────────────────────────────────


def test_meta_header_carries_the_four_required_keys_and_the_series(cap):
    turns = [
        _turn(cap, "ordinary", [100]),
        _turn(cap, "ordinary", [200]),
        _turn(cap, "compact", [10]),
        _turn(cap, "ordinary", [50]),
    ]
    header = cap.meta_header(
        backend="goose",
        agent_version="1.50.1",
        measurement=cap.measure(turns),
        turns=turns,
        notes=["the session id is replaced"],
    )["_meta"]
    assert header["backend"] == "goose"
    assert header["recorded"] == "live"
    assert header["agent_version"] == "1.50.1"
    assert header["date"]
    assert "100 -> 200" in header["note"]
    assert "COMPACTED" in header["note"]
    assert "the session id is replaced" in header["note"]


def test_header_is_shaped_the_way_the_replay_harness_reads_a_fixture(cap, tmp_path):
    """The corpus's own reader must accept what this script writes.

    Otherwise a capture is evidence nobody can add to the corpus without editing it,
    and an edited frame stops being evidence of what the wire carried.
    """
    sys.path.insert(0, str(REPO_ROOT / "test"))
    import acp_frame_replay_harness as harness

    turns = [_turn(cap, "ordinary", [5]), _turn(cap, "compact", [1]), _turn(cap, "ordinary", [2])]
    header = cap.meta_header(
        backend="goose",
        agent_version="1.50.1",
        measurement=cap.measure(turns),
        turns=turns,
        notes=[],
    )
    path = tmp_path / "compact-live.jsonl"
    path.write_text(
        json.dumps(header) + "\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n",
        encoding="utf-8",
    )
    meta, frames = harness.read_fixture(path)
    assert meta["backend"] == "goose"
    assert len(frames) == 1


def test_host_marker_sweep_uses_the_repository_gates_patterns(cap):
    assert cap.host_markers('{"cwd": "/home/someone/project"}')
    assert cap.host_markers('{"x": "availableCommands"}')
    assert cap.host_markers('{"id": "goose-session-1", "used": 14577}') == []


# ── environment reporting ───────────────────────────────────────────────────────


def test_a_missing_binary_is_reported_with_the_install_command(cap, monkeypatch, capsys):
    from kiro_crew.acp import client as acp_client

    monkeypatch.setattr(
        acp_client, "_resolve_self_served_bin", lambda backend: (None, "/usr/bin:/bin")
    )
    assert cap.main(["--harness", "goose"]) == cap.EXIT_USAGE
    err = capsys.readouterr().err
    assert "goose not found" in err
    assert "download_cli.sh" in err  # the launch record's own install command
    assert "GOOSE_BIN" in err


def test_the_sign_in_remedy_is_read_from_the_repository_not_restated(cap):
    remedy = cap.sign_in_remedy("goose")
    assert "goose configure" in remedy
    assert cap.sign_in_remedy("pi")


def test_a_permission_request_is_refused_not_approved(cap):
    """The capture client says NO, because it spawns the harness unsandboxed.

    Approving a tool call here would run it in the operator's own environment with no
    gate in front of it, and a compaction needs no tool call at all.
    """
    params = {
        "options": [
            {"optionId": "yes", "kind": "allow_once"},
            {"optionId": "always", "kind": "allow_always"},
            {"optionId": "no", "kind": "reject_once"},
        ]
    }
    assert cap._first_reject_option(params) == "no"
    # reject_once is preferred over reject_always: this client answers for THIS call.
    both = {
        "options": [
            {"optionId": "never", "kind": "reject_always"},
            {"optionId": "no", "kind": "reject_once"},
        ]
    }
    assert cap._first_reject_option(both) == "no"
    # Nothing reject-shaped on offer: no allow option is ever selected instead.
    assert cap._first_reject_option({"options": [{"optionId": "yes", "kind": "allow_once"}]}) == ""
    assert cap._first_reject_option({}) == ""


def test_a_root_home_is_never_substituted(cap, monkeypatch):
    """``HOME=/`` must not turn every separator in every frame into a tilde."""
    monkeypatch.setattr(cap.Path, "home", staticmethod(lambda: pathlib.Path("/")))
    frames = [{"jsonrpc": "2.0", "method": "session/update", "params": {"x": "a/b"}}]
    reduced, notes = cap.reduce_frames(
        frames, backend="goose", session_id="", cwd=pathlib.Path("/tmp"), keep_ids=False
    )
    assert json.dumps(reduced).count("session/update") == 1
    assert not any("home directory" in note for note in notes)


def test_a_credential_in_a_frame_is_scrubbed_before_it_is_written(cap, tmp_path):
    """The capture runs the product's own scrub, because a frame is agent-written text.

    The marker sweep reads host IDENTITY — paths, ids, catalog entries — and a secret the
    model echoed into its own reply is none of those. The scrub is the recorder's, so a
    script-driven capture is held to the same floor as one taken through the gateway.
    """
    leaked = "ghp_" + "a" * 36
    frames = [
        {
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"the key is {leaked}"},
                }
            }
        }
    ]
    reduced, notes = cap.reduce_frames(
        frames, backend="goose", session_id="", cwd=tmp_path, keep_ids=False
    )
    blob = json.dumps(reduced)
    assert leaked not in blob
    assert any("redact_text" in note for note in notes)


def test_a_filesystem_root_is_refused_as_the_working_directory(cap, capsys):
    """A root is every path's separator, so substituting it would mangle every frame.

    ``/`` replaced with ``<cwd>`` turns ``session/update`` into ``session<cwd>update``:
    the capture would be written, exit 0, and be corrupt. Refused in validation instead.
    """
    assert cap.is_filesystem_root(pathlib.Path("/")) is True
    assert cap.is_filesystem_root(pathlib.Path("/tmp")) is False
    code = cap.main(["--harness", "goose", "--cwd", "/"])
    assert code == cap.EXIT_USAGE
    assert "filesystem root" in capsys.readouterr().err


def test_a_root_working_directory_is_never_substituted(cap):
    """The reduction's own guard, so the corruption cannot arrive by another caller."""
    frames = [{"jsonrpc": "2.0", "method": "session/update", "params": {"x": "a/b"}}]
    reduced, notes = cap.reduce_frames(
        frames, backend="goose", session_id="", cwd=pathlib.Path("/"), keep_ids=False
    )
    assert json.dumps(reduced).count("session/update") == 1
    assert not any("working directory" in note for note in notes)


def test_a_brace_bearing_prompt_is_not_a_format_string(cap):
    """An operator's JSON-shaped prompt must reach the harness, not raise."""
    assert cap.turn_prompt("turn {n}", 3) == "turn 3"
    assert cap.turn_prompt('{"task":"probe","turn":{n}}', 2) == '{"task":"probe","turn":2}'
    assert cap.turn_prompt("no placeholder", 1) == "no placeholder"


def test_a_timeout_below_the_floor_is_refused(cap, capsys):
    """Refused in validation rather than raised from inside the read loop."""
    assert cap.main(["--harness", "goose", "--timeout", "-1"]) == cap.EXIT_USAGE
    assert "--timeout must be at least" in capsys.readouterr().err
    assert cap.main(["--harness", "goose", "--timeout", "nan"]) == cap.EXIT_USAGE
    assert "--timeout must be at least" in capsys.readouterr().err


# ── the drive, against a fake agent ─────────────────────────────────────────────


FAKE_AGENT = textwrap.dedent('''
    """A minimal ACP agent over stdio whose context falls on /compact."""
    import json, sys

    used = 10000

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "result": {"protocolVersion": 1,
                             "agentInfo": {"name": "Fake", "version": "9.9.9"}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "ses-fake-1"}})
        elif method == "session/prompt":
            text = msg["params"]["prompt"][0]["text"]
            if text.strip() == "/compact":
                used = 400
            else:
                used += 1000
            send({"jsonrpc": "2.0", "method": "session/update",
                  "params": {"sessionId": "ses-fake-1",
                             "update": {"sessionUpdate": "usage_update",
                                        "used": used, "size": 200000}}})
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"stopReason": "end_turn"}})
        else:
            send({"jsonrpc": "2.0", "id": msg.get("id"),
                  "error": {"code": -32601, "message": "nope"}})
    ''').strip()


@pytest.fixture
def fake_harness(cap, tmp_path, monkeypatch):
    agent = tmp_path / "fake_agent.py"
    agent.write_text(FAKE_AGENT, encoding="utf-8")
    spawn = cap.Spawn(
        backend="goose",
        argv=(sys.executable, str(agent)),
        env={"PATH": "/usr/bin:/bin"},
        protocol_version=1,
        label="fake",
    )
    monkeypatch.setattr(cap, "resolve_spawn", lambda backend: spawn)
    return spawn


def test_a_falling_context_is_measured_and_written(cap, fake_harness, tmp_path, capsys):
    out = tmp_path / "capture" / "compact-live.jsonl"
    code = cap.main(
        [
            "--harness",
            "goose",
            "--turns",
            "3",
            "--out",
            str(out),
            "--cwd",
            str(tmp_path),
            "--timeout",
            "30",
        ]
    )
    report = capsys.readouterr().out
    assert code == cap.EXIT_COMPACTED
    assert "COMPACTED" in report
    assert "peak before    13000" in report
    assert "after compact  1400" in report

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    meta = json.loads(lines[0])["_meta"]
    assert meta["agent_version"] == "9.9.9"
    assert meta["recorded"] == "live"
    assert "11000 -> 12000 -> 13000" in meta["note"]
    # The harness's own session id never reaches the file.
    body = "\n".join(lines)
    assert "ses-fake-1" not in body
    assert "goose-session-1" in body


def test_keep_ids_never_reports_a_commit_ready_capture(cap, fake_harness, tmp_path, capsys):
    """The flag's output is a debugging read, and the exit code says so.

    A kept id can pass the marker sweep and still be the harness's own: a uuid-shaped one
    is a pattern the set names, opencode's ``ses_...`` is not. So the flag answers 5
    whatever the sweep found, and the frames are still written.
    """
    out = tmp_path / "kept.jsonl"
    code = cap.main(
        [
            "--harness",
            "goose",
            "--turns",
            "1",
            "--out",
            str(out),
            "--cwd",
            str(tmp_path),
            "--keep-ids",
        ]
    )
    assert code == cap.EXIT_MARKERS
    assert "--keep-ids was set" in capsys.readouterr().err
    assert "ses-fake-1" in out.read_text(encoding="utf-8")


def test_a_context_that_does_not_fall_exits_one(cap, tmp_path, monkeypatch, capsys):
    agent = tmp_path / "stubborn_agent.py"
    agent.write_text(FAKE_AGENT.replace("used = 400", "used += 1000"), encoding="utf-8")
    monkeypatch.setattr(
        cap,
        "resolve_spawn",
        lambda backend: cap.Spawn(
            backend="goose",
            argv=(sys.executable, str(agent)),
            env={"PATH": "/usr/bin:/bin"},
            protocol_version=1,
            label="fake",
        ),
    )
    out = tmp_path / "capture.jsonl"
    code = cap.main(
        ["--harness", "goose", "--turns", "2", "--out", str(out), "--cwd", str(tmp_path)]
    )
    assert code == cap.EXIT_NOT_COMPACTED
    assert "NOT_COMPACTED" in capsys.readouterr().out
    # The capture is still written: a harness that did not compact is evidence too.
    assert out.exists()


SIGNED_OUT_AGENT = textwrap.dedent('''
    """An ACP agent that handshakes and then refuses every prompt, as a signed-out one does."""
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "ses-fake-1"}})
        else:
            send({"jsonrpc": "2.0", "id": msg.get("id"),
                  "error": {"code": -32603,
                            "message": "Failed to resolve provider: GOOSE_PROVIDER"}})
    ''').strip()


def test_an_auth_refusal_is_reported_with_the_harnesss_remedy(cap, tmp_path, monkeypatch, capsys):
    agent = tmp_path / "signed_out_agent.py"
    agent.write_text(SIGNED_OUT_AGENT, encoding="utf-8")
    monkeypatch.setattr(
        cap,
        "resolve_spawn",
        lambda backend: cap.Spawn(
            backend="goose",
            argv=(sys.executable, str(agent)),
            env={"PATH": "/usr/bin:/bin"},
            protocol_version=1,
            label="goose",
        ),
    )
    code = cap.main(
        [
            "--harness",
            "goose",
            "--turns",
            "1",
            "--out",
            str(tmp_path / "c.jsonl"),
            "--cwd",
            str(tmp_path),
        ]
    )
    err = capsys.readouterr().err
    assert code == cap.EXIT_HARNESS_REFUSED
    assert "will not talk to a model" in err
    # The repository's own remedy for this harness, not a sentence written here.
    assert "goose configure" in err

"""Replay recorded ACP frames and check what the dispatch layer makes of them.

The Agent SDK boundary refactors (``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``)
move the frame-to-event translation behind a driver. Every one of them is
supposed to be behaviour-preserving, and nothing here checked that: the dispatch
parsers are covered by unit tests that each assert one field of one frame they
construct inline, so a refactor that changes the SHAPE of a turn -- drops the
refinement event a ``tool_call_update`` emits alongside its result, stops
redacting a tool input, reorders two events -- passes every one of them.

This walks the other way round. ``test/fixtures/acp_frames/<backend>/*.jsonl``
holds real frame sequences per backend, one JSON-RPC frame per line, and
``acp_frame_replay_harness`` feeds each sequence through the same parsers the two
reader loops call. This module compares the whole resulting event stream against
a committed snapshot, so a refactor that changes what a backend's stream becomes
fails here with a diff of the events rather than passing green.

**This module is read-only.** It never writes a snapshot, because a test must not
create files in the repo that outlive the run (AUTOSDE ``no-test-side-effects``,
whose own history is a file a test left at the repo root and shipped to main).
Regenerating a snapshot is ``python3 scripts/update_acp_frame_snapshots.py``, and
the rewrite belongs in the same commit as the behaviour change that caused it, so
a reviewer sees the event diff.

**What this does NOT pin.** ``replay_frames`` mirrors the routing the reader
loops perform -- which parser runs for which action, how the caches thread, and
the order events come out -- rather than calling the loops themselves. The
parsers it calls are the real ones, so a change inside a ``_dispatch`` parser
fails here. A change that moves translation INTO ``AcpRuntime._reader_loop`` or
``AcpClient`` and out of a parser does NOT, and event reordering done there is
exactly the kind of change this corpus was built to catch. The mirror is the
price of having a gate before the driver exists: when the RFC's driver lands, the
harness should be retargeted at the driver's public entry point and the mirrored
routing deleted, which turns this from pinning the parsers into pinning the
boundary. ``HANDLED_ACTIONS`` catches a NEW action; it does not catch a changed
mapping for an existing one.

Two ratchets make it hold for backends added later:

* Every id in ``ACP_BACKENDS_KNOWN`` must have a fixture directory. A new
  provider that skips the corpus fails ``test_every_known_backend_has_fixtures``
  -- it does not skip, because a skip is what let the section 5 MCP-projection
  failure reach a public build twice.
* Every action ``classify_notification`` can return must be handled by
  ``replay_frames``. A new frame class routed in production but not there would
  otherwise be silently invisible to the corpus.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from acp_frame_replay_harness import (
    CORPUS,
    HANDLED_ACTIONS,
    RECORDED_KINDS,
    FixtureError,
    backend_dirs,
    expected_path,
    fixture_dir_name,
    fixture_files,
    fixture_ids,
    read_fixture,
    replay_frames,
    snapshot_json,
)

from kiro_crew.acp._dispatch import agent_version_from_init, classify_notification
from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN

REPO_ROOT = Path(__file__).resolve().parents[1]

_UPDATE_SCRIPT = "scripts/update_acp_frame_snapshots.py"

#: A fixture is read by people, so it stays small enough to read.
_MAX_FRAMES = 50


# ── the snapshot walk ───────────────────────────────────────────────────────


@pytest.mark.parametrize("fixture", fixture_files(), ids=fixture_ids())
def test_replayed_events_match_snapshot(fixture: Path) -> None:
    meta, frames = read_fixture(fixture)
    assert meta.get("recorded") in RECORDED_KINDS, (
        f"{fixture.name}: _meta.recorded is {meta.get('recorded')!r}, "
        f"expected one of {sorted(RECORDED_KINDS)}"
    )
    assert fixture_dir_name(meta.get("backend", "")) == fixture.parent.name, (
        f"{fixture.name}: _meta.backend {meta.get('backend')!r} maps to directory "
        f"{fixture_dir_name(meta.get('backend', ''))!r}, but it sits in {fixture.parent.name!r}"
    )
    assert frames, f"{fixture.name} carries a header but no frames"
    assert (
        len(frames) < _MAX_FRAMES
    ), f"{fixture.name} has {len(frames)} frames; keep a fixture under {_MAX_FRAMES}"

    replayed = replay_frames(frames)
    target = expected_path(fixture)
    assert target.exists(), (
        f"{target.name} is missing. Run `python3 {_UPDATE_SCRIPT}` to write it, "
        "and commit it with the change that produced it."
    )
    assert json.loads(target.read_text(encoding="utf-8")) == replayed, (
        f"the events {fixture.parent.name}/{fixture.name} replays into no longer match "
        f"{target.name}. If the change is intended, re-record with "
        f"`python3 {_UPDATE_SCRIPT}` and put the event diff in the review."
    )


def test_the_snapshot_render_is_byte_stable() -> None:
    """The script's on-disk form must round-trip, or every run reports a diff."""
    for fixture in fixture_files():
        _meta, frames = read_fixture(fixture)
        rendered = snapshot_json(replay_frames(frames))
        assert rendered == expected_path(fixture).read_text(encoding="utf-8"), (
            f"{expected_path(fixture).name} differs from the script's render even though the "
            f"parsed events match -- run `python3 {_UPDATE_SCRIPT}`"
        )


# ── ratchets ────────────────────────────────────────────────────────────────


def test_every_known_backend_has_fixtures() -> None:
    """A backend in ACP_BACKENDS_KNOWN without a corpus directory fails here.

    Deliberately a failure and not a skip: the host contract's own history is
    that a provider landed selectable while appearing in none of the buckets,
    and a skipped test is indistinguishable from a passing one on a dashboard.
    """
    present = {d.name for d in backend_dirs()}
    required = {fixture_dir_name(b) for b in ACP_BACKENDS_KNOWN}
    missing = sorted(required - present)
    assert not missing, (
        f"no frame-replay fixtures for {missing}. Every backend in ACP_BACKENDS_KNOWN "
        "needs test/fixtures/acp_frames/<id>/ with at least one .jsonl -- see that "
        "directory's README.md for how to record one."
    )
    stray = sorted(present - required)
    assert not stray, (
        f"fixture directories {stray} name no backend in ACP_BACKENDS_KNOWN; a removed "
        "backend's corpus should be deleted with it"
    )


def test_every_backend_covers_the_required_frame_kinds() -> None:
    """Each backend's corpus must reach the frame classes a turn is made of.

    Without this a directory holding one text chunk would satisfy the ratchet
    above while locking almost nothing.
    """
    gaps: list[str] = []
    for directory in backend_dirs():
        methods: set[str] = set()
        updates: set[str] = set()
        stop_reasons: set[str] = set()
        saw_init = False
        saw_session = False
        for path in sorted(directory.glob("*.jsonl")):
            _meta, frames = read_fixture(path)
            for frame in frames:
                method = frame.get("method")
                if method:
                    methods.add(method)
                result = frame.get("result")
                if isinstance(result, dict):
                    if agent_version_from_init(result):
                        saw_init = True
                    if "sessionId" in result:
                        saw_session = True
                    if result.get("stopReason"):
                        stop_reasons.add(result["stopReason"])
                params = frame.get("params")
                update = params.get("update") if isinstance(params, dict) else None
                if isinstance(update, dict) and update.get("sessionUpdate"):
                    updates.add(update["sessionUpdate"])

        name = directory.name
        if not saw_init:
            gaps.append(f"{name}: no initialize response (a result carrying agentInfo.version)")
        if not saw_session:
            gaps.append(f"{name}: no session/new response (a result carrying sessionId)")
        if "agent_message_chunk" not in updates:
            gaps.append(f"{name}: no agent_message_chunk turn")
        if "tool_call" not in updates:
            gaps.append(f"{name}: no tool_call update")
        if "tool_call_update" not in updates:
            gaps.append(f"{name}: no tool_call_update (tool result) update")
        if "session/request_permission" not in methods:
            gaps.append(f"{name}: no session/request_permission frame")
        if not stop_reasons:
            gaps.append(f"{name}: no end-of-turn response carrying a stopReason")
    assert not gaps, "corpus coverage gaps:\n" + "\n".join(gaps)


def test_replay_handles_every_action_classify_can_return() -> None:
    """``replay_frames`` must have a branch for every action in production.

    The action list is pinned in the harness rather than imported from
    ``_dispatch`` so this fails when ``classify_notification`` grows a return
    value: a frame class routed in production but unhandled in the replay would
    be invisible to every snapshot.
    """
    body = inspect.getsource(classify_notification)
    returned = set(re.findall(r'return "([a-z_]+)"', body))
    assert returned, "found no return values in classify_notification; this ratchet has gone blind"
    unhandled = sorted(returned - set(HANDLED_ACTIONS))
    assert not unhandled, (
        f"classify_notification can return {unhandled}, which replay_frames does not "
        "handle. Add a branch (and a fixture that reaches it) before the frame class "
        "becomes invisible to the corpus."
    )


def test_the_corpus_is_not_vacuous() -> None:
    """A gate that walks an empty corpus must not pass."""
    files = fixture_files()
    assert len(files) >= len(
        ACP_BACKENDS_KNOWN
    ), f"only {len(files)} fixture file(s) for {len(ACP_BACKENDS_KNOWN)} known backend(s)"
    total_events = 0
    for path in files:
        _meta, frames = read_fixture(path)
        total_events += sum(len(entry.get("events", [])) for entry in replay_frames(frames))
    assert total_events > 0, "the corpus replays into zero events; the parsers are not reached"


def test_a_recording_is_marked_live_or_synthesized_honestly() -> None:
    """Every fixture declares its provenance, and the README says which are which."""
    readme = (CORPUS / "README.md").read_text(encoding="utf-8")
    for path in fixture_files():
        meta, _frames = read_fixture(path)
        assert meta.get("recorded") in RECORDED_KINDS, path.name
        assert meta.get("date"), f"{path.name}: _meta.date is required"
        assert "agent_version" in meta, f"{path.name}: _meta.agent_version is required"
    assert (
        "synthesized" in readme
    ), "the corpus README must state which backends are synthesized rather than captured"


def test_replaying_the_whole_corpus_modifies_no_committed_file() -> None:
    """Replay must leave the corpus byte-identical on disk.

    A pytest-time snapshot-update mode is exactly the shape AUTOSDE
    ``no-test-side-effects`` forbids -- its own history is a file a test left at
    the repo root and shipped to main. So the invariant is checked by OBSERVING
    the corpus rather than by grepping this module for a write call: every
    fixture and every snapshot is stat'd, the full replay runs, and nothing may
    have moved.
    """
    tracked = sorted(CORPUS.rglob("*"))
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in tracked if p.is_file()}
    assert before, "the corpus is empty; this check would be vacuous"

    for path in fixture_files():
        _meta, frames = read_fixture(path)
        snapshot_json(replay_frames(frames))

    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in tracked if p.is_file()}
    assert after == before, (
        "replaying the corpus changed a file on disk: "
        f"{sorted(str(p) for p in set(before) ^ set(after)) or 'contents differ'}"
    )
    assert (
        REPO_ROOT / _UPDATE_SCRIPT
    ).exists(), f"{_UPDATE_SCRIPT} is missing, so there is no way to regenerate a snapshot"


# ── directory naming ─────────────────────────────────────────────────────────


def test_every_known_backend_maps_to_a_usable_directory_name() -> None:
    names = {fixture_dir_name(b) for b in ACP_BACKENDS_KNOWN}
    assert len(names) == len(ACP_BACKENDS_KNOWN), f"two backends share a directory: {names}"
    for name in names:
        assert name
        assert "/" not in name and "\\" not in name and name not in (".", "..")


# ── fixture shape ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_line", ["null", "[]", '"text"', "42"])
def test_a_non_object_frame_is_a_fixture_error_not_a_crash(tmp_path, bad_line: str) -> None:
    """A malformed line names the file and line, rather than dying inside a parser.

    ``read_fixture`` promises ``list[dict]``; without the check a ``null`` line
    reaches ``JsonRpcMessage.from_dict`` and fails with a bare ``AttributeError``
    that points at the parser instead of the fixture.
    """
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text(
        '{"_meta": {"backend": "kas", "recorded": "synthesized"}}\n'
        '{"jsonrpc": "2.0", "id": 1, "result": {}}\n'
        f"{bad_line}\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureError, match=r"bad\.jsonl:3: a frame must be a JSON object"):
        read_fixture(fixture)


# ── the writer's side effects ────────────────────────────────────────────────


def test_the_snapshot_writer_never_records_telemetry(tmp_path) -> None:
    """Regenerating snapshots must not emit ``kirocrew.tool.call.duration``.

    The parsers ``replay_frames`` drives are the production ones, and each
    ``tool_call`` -> terminal ``tool_call_update`` pair records a duration
    sample. pytest is safe because the root conftest pins ``KIROCREW_TELEMETRY=0``;
    the writer runs outside pytest, so it pins the same var itself. Checked in a
    fresh interpreter with the var UNSET and a config that says telemetry is on,
    which is the host the finding described.
    """
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_TELEMETRY"}
    env["KIROCREW_HOME"] = str(tmp_path)
    (tmp_path / "config.json").write_text('{"telemetry": {"enabled": true}}', encoding="utf-8")
    probe = (
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('writer', {str(REPO_ROOT / _UPDATE_SCRIPT)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "import os\n"
        "from kiro_crew.metrics.provider import get_recorder\n"
        "print(os.environ.get('KIROCREW_TELEMETRY'), get_recorder().enabled)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0 False", (
        "the snapshot writer imported the parsers with telemetry live: " f"{out.stdout.strip()!r}"
    )

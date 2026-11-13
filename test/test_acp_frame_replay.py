"""Replay recorded ACP frames and check what the dispatch layer makes of them.

``test/fixtures/acp_frames/<backend>/*.jsonl`` holds real frame sequences, and
``acp_frame_replay_harness`` feeds each through the real parsers; the whole event
stream is compared to a committed snapshot, so a change to a turn's shape fails
with an event diff (see ``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``).

Read-only (AUTOSDE ``no-test-side-effects``): regenerate with
``python3 scripts/update_acp_frame_snapshots.py`` in the commit that changes behaviour.

``replay_frames`` mirrors the reader loops' routing, so a change made inside
``AcpRuntime._reader_loop`` or ``AcpClient`` is not caught; retarget the harness at
the driver's entry point when the RFC's driver lands. ``HANDLED_ACTIONS`` catches
a new action, not a changed mapping.

Ratchets: every id in ``ACP_BACKENDS_KNOWN`` needs a fixture directory (fails, never
skips), and every action ``classify_notification`` returns must be handled.
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
from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_UPDATE_SCRIPT = "scripts/update_acp_frame_snapshots.py"

#: A fixture is read by people, so it stays small enough to read.
_MAX_FRAMES = 50

#: Backends whose ``initialize`` carries no ``agentInfo``, listed by name so each
#: omission is a written decision. ``kas`` (0.63.3): nothing consumes its version and
#: ``agent_version_from_init`` reads a missing ``agentInfo`` as ``""``.
_BACKENDS_WITHOUT_AGENT_VERSION = frozenset({"kas"})


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

    replayed = replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
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
        meta, frames = read_fixture(fixture)
        rendered = snapshot_json(
            replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
        )
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

    The agent version is checked apart from ``protocolVersion`` because
    ``agentInfo`` is optional in ACP (``_BACKENDS_WITHOUT_AGENT_VERSION``).
    """
    gaps: list[str] = []
    for directory in backend_dirs():
        methods: set[str] = set()
        updates: set[str] = set()
        stop_reasons: set[str] = set()
        saw_init = False
        saw_version = False
        saw_session = False
        for path in sorted(directory.glob("*.jsonl")):
            _meta, frames = read_fixture(path)
            for frame in frames:
                method = frame.get("method")
                if method:
                    methods.add(method)
                result = frame.get("result")
                if isinstance(result, dict):
                    if "protocolVersion" in result:
                        saw_init = True
                    if agent_version_from_init(result):
                        saw_version = True
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
            gaps.append(f"{name}: no initialize response (a result carrying protocolVersion)")
        if not saw_version and name not in _BACKENDS_WITHOUT_AGENT_VERSION:
            gaps.append(
                f"{name}: initialize response carries no agentInfo.version; if this backend "
                "really sends none, name it in _BACKENDS_WITHOUT_AGENT_VERSION with the evidence"
            )
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
        meta, frames = read_fixture(path)
        total_events += sum(
            len(entry.get("events", []))
            for entry in replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce"))
        )
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


def _init_agent_info(frames: list[dict]) -> dict | None:
    for frame in frames:
        result = frame.get("result")
        if isinstance(result, dict) and "protocolVersion" in result:
            info = result.get("agentInfo")
            return info if isinstance(info, dict) else None
    return None


def test_a_live_header_names_the_agent_its_capture_answered_as() -> None:
    """``_meta.agent_name`` and ``agent_version`` are read off the wire, not typed.

    A live capture holding an ``initialize`` answer must agree with its
    ``agentInfo``. One without (a slice taken later in a session) must name the
    agent its directory's own handshake captures answer as. The pin gate in
    ``test_real_adapter_fixture_pins.py`` keys on these two fields.
    """
    wire_names: dict[str, set[str]] = {}
    headers = []
    for path in fixture_files():
        meta, frames = read_fixture(path)
        if meta.get("recorded") != "live" or meta.get("backend") in _BACKENDS_WITHOUT_AGENT_VERSION:
            continue
        info = _init_agent_info(frames)
        if info is not None:
            wire_names.setdefault(path.parent.name, set()).add(info.get("name"))
            # pi's header adds the host binary's release after a space: "0.0.34 (pi 1.3.0)".
            assert (meta.get("agent_name"), str(meta.get("agent_version")).split(" ")[0]) == (
                info.get("name"),
                info.get("version"),
            ), f"{path.parent.name}/{path.name}: _meta disagrees with its own initialize agentInfo"
        headers.append((path, meta))
    for path, meta in headers:
        assert meta.get("agent_name") in wire_names.get(path.parent.name, set()), (
            f"{path.parent.name}/{path.name}: _meta.agent_name {meta.get('agent_name')!r} is not "
            f"what this directory's handshake captures answer as"
        )


def test_replaying_the_whole_corpus_modifies_no_committed_file() -> None:
    """Replay must leave the corpus byte-identical on disk.

    AUTOSDE ``no-test-side-effects``: every file is stat'd before and after a full
    replay, rather than grepping this module for a write call.
    """
    tracked = sorted(CORPUS.rglob("*"))
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in tracked if p.is_file()}
    assert before, "the corpus is empty; this check would be vacuous"

    for path in fixture_files():
        meta, frames = read_fixture(path)
        snapshot_json(replay_frames(frames, gate_envelope_nonce=meta.get("gate_envelope_nonce")))

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

    The real parsers record a duration per tool call; the writer runs outside the
    conftest's ``KIROCREW_TELEMETRY=0``, so it is run fresh with the var unset.
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


def test_a_numeric_handshake_fixture_requires_a_protocol_version_row() -> None:
    """A harness whose OWN wire answers a numeric version must not be sent a date string.

    ``_PROTOCOL_VERSION_BY_BACKEND`` defaults to kiro-cli's date-stamped dialect, which
    goose 1.50.1 refuses with ``-32602 Invalid params``. Derived from each backend's
    recorded ``initialize`` response, so a new harness fails here with no edit.

    The kiro family is exempt by name: ACP negotiates, so their numeric answer is the
    version they chose, and adding an id there claims the harness takes the date string.
    """
    from kiro_crew.acp.client import _PROTOCOL_VERSION_BY_BACKEND
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp_backends import ACP_BACKENDS_ACP_RUNTIME

    # The kiro family: handed the date-stamped version by construction, and named here
    # rather than inferred so an addition is a decision a reviewer sees.
    date_stamped_family = {ACP_BACKEND_KIRO, ACP_BACKEND_KAS}

    def _declares_a_numeric_dialect(backend: str) -> bool:
        """Whether the core that DRIVES *backend* would send a numeric version.

        A shared-runtime harness answers from its ``protocol_version`` seam and has no
        client row on purpose; a per-session harness answers from the client table.
        """
        if backend in ACP_BACKENDS_ACP_RUNTIME:
            declared = harness_for(backend).protocol_version
            return isinstance(declared, int) and not isinstance(declared, bool)
        return backend in _PROTOCOL_VERSION_BY_BACKEND

    missing: list[str] = []
    checked: list[str] = []
    for directory in backend_dirs():
        for path in sorted(directory.glob("*.jsonl")):
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines:
                continue
            backend = (json.loads(lines[0]).get("_meta") or {}).get("backend")
            if backend is None or backend not in ACP_BACKENDS_KNOWN:
                continue
            if backend in date_stamped_family:
                continue
            for line in lines[1:]:
                result = json.loads(line).get("result")
                if not isinstance(result, dict) or "protocolVersion" not in result:
                    continue
                # An integer is the SPEC dialect; kiro-cli answers with a date string.
                # ``bool`` is an int subclass and would be a malformed frame, so it is
                # excluded rather than read as a version.
                version = result["protocolVersion"]
                if isinstance(version, bool) or not isinstance(version, int):
                    continue
                checked.append(f"{backend} ({path.name})")
                if not _declares_a_numeric_dialect(backend):
                    missing.append(
                        f"{backend}: {path.name} answers protocolVersion {version!r}, but "
                        "neither its harness seam nor the client core's "
                        "_PROTOCOL_VERSION_BY_BACKEND declares a numeric dialect for it, "
                        "so Crew sends the date-stamped version instead"
                    )
                break

    assert checked, (
        "no fixture outside the kiro family carries a numeric initialize response, so "
        "this ratchet is vacuous -- either the corpus lost its handshake captures, the "
        "_meta backend ids drifted, or the exempt family grew to cover everything"
    )
    assert missing == [], "\n".join(missing)

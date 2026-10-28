"""Reading the kiro-cli sessions a person ran in a terminal (``kiro_crew.kiro_cli_sessions``).

Pins the one listing rule (``is_terminal_session``: Crew-owned, sub-agent, untitled,
Crew-prompted and Crew-directory sessions are never listed), the envelope reader on
compact, pretty-printed, malformed, partial and non-UTF-8 files, the preview's paging,
cap and skip count, and the snapshot "Continue in Crew" copies: a torn trailing record
is left out, a pair that keeps changing is refused as busy, a corrupt log is refused,
and the files are never written.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from kiro_crew import kiro_cli_sessions as kcs
from kiro_crew.config import paths

SID = "11111111-2222-4333-8444-555555555555"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "sessions" / "cli"
    d.mkdir(parents=True)
    monkeypatch.setattr(paths, "_sessions_dir_override", lambda: d)
    kcs._META_CACHE.clear()
    return d


def _envelope(
    sid=SID,
    *,
    title="fix the flaky test",
    cwd="/work/repo",
    pretty=False,
    agent="fable",
    updated_at="2026-10-02T10:00:00Z",
):
    doc = {
        "session_id": sid,
        "cwd": cwd,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": updated_at,
        "title": title,
        "session_created_reason": "subagent",
        # The conversation sits between the head and the agent name, as it does
        # in kiro-cli's own files, so the head read cannot reach the name.
        "session_state": {
            "version": "v1",
            "conversation_metadata": {"turns": ["x" * 40_000]},
            "agent_name": agent,
        },
    }
    return json.dumps(doc, indent=2 if pretty else None)


def _event(kind, *parts, ts=None):
    data = {"message_id": "m", "content": [{"kind": k, "data": v} for k, v in parts]}
    if ts is not None:
        data["meta"] = {"timestamp": ts}
    return json.dumps({"version": "v1", "kind": kind, "data": data})


def _write(store: Path, sid=SID, envelope=None, events=None) -> None:
    (store / f"{sid}.json").write_text(envelope if envelope is not None else _envelope(sid))
    if events is not False:
        lines = (
            events
            if events is not None
            else [
                _event("Prompt", ("text", "why is it flaky?"), ts=1793022032),
                _event("AssistantMessage", ("thinking", {"text": "hmm"}), ("text", "A race.")),
                _event("ToolResults", ("toolResult", {"toolUseId": "t"})),
            ]
        )
        (store / f"{sid}.jsonl").write_text("".join(f"{line}\n" for line in lines))


def _meta(**over) -> kcs.CliSessionMeta:
    base = dict(
        session_id=SID,
        title="fix the flaky test",
        cwd="/work/repo",
        created_at="",
        updated_at="",
        agent_name="fable",
    )
    base.update(over)
    return kcs.CliSessionMeta(**base)


def _listable(meta, owned=frozenset(), roots=("/home/u/.kiro/crew", "/home/u/workplace/ws")):
    return kcs.is_terminal_session(meta, owned_sids=owned, crew_roots=roots, realpath=str)


# ── the listing rule ──────────────────────────────────────────────────────


def test_a_titled_session_in_a_project_folder_is_listed():
    assert _listable(_meta())


def test_a_session_crew_owns_is_never_listed():
    assert not _listable(_meta(), owned=frozenset({SID}))


def test_a_sub_agent_of_another_session_is_not_listed():
    assert not _listable(_meta(parent_session_id=OTHER))


@pytest.mark.parametrize("title", ["", "(no title)"])
def test_an_untitled_session_is_not_listed(title):
    assert not _listable(_meta(title=title))


@pytest.mark.parametrize(
    "title",
    [
        "[AGENT SYSTEM PROMPT]",
        "[SESSION CONTEXT — background reference only",
        "[Previous chat history for this tab — session was reset after stop]",
        "[sent by session chat-1 via session_send]",
    ],
)
def test_a_session_titled_from_a_crew_prompt_is_not_listed(title):
    assert not _listable(_meta(title=title))


def test_a_session_under_a_crew_generated_agent_is_not_listed():
    assert not _listable(_meta(agent_name="kirocrew-skill-view-0123abcd"))


@pytest.mark.parametrize(
    "cwd",
    [
        "/home/u/.kiro/crew",
        "/home/u/.kiro/crew/workspace",
        "/home/u/workplace/ws/_bg",
        "/home/u/workplace/ws/_consolidate",
        "/home/u/workplace/ws/cron_e068994f",
        # Another gateway's home on the same machine, named only by its segment.
        "/home/u/src/checkout/.kirocrew-dev/workspace",
        "/home/u/.kirocrew/workspace",
        "/home/u/.kirocrew-pods/pod-1/workspace",
        "/elsewhere/kirocrew-workspace/cron_1",
        "/srv/other/.kiro/crew/workspace",
    ],
)
def test_a_session_in_a_crew_directory_is_not_listed(cwd):
    assert not _listable(_meta(cwd=cwd))


def test_a_sibling_of_a_crew_root_with_a_shared_prefix_is_listed():
    assert _listable(_meta(cwd="/home/u/workplace/ws-other/project"))


def test_the_rule_compares_the_resolved_folder_too():
    resolved = {"/link/to/crew": "/home/u/.kiro/crew/workspace"}
    meta = _meta(cwd="/link/to/crew")
    assert not kcs.is_terminal_session(
        meta,
        owned_sids=frozenset(),
        crew_roots=("/home/u/.kiro/crew",),
        realpath=lambda p: resolved.get(p, p),
    )


def test_crew_roots_include_the_data_home_and_workspace_root(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(ws))
    roots = kcs.crew_working_roots()
    assert os.path.realpath(str(paths.data_home())) in roots
    assert os.path.realpath(str(ws)) in roots


# ── the envelope reader ───────────────────────────────────────────────────


@pytest.mark.parametrize("pretty", [False, True])
def test_the_envelope_head_is_read_compact_or_pretty(store, pretty):
    _write(store, envelope=_envelope(pretty=pretty))
    meta = kcs.read_envelope_meta(store / f"{SID}.json")
    assert meta is not None
    assert (meta.session_id, meta.title, meta.cwd, meta.agent_name) == (
        SID,
        "fix the flaky test",
        "/work/repo",
        "fable",
    )


def test_a_crew_generated_agent_is_read_from_the_file_tail(store):
    _write(store, envelope=_envelope(agent="kirocrew-skill-view-0123abcd"))
    _write(store, OTHER, envelope=_envelope(OTHER))
    rows = kcs.list_terminal_sessions(frozenset())["sessions"]
    assert [r["id"] for r in rows] == [OTHER]


def test_a_parent_id_written_after_the_state_is_still_seen(store):
    doc = json.loads(_envelope())
    doc["parent_session_id"] = OTHER
    (store / f"{SID}.json").write_text(json.dumps(doc))
    meta = kcs.read_envelope_meta(store / f"{SID}.json")
    assert meta is not None and meta.parent_session_id == OTHER


@pytest.mark.parametrize(
    "raw",
    [b"", b"{not json", b'{"session_id": "' + SID.encode() + b'", "cwd":', b"\xff\xfe\x00junk"],
    ids=["empty", "malformed", "partial", "non-utf8"],
)
def test_an_unreadable_envelope_answers_none(store, raw):
    (store / f"{SID}.json").write_bytes(raw)
    assert kcs.read_envelope_meta(store / f"{SID}.json") is None


def test_an_envelope_naming_another_id_is_not_listed(store):
    _write(store, envelope=_envelope(OTHER))
    assert kcs.scan_sessions() == []


def test_the_scan_skips_links_and_foreign_names(store, tmp_path):
    _write(store)
    outside = tmp_path / "outside.json"
    outside.write_text(_envelope(OTHER))
    (store / f"{OTHER}.json").symlink_to(outside)
    (store / "notes.json").write_text(_envelope(SID))
    assert [m.session_id for m in kcs.scan_sessions()] == [SID]


def test_the_scan_is_newest_first_and_rereads_only_changed_files(store, monkeypatch):
    _write(store, envelope=_envelope(SID, updated_at="2026-10-01T00:00:00Z"))
    _write(store, OTHER, envelope=_envelope(OTHER, updated_at="2026-10-05T00:00:00Z"))
    assert [m.session_id for m in kcs.scan_sessions()] == [OTHER, SID]
    reads: list[Path] = []
    real = kcs.read_envelope_meta
    monkeypatch.setattr(kcs, "read_envelope_meta", lambda p: reads.append(p) or real(p))
    kcs.scan_sessions()
    assert reads == []
    (store / f"{SID}.json").write_text(_envelope(SID, title="renamed", updated_at="2026-10-09"))
    os.utime(store / f"{SID}.json", ns=(1, 1))
    assert {m.title for m in kcs.scan_sessions()} == {"renamed", "fix the flaky test"}
    assert [p.name for p in reads] == [f"{SID}.json"]


def test_a_vanished_file_leaves_the_cache(store):
    _write(store)
    kcs.scan_sessions()
    (store / f"{SID}.json").unlink()
    assert kcs.scan_sessions() == []
    assert f"{SID}.json" not in kcs._META_CACHE


def test_the_list_pages_and_filters(store):
    for i in range(5):
        sid = f"{i:08d}-2222-4333-8444-555555555555"
        _write(
            store, sid, envelope=_envelope(sid, title=f"task {i}", updated_at=f"2026-10-0{i + 1}")
        )
    out = kcs.list_terminal_sessions(frozenset(), offset=1, limit=2)
    assert [r["title"] for r in out["sessions"]] == ["task 3", "task 2"]
    assert (out["total"], out["has_more"]) == (5, True)
    assert kcs.list_terminal_sessions(frozenset(), query="TASK 4")["total"] == 1
    assert kcs.list_terminal_sessions(frozenset(), query="/work/")["total"] == 5


# ── session ids never name a path ─────────────────────────────────────────


@pytest.mark.parametrize(
    "sid",
    ["../" + SID, SID + "/..", OTHER.upper(), "x", "", f"{SID}.json", "/etc/passwd", None, 7],
)
def test_only_a_uuid_resolves_to_a_path(store, sid):
    assert kcs.session_paths(sid) is None
    assert kcs.read_preview(sid) is None
    with pytest.raises(kcs.SnapshotRefused) as refused:
        kcs.snapshot_session(sid)
    assert refused.value.code == "kiro_cli_session_invalid_id"


def test_a_uuid_resolves_inside_the_store(store):
    json_path, jsonl_path = kcs.session_paths(SID)
    assert json_path.parent == store and jsonl_path.parent == store


# ── the preview ───────────────────────────────────────────────────────────


def test_the_preview_shows_only_the_visible_turns(store):
    _write(store)
    page = kcs.read_preview(SID)
    assert [(m["role"], m["content"]) for m in page["messages"]] == [
        ("user", "why is it flaky?"),
        ("assistant", "A race."),
    ]
    assert page["messages"][0]["ts"] == "2026-10-26T13:40:32Z"
    assert (page["total"], page["start"], page["skipped"], page["has_transcript"]) == (
        2,
        0,
        0,
        True,
    )


def test_the_preview_pages_back_from_the_newest(store):
    _write(store, events=[_event("Prompt", ("text", f"q{i}")) for i in range(7)])
    last = kcs.read_preview(SID, limit=3)
    assert [m["content"] for m in last["messages"]] == ["q4", "q5", "q6"]
    older = kcs.read_preview(SID, before=last["start"], limit=3)
    assert [m["content"] for m in older["messages"]] == ["q1", "q2", "q3"]
    first = kcs.read_preview(SID, before=older["start"], limit=3)
    assert [m["content"] for m in first["messages"]] == ["q0"] and first["start"] == 0


def test_a_huge_message_is_cut_after_it_is_sanitized(store):
    secret_at_the_cut = "a" * (kcs.PREVIEW_MAX_CHARS - 3) + "SECRET-TOKEN"
    _write(store, events=[_event("AssistantMessage", ("text", secret_at_the_cut))])
    page = kcs.read_preview(SID, sanitize_assistant=lambda t: t.replace("SECRET-TOKEN", "[x]"))
    msg = page["messages"][0]
    assert msg["truncated"] is False
    assert msg["content"].endswith("[x]") and "SECRET" not in msg["content"]
    _write(store, events=[_event("AssistantMessage", ("text", "b" * (kcs.PREVIEW_MAX_CHARS + 50)))])
    msg = kcs.read_preview(SID)["messages"][0]
    assert msg["truncated"] is True and len(msg["content"]) == kcs.PREVIEW_MAX_CHARS


def test_the_preview_skips_corrupt_and_non_utf8_records(store):
    good = _event("Prompt", ("text", "still here"))
    (store / f"{SID}.json").write_text(_envelope())
    (store / f"{SID}.jsonl").write_bytes(
        b"{broken\n" + b"\xff\xfe\n" + good.encode() + b"\n" + b'{"kind": "Prompt", "data"'
    )
    page = kcs.read_preview(SID)
    assert [m["content"] for m in page["messages"]] == ["still here"]
    assert page["skipped"] == 3


def test_a_session_with_no_log_previews_empty(store):
    _write(store, events=False)
    page = kcs.read_preview(SID)
    assert (page["messages"], page["has_transcript"]) == ([], False)


def test_the_preview_of_a_missing_session_is_none(store):
    assert kcs.read_preview(SID) is None


# ── the snapshot Continue copies ──────────────────────────────────────────


def _digest(store: Path) -> dict[str, tuple[str, int]]:
    return {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(store.iterdir())
    }


def test_the_snapshot_copies_the_pair_and_never_writes_it(store):
    _write(store)
    before = _digest(store)
    snap = kcs.snapshot_session(SID)
    assert snap.events == (store / f"{SID}.jsonl").read_text()
    assert snap.envelope == json.loads((store / f"{SID}.json").read_text())
    assert [m["role"] for m in snap.messages] == ["user", "assistant"]
    assert _digest(store) == before


def test_a_torn_trailing_record_is_left_out_of_the_copy(store):
    complete = _event("Prompt", ("text", "done"))
    (store / f"{SID}.json").write_text(_envelope())
    (store / f"{SID}.jsonl").write_text(complete + "\n" + '{"version":"v1","kind":"Assist')
    snap = kcs.snapshot_session(SID)
    assert snap.events == complete + "\n"
    assert [m["content"] for m in snap.messages] == ["done"]


def test_a_complete_last_record_without_a_newline_is_kept(store):
    record = _event("Prompt", ("text", "no newline yet"))
    (store / f"{SID}.json").write_text(_envelope())
    (store / f"{SID}.jsonl").write_text(record)
    assert kcs.snapshot_session(SID).events == record


def test_a_line_separator_inside_a_string_is_not_a_record_break(store):
    record = _event("Prompt", ("text", "one\u2028two"))
    (store / f"{SID}.json").write_text(_envelope())
    (store / f"{SID}.jsonl").write_text(record + "\n", encoding="utf-8")
    assert kcs.snapshot_session(SID).messages[0]["content"] == "one\u2028two"


def test_a_pair_that_changes_during_the_read_is_read_again(store, monkeypatch):
    _write(store)
    real = Path.read_bytes
    calls = {"n": 0}

    def growing(self):
        data = real(self)
        if self.suffix == ".jsonl" and calls["n"] == 0:
            calls["n"] += 1
            with open(self, "a") as f:
                f.write(_event("Prompt", ("text", "typed meanwhile")) + "\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", growing)
    monkeypatch.setattr(kcs, "_SNAPSHOT_RETRY_SECS", 0)
    snap = kcs.snapshot_session(SID)
    assert snap.messages[-1]["content"] == "typed meanwhile"


def test_a_pair_that_never_settles_is_refused_as_busy(store, monkeypatch):
    _write(store)
    real = Path.read_bytes

    def always_growing(self):
        data = real(self)
        if self.suffix == ".jsonl":
            with open(self, "a") as f:
                f.write(_event("Prompt", ("text", "more")) + "\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", always_growing)
    monkeypatch.setattr(kcs, "_SNAPSHOT_RETRY_SECS", 0)
    with pytest.raises(kcs.SnapshotRefused) as refused:
        kcs.snapshot_session(SID)
    assert refused.value.code == "kiro_cli_session_busy"


@pytest.mark.parametrize(
    ("envelope", "events", "code"),
    [
        (None, False, "kiro_cli_session_no_transcript"),
        ("{bad", None, "kiro_cli_session_unreadable"),
        (_envelope(OTHER), None, "kiro_cli_session_unreadable"),
        (None, ["{corrupt middle", _event("Prompt", ("text", "x"))], "kiro_cli_session_unreadable"),
        (None, [_event("ToolResults", ("toolResult", {}))], "kiro_cli_session_empty"),
    ],
    ids=["no-log", "bad-envelope", "foreign-envelope", "corrupt-log", "nothing-visible"],
)
def test_a_session_that_cannot_resume_is_refused(store, envelope, events, code):
    _write(store, envelope=envelope, events=events)
    with pytest.raises(kcs.SnapshotRefused) as refused:
        kcs.snapshot_session(SID)
    assert refused.value.code == code


def test_a_non_utf8_log_is_refused(store):
    (store / f"{SID}.json").write_text(_envelope())
    (store / f"{SID}.jsonl").write_bytes(b'{"kind": "Prompt", "x": "\xff"}\n')
    with pytest.raises(kcs.SnapshotRefused) as refused:
        kcs.snapshot_session(SID)
    assert refused.value.code == "kiro_cli_session_unreadable"


def test_a_missing_session_is_refused_as_gone(store):
    with pytest.raises(kcs.SnapshotRefused) as refused:
        kcs.snapshot_session(SID)
    assert refused.value.code == "kiro_cli_session_not_found"


# ── the copies Continue made ──────────────────────────────────────────────


def test_a_recorded_copy_is_remembered_once_and_in_order(store):
    assert kcs.continued_sids() == frozenset()
    kcs.record_continued(SID)
    kcs.record_continued(OTHER)
    kcs.record_continued(SID)
    assert json.loads(kcs._continued_path().read_text())["sids"] == [OTHER, SID]
    assert kcs.continued_sids() == {SID, OTHER}


def test_only_session_ids_are_recorded(store):
    kcs.record_continued("../etc/passwd")
    assert kcs.continued_sids() == frozenset()


@pytest.mark.parametrize("raw", ["{bad", "[]", '{"sids": "x"}', '{"sids": ["nope", 3]}'])
def test_a_malformed_record_reads_as_empty(store, raw):
    kcs._continued_path().parent.mkdir(parents=True, exist_ok=True)
    kcs._continued_path().write_text(raw)
    assert kcs.continued_sids() == frozenset()


def test_the_record_keeps_only_the_newest(store, monkeypatch):
    monkeypatch.setattr(kcs, "_CONTINUED_MAX", 2)
    third = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    for sid in (SID, OTHER, third):
        kcs.record_continued(sid)
    assert kcs.continued_sids() == {OTHER, third}

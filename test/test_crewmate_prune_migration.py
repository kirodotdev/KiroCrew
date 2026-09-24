"""The one-time startup prune of sync-generated crewmates (``crewmate_prune_migration``).

Seeds an old-style ``config.json`` -- the rows an older ``POST /api/agents/sync``
left behind (no ``member_id``, shared ``default`` store, bound to the user's own
spec by name) beside a hand-created member and a sync-shaped row that owns a
non-empty memory store -- and asserts the pass does exactly what its docstring
promises: the never-chatted synced row on the shared store is removed, the
chatted one keeps its exact binding (shared store, no ``member_id``), the
hand-created one and the one with memory are untouched, the marker is written,
and a second boot is a no-op. Then the evidence edges: a session file the pass
cannot read makes the history incomplete and keeps every candidate, while one
that reads but names nobody (or another agent) is evidence and voids nothing; a
candidate whose own activity log or binding cannot be read is kept on doubt,
a FIFO or link at either path is "no record" rather than a hang, and the pass
always finishes; a spec that is gone, a package spec, the
runtime's own, a row with an extra key or one the overlay touches is never a
candidate; a row, spec or overlay that changes between the judgement and the
delete refuses the delete. Last, the request gate the pass relies on:
registering it arms the barrier, every mutating request -- under ``/api/`` or
not -- and every read of the member roster waits until the pass settles (or
gets 503 when it never does), other reads are never held; and the writer-side
wait, which on timeout tells the pass to stop deleting and still waits for it
to return. And the cross-process lock the pass runs under: a second process
holding it keeps this one from removing anything, and the marker is created
exclusively.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import crewmate_prune_migration as mig
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.memory_stores import provision_member_memory


@pytest.fixture(autouse=True)
def _agents_dir(tmp_path, monkeypatch):
    """Point the agents directory at a scratch dir through the documented hook.

    The delete re-reads each candidate's spec file under the spec lock, so the
    specs discovery is patched to return must also exist on disk; ``_run``
    writes them here.
    """
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", root)
    return root


def _spec(name: str, **kw) -> AgentInfo:
    base = dict(
        name=name,
        filename=f"{name}.json",
        description=f"{name} agent",
        model="auto",
        source="builtin",
    )
    base.update(kw)
    return AgentInfo(**base)


def _synced(name: str) -> KiroCrewAgentConfig:
    # Exactly what an older sync wrote: the spec's description, nothing else set.
    return KiroCrewAgentConfig(kiro_agent=name, description=f"{name} agent", source="builtin")


def _spec_path(name: str) -> Path:
    return Path(kiro_agents_dir_path()) / f"{name}.json"


def _write_specs(specs: dict) -> None:
    """Materialise the spec files discovery will report, with the same description."""
    root = Path(kiro_agents_dir_path())
    root.mkdir(parents=True, exist_ok=True)
    for spec in specs.values():
        (root / spec.filename).write_text(
            json.dumps({"name": spec.name, "description": spec.description})
        )


class _Log:
    """A conversation log: ``_dir`` holds one ``<key>.jsonl`` per session, the
    first line the metadata record the real log writes."""

    def __init__(self, root: Path):
        self._dir = root
        root.mkdir(parents=True, exist_ok=True)

    def session(self, key: str, agent: str | None = None, *, raw: bytes | None = None):
        path = self._dir / f"{key}.jsonl"
        if raw is not None:
            path.write_bytes(raw)
            return path
        meta: dict = {"_type": "metadata", "title": key}
        if agent:
            meta["agent"] = agent
        path.write_text(
            json.dumps(meta) + "\n" + json.dumps({"role": "user", "content": "hi"}) + "\n"
        )
        return path


@pytest.fixture
def log(tmp_path):
    return _Log(tmp_path / "sessions")


@pytest.fixture
def bindings_dir(tmp_path, monkeypatch):
    """Point the DM-binding path at a scratch dir; ``write(name)`` opens a thread."""
    root = tmp_path / "dm"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.members.dm_binding_path", lambda slug: root / f"{slug}.json")
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, cfg=None: name)

    def write(name: str, *, member: str | None = None, raw: bytes | None = None):
        path = root / f"{name}.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(json.dumps({"slot_key": f"member-{name}", "member": member or name}))
        return path

    return write


@pytest.fixture
def activity_dir(tmp_path, monkeypatch):
    """Point the member directory at a scratch dir; ``write(name)`` records a session."""
    root = tmp_path / "members"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.members.member_dir", lambda slug: root / slug)
    monkeypatch.setattr("kiro_crew.members.member_slug", lambda name, cfg=None: name)

    def write(name: str, *, member: str | None = None, raw: bytes | None = None, rotated=False):
        d = root / name
        d.mkdir(exist_ok=True)
        path = d / ("activity.jsonl.1" if rotated else "activity.jsonl")
        if raw is not None:
            path.write_bytes(raw)
        else:
            row = {"ts": "2026-01-01T00:00:00Z", "member": member or name, "session": f"s-{name}"}
            path.write_text(json.dumps(row) + "\n")
        return path

    return write


@pytest.fixture
def old_style_config():
    """Three rows: two the sync left (one chatted, one never), one made by hand."""
    cfg = KiroCrewConfig.load()
    cfg.agents["radar"] = _synced("radar")
    cfg.agents["scout"] = _synced("scout")
    cfg.agents["by-hand"] = KiroCrewAgentConfig(kiro_agent="radar", description="mine")
    provision_member_memory(cfg, "by-hand")
    # A row that was provisioned (own store, so `memory_store != "default"`)
    # but whose member_id is blank: not the never-provisioned signature, so not
    # a candidate whatever its history. No directory is inspected to decide that.
    cfg.agents["kept-memory"] = _synced("kept-memory")
    store = provision_member_memory(cfg, "kept-memory")
    cfg.agents["kept-memory"].member_id = ""  # back to the sync's shape, store kept
    cfg.save()
    from kiro_crew.memory_stores import _named_store_dir

    (_named_store_dir(store) / "memory" / "note.md").write_text("kept\n")
    hand = KiroCrewConfig.load().agents["by-hand"]
    assert hand.member_id and hand.memory_store != "default"
    return {"radar": _spec("radar"), "scout": _spec("scout"), "kept-memory": _spec("kept-memory")}


def _run(specs: dict, log, **kw):
    _write_specs(specs)
    with patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())):
        return mig.prune_synced_crewmates(log, **kw)


class TestThePass:
    def test_removes_never_chatted_keeps_chatted_leaves_hand_made(
        self, old_style_config, bindings_dir, log
    ):
        before = KiroCrewConfig.load()
        hand_before = before.agents["by-hand"]
        assert not mig.marker_path().exists()
        bindings_dir("radar")  # the owner opened radar's thread once
        log.session("chat-1", "kirocrew")

        report = _run(old_style_config, log)

        assert report.removed == ["scout"]
        assert report.kept == ["radar"]
        after = KiroCrewConfig.load()
        assert "scout" not in after.agents
        # The chatted row keeps its exact binding: a memory binding is identity,
        # chosen at creation, and no startup pass rewrites it.
        assert after.agents["radar"] == before.agents["radar"]
        assert after.agents["radar"].member_id == ""
        assert after.agents["radar"].memory_store == "default"
        assert after.agents["by-hand"] == hand_before
        assert after.agents["kept-memory"] == before.agents["kept-memory"]
        assert after.agents["kept-memory"].memory_store != "default"
        # The spec under the agents directory is only read, never touched.
        assert _spec_path("scout").exists()
        marker = json.loads(mig.marker_path().read_text())
        assert marker["removed"] == ["scout"]
        assert marker["kept"] == ["radar"]
        assert marker["doubted"] == {}

    def test_an_activity_record_counts_as_chatted(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        # The slot later switched agents, so no session metadata names scout
        # and no DM thread exists; the member activity log still holds the
        # session pointer record_activity wrote when the chat ran as scout.
        log.session("chat-3", "someone-else")
        activity_dir("scout")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.kept == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].member_id == ""

    def test_a_rotated_activity_file_counts_too(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        activity_dir("scout", rotated=True)
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    def test_an_event_log_record_counts_as_chatted(
        self, old_style_config, bindings_dir, activity_dir, log, monkeypatch
    ):
        class _FakeLog:
            def __init__(self, slug):
                self.slug = slug

            def exists(self):
                return self.slug == "scout"

            def iter_events(self):
                yield {"type": "activity/record", "data": {"member": "scout", "session": "s"}}

        monkeypatch.setattr("kiro_crew.eventlog.log.MemberLog", _FakeLog)
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]
        assert report.removed == ["radar"]

    def test_an_activity_record_for_another_name_is_not_this_crewmates(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        # Slugs collide: one log can hold two members. The name decides.
        activity_dir("scout", member="Scout!")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_a_session_that_named_the_crewmate_counts_as_chatted(
        self, old_style_config, bindings_dir, log
    ):
        # No DM thread, but a plain session selected it: kept, untouched.
        log.session("chat-2", "scout")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.kept == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].member_id == ""

    def test_second_boot_is_a_no_op(self, old_style_config, bindings_dir, log):
        bindings_dir("radar")
        _run(old_style_config, log)
        cfg = KiroCrewConfig.load()
        cfg.agents["late"] = _synced("late")
        cfg.save()
        specs = dict(old_style_config, late=_spec("late"))
        report = _run(specs, log)
        assert report.skipped_marker is True
        assert "late" in KiroCrewConfig.load().agents

    def test_a_no_op_pass_still_writes_the_marker(self, bindings_dir, log):
        report = _run({}, log)
        assert report.removed == [] and report.kept == []
        assert mig.marker_path().exists()


class TestKeptOnDoubt:
    """A session file the pass cannot read makes the history incomplete, and an
    incomplete history removes nothing: every candidate is kept. A session file
    that reads but names nobody is evidence, not doubt. A candidate whose own
    history cannot be read is kept; the pass still finishes and the marker
    names both, so no boot re-runs it."""

    def test_a_session_file_that_does_not_parse_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        # The torn file could have been the one session that ran as radar, so
        # the history is incomplete and nothing is removed -- even scout, whom
        # another session vouches for, is only "kept on doubt": the pass never
        # reached the per-candidate judgement. The marker says why.
        log.session("broken", raw=b"{not json\n")
        log.session("chat-9", "scout")
        report = _run(old_style_config, log)
        assert report.removed == []
        assert report.kept == []
        assert set(report.doubted) == {"radar", "scout"}
        assert "incomplete" in report.doubted["radar"]
        assert "broken.jsonl" in report.doubted["radar"]
        assert report.unreadable_sessions == ["broken.jsonl"]
        after = KiroCrewConfig.load()
        assert "radar" in after.agents and "scout" in after.agents
        marker = json.loads(mig.marker_path().read_text())
        assert marker["unreadable_sessions"] == ["broken.jsonl"]
        assert marker["removed"] == []
        assert set(marker["doubted"]) == {"radar", "scout"}

    def test_a_session_file_that_is_not_utf8_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        log.session("binary", raw=b"\xff\xfe\n")
        report = _run(old_style_config, log)
        assert report.removed == []
        assert set(report.doubted) == {"radar", "scout"}
        assert report.unreadable_sessions == ["binary.jsonl"]

    def test_an_empty_session_file_is_a_torn_write_and_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        # A session file is born with its metadata line; one with no bytes is
        # a write that did not finish, and what it would have said is unknown.
        log.session("torn", raw=b"")
        report = _run(old_style_config, log)
        assert report.removed == []
        assert report.unreadable_sessions == ["torn.jsonl"]
        # The doubt names the incomplete history and the file behind it; the
        # per-file reason is the WARNING line's, not the marker's.
        assert "torn.jsonl" in report.doubted["scout"]

    def test_a_first_line_over_budget_keeps_every_candidate(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        monkeypatch.setattr(mig, "_SESSION_META_LINE_MAX", 32)
        log.session("chat-1", "kirocrew")  # a real metadata line is longer than 32 bytes
        report = _run(old_style_config, log)
        assert report.removed == []
        assert report.unreadable_sessions == ["chat-1.jsonl"]
        assert "chat-1.jsonl" in report.doubted["radar"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_session_file_that_cannot_be_opened_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        path = log.session("locked", "kirocrew")
        path.chmod(0)
        if os.access(path, os.R_OK):
            pytest.skip("file modes are not enforced for this user")
        try:
            report = _run(old_style_config, log)
        finally:
            path.chmod(0o644)
        assert report.removed == []
        assert report.unreadable_sessions == ["locked.jsonl"]
        assert "could not be read" in report.doubted["scout"]

    def test_a_session_that_reads_but_names_another_agent_is_evidence_not_doubt(
        self, old_style_config, bindings_dir, log
    ):
        # The other half of the distinction: a file that READS is history, and
        # history that names someone else -- or nobody, as an older build's
        # metadata record without ``agent`` -- is complete evidence about the
        # candidates. Neither voids the pass; both candidates are judged, and
        # neither is named anywhere, so both go.
        log.session(
            "other",
            raw=json.dumps({"_type": "metadata", "agent": "someone-else"}).encode() + b"\n",
        )
        log.session("old-build", raw=b'{"_type": "metadata", "created_at": "2025-01-01"}\n')
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}
        assert report.doubted == {}
        assert report.unreadable_sessions == []

    def test_a_first_line_that_is_json_but_not_a_record_names_nobody(
        self, old_style_config, bindings_dir, log
    ):
        # Parses (a JSON list), so it is not an unreadable file; it is also not
        # a metadata record, so it names nobody. Evidence, not doubt.
        log.session("odd", raw=b"[1, 2, 3]\n")
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}
        assert report.unreadable_sessions == []

    def test_every_agent_naming_field_of_the_record_counts(
        self, old_style_config, bindings_dir, log
    ):
        # Usage evidence is the union of what the record names, not one field.
        # An interrupted switch can leave ``agent`` and the durable selection
        # apart; a name either holds is a name the session ran as. Here the
        # slot-owned field says the default crew, while ``execution_context``
        # names ``scout`` as the selection and ``radar`` as the template it
        # ran on. Both are chatted; neither is removed.
        record = {
            "_type": "metadata",
            "agent": "kirocrew",
            "execution_context": {
                "member_id": None,
                "store": {"member_id": None, "store": "default"},
                "selection_kind": "template",
                "template_id": "radar",
                "selection_name": "scout",
            },
        }
        log.session("split", raw=json.dumps(record).encode() + b"\n")
        report = _run(old_style_config, log)
        assert report.removed == []
        assert set(report.kept) == {"radar", "scout"}
        assert report.doubted == {}

    def test_an_execution_context_of_the_wrong_shape_names_nobody(
        self, old_style_config, bindings_dir, log
    ):
        # Read defensively: a record whose ``execution_context`` is not a dict,
        # or whose fields are not strings, contributes nothing and voids
        # nothing -- it is a record that reads, so it is evidence.
        record = {"_type": "metadata", "execution_context": ["scout"]}
        log.session("odd-ctx", raw=json.dumps(record).encode() + b"\n")
        record = {"_type": "metadata", "execution_context": {"selection_name": 7}}
        log.session("odd-field", raw=json.dumps(record).encode() + b"\n")
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}
        assert report.unreadable_sessions == []

    def test_a_doubted_pass_is_recorded_and_not_re_run(self, old_style_config, bindings_dir, log):
        bindings_dir("scout", raw=b"{not json")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        report = _run(old_style_config, log)
        assert report.skipped_marker is True

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
    def test_a_fifo_at_a_session_path_is_no_record_and_no_hang(
        self, old_style_config, bindings_dir, log
    ):
        os.mkfifo(log._dir / "trap.jsonl")
        report = _run(old_style_config, log)  # returns: the open does not wait
        assert set(report.removed) == {"radar", "scout"}
        assert report.unreadable_sessions == []

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks")
    def test_a_link_at_a_session_path_is_no_record(
        self, old_style_config, bindings_dir, log, tmp_path
    ):
        real = tmp_path / "elsewhere.jsonl"
        real.write_text(json.dumps({"_type": "metadata", "agent": "scout"}) + "\n")
        os.symlink(real, log._dir / "alias.jsonl")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_a_session_that_vanishes_between_listing_and_open_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        # The listing saw the file; the open did not find it. Whatever it
        # named is gone, and the pass cannot tell it was not the one session
        # that ran as scout -- so the history is incomplete and nothing is
        # removed, the same as a file that would not read. The name is in the
        # marker so the next boot does not re-run the question.
        gone = log.session("chat-7", "scout")
        real = mig._session_agents_named

        def _unlink_then_read(path):
            if path == gone:
                gone.unlink()
            return real(path)

        with patch.object(mig, "_session_agents_named", _unlink_then_read):
            report = _run(old_style_config, log)
        assert report.removed == []
        assert set(report.doubted) == {"radar", "scout"}
        assert report.unreadable_sessions == ["chat-7.jsonl"]
        after = KiroCrewConfig.load()
        assert "radar" in after.agents and "scout" in after.agents
        marker = json.loads(mig.marker_path().read_text())
        assert marker["unreadable_sessions"] == ["chat-7.jsonl"]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
    def test_a_fifo_at_the_activity_path_is_no_record_and_no_hang(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        d = activity_dir("radar").parent  # creates the member dir
        (d / "activity.jsonl").unlink()
        os.mkfifo(d / "activity.jsonl")
        report = _run(old_style_config, log)
        assert "radar" in report.removed

    def test_an_activity_file_over_budget_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.eventlog.service.MAX_LEGACY_ACTIVITY_BYTES", 16)
        activity_dir("scout")  # one row, well over 16 bytes
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "exceeds" in report.doubted["scout"]

    def test_a_session_file_without_metadata_names_nobody(
        self, old_style_config, bindings_dir, log
    ):
        # A first line that reads and parses but is not a metadata record names
        # no agent; that is the same contract list_sessions applies. It is
        # evidence that speaks for nobody, not an unreadable file.
        log.session("plain", raw=b'{"role": "user", "content": "x"}\n')
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}

    def test_no_conversation_log_keeps_every_candidate(self, old_style_config, bindings_dir):
        report = _run(old_style_config, None)
        assert report.removed == []
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_malformed_binding_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        # The roster's own reader answers "not bound" for this file; the prune
        # must not: a damaged member directory is unknown history, not none.
        # Only scout's evidence is in doubt, so only scout is kept on it.
        bindings_dir("scout", raw=b"{not json")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_an_unreadable_binding_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        bindings_dir("scout", raw=b"\xff\xfe")  # not UTF-8
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents

    def test_an_unresolvable_binding_path_keeps_the_crewmate(
        self, old_style_config, activity_dir, monkeypatch, log
    ):
        def _boom(slug):
            raise OSError("members root unreadable")

        monkeypatch.setattr("kiro_crew.members.dm_binding_path", _boom)
        report = _run(old_style_config, log)
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents

    def test_a_binding_path_refused_by_containment_keeps_the_crewmate(
        self, old_style_config, activity_dir, monkeypatch, log
    ):
        # A slug that passed ``member_slug`` but whose binding path resolves
        # outside the trust root (a symlinked component) is a binding that MAY
        # exist, not one that does not.
        from kiro_crew.members import MemberSlugError

        def _escapes(slug):
            raise MemberSlugError(f"member slug {slug!r} escapes root")

        monkeypatch.setattr("kiro_crew.members.dm_binding_path", _escapes)
        report = _run(old_style_config, log)
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents

    def test_an_unparseable_activity_file_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        activity_dir("scout", raw=b"{torn")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]

    def test_a_corrupt_event_log_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log, monkeypatch
    ):
        class _Corrupt:
            def __init__(self, slug):
                self.slug = slug

            def exists(self):
                return self.slug == "scout"

            def iter_events(self):
                raise RuntimeError("committed region unreadable")

        monkeypatch.setattr("kiro_crew.eventlog.log.MemberLog", _Corrupt)
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]

    def test_a_torn_event_log_keeps_that_crewmate(
        self, old_style_config, bindings_dir, activity_dir, log
    ):
        # The store reads a zero-byte segment as "no log" -- what a torn write
        # leaves behind. The prune must not read that as "never chatted": the
        # segment is there and what it held is unknown. Radar, whose log truly
        # does not exist, is judged on the other sources and goes.
        from kiro_crew.crew_log.schema import KIND_MEMBER
        from kiro_crew.crew_log.store import LOG_FILE, crew_log_dir

        unit = crew_log_dir(KIND_MEMBER, "scout")
        unit.mkdir(parents=True, exist_ok=True)
        (unit / LOG_FILE).write_bytes(b"")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "torn" in report.doubted["scout"]

    def test_a_binding_for_another_name_is_not_this_crewmates(
        self, old_style_config, bindings_dir, log
    ):
        # A colliding slug's file names the other crew: scout was never opened.
        bindings_dir("scout", member="someone-else")
        bindings_dir("radar")
        report = _run(old_style_config, log)
        assert report.removed == ["scout"]

    def test_a_doubt_on_one_candidate_does_not_spare_the_next(
        self, old_style_config, bindings_dir, log
    ):
        # Check-then-delete is per candidate: radar's binding is in doubt and
        # radar stays; scout's evidence is clean and scout is judged on it.
        bindings_dir("radar", raw=b"{not json")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["radar"]
        assert report.removed == ["scout"]
        after = KiroCrewConfig.load()
        assert "radar" in after.agents and "scout" not in after.agents

    def test_a_row_that_changed_under_the_lock_is_refused_and_no_marker(
        self, old_style_config, bindings_dir, log
    ):
        bindings_dir("radar")
        # The on-disk row gained a model between the judgement and the lock:
        # newer evidence wins, the delete is refused, and a refusal is not a
        # commit -- no marker, so the next boot re-judges it.
        original = mig.remove_never_chatted

        def _edit_then_remove(cfg_, names, **kw):
            live = KiroCrewConfig.load()
            live.agents["scout"].model = "some-model"
            live.save()
            return original(cfg_, names, **kw)

        with patch.object(mig, "remove_never_chatted", _edit_then_remove):
            report = _run(old_style_config, log)
        assert report.removed == []
        assert report.refused == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].model == "some-model"
        assert not mig.marker_path().exists()

    def test_a_legacy_row_with_fewer_keys_is_still_removed(
        self, old_style_config, bindings_dir, log
    ):
        # A build whose record had no member_id wrote rows without that key;
        # the delete's fence is identity and shape, not key-for-key equality.
        from kiro_crew.config.loader import read_config_for_update, update_config_locked

        def _strip(doc):
            row = doc["agents"]["scout"]
            for key in ("member_id", "starred", "session_color", "avatar", "reasoning_effort"):
                row.pop(key, None)
            return doc

        update_config_locked(mutate=_strip)
        assert "member_id" not in read_config_for_update()["agents"]["scout"]
        bindings_dir("radar")
        report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        assert mig.marker_path().exists()


class TestAbandoned:
    """The gateway's stop signal: a pass told to stop keeps what it has not
    judged, still writes its marker, and never deletes past the check inside
    the config lock."""

    def test_a_pass_told_to_stop_before_judging_keeps_everyone_and_records_it(
        self, old_style_config, bindings_dir, log
    ):
        report = _run(old_style_config, log, abandoned=lambda: True)
        assert report.removed == [] and report.kept == []
        assert report.doubted == {
            "radar": mig.ABANDONED_REASON,
            "scout": mig.ABANDONED_REASON,
        }
        after = KiroCrewConfig.load()
        assert "radar" in after.agents and "scout" in after.agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted
        assert _run(old_style_config, log).skipped_marker is True

    def test_a_stop_that_lands_inside_the_lock_leaves_the_row(self, old_style_config, log):
        # ``remove_never_chatted`` polls twice: once before the row, once inside
        # the config lock right before the delete. The signal lands between the
        # two, which is the last place a delete could still happen.
        _write_specs(old_style_config)
        cfg = KiroCrewConfig.load()
        polls = 0

        def _second_poll_says_stop() -> bool:
            nonlocal polls
            polls += 1
            return polls >= 2

        cand = mig.SyncedCandidate(description="scout agent", filename="scout.json")
        removed, refused, left = mig.remove_never_chatted(
            cfg, {"scout": cand}, abandoned=_second_poll_says_stop
        )
        assert (removed, refused, left) == ([], [], ["scout"])
        assert polls == 2
        assert "scout" in KiroCrewConfig.load().agents

    def test_a_stop_during_the_pass_keeps_the_rest_and_writes_the_marker(
        self, old_style_config, bindings_dir, log
    ):
        import threading

        bindings_dir("radar")
        stop = threading.Event()
        original = mig.remove_never_chatted

        def _stop_then_remove(cfg_, cands, **kw):
            stop.set()
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _stop_then_remove):
            report = _run(old_style_config, log, abandoned=stop.is_set)
        assert report.kept == ["radar"]
        assert report.removed == [] and report.refused == []
        assert report.doubted == {"scout": mig.ABANDONED_REASON}
        assert "scout" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()


class TestCandidates:
    def test_only_untouched_user_spec_rows(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")  # candidate
        cfg.agents["gone"] = _synced("gone")  # spec absent from disk
        cfg.agents["omni"] = KiroCrewAgentConfig(kiro_agent="omni", source="package")
        cfg.agents["own"] = KiroCrewAgentConfig(
            kiro_agent="own", source="builtin"
        )  # kirocrew-owned spec
        cfg.agents["copy"] = KiroCrewAgentConfig(
            kiro_agent="copy", source="builtin"
        )  # private copy
        specs = {
            "radar": _spec("radar"),
            "omni": _spec("omni", filename="Pkg-omni.json", source="package", package="Pkg"),
            "own": _spec("own", kirocrew_owned=True),
            "copy": _spec("copy", private_to="someone"),
        }
        cfg.agents["tuned"] = KiroCrewAgentConfig(kiro_agent="tuned", source="builtin", model="m")
        cfg.agents["routed"] = KiroCrewAgentConfig(
            kiro_agent="routed", source="builtin", triggers="x"
        )
        cfg.agents["renamed"] = _synced("radar")  # name != kiro_agent: not the sync's row
        specs["tuned"] = _spec("tuned")
        specs["routed"] = _spec("routed")
        cfg.save()
        raw = mig._raw_agents_section()
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert list(mig._synced_candidates(cfg, raw, {})) == ["radar"]

    def test_a_row_whose_description_differs_from_the_spec_is_kept(self):
        # The owner rewrote the description (or the spec moved on): either way
        # the row is not what the sync wrote, so it is the owner's.
        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")
        cfg.agents["scout"] = KiroCrewAgentConfig(
            kiro_agent="scout", description="my own words", source="builtin"
        )
        cfg.save()
        cfg = KiroCrewConfig.load()
        raw = mig._raw_agents_section()
        specs = {"radar": _spec("radar"), "scout": _spec("scout")}
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert mig._synced_candidates(cfg, raw, {}) == {
                "radar": mig.SyncedCandidate(description="radar agent", filename="radar.json")
            }

    def test_a_description_edit_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        original = mig.remove_never_chatted

        def _edit_then_remove(cfg_, cands, **kw):
            live = KiroCrewConfig.load()
            live.agents["scout"].description = "renamed by hand"
            live.save()
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _edit_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert KiroCrewConfig.load().agents["scout"].description == "renamed by hand"
        assert not mig.marker_path().exists()

    def test_a_spec_description_edited_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        # Discovery read the spec before the delete; the owner rewrote the
        # SPEC's description in between (a hand edit of the file -- the gate
        # holds every in-app writer). The row still matches the stale snapshot,
        # so only a re-read of the file under the spec lock can tell: the
        # description moved on, that is newer evidence, and the delete is
        # refused. The row and the spec both stay; no marker.
        original = mig.remove_never_chatted

        def _edit_spec_then_remove(cfg_, cands, **kw):
            _spec_path("scout").write_text(
                json.dumps({"name": "scout", "description": "rewritten in the file"})
            )
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _edit_spec_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(_spec_path("scout").read_text())["description"] == (
            "rewritten in the file"
        )
        assert not mig.marker_path().exists()

    def test_a_spec_that_vanished_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        # A spec the delete cannot read as a spec is doubt, and doubt
        # refuses: the row is left for the next boot to judge against whatever
        # is on disk then.
        original = mig.remove_never_chatted

        def _unlink_then_remove(cfg_, cands, **kw):
            # Called once per candidate; the file is gone after the first call.
            _spec_path("scout").unlink(missing_ok=True)
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _unlink_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_a_row_the_overlay_touches_is_never_a_candidate(self):
        # ``kirocrew config set --local agents.radar.model m`` leaves the base
        # row pristine and puts one leaf in config.local.json; deleting the
        # base row would leave that leaf as a crewmate bound to nothing.
        from kiro_crew.config.loader import config_local_path

        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")
        cfg.agents["scout"] = _synced("scout")
        cfg.save()
        config_local_path().write_text(json.dumps({"agents": {"radar": {"model": "m"}}}))
        cfg = KiroCrewConfig.load()
        raw = mig._raw_agents_section()
        overlay = mig._raw_agents_section(config_local_path())
        assert overlay == {"radar": {"model": "m"}}
        specs = {"radar": _spec("radar"), "scout": _spec("scout")}
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert list(mig._synced_candidates(cfg, raw, overlay)) == ["scout"]

    def test_an_overlay_leaf_appearing_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew.config.loader import config_local_path

        original = mig.remove_never_chatted

        def _overlay_then_remove(cfg_, names, **kw):
            config_local_path().write_text(json.dumps({"agents": {"scout": {"model": "m"}}}))
            return original(cfg_, names, **kw)

        with patch.object(mig, "remove_never_chatted", _overlay_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_the_overlay_lock_is_held_until_the_base_delete_has_committed(
        self, old_style_config, bindings_dir, log
    ):
        # The overlay's sidecar lock is released after the base write, not
        # after the peek: at the moment it is released, the base file on disk
        # has already lost the row. An overlay writer that waited on the lock
        # therefore finds the row gone, never a row it can still bind to.
        from kiro_crew.config.loader import config_path

        bindings_dir("radar")
        base_at_release: list[dict] = []
        real = mig._config_write_lock

        @contextlib.contextmanager
        def _recording(p, **kw):
            with real(p, **kw):
                yield
                base_at_release.append(
                    json.loads(config_path().read_text(encoding="utf-8")).get("agents", {})
                )

        with patch.object(mig, "_config_write_lock", _recording):
            report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        # One overlay hold per delete; the base row was gone when it ended.
        assert len(base_at_release) == 1
        assert "scout" not in base_at_release[0]
        assert "radar" in base_at_release[0]

    def test_the_spec_lock_is_held_until_the_base_delete_has_committed(
        self, old_style_config, bindings_dir, log
    ):
        # Same rule for the spec lock: it is released after the base write,
        # not after the re-read. At the moment it is released the base file
        # has already lost the row, so a spec writer that waited on the lock
        # edits a spec no row is bound to -- never one about to be judged
        # against a description it just changed.
        import kiro_crew.agent as agent_mod
        from kiro_crew.config.loader import config_path

        bindings_dir("radar")
        base_at_release: list[dict] = []
        real = agent_mod.agents_spec_lock

        @contextlib.contextmanager
        def _recording(agents_dir):
            with real(agents_dir):
                yield
                base_at_release.append(
                    json.loads(config_path().read_text(encoding="utf-8")).get("agents", {})
                )

        with patch.object(agent_mod, "agents_spec_lock", _recording):
            report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        # One spec hold per delete; the base row was gone when it ended.
        assert len(base_at_release) == 1
        assert "scout" not in base_at_release[0]
        assert "radar" in base_at_release[0]

    def test_the_locks_nest_base_then_overlay_then_spec(self, old_style_config, bindings_dir, log):
        # The order every binding writer keeps, so no writer that holds the
        # base lock can wait on this pass for a lock this pass waits on it for.
        import kiro_crew.agent as agent_mod

        bindings_dir("radar")
        order: list[str] = []
        real_overlay = mig._config_write_lock
        real_spec = agent_mod.agents_spec_lock

        @contextlib.contextmanager
        def _overlay(p, **kw):
            with real_overlay(p, **kw):
                order.append("overlay")
                yield

        @contextlib.contextmanager
        def _spec(agents_dir):
            with real_spec(agents_dir):
                order.append("spec")
                yield

        with (
            patch.object(mig, "_config_write_lock", _overlay),
            patch.object(agent_mod, "agents_spec_lock", _spec),
        ):
            report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        assert order == ["overlay", "spec"]

    def test_a_key_the_record_does_not_declare_disqualifies(self):
        raw = {"kiro_agent": "radar", "description": "d", "source": "builtin", "extra": 1}
        assert not mig._is_fresh_sync_shape(raw, kiro_agent="radar", description="d")

    def test_every_field_including_the_description_is_the_owners_signal(self):
        raw = {"kiro_agent": "radar", "description": "d", "source": "builtin"}

        def shape(r):
            return mig._is_fresh_sync_shape(r, kiro_agent="radar", description="d")

        assert shape(raw)
        assert not shape({**raw, "description": "rewritten"})
        assert not shape({**raw, "starred": True})
        assert not shape({**raw, "avatar": {"kind": "image"}})
        assert not shape({**raw, "workspace": "other"})
        assert not shape({**raw, "member_id": "m1"})
        assert not shape({**raw, "memory_store": "member-x"})


class TestTheGate:
    """``_register_crewmate_prune_gate``: armed before bind, holds writers only.

    A stub ``state`` carrying just the event stands in for ``DashboardState``;
    the gate reads nothing else. The timeout is patched down so the 503 path
    runs in milliseconds.
    """

    @staticmethod
    def _app():
        import asyncio
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.dashboard import server as server_mod

        event = asyncio.Event()
        event.set()
        state = SimpleNamespace(crewmate_prune_settled=event)
        app = web.Application()
        hits: list[str] = []

        async def _write(request):
            hits.append(request.method)
            return web.json_response({"ok": True})

        app.router.add_post("/api/chat/slots/s1/agent", _write)
        app.router.add_get("/api/chat/slots", _write)
        app.router.add_post("/v1/chat/completions", _write)
        app.router.add_get("/api/members", _write)
        app.router.add_get("/api/members/{slug}/activity", _write)
        app.router.add_get("/api/membersx", _write)
        server_mod._register_crewmate_prune_gate(app, state)
        return app, state, hits

    def test_registering_arms_the_barrier(self):
        _app, state, _hits = self._app()
        assert not state.crewmate_prune_settled.is_set()

    @pytest.mark.asyncio
    async def test_a_mutating_api_request_waits_until_the_pass_settles(self):
        import asyncio

        from aiohttp.test_utils import TestClient, TestServer

        app, state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            pending = asyncio.ensure_future(client.post("/api/chat/slots/s1/agent"))
            await asyncio.sleep(0.05)
            assert hits == []  # held, not refused
            state.crewmate_prune_settled.set()
            resp = await pending
            assert resp.status == 200
            assert hits == ["POST"]

    @pytest.mark.asyncio
    async def test_reads_are_never_held(self):
        from aiohttp.test_utils import TestClient, TestServer

        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/chat/slots")).status == 200
        assert hits == ["GET"]

    @pytest.mark.asyncio
    async def test_the_member_roster_read_is_held_too(self):
        # ``GET /api/members`` folds and retires a member's legacy activity file
        # and appends to the member log -- the places the prune reads -- so it
        # waits like a write, and so does every route under the prefix. A path
        # that merely shares the letters does not.
        import asyncio

        from aiohttp.test_utils import TestClient, TestServer

        app, state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            roster = asyncio.ensure_future(client.get("/api/members"))
            activity = asyncio.ensure_future(client.get("/api/members/radar/activity"))
            await asyncio.sleep(0.05)
            assert hits == []
            assert (await client.get("/api/membersx")).status == 200
            assert hits == ["GET"]
            state.crewmate_prune_settled.set()
            assert (await roster).status == 200
            assert (await activity).status == 200
        assert hits == ["GET", "GET", "GET"]

    @pytest.mark.asyncio
    async def test_a_write_outside_api_is_held_too(self, monkeypatch):
        # ``POST /v1/chat/completions`` binds a session's agent like any
        # dashboard route; the gate keys on the method, never on the path.
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import server as server_mod

        monkeypatch.setattr(server_mod, "_CREWMATE_PRUNE_GATE_TIMEOUT_S", 0.05)
        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/v1/chat/completions")
            assert resp.status == 503
        assert hits == []

    @pytest.mark.asyncio
    async def test_a_pass_that_never_settles_answers_503(self, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer

        from kiro_crew.dashboard import server as server_mod

        monkeypatch.setattr(server_mod, "_CREWMATE_PRUNE_GATE_TIMEOUT_S", 0.05)
        app, _state, hits = self._app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/agent")
            assert resp.status == 503
            assert (await resp.json())["code"] == "prune_in_progress"
        assert hits == []

    @pytest.mark.asyncio
    async def test_once_settled_every_request_passes(self):
        from aiohttp.test_utils import TestClient, TestServer

        app, state, hits = self._app()
        state.crewmate_prune_settled.set()
        async with TestClient(TestServer(app)) as client:
            assert (await client.post("/api/chat/slots/s1/agent")).status == 200
        assert hits == ["POST"]


class TestAwaitSettled:
    """``await_crewmate_prune_settled``: the writer-side wait returns only once
    the pass has returned; on timeout it tells the pass to stop and keeps waiting."""

    @staticmethod
    def _state():
        import asyncio
        import threading
        from types import SimpleNamespace

        return SimpleNamespace(
            crewmate_prune_settled=asyncio.Event(),
            crewmate_prune_abandon=threading.Event(),
        )

    @pytest.mark.asyncio
    async def test_a_pass_that_settles_in_time_is_not_told_to_stop(self):
        import asyncio

        from kiro_crew.dashboard import server as server_mod

        state = self._state()
        waiter = asyncio.ensure_future(
            server_mod.await_crewmate_prune_settled(state, before="cron")
        )
        await asyncio.sleep(0.02)
        assert not waiter.done()
        state.crewmate_prune_settled.set()
        await waiter
        assert not state.crewmate_prune_abandon.is_set()

    @pytest.mark.asyncio
    async def test_a_pass_past_its_budget_is_told_to_stop_and_still_waited_for(self, monkeypatch):
        import asyncio

        from kiro_crew.dashboard import server as server_mod

        monkeypatch.setattr(server_mod, "_CREWMATE_PRUNE_GATE_TIMEOUT_S", 0.05)
        state = self._state()
        waiter = asyncio.ensure_future(
            server_mod.await_crewmate_prune_settled(state, before="cron")
        )
        await asyncio.sleep(0.2)
        assert state.crewmate_prune_abandon.is_set()
        assert not waiter.done()  # the writer does not start beside the pass
        state.crewmate_prune_settled.set()
        await asyncio.wait_for(waiter, timeout=1)

    @pytest.mark.asyncio
    async def test_deferred_transcript_removal_runs_only_after_the_pass_returns(self, monkeypatch):
        # The startup merge kept its copies while the pass was running; the
        # removing pass starts only once the event is set, with removal on and
        # the same claimed-slot set, and it is tracked like the pass itself.
        import asyncio

        from kiro_crew.dashboard import server as server_mod

        calls: list[dict] = []

        def _migrate(**kw):
            calls.append(kw)
            return 1

        monkeypatch.setattr(server_mod, "migrate_channel_transcripts", _migrate)
        state = self._state()
        state._background_tasks = set()
        claimed = frozenset({"chat-1"})
        server_mod._kick_deferred_transcript_removal(state, claimed)
        assert len(state._background_tasks) == 1
        await asyncio.sleep(0.05)
        assert calls == []  # nothing removed beside a pass that can still delete
        state.crewmate_prune_settled.set()
        for _ in range(50):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [{"dashboard_slots": claimed, "remove": True}]
        for _ in range(50):
            if not state._background_tasks:
                break
            await asyncio.sleep(0.01)
        assert not state._background_tasks


class TestOneProcess:
    """The whole pass runs under a cross-process lock beside the marker, and the
    marker is created exclusively: two gateways on one data home cannot both
    prune, and the one that did not settles for the other's marker."""

    def test_a_pass_waits_for_the_holder_and_then_finds_its_marker(
        self, old_style_config, bindings_dir, log
    ):
        # "Another process": a second open of the lock file is a second open
        # file description, and the lock is per description, so a thread
        # stands in for it. It holds the lock, finishes its pass, writes the
        # marker, releases. This pass waits, then finds the marker and removes
        # nothing of its own -- the other pass already judged the rows.
        import threading

        from kiro_crew.platform_compat import file_lock, open_lock_file

        bindings_dir("radar")
        holding = threading.Event()
        release = threading.Event()

        def _other_gateway():
            with open_lock_file(mig.lock_path()) as fd, file_lock(fd, exclusive=True):
                holding.set()
                release.wait(5)
                mig._write_marker(mig.PruneReport(removed=["scout"], kept=["radar"]))

        other = threading.Thread(target=_other_gateway)
        other.start()
        assert holding.wait(5)
        threading.Timer(0.2, release.set).start()
        report = _run(old_style_config, log)
        other.join(5)
        assert report.skipped_marker is True
        assert report.lock_waits == 0
        assert report.removed == []
        # The other pass's record stands; this one wrote nothing over it.
        assert json.loads(mig.marker_path().read_text())["removed"] == ["scout"]
        # And the row is still here: the stand-in never deleted it, and this
        # pass, finding the marker, did not either.
        assert "scout" in KiroCrewConfig.load().agents

    def test_a_holder_that_outlasts_the_wait_is_waited_for_not_given_up_on(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        # The contender's writers are held for exactly as long as its pass
        # runs, so a pass that RETURNED with the lock still held would let a
        # session bind a crewmate the holder has not judged yet, and the
        # holder would then delete a row in use. So the wait has no give-up:
        # each expired wait is one WARNING and one more try. Here the holder
        # outlasts several waits, then finishes WITHOUT a marker (it was the
        # kind of pass that judged nothing); the contender then takes the
        # lock and runs the pass itself.
        import threading

        from kiro_crew.platform_compat import file_lock, open_lock_file

        monkeypatch.setattr(mig, "PRUNE_LOCK_WAIT_S", 0.1)
        bindings_dir("radar")
        holding = threading.Event()
        release = threading.Event()

        def _other_gateway():
            with open_lock_file(mig.lock_path()) as fd, file_lock(fd, exclusive=True):
                holding.set()
                release.wait(5)

        other = threading.Thread(target=_other_gateway)
        other.start()
        assert holding.wait(5)
        threading.Timer(0.45, release.set).start()
        report = _run(old_style_config, log)
        other.join(5)
        # It waited through more than one budget rather than returning.
        assert report.lock_waits >= 2
        assert report.skipped_marker is False
        assert report.removed == ["scout"]
        assert report.kept == ["radar"]
        assert mig.marker_path().exists()

    def test_a_marker_that_appears_while_waiting_ends_the_wait(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        # The marker is written last, under the lock: its presence means the
        # holder's deletes are done, so a waiting pass may settle on it even
        # before the holder lets go of the lock.
        from kiro_crew.platform_compat import file_lock, open_lock_file

        monkeypatch.setattr(mig, "PRUNE_LOCK_WAIT_S", 0.1)
        bindings_dir("radar")
        with open_lock_file(mig.lock_path()) as fd, file_lock(fd, exclusive=True):
            mig._write_marker(mig.PruneReport(removed=["scout"], kept=["radar"]))
            report = _run(old_style_config, log)
        assert report.skipped_marker is True
        assert report.lock_waits == 1
        assert report.removed == []
        assert "scout" in KiroCrewConfig.load().agents

    def test_the_marker_is_created_exclusively(self, bindings_dir):
        mig._write_marker(mig.PruneReport(kept=["radar"]))
        with pytest.raises(FileExistsError):
            mig._write_marker(mig.PruneReport(removed=["scout"]))
        marker = json.loads(mig.marker_path().read_text())
        assert marker["kept"] == ["radar"] and marker["removed"] == []

    def test_a_failure_inside_the_pass_is_not_read_as_contention(
        self, old_style_config, bindings_dir, log
    ):
        # Only the lock acquire may report "another process holds the pass";
        # an error from the pass proper is that error, so the gateway logs
        # the real cause instead of a phantom second gateway.
        with patch.object(mig, "_synced_candidates", side_effect=PermissionError("config")):
            with pytest.raises(PermissionError):
                _run(old_style_config, log)
        assert not mig.marker_path().exists()
        assert "scout" in KiroCrewConfig.load().agents

    def test_the_lock_is_released_after_the_pass(self, old_style_config, bindings_dir, log):
        from kiro_crew.platform_compat import file_lock, open_lock_file

        bindings_dir("radar")
        _run(old_style_config, log)
        # A second opener can take it at once: the pass did not leak its hold.
        with open_lock_file(mig.lock_path()) as fd:
            with file_lock(fd, exclusive=True, wait=False):
                pass

    def test_the_lock_is_released_when_the_pass_raises(self, old_style_config, bindings_dir, log):
        from kiro_crew.platform_compat import file_lock, open_lock_file

        with patch.object(mig, "_synced_candidates", side_effect=PermissionError("config")):
            with pytest.raises(PermissionError):
                _run(old_style_config, log)
        with open_lock_file(mig.lock_path()) as fd:
            with file_lock(fd, exclusive=True, wait=False):
                pass

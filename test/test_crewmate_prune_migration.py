"""The one-time startup prune of sync-generated crewmates (``crewmate_prune_migration``).

Seeds old-style ``config.json`` rows beside created and customized crewmates and
asserts that only generated rows bound to installed matching-source specs are
candidates. A candidate's description may differ from its spec; ``member_id``,
private specs, the runtime's own (``kirocrew_owned``) specs, missing specs,
non-string or mismatched sources, ``kirocrew``
rows, non-default protected fields, unknown keys, overlay-owned rows and rows
some team lists keep a
crewmate. Package and legacy ``aim`` sources are equivalent.

The pass removes a candidate only when its Crewmates-page thread has no turn.
A turn in the live transcript or an archived segment keeps it; another session
that ran the agent and a thread opened but never written to do not. Unreadable
bindings, transcript evidence and an unreadable team document fail closed, while
links and FIFOs are no
record. Delete-time checks retain row identity, source identity, overlay
absence, team absence and a matching, non-private, non-runtime-owned installed
spec under their locks. The request
barrier, abandon signal, marker and cross-process lock tests pin the serialization
that makes candidate checks safe.
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
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_path
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


def _synced(name: str, source: str = "builtin") -> KiroCrewAgentConfig:
    # Exactly what an older sync wrote: the spec's description and source.
    return KiroCrewAgentConfig(kiro_agent=name, description=f"{name} agent", source=source)


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


_META = json.dumps({"_type": "metadata", "title": "t", "mode": "member"}) + "\n"
_TURN = json.dumps({"role": "user", "content": "hi"}) + "\n"


class _Log:
    """A conversation log: ``_dir`` holds one ``<key>.jsonl`` per session, the
    first line the metadata record the real log writes."""

    def __init__(self, root: Path):
        self._dir = root
        root.mkdir(parents=True, exist_ok=True)

    def session(self, key: str, agent: str | None = None):
        """A session elsewhere -- a plain chat, a spawn, a cron run -- that ran ``agent``."""
        meta: dict = {"_type": "metadata", "title": key}
        if agent:
            meta["agent"] = agent
        path = self._dir / f"{key}.jsonl"
        path.write_text(json.dumps(meta) + "\n" + _TURN)
        return path

    def dm(
        self,
        slug: str,
        *,
        turns: bool = True,
        raw: bytes | None = None,
        archived: str = "",
        slot_key: str = "",
    ):
        """The Crewmates-page thread of ``slug``: metadata plus a turn by default."""
        stem = f"dashboard_{slot_key or 'member-' + slug}"
        if archived:
            path = self._dir / "archive" / f"{stem}__{archived}.jsonl"
            path.parent.mkdir(exist_ok=True)
        else:
            path = self._dir / f"{stem}.jsonl"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(_META + (_TURN if turns else ""))
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

    def write(
        name: str,
        *,
        member: str | None = None,
        raw: bytes | None = None,
        slot_key: str = "",
    ):
        path = root / f"{name}.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(
                json.dumps({"slot_key": slot_key or f"member-{name}", "member": member or name})
            )
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
        bindings_dir("radar")
        log.dm("radar")  # the owner chatted with radar on the Crewmates page
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

    def test_records_bound_to_a_removed_row_stay_executable(
        self, old_style_config, bindings_dir, log
    ):
        """Removing a row must never strand the records that picked it.

        Chats, forks, subagent runs and cron jobs that picked a synced crewmate
        persist a ``member`` record naming it. After the pass removes the row,
        each such record must decode as the installed template on the shared
        store -- otherwise every reader refuses it as an unavailable member.
        """
        from kiro_crew.execution_context import (
            EXECUTION_CONTEXT_KEY,
            ExecutionContext,
            MemoryStoreRef,
            execution_from_record,
        )

        report = _run(old_style_config, log)
        assert report.removed
        for name in report.removed:
            record = ExecutionContext(
                None, MemoryStoreRef("default"), "member", name, selection_name=name
            )
            decoded = execution_from_record({EXECUTION_CONTEXT_KEY: record.to_record()})
            assert decoded.selection_kind == "template", name
            assert decoded.template_id == name
            assert decoded.store.store_id == "default"
            assert _spec_path(name).exists()

    def test_a_session_that_ran_the_agent_elsewhere_does_not_count(
        self, old_style_config, bindings_dir, log
    ):
        # A plain chat, a spawn, a cron run or an app's own slot used the AGENT,
        # which stays installed; only the crewmate's own thread keeps the row.
        log.session("chat-2", "scout")
        log.session("dashboard_chat-3", "radar")
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}
        assert _spec_path("scout").exists()

    def test_a_thread_opened_but_never_written_to_does_not_count(
        self, old_style_config, bindings_dir, log
    ):
        # Clicking the crewmate in the roster writes the binding and at most a
        # metadata line; nobody chatted.
        bindings_dir("radar")
        log.dm("radar", turns=False)
        bindings_dir("scout")
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}

    def test_an_archived_segment_of_the_thread_counts(self, old_style_config, bindings_dir, log):
        # Compaction moved the turns into an archive segment; the live file
        # holds only the metadata line.
        log.dm("scout", turns=False)
        log.dm("scout", archived="20260901-195926")
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]
        assert report.removed == ["radar"]

    def test_a_missing_live_transcript_without_an_archive_is_no_turn(
        self, old_style_config, bindings_dir, log
    ):
        live = log._dir / "dashboard_member-scout.jsonl"
        assert not live.exists()
        assert mig._transcript_has_a_turn(live) is False

        report = _run(old_style_config, log)

        assert "scout" in report.removed

    def test_another_crewmates_archive_is_not_this_ones(self, old_style_config, bindings_dir, log):
        # ``member-scout`` is a prefix of ``member-scout-2``; the segment
        # delimiter keeps them apart.
        log.dm("scout-2", archived="20260901-195926")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_the_bindings_recorded_slot_key_is_read_too(self, old_style_config, bindings_dir, log):
        bindings_dir("scout", slot_key="member-scout-v2abc")
        log.dm("scout", slot_key="member-scout-v2abc")
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    def test_a_binding_for_another_name_does_not_lend_its_thread(
        self, old_style_config, bindings_dir, log
    ):
        # A colliding slug's binding names the other crew: its thread is not
        # scout's, so its turns do not keep scout.
        bindings_dir("scout", member="someone-else", slot_key="member-elsewhere")
        log.dm("x", slot_key="member-elsewhere")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_an_older_builds_first_line_that_is_a_message_counts(
        self, old_style_config, bindings_dir, log
    ):
        log.dm("scout", raw=_TURN.encode())
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    def test_sync_rows_with_matching_sources_are_removed(self, bindings_dir, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["builtin"] = _synced("builtin", source="builtin")
        cfg.agents["omni"] = _synced("omni", source="package")
        cfg.agents["legacy"] = _synced("legacy", source="aim")
        cfg.save()
        specs = {
            "builtin": _spec("builtin"),
            "omni": _spec("omni", filename="Pkg-omni.json", source="package", package="Pkg"),
            "legacy": _spec("legacy", filename="Pkg-legacy.json", source="package", package="Pkg"),
        }
        report = _run(specs, log)
        assert set(report.removed) == {"builtin", "omni", "legacy"}

    def test_an_edited_description_without_a_dm_turn_is_removed(self, bindings_dir, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["scout"] = KiroCrewAgentConfig(
            kiro_agent="scout", description="owner wording", source="package"
        )
        cfg.save()
        report = _run(
            {"scout": _spec("scout", filename="Pkg-scout.json", source="package", package="Pkg")},
            log,
        )
        assert report.removed == ["scout"]
        assert "scout" not in KiroCrewConfig.load().agents

    def test_a_spec_less_row_is_never_removed(self, bindings_dir, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["empty"] = KiroCrewAgentConfig(
            kiro_agent="empty", description="", source="package"
        )
        cfg.agents["described"] = KiroCrewAgentConfig(
            kiro_agent="described", description="owner wording", source="package"
        )
        cfg.save()
        report = _run({}, log)
        assert report.removed == []
        assert {"empty", "described"} <= KiroCrewConfig.load().agents.keys()

    def test_rows_bound_to_skill_view_aliases_are_removed(self, bindings_dir, log):
        # Discovery never lists a ``kirocrew-skill-view-*`` alias, so no spec is
        # reported for these rows; the older sync still wrote one per alias file.
        a = "kirocrew-skill-view-000d98d7ce0f52f0500da355"
        b = "kirocrew-skill-view-0245aff77b1473735250d2a0"
        cfg = KiroCrewConfig.load()
        cfg.save()
        raw = json.loads(config_path().read_text())
        raw["agents"][a] = {
            "member_id": "",
            "kiro_agent": a,
            "workspace": "default",
            "memory_store": "default",
            "model": "",
            "display_name": "",
            "description": "One-click setup agent -- managed by AIM.",
            "triggers": "",
            "source": "builtin",
            "starred": False,
        }
        raw["agents"][b] = {
            "member_id": "",
            "kiro_agent": b,
            "workspace": "default",
            "memory_store": "default",
            "description": "Communication agent -- managed by AIM.",
            "triggers": "",
            "source": "builtin",
            "starred": False,
        }
        config_path().write_text(json.dumps(raw))
        report = _run({}, log)
        assert set(report.removed) == {a, b}
        assert not {a, b} & KiroCrewConfig.load().agents.keys()

    def test_a_skill_view_row_the_owner_chatted_with_or_claimed_is_kept(self, bindings_dir, log):
        chatted = "kirocrew-skill-view-" + "1" * 24
        created = "kirocrew-skill-view-" + "2" * 24
        tuned = "kirocrew-skill-view-" + "3" * 24
        runtime = "kirocrew-skill-view-" + "4" * 24
        plain = "kirocrew-skill-view-" + "5" * 24
        cfg = KiroCrewConfig.load()
        for name in (chatted, created, tuned, plain):
            cfg.agents[name] = _synced(name)
        cfg.agents[created].member_id = "member-created"
        cfg.agents[tuned].model = "m"
        cfg.agents[runtime] = _synced(runtime, source="kirocrew")
        cfg.save()
        log.dm(chatted)
        report = _run({}, log)
        assert report.removed == [plain] and report.refused == []
        assert {chatted, created, tuned, runtime} <= KiroCrewConfig.load().agents.keys()

    @pytest.mark.parametrize(
        "name",
        [
            "kirocrew-skill-view-mine",
            "kirocrew-skill-view-" + "a" * 23,
            "kirocrew-skill-view-" + "a" * 25,
            "kirocrew-skill-view-" + "A" * 24,
            "kirocrew-skill-view-" + "g" * 24,
        ],
    )
    def test_a_prefixed_row_that_is_not_an_exact_alias_name_needs_a_spec(
        self, bindings_dir, log, name
    ):
        # Only the name the projection writes (prefix + 24 lowercase hex) is
        # judged on the row alone; any other tail is an ordinary row, and with
        # no installed spec it is kept.
        cfg = KiroCrewConfig.load()
        cfg.agents[name] = _synced(name)
        cfg.save()
        report = _run({}, log)
        assert report.removed == []
        assert name in KiroCrewConfig.load().agents

    def test_a_created_crewmate_is_never_removed(self, bindings_dir, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["created"] = _synced("created")
        cfg.agents["created"].member_id = "member-created"
        cfg.save()
        report = _run({"created": _spec("created")}, log)
        assert report.removed == []
        assert KiroCrewConfig.load().agents["created"].member_id == "member-created"

    def test_second_boot_is_a_no_op(self, old_style_config, bindings_dir, log):
        log.dm("radar")
        _run(old_style_config, log)
        cfg = KiroCrewConfig.load()
        cfg.agents["late"] = _synced("late")
        cfg.save()
        specs = dict(old_style_config, late=_spec("late"))
        report = _run(specs, log)
        assert report.skipped_marker is True
        assert "late" in KiroCrewConfig.load().agents

    def test_the_first_passes_marker_does_not_stop_this_one(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew.config.paths import config_dir

        earlier = [
            config_dir() / "crewmate_prune_migrated.json",
            config_dir() / "crewmate_prune_v2_migrated.json",
        ]
        for marker in earlier:
            marker.write_text(json.dumps({"removed": [], "kept": [], "doubted": {}}))
        report = _run(old_style_config, log)
        assert report.skipped_marker is False
        assert set(report.removed) == {"radar", "scout"}
        assert all(marker.exists() for marker in earlier)

    def test_a_no_op_pass_still_writes_the_marker(self, bindings_dir, log):
        report = _run({}, log)
        assert report.removed == [] and report.kept == []
        assert mig.marker_path().exists()


class TestKeptOnDoubt:
    """A candidate whose binding or thread transcript is there but cannot be
    judged is kept; the others are judged on their own evidence. No
    conversation log keeps everyone. The pass still finishes and the marker
    names the doubted, so no boot re-runs it."""

    def test_an_archive_directory_that_cannot_be_listed_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        log.dm("radar")
        archive = log._dir / "archive"
        archive.mkdir()
        real_scandir = os.scandir

        def _unreadable(path):
            if isinstance(path, (str, os.PathLike)) and Path(path) == archive:
                raise PermissionError("archive unreadable")
            return real_scandir(path)

        monkeypatch.setattr(mig.os, "scandir", _unreadable)
        report = _run(old_style_config, log)

        assert report.removed == []
        assert report.kept == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "could not list archived transcripts" in report.doubted["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_a_listed_archive_segment_that_disappears_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        segment = log.dm("scout", archived="20260901-195926")
        real_open = mig.open_file_no_reparse

        def _remove_then_open(path, *, nonblocking=False):
            if Path(path) == segment:
                segment.unlink()
            return real_open(path, nonblocking=nonblocking)

        monkeypatch.setattr(mig, "open_file_no_reparse", _remove_then_open)
        report = _run(old_style_config, log)

        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "disappeared before it could be opened" in report.doubted["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_a_thread_whose_first_line_does_not_parse_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        log.dm("scout", raw=b"{not json\n")
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_a_thread_that_is_not_utf8_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        log.dm("scout", raw=b"\xff\xfe\n")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]

    def test_an_empty_thread_is_a_torn_write_and_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        log.dm("scout", raw=b"")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "empty" in report.doubted["scout"]

    def test_a_first_line_over_budget_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        monkeypatch.setattr(mig, "_TRANSCRIPT_META_LINE_MAX", 16)
        log.dm("scout")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]

    def test_a_torn_row_after_the_metadata_still_counts(self, old_style_config, bindings_dir, log):
        # A row that was being written proves a turn was sent.
        log.dm("scout", raw=_META.encode() + b'{"role": "us')
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    def test_a_row_past_the_read_cap_still_counts(
        self, old_style_config, bindings_dir, log, monkeypatch
    ):
        # A row too long to buffer is still a written turn, never "no turn".
        monkeypatch.setattr(mig, "_TRANSCRIPT_META_LINE_MAX", len(_META) + 8)
        log.dm(
            "scout", raw=_META.encode() + b'{"role": "user", "content": "' + b"x" * 256 + b'"}\n'
        )
        report = _run(old_style_config, log)
        assert report.kept == ["scout"]

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="POSIX permission bits; root reads any file",
    )
    def test_a_thread_that_cannot_be_opened_keeps_that_crewmate(
        self, old_style_config, bindings_dir, log
    ):
        path = log.dm("scout")
        path.chmod(0)
        try:
            report = _run(old_style_config, log)
        finally:
            path.chmod(0o644)
        assert list(report.doubted) == ["scout"]
        assert report.removed == ["radar"]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
    def test_a_fifo_at_a_thread_path_is_no_record_and_no_hang(
        self, old_style_config, bindings_dir, log
    ):
        os.mkfifo(log._dir / "dashboard_member-scout.jsonl")
        report = _run(old_style_config, log)  # returns: the open does not wait
        assert set(report.removed) == {"radar", "scout"}

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks")
    def test_a_link_at_a_thread_path_is_no_record(
        self, old_style_config, bindings_dir, log, tmp_path
    ):
        real = tmp_path / "elsewhere.jsonl"
        real.write_text(_META + _TURN)
        os.symlink(real, log._dir / "dashboard_member-scout.jsonl")
        report = _run(old_style_config, log)
        assert "scout" in report.removed

    def test_a_doubted_pass_is_recorded_and_not_re_run(self, old_style_config, bindings_dir, log):
        bindings_dir("scout", raw=b"{not json")
        report = _run(old_style_config, log)
        assert list(report.doubted) == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        report = _run(old_style_config, log)
        assert report.skipped_marker is True

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
        # must not: the binding may record the thread's slot key.
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
        self, old_style_config, bindings_dir, monkeypatch, log
    ):
        def _boom(slug):
            raise OSError("members root unreadable")

        monkeypatch.setattr("kiro_crew.members.dm_binding_path", _boom)
        report = _run(old_style_config, log)
        assert set(report.doubted) == {"radar", "scout"}
        assert "scout" in KiroCrewConfig.load().agents

    def test_a_binding_path_refused_by_containment_keeps_the_crewmate(
        self, old_style_config, bindings_dir, monkeypatch, log
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
        log.dm("radar")
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
        log.dm("radar")
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

        cand = mig.SyncedCandidate(filename="scout.json", source="builtin")
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

        log.dm("radar")
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
    def test_only_matching_builtin_package_and_aim_sources_are_candidates(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")
        cfg.agents["omni"] = _synced("omni", source="package")
        cfg.agents["older"] = _synced("older", source="aim")
        cfg.agents["runtime"] = KiroCrewAgentConfig(kiro_agent="runtime", source="kirocrew")
        # The runtime's own helper specs read as ``builtin`` -- discovery keeps
        # ``kirocrew_owned`` apart from ``source`` -- and a sync-shaped row
        # bound to one is still never a candidate.
        cfg.agents["kirocrew-conductor"] = _synced("kirocrew-conductor")
        # Never a candidate: a private copy, a row the owner tuned or routed, a
        # row whose name is not its binding, or a source the sync rule excludes.
        cfg.agents["copy"] = KiroCrewAgentConfig(kiro_agent="copy", source="builtin")
        cfg.agents["tuned"] = KiroCrewAgentConfig(kiro_agent="tuned", source="builtin", model="m")
        cfg.agents["routed"] = KiroCrewAgentConfig(
            kiro_agent="routed", source="builtin", triggers="x"
        )
        cfg.agents["renamed"] = _synced("radar")
        cfg.agents["odd"] = KiroCrewAgentConfig(kiro_agent="odd", source="somewhere")
        specs = {
            "radar": _spec("radar"),
            "omni": _spec("omni", filename="Pkg-omni.json", source="package", package="Pkg"),
            "older": _spec("older", source="package", package="Pkg"),
            "runtime": _spec("runtime", source="kirocrew", kirocrew_owned=True),
            "kirocrew-conductor": _spec("kirocrew-conductor", kirocrew_owned=True),
            "copy": _spec("copy", private_to="someone"),
            "tuned": _spec("tuned"),
            "routed": _spec("routed"),
            "odd": _spec("odd"),
        }
        cfg.save()
        raw = mig._raw_agents_section()
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            got = mig._synced_candidates(cfg, raw, {}, frozenset())
        assert list(got) == ["radar", "omni", "older"]
        assert got["omni"] == mig.SyncedCandidate(filename="Pkg-omni.json", source="package")
        assert got["older"].source == "package"

    def test_a_spec_less_row_is_not_a_candidate(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["gone"] = KiroCrewAgentConfig(
            kiro_agent="gone", description="any wording", source="package"
        )
        cfg.save()
        assert mig._synced_candidates(cfg, mig._raw_agents_section(), {}, frozenset()) == {}

    def test_a_non_string_row_source_is_kept_without_exception(self, log):
        from kiro_crew.config.loader import update_config_locked

        cfg = KiroCrewConfig.load()
        cfg.agents["scout"] = _synced("scout")
        cfg.save()

        def _break_source(doc):
            doc["agents"]["scout"]["source"] = []
            return doc

        update_config_locked(mutate=_break_source)
        report = _run({"scout": _spec("scout")}, log)
        assert report.removed == [] and report.refused == []
        assert "scout" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_source_mismatch_is_kept(self, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["scout"] = _synced("scout", source="builtin")
        cfg.save()
        spec = _spec("scout", source="package", package="Pkg")
        report = _run({"scout": spec}, log)
        assert report.removed == [] and report.refused == []
        assert "scout" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_an_aim_row_bound_to_a_package_spec_is_a_candidate(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["legacy"] = _synced("legacy", source="aim")
        cfg.save()
        spec = _spec("legacy", source="package", package="Pkg")
        with patch("kiro_crew.agent_discovery.list_agents", return_value=[spec]):
            got = mig._synced_candidates(cfg, mig._raw_agents_section(), {}, frozenset())
        assert got == {"legacy": mig.SyncedCandidate(filename="legacy.json", source="package")}

    def test_a_kirocrew_row_is_kept(self, log):
        cfg = KiroCrewConfig.load()
        cfg.agents["runtime"] = KiroCrewAgentConfig(kiro_agent="runtime", source="kirocrew")
        cfg.save()
        spec = _spec("runtime", source="kirocrew", kirocrew_owned=True)
        report = _run({"runtime": spec}, log)
        assert report.removed == [] and report.refused == []
        assert "runtime" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_row_bound_to_a_runtime_owned_builtin_spec_is_kept(self, bindings_dir, log):
        # The conductor, worker, knowledge, research and heartbeat specs are
        # ``builtin``-sourced but ``kirocrew_owned``: the flag is kept apart
        # from ``source`` in discovery, so the source rule alone would let a
        # sync-shaped row bound to one through. It is never a candidate, so
        # with no DM turn it is neither removed nor refused, and the pass
        # records itself as complete.
        cfg = KiroCrewConfig.load()
        cfg.agents["kirocrew-conductor"] = _synced("kirocrew-conductor")
        cfg.agents["scout"] = _synced("scout")
        cfg.save()
        specs = {
            "kirocrew-conductor": _spec("kirocrew-conductor", kirocrew_owned=True),
            "scout": _spec("scout"),
        }
        report = _run(specs, log)
        assert report.removed == ["scout"] and report.refused == []
        assert "kirocrew-conductor" in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_spec_that_became_runtime_owned_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        # The bound file is now one of ``OWNED_KIRO_AGENT_FILES``: discovery
        # still derives ``builtin`` from it (the stem is not ``<pkg>-scout``),
        # so only the ``kirocrew_owned`` re-check can refuse.
        specs = dict(old_style_config)
        specs["scout"] = _spec("scout", filename="kirocrew-conductor.json")
        original = mig.remove_never_chatted

        def _replace_spec_then_remove(cfg_, cands, **kw):
            (Path(kiro_agents_dir_path()) / "kirocrew-conductor.json").write_text(
                json.dumps({"name": "scout", "description": "now the conductor"})
            )
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _replace_spec_then_remove):
            report = _run(specs, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_an_unchanged_spec_identity_under_the_lock_still_allows_delete(
        self, old_style_config, bindings_dir, log
    ):
        original = mig.remove_never_chatted

        def _edit_description_then_remove(cfg_, cands, **kw):
            _spec_path("scout").write_text(
                json.dumps({"name": "scout", "description": "rewritten in the file"})
            )
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _edit_description_then_remove):
            report = _run(old_style_config, log)
        assert "scout" in report.removed
        assert "scout" not in KiroCrewConfig.load().agents
        assert json.loads(_spec_path("scout").read_text())["description"] == (
            "rewritten in the file"
        )
        assert mig.marker_path().exists()

    def test_a_spec_renamed_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        original = mig.remove_never_chatted

        def _rename_spec_then_remove(cfg_, cands, **kw):
            _spec_path("scout").write_text(json.dumps({"name": "renamed-scout"}))
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _rename_spec_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_a_spec_made_private_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew import agent_state

        original = mig.remove_never_chatted

        def _make_spec_private_then_remove(cfg_, cands, **kw):
            agent_state.set_fork_info("scout", forked_from="shared-scout", private_to="owner")
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _make_spec_private_then_remove):
            report = _run(old_style_config, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert not mig.marker_path().exists()

    def test_a_spec_replaced_by_a_kirocrew_source_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        specs = dict(old_style_config)
        specs["scout"] = _spec("scout", filename="kirocrew.json")
        original = mig.remove_never_chatted

        def _replace_spec_then_remove(cfg_, cands, **kw):
            (Path(kiro_agents_dir_path()) / "kirocrew.json").write_text(
                json.dumps({"name": "scout", "description": "runtime replacement"})
            )
            return original(cfg_, cands, **kw)

        with patch.object(mig, "remove_never_chatted", _replace_spec_then_remove):
            report = _run(specs, log)
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
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
            assert list(mig._synced_candidates(cfg, raw, overlay, frozenset())) == ["scout"]

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

    def test_a_teamed_row_is_never_a_candidate(self):
        # Placing a crewmate on a team is the owner's own act, so the row is
        # the owner's whatever its shape.
        from kiro_crew import crew_teams

        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = _synced("radar")
        cfg.agents["scout"] = _synced("scout")
        cfg.save()
        crew_teams.create_team("ops", ["radar"], known=lambda: {"radar", "scout"})
        teamed = mig._teamed_names()
        assert teamed == frozenset({"radar"})
        specs = {"radar": _spec("radar"), "scout": _spec("scout")}
        with (
            patch("kiro_crew.agent_discovery.list_agents", return_value=list(specs.values())),
            patch("kiro_crew.agent.kiro_agents_dir_path", return_value="/nowhere"),
        ):
            assert list(mig._synced_candidates(cfg, mig._raw_agents_section(), {}, teamed)) == [
                "scout"
            ]

    def test_a_teamed_never_chatted_generated_row_is_kept(
        self, old_style_config, bindings_dir, log
    ):
        # Neither removed, refused nor doubted: not a candidate at all, so the
        # pass records itself as complete with the row in place.
        from kiro_crew import crew_teams

        crew_teams.create_team("ops", ["scout"], known=lambda: {"radar", "scout"})
        report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.refused == [] and report.doubted == {}
        assert "scout" in KiroCrewConfig.load().agents
        assert [t.members for t in crew_teams.read_teams()] == [["scout"]]
        assert mig.marker_path().exists()

    def test_an_unteamed_row_is_still_removed_when_teams_exist(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew import crew_teams

        crew_teams.create_team("ops", ["by-hand"], known=lambda: {"by-hand"})
        report = _run(old_style_config, log)
        assert set(report.removed) == {"radar", "scout"}
        assert "scout" not in KiroCrewConfig.load().agents
        assert mig.marker_path().exists()

    def test_a_row_teamed_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        # The team write lands between discovery and the delete: newer
        # evidence, the delete is refused, no marker, the row and its team stay.
        from kiro_crew import crew_teams

        original = mig.remove_never_chatted

        def _team_then_remove(cfg_, names, **kw):
            # Called once per candidate; the team is made on the first call.
            if not crew_teams.read_teams():
                crew_teams.create_team("ops", ["scout"], known=lambda: {"radar", "scout"})
            return original(cfg_, names, **kw)

        with patch.object(mig, "remove_never_chatted", _team_then_remove):
            report = _run(old_style_config, log)
        assert report.removed == ["radar"]
        assert report.refused == ["scout"]
        assert "scout" in KiroCrewConfig.load().agents
        assert [t.members for t in crew_teams.read_teams()] == [["scout"]]
        assert not mig.marker_path().exists()

    def test_an_unreadable_team_document_keeps_every_candidate(
        self, old_style_config, bindings_dir, log
    ):
        # None of them can be shown to be off a team, so all are kept on doubt
        # -- the way no conversation log keeps them -- and the marker records it.
        from kiro_crew import crew_teams

        path = crew_teams.teams_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        report = _run(old_style_config, log)
        assert report.removed == [] and report.refused == []
        assert set(report.doubted) == {"radar", "scout"}
        assert all(mig.TEAMS_UNREADABLE_REASON in why for why in report.doubted.values())
        assert {"radar", "scout"} <= KiroCrewConfig.load().agents.keys()
        assert json.loads(mig.marker_path().read_text())["doubted"] == report.doubted

    def test_a_team_document_turned_unreadable_under_the_lock_refuses_the_delete(
        self, old_style_config, bindings_dir, log
    ):
        from kiro_crew import crew_teams

        original = mig.remove_never_chatted

        def _corrupt_then_remove(cfg_, names, **kw):
            path = crew_teams.teams_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json")
            return original(cfg_, names, **kw)

        with patch.object(mig, "remove_never_chatted", _corrupt_then_remove):
            report = _run(old_style_config, log)
        assert set(report.refused) == {"radar", "scout"}
        assert not mig.marker_path().exists()

    def test_the_team_lock_is_held_until_the_base_delete_has_committed(
        self, old_style_config, bindings_dir, log
    ):
        # Same rule as the overlay and spec locks: at the moment the team
        # document lock is released the base file has already lost the row, so
        # a team write that waited on it cannot place a name the pass is about
        # to delete.
        from kiro_crew.config.loader import config_path

        log.dm("radar")
        base_at_release: list[dict] = []
        real = mig.document_lock

        @contextlib.contextmanager
        def _recording(directory=None):
            with real(directory):
                yield
                base_at_release.append(
                    json.loads(config_path().read_text(encoding="utf-8")).get("agents", {})
                )

        with patch.object(mig, "document_lock", _recording):
            report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        assert len(base_at_release) == 1
        assert "scout" not in base_at_release[0]
        assert "radar" in base_at_release[0]

    def test_the_overlay_lock_is_held_until_the_base_delete_has_committed(
        self, old_style_config, bindings_dir, log
    ):
        # The overlay's sidecar lock is released after the base write, not
        # after the peek: at the moment it is released, the base file on disk
        # has already lost the row. An overlay writer that waited on the lock
        # therefore finds the row gone, never a row it can still bind to.
        from kiro_crew.config.loader import config_path

        log.dm("radar")
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

        log.dm("radar")
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

    def test_the_locks_nest_base_then_overlay_then_spec_then_teams(
        self, old_style_config, bindings_dir, log
    ):
        # The order every binding writer keeps, then the team document lock
        # innermost (its own contract is registry lock first, then it, and
        # nothing takes a registry, overlay or spec lock while holding it), so
        # no writer that holds the base lock can wait on this pass for a lock
        # this pass waits on it for.
        import kiro_crew.agent as agent_mod

        log.dm("radar")
        order: list[str] = []
        real_overlay = mig._config_write_lock
        real_spec = agent_mod.agents_spec_lock
        real_teams = mig.document_lock

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

        @contextlib.contextmanager
        def _teams(directory=None):
            with real_teams(directory):
                order.append("teams")
                yield

        with (
            patch.object(mig, "_config_write_lock", _overlay),
            patch.object(agent_mod, "agents_spec_lock", _spec),
            patch.object(mig, "document_lock", _teams),
        ):
            report = _run(old_style_config, log)
        assert report.removed == ["scout"]
        assert order == ["overlay", "spec", "teams"]

    def test_a_key_the_record_does_not_declare_disqualifies(self):
        raw = {"kiro_agent": "radar", "description": "d", "source": "builtin", "extra": 1}
        assert not mig._is_fresh_sync_shape(raw, kiro_agent="radar")

    def test_protected_fields_disqualify(self):
        raw = {"kiro_agent": "radar", "description": "d", "source": "builtin"}

        def shape(r):
            return mig._is_fresh_sync_shape(r, kiro_agent="radar")

        assert shape(raw)
        assert shape({**raw, "description": "rewritten"})
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

        log.dm("radar")
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
        log.dm("radar")
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
        log.dm("radar")
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

        log.dm("radar")
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

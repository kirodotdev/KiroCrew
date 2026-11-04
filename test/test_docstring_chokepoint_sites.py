"""Call-site pins for four rules their helpers declare only in prose.

Each helper below states a rule in its own docstring that every site doing the same
job is meant to follow, and nothing else enforces it, so a new caller can miss it
without anything going red. Each class pins the named sites to the helper:

| Rule | Helper | Sites pinned here |
|---|---|---|
| a cut never splits a grapheme cluster | ``imessage.plaintext._grapheme_boundary`` / ``_grapheme_end`` | ``computer_use.render._clip``, ``ops_mission_control`` ``notify_out._clip``, ``dashboard.chat_title._title_reveal_prefixes`` |
| a download name is sent as RFC 5987 ``filename*=UTF-8''`` | ``dashboard.session_export.content_disposition`` | ``GET /api/knowledge/export`` |
| a temp-write-and-rename uses a unique temp name | ``atomic_write.atomic_write`` | ``auto_research.subquestion_queue.save_queue`` |
| column alignment is measured in display columns, not ``len`` | ``messaging.tables.display_width`` | ``kirocrew knowledge stats`` |

Two kinds of assertion. A spy on the module's own binding of the helper proves the
site still reaches it. A behavioural assertion proves what the helper buys: the
occupied fixed temp name, the non-ASCII download name and the CJK column each fail
on the hand-rolled form the site would otherwise have.
"""

from __future__ import annotations

import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from knowledge_handlers_coverage_helpers import _client, _make_app

import kiro_crew.apps.builtins.auto_research.subquestion_queue as sq
import kiro_crew.apps.builtins.ops_mission_control.backend.notify_out as notify_out
import kiro_crew.computer_use.render as render
import kiro_crew.dashboard.chat_title as chat_title
import kiro_crew.dashboard.handlers.knowledge_bundles as knowledge_bundles
from kiro_crew.dashboard.session_export import content_disposition
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.messaging.tables import display_width

FAMILY = "👨\u200d👩\u200d👧"


def _recording(original, calls: list):
    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    return spy


class TestGraphemeCutSites:
    def test_render_clip_reaches_the_shared_boundary(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            render, "_grapheme_boundary", _recording(render._grapheme_boundary, calls)
        )

        assert render._clip("AB" + FAMILY + "CD", 3) == "AB…"
        assert calls, "computer_use.render._clip cut without the shared grapheme boundary"

    def test_notify_clip_reaches_the_shared_boundary(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            notify_out, "_grapheme_boundary", _recording(notify_out._grapheme_boundary, calls)
        )

        assert notify_out._clip("AB" + FAMILY + "CD", 4) == "AB…"
        assert calls, "notify_out._clip cut without the shared grapheme boundary"

    def test_title_reveal_reaches_the_shared_cluster_end(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            chat_title, "_grapheme_end", _recording(chat_title._grapheme_end, calls)
        )

        prefixes = chat_title._title_reveal_prefixes("今日は良い天気ですね")

        assert prefixes, "a ten-character unspaced title should reveal in steps"
        assert calls, "_title_reveal_prefixes cut without the shared cluster end"


class TestKnowledgeExportDownloadName:
    @pytest.fixture
    def store(self, tmp_path):
        s = KnowledgeStore(str(tmp_path / "kb.db"))
        yield s
        s._close_all_for_tests()

    async def _export(self, store, namespace: str) -> str:
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/export", params={"namespace": namespace})
            assert resp.status == 200
            return resp.headers["Content-Disposition"]

    @pytest.mark.asyncio
    async def test_a_non_ascii_namespace_is_sent_percent_encoded(self, store):
        """The bare ``filename=`` form puts raw UTF-8 bytes in the header."""
        store.add_item("a", "body", "note", namespace="研究")

        disposition = await self._export(store, "研究")

        assert disposition == content_disposition("研究.knowledge")
        assert disposition == "attachment; filename*=UTF-8''" + urllib.parse.quote(
            "研究.knowledge", safe=""
        )

    @pytest.mark.asyncio
    async def test_the_export_reaches_the_shared_helper(self, store, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            knowledge_bundles,
            "content_disposition",
            _recording(knowledge_bundles.content_disposition, calls),
        )
        store.add_item("a", "body", "note", namespace="work")

        await self._export(store, "work")

        assert calls == [("work.knowledge",)]


class TestSubquestionQueueSave:
    def test_save_routes_through_atomic_write(self, tmp_path, monkeypatch):
        calls: list = []
        monkeypatch.setattr(sq, "atomic_write", _recording(sq.atomic_write, calls))

        sq.save_queue(tmp_path, sq.new_queue())

        assert [str(args[0]) for args in calls] == [str(tmp_path / sq.QUEUE_FILENAME)]

    def test_an_occupied_fixed_temp_name_does_not_stop_the_save(self, tmp_path):
        """A second writer's staging file at ``<name>.tmp`` is what a fixed name collides with.

        The collision is expressed deterministically as a directory at that name: a
        hand-rolled ``write_text`` to it raises ``IsADirectoryError`` (Windows:
        ``PermissionError``), while ``atomic_write`` stages under a unique name.
        """
        (tmp_path / (sq.QUEUE_FILENAME + ".tmp")).mkdir()
        queue = sq.new_queue()

        sq.save_queue(tmp_path, queue)

        assert sq.load_queue(tmp_path) == queue
        leftovers = sorted(p.name for p in tmp_path.iterdir())
        assert leftovers == sorted([sq.QUEUE_FILENAME, sq.QUEUE_FILENAME + ".tmp"])

    def test_the_payload_is_unchanged(self, tmp_path):
        queue = sq.new_queue()

        sq.save_queue(tmp_path, queue)

        assert json.loads((tmp_path / sq.QUEUE_FILENAME).read_text(encoding="utf-8")) == queue


class TestKnowledgeStatsColumns:
    NAMES = ("研究ノート", "alpha", "café")

    @pytest.fixture
    def home(self, tmp_path):
        db_path = tmp_path / "workspace" / "knowledge" / "knowledge.db"
        db_path.parent.mkdir(parents=True)
        store = KnowledgeStore(str(db_path))
        for name in self.NAMES:
            source_id = store.add_source(name, "local_folder", str(tmp_path / "src" / name))
            store.add_item(
                title=f"{name}-doc",
                content="body",
                item_type="document",
                source_id=source_id,
                chunk_index=0,
            )
        store._close_all_for_tests()
        return tmp_path

    def _table(self, home, capsys) -> list[str]:
        from kiro_crew import cli

        with (
            patch("kiro_crew.cli.config_dir", return_value=home),
            patch("kiro_crew.cli.sel", return_value=MagicMock()),
        ):
            cli._knowledge(SimpleNamespace(knowledge_action="stats", json=False))
        lines = capsys.readouterr().out.splitlines()
        start = next(i for i, line in enumerate(lines) if line.lstrip().startswith("SOURCE"))
        return lines[start:]

    def test_the_count_columns_align_in_display_columns(self, home, capsys):
        """A two-column CJK name padded by ``len`` pushes its counts right."""
        table = self._table(home, capsys)

        assert len(table) == 1 + len(self.NAMES)
        widths = {display_width(line.rstrip()) for line in table}
        assert len(widths) == 1, table

    def test_every_source_has_a_row(self, home, capsys):
        table = self._table(home, capsys)

        for name in self.NAMES:
            assert any(line.strip().startswith(name) for line in table[1:]), name

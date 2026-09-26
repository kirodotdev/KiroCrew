"""Load-time contract of ``session.compaction_method``.

The compaction coordinator walks down from ``compaction_method`` (a ceiling: the
chosen method, then every weaker one, ``native`` last), so a hand-edited config
must never hand it a name it has no step for. It normalizes at load, in
``config.sections._compaction_method``. There is deliberately no second key
sizing the tail a rotation carries: that figure is the session replay's own
window-scaled budget (``context_budget.replay_budget_chars``), so the digest a rotation
writes and the tail the successor replays can never disagree. And there is no
order list: the methods form one line of strength, so a list could only spell
out orders the ladder already implies or dead letters.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config import sections
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session_compaction_methods import rotation_ladder


def _load(tmp_path: Path, session: dict) -> KiroCrewConfig:
    """Load a config whose ``session`` section is *session*, hermetically."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"session": session}), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


class TestCompactionMethod:
    def test_default_is_native(self, tmp_path: Path) -> None:
        """An omitted key loads the module default, so a config that never set
        it keeps today's behaviour: the runtime's own in-place compaction."""
        cfg = _load(tmp_path, {})
        assert cfg.session.compaction_method == sections.DEFAULT_COMPACTION_METHOD == "native"

    def test_known_name_is_kept(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, {"compaction_method": "shake"})
        assert cfg.session.compaction_method == "shake"

    def test_a_cased_or_padded_name_is_not_accepted(self, tmp_path: Path) -> None:
        """The field carries an enum, so schema validation removes anything but
        the exact names before section extraction; the fallback is native."""
        cfg = _load(tmp_path, {"compaction_method": " Shake "})
        assert cfg.session.compaction_method == "native"

    @pytest.mark.parametrize(
        "raw",
        ["snapcompact", "", 42, ["soft"], {"native": True}, None],
        ids=["unknown", "empty", "int", "list", "dict", "null"],
    )
    def test_anything_else_falls_back_to_native(self, tmp_path: Path, raw: object) -> None:
        """The coordinator always gets a ceiling it knows. A list (the shape an
        earlier draft of this key had) is not honoured: a stray one means native,
        never a silently different ladder."""
        cfg = _load(tmp_path, {"compaction_method": raw})
        assert cfg.session.compaction_method == "native"

    def test_the_default_is_a_known_method(self) -> None:
        assert sections.DEFAULT_COMPACTION_METHOD in sections.COMPACTION_METHODS

    def test_the_field_enum_is_the_method_tuple(self) -> None:
        """The settings UI offers exactly the names the coordinator implements."""
        field = sections.SessionConfig.__dataclass_fields__["compaction_method"]
        assert field.metadata["enum"] == list(sections.COMPACTION_METHODS)

    def test_the_help_names_each_value_and_its_outcome_before_the_mechanics(self) -> None:
        """A reader picking a value needs the per-value consequences first; the
        ladder ("the strongest allowed", then which method hands off to which) is
        one sentence after them, and no clause promises a compaction that a
        running turn would defer."""
        help_text = sections.SessionConfig.__dataclass_fields__["compaction_method"].metadata[
            "help"
        ]
        clauses = ["native (default):", "soft:", "shake:"]
        positions = [help_text.index(clause) for clause in clauses]
        assert positions == sorted(positions), "one clause per value, weakest first"
        mechanics = help_text.index("The value is the strongest method allowed")
        assert positions[-1] < mechanics, "per-value outcomes come before the ladder"
        assert "kept as a summary" in help_text and "gone from the agent's memory" in help_text
        assert "condensed into the agent's memory" in help_text
        assert "the transcript stays on screen" in help_text
        assert "defers the attempt" in help_text and "always compacted" not in help_text
        # "would not free enough hands to" garden-paths on "free enough hands".
        # A shake that cannot write its digest skips soft (the ladder breaks to
        # native), so the help may not promise "the next weaker one".
        assert "shake to soft, soft to native" in help_text
        assert "one that cannot run skips to native" in help_text
        assert "next weaker one" not in help_text
        assert "enough hands" not in help_text
        assert len(help_text.split()) <= 125


class TestTheLadderIsDerivedNotConfigured:
    def test_methods_are_ordered_weakest_first(self) -> None:
        assert sections.COMPACTION_METHODS == ("native", "soft", "shake")

    @pytest.mark.parametrize(
        ("ceiling", "expected"),
        [("native", ()), ("soft", ("soft",)), ("shake", ("shake", "soft")), ("bogus", ())],
    )
    def test_rotation_ladder_walks_down_to_soft(self, ceiling: str, expected: tuple) -> None:
        """Strongest first, ``native`` excluded because the coordinator always
        runs it last on its own."""
        assert rotation_ladder(ceiling) == expected

    def test_there_is_no_order_key(self, tmp_path: Path) -> None:
        cfg = _load(tmp_path, {"compaction_method_order": ["shake", "soft"]})
        assert not hasattr(cfg.session, "compaction_method_order")
        assert cfg.session.compaction_method == "native"


class TestNoSeparateTailKey:
    def test_the_tail_is_not_a_config_key(self, tmp_path: Path) -> None:
        """A second tail figure is what would open a gap between digest and replay."""
        cfg = _load(tmp_path, {"compaction_keep_recent_tokens": 32000})
        assert not hasattr(cfg.session, "compaction_keep_recent_tokens")
        assert not hasattr(sections, "DEFAULT_COMPACTION_KEEP_RECENT_TOKENS")
        # The stray key is ignored like any unknown session key: load still works.
        assert cfg.session.compaction_method == "native"


class TestTheSettingIsWritableFromTheDashboard:
    """Settings > Chat > Advanced offers the method beside the threshold, so the
    key must clear the dashboard's PATCH allowlist (``_EDITABLE_CONFIG``); a
    key the allowlist omits is refused as ``field not editable`` however the
    schema describes it. The value set is the load path's own tuple."""

    KEY = "session.compaction_method"

    def test_the_allowlist_entry_is_an_enum_over_the_method_tuple(self) -> None:
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert _EDITABLE_CONFIG[self.KEY] == {
            "type": "enum",
            "values": list(sections.COMPACTION_METHODS),
        }

    @staticmethod
    def _app():
        from aiohttp import web

        from kiro_crew.dashboard.handlers.core import api_kirocrew_config_patch

        app = web.Application()
        app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
        return app

    @pytest.mark.asyncio
    async def test_a_known_method_is_stored_and_loads_back(self, tmp_path: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"session": {}}), encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(self._app())) as client:
                resp = await client.patch(
                    "/api/config/kirocrew", json={"path": self.KEY, "value": "soft"}
                )
                assert resp.status == 200, await resp.text()
            assert json.loads(cfg_file.read_text(encoding="utf-8"))["session"] == {
                "compaction_method": "soft"
            }
            assert KiroCrewConfig.load().session.compaction_method == "soft"

    @pytest.mark.asyncio
    async def test_an_unknown_method_is_refused_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        cfg_file = tmp_path / "config.json"
        before = json.dumps({"session": {"compaction_method": "shake"}})
        cfg_file.write_text(before, encoding="utf-8")
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(self._app())) as client:
                resp = await client.patch(
                    "/api/config/kirocrew", json={"path": self.KEY, "value": "Shake"}
                )
                assert resp.status == 400
                assert "invalid value" in (await resp.json())["error"]
        assert cfg_file.read_text(encoding="utf-8") == before

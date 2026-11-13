"""Round-trip contracts: the conductor's probe scripts read the REAL writers.

The scripts re-implement readers for formats this package writes: the transcript JSONL
entry (``ConversationLog.append``), the ``dashboard_<slot>.jsonl`` filename
(``_history_key_for`` then ``_safe_key``), and the usage token row
(``persist_token_record``).

Hand-written fixtures cannot see another module change its format, and a drifted
reader marks a running session GONE so its item is dispatched twice. Each test here
drives the real writer and lets the script classify what it produced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline_conductor_fleet_probe_helpers import LOAD_NAME
from skill_script_helpers import load_skill_script

from kiro_crew.acp.types import TurnUsage
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.handlers import usage as usage_mod
from kiro_crew.history import ConversationLog

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "pipeline-conductor"
)


def _probe(tmp_path: Path, monkeypatch, sessions: list[str]):
    """The fleet-probe module plus a config, with the script's derived paths
    (``$KIROCREW_HOME/sessions``, the ``/proc`` seam) pointed into the tmp
    tree -- same harness shape as ``test_pipeline_conductor_agent``."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(tmp_path / "no-proc"))
    cfg_path = tmp_path / "probe-config.json"
    cfg_path.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
    mod = load_skill_script(LOAD_NAME, SKILL_DIR / "scripts" / "fleet_probe.py")
    return mod, cfg_path


class TestTranscriptShapeRoundTrip:
    def test_probe_classifies_what_conversation_log_wrote(self, tmp_path, capsys, monkeypatch):
        """Format 1: the JSONL entry shape.

        Entries come from ``ConversationLog.append`` into the ``<data home>/sessions``
        directory the probe derives, so a changed entry shape misclassifies here.
        """
        mod, cfg = _probe(tmp_path, monkeypatch, ["s-real-writer"])
        log = ConversationLog(base_dir=tmp_path / "crew" / "sessions")
        log.append("s-real-writer", "user", "seed")
        log.append("s-real-writer", "assistant", "GREEN: PR #12 head abc123")
        assert mod.main(["--config", str(cfg)]) == 0
        out = capsys.readouterr().out
        assert "s-real-writer" in out and "GREEN" in out
        assert "GONE" not in out
        assert "OK 1 watched, 1 fired" in out


class TestDeliveryPatternRoundTrip:
    """Format 4: the phrases the delivery counters look for.

    ``DEFAULT_WATCHDOG_RES`` and ``DEFAULT_INIT_TIMEOUT_RES`` match text other modules
    emit, so they are checked against the real constants: a reworded emitter would
    read zero and a stalled fleet would report healthy.
    """

    def test_watchdog_patterns_match_the_constants_the_gateway_emits(self):
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
        from kiro_crew.dashboard.state import (
            STALE_RECOVERY_PREFIX,
            TOOL_STALL_RECOVERY_PREFIX,
        )

        mod = load_skill_script(LOAD_NAME, SKILL_DIR / "scripts" / "fleet_probe.py")
        patterns = [re.compile(rx) for rx in mod.DEFAULT_WATCHDOG_RES]
        for emitted in (
            f"{TOOL_STALL_RECOVERY_PREFIX} resume from your last committed step",
            f"{STALE_RECOVERY_PREFIX} resume from your last committed step",
            f"turn ended: {STOP_REASON_TOOL_STALL}",
        ):
            assert any(rx.search(emitted) for rx in patterns), emitted

    def test_the_index_needle_matches_what_the_real_writer_emits(self, tmp_path, monkeypatch):
        """The tail index counts session-produced rows with a BYTE needle over the
        whole file, so it depends on how the writer spells the role field. Drive
        the real writer and count what the script would count: a reworded
        separator would understate the index and make a talking session read as
        deadlocked."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        mod = load_skill_script(LOAD_NAME, SKILL_DIR / "scripts" / "fleet_probe.py")
        log = ConversationLog(base_dir=tmp_path / "crew" / "sessions")
        log.append("s-needle", "user", "seed")  # inbound: must NOT count
        log.append("s-needle", "assistant", "WORKING: one")
        log.append("s-needle", "assistant", "WORKING: two")
        path = tmp_path / "crew" / "sessions" / "s-needle.jsonl"
        _, index = mod._tail_entries(path, 200_000)
        assert index == 1, "two assistant rows, zero-based, and the user row excluded"


class TestFilenamePrefixRoundTrip:
    def test_raw_slot_key_finds_the_transcript_the_dashboard_writes(
        self, tmp_path, capsys, monkeypatch
    ):
        """Format 2: the ``dashboard_`` filename prefix.

        The file is named the way production names it (``_history_key_for`` then
        ``_safe_key``); the probe, given only the RAW slot key, must still find it.
        """
        slot = "chat-7-1756700000"
        mod, cfg = _probe(tmp_path, monkeypatch, [slot])
        log = ConversationLog(base_dir=tmp_path / "crew" / "sessions")
        history_key = _history_key_for(slot)
        log.append(history_key, "user", "seed")
        log.append(history_key, "assistant", "GREEN: PR #12 head abc123")
        assert mod.main(["--config", str(cfg)]) == 0
        out = capsys.readouterr().out
        assert "GONE" not in out, "raw slot key must not read as a missing session"
        assert slot in out and "GREEN" in out


class TestUsageShardRoundTrip:
    class _Event:
        """Event double carrying a REAL ``TurnUsage`` -- the shape
        ``_build_token_record`` reads its billing dimensions from."""

        def __init__(self, usage: TurnUsage) -> None:
            self.usage = usage

    def test_credit_spend_sums_what_the_recorder_wrote(self, tmp_path, capsys, monkeypatch):
        """Format 3: the token-row schema.

        Rows come from ``persist_token_record``. Production rows are per-turn with a
        literal ``turns: 0`` field, so accepted rows ARE turns.
        """
        shard_dir = tmp_path / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        slot = "chat-7-1756700000"
        usage_mod.persist_token_record(slot, "model-x", self._Event(TurnUsage(credits=2.5)))
        usage_mod.persist_token_record(slot, "model-x", self._Event(TurnUsage(credits=1.5)))
        usage_mod.persist_token_record(
            "other-slot", "model-x", self._Event(TurnUsage(credits=99.0))
        )
        mod = load_skill_script("credit_spend", SKILL_DIR / "scripts" / "credit_spend.py")
        rc = mod.main(["--slots", slot, "--budget", "100", "--usage-dir", str(shard_dir)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["slots"][slot]["credits"] == 4.0
        assert out["slots"][slot]["turns"] == 2  # two recorded turns = two rows
        assert "unmetered" not in out["slots"][slot]
        assert out["truncated"] is False
        assert out["verdict"] == "within"

    def test_a_slot_the_recorder_never_wrote_is_unmetered(self, tmp_path, capsys, monkeypatch):
        """Negative half of the same contract: real shards on disk, a watched
        slot with no recorded row -- the verdict must be ``unmetered``, never
        a silent zero-spend ``within``."""
        shard_dir = tmp_path / "tokens"
        monkeypatch.setattr(usage_mod, "_TOKEN_USAGE_DIR", shard_dir)
        usage_mod.persist_token_record("other-slot", "model-x", self._Event(TurnUsage(credits=1.0)))
        mod = load_skill_script("credit_spend", SKILL_DIR / "scripts" / "credit_spend.py")
        rc = mod.main(["--slots", "never-ran", "--budget", "100", "--usage-dir", str(shard_dir)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["verdict"] == "unmetered"

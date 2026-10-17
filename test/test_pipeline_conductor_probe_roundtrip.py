"""Round-trip contracts: the conductor's probe scripts read the REAL writers.

The two pipeline-conductor scripts re-implement readers for three on-disk
formats this package itself writes:

1. the session-transcript JSONL entry shape -- written by
   ``kiro_crew.history.ConversationLog.append``;
2. the ``dashboard_`` transcript-filename prefix -- a dashboard slot key
   becomes ``dashboard:<slot>`` via ``chat_utils._history_key_for`` and then
   the ``dashboard_<slot>.jsonl`` stem via ``history._safe_key``;
3. the usage-shard token-row schema -- written by
   ``dashboard.handlers.usage._build_token_record`` /
   ``_write_token_record`` (via ``persist_token_record``).

The sibling tests in ``test_pipeline_conductor_agent.py`` exercise the scripts
over HAND-AUTHORED fixtures. A fixture authored to match a format another
module owns cannot detect that module changing the format: drift keeps every
fixture test green while the live probe silently misclassifies -- and the
conductor's reclaim decision runs off that classification, so the failure mode
is a running session read as GONE and its work item dispatched twice.

Each test here drives the real writer and lets the script classify what the
writer produced, so the writer changing its format reds a test. Mutation-
verified for all three formats (see the PR): renaming the transcript
``content`` field, changing ``_history_key_for``'s prefix, and renaming the
token row's ``_type`` each red exactly these tests while every fixture test
stays green.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
    return mod, cfg_path


class TestTranscriptShapeRoundTrip:
    def test_probe_classifies_what_conversation_log_wrote(self, tmp_path, capsys, monkeypatch):
        """Format 1: the JSONL entry shape.

        ``fleet_probe._classify`` walks entries for ``role``/``content`` and
        tags the last assistant line. Here those entries come from the real
        writer -- ``ConversationLog.append`` into the same
        ``<data home>/sessions`` directory the probe derives -- so a change to
        the persisted entry shape (field names, content encoding, the
        metadata header line) surfaces as a misclassification here.
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

    ``DEFAULT_WATCHDOG_RES`` and ``DEFAULT_INIT_TIMEOUT_RES`` are regexes over
    text OTHER modules emit. A fixture that hand-copies the phrase cannot notice
    the emitter rewording it: every fixture test stays green, the counters
    silently read zero, and a fleet that cannot deliver reports as healthy --
    which is the one failure these counters exist to make visible. So the
    patterns are matched against the real constants.
    """

    def test_watchdog_patterns_match_the_constants_the_gateway_emits(self):
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
        from kiro_crew.dashboard.state import (
            STALE_RECOVERY_PREFIX,
            TOOL_STALL_RECOVERY_PREFIX,
        )

        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
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
        mod = load_skill_script("fleet_probe", SKILL_DIR / "scripts" / "fleet_probe.py")
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

        The conductor watches raw slot keys while the store prefixes the
        surface. Derive the on-disk name the way production does -- the slot
        key through ``_history_key_for`` (``dashboard:<slot>``), folded to a
        filename stem by ``ConversationLog``'s own ``_safe_key`` -- and the
        probe, given only the RAW slot key, must still find and classify the
        transcript. A false GONE here is the reclaim-and-double-dispatch
        failure mode, so the assertion is spelled both ways.
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

        Rows come from the real recorder (``persist_token_record`` ->
        ``_build_token_record`` -> ``_write_token_record``), so a renamed
        field, a changed ``_type``, or the per-row ``turns: 0`` contract
        changing all surface here. The turns assertion pins the row-counting
        contract the script documents: production rows are per-turn with a
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

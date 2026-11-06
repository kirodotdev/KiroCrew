"""Every batch transcription caller holds the configured model's claim.

``DELETE /api/stt/models/{name}`` refuses a claimed model, and ``_transcribe_local``
claims only from the decode onward. A caller that reads the STT config and then
awaits anything before ``transcribe_audio`` (an upload, a transcode, a duration
probe) must hold ``local_model_claim`` across that span itself, or a model the user
just deselected can be removed in between: the decode then re-downloads it, or loses
the audio when offline.

Nothing in the type system enforces that, so this pins the list of modules that call
``transcribe_audio`` outside the ``transcribe`` package. A new caller fails here until
it holds the claim and is added below with the test that proves it does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group(name="tree_scan_stt_model_claim_callers")

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: Module -> the test that shows it holds the claim across its own awaits.
CLAIMED_CALLERS = {
    "apps/builtins/meetings/backend/routes/audio_import.py": (
        "test_meetings_audio_import.py::test_a_local_import_claims_its_model_from_config_to_transcript"
    ),
    "dashboard/handlers/core_stt_speech.py": (
        "test_stt_model_delete.py::test_transcribe_route_claims_its_model_for_the_whole_request"
    ),
    "messaging/attachments.py": (
        "test_stt_model_delete.py::test_channel_attachments_claim_the_model_before_the_duration_probe"
    ),
    "slack/events_voice.py": (
        "test_stt_model_delete.py::test_slack_voice_memos_claim_the_model_before_the_duration_probe"
    ),
}

#: The ``transcribe`` package itself: it defines ``transcribe_audio`` and claims inside it.
_OWN = re.compile(r"^transcribe(_[a-z]+)?\.py$")
_CALL = re.compile(r"\btranscribe_audio\(")


def _callers() -> dict[str, str]:
    """Each module under ``src/kiro_crew`` that calls ``transcribe_audio``, with its text."""
    found: dict[str, str] = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if _OWN.match(rel):
            continue
        text = path.read_text(encoding="utf-8")
        if _CALL.search(text) and not re.search(r"\bdef transcribe_audio\(", text):
            found[rel] = text
    return found


def test_every_transcribe_audio_caller_is_a_known_claim_holder() -> None:
    callers = _callers()
    assert callers, "the scan found no caller of transcribe_audio -- it went blind"
    assert sorted(callers) == sorted(CLAIMED_CALLERS), (
        "a module now calls transcribe_audio without being listed here. Hold "
        "transcribe.local_model_claim(stt_config) from the config read through the "
        "decode, add a test that the model is claimed during its awaits, and list it"
    )


def test_every_listed_caller_takes_the_claim() -> None:
    for rel, text in _callers().items():
        # An awaited config read must claim across the read, which only the
        # snapshot helper does; a caller that loads on the loop may claim after it.
        awaited_read = re.search(r"await asyncio\.to_thread\(\s*(\w+\.)?load_stt_config", text)
        assert (
            "claimed_stt_snapshot" in text or "local_model_claim" in text
        ), f"{rel} calls transcribe_audio without a model claim"
        assert (
            not awaited_read
        ), f"{rel} reads the STT config off the loop; use claimed_stt_snapshot(load_stt_config)"


def test_every_listed_proof_exists() -> None:
    tests = Path(__file__).resolve().parent
    for rel, proof in CLAIMED_CALLERS.items():
        file, name = proof.split("::")
        source = (tests / file).read_text(encoding="utf-8")
        assert re.search(rf"\bdef {re.escape(name)}\(", source), f"{rel}: {proof} is missing"

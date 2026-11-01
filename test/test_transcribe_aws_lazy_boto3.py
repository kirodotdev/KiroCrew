"""``import kiro_crew.transcribe_aws`` must not load ``boto3``.

The module is on the dashboard server's import chain (``handlers → files →
messaging → core → stt_stream → transcribe → transcribe_aws``), so a
module-scope ``import boto3`` costs every gateway start ~200 ms wherever the
``voice-aws`` extra is installed, even when the configured STT provider is the
local recogniser and no AWS call is ever made. ``boto3`` is imported on the
first attribute access instead, and a partial install whose import fails still
reads as the missing extra.

The import check runs in a fresh interpreter: the test process has long since imported
``boto3`` through other tests, so an in-process ``sys.modules`` assertion would
be vacuous.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import stt, transcribe_aws
from kiro_crew.config.loader import SttConfig


@pytest.mark.skipif(
    importlib.util.find_spec("boto3") is None,
    reason="boto3 is not installed, so there is no import to defer",
)
def test_import_does_not_load_boto3(tmp_path: Path) -> None:
    code = (
        "import sys; "
        "import kiro_crew.transcribe_aws; "
        "print(repr(('boto3' in sys.modules, 'botocore' in sys.modules)))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        cwd=tmp_path,
    )
    assert res.returncode == 0, f"import kiro_crew.transcribe_aws failed:\n{res.stderr}"
    loaded = ast.literal_eval(res.stdout.strip().splitlines()[-1])
    assert loaded == (False, False), (
        "import kiro_crew.transcribe_aws loaded boto3/botocore at module scope; "
        "it must stay deferred until an AWS Transcribe call needs it"
    )


@pytest.mark.skipif(
    importlib.util.find_spec("boto3") is None or transcribe_aws.boto3 is None,
    reason="the voice-aws extra is not installed",
)
def test_deferred_boto3_resolves_to_the_real_module() -> None:
    import boto3

    assert transcribe_aws.boto3 is not None
    assert transcribe_aws.boto3.Session is boto3.Session


# ── A partial install: boto3 is found, but importing it fails ─────────────
#
# The module-scope check only finds the package, so each gate that decides "the
# extra is missing" must see the failed import itself. ``sys.modules[name] = None``
# makes ``import boto3`` raise ImportError on any host, with or without the extra.


@pytest.fixture
def broken_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_aws, "boto3", transcribe_aws._DeferredBoto3())
    monkeypatch.setitem(sys.modules, "boto3", None)


@pytest.mark.usefixtures("broken_boto3")
def test_a_boto3_that_fails_to_import_reads_as_the_missing_extra() -> None:
    from kiro_crew.dashboard.handlers import core_stt_settings

    detail = transcribe_aws._aws_availability()
    assert (detail.ok, detail.code) == (False, stt.CODE_EXTRA_MISSING)
    # The Settings panel's repair-command switch reads the same answer.
    assert core_stt_settings._transcribe_extra_importable() is False


@pytest.mark.usefixtures("broken_boto3")
def test_a_boto3_that_fails_to_import_is_a_vocabulary_list_failure() -> None:
    with pytest.raises(transcribe_aws.VocabularyListError) as raised:
        transcribe_aws.list_custom_vocabularies("", "us-east-1")
    assert raised.value.code == transcribe_aws.VOCABULARIES_LIST_FAILED


@pytest.mark.asyncio
@pytest.mark.usefixtures("broken_boto3")
async def test_a_boto3_that_fails_to_import_stops_batch_transcription(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew import aws_consent

    async def _granted(*args, **kwargs):
        return True

    def _no_client():
        raise AssertionError("the streaming client must not load once boto3 failed to import")

    monkeypatch.setattr(aws_consent, "refuse_and_log", _granted)
    monkeypatch.setattr(transcribe_aws, "_load_aws_transcribe_components", _no_client)
    audio = tmp_path / "memo.ogg"
    audio.write_bytes(b"OggS")
    cfg = SttConfig(enabled=True, provider="transcribe")
    assert await transcribe_aws._transcribe_aws(str(audio), cfg) is None

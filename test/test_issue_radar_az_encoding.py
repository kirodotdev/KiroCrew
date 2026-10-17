"""``_az_run`` must decode ``az`` output as UTF-8, not as the host code page.

``_az_run`` is Issue Radar's single spawn chokepoint for every ``az`` call, and
what comes back through it is ``az devops invoke`` JSON: work-item titles,
descriptions and comments, written by people. Non-ASCII is the normal case there,
not an edge one.

The call used ``text=True`` with no ``encoding=``, so that JSON was decoded with
``locale.getpreferredencoding(False)`` — cp950 / cp932 / cp1252 on a Windows
console code page. Two distinct failures follow, and both are asserted below:

* a code page that **cannot** decode the bytes raises ``UnicodeDecodeError``,
  which is neither ``FileNotFoundError`` nor ``TimeoutExpired``. It escapes both
  handlers in ``_az_run``, so the caller never sees a ``ProviderCliError`` and the
  route answers an opaque 500 instead of its own 502 contract;
* a code page that decodes *most* bytes (cp1252) fails the other way, and does it
  inconsistently: five byte values are undefined there, so the same title may raise
  on one work item and come back mojibaked on the next.

Decoding is only one end of the contract. ``encoding=`` says how the parent READS
the pipe; it cannot change what the child WRITES, and ``az`` is a Python program
whose stdout to a pipe uses ``locale.getpreferredencoding(False)`` unless
``PYTHONIOENCODING`` says otherwise. A character the child's code page cannot
encode leaves it as a ``?`` before the parent ever sees a byte, so the last tests
below pin the child end in the spawn environment.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import azure_client, azure_transport

# The autouse fixture below stubs ``azure_client._az_env`` to ``{}`` for the
# decoding tests, which are not about the environment. Capture the real one at
# import time so the env tests can put it back for their own spawn.
_REAL_AZ_ENV = azure_client._az_env

# A work item as Azure DevOps actually returns one, with prose in it.
WORK_ITEM = {
    "id": 4211,
    "fields": {
        "System.Title": "Café にログインできない — 認証が 401 を返す",
        "System.Description": "<div>Größe: naïve</div>",
    },
}
RAW_JSON = json.dumps(WORK_ITEM, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _stub_chokepoint_preflight(monkeypatch: pytest.MonkeyPatch):
    """Neutralise the host/binary/audit preflight, which is not under test here.

    ``_az_run`` re-resolves the host, re-validates the binary and writes the
    ``invoked`` audit before it spawns; none of that touches decoding, and all of
    it needs a configured host. Stubbing it keeps these tests about the one thing
    they are about.
    """
    monkeypatch.setattr(azure_client, "_resolve_host", lambda host: host)
    monkeypatch.setattr(azure_client, "_az_bin", lambda: "az")
    monkeypatch.setattr(azure_client, "_az_env", lambda host: {})
    monkeypatch.setattr(azure_client, "_audit", lambda *a, **k: None)


def _decoding_run(host_encoding: str, payload: bytes = RAW_JSON.encode()):
    """A ``subprocess.run`` stand-in that decodes the way ``subprocess`` documents.

    The bytes are what ``az`` writes; the only variable is the codec. ``subprocess``
    uses the ``encoding=`` keyword when one is given and
    ``locale.getpreferredencoding(False)`` when it is not, so the interesting code
    page is a parameter here rather than a property of the test runner — these are
    deterministic on a UTF-8 CI box and on a cp950 laptop alike.
    """

    def _fake(argv, **kwargs):
        codec = kwargs.get("encoding") or host_encoding
        return subprocess.CompletedProcess(argv, 0, payload.decode(codec), "")

    return _fake


@pytest.mark.parametrize("host_encoding", ["cp950", "ascii"])
def test_work_item_json_survives_a_non_utf8_host_codepage(host_encoding: str) -> None:
    """The load-bearing one: the JSON must come back byte-for-byte as az wrote it.

    ``cp950`` is a real shipped Windows code page and ``ascii`` is the degenerate
    POSIX ``LC_ALL=C`` case; both fail on the same bytes, from opposite directions.
    """
    with patch.object(subprocess, "run", side_effect=_decoding_run(host_encoding)):
        proc = azure_client._az_run(["az", "devops", "invoke"], host="dev.azure.com", timeout=30)

    assert proc.stdout == RAW_JSON, "az's JSON did not survive decoding"
    # The consequence, not just the string: the caller parses this.
    assert json.loads(proc.stdout)["fields"]["System.Title"] == (
        WORK_ITEM["fields"]["System.Title"]
    )


def test_a_codepage_that_silently_mojibakes_is_also_refused() -> None:
    """The inconsistent half, and why ``errors=`` would not be a fix.

    cp1252 decodes most bytes to *something* and leaves five undefined, so the same
    UTF-8 title can raise on one work item and come back as garbage on the next —
    which is worse to diagnose than a clean failure. Asserting on the decoded text
    refuses both outcomes with one assertion, where ``errors="replace"`` would
    convert the raising case into the silent one.
    """
    with patch.object(subprocess, "run", side_effect=_decoding_run("cp1252")):
        proc = azure_client._az_run(["az", "devops", "invoke"], host="dev.azure.com", timeout=30)

    assert proc.stdout == RAW_JSON


def test_ascii_output_is_unaffected() -> None:
    """Over-fix guard: the ordinary case is unchanged, and passes on both sides."""
    plain = b'{"id": 7, "fields": {"System.Title": "plain title"}}'
    with patch.object(subprocess, "run", side_effect=_decoding_run("cp950", plain)):
        proc = azure_client._az_run(["az", "devops", "invoke"], host="dev.azure.com", timeout=30)

    assert json.loads(proc.stdout)["fields"]["System.Title"] == "plain title"


def test_the_spawn_environment_tells_az_to_write_utf8() -> None:
    """The child end, through the real chokepoint: the env reaches ``subprocess.run``.

    ``encoding="utf-8"`` on the parent is unfalsifiable on its own -- it is correct
    only if the child actually emits UTF-8. This asserts the pairing where it ships,
    on the env ``_az_run`` hands to the spawn.
    """
    seen: dict[str, str] = {}

    def _capture(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, RAW_JSON, "")

    with patch.object(azure_client, "_az_env", _REAL_AZ_ENV):
        with patch.object(subprocess, "run", side_effect=_capture):
            azure_client._az_run(["az", "devops", "invoke"], host="dev.azure.com", timeout=30)

    pinned = seen.get("PYTHONIOENCODING")
    assert pinned == "utf-8", "the child was left to encode its stdout with the host code page"


def test_a_hostile_inherited_encoding_does_not_reach_az() -> None:
    """An operator's own ``PYTHONIOENCODING`` must not decide what az emits.

    The assignment is unconditional rather than a default, so a shell that exports
    a code page cannot put the child back on it. Driven at the transport helper so
    the inherited value is a parameter instead of a property of the test runner.
    """
    env = azure_transport._az_env(
        "dev.azure.com",
        source_env={"PYTHONIOENCODING": "cp1252", "AZURE_DEVOPS_EXT_PAT": "pat"},
        passthrough_keys=azure_client._AZ_ENV_PASSTHROUGH,
        minimal_env=lambda **extra: dict(extra),
    )

    assert env["PYTHONIOENCODING"] == "utf-8"

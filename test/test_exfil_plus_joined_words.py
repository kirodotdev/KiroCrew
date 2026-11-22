"""The exfil base64 heuristic reads ``+``-joined words in a query as words.

A ticket search that batches ids, ``q=ids:(T1000000001+OR+T1000000002+OR+...)``,
is one 40+ character run of the base64 class once three ids are joined, so a cron
script's ``call_tool`` was refused with "reads as a credential" while one id alone
passed. In a query ``+`` is a space, and the same ids joined with ``%20`` already
passed. These tests pin both directions: plain word and id lists pass, and every
secret shape the heuristic exists for still fires, a ``+`` inside it or not.

Sample URLs are built from parts so no literal long-query URL appears here.
"""

from __future__ import annotations

import base64

import pytest

from kiro_crew.cron_script_context import _scanned_tool_args
from kiro_crew.security import scan_exfiltration_urls
from kiro_crew.security.exfil import _exfil_blob_hit
from kiro_crew.testing.synthetic_credentials import B64, SAMPLE_AWS_SECRET, synth

# Invented ticket ids of the reported shape: one letter, then ten digits.
_IDS = [f"T{n:010d}" for n in (1032055033, 1033586867, 1031939071, 1079542916, 1073045210)]
_IDS += [f"T{n:010d}" for n in (1017505051, 1049654541, 1081056775, 1063626388, 1083982757)]


def _url(query: str) -> str:
    return "https://tickets.example.com/issues?" + query


def _flagged(url: str) -> bool:
    return bool(scan_exfiltration_urls(f"see {url} for details"))


class TestWordListsPass:
    @pytest.mark.parametrize("count", [3, 5, 10])
    def test_ids_joined_with_plus_or_plus(self, count: int) -> None:
        assert not _flagged(_url("q=aliases:(" + "+OR+".join(_IDS[:count]) + ")"))

    def test_ids_joined_with_bare_plus(self) -> None:
        assert not _flagged(_url("q=" + "+".join(_IDS)))

    def test_ids_joined_with_percent_twenty_still_pass(self) -> None:
        assert not _flagged(_url("q=aliases:(" + "%20OR%20".join(_IDS) + ")"))

    def test_plain_words_joined_with_plus(self) -> None:
        words = "how to set the time zone for a scheduled job in the dashboard"
        assert not _flagged(_url("q=" + words.replace(" ", "+")))

    def test_cron_call_tool_sends_the_batched_lookup(self) -> None:
        url = _url("q=aliases:(" + "+OR+".join(_IDS) + ")")
        args = {"inputs": [url]}
        assert _scanned_tool_args("builder", "ReadPage", args) is args


class TestSecretsStillFire:
    def test_aws_secret_with_plus_in_place_of_slash(self) -> None:
        assert "+" in SAMPLE_AWS_SECRET.replace("/", "+")
        assert _flagged(_url("k=" + SAMPLE_AWS_SECRET.replace("/", "+")))

    @pytest.mark.parametrize("seed", range(40))
    def test_synthetic_base64_secrets_with_plus(self, seed: int) -> None:
        raw = synth(38, B64, seed)
        secret = raw[:13] + "+" + raw[13:26] + "+" + raw[26:]
        assert _exfil_blob_hit(secret)

    def test_real_base64_payload_with_plus(self) -> None:
        blob = base64.b64encode(bytes(range(200, 236))).decode()
        assert "+" in blob or "/" in blob
        assert _flagged(_url("d=" + blob))

    def test_aws_key_id_inside_a_word_list(self) -> None:
        key_id = "AKIA" + "ABCDEFGHIJKL1234"
        joined = "+OR+".join([_IDS[0], key_id, _IDS[1]])
        assert _exfil_blob_hit(joined)
        assert _flagged(_url("q=" + joined))

    def test_a_word_shaped_piece_that_is_itself_a_blob(self) -> None:
        piece = "ABCDEFGHIJKLMNOPQRSTUVWX" + "1234567890" * 2
        assert _exfil_blob_hit("+".join([_IDS[0], piece]))

    def test_a_piece_that_is_itself_a_blob(self) -> None:
        assert _flagged(_url("q=" + "+".join([_IDS[0], "Ab3" * 14])))

    def test_empty_piece_keeps_firing(self) -> None:
        assert _exfil_blob_hit("++".join(_IDS[:4]))

    def test_slash_in_the_run_keeps_firing(self) -> None:
        assert _exfil_blob_hit("+".join(_IDS[:3]) + "/" + _IDS[3])

    def test_mixed_case_piece_keeps_firing(self) -> None:
        assert _exfil_blob_hit("+".join(["kJxQwErTyUiOp", "aBcDeFgHiJkLm", "QwErTyUiOpAsD"]))

"""The exfil base64-blob heuristic does not fire on a bare git object name.

A commit permalink that carries the SHA in its QUERY (``?commit=<sha>``, a
change-history view keyed as ``?changes=<pkg>/<branch>:<sha>``) was rewritten
to ``[REDACTED: suspicious URL to <host>]`` on every surface: 40 hex digits are
40 characters of the base64 class, so the blob branch of ``_EXFIL_PATTERNS``
matched them. The same SHA in the PATH was already benign (the heuristics are
query-only), so the two spellings of one permalink disagreed.

These tests pin the carve-out's edges rather than just the happy path: it is
the exact SHA-1 length (40) in lowercase, judged on the MAXIMAL run, so
every neighbouring shape still redacts. 64 hex (SHA-256) stays redacted: it is
the shape of the product's own ``token_hex(32)`` secrets. Sample URLs are built from parts so no
literal long-query URL appears here.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls
from kiro_crew.security.exfil import _exfil_blob_hit

# Invented values: the SHA-1 of the empty tree and a 64-hex (SHA-256 / app
# secret) shaped run.
SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _url(query: str) -> str:
    return "https://git.example.com/pkg/history?" + query


def _redacted(url: str) -> bool:
    result, warnings = redact_exfiltration_urls(f"see {url} for details")
    return bool(warnings) and url not in result


class TestObjectNamesAreKept:
    def test_sha1_as_a_query_value(self) -> None:
        url = _url("commit=" + SHA1)
        assert not scan_exfiltration_urls(url)
        assert not _redacted(url)

    def test_sha256_shaped_value_still_redacts(self) -> None:
        # 64 lowercase hex is what ``generate_app_secret()`` emits.
        assert _redacted(_url("commit=" + SHA256))

    def test_sha1_after_a_routed_prefix(self) -> None:
        # ``<pkg>/<branch>:<sha>`` -- the colon ends the run before the SHA, so
        # the maximal run IS the SHA.
        assert not _redacted(_url("changes=Commit:pkg/mainline:" + SHA1))

    def test_two_object_names_in_one_query(self) -> None:
        assert not _redacted(_url("from=" + SHA1 + "&to=" + SHA1[::-1]))

    def test_redact_with_records_keeps_the_link(self) -> None:
        from kiro_crew.security.exfil import redact_exfiltration_urls_with_records

        url = _url("commit=" + SHA1)
        result, warnings, records = redact_exfiltration_urls_with_records(f"link {url}")
        assert url in result
        assert warnings == []
        assert records == []


class TestNeighbouringShapesStillRedact:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(SHA1 + "a", id="41-hex"),
            pytest.param(SHA256 + "0", id="65-hex"),
            pytest.param(SHA1[:20] + SHA1[:20].upper(), id="mixed-case-40"),
            pytest.param(SHA1.upper(), id="uppercase-40"),
            pytest.param(SHA1 + "=", id="sha-plus-padding"),
            pytest.param("x" + SHA1, id="glued-to-a-letter"),
            pytest.param(SHA1 + "/" + SHA1[:3], id="glued-to-a-slash-run"),
            # The raw query is judged as well as the decoded one, and the two
            # hex digits of a percent escape are in the class: ``%3A<sha>`` is
            # a 42-char run in the raw form, so the carve-out does not reach it.
            pytest.param("pkg%3A" + SHA1, id="percent-encoded-separator-glued"),
            pytest.param("0" * 48, id="48-hex-is-no-git-length"),
            pytest.param("abcdefghij" * 4, id="40-lower-non-hex"),
        ],
    )
    def test_value_is_a_blob_hit(self, value: str) -> None:
        assert _exfil_blob_hit(value)
        assert _redacted(_url("v=" + value))

    def test_object_name_beside_a_real_payload(self) -> None:
        # The SHA is kept out of the verdict; the blob next to it still decides.
        assert _redacted(_url("commit=" + SHA1 + "&data=" + "Ab3x" * 10))

    def test_object_name_beside_a_fixed_credential(self) -> None:
        assert _redacted(_url("commit=" + SHA1 + "&k=AKIAIOSFODNN7EXAMPLE"))

    def test_object_name_does_not_shorten_the_length_gate(self) -> None:
        # Many SHAs still add up: the aggregate length signal is untouched.
        many = "&".join(f"c{i}=" + SHA1 for i in range(5))
        assert len(many) >= 200
        assert _redacted(_url(many))


class TestHelperJudgesTheMaximalRun:
    def test_bare_object_names(self) -> None:
        assert not _exfil_blob_hit(SHA1)
        assert not _exfil_blob_hit("a=" + SHA1 + "&b=" + SHA1[::-1])
        assert _exfil_blob_hit(SHA256)
        assert _exfil_blob_hit("a=" + SHA1 + "&b=" + SHA256)

    def test_other_alternatives_are_unchanged(self) -> None:
        assert _exfil_blob_hit("AKIAIOSFODNN7EXAMPLE")
        assert _exfil_blob_hit("xoxb-1234567890-abcdefghij")
        assert _exfil_blob_hit("%41" * 21)
        assert _exfil_blob_hit("ssh-rsa AAAA")

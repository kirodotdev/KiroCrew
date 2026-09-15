"""``redact_path_segments``: path-aware redaction whose output keeps distinct
paths distinct, carries nothing recoverable about the secret, and labels the
same path the same way in every response of one gateway process.

The whole-string redactors collapse every matched token to one fixed tag, so two
project-relative paths whose only differing segment is credential-shaped redact
to the SAME string and a de-duplicating listing silently drops one. The helper
redacts each segment on its own, never emits less redaction than the
whole-string redactor it wraps, and suffixes every redacted segment with an
opaque label: an HMAC of the original segment under a random per-process key.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from kiro_crew import security
from kiro_crew.security import _exports, redact, redact_path_segments
from kiro_crew.security import redaction as redaction_mod
from kiro_crew.security.redaction import (
    _PATH_SEGMENT_DISCRIMINATOR_SEP,
    _REDACTED_CREDENTIAL_TAG,
    _path_segment_label,
)

# Two DISTINCT credential-shaped tokens. key_a is the documented example id the
# Semgrep allowlist knows; key_b stays a split literal because the AKIA-shaped
# detector cannot tell a fixture from a real leak.
_KEY_A = "AKIAIOSFODNN7EXAMPLE"
_KEY_B = "AKIA" + "JKLMNOPQRSTUVWXY"
# A bare 40-char secret-shaped key that contains ``/`` -- it spans two path
# segments and is matched only by the whole-string pass.
_SPANNING_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

_SEP = _PATH_SEGMENT_DISCRIMINATOR_SEP


def _sha256_prefixes(text: str) -> list[str]:
    """Every hex prefix (8..64 digits) of the UNKEYED SHA-256 over *text* -- the
    shapes a digest-derived discriminator could take. None may appear in output."""
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return [digest[:n] for n in range(8, 65)]


def _assert_no_secret_material(out: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in out
        # No byte-run of the secret either: the tag replaces the token whole.
        for i in range(0, len(secret) - 3):
            assert secret[i : i + 4] not in out, (secret[i : i + 4], out)
        for prefix in _sha256_prefixes(secret):
            assert prefix not in out
        for prefix in _sha256_prefixes(secret + "_model.txt"):
            assert prefix not in out


def _label(out: str) -> str:
    """The label suffix of a single-segment redacted output."""
    return out.rsplit(_SEP, 1)[1]


class TestDistinctInputsStayDistinct:
    def test_two_credential_shaped_filenames_do_not_collapse(self) -> None:
        a = redact_path_segments(f"models/{_KEY_A}_model.txt")
        b = redact_path_segments(f"models/{_KEY_B}_model.txt")
        # The whole-string redactor collapses them; the segment-wise helper does not.
        assert redact(f"models/{_KEY_A}_model.txt") == redact(f"models/{_KEY_B}_model.txt")
        assert a != b
        # Each keeps the directory, the tag, its non-secret tail and the segment
        # structure; the label closes the redacted SEGMENT and is the HMAC of
        # that segment's original bytes.
        tag = _REDACTED_CREDENTIAL_TAG
        assert a == f"models/{tag}_model.txt{_SEP}{_path_segment_label(_KEY_A + '_model.txt')}"
        assert b == f"models/{tag}_model.txt{_SEP}{_path_segment_label(_KEY_B + '_model.txt')}"

    def test_two_credential_shaped_directories_do_not_collapse(self) -> None:
        a = redact_path_segments(f"cdk.out/{_KEY_A}/index.js")
        b = redact_path_segments(f"cdk.out/{_KEY_B}/index.js")
        assert a != b
        assert (
            a == f"cdk.out/{_REDACTED_CREDENTIAL_TAG}{_SEP}{_path_segment_label(_KEY_A)}/index.js"
        )
        assert (
            b == f"cdk.out/{_REDACTED_CREDENTIAL_TAG}{_SEP}{_path_segment_label(_KEY_B)}/index.js"
        )
        assert a.count("/") == 2 and b.count("/") == 2

    def test_identical_originals_map_to_identical_outputs(self) -> None:
        same = f"models/{_KEY_A}_model.txt"
        other = f"models/{_KEY_B}_model.txt"
        outs = [redact_path_segments(p) for p in (same, other, same, same)]
        assert outs[0] == outs[2] == outs[3]
        assert outs[0] != outs[1]

    def test_the_label_is_twelve_hex_digits(self) -> None:
        out = redact_path_segments(f"models/{_KEY_A}_model.txt")
        label = _label(out)
        assert len(label) == 12
        assert set(label) <= set("0123456789abcdef")

    def test_a_token_whose_value_spans_a_separator_falls_back_to_the_whole_pass(self) -> None:
        """A ``key=value`` pattern that admits ``/`` in the value is matched by
        the whole-string pass across the separator but only up to it by the
        segment pass; the leftover tail is not a match on its own, so a
        fixed-point check alone would emit it. The segment result is accepted
        only when its unlabelled join equals the whole-string result."""
        import re

        pattern = re.compile(r"token=[A-Za-z0-9/]+")

        def redactor(text: str) -> str:
            return pattern.sub("[REDACTED: token]", text)

        path = "logs/token=abc/def/notes.txt"
        out = redact_path_segments(path, redactor)
        # Segment-wise, only "token=abc" matches and "def" would survive; the
        # whole pass wins instead, and no byte of the value is emitted.
        assert out == redactor(path)
        assert "abc" not in out and "def" not in out
        assert _PATH_SEGMENT_DISCRIMINATOR_SEP not in out

    def test_a_clean_path_is_returned_unchanged(self) -> None:
        for path in ["src/mod.py", "a.txt", "cdk.out/asset.0123abcd/index.js", ""]:
            assert redact_path_segments(path) == path
            assert _SEP not in redact_path_segments(path)


class TestTheLabelCarriesNoSecretMaterial:
    def test_the_output_carries_no_byte_and_no_unkeyed_digest_of_either_secret(self) -> None:
        for path in (
            f"models/{_KEY_A}_model.txt",
            f"models/{_KEY_B}_model.txt",
            f"cdk.out/{_KEY_A}/x",
        ):
            _assert_no_secret_material(redact_path_segments(path), _KEY_A, _KEY_B)

    def test_the_label_is_not_the_unkeyed_sha256_prefix_of_the_segment(self) -> None:
        segment = f"{_KEY_A}_model.txt"
        label = _path_segment_label(segment)
        unkeyed = hashlib.sha256(segment.encode("utf-8", "surrogatepass")).hexdigest()
        assert label != unkeyed[:12]
        assert label not in unkeyed
        # It IS the keyed digest under the module key -- the property the join
        # relies on, and the reason an offline guess needs the key.
        keyed = hmac.new(
            redaction_mod._PATH_LABEL_KEY, segment.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert label == keyed[:12]


class TestTheLabelIsStableWithinOneProcess:
    def test_two_calls_yield_the_same_label(self) -> None:
        path = f"models/{_KEY_A}_model.txt"
        assert redact_path_segments(path) == redact_path_segments(path)
        assert _path_segment_label(_KEY_A) == _path_segment_label(_KEY_A)

    def test_two_listings_sharing_a_path_label_it_identically(self) -> None:
        # The tree listing and the git-status listing are two responses; the
        # dashboard joins them by path, so a path common to both must label the
        # same way in each, whatever else each listing holds.
        shared = f"models/{_KEY_A}_model.txt"
        tree = [shared, f"models/{_KEY_B}_model.txt", "src/mod.py", "a.txt"]
        status = [shared]
        tree_out = {p: redact_path_segments(p) for p in tree}
        status_out = {p: redact_path_segments(p) for p in status}
        assert tree_out[shared] == status_out[shared]

    def test_a_label_is_independent_of_the_other_paths_in_the_listing(self) -> None:
        path = f"models/{_KEY_A}_model.txt"
        alone = redact_path_segments(path)
        with_neighbours = [
            redact_path_segments(p)
            for p in (f"models/{_KEY_B}_model.txt", path, f"cdk.out/{_KEY_B}/x")
        ][1]
        assert alone == with_neighbours


class TestTheLabelIsAFunctionOfTheKey:
    def test_another_key_yields_another_label(self, monkeypatch) -> None:
        segment = f"{_KEY_A}_model.txt"
        before = _path_segment_label(segment)
        before_path = redact_path_segments(f"models/{segment}")
        monkeypatch.setattr(redaction_mod, "_PATH_LABEL_KEY", b"\x01" * 32)
        after = _path_segment_label(segment)
        assert after != before
        # Under the swapped key the whole path labels differently too, so the
        # label is a function of the key, not of the secret alone.
        assert redact_path_segments(f"models/{segment}") != before_path
        # Still distinct from a different secret under the same swapped key.
        assert after != _path_segment_label(f"{_KEY_B}_model.txt")

    def test_the_key_is_a_fresh_32_byte_secret(self) -> None:
        assert isinstance(redaction_mod._PATH_LABEL_KEY, bytes)
        assert len(redaction_mod._PATH_LABEL_KEY) == 32


class TestLabelsCarryNoOrder:
    def test_reversing_the_input_yields_the_same_per_path_outputs(self) -> None:
        paths = [
            "b.txt",
            f"m/{_KEY_A}_x",
            "a.txt",
            f"m/{_KEY_B}_x",
            f"cdk.out/{_KEY_A}/index.js",
            "src/mod.py",
        ]
        forward = {p: redact_path_segments(p) for p in paths}
        backward = {p: redact_path_segments(p) for p in reversed(paths)}
        assert forward == backward
        assert len(set(forward.values())) == len(paths)

    def test_sorting_the_listing_does_not_change_a_label(self) -> None:
        paths = [f"models/{_KEY_A[:-1]}{ch}_model.txt" for ch in "FEDCBA"]
        unsorted_out = [redact_path_segments(p) for p in paths]
        sorted_out = [redact_path_segments(p) for p in sorted(paths)]
        assert sorted(unsorted_out) == sorted(sorted_out)
        assert len(set(unsorted_out)) == 6


class TestNeverLessRedactionThanTheWholeString:
    @pytest.mark.parametrize(
        "path",
        [
            f"models/{_KEY_A}_model.txt",
            f"cdk.out/{_KEY_A}/index.js",
            f"{_KEY_A}",
            f"a/{_SPANNING_SECRET}/b",
            f"{_SPANNING_SECRET}",
        ],
    )
    def test_the_original_sensitive_bytes_never_survive(self, path: str) -> None:
        out = redact_path_segments(path)
        assert _KEY_A not in out
        assert _SPANNING_SECRET not in out
        for piece in _SPANNING_SECRET.split("/"):
            assert piece not in out
        # The output is a fixed point of the whole-string redactor: nothing
        # sensitive remains for it to find.
        assert redact(out) == out

    def test_a_token_spanning_segments_falls_back_to_the_whole_string_result(self) -> None:
        path = f"a/{_SPANNING_SECRET}/b"
        assert redact_path_segments(path) == redact(path)
        assert _SEP not in redact_path_segments(path)

    def test_a_secret_bearing_segment_is_fully_redacted(self) -> None:
        out = redact_path_segments(f"cdk.out/{_KEY_A}/index.js")
        middle = out.split("/")[1]
        assert middle == f"{_REDACTED_CREDENTIAL_TAG}{_SEP}{_path_segment_label(_KEY_A)}"

    def test_the_custom_redactor_is_the_floor(self) -> None:
        # A caller-supplied redactor (the context-aware shim) decides what is
        # sensitive; the helper applies it per segment and re-checks the result.
        def strict(text: str) -> str:
            return text.replace("secret", "[X]")

        out = redact_path_segments("a/secret-one/secret-two/b", strict)
        assert out == (
            f"a/[X]-one{_SEP}{_path_segment_label('secret-one')}"
            f"/[X]-two{_SEP}{_path_segment_label('secret-two')}/b"
        )
        assert "secret" not in out

        def collapse(text: str) -> str:
            return text.replace("secretA", "[X]").replace("secretB", "[X]")

        c = redact_path_segments("x/secretA/y", collapse)
        d = redact_path_segments("x/secretB/y", collapse)
        assert c != d
        assert c == f"x/[X]{_SEP}{_path_segment_label('secretA')}/y"

    def test_a_redactor_that_cannot_reach_a_fixed_point_falls_back(self) -> None:
        # Every call changes the text, so the segment-wise candidate is never a
        # fixed point and the whole-string result is what comes back.
        def churn(text: str) -> str:
            return text + "!"

        assert redact_path_segments("a/b", churn) == "a/b!"


class TestExport:
    def test_exported_on_the_facade(self) -> None:
        assert security.redact_path_segments is redact_path_segments
        assert "redact_path_segments" in _exports.EXPORTED_NAMES

    def test_the_listing_helper_is_gone(self) -> None:
        assert not hasattr(security, "redact_paths_distinct")
        assert "redact_paths_distinct" not in _exports.EXPORTED_NAMES
        assert not hasattr(redaction_mod, "redact_paths_distinct")

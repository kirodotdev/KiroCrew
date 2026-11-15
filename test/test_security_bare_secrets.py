"""Tests for kiro_crew.security: the bare-secret classifier behind ``redact_credentials``.

Deep CamelCase paths are not bare secrets, the slash cap is the knee of the classifier, the
run-level fast path is an optimization only, the gates run in cost order without changing a
verdict, and the character-class helper, the Shannon-entropy terms and the lowercase-run check
agree with the definitions they replace.
"""

from __future__ import annotations

import math
import random
import string
import struct
import sys
from collections import Counter
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.security import (
    _SECRET_KEY_LEN,
    _SECRET_MAX_SLASHES,
    REDACTED_CREDENTIAL_TAG,
    redact_credentials,
)
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET

#: A synthetic secret access key in the AWS documentation example's shape (the
#: example itself is kept visible as an identifier). It CONTAINS two ``/``
#: characters, which is what makes it the fixture for the path-versus-secret
#: boundary: a fix that treated every slash-bearing run as a path would leak
#: exactly this value.
_SLASH_KEY = SAMPLE_AWS_SECRET
#: A second slash-bearing shape, so no claim about slash-bearing keys rests
#: on one fixture's particular letters.
_ALT_SLASH_KEY = "Kx3Q51tPusV/D0URlGfM" "mNbVc7Z8yJhLpQrStUwZ"
#: The same shape without separators, which the ceiling cannot reach.
_NO_SLASH_KEY = "Kx3Q51tPusVkD0URlGfM" "mNbVc7Z8yJhLpQrStUwZ"


class TestPathWindowsAreNotBareSecrets:
    """A deep CamelCase absolute path must survive ``redact_credentials``.

    ``/`` is in ``_B64_CHUNK_RE``'s character class, so an absolute path built
    from CamelCase segments is ONE base64-alphabet run -- it breaks only at
    ``-``, ``_``, ``.`` or whitespace. ``_contains_bare_secret`` slides a 40-char
    window across it, and a window cut from two or three components defeats the
    structural gates on their own terms: CamelCase caps the lowercase run,
    consonant-heavy acronyms crush the vowel ratio, a version digit supplies the
    third character class, and the separators lift the entropy. One passing window
    replaced the WHOLE run, so the path came back as the tag and every consumer
    that dereferences it -- a file-path chip, a screenshot note -- got an unusable
    string instead of a path.

    ``_SECRET_MAX_SLASHES`` declines such a window, and the two properties that
    keep it from being a hole are pinned below: every window is still classified,
    and a run of exactly one key length is never subject to the ceiling.
    """

    # ── benign paths must survive ──

    @pytest.mark.parametrize(
        "path",
        [
            # The reported shape: consonant-heavy acronyms plus a version digit.
            "/Volumes/workplace/TRAM/QuickProp2/src/ATVTramQuickPropCDK/lib/config/consumerVpcs.ts",
            "/Volumes/workplace/CMS/A3P3/src/LPTCoreServiceCDK/lib/stacks/MainStack2.ts",
            "/opt/workplace/ATVDeviceRegistry2/src/ATVDeviceRegistryCDKv2/lib/Vpcs3.ts",
            # A mixed-case stand-in for the macOS per-user temp directory, which
            # the separator ceiling saves on every platform. The real shape is
            # pinned by the macOS-host tests below.
            "/var/folders/6r/qKz9XyT3wLmNp7vB2cQ4hJ8000gn/T/screenshot.png",
            "/Users/Someone/Projects/DeepCamelCaseFolder/AnotherCamelFolder/SomeComponentName.ts",
            "/srv/build/src/main/java/com/Example/Service2/FooBarBazClas1/Handler9.java",
            "/mnt/data/RepoName2/PackageNameCDK/lib/config/RegionConfig3/UsEast1Props.ts",
            "/srv/app/BuildArtifacts9/SubProjectCore/GeneratedSources2/ModelTypes4.ts",
            "/private/var/db/CryptexStore2/StagedCryptexes/OsVariant3/PayloadRoot9",
            "/usr/lib/NodeModules2/SomePackageName/DistBundles3/EsmChunkNames9.mjs",
            # A '+' in a component is ordinary: the ceiling counts separators, so it
            # needs no base64-alphabet carve-out.
            "/usr/include/c++/v1/LongCamelNameHere2/AnotherCamelName3/ThirdName4",
            # The root name is ordinary too: nothing here consults a list of roots,
            # so a path under an unusual mount is covered like any other.
            "/data/scratch/BuildRoot2/SubPackageCore/GeneratedModels3/TypeDefs4.ts",
            # Relative, and long enough to be one run.
            "src/main/java/com/Example/Service2/FooBarBazClas1/HandlerImpl9.java",
        ],
    )
    def test_camelcase_path_survives(self, path: str) -> None:
        result, warnings = redact_credentials(path)
        assert result == path, f"path over-redacted: {path!r}"
        assert not warnings

    def test_the_reported_path_is_still_openable(self) -> None:
        """The end-to-end symptom: the redacted string is what the viewer sends.

        A tag in place of the path is not a display glitch -- it is the value the
        file-read request carries, so the chip can never resolve. Assert the two
        properties a consumer needs, not only equality.
        """
        path = (
            "/Volumes/workplace/TRAM/QuickProp2/src/ATVTramQuickPropCDK/lib/config/consumerVpcs.ts"
        )
        result, _ = redact_credentials(path)
        assert REDACTED_CREDENTIAL_TAG not in result
        assert result.startswith("/") and result.endswith(".ts")

    # ── the macOS per-user directory, in its real shape ──

    # ``confstr`` ids as macOS generates them: a 2-character bucket and a
    # 30-character name of lowercase letters and digits with no vowel, ending in
    # the encoded uid. The shape is what matters: a mixed-case or shorter
    # stand-in clears the heuristic on its own and would pin nothing.
    DARWIN_USER_DIR_IDS = (
        ("zz", "pq7mtk933xwbn58cmrlt6hv40000gn"),
        ("6r", "8b2dmqf94w3bb1m3dqx6h6h40000gn"),
    )

    @staticmethod
    def _darwin_root(bucket: str, name: str) -> str:
        return f"/var/folders/{bucket}/{name}/T"

    @staticmethod
    def _produced_spool_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        """The spool-relative path ``ComputerUseService`` writes a screenshot to.

        Produced by the real spooler rather than spelled here, so the directory
        name and the ``shot-<ms>-<random>.jpeg`` file name are the shipped ones.
        """
        from kiro_crew.computer_use import service as service_mod
        from kiro_crew.computer_use.types import AppRef, Snapshot

        monkeypatch.setattr(service_mod.tempfile, "gettempdir", lambda: str(tmp_path))
        snap = Snapshot(
            app=AppRef(name="A", pid=1, bundle_id="com.acme.a", window_id=7),
            elements=(),
            captured_at=0.0,
            image_jpeg=b"\xff\xd8\xff\xd9",
            image_width=1,
            image_height=1,
        )
        produced = service_mod.ComputerUseService()._persist_image(snap).image_path
        assert produced, "the spooler must have written the frame"
        return Path(produced).relative_to(tmp_path).as_posix()

    @staticmethod
    def _edge_key(name: str, shared: int) -> str:
        """A 40-char ``_looks_like_secret_key`` key that reuses *shared* id bytes.

        The key's first *shared* bytes are the host id ``name``'s last *shared*
        bytes, then ``/T/``, so when it is glued right after ``{bucket}/{name}``
        minus those bytes the id regex still matches and a key-shaped window
        begins inside the id, sharing only those bytes in place. A seeded search
        fills the rest until the heuristic accepts, which is deterministic.
        """
        from kiro_crew.security import redaction as redaction_mod

        head = name[-shared:] + "/T/"
        rng = random.Random(f"edge-key-{name}-{shared}")
        alphabet = string.ascii_letters + string.digits
        for _ in range(200_000):
            key = head + "".join(rng.choice(alphabet) for _ in range(_SECRET_KEY_LEN - len(head)))
            if redaction_mod._looks_like_secret_key(key):
                return key
        raise AssertionError("no key-shaped tail found")

    # ── the split is entered only for a run the whole-run scan would redact ──

    @pytest.mark.skipif(sys.platform != "darwin", reason="reads the real macOS confstr")
    def test_the_real_temp_dir_survives_on_macos(self) -> None:
        import os

        # ``_CS_DARWIN_USER_TEMP_DIR`` in Darwin's ``<unistd.h>``, which Python does not
        # name; ``tempfile.gettempdir()`` would follow the run's ``$TMPDIR`` instead.
        root = os.confstr(65537)
        assert root and root.startswith("/var/folders/"), root
        path = f"{root.rstrip('/')}/kirocrew-computer-shots/shot-1700000000000-ab12cd34.jpeg"
        assert redact_credentials(f"![screenshot]({path})")[0] == f"![screenshot]({path})"

    # ── a standalone key is never subject to the ceiling ──

    # Uniformly random 40-char base64 tokens that the seven gates accept AND that
    # carry more separators than the ceiling -- the population the ceiling would
    # cost if it reached a standalone token. They cover 4 to 8 separators,
    # including a leading one, a trailing one and an adjacent pair.
    SEPARATOR_HEAVY_KEYS = (
        "KSd5/MUOI7pQ/+X/lzj0" "Ma/DwFo5hMVm6ScMCQUs",
        "dMtmrCnv/fCJ/FcS/N/V" "WJV/t4OMtde4urD+evIx",
        "/ybCN+5DAYirSIyzpX/f" "1cjuq5b/mLA4NxO9/3sH",
        "EuaHEF+2RGfJHHMc/r6/" "/y7R0zTqsz/9/PZs6zcj",
        "wu9P3axJ2M/J0fFa/MG/" "hJ/LP/C7KzMSpSVr5w//",
        "Zy6//Pgqw1X5bzp55Iv/" "RvJ/y/KSojZIKIlR/umL",
    )

    # ── secrets must still be redacted, in every path context ──

    # ── the accepted residual, pinned so it is a decision and not a gap ──

    @pytest.mark.parametrize("key", SEPARATOR_HEAVY_KEYS[:2])
    @pytest.mark.parametrize("enclosing", ["/srv/ci/keys/{}", "https://example.com/v1/{}"])
    def test_the_accepted_residual_is_a_separator_heavy_key_glued_into_a_run(
        self, key: str, enclosing: str
    ) -> None:
        """DOCUMENTS AN ACCEPTED LOSS. This asserts what is NOT redacted.

        A key carrying more separators than the ceiling AND glued into a longer run
        is not detected. That is the trade this ceiling makes, and it is asserted
        here so it is a reviewed decision with a name rather than an untested
        corner of the credential-redaction guarantee.

        It cannot be closed at this layer. The benign path the ceiling exists to
        save and the key below are the SAME BYTES to the classifier: both are
        40-char windows over ``[A-Za-z0-9+/]`` carrying several separators that
        clear all seven gates, inside a run longer than one key. Any rule reading
        only the run's bytes that declines the first must decline the second.
        Distinguishing them needs information the run does not carry -- that the
        value is a path FIELD -- and ``redact_credentials`` runs over free-form
        transcript text, which has no such field.

        What bounds the loss is ``is_fragment``: the run must be longer than one
        whole key, so every shape an accidental echo takes is excluded. The
        surviving class needs a key that both carries 4+ separators (0.36% of
        uniformly random keys) and is concatenated to further base64 characters
        with no delimiter.

        If a future change adds a signal that separates the two, this test is the
        one to delete -- deliberately, and with the ceiling's rationale reread.
        """
        text = enclosing.format(key)
        assert key.count("/") > _SECRET_MAX_SLASHES
        assert security._looks_like_secret_key(key), "fixture must be key-shaped"
        result, warnings = redact_credentials(text)
        assert result == text
        assert not warnings

    # ── the two properties that keep the ceiling from being a hole ──

    def test_no_window_of_a_path_run_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ceiling may decline a window; it may not decline to LOOK at one.

        Narrowing which offsets are examined is the other available shape of this
        fix, and it leaves a key whose true offset is not examined undetected.
        This counts classifications: every offset of the run must still reach the
        classifier even though the run is a path and the verdict is False.
        """
        run = "/Volumes/workplace/TRAM/QuickProp2/src/ATVTramQuickPropCDK/lib/config/consumerVpcs"
        seen: list[str] = []
        real = security._looks_like_secret_key

        def counting(token: str) -> bool:
            seen.append(token)
            return real(token)

        monkeypatch.setattr(security, "_looks_like_secret_key", counting)
        assert security._contains_bare_secret(run) is False
        assert len(seen) == len(run) - _SECRET_KEY_LEN + 1
        # And the classifier itself said yes to at least one of them, which is what
        # makes the ceiling -- rather than some other gate -- the thing that saved
        # this path.
        assert any(real(token) for token in seen)

    def test_the_classifier_itself_is_unchanged_by_the_ceiling(self) -> None:
        """``_looks_like_secret_key`` stays a seven-gate predicate on its token.

        The ceiling lives in the slide, not in the classifier, because it is a
        statement about a window's relationship to a longer run rather than about
        the token. A token with many separators is still a secret to the
        classifier, and that is what lets a standalone key keep redacting.
        """
        token = self.SEPARATOR_HEAVY_KEYS[0]
        assert token.count("/") > _SECRET_MAX_SLASHES
        assert security._looks_like_secret_key(token) is True


class TestSecretSlashCapIsTheKnee:
    """Pin the ceiling's value against the real classifier, not a re-implementation.

    The two deep paths below are the ones the seven older gates cannot save. The
    chosen ceiling saves both; one notch looser saves neither. Swapping the
    constant rather than re-implementing the slide is what makes this a test of
    the shipped code.
    """

    PATHS = (
        "/Volumes/workplace/TRAM/QuickProp2/src/ATVTramQuickPropCDK/lib/config/consumerVpcs",
        "/var/folders/6r/qKz9XyT3wLmNp7vB2cQ4hJ8000gn/T/screenshot",
    )

    @pytest.mark.parametrize("path", PATHS)
    def test_the_chosen_ceiling_saves_the_path(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(security, "_SECRET_MAX_SLASHES", _SECRET_MAX_SLASHES)
        assert security._contains_bare_secret(path) is False

    @pytest.mark.parametrize("path", PATHS)
    def test_one_notch_looser_does_not(self, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(security, "_SECRET_MAX_SLASHES", _SECRET_MAX_SLASHES + 1)
        assert security._contains_bare_secret(path) is True


class TestBareSecretRunLevelFastPath:
    """The run-level fast path must be an optimization ONLY, never a hole.

    ``_contains_bare_secret`` slides a 40-char window byte by byte, so a long
    base64-alphabet run costs one full classification per offset. Two per-window
    gates reject on a property closed under substring -- a missing character
    class (gate 2) and all-hex (gate 3) -- so the whole run can be asked once and
    every window retired. These tests pin both halves of that claim: the fast
    path really fires (a behaviour-only test cannot see it), and it cannot
    swallow a genuine secret hidden inside a long run.
    """

    @staticmethod
    def _count_window_classifications(run: str, monkeypatch: pytest.MonkeyPatch) -> int:
        """Return how many 40-char windows of *run* got fully classified."""
        calls = []
        original = security._looks_like_secret_key

        def counting(token: str) -> bool:
            calls.append(token)
            return original(token)

        monkeypatch.setattr(security, "_looks_like_secret_key", counting)
        security._contains_bare_secret(run)
        return len(calls)

    def test_run_missing_a_char_class_skips_every_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 520 lowercase chars: no window can hold an uppercase char or a digit,
        # so gate 2 rejects all 481 of them. Without the fast path this is 481
        # full classifications; with it, zero.
        run = "abcdefghijklmnopqrstuvwxyz" * 20
        assert len(run) == 520
        assert security._contains_bare_secret(run) is False
        assert self._count_window_classifications(run, monkeypatch) == 0

    def test_all_hex_run_skips_every_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A long mixed-case hex digest passes gate 2 in every window but dies at
        # gate 3 in every window. All-hex is closed under substring, so one
        # whole-run test retires the slide -- 137 classifications become zero.
        run = "0123456789abcdefABCDEF" * 8
        assert len(run) == 176
        assert security._HEX_ONLY_RE.match(run)
        assert security._contains_bare_secret(run) is False
        assert self._count_window_classifications(run, monkeypatch) == 0

    def test_exactly_one_window_run_is_still_classified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BOUNDARY: the fast path is gated on `len(run) > _SECRET_KEY_LEN`, so a
        # 40-char run must still reach the classifier.
        #
        # The fixture must FAIL one of the two fast-path gates, or this test
        # cannot detect the boundary being wrong. With 40 lowercase chars: under
        # `>` the fast path is skipped and the sole window is classified (1);
        # under a mutated `>=` the fast path fires, the class check rejects, and
        # nothing is classified (0). A fixture that clears both gates -- an AWS
        # example key, say -- passes either way and pins nothing.
        run = "abcdefghijklmnopqrstuvwxyz" + "abcdefghijklmn"
        assert len(run) == _SECRET_KEY_LEN
        assert not security._has_all_three_char_classes(run)
        assert self._count_window_classifications(run, monkeypatch) == 1

    def test_run_with_all_three_classes_is_fully_slid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NEGATIVE CONTROL: the fast path may skip a run only when it can PROVE
        # no window qualifies. This run holds all three classes and is not
        # all-hex, so every one of its 21 windows must still be classified --
        # 60 - 40 + 1 == 21. (Beware fixtures like "aB3" * 30: a, B and 3 are
        # all hex digits, so that run is all-hex and is legitimately skipped.)
        run = "Zz9" * 20
        assert len(run) == 60
        assert not security._HEX_ONLY_RE.match(run)
        assert security._contains_bare_secret(run) is False
        assert self._count_window_classifications(run, monkeypatch) == 21


class TestCharClassHelperMatchesTheThreeScanDefinition:
    """``_has_all_three_char_classes`` replaced three ``any()`` scans.

    The single-pass early-exit loop must agree with the definition it replaced on
    every input, including the elif-chain cases where one character could be
    considered for more than one class.
    """

    @staticmethod
    def _reference(text: str) -> bool:
        return (
            any(ch.islower() for ch in text)
            and any(ch.isupper() for ch in text)
            and any(ch.isdigit() for ch in text)
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "a",
            "A",
            "1",
            "aA1",
            "1Aa",
            "A1a",
            "aaaaaaaa",
            "AAAAAAAA",
            "12345678",
            "aaaa1111",
            "AAAA1111",
            "aaaaAAAA",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "0123456789abcdef0123456789abcdef01234567",
            "+/+/+/+/",
            "MASSE",
            "straße",
        ],
    )
    def test_agrees_with_reference_on_representative_shapes(self, text: str) -> None:
        assert security._has_all_three_char_classes(text) is self._reference(text)

    def test_agrees_with_reference_across_a_random_corpus(self) -> None:
        rng = random.Random(20260810)
        alphabet = string.ascii_letters + string.digits + "+/=-_ "
        for _ in range(4000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 44)))
            assert security._has_all_three_char_classes(text) is self._reference(
                text
            ), f"disagreement on {text!r}"


class TestSecretGateOrderIsCostOrdered:
    """The gate ORDER is the point of the cost ordering, so pin it directly.

    ``TestSecretGateOrderIsVerdictNeutral`` cannot pin it: a conjunction of pure
    predicates is order-independent by construction, so no corpus can witness a
    reordering. Reverting the gates to entropy-first therefore passes every
    verdict test while silently undoing the optimisation. These tests count which
    gates get EVALUATED, which is the only observable that distinguishes one
    order from another.
    """

    @staticmethod
    def _counting_classify(token: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        """Classify *token*, counting calls to each expensive gate."""
        counts = {"entropy": 0, "decode": 0}
        real_entropy = security._shannon_entropy
        real_decode = security._decodes_to_printable_text

        def entropy(t: str) -> float:
            counts["entropy"] += 1
            return real_entropy(t)

        def decode(t: str) -> bool:
            counts["decode"] += 1
            return real_decode(t)

        monkeypatch.setattr(security, "_shannon_entropy", entropy)
        monkeypatch.setattr(security, "_decodes_to_printable_text", decode)
        security._looks_like_secret_key(token)
        return counts

    def test_a_structural_rejection_never_pays_for_entropy_or_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "aB3/" * 10 is 40 chars, holds all three classes, is not all-hex, and
        # has a vowel ratio of 0.5 -- so a structural gate rejects it. With the
        # structural gates first, neither expensive gate is ever called. Revert
        # to entropy-first and entropy is called, failing this test. That revert
        # is exactly the mutation no verdict-based test can catch.
        token = "aB3/" * 10
        assert len(token) == _SECRET_KEY_LEN
        assert security._has_all_three_char_classes(token)
        assert not security._HEX_ONLY_RE.match(token)
        counts = self._counting_classify(token, monkeypatch)
        assert counts == {"entropy": 0, "decode": 0}, (
            "a token rejected by a structural gate must not pay for entropy or "
            f"decode; got {counts}"
        )

    def test_decode_is_last_so_an_entropy_rejection_never_pays_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "Zz9" * 20 clears both structural gates but fails the entropy floor
        # (1.58 < 4.3). With decode last it is never called; move decode ahead of
        # entropy and this fails.
        token = ("Zz9" * 20)[:_SECRET_KEY_LEN]
        assert not security._lowercase_run_exceeds(token, security._SECRET_MAX_LOWER_RUN)
        assert security._vowel_ratio(token) <= security._SECRET_MAX_VOWEL_RATIO
        assert security._shannon_entropy(token) < security._SECRET_ENTROPY_MIN
        counts = self._counting_classify(token, monkeypatch)
        assert counts["entropy"] == 1, f"entropy should be reached: {counts}"
        assert counts["decode"] == 0, f"decode must run after entropy: {counts}"

    def test_a_real_key_still_pays_for_every_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The pass-through case: a genuine key clears all gates, so every gate
        # runs exactly once. This is what proves the cheap gates are not
        # short-circuiting a real secret away from the expensive checks.
        counts = self._counting_classify("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", monkeypatch)
        assert counts == {"entropy": 1, "decode": 1}


class TestSecretGateOrderIsVerdictNeutral:
    """Gates 4-7 are ordered by measured cost, so the order must not change verdicts.

    Every one of those gates is a pure predicate whose failure returns False, so
    reordering them can only change WHICH gate reports a rejection -- never
    whether the token is rejected. That is the property this class pins, because
    a reorder that silently changed one verdict in the redaction path would mean
    either a leaked credential or a corrupted benign output.
    """

    # Shapes chosen to exercise each gate as the deciding one: real keys, base64
    # blobs, JWT segments, file paths, camelCase identifiers, hex digests, prose.
    SOURCES = (
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ",
        "src/kiro_crew/security/redaction/Handler2/Manager3/Factory4/Builder5x",
        "getUserAccountManagerFactory2BuilderHelperImpl3ServiceProvider4x",
        "0123456789abcdefABCD" "EF0123456789abcdefAB",
        "TheGatewayRestoredTheSessionAndReplayed12ToolCallsSeeSecurityPy",
        "aB3/" * 24,
        "Zz9" * 20,
        # base64 of printable ASCII: the encoded-text-blob shape gate 7 exists to
        # exclude. This token clears gates 1-6 (vowel 0.079, no long lowercase
        # run, entropy 4.48) and is rejected ONLY by the decode gate, which is
        # what lets this corpus detect that gate being dropped or bypassed.
        "dFlnal9tVWgsQmVsMzFp" "RWwyaHBDaFlnQ2ZyTDFz",
    )

    @staticmethod
    def _reference(token: str) -> bool:
        """The classifier with gates 4-7 in every order, evaluated exhaustively.

        Rather than hard-code one alternative ordering, evaluate all four gates
        independently and AND them. Any ordering of short-circuiting checks must
        agree with the unordered conjunction.
        """
        if len(token) != _SECRET_KEY_LEN:
            return False
        if not security._has_all_three_char_classes(token):
            return False
        if security._HEX_ONLY_RE.match(token):
            return False
        return (
            security._vowel_ratio(token) <= security._SECRET_MAX_VOWEL_RATIO
            and not security._lowercase_run_exceeds(token, security._SECRET_MAX_LOWER_RUN)
            and security._shannon_entropy(token) >= security._SECRET_ENTROPY_MIN
            and not security._decodes_to_printable_text(token)
        )

    def _windows(self) -> list[str]:
        out = []
        for src in self.SOURCES:
            for i in range(max(1, len(src) - _SECRET_KEY_LEN + 1)):
                out.append(src[i : i + _SECRET_KEY_LEN])
        rng = random.Random(20260811)
        b64 = string.ascii_letters + string.digits + "+/"
        out += ["".join(rng.choice(b64) for _ in range(40)) for _ in range(500)]
        return out

    def test_ordered_classifier_matches_the_unordered_conjunction(self) -> None:
        windows = self._windows()
        assert len(windows) > 500
        for w in windows:
            assert security._looks_like_secret_key(w) is self._reference(
                w
            ), f"gate order changed the verdict for {w!r}"

    def test_the_corpus_actually_exercises_every_gate(self) -> None:
        # A verdict-equivalence test over a corpus that never reaches gates 4-7
        # would pass no matter how they were ordered. Prove the corpus bites.
        reached = {"vowel": 0, "lower": 0, "entropy": 0, "decode": 0, "passed": 0}
        for w in self._windows():
            if len(w) != _SECRET_KEY_LEN or not security._has_all_three_char_classes(w):
                continue
            if security._HEX_ONLY_RE.match(w):
                continue
            if security._lowercase_run_exceeds(w, security._SECRET_MAX_LOWER_RUN):
                reached["lower"] += 1
            elif security._vowel_ratio(w) > security._SECRET_MAX_VOWEL_RATIO:
                reached["vowel"] += 1
            elif security._shannon_entropy(w) < security._SECRET_ENTROPY_MIN:
                reached["entropy"] += 1
            elif security._decodes_to_printable_text(w):
                reached["decode"] += 1
            else:
                reached["passed"] += 1
        for gate in ("vowel", "lower", "entropy", "decode", "passed"):
            assert reached[gate] > 0, f"corpus never exercised gate {gate}: {reached}"


class TestShannonEntropyIsBitIdentical:
    """``_shannon_entropy`` precomputes its terms, and must not move a single bit.

    The value feeds a ``>= _SECRET_ENTROPY_MIN`` comparison in
    :func:`~kiro_crew.security._looks_like_secret_key`, so it decides whether a
    token is redacted. That makes ``math.isclose`` the WRONG assertion for this
    function: a drift small enough to pass a tolerance check is still large
    enough to flip the comparison for a token sitting on the boundary, and a flip
    in the permissive direction leaks a credential verbatim. So these tests
    compare IEEE-754 bit patterns via :func:`struct.pack`, which fails on a
    one-ULP difference and cannot be satisfied by "close enough".

    :meth:`_oracle` holds the pre-optimisation implementation verbatim. Keeping it
    here rather than deleting it is the point: the optimisation's whole claim is
    equality with THAT expression, so the claim needs the expression to still
    exist somewhere executable.
    """

    # Character counts of the two 40-char tokens whose entropy sits closest to
    # 4.3 from either side. Entropy depends only on the MULTISET OF COUNTS, so a
    # partition of 40 pins the value exactly and any token realising it has that
    # entropy. Searching every partition of 40 (restricted to at most one
    # base64-alphabet character each) found these two as the nearest achievable
    # neighbours of the threshold -- 4.3012... above and 4.2964... below.
    _NEAREST_ABOVE_COUNTS = (5, 5, 5, 2, 2, 2) + (1,) * 19
    _NEAREST_BELOW_COUNTS = (3, 3, 3, 3) + (2,) * 11 + (1,) * 6

    _ALPHABET = string.ascii_letters + string.digits + "+/"

    @staticmethod
    def _oracle(token: str) -> float:
        """The implementation from before the term table, character for character."""
        if not token:
            return 0.0
        counts = Counter(token)
        length = len(token)
        return -sum((c / length) * math.log2(c / length) for c in counts.values())

    @staticmethod
    def _bits(value: float) -> bytes:
        """Return *value*'s IEEE-754 bytes, so ``-0.0`` and ``0.0`` differ."""
        return struct.pack("<d", value)

    @classmethod
    def _realize(cls, counts: tuple[int, ...], shuffle_seed: int | None = None) -> str:
        """Build a token whose character counts are exactly *counts*."""
        chars: list[str] = []
        for index, count in enumerate(counts):
            chars.extend(cls._ALPHABET[index] * count)
        if shuffle_seed is not None:
            random.Random(shuffle_seed).shuffle(chars)
        return "".join(chars)

    @classmethod
    def _corpus(cls) -> list[str]:
        """Tokens spanning every shape this function is asked about, and then some."""
        tokens: list[str] = []

        # 1. The gate-order corpus: real keys, JWT segments, paths, identifiers,
        #    hex digests, prose, base64 blobs -- every 40-char window of each.
        for source in TestSecretGateOrderIsVerdictNeutral.SOURCES:
            for i in range(max(1, len(source) - _SECRET_KEY_LEN + 1)):
                tokens.append(source[i : i + _SECRET_KEY_LEN])

        # 2. Random base64-alphabet windows, the shape a real secret has.
        rng = random.Random(20260901)
        tokens += [
            "".join(rng.choice(cls._ALPHABET) for _ in range(_SECRET_KEY_LEN)) for _ in range(500)
        ]

        # 3. ADVERSARIAL: the nearest-to-threshold tokens from both sides, each in
        #    its natural order plus seeded shuffles. The shuffles vary the
        #    first-occurrence order that drives the summation sequence, so a
        #    rewrite that canonicalised or sorted the counts would have to survive
        #    many different orders of the same addends.
        for counts in (cls._NEAREST_ABOVE_COUNTS, cls._NEAREST_BELOW_COUNTS):
            tokens.append(cls._realize(counts))
            tokens += [cls._realize(counts, seed) for seed in range(16)]

        # 4. Degenerate and boundary shapes: empty, single character, all-identical
        #    (whose entropy is -0.0, a distinct bit pattern from 0.0), two
        #    characters, the whole alphabet once each, non-ASCII, and an astral
        #    character whose UTF-16 surrogate pair must not be counted as two.
        tokens += [
            "",
            "a",
            "a" * _SECRET_KEY_LEN,
            "ab" * 20,
            cls._ALPHABET,
            "h\u00e9llo w\u00f6rld",
            "\U0001f511" * 8,
        ]

        # 5. Lengths on both sides of the one length the table covers, so both the
        #    table path and the inline fallback are exercised.
        for length in (1, 2, 3, 39, 40, 41, 255, 256, 257, 1024):
            tokens.append("".join(cls._ALPHABET[i % len(cls._ALPHABET)] for i in range(length)))
            tokens.append("z" * length)

        return tokens

    def test_every_token_is_bit_identical_to_the_pre_table_implementation(self) -> None:
        corpus = self._corpus()
        assert len(corpus) > 500, "corpus collapsed; the rest of this class proves nothing"
        for token in corpus:
            got = security._shannon_entropy(token)
            want = self._oracle(token)
            assert self._bits(got) == self._bits(want), (
                f"entropy drifted for {token!r}: got {got!r} "
                f"({self._bits(got).hex()}) want {want!r} ({self._bits(want).hex()})"
            )

    def test_no_token_in_the_corpus_changes_side_of_the_redaction_threshold(self) -> None:
        # Bit-identity implies this, but assert it directly: this is the property
        # a leak would violate, and it survives a future refactor that relaxes the
        # bit-level assertion above.
        for token in self._corpus():
            new_side = security._shannon_entropy(token) >= security._SECRET_ENTROPY_MIN
            old_side = self._oracle(token) >= security._SECRET_ENTROPY_MIN
            assert new_side is old_side, f"redaction verdict flipped for {token!r}"

    def test_the_corpus_straddles_the_threshold_from_both_sides(self) -> None:
        # A bit-identity test over a corpus that never approaches 4.3 would pass
        # no matter how the boundary behaved. Prove the corpus bites.
        values = [security._shannon_entropy(token) for token in self._corpus()]
        threshold = security._SECRET_ENTROPY_MIN
        above = [v for v in values if v >= threshold]
        below = [v for v in values if v < threshold]
        assert above, "corpus has no token at or above the threshold"
        assert below, "corpus has no token below the threshold"
        # And the nearest neighbours really are within a few thousandths of it.
        # Those two bounds are the MEASURED gaps: no 40-char token can sit closer
        # to 4.3 than 1.21e-3 above or 3.57e-3 below, because entropy at a fixed
        # length takes only the discrete values the partitions of that length
        # allow. Tightening either bound past its gap would assert an input that
        # does not exist.
        assert min(above) - threshold < 2e-3, f"closest token above is {min(above)!r}"
        assert threshold - max(below) < 4e-3, f"closest token below is {max(below)!r}"

    def test_the_nearest_neighbour_tokens_land_on_opposite_sides(self) -> None:
        threshold = security._SECRET_ENTROPY_MIN
        above = security._shannon_entropy(self._realize(self._NEAREST_ABOVE_COUNTS))
        below = security._shannon_entropy(self._realize(self._NEAREST_BELOW_COUNTS))
        assert above >= threshold, f"expected {above!r} at or above {threshold}"
        assert below < threshold, f"expected {below!r} below {threshold}"

    def test_the_corpus_exercises_both_the_table_and_the_fallback(self) -> None:
        # The two code paths must both be reached, or the fallback is untested and
        # the table branch is a silent behaviour change for every other length.
        lengths = {len(token) for token in self._corpus()}
        assert _SECRET_KEY_LEN in lengths, lengths
        assert any(n != _SECRET_KEY_LEN for n in lengths), lengths

    def test_an_all_identical_token_keeps_its_negative_zero(self) -> None:
        # Every term is 1.0 * log2(1.0) == 0.0, and negating the sum yields -0.0.
        # math.isclose and == both treat -0.0 as 0.0, so only the bit pattern can
        # tell that the sign was preserved.
        value = security._shannon_entropy("a" * _SECRET_KEY_LEN)
        assert self._bits(value) == self._bits(-0.0)
        assert self._bits(value) != self._bits(0.0)

    def test_an_empty_token_is_positive_zero(self) -> None:
        # The early return is a literal 0.0, not a negated sum, so its sign
        # differs from the all-identical case above. Pin both.
        assert self._bits(security._shannon_entropy("")) == self._bits(0.0)

    def test_each_table_entry_equals_the_inline_expression_it_replaced(self) -> None:
        # The table is only a precomputation if every entry is what the inline
        # expression would have produced. The table covers exactly one length, so
        # check it exhaustively.
        table = security._ENTROPY_TERMS_KEY_LEN
        assert len(table) == _SECRET_KEY_LEN + 1
        for count in range(1, _SECRET_KEY_LEN + 1):
            want = (count / _SECRET_KEY_LEN) * math.log2(count / _SECRET_KEY_LEN)
            detail = f"term {count} of {_SECRET_KEY_LEN}: {table[count]!r} != {want!r}"
            assert self._bits(table[count]) == self._bits(want), detail

    def test_the_table_covers_the_only_length_the_gate_can_ask_about(self) -> None:
        # The table is built for one length rather than parameterised, so that
        # length must be the one gate 1 admits. If _SECRET_KEY_LEN ever changes
        # without the table following, every 40-char token would silently take the
        # inline fallback and the optimisation would be dead code.
        token = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        assert len(token) == _SECRET_KEY_LEN
        assert security._looks_like_secret_key(token)
        assert self._bits(security._shannon_entropy(token)) == self._bits(self._oracle(token))


class TestLowercaseRunExceedsStopsAtTheCap:
    """``_lowercase_run_exceeds`` replaced a full-maximum scan with a capped check.

    The caller only compares against a threshold, so the helper answers the
    threshold question directly. These tests pin the boundary in both directions
    -- a run exactly at the cap must NOT trip it, cap+1 must -- so an off-by-one
    in either direction fails.
    """

    @pytest.mark.parametrize(
        ("token", "cap", "expected"),
        [
            ("", 5, False),
            ("ABC123", 5, False),
            ("abcde", 5, False),  # exactly at cap
            ("abcdef", 5, True),  # cap + 1
            ("abcdeX", 5, False),  # run broken before exceeding
            ("abcdeXabcde", 5, False),  # two runs at cap, neither exceeds
            ("Xabcdefghij", 5, True),  # run starts after a non-lower char
            ("abcdefghij", 0, True),  # zero cap: any lowercase exceeds
            ("ABCDEF", 0, False),
            ("aB3" * 20, 5, False),  # never two lowercase in a row
        ],
    )
    def test_boundary(self, token: str, cap: int, expected: bool) -> None:
        assert security._lowercase_run_exceeds(token, cap) is expected

    def test_agrees_with_the_full_maximum_it_replaced(self) -> None:
        def longest_run(token: str) -> int:
            best = current = 0
            for ch in token:
                if ch.islower():
                    current += 1
                    best = max(best, current)
                else:
                    current = 0
            return best

        rng = random.Random(20260811)
        alphabet = string.ascii_letters + string.digits + "+/"
        for _ in range(3000):
            t = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 44)))
            cap = security._SECRET_MAX_LOWER_RUN
            assert security._lowercase_run_exceeds(t, cap) is (
                longest_run(t) > cap
            ), f"disagreement on {t!r}"

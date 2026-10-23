"""Tests for kiro_crew.security: the bare-secret classifier behind ``redact_credentials``.

Deep CamelCase paths are not bare secrets, the slash cap is the knee of the classifier, the
run-level fast path is an optimization only, the gates run in cost order without changing a
verdict, and the character-class helper, the Shannon-entropy terms and the lowercase-run check
agree with the definitions they replace.
"""

from __future__ import annotations

import base64
import math
import random
import re
import string
import struct
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.security import (
    _SECRET_KEY_LEN,
    _SECRET_MAX_SLASHES,
    REDACTED_CREDENTIAL_TAG,
    redact_credentials,
)

#: The canonical AWS documentation example secret access key. It CONTAINS
#: two ``/`` characters, which is what makes it the fixture for the
#: path-versus-secret boundary: a fix that treated every slash-bearing run
#: as a path would leak exactly this value.
_AWS_EXAMPLE_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
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
    def _as_host(monkeypatch: pytest.MonkeyPatch, host_id: str | None) -> None:
        """Make *host_id* the directory id the OS reports; None is "not macOS"."""
        from kiro_crew.security import redaction as redaction_mod

        monkeypatch.setattr(redaction_mod, "_host_darwin_user_dir_id", lambda: host_id)

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

    def test_the_darwin_fixtures_have_the_real_shape(self) -> None:
        from kiro_crew.security import redaction as redaction_mod

        for bucket, name in self.DARWIN_USER_DIR_IDS:
            assert re.fullmatch(r"[a-z0-9]{2}", bucket)
            assert re.fullmatch(r"[a-z0-9]{30}", name)
            assert name.endswith("0000gn")
            assert not set(name) & set("aeiou")
            # And it is an id ``confstr`` could have answered with.
            match = redaction_mod._DARWIN_USER_TEMP_DIR_RE.match(f"/var/folders/{bucket}/{name}/T/")
            assert match and match.group("id") == f"{bucket}/{name}"

    def test_the_produced_screenshot_path_survives_on_its_own_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported symptom: the model echoes the path and the image renders.

        Checked in every form the path reaches the redactor in: bare, resolved
        through ``/private``, as the markdown image the agent prompt asks for,
        and as the tool result's own ``Screenshot:`` line.
        """
        from kiro_crew.computer_use.types import SCREENSHOT_DIR_NAME

        spooled = self._produced_spool_path(tmp_path, monkeypatch)
        assert spooled.startswith(f"{SCREENSHOT_DIR_NAME}/shot-")
        for bucket, name in self.DARWIN_USER_DIR_IDS:
            self._as_host(monkeypatch, f"{bucket}/{name}")
            path = f"{self._darwin_root(bucket, name)}/{spooled}"
            for text in (
                path,
                f"/private{path}",
                f"![screenshot]({path})",
                f"Screenshot: {path}",
            ):
                assert redact_credentials(text) == (text, []), text

    def test_the_cache_dir_and_an_underscore_bucket_survive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, name = self.DARWIN_USER_DIR_IDS[0]
        for host_id, path in (
            (f"zz/{name}", f"/var/folders/zz/{name}/C/com.apple.QuickLook.thumbnailcache/index"),
            (f"_w/{name}", f"/var/folders/_w/{name}/T/kirocrew-computer-shots/shot-1.jpeg"),
        ):
            self._as_host(monkeypatch, host_id)
            assert redact_credentials(path) == (path, []), path

    @pytest.mark.parametrize(
        "letter",
        ["T", "C"],
    )
    @pytest.mark.parametrize(
        "tail",
        [
            # Shapes a real temp dir carries: an mkdtemp ``tmpXXXXXXXX`` dir (the
            # 24-byte overlap floor the exemption is set to), a UUID subdir,
            # macOS's TemporaryItems and a short CamelCase name.
            "/tmpab12cd34/x.txt",
            "/3F2504E0-4F89-11D3-9A0C-0305E82C3301/x",
            "/TemporaryItems/x",
            "/A1B2C3D4/x",
        ],
    )
    def test_a_benign_temp_path_survives_on_its_host(
        self, letter: str, tail: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Benign temp paths whose only key-shaped windows sit over the host id.

        Each window on these overlaps the withheld id by at least the exempt
        bound, so the exemption spares it and the whole path survives untouched.
        """
        for bucket, name in self.DARWIN_USER_DIR_IDS:
            self._as_host(monkeypatch, f"{bucket}/{name}")
            text = f"/var/folders/{bucket}/{name}/{letter}{tail}"
            assert redact_credentials(text) == (text, []), text

    def test_without_a_host_id_the_same_path_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is withheld off macOS, and the fixtures really trip the heuristic.

        This is also what keeps the tests above from passing vacuously: these
        ids are the shape the heuristic matches.
        """
        self._as_host(monkeypatch, None)
        for bucket, name in self.DARWIN_USER_DIR_IDS:
            path = f"{self._darwin_root(bucket, name)}/kirocrew-computer-shots/shot-1.jpeg"
            result, warnings = redact_credentials(path)
            assert result != path, path
            assert warnings

    def test_an_id_that_is_not_the_hosts_is_scanned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only the id the OS reported is withheld, never any string of its shape.

        In free text the id's bytes are the writer's to choose, and a chosen id
        plus a few bytes after ``/T`` is a key-shaped window the split would give
        up. Another id -- another user's, or one composed to carry a window -- is
        therefore judged exactly as it would be with no exemption at all.
        """
        (host_bucket, host_name), (bucket, name) = self.DARWIN_USER_DIR_IDS
        path = f"{self._darwin_root(bucket, name)}/kirocrew-computer-shots/shot-1.jpeg"
        self._as_host(monkeypatch, None)
        canonical = redact_credentials(path)
        self._as_host(monkeypatch, f"{host_bucket}/{host_name}")
        assert redact_credentials(path) == canonical
        assert canonical[0] != path

    @pytest.mark.parametrize(
        "label,shape",
        [
            ("key as a file in the temp dir", "{root}/{key}"),
            ("key with an extension", "{root}/{key}.txt"),
            ("key deeper in the temp dir", "{root}/kirocrew-computer-shots/{key}"),
            ("key in the markdown image form", "![shot]({root}/{key})"),
            # No exemption applies: the glued key means ``T`` does not end its component.
            ("key glued to the directory letter", "{root}{key}"),
            # No exemption applies: with the key glued on, the prefix does not start a path.
            ("key glued before the prefix", "{key}{root}/x"),
            ("key before the prefix across a slash", "{key}/{root}/x"),
        ],
    )
    def test_a_key_next_to_the_temp_dir_is_still_redacted(
        self, label: str, shape: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        root = self._darwin_root(bucket, name)
        for key in (_AWS_EXAMPLE_KEY, _ALT_SLASH_KEY, _NO_SLASH_KEY):
            result, warnings = redact_credentials(shape.format(root=root, key=key))
            assert key not in result, f"{label}: key leaked"
            assert warnings, label

    def test_only_the_piece_holding_the_key_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The id is withheld from the scan, so it is not what a hit replaces."""
        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        result, _ = redact_credentials(f"{self._darwin_root(bucket, name)}/{_AWS_EXAMPLE_KEY}")
        assert result == f"/var/folders/{bucket}/{name}{REDACTED_CREDENTIAL_TAG}"

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

    @pytest.mark.parametrize("shared", [1, 3])
    def test_a_key_sharing_the_ids_tail_is_still_redacted(
        self, shared: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 5.6's shape: a key reusing a few id bytes is below the exempt bound.

        The window begins at the key's first byte, which is the id's last byte,
        so it shares only ``shared`` bytes with the withheld id -- far under the
        24-byte exemption -- and must still redact. ``shared=1`` is the reported
        ``<last-id-byte>/T/`` case; ``shared=3`` pins that a three-byte overlap
        is judged too.
        """
        from kiro_crew.security import redaction as redaction_mod

        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        key = self._edge_key(name, shared)
        assert redaction_mod._looks_like_secret_key(key)
        # Glue the key where its leading reused bytes complete the id, so the id
        # regex still matches ``{bucket}/{name}`` ending in ``/T``.
        text = f"/var/folders/{bucket}/{name[:-shared]}" + key
        self._as_host(monkeypatch, f"{bucket}/{name}")
        assert redaction_mod._darwin_user_dir_ids(text), "the id must still be found"
        result, warnings = redact_credentials(text)
        assert key[len(name[-shared:]) :] not in result, "the key tail leaked"
        assert warnings
        assert result.startswith(f"/var/folders/{bucket}/{name}")

    def test_a_key_under_the_temp_dir_is_judged_as_on_any_other_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Withholding the id never removes a verdict on a real key.

        Every key -- including the separator-heavy ones the ceiling's accepted
        residual lets through when glued into a run -- that is redacted under an
        ordinary directory, where no exemption is near it, is still redacted
        under the temp dir. The id exemption can only ADD survival for a window
        that shares most of its bytes with the host id, never remove a verdict a
        real key earns on its own.
        """
        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        root = self._darwin_root(bucket, name)
        for key in (_AWS_EXAMPLE_KEY, _ALT_SLASH_KEY, _NO_SLASH_KEY, *self.SEPARATOR_HEAVY_KEYS):
            under_temp = REDACTED_CREDENTIAL_TAG in redact_credentials(f"{root}/{key}")[0]
            elsewhere = REDACTED_CREDENTIAL_TAG in redact_credentials(f"/srv/ci/tt/{key}")[0]
            assert under_temp or not elsewhere, key

    @pytest.mark.parametrize(
        "label,path",
        [
            ("T not ending its component", "/var/folders/zz/{name}/Tevil{tail}"),
            ("another directory letter", "/var/folders/zz/{name}/X{tail}"),
            ("the prefix does not start a path", "/Users/x/var/folders/zz/{name}/T{tail}"),
            ("a character glued before the prefix", "X/var/folders/zz/{name}/T{tail}"),
            ("a doubled slash before the prefix", "/srv//var/folders/zz/{name}/T{tail}"),
            ("a file URL", "file:///var/folders/zz/{name}/T{tail}"),
        ],
    )
    def test_a_look_alike_gets_the_canonical_verdict(
        self, label: str, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host's id outside the path grammar is scanned whole.

        Asserted as an identity with the no-host result, and each look-alike
        trips the heuristic, so the identity cannot hold merely because nothing
        was found.
        """
        _, name = self.DARWIN_USER_DIR_IDS[0]
        text = path.format(name=name, tail="/kirocrew-computer-shots/shot-1.jpeg")
        self._as_host(monkeypatch, None)
        canonical = redact_credentials(text)
        self._as_host(monkeypatch, f"zz/{name}")
        assert redact_credentials(text) == canonical, label
        assert canonical[0] != text, f"{label}: the look-alike must trip the heuristic"

    @pytest.mark.parametrize(
        "label,shape",
        [
            ("at the end of the text", "TMPDIR={root}"),
            ("followed by a newline", "{root}\n"),
            ("as a JSON string value", '{{"tmp": "{root}"}}'),
            ("inside a PosixPath repr", "PosixPath('{root}')"),
            ("as a call argument", "print({root})"),
            ("followed by a space", "{root} x"),
            # Pins the rule as a complement of the run alphabet, not a list of
            # accepted delimiters: ``|`` is in neither.
            ("followed by a pipe", "{root}|x"),
            # The byte after the letter ends the run, so these are the bare root
            # followed by another run, and the prefix and ``/T`` pieces are each
            # shorter than one key.
            ("T followed by an underscore", "{root}_evil/kirocrew-computer-shots/shot-1.jpeg"),
            ("T followed by a hyphen", "{root}-evil/kirocrew-computer-shots/shot-1.jpeg"),
            ("T followed by a dot", "{root}.evil/kirocrew-computer-shots/shot-1.jpeg"),
        ],
    )
    def test_a_bare_temp_root_survives_whatever_ends_it(
        self, label: str, shape: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A byte outside the run alphabet after the directory letter ends the run.

        ``print(tempfile.gettempdir())``, a JSON dump and a ``PosixPath`` repr all
        show the root followed by something other than ``/`` or the end of the
        text, and each is the same two-byte ``/T`` piece once the id is withheld.
        """
        for bucket, name in self.DARWIN_USER_DIR_IDS:
            self._as_host(monkeypatch, f"{bucket}/{name}")
            text = shape.format(root=self._darwin_root(bucket, name))
            assert redact_credentials(text) == (text, []), f"{label}: {text!r}"

    @pytest.mark.parametrize(
        "label,shape",
        [
            ("key in the next run after an underscored component", "{root}_evil/{key}"),
            ("key glued after an underscore", "{root}_{key}"),
        ],
    )
    def test_a_key_after_the_root_and_a_run_break_is_still_redacted(
        self, label: str, shape: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ending the run at the letter gives up nothing past it: the key is its own run."""
        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        root = self._darwin_root(bucket, name)
        for key in (_AWS_EXAMPLE_KEY, _ALT_SLASH_KEY, _NO_SLASH_KEY):
            result, warnings = redact_credentials(shape.format(root=root, key=key))
            assert key not in result, f"{label}: key leaked"
            assert result.startswith(root), f"{label}: the root itself must survive"
            assert warnings, label

    # ── the split is entered only for a run the whole-run scan would redact ──

    def test_a_split_run_is_gated_on_its_whole(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Withholding the id can only remove redaction, never add it.

        The tail piece is EXACTLY one key long and carries four separators, so
        on its own it is a standalone token the fragment ceiling does not reach.
        Scanned whole, the run is a fragment and every key-shaped window in it
        holds four or more ``/``, so the ceiling spares it. The verdict with the
        id withheld must be the no-host verdict, which is the base one.
        """
        _, name = self.DARWIN_USER_DIR_IDS[0]
        # An underscored bucket, as in the cache-dir test above: ``_`` ends the run
        # before the bucket letter, so the run is the letter, the name and the tail,
        # and the withheld id reaches the run's start. The one piece left is the tail.
        text = f"/var/folders/_w/{name}/T/kc/S/p0DXy4J2KQC3GfCAwFuLPZSVLmetMYXb"
        tail = text[text.index("/T/") :]
        assert len(tail) == 40 and tail.count("/") == 4
        self._as_host(monkeypatch, None)
        assert redact_credentials(text) == (text, [])
        self._as_host(monkeypatch, f"_w/{name}")
        assert redact_credentials(text) == (text, [])

    def test_a_split_run_that_decodes_to_text_keeps_the_whole_run_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The encoded-text exclusion reads the whole run's alignment.

        The run base64-decodes to printable text and is left to the decode-and-scan
        pass. The tail piece begins at a different offset modulo four, so cut
        loose it decodes to garbage and its windows read as key-shaped.
        """
        from kiro_crew.security import redaction as redaction_mod

        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        head = f"{self._darwin_root(bucket, name)}/"
        assert len(head) % 4 == 1
        words = (
            b"The quick brown fox jumps over the lazy dog. Pack my box with five dozen "
            b"liquor jugs. How vexingly quick daft zebras jump! Sphinx of black quartz, "
            b"judge my vow. Crazy Frederick bought many very exquisite opal jewels. "
            b"Jinxed wizards pluck ivy from the big quilt."
        )
        # The block of four holding the head's last ``/`` also holds the first
        # three tail characters, so those encode the first two words bytes at the
        # whole run's alignment; the rest is plain base64 of the remainder.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        first, second = words[0], words[1]
        rest = words[2:]
        rest = rest[: len(rest) - len(rest) % 3]
        tail = (
            alphabet[(first >> 4) & 0xF]
            + alphabet[((first & 0xF) << 2) | (second >> 6)]
            + alphabet[second & 0x3F]
            + base64.b64encode(rest).decode()
        )
        text = head + tail
        assert redaction_mod._decodes_to_printable_text(text)
        assert redaction_mod._contains_bare_secret(text[text.index("/T/") :])
        self._as_host(monkeypatch, None)
        assert redact_credentials(text) == (text, [])
        self._as_host(monkeypatch, f"{bucket}/{name}")
        assert redact_credentials(text) == (text, [])

    @pytest.mark.parametrize("key", [_AWS_EXAMPLE_KEY, _ALT_SLASH_KEY, _NO_SLASH_KEY])
    @pytest.mark.parametrize("prefix", ["", "/private"])
    def test_a_printable_piece_cannot_hide_a_key_in_a_nonprintable_run(
        self, key: str, prefix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A piece's printable decode must not replace the whole run's verdict."""
        from kiro_crew.security import redaction as redaction_mod

        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        # /T/A occupies one base64 quartet; the filler dominates its printable
        # decode while the host-id prefix shifts the whole run's alignment.
        filler = (
            "A" + base64.b64encode(b"The quick brown fox jumps over the lazy dog. " * 9).decode()
        )
        text = f"{prefix}{self._darwin_root(bucket, name)}/{filler}{key}"
        piece = text[text.index("/T/") :]
        assert redaction_mod._BARE_SECRET_RUN_RE.fullmatch(text)
        assert redaction_mod._looks_like_secret_key(key)
        assert redaction_mod._decodes_to_printable_text(piece)
        assert not redaction_mod._decodes_to_printable_text(text)
        assert redaction_mod._contains_bare_secret(text)
        result, warnings = redact_credentials(text)
        assert key not in result
        assert result.startswith(f"{prefix}/var/folders/{bucket}/{name}")
        assert warnings

    @pytest.mark.parametrize("key", [_AWS_EXAMPLE_KEY, _ALT_SLASH_KEY, _NO_SLASH_KEY])
    def test_benign_filler_keeps_the_root_and_redacts_the_key(
        self, key: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        self._as_host(monkeypatch, f"{bucket}/{name}")
        root = self._darwin_root(bucket, name)
        result, warnings = redact_credentials(f"{root}/kirocrew-computer-shots/{key}")
        assert key not in result
        assert result.startswith(root)
        assert warnings

    def test_the_host_id_is_what_confstr_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Asked once of the OS; a refusal or an off-grammar answer withholds nothing."""
        from kiro_crew.security import redaction as redaction_mod

        bucket, name = self.DARWIN_USER_DIR_IDS[0]
        calls: list[int] = []

        def answer(value: object) -> Callable[[int], str]:
            def confstr(key: int) -> str:
                calls.append(key)
                if isinstance(value, Exception):
                    raise value
                return str(value)

            return confstr

        read = redaction_mod._host_darwin_user_dir_id
        try:
            with monkeypatch.context() as mp:
                for reported, expected in (
                    (f"/var/folders/{bucket}/{name}/T/", f"{bucket}/{name}"),
                    (f"/private/var/folders/{bucket}/{name}/T/", f"{bucket}/{name}"),
                    ("/tmp/", None),
                    (f"/var/folders/{bucket}/{name}x/T/", None),
                    (OSError(22, "Invalid argument"), None),
                ):
                    read.cache_clear()
                    calls.clear()
                    mp.setattr(redaction_mod.os, "confstr", answer(reported), raising=False)
                    assert read() == expected, reported
                    assert read() == expected
                    assert calls == [redaction_mod._CS_DARWIN_USER_TEMP_DIR], "asked once"
                # Windows: ``os.confstr`` does not exist, which must read as no host
                # id rather than raise out of every redaction of a macOS path.
                read.cache_clear()
                mp.delattr(redaction_mod.os, "confstr", raising=False)
                assert read() is None
                assert redact_credentials("/var/folders/zz/x/T/a")[0] == "/var/folders/zz/x/T/a"
        finally:
            read.cache_clear()

    @pytest.mark.skipif(sys.platform != "darwin", reason="reads the real macOS confstr")
    def test_the_real_temp_dir_survives_on_macos(self) -> None:
        from kiro_crew.security import redaction as redaction_mod

        redaction_mod._host_darwin_user_dir_id.cache_clear()
        host_id = redaction_mod._host_darwin_user_dir_id()
        assert host_id is not None, "confstr(_CS_DARWIN_USER_TEMP_DIR) must report the id"
        path = f"/var/folders/{host_id}/T/kirocrew-computer-shots/shot-1700000000000-ab12cd34.jpeg"
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

    @pytest.mark.parametrize("key", SEPARATOR_HEAVY_KEYS)
    def test_a_standalone_key_is_redacted_however_many_separators_it_holds(self, key: str) -> None:
        """The ceiling applies to a FRAGMENT, and a standalone key is not one.

        A genuine AWS secret access key is uniformly random base64, so roughly 1
        in 200 of them carries four or more separators. Such a key echoed on its
        own -- in prose, in a JSON array element, after a label, on its own line --
        is a run of exactly ``_SECRET_KEY_LEN`` chars, which is the token somebody
        wrote rather than a window cut out of anything. It must redact whatever its
        separator count, and this is what pins that.
        """
        assert len(key) == _SECRET_KEY_LEN
        assert key.count("/") > _SECRET_MAX_SLASHES, "fixture must exceed the ceiling"
        assert security._looks_like_secret_key(key), "fixture must be key-shaped"
        for label, text in (
            ("standalone", key),
            ("in prose", f"the key is {key} - keep it safe"),
            ("in a JSON array", f'{{"keys": ["{key}"]}}'),
            ("after a label", f"SECRET_ACCESS_KEY={key}"),
        ):
            result, _ = redact_credentials(text)
            assert key not in result, f"{label}: key leaked: {key!r}"

    # ── secrets must still be redacted, in every path context ──

    @pytest.mark.parametrize(
        "label,text",
        [
            ("standalone slash-bearing key", _AWS_EXAMPLE_KEY),
            ("standalone second slash-bearing key", _ALT_SLASH_KEY),
            ("standalone slash-free key", _NO_SLASH_KEY),
            ("key as a path component", f"/srv/ci/keys/{_AWS_EXAMPLE_KEY}"),
            ("second key as a path component", f"/srv/ci/keys/{_ALT_SLASH_KEY}"),
            ("slash-free key as a component", f"/srv/ci/keys/{_NO_SLASH_KEY}"),
            ("key in a URL path", f"https://example.com/v1/data/{_AWS_EXAMPLE_KEY}"),
            ("key with components after it", f"/srv/ci/keys/dir/{_AWS_EXAMPLE_KEY}/more/here"),
            ("key glued after a prefix", f"/srv/ci/keys/pfx{_AWS_EXAMPLE_KEY}"),
            ("key glued before a suffix", f"/srv/ci/keys/{_AWS_EXAMPLE_KEY}sfx"),
            ("key glued on both sides", f"/srv/ci/keys/X{_AWS_EXAMPLE_KEY}X"),
            ("key buried in one component", f"/srv/ci/keys/pfx{_NO_SLASH_KEY}sfx"),
            # A key whose true offset is not where any component begins, and whose
            # component-start window is a word-shaped prefix plus a partial key.
            # A gate on the window's own bytes still classifies the key itself.
            (
                "key glued after a word-shaped prefix",
                f"/aa/bb/cc/loremipsumdolorsitam{_AWS_EXAMPLE_KEY}",
            ),
            (
                "second key glued after a word-shaped prefix",
                f"/aa/bb/cc/loremipsumdolorsitam{_ALT_SLASH_KEY}",
            ),
        ],
    )
    def test_secret_is_still_redacted(self, label: str, text: str) -> None:
        result, warnings = redact_credentials(text)
        assert REDACTED_CREDENTIAL_TAG in result, f"{label}: secret leaked: {text!r}"
        assert warnings, label

    def test_a_key_at_the_ceiling_is_still_found_inside_a_run(self) -> None:
        """The ceiling is inclusive: exactly ``_SECRET_MAX_SLASHES`` separators pass.

        A key-shaped window carrying exactly the ceiling's count, glued into a
        path so the run is a fragment, must still redact. Pins the boundary the
        residual test above sits one past.
        """
        key = "1GQmz9m/Mbpp/8bv4mTv" "EFNQR/Y8O2C2SSYEDQYD"
        assert len(key) == _SECRET_KEY_LEN
        assert key.count("/") == _SECRET_MAX_SLASHES
        assert security._looks_like_secret_key(key)
        result, warnings = redact_credentials(f"/srv/ci/keys/{key}")
        assert REDACTED_CREDENTIAL_TAG in result
        assert warnings

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

    def test_the_ceiling_is_what_saves_the_reported_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raise the ceiling and the reported path is swallowed again.

        Asserted directly so the benign-path fixtures above cannot silently stop
        depending on the ceiling.
        """
        path = (
            "/Volumes/workplace/TRAM/QuickProp2/src/ATVTramQuickPropCDK/lib/config/consumerVpcs.ts"
        )
        assert redact_credentials(path)[0] == path
        monkeypatch.setattr(security, "_SECRET_MAX_SLASHES", _SECRET_KEY_LEN)
        assert redact_credentials(path)[0] != path

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

    def test_secret_glued_into_a_long_mixed_run_is_still_found(self) -> None:
        # The fast path must not retire a run that DOES contain a secret. A real
        # key glued to base64 padding on both sides makes a 60-char run whose
        # only qualifying window is at a non-zero offset.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        run = "abc123XYZ/" + secret + "0123456789"
        assert len(run) > _SECRET_KEY_LEN
        assert security._contains_bare_secret(run) is True
        result, warnings = redact_credentials(f"token={run}")
        assert secret not in result
        assert warnings

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

    def test_a_real_secret_key_still_redacts_end_to_end(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        result, warnings = redact_credentials(f"AWS_SECRET={secret} keep this prose")
        assert secret not in result
        assert warnings
        assert "keep this prose" in result


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

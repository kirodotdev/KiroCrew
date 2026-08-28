"""Names that came out of an archive are escaped before they reach the terminal.

An untrusted archive supplies its own member names, manifest keys and root directory
names. Two of the sites print while REJECTING a hostile entry, so the raw value there is
the payload itself.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap

# A cursor-up plus carriage return: enough to overwrite the line printed above it.
EVIL = "\x1b[1A\rinnocent-looking"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


class TestTarMemberNamesAreEscaped:
    def test_a_traversal_rejection_does_not_print_the_raw_name(self, capsys):
        info = tarfile.TarInfo(name=f"../{EVIL}")
        assert snap._data_filter(info) is None
        out = capsys.readouterr().out
        assert "\x1b" not in out, "the rejected member's escape sequence reached the terminal"
        assert "\\x1b" in out, out

    def test_a_symlink_rejection_does_not_print_the_raw_name(self, capsys):
        info = tarfile.TarInfo(name=f"payload/{EVIL}")
        info.type = tarfile.SYMTYPE
        assert snap._data_filter(info) is None
        out = capsys.readouterr().out
        assert "\x1b" not in out, "the rejected symlink's escape sequence reached the terminal"


class TestManifestDerivedNamesAreEscaped:
    def test_an_unknown_component_key_is_escaped(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps({"version": 3, "components": {"memory": "unresolved", EVIL: "x"}}),
            encoding="utf-8",
        )
        known = snap._manifest_components(payload)
        out = capsys.readouterr().out
        assert "memory" in known
        assert "\x1b" not in out, "a hostile manifest key reached the terminal raw"
        assert "\\x1b" in out, out

    def test_the_manifest_summary_escapes_its_fields(self, tmp_path, capsys):
        payload = tmp_path / "snap"
        payload.mkdir()
        (payload / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "created_at": EVIL,
                    "user": EVIL,
                    "hostname": EVIL,
                    "components": {EVIL: EVIL},
                }
            ),
            encoding="utf-8",
        )
        snap._print_manifest(payload)
        out = capsys.readouterr().out
        assert "\x1b" not in out, "a manifest field reached the terminal raw"


class TestArchiveRootNamesAreEscaped:
    def test_two_roots_are_named_without_their_escapes(self, home, tmp_path, capsys):
        """The ambiguity refusal names what it found, so it must escape those names.

        The roots are written as tar METADATA rather than as real directories. That is
        both the portable form -- Windows rejects control characters in a filename
        outright, so the escape could never exist on disk there -- and the honest one: a
        hostile root name reaches us inside an archive someone else wrote, which is
        exactly the surface being tested.
        """
        bundle = tmp_path / "two-roots.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            for name in (f"kirocrew-snapshot-{EVIL}", "kirocrew-snapshot-20260101T000000Z"):
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)

        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "\x1b" not in out, "an archive root name reached the terminal raw"


class TestTheRuleCoversEveryArchiveDerivedPrint:
    def test_no_site_prints_a_raw_member_or_manifest_name(self):
        """A new print of archive-derived text must go through the helper.

        Pinned structurally because the vulnerable sites are spread across the tar
        filter, the manifest reader and the root-selection refusal -- three places that
        do not otherwise look alike, which is how two of them were missed.
        """
        source = Path(snap.__file__).read_text(encoding="utf-8")
        assert "{info.name}" not in source, "a tar member name is interpolated without _safe_name"
        assert "', '.join(dropped)" not in source, "manifest keys are joined without _safe_name"
        assert (
            "sorted(d.name for d in snap_dirs)" not in source
        ), "archive root names are joined without _safe_name"

    def test_the_helper_escapes_control_bytes_and_keeps_printable_text(self):
        """The escaping property itself, asserted directly on `_safe_name`.

        This used to compare `_safe_name` against a shared sanitiser that lived beside the
        off-host destination code; that helper is gone and the escaping now lives inside
        `_safe_name`. The property is unchanged: every control byte is rendered as a
        visible `\\xNN` escape so it cannot drive the terminal, while ordinary printable
        text is left intact and an empty value falls back.
        """
        got = snap._safe_name(EVIL)
        # No raw control byte survives -- neither the cursor-up escape nor the CR.
        assert "\x1b" not in got and "\r" not in got, got
        # Each is rendered as its visible \xNN form instead.
        assert "\\x1b" in got, got
        assert "\\x0d" in got, got
        # The printable remainder is untouched.
        assert "innocent-looking" in got, got
        assert snap._safe_name("", "fallback") == "fallback"

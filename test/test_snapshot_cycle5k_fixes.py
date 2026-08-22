"""Names that came out of an archive are escaped before they reach the terminal.

The sanitiser existed and was justified for S3 object keys, then was not applied to the
other class of bytes an attacker supplies: the ones inside the bundle. Two of the sites
print while REJECTING a hostile entry, so the raw value there is the payload itself.
"""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote

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

    def test_the_helper_delegates_to_the_shared_sanitiser(self):
        assert snap._safe_name(EVIL) == snapshot_remote.safe_for_terminal(EVIL)
        assert snap._safe_name("", "fallback") == "fallback"


class TestTheBackupCommandDispatches:
    """The `backup` subcommand was wired but never exercised, so nothing held its import.

    It is resolved lazily inside its dispatch branch, because `kirocrew gateway` routes
    through this module and must not load the off-host destination code to reach
    readiness. What that costs is measured in `test_snapshot_cycle5b_fixes.py`; this
    covers the wiring, which is what kept the omission invisible.
    """

    def test_the_handler_is_resolved_in_its_own_branch(self):
        import inspect

        from kiro_crew import cli

        source = inspect.getsource(cli)
        assert (
            'importlib.import_module("kiro_crew.backup_cli")' in source
        ), "the dispatch branch must resolve the backup handler"
        assert (
            "\nfrom kiro_crew.backup_cli import" not in source
        ), "a module-scope import puts the backup subsystem on the gateway boot path"

    def test_the_command_reaches_the_handler(self, monkeypatch):
        from kiro_crew import cli

        seen: list[object] = []
        import kiro_crew.backup_cli as bc

        monkeypatch.setattr(bc, "backup_main", lambda args: seen.append(args) or 0)
        monkeypatch.setattr(sys, "argv", ["kirocrew", "backup", "list"])
        try:
            cli.main()
        except SystemExit as e:  # argparse/other subcommands may exit cleanly
            assert e.code in (None, 0), e.code
        assert seen, "the backup subcommand did not reach its handler"

    def test_a_nonzero_return_becomes_the_exit_code(self, monkeypatch):
        import kiro_crew.backup_cli as bc
        from kiro_crew import cli

        monkeypatch.setattr(bc, "backup_main", lambda args: 3)
        monkeypatch.setattr(sys, "argv", ["kirocrew", "backup", "list"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 3

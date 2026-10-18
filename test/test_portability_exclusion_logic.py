"""Which archive paths an export leaves out.

Secret basenames, pid files, the snapshot and outbox directories and the ``skills/auto``
rule exclude what they name, also when the relative path arrives Windows-separated, and
archive member names stay POSIX-separated.

Export packaging and containment are pinned in ``test_portability.py``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath, PureWindowsPath

import portability_helpers
import pytest

from kiro_crew import portability
from kiro_crew.portability import _is_excluded, create_export_zip

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir


# ── Exclusion Logic Tests ──


class TestExclusionLogic:
    def test_excludes_env_file(self):
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath(".env"))

    def test_excludes_local_secret(self):
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath(".local_secret"))

    def test_excludes_sel_hmac_key_at_trust_path(self):
        # The SEL key moved to trust/sel_hmac.key; exclusion is basename-based
        # so the key must stay excluded at BOTH the new and legacy locations.
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath("sel_hmac.key"))
        assert _is_excluded(PurePosixPath("trust/sel_hmac.key"))

    def test_excludes_pid_files(self):
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath("gateway.pid"))
        assert _is_excluded(PurePosixPath("some/nested/thing.pid"))

    def test_excludes_snapshots_dir(self):
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath("snapshots/backup.tar.gz"))

    def test_excludes_outbox_dir(self):
        from pathlib import PurePosixPath

        assert _is_excluded(PurePosixPath("outbox/file.txt"))

    def test_allows_config_json(self):
        from pathlib import PurePosixPath

        assert not _is_excluded(PurePosixPath("config.json"))

    def test_allows_memory_files(self):
        from pathlib import PurePosixPath

        assert not _is_excluded(PurePosixPath("workspace/memory/preferences.md"))

    def test_allows_skills(self):
        from pathlib import PurePosixPath

        assert not _is_excluded(PurePosixPath("skills/my-skill/SKILL.md"))


class TestExclusionsSurviveAWindowsSeparator:
    """`_is_excluded` must be asked in the separator its rules are written in.

    `_keep_for_export` built its argument as `PurePosixPath(str(rel))`. On Windows
    `str(rel)` is backslash-separated, so `PurePosixPath` parses the WHOLE relative
    path as one component: `.name` becomes `workspace\\notes\\.env` and `.parts`
    has length one. The `EXPORT_EXCLUDE` basename set and the `EXCLUDE_DIRS` walk
    then both stop matching, and `workspace/notes/.env` — a credential file the
    export exists to keep out — went into an archive the user downloads and hands
    on.

    Driven through `PureWindowsPath` rather than through a real export, so the
    assertion is PLATFORM-INDEPENDENT: `parts` and `str()` differ on every host,
    so these fail everywhere if the `PurePosixPath(str(rel))` spelling returns.
    """

    @pytest.mark.parametrize(
        "rel",
        [
            "workspace/notes/.env",
            "workspace/deep/.local_secret",
            "workspace/a/sel_hmac.key",
            "workspace/b/telemetry_salt",
            "workspace/nested/gateway.pid",
        ],
    )
    def test_an_excluded_basename_is_excluded_below_the_top_level(self, rel: str) -> None:
        assert (
            portability._keep_for_export(PureWindowsPath(rel)) is False
        ), f"{rel} would be packaged on Windows"

    @pytest.mark.parametrize("bad_dir", sorted(portability.EXCLUDE_DIRS))
    def test_an_excluded_directory_is_excluded_below_the_top_level(self, bad_dir: str) -> None:
        rel = PureWindowsPath(f"workspace/{bad_dir}/inner.txt")
        assert (
            portability._keep_for_export(rel) is False
        ), f"workspace/{bad_dir}/ would be packaged on Windows"

    def test_an_ordinary_nested_file_is_still_kept(self) -> None:
        """Negative control: the rebuild must not start excluding everything."""
        assert portability._keep_for_export(PureWindowsPath("workspace/notes/ok.txt")) is True
        assert portability._keep_for_export(PureWindowsPath("skills/manual/s1.md")) is True

    def test_the_skills_auto_rule_still_reads_parts(self) -> None:
        assert portability._keep_for_export(PureWindowsPath("skills/auto/gen.md")) is False
        assert portability._keep_for_export(PureWindowsPath("workspace/auto/keep.md")) is True

    def test_the_guard_can_actually_fail(self) -> None:
        """Guard the guard: the OLD spelling really does miss these.

        Without this the tests above would pass on a build where `str()` and
        `parts` happened to agree, and would be pinning nothing.
        """
        rel = PureWindowsPath("workspace/notes/.env")
        assert len(PurePosixPath(str(rel)).parts) == 1  # the bug, reproduced
        assert PurePosixPath(str(rel)).name != ".env"
        assert PurePosixPath(*rel.parts).name == ".env"  # the fix

    def test_the_archive_name_is_posix_separated(self, patched_config_dir) -> None:
        """A zip member name is POSIX-separated by spec, on every host."""
        nested = patched_config_dir / "workspace" / "deep" / "keep.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("ours", encoding="utf-8")

        zip_bytes, _ = create_export_zip()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
        assert any(n.endswith("workspace/deep/keep.md") for n in names), names
        assert not any("\\" in n for n in names), names

"""Windows support for the Spec Builder app.

Pinned in the app's own suite because the manifest is a published capability
label, not an enable gate: ``apps/routes.py`` only calls ``supports_platform``
for a ``platform.installMode == "client"`` app, which no builtin sets. Omitting
the block falls back to the implicit ``["macos", "linux"]`` default, which
misreports Windows support on the App Store detail page even though every part
of this app -- in-process route handlers plus JSON/markdown spec files under
the crew home -- runs there unchanged.
"""

import json
from pathlib import Path

from kiro_crew import platform_compat

APP_DIR = Path(__file__).resolve().parents[1]


def _manifest() -> dict:
    return json.loads((APP_DIR / "app.json").read_text(encoding="utf-8"))


# --- phase 2: encoding pinned on every JSON state read ---
#
# All four files are WRITTEN as UTF-8 (``text.encode("utf-8")`` in the atomic
# writer), but were READ with ``encoding=None``, i.e. the platform default: cp936
# under a zh-CN Windows install, cp1252 under en-US. Any non-ASCII byte in a spec
# name, project path or branch then decoded wrong or raised -- and
# ``UnicodeDecodeError`` is a ``ValueError``, NOT an ``OSError`` or a
# ``JSONDecodeError``, so it slipped past the very handlers written to degrade
# these reads to empty state: the spec list 500'd instead of coming back empty.


class _ReadTextSpy:
    """Records the ``encoding`` every ``Path.read_text`` was called with.

    Asserted on rather than the decoded bytes because the default encoding is a
    property of the HOST: a round-trip test passes on the UTF-8 CI runner whether
    or not the call pins anything, so it cannot catch a regression where the
    developer's shard is the only one that stays green.
    """

    def __init__(self, monkeypatch) -> None:
        self.seen: dict[str, str | None] = {}
        real = Path.read_text

        def spy(path_self, *args, **kwargs):
            self.seen[Path(path_self).name] = kwargs.get("encoding")
            return real(path_self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)


def test_settings_read_pins_utf8_and_round_trips_a_non_ascii_base_path(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import repository as repo

    project = tmp_path / "项目-café"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"base_path": str(project), "model": ""}), encoding="utf-8")
    monkeypatch.setattr(repo, "_settings_path", lambda: path)
    spy = _ReadTextSpy(monkeypatch)

    assert repo._load_settings()["base_path"] == str(project)
    assert spy.seen["settings.json"] == "utf-8"


def test_deleted_tombstones_read_pins_utf8_and_keeps_non_ascii_entries(tmp_path, monkeypatch):
    """A tombstone that fails to decode is a DELETED spec that reappears."""
    from kiro_crew.apps.builtins.spec_builder.backend import repository as repo

    gone = str(tmp_path / "已删除的规格")
    path = tmp_path / "deleted.json"
    path.write_text(json.dumps([gone]), encoding="utf-8")
    monkeypatch.setattr(repo, "_deleted_path", lambda: path)
    spy = _ReadTextSpy(monkeypatch)

    assert repo._load_deleted() == [gone]
    assert spy.seen["deleted.json"] == "utf-8"


def test_index_snapshot_read_pins_utf8(tmp_path, monkeypatch):
    """The index is the whole spec list; an undecodable read is a 500 on every poll."""
    from kiro_crew.apps.builtins.spec_builder.backend import repository as repo

    path = tmp_path / "index.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(repo, "_index_path", lambda: path)
    spy = _ReadTextSpy(monkeypatch)

    assert repo._load_index_snapshot() == ({}, True)
    assert spy.seen["index.json"] == "utf-8"


def test_recent_projects_read_pins_utf8_and_keeps_a_non_ascii_folder(tmp_path, monkeypatch):
    from kiro_crew.apps.builtins.spec_builder.backend import repository as repo

    project = tmp_path / "最近的项目"
    project.mkdir()
    (tmp_path / "recent_projects.json").write_text(json.dumps([str(project)]), encoding="utf-8")
    monkeypatch.setattr(repo, "config_dir", lambda: tmp_path)
    spy = _ReadTextSpy(monkeypatch)

    assert repo._read_recent_projects() == [str(project)]
    assert spy.seen["recent_projects.json"] == "utf-8"


def test_a_corrupt_state_file_degrades_to_empty_instead_of_raising(tmp_path, monkeypatch):
    """Pinning UTF-8 leaves one real failure mode: a file that is not UTF-8.

    It must take the same degrade-to-empty path as a truncated one, so
    ``UnicodeDecodeError`` has to be in the caught tuple -- it is not an
    ``OSError`` and not a ``JSONDecodeError``.
    """
    from kiro_crew.apps.builtins.spec_builder.backend import repository as repo

    settings = tmp_path / "settings.json"
    settings.write_bytes(b'{"base_path": "\xff\xfe not utf-8"}')
    deleted = tmp_path / "deleted.json"
    deleted.write_bytes(b'["\xff\xfe"]')
    index = tmp_path / "index.json"
    index.write_bytes(b'{"\xff\xfe": {}}')
    monkeypatch.setattr(repo, "_settings_path", lambda: settings)
    monkeypatch.setattr(repo, "_deleted_path", lambda: deleted)
    monkeypatch.setattr(repo, "_index_path", lambda: index)

    assert repo._load_settings() == {"base_path": "", "model": ""}
    assert repo._load_deleted() == []
    assert repo._load_index_snapshot() == ({}, False)


# --- phase 2: the turn-lock case fold is macOS-only ON PURPOSE ---


def test_turn_lock_fold_is_macos_only_because_normcase_already_folds_windows():
    """Windows must NOT be added to this flag.

    ``_decision_key`` runs ``os.path.normcase``, which lowercases and unifies
    separators on Windows, so ``MySpec`` and ``myspec`` already collapse to one
    turn-lock key there. Darwin is the case-insensitive-by-default filesystem
    ``normcase`` leaves alone (it is a no-op on POSIX), which is why it alone
    needs the extra fold. Pinned because "Windows is case-insensitive too" is a
    tempting patch that only double-folds.
    """
    from kiro_crew.apps.builtins.spec_builder.backend import runtime

    assert runtime._CASE_FOLD_TURN_KEYS is platform_compat.IS_MACOS

    mixed, lower = r"C:\p\.kiro\specs\MySpec", r"C:\p\.kiro\specs\myspec"
    if platform_compat.IS_WINDOWS:
        assert runtime._turn_key(mixed) == runtime._turn_key(lower)


def test_turn_key_folding_collapses_case_variants_when_enabled(monkeypatch):
    """The fold itself, independent of which platform turns it on."""
    from kiro_crew.apps.builtins.spec_builder.backend import runtime

    mixed, lower = "/p/.kiro/specs/MySpec", "/p/.kiro/specs/myspec"

    monkeypatch.setattr(runtime, "_CASE_FOLD_TURN_KEYS", True)
    assert runtime._turn_key(mixed) == runtime._turn_key(lower)

    monkeypatch.setattr(runtime, "_CASE_FOLD_TURN_KEYS", False)
    if not platform_compat.IS_WINDOWS:  # normcase would fold them anyway
        assert runtime._turn_key(mixed) != runtime._turn_key(lower)

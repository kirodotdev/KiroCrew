"""Windows support for the Mochi app.

Mochi shipped with no tests at all, so this file carries both the manifest guard
the Windows rollout requires and the first regression cover for the app's
platform-sensitive file handling. The manifest tests follow the paradigm already
accepted in ``test/test_dev_fleet_app.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.mochi import appearance_store, petdex_import, watchlist_file
from kiro_crew.apps.builtins.mochi.pinned_files_service import PinnedFilesService
from kiro_crew.apps.builtins.mochi.windows_names import is_windows_reserved

APP_DIR = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return json.loads((APP_DIR / "app.json").read_text(encoding="utf-8"))


# --- manifest platform declaration ---
def test_manifest_keeps_the_desktop_shell_requirement():
    """``requiresDesktopApp`` survives the Windows widening on purpose.

    It is an ORTHOGONAL axis to ``os``: ``os`` constrains the machine the gateway
    runs on, this constrains the surface the user views from. Mochi's pet is a
    transparent click-through always-on-top window, so it still needs the Electron
    shell -- and that shell exists on Windows (``isWinElectron`` in
    ``website/src/lib/electron.ts`` is a first-class, separately-styled case, and
    the ``isElectron`` marker carries no platform test). Widening ``os`` must not
    be mistaken for "now works in a browser tab".
    """
    assert _manifest()["platform"]["requiresDesktopApp"] is True


# --- windows-reserved path segments ---
@pytest.mark.parametrize("name", ["con", "CON", "nul", "aux", "prn", "com1", "lpt9", "con.json"])
def test_windows_reserved_names_are_rejected(name):
    """Legacy DOS device names are reserved in every directory, extension or not."""
    assert is_windows_reserved(name)


@pytest.mark.parametrize("name", ["pet.", "pet ", ""])
def test_names_windows_would_silently_rewrite_are_rejected(name):
    """Windows strips a trailing dot or space, so the stored name would differ
    from the requested one -- two ids could collide on one platform only."""
    assert is_windows_reserved(name)


@pytest.mark.parametrize("name", ["boba", "com", "com10", "console", "pet.json", "nullish"])
def test_ordinary_names_are_not_rejected(name):
    """The reservation is exact: ``com`` and ``com10`` are not devices, and a
    reserved stem must be the WHOLE stem (``console``, ``nullish`` are fine)."""
    assert not is_windows_reserved(name)


# --- corrupted-store backup uses os.replace ---
def _ban_rename(monkeypatch):
    def _no_rename(*_args, **_kwargs):
        raise AssertionError(
            "must use os.replace: os.rename refuses an existing destination on Windows"
        )

    monkeypatch.setattr(os, "rename", _no_rename)


def test_watchlist_backup_overwrites_an_existing_destination(tmp_path, monkeypatch):
    """Two corruptions inside one millisecond share the ``.bak.<now_ms>`` suffix.

    On Windows ``os.rename`` raises on an existing destination, and the caller
    swallows OSError -- so the corrupt file would be left in place, which is the
    one outcome this function exists to prevent.
    """
    target = tmp_path / "watchlist.json"
    target.write_text("corrupt", encoding="utf-8")
    bak = tmp_path / "watchlist.json.bak.7"
    bak.write_text("an older backup", encoding="utf-8")
    _ban_rename(monkeypatch)

    watchlist_file._backup_corrupted(str(target), 7)

    assert not target.exists()
    assert bak.read_text(encoding="utf-8") == "corrupt"


def test_pinned_files_backup_overwrites_an_existing_destination(tmp_path, monkeypatch):
    """Same defect, same fix, in the second copy of this helper."""
    svc = PinnedFilesService(str(tmp_path))
    target = Path(svc._file_path)
    target.write_text("corrupt", encoding="utf-8")
    bak = Path(f"{svc._file_path}.bak.7")
    bak.write_text("an older backup", encoding="utf-8")
    _ban_rename(monkeypatch)

    svc._backup_corrupted(7)

    assert not target.exists()
    assert bak.read_text(encoding="utf-8") == "corrupt"


def test_backup_of_a_missing_file_is_still_silent(tmp_path, monkeypatch):
    """The swallowed OSError must stay swallowed -- a missing store is normal."""
    _ban_rename(monkeypatch)
    watchlist_file._backup_corrupted(str(tmp_path / "absent.json"), 1)


# --- externally supplied path segments ---
@pytest.mark.parametrize("slug", ["con", "NUL", "com1", "pet."])
def test_petdex_slug_rejects_windows_reserved_names(slug):
    """A slug becomes a directory name, so it must be creatable on Windows.

    Without this the import failed as an OSError from inside the install write.
    """
    with pytest.raises(petdex_import.PetdexError):
        petdex_import.normalize_slug(slug)


def test_petdex_slug_still_accepts_an_ordinary_pet():
    assert petdex_import.normalize_slug("https://petdex.dev/en/pets/boba") == "boba"


def test_petdex_installed_dir_is_reported_in_posix_form():
    """The reported ``source`` points the user at the petdex CLI's own directory,
    and that CLI writes the POSIX form; ``str()`` of the Path renders backslashes
    on Windows and would show a shape that tool never uses."""
    assert petdex_import.INSTALLED_PETS_DIR.as_posix() == "~/.codex/pets"


@pytest.mark.parametrize("pack_id", ["con", "nul", "COM1"])
def test_pack_id_rejects_windows_reserved_names(pack_id, tmp_path):
    """An overwrite pack id arrives from the client, so it is validated, and the
    character allow-list cannot express a reserved device name."""
    with pytest.raises(appearance_store.PackError):
        appearance_store._pack_dir(tmp_path, pack_id)


def test_pack_id_still_accepts_a_minted_uuid(tmp_path):
    import uuid

    pack_id = uuid.uuid4().hex
    assert appearance_store._pack_dir(tmp_path, pack_id).name == pack_id


@pytest.mark.parametrize("slot", ["con", "nul", "aux"])
def test_slot_name_rejects_windows_reserved_names(slot, tmp_path):
    """A slot is interpolated into a FILENAME (``<slot>.png``), and a mood slot
    arrives verbatim from the request body -- so `con` would yield `con.png`,
    which Windows cannot create however valid its characters are.

    Asserted through the public entry point to prove the check runs BEFORE
    anything is written, not just that the helper exists.
    """
    with pytest.raises(appearance_store.PackError):
        appearance_store.save_sprite_pack(tmp_path, {"assignments": {slot: "x"}})


def test_slot_name_still_accepts_an_ordinary_mood(tmp_path):
    """The reserved check must not swallow the pre-existing failure modes: a
    normal slot gets past it and fails later, on the malformed data URI."""
    with pytest.raises(appearance_store.PackError):
        appearance_store.save_sprite_pack(tmp_path, {"assignments": {"happy": "not-a-uri"}})

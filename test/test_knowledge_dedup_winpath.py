"""Windows-backslash filename handling in knowledge dedup (knowledge/dedup.py).

On a Windows ``local_folder`` source, ``folder_watcher`` stores each row's
``file_path`` as ``os.path.join(dirpath, fname)`` from ``os.walk(uri)``, which is
a BACKSLASH-separated OS path (e.g. ``C:\\Users\\me\\Vault\\Work\\notes.md``).
``normalize_filename`` and ``_file_ext`` take the basename with
``rsplit("/", 1)[-1]``, which does nothing to a backslash path, so the folder
prefix survives into the normalized stem. Two genuine copies of one note in
differently-named folders then normalise to ``work\\meeting notes`` vs
``archive\\meeting notes``; the ``SequenceMatcher`` ratio falls below the 0.9
floor and ``filename_near_match`` fails to collapse them, while POSIX collapses
them correctly.

Pure string logic -- the backslash ``file_path`` is exactly what the Windows
walk persists, so no OS simulation is needed. The POSIX controls prove the same
inputs behave identically once the separator is normalised.
"""

from __future__ import annotations

from kiro_crew.knowledge.dedup import (
    _file_ext,
    filename_near_match,
    normalize_filename,
)


def test_posix_control_same_note_two_folders_collapses():
    # Same note in two differently-named POSIX folders -> same basename stem.
    a = "Work/meeting-notes.md"
    b = "Archive/meeting-notes.md"
    assert normalize_filename(a) == "meeting notes"
    assert normalize_filename(b) == "meeting notes"
    assert filename_near_match(a, b) is True


def test_windows_same_note_two_folders_must_collapse():
    # The SAME two notes as stored by folder_watcher on a Windows local_folder
    # source (backslash file_path): one document copied into two folders, so
    # dedup must collapse them exactly as it does on POSIX.
    a = r"Work\meeting-notes.md"
    b = r"Archive\meeting-notes.md"
    assert normalize_filename(a) == "meeting notes"
    assert normalize_filename(b) == "meeting notes"
    assert filename_near_match(a, b) is True, (
        "Windows duplicate not collapsed -- folder prefix left in stem: "
        f"norm_a={normalize_filename(a)!r} norm_b={normalize_filename(b)!r}"
    )


def test_windows_absolute_path_strips_to_basename():
    # A full absolute Windows path reduces to the bare filename stem.
    assert normalize_filename(r"C:\Users\me\Vault\Work\meeting-notes.md") == "meeting notes"


def test_file_ext_handles_windows_backslash_path():
    # _file_ext must read the extension off the basename, not a backslash path.
    assert _file_ext(r"Work\meeting-notes.md") == "md"
    assert _file_ext(r"C:\Users\me\Vault\Archive\report.PDF") == "pdf"
    # POSIX control.
    assert _file_ext("Archive/report.pdf") == "pdf"


def test_windows_shared_folder_date_does_not_force_collapse():
    # Two DIFFERENT-period reports of one series, each in a folder named for a
    # shared ingestion date, stored as Windows backslash paths. Their stems both
    # reduce to "report", so once the separator is normalised they clear the stem
    # gate -- but their in-FILENAME dates (Apr 2026 vs Dec 2025) are months apart,
    # so the date gate must still BLOCK the collapse. The date gate must read the
    # filename's date, never the shared folder-name date: otherwise _collapse_doc
    # would silently delete one report's indexed text.
    a = r"C:\Finance\2026-01-01\Report Apr 2026.md"
    b = r"D:\Archive\2026-01-01\Report Dec25.md"
    assert normalize_filename(a) == "report"
    assert normalize_filename(b) == "report"
    assert filename_near_match(a, b) is False, (
        "Windows shared folder date wrongly forced a collapse of two distinct "
        "report periods -- folder date leaked into the date gate"
    )
    # POSIX control: same shape, same expectation.
    assert (
        filename_near_match(
            "Finance/2026-01-01/Report Apr 2026.md",
            "Archive/2026-01-01/Report Dec25.md",
        )
        is False
    )

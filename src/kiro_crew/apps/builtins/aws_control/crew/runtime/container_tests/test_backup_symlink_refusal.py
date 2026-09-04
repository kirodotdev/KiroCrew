"""Backup uploads real files inside its root, and nothing a link points at.

This is the WRITE direction of a defect class this branch already fixed twice on
the read side, and the write direction is worse: a link the backup follows sends
whatever it points at to the owner's bucket, where it persists.

The agent writes into this tree -- that is what the artifacts directory is for --
and the container's backend auto-approves every tool, because an unattended crew
cannot stall on an approval nobody will see. So a planted symlink is a reachable
input, not a hypothetical one. A link named like an artifact and pointing at
``/proc/self/environ`` would upload the task's own credentials.

``os.walk(followlinks=False)`` is not enough on its own, which is the trap: it
refuses to DESCEND through a directory symlink, but it still lists a symlinked
directory in ``dirnames`` and a symlinked FILE in ``filenames``, and reading one
follows it.
"""

from __future__ import annotations

import os
from pathlib import Path

from container.backup import layout

from .test_backup_restore import make_settings


def _files(settings) -> set[str]:
    return {rel for _fp, rel, _r in layout.iter_backup_files(settings)}


def test_a_symlinked_file_is_not_backed_up(tmp_path):
    settings = make_settings(tmp_path)
    secret = tmp_path / "outside" / "environ"
    secret.parent.mkdir(parents=True)
    secret.write_text("AWS_SECRET_ACCESS_KEY=would-be-uploaded\n", encoding="utf-8")

    real = settings.sessions_dir / "dashboard_c1.jsonl"
    real.write_text('{"role":"user"}\n', encoding="utf-8")
    (settings.sessions_dir / "planted.jsonl").symlink_to(secret)

    rels = _files(settings)

    assert any(r.endswith("dashboard_c1.jsonl") for r in rels), "the real file was skipped"
    assert not any("planted" in r for r in rels), f"a symlink was selected for upload: {rels}"


def test_a_symlinked_directory_is_not_descended(tmp_path):
    """The other half: os.walk lists it even though it will not descend."""
    settings = make_settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text("secret\n", encoding="utf-8")
    (settings.sessions_dir / "linkdir").symlink_to(outside, target_is_directory=True)

    rels = _files(settings)

    assert not any("secret" in r for r in rels), f"walked into a linked directory: {rels}"


def test_a_hardlink_style_escape_is_refused(tmp_path):
    """A relative link that climbs out resolves outside the root and is refused."""
    settings = make_settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "creds"
    target.write_text("secret\n", encoding="utf-8")
    rel = os.path.relpath(target, settings.sessions_dir)
    (settings.sessions_dir / "climb.jsonl").symlink_to(rel)

    rels = _files(settings)

    assert not any("climb" in r for r in rels), f"a climbing link was selected: {rels}"


def test_an_alias_to_a_file_inside_the_root_is_still_skipped(tmp_path):
    """The case ONLY the per-file symlink check catches.

    Found by mutation: removing the ``islink`` half left every other test in this
    file green, because their planted links point OUTSIDE the root and the
    containment check refuses those on its own. The two guards overlap, and this is
    the gap between them -- a link to a real file INSIDE the root passes
    containment, and without the islink check the same bytes would be uploaded
    twice under two different keys, so a later restore would depend on which key it
    happened to read.
    """
    settings = make_settings(tmp_path)
    real = settings.sessions_dir / "dashboard_c1.jsonl"
    real.write_text('{"role":"user"}\n', encoding="utf-8")
    (settings.sessions_dir / "alias.jsonl").symlink_to(real)

    rels = _files(settings)

    assert any(r.endswith("dashboard_c1.jsonl") for r in rels), "the real file was skipped"
    assert not any(
        "alias" in r for r in rels
    ), f"an in-root alias was selected, so one conversation uploads twice: {rels}"


def test_real_files_are_still_backed_up(tmp_path):
    """The guard must not have turned the backup into a no-op."""
    settings = make_settings(tmp_path)
    for name in ("a.jsonl", "b.jsonl"):
        (settings.sessions_dir / name).write_text("{}\n", encoding="utf-8")
    nested = settings.sessions_dir / "sub"
    nested.mkdir()
    (nested / "c.jsonl").write_text("{}\n", encoding="utf-8")

    rels = _files(settings)

    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        assert any(r.endswith(name) for r in rels), f"{name} was not selected"


def test_MUTATION_the_symlink_guard(tmp_path):
    """With the guard gone the planted link is selected again.

    Asserted by reading the source rather than by re-importing a mutated module:
    this walk is a generator in a package the container imports by path, and the
    sibling mutation helper only knows how to rebuild ``packaging.build``.
    """
    src = Path(layout.__file__).read_text(encoding="utf-8")
    assert "followlinks=False" in src, "the walk no longer refuses to descend links"
    assert "os.path.islink(fp)" in src, "the per-file symlink check is gone"
    assert "real_root" in src, "the containment check on the resolved target is gone"

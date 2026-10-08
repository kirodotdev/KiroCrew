"""A cron child cannot read any app's ``.app_secret``, and still runs its own bundle.

``<config_dir>/apps/<app>/.app_secret`` is a bearer credential: whoever reads it can act
as that app against the Gateway. Both cron exec paths mask the whole apps tree, so every
app's secret is covered, including an app installed while the child runs. The bundle a
cron runs from comes back as a read-write private window (its code and sibling modules
import, its ``data/`` stays writable) with that app's secret masked inside, and listed
as a required mask so a secret moved aside before mask time refuses the spawn.

PER-WINDOW scan: before a bundle's window opens, that bundle tree is scanned for a hard
link to any app's ``.app_secret`` inode. The cron's OWN bundle holding one refuses only
that app's cron; a command-named bundle holding one withholds only that window. One
app's stray link never withholds every window.

Two layers are pinned:

* ``cron_apps_mask`` -- which paths each kind of cron asks the sandbox to hide and
  re-expose, including both spellings of a symlinked home and the per-window refusal.
  These are pure planner tests and run on every host.
* REAL cases a-f -- each actually launches a cron child under the real OS sandbox
  backend (Linux namespace / macOS Seatbelt, no mocks) and asserts the read is denied
  or the import works. They SKIP cleanly, with the reason, where no backend is
  available (a nested sandbox, Windows, or a CI runner without unprivileged user
  namespaces); the macOS CI job runs them under real Seatbelt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import cron_script
from kiro_crew.cron_script import (
    CronAppsMask,
    McpServerUnreachableError,
    _app_from_launch_spec,
    _path_within,
    _safe_cron_cwd,
    _unreachable_app_server,
    cron_apps_mask,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no cron sandbox backend on Windows; the launcher and its paths are POSIX-only",
)

OWN = "own-app"
OTHER = "other-app"


def _make_app(apps: Path, name: str) -> Path:
    app = apps / name
    (app / "lib").mkdir(parents=True)
    (app / "data").mkdir()
    (app / ".app_secret").write_text(f"secret-of-{name}\n")
    (app / "job.py").write_text("def run(ctx):\n    pass\n")
    (app / "backend").mkdir()
    (app / "backend" / "sibling.mjs").write_text("export const two = 2;\n")
    # server.mjs imports its sibling, so a real `node` run of it exercises an ESM
    # sibling import resolving inside the bundle's read-only window (case a).
    (app / "backend" / "server.mjs").write_text(
        "import { two } from './sibling.mjs';\n" "process.stdout.write('NODE_OK:' + two + '\\n');\n"
    )
    return app


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> Path:
    """A crew home with two installed apps, reached through a plain (unlinked) $HOME."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.fixture()
def linked_crew_home(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The same layout behind a symlinked $HOME (``/home -> /local/home``)."""
    real_home = tmp_path / "local" / "home"
    crew = real_home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    link_home = tmp_path / "home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link_home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew.resolve()))
    return crew.resolve(), link_home / ".kiro" / "crew"


def _under(parent: str, child: str) -> bool:
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def _readable(path: str, mask: CronAppsMask) -> bool:
    """Whether *path* is readable under *mask*, by the rules both backends implement.

    Masked when some hidden entry covers it and no window between that entry and the
    path re-exposes it; a hidden entry nested INSIDE a window masks again.
    """
    best_hidden = max((h for h in mask.hidden if _under(h, path)), key=len, default=None)
    if best_hidden is None:
        return True
    best_window = max((w for w in mask.windows if _under(w, path)), key=len, default=None)
    return best_window is not None and len(best_window) > len(best_hidden)


def _writable(path: str, mask: CronAppsMask) -> bool:
    # Every window here is read-write (data/ and the bundle code alike), so writable
    # tracks readable: a path reachable inside a window is writable.
    return _readable(path, mask)


class TestCronAppsMask:
    def test_own_bundle_script_reads_its_bundle_and_no_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        other = crew_home / "apps" / OTHER

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not mask.refusal
        assert _readable(str(own / "job.py"), mask)
        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert _readable(str(own / "lib"), mask)
        assert _writable(str(own / "data" / "state.json"), mask), "data/ stays writable"
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(other / ".app_secret"), mask)
        assert not _readable(str(other / "backend" / "server.mjs"), mask)
        assert not _readable(str(other / "data"), mask)

    def test_own_secret_is_hidden_and_required(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert str(own / ".app_secret") in mask.hidden
        assert str(own / ".app_secret") in mask.required
        assert str(crew_home / "apps" / OTHER / ".app_secret") not in mask.required

    def test_unrelated_cron_sees_no_app(self, crew_home):
        script = crew_home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    pass\n")

        mask = cron_apps_mask(script_file=str(script))

        assert mask.windows == ()
        assert not mask.refusal
        for name in (OWN, OTHER):
            assert not _readable(str(crew_home / "apps" / name / ".app_secret"), mask)
            assert not _readable(str(crew_home / "apps" / name / "job.py"), mask)
        # An app installed after the spawn lands under the masked tree too.
        assert not _readable(str(crew_home / "apps" / "installed-later" / ".app_secret"), mask)

    def test_host_stamped_owner_gets_its_bundle(self, crew_home):
        # A builtin app's script lives in the package, so only ``created_by`` says
        # which installed tree is its own.
        mask = cron_apps_mask(owner_app=OWN)

        assert _readable(str(crew_home / "apps" / OWN / "backend" / "server.mjs"), mask)
        assert _writable(str(crew_home / "apps" / OWN / "data" / "x"), mask)
        assert not _readable(str(crew_home / "apps" / OWN / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    @pytest.mark.parametrize("owner", ["", "..", "../other-app", ".own-app-secret-tmp", "x/y"])
    def test_an_unsafe_owner_name_opens_nothing(self, crew_home, owner):
        assert cron_apps_mask(owner_app=owner).windows == ()

    def test_command_naming_a_bundle_reads_code_not_data_or_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(command=f"node {own / 'backend' / 'server.mjs'} --flag")

        assert not mask.refusal
        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / "data"), mask)
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    def test_command_naming_a_secret_gets_no_secret(self, crew_home):
        target = crew_home / "apps" / OTHER / ".app_secret"
        mask = cron_apps_mask(command=f"cat {target}")

        assert not _readable(str(target), mask)

    def test_absent_apps_tree_is_created_so_the_mask_has_a_target(self, tmp_path, monkeypatch):
        crew = tmp_path / "home" / ".kiro" / "crew"
        crew.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KIROCREW_HOME", str(crew))

        mask = cron_apps_mask()

        assert (crew / "apps").is_dir()
        assert str(crew / "apps") in mask.hidden

    def test_both_spellings_of_a_symlinked_home_are_masked(self, linked_crew_home):
        real, link = linked_crew_home

        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))

        for root in (real, link):
            assert str(root / "apps") in mask.hidden, f"{root} spelling of apps/ is not masked"
            assert not _readable(str(root / "apps" / OTHER / ".app_secret"), mask)
            assert not _readable(str(root / "apps" / OWN / ".app_secret"), mask)
            assert _readable(str(root / "apps" / OWN / "backend" / "server.mjs"), mask)
            assert _writable(str(root / "apps" / OWN / "data" / "x"), mask)


class TestPerWindowScan:
    def test_own_bundle_linking_another_secret_refuses_only_that_app(self, crew_home):
        own = crew_home / "apps" / OWN
        os.link(crew_home / "apps" / OTHER / ".app_secret", own / "data" / "stolen")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal, "the owning app's cron must be refused"
        assert OWN in mask.refusal and "stolen" in mask.refusal
        assert mask.windows == ()
        # The apps tree is still masked in the refusal result, so nothing leaks.
        assert str(crew_home / "apps") in mask.hidden

    def test_a_stray_link_in_one_bundle_does_not_refuse_another_apps_cron(self, crew_home):
        # OTHER's bundle holds a bad link, but OWN's cron (its own clean bundle) runs.
        other = crew_home / "apps" / OTHER
        os.link(crew_home / "apps" / OWN / ".app_secret", other / "data" / "stolen")

        mask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"))

        assert not mask.refusal
        assert mask.windows, "a clean bundle must still get its window"
        assert _readable(str(crew_home / "apps" / OWN / "job.py"), mask)

    def test_command_naming_a_linking_bundle_withholds_only_that_window(self, crew_home):
        other = crew_home / "apps" / OTHER
        os.link(crew_home / "apps" / OWN / ".app_secret", other / "data" / "stolen")

        # The command names OTHER's bundle; its window is withheld, the cron still runs.
        mask = cron_apps_mask(command=f"node {other / 'backend' / 'server.mjs'}")

        assert not mask.refusal
        assert not _readable(str(other / "backend" / "server.mjs"), mask)
        assert not _readable(str(other / "data" / "stolen"), mask)

    def test_a_second_link_to_the_bundles_own_secret_also_refuses(self, crew_home):
        # The own secret is masked at its own name, but a SECOND link to it elsewhere in
        # the bundle is not at that name, so it would ride into the window. The
        # per-window scan catches it and refuses the owning app's cron.
        own = crew_home / "apps" / OWN
        os.link(own / ".app_secret", own / "lib" / "copy")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal and "copy" in mask.refusal
        assert mask.windows == ()

    def test_an_unreadable_subdir_fails_the_scan_closed(self, crew_home):
        # os.walk silently skips a directory it cannot enter, so a link planted inside a
        # mode-000 subtree would be missed and the window opened over it. The onerror
        # recorder turns that traversal error into an unsafe scan, so the owning cron is
        # refused (the window withheld) rather than opened on a tree it could not read.
        if os.geteuid() == 0:
            pytest.skip("root traverses a mode-000 directory, so the walk never errors")
        own = crew_home / "apps" / OWN
        locked = own / "data" / "private"
        locked.mkdir()
        (locked / "whatever").write_text("x\n")
        original_mode = os.stat(locked).st_mode
        os.chmod(locked, 0o000)
        try:
            mask = cron_apps_mask(script_file=str(own / "job.py"))
            assert mask.refusal, "an unreadable subtree must refuse the owning cron"
            assert mask.windows == ()
            # The apps tree is still masked in the refusal result, so nothing leaks.
            assert str(crew_home / "apps") in mask.hidden
        finally:
            # Restore the captured mode so tmp cleanup can remove the dir; a dynamic
            # value avoids pinning an explicit octal here.
            os.chmod(locked, original_mode)

    def test_a_symlinked_secrets_referent_is_still_scanned(self, crew_home, tmp_path):
        # OTHER's .app_secret is a SYMLINK to a regular file; the credential bytes live
        # at that referent. OWN hard-links the referent into its own bundle. The scan
        # must resolve the symlink to its referent inode and catch OWN's alias, refusing
        # OWN -- a secret stored behind a symlink is still a credential to protect.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility across the referent relies on normal perms")
        other = crew_home / "apps" / OTHER
        own = crew_home / "apps" / OWN
        referent = tmp_path / "other-secret-bytes"
        referent.write_text("tok\n")
        (other / ".app_secret").unlink()
        (other / ".app_secret").symlink_to(referent)
        os.link(referent, own / "lib" / "alias")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal and "alias" in mask.refusal
        assert mask.windows == ()

    def test_a_symlinked_app_directorys_secret_is_still_scanned(self, crew_home, tmp_path):
        # OTHER is installed as a SYMLINK to a directory holding a real .app_secret.
        # Such a bundle is never windowed (it is not a plain dir), but its credential
        # bytes are real, so a hard link to them in OWN's bundle must still be caught.
        # The scan must enumerate the symlinked dir's secret and refuse OWN.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility relies on normal perms")
        own = crew_home / "apps" / OWN
        real_other = tmp_path / "other-real"
        real_other.mkdir()
        secret = real_other / ".app_secret"
        secret.write_text("tok\n")
        shutil.rmtree(crew_home / "apps" / OTHER)  # replace the real OTHER dir
        (crew_home / "apps" / OTHER).symlink_to(real_other)
        os.link(secret, own / "lib" / "alias")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal and "alias" in mask.refusal
        assert mask.windows == ()

    def test_a_mid_update_staged_secret_is_still_scanned(self, crew_home, monkeypatch):
        # While ``update_app(OTHER)`` runs, OTHER's live ``.app_secret`` is moved aside
        # to a ``.<name>-secret-tmp`` sibling before being moved back. If that move
        # lands between the root scandir and the per-candidate lstat, OTHER's live
        # secret is absent from the lstat -- but its inode is still live under the
        # staging name, and a hard link to that inode in OWN's bundle would ride into
        # OWN's window. The scan must recover the staged inode and refuse OWN, not treat
        # the absence as genuine and fail open.
        #
        # The race is an interleaving, not a filesystem end-state, so it is driven here
        # by wrapping ``os.lstat``: the first lstat of OTHER's live ``.app_secret`` fires
        # the move (secret -> staging sibling) and then raises FileNotFoundError, exactly
        # as a concurrent ``update_app`` that landed between scandir and this lstat would.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility relies on normal perms")
        apps = crew_home / "apps"
        other = apps / OTHER
        own = apps / OWN
        staging = apps / f".{OTHER}-secret-tmp"
        live_secret = str(other / ".app_secret")
        real_lstat = os.lstat
        fired = {"done": False}

        def racing_lstat(path, *a, **k):
            if not fired["done"] and os.fspath(path) == live_secret:
                # The concurrent update lands NOW: move the live secret to its staging
                # sibling (so a hard link to the inode still resolves there), then make
                # this lstat miss it the way the real race would.
                fired["done"] = True
                shutil.move(live_secret, str(staging))
                raise FileNotFoundError(live_secret)
            return real_lstat(path, *a, **k)

        monkeypatch.setattr(os, "lstat", racing_lstat)
        # OWN holds a hard link to OTHER's credential inode (placed before the move;
        # the link survives the rename, still pointing at the same inode).
        os.link(live_secret, own / "lib" / "alias")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert fired["done"], "the race on OTHER's live .app_secret must have fired"
        assert (
            mask.refusal and "alias" in mask.refusal
        ), "a hard link to a mid-update staged secret must still refuse the owning cron"
        assert mask.windows == ()

    def test_a_secret_restored_between_lookups_is_still_scanned(self, crew_home, monkeypatch):
        # GPT F1: ``update_app(OTHER)`` moves the secret live -> staging -> live. If the
        # live lstat misses because the secret is at the staging name, the recovery
        # looks up the staging sibling -- but if the secret is moved BACK to its live
        # name in the window between those two lookups, the staging lstat ALSO misses.
        # Treating that double-miss as genuine absence would drop OTHER's inode while
        # reporting the set complete, so a hard link to it in OWN's bundle would ride
        # into OWN's window (fail OPEN). The recovery must recheck the live name, find
        # the restored inode, and refuse OWN.
        #
        # Driven by wrapping ``os.lstat``: the first live lstat moves the secret to the
        # staging sibling then misses; the subsequent staging lstat moves it BACK to the
        # live name then misses too -- exactly the restore-between-lookups interleaving.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility relies on normal perms")
        apps = crew_home / "apps"
        other = apps / OTHER
        own = apps / OWN
        staging = apps / f".{OTHER}-secret-tmp"
        live_secret = str(other / ".app_secret")
        staging_path = str(staging)
        real_lstat = os.lstat
        fired = {"live": False, "staging": False}

        def racing_lstat(path, *a, **k):
            p = os.fspath(path)
            if not fired["live"] and p == live_secret:
                # First the secret is at the staging name (update in flight): this live
                # lstat misses it.
                fired["live"] = True
                shutil.move(live_secret, staging_path)
                raise FileNotFoundError(live_secret)
            if not fired["staging"] and p == staging_path:
                # ...then the update completes and moves it BACK to the live name before
                # the recovery can lstat the staging sibling, so this lstat misses too.
                fired["staging"] = True
                shutil.move(staging_path, live_secret)
                raise FileNotFoundError(staging_path)
            return real_lstat(path, *a, **k)

        monkeypatch.setattr(os, "lstat", racing_lstat)
        # OWN holds a hard link to OTHER's credential inode (survives both renames: it
        # points at the inode, not a name).
        os.link(live_secret, own / "lib" / "alias")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert fired["live"] and fired["staging"], "both lookups must have missed (the race)"
        assert (
            mask.refusal and "alias" in mask.refusal
        ), "a secret restored between the live and staging lookups must still refuse OWN"
        assert mask.windows == ()

    def test_a_retired_bundles_vanished_staging_candidate_recovers_the_live_inode(
        self, crew_home, monkeypatch
    ):
        # GPT F1 (mirror case): ``update_app(OTHER)`` RETIRES OTHER's bundle directory
        # (``os.replace(dest, retired)``) while it swaps the tree, so ``scandir`` sees
        # OTHER's dir GONE and only the top-level ``.OTHER-secret-tmp`` staging file --
        # OTHER's live ``.app_secret`` was never a candidate. If that staging file then
        # moves BACK to the live name before the lstat loop reaches it, the vanished
        # staging candidate must NOT be treated as genuine absence: OTHER's inode is live
        # under ``OTHER/.app_secret`` again, and OWN's hard link to it would ride into
        # OWN's window. The scan must recover the inode from the live name and refuse OWN.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility relies on normal perms")
        apps = crew_home / "apps"
        other = apps / OTHER
        own = apps / OWN
        staging = apps / f".{OTHER}-secret-tmp"
        live_secret = str(other / ".app_secret")
        staging_path = str(staging)

        # OWN holds a hard link to OTHER's credential inode BEFORE the update begins; the
        # link survives every rename because it points at the inode, not a name.
        os.link(live_secret, own / "lib" / "alias")

        # Simulate mid-update: OTHER's secret is at the staging name and OTHER's bundle
        # directory is retired (renamed away), so scandir will not list OTHER's dir --
        # only the top-level ``.OTHER-secret-tmp`` entry.
        shutil.move(live_secret, staging_path)
        retired = apps / f".{OTHER}-retired"
        os.rename(str(other), str(retired))

        real_lstat = os.lstat
        fired = {"staging": False}

        def racing_lstat(path, *a, **k):
            if not fired["staging"] and os.fspath(path) == staging_path:
                # The update completes NOW: OTHER's dir comes back and the secret moves
                # back to its live name, so this staging lstat misses.
                fired["staging"] = True
                os.rename(str(retired), str(other))
                shutil.move(staging_path, live_secret)
                raise FileNotFoundError(staging_path)
            return real_lstat(path, *a, **k)

        monkeypatch.setattr(os, "lstat", racing_lstat)

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert fired["staging"], "the staging candidate must have vanished (the race)"
        assert (
            mask.refusal and "alias" in mask.refusal
        ), "a retired bundle's restored secret must still refuse OWN's aliasing cron"
        assert mask.windows == ()

    def test_a_symlinked_staging_secret_is_still_scanned(self, crew_home):
        # Family audit: ``update_app`` moves ``.app_secret`` aside with ``shutil.move``,
        # so when the live secret was a SYMLINK the staging ``.<name>-secret-tmp`` entry
        # is a symlink to the real credential file. The candidate-build loop must not drop
        # a non-dir symlink: a ``-secret-tmp`` symlink is a staging secret whose referent
        # is a live inode a hard link could alias, so it must become a candidate and the
        # scan must record the referent inode and refuse OWN's aliasing cron.
        if os.geteuid() == 0:
            pytest.skip("hard-link visibility relies on normal perms")
        apps = crew_home / "apps"
        other = apps / OTHER
        own = apps / OWN
        # OTHER's real credential bytes live at a referent; the live .app_secret is a
        # symlink to it (a supported shape -- validate_app_secret reads through it).
        referent = other / ".app_secret.real"
        referent.write_text("secret-of-other-referent\n")
        (other / ".app_secret").unlink()
        (other / ".app_secret").symlink_to(referent)
        # Mid-update: the symlinked secret is moved aside to the staging name (the symlink
        # itself relocates, still pointing at the referent), and OTHER's dir retired so
        # scandir sees only the top-level staging symlink.
        staging = apps / f".{OTHER}-secret-tmp"
        shutil.move(str(other / ".app_secret"), str(staging))
        # OWN holds a hard link to OTHER's real credential inode (the referent) BEFORE the
        # referent moves with the retired dir.
        os.link(str(referent), own / "lib" / "alias")
        # Retire OTHER's bundle dir so it is NOT a scandir candidate: the ONLY path to the
        # credential inode is now the top-level staging symlink in the candidate-build
        # loop. (The referent rode into the retired dir, but OWN's hard link still points
        # at that inode, and the staging symlink still resolves to it.)
        retired = apps / f".{OTHER}-update-old"
        os.rename(str(other), str(retired))
        # Re-point the staging symlink at the referent's new location inside the retired
        # dir so it still resolves (mirrors update_app keeping the referent reachable).
        staging.unlink()
        staging.symlink_to(retired / ".app_secret.real")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert (
            mask.refusal and "alias" in mask.refusal
        ), "a symlinked staging secret's referent inode must still refuse OWN"
        assert mask.windows == ()

    def test_an_unknown_non_dir_symlink_fails_the_scan_closed(self, crew_home):
        # Family audit: a top-level symlink that resolves to a non-dir and is NOT a
        # ``-secret-tmp`` staging name is an unknown shape we cannot classify as safe.
        # It must fail the scan CLOSED (refuse OWN) rather than being silently dropped.
        if os.geteuid() == 0:
            pytest.skip("root bypasses the perms this relies on")
        apps = crew_home / "apps"
        own = apps / OWN
        target = apps / "a-regular-file"
        target.write_text("not a bundle\n")
        (apps / ".mystery-link").symlink_to(target)  # non-dir symlink, not a staging name

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal, "an unclassifiable non-dir symlink must fail the scan closed"
        assert mask.windows == ()

    def test_a_genuinely_absent_secret_with_no_staging_still_windows(self, crew_home):
        # An app that simply ships no ``.app_secret`` and has NO ``.<name>-secret-tmp``
        # sibling is genuine absence, not a mid-update. It must not be mistaken for a
        # dropped inode: OWN's clean bundle still gets its window.
        other = crew_home / "apps" / OTHER
        (other / ".app_secret").unlink()  # genuinely absent, no staging sibling

        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not mask.refusal
        assert mask.windows, "a clean bundle must still get its window on genuine absence"
        assert _readable(str(own / "job.py"), mask)

    def test_an_unreadable_app_dir_fails_the_scan_closed(self, crew_home):
        # If another app's directory cannot be read, its secret inode is missing from
        # the scan's set and a hard link to it would pass the scan fail-OPEN. The scan
        # must fail CLOSED instead: the owning cron is refused rather than opening a
        # window over a set that could not be fully built.
        if os.geteuid() == 0:
            pytest.skip("root bypasses the directory read permission this relies on")
        other = crew_home / "apps" / OTHER
        own = crew_home / "apps" / OWN
        original_mode = os.stat(other).st_mode
        os.chmod(other, 0o000)
        try:
            mask = cron_apps_mask(script_file=str(own / "job.py"))
        finally:
            os.chmod(other, original_mode)

        assert mask.refusal, "an unreadable app dir must fail the scan closed"
        assert mask.windows == ()


class TestSafeCronCwd:
    """GPT F2: a cron child must never inherit a working directory inside the apps tree.

    The apps-tree mask hides every ``.app_secret`` by ABSOLUTE path. A child whose cwd
    is under an app could read a sibling app's secret through a RELATIVE path
    (``cat other-app/.app_secret``) that never hits the mask, so the spawn pins a cwd
    that cannot resolve into the tree.
    """

    def test_a_cwd_outside_the_apps_tree_is_pinned_as_is(self, crew_home, monkeypatch):
        apps_root = os.path.realpath(str(crew_home / "apps"))
        outside = os.path.realpath(str(crew_home))  # the crew home, parent of apps/
        monkeypatch.chdir(outside)

        cwd = _safe_cron_cwd(apps_root)

        assert cwd is not None
        assert os.path.realpath(cwd) == outside
        assert not _path_within(cwd, apps_root)

    def test_a_cwd_inside_the_apps_tree_is_replaced_with_a_neutral_dir(
        self, crew_home, monkeypatch
    ):
        apps_root = os.path.realpath(str(crew_home / "apps"))
        inside = crew_home / "apps" / OWN
        monkeypatch.chdir(inside)

        cwd = _safe_cron_cwd(apps_root)

        # Never an apps-tree cwd: a relative read from the returned dir reaches no app.
        assert cwd is not None, "a neutral dir outside the tree must be available"
        assert not _path_within(cwd, apps_root), "the replacement cwd must be outside the tree"

    def test_refuses_when_no_safe_dir_can_be_established(self, crew_home, monkeypatch):
        # If the inherited cwd is inside the tree AND the neutral fallback is itself
        # inside the tree (pathological), there is no safe cwd -> refuse (None), so the
        # caller fails the spawn closed rather than launching with an apps-tree cwd.
        apps_root = os.path.realpath(str(crew_home / "apps"))
        inside = crew_home / "apps" / OWN
        monkeypatch.chdir(inside)
        # Force the neutral fallback to resolve inside the tree.
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(crew_home / "apps" / OTHER))

        cwd = _safe_cron_cwd(apps_root)

        assert cwd is None, "no safe cwd must refuse rather than return an apps-tree dir"


class TestReadOnlyWindowReachesBothBackends:
    """The read-only bundle window must reach the Linux launcher AND the Seatbelt profile.

    A read-only request that only the Seatbelt plan honoured would leave the Linux
    namespace launcher binding the bundle read-WRITE, so a command naming another app's
    bundle could rewrite its code. Both renderers must carry the restriction.
    """

    def test_linux_launcher_seals_the_bundle_read_only(self, crew_home):
        from kiro_crew import sandbox

        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        assert str(own) in mask.readonly

        script = sandbox._build_launcher_script(
            "cc",
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_private_dir_ids=mask.window_ids,
            extra_readonly_private_dirs=mask.readonly,
        )
        match = re.search(r'"private_readonly_windows":\s*(\[[^\]]*\])', script)
        assert match, "the launcher plan carries no private_readonly_windows"
        readonly = json.loads(match.group(1))
        assert str(own) in readonly, "the bundle is not sealed read-only on Linux"
        # The owned bundle's data/ is NOT read-only (it stays a read-write window).
        assert str(own / "data") not in readonly


class TestSettledBundlesOnly:
    @pytest.mark.parametrize("staging", ["data-tmp", "secret-tmp"])
    def test_an_owned_bundle_mid_update_refuses_so_writes_are_not_lost(self, crew_home, staging):
        # An update stages the owned bundle's data/ aside. Opening no window but still
        # launching would land the cron's writes in the empty apps mask and lose them,
        # so the owned bundle mid-update REFUSES rather than running windowless.
        (crew_home / "apps" / f".{OWN}-{staging}").mkdir()
        own = crew_home / "apps" / OWN

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal, "an owned bundle mid-update must refuse, not run windowless"
        assert mask.windows == ()
        assert not _readable(str(own / "job.py"), mask)

    @pytest.mark.parametrize("staging", ["data-tmp", "secret-tmp"])
    def test_a_referenced_bundle_mid_update_stays_masked_and_runs(self, crew_home, staging):
        # A command NAMES another app's bundle (read-only reference) while that app is
        # updating. No durable state of the running cron is at stake, so the referenced
        # window is withheld but the cron still runs -- only the owned case refuses.
        (crew_home / "apps" / f".{OTHER}-{staging}").mkdir()
        own = crew_home / "apps" / OWN
        other = crew_home / "apps" / OTHER

        mask = cron_apps_mask(script_file=str(own / "job.py"), command=f"cat {other}/job.py")

        assert not mask.refusal, "a referenced bundle mid-update must not refuse the cron"
        # The owned bundle still gets its window; the referenced one is withheld.
        assert any(str(own) == w for w in mask.windows)
        assert not any(str(other) == w for w in mask.windows)

    @pytest.mark.parametrize("staging", ["data-tmp", "secret-tmp"])
    def test_owner_app_mid_update_with_absent_live_dir_refuses(self, crew_home, staging):
        # During update_app the live bundle dir is momentarily REMOVED (os.replace) before
        # copytree recreates it. In that gap _is_bundle_name drops the owner, but its
        # staging markers prove it is this owner mid-update, so a cron owned via owner_app
        # must still refuse (not launch into the gap and lose its data/ write).
        import shutil as _shutil

        (crew_home / "apps" / f".{OWN}-{staging}").mkdir()
        _shutil.rmtree(crew_home / "apps" / OWN)  # the os.replace gap: live dir absent

        mask = cron_apps_mask(owner_app=OWN)

        assert mask.refusal, "owner_app mid-update must refuse even with its live dir absent"
        assert mask.windows == ()

    def test_owner_app_whose_bundle_dir_is_a_symlink_refuses(self, crew_home, tmp_path):
        # If the owner's bundle directory is itself a SYMLINK, _is_bundle_name drops it.
        # Admitting the owner by name instead lets the settled-identity check refuse it:
        # launching ownerless would land the cron's data/ write in the empty mask and
        # lose it. (The installer never makes a symlinked bundle dir, but a hand-placed
        # one must fail safe, not silently.)
        import shutil as _shutil

        own = crew_home / "apps" / OWN
        _shutil.rmtree(own)
        target = tmp_path / "real-own-bundle"
        (target / "data").mkdir(parents=True)
        own.symlink_to(target, target_is_directory=True)

        mask = cron_apps_mask(owner_app=OWN)

        assert mask.refusal and OWN in mask.refusal
        assert mask.windows == ()

    def test_each_window_is_pinned_to_the_planned_directory(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        pins = {path: (dev, ino) for path, dev, ino in mask.window_ids}
        assert mask.windows
        for window in mask.windows:
            info = os.lstat(window)
            assert pins[window] == (info.st_dev, info.st_ino)

    def test_an_owned_bundle_without_a_secret_refuses(self, crew_home):
        # An owned bundle whose window cannot open (here: no settled .app_secret) would
        # land the cron's data/ writes in the empty apps mask and lose them silently.
        # The owned spawn REFUSES (fail-safe, loud) rather than running windowless.
        own = crew_home / "apps" / OWN
        (own / ".app_secret").unlink()

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal and OWN in mask.refusal
        assert mask.windows == ()
        assert str(crew_home / "apps") in mask.hidden

    def test_a_secret_linked_outside_the_apps_tree_is_the_known_residual(self, crew_home, tmp_path):
        # A hard link to the secret at a path OUTSIDE the apps tree is a known residual:
        # the per-window scan walks only the bundle tree, so it does not see it, the
        # bundle is still settled, and its window opens with the secret masked by name.
        # The external link stays readable -- the gap the design keeps.
        own = crew_home / "apps" / OWN
        os.link(own / ".app_secret", tmp_path / "elsewhere")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not mask.refusal
        assert mask.windows, "an external link is not an in-bundle link; the window opens"
        assert not _readable(str(own / ".app_secret"), mask)

    def test_an_owned_symlinked_secret_bundle_refuses(self, crew_home, tmp_path):
        # A symlinked secret points at bytes no window pins, so the bundle is not settled
        # and its window cannot open. An owned cron there refuses (fail-safe) rather than
        # running windowless and losing its data/ writes to the empty mask.
        own = crew_home / "apps" / OWN
        (own / ".app_secret").unlink()
        (tmp_path / "real-secret").write_text("x\n")
        (own / ".app_secret").symlink_to(tmp_path / "real-secret")

        mask = cron_apps_mask(script_file=str(own / "job.py"))
        assert mask.refusal and OWN in mask.refusal
        assert mask.windows == ()


class TestMaskHelperEdges:
    def test_a_script_outside_the_apps_tree_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._app_name_under(str(crew_home / "crons" / "x.py"), real) == ""
        assert cron_script._app_name_under(real, real) == ""

    def test_an_unparsable_command_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._command_bundle_refs("echo 'unterminated", real) == []

    def test_a_relative_command_path_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._command_bundle_refs(f"node apps/{OWN}/job.py", real) == []

    def test_a_linked_bundle_is_not_a_bundle(self, crew_home, tmp_path):
        target = tmp_path / "outside-bundle"
        target.mkdir()
        (crew_home / "apps" / "linked-app").symlink_to(target, target_is_directory=True)

        assert cron_apps_mask(owner_app="linked-app").windows == ()

    def test_a_variable_spelled_command_path_names_the_bundle(self, crew_home, monkeypatch):
        own = crew_home / "apps" / OWN
        monkeypatch.setenv("APPS_ROOT_FOR_TEST", str(crew_home / "apps"))

        mask = cron_apps_mask(command=f"node $APPS_ROOT_FOR_TEST/{OWN}/backend/server.mjs")

        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / ".app_secret"), mask)

    def test_a_failed_mkdir_refuses_the_spawn(self, crew_home, monkeypatch):
        # A tree that cannot be created cannot be masked, so the spawn is refused
        # (fail-closed) rather than launched with an unenforceable mask. The apps tree
        # is still listed as hidden so nothing about it is disclosed in the refusal.
        real_mkdir = Path.mkdir

        def _refuse(self, *args, **kwargs):
            if self.name == "apps":
                raise PermissionError("read-only home")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _refuse)

        mask = cron_apps_mask()

        assert mask.refusal and "could not be created" in mask.refusal
        assert mask.windows == ()
        assert str(crew_home / "apps") in mask.hidden


class TestSeatbeltProfile:
    """The macOS Seatbelt profile the mask produces: bundle readable, secret denied.

    Pinned as profile TEXT (no backend needed), so the macOS regression -- a bundle
    window refused because it holds the masked ``.app_secret`` -- cannot come back
    without this failing. The window is read-ONLY (carved out of the read deny), the
    secret keeps its own literal deny (deny-wins inside the window), and the bundle code
    is not writable.
    """

    def test_bundle_reads_secret_denied_and_bundle_write_sealed(self, crew_home):
        from kiro_crew import sandbox

        own = crew_home / "apps" / OWN
        apps = str(crew_home / "apps")
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        assert not mask.refusal and str(own) in mask.readonly

        profile = sandbox._build_seatbelt_profile(
            "cc",
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_readonly_private_dirs=mask.readonly,
        )
        lines = profile.splitlines()
        sub = f"(subpath {json.dumps(apps)})"
        read = [ln for ln in lines if ln.startswith(f"(deny file-read* (require-all {sub}")]
        write = [ln for ln in lines if ln.startswith(f"(deny file-write* (require-all {sub}")]
        write_blanket = [ln for ln in lines if ln == f"(deny file-write* {sub})"]

        # The bundle is carved out of the apps-tree READ deny (code imports).
        assert read and f"(require-not (subpath {json.dumps(str(own))}))" in read[0]
        # The bundle is NOT carved out of the WRITE deny (read-only): either a blanket
        # write deny over apps/, or a write deny whose exceptions do not include the
        # read-only bundle.
        assert write_blanket or (
            write and f"(require-not (subpath {json.dumps(str(own))}))" not in write[0]
        )
        # The owned bundle's data/ IS writable (a nested read-write window).
        data_exc = f"(require-not (subpath {json.dumps(str(own / 'data'))}))"
        assert any(data_exc in ln for ln in write) or write_blanket
        # The secret keeps its own read deny, which deny-wins inside the window.
        secret = json.dumps(str(own / ".app_secret"))
        assert f"(deny file-read* (literal {secret}))" in lines


# --------------------------------------------------------------------------- #
# REAL sandbox cases a-f: actually launch a child under the real backend.
# --------------------------------------------------------------------------- #


def _backend() -> str:
    from kiro_crew import sandbox

    return sandbox.detect_backend(config_mode="cc")


_REAL_BACKEND = pytest.mark.skipif(
    _backend() not in ("namespace", "sandbox-exec"),
    reason=(
        f"no real OS sandbox backend here (detect_backend='{_backend()}'): a nested "
        "agent sandbox or a host/CI runner without unprivileged user namespaces. The "
        "macOS CI job runs these under real Seatbelt; the Linux namespace runner runs "
        "them where userns is permitted."
    ),
)


def _run_child(
    mask: CronAppsMask, program: str, *, mode: str = "cc"
) -> subprocess.CompletedProcess:
    """Launch a python child under the REAL sandbox with *mask* applied, running *program*.

    Mirrors what ``run_script_sandboxed`` / ``run_command_sandboxed`` build: the apps
    tree masked, the owning bundle a window, the secret masked and required inside it.
    """
    from kiro_crew import sandbox

    argv = [sys.executable, "-c", program]
    # Give the launcher a dedicated, test-owned tmpfs root for its bind-source stand-ins
    # (TMPDIR is where it mkdtemps them) and remove the whole tree afterwards, including
    # on failure, so a real-sandbox run leaves nothing behind in the shared host tmpdir.
    sandbox_tmp = tempfile.mkdtemp(
        prefix=SHORT_TMP_PREFIX + "cron-apps-mask-", dir=short_tmp_base()
    )
    cleanup = None
    try:
        wrapped, cleanup = sandbox.wrap_argv(
            argv,
            mode=mode,
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_private_dir_ids=mask.window_ids,
            extra_readonly_private_dirs=mask.readonly,
            extra_required_mask_targets=mask.required,
        )
        return subprocess.run(
            wrapped,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env={**os.environ, "TMPDIR": sandbox_tmp},
        )
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass
        shutil.rmtree(sandbox_tmp, ignore_errors=True)


def _reads(path: Path) -> str:
    # A tiny program that reports whether any bytes were disclosed from *path*.
    #
    # On the macOS Seatbelt backend a masked leaf fails to open (ENOENT/EPERM); on the
    # Linux namespace backend a masked leaf inside a window is an EMPTY bind -- it opens
    # and reads as zero bytes. Both outcomes disclose nothing, so the test must assert on
    # "no bytes disclosed" rather than on a failed open. Prints:
    #   DENIED       -- open/read raised (nothing disclosed)
    #   EMPTY        -- opened, read zero bytes (nothing disclosed)
    #   BYTES:<n>    -- opened, read n>0 bytes (DISCLOSED)
    return textwrap.dedent(f"""
        import sys
        try:
            with open({str(path)!r}, "rb") as fh:
                data = fh.read()
            sys.stdout.write("EMPTY" if not data else "BYTES:%d" % len(data))
        except OSError:
            sys.stdout.write("DENIED")
        """)


def _assert_no_bytes(stdout: str, message: str) -> None:
    # No bytes disclosed means the read was denied OR returned empty. A ``BYTES:<n>``
    # (n>0) is the only disclosure, and the only failure.
    assert stdout in ("DENIED", "EMPTY"), f"{message}: {stdout}"


@_REAL_BACKEND
class TestRealSandbox:
    """Cases a-f from the re-land plan, run under the real OS sandbox backend."""

    def test_a_app_cron_imports_a_sibling_in_its_own_bundle(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        # Run `node` on the bundle's own server.mjs, which ESM-imports its sibling.mjs.
        # This is the plan's case (a): a sibling import must resolve inside the bundle's
        # read-only window. The child shells out to node from inside the sandbox; if node
        # is not installed on the runner, it skips cleanly with the reason (the Python
        # listing below still proves the bundle code is readable in the window).
        prog = textwrap.dedent(f"""
            import sys, os, shutil, subprocess
            backend = {str(own / "backend")!r}
            names = sorted(os.listdir(backend))
            node = shutil.which("node")
            if node is None:
                sys.stdout.write("NODE_ABSENT:" + ",".join(names))
                sys.exit(0)
            proc = subprocess.run(
                [node, os.path.join(backend, "server.mjs")],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                sys.stdout.write("NODE_FAIL:" + proc.stderr.strip()[:300])
                sys.exit(0)
            sys.stdout.write(proc.stdout.strip() + "|" + ",".join(names))
            """)
        result = _run_child(mask, prog)
        assert result.returncode == 0, result.stderr
        if result.stdout.startswith("NODE_ABSENT:"):
            pytest.skip(f"node not installed on this runner; bundle listing: {result.stdout}")
        assert not result.stdout.startswith("NODE_FAIL:"), result.stdout
        # node ran server.mjs and its ESM import of sibling.mjs resolved inside the window.
        assert result.stdout.startswith("NODE_OK:2|"), result.stdout

    def test_b_script_and_command_cron_cannot_read_another_apps_secret(self, crew_home):
        other_secret = crew_home / "apps" / OTHER / ".app_secret"
        # Script cron: owns OWN's bundle, reads OTHER's secret -> denied.
        smask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"))
        sres = _run_child(smask, _reads(other_secret))
        assert sres.returncode == 0, sres.stderr
        _assert_no_bytes(sres.stdout, "script cron read another app's secret")
        # Command cron: no window, reads OTHER's secret -> denied.
        cmask = cron_apps_mask(command="true")
        cres = _run_child(cmask, _reads(other_secret))
        assert cres.returncode == 0, cres.stderr
        _assert_no_bytes(cres.stdout, "command cron read another app's secret")

    def test_c_app_cron_cannot_read_its_own_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        result = _run_child(mask, _reads(own / ".app_secret"))
        assert result.returncode == 0, result.stderr
        # A masked leaf inside the window is an empty bind on the Linux namespace backend
        # (opens, reads zero bytes) and an ENOENT/EPERM on macOS Seatbelt. Both disclose
        # nothing, so the bar is "no bytes disclosed", not "open failed".
        _assert_no_bytes(result.stdout, "app cron read its OWN secret")

    def test_d_symlinked_home_covers_both_spellings(self, linked_crew_home):
        real, link = linked_crew_home
        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))
        for root in (real, link):
            result = _run_child(mask, _reads(root / "apps" / OTHER / ".app_secret"))
            assert result.returncode == 0, result.stderr
            _assert_no_bytes(result.stdout, f"{root} spelling leaked a secret")

    def test_e_hardlink_in_one_bundle_refuses_only_that_app(self, crew_home):
        # X=OWN holds a link to Y=OTHER's secret inside its bundle. OWN's cron is refused
        # at the planner (never spawned); OTHER's cron (clean bundle) still runs and masks.
        own = crew_home / "apps" / OWN
        os.link(crew_home / "apps" / OTHER / ".app_secret", own / "lib" / "stolen")

        xmask = cron_apps_mask(script_file=str(own / "job.py"))
        assert xmask.refusal and OWN in xmask.refusal, "X's cron must be refused"

        ymask = cron_apps_mask(script_file=str(crew_home / "apps" / OTHER / "job.py"))
        assert not ymask.refusal, "Y's cron must still run"
        yres = _run_child(ymask, _reads(crew_home / "apps" / OWN / ".app_secret"))
        assert yres.returncode == 0, yres.stderr
        _assert_no_bytes(yres.stdout, "Y's cron could read another app's secret")

    def test_f_unrelated_crons_script_sees_no_app_tree(self, crew_home):
        script = crew_home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    pass\n")
        mask = cron_apps_mask(script_file=str(script))
        assert mask.windows == ()
        # Reads of either app's secret and bundle code are denied; the tree is masked.
        for name in (OWN, OTHER):
            res = _run_child(mask, _reads(crew_home / "apps" / name / ".app_secret"))
            assert res.returncode == 0, res.stderr
            _assert_no_bytes(res.stdout, f"unrelated cron read {name}'s secret")


class TestCallToolRefusesUnreachableAppServer:
    """A cron child that calls a DIFFERENT app's MCP server is refused with a named error.

    Inside a cron child every app's bundle except the cron's own window is masked to an
    empty tree, so starting another app's server would hand it a bundle that reads empty.
    The bridge refuses before the server starts, keyed on the server name's ``<app>:``
    prefix versus the cron's own app -- correct on both sandbox backends and for every
    launch shape (interpreter-first, ``-m`` module, or path), none of which name a bundle
    in ``argv[0]``.
    """

    def test_a_different_apps_server_is_reported(self):
        # own-app's cron calls other-app's server -> refused, naming other-app.
        assert _unreachable_app_server("other-app:tools", own_app="own-app") == "other-app"

    def test_the_crons_own_app_server_is_allowed(self):
        # A call into the cron's OWN app's server proceeds.
        assert _unreachable_app_server("own-app:tools", own_app="own-app") == ""

    def test_a_non_app_server_is_allowed(self):
        # A server with no <app>: prefix (the host kirocrew-cron) is never refused.
        assert _unreachable_app_server("kirocrew-cron", own_app="own-app") == ""
        assert _unreachable_app_server("", own_app="own-app") == ""

    def test_a_non_app_cron_refuses_any_app_server(self):
        # A non-app cron (own_app == "") holds no app window, so ANY <app>: server is
        # refused -- its bundle is masked in this child.
        assert _unreachable_app_server("mochi:mochi", own_app="") == "mochi"

    def test_interpreter_first_launch_still_refuses(self, monkeypatch):
        # The real app MCP launch is interpreter-first (argv[0] is python, the app is
        # named by -m or a later token), so a path-based probe would miss it. The
        # name-keyed check does not: it refuses before resolving argv at all.
        def _should_not_resolve(name):  # pragma: no cover - must not run
            raise AssertionError("resolution must not happen for a refused cross-app call")

        def _no_spawn(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("the server must not be wrapped or spawned")

        monkeypatch.setattr(cron_script, "_resolve_mcp_server", _should_not_resolve)
        monkeypatch.setattr(cron_script, "wrap_argv", _no_spawn)
        with pytest.raises(McpServerUnreachableError) as exc:
            cron_script.McpToolClient("mochi:mochi", session_key="cron:j1", own_app="other-app")
        assert "mochi" in str(exc.value)
        assert "not visible" in str(exc.value)

    def test_an_unsandboxed_preview_client_is_exempt(self, monkeypatch):
        # The in-process `kirocrew cron preview` builds McpToolClient with NO session key
        # and runs unsandboxed (no apps mask), so a cross-app call must NOT be refused --
        # it proceeds to resolution (which has no such server here -> RuntimeError), not
        # McpServerUnreachableError.
        def _no_spawn(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("the server must not be wrapped or spawned")

        monkeypatch.setattr(cron_script, "wrap_argv", _no_spawn)
        with pytest.raises(RuntimeError) as exc:
            cron_script.McpToolClient("mochi:mochi", session_key="", own_app="")
        assert "not found in agent config" in str(exc.value)
        assert not isinstance(exc.value, McpServerUnreachableError)

    def test_kept_servers_threads_own_app_into_the_refusal(self, monkeypatch):
        # KeptMcpServers must carry own_app so the refusal fires through ctx.call_tool.
        def _no_spawn(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("the server must not be wrapped or spawned")

        monkeypatch.setattr(cron_script, "wrap_argv", _no_spawn)
        kept = cron_script.KeptMcpServers(session_key="cron:x", own_app="own-app")
        with pytest.raises(McpServerUnreachableError) as exc:
            kept.call_tool("mochi:update_watchlist", "update_watchlist", {})
        assert "mochi" in str(exc.value)

    def test_script_context_resolves_own_app_from_created_by(self):
        # The GPT finding: a child job with no created_by made own_app empty, so an
        # app-owned script could not call its OWN app's tools. ScriptContext must read
        # the owning app from job.created_by (app:<name>) and thread it in: the own app's
        # server proceeds, a different app's is refused.
        import types

        job = types.SimpleNamespace(id="j1", message="", created_by="app:own-app")
        ctx = cron_script.ScriptContext(job=job)
        try:
            # Own app: NOT refused. It proceeds past the refusal to _resolve_mcp_server,
            # which has no such server in the test config -> RuntimeError "not found",
            # NOT McpServerUnreachableError. That it reaches resolution proves the own
            # app was allowed through.
            with pytest.raises(RuntimeError) as own:
                ctx.call_tool("own-app:tools", "t", {})
            assert "not found in agent config" in str(own.value)
            assert not isinstance(own.value, McpServerUnreachableError)
            # A different app: refused before resolution.
            with pytest.raises(McpServerUnreachableError):
                ctx.call_tool("other-app:tools", "t", {})
        finally:
            ctx.close()

    # --- Option (a): ownership from the resolved launch spec, not the name prefix. ---
    # An operator-written ALIAS can carry a prefix-less name while its command launches
    # another app's packaged server (`kirocrew app mcp <app>`). The name check alone let
    # that through; ownership is now read from the resolved argv and fails closed on an
    # alias whose owning app cannot be established.

    @pytest.mark.parametrize(
        "argv, expected",
        [
            # `app mcp <app>` -- the manifest / host-CLI launch shape (an app bundle).
            (("kirocrew", "app", "mcp", "mochi"), "mochi"),
            # interpreter-first, host-CLI pinned (`<python> -P -m kiro_crew app mcp mochi`).
            (("/usr/bin/python3", "-P", "-m", "kiro_crew", "app", "mcp", "mochi"), "mochi"),
            # `app mcp` with no app name -> "" (caller fails this CLOSED).
            (("kirocrew", "app", "mcp"), ""),
            # `app mcp` followed by a flag names no app -> "" (fails CLOSED).
            (("kirocrew", "app", "mcp", "--help"), ""),
            # `mcp-<name>` is the HOST builtin dispatch (kirocrew-cron / kirocrew-core),
            # not an app-bundle launch -> None (must proceed from a cron child).
            (("kirocrew", "mcp-cron"), None),
            (("/usr/bin/python3", "-P", "-m", "kiro_crew", "mcp-core"), None),
            (("/usr/bin/python3", "-m", "kiro_crew", "mcp-mochi"), None),
            (("/usr/bin/python3", "-m", "kiro_crew", "mcp-"), None),
            # common third-party server commands carry an `mcp-` token but launch no app.
            (("npx", "-y", "mcp-remote", "https://example/mcp"), None),
            (("uvx", "mcp-server-time"), None),
            (("node", "mcp-server.js"), None),
            # not an app-MCP launch at all -> None (a host / non-app server).
            (("kirocrew-cron-helper", "--serve"), None),
            (("node", "/opt/some/server.mjs"), None),
            ((), None),
            (None, None),
            # a `mcp-` path token is not a launch token either.
            (("/opt/pkg/mcp-tool/bin", "run"), None),
        ],
    )
    def test_app_from_launch_spec_reads_the_owning_app(self, argv, expected):
        assert _app_from_launch_spec(argv) == expected

    def test_host_managed_servers_are_allowed_from_a_cron_child(self):
        # Regression (Opus): the host kirocrew-cron / kirocrew-core servers launch as
        # `kirocrew mcp-cron` / `mcp-core`. They are host code, NOT an app bundle under
        # the mask, so a cron child must still reach them -- the launch-spec check must
        # not read `mcp-cron` as app "cron".
        cron_argv = ("kirocrew", "mcp-cron")
        core_argv = ("/usr/bin/python3", "-P", "-m", "kiro_crew", "mcp-core")
        assert _unreachable_app_server("kirocrew-cron", own_app="", server_argv=cron_argv) == ""
        assert _unreachable_app_server("kirocrew-core", own_app="own", server_argv=core_argv) == ""

    def test_third_party_mcp_prefixed_server_is_allowed(self):
        # Regression (Opus): an operator server whose argv carries a non-path `mcp-` token
        # (`npx -y mcp-remote ...`) launches no app bundle and must proceed.
        argv = ("npx", "-y", "mcp-remote", "https://example/mcp")
        assert _unreachable_app_server("remote-tools", own_app="own", server_argv=argv) == ""

    def test_colon_prefixed_own_app_is_allowed(self):
        # Own app, colon-prefixed: allowed on the name alone (no argv needed).
        assert _unreachable_app_server("own-app:tools", own_app="own-app") == ""

    def test_colon_prefixed_other_app_is_refused(self):
        # Different app, colon-prefixed: refused on the name alone.
        assert _unreachable_app_server("other-app:tools", own_app="own-app") == "other-app"

    def test_no_colon_alias_to_another_app_is_refused(self):
        # The finding: a prefix-less alias whose argv runs `app mcp other-app` is the
        # cross-app launch the name check missed -> refused via the resolved spec.
        argv = ("python", "-P", "-m", "kiro_crew", "app", "mcp", "other-app")
        assert _unreachable_app_server("mochi-local", own_app="own-app", server_argv=argv) == (
            "other-app"
        )

    def test_no_colon_alias_to_the_own_app_is_allowed(self):
        # A prefix-less alias that launches the cron's OWN app is fine.
        argv = ("python", "-m", "kiro_crew", "app", "mcp", "own-app")
        assert _unreachable_app_server("own-local", own_app="own-app", server_argv=argv) == ""

    def test_unknown_alias_fails_closed(self):
        # An alias whose argv is an app-MCP launch but names no app cannot be matched to
        # own_app, so its owning app cannot be established -> refused (fail closed).
        argv = ("python", "-m", "kiro_crew", "app", "mcp")
        blocked = _unreachable_app_server("weird-alias", own_app="own-app", server_argv=argv)
        assert blocked and blocked != ""

    def test_prefixless_non_app_server_still_allowed(self):
        # A prefix-less server whose argv runs no app launch (the host kirocrew-cron) is
        # not an app server and must still proceed.
        argv = ("kirocrew-cron-helper", "--serve")
        assert _unreachable_app_server("kirocrew-cron", own_app="own-app", server_argv=argv) == ""

    def test_alias_launch_is_refused_end_to_end(self, monkeypatch):
        # End-to-end through McpToolClient: a prefix-less alias resolving to another app's
        # `app mcp` launch is refused BEFORE the server is wrapped or spawned. The spec
        # must be RESOLVED here (pass 2 reads the argv), so _resolve_mcp_server runs, but
        # wrap_argv must not.
        def _resolve(name):
            assert name == "mochi-local"
            return (("python", "-m", "kiro_crew", "app", "mcp", "mochi"), {})

        def _no_spawn(*a, **k):  # pragma: no cover - must not run
            raise AssertionError("the server must not be wrapped or spawned")

        monkeypatch.setattr(cron_script, "_resolve_mcp_server", _resolve)
        monkeypatch.setattr(cron_script, "wrap_argv", _no_spawn)
        with pytest.raises(McpServerUnreachableError) as exc:
            cron_script.McpToolClient("mochi-local", session_key="cron:j1", own_app="other-app")
        assert "mochi" in str(exc.value)
        assert "not visible" in str(exc.value)

    def test_own_app_alias_launch_proceeds_to_spawn_end_to_end(self, monkeypatch):
        # An alias launching the cron's OWN app passes both ownership passes and reaches
        # wrap_argv (we stop it there to avoid a real spawn).
        def _resolve(name):
            return (("python", "-m", "kiro_crew", "app", "mcp", "own-app"), {})

        class _Stop(Exception):
            pass

        def _wrap(argv, mode="standard"):
            raise _Stop  # reached only if both ownership passes allowed the call

        monkeypatch.setattr(cron_script, "_resolve_mcp_server", _resolve)
        monkeypatch.setattr(cron_script, "wrap_argv", _wrap)
        with pytest.raises(_Stop):
            cron_script.McpToolClient("own-local", session_key="cron:j1", own_app="own-app")

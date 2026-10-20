"""Tests for how the Linux service decides which unit is ours.

The system scope is decided by the manager rather than by the unit file:
masks, links, aliases, vendor units and the managed marker each decide whether
a teardown arm may act.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from service_helpers import (  # noqa: F401 -- fixtures are found by name
    _DEAD,
    _OURS_UNIT_TEXT,
    _RUNNING,
    _UNIT,
    _clear_sudo_user,
    _fake_systemctl,
    _no_host_env_file,
    _no_host_unit_files,
    _restart_settle_without_waiting,
)

from kiro_crew.service.common import RESTART_REFUSED, Platform


class TestLinuxServiceScopesOwnership:
    """``status`` / ``is_active`` / ``uninstall`` (and the ``stop`` / ``restart``
    that ``is_active`` gates) see BOTH systemd scopes and name the one they
    report on, so a gateway running as the SELinux remedy's user unit is never
    reported as a dead system unit.

    The systemctl runner is mocked in every test: the systemd user manager is
    host state, and a real ``systemctl --user`` from here would touch the
    operator's own units.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture on the shared monkeypatch, moved verbatim; tracked for a de-flake pass
    def _not_root(self, monkeypatch, tmp_path):
        from kiro_crew.service import linux as svc_linux

        monkeypatch.setattr(svc_linux.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setenv("USER", "tester")
        # No system unit file unless a test writes one: uninstall's system-scope
        # probe is a stat of this path. The per-user unit's own path is the
        # remedy's layout under tmp_path, so a test that writes there owns the
        # file by path and one that writes elsewhere owns it by the marker.
        monkeypatch.setattr(svc_linux, "UNIT_PATH", tmp_path / "absent" / _UNIT)
        monkeypatch.setattr(
            svc_linux,
            "user_unit_file_path",
            lambda: tmp_path / ".config" / "systemd" / "user" / _UNIT,
        )

    @staticmethod
    def _user_calls(run):
        return [c for c in run.calls if "--user" in c]

    # -- the system scope is decided by the manager, not by the unit file ----

    def test_uninstall_asks_the_system_manager_when_the_unit_file_is_absent(self):
        """No file at `/etc/systemd/system/kirocrew.service` is not the manager's
        word: the system scope is still asked with an unprivileged `show` (no
        password prompt) before it is called `not installed`."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=None)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        system_shows = [c for c in run.calls if c[:2] == ["systemctl", "show"]]
        assert system_shows and all(_UNIT in c for c in system_shows), run.calls
        assert all("sudo" not in c for c in run.calls), run.calls
        assert report.system == "not installed" and report.user == "not installed"
        assert not any(c[-2:-1] in (["stop"], ["disable"]) or "rm" in c for c in run.calls)

    def test_a_running_system_unit_whose_file_was_deleted_is_stopped_not_reported_not_installed(
        self, capsys
    ):
        """`rm /etc/systemd/system/kirocrew.service` with no `daemon-reload`: the
        manager still has the unit LOADED and the gateway keeps running. The old
        stat-only probe read `not installed` and walked past it. Now the unit is
        stopped (checked) and the `daemon-reload` makes it `not-found`; there is no
        file for `disable` to read an `[Install]` section from, so that verb is not
        issued and nothing is `rm`'d."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None)
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert report.system != "not installed", report.system
        assert ["sudo", "systemctl", "stop", _UNIT] in run.calls, run.calls
        assert not any("disable" in c or "rm" in c for c in run.calls), run.calls
        assert any(c[-1:] == ["daemon-reload"] and "--user" not in c for c in run.calls), run.calls
        assert report.system.startswith("stopped (its unit file ")
        assert "already gone" in report.system and "daemon-reload run" in report.system
        assert report.unfinished == frozenset()
        # The unit IS gone from the manager: the headline must say so. Keyed on
        # the report's structure — a prefix test on the line read this scope as
        # nothing removed and printed "No kirocrew service was removed."
        assert report.removed == frozenset({"system"}) and report.removed_any
        assert rc == 0
        out = capsys.readouterr().out
        assert "system scope: stopped (its unit file " in out
        assert "✅ kirocrew service stopped and removed." in out
        assert "No kirocrew service was removed" not in out

    def test_a_running_system_unit_left_not_found_by_a_daemon_reload_is_refused_not_not_installed(
        self, capsys
    ):
        """The same deletion followed by a `daemon-reload`: `show` answers
        `LoadState=not-found ActiveState=active` while the process runs on. That
        is the refusal the user scope already gives, not `not installed`: nothing
        is stopped, disabled or removed on this module's account, the operator is
        told the step, and the controller exits 1."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_RUNNING, user=None, system_load="not-found")
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert report.system == (
            "left in place (an active unit whose load state is not-found: stop it first, "
            "then run `kirocrew service uninstall` again)"
        )
        assert report.unfinished == frozenset({"system"})
        assert not any("stop" in c or "disable" in c or "rm" in c for c in run.calls), run.calls
        assert rc == 1
        assert "⚠️ system scope: left in place (an active unit whose load state is not-found" in (
            capsys.readouterr().out
        )

    def test_a_runtime_masked_system_unit_with_no_file_is_reported_not_claimed_removed(self):
        """`systemctl mask --runtime` puts the mask under /run, so nothing sits at
        the path this module owns while the manager answers `masked`. Not running,
        so no verb is issued and nothing is unlinked — the line says what the
        manager holds rather than `not installed` or `removed`."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_DEAD, user=None, system_load="masked")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert (
            report.system == f"left in place (load state masked, unit file {svc_linux.UNIT_PATH})"
        )
        assert report.unfinished == frozenset()
        assert not any(
            "stop" in c or "disable" in c or "rm" in c or "daemon-reload" in c for c in run.calls
        ), run.calls

    @pytest.mark.skipif(os.name != "posix", reason="a mask is a symlink to /dev/null")
    @pytest.mark.parametrize(
        "reported_fragment", ["own_path", os.devnull], ids=["name-map-loader", "open-follow-loader"]
    )
    def test_a_persistent_mask_of_our_name_is_removed_in_the_system_scope(
        self, tmp_path, monkeypatch, reported_fragment
    ):
        """`systemctl mask kirocrew.service` writes `/etc/systemd/system/kirocrew.service`
        → /dev/null: a mask of OUR name at exactly the path the installer writes to.
        systemd answers `LoadState=masked`, not running, with `FragmentPath` either
        the mask entry itself (the name-map loader, v246+) or `/dev/null` (the
        older `open_follow()` loader) — the resolver reads the entry at our path
        either way. There is no definition to read a marker from, so the old
        resolver followed the link to /dev/null, found no marker and left the
        mask in place while the spec said removed. Ours by name: the entry is
        unlinked under sudo and the manager reloaded — the name is unmasked —
        with no `stop` or `disable`, since nothing is loaded."""
        from kiro_crew.service import linux as svc_linux

        mask = tmp_path / "etc-systemd-system" / _UNIT
        mask.parent.mkdir(parents=True)
        os.symlink(os.devnull, mask)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", mask)
        fragment = str(mask) if reported_fragment == "own_path" else reported_fragment
        run = _fake_systemctl(
            system=_DEAD, user=None, system_load="masked", system_fragment=fragment
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system == f"removed (the mask {mask}; {_UNIT} is unmasked)", report.system
        assert report.removed == frozenset({"system"})
        assert report.unfinished == frozenset()
        assert ["sudo", "rm", "-f", str(mask)] in run.calls, run.calls
        assert ["sudo", "systemctl", "daemon-reload"] in run.calls, run.calls
        assert not any("stop" in c or "disable" in c for c in run.calls), run.calls
        # The fake's `rm` removes nothing: the entry is still a link to /dev/null
        # and /dev/null itself was never a target of anything but readlink.
        assert os.path.realpath(mask) == os.path.realpath(os.devnull)

    @pytest.mark.skipif(os.name != "posix", reason="a mask is a symlink to /dev/null")
    def test_a_persistent_mask_of_our_name_is_removed_in_the_user_scope(self, tmp_path):
        """`systemctl --user mask kirocrew.service` writes the /dev/null symlink at
        `~/.config/systemd/user/kirocrew.service` — the per-user path the SELinux
        remedy installs to — so the user scope reads the same rule: the mask is
        unlinked as the calling user, `daemon-reload --user` runs, nothing is
        stopped, disabled or run under sudo, and the system scope is untouched."""
        from kiro_crew.service import linux as svc_linux

        mask = svc_linux.user_unit_file_path()
        mask.parent.mkdir(parents=True)
        os.symlink(os.devnull, mask)
        run = _fake_systemctl(system=None, user=_DEAD, user_fragment=str(mask), user_load="masked")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.user == f"removed (the mask {mask}; {_UNIT} is unmasked)", report.user
        assert not mask.is_symlink() and not mask.exists(), "the mask entry was left in place"
        assert report.removed == frozenset({"user"})
        assert report.unfinished == frozenset()
        assert ["systemctl", "--user", "daemon-reload"] in run.calls, run.calls
        assert not any("stop" in c or "disable" in c or "sudo" in c for c in run.calls), run.calls
        assert report.system == "not installed"

    @pytest.mark.skipif(os.name != "posix", reason="symlink layout")
    def test_a_link_at_our_path_that_does_not_resolve_to_dev_null_is_not_a_mask(
        self, tmp_path, monkeypatch
    ):
        """The mask rule is exact: the entry at the installer's path must be a
        symlink resolving to /dev/null. A symlink there to an operator's unmarked
        file whose unit the manager does not have loaded (`bad-setting` after a
        bad edit), or one left dangling, is neither a mask nor ours — left in
        place as before, nothing unlinked, no verb."""
        from kiro_crew.service import linux as svc_linux

        theirs = tmp_path / "srv" / _UNIT
        theirs.parent.mkdir(parents=True)
        theirs.write_text(
            "[Unit]\nDescription=theirs\n[Service]\nExecStart=/bin/true\n", encoding="utf-8"
        )
        link = tmp_path / "etc-systemd-system" / _UNIT
        link.parent.mkdir(parents=True)
        os.symlink(theirs, link)
        monkeypatch.setattr(svc_linux, "UNIT_PATH", link)
        for load in ("bad-setting", "not-found"):
            run = _fake_systemctl(
                system=_DEAD, user=None, system_load=load, system_fragment=str(link)
            )
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                report = svc_linux.uninstall()
            assert report.system == f"left in place (load state {load}, unit file {link})", report
            assert report.removed == frozenset()
            assert self._no_verb_issued(run), run.calls
            assert link.is_symlink() and theirs.exists()
        theirs.unlink()
        run = _fake_systemctl(
            system=_DEAD, user=None, system_load="not-found", system_fragment=str(link)
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()
        assert report.system == f"left in place (load state not-found, unit file {link})", report
        assert self._no_verb_issued(run), run.calls
        assert link.is_symlink()

    def test_a_fileless_system_refusal_that_leaves_nothing_behind_finishes_the_scope(
        self, tmp_path
    ):
        """The unfinished rule is one rule: a refusal is unfinished when it leaves a
        running unit or this module's file at `UNIT_PATH` behind. An inactive
        alias of another unit provided from a directory this module does not
        own, with no file at `UNIT_PATH`, leaves neither, so it is a report (exit
        0) — the same reading the user scope gives an inactive alias. The same
        alias WITH a file at `UNIT_PATH`, or while running, stays unfinished."""
        from kiro_crew.service import linux as svc_linux

        def teardown(system, **kw):
            run = _fake_systemctl(system=system, user=None, system_id="other.service", **kw)
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                report = svc_linux.uninstall()
            assert not any("stop" in c or "disable" in c or "rm" in c for c in run.calls), run.calls
            assert report.system == (
                f"left in place ({_UNIT} is an alias of other.service; manage that unit)"
            )
            return report

        assert teardown(_DEAD).unfinished == frozenset()
        assert teardown(_RUNNING).unfinished == frozenset({"system"})
        unit_path = tmp_path / "etc" / _UNIT
        unit_path.parent.mkdir()
        unit_path.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        with patch.object(svc_linux, "UNIT_PATH", unit_path):
            assert teardown(_DEAD).unfinished == frozenset({"system"})
        assert unit_path.exists()

    def test_removed_any_is_the_reports_structure_not_a_line_prefix(self):
        """The controller's headline reads `removed`, never the wording of a line:
        the fileless system unit that was stopped and `daemon-reload`ed counts,
        and a `removed (…)` line with no scope in the set does not."""
        from kiro_crew.service import linux as svc_linux

        gone = svc_linux.UninstallReport(
            f"stopped (its unit file /etc/systemd/system/{_UNIT} was already gone, so "
            "nothing was disabled or unlinked; daemon-reload run)",
            "not installed",
            removed=frozenset({"system"}),
        )
        assert gone.removed_any is True
        prefix_only = svc_linux.UninstallReport("removed (/etc/x.service)", "not installed")
        assert prefix_only.removed_any is False
        assert svc_linux.UninstallReport("not installed", "left in place (x)").removed_any is False

    @staticmethod
    def _linked_user_unit(tmp_path, *, marked=False):
        """An operator's own unit file OUTSIDE the account's unit directory, linked
        into it the way `systemctl --user link /path/kirocrew.service` does: the
        unit directory holds only the symlink. `marked` writes the file the way
        the installer does — the managed-marker line — which is what makes a
        linked unit Kiro Crew's own."""
        search_dir = tmp_path / ".config" / "systemd" / "user"
        search_dir.mkdir(parents=True)
        source = tmp_path / "operator" / "units" / _UNIT
        source.parent.mkdir(parents=True)
        text = _OURS_UNIT_TEXT if marked else "[Unit]\nDescription=the operator's own definition\n"
        source.write_text(text, encoding="utf-8")
        link = search_dir / _UNIT
        link.symlink_to(source)
        return search_dir, source, link

    @staticmethod
    def _no_verb_issued(run):
        return not any(
            "stop" in c or "disable" in c or "daemon-reload" in c or "rm" in c for c in run.calls
        )

    def test_a_vendor_unit_with_no_unit_file_of_ours_is_left_untouched_not_stopped(
        self, tmp_path, monkeypatch, capsys
    ):
        """A distribution's `kirocrew.service` under `/usr/lib/systemd/system`,
        loaded and running because no file of ours shadows it, and nothing at
        `/etc/systemd/system/kirocrew.service`. The fileless arm stopped it and
        `daemon-reload`ed — a unit nobody installed through this module, left
        installed and enabled while the headline read removed. Ownership is
        decided once, from the file the manager names: no marker, not the
        installer's path — not ours. Nothing is stopped, the scope is finished,
        nothing is removed, exit 0."""
        from kiro_crew.service import controller
        from kiro_crew.service import linux as svc_linux

        vendor = tmp_path / "usr" / "lib" / "systemd" / "system" / _UNIT
        vendor.parent.mkdir(parents=True)
        vendor.write_text(
            "[Unit]\nDescription=the distribution's own definition\n", encoding="utf-8"
        )
        run = _fake_systemctl(system=_RUNNING, user=None, system_fragment=str(vendor))
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch("kiro_crew.service.controller.current_platform", return_value=Platform.SYSTEMD),
            patch.object(svc_linux, "remove_apparmor_profile", return_value=MagicMock(message="")),
        ):
            report = svc_linux.uninstall()
            rc = controller.uninstall_service()

        assert ["sudo", "systemctl", "stop", _UNIT] not in run.calls, run.calls
        assert self._no_verb_issued(run), run.calls
        assert report.system.startswith("left untouched (not installed by Kiro Crew: ")
        assert str(vendor) in report.system and str(svc_linux.UNIT_PATH) in report.system
        assert report.unfinished == frozenset() and report.removed == frozenset()
        assert vendor.exists()
        assert rc == 0
        out = capsys.readouterr().out
        assert "No kirocrew service was removed." in out
        assert "system scope: left untouched (not installed by Kiro Crew" in out

    def test_the_marker_makes_a_unit_ours_wherever_it_lives(self, tmp_path):
        """The one line every rendered unit carries, `Environment="KIROCREW_SERVICE_MANAGED=1"`,
        read the way `kirocrew doctor` reads it. A copy of our unit under another
        unit directory is ours to tear down; a file that cannot be read, or that
        does not exist, makes no claim."""
        from kiro_crew.service import linux as svc_linux

        elsewhere = tmp_path / "usr" / "lib" / "systemd" / "system" / _UNIT
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        assert svc_linux.unit_file_carries_managed_marker(elsewhere) is True
        assert svc_linux._owned_unit(str(elsewhere), user=False) == svc_linux.OwnedUnit(
            "system", str(elsewhere), str(elsewhere)
        )
        elsewhere.write_text("[Unit]\nEnvironment=KIROCREW_SERVICE_MANAGED=0\n", encoding="utf-8")
        assert svc_linux.unit_file_carries_managed_marker(elsewhere) is False
        assert svc_linux._owned_unit(str(elsewhere), user=False) is None
        assert svc_linux.unit_file_carries_managed_marker(tmp_path / "absent.service") is False
        elsewhere.write_bytes(b"\xff\xfe[Unit]\n")
        assert svc_linux.unit_file_carries_managed_marker(elsewhere) is False
        # The installer's own path is ours by path, marker or not (units written
        # before the marker existed) — and a definition under another name never is.
        own = Path(svc_linux._installer_unit_path(user=True))
        own.parent.mkdir(parents=True)
        own.write_text("[Unit]\n", encoding="utf-8")
        assert svc_linux._owned_unit(str(own), user=True) == svc_linux.OwnedUnit(
            "user", str(own), str(own)
        )
        other = own.with_name("kirocrew-pod@.service")
        other.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        assert svc_linux._owned_unit(str(other), user=True) is None

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_uninstall_never_deletes_a_linked_units_source_file(self, tmp_path):
        """`systemctl --user link /path/kirocrew.service` on an operator's own
        unit, reported by its resolved target (the `open_follow()` loader) — the
        layout whose source an earlier head unlinked. The definition is the
        operator's: no marker, not the installer's path. Not ours, so not
        stopped, not disabled, and neither the source nor the link is touched:
        the scope finishes with `left untouched (…)`."""
        from kiro_crew.service import linux as svc_linux

        search_dir, source, link = self._linked_user_unit(tmp_path)
        contents = source.read_text(encoding="utf-8")
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(source))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert source.exists(), "the operator's linked unit file was deleted"
        assert source.read_text(encoding="utf-8") == contents
        assert link.is_symlink(), "the operator's link entry was removed"
        assert report.user.startswith("left untouched (not installed by Kiro Crew: ")
        assert report.removed == frozenset() and report.unfinished == frozenset()
        assert self._no_verb_issued(run), run.calls

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_uninstall_removes_only_the_link_when_the_manager_reports_the_link_itself(
        self, tmp_path
    ):
        """A linked unit whose source carries OUR marker (a copy of the rendered
        unit an operator parked elsewhere and linked), on the name-map loader
        (systemd >= 245), which reports the LINK as `FragmentPath`. Ours by the
        marker; linked, so the link entry is removed and the source it points at
        is kept — removing the link is what uninstalls it."""
        from kiro_crew.service import linux as svc_linux

        search_dir, source, link = self._linked_user_unit(tmp_path, marked=True)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(link))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert source.exists() and "KIROCREW_SERVICE_MANAGED" in source.read_text(encoding="utf-8")
        assert not link.is_symlink() and not link.exists()
        assert report.user == (
            f"removed (the link to {source}; the linked unit file {source} itself was kept)"
        )
        assert report.removed == frozenset({"user"}) and report.unfinished == frozenset()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "disable", _UNIT] in run.calls
        assert ["systemctl", "--user", "daemon-reload"] in run.calls

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_a_higher_priority_link_to_a_lower_priority_unit_file_keeps_that_file(self, tmp_path):
        """A `kirocrew.service` symlink at the installer's own path pointing at a
        direct file in a lower-priority unit directory (a packaged unit), the
        manager reporting the RESOLVED file as `FragmentPath` (what a live
        systemd 252 does — PR body, transcript 4, C5). The file's own shape read
        like the remedy's unit and an earlier head unlinked it. Ownership now
        looks at the definition: the packaged file carries no marker and is not
        the installer's path — not ours. Nothing is stopped; link and file stay."""
        from kiro_crew.service import linux as svc_linux

        own = Path(svc_linux._installer_unit_path(user=True))
        own.parent.mkdir(parents=True)
        lib_dir = tmp_path / "usr" / "lib" / "systemd" / "user"
        lib_dir.mkdir(parents=True)
        packaged = lib_dir / _UNIT
        packaged.write_text(
            "[Unit]\nDescription=the distribution's own definition\n", encoding="utf-8"
        )
        own.symlink_to(packaged)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(packaged))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert packaged.exists(), "the operator's lower-priority unit file was deleted"
        assert "distribution's own definition" in packaged.read_text(encoding="utf-8")
        assert own.is_symlink(), "the operator's link at our path was removed"
        assert report.user.startswith("left untouched (not installed by Kiro Crew: ")
        assert report.removed == frozenset() and report.unfinished == frozenset()
        assert self._no_verb_issued(run), run.calls

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_a_disable_the_manager_refuses_on_that_layout_keeps_both_the_link_and_the_file(
        self, tmp_path
    ):
        """The same layout with a MARKED lower-priority file — ours through the
        link at the installer's path — where the manager refuses `disable` the
        way a live systemd 252 does on a same-name link inside the search path
        (`Refusing to operate on alias name or linked unit file`, transcript 4,
        C5). The teardown ends at that refusal, after the stop and before any
        unlink: link and file both survive, the scope is unfinished."""
        from kiro_crew.service import linux as svc_linux

        own = Path(svc_linux._installer_unit_path(user=True))
        own.parent.mkdir(parents=True)
        lib_dir = tmp_path / "usr" / "lib" / "systemd" / "user"
        lib_dir.mkdir(parents=True)
        packaged = lib_dir / _UNIT
        packaged.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        own.symlink_to(packaged)
        refused = subprocess.CompletedProcess(
            [],
            1,
            "",
            f"Failed to disable unit: Refusing to operate on alias name or linked unit file: {_UNIT}\n",
        )
        run = _fake_systemctl(
            system=None, user=_RUNNING, user_fragment=str(packaged), overrides={"disable": refused}
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert packaged.exists() and own.is_symlink()
        assert report.user.startswith(f"left in place (`systemctl --user disable {_UNIT}` failed: ")
        assert "Refusing to operate on alias name or linked unit file" in report.user
        assert report.user.endswith("; the unit file was not removed)")
        assert report.unfinished == frozenset({"user"}) and report.removed == frozenset()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert ["systemctl", "--user", "daemon-reload"] not in run.calls

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_a_marked_file_reached_through_a_link_at_our_path_loses_the_link_and_keeps_the_file(
        self, tmp_path
    ):
        """The marked lower-priority file when `disable` passes: ours by the
        marker, reached through the link at the installer's path, so the link
        goes and the file stays — the line says which."""
        from kiro_crew.service import linux as svc_linux

        own = Path(svc_linux._installer_unit_path(user=True))
        own.parent.mkdir(parents=True)
        lib_dir = tmp_path / "usr" / "lib" / "systemd" / "user"
        lib_dir.mkdir(parents=True)
        packaged = lib_dir / _UNIT
        packaged.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        own.symlink_to(packaged)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(packaged))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert packaged.exists() and not own.is_symlink() and not own.exists()
        assert report.user == (
            f"removed (the link to {packaged}; the linked unit file {packaged} itself was kept)"
        )
        assert report.removed == frozenset({"user"}) and report.unfinished == frozenset()

    def test_a_direct_unit_file_nothing_links_to_is_still_unlinked(self, tmp_path):
        """The remedy's own unit at the installer's path, beside a same-named
        direct file in a lower-priority directory that nothing links. Ours by
        path; unlinked as before, and the other file — not the definition the
        manager loaded — is not touched."""
        from kiro_crew.service import linux as svc_linux

        ours = Path(svc_linux._installer_unit_path(user=True))
        ours.parent.mkdir(parents=True)
        ours.write_text("[Unit]\nDescription=Kiro Crew gateway\n", encoding="utf-8")
        lib_dir = tmp_path / "usr" / "lib" / "systemd" / "user"
        lib_dir.mkdir(parents=True)
        shadowed = lib_dir / _UNIT
        shadowed.write_text(
            "[Unit]\nDescription=a packaged unit the account's own shadows\n", encoding="utf-8"
        )
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(ours))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert not ours.exists()
        assert shadowed.exists(), "a same-named direct file elsewhere is not ours to touch"
        assert report.user == f"removed ({ours})"
        assert report.removed == frozenset({"user"}) and report.unfinished == frozenset()

    @pytest.mark.skipif(
        os.name != "posix",
        reason="creates a real symlink for a linked systemd user unit; Windows has no "
        "unprivileged symlink creation and no systemd. Runs on the Linux and macOS jobs.",
    )
    def test_a_link_entry_that_cannot_be_removed_is_reported_and_the_source_still_kept(
        self, tmp_path
    ):
        from kiro_crew.service import linux as svc_linux

        search_dir, source, link = self._linked_user_unit(tmp_path, marked=True)
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(source))
        with (
            patch("kiro_crew.service.linux.subprocess.run", side_effect=run),
            patch(
                "kiro_crew.service.linux.os.unlink",
                side_effect=PermissionError("Operation not permitted"),
            ),
        ):
            report = svc_linux.uninstall()

        assert source.exists() and link.is_symlink()
        assert report.user.startswith(
            f"stopped and disabled, but the link {link} to its unit file {source} "
        )
        assert "not the file it points at" in report.user
        assert report.unfinished == frozenset({"user"}) and report.removed == frozenset()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls

    def test_every_teardown_arm_rests_on_the_one_ownership_decision(self, tmp_path, monkeypatch):
        """Structural: the verbs of a teardown — stop, disable, the unlink / rm, the
        daemon-reload — are reachable only through `_teardown_owned` and
        `_remove_stale_owned`, whose first parameter is an `OwnedUnit`, so no arm
        can act without the resolver having claimed the unit. Checked two ways:
        by the module's own source (no other function spawns `disable`, `rm` or
        `os.unlink` on a unit file), and at run time — with `_owned_unit`
        answering `None` for every layout, `uninstall()` issues no verb in either
        scope, whatever the manager reports."""
        import ast
        import importlib
        import inspect

        from kiro_crew.service import linux as svc_linux

        family = (svc_linux, *map(importlib.import_module, svc_linux._OWNERS))
        tree = ast.parse("".join(inspect.getsource(module) for module in family))
        owned_first = set()
        arms = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            args = node.args.args
            if args and getattr(args[0].annotation, "id", None) == "OwnedUnit":
                owned_first.add(node.name)
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                target = call.func
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                literal = [a.value for a in call.args if isinstance(a, ast.Constant)]
                if name == "_stop_and_disable" or (name == "_sudo_run" and literal[:1] == ["rm"]):
                    arms.add(node.name)
                if name == "unlink" and getattr(target.value, "id", None) == "os":
                    arms.add(node.name)
        # The installer's helpers unlink their own temp files on the way to a
        # sudo write; nothing the manager loads. Every other arm takes the
        # OwnedUnit.
        installer_temp_files = {"_seed_env_file", "_install_file_via_sudo", "_write_unit_via_sudo"}
        assert arms - installer_temp_files <= owned_first, (arms, owned_first)
        assert {
            "_teardown_owned",
            "_remove_stale_owned",
            "_remove_owned",
            "_finish_scope",
        } <= owned_first

        monkeypatch.setattr(svc_linux, "_owned_unit", lambda fragment, *, user: None)
        system_unit = Path(svc_linux._installer_unit_path(user=False))
        system_unit.parent.mkdir(parents=True)
        system_unit.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        user_unit = Path(svc_linux._installer_unit_path(user=True))
        user_unit.parent.mkdir(parents=True)
        user_unit.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        for system, user in (
            (_RUNNING, _RUNNING),
            (_DEAD, _DEAD),
            (None, _RUNNING),
            (_RUNNING, None),
        ):
            run = _fake_systemctl(system=system, user=user, user_fragment=str(user_unit))
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                report = svc_linux.uninstall()
            assert self._no_verb_issued(run), (system, user, run.calls)
            assert report.removed == frozenset(), (system, user, report)
            assert system_unit.exists() and user_unit.exists()

    def test_with_no_unit_file_an_unreachable_system_manager_is_reported_only_where_one_can_run(
        self, tmp_path, monkeypatch
    ):
        """`sd_booted(3)`'s test decides the empty answer: with `/run/systemd/system`
        present a manager runs that this shell cannot see, and it may well hold the
        unit, so the line is `not reachable from this shell (…)`, not `not
        installed` — nothing is left behind, so the scope is not unfinished. Without
        that directory no manager exists and `not installed` is the fact."""
        from kiro_crew.service import linux as svc_linux

        inner = _fake_systemctl(system=None, user=None)
        denied = "Failed to connect to bus: Permission denied"

        def run(argv, *a, **kw):
            res = inner(argv, *a, **kw)
            if "systemctl" in argv:
                return subprocess.CompletedProcess(list(argv), 1, "", denied + "\n")
            return res

        booted = tmp_path / "run-systemd"
        booted.mkdir()
        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", booted)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            on_host = svc_linux.uninstall()
        assert on_host.system == f"not reachable from this shell ({denied})"
        assert on_host.unfinished == frozenset()
        assert not any("sudo" in c for c in inner.calls), inner.calls

        monkeypatch.setattr(svc_linux, "_SYSTEMD_BOOTED_DIR", tmp_path / "run-systemd-absent")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            in_container = svc_linux.uninstall()
        assert in_container.system == "not installed"
        assert in_container.unfinished == frozenset()

    def test_user_unit_path_answers_only_for_a_loaded_unit_that_is_ours(self, tmp_path):
        """Doctor reads the file this returns as the service definition, so it is
        handed one only when the manager has it LOADED under our canonical `Id`:
        a blank or malformed `show` answer, a unit that failed to parse
        (`bad-setting`), a mask and an alias all answer None — the old
        `installed` test (any load state but `not-found`) let each of them through."""
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")

        def path_for(fragment_path=str(fragment), **kw):
            run = _fake_systemctl(system=None, user=_DEAD, user_fragment=fragment_path, **kw)
            with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
                return svc_linux.user_unit_path()

        assert path_for() == fragment
        assert path_for(user_load="", user_id="") is None, "a blank answer was taken as a unit"
        assert path_for(user_load="bad-setting") is None, "an unparseable unit was handed to doctor"
        assert path_for(user_load="error") is None
        assert path_for("/dev/null", user_load="masked") is None
        assert path_for(user_id="shared.service") is None
        assert path_for(user_load="not-found") is None

    def test_a_system_scope_alias_is_never_stopped_or_disabled_on_our_name(
        self, tmp_path, monkeypatch
    ):
        """`stop` / `disable` on an alias act on the unit it resolves to; the
        system scope refuses the alias like the user scope does."""
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        run = _fake_systemctl(system=_RUNNING, user=None, system_id="shared.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert system_unit.exists()
        assert not any("rm" in c or "stop" in c or "disable" in c for c in run.calls), run.calls
        assert report.system == (
            f"left in place ({_UNIT} is an alias of shared.service; manage that unit)"
        )
        assert report.unfinished == frozenset({"system"})

    def test_a_stop_that_returns_0_but_leaves_the_unit_running_is_not_followed_by_unlink(
        self, tmp_path
    ):
        """The unlink rests on the manager's ActiveState after the stop, not on
        the stop verb's exit code alone."""
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(
            system=None, user=_RUNNING, user_fragment=str(fragment), stop_is_ignored=True
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert fragment.exists()
        assert ["systemctl", "--user", "stop", _UNIT] in run.calls
        assert not any(c[-1:] == ["daemon-reload"] for c in run.calls), run.calls
        assert report.user.startswith("left in place (still active (running) after")
        assert "returned 0" in report.user
        assert report.unfinished == frozenset({"user"})

    def test_uninstall_reads_the_state_back_after_the_stop_before_unlinking(self, tmp_path):
        """Order per scope: read the unit, read the manager's search path (which
        settles what the unlink may touch BEFORE `disable` removes a linked unit's
        entry) -> stop -> disable -> verify inactive (a second unit `show`) ->
        unlink -> daemon-reload."""
        from kiro_crew.service import linux as svc_linux

        fragment = tmp_path / "user" / _UNIT
        fragment.parent.mkdir()
        fragment.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(fragment))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.user == f"removed ({fragment})"
        assert report.removed == frozenset({"user"})
        verbs = [
            next(t for t in c if t in ("show", "stop", "disable", "daemon-reload"))
            for c in run.calls
            if "--user" in c
        ]
        assert verbs == ["show", "stop", "disable", "show", "daemon-reload"], verbs

    def test_a_crash_looping_unit_in_auto_restart_is_still_stopped_and_counted(self):
        """The reporter's `203/EXEC` loop spends nearly all its time in
        `activating (auto-restart)`, which `is-active` answers non-zero for. The
        scope selector reads ActiveState from `show`, so `kirocrew stop` reaches
        the loop, `is_active()` counts it and the logs command prefers its journal."""
        from kiro_crew.service import linux as svc_linux

        looping = {"ActiveState": "activating", "SubState": "auto-restart"}
        run = _fake_systemctl(system=None, user=looping)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()
            assert svc_linux.is_active() is True
            assert svc_linux.user_unit_active() is True
            svc_linux.stop()

        assert ["systemctl", "--user", "stop", _UNIT] in run.calls, run.calls
        assert not any("is-active" in c for c in run.calls), run.calls
        assert "user scope: activating (auto-restart)" in out

    def test_restart_never_starts_a_stopped_unit_in_the_other_scope(self):
        """Selecting on "running" rather than "installed": a stopped system unit
        left beside the running user unit is not restarted (started) on the side."""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=_DEAD, user=_RUNNING)
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert report.ok is True and [o.scope for o in report.outcomes] == ["user"]

        assert ["systemctl", "--user", "restart", _UNIT] in run.calls, run.calls
        assert not any(c[:3] == ["sudo", "systemctl", "restart"] for c in run.calls), run.calls

    def test_a_system_unit_file_the_manager_has_not_loaded_is_removed_without_a_stop(
        self, tmp_path, monkeypatch
    ):
        """A file dropped without a daemon-reload: the manager answers
        `LoadState=not-found`, a `stop` would only fail (exit 5, not loaded), and
        nothing runs under it -- removed, no stop/disable issued."""
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        not_loaded = subprocess.CompletedProcess(
            [], 5, "", f"Failed to stop {_UNIT}: Unit {_UNIT} not loaded.\n"
        )
        run = _fake_systemctl(system=None, user=None, overrides={"stop": not_loaded})
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system == f"removed ({system_unit})"
        assert any(c[:3] == ["sudo", "rm", "-f"] for c in run.calls), run.calls
        assert not any("stop" in c or "disable" in c for c in run.calls), run.calls
        assert ["sudo", "systemctl", "daemon-reload"] in run.calls
        assert report.unfinished == frozenset()

    def test_a_system_unit_file_rm_that_does_not_take_is_reported_not_claimed_removed(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.service import linux as svc_linux

        system_unit = tmp_path / "etc" / _UNIT
        system_unit.parent.mkdir()
        system_unit.write_text("", encoding="utf-8")
        monkeypatch.setattr(svc_linux, "UNIT_PATH", system_unit)
        inner = _fake_systemctl(system=_DEAD, user=None)

        def run(argv, *a, **kw):
            res = inner(argv, *a, **kw)
            if "rm" in argv:
                return subprocess.CompletedProcess(
                    list(argv), 1, "", f"rm: cannot remove '{system_unit}': Read-only file system\n"
                )
            return res

        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert report.system.startswith("stopped and disabled, but its unit file")
        assert "Read-only file system" in report.system
        assert "sudo systemctl daemon-reload" in report.system
        assert report.unfinished == frozenset({"system"})
        assert not any(c[-1:] == ["daemon-reload"] for c in inner.calls), inner.calls

    def test_uninstall_never_deletes_the_target_of_an_alias(self, tmp_path):
        """`systemctl show kirocrew.service` on an ALIAS answers for the canonical
        unit -- `Id=shared.service`, that unit's `FragmentPath` -- so following
        the path would delete someone else's unit file. Nothing in that scope is
        stopped, disabled or unlinked; the report names the alias."""
        from kiro_crew.service import linux as svc_linux

        shared = tmp_path / "shared.service"
        shared.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(
            system=None, user=_RUNNING, user_fragment=str(shared), user_id="shared.service"
        )
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert shared.exists()
        assert report.user.startswith("left in place")
        assert "shared.service" in report.user
        assert all("show" in c for c in self._user_calls(run)), run.calls

    def test_uninstall_leaves_a_masked_user_unit_alone_when_the_mask_is_not_at_our_path(
        self, tmp_path
    ):
        """The manager answers `masked` for the user unit and reports `/dev/null` as
        its fragment (the older loader's reading), with NO entry at the per-user
        path the installer writes to: a runtime mask, or one placed in another
        user-unit directory. Nothing at our path means nothing of ours to remove
        — the scope is reported, not acted on. (A mask AT our path is a mask of
        our name and is removed: see the persistent-mask tests above.)"""
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(
            system=None, user=_DEAD, user_fragment="/dev/null", user_load="masked"
        )
        # fmt: off
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run), patch(
            "kiro_crew.service.linux.os.unlink"
        ) as unlink:
            report = svc_linux.uninstall()
        # fmt: on

        unlink.assert_not_called()
        assert report.user.startswith("left in place")
        assert "masked" in report.user
        assert not any("stop" in c or "disable" in c for c in run.calls), run.calls

    def test_uninstall_only_unlinks_a_file_named_after_the_unit(self, tmp_path):
        """A definition under another name is never ours, marker or not: the
        installer names its files after the unit."""
        from kiro_crew.service import linux as svc_linux

        other = tmp_path / "gateway.service"
        other.write_text(_OURS_UNIT_TEXT, encoding="utf-8")
        run = _fake_systemctl(system=None, user=_RUNNING, user_fragment=str(other))
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.uninstall()

        assert other.exists()
        assert report.user.startswith("left untouched (not installed by Kiro Crew: ")
        assert self._no_verb_issued(run), run.calls

    def test_status_names_an_alias_for_what_it_is(self, tmp_path):
        from kiro_crew.service import linux as svc_linux

        run = _fake_systemctl(system=None, user=_RUNNING, user_id="shared.service")
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            out = svc_linux.status()

        assert "user scope: active (running), an alias of shared.service" in out

    def test_restart_reaches_every_active_scope_even_after_a_refusal(self):
        """Two scopes running the unit and both restarts refused: the second
        scope's restart must still be issued, and the report names each scope's
        refusal with the restart command for THAT scope."""
        from kiro_crew.service import linux as svc_linux

        refused = subprocess.CompletedProcess([], 1, "", "Interactive authentication required")
        run = _fake_systemctl(system=_RUNNING, user=_RUNNING, overrides={"restart": refused})
        with patch("kiro_crew.service.linux.subprocess.run", side_effect=run):
            report = svc_linux.restart()
        assert report.ok is False
        assert [(f.scope, f.kind, f.hint) for f in report.failures] == [
            ("system", RESTART_REFUSED, "sudo systemctl restart kirocrew"),
            ("user", RESTART_REFUSED, "systemctl --user restart kirocrew"),
        ]

        assert ["sudo", "systemctl", "restart", _UNIT] in run.calls, run.calls
        assert ["systemctl", "--user", "restart", _UNIT] in run.calls, run.calls

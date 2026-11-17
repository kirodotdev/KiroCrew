"""macOS service verbs all address the same launchd domain.

This host cannot run launchd, so the narrowest honest check is the argv the
module builds, run against a stateful ``launchctl`` stand-in that models the
one behaviour at issue: the legacy ``load``/``unload`` verbs act on the
CALLER's domain (``user/<uid>`` from SSH), while ``bootstrap``/``bootout``/
``print``/``kickstart`` name a domain explicitly.
"""

from __future__ import annotations

import plistlib
import subprocess
from unittest.mock import patch

import pytest

from kiro_crew.service import controller
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import LAUNCHD_LABEL, Platform

UID = 501
GUI = f"gui/{UID}"
USER = f"user/{UID}"
NOT_FOUND = f'Could not find service "{LAUNCHD_LABEL}" in domain for uid: {UID}'


class FakeLaunchctl:
    """Just enough launchd to tell the domains apart.

    ``session`` is the caller's own domain (what ``load``/``unload`` act on).
    ``desktop`` says whether a gui/<uid> domain exists at all (someone is logged
    in at the Mac). ``linger`` is how many ``print`` probes still see a job after
    its ``bootout`` was accepted — launchd unloads asynchronously.
    """

    def __init__(self, *, session: str = GUI, desktop: bool = True, linger: int = 0):
        self.session = session
        self.desktop = desktop
        self.linger = linger
        self.loaded: set[str] = set()
        self.disabled: set[str] = set()
        self.leaving: dict[str, int] = {}
        self.refuse_bootout = False
        self.crashing = False
        # How many `print` probes after a bootstrap still show no pid: launchd
        # spawns a job it has just bootstrapped asynchronously.
        self.spawn_after = 0
        self.unspawned: dict[str, int] = {}
        # Older macOS prints `=> true` for a disabled label; None answers rc 1.
        self.disabled_spelling: str | None = "disabled"
        self.calls: list[list[str]] = []

    def _job_in(self, domain: str) -> bool:
        return domain in self.loaded or domain in self.leaving

    def _settle(self, domain: str) -> None:
        if domain in self.leaving:
            self.leaving[domain] -= 1
            if self.leaving[domain] <= 0:
                del self.leaving[domain]

    def __call__(self, argv, **_kwargs):
        assert argv[0] == "launchctl", argv
        self.calls.append(list(argv))
        verb, args = argv[1], argv[2:]
        ok = subprocess.CompletedProcess(argv, 0, "", "")
        missing = subprocess.CompletedProcess(argv, 113, "", NOT_FOUND)
        if verb == "list":
            # Domain-implicit like load/unload: reads the caller's domain.
            if self.session in self.loaded:
                return subprocess.CompletedProcess(argv, 0, '\t"PID" = 4242;\n', "")
            return subprocess.CompletedProcess(argv, 113, "", NOT_FOUND)
        if verb in ("load", "unload"):
            # Legacy, domain-implicit spelling: the caller's domain.
            if verb == "load":
                self.loaded.add(self.session)
                if "-w" in args:
                    self.disabled.discard(self.session)
            else:
                self.loaded.discard(self.session)
                if "-w" in args:
                    self.disabled.add(self.session)
            return ok
        if verb == "bootstrap":
            domain = args[0]
            if domain.startswith("gui/") and not self.desktop:
                return subprocess.CompletedProcess(
                    argv, 125, "", "Bootstrap failed: 125: Domain does not support specified action"
                )
            if domain in self.disabled:
                return subprocess.CompletedProcess(
                    argv, 5, "", "Bootstrap failed: 5: Input/output error (service is disabled)"
                )
            if self._job_in(domain):
                return subprocess.CompletedProcess(
                    argv, 5, "", "Bootstrap failed: 5: Input/output error"
                )
            self.loaded.add(domain)
            self.unspawned[domain] = self.spawn_after
            return ok
        if verb == "print-disabled":
            if self.disabled_spelling is None:
                return subprocess.CompletedProcess(argv, 1, "", "Operation not permitted")
            # An unlisted label is enabled.
            spelling = self.disabled_spelling
            listed = f'\t"{LAUNCHD_LABEL}" => {spelling}\n' if args[0] in self.disabled else ""
            return subprocess.CompletedProcess(argv, 0, f"disabled services = {{\n{listed}}}\n", "")
        domain = args[-1].rsplit("/", 1)[0]
        if domain.startswith("gui/") and not self.desktop:
            # No desktop login: the gui domain itself is missing, for every verb.
            return subprocess.CompletedProcess(
                argv, 125, "", "125: Domain does not support specified action"
            )
        if verb == "bootout":
            if self.refuse_bootout:
                return subprocess.CompletedProcess(argv, 1, "", "Operation not permitted")
            if domain not in self.loaded:
                return subprocess.CompletedProcess(
                    argv, 3, "", "Boot-out failed: 3: No such process"
                )
            self.loaded.discard(domain)
            if self.linger:
                self.leaving[domain] = self.linger
            return ok
        if verb == "print":
            present = self._job_in(domain)
            self._settle(domain)
            if not present:
                return missing
            # A running job's print names its pid; one whose program exits at
            # once (`crashing`) or that is on its way out names none.
            running = domain in self.loaded and not self.crashing
            if running and self.unspawned.get(domain, 0) > 0:
                self.unspawned[domain] -= 1
                running = False
            return subprocess.CompletedProcess(argv, 0, "\tpid = 4242\n" if running else "", "")
        if verb == "kickstart":
            return ok if domain in self.loaded else missing
        # Both override verbs come after the no-desktop check above, as on a real Mac.
        if verb == "disable":
            self.disabled.add(domain)
            return ok
        if verb == "enable":
            self.disabled.discard(domain)
            return ok
        raise AssertionError(f"unexpected launchctl verb: {argv}")


@pytest.fixture
def mac(tmp_path, monkeypatch):
    plist_dir = tmp_path / "LaunchAgents"
    log_dir = tmp_path / "Logs"
    monkeypatch.setattr(svc_macos, "PLIST_DIR", plist_dir)
    monkeypatch.setattr(svc_macos, "PLIST_PATH", plist_dir / f"{LAUNCHD_LABEL}.plist")
    monkeypatch.setattr(svc_macos, "LOG_DIR", log_dir)
    monkeypatch.setattr(svc_macos, "STDOUT_LOG", log_dir / "gateway.log")
    monkeypatch.setattr(svc_macos, "STDERR_LOG", log_dir / "gateway.err")
    monkeypatch.setattr(svc_macos, "LIVE_PROGRAM", tmp_path / "gateway-live")
    monkeypatch.setattr(svc_macos, "kirocrew_bin", lambda: "/opt/homebrew/bin/kirocrew")
    monkeypatch.setattr(svc_macos.os, "getuid", lambda: UID, raising=False)
    monkeypatch.setattr(svc_macos, "_BOOTOUT_POLL_SECS", 0.0, raising=False)
    monkeypatch.setattr(svc_macos, "_START_SETTLE_SECS", 0.0, raising=False)
    return svc_macos


def _run(fake):
    return patch("kiro_crew.service.macos.subprocess.run", side_effect=fake)


def _with_plist(mac) -> None:
    mac.PLIST_DIR.mkdir(parents=True, exist_ok=True)
    mac.PLIST_PATH.write_text("<plist/>")


def test_install_from_ssh_lands_where_restart_and_stop_address_it(mac):
    """The reported shape: an install from a non-Aqua session while the
    desktop is logged in. Every later verb names gui/<uid>, so the job must be
    there and nowhere else."""
    fake = FakeLaunchctl(session=USER)
    with _run(fake):
        mac.install()
        assert fake.loaded == {GUI}
        assert mac.restart() is True
        assert mac.stop() is True
    assert fake.loaded == set()
    assert not any(c[1] in ("load", "unload") for c in fake.calls), fake.calls


def test_kirocrew_stop_from_ssh_boots_out_the_gui_job(mac):
    """`kirocrew stop` over SSH: the caller-domain `list` misses the gui/<uid>
    job, and stop_service must still boot it out rather than report nothing
    to stop (which would leave KeepAlive respawning a SIGTERMed gateway)."""
    fake = FakeLaunchctl(session=USER)
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        assert mac.is_active() is False  # the caller-domain probe misses it
        assert controller.stop_service() is True
    assert fake.loaded == set()


def test_kirocrew_stop_with_nothing_loaded_reports_no_stop(mac):
    fake = FakeLaunchctl(session=USER)
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        assert controller.stop_service() is True
        assert controller.stop_service() is False


@pytest.mark.parametrize("desktop", [True, False], ids=["desktop", "no-desktop"])
def test_kirocrew_restart_from_ssh_kickstarts_the_service_job(mac, desktop):
    """`kirocrew restart` over SSH: the caller-domain `list` misses the job, and
    restart_service must still kickstart it rather than return an empty report
    (which sends the CLI to SIGTERM-by-port + a detached spawn beside the
    KeepAlive job)."""
    fake = FakeLaunchctl(session=USER, desktop=desktop)
    job = GUI if desktop else USER
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        report = controller.restart_service()
    assert report and report.ok is True
    assert ["launchctl", "kickstart", "-k", f"{job}/{LAUNCHD_LABEL}"] in fake.calls


def test_kirocrew_restart_with_nothing_loaded_leaves_it_to_the_foreground_path(mac):
    fake = FakeLaunchctl(session=USER)
    _with_plist(mac)
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        report = controller.restart_service()
    assert not report and report.attempted is False
    assert not any(c[1] == "kickstart" for c in fake.calls), fake.calls


def test_install_without_a_desktop_session_falls_back_to_the_user_domain(mac, caplog):
    """SSH with nobody at the desktop: gui/<uid> does not exist. A legacy
    `load -w` succeeded here by loading into user/<uid>, so the install must
    too -- landing in user/<uid>, naming it in the log, and staying reachable
    by stop and uninstall."""
    fake = FakeLaunchctl(session=USER, desktop=False)
    with _run(fake), caplog.at_level("INFO", logger=svc_macos.log.name):
        mac.install()
        assert fake.loaded == {USER}
        assert ["launchctl", "bootstrap", GUI, str(mac.PLIST_PATH)] in fake.calls
        assert ["launchctl", "bootstrap", USER, str(mac.PLIST_PATH)] in fake.calls
        assert USER in caplog.text and "unavailable" in caplog.text
        assert mac.restart() is True
        assert ["launchctl", "kickstart", "-k", f"{USER}/{LAUNCHD_LABEL}"] in fake.calls
        assert mac.stop() is True
    assert fake.loaded == set()


def test_reinstall_and_uninstall_without_a_desktop_session_reach_the_user_job(mac):
    """bootout of the missing gui domain answers 125, which must not read as a
    refusal: the user-domain job behind it still has to be found."""
    fake = FakeLaunchctl(session=USER, desktop=False)
    with _run(fake):
        mac.install()
        mac.install()  # reinstall boots the first copy out before bootstrapping
        assert fake.loaded == {USER}
        mac.uninstall()
    assert fake.loaded == set()
    assert not mac.PLIST_PATH.exists()


def test_install_fails_on_a_refusal_other_than_an_unavailable_domain(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    fake.refuse_bootout = True  # launchd keeps the job, so bootstrap is refused
    with _run(fake), pytest.raises(svc_macos.ServiceInstallError) as err:
        mac.install()
    assert "Input/output error" in str(err.value)
    assert f"bootstrap {GUI}" in str(err.value)
    # Only an unavailable domain falls back; any other refusal is reported.
    assert not any(c[1:3] == ["bootstrap", USER] for c in fake.calls), fake.calls


def test_install_boots_out_a_loaded_job_whose_plist_is_gone(mac):
    """An uninstall launchd refused, or a hand-deleted plist, leaves the job
    loaded with no file. Install must still boot it out before bootstrapping."""
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    with _run(fake):
        try:
            mac.install()
        except svc_macos.ServiceInstallError as exc:
            pytest.fail(f"install over a loaded job with no plist failed: {exc}")
    assert fake.loaded == {GUI}
    verbs = [c[1] for c in fake.calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")


@pytest.mark.parametrize("legacy_domain", [GUI, USER], ids=["gui", "user"])
def test_reinstall_migrates_a_legacy_load_w_job(mac, legacy_domain):
    """An agent an older version loaded with `load -w` — into gui/<uid> from a
    desktop session or user/<uid> from SSH — is booted out and reinstalled
    into gui/<uid>, with no second copy left behind."""
    _with_plist(mac)
    fake = FakeLaunchctl(session=legacy_domain, linger=3)
    fake.loaded.add(legacy_domain)
    with _run(fake):
        mac.install()
    assert fake.loaded == {GUI}
    assert not fake.leaving
    verbs = [c[1] for c in fake.calls]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_reinstall_waits_for_the_async_bootout_before_bootstrapping(mac):
    _with_plist(mac)
    fake = FakeLaunchctl(linger=5)
    fake.loaded.add(GUI)
    with _run(fake):
        mac.install()
    assert fake.loaded == {GUI}
    boot = next(i for i, c in enumerate(fake.calls) if c[1] == "bootout")
    strap = next(i for i, c in enumerate(fake.calls) if c[1] == "bootstrap")
    assert any(c[1] == "print" for c in fake.calls[boot:strap])


def test_install_clears_a_disabled_override_left_by_legacy_uninstall(mac):
    """A legacy `unload -w` persists Disabled=true, and bootstrap refuses a
    disabled job, so install must `enable` it first."""
    fake = FakeLaunchctl()
    fake.disabled.add(GUI)
    with _run(fake):
        mac.install()
    assert fake.loaded == {GUI}


def test_stop_reports_a_refused_bootout(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    fake.refuse_bootout = True
    with _run(fake):
        assert mac.stop() is False
    assert fake.loaded == {GUI}


def test_stop_leaves_nothing_to_respawn_and_keeps_the_plist_enabled(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    with _run(fake):
        assert mac.stop() is True
    assert fake.loaded == set()
    assert mac.PLIST_PATH.exists()
    assert fake.disabled == set()
    assert not any(c[1] in ("stop", "disable") or "-w" in c for c in fake.calls)


def test_stop_still_finds_a_legacy_user_domain_job(mac):
    _with_plist(mac)
    fake = FakeLaunchctl(session=USER)
    fake.loaded.add(USER)
    with _run(fake):
        assert mac.stop() is True
    assert fake.loaded == set()


def test_stop_with_nothing_loaded_is_not_a_stop(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    with _run(fake):
        assert mac.stop() is False


def test_uninstall_boots_out_by_domain_and_removes_the_plist(mac):
    _with_plist(mac)
    fake = FakeLaunchctl(session=USER)
    fake.loaded.add(GUI)
    with _run(fake):
        mac.uninstall()
    assert fake.loaded == set()
    assert not mac.PLIST_PATH.exists()
    assert ["launchctl", "bootout", f"{GUI}/{LAUNCHD_LABEL}"] in fake.calls


@pytest.mark.parametrize("stopped", [True, False])
def test_stop_service_propagates_the_launchd_answer(stopped):
    with (
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
        patch.object(svc_macos, "stop", return_value=stopped),
    ):
        assert controller.stop_service() is stopped


# --------------------------------------------------------------------------
# service disable / enable and the login line in status
# --------------------------------------------------------------------------


def test_disable_boots_the_job_out_and_keeps_it_from_loading_at_login(mac):
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        assert "Starts at the next login: yes" in mac.status()
        assert mac.disable() == (True, [])
        status = mac.status()
    assert fake.loaded == set()
    assert fake.disabled == {GUI, USER}
    assert mac.PLIST_PATH.exists()
    assert "Starts at the next login: no (disabled)" in status
    assert "kirocrew service enable" in status


def test_install_after_disable_re_enables_every_domain_before_bootstrap(mac):
    """The trap: a disabled label refuses `bootstrap`, so a reinstall over
    `service disable` must clear the override first, in every domain. Run from
    an SSH shell while someone is at the desktop, so both domains exist; the
    no-desktop case is pinned by the test below."""
    fake = FakeLaunchctl(session=USER)
    with _run(fake):
        mac.install()
        mac.disable()
        mac.install()
    assert fake.loaded == {GUI}
    assert fake.disabled == set()
    strap = max(i for i, c in enumerate(fake.calls) if c[1] == "bootstrap")
    enabled = {c[2] for c in fake.calls[:strap] if c[1] == "enable"}
    assert enabled == {f"{GUI}/{LAUNCHD_LABEL}", f"{USER}/{LAUNCHD_LABEL}"}


def test_install_without_a_desktop_warns_that_the_gui_override_stays(mac, caplog):
    """With nobody at the desktop launchd cannot clear a gui/<uid> override, so
    install lands in user/<uid> and says the gui one is still there."""
    fake = FakeLaunchctl(session=USER, desktop=False)
    fake.disabled.update({GUI, USER})
    with _run(fake), caplog.at_level("WARNING", logger=svc_macos.log.name):
        mac.install()
    assert fake.loaded == {USER}
    assert fake.disabled == {GUI}
    assert any(GUI in r.getMessage() and "not cleared" in r.getMessage() for r in caplog.records)


def test_disable_without_a_desktop_reports_the_gui_domain_it_missed(mac):
    """From SSH the gui/<uid> disable is not recorded, so the agent loads
    there at the next desktop login; disable must say so rather than succeed."""
    fake = FakeLaunchctl(session=USER, desktop=False)
    with _run(fake):
        mac.install()
        assert mac.disable() == (True, [GUI])
    assert fake.loaded == set()
    assert fake.disabled == {USER}


def test_enable_without_a_desktop_reports_the_gui_domain_it_missed(mac):
    fake = FakeLaunchctl(session=USER, desktop=False)
    with _run(fake):
        mac.install()
        mac.disable()
        assert mac.enable() == (True, [GUI])
    assert fake.loaded == {USER}


def test_service_disable_without_a_desktop_is_not_reported_as_lasting(mac, capsys):
    fake = FakeLaunchctl(session=USER, desktop=False)
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        capsys.readouterr()
        rc = controller.disable_service()
    out, err = capsys.readouterr()
    assert rc == 1
    assert "✅" not in out
    assert f"{GUI}: no desktop login" in err
    assert "starts again at the next desktop login" in err
    assert fake.loaded == set()


def test_service_enable_without_a_desktop_warns_about_the_gui_domain(mac, capsys):
    fake = FakeLaunchctl(session=USER, desktop=False)
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        mac.disable()
        capsys.readouterr()
        rc = controller.enable_service()
    out, _err = capsys.readouterr()
    assert rc == 0
    assert "Warning:" in out
    assert f"{GUI}: no desktop login" in out


def test_enable_after_disable_bootstraps_the_agent_again(mac):
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        mac.disable()
        assert mac.enable() == (True, [])
        status = mac.status()
    assert fake.loaded == {GUI}
    assert fake.disabled == set()
    assert "Starts at the next login: yes" in status


def test_enable_on_a_loaded_agent_only_clears_the_override(mac):
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        before = sum(c[1] == "bootstrap" for c in fake.calls)
        assert mac.enable() == (True, [])
    assert sum(c[1] == "bootstrap" for c in fake.calls) == before


def test_restart_after_disable_does_not_load_the_agent(mac):
    """`kirocrew restart` never bootstraps: with the agent disabled and booted
    out it attempts nothing, so the CLI refuses rather than load it again."""
    fake = FakeLaunchctl()
    with (
        _run(fake),
        patch("kiro_crew.service.controller.current_platform", return_value=Platform.LAUNCHD),
    ):
        mac.install()
        mac.disable()
        report = controller.restart_service()
    assert report.attempted is False
    assert fake.loaded == set()


def test_disable_and_enable_without_a_plist_are_none(mac):
    fake = FakeLaunchctl()
    with _run(fake):
        assert mac.disable() is None
        assert mac.enable() is None
    assert fake.calls == []


def test_a_refused_disable_does_not_boot_the_job_out(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    refused = subprocess.CompletedProcess([], 1, "", "Operation not permitted")
    with patch(
        "kiro_crew.service.macos.subprocess.run",
        side_effect=lambda argv, **kw: refused if argv[1] == "disable" else fake(argv, **kw),
    ):
        assert mac.disable()[0] is False
    assert fake.loaded == {GUI}


def test_a_refused_bootout_after_disable_is_not_reported_as_stopped(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    fake.refuse_bootout = True
    with _run(fake):
        assert mac.disable()[0] is False
    assert fake.loaded == {GUI}


def test_enable_right_after_disable_waits_out_the_async_bootout(mac):
    """launchd unloads asynchronously: `disable` waits for the job to leave, so
    an `enable` straight after it bootstraps instead of seeing a job that is
    still on its way out and reporting it started."""
    fake = FakeLaunchctl(linger=3)
    with _run(fake):
        mac.install()
        assert mac.disable() == (True, [])
        assert not fake.leaving
        assert mac.enable() == (True, [])
    assert fake.loaded == {GUI}


@pytest.mark.parametrize("loaded_before", [False, True], ids=["bootstrapped", "already-loaded"])
def test_enable_of_an_agent_that_does_not_run_is_not_a_start(mac, loaded_before):
    """A loaded job whose program exits at once has no pid: enable reports it
    rather than "enabled and started"."""
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        if not loaded_before:
            mac.disable()
        fake.crashing = True
        assert mac.enable() == (False, [])
    assert fake.loaded == {GUI}


def test_enable_right_after_stop_waits_out_the_async_bootout(mac):
    """`kirocrew stop` then `service enable`: the stop waits for the job to
    leave, so enable bootstraps rather than reporting a departing job as
    started and leaving the gateway down."""
    fake = FakeLaunchctl(linger=3)
    with _run(fake):
        mac.install()
        assert mac.stop() is True
        assert not fake.leaving
        assert mac.enable() == (True, [])
    assert fake.loaded == {GUI}


def _refusing(fake, verb, domain):
    refused = subprocess.CompletedProcess([], 1, "", "Operation not permitted")

    def run(argv, **kw):
        if argv[1] == verb and argv[2].startswith(f"{domain}/"):
            return refused
        return fake(argv, **kw)

    return patch("kiro_crew.service.macos.subprocess.run", side_effect=run)


def test_a_refused_enable_write_is_not_a_success(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    fake.disabled.add(GUI)
    with _refusing(fake, "enable", GUI):
        assert mac.enable()[0] is False
    assert fake.disabled == {GUI}


def test_a_disable_refused_in_one_domain_is_not_a_success(mac):
    _with_plist(mac)
    fake = FakeLaunchctl()
    fake.loaded.add(GUI)
    with _refusing(fake, "disable", USER):
        assert mac.disable()[0] is False
    assert fake.loaded == {GUI}


def test_enable_with_an_indeterminate_probe_is_not_a_start(mac):
    """`print` failing with neither "loaded" nor "not found" is not proof the
    agent runs: enable reports failure instead of skipping the bootstrap."""
    _with_plist(mac)
    fake = FakeLaunchctl()
    with _refusing(fake, "print", GUI):
        assert mac.enable()[0] is False
    assert fake.loaded == set()
    assert not any(c[1] == "bootstrap" for c in fake.calls), fake.calls


class _Clock:
    """A clock that moves one second per read and never sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 1.0
        return self.now

    def sleep(self, _secs: float) -> None:
        pass


@pytest.mark.parametrize(("spawn_after", "started"), [(3, True), (30, False)])
def test_enable_rereads_until_the_bootstrapped_agent_shows_a_pid(
    mac, monkeypatch, spawn_after, started
):
    """launchd spawns a job it has just bootstrapped asynchronously, so enable
    re-reads `print` for the settle window: a pid on the fourth read is a
    start, and none within the window is not."""
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        mac.disable()
        fake.spawn_after = spawn_after
        monkeypatch.setattr(
            svc_macos,
            "_START_SETTLE_SECS",
            10.0,  # flake-ok: the injected _Clock never sleeps or reads wall time
        )
        monkeypatch.setattr(svc_macos, "time", _Clock())
        assert mac.enable() == (started, [])
    assert fake.loaded == {GUI}


def test_the_bootout_wait_covers_the_plists_exit_timeout():
    """launchd gives a busy gateway the plist's ExitTimeOut before it kills it;
    a shorter wait would make `service disable` fail on a stop that lands."""
    timeout = plistlib.loads(svc_macos.render_plist().encode())["ExitTimeOut"]
    assert svc_macos._BOOTOUT_SETTLE_SECS > timeout


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("disabled", "Starts at the next login: no (disabled)"),
        ("true", "Starts at the next login: no (disabled)"),
        (None, "Starts at the next login: unknown"),
    ],
    ids=["newer-macos", "older-macos", "print-disabled-fails"],
)
def test_status_reads_both_print_disabled_spellings(mac, spelling, expected):
    fake = FakeLaunchctl()
    with _run(fake):
        mac.install()
        mac.disable()
        fake.disabled_spelling = spelling
        status = mac.status()
    assert expected in status

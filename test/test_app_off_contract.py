"""The third-party execution OFF contract: turning it off REVOKES.

``agent.apps_allow_third_party=false`` means the code the flag was admitting
stops, not merely that new admissions are refused. The setting has three writers
and only one of them sweeps: the dashboard endpoint sweeps on the falling edge,
and the setting is excluded from the generic settings PATCH so no caller reaches
it without that sequencing, while the CLI and a text editor reach the same value
directly. The boot reconcile revokes at the next start, which leaves the case
these tests cover: a backend admitted solely by the blanket flag, still serving
under a ceiling the operator has closed.

Enforcement lives at the one mechanism that already revisits every live backend.
These tests pin that, and pin the three things it must NOT do: touch an app
holding its own grant, touch a builtin, or flood the audit trail with one
admission row per poll.
"""

from __future__ import annotations

import inspect
import json
import sys
from typing import Any

import pytest

from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _read_installed,
    _write_installed,
    install_app,
)

APP = "off-contract-app"


def _install(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, name: str = APP) -> None:
    """Install *name* into a scratch home so it has a real installed record."""
    home = tmp_path / "kirocrew-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    source = tmp_path / "source" / name
    source.mkdir(parents=True)
    (source / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": "Off Contract App",
                "description": "Exercises the OFF contract",
                "author": "tester",
            }
        ),
        encoding="utf-8",
    )
    assert install_app(source).ok
    meta = _read_installed(name)
    assert meta is not None
    meta.enabled = True
    meta.origin = "registry"
    _write_installed(name, meta)


def _write_config(**agent: Any) -> None:
    """Write config.json the way a text editor or the CLI would.

    Deliberately NOT a monkeypatch of the loader: the whole point of the gap is
    that this route never passes through the dashboard endpoint, so a test that
    patched the loaded value would not exercise the route that was broken.
    """
    from kiro_crew.config.loader import KiroCrewConfig, config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent": agent}), encoding="utf-8")
    # The loader keys its cache on a (mtime, size, mode) fingerprint, and a test
    # writes both files inside one coarse timestamp tick.
    KiroCrewConfig.load()


class _LiveProc:
    """A backend process that is still running.

    Chosen deliberately: with a LIVE process the sweep would go on to probe health,
    so a test that asserts ``_health_probe`` is never called proves the ceiling is
    enforced BEFORE health is judged. A record with ``proc=None`` and no
    ``adopted_pids`` is a different path — an adopted backend whose PIDs the stop
    refuses to signal — exercised in
    :class:`TestAnUnstoppableAdoptedBackendKeepsBeingRetried`.

    ``pid`` is fabricated, which is why :func:`_tracked` always stubs the signal
    calls. A low PID can belong to a live same-user process, and signalling it
    would be an irreversible side effect on something the suite does not own.
    """

    pid = 4242
    returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def _serving() -> Any:
    """A health probe that answered, in the PRODUCTION outcome type.

    Deliberately not a bare ``True``. The outcome is a dataclass, so every instance is
    truthy, and a caller that reads the returned object instead of its ``healthy`` field
    still passes against a bool stub while treating every port in production as alive.
    Stubbing the real type is what makes that mistake fail here.
    """
    from kiro_crew.apps.backend import HealthProbeOutcome

    return HealthProbeOutcome.answered(200)


def _silent() -> Any:
    """A health probe that produced no healthy answer, in the production type."""
    from kiro_crew.apps.backend import HealthProbeOutcome

    return HealthProbeOutcome(None, "nothing answered")


def _tracked(monkeypatch: pytest.MonkeyPatch, name: str = APP, *, proc: Any = ...):
    """Register a live backend record for *name* and run sweeps with no delay.

    Returns ``(module, record, signalled)``. ``signalled`` collects the
    ``(pid, signal)`` pairs the stop would have sent. Every signal path is stubbed,
    so no test here can reach a process the suite does not own, and asserting on
    that list says more than asserting the record vanished.
    """
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps.backend import AppProcess

    signalled: list[tuple[int, Any]] = []
    monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
    monkeypatch.setattr(
        bmod.platform_compat,
        "kill_process_tree",
        lambda pid, sig: signalled.append((pid, sig)),
    )
    monkeypatch.setattr(
        bmod.platform_compat,
        "kill_pid_pinned",
        lambda pid, token, sig: bool(signalled.append((pid, sig))) or True,
    )
    ap = AppProcess(
        app_name=name,
        port=9301,
        healthy=True,
        mcp_healthy=True,
        proc=_LiveProc() if proc is ... else proc,
        # What the gateway sets when it starts or adopts a backend, and what makes
        # the ceiling applicable. A record without it is not the sweep's business.
        gateway_started=True,
    )
    with bmod._lock:
        bmod._processes[name] = ap
    return bmod, ap, signalled


class TestAConfigFileEditRevokes:
    """The bypass this change closes."""

    def test_a_file_edit_stops_a_backend_it_no_longer_admits(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=True)
        bmod, ap, signalled = _tracked(monkeypatch)
        try:
            # While the file grants it, the sweep must leave it alone.
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"
            with bmod._lock:
                assert ap.app_name in bmod._processes

            # Now the operator edits the file. No endpoint, no CLI, no restart.
            _write_config(apps_allow_third_party=False)
            # The ordering is asserted by COUNTING probes, not by forbidding them: the
            # stop pays for exactly one, to check nothing outlived the process it
            # signalled. A health JUDGEMENT would probe once per sweep attempt and
            # would demote rather than signal, so one probe plus a SIGTERM plus a
            # popped record is only reachable by enforcing the ceiling first.
            probes: list[int] = []
            monkeypatch.setattr(
                bmod,
                "_health_probe",
                lambda port, *_a, **_k: bool(probes.append(port)) or _silent(),
            )
            bmod._watch_backend_health_sweeps(ap, "/health")

            assert probes == [ap.port], "only the stop's own port check may run"
            with bmod._lock:
                assert ap.app_name not in bmod._processes, (
                    "a backend running only on blanket trust must be stopped once "
                    "the file no longer grants it"
                )
            assert signalled == [
                (4242, bmod.platform_compat.SIGTERM)
            ], "the process itself must be signalled, not merely untracked"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_the_apps_own_grant_survives_the_blanket_flag_going_off(
        self, tmp_path, monkeypatch
    ) -> None:
        """A per-app grant is independent of the blanket flag, so it is untouched."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False, apps_trusted=[APP])
        bmod, ap, signalled = _tracked(monkeypatch)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"
            with bmod._lock:
                assert ap.app_name in bmod._processes, "a granted app must not be swept up"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_revocation_is_audited_even_though_the_poll_is_not(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        rows: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.apps.execution.sel",
            lambda: type(
                "_Sel",
                (),
                {"log_api_access": staticmethod(lambda **kw: rows.append(kw))},
            )(),
        )
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
        finally:
            with bmod._lock:
                bmod._processes.clear()
        admissions = [r for r in rows if r.get("operation") == "app_execution_admission"]
        assert (
            len(admissions) == 1
        ), "exactly one row: the poll writes none and the action writes one"
        assert admissions[0]["outcome"] == "denied"
        assert "health_watch_ceiling_revocation" in admissions[0]["resources"]

    def test_a_scrub_that_does_not_land_is_retried_by_name_after_the_stop(
        self, tmp_path, monkeypatch
    ) -> None:
        """`_set_backend_health` advances `mcp_healthy` only on a landed write.

        A transient failure therefore leaves it not-False while the demote still
        reports success, and by then the record is popped, so the identity-gated
        reconcile can never land. The by-name scrub needs no record.
        """
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        # The write does not land: `healthy` moves, `mcp_healthy` does not.
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        scrubbed: list[str] = []
        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.apps.bridges",
            type(
                "_Bridges",
                (),
                {
                    "_deregister_mcp_servers": staticmethod(
                        lambda name: bool(scrubbed.append(name)) or 1
                    )
                },
            ),
        )
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert ap.mcp_healthy is not False, "the reconcile did not land"
            assert scrubbed == [
                APP
            ], "an entry the reconcile could not land must still be scrubbed by name"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_landed_scrub_is_not_repeated(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)

        def _set_health(record, *, healthy):
            record.healthy = healthy
            record.mcp_healthy = healthy
            return True

        monkeypatch.setattr(bmod, "_set_backend_health", _set_health)
        scrubbed: list[str] = []
        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.apps.bridges",
            type(
                "_Bridges",
                (),
                {
                    "_deregister_mcp_servers": staticmethod(
                        lambda name: bool(scrubbed.append(name)) or 1
                    )
                },
            ),
        )
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert ap.mcp_healthy is False
            assert scrubbed == [], "a confirmed scrub needs no second pass"
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestMetadataCannotRemoveAnAppFromTheSweep:
    """The population is what the gateway started, not what the app's file says.

    ``installed.json`` sits in a tree the app can write, so any rule that reads it to
    decide whether the ceiling applies hands the app an escape: delete or corrupt the
    file and the sweep skips a still-running backend. The record's own
    ``gateway_started`` provenance cannot be reached that way.
    """

    def test_a_deleted_installed_record_still_revokes(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.manager import INSTALLED_META_FILENAME
        from kiro_crew.apps.manager import _read_installed as read_installed
        from kiro_crew.apps.manager import app_dir

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        (app_dir(APP) / INSTALLED_META_FILENAME).unlink()
        assert read_installed(APP) is None, "the record must be gone for this test"

        bmod, ap, signalled = _tracked(monkeypatch)
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            ), "deleting its own metadata must not buy an app continued execution"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_corrupt_installed_record_still_revokes(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        meta = app_dir(APP) / INSTALLED_META_FILENAME
        meta.write_text('{"name": "off-contract-app", "enabl', encoding="utf-8")

        bmod, ap, signalled = _tracked(monkeypatch)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_record_the_gateway_did_not_create_is_left_alone(self, tmp_path, monkeypatch) -> None:
        """The flag is set by the spawn alone, so it names what the gateway started."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        ap.gateway_started = False
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"
            assert signalled == []
            with bmod._lock:
                assert ap.app_name in bmod._processes
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestAShadowingAppGetsNoBuiltinExemption:
    """The exemption comes from the path the gate admitted, captured at start.

    Re-deriving it read ``origin`` out of ``installed.json``, which is taken verbatim
    from a file the app can write, so a record shadowing a shipped builtin's name
    could forge ``origin="builtin"`` and keep executing under a closed ceiling.
    """

    def test_a_forged_builtin_origin_does_not_win_the_exemption(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps.manager import _read_installed, _write_installed

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        # The forgery: the app stamps itself first-party in its own record.
        meta = _read_installed(APP)
        assert meta is not None
        meta.origin = "builtin"
        _write_installed(APP, meta)

        bmod, ap, signalled = _tracked(monkeypatch)
        # What the gate actually admitted: a path in the mutable installed tree.
        ap.admitted_builtin = False
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            ), "a forged origin must not buy the builtin exemption"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_record_admitted_from_the_shipped_root_keeps_its_exemption(
        self, tmp_path, monkeypatch
    ) -> None:
        """The captured path is load-bearing in both directions."""
        import kiro_crew.apps.backend as backend_mod
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        shipped_root = tmp_path / "shipped"
        (shipped_root / APP).mkdir(parents=True)
        entry = shipped_root / APP / "backend.py"
        entry.write_text("", encoding="utf-8")
        monkeypatch.setattr(execution, "shipped_builtin_app_root", lambda _n: shipped_root / APP)
        monkeypatch.setattr(backend_mod, "shipped_builtin_app_root", lambda _n: shipped_root / APP)

        bmod, ap, signalled = _tracked(monkeypatch)
        ap.admitted_builtin = True
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"
            ), "shipped code is exempt at the gate on its executed path"
            assert signalled == []
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_record_with_no_vetted_path_is_judged_third_party(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        ap.admitted_builtin = False
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    """The builtin exemption comes from the record's ownership, not the name.

    ``shipped_builtin_app_root`` resolves a root for any name a shipped manifest
    declares, and ``is_builtin_app`` only checks containment in it. A user app
    installed under a shipped builtin's name -- registration stands down and leaves
    the user's record in place -- would otherwise be exempted as first-party and
    keep executing under a closed ceiling.
    """


class TestTheExemptionIsNotReResolved:
    """The classification is read, never recomputed.

    A stored PATH would be resolved against a filesystem the app owns, so replacing
    an entry point with a symlink into the shipped root could win the exemption after
    admission. The re-check therefore consults the boolean the gate decided and
    resolves nothing.
    """

    def test_the_recheck_resolves_no_path(self, tmp_path, monkeypatch) -> None:
        import kiro_crew.apps.backend as backend_mod
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        ap.admitted_builtin = False
        for mod in (execution, backend_mod):
            monkeypatch.setattr(
                mod,
                "shipped_builtin_app_root",
                lambda _n: pytest.fail("the re-check must not re-resolve a shipped root"),
                raising=False,
            )
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_the_poller_takes_a_name_and_offers_no_path(self) -> None:
        """A parameter nobody passes is still a door.

        ``_execution_admission`` resolves an ``app_root`` when it is given one.
        That is right at the gate, which vets the path once before the code runs,
        and wrong at re-check time, where the path is read from a filesystem the
        app can write. The poller form therefore accepts no such parameter: a
        caller cannot pass what the signature does not take. The core keeps it,
        because the gate must still vet a real path.
        """
        from kiro_crew.apps import execution

        poller = inspect.signature(execution.third_party_ceiling_closed)
        assert list(poller.parameters) == ["app_name"]
        core = inspect.signature(execution._execution_admission)
        assert "app_root" in core.parameters


class TestASurvivingDescendantRefusesTheStop:
    """A root that exits cleanly proves nothing about what it forked.

    The signal goes to the process GROUP and the wait watches only the root, so app
    code that ignores SIGTERM in a child, or leaves the group before binding, keeps
    serving its port while the root's exit reads as a successful stop. Under a
    withdrawn ceiling that is the whole failure, so the revocation caller pays for a
    probe and a port that still answers is reported as a REFUSED stop.

    No second signal is sent: the root has been reaped, so its pid may already be
    reused, and the descendant has no recorded identity of its own.
    """

    def test_a_still_served_port_refuses_the_spawned_stop(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        # The forked listener is still answering after the root went away.
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a, **_k: _serving())
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "retry"
            with bmod._lock:
                assert (
                    bmod._processes.get(ap.app_name) is ap
                ), "tracking must survive or the next sweep has nothing to act on"
            assert signalled == [
                (4242, bmod.platform_compat.SIGTERM)
            ], "the reaped root must not be signalled a second time"
        finally:
            with bmod._lock:
                bmod._processes.clear()
                bmod._allocated_ports.clear()

    def test_a_silent_port_still_reports_a_successful_spawned_stop(
        self, tmp_path, monkeypatch
    ) -> None:
        """The probe is the discriminator, so the ordinary case is unchanged."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a, **_k: _silent())
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            with bmod._lock:
                assert ap.app_name not in bmod._processes
        finally:
            with bmod._lock:
                bmod._processes.clear()
                bmod._allocated_ports.clear()


class TestAnUnreadablePolicyFileRevokes:
    """A policy the gateway cannot read is a DENY, not a reason to keep running.

    The fail-closed read was written for an admission, where the cost of an
    unreadable `config.json` is one refused load. Here it also stops running code,
    so it is worth stating why the same answer is still right.

    Sparing a backend whenever the policy cannot be read would make deleting
    `config.json` the one operator action guaranteed to stop nothing, which is the
    fail-open this whole contract exists to remove. It is also the opposite of the
    `installed.json` case: that file is the APP's, so its absence is an attack and
    must not spare anything, while this file is the OPERATOR's, so its absence is a
    policy signal and must be honoured as deny.

    The cost is availability, it is bounded, and it is disclosed: a genuine
    transient fault stops third-party backends for that sweep, and they return at
    the next gateway start.
    """

    def test_a_missing_config_revokes_a_running_backend(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.config.loader import KiroCrewConfig, config_path

        _install(tmp_path, monkeypatch)
        # Granted, running, and admitted -- the state the revocation must not reach
        # while the policy still says yes.
        _write_config(apps_allow_third_party=True)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        assert bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"

        # Now the policy file goes away under the running gateway. The loader keys its
        # cache on a fingerprint that includes a missing-file sentinel, so this is a
        # cache MISS rather than a stale hit, and the load falls back to defaults.
        config_path().unlink()
        KiroCrewConfig.load()
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestASuccessorKeepsItsMcpRegistration:
    """The by-name scrub cannot tell a stale entry from a successor's live one.

    A re-enable racing the revocation registers a replacement under the same name;
    removing its entry would leave a running backend with no reachable tools.
    """

    def _bridges(self, monkeypatch, scrubbed):
        monkeypatch.setitem(
            sys.modules,
            "kiro_crew.apps.bridges",
            type(
                "_Bridges",
                (),
                {
                    "_deregister_mcp_servers": staticmethod(
                        lambda name: bool(scrubbed.append(name)) or 1
                    )
                },
            ),
        )

    def test_a_tracked_successor_is_left_alone(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.backend import AppProcess

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        scrubbed: list[str] = []
        self._bridges(monkeypatch, scrubbed)
        successor = AppProcess(app_name=APP, port=9302, gateway_started=True)

        real_stop = bmod.stop_app_backend

        def _stop(name, *, _expected=None, _retry_if_serving=None):
            result = real_stop(name, _expected=_expected, _retry_if_serving=_retry_if_serving)
            # A re-enable wins the race and registers its replacement.
            with bmod._lock:
                bmod._processes[name] = successor
            return result

        monkeypatch.setattr(bmod, "stop_app_backend", _stop)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert scrubbed == [], "the successor owns the registration"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_starting_placeholder_does_not_own_the_registration(
        self, tmp_path, monkeypatch
    ) -> None:
        """A placeholder claims the NAME before the spawn, so it owns no entry yet.

        Treating it as the owner is unrecoverable rather than merely late: a start
        that fails removes the placeholder, so no record is left for a later sweep
        to act on, and this record's dead url stays in `mcp.json` with nothing that
        would ever scrub it.
        """
        from kiro_crew.apps.backend import AppProcess

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        scrubbed: list[str] = []
        self._bridges(monkeypatch, scrubbed)
        placeholder = AppProcess(app_name=APP, starting=True)

        real_stop = bmod.stop_app_backend

        def _stop(name, *, _expected=None, _retry_if_serving=None):
            result = real_stop(name, _expected=_expected, _retry_if_serving=_retry_if_serving)
            # A re-enable claims the name, but its spawn has not registered anything.
            with bmod._lock:
                bmod._processes[name] = placeholder
            return result

        monkeypatch.setattr(bmod, "stop_app_backend", _stop)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert scrubbed == [APP], "a placeholder owns no entry to protect"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_with_no_successor_the_entry_is_scrubbed(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda record, *, healthy: True)
        scrubbed: list[str] = []
        self._bridges(monkeypatch, scrubbed)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            assert scrubbed == [APP]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestTrustRestoredDuringTheWindowIsRespected:
    """The audited answer must be the one acted on.

    The poll and the gate call are two separate reads of the config, and an
    operator can turn the flag back on between them. Acting on the poll alone
    would stop a backend whose trust was just restored, and write an ``allowed``
    audit row for the stop.
    """

    def test_a_flag_flipped_back_on_between_the_two_reads_stops_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)

        # The poll sees a closed ceiling; the operator restores trust; the gate
        # call that follows sees it open.
        reads: list[str] = []

        def _poll(name, *, app_root=None):
            reads.append("poll")
            _write_config(apps_allow_third_party=True)
            return "closed at poll time"

        monkeypatch.setattr(bmod, "third_party_ceiling_closed", _poll)
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed"
            ), "a restored flag must not be stopped on a stale poll"
            assert reads == ["poll"]
            assert signalled == [], "nothing may be signalled"
            with bmod._lock:
                assert ap.app_name in bmod._processes
            assert execution.third_party_ceiling_closed(APP) is None
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestARemovedGrantAlsoStopsTheBackend:
    """Declared behaviour, not a side effect.

    The check is level-triggered on the ceiling rather than edge-triggered on one
    setting, so deleting an app's own grant from ``config.json`` while the blanket
    flag is off stops its backend too. ``app-platform-trust-model.md`` states this;
    the test is what keeps the two in step.
    """

    def test_deleting_the_grant_revokes_the_running_backend(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False, apps_trusted=[APP])
        bmod, ap, signalled = _tracked(monkeypatch)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "proceed", "the grant stands"

            _write_config(apps_allow_third_party=False, apps_trusted=[])
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            ), "an app the gateway would refuse to load must not keep running"
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestTheMcpEntryIsScrubbedBeforeTheStop:
    """A revocation must not strand a dead MCP url.

    ``stop_app_backend`` does no MCP work, and returning from the sweep skips the
    exited-backend branch that is the only other place an entry is reconciled. An
    entry left pointing at a dead port breaks every kiro session until the next
    boot reconcile, so the scrub happens here, while the record is still tracked.
    """

    def test_the_registration_is_reconciled_while_the_record_is_still_tracked(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        order: list[str] = []

        def _set_health(record, *, healthy):
            # Tracked at this point is the whole point: the reconcile is identity
            # gated, so a scrub after the pop cannot land.
            with bmod._lock:
                tracked = bmod._processes.get(record.app_name) is record
            order.append(f"set_health healthy={healthy} tracked={tracked}")
            record.healthy = healthy
            record.mcp_healthy = healthy
            return True

        real_stop = bmod.stop_app_backend

        def _stop(name, *, _expected=None, _retry_if_serving=None):
            order.append("stop")
            return real_stop(name, _expected=_expected, _retry_if_serving=_retry_if_serving)

        monkeypatch.setattr(bmod, "_set_backend_health", _set_health)
        monkeypatch.setattr(bmod, "stop_app_backend", _stop)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
        finally:
            with bmod._lock:
                bmod._processes.clear()

        assert order == [
            "set_health healthy=False tracked=True",
            "stop",
        ], "the MCP entry must be reconciled before the record is popped"

    def test_an_unlanded_entry_is_reconciled_even_when_already_unhealthy(
        self, tmp_path, monkeypatch
    ) -> None:
        """`mcp_healthy` is tri-state: None is unknown, not "nothing to unwind"."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        ap.healthy = False
        ap.mcp_healthy = None
        reconciled: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_set_backend_health",
            lambda record, *, healthy: reconciled.append(healthy) or True,
        )
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
        finally:
            with bmod._lock:
                bmod._processes.clear()
        assert reconciled == [False], "an unknown entry must still be unwound"


class TestAnAdoptedBackendThatSurvivesTheStopIsRetried:
    """The watch must not exit while un-trusted code is very likely still serving.

    When an external supervisor replaces an adopted backend's PID, no recorded PID
    matches its adoption identity, so nothing can be signalled. ``stop_app_backend``
    reports that the way it reports its two sibling refusals -- restore tracking,
    return False -- and the watch keeps sweeping instead of exiting on a success it
    did not get.
    """

    def _adopted_with_replaced_pid(self, monkeypatch, bmod, ap):
        ap.adopted_pids = [4242]
        ap.adopted_start_times = {4242: "stale-token"}
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "live-token")
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: True)

    def test_nothing_signalled_keeps_tracking_and_keeps_watching(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        # proc=None plus recorded PIDs is the adopted shape.
        bmod, ap, signalled = _tracked(monkeypatch, proc=None)
        self._adopted_with_replaced_pid(monkeypatch, bmod, ap)
        # The port still answers: the supervisor replaced the process rather than
        # the app having exited, so the default reading would be wrong here.
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a, **_k: _serving())
        rebound: list[str] = []
        monkeypatch.setattr(
            bmod,
            "_rebind_adopted_owners",
            lambda record, path: bool(rebound.append(path)) or True,
        )
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "retry"
            ), "a backend still answering must keep the watch alive"
            with bmod._lock:
                assert (
                    bmod._processes.get(ap.app_name) is ap
                ), "tracking must survive or the next sweep exits immediately"
            assert rebound == ["/health"], "the owners must be re-bound for the retry"
            assert signalled == [], "no recorded PID confirmed identity, so none is signalled"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_the_probe_is_opt_in_so_an_ordinary_stop_is_unchanged(
        self, tmp_path, monkeypatch
    ) -> None:
        """The default reading stays "exited, PID recycled" -- a committed contract.

        `TestStopAdoptedBackend` pins it, and only a caller enforcing a withdrawn
        ceiling asks for the stricter one by paying for a probe.
        """
        _install(tmp_path, monkeypatch)
        bmod, ap, signalled = _tracked(monkeypatch, proc=None)
        self._adopted_with_replaced_pid(monkeypatch, bmod, ap)
        probes: list[int] = []
        monkeypatch.setattr(
            bmod, "_health_probe", lambda port, *_a, **_k: bool(probes.append(port)) or _serving()
        )
        try:
            assert (
                bmod.stop_app_backend(ap.app_name, _expected=ap) is True
            ), "without a health path the default reading is unchanged"
            assert probes == [], "and no probe is paid for"
            with bmod._lock:
                assert ap.app_name not in bmod._processes
        finally:
            with bmod._lock:
                bmod._processes.clear()
                bmod._allocated_ports.clear()

    def test_a_dead_port_still_reads_as_a_successful_stop(self, tmp_path, monkeypatch) -> None:
        """The probe is the discriminator, so a silent port keeps the default answer."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch, proc=None)
        self._adopted_with_replaced_pid(monkeypatch, bmod, ap)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a, **_k: _silent())
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            with bmod._lock:
                assert ap.app_name not in bmod._processes
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_confirmed_pid_is_signalled_and_the_watch_ends(self, tmp_path, monkeypatch) -> None:
        """The identity check is what separates the two outcomes."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch, proc=None)
        ap.adopted_pids = [4242]
        ap.adopted_start_times = {4242: "live-token"}
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "live-token")
        # Dies on SIGTERM, so no SIGKILL escalation muddies the record.
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)
        try:
            assert bmod._revoke_if_ceiling_closed(ap, "/health") == "stopped"
            with bmod._lock:
                assert ap.app_name not in bmod._processes
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestAnUnstoppableAdoptedBackendKeepsBeingRetried:
    """The watch must not exit when the stop refused and put the record back.

    `stop_app_backend` restores tracking for an adopted backend whose PIDs it will
    not signal, precisely so the stop can be retried. Exiting on that would abandon
    the worst case: un-trusted code still serving, with nothing retrying the
    revocation and nothing watching its liveness.
    """

    def test_a_refused_stop_leaves_the_watch_running(self, tmp_path, monkeypatch) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        # proc=None with no adopted_pids is the record whose stop is refused.
        bmod, ap, signalled = _tracked(monkeypatch, proc=None)
        try:
            assert (
                bmod._revoke_if_ceiling_closed(ap, "/health") == "retry"
            ), "a refused stop must keep the watch alive so it retries"
            with bmod._lock:
                assert (
                    bmod._processes.get(ap.app_name) is ap
                ), "stop_app_backend restored tracking; the watch must respect that"
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestTheStartupWindowIsNotAHole:
    """The startup poll owns the record before the standing watch exists.

    A spawn polls for up to `_HEALTH_CHECK_RETRIES * _HEALTH_CHECK_INTERVAL` seconds
    and the watch only takes over afterwards, then sleeps its own first interval. An
    operator closing the ceiling inside that window is an ordinary race, and without
    a check here the poll would keep going and could still promote -- the step that
    writes the app's url into mcp.json -- under a ceiling already closed.
    """

    def test_a_ceiling_closed_mid_startup_stops_the_spawn_before_it_is_promoted(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        bmod, ap, signalled = _tracked(monkeypatch)
        # A record mid-startup: spawned, not yet promoted.
        ap.healthy = False
        ap.mcp_healthy = None
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        # Counted rather than forbidden: the stop pays for one port check. The startup
        # poll would probe once per attempt and could then PROMOTE, so a single probe
        # alongside a SIGTERM and a popped record is only reachable by reading the
        # ceiling first.
        probes: list[int] = []
        monkeypatch.setattr(
            bmod,
            "_health_probe",
            lambda port, *_a, **_k: bool(probes.append(port)) or _silent(),
        )
        try:
            assert (
                bmod._health_check_loop(ap, "/health") is None
            ), "a poll under a closed ceiling must not report a promotion"
            assert probes == [ap.port], "only the stop's own port check may run"
            with bmod._lock:
                assert ap.app_name not in bmod._processes, (
                    "the spawn must be stopped inside the startup window, not left "
                    "running until the watch takes over"
                )
            assert signalled == [(4242, bmod.platform_compat.SIGTERM)]
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_an_open_ceiling_still_lets_the_startup_poll_promote(
        self, tmp_path, monkeypatch
    ) -> None:
        """The check must not become a blanket refusal to ever come up."""
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=True)
        bmod, ap, signalled = _tracked(monkeypatch)
        ap.healthy = False
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a, **_k: _serving())
        monkeypatch.setattr(bmod, "_set_backend_health", lambda _ap, **_k: True)
        try:
            assert bmod._health_check_loop(ap, "/health") is ap
            assert signalled == []
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestAdoptionCannotInheritTheBuiltinExemption:
    """An adopted listener's provenance is unverified, so it is not exempt.

    The builtin classification is sound only for a process the GATEWAY launched
    from the path the gate vetted. Adoption launches nothing -- it finds something
    already answering the port and takes it over -- so carrying the exemption
    across would let anything that answers a builtin's port inherit shipped
    provenance and be skipped by the sweep for good: the next boot re-probes and
    re-adopts to the same verdict, so it would never self-correct.

    An AST ratchet rather than a behavioural test, for the same reason the
    lifecycle-owner ratchet is one: the defect is a single keyword at a
    construction site, and it must stay wrong-proof against an edit that never
    runs the adoption path in a test.
    """

    def test_the_adoption_site_does_not_pass_the_vetted_path_classification(self) -> None:
        import ast
        import inspect

        import kiro_crew.apps.backend as bmod

        tree = ast.parse(inspect.getsource(bmod))
        adoption_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AppProcess"
            and any(kw.arg == "adopted_pids" for kw in node.keywords)
        ]
        assert adoption_sites, "the adoption construction moved; re-anchor this ratchet"
        for site in adoption_sites:
            classification = [kw for kw in site.keywords if kw.arg == "admitted_builtin"]
            assert classification, (
                "the adoption site must state the classification explicitly rather "
                "than inherit a default that could change"
            )
            value = classification[0].value
            assert isinstance(value, ast.Constant) and value.value is False, (
                "an adopted backend's provenance is unverified, so it must not carry "
                "the builtin exemption the revocation sweep honours"
            )

    def test_both_production_sites_state_provenance_explicitly(self) -> None:
        """The sweep's population and exemption are decided at CONSTRUCTION.

        Deleting ``gateway_started=True`` silently empties the population the sweep
        acts on, and changing the spawned site's ``admitted_builtin`` silently
        exempts or condemns every app. Neither is visible to a behavioural test that
        builds its own records, so both sites are pinned here at the source.
        """
        import ast
        import inspect

        import kiro_crew.apps.backend as bmod

        tree = ast.parse(inspect.getsource(bmod))
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AppProcess"
            and any(kw.arg == "gateway_started" for kw in node.keywords)
        ]
        assert len(sites) == 2, (
            "exactly two sites create a gateway-started record (spawn and adoption); "
            f"found {len(sites)} -- a new one needs its own provenance decision"
        )
        classifications = []
        for site in sites:
            started = [kw for kw in site.keywords if kw.arg == "gateway_started"][0]
            assert (
                isinstance(started.value, ast.Constant) and started.value.value is True
            ), "a record the gateway created must say so, or the sweep skips it"
            admitted = [kw for kw in site.keywords if kw.arg == "admitted_builtin"]
            assert admitted, "every gateway-started record must state its exemption"
            classifications.append(admitted[0].value)

        shapes = sorted(
            (
                "False"
                if isinstance(v, ast.Constant) and v.value is False
                else v.id if isinstance(v, ast.Name) else "other"
            )
            for v in classifications
        )
        assert shapes == ["False", "_admitted_builtin"], (
            "one site adopts and must pass False; the other spawns and must pass the "
            f"verdict the gate reached on the vetted path; found {shapes}"
        )

    def test_no_shipped_builtin_can_reach_the_adoption_path(self) -> None:
        """The blast radius of the rule above, pinned so it cannot widen silently.

        Losing the exemption means a revoked adopted backend is not respawned until
        the next gateway start. That is acceptable only because no shipped builtin
        is adoptable: adoption is gated on a manifest declaring a CONCRETE port, and
        every shipped builtin declares ``auto`` or omits the key, which defaults to
        ``auto``. A builtin that later declares a fixed port would take that
        downtime, so it fails here instead and its exemption gets revisited.
        """
        import json
        from pathlib import Path

        import kiro_crew.apps.execution as emod

        builtins_dir = Path(emod.__file__).parent / "builtins"
        declared: dict[str, str] = {}
        for app_json in sorted(builtins_dir.glob("*/app.json")):
            try:
                spec = json.loads(app_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):  # a malformed builtin is another test's job
                continue
            backend = spec.get("backend") or {}
            if not backend:
                continue
            declared[app_json.parent.name] = str(backend.get("port", "auto"))

        assert declared, "no builtin backends found; re-anchor this ratchet"
        fixed = {n: p for n, p in declared.items() if p != "auto"}
        assert not fixed, (
            "these builtins declare a concrete port, so their backends become "
            f"adoptable and lose the builtin exemption on revocation: {fixed}"
        )


class TestARefusedStopIsNeverRePromoted:
    """The flap a bare fall-through would leave open.

    The revocation demotes the record, so from the next sweep on ``was_healthy`` is
    False. If the sweep were allowed to judge health while the stop keeps being
    refused, the port an external supervisor holds open would read as a RECOVERY and
    promote -- putting back in ``mcp.json`` the tools the revocation just scrubbed,
    every other sweep, while the operator believes the trust is withdrawn.
    """

    def test_a_still_serving_revoked_backend_is_never_judged_healthy_again(
        self, tmp_path, monkeypatch
    ) -> None:
        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        # proc=None with no adopted_pids: the stop is refused, tracking restored.
        bmod, ap, _signalled = _tracked(monkeypatch, proc=None)

        class _Stop(Exception):
            """Bounds a loop that is correct to run forever."""

        # Counted at the loop's OWN call, not at `time.sleep`: sleep is global, so a
        # sibling thread's sleep would be miscounted as a sweep.
        real_revoke = bmod._revoke_if_ceiling_closed
        verdicts: list[str] = []

        def _counted(record, path):
            verdict = real_revoke(record, path)
            verdicts.append(verdict)
            if len(verdicts) > 3:
                raise _Stop
            return verdict

        monkeypatch.setattr(bmod, "_revoke_if_ceiling_closed", _counted)
        monkeypatch.setattr(
            bmod,
            "_health_probe",
            lambda *_a, **_k: pytest.fail("health must not be judged while the ceiling is closed"),
        )
        monkeypatch.setattr(
            bmod,
            "_promote",
            lambda *_a, **_k: pytest.fail(
                "a backend under a closed ceiling must never be re-promoted"
            ),
        )
        try:
            with pytest.raises(_Stop):
                bmod._watch_backend_health_sweeps(ap, "/health")
            assert verdicts == ["retry"] * 4, (
                "every sweep must re-assert the closed ceiling and keep retrying "
                "the stop, never reaching a health verdict"
            )
            with bmod._lock:
                assert (
                    bmod._processes.get(ap.app_name) is ap
                ), "the retry needs the record stop_app_backend restored"
            assert ap.healthy is False, "the revocation's demote must stand"
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestThePollerDoesNotFloodTheAuditTrail:
    """Why `third_party_ceiling_closed` exists at all.

    The watch re-asks this about every live backend every `_HEALTH_WATCH_INTERVAL`
    seconds. Routing those polls through `app_execution_denied` would add a row per
    app per sweep whose whole content is "nothing changed", burying the admissions
    the trail exists to show.
    """

    def _rows(self, monkeypatch) -> list[dict]:
        rows: list[dict] = []
        monkeypatch.setattr(
            "kiro_crew.apps.execution.sel",
            lambda: type(
                "_Sel",
                (),
                {"log_api_access": staticmethod(lambda **kw: rows.append(kw))},
            )(),
        )
        return rows

    def test_a_poll_writes_nothing_while_the_gate_writes_a_row(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps.execution import (
            app_execution_denied,
            third_party_ceiling_closed,
        )

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=True)
        rows = self._rows(monkeypatch)

        for _ in range(5):
            assert third_party_ceiling_closed(APP) is None
        assert rows == [], "a poll is not an admission and must not be recorded"

        assert app_execution_denied(APP, action="module_load") is None
        assert len(rows) == 1, "the gate itself still records every admission"
        assert rows[0]["outcome"] == "allowed"


class TestADeletedRecordCannotBuyImmunityFromRevocation:
    """An app may not spare its own backend by destroying its own metadata.

    ``installed.json`` lives in the app's writable tree, so an app trusted to run
    code can delete it, chmod it, or hold it locked at will. Any rule that reads
    "provenance unresolvable" as "leave it running" is therefore a switch the app
    operates itself -- the same evasion the population rule refuses, by another door.
    """

    def test_a_granted_app_whose_record_is_unreadable_is_still_revoked(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False, apps_trusted=[APP])
        monkeypatch.setattr(
            execution, "repository_bound_grant_denied", lambda *_a, **_k: "unresolved"
        )
        # The record cannot be read -- which an app can arrange for itself.
        monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _n: None, raising=False)
        assert (
            execution.third_party_ceiling_closed(APP) == "unresolved"
        ), "deleting its own metadata must not buy an app a reprieve from the sweep"

    def test_the_poller_still_agrees_with_the_gate_on_that_input(
        self, tmp_path, monkeypatch
    ) -> None:
        """The reason the suppression was removed: both must fail closed alike."""
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False, apps_trusted=[APP])
        monkeypatch.setattr(
            execution, "repository_bound_grant_denied", lambda *_a, **_k: "unresolved"
        )
        monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _n: None, raising=False)
        assert execution.app_execution_denied(APP, action="module_load") is not None
        assert execution.third_party_ceiling_closed(APP) is not None

    def test_a_resolved_mismatch_still_revokes(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False, apps_trusted=[APP])
        monkeypatch.setattr(
            execution, "repository_bound_grant_denied", lambda *_a, **_k: "mismatch"
        )
        assert execution.third_party_ceiling_closed(APP) == "mismatch"

    def test_an_ungranted_app_with_no_record_still_revokes(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.apps import execution

        _install(tmp_path, monkeypatch)
        _write_config(apps_allow_third_party=False)
        monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _n: None, raising=False)
        assert execution.third_party_ceiling_closed(APP) is not None


class TestThePollerAndTheGateCannotDisagree:
    """The divergence ratchet.

    Both read the same `_execution_admission` core, so this asserts the property
    that made that split worth doing: on this boundary a disagreement means a
    backend still executing under a ceiling the operator believes is closed.
    """

    @pytest.mark.parametrize(
        "agent",
        [
            {"apps_allow_third_party": True},
            {"apps_allow_third_party": False},
            {"apps_allow_third_party": False, "apps_trusted": [APP]},
            {"apps_allow_third_party": True, "apps_trusted": [APP]},
            {"apps_allow_third_party": "true"},
            {"apps_allow_third_party": 1},
            {},
        ],
    )
    def test_both_paths_reach_the_same_verdict(self, tmp_path, monkeypatch, agent) -> None:
        from kiro_crew.apps.execution import (
            app_execution_denied,
            third_party_ceiling_closed,
        )

        _install(tmp_path, monkeypatch)
        _write_config(**agent)
        gate = app_execution_denied(APP, action="module_load")
        poll = third_party_ceiling_closed(APP)
        assert poll == gate

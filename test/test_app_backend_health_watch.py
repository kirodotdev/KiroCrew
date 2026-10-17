"""The post-startup health watch over a running app backend.

Liveness, the startup poll's hand-off and generation, stale-record refusals,
the loopback probe's security and its failure reports, and the watch's
survival of a sweep fault.
Fakes come from ``app_backend_helpers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app_backend_helpers import _FakeHealthResp, _FakeProc, _probe_outcome

from kiro_crew.apps.backend import AppProcess

# =============================================================================
# Post-startup liveness watch
# =============================================================================


class TestBackendLivenessWatch:
    """``healthy`` must be able to go back to False.

    Before the watch, the startup poll wrote ``healthy = True`` once and nothing ever
    unwrote it, so a backend that died later kept the reverse proxy routing to its dead
    port and kept ``/api/apps`` reporting it healthy.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        """A tracked, healthy backend plus the seams the watch runs through.

        Yields ``(bmod, ap, probes, gate_calls)``. ``probes`` is the scripted probe
        result list — the watch stops by untracking the record once the script runs out,
        which is the same guard ``stop_app_backend`` exits it through in production.
        """
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        gate_calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (gate_calls.append((name, port, healthy)), True)[1],
        )

        # mcp_healthy=True models the real precondition: a record only reaches the
        # watch after a promotion has already reconciled mcp.json for it.
        ap = AppProcess(
            app_name="watched",
            port=9160,
            pid=4242,
            proc=_FakeProc(),
            healthy=True,
            mcp_healthy=True,
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["watched"] = ap

        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                # Script exhausted: untrack so the watch exits at its top-of-loop guard.
                with bmod._lock:
                    bmod._processes.pop("watched", None)
                return _probe_outcome(False)
            return _probe_outcome(probes.pop(0))

        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes, gate_calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_survives_transient_failures_below_the_threshold(self, watched):
        # A backend that misses one probe and answers the next is briefly busy, not
        # dead: demoting there would take a working app offline.
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES - 1) + [True])

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert gate_calls == []  # never demoted, so the MCP entry was never scrubbed

    def test_demotes_after_consecutive_failures_and_scrubs_mcp(self, watched):
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False
        assert ("watched", 9160, False) in gate_calls

    def test_a_demoted_backend_is_no_longer_routable(self, watched):
        # The load-bearing consequence: get_app_backend_port gates the reverse proxy
        # purely on this flag, so before the watch the proxy kept dialing a dead port.
        bmod, ap, probes, gate_calls = watched
        assert bmod.get_app_backend_port("watched") == 9160

        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES)
        bmod._watch_backend_health(ap, "/health")

        assert bmod.get_app_backend_port("watched") is None

    def test_demotion_is_reversible(self, watched):
        # An app that wedged briefly must heal without operator action.
        bmod, ap, probes, gate_calls = watched
        probes.extend([False] * bmod._HEALTH_WATCH_FAILURES + [True])

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert gate_calls == [("watched", 9160, False), ("watched", 9160, True)]

    def test_an_exited_process_demotes_on_one_observation(self, watched):
        # No threshold: a dead Popen cannot recover, so waiting for three misses would
        # only keep routing to it for longer. The probe list stays untouched, pinning
        # that liveness is answered from the exit status without an HTTP round trip.
        bmod, ap, probes, gate_calls = watched
        ap.proc = _FakeProc(returncode=1)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False
        assert gate_calls == [("watched", 9160, False)]
        assert probes == []  # never consulted the health endpoint

    def test_does_not_demote_a_newer_generation_under_the_same_name(self, watched):
        # A stop/start replaces the record; the old watch must not carry its verdict
        # over to a backend it never observed.
        bmod, ap, probes, gate_calls = watched
        replacement = AppProcess(
            app_name="watched", port=9161, pid=99, proc=_FakeProc(), healthy=True
        )
        with bmod._lock:
            bmod._processes["watched"] = replacement
        ap.proc = _FakeProc(returncode=1)  # the OLD generation is dead

        bmod._watch_backend_health(ap, "/health")

        assert replacement.healthy is True
        assert gate_calls == []
        assert bmod.get_app_backend_port("watched") == 9161

    def test_an_untracked_record_is_never_probed(self, watched):
        # stop_app_backend pops the record; the watch exits before touching the network,
        # so a stale watcher can never probe a port another backend has since taken.
        bmod, ap, probes, gate_calls = watched
        probes.append(True)
        with bmod._lock:
            bmod._processes.pop("watched")

        bmod._watch_backend_health(ap, "/health")

        assert probes == [True]  # untouched
        assert gate_calls == []


class TestBackendRunningReflectsTheProcess:
    """``/api/apps`` must not report an exited backend as running."""

    def test_running_is_false_once_the_process_exits(self):
        ap = AppProcess(app_name="gone", port=9162, pid=7, proc=_FakeProc(returncode=0))
        assert ap.to_dict()["running"] is False

    def test_running_is_true_while_the_process_lives(self):
        ap = AppProcess(app_name="live", port=9163, pid=7, proc=_FakeProc())
        assert ap.to_dict()["running"] is True

    def test_an_adopted_backend_has_no_handle_to_poll(self):
        # proc=None is the adopted shape: the instance belongs to another supervisor, so
        # "we still track it" is the only honest answer and `healthy` carries the signal.
        ap = AppProcess(app_name="adopted", port=9164, pid=0, proc=None, healthy=True)
        assert ap.to_dict()["running"] is True


class TestHealthSupervisorHandoff:
    """The startup poll must hand the watch its record, or the whole watch is dead wiring."""

    def test_watch_runs_when_the_backend_came_up(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        ap = AppProcess(app_name="up", port=9170, healthy=True)
        watched: list[tuple[AppProcess, str]] = []
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: ap)
        monkeypatch.setattr(
            bmod, "_watch_backend_health", lambda rec, path: watched.append((rec, path))
        )

        bmod._supervise_backend_health(ap, "/health")

        assert watched == [(ap, "/health")]

    def test_no_watch_when_the_backend_never_came_up(self, monkeypatch):
        # Nothing to watch: the startup path already scrubbed the MCP entry and gave up.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        watched: list[Any] = []
        monkeypatch.setattr(bmod, "_health_check_loop", lambda *_a, **_k: None)
        monkeypatch.setattr(bmod, "_watch_backend_health", lambda rec, path: watched.append(rec))

        bmod._supervise_backend_health(AppProcess(app_name="down", port=9171), "/health")

        assert watched == []


class TestHealthTransitionsRefuseAStaleRecord:
    """stop_app_backend can land between a probe and its verdict.

    Both transitions re-check identity under the lock, so a verdict formed about a
    record that has since been popped or replaced cannot be written — and cannot move
    the MCP entry of whatever holds the name now.
    """

    @pytest.fixture
    def gate_calls(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_demote_refuses_an_untracked_record(self, gate_calls):
        bmod, calls = gate_calls
        ap = AppProcess(app_name="stale", port=9172, healthy=True)  # never tracked

        bmod._demote(ap, reason="test")

        assert ap.healthy is True  # untouched
        assert calls == []  # no scrub of a name this record does not own

    def test_promote_refuses_an_untracked_record(self, gate_calls):
        bmod, calls = gate_calls
        ap = AppProcess(app_name="stale", port=9173, healthy=False)

        bmod._promote(ap)

        assert ap.healthy is False
        assert calls == []


class TestStartupProbeCannotPromoteAReplacement:
    """The startup poll must promote only the record it actually probed (review finding).

    Resolving the record by name AFTER the probe let a stop/start landing mid-probe hand
    back the successor, which was then marked healthy — and therefore routed to, and MCP
    registered — on evidence gathered about its predecessor, without ever having
    answered a probe itself.
    """

    def test_a_successor_installed_mid_probe_is_not_promoted(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )

        original = AppProcess(app_name="app", port=9190, healthy=False)
        successor = AppProcess(app_name="app", port=9191, healthy=False)

        def _probe_then_swap(_port, _path):
            # The stop/start lands while the probe is in flight.
            with bmod._lock:
                bmod._processes["app"] = successor
            return _probe_outcome(True)

        monkeypatch.setattr(bmod, "_health_probe", _probe_then_swap)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = original
        try:
            result = bmod._health_check_loop(original, "/health")

            assert result is None  # nothing promoted, so no watch is armed either
            assert successor.healthy is False  # never answered a probe of its own
            assert calls == []  # and its MCP entry was not written from a stale port
            assert bmod.get_app_backend_port("app") is None
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestStartupPollBelongsToOneGeneration:
    """The whole startup poll is bound to the record it started on (design review).

    Pinning per ATTEMPT is not enough: a stop/start between attempts hands a later
    attempt the successor, and this poll then acts on it — promoting it, or on
    exhaustion scrubbing it — on evidence gathered entirely about its predecessor,
    having never probed the successor's port.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 4)
        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_exhaustion_does_not_scrub_a_successor_that_replaced_it(self, monkeypatch, wired):
        # The harmful shape the design review named: the retiring poll's terminal scrub
        # deregisters by app name, taking the healthy successor's entry with it — and
        # because that bypasses the record, the successor's mcp_healthy still reads True,
        # so the watch's retry never fires and the live backend stays MCP-less.
        bmod, calls = wired
        original = AppProcess(app_name="a", port=9210, healthy=False)
        successor = AppProcess(app_name="a", port=9211, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes["a"] = original

        swapped = {"done": False}

        def _never_answers(_port, _path):
            if not swapped["done"]:  # the restart lands between attempts
                swapped["done"] = True
                with bmod._lock:
                    bmod._processes["a"] = successor
            return _probe_outcome(False)

        monkeypatch.setattr(bmod, "_health_probe", _never_answers)

        assert bmod._health_check_loop(original, "/health") is None
        assert calls == []  # the successor's `a:` entries survive
        assert successor.mcp_healthy is True
        assert bmod.get_app_backend_port("a") == 9211

    def test_a_successor_is_not_promoted_by_a_later_attempt(self, monkeypatch, wired):
        bmod, calls = wired
        original = AppProcess(app_name="a", port=9212, healthy=False)
        successor = AppProcess(app_name="a", port=9213, healthy=False)
        with bmod._lock:
            bmod._processes["a"] = original

        state = {"n": 0}

        def _probe(_port, _path):
            state["n"] += 1
            if state["n"] == 1:
                with bmod._lock:
                    bmod._processes["a"] = successor
                return _probe_outcome(False)
            # a later attempt would "succeed" against the OLD port
            return _probe_outcome(True)

        monkeypatch.setattr(bmod, "_health_probe", _probe)

        assert bmod._health_check_loop(original, "/health") is None
        assert successor.healthy is False  # never probed on its own port
        assert calls == []


class TestHealthProbeSecurity:
    """App-authored health paths cannot redirect a loopback liveness probe."""

    @pytest.mark.parametrize(
        "path",
        ["/health", "/healthz?verbose=1", "/a/b_c-d.e~f", "/", "//example.com/x"],
    )
    def test_ordinary_paths_preserve_the_loopback_authority(self, path):
        import urllib.parse

        import kiro_crew.apps.backend as bmod

        url = bmod._health_probe_url(9101, path)
        assert url == f"http://127.0.0.1:9101{path}"
        assert urllib.parse.urlsplit(url).hostname == "127.0.0.1"

    @pytest.mark.parametrize(
        "path",
        [
            "@example.com/",
            "health",
            "",
            "/a@b",
            "/x\ny",
            "/x\ty",
            "/x\ry",
            "/x y",
            "http://example.com/",
            "/#frag",
            "/a\\b",
            "/[::1]",
        ],
    )
    def test_authority_smuggling_and_ambiguous_paths_are_refused(self, path):
        import kiro_crew.apps.backend as bmod

        assert bmod._health_probe_url(9101, path) is None

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_impossible_ports_are_refused(self, port):
        import kiro_crew.apps.backend as bmod

        assert bmod._health_probe_url(port, "/health") is None

    def test_the_userinfo_payload_would_otherwise_leave_loopback(self):
        import urllib.parse

        import kiro_crew.apps.backend as bmod

        naive = "http://127.0.0.1:9101@example.com/"
        assert urllib.parse.urlsplit(naive).hostname == "example.com"
        assert bmod._health_probe_url(9101, "@example.com/") is None

    def test_an_invalid_path_never_reaches_http_and_warns_only_once(self, monkeypatch, caplog):
        import logging

        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_warned_health_paths", set())
        monkeypatch.setattr(
            bmod,
            "loopback_urlopen",
            lambda *_a, **_k: pytest.fail("unsafe health path reached HTTP client"),
        )
        with caplog.at_level(logging.WARNING, logger=bmod.logger.name):
            for _ in range(3):
                assert bmod._health_probe(9101, "@example.com/").healthy is False

        warnings = [r.message for r in caplog.records if "health path" in r.message]
        assert len(warnings) == 1
        assert "@example.com/" in warnings[0]

    def test_a_valid_path_uses_the_hardened_opener_with_the_exact_url(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        seen = {}

        def _open(req, *, timeout):
            seen.update(url=req.full_url, timeout=timeout)
            return _FakeHealthResp()

        monkeypatch.setattr(bmod, "loopback_urlopen", _open)
        monkeypatch.setattr(
            bmod.urllib.request,
            "urlopen",
            lambda *_a, **_k: pytest.fail("bare urlopen bypassed loopback policy"),
        )

        outcome = bmod._health_probe(9101, "/health?q=1", timeout=1.5)
        assert (outcome.status, outcome.detail, outcome.healthy) == (200, "HTTP 200", True)
        assert seen == {"url": "http://127.0.0.1:9101/health?q=1", "timeout": 1.5}

    def test_adoption_startup_and_watch_share_the_same_probe_contract(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls = []

        def _probe(port, path, **kwargs):
            calls.append((port, path, kwargs))
            return _probe_outcome(False)

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        assert bmod._probe_adoption_health(9101, "/adopt") is False

        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 1)
        monkeypatch.setattr(bmod, "_set_backend_health", lambda *_a, **_k: True)
        startup = AppProcess(app_name="startup-contract", port=9102)
        with bmod._lock:
            bmod._processes[startup.app_name] = startup
        try:
            assert bmod._health_check_loop(startup, "/startup") is None
        finally:
            with bmod._lock:
                bmod._processes.clear()

        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        watch = AppProcess(app_name="watch-contract", port=9103, healthy=True, mcp_healthy=True)

        def _watch_probe(port, path, **kwargs):
            calls.append((port, path, kwargs))
            with bmod._lock:
                bmod._processes.pop(watch.app_name, None)
            return _probe_outcome(True)

        monkeypatch.setattr(bmod, "_health_probe", _watch_probe)
        with bmod._lock:
            bmod._processes[watch.app_name] = watch
        bmod._watch_backend_health_sweeps(watch, "/watch")

        assert calls == [
            (9101, "/adopt", {"timeout": 3}),
            (9102, "/startup", {}),
            (9103, "/watch", {}),
        ]


class TestProbeSurvivesAMalformedHttpResponse:
    """`http.client.HTTPException` is not an `OSError` (review finding).

    An app backend is arbitrary third-party code — an `exec` backend, or an adopted
    process we do not own — so a non-HTTP first line on the port is a real condition.
    `BadStatusLine` escaping `_health_probe` would kill the standing daemon watch and
    freeze `healthy` at its last value: the write-once behaviour this PR removes,
    silently restored.
    """

    def _serve_garbage(self, payload: bytes) -> int:
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def _serve():
            try:
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.sendall(payload)
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        threading.Thread(target=_serve, daemon=True).start()
        return port

    def test_a_malformed_status_line_is_a_failed_probe_not_a_raise(self):
        # Drives a REAL socket rather than a stubbed exception: the whole finding is
        # about which concrete exception urllib lets through, so faking it would beg
        # the question.
        import kiro_crew.apps.backend as bmod

        port = self._serve_garbage(b"NOT-HTTP garbage\r\n\r\n")

        assert bmod._health_probe(port, "/health").healthy is False

    def test_the_raw_status_line_cannot_add_a_line_to_the_gateway_log(self):
        # The first line off the socket belongs to the app backend, which is arbitrary
        # third-party code, and `BadStatusLine` carries it verbatim. Printed raw into the
        # log it could forge an entry, so the detail escapes it.
        import kiro_crew.apps.backend as bmod

        port = self._serve_garbage(b"NOT-HTTP \x1b[2Jforged WARNING line\r\n")

        detail = bmod._health_probe(port, "/health").detail

        assert "BadStatusLine" in detail
        assert not any(ch in detail for ch in "\r\n\x1b")
        assert "\\x1b" in detail  # escaped, not swallowed

    def test_an_enormous_status_line_does_not_fill_the_log(self):
        # A status line may be 64 KB; one failed probe must not write a screenful.
        import kiro_crew.apps.backend as bmod

        port = self._serve_garbage(b"NOT-HTTP " + b"A" * 5000 + b"\r\n")

        detail = bmod._health_probe(port, "/health").detail

        assert len(detail) < 4 * bmod._PROBE_DETAIL_MAX_CHARS
        assert detail.endswith("...'")

    def test_the_adoption_probe_survives_it_too(self):
        # Same class of bug in the sibling probe — fixed together so one does not sit
        # next to the other still wrong.
        import kiro_crew.apps.backend as bmod

        port = self._serve_garbage(b"\x00\x01binary noise\r\n\r\n")

        assert bmod._probe_adoption_health(port, "/health") is False


class TestAFailedHealthCheckNamesWhatItObserved:
    """A 403, a 404 and a dead port were one indistinguishable log line.

    The probe is deliberately UNSIGNED, so a manifest whose ``healthCheck`` names a route
    behind the backend's own auth can never pass — the app stays unreachable and the only
    evidence was "failed health check after 15 attempts". Reading the status off the wire
    is the whole point, so these drive a REAL socket: ``urlopen`` RAISES on >= 400 instead
    of returning the response, and stubbing that would beg the question.
    """

    @staticmethod
    def _serve_status(status: int, phrase: str, *, answers: int = 1) -> int:
        """Answer *answers* requests with a bare *status*, then close. Returns the port."""
        import socket
        import threading

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(answers + 1)

        def _serve():
            try:
                for _ in range(answers):
                    conn, _ = srv.accept()
                    with conn:
                        conn.recv(4096)
                        conn.sendall(
                            f"HTTP/1.1 {status} {phrase}\r\n"
                            "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                        )
            except OSError:
                pass
            finally:
                srv.close()

        threading.Thread(target=_serve, daemon=True).start()
        return port

    @pytest.fixture
    def exhausts_at_once(self, monkeypatch):
        """One attempt, no sleeping, and a recording MCP gate — nothing else stubbed."""
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 1)
        gate: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda _name, _port, *, healthy: (gate.append(healthy), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, gate
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def _exhaust(self, bmod, port: int, health_path: str, caplog) -> tuple:
        import logging

        ap = AppProcess(app_name="h", port=port, healthy=False)
        with bmod._lock:
            bmod._processes["h"] = ap
        with caplog.at_level(logging.WARNING, logger=bmod.logger.name):
            promoted = bmod._health_check_loop(ap, health_path)
        failure = next(
            r.getMessage() for r in caplog.records if "failed health check" in r.getMessage()
        )
        return ap, promoted, failure

    def test_an_auth_gated_health_path_names_its_403_and_the_way_out(
        self, exhausts_at_once, caplog
    ):
        # The reported shape: the backend is up and answering, the probe just cannot
        # authenticate, so the log has to say which of the two it is.
        bmod, gate = exhausts_at_once
        port = self._serve_status(403, "Forbidden")

        ap, promoted, failure = self._exhaust(bmod, port, "/api/workspaces", caplog)

        assert "HTTP 403" in failure
        assert "usually the healthCheck path is behind auth" in failure
        assert "point backend.healthCheck at an unauthenticated route" in failure
        assert promoted is None and ap.healthy is False
        assert gate == [False]  # scrubbed, never promoted

    def test_a_missing_handler_names_its_404_without_the_auth_hint(self, exhausts_at_once, caplog):
        # The other half of the same misconfiguration — a path nobody serves. Naming the
        # auth fix here would send the reader after the wrong thing.
        bmod, gate = exhausts_at_once
        port = self._serve_status(404, "Not Found")

        ap, promoted, failure = self._exhaust(bmod, port, "/health", caplog)

        assert "HTTP 404" in failure
        assert "unauthenticated route" not in failure
        assert promoted is None and ap.healthy is False
        assert gate == [False]

    def test_a_redirect_is_unhealthy_even_though_its_status_is_below_400(
        self, exhausts_at_once, caplog
    ):
        # `loopback_urlopen` REFUSES redirects so the secret is never replayed, which
        # makes a 302 arrive as an HTTPError whose code reads like success. Nothing served
        # the health check, so promoting on the number would route the proxy at a backend
        # that never answered — and adoption would bind ownership to whatever holds the
        # port.
        bmod, gate = exhausts_at_once
        port = self._serve_status(302, "Found")

        ap, promoted, failure = self._exhaust(bmod, port, "/health", caplog)

        assert "HTTP 302" in failure
        assert "does not follow redirects" in failure
        assert promoted is None and ap.healthy is False
        assert gate == [False]

    def test_a_dead_port_says_so_instead_of_naming_a_status(self, exhausts_at_once, caplog):
        bmod, _gate = exhausts_at_once
        dead = bmod._find_free_port()

        ap, promoted, failure = self._exhaust(bmod, dead, "/health", caplog)

        assert "connection refused" in failure
        assert "HTTP" not in failure and "unauthenticated route" not in failure
        assert promoted is None and ap.healthy is False

    def test_a_healthy_backend_still_promotes_on_its_status(self, exhausts_at_once, caplog):
        # The verdict is unchanged: below 400 is healthy, read off the same outcome.
        bmod, gate = exhausts_at_once
        port = self._serve_status(204, "No Content")
        ap = AppProcess(app_name="h", port=port, healthy=False)
        with bmod._lock:
            bmod._processes["h"] = ap

        assert bmod._health_check_loop(ap, "/health") is ap
        assert ap.healthy is True
        assert gate == [True]

    def test_the_standing_watch_names_the_status_in_its_demotion(self, monkeypatch, caplog):
        # The sibling branch: a backend that goes auth-gated AFTER promotion is demoted
        # by the watch, whose reason was equally silent about why.
        import logging

        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda *_a, **_k: True)
        sweeps = {"n": 0}

        def _probe(_port, _path):
            sweeps["n"] += 1
            if sweeps["n"] > bmod._HEALTH_WATCH_FAILURES:
                with bmod._lock:  # demotion recorded — let the watch leave
                    bmod._processes.pop("d", None)
            return bmod.HealthProbeOutcome.refused(403)

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        ap = AppProcess(app_name="d", port=9301, proc=_FakeProc(), healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["d"] = ap
        try:
            with caplog.at_level(logging.WARNING, logger=bmod.logger.name):
                bmod._watch_backend_health_sweeps(ap, "/api/workspaces")
        finally:
            with bmod._lock:
                bmod._processes.clear()

        demotion = next(
            r.getMessage() for r in caplog.records if "went unhealthy" in r.getMessage()
        )
        assert "HTTP 403" in demotion
        assert "point backend.healthCheck at an unauthenticated route" in demotion
        assert ap.healthy is False


class TestWatchSurvivesAnUnexpectedSweepFault:
    """A dying watch silently restores write-once, so a sweep fault must not kill it."""

    def test_the_watch_restarts_after_an_unexpected_exception(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True)
        ap = AppProcess(app_name="w", port=9260, proc=_FakeProc(), healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap

        calls = {"n": 0}

        def _probe(_port, _path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("something nobody anticipated")
            with bmod._lock:  # second sweep: end the watch cleanly
                bmod._processes.pop("w", None)
            return _probe_outcome(True)

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        try:
            bmod._watch_backend_health(ap, "/health")
            assert calls["n"] == 2  # survived the fault and swept again
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_it_does_not_restart_once_the_record_is_gone(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(app_name="w", port=9261, proc=_FakeProc(), healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap

        calls = {"n": 0}

        def _probe(_port, _path):
            calls["n"] += 1
            with bmod._lock:
                bmod._processes.pop("w", None)
            raise RuntimeError("faults while being torn down")

        monkeypatch.setattr(bmod, "_health_probe", _probe)
        try:
            bmod._watch_backend_health(ap, "/health")
            assert calls["n"] == 1  # did not spin on a record nobody tracks
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestSupervisorIsBoundAtSpawn:
    """The supervisor carries its record, not a name to re-resolve (review finding).

    A name and a port are two independent inputs that can disagree. A stop/restart
    landing between the spawn inserting the record and the supervisor thread's first
    statement would hand a name lookup the SUCCESSOR while the port argument still named
    the predecessor — and the poll would then promote, or on exhaustion scrub, a backend
    whose port it never probed.
    """

    def test_the_starter_hands_over_the_record_itself(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        captured: list[object] = []
        monkeypatch.setattr(
            bmod.threading,
            "Thread",
            lambda **kw: SimpleNamespace(start=lambda: captured.append(kw["args"])),
        )
        ap = AppProcess(app_name="s", port=9290)

        bmod._start_health_supervisor(ap, "/health")

        assert captured == [(ap, "/health")]  # the record, not ("s", 9290)

    def test_a_restart_before_the_thread_runs_cannot_divert_the_poll(self, monkeypatch):
        # Simulates the window: the successor is installed before the supervisor's first
        # statement. Binding to the record means the poll retires instead of acting on a
        # generation it never probed.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        gate: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (gate.append((name, port, healthy)), True)[1],
        )
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: _probe_outcome(True))

        original = AppProcess(app_name="s", port=9291, healthy=False)
        successor = AppProcess(app_name="s", port=9292, healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["s"] = successor  # the restart already landed
        try:
            bmod._supervise_backend_health(original, "/health")
            assert successor.healthy is False  # never promoted on the old port's evidence
            assert gate == []
        finally:
            with bmod._lock:
                bmod._processes.clear()

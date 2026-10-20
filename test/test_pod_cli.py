"""The pod CLI: audit events, ``ls``, ``status``, ``url``, ``install``, dispatch and ``pod up``.

``pod up`` runs end to end with the host boundary mocked. The fixtures come from
``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
    cfg_fixture,
    requires_posix_pod_lifecycle,
)

from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


class TestAuditEvents:
    @requires_posix_pod_lifecycle
    def test_down_emits_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("pod.down" in str(r) for r in recorded)

    @requires_posix_pod_lifecycle
    def test_down_dies_when_stop_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: recorded.append((a, k)))
        monkeypatch.setattr(rt, "is_active", lambda cfg, name: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1, stderr="bus error"))
        with pytest.raises(SystemExit):
            pod_cli._down(PodConfig.load(), argparse.Namespace(name="demo"))
        assert any("failure" in str(r) for r in recorded)

    def test_token_emits_audit_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        monkeypatch.setattr(rt, "mint_token", lambda cfg, name, ttl: "tok-abc")
        pod_cli._token(PodConfig.load(), argparse.Namespace(name="demo", ttl="2h"))
        assert ("pod.token", "allowed") in recorded

    def test_audit_failure_is_logged_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        def _boom(*a: object, **k: object) -> None:
            raise RuntimeError("sel down")

        monkeypatch.setattr(pod_cli, "sel", lambda: type("S", (), {"log_api_access": _boom})())
        with caplog.at_level(logging.WARNING):
            pod_cli._audit("pod.up", "allowed", "name=x")  # must NOT raise
        assert any("SEL audit failed for pod.up" in r.message for r in caplog.records)


class TestCliVerbs:
    def test_ls_empty(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: set())
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "no pods running" in capsys.readouterr().out

    def test_ls_table_and_json(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(rt, "active_names", lambda c: {"alpha"})
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "health", lambda cfg, name, p: 403)
        pod_cli._ls(cfg, argparse.Namespace(json=False))
        assert "alpha" in capsys.readouterr().out
        pod_cli._ls(cfg, argparse.Namespace(json=True))
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"health": 403' in out

    def test_status(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda c, n: True)
        monkeypatch.setattr(rt, "health", lambda cfg, name, p: 403)
        pod_cli._status(cfg, argparse.Namespace(name="alpha", json=True))
        out = capsys.readouterr().out
        assert '"status": "up"' in out and '"health": 403' in out

    def test_status_on_linux_refuses_when_the_backend_is_absent(
        self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """With no user manager, `is-active` fails and would read as "down"; the
        verb must refuse through the backend gate instead of reporting a state."""

        def _absent() -> None:
            raise rt.PodBackendAbsent("no systemd user session on this host")

        monkeypatch.setattr(rt, "IS_LINUX", True)
        monkeypatch.setattr(rt, "require_backend", _absent)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda c, n: False)
        monkeypatch.setattr(rt, "health", lambda cfg, name, p: 0)

        with pytest.raises(rt.PodError, match="no systemd user session"):
            pod_cli._status(cfg, argparse.Namespace(name="alpha", json=True))
        assert "down" not in capsys.readouterr().out

    def test_url(self, cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        pod_cli._url(cfg, argparse.Namespace(name="alpha"))
        assert "http://127.0.0.1:7811" in capsys.readouterr().out

    @requires_posix_pod_lifecycle
    def test_install_writes_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # This test asserts the Linux install path, so satisfy the verb-entry
        # backend gate. The low-level systemctl stub below owns reload behavior.
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        c = PodConfig.load()
        pod_cli._install(c, argparse.Namespace())
        dst = tmp_path / ".config" / "systemd" / "user" / f"{c.unit_prefix}@.service"
        assert dst.exists() and "pod _run %i" in dst.read_text(encoding="utf-8")
        assert "daemon-reload OK" in capsys.readouterr().out
        assert ("pod.install", "allowed") in recorded

    @requires_posix_pod_lifecycle
    def test_install_dies_on_reload_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=1))
        recorded: list[tuple] = []
        monkeypatch.setattr(
            pod_cli, "_audit", lambda op, outcome, *a, **k: recorded.append((op, outcome))
        )
        with pytest.raises(SystemExit):
            pod_cli._install(PodConfig.load(), argparse.Namespace())
        assert ("pod.install", "failure") in recorded

    def test_dispatch_unknown_verb_exits(self) -> None:
        with pytest.raises(SystemExit):
            pod_cli.dispatch(argparse.Namespace(pod_action=None))


@requires_posix_pod_lifecycle
class TestUpVerb:
    """Drive the big _up body end-to-end with the host boundary mocked."""

    def _prep(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        ready: bool = True,
        dist: bool = True,
    ) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        # Force git resolution to miss so the root fallback resolves deterministically.
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        # Insulate `_up` from the HOST's real port occupancy. `allocate_port`
        # bind-probes, so without this a developer's own pod sitting on the
        # derived port would push these tests down the fallback path and they
        # would disagree with the port they assert -- a failure that depends on
        # who is running them. Tests about the fallback override this.
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        if ready:
            _ready_worktree(tmp_path / "wts", "demo", venv=True, dist=dist)
        return PodConfig.load()

    def test_up_happy_path_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        out = capsys.readouterr().out
        assert '"port": 7811' in out and '"token": "tok-9"' in out
        # _up must pin the resolved checkout for the systemd boot.
        assert rt.read_env_file(c, "demo").get("CHECKOUT", "").endswith("wts/demo")

    def test_up_with_a_seed_runs_the_landing_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        calls: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(
            pod_cli,
            "_verify_seed_landed",
            lambda cfg, name, scenario, home_was_populated: calls.append(
                (name, scenario, home_was_populated)
            ),
        )

        pod_cli._up(
            c,
            argparse.Namespace(name="demo", json=True, seed="minimal", ttl="2h", provision=False),
        )

        assert calls == [("demo", "minimal", False)]

    @pytest.mark.parametrize("landed", ["", "minimal"])
    def test_populated_home_seed_request_refuses_before_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, landed: str
    ) -> None:
        """A failed start cleans up the pod home, so verification after health is
        too late for state that existed before this command. Every seed request
        against a populated home must refuse before `start_pod` can reach
        `stop_pod`, including one whose marker already matches the request."""
        c = self._prep(tmp_path, monkeypatch)
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        starts: list[str] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(
            rt,
            "start_pod",
            lambda cfg, name: (starts.append(name) or _cp(returncode=0)),
        )
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        # This wiring test owns the pre-start decision, not POSIX marker I/O.
        monkeypatch.setattr(rt, "seeded_scenario_in_home", lambda cfg, name: landed)

        with pytest.raises(SystemExit) as excinfo:
            pod_cli._up(
                c,
                argparse.Namespace(
                    name="demo", json=True, seed="minimal", ttl="2h", provision=False
                ),
            )

        assert excinfo.value.code == 1
        assert starts == []
        assert (home / "sessions").is_dir()

    def test_up_still_succeeds_when_ownership_cannot_be_proven(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """An lsof-less host must not boot the pod and then fail the command.

        The credential is the last step of `up`, not its point. Dying here would
        make `pod up` impossible on a POSIX host with no listener-lookup tool,
        where ownership can never be proven -- a self-inflicted outage in place of
        a hardened credential path. The secret still never reaches the wire: the
        guard is inside mint_token, which raised before dialling.
        """
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)

        def _unprovable(cfg: object, n: object, ttl: object) -> str:
            raise rt.PodOwnershipUnproven("could not prove which process holds :7811")

        monkeypatch.setattr(rt, "mint_token", _unprovable)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        cap = capsys.readouterr()
        out = cap.out
        # Reports what it knows, withholds only the credential.
        assert '"status": "up"' in out and '"port": 7811' in out
        assert '"base_url": "http://127.0.0.1:7811"' in out
        # The empty token IS the machine signal; the JSON shape gains no field for
        # it, so the reason travels on stderr where a caller and a human both see
        # it. A payload key with no consumer would be schema committed forever.
        assert '"token": ""' in out
        assert "token_withheld" not in out
        assert "could not prove" in cap.err

    def test_up_still_dies_on_a_foreign_port_holder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FOREIGN is positive knowledge, so there is nothing truthful left to print."""
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)

        def _foreign(cfg: object, n: object, ttl: object) -> str:
            raise rt.PodError("held by another process")

        monkeypatch.setattr(rt, "mint_token", _foreign)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
            )

    def test_up_refuses_live_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: c.live_port)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )

    def test_up_pins_the_fallback_so_every_reader_agrees(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The defect end to end: a busy derived port must not be booted onto.

        The pin is the load-bearing part. `pod url`, `pod ls`, `pod exec` and Dev
        Fleet each call `derive_port` independently, so without recording the move
        they would keep printing the derived port while the gateway listens
        somewhere else -- which is what presented as "a healthy pod that is not the
        one I just built".
        """
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        captured = capsys.readouterr()
        pinned = rt.read_env_file(c, "demo").get("PORT")
        assert pinned and int(pinned) != derived, "the move must be recorded as a PORT= pin"
        # The whole point of pinning: the readers now agree with the boot.
        assert rt.derive_port(c, "demo") == int(pinned)
        assert f'"port": {int(pinned)}' in captured.out, "the reported port must be the real one"
        assert "already in use" in captured.err, "a silent move is the defect, not the fix"

    def test_up_does_not_move_a_running_pods_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An `up` against an ALREADY-ACTIVE pod must not reallocate.

        That pod's port is busy precisely because it owns it, so probing would
        "discover" a collision and move a running pod out from under every reader.
        The active check therefore gates the allocation, not just the start.
        """
        c = self._prep(tmp_path, monkeypatch)
        # Nothing is free: if allocation ran at all it would raise or relocate.
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: False)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(
            rt, "start_pod", lambda *a, **k: pytest.fail("an active pod must not be restarted")
        )

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert "PORT" not in rt.read_env_file(
            c, "demo"
        ), "a running pod's port must be left exactly as its readers already resolve it"

    def test_up_clears_a_stale_auto_marker_when_the_operator_takes_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker must not outlive the pin it described.

        The trap is a natural user action, not a coincidence: the pod is auto-moved
        to a port, the operator sees that and pins THAT port by hand. If the old
        marker survives, their deliberate pin now matches it, reads as machine-made,
        and may be relocated out from under them -- the exact guarantee this PR
        exists to give them.

        So the marker is cleared the moment an operator pin is detected, and the
        follow-on is asserted too: pinning the once-auto value is respected.
        """
        c = self._prep(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        # State after an earlier automatic allocation to 7850, then the operator
        # editing PORT to something else.
        c.env_file("demo").write_text(f"PORT='7999'\n{rt.AUTO_PORT_KEY}='7850'\n")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == "7999", "the operator's pin must be honoured"
        assert not env.get(rt.AUTO_PORT_KEY), (
            f"a stale marker survived as {env.get(rt.AUTO_PORT_KEY)!r}; if the "
            "operator later pins that same port it would read as machine-made"
        )
        # The follow-on: pinning the once-auto port is respected.
        c.env_file("demo").write_text(f"PORT='7850'\n{rt.AUTO_PORT_KEY}=''\n")
        assert rt.operator_pinned(c, "demo") is True, (
            "after the marker is cleared, pinning the old auto port must read as "
            "the operator's deliberate choice"
        )

    def test_up_never_marks_an_operator_pin_as_automatic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recording the claim must not launder a deliberate pin into a movable one.

        `PORT_AUTO` is what licenses a later relocation. Since the claim is now
        recorded on EVERY allocation, an operator's own `PORT=` passes through that
        write -- and stamping the marker on it would quietly convert the one pin this
        code promises never to move into one it may move on the next contention.
        """
        c = self._prep(tmp_path, monkeypatch)
        c.pods_dir.mkdir(parents=True, exist_ok=True)
        c.env_file("demo").write_text("PORT='7999'\n")  # a person set this, no marker
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == "7999", "the operator's pin must be honoured"
        assert not env.get(rt.AUTO_PORT_KEY), (
            "the operator's pin was marked automatic, so a later collision would "
            "silently relocate a port they deliberately chose. (An EMPTY marker is "
            "no marker -- the key may be present and blank, since write_env_file "
            "merges and cannot delete.)"
        )
        assert rt.operator_pinned(c, "demo") is True, "it must still read as theirs"

    def test_up_records_the_claim_even_when_nothing_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The claim must not be conditional on having been displaced.

        This is the ruling's whole point. A unit is `Type=simple`, so `start_pod`
        returns before the gateway binds; until it does, a bind probe reports this
        pod's port FREE. If an undisplaced pod records nothing, a concurrently
        starting colliding name is handed the same port and one gateway crash-loops
        -- the original defect arriving through the concurrent door.

        So the recording is asserted for the case where NOTHING moved, which is the
        case a displacement-only test cannot see.
        """
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda _p: True)  # nothing is busy
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        env = rt.read_env_file(c, "demo")
        assert env.get("PORT") == str(derived), (
            "an undisplaced pod must still record its claim -- otherwise it is "
            "invisible to a colliding name until its gateway binds"
        )
        assert env.get(rt.AUTO_PORT_KEY) == str(derived), "the claim must be marked ours"

    def test_up_records_the_auto_marker_beside_the_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the marker the fallback is indistinguishable from a deliberate
        # operator pin, and the pod can never be relocated again.
        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        monkeypatch.setattr(rt, "_port_is_free", lambda p: p != derived)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )
        env = rt.read_env_file(c, "demo")
        assert env["PORT"] == env[rt.AUTO_PORT_KEY], "the marker must record the port WE chose"

    def test_up_refreshes_a_stale_port_when_the_pod_is_already_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The concurrent same-name case, which must not tear down a healthy pod.

        `port` is first read BEFORE the mutex. If a concurrent `up` wins the lock,
        allocates a fallback and pins it, this call wakes holding the OLD derived
        port -- and the health wait stops the unit when it cannot reach `port`, so a
        stale value would stop the pod the other `up` just started successfully.

        The pin therefore has to appear DURING the lock wait, not before it: with it
        already on disk the pre-lock read picks it up too and the staleness never
        occurs. So the name mutex stands in for the concurrent winner and writes the
        pin as we acquire.
        """
        import contextlib as _contextlib

        c = self._prep(tmp_path, monkeypatch)
        derived = rt.derive_port(c, "demo")
        fallback = derived + 7
        c.pods_dir.mkdir(parents=True, exist_ok=True)

        @_contextlib.contextmanager
        def _mutex_that_loses_the_race(_cfg, _name):
            # The other `up` got here first: it pinned its fallback and started the
            # pod while we were blocked on this lock. Written DIRECTLY and once --
            # `write_env_file` re-acquires this very mutex, so going through it here
            # would recurse, and `pin_checkout` below re-enters on the same thread.
            if not raced:
                raced.append(True)
                c.env_file("demo").write_text(
                    f"PORT='{fallback}'\n{rt.AUTO_PORT_KEY}='{fallback}'\n"
                )
            yield

        raced: list[bool] = []
        monkeypatch.setattr(rt, "pod_name_mutex", _mutex_that_loses_the_race)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        probed: list[int] = []
        monkeypatch.setattr(
            pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: (probed.append(p), 403)[1]
        )

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert probed == [fallback], (
            "the health wait must use the port resolved INSIDE the lock "
            f"({fallback}), not the pre-lock derivation ({derived})"
        )

    def test_allocation_and_start_happen_under_the_plane_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two DIFFERENT colliding names hold disjoint NAME locks, so the claim has
        to be serialized plane-wide or both can probe one port free and both boot."""
        import contextlib as _contextlib

        c = self._prep(tmp_path, monkeypatch)
        order: list[str] = []

        @_contextlib.contextmanager
        def _tracking_plane(_cfg):
            order.append("plane-acquire")
            try:
                yield
            finally:
                order.append("plane-release")

        monkeypatch.setattr(rt, "pod_plane_mutex", _tracking_plane)
        monkeypatch.setattr(
            rt, "allocate_port", lambda cfg, n: (order.append("allocate"), (7811, None))[1]
        )
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "start_pod", lambda cfg, n: (order.append("start"), _cp())[1])
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)

        pod_cli._up(
            c, argparse.Namespace(name="demo", json=True, seed="", ttl="2h", provision=False)
        )

        assert order == ["plane-acquire", "allocate", "start", "plane-release"], (
            "the claim must be held from choosing the port through starting the pod "
            f"-- got {order}"
        )

    def test_up_missing_worktree_teaches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, ready=False)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="ghost", json=False, seed="", ttl="2h", provision=False)
            )
        assert "git worktree add" in capsys.readouterr().err  # git-native resolution

    def test_up_no_dist_points_at_provision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, dist=False)  # venv but no dist
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        assert "--provision" in capsys.readouterr().err

    def test_up_broken_gateway_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: False)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: -1)
        monkeypatch.setattr(rt, "recent_journal", lambda cfg, n, ln=30, **_kw: "ImportError: boom")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        with pytest.raises(SystemExit):
            pod_cli._up(
                c, argparse.Namespace(name="demo", json=False, seed="", ttl="2h", provision=False)
            )
        err = capsys.readouterr().err
        assert "worktree build, not pod" in err and "ImportError: boom" in err

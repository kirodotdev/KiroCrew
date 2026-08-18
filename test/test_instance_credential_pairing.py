"""The internal-API credential must stay paired with the port it guards.

The defect these cover: the credential is generated per gateway start and held in
memory as the value the auth middleware compares against, but it was published
only to one shared ``.local_secret`` per data home. A second gateway starting in
the same home replaced that file while the first kept serving, so every internal
caller then sent the newcomer's credential to the incumbent and the whole internal
channel answered 403 with a bare ``Forbidden`` until something restarted.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.dashboard import server as dashboard_server
from kiro_crew.dashboard import token_auth
from kiro_crew.instances import run_marker


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    # config_dir memoises on (env value, resolved home); clear it so the temp
    # home is honoured rather than a value cached by an earlier test.
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_config_dir_memo", None, raising=False)
    return tmp_path


class TestPerPortCredentialFile:
    def test_path_is_keyed_by_port_and_sits_beside_the_marker(self, home: Path) -> None:
        assert run_marker.secret_path(5476).name == "gateway-5476.secret"
        assert run_marker.secret_path(5476).parent == run_marker.marker_path(5476).parent

    def test_read_returns_empty_when_absent(self, home: Path) -> None:
        assert run_marker.read_secret(5476) == ""

    def test_read_does_not_create_the_run_dir(self, home: Path) -> None:
        run_marker.read_secret(5476)
        assert not (home / "run").exists()

    def test_round_trip(self, home: Path) -> None:
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "deadbeef")
        assert run_marker.read_secret(7811) == "deadbeef"

    def test_written_owner_only(self, home: Path) -> None:
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "deadbeef")
        path = run_marker.secret_path(7811)
        # The value must land whatever the platform's permission model is.
        assert path.read_text(encoding="utf-8").strip() == "deadbeef"
        if os.name == "nt":
            # Windows does not honour POSIX mode bits: chmod only toggles the
            # read-only attribute, so a 0600 write reports back as 0666 and the
            # assertion below would describe the platform, not the code. The
            # credential is protected there by the ACL on the user-profile data
            # home instead.
            pytest.skip("POSIX mode bits are not honoured on Windows")
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)

    def test_cleared_with_the_marker_so_a_dead_generation_leaves_no_credential(
        self, home: Path
    ) -> None:
        run_marker.write_marker(5476)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "deadbeef")
        run_marker.clear_marker(5476)
        assert run_marker.read_secret(5476) == ""


class TestSharedFileIsNotClobberedWhileASiblingServes:
    def test_shared_file_written_when_this_is_the_only_gateway(self, home: Path) -> None:
        shared = home / ".local_secret"
        with mock.patch.object(dashboard_server, "_live_sibling_port", return_value=None):
            dashboard_server._write_instance_credentials(shared, 5476, "mine")
        assert shared.read_text() == "mine"
        assert run_marker.read_secret(5476) == "mine"

    def test_shared_file_preserved_when_another_gateway_is_live(self, home: Path) -> None:
        shared = home / ".local_secret"
        shared.write_text("incumbent")
        with mock.patch.object(dashboard_server, "_live_sibling_port", return_value=5476):
            dashboard_server._write_instance_credentials(shared, 7811, "newcomer")
        # The incumbent keeps comparing against "incumbent"; clients that resolve
        # its port must keep reading it.
        assert shared.read_text() == "incumbent"
        # The newcomer is still reachable, under its own port.
        assert run_marker.read_secret(7811) == "newcomer"

    def test_a_stale_marker_is_not_a_sibling(self, home: Path) -> None:
        # A crashed gateway leaves its marker behind. Treating that as a live
        # sibling would stop every subsequent gateway from publishing the shared
        # credential at all -- the guard must key on the ownership proof, not on
        # the file's presence.
        run_marker.write_marker(5476)
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port", return_value=False
        ):
            assert dashboard_server._live_sibling_port(7811) is None

    def test_a_verified_live_marker_is_a_sibling(self, home: Path) -> None:
        run_marker.write_marker(5476)
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port", return_value=True
        ):
            assert dashboard_server._live_sibling_port(7811) == 5476

    def test_own_port_is_never_its_own_sibling(self, home: Path) -> None:
        run_marker.write_marker(5476)
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port", return_value=True
        ):
            assert dashboard_server._live_sibling_port(5476) is None

    def test_discovery_failure_does_not_block_startup(self, home: Path) -> None:
        with mock.patch.object(
            run_marker, "marker_ports", side_effect=OSError("boom")
        ):
            assert dashboard_server._live_sibling_port(5476) is None


class TestClientReadsTheCredentialForThePortItDials:
    def test_per_port_beats_the_shared_file(self, home: Path) -> None:
        from kiro_crew import mcp_core

        (home / ".local_secret").write_text("newcomer-that-replaced-the-file")
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "owner-of-5476")
        with mock.patch.object(mcp_core, "_api_port", return_value=5476):
            assert mcp_core._internal_secret() == "owner-of-5476"

    def test_falls_back_to_shared_file_for_a_gateway_without_a_per_port_file(
        self, home: Path
    ) -> None:
        from kiro_crew import mcp_core

        (home / ".local_secret").write_text("older-gateway")
        with mock.patch.object(mcp_core, "_api_port", return_value=5476):
            assert mcp_core._internal_secret() == "older-gateway"

    def test_no_credential_anywhere_yields_empty_not_an_exception(self, home: Path) -> None:
        from kiro_crew import mcp_core

        with mock.patch.object(mcp_core, "_api_port", return_value=5476):
            assert mcp_core._internal_secret() == ""

    def test_two_generations_in_one_home_no_longer_collide(self, home: Path) -> None:
        """End-to-end of the reported failure, at the credential layer.

        Incumbent owns 5476. A second gateway starts in the same home on 7811.
        Before the fix the client read the shared file and sent the newcomer's
        credential to the incumbent; now each port resolves to its own owner.
        """
        from kiro_crew import mcp_core

        shared = home / ".local_secret"
        with mock.patch.object(dashboard_server, "_live_sibling_port", return_value=None):
            dashboard_server._write_instance_credentials(shared, 5476, "incumbent")
        with mock.patch.object(dashboard_server, "_live_sibling_port", return_value=5476):
            dashboard_server._write_instance_credentials(shared, 7811, "newcomer")

        with mock.patch.object(mcp_core, "_api_port", return_value=5476):
            assert mcp_core._internal_secret() == "incumbent"
        with mock.patch.object(mcp_core, "_api_port", return_value=7811):
            assert mcp_core._internal_secret() == "newcomer"


class TestPruneKeepsALiveSibling:
    def test_a_false_ownership_answer_does_not_take_the_credential_with_it(
        self, home: Path
    ) -> None:
        """False from the ownership check means UNPROVEN, never "process gone".

        ``_gateway_owns_port`` fails closed by returning False: non-POSIX returns
        False outright, and a missing or throwing listener-lookup tool is folded
        into False too. Treating False as death would delete a LIVE incumbent's
        credential on every Windows host, drop its clients onto a shared file a
        newcomer may have replaced, and make the prune cause the 403 this whole
        change exists to prevent. The marker may go; the credential may not.
        """
        run_marker.write_marker(5476)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "incumbent")
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port", return_value=False
        ):
            run_marker.prune_markers(keep_port=7811)
        assert run_marker.marker_ports() == []
        assert run_marker.read_secret(5476) == "incumbent"

    def test_an_unverifiable_host_also_keeps_the_credential(self, home: Path) -> None:
        run_marker.write_marker(5476)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "incumbent")
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port",
            side_effect=OSError("no listener tooling"),
        ):
            run_marker.prune_markers(keep_port=7811)
        assert run_marker.read_secret(5476) == "incumbent"

    def test_live_sibling_is_kept(self, home: Path) -> None:
        # A blanket prune here deletes a serving gateway's marker + pid, which
        # makes it undiscoverable to client commands AND removes the evidence the
        # credential writer needs to leave its credential alone.
        run_marker.write_marker(5476)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "alive")
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port", return_value=True
        ):
            run_marker.prune_markers(keep_port=7811)
        assert run_marker.marker_ports() == [5476]
        assert run_marker.read_secret(5476) == "alive"

    def test_unverifiable_ownership_still_prunes(self, home: Path) -> None:
        run_marker.write_marker(5476)
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port",
            side_effect=OSError("no /proc"),
        ):
            run_marker.prune_markers(keep_port=7811)
        assert run_marker.marker_ports() == []

    def test_unverifiable_ownership_never_takes_a_live_credential(
        self, home: Path
    ) -> None:
        """The prune must not be able to break a serving gateway.

        On a host where ownership cannot be reported the check fails open to the
        prune, so if the credential went with the marker a starting sibling would
        strip the incumbent's credential, its clients would fall back to a shared
        file the sibling may have replaced, and every incumbent internal call
        would 403 -- this PR's own bug, reintroduced from the other direction.
        """
        run_marker.write_marker(5476)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "incumbent")
        with mock.patch(
            "kiro_crew.port_resolution._gateway_owns_port",
            side_effect=OSError("ownership unverifiable on this host"),
        ):
            run_marker.prune_markers(keep_port=7811)
        assert run_marker.read_secret(5476) == "incumbent"


class TestEphemeralBindPublishesUnderTheRealPort:
    """`--port auto` binds port 0; the credential must not be filed under it.

    A credential at `gateway-0.secret` is unreachable for every client, and they
    would fall back to the shared file -- which the live-sibling guard
    deliberately leaves pointing at the sibling, so the ephemeral gateway would
    403 every internal call. `--test-mode` implies `--port auto`, so this is the
    default shape for a throwaway instance.
    """

    class _Runner:
        def __init__(self, addresses: object) -> None:
            self.addresses = addresses

    def test_declared_port_is_used_when_non_zero(self) -> None:
        runner = self._Runner([("127.0.0.1", 5476)])
        assert dashboard_server._resolved_bound_port(runner, 5476) == 5476

    def test_os_assigned_port_is_read_back_when_declared_is_zero(self) -> None:
        runner = self._Runner([("127.0.0.1", 41234)])
        assert dashboard_server._resolved_bound_port(runner, 0) == 41234

    def test_a_unix_socket_address_is_not_mistaken_for_a_port(self) -> None:
        runner = self._Runner(["/run/user/1000/kirocrew/gateway.sock", ("127.0.0.1", 41234)])
        assert dashboard_server._resolved_bound_port(runner, 0) == 41234

    def test_zero_when_no_tcp_address_is_readable(self) -> None:
        runner = self._Runner(["/run/user/1000/kirocrew/gateway.sock"])
        assert dashboard_server._resolved_bound_port(runner, 0) == 0

    def test_credential_lands_under_the_assigned_port_not_zero(self, home: Path) -> None:
        shared = home / ".local_secret"
        with mock.patch.object(dashboard_server, "_live_sibling_port", return_value=5476):
            dashboard_server._write_instance_credentials(shared, 41234, "ephemeral")
        assert run_marker.read_secret(41234) == "ephemeral"
        assert run_marker.read_secret(0) == ""


class TestDenialNamesTheMismatchWithoutDisclosingTheCredential:
    def test_absent_is_distinguished_from_wrong(self) -> None:
        assert token_auth._credential_fingerprint("") == "absent"
        assert token_auth._credential_fingerprint("abc") != "absent"

    def test_fingerprint_does_not_contain_the_credential(self) -> None:
        secret = os.urandom(16).hex()
        fp = token_auth._credential_fingerprint(secret)
        assert secret not in fp
        assert len(fp.split("/")[0]) == 8

    def test_same_value_same_fingerprint_different_value_different(self) -> None:
        a = token_auth._credential_fingerprint("aaaa")
        b = token_auth._credential_fingerprint("bbbb")
        assert a == token_auth._credential_fingerprint("aaaa")
        assert a != b

    def test_detail_names_both_sides(self) -> None:
        detail = token_auth._credential_mismatch_detail("expected-one", "received-one")
        assert "expected=" in detail and "received=" in detail
        assert "expected-one" not in detail and "received-one" not in detail

    def test_detail_marks_a_caller_that_had_no_credential_at_all(self) -> None:
        detail = token_auth._credential_mismatch_detail("expected-one", "")
        assert "received=absent" in detail


class TestEveryToolGetsTheExplanation:
    """The copy lives in the shared decoder, so no tool needs its own branch."""

    def _body(self, payload: bytes, code: int = 403) -> dict:
        import urllib.error

        exc = urllib.error.HTTPError(
            "http://127.0.0.1/api/x", code, "Forbidden", {}, io.BytesIO(payload)
        )
        from kiro_crew import mcp_core

        return mcp_core._http_error_body(exc)

    def test_auth_mismatch_is_rewritten_for_every_caller(self) -> None:
        out = self._body(b'{"error": "Forbidden", "code": "internal_auth_mismatch"}')
        assert "wrong Kiro Crew instance" in out["error"]
        assert out["error"] != "Forbidden"

    def test_a_plain_forbidden_is_not_misdiagnosed(self) -> None:
        # A genuine permission denial carries the same body; explaining it as a
        # credential desync would send that user after a bug they do not have.
        out = self._body(b'{"error": "Forbidden"}')
        assert "wrong Kiro Crew instance" not in out["error"]
        assert out["error"] == "Forbidden"

    def test_learn_add_surfaces_the_rewritten_message(self) -> None:
        from kiro_crew.mcp_tools import learn

        rewritten = self._body(
            b'{"error": "Forbidden", "code": "internal_auth_mismatch"}'
        )
        with mock.patch.object(
            learn.mcp_core, "_post", return_value=rewritten
        ), mock.patch.object(
            learn.mcp_core, "_vet_memory_writes_governance", return_value=""
        ), mock.patch.object(
            learn.mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"
        ):
            out = learn.learn_add("learn_add", {"rule": "always check the port"})
        assert "wrong Kiro Crew instance" in out
        assert out.strip() != "Error: Forbidden"


class TestTheSharedHelperOwnsThePairing:
    """The invariant lives at one chokepoint, and the dial target is never inferred.

    An optional port would let a converted call site read a credential for one
    gateway while dialing another -- the desync this helper exists to close,
    reintroduced one call site at a time and invisible at the call site. So the
    parameter is required, and these tests pin that.
    """

    def test_helper_prefers_the_per_port_credential(self, home: Path) -> None:
        from kiro_crew.config.loader import read_local_secret

        (home / ".local_secret").write_text("shared-replaced-by-newcomer")
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "owner-of-5476")
        assert read_local_secret(5476) == "owner-of-5476"

    def test_helper_falls_back_to_the_shared_file(self, home: Path) -> None:
        from kiro_crew.config.loader import read_local_secret

        (home / ".local_secret").write_text("older-gateway")
        assert read_local_secret(5476) == "older-gateway"

    def test_helper_returns_empty_when_nothing_is_readable(self, home: Path) -> None:
        from kiro_crew.config.loader import read_local_secret

        assert read_local_secret(5476) == ""

    def test_port_is_required_so_a_call_site_cannot_omit_the_dial_target(self) -> None:
        import inspect

        from kiro_crew.config.loader import read_local_secret

        param = inspect.signature(read_local_secret).parameters["port"]
        assert param.default is inspect.Parameter.empty, (
            "read_local_secret(port) must stay required: a default would let a "
            "caller dial one gateway and authenticate for another"
        )
        with pytest.raises(TypeError):
            read_local_secret()  # type: ignore[call-arg]

    def test_no_caller_relies_on_ambient_port_resolution(self) -> None:
        """Every call site names its dial target.

        Grep-level because the failure is a MISSING argument: a reviewer reading one
        hunk cannot see that the port came from somewhere else, and the runtime
        symptom is a 403 on a different machine shape than the developer's.
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        offenders = []
        for path in src.rglob("*.py"):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "read_local_secret()" in line and "def " not in line:
                    offenders.append(f"{path.relative_to(src)}:{i}")
        assert not offenders, f"read_local_secret called with no port: {offenders}"

    def test_only_the_shared_helper_spells_the_resolution_order(self) -> None:
        """No module re-implements per-port-then-shared under its own name.

        The previous test matches one call NAME, so a surface that copies the
        ORDER into a private helper escapes it -- which is how a duplicate spelling
        got into this change's own diff. This matches on the behaviour instead: a
        module that reads a per-port credential must not also read the shared file,
        unless it is the shared helper itself (or this test).
        """
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        allowed = {pathlib.Path("config/loader.py")}
        offenders = []
        for path in src.rglob("*.py"):
            rel = path.relative_to(src)
            if rel in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            reads_per_port = "run_marker.read_secret(" in text
            reads_shared = '".local_secret"' in text
            if reads_per_port and reads_shared:
                offenders.append(str(rel))
        assert not offenders, (
            "these modules re-implement the per-port-then-shared order instead of "
            f"calling config.loader.read_local_secret: {offenders}"
        )

    def test_mcp_core_delegates_rather_than_reimplementing(self, home: Path) -> None:
        from kiro_crew import mcp_core

        with mock.patch.object(mcp_core, "_api_port", return_value=7811), mock.patch(
            "kiro_crew.mcp_core.read_local_secret", return_value="from-helper"
        ) as helper:
            assert mcp_core._internal_secret() == "from-helper"
        helper.assert_called_once_with(7811)

    def test_cron_trigger_pairs_its_credential_with_the_port(self, home: Path) -> None:
        from kiro_crew import cron_trigger

        shared = home / ".local_secret"
        shared.write_text("shared-replaced-by-newcomer")
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "owner-of-7811")
        seen: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true, "name": "job"}'

        def _fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
            seen["secret"] = req.headers.get("X-internal-secret", "")
            return _Resp()

        with mock.patch.object(cron_trigger, "loopback_urlopen", _fake_urlopen):
            ok, _msg = cron_trigger.trigger_cron_job("abc123", 7811, shared)
        assert ok
        assert seen["secret"] == "owner-of-7811"

    def test_sage_probe_only_considers_ports_this_process_can_claim(self) -> None:
        """An authenticated probe must never sweep for a stranger's gateway.

        Each candidate is probed with THAT port's own credential, so a blind range
        sweep would authenticate against whichever sibling answered first and this
        app would then create and delete review artifacts in that instance's store.
        With no self-declared port the list is empty and the caller falls back to a
        default whose request errors clearly -- failing closed rather than writing to
        a stranger.
        """
        from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_driver

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                review_driver.store, "crew_home", return_value=Path("/nonexistent")
            ):
                assert review_driver._candidate_ports() == []

        with mock.patch.dict(os.environ, {"KIROCREW_PORT": "7811"}, clear=True):
            with mock.patch.object(
                review_driver.store, "crew_home", return_value=Path("/nonexistent")
            ):
                assert review_driver._candidate_ports() == [7811]

        # The parent gateway exports its ACTUAL bound port, which is the only numeric
        # source a `--port auto` instance has and the correct one when the requested
        # port was taken. It therefore leads, and remains self-declared: a pod drops
        # it precisely so it never inherits its parent's listener.
        with mock.patch.dict(
            os.environ,
            {"KIROCREW_BOUND_PORT": "7899", "KIROCREW_PORT": "5476"},
            clear=True,
        ):
            with mock.patch.object(
                review_driver.store, "crew_home", return_value=Path("/nonexistent")
            ):
                assert review_driver._candidate_ports() == [7899, 5476]

    def test_cron_child_dials_the_port_its_credential_was_minted_for(self) -> None:
        """One resolution owns both, so credential and dial target cannot diverge.

        The parent mints the credential and the child sends it. If the child resolves
        its own port it reads KIROCREW_PORT, which is 5476 on a `--port auto` gateway
        -- a SIBLING -- so it would present a valid credential for one gateway to a
        different one and the call would 403. The parent therefore injects the port it
        minted for, and the child prefers it.
        """
        from kiro_crew import cron_script

        job = mock.Mock()

        with mock.patch.dict(
            os.environ, {"_KIROCREW_DIAL_PORT": "7899", "KIROCREW_PORT": "5476"}
        ):
            ctx = cron_script.ScriptContext(job=job)
            assert ctx._port == 7899

        # Without the injection the fallback still holds for a directly-constructed
        # context, so this is a preference and not a hard dependency.
        with mock.patch.dict(os.environ, {"KIROCREW_PORT": "5476"}, clear=True):
            assert cron_script.ScriptContext(job=job)._port == 5476

    def test_cron_trigger_prefers_a_named_path_over_the_home_wide_file(
        self, home: Path, tmp_path: Path
    ) -> None:
        """With no per-port credential, the named path beats the home-wide file.

        This is the arm the named ``secret_path`` genuinely wins: the home-wide file
        is the one a second gateway generation replaces, so falling back to it when
        the caller named a file would authenticate with whichever generation wrote
        last. It does NOT outrank the per-port read -- see the companion test.
        """
        from kiro_crew import cron_trigger

        (home / ".local_secret").write_text("ambient-home-of-this-process")
        explicit = tmp_path / "pod-home-secret"
        explicit.write_text("explicitly-named-home")
        seen: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true, "name": "job"}'

        def _fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
            seen["secret"] = req.headers.get("X-internal-secret", "")
            return _Resp()

        with mock.patch.object(cron_trigger, "loopback_urlopen", _fake_urlopen):
            ok, _msg = cron_trigger.trigger_cron_job("abc123", 9999, explicit)
        assert ok
        assert seen["secret"] == "explicitly-named-home"

    def test_cron_trigger_prefers_the_dialed_ports_credential_over_a_named_path(
        self, home: Path, tmp_path: Path
    ) -> None:
        """The per-port credential wins, and this test states the cost of that.

        Both real callers pass the home-wide file as ``secret_path``, so the per-port
        read MUST outrank it or the original defect returns. The consequence, pinned
        here so it cannot be discovered as a surprise: a per-port credential left
        behind by a crashed gateway (the prune never deletes credentials) is also
        preferred over a named path. Closing that means removing the parameter, not
        flipping this order -- flipping it would make both callers prefer the
        home-wide file and reinstate the bug.
        """
        from kiro_crew import cron_trigger

        dashboard_server._write_secret_file(run_marker.secret_path(9999), "per-port")
        named = tmp_path / "named-secret"
        named.write_text("named-home")
        seen: dict[str, str] = {}

        class _Resp:
            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true, "name": "job"}'

        def _fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
            seen["secret"] = req.headers.get("X-internal-secret", "")
            return _Resp()

        with mock.patch.object(cron_trigger, "loopback_urlopen", _fake_urlopen):
            ok, _msg = cron_trigger.trigger_cron_job("abc123", 9999, named)
        assert ok
        assert seen["secret"] == "per-port"


class TestFrameRelayNeverCredentialsASiblingGateway:
    """A desktop frame must not be POSTed, credentialed, to another instance.

    ``screencast`` mirrors captures to its own gateway's ingress, which is strict:
    no credential means the POST is refused and the frame is dropped. The hazard is
    the opposite case. ``parse_dashboard_url`` reads ``KIROCREW_PORT`` then
    ``dashboard.url``, and on a ``--port auto`` gateway neither names the port that
    was actually bound -- only ``KIROCREW_BOUND_PORT`` does. So both the ingress URL
    and the credential resolved to 5476, a SIBLING on a multi-gateway host, and the
    sibling then ACCEPTED the frame and broadcast somebody else's desktop to its own
    owners.

    Both halves are pinned here: the URL follows the bound port, and a port that is
    only a guess gets no credential at all.
    """

    def test_ingress_follows_the_bound_port_not_the_configured_one(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.computer_use import screencast

        # The shape of the bug: config names the sibling, the bound port is ours.
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        (home / "config.json").write_text('{"dashboard": {"url": "http://127.0.0.1:5476"}}')

        url = screencast._ingress_url()
        assert ":7811" in url, f"frames still aim at the configured port: {url}"
        assert ":5476" not in url

    def test_the_credential_matches_the_port_the_frame_is_posted_to(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.computer_use import screencast

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "sibling-secret")
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "our-secret")

        headers = screencast._headers()
        assert headers.get(screencast.FRAME_SECRET_HEADER) == "our-secret"
        assert "sibling-secret" not in headers.values()

    def test_a_default_port_install_still_gets_its_credential(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common install must keep mirroring, not be sacrificed to the fix.

        With no port evidence at all the resolver returns the default and reports
        ``evidence_backed=False``. An earlier draft of this fix withheld the
        credential in that case, reasoning that a guessed port might be a sibling's.
        It was the wrong trade twice over: a ``--port auto`` gateway always has
        ``KIROCREW_BOUND_PORT`` to offer, so the guard never fired in the scenario
        it was written for, and it silently stopped every ordinary default-port
        install from mirroring. ``test_computer_use_api`` caught it.
        """
        from kiro_crew.computer_use import screencast
        from kiro_crew.port_resolution import resolve_client_port_ex

        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        port, evidence_backed = resolve_client_port_ex(None)
        if evidence_backed:  # pragma: no cover - environment-dependent guard
            pytest.skip("this environment supplies positive port evidence")
        dashboard_server._write_secret_file(run_marker.secret_path(port), "default-port-secret")

        headers = screencast._headers()
        assert headers.get(screencast.FRAME_SECRET_HEADER) == "default-port-secret"

    def test_url_and_credential_cannot_diverge(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both read the same resolver, so no input can split them apart."""
        from kiro_crew.computer_use import screencast

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")
        monkeypatch.delenv("KIROCREW_PORT", raising=False)
        (home / "config.json").write_text('{"dashboard": {"url": "http://127.0.0.1:5476"}}')
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "our-secret")

        url = screencast._ingress_url()
        headers = screencast._headers()
        assert ":7811" in url
        assert headers.get(screencast.FRAME_SECRET_HEADER) == "our-secret"

    def test_an_inherited_kirocrew_port_does_not_win_over_the_bound_port(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The generic client resolver's own ordering is the hazard here.

        ``resolve_client_port_ex`` reads ``KIROCREW_PORT`` BEFORE
        ``KIROCREW_BOUND_PORT``, which is right for a CLI client aiming at a chosen
        instance and wrong for code running inside the gateway. A shell that
        exported ``KIROCREW_PORT=5476`` and then started a second gateway with
        ``--port auto`` leaves both set, and the frame would carry 5476's own valid
        credential -- so that sibling ACCEPTS the capture rather than refusing it.
        """
        from kiro_crew.computer_use import screencast

        monkeypatch.setenv("KIROCREW_PORT", "5476")  # inherited, names the sibling
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")  # what we actually bound
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "sibling-secret")
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "our-secret")

        url = screencast._ingress_url()
        headers = screencast._headers()
        assert ":7811" in url, f"frames aim at the sibling: {url}"
        assert headers.get(screencast.FRAME_SECRET_HEADER) == "our-secret", (
            "the frame carries the sibling's credential, so its ingress accepts the "
            "capture and broadcasts this desktop to its owners"
        )

    def test_kirocrew_port_still_decides_when_no_port_was_bound(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preferring the bound port must not disable the dev-instance override."""
        from kiro_crew.computer_use import screencast

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        dashboard_server._write_secret_file(run_marker.secret_path(6777), "dev-secret")

        assert ":6777" in screencast._ingress_url()
        assert screencast._headers().get(screencast.FRAME_SECRET_HEADER) == "dev-secret"

    def test_a_malformed_bound_port_falls_through_instead_of_raising(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.computer_use import screencast

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "not-a-port")
        monkeypatch.setenv("KIROCREW_PORT", "6777")
        assert ":6777" in screencast._ingress_url()


class TestCronDialsTheGatewayItRunsUnderNotASibling:
    """A startup cron must not mint-and-send a credential to a sibling gateway.

    ``_resolve_dial_port`` is the single resolution the parent injects as
    ``_KIROCREW_DIAL_PORT`` so credential and dial target cannot diverge. The
    hazard is the same one screencast had: the generic resolver reads
    ``KIROCREW_PORT`` before ``KIROCREW_BOUND_PORT``, so an inherited
    ``KIROCREW_PORT=5476`` beside a ``--port auto`` gateway dials 5476 -- a
    SIBLING -- and an overdue cron then authenticates a real callback against it.

    Deliberately NOT tested: a "refuse until the per-port credential exists" gate.
    There is none, on purpose -- an unresolved secret reads empty and the ingress
    refuses the empty header, so fail-closed already holds, and an explicit gate
    would only reintroduce the default-port regression a sibling fix already hit.
    """

    def test_bound_port_wins_over_an_inherited_kirocrew_port(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cron_script

        monkeypatch.setenv("KIROCREW_PORT", "5476")  # inherited, the sibling
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")  # what we actually bound
        assert cron_script._resolve_dial_port() == 7811

    def test_the_credential_is_read_for_the_dialed_port(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cron_script

        monkeypatch.setenv("KIROCREW_PORT", "5476")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")
        dashboard_server._write_secret_file(run_marker.secret_path(5476), "sibling-secret")
        dashboard_server._write_secret_file(run_marker.secret_path(7811), "our-secret")
        # The caller resolves the dial port once and passes it in; the credential
        # must be the one for that port, never the inherited-KIROCREW_PORT sibling.
        assert cron_script._resolve_internal_secret(cron_script._resolve_dial_port()) == "our-secret"

    def test_kirocrew_port_still_decides_when_no_port_was_bound(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cron_script

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        assert cron_script._resolve_dial_port() == 6777

    def test_a_malformed_bound_port_falls_through(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import cron_script

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "not-a-port")
        monkeypatch.setenv("KIROCREW_PORT", "6777")
        assert cron_script._resolve_dial_port() == 6777


class TestTheServingResolverIsTheOneGatewaySideChokepoint:
    """One shared resolver for every in-gateway caller, bound-port-first.

    The client resolver reads KIROCREW_PORT first, which is right for a CLI client
    and wrong for code inside the gateway. Rather than each in-gateway module
    carrying its own bound-port-first override (screencast, cron_script did, and
    mcp_cron was missed entirely), resolve_serving_port is the single chokepoint they
    all delegate to, so a new consumer cannot silently reintroduce the sibling bug by
    reaching for the client resolver.
    """

    def test_bound_port_beats_an_inherited_kirocrew_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.port_resolution import resolve_serving_port

        monkeypatch.setenv("KIROCREW_PORT", "5476")
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "7811")
        assert resolve_serving_port() == 7811

    def test_kirocrew_port_still_decides_with_no_bound_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.port_resolution import resolve_serving_port

        monkeypatch.setenv("KIROCREW_PORT", "6777")
        monkeypatch.delenv("KIROCREW_BOUND_PORT", raising=False)
        assert resolve_serving_port() == 6777

    def test_a_malformed_bound_port_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.port_resolution import resolve_serving_port

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "not-a-port")
        monkeypatch.setenv("KIROCREW_PORT", "6777")
        assert resolve_serving_port() == 6777

    def test_every_in_gateway_consumer_routes_through_it(self) -> None:
        # A grep-level guard: the three in-gateway consumers must not reach for the
        # client resolver directly, or the sibling bug returns one file at a time.
        src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        offenders = []
        for rel in (
            "computer_use/screencast.py",
            "cron_script.py",
            "mcp_cron.py",
        ):
            text = (src / rel).read_text(encoding="utf-8")
            if "resolve_client_port_ex" in text:
                offenders.append(rel)
        assert not offenders, (
            "these in-gateway modules still reference the client resolver; they must "
            f"use resolve_serving_port so credentials pair with the bound port: {offenders}"
        )


class TestReviewDriverDoesNotGuessASiblingPort:
    """The report-write base must be a port a source NAMED, never an invented 5476.

    _gateway_base probes candidate ports, each with its own credential. When none
    answers it may fall back only to a port _candidate_ports positively named; when
    NO source names one, guessing 5476 and dialing it with 5476's credential is how a
    review artifact gets created or pruned in whatever sibling owns that port.
    """

    def test_no_named_port_fails_closed_instead_of_guessing_5476(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_driver

        monkeypatch.setattr(review_driver, "_RESOLVED_BASE", "", raising=False)
        monkeypatch.setattr(review_driver, "_candidate_ports", lambda: [])
        # If _gateway_base returned a base anyway, _api_request would read a
        # credential for it; assert it refuses instead.
        assert review_driver._gateway_base() == ""
        result = review_driver._api_request("GET", "/whatever")
        assert "error" in result
        assert "5476" not in review_driver._gateway_base()

    def test_a_named_port_is_still_used_as_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.code_review_sage.sage_lib import review_driver

        monkeypatch.setattr(review_driver, "_RESOLVED_BASE", "", raising=False)
        monkeypatch.setattr(review_driver, "_candidate_ports", lambda: [7811])
        monkeypatch.setattr(review_driver, "_probe", lambda base, secret: False)
        assert review_driver._gateway_base() == "http://localhost:7811"

"""Publishing the dashboard on a tailnet: a write path, so failures must survive.

The sibling suite (``test_tailnet_origin.py``) pins the opposite property — that
the READ path swallows everything and degrades to "contributes nothing". Here the
weighting is inverted, because a publish that fails silently is the bug:

* :class:`TestFailureReporting` pins that the daemon's own stderr reaches the
  caller **verbatim**, and that the two refusals with different remedies
  (permission vs. daemon down) are told apart. The classifier matches on upstream
  wording we do not own, so the tests assert the raw text is present regardless of
  whether the classification lands.
* :class:`TestGovernance` pins the ASYMMETRY that makes the ceiling coherent:
  publishing is refused when pinned and does not spawn the CLI at all, while
  *withdrawing* is never gated — a fail-closed control that could not un-expose a
  host would fail open in effect.
* :class:`TestArgv` pins the exact argv, because the HTTPS port is what makes the
  derived origin (which carries no port) match, and the upstream target is
  loopback so publishing never widens the gateway's bind.
* :class:`TestPublishedDetection` pins that an unreadable status document reports
  ``None`` rather than ``False``. This code has never seen a real
  ``tailscale serve status --json``, so "we could not tell" has to be
  representable — reporting "not published" for a published node is the
  checked-but-never-ran defect in a new costume.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet_serve
from kiro_crew.dashboard.tailnet_serve import ServeState

_PORT = 5476
_CLI = "/usr/bin/tailscale"


def _doc(target: str) -> str:
    """A minimal status document with *target* at the mount we would withdraw.

    Detection is scoped to the HTTPS port AND the mount, because that pair is
    exactly what withdrawal removes — so a document that omits either is
    (correctly) `unknown` rather than a negative.
    """
    port = tailnet_serve.SERVE_HTTPS_PORT
    mount = tailnet_serve.SERVE_MOUNT
    return json.dumps(
        {"Web": {f"desk.tail1a2b3c.ts.net:{port}": {"Handlers": {mount: {"Proxy": target}}}}}
    )


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["tailscale"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_cli(**run_kwargs):
    """Patch the CLI as installed and ``subprocess.run`` in the module."""
    return (
        patch.object(tailnet_serve, "_cli_path", return_value=_CLI),
        patch.object(tailnet_serve.subprocess, "run", **run_kwargs),
    )


class TestArgv:
    def test_publish_argv_is_exact(self) -> None:
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            assert tailnet_serve.publish(_PORT).ok
        argv = m.call_args[0][0]
        assert argv == [
            _CLI,
            "serve",
            "--bg",
            "--https=443",
            f"http://127.0.0.1:{_PORT}",
        ]

    def test_upstream_target_is_loopback(self) -> None:
        """Publishing must not require the gateway to listen off-loopback.

        Serve runs on this same host, so the target is 127.0.0.1 — the exposure is
        added by the tailnet-facing TLS terminator, not by widening our bind.
        """
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            tailnet_serve.publish(_PORT)
        target = m.call_args[0][0][-1]
        assert target.startswith("http://127.0.0.1:")
        assert "0.0.0.0" not in target

    def test_https_port_matches_the_derived_origin(self) -> None:
        """443 is load-bearing, not cosmetic.

        A browser omits ``:443`` from ``Origin``, which is why the derived origin
        is a bare ``https://<name>``. Serving on any other port would produce an
        Origin that the allowlist does not contain.
        """
        assert tailnet_serve.SERVE_HTTPS_PORT == 443

    def test_unpublish_argv_names_its_mount(self) -> None:
        """`off` must name the mount, or upstream deletes every handler on 443.

        `unsetServe` treats an absent `--set-path` as "every mount under this port"
        and prompts interactively when there is more than one — a prompt this
        command has no TTY to answer.
        """
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "serve_state", return_value=ServeState(True, True, "ours")
        ):
            assert tailnet_serve.unpublish(_PORT).ok
        argv = m.call_args[0][0]
        assert argv == [_CLI, "serve", "--https", "443", "--set-path=/", "off"]
        assert "reset" not in argv


class TestGovernance:
    def test_publish_is_refused_when_pinned(self) -> None:
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=True
        ):
            result = tailnet_serve.publish(_PORT)
        assert not result.ok
        assert result.code == "governance_pinned"
        # Not merely refused AFTER the fact: the CLI must not run at all, or the
        # pin would only be cosmetic on the half an administrator cares about.
        m.assert_not_called()

    def test_publish_routes_the_decision_through_the_audited_seam(self) -> None:
        cli, run = _patch_cli(return_value=_proc())
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ) as probe:
            tailnet_serve.publish(_PORT)
        assert probe.call_args.kwargs["audit_tool"] == "tailnet_publish"

    def test_unpublish_is_never_gated(self) -> None:
        """Withdrawal survives a pin — including an unevaluable one.

        ``is_governance_pinned_off`` returns True both for a real policy deny and
        for a ceiling it could not evaluate. Gating withdrawal on it would mean a
        transient policy-read failure could leave a dashboard published on a
        tailnet with no supported way to take it down.
        """
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=True
        ), patch.object(
            tailnet_serve, "serve_state", return_value=ServeState(True, True, "ours")
        ):
            assert tailnet_serve.unpublish(_PORT).ok
        m.assert_called_once()


class TestFailureReporting:
    @pytest.mark.parametrize(
        "stderr,expected_code",
        [
            ("access denied: serve config", "no_permission"),
            ("must be run as root", "no_permission"),
            ("tailscaled is not running", "daemon_unavailable"),
            ("cannot connect to local tailscaled", "daemon_unavailable"),
            ("something nobody predicted", "failed"),
        ],
    )
    def test_classification(self, stderr: str, expected_code: str) -> None:
        cli, run = _patch_cli(return_value=_proc(returncode=1, stderr=stderr))
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            result = tailnet_serve.publish(_PORT)
        assert not result.ok
        assert result.code == expected_code

    @pytest.mark.parametrize(
        "stderr",
        ["access denied: serve config", "something nobody predicted"],
    )
    def test_daemon_wording_is_never_swallowed(self, stderr: str) -> None:
        """The classifier is a hint; the daemon's own words are the answer.

        Upstream owns this phrasing and can change it, so a wrong classification
        must still leave the operator with the real reason.
        """
        cli, run = _patch_cli(return_value=_proc(returncode=1, stderr=stderr))
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            result = tailnet_serve.publish(_PORT)
        assert stderr in result.detail

    def test_permission_failure_names_the_remedy(self) -> None:
        cli, run = _patch_cli(return_value=_proc(returncode=1, stderr="access denied"))
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            result = tailnet_serve.publish(_PORT)
        assert "--operator" in result.detail

    def test_timeout_does_not_claim_nothing_happened(self) -> None:
        """A timeout is ambiguous and must be reported as ambiguous.

        ``tailscale serve`` may well have applied the config before we gave up, so
        telling the operator it failed would invite a confusing retry.
        """
        cli, run = _patch_cli(side_effect=subprocess.TimeoutExpired("tailscale", 15))
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            result = tailnet_serve.publish(_PORT)
        assert result.code == "timeout"
        assert "may still have applied" in result.detail

    def test_missing_cli_is_its_own_code(self) -> None:
        with patch.object(tailnet_serve, "_cli_path", return_value=None), patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            result = tailnet_serve.publish(_PORT)
        assert result.code == "no_cli"
        assert not result.ok


class TestSpawnHardening:
    def test_path_is_never_consulted(self) -> None:
        """Binary resolution is shared with the read path, which forbids PATH.

        A ``PATH`` lookup would make the executable itself agent-selectable
        (``~/.local/bin`` is both on ``PATH`` and agent-writable), which is the
        defect the read path was hardened against. Asserted here too so the two
        cannot drift apart unnoticed.
        """
        with patch("shutil.which") as which, patch.object(
            tailnet_serve.subprocess, "run", return_value=_proc()
        ), patch.object(tailnet_serve, "is_governance_pinned_off", return_value=False):
            tailnet_serve.publish(_PORT)
        which.assert_not_called()

    def test_credentials_are_not_inherited(self) -> None:
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.dict(
            "os.environ", {"AWS_SECRET_ACCESS_KEY": "leaked"}, clear=False
        ), patch.object(tailnet_serve, "is_governance_pinned_off", return_value=False):
            tailnet_serve.publish(_PORT)
        env = m.call_args.kwargs["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in env
        # A non-empty env is part of the contract: emptying it would break the
        # CLI's own daemon lookup on macOS and Windows.
        assert env

    def test_no_shell_and_no_cwd(self) -> None:
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            tailnet_serve.publish(_PORT)
        assert m.call_args.kwargs.get("shell") in (None, False)
        assert m.call_args.kwargs.get("cwd") is None


class TestPublishDoesNotOverwrite:
    """`serve --bg --https=443 <target>` REPLACES the handler at that mount.

    Guarding only the withdrawal side was an asymmetry, not a decision: publishing
    over an operator's own service loses their configuration exactly as a port-wide
    `off` would have deleted it.
    """

    def _publish_with_state(self, state: ServeState):
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "serve_state", return_value=state
        ), patch.object(tailnet_serve, "is_governance_pinned_off", return_value=False):
            return tailnet_serve.publish(_PORT), m

    def test_a_free_mount_is_published(self) -> None:
        result, run = self._publish_with_state(ServeState(False, False, "nothing configured"))
        assert result.ok
        run.assert_called_once()

    def test_republishing_our_own_mount_is_allowed(self) -> None:
        """Idempotent: `up` twice must not be a refusal."""
        result, run = self._publish_with_state(ServeState(True, True, "ours"))
        assert result.ok
        run.assert_called_once()

    def test_someone_elses_mount_is_not_overwritten(self) -> None:
        result, run = self._publish_with_state(ServeState(False, True, "not this dashboard"))
        assert not result.ok
        assert result.code == "not_ours"
        run.assert_not_called()

    def test_an_unattributable_mount_is_not_overwritten(self) -> None:
        """The daemon answered, but this build cannot say what is there → refuse."""
        result, run = self._publish_with_state(ServeState(None, True, "unreadable shape"))
        assert not result.ok
        assert result.code == "not_ours"
        run.assert_not_called()

    def test_an_unreachable_daemon_does_not_masquerade_as_occupancy(self) -> None:
        """`configured is None` means no usable answer — a different failure.

        Refusing here would replace the authoritative reason (which the publish call
        reports verbatim) with a vague "could not confirm the mount is free".
        """
        cli, run = _patch_cli(return_value=_proc(returncode=1, stderr="tailscaled is not running"))
        with cli, run as m, patch.object(
            tailnet_serve, "serve_state", return_value=ServeState(None, None, "daemon down")
        ), patch.object(tailnet_serve, "is_governance_pinned_off", return_value=False):
            result = tailnet_serve.publish(_PORT)
        m.assert_called_once()
        assert result.code == "daemon_unavailable"
        assert "tailscaled is not running" in result.detail


class TestLaunchFailureIsNotReportedAsMissing:
    """A binary that exists but cannot be launched is NOT "tailscale not found".

    Both used to collapse to the same synthetic return code, so an `OSError` from
    the spawn was reported as "not installed" about a binary sitting right there.
    A Windows CI run produced exactly that, which is what surfaced it.
    """

    def test_publish_says_it_could_not_launch(self) -> None:
        cli, run = _patch_cli(side_effect=OSError("Exec format error"))
        with cli, run, patch.object(
            tailnet_serve, "serve_state", return_value=ServeState(False, False, "free")
        ), patch.object(tailnet_serve, "is_governance_pinned_off", return_value=False):
            result = tailnet_serve.publish(_PORT)
        assert result.code != "no_cli"
        assert "could not be launched" in result.detail
        assert "Exec format error" in result.detail

    def test_serve_state_says_it_could_not_launch(self) -> None:
        cli, run = _patch_cli(side_effect=OSError("Exec format error"))
        with cli, run:
            state = tailnet_serve.serve_state(_PORT)
        assert state.published is None
        assert "could not be launched" in state.detail
        assert "Exec format error" in state.detail

    def test_a_genuinely_missing_binary_still_says_not_found(self) -> None:
        with patch.object(tailnet_serve, "_cli_path", return_value=None), patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=False
        ):
            assert tailnet_serve.publish(_PORT).code == "no_cli"


class TestWithdrawalSafety:
    """`--https 443 off` removes whatever is on 443, not "the mapping we made".

    An earlier revision of `unpublish` documented the narrow behaviour it did not
    have, so an operator who had published something else on 443 by hand would
    have had it deleted by a command that claimed to leave it alone. Withdrawal
    now requires positive confirmation that 443 is fronting THIS dashboard.
    """

    def _unpublish_with_state(self, state: ServeState):
        cli, run = _patch_cli(return_value=_proc())
        with cli, run as m, patch.object(
            tailnet_serve, "serve_state", return_value=state
        ):
            return tailnet_serve.unpublish(_PORT), m

    def test_confirmed_ours_is_withdrawn(self) -> None:
        result, run = self._unpublish_with_state(ServeState(True, True, "ours"))
        assert result.ok
        run.assert_called_once()

    def test_someone_elses_mapping_is_never_touched(self) -> None:
        result, run = self._unpublish_with_state(
            ServeState(False, True, "Serve is configured, but not for this dashboard's port.")
        )
        assert not result.ok
        assert result.code == "not_ours"
        run.assert_not_called()

    def test_an_undetermined_state_also_refuses(self) -> None:
        """"Could not tell" must not be read as "go ahead".

        The costs are not symmetric: wrongly proceeding destroys configuration the
        operator has to rebuild from memory, while wrongly refusing costs one
        copy-pasted command — which the refusal prints.
        """
        result, run = self._unpublish_with_state(ServeState(None, None, "daemon down"))
        assert not result.ok
        assert result.code == "not_ours"
        run.assert_not_called()

    def test_a_refusal_hands_over_the_manual_command(self) -> None:
        result, _run = self._unpublish_with_state(ServeState(None, None, "daemon down"))
        assert "tailscale serve --https 443 --set-path=/ off" in result.detail

    def test_nothing_configured_is_an_idempotent_success(self) -> None:
        """The caller's goal ("not published") already holds, so do nothing."""
        result, run = self._unpublish_with_state(ServeState(False, False, "no config"))
        assert result.ok
        run.assert_not_called()


class TestPublishedDetection:
    def _state(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        cli, run = _patch_cli(return_value=_proc(stdout, returncode, stderr))
        with cli, run:
            return tailnet_serve.serve_state(_PORT)

    def test_finds_our_port_at_any_depth(self) -> None:
        """Shape-agnostic on purpose — the real schema is unverified here."""
        doc = {
            "Web": {
                "desk.tail1a2b3c.ts.net:443": {
                    "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{_PORT}"}}
                }
            }
        }
        assert self._state(json.dumps(doc)).published is True

    def test_the_dashboard_on_another_https_port_is_not_ours_on_443(self) -> None:
        """The defect a document-wide search produced.

        Dashboard served on HTTPS 8443, something unrelated on 443. Answering "is
        the dashboard served anywhere" said "ours", and the withdrawal then removed
        the unrelated 443 mapping. The question has to be scoped to 443.
        """
        doc = {
            "Web": {
                "desk.tail1a2b3c.ts.net:8443": {
                    "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{_PORT}"}}
                },
                "desk.tail1a2b3c.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}
                },
            }
        }
        assert self._state(json.dumps(doc)).published is False

    def test_accepts_localhost_spelling(self) -> None:
        assert self._state(_doc(f"http://localhost:{_PORT}")).published is True

    def test_tolerates_a_trailing_slash(self) -> None:
        assert self._state(_doc(f"http://127.0.0.1:{_PORT}/")).published is True

    def test_another_port_is_not_ours(self) -> None:
        assert self._state(_doc("http://127.0.0.1:9999")).published is False

    def test_a_port_prefix_is_not_a_match(self) -> None:
        """``54760`` must not satisfy a query for ``5476``."""
        assert self._state(_doc(f"http://127.0.0.1:{_PORT}0")).published is False

    def test_a_bare_numeric_443_key_also_scopes(self) -> None:
        """The TCP-style ``"443"`` key shape, not just ``"host:443"``."""
        doc = {"TCP": {"443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{_PORT}"}}}}}
        assert self._state(json.dumps(doc)).published is True

    def test_empty_output_is_a_real_negative(self) -> None:
        assert self._state("").published is False

    def test_empty_document_is_a_real_negative(self) -> None:
        st = self._state("{}")
        assert st.published is False
        # `configured` is what separates "nothing is served" from "something is
        # served and it is not ours" — the distinction unpublish() relies on.
        assert st.configured is False

    def test_configured_but_not_ours_is_distinguishable(self) -> None:
        st = self._state(_doc("http://127.0.0.1:9999"))
        assert st.published is False
        assert st.configured is True

    def test_our_handler_beside_a_strangers_at_the_mount_is_not_ours(self) -> None:
        """Ours at `/api`, a stranger's at `/` — the mount we would remove.

        Scoping to the port alone said "ours" here, and the withdrawal would then
        have deleted the stranger's handler at `/`.
        """
        doc = {
            "Web": {
                f"desk.tail1a2b3c.ts.net:{tailnet_serve.SERVE_HTTPS_PORT}": {
                    "Handlers": {
                        "/api": {"Proxy": f"http://127.0.0.1:{_PORT}"},
                        "/": {"Proxy": "http://127.0.0.1:3000"},
                    }
                }
            }
        }
        assert self._state(json.dumps(doc)).published is False

    def test_a_443_subtree_without_our_mount_is_unknown(self) -> None:
        """Something is on 443 but the handler map is unrecognisable → refuse."""
        doc = {"Web": {f"h:{tailnet_serve.SERVE_HTTPS_PORT}": {"Opaque": "shape"}}}
        assert self._state(json.dumps(doc)).published is None

    def test_no_443_key_at_all_is_unknown_not_negative(self) -> None:
        """Serve has config, but nothing this build can attribute to 443.

        Reported as unknown so withdrawal refuses. The assumption being made — a
        mapping on 443 names 443 in some key — holds for anything `publish` created,
        and when it does not hold the cost is a refusal, not a deleted mapping.
        """
        st = self._state(json.dumps({"Foreign": {"proxy": f"http://127.0.0.1:{_PORT}"}}))
        assert st.published is None
        assert st.configured is True

    @pytest.mark.parametrize("stdout", ["not json at all", "<html>nope</html>"])
    def test_unreadable_output_is_unknown_not_negative(self, stdout: str) -> None:
        assert self._state(stdout).published is None

    def test_missing_cli_is_unknown(self) -> None:
        with patch.object(tailnet_serve, "_cli_path", return_value=None):
            assert tailnet_serve.serve_state(_PORT).published is None

    def test_nonzero_exit_is_unknown_and_keeps_the_reason(self) -> None:
        state = self._state(returncode=1, stderr="daemon busy")
        assert state.published is None
        assert "daemon busy" in state.detail

    def test_status_read_is_not_governance_gated(self) -> None:
        """Inspection is never refused — only the action is.

        Gating the read would make a pinned host unable to see that it is pinned,
        which is the opposite of what the ceiling's own reporting needs.
        """
        cli, run = _patch_cli(return_value=_proc("{}"))
        with cli, run, patch.object(
            tailnet_serve, "is_governance_pinned_off", return_value=True
        ) as probe:
            assert tailnet_serve.serve_state(_PORT).published is False
        probe.assert_not_called()

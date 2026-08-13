"""Tailnet origin derivation: a name that reaches the allowlist must be earned.

The value under test travels from a subprocess into the CSRF origin set and the
DNS-rebinding ``Host`` barrier, so the tests are weighted accordingly:

* :class:`TestValidation` is an injection-rejection suite, not a formatting one.
  Anything carrying a scheme, port, path, credentials, whitespace or uppercase
  must be refused, and so must a plausible hostname outside ``*.ts.net`` —
  "looks like a hostname" is not "is a tailnet name".
* :class:`TestFailureModes` pins that **nothing raises**. A host that has never
  installed Tailscale, a stopped daemon, a timeout, a non-zero exit and garbage
  on stdout all have to degrade to "contributes nothing", because the dashboard
  has to start regardless.
* :class:`TestOriginSet` pins that the contribution lands as `https://` with no
  port, and that the ``Host`` allowlist follows without a second change.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.urls import build_allowed_hosts, build_allowed_origins

_GOOD = "desk.tail1a2b3c.ts.net"
_SUFFIX = "tail1a2b3c.ts.net"


def _status(dns_name: object, suffix: object = _SUFFIX, *, current_tailnet: bool = True) -> str:
    body: dict = {"Self": {"DNSName": dns_name}}
    if current_tailnet:
        body["CurrentTailnet"] = {"MagicDNSSuffix": suffix}
    return json.dumps(body)


def _fake_proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=["tailscale"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestValidation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (_GOOD, _GOOD),
            (f"{_GOOD}.", _GOOD),          # upstream documents the trailing dot
            (f"  {_GOOD}  ", _GOOD),       # surrounding whitespace is trimmed
        ],
    )
    def test_accepts_a_name_under_the_reported_suffix(self, raw: str, expected: str) -> None:
        assert tailnet._valid_magicdns_name(raw, _SUFFIX) == expected

    def test_accepts_a_non_ts_net_tailnet(self) -> None:
        """Upstream's own suffix example is ``userfoo.tailscale.net``.

        A hardcoded ``.ts.net`` requirement would reject this legitimate tailnet,
        which is why the check is self-consistency against the reported suffix.
        """
        assert (
            tailnet._valid_magicdns_name("desk.userfoo.tailscale.net", "userfoo.tailscale.net")
            == "desk.userfoo.tailscale.net"
        )

    def test_accepts_a_self_hosted_suffix(self) -> None:
        """Falls out of the same rule — no special case for Headscale."""
        assert (
            tailnet._valid_magicdns_name("desk.net.example.org", "net.example.org")
            == "desk.net.example.org"
        )

    @pytest.mark.parametrize("suffix", [_SUFFIX, f".{_SUFFIX}.", f"  {_SUFFIX}  "])
    def test_suffix_shape_is_normalised_not_trusted(self, suffix: str) -> None:
        assert tailnet._valid_magicdns_name(_GOOD, suffix) == _GOOD

    @pytest.mark.parametrize(
        "raw",
        [
            "https://desk.tail1a2b3c.ts.net",   # scheme
            "desk.tail1a2b3c.ts.net:8443",      # port
            "desk.tail1a2b3c.ts.net/admin",     # path
            "user@desk.tail1a2b3c.ts.net",      # userinfo
            "desk.tail1a2b3c.ts.net?x=1",       # query
            "desk.tail1a2b3c.ts.net#f",         # fragment
            "desk tail1a2b3c.ts.net",           # space
            "desk\ttail1a2b3c.ts.net",          # tab
            "desk\ntail1a2b3c.ts.net",          # newline
            "desk\\tail1a2b3c.ts.net",          # backslash
            "DESK.tail1a2b3c.ts.net",           # uppercase
            "-bad.tail1a2b3c.ts.net",           # label may not start with a hyphen
            "",
            "   ",
        ],
    )
    def test_rejects_structurally_bad_names(self, raw: str) -> None:
        assert tailnet._valid_magicdns_name(raw, _SUFFIX) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "evil.example.com",                     # different domain entirely
            "desk.tail1a2b3c.ts.net.evil.com",      # suffix present but not a suffix
            "tail1a2b3c.ts.net",                    # the bare suffix, no host label
            "desk.other9z8y7x.ts.net",              # a DIFFERENT tailnet's suffix
        ],
    )
    def test_rejects_names_outside_the_reported_suffix(self, raw: str) -> None:
        assert tailnet._valid_magicdns_name(raw, _SUFFIX) is None

    @pytest.mark.parametrize("suffix", [None, "", "   ", 42, [], {}])
    def test_rejects_a_missing_or_non_string_suffix(self, suffix: object) -> None:
        """No tailnet reported means no origin to add."""
        assert tailnet._valid_magicdns_name(_GOOD, suffix) is None

    @pytest.mark.parametrize("raw", [None, 42, [], {}, True])
    def test_rejects_non_strings(self, raw: object) -> None:
        assert tailnet._valid_magicdns_name(raw, _SUFFIX) is None

    def test_rejects_an_over_long_name(self) -> None:
        assert tailnet._valid_magicdns_name("a" * 250 + f".{_SUFFIX}", _SUFFIX) is None


class TestFailureModes:
    """Every one of these must return None, and none may raise."""

    def test_cli_absent(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value=None):
            assert tailnet.self_dns_name() is None

    def test_timeout(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("tailscale", 3.0)
        ):
            assert tailnet.self_dns_name() is None

    def test_oserror_on_exec(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", side_effect=OSError("permission denied")
        ):
            assert tailnet.self_dns_name() is None

    def test_non_zero_exit(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", return_value=_fake_proc(returncode=1, stderr="not running")
        ):
            assert tailnet.self_dns_name() is None

    @pytest.mark.parametrize(
        "stdout",
        [
            "",                      # empty
            "not json at all",       # garbage
            "[]",                    # JSON, wrong top-level type
            '"a string"',            # JSON, wrong top-level type
            "{}",                    # no Self
            '{"Self": null}',        # Self present but not a dict
            '{"Self": {}}',          # Self present, no DNSName
        ],
    )
    def test_unusable_output(self, stdout: str) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", return_value=_fake_proc(stdout=stdout)
        ):
            assert tailnet.self_dns_name() is None

    def test_logged_out_node_has_no_tailnet_and_so_no_origin(self) -> None:
        """``CurrentTailnet`` is nil until the node joins a tailnet."""
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run",
            return_value=_fake_proc(stdout=_status(f"{_GOOD}.", current_tailnet=False)),
        ):
            assert tailnet.self_dns_name() is None

    def test_falls_back_to_the_deprecated_top_level_suffix(self) -> None:
        """An older daemon reports only the deprecated top-level MagicDNSSuffix."""
        legacy = json.dumps({"Self": {"DNSName": f"{_GOOD}."}, "MagicDNSSuffix": _SUFFIX})
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", return_value=_fake_proc(stdout=legacy)
        ):
            assert tailnet.self_dns_name() == _GOOD

    def test_name_from_a_different_tailnet_than_reported_is_refused(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run",
            return_value=_fake_proc(stdout=_status("desk.other9z8y7x.ts.net.", _SUFFIX)),
        ):
            assert tailnet.self_dns_name() is None

    def test_a_hostile_name_in_otherwise_valid_output_is_refused(self) -> None:
        """The daemon is trusted to be the daemon, not to be well-behaved."""
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run",
            return_value=_fake_proc(stdout=_status("evil.example.com:1234/x")),
        ):
            assert tailnet.self_dns_name() is None

    def test_happy_path(self) -> None:
        with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"), patch(
            "subprocess.run", return_value=_fake_proc(stdout=_status(f"{_GOOD}."))
        ):
            assert tailnet.self_dns_name() == _GOOD
            assert tailnet.tailnet_origin() == f"https://{_GOOD}"


class TestResolveEntryPoint:
    @pytest.mark.asyncio
    async def test_disabled_never_touches_the_daemon(self) -> None:
        """Short-circuits before the thread hop, so a non-user pays nothing."""
        with patch.object(tailnet, "self_dns_name") as probe:
            assert await tailnet.resolve_tailnet_host(False) == ""
        probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_resolves_off_the_event_loop(self) -> None:
        """The subprocess must not run inline — it blocks for seconds."""
        import asyncio

        loop_thread = None

        def _record() -> str:
            nonlocal loop_thread
            import threading

            loop_thread = threading.current_thread()
            return _GOOD

        main_thread = __import__("threading").current_thread()
        with patch.object(tailnet, "self_dns_name", side_effect=_record):
            assert await tailnet.resolve_tailnet_host(True) == _GOOD
        assert loop_thread is not None and loop_thread is not main_thread
        assert asyncio.get_running_loop() is not None

    @pytest.mark.asyncio
    async def test_enabled_but_unresolvable_yields_empty_string(self) -> None:
        with patch.object(tailnet, "self_dns_name", return_value=None):
            assert await tailnet.resolve_tailnet_host(True) == ""

    @pytest.mark.asyncio
    async def test_enabled_but_unresolvable_warns(self, caplog) -> None:
        """An explicit opt-in that resolves to nothing must not fail silently.

        Debug-level silence is correct for a host that never opted in; for one
        that did, it reproduces the bare 403 this feature removes with nothing
        above debug explaining it.
        """
        with patch.object(tailnet, "self_dns_name", return_value=None):
            with caplog.at_level("WARNING", logger=tailnet.logger.name):
                assert await tailnet.resolve_tailnet_host(True) == ""
        assert any(r.levelname == "WARNING" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_disabled_stays_silent(self, caplog) -> None:
        """The off path must emit nothing — that is what keeps the warning useful."""
        with caplog.at_level("WARNING", logger=tailnet.logger.name):
            assert await tailnet.resolve_tailnet_host(False) == ""
        assert not caplog.records


class TestSpawnHardening:
    """The binary is pinned and the environment is scrubbed.

    Both defend the same reachable path: the feature is enabled, the dashboard
    starts, and whatever ``_run_json`` executes runs inside the gateway process.
    """

    def test_path_is_never_consulted(self) -> None:
        """A planted binary on ``PATH`` must not be selected.

        ``~/.local/bin`` is on ``PATH`` on a normal dev box and is agent-writable,
        so resolving through ``PATH`` would let an agent choose the executable the
        gateway runs at startup. Arguments were never agent-influenced; the binary
        was.
        """
        with patch("shutil.which", return_value="/tmp/planted/tailscale") as which:
            with patch("os.path.isfile", return_value=False):
                assert tailnet._cli_path() is None
        which.assert_not_called()

    def test_candidates_are_absolute_and_vetted(self) -> None:
        for candidate in tailnet._CLI_CANDIDATE_PATHS:
            assert candidate.startswith(("/", "C:\\")), candidate
            assert "tailscale" in candidate.lower(), candidate

    def test_credentials_are_not_inherited(self) -> None:
        """The child gets a scrubbed environment, not ``os.environ``."""
        witness = "AWS_SECRET_ACCESS_KEY"
        with patch.dict(os.environ, {witness: "must-not-leak"}, clear=False):
            with patch.object(tailnet, "_cli_path", return_value="/usr/bin/tailscale"):
                with patch("subprocess.run") as run:
                    run.return_value = SimpleNamespace(returncode=0, stdout="{}", stderr="")
                    tailnet._run_json(["status", "--json"])
        passed_env = run.call_args.kwargs["env"]
        assert witness not in passed_env
        # Still a real environment, not an empty one — over-scrubbing breaks the
        # macOS and Windows CLIs, which need HOME / SystemRoot to find the daemon.
        assert passed_env


class TestOriginSet:
    def test_absent_by_default(self) -> None:
        """Passing ``tailnet_host`` adds exactly one origin and nothing else.

        Stated as set arithmetic rather than a substring scan over each origin:
        the difference pins both halves at once — the default set contributes
        nothing tailnet-ish, and the opt-in contributes no second entry.
        """
        baseline = build_allowed_origins(5476, local_only=True)
        contributed = build_allowed_origins(5476, local_only=True, tailnet_host=_GOOD)
        assert contributed - baseline == {f"https://{_GOOD}"}

    def test_contributed_as_https_without_a_port(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, tailnet_host=_GOOD)
        assert f"https://{_GOOD}" in origins
        assert f"https://{_GOOD}:5476" not in origins

    def test_host_barrier_follows_without_a_second_change(self) -> None:
        """build_allowed_hosts derives from the origin set — the RFC's invariant."""
        origins = build_allowed_origins(5476, local_only=True, tailnet_host=_GOOD)
        assert _GOOD in build_allowed_hosts(origins)

    def test_coexists_with_dashboard_url(self) -> None:
        """A re-derivation for dashboard_url must not drop the tailnet origin."""
        origins = build_allowed_origins(
            5476,
            local_only=True,
            dashboard_url="https://crew.example.com",
            tailnet_host=_GOOD,
        )
        assert origins.issuperset({f"https://{_GOOD}", "https://crew.example.com"})

    def test_loopback_floor_is_untouched(self) -> None:
        origins = build_allowed_origins(5476, local_only=True, tailnet_host=_GOOD)
        assert "http://127.0.0.1:5476" in origins
        assert "http://localhost:5476" in origins

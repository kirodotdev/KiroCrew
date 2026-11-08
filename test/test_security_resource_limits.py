"""Tests for kiro_crew.security: ``apply_resource_limits``, the preexec that caps a child.

The rlimit probes spawn a real Python child under the preexec, so the class runs only where the
POSIX ``resource`` module exists.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from kiro_crew.security import apply_resource_limits

# resource is POSIX-only. Import it conditionally + skip ONLY the class below
# via skipif, so on non-POSIX platforms this module still imports and its tests
# report as skipped.
try:
    import resource as _resource_mod
except ImportError:
    _resource_mod = None


@pytest.mark.skipif(_resource_mod is None, reason="resource module is POSIX-only")
class TestApplyResourceLimits:
    """apply_resource_limits() returns a preexec_fn that caps a child's
    resources. These tests pin its behavior AND its wiring guarantees, so it
    cannot drift into dead code."""

    def test_returns_callable(self) -> None:
        assert callable(apply_resource_limits())
        assert callable(apply_resource_limits({"resource_limits": {"max_processes": 64}}))

    def test_bias_helper_writes_oom_score_adj(self) -> None:
        """In-process check of the helper: opens /proc/self/oom_score_adj
        write-only and writes b"1000" (intercepted — we must not re-bias the
        test worker itself)."""
        from unittest.mock import patch

        from kiro_crew.security import _bias_child_oom_score

        calls: dict = {}

        def fake_open(path, flags):
            calls["path"] = path
            calls["flags"] = flags
            return 42

        with (
            patch("kiro_crew.security.sys.platform", "linux"),
            patch("kiro_crew.security.os.open", side_effect=fake_open),
            patch("kiro_crew.security.os.write", return_value=4) as mwrite,
            patch("kiro_crew.security.os.close") as mclose,
        ):
            _bias_child_oom_score()
        assert calls["path"] == "/proc/self/oom_score_adj"
        assert calls["flags"] == os.O_WRONLY
        mwrite.assert_called_once_with(42, b"1000")
        mclose.assert_called_once_with(42)

    def test_bias_helper_swallows_oserror(self) -> None:
        """A read-only /proc or containerized denial must never fail the spawn."""
        from unittest.mock import patch

        from kiro_crew.security import _bias_child_oom_score

        with (
            patch("kiro_crew.security.sys.platform", "linux"),
            patch("kiro_crew.security.os.open", side_effect=OSError("denied")),
        ):
            _bias_child_oom_score()  # must not raise

    def test_bias_helper_noop_off_linux(self) -> None:
        from unittest.mock import patch

        from kiro_crew.security import _bias_child_oom_score

        with (
            patch("kiro_crew.security.sys.platform", "darwin"),
            patch("kiro_crew.security.os.open") as mopen,
        ):
            _bias_child_oom_score()
        mopen.assert_not_called()

    @staticmethod
    def _fake_resource(hard: int, *, reject: bool = False):
        """A stand-in ``resource`` module so the preexec body runs IN-PROCESS.

        The real closure runs post-fork in the child, where coverage cannot
        see it and where calling it here would cap the test worker itself.
        """
        from types import SimpleNamespace

        calls: list[tuple[int, tuple[int, int]]] = []

        def setrlimit(res_id, limits):
            if reject:
                raise ValueError("kernel rejected")
            calls.append((res_id, limits))

        fake = SimpleNamespace(
            RLIM_INFINITY=-1,
            RLIMIT_NOFILE=7,
            getrlimit=lambda _res_id: (100, hard),
            setrlimit=setrlimit,
        )
        return fake, calls

    def test_preexec_clamps_to_the_inherited_hard_cap_and_pins_both_limits(self) -> None:
        """A request above the hard cap tightens to it; soft AND hard are set so the
        child cannot raise its own soft limit back up."""
        from unittest.mock import patch

        from kiro_crew.security import helpers

        fake, calls = self._fake_resource(hard=512)
        with (
            patch.object(helpers, "_resource", fake),
            patch.object(helpers, "_bias_child_oom_score") as bias,
        ):
            apply_resource_limits({"resource_limits": {"max_open_files": 4096}})()
        assert calls == [(7, (512, 512))]
        bias.assert_called_once_with()

    def test_preexec_leaves_a_request_under_an_infinite_hard_cap_alone(self) -> None:
        from unittest.mock import patch

        from kiro_crew.security import helpers

        fake, calls = self._fake_resource(hard=-1)
        with (
            patch.object(helpers, "_resource", fake),
            patch.object(helpers, "_bias_child_oom_score"),
        ):
            apply_resource_limits({"resource_limits": {"max_open_files": 4096}})()
        assert calls == [(7, (4096, 4096))]

    def test_preexec_swallows_a_rejected_rlimit_so_the_spawn_proceeds(self) -> None:
        from unittest.mock import patch

        from kiro_crew.security import helpers

        fake, calls = self._fake_resource(hard=512, reject=True)
        with (
            patch.object(helpers, "_resource", fake),
            patch.object(helpers, "_bias_child_oom_score") as bias,
        ):
            apply_resource_limits({"resource_limits": {"max_open_files": 4096}})()
        assert calls == []
        bias.assert_called_once_with()

    def test_preexec_is_a_noop_without_the_resource_module(self) -> None:
        """Windows has no ``resource``; the limiter must still be a callable."""
        from unittest.mock import patch

        from kiro_crew.security import helpers

        with (
            patch.object(helpers, "_resource", None),
            patch.object(helpers, "_bias_child_oom_score") as bias,
        ):
            limiter = apply_resource_limits({"resource_limits": {"max_open_files": 4096}})
            assert limiter() is None
        bias.assert_not_called()

    @pytest.mark.skipif(sys.platform != "linux", reason="oom_score_adj is Linux-only")
    def test_child_oom_score_adj_biased(self) -> None:
        """The preexec biases the OOM killer toward the child (oom_score_adj
        = 1000) so a memory-ballooning tool dies before the whole agent scope
        does. Descendants inherit the value automatically."""
        import subprocess

        out = subprocess.run(
            [sys.executable, "-c", "print(open('/proc/self/oom_score_adj').read().strip())"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(),
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "1000"

    def test_defaults_set_nofile_only(self) -> None:
        """With no config only NOFILE is capped (per-process, safe); NPROC/CPU/AS
        stay inherited (default 0 = disabled) so a long-lived Node agent on a
        busy UID is not EAGAIN/SIGXCPU/ENOMEM-killed."""
        import subprocess
        import sys

        inherited_nproc = _resource_mod.getrlimit(_resource_mod.RLIMIT_NPROC)[0]
        inherited_cpu = _resource_mod.getrlimit(_resource_mod.RLIMIT_CPU)[0]
        inherited_as = _resource_mod.getrlimit(_resource_mod.RLIMIT_AS)[0]
        probe = (
            "import resource,json;"
            "print(json.dumps({"
            "'nproc':resource.getrlimit(resource.RLIMIT_NPROC)[0],"
            "'nofile':resource.getrlimit(resource.RLIMIT_NOFILE)[0],"
            "'cpu':resource.getrlimit(resource.RLIMIT_CPU)[0],"
            "'as':resource.getrlimit(resource.RLIMIT_AS)[0],"
            "}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(),
        )
        assert out.returncode == 0, out.stderr
        limits = json.loads(out.stdout)
        assert limits["nofile"] == 1024
        # NPROC, CPU, AS disabled by default -> left exactly at the inherited
        # value (NOT clamped to a fixed cap). Assert equality to the parent's
        # inherited limit rather than a tautology that only excludes 0.
        assert limits["nproc"] == inherited_nproc
        assert limits["cpu"] == inherited_cpu
        assert limits["as"] == inherited_as

    def test_config_overrides_applied(self) -> None:
        import subprocess
        import sys

        # NOFILE is per-process so a small override (256, distinct from the 1024
        # default) is safe. NPROC is per-real-UID against the user's whole
        # process+thread count, so it MUST be requested well above any real
        # count — clamping min(requested, inherited_hard) down to the inherited
        # hard cap is always >= current usage (nothing could be running
        # otherwise), so the child can still fork. A small NPROC (e.g. 77) would
        # make the probe child fail to start on any busy/CI UID.
        nproc_hard = _resource_mod.getrlimit(_resource_mod.RLIMIT_NPROC)[1]
        nproc_req = 100_000
        expected_nproc = (
            nproc_req
            if nproc_hard == _resource_mod.RLIM_INFINITY or nproc_hard >= nproc_req
            else nproc_hard
        )
        if sys.platform == "darwin":
            # Darwin SILENTLY clamps a non-root setrlimit(RLIMIT_NPROC) to
            # kern.maxprocperuid, which can sit BELOW the inherited hard cap
            # (kern.maxproc) — e.g. 8000 vs a 12000 hard cap — so the child
            # observes the per-UID cap, not min(requested, hard), and this
            # assertion fails on every Mac while passing on Linux. Fold the
            # kernel cap into the expectation. (os.sysconf('SC_CHILD_MAX')
            # tracks the *soft rlimit*, not this cap — read the sysctl.)
            per_uid_cap = int(
                subprocess.run(
                    ["/usr/sbin/sysctl", "-n", "kern.maxprocperuid"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=True,
                ).stdout.strip()
            )
            expected_nproc = min(expected_nproc, per_uid_cap)
        cfg = {"resource_limits": {"max_processes": nproc_req, "max_open_files": 256}}
        probe = (
            "import resource,json;"
            "print(json.dumps({"
            "'nproc':resource.getrlimit(resource.RLIMIT_NPROC)[0],"
            "'nofile':resource.getrlimit(resource.RLIMIT_NOFILE)[0],"
            "}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(cfg),
        )
        assert out.returncode == 0, out.stderr
        limits = json.loads(out.stdout)
        assert limits["nproc"] == expected_nproc
        assert limits["nofile"] == 256

    def test_nofile_limit_actually_enforced(self) -> None:
        """The NOFILE cap is real: a child told it may open few FDs hits the
        ceiling."""
        import subprocess
        import sys

        probe = (
            "import sys\n"
            "fds=[]\n"
            "try:\n"
            "    for _ in range(200):\n"
            "        fds.append(open('/dev/null'))\n"
            "    print('opened-all')\n"
            "except OSError:\n"
            "    print('hit-limit')\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits({"resource_limits": {"max_open_files": 32}}),
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "hit-limit"

    def test_zero_disables_a_limit(self) -> None:
        """max_open_files=0 leaves NOFILE inherited (not clamped to the
        default), so an operator can opt a limit out."""
        import subprocess
        import sys

        inherited = _resource_mod.getrlimit(_resource_mod.RLIMIT_NOFILE)[0]
        probe = "import resource,json;" "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits({"resource_limits": {"max_open_files": 0}}),
        )
        assert out.returncode == 0, out.stderr
        assert int(out.stdout.strip()) == inherited

    def test_never_raises_above_inherited_hard_limit(self) -> None:
        """A request larger than the inherited hard cap is clamped down, so the
        setrlimit call cannot raise EPERM and abort the spawn."""
        import subprocess
        import sys

        hard = _resource_mod.getrlimit(_resource_mod.RLIMIT_NOFILE)[1]
        if hard == _resource_mod.RLIM_INFINITY:
            pytest.skip("NOFILE hard limit is unlimited; nothing to clamp against")
        probe = "import resource;print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(
                {"resource_limits": {"max_open_files": hard + 100_000}}
            ),
        )
        assert out.returncode == 0, out.stderr
        assert int(out.stdout.strip()) <= hard

    def test_junk_config_values_ignored(self) -> None:
        """Non-numeric / negative / bool values fall back to defaults rather
        than crashing or disabling protection."""
        import subprocess
        import sys

        inherited_nproc = _resource_mod.getrlimit(_resource_mod.RLIMIT_NPROC)[0]
        cfg = {"resource_limits": {"max_processes": "lots", "max_open_files": -5}}
        probe = (
            "import resource,json;"
            "print(json.dumps({"
            "'nproc':resource.getrlimit(resource.RLIMIT_NPROC)[0],"
            "'nofile':resource.getrlimit(resource.RLIMIT_NOFILE)[0],"
            "}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(cfg),
        )
        assert out.returncode == 0, out.stderr
        limits = json.loads(out.stdout)
        # Junk -> defaults retained: NOFILE default-on (1024); NPROC stays
        # disabled by default -> inherited (junk "lots" ignored, not clamped).
        assert limits["nproc"] == inherited_nproc
        assert limits["nofile"] == 1024

    def test_default_preexec_allows_child_to_fork(self) -> None:
        """Regression: the DEFAULT preexec must not cap RLIMIT_NPROC, because it
        is enforced per-real-UID against the user's existing process+thread
        count (often thousands on a shared/desktop UID). A fixed NPROC default
        tight enough to matter would make every child fail to fork with EAGAIN —
        strictly worse than the DoS gap it aims to close. Verify a spawned child
        under the default preexec can itself spawn a subprocess."""
        import subprocess
        import sys

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess,sys;"
                "subprocess.run([sys.executable,'-c','pass'],check=True);"
                "print('nested-fork-ok')",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            preexec_fn=apply_resource_limits(),
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "nested-fork-ok"

    def test_none_resource_module_is_noop(self, monkeypatch) -> None:
        """On non-POSIX (resource is None) the helper returns a harmless no-op."""
        import kiro_crew.security as sec

        monkeypatch.setattr(sec, "_resource", None)
        fn = sec.apply_resource_limits({"resource_limits": {"max_processes": 1}})
        assert fn() is None

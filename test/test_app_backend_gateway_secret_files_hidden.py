"""The install and probe spawns cannot read the gateway's internal-API secret files.

``.local_secret`` in the data home and every ``run/gateway-<port>.secret`` and
``run/gateway-<port>-<addr>.secret`` hold the same value. The spawns that run app code
before its backend starts pass ``sandbox.app_backend_secret_mask_targets()`` to
``wrap_argv`` as ``extra_hidden_dirs``, so the sandbox plan masks them. The running
backend keeps them readable (``test_app_backend_keeps_gateway_secret_readable.py``).

Only the arguments of the spawn and the sandbox plan are checked; no sandbox and no
process runs here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401
from test_sandbox_launcher_program import CoveringLibc, launch, payload

from kiro_crew import sandbox, sandbox_launcher_program, sandbox_plan

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox only")
_SECRET_NAMES = (
    ".local_secret",
    "run/gateway-8765.secret",
    "run/gateway-8765-127.0.0.1.secret",
)
_NOT_SECRET_NAMES = ("run/gateway-8765.pid", "run/gateway-8765.bin", "other.json")


def _seed(home: Path) -> None:
    (home / "run").mkdir(exist_ok=True)
    for name in (*_SECRET_NAMES, *_NOT_SECRET_NAMES):
        (home / name).write_text("x", encoding="utf-8")


@_POSIX_ONLY
class TestTheTargetsAndThePlan:
    def test_the_targets_list_only_files_that_exist(self, app_env):
        (app_env / "run").mkdir()
        (app_env / "run" / "gateway-1.secret").write_text("x", encoding="utf-8")

        targets = sandbox.app_backend_secret_mask_targets()

        assert str(app_env / "run" / "gateway-1.secret") in targets
        assert str(app_env / ".local_secret") not in targets

    def test_a_boot_spawn_names_this_gateways_files_before_the_gateway_writes_them(
        self, app_env, monkeypatch
    ):
        """A backend started at boot is spawned before ``_write_instance_credentials``."""
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)

        targets = sandbox.app_backend_secret_mask_targets()

        for name in (
            ".local_secret",
            "run/gateway-8765.secret",
            "run/gateway-8765-127.0.0.1.secret",
            "run/gateway-8765-__1.secret",
        ):
            assert str(app_env / name) in targets, name
            assert (app_env / name).read_bytes() == b"", name
            assert (app_env / name).stat().st_mode & 0o777 == 0o600, name
        assert str(app_env / "run" / "gateway-8765.pid") not in targets

    def test_the_names_cover_a_specific_bound_interface(self, app_env, monkeypatch):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
        monkeypatch.setenv("KIROCREW_BOUND_HOST", "192.0.2.7")

        targets = sandbox.app_backend_secret_mask_targets()

        assert str(app_env / "run" / "gateway-8765-192.0.2.7.secret") in targets

    def test_a_file_already_there_keeps_its_value(self, app_env, monkeypatch):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
        (app_env / "run").mkdir()
        (app_env / "run" / "gateway-8765.secret").write_text("live", encoding="utf-8")

        sandbox.app_backend_secret_mask_targets()

        assert (app_env / "run" / "gateway-8765.secret").read_text(encoding="utf-8") == "live"

    @pytest.mark.parametrize("bound", ["", "0", "70000", "abc", "-1"])
    def test_no_valid_bound_port_creates_nothing(self, app_env, monkeypatch, bound):
        monkeypatch.setenv("KIROCREW_BOUND_PORT", bound)

        sandbox.app_backend_secret_mask_targets()

        assert not (app_env / "run").exists()
        assert not (app_env / ".local_secret").exists()

    def test_a_restart_still_publishes_and_serves_the_credential(self, app_env, monkeypatch):
        """The normal boot order: spawn prep first, then the gateway writes its files.

        What the precreated empty names must not change: the gateway's own write, the
        port-keyed read and the listener-keyed read all see the new value.
        """
        from kiro_crew.config.loader_locations import read_local_secret
        from kiro_crew.dashboard import server
        from kiro_crew.instances import run_marker

        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8765")
        # A previous generation's clean shutdown removed its files.
        sandbox.app_backend_secret_mask_targets()
        monkeypatch.setattr(server, "_live_sibling_port", lambda own_port: None)

        server._write_instance_credentials(
            app_env / ".local_secret", 8765, "127.0.0.1", "new-secret", ("::1",)
        )

        assert (app_env / ".local_secret").read_text(encoding="utf-8") == "new-secret"
        assert run_marker.read_secret(8765) == "new-secret"
        assert read_local_secret(8765, "127.0.0.1") == "new-secret"
        assert read_local_secret(8765, "localhost") == "new-secret"
        # A name the gateway did not publish stays empty, which reads as "not covered".
        assert (app_env / "run" / "gateway-8765-0.0.0.0.secret").read_bytes() == b""

    def test_the_backend_plan_masks_the_files_and_the_plain_plan_does_not(self, app_env):
        _seed(app_env)
        targets = sandbox.app_backend_secret_mask_targets()
        backend_plan = sandbox._spawn_plan(
            sandbox_plan.BACKEND_NAMESPACE, "standard", extra_hidden_dirs=targets
        )
        plain_plan = sandbox._spawn_plan(sandbox_plan.BACKEND_NAMESPACE, "standard")

        masked = set(backend_plan.sensitive_dirs)
        for name in _SECRET_NAMES:
            assert str(app_env / name) in masked
        assert str(app_env / "run") not in masked
        # A spawn that passes no hidden paths keeps today's plan.
        for name in _SECRET_NAMES:
            assert str(app_env / name) not in set(plain_plan.sensitive_dirs)


@_POSIX_ONLY
class TestTheLauncherMasksAFileEntry:
    """A regular file in the hidden entries is bound over, not skipped as a non-directory.

    The plan offers every hidden path to both launcher loops (``sensitive_files`` is the
    files plus every masked tree), and each loop classifies the entry itself. The fake
    libc covers a bound name the way a mount does; no namespace and no process run.
    """

    def test_a_listed_regular_file_reads_empty_and_its_sibling_is_untouched(
        self, tmp_path: Path
    ) -> None:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        secret = run_dir / "gateway-8765.secret"
        secret.write_text("value", encoding="utf-8")
        sibling = run_dir / "gateway-8765.pid"
        sibling.write_text("42", encoding="utf-8")
        entry = str(secret)
        run = launch(
            tmp_path,
            payload(sensitive_dirs=[entry], sensitive_files=[entry]),
            libc=CoveringLibc(),
        )

        sandbox_launcher_program.mask_sensitive(run)
        sandbox_launcher_program.mask_sensitive_files(run)

        assert secret.read_text(encoding="utf-8") == ""
        assert sibling.read_text(encoding="utf-8") == "42"

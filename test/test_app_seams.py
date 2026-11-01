"""The core registration seams in :mod:`kiro_crew.apps.seams`, and aws-control's providers.

The registry is process-global, so every test that changes it does so through
``monkeypatch`` on ``seams._providers`` and the change is undone afterwards.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kiro_crew.apps import seams

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def registry(monkeypatch):
    """The real registry with the shipped providers loaded, restored after the test."""
    seams.crew_runtime()
    monkeypatch.setattr(seams, "_providers", dict(seams._providers))
    return seams._providers


class TestTheRegistry:
    def test_a_seam_nobody_provides_answers_none(self, registry):
        assert seams.provider("no-such-seam") is None
        assert seams.provider_app("no-such-seam") is None

    def test_a_registered_provider_answers(self, registry):
        marker = object()
        seams.register("probe", "app-a", marker)
        assert seams.provider("probe") is marker
        assert seams.provider_app("probe") == "app-a"

    def test_the_same_app_may_replace_its_own_provider(self, registry):
        seams.register("probe", "app-a", 1)
        seams.register("probe", "app-a", 2)
        assert seams.provider("probe") == 2

    def test_a_second_app_is_refused(self, registry):
        seams.register("probe", "app-a", 1)
        with pytest.raises(ValueError, match="already provided by app 'app-a'"):
            seams.register("probe", "app-b", 2)
        assert seams.provider("probe") == 1

    def test_registering_never_waits_on_the_builtin_load(self, registry):
        """A provider module imported on another thread registers while the load runs.

        The load holds its own lock across the import, and :func:`seams.register`
        must not need it, or that thread and the loading one would deadlock.
        """
        done = threading.Event()

        def register_elsewhere():
            seams.register("probe", "app-a", 1)
            done.set()

        with seams._load_lock:
            worker = threading.Thread(target=register_elsewhere, daemon=True)
            worker.start()
            assert done.wait(timeout=30), "register() blocked on the builtin-load lock"
        worker.join(timeout=30)
        assert seams.provider("probe") == 1

    def test_only_the_holder_can_unregister(self, registry):
        seams.register("probe", "app-a", 1)
        seams.unregister("probe", "app-b")
        assert seams.provider("probe") == 1
        seams.unregister("probe", "app-a")
        assert seams.provider("probe") is None


class TestTheShippedProviders:
    def test_aws_control_provides_all_three_seams(self):
        for seam in (
            seams.SEAM_CREW_RUNTIME,
            seams.SEAM_AWS_ACCOUNT_POLICY,
            seams.SEAM_TRANSFER_STAGING,
        ):
            assert seams.provider_app(seam) == "aws-control", seam

    def test_a_lookup_does_not_load_the_apps_backend(self):
        """A CLI process that asks for the runtime must not pay for the app's routes."""
        code = (
            "import sys\n"
            "from kiro_crew.apps import seams\n"
            "assert seams.crew_runtime() is not None\n"
            "loaded = sorted(m for m in sys.modules\n"
            "                if m.startswith('kiro_crew.apps.builtins.aws_control.backend'))\n"
            "print(loaded)\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=120,
            cwd=ROOT,
        )
        assert out.stdout.strip() == "[]", out.stdout

    def test_the_package_still_serves_register_routes(self):
        """The gateway's startup loop asks ``hasattr(pkg, "register_routes")``."""
        import kiro_crew.apps.builtins.aws_control as pkg
        from kiro_crew.apps.builtins.aws_control.backend.routes import register_routes

        assert pkg.register_routes is register_routes
        assert not hasattr(pkg, "on_disable")

    def test_the_account_policy_delegates_to_the_accounts_module(self, monkeypatch):
        """Looked up at call time, so a patch of ``accounts`` reaches core's callers."""
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        monkeypatch.setattr(accounts, "resolve_consent_target", AsyncMock(return_value=("p", "r")))
        monkeypatch.setattr(accounts, "configured_region", AsyncMock(return_value="eu-west-1"))
        monkeypatch.setattr(accounts, "classify_profile", AsyncMock(return_value=accounts.KIND_SSO))
        policy = seams.aws_account_policy()
        assert policy is not None
        assert asyncio.run(policy.resolve_consent_target()) == ("p", "r")
        assert asyncio.run(policy.configured_region("x")) == "eu-west-1"
        assert asyncio.run(policy.is_sso_profile("x")) is True
        monkeypatch.setattr(
            accounts, "classify_profile", AsyncMock(return_value=accounts.KIND_OTHER)
        )
        assert asyncio.run(policy.is_sso_profile("x")) is False

    def test_the_staging_provider_is_the_storage_modules(self, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        calls = []
        monkeypatch.setattr(storage, "pinned_staging", lambda prefix: calls.append(prefix) or 7)
        staging = seams.transfer_staging()
        assert staging is not None
        assert staging.pinned_staging("kirocrew-x-") == 7
        assert calls == ["kirocrew-x-"]

    def test_the_bundle_digest_is_the_packaging_builders(self, monkeypatch, tmp_path):
        from kiro_crew.apps.builtins.aws_control.crew.packaging import build

        monkeypatch.setattr(build, "bundle_digest", lambda path: f"digest:{path.name}")
        runtime = seams.crew_runtime()
        assert runtime is not None
        assert runtime.bundle_digest(tmp_path) == f"digest:{tmp_path.name}"


class TestCoreCallersWithNoProvider:
    """What each core caller does when no app provides its seam."""

    def test_the_consent_card_names_the_default_chain(self, registry):
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        del registry[seams.SEAM_AWS_ACCOUNT_POLICY]
        target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        assert target == ("", deploy_profiles.DEFAULT_REGION)

    def test_the_project_pill_reports_a_failed_probe_as_unavailable(self, registry, monkeypatch):
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers import aws_project_profile as handler
        from kiro_crew.deploy import profiles as deploy_profiles

        del registry[seams.SEAM_AWS_ACCOUNT_POLICY]
        seen = []

        async def probe(profile, region, **_kw):
            seen.append((profile, region))
            return aws_consent.Identity(ok=False, detail="no")

        monkeypatch.setattr(aws_consent, "probe_identity", probe)
        assert asyncio.run(handler._profile_status("dev")) == handler.STATUS_UNAVAILABLE
        assert seen == [("dev", deploy_profiles.DEFAULT_REGION)]

    def test_the_microvm_staging_refuses_rather_than_using_tmp(self, registry):
        from kiro_crew.cloud.microvm import api

        del registry[seams.SEAM_TRANSFER_STAGING]
        with pytest.raises(RuntimeError, match="no sandbox-masked staging root"):
            with api.staging_dir("kirocrew-x-"):
                pass

"""The aws-control app's providers for core's registration seams.

Core asks for these through :mod:`kiro_crew.apps.seams` and never imports this app.
That module imports this file the first time any seam is looked up, so it stays
light: it binds paths and small provider objects, and every provider method imports
the app module it delegates to when it is called. Each method looks its target up
on the owning module at call time, so a test that patches that module's function
patches what core reaches too.

A provider answers whether or not the app is enabled. The core features it serves
worked with the app switched off, and they still do.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from kiro_crew.apps import seams

#: The manifest name, which is the id every seam records its provider under.
APP_ID = "aws-control"

#: The crew container runtime: the Docker build context the remote-crew images are
#: built from (``Dockerfile``, ``Dockerfile.crew``, ``Dockerfile.microvm``,
#: ``container/``, ``vendor/``).
CREW_RUNTIME_DIR = Path(__file__).resolve().parent / "crew" / "runtime"


class _CrewRuntime:
    """:class:`kiro_crew.apps.seams.CrewRuntime`, from ``crew/``."""

    @property
    def runtime_dir(self) -> Path:
        return CREW_RUNTIME_DIR

    def bundle_digest(self, bundle_dir: Path) -> str:
        from kiro_crew.apps.builtins.aws_control.crew.packaging import build

        return build.bundle_digest(bundle_dir)


class _AwsAccountPolicy:
    """:class:`kiro_crew.apps.seams.AwsAccountPolicy`, from ``backend/accounts.py``."""

    async def resolve_consent_target(self) -> tuple[str, str] | None:
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        return await accounts.resolve_consent_target()

    async def configured_region(self, profile: str) -> str:
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        return await accounts.configured_region(profile)

    async def is_sso_profile(self, profile: str) -> bool:
        from kiro_crew.apps.builtins.aws_control.backend import accounts

        return await accounts.classify_profile(profile) == accounts.KIND_SSO


class _TransferStaging:
    """:class:`kiro_crew.apps.seams.TransferStaging`, from ``backend/storage.py``."""

    def pinned_staging(self, prefix: str) -> AbstractContextManager[tuple[Path, int]]:
        from kiro_crew.apps.builtins.aws_control.backend import storage

        return storage.pinned_staging(prefix)


seams.register(seams.SEAM_CREW_RUNTIME, APP_ID, _CrewRuntime())
seams.register(seams.SEAM_AWS_ACCOUNT_POLICY, APP_ID, _AwsAccountPolicy())
seams.register(seams.SEAM_TRANSFER_STAGING, APP_ID, _TransferStaging())

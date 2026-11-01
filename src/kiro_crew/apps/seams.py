"""Registration seams a core feature asks and an app fills, so core never imports an app.

Some core features need something only an app ships. The MicroVM remote-crew lane
builds its image from the crew container runtime the aws-control app carries. The
paid-service consent cards and the composer's project pill resolve an AWS profile
through that app's account policy. The MicroVM control calls stage a secret under
the app's sandbox-masked transfer root. Core names each such need as a SEAM here and
asks for its provider when it runs. The app registers the provider, and core never
imports the app, so the app can leave this repository without core changing.

Empty by default. A seam that no app filled answers ``None``. Each core caller's own
docstring says what it does then: degrade to a fixed answer, or refuse.

How a provider gets here:

* A builtin app ships a ``seams.py`` at its package root that calls :func:`register`
  when it is imported. The first lookup imports every such module once, for each name
  in ``BUILTIN_NAMES``. A seam therefore answers the same in the gateway, in a CLI
  process and in a test, whatever was imported first. Core callers that never load
  the app's backend import that module, so it must stay light: heavy imports belong
  in the provider's methods, which run at call time.
* Any other in-process code, such as an installed app's ``on_startup`` hook, calls
  :func:`register` itself.

A provider answers whether or not its app is ENABLED. Every core feature above
worked with aws-control switched off, which is its default, and a seam must not
change what they do.

One provider per seam. A second app that registers a seam another app already holds
is refused, because otherwise the answer would depend on import order.
"""

from __future__ import annotations

import importlib
import logging
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, cast

logger = logging.getLogger(__name__)

#: The crew container runtime the remote-crew image is built from. Provider:
#: :class:`CrewRuntime`.
SEAM_CREW_RUNTIME = "crew_runtime"
#: Which AWS profile an account-level paid service runs under, and what kind of
#: sign-in a profile uses. Provider: :class:`AwsAccountPolicy`.
SEAM_AWS_ACCOUNT_POLICY = "aws_account_policy"
#: A private directory under a root every agent sandbox masks, for a file the AWS
#: CLI must read by name. Provider: :class:`TransferStaging`.
SEAM_TRANSFER_STAGING = "transfer_staging"

#: The module a builtin app ships at its package root to register its providers.
BUILTIN_SEAMS_MODULE = "seams"


class CrewRuntime(Protocol):
    """The crew container runtime: its directory on disk, and its bundle digest."""

    @property
    def runtime_dir(self) -> Path:
        """The directory that holds ``Dockerfile``, ``Dockerfile.crew`` and ``container/``."""
        ...

    def bundle_digest(self, bundle_dir: Path) -> str:
        """The digest the image cache keys a crew bundle on. Refusals propagate."""
        ...


class AwsAccountPolicy(Protocol):
    """How the app's account policy picks and describes an AWS profile."""

    async def resolve_consent_target(self) -> tuple[str, str] | None:
        """``(profile, region)`` an account-level paid service would bill, or ``None``."""
        ...

    async def configured_region(self, profile: str) -> str:
        """The region *profile* is configured with, or ``""``."""
        ...

    async def is_sso_profile(self, profile: str) -> bool:
        """Whether *profile* signs in through IAM Identity Center."""
        ...


class TransferStaging(Protocol):
    """A sandbox-masked staging directory for one AWS CLI call."""

    def pinned_staging(self, prefix: str) -> AbstractContextManager[tuple[Path, int]]:
        """A private directory and its pinned descriptor, removed on exit."""
        ...


#: Guards :data:`_providers`. Never held across an import: a seams module being
#: imported on another thread takes it in :func:`register` while it holds that
#: module's import lock.
_lock = threading.Lock()
#: seam -> (app id, provider).
_providers: dict[str, tuple[str, object]] = {}
#: Serializes the one-time import of the builtin seams modules. :func:`register`
#: never takes it, so a provider module can register while it is held. A seams
#: module must therefore only register while it is imported, never look a seam up.
_load_lock = threading.Lock()
_builtins_loaded = False
#: The thread running that import, so a lookup it makes re-entrantly returns at once.
_loading_thread: int | None = None


def register(seam: str, app_id: str, provider: object) -> None:
    """Make *provider* the answer to *seam*, on behalf of the app *app_id*.

    The same app registering again replaces its own provider, so a module that is
    imported twice does not fail. A different app is refused.
    """
    with _lock:
        held = _providers.get(seam)
        if held is not None and held[0] != app_id:
            raise ValueError(
                f"seam {seam!r} is already provided by app {held[0]!r}; "
                f"app {app_id!r} cannot provide it too"
            )
        _providers[seam] = (app_id, provider)


def unregister(seam: str, app_id: str) -> None:
    """Withdraw *app_id*'s provider for *seam*. A no-op when it holds none."""
    with _lock:
        held = _providers.get(seam)
        if held is not None and held[0] == app_id:
            del _providers[seam]


def provider_app(seam: str) -> str | None:
    """The id of the app that provides *seam*, or ``None``."""
    _load_builtin_seams()
    with _lock:
        held = _providers.get(seam)
    return held[0] if held is not None else None


def provider(seam: str) -> object | None:
    """The registered provider for *seam*, or ``None`` when no app provides it."""
    _load_builtin_seams()
    with _lock:
        held = _providers.get(seam)
    return held[1] if held is not None else None


def crew_runtime() -> CrewRuntime | None:
    """The :data:`SEAM_CREW_RUNTIME` provider, or ``None``."""
    return cast("CrewRuntime | None", provider(SEAM_CREW_RUNTIME))


def aws_account_policy() -> AwsAccountPolicy | None:
    """The :data:`SEAM_AWS_ACCOUNT_POLICY` provider, or ``None``."""
    return cast("AwsAccountPolicy | None", provider(SEAM_AWS_ACCOUNT_POLICY))


def transfer_staging() -> TransferStaging | None:
    """The :data:`SEAM_TRANSFER_STAGING` provider, or ``None``."""
    return cast("TransferStaging | None", provider(SEAM_TRANSFER_STAGING))


def _load_builtin_seams() -> None:
    """Import each shipped builtin's ``seams`` module, once per process.

    Found by FILE rather than by import, because asking the import system whether a
    submodule exists imports its package first. A broken provider module is logged
    and skipped, so one app cannot take a core feature down.

    A lookup on another thread waits on :data:`_load_lock` until the import ends, so
    it never sees a half-loaded registry. A lookup from a seams module mid-import, on
    the loading thread itself, returns at once with what is registered so far.
    """
    global _builtins_loaded, _loading_thread
    if _builtins_loaded or _loading_thread == threading.get_ident():
        return
    with _load_lock:
        if _builtins_loaded:
            return
        _loading_thread = threading.get_ident()
        try:
            from kiro_crew.apps import builtins as builtins_pkg

            root = Path(builtins_pkg.__file__).resolve().parent
            for name in builtins_pkg.BUILTIN_NAMES:
                if not (root / name / f"{BUILTIN_SEAMS_MODULE}.py").is_file():
                    continue
                module = f"{builtins_pkg.__name__}.{name}.{BUILTIN_SEAMS_MODULE}"
                try:
                    importlib.import_module(module)
                except Exception:
                    logger.warning("could not load the seam providers in %s", module, exc_info=True)
        finally:
            _loading_thread = None
            _builtins_loaded = True

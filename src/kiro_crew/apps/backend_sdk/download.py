"""Verified downloads: ``ctx.verified_download`` for a spawned backend (seam level 18)."""

from __future__ import annotations

import os

from kiro_crew.apps.backend_sdk.audit import APP_NAME_ENV
from kiro_crew.apps.download_sdk import VerifiedDownloadError, VerifiedDownloadSDK

__all__ = ["VerifiedDownloadError", "verified_download"]


def verified_download() -> VerifiedDownloadSDK:
    """The verified downloader for this spawned backend's own app.

    The same object ``ctx.verified_download`` is, for the app named by the
    ``KIROCREW_APP_NAME`` the gateway set when it spawned this process, writing into
    that app's data directory (``<data home>/apps/<name>/data``). It takes no name:
    a backend cannot ask for another app's directory. Its ``fetch(url, dest, *,
    sha256, max_bytes, on_progress=None)`` requires the digest and a size cap, vets
    the url's addresses and dials only them, refuses any redirect off the host, and
    raises :class:`VerifiedDownloadError` when nothing was installed.

    Raises ``LookupError`` when the variable is unset, blank or not an app name: an
    in-process backend uses ``ctx.verified_download``.
    """
    from kiro_crew.apps.execution import APP_NAME_RE

    name = os.environ.get(APP_NAME_ENV, "").strip()
    if not APP_NAME_RE.fullmatch(name):
        raise LookupError(
            f"no app name: call this from a backend the gateway spawned (it sets "
            f"{APP_NAME_ENV}), or use ctx.verified_download in-process"
        )
    from kiro_crew.apps.manager_records import app_data_dir

    return VerifiedDownloadSDK(name, app_data_dir(name))

"""The verified download for a spawned backend: ``ctx.verified_download`` (seam level 20)."""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.apps.backend_sdk.audit import APP_NAME_ENV
from kiro_crew.apps.download_sdk import VerifiedDownloadError, download_into

__all__ = ["VerifiedDownloadError", "verified_download"]


def verified_download(url: str, dest: str | os.PathLike[str], *, sha256: str, size: int) -> Path:
    """Download one pinned https file into this backend's own app data directory.

    The same call as ``ctx.verified_download``, for the app named by the
    ``KIROCREW_APP_NAME`` the gateway set when it spawned this process, writing into
    that app's data directory (``<data home>/apps/<name>/data``). It takes no app name,
    so a backend cannot write another app's directory through it.

    *dest* is a path relative to the data directory whose folders already exist;
    *sha256* and *size* are the file's digest and exact length, and nothing is visible
    at *dest* until both match. One fetch runs in a folder at a time: a second one is
    refused, never run beside it. https only, the certificate verified, and a redirect
    followed only on the same host. Returns the installed path. Blocking: call it from
    a worker thread.

    Raises ``LookupError`` when the variable is unset or not an app name (an
    in-process backend uses ``ctx.verified_download``), ``ValueError`` or
    ``TypeError`` for a refused argument before anything is opened, and
    :class:`VerifiedDownloadError` when nothing was installed.
    """
    from kiro_crew.apps.execution import APP_NAME_RE

    name = os.environ.get(APP_NAME_ENV, "").strip()
    if not APP_NAME_RE.fullmatch(name):
        raise LookupError(
            f"no app name: call this from a backend the gateway spawned (it sets "
            f"{APP_NAME_ENV}), or use ctx.verified_download in-process"
        )
    from kiro_crew.apps.manager import app_data_dir

    return download_into(
        app_data_dir(name), url, dest, sha256=sha256, size=size, label=f"{name} download"
    )

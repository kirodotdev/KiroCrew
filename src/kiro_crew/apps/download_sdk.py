"""Verified download SDK: one sha256-pinned https file into an app's own data directory.

An app that installs a third-party artifact (a compiler, an engine archive) needs the
same transfer the gateway gives its own pinned downloads. The private
``asset_downloader.download_to`` moves with the wheel and takes a cross-host redirect
switch, a target anywhere on disk and an unvetted url, so it is not offered;
``VerifiedDownloadSDK.fetch`` is that transfer with every choice fixed (seam level 18):

* **The digest is required.** ``sha256`` is 64 hex characters or the call is refused;
  nothing reaches the destination until the streamed bytes match it.
* **The url is vetted and the connection pinned.** https only, through
  ``link_unfurl.vet_unfurl_url`` with no option (no credentials in the url, port 80 or
  443, every address the name resolves to public), and every connection dials only the
  approved addresses (``download_to``'s ``pinned_addresses``), so a second lookup at
  connect time cannot reach an address the vet never saw.
* **No redirect leaves the host.** A redirect to another host, port or scheme is
  refused (``asset_downloader._SameHostRedirectHandler``); there is no switch for it.
* **The destination is the app's own data directory.** A relative ``dest`` is a path
  inside it, an absolute one must resolve inside it, and its folder must already
  exist (nothing is created by name). The folder is pinned open while the transfer
  runs, and on every platform the kernel's answer for that handle (``fd_real_path``,
  ``GetFinalPathNameByHandleW`` on Windows) must be exactly the folder that was
  checked, and the open staging file's must be inside it before the rename, so an
  ancestor swapped for a link or a junction refuses the transfer. The file is
  written owner-only.
* **The size is capped.** ``max_bytes`` is required and at most the engine's own
  ceiling; a longer body is abandoned and nothing is installed.
* **No file of the app's is touched but the destination.** The bytes land in a
  staging file with a random name (``.<dest name>.<token>.kc-dl``) created
  exclusively in the pinned directory (``download_to``'s ``exclusive_staging``), so
  an existing file is never replaced or removed, ``<dest>.part`` included, and two
  fetches at once never share one. Only that staging file is removed when the
  transfer does not install, on every way out.

It adds no reach: a backend can open an https connection and write its own data
directory without it. It only removes choices the private function leaves open.
Attribution is cooperative, as it is for ``ctx.audit``: ``ctx.verified_download`` is
built with the manifest name and data directory, and ``backend_sdk.verified_download()``
with the ``KIROCREW_APP_NAME`` the gateway set on a spawned backend; hook code that
builds one for another app's directory is outside what an in-process API prevents.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

#: An accepted digest: 64 hex characters, compared lowercased.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: The staging file's suffix, after a random token: ``.<dest name>.<token>.kc-dl``.
STAGING_SUFFIX = ".kc-dl"

#: How much of the destination's name the staging name repeats, so the token and the
#: suffix still fit a file system's name limit.
_STAGING_NAME_PREFIX_CHARS = 64


def _staging_name(name: str) -> str:
    """A fresh, unpredictable staging name beside *name*.

    A 128-bit token makes a file already at that name a planted one rather than a
    coincidence, and the exclusive create refuses it and removes nothing.
    """
    return f".{name[:_STAGING_NAME_PREFIX_CHARS]}.{secrets.token_hex(16)}{STAGING_SUFFIX}"


class VerifiedDownloadError(RuntimeError):
    """A verified download did not install, and nothing is at the destination.

    ``reason`` is the transfer engine's own wording (a transport cause, the size cap,
    a digest mismatch, a refused redirect), which names a host and never the url's
    path, query or a credential.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _checked_sha256(sha256: object) -> str:
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256.lower()):
        raise ValueError("sha256 must be the file's 64-character hex SHA-256 digest")
    return sha256.lower()


def _checked_cap(max_bytes: object) -> int:
    from kiro_crew import asset_downloader

    ceiling = asset_downloader.DEFAULT_MAX_BYTES
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= ceiling
    ):
        raise ValueError(f"max_bytes must be an int from 1 to {ceiling}")
    return max_bytes


def _within(child: str, parent: str) -> bool:
    child, parent = os.path.normcase(child), os.path.normcase(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


class VerifiedDownloadSDK:
    """Verified downloads into one app's data directory.

    Reached as ``ctx.verified_download`` (in-process) or through
    ``backend_sdk.verified_download()`` (a spawned backend). Requires no permission:
    every option it has only narrows what the app's own code could already do.
    """

    def __init__(self, app_name: str, data_dir: str | os.PathLike[str]) -> None:
        self.app_name = app_name
        self._data_dir = os.fspath(data_dir)

    def _destination(self, dest: object) -> tuple[str, str]:
        """``(parent, name)`` for *dest*, the parent resolved once, or ``ValueError``.

        A relative *dest* is joined to the data directory and an absolute one kept;
        the parent is then resolved (``realpath``) once, here, and that one spelling is
        what the transfer pins and what the pinned handle must turn out to be. The
        parent must already be a directory: nothing is created by name, since a
        ``mkdir`` through an ancestor swapped for a link would write outside.
        """
        if not isinstance(dest, (str, os.PathLike)):
            raise TypeError(f"dest must be a str or os.PathLike, not {type(dest).__name__}")
        raw = os.fspath(dest)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("dest must be a non-empty str path")
        joined = os.path.join(self._data_dir, raw)
        name = os.path.basename(joined)
        if name in ("", ".", ".."):
            raise ValueError("dest must name a file")
        data_real = os.path.realpath(self._data_dir)
        parent = os.path.realpath(os.path.dirname(joined))
        if not _within(parent, data_real):
            raise ValueError(f"dest must be inside {self.app_name}'s own data directory")
        if not os.path.isdir(parent):
            raise ValueError("dest's folder must already exist inside the data directory")
        return parent, name

    def fetch(
        self,
        url: str,
        dest: str | os.PathLike[str],
        *,
        sha256: str,
        max_bytes: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download *url* to *dest*, verified against *sha256*, and return where it is.

        *dest* is inside this app's data directory: a relative path is resolved in it
        and an absolute one must resolve into it, and its folder must already exist
        there. The folder is pinned open and must be, by the kernel's own answer for
        that handle, exactly the folder that was checked, on every platform, so an
        ancestor swapped for a link or a junction after the check refuses the
        transfer. The file replaces whatever was at *dest* in one rename, and it is
        owner-only. *max_bytes* caps the body. *on_progress*, when given, is
        called with ``(bytes so far, expected total)`` as the body arrives. No other
        file in the directory is created, replaced or removed but a staging file of
        this call's own, which is gone when it returns or raises.

        Raises ``ValueError`` or ``TypeError`` for a refused argument (a digest, cap,
        destination or url outside the rules) and the gateway's ``UrlRejectedError``
        for a url the public-address vet refuses, in both cases before anything is
        fetched, and :class:`VerifiedDownloadError` when the transfer did not install.
        Blocking (a DNS lookup and the whole transfer): call it from a worker thread.
        """
        from kiro_crew import asset_downloader, link_unfurl, pinned_fs

        digest = _checked_sha256(sha256)
        cap = _checked_cap(max_bytes)
        if on_progress is not None and not callable(on_progress):
            raise TypeError("on_progress must be callable")
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            raise ValueError("url must be an https:// URL")
        parent, name = self._destination(dest)
        vetted = link_unfurl.vet_unfurl_url(url)
        if vetted.scheme != "https":
            raise ValueError("url must be an https:// URL")
        ok, reason = False, ""
        try:
            with asset_downloader.pin_target_dir(Path(parent), what="download directory") as where:
                # The kernel's answer for the handle, on every platform
                # (GetFinalPathNameByHandleW on Windows): the folder checked above, not
                # one reached through an ancestor swapped since. Unreadable refuses.
                held = pinned_fs.fd_real_path(where.fd)
                if held is None or not asset_downloader.same_real_path(held, parent):
                    raise VerifiedDownloadError(
                        f"the download directory is not the folder inside {self.app_name}'s "
                        "data directory that was checked"
                    )
                ok, reason = asset_downloader.download_to(
                    Path(parent) / name,
                    vetted.url,
                    sha256=digest,
                    max_bytes=cap,
                    staging=Path(parent) / _staging_name(name),
                    exclusive_staging=True,
                    on_progress=on_progress,
                    restrict_to_owner=True,
                    label=f"{self.app_name} download",
                    target_dir=where,
                    pinned_addresses=tuple(vetted.addresses),
                )
        except asset_downloader.TargetDirRefused as exc:
            # Its message names a local path and nothing else.
            raise VerifiedDownloadError(str(exc)) from None
        except OSError as exc:
            # Opening the directory failed; no transfer started.
            raise VerifiedDownloadError(
                f"the download directory could not be prepared ({type(exc).__name__})"
            ) from None
        if not ok:
            raise VerifiedDownloadError(reason)
        return Path(parent) / name

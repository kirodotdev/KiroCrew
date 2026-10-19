"""Moving a release's bytes over the network, within bounds.

The staging directory under the data home's ``trust/`` keystone directory, the bounded
HTTPS read with its socket deadline, the download of a wheel into a file with its SHA-256
compared to the signed digest as the bytes land, and the fetch of a small document such
as the channel manifest. The offline trust root and the manifest's signature check stay
in the facade, whose ``download_verified_wheel`` is the one caller of the download.

:mod:`kiro_crew.platform.wheel_engine` is the import path and the patch surface: callers
import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import contextlib
import hashlib
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from kiro_crew.config.paths import data_home
from kiro_crew.platform.wheel_engine_contract import (
    _WHEEL_FETCH_TOTAL_SECS,
    ApplyCancel,
    WheelUpdateError,
    _raise_if_cancelled,
)
from kiro_crew.platform_compat import make_owner_only_dir


def _staging_dir() -> Path:
    """Per-run staging area under the data home's ``trust/`` keystone directory.

    The verified wheel sits on disk between its SHA-256 check and the pip
    install, and a TMPDIR staging file is writable by the agent (same uid, and
    TMPDIR is deliberately agent-reachable scratch) — a swap in that window
    would promote unverified code. ``trust/`` is the existing whole-directory
    keystone leaf: the agent's file gate and every bash form refuse it, while
    this engine (the gateway or the operator's CLI) writes directly. Same
    accepted residual as every keystone leaf: arbitrary local code as the
    user is out of scope, the AGENT's tool surface is what is fenced.
    """
    root = data_home() / "trust" / "update-staging"
    make_owner_only_dir(root)
    return root


def _response_socket(resp: object) -> object | None:
    """The socket under a urllib response, when the stdlib layout exposes it."""
    raw = getattr(getattr(resp, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


class _SinkWriteError(Exception):
    """A :func:`_read_bounded` sink's own write failed (a full or failing staging disk).

    Not a :class:`WheelUpdateError` and not an :class:`OSError`, so it crosses
    the fetch's own error handling untouched: the fetch would otherwise report
    the caller's write failure as ``could not fetch <url>``. The caller that
    owns the destination converts it, naming that destination.
    """

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


def _read_bounded(
    url: str,
    *,
    cap: int,
    read_timeout: float,
    total_secs: float,
    cancel: ApplyCancel | None,
    sink: Callable[[bytes], None],
) -> None:
    """Stream *url* into *sink*, bounded per read, in total, in size and by *cancel*.

    ``read1`` returns whatever has arrived rather than looping until a full
    chunk does, so the deadline and the cancel are checked after every read even
    against an origin that drips one byte at a time. Each read's socket timeout
    is the smaller of *read_timeout* and what is left of *total_secs*, and
    setting *cancel* shuts the socket down, so neither waits on a silent origin.
    Enforced against received bytes, never Content-Length. An ``OSError`` from
    *sink* itself is raised as :class:`_SinkWriteError`, never as a fetch failure.
    """
    if not url.startswith("https://"):
        raise WheelUpdateError(f"refusing non-HTTPS URL: {url}")
    _raise_if_cancelled(cancel)
    deadline = time.monotonic() + total_secs
    req = urllib.request.Request(url, headers={"User-Agent": "kirocrew-update/1"})
    received = 0
    undo: Callable[[], None] | None = None
    try:
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=min(read_timeout, total_secs)
        ) as resp:
            sock = _response_socket(resp)
            if cancel is not None and sock is not None:

                def _shut() -> None:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]

                undo = cancel.on_set(_shut)
            while True:
                _raise_if_cancelled(cancel)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WheelUpdateError(f"{url} did not finish within {total_secs:.0f}s")
                # http.client closes the response, and the socket under it, in the
                # same read that consumes the last byte of a Content-Length body.
                # settimeout on that socket raises EBADF; the closed response's
                # read1 returns b"", which is the clean end below.
                if sock is not None and not resp.isclosed():
                    sock.settimeout(min(read_timeout, left))  # type: ignore[attr-defined]
                chunk = resp.read1(65536)
                # Before EOF is read as "complete": a cancel shuts the socket,
                # which ends the read the same way a finished body does.
                _raise_if_cancelled(cancel)
                if not chunk:
                    return
                received += len(chunk)
                if received > cap:
                    raise WheelUpdateError(f"{url} exceeded the {cap}-byte ceiling")
                try:
                    sink(chunk)
                except OSError as exc:
                    raise _SinkWriteError(exc) from exc
    except (WheelUpdateError, _SinkWriteError):
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # A socket shut down by the cancel surfaces here as a read error.
        _raise_if_cancelled(cancel)
        if time.monotonic() >= deadline:
            raise WheelUpdateError(f"{url} did not finish within {total_secs:.0f}s") from exc
        raise WheelUpdateError(f"could not fetch {url}: {exc}") from exc
    finally:
        if undo is not None:
            undo()


def _download_to_file(
    url: str,
    dest: Path,
    cap: int,
    timeout: float,
    expected_sha: str,
    *,
    cancel: ApplyCancel | None = None,
    total_secs: float = _WHEEL_FETCH_TOTAL_SECS,
) -> None:
    """Stream *url* into *dest*, enforcing *cap* and the digest INCREMENTALLY.

    Chunked rather than buffered: the wheel ceiling is 500 MiB, and one
    ``resp.read()`` of that size doubles as a memory spike on the very gateway
    the update is trying to keep alive. The hash is folded in as chunks land,
    so the verify step never re-reads the file it just wrote — and the digest
    therefore covers the exact bytes on disk. Bounded as :func:`_read_bounded`
    describes.
    """
    digest = hashlib.sha256()
    try:
        with open(dest, "wb") as out:

            def _sink(chunk: bytes) -> None:
                digest.update(chunk)
                out.write(chunk)

            _read_bounded(
                url,
                cap=cap,
                read_timeout=timeout,
                total_secs=total_secs,
                cancel=cancel,
                sink=_sink,
            )
    except (OSError, _SinkWriteError) as exc:
        # The open itself, or a write mid-stream (surfaced distinctly by
        # ``_read_bounded`` so it is not reported as a fetch failure).
        cause = exc.error if isinstance(exc, _SinkWriteError) else exc
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise WheelUpdateError(f"could not write {dest}: {cause}") from cause
    except BaseException:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise
    got = digest.hexdigest()
    if got != expected_sha:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise WheelUpdateError(
            f"wheel SHA-256 mismatch (expected {expected_sha}, got {got}) — refusing to install"
        )


def _fetch_bytes(url: str, cap: int, timeout: float, *, cancel: ApplyCancel | None = None) -> bytes:
    """GET *url*, refusing more than *cap* received bytes or *timeout* in total.

    The single small-document network seam of this module, so tests stub one
    function. HTTPS is asserted as defence in depth — the callers validate the
    CDN base before composing the URL, but this function must hold on its own.
    """
    parts: list[bytes] = []
    _read_bounded(
        url, cap=cap, read_timeout=timeout, total_secs=timeout, cancel=cancel, sink=parts.append
    )
    return b"".join(parts)

"""Slide images for the file panel: a deck rendered soffice -> PDF -> PNG, cached by content.

The file panel's ``.pptx`` preview extracts text (``/api/file-office-preview``);
a presentation is its layout, so the panel also needs the slides as pictures.
Nothing in the dashboard can draw a slide itself: that takes an office suite,
and the one open-source renderer with a faithful Impress import is LibreOffice
(``soffice``), a system package the product neither ships nor installs from a
browser request (the same stance ``apps/builtins/pptx_maker`` takes for its own
thumbnails). So this module renders WHEN ``soffice`` is on the gateway host and
reports honestly when it is not, and the panel degrades to the text outline.

Two endpoints:

* ``GET /api/file-office-slides?path=...`` -- the manifest. Validates the path
  through the file endpoints' shared open-and-check prefix, hashes the bytes,
  answers from the cache when the deck was rendered before, otherwise converts
  and rasterizes, then answers ``{"status": "ready", "digest", "count",
  "slides": [{"n", "width", "height"}]}``. Without ``soffice`` it answers
  ``{"status": "unavailable", "reason": "soffice_unavailable", "hint": "<per-OS
  install command>"}`` -- a normal state the panel renders, not an error.
* ``GET /api/file-office-slide?path=...&n=<k>`` -- one PNG. Re-runs the same
  path prefix (the picture is a derivative of the file, so reading it needs the
  same authorization as reading the file) and serves ``slide-<k>.png`` from the
  deck's cache directory.

Rendering pipeline. The deck's bytes are COPIED from the checked handle into a
private work directory (the child never opens the user's path: what it converts
is exactly what was measured and hashed, and the work dir is the only tree it
needs), then ``soffice --headless --convert-to pdf`` runs through the sandbox
chokepoint in ``strict`` mode with a private ``-env:UserInstallation`` profile
so concurrent conversions never contend for LibreOffice's profile lock, a hard
wall-clock timeout, and captured (never inherited) pipes. The PDF is rasterized
in-process with ``pypdfium2`` -- already a core dependency through ``pdfplumber``
-- at a fixed width, and deleted afterwards. Before any page is rasterized the
PDF's visible text goes through the same two screens the download path applies
(the context-aware credential redactor, then the exfiltration-URL redactor); a
deck either would change is not rendered at all -- a slide is a picture of that
text and cannot be redacted afterwards -- and the panel falls back to the text
outline, which is redacted per slide. The finished directory is renamed
into place atomically so a reader never sees a half-rendered deck.

Staging is PINNED. The work directory lives in the same agent-writable cache
tree, so between any two path-based steps its name could be swapped for a
link. Every write into it and every read out of it therefore goes through one
descriptor opened on the directory when it is created (``_Staging``):
descriptor-relative opens with ``O_NOFOLLOW``, ``O_EXCL`` on every create,
descriptor-relative unlinks, and a publish that is verified by inode after the
rename. The child gets paths (``soffice`` takes nothing else, and runs
sandboxed); the gateway itself never dereferences the staging path by name.
A platform without descriptor-relative calls (Windows) does not render at all:
the manifest answers ``unavailable``/``platform_unsupported`` and the panel shows
the text outline. A by-name fallback behind ``lstat`` checks was considered and
rejected -- it leaves a window in which a same-uid process swaps the work
directory for a junction and the cleanup deletes through it.

Cache. ``<data home>/cache/slide-previews/<sha256 of the bytes>/`` holds
``manifest.json`` plus ``slide-<n>.png``. Keyed by CONTENT, so an edited deck
re-renders and an unchanged one never does, and no path or user identity enters
the key. A per-digest lock dedupes concurrent requests for the same deck; a
size budget evicts least-recently-used decks so the cache cannot grow without
bound. Every request hashes the file it was asked about -- a per-slide fetch
included. There is deliberately no stat-keyed shortcut: a ``(size, mtime)``
key can collide for a same-size rewrite that preserves the timestamp and would
then answer the previous deck's slides; a sha256 over a deck under the size cap
costs milliseconds and cannot.

The cache is NOT a trust root. It lives under the data home, which a same-uid
agent can write, so nothing served from it may be taken on faith: a planted
``manifest.json`` beside a ``slide-1.png`` that is a symlink (or a hard link)
to a credential would otherwise turn an authenticated slide request into a
disclosure of that credential. Three properties close that class:

* the manifest is SIGNED -- an HMAC over its canonical body, keyed by a subkey
  derived from the dashboard's token-signing secret (``token_signing.key``,
  gateway-only: masked inside every agent sandbox and behind the file-tool
  fence, unlike the SEL key, which in-sandbox MCP servers must read) with a
  purpose label, the same derivation construction ``session_pid_sig``
  uses. An unsigned or mis-signed manifest is a cache miss, never an answer;
* the manifest records the sha256 of EVERY slide it covers, and a slide is
  served only when the bytes read hash to the recorded value -- so a swapped,
  linked or edited slide file is refused whatever it points at;
* every read opens with ``O_NOFOLLOW`` and refuses anything but a regular file
  under a size cap (``_read_regular_nofollow``, the ``session_pid_sig`` shape).

Without a loadable trust root the cache is treated as absent: nothing is read
from it or written to it, and rendering is refused (``503 cache_unsigned``)
rather than performed into a store nothing can vouch for. That is the
fail-closed answer for a broken install, not a degraded mode to design for.

Everything blocking here runs off the event loop on the file endpoints' transfer
pool, like the other file-serving handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import io
import json
import logging
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from aiohttp import web

from kiro_crew import platform_compat, security
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard import token_secret as _token_secret
from kiro_crew.dashboard.handlers import files as _files
from kiro_crew.executors import subprocess_executor
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.validation import FILE_READ_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

#: Formats LibreOffice's Impress import renders. Wider than the text preview's
#: ``.pptx``-only list on purpose: ``.ppt`` has no XML to extract text from, but
#: soffice reads it, so a legacy deck gets slides where it could never get text.
SLIDE_EXTS = frozenset({".pptx", ".ppt"})
#: Rendered slide width. Wide enough to read body text in a full-width panel,
#: small enough that a 100-slide deck stays under ~10 MB of PNG.
SLIDE_WIDTH_PX = 1280
#: Decks longer than this are rendered up to the cap; the manifest says so.
MAX_SLIDES = 500
#: soffice's cold start bootstraps a profile (~10 s) before converting; a large
#: deck with embedded media takes tens of seconds more. Killed past this.
CONVERT_TIMEOUT_SEC = 180.0
#: Total bytes the cache may hold before least-recently-used decks are evicted.
CACHE_BUDGET_BYTES = 256 * 1024 * 1024
#: Size gate on the deck itself: the same 50 MB ceiling as uploads and the text
#: preview, enforced on the fd before a byte is copied.
MAX_DECK_BYTES = _files._MAX_UPLOAD_BYTES

_MANIFEST_NAME = "manifest.json"
_SLIDE_TEMPLATE = "slide-{n}.png"
_WORK_PREFIX = ".work-"
#: Upper bounds on what a cache read will buffer. A manifest is a few KB even
#: for a 500-slide deck; a 1280 px PNG is well under a megabyte.
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_SLIDE_FILE_BYTES = 16 * 1024 * 1024
#: Domain-separation label for the cache-signing subkey (see the module doc).
#: Versioned: bumping it invalidates every cached manifest without touching the
#: trust root, which is the rotation story for a format change.
_SUBKEY_DOMAIN = b"kirocrew.slide_previews.manifest.v1"
#: Mirrors sel.py / session_pid_sig: a shorter key is corruption, not a key.
_HMAC_KEY_MIN_BYTES = 32

_TOOL_NAME = "file_office_slides"


def cache_root() -> Path:
    """The cache directory. Under the data home's ``cache/`` beside the other
    regenerable caches; resolved per call because the data home is per instance."""
    return config_dir() / "cache" / "slide-previews"


def soffice_path() -> str | None:
    """``soffice`` on this host, or ``None``.

    The pptx-maker app already owns the answer -- ``PATH`` first, then the fixed
    Windows install root a by-name lookup cannot reach -- so this asks it rather
    than growing a second discovery. Imported lazily, the way
    ``handlers/aws_consent.py`` reaches the AWS Control backend: the builtin
    app package is heavier than this module and only needed on a miss.
    """
    from kiro_crew.apps.builtins.pptx_maker.backend import engine

    return engine.optional_dep_path("soffice")


def soffice_hint() -> str:
    """The per-OS install command the app shows for a missing ``soffice``."""
    from kiro_crew.apps.builtins.pptx_maker.backend import preview_tools

    return preview_tools.soffice_hint()


def _sel():
    from kiro_crew.sel import sel

    return sel()


def _log(outcome: str, resources: str, error: str = "") -> None:
    kw = {"error": error} if error else {}
    _sel().log_tool_invocation(
        session_key="dashboard", tool_name=_TOOL_NAME, outcome=outcome, resources=resources, **kw
    )


# --------------------------------------------------------------------------- #
# Cache layout
# --------------------------------------------------------------------------- #


def _deck_dir(digest: str) -> Path:
    return cache_root() / digest


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _signing_key() -> bytes | None:
    """The cache-signing subkey, or ``None`` when the trust root cannot be loaded.

    The root is the dashboard's token-signing secret: the one key the gateway
    web server alone holds. It is masked inside every agent sandbox and fenced
    from the file tools, so a same-uid agent cannot read it and forge a signed
    manifest. The SEL key would not do -- in-sandbox MCP servers read it to
    resolve their session identity, so it is deliberately VISIBLE to sandboxed
    processes. Derived with :data:`_SUBKEY_DOMAIN` so this protocol and token
    auth never share a signing key. Loaded through the secret's own owner
    (``token_secret``), which creates it on first use exactly as token auth does.
    """
    try:
        raw = _token_secret._get_secret()
    except OSError:
        return None
    if len(raw) < _HMAC_KEY_MIN_BYTES:
        return None
    return hmac.new(raw, _SUBKEY_DOMAIN, hashlib.sha256).digest()


def _canonical(manifest: dict[str, Any]) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(manifest: dict[str, Any], key: bytes) -> dict[str, Any]:
    signed = dict(manifest)
    signed.pop("sig", None)
    signed["sig"] = hmac.new(key, _canonical(signed), hashlib.sha256).hexdigest()
    return signed


def _verified(manifest: object, key: bytes) -> bool:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sig"), str):
        return False
    expected = hmac.new(key, _canonical(manifest), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, manifest["sig"])


def _read_regular_nofollow(path: Path, max_bytes: int) -> bytes | None:
    """Read *path* refusing symlinks, non-regular files and oversize.

    The cache directory is same-uid agent-writable, so a by-name ``read_bytes``
    here is a disclosure primitive pointed at whatever the name resolves to.
    ``O_NOFOLLOW`` refuses a link at the final component race-free; where the
    flag is absent, an ``lstat`` pre-check plus a post-open ``(st_dev, st_ino)``
    identity check closes the swap window; ``S_ISREG`` rejects FIFOs and
    devices; the size is bounded by ``fstat`` and by the bytes actually read.
    Returns ``None`` on every refusal (callers fail closed).
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    pre: os.stat_result | None = None
    try:
        if not nofollow:
            pre = os.lstat(path)
            if stat.S_ISLNK(pre.st_mode):
                return None
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if pre is not None and (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return None if len(data) > max_bytes else data
    except OSError:
        return None
    finally:
        os.close(fd)


def read_manifest(digest: str) -> dict[str, Any] | None:
    """The cached, VERIFIED manifest for *digest*, or ``None`` (a cache miss).

    A miss covers everything the cache cannot vouch for: no directory, an
    unreadable or non-regular manifest, a signature that does not verify, or a
    manifest for another digest. Touches the directory's mtime on a hit: that
    is the recency the evictor orders by, so a deck the user keeps opening is
    the last to go.
    """
    if not _is_digest(digest):
        return None
    key = _signing_key()
    if key is None:
        return None
    path = _deck_dir(digest) / _MANIFEST_NAME
    raw = _read_regular_nofollow(path, _MAX_MANIFEST_BYTES)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not _verified(manifest, key) or manifest.get("digest") != digest:
        return None
    try:
        os.utime(path.parent)
    except OSError:
        pass
    return manifest


def read_slide(manifest: dict[str, Any], n: int) -> bytes | None:
    """The bytes of slide *n*, or ``None`` unless they hash to what the manifest signed.

    The manifest is trusted (it verified); the slide FILE is not -- it is read
    no-follow and its sha256 must equal the recorded one, so a replaced, linked
    or edited file is refused whatever it now points at.
    """
    name = _SLIDE_TEMPLATE.format(n=n)
    hashes = manifest.get("sha256")
    expected = hashes.get(name) if isinstance(hashes, dict) else None
    if not isinstance(expected, str):
        return None
    data = _read_regular_nofollow(_deck_dir(str(manifest["digest"])) / name, _MAX_SLIDE_FILE_BYTES)
    if data is None or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
        return None
    return data


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def evict_to_budget(budget: int = CACHE_BUDGET_BYTES, keep: str | None = None) -> int:
    """Delete least-recently-used deck directories until the cache fits *budget*.

    *keep* is the digest just rendered: it is the most recent by definition and
    must survive even when it alone exceeds the budget, or the request that
    produced it would answer a manifest whose files are already gone. Also
    sweeps abandoned work directories older than the conversion timeout -- a
    gateway killed mid-render leaves one behind. Returns the bytes removed.
    """
    root = cache_root()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    now = time.time()
    decks: list[tuple[float, int, Path]] = []
    removed = 0
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        if entry.name.startswith(_WORK_PREFIX):
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime > CONVERT_TIMEOUT_SEC * 2:
                    removed += _dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue
            continue
        if not _is_digest(entry.name):
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        decks.append((mtime, _dir_size(path), path))
    total = sum(size for _, size, _ in decks)
    for _, size, path in sorted(decks):  # oldest first
        if total <= budget:
            break
        if keep and path.name == keep:
            continue
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        removed += size
    return removed


# --------------------------------------------------------------------------- #
# Digest bookkeeping
# --------------------------------------------------------------------------- #


def _hash_handle(fobj: BinaryIO) -> str:
    """sha256 of the whole handle, rewound afterwards so the caller can copy it.

    Called on every request, slide fetches included. A stat-keyed memo was
    considered and rejected: ``(size, mtime_ns)`` collides for a same-size
    rewrite that keeps the timestamp, and the collision would serve the old
    deck's slides for the new bytes. The hash is bounded by ``MAX_DECK_BYTES``
    and takes milliseconds for a typical deck.
    """
    fobj.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: fobj.read(1024 * 1024), b""):
        h.update(chunk)
    fobj.seek(0)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class _ContentRedacted(Exception):
    """The rendered deck's text carries what the credential or exfiltration screens
    redact. Slides are pictures of that text, so they are not produced at all;
    the panel degrades to the text outline, which IS redacted."""


class _ConvertFailed(Exception):
    """soffice did not produce a PDF: a non-zero exit, a timeout, or no output."""


def _sensitive_hidden_dirs() -> tuple[str, ...]:
    """Trust-root paths to hide from the child, beyond ``strict`` mode's own list.

    Same derivation as the Papyrus compiler's: ``strict`` hides the third-party
    credential dirs, but Kiro Crew's own keystone files (``.local_secret``,
    ``sel_hmac.key``, ...) sit in the data home, and LibreOffice reads files --
    a crafted deck can link or embed one. Re-anchored under the live data home
    because ``KIROCREW_HOME`` may point away from the default.
    """
    home = os.path.expanduser("~")
    rels = security.sensitive_home_dirs()
    paths = [os.path.join(home, rel) for rel in rels]
    data_home = str(config_dir())
    prefix = f".kiro{os.sep}crew{os.sep}"
    for rel in rels:
        normalized = rel.replace("/", os.sep)
        if normalized.startswith(prefix):
            paths.append(os.path.join(data_home, normalized[len(prefix) :]))
    return tuple(dict.fromkeys(paths))


def _soffice_argv(soffice: str, profile_dir: Path, out_dir: Path, deck: Path) -> list[str]:
    """The conversion command as an argv LIST -- never a shell string.

    ``-env:UserInstallation`` gives this run its own profile: LibreOffice locks
    the profile it starts with, so two conversions sharing one would serialize
    or fail; a private, throwaway profile also means the child never reads the
    operator's own LibreOffice settings. ``--norestore`` keeps a crash in a
    previous run from opening the recovery dialog in a headless process.
    """
    return [
        soffice,
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless",
        "--norestore",
        "--nologo",
        "--nolockcheck",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(deck),
    ]


#: Bytes kept from each of the child's pipes. soffice is chatty on stderr and a
#: deck can make it arbitrarily so; the tail is all a failure report needs.
_CAPTURE_CAP = 64 * 1024


async def _read_capped(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently, keeping the first ``_CAPTURE_CAP`` bytes of each.

    Not ``communicate()``: that buffers both streams without bound. Both pipes
    are read at once (draining one to EOF first deadlocks when the child fills
    the other), and the excess is read and DROPPED rather than the pipe closed,
    so the child never sees EPIPE for being verbose.
    """

    async def _drain(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        kept = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(kept)
            if len(kept) < _CAPTURE_CAP:
                kept.extend(chunk[: _CAPTURE_CAP - len(kept)])

    stdout, stderr = await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr))
    await proc.wait()
    return stdout, stderr


async def _run_soffice(argv: list[str], work: _Staging | Path) -> None:
    """Run the conversion under the sandbox chokepoint with a wall-clock timeout.

    Strict mode: the input is a user-supplied deck, and LibreOffice will follow
    a link inside it to any file it can read, so it gets the credential-scrubbed
    environment and the hidden trust root. The chokepoint is prepared OFF the
    loop (its cold backend probe can block for seconds). Fail-closed on a host
    with no sandbox backend and no opt-in: ``SandboxUnavailableError`` surfaces
    to the endpoint, which reports it rather than converting unisolated.
    """
    # A minimal environment, not an inherited one: PATH so soffice finds its
    # own program dir, HOME because fontconfig keeps its cache there (a run
    # without it rebuilds the font cache every time), TMPDIR pointed INTO the
    # work dir so every temp file the conversion writes is deleted with it.
    # No DISPLAY: a headless soffice that inherits one still tries to connect.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", os.path.expanduser("~")),
        "TMPDIR": os.fspath(work),
        "TMP": os.fspath(work),
        "TEMP": os.fspath(work),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    wrapped, scrubbed, cleanup = await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            "strict",
            env=env,
            extra_hidden_dirs=_sensitive_hidden_dirs(),
        ),
        executor=subprocess_executor(),
    )
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await create_subprocess_limited(
            *wrapped,
            cwd=os.fspath(work),
            env=scrubbed,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        stdout, stderr = await asyncio.wait_for(_read_capped(proc), timeout=CONVERT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
            except (ProcessLookupError, OSError, ValueError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.warning("office_slides: soffice did not exit after SIGKILL")
        raise _ConvertFailed(f"soffice timed out after {CONVERT_TIMEOUT_SEC:.0f}s")
    finally:
        if cleanup:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), functools.partial(Path(cleanup).unlink, missing_ok=True)
            )
    if proc.returncode != 0:
        tail = (stderr or stdout or b"")[-400:].decode("utf-8", "replace")
        raise _ConvertFailed(f"soffice exited {proc.returncode}: {tail.strip()}")


_DIR_FD_OPS = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.scandir in os.supports_fd
    and shutil.rmtree.avoids_symlink_attacks
)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_MANIFEST_TMP = ".manifest.tmp"


def _is_link_like(st: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (a junction counts)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0))


class _PlatformUnsupported(Exception):
    """The platform has no descriptor-relative file operations, so the staging tree
    cannot be pinned. Rendering FAILS CLOSED here: a by-name fallback behind
    ``lstat`` checks leaves a window in which a same-uid process swaps the work
    directory for a link and the cleanup deletes through it. The text outline is
    the preview such a host gets."""


def _open_cache_root() -> int:
    """A descriptor on the cache root, reached WITHOUT following a link at any step.

    The cache root and its parent live in the agent-writable data home, so a
    by-name ``mkdir(parents=True, exist_ok=True)`` would follow a link planted at
    either name and stage the render wherever it points. Instead the two names
    under the anchor (the cache root's grandparent -- the data home in the shipped
    layout) are created and opened descriptor-relative with ``O_NOFOLLOW``, so a
    link at either step is ``ELOOP``/``ENOTDIR`` -> ``_ConvertFailed``, never a
    place to write. Raises ``_PlatformUnsupported`` where descriptor-relative
    calls do not exist.
    """
    if not _DIR_FD_OPS:
        raise _PlatformUnsupported("no descriptor-relative file operations")
    root = cache_root()
    anchor = root.parent.parent
    anchor.mkdir(parents=True, exist_ok=True)
    fd = os.open(anchor, os.O_RDONLY | _O_DIRECTORY)
    try:
        for name in (root.parent.name, root.name):
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, stat.S_IRWXU, dir_fd=fd)
            try:
                nxt = os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise _ConvertFailed(
                    f"cache directory {name!r} is not a directory: {exc.strerror}"
                ) from exc
            os.close(fd)
            fd = nxt
        with contextlib.suppress(OSError):
            os.fchmod(fd, stat.S_IRWXU)
        return fd
    except BaseException:
        os.close(fd)
        raise


class _Staging:
    """The render's work directory, pinned by descriptors for its whole life.

    The cache tree is same-uid agent-writable, so between two path-based steps
    a NAME in it can be swapped for a link. Path-based code would then delete
    through the link (``iterdir`` + ``unlink``), write the PNGs over whatever it
    points at, rasterize a PDF the agent may not read, or publish into a tree of
    the agent's choosing. A descriptor names the inode, not the name, so every
    write into and read out of the staging tree goes through ``dir_fd`` here:
    ``O_NOFOLLOW`` on every open, ``O_EXCL`` on every create, descriptor-relative
    unlink, and a rename relative to the cache-root descriptor (``root_fd``, from
    ``_open_cache_root``) for both the manifest and the publish, verified by
    inode afterwards. Only the child gets paths (``soffice`` takes nothing else,
    and runs sandboxed). There is deliberately no by-name fallback: a platform
    without these calls does not render (``_PlatformUnsupported``).
    """

    def __init__(self, path: Path, root_fd: int = -1) -> None:
        if not _DIR_FD_OPS:
            raise _PlatformUnsupported("no descriptor-relative file operations")
        self.path = path
        self.name = path.name
        self.fd = -1
        self.root_fd = -1
        # A caller without a root descriptor (tests) gets one on the parent as
        # named. A caller's descriptor becomes OURS only once construction
        # succeeds: on failure the caller still owns it and closes it, so no
        # descriptor is ever closed twice (a second close on a shared pool can
        # hit an unrelated file another request just opened).
        opened_root = root_fd < 0
        if opened_root:
            root_fd = os.open(path.parent, os.O_RDONLY | _O_DIRECTORY)
        try:
            self.fd = os.open(self.name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            if opened_root:
                os.close(root_fd)
            raise _ConvertFailed(f"staging directory is not a directory: {exc.strerror}") from exc
        self.root_fd = root_fd
        st = os.fstat(self.fd)
        if not stat.S_ISDIR(st.st_mode):
            self.close()
            raise _ConvertFailed("staging directory is not a directory")
        self.identity = (st.st_dev, st.st_ino)

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, name: str) -> Path:
        """Paths for the CHILD's argv only; the gateway itself uses the descriptors."""
        return self.path / name

    def close(self) -> None:
        for attr in ("fd", "root_fd"):
            fd = getattr(self, attr)
            if fd >= 0:
                os.close(fd)
                setattr(self, attr, -1)

    def mkdir(self, name: str) -> None:
        os.mkdir(name, stat.S_IRWXU, dir_fd=self.fd)

    def create(self, name: str) -> BinaryIO:
        """A NEW regular file in the staging dir. Never follows, never overwrites."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        return os.fdopen(fd, "wb")

    def open_regular(self, name: str, *, subdir: str | None = None) -> BinaryIO:
        """An existing REGULAR file in the staging dir (or *subdir*), read-only.

        Refuses a link at either component and anything but a regular file, so
        a planted ``out/deck.pdf`` pointing elsewhere is a failure, not an input.
        """
        sub = -1
        try:
            where = self.fd
            if subdir is not None:
                sub = os.open(subdir, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=self.fd)
                where = sub
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=where)
        except OSError as exc:
            raise _ConvertFailed(f"cannot open staged {name!r}: {exc.strerror}") from exc
        finally:
            if sub >= 0:
                os.close(sub)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise _ConvertFailed(f"staged {name!r} is not a regular file")
        return os.fdopen(fd, "rb")

    def regular_files(self, subdir: str) -> list[str]:
        """Names of the regular files directly under *subdir* (links excluded)."""
        sub = os.open(subdir, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=self.fd)
        try:
            with os.scandir(sub) as it:
                return [e.name for e in it if e.is_file(follow_symlinks=False)]
        finally:
            os.close(sub)

    def remove_except(self, keep: frozenset[str]) -> None:
        """Delete every entry of the staging dir not named in *keep*."""
        with os.scandir(self.fd) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for name, is_dir in entries:
            if name in keep:
                continue
            if is_dir:
                shutil.rmtree(name, ignore_errors=True, dir_fd=self.fd)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=self.fd)

    def write_manifest(self, data: bytes) -> None:
        """``manifest.json`` via a sibling temp file and a descriptor-relative rename."""
        with self.create(_MANIFEST_TMP) as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.rename(_MANIFEST_TMP, _MANIFEST_NAME, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def sibling_kind(self, name: str) -> str:
        """What sits at *name* beside the staging dir: ``absent``, ``link`` or ``entry``."""
        try:
            st = os.lstat(name, dir_fd=self.root_fd)
        except OSError:
            return "absent"
        return "link" if _is_link_like(st) else "entry"

    def remove_sibling(self, name: str) -> None:
        """Remove *name* beside the staging dir -- a link is unlinked itself, never followed."""
        kind = self.sibling_kind(name)
        if kind == "link":
            os.unlink(name, dir_fd=self.root_fd)
        elif kind == "entry":
            shutil.rmtree(name, ignore_errors=True, dir_fd=self.root_fd)

    def publish(self, name: str) -> None:
        """Rename the staging dir to *name* beside it and prove the result is OUR directory.

        Relative to the cache-root descriptor, so the rename cannot be redirected
        by a swapped root; checked afterwards all the same: the published entry
        must be a real directory with the pinned inode. Anything else means a
        name was swapped under us -- the planted entry is removed and the render
        fails rather than publishing a link as a deck.
        """
        os.rename(self.name, name, src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
        try:
            st = os.lstat(name, dir_fd=self.root_fd)
        except OSError as exc:
            raise _ConvertFailed(f"published deck vanished: {exc}") from exc
        if (
            _is_link_like(st)
            or not stat.S_ISDIR(st.st_mode)
            or (st.st_dev, st.st_ino) != self.identity
        ):
            with contextlib.suppress(OSError):
                self.remove_sibling(name)
            raise _ConvertFailed("staging directory was replaced during rendering")

    def discard(self) -> None:
        """Delete the staging tree and unpin. A link planted at the name is
        unlinked itself -- ``rmtree`` refuses to follow one -- never its target."""
        if self.root_fd >= 0:
            with contextlib.suppress(OSError):
                self.remove_sibling(self.name)
        self.close()


def _rasterize(
    staging: _Staging, pdf_name: str
) -> tuple[list[dict[str, int]], bool, dict[str, str]]:
    """Render every page of ``out/<pdf_name>`` to ``slide-<n>.png`` in the staging dir.

    Returns the slides' sizes, whether the deck ran past ``MAX_SLIDES``, and the
    sha256 of each PNG as written -- the manifest records those, so they are
    taken from the bytes handed to the descriptor, never from a later by-name
    re-read. In-process ``pypdfium2`` (a core dependency through ``pdfplumber``):
    no poppler, no second venv. The PDF is opened through the staging descriptor
    as a regular file, so a link planted at its name is refused, not rendered.
    Blocking; runs on the transfer pool.
    """
    import pypdfium2 as pdfium

    slides: list[dict[str, int]] = []
    hashes: dict[str, str] = {}
    pdf = staging.open_regular(pdf_name, subdir="out")
    doc = pdfium.PdfDocument(pdf, autoclose=True)
    try:
        count = min(len(doc), MAX_SLIDES)
        _screen_rendered_text(_deck_text(doc, count))
        for i in range(count):
            page = doc[i]
            try:
                width, _height = page.get_size()
                scale = SLIDE_WIDTH_PX / width if width else 1.0
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                buf = io.BytesIO()
                image.save(buf, format="PNG", optimize=True)
                data = buf.getvalue()
                name = _SLIDE_TEMPLATE.format(n=i + 1)
                with staging.create(name) as out:
                    out.write(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
                slides.append({"n": i + 1, "width": image.width, "height": image.height})
            finally:
                page.close()
        truncated = len(doc) > count
    finally:
        doc.close()
    return slides, truncated, hashes


def _deck_text(doc: Any, count: int) -> str:
    """Every text object on the first *count* pages of the converted PDF.

    Taken from the RENDERED document rather than the deck's XML: it is exactly
    the text the slides will show (so it covers ``.ppt``, which has no XML to
    parse), and nothing the conversion dropped -- notes, hidden slides -- can
    trip the screen.
    """
    parts: list[str] = []
    for i in range(count):
        page = doc[i]
        try:
            textpage = page.get_textpage()
            try:
                parts.append(textpage.get_text_bounded())
            finally:
                textpage.close()
        finally:
            page.close()
    return "\n".join(parts)


def _safe_diagnostic(text: str) -> str:
    """A converter diagnostic fit for a log line and an error response.

    ``soffice``'s stderr tail is folded into ``_ConvertFailed``; it is the
    child's own words about a user-supplied deck on the operator's host, so it
    goes through the credential redactor and the exfiltration-URL redactor before
    it is logged or returned -- the same order the file endpoints use for theirs.
    """
    scrubbed = redact(text)
    scrubbed, _warnings = redact_exfiltration_urls(scrubbed)
    return scrubbed


def _screen_rendered_text(text: str) -> None:
    """Refuse to rasterize a deck whose visible text the redactors would change.

    The text preview redacts per slide before it is shown; a slide PNG is a
    picture of the same text and cannot be redacted after the fact, so the same
    two screens the download path applies (the context-aware credential
    redactor, then the exfiltration-URL redactor) gate the render instead. Any
    change means the outline -- which is redacted -- is the only preview offered.
    """
    # PDF text extraction can space out letters the exporter kerned ("E xe c u t i v e"),
    # which would split a token past its pattern; the credential screen therefore also
    # runs over the text with its whitespace removed. A false positive costs the user
    # the pictures, never the (redacted) outline.
    for candidate in (text, "".join(text.split())):
        if redact(candidate) != candidate:
            raise _ContentRedacted("credential-like text on a slide")
    cleaned, _warnings = redact_exfiltration_urls(text)
    if cleaned != text:
        raise _ContentRedacted("exfiltration-shaped URL on a slide")


def _stage_deck(fobj: BinaryIO, staging: _Staging, ext: str) -> str:
    """Copy the checked handle's bytes into the staging dir; return THEIR sha256.

    One pass hashes exactly the bytes written, so what the child converts is
    what the manifest is keyed by. Hashing the handle first and copying it
    second would leave a window in which an in-place rewrite makes the staged
    copy differ from the digest it is signed under.
    """
    fobj.seek(0)
    h = hashlib.sha256()
    with staging.create(f"deck{ext}") as out:
        for chunk in iter(lambda: fobj.read(1024 * 1024), b""):
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _finish(
    staging: _Staging,
    digest: str,
    slides: list[dict[str, int]],
    truncated: bool,
    ext: str,
    key: bytes,
    hashes: dict[str, str],
) -> dict[str, Any]:
    """Write the signed manifest, drop the intermediates, publish the staging dir.

    Every step is descriptor-relative (see ``_Staging``); the manifest's per-slide
    hashes are the ones ``_rasterize`` computed from the bytes it wrote.
    """
    manifest = _sign(
        {
            "status": "ready",
            "digest": digest,
            "count": len(slides),
            "truncated": truncated,
            "width": SLIDE_WIDTH_PX,
            "slides": slides,
            "sha256": hashes,
            "source_ext": ext,
            "rendered_at": time.time(),
        },
        key,
    )
    staging.remove_except(frozenset(hashes))
    staging.write_manifest(json.dumps(manifest).encode("utf-8"))
    kind = staging.sibling_kind(digest)
    if kind == "link":
        # Planted: a link where the deck directory belongs. Remove the link
        # itself (never what it points at) and publish over it.
        staging.remove_sibling(digest)
    elif kind == "entry":
        if read_manifest(digest) is not None:
            # A concurrent renderer (another gateway on the same data home)
            # won, and its result verifies; ours is byte-identical by
            # construction of the key.
            staging.discard()
            return read_manifest(digest) or manifest
        # Present but does not verify: stale format, or planted. Replace.
        staging.remove_sibling(digest)
    staging.publish(digest)
    staging.close()
    return manifest


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


class _KeyedLocks:
    """One ``asyncio.Lock`` per digest, held only while someone is inside or waiting.

    Reference-counted so the registry does not grow by one entry per deck ever
    opened: the entry is dropped when the last holder releases, and a waiter that
    arrived while a render was in flight still serializes on the same lock object.
    """

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str):
        lock, refs = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock, refs = self._locks[key]
            if refs <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, refs - 1)


_DIGEST_LOCKS = _KeyedLocks()


class _Refusal(Exception):
    """A typed refusal from the worker prefix, mapped onto a response by the caller."""

    def __init__(self, response: web.Response, outcome: str, resources: str, error: str = ""):
        super().__init__(outcome)
        self.response = response
        self.outcome = outcome
        self.resources = resources
        self.error = error


def _refuse_open(denied: _files._OpenDenied, raw_path: str) -> _Refusal:
    """Map the shared prefix's typed refusals onto this endpoint's responses.

    Same vocabulary as ``api_file_office_preview`` so the two office endpoints
    answer identical inputs identically.
    """
    code, res = denied.code, denied.path
    if code == "invalid_path":
        return _Refusal(
            web.json_response(
                {"error": "invalid or forbidden path", "code": "forbidden_path"}, status=400
            ),
            "denied",
            res,
        )
    if code == "sensitive_path":
        return _Refusal(
            web.json_response(
                {"error": "sensitive path blocked", "code": "sensitive_path"}, status=403
            ),
            "denied",
            res,
            "sensitive_path",
        )
    if code == "not_found":
        return _Refusal(
            web.json_response({"error": "not found", "code": "not_found"}, status=404),
            "not_found",
            res,
        )
    if code == "symlink_refused":
        return _Refusal(
            web.json_response(
                {"error": "symlinks not allowed", "code": "symlink_rejected"}, status=403
            ),
            "denied",
            res,
            "symlink_rejected",
        )
    if code == "file_too_large":
        return _Refusal(
            web.json_response(
                {
                    "error": f"file too large to render (max {MAX_DECK_BYTES // 1024 // 1024}MB)",
                    "code": "file_too_large",
                },
                status=413,
            ),
            "denied",
            res,
            "file_too_large",
        )
    return _Refusal(
        web.json_response({"error": "failed to read file", "code": "read_failed"}, status=500),
        "failure",
        res,
        code,
    )


def _unsupported(path: str) -> _Refusal:
    return _Refusal(
        web.json_response(
            {"error": "unsupported format for slide rendering", "code": "unsupported_slide_format"},
            status=415,
        ),
        "denied",
        path,
        "unsupported_slide_format",
    )


async def _resolve_path(request: web.Request, tool_name: str) -> tuple[str, web.Response | None]:
    """The query's ``path`` after the shared ``resolve=1`` handling and schema check."""
    raw_path = request.query.get("path", "")
    if request.query.get("resolve") == "1":
        try:
            raw_path, err = await _files._run_path_probe(_files._resolve_project_relative, raw_path)
        except _files._PathProbeBusy:
            return raw_path, _files._probe_busy_response(resource=raw_path, tool_name=tool_name)
        if err == "cannot_resolve":
            _log("denied", request.query.get("path", ""), "cannot_resolve")
            return raw_path, web.json_response(
                {"error": "cannot resolve: no project dir configured", "code": "no_project_dir"},
                status=400,
            )
        if err == "outside_project":
            _log("denied", request.query.get("path", ""), "outside_project")
            return raw_path, web.json_response(
                {"error": "path outside project directory", "code": "path_outside_project"},
                status=400,
            )
    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _log("denied", raw_path)
        return raw_path, web.json_response(
            {"error": "invalid input", "code": "invalid_input"}, status=400
        )
    return raw_path, None


class _Checked:
    """What the manifest worker learned about the deck before deciding to render."""

    def __init__(self, path: str, digest: str, ext: str, manifest: dict[str, Any] | None):
        self.path = path
        self.digest = digest
        self.ext = ext
        self.manifest = manifest


def _inspect(raw_path: str) -> _Checked:
    """Open-and-check, hash, and consult the cache -- one worker hop."""
    checked = _files._open_checked_file(
        raw_path, tool_name=_TOOL_NAME, fstat_cap=MAX_DECK_BYTES, log_open_failure=False
    )
    if isinstance(checked, _files._OpenDenied):
        raise _refuse_open(checked, raw_path)
    with checked.file as fobj:
        ext = os.path.splitext(checked.path)[1].lower()
        if ext not in SLIDE_EXTS:
            raise _unsupported(checked.path)
        digest = _hash_handle(fobj)
    return _Checked(checked.path, digest, ext, read_manifest(digest))


def _render_prepare(raw_path: str, digest: str, ext: str) -> _Staging:
    """Re-open the deck through the prefix and stage its bytes into a fresh work dir.

    Re-opened rather than kept from ``_inspect``: the handle must not cross the
    lock wait on the event loop. The bytes are re-hashed against *digest* so a
    file that changed in between is refused rather than rendered under the old
    key -- hashed WHILE staging, so the staged copy is the hashed bytes. The
    cache root is opened without following links (``_open_cache_root``) and the
    work dir is pinned by descriptor the moment it exists; nothing after
    ``mkdir`` touches either by name (see ``_Staging``).
    """
    checked = _files._open_checked_file(
        raw_path, tool_name=_TOOL_NAME, fstat_cap=MAX_DECK_BYTES, log_open_failure=False
    )
    if isinstance(checked, _files._OpenDenied):
        raise _refuse_open(checked, raw_path)
    root_fd = _open_cache_root()
    name = f"{_WORK_PREFIX}{digest[:12]}-{uuid.uuid4().hex[:8]}"
    path = cache_root() / name
    try:
        os.mkdir(name, stat.S_IRWXU, dir_fd=root_fd)
        work = _Staging(path, root_fd)  # owns root_fd from here on
    except BaseException:
        if root_fd >= 0:
            os.close(root_fd)
        raise
    # From here the staging object owns two descriptors and a directory; every
    # exit but success -- a full disk, a refused copy, a cancelled request --
    # discards all three, so a failing request cannot leak them.
    try:
        work.mkdir("profile")
        work.mkdir("out")
        with checked.file as fobj:
            # The staged copy is hashed AS it is written: the bytes the child
            # converts are the bytes the digest names, or the render is refused.
            if _stage_deck(fobj, work, ext) != digest:
                raise _Refusal(
                    web.json_response(
                        {"error": "file changed while rendering", "code": "file_changed"},
                        status=409,
                    ),
                    "failure",
                    checked.path,
                    "file_changed",
                )
    except BaseException:
        work.discard()
        raise
    return work


def _render_finish(work: _Staging, digest: str, ext: str, key: bytes) -> dict[str, Any]:
    """Rasterize the PDF soffice wrote and publish the signed deck directory."""
    pdfs = sorted(n for n in work.regular_files("out") if n.lower().endswith(".pdf"))
    if not pdfs:
        raise _ConvertFailed("soffice produced no PDF")
    slides, truncated, hashes = _rasterize(work, pdfs[0])
    manifest = _finish(work, digest, slides, truncated, ext, key, hashes)
    evict_to_budget(keep=digest)
    return manifest


async def api_file_office_slides(request: web.Request) -> web.Response:
    """GET /api/file-office-slides?path=... -- the rendered deck's manifest (renders on a miss)."""
    raw_path, early = await _resolve_path(request, _TOOL_NAME)
    if early is not None:
        return early
    try:
        try:
            info = await _files._run_path_probe(_inspect, raw_path, transfer=True)
        except _files._PathProbeBusy:
            return _files._probe_busy_response(resource=raw_path, tool_name=_TOOL_NAME)
        if info.manifest is not None:
            _log("success", info.path)
            return web.json_response(info.manifest)
        soffice = await asyncio.to_thread(soffice_path)
        if not soffice:
            _log("denied", info.path, "soffice_unavailable")
            return web.json_response(
                {"status": "unavailable", "reason": "soffice_unavailable", "hint": soffice_hint()}
            )
        key = await asyncio.to_thread(_signing_key)
        if key is None:
            # No trust root, no cache (see the module doc): the deck is not
            # rendered into a store nothing can vouch for.
            _log("failure", info.path, "cache_unsigned")
            return web.json_response(
                {
                    "error": "slide cache unavailable: trust root not loadable",
                    "code": "cache_unsigned",
                },
                status=503,
            )
        async with _DIGEST_LOCKS.hold(info.digest):
            cached = await asyncio.to_thread(read_manifest, info.digest)
            if cached is not None:
                _log("success", info.path)
                return web.json_response(cached)
            try:
                work = await _files._run_path_probe(
                    _render_prepare, raw_path, info.digest, info.ext, transfer=True
                )
            except _PlatformUnsupported:
                _log("denied", info.path, "platform_unsupported")
                return web.json_response(
                    {"status": "unavailable", "reason": "platform_unsupported"}
                )
            try:
                argv = _soffice_argv(
                    soffice, work / "profile", work / "out", work / f"deck{info.ext}"
                )
                await _run_soffice(argv, work)
                manifest = await _files._run_path_probe(
                    _render_finish, work, info.digest, info.ext, key, transfer=True
                )
            except _ContentRedacted as exc:
                await asyncio.to_thread(work.discard)
                _log("denied", info.path, "content_redacted")
                logger.info("office_slides: not rendering %s: %s", info.path, exc)
                return web.json_response({"status": "unavailable", "reason": "content_redacted"})
            except _ConvertFailed as exc:
                await asyncio.to_thread(work.discard)
                _log("failure", info.path, "convert_failed")
                # The diagnostic carries the child's stderr tail, which can echo
                # deck- or host-borne secret material: redacted before EITHER sink.
                detail = _safe_diagnostic(str(exc))
                logger.warning("office_slides: %s for %s", detail, info.path)
                return web.json_response(
                    {
                        "error": "could not render slides",
                        "code": "convert_failed",
                        "detail": detail[:400],
                    },
                    status=502,
                )
            except SandboxUnavailableError as exc:
                await asyncio.to_thread(work.discard)
                _log("denied", info.path, f"sandbox unavailable ({exc.kind})")
                return web.json_response(
                    {
                        "error": "could not render slides: sandbox unavailable",
                        "code": "sandbox_unavailable",
                    },
                    status=503,
                )
            except BaseException:
                await asyncio.to_thread(work.discard)
                raise
        _log("success", info.path)
        return web.json_response(manifest)
    except _Refusal as ref:
        _log(ref.outcome, ref.resources, ref.error)
        return ref.response
    except asyncio.CancelledError:
        _log("cancelled", raw_path)
        raise
    except _files._PathProbeBusy:
        return _files._probe_busy_response(resource=raw_path, tool_name=_TOOL_NAME)
    except Exception:  # noqa: BLE001 - last-resort guard; details in the log
        logger.exception("office_slides: rendering failed for %s", raw_path)
        _log("failure", raw_path)
        return web.json_response(
            {"error": "failed to render slides", "code": "render_failed"}, status=500
        )


def _locate_slide(raw_path: str, n: int, want_digest: str) -> tuple[str, bytes]:
    """Authorize the path, find its verified cached deck, and return (validated path, slide *n*'s bytes).

    *want_digest* is the digest the manifest handed the client. It must match
    the file's current digest: a deck edited between the manifest and the
    slide fetch answers 409 instead of a slide from either version, and the
    client-side URL carrying it changes with every edit, so the browser's own
    HTTP cache can never show yesterday's slide for today's file.
    """
    checked = _files._open_checked_file(
        raw_path, tool_name=_TOOL_NAME, fstat_cap=MAX_DECK_BYTES, log_open_failure=False
    )
    if isinstance(checked, _files._OpenDenied):
        raise _refuse_open(checked, raw_path)
    with checked.file as fobj:
        if os.path.splitext(checked.path)[1].lower() not in SLIDE_EXTS:
            raise _unsupported(checked.path)
        digest = _hash_handle(fobj)
    if want_digest and want_digest != digest:
        raise _Refusal(
            web.json_response(
                {"error": "file changed since it was rendered", "code": "stale_digest"}, status=409
            ),
            "denied",
            checked.path,
            "stale_digest",
        )
    manifest = read_manifest(digest)
    if manifest is None or not (1 <= n <= int(manifest.get("count", 0))):
        raise _Refusal(
            web.json_response({"error": "slide not rendered", "code": "not_rendered"}, status=404),
            "not_found",
            checked.path,
            "not_rendered",
        )
    data = read_slide(manifest, n)
    if data is None:
        # The file under the signed name is not what was rendered. Refuse and
        # audit: this is the shape a planted link or a tampered cache takes.
        raise _Refusal(
            web.json_response({"error": "slide not rendered", "code": "not_rendered"}, status=404),
            "denied",
            checked.path,
            "cache_integrity",
        )
    return checked.path, data


async def api_file_office_slide(request: web.Request) -> web.StreamResponse:
    """GET /api/file-office-slide?path=...&n=<k> -- one rendered slide as PNG."""
    raw_path, early = await _resolve_path(request, _TOOL_NAME)
    if early is not None:
        return early
    try:
        n = int(request.query.get("n", ""))
    except ValueError:
        _log("denied", raw_path, "invalid_slide_number")
        return web.json_response(
            {"error": "invalid slide number", "code": "invalid_input"}, status=400
        )
    want_digest = request.query.get("digest", "")
    if want_digest and not _is_digest(want_digest):
        _log("denied", raw_path, "invalid_digest")
        return web.json_response({"error": "invalid digest", "code": "invalid_input"}, status=400)
    try:
        path, data = await _files._run_path_probe(
            _locate_slide, raw_path, n, want_digest, transfer=True
        )
    except _Refusal as ref:
        _log(ref.outcome, ref.resources, ref.error)
        return ref.response
    except _files._PathProbeBusy:
        return _files._probe_busy_response(resource=raw_path, tool_name=_TOOL_NAME)
    # Every served slide is a tool invocation in the audit log, like every
    # refused one -- the read endpoints log their successes too.
    _log("success", path)
    return web.Response(
        body=data,
        content_type="image/png",
        headers={
            # Cacheable only because the URL carries the deck's content digest:
            # an edited deck gets a new digest and therefore a new URL, so the
            # browser cache can never answer a stale slide for the new file.
            # A digest-less request (a direct caller) gets no such promise.
            "Cache-Control": "private, max-age=3600" if want_digest else "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

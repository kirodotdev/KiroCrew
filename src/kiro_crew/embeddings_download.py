"""Downloading the bundled embedding model.

``ModelDownloadManager`` installs the sha256-pinned model in the background, with
retries and progress, from a salvaged legacy blob or the configured URL; the
process holds one manager and one background download task.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import urllib.parse
from pathlib import Path
from typing import Callable

from kiro_crew import asset_downloader
from kiro_crew.embeddings_model import (
    _sha256_file,
    default_model_path,
    embedding_model_is_custom,
    model_file_present,
)
from kiro_crew.embeddings_settings import (
    _DEFAULT_MODEL_URL,
    _DOWNLOAD_BACKOFF_BASE_SECS,
    _DOWNLOAD_BACKOFF_CAP_SECS,
    _DOWNLOAD_MAX_ATTEMPTS,
    _GGUF_MIN_BYTES,
    _GGUF_SHA256,
    _HTTP_CHUNK_BYTES,
    _HTTP_TIMEOUT_SECS,
    _MODEL_URL_ENV,
    _PROGRESS_EVERY_BYTES,
    _SKIP_DOWNLOAD_ENV,
    _read_memory_config,
)

logger = logging.getLogger("kiro_crew.embeddings")


# ── Model download manager ──


async def _run_download_on_daemon_thread(fn: "Callable[[], tuple[bool, str]]") -> tuple[bool, str]:
    """Run the blocking download step on a DAEMON thread, awaitable from asyncio.

    Deliberately not ``run_in_executor``: both the loop's default executor and
    ``ThreadPoolExecutor`` use non-daemon threads that are joined at interpreter
    exit — an in-flight 610MB download would hang a Ctrl-C or a finished
    one-shot CLI for up to the HTTP timeout. A daemon thread is abandoned at
    exit instead (the orphaned staging file is unlinked on the next attempt).
    Cancelling the awaiting task detaches from — but does not interrupt — the
    thread; the manager's asyncio lock prevents a second concurrent download.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    result: list[tuple[bool, str]] = []

    def _worker() -> None:
        try:
            result.append(fn())
        except Exception as exc:  # pragma: no cover - fn already catches
            result.append((False, f"download thread crashed: {exc}"))
        finally:
            loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_worker, name="kc-model-download", daemon=True).start()
    await done.wait()
    return result[0]


def _operator_env_model_url() -> str:
    """The ``KIROCREW_EMBED_MODEL_URL`` override if set and https, else ``""``."""
    env_url = os.environ.get(_MODEL_URL_ENV, "").strip()
    return env_url if env_url.lower().startswith("https://") else ""


def _resolve_model_url() -> str:
    """Resolve the model download URL: env > config knob > CDN default.

    The ``KIROCREW_EMBED_MODEL_URL`` env var wins (mirrored/airgapped
    deployments), then a non-empty ``memory.embed_model_url`` in
    ``config.json``, then the public CDN default. The config file is read
    raw (not via the full loader) so the download thread never depends on
    the config dataclass import graph. Overrides must be ``https://`` —
    other schemes (``file://``, ``http://``) are rejected so an
    operator-controlled value can't read local files or fetch plaintext.
    Whatever the source, the download is only trusted after the streaming
    sha256 matches ``_GGUF_SHA256``.
    """
    env_url = os.environ.get(_MODEL_URL_ENV, "").strip()
    if env_url:
        if env_url.lower().startswith("https://"):
            return env_url
        logger.warning(
            "%s must be an https:// URL — ignoring the override and using " "the CDN default",
            _MODEL_URL_ENV,
        )
    cfg_url = str(_read_memory_config().get("embed_model_url", "") or "").strip()
    if cfg_url:
        if cfg_url.lower().startswith("https://"):
            return cfg_url
        logger.warning(
            "memory.embed_model_url must be an https:// URL — ignoring "
            "the override and using the CDN default",
        )
    return _DEFAULT_MODEL_URL


def redact_model_url(url: str) -> str:
    """Return *url* safe for logs/terminal: strip userinfo, query, fragment.

    A private-mirror override may carry credentials in userinfo or a signed
    query string (e.g. presigned URLs). Only scheme + host + path are ever
    logged or printed; the full URL is used exclusively for the request.

    Deliberately NOT :func:`asset_downloader.redact_url`, which drops the path
    too: ``kirocrew doctor`` prints this to tell the operator WHICH model file
    resolved, and the model url's path is the operator's own override or the
    shipped default — never a value a remote party chose.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "<unparseable-url>"


class ModelDownloadManager:
    """Background download of the embedding GGUF from the CDN, with retries.

    The gateway kicks ``ensure_model()`` as a background task at startup, so
    boot is never blocked by the 610MB transfer. ``status`` is a plain dict
    readable at any time by the dashboard status endpoint. Concurrent
    ``ensure_model()`` calls (startup task + a dashboard Enable click) share
    one in-flight download via ``_lock``.
    """

    def __init__(self, target: Path | None = None):
        self._target = target or default_model_path()
        self._lock: asyncio.Lock | None = None  # created lazily inside the running loop
        self.status: dict[str, object] = {"step": "idle", "error": "", "attempt": 0}

    @property
    def target(self) -> Path:
        return self._target

    def model_ready(self) -> bool:
        return model_file_present(self._target)

    async def ensure_model(self, attempts: int = 1) -> bool:
        """Download the model if absent. Returns True when the file is in place.

        Runs up to ``attempts`` download cycles with exponential backoff between
        failures (network-unstable hosts recover automatically; a further
        retry also happens on every gateway boot and on each dashboard Retry
        click). Skipped entirely (returns False) when the
        ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` escape hatch is set — test runs
        must never trigger a 610MB model download.
        """
        if await asyncio.to_thread(self.model_ready):
            self.status = {"step": "ready", "error": "", "attempt": 0}
            return True
        if await asyncio.to_thread(embedding_model_is_custom):
            # Reached via the dashboard's Enable/Retry click. Downloading the
            # bundled model here would install a second model the embedder is
            # not using, and would imply the custom one had been replaced.
            msg = "a custom embedding model is configured (memory.embed_model_path)"
            logger.info("Skipping default model download — %s", msg)
            self.status = {"step": "custom_model", "error": "", "attempt": 0}
            return False
        if os.environ.get(_SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1 — skipping embedding model download", _SKIP_DOWNLOAD_ENV)
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Re-check under the lock: a concurrent caller may have finished it.
            if await asyncio.to_thread(self.model_ready):
                self.status = {"step": "ready", "error": "", "attempt": 0}
                return True
            last_error = "download failed"
            for attempt in range(1, max(1, attempts) + 1):
                self.status = {"step": "downloading", "error": "", "attempt": attempt}
                logger.info(
                    "Downloading embedding model (attempt %d/%d)...", attempt, max(1, attempts)
                )
                # Daemon thread: the blocking download must never pin
                # interpreter exit (Ctrl-C / one-shot CLI) — see helper docs.
                ok, err = await _run_download_on_daemon_thread(self._download_once)
                if ok:
                    self.status = {"step": "ready", "error": "", "attempt": attempt}
                    logger.info("Embedding model installed at %s", self._target)
                    return True
                last_error = err
                logger.warning(
                    "Embedding model download attempt %d/%d failed: %s",
                    attempt,
                    max(1, attempts),
                    err,
                )
                if attempt < attempts:
                    delay = min(
                        _DOWNLOAD_BACKOFF_BASE_SECS * (2 ** (attempt - 1)),
                        _DOWNLOAD_BACKOFF_CAP_SECS,
                    )
                    self.status = {"step": "waiting_retry", "error": err, "attempt": attempt}
                    await asyncio.sleep(delay)
            self.status = {"step": "failed", "error": last_error, "attempt": max(1, attempts)}
            return False

    # ── blocking helpers (run in executor) ──

    def _salvage_legacy_ollama_blob(self) -> bool:
        """Copy the GGUF from a legacy Ollama install instead of re-downloading.

        Users migrating from the Ollama-era embeddings already hold the exact
        model bytes on disk: Ollama stores layer blobs content-addressed as
        ``~/.ollama/models/blobs/sha256-<digest>``, and ``ollama pull`` of the
        same published GGUF stored it byte-identical. Copying locally saves
        the 610MB transfer. Best-effort: any failure falls through to the
        normal download; the copy is sha256-verified like a real download.
        """
        blobs_root = os.environ.get("OLLAMA_MODELS") or (Path.home() / ".ollama" / "models")
        blob = Path(blobs_root) / "blobs" / f"sha256-{_GGUF_SHA256}"
        try:
            if not blob.is_file() or blob.stat().st_size < _GGUF_MIN_BYTES:
                return False
            if _sha256_file(blob) != _GGUF_SHA256:
                return False
            self._install_file(blob, copy=True)
            logger.info("Reused embedding model from legacy Ollama blob store (%s)", blob)
            return True
        except OSError:
            logger.debug("Legacy Ollama blob salvage failed", exc_info=True)
            return False

    def _install_file(self, src: Path, *, copy: bool) -> None:
        """Atomically install *src* as the target model file.

        Stages into the TARGET directory (same filesystem → ``os.replace`` is
        atomic) under a per-process unique name so two concurrent processes
        (gateway + one-shot CLI) can never interleave writes into a shared
        staging file. The final replace is atomic either way — last writer
        wins with an identical, sha-verified payload.
        """
        self._target.parent.mkdir(parents=True, exist_ok=True)
        staging = self._target.parent / f".{self._target.name}.{os.getpid()}.tmp"
        try:
            if copy:
                shutil.copyfile(src, staging)
            else:
                # Cross-filesystem safe: shutil.move copies when rename fails.
                shutil.move(str(src), str(staging))
            os.replace(staging, self._target)
        finally:
            staging.unlink(missing_ok=True)

    def _download_once(self) -> tuple[bool, str]:
        """Try sources in order: Ollama salvage → HTTPS CDN."""
        # Migration fast path: users coming from the Ollama-era embeddings
        # already have the identical GGUF in Ollama's content-addressed store.
        if self._salvage_legacy_ollama_blob():
            return True, ""
        # HTTPS download from the CloudFront CDN (works for everyone, no
        # git/SSH and no cloud SDK required). Reports byte-level progress.
        return self._download_via_https()

    def _download_via_https(self) -> tuple[bool, str]:
        """Download the GGUF from the CDN through the shared transfer engine.

        Everything about the transfer — streamed sha256, atomic install, the
        wording of each failure — is :mod:`kiro_crew.asset_downloader`'s. What
        stays here is the part that is about the MODEL: which url to resolve, the
        sha and size pins, and turning byte counts into the ``status`` dict the
        dashboard renders a progress bar from.

        ``resume=False``: the staging name carries this process's pid so a
        gateway and a one-shot CLI can never interleave writes into a shared
        partial, and a resumable transfer needs the opposite (one stable name).
        For a 610MB one-shot pull that a caller already retries for hours,
        restarting is the simpler correct behaviour.
        """
        url = _resolve_model_url()
        # Only the OPERATOR's env override may redirect across https hosts: it
        # names their own mirror, and the common mirror shapes hop to a storage
        # host. The config knob is agent-writable and the CDN default is not
        # ours to trust, so both keep the host pin. The sha256 pin decides
        # what is installed either way.
        operator_mirror = url == _operator_env_model_url()

        def _progress(done: int, total: int) -> None:
            self.status = {
                "step": "downloading",
                "error": "",
                "attempt": self.status.get("attempt", 0),
                "bytes_downloaded": done,
                "bytes_total": total,
            }

        def _verifying() -> None:
            self.status = {
                "step": "verifying",
                "error": "",
                "attempt": self.status.get("attempt", 0),
            }

        return asset_downloader.download_to(
            self._target,
            url,
            sha256=_GGUF_SHA256,
            min_bytes=_GGUF_MIN_BYTES,
            resume=False,
            staging=self._target.parent / f".{self._target.name}.http.{os.getpid()}.tmp",
            timeout_secs=_HTTP_TIMEOUT_SECS,
            chunk_bytes=_HTTP_CHUNK_BYTES,
            progress_every_bytes=_PROGRESS_EVERY_BYTES,
            on_progress=_progress,
            on_verifying=_verifying,
            allow_cross_host_redirects=operator_mirror,
            label="embedding model",
        )


_download_manager: ModelDownloadManager | None = None
_download_manager_lock = threading.Lock()
# Module-level reference to the in-flight download task. asyncio only holds
# weak references to tasks — without this anchor a caller that drops the
# return value (e.g. cli_server) could see the download garbage-collected
# mid-transfer.
_download_task: "asyncio.Task[bool] | None" = None


def model_download_manager() -> ModelDownloadManager:
    """Process-wide download manager singleton (shared by gateway + dashboard)."""
    global _download_manager
    with _download_manager_lock:
        if _download_manager is None:
            _download_manager = ModelDownloadManager()
        return _download_manager


def reset_download_manager() -> None:
    """Drop the singleton (tests, KIROCREW_HOME changes)."""
    global _download_manager, _download_task
    with _download_manager_lock:
        _download_manager = None
        if _download_task is not None and not _download_task.done():
            _download_task.cancel()
        _download_task = None


def start_background_model_download() -> "asyncio.Task[bool] | None":
    """Kick the default-on background model download. Returns the task or None.

    Called from gateway/server startup. Returns None when a custom model is
    configured (the bundled model must never overwrite or compete with it —
    this is what makes a custom model survive a default-model version bump),
    when the model is already present (nothing to do), or when downloads are
    skipped via env. Idempotent: a second call while a download is in flight
    returns the existing task instead of spawning another.
    """
    global _download_task
    if embedding_model_is_custom():
        logger.info("Custom embedding model configured — skipping the default model download")
        return None
    mgr = model_download_manager()
    if mgr.model_ready():
        mgr.status = {"step": "ready", "error": "", "attempt": 0}
        return None
    if os.environ.get(_SKIP_DOWNLOAD_ENV) == "1":
        return None
    if _download_task is not None and not _download_task.done():
        return _download_task
    _download_task = asyncio.create_task(mgr.ensure_model(attempts=_DOWNLOAD_MAX_ATTEMPTS))
    return _download_task

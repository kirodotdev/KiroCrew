"""Which embedding model file is active, and what it is.

The bundled model's path, the operator's custom model (validated, refused when
sensitive, identified by a content digest and verified in the background), and
the answer every caller asks first: is the active model file present.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import NamedTuple

from kiro_crew.config.loader import config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.embeddings_gguf import gguf_declared_pooling
from kiro_crew.embeddings_settings import (
    _DEFAULT_DIM,
    _GGUF_FILENAME,
    _GGUF_MIN_BYTES,
    _MODEL_PATH_ENV,
    _MODELS_DIR_NAME,
    _POOLING_TYPE_LAST,
    _POOLING_TYPES,
    _read_memory_config,
)
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger("kiro_crew.embeddings")


# ── Per-model pooling ──

# A custom file whose GGUF declares mean or CLS pooling is recorded with that
# pooling after its digest (`custom:sha256:<digest>@mean`), so the pooling is part
# of what names its vector space; last-token keeps the plain `custom:sha256:<digest>`.
_CUSTOM_LABEL = "custom"
_POOLING_SUFFIX_SEPARATOR = "@"


class _ParsedModelId(NamedTuple):
    """The parts of a model id (see :func:`_parse_model_id`)."""

    label: str
    digest: str  # "" unless it is 64 lowercase hex characters
    pooling: str  # the text after the digest's `@`, "" when there is none


def _parse_model_id(model_id: str) -> _ParsedModelId:
    """Split *model_id* into its label, its digest and the pooling after the digest.

    The suffix is provenance, not just syntax. Every id an earlier release computed
    is ``<label>:sha256:<digest>`` with a 64-hex digest, and it kept a configured id
    only when the text after its last ``:sha256:`` was such a digest, re-hashing
    anything else. So no stored vector sits under an id ending in ``@mean`` or
    ``@cls``: that pooling always arrives with a fresh vector space, while a label
    an operator typed, ``custom@mean`` included, names no pooling.
    """
    label, separator, rest = model_id.rpartition(":sha256:")
    digest, _at, pooling = rest.partition(_POOLING_SUFFIX_SEPARATOR)
    valid = (
        bool(separator) and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
    )
    return _ParsedModelId(label, digest if valid else "", pooling)


def model_pooling_type(model_id: str) -> int:
    """The llama.cpp pooling the model named by *model_id* runs with.

    Only an id the loader writes names a pooling: ``custom:sha256:<digest>@mean``
    or ``@cls``. Everything else -- the bundled model, a plain ``custom`` id, an
    operator's label with or without a suffix -- runs with last-token pooling.
    """
    label, digest, pooling = _parse_model_id(model_id)
    if label == _CUSTOM_LABEL and digest and pooling in _POOLING_TYPES:
        return _POOLING_TYPES[pooling]
    return _POOLING_TYPE_LAST


# ── Model paths ──


def models_dir() -> Path:
    """Directory where downloaded embedding models live (respects KIROCREW_HOME)."""
    return config_dir() / _MODELS_DIR_NAME


def default_model_path() -> Path:
    """On-disk path of the BUNDLED model (the CDN download target)."""
    return models_dir() / _GGUF_FILENAME


class CustomModelSpec(NamedTuple):
    """A user-configured local embedding model.

    ``error`` is non-empty when the configured path is unusable. Such a spec is
    still returned (rather than falling back to the bundled model) so a typo can
    never silently swap the user's vector space back to the default and trigger a
    full re-embed behind their back: embeddings simply stay unavailable, memory
    degrades to keyword search, and the reason is logged and surfaced by
    ``kirocrew doctor``.
    """

    path: Path
    model_id: str
    dim: int
    error: str
    error_code: str = ""


class _ModelIdentityUnverified(OSError):
    """The model needs off-loop weight verification before it can be served."""


LEGACY_EMBEDDING_WARNING = (
    "These memory vectors were built before the model file that produced them was recorded. "
    "If you have changed the model file since, reapply it in Memory settings (Embedding Model) "
    "to rebuild them."
)

_model_verification_lock = threading.Lock()
_model_identity_lock = threading.Lock()
_model_verification_thread: threading.Thread | None = None


def legacy_embedding_ids(value: object) -> list[str]:
    """Accept compatibility labels only when the whole value is a list of strings."""
    return (
        value if isinstance(value, list) and all(isinstance(label, str) for label in value) else []
    )


def _verify_custom_model(path: Path, configured: str, recorded_stamp: object, config: Path) -> str:
    """Verify and persist only the configuration and file generation inspected."""
    from kiro_crew.config.loader import ConfigReadError, update_config_locked

    with _model_identity_lock:
        if _is_sensitive_model_path(path):
            raise OSError("custom model path is protected")
        stamp = _model_file_stamp(path)
        model_id = _custom_model_id(path, configured, recorded_stamp=recorded_stamp)
        if recorded_stamp == list(stamp) and model_id == configured:
            return model_id

        inherited = False

        def update(data: dict) -> dict | None:
            nonlocal inherited
            memory = data.get("memory", {})
            if (
                isinstance(memory, dict)
                and Path(str(memory.get("embed_model_path", "") or "").strip()).expanduser() == path
                and str(memory.get("embed_model_id", "") or "").strip() == configured
                and _model_file_stamp(path) == stamp
            ):
                if (
                    not memory.get("embed_model_stamp")
                    and ":sha256:" not in configured
                    # Legacy vectors were made with last-token pooling; a model
                    # running another rebuilds them, so it inherits nothing.
                    and model_pooling_type(model_id) == _POOLING_TYPE_LAST
                ):
                    labels = {f"custom:{path.name}:{stamp[2]}"}
                    if configured:
                        labels.add(configured)
                    memory["embed_model_legacy_ids"] = sorted(labels)
                    inherited = True
                elif memory.get("embed_model_id") != model_id:
                    memory.pop("embed_model_legacy_ids", None)
                memory["embed_model_id"] = model_id
                memory["embed_model_stamp"] = list(stamp)
                return data
            return None

        try:
            update_config_locked(config, mutate=update)
        except ConfigReadError as exc:
            raise OSError(
                "custom model identity could not be persisted: unreadable config"
            ) from exc
        if inherited:
            logger.warning(LEGACY_EMBEDDING_WARNING)
        return model_id


def _start_model_verification(path: Path, configured: str, recorded_stamp: object) -> None:
    """Keep one off-loop verification worker; later polls retry a changed file."""
    global _model_verification_thread
    config = config_path()
    with _model_verification_lock:
        if _model_verification_thread is not None and _model_verification_thread.is_alive():
            return

        def verify() -> None:
            try:
                _verify_custom_model(path, configured, recorded_stamp, config)
            except Exception:
                logger.warning("Custom model identity verification failed", exc_info=True)

        _model_verification_thread = threading.Thread(
            target=verify, name="kc-model-verify", daemon=True
        )
        _model_verification_thread.start()


def _refuse_on_event_loop() -> None:
    """Raise :class:`_ModelIdentityUnverified` when called on a running event loop.

    Called first in each cached file read, so a loop caller that misses the cache
    raises instead of reading; ``lru_cache`` does not cache the exception, and the
    off-loop verification worker fills the entry.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise _ModelIdentityUnverified(
            "custom model identity is unverified; verification is running"
        )


@functools.lru_cache(maxsize=8)
def _model_content_digest(path: Path, stamp: tuple[int, ...]) -> str:
    """Reuse the digest until file identity, size or write timestamps change."""
    _refuse_on_event_loop()
    digest = _sha256_file(path)
    if _model_file_stamp(path) != stamp:
        raise OSError("embedding model changed while computing its identity")
    return digest


def _model_file_stamp(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _model_file_stamp_unaliased(path: Path) -> tuple[int, ...] | None:
    """:func:`_model_file_stamp` for a load, judged on one descriptor. ``None`` refuses it.

    The bundled GGUF has no digest at load, so a second hard link sitting in a tree the
    sandbox can write is a writable handle onto the weights the next load maps. For a file
    under the live :func:`models_dir`, ``sandbox.require_real_model_path`` first refuses a
    symlink or junction at any name from ``models`` down to the file; the file is then
    opened ``O_NOFOLLOW`` and ``sandbox.require_unaliased_model_file`` judges that
    descriptor; the stamp comes from the same ``fstat``, so the comparison after the
    constructor proves llama.cpp mapped the inode judged here. A link or a second hard
    link refuses this LOAD, never a spawn. A custom model outside ``models_dir`` is the
    operator's own file, outside the seal, and keeps the plain stamp.
    """
    models_root = os.path.abspath(models_dir())
    try:
        inside = os.path.commonpath([os.path.abspath(path), models_root]) == models_root
    except ValueError:  # different Windows drives: not under the models tree
        inside = False
    if not inside:
        return _model_file_stamp(path)
    # Lazy: keeps the sandbox module out of this module's import graph, the same
    # direction sandbox itself uses when it reads _GGUF_FILENAME.
    from kiro_crew import sandbox

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        sandbox.require_real_model_path(str(path), models_root=models_root)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.EMLINK) and not os.path.islink(path):
                raise
            shown = sandbox.safe_terminal_line(str(path))
            raise sandbox.SandboxCeilingUnsealable(
                f"the embedding model file {shown} is a SYMLINK. The seal covers paths, so "
                "its target could remain writable outside the model tree. Keep the weights "
                "as real files under models/; to put them on another disk, relocate the "
                "whole data home (KIROCREW_HOME), so the seal covers them."
            ) from exc
        try:
            sandbox.require_unaliased_model_file(str(path), fd=fd)
            info = os.fstat(fd)
        finally:
            os.close(fd)
    except sandbox.SandboxCeilingUnsealable as exc:
        logger.error("Refusing to load the embedding model: %s", exc)
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _custom_model_id(path: Path, configured: str, *, recorded_stamp: object = None) -> str:
    """Reuse a verified file stamp; never hash model weights on the event loop."""
    stamp = _model_file_stamp(path)
    recorded = _parse_model_id(configured)
    if recorded_stamp == list(stamp) and recorded.digest:
        return configured
    digest = _model_content_digest(path, stamp)
    if recorded.digest == digest:
        return configured
    # A model recorded for the first time takes its file's declared pooling into
    # its id, and so does a file replaced under a `custom` id, whose suffix named
    # the old file's pooling (the digest changed, so it re-embeds anyway). An
    # operator's own label carries over to the new digest and names no pooling.
    if not configured or (recorded.digest and recorded.label == _CUSTOM_LABEL):
        return f"{_CUSTOM_LABEL}:sha256:{digest}{_new_model_pooling(path, stamp)}"
    label = configured.split(":sha256:", 1)[0]
    return f"{label}:sha256:{digest}"


@functools.lru_cache(maxsize=8)
def _new_model_pooling(path: Path, stamp: tuple[int, ...]) -> str:
    """The id suffix naming the pooling *path*'s GGUF declares, cached per stamp.

    ``""`` for last-token. A loop caller that misses the cache is refused before
    the GGUF header is walked, exactly as a digest miss is, and the off-loop
    verification worker fills the entry.
    """
    _refuse_on_event_loop()
    pooling = gguf_declared_pooling(path)
    if _POOLING_TYPES[pooling] == _POOLING_TYPE_LAST:
        return ""
    return f"{_POOLING_SUFFIX_SEPARATOR}{pooling}"


def _is_sensitive_model_path(path: Path) -> bool:
    """True when *path* is a credential store or the governance trust-root.

    Failing CLOSED on an evaluation error is deliberate — a gate that cannot be
    evaluated must not silently widen what this knob is allowed to open.
    """
    try:
        return is_sensitive_path(str(path))
    except Exception:
        # The path is deliberately NOT logged. These are the two sites where the
        # value is, by this gate's own determination, likely to name a credential
        # store or the governance trust-root, and a gateway log may be shipped or
        # shared. The exact path stays available on demand: `kirocrew doctor`
        # prints it, the dashboard returns it in `setup_error`, and the resolver
        # reports it through CustomModelSpec.error.
        logger.warning(
            "Could not evaluate the sensitive-path gate for the configured custom "
            "embedding model — refusing it. Run 'kirocrew doctor' for the path.",
            exc_info=True,
        )
        return True


def validate_custom_model_path(raw: str, origin: str = "embed_model_path") -> tuple[Path, str, str]:
    """Validate a candidate model path. Returns ``(resolved_path, error, code)``.

    ``error`` is empty when the path is usable; ``code`` is a stable
    lower_snake identifier for it. The prose is advisory (it carries the concrete
    path and size), the code is the contract — the dashboard renders a LOCALIZED
    message keyed on it, so returning prose alone would be untranslatable by
    construction.

    Split out of :func:`resolve_custom_model` so a request handler can validate a
    path the user just typed WITHOUT writing it to config first — the dashboard
    needs to reject a bad path before it is persisted, and the resolver only ever
    reads what is already on disk.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return path, f"{origin} must be an absolute path (got {raw!r})", "model_path_not_absolute"
    if _is_sensitive_model_path(path):
        # Same gate every other file-access surface uses (hooks, artifacts,
        # dashboard file I/O, knowledge indexing). A GGUF is handed to native
        # llama.cpp to mmap and parse, so pointing this knob at a credential
        # store or the governance trust-root would feed those bytes to native
        # code. Refuse by path rather than relying on the parse to fail.
        return path, f"{origin} points at a protected location: {path}", "model_path_protected"
    if not path.exists():
        return (
            path,
            f"{origin} points at a file that does not exist: {path}",
            "model_path_not_found",
        )
    if not path.is_file():
        return path, f"{origin} is not a regular file: {path}", "model_path_not_a_file"
    try:
        size = path.stat().st_size
        if size <= _GGUF_MIN_BYTES:
            return (
                path,
                f"{origin} is too small to be model weights ({size} bytes): {path}",
                "model_path_too_small",
            )
    except OSError as exc:
        return path, f"{origin} could not be read: {exc}", "model_path_unreadable"
    return path, "", ""


def bundled_embedding_dim() -> int:
    """Output width of the bundled model, for reverting to it.

    Exposed so the dashboard does not duplicate the literal: a future change to
    the bundled model would otherwise leave a stale 1024 in the handler and make
    every revert-to-bundled write an unloadable config.
    """
    return _DEFAULT_DIM


def resolve_custom_model() -> "CustomModelSpec | None":
    """Resolve the user-configured local model, or ``None`` to use the bundled one.

    Resolution order for the path: ``KIROCREW_EMBED_MODEL_PATH`` env, then the
    ``memory.embed_model_path`` config knob. Returning ``None`` means "no custom
    model configured" — it never means "configured but broken", which is
    reported via :attr:`CustomModelSpec.error` instead.
    """
    memory_cfg = _read_memory_config()
    raw = os.environ.get(_MODEL_PATH_ENV, "").strip()
    origin = _MODEL_PATH_ENV
    if not raw:
        raw = str(memory_cfg.get("embed_model_path", "") or "").strip()
        origin = "memory.embed_model_path"
    if not raw:
        return None

    dim = _DEFAULT_DIM
    raw_dim = memory_cfg.get("embedding_dim")
    if isinstance(raw_dim, int) and not isinstance(raw_dim, bool) and raw_dim > 0:
        dim = raw_dim
    configured_id = str(memory_cfg.get("embed_model_id", "") or "").strip()

    path, error, code = validate_custom_model_path(raw, origin)
    if (not configured_id or configured_id.split(":sha256:", 1)[0] == _CUSTOM_LABEL) and Path(
        str(memory_cfg.get("embed_model_path", "") or "").strip()
    ).expanduser() != path:
        # Only memory.embed_model_path is ever recorded (_verify_custom_model), so
        # a file named only by the env var is never recorded; it keeps the plain
        # label and last-token pooling rather than taking a pooling no apply chose,
        # whether from its own header or from an id recorded for another file.
        configured_id = _CUSTOM_LABEL
    model_id = "custom:unavailable"
    if not error:
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                model_id = _verify_custom_model(
                    path, configured_id, memory_cfg.get("embed_model_stamp"), config_path()
                )
            else:
                model_id = _custom_model_id(
                    path, configured_id, recorded_stamp=memory_cfg.get("embed_model_stamp")
                )
                if _model_identity_lock.locked():
                    raise _ModelIdentityUnverified(
                        "custom model identity is unverified; verification is running"
                    )
        except _ModelIdentityUnverified as exc:
            model_id = "custom:unverified"
            error = str(exc)
            code = "model_identity_unverified"
            _start_model_verification(path, configured_id, memory_cfg.get("embed_model_stamp"))
        except OSError as exc:
            error = f"{origin} could not be read: {exc}"
            code = "model_verification_failed"
    _log_custom_model_error(error)
    return CustomModelSpec(path, model_id, dim, error, code)


# Last error reported by resolve_custom_model(), so a persistent misconfiguration
# is logged once instead of on every call. The resolver is invoked from hot-ish
# paths — model_file_present() runs it, and the dashboard polls the embedding
# status endpoint every ~2s — so logging unconditionally would flood the log for
# as long as the path stays broken. A CHANGED message still logs, and a resolve
# that succeeds clears the state, so a fix-then-break cycle is not swallowed.
_last_custom_model_error = ""


def _log_custom_model_error(error: str) -> None:
    global _last_custom_model_error
    if error != _last_custom_model_error:
        _last_custom_model_error = error
        if error:
            logger.error("Custom embedding model unusable — %s", error)


def embedding_model_is_custom() -> bool:
    """True when a custom model path is configured (valid or not).

    Gates the default-model download: a configured custom model must never be
    replaced by the bundled one, and a BROKEN custom path must not silently
    fall back to it either.
    """
    return resolve_custom_model() is not None


def active_model_path() -> Path:
    """Path of the model actually in use — the custom one when configured."""
    spec = resolve_custom_model()
    return spec.path if spec is not None else default_model_path()


def model_file_present(path: Path | None = None) -> bool:
    """True when the GGUF exists and is not a truncated/placeholder file.

    Defaults to :func:`active_model_path`, so callers asking "is the embedding
    model on disk?" get an answer about the model actually in use. Callers that
    specifically mean the bundled download target (the download manager) pass
    :func:`default_model_path` explicitly.
    """
    target = path or active_model_path()
    try:
        return target.is_file() and target.stat().st_size > _GGUF_MIN_BYTES
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

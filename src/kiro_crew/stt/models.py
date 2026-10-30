"""The whisper.cpp model catalog, and a sha256-pinned downloader for it.

Speech recognition needs weights, and shipping them in the wheel is not an
option: the smallest useful one is 148 MB. So the first use of voice input
fetches one model, once, and every later session loads it from disk.

**The sha256 pin is the trust anchor, and not only for the network fetch.** A
model is written to its final path only after the digest computed while streaming
matches the pinned one, so a tampered mirror, a truncated transfer or a
captive-portal HTML body can only fail verification.

A file ALREADY on disk is verified too, on every load (see
``ModelStore._verified_on_disk``), because provenance is not enough: a same-size
file dropped over the weights would otherwise be trusted forever. Not cached
against the file's metadata either -- ``os.utime`` is available to anything that can
write the file, so a size-and-mtime memo is forgeable by the same actor it is meant
to catch.

The digest is the second line, not the first. Verifying and then handing a PATH to a
native loader leaves a window in which the bytes can be swapped, and re-hashing
cannot close it because the loader re-opens by name. What closes it is that
``<data home>/models`` is WRITE-PROTECTED from the agent
(``security._WRITE_PROTECTED_HOME_PATHS`` for the file tools; the agent's shell is
confined by the OS sandbox, not by a matcher over command text), so the verified
bytes are the loaded bytes. Adding a row to :data:`CATALOG` therefore needs no edit
in ``security``: the directory entry covers any file beneath it. Kiro Crew's own
downloader writes here directly and does not route through the tool gate, so a
first-run fetch and a re-download after a failed check both still work. That is a
deliberate divergence from ``embeddings.ModelDownloadManager``, which documents the
same size-only trade for its own GGUF -- that reasoning weighed a corrupted
download, not a writable directory and an agent with a shell.

This deliberately does not reuse that manager. It is bound to one pinned
artifact through ``default_model_path()``, ``_GGUF_SHA256`` and the
``memory.embed_model_path`` custom-model escape hatch, so sharing it would mean
generalising the memory subsystem's download path to carry a catalog. The shape,
the doctrine and the ``KIROCREW_SKIP_MODEL_DOWNLOAD`` escape hatch are mirrored
instead, which keeps the two subsystems independent.

Digests here were obtained by downloading each file and hashing it, not by
trusting an upstream header: HuggingFace's ``X-Linked-Etag`` for these objects is
not the content digest. Two of them (``base``, ``small``) independently match the
digests kiro-cli pins for the same models, which is a second source on the same
bytes.

A model outside this catalog is reachable through ``stt.model = "custom"`` plus
``stt.custom_model_path`` and ``stt.custom_model_sha256``. The path names a file
the user has already placed in :func:`models_dir`; nothing is ever downloaded for
it. Two checks stand where a catalog pin stands: the path must resolve -- symlinks
followed -- to a regular file INSIDE that directory, and the file must hash to the
configured digest on every load. A file that fails either is refused, and left
where it is: it is the user's file, not one this module fetched.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Upstream for the ggml conversions of OpenAI's Whisper weights. This is the
#: canonical publisher that whisper.cpp itself points at. Unlike the embedding
#: GGUF, Kiro Crew serves no mirror of these, so the pin below is doing the whole
#: job of establishing what we accept.
MODELS_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

#: Operator override for the base URL, for an air-gapped or mirrored install. The
#: pin still applies, so a mirror can serve the bytes but cannot substitute them.
MODEL_URL_ENV = "KIROCREW_WHISPER_MODEL_BASE_URL"

#: Shared with the embedding downloader on purpose: one switch means "this
#: process must not pull model weights over the network", and a test run wants
#: that to hold for every subsystem, not one of them.
SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"

#: Read size while streaming a download. Large enough that the digest update and
#: the progress callback are not per-packet work, small enough that a cancelled
#: download stops promptly.
_CHUNK_BYTES = 1 << 20

#: ``0`` where the platform has no such flag, so one ``open`` call covers both.
#: ``O_NOFOLLOW`` is POSIX-only and ``O_BINARY`` is Windows-only.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)

#: The ``stt.model`` value that selects a user-supplied model instead of a catalog
#: row. Not a member of :data:`CATALOG`: it names no fixed artifact, so it has no
#: size to advertise and no digest to state here -- both come from configuration.
CUSTOM_MODEL = "custom"

#: Length of a hex-encoded sha256, which is the whole shape a digest has to have
#: before it is worth comparing anything against.
_SHA256_HEX_LEN = 64

#: Longest ``stt.custom_model_path`` worth resolving. A real file name under the
#: models directory is far shorter; the bound keeps a pasted blob out of a stat.
_CUSTOM_PATH_MAX_CHARS = 1024

#: Suffix for the staging file. Staged inside the TARGET directory so the final
#: step is a same-filesystem ``os.replace`` and therefore atomic; a staging area
#: under the system temp dir can land on a different device, where the rename
#: degrades into a copy that a reader can observe half-finished.
_STAGING_SUFFIX = ".part"

#: Per-socket-operation ceiling on the model download, NOT a ceiling on the whole
#: transfer: `urlopen`'s timeout bounds each individual read, so a 1.6 GB model on a
#: slow line still completes while a connection that stops delivering bytes fails
#: instead of hanging. Without it a stalled TCP connection held its worker for the
#: life of the process, and the only visible symptom was a download progress bar
#: frozen at some percentage -- indistinguishable from the slow-but-working case the
#: byte counter exists to prove apart.
_NETWORK_STALL_TIMEOUT_SECS = 60.0


@dataclass(frozen=True)
class WhisperModel:
    """One model the recogniser can load -- a catalog row, or the configured custom one.

    ``size_bytes`` is carried for two reasons: the UI states the download cost
    before asking for it, and a file whose size does not match cannot be the
    pinned artifact, which is a free pre-check before any expensive work. ``0``
    means the size is unknown, which only a custom model can be.

    ``custom_path`` is the configured ``stt.custom_model_path`` for a custom model
    and ``""`` for a catalog row. It is the value as configured, not a vetted
    location: :func:`custom_model_file` is what decides whether it may be opened.
    """

    name: str
    size_bytes: int
    sha256: str
    custom_path: str = ""

    @property
    def filename(self) -> str:
        if self.name == CUSTOM_MODEL:
            return Path(self.custom_path).name
        return f"ggml-{self.name}.bin"


#: The offered models, smallest first. Deliberately short. Every extra row is a
#: choice the user has to make before they can dictate a sentence, and the
#: accuracy ladder here already spans the useful range: ``tiny`` for a slow
#: machine, ``base`` for everyone, ``small`` when accents or jargon need it, and
#: ``large-v3-turbo`` for the accuracy ceiling. The English-only (``.en``)
#: variants are left out because they are a trap for a multilingual user and buy
#: little for an English one.
CATALOG: tuple[WhisperModel, ...] = (
    WhisperModel(
        "tiny",
        77_691_713,
        "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    ),
    WhisperModel(
        "base",
        147_951_465,
        "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    ),
    WhisperModel(
        "small",
        487_601_967,
        "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    WhisperModel(
        "large-v3-turbo",
        1_624_555_275,
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
    ),
)

#: The default balances download size and multilingual recognition. Inference
#: latency still depends on the native backend and competing host workloads;
#: model size alone does not establish real-time performance.
DEFAULT_MODEL = "base"

_BY_NAME: dict[str, WhisperModel] = {m.name: m for m in CATALOG}

#: The on-disk names the catalog downloads into, which a custom model may not use.
_CATALOG_FILENAMES: frozenset[str] = frozenset(m.filename for m in CATALOG)
_CATALOG_FILENAMES_FOLDED: frozenset[str] = frozenset(n.casefold() for n in _CATALOG_FILENAMES)

#: Names accepted from superseded configuration, mapped to the entry that best
#: honours what the user actually asked for. Without this table every one of them
#: falls back to the default, which for someone who deliberately picked the
#: accuracy ceiling is a silent downgrade to the second-smallest model.
#:
#: Two mappings deserve their reasoning stated, because they look like
#: substitutions and are not:
#:
#: - The full-size ``large`` lineage resolves to ``large-v3-turbo``. Turbo is the
#:   same encoder with a distilled decoder, so it keeps the accuracy the user was
#:   asking for while decoding several times faster.
#: - ``medium`` also resolves to ``large-v3-turbo``, which is an upgrade rather
#:   than a compromise: turbo is both more accurate and faster than medium, so
#:   there is no reading of "medium" that turbo does not satisfy better.
#:
#: The English-only (``.en``) names drop to their multilingual sibling of the same
#: size. That loses a little English accuracy and gains every other language,
#: which is the right default for a config value nobody will revisit.
_ALIASES: dict[str, str] = {
    "turbo": "large-v3-turbo",
    "large": "large-v3-turbo",
    "large-v1": "large-v3-turbo",
    "large-v2": "large-v3-turbo",
    "large-v3": "large-v3-turbo",
    "large-v3-turbo-q5_0": "large-v3-turbo",
    "large-v3-turbo-q8_0": "large-v3-turbo",
    "medium": "large-v3-turbo",
    "medium.en": "large-v3-turbo",
    "small.en": "small",
    "base.en": "base",
    "tiny.en": "tiny",
}


def canonical_name(name: str) -> str | None:
    """The catalog name *name* selects, or ``None`` when it names nothing.

    The distinction :func:`resolve` deliberately cannot make. ``resolve`` exists for
    a value already stored in ``config.json`` and must always answer with a usable
    model, so an unrecognised name degrades to the default. A WRITE path needs the
    opposite: an unknown name must leave the stored field alone, because silently
    replacing a good stored value with the default is worse than ignoring a bad
    request.

    Accepts the alias table as well as the catalog, which is what makes a superseded
    name still SETTABLE. A user whose stored model is ``medium`` has a value the
    catalog does not contain; a membership test against the catalog alone rejects it,
    so saving the Voice panel failed on a field they never edited.
    """
    if not isinstance(name, str):
        return None
    candidate = name.strip()
    if not candidate:
        return None
    canonical = _ALIASES.get(candidate, candidate)
    return canonical if canonical in _BY_NAME else None


def valid_custom_sha256(value: object) -> str:
    """Return *value* lower-cased if it is a hex sha256, else ``""``.

    Two carrier shapes are unwrapped first, because they are what a user actually
    has in the clipboard and neither is ambiguous: a ``sha256:`` prefix, as
    published beside an artifact, and ``sha256sum``'s own ``<hex>  <file>`` line.
    Unwrapping is not widening -- the result still has to be exactly 64 hex
    characters.
    """
    if not isinstance(value, str):
        return ""
    fields = value.strip().lower().split(maxsplit=1)
    digest = fields[0] if fields else ""
    for prefix in ("sha256:", "sha-256:", "sha256="):
        if digest.startswith(prefix):
            digest = digest[len(prefix) :]
            break
    if len(digest) != _SHA256_HEX_LEN:
        return ""
    return digest if all(c in "0123456789abcdef" for c in digest) else ""


def valid_custom_path(value: object) -> str:
    """Return *value* stripped if it has the SHAPE of a custom model path, else ``""``.

    Shape only -- a string, not blank, no NUL or other control character, of a sane
    length -- so it can run during a configuration load without touching the disk.
    Whether the path may actually be opened is :func:`custom_model_file`'s question,
    asked when a model is about to be used and when the dashboard saves the field.
    """
    if not isinstance(value, str):
        return ""
    path = value.strip()
    if not path or len(path) > _CUSTOM_PATH_MAX_CHARS:
        return ""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return ""
    return path


class CustomModelPathError(ValueError):
    """A custom model path that may not be used. The message is shown to the user.

    ``code`` is stable for the dashboard; the message never quotes the path, so it
    is safe in a log line and on any surface that renders it.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Different drives on Windows, or a mix of absolute and relative paths.
        return False


def custom_model_file(value: object) -> Path:
    """The real path of the custom model file *value* names. Raises when unusable.

    *value* is a file name relative to :func:`models_dir`, or an absolute path into
    it. It is usable only when it resolves -- every symlink followed -- to a regular
    file strictly inside that directory. The check runs twice, before and after
    resolution: before, so a ``..`` component and an absolute path elsewhere are
    refused without looking at the disk outside the directory; after, so a symlink
    inside the directory that points out of it is refused too.

    A ``..`` component is refused outright rather than collapsed. Collapsing it as
    text answers a different question than the OS does once a symlink precedes it
    (``alias/../x`` with ``alias -> .``), and the path returned here is the one the
    loader opens, so it has to be the OS's answer. ``~`` is not expanded, for the
    same reason: there is no home directory in this check, only the models folder.

    Raises :class:`CustomModelPathError` with a code of ``custom_model_path_empty``,
    ``custom_model_path_outside``, ``custom_model_path_missing`` or
    ``custom_model_path_not_file``.
    """
    raw = valid_custom_path(value)
    if not raw:
        raise CustomModelPathError(
            "custom_model_path_empty",
            "Enter the name of a model file in the speech models folder.",
        )
    root = models_dir()
    outside = CustomModelPathError(
        "custom_model_path_outside",
        "The model file must be inside the speech models folder.",
    )
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise outside
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = os.path.normpath(candidate)
    real_root = os.path.realpath(root)
    roots = {os.path.normpath(root), real_root}
    if lexical in roots or not any(_is_within(lexical, r) for r in roots):
        raise outside
    try:
        real = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CustomModelPathError(
            "custom_model_path_missing",
            "No file with that name is in the speech models folder.",
        ) from None
    if str(real) == real_root or not _is_within(str(real), real_root):
        raise outside
    if not real.is_file():
        raise CustomModelPathError(
            "custom_model_path_not_file",
            "That name is a folder, not a model file.",
        )
    # The catalog owns these names: preparing a built-in model deletes a file at its
    # path that fails the catalog pin and overwrites it with a download, so a custom
    # file there would be destroyed. Checked on the RESOLVED name, so a symlink that
    # points at a catalog file is refused too; staging files are never a model.
    # Compared case-folded, and by identity against any catalog file already there:
    # on a case-insensitive volume (the macOS default) `GGML-BASE.BIN` IS the
    # catalog's `ggml-base.bin`, so a byte-exact name test would let it through.
    in_root = os.path.samefile(real.parent, real_root)
    folded = real.name.casefold()
    reserved = (in_root and folded in _CATALOG_FILENAMES_FOLDED) or folded.endswith(_STAGING_SUFFIX)
    if not reserved:
        for name in _CATALOG_FILENAMES:
            try:
                if os.path.samefile(real, os.path.join(real_root, name)):
                    reserved = True
                    break
            except OSError:
                continue
    if reserved:
        raise CustomModelPathError(
            "custom_model_path_reserved",
            "That file name belongs to a built-in model. Rename your file, "
            "for example to ggml-custom.bin.",
        )
    return real


def custom_model(path: object, sha256: object) -> WhisperModel | None:
    """A :class:`WhisperModel` for the configured custom file, or ``None`` if unusable.

    ``None`` means the pair is absent or malformed, which callers turn into the same
    degrade-to-default the catalog uses for an unknown name. Both halves are
    required: a path with no digest would load bytes nobody pinned.

    A well-formed pair whose file is missing or outside the models directory still
    builds a model here, deliberately. Degrading it to the default would download
    and run a different model than the one the user chose, with nothing on screen to
    say so; kept, it fails at :meth:`ModelStore.ensure` with the reason shown.
    """
    configured = valid_custom_path(path)
    digest = valid_custom_sha256(sha256)
    if not configured or not digest:
        return None
    return WhisperModel(CUSTOM_MODEL, 0, digest, configured)


def _configured_custom_model() -> WhisperModel | None:
    """The custom model current configuration names, or ``None``.

    The import is deferred because ``config.sections`` imports THIS module at module
    scope to derive the accepted ``stt.model`` values. Reading configuration here
    cannot recurse: the config loader validates ``custom`` from its sibling fields
    instead of calling :func:`resolve`.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        stt = KiroCrewConfig.load().stt
        return custom_model(stt.custom_model_path, stt.custom_model_sha256)
    except Exception:
        logger.warning("Could not read the configured custom whisper model", exc_info=True)
        return None


def resolve(name: str) -> WhisperModel:
    """Return the model *name* selects, falling back to the default.

    Falls back with a warning rather than raising: this value arrives from
    ``config.json``, and an unrecognised model must degrade to a working default
    the way an unusable backend does, not fail the voice session that read it.
    That covers :data:`CUSTOM_MODEL` with no usable path/digest pair too.

    For ``custom`` this reads configuration, so async callers run it off the loop.
    """
    if name == CUSTOM_MODEL:
        custom = _configured_custom_model()
        if custom is not None:
            return custom
        logger.warning(
            "stt.model is %r but stt.custom_model_path and stt.custom_model_sha256 are "
            "not both set (a file name, 64 hex characters); using %r.",
            CUSTOM_MODEL,
            DEFAULT_MODEL,
        )
        return _BY_NAME[DEFAULT_MODEL]
    canonical = _ALIASES.get(name, name)
    model = _BY_NAME.get(canonical)
    if model is not None:
        return model
    logger.warning(
        "Unknown whisper model %r; using %r. Known models: %s",
        name,
        DEFAULT_MODEL,
        ", ".join(_BY_NAME),
    )
    return _BY_NAME[DEFAULT_MODEL]


def models_dir() -> Path:
    """Where whisper weights live. Respects ``KIROCREW_HOME``.

    A subdirectory of the shared ``models/`` tree rather than a sibling of it, so
    the embedding GGUF and these cannot collide on a filename and an operator
    clearing one does not take the other with it.
    """
    return config_dir() / "models" / "whisper"


def model_path(model: WhisperModel) -> Path:
    """Where *model*'s weights are. Touches the disk for a custom model.

    For a custom model this is the REAL path :func:`custom_model_file` resolves, so
    a residency key built from it names the same file :meth:`ModelStore.ensure`
    hashes. An unusable custom path answers the configured value joined to the
    models folder instead: a key for a file nothing will load.
    """
    if model.name == CUSTOM_MODEL:
        try:
            return custom_model_file(model.custom_path)
        except CustomModelPathError:
            configured = Path(model.custom_path)
            return configured if configured.is_absolute() else models_dir() / configured
    return models_dir() / model.filename


def is_present(model: WhisperModel) -> bool:
    """Whether *model* is on disk and the right size.

    The size check is what makes an interrupted download visible. A staging file
    is never at this path, so a wrong size here means a truncated or replaced
    file, and treating it as absent lets the next download overwrite it.

    A custom model has no pinned size, so it is present when its path is usable
    (see :func:`custom_model_file`) and the file is not empty. Its bytes are checked
    against the digest by ``ModelStore.ensure`` on every load.
    """
    if model.name == CUSTOM_MODEL:
        try:
            return custom_model_file(model.custom_path).stat().st_size > 0
        except (CustomModelPathError, OSError):
            return False
    try:
        return model_path(model).stat().st_size == model.size_bytes
    except OSError:
        return False


#: Empty file whose mtime records the last real (non-prewarm) decode on this host.
#: It holds no audio, no transcript and no model name: only "speech recognition was
#: used at about this time", which is what the boot prewarm needs to decide whether
#: loading 1.6 GB of weights on every launch is serving anyone.
_USAGE_MARKER = ".last-decode"

#: How often a busy gateway re-stamps the marker. The boot gate asks about DAYS,
#: so an hour of slack costs nothing, and every partial of a dictation does not
#: turn into a filesystem write.
_USAGE_STAMP_INTERVAL_SECS = 3600.0

_last_usage_stamp: float | None = None


def usage_marker_path() -> Path:
    return models_dir() / _USAGE_MARKER


def claim_usage_stamp() -> bool:
    """Whether a stamp is due now, claiming it if so. No I/O: safe on the event loop.

    True at most once per :data:`_USAGE_STAMP_INTERVAL_SECS` per process. The caller
    that gets True then runs :func:`write_usage_marker`, off the loop.
    """
    global _last_usage_stamp
    now = time.monotonic()
    if _last_usage_stamp is not None and now - _last_usage_stamp < _USAGE_STAMP_INTERVAL_SECS:
        return False
    _last_usage_stamp = now
    return True


def write_usage_marker() -> None:
    """Create or touch the usage marker. Blocking I/O; best-effort, never raises."""
    path = usage_marker_path()
    try:
        path.touch(exist_ok=True)
        os.utime(path)
    except OSError:
        logger.debug("Could not stamp the speech usage marker", exc_info=True)


def used_within(seconds: float) -> bool:
    """Whether a real decode was recorded in the last *seconds*. False when unknown."""
    try:
        return time.time() - usage_marker_path().stat().st_mtime <= seconds
    except OSError:
        return False


def prewarm_size_bytes(model: WhisperModel) -> int:
    """The size, in bytes, to hold *model* against a memory guard before loading it.

    The pinned size for a catalog row. A custom model has none, and a guard fed
    ``0`` admits any file, so its own size on disk stands in. ``0`` back means the
    file could not be sized, which the caller reads as "do not speculate".
    """
    if model.size_bytes:
        return model.size_bytes
    try:
        return custom_model_file(model.custom_path).stat().st_size
    except (CustomModelPathError, OSError):
        return 0


def _model_url(model: WhisperModel) -> str:
    base = os.environ.get(MODEL_URL_ENV, "").strip() or MODELS_BASE_URL
    return f"{base.rstrip('/')}/{model.filename}"


ProgressFn = Callable[[int, int], None]


class ModelDownloadError(RuntimeError):
    """A download did not produce a verified model file."""


def _sha256_file(path: Path) -> str:
    """Digest a file on disk. Blocking; callers run it off the loop.

    Read in `_CHUNK_BYTES` pieces rather than whole: the largest model is 1.6 GB, and
    reading that into memory to hash it would cost more than the hash.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _open_verified_nofollow(path: Path, sandbox: ModuleType) -> int:
    """Open *path* for the trust check without following a symlinked leaf.

    ``O_NOFOLLOW`` refuses a symlink at the leaf with ``ELOOP`` (``EMLINK`` on some BSDs).
    That is the same refusal ``sandbox.require_real_model_path`` makes for a linked step on
    the way, so it is raised the same way, as ``SandboxCeilingUnsealable`` naming the file
    and the remedy, rather than as an ``OSError`` that would abort voice preparation with
    nothing the operator can act on. Any other open error propagates unchanged.
    """
    try:
        return os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.EMLINK) and not os.path.islink(path):
            raise
        shown = sandbox.safe_terminal_line(str(path))
        raise sandbox.SandboxCeilingUnsealable(
            f"the verified model file {shown} is a SYMLINK. The seal "
            "covers paths, so its target could remain writable outside the model tree. "
            "Keep the weights as real files under models/; to put them on another disk, "
            "relocate the whole data home (KIROCREW_HOME), so the seal covers them."
        ) from exc


def _sha256_verified_file(path: Path) -> str:
    """Digest *path* and judge the very descriptor the bytes came from. Blocking.

    Separate from :func:`_sha256_file`, which the download paths use on a staging file
    this process just created with an exclusive open: a staging name cannot carry an
    operator's second link, and the decoder store shares that function too. This one is
    for the trust check on a file that was ALREADY on disk, where the alias question is
    live.

    One ``open`` for both answers, which is the point: hashing a name and then asking
    about that name are two resolutions with a swap window between them, so the digest
    would describe one inode and the refusal another. ``O_NOFOLLOW`` because a symlinked
    leaf must fail here rather than resolve somewhere the seal does not cover.

    Judged BEFORE the first byte is read, and the inode's stamp compared after the last.
    A judgement only at the end of the read can be raced: write through the alias into a
    chunk already hashed, unlink the alias, and the final ``fstat`` sees one link while
    the digest describes bytes the file does not hold. Before the read, an alias that
    exists refuses outright; across the read, a write, link or unlink through any handle
    changes the stamp, and a stamp that moved refuses too, because the digest then
    describes bytes other than the file's. A sandboxed child cannot create the alias in
    between: the seal refuses ``link(2)`` into or out of ``models/``.
    """
    # Imported here, not at module scope: this keeps the sandbox module out of the stt
    # import graph, mirroring the lazy direction sandbox itself uses for stt.
    from kiro_crew import sandbox

    fd = _open_verified_nofollow(path, sandbox)
    try:
        # Raises SandboxCeilingUnsealable, which is NOT a failed digest: it refuses the
        # load rather than returning a verdict the caller would answer with a download.
        sandbox.require_unaliased_model_file(str(path), fd=fd)
        before = _inode_stamp(os.fstat(fd))
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
        if _inode_stamp(os.fstat(fd)) != before:
            raise sandbox.SandboxCeilingUnsealable(
                f"the verified model file {sandbox.safe_terminal_line(str(path))} changed "
                "while it was being verified (written, linked or unlinked through another "
                "name or descriptor), so its digest no longer describes the bytes the loader "
                "would open. Find what else writes it, and any second name for it "
                "(find -samefile, or fsutil hardlink list on Windows); the next load "
                "verifies it again."
            )
        return digest.hexdigest()
    finally:
        os.close(fd)


def _inode_stamp(info: os.stat_result) -> tuple[int, ...]:
    """What a write, link or unlink on an inode changes, for :func:`_sha256_verified_file`.

    ``st_ctime_ns`` is the field that matters on POSIX: every write, ``link``,
    ``unlink`` and ``utime`` sets it to the current time, and no caller can set it
    back, so restoring ``st_mtime_ns`` does not hide a write. ``st_nlink`` names a link
    change directly. Access time is left out: reading the file to hash it moves it.
    """
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def stream_pinned_payload(
    url: str,
    *,
    label: str,
    expected_size: int,
    expected_sha256: str,
    write: Callable[[bytes], object],
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    """Stream *url* into *write*, refusing anything but the pinned payload.

    Shared by the whisper weights and by the ffmpeg decoder executable
    (:mod:`kiro_crew.stt.decoder`), because every property below is a property of
    "fetching a sha256-pinned artifact over the network" rather than of either
    artifact: the https floor, the per-socket stall timeout, the digest computed
    while streaming, and the size enforced as a CEILING before each write. A
    second copy of this is a second place for one of them to be missing.

    *write* is a callable rather than a path, so the caller owns where the bytes
    land -- an exclusively-created staging descriptor in both cases -- and this
    function never opens or names a file.

    Raises :class:`ModelDownloadError` on a refusal; the transport's own errors
    (an unreachable host, an HTTP status, the stall timeout) propagate unchanged
    so a caller can tell a network failure from a rejected payload.
    """
    if not url.startswith("https://"):
        # The pin bounds what we accept, but plaintext would still leak which
        # artifact an operator uses and let a network attacker waste the transfer.
        raise ModelDownloadError(f"{label}: refusing a non-https URL: {url}")
    digest = hashlib.sha256()
    written = 0
    with (
        # Inside the parentheses, immediately above the call: `nosemgrep` only
        # covers its own line and the next one.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- https enforced above and the payload is sha256-pinned
        urllib.request.urlopen(url, timeout=_NETWORK_STALL_TIMEOUT_SECS) as response,
    ):
        while True:
            if should_cancel is not None and should_cancel():
                raise ModelDownloadError("cancelled")
            chunk = response.read(_CHUNK_BYTES)
            if not chunk:
                break
            # Refused BEFORE the write, because the pinned size is a ceiling on
            # what we are willing to store and not merely something to check
            # afterwards. Streaming to EOF first and comparing the total lets a
            # hostile or misconfigured mirror fill the disk: nothing about an
            # HTTPS response bounds its length, and `Content-Length` is not
            # consulted (it is the server's claim, not the pin). Failing on the
            # first excess chunk caps the damage at one `_CHUNK_BYTES` over the
            # size we already agreed to.
            if written + len(chunk) > expected_size:
                raise ModelDownloadError(
                    f"{label}: response exceeds the pinned "
                    f"{expected_size} bytes; refusing to keep writing"
                )
            write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if on_progress is not None:
                on_progress(written, expected_size)
    # Only a SHORT response can reach this now; the oversized case fails inside the
    # loop. Kept as a distinct check because a truncated transfer is the common
    # failure (a dropped connection) and deserves its own message.
    if written != expected_size:
        raise ModelDownloadError(f"{label}: expected {expected_size} bytes, received {written}")
    actual = digest.hexdigest()
    if actual != expected_sha256:
        raise ModelDownloadError(
            f"{label}: sha256 mismatch (got {actual[:16]}…, expected {expected_sha256[:16]}…)"
        )


def _download_blocking(
    model: WhisperModel,
    on_progress: ProgressFn | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """Fetch and verify *model*, returning its final path. Blocks; never on the loop.

    The digest is computed while streaming rather than by re-reading the finished
    file, so a tampered payload is rejected without ever having been written to
    the path a loader would pick up.
    """
    target = model_path(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = _model_url(model)

    # `mkstemp`, not a name this function composes. Two properties matter and neither
    # is available from `open(path, "wb")`:
    #
    # * It opens with ``O_CREAT | O_EXCL``, so it CANNOT follow a symlink. This
    #   directory is agent-writable, and a predictable staging path (a fixed
    #   ``.bin.part``, or one derived from a PID that is trivially observable) let an
    #   agent pre-plant a symlink there and have the download truncate and overwrite
    #   whatever it pointed at.
    # * The name is unpredictable and unique, which also settles the collision the
    #   fixed name caused: nothing serialises across processes -- a gateway, an MCP
    #   server and a `kirocrew` CLI each run their own store over the same data home --
    #   so two transfers interleaved their writes into one file and the sha256 pin then
    #   failed BOTH of them.
    #
    # Created in the TARGET directory so the finishing rename stays same-filesystem
    # and therefore atomic; a system-temp staging area can land on another device,
    # where the rename degrades into a copy a reader can observe half-finished.
    staging_fd, staging_name = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.", suffix=_STAGING_SUFFIX
    )
    staging = Path(staging_name)
    try:
        # The descriptor is adopted FIRST, and the order is load-bearing even
        # though opening the connection first reads more naturally. `mkstemp`
        # hands back an unowned fd and nothing else here closes one -- the
        # `finally` below unlinks the PATH -- so with the transfer in front, every
        # unreachable host, HTTP error and stall timeout leaked one descriptor per
        # attempt, because a context manager that never gets evaluated never gets
        # exited. On Windows it also stranded the partial file, since the unlink
        # cannot remove a file that still has a live handle.
        #
        # Written through the DESCRIPTOR `mkstemp` returned, never reopened by
        # name: reopening would reintroduce the very race the exclusive create
        # closed, since the path is public the moment it exists.
        with os.fdopen(staging_fd, "wb") as fh:
            stream_pinned_payload(
                url,
                label=model.name,
                expected_size=model.size_bytes,
                expected_sha256=model.sha256,
                write=fh.write,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
        # replace_with_retry, not os.replace: on Windows the rename fails with a
        # PermissionError while ANY other handle is open on either path, and a
        # just-written 148MB file is exactly what an AV scanner or the Search
        # indexer reaches for.
        replace_with_retry(staging, target)
        logger.info("Whisper model %s verified and installed at %s", model.name, target)
        return target
    finally:
        # A failed or cancelled attempt must not leave a partial file behind. On
        # the success path the rename already consumed it, so missing is normal.
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove staging file %s", staging, exc_info=True)


class ModelStore:
    """Serialises model downloads and exposes their progress.

    One store per process. Concurrent callers (a voice session that needs the
    model and a settings panel that asked for it) share one in-flight transfer
    through ``_lock`` instead of both pulling the same gigabyte.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        #: Strong references to detached decoder fetches. A task nobody references
        #: can be collected mid-await, and the set is per-store so a test with its
        #: own store cannot be handed another one's pending work.
        self._decoder_tasks: set[asyncio.Task[None]] = set()
        self.status: dict[str, object] = {
            "step": "idle",
            "model": "",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
        }

    def _start_decoder_autofetch(self) -> None:
        """Fetch the ffmpeg decoder in the background, if this install needs one.

        Started here because a completed model download is the one moment a second
        transfer reads as part of the same setup: the user has just committed to
        local speech-to-text, and a source install with no system FFmpeg cannot
        decode a browser recording or a voice memo at all. The alternative is
        discovering that at the first upload -- a failure the settings page can only
        answer with a shell command.

        Detached rather than awaited: the caller is a voice session or a settings
        poll waiting on the WEIGHTS, and live PCM never touches the decoder.
        Failures are the decoder store's to report through its own status, so this
        only has to keep the task alive and its exception retrieved.
        """
        # Imported here, not at module scope: decoder imports this module, so a
        # module-scope import back would be a cycle.
        from kiro_crew.stt import decoder as stt_decoder

        try:
            task = asyncio.ensure_future(stt_decoder.maybe_autofetch())
        except RuntimeError:
            # No running loop. `ensure` is always awaited, so this is not reachable
            # from production; it keeps a synchronous caller in a test from turning
            # a successful model download into a failure.
            logger.debug("No event loop for the decoder autofetch", exc_info=True)
            return
        self._decoder_tasks.add(task)
        task.add_done_callback(self._decoder_task_done)

    def _decoder_task_done(self, task: asyncio.Task[None]) -> None:
        """Drop the reference and retrieve the exception, so neither is a warning."""
        self._decoder_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Background ffmpeg decoder fetch failed: %s", exc)

    async def ensure(self, model: WhisperModel) -> Path | None:
        """Return the on-disk path of *model*, downloading it if absent.

        Returns ``None`` when the model is not available and cannot be fetched,
        so a caller degrades to reporting voice as unavailable rather than
        raising into a websocket handler.

        A file already on disk is verified against the pin (see
        :meth:`_verified_on_disk`) rather than trusted on its size alone. BOTH
        already-present branches go through :meth:`_accept_existing`, which is the
        point: an earlier revision gated only the pre-lock one and left the post-lock
        "a concurrent caller finished while we waited" branch returning the file
        unverified, which is the same hole through the other door.
        """
        if model.name == CUSTOM_MODEL:
            return await self._accept_custom(model)
        accepted, refused = await self._accept_existing_or_refuse(model)
        if accepted is not None or refused:
            return accepted
        if os.environ.get(SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1, not downloading whisper model %s", SKIP_DOWNLOAD_ENV, model.name)
            self._set(step="skipped", model=model.name, error="model download disabled")
            return None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # A concurrent caller may have completed it while we waited, and its file
            # gets the same check ours would have.
            accepted, refused = await self._accept_existing_or_refuse(model)
            if accepted is not None or refused:
                return accepted
            self._set(step="downloading", model=model.name, total=model.size_bytes)
            try:
                path = await asyncio.to_thread(
                    _download_blocking,
                    model,
                    lambda done, total: self._set(
                        step="downloading", model=model.name, done=done, total=total
                    ),
                )
            except Exception as exc:
                logger.warning("Whisper model %s download failed: %s", model.name, exc)
                self._set(step="failed", model=model.name, error=str(exc))
                return None
            self._set(step="ready", model=model.name, total=model.size_bytes)
            self._start_decoder_autofetch()
            return path

    async def _accept_existing_or_refuse(self, model: WhisperModel) -> tuple[Path | None, bool]:
        """:meth:`_accept_existing`, with an aliased file reported instead of raised.

        Returns ``(path, refused)``. A file reachable under a second hard link, or through
        a symlink or junction at any step from ``models``, is refused at load (see
        :meth:`_accept_existing` and :meth:`_verified_on_disk`). ``ensure`` promises ``None`` rather than
        an exception into a websocket handler, so the refusal becomes a
        ``failed`` status (flagged ``refused``) carrying the message that names the file
        and the remedy, which the settings page shows as it shows any failure, and ``ensure``
        stops there without downloading: the bytes are the pinned ones, and only the
        operator can remove the second name.
        """
        # Imported here, not at module scope: this keeps the sandbox module out of the
        # stt import graph, mirroring the lazy direction sandbox itself uses for stt.
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        try:
            return await self._accept_existing(model), False
        except SandboxCeilingUnsealable as exc:
            logger.error("Refusing to load whisper model %s: %s", model.name, exc)
            self._set(step="failed", model=model.name, error=str(exc), refused=True)
            return None, True

    async def _accept_custom(self, model: WhisperModel) -> Path | None:
        """The vetted path of the custom model file, or ``None`` with the reason in status.

        Never downloads and never deletes. The path check and the hash both run on
        every call, for the reason ``_verified_on_disk`` gives for catalog weights: a
        file that passed once proves nothing about the next load. A refusal leaves the
        file alone, because it is the user's own file and not one this store fetched.

        The catalog's two seal checks apply here too, and refuse the load as a
        ``failed`` status flagged ``refused``. ``models`` and the whisper folder must be
        real directories (``sandbox.require_real_model_path``), so the file
        :func:`custom_model_file` resolved inside them sits under the read-only seal; a
        link further down is followed and has to stay inside the folder. The hash is the
        one-descriptor check (:func:`_sha256_verified_file`), so a second hard link, a
        leaf that became a symlink, or a change during the read refuses it.
        """
        # Imported here, not at module scope: this keeps the sandbox module out of the
        # stt import graph, mirroring the lazy direction sandbox itself uses for stt.
        from kiro_crew import sandbox

        try:
            path = await asyncio.to_thread(custom_model_file, model.custom_path)
        except CustomModelPathError as exc:
            logger.warning("Custom whisper model refused: %s", exc)
            self._set(step="failed", model=model.name, error=str(exc))
            return None
        try:
            folder = models_dir()
            sandbox.require_real_model_path(str(folder), models_root=str(folder.parent))
            actual = await asyncio.to_thread(_sha256_verified_file, path)
        except sandbox.SandboxCeilingUnsealable as exc:
            logger.error("Refusing to load the custom whisper model: %s", exc)
            self._set(step="failed", model=model.name, error=str(exc), refused=True)
            return None
        except OSError as exc:
            logger.warning("Custom whisper model could not be read: %s", exc.strerror)
            self._set(step="failed", model=model.name, error="The model file could not be read.")
            return None
        if actual != model.sha256:
            logger.error(
                "Custom whisper model %s does not match stt.custom_model_sha256 "
                "(got %s…, expected %s…); not loading it.",
                path.name,
                actual[:16],
                model.sha256[:16],
            )
            self._set(
                step="failed",
                model=model.name,
                error="The model file does not match its SHA-256, so it was not loaded.",
            )
            return None
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        self._set(step="ready", model=model.name, total=size)
        return path

    async def _accept_existing(self, model: WhisperModel) -> Path | None:
        """The on-disk path if *model* is present AND matches its pin, else ``None``.

        The single gate for "there is already a usable file here". Both of `ensure`'s
        already-present branches ask this rather than `is_present` directly, so a
        digest check cannot be added to one door and forgotten at the other.

        ``None`` means "carry on to the download": either nothing is there, or what was
        there failed the pin and has been deleted.

        Raises ``SandboxCeilingUnsealable`` when a name from ``models`` down to the file
        is a symlink or junction (``sandbox.require_real_model_path``), asked before
        anything reads or writes through the path, so the link refuses this model's load
        and the download that would otherwise land through it, and nothing else.
        """
        # Imported here, not at module scope: this keeps the sandbox module out of the
        # stt import graph, mirroring the lazy direction sandbox itself uses for stt.
        from kiro_crew import sandbox

        sandbox.require_real_model_path(
            str(model_path(model)), models_root=str(models_dir().parent)
        )
        if not is_present(model):
            return None
        if not await self._verified_on_disk(model):
            logger.warning("Re-downloading whisper model %s after a failed check", model.name)
            return None
        self._set(step="ready", model=model.name, total=model.size_bytes)
        return model_path(model)

    async def _verified_on_disk(self, model: WhisperModel) -> bool:
        """Check an already-present model against its pin, once per process.

        `is_present` deliberately tests only the size: it answers "must this be
        downloaded", and it is on the path of a UI poll. But size alone is not a
        trust check. The agent's file-edit tool is refused under ``models``
        (``security.paths._WRITE_PROTECTED_HOME_PATHS``) and a sandboxed shell meets the
        read-only seal (``sandbox._CREW_READONLY_LEAVES``), but an unsandboxed process, a
        file swapped before a seal existed, or a second name in a writable tree can still
        replace the weights with a same-size file, and every later session would
        transcribe the user's speech through weights of its choosing, persistently and
        with nothing to show it had happened.

        Verified on EVERY call, with no metadata cache. A first version memoised the
        result against the file's size and mtime, which does not hold: `os.utime` is
        available to anything that can write the file, so a same-size overwrite with a
        restored mtime satisfied the memo and the next load trusted it. Anything cheap
        enough to check is also cheap enough to forge.

        Affordable because `WhisperEngine.ensure_loaded` decides residency BEFORE
        asking the store, so this runs once per model LOAD rather than once per
        session. That is also precisely when an unverified file would take effect: a
        resident model is already loaded from bytes that passed, and the next thing
        that could load different ones is the next load.

        A file reachable under a SECOND HARD LINK refuses this load instead, and does
        not reach the digest comparison at all. A digest answers "these bytes are the
        pinned ones"; it cannot answer "and they will still be these bytes when the
        native loader reopens this path by name", and an alias is exactly a writable
        handle onto the inode that just passed. Asked about the DESCRIPTOR the bytes
        were read from (``sandbox.require_unaliased_model_file``), so the inode judged
        and the inode hashed are one resolution rather than two. It is asked BEFORE the
        first byte is read, and the inode's stamp must not move across the read, so an
        alias cannot be written through and unlinked while the hash runs. It refuses
        rather than returning ``False``: ``False`` means "download over it", which would
        hand an operator a silent multi-gigabyte transfer as the report of a condition
        only they can fix, and the file is deliberately left untouched -- a hard link is
        ordinary for a dotfile manager or a deduplicating backup, so deleting one name of
        the operator's own file is not this gate's call to make.

        A file that fails the DIGEST is deleted, so the caller's download path replaces
        it rather than reporting voice as broken.

        Deliberately diverging from `embeddings`, which documents the same size-only
        trade for its own GGUF. That reasoning ("a sha256 over ~600MB on every boot
        buys almost nothing") weighed a corrupted download, not a writable directory
        and an agent with a shell. The alias question applies there too, and harder: with
        no digest at load, a second name in a writable tree is the whole exposure, so
        ``embeddings._model_file_stamp_unaliased`` asks the same
        ``sandbox.require_unaliased_model_file`` about its own ``O_NOFOLLOW`` descriptor
        and refuses that one load.
        """
        path = model_path(model)
        if not path.is_file():
            return False
        actual = await asyncio.to_thread(_sha256_verified_file, path)
        if actual == model.sha256:
            return True
        logger.error(
            "Whisper model %s at %s does not match its pinned digest "
            "(got %s…, expected %s…). Removing it: a model of the right SIZE but the "
            "wrong CONTENT would transcribe every later utterance through weights "
            "nobody verified.",
            model.name,
            path,
            actual[:16],
            model.sha256[:16],
        )
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove the unverified model at %s", path, exc_info=True)
            return False
        return False

    def _set(
        self,
        *,
        step: str,
        model: str,
        done: int = 0,
        total: int = 0,
        error: str = "",
        refused: bool = False,
    ) -> None:
        self.status = {
            "step": step,
            "model": model,
            "downloaded_bytes": done,
            "total_bytes": total,
            "error": error,
        }
        if refused:
            # Present only on a load-time alias refusal, so every other status keeps its
            # shape. The engine reads it to report the refusal rather than "not downloaded".
            self.status["refused"] = True


_store: ModelStore | None = None


def store() -> ModelStore:
    """The process-wide model store.

    A module global holding shared, caller-independent state (which model files
    exist and whether a transfer is running) rather than per-caller data, so it is safe
    in the MCP servers that serve many sessions from one process.
    """
    global _store
    if _store is None:
        _store = ModelStore()
    return _store

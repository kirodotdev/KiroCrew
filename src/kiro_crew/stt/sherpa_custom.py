"""A Sherpa-ONNX model the owner brings: an archive in ``models/sherpa/custom``.

``stt.sherpa_model = "custom"`` runs any offline Zipformer transducer k2-fsa
publishes (or any archive with the same layout), the way ``stt.model = "custom"``
runs the owner's own whisper file. Two config fields name it: the archive's file
name and its sha256. Nothing is downloaded.

On each load the archive is hashed through the one-descriptor check the catalog
uses and refused unless it matches the configured digest. The first load of a
digest unpacks the four members the recogniser needs into ``custom/<digest>/``
with a manifest of their sizes and digests; later loads check the files against
it and unpack again when one fails. Everything is under the sealed ``models/``
leaf, and ``config.json`` is read-only to the agent, so an agent can choose
neither the bytes nor the digest.

No numpy and no ``sherpa_onnx`` here: the config loader imports this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.stt import models as whisper_models
from kiro_crew.stt import sherpa_models

logger = logging.getLogger(__name__)

#: The ``stt.sherpa_model`` value that selects the owner's archive.
CUSTOM_MODEL = "custom"

#: The same value as ``sherpa_engine.CODE_BZ2_MISSING``, which imports numpy.
CODE_BZ2_MISSING = "stt_sherpa_bz2_missing"
CODE_CUSTOM_MISSING = "stt_sherpa_custom_missing"
CODE_CUSTOM_MISMATCH = "stt_sherpa_custom_mismatch"
CODE_CUSTOM_UNSUPPORTED = "stt_sherpa_custom_unsupported"

#: A plain file name ending in ``.tar.bz2``: no separator, no leading dot, so it
#: can only name a file directly inside :func:`custom_dir`.
_ARCHIVE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.bz2")
_ARCHIVE_MAX = 200
_MANIFEST = "manifest.json"
_CHUNK_BYTES = 1 << 20
_STAGING_SUFFIX = ".part"
_ROLES = ("encoder", "decoder", "joiner")


class CustomModelError(Exception):
    """The custom model cannot be used. ``code`` and the text are safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def custom_dir() -> Path:
    """Where the owner puts the archive: ``<data home>/models/sherpa/custom``."""
    return sherpa_models.models_dir() / "custom"


def valid_archive_name(value: object) -> str:
    """*value* as a usable archive file name, or ``""`` when it is not one."""
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if len(name) > _ARCHIVE_MAX or not _ARCHIVE_RE.fullmatch(name) or ".." in name:
        return ""
    return name


def valid_sha256(value: object) -> str:
    """The configured digest, normalised the way the whisper custom digest is."""
    return whisper_models.valid_custom_sha256(value)


def _role_of(basename: str) -> str | None:
    if basename == "tokens.txt":
        return "tokens"
    if not basename.endswith(".onnx"):
        return None
    for role in _ROLES:
        if basename.startswith(role):
            return role
    return None


def pick_members(names: list[str]) -> dict[str, str]:
    """The archive members the recogniser loads, by role, or raise if any is missing.

    One ``tokens.txt`` and one each of ``encoder*.onnx``, ``decoder*.onnx`` and
    ``joiner*.onnx`` by base name. When both precisions are present the int8
    encoder and joiner and the fp32 decoder are taken, the choice k2-fsa
    recommends.
    """
    by_role: dict[str, list[str]] = {"encoder": [], "decoder": [], "joiner": [], "tokens": []}
    for name in names:
        role = _role_of(name.rsplit("/", 1)[-1])
        if role is not None:
            by_role[role].append(name)
    if len(by_role["tokens"]) != 1:
        raise CustomModelError(
            CODE_CUSTOM_UNSUPPORTED,
            "the archive must hold exactly one tokens.txt",
        )
    picked = {"tokens": by_role["tokens"][0]}
    for role in _ROLES:
        found = sorted(by_role[role])
        if not found:
            raise CustomModelError(
                CODE_CUSTOM_UNSUPPORTED,
                f"the archive has no {role}*.onnx, so it is not an offline transducer "
                "(CTC, Whisper and Paraformer models are not supported)",
            )
        int8 = [n for n in found if ".int8." in n]
        full = [n for n in found if ".int8." not in n]
        preferred = full if role == "decoder" else int8
        picked[role] = (preferred or found)[0]
    return picked


@dataclass(frozen=True)
class _Unpacked:
    role: str
    filename: str
    size_bytes: int
    sha256: str


def _install_dir(digest: str) -> Path:
    return custom_dir() / digest


def _as_model(digest: str, files: dict[str, _Unpacked]) -> sherpa_models.SherpaModel:
    """The installed custom model in the catalog's shape, so the engine loads it alike."""

    def entry(role: str) -> sherpa_models.SherpaFile:
        f = files[role]
        return sherpa_models.SherpaFile(f.filename, f.size_bytes, f.sha256)

    return sherpa_models.SherpaModel(
        name=CUSTOM_MODEL,
        languages=(),
        archive="",
        archive_size_bytes=0,
        archive_sha256=digest,
        encoder=entry("encoder"),
        decoder=entry("decoder"),
        joiner=entry("joiner"),
        tokens=entry("tokens"),
        license="",
        source_url="",
        directory=f"custom/{digest}",
    )


def _read_manifest(digest: str) -> dict[str, _Unpacked] | None:
    try:
        raw = json.loads((_install_dir(digest) / _MANIFEST).read_text(encoding="utf-8"))
        return {
            role: _Unpacked(role, str(v["file"]), int(v["size"]), str(v["sha256"]))
            for role, v in raw["files"].items()
            if role in ("encoder", "decoder", "joiner", "tokens")
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _manifest_holds(digest: str, files: dict[str, _Unpacked]) -> bool:
    """Whether every manifest file is on disk with its recorded digest."""
    if len(files) != 4:
        return False
    model = _as_model(digest, files)
    try:
        return sherpa_models._verify_blocking(model)
    except OSError:
        return False


def _unpack(archive: Path, digest: str) -> dict[str, _Unpacked]:
    """Unpack the four members from *archive* into ``custom/<digest>/``. Blocking."""
    if not sherpa_models.bz2_available():
        raise CustomModelError(CODE_BZ2_MISSING, sherpa_models.BZ2_MISSING_ERROR)
    target = _install_dir(digest)
    target.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    unpacked: dict[str, _Unpacked] = {}
    try:
        try:
            tar = tarfile.open(archive, mode="r:bz2")
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise CustomModelError(
                CODE_CUSTOM_UNSUPPORTED, f"the file is not a readable .tar.bz2 archive ({exc})"
            ) from None
        with tar:
            members = {m.name: m for m in tar.getmembers() if m.isreg()}
            picked = pick_members(list(members))
            # In archive order: a .tar.bz2 cannot seek back without decompressing
            # from the start again, so each earlier member would cost a whole pass.
            ordered = sorted(picked.items(), key=lambda item: members[item[1]].offset_data)
            for role, member in ordered:
                filename = "tokens.txt" if role == "tokens" else f"{role}.onnx"
                source = tar.extractfile(members[member])
                if source is None:
                    raise CustomModelError(CODE_CUSTOM_UNSUPPORTED, f"{member}: unreadable")
                fd, name = tempfile.mkstemp(
                    dir=target, prefix=f"{filename}.", suffix=_STAGING_SUFFIX
                )
                staged_path = Path(name)
                staged.append((staged_path, target / filename))
                digest_h = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as out, source:
                    while chunk := source.read(_CHUNK_BYTES):
                        out.write(chunk)
                        digest_h.update(chunk)
                        size += len(chunk)
                unpacked[role] = _Unpacked(role, filename, size, digest_h.hexdigest())
        for source_path, final in staged:
            replace_with_retry(source_path, final)
        staged.clear()
        manifest = {
            "archive_sha256": digest,
            "files": {
                r: {"file": u.filename, "size": u.size_bytes, "sha256": u.sha256}
                for r, u in unpacked.items()
            },
        }
        (target / _MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        logger.info("Custom Sherpa-ONNX model unpacked into %s", target)
        return unpacked
    finally:
        for source_path, _final in staged:
            try:
                source_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove staging file %s", source_path, exc_info=True)


def prepare_blocking(archive_name: object, sha256: object) -> sherpa_models.SherpaModel:
    """The verified, unpacked custom model, or raise :class:`CustomModelError`. Blocking.

    ``SandboxCeilingUnsealable`` from the sandbox checks propagates: a link on the
    path refuses the load and is the operator's to remove.
    """
    from kiro_crew import sandbox

    name = valid_archive_name(archive_name)
    digest = valid_sha256(sha256)
    if not name or not digest:
        raise CustomModelError(
            CODE_CUSTOM_MISSING,
            "set stt.sherpa_custom_archive to the archive's file name and "
            "stt.sherpa_custom_sha256 to its sha256",
        )
    archive = custom_dir() / name
    sandbox.require_real_model_path(str(archive), models_root=str(config_dir() / "models"))
    if not archive.is_file():
        raise CustomModelError(
            CODE_CUSTOM_MISSING, f"{name} is not in the custom model folder ({custom_dir()})"
        )
    if whisper_models._sha256_verified_file(archive) != digest:
        raise CustomModelError(
            CODE_CUSTOM_MISMATCH,
            f"{name} does not match stt.sherpa_custom_sha256, so it was not loaded",
        )
    files = _read_manifest(digest)
    if files is None or not _manifest_holds(digest, files):
        files = _unpack(archive, digest)
    return _as_model(digest, files)


#: The outcome of the last :func:`prepare_reporting`, for the settings page. Caller-
#: independent (which archive was checked, and how it went), so a module global is safe.
status: dict[str, str] = {"step": "idle", "code": "", "error": ""}


def prepare_reporting(archive_name: object, sha256: object) -> None:
    """:func:`prepare_blocking` for the Settings button: the outcome goes to :data:`status`."""
    from kiro_crew.sandbox import SandboxCeilingUnsealable

    global status
    status = {"step": "unpacking", "code": "", "error": ""}
    try:
        prepare_blocking(archive_name, sha256)
    except CustomModelError as exc:
        status = {"step": "failed", "code": exc.code, "error": str(exc)}
    except (SandboxCeilingUnsealable, OSError) as exc:
        status = {"step": "failed", "code": CODE_CUSTOM_MISMATCH, "error": str(exc)}
    else:
        status = {"step": "ready", "code": "", "error": ""}


def is_unpacked(sha256: object) -> bool:
    """Whether an unpacked install for *sha256* is on disk. A read, not a hash."""
    digest = valid_sha256(sha256)
    if not digest:
        return False
    files = _read_manifest(digest)
    if files is None or len(files) != 4:
        return False
    return all(
        (_install_dir(digest) / f.filename).is_file()
        and (_install_dir(digest) / f.filename).stat().st_size == f.size_bytes
        for f in files.values()
    )


def archive_present(archive_name: object) -> bool:
    name = valid_archive_name(archive_name)
    return bool(name) and (custom_dir() / name).is_file()

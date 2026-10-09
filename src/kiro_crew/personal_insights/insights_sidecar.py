from __future__ import annotations

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SIDECAR_SCHEMA: Final[str] = "kiro.personal-insights.sidecar/1.0"
BINDING_RECOVERY_REEXPORT: Final[str] = "binding_recovery_reexport"
BINDING_BOUND: Final[str] = "binding_bound"
_KEY_RAW: Final[str] = "raw"
_KEY_NEUTRAL: Final[str] = "neutral"
_HEX_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_NEUTRAL_RE: Final[re.Pattern[str]] = re.compile(r"ne-[0-9a-f]{64}\Z")
_OPAQUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class SidecarError(Exception):
    pass


class SidecarTransactionInterrupted(SidecarError):
    pass


class EvidenceDisposed(SidecarError):
    pass


class RedactionDriftError(SidecarError):
    pass


@dataclass(frozen=True)
class BindingValidation:
    schema_version: str
    source_content_digest: str
    projection_generation_id: str
    descriptor_sha256: str
    redaction_contract_version: str = "redaction/1.0"

    def __post_init__(self) -> None:
        if self.schema_version != SIDECAR_SCHEMA:
            raise ValueError("sidecar schema version is unsupported")
        if _HEX_RE.fullmatch(self.source_content_digest) is None:
            raise ValueError("source content digest is invalid")
        if not _opaque(self.projection_generation_id):
            raise ValueError("projection generation id is invalid")
        if _HEX_RE.fullmatch(self.descriptor_sha256) is None:
            raise ValueError("descriptor digest is invalid")
        if not _opaque(self.redaction_contract_version):
            raise ValueError("redaction contract version is invalid")


@dataclass(frozen=True)
class BindingMetadata:
    session_key: str
    snapshot_digest: str
    group_id: str | None = None
    retained_until: str | None = None

    def __post_init__(self) -> None:
        if not _opaque(self.session_key):
            raise ValueError("session key is invalid")
        if _HEX_RE.fullmatch(self.snapshot_digest) is None:
            raise ValueError("snapshot digest is invalid")
        if self.group_id is not None and not _opaque(self.group_id):
            raise ValueError("group id is invalid")
        if self.retained_until is not None and not self.retained_until:
            raise ValueError("retention boundary is invalid")


@dataclass(frozen=True)
class EvidenceLocator:
    source_id: str
    session_key: str
    source_content_digest: str
    projection_generation_id: str
    group_id: str | None
    neutral_event_id: str
    descriptor_sha256: str
    redaction_contract_version: str
    retained_until: str | None


def _opaque(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_RE.fullmatch(value) is not None


def _validation_tuple(validation: BindingValidation) -> tuple[str, str, str, str, str]:
    return (
        validation.schema_version,
        validation.source_content_digest,
        validation.projection_generation_id,
        validation.descriptor_sha256,
        validation.redaction_contract_version,
    )


class SidecarStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._secure_parent()
        if self.path.exists() and self.path.is_symlink():
            raise SidecarError("sidecar path is a symlink")
        self._initialize()
        self.path.chmod(0o600)

    def _secure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        status = self.path.parent.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise SidecarError("sidecar parent is unsafe")
        if hasattr(os, "getuid") and status.st_uid != os.getuid():
            raise SidecarError("sidecar parent is not owner-held")
        if status.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SidecarError("sidecar parent permissions are unsafe")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bindings ("
                "source_id TEXT NOT NULL, "
                "key_kind TEXT NOT NULL, "
                "event_id TEXT NOT NULL, "
                "text TEXT, "
                "schema_version TEXT NOT NULL, "
                "source_content_digest TEXT NOT NULL, "
                "projection_generation_id TEXT NOT NULL, "
                "descriptor_sha256 TEXT NOT NULL, "
                "redaction_contract_version TEXT NOT NULL, "
                "session_key TEXT NOT NULL, "
                "snapshot_digest TEXT NOT NULL, "
                "group_id TEXT, "
                "retained_until TEXT, "
                "PRIMARY KEY (source_id, key_kind, event_id))"
            )

    def stage_raw_text(
        self,
        source_id: str,
        raw_text: dict[str, str],
        validation: BindingValidation,
        metadata: BindingMetadata | None = None,
    ) -> None:
        if not _opaque(source_id):
            raise SidecarError("source id is invalid")
        binding_metadata = metadata or BindingMetadata(
            session_key="unbound",
            snapshot_digest=validation.source_content_digest,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for raw_id, text in raw_text.items():
                    if not _opaque(raw_id) or not isinstance(text, str):
                        raise SidecarError("raw sidecar entry is invalid")
                    existing = self._row(connection, source_id, _KEY_RAW, raw_id)
                    expected = self._row_values(
                        source_id,
                        _KEY_RAW,
                        raw_id,
                        text,
                        validation,
                        binding_metadata,
                    )
                    if existing is not None:
                        if tuple(existing) != expected:
                            raise SidecarError("raw sidecar binding conflicts with retained state")
                        continue
                    connection.execute(
                        "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        expected,
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _row_values(
        self,
        source_id: str,
        key_kind: str,
        event_id: str,
        text: str | None,
        validation: BindingValidation,
        metadata: BindingMetadata,
    ) -> tuple[object, ...]:
        return (
            source_id,
            key_kind,
            event_id,
            text,
            *_validation_tuple(validation),
            metadata.session_key,
            metadata.snapshot_digest,
            metadata.group_id,
            metadata.retained_until,
        )

    @staticmethod
    def _row(
        connection: sqlite3.Connection,
        source_id: str,
        key_kind: str,
        event_id: str,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            "SELECT source_id,key_kind,event_id,text,schema_version,"
            "source_content_digest,projection_generation_id,descriptor_sha256,"
            "redaction_contract_version,session_key,snapshot_digest,group_id,retained_until "
            "FROM bindings WHERE source_id=? AND key_kind=? AND event_id=?",
            (source_id, key_kind, event_id),
        ).fetchone()
        return None if row is None else tuple(row)

    @staticmethod
    def _row_matches(row: tuple[object, ...], validation: BindingValidation) -> bool:
        return tuple(str(value) for value in row[4:9]) == _validation_tuple(validation)

    def rekey(
        self,
        source_id: str,
        raw_to_neutral: dict[str, str],
        validation: BindingValidation,
    ) -> None:
        self._validate_map(source_id, raw_to_neutral)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._rekey_locked(connection, source_id, raw_to_neutral, validation)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _validate_map(self, source_id: str, raw_to_neutral: dict[str, str]) -> None:
        if not _opaque(source_id) or not isinstance(raw_to_neutral, dict):
            raise SidecarError("raw-to-neutral map is invalid")
        if len(set(raw_to_neutral.values())) != len(raw_to_neutral):
            raise SidecarError("raw-to-neutral map has duplicate neutral ids")
        for raw_id, neutral_id in raw_to_neutral.items():
            if not _opaque(raw_id) or _NEUTRAL_RE.fullmatch(neutral_id) is None:
                raise SidecarError("raw-to-neutral map has invalid ids")

    def _rekey_locked(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        raw_to_neutral: dict[str, str],
        validation: BindingValidation,
    ) -> None:
        for raw_id, neutral_id in raw_to_neutral.items():
            raw = self._row(connection, source_id, _KEY_RAW, raw_id)
            neutral = self._row(connection, source_id, _KEY_NEUTRAL, neutral_id)
            if neutral is not None:
                if not self._row_matches(neutral, validation):
                    raise SidecarError("neutral binding conflicts with current validation")
                if raw is not None:
                    connection.execute(
                        "DELETE FROM bindings WHERE source_id=? AND key_kind=? AND event_id=?",
                        (source_id, _KEY_RAW, raw_id),
                    )
                continue
            if raw is None or not self._row_matches(raw, validation):
                raise SidecarError("raw binding is absent or stale")
            inserted = list(raw)
            inserted[1] = _KEY_NEUTRAL
            inserted[2] = neutral_id
            connection.execute(
                "INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(inserted),
            )
            connection.execute(
                "DELETE FROM bindings WHERE source_id=? AND key_kind=? AND event_id=?",
                (source_id, _KEY_RAW, raw_id),
            )

    def recover(
        self,
        source_id: str,
        raw_to_neutral: dict[str, str],
        require: BindingValidation,
    ) -> str:
        self._validate_map(source_id, raw_to_neutral)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for raw_id, neutral_id in raw_to_neutral.items():
                    raw = self._row(connection, source_id, _KEY_RAW, raw_id)
                    neutral = self._row(connection, source_id, _KEY_NEUTRAL, neutral_id)
                    if neutral is not None and not self._row_matches(neutral, require):
                        connection.execute("ROLLBACK")
                        return BINDING_RECOVERY_REEXPORT
                    if neutral is None and (raw is None or not self._row_matches(raw, require)):
                        connection.execute("ROLLBACK")
                        return BINDING_RECOVERY_REEXPORT
                self._rekey_locked(connection, source_id, raw_to_neutral, require)
                connection.execute("COMMIT")
                return BINDING_BOUND
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def reconcile(
        self,
        source_id: str,
        raw_to_neutral: dict[str, str],
        validation: BindingValidation,
    ) -> str:
        return self.recover(source_id, raw_to_neutral, validation)

    def raw_text(
        self,
        source_id: str,
        raw_id: str,
        require: BindingValidation | None = None,
    ) -> str | None:
        with self._connect() as connection:
            row = self._row(connection, source_id, _KEY_RAW, raw_id)
        if row is None or (require is not None and not self._row_matches(row, require)):
            return None
        return None if row[3] is None else str(row[3])

    def neutral_text(
        self,
        source_id: str,
        neutral_id: str,
        require: BindingValidation | None = None,
    ) -> str | None:
        with self._connect() as connection:
            row = self._row(connection, source_id, _KEY_NEUTRAL, neutral_id)
        if row is None or (require is not None and not self._row_matches(row, require)):
            return None
        return None if row[3] is None else str(row[3])

    def locator(self, source_id: str, neutral_id: str) -> EvidenceLocator | None:
        with self._connect() as connection:
            row = self._row(connection, source_id, _KEY_NEUTRAL, neutral_id)
        if row is None:
            return None
        return EvidenceLocator(
            source_id=str(row[0]),
            session_key=str(row[9]),
            source_content_digest=str(row[5]),
            projection_generation_id=str(row[6]),
            group_id=None if row[11] is None else str(row[11]),
            neutral_event_id=str(row[2]),
            descriptor_sha256=str(row[7]),
            redaction_contract_version=str(row[8]),
            retained_until=None if row[12] is None else str(row[12]),
        )

    def retrieve_for_view(
        self,
        source_id: str,
        neutral_id: str,
        require: BindingValidation,
    ) -> str:
        locator = self.locator(source_id, neutral_id)
        if locator is None:
            raise EvidenceDisposed("evidence binding is unavailable")
        if locator.redaction_contract_version != require.redaction_contract_version:
            raise RedactionDriftError("redaction policy changed since binding")
        if (
            locator.source_content_digest != require.source_content_digest
            or locator.projection_generation_id != require.projection_generation_id
            or locator.descriptor_sha256 != require.descriptor_sha256
        ):
            raise EvidenceDisposed("evidence binding validation failed")
        text = self.neutral_text(source_id, neutral_id, require)
        if text is None:
            raise EvidenceDisposed("viewer text was disposed")
        return text

    def dispose_viewer_text(self, source_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE bindings SET text=NULL WHERE source_id=? AND key_kind=?",
                    (source_id, _KEY_NEUTRAL),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def dispose(self, source_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM bindings WHERE source_id=?", (source_id,))

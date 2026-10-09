from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Final

CANONICAL_VERSION: Final[str] = "1.0"

LESSON_FIELDS: Final[tuple[str, ...]] = ("rule", "category", "negative", "repo_scope", "applies")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_lesson_bytes(fields: dict[str, Any]) -> bytes:
    reduced = {key: fields.get(key) for key in LESSON_FIELDS}
    return json.dumps(reduced, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )


def canonical_lesson_digest(fields: dict[str, Any]) -> str:
    return sha256_hex(canonical_lesson_bytes(fields))


def canonical_text_bytes(text: str) -> bytes:
    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized = normalized + "\n"
    return normalized.encode("utf-8")


def canonical_text_digest(text: str) -> str:
    return sha256_hex(canonical_text_bytes(text))


def canonical_steering_bytes(compiled_post_state: str) -> bytes:
    normalized = unicodedata.normalize("NFC", compiled_post_state)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.encode("utf-8")


def canonical_steering_digest(compiled_post_state: str) -> str:
    return sha256_hex(canonical_steering_bytes(compiled_post_state))


def canonical_argv_bytes(argv: tuple[str, ...]) -> bytes:
    return json.dumps(list(argv), ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def canonical_argv_digest(argv: tuple[str, ...]) -> str:
    return sha256_hex(canonical_argv_bytes(argv))


def source_content_digest(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return sha256_hex(normalized.encode("utf-8"))


def bound_source_digest(kind: str, scope: str, content: str) -> str:
    normalized = unicodedata.normalize("NFC", content).replace("\r\n", "\n").replace("\r", "\n")
    payload = json.dumps(
        {"kind": kind, "scope": scope, "content": normalized},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(payload)

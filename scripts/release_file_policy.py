"""Shared release-file safety policy for source, App ZIP and release gate.

A filename check is a *packaging* boundary, not a replacement for secret scanning
inside files. The release gate and the builder intentionally use the same rule.
"""
from __future__ import annotations

from pathlib import Path

SENSITIVE_NAMES = frozenset({
    ".env", ".envrc", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "credentials", "credentials.json", "secrets.json", "token.json",
    "private.key", "service-account.json", "service_account.json",
})
SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})
BACKUP_SUFFIXES = frozenset({".bak", ".backup", ".orig", ".rej"})
BACKUP_FRAGMENTS = (".bak-", ".backup-", ".orig-", ".rej-")


def unsafe_release_path(rel: Path) -> str | None:
    """Explain why a path is forbidden; operate on *relative* tree paths."""
    if rel.is_absolute() or ".." in rel.parts:
        return "path escapes the release tree"
    parts = tuple(part.lower() for part in rel.parts)
    name = parts[-1] if parts else ""
    if any(part in {".ssh", ".aws", ".gnupg"} for part in parts[:-1]):
        return "private credential directory"
    if name in SENSITIVE_NAMES or name.startswith(".env."):
        return "credential or environment file"
    if Path(name).suffix in SENSITIVE_SUFFIXES:
        return "private key or certificate"
    if name.endswith("~") or Path(name).suffix in BACKUP_SUFFIXES or any(fragment in name for fragment in BACKUP_FRAGMENTS):
        return "editor backup or patch residue"
    # data/ is always mutable runtime state, including future subdirectories.
    if parts and parts[0] == "data":
        return "mutable runtime data"
    return None

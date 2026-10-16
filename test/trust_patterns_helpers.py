"""Title-shaped fixture adapters shared by the trusted-pattern test modules."""

from __future__ import annotations

from kiro_crew.trust_patterns import extract_base_command as _canonical_extract_base_command
from kiro_crew.trust_patterns import extract_full_command as _canonical_extract_full_command
from kiro_crew.trust_patterns import matches_trusted_pattern as _canonical_matches_trusted_pattern


def _legacy_title_command(value: str) -> str:
    """Adapt old title-shaped fixtures into canonical command input.

    Production helpers do not strip display prefixes.  Many segmentation
    tests use title-shaped fixtures rather than structured ``tool_input``;
    explicit boundary regressions exercise the raw helpers directly.
    """
    for prefix in ("Running: ", "Reading "):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _extract_base_command(value: str) -> str:
    return _canonical_extract_base_command(_legacy_title_command(value))


def _extract_full_command(value: str) -> str:
    return _canonical_extract_full_command(_legacy_title_command(value))


def _matches_trusted_pattern(value: str, patterns: set[str]) -> str | None:
    return _canonical_matches_trusted_pattern(_legacy_title_command(value), patterns)

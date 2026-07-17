"""Agent metadata store — plain .md files per agent in ~/.kirocrew/agent-metadata/."""

from __future__ import annotations

from pathlib import Path

from kiro_crew.config.paths import config_dir

METADATA_DIR_NAME = "agent-metadata"

_SAFE_NAME_RE = __import__("re").compile(r"^[a-zA-Z0-9_-]+$")


def _validate_name(name: str) -> str:
    if not name or not _SAFE_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid agent name: {name!r}")
    return name


def metadata_dir() -> Path:
    """Return the agent metadata directory, creating it if needed."""
    d = config_dir() / METADATA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(name: str) -> str:
    """Load metadata for *name*. Returns empty string if not found."""
    p = metadata_dir() / f"{_validate_name(name)}.md"
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def save(name: str, content: str) -> Path:
    """Save metadata for *name*. Returns the written path."""
    p = metadata_dir() / f"{_validate_name(name)}.md"
    p.write_text(content, encoding="utf-8")
    return p


def delete(name: str) -> bool:
    """Delete metadata for *name*. Returns True if file existed."""
    p = metadata_dir() / f"{_validate_name(name)}.md"
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def load_all() -> dict[str, str]:
    """Load all metadata files. Returns {agent_name: content}."""
    d = metadata_dir()
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))}

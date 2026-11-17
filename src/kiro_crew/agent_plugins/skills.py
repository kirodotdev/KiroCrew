"""Discover a plugin's skills (Agent Plugins 1.0.0, §7.1).

Each IMMEDIATE child of ``skills/`` that holds a regular-file ``SKILL.md`` is one
skill; deeper folders are never searched. A skill whose ``SKILL.md`` fails the
Agent Skills rules is skipped and reported, and the others still load.

The Agent Skills rules applied: YAML frontmatter holding a ``name`` of 1-64
lowercase letters, digits and single hyphens (no hyphen at either end) that
matches the folder name, and a non-empty ``description`` of at most 1024
characters.
"""

from __future__ import annotations

import os
import string

import yaml  # type: ignore[import-untyped]

from kiro_crew.agent_plugins.model import Issue, Skill
from kiro_crew.agent_plugins.paths import resolves_within
from kiro_crew.yaml_safe import NoAliasSafeLoader

SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
#: A SKILL.md larger than this is skipped unread.
MAX_SKILL_BYTES = 1024 * 1024
MAX_DESCRIPTION_CHARS = 1024

_SKILL_NAME_ENDS = frozenset(string.ascii_lowercase + string.digits)
_SKILL_NAME_CHARS = _SKILL_NAME_ENDS | frozenset("-")
_MAX_SKILL_NAME_CHARS = 64
_FENCE = "---"


class _SkillError(Exception):
    """One skill is invalid; the message says why."""


class _SkillReadError(_SkillError):
    """One skill could not be read on this host; no rule was broken."""


def _load_yaml(text: str) -> object:
    """Parse one YAML document by driving the alias-refusing SafeLoader."""
    loader = NoAliasSafeLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _is_fence(line: str) -> bool:
    """True for a ``---`` line; trailing spaces and tabs are allowed."""
    return line.startswith(_FENCE) and not line[len(_FENCE) :].strip(" \t")


def _frontmatter_block(text: str) -> str | None:
    """The YAML between a leading ``---`` line and the next ``---`` line, or ``None``.

    A leading byte-order mark is ignored and a line ends at LF or CRLF. The
    opening fence is the first line; the closing fence is a later line, with
    at least one line between them (which may be empty), and it ends the text
    or a line. A text with no closing fence has no frontmatter.
    """
    lines = text.removeprefix("\ufeff").split("\n")
    if not _is_fence(lines[0].removesuffix("\r")):
        return None
    last = len(lines) - 1
    for index in range(2, len(lines)):
        line = lines[index]
        # Only a fence that ends a line may drop its CR; at the end of the text
        # the line is matched as it stands.
        if _is_fence(line if index == last else line.removesuffix("\r")):
            return "\n".join(lines[1:index]).removesuffix("\r")
    return None


def frontmatter_name(text: str) -> str | None:
    """The ``name`` a ``SKILL.md`` declares, or ``None`` when it declares none."""
    block = _frontmatter_block(text)
    if block is None:
        return None
    try:
        meta = _load_yaml(block)
    except (yaml.YAMLError, ValueError, RecursionError):
        return None
    name = meta.get("name") if isinstance(meta, dict) else None
    return name if isinstance(name, str) else None


def is_valid_skill_name(name: object) -> bool:
    """Agent Skills name rule."""
    if not isinstance(name, str) or not 0 < len(name) <= _MAX_SKILL_NAME_CHARS:
        return False
    if name[0] not in _SKILL_NAME_ENDS or name[-1] not in _SKILL_NAME_ENDS:
        return False
    return all(char in _SKILL_NAME_CHARS for char in name) and "--" not in name


def discover_skills(root_real: str) -> tuple[tuple[Skill, ...], list[Issue]]:
    """Skills under the resolved plugin root, plus the issues met on the way."""
    path = os.path.join(root_real, SKILLS_DIR)
    if not os.path.lexists(path):
        return (), []
    resolved = resolves_within(path, root_real)
    if resolved is None:
        return (), [Issue("skills", "skills/ points outside the plugin folder; skills are off")]
    if not os.path.isdir(resolved):
        return (), [Issue("skills", "skills/ is not a folder; skills are off")]
    try:
        children = sorted(os.listdir(resolved))
    except OSError:
        return (), [Issue("skills", "skills/ could not be listed; skills are off", failed=True)]

    skills: list[Skill] = []
    issues: list[Issue] = []
    for child in children:
        skill_file = os.path.join(resolved, child, SKILL_FILE)
        # Not os.path.lexists: it answers False for a folder it cannot read, which
        # would drop that skill without a word.
        try:
            os.lstat(skill_file)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            message = "skill skipped: its folder could not be read"
            issues.append(Issue("skills", message, entry=child, failed=True))
            continue
        try:
            skills.append(_load_skill(child, skill_file, root_real))
        except _SkillError as exc:
            failed = isinstance(exc, _SkillReadError)
            issues.append(Issue("skills", f"skill skipped: {exc}", entry=child, failed=failed))
    return tuple(skills), issues


def _load_skill(folder: str, skill_file: str, root_real: str) -> Skill:
    folder_real = resolves_within(os.path.dirname(skill_file), root_real)
    resolved = resolves_within(skill_file, root_real)
    if folder_real is None or resolved is None:
        raise _SkillError("SKILL.md points outside the plugin folder")
    if not os.path.isfile(resolved):
        raise _SkillError("SKILL.md is not a regular file")
    try:
        if os.path.getsize(resolved) > MAX_SKILL_BYTES:
            raise _SkillError("SKILL.md is too large")
        with open(resolved, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise _SkillReadError("SKILL.md could not be read") from exc
    except UnicodeDecodeError as exc:
        raise _SkillError("SKILL.md is not UTF-8 text") from exc

    block = _frontmatter_block(text)
    if block is None:
        raise _SkillError("SKILL.md has no frontmatter")
    try:
        meta = _load_yaml(block)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        # The constructors raise a bare ValueError for a malformed scalar such as
        # an impossible timestamp, and deep nesting exhausts the parser's stack.
        raise _SkillError("SKILL.md frontmatter is not valid YAML") from exc
    if not isinstance(meta, dict):
        raise _SkillError("SKILL.md frontmatter is not a mapping")

    name = meta.get("name")
    if not is_valid_skill_name(name):
        raise _SkillError("name must be 1-64 lowercase letters, digits or single hyphens")
    if name != folder:
        raise _SkillError(f"name {name!r} does not match its folder")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise _SkillError("description is missing")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise _SkillError("description is longer than 1024 characters")

    return Skill(name=name, description=description, path=folder_real)

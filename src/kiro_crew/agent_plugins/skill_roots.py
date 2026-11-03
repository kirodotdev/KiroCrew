"""The skills of every ``on`` plugin, as roots the skill catalog walks.

Each ``on`` plugin contributes its own resolved ``skills/`` directory and the
skills discovery validated there (§7.1: immediate children holding ``SKILL.md``),
each named ``plugin-<name>/<skill>`` so two plugins with a skill of the same name
never collide. An ``off``, ``invalid`` or ``not-a-plugin`` plugin contributes
nothing, and ``~/.kiro/powers/installed/`` is never a root as a whole.

The answer is re-derived from discovery, as the plugin MCP source is. Discovery
reads every installed plugin's manifest and skills, and the skill code asks for
the trusted roots on every unvetted read, so one answer is kept while the
owner's record and ``installed.json`` are unchanged, for at most
:data:`_CACHE_TTL_SECS`. Turning a plugin off or uninstalling it rewrites one of
those files, so it is seen on the next call. A skill added to or removed from an
``on`` plugin is seen within the TTL; an edit to a skill's text is served at once,
because the catalog reads ``SKILL.md`` itself.

Never raises: a failure contributes nothing, and a failure on one plugin skips
only that plugin.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from kiro_crew.agent_plugins.paths import INSTALLED_INDEX, powers_home, resolves_within
from kiro_crew.agent_plugins.skills import SKILL_FILE, SKILLS_DIR

logger = logging.getLogger(__name__)

#: The catalog-key namespace of a plugin's skills: ``plugin-<name>/<skill>``.
PLUGIN_SKILL_PREFIX = "plugin-"
#: Longest an answer is served while the record and the index are unchanged. The
#: skill catalog revalidates its own walk on the same period.
_CACHE_TTL_SECS = 60.0


@dataclass(frozen=True)
class PluginSkillRoot:
    """One ``on`` plugin's skills."""

    plugin: str
    #: The plugin's resolved ``skills/`` directory: the one path trusted for it.
    directory: str
    #: ``(catalog key, SKILL.md path)`` for each skill inside :attr:`directory`.
    skills: tuple[tuple[str, str], ...]


def plugin_skill_key(plugin: str, skill: str) -> str:
    """The catalog key of *skill* of *plugin*."""
    return f"{PLUGIN_SKILL_PREFIX}{plugin}/{skill}"


_lock = threading.Lock()
_cache: tuple[tuple, float, tuple[PluginSkillRoot, ...]] | None = None


def _stamp(path: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _cache_key() -> tuple:
    from kiro_crew.agent_plugins.consent import consent_path

    home = str(powers_home())
    record = str(consent_path())
    return (home, record, _stamp(record), _stamp(os.path.join(home, INSTALLED_INDEX)))


def on_plugin_skill_roots() -> tuple[PluginSkillRoot, ...]:
    """The skill roots of every ``on`` plugin, sorted by plugin name."""
    global _cache
    try:
        key = _cache_key()
    except Exception:  # noqa: BLE001 — an unresolvable home contributes no root
        logger.warning("agent plugins: skill roots unavailable", exc_info=True)
        return ()
    now = time.monotonic()
    with _lock:
        cached = _cache
    if cached is not None and cached[0] == key and now < cached[1]:
        return cached[2]
    roots = _derive()
    with _lock:
        _cache = (key, now + _CACHE_TTL_SECS, roots)
    return roots


def _derive() -> tuple[PluginSkillRoot, ...]:
    try:
        from kiro_crew.agent_plugins.discovery import discover_plugins

        records = discover_plugins()
    except Exception:  # noqa: BLE001 — a broken plugin tree must not stop the catalog
        logger.warning("agent plugins: discovery failed; no plugin skills", exc_info=True)
        return ()
    roots: list[PluginSkillRoot] = []
    for rec in records:
        if rec.state != "on" or not rec.skills:
            continue
        try:
            root = _root_of(rec.name, rec.root, rec.skills)
        except Exception:  # noqa: BLE001 — one bad plugin must not hide the rest
            logger.warning("agent plugins: skipping the skills of plugin %s", rec.name)
            continue
        if root is not None:
            roots.append(root)
    return tuple(roots)


def _root_of(name: str, root: str, skills: tuple) -> PluginSkillRoot | None:
    root_real = os.path.realpath(root)
    directory = resolves_within(os.path.join(root_real, SKILLS_DIR), root_real)
    if directory is None or not os.path.isdir(directory):
        return None
    inside = os.path.normcase(directory)
    entries = tuple(
        (plugin_skill_key(name, skill.name), os.path.join(skill.path, SKILL_FILE))
        for skill in skills
        # A skill folder linked elsewhere in the plugin resolves outside the one
        # directory this plugin is trusted for, so it is left out.
        if os.path.normcase(os.path.dirname(skill.path)) == inside
    )
    return PluginSkillRoot(name, directory, entries) if entries else None

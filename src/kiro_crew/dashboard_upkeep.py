"""Dashboard upkeep: what each field a crewmate writes means, when, and how.

rfc-crewmate-dashboard-upkeep-skill.md. A fold field (``{"fold", "path"}``) is filled
by the gateway from the crew log, so nothing here is about it. An agentic field
(``{"agentic": true}``) is the crewmate's to write, and the ``[DASHBOARD]`` block only
names it. This module supplies two things for exactly those fields:

**The text**, through :func:`upkeep_rows`: each agentic field's ``upkeep`` object --
``means``, ``when``, ``how`` and, where the data lives outside the crew log, the
absolute path of a ``helper`` script. ``dashboard_agentic.fields_for_agent`` puts it on
each agentic row of the ``dashboard_fields`` reply, so a crewmate with no file tools
reads it the way it reads the field shapes. Nothing is generated for the text: it is
read when the tool is called, so it cannot go stale against the page in force.

**The helper scripts**, through :func:`sync`: a COPY of each shipped
``dashboard_templates/builtin/<id>/helpers/<file>`` the page in force names, under
``<skills dir>/.dashboard-upkeep/<slug>/scripts/``. A helper runs ``gh``, reads the
data and prints a ready ``dashboard_write`` payload. The directory holds the scripts
and nothing else; the leading dot keeps it out of the skills index, because the skill
tree walk skips dot directories.

**From what.** Field NAMES come from the page in force
(:func:`dashboard_agentic.page_in_force`), the same lookup ``[DASHBOARD]`` reads. The
TEXT and the helpers come from the SHIPPED catalog entry, never from the crewmate's copy
of the manifest: that copy sits under ``members/<slug>/dashboard/`` on the
agent-writable side, and ``instance.edit`` may change it. Only built-in templates
render (``instance.RENDERABLE_SOURCES``), so every page in force has a shipped entry.

**When.** :func:`sync` writes only what differs from disk, removes a script the page
does not name, and puts back a hand edit. Two callers: ``instance._commit`` (adopt,
apply, rollback and edit all commit through it) and ``dashboard_agentic.turn_block`` (a
crewmate on the default page never commits, and a gateway upgrade can change a shipped
helper's bytes). A root session's own dashboard (``instance.is_session_key``) has no
crewmate, no shell and no skills directory of its own, so it gets no scripts and no
helper path. Every function here is TOTAL: an upkeep write that fails costs the
pointer line, never the turn or the commit.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The dot-directory under the skills dir. The skill walk skips dot directories.
UPKEEP_DIR_NAME: Final[str] = ".dashboard-upkeep"

#: The one line ``[DASHBOARD]`` carries, so the route to the upkeep text survives every
#: compaction. It names the tool, not a file: a crewmate with no file tools can call it.
POINTER: Final[str] = (
    "Upkeep: dashboard_fields also says what each field means, when to write it, and "
    "how -- read it before you write a field."
)

#: The one line ``dashboard_fields`` answers for a page with no agentic field.
NOTHING_TO_WRITE: Final[str] = (
    "Every field on this page fills itself from the crew log. Nothing here is yours to "
    "write; do not republish it."
)

#: The ``when`` of a field whose shipped manifest declares no ``upkeep``.
DEFAULT_WHEN: Final[str] = "in the same turn what it shows changes"

_lock = threading.Lock()


def scripts_dir(slug: str) -> Path:
    """``<skills dir>/.dashboard-upkeep/<slug>/scripts``. *slug* is checked, never trusted."""
    from kiro_crew.members import validate_slug
    from kiro_crew.skills import skills_dir

    return skills_dir() / UPKEEP_DIR_NAME / validate_slug(slug) / "scripts"


def pointer() -> str:
    """The pointer line for ``[DASHBOARD]``."""
    return POINTER


def _shipped(template_id: str) -> tuple[dict[str, Any], Path | None]:
    """The SHIPPED catalog entry's field specs and ``helpers/`` dir, or empty. TOTAL."""
    try:
        from kiro_crew.dashboard_templates import catalog

        entry = catalog.load_one(template_id)
    except Exception:
        logger.debug("no shipped template for %r", template_id, exc_info=True)
        return {}, None
    return dict(entry.manifest.fields), entry.directory / "helpers"


def _has_scripts(slug: str) -> bool:
    """Whether *slug* gets a scripts directory: a member slug, not a session key."""
    from kiro_crew.dashboard_templates import instance as instance_store

    return not instance_store.is_session_key(slug)


def upkeep_rows(manifest: Any, slug: str | None = None) -> dict[str, dict[str, str]]:
    """Each agentic field's ``{means, when, how, helper?}``, by field name.

    *manifest* is the page in force (the crewmate's copy: it supplies the NAMES). The
    text is the shipped template's; a field the shipped manifest gives no ``upkeep``
    gets a default whose ``means`` and ``how`` point at the field's shape. ``helper``
    is the absolute path of the copied script when *slug* is a member slug; a session
    dashboard, or no slug, gets no helper path. TOTAL: an unreadable catalog gives
    every field the default.
    """
    from kiro_crew import dashboard_agentic

    shipped, _helpers = _shipped(manifest.id)
    rows: dict[str, dict[str, str]] = {}
    for name in sorted(dashboard_agentic.agentic_fields(manifest)):
        spec = shipped.get(name)
        upkeep = getattr(spec, "upkeep", None) if spec is not None and spec.agentic else None
        if upkeep is None:
            rows[name] = {
                "means": f"the {name} field, in the shape dashboard_fields gives for it",
                "when": DEFAULT_WHEN,
                "how": "write it with dashboard_write in that shape",
            }
            continue
        row = {"means": upkeep.means, "when": upkeep.when, "how": upkeep.how}
        if upkeep.helper and slug and _has_scripts(slug):
            try:
                row["helper"] = str(scripts_dir(slug) / upkeep.helper)
            except Exception:
                logger.debug("no helper path for %r", slug, exc_info=True)
        rows[name] = row
    return rows


def render_scripts(manifest: Any) -> dict[str, bytes]:
    """``{file name: bytes}`` of every shipped helper the page in force names. TOTAL."""
    from kiro_crew import dashboard_agentic

    shipped, helpers_dir = _shipped(manifest.id)
    scripts: dict[str, bytes] = {}
    if helpers_dir is None:
        return scripts
    for name in sorted(dashboard_agentic.agentic_fields(manifest)):
        spec = shipped.get(name)
        upkeep = getattr(spec, "upkeep", None) if spec is not None and spec.agentic else None
        if upkeep is None or not upkeep.helper:
            continue
        try:
            scripts[upkeep.helper] = (helpers_dir / upkeep.helper).read_bytes()
        except OSError:
            logger.debug("helper %s unreadable", upkeep.helper, exc_info=True)
    return scripts


def sync(slug: str, *, page: tuple[Any, Any] | None = None) -> Path | None:
    """Make *slug*'s helper scripts on disk match its page; the scripts dir, or ``None``.

    ``None`` for a session dashboard (nothing to write), for no page in force, and for
    a write that failed. TOTAL.
    """
    if not _has_scripts(slug):
        return None
    try:
        from kiro_crew import dashboard_agentic

        if page is None:
            page = dashboard_agentic.page_in_force(slug)
        if page is None:
            return None
        scripts = render_scripts(page[1])
    except Exception:
        logger.warning("dashboard upkeep scripts for %r not rendered", slug, exc_info=True)
        return None
    return _write(slug, scripts)


def view(slug: str) -> dict[str, Any] | None:
    """What the profile shows: the pointer, each field's upkeep text, the helper names.

    A read that writes nothing: the prompt path keeps the scripts current. TOTAL.
    """
    try:
        from kiro_crew import dashboard_agentic

        page = dashboard_agentic.page_in_force(slug)
        if page is None:
            return None
        manifest = page[1]
        rows = upkeep_rows(manifest, slug)
        # The suggestions page on the tab means [DASHBOARD] offers no fields and no
        # pointer, so the profile must not claim one either.
        suggestions = dashboard_agentic.tab_shows_suggestions(slug)
        return {
            # The pointer only when [DASHBOARD] carries it: a page with nothing to
            # write gets none there, so the profile must not claim one.
            "pointer": POINTER if rows and not suggestions else "",
            "fields": [{"field": name, **row} for name, row in rows.items()],
            "scripts": sorted(render_scripts(manifest)),
        }
    except Exception:
        logger.warning("dashboard upkeep for %r not rendered", slug, exc_info=True)
        return None


def _write(slug: str, scripts: dict[str, bytes]) -> Path | None:
    """Write only what differs from disk, and drop a script the page does not name.

    The tree sits under the data home, which a sandboxed crewmate can write, and this
    runs in the gateway. So every directory from the data home down is opened through
    :func:`pinned_fs.open_pinned_descendant_dir`, which refuses a link at each step, and
    every read, write, listing and unlink is relative to that descriptor and refuses a
    link at the leaf. A planted link costs the scripts, never a file elsewhere. The
    removal is what keeps a page swap from leaving the old board's helper behind.
    """
    try:
        from kiro_crew import pinned_fs
        from kiro_crew.config.loader import config_dir
        from kiro_crew.members import validate_slug
        from kiro_crew.owner_only_files import mkdirs_owner_only
        from kiro_crew.skills import SKILLS_DIR_NAME

        parts = (SKILLS_DIR_NAME, UPKEEP_DIR_NAME, validate_slug(slug), "scripts")
        # The data home itself is the trusted root; a fresh one may not exist yet.
        mkdirs_owner_only(config_dir())
        where = scripts_dir(slug)
        with (
            _lock,
            pinned_fs.open_pinned_descendant_dir(
                config_dir(), parts, what="dashboard upkeep scripts", create=True
            ) as dir_fd,
        ):
            for name, data in scripts.items():
                _put(dir_fd, where, name, data)
            for name in _names(dir_fd, where):
                if name not in scripts and _is_regular(dir_fd, where, name):
                    if dir_fd is None:
                        (where / name).unlink()
                    else:
                        os.unlink(name, dir_fd=dir_fd)
        return where
    except Exception:
        logger.warning("dashboard upkeep scripts for %r not written", slug, exc_info=True)
        return None


def _put(dir_fd: int | None, where: Path, name: str, data: bytes) -> None:
    """Replace *name* with *data* unless it already holds exactly that. Refuses a link."""
    from kiro_crew import pinned_fs
    from kiro_crew.atomic_write import atomic_write, atomic_write_at

    if dir_fd is None:
        # No descriptor-relative IO on this platform: the chain was lstat-checked, and
        # the leaf is checked here, which refuses a planted link.
        st = pinned_fs.lstat_by_name(where / name)
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise OSError(f"{where / name} is not a regular file")
        if st is None or (where / name).read_bytes() != data:
            atomic_write(where / name, data)
        return
    st = pinned_fs.stat_at(dir_fd, name)
    if st is not None:
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{where / name} is not a regular file")
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as fh:
            if fh.read(len(data) + 1) == data:
                return
    atomic_write_at(dir_fd, name, data.decode("utf-8"))


def _names(dir_fd: int | None, where: Path) -> list[str]:
    return os.listdir(dir_fd) if dir_fd is not None else os.listdir(where)


def _is_regular(dir_fd: int | None, where: Path, name: str) -> bool:
    from kiro_crew import pinned_fs

    if dir_fd is not None:
        return pinned_fs.is_regular_at(dir_fd, name)
    st = pinned_fs.lstat_by_name(where / name)
    return st is not None and stat.S_ISREG(st.st_mode)

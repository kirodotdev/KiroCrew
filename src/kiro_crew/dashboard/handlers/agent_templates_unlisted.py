"""The spec files the Custom agents roster does not show, though each still takes a name.

The roster (``GET /api/agents/templates``) is built from discovery, which lists
only a spec that reads cleanly and keeps one row per declared name. Every writer
that mints ``<name>.json`` asks a stricter question first: is the stem taken by
ANY spec file, readable or not (``_load_template_specs``, ``_spec_stem_on_disk``).
So a file the roster drops still takes its stem, and a create answers
``409 name_taken`` while the tab has nothing to open, rename or delete. This
module names those files, so the tab can list them with the reason and the
create refusal can say which file holds the name. A listed file holds one more
name than its row shows when its stem differs from the name it declares (a
copy saved as ``src-custom.json`` that still declares ``src``), and the
refusal of that stem names the file and the row too (``listed_refusal_text``).

Each entry has one of two reasons, and each reason has its own remedy:

* ``unreadable``: the hardened reader refused the file. It holds invalid JSON
  or frontmatter, or is not an object, or is over the size cap, or is a link
  the reader will not follow, or its read raises (a document nested past the
  parser's depth raises ``RecursionError``). A file that reads but raises while
  its row is built (``spec_row``) counts the same, as discovery skips it too.
  Such a file still declares a name, and the create's taken check counts that
  name as well as the stem, so its entry carries it as
  ``declared_name`` and it is classified by it (``spec_identity``). The tab can
  delete it when it is directly beneath the agents directory, no readable spec
  answers to its stem, and it is not read-only by what is known of it (a
  markdown file, a private copy, a package's file). The delete is the same
  tombstone rename a template delete uses.
* ``duplicate_name``: the file reads, but it declares a name another listed
  file already holds, so discovery's one-row-per-name rule dropped it. It is
  deleted by its stem when that stem reaches this file alone and the listed
  file keeps the name.

When a listed file still declares the name a hidden file declares, the name
stays with the listed file: the delete of the hidden file counts its stem
alone in the reference guard and keeps that name's sidecar entry. Not when the
listed file holds nothing of its own once the hidden file goes (a symlink that
reads the hidden file, or a markdown spec while a configured host loads JSON
specs alone; ``holds_once_retired``, the rule the delete applies too): the
tab's confirm would say the listed file and the agent stay as they are, so
the tab offers no delete of it, and the delete guards the name as well. Nor
when ``agent.spec_pins`` picks the hidden file for that name, by naming it or
a symlink that reads it (``pinned_here``): every session of the name runs
that file whichever one the roster lists, so the tab offers no delete of it,
and the delete guards the name too.

``deletable`` says whether the tab may offer that delete. The delete handler
still decides; it re-checks everything under its locks and its reference guard.

The files the roster leaves out on purpose are left out here too, so the list
never names one as a problem: an app launch copy, an AppleDouble sidecar, the
runtime's own spec files (a create refuses those names as reserved before it
checks for a taken name), and the same-package twin a locally published package
ships beside its listed file (``{package}-{name}.json`` and
``local-{package}-{name}.json``; the package manager owns both). A file whose
filename would be masked stays out, as its row would, and so does a duplicate
whose declared name would be. An unreadable file whose declared name would be
masked is listed without ``declared_name``: its stem is still taken.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any, Callable, Mapping

from kiro_crew.agent_discovery import (
    AgentInfo,
    _global_agent_info,
    _read_agent_spec,
    iter_agent_spec_files,
)
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    is_app_agent_launch_name,
    is_markdown_spec,
)
from kiro_crew.agent_spec_pins import spec_pin_for, spec_pins

logger = logging.getLogger(__name__)

UNLISTED_UNREADABLE = "unreadable"
UNLISTED_DUPLICATE_NAME = "duplicate_name"


def spec_row(path: Path, spec: dict[str, Any]) -> AgentInfo | None:
    """Discovery's row for *spec*, read from *path*, or ``None`` when no row
    can be built from it.

    A spec can read as an object yet raise while its row is built, and the
    agents directory is shared with other tools. ``list_agents`` skips such a
    file at DEBUG, as it skips one that does not read, so the handlers here
    treat it as unreadable too rather than failing the whole request.
    """
    try:
        return _global_agent_info(path, spec)
    except Exception:
        logger.debug("no agent row can be built from %s", path, exc_info=True)
        return None


def spec_identity(path: Path, spec: dict[str, Any] | None) -> AgentInfo:
    """What a spec file no row can be built from is still known by.

    *spec* is the parse, or ``None`` for a file that does not read. The row is
    the one discovery builds from the declared ``name`` alone: that name (the
    stem when the file declares none or does not read), the package the
    ``{package}-{name}.json`` convention reads from it, and the runtime-file
    rule. The create's taken check counts a declared name as well as the stem,
    so the delete guards and classifies a file that reads by that name, not by
    its stem only.
    """
    declared = None if spec is None else spec.get("name")
    return _global_agent_info(path, {"name": declared} if declared else {})


def _resolved(path: Path) -> Path | None:
    """Where *path* ends, or ``None`` when it does not resolve. A symlink loop
    does not either; Python 3.12's ``Path.resolve`` reports one with
    ``RuntimeError``, not ``OSError``."""
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def link_reads(path: Path, retiring: Path) -> bool:
    """Whether *path* is a symlink whose chain ends at *retiring*, where a spec
    file about to be retired resolves: it reads that file, and dangles once the
    file is renamed. When the retiring file is itself a link, *retiring* is its
    target, so another link to that target counts too and keeps the guard. A
    link that does not resolve reads nothing."""
    try:
        return path.is_symlink() and _resolved(path) == retiring
    except OSError:
        return False


def holds_once_retired(path: Path, retiring: Path, markdown_holds: Callable[[], bool]) -> bool:
    """Whether the spec file at *path*, which declares the name a file
    resolving to *retiring* declares, still holds that name once that file is
    retired.

    It holds nothing when it does not resolve, when it is a link that reads the
    retiring file (``link_reads``), or when it is a markdown spec and
    *markdown_holds*, asked only then, answers that a configured host loads
    JSON specs alone: on that host the name would reach no spec. The regular
    file a retiring link ends at survives the rename, and holds.
    """
    if _resolved(path) is None or link_reads(path, retiring):
        return False
    return not is_markdown_spec(path.name) or markdown_holds()


def pinned_here(name: str, spec_path: Path, pins: Mapping[str, str] | None = None) -> bool:
    """Whether ``agent.spec_pins`` picks the spec file *spec_path* for the agent
    *name*: the pin names that file, or a symlink that reads it
    (``link_reads``). Every session of *name* then runs that file
    (``declared_spec_claim``), whichever file the roster lists for the name.
    *pins* overrides the live read.
    """
    pin = spec_pin_for(name, pins)
    if pin is None:
        return False
    if pin == spec_path.name:
        return True
    retiring = _resolved(spec_path)
    return retiring is not None and link_reads(spec_path.parent / pin, retiring)


def _not_a_template_file(path: Path) -> bool:
    """A file in the agents directory that no roster offers as a template."""
    return path.name.startswith("._") or is_app_agent_launch_name(path.name)


def unlisted_spec_files(
    agents_dir: Path,
    infos: list[AgentInfo],
    shown: set[str],
    forks: dict[str, dict[str, Any]],
    mask: Callable[[object], str],
) -> list[dict[str, Any]]:
    """Every spec file in *agents_dir* that backs no row of the roster.

    Thread-side. *infos* is discovery's list (every row, before masking),
    *shown* the filenames of the rows the roster returns, *forks* the fork
    sidecar. Only the files backing no shown row are read again, so a roster
    with nothing hidden costs one directory listing.
    """
    # circular import: agent_templates imports this module at its top.
    from kiro_crew.dashboard.handlers.agent_templates import (
        _every_host_reads_markdown,
        _spec_file_beneath,
        template_read_only_reason,
    )

    live = [f for f in iter_agent_spec_files(agents_dir) if not _not_a_template_file(f)]
    on_disk = {f.name for f in live}
    pins = spec_pins()
    # Read the config at most once, and only for a listed markdown holder.
    markdown_holds = functools.cache(_every_host_reads_markdown)
    # Which readable files answer to a name, by declared name or by stem: the
    # same two ways the delete handler resolves a name to a file.
    reach: dict[str, set[str]] = {}

    def _claim(filename: str, declared: str) -> None:
        for key in {declared, Path(filename).stem}:
            reach.setdefault(key, set()).add(filename)

    holder: dict[str, AgentInfo] = {}
    for row in infos:
        if row.filename in on_disk:
            _claim(row.filename, row.name)
        if row.filename in shown:
            holder.setdefault(row.name, row)
    hidden: list[tuple[Path, dict[str, Any] | None, AgentInfo | None]] = []
    for f in live:
        if f.name in shown or f.name in OWNED_KIRO_AGENT_FILES:
            continue
        try:
            spec = _read_agent_spec(f, operation="api_agent_templates", source="dashboard")
        except Exception:
            # As ``list_agents`` skips it: a document nested past the parser's
            # depth raises ``RecursionError``, and one such file in a shared
            # directory must not fail the whole tab.
            logger.debug("hidden agent spec %r does not read", f, exc_info=True)
            spec = None
        info = None if spec is None else spec_row(f, spec)
        if info is not None:
            _claim(f.name, info.name)
        hidden.append((f, spec, info))

    entries: list[dict[str, Any]] = []
    for f, spec, info in hidden:
        if mask(f.name) != f.name:
            continue
        beneath = _spec_file_beneath(agents_dir, f.name) is not None
        if info is None:
            # Classified as the delete classifies it (``unreadable_spec_at``).
            known = spec_identity(f, spec)
            known.private_to = str((forks.get(known.name) or {}).get("private_to", ""))
            entry: dict[str, Any] = {
                "filename": f.name,
                "stem": f.stem,
                "reason": UNLISTED_UNREADABLE,
            }
            if spec is not None and mask(known.name) == known.name:
                entry["declared_name"] = known.name
            entry["deletable"] = (
                beneath
                and not reach.get(f.stem)
                and template_read_only_reason(known) is None
                and not (spec is not None and pinned_here(known.name, f, pins))
            )
            entries.append(entry)
            continue
        listed = holder.get(info.name)
        # A readable file whose name no shown row holds is one whose own row
        # was left out (a masked-looking name), or one that changed after the
        # roster was read; neither is a hidden duplicate.
        if listed is None or mask(info.name) != info.name:
            continue
        # Discovery's same-package-twin rule: one package shipping the name
        # twice is expected, not a problem to fix by hand.
        if info.package and info.package == listed.package:
            continue
        fork = forks.get(info.name) or {}
        info.private_to = str(fork.get("private_to", ""))
        # The confirm says the listed file and the agent of this name stay as
        # they are, so the delete is offered only when the listed file still
        # holds the name once this one goes: the rule the delete applies to
        # decide where the name stays (``_declared_elsewhere``).
        retiring = _resolved(f) if beneath else None
        entries.append(
            {
                "filename": f.name,
                "stem": f.stem,
                "reason": UNLISTED_DUPLICATE_NAME,
                "declared_name": info.name,
                "listed_as": mask(listed.filename),
                "deletable": retiring is not None
                and reach.get(f.stem) == {f.name}
                and template_read_only_reason(info) is None
                and not pinned_here(info.name, f, pins)
                and listed.filename in on_disk
                and holds_once_retired(agents_dir / listed.filename, retiring, markdown_holds),
            }
        )
    return entries


def unreadable_spec_at(agents_dir: Path, name: str) -> AgentInfo | None:
    """The live spec file whose stem is exactly *name* and that does not read
    (or reads but yields no row, ``spec_row``), as ``spec_identity`` knows it,
    or ``None``.

    Thread-side. ``DELETE /api/agents/detail/{name}`` resolves a name through
    the specs that read, and falls back to this so a file the roster cannot
    show, whose stem stays taken, can still be deleted. Only a directory entry
    of *agents_dir* qualifies: *name* comes from the URL, so a candidate that is
    not one of the listed spec files (a traversing name) names nothing.
    """
    if not name or Path(name).name != name:
        return None
    live = set(iter_agent_spec_files(agents_dir))
    for candidate in agent_spec_candidates(agents_dir, name):
        if candidate not in live or _not_a_template_file(candidate):
            continue
        try:
            spec = _read_agent_spec(
                candidate, operation="api_agent_template_delete", source="dashboard"
            )
        except Exception:
            # Unreadable, as ``unlisted_spec_files`` lists it.
            logger.debug("agent spec %r does not read", candidate, exc_info=True)
            spec = None
        if spec is not None and spec_row(candidate, spec) is not None:
            return None
        return spec_identity(candidate, spec)
    return None


def unlisted_refusal_text(name: str, entry: dict[str, Any]) -> str:
    """The ``409 name_taken`` sentence when *entry*, a file the roster does not
    show, is what holds *name*: by its stem, or by the name it declares when it
    reads but yields no row. Every value in *entry* is already masked."""
    filename = entry["filename"]
    if entry["reason"] == UNLISTED_DUPLICATE_NAME:
        why = (
            f"it declares '{entry['declared_name']}', which {entry['listed_as']} "
            "already holds, so the list shows only that file"
        )
    elif entry.get("declared_name") and name.lower() != entry["stem"].lower():
        why = (
            f"it declares '{entry['declared_name']}' but cannot be read as an agent "
            "spec, so the list does not show it"
        )
    else:
        why = "it cannot be read as an agent spec, so the list does not show it"
    return (
        f"'{name}' is taken by ~/.kiro/agents/{filename}: {why}. "
        "Fix, rename or delete that file to free the name."
    )


def listed_refusal_text(name: str, listed: dict[str, str]) -> str:
    """The ``409 name_taken`` sentence when *name* is the stem of a listed file
    whose row carries another name, the one the file declares. *listed* comes
    from a row the roster returns, so its values already passed the mask."""
    return (
        f"'{name}' is taken by ~/.kiro/agents/{listed['filename']}, which the list "
        f"shows as '{listed['name']}', the name that file declares. Change the name "
        "inside that file, or rename or delete the file, to free the name."
    )

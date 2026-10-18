"""What a manifest's declarations may reach: app-root paths and contributed agents.

A resource path an app declares (an agent spec, a skill, a UI entry, a backend entry
point) is joined onto the app root, so ``_app_path_refusal`` refuses one that is rooted
or carries a ``..`` segment, judged the same way on every host, or that the shared path
screen (``screen_app_path``, which reads every link before anything follows it) places
outside the root, refuses, or could not finish checking -- the four ``Containment``
answers. A contributed command may select only an agent the app itself registers:
``_declared_agent_names`` derives those names from the app's own specs and
``contain_command_agents`` drops any other selection from a listed manifest.

Served through :mod:`kiro_crew.apps.manifest`, which callers and tests import and patch;
this module never imports it.
"""

from __future__ import annotations

import enum
import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX


def _is_rooted_path(rel_path: str) -> bool:
    """Return True if ``rel_path`` is anything other than a purely relative path.

    App-resource paths are joined onto the app root, so any path carrying a drive
    or a root anchor can relocate that join and must be refused. ``is_absolute()``
    is too narrow twice over:

    - It is flavour-bound to the RUNNING host, and ``os.path`` IS ``ntpath`` on
      Windows, so ``os.path.isabs(x) or ntpath.isabs(x)`` collapses to one
      Windows-only test there. Windows' flavour does not consider ``/etc/passwd``
      anchored (no drive), so a POSIX-absolute path passed validation on Windows.
      The Windows flavour is a strict superset — it reads ``/`` and ``\\`` as a
      root — so testing it alone covers both syntaxes on either host.
    - A drive-relative path (``D:evil.py``) has a drive but no root, so
      ``is_absolute()`` is False, yet joining it onto the app root yields
      ``D:evil.py`` and escapes. Hence drive-OR-root, not ``is_absolute()``.
    """
    win = PureWindowsPath(rel_path)
    return bool(win.drive or win.root)


def _has_dotdot_segment(rel_path: str) -> bool:
    """Return True if ``rel_path`` contains a ``..`` segment under EITHER flavour.

    Segmenting both ways is load-bearing: a POSIX host reads ``..\\evil`` as one
    opaque filename, so a POSIX-only split lets a backslash traversal through —
    and the manifest that declares it is portable data, validated on whichever
    host happens to install the app. No legitimate resource path has a bare
    ``..`` segment (``a..b`` and ``notes..md`` are single segments and unaffected).
    """
    return ".." in PurePosixPath(rel_path).parts or ".." in PureWindowsPath(rel_path).parts


class Containment(enum.Enum):
    """Where a declared app-resource path lands, as the shared path screen sees it.

    ``REFUSED`` is a lasting answer from the screen (the path or its link target
    is one it never accepts); ``UNCHECKED`` is a screen check that did not
    complete, which a later attempt may resolve.
    """

    INSIDE = "inside"
    OUTSIDE = "outside"
    REFUSED = "refused"
    UNCHECKED = "unchecked"


# Named by the path callers import it from, the facade that serves it.
Containment.__module__ = "kiro_crew.apps.manifest"


def screen_app_path(path: Path) -> str | Containment:
    """``path`` canonicalised by :func:`kiro_crew.hooks.screen_file_path`.

    The screen reads a link's own target before anything follows the link. A path
    it declines comes back as :attr:`Containment.REFUSED` when the decline is
    lasting and as :attr:`Containment.UNCHECKED` when one of its checks did not
    complete. An ``OSError`` or ``ValueError`` from the screen is also
    :attr:`Containment.UNCHECKED`: the path was not judged.
    """
    from kiro_crew import hooks

    try:
        screened = hooks.screen_file_path(str(path))
    except (OSError, ValueError):
        return Containment.UNCHECKED
    if isinstance(screened, str):
        return screened
    if screened is hooks.PathDecline.UNCHECKED:
        return Containment.UNCHECKED
    return Containment.REFUSED


def contained_in_screened_root(candidate: Path, screened_root: str | Containment) -> Containment:
    """Whether ``candidate`` stays inside ``screened_root``, :func:`screen_app_path`'s root answer.

    A caller checking several paths against one root screens the root once and
    passes the answer here, so each path costs one screen call. A root the screen
    declined gives every candidate that same answer.
    """
    if isinstance(screened_root, Containment):
        return screened_root
    resolved = screen_app_path(candidate)
    if isinstance(resolved, Containment):
        return resolved
    if Path(resolved).is_relative_to(screened_root):
        return Containment.INSIDE
    return Containment.OUTSIDE


def app_root_containment(candidate: Path, app_root: Path) -> Containment:
    """:func:`contained_in_screened_root` for a single path, screening ``app_root`` too."""
    return contained_in_screened_root(candidate, screen_app_path(app_root))


#: Refusal wording for an app-resource path that leaves the app root.
PATH_TRAVERSAL_REFUSAL = "contains path traversal"
#: Refusal wording for an app-resource path the shared screen does not accept.
PATH_SCREEN_REFUSAL = "is refused by the path screen"
#: Refusal wording for an app-resource path the shared screen could not finish
#: checking (a slow disk, a busy resolver, a link it could not read). Kept apart
#: from the two refusals above because the manifest may be fine and a later
#: attempt may pass.
PATH_UNVERIFIED_REFUSAL = "could not be checked against the app root"


def _app_path_refusal(
    rel_path: str, app_root: Path | None, screened_root: str | Containment | None = None
) -> str:
    """Why ``rel_path`` is not an acceptable app-resource path, or ``""`` when it is.

    Refused as :data:`PATH_TRAVERSAL_REFUSAL` when it is rooted (drive-qualified,
    POSIX-absolute, or UNC), contains a ``..`` segment, or — canonicalised against
    ``app_root`` — leaves ``app_root`` (canonical containment, matching the runtime
    checks in ``module_loader`` / ``bridges``). Refused as
    :data:`PATH_SCREEN_REFUSAL` when the shared screen does not accept it, and as
    :data:`PATH_UNVERIFIED_REFUSAL` when the screen could not finish checking it.
    ``screened_root`` is :func:`screen_app_path`'s answer for ``app_root``.

    The lexical checks run BEFORE the canonical one and regardless of whether
    ``app_root`` is known, so a manifest is judged identically on every host.
    Deferring them to ``resolve()`` would make the verdict host-dependent: on a
    POSIX host ``app_root / "..\\evil.py"`` is a single odd filename that stays
    inside the root and would be accepted, while the same manifest is rejected on
    Windows. Canonical containment then adds what no lexical check can see — a
    symlink or reparse point inside the root whose target leaves it.
    """
    if not rel_path:
        return ""
    if _is_rooted_path(rel_path) or _has_dotdot_segment(rel_path):
        return PATH_TRAVERSAL_REFUSAL
    if app_root is not None:
        if screened_root is None:
            screened_root = screen_app_path(app_root)
        contained = contained_in_screened_root(app_root / rel_path, screened_root)
        if contained is Containment.UNCHECKED:
            return PATH_UNVERIFIED_REFUSAL
        if contained is Containment.REFUSED:
            return PATH_SCREEN_REFUSAL
        if contained is Containment.OUTSIDE:
            return PATH_TRAVERSAL_REFUSAL
    return ""


#: ``bridges._TEMPLATE_PLACEHOLDER_RE``, duplicated because bridges imports the manifest
#: facade, which imports this module.
_AGENT_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")


def _declared_agent_names(
    agent_paths: list[str],
    app_root: Path | None,
    unchecked: list[str] | None = None,
    *,
    known_refusals: dict[str, str] | None = None,
) -> set[str]:
    """Names the app's own ``agents`` register under, for containing ``commands[].agent``.

    The name a command can select: the agent JSON's explicit ``name``. A path
    :func:`_app_path_refusal` does not accept contributes nothing: registration
    skips it. That includes a path the screen could not check yet, which vouches
    for no name on this listing; the next listing asks the screen again.

    With ``app_root`` known, a spec that registration would also skip -- missing,
    unreadable, malformed, or not a JSON object -- contributes nothing either. Its
    stem would otherwise authorize a name the app never registers, which then
    resolves to whatever HOST agent carries it. Without ``app_root`` only the stem is
    known (a lexical, host-independent verdict); the app-root pass at listing time
    re-checks it against the files.

    ``unchecked``, when given, collects the paths the screen could not finish
    checking, so a caller can tell "names no agent" from "not checked yet".
    ``known_refusals`` maps each path to the :func:`_app_path_refusal` answer the
    caller already has for it in this pass, so no path is screened twice.
    """
    names: set[str] = set()
    known = known_refusals if known_refusals is not None else {}
    screened_root: str | Containment | None = None
    if app_root is not None and any(str(rel) not in known for rel in agent_paths):
        screened_root = screen_app_path(app_root)
    for rel in agent_paths:
        rel = str(rel)
        if not rel:
            continue
        refusal = known.get(rel)
        if refusal is None:
            refusal = _app_path_refusal(rel, app_root, screened_root)
        if refusal:
            if refusal == PATH_UNVERIFIED_REFUSAL and unchecked is not None:
                unchecked.append(rel)
            continue
        name: Any = PurePosixPath(rel.replace("\\", "/")).stem
        if app_root is not None:
            # The hardened, audited reader: it authorizes the descriptor it actually
            # reads, so a source spec swapped for a symlink to a credential file after
            # the containment check is refused before a byte is consumed.
            from kiro_crew.agent_discovery import _read_agent_spec

            data = _read_agent_spec(
                app_root / rel, operation="app_manifest_agent_names", source="unknown"
            )
            if not isinstance(data, dict):
                continue
            # A templated spec is rendered by the gateway and registers nothing when a
            # placeholder cannot be resolved, so it vouches for no name here: fail
            # closed rather than re-deriving which placeholders this host resolves.
            if _AGENT_TEMPLATE_PLACEHOLDER_RE.search(json.dumps(data)):
                continue
            # Registration writes ``<app>--<name>.json`` keyed by the source's own
            # ``name``; a spec without one registers under no name a command can
            # select, so only an explicit string ``name`` vouches here.
            name = data.get("name")
        # The unsafe names `_register_agents` refuses, plus the reserved skill-view
        # prefix: the spawn maps such a name to the HOST agent its view was built
        # from, so it can never name one of the app's own agents.
        if (
            isinstance(name, str)
            and name
            and name not in (".", "..")
            and not any(c in name for c in ("/", "\\", "\x00"))
            and not name.startswith(NATIVE_SKILL_ALIAS_PREFIX)
        ):
            names.add(name)
    return names


def contain_command_agents(
    manifest_data: dict[str, Any],
    agent_paths: list[str],
    app_root: Path,
    is_registered: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Drop every ``contributes.commands[].agent`` that is not one of the app's own.

    ``AppManifest.validate`` refuses such an agent at install, but a self-managed app
    registers (and may later rewrite) its own ``app.json`` without passing through it.
    This runs on the listing the launcher reads, against the files on disk, so no
    unverified selection reaches it whatever path the manifest arrived by. The command
    itself is kept and runs on the dashboard default. Mutates and returns
    ``manifest_data``.

    ``is_registered`` (when given) is the final word: an agent survives only if the
    app actually REGISTERED it, judged from what registration wrote. The lexical pass
    above mirrors registration's skip rules; this one does not depend on mirroring
    them, so a skip reason nobody listed here still cannot authorize a name.
    """
    contributes = manifest_data.get("contributes")
    commands = contributes.get("commands") if isinstance(contributes, dict) else None
    if not isinstance(commands, list):
        return manifest_data
    own: set[str] | None = None
    for cmd in commands:
        if not isinstance(cmd, dict) or "agent" not in cmd:
            continue
        if own is None:
            own = _declared_agent_names(agent_paths, app_root)
        agent = cmd.get("agent")
        if (
            not isinstance(agent, str)
            or agent not in own
            or (is_registered is not None and not is_registered(agent))
        ):
            cmd.pop("agent", None)
    return manifest_data

"""Resolving a script cron's ``"<path>:<func>"`` spec to the file it runs.

``resolve_script_path`` is the one resolver for every script spec. By default a
relative path resolves against the process CWD and the resolved file must then sit
under ``<config_dir>/crons/``. With ``app_root`` a relative spec is joined onto that
app's bundle and must stay inside it; with ``allow_bundle_roots`` a persisted spec
must sit under the shared bundle roots. Every candidate is resolved before the
containment check, so a link out of a root is judged by its target.

Part of :mod:`kiro_crew.cron_script`, the import path callers, tests and docs use: it
reaches every name here, and a patch through it reaches this module too
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.config.loader import config_dir
from kiro_crew.security import is_unverifiable_path_refusal, sensitive_path_refusal


def _split_script_spec(script_path: str) -> tuple[str, str]:
    """Split a ``"<path>:<func>"`` spec into ``(path, func)``, drive-aware.

    Splits on the LAST colon. A Windows drive letter adds a second colon at
    index 1 (``C:\\...``); taking the rightmost colon keeps the whole drive path
    and the trailing func (``C:\\crons\\job.py:run`` -> ``C:\\crons\\job.py`` +
    ``run``). The only ambiguous input is a bare drive path with no ``:func``
    suffix, which would otherwise split at the drive colon into the nonsense
    ``("C", "\\crons\\job.py")`` — so a colon that IS the drive colon does not
    count as the separator.
    """

    drive_colon = len(script_path) >= 2 and script_path[1] == ":" and script_path[0].isalpha()
    func_colon = script_path.rfind(":")
    if func_colon == -1 or (drive_colon and func_colon == 1):
        raise ValueError(f"Invalid script path '{script_path}': expected 'path.py:func'")
    return script_path[:func_colon], script_path[func_colon + 1 :]


def _trusted_script_bundle_roots() -> tuple[Path, ...]:
    """Roots, besides ``crons/``, that legitimately hold a cron script.

    An app ships its cron script inside its OWN tree, so a bundle script's
    resolved path lands outside ``crons/`` by construction. Two kinds of root
    provide bundles, matching the two sources
    ``apps.bridges._registration_source`` reads a manifest from:

    * the BUILTIN manifest sources, which is where a shipped builtin's bundle
      lives, and which the bridge deliberately reads builtins from so a mutable
      installed directory cannot borrow a builtin's name.
    * ``<config_dir>/apps``, a third-party app's installed snapshot.

    The builtin leg delegates to ``apps.execution._builtin_manifest_sources``,
    the SAME function ``shipped_builtin_app_root`` walks to CHOOSE a builtin's
    root, rather than assuming that root is under this package. It is not: that
    function also returns the active edition's
    ``apps_loader.manifest_sources()``, which can sit anywhere. One authority for
    both ends is what keeps registration and fire time in agreement -- the
    registrar is handed a builtin's chosen root as ``app_root``, and the
    context-free consumers must recognise that same root, or a cron registers and
    is then refused when it fires. The package directory is admitted ONLY when that
    walk fails, since a successful walk already reports this package's
    ``apps/builtins`` and no builtin bundle lives under the package outside it.

    Deliberately NOT delegated to ``skills._trusted_skill_roots``, which today
    computes a similar set for app-shipped SKILLS. The sets overlap by
    coincidence, not by rule: a skill is prose the scanner reads, a cron script
    is code the launcher executes, and the two admit different things (a skill
    root holds a directory tree with ``SKILL.md``, a script root holds a ``.py``
    file). Sharing one helper would let a future change to which trees may
    supply ``SKILL.md`` silently change which files are EXECUTABLE as crons, in
    a module whose tests would not run.

    Imports are function-local because ``cron_script`` is imported by
    ``mcp_cron``, which ``apps.bridges`` imports back, so a module-level edge
    into the apps package would close that cycle.
    """
    roots: list[Path] = []
    try:
        from kiro_crew.apps.execution import _builtin_manifest_sources

        roots.extend(_builtin_manifest_sources())
    except Exception:  # noqa: BLE001 — an unavailable seam must not stop resolution
        # Fallback, on THIS leg only: a composition where the platform seam is not
        # available, which is how ``_builtin_manifest_sources`` itself degrades. It
        # is deliberately not appended when the walk succeeded -- the walk's own
        # first entry is this package's ``apps/builtins``, so the only paths this
        # would add are ones OUTSIDE the builtins tree, and no builtin bundle lives
        # there. Admitting the package directory wholesale would make every ``.py``
        # under it resolvable as a bundle script for no bundle that needs it.
        roots.append(Path(__file__).parent.resolve())
    try:
        from kiro_crew.apps.manager import apps_dir

        roots.append(apps_dir().resolve())
    except (OSError, ValueError, ImportError):  # an unresolvable home must not stop resolution
        pass
    # Order-preserving dedupe: _builtin_manifest_sources may already report this
    # package's builtins dir, and a repeated root would be checked twice.
    return tuple(dict.fromkeys(roots))


def _bundle_relative_spec(module_part: str, app_root: Path) -> str:
    """Rebase a bundle-RELATIVE script path onto ``app_root``; pass others through.

    Pure path arithmetic. It touches no filesystem: no ``resolve()``, no
    ``exists()``, no read. Resolution and canonical containment stay in
    :func:`resolve_script_path`, which is deliberate -- keeping the join here
    lets that function's ``resolve()`` line stay exactly as it has always been,
    and keeps this step's only job legible.

    An absolute spec is returned unchanged, so an app naming a full path is
    judged by containment rather than silently re-rooted.

    A ``..`` segment is refused LEXICALLY, before any join, in the same order and
    for the same reason as ``apps.manifest._app_path_refusal``: the verdict
    is then identical on every host, where deferring to ``resolve()`` would make
    it host-dependent (on POSIX ``..\\evil.py`` is one odd filename that stays
    inside the root; on Windows it escapes). Canonical containment in the caller
    adds what no lexical check can see -- a link inside the root whose target
    leaves it.
    """
    expanded = Path(os.path.expanduser(module_part))
    if expanded.is_absolute():
        return module_part
    if ".." in expanded.parts:
        raise PermissionError(f"Script path may not traverse upward: {module_part}")
    return str(app_root / expanded)


def resolve_script_path(
    script_path: str,
    *,
    app_root: Path | None = None,
    allow_bundle_roots: bool = False,
) -> tuple[str, str]:
    """Validate and resolve a script path. Returns (file_path, func_name).

    Format: ``"<path>.py:function"``. Default behaviour is the OPERATOR
    contract, byte for byte: a relative path resolves against the process CWD,
    and the resolved file must sit under ``<config_dir>/crons/``. ``cron_add``,
    the CLI and the vault-grant paths pass neither keyword, so nothing below
    reaches them.

    An app cron's script legitimately lives in the app's own bundle rather than
    in ``crons/``, and the two keywords are how a caller says so. They are
    separate because they answer different questions, and each opens one root.

    ``app_root`` says "this spec belongs to THIS app", and is passed where a
    manifest's own spec is vetted (``apps.bridges``, ``apps.cron_sdk``). It
    becomes the base a RELATIVE spec resolves against, because ``"job.py:run"``
    means "next to my manifest" and is the only spelling an app can write
    without knowing its install location. With no base that resolved against
    whatever directory the gateway process happened to start in, naming a file
    that was never there. Containment is that ONE bundle, so app A cannot name a
    script inside app B's tree.

    ``allow_bundle_roots`` says "this spec was ALREADY vetted and persisted",
    and is passed only by the consumers that re-resolve a stored ``job.script``
    holding no app context: the fire-time governance gate, the launcher, and the
    dashboard's script-source endpoint. Containment is the shared bundle roots,
    because a stored absolute bundle path is all those callers have to go on. It
    widens no authoring path: a freshly authored spec must still be under
    ``crons/``, so ``cron_add`` cannot register a script inside a bundle.

    A bundle root accepts ``.py`` files only, under either keyword. That is a
    containment control rather than a style rule, and the surface it guards is
    EXECUTION: the launcher puts the resolved file's directory on ``sys.path``,
    imports the file as a module and calls ``func_name``, so whatever this
    function returns is a path the gateway will run. A bundle holds more than
    code -- ``.app_secret`` is the app's gateway credential (see
    ``dashboard.token_auth``) and ``data/`` holds app state -- and nothing later
    in the chain re-checks the suffix, so without it a manifest could name any
    bundle file as an entry point and have the launcher try to execute it.
    ``crons/`` keeps no such rule, because it exists only to hold scripts.

    The dashboard's script-source endpoint is NOT part of that reasoning: its
    read stays pinned to ``crons/``, so a bundle path is refused there with
    ``script_read_refused`` whatever its suffix.

    Unchanged on every path: a ``..``-bearing relative spec is refused
    lexically before any join, so the verdict never depends on the host's path
    grammar; ``.resolve()`` runs BEFORE containment, so a link pointing out of a
    trusted root is rejected on its target rather than followed;
    ``is_sensitive_path`` still vets the resolved path; and the body scan
    (``mcp_cron._vet_script_file``) is a separate gate this function does not
    speak for. Vault secret GRANTS stay narrower than all of it: their reader
    (:func:`_read_script_body`) is pinned to ``crons/`` alone and the grant paths
    pass neither keyword, so a bundle script can register and run but can never
    be handed a secret.
    """
    module_part, func_name = _split_script_spec(script_path)

    if app_root is not None:
        module_part = _bundle_relative_spec(module_part, app_root)
    file_path = Path(os.path.expanduser(module_part)).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Script file not found: {file_path}")
    if reason := sensitive_path_refusal(str(file_path)):
        if is_unverifiable_path_refusal(reason):
            raise PermissionError(reason)
        raise PermissionError(f"Script path blocked by security policy: {file_path}")
    crons_dir = (config_dir() / "crons").resolve()
    if app_root is None and file_path.is_relative_to(crons_dir):
        return str(file_path), func_name
    if app_root is not None:
        bundle_roots: tuple[Path, ...] = (app_root.resolve(),)
    elif allow_bundle_roots:
        bundle_roots = _trusted_script_bundle_roots()
    else:
        bundle_roots = ()
    for root in bundle_roots:
        if not file_path.is_relative_to(root):
            continue
        if file_path.suffix.lower() != ".py":
            raise PermissionError(f"App bundle script must be a .py file, got: {file_path}")
        return str(file_path), func_name
    admitted = bundle_roots if app_root is not None else (crons_dir, *bundle_roots)
    roots_shown = ", ".join(str(r) for r in admitted)
    raise PermissionError(f"Script must be under one of {roots_shown}, got: {file_path}")

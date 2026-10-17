"""Keep an agent workspace and its temp paths clear of sealed runtime paths.

The voice-runtime directories, the declared temp paths a spawn may use, and the bound
agent workspace whose identity is pinned before a session starts. One of the owners
``kiro_crew.sandbox`` is carved into: every name here stays readable and patchable as
``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import sys
import threading
from typing import TYPE_CHECKING, Literal

from kiro_crew.config.paths import config_dir
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.sandbox_leaves import _VOICE_RUNTIME_LEAF
from kiro_crew.sandbox_plan import path_within as _path_within

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


_voice_runtime_paths_lock = threading.Lock()
_voice_runtime_paths_cache: (
    tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None
) = None


def _ensure_voice_runtime_directory(path: str) -> None:
    """Create one gateway-owned runtime directory without following its leaf."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"voice runtime path is not a real directory: {path}")
    os.chmod(path, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only and the tightest traversable directory mode; Semgrep's suggested 0o644 would remove directory traversal and grant reads to other users.  # noqa: E501  # fmt: skip


def _literal_ancestor_guards(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return every rename-sensitive ancestor below the filesystem root."""
    guards: list[str] = []
    for item in paths:
        current = os.path.normpath(item)
        while True:
            parent = os.path.dirname(current)
            if parent == current:
                break
            if current not in guards:
                guards.append(current)
            current = parent
    return tuple(guards)


def prime_voice_runtime_sandbox_paths() -> str:
    """Cache and create the canonical agent-denied decoder runtime off-loop.

    ``config_dir()`` deliberately preserves a supported symlinked default data
    home. Seatbelt rules are path-based, so both that lexical spelling and the
    canonical target must be denied. Realpath resolution and directory creation
    happen here. Async agent startup reaches this through
    :func:`bind_voice_safe_agent_workspace_async`, which performs the work in a
    worker thread so gateway readiness is never gated on data-home filesystem IO.
    """
    global _voice_runtime_paths_cache

    lexical_home = os.path.normpath(str(config_dir()))
    cached = _voice_runtime_paths_cache
    if cached is not None and cached[0] == lexical_home:
        return cached[1]
    with _voice_runtime_paths_lock:
        cached = _voice_runtime_paths_cache
        if cached is not None and cached[0] == lexical_home:
            return cached[1]

        canonical_home = os.path.realpath(lexical_home)
        home_info = os.lstat(canonical_home)
        if not stat.S_ISDIR(home_info.st_mode) or stat.S_ISLNK(home_info.st_mode):
            raise OSError("Kiro Crew data home does not resolve to a real directory")

        canonical_run = os.path.join(canonical_home, "run")
        canonical_root = os.path.join(canonical_home, _VOICE_RUNTIME_LEAF)
        _ensure_voice_runtime_directory(canonical_run)
        _ensure_voice_runtime_directory(canonical_root)

        lexical_run = os.path.join(lexical_home, "run")
        lexical_root = os.path.join(lexical_home, _VOICE_RUNTIME_LEAF)
        roots = tuple(dict.fromkeys((lexical_root, canonical_root)))
        parents = tuple(dict.fromkeys((lexical_run, canonical_run)))
        guards = _literal_ancestor_guards(parents)
        _voice_runtime_paths_cache = (
            lexical_home,
            canonical_root,
            roots,
            parents,
            guards,
        )
        return canonical_root


def _restore_missing_voice_runtime_roots(runtime_paths: tuple[str, ...]) -> None:
    """Re-create a cached voice-runtime root that was deleted after priming.

    The prime cache is filled once, so a root removed later stays missing and
    every identity check on it fails with ENOENT. When a root the cache names
    has no directory entry, re-create the gateway-owned 0700 ``run`` and root
    directories the same way priming does. The guards that call this still
    check the re-created paths exactly as before and fail closed on any error.
    """
    cached = _voice_runtime_paths_cache
    if cached is None:
        return
    missing = False
    for path in runtime_paths:
        if path not in cached[2]:
            continue
        try:
            os.lstat(path)
        except FileNotFoundError:
            missing = True
            break
    if not missing:
        return
    canonical_root = cached[1]
    with _voice_runtime_paths_lock:
        _ensure_voice_runtime_directory(os.path.dirname(canonical_root))
        _ensure_voice_runtime_directory(canonical_root)


def _voice_runtime_sandbox_paths() -> tuple[str, ...]:
    """Return lexical and canonical snapshot roots, priming as a safe fallback."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[2]


def _voice_runtime_parent_paths() -> tuple[str, ...]:
    """Return runtime parents that agent processes may read but never write."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[3]


def _voice_runtime_ancestor_guards() -> tuple[str, ...]:
    """Return literal paths an agent must not rename around path-based rules."""
    prime_voice_runtime_sandbox_paths()
    assert _voice_runtime_paths_cache is not None
    return _voice_runtime_paths_cache[4]


def _path_identity(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for *path*, or ``None`` when it cannot be stat'd.

    THE seam every identity comparison below goes through, so a path that
    cannot be inspected (not created yet, an ancestor this process may not
    traverse, a race that unlinks mid-walk) degrades to the spelling answer
    instead of raising into the caller's spawn path.

    ``os.lstat`` keeps the identity walk on the component named by the
    spelling being checked. The canonical spelling was already resolved with
    ``realpath(strict=True)``, so following each component again would add no
    information and would make the lexical alias walk perform a second path
    resolution.
    """

    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _identity_within_sealed_parent(path: str, parent: str) -> bool:
    """Whether *path* is *parent* or lies inside it BY FILESYSTEM IDENTITY.

    Spelling alone misses an alias the filesystem itself treats as the same
    directory. On case-insensitive APFS ``<data home>/RUN`` and
    ``<data home>/run`` are ONE directory, and ``realpath`` does not fold the
    difference (it walks with ``lstat``/``readlink``, neither of which
    canonicalizes case) -- so a differently-cased declaration walks straight
    past the lexical predicate, which would let the probe honor it and hand the
    child a TMPDIR the seal has already made read-only. Identity is the
    filesystem's own answer, which covers firmlink and normalization aliases for
    free.

    A declared temp usually does NOT exist yet, so the walk simply climbs until
    a component stats: the deepest EXISTING ancestor is what carries identity,
    and if that ancestor is the sealed parent or lives under it then so does
    every not-yet-created component below it -- *path* arrives resolved and
    normalized, so no remainder can climb back out. Only the sealed parent's
    identity is compared, never a case-folded spelling, so a case-SENSITIVE
    filesystem keeps answering exactly as it did: there ``<data home>/RUN`` is a
    different directory, it does not exist, and nothing seals it.

    An unstat-able parent yields ``False`` rather than a folded-spelling guess.
    :func:`_voice_runtime_parent_paths` primes and CREATES the sealed parent, so
    that needs an out-of-band deletion race to reach -- and in that state
    case-folding would mis-refuse a genuinely distinct ``RUN`` directory on a
    case-sensitive filesystem, which is the worse answer: the OS seal is the
    actual boundary, and this predicate only turns its EROFS into a clean
    refusal.
    """
    parent_identity = _path_identity(parent)
    if parent_identity is None:
        return False
    current = path
    while True:
        if _path_identity(current) == parent_identity:
            return True
        next_up = os.path.dirname(current)
        if next_up == current:
            return False
        current = next_up


#: Temp keys a child's ``tempfile``/``mktemp`` consults, in
#: POSIX-then-Windows order. Spec keys are matched case-insensitively.
CANONICAL_TEMP_KEYS = ("TMPDIR", "TMP", "TEMP")


#: Operator-facing reason per refusal cause. ``check-failed`` belongs to the
#: shared env wrapper below; the path classifier itself returns only the first
#: two causes.
DECLARED_TEMP_REFUSAL_REASONS = {
    "sealed": ("it is inside the sandbox-sealed runtime parent, where the server " "cannot write"),
    "unclassifiable": (
        "it is relative and the daemon and child use different working directories, or its "
        "canonical form cannot be established (a symlink cycle or a component that cannot "
        "be traversed), so containment cannot be verified"
    ),
    "check-failed": "the seal check itself failed, so containment cannot be verified",
}


#: Why a spec-declared temp path is refused. ``sealed`` is a path inside
#: ``<data home>/run``; ``unclassifiable`` is a path whose canonical form cannot
#: be established (a symlink cycle, a component that cannot be traversed), so
#: containment cannot be verified.
DeclaredTempRefusal = Literal["sealed", "unclassifiable"]


def classify_declared_temp_path(path: str) -> "DeclaredTempRefusal | None":
    """Why a spec-declared temp *path* is refused, or ``None`` when it may be honored.

    The cause matters to the caller because only ``"sealed"`` describes a
    path inside ``<data home>/run``: an ``"unclassifiable"`` refusal is about a
    path whose canonical form could not be established, and a diagnostic that
    called it sealed would send an operator looking in the wrong place. Every
    cause gets the same response -- stop honoring the path.

    The question a caller must ask BEFORE it hands a sandboxed child a directory
    that came from config text. ``<data home>/run`` is sealed read-only
    on both backends, so a spec-declared ``TMPDIR`` under it silently gives the
    child a temp dir it cannot write -- and the write carve-out is not the answer:
    ``extra_writable_dirs`` is validated for SELF-DERIVED scratch paths only, so
    pointing it at spec text would hand untrusted config a write window under
    ``run``. A caller that gets a refusal cause must therefore stop honoring the
    path, not try to open it.

    Relative declarations are ``"unclassifiable"`` because the daemon and the
    spawned child do not share a required working directory. The classifier
    would resolve one against the daemon's cwd while ``spawn_backend`` runs the
    child under ``work_dir``. Refusing the spelling is the only way to keep one
    decision valid at every spawn boundary.

    Windows: nothing else to refuse, so ``None`` for every absolute path. Kiro Crew has no
    native Windows sandbox backend (see the delegation note in
    :func:`wrap_argv`): a probe child there is either not spawned at all or runs
    unsandboxed with a writable ``run``, so a declared temp under it is
    writable and is honored exactly as it was before this check existed. Gating
    here rather than resolving is also what keeps the check local -- on Windows
    ``realpath`` and ``stat`` OPEN the path, so classifying untrusted config
    text there would mean opening whatever it names.

    Both spellings are tested because the data home may be a supported symlink
    and path-based rules see each spelling independently. Spelling is only the
    fast answer: :func:`_identity_within_sealed_parent` then asks the filesystem
    itself, so a case, firmlink, or normalization alias of the seal cannot walk
    past this predicate.

    Blocking (``realpath``, the identity walk, plus the one-time priming of the
    runtime directories), so an async caller must reach it off the event loop.
    """
    if not path:
        return None
    if not os.path.isabs(path):
        return "unclassifiable"
    if sys.platform == "win32":
        return None
    # realpath resolves the ORIGINAL spelling, BEFORE any lexical pass. Order is
    # load-bearing: normalizing first collapses ``..`` lexically and so deletes
    # the very symlink that ``..`` was climbing out of, which would make a
    # ``<symlink-into-run>/../tmp`` declaration read as outside the seal while
    # the child's libc resolves symlink-first and lands back inside it. Both
    # spellings are still checked, since path-based sandbox rules
    # see the lexical one independently.
    #
    # Strict first, so the kernel's own loop detection answers for a symlink
    # cycle: the lenient form returns a cycle's link with the remainder appended
    # and never raises, and comparing that half-resolved spelling would honor a
    # declaration whose real location is unknown. A declared temp usually does
    # not exist yet, so a missing component is the ordinary case and falls back
    # to resolving as far as the path goes; every other failure (ELOOP, ENOTDIR,
    # EACCES) leaves the canonical form unestablished, which is refused like any
    # other unverifiable declaration.
    try:
        canonical = os.path.realpath(path, strict=True)
    except FileNotFoundError:
        canonical = os.path.realpath(path)
    except OSError:
        return "unclassifiable"
    lexical = os.path.normpath(os.path.abspath(path))
    spellings = tuple(dict.fromkeys((lexical, canonical)))
    parents = _voice_runtime_parent_paths()
    for spelling in spellings:
        for parent in parents:
            if _path_within(spelling, parent) or _identity_within_sealed_parent(spelling, parent):
                return "sealed"
    return None


def classify_declared_temp_env(
    env: "Mapping[str, object]",
    declared_temp_keys: "Sequence[str] | None" = None,
    *,
    classifier: "Callable[[str], DeclaredTempRefusal | None] | None" = None,
) -> tuple[tuple[str, ...], dict[str, tuple[str, str]], str]:
    """Classify the temp declaration in *env* once for every MCP spawn path.

    The first item is the accepted canonical key set in lookup order. The
    second maps each refused key to ``(declared path, cause)``. The third names
    a classifier failure for the WARNING. One refused key drops the whole temp
    declaration because ``tempfile`` may consult a sibling first.

    When *declared_temp_keys* is ``None``, every temp key in *env* is declared.
    A caller whose environment mixes operator and ambient values passes the
    operator-owned key names explicitly. Path inspection blocks, so async
    callers run this function through :func:`asyncio.to_thread`.
    """
    if declared_temp_keys is None:
        declared_upper = {
            key.upper()
            for key in env
            if isinstance(key, str) and key.upper() in CANONICAL_TEMP_KEYS
        }
    else:
        declared_upper = {
            key.upper()
            for key in declared_temp_keys
            if isinstance(key, str) and key.upper() in CANONICAL_TEMP_KEYS
        }
    accepted = tuple(key for key in CANONICAL_TEMP_KEYS if key in declared_upper)
    declared_values = {
        key.upper(): value
        for key, value in env.items()
        if isinstance(key, str) and key.upper() in declared_upper and isinstance(value, str)
    }
    if not declared_values:
        return accepted, {}, ""

    check = classifier or classify_declared_temp_path
    try:
        refused: dict[str, tuple[str, str]] = {
            key: (path, cause)
            for key, path in declared_values.items()
            if (cause := check(path)) is not None
        }
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        return (
            (),
            {key: (path, "check-failed") for key, path in declared_values.items()},
            failure,
        )
    if refused:
        return (), refused, ""
    return accepted, {}, ""


def _repr_declared_temp_log_value(
    value: str,
    redactor: "Callable[[str], str] | None",
) -> str:
    """Return one redacted, control-character-safe log field."""
    display = value
    if redactor is not None:
        try:
            display = redactor(value)
        except Exception:
            display = "<redaction failed>"
    return repr(display)


def declared_temp_refusal_reasons(
    refused: "Mapping[str, tuple[str, str]]",
    failure: str = "",
    *,
    redactor: "Callable[[str], str] | None" = None,
) -> list[str]:
    """Render one operator-facing reason per distinct refusal cause."""
    reasons: list[str] = []
    for _key, (_path, cause) in sorted(refused.items()):
        reason = DECLARED_TEMP_REFUSAL_REASONS[cause]
        if cause == "check-failed" and failure:
            reason = f"{reason} ({_repr_declared_temp_log_value(failure, redactor)})"
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def format_declared_temp_refusals(
    refused: "Mapping[str, tuple[str, str]]",
    *,
    hidden_keys: "Sequence[str]" = (),
    redactor: "Callable[[str], str] | None" = None,
) -> str:
    """Render refused key/path fields as one credential-safe log line.

    ``repr`` keeps control characters inside the field instead of letting a
    declared path forge another log record. Keys in *hidden_keys* came from a
    resolved ``secret://`` value, so their path is never rendered at all.
    """
    hidden_upper = {key.upper() for key in hidden_keys}
    fields: list[str] = []
    for key, (path, _cause) in sorted(refused.items()):
        if key.upper() in hidden_upper:
            display = repr("<resolved secret>")
        else:
            display = _repr_declared_temp_log_value(path, redactor)
        fields.append(f"{key}={display}")
    return ", ".join(fields)


_VOICE_GUARD_REMEDY = "Pick a project subdirectory that does not contain the Kiro Crew data home."

_VoiceGuardRelationship = Literal["contains", "inside", "alias", "cannot-verify"]


def _voice_runtime_guard_message(
    workspace_path: str,
    runtime_path: str,
    relationship: _VoiceGuardRelationship,
    failed_path: str | None = None,
    failure_reason: str | None = None,
) -> str:
    """Build every variant of the voice-runtime workspace refusal.

    Each variant leads with the two concrete absolute paths, keeps its own
    distinguishing detail, and ends with the same remedy sentence, so a user
    who picked ``~`` (an ancestor of the default ``~/.kiro/crew`` data home)
    sees exactly which two paths collide and what to choose instead. The
    message is operator-facing and may reach logs: it carries only the two
    paths the caller already knows (plus, on the cannot-verify variant, the
    path whose filesystem check failed).
    """
    if relationship == "contains":
        return (
            f"macOS agent workspace {workspace_path!r} overlaps Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: the workspace contains "
            f"the voice runtime / data home. {_VOICE_GUARD_REMEDY}"
        )
    if relationship == "inside":
        return (
            f"macOS agent workspace {workspace_path!r} overlaps Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: the workspace is the "
            f"voice runtime / data home or lives inside it. {_VOICE_GUARD_REMEDY}"
        )
    if relationship == "alias":
        return (
            f"macOS agent workspace {workspace_path!r} aliases Kiro Crew's "
            f"protected voice runtime {runtime_path!r}: by filesystem identity "
            "(a case, normalization, symlink, or firmlink alias) one of these "
            f"paths is the other or an ancestor of the other. {_VOICE_GUARD_REMEDY}"
        )
    return (
        f"cannot verify that macOS agent workspace {workspace_path!r} is "
        f"separate from Kiro Crew's protected voice runtime {runtime_path!r}: "
        f"a filesystem check failed on {failed_path!r} ({failure_reason}), "
        "so the guard cannot prove the paths are disjoint and fails closed "
        f"rather than start an agent it cannot isolate. {_VOICE_GUARD_REMEDY}"
    )


def _lexical_runtime_overlap(
    workspace_paths: tuple[str, ...], runtime_paths: tuple[str, ...]
) -> tuple[str, str, _VoiceGuardRelationship] | None:
    """First lexical containment hit between workspace and runtime spellings.

    THE shared containment scan: :func:`voice_runtime_workspace_conflict` (the
    non-raising pre-flight) and :func:`assert_voice_runtime_outside_agent_workspace`
    (the fail-closed spawn guard) both call this, so the pre-flight cannot
    silently drift from the guard it mirrors.

    Returns ``(workspace_path_to_name, runtime_path, relationship)`` for the
    first hit, or ``None``. Naming convention is the guard's: refusals always
    name the workspace as the caller spelled it (``workspace_paths[0]``); a
    hit found only on a non-original spelling (the realpath of a symlinked
    workspace) is an ``"alias"`` relationship — formatting the resolved path
    instead would print the runtime path twice and omit the path the user
    actually configured.
    """
    original = workspace_paths[0]
    for workspace_path in workspace_paths:
        for runtime_path in runtime_paths:
            try:
                common = os.path.commonpath((workspace_path, runtime_path))
            except ValueError:
                continue
            if common not in (workspace_path, runtime_path):
                continue
            if workspace_path != original:
                return (original, runtime_path, "alias")
            return (
                original,
                runtime_path,
                "inside" if common == runtime_path else "contains",
            )
    return None


def voice_runtime_workspace_conflict(workspace: str | os.PathLike[str]) -> str | None:
    """Pre-flight: describe why *workspace* would be rejected, or ``None``.

    A non-raising lexical version of
    :func:`assert_voice_runtime_outside_agent_workspace` for validation
    surfaces (the project endpoint, pickers) that want to warn BEFORE a
    session exists. Lexical containment only — the descriptor/identity walks
    stay in the spawn-time guards, which remain authoritative; a ``None`` here
    is a pre-flight pass, not a security verdict. Darwin-gated to MATCH the
    guards it pre-flights: every spawn-time guard early-returns off macOS, so
    a workspace that overlaps the data home spawns fine on Linux/Windows
    today — refusing it here would remove a working configuration to prevent
    a macOS-only harm (and with macOS-worded copy).

    Messages come from :func:`_voice_runtime_guard_message` and the
    containment scan is shared with the spawn-time guard
    (:func:`_lexical_runtime_overlap`), so the pre-flight warning and the
    spawn-time refusal read identically and cannot drift apart.
    """
    if sys.platform != "darwin":
        return None
    workspace_paths = tuple(
        dict.fromkeys(
            (
                os.path.abspath(os.fspath(workspace)),
                os.path.realpath(os.fspath(workspace)),
            )
        )
    )
    try:
        runtime_paths = tuple(
            dict.fromkeys(os.path.abspath(path) for path in _voice_runtime_sandbox_paths())
        )
    except OSError:
        # Pre-flight only: if the runtime paths cannot be resolved here, let
        # the spawn-time guard (which fails closed) produce the verdict.
        return None
    hit = _lexical_runtime_overlap(workspace_paths, runtime_paths)
    if hit is not None:
        return _voice_runtime_guard_message(*hit)
    return None


def assert_voice_runtime_outside_agent_workspace(workspace: str | os.PathLike[str]) -> None:
    """Fail closed when a macOS agent workspace can reach decoder snapshots.

    Kiro's internal macOS sandbox cannot nest inside Kiro Crew's Seatbelt
    profile, so delegated Kiro agents do not inherit our voice-runtime deny
    rules. A workspace that is the voice root, lives below it, or contains it
    would therefore let a same-UID agent replace a verified named Mach-O image
    before ``posix_spawn`` opens it. Check both lexical and canonical spellings
    before either ACP agent path delegates isolation to Kiro.
    """
    if sys.platform != "darwin":
        return

    def _identity_in_ancestor_chain(identity: tuple[int, int], path: str) -> bool:
        current = os.path.abspath(path)
        while True:
            info = os.stat(current)
            if (info.st_dev, info.st_ino) == identity:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    raw_workspace_paths = tuple(
        dict.fromkeys(
            (
                os.path.abspath(os.fspath(workspace)),
                os.path.realpath(os.fspath(workspace)),
            )
        )
    )
    raw_runtime_paths = tuple(
        dict.fromkeys(os.path.abspath(path) for path in _voice_runtime_sandbox_paths())
    )
    # Refusals always name the workspace as the caller spelled it. A hit found
    # only on the canonical (realpath) spelling of a symlinked workspace is an
    # alias relationship from the caller's own spelling -- formatting the
    # resolved path instead would print the runtime path twice and omit the
    # path the user actually configured. The scan itself is shared with the
    # non-raising pre-flight (_lexical_runtime_overlap), so the two surfaces
    # cannot drift apart.
    original_workspace_path = raw_workspace_paths[0]
    lexical_hit = _lexical_runtime_overlap(raw_workspace_paths, raw_runtime_paths)
    if lexical_hit is not None:
        raise RuntimeError(_voice_runtime_guard_message(*lexical_hit))

    # Path spelling is only a fast reject. Compare filesystem identities too,
    # walking both ancestor directions so case, normalization, symlink, and
    # firmlink aliases on an existing APFS workspace cannot evade the guard.
    try:
        _restore_missing_voice_runtime_roots(raw_runtime_paths)
        workspace_identities = tuple(
            (info.st_dev, info.st_ino) for info in (os.stat(path) for path in raw_workspace_paths)
        )
        runtime_identities = tuple(
            (info.st_dev, info.st_ino) for info in (os.stat(path) for path in raw_runtime_paths)
        )
        for workspace_identity in workspace_identities:
            for runtime_path in raw_runtime_paths:
                if _identity_in_ancestor_chain(workspace_identity, runtime_path):
                    raise RuntimeError(
                        _voice_runtime_guard_message(original_workspace_path, runtime_path, "alias")
                    )
        for runtime_path, runtime_identity in zip(raw_runtime_paths, runtime_identities):
            for workspace_path in raw_workspace_paths:
                if _identity_in_ancestor_chain(runtime_identity, workspace_path):
                    raise RuntimeError(
                        _voice_runtime_guard_message(original_workspace_path, runtime_path, "alias")
                    )
    except OSError as exc:
        raise RuntimeError(
            _voice_runtime_guard_message(
                raw_workspace_paths[0],
                raw_runtime_paths[0] if raw_runtime_paths else "<unknown>",
                "cannot-verify",
                failed_path=getattr(exc, "filename", None) or "<unknown path>",
                failure_reason=getattr(exc, "strerror", None) or str(exc),
            )
        ) from exc


def _open_directory_descriptor(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
    """Open a directory identity without making its descriptor inheritable."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(os.fspath(path), flags, dir_fd=dir_fd)


def _directory_ancestor_identities(descriptor: int) -> tuple[tuple[int, int], ...]:
    """Walk directory ancestors by descriptor, immune to pathname retargeting."""
    current = os.dup(descriptor)
    identities: list[tuple[int, int]] = []
    try:
        while True:
            current_info = os.fstat(current)
            current_identity = (current_info.st_dev, current_info.st_ino)
            identities.append(current_identity)
            parent = _open_directory_descriptor("..", dir_fd=current)
            try:
                parent_info = os.fstat(parent)
            except OSError:
                os.close(parent)
                raise
            parent_identity = (parent_info.st_dev, parent_info.st_ino)
            if parent_identity == current_identity:
                os.close(parent)
                break
            os.close(current)
            current = parent
        return tuple(identities)
    finally:
        os.close(current)


def bind_voice_safe_agent_workspace(
    workspace: str | os.PathLike[str],
) -> tuple[str, int | None]:
    """Bind a verified macOS workspace identity for delegated Kiro startup.

    A pathname-only overlap check has an unavoidable check/use window: another
    sandboxed process can retarget a workspace symlink after ``stat`` and before
    Kiro initializes its own sandbox. On macOS, open the workspace first and
    compare directory ancestry entirely through descriptors.

    The descriptor is returned ALONGSIDE the pathname, never baked into it. The
    child enters it with ``fchdir`` (see ``create_subprocess_limited``'s
    ``chdir_fd``), so nothing re-resolves the name. Handing the spawn a
    ``cwd="/dev/fd/<n>"`` pathname instead does not work: only Linux publishes
    those entries as symlinks to the target, and on macOS -- the only platform
    that binds here at all -- ``chdir()`` on one is refused (``EACCES`` on one
    reporting host, ``ENOTDIR`` on macOS 26), which is every delegated spawn on a
    packaged build.

    The returned descriptor must stay open as long as the caller re-verifies the
    binding through :func:`bound_agent_workspace_target`. The child's copy is
    independent, so closing this one does not disturb a running agent.

    Other platforms keep their original pathname and do not inherit a descriptor.
    """
    workspace_path = os.fspath(workspace)
    if sys.platform != "darwin":
        return workspace_path, None

    workspace_fd = -1
    runtime_fds: list[int] = []
    runtime_paths: tuple[str, ...] = ()
    try:
        # Resolve the runtime paths before opening the workspace: a workspace
        # open() failure lands in the OSError handler below, which names the
        # colliding runtime path in its refusal -- resolving after the open
        # would print "<unknown>" for exactly the failure a user hits first.
        runtime_paths = _voice_runtime_sandbox_paths()
        _restore_missing_voice_runtime_roots(runtime_paths)

        workspace_fd = _open_directory_descriptor(workspace_path)
        workspace_identity = os.fstat(workspace_fd)
        workspace_id = (workspace_identity.st_dev, workspace_identity.st_ino)
        workspace_ancestors = set(_directory_ancestor_identities(workspace_fd))

        for runtime_path in runtime_paths:
            runtime_fd = _open_directory_descriptor(runtime_path)
            runtime_fds.append(runtime_fd)
            runtime_identity = os.fstat(runtime_fd)
            runtime_id = (runtime_identity.st_dev, runtime_identity.st_ino)
            runtime_ancestors = set(_directory_ancestor_identities(runtime_fd))
            if workspace_id in runtime_ancestors or runtime_id in workspace_ancestors:
                raise RuntimeError(
                    _voice_runtime_guard_message(
                        os.path.abspath(workspace_path),
                        os.path.abspath(runtime_path),
                        "inside" if runtime_id in workspace_ancestors else "contains",
                    )
                )

        return workspace_path, workspace_fd
    except OSError as exc:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        raise RuntimeError(
            _voice_runtime_guard_message(
                os.path.abspath(workspace_path),
                os.path.abspath(runtime_paths[0]) if runtime_paths else "<unknown>",
                "cannot-verify",
                failed_path=getattr(exc, "filename", None) or "<unknown path>",
                failure_reason=getattr(exc, "strerror", None) or str(exc),
            )
        ) from exc
    except BaseException:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        raise
    finally:
        for runtime_fd in runtime_fds:
            os.close(runtime_fd)


def _close_bound_agent_workspace(descriptor: int) -> None:
    """Close a workspace descriptor, swallowing an already-closed race."""
    try:
        os.close(descriptor)
    except OSError:
        pass


async def release_bound_agent_workspace(descriptor: int) -> None:
    """Close a bound workspace descriptor off-loop before honoring cancellation."""
    closing = asyncio.create_task(asyncio.to_thread(_close_bound_agent_workspace, descriptor))
    cancellation: asyncio.CancelledError | None = None
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError as exc:
            # A descriptor is a process-lifetime resource.  A second cancellation
            # must not detach the worker that owns its close and leak it until the
            # gateway exits, so settle the tiny close before propagating cancel.
            cancellation = exc
    closing.result()
    if cancellation is not None:
        raise cancellation


async def bind_voice_safe_agent_workspace_async(
    workspace: str | os.PathLike[str],
) -> tuple[str, int | None]:
    """Cancellation-safe off-loop wrapper for workspace identity binding.

    ``asyncio.to_thread`` cannot stop a running worker.  If its awaiter is
    cancelled after the worker opens the descriptor but before ownership is
    transferred, a plain await loses the returned fd.  Shield and settle the
    worker; on cancellation, close any descriptor it produced before re-raising.
    """
    binding = asyncio.create_task(asyncio.to_thread(bind_voice_safe_agent_workspace, workspace))
    cancellation: asyncio.CancelledError | None = None
    while not binding.done():
        try:
            await asyncio.shield(binding)
        except asyncio.CancelledError as exc:
            cancellation = exc

    if cancellation is None:
        return binding.result()

    try:
        _path, descriptor = binding.result()
    except BaseException:
        # The caller's cancellation remains authoritative, but retrieving the
        # worker exception prevents a false "Task exception was never retrieved".
        raise cancellation
    if descriptor is not None:
        try:
            await release_bound_agent_workspace(descriptor)
        except asyncio.CancelledError as exc:
            cancellation = exc
    raise cancellation


def _bound_agent_workspace_matches(descriptor: int, workspace: str | os.PathLike[str]) -> bool:
    """Whether *workspace* currently names an already-bound directory identity.

    The caller uses the bound identity after this comparison, never the supplied
    pathname, so a subsequent symlink retarget cannot change what is authorized.
    """
    candidate = _open_directory_descriptor(workspace)
    try:
        expected = os.fstat(descriptor)
        actual = os.fstat(candidate)
        return (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
    finally:
        os.close(candidate)


def bound_agent_workspace_target(descriptor: int, workspace: str | os.PathLike[str]) -> str | None:
    """The bound directory's OWN pathname, or None when *workspace* is not it.

    The identity check and the name read are one call because a caller needs both
    in the same worker hop, and because returning the caller's own pathname would
    defeat the check: that string is exactly what a same-UID retarget controls,
    and a peer handed it re-resolves it after this returns.

    What comes back is the kernel's name for the descriptor that was verified
    (``/proc/self/fd`` on Linux, ``F_GETPATH`` on macOS), so it carries no symlink
    component left to swap and it cannot name a descendant the check never covered.

    It does NOT make a peer's own resolution descriptor-bound, and nothing can: a
    pathname handed to another process is re-resolved by that process, and macOS has
    no descriptor-addressable path namespace to hand instead (``/dev/fd/<n>`` is
    exactly what it cannot resolve). A same-UID rename of the canonical directory
    between this call and that resolution therefore stays open. Callers that own the
    child's cwd should pin it with ``create_subprocess_limited``'s ``chdir_fd``,
    which does not go through a name at all; this is for the ACP ``session/new`` cwd,
    where a string is the only thing the protocol carries.

    Raises OSError when this platform exposes no way to ask, so the caller fails
    closed instead of falling back to the mutable spelling.
    """
    if not _bound_agent_workspace_matches(descriptor, workspace):
        return None
    resolved = fd_real_path(descriptor)
    if resolved is None:
        raise OSError(
            errno.ENOSYS,
            "cannot read a bound workspace descriptor's own path on this platform",
        )
    return resolved


class BoundWorkspaceMismatch(Exception):
    """A requested session workspace is not the bound directory identity."""


# Named by the path callers import it from: an error chain renders each raised class
# as ``module.qualname`` (``subagent._describe_exception``), and that text reaches the
# Subagents panel and tracebacks, so this module's name must not leak into it.
BoundWorkspaceMismatch.__module__ = "kiro_crew.sandbox"


async def resolve_bound_session_workspace(
    descriptor: int, workspace: str | os.PathLike[str]
) -> str:
    """Off-loop verify-then-substitute for an ACP session cwd on a bound runtime.

    Both ACP front ends enforce one rule -- prove the requested path still names the
    bound identity, then hand the peer the DESCRIPTOR's own name instead of the
    caller's spelling -- so the rule lives here once rather than in two places that
    can drift apart. Each caller keeps only the mapping to its own error type:
    :class:`BoundWorkspaceMismatch` when the path is not the bound identity, OSError
    when the binding cannot be verified at all.

    Off-loop because it opens a directory and reads a descriptor's name; on the loop
    that is filesystem IO in front of every session start.
    """
    resolved = await asyncio.to_thread(bound_agent_workspace_target, descriptor, workspace)
    if resolved is None:
        raise BoundWorkspaceMismatch(os.fspath(workspace))
    return resolved

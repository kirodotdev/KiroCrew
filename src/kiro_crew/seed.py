"""``kirocrew seed`` — copy a hand-authored fixture into ``$KIROCREW_HOME``.

Phase 1.A (walking skeleton): one subcommand, one fixture (``empty``), one guardrail
(``KIROCREW_HOME`` must be set). Safety guardrails and ``--seed-replace`` land in 1.B.

Fixtures ship as package data at ``src/kiro_crew/tests_fixtures/<name>/``.
Each fixture is a valid ``KIROCREW_HOME`` tree with a ``fixture.yaml``
declaring ``schema-version``. Shipping inside the package is what lets
``kirocrew seed`` work after ``pip`` installs the wheel into
``site-packages/kiro_crew/`` (a source-tree-relative path walk would
not find ``<repo>/test/fixtures/`` from there).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

try:  # py3.9+ stdlib; kept in a try so older runtimes raise cleanly.
    from importlib.resources import files as _resource_files
except ImportError:  # pragma: no cover — KiroCrew targets py3.10.
    _resource_files = None  # type: ignore[assignment]

# ``kiro_crew.sel`` is a first-party module shipped in the same package, so
# we import it at top level like every other caller (``subagent.py``,
# ``taskrunner.py``, ``dashboard/stt_stream.py``, ...). An ImportError here
# would mean the KiroCrew install itself is broken — there's no scenario
# where it's optional. ``_safe_audit`` still handles *runtime* SEL failures
# (read-only ``$HOME``, HMAC-key write failure) via its broad except.
from kiro_crew.config.paths import _default_home, _legacy_home
from kiro_crew.sel import sel

# Exit codes — pinned in PRD acceptance. Only 0 and 2 are reachable in 1.A;
# 1 (I/O error) lands with the non-empty guardrail in 1.B.
EXIT_OK = 0
EXIT_IO_ERROR = 1
EXIT_GUARDRAIL = 2

# Name of the package-data directory that holds the shipped fixtures.
# Kept as a constant so 1.C's ``seeded_home`` helper reuses the same name.
_FIXTURES_PKG = "tests_fixtures"


class SeedError(Exception):
    """Guardrail violation or lookup failure. ``code`` is the intended exit code.

    ``guardrail`` is a short code-controlled discriminator used in SEL audit
    events (see ``_safe_audit`` in ``seed_cmd``). Keeping it separate from
    the human-readable message keeps the audit stream path-free AND
    SOC-meaningful — ``type(exc).__name__`` would always be ``"SeedError"``
    and the message contains resolved filesystem paths we must NOT leak.
    """

    # Guardrail constants. Used in audit ``resources=... reason=<guardrail>`` strings,
    # so any change here has an audit-schema impact — prefer additive.
    GUARDRAIL_UNSET_HOME = "unset_home"
    GUARDRAIL_MAIN_HOME = "main_home"
    GUARDRAIL_NON_EMPTY = "non_empty"
    GUARDRAIL_SYMLINK_REPLACE = "symlink_replace"
    GUARDRAIL_SYMLINK_EMPTY = "symlink_empty"
    GUARDRAIL_DANGLING_SYMLINK = "dangling_symlink"
    GUARDRAIL_NOT_A_DIRECTORY = "not_a_directory"
    GUARDRAIL_BAD_NAME = "bad_name"
    GUARDRAIL_UNKNOWN_FIXTURE = "unknown_fixture"
    GUARDRAIL_ROOT_ESCAPE = "root_escape"
    GUARDRAIL_RESOLVE_FAILED = "resolve_failed"

    def __init__(
        self,
        message: str,
        *,
        code: int = EXIT_GUARDRAIL,
        guardrail: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.guardrail = guardrail


def _fixtures_root() -> Path:
    """Locate the checked-in fixtures tree.

    Uses ``importlib.resources`` so the path works both in source-checkout
    workspaces (``src/kiro_crew/tests_fixtures/``) and in the pip-installed
    layout where the package lives under
    ``site-packages/kiro_crew/``. ``files()`` returns a ``Traversable``
    which, for the regular filesystem loader KiroCrew uses, is a real
    ``PosixPath`` — no ``as_file`` context manager gymnastics needed.
    """
    if _resource_files is None:  # pragma: no cover — see import guard above.
        raise SeedError(
            "importlib.resources unavailable — need Python >= 3.9",
            guardrail=SeedError.GUARDRAIL_RESOLVE_FAILED,
        )
    return Path(str(_resource_files("kiro_crew") / _FIXTURES_PKG))


def _resolve_fixture(name: str) -> Path:
    """Return the path to fixture ``name``, or raise ``SeedError``.

    The full "list valid names" error message lands in 1.B; 1.A just names
    the missing fixture.

    Rejects path-traversal attempts (``--fixture ../../.ssh``) before
    ``shutil.copytree`` ever sees the path. Multiple gates are needed because
    each alone is bypassable:

    - Empty or ``.``-only names resolve to the fixtures root itself — would
      copy the whole ``tests_fixtures/`` tree into ``$KIROCREW_HOME``.
    - Path-separator / ``..`` character checks catch the obvious traversal
      cases (``../../.ssh``, ``foo/bar``).
    - A final ``relative_to`` containment check catches symlinks inside the
      fixtures tree that resolve outside it (e.g.
      ``tests_fixtures/foo -> ../../.ssh``).
    - A post-resolve ``candidate != root`` check catches the edge case
      where all character-level gates pass but the resolved path still
      picks the root itself — reachable only through a self-symlink inside
      ``tests_fixtures/`` (e.g. ``tests_fixtures/self -> .``); strings
      like ``"./"`` are caught earlier at the empty-or-root gate.
    - NUL-byte / control-char rejection at the empty-or-root gate: a name
      like ``"foo\x00bar"`` would otherwise let ``(root / name).resolve()``
      raise a bare ``ValueError``, bypassing both the plain-ASCII
      ``seed: error:`` contract AND the SEL audit event in ``seed_cmd``.
    """
    if name in ("", ".", "./") or any(ord(c) < 0x20 for c in name):
        raise SeedError(
            f"fixture name is empty or refers to the root: {name!r}",
            guardrail=SeedError.GUARDRAIL_BAD_NAME,
        )
    if "/" in name or "\\" in name or ".." in Path(name).parts:
        raise SeedError(
            f"fixture name has path separators or '..': {name!r}",
            guardrail=SeedError.GUARDRAIL_BAD_NAME,
        )
    root = _fixtures_root()
    candidate = (root / name).resolve()
    if not candidate.is_dir():
        # List the available fixtures so the user doesn't have to go read
        # ``src/kiro_crew/tests_fixtures/`` or the PRD. Sorted for stable
        # test assertions and so ``empty`` / ``minimal`` / ``rich`` land
        # in the obvious order as 1.C / 1.D ship them.
        available = sorted(
            p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
        available_str = ", ".join(available) if available else "(none)"
        raise SeedError(
            f"unknown fixture: {name!r}. Available fixtures: {available_str}.",
            guardrail=SeedError.GUARDRAIL_UNKNOWN_FIXTURE,
        )
    resolved_root = root.resolve()
    if candidate == resolved_root:
        raise SeedError(
            f"fixture name resolves to fixtures root: {name!r}",
            guardrail=SeedError.GUARDRAIL_BAD_NAME,
        )
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SeedError(
            f"fixture name escapes fixtures root: {name!r}",
            guardrail=SeedError.GUARDRAIL_ROOT_ESCAPE,
        ) from exc
    return candidate


def _protected_homes() -> set[Path]:
    """Return the resolved gateway homes we refuse to seed into, ever.

    These are the paths that could hold a dev's LIVE gateway state, so seeding
    into them — even with ``--seed-replace`` — is the single most destructive
    outcome this tool could produce and is always refused:

    * ``_default_home()`` → ``~/.kiro/crew`` — the current main gateway home.
    * ``_legacy_home()`` → ``~/.kirocrew`` — the pre-move home. A box that
      hasn't migrated yet (or was rolled back) still keeps its real data here,
      so it must stay protected until the user removes it themselves.

    Each is ``.resolve()``-d to collapse symlinks along the way.

    Uses ``_default_home()`` / ``_legacy_home()`` rather than ``config_dir()`` on
    purpose: seeding requires ``$KIROCREW_HOME`` to be set, so ``config_dir()``
    would return that override itself and the guardrail equality check would
    always fire. The guardrail must compare against the DEFAULT (non-override)
    homes. Extracted as a helper so tests can monkeypatch ``Path.home()`` via
    ``$HOME`` and exercise the guardrail on synthetic default-home paths.
    """
    homes: set[Path] = set()
    for home in (_default_home(), _legacy_home()):
        try:
            homes.add(home.resolve())
        except OSError:
            homes.add(home)
    return homes


def _resolve_target(*, for_main_home_check: bool = False) -> Path:
    """Return ``$KIROCREW_HOME`` as a ``Path``, or raise ``SeedError``.

    ``expanduser()`` is applied so ``KIROCREW_HOME=~/dev`` works. Pass
    ``for_main_home_check=True`` to additionally ``resolve()`` the path so
    a symlinked ``KIROCREW_HOME`` pointing at ``~/.kiro/crew`` is caught by
    the main-home guardrail. The unresolved form is used for ``copytree`` /
    ``rmtree`` because a non-existent target is valid input to those.
    """
    raw = os.environ.get("KIROCREW_HOME")
    if not raw:
        raise SeedError(
            "$KIROCREW_HOME is not set. Point it at a dev directory "
            "(e.g. KIROCREW_HOME=~/.kirocrew-dev kirocrew gateway --seed empty).",
            guardrail=SeedError.GUARDRAIL_UNSET_HOME,
        )
    target = Path(raw).expanduser()
    if for_main_home_check:
        # ``resolve(strict=False)`` tolerates non-existent targets while
        # still collapsing any symlinks that DO exist along the way. That
        # catches ``$KIROCREW_HOME -> ~/.kiro/crew`` even when the symlink
        # target doesn't exist yet on some platforms.
        #
        # On resolution failure (broken symlink chain, permission error,
        # exotic cross-mount issues) we MUST fail closed — falling back
        # to the unresolved path would silently bypass the main-home
        # guardrail: if the unresolved path is actually a symlink to
        # ``~/.kiro/crew`` that ``resolve()`` couldn't evaluate, the
        # guardrail comparison won't match, and ``--seed-replace`` would
        # then ``rmtree`` the dev's live gateway home. That's the one
        # outcome this tool must never produce.
        try:
            target = target.resolve(strict=False)
        except OSError as exc:
            raise SeedError(
                f"cannot resolve $KIROCREW_HOME ({raw!r}) for main-home "
                f"safety check — refusing to proceed: {exc}",
                guardrail=SeedError.GUARDRAIL_RESOLVE_FAILED,
            ) from exc
    return target


def seed(fixture_name: str, *, replace: bool = False) -> None:
    """Copy fixture ``fixture_name`` into ``$KIROCREW_HOME``.

    Raises ``SeedError`` on guardrail violations. Enforces, in order:

    1. **Main-home guardrail** — refuses when ``$KIROCREW_HOME`` resolves to
       ``~/.kiro/crew`` (the dev's live gateway home). This guardrail is
       ABSOLUTE: ``replace=True`` does NOT override it. Clobbering the
       main gateway is the one outcome we never want to enable.
    2. **Non-empty guardrail** — refuses when the target exists and contains
       anything, unless ``replace=True``. With ``replace=True``, the
       shutil.rmtree the target first, then copytree. The sequence is not
       atomic — documented in this docstring — but that's acceptable for a
       dev tool. The caller can re-run with ``--seed-replace`` to clean up.

    ``symlinks`` is left at the ``shutil.copytree`` default (``False``) so
    symlinks inside fixtures are followed. We don't ship any fixtures with
    symlinks today; revisit in 1.C if ``minimal`` / ``rich`` add one.
    """
    src = _resolve_fixture(fixture_name)
    # ``_resolve_target`` is called twice deliberately: first with
    # ``for_main_home_check=True`` to apply ``resolve()`` (symlink-collapsed
    # form, used ONLY for the main-home equality check), then again without
    # the flag to get the unresolved user-provided path for the actual
    # ``copytree`` / ``rmtree`` work. ``resolve()`` would rewrite a symlinked
    # ``$KIROCREW_HOME`` into its target and we'd copy/rmtree the wrong
    # location; conversely, the raw path can't be trusted for the main-home
    # guardrail because ``KIROCREW_HOME=~/my-link`` where ``my-link -> ~/.kiro/crew``
    # would bypass the string compare. Two calls keeps each value's purpose
    # explicit at the cost of one extra ``os.environ`` lookup — acceptable.
    dst_resolved = _resolve_target(for_main_home_check=True)
    if dst_resolved in _protected_homes():
        raise SeedError(
            f"refusing to seed main gateway home: {dst_resolved}. "
            "Point $KIROCREW_HOME at a separate dev directory "
            "(e.g. ~/.kirocrew-dev).",
            guardrail=SeedError.GUARDRAIL_MAIN_HOME,
        )
    dst = _resolve_target()

    # Dangling symlink: ``dst.exists()`` follows symlinks, so a symlink
    # whose target has been deleted (or never existed) returns ``False``.
    # That means it bypasses every ``exists()``-gated branch below AND
    # falls straight into ``shutil.copytree(src, dst)``, which raises a
    # raw ``FileExistsError`` because the symlink path entry itself is
    # still on the filesystem. ``is_symlink()`` does NOT follow the link,
    # so ``is_symlink() and not exists()`` is the only way to detect a
    # dangling link. Keep this guard FIRST among the symlink / not-a-dir
    # checks — both the populated-symlink and empty-symlink guardrails below
    # rely on ``exists()`` returning ``True``, so they don't fire here.
    if dst.is_symlink() and not dst.exists():
        raise SeedError(
            f"$KIROCREW_HOME is a dangling symlink: {dst}. "
            "Point it at a real directory (or remove the link so seed "
            "can create a fresh directory).",
            guardrail=SeedError.GUARDRAIL_DANGLING_SYMLINK,
        )

    # ``$KIROCREW_HOME`` pointing at a regular file (or a symlink to one) would
    # reach ``dst.iterdir()`` below and raise a raw ``NotADirectoryError`` —
    # EXIT_IO_ERROR with a cryptic message. Refuse up front with a guardrail so the
    # user gets the plain ``seed: error:`` prefix and the SEL stream records
    # ``guardrail=not_a_directory`` instead of a raw OS error type. ``is_dir()``
    # follows symlinks, so ``exists() and not is_dir()`` catches both regular
    # files AND symlink-to-file; symlink-to-empty-dir and symlink-to-populated-dir
    # still flow through the non-empty branch below (which has its own
    # symlink-specific guardrails). Keep this ahead of ``iterdir()``.
    if dst.exists() and not dst.is_dir():
        raise SeedError(
            f"$KIROCREW_HOME is not a directory: {dst}. "
            "Point it at a directory (will be created if absent) or a dev dir.",
            guardrail=SeedError.GUARDRAIL_NOT_A_DIRECTORY,
        )

    if dst.exists() and any(dst.iterdir()):
        # Symlinked non-empty target: refuse regardless of ``replace``. The
        # downstream ``rmtree`` would follow the link and delete the link
        # target; even the ``--replace``-less path leads the user into a
        # dead end — ``GUARDRAIL_NON_EMPTY`` would tell them to pass
        # ``--seed-replace``, then ``GUARDRAIL_SYMLINK_REPLACE`` would
        # refuse it. Short-circuit with the actionable message on the
        # first attempt. Uses ``GUARDRAIL_SYMLINK_REPLACE`` since the
        # populated-symlink case is the "rmtree would follow the link"
        # risk the name calls out; ``GUARDRAIL_SYMLINK_EMPTY`` distinguishes
        # the empty-symlink branch below.
        if dst.is_symlink():
            raise SeedError(
                f"refusing to seed into a symlinked $KIROCREW_HOME: {dst}. "
                "Point it at a real directory.",
                guardrail=SeedError.GUARDRAIL_SYMLINK_REPLACE,
            )
        if not replace:
            raise SeedError(
                f"$KIROCREW_HOME is not empty: {dst}. "
                "Pass --seed-replace to wipe it and re-seed.",
                guardrail=SeedError.GUARDRAIL_NON_EMPTY,
            )
        shutil.rmtree(dst)
    elif dst.exists() and dst.is_symlink():
        # Empty-but-existing symlink: ``shutil.copytree(src, dst)`` would
        # raise ``FileExistsError`` with a raw message because the symlink
        # itself exists as a path. Previously this fell through to the
        # ``OSError`` branch in ``seed_cmd`` and landed as EXIT_IO_ERROR
        # with ``reason=FileExistsError`` in the audit — technically
        # correct but the user got a cryptic error. Refuse explicitly with
        # a symlink-empty guardrail so the same symlink-is-dangerous message
        # pattern applies whether the link target is empty or populated.
        raise SeedError(
            f"refusing to seed into a symlinked $KIROCREW_HOME: {dst}. "
            "Point it at a real directory.",
            guardrail=SeedError.GUARDRAIL_SYMLINK_EMPTY,
        )
    elif dst.exists():
        # Empty-but-existing real dir — PRD accepts this (only non-empty needs
        # --seed-replace). ``shutil.copytree`` refuses ANY pre-existing dst, so
        # we rmdir first.
        dst.rmdir()

    shutil.copytree(src, dst)


def seed_cmd(args) -> int:  # noqa: ANN001 — argparse.Namespace at call site
    """CLI entry point — catches ``SeedError`` and maps to exit codes.

    Error prefix is plain ASCII ``seed: error:`` so CI terminals without
    a UTF-8 locale don't swallow the message.

    Emits a SEL audit event on every invocation (allowed / denied / error)
    per the repo's security-controls guideline — ``seed`` writes an entire
    directory tree into ``$KIROCREW_HOME`` and 1.B's ``--seed-replace`` will
    ``rmtree`` first, so an audit trail is required now and cheaper to
    retrofit in 1.A than later.
    """
    fixture = args.seed  # required when gateway --seed was passed
    # ``--seed-replace`` on the CLI becomes ``args.seed_replace`` via argparse's
    # dash-to-underscore conversion. Kept as plain ``replace=`` inside the
    # module because the kwarg is module-internal (no flag collision risk
    # at the Python call site).
    replace = bool(getattr(args, "seed_replace", False))
    # Capture invocation-time env state BEFORE ``seed()`` runs — otherwise
    # this flag is always ``True`` on the success path (if ``KIROCREW_HOME``
    # were unset, ``_resolve_target()`` would have raised ``SeedError`` and
    # we'd be in the ``except SeedError`` branch, not here).  Capturing up
    # front keeps the flag meaningful.
    target_set = "KIROCREW_HOME" in os.environ
    try:
        seed(fixture, replace=replace)
    except SeedError as exc:
        print(f"seed: error: {exc}", file=sys.stderr)
        # EXIT_GUARDRAIL = user-input rejection ("denied"). SeedError with any
        # other code (reserved for 1.B's EXIT_IO_ERROR) maps to "error".
        # Log ``exc.guardrail`` (short code-controlled constant) rather than
        # ``{exc}`` — the message text can contain resolved filesystem
        # paths we must NOT leak to the SEL stream. ``target_set`` is
        # still presence-only. See class docstring on SeedError for the
        # full list of guardrail values an SOC analyst might see.
        _safe_audit(
            outcome="denied" if exc.code == EXIT_GUARDRAIL else "error",
            resources=f"fixture={fixture!r} replace={replace} guardrail={exc.guardrail}",
        )
        return exc.code
    except OSError as exc:
        # ``shutil.copytree`` raises ``FileExistsError`` when ``dst`` exists
        # (without --seed-replace; with --seed-replace we rmtree first so this
        # path is reached for disk-full / permission / similar OSErrors only).
        # Map the raw OSError to EXIT_IO_ERROR so the ``seed: error:`` prefix
        # + SEL audit contracts are preserved instead of leaking a traceback.
        #
        # Log only the exception TYPE in the audit stream (not ``{exc}``):
        # ``FileExistsError``'s string representation includes the full
        # target path (e.g. ``[Errno 17] File exists: '/home/user/dev'``)
        # which would defeat the ``target_set`` presence-only design below.
        # The user still sees the full detail on stderr for debuggability.
        print(f"seed: error: {exc}", file=sys.stderr)
        _safe_audit(
            outcome="error",
            resources=f"fixture={fixture!r} replace={replace} reason={type(exc).__name__}",
        )
        return EXIT_IO_ERROR
    _safe_audit(
        outcome="allowed",
        # ``{fixture!r}`` escapes control chars so a crafted name like
        # ``"foo\n<forged-event>"`` can't inject fake rows into the SEL
        # JSONL log (and ``sel.py`` json.dumps-escapes again downstream).
        # ``target_set`` records presence-only (captured pre-``seed()``) —
        # never the raw path, which would leak ``$HOME``-derived info.
        # ``replace`` records whether the rmtree path was taken.
        resources=(
            f"fixture={fixture!r} target_set={target_set} replace={replace}"
        ),
    )
    return EXIT_OK


def _safe_audit(*, outcome: str, resources: str) -> None:
    """Emit a SEL audit event; swallow any failure.

    SEL singleton init (``SecurityEventLog.__init__``) calls
    ``self._dir.mkdir(...)`` and ``_load_or_create_hmac_key()`` which can
    raise ``OSError`` / ``PermissionError`` in sandboxed CI accounts or
    read-only ``$HOME`` scenarios. ``seed_cmd``'s contract is "plain
    ASCII ``seed: error:`` prefix on every failure, clean exit code" —
    audit emission must never change user-visible exit behavior.

    Pattern matches ``dashboard/chat.py``'s forward-callback handling
    (``except Exception: logger.warning(...)``). Using ``.warning`` (not
    ``.debug``) is deliberate: the ``seed`` subcommand short-circuits in
    ``cli.py`` before ``logging.basicConfig()`` runs, so a ``.debug``
    call would be silently dropped by Python's last-resort handler
    (WARNING+ only). ``.warning`` survives the last-resort handler and
    emits one line to stderr on the rare path where SEL init fails —
    preserving the security-controls audit-observability requirement
    requirement.  SEL init failures are rare
    (once per read-only $HOME install), so the stderr line is not
    spammy; the alternative — silent drop — would violate the guideline
    even if the debug-level comment at the call site pretended otherwise.
    """
    # ``sel`` is imported at module level.  If SEL init itself fails at call
    # time (``SecurityEventLog.__init__`` does ``mkdir`` + HMAC-key load in
    # read-only $HOME / sandboxed CI), the broad ``except`` below catches it
    # and logs a WARNING — we never let the tool crash just because the
    # audit sink is unavailable.
    try:
        sel().log_api_access(
            caller="cli",
            operation="seed",
            outcome=outcome,
            source="cli",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 — audit must never fail the tool.
        logging.getLogger(__name__).warning(
            "seed: SEL audit emit failed", exc_info=True
        )

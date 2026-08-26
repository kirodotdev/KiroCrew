#!/usr/bin/env python3
"""Refuse an owner-only lockdown applied to a published path AFTER content.

A secret-bearing file must be locked down BEFORE any content reaches it.
Applying the lockdown once the payload is already at its final path leaves a
window in which the file exists under whatever permissions it inherited -- on
Windows that is the parent directory's DACL, and POSIX mode bits are not
enforced there at all, so ``atomic_write(mode=0o600)`` does not close it.

Issue #5307 converted seven such writers to
``atomic_write(..., restrict_to_owner=True)``, which locks the temp file down
before the first content byte and before the rename. Nothing prevented a NEW
writer from reintroducing the shape; this checker does.

A violation is, within ONE function body, an owner-only lockdown applied to a
path expression that an EARLIER statement in that same function already wrote
content to, where that path is the PUBLISHED one.

Deliberately NOT violations -- flagging these would make the gate worse than no
gate:

* a lockdown on a temp path the same function LATER renames into place, in any
  spelling (``os.replace(tmp, final)``, ``tmp.rename(final)``, or
  ``tmp.replace(target=final)``): the published file is never exposed, and this
  is the shape ``atomic_write`` uses. The rename must come AFTER the lockdown
  and in the same scope -- an EARLIER rename means the path was rotated aside
  and the payload written to it afterwards, which is the defect, not the fix;
* the delegated form ``atomic_write(..., restrict_to_owner=True)``, which is
  the fix this checker exists to protect;
* ``chmod``/``chmod_safe``/``fchmod_safe`` with a mode that grants anything to
  group or other
  -- making a launcher executable (``0o755``) is not a lockdown. Owner-only is
  tested as a property, so the symbolic spellings (``stat.S_IRWXU``,
  ``stat.S_IRUSR | stat.S_IWUSR``) count the same as ``0o700``/``0o600``;
* ``fd = os.open(p, O_CREAT...)`` then ``restrict_to_owner(p)`` then writing
  through the descriptor: ``os.open`` creates an EMPTY file, so the lockdown
  lands before any content. The content-write moment for that idiom is
  ``os.fdopen(fd, ...)``, and only a lockdown after THAT is a violation;
A path reached through a single-assignment local alias (``target = secret_path``)
counts as the same path; anything less tractable -- a name rebound in a branch, an
attribute, a write and lockdown split across two functions -- is outside the rule
by construction, so a green run is not proof the shape is absent.

Deliberately NOT violations (continued):

* a site carrying ``# lockdown-ok: <reason>`` on the lockdown line. The reason
  is mandatory, and there are three legitimate kinds: a load-time re-assert; a
  file that holds no data; or a call site where a REVIEWED decision says the
  helper must not be used here at all -- ``workflows/store.py::save`` runs on
  the event loop and ``restrict_to_owner`` spawns ``icacls``, so #5228 settled
  it on POSIX ``chmod`` with a ``_redact``-ed payload.

  The marker and ``KNOWN_UNCONVERTED`` are not interchangeable, and picking the
  wrong one misreports the site. ``KNOWN_UNCONVERTED`` means "this SHOULD be
  converted and has not been yet", so it is watched: the shrink-only rule fails
  once the debt is paid. The marker means "converting this would be wrong", so
  there is nothing to watch -- filing such a site as debt would instead tell the
  next contributor to make the change the decision rejected.

Usage:
    python scripts/check_lockdown_before_publish.py [PATH ...]

Exits non-zero and prints ``file:line`` per violation. With no arguments it
scans ``src/kiro_crew``. Sites tracked by an open conversion issue live in
``KNOWN_UNCONVERTED``; that list is shrink-only.
"""

from __future__ import annotations

import ast
import functools
import re
import stat
import sys
from pathlib import Path

#: Unambiguous lockdown helper: always means "owner-only, and nobody else".
RESTRICT_CALL = "restrict_to_owner"

#: ``chmod``-family calls are a lockdown only with an owner-only mode. The same
#: helpers set directory modes and executable bits.
CHMOD_CALLS = frozenset({"chmod_safe", "chmod"})

#: Descriptor-addressed lockdowns: ``fchmod_safe(fd, 0o600)`` / ``os.fchmod``.
#: The path is recovered through the fd map, exactly as the content-write side
#: already does for ``os.fdopen``/``os.write``. Without these, a writer that
#: hardened through the descriptor recorded NO lockdown at all -- and
#: ``fchmod_safe`` is the tree's own helper (platform_compat.py), not a
#: hypothetical spelling.
FCHMOD_CALLS = frozenset({"fchmod_safe", "fchmod"})

#: A mode is a lockdown when it grants nothing to group or other. Expressed as a
#: property rather than a list so 0o600/0o700/0o400 and their symbolic spellings
#: (``stat.S_IRWXU``, ``stat.S_IRUSR | stat.S_IWUSR``) are all covered, while
#: 0o755 and 0o644 -- an executable launcher, a world-readable file -- are not.
GROUP_OTHER_BITS = 0o077

#: Calls whose Nth positional argument is a path that RECEIVES content.
#: ``replace``/``rename`` are included because the rename is how the published
#: path gets its content in the temp-file idiom -- a lockdown after it is the
#: exact defect (see ``workflows/store.py`` before this checker landed).
WRITE_DESTINATION_ARG = {
    "atomic_write": 0,
    # This repo's private JSON writer (agent.py): mkstemp + write + rename, so
    # the payload lands at the FINAL path when it returns. It is not
    # ``atomic_write`` and takes no ``restrict_to_owner``, so a lockdown after it
    # is the #5307 shape -- and it is the single most widely used writer in the
    # dashboard handlers, which is why leaving it out made the gate quiet about a
    # real keystone site.
    "_atomic_json_write": 0,
    "copy2": 1,  # shutil.copy2(src, dst)
    "copyfile": 1,
    "copy": 1,
    "replace": 1,  # os.replace(tmp, final)
    "rename": 1,  # os.rename(tmp, final)
    "move": 1,  # shutil.move(tmp, final)
    "link": 1,  # os.link(tmp, final) — create-only publish; still a content landing
}

#: The same destinations by KEYWORD name. Reading positionally alone let the
#: keyword form (``atomic_write(path=...)``) register no write at all, so a
#: lockdown after it went unflagged.
WRITE_DESTINATION_KWARG = {
    "atomic_write": "path",
    "_atomic_json_write": "path",
    "copy2": "dst",
    "copyfile": "dst",
    "copy": "dst",
    "replace": "dst",
    "rename": "dst",
    "move": "dst",
    "link": "dst",
}

#: Keyword name of a publish call's SOURCE (the temp being renamed away).
PUBLISH_SOURCE_KWARG = "src"

#: Keyword name of the DESTINATION in the pathlib publish spelling:
#: ``tmp.replace(target=final)``. The os spelling uses ``src``/``dst``, so
#: reading only those left the pathlib keyword form recording no write at all.
PUBLISH_TARGET_KWARG = "target"

#: Keyword name of the builtin ``open`` path: ``open(file=final, mode="w")``.
#: Read positionally alone, the keyword form registered no content write.
OPEN_PATH_KWARG = "file"

#: ``<path>.write_text(...)`` / ``.write_bytes(...)`` -- receiver is the path.
WRITE_METHODS = frozenset({"write_text", "write_bytes"})

#: Calls that publish a path UNDER ANOTHER NAME, so a lockdown on the source is
#: a lockdown on a temp. ``shutil.move`` was absent, which failed BOTH ways: a
#: ``move`` to the final path recorded no publication (so a lockdown after it was
#: not flagged) AND the source got no temp exemption (so locking a temp and then
#: moving it -- correct code -- was flagged).
PUBLISH_CALLS = frozenset({"replace", "rename", "move", "link"})

#: The subset with a pathlib METHOD spelling where the receiver is the temp:
#: ``<tmp>.rename(final)`` / ``<tmp>.replace(final)``. ``shutil.move`` is a
#: module function only, and treating an arbitrary one-argument ``x.move(y)`` as
#: a publication would read unrelated receivers as temp paths.
PUBLISH_METHOD_CALLS = frozenset({"replace", "rename"})

WRITE_MODE_CHARS = ("w", "a", "x", "+")

#: ``os.fdopen(fd, ...)`` is where content starts flowing for the
#: os.open-then-lock-then-write idiom; the fd is mapped back to its path.
FDOPEN_CALL = "fdopen"

#: ``os.write(fd, data)`` writes content through the descriptor directly, without
#: ever wrapping it in a file object. Matched only with ``os`` as the literal
#: receiver -- a bare ``.write(...)`` is the commonest method call in the tree.
OS_WRITE_CALL = "write"

#: ``# lockdown-ok: <reason>`` -- reason is mandatory and must be non-empty.
SUPPRESS_RE = re.compile(r"#\s*lockdown-ok\s*:\s*(?P<reason>\S.*)$")

#: Sites carrying the shape under an OPEN conversion issue. Shrink-only: when a
#: site is converted, delete its entry -- the checker fails if an entry here no
#: longer violates, so the list cannot outlive the debt it tracks.
#: ``file::function`` -> ``(issue, path expression)``. The path expression is
#: load-bearing: keyed by function alone, a SECOND unrelated violating writer
#: added to an allowlisted function would be suppressed along with the tracked
#: one, so an allowlist entry would quietly widen into a whole-function waiver.
KNOWN_UNCONVERTED: dict[str, tuple[str, str]] = {
    # Empty: #5493 converted denied_commands; this PR converts write_pod_config
    # and snapshot._do_merge, and annotates sel._append_lines_locked lockdown-ok
    # (same event-loop / icacls reason as #5228). Shrink-only: do not re-add a
    # converted site.
}


def _norm(expr: str | None) -> str:
    """Normalise a path expression so spellings of one path compare equal."""
    if not expr:
        return ""
    text = " ".join(expr.split())
    changed = True
    while changed:
        changed = False
        for wrapper in ("str(", "Path(", "os.fspath("):
            if text.startswith(wrapper) and text.endswith(")"):
                text = text[len(wrapper) : -1].strip()
                changed = True
    return text


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


@functools.lru_cache(maxsize=8)
def _source_lines(source: str) -> tuple[str, ...]:
    """Line split, cached per file.

    ``ast.get_source_segment`` re-splits the entire file on EVERY call, which is
    fine for a handful of lookups and quadratic once the alias map asks for one
    per assignment: the whole-tree scan went from ~15s to ~117s before this.
    Python caches a str's hash after the first use, so keying the cache on the
    source itself is O(1) per call after the first.
    """
    return tuple(source.splitlines(True))


def _seg(source: str, node: ast.AST) -> str:
    """The source text of ``node``.

    Byte offsets, not character offsets: CPython reports ``col_offset`` as a
    UTF-8 byte index, so each edge line is encoded before slicing -- exactly what
    ``ast.get_source_segment`` does. This tree has non-ASCII string literals, so
    slicing the str directly would cut them in the wrong place. A test asserts
    this agrees with the stdlib on every node in the scanned tree.
    """
    lines = _source_lines(source)
    try:
        start = node.lineno - 1  # type: ignore[attr-defined]
        end = node.end_lineno - 1  # type: ignore[attr-defined]
        col = node.col_offset  # type: ignore[attr-defined]
        end_col = node.end_col_offset  # type: ignore[attr-defined]
        if start == end:
            return lines[start].encode()[col:end_col].decode()
        first = lines[start].encode()[col:].decode()
        last = lines[end].encode()[:end_col].decode()
        return "".join([first, *lines[start + 1 : end], last])
    except Exception:  # pragma: no cover - mirrors get_source_segment's leniency
        return ""


def _arg(node: ast.Call, index: int | None, keyword: str | None, source: str) -> str | None:
    """Read an argument positionally or by keyword, normalised."""
    if index is not None and len(node.args) > index:
        return _norm(_seg(source, node.args[index]))
    if keyword:
        for kw in node.keywords:
            if kw.arg == keyword:
                return _norm(_seg(source, kw.value))
    return None


def _exempted_by_restrict_kwarg(node: ast.Call) -> bool:
    """Only a LITERAL ``restrict_to_owner=True`` is the fix.

    Testing mere presence exempted ``restrict_to_owner=False`` -- a real write
    followed by a real lockdown, waved through as though already protected.
    Anything that is not a literal True (False, a name, an expression) reads as
    a plain write, which fails closed.
    """
    for kw in node.keywords:
        if kw.arg != "restrict_to_owner":
            continue
        return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _module_constants(tree: ast.AST) -> dict[str, object]:
    """``NAME -> value`` for module-level assignments of a resolvable mode.

    Naming the mode is this tree's prevailing style (``_LAUNCHER_MODE``,
    ``_DIR_MODE``, ``_SHOT_DIR_MODE``), and an unresolved mode is not owner-only,
    so ``chmod_safe(path, _SECRET_MODE)`` recorded no lockdown and the writer
    passed. Module level only: a name assigned inside a function can be rebound
    per call, and guessing there would trade a false negative for a false
    positive.
    """
    out: dict[str, object] = {}
    for node in ast.iter_child_nodes(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not targets or getattr(node, "value", None) is None:
            continue
        value = _resolve_mode(node.value)
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _resolve_mode(node: ast.AST, consts: dict[str, object] | None = None) -> object:
    """An int/str literal, a ``stat.S_*`` constant, a ``|`` chain, or a named one.

    Deliberately NOT general expression evaluation: the ``stat`` names are a
    closed set with fixed values, so resolving them is exact, and ``consts`` only
    ever holds module-level names that themselves resolved. Anything else returns
    None and the caller treats the mode as unknown.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and consts:
        return consts.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr.startswith("S_I"):
        value = getattr(stat, node.attr, None)
        return value if isinstance(value, int) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _resolve_mode(node.left, consts)
        right = _resolve_mode(node.right, consts)
        if isinstance(left, int) and isinstance(right, int):
            return left | right
        return None
    return None


def _mode_of(node: ast.Call, consts: dict[str, object] | None = None) -> object:
    if len(node.args) >= 2:
        resolved = _resolve_mode(node.args[1], consts)
        if resolved is not None:
            return resolved
    for kw in node.keywords:
        if kw.arg == "mode":
            return _resolve_mode(kw.value, consts)
    return None


def _is_owner_only_mode(mode: object) -> bool:
    """Owner-only: something for the owner, nothing for group or other."""
    return isinstance(mode, int) and mode != 0 and not (mode & GROUP_OTHER_BITS)


def _resolve_mode_kwarg(node: ast.Call, consts: dict[str, object] | None = None) -> object:
    """``mode=`` for a call whose positional slot carries no mode."""
    for kw in node.keywords:
        if kw.arg == "mode":
            return _resolve_mode(kw.value, consts)
    return None


def _is_publish_method_form(node: ast.Call) -> bool:
    """``tmp.rename(final)`` / ``tmp.replace(target=final)`` -- receiver is the TEMP.

    Told apart from the os-function spelling by ARITY, not node type: ``os.replace``
    is an attribute access too (``replace`` on ``os``), so keying on
    ``ast.Attribute`` alone would break ``os.replace(tmp, final)``. The os
    spelling names its arguments ``src``/``dst``; pathlib takes a single
    ``target``, so the presence of an os keyword settles the ambiguous arities.
    """
    if not isinstance(node.func, ast.Attribute):
        return False
    if _callee(node) not in PUBLISH_METHOD_CALLS:
        return False
    if any(kw.arg in (PUBLISH_SOURCE_KWARG, "dst") for kw in node.keywords):
        return False
    return len(node.args) <= 1


def _is_pathlib_chmod_form(node: ast.Call) -> bool:
    """``final.chmod(0o600)`` -- receiver is the path, argument 0 is the MODE.

    The os/helper spelling puts the path first (``os.chmod(p, 0o600)``,
    ``platform_compat.chmod_safe(p, 0o600)``), so the two are told apart by
    ARITY, as with the publish forms above. That is sound rather than merely
    convenient: ``mode`` is a REQUIRED parameter of both ``os.chmod`` and
    ``chmod_safe`` (see platform_compat.py), so a one-argument attribute-form
    chmod cannot be either of them.
    """
    if not isinstance(node.func, ast.Attribute):
        return False
    if any(kw.arg == "path" for kw in node.keywords):
        return False
    if any(kw.arg == "mode" for kw in node.keywords):
        return not node.args  # `final.chmod(mode=0o600)`
    return len(node.args) == 1  # `final.chmod(0o600)`


def _opens_for_write(node: ast.Call, consts: dict[str, object] | None = None) -> bool:
    """``open(p, "w")`` -- a builtin open in a writable text/binary mode.

    ``os.open`` is deliberately NOT a content write: it creates an empty file
    and the payload arrives through the descriptor, which is why the correct
    idiom locks the empty file down between the two.
    """
    mode = _mode_of(node, consts)
    if not isinstance(mode, str):
        return False
    return any(ch in mode for ch in WRITE_MODE_CHARS)


def _own_nodes(func: ast.AST):
    """Every node in this function EXCEPT the bodies of functions nested in it.

    ``ast.walk`` descends into nested ``def``/``lambda``, which pooled an inner
    function's writes, lockdowns and publishes into the outer one. A rename
    inside an unrelated nested helper therefore entered the outer function's
    temp set and exempted a real violation there. Nested functions are still
    scanned in their own right -- ``scan_source`` visits every FunctionDef in
    the module -- so nothing stops being checked, it only stops leaking.
    """
    stack = [func]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if child is not func and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            stack.append(child)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _binds(func: ast.AST) -> dict[str, int]:
    """How many times each bare name is bound in this scope.

    Every binding form counts, not just ``=``: a ``for`` target, ``with ... as``,
    a walrus or an augmented assignment all rebind the name, and a name bound
    more than once has no single value to alias to.
    """
    counts: dict[str, int] = {}

    def bump(node):
        if isinstance(node, ast.Name):
            counts[node.id] = counts.get(node.id, 0) + 1
        elif isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                bump(element)

    for node in _own_nodes(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bump(target)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            bump(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bump(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                bump(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node is not func:
                counts[node.name] = counts.get(node.name, 0) + 1
    return counts


def _alias_map(func: ast.AST, source: str) -> dict[str, str]:
    """``local name -> the normalised expression it was assigned``.

    Closes an alias hole that ran in BOTH directions. The rule compares path
    EXPRESSIONS, so ``target = secret_path`` split a single path into two names:

        target = secret_path
        target.write_text(secret)      # write recorded against `target`
        os.chmod(secret_path, 0o600)   # lockdown recorded against `secret_path`

    -- no match, so a real post-content lockdown went unflagged. The same split
    also FALSELY flagged correct code, because a temp aliased under a second name
    no longer matched the publish that exempted it.

    Deliberately narrow, and the narrowness is the point:

    * only names bound EXACTLY ONCE in this scope (see ``_binds``) -- a rebound
      name has no single value, and guessing one would invent findings;
    * only a bare identifier resolves, so ``self._path`` and ``cfg["p"]`` are
      left alone -- an attribute or subscript can change under us between the
      two statements;
    * same scope only, so nothing is inferred across a function boundary.

    Aliasing beyond that (rebound in a branch, carried through a helper, stored
    on an object) stays out of reach by construction, which is what makes this a
    ratchet rather than a dataflow analysis.
    """
    binds = _binds(func)
    out: dict[str, str] = {}
    for node in _own_nodes(func):  # noqa: SIM118
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or binds.get(target.id, 0) != 1:
            continue
        value = _norm(_seg(source, node.value))
        if value and value != target.id:
            out[target.id] = value
    return out


def _resolve_alias(expr: str, aliases: dict[str, str]) -> str:
    """Follow a bare-name alias chain to the expression it ultimately names."""
    seen: set[str] = set()
    while _IDENT_RE.match(expr) and expr in aliases and expr not in seen:
        seen.add(expr)
        expr = aliases[expr]
    return expr


def _fd_paths(func: ast.AST, source: str) -> dict[str, str]:
    """``fd variable -> path`` for each ``fd = os.open(path, ...)``."""
    out: dict[str, str] = {}
    for node in _own_nodes(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        if _callee(value) == "open" and isinstance(value.func, ast.Attribute) and value.args:
            out[target.id] = _norm(_seg(source, value.args[0]))
    return out


def _write_target(
    node: ast.Call,
    source: str,
    fd_paths: dict[str, str] | None = None,
    consts: dict[str, object] | None = None,
) -> str | None:
    """The path this call writes content to, if any."""
    name = _callee(node)
    if name is None:
        return None

    # `open(p, "w")` -- but NOT `os.open`, which only creates the file.
    # The path is read positionally OR as `file=`: `open(file=final, mode="w")`
    # is a valid builtin call that recorded no write at all, so a lockdown after
    # it found no preceding write and the writer escaped the gate.
    if name == "open" and isinstance(node.func, ast.Name):
        if _opens_for_write(node, consts):
            return _arg(node, 0, OPEN_PATH_KWARG, source)
        return None

    # Method-form `.open()` is two different calls, told apart by whether the
    # first positional argument is a string literal (a mode) or not (a path):
    #   `path.open("w")`             -> receiver is the path, arg 0 is the mode
    #   `tarfile.open(name, "w:gz")` -> receiver is a module, arg 0 is the path
    if name == "open" and isinstance(node.func, ast.Attribute):
        first_is_mode = (
            bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
        if first_is_mode or not node.args:
            # pathlib spelling: the mode sits where the builtin's path would be.
            mode = node.args[0].value if first_is_mode else _resolve_mode_kwarg(node, consts)
            if isinstance(mode, str) and any(ch in mode for ch in WRITE_MODE_CHARS):
                return _norm(_seg(source, node.func.value))
            return None
        # module spelling (tarfile/gzip/io): path at 0, mode at 1.
        if _opens_for_write(node, consts):
            return _norm(_seg(source, node.args[0]))
        return None

    # `os.fdopen(fd, ...)` / `os.write(fd, data)` -- content flows into the fd's
    # path here. os.write goes straight to the descriptor with no file object, so
    # without it a write to a descriptor opened on the FINAL path went unrecorded
    # and a lockdown after it was not flagged.
    if name == FDOPEN_CALL or (
        name == OS_WRITE_CALL
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ):
        if node.args and fd_paths:
            first = node.args[0]
            if isinstance(first, ast.Name):
                return fd_paths.get(first.id)
        return None

    if name in WRITE_METHODS and isinstance(node.func, ast.Attribute):
        return _norm(_seg(source, node.func.value))

    index = WRITE_DESTINATION_ARG.get(name)
    if index is None:
        return None
    if name == "atomic_write" and _exempted_by_restrict_kwarg(node):
        # Locks the temp down before content -- the fix, not the defect.
        return None
    if name in PUBLISH_CALLS and _is_publish_method_form(node):
        # Method form: `tmp.rename(final)` / `tmp.replace(target=final)` -- the
        # destination is the SOLE positional arg, or pathlib's `target=`, never
        # index 1. The indices above are for the os-function spelling
        # `os.replace(tmp, final)`; reading index 1 here recorded no write at
        # all, so a later lockdown on `final` found no preceding write and the
        # pathlib idiom escaped the gate in BOTH its spellings.
        return _arg(node, 0, PUBLISH_TARGET_KWARG, source)
    return _arg(node, index, WRITE_DESTINATION_KWARG.get(name), source)


def _lockdown_target(
    node: ast.Call,
    source: str,
    fd_paths: dict[str, str] | None = None,
    consts: dict[str, object] | None = None,
) -> str | None:
    name = _callee(node)
    if name == RESTRICT_CALL:
        return _arg(node, 0, "path", source)
    if name in FCHMOD_CALLS:
        # `fchmod_safe(fd, 0o600)`: addressed by DESCRIPTOR, so the path comes
        # from the fd map -- the same recovery the content-write side already
        # does for `os.fdopen(fd)`/`os.write(fd, ...)`. Unmapped fd -> unknown
        # path -> no lockdown recorded, which is the pre-existing behaviour for
        # any path this rule cannot name.
        if _is_owner_only_mode(_mode_of(node, consts)) and node.args and fd_paths:
            first = node.args[0]
            if isinstance(first, ast.Name):
                return fd_paths.get(first.id)
        return None
    if name in CHMOD_CALLS:
        if _is_pathlib_chmod_form(node):
            # `final.chmod(0o600)`: the mode sits where the os spelling's path
            # would be, so _mode_of read no mode, NO lockdown was recorded at
            # all, and a post-write chmod in this spelling escaped the gate --
            # the rule needs to see the lockdown before it can flag it.
            mode = _resolve_mode(node.args[0], consts) if node.args else _mode_of(node, consts)
            if _is_owner_only_mode(mode):
                return _norm(_seg(source, node.func.value))
            return None
        if _is_owner_only_mode(_mode_of(node, consts)):
            return _arg(node, 0, "path", source)
    return None


def _temp_sources(node: ast.Call, source: str) -> list[str]:
    """Paths this call publishes UNDER ANOTHER NAME, in either spelling.

    ``os.replace(tmp, final)``   -- the temp is the first argument.
    ``os.link(tmp, final)``      -- create-only publish; the temp is still arg 0.
    ``tmp.rename(final)``        -- the temp is the receiver.
    ``tmp.replace(target=final)`` -- likewise, and missing this spelling made the
    exemption fail OPEN in the other direction: a CORRECT writer that locked its
    temp down and then published with the keyword form recorded no temp at all,
    so its temp lockdown was reported as a violation.
    """
    if _callee(node) not in PUBLISH_CALLS:
        return []
    found = []
    if _is_publish_method_form(node):
        found.append(_norm(_seg(source, node.func.value)))
    else:
        # `os.replace(tmp, final)` / `os.replace(src=tmp, dst=final)`.
        found.append(_arg(node, 0, PUBLISH_SOURCE_KWARG, source))
    return [f for f in found if f]


def _suppressions(source: str) -> dict[int, str]:
    """``lineno -> reason`` for every ``# lockdown-ok:`` marker."""
    out: dict[int, str] = {}
    for i, line in enumerate(source.splitlines(), 1):
        match = SUPPRESS_RE.search(line)
        if match:
            out[i] = match.group("reason").strip()
    return out


def scan_source(source: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, function, path_expression)`` per violation."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    suppressed = _suppressions(source)
    consts = _module_constants(tree)
    violations: list[tuple[int, str, str]] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        writes: list[tuple[int, str]] = []
        lockdowns: list[tuple[int, str]] = []
        # (line, path): the LINE is load-bearing -- see the exemption below.
        temps: list[tuple[int, str]] = []
        fd_paths = _fd_paths(func, source)
        # Every recorded path goes through the alias map, so the three sides of
        # the rule name a path the same way even when the code does not.
        aliases = _alias_map(func, source)

        for node in _own_nodes(func):
            if not isinstance(node, ast.Call):
                continue
            target = _write_target(node, source, fd_paths, consts)
            if target:
                writes.append((node.lineno, _resolve_alias(target, aliases)))
            locked = _lockdown_target(node, source, fd_paths, consts)
            if locked:
                lockdowns.append((node.lineno, _resolve_alias(locked, aliases)))
            temps.extend(
                (node.lineno, _resolve_alias(t, aliases)) for t in _temp_sources(node, source)
            )

        for line, path in lockdowns:
            # Exempt only a publish that comes AFTER this lockdown: that is what
            # "locked down before it was published" means, and it is the whole
            # basis of the exemption. Keyed on the path alone, an EARLIER publish
            # exempted it too -- so rotating an existing secret aside and then
            # writing a new one to the freed path passed the gate:
            #
            #     path.rename(backup)        # `path` published away FIRST
            #     path.write_text(secret)    # new payload at the PUBLISHED path
            #     os.chmod(path, 0o600)      # locked only after content
            #
            # This is #5307's own invariant, not the stricter temp-path one: the
            # correct idiom (write temp, lock temp, THEN rename) still exempts,
            # because there the publish follows the lockdown.
            if any(t_path == path and t_line > line for t_line, t_path in temps):
                continue  # published under another name, after being locked
            if line in suppressed:
                continue  # reasoned re-assert / dataless file
            if any(w_path == path and w_line < line for w_line, w_path in writes):
                violations.append((line, func.name, path))

    return sorted(set(violations))


def scan_path(path: Path, root: Path) -> list[tuple[str, int, str, str]]:
    source = path.read_text(encoding="utf-8", errors="replace")
    # POSIX form on every platform: str() yields backslashes on Windows, which
    # would make every KNOWN_UNCONVERTED key look stale AND every real site look
    # new -- the Windows shard caught exactly that.
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    return [(rel, line, fn, expr) for line, fn, expr in scan_source(source)]


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [Path(a) for a in argv[1:]] or [root / "src" / "kiro_crew"]

    found: list[tuple[str, int, str, str]] = []
    for target in targets:
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for py in files:
            found.extend(scan_path(py, root))

    new: list[tuple[str, int, str, str]] = []
    seen_known: set[str] = set()
    for rel, line, fn, expr in found:
        key = "%s::%s" % (rel, fn)
        entry = KNOWN_UNCONVERTED.get(key)
        # Only the exact tracked path is waived. A different path in the same
        # function is a NEW violation and reports.
        if entry is not None and entry[1] == expr:
            seen_known.add(key)
        else:
            new.append((rel, line, fn, expr))

    stale = sorted(set(KNOWN_UNCONVERTED) - seen_known)

    if new:
        print("Lockdown-before-publish check FAILED:\n")
        for rel, line, fn, expr in new:
            print(
                "  %s:%d: `%s` is locked down only AFTER content was written to "
                "it in %s().\n      Use atomic_write(..., restrict_to_owner=True) "
                "so the temp file is locked down before the payload and before "
                "the rename (issue #5307). If this is a load-time re-assert or a "
                "file that holds no data, annotate the lockdown line with "
                "`# lockdown-ok: <reason>`." % (rel, line, expr, fn)
            )
        print("\n%d new violation(s)." % len(new))

    if stale:
        print(
            "\nLockdown-before-publish check FAILED: %d KNOWN_UNCONVERTED entry(ies) "
            "no longer violate -- the debt was paid, so delete the entry.\n"
            "Edit KNOWN_UNCONVERTED in scripts/%s and remove:\n" % (len(stale), Path(__file__).name)
        )
        for key in stale:
            issue, expr = KNOWN_UNCONVERTED[key]
            print("  %s   (`%s`, was tracked by %s)" % (key, expr, issue))

    if new or stale:
        return 1

    if seen_known:
        print(
            "Lockdown-before-publish check passed "
            "(%d known unconverted site(s) tracked by an open issue)." % len(seen_known)
        )
    else:
        print("Lockdown-before-publish check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

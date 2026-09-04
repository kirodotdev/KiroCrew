#!/usr/bin/env python3
"""check_sync_io_in_async.py -- no NEW blocking IO inside an ``async def``.

## The failure class

The gateway runs one event loop. A synchronous call that blocks it -- a SQLite
statement waiting on a write lock, ``subprocess.run`` on a pip install,
``requests.get`` with no timeout, ``time.sleep`` -- stalls every other session's
turn while it runs, and when the stall outlasts
``dashboard.loop_stall_exit_after_secs`` (25s) the watchdog kills the process.
What the user sees is the gateway restarting for no apparent reason, dropping
every in-flight turn in every channel (#1572).

The audit behind #3057 walked 887 files and 4054 ``async def``s and found the
defects are not spread evenly -- they cluster where nothing was watching:
``dashboard/handlers/knowledge.py`` had ~70 direct ``store.db.execute()`` calls
on the loop while ``dashboard/handlers/memory.py``, in the same directory,
wraps every store call in ``asyncio.to_thread``. Same repo, same primitives,
opposite outcomes. The difference is not knowledge -- it is that nothing failed
when you got it wrong. This gate is the thing that fails.

## INVARIANT this gate exists to protect

**A wait reachable from the event loop must be shorter than the watchdog budget
-- or, better, must not be reachable from the loop at all.** SQLite
``busy_timeout`` values in this tree run to 30s
(``apps/builtins/auto_research/handlers.py``), 10s (``knowledge/store.py``) and
5s (``vector_memory.py``), none of them chosen against
``dashboard.loop_stall_exit_after_secs=25``. A 30s lock wait REACHABLE FROM THE
LOOP kills the process by arithmetic, with no bug anywhere else.

That 30s case is the worked example of the invariant's second clause, and #7039
has since settled it the right way round: it offloaded the six DB sections
rather than lowering the timeout, and put an on-loop guard on the accessor
itself (raise under strict/dev, throttled warning in production -- the
``history.py`` shape). 30s is now correct there BECAUSE the wait is no longer
reachable from the loop.

This gate enforces that second clause -- reachability -- and nothing here reads
a timeout value. So a future ``busy_timeout=60000`` on a loop-reachable
connection still ships green past this gate; #7020 carries that gap, and #7078
carries generalizing #7039's accessor guard beyond the one app.

## What counts as a violation

A call, lexically inside an ``async def`` body in ``src/``, to something that
blocks the calling thread:

* **db** -- an attribute call ``execute`` / ``executemany`` / ``executescript``
  / ``commit`` whose receiver reads like a database handle (``db``, ``_db``,
  ``conn``, ``connection``, ``cursor``, ``cur``, ``sql``, ``store``, ``sqlite``
  as a name component -- so ``store.db.execute(...)`` and ``self._conn.commit()``
  match, while ``workflow.execute(...)`` does not).
* **subprocess** -- ``subprocess.run`` / ``call`` / ``check_call`` /
  ``check_output``, this repo's kwargs-forwarding wrappers ``run_limited`` /
  ``popen_limited``, ``os.system``, ``os.popen``.
* **http** -- a synchronous client: ``requests.*`` / ``httpx.*`` verbs,
  ``urlopen``, ``socket.create_connection``. Also the client-OBJECT spellings,
  where the receiver is not a module: a name, attribute or ``with`` binding this
  file proves is a ``requests.Session()`` / ``httpx.Client()``, or the
  constructor called inline (``requests.Session().get(url)``). A verb is only
  read as a request on a receiver the file itself proves is a client, which is
  what keeps ``.get(`` away from ``dict.get``. ``httpx.AsyncClient`` is excluded
  on purpose: its verbs are awaitable and correct.
* **sleep** -- ``time.sleep``.

The set is deliberately the high-severity end (seconds to minutes per call), not
everything that touches a descriptor. A local ``read_text()`` is microseconds
and gating it would bury the calls that actually reach the watchdog budget under
thousands of harmless entries.

## What is NOT a violation, and why

* **NOT an exemption: `await`.** Nothing gated here is awaitable in this tree --
  the dependencies carry no async DB driver, and no awaitable
  requests/httpx-sync/subprocess/``time.sleep`` exists at all -- so ``await
  cur.execute(...)`` or ``await requests.get(url)`` performs the entire blocking
  call on the loop and only then raises ``TypeError`` on the non-awaitable
  result. The stall is identical, so the awaited spelling is reported too. If an
  async driver is ever adopted, its call sites carry the opt-out marker below,
  which records WHICH driver made them awaitable -- a blanket exemption records
  nothing and would hide this shape for every unconverted site at the same time.
* **A callable handed to an offload.** ``await asyncio.to_thread(cur.execute,
  sql)`` names the function without calling it, so there is no call on the loop
  to flag -- and the same is true of ``run_in_executor`` and the named lanes in
  ``executors.py`` (``maintenance``, ``subprocess``, ``cron``, ``discovery``,
  ``embed``, ``governance``). Note what this does NOT exempt: an offload's
  ARGUMENTS are evaluated on the loop before the thread starts, so
  ``to_thread(worker, requests.get(url))`` is a real on-loop request and is
  flagged. There is deliberately no "inside a to_thread call" exemption --
  writing one would have hidden exactly that defect.
* **A nested ``def`` / ``lambda`` body.** Those run when called, and the
  sanctioned idiom is precisely to put the blocking work in one and hand it to
  ``to_thread``. A nested ``async def`` is judged on its own, as its own scope.
* **Test code** (``test_*.py``, ``tests/`` packages) and ``_vendor/``: a test's
  own setup blocks nobody's turn, and vendored code is upstream's.

## Known limits, on purpose

The gate is LEXICAL. A blocking call one frame down -- an ``async def`` calling
a plain helper that runs the query -- is invisible to it, and no name-based AST
scan can see that without whole-program type inference. It is the same trade the
loop-bound-locks gate documents for lock registries: catch the shape that is
mechanically checkable, leave the interprocedural case to review and to the
runtime guard (``history.py``'s ``OnLoopPersistError``, which is #3057's remedy A,
tracked at #7078 -- until it lands this gate is the whole defence and it does not
cover that shape). Matching is by NAME, so an aliased
``from requests import get as fetch`` escapes; the ratchet's job is to stop the
ordinary spelling from growing back, not to outrun a determined author.

## The opt-out marker

A site that genuinely cannot block -- an in-memory SQLite handle, a call reached
only from a sync entry point, a subprocess spawned with a sub-second timeout on
a path where offloading would be more risk than the call -- opts out with an
inline COMMENT on any line of the call:

    cur.execute(sql)  # on-loop-io-ok: in-memory handle, no lock wait possible

The syntax is enforced, not just conventional: the comment must OPEN with the
marker and carry a non-empty reason. A bare ``# on-loop-io-ok``, or the phrase
inside a sentence ("this is not on-loop-io-ok"), exempts nothing -- and only a
real comment token counts, so the phrase inside a string literal exempts nothing
either. The reason is the point: the marker is an audit trail, not a mute button.

## Interaction with the black baseline, which is a real cost

All four files listed here are also in ``.github/black-baseline.txt``. A
format-only commit that reformats one of them rewrites the lines its violations
sit on, so every one of them becomes an ADDED line and the added-line rule fires
even though the count is level and nothing changed semantically. That is a
deliberate, accepted cost: it means the four listed files should not be
reformatted until their entries are cleared, which is the cleanup tracked at
#7019 -- and it costs nothing today, because a reformat now would collide with
that rewrite regardless. The escape is NOT a row of semantically false markers.

## The ratchet

The repository predates this gate, so existing sites are recorded as legacy in
``.github/sync-io-in-async-baseline.txt`` as ``<count> <path>`` lines. The rules
mirror ``check_subprocess_encoding.py`` and ``check_black_formatting.py`` (same
problem -- a large pre-existing set that must only shrink):

* a file NOT in the baseline must be clean;
* a baselined file may not grow its count;
* in a file this change touches, a violation on an ADDED line is a new offender
  even when the count is level -- otherwise fixing one old call while adding one
  new one would slip through with the count unchanged;
* a baselined file whose count shrank (or that is clean or gone) must be pruned
  so the list only shrinks -- run ``--update-baseline``, which only ever lowers
  counts and deletes lines, never adds or raises one.

Verdicts cover only the files this change touches: CI evaluates a merge ref, so
an unscoped gate would redden a PR for a file the base branch merged after the
baseline was recorded, and a stale count from someone else's cleanup is their
prune to record.

## Usage

    python3 scripts/check_sync_io_in_async.py            # the gate
    python3 scripts/check_sync_io_in_async.py --test     # self-test the rules
    python3 scripts/check_sync_io_in_async.py --report   # what is still listed
    python3 scripts/check_sync_io_in_async.py --update-baseline   # record progress
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import re
import tokenize
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".github" / "sync-io-in-async-baseline.txt"
SCAN_ROOT = "src"
MARKER = "on-loop-io-ok"
# The comment must OPEN with the marker and carry a reason. A bare marker, or the
# phrase inside a larger sentence, does not exempt anything: the point of the
# marker is the reason it records.
MARKER_RE = re.compile(r"^#\s*" + re.escape(MARKER) + r":\s*\S")

# module alias -> {attribute: family}. Matched as `<alias>.<attr>(...)` where the
# alias is bound by an import of that module anywhere in the file (lazy
# function-local imports are the norm in this tree).
MODULE_FUNCS: dict[str, dict[str, str]] = {
    "subprocess": {
        "run": "subprocess",
        "call": "subprocess",
        "check_call": "subprocess",
        "check_output": "subprocess",
    },
    "os": {"system": "subprocess", "popen": "subprocess"},
    "time": {"sleep": "sleep"},
    "requests": {
        "get": "http",
        "post": "http",
        "put": "http",
        "patch": "http",
        "delete": "http",
        "head": "http",
        "options": "http",
        "request": "http",
    },
    "httpx": {
        "get": "http",
        "post": "http",
        "put": "http",
        "patch": "http",
        "delete": "http",
        "head": "http",
        "options": "http",
        "request": "http",
        "stream": "http",
    },
    "socket": {"create_connection": "http"},
}

# Names distinctive enough to match on any receiver (or none): `urlopen` reached
# through `urllib.request.urlopen`, `request.urlopen` or a bare from-import is
# the same blocking call, and the two wrappers are this repo's own.
NAME_FUNCS: dict[str, str] = {
    "urlopen": "http",
    "run_limited": "subprocess",
    "popen_limited": "subprocess",
}

# Verbs that perform a request, checked on a receiver PROVEN to be a synchronous
# client in this same file. `requests.get(url)` is caught by MODULE_FUNCS above,
# but the client-object spellings -- `s = requests.Session(); s.get(url)`, `with
# httpx.Client() as c: c.get(url)`, `requests.Session().get(url)` -- have a
# receiver that is not a module alias, so they need this second path. Bare
# `.get(` is never matched on its own: that is `dict.get`.
HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream", "send"}
)
# httpx.AsyncClient is deliberately absent: its verbs ARE awaitable and correct.
SYNC_CLIENT_CTORS = frozenset({("requests", "Session"), ("httpx", "Client")})
# Tracked only to DISAMBIGUATE: a name bound to one of these is not a sync client,
# so it must not inherit a same-named sync binding from another scope.
ASYNC_CLIENT_CTORS = frozenset({("httpx", "AsyncClient")})

DB_METHODS = frozenset(
    {"execute", "executemany", "executescript", "commit"}
)  # A receiver that reads like a DB handle. `.execute(` alone is far too generic --
# this tree also has workflow/plan/action executors, and flagging those would
# teach contributors that the marker means "not IO" instead of "cannot block".
# `_` counts as a boundary, so the private spellings (`self._db`, `self._conn`)
# match while `mydbcache` does not.
DB_RECEIVER_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:db|dbs|conn|conns|connection|cursor|cur|sql|sqlite|store)"
    r"(?:[^A-Za-z0-9]|$)"
)

FAMILY_REMEDY = {
    "db": (
        "a statement blocks the loop for as long as the write lock is held "
        "(busy_timeout is 5-30s in this tree, against a 25s watchdog budget) -- "
        "offload it: `await asyncio.to_thread(cur.execute, sql, params)`"
    ),
    "subprocess": (
        "a child process blocks the loop until it exits -- use "
        "`asyncio.create_subprocess_exec`, or `await asyncio.to_thread(...)` / "
        "`kiro_crew.executors.subprocess_executor()`"
    ),
    "http": (
        "a synchronous request blocks the loop for the whole round trip -- use "
        "aiohttp (already a dependency), or `await asyncio.to_thread(...)`"
    ),
    "sleep": "`time.sleep` blocks the loop; `await asyncio.sleep(...)` does not",
}

HEADER = """\
# Blocking IO calls inside an `async def`, as `<count> <path>`.
# Each one stalls every session's turn while it runs, and a stall longer than
# dashboard.loop_stall_exit_after_secs (25s) makes the watchdog kill the
# gateway (#3057, #1572). The gate requires every OTHER file to be clean and
# none of these counts to grow, so this list can only shrink.
#
# Do NOT add or raise a line to make a red gate green: a new offender belongs
# in a thread (`await asyncio.to_thread(...)`, or a named lane from
# src/kiro_crew/executors.py), or carries an inline `# on-loop-io-ok: <why it
# cannot block>` comment. The refresh command below only lowers counts and
# deletes lines.
#
# Refresh (after fixing something listed here):
#   python3 scripts/check_sync_io_in_async.py --update-baseline
#
# See where the remaining calls are, worst files first (the work queue for the
# cleanup tracked at #7019):
#   python3 scripts/check_sync_io_in_async.py --report
"""


def _scope():
    """The shared diff-scope helpers (see scripts/ratchet_scope.py).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` -- and not at all
    when a test loads this gate by path.
    """
    path = ROOT / "scripts" / "ratchet_scope.py"
    spec = importlib.util.spec_from_file_location("ratchet_scope", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_names(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """(alias -> module, local name -> (module, attribute), ctor name -> (module, ctor)).

    Every import in the file is read, not just module-scope ones: a lazy
    ``import requests`` inside the coroutine that uses it is the common spelling
    here, and it blocks the loop exactly as much as a top-level one. The third map
    carries from-imported HTTP client constructors (``from requests import
    Session``), whose call site is a bare Name; it records WHICH constructor the
    local name came from, so an ``as`` alias still resolves and a sync client is
    told apart from an async one.
    """
    aliases: dict[str, str] = {}
    from_names: dict[str, tuple[str, str]] = {}
    from_ctors: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import urllib.request` binds `urllib`; `import subprocess as sp`
                # binds `sp`. Only the gated module names matter.
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                if target in MODULE_FUNCS:
                    aliases[bound] = target
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                # Async constructors are recorded too, purely to DISAMBIGUATE a
                # name that another scope also binds to a sync client.
                origin = (module, alias.name)
                if origin in SYNC_CLIENT_CTORS or origin in ASYNC_CLIENT_CTORS:
                    from_ctors[alias.asname or alias.name] = origin
            table = MODULE_FUNCS.get(module)
            if not table:
                continue
            for alias in node.names:
                if alias.name in table:
                    from_names[alias.asname or alias.name] = (module, alias.name)
    return aliases, from_names, from_ctors


def _is_client_ctor(
    value: ast.expr,
    aliases: dict[str, str],
    from_ctors: dict[str, tuple[str, str]],
    table: frozenset[tuple[str, str]],
) -> bool:
    """True when ``value`` constructs a client from ``table``.

    Both spellings: ``requests.Session()`` through a module alias, and a bare
    ``Session()`` from ``from requests import Session``.
    """
    if not isinstance(value, ast.Call):
        return False
    fn = value.func
    if isinstance(fn, ast.Name):
        return from_ctors.get(fn.id) in table
    if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
        return False
    module = aliases.get(fn.value.id)
    return module is not None and (module, fn.attr) in table


def _is_sync_client_ctor(
    value: ast.expr, aliases: dict[str, str], from_ctors: dict[str, tuple[str, str]]
) -> bool:
    return _is_client_ctor(value, aliases, from_ctors, SYNC_CLIENT_CTORS)


def _is_async_client_ctor(
    value: ast.expr, aliases: dict[str, str], from_ctors: dict[str, tuple[str, str]]
) -> bool:
    return _is_client_ctor(value, aliases, from_ctors, ASYNC_CLIENT_CTORS)


def _scope_own_nodes(scope: ast.AST):
    """Every node belonging to ``scope``'s OWN body, not a nested function's.

    Used to bind a client name to the scope that created it: a local name is
    function-scoped in Python, so two coroutines may reuse one name for different
    objects.
    """
    stack: list[ast.AST] = list(getattr(scope, "body", []) or [])
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _client_resolver(
    tree: ast.Module, aliases: dict[str, str], from_ctors: dict[str, tuple[str, str]]
):
    """Build ``visible_for(coroutine) -> {client name: binding line}``.

    Bindings are scoped the way Python scopes them, because conflating them goes
    wrong in BOTH directions and a gate can afford neither:

    * a local NAME is function-scoped -- a sync ``session`` in one coroutine must
      not make a same-named ``httpx.AsyncClient()`` in another look blocking
      (a FALSE POSITIVE, whose only escape would be a marker asserting something
      untrue);
    * an ATTRIBUTE is instance state, so it is scoped to its CLASS -- bound in
      ``__init__``, read in the handlers. Treating attributes as file-wide let an
      ``AsyncClient`` attribute in one class erase a DIFFERENT class's synchronous
      binding of the same attribute name, which is a MISS;
    * a MODULE-level name is visible everywhere, since functions read globals.

    Within one bucket, a name also bound to an async client is dropped: there the
    ambiguity is real, and a miss is the safe direction.
    """
    module_names: dict[str, int] = {}
    loose_attrs: dict[str, int] = {}  # attributes bound outside any class
    attrs_by_class: dict[int, dict[str, int]] = {}
    locals_by_scope: dict[int, dict[str, int]] = {}
    class_of: dict[int, int] = {}

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for inner in ast.walk(cls):
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                class_of.setdefault(id(inner), id(cls))

    scopes: list[ast.AST] = [tree]
    scopes.extend(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for scope in scopes:
        names: dict[str, int] = {}
        attrs: dict[str, int] = {}
        async_names: set[str] = set()
        async_attrs: set[str] = set()

        def record(target: ast.expr, lineno: int, sync: bool) -> None:
            if isinstance(target, ast.Name):
                (names.setdefault(target.id, lineno) if sync else async_names.add(target.id))
            elif isinstance(target, ast.Attribute):
                (attrs.setdefault(target.attr, lineno) if sync else async_attrs.add(target.attr))

        for node in _scope_own_nodes(scope):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                sync = _is_sync_client_ctor(value, aliases, from_ctors)
                if not sync and not _is_async_client_ctor(value, aliases, from_ctors):
                    continue
                for target in targets:
                    record(target, node.lineno, sync)
            elif isinstance(node, ast.withitem) and node.optional_vars is not None:
                sync = _is_sync_client_ctor(node.context_expr, aliases, from_ctors)
                if not sync and not _is_async_client_ctor(node.context_expr, aliases, from_ctors):
                    continue
                record(node.optional_vars, node.context_expr.lineno, sync)

        for name in async_names:
            names.pop(name, None)
        for attr in async_attrs:
            attrs.pop(attr, None)

        owner = class_of.get(id(scope), id(scope) if isinstance(scope, ast.ClassDef) else None)
        if attrs:
            if owner is None:
                loose_attrs.update(attrs)
            else:
                attrs_by_class.setdefault(owner, {}).update(attrs)
        if owner is not None:
            for attr in async_attrs:
                attrs_by_class.get(owner, {}).pop(attr, None)
        else:
            for attr in async_attrs:
                loose_attrs.pop(attr, None)
        if scope is tree:
            module_names.update(names)
        elif names:
            locals_by_scope[id(scope)] = names

    def visible_for(fn: ast.AsyncFunctionDef) -> dict[str, int]:
        merged: dict[str, int] = {**module_names, **loose_attrs}
        owner = class_of.get(id(fn))
        if owner is not None:
            merged.update(attrs_by_class.get(owner, {}))
        merged.update(locals_by_scope.get(id(fn), {}))
        return merged

    return visible_for


def _family(
    node: ast.Call,
    aliases: dict[str, str],
    from_names: dict[str, tuple[str, str]],
    clients: dict[str, int],
    from_ctors: dict[str, tuple[str, str]],
) -> tuple[str, int | None] | None:
    """(blocking family, provenance line) for this call, or None.

    The provenance line is the line whose presence made the verdict true when
    that is NOT the call's own line -- today only the client-binding line behind a
    client-object HTTP verb. The added-line rule checks it too, so adding the
    binding is caught even when the call it flips is untouched.
    """
    fn = node.func
    if isinstance(fn, ast.Attribute):
        if isinstance(fn.value, ast.Name):
            module = aliases.get(fn.value.id)
            if module and fn.attr in MODULE_FUNCS[module]:
                return MODULE_FUNCS[module][fn.attr], None
        if fn.attr in NAME_FUNCS:
            return NAME_FUNCS[fn.attr], None
        if fn.attr in HTTP_VERBS:
            bound_at = _on_sync_client(fn.value, aliases, clients, from_ctors)
            if bound_at is not None:
                return "http", bound_at
        if fn.attr in DB_METHODS and DB_RECEIVER_RE.search(ast.unparse(fn.value)):
            return "db", None
        return None
    if isinstance(fn, ast.Name):
        origin = from_names.get(fn.id)
        if origin is not None:
            return MODULE_FUNCS[origin[0]][origin[1]], None
        family = NAME_FUNCS.get(fn.id)
        return None if family is None else (family, None)
    return None


def _on_sync_client(
    receiver: ast.expr,
    aliases: dict[str, str],
    clients: dict[str, int],
    from_ctors: dict[str, tuple[str, str]],
) -> int | None:
    """The line binding ``receiver`` to a synchronous HTTP client, or None.

    A receiver that IS the constructor call (``requests.Session().get(url)``)
    carries its own provenance, reported as line 0 so the caller can tell "is a
    client, no separate binding line" from "is not a client".
    """
    if isinstance(receiver, ast.Name):
        return clients.get(receiver.id)
    if isinstance(receiver, ast.Attribute):
        return clients.get(receiver.attr)
    if _is_sync_client_ctor(receiver, aliases, from_ctors):
        return 0
    return None


def _own_nodes(fn: ast.AsyncFunctionDef):
    """Every node in this coroutine's OWN body.

    Never descends into a nested function or lambda: their bodies run when
    called, and handing one to ``to_thread`` is the sanctioned way to do
    blocking work. A nested ``async def`` is reached separately, as its own
    scope, by the module-level walk.
    """
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _marker_lines(source: str) -> set[int]:
    """Line numbers whose COMMENT token is a well-formed opt-out marker.

    tokenize, not a substring scan, so the phrase inside a string literal cannot
    exempt a call. The comment must START with the marker AND carry a non-empty
    reason: a bare ``# on-loop-io-ok`` or a passing mention ("this is not
    on-loop-io-ok") would otherwise silence the gate, which is the opposite of
    an audit trail.
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and MARKER_RE.match(tok.string):
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # the AST parse of the same source decides parseability
    return lines


def _violations_in_source(source: str) -> list[tuple[int, str, int | None]]:
    """(line, family, provenance line) for every blocking call on the loop, sorted.

    The third element is the line that made the verdict true when it is not the
    call's own -- the client-binding line behind a client-object HTTP verb, or
    None when the call speaks for itself. It exists so the added-line rule can
    catch a change that adds the binding while leaving the call it flips untouched.

    Raises SyntaxError for an unparseable file: the caller turns that into a
    hard error, because under a shrink-only ratchet "could not parse" read as
    "clean" would invite a prune that deletes the file's real baseline entry.
    """
    tree = ast.parse(source)
    markers = _marker_lines(source)
    aliases, from_names, from_ctors = _import_names(tree)
    visible_for = _client_resolver(tree, aliases, from_ctors)
    found: set[tuple[int, int, str, int | None]] = set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        # Clients this coroutine can actually see: module globals, its own class's
        # instance state, and its own locals. A sibling coroutine's local and
        # another class's attribute are both deliberately invisible here.
        clients = visible_for(fn)
        for node in _own_nodes(fn):
            if not isinstance(node, ast.Call):
                continue
            verdict = _family(node, aliases, from_names, clients, from_ctors)
            if verdict is None:
                continue
            family, provenance = verdict
            # `await` is NOT an exemption. Nothing gated here is awaitable in this
            # tree -- there is no async DB driver in the dependencies, and no
            # awaitable requests/httpx-sync/subprocess/time.sleep exists at all --
            # so `await cur.execute(...)` runs the ENTIRE blocking call on the loop
            # and only then raises TypeError on the non-awaitable result. The stall
            # is identical either way. If an async driver is ever adopted, its call
            # sites carry the opt-out marker, which records WHICH driver made them
            # awaitable; a blanket exemption records nothing and hides this shape.
            end = node.end_lineno or node.lineno
            if markers & set(range(node.lineno, end + 1)):
                continue
            found.add((node.lineno, node.col_offset, family, provenance or None))
    return [(line, family, prov) for line, _col, family, prov in sorted(found)]


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in {"test", "tests"} for part in parts) or Path(rel).name.startswith("test_")


def _scan() -> dict[str, list[tuple[int, str, int | None]]]:
    """Repo-relative path -> violations, for every file that has any."""
    results: dict[str, list[tuple[int, str, int | None]]] = {}
    for path in sorted((ROOT / SCAN_ROOT).rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "_vendor" in Path(rel).parts or _is_test_path(rel):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "async def" not in source:
            continue
        try:
            found = _violations_in_source(source)
        except SyntaxError as exc:
            raise SystemExit(
                f"{rel} does not parse ({exc.msg}, line {exc.lineno}); refusing to "
                "read a parse failure as zero violations -- under a shrink-only "
                "ratchet that would invite a prune deleting the file's real "
                "baseline entry"
            )
        if found:
            results[rel] = found
    return results


def _read_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise SystemExit(
            f"baseline {path} is missing; restore it from git rather than "
            "regenerating it, since a regenerated baseline would silently absorb "
            "every offender added since it was recorded"
        )
    entries: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        count_str, _, rel = line.partition(" ")
        if not count_str.isdigit() or not rel:
            raise SystemExit(f"malformed baseline line: {line!r}")
        if rel in entries:
            raise SystemExit(
                f"duplicate baseline entry for {rel}; a later duplicate would "
                "silently override the recorded ceiling"
            )
        entries[rel] = int(count_str)
    return entries


def _write_baseline(path: Path, entries: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{count} {rel}\n" for rel, count in sorted(entries.items()))
    path.write_text(HEADER + body, encoding="utf-8")


def _shrunken_baseline(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """The refresh result: counts only ever lowered, clean/gone entries dropped."""
    survivors: dict[str, int] = {}
    for rel, recorded in baseline.items():
        now = current.get(rel, 0)
        if now > 0:
            survivors[rel] = min(recorded, now)
    return survivors


def _verdicts(
    violations: dict[str, list[tuple[int, str, int | None]]],
    baseline: dict[str, int],
    changed: set[str] | None,
    added: dict[str, set[int]] | None,
) -> tuple[list[str], list[str], dict[str, list[tuple[int, str, int | None]]], list[str]]:
    """(new_offenders, grown, added_line_offenders, shrunk) under the ratchet.

    ``changed`` None means the scope was undeterminable: judge the whole tree.
    ``added`` None means added-line info was unavailable: skip only that rule.
    """
    current = {rel: len(found) for rel, found in violations.items()}

    def in_scope(rel: str) -> bool:
        return changed is None or rel in changed

    new_offenders: list[str] = []
    grown: list[str] = []
    added_line_offenders: dict[str, list[tuple[int, str, int | None]]] = {}
    shrunk: list[str] = []
    for rel, count in sorted(current.items()):
        recorded = baseline.get(rel)
        if recorded is None:
            if in_scope(rel):
                new_offenders.append(rel)
        elif in_scope(rel):
            if count > recorded:
                grown.append(rel)
            elif added is not None:
                added_here = added.get(rel, set())
                # A violation counts as added when its own line is added OR when
                # the line that MADE it a violation is -- adding
                # `s = requests.Session()` turns an untouched `s.get(url)` into
                # blocking I/O, and pairing that with a removed violation would
                # otherwise keep the count level and pass.
                on_added = [
                    f
                    for f in violations[rel]
                    if f[0] in added_here or (f[2] is not None and f[2] in added_here)
                ]
                if on_added:
                    added_line_offenders[rel] = on_added
    for rel, recorded in sorted(baseline.items()):
        if current.get(rel, 0) < recorded and in_scope(rel):
            shrunk.append(rel)
    return new_offenders, grown, added_line_offenders, shrunk


def _report_sites(found: list[tuple[int, str, int | None]], rel: str) -> None:
    for line, family, provenance in found:
        print(f"  {rel}:{line}: blocking {family} call on the event loop")
        if provenance:
            print(f"    (a synchronous client bound at {rel}:{provenance} makes it one)")
    for family in sorted({f for _line, f, _prov in found}):
        print(f"    {family}: {FAMILY_REMEDY[family]}")


def run_gate(baseline_path: Path, update: bool) -> int:
    violations = _scan()
    current = {rel: len(found) for rel, found in violations.items()}
    baseline = _read_baseline(baseline_path)

    if update:
        survivors = _shrunken_baseline(baseline, current)
        pruned = len(baseline) - len(survivors)
        lowered = sum(1 for rel in survivors if survivors[rel] < baseline[rel])
        _write_baseline(baseline_path, survivors)
        print(f"pruned {pruned} entr(y/ies), lowered {lowered}; {len(survivors)} remain")
        return 0

    scope = _scope()
    changed, scope_label = scope.changed_paths()
    print(f"sync-io-in-async gate scope: {scope_label}", end="")
    print("" if changed is None else f" ({len(changed)} changed file(s))")
    added = scope.added_lines(scope_label) if changed is not None else None

    new_offenders, grown, added_line_offenders, shrunk = _verdicts(
        violations, baseline, changed, added
    )

    for rel in new_offenders:
        print(
            f"::error file={rel}::blocking IO inside an `async def` in a file the "
            "baseline does not list. It stalls every session's turn and can "
            "trip the 25s loop watchdog (#3057)."
        )
        _report_sites(violations[rel], rel)
    for rel in grown:
        print(
            f"::error file={rel}::on-loop blocking calls grew from {baseline[rel]} "
            f"to {current[rel]}. A new one must run in a thread, or carry an "
            f"inline `# {MARKER}: <why it cannot block>` comment."
        )
        _report_sites(violations[rel], rel)
    for rel, found in added_line_offenders.items():
        print(
            f"::error file={rel}::this change ADDS blocking call(s) inside an "
            "`async def` (the baseline covers only pre-existing lines)."
        )
        _report_sites(found, rel)
    if shrunk:
        print(
            f"::error::{len(shrunk)} baselined file(s) now have fewer on-loop "
            "blocking calls. Record the progress so the baseline keeps shrinking: "
            "python3 scripts/check_sync_io_in_async.py --update-baseline"
        )
        for rel in shrunk:
            print(f"  {rel}: {baseline[rel]} -> {current.get(rel, 0)}")

    if new_offenders or grown or added_line_offenders or shrunk:
        print(
            f"\nsync-io-in-async gate FAILED: {len(new_offenders)} new offender(s), "
            f"{len(grown)} grown count(s), {len(added_line_offenders)} file(s) with "
            f"new calls on added lines, {len(shrunk)} entr(y/ies) to prune."
        )
        return 1

    total = sum(baseline.values())
    print(
        "sync-io-in-async gate passed: nothing in scope blocks the loop outside "
        f"the baseline ({total} known call(s) in {len(baseline)} file(s) still "
        "listed)."
    )
    return 0


def report() -> int:
    """Print what is still on the loop, worst files first. No verdict."""
    violations = _scan()
    families: Counter[str] = Counter()
    for found in violations.values():
        families.update(family for _line, family, _prov in found)
    total = sum(families.values())
    print(f"{total} on-loop blocking call(s) in {len(violations)} file(s)")
    for family, count in families.most_common():
        print(f"  {family:<11} {count}")
    print("\nworst files:")
    ranked = sorted(violations.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for rel, found in ranked[:25]:
        breakdown = ", ".join(
            f"{fam} x{n}" for fam, n in Counter(f for _line, f, _prov in found).most_common()
        )
        print(f"  {len(found):>4}  {rel}  ({breakdown})")
    return 0


# -- self-test ---------------------------------------------------------------

_FLAGGED: dict[str, str] = {
    "db execute on a store handle": ("async def f(store):\n    store.db.execute('SELECT 1')\n"),
    "db commit on a connection": ("async def f(self):\n    self._conn.commit()\n"),
    "db executemany on a cursor": ("async def f(cur):\n    cur.executemany('INSERT', rows)\n"),
    "subprocess.run": ("import subprocess\nasync def f():\n    subprocess.run(['git'])\n"),
    "aliased subprocess module": (
        "import subprocess as sp\nasync def f():\n    sp.check_output(['git'])\n"
    ),
    "repo wrapper run_limited": (
        "from kiro_crew.sandbox import run_limited\n"
        "async def f():\n"
        "    run_limited(['git'])\n"
    ),
    "os.system": ("import os\nasync def f():\n    os.system('true')\n"),
    "requests verb": ("import requests\nasync def f():\n    requests.get('http://x')\n"),
    "urlopen through urllib.request": (
        "import urllib.request\nasync def f():\n    urllib.request.urlopen('http://x')\n"
    ),
    "from-imported time.sleep": ("from time import sleep\nasync def f():\n    sleep(5)\n"),
    "time.sleep": ("import time\nasync def f():\n    time.sleep(5)\n"),
    "multi-line call": (
        "import subprocess\n"
        "async def f():\n"
        "    subprocess.run(\n"
        "        ['git'],\n"
        "        check=True,\n"
        "    )\n"
    ),
    "nested async def is its own scope": (
        "import time\n"
        "async def outer():\n"
        "    async def inner():\n"
        "        time.sleep(1)\n"
        "    return inner\n"
    ),
    "inside a comprehension in the body": (
        "async def f(db, keys):\n    return [db.execute(k) for k in keys]\n"
    ),
    "marker inside a string is not a comment": (
        "import time\n" f"async def f():\n    time.sleep(1)  and '{MARKER}'\n"
    ),
    "lazy import inside the coroutine": (
        "async def f():\n    import requests\n    requests.post('http://x')\n"
    ),
    "an offload's ARGUMENTS still run on the loop": (
        "import asyncio\n"
        "import requests\n"
        "async def f(worker, url):\n"
        "    await asyncio.to_thread(worker, requests.get(url))\n"
    ),
    "awaiting a db call does not save the loop either": (
        "async def f(db):\n    await db.execute('SELECT 1')\n"
    ),
    "awaiting a sync http call does not save the loop": (
        "import requests\nasync def f(url):\n    await requests.get(url)\n"
    ),
    "awaiting time.sleep does not save the loop": (
        "import time\nasync def f():\n    await time.sleep(5)\n"
    ),
    "awaiting subprocess.run does not save the loop": (
        "import subprocess\nasync def f():\n    await subprocess.run(['git'])\n"
    ),
    "bare marker with no reason exempts nothing": (
        "import time\n" f"async def f():\n    time.sleep(1)  # {MARKER}\n"
    ),
    "marker mentioned inside a sentence exempts nothing": (
        "import time\n" f"async def f():\n    time.sleep(1)  # this is not {MARKER}: really\n"
    ),
    "marker with an empty reason exempts nothing": (
        "import time\n" f"async def f():\n    time.sleep(1)  # {MARKER}:\n"
    ),
    "requests.Session bound to a name": (
        "import requests\n" "async def f(url):\n" "    s = requests.Session()\n" "    s.get(url)\n"
    ),
    "client constructed inline": (
        "import requests\nasync def f(url):\n    requests.Session().get(url)\n"
    ),
    "httpx.Client in a with binding": (
        "import httpx\n"
        "async def f(url):\n"
        "    with httpx.Client() as c:\n"
        "        c.get(url)\n"
    ),
    "client stored on self": (
        "import requests\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        self._session = requests.Session()\n"
        "    async def f(self, url):\n"
        "        self._session.post(url)\n"
    ),
    "from-imported client constructor": (
        "from requests import Session\n"
        "async def f(url):\n"
        "    s = Session()\n"
        "    s.get(url)\n"
    ),
    "from-imported constructor called inline": (
        "from httpx import Client\nasync def f(url):\n    Client().get(url)\n"
    ),
    "a module-global sync client is visible inside a coroutine": (
        "import requests\n"
        "SESSION = requests.Session()\n"
        "async def f(url):\n"
        "    SESSION.get(url)\n"
    ),
    "an aliased sync constructor still counts": (
        "from requests import Session as Sess\n"
        "async def f(url):\n"
        "    s = Sess()\n"
        "    s.get(url)\n"
    ),
    "another class's async attribute does not erase this one's sync client": (
        "import requests\n"
        "import httpx\n"
        "class A:\n"
        "    def __init__(self):\n"
        "        self._client = requests.Session()\n"
        "    async def f(self, url):\n"
        "        self._client.get(url)\n"
        "class B:\n"
        "    def __init__(self):\n"
        "        self._client = httpx.AsyncClient()\n"
        "    async def g(self, url):\n"
        "        return await self._client.get(url)\n"
    ),
}

_CLEAN: dict[str, str] = {
    "an async driver opts out through the marker": (
        "async def f(db):\n" f"    await db.execute('SELECT 1')  # {MARKER}: aiosqlite, awaitable\n"
    ),
    "to_thread with the callable form": (
        "import asyncio\n"
        "async def f(db):\n"
        "    await asyncio.to_thread(db.execute, 'SELECT 1')\n"
    ),
    "to_thread with a lambda": (
        "import asyncio\n"
        "async def f(db):\n"
        "    await asyncio.to_thread(lambda: db.execute('SELECT 1'))\n"
    ),
    "run_in_executor": (
        "async def f(loop, db):\n" "    await loop.run_in_executor(None, lambda: db.commit())\n"
    ),
    "named pool helper": (
        "from kiro_crew.executors import run_in_embed_pool\n"
        "async def f(db):\n"
        "    await run_in_embed_pool(db.execute, 'SELECT 1')\n"
    ),
    "nested sync def is the offload idiom": (
        "import asyncio\n"
        "async def f(db):\n"
        "    def work():\n"
        "        db.execute('SELECT 1')\n"
        "    await asyncio.to_thread(work)\n"
    ),
    "non-db receiver is not a db call": ("async def f(workflow):\n    workflow.execute(step)\n"),
    "sync def is not gated": ("import time\ndef f():\n    time.sleep(5)\n"),
    "module scope is not gated": ("import time\ntime.sleep(5)\n"),
    "awaited asyncio.sleep": ("import asyncio\nasync def f():\n    await asyncio.sleep(5)\n"),
    "aiohttp is async": (
        "import aiohttp\n"
        "async def f(session):\n"
        "    async with session.get('http://x') as r:\n"
        "        return r\n"
    ),
    "marker opts out": (
        "import time\n" f"async def f():\n    time.sleep(1)  # {MARKER}: bounded, sub-second\n"
    ),
    "marker on a multi-line call's kwarg line": (
        "import subprocess\n"
        "async def f():\n"
        "    subprocess.run(\n"
        "        ['git'],\n"
        f"        check=True,  # {MARKER}: local git, bounded\n"
        "    )\n"
    ),
    "unrelated commit method": ("async def f(repo):\n    repo.commit(message)\n"),
    "sleep from asyncio is awaited": (
        "from asyncio import sleep\nasync def f():\n    await sleep(1)\n"
    ),
    "dict.get is not an HTTP verb": ("async def f(payload):\n    return payload.get('key')\n"),
    "httpx.AsyncClient verbs are awaitable and correct": (
        "import httpx\n"
        "async def f(url):\n"
        "    async with httpx.AsyncClient() as c:\n"
        "        return await c.get(url)\n"
    ),
    "an unrelated object's .send is not a client": ("async def f(bus, msg):\n    bus.send(msg)\n"),
    "from-imported AsyncClient is not a sync client": (
        "from httpx import AsyncClient\n"
        "async def f(url):\n"
        "    async with AsyncClient() as c:\n"
        "        return await c.get(url)\n"
    ),
    "a sibling coroutine's sync name does not leak into this one": (
        "import requests\n"
        "import httpx\n"
        "async def a(url):\n"
        "    session = requests.Session()\n"
        "    session.get(url)  # on-loop-io-ok: probe keeps this scope clean\n"
        "async def b(url):\n"
        "    session = httpx.AsyncClient()\n"
        "    return await session.get(url)\n"
    ),
    "an async binding disambiguates a same-named sync one in its own scope": (
        "import requests\n"
        "import httpx\n"
        "async def f(url):\n"
        "    c = requests.Session()\n"
        "    c = httpx.AsyncClient()\n"
        "    return await c.get(url)\n"
    ),
}


def _self_test() -> int:
    """Plant one probe per rule family; a broken rule fails here, not in prod."""
    failures: list[str] = []
    for label, source in _FLAGGED.items():
        if not _violations_in_source(source):
            failures.append(f"NOT flagged but should be: {label}")
    for label, source in _CLEAN.items():
        found = _violations_in_source(source)
        if found:
            failures.append(f"flagged but should be clean: {label} -> {found}")
    # The marker must not exempt a call it does not sit on.
    leak = (
        "import time\nasync def f():\n    time.sleep(1)\n    time.sleep(2)  # %s: bounded\n"
        % MARKER
    )
    if len(_violations_in_source(leak)) != 1:
        failures.append("marker leaked to a call on another line")
    for failure in failures:
        print(f"::error::self-test: {failure}")
    if failures:
        return 1
    print(f"self-test passed: {len(_FLAGGED)} flagged probes, {len(_CLEAN)} clean probes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="lower counts / prune entries that improved; never adds or raises",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="print the per-family and per-file breakdown of what is still on the loop",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the rule-family self-test instead of the gate",
    )
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    if args.report:
        return report()
    return run_gate(args.baseline, args.update_baseline)


if __name__ == "__main__":
    raise SystemExit(main())

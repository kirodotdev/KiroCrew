"""Every internal-secret call site into the dashboard must be allowlisted.

Registering a dashboard route and granting it internal access are two separate
manual edits, and nothing couples them. When they disagree the failure only shows
up at runtime: the caller sends ``X-Internal-Secret``, the middleware does not
find the path in either internal allowlist, and the call 403s. Nothing fails at
import, review, or build time. This test is the missing coupling.

Callers reach the dashboard two ways, and BOTH are in scope. Most go through the
``_post``/``_get``/``_put``/``_patch``/``_delete`` helpers in ``mcp_core``, which
attach the secret centrally. A few build their own ``urllib.request.Request`` and
set the header themselves (``mcp_computer``, ``cron_script``, the code-review-sage
driver). The middleware grants any of them only for paths in
``_STRICT_INTERNAL_API_PATHS`` or ``_MIXED_INTERNAL_API_PATHS``, matched by
prefix. A path in neither set falls through to ordinary token auth and is
unreachable for that caller. The population is therefore "sends the internal
secret", not "is an MCP module" -- naming it after MCP is what let
``mcp_computer.py`` sit outside an earlier revision of ``_SOURCES``.

EXHAUSTIVENESS IS THE WHOLE POINT, so this file must never quietly skip a call
site it cannot understand. A guard that skips the calls it cannot resolve reads
green while those calls go unchecked, which is worse than no guard because it
manufactures confidence. So every transport call must resolve to an ``/api``
path, and ``test_every_transport_call_resolves_to_a_path`` FAILS on any that does
not, except for the small reviewed set in ``_KNOWN_UNRESOLVED``.

``_SOURCES`` is a hand-listed set, so the same reasoning applies to the list
itself: ``test_no_secret_caller_is_unscanned`` fails if ANY module outside it
reaches the dashboard, by any of the three routes (setting the secret header,
bare-importing a helper, or calling one qualified). Keying that check on the
capability rather than on one call shape is what makes it escape-proof, and it
is why no same-name exemption list is needed: a module with its own private
``_get`` neither imports ``mcp_core`` nor sets the header, so it never matches.

Resolving the path argument therefore has to follow the shapes these modules
really use, not just literals:

* a literal, or an f-string whose interpolations are unknown at rest
* a module-level constant, including one interpolated into an f-string
  (``f"{_api_base()}{COMMAND_PATH}"`` in ``mcp_tools/browser.py``)
* a local variable built earlier in the same function, including one assembled by
  ``+`` concatenation (``"/api/apps/ops-mission-control" + _omc_path``) or
  extended with ``+=``

An unknown span becomes the marker ``{X}``. Where a marker occupies a whole path
segment the path is still CONCRETE and is matched against the allowlists exactly
as the middleware would. Where a marker is glued to the tail of a segment the
runtime path is only known up to that point, so the path is treated as a PREFIX
and the assertion weakens honestly: some allowlist entry must begin with the
literal head. That is what covers the Ops Mission Control family, whose thirteen
endpoints are allowlisted individually and deliberately not by bare prefix.

Scope, and what this deliberately does not check:

* Membership, not bucket choice. It asserts a path is in strict OR mixed, not
  that it is in the RIGHT one. A browser-polled route wrongly placed in strict
  passes here and still hard-denies forwarded browsers at runtime.
* For a PREFIX path it proves the family is granted, not that the specific
  endpoint is. An unallowlisted endpoint added under an already-granted prefix
  would pass here.
* Bucket disjointness already has its own guard in
  ``test_internal_path_bucket_disjointness.py``. This file does not repeat it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    _STRICT_INTERNAL_API_PATHS,
)
from kiro_crew.dashboard.token_auth import _BYPASS_EXACT

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
_CORE = _SRC / "mcp_core.py"

# The helpers that carry X-Internal-Secret. Defined in mcp_core; called either
# bare (imported) or qualified as ``mcp_core._get(...)``.
_TRANSPORT_HELPERS = frozenset({"_post", "_get", "_put", "_patch", "_delete"})

# Stands in for a span of the path that is only known at runtime.
_UNKNOWN = "{X}"

# Modules that talk to the dashboard: the top-level MCP modules, plus the whole
# mcp_tools package, where most tool implementations now live. Globbed rather
# than listed so a new tool module is scanned the day it is added.
_SOURCES = (
    _CORE,
    _SRC / "mcp_shared.py",
    _SRC / "mcp_dashboard.py",
    _SRC / "mcp_computer.py",
    _SRC / "cli_commands.py",
    _SRC / "cron_script.py",
    _SRC / "cron_trigger.py",
    _SRC / "apps/builtins/code_review_sage/sage_lib/review_driver.py",
    _SRC / "computer_use/screencast.py",
    *sorted((_SRC / "mcp_tools").glob("*.py")),
)

# Ceiling on how many possible values one expression may resolve to. Past it the
# site is reported unresolved rather than silently narrowed to a subset.
_MAX_CANDIDATES = 24

# The header that makes a request an internal one. A module that sets it reaches
# the dashboard without going through mcp_core's helpers at all. Matched with
# either quote style AND via a constant holding the name, because
# ``computer_use/screencast.py`` assigns it as ``headers[FRAME_SECRET_HEADER]``
# and a literal-only pattern cannot see that.
_SECRET_HEADER = "X-Internal-Secret"
_SETS_SECRET = re.compile(
    rf"""["']{_SECRET_HEADER}["']\s*:|headers\[\s*["']{_SECRET_HEADER}["']\s*\]\s*="""
)

# Call sites whose path genuinely cannot be known from the source, keyed by
# module and enclosing function (not line number, which drifts). Every entry is a
# generic wrapper taking the path as a PARAMETER, so the path belongs to its
# callers -- and ``_wrapper_path_arg_index`` scans those callers, which is what
# earns the exemption. That set is derived from the code, not from this list: a
# wrapper is scanned because it forwards a path, whether or not its own site
# resolves. RATCHET: may only shrink, and a stale entry fails the test. Do not add
# a site here to silence it; a call whose path cannot be resolved is a call this
# guard cannot vouch for.
_KNOWN_UNRESOLVED = frozenset(
    {
        # ``_send(path)`` is the central helper every verb now delegates to; its
        # nested ``_once(base)`` builds the Request, so the site is attributed here.
        "mcp_core.py:_send",
        "mcp_core.py:_post",
        "mcp_core.py:_get",
        "mcp_core.py:_put",
        "mcp_core.py:_patch",
        "mcp_core.py:_delete",
        "mcp_dashboard.py:_get_rows",
        "cron_script.py:_post",
        "review_driver.py:_api_request",
    }
)

# Secret-transport paths knowingly unreachable, as an explicit ratchet. Empty
# here: every resolved call site matches an allowlist today. It is kept, empty,
# so the exception set stays visible rather than implied.
_KNOWN_UNREACHABLE: frozenset[str] = frozenset()


def _normalise(path: str) -> str:
    """Drop any query string and collapse interpolated segments to the marker."""
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", _UNKNOWN, path)


def _str_assignments(nodes: list[ast.stmt]) -> dict[str, list[str]]:
    """Map ``NAME = "..."`` string constants from a list of statements."""
    out: dict[str, list[str]] = {}
    for node in nodes:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, [value.value])
    return out


class _TooManyCandidates(Exception):
    """A name has more possible values than the ceiling allows.

    Raised instead of truncating. Truncation would drop a real path and leave the
    guard green, which is the silent skip this file exists to prevent.
    """


def _join(left: list[str], right: list[str]) -> list[str]:
    if len(left) * len(right) > _MAX_CANDIDATES:
        raise _TooManyCandidates
    return [a + b for a in left for b in right]


def _resolve(
    node: ast.expr, scopes: list[dict[str, list[str]]], core: dict[str, list[str]]
) -> list[str]:
    """Resolve an expression to candidate strings, marking unknown spans.

    Returns a list because a name reassigned on several branches has one possible
    value per branch and every one of them is a real call path. Nothing is
    truncated: past ``_MAX_CANDIDATES`` this raises and the site is reported
    unresolved instead.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else [_UNKNOWN]
    if isinstance(node, ast.JoinedStr):
        joined = [""]
        for value in node.values:
            joined = _join(joined, _resolve(value, scopes, core))
        return joined
    if isinstance(node, ast.FormattedValue):
        return _resolve(node.value, scopes, core)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _join(
            _resolve(node.left, scopes, core), _resolve(node.right, scopes, core)
        )
    if isinstance(node, ast.Name):
        for scope in reversed(scopes):
            if node.id in scope:
                return scope[node.id]
        return [_UNKNOWN]
    if isinstance(node, ast.Attribute):
        # ``mcp_core._CREW_READ_PATH`` -- resolve against mcp_core's constants.
        return core.get(node.attr, [_UNKNOWN])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args:
        # A zero-arg URL builder, pre-resolved into scope under ``name()``.
        for scope in reversed(scopes):
            if f"{node.func.id}()" in scope:
                return scope[f"{node.func.id}()"]
    return [_UNKNOWN]


def _url_builders(
    tree: ast.Module, scopes: list[dict[str, list[str]]], core: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Zero-arg module functions returning a URL, keyed ``name()``.

    ``screencast.py`` passes ``Request(_ingress_url(), ...)``, so the path lives in
    a builder's return rather than at the call -- invisible without this.
    """
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.args.args or node.args.kwonlyargs:
            continue
        cands: list[str] = []
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or inner.value is None:
                continue
            try:
                cands.extend(_resolve(inner.value, scopes, core))
            except _TooManyCandidates:
                cands = [_UNKNOWN]
                break
        if cands:
            out[f"{node.name}()"] = cands
    return out


def _function_locals(
    fn: ast.AST, scopes: list[dict[str, list[str]]], core: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Candidate string values for names assigned inside one function body.

    A name whose candidates overflow is bound to the unknown marker rather than a
    truncated list, so a call using it reports unresolved instead of passing on a
    subset of its real paths.
    """
    local: dict[str, list[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        found = _resolve(node.value, [*scopes, local], core)
                    except _TooManyCandidates:
                        local[target.id] = [_UNKNOWN]
                        continue
                    local.setdefault(target.id, []).extend(found)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            base = local.get(node.target.id, [_UNKNOWN])
            try:
                local[node.target.id] = base + _join(
                    base, _resolve(node.value, [*scopes, local], core)
                )
            except _TooManyCandidates:
                local[node.target.id] = [_UNKNOWN]
    return {k: v[:_MAX_CANDIDATES] if len(v) <= _MAX_CANDIDATES else [_UNKNOWN]
            for k, v in local.items()}


def _sends_secret(text: str, tree: ast.Module) -> bool:
    """True when a module attaches the internal secret to an outgoing request.

    Three spellings: the header name written literally, or held in a module
    constant used either as a subscript target or as a dict-literal key.
    """
    if _SETS_SECRET.search(text):
        return True
    names = {
        name
        for name, values in _str_assignments(tree.body).items()
        if values and values[0] == _SECRET_HEADER
    }
    if not names:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Name) and key.id in names for key in node.keys
        ):
            return True
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Name)
                and target.slice.id in names
            ):
                return True
    return False


def _wrapper_path_arg_index() -> dict[str, dict[str, int]]:
    """Path-argument position for every wrapper that forwards a path to the wire.

    Keyed by MODULE then function, because these names are not unique across the
    tree: ``mcp_core._send(path)`` forwards a path, while ``cron_script._send(msg)``
    is an unrelated JSON-RPC sender taking a dict. A flat name-keyed index treated
    the latter as a transport call and reported its callers unresolved.

    Derived from the CODE -- any function taking a ``path`` parameter that itself
    performs a transport call -- and deliberately NOT from ``_KNOWN_UNRESOLVED``.
    Those are two different questions, and conflating them left a hole: a wrapper
    whose own site happens to resolve is absent from that list, so its callers went
    unscanned and a literal they passed was never checked. ``cli_commands._request``
    is exactly that shape, and its path sits at index 1, not 0.
    """
    index: dict[str, dict[str, int]] = {}
    for src in _SOURCES:
        tree = ast.parse(src.read_text(encoding="utf-8"))
        per_module = index.setdefault(src.name, {})
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in node.args.args]
            if args and args[0] in ("self", "cls"):
                args = args[1:]
            if "path" not in args:
                continue
            forwards = any(
                isinstance(inner, ast.Call)
                and _plain_callee(inner.func) in (_TRANSPORT_HELPERS | {"Request"})
                for inner in ast.walk(node)
            )
            if forwards:
                per_module[node.name] = args.index("path")
    return index


def _plain_callee(func: ast.expr) -> str | None:
    """The bare name a call targets, whether written plain or attribute-style."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


# The parameter every transport helper and path-forwarding wrapper names, so a
# keyword-passed path is found whatever the call's positional shape.
_PATH_PARAM = "path"


def _path_expr(call: ast.Call, at_arg: int) -> ast.expr | None:
    """The expression carrying the path, from either argument shape.

    Recognition keys on the callee, not on the presence of positional args, so a
    keyword-only call reaches the resolver instead of being skipped before it.
    """
    if at_arg < len(call.args):
        return call.args[at_arg]
    for kw in call.keywords:
        if kw.arg == _PATH_PARAM:
            return kw.value
    return None


def _called_helper_name(func: ast.expr, wrappers: dict[str, int]) -> str | None:
    """The transport helper, ``Request``, or exempted wrapper this call targets."""
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return None
    if name in _TRANSPORT_HELPERS or name == "Request" or name in wrappers:
        return name
    return None


def _scan() -> tuple[dict[str, set[str]], set[str]]:
    """Walk every source, returning (path -> call sites, unresolved site keys).

    An unresolved site is keyed ``module.py:enclosing_function`` so the reviewed
    exception list does not churn when line numbers move.
    """
    core = _str_assignments(ast.parse(_CORE.read_text(encoding="utf-8")).body)
    wrapper_index = _wrapper_path_arg_index()
    paths: dict[str, set[str]] = {}
    unresolved: set[str] = set()

    for src in _SOURCES:
        tree = ast.parse(src.read_text(encoding="utf-8"))
        wrappers = wrapper_index.get(src.name, {})
        module_scope = _str_assignments(tree.body)
        module_scope.update(_url_builders(tree, [module_scope], core))

        def visit(
            node: ast.AST,
            scopes: list[dict[str, list[str]]],
            fname: str,
            enclosing: list[ast.AST],
        ) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    inner = [*scopes, _function_locals(child, scopes, core)]
                    visit(child, inner, child.name, [*enclosing, child])
                    continue
                if isinstance(child, ast.Call):
                    called = _called_helper_name(child.func, wrappers)
                    if called is not None:
                        found = False
                        expr = _path_expr(child, wrappers.get(called, 0))
                        if expr is not None:
                            try:
                                cands = _resolve(expr, scopes, core)
                            except _TooManyCandidates:
                                cands = []
                            for cand in cands:
                                norm = _normalise(cand)
                                at = norm.find("/api/")
                                if at >= 0:
                                    paths.setdefault(norm[at:], set()).add(
                                        f"{src.name}:{child.lineno}"
                                    )
                                    found = True
                        if not found:
                            owner = next(
                                (
                                    fn.name
                                    for fn in reversed(enclosing)
                                    if isinstance(
                                        fn, (ast.FunctionDef, ast.AsyncFunctionDef)
                                    )
                                    and fn.name in wrappers
                                ),
                                fname,
                            )
                            unresolved.add(f"{src.name}:{owner}")
                visit(child, scopes, fname, enclosing)

        visit(tree, [module_scope], "<module>", [])
    return paths, unresolved


def _call_sites() -> dict[str, set[str]]:
    return _scan()[0]


def _unresolved_sites() -> set[str]:
    return _scan()[1]


def _is_allowlisted(path: str) -> bool:
    """Mirror the middleware's prefix-or-exact match over both internal sets."""
    allow = _STRICT_INTERNAL_API_PATHS | _MIXED_INTERNAL_API_PATHS
    return any(path == entry or path.startswith(entry + "/") for entry in allow)


def _is_reachable(path: str) -> bool:
    """Allowlisted, or auth-exempt outright via ``token_auth._BYPASS_EXACT``."""
    return _is_allowlisted(path) or path in _BYPASS_EXACT


def _is_prefix_path(path: str) -> bool:
    """True when a marker is glued to a segment tail, so the path is a prefix.

    A marker that occupies a whole segment (``/api/artifacts/{X}/comments``) is
    an opaque id and the path is still concrete. A marker glued to text
    (``/api/apps/ops-mission-control{X}``) means the rest of the segment is only
    known at runtime.
    """
    at = path.find(_UNKNOWN)
    return at > 0 and path[at - 1] != "/"


def _is_granted(path: str) -> bool:
    """Reachability for a concrete path; family-granted for a prefix path."""
    if not _is_prefix_path(path):
        return _is_reachable(path)
    head = path[: path.find(_UNKNOWN)]
    allow = _STRICT_INTERNAL_API_PATHS | _MIXED_INTERNAL_API_PATHS
    return _is_reachable(head) or any(entry.startswith(head) for entry in allow)


class TestMcpCallSiteAuthCoverage:
    def test_every_transport_call_resolves_to_a_path(self):
        """No transport call may be skipped just because it looks hard.

        Skipping is the failure this file exists to prevent: the call goes
        unchecked while the suite reads green. Resolve the indirection (a local
        variable, a module constant, a concatenation are all handled), or route
        the call through a wrapper whose callers pass a concrete path.
        """
        strays = sorted(_unresolved_sites() - _KNOWN_UNRESOLVED)
        assert not strays, (
            "transport call site(s) whose /api path could not be resolved, so "
            f"this guard cannot vouch for them: {strays}. Resolve the "
            "indirection rather than adding them to _KNOWN_UNRESOLVED."
        )

    def test_known_unresolved_ratchet_has_no_stale_entries(self):
        """The wrapper exception list may only shrink."""
        stale = sorted(_KNOWN_UNRESOLVED - _unresolved_sites())
        assert not stale, (
            f"_KNOWN_UNRESOLVED lists site(s) that now resolve: {stale}. "
            "Remove them so the list keeps tightening."
        )

    def test_every_resolved_path_is_granted(self):
        """A path in neither allowlist 403s for every MCP caller.

        Fix by adding it to ``_STRICT_INTERNAL_API_PATHS`` (MCP-only) or
        ``_MIXED_INTERNAL_API_PATHS`` (also polled by the browser) in
        ``dashboard/server.py`` -- not by listing it as a known gap.
        """
        gaps = {
            path: sorted(sites)
            for path, sites in _call_sites().items()
            if not _is_granted(path) and path not in _KNOWN_UNREACHABLE
        }
        assert not gaps, (
            "MCP call site(s) reach a path that is in no internal allowlist, so "
            f"every MCP call to them 403s: {sorted(gaps)}. Call sites: {gaps}"
        )

    def test_known_unreachable_ratchet_has_no_stale_entries(self):
        """The known-gap ratchet may only shrink, so a fixed path is de-listed."""
        unreachable = {p for p in _call_sites() if not _is_granted(p)}
        stale = sorted(_KNOWN_UNREACHABLE - unreachable)
        assert not stale, (
            f"_KNOWN_UNREACHABLE lists path(s) that are now reachable: {stale}. "
            "Remove them so the ratchet keeps tightening."
        )

    def test_extraction_is_not_vacuous(self):
        """If extraction silently found nothing, the coverage test passes free."""
        found = _call_sites()
        assert len(found) >= 25, f"suspiciously few call sites found: {sorted(found)}"

    def test_extraction_covers_every_call_shape(self):
        """Pin one real path per argument shape and per source module.

        A regression that blinded any single shape would otherwise just shrink
        the set quietly, and the floor above is slack enough to absorb it.
        """
        found = _call_sites()
        for path, shape in (
            ("/api/spawn", "literal arg, qualified mcp_core._post"),
            ("/api/chat/folders", "literal arg, bare imported _post"),
            ("/api/apps/issue-radar/investigation", "the _put helper"),
            ("/api/artifacts/{X}/comments", "f-string with an interpolation"),
            ("/api/session-tool-policy", "direct urllib Request, leading f-string"),
            ("/api/apps/issue-radar/crew", "path held in a module-level constant"),
            ("/api/browser/command", "module constant interpolated into an f-string"),
            ("/api/computer-use/invoke", "own Request + header, outside mcp_tools"),
            ("/api/computer-use/frame", "path returned by a zero-arg URL builder"),
            ("/api/chat/slots", "literal passed to an exempted wrapper's caller"),
            ("/api/crons/{X}/run", "a non-MCP caller (cron_trigger)"),
            ("/api/artifacts/{X}/versions/{X}", "local variable assigned in a branch"),
            ("/api/apps/ops-mission-control{X}", "local built by + concatenation"),
        ):
            assert path in found, f"{path} not extracted -- {shape} is now blind"

    def test_keyword_passed_paths_are_visited(self):
        """A transport call with no positional args must still be resolved.

        Recognition keys on the callee rather than on argument shape, so a
        keyword-only call reaches the resolver instead of being skipped before
        detection -- which would pass green with the call site unguarded.
        """
        kw = ast.parse('_post(path="/api/kw-shape-probe", body={})').body[0].value
        expr = _path_expr(kw, 0)
        assert expr is not None, (
            "a keyword-passed path is invisible, so such a call would be "
            "skipped before detection and read green while unguarded"
        )
        assert _resolve(expr, [{}], {}) == ["/api/kw-shape-probe"], (
            "the keyword path expression did not resolve to its literal"
        )
        bare = ast.parse("_post()").body[0].value
        assert _path_expr(bare, 0) is None, (
            "a call carrying no path must yield no expression, so the walker "
            "records it unresolved rather than silently finding nothing"
        )

    def test_the_walker_visits_a_keyword_only_transport_call(self, monkeypatch, tmp_path):
        """End-to-end: shape must not decide whether a call is examined.

        ``_path_expr`` is unit-pinned above, but the escape this closes lived in
        the walker's entry condition, so it has to be asserted through ``_scan``.
        """
        mod = tmp_path / "kwshape.py"
        mod.write_text(
            "from kiro_crew.mcp_core import _post\n"
            "def go():\n"
            '    return _post(path="/api/kw-walker-probe", body={})\n',
            encoding="utf-8",
        )
        monkeypatch.setitem(globals(), "_SOURCES", (_CORE, mod))
        paths, unresolved = _scan()
        assert "/api/kw-walker-probe" in paths, (
            "the walker skipped a transport call whose path is keyword-passed, "
            f"so it is unguarded and nothing failed; unresolved={sorted(unresolved)}"
        )

    def test_secret_header_is_detected_in_a_dict_literal(self):
        """A constant header key inside a dict literal marks a sender.

        ``{FRAME_SECRET_HEADER: token}`` is neither literal header text nor a
        subscript assignment, so without this a module sending the secret that
        way would never be added to the scanned set.
        """
        src = 'H = "X-Internal-Secret"\ndef go(t):\n    return {H: t}\n'
        assert _sends_secret(src, ast.parse(src)), (
            "a dict-literal header key is invisible, so a sender spelled that "
            "way would sit outside _SOURCES entirely"
        )
        # Must still DEFINE the header constant, or the predicate returns early
        # at the empty-names guard and this passes without testing membership.
        other = (
            'H = "X-Internal-Secret"\nOTHER = "x-unrelated"\n'
            "def go(t):\n    return {OTHER: t}\n"
        )
        assert not _sends_secret(other, ast.parse(other)), (
            "an unrelated constant used as a dict key must not count as a sender"
        )

    def test_prefix_paths_are_distinguished_from_concrete_ones(self):
        """Guard the classifier: reading a prefix as concrete would false-FAIL,
        and reading a concrete path as a prefix would weaken a real check."""
        assert _is_prefix_path("/api/apps/ops-mission-control{X}"), (
            "a marker glued to a segment tail is a prefix, not a concrete path"
        )
        assert not _is_prefix_path("/api/artifacts/{X}/comments"), (
            "a marker filling a whole segment is an opaque id, still concrete"
        )
        assert not _is_prefix_path("/api/spawn"), (
            "a path with no marker at all is concrete"
        )

    def test_reachability_mirror_matches_the_middleware(self):
        """Guard the mirror itself: a wrong mirror passes everything silently."""
        entry = next(iter(_STRICT_INTERNAL_API_PATHS))
        assert _is_allowlisted(entry), "exact match must be allowlisted"
        assert _is_allowlisted(entry + "/child"), "prefix + / must be allowlisted"
        assert not _is_allowlisted(entry + "-sibling"), (
            "a shared string prefix is not a shared path prefix -- the / matters"
        )

    def test_no_secret_caller_is_unscanned(self):
        """Any module reaching the dashboard must be in ``_SOURCES``.

        Keyed on capability, not on one call shape: setting the secret header,
        bare-importing a helper, or calling one qualified all count. Detecting
        only qualified calls is what let ``mcp_computer.py`` (its own ``Request``
        plus the header) and any bare-import caller escape unnoticed.
        """
        scanned = {p.resolve() for p in _SOURCES}
        unscanned: dict[str, list[str]] = {}
        for path in sorted(_SRC.rglob("*.py")):
            rel = path.relative_to(_SRC).as_posix()
            if path.resolve() in scanned:
                continue
            if "/tests/" in f"/{rel}" or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            reasons: list[str] = []
            if _sends_secret(text, tree):
                reasons.append(f"sets {_SECRET_HEADER}")
            aliases = {"mcp_core", "core"}
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
                    "mcp_core"
                ):
                    for alias in node.names:
                        if alias.name in _TRANSPORT_HELPERS:
                            imported.add(alias.asname or alias.name)
                        elif alias.name == "mcp_core":
                            aliases.add(alias.asname or alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith("mcp_core"):
                            aliases.add(alias.asname or alias.name.split(".")[-1])
            if imported:
                reasons.append(f"imports {sorted(imported)}")
            qualified = sorted(
                {
                    node.func.attr
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _TRANSPORT_HELPERS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in aliases
                }
            )
            if qualified:
                reasons.append(f"calls {qualified}")
            if reasons:
                unscanned[rel] = reasons
        assert not unscanned, (
            "module(s) reach the dashboard with the internal secret but are not "
            f"scanned by this guard, so their call sites are unchecked: "
            f"{unscanned}. Add them to _SOURCES."
        )

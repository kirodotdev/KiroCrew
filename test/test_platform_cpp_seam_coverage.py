"""Anti-rot gate for the CPP seam: every extension point is wired or RESERVED.

The failure mode this suite exists to prevent: a ``PlatformContext`` field is
added (or an interface method is), an edition author reads the dataclass, writes
an adapter, ``dataclasses.replace``\\ s it in — and gets **silence**. No boot
warning, no log line, no failing test, because nothing in the core ever reads
the field. The seam looks like a contract but is inert.

So the contract is made total here. For every field of ``PlatformContext``,
exactly one of the following must hold:

* it has at least one **consumption site** in non-``platform/`` core code, or
* it is declared inert in ``context.RESERVED_SLOTS``.

And the reservation cannot rot in either direction:

* a reserved slot that GAINS a consumption site fails
  :func:`test_reserved_slots_have_no_consumption_site` — so wiring a slot forces
  its ``RESERVED_SLOTS`` entry to be deleted deliberately, in the same change;
* a new field with NO consumption site fails
  :func:`test_every_context_field_is_wired_or_reserved` — so it cannot quietly
  become the next dead seam.

``context.RESERVED_METHODS`` gets the same treatment one level finer, for
individual methods on fields that are otherwise live (today: the unread
``AgentRuntime.managed_mcp_servers`` and ``IdentityProvider.whoami``/``issuer``).

Consumption sites are discovered by **static analysis of the real source tree**
(``ast``), not from a hand-maintained list — a list would itself rot. See
:func:`_find_seam_reads`.

The same shape guards the one accessor that opts OUT of the fail-closed contract.
``context.installed_context()`` answers ``None`` instead of refusing to compose,
which is safe only where the caller's no-context answer is already the
conservative one — a property of the CALLER, so the function cannot check it.
Every call site must appear in ``context.PEEK_CALLERS`` with that reason
(:class:`TestInstalledContextPeeks`), and an entry whose call site is gone fails
too, so the map cannot outlive the callers it permits.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import pytest

import kiro_crew
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    RESERVED_METHODS,
    RESERVED_SLOTS,
    PlatformCompositionError,
    build_default_context,
    reset_context,
    set_context,
)
from kiro_crew.platform.context import (
    _RESERVED_DEFAULT_ADAPTERS,
    _RESERVED_WARNED,
    PEEK_CALLERS,
    PlatformContext,
    _reserved_slot_is_default,
)
from kiro_crew.platform.governance import (
    CAPABILITY,
    SCOPE_CATALOG,
    parse_policy,
    parse_profile,
    resolve,
)
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal

# ── Static-analysis configuration ──

_SRC_ROOT = Path(kiro_crew.__file__).resolve().parent

# Directories under the package that are NOT core consumption sites.
#  - ``platform``: the seam's own definition/composition code. A read here is
#    plumbing (``bootstrap`` validating ``contract_version``, ``defaults``
#    delegating), never a core consumer, so it must not count as "wired".
#  - ``_vendor``: vendored third-party source (llama-cpp-python) — not ours, and
#    it has unrelated attributes named ``embeddings``/``cfg``.
_EXCLUDED_DIRS = frozenset({"platform", "_vendor"})

# Callables that return a ``PlatformContext``. An attribute access on a call to
# one of these is a seam read: ``current_context().identity``.
_CONTEXT_FACTORIES = frozenset(
    {
        "current_context",
        "build_default_context",
        "bootstrap_context",
        "boot_platform",
    }
)

# Local variable names that conventionally hold a ``PlatformContext``, for the
# ``ctx = current_context(); ctx.jail`` two-step (used by ``cli.py`` and
# ``cli_doctor.py``). Kept narrow on purpose: a broad name set would count
# unrelated ``ctx.foo`` reads (e.g. an aiohttp request ctx) as seam wiring and
# make this gate pass vacuously.
_CONTEXT_VAR_NAMES = frozenset({"ctx", "_ctx", "pctx", "platform_ctx"})

# Fields whose ONLY consumer is the composition root itself (``bootstrap.py``),
# by design — they are boot-protocol carriers, not leaf-module extension points.
# These are genuinely LIVE: overriding them changes real behavior at boot, which
# is why they are neither "wired in a leaf module" nor "reserved/inert".
#
# This is a narrow, enumerated allowance, not an escape hatch: it names exactly
# two fields with an explicit reason each, and
# ``test_boot_consumed_fields_are_really_read_by_bootstrap`` proves each one is
# actually read there — so a field cannot be parked here to dodge the gate.
_BOOT_CONSUMED_IN_PLATFORM: Dict[str, str] = {
    "contract_version": (
        "bootstrap._assert_contract compares it against CONTRACT_VERSION and "
        "refuses to compose a mismatched companion — a boot-protocol guard, not a "
        "behavior adapter, so no leaf module reads it."
    ),
    "publish": (
        "bootstrap_context calls publish.register_publish_providers() once after "
        "the context installs; the override's effect is the registry side effect "
        "it performs there, so there is no later read."
    ),
}


def _is_context_expr(node: ast.expr) -> bool:
    """True when *node* evaluates to a ``PlatformContext`` by our conventions."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in _CONTEXT_FACTORIES
        if isinstance(func, ast.Attribute):
            return func.attr in _CONTEXT_FACTORIES
        return False
    if isinstance(node, ast.Name):
        return node.id in _CONTEXT_VAR_NAMES
    return False


def _core_source_files() -> List[Path]:
    """Every non-excluded ``.py`` file under ``src/kiro_crew``."""
    return [
        path
        for path in sorted(_SRC_ROOT.rglob("*.py"))
        if not _EXCLUDED_DIRS & set(path.relative_to(_SRC_ROOT).parts)
    ]


def _rel(path: Path) -> str:
    return str(path.relative_to(_SRC_ROOT.parent))


def _find_seam_reads(field_names: Set[str]) -> Dict[str, List[str]]:
    """Map each context field name → ``["module.py:LINE", ...]`` read sites.

    Recognizes both documented CPP read shapes:

        current_context().identity.status()      → identity
        ctx = current_context(); ctx.jail        → jail

    Deliberately conservative — it under-counts rather than over-counts. A
    genuinely-wired field reached by some *other* shape would show up as an
    unexpected failure here (loud, fixable by teaching this scanner the shape)
    rather than as a silently-passing gate, which is the outcome that matters:
    this test's job is to make inertness impossible to miss.
    """
    reads: Dict[str, List[str]] = {name: [] for name in field_names}
    for path in _core_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr in field_names and _is_context_expr(node.value):
                reads[node.attr].append(f"{_rel(path)}:{node.lineno}")
    return reads


def _find_method_reads(field_names: Set[str]) -> Dict[str, List[str]]:
    """Map ``"field.method"`` → read sites, for methods reached via the seam.

    Shape: ``<ctx-expr>.<field>.<method>``. Only direct attribute access counts;
    ``getattr(ctx.identity, name)`` is deliberately not chased (dynamic, and not
    a documented read shape).
    """
    method_reads: Dict[str, List[str]] = {}
    for path in _core_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            inner = node.value
            if not isinstance(inner, ast.Attribute):
                continue
            if inner.attr in field_names and _is_context_expr(inner.value):
                method_reads.setdefault(f"{inner.attr}.{node.attr}", []).append(
                    f"{_rel(path)}:{node.lineno}"
                )
    return method_reads


# ── installed_context() peek discovery ──
#
# The peek scan differs from the seam scan above in one deliberate way: it does
# NOT exclude ``platform/``. A seam READ inside the platform package is plumbing,
# but a peek inside it is a real bypass of the module's own fail-closed contract
# (``redact_log_via_context`` and ``governance.active_policy_distribution`` are
# both in there), so excluding the directory would hide two of today's three
# callers and make the gate pass while permitting exactly what it forbids.
_PEEK_FUNC = "installed_context"


def _peek_scanned_files() -> List[Path]:
    """Every ``.py`` under ``src/kiro_crew`` except vendored third-party source."""
    return [
        path
        for path in sorted(_SRC_ROOT.rglob("*.py"))
        if "_vendor" not in path.relative_to(_SRC_ROOT).parts
    ]


def _module_key(path: Path) -> str:
    """``src/kiro_crew/platform/context.py`` → ``platform/context.py``."""
    return path.relative_to(_SRC_ROOT).as_posix()


def _calls_with_scope(tree: ast.AST) -> Iterator[Tuple[str, ast.Call]]:
    """Yield ``(enclosing-scope-qualname, Call)`` for every call in *tree*.

    The scope is the dotted chain of enclosing ``def``/``class`` names, so a peek
    is attributed to the function a reader would name it by. Manual descent rather
    than ``ast.walk`` because ``walk`` discards the nesting the attribution needs.
    """

    def _descend(node: ast.AST, scope: Tuple[str, ...]) -> Iterator[Tuple[str, ast.Call]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from _descend(child, scope + (child.name,))
                continue
            if isinstance(child, ast.Call):
                yield ".".join(scope), child
            yield from _descend(child, scope)

    yield from _descend(tree, ())


def _is_peek_call(node: ast.Call) -> bool:
    """True for ``installed_context()`` and ``<module>.installed_context()``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _PEEK_FUNC
    if isinstance(func, ast.Attribute):
        return func.attr == _PEEK_FUNC
    return False


def _peeks_in_source(source: str, module: str) -> Dict[str, List[str]]:
    """Map ``"<module>::<function>"`` → ``["<module>:LINE", ...]`` for one file.

    Split out from :func:`_find_installed_context_peeks` so the scanner can be
    exercised against a synthetic source string — the coherence guard that keeps
    the gate from passing because it silently found nothing.
    """
    peeks: Dict[str, List[str]] = {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return peeks
    for scope, node in _calls_with_scope(tree):
        if not _is_peek_call(node):
            continue
        peeks.setdefault(f"{module}::{scope or '<module>'}", []).append(f"{module}:{node.lineno}")
    return peeks


def _find_installed_context_peeks() -> Dict[str, List[str]]:
    """Every ``installed_context()`` call site in the package, keyed by caller."""
    peeks: Dict[str, List[str]] = {}
    for path in _peek_scanned_files():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            continue
        if _PEEK_FUNC not in source:  # cheap pre-filter before the parse
            continue
        for key, sites in _peeks_in_source(source, _module_key(path)).items():
            peeks.setdefault(key, []).extend(sites)
    return peeks


@pytest.fixture(scope="module")
def peek_sites() -> Dict[str, List[str]]:
    return _find_installed_context_peeks()


@pytest.fixture(scope="module")
def context_field_names() -> Set[str]:
    """Every ``PlatformContext`` field, driven off the dataclass itself."""
    return {f.name for f in dataclasses.fields(PlatformContext)}


@pytest.fixture(scope="module")
def field_reads(context_field_names: Set[str]) -> Dict[str, List[str]]:
    return _find_seam_reads(context_field_names)


@pytest.fixture(scope="module")
def method_reads(context_field_names: Set[str]) -> Dict[str, List[str]]:
    return _find_method_reads(context_field_names)


# ── The scanner must actually work (guards against a vacuous gate) ──


class TestScannerCoherence:
    """If the scanner silently found nothing, every other test here would pass
    for the wrong reason. Pin its behavior on seams we know are wired."""

    def test_scanner_finds_known_wired_fields(self, field_reads) -> None:
        # A representative spread across the read shapes and file locations.
        for field in ("identity", "slack_gate", "dashboard", "tunnel", "governance"):
            assert field_reads[field], f"scanner found no read of the wired {field!r} seam"

    def test_scanner_finds_two_step_ctx_var_shape(self, field_reads) -> None:
        """``ctx = current_context()`` then ``ctx.jail`` must be recognized."""
        assert any("cli.py" in site for site in field_reads["jail"]), field_reads["jail"]

    def test_scanner_finds_method_level_reads(self, method_reads) -> None:
        assert method_reads.get("identity.status_line"), "method-level scan found nothing"

    def test_scanner_excludes_platform_package(self, field_reads) -> None:
        """Seam-internal plumbing must never count as a consumption site."""
        for sites in field_reads.values():
            assert not [s for s in sites if "/platform/" in s.replace("\\", "/")]

    def test_scanner_reads_a_nonempty_tree(self) -> None:
        files = _core_source_files()
        assert len(files) > 100, f"only {len(files)} core files scanned"


# ── The gate: every field is wired or explicitly reserved ──


class TestSeamCoverage:
    def test_every_context_field_is_wired_or_reserved(
        self, context_field_names, field_reads
    ) -> None:
        """No field may be silently inert.

        A new ``PlatformContext`` field with no consumption site fails HERE —
        either wire it, or declare it in ``RESERVED_SLOTS`` with the reason and
        the wired alternative. (``_BOOT_CONSUMED_IN_PLATFORM`` covers the two
        boot-protocol carriers whose only legitimate consumer is ``bootstrap``.)
        """
        accounted = set(RESERVED_SLOTS) | set(_BOOT_CONSUMED_IN_PLATFORM)
        unaccounted = sorted(
            name for name in context_field_names if not field_reads[name] and name not in accounted
        )
        assert not unaccounted, (
            "PlatformContext field(s) with NO core consumption site and no "
            f"RESERVED_SLOTS entry: {unaccounted}. Either wire the field to a real "
            "call site, or add it to kiro_crew.platform.context.RESERVED_SLOTS with "
            "the reason it is inert and the wired alternative — otherwise an edition "
            "can override it and get silence."
        )

    def test_boot_consumed_fields_are_really_read_by_bootstrap(self, context_field_names) -> None:
        """Prove the boot-protocol allowance is not a rubber stamp.

        Each ``_BOOT_CONSUMED_IN_PLATFORM`` field must genuinely be read in
        ``platform/bootstrap.py`` — so a future field cannot be parked there to
        dodge :func:`test_every_context_field_is_wired_or_reserved`.
        """
        unknown = sorted(set(_BOOT_CONSUMED_IN_PLATFORM) - context_field_names)
        assert not unknown, f"_BOOT_CONSUMED_IN_PLATFORM names non-fields: {unknown}"

        bootstrap_src = (_SRC_ROOT / "platform" / "bootstrap.py").read_text(encoding="utf-8")
        tree = ast.parse(bootstrap_src)
        read_there = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in context_field_names
            and _is_context_expr(node.value)
        }
        missing = sorted(set(_BOOT_CONSUMED_IN_PLATFORM) - read_there)
        assert not missing, (
            f"_BOOT_CONSUMED_IN_PLATFORM field(s) NOT actually read in "
            f"platform/bootstrap.py: {missing}. The allowance exists only for "
            "fields the composition root really consumes — wire it or reserve it."
        )

    def test_boot_consumed_and_reserved_are_disjoint(self) -> None:
        overlap = sorted(set(_BOOT_CONSUMED_IN_PLATFORM) & set(RESERVED_SLOTS))
        assert not overlap, (
            f"field(s) both boot-consumed and RESERVED: {overlap} — a field cannot "
            "be live at boot and inert at the same time."
        )

    def test_reserved_slots_have_no_consumption_site(self, field_reads) -> None:
        """A RESERVED marker must not outlive the inertness it documents.

        If a reserved slot gains a real consumption site, this fails until the
        ``RESERVED_SLOTS`` entry (and the ``[RESERVED]`` field comment, and the
        Protocol docstring) are removed — so the marker can never go stale and
        mislead the next implementor into thinking a live seam is dead.
        """
        now_wired = {name: field_reads[name] for name in RESERVED_SLOTS if field_reads.get(name)}
        assert not now_wired, (
            f"RESERVED_SLOTS entries that now HAVE consumption sites: {now_wired}. "
            "Wiring a reserved slot is good — now finish the job: delete its "
            "RESERVED_SLOTS entry, drop the '[RESERVED]' comment on the "
            "PlatformContext field, and update the Protocol docstring in "
            "interfaces.py."
        )

    def test_reserved_slots_are_real_fields(self, context_field_names) -> None:
        """A reservation for a field that no longer exists is dead weight."""
        unknown = sorted(set(RESERVED_SLOTS) - context_field_names)
        assert not unknown, f"RESERVED_SLOTS names non-existent field(s): {unknown}"

    def test_reserved_slots_explain_themselves(self) -> None:
        """Each reason must be substantive — a bare 'TODO' helps nobody."""
        for name, reason in RESERVED_SLOTS.items():
            assert len(reason) > 60, f"RESERVED_SLOTS[{name!r}] reason is too terse"
            assert "no core call site" in reason, (
                f"RESERVED_SLOTS[{name!r}] must state 'no core call site' so the "
                "reason is greppable and unambiguous"
            )

    def test_reserved_slots_are_marked_on_the_dataclass(self) -> None:
        """The marker must be visible in the source an implementor actually reads.

        The whole point of this change: the inertness has to be legible from
        ``PlatformContext`` itself, not only from a docstring three modules away.
        A reserved field's declaration line — or the comment block immediately
        above it — must carry the ``[RESERVED]`` token.
        """
        source = (_SRC_ROOT / "platform" / "context.py").read_text(encoding="utf-8")
        # Isolate the dataclass body so a match inside RESERVED_SLOTS itself (or
        # the class docstring) cannot satisfy the assertion.
        body = source.split("class PlatformContext:", 1)[1].split("def __post_init__", 1)[0]
        lines = body.splitlines()
        for name in RESERVED_SLOTS:
            decl = [i for i, ln in enumerate(lines) if ln.strip().startswith(f"{name}:")]
            assert decl, f"reserved field {name!r} not found in the dataclass body"
            idx = decl[0]
            # The declaration line, plus any contiguous comment lines above it.
            block = [lines[idx]]
            probe = idx - 1
            while probe >= 0 and lines[probe].strip().startswith("#"):
                block.append(lines[probe])
                probe -= 1
            assert any("[RESERVED]" in ln for ln in block), (
                f"reserved field {name!r} carries no '[RESERVED]' marker on its "
                "declaration line or in the comment block directly above it — an "
                "implementor reading the dataclass would not see that overriding "
                "it does nothing"
            )


class TestReservedMethodCoverage:
    """Same contract, one level finer: methods on otherwise-live fields."""

    def test_reserved_methods_have_no_consumption_site(self, method_reads) -> None:
        now_wired = {
            f"{field}.{method}": method_reads[f"{field}.{method}"]
            for field, methods in RESERVED_METHODS.items()
            for method in methods
            if method_reads.get(f"{field}.{method}")
        }
        assert not now_wired, (
            f"RESERVED_METHODS entries that now HAVE consumption sites: {now_wired}. "
            "Delete the RESERVED_METHODS entry and update the Protocol docstring in "
            "interfaces.py."
        )

    def test_reserved_methods_name_real_fields(self, context_field_names) -> None:
        unknown = sorted(set(RESERVED_METHODS) - context_field_names)
        assert not unknown, f"RESERVED_METHODS names non-existent field(s): {unknown}"

    def test_reserved_methods_exist_on_the_default_adapter(self) -> None:
        """A reserved method must actually be part of the contract.

        Catches a rename: if ``whoami`` were renamed and the reservation left
        behind, the entry would silently protect nothing.
        """
        ctx = build_default_context(KiroCrewConfig())
        for field, methods in RESERVED_METHODS.items():
            adapter = getattr(ctx, field)
            for method in methods:
                assert callable(
                    getattr(adapter, method, None)
                ), f"RESERVED_METHODS[{field!r}] names missing method {method!r}"

    def test_reserved_methods_are_not_whole_field_reservations(self) -> None:
        """A field with EVERY method reserved should be a RESERVED_SLOT instead."""
        overlap = sorted(set(RESERVED_METHODS) & set(RESERVED_SLOTS))
        assert not overlap, (
            f"field(s) in BOTH RESERVED_METHODS and RESERVED_SLOTS: {overlap}. A "
            "reserved slot is already wholly inert — the per-method entry is "
            "redundant and will drift."
        )

    def test_reserved_methods_explain_themselves(self) -> None:
        for field, methods in RESERVED_METHODS.items():
            for method, reason in methods.items():
                assert "no core call site" in reason, (
                    f"RESERVED_METHODS[{field!r}][{method!r}] must state " "'no core call site'"
                )


# ── The peek gate: installed_context() callers are declared, or the build fails ──


class TestInstalledContextPeeks:
    """``installed_context()`` opts out of the fail-closed contract; its callers
    must say why that is safe for them, in a map a reviewer sees."""

    def test_scanner_detects_a_synthetic_peek(self) -> None:
        """Guard against a vacuous gate.

        Both arms below pass trivially if the scanner finds nothing, so pin the
        two call shapes and the scope attribution against source we control.
        """
        found = _peeks_in_source(
            "\n".join(
                (
                    "from kiro_crew.platform.context import installed_context",
                    "def outer():",
                    "    return installed_context()",
                    "class C:",
                    "    def m(self):",
                    "        return pc.installed_context()",
                    "def unrelated():",
                    "    return current_context()",
                )
            ),
            "synthetic.py",
        )
        assert set(found) == {"synthetic.py::outer", "synthetic.py::C.m"}, found

    def test_scanner_finds_the_real_call_sites(self, peek_sites) -> None:
        """The scan must see the live tree, not an empty one."""
        assert peek_sites, "peek scanner found no installed_context() call site at all"
        assert "security.py::_exempt_exact_hosts" in peek_sites, sorted(peek_sites)

    def test_scanner_does_not_count_the_definition(self, peek_sites) -> None:
        """``def installed_context()`` is not a call — the accessor is not its own
        caller, and counting it would let the map self-justify."""
        assert f"platform/context.py::{_PEEK_FUNC}" not in peek_sites

    def test_every_peek_is_declared(self, peek_sites) -> None:
        """An undeclared peek fails HERE.

        Adding a caller now costs a ``PEEK_CALLERS`` entry stating why ITS
        no-context answer is the conservative one — the question that decides
        whether skipping the fail-closed path is safe, and the one a docstring
        could only ask politely.
        """
        undeclared = {key: sites for key, sites in peek_sites.items() if key not in PEEK_CALLERS}
        assert not undeclared, (
            f"undeclared installed_context() caller(s): {undeclared}. This accessor "
            "answers None instead of refusing to compose, so it is safe ONLY where "
            "the no-context answer is already the conservative one. Add an entry to "
            "kiro_crew.platform.context.PEEK_CALLERS stating why that holds for this "
            "caller, or use current_context() and take the fail-closed error."
        )

    def test_declared_peeks_still_have_a_call_site(self, peek_sites) -> None:
        """A permission must not outlive the caller it was written for.

        Without this arm a deleted or renamed caller leaves behind an entry that
        reads as a reviewed decision, and the next author points at it as
        precedent for a call site nobody examined.
        """
        stale = sorted(key for key in PEEK_CALLERS if key not in peek_sites)
        assert not stale, (
            f"PEEK_CALLERS entries with no matching call site: {stale}. The caller was "
            "removed or renamed — delete the entry (or re-key it) so the map keeps "
            f"describing the real callers. Live sites: {sorted(peek_sites)}"
        )

    def test_peek_justifications_answer_the_question(self) -> None:
        """Each reason must actually address the no-context answer, greppably."""
        for key, reason in PEEK_CALLERS.items():
            assert "no-context answer" in reason, (
                f"PEEK_CALLERS[{key!r}] must state what the 'no-context answer' is, "
                "so the justification addresses the contract it bypasses"
            )
            assert len(reason) > 60, f"PEEK_CALLERS[{key!r}] reason is too terse"

    def test_peek_keys_name_real_modules(self) -> None:
        """A key that names no file could never match a site, in either arm."""
        missing = sorted(
            key for key in PEEK_CALLERS if not (_SRC_ROOT / key.split("::", 1)[0]).is_file()
        )
        assert not missing, f"PEEK_CALLERS keys naming non-existent module(s): {missing}"


# ── The runtime signal: composing into a reserved slot is loud ──


class TestReservedSlotWarning:
    """A non-default value in a reserved slot logs once, loudly — and the
    all-defaults standalone context stays silent."""

    @pytest.fixture(autouse=True)
    def _clear_warn_dedup(self):
        """The dedup set is process-global; isolate each test from the others."""
        saved = set(_RESERVED_WARNED)
        _RESERVED_WARNED.clear()
        yield
        _RESERVED_WARNED.clear()
        _RESERVED_WARNED.update(saved)

    def test_default_context_warns_about_nothing(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            build_default_context(KiroCrewConfig())
        assert "RESERVED slot" not in caplog.text

    def test_override_into_reserved_slot_warns(self, caplog) -> None:
        class _MyPackageManager:
            def install_plan(self, tool: str) -> List[str]:
                return ["brew", "install", tool]

            def which(self, tool: str) -> Optional[str]:
                return None

        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            dataclasses.replace(base, package_manager=_MyPackageManager())
        assert "PlatformContext.package_manager is a RESERVED slot" in caplog.text
        # The warning must be actionable: it names the offending adapter AND the
        # wired alternative, so the reader knows what to do instead.
        assert "_MyPackageManager" in caplog.text
        assert "cli_doctor" in caplog.text

    def test_non_empty_feature_apps_warns(self, caplog) -> None:
        """``feature_apps`` is a tuple, not an adapter — its default is ``()``."""

        class _App:
            name = "demo"

            def manifest_path(self) -> Path:
                return Path("/nonexistent/manifest.json")

            def register(self, ctx: object) -> None:
                return None

        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            dataclasses.replace(base, feature_apps=(_App(),))
        assert "PlatformContext.feature_apps is a RESERVED slot" in caplog.text

    def test_warning_is_deduped_per_adapter(self, caplog) -> None:
        """A composition root that rebuilds the context must not spam the log."""

        class _MyPackageManager:
            def install_plan(self, tool: str) -> List[str]:
                return []

            def which(self, tool: str) -> Optional[str]:
                return None

        mine = _MyPackageManager()
        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            first = dataclasses.replace(base, package_manager=mine)
            dataclasses.replace(first, profile="enterprise")
            dataclasses.replace(first, telemetry=first.telemetry)
        assert caplog.text.count("PlatformContext.package_manager is a RESERVED slot") == 1

    def test_a_different_override_still_warns(self, caplog) -> None:
        """Dedup is keyed by adapter type, so a LATER, different override reports."""

        class _First:
            def install_plan(self, tool: str) -> List[str]:
                return []

            def which(self, tool: str) -> Optional[str]:
                return None

        class _Second(_First):
            pass

        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            dataclasses.replace(base, package_manager=_First())
            dataclasses.replace(base, package_manager=_Second())
        assert caplog.text.count("PlatformContext.package_manager is a RESERVED slot") == 2

    def test_subclass_of_default_adapter_still_warns(self, caplog) -> None:
        """A companion SUBCLASS of a ``Default*`` adapter is a real override.

        It can change behavior, so class identity (not ``isinstance``) decides —
        otherwise the loudest case (an edition extending the default) would be
        the one case that stays silent.
        """
        from kiro_crew.platform.defaults import DefaultPackageManager

        class _Extended(DefaultPackageManager):
            def install_plan(self, tool: str) -> List[str]:
                return ["managed-install", tool]

        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            dataclasses.replace(base, package_manager=_Extended())
        assert "PlatformContext.package_manager is a RESERVED slot" in caplog.text

    def test_warning_never_breaks_composition(self, monkeypatch, caplog) -> None:
        """Diagnostics must not be able to abort boot.

        The warning pass is best-effort by contract, so if any part of it raises
        the context must still compose. Forced here by making the
        default-recognizer itself blow up — the same fail-safe that protects boot
        from a hostile adapter whose ``type()``/``__qualname__`` misbehaves.
        """

        def _boom(*_args, **_kwargs):
            raise RuntimeError("recognizer exploded")

        monkeypatch.setattr("kiro_crew.platform.context._reserved_slot_is_default", _boom)
        base = build_default_context(KiroCrewConfig())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.platform.context"):
            ctx = dataclasses.replace(base, telemetry=base.telemetry)
        assert isinstance(ctx, PlatformContext)
        assert "RESERVED slot" not in caplog.text


class TestReservedDefaultAdapterMap:
    """``_RESERVED_DEFAULT_ADAPTERS`` must stay in step with ``RESERVED_SLOTS``,
    or a new reserved slot would warn on every boot of the DEFAULT context."""

    def test_map_covers_every_adapter_backed_reserved_slot(self) -> None:
        # ``feature_apps`` is the one non-adapter slot (its default is ``()``).
        expected = set(RESERVED_SLOTS) - {"feature_apps"}
        assert set(_RESERVED_DEFAULT_ADAPTERS) == expected, (
            "every RESERVED_SLOTS entry except feature_apps needs a "
            "_RESERVED_DEFAULT_ADAPTERS entry naming its Default* class, else the "
            "all-defaults standalone context warns spuriously at every boot"
        )

    def test_named_default_classes_exist_and_are_composed(self) -> None:
        ctx = build_default_context(KiroCrewConfig())
        for field, class_name in _RESERVED_DEFAULT_ADAPTERS.items():
            assert type(getattr(ctx, field)).__name__ == class_name, (
                f"_RESERVED_DEFAULT_ADAPTERS[{field!r}]={class_name!r} is not what "
                "build_default_context actually composes"
            )

    def test_default_recognizer_agrees_with_the_default_context(self) -> None:
        ctx = build_default_context(KiroCrewConfig())
        for field in RESERVED_SLOTS:
            assert _reserved_slot_is_default(field, getattr(ctx, field)) is True


# ── Agent identity CPP slot + capabilities.agentcore (public no-ops) ──

_TOKEN_LIKE_STATUS_NEEDLES = (
    "token",
    "jwt",
    "bearer",
    "authorization",
    "secret",
    "password",
    "credential",
)


def _agentcore_policy_body(
    *,
    fail_closed: bool = True,
    agentcore: Optional[dict] = None,
) -> dict:
    body: dict = {
        "version": 1,
        "boot": {"fail_closed": fail_closed},
    }
    if agentcore is not None:
        body["capabilities"] = {"agentcore": agentcore}
    return body


class TestAgentIdentitySeam:
    """Public no-op slot: disabled Default + opt-in catalog row + fail-closed posture."""

    def test_platform_context_has_agent_identity_slot(self) -> None:
        names = {f.name for f in dataclasses.fields(PlatformContext)}
        assert "agent_identity" in names
        ctx = build_default_context(KiroCrewConfig())
        assert hasattr(ctx, "agent_identity")

    def test_default_agent_identity_is_disabled(self) -> None:
        ctx = build_default_context(KiroCrewConfig())
        adapter = ctx.agent_identity
        assert adapter.enabled() is False
        assert adapter.workload_identity() is None
        assert adapter.gateway_mcp_spec() is None
        status = adapter.status()
        assert isinstance(status, dict)
        for key in status:
            lowered = str(key).lower()
            assert not any(
                needle in lowered for needle in _TOKEN_LIKE_STATUS_NEEDLES
            ), f"agent_identity.status() must not expose token-like key {key!r}"

    def test_bearer_fields_are_omitted_from_repr(self) -> None:
        principal = SessionPrincipal(
            surface="dashboard",
            subject="user-1",
            session_key="dashboard:main",
            user_jwt="secret.jwt.token",
        )
        inbound = InboundToken(
            scheme="Bearer",
            token="secret-inbound",
            expires_at=0.0,
            audience="gateway",
        )
        assert "secret.jwt.token" not in repr(principal)
        assert "secret-inbound" not in repr(inbound)
        assert principal.user_jwt == "secret.jwt.token"
        assert inbound.token == "secret-inbound"

    def test_agent_identity_capability_is_opt_in(self) -> None:
        spec = SCOPE_CATALOG["capabilities.agentcore"]
        assert spec.kind == CAPABILITY
        assert spec.capability_default is False

    def test_agent_identity_enabled_without_posture_aborts_when_fail_closed(self) -> None:
        with pytest.raises(PlatformCompositionError, match="posture"):
            parse_policy(_agentcore_policy_body(agentcore={"enabled": True}))

    def test_agent_identity_enabled_unknown_posture_aborts_when_fail_closed(self) -> None:
        with pytest.raises(PlatformCompositionError, match="posture"):
            parse_policy(
                _agentcore_policy_body(agentcore={"enabled": True, "posture": "federated"})
            )

    def test_agent_identity_non_boolean_enabled_aborts_when_fail_closed(self) -> None:
        with pytest.raises(PlatformCompositionError, match="boolean"):
            parse_policy(
                _agentcore_policy_body(
                    agentcore={"enabled": "false", "posture": "workload"},
                )
            )

    def test_agent_identity_null_enabled_aborts_when_fail_closed(self) -> None:
        """A present ``enabled: null`` must not default the row on."""
        with pytest.raises(PlatformCompositionError, match="boolean"):
            parse_policy(
                _agentcore_policy_body(
                    agentcore={"enabled": None, "posture": "workload"},
                )
            )

    def test_agent_identity_non_boolean_enabled_raises_when_not_fail_closed(
        self,
    ) -> None:
        # CapabilityGate.from_dict rejects unconditionally; fail_closed
        # cannot salvage a stringly-typed enabled into a disabled row.
        with pytest.raises(PlatformCompositionError, match="boolean"):
            parse_policy(
                _agentcore_policy_body(
                    fail_closed=False,
                    agentcore={"enabled": "false", "posture": "workload"},
                )
            )

    def test_agent_identity_enabled_without_posture_disables_when_not_fail_closed(
        self,
    ) -> None:
        ceiling = parse_policy(
            _agentcore_policy_body(fail_closed=False, agentcore={"enabled": True})
        )
        decision = resolve(ceiling, None, "capabilities.agentcore", "")
        assert not decision.permitted

    def test_agent_identity_enabled_with_known_posture_parses(self) -> None:
        for posture in ("workload", "login"):
            ceiling = parse_policy(
                _agentcore_policy_body(
                    agentcore={"enabled": True, "posture": posture},
                )
            )
            decision = resolve(ceiling, None, "capabilities.agentcore", "")
            assert decision.permitted, f"posture={posture!r} must remain enabled"

    def test_agent_identity_known_posture_is_stored_on_ceiling(self) -> None:
        from kiro_crew.platform.governance import agentcore_posture

        for posture in ("workload", "login"):
            ceiling = parse_policy(
                _agentcore_policy_body(agentcore={"enabled": True, "posture": posture})
            )
            assert agentcore_posture(ceiling) == posture

    def test_agent_identity_omitted_capability_has_no_stored_posture(self) -> None:
        from kiro_crew.platform.governance import agentcore_posture

        ceiling = parse_policy(_agentcore_policy_body())
        assert agentcore_posture(ceiling) is None
        assert agentcore_posture(None) is None

    def test_agent_identity_disabled_row_does_not_require_posture(self) -> None:
        from kiro_crew.platform.governance import agentcore_posture

        ceiling = parse_policy(_agentcore_policy_body(agentcore={"enabled": False}))
        assert not resolve(ceiling, None, "capabilities.agentcore", "").permitted
        assert agentcore_posture(ceiling) is None

    def test_agent_identity_fail_closed_disabled_has_no_stored_posture(self) -> None:
        from kiro_crew.platform.governance import agentcore_posture

        ceiling = parse_policy(
            _agentcore_policy_body(fail_closed=False, agentcore={"enabled": True})
        )
        assert agentcore_posture(ceiling) is None

    def test_agent_identity_profile_enabled_without_posture_parses(self) -> None:
        """A profile may toggle enabled; posture is policy-only and not required."""
        from kiro_crew.platform.governance import CapabilityGate

        profile = parse_profile({"name": "host", "capabilities": {"agentcore": {"enabled": True}}})
        gate = profile.controls["capabilities.agentcore"]
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is True

    def test_agent_identity_profile_non_boolean_enabled_is_rejected(self) -> None:
        """``enabled: "false"`` must not coerce to a permit through ``bool()``."""
        with pytest.raises(PlatformCompositionError, match="boolean"):
            parse_profile({"name": "host", "capabilities": {"agentcore": {"enabled": "false"}}})

    def test_agent_identity_profile_carrying_posture_is_rejected(self) -> None:
        """Carrying posture on a profile is a silent lie — reject like ScopedMap.posture."""
        with pytest.raises(PlatformCompositionError, match="posture"):
            parse_profile(
                {
                    "name": "host",
                    "capabilities": {
                        "agentcore": {"enabled": True, "posture": "login"},
                    },
                }
            )

    def test_agent_identity_policy_gateway_url_is_stored(self) -> None:
        from kiro_crew.platform.governance import agentcore_gateway_url

        ceiling = parse_policy(
            _agentcore_policy_body(
                agentcore={
                    "enabled": True,
                    "posture": "workload",
                    "gateway_url": "https://gw.example.test/mcp",
                }
            )
        )
        assert agentcore_gateway_url(ceiling) == "https://gw.example.test/mcp"

    def test_agent_identity_policy_rejects_http_gateway_url(self) -> None:
        with pytest.raises(PlatformCompositionError, match="https"):
            parse_policy(
                _agentcore_policy_body(
                    agentcore={
                        "enabled": True,
                        "posture": "workload",
                        "gateway_url": "http://insecure.example/mcp",
                    }
                )
            )

    def test_agent_identity_profile_carrying_gateway_url_is_rejected(self) -> None:
        with pytest.raises(PlatformCompositionError, match="gateway_url"):
            parse_profile(
                {
                    "name": "host",
                    "capabilities": {
                        "agentcore": {
                            "enabled": True,
                            "gateway_url": "https://gw.example.test/mcp",
                        },
                    },
                }
            )

    def test_agent_identity_profile_disabled_row_does_not_require_posture(self) -> None:
        profile = parse_profile({"name": "host", "capabilities": {"agentcore": {"enabled": False}}})
        gate = profile.controls["capabilities.agentcore"]
        assert gate.enabled is False

    def test_agent_identity_seam_stays_off_when_capability_is_off(self) -> None:
        """Adapter-on is not enough: capability off / no posture keeps the seam off."""
        from kiro_crew.agent import _agent_identity_enabled
        from kiro_crew.platform.defaults import DefaultAgentIdentityProvider

        class _ForcedOn(DefaultAgentIdentityProvider):
            def enabled(self) -> bool:
                return True

        base = build_default_context(KiroCrewConfig())
        off_ceiling = parse_policy(_agentcore_policy_body(agentcore={"enabled": False}))
        on_ceiling = parse_policy(
            _agentcore_policy_body(agentcore={"enabled": True, "posture": "workload"})
        )
        try:
            set_context(dataclasses.replace(base, agent_identity=_ForcedOn(), governance=None))
            assert _agent_identity_enabled() is False
            set_context(
                dataclasses.replace(base, agent_identity=_ForcedOn(), governance=off_ceiling)
            )
            assert _agent_identity_enabled() is False
            set_context(
                dataclasses.replace(base, agent_identity=_ForcedOn(), governance=on_ceiling)
            )
            assert _agent_identity_enabled() is True
        finally:
            reset_context()

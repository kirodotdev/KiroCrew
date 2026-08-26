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
"""

from __future__ import annotations

import ast
import dataclasses
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set

import pytest

import kiro_crew
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    RESERVED_METHODS,
    RESERVED_SLOTS,
    build_default_context,
)
from kiro_crew.platform.context import (
    _RESERVED_DEFAULT_ADAPTERS,
    _RESERVED_WARNED,
    PlatformContext,
    _reserved_slot_is_default,
)

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

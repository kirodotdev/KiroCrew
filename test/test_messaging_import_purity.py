"""The one-way dependency of ``kiro_crew.messaging``, enforced over every edge.

``docs/system-specs/modules/messaging.md`` states the invariant twice — the
neutral messaging package imports nothing from a channel package and nothing from
``dashboard`` — and until this file it had no gate. The one check that existed
lived inside ``test_teams_transport.py`` and named ``kiro_crew.teams`` only, so
a ``dashboard`` import was added to ``messaging/upload_gate.py`` while hoisting
shared code out of a channel, and nothing went red.

That is the shape of the bug this file is aimed at: the *class* of defect recurs
per forbidden package, so the gate enumerates every forbidden package once rather
than being restated per channel. Adding a channel needs no edit here at all — the
channel half of ``_FORBIDDEN`` is derived from the roster, because a hand-kept list
fails OPEN and had already missed two channels.

A function-local import does not escape it. ``ast.walk`` descends into function
bodies, and a guarded ``try: from kiro_crew.dashboard import x`` is the same edge
as a module-level one: it still couples the packages, and it fails at call time
instead of at import time, which is strictly worse. Where the neutral module
genuinely needs something a channel owns, the caller passes it in — see
``messaging/upload_gate.uploads_restricted``'s ``persisted_probe``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import kiro_crew.messaging as messaging_pkg


def _channel_packages() -> tuple[str, ...]:
    """The channel packages, DERIVED from the roster rather than listed here.

    A hand-kept list is the wrong shape for this gate: it fails open. A channel
    that lands without being added is simply not checked, and nothing reports it --
    which is how ``feishu`` came to be missing after a hand-kept copy had already
    missed ``whatsapp`` once. The roster is the same source the pre-turn ratchet
    reads, so a new channel is covered by existing.

    The mapping assumed here is that a ``channel_type`` names its package. That
    assumption is checked by ``test_every_rostered_channel_is_covered``, which walks
    the roster and puts each name through the same predicate the scan uses — so a
    channel whose type stops matching its package fails there rather than quietly
    dropping out of the forbidden set. It is not re-checked here: a second copy of
    the check would be one nothing exercises.
    """
    from kiro_crew.channels import builtin_channel_descriptors

    names = sorted({d.channel_type for d in builtin_channel_descriptors()})
    return tuple(f"kiro_crew.{name}" for name in names)


#: Every package ``kiro_crew.messaging`` may not depend on. The channels are
#: forbidden because messaging is the layer they all sit on; ``dashboard`` is
#: forbidden because the dashboard gateway imports the channel transports, so the
#: edge is also a cycle. Only the non-channel entry is written out.
_FORBIDDEN = ("kiro_crew.dashboard",) + _channel_packages()

#: Dynamic-import call names. Enumerated because they are the only way to reach a
#: module without an import statement, and therefore the only way an edge could be
#: reintroduced without tripping the statement scan above.
_DYNAMIC_IMPORTERS = ("import_module", "__import__")

#: ``(module, imported package) -> reason``. Edges that exist today and are NOT
#: sanctioned. Recorded rather than silently permitted: an exemption with a reason
#: is a debt someone can pay, and an unlisted edge is one nobody knows about.
#:
#: The bar for adding a row is deliberately high — the fix for every entry below is
#: the same one ``upload_gate`` took, which is to accept the dependency as a
#: PARAMETER from a caller that may legally hold it. A row belongs here only while
#: that change is genuinely out of the current change's scope.
_KNOWN_VIOLATIONS: dict[tuple[str, str], str] = {
    ("dispatch.py", "kiro_crew.dashboard"): (
        "build_directive_consumer reaches dashboard.session_directive_apply through a "
        "function-local import. The applier is the shared security core both the "
        "dashboard and the channel consumers run through, so it is not dashboard-"
        "specific in substance — only in where it currently lives. Fixing it means "
        "either threading it through all eight channel dispatchers as a parameter or "
        "moving the applier to a neutral module; both belong to whoever owns the "
        "session-directive feature, not to a channel-parity change."
    ),
}


def _forbidden_prefix(module: str) -> str:
    """The ``_FORBIDDEN`` entry *module* names, or ``""``.

    Prefix-matched on a dotted boundary so a future ``kiro_crew.slackbot`` is not
    mistaken for ``kiro_crew.slack``.
    """
    for banned in _FORBIDDEN:
        if module == banned or module.startswith(banned + "."):
            return banned
    return ""


def _is_known(module_name: str, offence: str) -> bool:
    """Whether *offence* in *module_name* is a recorded, still-unpaid exemption."""
    for (mod, pkg), _reason in _KNOWN_VIOLATIONS.items():
        if module_name == mod and pkg in offence:
            return True
    return False


def _offenders_in(source: str, name: str) -> list[str]:
    """Every forbidden edge *source* declares, as human-readable strings.

    Takes SOURCE, not a path, so the probe cases below need no file. Writing one
    into the package under test would be a real isolation bug rather than a
    convenience: ``testpaths`` runs under ``-n auto``, so a second worker can be
    walking that directory while the probe file exists and would attribute the
    deliberate violation to production code.
    """
    found: list[str] = []
    tree = ast.parse(source, filename=name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _forbidden_prefix(alias.name):
                    found.append(f"{name}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # A relative import cannot leave the package, so only absolute ones
            # can name a forbidden target; node.module is None for `from . import`.
            if node.level == 0 and _forbidden_prefix(node.module or ""):
                found.append(f"{name}:{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            fn = getattr(func, "attr", None) or getattr(func, "id", None)
            if fn not in _DYNAMIC_IMPORTERS or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if _forbidden_prefix(first.value):
                    found.append(f"{name}:{node.lineno}: {fn}({first.value!r})")
    return found


def _offenders(py: Path) -> list[str]:
    """:func:`_offenders_in` for a real module on disk."""
    return _offenders_in(py.read_text(encoding="utf-8"), py.name)


class TestMessagingImportPurity:
    def test_messaging_imports_no_channel_and_no_dashboard(self) -> None:
        pkg_dir = Path(messaging_pkg.__file__).parent
        modules = sorted(pkg_dir.rglob("*.py"))
        # A glob that silently matched nothing would make this test vacuous, which
        # is exactly how the gate it replaces went unnoticed.
        assert len(modules) > 5, f"expected the messaging package to have modules, saw {modules}"
        offenders = [
            line for py in modules for line in _offenders(py) if not _is_known(py.name, line)
        ]
        assert not offenders, (
            "kiro_crew.messaging must not import a channel package or dashboard; "
            "pass what it needs in as a parameter instead. Offending edges: " + str(offenders)
        )

    def test_every_recorded_violation_still_exists(self) -> None:
        """A stale exemption silently un-covers the module it names.

        The whole value of the table is that it shrinks. An entry whose edge has
        since been removed keeps that module permanently exempt for that package,
        so the next real violation there goes unreported.
        """
        pkg_dir = Path(messaging_pkg.__file__).parent
        found = {(py.name, line) for py in pkg_dir.rglob("*.py") for line in _offenders(py)}
        stale = [
            f"{mod} no longer imports {pkg}"
            for (mod, pkg) in _KNOWN_VIOLATIONS
            if not any(name == mod and pkg in line for name, line in found)
        ]
        assert not stale, f"remove these rows from _KNOWN_VIOLATIONS: {stale}"

    def test_a_violation_outside_the_table_is_still_caught(self) -> None:
        """The exemption is scoped to one module AND one package, not blanket."""
        # dispatch.py is exempt for `dashboard` only.
        assert _is_known("dispatch.py", "dispatch.py:1: from kiro_crew.dashboard.x import y")
        assert not _is_known("dispatch.py", "dispatch.py:1: from kiro_crew.slack.x import y")
        assert not _is_known("driver.py", "driver.py:1: from kiro_crew.dashboard.x import y")

    def test_a_type_checking_only_import_is_still_refused(self) -> None:
        """A ``TYPE_CHECKING`` guard does not make the edge acceptable.

        Duck-typing under ``TYPE_CHECKING`` is the sanctioned way for a neutral
        module to *annotate* a channel-owned service (``messaging/commands.py``
        does exactly that), and it does so WITHOUT naming the package — the type
        is `Any`-shaped. An actual ``from kiro_crew.slack import ...`` inside the
        guard would still couple the two at type-check time and would still be a
        lie about the layering, so the scan does not special-case it.
        """
        src = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from kiro_crew.slack import x\n"
        assert _offenders_in(
            src, "probe.py"
        ), "a TYPE_CHECKING-guarded forbidden import went undetected"

    def test_a_dynamic_import_does_not_escape_the_scan(self) -> None:
        """``importlib.import_module`` is the obvious bypass, so it is covered."""
        src = 'import importlib\nm = importlib.import_module("kiro_crew.dashboard.handlers")\n'
        assert _offenders_in(src, "probe.py"), "a dynamic forbidden import went undetected"

    def test_a_lookalike_package_is_not_a_false_positive(self) -> None:
        """Prefix matching is on a dotted boundary, not a bare ``startswith``."""
        assert _forbidden_prefix("kiro_crew.slackbot") == ""
        assert _forbidden_prefix("kiro_crew.slack") == "kiro_crew.slack"
        assert _forbidden_prefix("kiro_crew.slack.handler") == "kiro_crew.slack"

    def test_a_relative_import_inside_messaging_is_allowed(self) -> None:
        """``from . import x`` cannot leave the package, so it must not be flagged."""
        src = "from . import renderer\nfrom .link import ChannelLink\n"
        assert _offenders_in(src, "probe.py") == []

    def test_every_rostered_channel_is_covered(self) -> None:
        """The derivation, asserted per channel rather than by count.

        A count passes just as happily when one channel is swapped for another, and
        the failure mode here is a channel silently NOT being covered — so each one
        is named by the roster and checked through the same predicate the scan uses.
        """
        from kiro_crew.channels import builtin_channel_descriptors

        for descriptor in builtin_channel_descriptors():
            package = f"kiro_crew.{descriptor.channel_type}"
            assert _forbidden_prefix(package) == package, f"{package} is not covered"
            assert _offenders_in(
                f"from {package}.transport import X\n", "probe.py"
            ), f"an import of {package} went undetected"

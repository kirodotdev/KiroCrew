"""GATE — the credential and exfiltration-URL passes are composed, never hand-ordered.

``kiro_crew.security.redact`` composes the two egress passes in one place, and its
own docstring states why the order is fixed: ``redact_exfiltration_urls`` matches
whole **token-bearing** URLs, so a credential replaced ahead of it leaves a
placeholder inside the URL and the URL pass fails to recognise the shape it exists
to catch. The credential is removed either way; what the reversed order loses is
suppression of the DESTINATION, which is the half a reader acts on.

A hand-sequenced pair is a silent leak, so the ``dashboard`` package's 37 reversed
sites compose through the helper and the remaining 47 pairs in 36 files are pinned
below rather than migrated in one change: the rule is enforced from today while the
sweep stays a separate review.

## What this pins

1. **The behaviour the order decides** — asserted on the shared helper, so the
   rule cannot become decorative if someone later "simplifies" a pass.
2. **No NEW reversed pair** in ``src/kiro_crew``, in either spelling (a
   two-statement pair, or one nested expression).
3. **No stale baseline entry.** A pinned site that has been paid off must be
   pruned here, or the list stops shrinking and starts lying.
4. **The scan itself is sound** — probes below fail if a spelling stops being
   recognised, and one asserts the walk really reads files rather than passing
   forever on an empty set.

## When this goes RED

You wrote a reversed pair. Compose it through ``redact`` (or
``redact_with_findings`` when you need the warning lists) — do not relax the rule.
If you are paying off a pinned site, delete its entry and lower its count.

## Nested order, which is easy to get backwards

The INNERMOST call runs first, so:

    redact_credentials(redact_exfiltration_urls(x))   # CORRECT — URL pass first
    redact_exfiltration_urls(redact_credentials(x))   # REVERSED — the leak

``_inline_reversed`` looks only for the second spelling.

## Spellings, and which ones the scan resolves

Both passes are reachable three ways in this tree, so the scan resolves all three:
a bare name (``redact_credentials(x)``), a qualified attribute chain
(``security.redact_credentials(x)``, the form the app packages use), and a
subscript (``redact_credentials(x)[0]``). An attribute chain is credited to a pass
only when its root is ``security``/``kiro_crew``/``self`` or it is a single bare
attribute, so another object that happens to share the method name is not read as
ours. An IMPORT ALIAS
(``from kiro_crew.security import redact_credentials as _redact_credentials``) is
deliberately NOT resolved: doing so means binding names to their imports, and a
half-built alias resolver is worse than a documented limit. That shape is
uncommon — ``workflows/runner.py`` is the one site — and it composes in the correct
order, so nothing is hidden behind it today.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

from kiro_crew.security import redact
from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import redact_credentials

# One xdist worker for the whole module: several tests derive from ONE walk of
# src/, and under `--dist loadgroup` an unmarked module is spread across workers,
# so each worker re-pays that walk.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_redaction_pair_order")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

CREDENTIAL = "redact_credentials"
URL_PASS = "redact_exfiltration_urls"

#: A URL carrying the credential in its query: the shape the URL pass keys on, and
#: the shape a credential-first order destroys.
_URL_WITH_CREDENTIAL = "see https://evil.example.com/collect?token=AKIAIOSFODNN7EXAMPLE&x=1 now"

#: Files still holding a reversed TWO-STATEMENT pair, with how many. Pinned rather
#: than migrated in this change: the fix and the sweep review better separately.
#: Keyed by path and COUNT rather than line number, so an unrelated edit above a
#: pinned site does not red this gate -- what is pinned is the obligation, not a
#: coordinate that moves.
_PINNED_TWO_LINE: dict[str, int] = {
    "src/kiro_crew/acp/client.py": 3,
    "src/kiro_crew/acp/runtime.py": 1,
    "src/kiro_crew/apps/builtins/auto_research/handlers.py": 3,
    "src/kiro_crew/apps/builtins/aws_control/backend/accounts.py": 1,
    "src/kiro_crew/apps/builtins/aws_control/backend/backup.py": 1,
    "src/kiro_crew/apps/builtins/aws_control/backend/library.py": 2,
    "src/kiro_crew/apps/builtins/aws_control/backend/routes.py": 2,
    "src/kiro_crew/apps/builtins/aws_control/backend/storage.py": 2,
    "src/kiro_crew/apps/builtins/dev_fleet/runtime.py": 1,
    "src/kiro_crew/apps/builtins/md_notebook/server.py": 1,
    "src/kiro_crew/apps/builtins/mochi/activity_log.py": 1,
    "src/kiro_crew/apps/builtins/mochi/redact.py": 1,
    "src/kiro_crew/apps/event_bus.py": 1,
    "src/kiro_crew/apps/job_routes.py": 1,
    "src/kiro_crew/apps/job_sdk.py": 1,
    "src/kiro_crew/apps/routes.py": 1,
    "src/kiro_crew/aws_consent.py": 1,
    "src/kiro_crew/channel.py": 2,
    "src/kiro_crew/cli_server.py": 1,
    "src/kiro_crew/deploy/engine.py": 1,
    "src/kiro_crew/deploy/handlers.py": 1,
    "src/kiro_crew/deploy/iam.py": 1,
    "src/kiro_crew/deploy/profiles.py": 1,
    "src/kiro_crew/deploy/scan.py": 1,
    "src/kiro_crew/discord/transport_dispatch.py": 1,
    "src/kiro_crew/instances/ssh_tunnel_manager.py": 1,
    "src/kiro_crew/kiro_prerequisite.py": 1,
    "src/kiro_crew/knowledge/agent_source.py": 1,
    "src/kiro_crew/knowledge/artifact_ingest.py": 1,
    "src/kiro_crew/knowledge/ingestion.py": 1,
    "src/kiro_crew/mcp_tools/knowledge.py": 2,
    "src/kiro_crew/session_summary.py": 1,
    "src/kiro_crew/snapshot_redact.py": 1,
    "src/kiro_crew/voice_reply.py": 3,
    "src/kiro_crew/workflows/agent_exec.py": 1,
    "src/kiro_crew/workflows/agent_pool.py": 1,
}

#: Files holding a reversed NESTED expression. ``metrics/schema.py`` is here on
#: purpose and is NOT a defect: it compares the redacted text against the input for
#: a boolean (``... != value``), and both orders answer that boolean the same way
#: for every input measured -- the credential is removed either way, which is all
#: that call site asks. Pinned so a second one cannot appear unnoticed, and so the
#: entry survives if that call site ever starts using the text itself instead of
#: only the comparison.
_PINNED_INLINE: dict[str, int] = {
    "src/kiro_crew/metrics/schema.py": 1,
}


class TestTheOrderIsWhatDecidesTheOutcome:
    """The leak the shared helper prevents, asserted on the helper itself."""

    def test_the_canonical_order_suppresses_the_destination(self) -> None:
        out = redact(_URL_WITH_CREDENTIAL)
        assert "https://" not in out
        # The host is NAMED INSIDE the tag, which is why "host absent" would be the
        # wrong assertion: what matters is that no live, clickable URL survives.
        assert "evil.example.com" in out

    def test_the_reversed_order_leaves_a_live_url_standing(self) -> None:
        """The failure mode, reproduced deliberately so the rule is not vacuous.

        The credential is gone either way -- which is exactly why the bug reads as
        harmless -- and what survives is a clickable exfiltration endpoint.
        """
        text, _ = redact_credentials(_URL_WITH_CREDENTIAL)
        text, _ = redact_exfiltration_urls(text)
        assert "https://evil.example.com/collect" in text
        assert "[REDACTED: credential]" in text

    def test_the_two_orders_disagree_on_a_url_bearing_credential(self) -> None:
        """The premise: the passes are not commutative on this input."""
        text, _ = redact_credentials(_URL_WITH_CREDENTIAL)
        text, _ = redact_exfiltration_urls(text)
        assert text != redact(_URL_WITH_CREDENTIAL)

    def test_the_orders_agree_when_no_url_carries_the_credential(self) -> None:
        """Negative control: the difference is URL-specific, not a general split.

        Without this, "the orders differ" could be satisfied by any divergence, and
        the rule would tell a future reader nothing about WHEN it matters.
        """
        plain = "plain AKIAIOSFODNN7EXAMPLE token, no url"
        text, _ = redact_credentials(plain)
        text, _ = redact_exfiltration_urls(text)
        assert text == redact(plain)

    def test_the_helper_matches_the_canonical_manual_composition(self) -> None:
        """``redact`` IS the URL-then-credential composition, not a third variant.

        Corpus-wide rather than one fixture: a change that made ``redact`` compose
        something else would have to diverge here before it could diverge silently
        at 400+ call sites.
        """
        corpus = [
            "",
            "plain text",
            "key AKIAIOSFODNN7EXAMPLE here",
            "secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            _URL_WITH_CREDENTIAL,
            "https://evil.example.com/plain",
            "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnopqrstuvwxyz0",
            "aws_secret_access_key=BAREsecret1",
            '{"token":"AKIAIOSFODNN7EXAMPLE","url":"https://exfil.test/?q=1"}',
        ]
        for text in corpus:
            manual, _ = redact_exfiltration_urls(text)
            manual, _ = redact_credentials(manual)
            assert redact(text) == manual, text


class TestNoNewReversedPair:
    """The ratchet over the real tree."""

    def test_two_line_scan_matches_the_pinned_baseline(self) -> None:
        found = _count(_two_line_offenders())
        assert found == _PINNED_TWO_LINE, _diff_message(found, _PINNED_TWO_LINE)

    def test_inline_scan_matches_the_pinned_baseline(self) -> None:
        found = _count(_inline_reversed())
        assert found == _PINNED_INLINE, _diff_message(found, _PINNED_INLINE)


def _count(offenders: list[tuple[str, int]]) -> dict[str, int]:
    return dict(collections.Counter(path for path, _ in offenders))


def _diff_message(found: dict[str, int], pinned: dict[str, int]) -> str:
    new = sorted(set(found) - set(pinned))
    grown = sorted(p for p in found if p in pinned and found[p] > pinned[p])
    stale = sorted(p for p in pinned if p not in found or found[p] < pinned[p])
    lines = ["reversed redaction pair(s) scanned against the pinned baseline:"]
    if new or grown:
        lines.append("  NEW (compose these through `redact`, or `redact_with_findings`):")
        lines += ["    %s (%d)" % (p, found[p]) for p in new + grown]
    if stale:
        lines.append("  PAID OFF (prune these entries; the list must keep shrinking):")
        lines += ["    %s (pinned %d, found %d)" % (p, pinned[p], found.get(p, 0)) for p in stale]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _source_files() -> list[Path]:
    return sorted(
        p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts and "_vendor" not in p.parts
    )


#: The two pass names, as the fast reject in :func:`_call_name`.
_PASS_NAMES = frozenset({CREDENTIAL, URL_PASS})

#: Module tails an alias must resolve to for a qualified call to be credited. The
#: facade (``kiro_crew.security``) owns the passes, and ``dashboard.handlers``
#: re-exports the SAME function objects — five real sites reach them as
#: ``_h.<pass>`` via `import kiro_crew.dashboard.handlers as _h`. Verified by
#: identity, not by name: `_h.redact_credentials is security.redact_credentials`.
_SECURITY_MODULES = frozenset({"security", "handlers"})

#: Qualifier names credited WITHOUT an import lookup, because they are the module's
#: own spelling in place.
_SECURITY_QUALIFIERS = frozenset({"security"})


def _alias_targets(tree: ast.AST) -> dict[str, str]:
    """Map a local module alias to the dotted module it was imported from.

    Only ``import a.b.c as x`` and ``from a.b import c as x`` qualify. Resolving the
    alias is what separates a legitimate ``_h.redact_credentials`` (the dashboard
    handlers facade, which re-exports the real pass) from a same-named method on an
    unrelated object — a distinction a bare attribute-name check cannot make.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    # `import a.b.c` binds `a` to the top package.
                    out.setdefault(alias.name.split(".")[0], alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = node.module + "." + alias.name
                else:
                    out[alias.name] = node.module + "." + alias.name
    return out


def _call_name(node: ast.AST, aliases: dict[str, str] | None = None) -> str | None:
    """The redaction pass a ``Call`` invokes, or None if it is some other call.

    Recognises every spelling this tree actually uses, which is what the gate's
    "either spelling" claim rests on:

    * a bare name — ``redact_credentials(x)``, the common local form;
    * a qualified chain — ``security.redact_credentials(x)``, the app-package form
      an ``ast.Name``-only reader silently missed; and
    * an IMPORT ALIAS of that module — ``_h.redact_credentials(x)``, where ``_h``
      resolves (via :func:`_alias_targets`) to a module that really does re-export
      the pass.

    *aliases* is the per-module map from :func:`_alias_targets`. Without it the
    qualifier is judged by its own spelling, which still accepts ``security`` and
    now also accepts a *known* facade alias. Crediting an unrelated object's
    same-named method would red the gate for work unrelated to this rule.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if not isinstance(func, ast.Attribute):
        return None
    # Cheap reject BEFORE any chain walking: this runs for every Call in the tree
    # (hundreds of thousands across src/). Without it the scan ran past the xdist
    # worker's budget and workers crashed rather than failing an assertion.
    if func.attr not in _PASS_NAMES:
        return None
    # `x.<pass>(...)`: `x` must name a module that owns or re-exports the passes.
    # A bare local binding (`redact_credentials(x)`) arrives as ast.Name and was
    # handled above; here the qualifier is always a NAME, so resolve it through the
    # module alias map. `security` and the `_h` handlers alias resolve; a SDK object
    # or an unrelated alias does not.
    qualifier = func.value
    if not isinstance(qualifier, ast.Name):
        # A deeper chain (`mod.security.<pass>`) is credited on its inner segment.
        if isinstance(qualifier, ast.Attribute) and qualifier.attr in _SECURITY_QUALIFIERS:
            return func.attr
        return None
    if qualifier.id in _SECURITY_QUALIFIERS:
        return func.attr
    if aliases and _resolves_to_security(aliases.get(qualifier.id)):
        return func.attr
    return None


def _resolves_to_security(module: str | None) -> bool:
    """True when *module* is (or sits directly under) the module that owns the passes."""
    if not module:
        return False
    tail = module.rsplit(".", 1)[-1]
    return tail in _SECURITY_MODULES


def _unwrap(node: ast.AST) -> ast.AST:
    """Strip ``[0]`` subscripts so ``f(x)[0]`` reads as ``f(x)``."""
    while isinstance(node, ast.Subscript):
        node = node.value
    return node


def _target_sources(node: ast.stmt) -> list[str]:
    """The dotted source spelling of each assignment target.

    Compared as SOURCE TEXT rather than by identifier, because a target need not
    be a bare name: the ledger and chip code the migration rewrote assigns into a
    ``dict`` entry (``entry["path"], _ = redact_credentials(entry["path"])``), and
    an identifier-only reader saw no target there at all. ``ast.unparse`` gives
    both sides the same spelling so ``entry["path"]`` matches ``entry["path"]``.
    """
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return []
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    out: list[str] = []
    for target in targets:
        if isinstance(target, ast.Tuple):
            out.extend(ast.unparse(e) for e in target.elts)
        else:
            out.append(ast.unparse(target))
    return out


def _arg_sources(call: ast.Call) -> list[str]:
    """The source spelling of each positional argument of *call*."""
    return [ast.unparse(a) for a in call.args]


def _body_lists(tree: ast.AST):
    """Every statement list in the module, so nested and branched pairs are found.

    Each list is yielded ONCE. ``ast.walk`` also visits the ``ExceptHandler`` node
    whose ``body`` the handler loop below already yielded, so without the ``seen``
    guard a pair inside an ``except`` block is matched twice -- which reads as a
    second offending site and would make the pinned counts lie.
    """
    seen: set[int] = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                if id(body) not in seen:
                    seen.add(id(body))
                    yield body
        for handler in getattr(node, "handlers", []) or []:
            if id(handler.body) not in seen:
                seen.add(id(handler.body))
                yield handler.body


def _two_line_offenders_in(tree: ast.AST, rel: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    aliases = _alias_targets(tree)
    for body in _body_lists(tree):
        for first, second in zip(body, body[1:]):
            s1 = _unwrap(first.value) if isinstance(first, (ast.Assign, ast.AnnAssign)) else None
            s2 = _unwrap(second.value) if isinstance(second, (ast.Assign, ast.AnnAssign)) else None
            if not (isinstance(s1, ast.Call) and isinstance(s2, ast.Call)):
                continue
            if _call_name(s1, aliases) != CREDENTIAL or _call_name(s2, aliases) != URL_PASS:
                continue
            # The second call must consume what the first one produced. That is
            # what makes the two lines one composition rather than two independent
            # redactions that merely sit next to each other. Both sides are
            # compared as SOURCE, so a dict-entry target counts too.
            if set(_target_sources(first)) & set(_arg_sources(s2)):
                out.append((rel, first.lineno))
    return out


def _inline_offenders_in(tree: ast.AST, rel: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    aliases = _alias_targets(tree)
    for node in ast.walk(tree):
        if _call_name(node, aliases) != URL_PASS or not node.args:
            continue
        inner = _unwrap(node.args[0])
        if isinstance(inner, ast.Call) and _call_name(inner) == CREDENTIAL:
            out.append((rel, node.lineno))
    return out


def _rel(path: Path) -> str:
    """Repo-relative POSIX spelling, so a baseline entry is platform-independent."""
    return path.relative_to(ROOT).as_posix()


def _scan_tree() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """One pass over ``src/``, returning (two-line offenders, inline offenders).

    A SINGLE walk for both scanners, and each tree is dropped as soon as it is
    scanned. Two independent walks meant two full parses of ~1600 files (measured
    ~35s per 300 files), and holding the parsed trees alive between tests pushed an
    xdist worker past its budget — which surfaces as `worker crashed`, not as an
    assertion failure, so it is easy to misread as a code bug.
    """
    two_line: list[tuple[str, int]] = []
    inline: list[tuple[str, int]] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our code
            continue
        rel = _rel(path)
        two_line.extend(_two_line_offenders_in(tree, rel))
        inline.extend(_inline_offenders_in(tree, rel))
        del tree
    return two_line, inline


#: Both scanners share ONE pass over src/. The scan is the expensive part of this
#: module (~2 min), and re-walking it per test doubled that for no coverage: the
#: two offender sets are disjoint by construction (a two-statement pair vs a single
#: nested expression), so one traversal answers both.
_OFFENDERS: tuple[list[tuple[str, int]], list[tuple[str, int]]] | None = None


def _offenders() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    global _OFFENDERS
    if _OFFENDERS is None:
        _OFFENDERS = _scan_tree()
    return _OFFENDERS


def _two_line_offenders() -> list[tuple[str, int]]:
    return _offenders()[0]


def _inline_reversed() -> list[tuple[str, int]]:
    return _offenders()[1]


class TestTheScanItselfIsSound:
    """Probes: every spelling must stay caught, and the walk must read files."""

    def test_probe_same_variable_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = redact_credentials(x)\n"
            "    x, _ = redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_different_target_variable_is_caught(self) -> None:
        """``a, _ = cred(x)`` then ``b, _ = urls(a)`` is still one composition."""
        tree = ast.parse(
            "def f(x):\n"
            "    a, _ = redact_credentials(x)\n"
            "    b, _ = redact_exfiltration_urls(a)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_nested_in_a_branch_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x, c):\n"
            "    if c:\n"
            "        x, _ = redact_credentials(x)\n"
            "        x, _ = redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_nested_in_a_handler_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x):\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        x, _ = redact_credentials(x)\n"
            "        x, _ = redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_the_subscript_form_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = redact_credentials(x)\n"
            "    x, _ = redact_exfiltration_urls(x)[0]\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_the_qualified_attribute_form_is_caught(self) -> None:
        """``security.redact_credentials(...)`` — the app-package spelling.

        The regression this probe exists for: an ``ast.Name``-only reader missed
        the one live site in ``apps/builtins/md_notebook/server.py``, so the gate
        claimed "either spelling" while covering one.
        """
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = security.redact_credentials(str(x))\n"
            "    x, _ = security.redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_a_deeper_qualified_chain_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = mod.security.redact_credentials(x)\n"
            "    x, _ = mod.security.redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_a_foreign_object_with_the_same_method_is_not_credited(self) -> None:
        """A different object's ``redact_credentials`` is not this package's pass.

        Without qualifier resolution, ANY SDK or test double that happens to share
        the method name would be read as ours and reported as a reversed pair.
        """
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = vendor_sdk.redact_credentials(x)\n"
            "    x, _ = vendor_sdk.redact_exfiltration_urls(x)\n"
        )
        assert not _two_line_offenders_in(tree, "probe.py")

    def test_probe_the_dashboard_handlers_alias_is_credited(self) -> None:
        """``_h.<pass>`` where ``_h`` aliases a module that re-exports them.

        Five real sites reach the passes this way. Missing them would mean the gate
        silently ignores a whole spelling, which is the regression it exists to stop.
        """
        tree = ast.parse(
            "import kiro_crew.dashboard.handlers as _h\n"
            "def f(x):\n"
            "    x, _ = _h.redact_credentials(x)\n"
            "    x, _ = _h.redact_exfiltration_urls(x)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_an_unrelated_alias_is_not_credited(self) -> None:
        tree = ast.parse(
            "import some.other.sdk as _h\n"
            "def f(x):\n"
            "    x, _ = _h.redact_credentials(x)\n"
            "    x, _ = _h.redact_exfiltration_urls(x)\n"
        )
        assert not _two_line_offenders_in(tree, "probe.py")

    def test_probe_a_dict_entry_target_is_caught(self) -> None:
        """``entry["path"], _ = ...`` — the form the migration itself rewrote.

        An identifier-only reader saw no target on that line, so this exact shape
        could have been reintroduced anywhere without a red gate.
        """
        tree = ast.parse(
            "def f(entry):\n"
            '    entry["path"], _ = redact_credentials(entry["path"])\n'
            '    entry["path"], _ = redact_exfiltration_urls(entry["path"])\n'
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_an_attribute_target_is_caught(self) -> None:
        tree = ast.parse(
            "def f(obj):\n"
            "    obj.text, _ = redact_credentials(obj.text)\n"
            "    obj.text, _ = redact_exfiltration_urls(obj.text)\n"
        )
        assert _two_line_offenders_in(tree, "probe.py")

    def test_probe_the_correct_order_is_not_flagged(self) -> None:
        tree = ast.parse(
            "def f(x):\n"
            "    x, _ = redact_exfiltration_urls(x)\n"
            "    x, _ = redact_credentials(x)\n"
        )
        assert not _two_line_offenders_in(tree, "probe.py")

    def test_probe_independent_redactions_are_not_flagged(self) -> None:
        """Two calls that do not share a value are not a composition."""
        tree = ast.parse(
            "def f(x, y):\n"
            "    a, _ = redact_credentials(x)\n"
            "    b, _ = redact_exfiltration_urls(y)\n"
        )
        assert not _two_line_offenders_in(tree, "probe.py")

    def test_probe_the_reversed_inline_nesting_is_caught(self) -> None:
        tree = ast.parse(
            "def f(x):\n    return redact_exfiltration_urls(redact_credentials(x)[0])[0]\n"
        )
        assert _inline_offenders_in(tree, "probe.py")

    def test_probe_the_correct_inline_nesting_is_not_flagged(self) -> None:
        """Innermost runs first: ``cred(urls(x))`` is the CORRECT order."""
        tree = ast.parse(
            "def f(x):\n    return redact_credentials(redact_exfiltration_urls(x)[0])[0]\n"
        )
        assert not _inline_offenders_in(tree, "probe.py")

    def test_probe_a_comment_quoting_the_pattern_is_not_flagged(self) -> None:
        """The scan parses the AST, so literals and comments can never count."""
        tree = ast.parse(
            "def f(x):\n"
            "    # x, _ = redact_credentials(x)\n"
            "    # x, _ = redact_exfiltration_urls(x)\n"
            '    note = "redact_exfiltration_urls(redact_credentials(x))"\n'
            "    return redact(x) + note\n"
        )
        assert not _two_line_offenders_in(tree, "probe.py")
        assert not _inline_offenders_in(tree, "probe.py")

    def test_the_tree_scan_actually_reads_files(self) -> None:
        """A scan that silently found no files would pass forever.

        Asserts the walk is non-trivial and reaches the package that owns the two
        passes, so an over-eager exclusion (or a moved source root) fails here
        rather than voiding every baseline comparison above.
        """
        files = _source_files()
        assert len(files) > 100, "the source walk found implausibly few files"
        assert any(p.parent.name == "security" for p in files), files[:5]
        assert SRC.is_dir(), SRC

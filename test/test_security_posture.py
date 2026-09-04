"""Tests for the security-posture detail registry (``security_posture``).

The registry exists because Settings → Security used to render hardcoded counts
that had silently gone stale by 2-3x. These tests pin the two properties that
make that regression structurally impossible:

1. **Derivation** — every ``count`` is ``len(items)``, and the items come from the
   LIVE control (``security._SENSITIVE_HOME_DIRS``, ``BUILTIN_DENIED_RULES``,
   ``SUSPICIOUS_BASH_PATTERNS``, the validation schemas). A control that grows
   without its posture entry growing fails here.
2. **Disclosure** — the payload carries public control definitions and derived
   counts only: no credential material, no governance rule contents, no user data.
"""

from __future__ import annotations

import ast
import base64
import functools
import re

import pytest

import kiro_crew.validation as validation
from kiro_crew import security, security_posture
from kiro_crew import sel as _sel_mod
from kiro_crew.security_posture import (
    _CONTROLS,
    PostureItem,
    build_posture_snapshot,
    build_posture_snapshot_async,
)

# ANY use of a redactor, including every wrapper.
#
# This alternation is load-bearing: the guard below can only classify a module it
# SEES, so a redactor form missing from here is a hole of exactly the kind the
# original "5 output paths" bug was — one level up. An earlier version listed only
# the three primitives and used a `(?<![_.\w])redact\(` lookbehind, which silently
# skipped seven modules: `redact_and_truncate` (the Slack egress in
# `dashboard/chat_slack.py` + `slack/blocks.py`), `redact_via_context`, and
# qualified `security.redact(...)` calls (excluded by the very lookbehind meant to
# avoid matching `_redact(`). Prefer over-matching here — an extra module just has
# to be classified once — and keep `\w*redact` broad enough that a NEW wrapper name
# is caught by default rather than ignored by default.
_REDACTOR_CALL_RE = re.compile(
    r"\bStreamRedactor\("  # streaming redactor
    r"|\b\w*redact\w*\("  # redact/redact_credentials/redact_and_truncate/_redact/...
    r"|\.redact\("  # qualified: security.redact(...), credentials.redact(...)
)


#: The baseline (companion-blind) redactor entry points defined in ``security.py``.
#: A call to any of these runs the OSS credential/exfil pass and nothing else, so on
#: a host with a companion loaded it misses whatever the companion's extra regexes
#: would have caught. The context spellings (``redact_via_context`` for an egress
#: sink, ``redact_log_via_context`` for a gate-side log line) are deliberately NOT
#: here: they are the answer, not the finding.
_BASELINE_REDACTORS = frozenset(
    {
        "redact",
        "redact_credentials",
        "redact_exfiltration_urls",
        "redact_and_truncate",
    }
)

#: Logger method names. Paired with a receiver whose source spells ``log`` (so
#: ``logger.warning``, ``self._log.debug`` and ``LOG.error`` all count, while
#: ``resp.warning`` does not).
_LOG_LEVEL_ATTRS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})


def _is_log_write(call: ast.Call) -> bool:
    """True when *call* writes an operational log or audit line.

    Keys on how a log WRITE is spelled — a logger level method, or a ``log_*``
    function/method such as the SEL rows ``log_tool_invocation`` /
    ``log_api_access`` / ``log_governance_decision``. That is deliberately the
    only name-shaped input to this scan: the SUBJECT of the line is matched
    structurally (see :func:`_gate_side_baseline_log_sites`), because keying on
    the subject's name is exactly what missed ``task_planner.py`` (an LLM
    response) and ``name_grant.py`` (a model-authored title) when the class was
    first swept with a grep scoped to files mentioning stderr.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in _LOG_LEVEL_ATTRS:
            return "log" in ast.unparse(func.value).lower()
        return func.attr.startswith("log_")
    if isinstance(func, ast.Name):
        return func.id.startswith("log_")
    return False


def _wraps_baseline_call(node: ast.AST | None) -> bool:
    """True when *node* is, or contains, a call to a baseline redactor."""
    if node is None:
        return False
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, (ast.Name, ast.Attribute))
        and (sub.func.id if isinstance(sub.func, ast.Name) else sub.func.attr)
        in _BASELINE_REDACTORS
        for sub in ast.walk(node)
    )


def _scope_body(scope: ast.AST):
    """Walk *scope*'s own statements, not those of a nested def/class.

    Taint must not leak between scopes: a name redacted inside one method says
    nothing about a same-named local in a sibling method. Lambdas and
    comprehensions are NOT stopped at — they bind no assignments this scan cares
    about, and stopping would hide a redactor call nested in one.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _gate_side_baseline_log_sites(
    source: str, *, tree: ast.AST | None = None
) -> set[tuple[int, str]]:
    """Find log/audit writes in *source* whose text came from a BASELINE redactor.

    The property, not a name: a baseline redactor's result reaches a log write —
    either nested directly in the call (``logger.error("%s", redact(x))``) or
    through a local whose most recent assignment in the same scope was a baseline
    call (the ``x, _ = redact_exfiltration_urls(x)`` / ``x, _ =
    redact_credentials(x)`` pair idiom, which is a hand-rolled ``security.redact``
    and the exact shape #7151 converged in three modules).

    Flow-lite, and honest about it: the most recent assignment WINS, so a name
    reassigned from an unredacted source stops counting, but branches and loops
    are not modelled and a value redacted in a helper and logged by its caller is
    not seen. This is a floor on the class, not a proof of its absence.

    Returns ``{(lineno, "<log call>")}`` — the count is what the ratchet pins;
    the rendering is for the failure message.
    """
    if tree is None:
        tree = ast.parse(source)
    scopes: list[ast.AST] = [tree]
    scopes += [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    sites: set[tuple[int, str]] = set()
    for scope in scopes:
        # (lineno, name, came-from-a-baseline-redactor). Collection order does not
        # matter: the line number is what decides which assignment a log write sees.
        assignments: list[tuple[int, str, bool]] = []
        log_writes: list[ast.Call] = []
        for node in _scope_body(scope):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            for target in targets:
                # `obj.attr = redact(...)` and `d[k] = redact(...)` do NOT make the
                # BASE object redacted text; tainting it would fire on every later
                # log line that merely mentions the object.
                if isinstance(target, ast.Name):
                    bound = [target]
                elif isinstance(target, (ast.Tuple, ast.List)):
                    bound = [e for e in target.elts if isinstance(e, ast.Name)]
                else:
                    bound = []
                for name_node in bound:
                    assignments.append(
                        (name_node.lineno, name_node.id, _wraps_baseline_call(value))
                    )
            if isinstance(node, ast.Call) and _is_log_write(node):
                log_writes.append(node)
        for call in log_writes:
            args = list(call.args) + [kw.value for kw in call.keywords]
            if any(_wraps_baseline_call(arg) for arg in args):
                sites.add((call.lineno, ast.unparse(call.func)))
                continue
            referenced = {
                sub.id for arg in args for sub in ast.walk(arg) if isinstance(sub, ast.Name)
            }
            for name in sorted(referenced):
                prior = [
                    (lineno, tainted)
                    for lineno, bound_name, tainted in assignments
                    if bound_name == name and lineno <= call.lineno
                ]
                if prior and max(prior)[1]:
                    sites.add((call.lineno, ast.unparse(call.func)))
                    break
    return sites


#: Gate-side log/audit sites still reading the BASELINE redactor, counted per
#: module on the commit that added this ratchet. A census, and three things it is
#: NOT:
#:
#: * NOT a to-do list. For a site in a process that never composes a companion the
#:   baseline is the honest answer, not a defect — ``mcp_gateway/backend.py`` is
#:   exactly that case (``gatewayd`` never runs ``boot_platform``, which
#:   ``mcp_gateway/app_call.py`` relies on for its security ceiling), and
#:   converting it would trade every backend diagnostic for a per-line lazy
#:   resolution on the event loop.
#: * NOT a list of approved exceptions. No entry here has been ruled correct. The
#:   only claim these numbers make is "this many were here when the gate went up".
#: * NOT the egress axis. ``NON_EGRESS_REDACTION_MODULES`` and ``_REDACTION_SINKS``
#:   decide whether a module is an output boundary; that decision says nothing
#:   about WHICH redactor spelling a site uses, which is why all three sites #7151
#:   converged sat correctly bucketed for years while reading the weaker pass.
#:
#: What the gate buys: a NEW gate-side log line cannot be born on the baseline
#: silently. Growth in any module — or a first site in a module absent from here —
#: fails, and clearing it means either calling ``redact_log_via_context`` or
#: raising the number and saying which PROCESS the site runs in and why the
#: baseline is right there, to the standard that helper's docstring sets.
#:
#: Two limits, stated so nobody reads more into a green run: counts do not pin
#: identity, so converting one site while adding another inside the SAME module
#: nets to zero (both edits land in one reviewed diff), and the scan is
#: single-scope (see :func:`_gate_side_baseline_log_sites`).
_BASELINE_LOG_SITE_CENSUS: dict[str, int] = {
    "acp/client.py": 7,
    "apps/builtins/pptx_maker/backend/routes.py": 1,
    "dashboard/chat_nav.py": 1,
    "dashboard/chat_orchestrator.py": 1,
    "dashboard/chat_runner.py": 10,
    "dashboard/chat_title.py": 1,
    "dashboard/handlers/discover.py": 3,
    "dashboard/handlers/files.py": 1,
    "dashboard/handlers/hooks.py": 1,
    "dashboard/handlers/memory.py": 2,
    "dashboard/handlers/messaging.py": 12,
    "dashboard/handlers/updates.py": 1,
    "dashboard/session_control.py": 1,
    "dashboard/session_transfer.py": 1,
    "dashboard/state.py": 1,
    "knowledge/agent_fetch.py": 1,
    "mcp_cron.py": 1,
    "mcp_gateway/backend.py": 2,
    "mcp_tools/knowledge.py": 5,
    "mcp_tools/messaging.py": 1,
    "mcp_tools/skills.py": 2,
    "messaging/sessions_view.py": 1,
    "slack/events.py": 2,
    "slack/gateway.py": 6,
    "slack/handler.py": 3,
    "subagent_manager/admission.py": 4,
    "voice_reply.py": 4,
}


@functools.lru_cache(maxsize=1)
def _package_baseline_log_census() -> "tuple[tuple[str, int], ...]":
    """Live per-module count of gate-side log writes reading a baseline redactor.

    Cached because two tests read it and the walk parses every package module;
    returned as a tuple of pairs so the cached value cannot be mutated by a
    caller. A ``SyntaxError`` is deliberately NOT swallowed: a module the scanner
    cannot parse is a module it cannot see, and skipping one quietly is how a
    scan-based gate goes blind.
    """
    from source_corpus import parsed_candidates, src_root  # noqa: PLC0415

    pkg = src_root()
    found: list[tuple[str, int]] = []
    # Every name in `_BASELINE_REDACTORS` starts with "redact", so a module
    # without that text cannot hold a site and is not worth parsing. The
    # SyntaxError is still not swallowed (`skip_syntax_errors=False`).
    for path, source, tree in parsed_candidates(("redact",), skip_syntax_errors=False):
        rel = path.relative_to(pkg).as_posix()
        if rel.startswith(("_vendor/", "testing/")):
            continue
        if "/tests/" in rel or rel.endswith("_test.py"):
            continue
        sites = _gate_side_baseline_log_sites(source, tree=tree)
        if sites:
            found.append((rel, len(sites)))
    return tuple(found)


def _mcp_schema_registry_names() -> list[str]:
    """Every ``MCP_*_SCHEMAS`` dispatch registry in ``validation``, DISCOVERED.

    Deliberately not a hardcoded list: hardcoding is what let the ten
    ``MCP_COMPUTER_SCHEMAS`` tools go missing from the posture report while the drift
    test that exists to catch that stayed green, because the test named the same two
    registries the implementation did.
    """
    return sorted(
        name
        for name in dir(validation)
        if name.startswith("MCP_")
        and name.endswith("_SCHEMAS")
        and isinstance(getattr(validation, name), dict)
    )


@pytest.fixture()
def snapshot():
    return build_posture_snapshot()


class TestSnapshotShape:
    def test_every_control_is_present_with_the_documented_fields(self, snapshot):
        keys = [c["key"] for c in snapshot["controls"]]
        assert keys == [c.key for c in _CONTROLS]
        for control in snapshot["controls"]:
            assert set(control) == {
                "key",
                "label",
                "unit",
                "summary",
                "source",
                "count",
                "items",
                "unavailable",
            }
            assert control["label"]
            assert control["unit"]

    def test_counts_map_mirrors_the_controls(self, snapshot):
        assert snapshot["counts"] == {c["key"]: c["count"] for c in snapshot["controls"]}

    def test_count_is_always_len_items(self, snapshot):
        # The invariant the whole feature rests on: a pill can never disagree with
        # the list it summarizes.
        for control in snapshot["controls"]:
            assert control["count"] == len(control["items"]), control["key"]

    def test_no_control_is_empty_or_unavailable_on_a_healthy_host(self, snapshot):
        for control in snapshot["controls"]:
            assert control["unavailable"] is False, control["key"]
            assert control["count"] > 0, control["key"]

    def test_every_item_has_a_label(self, snapshot):
        for control in snapshot["controls"]:
            for item in control["items"]:
                assert set(item) == {"label", "detail"}
                assert item["label"].strip(), control["key"]

    def test_source_paths_point_at_real_repo_modules(self, snapshot):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        for control in snapshot["controls"]:
            if not control["source"]:
                continue
            # The UI turns `source` into a GitHub deep link; a typo would render a
            # 404 for every reader of that row.
            assert (repo_root / control["source"]).is_file(), control["source"]

    @pytest.mark.asyncio
    async def test_async_builder_matches_the_sync_one(self, snapshot):
        assert await build_posture_snapshot_async() == snapshot


class TestDerivation:
    """Each count must be derived from the live control, never a literal."""

    def _control(self, snapshot, key):
        return next(c for c in snapshot["controls"] if c["key"] == key)

    def test_sensitive_paths_tracks_the_live_blocklist(self, snapshot):
        control = self._control(snapshot, "sensitive_paths")
        assert control["count"] == len(security.sensitive_home_dirs())
        labels = {i["label"] for i in control["items"]}
        assert "~/.aws" in labels
        assert "~/.ssh" in labels

    def test_write_protected_paths_tracks_the_live_list(self, snapshot):
        control = self._control(snapshot, "write_protected_paths")
        assert control["count"] == len(security.write_protected_home_paths())

    def test_denied_commands_tracks_the_builtin_rule_table(self, snapshot):
        control = self._control(snapshot, "denied_commands")
        assert control["count"] == len(security.BUILTIN_DENIED_RULES)

    def test_suspicious_patterns_tracks_the_live_pattern_list(self, snapshot):
        control = self._control(snapshot, "suspicious_patterns")
        assert control["count"] == len(security.SUSPICIOUS_BASH_PATTERNS)
        assert {i["label"] for i in control["items"]} == set(security.SUSPICIOUS_BASH_PATTERNS)

    def test_tool_schemas_covers_every_dispatch_registry_entry(self, snapshot):
        """Derive from the REGISTRY, not the naming convention.

        Asserting against ``dir(validation)`` + ``*_SCHEMA`` would just re-run the
        implementation's own selection predicate — a tautology that cannot fail.
        The registries are what ``mcp_core``/``mcp_cron`` dispatch on, so they are
        the honest source of truth: every registered tool must be represented,
        including the ones defined as inline/shared ToolSchema objects with no
        module-level name.
        """
        control = self._control(snapshot, "tool_schemas")
        labels = {i["label"] for i in control["items"]}
        registered: set[str] = set()
        for name in _mcp_schema_registry_names():
            registered |= set(getattr(validation, name))
        missing = registered - labels
        assert not missing, f"registered MCP tools absent from the posture view: {missing}"

    def test_every_mcp_schema_registry_is_covered_by_the_posture_view(self, snapshot):
        """The drift guard's OWN blind spot, closed.

        This class previously hardcoded ``MCP_CORE_SCHEMAS | MCP_CRON_SCHEMAS`` — the
        same two names the implementation hardcoded — so when ``MCP_COMPUTER_SCHEMAS``
        was added, all ten computer-use tools were absent from the security-posture
        report and the test written to catch exactly that stayed green. Discovering the
        registries from ``validation`` instead means a NEW one fails here until it is
        added to ``security_posture._SCHEMA_REGISTRY_NAMES``.
        """
        from kiro_crew import security_posture

        discovered = set(_mcp_schema_registry_names())
        assert discovered, "no MCP_*_SCHEMAS registries found in validation"
        declared = set(security_posture._SCHEMA_REGISTRY_NAMES)
        assert discovered <= declared, (
            f"registries {sorted(discovered - declared)} exist in validation but are not "
            "in security_posture._SCHEMA_REGISTRY_NAMES — their tools are validated but "
            "invisible in the posture report"
        )
        # And every declared name must still exist, so a rename cannot leave a
        # silently-empty entry behind.
        for name in declared:
            assert isinstance(getattr(validation, name, None), dict), (
                f"security_posture._SCHEMA_REGISTRY_NAMES lists {name!r}, which is no "
                "longer a dict in validation"
            )

    def test_tool_schemas_includes_inline_registry_only_schemas(self, snapshot):
        """Regression guard for the naming-convention blind spot.

        ``cron_trigger`` is registered in ``MCP_CRON_SCHEMAS`` but has no
        module-level ``CRON_TRIGGER_SCHEMA``, so the old ``dir()``-walk missed it.
        """
        control = self._control(snapshot, "tool_schemas")
        labels = {i["label"] for i in control["items"]}
        assert "cron_trigger" in validation.MCP_CRON_SCHEMAS
        assert not hasattr(validation, "CRON_TRIGGER_SCHEMA")
        assert "cron_trigger" in labels

    def test_exfil_heuristic_threshold_comes_from_the_enforcing_constant(self, snapshot):
        control = self._control(snapshot, "exfil_heuristics")
        labels = " ".join(i["label"] for i in control["items"])
        assert str(security.exfil_query_min_len()) in labels

    def test_token_auth_ttls_come_from_the_enforcing_module(self, snapshot):
        from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS

        control = self._control(snapshot, "token_auth")
        details = " ".join(i["detail"] for i in control["items"])
        assert f"{LINK_WINDOW_SECS // 60}-minute" in details
        assert f"{MAX_SESSION_TTL_SECS // 3600} hours" in details


class TestDisclosureContract:
    """Posture only — no secrets, no governance rule contents, no user data."""

    def test_denied_command_items_expose_descriptions_not_raw_regexes(self, snapshot):
        control = next(c for c in snapshot["controls"] if c["key"] == "denied_commands")
        descriptions = {r.description for r in security.BUILTIN_DENIED_RULES}
        patterns = {r.pattern for r in security.BUILTIN_DENIED_RULES}
        labels = {i["label"] for i in control["items"]}
        assert labels <= descriptions
        # The raw pattern surface already has its own opt-out UI (Card A's chevron);
        # the posture row is the human-readable summary, so it must not duplicate
        # the regex text here.
        assert not (labels & patterns)

    def test_credential_families_are_family_names_not_live_secrets(self, snapshot):
        control = next(c for c in snapshot["controls"] if c["key"] == "credential_families")
        blob = " ".join(f"{i['label']} {i['detail']}" for i in control["items"])
        # A real key would match the redaction scanner. Family names / prefixes
        # (e.g. "AKIA / ASIA key IDs") must not.
        redacted, warnings = security.redact_credentials(blob)
        assert warnings == []
        assert redacted == blob

    def test_no_control_leaks_governance_rule_contents(self, snapshot):
        # Governance rule contents are the ceiling the agent is fenced from; the
        # governance viewer deliberately ships counts only, and this endpoint must
        # not become a side channel around it.
        #
        # Asserted on PROVENANCE, not on control key names: a name-only guard
        # (`"governance" not in keys`) is trivially bypassed — a control keyed
        # `ceiling_scopes` could emit literal policy deny globs and still pass it.
        # Pinning the module as governance-free is the property that actually
        # holds the boundary: if this module cannot reach the governance
        # machinery, it cannot republish its rules under any key name.
        import inspect

        src = inspect.getsource(security_posture)
        for forbidden in (
            "platform.governance",
            "platform import governance",
            "governance_profiles",
            "resolve_active_scope",
            "GovernanceCeiling",
            "current_context",
            "security_policy",
            "admission_policy",
        ):
            # Comments legitimately discuss the boundary, so strip them first —
            # only real code references should fail.
            code = "\n".join(
                line.split("#", 1)[0]
                for line in src.splitlines()
                if not line.strip().startswith("#")
            )
            assert forbidden not in code, forbidden

    def test_whole_payload_survives_both_redaction_scanners(self, snapshot):
        # A description written with a live credential/URL shape in it would make
        # this very payload self-redact wherever it is itself scanned (the SEL
        # audit log, a Slack-relayed summary) — the row would render as
        # "[REDACTED: ...]". Both passes are pinned, not just credentials: an
        # embedded URL with a >=_EXFIL_QUERY_MIN_LEN query would otherwise slip
        # through the credential-only assertion.
        import json

        blob = json.dumps(snapshot)
        redacted, warnings = security.redact_credentials(blob)
        assert warnings == [], warnings
        assert redacted == blob

        redacted, warnings = security.redact_exfiltration_urls(blob)
        assert warnings == [], warnings
        assert redacted == blob

        # The dual-pass helper used at real output boundaries.
        assert security.redact(blob) == blob

    def test_the_redaction_guard_is_not_vacuous(self):
        # Guard the guard: if redact_credentials ever stopped firing, the
        # assertion above would pass trivially and the contract would be unpinned.
        _redacted, warnings = security.redact_credentials("AKIAIOSFODNN7EXAMPLE")
        assert warnings != []


class TestSensitivePathClassification:
    def test_crew_owned_paths_are_labelled_as_trust_roots(self, snapshot):
        control = next(c for c in snapshot["controls"] if c["key"] == "sensitive_paths")
        by_label = {i["label"]: i["detail"] for i in control["items"]}
        assert "trust root" in by_label["~/.kiro/crew/security_policy.json"]
        assert "Third-party" in by_label["~/.aws"]

    def test_classification_is_a_path_boundary_match(self, monkeypatch):
        # A sibling that merely shares a string prefix with a crew home must not be
        # mislabelled as ours.
        monkeypatch.setattr(
            security, "sensitive_home_dirs", lambda: (".kirocrew-notes/x", ".kiro/crew/.env")
        )
        items = security_posture._sensitive_path_items()
        by_label = {i.label: i.detail for i in items}
        assert "Third-party" in by_label["~/.kirocrew-notes/x"]
        assert "trust root" in by_label["~/.kiro/crew/.env"]


class TestFailureIsolation:
    def test_one_broken_control_does_not_sink_the_snapshot(self, monkeypatch):
        # This endpoint is purely informational — a control whose items cannot be
        # resolved must degrade to `unavailable`, leaving the rest readable.
        def boom():
            raise RuntimeError("control unavailable")

        broken = security_posture.PostureControl(
            key="broken",
            label="Broken control",
            unit="things",
            items_fn=boom,
        )
        monkeypatch.setattr(security_posture, "_CONTROLS", (broken, *_CONTROLS))

        snapshot = build_posture_snapshot()
        first = snapshot["controls"][0]
        assert first["key"] == "broken"
        assert first["unavailable"] is True
        # None, NOT 0 — a zero would tell the operator the control covers nothing.
        assert first["count"] is None
        assert first["items"] == []
        # Every real control still resolved.
        assert all(c["unavailable"] is False for c in snapshot["controls"][1:])

    def test_items_are_resolved_per_request_not_frozen_at_import(self, monkeypatch):
        # A governance profile reload or a user-added rule must be reflected without
        # a gateway restart, so items_fn is a callable, not a captured list.
        calls: list[int] = []

        def counting():
            calls.append(1)
            return [PostureItem(label="x")]

        control = security_posture.PostureControl(
            key="counted", label="Counted", unit="x", items_fn=counting
        )
        monkeypatch.setattr(security_posture, "_CONTROLS", (control,))
        build_posture_snapshot()
        build_posture_snapshot()
        assert len(calls) == 2


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_handler_serves_the_snapshot(self):
        import json
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.core import api_security_posture

        resp = await api_security_posture(MagicMock())
        assert resp.status == 200
        body = json.loads(resp.body.decode("utf-8"))
        assert body == build_posture_snapshot()

    def test_route_is_actually_registered_on_the_router(self):
        """Assert the ROUTE, not just the re-export.

        A `hasattr(handlers, ...)` check passes even if the
        `app.router.add_get("/api/security/posture", ...)` line is deleted — which
        would ship a 404 with the whole suite green.

        The registration lives in the route table under ``dashboard/routes/``, so
        both that package and ``server`` are scanned and the assertion holds
        wherever the route sits.
        """
        import importlib
        import inspect

        from kiro_crew.dashboard import handlers
        from kiro_crew.dashboard import routes as routes_pkg
        from kiro_crew.dashboard import server

        assert hasattr(handlers, "api_security_posture")
        src = inspect.getsource(server) + "".join(
            inspect.getsource(importlib.import_module(f"kiro_crew.dashboard.routes.{name}"))
            for name in routes_pkg.REGISTRAR_NAMES
        )
        assert '"/api/security/posture"' in src
        assert "handlers.api_security_posture" in src

    def test_stats_counts_come_from_the_posture_registry(self):
        # Guard the de-duplication: `api_security_stats` must not grow its own
        # parallel count logic again (that divergence is how the old hardcoded 5
        # survived while the real number reached 16).
        import inspect

        from kiro_crew.dashboard.handlers import core

        src = inspect.getsource(core.api_security_stats)
        assert "posture_counts_async" in src
        assert "SUSPICIOUS_BASH_PATTERNS" not in src

    @pytest.mark.asyncio
    async def test_counts_only_path_agrees_with_the_full_snapshot(self):
        """``posture_counts`` must not become a second, divergent count source.

        It exists purely to skip materializing the items for a counts-only caller,
        so it has to agree with the full snapshot exactly — otherwise
        ``/api/security/stats`` and ``/api/security/posture`` could disagree, which
        is the drift this whole change removes.
        """
        from kiro_crew.security_posture import posture_counts, posture_counts_async

        assert posture_counts() == build_posture_snapshot()["counts"]
        assert await posture_counts_async() == posture_counts()


class TestOmissionDetection:
    """The tests that would actually have caught the original bug.

    "5 output paths" was wrong because someone ADDED a redaction sink and nobody
    updated a hand-written list. Every assertion in this class runs in the
    *inverse* direction — from the live code TO the registry — because a
    ``count == len(items)`` check can never detect an omission from the list it
    is counting.
    """

    def test_every_redactor_call_site_is_a_registered_sink_or_allowlisted(self):
        """Walk the package for redactor call sites; each module must be classified.

        A new output path therefore cannot be added without someone deciding
        whether it is an egress boundary (→ ``_REDACTION_SINKS``, so it shows in
        the panel) or not (→ ``NON_EGRESS_REDACTION_MODULES``, with the reason in
        that set's comment). This is the guard that makes the "every output path"
        claim honest instead of aspirational.
        """
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        call = _REDACTOR_CALL_RE

        registered = {module for _label, module, _detail in security_posture._REDACTION_SINKS}
        allowlisted = security_posture.NON_EGRESS_REDACTION_MODULES
        # security.py DEFINES the redactors; security_posture.py only names them.
        self_referential = {"security.py", "security_posture.py"}

        unclassified: list[str] = []
        for path in sorted(pkg.rglob("*.py")):
            rel = path.relative_to(pkg).as_posix()
            if rel in self_referential or rel.startswith(("_vendor/", "testing/")):
                continue
            if "/tests/" in rel or rel.endswith("_test.py"):
                continue
            if not call.search(path.read_text(encoding="utf-8", errors="replace")):
                continue
            if rel in registered or rel in allowlisted:
                continue
            unclassified.append(rel)

        assert not unclassified, (
            "These modules call a redactor but are neither a registered "
            "`redaction_paths` sink nor in NON_EGRESS_REDACTION_MODULES. If a module "
            "is an output boundary, add it to _REDACTION_SINKS so the panel counts "
            "it; if not, allowlist it with a reason: " + ", ".join(unclassified)
        )

    def test_allowlist_has_no_stale_entries(self):
        """A path that no longer calls a redactor must leave the allowlist.

        Otherwise the allowlist silently grows into a place where a real sink can
        hide behind a dead entry.
        """
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        call = _REDACTOR_CALL_RE
        stale = [
            rel
            for rel in sorted(security_posture.NON_EGRESS_REDACTION_MODULES)
            if not (pkg / rel).is_file()
            or not call.search((pkg / rel).read_text(encoding="utf-8", errors="replace"))
        ]
        assert not stale, f"allowlisted but no longer calls a redactor: {stale}"

    def test_a_module_cannot_be_in_both_buckets(self):
        registered = {module for _label, module, _detail in security_posture._REDACTION_SINKS}
        overlap = registered & security_posture.NON_EGRESS_REDACTION_MODULES
        assert not overlap, f"classified as both egress and non-egress: {overlap}"

    def test_every_credential_family_is_actually_redacted(self):
        """Each advertised family must have a live pattern behind it.

        Pairs a synthetic (non-real) token shape with every family row, so a family
        whose regex alternative is deleted stops being advertised. The count
        assertion below is what catches the reverse — a regex alternative added
        without a family row.
        """
        samples = {
            "AWS access keys": "AKIAIOSFODNN7EXAMPLE",
            "Private keys": "-----BEGIN RSA PRIVATE KEY-----\nQUJD\n-----END RSA PRIVATE KEY-----",
            "Slack tokens": "xoxb-000000000000-abcdefghijkl",
            "GitHub tokens": "ghp_" + "a" * 36,
            "GitLab tokens": "glpat-" + "a" * 20,
            "Stripe keys": "sk_live_" + "a" * 24,
            "SendGrid keys": "SG." + "a" * 22 + "." + "b" * 22,
            "OpenAI keys": "sk-proj-" + "a" * 20,
            "Anthropic keys": "sk-ant-" + "a" * 20,
            "npm tokens": "npm_" + "a" * 36,
            "PyPI tokens": "pypi-" + "a" * 20,
            "DigitalOcean tokens": "dop_v1_" + "a" * 44,
            "Google OAuth secrets": "GOCSPX-" + "a" * 24,
            "Telegram bot tokens": "123456789:" + "A" * 35,
            "JWT / JWE tokens": "eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.c2ln",
            "HTTP bearer tokens": "Authorization: Bearer abc.def-ghi~jkl",
            "Database URIs": "postgres://user:secret@db.example.com:5432/app",
            "Base64-encoded variants": base64.b64encode(
                b"AKIAIOSFODNN7EXAMPLE and more padding text here"
            ).decode(),
        }
        advertised = [i.label for i in security_posture._credential_family_items()]

        # Reverse direction: a new regex alternative without a family row.
        assert sorted(advertised) == sorted(samples), (
            "every advertised credential family needs a sample shape here (and vice "
            "versa) — a family added to the regex without a row understates the panel"
        )
        for family, sample in samples.items():
            _redacted, warnings = security.redact_credentials(sample)
            assert warnings, f"advertised family {family!r} is not actually redacted"

    def test_every_exfil_heuristic_actually_fires(self):
        """Each advertised heuristic must trigger a verdict from the live scanner."""
        long_q = "x" * (security.exfil_query_min_len() + 10)
        cases = {
            "Credential in path or query": "https://evil.example.com/AKIAIOSFODNN7EXAMPLE",
            "Base64-encoded credential": "https://evil.example.com/p?d="
            + base64.b64encode(b"AKIAIOSFODNN7EXAMPLE and more padding text here").decode(),
            f"Long query string (>= {security.exfil_query_min_len()} chars)": (
                f"https://evil.example.com/p?d={long_q}"
            ),
            "Credential-like query data": "https://evil.example.com/p?d=" + "QUJDRA" * 12,
            "Heavy percent-encoding": "https://evil.example.com/p?d=" + "%41" * 25,
        }
        advertised = [i.label for i in security_posture._exfil_heuristic_items()]
        assert sorted(advertised) == sorted(cases), (
            "every advertised URL-exfil heuristic needs a triggering case here (and "
            "vice versa) — a branch added to _exfil_url_warning without a row "
            "understates the panel"
        )
        for heuristic, url in cases.items():
            warnings = security.scan_exfiltration_urls(url)
            assert warnings, f"advertised heuristic {heuristic!r} did not fire"

    def test_audit_surfaces_are_derived_from_the_sel_vocabulary(self):
        """The audited-surface list must track ``sel._infer_source``'s branches.

        Guards the drift the previous hand-typed 8-tuple had already suffered.
        """
        from kiro_crew import sel as sel_mod

        # Every source _infer_source can return must be in the published tuple...
        probes = {
            "": "unknown",
            "_host": "host",
            "dashboard:1": "dashboard",
            "cron:job": "cron",
            "subagent:x": "subagent",
            "taskrunner": "taskrunner",
            "_bg": "background",
            "_hb": "heartbeat",
            "cli_chat": "cli",
            "discord:1": "discord",
            "telegram:1": "telegram",
            "wecom:1": "wecom",
            "weixin:1": "weixin",
            "whatsapp:1": "whatsapp",
            "feishu:1": "feishu",
            "webex:1": "webex",
            "teams:1": "teams",
            "imessage:1": "imessage",
            "C123:456.789": "slack",
        }
        for key, expected in probes.items():
            assert sel_mod._infer_source(key) == expected, key
        assert set(probes.values()) == set(sel_mod.audit_sources()), (
            "sel.audit_sources() has drifted from _infer_source's branches — a new "
            "surface must be added to both (the posture view derives from it)"
        )
        # ...and every one carries a human gloss, so no row renders bare.
        for source in sel_mod.audit_sources():
            assert source in security_posture._AUDIT_SURFACE_DETAIL, source

    def test_audit_control_does_not_claim_to_be_a_total(self):
        """The audit row is a FLOOR, and must be labelled as one.

        `_infer_source` covers only session-key-derived surfaces; ~70 explicit
        `source=` literals bypass it (`channel`, `token_auth`, `migration`, …), many
        of which are event kinds rather than surfaces and are not cleanly
        enumerable. So this control is deliberately scoped, and its unit + summary
        have to disclose that — a row implying "all audited surfaces" would be the
        same overstatement this module exists to remove.
        """
        control = next(c for c in _CONTROLS if c.key == "audit_surfaces")
        assert "session-key" in control.unit, control.unit
        assert "floor, not a total" in control.summary

        # And the scope claim must stay true: explicit sources really do exist.
        import re
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        explicit: set[str] = set()
        for path in pkg.rglob("*.py"):
            if path.name in {"sel.py", "security_posture.py"}:
                continue
            explicit |= set(
                re.findall(
                    r'source="([a-z_]+)"', path.read_text(encoding="utf-8", errors="replace")
                )
            )
        beyond = explicit - set(_sel_mod.audit_sources())
        assert beyond, (
            "no explicit source= literal outside the inferred vocabulary was found — "
            "if that is now true, this control can be widened to claim a real total"
        )

    def test_token_auth_controls_name_live_mechanisms(self):
        """Each advertised auth control must have its enforcement present."""
        import inspect

        from kiro_crew.dashboard import token_auth

        src = inspect.getsource(token_auth)
        for marker in (
            "hmac.new",  # HMAC-SHA256 signature
            "bind_peer",  # session pinning (peer-keyed: ip:<addr> / ts:… identity)
            "try_consume",  # single-use link nonce
            "MAX_SESSION_TTL_SECS",  # bounded session lifetime
            "revocation_gen",  # revocation generation
            "app_token_path_allowed",  # app-token scoping
        ):
            assert marker in src, f"advertised auth control's mechanism is gone: {marker}"


class TestRedactionSinkRegistry:
    def test_every_named_sink_module_exists(self):
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        for _label, module, _detail in security_posture._REDACTION_SINKS:
            assert (pkg / module).is_file(), module

    def test_every_named_sink_module_actually_redacts(self):
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        for label, module, _detail in security_posture._REDACTION_SINKS:
            text = (pkg / module).read_text(encoding="utf-8")
            # The claim "this is an output path where redaction is applied" must be
            # true of the module named, or the count is fiction.
            assert "redact" in text or "StreamRedactor" in text, label

    def test_a_partially_covered_sink_says_so(self):
        """A sink running only ONE scanner must disclose that on its own row.

        The control summary promises "most run both scanners; the few that run only
        one say so on their own row" — this is what makes that true. Without it, a
        sink like ``task_reporter.py`` (exfil-URL only, no credential scan) would sit
        under a blanket "runs the credential and exfiltration-URL scanners" claim
        and overstate the coverage an operator is reading.
        """
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        # Wrappers that run BOTH scanners internally, so a sink using one is fully
        # covered: StreamRedactor (rolling dual-pass), redact() (the dual-pass
        # helper), redact_and_truncate() (redact-then-slice, so a credential cannot
        # straddle the truncation boundary), redact_via_context() (routes to
        # CredentialPolicy.redact, whose Default delegates to security.redact), and
        # display_safe() (redact_for_display with the exfil+credential redactor,
        # then the mention defang).
        dual_pass = (
            "StreamRedactor",
            "redact(",
            "redact_tree",
            "redact_and_truncate",
            "redact_via_context",
            "display_safe",
            # redact_mcp_error runs redact_exfiltration_urls THEN redact_credentials
            # (mcp_discovery.py) and then scrubs the exact configured header values
            # the generic scanners cannot know about — strictly more than either
            # scanner alone, so a sink using it is fully covered.
            "redact_mcp_error",
        )
        for label, module, detail in security_posture._REDACTION_SINKS:
            text = (pkg / module).read_text(encoding="utf-8")
            full = any(w in text for w in dual_pass) or (
                "redact_credentials" in text and "redact_exfiltration_urls" in text
            )
            if full:
                continue
            assert "only" in detail.lower(), (
                f"{label} ({module}) runs only one redaction pass but its detail text "
                "does not disclose that — either say which scanner it runs, or fix "
                "the sink to run both"
            )

    def test_sink_prose_claims_are_true_of_the_named_module(self):
        """A sink's `detail` makes testable claims — pin them to the call site.

        Closes the drift mode one level down from the counts: the numbers are
        derived, but the 26 curated sentences could still rot into the same
        stale-doc failure. So every mechanism a detail NAMES must actually appear
        in the module it names. Keep new details phrased in terms of a real symbol
        (or non-behavioral facts) so they stay checkable.
        """
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parent
        # claim substring (case-insensitive) -> symbol that must exist in the module
        claims = {
            "streamredactor": "StreamRedactor",
            "redact_and_truncate": "redact_and_truncate",
            "redact_via_context": "redact_via_context",
            "display_safe": "display_safe",
            "exfiltration-url scanning only": "redact_exfiltration_urls",
        }
        for label, module, detail in security_posture._REDACTION_SINKS:
            text = (pkg / module).read_text(encoding="utf-8")
            low = detail.lower()
            for claim, symbol in claims.items():
                if claim in low:
                    assert symbol in text, (
                        f"{label} ({module}) claims {claim!r} but {symbol!r} does not "
                        f"appear in that module — the description has drifted from the code"
                    )
            # "only ... not the credential scanner" must actually be true.
            if "not the credential scanner" in low:
                assert "redact_credentials" not in text, (
                    f"{label} ({module}) says it does NOT run the credential scanner, "
                    "but the module calls redact_credentials — update the description"
                )

    def test_sink_labels_are_unique(self):
        labels = [label for label, _m, _d in security_posture._REDACTION_SINKS]
        assert len(labels) == len(set(labels))


class TestGateSideLogRedactorSpelling:
    """The second axis: WHICH redactor spelling a gate-side log line reads.

    ``TestOmissionDetection`` above forces every redactor call site into a bucket —
    egress sink or not. That decision is orthogonal to this one, and all three
    sites PR #7151 converged prove it: each was correctly listed as non-egress,
    with an accurate reason, while reading the companion-blind baseline pass. The
    bucket was never wrong; the redactor was, and no gate looked at that.

    So this class asks the other question — does a gate-side log line reach its
    text through ``redact_log_via_context`` — and it asks it of a PROPERTY rather
    than of a list of already-decided sites, because a converged-sites list (the
    narrow guard #7151 shipped, in ``test_platform_context``) is a regression
    guard: it protects what has been decided and says nothing about a site born
    tomorrow, which is the direction the class grew in the first place.
    """

    def test_no_new_gate_side_log_line_reads_the_baseline_redactor(self):
        """A gate-side log line born on the baseline fails until someone decides.

        The half the narrow guard cannot cover. Two ways to fail: a module absent
        from the census growing its first site, and a module in it growing another
        one. Either is cleared by calling ``redact_log_via_context`` — or, when the
        site runs in a process that never composes a companion, by raising that
        module's number and recording which process and why, to the standard
        ``redact_log_via_context``'s own docstring sets for ``gatewayd``.
        """
        found = dict(_package_baseline_log_census())
        grew = {
            rel: (count, _BASELINE_LOG_SITE_CENSUS.get(rel, 0))
            for rel, count in found.items()
            if count > _BASELINE_LOG_SITE_CENSUS.get(rel, 0)
        }
        assert not grew, (
            "New gate-side log/audit line(s) reading the BASELINE redactor "
            + ", ".join(
                f"{rel}: {now} sites, census says {was}" for rel, (now, was) in grew.items()
            )
            + ". A gate-side log line goes through `redact_log_via_context` so a host with a "
            "companion loaded is not scanned with the weaker OSS pass. If the baseline is "
            "correct here because this PROCESS never composes a companion, raise the number "
            "in `_BASELINE_LOG_SITE_CENSUS` and say which process and why."
        )

    def test_the_census_holds_no_slack(self):
        """A converted site must lower its number, and an emptied module must go.

        Without this the census only ever ratchets one way: a module that drops
        from 7 sites to 1 would keep 6 free slots for a future baseline line to
        appear in silently.
        """
        found = dict(_package_baseline_log_census())
        slack = {
            rel: (found.get(rel, 0), recorded)
            for rel, recorded in _BASELINE_LOG_SITE_CENSUS.items()
            if found.get(rel, 0) < recorded
        }
        assert not slack, (
            "`_BASELINE_LOG_SITE_CENSUS` is now looser than the code — lower or drop these: "
            + ", ".join(
                f"{rel}: {now} sites, census says {was}" for rel, (now, was) in slack.items()
            )
        )

    def test_the_scanner_flags_the_shape_it_exists_to_catch(self):
        """Teeth, proven on planted source rather than assumed.

        A scan-based ratchet whose matcher silently matches nothing keeps a green
        census forever, so each half is planted here: the pair idiom feeding an
        audit row (``name_grant``'s shape), a lone baseline call nested in a logger
        call (``update_provider``'s shape), and — as the control — the same lines
        written through the context spelling, which must NOT be flagged.
        """
        pair_into_audit = (
            "def log_decline(title):\n"
            "    title, _ = redact_exfiltration_urls(title)\n"
            "    title, _ = redact_credentials(title)\n"
            "    sel().log_tool_invocation(session_key='s', tool_name=title)\n"
        )
        nested_in_logger = (
            "def apply(stderr):\n"
            "    logger.error('install failed: %s', redact(stderr.decode()))\n"
        )
        assert len(_gate_side_baseline_log_sites(pair_into_audit)) == 1
        assert len(_gate_side_baseline_log_sites(nested_in_logger)) == 1

        converged_pair = (
            "def log_decline(title):\n"
            "    title = redact_log_via_context(title)\n"
            "    sel().log_tool_invocation(session_key='s', tool_name=title)\n"
        )
        converged_nested = (
            "def apply(stderr):\n"
            "    logger.error('install failed: %s', redact_log_via_context(stderr.decode()))\n"
        )
        assert _gate_side_baseline_log_sites(converged_pair) == set()
        assert _gate_side_baseline_log_sites(converged_nested) == set()

    def test_the_scanner_does_not_flag_what_is_not_a_log_line(self):
        """Zero false positives on the shapes that are NOT this class.

        Each of these would put an unearned entry in the census and, worse, teach
        the next reader that the gate fires on noise: a baseline call whose result
        never reaches a log write, an egress send (whose spelling question is
        ``redact_via_context`` and a separate decision), a same-named local in a
        SIBLING scope, and a redacted value written to an object ATTRIBUTE whose
        base object is later merely mentioned in a log line.
        """
        not_logged = "def f(x):\n    safe, _ = redact_credentials(x)\n    return safe\n"
        egress = (
            "def f(x):\n"
            "    safe, _ = redact_credentials(x)\n"
            "    await client.chat_postMessage(text=safe)\n"
        )
        sibling_scope = (
            "def a(x):\n"
            "    text, _ = redact_credentials(x)\n"
            "    return text\n"
            "def b(text):\n"
            "    logger.info('raw %s', text)\n"
        )
        attribute_target = (
            "def f(job, err):\n"
            "    job.last_error = redact(err)\n"
            "    logger.warning('job %s paused', job.name)\n"
        )
        reassigned = (
            "def f(x, raw):\n"
            "    text, _ = redact_credentials(x)\n"
            "    text = raw\n"
            "    logger.info('%s', text)\n"
        )
        for label, source in (
            ("not logged", not_logged),
            ("egress send", egress),
            ("sibling scope", sibling_scope),
            ("attribute target", attribute_target),
            ("reassigned from raw", reassigned),
        ):
            assert _gate_side_baseline_log_sites(source) == set(), label

    def test_the_converged_sites_stay_out_of_the_census(self):
        """#7151's three sites must have nothing left for this scan to find.

        Ties the general rule to the narrow guard: ``test_platform_context`` pins
        that each of these still CALLS the helper, and this pins that none of them
        has a gate-side log line reading the baseline alongside it — a drift the
        call-count check on its own would not see.
        """
        for rel in ("platform/update_provider.py", "task_planner.py", "name_grant.py"):
            assert (
                rel not in _BASELINE_LOG_SITE_CENSUS
            ), f"{rel} was converged by #7151 and must not carry a baseline log site"


class TestPostureClaimsMatchEnforcement:
    """The posture must not claim protection the code does not enforce.

    This module's whole value is truthful attestation -- a report that
    OVERSTATES coverage is worse than no report, because consumers treat it as
    ground truth. These pin the three claims that were stronger than the code.
    """

    def test_dispatcher_falls_open_on_an_unregistered_tool(self) -> None:
        """The behaviour the summary must not paper over.

        Registry membership is NOT a proxy for coverage: ``spawn_steer`` and
        friends are absent from ``MCP_CORE_SCHEMAS`` yet validate inside their
        own handler. So this probes the DISPATCHER seam directly -- a registered
        name rejects an unknown field, an unregistered one returns the caller's
        dict untouched, which is what makes the old universal claim false.
        """
        from kiro_crew import mcp_computer, mcp_core
        from kiro_crew.validation import ValidationError

        payload = {"UNKNOWN_FIELD": "z" * 4096}
        passed_through = mcp_core._validate_args("browser", dict(payload))
        assert passed_through == payload, "expected the unregistered tool to fall open"

        with pytest.raises(ValidationError):
            mcp_core._validate_args("learn_add", {"rule": "r", "category": "tool", **payload})

        # Computer-use is the one server that fails closed, which the summary says.
        with pytest.raises(ValidationError):
            mcp_computer._validate_args("definitely_not_a_tool", {})

    def test_tool_schema_summary_does_not_claim_universal_coverage(self, snapshot) -> None:
        control = self._control(snapshot, "tool_schemas")
        summary = " ".join(control["summary"].split())
        # The claim must be scoped to REGISTERED schemas and must name the
        # pass-through, because two of the three servers fall open.
        assert "registered schema" in summary
        assert "unvalidated" in summary
        assert not summary.startswith("Every MCP tool call is checked")

    def test_denied_command_summary_names_shipped_vs_enforced(self, snapshot) -> None:
        from kiro_crew import security

        control = self._control(snapshot, "denied_commands")
        summary = " ".join(control["summary"].split())
        # The count is the catalogue; the enforced set drops disabled rules.
        assert control["count"] == len(security.BUILTIN_DENIED_RULES)
        assert "SHIPPED catalogue" in summary
        assert (
            len(security.compute_effective_denied(security.BUILTIN_DENIED_RULES, (), True, (), ()))
            < control["count"]
        )

    def test_write_protected_detail_is_not_unconditional(self, snapshot) -> None:
        control = self._control(snapshot, "write_protected_paths")
        for item in control["items"]:
            detail = " ".join(item["detail"].split())
            assert "edit kind" in detail, detail
            assert "cannot modify it when" in detail, detail
            # The bash gate does NOT cover these paths, so the detail must not
            # claim it does.
            assert "shell writes are blocked" not in detail, detail

    def test_the_bash_gate_really_does_not_cover_write_protected_paths(self) -> None:
        """The detail says shell writes are uncovered; prove that is still true.

        Every shell-write form against every protected entry is allowed today.
        If the bash gate ever grows real coverage here this fails, and the
        posture wording should then be corrected upwards rather than left stale.
        """
        entries = list(security.write_protected_home_paths())
        assert entries, "expected at least one write-protected path"
        for entry in entries:
            assert security.is_denied(f"echo x > ~/{entry}") is None, entry

    def test_mcp_summary_names_every_fall_open_dispatcher(self, snapshot) -> None:
        """The summary attests over the dispatchers; all three that fall open
        must be named, or the claim re-rots the moment a schemaless tool lands.
        """
        from kiro_crew import mcp_dashboard

        control = self._control(snapshot, "tool_schemas")
        summary = " ".join(control["summary"].split())
        for dispatcher in ("core", "cron", "dashboard"):
            assert dispatcher in summary, (dispatcher, summary)
        assert mcp_dashboard._validate_args("__not_a_tool__", {"a": 1}) == {"a": 1}

    def _control(self, snapshot, key):
        return next(c for c in snapshot["controls"] if c["key"] == key)

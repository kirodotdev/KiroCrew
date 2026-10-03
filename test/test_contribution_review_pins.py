"""Pins for the defects the review found on this branch.

Three separate mechanisms, each of which passed every other test in the suite:

* a catch-up read that answered from the in-memory tail, so a cold subscriber on a
  long log folded the newest events and silently never saw the older ones;
* an app removed through the CLI keeping its contributed projection rows, which the
  Members drawer treats as authority and renders;
* a route registered against a handler that does not exist, which raises at
  dashboard startup and which no test noticed because nothing exercised
  registration.

The third pin is deliberately broader than this feature: it walks EVERY dashboard
route module, because the failure was not specific to this change and the next one
would be found the same way -- by a reviewer, or by a gateway that will not boot.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Catch-up below the retained floor
# ---------------------------------------------------------------------------
class TestCatchUpReachesBelowTheRetainedTail:
    """A fold cannot survive a gap, so the catch-up read must not have one.

    The in-memory tail keeps only the newest ``MAX_RETAINED_EVENTS``. Answering a
    cursor from it returns the newest events and omits every durable one below the
    floor -- and the consumer applies later state over earlier state it never saw,
    which is worse than a slow answer or an error.
    """

    def _log_with_events(self, tmp_path, monkeypatch, *, count: int, cap: int):
        from kiro_crew.eventlog import log as log_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        log = log_mod.MemberLog("someone")
        log.create("Someone")
        for i in range(count):
            log.append("member/message", {"n": i})
        # Force the floor: a fresh reader loads with a smaller cap, which is what
        # sets ``retained_from`` (log.py sets it at load time, not at append time).
        monkeypatch.setattr(log_mod, "MAX_RETAINED_EVENTS", cap)
        fresh = log_mod.MemberLog("someone")
        # And force the LOAD. ``retained_from`` is 0 until the first read, so
        # asserting on it beforehand reads the constructor's value and would make
        # the guard below pass against the very state it exists to reject.
        fresh._ensure_loaded()
        return fresh

    def test_a_cold_cursor_returns_every_durable_event_not_just_the_tail(
        self, tmp_path, monkeypatch
    ):
        fresh = self._log_with_events(tmp_path, monkeypatch, count=6, cap=2)
        assert fresh.retained_from, (
            "the test did not actually create a retained floor, so it would pass "
            "against the defect it exists to catch"
        )

        got = fresh.events_after(-1, 200)

        assert [e["seq"] for e in got] == [1, 2, 3, 4, 5, 6], (
            "the catch-up read answered from the retained tail, so a cold "
            "subscriber folds the newest events and never sees the older ones"
        )

    def test_a_cursor_above_the_floor_is_still_exclusive_and_oldest_first(
        self, tmp_path, monkeypatch
    ):
        fresh = self._log_with_events(tmp_path, monkeypatch, count=6, cap=2)

        got = fresh.events_after(4, 200)

        assert [e["seq"] for e in got] == [5, 6]

    def test_the_limit_still_bounds_the_page_when_reading_from_the_store(
        self, tmp_path, monkeypatch
    ):
        fresh = self._log_with_events(tmp_path, monkeypatch, count=6, cap=2)

        got = fresh.events_after(-1, 3)

        assert [e["seq"] for e in got] == [1, 2, 3], (
            "reading past the floor ignored the limit, so one catch-up request "
            "can stream a whole log into memory"
        )


# ---------------------------------------------------------------------------
# Removing an app removes its cards, whichever path removes it
# ---------------------------------------------------------------------------
class TestEveryRemovalPathRetractsContributions:
    """A contributed row renders because ``store.values`` trusts the file.

    So a removal path that leaves the row behind leaves a card on the user's page
    for an app that is gone, and nothing later cleans it up. The dashboard's own
    disable and uninstall retract; the CLI paths reach the same files.
    """

    def test_the_cli_disable_and_uninstall_both_retract(self):
        from kiro_crew import cli_commands

        src = inspect.getsource(cli_commands)
        tree = ast.parse(src)

        targets = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
                continue
        # Locate the two literal branch guards and require the call inside each.
        for action in ("disable", "uninstall"):
            marker = f'elif action == "{action}":'
            assert marker in src, f"the CLI is missing its {action} branch"
            start = src.index(marker)
            nxt = src.find("\n    elif action ==", start + 1)
            body = src[start : nxt if nxt != -1 else len(src)]
            targets[action] = body

        for action, body in targets.items():
            assert "_retract_app_contributions(" in body, (
                f"the CLI {action} path does not retract the app's contributed "
                f"projection rows, so the Members drawer keeps rendering cards for "
                f"an app that is gone"
            )

    def test_the_retraction_helper_actually_deletes_rows(self):
        from kiro_crew import cli_commands

        src = inspect.getsource(cli_commands._retract_app_contributions)
        assert "teardown_contributions" in src, (
            "the CLI retraction helper does not call the contribution teardown, so "
            "every caller of it is a no-op"
        )

    def test_a_failed_lifecycle_action_does_not_delete_the_rows(self):
        """Deleting the rows is not reversible, so it must follow success.

        A disable or uninstall that FAILS leaves the app installed and running. If
        the retraction already ran, its cards are gone off the user's page with no
        way back except the contributor republishing them. So the call has to sit
        inside the success branch, which this checks structurally: every
        `_retract_app_contributions` call in the CLI must be nested under an `if`
        that tests a lifecycle result.
        """
        import ast
        import textwrap

        from kiro_crew import cli_commands

        tree = ast.parse(textwrap.dedent(inspect.getsource(cli_commands)))

        # Parent map, then climb. The first version of this walked DOWNWARDS
        # threading a flag, reported 1 of 2 guarded, and was wrong -- both calls
        # were guarded in the source. Climbing from each call to its enclosing
        # `if` statements cannot miss one by recursing the wrong way.
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def guarded_by_success(call):
            node = call
            while node in parent:
                up = parent[node]
                if isinstance(up, ast.If) and node in up.body:
                    if "attr='ok'" in ast.dump(up.test):
                        return True
                node = up
            return False

        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_retract_app_contributions"
        ]
        total = len(calls)
        guarded = sum(1 for c in calls if guarded_by_success(c))

        assert (
            total >= 2
        ), f"expected the CLI to retract on both removal paths, found {total} call(s)"
        assert guarded == total, (
            f"{total} retraction call(s) but only {guarded} inside a success "
            "branch: a failed disable or uninstall would delete the app's "
            "contributed rows while the app is still installed"
        )


class TestAnOrphanedRowIsNotRendered:
    """The teardown is scheduled, not awaited, so the read must not trust it.

    It is deferred on purpose: it has to run after the lifecycle lock releases to
    tell a real removal from a same-name reinstall. That means a gateway stopping
    first leaves rows on disk, and the roster read treats a stored row as authority
    -- so a removed app's cards would come back after a restart with nothing later
    clearing them. Guarding the READ closes that for any reason the teardown did
    not run, not only the shutdown race.
    """

    def test_the_roster_read_checks_the_contributor_still_declares(self):
        """Read the HANDLER's source, not the module's.

        Grepping the module passed against the defect: deleting the call left the
        helper's own definition behind, and that definition contains the name. The
        mutation harness caught it. The call has to be inside the function that
        builds the rows.
        """
        import inspect as _inspect

        from kiro_crew.dashboard.handlers import members as members_mod

        src = _inspect.getsource(members_mod.api_members)
        assert "_contributor_may_publish(" in src, (
            "the roster read serves every stored contributed row, so a row whose "
            "app is gone keeps rendering after a restart"
        )

    def test_the_check_is_per_key_not_per_app(self):
        """An app-level check is too coarse to be the guard.

        A manifest narrowed to fewer keys still declares contributions, so asking
        only "does this app contribute" keeps rendering a key outside its current
        declaration. The guard has to ask the question the WRITE path asks.
        """
        import inspect as _inspect

        from kiro_crew.dashboard.handlers import members as members_mod

        src = _inspect.getsource(members_mod._contributor_may_publish)
        assert "may_publish" in src, (
            "the roster guard does not ask per key, so a revoked key's row keeps "
            "rendering while the app still contributes anything at all"
        )

    def test_the_uninstall_retracts_while_it_still_holds_the_lock(self):
        """The scheduled pass cannot be the primary route.

        It runs after the lifecycle lock is released, so a same-name install can
        win that lock first -- and the scheduled pass then sees an installed app,
        skips retraction by design, and the replacement inherits the previous
        app's rows. Awaiting inside the lock is what removes that.
        """
        import inspect as _inspect

        from kiro_crew.apps import routes as routes_mod

        src = _inspect.getsource(routes_mod.handle_uninstall_app)
        assert "await teardown_contributions(" in src, (
            "the uninstall does not await the contribution retraction, so it "
            "relies on a detached pass a same-name reinstall can skip"
        )

    def test_the_guard_allows_a_granted_key_and_refuses_an_unowned_one(self, monkeypatch):
        """The pin the others were all missing.

        Every structural pin here -- per key, deny-safe, called from the handler --
        is satisfied by a guard that returns False for everything, and that is
        exactly what a broken one does: a stray NameError inside it was reported as
        "policy says no" and silently hid every contributed row. So this one
        exercises BOTH answers against a real declaration.
        """
        from kiro_crew.apps.manifest import AppManifest, Contributions
        from kiro_crew.dashboard.handlers import members as members_mod
        from kiro_crew.eventlog import grants

        manifest = AppManifest(
            name="pinapp",
            version="1.0.0",
            displayName="pinapp",
            description="d",
            contributions=Contributions(
                events=["pinapp/*"], projections=["pinapp/count"], units=["member"]
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda n: manifest if n == "pinapp" else None,
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True)
        # A unit kind also needs the operator's approval record, not just the manifest.
        monkeypatch.setattr(
            "kiro_crew.apps.manager.approved_unit_kinds", lambda n: frozenset({"member"})
        )
        grants.invalidate()

        assert members_mod._contributor_may_publish("pinapp", "pinapp/count") is True, (
            "the guard refuses a key the app's own manifest grants, so every "
            "contributed row would be hidden from the roster"
        )
        assert (
            members_mod._contributor_may_publish("pinapp", "pinapp/other") is False
        ), "the guard allows a key the manifest does not grant"
        assert (
            members_mod._contributor_may_publish("ghostapp", "ghostapp/x") is False
        ), "the guard allows a key for an app with no manifest at all"

    def test_the_check_is_deny_safe(self):
        """A lookup that fails must HIDE the row, not show it.

        The two errors are not symmetric: a row wrongly shown is state the drawer
        presents as authority for an app that may not own it, while a row wrongly
        hidden reappears on the next read.
        """
        import inspect as _inspect

        from kiro_crew.dashboard.handlers import members as members_mod

        src = _inspect.getsource(members_mod._contributor_may_publish)
        assert "return False" in src, (
            "the contributor check is not deny-safe: a failed declaration lookup "
            "would render the row"
        )


# ---------------------------------------------------------------------------
# Every registered route names a handler that exists
# ---------------------------------------------------------------------------
class TestEveryRouteRegistrationResolves:
    """A route registered against a missing handler raises at dashboard startup.

    This branch shipped one (`/api/members/{slug}/history`), and the whole suite
    stayed green because nothing calls ``register()``. Reading the attribute name
    out of the AST and resolving it against the handlers package costs milliseconds
    and closes the class, not just the instance.
    """

    ADDERS = {
        "add_get",
        "add_post",
        "add_put",
        "add_patch",
        "add_delete",
        "add_route",
        "add_view",
    }

    def _route_modules(self):
        import kiro_crew.dashboard.routes as routes_pkg

        root = Path(inspect.getfile(routes_pkg)).parent
        return sorted(p for p in root.glob("*.py") if p.name != "__init__.py")

    def test_every_handlers_attribute_named_in_a_route_module_exists(self):
        handlers = importlib.import_module("kiro_crew.dashboard.handlers")
        modules = self._route_modules()
        assert modules, "found no dashboard route modules, so this pin proves nothing"

        missing: list[str] = []
        checked = 0
        for path in modules:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", None) not in self.ADDERS:
                    continue
                for arg in node.args:
                    # only `handlers.<name>`; an inline lambda or a local is not ours
                    if (
                        isinstance(arg, ast.Attribute)
                        and isinstance(arg.value, ast.Name)
                        and arg.value.id == "handlers"
                    ):
                        checked += 1
                        if not hasattr(handlers, arg.attr):
                            missing.append(f"{path.name}: handlers.{arg.attr}")

        assert checked > 50, (
            f"only resolved {checked} route handlers, which is too few to believe -- "
            "the AST shape this walks has probably changed"
        )
        assert not missing, (
            "route(s) registered against a handler that does not exist; "
            "register() raises AttributeError at dashboard startup:\n  " + "\n  ".join(missing)
        )


# ---------------------------------------------------------------------------
# Payload shapes that were accepted and then broke something downstream
# ---------------------------------------------------------------------------
class TestNonStandardJsonIsRefusedAtTheDoor:
    """Both are refused at validation rather than handled later.

    Both become unfixable once past it: a stored ``NaN`` is already non-standard
    JSON on disk, and a body deep enough to raise while DECODING never reaches the
    depth guard, which runs on the parsed value.
    """

    def test_a_non_finite_number_is_refused(self):
        import pytest as _pytest

        from kiro_crew.eventlog import contrib

        for bad in (float("nan"), float("inf"), float("-inf")):
            with _pytest.raises(Exception):
                contrib.check_event_data({"n": bad})

    def test_a_non_finite_projection_value_is_refused(self):
        import pytest as _pytest

        from kiro_crew.eventlog import contrib

        with _pytest.raises(Exception):
            contrib.check_projection_value({"n": float("inf")})

    def test_a_null_projection_value_is_refused(self):
        """GPT eventlog.py:430 -- a stored null is the deletion signal, so reject it.

        ``null`` is JSON-serialisable, so the depth/size checks below it would let
        ``{"value": null}`` persist as an authoritative row -- but every client
        path (the live push and the baseline seed) reads ``value === null`` as a
        DELETION and drops the row, and no read self-corrects that divergence. So
        ``check_projection_value`` must refuse ``None`` outright (an ordinary value
        still passes), reserving ``null`` for the wire-level teardown frames that do
        not go through this write validator.
        """
        import pytest as _pytest

        from kiro_crew.eventlog import contrib
        from kiro_crew.eventlog.contrib import ContribError

        with _pytest.raises(ContribError) as caught:
            contrib.check_projection_value(None)
        assert caught.value.code == "invalid_projection_value"
        # An ordinary value is unaffected -- the guard is specific to None.
        contrib.check_projection_value({"ok": 1})
        contrib.check_projection_value(0)
        contrib.check_projection_value("")

    def test_every_body_parse_guards_the_recursion_error(self):
        import inspect as _inspect

        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        src = _inspect.getsource(handlers_mod)
        parses = src.count("await request.json()")
        guarded = src.count("except (ValueError, RecursionError):")
        assert parses >= 3, f"expected at least 3 body parses, found {parses}"
        assert guarded >= parses, (
            f"{parses} body parse(s) but only {guarded} guarding RecursionError: a "
            "deeply nested body answers 500 rather than a coded refusal"
        )


# ---------------------------------------------------------------------------
# One log, two writers, one ceiling
# ---------------------------------------------------------------------------
class TestAContributorCannotCrowdOutTheGateway:
    """A member's log is written by the contributor AND by the gateway.

    With a single ceiling, an authorized contributor that fills it does not merely
    stop contributing: it stops the gateway recording that member's activity and
    config, permanently, because those appends meet the same limit. The contributor
    is refused earlier by a reserve the gateway keeps.
    """

    def test_the_contributor_ceiling_sits_below_the_absolute_one(self):
        from kiro_crew.eventlog import log as log_mod

        assert log_mod.GATEWAY_RESERVE_BYTES > 0, (
            "no reserve, so a contributor may fill the log to the cap and silence "
            "the gateway's own record of the member"
        )
        assert (
            log_mod.GATEWAY_RESERVE_BYTES < log_mod.MAX_UNIT_LOG_BYTES
        ), "the reserve is the whole ceiling, so no contributor could ever append"

    def test_the_append_applies_the_reserve_only_to_a_contributed_event(self):
        """Both halves matter.

        A reserve applied to every writer would shrink the gateway's own ceiling for
        no reason; a reserve applied to none is the defect. So the check has to be
        conditional on the event being contributed.
        """
        import inspect as _inspect

        from kiro_crew.eventlog import log as log_mod

        # The reserve logic lives in the shared _refuse_if_full helper that BOTH
        # append and append_if call, so the ceiling holds on every write path.
        src = _inspect.getsource(log_mod.MemberLog._refuse_if_full)
        assert "GATEWAY_RESERVE_BYTES" in src, (
            "the append path does not reserve any headroom, so a contributor can "
            "fill the log and silence the gateway"
        )
        assert "is_contributed_event_type(" in src, (
            "the reserve is not conditional on the writer, so it either applies to "
            "the gateway too or to nobody"
        )
        # And both write paths must actually enforce it.
        for path in (log_mod.MemberLog.append, log_mod.MemberLog.append_if):
            assert "_refuse_if_full(" in _inspect.getsource(path), (
                f"{path.__name__} does not enforce the unit-log ceiling, so it "
                "bypasses the bound"
            )


# ---------------------------------------------------------------------------
# Invalidating a grant cache is not enough on its own
# ---------------------------------------------------------------------------
class TestEveryManifestChangeClosesTheSockets:
    """Three separate findings had one shape, so this pins the shape.

    A subscription is authorized ONCE, at subscribe time. Invalidating the cached
    grants gates the next subscribe and says nothing about a socket already
    streaming -- so a path that narrows an app's manifest and only invalidates
    leaves the old authorization live. Disable, update and external re-registration
    each arrived as its own finding; this fails the next one before a reviewer sees
    it.
    """

    def test_every_handler_that_invalidates_grants_also_closes_sockets(self):
        import ast
        import inspect as _inspect
        import textwrap

        from kiro_crew.apps import routes as routes_mod

        tree = ast.parse(textwrap.dedent(_inspect.getsource(routes_mod)))

        offenders = []
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if "invalidate_grants" not in body:
                continue
            checked += 1
            if "_close_event_log_sockets" not in body:
                offenders.append(node.name)

        assert checked >= 1, (
            "found no handler that invalidates the grant cache, so this pin is "
            "watching nothing -- the call was probably renamed"
        )
        assert not offenders, (
            "handler(s) invalidate an app's cached grants without closing its "
            "event-log sockets, so a subscription authorized under the replaced "
            f"manifest keeps streaming: {offenders}"
        )


# ---------------------------------------------------------------------------
# A bound bounds every field it retains
# ---------------------------------------------------------------------------
class TestARetainedKeyIsBoundedToo:
    """The COUNT of keys was capped and each VALUE was capped; the key was not.

    A key is retained as well -- in the row map and in every durable rewrite of it
    -- so a wildcard grant plus the permitted number of very long keys grows both
    without any existing cap noticing, because none of them measures this field.
    """

    def test_an_over_long_key_is_refused(self):
        import pytest as _pytest

        from kiro_crew.eventlog import contrib

        contrib.check_projection_key("demoapp/" + "a" * 32)  # comfortably legitimate
        with _pytest.raises(Exception):
            contrib.check_projection_key("demoapp/" + "a" * (contrib.MAX_PROJECTION_KEY_CHARS + 1))

    def test_both_publish_paths_check_the_key_before_retaining_it(self):
        """Counting against the ownership gates, not just "appears somewhere".

        Each publish path already asks whether the app owns the key; each must ask
        how long it is, or the bound covers one path and not the other.
        """
        import inspect as _inspect

        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        src = _inspect.getsource(handlers_mod)
        gates = src.count("grants.may_publish, app, kind, key")
        checks = src.count("check_projection_key(key)")
        assert gates >= 2, f"expected both publish gates, found {gates}"
        assert checks >= gates, f"{gates} publish path(s) but only {checks} bound the key length"


# ---------------------------------------------------------------------------
# A contributor chooses the identifiers too, not only the payload
# ---------------------------------------------------------------------------
class TestContributorIdentifiersAreRedactedOnEgress:
    """The event TYPE and the projection KEY are attacker-controlled strings.

    Only the `data` passed the outbound chain, so a credential-shaped identifier
    reached the dashboard -- and, on the live frame, every co-subscribed app --
    verbatim while its value beside it was scrubbed.
    """

    @staticmethod
    def _identifier_egress_is_redacted(module, field: str, sibling: str) -> list[bool]:
        """For each payload dict carrying *field* AND *sibling*, is *field* a CALL?

        Structural, not textual. The earlier version asserted one exact spelling --
        ``"type": _redact_projection_value(`` -- so renaming the helper broke all
        three pins while the behaviour they protect got STRONGER. A pin that fails on
        a refactor and would pass on a raw emit is measuring the wrong thing.

        *sibling* is what makes this precise rather than merely broad: requiring the
        dict to carry the payload field too (``key`` beside ``value``, ``type`` beside
        ``data``) selects the contributed payload shapes and ignores unrelated dicts.

        A value that is a literal, or a Name spelled in CAPS, is EXEMPT: those are
        fixed protocol tokens chosen by this repository -- the WS envelope's own
        ``"type": WS_EVENT`` is one -- and redacting them would corrupt the frame while
        protecting nothing. Only a value derived from the app's own event has to pass
        the chain. Demanding redaction everywhere flagged that envelope and would have
        pushed me to "fix" correct code.

        Returns one verdict per matching site, so a caller can require that EVERY
        app-controlled egress is covered rather than just the first one found.
        """
        import ast as _ast
        import inspect as _inspect

        tree = _ast.parse(_inspect.getsource(module))
        verdicts: list[bool] = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Dict):
                continue
            names = {k.value for k in node.keys if isinstance(k, _ast.Constant)}
            if field not in names or sibling not in names:
                continue
            for k, v in zip(node.keys, node.values):
                if not (isinstance(k, _ast.Constant) and k.value == field):
                    continue
                # A fixed protocol token, not an app-chosen identifier.
                if isinstance(v, _ast.Constant):
                    continue
                if isinstance(v, _ast.Name) and v.id.isupper():
                    continue
                call = v
                # Allow a conditional wrapper: the live frame guards on isinstance
                # before redacting, so the value is an IfExp whose body is the call.
                if isinstance(call, _ast.IfExp):
                    call = call.body
                named = ""
                if isinstance(call, _ast.Call):
                    fn = call.func
                    named = getattr(fn, "id", "") or getattr(fn, "attr", "")
                verdicts.append("redact" in named)
        return verdicts

    def test_the_projection_push_redacts_its_key(self):
        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        verdicts = self._identifier_egress_is_redacted(handlers_mod, "key", "value")
        assert verdicts, "no projection payload carrying key+value was found to check"
        assert all(verdicts), (
            "a projection push emits its app-authored key unredacted beside a value "
            f"it scrubs (sites: {verdicts})"
        )

    def test_the_catch_up_read_redacts_the_event_type(self):
        from kiro_crew.dashboard.handlers import eventlog as handlers_mod

        verdicts = self._identifier_egress_is_redacted(handlers_mod, "type", "data")
        assert verdicts, "no event payload carrying type+data was found to check"
        assert all(
            verdicts
        ), f"the catch-up read emits the contributor's event type unredacted ({verdicts})"

    def test_the_live_frame_redacts_the_event_type(self):
        from kiro_crew.dashboard import eventlog_ws as ws_mod

        verdicts = self._identifier_egress_is_redacted(ws_mod, "type", "data")
        assert verdicts, "no live frame carrying type+data was found to check"
        assert all(verdicts), (
            "the live frame fans the contributor's event type to every co-subscriber "
            f"unredacted (sites: {verdicts})"
        )


class TestRedactionDoesNotAliasDistinctKeys(unittest.TestCase):
    """GPT service.py:112 -- redaction must not alias two distinct identities.

    Two DISTINCT source keys can redact to the SAME placeholder (both greedy
    credential/URL spans collapse to one string). Writing both under that one
    redacted key lets the second overwrite the first, so one identity's value
    vanishes from the egress frame or is served under the other's identity. The
    redactor must instead keep every source key as its own entry.
    """

    def test_two_keys_that_redact_alike_do_not_overwrite_each_other(self):
        from kiro_crew.eventlog import service

        a = "ghp_" + "a" * 36
        b = "ghp_" + "b" * 36
        # Precondition: the two distinct keys really do collapse to one string.
        self.assertEqual(
            service._redact_projection_value(a),
            service._redact_projection_value(b),
            "test premise broke: the two keys no longer redact alike",
        )
        out = service._redact_projection_value({a: 1, b: 2})
        # Both values survive -- neither is dropped by a silent overwrite.
        self.assertEqual(len(out), 2, f"a colliding key overwrote another: {out}")
        self.assertEqual(sorted(out.values()), [1, 2])
        # Deterministic + stable: same input, same output.
        self.assertEqual(out, service._redact_projection_value({a: 1, b: 2}))

    def test_a_lone_redacted_key_is_not_suffixed(self):
        from kiro_crew.eventlog import service

        out = service._redact_projection_value({"ghp_" + "a" * 36: 1})
        # No collision -> no disambiguation suffix.
        self.assertEqual(list(out.values()), [1])
        self.assertTrue(all("#" not in k for k in out), out)

    def test_many_colliding_keys_are_disambiguated_deterministically(self):
        """Opus service.py:125 -- disambiguation is O(1) per key, output stable.

        The suffix is chosen with a per-base counter rather than rescanning the
        output for a free ``#N`` on every collision (which is O(N) per key -->
        O(N^2) over the value, run synchronously on the serving loop for every
        push -- an attacker-shaped value could stall the loop past the watchdog).
        The counter must still produce the same deterministic ``#2, #3, ...``
        sequence the rescan did, so a contributor's keys are never dropped.
        """
        from kiro_crew.eventlog import service

        # Four DISTINCT keys that all collapse to the same redacted placeholder.
        keys = ["ghp_" + c * 36 for c in "abcd"]
        placeholder = service._redact_projection_value(keys[0])
        for k in keys:
            self.assertEqual(
                service._redact_projection_value(k),
                placeholder,
                "test premise broke: the keys no longer redact alike",
            )
        out = service._redact_projection_value({k: i for i, k in enumerate(keys)})
        # No value dropped: four source keys -> four output entries.
        self.assertEqual(len(out), 4, f"a colliding key overwrote another: {out}")
        self.assertEqual(sorted(out.values()), [0, 1, 2, 3])
        # Deterministic naming contract: first keeps the bare placeholder, the
        # rest take #2, #3, #4 in source order.
        self.assertEqual(
            list(out.keys()),
            [placeholder, f"{placeholder}#2", f"{placeholder}#3", f"{placeholder}#4"],
        )
        # Stable across calls.
        self.assertEqual(out, service._redact_projection_value({k: i for i, k in enumerate(keys)}))

    def test_a_source_key_equal_to_a_generated_suffix_still_keeps_every_entry(self):
        """The counter's ``in out`` re-check guards a literal-suffix collision.

        A real source key that equals a suffixed form the disambiguation would
        emit (here a literal ``<placeholder>#2`` alongside two keys that redact
        to ``<placeholder>``) must not be overwritten: advancing the same counter
        keeps every source identity its own entry.
        """
        from kiro_crew.eventlog import service

        a = "ghp_" + "a" * 36
        b = "ghp_" + "b" * 36
        placeholder = service._redact_projection_value(a)
        literal_clash = f"{placeholder}#2"
        out = service._redact_projection_value({a: 1, literal_clash: 2, b: 3})
        # All three source identities survive as distinct entries.
        self.assertEqual(len(out), 3, f"a value was dropped by a literal-suffix clash: {out}")
        self.assertEqual(sorted(out.values()), [1, 2, 3])


class TestTheDeletionFrameRedactsItsKeyToo(unittest.TestCase):
    """The deletion broadcast is an egress like any other.

    Three publish-side sites redact the app-chosen key. The deletion frame is a
    fourth, reached on disable, carrying the same app-chosen string to the same
    dashboard sockets. A redaction contract with a hole in one of four paths
    protects nothing: an app that wants its key seen raw waits to be disabled.
    """

    @staticmethod
    def _unit_factory(seen):
        class _Svc:
            @staticmethod
            def broadcast(frame, payload):
                seen.append(payload)

        class _Unit:
            id_field = "member"
            frame = "member_projection"

            @staticmethod
            def service():
                return _Svc()

        return lambda kind: _Unit()

    def test_a_credential_shaped_key_is_redacted_on_the_deletion_frame(self):
        from kiro_crew.apps import teardown as td

        seen: list = []
        secret = "demo/AKIAIOSFODNN7EXAMPLE"
        td._push_projection_deletions([("member", "alice", secret, 0, 1)], self._unit_factory(seen))

        self.assertEqual(len(seen), 1, "the deletion frame must still be pushed")
        pushed = seen[0]["key"]
        self.assertNotEqual(
            pushed, secret, "the deletion frame carried the app-chosen key verbatim"
        )
        self.assertTrue(pushed, "redaction must not empty the key: the client folds on it")

    def test_an_ordinary_key_survives_the_deletion_frame_unchanged(self):
        """The guard must ALLOW the legitimate case.

        Without this, a redactor returning a constant would satisfy the test above
        while breaking every real deletion -- the deny-safe failure that already
        slipped past three of my own guards.
        """
        from kiro_crew.apps import teardown as td

        seen: list = []
        td._push_projection_deletions(
            [("member", "alice", "demo/build-status", 0, 1)], self._unit_factory(seen)
        )
        self.assertEqual(seen[0]["key"], "demo/build-status")


class TestRowsOutliveAFailedDisablePersist(unittest.TestCase):
    """Contributed rows are deleted only once the disabled state is durable.

    The rows cannot be reconstructed. Deleting them before a metadata write that can
    still fail means a 400 leaves the app ENABLED with its data destroyed. Authority
    is retracted first either way, so nothing can write during the gap.
    """

    def test_the_runtime_teardown_defers_the_destructive_half_when_asked(self):
        import ast as _ast
        import inspect

        from kiro_crew.apps import teardown as td

        sig = inspect.signature(td.teardown_app_runtime)
        self.assertIn(
            "defer_projection_deletion",
            sig.parameters,
            "the disable path needs a way to order the deletion after its persist",
        )
        self.assertIs(
            sig.parameters["defer_projection_deletion"].default,
            False,
            "deferral must be opt-in: every other caller keeps deleting as before",
        )

        src = inspect.getsource(td.teardown_app_runtime)
        self.assertIn(
            "await retract_contribution_authority(name)",
            src,
            "authority retraction must still run unconditionally and early",
        )

        # The parameter existing proves nothing; it has to be HONOURED. Every call to
        # the destructive half inside this function must sit under a test that reads
        # the flag. Checked as a tree because a mutation that simply deletes the
        # ``if`` leaves the call present and every substring check still passing --
        # which is exactly how this pin first let that mutation survive.
        tree = _ast.parse(inspect.getsource(td))
        target = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef) and node.name == "teardown_app_runtime":
                target = node
                break
        self.assertIsNotNone(target)

        guarded: list[bool] = []
        for node in _ast.walk(target):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            if (getattr(fn, "id", "") or getattr(fn, "attr", "")) != "delete_contribution_rows":
                continue
            # Is this call inside an `if` whose test mentions the flag?
            enclosing = [
                n
                for n in _ast.walk(target)
                if isinstance(n, _ast.If)
                and "defer_projection_deletion" in _ast.dump(n.test)
                and any(node is sub for sub in _ast.walk(n))
            ]
            guarded.append(bool(enclosing))

        self.assertTrue(guarded, "the runtime teardown must call the destructive half at all")
        self.assertTrue(
            all(guarded),
            "a deletion call is not guarded by defer_projection_deletion, so the flag "
            "is accepted and ignored",
        )

    def test_the_disable_route_asks_for_the_deferral(self):
        """The route must PASS the flag, not merely be able to.

        Separate from the ordering check below because the two fail independently: a
        route that drops the flag still calls the deletion in the right order, so the
        ordering pin passes while the rows are destroyed early inside the teardown.
        """
        import ast as _ast
        import inspect

        from kiro_crew.apps import routes

        tree = _ast.parse(inspect.getsource(routes))
        passed = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            fn = node.func
            if (getattr(fn, "id", "") or getattr(fn, "attr", "")) != "teardown_app_runtime":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            passed.append("defer_projection_deletion" in kwargs)

        self.assertTrue(passed, "the disable route must call the runtime teardown")
        self.assertTrue(
            any(passed),
            "no call asks for the deferral, so the rows are deleted before the "
            "disabled state is durable",
        )

    def test_the_disable_route_deletes_only_after_a_successful_persist(self):
        """Read as a TREE, not as text: the deletion must be preceded by the
        ``result.ok`` failure RETURN, which a substring search cannot establish."""
        import ast
        import inspect

        from kiro_crew.apps import routes

        tree = ast.parse(inspect.getsource(routes))

        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                dumped = ast.dump(node)
                if "delete_contribution_rows" in dumped and "disable_app" in dumped:
                    target = node
                    break
        self.assertIsNotNone(target, "the disable handler must call both")

        def call_line(needle: str) -> int:
            """Line of the call named *needle*, anywhere in the handler.

            By POSITION, not by index into the top-level statement list: both calls
            live inside the same ``try`` block, so an index over ``body`` scores them
            equal and the ordering assertion cannot fail no matter what the code does.
            """
            for sub in ast.walk(target):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    if (getattr(fn, "id", "") or getattr(fn, "attr", "")) == needle:
                        return sub.lineno
            return -1

        i_persist = call_line("disable_app")
        i_delete = call_line("delete_contribution_rows")
        self.assertGreater(i_persist, -1, "the persist call must be in this handler")
        self.assertGreater(i_delete, -1, "the deletion call must be in this handler")
        self.assertLess(
            i_persist,
            i_delete,
            "the rows are deleted before the disabled state is made durable",
        )

        returns_between = [
            sub
            for sub in ast.walk(target)
            if isinstance(sub, ast.Return) and i_persist < sub.lineno < i_delete
        ]
        self.assertTrue(
            returns_between,
            "a failed persist must RETURN before the deletion, not merely precede it",
        )

    def test_authority_is_retracted_before_the_on_disable_hook_runs(self):
        """``onDisable`` must not run while the contribution grant is still live.

        The log is append-only (never rewritten), so an event-writing ``onDisable``
        that runs before retraction persists an event under authority the disable is
        removing -- deterministic on every disable of such an app. Retracting first
        means the hook may still run its shutdown work while being unable to append.
        Pinned on call ORDER in the source, because the two calls are both present
        either way and only their order carries the guarantee.
        """
        import ast as _ast
        import inspect

        from kiro_crew.apps import teardown as td

        tree = _ast.parse(inspect.getsource(td))
        target = None
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef) and node.name == "teardown_app_runtime":
                target = node
                break
        self.assertIsNotNone(target)

        retract_line = None
        on_disable_line = None
        for node in _ast.walk(target):
            if not isinstance(node, _ast.Call):
                continue
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name == "retract_contribution_authority" and retract_line is None:
                retract_line = node.lineno
            if name == "run_lifecycle_script" and on_disable_line is None:
                on_disable_line = node.lineno

        self.assertIsNotNone(retract_line, "retract_contribution_authority call not found")
        self.assertIsNotNone(on_disable_line, "onDisable run_lifecycle_script call not found")
        self.assertLess(
            retract_line,
            on_disable_line,
            f"retract_contribution_authority (line {retract_line}) must precede the "
            f"onDisable run_lifecycle_script (line {on_disable_line}); otherwise a "
            "shutdown hook can append a permanent event under authority being revoked",
        )


class TestIdentifierRedactionIsStringTyped(unittest.TestCase):
    """The identifier helper is typed ``str -> str``.

    The recursive redactor is ``object -> object`` for the nested payload it walks,
    and feeding it straight into a ``str`` TypedDict field fails the type check CI
    runs. A narrow signature is the fix; a cast that silences the checker would leave
    the same unproven assumption in place.
    """

    def test_the_helper_takes_and_returns_a_string(self):
        import typing

        from kiro_crew.eventlog.service import redact_projection_identifier

        hints = typing.get_type_hints(redact_projection_identifier)
        self.assertIs(hints["value"], str)
        self.assertIs(hints["return"], str)

    def test_it_redacts_and_leaves_an_ordinary_identifier_alone(self):
        from kiro_crew.eventlog.service import redact_projection_identifier

        self.assertEqual(redact_projection_identifier("demo/build-status"), "demo/build-status")
        self.assertNotEqual(
            redact_projection_identifier("demo/AKIAIOSFODNN7EXAMPLE"),
            "demo/AKIAIOSFODNN7EXAMPLE",
        )


# ---------------------------------------------------------------------------
# Unit-kind authority: the manifest declares, the operator's record approves
# ---------------------------------------------------------------------------
def _units_source(tmp_path, name: str, *, units: list[str] | None, version: str = "1.0.0") -> Path:
    """An app source whose manifest declares *units*, or no contributions at all.

    ``units=None`` omits the whole ``contributions`` block, which is how a pin sets
    up an app that is approved for nothing: a block declaring ``events`` or
    ``projections`` with an EMPTY ``units`` list is refused at install, so it is not
    a state a real app can be in.
    """
    src = tmp_path / f"source-{version}" / name
    src.mkdir(parents=True)
    manifest: dict = {
        "name": name,
        "version": version,
        "displayName": name,
        "description": "d",
        "author": "t",
    }
    if units is not None:
        manifest["contributions"] = {
            "events": [f"{name}/*"],
            "projections": [f"{name}/count"],
            "units": units,
        }
    (src / "app.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return src


@pytest.fixture()
def units_home(tmp_path, monkeypatch):
    """An isolated KIROCREW_HOME that admits a synthetic third-party app."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    from kiro_crew.eventlog import grants

    grants.invalidate()
    yield home
    grants.invalidate()


class TestAUnitKindNeedsBothADeclarationAndAnApproval:
    """``contributions.units`` names a kind the app owns no namespace under.

    ``events`` and ``projections`` are guarded by their ``<app>/`` prefix, which is
    re-checked on every request -- so a manifest claiming someone else's namespace
    is ignored. A unit KIND has no prefix (``member`` is the gateway's name), so
    that re-check cannot guard it, and a manifest is a file the app's own code can
    rewrite. These pins are the four cases that distinguish a declaration from an
    approval.

    Deliberately end-to-end against the real lifecycle functions and a real
    approvals record: the defect lives in the relationship between two files on
    disk, so a test that stubbed either of them could not see it.
    """

    APP = "pinunits"

    def _install_enabled(self, tmp_path, *, units):
        from kiro_crew.apps.manager import enable_app, install_app
        from kiro_crew.eventlog import grants

        src = _units_source(tmp_path, self.APP, units=units)
        assert install_app(str(src)).ok
        assert enable_app(self.APP).ok
        grants.invalidate()

    def test_a_declared_and_approved_kind_is_granted(self, tmp_path, units_home):
        """The control. Without it, every pin below passes against a broken guard.

        An `approved_unit_kinds` that returned the empty set for everything would
        satisfy all three negative pins, which is exactly what a bug looks like.
        """
        from kiro_crew.eventlog import grants

        self._install_enabled(tmp_path, units=["member"])
        assert grants.may_use_kind(self.APP, "member") is True

    def test_a_manifest_rewritten_after_install_cannot_grant_a_kind(self, tmp_path, units_home):
        from kiro_crew.apps.manager import app_dir
        from kiro_crew.eventlog import grants

        # Installed declaring nothing, so nothing was approved.
        self._install_enabled(tmp_path, units=None)
        assert grants.may_use_kind(self.APP, "member") is False

        # The app now rewrites its OWN manifest in place -- no install, no update,
        # no registration call -- to claim the gateway's own kind.
        live = app_dir(self.APP) / "app.json"
        manifest = json.loads(live.read_text(encoding="utf-8"))
        manifest["contributions"] = {
            "events": [f"{self.APP}/*"],
            "projections": [f"{self.APP}/count"],
            "units": ["member"],
        }
        live.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        grants.invalidate()

        assert grants.may_use_kind(self.APP, "member") is False

    def test_a_self_edit_cannot_add_a_kind_beside_an_approved_one(self, tmp_path, units_home):
        """The narrowest case, and the one that shows why validation cannot do this.

        A manifest written straight to disk never meets ``Contributions.validate``,
        so it can claim a kind that is not even registered -- while the kind it WAS
        approved for keeps working. A self-edit is ignored, not treated as poisoning
        the whole declaration.
        """
        from kiro_crew.apps.manager import app_dir
        from kiro_crew.eventlog import grants

        self._install_enabled(tmp_path, units=["member"])
        live = app_dir(self.APP) / "app.json"
        manifest = json.loads(live.read_text(encoding="utf-8"))
        manifest["contributions"]["units"] = ["member", "board"]
        live.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        grants.invalidate()

        assert grants.may_use_kind(self.APP, "board") is False
        assert grants.may_use_kind(self.APP, "member") is True

    def test_an_update_the_operator_ran_does_grant_the_new_kind(self, tmp_path, units_home):
        from kiro_crew.apps.manager import update_app
        from kiro_crew.eventlog import grants

        self._install_enabled(tmp_path, units=None)
        assert grants.may_use_kind(self.APP, "member") is False

        # An update IS an operator installing this manifest, so it may widen.
        v2 = _units_source(tmp_path, self.APP, units=["member"], version="2.0.0")
        assert update_app(str(v2)).ok
        grants.invalidate()

        assert grants.may_use_kind(self.APP, "member") is True

    def test_an_update_that_fails_does_not_grant_the_new_kind(
        self, tmp_path, units_home, monkeypatch
    ):
        """Same widening update as above, failed partway -- and it grants nothing.

        The pair matters: one test alone cannot tell "the failure was respected"
        from "the update never granted anything anyway".
        """
        from kiro_crew.apps import manager as mgr
        from kiro_crew.eventlog import grants

        self._install_enabled(tmp_path, units=None)
        v2 = _units_source(tmp_path, self.APP, units=["member"], version="2.0.0")

        def _boom(*_a, **_k):
            raise OSError("copy failed")

        monkeypatch.setattr(mgr, "_copy_app_tree", _boom)
        assert mgr.update_app(str(v2)).ok is False
        grants.invalidate()

        # The rollback restored the previous record, so authority did not move.
        assert sorted(mgr.approved_unit_kinds(self.APP)) == []
        assert grants.may_use_kind(self.APP, "member") is False

    def test_a_record_written_before_the_field_existed_approves_no_kind(self, tmp_path, units_home):
        from kiro_crew.apps.manager import (
            _unit_approvals_path,
            get_app,
            get_app_manifest,
            units_pending_approval,
        )
        from kiro_crew.eventlog import grants

        self._install_enabled(tmp_path, units=["member"])
        # Drop the app's entry, which is how an app installed before the approvals
        # record existed reads: absent, and so approving nothing.
        record = _unit_approvals_path()
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data.pop(self.APP, None) == ["member"], "fixture must start approved"
        record.write_text(json.dumps(data, indent=2), encoding="utf-8")
        grants.invalidate()

        # Fails closed: the app keeps running and keeps its other grants, but
        # contributes to no kind.
        assert grants.may_use_kind(self.APP, "member") is False
        # ...and the operator is told which kinds are waiting, rather than the
        # denial being visible only as an app that quietly stopped working.
        assert units_pending_approval(approved=(), manifest=get_app_manifest(self.APP)) == (
            "member",
        )
        assert get_app(self.APP).get("unitsPendingApproval") == ["member"]


class TestASelfRegistrationCanNarrowButNotWidenTheApprovedKinds:
    """``register_external_app`` runs under the app's own token.

    For an app that is already installed it is the app's update path, not the
    operator's -- so re-snapshotting there would let an app grant itself a kind by
    re-registering, the same escalation as rewriting its manifest one call further
    out.
    """

    def test_re_registering_with_a_new_kind_does_not_add_it(self, tmp_path, units_home):
        from kiro_crew.apps import manager as mgr

        src = _units_source(tmp_path, "extunits", units=None)
        assert mgr.install_app(str(src)).ok
        assert sorted(mgr.approved_unit_kinds("extunits")) == []

        assert mgr.register_external_app(
            name="extunits",
            version="2.0.0",
            display_name="extunits",
            manifest_data={
                "name": "extunits",
                "version": "2.0.0",
                "displayName": "extunits",
                "description": "d",
                "author": "t",
                "contributions": {
                    "events": ["extunits/*"],
                    "projections": ["extunits/count"],
                    "units": ["member"],
                },
            },
        ).ok
        # The registration succeeds -- it is a legitimate call -- but it carries no
        # authority it did not already have.
        assert sorted(mgr.approved_unit_kinds("extunits")) == []

    def test_re_registering_without_a_kind_drops_it(self, tmp_path, units_home):
        from kiro_crew.apps import manager as mgr

        src = _units_source(tmp_path, "extunits", units=["member"])
        assert mgr.install_app(str(src)).ok

        assert mgr.register_external_app(
            name="extunits",
            version="2.0.0",
            display_name="extunits",
            manifest_data={
                "name": "extunits",
                "version": "2.0.0",
                "displayName": "extunits",
                "description": "d",
                "author": "t",
            },
        ).ok
        # Narrowing is the app withdrawing its own claim, which is always safe.
        assert sorted(mgr.approved_unit_kinds("extunits")) == []


# ---------------------------------------------------------------------------
# A grant question must not read the manifest on the serving loop
# ---------------------------------------------------------------------------
class TestGrantChecksInTheWebSocketPathNeverBlockTheLoop:
    """On a cold cache a grant question reads app metadata and a manifest.

    The questions are sync because one of their callers is a sync frame filter, so
    the read cannot be made async -- it has to happen off the loop, every time.
    Branching on a warm probe to keep the common path inline does not work: the
    grant generation can move between the probe and the call that trusted it, and
    that call is then the one reading the manifest on the loop. This walks EVERY
    grant question in the module rather than the one line a review cited, so the
    next one added is covered before a reviewer sees it.
    """

    QUESTIONS = {"may_use_kind", "may_append", "may_publish", "declares_contributions"}

    def _module_tree(self):
        from kiro_crew.dashboard import ws

        return ast.parse(Path(inspect.getsourcefile(ws)).read_text(encoding="utf-8"))

    def _grant_refs(self, node):
        """Every ``grants.<question>`` reference inside *node*.

        References, not Calls: an offloaded question is PASSED to the executor
        rather than called, so a Call-only walk finds nothing and reports a broken
        module as a clean one. Matching the reference covers both spellings, which
        is what lets the assertion below demand one of them.
        """
        return [
            sub
            for sub in ast.walk(node)
            if isinstance(sub, ast.Attribute)
            and sub.attr in self.QUESTIONS
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "grants"
        ]

    def test_every_grant_question_is_offloaded(self):
        tree = self._module_tree()
        checked = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            refs = self._grant_refs(fn)
            if not refs:
                continue
            # The reference must BE an argument to `asyncio.to_thread`, matched by
            # identity rather than by name: an inline `grants.may_use_kind(...)`
            # elsewhere in the same function would otherwise be excused by an
            # offloaded call to the same question.
            offloaded = set()
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "to_thread"
                ):
                    for arg in node.args:
                        if isinstance(arg, ast.Attribute) and arg.attr in self.QUESTIONS:
                            offloaded.add(id(arg))
            for ref in refs:
                checked += 1
                assert id(ref) in offloaded, (
                    f"{fn.name}: grants.{ref.attr} at line {ref.lineno} is not handed "
                    "to asyncio.to_thread, so on a cold cache it reads a manifest on "
                    "the serving loop"
                )
        assert checked, "found no grant question in ws.py -- the walk is broken"


# ---------------------------------------------------------------------------
# A log is an egress too
# ---------------------------------------------------------------------------
class TestTheDeletionFailureLogCarriesNoAppChosenValue:
    """The deletion FRAME scrubs the key; the failure log beside it carries none.

    Redacting the key at runtime still routes the app's own string into a log, and
    code scanning flags the path rather than the resulting value -- correctly, since
    a helper it cannot see through is not a barrier. So the line is constant. The
    frame keeps its redaction because a frame has to carry the key to identify the
    row it clears; a log does not.
    """

    #: A credential-shaped key, so a leak of ANY part of it is unmistakable in the
    #: captured records rather than a judgement call.
    SECRET = "ghp_" + "A" * 36
    NAMESPACE = "pinlogapp"

    def test_a_failed_push_logs_no_part_of_the_app_chosen_key(self, caplog):
        from kiro_crew.apps.teardown import _push_projection_deletions

        key = f"{self.NAMESPACE}/{self.SECRET}"

        class _Boom:
            def service(self):
                raise RuntimeError("no service")

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.apps.teardown"):
            _push_projection_deletions([("member", "someone", key, 0, 1)], lambda _kind: _Boom())

        # It must still log SOMETHING, or the pin passes by the line disappearing.
        assert caplog.records, "the failure path did not log at all"
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert self.SECRET not in logged
        # Not even the namespace half, which is the app's chosen name.
        assert self.NAMESPACE not in logged
        # A redaction MARKER would mean an app-chosen value still reached the call
        # and was scrubbed on the way -- the shape this fix removes entirely.
        assert "REDACTED" not in logged


# ---------------------------------------------------------------------------
# A failed disable must not leave the grant tombstone behind
# ---------------------------------------------------------------------------
class TestAFailedDisableRestoresContributionAuthority:
    """The tombstone denies regardless of the enabled flag -- that is the point.

    ``teardown_app_runtime`` sets it BEFORE ``disable_app`` persists, so an append
    in flight cannot land after the rows it would fold into are gone. But the
    persist can fail, and then the app is still enabled while ``_revoked`` -- a
    module global -- keeps denying every contribution until a re-enable, a global
    invalidate, or a restart. The operator sees a running app that can write
    nothing, with no error to explain it.

    Both answers are pinned, because a compensation that always runs would be a
    different defect: a SUCCESSFUL disable must leave the tombstone in place.
    """

    APP = "pintomb"

    def _granted(self, monkeypatch):
        """Make grants answer as if APP were installed, enabled and approved."""
        from kiro_crew.apps.manifest import AppManifest, Contributions
        from kiro_crew.eventlog import grants

        manifest = AppManifest(
            name=self.APP,
            version="1.0.0",
            displayName=self.APP,
            description="d",
            contributions=Contributions(
                events=[f"{self.APP}/*"], projections=[f"{self.APP}/count"], units=["member"]
            ),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.manager.get_app_manifest",
            lambda n: manifest if n == self.APP else None,
        )
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: n == self.APP)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.approved_unit_kinds",
            lambda n: frozenset({"member"}) if n == self.APP else frozenset(),
        )
        grants.invalidate()

    async def _drive_disable(self, monkeypatch, *, persist_ok: bool):
        """Run the disable handler with the teardown stubbed and the persist forced."""
        from kiro_crew.apps import routes as routes_mod
        from kiro_crew.apps.teardown import TeardownResult

        async def _no_teardown(_name, _record, **_kw):
            return TeardownResult(warnings=[], failures=[])

        async def _no_rows(_name):
            return []

        monkeypatch.setattr(routes_mod, "teardown_app_runtime", _no_teardown)
        monkeypatch.setattr("kiro_crew.apps.teardown.delete_contribution_rows", _no_rows)
        monkeypatch.setattr(
            routes_mod, "sel", lambda: SimpleNamespace(log_api_access=lambda **_k: None)
        )
        monkeypatch.setattr(
            routes_mod,
            "get_app",
            lambda _n: {
                "name": self.APP,
                "origin": "registry",
                "resources": "gateway",
                "lifecycle": "gateway",
                "enabled": True,
                "manifest": {},
            },
        )
        monkeypatch.setattr(
            routes_mod,
            "disable_app",
            lambda _n: SimpleNamespace(
                ok=persist_ok,
                error="" if persist_ok else "metadata write failed",
                to_dict=lambda: {"ok": persist_ok},
            ),
        )
        monkeypatch.setattr(routes_mod, "_unregister_notification_channels", lambda *_a: None)

        # handle_disable_app never awaits the request object itself (it only
        # reads match_info / app / user), so a sync MagicMock is correct here --
        # an AsyncMock would make request.get() return a coroutine and fail the
        # owner gate. main's handle_disable_app now consults
        # require_owner_dashboard_request, which reads request.app["state"].owner_id
        # and request["app"]/["user"]: present a local dashboard owner (empty
        # app-auth, a local owner subject) so the disable path runs -- this pin is
        # about the tombstone lifecycle, not owner gating.
        from unittest.mock import MagicMock

        request = MagicMock()
        request.match_info = {"name": self.APP}
        request.app = {"state": SimpleNamespace(owner_id="")}
        _req_items = {"app": "", "user": "local-app"}
        request.__getitem__.side_effect = _req_items.__getitem__
        request.__contains__.side_effect = _req_items.__contains__
        request.get.side_effect = _req_items.get
        return await routes_mod.handle_disable_app(request)

    @pytest.mark.asyncio
    async def test_a_failed_persist_lifts_the_tombstone(self, monkeypatch):
        from kiro_crew.eventlog import grants

        self._granted(monkeypatch)
        grants.revoke(self.APP)
        # The guard: the tombstone really is what denies here, so the assertion
        # below cannot pass just because the app was never granted.
        assert grants.may_use_kind(self.APP, "member") is False

        resp = await self._drive_disable(monkeypatch, persist_ok=False)

        assert resp.status == 400
        assert grants.may_use_kind(self.APP, "member") is True
        grants.invalidate()

    @pytest.mark.asyncio
    async def test_a_successful_disable_leaves_the_tombstone_in_place(self, monkeypatch):
        from kiro_crew.eventlog import grants

        self._granted(monkeypatch)
        grants.revoke(self.APP)
        assert grants.may_use_kind(self.APP, "member") is False

        await self._drive_disable(monkeypatch, persist_ok=True)

        # Still denied: the disable succeeded, so the app SHOULD have no authority.
        assert grants.may_use_kind(self.APP, "member") is False
        grants.invalidate()


# ---------------------------------------------------------------------------
# An update closes the app's sockets before anything that can raise
# ---------------------------------------------------------------------------
class TestAnUpdateClosesSocketsInsideTheLifecycleLock:
    """A subscription is authorized once, so the close IS the enforcement.

    Both update branches must close inside the lifecycle lock and ahead of the
    resource swap. A close standing after the backend stop, the deregister, the
    re-register or the backend start is a close any of those can skip by raising,
    and that leaves a socket streaming a unit the replacement manifest does not
    grant.

    Structural because the property is about POSITION -- which statement precedes
    which, and inside which block -- and no runtime assertion can observe an
    ordering that only matters when an intervening step raises.
    """

    def _update_handler(self):
        from kiro_crew.apps import routes as routes_mod

        tree = ast.parse(Path(inspect.getsourcefile(routes_mod)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_update_app":
                return node
        raise AssertionError("handle_update_app not found -- the walk is broken")

    def _calls_named(self, node, name: str) -> list[int]:
        out = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name) and fn.id == name:
                    out.append(sub.lineno)
                elif isinstance(fn, ast.Attribute) and fn.attr == name:
                    out.append(sub.lineno)
        return sorted(out)

    def test_every_socket_close_runs_inside_the_lifecycle_lock(self):
        fn = self._update_handler()
        closes = self._calls_named(fn, "_close_event_log_sockets")
        assert closes, "no socket close in handle_update_app -- the walk is broken"

        locked_spans = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.AsyncWith):
                continue
            if not any("app_lifecycle_lock" in ast.dump(item.context_expr) for item in node.items):
                continue
            for stmt in node.body:
                locked_spans.append((stmt.lineno, stmt.end_lineno or stmt.lineno))

        for line in closes:
            assert any(
                lo <= line <= hi for lo, hi in locked_spans
            ), f"_close_event_log_sockets at line {line} runs outside app_lifecycle_lock"

    def test_the_registry_branch_closes_before_the_resource_swap(self):
        fn = self._update_handler()
        registry_branch = None
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and "is_registry_source" in ast.dump(node.test):
                registry_branch = node
                break
        assert registry_branch is not None, "registry branch not found -- the walk is broken"

        closes = self._calls_named(registry_branch, "_close_event_log_sockets")
        assert closes, "the registry branch does not close the app's sockets at all"
        # Each of these can raise AFTER the replacement manifest is durable, so the
        # close has to precede every one of them.
        for risky in ("stop_app_backend", "_deregister_app_off_loop", "_register_app_off_loop"):
            later = self._calls_named(registry_branch, risky)
            if not later:
                continue
            assert min(closes) < min(later), (
                f"the socket close (line {min(closes)}) runs after {risky} "
                f"(line {min(later)}), which can raise and skip it"
            )

    def test_the_registry_branch_closes_again_after_the_replacement(self):
        """A SECOND close must run after install_from_registry.

        The first close can itself prompt a reconnect, and an app that re-subscribes
        after it but before ``install_from_registry`` revokes lands under the OLD
        manifest -- fan-out re-checks only ``is_revoked``, unset in that window, so
        the socket would resume at ``unrevoke`` streaming a unit the narrowed
        manifest does not grant. A close AFTER the replacement is durable tears it
        down against the new manifest.
        """
        fn = self._update_handler()
        registry_branch = None
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and "is_registry_source" in ast.dump(node.test):
                registry_branch = node
                break
        assert registry_branch is not None, "registry branch not found -- the walk is broken"

        closes = self._calls_named(registry_branch, "_close_event_log_sockets")
        installs = self._calls_named(registry_branch, "install_from_registry")
        assert installs, "install_from_registry call not found -- the walk is broken"
        assert any(c > min(installs) for c in closes), (
            "the registry branch does not close sockets AGAIN after "
            f"install_from_registry (closes={closes}, install={min(installs)}); an app "
            "that reconnected under the old manifest during the pre-revoke window is "
            "never torn down"
        )

    def test_the_local_branch_closes_before_update_app_narrows(self):
        """A socket on the OLD manifest is torn down before the replacement.

        ``update_app`` lifts its own replacement-window revocation internally once
        the tree is durable, and fan-out re-admits on the tombstone alone -- so a
        socket still registered when that lift lands would receive units the NEW,
        narrowed manifest does not grant. Closing BEFORE ``update_app`` removes that
        socket while the app is still on the old manifest, so any renewed
        subscription is re-authorized against the new one (a close AFTER stays as
        the backstop for a socket that raced in during the window).
        """
        fn = self._update_handler()
        closes = self._calls_named(fn, "_close_event_log_sockets")
        updates = self._calls_named(fn, "update_app")
        assert closes, "no socket close in handle_update_app -- the walk is broken"
        assert updates, "no update_app call found -- the walk is broken"
        assert min(closes) < min(updates), (
            f"the earliest socket close (line {min(closes)}) does not precede the "
            f"update_app call (line {min(updates)}), so a socket on the old manifest "
            "survives the narrow and is re-admitted when update_app lifts its revocation"
        )


# ---------------------------------------------------------------------------
# A file-only disable cannot be undone by the app's own metadata (F1)
# ---------------------------------------------------------------------------
class TestAFileOnlyDisableHoldsAgainstTheAppsOwnMetadata:
    """``is_app_enabled`` reads the app's OWN writable ``installed.json``.

    A file-only CLI disable (the no-AF_UNIX/Windows case) leaves a malicious
    backend running; it can rewrite ``enabled: true`` there, and before this fix
    the next cold grant resolution trusted that and re-granted. The protected
    disabled-latch under ``.vault`` is the authority the app cannot forge: grant
    resolution denies while the name is latched, and only an operator ``enable_app``
    lifts it.
    """

    def test_the_latch_denies_grants_even_when_the_app_claims_enabled(self, tmp_path, monkeypatch):
        from kiro_crew.apps.manifest import AppManifest, Contributions
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        app = "evil-app"
        # The app's own metadata lies: it says enabled, and its manifest declares a
        # real contribution that WOULD resolve a grant. The manifest + approvals are
        # stubbed present so the ONLY thing that can stand between the app and a live
        # grant is the protected latch -- otherwise the test would pass for the wrong
        # reason (an absent manifest resolving empty regardless of the latch).
        monkeypatch.setattr("kiro_crew.apps.manager.is_app_enabled", lambda n: True)
        manifest = AppManifest(
            name=app,
            contributions=Contributions(
                events=[f"{app}/thing"], projections=[f"{app}/card"], units=["crew_member"]
            ),
        )
        monkeypatch.setattr("kiro_crew.apps.manager.get_app_manifest", lambda n: manifest)
        monkeypatch.setattr(
            "kiro_crew.apps.manager.approved_unit_kinds", lambda n: ("crew_member",)
        )

        # Positive control: with no latch, the stubbed manifest DOES resolve a grant.
        grants.set_disabled_latch(app, disabled=False)
        grants.invalidate()
        assert grants._declaration(app) != ((), (), ()), (
            "control broken: a never-latched enabled app with a declaring manifest "
            "should resolve a live grant"
        )

        # Latch it off (what disable_app does) and resolve: no grant, despite
        # is_app_enabled -> True and a declaring manifest.
        grants.set_disabled_latch(app, disabled=True)
        grants.invalidate()
        assert grants.is_disabled_latched(app) is True
        assert grants._declaration(app) == ((), (), ()), (
            "a latched app resolved a live grant -- the writable installed.json "
            "overrode the protected latch"
        )

        # An operator enable lifts the latch; the grant resolves again.
        grants.set_disabled_latch(app, disabled=False)
        grants.invalidate()
        assert grants.is_disabled_latched(app) is False
        assert grants._declaration(app) != (
            (),
            (),
            (),
        ), "lifting the latch did not restore the grant"

    def test_an_unreadable_latch_denies(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        path = grants._disabled_latch_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        assert (
            grants.is_disabled_latched("any-app") is None
        ), "an unreadable latch file must answer None so the caller fails closed"

    def test_disable_app_sets_the_latch_and_enable_app_clears_it(self):
        import inspect as _inspect

        from kiro_crew.apps import manager as manager_mod

        disable_src = _inspect.getsource(manager_mod.disable_app)
        enable_src = _inspect.getsource(manager_mod.enable_app)
        assert (
            "set_disabled_latch(name, disabled=True)" in disable_src
        ), "disable_app must set the protected disabled latch, not only bump the epoch"
        assert (
            "set_disabled_latch(name, disabled=False)" in enable_src
        ), "enable_app (the operator action) must be what clears the latch"

    def test_the_declaration_checks_the_latch_before_is_app_enabled(self):
        import inspect as _inspect

        from kiro_crew.eventlog import grants

        src = _inspect.getsource(grants._declaration)
        latch_at = src.find("is_disabled_latched(app)")
        enabled_at = src.find("is_app_enabled(app)")
        assert latch_at != -1 and enabled_at != -1, "both checks must be present"
        assert latch_at < enabled_at, (
            "the forgeable is_app_enabled must not run before the protected latch " "check decides"
        )


# ---------------------------------------------------------------------------
# A retired app token does not inherit a same-name reinstall's grants (F2)
# ---------------------------------------------------------------------------
class TestARetiredAppTokenIsRefusedAcrossReinstall:
    """App tokens are name-keyed and survive a same-name reinstall.

    Without an installation-generation binding, a token minted for a now-uninstalled
    app still carries a signature-valid ``app`` claim, so the replacement's scope
    and approvals resolve for the retired app. Binding the token to the protected
    installation generation (bumped on uninstall) retires it.
    """

    def _mint(self, app, monkeypatch, gen):
        from kiro_crew.dashboard import token_auth

        monkeypatch.setattr("kiro_crew.eventlog.grants.app_installation_generation", lambda n: gen)
        return token_auth.generate_token("u", app=app)

    def test_a_token_from_the_prior_installation_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import token_auth

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        app = "some-app"
        # Minted while the app's installation generation was 0.
        token = self._mint(app, monkeypatch, 0)
        valid, _uid, _reason, name = token_auth.validate_token_with_app(token)
        assert valid and name == app, "a current-generation token must validate"

        # Uninstall bumps the generation to 1; the OLD token does not match.
        monkeypatch.setattr("kiro_crew.eventlog.grants.app_installation_generation", lambda n: 1)
        valid, _uid, reason, name = token_auth.validate_token_with_app(token)
        assert not valid and name == "", (
            "a token minted for the prior installation was accepted after a " "same-name reinstall"
        )
        assert "previous installation" in reason

    def test_a_legacy_token_without_app_gen_validates_only_at_generation_zero(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import token_auth

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        app = "legacy-app"
        # Simulate a token minted before this binding existed: no app_gen claim.
        monkeypatch.setattr(token_auth, "_mint_app_gen_for_test", None, raising=False)
        import json as _json

        payload = {
            "sub": "u",
            "exp": 9999999999,
            "session_exp": 9999999999,
            "iat": 0,
            "nonce": "deadbeef",
            "gen": token_auth.current_revocation_gen(),
            "app": app,
        }
        token_auth._state.register_nonce("deadbeef", 9999999999)
        enc = token_auth._b64url_encode(_json.dumps(payload, separators=(",", ":")).encode())
        legacy = f"{enc}.{token_auth._sign(_json.dumps(payload, separators=(',', ':')).encode())}"

        # Never-uninstalled app (generation 0): legacy token still validates.
        monkeypatch.setattr("kiro_crew.eventlog.grants.app_installation_generation", lambda n: 0)
        valid, _uid, _reason, name = token_auth.validate_token_with_app(legacy)
        assert valid and name == app, "a legacy token for a never-uninstalled app must still work"

        # Once the app has been uninstalled (generation 1), the legacy token is refused.
        monkeypatch.setattr("kiro_crew.eventlog.grants.app_installation_generation", lambda n: 1)
        valid, _uid, _reason, name = token_auth.validate_token_with_app(legacy)
        assert (
            not valid and name == ""
        ), "a legacy token for an app that has since been uninstalled must be refused"

    def test_an_unreadable_generation_fails_closed(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import token_auth

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        token = self._mint("x-app", monkeypatch, 0)
        monkeypatch.setattr("kiro_crew.eventlog.grants.app_installation_generation", lambda n: None)
        valid, _uid, _reason, name = token_auth.validate_token_with_app(token)
        assert not valid and name == "", "an unreadable installation generation must deny"

    def test_uninstall_bumps_the_installation_generation(self):
        import inspect as _inspect

        from kiro_crew.apps import manager as manager_mod

        src = _inspect.getsource(manager_mod.uninstall_app)
        assert "bump_installation_generation(name)" in src, (
            "uninstall_app must bump the installation generation so a reinstall's "
            "tokens differ from the retired install's"
        )

    def test_the_generation_bump_raises_when_it_cannot_be_persisted(self, tmp_path, monkeypatch):
        """GPT 6.1 F1: token-retirement state must fail closed.

        A disk-full / I/O failure while persisting the installation generation is
        the ONLY thing that invalidates a prior install's still-signature-valid
        tokens, so the helper must RAISE (not log-and-swallow) and let the caller
        refuse the uninstall.
        """
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))

        def _boom(*_a, **_k):
            raise OSError("No space left on device")

        # The atomic write (os.replace) is the durable step; make it fault.
        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            grants.bump_installation_generation("some-app")

    def test_uninstall_refuses_pre_delete_when_retirement_cannot_be_preserved(self):
        """The caller runs the bump BEFORE any destruction and refuses on failure.

        Pinned structurally: the bump and its refuse-with-token_generation_not_retired
        must sit before the data move / rmtree, so a persistence failure aborts with
        nothing destroyed (retryable) rather than completing an uninstall that leaves
        a retired token valid.
        """
        import inspect as _inspect

        from kiro_crew.apps import manager as manager_mod

        src = _inspect.getsource(manager_mod.uninstall_app)
        assert "token_generation_not_retired" in src, (
            "uninstall_app must refuse (not log-and-continue) when the installation "
            "generation cannot be advanced"
        )
        bump_at = src.index("bump_installation_generation(name)")
        refuse_at = src.index("token_generation_not_retired")
        rmtree_at = src.index("shutil.rmtree")
        assert bump_at < rmtree_at and refuse_at < rmtree_at, (
            "the generation retirement and its refusal must run BEFORE the "
            "destructive rmtree so a failure leaves nothing destroyed"
        )


# ---------------------------------------------------------------------------
# c552 round: Opus 5.5 BLOCK (selector loop-stall) + GPT 6.1 F1/F2/F3
# ---------------------------------------------------------------------------
class TestSchemaSelectorIsBoundedBeforeRedacting:
    """Opus 5.5 BLOCK, contrib.py:1379 -- an unbounded raw selector stalls the loop.

    redact_and_truncate is quadratic (0.34s at 12 KB, 5.6s at 48 KB); the 60 MB body
    cap admits a multi-KB selector, so running the scan on the raw string before
    cutting to 120 stalls the serving loop for minutes. Refuse any raw selector over
    the raw cap, exactly as the title is refused.
    """

    def test_a_raw_selector_over_the_cap_is_refused(self):
        from kiro_crew.eventlog.contrib import (
            _MAX_RAW_SCHEMA_FIELD_CHARS,
            ContribError,
            normalize_schema,
        )

        over = "e" * (_MAX_RAW_SCHEMA_FIELD_CHARS + 1)
        with pytest.raises(ContribError) as exc:
            normalize_schema({"kind": "keyvalue", "path": [over]})
        assert "selector" in str(exc.value) and "raw limit" in str(exc.value)

    def test_the_refusal_runs_before_the_quadratic_redact(self):
        """Pinned structurally: the raw-length check must sit before redact_and_truncate.

        If the order ever flips, the scan runs on the oversized raw string first and
        the DoS returns even though the selector is ultimately refused.
        """
        import inspect as _inspect

        from kiro_crew.eventlog import contrib as _contrib

        src = _inspect.getsource(_contrib.normalize_schema)
        # Within the path-list branch, the raw-length guard precedes the redact call.
        guard = src.index("schema path selector is")
        redact_in_path = src.index("selectors.append(redact_and_truncate")
        assert guard < redact_in_path, (
            "the raw selector-length refusal must run BEFORE redact_and_truncate so "
            "the quadratic scan never touches an oversized raw selector"
        )


class TestEventDataThatCannotBeUtf8EncodedIsInvalid:
    """GPT 6.1 F3, contrib.py:1237 -- a lone surrogate is a coded 400, not a 500.

    json.dumps(ensure_ascii=False) emits a lone surrogate it accepted in a str, but
    encoding it to UTF-8 raises UnicodeEncodeError, which escaped as a 500.
    """

    def test_a_lone_surrogate_payload_is_refused_with_a_code(self):
        from kiro_crew.eventlog.contrib import ContribError, check_event_data

        with pytest.raises(ContribError) as exc:
            check_event_data({"v": "\ud800"})
        assert exc.value.code == "invalid_projection_value"
        assert "UTF-8" in str(exc.value)


class TestInstallationGenerationMapSurvivesAReadFault:
    """GPT 6.1 F2, grants.py:468 -- a read fault must not wipe other apps' generations.

    Resetting the map to {} on an unreadable (not absent) file would drop every other
    app's retirement generation and revive their retired tokens across a same-name
    reinstall. The bump fails closed (raises) instead.
    """

    def test_an_unreadable_existing_map_raises_rather_than_clobbering(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        # Seed a real map so the bump has other apps' generations to protect, then
        # make the READ of it fault (not FileNotFoundError).
        grants.bump_installation_generation("other-app")

        real_open = open

        def _faulting_open(path, *a, **k):
            if str(path).endswith("app_install_gen.json") and (not a or "r" in str(a[0])):
                raise OSError("simulated read fault")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", _faulting_open)
        with pytest.raises(OSError):
            grants.bump_installation_generation("victim-app")


class TestDisableWritesTheLatchBeforeTheEpoch:
    """GPT 6.1 F1, manager.py:2894 -- the protected latch is written before the epoch bump.

    The epoch bump invalidates the warm cache and forces a cold re-read; if the latch
    is written after, that re-read lands in the window before the latch exists and
    trusts the app's own writable installed.json. Pinned structurally by call order.
    """

    def test_set_disabled_latch_precedes_bump_disable_epoch(self):
        import inspect as _inspect

        from kiro_crew.apps import manager as manager_mod

        src = _inspect.getsource(manager_mod.disable_app)
        latch_at = src.index("set_disabled_latch(name, disabled=True)")
        epoch_at = src.index("bump_disable_epoch(name)")
        assert latch_at < epoch_at, (
            "disable_app must write the protected disabled latch BEFORE bumping the "
            "grant epoch, or the epoch-triggered cold re-read can cache a grant in "
            "the window before the latch exists"
        )


class TestUninstallClearsTheDisabledLatch:
    """Opus 5.5 FINDING, manager.py:2316 -- disable->uninstall->reinstall must work.

    Without clearing the latch on uninstall, a reinstall under the same name is seen
    as still-disabled by _declaration and silently refuses every append/publish.
    """

    def test_uninstall_clears_the_latch(self):
        import inspect as _inspect

        from kiro_crew.apps import manager as manager_mod

        src = _inspect.getsource(manager_mod.uninstall_app)
        assert "set_disabled_latch(name, disabled=False)" in src, (
            "uninstall_app must clear the protected disabled latch so a same-name "
            "reinstall is not left silently denied"
        )


# ---------------------------------------------------------------------------
# c555 round: GPT 6.1 F1/F2/F3/F4 (security-class)
# ---------------------------------------------------------------------------
class TestDisabledLatchSurvivesAReadFault:
    """GPT 6.1 F1, grants.py:361 -- a read fault must not drop unrelated latches.

    set_disabled_latch read the latch, and on an unreadable/malformed file reset to
    an empty set and rewrote {app} alone -- dropping every OTHER latched app and
    re-granting each still-running disabled app. It must abort the rewrite instead.
    """

    def test_an_unreadable_latch_file_is_not_rewritten_to_drop_others(self, tmp_path, monkeypatch):
        import builtins

        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        # Latch a FIRST app, so the file holds a value worth protecting.
        grants.set_disabled_latch("first-app", disabled=True)
        assert grants.is_disabled_latched("first-app") is True

        latch_path = grants._disabled_latch_path()
        real_open = builtins.open

        def _faulting_open(path, *a, **k):
            if str(path) == str(latch_path) and (not a or "r" in str(a[0])):
                raise OSError("simulated read fault")
            return real_open(path, *a, **k)

        # Swap in the fault for the second write, then restore ONLY open (not the
        # env, which must keep pointing at the same latch path for the read-back).
        builtins.open = _faulting_open
        try:
            # The latch write for a SECOND app cannot read the existing file; it
            # must NOT rewrite {second-app} alone (which would drop first-app). The
            # outer handler logs the raise; the file is left untouched.
            grants.set_disabled_latch("second-app", disabled=True)
        finally:
            builtins.open = real_open
        # first-app's latch survived -- it was not clobbered.
        assert grants.is_disabled_latched("first-app") is True

    def test_a_malformed_latch_record_is_rejected_not_coerced(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        grants.set_disabled_latch("keep-me", disabled=True)
        latch_path = grants._disabled_latch_path()
        # Corrupt the record to a parse-able but wrong shape (a list, not the dict).
        latch_path.write_text("[1, 2, 3]", encoding="utf-8")
        # A write against the malformed record must abort rather than coerce-to-empty.
        grants.set_disabled_latch("new-app", disabled=True)
        # The malformed file was left untouched (not rewritten to drop everything).
        assert latch_path.read_text(encoding="utf-8") == "[1, 2, 3]"


class TestRetirementRecordMalformedIsRejected:
    """GPT 6.1 F2, grants.py:462 -- a parsed-but-malformed retirement record aborts.

    bump_installation_generation coerced a non-dict top level to {} and rewrote it,
    dropping every app's retirement generation. It must raise instead.
    """

    def test_a_non_dict_generation_record_raises(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import grants

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        grants.bump_installation_generation("prior-app")
        gen_path = grants._install_gen_path()
        gen_path.write_text('"not a dict"', encoding="utf-8")
        with pytest.raises((OSError, ValueError)):
            grants.bump_installation_generation("another-app")
        # The malformed record was not rewritten to {}.
        assert gen_path.read_text(encoding="utf-8") == '"not a dict"'


class TestPumpRechecksAuthorizationPerFrame:
    """GPT 6.1 F3, eventlog_ws.py:639 -- queued frames are re-checked before delivery.

    The fence/revocation gates run at ENQUEUE; a disable racing a live stream leaves
    frames on the queue that _pump delivered without rechecking. _pump must re-check
    the same two gates before each send.
    """

    def test_pump_rechecks_the_fence_before_sending(self):
        import inspect as _inspect

        from kiro_crew.dashboard import eventlog_ws

        src = _inspect.getsource(eventlog_ws.EventLogHub._pump)
        assert "is_revoked" in src and "fence_admits" in src, (
            "_pump must re-check revocation + the captured fence before delivering a "
            "queued frame, so a disable racing a live stream stops delivery"
        )
        # The recheck must precede the send.
        recheck_at = src.index("is_revoked")
        send_at = src.index("send_str")
        assert recheck_at < send_at, "_pump must re-check authorization BEFORE send_str"


class TestProjectionValueThatCannotBeUtf8EncodedIsInvalid:
    """GPT 6.1 F4, contrib.py:1326 -- a lone-surrogate value is a coded 400, not a crash.

    check_projection_value encoded the serialized value to UTF-8 with no guard; a
    lone surrogate raises UnicodeEncodeError and crashes the request as a 500.
    """

    def test_a_lone_surrogate_value_is_refused_with_a_code(self):
        from kiro_crew.eventlog.contrib import ContribError, check_projection_value

        with pytest.raises(ContribError) as exc:
            check_projection_value("\ud800")
        assert exc.value.code == "invalid_projection_value"
        assert "UTF-8" in str(exc.value)

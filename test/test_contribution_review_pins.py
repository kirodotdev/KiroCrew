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
            assert marker in src, f"the CLI no longer has an {action} branch"
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

        src = _inspect.getsource(log_mod.MemberLog.append)
        assert "GATEWAY_RESERVE_BYTES" in src, (
            "the append path does not reserve any headroom, so a contributor can "
            "fill the log and silence the gateway"
        )
        assert "is_contributed_event_type(" in src, (
            "the reserve is not conditional on the writer, so it either applies to "
            "the gateway too or to nobody"
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
        from unittest.mock import AsyncMock

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

        request = AsyncMock()
        request.match_info = {"name": self.APP}
        request.app = {}
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

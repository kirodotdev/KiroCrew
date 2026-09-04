"""A malformed tailnet identity policy must DENY, not silently widen.

``dashboard.tailscale.allowed_logins`` is the only restriction on which tailnet
peer may authenticate, and its default is the empty list -- which the loader
turns into ``trust_identity = False``, i.e. no login restriction at all. So any
layer that quietly replaced a malformed value with the default handed the
dashboard to every tailnet peer holding a token, where before only an
allowlisted login was admitted, with no denial and no error surfaced.

Same shape as the publish destination allowlist (#4057, #3615) and the Slack
enterprise allowlist (#3945): an open default means "empty" is indistinguishable
from "the operator configured nothing", so emptiness can never be read as
consent. These tests pin the three layers that have to cooperate --
validation preserving the evidence, the loader recording it, the gate reading it.
"""

from __future__ import annotations

import asyncio
import json
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from multidict import CIMultiDict

from kiro_crew.config import validation
from kiro_crew.config.loader import (
    DEGRADED_TAILSCALE,
    DEGRADED_WHOLE_CONFIG,
    KiroCrewConfig,
    _tailscale_config_from,
    degraded_config_files,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)
from kiro_crew.dashboard import tailnet
from kiro_crew.dashboard.tailnet import (
    TailnetTrust,
    governed_tailnet_trust,
    is_forwarded_tailnet_request,
    login_allowed,
)

#: What an operator who wants exactly one login admitted writes.
NARROWED = {
    "enabled": True,
    "trust_identity": True,
    "allowed_logins": ["alice@example.com"],
    "pin_scope": "node",
}

#: A tailnet peer the operator never allowlisted.
INTRUDER = "bob@example.com"


def _loaded(
    tmp_path: Path, dashboard_value: object, overlay_text: str | None = None
) -> KiroCrewConfig:
    (tmp_path / "config.json").write_text(
        json.dumps({"dashboard": dashboard_value}), encoding="utf-8"
    )
    if overlay_text is not None:
        (tmp_path / "config.local.json").write_text(overlay_text, encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load()


def _trust(cfg: KiroCrewConfig) -> TailnetTrust:
    """The trust value both server startup surfaces build from *cfg*.

    Mirrors those call sites exactly, including which helper supplies the
    allowlist -- a shortcut here that passed ``allowed_logins`` straight through
    would test a trust object production never builds.
    """
    ts = cfg.dashboard.tailscale
    return TailnetTrust(
        trust_identity=ts.trust_identity,
        allowed_logins=tailnet_effective_allowed_logins(cfg.degraded_sections, ts.allowed_logins),
        pin_scope=ts.pin_scope,
        identity_unknown=tailnet_identity_unknown(cfg.degraded_sections),
    )


def _admits(trust: TailnetTrust, login: str) -> bool:
    """Whether *login* reaches the dashboard, as the gates decide it.

    Mirrors the conjunction every gate asks: identity is enforced at all, and
    then the login clears the allowlist. A peer is admitted when enforcement is
    off, because nothing then resolves or checks it.
    """
    return not trust.enforces_identity or login_allowed(login, trust.allowed_logins)


class TestMalformedPolicyDenies:
    """The three depths at which the operator's allowlist can be lost."""

    def test_a_wellformed_policy_admits_only_the_allowlisted_login(self, tmp_path: Path) -> None:
        """The baseline the malformed cases are measured against."""
        trust = _trust(_loaded(tmp_path, {"tailscale": dict(NARROWED)}))
        assert _admits(trust, "alice@example.com") is True
        assert _admits(trust, INTRUDER) is False

    def test_a_nonobject_dashboard_section_denies(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, "yes")
        assert "dashboard" in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_a_nonobject_tailscale_value_denies(self, tmp_path: Path) -> None:
        """The sharpest shape: the enclosing ``dashboard`` section is a valid
        object, so nothing above this notices."""
        cfg = _loaded(tmp_path, {"tailscale": "yes"})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_a_nonlist_allowed_logins_denies(self, tmp_path: Path) -> None:
        """A hand edit that drops the brackets: ``"allowed_logins": "alice@..."``.

        The loader already logged this one, but ONLY when ``trust_identity`` was
        still readable and true -- an edit that mangled both said nothing at all.
        """
        cfg = _loaded(
            tmp_path,
            {"tailscale": {**NARROWED, "allowed_logins": "alice@example.com"}},
        )
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_a_list_of_unusable_entries_denies(self, tmp_path: Path) -> None:
        """The entry-level shape: a LIST whose contents are not logins.

        ``[1]`` is a valid JSON array, so the list check passes; the parse
        filter then drops the entry and the allowlist becomes empty, which is
        the widening. Same shape publish.allowed_destinations already handles.
        """
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "allowed_logins": [1]}})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_a_partly_unusable_list_keeps_the_logins_that_parsed(self, tmp_path: Path) -> None:
        """Degrading must not lock out the admin whose own login was fine.

        Unlike publish -- whose gate denies one whole action -- this gate decides
        per peer, so the parseable entries are kept: access narrows to exactly
        what the operator demonstrably wrote, and everyone else is denied.
        """
        cfg = _loaded(
            tmp_path,
            {"tailscale": {**NARROWED, "allowed_logins": ["alice@example.com", None]}},
        )
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), "alice@example.com") is True
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_an_explicitly_empty_list_is_not_degraded(self, tmp_path: Path) -> None:
        """``allowed_logins: []`` is readable, if mistaken -- the loader's own
        trust_identity rule already refuses it with a dedicated error, so
        recording a degradation here would double-report one config typo."""
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "allowed_logins": []}})
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections


class TestAMalformedEnableFlagStillEnforces:
    """``trust_identity`` is the allowlist's own ON switch.

    That makes it the most permissive default in the section: ``_safe_bool``
    returns the default for anything non-boolean, and the default is ``False``,
    so a quoted boolean reads as "never asked for identity trust" and the valid
    allowlist beside it stops being enforced.

    The response is to enforce the allowlist AS WRITTEN, not to deny everyone --
    the entries came from a readable file, so the operator's own login keeps
    working while every peer they did not name is refused. That is the closest
    honest reading of a config whose intent to enable was garbled but whose list
    of who to admit was not.
    """

    def test_a_quoted_boolean_still_enforces_the_allowlist(self, tmp_path: Path) -> None:
        """The commonest hand-edit slip: ``"trust_identity": "true"``."""
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": "true"}})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_an_integer_still_enforces_the_allowlist(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": 1}})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_the_operators_own_login_is_not_locked_out(self, tmp_path: Path) -> None:
        """The whole reason this degrades to enforce-as-written rather than
        deny-all: the admin has to be able to reach the dashboard to fix it."""
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": "true"}})
        assert _admits(_trust(cfg), "alice@example.com") is True

    def test_an_absent_flag_is_not_degraded(self, tmp_path: Path) -> None:
        """No opt-in written is genuinely unconfigured, even with an allowlist
        sitting beside it -- the allowlist alone must never imply consent."""
        cfg = _loaded(tmp_path, {"tailscale": {"enabled": True, "allowed_logins": ["a@b"]}})
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_an_explicit_false_is_a_decision_not_a_degradation(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": False}})
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_a_narrowing_only_field_is_left_alone(self, tmp_path: Path) -> None:
        """``pin_scope`` is the section's other malformed-value path, and it is
        deliberately NOT degraded: an unrecognised value already falls back to
        ``node``, the narrower scope, so the repair direction is safe. Pinned so
        a future sweep does not widen the registry past the fields that need it.
        """
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "pin_scope": "nonsense"}})
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert cfg.dashboard.tailscale.pin_scope == "node"


class TestAnInertAllowlistIsNotADegradation:
    """A malformed allowlist only matters when identity trust could be ON.

    The allowlist is consulted only under identity trust, so when the operator
    cleanly said ``trust_identity: false`` -- or never said true -- a typo in it
    loses nothing: there is no restriction in force to lose. Recording a
    degradation there would turn a typo in an INERT field into a
    forwarded-tailnet lockout, against a config that read correctly permits
    those peers, and would contradict the explicit-false-admits rule the
    neighbouring tests assert.

    A malformed FLAG is the one case that keeps the allowlist live: intent is
    unknown, so it cannot be assumed off.
    """

    _MALFORMED_LISTS = ("alice@example.com", [123], ["a@b", None])

    def test_explicit_false_plus_a_malformed_allowlist_still_admits(self, tmp_path: Path) -> None:
        for bad in self._MALFORMED_LISTS:
            cfg = _loaded(
                tmp_path,
                {"tailscale": {"enabled": True, "trust_identity": False, "allowed_logins": bad}},
            )
            assert DEGRADED_TAILSCALE not in cfg.degraded_sections, bad
            assert _admits(_trust(cfg), INTRUDER) is True, bad

    def test_an_absent_flag_plus_a_malformed_allowlist_still_admits(self, tmp_path: Path) -> None:
        cfg = _loaded(
            tmp_path, {"tailscale": {"enabled": True, "allowed_logins": "alice@example.com"}}
        )
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_trust_on_plus_a_malformed_allowlist_still_denies(self, tmp_path: Path) -> None:
        """The gate must not have swallowed the real case: with the flag readable
        and true, a malformed allowlist is a LOST restriction and still denies."""
        for bad in self._MALFORMED_LISTS:
            cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "allowed_logins": bad}})
            assert DEGRADED_TAILSCALE in cfg.degraded_sections, bad
            assert _admits(_trust(cfg), INTRUDER) is False, bad

    def test_a_malformed_flag_keeps_the_allowlist_live(self, tmp_path: Path) -> None:
        """Both fields mangled: intent unknown, nothing readable to admit from,
        so every forwarded peer is denied."""
        cfg = _loaded(
            tmp_path,
            {"tailscale": {"enabled": True, "trust_identity": "true", "allowed_logins": [123]}},
        )
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False
        assert _admits(_trust(cfg), "alice@example.com") is False

    def test_the_denial_names_the_value_to_fix(self, tmp_path: Path, caplog) -> None:
        """An operator locked out of their phone needs the file and the key, not
        a bare refusal -- this is the only signal they get."""
        with caplog.at_level("WARNING", logger="kiro_crew.config.loader"):
            _loaded(tmp_path, {"tailscale": "yes"})
        assert any("dashboard.tailscale" in r.getMessage() for r in caplog.records)


class TestUnconfiguredIsUntouched:
    """An operator who never asked for identity trust must see NO change.

    This is the whole reason the fix keys off ``degraded_sections`` rather than
    off the value being empty: absent is genuinely unconfigured and stays
    permitted, exactly as the publish gate treats an absent allowlist. Reading
    emptiness itself as suspicious would deny every default install.
    """

    def test_no_tailscale_policy_at_all_still_admits(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"url": ""})
        assert cfg.degraded_sections == frozenset()
        assert _trust(cfg).enforces_identity is False
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_an_empty_dashboard_section_still_admits(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {})
        assert cfg.degraded_sections == frozenset()
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_an_explicitly_disabled_policy_still_admits(self, tmp_path: Path) -> None:
        """``trust_identity: false`` is a decision, not a degradation."""
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": False}})
        assert cfg.degraded_sections == frozenset()
        assert _admits(_trust(cfg), INTRUDER) is True


class TestValidationPreservesTheEvidence:
    """The loader can only record what validation left in place.

    ``_apply_field_default`` repairs a schema violation by deleting the value so
    the loader falls back to defaults. For a narrowing whose default is OPEN
    that repair IS the widening, and it happens before any detector runs -- so
    the registry entry is load-bearing, not decoration.
    """

    def test_the_tailnet_paths_are_exempt_from_repair(self) -> None:
        assert "dashboard" in validation._FAIL_CLOSED_PATHS
        assert "dashboard.tailscale" in validation._FAIL_CLOSED_PATHS

    def test_repair_leaves_an_exempt_value_in_place(self) -> None:
        data = {"dashboard": {"tailscale": "yes"}}
        assert validation._apply_field_default(data, "dashboard.tailscale") is False
        assert data == {"dashboard": {"tailscale": "yes"}}

    def test_a_repairable_dashboard_field_is_still_repaired(self) -> None:
        """Exact-match only: exempting the section must not exempt its siblings,
        whose defaults are restrictive and whose repair is the safe direction."""
        data = {"dashboard": {"url": 7}}
        assert validation._apply_field_default(data, "dashboard.url") is True
        assert data == {"dashboard": {}}


class TestUnattributablePeerUnderUnknownPolicy:
    """A forwarded peer that could not be attributed must still be denied.

    Under the ORDINARY enabled path an unresolved peer deliberately falls
    through -- fail-closed on identity, fail-open on availability -- because the
    operator's allowlist is KNOWN and availability is the only thing at stake.
    When the policy itself is unreadable there is no restriction left to be
    available for, so falling through would announce a deny and then not do it.

    ``is_forwarded_tailnet_request`` is the discriminator that keeps this from
    being a lockout, so it is tested for both answers.
    """

    _UNKNOWN = TailnetTrust(identity_unknown=True)

    def test_a_forwarded_tailnet_request_is_recognised(self) -> None:
        req = SimpleNamespace(
            remote="127.0.0.1", headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5"})
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is True

    def test_a_plain_local_request_is_not(self) -> None:
        """The operator's own browser, which must keep working so they can go and
        repair config.json."""
        req = SimpleNamespace(remote="127.0.0.1", headers=CIMultiDict({}))
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is False

    def test_a_remote_peers_forwarded_header_is_not_read(self) -> None:
        """An unverifiable claim from a non-loopback peer, so denying on it would
        let anyone reachable trigger the refusal."""
        req = SimpleNamespace(
            remote="203.0.113.7", headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5"})
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is False

    def test_a_default_install_is_never_a_candidate(self) -> None:
        """With no policy at all the predicate short-circuits, so the deny branch
        it guards is unreachable for an ordinary install."""
        req = SimpleNamespace(
            remote="127.0.0.1", headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5"})
        )
        assert is_forwarded_tailnet_request(req, TailnetTrust()) is False

    def test_a_comma_joined_chain_is_still_forwarded(self) -> None:
        """Attribution rejects an ambiguous chain -- it cannot say WHICH peer this
        is. Denial must not: it is still a forwarded tailnet request, and
        answering "not forwarded" let a caller add a second address and skip the
        deny entirely, with a valid token doing the rest.
        """
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5, 100.64.0.6"}),
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is True

    def test_repeated_headers_are_still_forwarded(self) -> None:
        """The same ambiguity in its other wire form."""
        headers = CIMultiDict()
        headers.add("X-Forwarded-For", "100.64.0.5")
        headers.add("X-Forwarded-For", "100.64.0.6")
        req = SimpleNamespace(remote="127.0.0.1", headers=headers)
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is True

    def test_a_mixed_chain_with_one_tailnet_hop_is_forwarded(self) -> None:
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "203.0.113.1, 100.64.0.5"}),
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is True

    def test_a_chain_with_no_tailnet_address_is_left_alone(self) -> None:
        """Some other proxy's business. Denying it would widen this gate past the
        tailnet policy it enforces."""
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "203.0.113.1, 198.51.100.2"}),
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is False

    def test_a_garbage_chain_is_left_alone(self) -> None:
        """Unparseable values must not raise, and must not be read as tailnet."""
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "not-an-ip, , 999.999.999.999"}),
        )
        assert is_forwarded_tailnet_request(req, self._UNKNOWN) is False

    def test_an_ambiguous_chain_is_still_never_attributed(self) -> None:
        """The weakening must stay confined to DENIAL. Attribution still refuses
        an ambiguous chain, so no identity is invented from one and nothing is
        pinned or audited to a peer this design cannot name.
        """
        req = SimpleNamespace(
            remote="127.0.0.1",
            headers=CIMultiDict({"X-Forwarded-For": "100.64.0.5, 100.64.0.6"}),
        )
        assert tailnet._forwarded_peer_candidate(req, self._UNKNOWN) is None


class TestNoSiteRespellsTheConjunction:
    """The property is only worth having if it is the ONLY spelling.

    ``enforces_identity`` was introduced so every site answering "is tailnet
    identity in force" answers the same way. A hand-written
    ``trust_identity and allowed_logins`` somewhere else converges today only by
    coincidence -- an empty allowlist happens to deny -- and silently stops
    converging the moment enforcement grows a third condition. That is the exact
    drift the property claims to prevent, so it is pinned rather than trusted.
    """

    def test_only_the_property_itself_spells_it_out(self) -> None:
        src_root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        offenders = []
        for path in src_root.rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "trust_identity and" not in line:
                    continue
                # The property's own definition is the one legitimate spelling,
                # and the section parser's config-validation rule is a different
                # question (is this combination writable), not this one.
                if path.name in ("tailnet.py", "sections.py"):
                    continue
                offenders.append(f"{path.relative_to(src_root)}:{lineno}")
        assert offenders == [], (
            "these sites re-spell the enforces_identity conjunction: " f"{offenders}"
        )


class TestALostOverlayCannotReadmitAnyone:
    """``config.local.json`` is deep-merged OVER ``config.json``.

    An overlay often exists precisely to NARROW the base -- to take a login back
    out. So an unreadable overlay leaves the base's WIDER list standing, and
    every login the operator removed is admitted again. The parsed list is not
    the effective policy, it is a stale one, so nothing may be enforced from it.
    """

    _BASE = {
        "tailscale": {
            "enabled": True,
            "trust_identity": True,
            "allowed_logins": ["alice@example.com", "bob@example.com"],
        }
    }
    #: The operator taking alice back out.
    _NARROWING_OVERLAY = json.dumps(
        {"dashboard": {"tailscale": {"allowed_logins": ["bob@example.com"]}}}
    )
    #: The same overlay caught mid-write by a torn read.
    _TRUNCATED_OVERLAY = '{"dashboard": {"tail'

    def test_a_readable_overlay_narrows_as_written(self, tmp_path: Path) -> None:
        """The baseline: alice is OUT because the overlay removed her."""
        trust = _trust(_loaded(tmp_path, self._BASE, self._NARROWING_OVERLAY))
        assert _admits(trust, "bob@example.com") is True
        assert _admits(trust, "alice@example.com") is False

    def test_a_truncated_overlay_denies_the_login_it_had_removed(self, tmp_path: Path) -> None:
        """Without this the base's wider list survives and alice walks back in."""
        cfg = _loaded(tmp_path, self._BASE, self._TRUNCATED_OVERLAY)
        assert DEGRADED_WHOLE_CONFIG in cfg.degraded_sections
        assert _admits(_trust(cfg), "alice@example.com") is False

    def test_a_truncated_overlay_denies_everyone_including_the_base(self, tmp_path: Path) -> None:
        """bob was in BOTH files, and is still denied: with a file unread there is
        no way to know he was not the entry the overlay removed."""
        cfg = _loaded(tmp_path, self._BASE, self._TRUNCATED_OVERLAY)
        assert _admits(_trust(cfg), "bob@example.com") is False

    def test_no_overlay_at_all_admits_the_base(self, tmp_path: Path) -> None:
        """An absent overlay is not a lost one -- the base is the whole policy."""
        cfg = _loaded(tmp_path, self._BASE)
        assert cfg.degraded_sections == frozenset()
        assert _admits(_trust(cfg), "alice@example.com") is True

    def test_the_refusal_names_the_overlay_not_the_base(self, tmp_path: Path, caplog) -> None:
        """The operator who has just lost REMOTE dashboard access gets one log
        line, and it has to name the file they actually broke.

        Saying "config.json" when the truncated file was "config.local.json"
        sends them to edit a file that is already fine, on a headless host where
        the only way in is SSH.
        """
        cfg = _loaded(tmp_path, self._BASE, self._TRUNCATED_OVERLAY)
        with unittest.mock.patch(
            "kiro_crew.dashboard.tailnet.is_governance_pinned_off", return_value=False
        ):
            with caplog.at_level("ERROR", logger="kiro_crew.dashboard.tailnet"):
                asyncio.run(
                    governed_tailnet_trust(
                        False,
                        (),
                        "node",
                        identity_unknown=True,
                        unreadable_files=tuple(degraded_config_files(cfg.degraded_sections)),
                    )
                )
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "config.local.json" in logged
        assert "SSH" in logged


class TestEffectiveAllowlistBySeverity:
    """Which degradations invalidate the parsed allowlist, and which do not."""

    _LOGINS = ["alice@example.com"]

    def test_an_unreadable_file_empties_it(self) -> None:
        assert (
            tailnet_effective_allowed_logins(frozenset({DEGRADED_WHOLE_CONFIG}), self._LOGINS) == ()
        )

    def test_a_malformed_field_keeps_what_parsed(self) -> None:
        """Inside a file that WAS read, whatever parsed is literally what the
        operator wrote -- keeping it narrows access instead of locking out the
        administrator whose own login was fine."""
        assert tailnet_effective_allowed_logins(frozenset({DEGRADED_TAILSCALE}), self._LOGINS) == (
            "alice@example.com",
        )

    def test_a_clean_load_keeps_it(self) -> None:
        assert tailnet_effective_allowed_logins(frozenset(), self._LOGINS) == ("alice@example.com",)

    def test_an_unrelated_degraded_section_keeps_it(self) -> None:
        assert tailnet_effective_allowed_logins(frozenset({"memory"}), self._LOGINS) == (
            "alice@example.com",
        )


class TestAnExplicitNullIsNotAnAbsentKey:
    """JSON ``null`` is the operator having written something unusable.

    An absent key means they never mentioned the setting; a key written as
    ``null`` means they mentioned it and the value cannot be read as an
    allowlist, a flag, or a section. Only the first is consent, so presence is
    tested with ``in`` rather than ``is not None`` -- the two states are
    indistinguishable from the value alone.

    Safe to treat this way because Kiro Crew's own ``to_dict()`` writes concrete
    values for every one of these keys (``False`` / ``[]`` / ``"node"``) and
    never ``null``, so no save path can manufacture the degraded state.
    """

    def test_a_null_section_denies(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"tailscale": None})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_a_null_flag_still_enforces_the_allowlist(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "trust_identity": None}})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False
        assert _admits(_trust(cfg), "alice@example.com") is True

    def test_a_null_allowlist_denies(self, tmp_path: Path) -> None:
        cfg = _loaded(tmp_path, {"tailscale": {**NARROWED, "allowed_logins": None}})
        assert DEGRADED_TAILSCALE in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is False

    def test_an_absent_section_is_still_absent(self, tmp_path: Path) -> None:
        """The distinction that makes this safe: no tailscale key at all must NOT
        degrade, or every install without one is denied."""
        cfg = _loaded(tmp_path, {"url": ""})
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is True

    def test_the_direct_value_reading_still_treats_none_as_absent(self) -> None:
        """``_tailscale_config_from(None)`` without ``key_present`` is the absent
        reading, which existing callers rely on -- pinned so the null handling
        cannot leak into it."""
        degraded: set[str] = set()
        _tailscale_config_from(None, degraded)
        assert degraded == set()

    def test_a_null_allowlist_under_explicit_false_still_admits(self, tmp_path: Path) -> None:
        """Null does not override the inert-allowlist rule: with trust cleanly
        off there is still no restriction in force to lose."""
        cfg = _loaded(
            tmp_path,
            {"tailscale": {"enabled": True, "trust_identity": False, "allowed_logins": None}},
        )
        assert DEGRADED_TAILSCALE not in cfg.degraded_sections
        assert _admits(_trust(cfg), INTRUDER) is True


class TestIdentityUnknownHelper:
    """One helper, because both server startup surfaces ask the question."""

    def test_each_losing_shape_reports_unknown(self) -> None:
        for key in (DEGRADED_WHOLE_CONFIG, "dashboard", DEGRADED_TAILSCALE):
            assert tailnet_identity_unknown(frozenset({key})) is True, key

    def test_an_unrelated_degraded_section_does_not(self) -> None:
        """Denying tailnet access over a malformed ``memory`` section would be a
        blast radius nobody asked for."""
        assert tailnet_identity_unknown(frozenset({"publish", "memory"})) is False

    def test_a_clean_load_reports_known(self) -> None:
        assert tailnet_identity_unknown(frozenset()) is False


class TestEnforcesIdentity:
    """The single predicate the four gate sites share."""

    def test_unknown_identity_enforces_even_with_trust_off(self) -> None:
        trust = TailnetTrust(identity_unknown=True)
        assert trust.enforces_identity is True
        assert login_allowed(INTRUDER, trust.allowed_logins) is False

    def test_a_configured_narrowing_enforces(self) -> None:
        assert TailnetTrust(trust_identity=True, allowed_logins=("a@b",)).enforces_identity

    def test_trust_on_with_an_empty_allowlist_does_not_enforce(self) -> None:
        """Unreachable via the loader (it refuses that combination) but pinned
        here so the predicate cannot start inferring trust from an opt-in
        alone -- "any tailnet member" is exactly what must never be inferred."""
        assert TailnetTrust(trust_identity=True).enforces_identity is False

    def test_a_default_install_does_not_enforce(self) -> None:
        assert TailnetTrust().enforces_identity is False


class TestGovernanceCeilingStillWins:
    """An administrator forbidding the tailnet integration outranks fail-closed.

    With the integration pinned off there is no tailnet allowlist left to fail
    closed on, and the ceiling's whole point is that no whois call happens.
    """

    def test_a_pinned_ceiling_clears_unknown_identity(self) -> None:
        with unittest.mock.patch(
            "kiro_crew.dashboard.tailnet.is_governance_pinned_off", return_value=True
        ):
            trust = asyncio.run(governed_tailnet_trust(False, (), "node", identity_unknown=True))
        assert trust.identity_unknown is False
        assert trust.enforces_identity is False

    def test_without_a_ceiling_unknown_identity_survives(self) -> None:
        with unittest.mock.patch(
            "kiro_crew.dashboard.tailnet.is_governance_pinned_off", return_value=False
        ):
            trust = asyncio.run(governed_tailnet_trust(False, (), "node", identity_unknown=True))
        assert trust.enforces_identity is True

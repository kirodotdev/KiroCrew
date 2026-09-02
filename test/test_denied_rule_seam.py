"""The ``denied_rules`` seam: edition-contributed, USER-DISABLEABLE deny rules.

Pins the contract of ``DeniedRuleProvider.denied_rules()`` as consumed by
``security.edition_denied_rules`` → ``hooks.resolve_effective_denied_regexes``
and by ``handlers/security.build_denied_commands_snapshot``: the public default
contributes nothing; a contributed rule is enforced by default AND can be
switched off by id or by ``disable_all`` (the property the ``SecurityOverlay``
floor deliberately does not have); an id colliding with a built-in is skipped so
one rule's toggle can never move another's; and the read is fail-soft — a raising
adapter degrades to the built-in catalog instead of wedging the deny gate.
"""

from __future__ import annotations

import dataclasses

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import security
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import HooksConfig, resolve_effective_denied_regexes
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.defaults import DefaultDeniedRuleProvider
from kiro_crew.security import DeniedCommandRule

_EDITION_ID = "acme-ada-credentials-mutate"
_EDITION_PATTERN = r"ada\s+credentials\s+(update|add|delete)"


def _rule(
    rid: str = _EDITION_ID,
    pattern: str = _EDITION_PATTERN,
    category: str = "acme-credential",
    description: str = "Vending AWS credentials at runtime.",
) -> DeniedCommandRule:
    return DeniedCommandRule(id=rid, pattern=pattern, category=category, description=description)


class _Source:
    """Minimal structural match for the ``DeniedRuleProvider`` protocol."""

    def __init__(self, rules):
        self._rules = list(rules)

    def denied_rules(self):
        return list(self._rules)


class _Boom:
    def denied_rules(self):
        raise RuntimeError("edition adapter exploded")


def _with_context(monkeypatch, **overrides):
    base = build_default_context(KiroCrewConfig())
    monkeypatch.setattr(ctx_mod, "current_context", lambda: dataclasses.replace(base, **overrides))


def _config(**denied_commands) -> HooksConfig:
    return HooksConfig.from_dict({"denied_commands": denied_commands})


# ── the default is a no-op ────────────────────────────────────────────────────


def test_default_contributes_nothing() -> None:
    assert DefaultDeniedRuleProvider().denied_rules() == []


def test_default_context_yields_no_edition_rules(monkeypatch) -> None:
    _with_context(monkeypatch)
    assert security.edition_denied_rules() == []


def test_default_context_leaves_the_effective_set_at_the_catalog(monkeypatch) -> None:
    _with_context(monkeypatch)
    assert resolve_effective_denied_regexes(_config()) == security.compute_effective_denied(
        security.BUILTIN_DENIED_RULES, (), False, (), ()
    )


# ── validation ────────────────────────────────────────────────────────────────


def test_contributed_rule_is_accepted(monkeypatch) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    got = security.edition_denied_rules()
    assert [r.id for r in got] == [_EDITION_ID]
    assert got[0].pattern == _EDITION_PATTERN


def test_id_colliding_with_a_builtin_is_skipped(monkeypatch) -> None:
    """``disabled_ids`` is one flat set, so a collision must not be admitted."""
    builtin_id = security.BUILTIN_DENIED_RULES[0].id
    _with_context(monkeypatch, denied_rules=_Source([_rule(rid=builtin_id)]))
    assert security.edition_denied_rules() == []


def test_duplicate_edition_id_keeps_the_first(monkeypatch) -> None:
    first = _rule(pattern="first")
    second = _rule(pattern="second")
    _with_context(monkeypatch, denied_rules=_Source([first, second]))
    got = security.edition_denied_rules()
    assert [r.pattern for r in got] == ["first"]


def test_a_pattern_the_matcher_would_disable_is_not_published(monkeypatch) -> None:
    """A pattern ``_DeniedMatcher`` refuses to run must not be published as a rule.

    The matcher DISABLES a malformed or ReDoS-prone regex and only logs, so a
    published row would read enabled in Settings → Security and toggle cleanly
    while matching nothing — a control that looks present and is not, which is the
    failure this seam exists to remove. Regression for the GPT 5.6 review finding
    on #7705; the earlier code published it."""
    # A TOP-LEVEL alternation, not a catastrophic-backtracking literal. Both are
    # rejected by `is_safe_user_regex` and both reach this publication path
    # identically, so this fixture proves the same property — while keeping a live
    # ReDoS regex out of the source, where CodeQL's py/redos rightly flags one
    # (the catastrophic shapes stay pinned by `_CATASTROPHIC` in
    # test_denied_commands_security.py, which is where the ReDoS budget lives).
    unrunnable = "a|b"
    assert not security.is_safe_user_regex(unrunnable), "fixture is no longer unsafe"

    _with_context(
        monkeypatch,
        denied_rules=_Source(
            [
                _rule(rid="edition-unsafe", pattern=unrunnable),
                _rule(rid="edition-ok", pattern=r"ada\s+credentials"),
            ]
        ),
    )
    got = {r.id for r in security.edition_denied_rules()}
    assert "edition-unsafe" not in got, "an unrunnable pattern was published as enabled"
    # One bad rule does not drop the batch — the safe sibling still lands.
    assert "edition-ok" in got


def test_a_malformed_pattern_is_not_published(monkeypatch) -> None:
    """Same property for an unparseable pattern: skipped, not published broken."""
    _with_context(monkeypatch, denied_rules=_Source([_rule(pattern="(unclosed")]))
    assert security.edition_denied_rules() == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_id_is_skipped(monkeypatch, bad: str) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule(rid=bad)]))
    assert security.edition_denied_rules() == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_pattern_is_skipped(monkeypatch, bad: str) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule(pattern=bad)]))
    assert security.edition_denied_rules() == []


def test_raising_provider_fails_soft_to_the_catalog(monkeypatch) -> None:
    _with_context(monkeypatch, denied_rules=_Boom())
    assert security.edition_denied_rules() == []
    assert resolve_effective_denied_regexes(_config()) == security.compute_effective_denied(
        security.BUILTIN_DENIED_RULES, (), False, (), ()
    )


def test_provider_not_implementing_the_protocol_fails_soft(monkeypatch) -> None:
    """A composed object that is not a DeniedRuleProvider must not raise.

    The field itself is always present (``PlatformContext`` is a frozen dataclass
    and the slot is required), so the read is a direct attribute access — a
    ``getattr`` by string would hide the wiring from the seam-coverage scanner in
    ``test_platform_cpp_seam_coverage.py`` and the field would read as inert.
    """
    _with_context(monkeypatch, denied_rules=object())
    assert security.edition_denied_rules() == []


def test_a_composition_error_still_fails_closed(monkeypatch) -> None:
    """``PlatformCompositionError`` must PROPAGATE, not be swallowed fail-soft.

    The broad ``except Exception`` above exists so a broken provider costs only an
    additive rule.  A composition error is a different animal: it means the
    platform seam itself is misconfigured, and every other reader in this module
    treats that as fail-closed.  Swallowing it here would make a misconfigured
    host look like a host that simply contributes no rules.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    class _Broken:
        def denied_rules(self):
            raise PlatformCompositionError("seam is misconfigured")

    _with_context(monkeypatch, denied_rules=_Broken())
    with pytest.raises(PlatformCompositionError):
        security.edition_denied_rules()


# ── enforcement: ON by default, and genuinely disableable ─────────────────────


def test_contributed_rule_is_enforced_by_default(monkeypatch) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    assert _EDITION_PATTERN in resolve_effective_denied_regexes(_config())


def test_contributed_rule_honours_disabled_ids(monkeypatch) -> None:
    """The whole point of the seam: an operator can switch this rule off."""
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    effective = resolve_effective_denied_regexes(_config(disabled_ids=[_EDITION_ID]))
    assert _EDITION_PATTERN not in effective
    # Disabling the edition rule must not disturb the built-in catalog.
    assert security.BUILTIN_DENIED_RULES[0].pattern in effective


def test_contributed_rule_honours_disable_all(monkeypatch) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    assert resolve_effective_denied_regexes(_config(disable_all=True)) == []


def test_disabling_a_builtin_does_not_disturb_the_edition_rule(monkeypatch) -> None:
    target = security.BUILTIN_DENIED_RULES[0]
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    effective = resolve_effective_denied_regexes(_config(disabled_ids=[target.id]))
    assert target.pattern not in effective
    assert _EDITION_PATTERN in effective


def test_contributed_rule_actually_denies_a_matching_command(monkeypatch) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    effective = resolve_effective_denied_regexes(_config())
    assert security.is_denied("ada credentials update --account 1234", denied_regexes=effective)
    # Scoped to the mutating verbs: the read-only form stays runnable, which is
    # what a glob-tier ``*ada credentials*`` overlay pattern cannot express.
    assert security.is_denied("ada credentials print", denied_regexes=effective) is None


# ── Settings snapshot + toggle ────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def test_snapshot_lists_the_edition_rule_as_toggleable(monkeypatch, home) -> None:
    from kiro_crew.dashboard.handlers.security import build_denied_commands_snapshot

    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    snap = build_denied_commands_snapshot()
    rows = [r for r in snap["builtins"] if r["id"] == _EDITION_ID]
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "edition"
    assert row["enabled"] is True
    # Never locked: a governance pin resolves a pattern to a rule id against the
    # static catalog only, so it cannot name a rule from this seam.
    assert row["pinned"] is False
    assert row["lock_reason"] is None
    assert all(r.get("source") == "builtin" for r in snap["builtins"] if r["id"] != _EDITION_ID)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.security import api_denied_command_builtin_toggle

    app = web.Application()
    app.router.add_patch(
        "/api/security/denied-commands/builtins/{id}", api_denied_command_builtin_toggle
    )
    return app


@pytest.mark.asyncio
async def test_toggle_accepts_an_edition_rule_id(monkeypatch, home) -> None:
    """A rule the panel renders must be toggleable there, not 404."""
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            f"/api/security/denied-commands/builtins/{_EDITION_ID}", json={"enabled": False}
        )
        assert resp.status == 200
        body = await resp.json()
    row = next(r for r in body["builtins"] if r["id"] == _EDITION_ID)
    assert row["enabled"] is False


@pytest.mark.asyncio
async def test_toggle_still_rejects_an_unknown_id(monkeypatch, home) -> None:
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            "/api/security/denied-commands/builtins/not-a-rule", json={"enabled": False}
        )
        assert resp.status == 404


# ── a contributed rule is matched over the FULL input, not a capped prefix ─────


def test_a_contributed_rule_denies_a_command_padded_past_the_scan_cap(monkeypatch) -> None:
    """The bypass this guard exists for: pad the command and the rule stops firing.

    Contributed patterns are not in ``_RULE_ID_BY_PATTERN``, and the matcher used
    to route every non-built-in to the length-bounded window. So an edition rule
    was enforced only over the first ``_DENY_FALLBACK_SCAN_MAX_CHARS`` characters
    and a benign pad in front of the needle walked straight through a rule the
    Settings panel showed as enabled. Anything that re-bounds a single-fragment
    contributed pattern fails here.
    """
    _with_context(monkeypatch, denied_rules=_Source([_rule()]))
    regexes = resolve_effective_denied_regexes(_config())
    pad = "x" * (security._DENY_FALLBACK_SCAN_MAX_CHARS + 100)
    needle = "ada credentials update"

    assert security.is_denied(needle, denied_regexes=regexes) is not None
    # ONE shell segment: the gate evaluates segments separately, so a pad behind a
    # ``;`` proves nothing — the cap only bites when the needle sits past it inside
    # the SAME segment, which is the ``env PAD=<...> <cmd>`` shape.
    assert security.is_denied(f"env PAD={pad} {needle}", denied_regexes=regexes) is not None


def test_a_contributed_pattern_that_would_be_capped_is_not_published(monkeypatch) -> None:
    """A rule the matcher would truncate is skipped rather than advertised.

    Same principle as the ``is_safe_user_regex`` screen: publishing it would put a
    row in Settings that reads enabled and toggles cleanly while being bypassable
    by padding. A top-level ``.*`` is what forces the bounded engine, so the
    edition rewrites the gap as a bounded class (``[^;&|\\n]*``) — which keeps the
    pattern one fragment — or uses the un-weakenable overlay for the loose form.
    """
    capped = _rule(rid="acme-capped", pattern=r"ada.*credentials")
    assert security._matches_full_input(capped.pattern) is False

    _with_context(monkeypatch, denied_rules=_Source([capped, _rule()]))
    published = {r.id for r in security.edition_denied_rules()}
    assert "acme-capped" not in published
    assert _EDITION_ID in published


def test_deny_matcher_full_input_agreement() -> None:
    """``_matches_full_input`` must answer what the matcher actually does.

    The predicate is consulted at publication time while the routing decision is
    made later inside ``_DenyMatcher.__init__`` from the fragments it already has.
    Two places deciding the same thing is how a control comes to claim a guarantee
    the engine does not give, so pin them to the same answer for a non-built-in.
    """
    for pattern in (
        r"ada\s+credentials\s+(update|add|delete)",
        r"isengardcli\b",
        r"[^;&#>|\n]*\bcredentials\b",
        r"ada.*credentials",
        r"needle \S+ .* tail",
        r"(ab|a).*b",
        r"a|b",
    ):
        matcher = security._DenyMatcher(pattern)
        if matcher._disabled:
            continue
        assert security._matches_full_input(pattern) is (matcher._bounded is False), (
            f"{pattern!r}: predicate says full_input="
            f"{security._matches_full_input(pattern)} but the matcher routed "
            f"bounded={matcher._bounded}"
        )

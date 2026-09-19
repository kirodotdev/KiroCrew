"""Tests for the best-effort per-member event-log hook helpers.

These must not depend on the real event-log service: its bodies may still
raise NotImplementedError while it is being filled in concurrently, which is
exactly the case emit() has to swallow.
"""

from __future__ import annotations

import kiro_crew.eventlog.service as svc_mod
from kiro_crew import eventlog_hooks
from kiro_crew.members import member_slot_key, slug_for_name


def test_member_slug_for_slot_bare_key():
    slug = slug_for_name("Review Agent")
    assert eventlog_hooks.member_slug_for_slot(member_slot_key(slug)) == slug


def test_member_slug_for_slot_prefixed_keys():
    slug = slug_for_name("Review Agent")
    base = member_slot_key(slug)
    assert eventlog_hooks.member_slug_for_slot("dashboard_" + base) == slug
    assert eventlog_hooks.member_slug_for_slot("dashboard:" + base) == slug


def test_member_slug_for_slot_rejects_non_member():
    assert eventlog_hooks.member_slug_for_slot("chat-42-1700000000") is None
    assert eventlog_hooks.member_slug_for_slot("cron-abc") is None
    assert eventlog_hooks.member_slug_for_slot("") is None
    assert eventlog_hooks.member_slug_for_slot(None) is None
    assert eventlog_hooks.member_slug_for_slot(1234) is None


def test_emit_swallows_a_raising_service(monkeypatch):
    class _Raiser:
        def ensure(self, slug, name):
            raise NotImplementedError("service not filled in yet")

        def append(self, slug, type, data):
            raise NotImplementedError

    monkeypatch.setattr(svc_mod, "get_service", lambda: _Raiser())
    # Must not raise despite ensure() blowing up.
    eventlog_hooks.emit("some-slug", "Some Name", "member/message", {"ts": 1.0, "preview": "hi"})


def test_emit_calls_ensure_then_append(monkeypatch):
    calls: list[tuple] = []

    class _Recorder:
        def ensure(self, slug, name):
            calls.append(("ensure", slug, name))

        def append(self, slug, type, data):
            calls.append(("append", slug, type, data))

    monkeypatch.setattr(svc_mod, "get_service", lambda: _Recorder())
    eventlog_hooks.emit("slug-a", "Name A", "member/message", {"ts": 2.0, "preview": "x"})
    assert calls[0] == ("ensure", "slug-a", "Name A")
    assert calls[1][0] == "append" and calls[1][1] == "slug-a"
    assert calls[1][2] == "member/message"


def test_emit_no_slug_is_noop(monkeypatch):
    def _boom():
        raise AssertionError("get_service must not be called for an empty slug")

    monkeypatch.setattr(svc_mod, "get_service", _boom)
    eventlog_hooks.emit("", "Name", "member/message", {})
    eventlog_hooks.emit(None, "Name", "member/message", {})


# ---------------------------------------------------------------------------
# member_name_for_slug: turning a slot's slug back into the member's exact NAME
# ---------------------------------------------------------------------------
class _Cfg:
    """Minimal stand-in for the config object these helpers read."""

    def __init__(self, agents):
        self.agents = agents


def test_member_name_for_slug_returns_the_exact_configured_name():
    """The event log stores the NAME, so a slug has to round-trip back to it.

    Storing the slug instead would be lossy: the slug is a lowercased, punctuation
    folded form, and the log is what the member timeline renders.
    """
    name = "Review-Agent"
    cfg = _Cfg({name: object(), "Other-Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(name)) == name


def test_member_name_for_slug_declines_a_name_outside_the_agent_grammar():
    """A roster name with a space resolves to None, and that is the contract.

    The primary resolver skips any name failing the agent-name grammar (which
    allows only alphanumerics, hyphens and underscores), so such a row is not
    addressable here -- it cannot have been created through the validated CRUD
    surface, and a hand-edited config row must not become addressable by writing
    it. Worth pinning because the slug itself round-trips fine, so the None looks
    surprising until you know it is the grammar talking.
    """
    spaced = "Review Agent"
    cfg = _Cfg({spaced: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(spaced)) is None


def test_member_name_for_slug_is_none_without_a_slug():
    """Called on every slot, most of which are not members, so this is the hot path."""
    cfg = _Cfg({"Review Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, "") is None
    assert eventlog_hooks.member_name_for_slug(cfg, None) is None


def test_member_name_for_slug_is_none_when_no_member_matches():
    """An unknown slug must read as "not a member", never as a guess."""
    cfg = _Cfg({"Review Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, "nobody-by-that-slug") is None


def test_member_name_for_slug_falls_back_to_a_direct_scan(monkeypatch):
    """The handler resolver is an optional import, so its absence cannot be fatal.

    ``dashboard.handlers.members`` pulls in the whole dashboard package; a CLI-only
    or partially installed process can fail that import, and the hook still has to
    answer. The fallback scans ``cfg.agents`` through ``members.slug_for_name``.
    """
    import kiro_crew.dashboard.handlers.members as members_handler

    def _unavailable(cfg, slug):
        raise RuntimeError("resolver unavailable in this process")

    monkeypatch.setattr(members_handler, "_member_names_for_slug", _unavailable)
    name = "Review Agent"
    cfg = _Cfg({name: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(name)) == name


def test_member_name_for_slug_skips_a_name_that_cannot_be_slugged(monkeypatch):
    """One unsluggable roster entry must not hide the members after it.

    The scan is ordered, so a raise on entry one would otherwise lose entry two --
    the same "one broken peer must not mask healthy ones" rule the doctor sections
    follow.
    """
    import kiro_crew.dashboard.handlers.members as members_handler
    from kiro_crew import members as members_mod

    monkeypatch.setattr(
        members_handler,
        "_member_names_for_slug",
        lambda cfg, slug: (_ for _ in ()).throw(RuntimeError("resolver unavailable")),
    )
    real_slug_for_name = members_mod.slug_for_name

    def _explode_on_first(name):
        if name == "Broken Entry":
            raise ValueError("cannot slug this name")
        return real_slug_for_name(name)

    monkeypatch.setattr(members_mod, "slug_for_name", _explode_on_first)
    wanted = "Review Agent"
    cfg = _Cfg({"Broken Entry": object(), wanted: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, real_slug_for_name(wanted)) == wanted


def test_member_name_for_slug_survives_a_config_without_agents(monkeypatch):
    """A config shape this helper cannot read is "no member", not a crash."""
    import kiro_crew.dashboard.handlers.members as members_handler

    monkeypatch.setattr(
        members_handler,
        "_member_names_for_slug",
        lambda cfg, slug: (_ for _ in ()).throw(RuntimeError("resolver unavailable")),
    )

    assert eventlog_hooks.member_name_for_slug(object(), "review-agent") is None

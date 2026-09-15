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

"""The product token outbound git traffic carries: one token, version only, always set."""

from __future__ import annotations

import re

import kiro_crew
from kiro_crew import outbound_user_agent as ua_mod
from kiro_crew.beacon import release
from kiro_crew.outbound_user_agent import (
    GIT_HTTP_USER_AGENT_ENV,
    apply_git_user_agent,
    drop_inherited_git_user_agent,
    git_http_user_agent,
    outbound_user_agent,
)


def test_the_token_is_the_product_and_the_clamped_release_version_only() -> None:
    token = outbound_user_agent()
    assert token == f"KiroCrew/{release(kiro_crew.__version__)}"
    # One RFC 9110 product token: no spaces, no comment, so a server's User-Agent
    # parser reads it as a single client.
    assert re.fullmatch(r"KiroCrew/\d+\.\d+(?:\.\d+)?", token), token


def test_a_per_build_stamp_never_reaches_the_token(monkeypatch) -> None:
    """A nightly or enterprise build stamp would name one build, and so nearly one
    install; the token carries the release the beacon reports and nothing finer."""
    monkeypatch.setattr(ua_mod, "__version__", "0.1.2-nightly.20260731t065756")
    assert outbound_user_agent() == "KiroCrew/0.1.2"
    monkeypatch.setattr(ua_mod, "__version__", "0.6.0.10")
    assert outbound_user_agent() == "KiroCrew/0.6.0"


def test_apply_sets_the_whole_header_when_nothing_of_the_operators_is_inherited() -> None:
    env: dict[str, str] = {"PATH": "/usr/bin"}
    out = apply_git_user_agent(env)
    assert out is env
    assert env[GIT_HTTP_USER_AGENT_ENV] == git_http_user_agent()
    assert env["PATH"] == "/usr/bin"
    # A header this product set itself, at this or an older version, is replaced whole.
    ours = {GIT_HTTP_USER_AGENT_ENV: "git/2.0 KiroCrew/0.1.0"}  # brand-ok: wire identifier
    assert apply_git_user_agent(ours)[GIT_HTTP_USER_AGENT_ENV] == git_http_user_agent()


def test_apply_keeps_an_operators_user_agent_and_appends_the_token_once() -> None:
    """git-config(1) documents ``GIT_HTTP_USER_AGENT`` for proxies that admit only
    known User-Agent strings. The operator's value is kept and the Kiro Crew token
    appended, so their proxy still matches and the host still counts; a value an
    older Kiro Crew already extended ends up with one token at this version."""
    theirs = {GIT_HTTP_USER_AGENT_ENV: "git/9.9.9 corp-proxy"}
    assert apply_git_user_agent(theirs)[GIT_HTTP_USER_AGENT_ENV] == (
        f"git/9.9.9 corp-proxy {outbound_user_agent()}"
    )
    extended = {GIT_HTTP_USER_AGENT_ENV: "git/9.9.9 corp-proxy KiroCrew/0.1.0"}  # brand-ok
    assert apply_git_user_agent(extended)[GIT_HTTP_USER_AGENT_ENV] == (
        f"git/9.9.9 corp-proxy {outbound_user_agent()}"
    )
    # Only our token, no git prefix (not a header this product writes): nothing of
    # the operator's is in it, so the whole header is set.
    bare = {GIT_HTTP_USER_AGENT_ENV: "KiroCrew/0.1.0"}  # brand-ok: wire identifier
    assert apply_git_user_agent(bare)[GIT_HTTP_USER_AGENT_ENV] == git_http_user_agent()


def test_the_gate_fails_closed_on_a_config_it_cannot_trust(monkeypatch, caplog) -> None:
    """A degraded ``telemetry`` or ``dashboard`` section holds defaults, and the
    default ``beacon_enabled`` is true: the verdict is withheld rather than read off
    a document the loader discarded. A load that raises never breaks a spawn."""
    import logging

    calls: list[str] = []

    def _verdict(*, enabled, acked, audit_tool=""):
        calls.append(audit_tool)
        return type("V", (), {"ok": True})()

    monkeypatch.setattr(ua_mod, "telemetry_permitted", _verdict)

    def _cfg(degraded: frozenset[str]):
        return type(
            "C",
            (),
            {
                "telemetry": type("T", (), {"beacon_enabled": True})(),
                "dashboard": type("D", (), {"privacy_acked": True})(),
                "degraded_sections": degraded,
            },
        )()

    for degraded in (frozenset({"telemetry"}), frozenset({"dashboard"}), frozenset({"*"})):
        monkeypatch.setattr(ua_mod.KiroCrewConfig, "load", staticmethod(lambda d=degraded: _cfg(d)))
        with caplog.at_level(logging.WARNING, logger=ua_mod.__name__):
            assert ua_mod.git_user_agent_permitted() is False
        assert "withheld" in caplog.text
    # A degraded section this gate does not read from leaves the verdict to the ladder.
    monkeypatch.setattr(
        ua_mod.KiroCrewConfig, "load", staticmethod(lambda: _cfg(frozenset({"slack"})))
    )
    assert ua_mod.git_user_agent_permitted() is True
    assert calls == ["git_user_agent"]

    def _boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(ua_mod.KiroCrewConfig, "load", staticmethod(_boom))
    assert ua_mod.git_user_agent_permitted() is False
    assert calls == ["git_user_agent"]


def test_the_gate_is_the_shared_telemetry_verdict(monkeypatch) -> None:
    """One consent ladder for every outbound signal: the gate hands the beacon the
    same two config inputs the heartbeat reads, under its own audit name, and
    answers with the verdict's ``ok``."""
    seen: dict[str, object] = {}

    class _Cfg:
        telemetry = type("T", (), {"beacon_enabled": True})()
        dashboard = type("D", (), {"privacy_acked": False})()
        degraded_sections: frozenset[str] = frozenset()

    def _verdict(*, enabled, acked, audit_tool=""):
        seen.update(enabled=enabled, acked=acked, audit_tool=audit_tool)
        return type("V", (), {"ok": False})()

    monkeypatch.setattr(ua_mod.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    monkeypatch.setattr(ua_mod, "telemetry_permitted", _verdict)
    assert ua_mod.git_user_agent_permitted() is False
    assert seen == {"enabled": True, "acked": False, "audit_tool": "git_user_agent"}


def test_drop_removes_only_this_products_inherited_token() -> None:
    """A gateway started from a tagged agent shell inherits a Kiro Crew token, at
    this version or an older one; a refused verdict drops exactly that and leaves
    an operator's own User-Agent as exported."""
    ours = {GIT_HTTP_USER_AGENT_ENV: git_http_user_agent()}
    assert GIT_HTTP_USER_AGENT_ENV not in drop_inherited_git_user_agent(ours)
    older = {GIT_HTTP_USER_AGENT_ENV: "git/2.0 KiroCrew/0.1.0"}  # brand-ok: wire identifier
    assert GIT_HTTP_USER_AGENT_ENV not in drop_inherited_git_user_agent(older)
    theirs = {GIT_HTTP_USER_AGENT_ENV: "git/9.9.9 corp-proxy", "PATH": "/usr/bin"}
    out = drop_inherited_git_user_agent(theirs)
    assert out is theirs and out[GIT_HTTP_USER_AGENT_ENV] == "git/9.9.9 corp-proxy"
    # An operator's value a permitted spawn extended loses only our token.
    extended = {GIT_HTTP_USER_AGENT_ENV: "git/9.9.9 corp-proxy KiroCrew/0.1.0"}  # brand-ok
    assert (
        drop_inherited_git_user_agent(extended)[GIT_HTTP_USER_AGENT_ENV] == "git/9.9.9 corp-proxy"
    )
    assert drop_inherited_git_user_agent({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_the_header_leads_with_a_git_product_token() -> None:
    """Some forges and SSO proxies tell a git client from a browser by the ``git/``
    prefix; the Kiro Crew token comes second, so a host counts by matching it."""
    header = git_http_user_agent()
    assert header.startswith("git/2.0 ")
    assert header == f"git/2.0 {outbound_user_agent()}"
    # Two RFC 9110 product tokens, nothing else.
    assert re.fullmatch(r"git/2\.0 KiroCrew/\d+\.\d+(?:\.\d+)?", header), header

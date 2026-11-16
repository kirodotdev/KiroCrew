"""The work board's dashboard deep links (``board/links.py``), built directly.

Every other board test installs its own link function, so the real builder is
exercised here: the configured origin, the opt-in tunnel, no token ever, and no
link at all when the config cannot be read.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.config import loader
from kiro_crew.dashboard import urls
from kiro_crew.slack.code_channels.board import links


def _config(monkeypatch, *, url="http://127.0.0.1:6767", use_tunnel=False):
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(url=url), slack=SimpleNamespace(use_tunnel_url=use_tunnel)
    )
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: cfg))


def test_a_row_links_to_its_session_on_the_configured_dashboard(monkeypatch):
    _config(monkeypatch)
    link = links.link_builder()
    assert link("chat-w1") == "http://127.0.0.1:6767/chat?sid=chat-w1"
    assert "token" not in link("chat-w1")  # never a click token: the channel is shared
    assert link("") == ""  # an undispatched row gets no link


def test_an_opted_in_tunnel_is_the_links_origin(monkeypatch):
    _config(monkeypatch, use_tunnel=True)
    monkeypatch.setattr(urls, "tunnel_origin_if_opted_in", lambda opted: "https://t.example")
    assert links.link_builder()("chat-w1") == "https://t.example/chat?sid=chat-w1"


def test_no_dashboard_origin_means_no_link(monkeypatch):
    _config(monkeypatch, url="")
    assert links.link_builder()("chat-w1") == ""


def test_an_unreadable_config_gives_a_builder_that_links_nothing(monkeypatch):
    def broken(cls):
        raise OSError("config unreadable")

    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(broken))
    link = links.link_builder()
    assert link is links._no_link and link("chat-w1") == ""

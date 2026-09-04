"""`kirocrew to`+`ken` must hand out a URL for the tailnet origin too.

The flow this closes: `tailnet up` publishes the dashboard and prints
`https://<MagicDNS name>`, but that URL carries no session, so a phone opening it lands
on a login it cannot complete. The gateway derives that origin itself precisely so the
operator does NOT have to set `dashboard.url` -- which also meant the existing
`dashboard.url` branch had nothing to print for it. The operator was left splicing a
query string onto a hostname by hand.
"""

from __future__ import annotations

import json

import pytest

SESSION = "s3ss10n-value"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _write_cfg(home, *, enabled: bool, url: str = "") -> None:
    dash: dict = {"tailscale": {"enabled": enabled}}
    if url:
        dash["url"] = url
    (home / "config.json").write_text(json.dumps({"timezone": "UTC", "dashboard": dash}))


def _stub_serve_state(monkeypatch, *, published: bool | None, detail: str = "serving"):
    """Stub the 443/ ownership probe.

    Published-by-us is a PRECONDITION of printing a tailnet URL, not an extra: the URL
    carries a bearer session, so it may only be handed out when this dashboard is the
    verified service behind that name.
    """
    from kiro_crew import cli_server
    from kiro_crew.dashboard.tailnet_serve import ServeState

    monkeypatch.setattr(
        cli_server.tailnet_serve,
        "serve_state",
        lambda _port: ServeState(published, True, detail),
    )


def _run(monkeypatch, capsys, *, name: str | None, published: bool | None = True):
    """Drive the real printer with the Tailscale lookups stubbed."""
    from kiro_crew import cli_server

    monkeypatch.setattr(
        cli_server, "tailnet_origin", lambda: (f"https://{name}" if name else None)
    )
    _stub_serve_state(monkeypatch, published=published)
    monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **_k: "localhost")
    monkeypatch.setattr(cli_server, "_probe_dashboard_health", lambda *_a, **_k: None)
    cli_server._emit_session_urls(5476, SESSION)  # type: ignore[attr-defined]
    return capsys.readouterr()


class TestTheTailnetUrlIsPrinted:
    def test_an_enabled_tailnet_gets_its_own_session_url(self, home, monkeypatch, capsys):
        _write_cfg(home, enabled=True)
        out = _run(monkeypatch, capsys, name="box.example-tailnet.ts.net")
        assert f"http://localhost:5476?token={SESSION}" in out.out
        assert f"https://box.example-tailnet.ts.net/?token={SESSION}" in out.out

    def test_it_is_not_printed_when_the_setting_is_off(self, home, monkeypatch, capsys):
        """Off means the gateway trusts no tailnet origin, so such a URL would 403."""
        _write_cfg(home, enabled=False)
        out = _run(monkeypatch, capsys, name="box.example-tailnet.ts.net")
        assert "ts.net" not in out.out

    def test_the_lookup_is_skipped_entirely_when_disabled(self, home, monkeypatch, capsys):
        """`tailnet_origin()` shells out with a multi-second timeout, and this command
        is run constantly in the foreground."""
        from kiro_crew import cli_server

        called: list[int] = []
        monkeypatch.setattr(
            cli_server, "tailnet_origin", lambda: called.append(1) or "https://x.ts.net"
        )
        monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **_k: "localhost")
        _write_cfg(home, enabled=False)
        cli_server._emit_session_urls(5476, SESSION)  # type: ignore[attr-defined]
        capsys.readouterr()
        assert called == [], "the Tailscale CLI was invoked even though the setting is off"

    def test_an_unresolvable_name_says_so_instead_of_printing_nothing(
        self, home, monkeypatch, capsys
    ):
        """Silence is ambiguous: a missing line and a broken tailnet look identical,
        and the second also means the gateway trusted no origin, so it would 403."""
        _write_cfg(home, enabled=True)
        out = _run(monkeypatch, capsys, name=None)
        assert "no tailnet name resolves" in out.err
        assert SESSION in out.out  # the loopback URL is still usable

    def test_a_configured_url_equal_to_the_tailnet_origin_is_not_duplicated(
        self, home, monkeypatch, capsys
    ):
        name = "box.example-tailnet.ts.net"
        _write_cfg(home, enabled=True, url=f"https://{name}")
        out = _run(monkeypatch, capsys, name=name)
        assert out.out.count(f"https://{name}/?token={SESSION}") == 1

    def test_both_a_custom_domain_and_the_tailnet_are_printed(self, home, monkeypatch, capsys):
        """A reverse proxy and a tailnet can be reachable at the same time."""
        _write_cfg(home, enabled=True, url="https://crew.example.com")
        out = _run(monkeypatch, capsys, name="box.example-tailnet.ts.net")
        assert f"https://crew.example.com/?token={SESSION}" in out.out
        assert f"https://box.example-tailnet.ts.net/?token={SESSION}" in out.out


class TestTheGovernanceCeilingIsHonoured:
    """A policy pin outranks the stored flag.

    An enterprise ceiling can pin `capabilities.tailnet_origin` off, and then the gateway
    derives no tailnet origin at startup regardless of config -- so a URL printed here
    would answer 403. The pin is also cheaper to consult than the Tailscale CLI, so it is
    checked first.
    """

    def test_no_tailnet_url_when_policy_pins_it_off(self, home, monkeypatch, capsys):
        from kiro_crew import cli_server

        _write_cfg(home, enabled=True)
        monkeypatch.setattr(cli_server, "is_governance_pinned_off", lambda **_k: True)
        looked_up: list[int] = []
        monkeypatch.setattr(
            cli_server,
            "tailnet_origin",
            lambda: looked_up.append(1) or "https://box.example-tailnet.ts.net",
        )
        monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **_k: "localhost")
        cli_server._emit_session_urls(5476, SESSION)  # type: ignore[attr-defined]
        out = capsys.readouterr()
        assert "ts.net" not in out.out
        assert "pinned tailnet access off" in out.err
        assert looked_up == [], "the Tailscale CLI ran even though policy pinned it off"
        assert f"?token={SESSION}" in out.out, "the loopback URL must still be usable"

    def test_the_pin_is_read_without_the_audit_seam(self, home, monkeypatch, capsys):
        """Passing `audit_tool` would append an HMAC-chained SEL row per invocation.

        The helper's own contract reserves that argument for ENFORCEMENT call sites; this
        is a read-shaped question on a command run constantly in the foreground.
        """
        from kiro_crew import cli_server

        _write_cfg(home, enabled=True)
        seen: list[dict] = []

        def _probe(**kw):
            seen.append(kw)
            return False

        monkeypatch.setattr(cli_server, "is_governance_pinned_off", _probe)
        monkeypatch.setattr(cli_server, "tailnet_origin", lambda: "https://b.ts.net")
        _stub_serve_state(monkeypatch, published=True)
        monkeypatch.setattr(cli_server, "resolve_dashboard_host", lambda **_k: "localhost")
        cli_server._emit_session_urls(5476, SESSION)  # type: ignore[attr-defined]
        capsys.readouterr()
        assert seen == [{}], f"expected an unaudited read, got {seen}"


class TestTheTokenIsNotHandedToAForeignService:
    """The URL carries a bearer session, so 443/ ownership must be verified first.

    `tailnet up` deliberately REFUSES to overwrite a foreign 443/ handler, so a host can
    sit with the trust flag on and a resolvable MagicDNS name while the mount belongs to
    something unrelated. Printing the URL there hands the operator a link that delivers
    their session to that other service, which can replay it against the dashboard. The
    flag and the name were never evidence of ownership.
    """

    def test_nothing_is_printed_when_another_service_holds_the_name(
        self, home, monkeypatch, capsys
    ):
        _write_cfg(home, enabled=True)
        out = _run(
            monkeypatch, capsys, name="box.example-tailnet.ts.net", published=False
        )
        assert "ts.net/?token=" not in out.out, "handed the session to a foreign service"
        assert "not verified" in out.err
        assert f"?token={SESSION}" in out.out, "the loopback URL must still be usable"

    def test_unknown_ownership_fails_closed(self, home, monkeypatch, capsys):
        """None means the serve config could not be read -- unknown ownership IS the risk."""
        _write_cfg(home, enabled=True)
        out = _run(monkeypatch, capsys, name="box.example-tailnet.ts.net", published=None)
        assert "ts.net/?token=" not in out.out
        assert "not verified" in out.err

    def test_the_refusal_names_the_remedy(self, home, monkeypatch, capsys):
        _write_cfg(home, enabled=True)
        out = _run(
            monkeypatch, capsys, name="box.example-tailnet.ts.net", published=False
        )
        assert "kirocrew tailnet up" in out.err

    def test_a_published_dashboard_still_gets_its_url(self, home, monkeypatch, capsys):
        """The guard must not break the case it exists to protect."""
        _write_cfg(home, enabled=True)
        out = _run(monkeypatch, capsys, name="box.example-tailnet.ts.net", published=True)
        assert f"https://box.example-tailnet.ts.net/?token={SESSION}" in out.out

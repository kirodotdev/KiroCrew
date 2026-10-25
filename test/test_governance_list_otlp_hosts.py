"""``capabilities.telemetry`` ``otlp_hosts``: which OTLP collectors metrics may reach.

RFC crew-enterprise-edition, appendix row "Diagnostic telemetry target". The key's
parse and evaluator contract is in ``test_governance_list_keys.py``; this file
covers host globs and the call sites: the reader build (audited), the per-export
gate, the local-only enable gate (unfiltered on purpose) and the context-less MCP
gateway daemon.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.platform.governance import parse_policy, resolve


def _policy(scope: str, body: dict) -> dict:
    return {
        "version": 1,
        "boot": {"fail_closed": True},
        "capabilities": {scope.split(".", 1)[1]: body},
    }


def _allow(scope: str, key: str, allowed: str) -> dict:
    return _policy(scope, {"scopes": {key: {"mode": "allow", "allow": [allowed]}}})


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_sel(monkeypatch):
    import kiro_crew.sel as sel_mod

    fake = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return fake


def _install_policy(monkeypatch, doc: dict | None) -> None:
    from kiro_crew.metrics import provider
    from kiro_crew.platform import context as pc

    ceiling = parse_policy(doc) if doc is not None else None
    ctx = SimpleNamespace(governance=ceiling)
    monkeypatch.setattr(pc, "current_context", lambda: ctx)
    # ``metrics.provider`` binds the name at import, so it is patched there too.
    monkeypatch.setattr(provider, "current_context", lambda: ctx)


# ── The keys, through the real evaluator ────────────────────────────────


class TestListKeys:
    def test_a_host_glob_binds_subdomains(self) -> None:
        ceiling = parse_policy(_allow("capabilities.telemetry", "otlp_hosts", "*.corp.example"))
        scope = "capabilities.telemetry"
        assert resolve(ceiling, None, scope, "otlp_hosts:otel.corp.example").permitted is True
        assert resolve(ceiling, None, scope, "otlp_hosts:corp.example.evil").permitted is False


# ── The call sites ──────────────────────────────────────────────────────


def _dest(name: str, endpoint: str):
    return SimpleNamespace(name=name, endpoint=endpoint, signals=frozenset({"metrics"}))


#: A telemetry config with no local endpoint, as the provider path reads it.
_CFG = SimpleNamespace(otlp_endpoint="")


def _provider_with(monkeypatch, dests, **extra):
    """Point ``metrics.provider`` at a telemetry provider that supplies *dests*."""
    from kiro_crew.metrics import provider

    prov = SimpleNamespace(otlp_destinations=lambda cfg: list(dests), **extra)
    monkeypatch.setattr(provider, "_telemetry_provider", lambda: prov)
    return provider


class TestTelemetryDestinations:
    _SCOPE = "capabilities.telemetry"
    _DESTS = (
        _dest("corp", "https://user:pw@otel.corp.example:4318/v1/metrics"),
        _dest("other", "https://evil.example/v1/metrics"),
    )

    def test_only_listed_collector_hosts_get_a_reader_and_each_is_audited(
        self, fake_sel, monkeypatch
    ) -> None:
        provider = _provider_with(monkeypatch, self._DESTS)
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["corp"]
        rows = [c[1] for c in fake_sel.log_governance_decision.call_args_list]
        assert [(r["item"], r["outcome"], r["layer"]) for r in rows] == [
            ("otlp_hosts:otel.corp.example", "allowed", "policy"),
            ("otlp_hosts:evil.example", "denied", "policy"),
        ]

    def test_a_gate_pinned_off_builds_no_reader(self, fake_sel, monkeypatch) -> None:
        provider = _provider_with(monkeypatch, self._DESTS)
        _install_policy(monkeypatch, _policy(self._SCOPE, {"enabled": False}))
        assert provider._otlp_destinations(_CFG) == ()

    def test_no_policy_keeps_every_destination(self, fake_sel, monkeypatch) -> None:
        provider = _provider_with(monkeypatch, self._DESTS)
        _install_policy(monkeypatch, None)
        assert provider._otlp_destinations(_CFG) == self._DESTS
        # A no-policy grant is still an egress grant, so the build audits it.
        rows = [c[1] for c in fake_sel.log_governance_decision.call_args_list]
        assert [(r["item"], r["outcome"]) for r in rows] == [
            ("otlp_hosts:otel.corp.example", "allowed"),
            ("otlp_hosts:evil.example", "allowed"),
        ]

    def test_the_enable_gate_still_reports_egress_for_a_refused_host(
        self, fake_sel, monkeypatch
    ) -> None:
        """``otlp_egress_active`` answers whether enabling collection could ever
        send metrics away; a host refused today may be permitted by a later policy,
        so the local-only enable switch must still be refused."""
        provider = _provider_with(monkeypatch, self._DESTS[1:])
        _install_policy(monkeypatch, _policy(self._SCOPE, {"enabled": False}))
        assert provider.otlp_egress_active(_CFG) is True
        assert fake_sel.log_governance_decision.call_count == 0

    def test_a_process_without_a_context_uses_the_companion_ceiling(
        self, fake_sel, monkeypatch
    ) -> None:
        """The MCP gateway daemon installs no context; the provider the bootstrap
        composes for it carries the companion's ceiling, for the build and for
        every export."""
        from kiro_crew.platform import context as pc
        from kiro_crew.platform.bootstrap import _PolicyBoundTelemetry

        def _raise():
            raise pc.PlatformCompositionError("no context")

        provider = _provider_with(monkeypatch, ())
        monkeypatch.setattr(provider, "current_context", _raise)
        inner = SimpleNamespace(otlp_destinations=lambda cfg: list(self._DESTS))
        ceiling = parse_policy(_allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        bound = _PolicyBoundTelemetry(inner, ceiling)
        monkeypatch.setattr(provider, "_telemetry_provider", lambda: bound)
        assert provider._policy_ceiling() is ceiling
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["corp"]
        # The inspection read is the companion's unfiltered set.
        assert list(bound.otlp_destinations(_CFG)) == list(self._DESTS)

    def test_a_process_without_a_context_still_writes_the_audit_row(self, monkeypatch) -> None:
        """With no context the SEL's own redaction shim raises; the row goes
        through the composed companion's redaction instead of being dropped."""
        from kiro_crew import sel as sel_mod
        from kiro_crew.platform import context as pc
        from kiro_crew.platform.bootstrap import _PolicyBoundTelemetry

        def _raise():
            raise pc.PlatformCompositionError("no context")

        provider = _provider_with(monkeypatch, ())
        monkeypatch.setattr(provider, "current_context", _raise)
        monkeypatch.setattr(pc, "current_context", _raise)
        redacted: list[str] = []

        def _redact(text: str) -> str:
            redacted.append(text)
            return text

        ceiling = parse_policy(_allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        bound = _PolicyBoundTelemetry(
            SimpleNamespace(otlp_destinations=lambda cfg: list(self._DESTS)),
            ceiling,
            _redact,
        )
        monkeypatch.setattr(provider, "_telemetry_provider", lambda: bound)
        log = MagicMock()
        real = sel_mod.SecurityEventLog.log_governance_decision
        monkeypatch.setattr(
            sel_mod,
            "sel",
            lambda: SimpleNamespace(
                log_governance_decision=lambda **kw: real(SimpleNamespace(log=log), **kw)
            ),
        )
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["corp"]
        events = [c[0][0] for c in log.call_args_list]
        assert [(e.resources, e.outcome) for e in events] == [
            ("otlp_hosts:otel.corp.example", "allowed"),
            ("otlp_hosts:evil.example", "denied"),
        ]
        assert "otlp_hosts:evil.example" in redacted

    def test_a_running_exporter_stops_when_a_new_policy_denies_its_host(
        self, fake_sel, monkeypatch
    ) -> None:
        """A policy installed at runtime binds the next export, the shutdown flush
        included, without a rebuild; the change is audited once."""
        pytest.importorskip("opentelemetry.sdk.metrics.export")
        from opentelemetry.sdk.metrics.export import MetricExportResult

        from kiro_crew.metrics import otlp_governance

        sent: list[object] = []
        exporter = SimpleNamespace(export=lambda data, **kw: sent.append(data) or "sent")
        ceiling = {"now": parse_policy(_allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))}
        otlp_governance.gate_exporter(
            exporter, "https://otel.corp.example/v1/metrics", lambda: ceiling["now"]
        )

        assert exporter.export("batch-1") == "sent"
        ceiling["now"] = parse_policy(_allow(self._SCOPE, "otlp_hosts", "other.example"))
        assert exporter.export("batch-2") is MetricExportResult.FAILURE
        assert exporter.export("batch-3") is MetricExportResult.FAILURE
        assert sent == ["batch-1"]
        rows = [c[1] for c in fake_sel.log_governance_decision.call_args_list]
        assert [(r["item"], r["outcome"]) for r in rows] == [
            ("otlp_hosts:otel.corp.example", "denied")
        ]


class TestEndpointReadings:
    """The host is asked in every reading a client could take, so a parser
    disagreement cannot reach a host the list refused."""

    _SCOPE = "capabilities.telemetry"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://evil.example\\@otel.corp.example/v1/metrics",
            "https://outside.example\\@otel.corp.example/v1/metrics",
        ],
    )
    def test_a_backslash_authority_is_refused(self, fake_sel, monkeypatch, endpoint) -> None:
        provider = _provider_with(monkeypatch, [_dest("trick", endpoint)])
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        assert provider._otlp_destinations(_CFG) == ()

    def test_a_root_dot_cannot_dodge_a_deny(self, fake_sel, monkeypatch) -> None:
        provider = _provider_with(monkeypatch, [_dest("dot", "https://evil.example./v1/metrics")])
        _install_policy(
            monkeypatch,
            _policy(
                self._SCOPE, {"scopes": {"otlp_hosts": {"mode": "deny", "deny": ["evil.example"]}}}
            ),
        )
        assert provider._otlp_destinations(_CFG) == ()

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://stra\u00dfe.example/v1/metrics",  # requests IDNA-encodes it
            "https://%6Ftel.corp.example.evil/v1/metrics",  # urllib percent-decodes it
        ],
    )
    def test_a_host_the_transport_reads_differently_is_refused(
        self, fake_sel, monkeypatch, endpoint
    ) -> None:
        provider = _provider_with(monkeypatch, [_dest("trick", endpoint)])
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "strasse.example"))
        assert provider._otlp_destinations(_CFG) == ()

    def test_the_ordinary_endpoint_is_still_permitted(self, fake_sel, monkeypatch) -> None:
        provider = _provider_with(
            monkeypatch, [_dest("corp", "https://user:pw@OTEL.corp.example.:4318/v1/metrics")]
        )
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["corp"]

    @pytest.mark.parametrize(
        ("endpoint", "deny"),
        [
            ("http://167772165:4318/v1/metrics", "10.0.0.5"),  # decimal IPv4
            ("http://0x0a000005:4318/v1/metrics", "10.0.0.5"),  # hex IPv4
            ("http://[::ffff:10.0.0.5]:4318/v1/metrics", "10.0.0.5"),  # v4-mapped
            ("http://[fe80::1%25eth0]:4318/v1/metrics", "fe80::*"),  # scoped IPv6
            ("http://[2001:db8:0:0:0:0:0:1]:4318/v1/metrics", "2001:db8::1"),  # long IPv6
            ("https://b\u00fccher.example/v1/metrics", "xn--bcher-*"),  # sent as xn--
            ("https://stra\u00dfe.example/v1/metrics", "strasse.example"),  # urllib's idna
            ("https://fa%C3%9F.example/v1/metrics", "fass.example"),  # decoded, then idna
            ("https://clip+one.blocked.example/v1/metrics", "*.blocked.example"),  # odd ASCII
            ("https://clip+one.blocked.example/v1/metrics", "clip+*.blocked.example"),
            ("https://clip+one.blocked.example/v1/metrics", "clip+one.blocked.example"),
        ],
        ids=[
            "decimal",
            "hex",
            "v4-mapped",
            "ipv6-zone",
            "long-ipv6",
            "unicode",
            "sharp-s",
            "pct-sharp-s",
            "odd-ascii",
            "odd-ascii-glob",
            "odd-ascii-exact",
        ],
    )
    def test_a_deny_names_every_spelling_of_its_host(
        self, fake_sel, monkeypatch, endpoint, deny
    ) -> None:
        provider = _provider_with(monkeypatch, [_dest("d", endpoint)])
        doc = _policy(self._SCOPE, {"scopes": {"otlp_hosts": {"mode": "deny", "deny": [deny]}}})
        _install_policy(monkeypatch, doc)
        assert provider._otlp_destinations(_CFG) == ()

    def test_the_shared_host_matcher_is_unchanged(self) -> None:
        """``network.egress`` shares the matcher, so these lists change nothing there:
        a Unicode entry still matches only the Unicode spelling."""
        from kiro_crew.platform.governance import _match_host

        assert _match_host("b\u00fccher.example", "b\u00fc*.example")
        assert not _match_host("xn--bcher-kva.example", "b\u00fccher.example")

    def test_userinfo_is_never_read_as_a_host(self, fake_sel, monkeypatch) -> None:
        """An encoded ``#`` or ``/`` in a password must not cut the authority."""
        provider = _provider_with(
            monkeypatch,
            [
                _dest("a", "https://tok%23en@otel.corp.example/v1/metrics"),
                _dest("b", "https://user:p%2Fsecret@otel.corp.example:4318/v1/metrics"),
            ],
        )
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "otel.corp.example"))
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["a", "b"]
        items = {c[1]["item"] for c in fake_sel.log_governance_decision.call_args_list}
        assert items == {"otlp_hosts:otel.corp.example"}

    def test_an_idna_name_is_asked_in_its_ascii_form(self, fake_sel, monkeypatch) -> None:
        provider = _provider_with(monkeypatch, [_dest("b", "https://b\u00fccher.example/v1")])
        _install_policy(monkeypatch, _allow(self._SCOPE, "otlp_hosts", "xn--bcher-kva.example"))
        assert [d.name for d in provider._otlp_destinations(_CFG)] == ["b"]


class TestUnevaluableCeiling:
    def test_no_context_and_no_carried_ceiling_refuses_every_host(
        self, fake_sel, monkeypatch
    ) -> None:
        """A daemon whose policy could not be loaded must not export as if ungoverned."""
        from kiro_crew.platform import context as pc

        def _raise():
            raise pc.PlatformCompositionError("policy unreadable")

        provider = _provider_with(monkeypatch, [_dest("a", "https://otel.corp.example/v1")])
        monkeypatch.setattr(provider, "current_context", _raise)
        assert provider._otlp_destinations(_CFG) == ()
        rows = [c[1] for c in fake_sel.log_governance_decision.call_args_list]
        assert [r["outcome"] for r in rows] == ["denied"]
        assert "fail-closed" in rows[0]["reason"]


class TestWrongShape:
    @pytest.mark.parametrize("value", [["otel.corp.example"], "otel.corp.example", 1, None])
    def test_a_host_list_in_the_wrong_shape_fails_to_load(self, value) -> None:
        """A malformed host list must not load as no list, which permits every host."""
        from kiro_crew.platform.context import PlatformCompositionError

        with pytest.raises(PlatformCompositionError, match="scopes.otlp_hosts must be an object"):
            parse_policy(_policy("capabilities.telemetry", {"scopes": {"otlp_hosts": value}}))


class TestNoCeilingAsksTheEvaluator:
    def test_no_ceiling_follows_what_resolve_answers(self, fake_sel, monkeypatch) -> None:
        """With no policy, the build and each export take ``resolve``'s answer."""
        from types import SimpleNamespace

        from kiro_crew.metrics import otlp_governance as og

        dest = SimpleNamespace(name="d", endpoint="https://otel.corp.example/v1/metrics")
        assert og.governed_destinations([dest], None) == (dest,)

        def _deny(*_a, **_k):
            return SimpleNamespace(permitted=False, rule="baseline", reason="denied")

        monkeypatch.setattr("kiro_crew.platform.governance.resolve", _deny)
        assert og.governed_destinations([dest], None) == ()

        sent = []
        exporter = SimpleNamespace(export=lambda data, *a, **k: sent.append(data) or "OK")
        og.gate_exporter(exporter, dest.endpoint, lambda: None)
        exporter.export("batch")
        assert sent == []


class TestUnmatchableEntriesAreRefused:
    """An entry no host reading can match fails the policy load, instead of a
    deny that refuses nothing or an allow that admits nothing."""

    _SCOPE = "capabilities.telemetry"

    @pytest.mark.parametrize("mode", ["allow", "deny"])
    @pytest.mark.parametrize(
        "entry",
        [
            "b\u00fccher.example",
            "b\u00fc*.example",
            "otel.corp.example.",
            "2001:db8:0:0:0:0:0:1",
            "010.0.0.5",
            "fe80::1%eth0",
            "[2001:db8::1]",
            "otel.corp.example:4318",
            "fe80::1%eth*",
            "evil.example:*",
            "*@evil.example",
            "evil.example/*",
            "[ab]vil.example",
            "ev il.example",
            "ev\\il*",
        ],
        ids=[
            "unicode",
            "unicode-glob",
            "root-dot",
            "long-ipv6",
            "octal-ipv4",
            "zoned-ipv6",
            "bracketed-ipv6",
            "port",
            "glob-zone",
            "glob-port",
            "glob-userinfo",
            "glob-path",
            "glob-class",
            "space",
            "glob-backslash",
        ],
    )
    def test_the_policy_fails_to_load(self, mode, entry) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        doc = _policy(self._SCOPE, {"scopes": {"otlp_hosts": {"mode": mode, mode: [entry]}}})
        with pytest.raises(PlatformCompositionError, match=r"otlp_hosts"):
            parse_policy(doc)

    @pytest.mark.parametrize(
        "entry",
        [
            "xn--bcher-kva.example",
            "*.corp.example",
            "otel-?.corp.example",
            "2001:db8::1",
            "10.0.0.5",
            "10.0.0.*",
            "fe80::*",
            "fd00:0:1::*",
            "2001:db8:10:0:*",
            "01-collector*.corp.example",
            "*.0x.org",
            "*.007agent.example",
            "*_svc.corp.example",
            "*",
            "10*",
            "*:1:0:0:1",
            "clip+one.blocked.example",
            "clip+*.blocked.example",
            "ev%69l.example",
            "*.ev%69l.example",
            "a!*.blocked.example",
        ],
    )
    def test_a_matchable_entry_loads(self, entry) -> None:
        doc = _policy(self._SCOPE, {"scopes": {"otlp_hosts": {"mode": "deny", "deny": [entry]}}})
        assert parse_policy(doc).get(self._SCOPE) is not None

    def test_network_egress_entries_are_not_checked(self) -> None:
        """Top-level ``network.egress`` keeps its own parsing."""
        doc = {
            "version": 1,
            "boot": {"fail_closed": True},
            "network": {"egress": {"mode": "deny", "deny": ["b\u00fccher.example"]}},
        }
        assert parse_policy(doc).get("network.egress") is not None

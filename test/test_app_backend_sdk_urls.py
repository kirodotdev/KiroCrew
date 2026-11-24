"""The backend SDK's outbound URL check (``kiro_crew.apps.backend_sdk.urls``).

What is pinned is what an app cannot check for itself: the names are the gateway's
own vet and address rule looked up at call time, the rejection is the gateway's
class, the result carries the attributes the docs name while its class stays private,
the vet offers no knob that approves what the default refuses, and the names are
published at their own seam level. No test touches the network: every URL is an IP
literal or a name answered by a patched resolver.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import link_unfurl
from kiro_crew.apps import backend_sdk as sdk
from kiro_crew.apps.app_sdk_level import APP_SDK_LEVEL
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_SRC = Path(__file__).resolve().parents[1] / "src"

_URL_NAMES = ("vet_public_url", "UrlRejectedError", "address_is_not_public")

#: Globally routable unicast addresses, outside every special-purpose range.
_PUBLIC_V4 = "93.184.216.34"
_PUBLIC_V6 = "2606:4700::1111"


class _Recorder:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture()
def no_lookup(monkeypatch):
    """Fail the test if the vet reaches the resolver."""

    def refuse(host, port):
        raise AssertionError(f"unexpected lookup of {host}:{port}")

    monkeypatch.setattr("kiro_crew.link_unfurl._default_resolve", refuse)


# ── the names and their level ───────────────────────────────────────────────


@pytest.mark.parametrize("name", _URL_NAMES)
def test_the_url_names_are_published_at_level_fourteen(name) -> None:
    assert name in sdk.__all__
    assert name in dir(sdk)
    assert sdk._LAZY_EXPORTS[name] == "urls"
    assert sdk._EXPORT_LEVELS[name] == 14
    assert APP_SDK_LEVEL >= 14
    assert sdk.BACKEND_SDK_LEVEL == 7


def test_the_rejection_is_the_gateway_class() -> None:
    assert sdk.UrlRejectedError is link_unfurl.UnfurlRejected


def test_the_result_class_is_not_an_sdk_name() -> None:
    from kiro_crew.apps.backend_sdk import urls

    assert "VettedUrl" not in sdk.__all__
    assert "VettedUrl" not in sdk._LAZY_EXPORTS
    assert "VettedUrl" not in urls.__all__
    assert not hasattr(sdk, "VettedUrl")


# ── vet_public_url ──────────────────────────────────────────────────────────


def test_vet_public_url_delegates_at_call_time_with_only_the_url(monkeypatch) -> None:
    sentinel = object()
    rec = _Recorder(sentinel)
    monkeypatch.setattr("kiro_crew.link_unfurl.vet_unfurl_url", rec)
    assert sdk.vet_public_url("https://feeds.example.com/a.ics") is sentinel
    assert rec.calls == [(("https://feeds.example.com/a.ics",), {})]


@pytest.mark.parametrize("knob", ["resolve", "allow_any_port", "allow_private"])
def test_vet_public_url_offers_no_widening_knob(monkeypatch, knob) -> None:
    rec = _Recorder(None)
    monkeypatch.setattr("kiro_crew.link_unfurl.vet_unfurl_url", rec)
    assert knob not in inspect.signature(sdk.vet_public_url).parameters
    with pytest.raises(TypeError):
        sdk.vet_public_url(f"https://{_PUBLIC_V4}/", **{knob: True})
    assert rec.calls == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        (f"ftp://{_PUBLIC_V4}/", "invalid_url"),
        (f"https://user:pw@{_PUBLIC_V4}/", "invalid_url"),
        ("https:///feed.xml", "invalid_url"),
        ("https://127.0.0.1/", "blocked_url"),
        ("http://169.254.169.254/latest/meta-data/", "blocked_url"),
        ("https://[::ffff:127.0.0.1]/", "blocked_url"),
        ("https://0x7f000001/", "blocked_url"),
        ("https://100.64.0.1/", "blocked_url"),
        (f"https://{_PUBLIC_V4}:8443/", "blocked_url"),
        ("https://printer.local/", "blocked_url"),
    ],
)
def test_the_vet_refuses_with_the_gateway_code_and_no_lookup(no_lookup, url, code) -> None:
    with pytest.raises(sdk.UrlRejectedError) as caught:
        sdk.vet_public_url(url)
    assert caught.value.code == code


def test_a_public_literal_passes_and_is_its_own_pin(no_lookup) -> None:
    vetted = sdk.vet_public_url(f"https://{_PUBLIC_V4}/feed.xml#top")
    assert isinstance(vetted, link_unfurl.VettedUrl)
    assert vetted.url == f"https://{_PUBLIC_V4}/feed.xml"
    assert vetted.scheme == "https"
    assert vetted.host == vetted.wire_host == vetted.domain == _PUBLIC_V4
    assert vetted.port == 443
    assert vetted.ip == _PUBLIC_V4
    assert vetted.addresses == (_PUBLIC_V4,)


def test_a_name_is_resolved_once_and_every_answer_is_checked(monkeypatch) -> None:
    rec = _Recorder([_PUBLIC_V4, "10.0.0.5"])
    monkeypatch.setattr("kiro_crew.link_unfurl._default_resolve", rec)
    with pytest.raises(sdk.UrlRejectedError) as caught:
        sdk.vet_public_url("https://feeds.example.com/a.ics")
    assert caught.value.code == "blocked_url"
    assert rec.calls == [(("feeds.example.com", 443), {})]

    rec = _Recorder([_PUBLIC_V4, _PUBLIC_V6, _PUBLIC_V4])
    monkeypatch.setattr("kiro_crew.link_unfurl._default_resolve", rec)
    vetted = sdk.vet_public_url("https://feeds.example.com/a.ics")
    assert vetted.addresses == (_PUBLIC_V4, _PUBLIC_V6)
    assert vetted.wire_host == "feeds.example.com"
    assert rec.calls == [(("feeds.example.com", 443), {})]


def test_an_unresolvable_name_is_refused(monkeypatch) -> None:
    def fail(host, port):
        raise OSError("no such host")

    monkeypatch.setattr("kiro_crew.link_unfurl._default_resolve", fail)
    with pytest.raises(sdk.UrlRejectedError) as caught:
        sdk.vet_public_url("https://feeds.example.com/a.ics")
    assert caught.value.code == "blocked_url"


# ── address_is_not_public ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("address", "refused"),
    [
        ("10.0.0.1", True),
        ("100.64.0.1", True),
        ("fec0::1", True),
        ("::ffff:127.0.0.1", True),
        ("169.254.169.254", True),
        ("not-an-address", True),
        (_PUBLIC_V4, False),
        (_PUBLIC_V6, False),
    ],
)
def test_address_is_not_public_answers_with_the_gateway_rule(address, refused) -> None:
    assert sdk.address_is_not_public(address) is refused
    assert sdk.address_is_not_public(address) == link_unfurl.address_is_not_public(address)


def test_address_is_not_public_delegates_at_call_time(monkeypatch) -> None:
    rec = _Recorder(1)
    monkeypatch.setattr("kiro_crew.link_unfurl.address_is_not_public", rec)
    assert sdk.address_is_not_public(_PUBLIC_V4) is True
    assert rec.calls == [((_PUBLIC_V4,), {})]


# ── import cost ─────────────────────────────────────────────────────────────


def test_a_spawned_child_loads_the_url_rule_only_on_first_use(tmp_path) -> None:
    probe = (
        "import sys\n"
        "import kiro_crew.apps.backend_sdk as sdk\n"
        "print('kiro_crew.link_unfurl' in sys.modules)\n"
        "vetted = sdk.vet_public_url(sys.argv[1])\n"
        "print('kiro_crew.link_unfurl' in sys.modules)\n"
        "print(','.join(vetted.addresses))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(_SRC),
        "KIROCREW_SPAWNED": "1",
        "KIROCREW_APP_NAME": "probe-app",
    }
    done = subprocess.run(
        [sys.executable, "-c", probe, f"https://{_PUBLIC_V4}/"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    before, after, addresses = done.stdout.split("\n")[:3]
    assert before == "False"
    assert after == "True"
    assert addresses == _PUBLIC_V4

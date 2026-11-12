"""Readers outside the load that feed enforcement or a write read the endorsed copy.

``loosening_provenance`` serves the gateway-endorsed copy of a sealed config file to
the load. A reader that parses the file itself -- the agent spec builder, the sandbox
resource limits, an uninstall's trust revocation, a channel save's read half -- has to
see the same document, or a tampered, deleted file decides what the load ignores.
These tests pin a few of those readers, plus the secret-segment rule the pending diff
withholds values by.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp home.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir


@pytest.fixture
def _enforcing(monkeypatch):
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    yield loader
    loader._invalidate_config_cache()


def _endorse_then_tamper(loader, endorsed: dict, on_disk: dict | None) -> None:
    """Endorse *endorsed* through the writer, then replace (or, for None, delete) the file."""
    path = config_dir() / "config.json"
    loader.write_config_atomically(path, endorsed)
    if on_disk is None:
        path.unlink()
    else:
        path.write_text(json.dumps(on_disk), encoding="utf-8")


@pytest.mark.parametrize("on_disk", [{"hooks": {}}, None], ids=["tampered", "deleted"])
def test_agent_spec_config_read_is_the_endorsed_copy(_enforcing, on_disk) -> None:
    from kiro_crew import agent

    _endorse_then_tamper(_enforcing, {"hooks": {"auto_deny_tools": ["rm"]}}, on_disk)

    assert agent._load_json(config_dir() / "config.json")["hooks"] == {"auto_deny_tools": ["rm"]}


def test_sandbox_resource_limits_read_the_endorsed_copy(_enforcing) -> None:
    loader = _enforcing
    _endorse_then_tamper(
        loader, {"resource_limits": {"memory_max_mb": 512}}, {"resource_limits": {}}
    )

    assert loader._raw_config()["resource_limits"] == {"memory_max_mb": 512}


@pytest.mark.parametrize(
    "on_disk", [{"agent": {"apps_trusted": ["bar"]}}, None], ids=["tampered", "deleted"]
)
def test_uninstall_sees_the_endorsed_trust_grant_and_waits_for_the_review(
    _enforcing, on_disk
) -> None:
    """The grant the load enforces is the endorsed one, so uninstall must withdraw it;
    with config.json pending that save is refused, so the precondition says so before
    any destructive step, and nothing is written."""
    from kiro_crew.apps import manager_uninstall

    endorsed = {"agent": {"apps_trusted": ["foo", "bar"]}}
    _endorse_then_tamper(_enforcing, endorsed, on_disk)

    assert manager_uninstall.trust_grant_removal_blocked("foo") == lp.PENDING_SAVE_REFUSED
    with pytest.raises(RuntimeError, match="changed outside Kiro Crew"):
        manager_uninstall._drop_trust_grant("foo")
    assert json.loads(lp.endorsed_bytes("config.json") or b"{}") == endorsed
    # An app with no grant needs no config write, so its uninstall is not held up.
    assert manager_uninstall.trust_grant_removal_blocked("baz") is None


def test_a_channel_save_starts_from_the_endorsed_copy(_enforcing) -> None:
    from kiro_crew.dashboard.handlers import weixin_qr

    _endorse_then_tamper(_enforcing, {"agent": {"sandbox": "auto"}}, {"agent": {"sandbox": "off"}})

    _path, staged = weixin_qr._stage_weixin_config(account_id="acct", base_url="")

    assert staged["agent"] == {"sandbox": "auto"}
    assert staged["weixin"]["account_id"] == "acct"


@pytest.mark.parametrize(
    ("segment", "secret"),
    [
        ("bot_token", True),
        ("clientSecret", True),
        ("PASSWORD", True),
        ("credentials", True),
        ("apiKey", True),
        ("api_key", True),
        ("api-key", True),
        ("private_key", True),
        ("oauth", True),
        ("Cookie", True),
        ("bearer", True),
        ("signing_secret", True),
        ("ssh_key", True),
        ("deploy-KEY", True),
        ("key", True),
        ("keyring", True),
        ("env", True),
        ("strands_env", True),
        ("header", True),
        ("Headers", True),
        ("env\n", True),
        ("\u017fecret", True),
        ("\u0130nfo_auth", True),
        ("auth\u0131", True),
        ("api.key", False),
        ("hotkey", False),
        ("monkey", False),
        ("environment", False),
        ("envs", False),
        ("headless", False),
        ("env\n\n", False),
        ("sandbox", False),
        ("model", False),
        ("", False),
    ],
)
def test_secret_segment_matching(segment: str, secret: bool) -> None:
    assert lp._secret_path(["x", segment]) is secret

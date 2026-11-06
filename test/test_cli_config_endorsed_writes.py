"""Kiro Crew's own writes of config.json take effect at once and are never pending.

Where the namespace sandbox seals the keystone files, a config.json that differs from
its endorsed copy loads as the endorsed copy and is held as pending
(``config.loosening_provenance``). So every Kiro Crew writer must publish through the
endorsing config writer: ``kirocrew config set`` (base and ``--local``)
and the Weixin dashboard savers. Each test here turns enforcement on, starts from an
endorsed file, writes through one of those paths, and checks the next load sees the
value with nothing pending.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def enforcing(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)


@pytest.fixture
def endorsed_config() -> Path:
    """config.json and an identical endorsed copy: in force, nothing pending."""
    path = config_dir() / "config.json"
    content = (json.dumps({"agent": {"bot_name": "Kiro"}, "keep": {"a": 1}}) + "\n").encode()
    lp.record(lp.LEAF_CONFIG, content, strict=True)
    path.write_bytes(content)
    return path


def _bot_name() -> str:
    return KiroCrewConfig.load().agent.bot_name


def _config_cmd(args: argparse.Namespace) -> None:
    from kiro_crew.cli_config import _config_cmd as run

    with patch("kiro_crew.cli_config.sel"):
        run(args)


@pytest.mark.parametrize("local", [False, True])
def test_config_set_takes_effect_at_once_and_is_not_pending(endorsed_config, local):
    _config_cmd(
        argparse.Namespace(
            config_action="set", key="agent.bot_name", value="Nova", file=None, local=local
        )
    )

    assert lp.pending_changes() == []
    assert _bot_name() == "Nova"


def test_weixin_config_save_takes_effect_at_once_and_is_not_pending(
    endorsed_config, _floor_monkeypatch
):
    from kiro_crew.dashboard.handlers import weixin_qr as qr

    _floor_monkeypatch.setattr(qr, "is_direct_local_request", lambda request: True)
    _floor_monkeypatch.setattr(qr, "_hot_apply_after_write", AsyncMock())
    request: Any = SimpleNamespace(json=AsyncMock(return_value={"dm_policy": "allowlist"}), app={})

    response = asyncio.run(qr.weixin_config_save(request))

    assert response.status == 200
    assert lp.pending_changes() == []
    assert KiroCrewConfig.load().weixin.dm_policy == "allowlist"


def test_weixin_sign_in_takes_effect_at_once_and_is_not_pending(
    endorsed_config, _floor_monkeypatch
):
    from kiro_crew.dashboard.handlers import weixin_qr as qr

    _floor_monkeypatch.setattr(qr, "apply_env_credential_changes", lambda *a, **k: None)
    cp, staged = qr._stage_weixin_config(account_id="acct1@im.bot", base_url="https://x")

    qr._commit_credential_and_config(cp, staged, "brand-new")

    assert lp.pending_changes() == []
    assert KiroCrewConfig.load().weixin.account_id == "acct1@im.bot"

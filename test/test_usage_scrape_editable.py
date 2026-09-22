"""``dashboard.usage_text_scrape_enabled`` is not a setting, and a config that still
carries it loads cleanly.

The ``/usage`` text scrape is the credit pill's automatic fallback: a kiro-cli slash
command answered locally from the free ``GetUsageLimits`` call. Nothing gates it,
so there is no key to declare, edit or document. Two things are pinned:

* THE KEY IS GONE everywhere a setting is declared -- the ``DashboardConfig``
  schema, the config PATCH allowlist and the schema registry -- so no surface can
  offer or accept it again by accident.
* LEGACY TOLERANCE -- a ``config.json`` written while the key existed (by
  ``kirocrew config set`` or the config PATCH) must load without error. The loader
  keeps an unmodelled nested key in ``_extra_keys`` and round-trips it on save, so
  the operator's file is neither rejected nor silently rewritten; it warns about
  nothing, because a nested unknown key is preserved rather than reported.
"""

from __future__ import annotations

import json
import logging

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.config.sections import DashboardConfig
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "dashboard.usage_text_scrape_enabled"
LEAF = "usage_text_scrape_enabled"


def test_the_key_is_neither_editable_nor_in_the_schema():
    assert FIELD not in _EDITABLE_CONFIG
    assert LEAF not in DashboardConfig.__dataclass_fields__
    assert FIELD not in {e.path for e in SCHEMA_REGISTRY}


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    """Point every config path at a temp home and return the config.json path."""
    cfgp = tmp_path / "config.json"
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")
    return cfgp


def test_a_config_still_carrying_the_key_loads_and_keeps_it(cfg_home, caplog):
    cfg_home.write_text(
        json.dumps({"dashboard": {LEAF: True, "link_previews": True}}), encoding="utf-8"
    )
    caplog.set_level(logging.WARNING)

    cfg = KiroCrewConfig.load()

    # Loaded, the modelled sibling intact, the stale key parked with the other
    # unmodelled keys rather than read into the dataclass.
    assert cfg.dashboard.link_previews is True
    assert not hasattr(cfg.dashboard, LEAF)
    assert cfg._extra_keys.get("dashboard") == {LEAF: True}
    # At most one line about it, and in fact none: preserved keys are not warned about.
    mentions = [r for r in caplog.records if LEAF in r.getMessage()]
    assert len(mentions) <= 1, [r.getMessage() for r in mentions]

    # A save of anything else round-trips the operator's file as written.
    cfg.timezone = "UTC"
    cfg.save()
    after = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert after["dashboard"][LEAF] is True
    assert after["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_the_config_patch_refuses_the_key(cfg_home) -> None:
    """The dashboard's write path answers "field not editable", like any unknown path."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    cfg_home.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    async with TestClient(TestServer(app)) as c:
        resp = await c.patch("/api/config/kirocrew", json={"path": FIELD, "value": True})
        assert resp.status == 400, await resp.text()
    written = json.loads(cfg_home.read_text(encoding="utf-8"))
    assert LEAF not in written["dashboard"]

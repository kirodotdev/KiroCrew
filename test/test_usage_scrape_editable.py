"""``dashboard.usage_text_scrape_enabled`` is writable from the dashboard, and stays off.

The Settings > Display toggle writes this field over ``PATCH /api/config/kirocrew``,
so it has to be in ``_EDITABLE_CONFIG`` at all -- before this it was absent and
every save came back "field not editable", which is why the only way to opt in was
to know the key name and edit ``config.json`` by hand.

The two halves pinned here are deliberately different in kind:

* REACHABILITY -- the path is in the allowlist, and it is the same path the reader
  resolves. Membership alone would pass for a typo, so the field name is also
  checked against ``DashboardConfig``'s declared fields.
* THE DEFAULT IS UNCHANGED -- the setting gates a REAL billed LLM turn, so making
  it reachable must not make it active. A config that has never carried the key
  still reads ``False``.

The ``bool`` spec is load-bearing rather than cosmetic: the handler's ``bool``
branch rejects a non-boolean outright, whereas a ``str`` spec would accept the
string ``"false"`` and store a value that is truthy everywhere it is read.
"""

import json
from unittest.mock import patch

import pytest

from kiro_crew.config.sections import DashboardConfig
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "dashboard.usage_text_scrape_enabled"


def test_the_scrape_gate_is_editable_from_the_dashboard():
    assert FIELD in _EDITABLE_CONFIG, f"{FIELD} must be PATCH-able or the toggle cannot save"


def test_a_path_that_is_not_in_the_allowlist_is_absent():
    """Control for the assertion above: membership is a test that can fail.

    Without this, a typo in ``FIELD`` would make the previous test pass by
    asserting nothing -- the same shape as a regex pinned false by construction.
    """
    assert "dashboard.usage_text_scrape" not in _EDITABLE_CONFIG
    assert "dashboard.usage_text_scrape_enabled_typo" not in _EDITABLE_CONFIG


def test_the_allowlist_path_resolves_to_a_declared_field():
    """The allowlist and the reader must name the SAME field.

    ``handlers/sessions._text_scrape_enabled`` reads
    ``KiroCrewConfig.load().dashboard.usage_text_scrape_enabled``. An allowlist
    entry whose leaf does not exist on ``DashboardConfig`` would accept a write
    that no reader ever consults -- a setting that saves and does nothing.
    """
    section, _, leaf = FIELD.partition(".")
    assert section == "dashboard"
    assert leaf in DashboardConfig.__dataclass_fields__


def test_the_spec_is_bool_so_a_string_cannot_be_stored():
    """A ``str`` spec would accept ``"false"`` -- truthy at every read site."""
    assert _EDITABLE_CONFIG[FIELD]["type"] == "bool"


def test_making_it_reachable_does_not_turn_it_on():
    """The billed fallback stays opt-in; the toggle only makes the opt-in findable."""
    assert DashboardConfig().usage_text_scrape_enabled is False
    assert DashboardConfig.__dataclass_fields__["usage_text_scrape_enabled"].default is False


# ── Enabling is owner-only; disabling is not ────────────────────────────────
#
# Reachability alone would be the wrong bar for this one field. Every other path
# in the allowlist is a preference, but enabling this one starts REAL billed
# ``kiro-cli /usage`` turns that repeat every refresh interval, and a dashboard
# token does not imply ownership -- an allow-listed messaging user holds one. So
# the write is split: the ENABLE needs the owner, the DISABLE never does, because
# someone who can see spend must always be able to stop it.


def _patch_app():
    """A bare app carrying only the PATCH route, as test_config_patch.py builds it."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def scrape_config(tmp_path):
    """Point the loader at a tmpfile through its own supported seam.

    ``config_path`` is the documented redirect; ``config_dir()`` ignores both
    ``HOME`` and ``KIROCREW_HOME``, so neither of those isolates anything.
    """
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dashboard": {}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg):
        yield cfg


def _owner(verdict: bool):
    """Pin the shared owner predicate the gate consults."""
    return patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=verdict,
    )


async def _patch_field(client, value):
    return await client.patch("/api/config/kirocrew", json={"path": FIELD, "value": value})


@pytest.mark.asyncio
async def test_the_owner_may_enable_the_billed_fallback(scrape_config) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_patch_app())) as c:
        with _owner(True):
            resp = await _patch_field(c, True)
        assert resp.status == 200, await resp.text()
    written = json.loads(scrape_config.read_text(encoding="utf-8"))
    assert written["dashboard"]["usage_text_scrape_enabled"] is True


@pytest.mark.asyncio
async def test_a_non_owner_cannot_enable_the_billed_fallback(scrape_config) -> None:
    """The finding this gate answers: a dashboard token is not ownership."""
    from aiohttp.test_utils import TestClient, TestServer

    async with TestClient(TestServer(_patch_app())) as c:
        with _owner(False):
            resp = await _patch_field(c, True)
        assert resp.status == 403, await resp.text()
        assert (await resp.json())["code"] == "owner_only"
    # And nothing was written: a refused enable must not bill later anyway.
    written = json.loads(scrape_config.read_text(encoding="utf-8"))
    assert "usage_text_scrape_enabled" not in written["dashboard"]


@pytest.mark.asyncio
async def test_a_non_owner_may_still_switch_the_billing_off(scrape_config) -> None:
    """Stopping spend is never gated: the narrower choice always composes."""
    from aiohttp.test_utils import TestClient, TestServer

    scrape_config.write_text(
        json.dumps({"dashboard": {"usage_text_scrape_enabled": True}}), encoding="utf-8"
    )
    async with TestClient(TestServer(_patch_app())) as c:
        with _owner(False):
            resp = await _patch_field(c, False)
        assert resp.status == 200, await resp.text()
    written = json.loads(scrape_config.read_text(encoding="utf-8"))
    assert written["dashboard"]["usage_text_scrape_enabled"] is False

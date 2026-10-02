"""Tests for /api/theme/boot and /api/config/theme endpoints."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard.handlers import core as core_mod


def _make_cfg(
    theme_mode: str = "",
    theme_color: str = "",
    onboarded: bool = False,
    import_onboarded: bool = False,
    language: str = "",
    privacy_acked: bool = False,
    crewmates_onboarded: bool = False,
):
    """Build a mock KiroCrewConfig with dashboard display fields.

    Every field the payload builder reads must be set explicitly: a bare
    ``MagicMock`` attribute serializes as a mock, so a field added to
    ``_theme_payload`` without a line here fails as a JSON TypeError rather than a
    readable assertion.
    """
    cfg = MagicMock()
    cfg.dashboard.theme_mode = theme_mode
    cfg.dashboard.theme_color = theme_color
    cfg.dashboard.onboarded = onboarded
    cfg.dashboard.import_onboarded = import_onboarded
    cfg.dashboard.language = language
    cfg.dashboard.privacy_acked = privacy_acked
    cfg.dashboard.crewmates_onboarded = crewmates_onboarded
    return cfg


def _owner_request() -> MagicMock:
    request = MagicMock(spec=web.Request)
    state = MagicMock()
    state.owner_id = ""
    request.app = {"state": state}
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    return request


@pytest.mark.asyncio
async def test_theme_boot_returns_defaults() -> None:
    """GET /api/theme/boot returns empty defaults when unconfigured."""
    cfg = _make_cfg()
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {
        "mode": "",
        "color": "",
        "language": "",
        "onboarded": False,
        "import_onboarded": False,
        "privacy_acked": False,
        "crewmates_onboarded": False,
    }


@pytest.mark.asyncio
async def test_theme_boot_returns_configured_values() -> None:
    """GET /api/theme/boot returns workspace config values."""
    cfg = _make_cfg(
        theme_mode="dark",
        theme_color="kiro",
        onboarded=True,
        import_onboarded=True,
    )
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    body = json.loads(resp.body)
    assert body == {
        "mode": "dark",
        "color": "kiro",
        "language": "",
        "onboarded": True,
        "import_onboarded": True,
        "privacy_acked": False,
        "crewmates_onboarded": False,
    }


@pytest.mark.asyncio
async def test_theme_config_get() -> None:
    """GET /api/config/theme returns current theme settings."""
    cfg = _make_cfg(
        theme_mode="light",
        theme_color="emerald",
        onboarded=True,
        import_onboarded=True,
    )
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        req.method = "GET"
        resp = await core_mod.api_theme_config(req)
    body = json.loads(resp.body)
    assert body == {
        "mode": "light",
        "color": "emerald",
        "language": "",
        "onboarded": True,
        "import_onboarded": True,
        "privacy_acked": False,
        "crewmates_onboarded": False,
    }


# ── PUT: a locked delta write of the named dashboard keys ────────────────────
#
# A whole-document ``load()`` + ``save()`` here would publish pure defaults over
# the real file whenever config.json is momentarily unreadable -- every setting
# gone, no backup -- from a PUT the SPA fires on its own at boot. These tests run against a real file in
# the per-test data home, so what they pin is the bytes on disk.


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _invalidate_config_cache()
    return tmp_path / "config.json"


#: A populated config: sections the theme PUT does not own, plus values the
#: validated loader would drop or rewrite if they were round-tripped through the
#: dataclass (a schema-invalid type, an unmodelled key inside a section).
_POPULATED = {
    "agent": {"approval_mode": "interactive", "max_subagents": "lots", "future_knob": 3},
    "agents": {
        "default": {"kiro_agent": "kirocrew", "workspace": "default", "memory_store": "default"},
        "writer": {"kiro_agent": "kirocrew", "workspace": "default", "memory_store": "default"},
    },
    "default_agent": "default",
    "dashboard": {"theme_mode": "light", "language": "zh-CN", "default_memory_mode": "bogus"},
    "session": {"timeout_secs": 7200},
    "timezone": "Asia/Shanghai",
    "auto_update": True,
}


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def _put(body: object) -> web.Response:
    req = _owner_request()
    req.method = "PUT"
    req.json = AsyncMock(return_value=body)
    return await core_mod.api_theme_config(req)


@pytest.mark.asyncio
async def test_theme_config_put_updates_and_persists(cfg_file: Path) -> None:
    """PUT persists the requested keys and answers with the stored values."""
    _write(cfg_file, {"timezone": "UTC"})
    resp = await _put(
        {"mode": "dark", "color": "monokai", "onboarded": True, "import_onboarded": True}
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "mode": "dark",
        "color": "monokai",
        "language": "",
        "onboarded": True,
        "import_onboarded": True,
        "privacy_acked": True,  # falls back to `onboarded` when never written
        "crewmates_onboarded": False,
    }
    dashboard = _read(cfg_file)["dashboard"]
    assert dashboard["theme_mode"] == "dark"
    assert dashboard["theme_color"] == "monokai"
    assert dashboard["onboarded"] is True
    assert dashboard["import_onboarded"] is True


@pytest.mark.asyncio
async def test_theme_config_put_writes_only_the_dashboard_keys_it_names(cfg_file: Path) -> None:
    """Every other value in the file survives exactly as written.

    A whole-document save re-emits the validated dataclass: a schema-invalid
    ``max_subagents`` is dropped, an invalid ``default_memory_mode`` is
    rewritten, and every modelled default is materialized. The delta write
    touches nothing it was not asked to.
    """
    _write(cfg_file, _POPULATED)
    resp = await _put({"mode": "dark"})
    assert resp.status == 200
    after = _read(cfg_file)
    after.pop("meta", None)
    expected = json.loads(json.dumps(_POPULATED))
    expected["dashboard"]["theme_mode"] = "dark"
    assert after == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"timezone": "Asia/Shanghai", "dashboard": {"theme_mode": "light"',
        b"",
        b"[1, 2]",
    ],
    ids=["truncated", "empty", "not-an-object"],
)
async def test_an_unreadable_config_is_refused_and_left_byte_identical(
    cfg_file: Path, raw: bytes
) -> None:
    """The reported data loss: a defaults-only load must never be published.

    Each of these makes ``load()`` answer pure defaults (``onboarded`` false),
    which is exactly what triggers the SPA's automatic boot-time PUT.
    """
    cfg_file.write_bytes(raw)
    resp = await _put({"mode": "dark", "onboarded": True})
    assert resp.status == 500
    assert json.loads(resp.body)["code"] == "config_unreadable"
    assert cfg_file.read_bytes() == raw


@pytest.mark.asyncio
async def test_theme_config_put_persists_privacy_acked(cfg_file: Path) -> None:
    """The route must persist the flag the gateway's first-heartbeat gate reads.

    Without this write the beacon stays withheld forever on a fresh install: the
    first-run chapter is the only thing that sets it, and the gateway cannot see
    the browser's localStorage copy.
    """
    _write(cfg_file, {})
    resp = await _put({"privacy_acked": True})
    assert resp.status == 200
    assert _read(cfg_file)["dashboard"]["privacy_acked"] is True
    assert json.loads(resp.body)["privacy_acked"] is True


@pytest.mark.asyncio
async def test_theme_config_put_persists_crewmates_onboarded(cfg_file: Path) -> None:
    """The route must persist the Meet CrewMates first-run flag.

    The flow is gated server-side so a second machine does not replay it; the
    browser's localStorage mirror is only a render cache the gateway cannot see.
    """
    _write(cfg_file, {})
    resp = await _put({"crewmates_onboarded": True})
    assert resp.status == 200
    assert _read(cfg_file)["dashboard"]["crewmates_onboarded"] is True
    assert json.loads(resp.body)["crewmates_onboarded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"mode": "invalid"},
        # ``bool("false")`` is True: coercing instead of rejecting would persist
        # the inverse of what the client asked for.
        {"onboarded": "false"},
        {"onboarded": 1},
        {"import_onboarded": "false"},
        {"privacy_acked": "true"},
        {"crewmates_onboarded": "true"},
        # A valid field ahead of an invalid one: the 400 must apply neither.
        {"color": "monokai", "privacy_acked": "true"},
    ],
    ids=[
        "mode",
        "onboarded",
        "onboarded_int",
        "import_onboarded",
        "privacy_acked",
        "crewmates_onboarded",
        "mixed",
    ],
)
async def test_an_invalid_field_is_a_400_that_writes_nothing(cfg_file: Path, body: dict) -> None:
    _write(cfg_file, {"timezone": "UTC"})
    before = cfg_file.read_bytes()
    with pytest.raises(web.HTTPBadRequest):
        await _put(body)
    assert cfg_file.read_bytes() == before


@pytest.mark.asyncio
async def test_theme_config_put_no_change_no_write(cfg_file: Path) -> None:
    """Re-sending the stored values leaves the file untouched (no meta restamp).

    Seeded from ``_POPULATED`` so the response's own ``load()`` has no
    default-agent migration to write back, which would mask the assertion.
    """
    doc = json.loads(json.dumps(_POPULATED))
    doc["dashboard"].update({"theme_mode": "dark", "theme_color": "kiro", "onboarded": True})
    _write(cfg_file, doc)
    before = cfg_file.read_bytes()
    resp = await _put({"mode": "dark", "color": "kiro", "onboarded": True})
    assert resp.status == 200
    assert cfg_file.read_bytes() == before


@pytest.mark.asyncio
async def test_theme_config_put_rejects_non_object_body() -> None:
    """PUT /api/config/theme rejects arrays instead of raising during key access."""
    with pytest.raises(web.HTTPBadRequest):
        await _put(["import_onboarded"])


@pytest.mark.asyncio
async def test_concurrent_puts_keep_both_writers_keys(cfg_file: Path) -> None:
    """Two PUTs in flight together both land: each is its own locked delta."""
    _write(cfg_file, {"timezone": "UTC"})
    json_waiters = 0
    both_parsed = asyncio.Event()

    async def body(value: dict[str, object]) -> dict[str, object]:
        nonlocal json_waiters
        json_waiters += 1
        if json_waiters == 2:
            both_parsed.set()
        await both_parsed.wait()
        return value

    first = _owner_request()
    first.method = "PUT"
    first.json = lambda: body({"mode": "dark"})
    second = _owner_request()
    second.method = "PUT"
    second.json = lambda: body({"import_onboarded": True})
    await asyncio.gather(core_mod.api_theme_config(first), core_mod.api_theme_config(second))

    dashboard = _read(cfg_file)["dashboard"]
    assert dashboard["theme_mode"] == "dark"
    assert dashboard["import_onboarded"] is True


@pytest.mark.asyncio
async def test_the_put_never_publishes_a_whole_document_save(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural half of the fix, pinned at runtime: no ``save()`` on this path."""
    _write(cfg_file, {})

    def _refuse(self) -> None:
        raise AssertionError("api_theme_config called KiroCrewConfig.save()")

    monkeypatch.setattr(KiroCrewConfig, "save", _refuse)
    resp = await _put({"mode": "dark", "language": "fr"})
    assert resp.status == 200


# ── UI language (dashboard.language) ──────────────────────────────────────────
#
# The language field rides on the EXISTING theme endpoints rather than a new
# pair, so these tests cover the field's own validation and the round trip. The
# empty string is a first-class value ("follow the browser"), not a missing
# value, so clearing a choice must be writable.


@pytest.mark.asyncio
async def test_theme_boot_exposes_language() -> None:
    """GET /api/theme/boot surfaces the configured UI language.

    Boot is unauthenticated, so the SPA can pick the right language before the
    token flow completes -- this is what prevents an English flash on load.
    """
    cfg = _make_cfg(language="zh-CN")
    with patch.object(core_mod, "KiroCrewConfig") as mock_cls:
        mock_cls.load.return_value = cfg
        req = MagicMock(spec=web.Request)
        resp = await core_mod.api_theme_boot(req)
    assert json.loads(resp.body)["language"] == "zh-CN"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["en", "zh-CN", "pt-BR", "zh-Hans-CN", "fr"])
async def test_theme_config_put_accepts_valid_language_tags(cfg_file: Path, tag: str) -> None:
    """A well-formed BCP-47 tag is accepted, including ones with no catalog.

    Shape is validated, not membership: keeping the shipped-language list a pure
    frontend concern means adding a language never needs a backend change.
    """
    _write(cfg_file, {})
    resp = await _put({"language": tag})
    assert resp.status == 200
    assert json.loads(resp.body)["language"] == tag
    assert _read(cfg_file)["dashboard"]["language"] == tag


@pytest.mark.asyncio
async def test_theme_config_put_clears_language_to_auto(cfg_file: Path) -> None:
    """Writing '' clears the stored choice back to browser auto-detect."""
    _write(cfg_file, {"dashboard": {"language": "zh-CN"}})
    resp = await _put({"language": ""})
    assert resp.status == 200
    assert json.loads(resp.body)["language"] == ""
    assert _read(cfg_file)["dashboard"]["language"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "e",  # too short
        "english-language-name",  # subtag over 8 chars
        "en_US",  # underscore is not BCP-47
        "en-US-x-toolong-extra",  # too many subtags
        "../../etc/passwd",  # path traversal shape
        "<script>",  # markup
        "zh CN",  # whitespace
    ],
)
async def test_theme_config_put_rejects_malformed_language(cfg_file: Path, bad: str) -> None:
    """A malformed tag is a 400 and never reaches the config file."""
    _write(cfg_file, {"dashboard": {"language": "zh-CN"}})
    before = cfg_file.read_bytes()
    with pytest.raises(web.HTTPBadRequest):
        await _put({"language": bad})
    assert cfg_file.read_bytes() == before


@pytest.mark.asyncio
async def test_theme_config_put_rejects_non_string_language(cfg_file: Path) -> None:
    """A non-string language is a 400, not a coerced value."""
    _write(cfg_file, {"dashboard": {"language": "zh-CN"}})
    before = cfg_file.read_bytes()
    with pytest.raises(web.HTTPBadRequest):
        await _put({"language": ["zh-CN"]})
    assert cfg_file.read_bytes() == before


@pytest.mark.asyncio
async def test_theme_config_put_omitting_language_leaves_it_untouched(cfg_file: Path) -> None:
    """A PUT that doesn't mention language must not reset it.

    The frontend patches single fields, so an unrelated theme write must never
    clobber the user's language choice.
    """
    _write(cfg_file, {"dashboard": {"language": "zh-CN"}})
    resp = await _put({"color": "monokai"})
    assert json.loads(resp.body)["language"] == "zh-CN"
    assert _read(cfg_file)["dashboard"]["language"] == "zh-CN"

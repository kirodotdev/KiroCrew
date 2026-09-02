"""Hardened-reader migration for the two remaining raw agent-spec reads (#6726).

``agent_discovery.agent_skill_globs`` and ``dashboard.handlers.hooks.api_kiro_hooks``
both read files from the user-writable, tool-shared kiro agents directory. Both
now route through ``agent_discovery._read_agent_spec`` — the single hardened
reader (#5423) — instead of hand-rolling a subset of its screens.

Differentially RED against the pre-change code:

- ``test_oversized_spec_widens_to_no_mapping`` — old code had no size cap and
  returned the oversized spec's globs.
- ``test_symlink_loop_is_skipped_not_raised`` — old code caught only ``OSError``
  on resolve; pathlib signals a symlink loop with ``RuntimeError`` (< 3.13),
  which escaped and violated the function's own never-raises docstring.
- ``test_sensitive_symlink_target_skipped_and_sel_denied`` — old code skipped
  the file but emitted no SEL ``denied`` event.
- ``test_oversized_kirocrew_json_degrades_to_empty`` — old handler had no size
  cap and served the oversized file's hooks.
- ``test_non_utf8_kirocrew_json_degrades_to_empty`` — old handler decoded with
  the platform default and caught only ``(OSError, JSONDecodeError)``, so a
  ``UnicodeDecodeError`` escaped as an unhandled 500 where the default codec is
  strict (UTF-8 platforms).
- ``test_non_dict_hooks_value_degrades_to_empty`` — old handler passed a
  non-dict ``hooks`` value straight to ``hooks.items()``, an ``AttributeError``
  500.

The rest are regression guards pinning behavior that must NOT change: the
AppleDouble skip, normal specs' globs, the symlinked-spec glob anchor (the
reader resolves internally but expansion must keep seeing the ORIGINAL path),
normal hooks serving, and bundled-source tagging.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.agent_discovery as ad
import kiro_crew.hooks as kc_hooks
from kiro_crew.agent_discovery import agent_skill_globs
from kiro_crew.dashboard.handlers.hooks import api_kiro_hooks

# Same patch targets as test_api_kiro_hooks.py: ``kiro_agents_dir_path()`` reads
# ``KIRO_AGENTS_DIR`` from ``agent``'s globals at call time, and the handler
# imports ``_shipped_defaults`` at module scope, so it is patched in the
# handler's namespace.
_P_AGENTS_DIR = "kiro_crew.agent.KIRO_AGENTS_DIR"
_P_DEFAULTS = "kiro_crew.dashboard.handlers.hooks._shipped_defaults"


def _agents_dir(tmp_path: Path) -> Path:
    """An agents dir nested like the real one (``<root>/.kiro/agents``).

    The two extra levels matter for the glob-anchor tests: a relative
    ``skill://`` URI expands against ``agent_path.parent.parent.parent``.
    """
    d = tmp_path / "root" / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


def _spec(name: str, *uris: str) -> str:
    return json.dumps({"name": name, "resources": list(uris)})


def _symlink_or_skip(target: Path | str, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")


class TestAgentSkillGlobsHardenedRead:
    def test_normal_spec_globs_unchanged(self, tmp_path: Path) -> None:
        """A plain readable spec keeps returning its expanded globs."""
        d = _agents_dir(tmp_path)
        (d / "mapped.json").write_text(
            _spec("mapped", "skill://skills/foo/SKILL.md", "file://.kiro/steering/**/*.md"),
            encoding="utf-8",
        )
        assert agent_skill_globs("mapped", agents_dir=d) == [
            str(tmp_path / "root" / "skills" / "foo" / "SKILL.md")
        ]

    def test_oversized_spec_widens_to_no_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-cap spec is refused, so the agent falls back to ``[]``.

        Documented consequence of the migration (#6726): the old code had no
        size cap and would have parsed this spec and returned its globs; the
        hardened reader refuses it and ``[]`` means "no explicit mapping", so
        the effect depends on the caller. The skills LISTING
        (``prompts.py``) drops its agent filter — scope WIDENS to the legacy
        all-or-nothing default, the same direction any malformed spec already
        took. The INJECTION plan (``context._skills_injection_plan``) returns
        ``not is_custom`` for empty globs, so a CUSTOM agent's refused spec
        drops skill injection to none — fail-closed on that axis.
        """
        monkeypatch.setattr(kc_hooks, "MAX_FILE_BYTES", 256)
        d = _agents_dir(tmp_path)
        body = json.dumps(
            {
                "name": "big",
                "resources": ["skill://skills/foo/SKILL.md"],
                "pad": "x" * 512,
            }
        )
        assert len(body.encode("utf-8")) > 256
        (d / "big.json").write_text(body, encoding="utf-8")
        assert agent_skill_globs("big", agents_dir=d) == []

    def test_appledouble_sidecar_is_skipped(self, tmp_path: Path) -> None:
        """A ``._`` sidecar never contributes a mapping, even with a matching name."""
        d = _agents_dir(tmp_path)
        (d / "._mapped.json").write_text(
            _spec("mapped", "skill://skills/foo/SKILL.md"), encoding="utf-8"
        )
        assert agent_skill_globs("mapped", agents_dir=d) == []

    def test_symlink_loop_is_skipped_not_raised(self, tmp_path: Path) -> None:
        """A self-referential symlink is skipped, honoring the never-raises contract.

        Pre-migration this RAISED on Python < 3.13: ``resolve(strict=True)``
        signals a symlink loop with ``RuntimeError``, and the inline screen
        caught only ``OSError``.
        """
        d = _agents_dir(tmp_path)
        _symlink_or_skip("loop.json", d / "loop.json")
        assert agent_skill_globs("loop", agents_dir=d) == []

    def test_sensitive_symlink_target_skipped_and_sel_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spec resolving into a sensitive target is skipped AND SEL-audited."""
        d = _agents_dir(tmp_path)
        secret = tmp_path / "creds"
        secret.write_text(_spec("evil", "skill://skills/foo/SKILL.md"), encoding="utf-8")
        _symlink_or_skip(secret, d / "evil.json")
        resolved = str(secret.resolve())
        monkeypatch.setattr(
            "kiro_crew.agent_discovery.is_sensitive_path", lambda p: str(p) == resolved
        )
        sel_events: list[dict] = []
        monkeypatch.setattr(
            ad, "_sel", lambda: SimpleNamespace(log_api_access=lambda **kw: sel_events.append(kw))
        )
        assert agent_skill_globs("evil", agents_dir=d) == []
        assert (
            sel_events and sel_events[0]["outcome"] == "denied"
        ), f"sensitive-target rejection must emit a SEL denial: {sel_events}"

    def test_symlinked_spec_globs_anchor_at_symlink(self, tmp_path: Path) -> None:
        """Glob expansion sees the ORIGINAL path, not the resolved target.

        The reader resolves internally, but ``f.stem`` (name fallback) and
        ``expand_skill_uri(uri, f)`` must keep operating on the symlink's own
        location: anchoring at the resolved target would silently relocate a
        symlinked spec's relative skill globs.
        """
        d = _agents_dir(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real = elsewhere / "real.json"
        real.write_text(_spec("other-name", "skill://skills/foo/SKILL.md"), encoding="utf-8")
        _symlink_or_skip(real, d / "linked.json")
        # Matched via ``f.stem`` ("linked"), and anchored three levels above the
        # SYMLINK (<root>), not above the real file (<elsewhere>).
        assert agent_skill_globs("linked", agents_dir=d) == [
            str(tmp_path / "root" / "skills" / "foo" / "SKILL.md")
        ]


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/kiro-hooks", api_kiro_hooks)
    return app


async def _get_hooks(kiro_dir: Path, defaults: Path) -> tuple[int, dict]:
    with patch(_P_AGENTS_DIR, kiro_dir), patch(_P_DEFAULTS, return_value=defaults):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/kiro-hooks")
            return resp.status, await resp.json()


class TestApiKiroHooksHardenedRead:
    @pytest.fixture
    def kiro_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "agents"
        d.mkdir(parents=True)
        return d

    @pytest.fixture
    def defaults(self, tmp_path: Path) -> Path:
        p = tmp_path / "defaults.json"
        p.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        return p

    @pytest.mark.asyncio
    async def test_oversized_kirocrew_json_degrades_to_empty(
        self, kiro_dir: Path, defaults: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An over-cap ``kirocrew.json`` yields no user hooks — 200, not 500."""
        monkeypatch.setattr(kc_hooks, "MAX_FILE_BYTES", 256)
        body = json.dumps(
            {
                "hooks": {"preToolUse": [{"command": "echo hi", "matcher": ""}]},
                "pad": "x" * 512,
            }
        )
        assert len(body.encode("utf-8")) > 256
        (kiro_dir / "kirocrew.json").write_text(body, encoding="utf-8")
        status, payload = await _get_hooks(kiro_dir, defaults)
        assert status == 200
        assert payload == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_non_utf8_kirocrew_json_degrades_to_empty(
        self, kiro_dir: Path, defaults: Path
    ) -> None:
        """Non-UTF-8 bytes are refused deterministically instead of decoded with
        the platform default (previously an unhandled 500 on UTF-8 platforms)."""
        (kiro_dir / "kirocrew.json").write_bytes(b'\xff\xfe{"hooks": {}}')
        status, payload = await _get_hooks(kiro_dir, defaults)
        assert status == 200
        assert payload == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_non_dict_hooks_value_degrades_to_empty(
        self, kiro_dir: Path, defaults: Path
    ) -> None:
        """A top-level object whose ``hooks`` value is not a dict yields no user
        hooks — 200, not an ``AttributeError`` 500 from ``hooks.items()``."""
        (kiro_dir / "kirocrew.json").write_text(json.dumps({"hooks": []}), encoding="utf-8")
        status, payload = await _get_hooks(kiro_dir, defaults)
        assert status == 200
        assert payload == {"hooks": {}}

    @pytest.mark.asyncio
    async def test_normal_kirocrew_json_still_serves_hooks(
        self, kiro_dir: Path, defaults: Path
    ) -> None:
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps({"hooks": {"preToolUse": [{"command": "echo hi", "matcher": ""}]}}),
            encoding="utf-8",
        )
        status, payload = await _get_hooks(kiro_dir, defaults)
        assert status == 200
        assert payload["hooks"]["preToolUse"][0]["command"] == "echo hi"
        assert payload["hooks"]["preToolUse"][0]["source"] == "user"

    @pytest.mark.asyncio
    async def test_bundled_source_tagging_unaffected(self, kiro_dir: Path, tmp_path: Path) -> None:
        """The `_shipped_defaults()` read is deliberately NOT migrated (it reads
        a file shipped inside the package, not the user-writable agents dir), so
        source tagging keeps working exactly as before."""
        entry = {"command": "echo bundled", "matcher": ""}
        (kiro_dir / "kirocrew.json").write_text(
            json.dumps({"hooks": {"preToolUse": [entry]}}), encoding="utf-8"
        )
        defaults = tmp_path / "defaults.json"
        defaults.write_text(json.dumps({"hooks": {"preToolUse": [entry]}}), encoding="utf-8")
        status, payload = await _get_hooks(kiro_dir, defaults)
        assert status == 200
        assert payload["hooks"]["preToolUse"][0]["source"] == "bundled"

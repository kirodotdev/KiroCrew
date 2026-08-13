"""Tests for prompts (agent SOPs) discovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat import _expand_prompt_mention, _run_chat
from kiro_crew.dashboard.handlers import (
    _extract_sop_description,
    _list_aim_prompts,
    api_prompt_detail,
    api_prompts,
)

# ── Shared fixtures ──


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """All tests get an isolated $HOME and no project dir."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: None)
    # Clear prompt cache between tests
    import kiro_crew.dashboard.handlers as h
    h._prompt_cache = None
    h._prompt_cache_ts = 0


@pytest.fixture()
def aim_dir(tmp_path, monkeypatch):
    """Base dir whose child package dirs are exposed via the prompt_source_roots seam.

    Each child directory becomes one edition prompt root; SOPs placed under it
    (at any depth) are discovered by ``_list_aim_prompts`` via ``rglob('*.sop.md')``
    with ``package = <root.name>``.
    """
    base = tmp_path / "prompt_pkgs"
    base.mkdir()
    from kiro_crew.platform.defaults import DefaultPromptSourceProvider

    monkeypatch.setattr(
        DefaultPromptSourceProvider,
        "prompt_source_roots",
        lambda self: [d for d in sorted(base.iterdir()) if d.is_dir()],
    )
    return base


@pytest.fixture()
def mock_sel(monkeypatch):
    """Patch sel() in both chat and handlers modules."""
    m = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: m)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: m)
    return m


@pytest.fixture()
def block_sensitive(monkeypatch):
    """Make is_sensitive_path return True everywhere."""
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.is_sensitive_path", lambda p: True)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_sensitive_path", lambda p: True)
    monkeypatch.setattr("kiro_crew.hooks.is_sensitive_path", lambda p: True)


# ── Helpers ──


def _aim_pkg(base, pkg_name, event_id, sops):
    """Create a package root under *base* exposing SOP files.

    ``event_id`` is retained for call-site compatibility but unused — the seam
    model has no eventId layout; SOPs are placed directly under the package root
    and found via ``rglob('*.sop.md')``.
    """
    pkg_dir = base / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sops.items():
        (pkg_dir / f"{name}.sop.md").write_text(content)
    return pkg_dir


def _user_prompt(tmp_path, name, content="# Placeholder"):
    """Create a user prompt in ~/.kiro/prompts/."""
    d = tmp_path / ".kiro" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content)
    return p


def _api_request(name):
    r = MagicMock()
    r.match_info = {"name": name}
    return r


class _Slot:
    """Minimal slot/state stub for prompt tests."""

    def __init__(self):
        self.messages = []
        self.key = "t"
        self.agent = "kirocrew"
        self.model = None
        self._queue = []
        self._stop_generation = 0
        self.linked_session_key = ""

    def append(self, role, text, cls):
        self.messages.append((role, text, cls))


class _State:
    _hook_store = None
    _yolo = False

    def push_refresh(self, *a):
        pass

    def __init__(self):
        self.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        self.sessions = type('_MockSessions', (), {
            'get_slack_link': lambda self, k: ('', ''),
            'set_slack_link': lambda self, k, t, c: None,
            'get_or_create': None, 'get_pid': lambda self, k: None,
            'set_approval_policy': lambda self, k, v: None,
            'check_context_usage': lambda self, k, c: None,
        })()

    def push_slots_update(self):
        pass

    def broadcast_ws(self, *a, **kw):
        pass


def _ss():
    """Fresh state + slot pair."""
    return _State(), _Slot()


# ── _extract_sop_description ──


class TestExtractSopDescription:
    def _write(self, tmp_path, content, *, binary=False):
        p = tmp_path / "t.sop.md"
        p.write_bytes(content) if binary else p.write_text(content)
        return p

    def test_frontmatter(self, tmp_path):
        p = self._write(tmp_path, "---\nname: t\ndescription: My desc\n---\n# T\n")
        assert _extract_sop_description(p) == "My desc"

    def test_fallback_to_heading(self, tmp_path):
        p = self._write(tmp_path, "# My Heading\nContent.\n")
        assert _extract_sop_description(p) == "My Heading"

    def test_missing_file(self, tmp_path):
        assert _extract_sop_description(tmp_path / "nope.sop.md") == ""

    def test_empty_file(self, tmp_path):
        assert _extract_sop_description(self._write(tmp_path, "")) == ""

    def test_quoted_description(self, tmp_path):
        p = self._write(tmp_path, "---\nname: t\ndescription: 'Quoted'\n---\n")
        assert _extract_sop_description(p) == "Quoted"

    def test_invalid_utf8(self, tmp_path):
        p = self._write(tmp_path, b"---\nname: t\ndescription: \xff\xfe\n---\n", binary=True)
        assert _extract_sop_description(p) == ""


# ── _list_aim_prompts ──


class TestListAimPrompts:
    def test_discovers_sops(self, aim_dir):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {
            "my-sop": "---\nname: my-sop\ndescription: Test SOP\n---\n",
        })
        r = _list_aim_prompts()
        assert len(r) == 1
        assert (r[0]["name"], r[0]["fullName"], r[0]["source"]) == (
            "my-sop", "agent-sop:my-sop", "package")
        assert r[0]["description"] == "Test SOP"
        assert r[0]["package"] == "Pkg-1.0"

    def test_discovers_nested_sops(self, aim_dir):
        # rglob finds SOPs at any depth under a prompt root (e.g. agent-sops/).
        pkg = aim_dir / "Deep-1.0" / "agent-sops" / "sub"
        pkg.mkdir(parents=True)
        (pkg / "deep.sop.md").write_text("---\nname: deep\ndescription: D\n---\n")
        r = _list_aim_prompts()
        assert [p["name"] for p in r] == ["deep"]
        assert r[0]["package"] == "Deep-1.0"
        assert r[0]["source"] == "package"

    def test_discovers_user_prompts(self, tmp_path):
        _user_prompt(tmp_path, "my-prompt", "# P\nDo things.\n")
        r = _list_aim_prompts()
        assert len(r) == 1
        assert (r[0]["name"], r[0]["source"]) == ("my-prompt", "global")

    def test_discovers_local_project_prompts(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "local.md").write_text("# L\n")
        assert any(r["source"] == "local" for r in _list_aim_prompts())

    def test_empty(self, tmp_path):
        assert _list_aim_prompts() == []

    def test_no_roots_lists_no_package_sops(self, monkeypatch):
        # Default seam ([], the OSS behavior) → no package SOPs discovered.
        from kiro_crew.platform.defaults import DefaultPromptSourceProvider

        monkeypatch.setattr(
            DefaultPromptSourceProvider, "prompt_source_roots", lambda self: []
        )
        assert _list_aim_prompts() == []

    def test_name_collision(self, aim_dir):
        _aim_pkg(aim_dir, "A-1.0", "1", {"shared": "# A\n"})
        _aim_pkg(aim_dir, "B-1.0", "1", {"shared": "# B\n"})
        r = _list_aim_prompts()
        assert [p["name"] for p in r].count("shared") == 2
        assert {p["package"] for p in r} == {"A-1.0", "B-1.0"}

    def test_sensitive_sop_symlink_skipped(self, aim_dir, tmp_path, monkeypatch):
        """SOP symlinks resolving to sensitive paths are skipped."""
        secret = tmp_path / "secrets" / "creds.sop.md"
        secret.parent.mkdir(parents=True)
        secret.write_text("# Creds\n")
        pkg = aim_dir / "Evil-1.0"
        pkg.mkdir(parents=True)
        (pkg / "evil.sop.md").symlink_to(secret)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.is_sensitive_path",
            lambda p: "secrets" in p,
        )
        assert _list_aim_prompts() == []


# ── _expand_prompt_mention ──


class TestExpandPromptMention:
    def test_resolves_fullname(self, aim_dir):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# Review\nDo review."})
        msg, status = _expand_prompt_mention("@agent-sop:review", _State(), _Slot())
        assert status == "ok"
        assert msg.startswith("Execute the following instructions:")
        assert "Do review." in msg

    def test_resolves_bare_name(self, tmp_path):
        _user_prompt(tmp_path, "p", "# P\nInstructions.")
        msg, status = _expand_prompt_mention("@p", _State(), _Slot())
        assert status == "ok" and "Instructions." in msg

    def test_appends_user_text(self, tmp_path):
        _user_prompt(tmp_path, "g", "# G\nGenerate.")
        msg, status = _expand_prompt_mention("@g for Q1", _State(), _Slot())
        assert status == "ok" and "Generate." in msg and "for Q1" in msg

    def test_no_match(self, tmp_path):
        msg, status = _expand_prompt_mention("@nope hello", _State(), _Slot())
        assert (msg, status) == ("@nope hello", "not_found")

    def test_package_qualified(self, aim_dir):
        _aim_pkg(aim_dir, "A-1.0", "1", {"d": "# A"})
        _aim_pkg(aim_dir, "B-1.0", "1", {"d": "# B"})
        msg, status = _expand_prompt_mention("@B-1.0/d", _State(), _Slot())
        assert status == "ok" and "B" in msg

    def test_shows_info_message(self, tmp_path):
        _user_prompt(tmp_path, "t", "# T")
        slot = _Slot()
        _expand_prompt_mention("@t", _State(), slot)
        assert any("Loaded prompt" in m[1] for m in slot.messages)

    def test_list_error_returns_original(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._find_prompt",
            lambda n: (_ for _ in ()).throw(PermissionError),
        )
        msg, status = _expand_prompt_mention("@x", _State(), _Slot())
        assert (msg, status) == ("@x", "not_found")

    def test_sensitive_path_blocked(self, tmp_path, block_sensitive):
        _user_prompt(tmp_path, "evil", "# Evil")
        msg, status = _expand_prompt_mention("@evil", _State(), _Slot())
        assert status == "blocked"

    def test_unreadable_file(self, tmp_path):
        path = _user_prompt(tmp_path, "broken")
        path.chmod(0o000)
        msg, status = _expand_prompt_mention("@broken", _State(), _Slot())
        path.chmod(0o644)
        assert status == "not_found"

    def test_too_large(self, tmp_path):
        _user_prompt(tmp_path, "huge", "x" * 200_000)
        msg, status = _expand_prompt_mention("@huge", _State(), _Slot())
        assert status == "too_large"


# ── API handlers ──


class TestApiPrompts:
    def test_list(self, aim_dir, mock_sel):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"sop": "# S\n"})
        resp = asyncio.run(api_prompts(MagicMock()))
        body = json.loads(resp.body)
        assert resp.status == 200 and len(body) == 1 and body[0]["name"] == "sop"

    def test_detail_found(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "hello", "# Hello\nWorld.")
        resp = asyncio.run(api_prompt_detail(_api_request("hello")))
        body = json.loads(resp.body)
        assert resp.status == 200 and "World." in body["content"]
        mock_sel.log_tool_invocation.assert_called_once()

    def test_detail_not_found(self, mock_sel):
        assert asyncio.run(api_prompt_detail(_api_request("nope"))).status == 404

    def test_detail_sensitive(self, tmp_path, mock_sel, block_sensitive):
        _user_prompt(tmp_path, "secret")
        resp = asyncio.run(api_prompt_detail(_api_request("secret")))
        assert resp.status == 403 and json.loads(resp.body)["error"] == "access denied"

    def test_detail_unreadable(self, tmp_path, mock_sel):
        path = _user_prompt(tmp_path, "broken")
        path.chmod(0o000)
        resp = asyncio.run(api_prompt_detail(_api_request("broken")))
        path.chmod(0o644)
        assert resp.status == 500
        mock_sel.log_tool_invocation.assert_called_once()
        assert mock_sel.log_tool_invocation.call_args[1]["outcome"] == "error"

    def test_detail_too_large(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "huge", "x" * 200_000)
        resp = asyncio.run(api_prompt_detail(_api_request("huge")))
        assert resp.status == 413
        mock_sel.log_tool_invocation.assert_called_once()
        assert mock_sel.log_tool_invocation.call_args[1]["outcome"] == "too_large"

    def test_detail_package_qualified(self, aim_dir, mock_sel):
        _aim_pkg(aim_dir, "A-1.0", "1", {"d": "# A"})
        _aim_pkg(aim_dir, "B-1.0", "1", {"d": "# B"})
        resp = asyncio.run(api_prompt_detail(_api_request("B-1.0/d")))
        assert resp.status == 200 and "B" in json.loads(resp.body)["content"]


# ── _run_chat prompt paths ──


class TestRunChatPrompts:
    def test_slash_list(self, aim_dir, mock_sel):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts"))
        assert "@agent-sop:review" in sl.messages[-2][1]

    def test_slash_list_empty(self):
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts"))
        assert "No prompts found" in sl.messages[-2][1]

    def test_slash_get_ok(self, aim_dir, mock_sel, monkeypatch):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        captured = {}
        original_run_chat = _run_chat

        async def _mock_run_chat(state, slot, msg, **kw):
            if msg.startswith("Execute the following instructions:"):
                captured["expanded"] = msg
                return
            await original_run_chat(state, slot, msg, **kw)

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _mock_run_chat)
        asyncio.run(_mock_run_chat(s, sl, "/prompts get agent-sop:review"))
        assert any("Loaded prompt" in m[1] for m in sl.messages)
        assert "Do review." in captured.get("expanded", "")

    def test_slash_get_no_name(self, aim_dir, mock_sel):
        """``/prompts get`` with no name falls through to list handler."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts get"))
        assert "@agent-sop:review" in sl.messages[-2][1]

    def test_slash_list_explicit(self, aim_dir, mock_sel):
        """``/prompts list`` works the same as ``/prompts``."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts list"))
        assert "@agent-sop:review" in sl.messages[-2][1]

    def test_slash_get_not_found(self, mock_sel):
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts get nonexistent"))
        assert "not found" in sl.messages[-2][1]

    def test_slash_get_blocked(self, aim_dir, mock_sel, monkeypatch):
        """Prompt discovered but blocked at read time by chat-level check."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"secret": "# S"})
        # Only patch chat-level check so prompt is discovered but blocked at read
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.is_sensitive_path", lambda p: True)
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts get agent-sop:secret"))
        assert any("blocked" in m[1].lower() for m in sl.messages)

    @pytest.mark.skip(reason="Broken by chat.py split (6d4e4493) — mock setup needs updating for new _run_chat flow.")
    def test_at_prompt_blocked(self, aim_dir, mock_sel, monkeypatch):
        """@mention prompt blocked at read time by chat-level check."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"secret": "# S"})
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.is_sensitive_path", lambda p: True)
        # @prompt path runs after session acquisition — needs full mock
        captured = []
        slot = MagicMock(key="t", agent="kirocrew", model=None, _trust=False, _queue=[])
        slot.append = lambda r, t, c: captured.append((r, t, c))
        slot._pending_subagent_failures = []
        state = MagicMock(_hook_store=None, _yolo=False)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        asyncio.run(_run_chat(state, slot, "@agent-sop:secret"))
        assert any("blocked" in m[1].lower() for m in captured)

    def test_api_prompts_does_not_corrupt_cache(self, aim_dir, mock_sel):
        """GET /api/prompts must not mutate cached paths (regression)."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"sop": "# S\nContent."})
        asyncio.run(api_prompts(MagicMock()))
        # After the API call, @mention expansion must still resolve the prompt
        msg, status = _expand_prompt_mention("@agent-sop:sop", MagicMock(), MagicMock())
        assert status == "ok", f"Cache corrupted: expansion returned {status!r}"

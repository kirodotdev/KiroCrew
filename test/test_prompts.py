"""Tests for prompts (agent SOPs) discovery."""

from __future__ import annotations

import ast
import asyncio
import errno
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from conftest import requires_symlinks
from kiro_crew.dashboard.chat import _expand_prompt_mention, _run_chat
from kiro_crew.dashboard.handlers import (
    MAX_PROMPT_BYTES,
    _extract_sop_description,
    _list_aim_prompts,
    api_prompt_detail,
    api_prompts,
    api_prompts_create,
)
from kiro_crew.dashboard.handlers import prompts as _prompts_mod

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
    """Create a user prompt in ~/.kiro/prompts/.

    Written byte-faithfully (UTF-8, no newline translation): the frontmatter
    tests assert exact line endings and a BOM, and Windows' default text mode
    would rewrite ``\\n`` to ``\\r\\n`` and choke on the BOM under cp1252.
    """
    d = tmp_path / ".kiro" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8", newline="")
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
        self.sessions = type(
            "_MockSessions",
            (),
            {
                "get_slack_link": lambda self, k: ("", ""),
                "set_slack_link": lambda self, k, t, c: None,
                "get_or_create": None,
                "get_pid": lambda self, k: None,
                "set_approval_policy": lambda self, k, v: None,
                "check_context_usage": lambda self, k, c: None,
            },
        )()

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
        _aim_pkg(
            aim_dir,
            "Pkg-1.0",
            "1",
            {
                "my-sop": "---\nname: my-sop\ndescription: Test SOP\n---\n",
            },
        )
        r = _list_aim_prompts()
        assert len(r) == 1
        assert (r[0]["name"], r[0]["fullName"], r[0]["source"]) == (
            "my-sop",
            "agent-sop:my-sop",
            "package",
        )
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

        monkeypatch.setattr(DefaultPromptSourceProvider, "prompt_source_roots", lambda self: [])
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
        by_tool: dict[str, list[str]] = {}
        for call in mock_sel.log_tool_invocation.call_args_list:
            by_tool.setdefault(call.kwargs["tool_name"], []).append(call.kwargs["outcome"])
        # The handler audits its own outcome exactly once...
        assert by_tool["api_prompt_detail"] == ["error"]
        # ...and the resolution that precedes it walks the listing, whose
        # description read is withheld by the same bad mode and records that
        # separately. Two reads were refused, so two lines are the honest count.
        assert by_tool["api_prompts"] == ["error"]

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


# ── Repository-supplied prompt paths are read through the descriptor gate ──


class TestPromptReadsGoThroughTheDescriptorGate:
    """A prompt path is READ through the same gate that judged it.

    A project's ``.kiro/prompts`` holds content the user CLONED, so deciding a
    path names a prompt and then re-opening that name is not enough: the bytes
    finally read need not be the bytes anything checked. A HARDLINK breaks the
    equivalence with no race at all — it shares its target's inode, so
    ``realpath`` yields the alias's own innocent path and every path-based check
    passes while the bytes belong to whatever it aliases. ``st_nlink`` is the
    only signal it leaves, and it is readable only on an open descriptor.
    """

    @staticmethod
    def _checkout_prompts(tmp_path, monkeypatch) -> Path:
        """A cloned project's ``.kiro/prompts``, wired in as the local scope."""
        proj = tmp_path / "checkout"
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        return d

    @staticmethod
    def _plant_alias(secret: Path, alias: Path) -> None:
        try:
            os.link(secret, alias)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
            pytest.skip(f"filesystem does not support hardlinks: {exc}")
        if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
            pytest.skip("filesystem did not create a second link")

    @staticmethod
    def _secret(tmp_path) -> Path:
        # A leading '#' comment is what an INI-style credentials file carries, and
        # it is exactly what the description extractor publishes as a heading.
        p = tmp_path / "credentials"
        p.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        return p

    def test_a_hardlinked_prompt_publishes_no_description(self, tmp_path, monkeypatch):
        secret = self._secret(tmp_path)
        d = self._checkout_prompts(tmp_path, monkeypatch)
        (d / "innocent.md").write_text("# Innocent\n", encoding="utf-8")
        self._plant_alias(secret, d / "aliased.md")

        listed = _list_aim_prompts()
        by_name = {p["name"]: p for p in listed}
        # Still listed — the scan sees an ordinary regular .md — but with no
        # metadata drawn out of the aliased inode.
        assert by_name["aliased"]["description"] == ""
        assert "SHOULD-NOT-APPEAR" not in json.dumps(listed)
        # The ordinary neighbour is unaffected: the refusal narrows metadata for
        # one entry, never the library around it.
        assert by_name["innocent"]["description"] == "Innocent"

    def test_a_refused_description_leaves_an_audit_line(self, tmp_path, monkeypatch, mock_sel):
        """An entry listing with no description must not also be invisible.

        With no audit record it is byte-identical to a prompt that simply has no
        description, so a planted alias leaves the operator nothing to find. The
        line records only THAT the bytes were withheld — the gate judges and
        reads through one descriptor and answers a bare ``None``, and re-``stat``
        ing the path to recover a cause would be another by-name look at exactly
        the input this read stopped trusting. Its ordinary neighbour must not
        produce one, or the log says nothing.
        """
        secret = self._secret(tmp_path)
        d = self._checkout_prompts(tmp_path, monkeypatch)
        (d / "innocent.md").write_text("# Innocent\n", encoding="utf-8")
        self._plant_alias(secret, d / "aliased.md")

        _list_aim_prompts()
        refusals = [
            c
            for c in mock_sel.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "api_prompts"
        ]
        assert [c.kwargs["outcome"] for c in refusals] == ["error"]
        assert refusals[0].kwargs["metadata"]["path"].endswith("aliased.md")

    @requires_symlinks
    def test_a_sensitive_description_target_is_audited_as_blocked(
        self, tmp_path, monkeypatch, mock_sel
    ):
        """The one cause that IS knowable is recorded as itself.

        ``validate_file_path`` refuses the name before any open, so unlike the
        gate's bare ``None`` this refusal has a reason the audit line can state.
        """
        store = tmp_path / "credential-store"
        store.mkdir()
        (store / "credentials").write_text("# k = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        monkeypatch.setattr(
            "kiro_crew.hooks.is_sensitive_path", lambda p: "credential-store" in str(p)
        )
        d = self._checkout_prompts(tmp_path, monkeypatch)
        (d / "creds.md").symlink_to(store / "credentials")

        _list_aim_prompts()
        refusals = [
            c
            for c in mock_sel.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "api_prompts"
        ]
        assert [c.kwargs["outcome"] for c in refusals] == ["blocked"]

    def test_a_hardlinked_prompt_is_not_served_by_the_unscoped_read(
        self, tmp_path, monkeypatch, mock_sel
    ):
        secret = self._secret(tmp_path)
        d = self._checkout_prompts(tmp_path, monkeypatch)
        self._plant_alias(secret, d / "aliased.md")

        resp = asyncio.run(api_prompt_detail(_api_request("aliased")))
        assert resp.status == 500
        assert b"SHOULD-NOT-APPEAR" not in resp.body
        # Reported as the outcome an unreadable file already produced, so a
        # refusal is not distinguishable from an I/O error.
        assert mock_sel.log_tool_invocation.call_args[1]["outcome"] == "error"

    @requires_symlinks
    def test_a_prompt_resolving_onto_a_sensitive_target_publishes_no_description(
        self, tmp_path, monkeypatch
    ):
        store = tmp_path / "credential-store"
        store.mkdir()
        secret = store / "credentials"
        secret.write_text("# aws_access_key_id = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        monkeypatch.setattr(
            "kiro_crew.hooks.is_sensitive_path", lambda p: "credential-store" in str(p)
        )
        d = self._checkout_prompts(tmp_path, monkeypatch)
        (d / "creds.md").symlink_to(secret)

        listed = _list_aim_prompts()
        assert [p["name"] for p in listed] == ["creds"]
        assert listed[0]["description"] == ""
        assert "SHOULD-NOT-APPEAR" not in json.dumps(listed)

    @requires_symlinks
    def test_a_prompt_symlinked_to_an_ordinary_file_still_describes(self, tmp_path, monkeypatch):
        """Tolerance, not a fix: the gate canonicalizes BEFORE it opens.

        ``O_NOFOLLOW`` therefore refuses nothing about a link the user chose —
        only a sensitive, hardlinked or non-regular TARGET loses its description.
        Passes before the fix too; it is here so a later round cannot tighten the
        description read into a blanket link refusal without going red.
        """
        real = tmp_path / "notes" / "review.md"
        real.parent.mkdir(parents=True)
        real.write_text("# Review Checklist\n", encoding="utf-8")
        d = self._checkout_prompts(tmp_path, monkeypatch)
        (d / "review.md").symlink_to(real)

        listed = _list_aim_prompts()
        assert [(p["name"], p["description"]) for p in listed] == [("review", "Review Checklist")]

    def test_a_prompt_at_exactly_the_cap_is_still_served(self, tmp_path, mock_sel):
        """The size cap moved onto the gate's own bound; the boundary did not.

        ``MAX_PROMPT_BYTES`` was checked by a ``stat`` before the read and is now
        the gate's ``max_bytes``, so an off-by-one there would refuse a prompt
        that has always been legal.
        """
        _user_prompt(tmp_path, "atcap", "x" * MAX_PROMPT_BYTES)
        resp = asyncio.run(api_prompt_detail(_api_request("atcap")))
        assert resp.status == 200
        assert len(json.loads(resp.body)["content"]) == MAX_PROMPT_BYTES

    # ── The Windows shape: no O_NOFOLLOW, so the fd's real path is the guard ──
    #
    # Windows has no ``O_NOFOLLOW`` at all and the gate asks for it with
    # ``getattr(os, "O_NOFOLLOW", 0)`` at call time, so deleting the attribute
    # reproduces that platform's open semantics on any host: a leaf swapped for a
    # link AFTER canonicalization is followed. What still refuses it is
    # ``within_root`` — the fd-real-path check, pinned to the inode actually
    # opened. The swap is injected deterministically by wrapping the gate's OWN
    # ``validate_file_path`` (which it calls immediately before the open), so no
    # timing is involved. Both handlers hold a module-level binding of that name,
    # so patching it in ``hooks`` reaches only the gate's internal call and leaves
    # each handler's own pre-open resolution — the one that supplies the root —
    # untouched, which is exactly the window being simulated.

    def _swap_leaf_inside_the_gate(self, monkeypatch, entry: Path, secret: Path) -> None:
        from kiro_crew import hooks as _hooks

        real_validate = _hooks.validate_file_path

        def _validate_then_swap(raw):
            out = real_validate(raw)
            if out is not None and Path(out) == entry and not entry.is_symlink():
                entry.unlink()
                entry.symlink_to(secret)
            return out

        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        monkeypatch.setattr(_hooks, "validate_file_path", _validate_then_swap)

    @requires_symlinks
    def test_a_leaf_swapped_after_validation_publishes_no_description(self, tmp_path, monkeypatch):
        secret = tmp_path / "outside" / "credentials"
        secret.parent.mkdir(parents=True)
        secret.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        d = self._checkout_prompts(tmp_path, monkeypatch)
        entry = d / "notes.md"
        entry.write_text("# Notes\n", encoding="utf-8")
        self._swap_leaf_inside_the_gate(monkeypatch, entry, secret)

        assert _extract_sop_description(entry) == ""

    @requires_symlinks
    def test_a_leaf_swapped_after_validation_is_not_served_by_the_unscoped_read(
        self, tmp_path, monkeypatch, mock_sel
    ):
        secret = tmp_path / "outside" / "credentials"
        secret.parent.mkdir(parents=True)
        secret.write_text("# aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        d = self._checkout_prompts(tmp_path, monkeypatch)
        entry = d / "notes.md"
        entry.write_text("# Notes\n", encoding="utf-8")
        # Resolve the entry without the listing, so the ONLY gate call in this
        # test — and so the only swap — is the read's own.
        monkeypatch.setattr(
            _prompts_mod,
            "_find_prompt",
            lambda raw, *a, **kw: {
                "name": "notes",
                "fullName": "notes",
                "description": "",
                "path": str(entry),
                "package": "",
                "source": "local",
            },
        )
        self._swap_leaf_inside_the_gate(monkeypatch, entry, secret)

        resp = asyncio.run(api_prompt_detail(_api_request("notes")))
        assert resp.status == 500
        assert b"SHOULD-NOT-APPEAR" not in resp.body


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

    @pytest.mark.skip(
        reason="Broken by chat.py split (6d4e4493) — mock setup needs updating for new _run_chat flow."
    )
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


# ── Prompt authoring (create / update / delete) ──


def _create_request(body, app="", user="owner-1", owner="owner-1"):
    """POST /api/prompts request stub. ``body`` of None simulates unparseable JSON.

    ``app`` is the auth middleware's app claim: "" (default) is a dashboard
    user; a name simulates an app-token caller; None simulates the claim being
    absent (middleware did not run), which the write gate must fail closed on.
    ``user``/``owner`` shape the owner gate: the default is an owner match
    (``is_owner_dashboard_request`` requires the claim present-and-empty AND
    the caller to equal the configured owner); pass a mismatched ``user`` to
    exercise the non-owner refusal, or ``owner=""`` for the no-owner install.
    """
    r = MagicMock()
    r.method = "POST"
    store = {"app": app, "user": user}
    r.get = MagicMock(side_effect=lambda k, d=None: store.get(k, d))
    r.__contains__ = lambda _self, key: key in store
    r.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    r.app = {"state": state}
    if body is None:
        r.json = AsyncMock(side_effect=ValueError("no json"))
    else:
        r.json = AsyncMock(return_value=body)
    return r


def _write_request(
    method, name, scope="global", body=None, app="", user="owner-1", owner="owner-1"
):
    """PUT/DELETE /api/prompts/{name}?scope= request stub.

    ``app`` mirrors ``_create_request``: "" dashboard user, a name for an
    app-token caller, None for an absent claim (fails closed). ``user``/
    ``owner`` mirror it too: the default is an owner match.
    """
    r = MagicMock()
    r.method = method
    store = {"app": app, "user": user}
    r.get = MagicMock(side_effect=lambda k, d=None: store.get(k, d))
    r.__contains__ = lambda _self, key: key in store
    r.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    r.app = {"state": state}
    r.match_info = {"name": name}
    r.query = {"scope": scope} if scope is not None else {}
    r.json = (
        AsyncMock(return_value=body)
        if body is not None
        else AsyncMock(side_effect=ValueError("no json"))
    )
    return r


def _sha(text: str) -> str:
    """The edit base a PUT must present: sha256 of the pre-state's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Format-valid hash for PUTs whose refusal fires before the compare-and-swap
#: (missing file, confinement, unresolvable scope) — the value is never compared.
_ANY_HASH = "0" * 64


def _listed_names():
    """Names currently visible through the list endpoint."""
    return [p["name"] for p in json.loads(asyncio.run(api_prompts(MagicMock())).body)]


def _outcomes(mock_sel):
    return [c[1]["outcome"] for c in mock_sel.log_tool_invocation.call_args_list]


class TestApiPromptsCreate:
    def test_creates_and_is_immediately_listed(self, tmp_path, mock_sel):
        """A created prompt is visible at once: the write invalidates the list
        cache rather than leaving the reader to wait out its TTL."""
        # Warm the cache first, so a stale hit would be observable.
        assert _listed_names() == []
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "my-prompt", "content": "# Hi"}))
        )
        assert resp.status == 201
        assert (tmp_path / ".kiro" / "prompts" / "my-prompt.md").read_text() == "# Hi"
        assert _listed_names() == ["my-prompt"]

    def test_creates_in_local_scope_under_project(self, tmp_path, monkeypatch, mock_sel):
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "p", "content": "x", "scope": "local"}))
        )
        assert resp.status == 201
        assert (proj / ".kiro" / "prompts" / "p.md").is_file()

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("My Prompt", "my-prompt"),
            ("UPPER", "upper"),
            ("weird!!name", "weird--name"),
            ("nested/path", "nested-path"),  # flat listing: a slash cannot survive
            ("--trim--", "trim"),
        ],
    )
    def test_sanitizes_name(self, tmp_path, mock_sel, raw, expected):
        resp = asyncio.run(api_prompts_create(_create_request({"name": raw, "content": "x"})))
        assert resp.status == 201
        assert json.loads(resp.body)["name"] == expected
        assert (tmp_path / ".kiro" / "prompts" / f"{expected}.md").is_file()

    def test_conflict_when_already_exists(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "dupe")
        resp = asyncio.run(api_prompts_create(_create_request({"name": "dupe", "content": "x"})))
        assert resp.status == 409
        assert _outcomes(mock_sel)[-1] == "conflict"

    def test_does_not_overwrite_existing_content(self, tmp_path, mock_sel):
        p = _user_prompt(tmp_path, "keep", "ORIGINAL")
        asyncio.run(api_prompts_create(_create_request({"name": "keep", "content": "NEW"})))
        assert p.read_text() == "ORIGINAL"

    @pytest.mark.parametrize(
        "body,reason",
        [
            (None, "invalid_json"),
            ([], "invalid_json"),
            ({"name": "n"}, "content_required"),
            ({"name": "n", "content": "   "}, "content_required"),
            ({"name": "n", "content": "x", "scope": "elsewhere"}, "bad_scope"),
            ({"name": "!!!", "content": "x"}, "invalid_name"),
            ({"name": "", "content": "x"}, "invalid_name"),
        ],
    )
    def test_rejects_bad_input(self, tmp_path, mock_sel, body, reason):
        resp = asyncio.run(api_prompts_create(_create_request(body)))
        assert resp.status == 400
        assert _outcomes(mock_sel)[-1] == "bad_request"

    def test_rejects_oversize_content(self, tmp_path, mock_sel):
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "big", "content": "x" * (MAX_PROMPT_BYTES + 1)})
            )
        )
        assert resp.status == 413
        assert _outcomes(mock_sel)[-1] == "too_large"

    def test_local_scope_without_project_rejected(self, tmp_path, mock_sel):
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "p", "content": "x", "scope": "local"}))
        )
        assert resp.status == 400
        assert "local" in json.loads(resp.body)["error"]


class TestApiPromptUpdate:
    def test_updates_and_reflects_immediately(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "edit-me", "---\ndescription: old\n---\n\nbody")
        assert _listed_names() == ["edit-me"]  # warm the cache
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT",
                    "edit-me",
                    body={
                        "content": "---\ndescription: new\n---\n\nb2",
                        "base_hash": _sha("---\ndescription: old\n---\n\nbody"),
                    },
                )
            )
        )
        assert resp.status == 200
        detail = json.loads(asyncio.run(api_prompt_detail(_api_request("edit-me"))).body)
        assert "b2" in detail["content"] and detail["description"] == "new"

    def test_preserves_a_name_the_sanitizer_would_rewrite(self, tmp_path, mock_sel):
        """Update addresses an existing file, so a hand-created ``My_Prompt.md``
        stays editable even though create would never mint that stem."""
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "My_Prompt.md").write_text("old")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "My_Prompt", body={"content": "new", "base_hash": _sha("old")}
                )
            )
        )
        assert resp.status == 200 and (d / "My_Prompt.md").read_text() == "new"

    def test_missing_file_is_404(self, tmp_path, mock_sel):
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "ghost", body={"content": "x", "base_hash": _ANY_HASH})
            )
        )
        assert resp.status == 404

    def test_package_sop_is_not_writable(self, aim_dir, tmp_path, mock_sel):
        """A package SOP is unreachable by the write path: it lives outside the
        user prompt directories, so there is nothing to reject."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"sop": "# S"})
        assert "sop" in _listed_names()
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "sop", body={"content": "hijacked", "base_hash": _ANY_HASH})
            )
        )
        assert resp.status == 404
        assert (aim_dir / "Pkg-1.0" / "sop.sop.md").read_text() == "# S"

    @pytest.mark.parametrize("name", ["../escape", "..", ".hidden", "a/b", "a\\b", ""])
    def test_rejects_names_that_leave_the_prompt_dir(self, tmp_path, mock_sel, name):
        resp = asyncio.run(api_prompt_detail(_write_request("PUT", name, body={"content": "x"})))
        assert resp.status == 400

    def test_rejects_symlink_escaping_the_prompt_dir(self, tmp_path, mock_sel):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET")
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "link.md").symlink_to(outside)
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "link", body={"content": "overwritten", "base_hash": _ANY_HASH}
                )
            )
        )
        assert resp.status == 403
        assert outside.read_text() == "SECRET"

    @pytest.mark.parametrize("scope", [None, "", "elsewhere"])
    def test_requires_a_valid_scope(self, tmp_path, mock_sel, scope):
        _user_prompt(tmp_path, "p")
        resp = asyncio.run(
            api_prompt_detail(_write_request("PUT", "p", scope=scope, body={"content": "x"}))
        )
        assert resp.status == 400

    @pytest.mark.parametrize("body", [None, {}, {"content": "  "}])
    def test_requires_content(self, tmp_path, mock_sel, body):
        _user_prompt(tmp_path, "p", "original")
        resp = asyncio.run(api_prompt_detail(_write_request("PUT", "p", body=body)))
        assert resp.status == 400
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "original"

    def test_rejects_oversize_content(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "p", "original")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": "x" * (MAX_PROMPT_BYTES + 1)})
            )
        )
        assert resp.status == 413
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "original"


class TestPromptEditCompareAndSwap:
    """A PUT names the file state its edit was based on; the writer refuses when
    the file no longer matches. Without this, an edit started before someone
    else's save silently discards their work on completion."""

    def test_stale_base_hash_answers_409_and_leaves_the_file(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "p", "THEIRS\n")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "p", body={"content": "MINE\n", "base_hash": _sha("WHAT I SAW\n")}
                )
            )
        )
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "content_conflict"
        assert _outcomes(mock_sel)[-1] == "conflict"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "THEIRS\n"

    def test_too_large_outcome_maps_to_the_conflict_contract(self, tmp_path, mock_sel, monkeypatch):
        """A file that outgrew the cap since the edit base was read cannot
        match that base: the handler answers the same coded 409 as any other
        conflict, not a 413 (the request body itself is within limits)."""
        _user_prompt(tmp_path, "p", "SMALL\n")
        monkeypatch.setattr(
            _prompts_mod, "verified_replace_file_nolink", lambda *a, **kw: "too_large"
        )
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": "NEW\n", "base_hash": _sha("SMALL\n")})
            )
        )
        assert resp.status == 409
        assert json.loads(resp.body)["code"] == "content_conflict"

    def test_matching_base_hash_writes_and_returns_the_new_hash(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "p", "BEFORE\n")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "p", body={"content": "AFTER\n", "base_hash": _sha("BEFORE\n")}
                )
            )
        )
        assert resp.status == 200
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "AFTER\n"
        # The response hands back the state this save created, so an immediate
        # re-save without a fresh GET presents the right edit base.
        assert json.loads(resp.body)["hash"] == _sha("AFTER\n")

    @pytest.mark.parametrize("bad", [None, 42, "", "not-a-hash", "0" * 63, "G" * 64])
    def test_missing_or_malformed_base_hash_is_a_coded_400(self, tmp_path, mock_sel, bad):
        _user_prompt(tmp_path, "p", "ORIGINAL\n")
        body = {"content": "NEW\n"}
        if bad is not None:
            body["base_hash"] = bad
        resp = asyncio.run(api_prompt_detail(_write_request("PUT", "p", body=body)))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "base_hash_required"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "ORIGINAL\n"

    def test_concurrent_saves_with_the_same_base_serialize_to_one_winner(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The compare-and-swap is check-then-write across two calls, and the
        executor pool is concurrent — so without serialization two PUTs could
        both verify the same base and then both land, the second silently
        discarding the first. The write lock makes the loser's verify read the
        winner's content and answer 409."""
        _user_prompt(tmp_path, "p", "BASE\n")
        base = _sha("BASE\n")

        real_read = _prompts_mod.safe_read_file_bytes_nolink

        def _slow_read(*a, **kw):
            # Widen the check-to-write window so an unserialized race is
            # certain, not lucky: both verifies complete before either write
            # unless the lock forces them into sequence.
            result = real_read(*a, **kw)
            time.sleep(0.2)
            return result

        monkeypatch.setattr(_prompts_mod, "safe_read_file_bytes_nolink", _slow_read)

        async def _race():
            return await asyncio.gather(
                api_prompt_detail(
                    _write_request("PUT", "p", body={"content": "FIRST\n", "base_hash": base})
                ),
                api_prompt_detail(
                    _write_request("PUT", "p", body={"content": "SECOND\n", "base_hash": base})
                ),
            )

        resps = asyncio.run(_race())
        statuses = sorted(r.status for r in resps)
        assert statuses == [200, 409], statuses
        # The file holds exactly the winner's write, never a torn or clobbered
        # mix, and the loser's payload is nowhere on disk.
        final = (tmp_path / ".kiro" / "prompts" / "p.md").read_text()
        winner = next(r for r in resps if r.status == 200)
        assert final in ("FIRST\n", "SECOND\n")
        assert json.loads(winner.body)["hash"] == _sha(final)

    def test_detail_read_hands_out_the_edit_base(self, tmp_path, mock_sel):
        """GET carries the hash a PUT presents — of the RAW bytes, so the pair
        round-trips: read, edit, save with the hash the read gave."""
        _user_prompt(tmp_path, "p", "CONTENT\n")
        detail = json.loads(
            asyncio.run(api_prompt_detail(_write_request("GET", "p", scope="global"))).body
        )
        assert detail["hash"] == _sha("CONTENT\n")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": "NEXT\n", "base_hash": detail["hash"]})
            )
        )
        assert resp.status == 200

    def test_a_redacted_copy_carries_no_hash(self, tmp_path, mock_sel):
        """Editing a redacted copy is refused, so its hash serves no caller —
        and sha256 of the raw bytes would be an offline verification oracle
        for exactly the content the redaction hides."""
        _user_prompt(tmp_path, "leaky", "aws_key = AKIAIOSFODNN7EXAMPLE\n")
        detail = json.loads(
            asyncio.run(api_prompt_detail(_write_request("GET", "leaky", scope="global"))).body
        )
        assert detail["redacted"] is True
        assert detail["hash"] == ""


class TestLinkRefusalIsUniform:
    """A symlink answers the same refusal whether its target exists or not.
    ``is_file()`` follows links, so checking it before the link check would
    answer 404 for a dangling link and 403 for a live one — a per-path
    existence oracle for anything the link's author points at."""

    @staticmethod
    def _link(tmp_path, target: Path):
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True, exist_ok=True)
        (d / "probe.md").symlink_to(target)

    @pytest.mark.parametrize("exists", [True, False])
    def test_scoped_get_refuses_links_identically(self, tmp_path, mock_sel, exists):
        target = tmp_path / "candidate.md"
        if exists:
            target.write_text("SENSITIVE")
        self._link(tmp_path, target)
        resp = asyncio.run(api_prompt_detail(_write_request("GET", "probe", scope="global")))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "access_denied"

    @pytest.mark.parametrize("exists", [True, False])
    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    def test_writes_refuse_links_identically(self, tmp_path, mock_sel, method, exists):
        target = tmp_path / "candidate.md"
        if exists:
            target.write_text("SENSITIVE")
        self._link(tmp_path, target)
        body = {"content": "x", "base_hash": _ANY_HASH} if method == "PUT" else None
        resp = asyncio.run(api_prompt_detail(_write_request(method, "probe", body=body)))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "access_denied"
        if exists:
            assert target.read_text() == "SENSITIVE"


class TestApiPromptDelete:
    def test_deletes_and_disappears_immediately(self, tmp_path, mock_sel):
        p = _user_prompt(tmp_path, "bye")
        assert _listed_names() == ["bye"]  # warm the cache
        resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "bye")))
        assert resp.status == 200 and not p.exists()
        assert _listed_names() == []

    def test_missing_file_is_404(self, tmp_path, mock_sel):
        resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "ghost")))
        assert resp.status == 404
        assert _outcomes(mock_sel)[-1] == "not_found"

    def test_leaves_a_symlink_target_intact(self, tmp_path, mock_sel):
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET")
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "link.md").symlink_to(outside)
        assert asyncio.run(api_prompt_detail(_write_request("DELETE", "link"))).status == 403
        assert outside.exists()

    def test_get_still_reads_after_the_method_branch(self, tmp_path, mock_sel):
        """The method branch must not shadow the read path."""
        _user_prompt(tmp_path, "readable", "# Readable")
        resp = asyncio.run(api_prompt_detail(_api_request("readable")))
        assert resp.status == 200 and "Readable" in json.loads(resp.body)["content"]


class TestPromptWriteRefusalContract:
    """Every refused write answers with a machine-readable ``code`` that is the
    same identifier it audited.

    The scenarios below are the evidence; the assertion is the rule. Codes and
    audit reasons are written at each call site (the error-code contract test
    checks a literal status there), so nothing but this stops the two from
    drifting apart on a later edit.
    """

    def _scenarios(self, tmp_path):
        _user_prompt(tmp_path, "exists")
        big = "x" * (MAX_PROMPT_BYTES + 1)
        return [
            ("invalid_json", 400, lambda: api_prompts_create(_create_request(None))),
            (
                "app_token_forbidden",
                403,
                lambda: api_prompts_create(
                    _create_request({"name": "n", "content": "c"}, app="someapp")
                ),
            ),
            ("content_required", 400, lambda: api_prompts_create(_create_request({"name": "n"}))),
            (
                "bad_scope",
                400,
                lambda: api_prompts_create(
                    _create_request({"name": "n", "content": "c", "scope": "x"})
                ),
            ),
            (
                "content_too_large",
                413,
                lambda: api_prompts_create(_create_request({"name": "n", "content": big})),
            ),
            (
                "invalid_name",
                400,
                lambda: api_prompts_create(_create_request({"name": "!!!", "content": "c"})),
            ),
            (
                "no_active_project",
                400,
                lambda: api_prompts_create(
                    _create_request({"name": "n", "content": "c", "scope": "local"})
                ),
            ),
            (
                "prompt_exists",
                409,
                lambda: api_prompts_create(_create_request({"name": "exists", "content": "c"})),
            ),
            (
                "bad_scope",
                400,
                lambda: api_prompt_detail(
                    _write_request("PUT", "exists", scope="x", body={"content": "c"})
                ),
            ),
            (
                "invalid_name",
                400,
                lambda: api_prompt_detail(_write_request("PUT", "../x", body={"content": "c"})),
            ),
            (
                "no_active_project",
                400,
                lambda: api_prompt_detail(
                    _write_request(
                        "PUT",
                        "exists",
                        scope="local",
                        body={"content": "c", "base_hash": _ANY_HASH},
                    )
                ),
            ),
            (
                "content_required",
                400,
                lambda: api_prompt_detail(_write_request("PUT", "exists", body={})),
            ),
            (
                "content_too_large",
                413,
                lambda: api_prompt_detail(_write_request("PUT", "exists", body={"content": big})),
            ),
            ("prompt_not_found", 404, lambda: api_prompt_detail(_write_request("DELETE", "ghost"))),
        ]

    def test_every_refusal_codes_what_it_audited(self, tmp_path, mock_sel):
        for code, status, call in self._scenarios(tmp_path):
            mock_sel.log_tool_invocation.reset_mock()
            resp = asyncio.run(call())
            body = json.loads(resp.body)
            assert resp.status == status, f"{code}: status {resp.status}"
            assert body["code"] == code, f"expected code {code}, got {body.get('code')!r}"
            audited = mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]
            assert audited.get("reason") == code, f"{code}: audited {audited.get('reason')!r}"

    def test_access_denied_codes_what_it_audited(self, tmp_path, mock_sel):
        """Symlink escape is the one refusal that needs a prepared filesystem."""
        outside = tmp_path / "outside.md"
        outside.write_text("SECRET")
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "link.md").symlink_to(outside)
        resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "link")))
        assert resp.status == 403 and json.loads(resp.body)["code"] == "access_denied"
        assert (
            mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]["reason"]
            == "access_denied"
        )


class TestPromptWriteHardening:
    """Refusals added because a reviewer traced each one to a concrete loss."""

    def test_detail_reports_a_filtered_copy(self, tmp_path, mock_sel):
        """The editor writes back what it was given, so the read path has to say
        when what it gave back is not what is on disk."""
        _user_prompt(tmp_path, "leaky", "aws_key = AKIAIOSFODNN7EXAMPLE\n")
        body = json.loads(asyncio.run(api_prompt_detail(_api_request("leaky"))).body)
        assert body["redacted"] is True
        assert "AKIAIOSFODNN7EXAMPLE" not in body["content"]

    def test_detail_reports_an_untouched_copy(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "clean", "# Just prose\n")
        body = json.loads(asyncio.run(api_prompt_detail(_api_request("clean"))).body)
        assert body["redacted"] is False

    def test_scoped_read_refuses_a_hardlinked_sensitive_file(self, tmp_path, mock_sel):
        """A hardlink has no link flag to detect by name: the entry looks like a
        plain regular file in the prompt dir. The read gate validates the inode
        it actually opened, so a second link to a file outside the dir is
        refused rather than served."""
        secret = tmp_path / "secret.txt"
        secret.write_text("AKIAIOSFODNN7EXAMPLE\n")
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        os.link(secret, d / "looks-normal.md")
        resp = asyncio.run(api_prompt_detail(_write_request("GET", "looks-normal", scope="global")))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "access_denied"

    def test_create_refuses_a_linked_prompt_root(self, tmp_path, mock_sel):
        """A linked root defeats confinement: both sides of the containment test
        resolve into the link's destination, so every path looks contained."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (tmp_path / ".kiro").mkdir()
        (tmp_path / ".kiro" / "prompts").symlink_to(outside)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 403 and json.loads(resp.body)["code"] == "linked_prompt_root"
        assert not (outside / "p.md").exists()

    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    def test_write_refuses_a_linked_prompt_root(self, tmp_path, mock_sel, method):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "victim.md").write_text("ORIGINAL")
        (tmp_path / ".kiro").mkdir()
        (tmp_path / ".kiro" / "prompts").symlink_to(outside)
        body = {"content": "hijacked", "base_hash": _ANY_HASH} if method == "PUT" else None
        resp = asyncio.run(api_prompt_detail(_write_request(method, "victim", body=body)))
        assert resp.status == 403 and json.loads(resp.body)["code"] == "linked_prompt_root"
        assert (outside / "victim.md").read_text() == "ORIGINAL"

    def test_create_refuses_an_overlong_name(self, tmp_path, mock_sel):
        """Bounded here so the filesystem's own ENAMETOOLONG cannot surface as an
        unaudited 500 from inside the executor."""
        resp = asyncio.run(api_prompts_create(_create_request({"name": "a" * 300, "content": "x"})))
        assert resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"
        assert _outcomes(mock_sel)[-1] == "bad_request"

    @pytest.mark.skipif(
        not _prompts_mod._DIR_FD_SUPPORTED,
        reason="the by-name fallback writes through Path.open, not os.write",
    )
    def test_create_audits_a_filesystem_failure(self, tmp_path, mock_sel, monkeypatch):
        def _boom(*a, **kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "write", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_update_audits_a_filesystem_failure(self, tmp_path, mock_sel, monkeypatch):
        _user_prompt(tmp_path, "p", "ORIGINAL")

        def _boom(*a, **kw):
            raise OSError(13, "Permission denied")

        # The update writes through the descriptor-pinned writer, not atomic_write.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.prompts.verified_replace_file_nolink", _boom
        )
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": "new", "base_hash": _sha("ORIGINAL")})
            )
        )
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "ORIGINAL"

    def test_update_replaces_atomically(self, tmp_path, mock_sel):
        """Atomic replace, not truncate-in-place: a torn write would leave the
        prompt unreadable rather than simply unchanged."""
        p = _user_prompt(tmp_path, "p", "ORIGINAL")
        resp = asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": "NEW", "base_hash": _sha("ORIGINAL")})
            )
        )
        assert resp.status == 200 and p.read_text() == "NEW"


class TestScopedPromptDetail:
    """A read addressed like a write, so the editor is seeded from the bytes a
    following PUT would replace."""

    def _scoped(self, name, scope):
        r = MagicMock()
        r.method = "GET"
        r.match_info = {"name": name}
        r.query = {"scope": scope}
        return r

    def test_same_stem_in_both_scopes_resolves_by_scope(self, tmp_path, monkeypatch, mock_sel):
        """Unscoped resolution is first-match, so a shared stem is ambiguous — and
        an editor seeded from the wrong one would save under the other's scope."""
        _user_prompt(tmp_path, "dup", "GLOBAL BODY")
        proj = tmp_path / "proj"
        (proj / ".kiro" / "prompts").mkdir(parents=True)
        (proj / ".kiro" / "prompts" / "dup.md").write_text("LOCAL BODY")
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)

        g = json.loads(asyncio.run(api_prompt_detail(self._scoped("dup", "global"))).body)
        loc = json.loads(asyncio.run(api_prompt_detail(self._scoped("dup", "local"))).body)
        assert g["content"] == "GLOBAL BODY" and g["source"] == "global"
        assert loc["content"] == "LOCAL BODY" and loc["source"] == "local"

    def test_scoped_read_reports_redaction(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "leaky", "aws_key = AKIAIOSFODNN7EXAMPLE\n")
        body = json.loads(asyncio.run(api_prompt_detail(self._scoped("leaky", "global"))).body)
        assert body["redacted"] is True and "AKIAIOSFODNN7EXAMPLE" not in body["content"]

    def test_scoped_read_missing_is_coded_404(self, tmp_path, mock_sel):
        resp = asyncio.run(api_prompt_detail(self._scoped("ghost", "global")))
        assert resp.status == 404 and json.loads(resp.body)["code"] == "prompt_not_found"

    @pytest.mark.parametrize("name", ["../escape", "..", ".hidden", "a/b"])
    def test_scoped_read_rejects_traversal(self, tmp_path, mock_sel, name):
        resp = asyncio.run(api_prompt_detail(self._scoped(name, "global")))
        assert resp.status == 400 and json.loads(resp.body)["code"] == "invalid_name"

    def test_scoped_read_refuses_a_linked_root(self, tmp_path, mock_sel):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "secret.md").write_text("SECRET")
        (tmp_path / ".kiro").mkdir()
        (tmp_path / ".kiro" / "prompts").symlink_to(outside)
        resp = asyncio.run(api_prompt_detail(self._scoped("secret", "global")))
        assert resp.status == 403 and json.loads(resp.body)["code"] == "linked_prompt_root"

    def test_unscoped_read_still_works(self, tmp_path, mock_sel):
        """The scope query is additive: the existing unscoped path is untouched."""
        _user_prompt(tmp_path, "plain", "# Plain")
        body = json.loads(asyncio.run(api_prompt_detail(_api_request("plain"))).body)
        assert "Plain" in body["content"]


class TestCreateFailureLeavesNoPartial:
    @pytest.mark.skipif(
        not _prompts_mod._DIR_FD_SUPPORTED,
        reason="the by-name fallback writes through Path.open, not os.fdopen",
    )
    def test_a_failed_write_does_not_block_the_retry(self, tmp_path, mock_sel, monkeypatch):
        """O_EXCL would answer every retry with 409 if the partial file survived."""
        real_write = os.write
        calls = {"n": 0}

        def _fail_first(fd, data, *a, **kw):
            if calls["n"] == 0:
                calls["n"] += 1
                # The O_CREAT|O_EXCL half already succeeded and the name exists;
                # only the body write fails.
                raise OSError(28, "No space left on device")
            return real_write(fd, data, *a, **kw)

        monkeypatch.setattr(os, "write", _fail_first)
        first = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert first.status == 500 and json.loads(first.body)["code"] == "write_failed"
        assert not (tmp_path / ".kiro" / "prompts" / "p.md").exists()

        second = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert second.status == 201


class TestScopedReadOversizeRace:
    """The cap is checked by a stat and again by the read gate. The gate signals
    with FileTooLargeError, which is NOT an OSError, so it needs its own catch
    to stay on the coded 413 path rather than escaping as an unaudited 500."""

    def test_gate_oversize_maps_to_a_coded_413(self, tmp_path, mock_sel, monkeypatch):
        _user_prompt(tmp_path, "grower", "small\n")
        import kiro_crew.dashboard.handlers.prompts as mod

        def _boom(*a, **k):
            raise mod.FileTooLargeError("grew after the stat")

        monkeypatch.setattr(mod, "safe_read_file_bytes_nolink", _boom)
        resp = asyncio.run(api_prompt_detail(_write_request("GET", "grower", scope="global")))
        assert resp.status == 413
        assert json.loads(resp.body)["code"] == "content_too_large"


class TestScopedReadDescriptionSource:
    """The scoped read validates an inode and returns its bytes. Metadata must
    come from those bytes: reopening the path would reintroduce the
    check-to-use window the read gate closes, and could answer with another
    file's contents through `description`."""

    def test_description_comes_from_the_validated_bytes(self, tmp_path, mock_sel, monkeypatch):
        _user_prompt(tmp_path, "p", "---\ndescription: real one\n---\n\n# Body\n")
        import kiro_crew.dashboard.handlers.prompts as mod

        # Any path reopen after the gate is a defect, so make one fail loudly.
        def _no_reopen(_path):
            raise AssertionError("description must not reopen the file")

        monkeypatch.setattr(mod, "_extract_sop_description", _no_reopen)
        resp = asyncio.run(api_prompt_detail(_write_request("GET", "p", scope="global")))
        assert resp.status == 200
        assert json.loads(resp.body)["description"] == "real one"

    def test_block_scalar_description_resolves_like_the_listing(self, tmp_path, mock_sel):
        """Same grammar on both paths: the text parser is the one the listing's
        path wrapper delegates to, so an indented block scalar resolves rather
        than surfacing as the bare indicator."""
        _user_prompt(tmp_path, "p", "---\ndescription: >-\n  folded one\n  liner\n---\n\nBody\n")
        body = json.loads(
            asyncio.run(api_prompt_detail(_write_request("GET", "p", scope="global"))).body
        )
        assert body["description"] == "folded one liner"


class TestUnencodableContentIsRefusedNotCrashed:
    """JSON permits lone surrogates; UTF-8 has no encoding for them. The size
    check is the first thing that encodes the body, so without a guard there a
    valid request body answered 500 with no audit line — and the size check runs
    before the executor, so the broad catch around that await never saw it."""

    @pytest.mark.parametrize("payload", ["\ud800", "ok then \udfff tail"])
    def test_create_refuses_a_lone_surrogate(self, tmp_path, mock_sel, payload):
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "p", "content": payload, "scope": "global"})
            )
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "content_not_encodable"
        assert _outcomes(mock_sel)[-1] == "bad_request"
        assert not (tmp_path / ".kiro" / "prompts" / "p.md").exists()

    def test_update_refuses_a_lone_surrogate_and_keeps_the_file(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "p", "ORIGINAL\n")
        resp = asyncio.run(
            api_prompt_detail(_write_request("PUT", "p", body={"content": "\ud800"}))
        )
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "content_not_encodable"
        assert _outcomes(mock_sel)[-1] == "bad_request"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "ORIGINAL\n"

    def test_astral_plane_text_is_still_accepted(self, tmp_path, mock_sel):
        """The guard rejects unpaired surrogates, not non-BMP characters: an
        emoji is four perfectly encodable bytes and must still round-trip."""
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "p", "content": "hello 🐾\n", "scope": "global"})
            )
        )
        assert resp.status == 201
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_bytes() == "hello 🐾\n".encode(
            "utf-8"
        )


class TestCreatedBytesAreExact:
    """A create must write the caller's bytes unchanged. Newline translation
    would silently inflate the file — on Windows every LF becomes CRLF — so a
    body just under the size cap lands as a file over it: created successfully,
    then refused by its own read with 413."""

    def test_create_writes_the_posted_bytes_verbatim(self, tmp_path, mock_sel):
        body = "line one\nline two\n\n  indented\n"
        resp = asyncio.run(api_prompts_create(_create_request({"name": "exact", "content": body})))
        assert resp.status == 201
        written = (tmp_path / ".kiro" / "prompts" / "exact.md").read_bytes()
        assert written == body.encode("utf-8")

    def test_create_and_update_agree_on_byte_handling(self, tmp_path, mock_sel):
        """Both write paths take newline="" — pinned together because a change to
        one that skipped the other would reintroduce the mismatch."""
        body = "a\nb\n"
        asyncio.run(api_prompts_create(_create_request({"name": "p", "content": body})))
        created = (tmp_path / ".kiro" / "prompts" / "p.md").read_bytes()
        asyncio.run(
            api_prompt_detail(
                _write_request("PUT", "p", body={"content": body, "base_hash": _sha(body)})
            )
        )
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_bytes() == created


class TestUpdatePreservesPermissions:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows has no POSIX mode bits; chmod there only toggles read-only",
    )
    def test_editing_does_not_widen_who_can_read_a_prompt(self, tmp_path, mock_sel):
        """A user who chmods a prompt to 0600 has said who may read it. The
        replacement file inherits umask defaults unless the mode is carried
        over, so without this an edit would publish a private prompt at 0644."""
        _user_prompt(tmp_path, "private", "secret-ish\n")
        path = tmp_path / ".kiro" / "prompts" / "private.md"
        path.chmod(0o600)
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT",
                    "private",
                    body={"content": "still private\n", "base_hash": _sha("secret-ish\n")},
                )
            )
        )
        assert resp.status == 200
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == "still private\n"


class TestLossyDecodeIsNotAnEditBase:
    """A prompt whose bytes are not valid UTF-8 is served with U+FFFD in place
    of what could not be decoded. That copy is a transformation of the file, so
    it must not be offered as an edit base: saving it would write the
    replacement characters over bytes that are still intact on disk. Same
    hazard as the redacted copy, reported separately so the UI can say which
    transformation happened."""

    def test_non_utf8_prompt_is_reported_lossy(self, tmp_path, mock_sel):
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        # 0xff is not valid UTF-8 in any position.
        (d / "legacy.md").write_bytes(b"caf\xe9 legacy \xff bytes\n")
        body = json.loads(
            asyncio.run(api_prompt_detail(_write_request("GET", "legacy", scope="global"))).body
        )
        assert body["lossy"] is True
        # Not a credential finding — the two facts are reported separately.
        assert body["redacted"] is False
        # Still readable: viewing a legacy-encoded prompt keeps working.
        assert "\ufffd" in body["content"]

    def test_clean_utf8_prompt_is_not_lossy(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "clean", "café is fine\n")
        body = json.loads(
            asyncio.run(api_prompt_detail(_write_request("GET", "clean", scope="global"))).body
        )
        assert body["lossy"] is False and body["redacted"] is False
        assert body["content"] == "café is fine\n"


class TestUpdateCarriesAccessControlMetadata:
    """`atomic_write(mode=...)` carries permission BITS onto a fresh inode, which
    silently drops a named POSIX ACL (`system.posix_acl_access`) and any other
    extended attribute. The update therefore goes through the repo's
    descriptor-pinned writer, which captures xattrs from the validated
    descriptor and refuses the write if an access-control attribute cannot be
    carried across the replace."""

    def test_extended_attributes_survive_an_edit(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "tagged", "ORIGINAL\n")
        path = tmp_path / ".kiro" / "prompts" / "tagged.md"
        try:
            os.setxattr(str(path), "user.kirocrew_test", b"keep-me")
        except (AttributeError, OSError) as exc:  # tmpfs and several net mounts
            pytest.skip(f"filesystem does not support user xattrs: {exc}")

        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "tagged", body={"content": "EDITED\n", "base_hash": _sha("ORIGINAL\n")}
                )
            )
        )
        assert resp.status == 200
        assert path.read_text() == "EDITED\n"
        assert os.getxattr(str(path), "user.kirocrew_test") == b"keep-me"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows has no POSIX mode bits; chmod there only toggles read-only",
    )
    def test_mode_still_survives_an_edit(self, tmp_path, mock_sel):
        """The narrower guarantee the previous writer gave must not regress."""
        _user_prompt(tmp_path, "private", "ORIGINAL\n")
        path = tmp_path / ".kiro" / "prompts" / "private.md"
        path.chmod(0o600)
        assert (
            asyncio.run(
                api_prompt_detail(
                    _write_request(
                        "PUT",
                        "private",
                        body={"content": "still private\n", "base_hash": _sha("ORIGINAL\n")},
                    )
                )
            ).status
            == 200
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not _prompts_mod._DIR_FD_SUPPORTED, reason="platform has no openat/unlinkat")
class TestCreateAndDeletePinTheDirectory:
    """Create and delete operate relative to a pinned directory descriptor, so a
    prompt root swapped for a link AFTER the check cannot redirect them.

    The swap is staged inside ``_pin_prompt_dir``: the real descriptor is opened,
    then the directory is displaced and its name pointed elsewhere. That is
    precisely the check-to-use window — every later name lookup would resolve to
    the attacker's directory, and only an operation relative to the descriptor
    still reaches the inode that was validated.
    """

    @pytest.fixture()
    def swap_after_pin(self, tmp_path, monkeypatch):
        """Displace the prompt root the instant it has been pinned."""
        real = tmp_path / ".kiro" / "prompts"
        moved = tmp_path / "pinned-real"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        real_pin = _prompts_mod._pin_prompt_dir
        swapped = {"done": False}

        def _pin(d, **kw):
            fd = real_pin(d, **kw)
            if real.is_dir() and not real.is_symlink():
                real.rename(moved)
                real.symlink_to(elsewhere)
                swapped["done"] = True
            return fd

        monkeypatch.setattr(_prompts_mod, "_pin_prompt_dir", _pin)
        return moved, elsewhere, swapped

    def test_create_writes_into_the_pinned_directory(self, tmp_path, mock_sel, swap_after_pin):
        moved, elsewhere, swapped = swap_after_pin
        (tmp_path / ".kiro" / "prompts").mkdir(parents=True)
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "pinned", "content": "BODY\n", "scope": "global"})
            )
        )
        assert resp.status == 201
        assert swapped["done"], "the swap never ran — the test would be vacuous"
        assert (moved / "pinned.md").read_text() == "BODY\n"
        assert list(elsewhere.iterdir()) == []

    def test_delete_removes_from_the_pinned_directory(self, tmp_path, mock_sel, swap_after_pin):
        moved, elsewhere, swapped = swap_after_pin
        _user_prompt(tmp_path, "doomed")
        decoy = elsewhere / "doomed.md"
        decoy.write_text("NOT YOURS")
        assert asyncio.run(api_prompt_detail(_write_request("DELETE", "doomed"))).status == 200
        assert swapped["done"], "the swap never ran — the test would be vacuous"
        assert not (moved / "doomed.md").exists()
        assert decoy.read_text() == "NOT YOURS"

    def test_a_symlinked_kiro_dir_is_still_usable(self, tmp_path, mock_sel):
        """An ancestor link the user chose is followed, as the read path does.

        Dotfile managers symlink ``~/.kiro``. The pin is deliberately no stricter
        than ``_linked_prompt_root``, which refuses a link at the prompt
        directory itself and documents that an ancestor link redirects nothing
        the user did not already choose. Refusing it here would make create and
        delete reject a layout the rest of the API accepts.
        """
        real_kiro = tmp_path / "dotfiles" / ".kiro"
        (real_kiro / "prompts").mkdir(parents=True)
        (tmp_path / ".kiro").symlink_to(real_kiro)
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "linked", "content": "BODY\n", "scope": "global"})
            )
        )
        assert resp.status == 201
        assert (real_kiro / "prompts" / "linked.md").read_text() == "BODY\n"
        assert asyncio.run(api_prompt_detail(_write_request("DELETE", "linked"))).status == 200
        assert not (real_kiro / "prompts" / "linked.md").exists()

    def test_the_walk_pins_every_level_not_just_the_leaf(self, tmp_path, mock_sel, monkeypatch):
        """A swap of ``.kiro`` DURING the walk cannot redirect the operation.

        The rename is staged between the two components of the walk, which is the
        interval a single ``O_DIRECTORY|O_NOFOLLOW`` open of the leaf would have
        resolved through — a probe confirms that shape follows the link. Because
        each level is pinned before the next lookup, the create still lands in
        the directory the walk was standing in.
        """
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "prompts").mkdir(parents=True)
        (tmp_path / ".kiro" / "prompts").mkdir(parents=True)
        kiro = tmp_path / ".kiro"
        real_open = os.open
        home_stat = tmp_path.stat()
        swapped = {"done": False}

        def _swap_between_components(path, *a, **kw):
            fd = real_open(path, *a, **kw)
            # Fire only for the isolated home's ``.kiro`` — identified by its
            # parent dir_fd — so a ``.kiro`` component on the walk TO the
            # isolated home (e.g. a TMPDIR under the real ~/.kiro) is not a
            # trigger. Without this the swap converts the wrong directory and
            # the walk's (correct) symlink refusal reads as a test failure.
            dir_fd = kw.get("dir_fd")
            in_home = dir_fd is not None and os.path.samestat(os.stat(dir_fd), home_stat)
            if path == ".kiro" and in_home and not swapped["done"]:
                swapped["done"] = True
                kiro.rename(tmp_path / "kiro-real")
                kiro.symlink_to(elsewhere)
            return fd

        monkeypatch.setattr(os, "open", _swap_between_components)
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "pinned", "content": "BODY\n", "scope": "global"})
            )
        )
        monkeypatch.undo()
        assert resp.status == 201
        assert swapped["done"], "the swap never ran — the test would be vacuous"
        assert (tmp_path / "kiro-real" / "prompts" / "pinned.md").read_text() == "BODY\n"
        assert list((elsewhere / "prompts").iterdir()) == []

    def test_a_pin_failure_that_is_not_a_link_is_not_reported_as_one(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """EACCES, EMFILE and friends are operational failures, not a linked root."""

        def _eacces(*a, **kw):
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(_prompts_mod, "_pin_prompt_dir", _eacces)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "p", "content": "x", "scope": "global"}))
        )
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_a_failing_write_is_audited_and_leaks_no_descriptor(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """Every outcome is audited, including a non-OS failure, and the create fd
        is owned here so each failed attempt closes exactly one.

        A ``MemoryError`` stands in for the whole class the narrower ``OSError``
        catch used to let escape: it would have answered 500 with no audit line,
        which is the one thing this handler promises not to do.

        Descriptors are counted through the process's own fd directory: Linux
        exposes ``/proc/self/fd`` and macOS ``/dev/fd``, and the repo already
        reads whichever exists elsewhere. Skipped where neither does.
        """
        fd_dir = next((p for p in ("/proc/self/fd", "/dev/fd") if os.path.isdir(p)), None)
        if fd_dir is None:
            pytest.skip("no fd directory to count descriptors through")

        def _boom(*a, **kw):
            raise MemoryError("no buffer")

        monkeypatch.setattr(os, "write", _boom)
        before = len(os.listdir(fd_dir))
        for _ in range(20):
            resp = asyncio.run(
                api_prompts_create(
                    _create_request({"name": "p", "content": "x", "scope": "global"})
                )
            )
            assert resp.status == 500
            assert json.loads(resp.body)["code"] == "write_failed"
            assert _outcomes(mock_sel)[-1] == "error"
        # A leak would grow this by one per attempt; the slack absorbs the
        # descriptors the event loop and the executor legitimately hold.
        assert len(os.listdir(fd_dir)) <= before + 4

    @pytest.mark.parametrize("method", ["POST", "DELETE"])
    def test_a_linked_root_is_refused_by_the_descriptor_itself(
        self, tmp_path, mock_sel, monkeypatch, method
    ):
        """The pin is a second, independent refusal — not a repeat of the lstat.

        ``_linked_prompt_root`` is forced to pass so the only thing left to catch
        a symlinked prompt root is ``O_NOFOLLOW`` on the open itself.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / ".kiro").mkdir()
        (tmp_path / ".kiro" / "prompts").symlink_to(outside)
        monkeypatch.setattr(_prompts_mod, "_linked_prompt_root", lambda d: False)
        (outside / "victim.md").write_text("SECRET")

        if method == "POST":
            resp = asyncio.run(
                api_prompts_create(
                    _create_request({"name": "x", "content": "c", "scope": "global"})
                )
            )
        else:
            resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "victim")))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "linked_prompt_root"
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (outside / "victim.md").read_text() == "SECRET"
        assert not (outside / "x.md").exists()

    def test_a_create_whose_close_fails_publishes_nothing_and_stays_retryable(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """A DEFERRED write error cannot leave anything behind, named or not.

        ``fsync`` and ``close`` are where a filesystem is entitled to first
        report a failure it deferred -- ENOSPC once the last block is flushed,
        EIO on a network mount -- so guarding the write loop alone does not cover
        it. The injected failure closes the descriptor for real and then raises,
        which is what the kernel does: POSIX leaves the descriptor's state
        unspecified after a failed close and Linux releases it either way.

        Retryability is the property under test. Anything left under the
        prompt's own name answers every later create 409 ``prompt_exists`` over a
        truncated body, and the API offers no way to remove a file it will not
        admit it wrote. The empty-directory assertion is exhaustive on purpose:
        on the unnamed path there is no temp entry to tolerate either.
        """
        real_fsync, real_close = os.fsync, os.close
        failed: list[str] = []

        def _fsync_fails(fd):
            failed.append("fsync")
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(os, "fsync", _fsync_fails)
        first = asyncio.run(
            api_prompts_create(_create_request({"name": "half", "content": "abcdefgh"}))
        )
        # Restored one name at a time, NOT through ``monkeypatch.undo()``: this
        # test and the autouse home-isolation fixture share one function-scoped
        # monkeypatch instance, so an undo here also reverts ``Path.home`` and
        # the retry below would create the prompt in the developer's REAL
        # ``~/.kiro/prompts`` while these assertions read ``tmp_path``.
        monkeypatch.setattr(os, "fsync", real_fsync)
        monkeypatch.setattr(os, "close", real_close)

        assert failed, "the flush never failed — the test would be vacuous"
        assert first.status == 500 and json.loads(first.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"
        assert list((tmp_path / ".kiro" / "prompts").iterdir()) == []

        second = asyncio.run(
            api_prompts_create(_create_request({"name": "half", "content": "abcdefgh"}))
        )
        assert second.status == 201
        assert (tmp_path / ".kiro" / "prompts" / "half.md").read_text() == "abcdefgh"
        assert [p.name for p in (tmp_path / ".kiro" / "prompts").iterdir()] == ["half.md"]

    @pytest.mark.skipif(
        not _prompts_mod._UNNAMED_CREATE_SUPPORTED or not os.path.isdir("/proc/self/fd"),
        reason="platform cannot build an unnamed inode (O_TMPFILE + /proc/self/fd)",
    )
    def test_the_published_prompt_has_exactly_one_link(self, tmp_path, mock_sel):
        """The prompt this handler creates is readable by its own read path.

        ``safe_read_file_bytes_nolink`` refuses ``st_nlink > 1``, so a publish
        that routes the body through a second name -- write a temp, link it into
        place, remove the temp -- leaves a window in which a crash strands a
        prompt that answers 403 forever. Building the inode UNNAMED and linking
        it once means the count goes from zero to one and is never two, so there
        is no such window and no temp to reclaim.
        """
        resp = asyncio.run(api_prompts_create(_create_request({"name": "solo", "content": "S"})))
        assert resp.status == 201
        d = tmp_path / ".kiro" / "prompts"
        published = d / "solo.md"
        assert published.stat().st_nlink == 1
        assert [p.name for p in d.iterdir()] == ["solo.md"]
        # Readable through the endpoint that enforces the link count.
        detail = asyncio.run(api_prompt_detail(_api_request("solo")))
        assert detail.status == 200 and json.loads(detail.body)["content"] == "S"

    def test_the_capability_constant_touches_no_filesystem(self):
        """``_UNNAMED_CREATE_SUPPORTED`` is settled by attribute lookups alone.

        This module is imported on the gateway boot path, where every statement
        runs once on one thread before the dashboard socket accepts requests --
        so a filesystem probe here is paid by every user on every launch
        (``no-new-work-on-gateway-boot-path``). The ``/proc/self/fd`` half of the
        capability is a mount question, so it belongs in the write job, which
        already runs in the executor.

        Read statically off the module's own source rather than by executing it:
        the assignment's expression may name only capability attributes and may
        call only ``getattr``/``bool``, so putting a ``stat`` back into it fails
        here no matter how the call is spelled.
        """
        tree = ast.parse(Path(_prompts_mod.__file__).read_text(encoding="utf-8"))
        assign = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "_UNNAMED_CREATE_SUPPORTED"
                for t in node.targets
            )
        )
        reads = {
            ast.unparse(sub) for sub in ast.walk(assign.value) if isinstance(sub, ast.Attribute)
        }
        calls = {
            ast.unparse(sub.func) for sub in ast.walk(assign.value) if isinstance(sub, ast.Call)
        }
        assert reads <= {
            "os.O_TMPFILE",
            "os.link",
            "os.supports_dir_fd",
        }, f"boot-path constant reads more than capability attributes: {sorted(reads)}"
        assert calls <= {
            "getattr",
            "bool",
        }, f"boot-path constant calls something that may touch the filesystem: {sorted(calls)}"

    def test_the_body_is_durable_before_the_name_appears(self, tmp_path, mock_sel, monkeypatch):
        """The flush precedes the publish, and the DIRECTORY is flushed too.

        201 says the prompt is on disk. Publishing first and flushing after
        inverts that: the entry is visible, the lister shows it, and the error a
        full or network-backed filesystem was deferring arrives afterwards --
        reported success over a body that never landed.

        Flushing only the file is the subtler half of the same promise. A
        directory is a separate object, so an inode that is durable under a name
        that is not comes back from a power loss with the body intact and nothing
        pointing at it -- acknowledged, then vanished. Both flushes are the
        contract, so the whole order is what is asserted.
        """
        order: list[str] = []
        real_fsync, real_link = os.fsync, os.link

        def _note_fsync(fd):
            # Distinguish the file's flush from the directory's by asking the
            # descriptor what it is, rather than by call order.
            order.append("fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file")
            return real_fsync(fd)

        def _note_link(*a, **kw):
            order.append("publish")
            return real_link(*a, **kw)

        monkeypatch.setattr(os, "fsync", _note_fsync)
        monkeypatch.setattr(os, "link", _note_link)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "durable", "content": "B"})))
        monkeypatch.setattr(os, "fsync", real_fsync)
        monkeypatch.setattr(os, "link", real_link)

        assert resp.status == 201
        assert order[:2] == ["fsync_file", "publish"], f"flush must precede publish, got {order}"
        assert "fsync_dir" in order, f"the directory entry was never flushed: {order}"
        assert order.index("fsync_dir") > order.index(
            "publish"
        ), f"the directory flush must follow the publish it is making durable: {order}"
        assert (tmp_path / ".kiro" / "prompts" / "durable.md").read_text() == "B"

    def test_a_failing_directory_flush_is_reported_not_swallowed(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """A create that cannot flush the ENTRY must not answer 201.

        201 is a claim the prompt is on disk. Flushing the body but only logging a
        failed directory flush reports a durability this call never established --
        the same reported-success-before-durable class the whole change exists to
        close. An earlier revision swallowed it on the theory that 500 would strand
        the caller in the 409-forever dead end; that was wrong. The body is already
        written, flushed and linked by this point, so the caller sees a complete
        readable prompt and a retry gets a truthful 409 on a prompt that genuinely
        exists and remains editable through the update path -- unlike the truncated
        leftover that made the original 409 a dead end.

        The directory descriptor is picked out by ``fstat`` rather than by call
        order, so the injection cannot hit the body's flush by accident.
        """
        real_fsync = os.fsync
        failed = {"dir": False}

        def _dir_flush_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                failed["dir"] = True
                raise OSError(errno.EIO, "Input/output error")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _dir_flush_fails)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "unflushed", "content": "BODY"}))
        )
        monkeypatch.setattr(os, "fsync", real_fsync)

        assert failed["dir"], "the directory flush never failed — the test would be vacuous"
        assert resp.status == 500, "a create that could not flush its entry reported success"
        assert json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    @pytest.fixture()
    def force_named_fallback(self, monkeypatch):
        """Run the by-name branch on a filesystem that would take the unnamed one.

        `O_TMPFILE` is present on every Linux runner, so without this the whole
        fallback -- its `O_EXCL` create, its two flushes, and its identity-checked
        cleanup -- executes only on filesystems no CI job uses, which is how it
        came to ship unverified. Forcing the capability off is what lets the same
        assertions run here as would run on an NFS home.
        """
        monkeypatch.setattr(_prompts_mod, "_UNNAMED_CREATE_SUPPORTED", False)

    def test_the_named_fallback_publishes_a_correct_prompt(
        self, tmp_path, mock_sel, force_named_fallback
    ):
        """The path taken where the mount has no O_TMPFILE still creates cleanly."""
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "byname", "content": "FALLBACK"}))
        )

        assert resp.status == 201
        path = tmp_path / ".kiro" / "prompts" / "byname.md"
        assert path.read_text() == "FALLBACK"
        st = os.stat(path)
        assert st.st_nlink == 1
        assert stat.S_IMODE(st.st_mode) == 0o644

    def test_the_named_fallback_still_refuses_an_occupied_name(
        self, tmp_path, mock_sel, force_named_fallback
    ):
        """`O_EXCL` carries the same create-if-absent promise `link` gives the
        unnamed path, so an occupied name is a 409 and the resident is untouched."""
        _user_prompt(tmp_path, "byname", "RESIDENT\n")

        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "byname", "content": "INTRUDER"}))
        )

        assert resp.status == 409
        assert (tmp_path / ".kiro" / "prompts" / "byname.md").read_text() == "RESIDENT\n"

    def test_the_named_fallback_withdraws_a_prompt_it_cannot_flush(
        self, tmp_path, mock_sel, monkeypatch, force_named_fallback
    ):
        """A failed directory flush here removes the leaf as well as answering 500.

        This is the half of the asymmetry the unnamed path cannot offer. This
        branch already holds a cleanup arm bound to the inode it created, so it can
        take the publication back and leave the caller a clean retry rather than a
        prompt whose entry may not have landed. The unnamed path deliberately
        keeps its leaf, because withdrawing there would mean unlinking the
        prompt's own name -- the hazard that branch exists to avoid.
        """
        real_fsync = os.fsync
        failed = {"dir": False}

        def _dir_flush_fails(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                failed["dir"] = True
                raise OSError(errno.EIO, "Input/output error")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _dir_flush_fails)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "withdrawn", "content": "BODY"}))
        )
        monkeypatch.setattr(os, "fsync", real_fsync)

        assert failed["dir"], "the directory flush never failed — the test would be vacuous"
        assert resp.status == 500
        assert json.loads(resp.body)["code"] == "write_failed"
        assert not (
            tmp_path / ".kiro" / "prompts" / "withdrawn.md"
        ).exists(), "the fallback left behind a prompt whose entry it could not flush"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_a_create_cannot_disturb_a_prompt_already_at_the_name(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The publish is create-if-absent, and a failure never reaches the name.

        Both halves matter, because the obvious alternative fails one or the
        other. Writing O_EXCL straight onto the prompt's own name and unlinking
        on failure has no way to bind the unlink to the inode it created (POSIX
        has no unlink-by-inode, so the verify and the unlink are two syscalls on
        one NAME), so an atomic save landing between them loses the replacement;
        dropping that cleanup instead strands a partial body and answers every
        retry 409 forever. Publishing an unnamed inode with ``link`` has neither
        problem: the name appears only on success, and ``link`` refuses an
        occupied destination rather than writing through it.
        """
        d = tmp_path / ".kiro" / "prompts"
        d.mkdir(parents=True)
        rival = d / "rival.md"
        rival.write_text("NOT YOURS")
        before = rival.stat()

        # A create losing the race for a name someone else holds is refused, and
        # the incumbent is untouched -- same inode, same bytes.
        taken = asyncio.run(
            api_prompts_create(_create_request({"name": "rival", "content": "MINE"}))
        )
        assert taken.status == 409 and json.loads(taken.body)["code"] == "prompt_exists"
        assert rival.read_text() == "NOT YOURS"
        assert (rival.stat().st_dev, rival.stat().st_ino) == (before.st_dev, before.st_ino)
        assert [p.name for p in d.iterdir()] == ["rival.md"]

        # And a create that FAILS mid-write never had the name to lose: the
        # incumbent survives and no debris is left beside it.
        real_write = os.write
        failed = {"done": False}

        def _fail_the_body(fd, data):
            if data == b"MINE" and not failed["done"]:
                failed["done"] = True
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", _fail_the_body)
        broke = asyncio.run(
            api_prompts_create(_create_request({"name": "rival", "content": "MINE"}))
        )
        monkeypatch.setattr(os, "write", real_write)

        assert failed["done"], "the write never failed — the test would be vacuous"
        assert broke.status == 500 and json.loads(broke.body)["code"] == "write_failed"
        assert rival.read_text() == "NOT YOURS"
        assert [p.name for p in d.iterdir()] == ["rival.md"]


class TestARefusedUpdateIsNotReportedAsSuccess:
    """The writer fails CLOSED by returning False, so the write dispatch must
    answer that. Falling through to the success response is the worst outcome
    available: the caller is told the edit landed while the file still holds the
    original, so they close the editor and lose the change."""

    def test_a_refused_write_answers_403_and_leaves_the_file(self, tmp_path, mock_sel, monkeypatch):
        _user_prompt(tmp_path, "p", "ORIGINAL\n")
        monkeypatch.setattr(
            _prompts_mod, "verified_replace_file_nolink", lambda *a, **kw: "refused"
        )
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "p", body={"content": "NEW\n", "base_hash": _sha("ORIGINAL\n")}
                )
            )
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "write_refused"
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "ORIGINAL\n"

    def test_a_non_os_failure_in_the_scoped_read_is_still_audited(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The read dispatch makes the same promise, and had no coverage for it."""
        _user_prompt(tmp_path, "p", "ORIGINAL\n")

        def _boom(*a, **kw):
            raise MemoryError("no buffer")

        monkeypatch.setattr(_prompts_mod, "safe_read_file_bytes_nolink", _boom)
        resp = asyncio.run(api_prompt_detail(_write_request("GET", "p", scope="global")))
        assert resp.status == 500
        assert json.loads(resp.body)["code"] == "read_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_a_non_os_failure_in_the_update_is_still_audited(self, tmp_path, mock_sel, monkeypatch):
        """The update dispatch makes the same every-outcome-audited promise as
        create, so its catch has to be as wide."""
        _user_prompt(tmp_path, "p", "ORIGINAL\n")

        def _boom(*a, **kw):
            raise MemoryError("no buffer")

        monkeypatch.setattr(_prompts_mod, "verified_replace_file_nolink", _boom)
        resp = asyncio.run(
            api_prompt_detail(
                _write_request(
                    "PUT", "p", body={"content": "NEW\n", "base_hash": _sha("ORIGINAL\n")}
                )
            )
        )
        assert resp.status == 500
        assert json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text() == "ORIGINAL\n"


class TestByNameFallbackStillWorks:
    """The no-``openat`` branch cannot run on CI's platform, so it is exercised by
    forcing the feature flag off. It gives a narrower guarantee (the leaf junction
    check only), but it must still create, delete, and answer the same codes."""

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)

    def test_create_then_delete_round_trips(self, tmp_path, mock_sel):
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "fallback", "content": "BODY\n", "scope": "global"})
            )
        )
        assert resp.status == 201
        path = tmp_path / ".kiro" / "prompts" / "fallback.md"
        assert path.read_text() == "BODY\n"
        assert asyncio.run(api_prompt_detail(_write_request("DELETE", "fallback"))).status == 200
        assert not path.exists()

    def test_a_linked_root_is_still_refused(self, tmp_path, mock_sel):
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / ".kiro").mkdir()
        (tmp_path / ".kiro" / "prompts").symlink_to(outside)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "x", "content": "c", "scope": "global"}))
        )
        assert resp.status == 403 and json.loads(resp.body)["code"] == "linked_prompt_root"
        assert not (outside / "x.md").exists()

    def test_a_duplicate_is_still_a_conflict(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "dupe", "ORIGINAL\n")
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "dupe", "content": "NEW\n", "scope": "global"})
            )
        )
        assert resp.status == 409 and json.loads(resp.body)["code"] == "prompt_exists"
        assert (tmp_path / ".kiro" / "prompts" / "dupe.md").read_text() == "ORIGINAL\n"

    def test_failed_create_cleans_up_its_own_partial_file(self, tmp_path, mock_sel, monkeypatch):
        """A write failure removes the partial file this create made, so the
        caller's retry is a clean create rather than a permanent 409."""
        original_open = Path.open

        def failing_open(self, mode="r", *args, **kwargs):
            fh = original_open(self, mode, *args, **kwargs)
            if "x" not in mode:
                return fh

            class _Failing:
                def __enter__(s):
                    return s

                def __exit__(s, *exc):
                    fh.close()
                    return False

                def fileno(s):
                    return fh.fileno()

                def write(s, data):
                    raise OSError(28, "No space left on device")

            return _Failing()

        monkeypatch.setattr(Path, "open", failing_open)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "p", "content": "x", "scope": "global"}))
        )
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert not (tmp_path / ".kiro" / "prompts" / "p.md").exists()

    def test_failed_create_does_not_unlink_a_concurrent_replacement(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The failure-path cleanup re-resolves the name, so a concurrent writer
        that replaced the entry inside the failure window must keep its file:
        the unlink is bound to the inode this create made, never the name."""
        original_open = Path.open

        def swapping_open(self, mode="r", *args, **kwargs):
            fh = original_open(self, mode, *args, **kwargs)
            if "x" not in mode:
                return fh

            class _Swapping:
                def __enter__(s):
                    return s

                def __exit__(s, *exc):
                    fh.close()
                    return False

                def fileno(s):
                    return fh.fileno()

                def write(s, data):
                    # A concurrent writer lands an atomic save (staged sibling +
                    # replace, allocating its inode while ours still exists, so
                    # the identities cannot collide), then this write fails.
                    staged = self.with_suffix(".swap")
                    staged.write_text("REPLACEMENT", encoding="utf-8")
                    fh.close()
                    os.replace(staged, self)
                    raise OSError(28, "No space left on device")

            return _Swapping()

        monkeypatch.setattr(Path, "open", swapping_open)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "p", "content": "x", "scope": "global"}))
        )
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert (tmp_path / ".kiro" / "prompts" / "p.md").read_text(
            encoding="utf-8"
        ) == "REPLACEMENT"


class TestAppTokenWriteGate:
    """App tokens must not reach prompt mutations (path-only grants are verb-blind)."""

    def _assert_forbidden(self, resp, mock_sel):
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "app_token_forbidden"
        # AUTOSDE backend-security-controls: every permission decision emits a
        # SEL audit event, coded with the same word the response carries.
        # "blocked" is this module's outcome vocabulary for every 403.
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (
            mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]["reason"]
            == "app_token_forbidden"
        )

    def test_app_token_cannot_create(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, app="someapp")
        self._assert_forbidden(asyncio.run(api_prompts_create(req)), mock_sel)
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_app_token_cannot_update(self, tmp_path, mock_sel):
        """The body carries a VALID base_hash so that, ungated, this request
        would succeed and overwrite — making the file-integrity assertion
        load-bearing rather than satisfied by a downstream 400."""
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request(
            "PUT",
            "keep",
            body={"content": "clobbered", "base_hash": _sha("original")},
            app="someapp",
        )
        self._assert_forbidden(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").read_text(
            encoding="utf-8"
        ) == "original"

    def test_app_token_cannot_delete(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request("DELETE", "keep", app="someapp")
        self._assert_forbidden(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").exists()

    def test_absent_app_claim_fails_closed(self, tmp_path, mock_sel):
        """A request the auth middleware never touched is refused, not trusted."""
        req = _create_request({"name": "x", "content": "b"}, app=None)
        self._assert_forbidden(asyncio.run(api_prompts_create(req)), mock_sel)

    def test_app_token_can_still_read(self, tmp_path, mock_sel):
        """The gate is mutations-only, as the spec sentence scopes it: hoisting
        it above the GET dispatch would revoke app read access with nothing red."""
        _user_prompt(tmp_path, "readable", "body\n")
        req = _write_request("GET", "readable", app="someapp")
        resp = asyncio.run(api_prompt_detail(req))
        assert resp.status == 200
        assert json.loads(resp.body)["content"] == "body\n"

    def test_an_unwritable_sel_still_answers_the_denial(self, tmp_path, mock_sel):
        """The audit is best-effort: SEL failing must not turn the 403 into a 500."""
        mock_sel.log_tool_invocation.side_effect = RuntimeError("SEL unwritable")
        req = _create_request({"name": "x", "content": "b"}, app="someapp")
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "app_token_forbidden"

    def test_dashboard_user_still_writes(self, tmp_path, mock_sel):
        """The gate must not catch the "" dashboard-user claim (regression guard)."""
        req = _create_request({"name": "ok-prompt", "content": "b"})
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 201


class TestNonOwnerWriteGate:
    """Prompt mutations require the CONFIGURED OWNER, not any dashboard user.

    ``!dashboard`` links give allowed messaging users dashboard sessions, so
    "claim present-and-empty" alone would let a non-owner mutate the owner's
    agent instructions. Reads stay open to every dashboard user.
    """

    def _assert_owner_required(self, resp, mock_sel):
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "dashboard_owner_required"
        # Same audit contract as the app-token gate: every permission decision
        # emits a SEL event coded with the word the response carries.
        assert _outcomes(mock_sel)[-1] == "blocked"
        assert (
            mock_sel.log_tool_invocation.call_args_list[-1][1]["metadata"]["reason"]
            == "dashboard_owner_required"
        )

    def test_non_owner_cannot_create(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, user="other-user")
        self._assert_owner_required(asyncio.run(api_prompts_create(req)), mock_sel)
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_non_owner_cannot_update(self, tmp_path, mock_sel):
        """The body carries a VALID base_hash so that, ungated, this request
        would succeed and overwrite — the file-integrity assertion is
        load-bearing rather than satisfied by a downstream 400."""
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request(
            "PUT",
            "keep",
            body={"content": "clobbered", "base_hash": _sha("original")},
            user="other-user",
        )
        self._assert_owner_required(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").read_text(
            encoding="utf-8"
        ) == "original"

    def test_non_owner_cannot_delete(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "keep", "original")
        req = _write_request("DELETE", "keep", user="other-user")
        self._assert_owner_required(asyncio.run(api_prompt_detail(req)), mock_sel)
        assert (tmp_path / ".kiro" / "prompts" / "keep.md").exists()

    def test_non_owner_can_still_read(self, tmp_path, mock_sel):
        """The owner gate is mutations-only, like the app-token gate above it."""
        _user_prompt(tmp_path, "readable", "body\n")
        req = _write_request("GET", "readable", user="other-user")
        resp = asyncio.run(api_prompt_detail(req))
        assert resp.status == 200

    def test_no_owner_configured_admits_local_subject(self, tmp_path, mock_sel):
        """A no-owner install keeps working: the signed local bootstrap subject
        passes the gate — the same rule every other owner-gated surface uses."""
        req = _create_request({"name": "boot", "content": "b"}, user="local-app", owner="")
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 201

    def test_no_owner_configured_still_refuses_unknown_subject(self, tmp_path, mock_sel):
        req = _create_request({"name": "x", "content": "b"}, user="random-user", owner="")
        self._assert_owner_required(asyncio.run(api_prompts_create(req)), mock_sel)


class TestLocalScopeStaysInProject:
    """A repo-authored ``.kiro`` link must not redirect local scope elsewhere.

    The ancestor-link tolerance exists for links the USER made under their own
    tree (a dotfile-managed home). A project ``.kiro`` is the repository
    author's file: a checkout shipping ``.kiro -> ~/.kiro`` would point "This
    project" mutations at the global tree. The resolver refuses any local dir
    that resolves outside the resolved project root.
    """

    def test_project_kiro_linked_to_home_cannot_delete_global(
        self, tmp_path, mock_sel, monkeypatch
    ):
        _user_prompt(tmp_path, "victim", "global body")
        proj = tmp_path / "checkout"
        proj.mkdir()
        (proj / ".kiro").symlink_to(tmp_path / ".kiro", target_is_directory=True)
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)
        resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "victim", scope="local")))
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "linked_prompt_root"
        assert (tmp_path / ".kiro" / "prompts" / "victim.md").exists()

    def test_project_kiro_linked_to_home_cannot_create(self, tmp_path, mock_sel, monkeypatch):
        proj = tmp_path / "checkout"
        proj.mkdir()
        (proj / ".kiro").symlink_to(tmp_path / ".kiro", target_is_directory=True)
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: proj)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "x", "content": "b", "scope": "local"}))
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "linked_prompt_root"
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_symlinked_project_root_itself_still_works(self, tmp_path, mock_sel, monkeypatch):
        """Resolved-to-resolved comparison: a project the user reaches THROUGH a
        link is a location the user chose, and must keep working — this pins
        the design against the over-strict unresolved comparison."""
        real = tmp_path / "real-checkout"
        real.mkdir()
        link = tmp_path / "link-checkout"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: link)
        resp = asyncio.run(
            api_prompts_create(_create_request({"name": "ok", "content": "b", "scope": "local"}))
        )
        assert resp.status == 201
        assert (real / ".kiro" / "prompts" / "ok.md").exists()


class TestFallbackDeleteRace:
    """No-dir-fd delete answers a raced-away file with the coded 404, not a 500."""

    def test_racing_unlink_maps_to_prompt_not_found(self, tmp_path, monkeypatch, mock_sel):
        _user_prompt(tmp_path, "racer", "body")
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        target = tmp_path / ".kiro" / "prompts" / "racer.md"
        original_unlink = Path.unlink

        def racing_unlink(self, *args, **kwargs):
            if self == target:
                # Simulate an external process removing the file between the
                # handler's existence check and this unlink.
                original_unlink(self)
                raise FileNotFoundError(str(self))
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", racing_unlink)
        resp = asyncio.run(api_prompt_detail(_write_request("DELETE", "racer")))
        assert resp.status == 404
        assert json.loads(resp.body)["code"] == "prompt_not_found"

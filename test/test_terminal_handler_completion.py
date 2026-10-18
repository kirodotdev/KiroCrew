"""Tests for the terminal panel's tab completion.

Covers path-token splitting, the completion directory and its vetted
descriptor, the listing and ranking rules, the sensitive-entry gate, the
session cwd memo, ``api_terminal_complete`` with its command tier, and the
completion-enabled flag.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew.dashboard import terminal_commands
from kiro_crew.dashboard.handlers import terminal


class TestSplitPathToken:
    def test_no_slash_is_all_prefix(self):
        assert terminal._split_path_token("Kiro") == ("", "Kiro")

    def test_trailing_slash_has_empty_prefix(self):
        assert terminal._split_path_token("src/") == ("src/", "")

    def test_relative_parent(self):
        assert terminal._split_path_token("../Kiro") == ("../", "Kiro")

    def test_absolute(self):
        assert terminal._split_path_token("/usr/lo") == ("/usr/", "lo")

    def test_empty(self):
        assert terminal._split_path_token("") == ("", "")


class TestResolveCompletionDir:
    def test_empty_dir_part_is_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "") == "/tmp/work"

    def test_relative_resolves_against_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "sub/") == "/tmp/work/sub"

    def test_parent_traversal(self):
        assert terminal._resolve_completion_dir("/tmp/work", "../") == "/tmp"

    def test_absolute_ignores_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "/usr/") == "/usr"

    def test_expands_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert terminal._resolve_completion_dir("/tmp/work", "~/") == str(tmp_path)


class TestListCompletions:
    """Directory listing rules: case-insensitive substring match ranked by match
    offset, dirs first among equals, hidden entries only once the user has typed
    a dot, folders_only narrowing."""

    @pytest.fixture
    def tree(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Beta").mkdir()
        (tmp_path / "aardvark.txt").write_text("x")
        (tmp_path / "zeta.md").write_text("x")
        (tmp_path / ".hidden").mkdir()
        return tmp_path

    def test_lists_dirs_before_files(self, tree):
        entries, truncated = terminal._list_completions(str(tree), "", False, 100)
        assert [e["name"] for e in entries] == ["alpha", "Beta", "aardvark.txt", "zeta.md"]
        assert truncated is False

    def test_folders_only_drops_files(self, tree):
        entries, _ = terminal._list_completions(str(tree), "", True, 100)
        assert [e["name"] for e in entries] == ["alpha", "Beta"]
        assert all(e["dir"] for e in entries)

    def test_prefix_match_is_case_insensitive(self, tree):
        entries, _ = terminal._list_completions(str(tree), "b", False, 100)
        assert [e["name"] for e in entries] == ["Beta"]

    def test_matches_a_fragment_in_the_middle_of_a_name(self, tree):
        # The whole point: a long name is reachable by its distinctive middle.
        entries, _ = terminal._list_completions(str(tree), "dvark", False, 100)
        assert [e["name"] for e in entries] == ["aardvark.txt"]
        assert entries[0]["at"] == 3

    def test_ranks_an_earlier_match_first(self, tree):
        # "a" is a prefix of alpha/aardvark and sits mid-name in Beta/zeta — the
        # prefix hits must lead, and dirs still win among equal offsets.
        entries, _ = terminal._list_completions(str(tree), "a", False, 100)
        assert [(e["name"], e["at"]) for e in entries] == [
            ("alpha", 0),
            ("aardvark.txt", 0),
            ("Beta", 3),
            ("zeta.md", 3),
        ]

    def test_a_dot_fragment_matches_as_a_prefix(self, tree):
        # A leading dot unhides entries; matching it as a substring would drag
        # in every foo.bar and defeat that filter.
        entries, _ = terminal._list_completions(str(tree), ".h", False, 100)
        assert [e["name"] for e in entries] == [".hidden"]

    def test_hides_dotfiles_until_dot_typed(self, tree):
        assert all(
            not e["name"].startswith(".")
            for e in terminal._list_completions(str(tree), "", False, 100)[0]
        )
        entries, _ = terminal._list_completions(str(tree), ".", False, 100)
        assert [e["name"] for e in entries] == [".hidden"]

    def test_truncates_at_limit(self, tree):
        entries, truncated = terminal._list_completions(str(tree), "", False, 2)
        assert len(entries) == 2
        assert truncated is True

    def test_ranking_survives_the_retention_cap(self, tree):
        # The size-bounded heap must keep the SAME top-N the full sort would:
        # dirs before files at equal match offset.
        entries, truncated = terminal._list_completions(str(tree), "a", False, 2)
        assert [e["name"] for e in entries] == ["alpha", "aardvark.txt"]
        assert truncated is True

    def test_scan_cap_marks_truncated(self, tmp_path, monkeypatch):
        # A pathological directory must not be walked to the end; `truncated`
        # stays truthful for the SCAN cap too, not just the retention cap.
        for i in range(6):
            (tmp_path / f"f{i}").write_text("x")
        monkeypatch.setattr(terminal, "_COMPLETE_MAX_SCAN", 3)
        entries, truncated = terminal._list_completions(str(tmp_path), "", False, 100)
        assert len(entries) == 3
        assert truncated is True

    def test_scan_cap_not_reported_when_directory_fits(self, tmp_path, monkeypatch):
        (tmp_path / "only").write_text("x")
        monkeypatch.setattr(terminal, "_COMPLETE_MAX_SCAN", 3)
        assert terminal._list_completions(str(tmp_path), "", False, 100) == (
            [{"name": "only", "dir": False, "at": 0}],
            False,
        )

    def test_excludes_names_with_control_characters(self, tmp_path):
        # The client TYPES the accepted name into the PTY, so a newline would
        # submit a command line. Such names must never reach the client.
        (tmp_path / "safe.txt").write_text("x")
        try:
            (tmp_path / "ev\nil.txt").write_text("x")
            (tmp_path / "esc\x1bape.txt").write_text("x")
        except OSError:  # pragma: no cover — filesystem refuses the name
            pytest.skip("filesystem rejects control characters in names")
        entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_excludes_names_with_lone_surrogates(self, tmp_path):
        # An undecodable byte in a filename survives as a lone surrogate through
        # Python's surrogateescape decoding and through JSON, but the browser's
        # TextEncoder turns it into U+FFFD — the client would then type a path
        # that does not exist.
        (tmp_path / "safe.txt").write_text("x")
        try:
            (tmp_path / b"bad\xffname.txt".decode("utf-8", "surrogateescape")).write_text("x")
        except (OSError, UnicodeEncodeError):  # pragma: no cover — fs refuses the name
            pytest.skip("filesystem rejects undecodable bytes in names")
        entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_filters_lone_surrogates_without_touching_the_filesystem(self, tmp_path):
        # Runs everywhere: macOS refuses to CREATE a name with undecodable bytes,
        # but a network/foreign volume can serve one, so the filter itself is
        # asserted against a synthetic directory read.
        names = ["safe.txt", "bad\udcffname.txt", "bell\x07name.txt"]

        class _Entry:
            def __init__(self, name):
                self.name = name

            def is_dir(self):
                return False

        class _Scandir:
            def __enter__(self):
                return iter([_Entry(n) for n in names])

            def __exit__(self, *exc):
                return False

        with patch.object(terminal.os, "scandir", return_value=_Scandir()):
            entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert terminal._list_completions(str(tmp_path / "nope"), "", False, 100) == ([], False)

    def test_sensitive_directory_yields_nothing(self, tmp_path):
        # ~/.kiro/crew/profiles is trust-root metadata: enumerating it would
        # disclose profile/policy filenames.
        (tmp_path / "leak").mkdir()
        with _sensitive(always=True):
            assert terminal._list_completions(str(tmp_path), "", False, 100) == ([], False)

    def test_symlink_into_sensitive_directory_yields_nothing(self, tmp_path):
        # The name-based check alone would pass: only the realpath of the link
        # lands in the protected tree.
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "profile.json").write_text("x")
        link = tmp_path / "benign"
        link.symlink_to(secret, target_is_directory=True)
        real_check = terminal.is_sensitive_path

        def _fake(path, base_dir=None):
            # Matches ONLY the canonical target, never the link's own name.
            return os.path.realpath(path) == os.path.realpath(str(secret)) or real_check(path)

        with _sensitive(predicate=_fake):
            assert terminal._list_completions(str(link), "", False, 100) == ([], False)


class TestSensitiveEntryGate:
    """Vetting the DIRECTORY is not enough: an allowed directory can hold
    protected children (``~/.kiro/crew`` holds ``security_policy.json``,
    ``profiles/``), so every entry is classified before it is returned.

    These call ``_list_vetted_completions`` so the patched predicate is exercised
    on ENTRIES only — the directory is already vetted by construction."""

    def test_withholds_a_sensitive_child_by_name(self, tmp_path):
        (tmp_path / "safe.txt").write_text("x")
        (tmp_path / "security_policy.json").write_text("x")

        def _fake(path, base_dir=None):
            return os.path.basename(path) == "security_policy.json"

        with _sensitive(predicate=_fake):
            entries, _ = terminal._list_vetted_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_withholds_a_child_symlinked_into_a_protected_tree(self, tmp_path):
        # Name-based classification alone would pass this: only the link's
        # canonical target lands in the protected tree.
        protected = tmp_path / "protected"
        protected.mkdir()
        listed = tmp_path / "listed"
        listed.mkdir()
        (listed / "safe.txt").write_text("x")
        (listed / "shortcut").symlink_to(protected, target_is_directory=True)

        def _fake(path, base_dir=None):
            return os.path.realpath(path) == os.path.realpath(str(protected))

        with _sensitive(predicate=_fake):
            entries, _ = terminal._list_vetted_completions(str(listed), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_withholds_an_entry_that_cannot_be_classified(self, tmp_path):
        # Over-refusing what we cannot classify is the safe direction.
        (tmp_path / "safe.txt").write_text("x")
        with _sensitive(raises=True):
            assert terminal._list_vetted_completions(str(tmp_path), "", False, 100) == ([], False)

    def test_classifies_only_entries_that_matched_the_fragment(self, tmp_path):
        # The gate runs at keystroke rate, so it must not be paid for every name
        # in a large directory — only for names the user could receive.
        for name in ("alpha", "beta", "gamma"):
            (tmp_path / name).write_text("x")
        with _sensitive(always=False) as (gate, _):
            terminal._list_vetted_completions(str(tmp_path), "alph", False, 100)
        assert gate.call_count == 1


@contextlib.contextmanager
def _sensitive(*, predicate=None, always=False, raises=False):
    """Force the sensitive-path verdict at BOTH layers that can decide it.

    The DIRECTORY gate runs inside `hooks.validate_file_path` (the required
    chokepoint), while the per-ENTRY gate calls `is_sensitive_path` directly from
    the terminal module. A test that patched only one of them would silently
    exercise half the guard."""
    import kiro_crew.hooks as hooks_mod

    kw = {}
    if raises:
        kw["side_effect"] = OSError
    elif predicate is not None:
        kw["side_effect"] = predicate
    else:
        kw["return_value"] = bool(always)
    with (
        patch.object(terminal, "is_sensitive_path", **kw) as a,
        patch.object(hooks_mod, "is_sensitive_path", **kw) as b,
    ):
        yield a, b


class TestOpenVettedDir:
    """The scan is pinned to a descriptor so a name swap after vetting cannot
    redirect it, and the open itself is verified against the vetted name."""

    def test_returns_a_descriptor_for_the_vetted_directory(self, tmp_path):
        fd = terminal._open_vetted_dir(str(tmp_path))
        assert fd is not None
        try:
            opened = os.fstat(fd)
            named = os.stat(tmp_path)
            assert (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        finally:
            os.close(fd)

    def test_refuses_a_missing_directory(self, tmp_path):
        assert terminal._open_vetted_dir(str(tmp_path / "gone")) is None

    def test_refuses_a_file(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x")
        assert terminal._open_vetted_dir(str(target)) is None

    def test_refuses_a_symlinked_final_component(self, tmp_path):
        # realpath guaranteed at vet time that the last component was not a link;
        # if it is one now, the name was swapped underneath us.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert terminal._open_vetted_dir(str(link)) is None

    def test_refuses_when_the_name_no_longer_matches_the_descriptor(self, tmp_path):
        """The swap window between open() and the check must fail closed."""
        real = tmp_path / "real"
        real.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        real_st = os.stat(real)
        other_st = os.stat(other)
        assert (real_st.st_dev, real_st.st_ino) != (other_st.st_dev, other_st.st_ino)
        # Simulate the name resolving elsewhere after the descriptor was opened.
        with patch.object(terminal.os, "stat", return_value=other_st):
            assert terminal._open_vetted_dir(str(real)) is None

    def test_listing_scans_the_descriptor_not_the_path(self, tmp_path):
        """Regression: a swap after vetting must not redirect the enumeration.

        `scandir` is asserted to receive the pinned fd (an int), never the path
        string — passing the string is what reopened the name and reintroduced
        the race."""
        (tmp_path / "visible").mkdir()
        seen: list[object] = []
        real_scandir = terminal.os.scandir

        def spy(arg):
            seen.append(arg)
            return real_scandir(arg)

        with patch.object(terminal.os, "scandir", spy):
            entries, _ = terminal._list_vetted_completions(str(tmp_path), "", False, 10)
        assert [e["name"] for e in entries] == ["visible"]
        assert seen and all(isinstance(a, int) for a in seen)

    def test_listing_yields_nothing_when_the_directory_cannot_be_pinned(self, tmp_path):
        with patch.object(terminal, "_open_vetted_dir", return_value=None):
            assert terminal._list_vetted_completions(str(tmp_path), "", False, 10) == ([], False)

    def test_listing_closes_the_descriptor(self, tmp_path):
        (tmp_path / "a").mkdir()
        closed: list[int] = []
        real_close = terminal.os.close

        def spy(fd):
            closed.append(fd)
            return real_close(fd)

        with patch.object(terminal.os, "close", spy):
            terminal._list_vetted_completions(str(tmp_path), "", False, 10)
        assert closed, "the pinned descriptor must be closed"


class TestVettedCompletionDir:
    def test_routes_through_the_hooks_chokepoint(self, tmp_path):
        """The backend security rules require file reads to pass through
        hooks.py rather than re-deriving its realpath + is_sensitive_path pair."""
        with patch.object(terminal, "validate_file_path", return_value=None) as gate:
            assert terminal._vetted_completion_dir(str(tmp_path)) is None
        gate.assert_called_once_with(str(tmp_path))

    def test_returns_canonical_path(self, tmp_path):
        assert terminal._vetted_completion_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))

    def test_rejects_sensitive_path(self, tmp_path):
        with _sensitive(always=True):
            assert terminal._vetted_completion_dir(str(tmp_path)) is None

    def test_rejects_when_canonicalization_fails(self, tmp_path):
        # Over-refusing a path we cannot canonicalize is the safe direction.
        with patch.object(terminal.os.path, "realpath", side_effect=OSError):
            assert terminal._vetted_completion_dir(str(tmp_path)) is None


class TestSessionCwdCached:
    @pytest.mark.asyncio
    async def test_probes_once_within_ttl(self):
        sess = _make_session()
        with patch.object(terminal, "_session_cwd", return_value="/tmp/a") as probe:
            assert await terminal._session_cwd_cached(sess) == "/tmp/a"
            # flake-ok: pre-existing memo hit that holds only while this second call lands inside the 0.4 s real-clock TTL across an executor hop, moved verbatim; tracked for a de-flake pass
            assert await terminal._session_cwd_cached(sess) == "/tmp/a"
        assert probe.call_count == 1

    @pytest.mark.asyncio
    async def test_reprobes_after_ttl_expires(self):
        # A `cd` must be visible to the next completion — a stale memo would
        # list the previous directory.
        sess = _make_session()
        # flake-ok: pre-existing real-clock stamp past the 0.4 s TTL so the memoized cwd reads as expired and is reprobed, moved verbatim; tracked for a de-flake pass
        sess.cwd_probe = (time.monotonic() - terminal._CWD_PROBE_TTL_S - 1, "/tmp/old")
        with patch.object(terminal, "_session_cwd", return_value="/tmp/new"):
            assert await terminal._session_cwd_cached(sess) == "/tmp/new"


class TestApiTerminalComplete:
    """POST /api/terminal/complete — path completions for a live PTY session."""

    @pytest.fixture(autouse=True)
    def sel_log(self):
        """Every outcome of this route audits, so the sink is patched for the
        whole class rather than per test."""
        with patch.object(terminal, "_sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            yield log

    def _req(self, body, user="testuser", registry=None):
        req = _make_request(user=user, registry=registry)
        req.json = AsyncMock(return_value=body)
        return req

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = self._req({"session_id": "s1"}, user=None)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self):
        req = self._req({"session_id": "s1"})
        with (
            patch.object(terminal, "_is_enabled", return_value=False),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_malformed_body(self):
        req = self._req({"session_id": 42})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_oversized_token(self):
        req = self._req({"session_id": "s1", "token": "x" * (terminal._COMPLETE_TOKEN_MAX + 1)})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 413

    @pytest.mark.asyncio
    async def test_unknown_session_is_404(self):
        # Requiring a live PTY is what keeps this from being a general
        # filesystem-enumeration endpoint.
        req = self._req({"session_id": "nope", "token": ""}, registry={})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_empty_when_cwd_unknown(self):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "x"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "dir": None,
            "prefix": "x",
            "entries": [],
            "truncated": False,
        }

    @pytest.mark.asyncio
    async def test_lists_matching_entries(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        (tmp_path / "other").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert body["dir"] == str(tmp_path)
        assert body["prefix"] == "doc"
        assert [e["name"] for e in body["entries"]] == ["docs", "doc.txt"]

    @pytest.mark.asyncio
    async def test_folders_only_narrows_listing(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "doc", "folders_only": True}, registry={"s1": sess}
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    async def test_resolves_token_directory_against_session_cwd(self, tmp_path):
        (tmp_path / "work").mkdir()
        (tmp_path / "sibling").mkdir()
        sess = _make_session(session_id="s1")
        # `cd ../s` typed from tmp_path/work resolves into tmp_path.
        req = self._req({"session_id": "s1", "token": "../s"}, registry={"s1": sess})
        with patch.object(
            terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path / "work"))
        ):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert body["dir"] == str(tmp_path)
        assert body["prefix"] == "s"
        assert [e["name"] for e in body["entries"]] == ["sibling"]

    @pytest.mark.asyncio
    async def test_sensitive_directory_returns_empty_listing_shape(self, tmp_path):
        # Same shape as the unknown-cwd branch: the client needs no special case,
        # and the response does not reveal whether the path exists.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "p"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            _sensitive(always=True),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "dir": None,
            "prefix": "p",
            "entries": [],
            "truncated": False,
        }

    @pytest.mark.asyncio
    async def test_audits_sensitive_path_refusal(self, tmp_path, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "p"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            _sensitive(always=True),
        ):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs == {
            "caller": "testuser",
            "operation": "terminal.complete",
            "outcome": "denied",
            "source": "dashboard",
            "resources": "sensitive_path",
        }

    @pytest.mark.asyncio
    async def test_audits_successful_listing_without_leaking_contents(self, tmp_path, sel_log):
        # The audit payload is deliberately coarse: this route fires per
        # keystroke, so the token and any filenames must stay out of the log.
        (tmp_path / "secretname").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "secret"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["secretname"]
        kwargs = sel_log.call_args.kwargs
        assert kwargs["operation"] == "terminal.complete"
        assert kwargs["outcome"] == "ok"
        assert kwargs["resources"] == "listed"
        payload = json.dumps(kwargs)
        assert "secret" not in payload
        assert str(tmp_path) not in payload

    @pytest.mark.asyncio
    async def test_audits_unknown_cwd_and_unknown_session(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "x"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["resources"] == "no_cwd"
        await terminal.api_terminal_complete(
            self._req({"session_id": "gone", "token": ""}, registry={})
        )
        assert sel_log.call_args.kwargs["resources"] == "unknown_session"

    @pytest.mark.asyncio
    async def test_lists_on_discovery_pool_not_subprocess_pool(self, tmp_path):
        # subprocess_executor's workers are shared with PTY teardown; a slow
        # directory scan must not be able to occupy one.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "discovery_executor") as disc,
            patch.object(terminal, "subprocess_executor") as sub,
        ):
            disc.return_value = None  # None → loop's default executor
            await terminal.api_terminal_complete(req)
        assert disc.called
        assert not sub.called

    @pytest.mark.asyncio
    async def test_resolution_and_vetting_run_off_loop_in_one_hop(self, tmp_path):
        # expanduser (a "~user" form triggers a synchronous name-service lookup)
        # and realpath (can stall on an unresponsive mount) are as blocking as
        # the scan, so neither may run inline in the coroutine — and all three
        # share ONE executor hop so a keystroke costs one thread round-trip.
        loop_thread = threading.current_thread()
        seen: list[tuple[str, object]] = []
        real_resolve = terminal._resolve_completion_dir
        real_vet = terminal._vetted_completion_dir

        def _resolve(cwd, dir_part):
            seen.append(("resolve", threading.current_thread()))
            return real_resolve(cwd, dir_part)

        def _vet(directory):
            seen.append(("vet", threading.current_thread()))
            return real_vet(directory)

        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_resolve_completion_dir", side_effect=_resolve),
            patch.object(terminal, "_vetted_completion_dir", side_effect=_vet),
            patch.object(terminal, "discovery_executor", return_value=None) as disc,
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [step for step, _ in seen] == ["resolve", "vet"]
        assert all(thread is not loop_thread for _, thread in seen)
        # ONE hop for the trio is pinned by thread IDENTITY, not by counting
        # `discovery_executor` calls: the completion gate's read is the first such
        # call on every request (it must precede any filesystem work, so it cannot
        # share this hop), and a count would conflate the two and pass for a
        # resolution that had been split across two threads.
        assert len({thread for _, thread in seen}) == 1
        assert disc.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], "0"])
    async def test_rejects_non_boolean_folders_only(self, value):
        # bool("false") is True: coercing would silently drop every file from
        # the listing for a client that sent the JSON string.
        req = self._req({"session_id": "s1", "token": "", "folders_only": value})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_accepts_real_boolean_folders_only(self, tmp_path, value):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "doc", "folders_only": value}, registry={"s1": sess}
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        names = [e["name"] for e in json.loads(resp.text)["entries"]]
        assert names == (["docs"] if value else ["docs", "doc.txt"])

    @pytest.mark.asyncio
    async def test_omitted_folders_only_defaults_to_files_included(self, tmp_path):
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["doc.txt"]

    @pytest.mark.asyncio
    async def test_a_path_listing_carries_no_entry_kind(self, tmp_path):
        # The client drops an entry that does not belong to the tier it asked for,
        # so the path tier must not start emitting `kind` — that would silently
        # empty every path menu.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert all("kind" not in e for e in json.loads(resp.text)["entries"])


class TestApiTerminalCompleteCommandTier:
    """POST /api/terminal/complete with `argv` — subcommand and flag completions.

    The engine itself is covered in test_terminal_commands.py; this class covers
    the ROUTE: tier selection, the audit vocabulary, and the response shape.
    """

    @pytest.fixture(autouse=True)
    def sel_log(self):
        with patch.object(terminal, "_sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            yield log

    def _req(self, body, user="testuser", registry=None):
        req = _make_request(user=user, registry=registry)
        req.json = AsyncMock(return_value=body)
        return req

    @staticmethod
    def _entries(*names):
        return [terminal_commands.CmdEntry(n, f"about {n}", n.startswith("-")) for n in names]

    def _complete(self, entries, reason="cmd_listed"):
        return patch.object(
            terminal_commands,
            "complete",
            AsyncMock(return_value=(entries, reason)),
        )

    @pytest.mark.asyncio
    async def test_argv_selects_the_command_tier(self, tmp_path):
        # A real directory is present, so answering with subcommands rather than its
        # contents proves the tier was chosen by `argv`, not by what happened to be
        # on disk.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh", "pr"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            self._complete(self._entries("create", "checkout")),
        ):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert resp.status == 200
        assert [e["name"] for e in body["entries"]] == ["create", "checkout"]
        assert all(e["kind"] == "sub" for e in body["entries"])
        # `dir: null` is the existing "nothing resolved on the filesystem" signal,
        # reused so the response needs no new top-level field.
        assert body["dir"] is None
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_passes_the_token_and_cwd_through_to_the_engine(self, tmp_path):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=(self._entries("create"), "cmd_listed"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal_commands, "complete", engine),
        ):
            await terminal.api_terminal_complete(req)
        assert engine.await_args.args[:3] == (["gh", "pr"], "cre", str(tmp_path))

    @pytest.mark.asyncio
    async def test_answers_flags_for_a_dash_token(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "--ti", "argv": ["gh", "pr", "create"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            self._complete(self._entries("--title")),
        ):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert [(e["name"], e["kind"]) for e in body["entries"]] == [("--title", "flag")]
        assert body["prefix"] == "--ti"

    @pytest.mark.asyncio
    async def test_answers_even_when_the_cwd_cannot_be_read(self):
        # A subcommand list does not depend on the working directory, so this branch
        # is ordered BEFORE the path tier's unknown-cwd refusal.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)),
            self._complete(self._entries("pr")),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["pr"]

    @pytest.mark.asyncio
    async def test_does_not_fall_back_to_a_path_listing(self, tmp_path):
        # The tiers are disjoint. Falling through would list the cwd for a word the
        # client already decided cannot be a path.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            self._complete([], "cmd_none"),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert json.loads(resp.text)["entries"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("argv", [[], "gh", ["./gh"], ["/usr/bin/gh"], [""], ["gh\n"], [7], {}])
    async def test_rejects_a_malformed_argv_with_400(self, argv):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": argv},
            registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_the_rejection_carries_a_machine_readable_code(self):
        # The dashboard renders `error` verbatim into a localized UI, so the prose
        # alone is untranslatable by construction; `code` is the contract.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]},
            registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            resp = await terminal.api_terminal_complete(req)
        assert json.loads(resp.text)["code"] == "terminal_invalid_argv"

    @pytest.mark.asyncio
    async def test_a_malformed_argv_never_reaches_the_engine(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]},
            registry={"s1": sess},
        )
        engine = AsyncMock()
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal_commands, "complete", engine),
        ):
            await terminal.api_terminal_complete(req)
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_path_tier_is_untouched_when_argv_is_absent(self, tmp_path):
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        engine = AsyncMock()
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal_commands, "complete", engine),
        ):
            resp = await terminal.api_terminal_complete(req)
        engine.assert_not_awaited()
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    async def test_passes_the_operator_command_map_to_the_engine(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["mytool"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(
                terminal,
                "_get_config",
                return_value={"completion": {"commands": {"mytool": "cobra"}}},
            ),
            patch.object(terminal_commands, "complete", engine),
        ):
            await terminal.api_terminal_complete(req)
        assert engine.await_args.args[3] == {"mytool": "cobra"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [False, True, "yes", 7, [], None])
    async def test_a_non_object_completion_config_is_not_an_error(self, value):
        # config.json is hand-edited: `"completion": false` would make a chained
        # `.get` raise on a boolean — an HTTP 500 on a keystroke from a typo. A
        # non-object value means "no operator additions", which is also the default.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": value}),
            patch.object(terminal_commands, "complete", engine),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    @pytest.mark.asyncio
    async def test_reads_the_config_off_the_event_loop(self):
        # `_get_config` does a synchronous `read_text()` of config.json and this
        # route fires per keystroke, so an inline read would stall every gateway
        # task on a slow home filesystem. Asserts the thread, not the timing.
        import threading

        seen = {}
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )

        def spy(_request):
            seen["thread"] = threading.current_thread().name
            return {}

        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", spy),
            self._complete([], "cmd_unknown"),
        ):
            await terminal.api_terminal_complete(req)
        assert "thread" in seen, "config was never read"
        assert seen["thread"] != threading.current_thread().name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cfg", [False, True, "yes", 7, [], None])
    async def test_a_non_object_terminal_config_is_not_an_error(self, cfg):
        # Both levels are type-checked: `"terminal": false` would make
        # `.get("completion")` raise on a boolean, an HTTP 500 from a typo.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value=cfg),
            patch.object(terminal_commands, "complete", engine),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    @pytest.mark.asyncio
    async def test_a_missing_completion_config_is_not_an_error(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={}),
            patch.object(terminal_commands, "complete", engine),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    # ── Audit ──
    # The rule for this route is a FIXED, content-free reason vocabulary: it fires
    # per keystroke, so a reason naming the command or flag would put the user's
    # command line in the audit trail.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason,outcome",
        [("cmd_listed", "ok"), ("cmd_none", "ok"), ("cmd_unknown", "ok")],
    )
    async def test_audits_each_engine_verdict(self, reason, outcome, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            self._complete(self._entries("create") if reason == "cmd_listed" else [], reason),
        ):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs == {
            "caller": "testuser",
            "operation": "terminal.complete",
            "outcome": outcome,
            "source": "dashboard",
            "resources": reason,
        }

    @pytest.mark.asyncio
    async def test_audits_a_refused_working_directory_as_denied(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            self._complete([], "sensitive_path"),
        ):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["outcome"] == "denied"
        assert sel_log.call_args.kwargs["resources"] == "sensitive_path"

    @pytest.mark.asyncio
    async def test_audits_a_malformed_argv(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]},
            registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["outcome"] == "denied"
        assert sel_log.call_args.kwargs["resources"] == "invalid_argv"

    @pytest.mark.asyncio
    async def test_the_audit_never_records_what_was_typed(self, sel_log):
        # The anti-leak proof, mirroring the path tier's own. Nothing from the
        # command line — command, subcommand, flag, prefix, or an entry name — may
        # appear anywhere in the event.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "--secret-flag", "argv": ["gh", "pr", "sneaky"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w/proj")),
            self._complete(self._entries("--secret-flag")),
        ):
            await terminal.api_terminal_complete(req)
        blob = repr(sel_log.call_args.kwargs)
        for leaked in ("gh", "sneaky", "secret-flag", "/w/proj"):
            assert leaked not in blob
        assert sel_log.call_args.kwargs["resources"] == "cmd_listed"


class TestApiTerminalCompletionEnabledFlag:
    """`dashboard.terminal.completion.enabled` — the popup's own switch.

    Its own class rather than either tier's: the gate is read ABOVE the tier
    split precisely so one key silences both, so tests that assert the path tier
    and the command tier go quiet together belong to neither."""

    @pytest.fixture(autouse=True)
    def sel_log(self):
        with patch.object(terminal, "_sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            yield log

    def _req(self, body, user="testuser", registry=None):
        req = _make_request(user=user, registry=registry)
        req.json = AsyncMock(return_value=body)
        return req

    def _entries(self, *names):
        return [terminal_commands.CmdEntry(n, f"about {n}", n.startswith("-")) for n in names]

    def _complete(self, entries, reason="cmd_listed"):
        return patch.object(
            terminal_commands,
            "complete",
            AsyncMock(return_value=(entries, reason)),
        )

    # ── dashboard.terminal.completion.enabled ──
    # A switch for the popup ALONE: `dashboard.terminal.enabled` also kills the
    # PTY, so it is not a way to silence completions on a terminal you still use.

    _EMPTY = {"dir": None, "entries": [], "truncated": False}

    @pytest.mark.asyncio
    async def test_disabled_completion_silences_the_path_tier(self, tmp_path):
        # The reporter's primary complaint: the `cd ` popup. A real directory is
        # present, so an empty answer proves the gate fired rather than the cwd
        # simply having nothing to offer.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {**self._EMPTY, "prefix": "do"}

    @pytest.mark.asyncio
    async def test_disabled_completion_silences_the_command_tier(self):
        # Gating only the command tier would leave the path popup alive; gating
        # only the path tier would leave this one. Both are covered because the
        # read sits ABOVE the tier split.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=(self._entries("create"), "cmd_listed"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
            patch.object(terminal_commands, "complete", engine),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {**self._EMPTY, "prefix": "cre"}
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_completion_is_an_empty_listing_not_a_403(self, tmp_path):
        # 403 is `_is_enabled`'s whole-panel signal and the client treats it
        # differently; the empty listing is the shape it already renders as "no
        # popup", which is why this needs no frontend change.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_disabled_completion_does_no_filesystem_work(self, tmp_path):
        # The gate sits before the cwd probe: a suppressed keystroke must not pay
        # for an `lsof`/`/proc` read it is going to throw away.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "x"}, registry={"s1": sess})
        probe = AsyncMock(return_value=str(tmp_path))
        with (
            patch.object(terminal, "_session_cwd_cached", probe),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        probe.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_completion_audits_as_ok_not_denied(self, sel_log):
        # Nothing was refused, and naming the state lets an operator tell a
        # configured silence from a broken route.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            await terminal.api_terminal_complete(req)
        kwargs = sel_log.call_args.kwargs
        assert kwargs["outcome"] == "ok"
        assert kwargs["resources"] == "completion_disabled"

    @pytest.mark.asyncio
    async def test_an_absent_enabled_key_preserves_current_behaviour(self, tmp_path):
        # The default must be indistinguishable from before the key existed.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value={"completion": {}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "no", 0, None, [], {}, "true", 1])
    async def test_a_non_boolean_enabled_falls_back_to_the_default(
        self,
        value,
        tmp_path,
    ):
        # config.json is hand-edited and `bool("false") is True`, so coercing
        # would make the JSON STRING "false" mean the opposite of what it reads
        # like. Only a real `false` disables; everything else is "absent".
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": value}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cfg", [False, True, "yes", 7, [], None])
    async def test_a_non_object_terminal_config_does_not_raise_at_the_gate(
        self,
        cfg,
        tmp_path,
    ):
        # `"terminal": false` would make `.get("completion")` raise on a boolean —
        # an HTTP 500 on a keystroke. Covered at the GATE, not only at the command
        # tier, because the read now happens for every request.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value=cfg),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("inner", [False, True, "yes", 7, [], None])
    async def test_a_non_object_completion_config_does_not_raise_at_the_gate(
        self,
        inner,
        tmp_path,
    ):
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))),
            patch.object(terminal, "_get_config", return_value={"completion": inner}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("document", ['{"dashboard": false}', '{"dashboard": 7}', "[]", '"x"'])
    async def test_a_malformed_parent_config_does_not_500_the_route(
        self,
        document,
        tmp_path,
        monkeypatch,
    ):
        # End-to-end companion to the `_get_config` unit tests: the gate now reads
        # config.json on EVERY completion request, so a malformed parent that once
        # raised AttributeError would have been a 500 per keystroke.
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(document)
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "do"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    async def test_the_gate_reads_the_config_off_the_event_loop(self):
        # Every request now pays this read, not just the command tier, so the
        # off-loop guarantee matters more than it did: `_get_config` does a
        # synchronous `read_text()` and this route fires per keystroke.
        seen = {}
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})

        def spy(_request):
            seen["thread"] = threading.current_thread().name
            return {"completion": {"enabled": False}}

        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", spy),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert "thread" in seen, "config was never read"
        assert seen["thread"] != threading.current_thread().name

    @pytest.mark.asyncio
    async def test_the_gate_does_not_touch_the_panel_flag_cache(self):
        # `_enabled_cache` belongs to `_is_enabled`; caching a second flag in the
        # same slot would cross-contaminate them.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        before = list(terminal._enabled_cache)
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            await terminal.api_terminal_complete(req)
        assert list(terminal._enabled_cache) == before

    @pytest.mark.asyncio
    async def test_a_malformed_body_still_400s_with_completion_disabled(self):
        # The gate sits after the body/token/session validations, so a malformed
        # request keeps its 400 instead of a spurious empty 200.
        req = self._req({"session_id": 42})
        with patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_an_oversized_token_still_413s_with_completion_disabled(self):
        req = self._req(
            {"session_id": "s1", "token": "x" * (terminal._COMPLETE_TOKEN_MAX + 1)},
        )
        with patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 413

    @pytest.mark.asyncio
    @pytest.mark.parametrize("argv", [[], "gh", ["./gh"], ["/usr/bin/gh"], [""], ["gh\n"], [7], {}])
    async def test_a_malformed_argv_still_400s_with_completion_disabled(self, argv):
        # `argv` is a body-shape contract, so turning the popup off must not turn a
        # contract violation into a silent 200 — the client would read "malformed
        # request" as "no suggestions" and never learn it sent garbage.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": argv},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "terminal_invalid_argv"

    @pytest.mark.asyncio
    async def test_a_malformed_argv_still_audits_denied_with_completion_disabled(
        self,
        sel_log,
    ):
        # The denial must stay in the SEL trail. A disabled popup that swallowed
        # `invalid_argv` would erase the only record that a caller is broken.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]},
            registry={"s1": sess},
        )
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            await terminal.api_terminal_complete(req)
        kwargs = sel_log.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["resources"] == "invalid_argv"

    @pytest.mark.asyncio
    async def test_a_malformed_argv_never_reaches_the_engine_when_disabled(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]},
            registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
            patch.object(terminal_commands, "complete", engine),
        ):
            await terminal.api_terminal_complete(req)
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_well_formed_argv_is_parsed_once(self):
        # The hoisted parse must be REUSED by the command tier, not repeated: a
        # second parse per keystroke is pure waste on this route.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]},
            registry={"s1": sess},
        )
        parse = MagicMock(side_effect=terminal_commands.parse_argv)
        with (
            patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")),
            patch.object(terminal, "_get_config", return_value={}),
            patch.object(terminal_commands, "parse_argv", parse),
            self._complete(self._entries("create")),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert parse.call_count == 1

    @pytest.mark.asyncio
    async def test_an_unknown_session_still_404s_with_completion_disabled(self):
        req = self._req({"session_id": "nope", "token": ""}, registry={})
        with patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_the_panel_flag_still_wins_over_the_completion_flag(self):
        # `dashboard.terminal.enabled = false` is the whole-panel refusal and must
        # keep answering 403, not the completion gate's empty listing.
        req = self._req({"session_id": "s1", "token": ""})
        with (
            patch.object(terminal, "_is_enabled", return_value=False),
            patch.object(terminal, "_get_config", return_value={"completion": {"enabled": False}}),
        ):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 403

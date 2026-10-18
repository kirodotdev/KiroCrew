"""Tests for prompts (agent SOPs) discovery."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from prompts_helpers import _isolate_home  # noqa: F401
from prompts_helpers import _no_turn_tail  # noqa: F401
from prompts_helpers import aim_dir_fixture  # noqa: F401
from prompts_helpers import block_sensitive_discovery_fixture  # noqa: F401
from prompts_helpers import block_sensitive_reads_fixture  # noqa: F401
from prompts_helpers import mock_sel_fixture  # noqa: F401
from prompts_helpers import (
    _aim_pkg,
    _api_request,
    _list_request,
    _Slot,
    _ss,
    _State,
    _user_prompt,
    _write_request,
)

from conftest import requires_symlinks
from kiro_crew.dashboard.chat import _expand_prompt_mention, _run_chat
from kiro_crew.dashboard.handlers import (
    MAX_PROMPT_BYTES,
    _extract_sop_description,
    _list_aim_prompts,
    api_prompt_detail,
    api_prompts,
)
from kiro_crew.dashboard.handlers import prompts as _prompts_mod
from kiro_crew.platform_compat import IS_POSIX

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

    def test_discovers_local_project_prompts(self, tmp_path):
        # Local prompts now come from the per-slot project the CALLER resolves
        # and passes in, not from the gateway-global _project_dir(); drive it
        # through the new project_dir argument.
        proj = tmp_path / "proj"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "local.md").write_text("# L\n")
        assert any(r["source"] == "local" for r in _list_aim_prompts(proj))
        # With no project_dir the same local prompt is NOT discovered.
        assert not any(r["source"] == "local" for r in _list_aim_prompts())

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


# ── user-prompt discovery gate ──


class TestUserPromptDiscoveryGate:
    """A user prompt exists only when its name resolves to a plain file inside its
    own prompt directory.

    A project's ``.kiro/prompts`` is content the user CLONED, so the repository's
    author chooses what is in it. Every consumer of a discovered entry reads its
    ``path`` — the listing publishes a description drawn from the bytes, and
    ``@mention`` injects the whole file into an agent turn — so a name that
    resolves out of the directory must not become a prompt at all, in EITHER the
    scan or the exact-name lookup.
    """

    @staticmethod
    def _project_prompts(tmp_path):
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        return proj, d

    def test_a_planted_symlink_out_of_the_dir_is_not_a_prompt(self, tmp_path):
        """The listing walk must not follow a repo-authored link out of the tree."""
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)

        entries = _list_aim_prompts(proj)
        assert entries == [], "an escaping symlink was published as a prompt"

    def test_the_escaped_target_is_not_described_either(self, tmp_path):
        """Not merely unnamed: the target's own heading must not reach the client."""
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)
        # A real prompt beside it, so an empty list cannot be a scan that failed.
        (d / "real.md").write_text("# Real\n")

        entries = _list_aim_prompts(proj)
        assert [e["name"] for e in entries] == ["real"]
        assert "AWS keys" not in json.dumps(entries)

    def test_an_escaping_symlink_is_not_mentionable(self, tmp_path):
        """The exact-name lookup is gated identically to the scan, so the chat
        surface cannot reach what the listing refused to name."""
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)

        msg, status = _expand_prompt_mention("@creds", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "SECRET-BODY" not in msg

    def test_an_escaping_symlink_is_not_readable_by_name(self, tmp_path, mock_sel):
        """Same gate behind the unscoped detail read, which serves whole bodies."""
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)

        resp = asyncio.run(api_prompt_detail(_api_request("creds", project=proj)))
        assert resp.status == 404
        assert b"SECRET-BODY" not in resp.body

    def test_a_hardlinked_prompt_is_not_a_prompt(self, tmp_path):
        """The scoped read already refuses a hardlinked prompt, so a listing that
        offered one would advertise a file its own scope will not serve."""
        outside = tmp_path / "elsewhere" / "creds"
        outside.parent.mkdir(parents=True)
        outside.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        os.link(outside, d / "creds.md")

        assert _list_aim_prompts(proj) == []
        msg, status = _expand_prompt_mention("@creds", _State(), _Slot(project=proj))
        assert (status, "SECRET-BODY" in msg) == ("not_found", False)

    def test_a_sensitive_resolved_target_is_dropped_from_both_halves(
        self, tmp_path, block_sensitive_discovery
    ):
        """The same predicate the edition SOP walk applies, now on the user half —
        so ``@mention`` of a sensitive prompt cannot be reached through discovery."""
        proj, d = self._project_prompts(tmp_path)
        (d / "local-one.md").write_text("# L\n")
        _user_prompt(tmp_path, "global-one", "# G\n")

        assert _list_aim_prompts(proj) == []

    def test_the_mention_read_refuses_an_escaped_path_it_is_handed(self, tmp_path, monkeypatch):
        """The read site does not delegate its safety to the discovery gate.

        The gate refuses an escaping name, so in production nothing reaches the
        expander with one — until a link is swapped in between the two. Handing the
        expander such an entry directly is how that window is exercised: it must
        read the path it CANONICALIZED and re-check it there, rather than checking
        the name as addressed and reading whatever it points at.
        """
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.is_sensitive_path", lambda p: "elsewhere" in str(p)
        )
        monkeypatch.setattr("kiro_crew.hooks.is_sensitive_path", lambda p: "elsewhere" in str(p))
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._find_prompt",
            lambda name, project_dir=None: {
                "name": "creds",
                "fullName": "creds",
                "description": "",
                "path": str(d / "creds.md"),
                "package": "",
                "source": "local",
            },
        )

        msg, status = _expand_prompt_mention("@creds", _State(), _Slot(project=proj))
        assert status == "blocked"
        assert "SECRET-BODY" not in msg

    def test_the_mention_read_refuses_a_hardlinked_prompt(self, tmp_path, monkeypatch):
        """A second NAME for a sensitive inode carries no link for any symlink
        check to see, and canonicalizing it changes nothing: the alias IS a real
        path inside the prompt directory. ``st_nlink`` is the only signal, and it
        is only readable on the descriptor that was actually opened — so the read
        has to go through the nolink gate rather than read the canonical name.
        """
        outside = tmp_path / "not-a-prompt-at-all"
        outside.write_text("# Notes\nSECRET-BODY\n")
        proj, d = self._project_prompts(tmp_path)
        alias = d / "aliased.md"
        os.link(outside, alias)  # a HARDLINK, not a symlink
        assert not alias.is_symlink(), "precondition: no symlink guard can see this"
        assert alias.stat().st_nlink == 2, "precondition: the alias is a second name"
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._find_prompt",
            lambda name, project_dir=None: {
                "name": "aliased",
                "fullName": "aliased",
                "description": "",
                "path": str(alias),
                "package": "",
                "source": "local",
            },
        )

        msg, status = _expand_prompt_mention("@aliased", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "SECRET-BODY" not in msg

    def test_the_mention_read_refuses_an_inode_outside_the_scope_root(self, tmp_path, monkeypatch):
        """``O_NOFOLLOW`` guards only the FINAL component, so an ancestor
        directory swapped for a link would still open a file outside the prompt
        tree. The gate is handed the scope's own root and pins the OPENED inode
        inside it; handing the expander an entry that already names a file
        outside is how that pin is exercised without racing a directory swap.
        """
        outside = tmp_path / "elsewhere" / "notes.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("# Notes\nSECRET-BODY\n")
        proj, _d = self._project_prompts(tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._find_prompt",
            lambda name, project_dir=None: {
                "name": "notes",
                "fullName": "notes",
                "description": "",
                "path": str(outside),
                "package": "",
                "source": "local",
            },
        )

        msg, status = _expand_prompt_mention("@notes", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "SECRET-BODY" not in msg

    @pytest.mark.parametrize("stem", ["notes..draft", ".hidden"])
    def test_a_stem_no_other_verb_can_address_is_not_listed(self, tmp_path, mock_sel, stem):
        """The listing offers exactly the names this API can address.

        ``_plain_stem_ok`` is the single predicate create, the scoped read and both
        write verbs address a prompt by, so a stem it rejects already answers
        ``invalid_name`` everywhere else. Listing it advertised a name nothing
        could open, edit or delete — and the listing and the ``@mention`` lookup
        have to agree, which is the whole point of this change.
        """
        proj, d = self._project_prompts(tmp_path)
        (d / f"{stem}.md").write_text("# Odd\nODD-BODY\n")
        (d / "ok.md").write_text("# Ok\n")

        assert [e["name"] for e in _list_aim_prompts(proj)] == ["ok"]
        msg, status = _expand_prompt_mention(f"@{stem}", _State(), _Slot(project=proj))
        assert status == "not_found" and "ODD-BODY" not in msg
        # The scoped read it disagreed with, pinned here so the agreement is the
        # assertion rather than a coincidence.
        scoped = asyncio.run(
            api_prompt_detail(_write_request("GET", stem, scope="local", project=proj))
        )
        assert json.loads(scoped.body)["code"] == "invalid_name"

    def test_a_link_inside_the_directory_is_refused_like_every_other_link(self, tmp_path, mock_sel):
        """A contained alias is refused too, and the ASSERTION is the agreement.

        The scoped read refuses every link before it dereferences anything, so an
        entry the listing kept because the link happened to stay inside the
        directory named a file no other verb on this API will open. That made
        "listed" and "serveable" different sets; the listing tests the same
        lstat-based predicate so they are one set again.
        """
        proj, d = self._project_prompts(tmp_path)
        (d / "target.md").write_text("# Target\nBODY\n")
        (d / "alias.md").symlink_to(d / "target.md")

        assert [e["name"] for e in _list_aim_prompts(proj)] == ["target"]
        msg, status = _expand_prompt_mention("@alias", _State(), _Slot(project=proj))
        assert status == "not_found"
        # The verb it has to agree with, pinned here rather than assumed.
        scoped = asyncio.run(
            api_prompt_detail(_write_request("GET", "alias", scope="local", project=proj))
        )
        assert scoped.status == 403
        # The target is still a prompt in its own right — this refuses the alias,
        # not the directory.
        msg, status = _expand_prompt_mention("@target", _State(), _Slot(project=proj))
        assert status == "ok" and "BODY" in msg

    def test_a_self_referential_link_drops_one_entry_and_not_the_library(self, tmp_path):
        """A cloned project shipping ``loop.md -> loop.md`` must lose one entry, not
        the whole listing. The link refusal stops before dereferencing anything."""
        proj, d = self._project_prompts(tmp_path)
        (d / "loop.md").symlink_to(d / "loop.md")
        (d / "real.md").write_text("# Real\nBODY\n")

        assert [e["name"] for e in _list_aim_prompts(proj)] == ["real"]

    def test_a_loop_appearing_after_the_link_check_is_still_only_one_entry(
        self, tmp_path, monkeypatch
    ):
        """The residual the ordinary catch would miss: ``Path.resolve()`` signals a
        symlink loop with ``RuntimeError``, which is not an ``OSError``.

        The link refusal normally keeps a loop out of ``resolve()`` entirely, so the
        only way there is an entry swapped for a loop between that lstat and this
        resolve. Neutralizing the lstat is how that race is made deterministic.
        """
        import kiro_crew.dashboard.handlers as h

        proj, d = self._project_prompts(tmp_path)
        (d / "loop.md").symlink_to(d / "loop.md")
        (d / "real.md").write_text("# Real\nBODY\n")
        monkeypatch.setattr(h, "is_link_or_junction", lambda p: False)

        assert [e["name"] for e in _list_aim_prompts(proj)] == ["real"]

    def test_a_self_referential_link_is_a_miss_not_a_crash_for_a_mention(self, tmp_path):
        """Same loop through the exact-name lookup, which shares the gate."""
        proj, d = self._project_prompts(tmp_path)
        (d / "loop.md").symlink_to(d / "loop.md")

        msg, status = _expand_prompt_mention("@loop", _State(), _Slot(project=proj))
        assert status == "not_found"

    def test_a_self_referential_link_does_not_500_the_listing_endpoint(self, tmp_path, mock_sel):
        """The consequence a user would see: the Prompts tab still answers 200."""
        proj, d = self._project_prompts(tmp_path)
        (d / "loop.md").symlink_to(d / "loop.md")
        (d / "real.md").write_text("# Real\n")

        resp = asyncio.run(api_prompts(_list_request(proj)))
        assert resp.status == 200
        assert [p["name"] for p in json.loads(resp.body)] == ["real"]

    # ── the opposite failure mode: what the gate must NOT refuse ──

    def test_a_dotfile_managed_kiro_dir_still_lists_its_prompts(self, tmp_path):
        """Resolved-to-resolved containment: a link the USER chose above the
        prompt directory must keep working, or every dotfile-managed home loses
        its prompt library."""
        real_kiro = tmp_path / "dotfiles" / "kiro"
        (real_kiro / "prompts").mkdir(parents=True)
        (real_kiro / "prompts" / "mine.md").write_text("# Mine\n")
        (tmp_path / ".kiro").symlink_to(real_kiro, target_is_directory=True)

        assert [e["name"] for e in _list_aim_prompts()] == ["mine"]

    def test_a_prompt_with_no_readable_description_keeps_its_entry(self, tmp_path):
        """A description that cannot be extracted is the read path's problem to
        report. Dropping the entry would make the prompt vanish from the library
        with no explanation anywhere.

        Undecodable bytes rather than a cleared mode, so the branch is reachable on
        every platform: mode-based read denial is POSIX-only (see below).
        """
        proj, d = self._project_prompts(tmp_path)
        (d / "broken.md").write_bytes(b"# \xff\xfe Broken\n")

        entries = _list_aim_prompts(proj)
        assert [(e["name"], e["description"]) for e in entries] == [("broken", "")]

    @pytest.mark.skipif(
        not IS_POSIX,
        reason="Windows grants the owner a read regardless of the mode bits, so a "
        "mode-denied file is not a state this test can construct there",
    )
    def test_an_unreadable_prompt_keeps_its_entry(self, tmp_path):
        """The same guarantee through the other failure mode a real library hits: a
        prompt whose mode denies the read."""
        proj, d = self._project_prompts(tmp_path)
        p = d / "broken.md"
        p.write_text("# Broken\n")
        p.chmod(0o000)
        try:
            entries = _list_aim_prompts(proj)
        finally:
            p.chmod(0o644)
        assert [(e["name"], e["description"]) for e in entries] == [("broken", "")]


# ── local lookup is bounded, not a scan ──


class TestLocalLookupIsBounded:
    """``_find_prompt`` resolves the local half by exact name.

    ``@mention`` expansion is paid once per turn, and the local half of the listing
    is uncacheable (it is keyed by the caller's project). Listing the project's
    prompt directory there the way the Prompts tab does would put a description
    READ per file on every turn beginning with ``@``, on a directory that may be
    network-backed. Exactly one entry can match a bare name, so the lookup finds
    that entry — one directory read, then at most one file open — instead of
    describing every prompt in the directory. Running off the loop
    (``TestPromptExpansionStaysOffTheEventLoop``) bounds who a slow read hurts; it
    does not make the read free, so both properties are needed.
    """

    def test_resolving_a_mention_does_not_scan_the_project_dir(self, tmp_path, monkeypatch):
        """The shape, not a duration: the project's prompt directory is never
        enumerated, and the prompt still resolves."""
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "local-sop.md").write_text("# Local\nBODY\n")

        scanned: list[str] = []
        real_scan = h._scan_prompt_dir
        monkeypatch.setattr(
            h,
            "_scan_prompt_dir",
            lambda prompts_dir, root_real, src: scanned.append(str(prompts_dir))
            or real_scan(prompts_dir, root_real, src),
        )

        msg, status = _expand_prompt_mention("@local-sop", _State(), _Slot(project=proj))
        assert status == "ok" and "BODY" in msg
        assert str(d) not in scanned, "the @mention path enumerated the project prompt dir"

    def test_resolving_a_mention_reads_only_the_matched_prompt(self, tmp_path, monkeypatch):
        """The bound that actually costs, stated as a shape: the on-loop work must
        not grow with the number of prompts in the directory.

        Naming ``_scan_prompt_dir`` alone would pass on a lookup that had inlined
        the same walk, so this counts the per-file description reads — the only
        part of the walk whose cost is unbounded — and requires exactly one, for
        the prompt that matched.
        """
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        for i in range(12):
            (d / f"filler-{i}.md").write_text(f"# Filler {i}\n")
        (d / "local-sop.md").write_text("# Local\nBODY\n")

        # Counted at ``_gated_sop_description``, the reader a USER-prompt entry
        # describes itself through. ``_extract_sop_description`` is counted too, so
        # that moving the read back to the by-name variant cannot make this bound
        # look satisfied by having stopped observing it.
        described: list[str] = []
        real_gated = h._gated_sop_description
        real_plain = h._extract_sop_description
        monkeypatch.setattr(
            h,
            "_gated_sop_description",
            lambda p, root: described.append(str(p)) or real_gated(p, root),
        )
        monkeypatch.setattr(
            h,
            "_extract_sop_description",
            lambda p: described.append(str(p)) or real_plain(p),
        )

        msg, status = _expand_prompt_mention("@local-sop", _State(), _Slot(project=proj))
        assert status == "ok" and "BODY" in msg
        assert described == [str(d / "local-sop.md")]

    def test_the_listing_still_scans_the_project_dir(self, tmp_path, mock_sel):
        """The bound belongs to the lookup only; the listing's job IS to enumerate,
        and it runs in an executor. Without this the test above would pass on a
        lookup that had simply stopped working."""
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "local-sop.md").write_text("# Local\n")

        names = [p["name"] for p in json.loads(asyncio.run(api_prompts(_list_request(proj))).body)]
        assert names == ["local-sop"]

    def test_the_project_independent_half_still_wins_a_stem_collision(self, tmp_path):
        """The two halves form a single ordering, global first, so it wins a stem collision."""
        _user_prompt(tmp_path, "shared", "# Global\nGLOBAL-BODY\n")
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "shared.md").write_text("# Local\nLOCAL-BODY\n")

        msg, status = _expand_prompt_mention("@shared", _State(), _Slot(project=proj))
        assert status == "ok"
        assert "GLOBAL-BODY" in msg and "LOCAL-BODY" not in msg

    @pytest.mark.parametrize("mention", ["sub/deep", "../escape", "/etc/passwd"])
    def test_a_name_that_is_not_a_direct_child_matches_nothing(self, tmp_path, mention):
        """``glob('*.md')`` yields direct children only, so the bounded lookup must
        address direct children only — or it would reach files no listing shows."""
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts" / "sub"
        d.mkdir(parents=True)
        (d / "deep.md").write_text("# Deep\nDEEP-BODY\n")
        (proj / ".kiro" / "escape.md").write_text("# Escape\nESCAPE-BODY\n")

        msg, status = _expand_prompt_mention(f"@{mention}", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "DEEP-BODY" not in msg and "ESCAPE-BODY" not in msg

    @pytest.mark.parametrize("mention", ["sub/deep", "../escape", ".hidden", "notes..draft"])
    def test_a_bad_name_never_reaches_the_filesystem(self, tmp_path, monkeypatch, mention):
        """Gate the name, THEN look it up — the order the scoped read already uses.

        The entry gate would refuse each of these anyway, so this pins the
        ORDERING rather than the outcome: a name that is not a plain component is
        a miss before any path derived from it is resolved or stat'd.
        """
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        (proj / ".kiro" / "prompts").mkdir(parents=True)
        touched: list[str] = []
        monkeypatch.setattr(
            h, "_prompt_dir_entry", lambda p, root, src: touched.append(str(p)) or None
        )

        assert _prompts_mod._find_prompt(mention, proj) is None
        assert touched == []

    def test_a_name_with_no_entry_builds_no_path_at_all(self, tmp_path, monkeypatch):
        """The candidate comes from the DIRECTORY's own entry name, never from the
        caller's string joined onto the prompt root.

        The two spellings would open the same inode, so the outcome cannot show
        which one is used — but a name with no matching entry reaches the gate only
        under the joined form. Requiring the gate not to be called at all is what
        pins the enumerated form, and with it the property that no path derived
        from caller input is ever handed to a filesystem call.
        """
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "present.md").write_text("# Present\n")
        built: list[str] = []
        real = h._prompt_dir_entry
        monkeypatch.setattr(
            h,
            "_prompt_dir_entry",
            lambda p, root, src: built.append(str(p)) or real(p, root, src),
        )

        assert _prompts_mod._find_prompt("absent", proj) is None
        assert built == []
        assert _prompts_mod._find_prompt("present", proj) is not None
        assert built == [str(d / "present.md")]

    def test_an_unencodable_name_is_a_miss_not_a_crash(self, tmp_path, mock_sel):
        """A ``%00`` in the URL path reaches the lookup as an embedded NUL. It is
        compared against directory entry names rather than joined into a path, and
        no real entry name can hold one — a 404, never an unaudited 500."""
        proj = tmp_path / "checkout"
        (proj / ".kiro" / "prompts").mkdir(parents=True)
        resp = asyncio.run(api_prompt_detail(_api_request("bad\x00name", project=proj)))
        assert resp.status == 404

    def test_a_packaged_spelling_does_not_probe_the_project(self, tmp_path, monkeypatch):
        """A user prompt's ``package`` is empty, so ``pkg/name`` can never name one."""
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "thing.md").write_text("# Thing\nBODY\n")
        probed: list[str] = []
        real = h._prompt_dir_entry
        monkeypatch.setattr(
            h,
            "_prompt_dir_entry",
            lambda p, root, src: probed.append(str(p)) or real(p, root, src),
        )

        assert _prompts_mod._find_prompt("Pkg-1.0/thing", proj) is None
        assert probed == []


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
        """A resolver that RAISES degrades to the original text, not a 500.

        The patch target is ``chat_runner``'s own global rather than the
        handlers package the name came from: chat_runner does ``from
        kiro_crew.dashboard.handlers import _find_prompt`` (chat_runner.py:120),
        so rebinding the package attribute leaves the call at chat_runner.py:3730
        resolving the REAL resolver — which finds no prompt named "x" under the
        autouse-isolated HOME and returns ``not_found`` from the ``if not match``
        arm instead, making this test a silent duplicate of ``test_no_match``.
        ``raised`` is what keeps that regression visible: without it, a patch
        that misses still passes every assertion below.
        """
        raised: list[str] = []

        def _raise_on_list(n, project_dir=None):
            raised.append(n)
            raise PermissionError

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._find_prompt", _raise_on_list)
        msg, status = _expand_prompt_mention("@x", _State(), _Slot())
        assert (msg, status) == ("@x", "not_found")
        assert raised == ["x"], "patched resolver never ran — the except arm was not exercised"

    def test_sensitive_path_blocked(self, tmp_path, block_sensitive_reads):
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
        resp = asyncio.run(api_prompts(_list_request()))
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

    def test_detail_sensitive(self, tmp_path, mock_sel, block_sensitive_reads):
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
    def _checkout_prompts(tmp_path) -> tuple[Path, Path]:
        """``(project, prompts_dir)`` for a cloned project's local scope.

        The project is RETURNED for the caller to pass, not installed in a
        process-wide resolver: the local scope is resolved per request now, so a
        test that patched ``agent._project_dir`` would exercise a seam no prompt
        surface reads and would see an empty local library.
        """
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        return proj, d

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

    def test_a_hardlinked_prompt_is_refused_by_the_listing(self, tmp_path):
        """A second NAME for an outside inode is not a prompt at all.

        Stricter than answering it with an empty description, which is what this
        surface gave while the listing had no entry gate: the local scoped read
        and both write verbs refuse ``st_nlink > 1`` outright, so an entry the
        listing kept advertised a name no verb on this API would open. What the
        descriptor gate still owns is the case the ``lstat`` cannot see — a swap
        landing after it — which the reads below pin.
        """
        secret = self._secret(tmp_path)
        proj, d = self._checkout_prompts(tmp_path)
        (d / "innocent.md").write_text("# Innocent\n", encoding="utf-8")
        self._plant_alias(secret, d / "aliased.md")

        listed = _list_aim_prompts(proj)
        by_name = {p["name"]: p for p in listed}
        assert "aliased" not in by_name
        assert "SHOULD-NOT-APPEAR" not in json.dumps(listed)
        # The ordinary neighbour is unaffected: the refusal costs one entry,
        # never the library around it.
        assert by_name["innocent"]["description"] == "Innocent"

    @pytest.mark.skipif(not IS_POSIX, reason="the owner reads regardless of mode bits on Windows")
    def test_a_refused_description_leaves_an_audit_line(self, tmp_path, mock_sel):
        """An entry listing with no description must not also be invisible.

        With no audit record it is byte-identical to a prompt that simply has no
        description, so a withheld read leaves the operator nothing to find. The
        line records only THAT the bytes were withheld — the gate judges and
        reads through one descriptor and answers a bare ``None``, and re-``stat``
        ing the path to recover a cause would be another by-name look at exactly
        the input this read stopped trusting. Its ordinary neighbour must not
        produce one, or the log says nothing.

        Driven by a mode the reader cannot open rather than by a hardlink: the
        entry gate now refuses an aliased inode before any description read, so
        an unreadable plain file is what still reaches the reader — and it is the
        shape the "an entry stays listed with no description" contract exists for.
        """
        proj, d = self._checkout_prompts(tmp_path)
        (d / "innocent.md").write_text("# Innocent\n", encoding="utf-8")
        denied = d / "denied.md"
        denied.write_text("# Denied\n", encoding="utf-8")
        denied.chmod(0o000)
        try:
            listed = _list_aim_prompts(proj)
        finally:
            denied.chmod(0o600)
        # Listed, with no description: a bad mode surfaces as the read path's own
        # error, never as a prompt vanishing from the user's library.
        by_name = {p["name"]: p for p in listed}
        assert by_name["denied"]["description"] == ""
        assert by_name["innocent"]["description"] == "Innocent"
        refusals = [
            c
            for c in mock_sel.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "api_prompts"
        ]
        assert [c.kwargs["outcome"] for c in refusals] == ["error"]
        assert refusals[0].kwargs["metadata"]["path"].endswith("denied.md")

    @requires_symlinks
    def test_a_sensitive_description_target_is_audited_as_blocked(
        self, tmp_path, aim_dir, mock_sel
    ):
        """The one cause that IS knowable is recorded as itself.

        ``validate_file_path`` refuses the name before any open, so unlike the
        gate's bare ``None`` this refusal has a reason the audit line can state.
        Asserted on a PACKAGE SOP, the reader that still canonicalizes a name it
        was handed: a user prompt's own root is validated, so its description read
        is pinned inside that root instead and a sensitive target is refused by
        the entry gate before any read at all.
        """
        store = tmp_path / "credential-store"
        store.mkdir()
        (store / "credentials").write_text("# k = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        monkeypatch_target = "kiro_crew.hooks.is_sensitive_path"
        pkg = aim_dir / "Pkg"
        pkg.mkdir()
        (pkg / "creds.sop.md").symlink_to(store / "credentials")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(monkeypatch_target, lambda p: "credential-store" in str(p))
            listed = _list_aim_prompts()

        assert "SHOULD-NOT-APPEAR" not in json.dumps(listed)
        refusals = [
            c
            for c in mock_sel.log_tool_invocation.call_args_list
            if c.kwargs["tool_name"] == "api_prompts"
        ]
        assert [c.kwargs["outcome"] for c in refusals] == ["blocked"]

    def test_a_hardlinked_prompt_is_not_served_by_the_unscoped_read(self, tmp_path, mock_sel):
        """The bytes are not served, and the answer agrees with the listing.

        The alias is refused when the entry is minted, so this route never
        resolves a name for it and answers the 404 an unknown prompt answers.
        That is the same non-oracle property the ``file not readable`` 500 carried
        while the listing still offered the name: the status a caller sees is one
        the surface produces for a prompt that is simply not there.
        """
        secret = self._secret(tmp_path)
        proj, d = self._checkout_prompts(tmp_path)
        self._plant_alias(secret, d / "aliased.md")

        resp = asyncio.run(api_prompt_detail(_api_request("aliased", project=proj)))
        assert resp.status == 404
        assert b"SHOULD-NOT-APPEAR" not in resp.body
        assert mock_sel.log_tool_invocation.call_args[1]["outcome"] == "not_found"

    @requires_symlinks
    def test_a_prompt_resolving_onto_a_sensitive_target_is_refused(self, tmp_path, monkeypatch):
        """Stricter than an empty description: the name is not offered at all."""
        store = tmp_path / "credential-store"
        store.mkdir()
        secret = store / "credentials"
        secret.write_text("# aws_access_key_id = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        monkeypatch.setattr(
            "kiro_crew.hooks.is_sensitive_path", lambda p: "credential-store" in str(p)
        )
        proj, d = self._checkout_prompts(tmp_path)
        (d / "creds.md").symlink_to(secret)

        listed = _list_aim_prompts(proj)
        assert [p["name"] for p in listed] == []
        assert "SHOULD-NOT-APPEAR" not in json.dumps(listed)

    @requires_symlinks
    def test_the_description_read_is_not_a_blanket_link_refusal(self, tmp_path):
        """Tolerance, not a fix: the gate canonicalizes BEFORE it opens.

        ``O_NOFOLLOW`` therefore refuses nothing about a link per se — only a
        sensitive, hardlinked or non-regular TARGET, or one resolving outside the
        pinned root, loses its description. Here so a later round cannot tighten
        the description READ into a blanket link refusal without going red.

        Asserted on the reader directly, and with a link that stays inside the
        prompt root, because the LISTING does not offer a linked entry to reach
        it with: ``_prompt_dir_entry`` refuses one outright so the local scope
        offers exactly the names its own read, update and delete verbs can
        address. That withdrawal is pinned by
        ``TestUserPromptDiscoveryGate.test_a_link_inside_the_directory_is_refused_like_every_other_link``;
        this test keeps the reader honest underneath it.
        """
        proj, d = self._checkout_prompts(tmp_path)
        (d / "review.md").write_text("# Review Checklist\n", encoding="utf-8")
        alias = d / "alias.md"
        alias.symlink_to(d / "review.md")

        assert _prompts_mod._gated_sop_description(alias, d.resolve()) == "Review Checklist"
        # And the listing does not offer the alias, so the two answers agree.
        assert [p["name"] for p in _list_aim_prompts(proj)] == ["review"]

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
        _proj, d = self._checkout_prompts(tmp_path)
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
        proj, d = self._checkout_prompts(tmp_path)
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

        resp = asyncio.run(api_prompt_detail(_api_request("notes", project=proj)))
        assert resp.status == 500
        assert b"SHOULD-NOT-APPEAR" not in resp.body


# ── _run_chat prompt paths ──


class TestRunChatPrompts:
    def test_slash_list(self, aim_dir, mock_sel):
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts"))
        assert "@agent-sop:review" in sl.messages[-1][1]

    def test_slash_list_empty(self):
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts"))
        assert "No prompts found" in sl.messages[-1][1]

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
        assert "@agent-sop:review" in sl.messages[-1][1]

    def test_slash_list_explicit(self, aim_dir, mock_sel):
        """``/prompts list`` works the same as ``/prompts``."""
        _aim_pkg(aim_dir, "Pkg-1.0", "1", {"review": "# R\nDo review."})
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts list"))
        assert "@agent-sop:review" in sl.messages[-1][1]

    def test_slash_get_not_found(self, mock_sel):
        s, sl = _ss()
        asyncio.run(_run_chat(s, sl, "/prompts get nonexistent"))
        assert "not found" in sl.messages[-1][1]

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
        asyncio.run(api_prompts(_list_request()))
        # After the API call, @mention expansion must still resolve the prompt
        msg, status = _expand_prompt_mention("@agent-sop:sop", _State(), _Slot())
        assert status == "ok", f"Cache corrupted: expansion returned {status!r}"


class TestPromptExpansionStaysOffTheEventLoop:
    """Expanding a mention resolves and READS files, so it must not run on the loop.

    The local half is a directory the gateway does not own — it may be
    network-backed — and it is uncacheable, so the cost cannot be amortized away:
    on the loop, one ``@mention`` on slow storage stalls every other request and
    the heartbeat with it. Same treatment as the ``$skill`` expansion beside it,
    which is offloaded for the same reason.

    The split is by which THREAD may do the work, so it cuts both ways: the
    resolve-and-read half must leave the loop, and the chip append must NOT —
    ``slot.append`` ends in ``slot.event.set()`` on an ``asyncio.Event``, whose
    waiters are resolved through the loop's non-threadsafe ``call_soon``.
    """

    def test_the_slash_get_expansion_runs_on_a_worker_thread(self, mock_sel, monkeypatch):
        """``asyncio.run`` drives the loop on the CALLING thread, so a resolve that
        reports that thread's id ran on the loop — and a chip append that reports
        anything else mutated a loop-owned ``asyncio.Event`` from a worker."""
        import kiro_crew.dashboard.chat_runner as cr

        real = cr._resolve_prompt_mention
        resolve_threads: list[int] = []
        append_threads: list[int] = []

        def _record(message, project_dir):
            resolve_threads.append(threading.get_ident())
            expanded, status, _chip = real(message, project_dir)
            # A miss produces no chip, and the chip is the half whose thread is
            # under test here, so one is substituted: the property asserted is
            # "whoever surfaces a chip is on the loop", not what was matched.
            return expanded, status, "loaded a prompt"

        monkeypatch.setattr(cr, "_resolve_prompt_mention", _record)
        s, sl = _ss()
        real_append = sl.append

        def _record_append(role, text, cls, **kw):
            append_threads.append(threading.get_ident())
            real_append(role, text, cls, **kw)

        monkeypatch.setattr(sl, "append", _record_append)
        asyncio.run(_run_chat(s, sl, "/prompts get nonexistent"))

        assert resolve_threads, "the expansion was never reached"
        assert threading.get_ident() not in resolve_threads
        assert append_threads, "the chip was never surfaced"
        assert set(append_threads) == {threading.get_ident()}

    def test_the_offloaded_resolver_cannot_reach_the_slot(self):
        """A worker thread is not handed the slot or the state at all.

        Keeping the append off the worker by convention is a rule the next caller
        can forget; keeping the slot out of the offloaded function's PARAMETERS is
        one it cannot, so that is what is pinned.
        """
        import kiro_crew.dashboard.chat_runner as cr

        assert list(inspect.signature(cr._resolve_prompt_mention).parameters) == [
            "message",
            "project_dir",
        ]

    def test_no_coroutine_resolves_a_mention_inline(self):
        """The ``@mention`` site cannot be driven without a live session, so the
        rule is pinned statically over the source of ``chat_runner`` and the
        ``chat_turn`` owners composed into it: a bare call from a coroutine is an
        on-loop call, whichever site adds it."""
        import importlib
        import pkgutil

        import kiro_crew.dashboard.chat_runner as cr
        from kiro_crew.dashboard import chat_turn

        tree = ast.parse(Path(cr.__file__).read_text(encoding="utf-8"))
        owners = [
            importlib.import_module(f"{chat_turn.__name__}.{info.name}")
            for info in pkgutil.iter_modules(chat_turn.__path__)
        ]
        assert owners, "the owner scan found no module, so it is measuring nothing"
        trees = [tree, *(ast.parse(Path(m.__file__).read_text(encoding="utf-8")) for m in owners)]
        blocking = {"_expand_prompt_mention", "_resolve_prompt_mention"}
        inline = [
            "{} -> {}".format(fn.name, node.func.id)
            for module_tree in trees
            for fn in ast.walk(module_tree)
            if isinstance(fn, ast.AsyncFunctionDef)
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in blocking
        ]
        offloaded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "asyncio.to_thread"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "_resolve_prompt_mention"
        ]
        assert not inline, "coroutine(s) resolve a mention on the event loop: {}".format(inline)
        assert offloaded, "no call site offloads the expansion — did the caller move?"


class TestUnscopedDetailReadStaysOffTheEventLoop:
    """The unscoped ``GET /api/prompts/{name}`` does ALL its filesystem work in one
    executor job — the resolution AND the body read.

    Offloading only the resolution was survivable while a match could name nothing
    but a package root or the gateway's own ``~/.kiro/prompts``. A match can now
    name ``<project>/.kiro/prompts``, a directory the gateway does not own and that
    may be network-backed, so the ``stat`` and ``read_text`` that would run after
    the metadata came back would stall every other request and the heartbeat on
    exactly the storage this route newly reaches. The scoped branch already reads
    inside one job (``_api_user_prompt_detail``'s ``_read``); this is the unscoped
    branch held to the same rule.
    """

    #: Path methods that each cost a filesystem syscall. Present in the source of a
    #: coroutine BODY (outside any nested ``def``) they name work being done on the
    #: event loop.
    _BLOCKING_PATH_METHODS = frozenset(
        {
            "stat",
            "lstat",
            "read_text",
            "read_bytes",
            "resolve",
            "iterdir",
            "glob",
            "rglob",
            "scandir",
            "is_file",
            "exists",
        }
    )

    def test_the_read_gate_runs_on_the_resolution_s_own_worker_thread(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """``asyncio.run`` drives the loop on the CALLING thread, so a step reporting
        that thread's id ran on the loop.

        ``hooks.validate_file_path`` is the first step after the match, and the
        ``stat`` and ``read_text`` follow it in a straight line inside the same job —
        so its thread is what says whether the tail came back to the loop before
        reading the file.
        """
        import kiro_crew.hooks as hooks

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "local-sop.md").write_text("# Local\nBODY\n")

        find_threads: list[int] = []
        gate_threads: list[int] = []
        real_find = _prompts_mod._find_prompt
        real_gate = hooks.validate_file_path

        def _record_find(raw_name, project_dir=None):
            find_threads.append(threading.get_ident())
            return real_find(raw_name, project_dir)

        def _record_gate(raw):
            gate_threads.append(threading.get_ident())
            return real_gate(raw)

        monkeypatch.setattr(_prompts_mod, "_find_prompt", _record_find)
        monkeypatch.setattr(hooks, "validate_file_path", _record_gate)
        resp = asyncio.run(api_prompt_detail(_api_request("local-sop", project=proj)))

        assert resp.status == 200 and "BODY" in json.loads(resp.body)["content"]
        assert find_threads, "the resolution was never reached"
        assert gate_threads, "the read gate was never reached"
        assert threading.get_ident() not in gate_threads
        # ONE job, not two: a tail that returned to the loop and re-offloaded would
        # be off the loop too, and would report a different worker.
        assert set(gate_threads) == set(find_threads)

    def test_no_filesystem_call_is_left_in_the_coroutine_body(self):
        """Pinned statically, because at runtime the failure is invisible: the route
        answers identically whichever thread read the file, and only slow storage
        tells them apart. Nested ``def``s are pruned — those are the executor jobs,
        which is exactly where the work belongs."""
        owner = Path(_prompts_mod.__file__).with_name("prompts_authoring.py")
        tree = ast.parse(owner.read_text(encoding="utf-8"))
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_prompt_detail"
        )
        on_loop: list[str] = []
        stack: list[ast.AST] = list(handler.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in self._BLOCKING_PATH_METHODS
            ):
                on_loop.append(node.func.attr)
            stack.extend(ast.iter_child_nodes(node))
        assert not on_loop, "api_prompt_detail calls {} on the event loop".format(
            sorted(set(on_loop))
        )

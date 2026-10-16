"""Tests for the link and swap defences that every prompt reader and writer shares.

The one no-link read gate behind every reader, the same refusal for a live and a
dangling link, create and delete relative to a pinned directory descriptor, the
by-name fallback where that descriptor is unavailable, a project ``.kiro`` that must
stay inside its project, an ancestor symlink loop that costs one library rather than
the request, and a root swapped after validation that publishes nothing.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import json
import os
import stat
from pathlib import Path

import pytest
from prompts_helpers import _isolate_home  # noqa: F401
from prompts_helpers import _no_turn_tail  # noqa: F401
from prompts_helpers import mock_sel_fixture  # noqa: F401
from prompts_helpers import (
    _ANY_HASH,
    _api_request,
    _create_request,
    _list_request,
    _outcomes,
    _Slot,
    _State,
    _user_prompt,
    _write_request,
)

from conftest import requires_symlinks
from kiro_crew.dashboard.chat import _expand_prompt_mention
from kiro_crew.dashboard.handlers import (
    _list_aim_prompts,
    api_prompt_detail,
    api_prompts,
    api_prompts_create,
)
from kiro_crew.dashboard.handlers import prompts as _prompts_mod


class TestEveryPromptReaderUsesTheNoLinkGate:
    """One directory, one read gate — for every reader of a prompt entry.

    A project's ``.kiro/prompts`` is content the user CLONED, so its author chooses
    what is in it and can swap an entry after the minting gate has looked at it.
    ``_prompt_dir_entry`` refuses a link and a hardlink by ``lstat``, but a reader
    that then opens the path BY NAME re-resolves it, so the bytes it serves are not
    the bytes any check ran against. The fix is one primitive,
    ``hooks.safe_read_file_bytes_nolink``: it opens first with ``O_NOFOLLOW`` and
    validates the descriptor it actually read, and ``within_root`` pins the opened
    inode's real path inside the root the entry was gated against — which is what
    ``O_NOFOLLOW`` alone cannot do, since it guards only the final component.

    The readers, and what each is pinned by:

    * chat ``@mention`` expansion — ``TestUserPromptDiscoveryGate``
    * scoped ``GET ...?scope=`` — ``TestScopedPromptDetail`` / ``TestLinkRefusalIsUniform``
    * unscoped ``GET /api/prompts/{name}`` — here
    * the listing's own description read — here
    * the write verbs — ``hooks.verified_replace_file_nolink``, ``TestPromptWriteHardening``
    """

    def test_the_listing_description_is_read_through_the_gate(self, tmp_path, monkeypatch):
        """The description read is a reader too, and its leak is one line rather than
        a whole file — a heading, or a ``description:`` frontmatter value.

        Pinned by making the by-name reader unusable: if the user-prompt entry still
        routed through ``_extract_sop_description`` this raises, and if it silently
        stopped reading at all the description would be empty.
        """
        import kiro_crew.dashboard.handlers as h

        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        (d / "plain.md").write_text("---\nname: plain\ndescription: Real desc\n---\n")

        def _boom(path):
            raise AssertionError("a user-prompt entry read its description by name")

        monkeypatch.setattr(h, "_extract_sop_description", _boom)
        assert [(e["name"], e["description"]) for e in _list_aim_prompts(proj)] == [
            ("plain", "Real desc")
        ]

    @requires_symlinks
    def test_the_description_gate_refuses_a_swapped_link(self, tmp_path):
        """The swap the entry gate cannot see, exercised directly on the helper.

        ``_prompt_dir_entry`` refuses a link by ``lstat``, so a link never survives
        minting — which means the window this closes is the one BETWEEN that lstat
        and the read. Calling the helper on a link is that window's outcome without
        having to race it: ``O_NOFOLLOW`` refuses to open it at all, so the target's
        heading cannot be published.
        """
        secret = tmp_path / "elsewhere" / "creds"
        secret.parent.mkdir(parents=True)
        secret.write_text("# AWS keys\nSECRET-BODY\n")
        d = tmp_path / "checkout" / ".kiro" / "prompts"
        d.mkdir(parents=True)
        link = d / "creds.md"
        link.symlink_to(secret)

        assert _prompts_mod._gated_sop_description(link, d) == ""

    def test_the_description_gate_refuses_a_hardlink_and_an_outside_inode(self, tmp_path):
        """The two shapes no symlink check sees: a second name for an outside inode,
        and an inode that is simply not under the root the entry was gated against."""
        outside = tmp_path / "elsewhere" / "creds.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("# AWS keys\nSECRET-BODY\n")
        d = tmp_path / "checkout" / ".kiro" / "prompts"
        d.mkdir(parents=True)
        alias = d / "aliased.md"
        os.link(outside, alias)

        assert _prompts_mod._gated_sop_description(alias, d) == ""
        assert _prompts_mod._gated_sop_description(outside, d) == ""

    def test_the_description_gate_still_describes_a_plain_contained_prompt(self, tmp_path):
        """The refusals must not be the only outcome, or the tests above would pass
        on a helper that had stopped reading anything."""
        d = tmp_path / "checkout" / ".kiro" / "prompts"
        d.mkdir(parents=True)
        plain = d / "plain.md"
        plain.write_text("# My Heading\nBody.\n")

        assert _prompts_mod._gated_sop_description(plain, d) == "My Heading"

    @requires_symlinks
    def test_the_read_root_is_derived_once_for_every_reader(self, tmp_path):
        """A root derived per reader is how two readers of one directory drift apart,
        so it is a property of the ENTRY: a user scope gets its own root, and a
        package SOP gets none (plural provider-supplied roots), which leaves it the
        ``O_NOFOLLOW`` and hardlink checks without a guessed containment.

        The user-scope roots come back RESOLVED, which is the property the gate
        turns on: it ``realpath``s whatever ``within_root`` it is handed, so an
        unresolved root re-traverses its chain at read time and a swapped root
        resolves into the link's destination on both sides of the comparison.
        Asserted against a project reached through a symlink so the two spellings
        are distinguishable at all — under an unlinked path they are equal and the
        assertion would hold either way.
        """
        real = tmp_path / "real-project"
        (real / ".kiro" / "prompts").mkdir(parents=True)
        proj = tmp_path / "via-link"
        proj.symlink_to(real)

        local = {"package": "", "source": "local"}
        assert _prompts_mod._prompt_read_root(local, proj) == real / ".kiro" / "prompts"
        assert _prompts_mod._prompt_read_root(local, proj) != proj / ".kiro" / "prompts"
        assert _prompts_mod._prompt_read_root(local, None) is None
        assert (
            _prompts_mod._prompt_read_root({"package": "", "source": "global"}, proj)
            == (Path.home() / ".kiro" / "prompts").resolve()
        )
        assert (
            _prompts_mod._prompt_read_root({"package": "Pkg-1.0", "source": "package"}, proj)
            is None
        )
        # An unfamiliar producer falls back to no root rather than a wrong one.
        assert (
            _prompts_mod._prompt_read_root({"package": "", "source": "somewhere-new"}, proj) is None
        )

    def test_a_user_scope_read_with_no_serveable_root_is_refused_not_widened(self, tmp_path):
        """``None`` from the root derivation means REFUSE, never "unconstrained".

        A user-scope entry that cannot name a serveable root is exactly the
        state a swapped root leaves, so falling back to the canonical path's own
        parent there would pin the read inside the directory the swap named — the
        leak the pin exists to close. A package SOP takes that fallback, because
        its roots are plural and no authorizing one exists to pass.
        """
        canonical = str(tmp_path / "anywhere" / "notes.md")
        local = {"package": "", "source": "local"}
        # No project, so the local scope names no root: refuse.
        assert _prompts_mod._prompt_read_within_root(local, None, canonical) is None
        # A package SOP is pinned inside the canonical path's own parent instead.
        pkg = {"package": "Pkg-1.0", "source": "package"}
        assert _prompts_mod._prompt_read_within_root(pkg, None, canonical) == str(
            tmp_path / "anywhere"
        )
        # ...and so is an entry shape naming neither scope, so an unfamiliar
        # producer loses no capability.
        other = {"package": "", "source": "somewhere-new"}
        assert _prompts_mod._prompt_read_within_root(other, None, canonical) == str(
            tmp_path / "anywhere"
        )

    def test_the_unscoped_read_refuses_a_hardlinked_prompt(self, tmp_path, mock_sel, monkeypatch):
        """Every reader of a prompt entry goes through the no-link gate, not just
        the two that already did.

        A second NAME for an outside inode carries no link for any symlink check to
        see, and canonicalizing it changes nothing — the alias IS a real path inside
        the prompt directory. ``st_nlink`` is readable only on the descriptor that
        was actually opened, so a by-name ``read_text`` after the match serves the
        target's bytes. The `@mention` expansion and the scoped read already
        refused this; this is the unscoped detail branch held to the same rule.
        """
        outside = tmp_path / "not-a-prompt-at-all"
        outside.write_text("# Notes\nSECRET-BODY\n")
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        alias = d / "aliased.md"
        os.link(outside, alias)  # a HARDLINK, not a symlink
        assert not alias.is_symlink(), "precondition: no symlink guard can see this"
        assert alias.stat().st_nlink == 2, "precondition: the alias is a second name"
        monkeypatch.setattr(
            _prompts_mod,
            "_find_prompt",
            lambda raw_name, project_dir=None: {
                "name": "aliased",
                "fullName": "aliased",
                "description": "",
                "path": str(alias),
                "package": "",
                "source": "local",
            },
        )

        resp = asyncio.run(api_prompt_detail(_api_request("aliased", project=proj)))
        assert resp.status != 200
        assert b"SECRET-BODY" not in resp.body

    def test_the_unscoped_read_refuses_an_inode_outside_the_scope_root(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """``O_NOFOLLOW`` guards only the FINAL component, so an ancestor directory
        swapped for a link would still open a file outside the prompt tree. The
        gate is handed the entry's own scope root (`_prompt_read_root`) and pins the
        OPENED inode inside it; handing the handler an entry that already names a
        file outside exercises that pin without racing a directory swap.
        """
        outside = tmp_path / "elsewhere" / "notes.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("# Notes\nSECRET-BODY\n")
        proj = tmp_path / "checkout"
        (proj / ".kiro" / "prompts").mkdir(parents=True)
        monkeypatch.setattr(
            _prompts_mod,
            "_find_prompt",
            lambda raw_name, project_dir=None: {
                "name": "notes",
                "fullName": "notes",
                "description": "",
                "path": str(outside),
                "package": "",
                "source": "local",
            },
        )

        resp = asyncio.run(api_prompt_detail(_api_request("notes", project=proj)))
        assert resp.status != 200
        assert b"SECRET-BODY" not in resp.body


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
        # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo of the os.open swap before the asserts, moved verbatim; tracked for a de-flake pass
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

        A ``MemoryError`` stands in for the whole class a narrower ``OSError``
        catch lets escape: that would answer 500 with no audit line,
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
        owner = Path(_prompts_mod.__file__).with_name("prompts_paths.py")
        tree = ast.parse(owner.read_text(encoding="utf-8"))
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

    @pytest.mark.parametrize("unsupported_tmpfile", [False, True], ids=["native", "unsupported"])
    def test_the_body_is_durable_before_the_name_appears(
        self, tmp_path, mock_sel, monkeypatch, unsupported_tmpfile
    ):
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

        WHICH order that is depends on the branch the filesystem takes, so the
        branch is read rather than assumed. The unnamed publish has no name until
        ``link`` makes one, so there the flush provably precedes the name. A mount
        without ``O_TMPFILE`` (every darwin host, and NFS) never calls ``link`` at
        all: its ``O_EXCL`` open IS the entry, so what is left to pin there is that
        the body is flushed before the flush that claims the entry is durable. That
        branch's other promises -- the 409 on an occupied name and the
        identity-checked cleanup -- are pinned by the ``force_named_fallback``
        tests below.
        """
        order: list[str] = []
        real_fsync, real_link, real_open = os.fsync, os.link, os.open
        unnamed_opens: list[bool] = []
        if unsupported_tmpfile:
            # Exercise EOPNOTSUPP even on POSIX hosts without O_TMPFILE/procfs.
            monkeypatch.setattr(_prompts_mod, "_UNNAMED_CREATE_SUPPORTED", True)
            monkeypatch.setattr(os, "O_TMPFILE", getattr(os, "O_TMPFILE", 1 << 30), raising=False)
            real_isdir = os.path.isdir
            monkeypatch.setattr(os.path, "isdir", lambda p: p == "/proc/self/fd" or real_isdir(p))
        attempts_unnamed = _prompts_mod._UNNAMED_CREATE_SUPPORTED and os.path.isdir("/proc/self/fd")
        tmpfile_flag = getattr(os, "O_TMPFILE", 0)

        def _note_open(path, flags, *a, **kw):
            if tmpfile_flag and flags & tmpfile_flag == tmpfile_flag:
                try:
                    if unsupported_tmpfile:
                        raise OSError(errno.EOPNOTSUPP, "test filesystem has no O_TMPFILE")
                    fd = real_open(path, flags, *a, **kw)
                except OSError:
                    unnamed_opens.append(False)
                    raise
                unnamed_opens.append(True)
                return fd
            return real_open(path, flags, *a, **kw)

        def _note_fsync(fd):
            # Distinguish the file's flush from the directory's by asking the
            # descriptor what it is, rather than by call order.
            order.append("fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file")
            return real_fsync(fd)

        def _note_link(*a, **kw):
            order.append("publish")
            return real_link(*a, **kw)

        monkeypatch.setattr(os, "open", _note_open)
        monkeypatch.setattr(os, "fsync", _note_fsync)
        monkeypatch.setattr(os, "link", _note_link)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "durable", "content": "B"})))
        monkeypatch.setattr(os, "open", real_open)
        monkeypatch.setattr(os, "fsync", real_fsync)
        monkeypatch.setattr(os, "link", real_link)

        assert len(unnamed_opens) == int(attempts_unnamed)
        if unsupported_tmpfile:
            assert unnamed_opens == [False]

        assert resp.status == 201
        assert "fsync_file" in order, f"the body was never flushed: {order}"
        assert "fsync_dir" in order, f"the directory entry was never flushed: {order}"
        assert order.index("fsync_file") < order.index(
            "fsync_dir"
        ), f"the body must be durable before the entry's flush claims it: {order}"
        # Observe the open outcome independently: a missing publish must never
        # make a successful unnamed create pass as the weaker named branch.
        if unnamed_opens == [True]:
            assert order[:2] == [
                "fsync_file",
                "publish",
            ], f"flush must precede publish, got {order}"
            assert order.index("fsync_dir") > order.index(
                "publish"
            ), f"the directory flush must follow the publish it is making durable: {order}"
        else:
            assert "publish" not in order, (
                "the by-name branch has no unnamed inode to link, so a publish here "
                f"means the branch predicate and the code disagree: {order}"
            )
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

    def test_the_named_fallback_flushes_the_body_before_the_entry(
        self, tmp_path, mock_sel, monkeypatch, force_named_fallback
    ):
        """The by-name branch's own half of the durability order, run HERE.

        ``O_TMPFILE`` is present on every Linux runner, so the branch every darwin
        host and every NFS home actually takes would otherwise be exercised only
        where nobody watches. It has no ``link`` to order against -- its ``O_EXCL``
        open is the entry -- so the property left is the one that still matters: the
        body is flushed before the flush that claims the entry is durable, and a
        201 therefore never rests on an unflushed body.
        """
        order: list[str] = []
        real_fsync = os.fsync

        def _note_fsync(fd):
            order.append("fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _note_fsync)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "byname", "content": "B"})))
        monkeypatch.setattr(os, "fsync", real_fsync)

        assert resp.status == 201
        assert order[:2] == [
            "fsync_file",
            "fsync_dir",
        ], f"the body must be flushed before the entry's flush claims it: {order}"
        assert (tmp_path / ".kiro" / "prompts" / "byname.md").read_text() == "B"

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

    @pytest.mark.skipif(
        not _prompts_mod._UNNAMED_CREATE_SUPPORTED or not os.path.isdir("/proc/self/fd"),
        reason="platform cannot build an unnamed inode (O_TMPFILE + /proc/self/fd)",
    )
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

        # And a create that FAILS mid-write never reaches its own name: the
        # incumbent beside it survives and no debris is left. The name is a FREE
        # one on purpose. A mount without ``O_TMPFILE`` refuses an OCCUPIED name at
        # its ``O_EXCL`` open, before any body is written, so aiming the failure at
        # ``rival`` again makes this half vacuous there -- which is exactly what it
        # reported on darwin.
        real_write = os.write
        failed = {"done": False}

        def _fail_the_body(fd, data):
            if data == b"MINE" and not failed["done"]:
                failed["done"] = True
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", _fail_the_body)
        broke = asyncio.run(
            api_prompts_create(_create_request({"name": "newcomer", "content": "MINE"}))
        )
        monkeypatch.setattr(os, "write", real_write)

        assert failed["done"], "the write never failed — the test would be vacuous"
        assert broke.status == 500 and json.loads(broke.body)["code"] == "write_failed"
        assert rival.read_text() == "NOT YOURS"
        # No ``newcomer.md``: the unnamed branch never named the inode, and the
        # by-name branch removed the leaf its own O_EXCL made.
        assert [p.name for p in d.iterdir()] == ["rival.md"]


class TestByNameFallbackStillWorks:
    """The no-``openat`` branch cannot run on CI's platform, so it is exercised by
    forcing the feature flag off. It gives a narrower guarantee (the leaf junction
    check only), but it must still create, delete, and answer the same codes."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the class's own fallback switch, moved verbatim; no test in the class calls monkeypatch.undo()
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


class TestLocalScopeStaysInProject:
    """A repo-authored ``.kiro`` link must not redirect local scope elsewhere.

    The ancestor-link tolerance exists for links the USER made under their own
    tree (a dotfile-managed home). A project ``.kiro`` is the repository
    author's file: a checkout shipping ``.kiro -> ~/.kiro`` would point "This
    project" mutations at the global tree. The resolver refuses any local dir
    that resolves outside the resolved project root.
    """

    def test_project_kiro_linked_to_home_cannot_delete_global(self, tmp_path, mock_sel):
        _user_prompt(tmp_path, "victim", "global body")
        proj = tmp_path / "checkout"
        proj.mkdir()
        (proj / ".kiro").symlink_to(tmp_path / ".kiro", target_is_directory=True)
        # The project comes from the request's chat slot now (per-slot); bind it.
        resp = asyncio.run(
            api_prompt_detail(_write_request("DELETE", "victim", scope="local", project=proj))
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "linked_prompt_root"
        assert (tmp_path / ".kiro" / "prompts" / "victim.md").exists()

    def test_project_kiro_linked_to_home_cannot_create(self, tmp_path, mock_sel):
        proj = tmp_path / "checkout"
        proj.mkdir()
        (proj / ".kiro").symlink_to(tmp_path / ".kiro", target_is_directory=True)
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "x", "content": "b", "scope": "local"}, project=proj)
            )
        )
        assert resp.status == 403
        assert json.loads(resp.body)["code"] == "linked_prompt_root"
        assert not (tmp_path / ".kiro" / "prompts" / "x.md").exists()

    def test_symlinked_project_root_itself_still_works(self, tmp_path, mock_sel):
        """Resolved-to-resolved comparison: a project the user reaches THROUGH a
        link is a location the user chose, and must keep working — this pins
        the design against the over-strict unresolved comparison."""
        real = tmp_path / "real-checkout"
        real.mkdir()
        link = tmp_path / "link-checkout"
        link.symlink_to(real, target_is_directory=True)
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "ok", "content": "b", "scope": "local"}, project=link)
            )
        )
        assert resp.status == 201
        assert (real / ".kiro" / "prompts" / "ok.md").exists()

    @pytest.mark.parametrize(
        "link_at, link_to, stem",
        [
            (".kiro/prompts", "docs", "secret-notes"),
            (".kiro", "", "private"),
        ],
        ids=["prompts-leaf-redirected", "kiro-redirected"],
    )
    def test_a_redirected_prompt_root_publishes_nothing(
        self, tmp_path, mock_sel, link_at, link_to, stem
    ):
        """A repository that picks the prompt ROOT gets no local library at all.

        ``_prompt_dir_entry`` gates an ENTRY against the directory it was found
        in, which by construction cannot see this: when the root is a link, both
        sides of the containment comparison resolve into its destination and every
        path inside looks confined. So a checkout shipping
        ``.kiro/prompts -> ~/docs`` would otherwise get the filename and
        first-heading description of every ``*.md`` there published by
        ``GET /api/prompts``, and ``@<stem>`` would inject one — while the scoped
        read answered ``linked_prompt_root`` for the same name. ``is_sensitive_path``
        does not cover it either: an ordinary document under the home directory is
        not sensitive.

        The ASSERTION is the agreement, the same shape the entry-link test uses:
        the listing excludes it, the mention misses, and the verb they have to
        agree with is pinned rather than assumed.
        """
        outside = tmp_path / "outside"
        (outside / "docs").mkdir(parents=True)
        (outside / "docs" / "secret-notes.md").write_text("# Tax notes\nSECRET-BODY\n")
        (outside / "prompts").mkdir()
        (outside / "prompts" / "private.md").write_text("# Private\nSECRET-BODY\n")

        proj = tmp_path / "checkout"
        link = proj / link_at
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside / link_to if link_to else outside, target_is_directory=True)

        assert [e["name"] for e in _list_aim_prompts(proj) if e["source"] == "local"] == []
        msg, status = _expand_prompt_mention(f"@{stem}", _State(), _Slot(project=proj))
        assert status == "not_found" and "SECRET-BODY" not in msg
        scoped = asyncio.run(
            api_prompt_detail(_write_request("GET", stem, scope="local", project=proj))
        )
        assert scoped.status == 403
        assert json.loads(scoped.body)["code"] == "linked_prompt_root"

    @staticmethod
    def _linked_kiro_project(tmp_path) -> Path:
        proj = tmp_path / "checkout"
        (proj / "cfg" / "prompts").mkdir(parents=True)
        (proj / "cfg" / "prompts" / "ok.md").write_text("# Ok\nBODY\n")
        (proj / ".kiro").symlink_to(proj / "cfg", target_is_directory=True)
        return proj

    @requires_symlinks
    def test_a_kiro_link_that_stays_in_the_project_still_lists(self, tmp_path, mock_sel):
        """The root gate is containment, not a ban on links — on every platform.

        A project that keeps its config under another directory of its OWN and
        links ``.kiro`` at it resolves inside the project root, so the root gate
        must keep listing it: the same tolerance the write verbs already grant.
        The listing half is decided by this API's own gates; whether a mention
        can then READ it also passes through ``hooks.validate_file_path``, which
        is pinned by the test below.
        """
        proj = self._linked_kiro_project(tmp_path)
        assert [e["name"] for e in _list_aim_prompts(proj) if e["source"] == "local"] == ["ok"]

    @requires_symlinks
    def test_a_kiro_link_that_stays_in_the_project_still_resolves(self, tmp_path, mock_sel):
        """...and the mention resolves it on every platform, so listed and serveable agree.

        On Windows ``hooks.validate_file_path`` screens each linked ancestor
        instead of refusing it: a link whose target is another local directory is
        rewritten to that target and the read proceeds, while a link aimed at a
        UNC share still refuses before ``realpath`` can probe it (pinned in
        ``test_hooks_coverage.py``). POSIX never walked the ancestors. Both
        platforms therefore serve this in-project link, which is what makes the
        listing above honest.
        """
        proj = self._linked_kiro_project(tmp_path)
        msg, status = _expand_prompt_mention("@ok", _State(), _Slot(project=proj))
        assert status == "ok" and "BODY" in msg


class TestAncestorSymlinkLoopCostsOneLibraryNotTheRequest:
    """An ANCESTOR loop must refuse the local root, never propagate.

    ``Path.resolve()`` signals a symlink loop with ``RuntimeError``, which is not
    an ``OSError``, and the root gate's containment check is the one place a loop
    can reach it: ``_linked_prompt_root`` asks ``os.path.islink``, which swallows
    the ``ELOOP`` and answers False, so a checkout shipping a cyclic ``.kiro``
    arrives at ``_local_prompt_dir_in_project``'s resolve undetected. That gate
    runs outside any broad catch on both enumerating callers — the listing scan
    and the exact-name lookup — so an escaping ``RuntimeError`` is a 500 on
    ``GET /api/prompts`` and on the unscoped detail lookup, not a lost entry.

    The ENTRY-level loop (``loop.md -> loop.md``) is a different gate and is
    pinned with the rest of the entry gate; this class covers the root.
    """

    @staticmethod
    def _looping_project(tmp_path):
        """A checkout whose ``.kiro`` is a two-link directory cycle.

        Planted with real symlinks, and therefore under ``requires_symlinks``: a
        junction cannot close a cycle at all. ``_winapi.CreateJunction`` checks
        ``GetFileAttributesW`` on the target before creating the link, so the
        target must already resolve — and in a cycle no member resolves until the
        last link is in place. The first call would fail with ``WinError 2``
        rather than plant anything, which is a broken fixture, not the defect
        under test. ``CreateSymbolicLinkW`` has no such requirement, so a symlink
        pair plants the cycle wherever the privilege exists; the marker PROBES for
        it, so a GitHub Windows runner (which holds it) still runs these and only
        an unprivileged shell skips them.

        Two names rather than one self-link keeps the shape a checkout would
        actually ship (``.kiro`` pointing at a sibling that points back), and it
        is a loop on every platform.
        """
        proj = tmp_path / "checkout"
        proj.mkdir()
        (proj / ".kiro").symlink_to(proj / "kiro-via", target_is_directory=True)
        (proj / "kiro-via").symlink_to(proj / ".kiro", target_is_directory=True)
        return proj

    @requires_symlinks
    def test_the_listing_endpoint_answers_200_and_no_local_entries(self, tmp_path, mock_sel):
        """The consequence a user would see: the Prompts tab still loads, minus a
        local library that never named a readable directory anyway."""
        _user_prompt(tmp_path, "mine", "global body")
        proj = self._looping_project(tmp_path)

        resp = asyncio.run(api_prompts(_list_request(proj)))
        assert resp.status == 200
        prompts = json.loads(resp.body)
        # The global half is proof the request was served rather than emptied.
        assert [p["name"] for p in prompts] == ["mine"]
        assert [p for p in prompts if p["source"] == "local"] == []

    @requires_symlinks
    def test_the_unscoped_detail_lookup_is_a_miss_not_a_500(self, tmp_path, mock_sel):
        """The second uncaught caller: ``_find_prompt``'s local half runs the same
        root gate from its own executor job, with no handler catch above it."""
        proj = self._looping_project(tmp_path)

        resp = asyncio.run(api_prompt_detail(_api_request("anything", project=proj)))
        assert resp.status == 404

    @requires_symlinks
    def test_the_mention_path_is_a_miss(self, tmp_path):
        """The chat surface, for completeness: a loop resolves nothing."""
        proj = self._looping_project(tmp_path)

        msg, status = _expand_prompt_mention("@anything", _State(), _Slot(project=proj))
        assert status == "not_found"

    def test_a_resolve_that_loops_refuses_the_root_on_every_platform(self, tmp_path, monkeypatch):
        """The gate's own answer, pinned platform-neutrally.

        The tests above plant a real cycle and so need the symlink privilege;
        which exception class the platform's ``resolve()`` then raises is its own
        choice. This one raises it directly, so the refusal the write verbs depend
        on stays pinned on every host including one that skips those:
        ``linked_prompt_root``, not a 500 and not a silent pass — a loop names no
        directory inside the project.
        """
        proj = tmp_path / "checkout"
        (proj / ".kiro" / "prompts").mkdir(parents=True)

        def looping_resolve(self, *args, **kwargs):
            raise RuntimeError(f"Symlink loop from {self!r}")

        monkeypatch.setattr(Path, "resolve", looping_resolve)
        assert _prompts_mod._resolve_prompt_dir("local", proj) == (None, "linked_prompt_root")


class TestARootSwappedAfterValidationPublishesNothing:
    """The prompt root is PINNED where it is validated, so a later swap costs the
    library rather than redirecting it.

    ``_resolve_prompt_dir`` decides that a root may be served. Every containment
    check downstream then re-resolved the same NAME — ``_prompt_dir_entry``'s
    parent comparison and the ``within_root`` its description read is pinned
    inside — so a root replaced by a link AFTER that decision resolved into the
    link's destination on BOTH sides of every later comparison and every file
    under the directory the swap named looked confined: published with a filename
    and first-heading description by ``GET /api/prompts`` and injectable in full
    by ``@<stem>``. Comparing against a value resolved once, before the swap,
    refuses them instead.

    The swap is injected deterministically by wrapping
    ``_local_prompt_scan_root`` — the function that TAKES the pin — so it lands in
    exactly the window under test and no timing is involved. A swap arriving
    EARLIER is a different case and is already refused, because the pinned value
    then escapes the project (``TestLocalScopeStaysInProject``).
    """

    @staticmethod
    def _plant(tmp_path) -> tuple[Path, Path, Path]:
        outside = tmp_path / "outside"
        outside.mkdir()
        # The heading is a plain marker, not a credential shape: it is what a
        # published description or a served body would carry verbatim, so an
        # assertion on it cannot be satisfied by the redaction pass instead of by
        # the refusal under test.
        (outside / "creds.md").write_text(
            "# OUTSIDE-ROOT-HEADING\naws_secret_access_key = SHOULD-NOT-APPEAR\n",
            encoding="utf-8",
        )
        proj = tmp_path / "checkout"
        d = proj / ".kiro" / "prompts"
        d.mkdir(parents=True)
        return proj, d, outside

    @staticmethod
    def _swap_root_after_the_pin(monkeypatch, prompts_dir: Path, outside: Path) -> None:
        import kiro_crew.dashboard.handlers as h

        real = _prompts_mod._local_prompt_scan_root

        def _pin_then_swap(project_dir):
            out = real(project_dir)
            if out is not None and prompts_dir.is_dir() and not prompts_dir.is_symlink():
                prompts_dir.rmdir()
                prompts_dir.symlink_to(outside)
            return out

        # Both modules hold their own binding of the name, and the listing reaches
        # it through the parent package while the mention lookup reaches it through
        # this module, so patching one would leave the other unswapped and the test
        # would pass without exercising the window.
        monkeypatch.setattr(_prompts_mod, "_local_prompt_scan_root", _pin_then_swap)
        monkeypatch.setattr(h, "_local_prompt_scan_root", _pin_then_swap)

    @requires_symlinks
    def test_the_listing_publishes_nothing_from_a_swapped_root(self, tmp_path, monkeypatch):
        proj, d, outside = self._plant(tmp_path)
        self._swap_root_after_the_pin(monkeypatch, d, outside)

        listed = _list_aim_prompts(proj)
        assert [p for p in listed if p["source"] == "local"] == []
        assert "OUTSIDE-ROOT-HEADING" not in json.dumps(listed)
        # Not vacuous: the swap landed, and the name the listing refused is one
        # that now resolves to a real file.
        assert d.is_symlink() and (d / "creds.md").is_file()

    @requires_symlinks
    def test_a_mention_does_not_resolve_through_a_swapped_root(self, tmp_path, monkeypatch):
        proj, d, outside = self._plant(tmp_path)
        self._swap_root_after_the_pin(monkeypatch, d, outside)

        msg, status = _expand_prompt_mention("@creds", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "OUTSIDE-ROOT-HEADING" not in msg
        assert d.is_symlink() and (d / "creds.md").is_file()

    # ── The same swap landing between the MINT and the READ ──
    #
    # The two tests above refuse the entry, so the read is never reached. A swap
    # arriving after a legitimate entry was minted is the other half: the entry is
    # real, its canonical path is real, and what decides whether an outside file of
    # the same name is served is where the read derives its root. These pin that it
    # is derived by re-running the scope's own gate — which SEES the swap — and not
    # assembled at the call site, which would have `safe_read_file_bytes_nolink`
    # `realpath` its way into the swapped root's destination.
    #
    # Injected at `validate_file_path`, so no timing is involved. WHICH binding is
    # patched decides where in the sequence it lands, and the two surfaces differ:
    # patching it on `chat_runner` fires at that module's own call, strictly after
    # the mint; patching it on `hooks` fires from inside the mint's own description
    # read (`safe_read_file_bytes_nolink` calls it immediately before its open), so
    # the entry there is minted with its containment already checked and the read
    # root is the only thing left that can refuse. Both leave the entry legitimate,
    # which is the point.
    #
    # What they do NOT pin, because a path-based `within_root` cannot deliver it, is
    # a swap landing between that derivation and the gate's own `realpath` — see
    # `prompts._prompt_read_root` for the recorded residual and what closes it.

    @staticmethod
    def _swap_root_before_the_read(monkeypatch, module, prompts_dir: Path, outside: Path) -> None:
        real_validate = module.validate_file_path

        def _validate_then_swap(raw):
            out = real_validate(raw)
            if out is not None and prompts_dir.is_dir() and not prompts_dir.is_symlink():
                prompts_dir.rename(prompts_dir.parent / "moved-aside")
                prompts_dir.symlink_to(outside)
            return out

        monkeypatch.setattr(module, "validate_file_path", _validate_then_swap)

    @requires_symlinks
    def test_a_mention_read_is_not_redirected_by_a_root_swapped_after_the_mint(
        self, tmp_path, monkeypatch
    ):
        proj, d, outside = self._plant(tmp_path)
        # Same stem as the outside file, so the swap alone decides which one is
        # read: the entry is legitimate and its own bytes are innocuous.
        (d / "creds.md").write_text("# Mine\nMINE\n", encoding="utf-8")
        from kiro_crew.dashboard import chat_runner as _cr

        self._swap_root_before_the_read(monkeypatch, _cr, d, outside)

        msg, status = _expand_prompt_mention("@creds", _State(), _Slot(project=proj))
        assert status == "not_found"
        assert "OUTSIDE-ROOT-HEADING" not in msg
        assert d.is_symlink() and (d / "creds.md").is_file()

    @requires_symlinks
    def test_the_unscoped_read_is_not_redirected_by_a_root_swapped_after_the_mint(
        self, tmp_path, monkeypatch, mock_sel
    ):
        from kiro_crew import hooks as _hooks

        proj, d, outside = self._plant(tmp_path)
        (d / "creds.md").write_text("# Mine\nMINE\n", encoding="utf-8")
        # ``_resolve_and_read`` imports the name inside the executor job, so the
        # swap is installed on the hooks module the job reaches.
        self._swap_root_before_the_read(monkeypatch, _hooks, d, outside)

        resp = asyncio.run(api_prompt_detail(_api_request("creds", project=proj)))
        # The property, on every platform: the swapped root's file is not served.
        assert b"OUTSIDE-ROOT-HEADING" not in resp.body
        # Refused by the read root: `_prompt_read_within_root` re-runs the
        # scope's gate, sees the linked root, and answers None. Windows reaches
        # the same stage: `hooks.validate_file_path` rewrites a local linked
        # ancestor to its target instead of refusing it, so the swapped root is
        # caught by this surface's own gate rather than one stage earlier.
        assert resp.status == 500
        assert mock_sel.log_tool_invocation.call_args[1]["outcome"] == "error"


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

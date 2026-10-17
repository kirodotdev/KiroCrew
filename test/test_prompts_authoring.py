"""Tests for the prompt authoring API: create, update and delete.

What each verb writes and answers: the slot and project a write resolves to, the
compare-and-swap on update, the coded refusal contract, the hardening refusals, the
scoped read that seeds the editor, and byte, encoding and permission fidelity, with
each verb's own link refusals. The defences every reader and writer shares (the
no-link read gate, the uniform link refusal, the pinned directory, local scope and
root swaps) are in ``test_prompts_link_gates.py``; who may write is in
``test_prompts_caller_gates.py``.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from prompts_helpers import _isolate_home  # noqa: F401
from prompts_helpers import _no_turn_tail  # noqa: F401
from prompts_helpers import aim_dir_fixture  # noqa: F401
from prompts_helpers import mock_sel_fixture  # noqa: F401
from prompts_helpers import (
    _ANY_HASH,
    _SLOT_KEY,
    _aim_pkg,
    _api_request,
    _claim_store,
    _create_request,
    _list_request,
    _listed_names,
    _outcomes,
    _sha,
    _Slot,
    _slot_state,
    _State,
    _user_prompt,
    _write_request,
)

from kiro_crew.dashboard.chat import _expand_prompt_mention
from kiro_crew.dashboard.handlers import (
    MAX_PROMPT_BYTES,
    api_prompt_detail,
    api_prompts,
    api_prompts_create,
)
from kiro_crew.dashboard.handlers import prompts as _prompts_mod


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

    def test_creates_in_local_scope_under_project(self, tmp_path, mock_sel):
        # The local project now comes from the request's chat slot (per-slot),
        # not the gateway-global _project_dir(); bind the slot via project=.
        proj = tmp_path / "proj"
        proj.mkdir()
        resp = asyncio.run(
            api_prompts_create(
                _create_request({"name": "p", "content": "x", "scope": "local"}, project=proj)
            )
        )
        assert resp.status == 201
        assert (proj / ".kiro" / "prompts" / "p.md").is_file()

    def test_local_create_is_listed_for_the_same_slot(self, tmp_path, mock_sel):
        """The create/list invariant, per slot: a local prompt created under a
        slot's project is then listed for that SAME slot. Both sides resolve
        "local" from the same per-slot project, so create and list agree."""
        proj = tmp_path / "proj"
        proj.mkdir()
        resp = asyncio.run(
            api_prompts_create(
                _create_request(
                    {"name": "slot-local", "content": "x", "scope": "local"}, project=proj
                )
            )
        )
        assert resp.status == 201
        # Listed for the SAME slot (bound to the same project) …
        assert "slot-local" in _listed_names(project=proj)

    def test_local_create_is_not_listed_for_a_different_slot(self, tmp_path, mock_sel):
        """A local prompt created under slot A's project must NOT leak into a
        different slot B bound to a different project. Per-slot resolution keeps
        each slot's local prompts to itself."""
        proj_a = tmp_path / "proj-a"
        proj_a.mkdir()
        proj_b = tmp_path / "proj-b"
        proj_b.mkdir()
        resp = asyncio.run(
            api_prompts_create(
                _create_request(
                    {"name": "a-only", "content": "x", "scope": "local"}, project=proj_a
                )
            )
        )
        assert resp.status == 201
        # Visible to slot A (its own project) …
        assert "a-only" in _listed_names(project=proj_a)
        # … but NOT to slot B, which is bound to a different project.
        assert "a-only" not in _listed_names(project=proj_b)
        # … and NOT to a slot with no project at all.
        assert "a-only" not in _listed_names()

    def test_session_key_header_selects_the_slots_project(self, tmp_path, mock_sel):
        """The step-1 header-selects-a-slot path the handlers actually rely on.

        With TWO real slots bound to different projects in one state,
        ``requesting_slot_project`` must resolve the project of the slot named
        by the ``X-Session-Key`` header — not a cross-slot fallback (there is
        none) and not the other slot's project. Each slot's own local prompt is
        listed only when its key is the one on the request; the OTHER slot's
        local prompt never leaks in. This is the multi-slot selection the
        empty-header single-slot tests never exercise."""
        proj_a = tmp_path / "sel-a"
        proj_a.mkdir()
        proj_b = tmp_path / "sel-b"
        proj_b.mkdir()
        # Author one local prompt under each project's .kiro/prompts.
        _user_prompt(proj_a, "in-a")
        _user_prompt(proj_b, "in-b")
        state = _slot_state(slots={"slot-a": proj_a, "slot-b": proj_b})

        # Header names slot A → A's project wins: A's local prompt, not B's.
        names_a = [
            p["name"]
            for p in json.loads(
                asyncio.run(api_prompts(_list_request(session_key="slot-a", state=state))).body
            )
        ]
        assert "in-a" in names_a
        assert "in-b" not in names_a

        # Header names slot B → B's project wins: B's local prompt, not A's.
        names_b = [
            p["name"]
            for p in json.loads(
                asyncio.run(api_prompts(_list_request(session_key="slot-b", state=state))).body
            )
        ]
        assert "in-b" in names_b
        assert "in-a" not in names_b

        # No/empty header, and the two slots disagree about the project → no
        # defensible answer, so neither slot's local prompt is listed. Picking
        # one would show the Prompts tab a checkout the user did not ask about,
        # and a following local write would land in it.
        names_none = [
            p["name"]
            for p in json.loads(
                asyncio.run(api_prompts(_list_request(session_key="", state=state))).body
            )
        ]
        assert "in-a" not in names_none
        assert "in-b" not in names_none

    def test_the_chat_and_http_surfaces_resolve_one_slot_identically(self, tmp_path, mock_sel):
        """The agreement the two surfaces are BUILT on, asserted rather than
        commented.

        ``chat_runner`` resolves an ``@mention``'s local scope by reading
        ``Path(slot.project)`` and the HTTP handlers resolve theirs through
        ``_prompt_local_project`` -> ``requesting_slot_project``. Nothing makes
        those two expressions equal except that they were written to be, so this
        pins the equality directly — and then pins the consequence, that a prompt
        one surface can resolve is one the other lists. Either expression drifting
        reds here instead of quietly showing one chat a neighbour's checkout.
        """
        proj_a = tmp_path / "agree-a"
        proj_a.mkdir()
        proj_b = tmp_path / "agree-b"
        proj_b.mkdir()
        _user_prompt(proj_a, "in-a")
        _user_prompt(proj_b, "in-b")
        state = _slot_state(slots={"slot-a": proj_a, "slot-b": proj_b})

        for key, stem in (("slot-a", "in-a"), ("slot-b", "in-b")):
            slot = state._slots[key]
            # The chat side's own expression, verbatim from _expand_prompt_mention.
            chat_project = Path(slot.project) if slot.project else None
            http_project = _prompts_mod._prompt_local_project(
                _list_request(session_key=key, state=state), state, key
            )
            assert http_project == chat_project
            # And the consequence: the mention that chat can resolve against its
            # slot is listed by the HTTP surface for the same slot, and the other
            # slot's prompt is absent from both.
            other = "in-b" if stem == "in-a" else "in-a"
            hit = _expand_prompt_mention(f"@{stem}", _State(), _Slot(project=slot.project))
            miss = _expand_prompt_mention(f"@{other}", _State(), _Slot(project=slot.project))
            assert hit[1] == "ok" and miss[1] == "not_found"
            listed = [
                p["name"]
                for p in json.loads(
                    asyncio.run(api_prompts(_list_request(session_key=key, state=state))).body
                )
            ]
            assert stem in listed and other not in listed

    def test_slotless_dashboard_request_uses_the_single_shared_project(self, tmp_path, mock_sel):
        """The overview Prompts tab and the command palette are the ONLY surfaces
        that offer "This project", and neither sits inside a chat: they send no
        slot key (or the shared ``dashboard:ui`` placeholder, which names no
        slot). A resolver with no fallback answers ``None`` for every request
        they can make, so the scope they render would be permanently dead. The
        single project every open slot shares is the answer for them, and it is
        the same one create resolves — so the round trip closes."""
        proj = tmp_path / "only"
        proj.mkdir()
        _user_prompt(proj, "shared-local")
        state = _slot_state(slots={"slot-a": proj, "slot-b": proj})

        for key in ("", "dashboard:ui"):
            names = [
                p["name"]
                for p in json.loads(
                    asyncio.run(api_prompts(_list_request(session_key=key, state=state))).body
                )
            ]
            assert "shared-local" in names, f"local scope went dark for session_key={key!r}"

        # …and a create from the same slotless surface lands in that project,
        # which is what makes list and create agree there.
        req = _create_request({"name": "made-here", "content": "x", "scope": "local"})
        req.headers = {}
        req.app = {"state": state}
        assert asyncio.run(api_prompts_create(req)).status == 201
        assert (proj / ".kiro" / "prompts" / "made-here.md").is_file()

    def test_a_named_slot_with_no_project_gets_no_neighbours_project(self, tmp_path, mock_sel):
        """The shared-project fallback is for SLOTLESS surfaces only. A request
        that names a real chat is speaking for that chat, and ``chat_runner``
        would match nothing for a chat with no project — so this surface must
        say the same, not offer the project a neighbouring chat happens to hold.
        Otherwise the tab would list a local prompt the chat cannot expand, and a
        local write from it would land in the neighbour's checkout."""
        proj = tmp_path / "neighbour"
        proj.mkdir()
        _user_prompt(proj, "neighbours-local")
        # "bare" exists but is bound to no project; "bound" holds the only project.
        state = _slot_state(slots={"bare": "", "bound": proj})

        names = [
            p["name"]
            for p in json.loads(
                asyncio.run(api_prompts(_list_request(session_key="bare", state=state))).body
            )
        ]
        assert "neighbours-local" not in names

        req = _create_request({"name": "leaked", "content": "x", "scope": "local"})
        req.headers = {"X-Session-Key": "bare"}
        req.app = {"state": state}
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "no_active_project"
        assert not (proj / ".kiro" / "prompts" / "leaked.md").exists()

    def test_slotless_local_create_refuses_when_slots_disagree(self, tmp_path, mock_sel):
        """Fail closed, not first-slot-wins: with two chats on different projects
        a slotless write has no defensible target, and guessing would create the
        file in the wrong checkout. The caller gets the existing
        ``no_active_project`` contract instead."""
        proj_a = tmp_path / "dis-a"
        proj_a.mkdir()
        proj_b = tmp_path / "dis-b"
        proj_b.mkdir()
        req = _create_request({"name": "ambiguous", "content": "x", "scope": "local"})
        req.headers = {}
        req.app = {"state": _slot_state(slots={"slot-a": proj_a, "slot-b": proj_b})}
        resp = asyncio.run(api_prompts_create(req))
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "no_active_project"
        assert not (proj_a / ".kiro" / "prompts" / "ambiguous.md").exists()
        assert not (proj_b / ".kiro" / "prompts" / "ambiguous.md").exists()

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
    the file does not match. Without this, an edit started before someone
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
            # flake-ok: pre-existing 200 ms delay inside the stubbed read that widens the check-to-write window, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed read's own delay, moved verbatim; tracked for a de-flake pass
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

    def test_create_maps_a_posix_too_long_joined_path_to_the_coded_400(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The name fits its budget, but the joined path did not fit this host.
        No pre-flight length can know that (a long-path-enabled Windows host takes
        what another refuses), so the create's own refusal is what is classified.

        Driven on the by-name branch (``_DIR_FD_SUPPORTED=False``) so the stub is
        deterministic on every host -- a host with ``dir_fd`` writes through
        ``os.write`` and one without through ``Path.open``, and pinning the branch
        removes that host-dependence rather than skipping the arm it does not take.
        """
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)

        def _boom(self, *a, **kw):
            raise OSError(errno.ENAMETOOLONG, "File name too long")

        monkeypatch.setattr(Path, "open", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"
        assert _outcomes(mock_sel)[-1] == "bad_request"
        assert not (tmp_path / ".kiro" / "prompts" / "p.md").exists()

    def test_create_maps_the_windows_too_long_error_on_the_by_name_branch(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The branch a Windows host takes (no dir_fd), raising what Windows really
        raises there: ``path.open`` is the CRT ``_wopen``, whose too-long refusal
        is a plain ``ENOENT`` with NO ``winerror``. The parent was just created,
        so the missing-file answer can only mean the joined path's length."""
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        monkeypatch.setattr(_prompts_mod, "_IS_WINDOWS", True)

        def _boom(self, *a, **kw):
            exc = OSError(errno.ENOENT, "No such file or directory")
            assert getattr(exc, "winerror", None) is None
            raise exc

        monkeypatch.setattr(Path, "open", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"
        assert _outcomes(mock_sel)[-1] == "bad_request"
        assert not (tmp_path / ".kiro" / "prompts" / "p.md").exists()

    def test_create_keeps_enoent_as_write_failed_when_the_parent_is_gone(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """The same plain ``ENOENT`` on Windows is NOT read as the length when the
        parent directory is missing after the ``mkdir``: then a missing parent is
        a real cause, and the generic ``write_failed`` handling keeps it."""
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        monkeypatch.setattr(_prompts_mod, "_IS_WINDOWS", True)
        monkeypatch.setattr(Path, "is_dir", lambda self: False)

        def _boom(self, *a, **kw):
            raise OSError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(Path, "open", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_create_keeps_plain_enoent_as_write_failed_off_windows(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """Off Windows a too-long path is ``ENAMETOOLONG``, so a plain ``ENOENT``
        from the by-name create keeps meaning what it says."""
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        monkeypatch.setattr(_prompts_mod, "_IS_WINDOWS", False)

        def _boom(self, *a, **kw):
            raise OSError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(Path, "open", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"

    def test_create_leaves_an_ambiguous_windows_error_as_write_failed(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """``ERROR_PATH_NOT_FOUND`` also names a missing parent, so it is not re-read
        as the length; it keeps the ordinary ``write_failed`` handling."""
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        monkeypatch.setattr(_prompts_mod, "_IS_WINDOWS", True)

        def _boom(self, *a, **kw):
            exc = OSError(errno.ENOENT, "The system cannot find the path specified")
            exc.winerror = 3
            raise exc

        monkeypatch.setattr(Path, "open", _boom)
        resp = asyncio.run(api_prompts_create(_create_request({"name": "p", "content": "x"})))
        assert resp.status == 500 and json.loads(resp.body)["code"] == "write_failed"
        assert _outcomes(mock_sel)[-1] == "error"

    def test_create_does_not_classify_a_write_the_host_accepts(
        self, tmp_path, mock_sel, monkeypatch
    ):
        """No pre-flight guess and no spurious refusal: when the filesystem accepts
        the joined path (no ``OSError`` at all), the create still returns 201, so a
        host that takes a long path -- a ``LongPathsEnabled`` Windows install, or
        any POSIX host -- loses nothing to the new classification. Deterministic on
        every host because it drives a write that simply succeeds rather than
        probing whether the host tolerates a path past ``MAX_PATH``."""
        monkeypatch.setattr(_prompts_mod, "_DIR_FD_SUPPORTED", False)
        name = "p" * 40
        resp = asyncio.run(api_prompts_create(_create_request({"name": name, "content": "x"})))
        assert resp.status == 201
        assert (tmp_path / ".kiro" / "prompts" / f"{name}.md").read_text() == "x"

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

    def _scoped(self, name, scope, project=None):
        r = MagicMock()
        r.method = "GET"
        r.match_info = {"name": name}
        r.query = {"scope": scope}
        # A scoped "local" read resolves its project through
        # _prompt_local_project(request, state, session_key): carry a real _slots
        # state, the X-Session-Key header, and the dashboard claim, and bind the
        # slot to *project* when one is given.
        r.headers = {"X-Session-Key": _SLOT_KEY}
        r.app = {"state": _slot_state(project)}
        return _claim_store(r)

    def test_same_stem_in_both_scopes_resolves_by_scope(self, tmp_path, mock_sel):
        """Unscoped resolution is first-match, so a shared stem is ambiguous — and
        an editor seeded from the wrong one would save under the other's scope."""
        _user_prompt(tmp_path, "dup", "GLOBAL BODY")
        proj = tmp_path / "proj"
        (proj / ".kiro" / "prompts").mkdir(parents=True)
        (proj / ".kiro" / "prompts" / "dup.md").write_text("LOCAL BODY")

        # The local project now comes from the request's chat slot (per-slot);
        # bind it via project= rather than the gateway-global _project_dir().
        g = json.loads(asyncio.run(api_prompt_detail(self._scoped("dup", "global"))).body)
        loc = json.loads(
            asyncio.run(api_prompt_detail(self._scoped("dup", "local", project=proj))).body
        )
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

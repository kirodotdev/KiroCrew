"""One Slack renderer for both ingress kinds, and images that flow both ways.

A session with a Slack attachment has ONE Slack output path: the
``SlackRenderer`` the transport dispatcher drives for a Slack-born turn is the
same object the dashboard turn loop drives for a dashboard-born turn on a linked
session. These tests pin that single path and the two image legs:

* outbound -- the dashboard's uploads directory is an approved upload root
  beside the session cwd, so a pasted or agent-drawn picture travels to the
  thread; the echo and the link-time history seed carry pictures too;
* inbound -- a picture posted in Slack is promoted into that same uploads
  directory and recorded in the transcript as ``![image](path)``, so the
  dashboard renders it after the turn instead of showing a dead temp path.

The dashboard-ingress turn tests drive the real ``_run_chat`` with a scripted
provider and a recording Slack client, and check every item a linked thread
receives from a turn -- echo, status, stream, task cards, reply, OPTIONS control,
approval prompt, teardown -- is provided by the renderer.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from conftest import make_dir_link, requires_symlinks
from kiro_crew import uploads
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner, chat_slack
from kiro_crew.messaging import TOOL_CALL, OutputEvent, outbound_files
from kiro_crew.messaging.attachments import (
    Attachment,
    IngestResult,
    append_attachment_context,
    ingest_attachments,
)
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.messaging.outbound_files import (
    REASON_SENSITIVE,
    REASON_SYMLINK,
    ApprovedRoot,
    approve_root,
    extract_local_refs,
)
from kiro_crew.security import REDACTED_CREDENTIAL_TAG
from kiro_crew.slack import renderer as slack_renderer_module
from kiro_crew.slack.renderer import (
    HISTORY_AGENT_ICON,
    HISTORY_USER_ICON,
    USER_ECHO_PREFIX,
    SlackRenderer,
    _approved_roots,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00picture-pixels"
KEY = "AKIAIOSFODNN7EXAMPLE"
SPLIT_KEY = "AKIA**IOSF**ODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def _quiet_sel():
    """No audit file is written by a renderer test."""
    fake = MagicMock()
    with patch("kiro_crew.slack.renderer.sel", return_value=fake):
        with patch("kiro_crew.messaging.attachments.sel", return_value=fake):
            yield fake


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch) -> Path:
    """Pin the shared uploads directory to a per-test path."""
    target = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "_UPLOAD_DIR", target)
    return target


class _RecSlack:
    """Recording ``SlackClientOps`` stand-in for everything the renderer calls."""

    def __init__(self, *, streaming: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[dict[str, Any]] = []
        self.streaming = streaming
        self.fail_post = False
        self._n = 0

    def _ts(self) -> str:
        self._n += 1
        return f"ts-{self._n}"

    async def start_stream(self, channel, thread_ts, **kw):
        self.calls.append(("start_stream", {"thread_ts": thread_ts, "user_id": kw.get("user_id")}))
        return self._ts() if self.streaming else None

    async def append_stream(self, channel, ts, text):
        self.calls.append(("append_stream", {"text": text}))
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.calls.append(("stop_stream", {"ts": ts, "final_text": final_text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, **kw):
        self.calls.append(
            (
                "append_task",
                {
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": kw.get("details", ""),
                },
            )
        )
        return True

    async def post_message(self, channel, text, thread_ts=None, **kw):
        if self.fail_post:
            raise RuntimeError("slack down")
        self.calls.append(("post_message", {"text": text, "thread_ts": thread_ts}))
        return self._ts()

    async def update_message(self, channel, ts, text="", blocks=None):
        self.calls.append(("update_message", {"text": text}))

    async def delete_message(self, channel, ts):
        self.calls.append(("delete_message", {"ts": ts}))

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw):
        self.calls.append(("post_blocks", {"blocks": blocks, "text": text}))
        return self._ts()

    async def set_thread_status(self, channel, thread_ts, status):
        self.calls.append(("set_thread_status", {"status": status}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        with open(file, "rb") as fh:
            data = fh.read()
        self.uploads.append(
            {"filename": filename, "title": title, "data": data, "thread": thread_ts}
        )

    # -- assertion helpers --
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    def shown(self) -> str:
        return "\n".join(str(kw.get("text") or kw.get("final_text") or "") for _, kw in self.calls)

    def displayed_task_cards(self) -> str:
        raw = "\n".join(f"{card['title']}\n{card['details']}" for card in self.of("append_task"))
        return canonicalize_display(raw)


def _renderer(slack: _RecSlack, cwd: str | None = None, **kw) -> SlackRenderer:
    renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False, show_thinking=False, **kw)
    if cwd is not None:
        renderer.authorize_upload_root(cwd)
    return renderer


# ---------------------------------------------------------------------------
# 1. The extractor accepts several roots and pins the read to the matching one
# ---------------------------------------------------------------------------


class TestMultiRootExtraction:
    def test_a_file_in_the_uploads_root_is_extracted(self, tmp_path) -> None:
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        pic = up / "abc_shot.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"look ![shot]({pic})", within_root=[str(cwd), str(up)])

        assert [f.path for f in result.files] == [str(pic)]
        assert result.rejections == []
        assert "![shot]" not in result.rewritten_text

    def test_a_file_in_the_cwd_root_is_extracted(self, tmp_path) -> None:
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        pic = cwd / "chart.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![c]({pic})", within_root=[str(cwd), str(up)])

        assert [f.path for f in result.files] == [str(pic)]

    def test_a_sensitive_path_is_refused_under_any_root(self, tmp_path, monkeypatch) -> None:
        # The candidate sits INSIDE an approved root, so containment admits it and
        # the denylist is the gate that has to refuse: a file outside both roots
        # would be refused by containment first and prove nothing about the
        # denylist. HOME stays as it is -- the denylist's own anchors are not
        # moved -- and the check is stubbed to flag this one path, so the test
        # pins that the extractor CONSULTS it and honours its answer.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        creds = up / "credentials.png"
        creds.write_bytes(PNG)
        seen: list[str] = []

        def _flag(candidate: str) -> bool:
            seen.append(candidate)
            return os.path.abspath(candidate) == str(creds)

        monkeypatch.setattr(outbound_files, "is_sensitive_path", _flag)

        result = extract_local_refs(f"![x]({creds})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [(r.reason, r.detail) for r in result.rejections] == [
            (REASON_SENSITIVE, "reading this location is blocked")
        ]
        assert seen == [str(creds)]
        assert str(creds) in result.rewritten_text  # the markup stays visible

    def test_a_root_outside_both_trees_is_refused(self, tmp_path) -> None:
        cwd, up, elsewhere = tmp_path / "cwd", tmp_path / "uploads", tmp_path / "elsewhere"
        cwd.mkdir(), up.mkdir(), elsewhere.mkdir()
        pic = elsewhere / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SENSITIVE]

    @requires_symlinks
    def test_a_symlink_into_the_uploads_root_is_refused(self, tmp_path) -> None:
        # Planted in the cwd, pointing into uploads: the leaf-symlink refusal
        # catches it, and even a link the leaf check missed would fail the read
        # gate, which pins the opened descriptor to the root the NAME matched.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        real = up / "real.png"
        real.write_bytes(PNG)
        link = cwd / "link.png"
        link.symlink_to(real)

        result = extract_local_refs(f"![x]({link})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SYMLINK]

    def test_a_linked_directory_cannot_launder_one_root_through_another(self, tmp_path) -> None:
        # The leaf is a regular file but an ANCESTOR under cwd is a link into
        # uploads. The name matches the cwd root, the descriptor resolves under
        # uploads, so the read pinned to cwd refuses it -- the union of two roots
        # must not admit what neither root would alone.
        cwd, up = tmp_path / "cwd", tmp_path / "uploads"
        cwd.mkdir(), up.mkdir()
        (up / "inner").mkdir()
        real = up / "inner" / "pic.png"
        real.write_bytes(PNG)
        make_dir_link(cwd / "alias", up / "inner")
        via_link = cwd / "alias" / "pic.png"

        result = extract_local_refs(f"![x]({via_link})", within_root=[str(cwd), str(up)])

        assert result.files == []
        assert len(result.rejections) == 1

    def test_a_single_string_root_still_works(self, tmp_path) -> None:
        pic = tmp_path / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=str(tmp_path))

        assert [f.path for f in result.files] == [str(pic)]

    def test_an_empty_root_list_refuses_everything(self, tmp_path) -> None:
        pic = tmp_path / "p.png"
        pic.write_bytes(PNG)

        result = extract_local_refs(f"![x]({pic})", within_root=[])

        assert result.files == []
        assert [r.reason for r in result.rejections] == [REASON_SENSITIVE]

    def test_a_root_replaced_after_approval_cannot_redefine_the_tree(self, tmp_path) -> None:
        # uploads/ is approved while it is a real directory, then swapped for a
        # link into another tree that holds a raster. The reference is spelled
        # under the approved root, so the NAME is admitted -- and the opened
        # inode resolves under the link's target, outside the identity the root
        # had when it was approved, so the pinned read refuses it. A root
        # re-resolved at read time would have followed the swap and admitted
        # every raster in the other tree.
        cwd, up, elsewhere = tmp_path / "cwd", tmp_path / "uploads", tmp_path / "elsewhere"
        cwd.mkdir(), up.mkdir(), elsewhere.mkdir()
        roots = [approve_root(cwd), approve_root(up)]
        assert all(isinstance(r, ApprovedRoot) for r in roots)
        up.rmdir()
        make_dir_link(up, elsewhere)
        (elsewhere / "leak.png").write_bytes(PNG)
        pic = up / "leak.png"

        result = extract_local_refs(f"![x]({pic})", within_root=roots)

        assert result.files == []
        assert len(result.rejections) == 1
        assert str(pic) in result.rewritten_text

    def test_a_root_approved_after_the_swap_resolves_to_the_swapped_target(self, tmp_path) -> None:
        # The contrast that gives the test above its teeth: approve the SAME
        # spelling only after the swap, and the root IS the link's target, so the
        # same reference is admitted. What protects the thread is that the Slack
        # renderer approves its roots when the turn authorizes them, not at
        # each read (see ``_approved_roots``).
        if os.name == "nt":
            pytest.skip("a linked ancestor is refused before the read on Windows")
        cwd, up, elsewhere = tmp_path / "cwd", tmp_path / "uploads", tmp_path / "elsewhere"
        cwd.mkdir(), elsewhere.mkdir()
        make_dir_link(up, elsewhere)
        (elsewhere / "leak.png").write_bytes(PNG)
        pic = up / "leak.png"

        result = extract_local_refs(
            f"![x]({pic})", within_root=[approve_root(cwd), approve_root(up)]
        )

        assert [f.path for f in result.files] == [str(pic)]

    def test_approve_root_refuses_what_is_not_a_tree(self, tmp_path) -> None:
        assert approve_root("") is None
        assert approve_root("relative/dir") is None
        approved = approve_root(tmp_path)
        assert approved is not None
        assert approved.spelled == os.path.abspath(tmp_path)
        assert approved.canonical == os.path.realpath(tmp_path)

    def test_approve_root_can_refuse_a_link_at_the_name(self, tmp_path) -> None:
        elsewhere, link = tmp_path / "elsewhere", tmp_path / "uploads"
        elsewhere.mkdir()
        make_dir_link(link, elsewhere)
        assert approve_root(link, refuse_link=True) is None
        assert approve_root(link) is not None  # the default follows the cwd rule
        assert approve_root(elsewhere, refuse_link=True) is not None


class TestCreateUploadFileIsAllOrNothing:
    def test_a_short_write_is_completed(self, uploads_dir, monkeypatch) -> None:
        # os.write may return a short count without raising (a nearly full disk
        # is the ordinary case); every byte must still land.
        real_write = os.write
        calls: list[int] = []

        def _short(fd, view):
            n = min(len(view), 3)
            calls.append(n)
            return real_write(fd, bytes(view[:n]))

        monkeypatch.setattr(uploads.os, "write", _short)
        dest = uploads.create_upload_file("shot.png", PNG)
        assert dest.read_bytes() == PNG
        assert len(calls) > 1

    def test_a_failed_write_leaves_no_partial_file(self, uploads_dir, monkeypatch) -> None:
        # The promotion deletes its temp source once this returns, so a partial
        # destination left behind would be the only "copy" of the picture.
        real_write = os.write
        state = {"n": 0}

        def _fail_second(fd, view):
            state["n"] += 1
            if state["n"] == 1:
                return real_write(fd, bytes(view[:4]))
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(uploads.os, "write", _fail_second)
        with pytest.raises(OSError):
            uploads.create_upload_file("shot.png", PNG)
        assert list(uploads_dir.iterdir()) == []

    def test_a_zero_length_write_that_makes_no_progress_fails_closed(
        self, uploads_dir, monkeypatch
    ) -> None:
        monkeypatch.setattr(uploads.os, "write", lambda fd, view: 0)
        with pytest.raises(OSError):
            uploads.create_upload_file("shot.png", PNG)
        assert list(uploads_dir.iterdir()) == []


class TestCreateUploadFileNeverFollowsAPlantedLink:
    """``uploads/`` shares a data home with the agent. A link planted at that name
    must never turn a promotion into a write wherever the link points."""

    def test_a_symlinked_uploads_directory_is_refused(self, tmp_path, monkeypatch) -> None:
        # ``make_dir_link`` plants a junction on Windows and a symlink elsewhere,
        # so the refusal is exercised on every platform rather than skipped
        # where a symlink needs a privilege.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = tmp_path / "uploads"
        make_dir_link(link, elsewhere)
        monkeypatch.setattr(uploads, "_UPLOAD_DIR", link)

        with pytest.raises(OSError):
            uploads.create_upload_file("shot.png", PNG)

        assert list(elsewhere.iterdir()) == [], "nothing was written through the link"

    def test_a_real_directory_is_created_and_written_through_its_pinned_parent(
        self, uploads_dir
    ) -> None:
        # The directory does not exist yet: it is created relative to its pinned
        # parent, the file relative to the directory, owner-only and exclusive.
        assert not uploads_dir.exists()
        dest = uploads.create_upload_file("shot.png", PNG)
        assert dest.parent == uploads_dir and dest.read_bytes() == PNG
        if os.name != "nt":
            assert stat.S_IMODE(dest.stat().st_mode) == 0o600

    def test_a_failed_write_unlinks_through_the_descriptor(self, uploads_dir, monkeypatch) -> None:
        monkeypatch.setattr(uploads.os, "write", lambda fd, view: 0)
        with pytest.raises(OSError):
            uploads.create_upload_file("shot.png", PNG)
        assert list(uploads_dir.iterdir()) == []

    def test_without_pinned_walk_pins_before_open_and_through_the_write(
        self, uploads_dir, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(uploads.pinned_fs, "supports_pinned_walk", lambda: False)
        pin_file = tmp_path / "pin-sentinel"
        pin_file.write_bytes(b"")
        pin_fd = os.open(pin_file, os.O_RDONLY)
        real_open = uploads.os.open
        real_write = uploads.os.write
        real_close = uploads.os.close
        events: list[str] = []

        def _pin(path):
            assert Path(path) == uploads_dir
            events.append("pin")
            return pin_fd

        def _open(path, flags, mode=0o777, *, dir_fd=None):
            events.append(f"open {os.fspath(path)}")
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def _write(fd, view):
            events.append("write")
            return real_write(fd, view)

        def _close(fd):
            events.append(f"close {fd}")
            return real_close(fd)

        monkeypatch.setattr(uploads.platform_compat, "pin_directory", _pin)
        monkeypatch.setattr(uploads.os, "open", _open)
        monkeypatch.setattr(uploads.os, "write", _write)
        monkeypatch.setattr(uploads.os, "close", _close)

        dest = uploads.create_upload_file("shot.png", PNG)

        assert events.index("pin") < events.index(f"open {dest}")
        assert events.index(f"close {pin_fd}") > max(
            index for index, event in enumerate(events) if event == "write"
        )
        assert events.count(f"close {pin_fd}") == 1
        assert dest.read_bytes() == PNG

    def test_without_pinned_walk_a_pin_refusal_creates_no_file(
        self, uploads_dir, monkeypatch
    ) -> None:
        monkeypatch.setattr(uploads.pinned_fs, "supports_pinned_walk", lambda: False)

        def _refuse_pin(path):
            raise NotADirectoryError(20, "not a real directory", os.fspath(path))

        monkeypatch.setattr(uploads.platform_compat, "pin_directory", _refuse_pin)

        with pytest.raises(NotADirectoryError):
            uploads.create_upload_file("shot.png", PNG)

        assert uploads_dir.is_dir()
        assert list(uploads_dir.iterdir()) == []

    def test_without_pinned_walk_a_write_failure_closes_the_pin(
        self, uploads_dir, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(uploads.pinned_fs, "supports_pinned_walk", lambda: False)
        pin_file = tmp_path / "pin-sentinel"
        pin_file.write_bytes(b"")
        pin_fd = os.open(pin_file, os.O_RDONLY)
        real_close = uploads.os.close
        closed: list[int] = []

        def _close(fd):
            closed.append(fd)
            return real_close(fd)

        def _fail_write(fd, view):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(uploads.platform_compat, "pin_directory", lambda path: pin_fd)
        monkeypatch.setattr(uploads.os, "close", _close)
        monkeypatch.setattr(uploads.os, "write", _fail_write)

        try:
            with pytest.raises(OSError):
                uploads.create_upload_file("shot.png", PNG)
        finally:
            if pin_fd not in closed:
                real_close(pin_fd)

        assert closed.count(pin_fd) == 1
        assert list(uploads_dir.iterdir()) == []


_MD_DEST_FIXTURE = Path(__file__).parent / "fixtures" / "markdown_image_dest.json"
_MD_DEST_TABLE = json.loads(_MD_DEST_FIXTURE.read_text(encoding="utf-8"))


class TestMarkdownImageDestMatchesTheDashboardProducer:
    """One spelling rule for ``![image](...)`` destinations, the composer's own
    (``mdImageDest`` in website/src/utils/fileTokens.ts).

    The cases are not written here: both sides walk the SAME table,
    ``test/fixtures/markdown_image_dest.json`` -- the producer and its inverse in
    TypeScript (``website/src/test/fileTokens.test.ts``) and the producer and the
    outbound scanner in Python (this class) -- so a change to either grammar fails
    the suite on the side that made it instead of silently breaking the other
    side's round-trip. A new case belongs in the fixture, not in either test."""

    def test_the_table_covers_the_grammar(self) -> None:
        # Enough rows to be a contract rather than a smoke test, and every
        # branch of the producer represented: pass-through, Windows slashes,
        # the wrap, the three escapes, the ASCII alphabet.
        dests = [c["dest"] for c in _MD_DEST_TABLE["cases"]]
        assert len(dests) >= 10
        assert any(not d.startswith("<") for d in dests)
        assert any(d.startswith("<") for d in dests)
        assert any("%25" in d for d in dests)
        assert any("\\<" in d and "\\>" in d for d in dests)
        assert any("\\\\" in d for d in dests)

    @pytest.mark.parametrize(
        "case", _MD_DEST_TABLE["cases"], ids=[c["name"] for c in _MD_DEST_TABLE["cases"]]
    )
    def test_the_producer_spells_each_path_as_the_table_says(self, case: dict) -> None:
        assert uploads.markdown_image_dest(case["path"]) == case["dest"]

    @pytest.mark.parametrize(
        "case", _MD_DEST_TABLE["cases"], ids=[c["name"] for c in _MD_DEST_TABLE["cases"]]
    )
    def test_the_outbound_scanner_reads_each_destination_back(self, case: dict) -> None:
        # The inverse the channels actually run: the scanner that finds the
        # reference in a reply and decodes exactly the wrapped form. A ``//``
        # spelling is a remote reference to it, never a local file.
        found = [ref.dest for ref in outbound_files.iter_local_refs(f"![image]({case['dest']})")]
        if case.get("outbound_local", True):
            assert found == [case["decoded"]]
        else:
            assert found == []

    @pytest.mark.parametrize(
        "case",
        _MD_DEST_TABLE["legacy_bare"],
        ids=[c["name"] for c in _MD_DEST_TABLE["legacy_bare"]],
    )
    def test_an_unwrapped_legacy_destination_is_never_decoded(self, case: dict) -> None:
        found = [ref.dest for ref in outbound_files.iter_local_refs(f"![image]({case['dest']})")]
        assert found == [case["decoded"]]

    def test_the_inbound_row_uses_the_rule(self) -> None:
        from kiro_crew.messaging.attachments import image_row_markdown

        assert image_row_markdown(r"C:\Users\John Doe\.kiro\crew\uploads\shot.png") == (
            "![image](<C:/Users/John Doe/.kiro/crew/uploads/shot.png>)"
        )
        assert image_row_markdown("/home/u/.kiro/crew/uploads/x_shot.png") == (
            "![image](/home/u/.kiro/crew/uploads/x_shot.png)"
        )

    def test_the_prompt_encoder_still_finds_a_wrapped_windows_path(self) -> None:
        # One string serves the row AND the prompt: the ACP encoder's Windows
        # grammar must locate the image inside the ``<...>`` wrap.
        from kiro_crew.acp import prompt_blocks

        row = f"![image]({uploads.markdown_image_dest(r'C:\Users\John Doe\uploads\shot.png')})"
        found = [m.group(1) for m in prompt_blocks._WINDOWS_PATH_RE.finditer(row)]
        assert found == ["C:/Users/John Doe/uploads/shot.png"]


class TestQueueMergeNeverMixesIngress:
    def test_a_slack_routed_entry_is_not_merged_with_a_dashboard_typed_one(self, tmp_path):
        # A merged batch takes one ingress value for the whole turn. Mixing would
        # either echo a Slack-born message back into its thread or withhold the
        # echo a dashboard-typed one is owed, so the merge run stops at the seam.
        from kiro_crew.dashboard.chat_utils import _dequeue_next_message

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("q1")
        slot.queue_append("from dashboard 1", directive_user_origin=True)
        slot.queue_append("from dashboard 2", directive_user_origin=True)
        slot.queue_append("from slack", directive_user_origin=True, ingress="slack")
        slot.queue_append("from dashboard 3", directive_user_origin=True)

        text, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert [c["content"] for c in consumed] == ["from dashboard 1", "from dashboard 2"]
        assert all(c.get("_ingress", "") == "" for c in consumed)

        text, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert [c["content"] for c in consumed] == ["from slack"]
        assert consumed[0]["_ingress"] == "slack"

        text, consumed = _dequeue_next_message(slot, merge_enabled=True)
        assert [c["content"] for c in consumed] == ["from dashboard 3"]


class TestApprovedRoots:
    def test_the_uploads_dir_rides_beside_a_valid_cwd(self, uploads_dir, tmp_path) -> None:
        uploads_dir.mkdir()
        roots = _approved_roots(str(tmp_path))
        assert [r.spelled for r in roots] == [str(tmp_path), str(uploads_dir)]
        # Approved HERE, identity fixed: the canonical form is what the read
        # verifies against, and nothing resolves the root again later.
        assert [r.canonical for r in roots] == [
            os.path.realpath(tmp_path),
            os.path.realpath(uploads_dir),
        ]

    def test_an_invalid_cwd_authorizes_nothing_not_even_uploads(self, uploads_dir) -> None:
        assert _approved_roots("") == ()
        assert _approved_roots("relative/dir") == ()

    def test_an_uploads_dir_that_does_not_exist_yet_authorizes_only_the_cwd(
        self, uploads_dir, tmp_path
    ) -> None:
        # The identity comes from a pinned open of the directory, so a directory
        # that is not there yet has none; it becomes a root on the next turn, once
        # the first promotion or paste has created it. Nothing under it could be
        # referenced before then.
        assert not uploads_dir.exists()
        assert [r.spelled for r in _approved_roots(str(tmp_path))] == [str(tmp_path)]

    def test_a_link_at_uploads_authorizes_only_the_cwd(self, uploads_dir, tmp_path) -> None:
        # The inbound promotion refuses a link sitting at uploads/ when it pins
        # the directory; the outbound side holds the same line, so a replaced
        # uploads/ ships nothing while the cwd root is unaffected.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        make_dir_link(uploads_dir, elsewhere)
        cwd = tmp_path / "cwd"
        cwd.mkdir()

        assert [r.spelled for r in _approved_roots(str(cwd))] == [str(cwd)]

    @pytest.mark.asyncio
    async def test_an_agent_picture_in_the_uploads_dir_reaches_the_thread(
        self, uploads_dir, tmp_path
    ) -> None:
        # The dashboard-pasted case: the dashboard composer wrote the
        # picture under uploads/, the transcript names that path, and the reply
        # references it. With uploads/ an approved root, it travels.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_pasted.png"
        pic.write_bytes(PNG)
        slack = _RecSlack(streaming=False)
        renderer = _renderer(slack, cwd=str(tmp_path / "project"))

        await renderer.on_text_chunk(f"as you pasted:\n\n![image]({pic})\n")
        await renderer.on_done(stop_reason="end_turn")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()


class TestUploadRootApprovalStaysOffTheLoop:
    """Approving a root resolves the cwd and pins the uploads directory, filesystem
    work a stalled mount would hold the gateway loop on, so the two production
    callers await the off-loop twin and only the result lands on the loop."""

    @pytest.mark.asyncio
    async def test_the_off_loop_twin_approves_in_a_worker_thread(self, uploads_dir, tmp_path):
        import threading

        uploads_dir.mkdir()
        slack = _RecSlack()
        renderer = _renderer(slack)
        seen: list[int] = []
        real = slack_renderer_module._approved_roots

        def _spy(cwd: str):
            seen.append(threading.get_ident())
            return real(cwd)

        with patch.object(slack_renderer_module, "_approved_roots", _spy):
            await renderer.authorize_upload_root_off_loop(str(tmp_path))

        assert seen and seen[0] != threading.get_ident(), "approval ran on the loop thread"
        assert [r.spelled for r in renderer._upload_roots] == [str(tmp_path), str(uploads_dir)]
        # Same answer as the synchronous form.
        other = _renderer(slack, cwd=str(tmp_path))
        assert other._upload_roots == renderer._upload_roots

    def test_both_production_callers_await_the_off_loop_twin(self) -> None:
        from kiro_crew.dashboard import slack_mirror
        from kiro_crew.slack import transport_dispatch

        for module in (slack_mirror, transport_dispatch):
            src = inspect.getsource(module)
            assert "await renderer.authorize_upload_root_off_loop(" in src, module.__name__
            assert "renderer.authorize_upload_root(" not in src, module.__name__


class TestMarkupSplitCredentialHoldback:
    """A key the model splits WITH markup -- ``[AKIA](url)`` ending one chunk, the
    rest opening the next -- renders whole on Slack while every raw scan sees the
    ``)`` end the run. The renderer holds back the raw tail whose DISPLAY form is
    a credential run, so the joined run is scanned as Slack shows it."""

    def test_the_boundary_helper_holds_the_markup_tail(self) -> None:
        from kiro_crew.slack.renderer import (
            _DISPLAY_HOLD_MAX,
            _DISPLAY_HOLD_SCAN,
            _split_trailing_display_run,
        )

        link = "[AKIA](https://example.invalid/a-long-path)"
        assert _split_trailing_display_run(f"your key is {link}") == ("your key is ", link)
        # Emphasis and Slack's own link grammar render away the same way.
        assert _split_trailing_display_run("shout **AKIA**IOSF") == ("shout ", "**AKIA**IOSF")
        assert _split_trailing_display_run("see <https://x.invalid|AKIA>") == (
            "see ",
            "<https://x.invalid|AKIA>",
        )
        # No markup in the run: raw and display forms coincide, and the raw
        # holdback upstream already covers it, so nothing is held here.
        assert _split_trailing_display_run("plain AKIAIOSFODNN7") == ("plain AKIAIOSFODNN7", "")
        assert _split_trailing_display_run("hello world") == ("hello world", "")
        # Whitespace at the end closes every run.
        assert _split_trailing_display_run(f"{link} ") == (f"{link} ", "")
        # An image reference belongs to the ref hold, whole: cutting between its
        # ``!`` and ``[`` would bisect the markup the uploader has to see.
        assert _split_trailing_display_run("Here: ![c](/tmp/chart.png)") == (
            "Here: ![c](/tmp/chart.png)",
            "",
        )
        # Bounds: an over-long display run or raw run is not a key mid-flight.
        long_label = "A" * (_DISPLAY_HOLD_MAX + 1)
        assert _split_trailing_display_run(f"x [{long_label}](https://x)") == (
            f"x [{long_label}](https://x)",
            "",
        )
        long_url = "b" * (_DISPLAY_HOLD_SCAN + 1)
        assert _split_trailing_display_run(f"x [a](https://{long_url})") == (
            f"x [a](https://{long_url})",
            "",
        )

    @pytest.mark.asyncio
    async def test_a_key_split_by_a_link_never_renders_whole_in_the_stream(self) -> None:
        # Throttled flush between the two chunks, so the first would otherwise
        # be appended on its own with the link intact.
        head, tail = KEY[:4], KEY[4:]
        clock = iter([1000.0, 1000.0, 1002.0, 1004.0, 1006.0, 1008.0])
        slack = _RecSlack()
        renderer = SlackRenderer(
            slack, "C1", "t1", reactions_enabled=False, show_thinking=False, now=lambda: next(clock)
        )
        await renderer.on_text_chunk(f"your key is [{head}](https://example.invalid/a-long-path)")
        await renderer.on_text_chunk(f"{tail} and that is all")
        await renderer.on_done(stop_reason="end_turn")

        appended = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert KEY not in canonicalize_display(appended), appended
        assert KEY not in canonicalize_display(slack.shown())
        assert REDACTED_CREDENTIAL_TAG in appended
        assert "and that is all" in appended


# ---------------------------------------------------------------------------
# 2. The two entry points a Slack-born turn never needed
# ---------------------------------------------------------------------------


class TestEchoUserMessage:
    @pytest.mark.asyncio
    async def test_the_echo_is_italic_behind_a_speech_balloon(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        await renderer.echo_user_message("what does this do?")

        assert slack.of("post_message") == [
            {"text": f"{USER_ECHO_PREFIX} _what does this do?_", "thread_ts": "t1"}
        ]

    @pytest.mark.asyncio
    async def test_redaction_runs_over_the_full_text_before_the_cut(self) -> None:
        # The credential sits past the 500-character cut. Cutting first would
        # split it into unmatchable fragments; redacting first catches it whole
        # and the echo is then cut to length.
        slack = _RecSlack()
        renderer = _renderer(slack)
        text = "x" * 490 + " " + KEY

        await renderer.echo_user_message(text)

        shown = slack.of("post_message")[0]["text"]
        assert KEY not in shown
        assert len(shown) <= len(f"{USER_ECHO_PREFIX} __") + 500

    @pytest.mark.asyncio
    async def test_a_pasted_picture_is_uploaded_after_the_echo(self, uploads_dir, tmp_path) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.echo_user_message(f"why is this red?\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _why is this red?_"
        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()

    @pytest.mark.asyncio
    async def test_extraction_precedes_the_length_cut(self, uploads_dir, tmp_path) -> None:
        # The image reference sits past the 500-char echo cut. Cutting first
        # would bisect the markup and lose the picture; extracting first sees it
        # whole, so the picture is uploaded and the text is cut afterwards.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_late.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.echo_user_message("y" * 600 + f"\n![image]({pic})")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert "![image]" not in slack.shown()

    @pytest.mark.asyncio
    async def test_no_roots_means_the_path_stays_prose_and_nothing_ships(self, tmp_path) -> None:
        pic = tmp_path / "shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack)  # no authorize_upload_root

        await renderer.echo_user_message(f"see ![image]({pic})")

        assert slack.uploads == []
        assert str(pic) in slack.shown()

    @pytest.mark.asyncio
    async def test_a_slack_refusal_never_raises(self) -> None:
        slack = _RecSlack()
        slack.fail_post = True
        renderer = _renderer(slack)

        await renderer.echo_user_message("hello")  # must not raise


class TestPostHistoryRow:
    @pytest.mark.asyncio
    async def test_roles_get_their_icons(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        assert await renderer.post_history_row("user", "hi") is True
        assert await renderer.post_history_row("assistant", "hello") is True

        texts = [kw["text"] for kw in slack.of("post_message")]
        assert texts == [f"{HISTORY_USER_ICON} hi", f"{HISTORY_AGENT_ICON} hello"]

    @pytest.mark.asyncio
    async def test_a_row_with_a_picture_uploads_it_after_the_text(
        self, uploads_dir, tmp_path
    ) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        slack = _RecSlack()
        renderer = _renderer(slack, cwd=str(tmp_path))

        await renderer.post_history_row("user", f"this one\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{HISTORY_USER_ICON} this one"
        assert [u["data"] for u in slack.uploads] == [PNG]

    @pytest.mark.asyncio
    async def test_a_credential_in_history_is_redacted(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        await renderer.post_history_row("assistant", f"your key is {KEY}")

        assert KEY not in slack.shown()

    @pytest.mark.asyncio
    async def test_a_failed_post_reports_false(self) -> None:
        slack = _RecSlack()
        slack.fail_post = True
        renderer = _renderer(slack)

        assert await renderer.post_history_row("user", "hi") is False


class TestTeardownAndApprovalGuard:
    @pytest.mark.asyncio
    async def test_native_dispatch_redacts_markdown_split_tool_fields(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)

        await renderer.dispatch(
            OutputEvent(
                kind=TOOL_CALL,
                tool_call_id="tc1",
                title=f"Running: {SPLIT_KEY}",
                tool_name="shell",
                tool_purpose=f"Use {SPLIT_KEY}",
            )
        )

        assert KEY not in slack.displayed_task_cards()
        assert SPLIT_KEY not in str(slack.of("append_task"))

    @pytest.mark.asyncio
    async def test_close_finishes_an_unfinished_turns_surface(self) -> None:
        # A turn that never reached on_done: the open card is completed, the
        # stream stopped and the status cleared, so the thread reads as ended.
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_tool_call("tc1", "Running: shell", tool_kind="execute")

        await renderer.close()

        statuses = [kw["status"] for kw in slack.of("append_task")]
        assert statuses == ["in_progress", "complete"]
        assert len(slack.of("stop_stream")) == 1
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_close_flushes_what_the_stream_still_holds(self) -> None:
        # A provider failure after a throttled chunk: the buffered text and the
        # tail withheld for a seal that will never run are appended before the
        # stream is stopped, so the thread keeps every sentence the model wrote.
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_text_chunk("first sentence.")
        # Force the state a throttled chunk and a pending image reference leave.
        renderer._stream_buffer = " second sentence."
        renderer._ref_hold = " see ![shot](/tmp/shot.png)"
        before = len(slack.of("append_stream"))

        await renderer.close()

        appended = "".join(kw["text"] for kw in slack.of("append_stream")[before:])
        assert "second sentence." in appended
        assert "![shot](/tmp/shot.png)" in appended
        # Every word the model wrote, in order, including the trailing word the
        # throttled flush of the first chunk held back: a non-final flush at
        # teardown would re-hold the last word for a flush that never comes.
        whole = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert whole.startswith("first sentence. second sentence.")
        assert renderer._stream_buffer == "" and renderer._ref_hold == ""
        assert renderer._word_hold == ""
        # The flush precedes the stop.
        names = [n for n, _ in slack.calls]
        assert names.index("stop_stream") > max(
            i for i, n in enumerate(names) if n == "append_stream"
        )

    @pytest.mark.asyncio
    async def test_close_without_publish_stops_the_stream_but_says_nothing_new(self) -> None:
        # A turn whose audience fence forbids publication: the open card is NOT
        # completed (that would publish its title for the first time), the
        # throttled chunk and the withheld reference tail are NOT appended, yet
        # the stream is stopped and the status cleared, so the thread reads as
        # ended without saying what the turn was not allowed to say.
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_tool_call("tc1", "Running: shell", tool_kind="execute")
        renderer._stream_buffer = " second sentence."
        renderer._ref_hold = " see ![shot](/tmp/shot.png)"
        before_appends = len(slack.of("append_stream"))
        before_tasks = len(slack.of("append_task"))

        await renderer.close(publish=False)

        assert slack.of("append_stream")[before_appends:] == []
        assert slack.of("append_task")[before_tasks:] == []
        assert [kw["status"] for kw in slack.of("append_task")] == ["in_progress"]
        assert len(slack.of("stop_stream")) == 1
        assert slack.of("set_thread_status")[-1]["status"] == ""
        assert renderer._ref_hold == "" and renderer._stream_ts is None

    @pytest.mark.asyncio
    async def test_close_without_publish_leaves_the_placeholder_as_shown(self) -> None:
        # No-stream fallback under the fence: a placeholder that already shows
        # text is left exactly as it is -- a reply already shown cannot be
        # recalled, and nothing buffered since is rendered into it -- while a
        # placeholder still reading only the Thinking marker is deleted.
        slack = _RecSlack(streaming=False)
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("already shown.")  # opens the placeholder
        renderer._stream_buffer = renderer._stream_buffer + " a buffered tail."
        before = len(slack.calls)

        await renderer.close(publish=False)

        after = [n for n, _ in slack.calls[before:]]
        assert "delete_message" not in after and "update_message" not in after
        assert "post_message" not in after and "post_blocks" not in after
        assert renderer._stream_ts is None

        empty_slack = _RecSlack(streaming=False)
        empty = _renderer(empty_slack)
        await empty.on_turn_start()
        await empty._ensure_stream()  # placeholder posted, nothing rendered yet
        empty._stream_buffer = ""
        await empty.close(publish=False)
        assert len(empty_slack.of("delete_message")) == 1

    @pytest.mark.asyncio
    async def test_close_flush_failure_still_stops_the_stream(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("first")
        renderer._stream_buffer = "tail"

        async def _boom(*a, **k):
            raise RuntimeError("slack down")

        slack.append_stream = _boom  # type: ignore[method-assign]
        await renderer.close()  # must not raise
        # A failed append also trips the renderer's own stream rotation (its own
        # stop), so the property is the outcome: the surface ends closed.
        assert slack.of("stop_stream")
        assert renderer._stream_ts is None
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_close_seals_the_fallback_placeholder_with_the_text_so_far(self) -> None:
        # No streaming surface: the turn renders into a posted placeholder. A
        # crash must not leave it reading "Thinking…" forever -- it is sealed
        # with what the reader already saw.
        slack = _RecSlack(streaming=False)
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("partial answer")
        assert slack.of("post_message"), "the placeholder was posted"

        await renderer.close()

        updates = [kw["text"] for kw in slack.of("update_message")]
        assert updates and "partial answer" in updates[-1]
        assert "Thinking" not in updates[-1]
        assert slack.of("delete_message") == []
        assert renderer._stream_ts is None

    @pytest.mark.asyncio
    async def test_close_deletes_an_empty_fallback_placeholder(self) -> None:
        # The placeholder opens on the first chunk. If the turn dies with nothing
        # readable to seal into it (only thinking tags arrived), it is deleted,
        # the way the native handler disposes of its own thinking placeholder.
        slack = _RecSlack(streaming=False)
        renderer = _renderer(slack)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("<thinking>working it out</thinking>")
        placeholder_ts = renderer._stream_ts
        assert placeholder_ts is not None

        await renderer.close()

        assert [kw["ts"] for kw in slack.of("delete_message")] == [placeholder_ts]
        assert renderer._stream_ts is None

    @pytest.mark.asyncio
    async def test_close_after_on_done_touches_nothing(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack)
        await renderer.on_text_chunk("done")
        await renderer.on_done(stop_reason="end_turn")
        before = list(slack.calls)

        await renderer.close()

        assert slack.calls == before

    @pytest.mark.asyncio
    async def test_no_decider_means_no_approval_card(self) -> None:
        slack = _RecSlack()
        renderer = _renderer(slack, decider=None)

        await renderer.on_prompt_choice(
            [{"id": "allow", "label": "Allow"}], "rq1", tool_title="shell"
        )

        assert slack.of("post_blocks") == []


# ---------------------------------------------------------------------------
# 3. A dashboard-ingress turn on a linked session drives the renderer
# ---------------------------------------------------------------------------


def _harness(tmp_path, uploads_dir, *, events, streaming=True):
    state = _make_state(tmp_path)
    client = MagicMock()
    client.shutdown = AsyncMock()
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    cwd = tmp_path / "project"
    cwd.mkdir(exist_ok=True)
    client.cwd = str(cwd)

    async def _stream(msg):
        for ev in events:
            yield ev

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.is_slack_paused = MagicMock(return_value=False)
    state.sessions.get_session_for_thread = MagicMock(return_value=None)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    state.owner_id = "U_OWNER"
    slack = _RecSlack(streaming=streaming)
    state.slack_client = slack
    slot = state.get_or_create_slot("linked-slot")
    slot.append("user", "hello", "msg msg-u")
    state.link_slack(slot.key, "111.222", "C123")
    return state, slot, slack, client, cwd


def _events(*extra: AcpEvent) -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="first part "),
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc1",
            title="Running: shell",
            tool_name="shell",
            tool_kind="execute",
        ),
        *extra,
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="final answer [OPTIONS: Yes | No]"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]


class TestDashboardIngressDrivesTheRenderer:
    @pytest.mark.asyncio
    async def test_markdown_split_credential_in_tool_title_is_redacted(
        self, tmp_path, uploads_dir
    ) -> None:
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title=f"Running: {SPLIT_KEY}",
                tool_name="shell",
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "run it")

        assert KEY not in slack.displayed_task_cards()
        assert SPLIT_KEY not in str(slack.of("append_task"))

    @pytest.mark.asyncio
    async def test_markdown_split_credential_in_tool_purpose_is_redacted(
        self, tmp_path, uploads_dir
    ) -> None:
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title="Running: shell",
                tool_name="shell",
                tool_purpose=f"Use {SPLIT_KEY}",
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "run it")

        assert KEY not in slack.displayed_task_cards()
        assert SPLIT_KEY not in str(slack.of("append_task"))

    @pytest.mark.asyncio
    async def test_dashboard_mirror_still_bounds_safe_tool_fields(
        self, tmp_path, uploads_dir
    ) -> None:
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title=f"Running: {'x' * 90}",
                tool_name="shell",
                tool_purpose="p" * 90,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "run it")

        card = slack.of("append_task")[0]
        assert card["title"] == "p" * 75
        assert card["details"] == "x" * 66

    @pytest.mark.asyncio
    async def test_a_credential_straddling_the_card_cut_is_redacted_on_the_mirror_leg(
        self, tmp_path, uploads_dir
    ) -> None:
        long_title = "x" * 70 + KEY
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title=long_title,
                tool_name="shell",
                tool_purpose=f"Use {KEY}",
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "run it")

        card = slack.of("append_task")[0]
        rendered = f"{card['title']}\n{card['details']}"
        assert REDACTED_CREDENTIAL_TAG in rendered
        assert KEY[:8] not in rendered
        assert KEY[:5] not in rendered
        assert len(card["title"]) <= 75
        assert len(card["details"]) <= 75

    @pytest.mark.asyncio
    async def test_mirror_and_native_tool_cards_redact_the_same_over_length_title(
        self, tmp_path, uploads_dir
    ) -> None:
        long_field = "x" * 70 + KEY
        native_slack = _RecSlack()
        native_renderer = _renderer(native_slack)
        await native_renderer.dispatch(
            OutputEvent(
                kind=TOOL_CALL,
                tool_call_id="tc1",
                title=long_field,
                tool_name="shell",
                tool_purpose=long_field,
            )
        )
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title=long_field,
                tool_name="shell",
                tool_purpose=long_field,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, mirror_slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "run it")

        native_card = native_slack.of("append_task")[0]
        mirror_card = mirror_slack.of("append_task")[0]
        assert (mirror_card["title"], mirror_card["details"]) == (
            native_card["title"],
            native_card["details"],
        )

    @pytest.mark.asyncio
    async def test_the_mirror_leg_passes_tool_fields_untouched(self, tmp_path, uploads_dir) -> None:
        raw_title = "x" * 100 + KEY
        raw_purpose = "p" * 100 + KEY
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id="tc1",
                title=raw_title,
                tool_name="shell",
                tool_kind="execute",
                tool_purpose=raw_purpose,
            ),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, _slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)

        with patch.object(SlackRenderer, "on_tool_call", new_callable=AsyncMock) as recorded:
            await chat_runner._run_chat(state, slot, "run it")

        recorded.assert_awaited_once_with(
            "tc1",
            raw_title,
            tool_kind="execute",
            tool_purpose=raw_purpose,
        )

    @pytest.mark.asyncio
    async def test_the_thread_gets_echo_status_stream_cards_reply_and_control(
        self, tmp_path, uploads_dir
    ) -> None:
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=_events())
        recorded: list[Any] = []
        with patch.object(
            chat_runner, "remember_slack_options", lambda s, k, p: recorded.append(p)
        ):
            await chat_runner._run_chat(state, slot, "typed in dashboard")

        # The user echo, redaction path and all.
        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _typed in dashboard_"
        # The in-flight indicator is the renderer's thread status.
        assert slack.of("set_thread_status")[0]["status"] == "is working on your request"
        # Streaming with a recipient, so chat.startStream does not demote.
        assert slack.of("start_stream")[0]["user_id"] == "U_OWNER"
        # Per-tool task cards, opened then completed.
        assert [kw["status"] for kw in slack.of("append_task")] == ["in_progress", "complete"]
        assert slack.of("append_task")[0]["title"] == "shell"
        # EVERY text segment streams -- the pre-tool text too, which a final-
        # segment-only mirror would drop -- and the OPTIONS markup never reaches
        # the stream.
        streamed = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert "first part" in streamed and "final answer" in streamed
        assert "[OPTIONS" not in slack.shown()
        assert len(slack.of("stop_stream")) == 1
        # The OPTIONS control rides the footer and its ts is retained.
        assert len(recorded) == 1
        assert recorded[0].choices == ("Yes", "No")
        assert recorded[0].ts
        # The status is cleared at the end.
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_fenced_turn_publishes_nothing_but_still_closes_its_stream(
        self, tmp_path, uploads_dir
    ) -> None:
        # The audience fence (a peer steered this turn and the containment moved
        # since) must hold at EVERY mirror publication, not only the seal: the
        # renderer streams the reply live, so a seal-only fence would let the
        # withheld reply out chunk by chunk. The predicate itself is pinned in
        # test_session_control; here it is forced true and the mirror observed.
        events = [AcpEvent(kind=EVENT_THINKING_CHUNK, text="let me reason"), *_events()]
        state, slot, slack, _client, _cwd = _harness(tmp_path, uploads_dir, events=events)
        recorded: list[Any] = []
        with (
            patch.object(chat_runner, "cross_surface_withheld", lambda s, sl: True),
            patch.object(chat_runner, "remember_slack_options", lambda s, k, p: recorded.append(p)),
        ):
            await chat_runner._run_chat(state, slot, "typed in dashboard")

        # Nothing of the reply reached the thread: no text, no reasoning, no card,
        # no footer, no OPTIONS control -- the stream is opened lazily by the first
        # publication, so a fully fenced turn never even opens one.
        assert slack.of("start_stream") == [] and slack.of("append_stream") == []
        assert slack.of("append_task") == []
        assert "first part" not in slack.shown() and "final answer" not in slack.shown()
        assert not any("💭" in kw["text"] for kw in slack.of("post_message"))
        assert recorded == []
        # The user's own message is still echoed (the fence guards the REPLY, the
        # same scope main gives it), and the surface is closed: status cleared.
        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _typed in dashboard_"
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_a_credential_split_across_chunks_never_reaches_the_stream(
        self, tmp_path, uploads_dir
    ) -> None:
        # Slack renders consecutive appends as one run, so two halves that each
        # pass a per-chunk scan would spell the key whole in the thread. The
        # mirror feeds the renderer only the rolling redactor's confirmed-safe
        # prefix, exactly as the transport driver does for a Slack-born turn.
        head, tail = KEY[:9], KEY[9:]
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"your key is {head}"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"{tail} and that is all"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "show me")

        streamed = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert KEY not in streamed and KEY not in slack.shown()
        assert "and that is all" in streamed

    @pytest.mark.asyncio
    async def test_a_key_split_by_link_markup_never_renders_whole_on_the_mirror_leg(
        self, tmp_path, uploads_dir, monkeypatch
    ) -> None:
        # The raw rolling redactor releases ``[AKIA](url)`` whole -- ``)`` ends
        # its run -- and the next chunk opens with the rest of the key, so each
        # append scans clean while Slack shows the key whole. Every chunk flushes
        # here (throttle zeroed) so the first append would stand on its own.
        monkeypatch.setattr(slack_renderer_module, "_EDIT_INTERVAL", 0.0)
        head, tail = KEY[:4], KEY[4:]
        events = [
            AcpEvent(
                kind=EVENT_TEXT_CHUNK,
                text=f"your key is [{head}](https://example.invalid/a-long-path)",
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"{tail} and that is all"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "show me")

        appended = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert KEY not in canonicalize_display(appended), appended
        assert KEY not in canonicalize_display(slack.shown())
        assert "and that is all" in appended

    @pytest.mark.asyncio
    async def test_a_companion_only_credential_is_redacted_on_the_reply_stream(
        self, tmp_path, uploads_dir, monkeypatch
    ) -> None:
        # The hand-rolled reply leg this PR replaces posted through
        # ``render_for_slack``, whose default redactor is ``redact_via_context``:
        # on a composed host the companion's extra credential patterns apply.
        # The streamed leg must keep that layer. A token only the context
        # redactor knows -- the baseline ``security.redact`` leaves it alone --
        # is split across two chunks and must not reach the thread whole.
        secret = "CMPSECRET-0123456789"

        def _context_redactor(text: str) -> str:
            return text.replace(secret, "[REDACTED_COMPANION]")

        monkeypatch.setattr(chat_runner, "redact_via_context", _context_redactor)
        head, tail = secret[:7], secret[7:]
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"token {head}"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"{tail} is yours"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)

        await chat_runner._run_chat(state, slot, "show me")

        streamed = "".join(kw["text"] for kw in slack.of("append_stream"))
        assert secret not in streamed and secret not in slack.shown()
        assert "[REDACTED_COMPANION]" in streamed
        assert "is yours" in streamed

    @pytest.mark.asyncio
    async def test_a_companion_only_credential_is_redacted_in_the_posted_reasoning(
        self, tmp_path, uploads_dir, monkeypatch
    ) -> None:
        # The renderer accumulates reasoning and redacts the whole with its
        # baseline floor at post time, so the context layer has to be applied on
        # the way in -- through a rolling redactor, because per-chunk redaction
        # would miss a credential split across two thinking chunks.
        secret = "CMPSECRET-0123456789"

        def _context_redactor(text: str) -> str:
            return text.replace(secret, "[REDACTED_COMPANION]")

        monkeypatch.setattr(chat_runner, "redact_via_context", _context_redactor)
        head, tail = secret[:7], secret[7:]
        events = [
            AcpEvent(kind=EVENT_THINKING_CHUNK, text=f"the token is {head}"),
            AcpEvent(kind=EVENT_THINKING_CHUNK, text=f"{tail}, use it"),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)
        cfg = KiroCrewConfig.load()
        cfg.slack.show_thinking = True
        with patch("kiro_crew.dashboard.slack_mirror.KiroCrewConfig.load", return_value=cfg):
            await chat_runner._run_chat(state, slot, "show me")

        reasoning = [kw["text"] for kw in slack.of("post_message") if "💭" in kw["text"]]
        assert reasoning, "reasoning was not posted"
        assert secret not in "".join(reasoning) and secret not in slack.shown()
        assert "[REDACTED_COMPANION]" in "".join(reasoning)
        assert "use it" in "".join(reasoning)

    @pytest.mark.asyncio
    async def test_a_synthetic_turn_is_not_echoed_but_still_answered(
        self, tmp_path, uploads_dir
    ) -> None:
        from kiro_crew.dashboard.chat_utils import _CONN_RECOVER_MSG

        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, _CONN_RECOVER_MSG, _synthetic_payload=True)

        assert not any(USER_ECHO_PREFIX in kw["text"] for kw in slack.of("post_message"))
        assert "final answer" in slack.shown()

    @pytest.mark.asyncio
    async def test_a_slash_turn_reaches_slack_not_at_all(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, "/context")

        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_a_slack_born_message_is_not_echoed_back(self, tmp_path, uploads_dir) -> None:
        # The message arrived from the thread (its files with it), so the mirror
        # must not repeat it there; the answer still goes to the thread.
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_from_slack.png"
        pic.write_bytes(PNG)
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(
            state,
            slot,
            f"from slack\n![image]({pic})",
            _directive_user_origin=True,
            _ingress="slack",
        )

        assert not any(USER_ECHO_PREFIX in kw["text"] for kw in slack.of("post_message"))
        assert slack.uploads == [], "a Slack-born picture must not be re-uploaded to Slack"
        assert "final answer" in slack.shown()

    @pytest.mark.asyncio
    async def test_a_dashboard_pasted_picture_travels_with_the_echo(
        self, tmp_path, uploads_dir
    ) -> None:
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_pasted.png"
        pic.write_bytes(PNG)
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())

        await chat_runner._run_chat(state, slot, f"why red?\n![image]({pic})")

        assert slack.of("post_message")[0]["text"] == f"{USER_ECHO_PREFIX} _why red?_"
        assert [u["data"] for u in slack.uploads] == [PNG]

    @pytest.mark.asyncio
    async def test_an_agent_drawn_chart_travels_with_the_reply(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, cwd = _harness(tmp_path, uploads_dir, events=[])
        chart = cwd / "chart.png"
        chart.write_bytes(PNG)
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text=f"here:\n\n![chart]({chart})\n"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

        async def _stream(msg):
            for ev in events:
                yield ev

        client = state.sessions.get_or_create.return_value[0]
        client.stream = _stream

        await chat_runner._run_chat(state, slot, "draw it")

        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(chart) not in slack.shown()

    @pytest.mark.asyncio
    async def test_thinking_follows_slack_show_thinking(self, tmp_path, uploads_dir) -> None:
        # Reasoning lands ABOVE the answer: the renderer posts it once, when the
        # first answer text arrives, so it has to precede that text.
        events = [AcpEvent(kind=EVENT_THINKING_CHUNK, text="let me reason"), *_events()]
        for enabled in (True, False):
            state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)
            # Flip the one flag the mirror reads on a REAL loaded config: the turn
            # loop also resolves agent bindings through `KiroCrewConfig.load`, and a
            # bare MagicMock there starves that resolution and fails the turn
            # before any thinking is posted.
            cfg = KiroCrewConfig.load()
            cfg.slack.show_thinking = enabled
            with patch("kiro_crew.dashboard.slack_mirror.KiroCrewConfig.load", return_value=cfg):
                await chat_runner._run_chat(state, slot, "think")
            posted = any("💭" in kw["text"] for kw in slack.of("post_message"))
            assert posted is enabled, f"show_thinking={enabled}: thinking posted={posted}"

    @pytest.mark.asyncio
    async def test_a_paused_mirror_silences_everything(self, tmp_path, uploads_dir) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=_events())
        state.sessions.is_slack_paused = MagicMock(return_value=True)

        await chat_runner._run_chat(state, slot, "typed in dashboard")

        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_a_crash_mid_tool_still_tears_the_stream_down(
        self, tmp_path, uploads_dir
    ) -> None:
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=[])

        async def _stream(msg):
            yield AcpEvent(
                kind=EVENT_TOOL_CALL, tool_call_id="tc1", title="Running: shell", tool_name="shell"
            )
            raise RuntimeError("provider died")

        client = state.sessions.get_or_create.return_value[0]
        client.stream = _stream

        await chat_runner._run_chat(state, slot, "typed in dashboard")

        assert [kw["status"] for kw in slack.of("append_task")] == ["in_progress", "complete"]
        assert len(slack.of("stop_stream")) == 1
        assert slack.of("set_thread_status")[-1]["status"] == ""

    @pytest.mark.asyncio
    async def test_the_approval_prompt_is_posted_once_through_the_dashboards_own_path(
        self, tmp_path, uploads_dir
    ) -> None:
        events = [
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title="shell",
                options=[{"id": "allow", "label": "Allow"}],
            ),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        state, slot, slack, _c, _d = _harness(tmp_path, uploads_dir, events=events)
        # Delivery "fails" so the dashboard auto-declines and the turn does not
        # park on the approval window; what matters here is WHO posts the prompt.
        with patch.object(
            chat_runner, "post_linked_approval", new_callable=AsyncMock, return_value=None
        ) as linked:
            await chat_runner._run_chat(state, slot, "run it")

        linked.assert_awaited_once()
        assert not any(
            kw["text"] == "Tool approval requested" for kw in slack.of("post_blocks")
        ), "the renderer must not post an approval card of its own"


class TestTheHandRolledPathIsGone:
    def test_chat_runner_no_longer_posts_to_slack_by_hand(self) -> None:
        src = inspect.getsource(chat_runner)
        for gone in (
            'initial_text="Thinking…"',
            "render_for_slack(",
            "build_options_blocks(",
            "state.slack_client.start_stream(",
            "state.slack_client.append_task(",
            "state.slack_client.stop_stream(",
            "_mirror_stream_ts",
        ):
            assert gone not in src, f"hand-rolled Slack call still present: {gone}"
        # The renderer is the one Slack output.
        assert "open_slack_mirror(" in src
        assert "_mirror.on_text_chunk(" in src
        assert "_mirror.on_tool_call(" in src
        assert "_mirror.on_done(" in src
        assert "_mirror.close()" in src

    def test_the_backfill_no_longer_formats_rows_by_hand(self) -> None:
        src = inspect.getsource(chat_slack)
        assert "_format_backfill_parts" not in src
        assert "post_history_row(" in src


# ---------------------------------------------------------------------------
# 4. Inbound: a Slack picture lands where the dashboard renders from
# ---------------------------------------------------------------------------


class TestInboundSlackImagePersists:
    @pytest.mark.asyncio
    async def test_the_image_is_promoted_into_uploads_with_the_dashboards_naming(
        self, uploads_dir
    ) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="Screenshot 2026-09-12 at 09.38.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert len(result.image_paths) == 1
        path = Path(result.image_paths[0])
        assert path.parent == uploads_dir
        assert path.exists() and path.read_bytes() == PNG
        # <uuid hex>_<sanitized name>: 32 hex chars, an underscore, the name with
        # every unsafe character replaced.
        prefix, _, name = path.name.partition("_")
        assert len(prefix) == 32 and all(c in "0123456789abcdef" for c in prefix)
        assert name == "Screenshot_2026-09-12_at_09.38.png"
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # Persisted: the caller must NOT delete it.
        assert result.persisted_paths == [str(path)]
        assert result.temp_paths == []

    @pytest.mark.asyncio
    async def test_the_suffix_follows_the_sniffed_type(self, uploads_dir) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16

        async def _download(url, dest):
            Path(dest).write_bytes(jpeg)

        result = await ingest_attachments(
            [Attachment(name="photo.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert Path(result.image_paths[0]).name.endswith("_photo.jpg")

    @pytest.mark.asyncio
    async def test_the_row_carries_the_dashboards_markdown_form(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="shot.png", mimetype="image/png", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        row = append_attachment_context("look at this", result)

        # The destination is the composer's spelling: on Windows that is the
        # forward-slash form, which is why the raw path is not compared here.
        expected_dest = uploads.markdown_image_dest(result.image_paths[0])
        assert row == f"look at this\n![image]({expected_dest})"

    @pytest.mark.asyncio
    async def test_the_prompt_encoder_still_finds_the_path_inside_the_markdown(
        self, uploads_dir
    ) -> None:
        from kiro_crew.acp.prompt_blocks import build_prompt_blocks

        pil_image = pytest.importorskip("PIL.Image")
        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pil_image.new("RGB", (2, 2), (255, 0, 0)).save(pic, format="PNG")
        row = append_attachment_context(
            "look", IngestResult(image_paths=[str(pic)], persisted_paths=[str(pic)])
        )

        blocks = build_prompt_blocks(row, max_image_bytes=1 << 20)

        assert any(b.get("type") == "image" for b in blocks), blocks

    @pytest.mark.asyncio
    async def test_an_opaque_file_keeps_the_temp_behaviour(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(b"PK\x03\x04zip-bytes")

        result = await ingest_attachments(
            [Attachment(name="bundle.zip", mimetype="application/zip", url="u")],
            download=_download,
            source="slack",
            persist_images=True,
        )

        assert len(result.file_paths) == 1
        assert not Path(result.file_paths[0]).is_relative_to(uploads_dir)
        assert result.persisted_paths == []
        assert result.temp_paths == result.file_paths
        assert result.file_paths[0] in append_attachment_context("", result)
        os.unlink(result.file_paths[0])

    @pytest.mark.asyncio
    async def test_without_the_flag_images_stay_temp(self, uploads_dir) -> None:
        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        result = await ingest_attachments(
            [Attachment(name="shot.png", mimetype="image/png", url="u")],
            download=_download,
            source="discord",
        )

        assert not Path(result.image_paths[0]).is_relative_to(uploads_dir)
        assert result.temp_paths == result.image_paths
        assert append_attachment_context("", result) == result.image_paths[0]
        os.unlink(result.image_paths[0])

    @pytest.mark.asyncio
    async def test_process_slack_files_opts_into_persistence(self, uploads_dir) -> None:
        from kiro_crew.slack.files import process_slack_files

        orch = MagicMock()
        orch.slack = AsyncMock()

        async def _download(url, dest):
            Path(dest).write_bytes(PNG)

        orch.slack.download_file = AsyncMock(side_effect=_download)

        result = await process_slack_files(
            orch,
            [
                {
                    "mimetype": "image/png",
                    "url_private_download": "u",
                    "filetype": "png",
                    "name": "a.png",
                }
            ],
            persist_images=True,
        )

        assert isinstance(result, IngestResult)
        assert result.persisted_paths == result.image_paths
        assert Path(result.image_paths[0]).parent == uploads_dir


# ---------------------------------------------------------------------------
# 5. The link-time backfill uploads pictures
# ---------------------------------------------------------------------------


class TestBackfillUploadsImages:
    @pytest.mark.asyncio
    async def test_a_history_row_with_a_picture_seeds_the_picture(
        self, tmp_path, uploads_dir, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_backfill import BackfillSelection

        uploads_dir.mkdir()
        pic = uploads_dir / "0123abcd_shot.png"
        pic.write_bytes(PNG)
        state = _make_state(tmp_path)
        state.owner_id = "U_OWNER"
        slack = _RecSlack()
        state.slack_client = slack
        slot = state.get_or_create_slot("s1")
        slot.project = str(tmp_path)
        rows = [
            {"role": "user", "content": f"this one\n![image]({pic})"},
            {"role": "assistant", "content": "I see a red button."},
        ]
        slot.messages.extend(rows)
        monkeypatch.setattr(
            chat_slack,
            "select_backfill_messages",
            lambda _s, _sl: BackfillSelection(first_turn=[], recent=[rows], skipped_turns=0),
        )
        state.link_slack(slot.key, "thread-1", "C-1")

        await chat_slack.drain_slack_backfill(state, slot, "C-1", "thread-1")

        texts = [kw["text"] for kw in slack.of("post_message")]
        assert texts == [
            f"{HISTORY_USER_ICON} this one",
            f"{HISTORY_AGENT_ICON} I see a red button.",
        ]
        assert [u["data"] for u in slack.uploads] == [PNG]
        assert str(pic) not in slack.shown()

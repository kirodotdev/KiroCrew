"""``file_send`` into the caller's own Slack code channel.

A code channel the crew opened is neither a DM nor a tracked channel. The
session-map rung of the Slack leg admits it only as the live, unarchived code
channel bound to the uploading session, top-level as its mirrored replies are. A code channel can be
Slack Connect, so that rung takes only the session's own files: the original file
(``source_path``, or the uploaded file itself) must lie in the session's work dir,
outside the outbox, Crew's memory and every dot-named entry, and hold the uploaded
bytes. These tests pin both directions through the real endpoint, the real binding
store and the real code-channel write fences.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from slack_code_channels_helpers import Env, make_env

import kiro_crew.hooks as hooks
from kiro_crew.dashboard import upload_destination
from kiro_crew.dashboard.handlers import files
from kiro_crew.dashboard.handlers.files import api_slack_upload_file
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.testing.links import make_dir_link

CHANNEL = "C0CODECHAN"
ANCHOR = "1700000000.000100"
SLOT_KEY = "chat-cc-1"
SESSION = f"dashboard:{SLOT_KEY}"
WORKER = "dashboard:chat-worker-1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Env]:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    gen = make_env(monkeypatch, tmp_path)
    e = next(gen)
    e.store.load()
    e.store.put_binding(
        Binding(channel_id=CHANNEL, team_id="T1", session_key=SLOT_KEY, anchor_ts=ANCHOR)
    )
    yield e
    next(gen, None)


class Crew:
    """A gateway with one code channel whose session runs in *workdir*."""

    def __init__(self, tmp_path: Path, workdir: Path, *, workspace: Path | None = None) -> None:
        self.workspace = workspace or tmp_path / "ws"
        self.outbox = self.workspace / "outbox"
        self.memory = self.workspace / "memory"
        for d in (self.outbox, self.memory, workdir):
            d.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.slack = MagicMock()
        self.slack.upload_file = AsyncMock()
        self.slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        self.slot = SimpleNamespace(key=SLOT_KEY, linked_session_key="", project=str(workdir))
        self.links: dict[str, tuple[str, str]] = {SESSION: (ANCHOR, CHANNEL)}
        #: The transport attests the declared key (``file_send`` sends the signed
        #: per-session token); ``None`` leaves the real check on an unsigned request.
        self.attested: bool | None = True
        state = MagicMock()
        state.slack_client = self.slack
        state.get_slot.return_value.is_restricted = False
        state.get_linked_slot.side_effect = lambda ts: self.slot if ts == ANCHOR else None
        state.sessions.get_slack_link.side_effect = lambda key: self.links.get(key, (None, None))
        self.state = state

    def outbox_copy(self, data: bytes = PNG, name: str = "shot.png") -> Path:
        copy = self.outbox / name
        copy.write_bytes(data)
        return copy

    async def upload(
        self,
        source: Path | str | None,
        *,
        copy: Path | None = None,
        session: str = SESSION,
        channel: str = "",
    ) -> tuple[int, dict[str, Any]]:
        if copy is None:
            copy = self.outbox_copy()
        body: dict[str, Any] = {"file_path": str(copy), "filename": copy.name, "channel": channel}
        if isinstance(source, list):
            body["source_path"] = source  # a malformed body, as a direct caller may send
        elif source is not None:
            body["source_path"] = str(source)
        app = web.Application()
        app["state"] = self.state
        app.router.add_post("/api/slack/upload-file", api_slack_upload_file)
        real_attest = _real_attestation()
        attested = self.attested
        with (
            patch(
                "kiro_crew.member_memory_auth.session_key_is_attested",
                side_effect=lambda req, key: (
                    real_attest(req, key) if attested is None else attested and key == session
                ),
            ),
            patch("kiro_crew.config.loader.outbox_dir", return_value=self.outbox),
            patch("kiro_crew.config.loader.workspace_root", return_value=self.workspace),
            patch("kiro_crew.memory.memory_dir", return_value=self.memory),
            patch("kiro_crew.dashboard.handlers.files.is_tracked_channel", return_value=False),
            patch("kiro_crew.config.loader.KiroCrewConfig.load") as cfg,
        ):
            cfg.return_value.load_credentials.return_value = {"KIROCREW_OWNER_ID": "U_OWNER"}
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file", json=body, headers={"X-Session-Key": session}
                )
                return resp.status, await resp.json()

    def png(self, rel: str, data: bytes = PNG) -> Path:
        path = self.workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


def _real_attestation() -> Any:
    from kiro_crew.member_memory_auth import session_key_is_attested

    return session_key_is_attested


def _refused_outside(status: int, body: dict[str, Any]) -> bool:
    return status == 403 and body.get("code") == "outside_session_workdir"


@pytest.fixture
def crew(env: Env, tmp_path: Path) -> Crew:
    return Crew(tmp_path, tmp_path / "projects" / "todo-app")


# ── The fix: the session's own file reaches its own code channel ─────────────


@pytest.mark.asyncio
async def test_a_png_from_the_session_cwd_posts_top_level_into_its_code_channel(crew: Crew):
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body) == (200, {"ok": True})
    crew.slack.upload_file.assert_called_once()
    channel, thread_ts = crew.slack.upload_file.call_args[0][:2]
    # Top-level, as the channel's mirrored replies are, never into the anchor's thread.
    assert (channel, thread_ts) == (CHANNEL, "")


@pytest.mark.asyncio
async def test_the_checked_bytes_are_what_posts_not_a_rewritten_outbox_copy(crew: Crew):
    # The outbox is writable by any session: a copy rewritten after the checks
    # read it must not be what reaches the channel.
    shot = crew.png("shot.png")
    copy = crew.outbox_copy()
    real = upload_destination.code_channel_source_refusal

    def rewrite_meanwhile(*args: Any) -> Any:
        verdict = real(*args)
        copy.write_bytes(b"\x89PNG\r\n\x1a\nswapped")
        return verdict

    with patch.object(upload_destination, "code_channel_source_refusal", rewrite_meanwhile):
        status, _ = await crew.upload(shot, copy=copy)
    assert status == 200
    assert crew.slack.upload_file.call_args[0][2] == PNG


# ── The roots the channel's mirrored pictures come from ──────────────────────


@pytest.mark.asyncio
async def test_a_browser_screenshot_posts_like_an_inline_image_would(crew: Crew, tmp_path: Path):
    snaps = tmp_path / "home" / "browser-snapshots"
    snaps.mkdir(parents=True)
    shot = snaps / "page.png"
    shot.write_bytes(PNG)
    with patch("kiro_crew.messaging.outbound_files.snapshot_dir", return_value=snaps):
        status, _ = await crew.upload(shot)
        hidden = snaps / ".cache" / "page.png"
        hidden.parent.mkdir()
        hidden.write_bytes(PNG)
        refused = await crew.upload(hidden)
    assert status == 200
    assert crew.slack.upload_file.call_args[0][:2] == (CHANNEL, "")
    # The dot-path refusal holds under every approved root, not only the cwd.
    assert _refused_outside(*refused)
    assert crew.slack.upload_file.call_count == 1


@pytest.mark.asyncio
async def test_the_sessions_own_scratch_posts_and_another_allocation_does_not(
    crew: Crew, tmp_path: Path
):
    managed = tmp_path / "scratch"
    mine, theirs = managed / "mine", managed / "theirs"
    for d in (mine, theirs):
        d.mkdir(parents=True)
        (d / "shot.png").write_bytes(PNG)
    crew.state.sessions.get_provider.return_value = SimpleNamespace(
        cwd=str(crew.workdir), work_scratch_dir=str(mine)
    )
    with patch("kiro_crew.messaging.outbound_files.managed_scratch_root", return_value=managed):
        status, _ = await crew.upload(mine / "shot.png")
        refused = await crew.upload(theirs / "shot.png")
    assert status == 200
    assert _refused_outside(*refused)
    assert crew.slack.upload_file.call_count == 1


# ── Still refused: the file is not the session's own ─────────────────────────


@pytest.mark.asyncio
async def test_a_file_outside_the_cwd_is_refused(crew: Crew, tmp_path: Path):
    other = tmp_path / "projects" / "other-app" / "shot.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(PNG)
    assert _refused_outside(*await crew.upload(other))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_dotdot_path_out_of_the_cwd_is_refused(crew: Crew, tmp_path: Path):
    other = tmp_path / "projects" / "other-app" / "shot.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(PNG)
    sneaky = f"{crew.workdir}{os.sep}..{os.sep}other-app{os.sep}shot.png"
    assert _refused_outside(*await crew.upload(sneaky))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_linked_directory_in_the_cwd_that_escapes_it_is_refused(crew: Crew, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG)
    make_dir_link(crew.workdir / "assets", outside)
    assert _refused_outside(*await crew.upload(crew.workdir / "assets" / "secret.png"))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_the_source_meets_the_shared_path_screen_before_anything_resolves_it(crew: Crew):
    # The shared screen (UNC shares, ``~``, linked ancestors) is what judges the raw
    # spelling; a refusal there ends the check before any realpath can touch it.
    shot = crew.png("shot.png")
    real_screen = hooks.validate_file_path
    seen: list[str] = []

    def screen(raw: str, **kw: Any) -> str | None:
        if raw != str(shot):
            return real_screen(raw, **kw)  # the admission gate's own read of the copy
        seen.append(raw)
        return None

    sel = MagicMock()
    with (
        patch("kiro_crew.dashboard.handlers.files._sel", return_value=sel),
        patch("kiro_crew.hooks.validate_file_path", side_effect=screen),
    ):
        assert _refused_outside(*await crew.upload(shot))
    assert seen == [str(shot)]
    errors = [c.kwargs.get("error") for c in sel.log_tool_invocation.call_args_list]
    assert "code_channel_source_refused: path_screen_refused" in errors
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_change_to_the_channel_during_the_source_read_is_honoured(env: Env, crew: Crew):
    # The channel is archived while the source is being read: the probe is asked
    # again after that await, so the file does not post.
    real = upload_destination.code_channel_source_refusal

    def archive_meanwhile(*args: Any) -> Any:
        env.store.update_binding(CHANNEL, archived_at=time.time())
        return real(*args)

    with patch.object(upload_destination, "code_channel_source_refusal", archive_meanwhile):
        status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_path_the_shared_sensitive_gate_flags_is_refused(crew: Crew):
    # The credential and trust-root gate every file surface uses has the last word.
    flagged = crew.png("keys/flagged.png")
    with patch(
        "kiro_crew.security.is_sensitive_canonical_path",
        side_effect=lambda p: str(p) == str(flagged.resolve()),
    ):
        assert _refused_outside(*await crew.upload(flagged))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_an_outbox_copy_alone_never_qualifies(crew: Crew):
    assert _refused_outside(*await crew.upload(None))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_cwd_file_with_other_bytes_does_not_vouch_for_the_copy(crew: Crew):
    decoy = crew.png("shot.png", data=PNG + b"different")
    assert _refused_outside(*await crew.upload(decoy))
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_non_string_source_path_is_refused(base_crew: Crew):
    # Not read as "no source given": a malformed claim is refused, even when the
    # uploaded file itself would have qualified.
    shot = base_crew.png("todo-app/shot.png")
    assert _refused_outside(*await base_crew.upload(["x"], copy=shot))  # type: ignore[arg-type]
    base_crew.slack.upload_file.assert_not_called()


# ── The base workspace folder as the cwd (a zero-config code channel) ────────


@pytest.fixture
def base_crew(env: Env, tmp_path: Path) -> Crew:
    workspace = tmp_path / "ws"
    return Crew(tmp_path, workspace, workspace=workspace)


@pytest.mark.asyncio
async def test_the_base_workspace_cwd_posts_a_project_file(base_crew: Crew):
    status, _ = await base_crew.upload(base_crew.png("todo-app/shot.png"))
    assert status == 200
    assert base_crew.slack.upload_file.call_args[0][:2] == (CHANNEL, "")


@pytest.mark.asyncio
async def test_a_cwd_file_uploaded_directly_needs_no_source_path(base_crew: Crew):
    # A direct caller may upload a work-dir file itself; it is then its own source.
    shot = base_crew.png("todo-app/shot.png")
    status, _ = await base_crew.upload(None, copy=shot)
    assert status == 200
    assert base_crew.slack.upload_file.call_args[0][:2] == (CHANNEL, "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rel",
    [
        "memory/history/shot.png",  # Crew's own memory
        ".hidden.png",  # a dotfile
        ".scratch/run/shot.png",  # a dot-dir (scratch, .kiro, .git...)
        "todo-app/.git/shot.png",  # a dot-dir deeper down
    ],
)
async def test_the_base_workspace_cwd_still_refuses_crew_internal_files(base_crew: Crew, rel):
    path = base_crew.workdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    assert _refused_outside(*await base_crew.upload(path))
    base_crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_the_base_workspace_cwd_refuses_an_outbox_copy_as_its_own_source(base_crew: Crew):
    # The outbox sits inside this cwd, but a copy there may have come from anywhere.
    copy = base_crew.outbox_copy()
    assert _refused_outside(*await base_crew.upload(copy, copy=copy))
    base_crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_home_folder_cwd_is_too_broad(env: Env, tmp_path: Path, monkeypatch):
    home = tmp_path / "userhome"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    c = Crew(tmp_path, home)
    assert _refused_outside(*await c.upload(c.png("Pictures/shot.png")))
    c.slack.upload_file.assert_not_called()


# ── Still refused: not this session's live code channel ──────────────────────


@pytest.mark.asyncio
async def test_another_session_linked_to_the_same_channel_is_refused(crew: Crew):
    # A worker (or any other session) whose link names the channel's anchor.
    crew.links[WORKER] = (ANCHOR, CHANNEL)
    status, body = await crew.upload(crew.png("shot.png"), session=WORKER)
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_session_linked_to_another_thread_of_the_channel_is_refused(crew: Crew):
    crew.links[SESSION] = ("1700000000.999999", CHANNEL)
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_an_archived_code_channel_is_refused(env: Env, crew: Crew):
    assert env.store.update_binding(CHANNEL, archived_at=time.time()) is not None
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_an_unbound_channel_is_refused(crew: Crew):
    crew.links[SESSION] = (ANCHOR, "C0NOTBOUND")
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_code_channels_switched_off_refuse(env: Env, crew: Crew):
    env.on = False
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_slot_that_only_claims_the_session_is_refused(crew: Crew):
    # The anchor resolves to a slot that is not the binding's, though a linked key
    # read back from its transcript names the caller's session: not the channel's.
    crew.slot = SimpleNamespace(
        key="chat-other", linked_session_key=SESSION, project=str(crew.workdir)
    )
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_naming_the_code_channel_in_the_request_is_still_refused(crew: Crew):
    status, body = await crew.upload(crew.png("shot.png"), channel=CHANNEL)
    assert (status, body.get("code"), body) == (403, "channel_not_tracked", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_declared_session_the_transport_does_not_attest_is_refused(crew: Crew):
    # The internal secret alone lets a local caller name any session's key.
    crew.attested = False
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_an_unsigned_request_naming_the_session_is_refused_by_the_real_check(crew: Crew):
    crew.attested = None  # no peer attestation, no X-Session-Token
    status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()


# ── The ceilings stay first ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_restricted_session_is_refused_before_the_code_channel_is_consulted(crew: Crew):
    crew.state.get_slot.return_value.is_restricted = True
    with patch.object(files, "code_channel_workdir", side_effect=AssertionError("consulted")):
        status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "restricted_session", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_governance_denial_is_refused_before_the_code_channel_is_consulted(crew: Crew):
    with (
        patch.object(files, "code_channel_workdir", side_effect=AssertionError("consulted")),
        patch("kiro_crew.dashboard.upload_destination._slack_egress_permitted", return_value=False),
    ):
        status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channels_governance_denied", body)
    crew.slack.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_a_raising_code_channel_probe_denies(crew: Crew):
    with patch.object(files, "code_channel_workdir", side_effect=RuntimeError("store")):
        status, body = await crew.upload(crew.png("shot.png"))
    assert (status, body.get("code"), body) == (403, "channel_not_authorized", body)
    crew.slack.upload_file.assert_not_called()

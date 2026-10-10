"""Tests for POST /api/reveal — local gate and action routing."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import external_editor
from kiro_crew.dashboard.handlers.files import api_reveal_path


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/api/reveal", api_reveal_path)
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


@pytest.mark.asyncio
async def test_reveal_path_no_crash(mock_sel, tmp_path):
    """Given a valid file path, when POST /api/reveal is called with action="reveal",
    then response status is 200 (not 500 TypeError)."""
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=True
    ), patch("kiro_crew.dashboard.handlers.files.platform_compat."
             "reveal_in_file_manager", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "reveal"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="success",
                resources=str(f),
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_path_sensitive_denied(mock_sel):
    """Given a path containing ~/.ssh/id_rsa, when POST /api/reveal is called,
    then response is 403 with {"error": "access denied"} and SEL logs the denial."""
    with patch(
        "kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": "/home/user/.ssh/id_rsa", "action": "reveal"},
            )
            assert resp.status == 403
            body = await resp.json()
            assert body == {"error": "access denied"}
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="denied",
                error="sensitive_path",
                resources="/home/user/.ssh/id_rsa",
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_path_traversal_rejected(mock_sel):
    """Given a path containing '..', when POST /api/reveal is called,
    then response is 400 with {"error": "invalid path"}."""
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/reveal",
            json={"path": "/tmp/../etc/passwd", "action": "reveal"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body == {"error": "invalid path"}
        mock_sel.log_tool_invocation.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_remote_request_returns_copy_fallback(mock_sel, tmp_path):
    """A remote (non-direct-local) reveal request returns the copy fallback
    without spawning a subprocess."""
    f = tmp_path / "readme.md"
    f.write_text("hello")
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=False
    ), patch("subprocess.Popen") as mock_popen:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "reveal"},
            )
            assert resp.status == 200
            body = await resp.json()
            # The degrade response carries `copy` (the path to write); the
            # remote cause lives in the SEL audit record below, not the wire body.
            assert body == {"ok": True, "copy": str(f)}
            mock_popen.assert_not_called()
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="denied",
                error="remote_request",
                resources=str(f),
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_reveal_remote_request_open_action_returns_copy(mock_sel, tmp_path):
    """A remote request with action=open also returns the copy fallback."""
    f = tmp_path / "doc.txt"
    f.write_text("data")
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=False
    ), patch("subprocess.Popen") as mock_popen:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "open"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "copy": str(f)}
            mock_popen.assert_not_called()


@pytest.mark.asyncio
async def test_reveal_local_headless_returns_no_desktop_copy(mock_sel, tmp_path):
    """A direct-local reveal on a host whose file manager could not be
    launched degrades to the clipboard: the response carries `copy`, the path
    to write, and the grant is still SUCCESS in the audit log."""
    f = tmp_path / "notes.md"
    f.write_text("local but headless")
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=True
    ), patch("kiro_crew.dashboard.handlers.files.platform_compat."
             "reveal_in_file_manager", return_value=False):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "reveal"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True, "copy": str(f)}
            # A granted decision whose host had no launcher is still SUCCESS in
            # the audit log — the copy is what happened, not a denial.
            mock_sel.log_tool_invocation.assert_called_with(
                session_key="api",
                source="api",
                tool_name="reveal_path",
                outcome="success",
                resources=str(f),
                metadata={"action": "reveal"},
            )


@pytest.mark.asyncio
async def test_open_action_spawns_opener_for_file(mock_sel, tmp_path):
    """A direct-local open action on a regular file spawns the opener."""
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=True
    ), patch("kiro_crew.dashboard.handlers.files.platform_compat."
             "open_with_default_app", return_value=True):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(f), "action": "open"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}


@pytest.mark.asyncio
async def test_open_action_on_directory_returns_400(mock_sel, tmp_path):
    """action=open on a directory returns 400 'not a regular file'."""
    d = tmp_path / "subdir"
    d.mkdir()
    with patch(
        "kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=True
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": str(d), "action": "open"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body == {"error": "not a regular file"}


# ── action=editor ────────────────────────────────────────────────────────────
#
# The one action that is deliberately NOT behind the direct-local gate, because
# the editor it reaches is attached to this host over Remote-SSH and paints its
# window on the caller's own machine. Every case below therefore asserts against
# the shared path validation and the shared sensitive-path refusal -- the endpoint
# widens which ORIGIN may spawn an editor, never which paths anyone may reach.


@pytest.mark.asyncio
async def test_editor_action_opens_the_path_and_audits_the_action(mock_sel, tmp_path):
    """The happy path: 200, and the audit record names the editor action."""
    f = tmp_path / "main.py"
    f.write_text("print('hi')")
    with (
        patch(
            "kiro_crew.external_editor.open_path",
            return_value="/tmp/vscode-ipc-zzq.sock",
        ) as open_path,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/reveal", json={"path": str(f), "action": "editor"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True}
    assert open_path.call_args.args[0] == str(f)
    mock_sel.log_tool_invocation.assert_called_with(
        session_key="api",
        source="api",
        tool_name="reveal_path",
        outcome="success",
        resources=str(f),
        metadata={"action": "editor"},
    )


@pytest.mark.asyncio
async def test_editor_action_serves_a_tunneled_caller(mock_sel, tmp_path):
    """The reason the action exists: a remote caller is who it serves.

    A reveal degrades to a clipboard copy here, because it would paint a window
    on a machine the caller is not sitting at. The editor inverts that, so the
    same origin must get a real open rather than the degrade.
    """
    f = tmp_path / "main.py"
    f.write_text("print('hi')")
    with (
        patch("kiro_crew.dashboard.handlers.files.is_direct_local_request", return_value=False),
        patch(
            "kiro_crew.external_editor.open_path", return_value=""
        ) as open_path,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/reveal", json={"path": str(f), "action": "editor"})
            assert resp.status == 200
            body = await resp.json()
    # Not the `copy` degrade the reveal action returns to this same caller.
    assert body == {"ok": True}
    open_path.assert_called_once()


@pytest.mark.asyncio
async def test_editor_action_sensitive_path_denied_before_any_spawn(mock_sel):
    """The editor action rides the endpoint's EXISTING refusal, not a second check."""
    with (
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True),
        patch("kiro_crew.external_editor.open_path") as open_path,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal",
                json={"path": "/home/user/.ssh/id_rsa", "action": "editor"},
            )
            assert resp.status == 403
            assert await resp.json() == {"error": "access denied"}
    open_path.assert_not_called()
    mock_sel.log_tool_invocation.assert_called_with(
        session_key="api",
        source="api",
        tool_name="reveal_path",
        outcome="denied",
        error="sensitive_path",
        resources="/home/user/.ssh/id_rsa",
        metadata={"action": "editor"},
    )


@pytest.mark.asyncio
async def test_editor_action_traversal_rejected_before_any_spawn(mock_sel):
    with patch("kiro_crew.external_editor.open_path") as open_path:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal", json={"path": "/tmp/../etc/passwd", "action": "editor"}
            )
            assert resp.status == 400
            assert await resp.json() == {"error": "invalid path"}
    open_path.assert_not_called()


@pytest.mark.asyncio
async def test_editor_action_no_attached_window_is_503(mock_sel, tmp_path):
    """503, not 500: nothing is wrong with the request, and the state self-heals
    the moment the user connects a window."""
    f = tmp_path / "main.py"
    f.write_text("x = 1")
    with (
        patch(
            "kiro_crew.external_editor.open_path",
            side_effect=external_editor.EditorUnavailable("no window attached"),
        ),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/reveal", json={"path": str(f), "action": "editor"})
            assert resp.status == 503
            body = await resp.json()
    assert body == {"error": "no window attached", "code": "editor_unavailable"}
    mock_sel.log_tool_invocation.assert_called_with(
        session_key="api",
        source="api",
        tool_name="reveal_path",
        outcome="failure",
        error="editor_unavailable",
        resources=str(f),
        metadata={"action": "editor"},
    )


@pytest.mark.asyncio
async def test_editor_action_launch_failure_is_502(mock_sel, tmp_path):
    f = tmp_path / "main.py"
    f.write_text("x = 1")
    with (
        patch(
            "kiro_crew.external_editor.open_path",
            side_effect=external_editor.EditorOpenFailed("zzq refused the open"),
        ),
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/reveal", json={"path": str(f), "action": "editor"})
            assert resp.status == 502
            body = await resp.json()
    assert body == {"error": "zzq refused the open", "code": "editor_open_failed"}
    mock_sel.log_tool_invocation.assert_called_with(
        session_key="api",
        source="api",
        tool_name="reveal_path",
        outcome="failure",
        error="editor_open_failed",
        resources=str(f),
        metadata={"action": "editor"},
    )


@pytest.mark.asyncio
async def test_editor_action_missing_path_is_400(mock_sel):
    """A malformed body is a coded 400, never a 500 out of the action."""
    with patch("kiro_crew.external_editor.open_path") as open_path:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/reveal", json={"action": "editor"})
            assert resp.status == 400
            assert await resp.json() == {"error": "invalid path"}
    open_path.assert_not_called()


@pytest.mark.asyncio
async def test_unparseable_body_is_400_not_500(mock_sel):
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.post(
            "/api/reveal",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400


# ── action=editor: the target is absolutized before it is screened ────────────
#
# The route takes the path as its caller spelled it, so a relative or `~` path
# is the ordinary case rather than the odd one. Two properties are pinned here:
# the editor receives the ANCHORED path, and the anchored path is what the
# sensitive-path refusal judges -- a relative `.ssh/id_rsa` is unremarkable as a
# string and names a key once joined to a project directory.


@pytest.mark.asyncio
async def test_editor_action_anchors_a_relative_path_to_the_project_dir(mock_sel, tmp_path):
    """The editor receives the anchored path, not the text the caller sent."""
    (tmp_path / "website").mkdir()
    (tmp_path / "website" / "main.tsx").write_text("export {}")
    with (
        patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}),
        patch(
            "kiro_crew.external_editor.open_path",
            return_value="/tmp/vscode-ipc-zzq.sock",
        ) as open_path,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal", json={"path": "website/main.tsx", "action": "editor"}
            )
            assert resp.status == 200
    assert open_path.call_args.args[0] == os.path.join(str(tmp_path), "website/main.tsx")


@pytest.mark.asyncio
async def test_editor_action_refuses_a_relative_path_with_no_project_dir(mock_sel):
    """Ambiguity is a coded refusal: every candidate base names a different file."""
    with (
        patch("kiro_crew.external_editor.open_path") as open_path,
        patch.dict(os.environ, {}, clear=False),
    ):
        os.environ.pop("KIROCREW_PROJECT_DIR", None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal", json={"path": "website/main.tsx", "action": "editor"}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "path_unresolved"
    open_path.assert_not_called()
    assert mock_sel.log_tool_invocation.call_args.kwargs["error"] == "path_unresolved"


@pytest.mark.asyncio
async def test_editor_action_screens_the_path_it_resolved_to(mock_sel, tmp_path):
    """A relative path that is innocuous as text and sensitive once anchored."""
    seen: list[str] = []

    def _sensitive(path_str: str) -> bool:
        seen.append(path_str)
        # Only the ANCHORED form names the key: as a bare relative string this is
        # an unremarkable path, which is exactly why the second screen exists.
        return os.path.isabs(path_str) and path_str.endswith("id_rsa")

    with (
        patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", side_effect=_sensitive),
        patch.dict(os.environ, {"KIROCREW_PROJECT_DIR": str(tmp_path)}),
        patch("kiro_crew.external_editor.open_path") as open_path,
    ):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/reveal", json={"path": ".ssh/id_rsa", "action": "editor"}
            )
            assert resp.status == 403
    # The raw string passed the first screen; the anchored one is what refused it.
    assert seen == [".ssh/id_rsa", os.path.join(str(tmp_path), ".ssh/id_rsa")]
    open_path.assert_not_called()

"""``dashboard_snapshot`` on ``kirocrew-panel``: a picture of the crewmate's own page.

The tool asks ``GET /api/agent-panel/dashboard/snapshot`` for a fresh PNG of the page in
force, drawn over the crewmate's real values. The gateway writes the picture to an
owner-only file in the data home and answers with its PATH; the agent opens it with its
own file-read tool. The MCP transport's one exit, ``validation.build_tool_response``,
carries text only, so a picture travels as a path, the way ``kirocrew-computer`` hands
back a screenshot.

STATELESS. Nothing here remembers a caller: the session is resolved strictly on every
call and every call asks the gateway for a new picture. When the gateway draws nothing
(another check running, a host short of memory, no browser, a template page) the answer
says "no image" and why, and names no file: an older picture is never offered as the page
as it is now.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

TOOL_NAME: Final[str] = "dashboard_snapshot"
ROUTE: Final[str] = "/api/agent-panel/dashboard/snapshot"

#: The route waits up to about twelve seconds for the browser run, as a save does, and
#: the read of the page and the record comes first.
READ_TIMEOUT_SECS: Final[float] = 30.0

TOOL_DEFINITION: Final[dict[str, Any]] = {
    "name": TOOL_NAME,
    "description": (
        "LOOK at your crewmate's own Dashboard page as the person sees it: the gateway "
        "draws the page in force once, over your real data, in the same offline browser "
        "run dashboard_save uses, and hands back a PNG FILE PATH. Open that file with your "
        "file-read tool to see it, and judge both what it says and how it looks. One "
        "1280x800 view, the top of the page only, at most 2 MiB. Only a page you composed "
        "and applied is drawn; a page you only saved is not, so dashboard_apply it first. "
        "Read only: nothing is saved, applied or versioned, and every call draws afresh. "
        "When the check cannot run (another check running, a machine short of memory, no "
        "browser, a template page) the answer is 'no image' with the reason and no file: "
        "an earlier picture is not this page as it is now. The file holds your real "
        "values, so do not copy it anywhere else. The verification verdict comes with it. "
        "Takes no arguments."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

#: Said for the picture, outside the redaction pass: it holds only the gateway's own spool
#: path and numbers, which the pass could only damage.
SHOT_NOTE: Final[str] = (
    "Snapshot of your page in force{version}: {path}\n"
    "  ({width}x{height} png, {size}) -- drawn just now over your real data, in the "
    "Dashboard's default light look: the person's own theme pack or dark mode can show "
    "other colours. Open the file with your file-read tool to look at it. It holds your "
    "crewmate's real values: leave it where it is."
)

NO_IMAGE: Final[str] = (
    "No image of your page: {reason}. Nothing was drawn this time, and an earlier "
    "snapshot is not this page as it is now."
)


def _human_size(size: int) -> str:
    return f"{size / 1024:.0f} KiB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MiB"


def call_snapshot(
    sk: str,
    get: Callable[..., dict[str, Any]],
    render_verification: Callable[[Any], list[str]],
) -> tuple[str, str]:
    """Run ``dashboard_snapshot`` for the strictly resolved session *sk*.

    Returns ``(own, gateway)``: ``own`` holds only the spool path and numbers, and
    ``gateway`` holds text the gateway sent (a reason, the verification lines). The
    caller sends ``gateway`` through its redaction pass and puts ``own`` in front of it
    unchanged, since the pass could only damage a path. *get* and *render_verification*
    are ``mcp_panel``'s own, so a test that patches them there reaches this call too.
    """
    d = get(ROUTE, session_key=sk, timeout=READ_TIMEOUT_SECS)
    if d.get("error"):
        return "", f"Error: {d['error']}"
    raw = d.get("snapshot")
    snapshot: dict[str, Any] = raw if isinstance(raw, dict) else {}
    checked = render_verification(d.get("verification"))
    tail = "\n".join(["", *checked]) if checked else ""
    path = snapshot.get("path")
    if snapshot.get("status") != "image" or not isinstance(path, str) or not path:
        reason = str(snapshot.get("reason") or "the gateway drew nothing")
        return "", NO_IMAGE.format(reason=reason) + tail
    version = snapshot.get("package_version")
    size = snapshot.get("bytes")
    own = SHOT_NOTE.format(
        version=f" (version {version})" if isinstance(version, int) else "",
        path=path,
        width=snapshot.get("width"),
        height=snapshot.get("height"),
        size=_human_size(size) if isinstance(size, int) else "size unknown",
    )
    return own, tail

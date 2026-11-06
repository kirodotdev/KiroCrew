"""``POST /api/plantuml/svg``: a ```plantuml fence drawn by the host's own PlantUML.

The spawn itself is faked: what is pinned is the contract around it -- who may
render, which PlantUML release may run at all, how it is started (sandbox
chokepoint in ``strict`` mode, PlantUML's own ``SANDBOX`` profile, the source on
stdin), and the three answers the dashboard tells apart (a picture, no renderer,
a diagram PlantUML could not draw).
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers import plantuml_render as pr
from kiro_crew.sandbox import SandboxUnavailableError

pytestmark = pytest.mark.asyncio

SOURCE = "@startuml\nAlice -> Bob: hi\n@enduml\n"
CURRENT = (
    b"PlantUML version 1.2026.9 / 18d0aae [2026-10-17 08:34:41 UTC]\n(GPL source distribution)\n"
)


class _FakeProc:
    """A ``Popen`` double: records stdin, answers *stdout* with *returncode*."""

    pid = 4242

    def __init__(self, returncode: int = 0, stdout: bytes = b"<svg></svg>", hang: bool = False):
        self.returncode = returncode
        self._stdout = stdout
        self._hang = hang
        self.stdin_seen: bytes | None = None

    def communicate(self, data: bytes | None = None, timeout: float | None = None):
        if self._hang:
            self._hang = False
            raise subprocess.TimeoutExpired("plantuml", timeout or 0)
        self.stdin_seen = data
        return self._stdout, None


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the vetted executable, the sandbox chokepoint and the spawn; record each run.

    ``-version`` answers ``seen["version"]``; a render answers ``seen["proc"]``.
    """
    seen: dict[str, Any] = {"proc": _FakeProc(), "version": _FakeProc(stdout=CURRENT), "runs": []}

    def fake_chokepoint(argv: list[str], mode: str, *, env: dict[str, str]):
        seen["runs"].append({"argv": argv, "mode": mode, "env": env})
        return argv, env, None

    def fake_popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
        return seen["version"] if "-version" in argv else seen["proc"]

    monkeypatch.setattr(pr, "_plantuml_executable", lambda: "/opt/bin/plantuml")
    monkeypatch.setattr(pr, "_release_verdict", None)
    monkeypatch.setattr(pr, "sandboxed_spawn_argv", fake_chokepoint)
    monkeypatch.setattr(pr, "popen_limited", fake_popen)
    return seen


async def _post(body: object, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    """One request against a fresh owner app. Built INSIDE the coroutine because
    ``TestClient`` binds to the running loop at construction."""
    app = web.Application()
    app.router.add_post("/api/plantuml/svg", pr.api_plantuml_svg)
    async with TestClient(TestServer(as_owner(app))) as client:
        resp = await client.post("/api/plantuml/svg", json=body, headers=headers or {})
        return resp.status, await resp.json()


async def test_the_owner_gets_the_svg_from_a_strict_sandboxed_plantuml(
    spawn: dict[str, Any],
) -> None:
    status, payload = await _post({"source": SOURCE})

    assert (status, payload) == (200, {"svg": "<svg></svg>"})
    version, render = spawn["runs"]
    assert version["argv"] == ["/opt/bin/plantuml", "-version"]
    assert render["argv"] == ["/opt/bin/plantuml", "-tsvg", "-pipe", "-charset", "UTF-8"]
    assert {run["mode"] for run in spawn["runs"]} == {"strict"}
    assert render["env"]["PLANTUML_SECURITY_PROFILE"] == "SANDBOX"
    assert spawn["proc"].stdin_seen == SOURCE.encode("utf-8")
    # The release is read once per executable, not once per diagram.
    await _post({"source": SOURCE})
    assert [run["argv"][1] for run in spawn["runs"]] == ["-version", "-tsvg", "-tsvg"]


@pytest.mark.parametrize(
    "version_line, status",
    [
        (b"PlantUML version 1.2023.9 (Tue Jun 13 18:21:24 UTC 2023)\n", 200),
        # Still reads the environment past the profile, and predates its CVE fixes.
        (b"PlantUML version 1.2023.8 (Sun May 21 12:00:00 UTC 2023)\n", 404),
        # Debian and Ubuntu's packaged release: it ignores the profile and would
        # read any file or URL the diagram names.
        (b"PlantUML version 1.2020.02 (Sun Mar 01 10:22:07 UTC 2020)\n", 404),
        (b"PlantUML version 8059\n", 404),
        (b"not plantuml at all\n", 404),
    ],
)
async def test_only_a_release_that_applies_the_profile_draws(
    spawn: dict[str, Any], version_line: bytes, status: int
) -> None:
    spawn["version"] = _FakeProc(stdout=version_line)

    got, _ = await _post({"source": SOURCE})

    assert got == status
    assert ("-tsvg" in spawn["runs"][-1]["argv"]) is (status == 200)


@pytest.mark.parametrize("missing", ["executable", "sandbox", "sandbox at render"])
async def test_no_usable_renderer_is_unavailable(
    spawn: dict[str, Any], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    if missing == "executable":
        monkeypatch.setattr(pr, "_plantuml_executable", lambda: None)
    else:
        chokepoint = pr.sandboxed_spawn_argv

        def no_backend(argv: list[str], mode: str, *, env: dict[str, str]):
            if missing == "sandbox" or "-tsvg" in argv:
                raise SandboxUnavailableError(
                    "no sandbox backend", kind="no_backend", detail="test"
                )
            return chokepoint(argv, mode, env=env)

        monkeypatch.setattr(pr, "sandboxed_spawn_argv", no_backend)

    status, payload = await _post({"source": SOURCE})

    assert status == 404
    assert payload["code"] == "plantuml_unavailable"
    assert spawn["proc"].stdin_seen is None


async def test_a_diagram_plantuml_cannot_draw_is_a_failure_not_a_picture(
    spawn: dict[str, Any],
) -> None:
    # PlantUML answers a syntax error with exit 200 and an error picture on stdout.
    spawn["proc"] = _FakeProc(returncode=200, stdout=b"<svg>Syntax Error?</svg>")

    status, payload = await _post({"source": "@startuml\nnot ->-> valid\n@enduml\n"})

    assert status == 422
    assert payload["code"] == "plantuml_failed"


async def test_a_render_past_its_deadline_is_killed_with_its_tree(
    spawn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn["proc"] = _FakeProc(hang=True)
    killed: list[Any] = []
    monkeypatch.setattr(pr.platform_compat, "kill_popen_tree", killed.append)

    status, payload = await _post({"source": SOURCE})

    assert status == 422
    assert payload["code"] == "plantuml_failed"
    assert killed == [spawn["proc"]]


@pytest.mark.parametrize("caller", [{"X-Test-User": "telegram:42"}, {"X-Test-App": "some-app"}])
async def test_only_the_owner_session_may_render(
    spawn: dict[str, Any], caller: dict[str, str]
) -> None:
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        status, payload = await _post({"source": SOURCE}, headers=caller)

    assert status == 403
    assert payload["code"] == "owner_only"
    assert spawn["runs"] == []


@pytest.mark.parametrize("body", [{}, {"source": "   "}, {"source": 7}])
async def test_a_request_without_a_source_is_refused_before_any_run(
    spawn: dict[str, Any], body: dict[str, Any]
) -> None:
    status, payload = await _post(body)

    assert status == 400
    assert payload["code"] == "source_required"
    assert spawn["runs"] == []


async def test_a_plantuml_that_fails_the_provenance_check_is_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(path: str) -> str:
        raise ValueError(f"{path} is writable by others")

    monkeypatch.setattr(pr.shutil, "which", lambda _name: "/tmp/plantuml")
    monkeypatch.setattr(pr, "validate_provider_executable", refuse)
    assert pr._plantuml_executable() is None

    monkeypatch.setattr(pr, "validate_provider_executable", lambda _path: "/opt/vetted/plantuml")
    assert pr._plantuml_executable() == "/opt/vetted/plantuml"

    monkeypatch.setattr(pr.shutil, "which", lambda _name: None)
    assert pr._plantuml_executable() is None

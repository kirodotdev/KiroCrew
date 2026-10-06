"""Exact-key skill reads through ``skill_search``: a body over the per-call capacity
is read whole across pages, and a read that returns nothing says which of three
things stopped it -- the key is outside the scope, the file is unreadable, or the
body exceeds the capacity -- instead of one sentence naming all three."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from body_stream_helpers import BodyStreamPayload

from kiro_crew import mcp_core
from kiro_crew.mcp_core import _call_tool
from kiro_crew.skills import (
    PROJECT_SKILL_BODY_CAP,
    SkillBodyPage,
    SkillReadRefusal,
    SkillsLoader,
)
from kiro_crew.validation import MAX_RESPONSE_LEN, sanitize_response

pytestmark = pytest.mark.xdist_group("skill_read_paging")

# The per-call bound the read verb holds a body to, in UTF-8 bytes. Spelled as a
# literal so a change to the constant is a visible test change, not a silent one.
_CAPACITY = 99_000
_KEY = "large/procedure"
# A body clearly over the capacity, the shape the read verb refuses today.
_LARGE = _CAPACITY + 20_000


def _skill(root: Path, key: str, body: str, *, newline: str = "\n") -> Path:
    """Write a skill file; ``newline="\\r\\n"`` writes the shape Git checks out on
    Windows, in bytes so the shape is the same on every platform."""
    path = root / key / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {key}\ndescription: desc for {key}\n---\n{body}"
    if newline == "\n":
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _body_over(size: int) -> str:
    """Numbered instruction lines whose UTF-8 total is at least ``size`` bytes."""
    lines: list[str] = []
    spent = 0
    while spent <= size:
        line = f"instruction line {len(lines):05d}: " + "x" * 40 + "\n"
        lines.append(line)
        spent += len(line.encode("utf-8"))
    return "".join(lines)


def _fits_one_response(out: str) -> None:
    """The invariant every read answer must hold: the tail cut never touches it."""
    assert len(out) <= MAX_RESPONSE_LEN
    assert sanitize_response(out) == out


def _named_capacity(out: str) -> int:
    """The per-read capacity an over-capacity refusal names for this key."""
    match = re.search(r"at most ([\d,]+) bytes", out)
    assert match, out
    return int(match.group(1).replace(",", ""))


def _rendered_page(out: str) -> tuple[str, int | None]:
    """The page text between the instruction markers, and the next offset it names."""
    start = out.index("]\n", out.index("[Skill instructions")) + len("]\n")
    end = out.index("\n[End skill instructions]", start)
    match = re.search(r"next page: offset=(\d+)", out)
    return out[start:end], int(match.group(1)) if match else None


@pytest.fixture
def skills_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``skill_search`` with no signed session reads through a loader on ``tmp_path``."""
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(
        mcp_core,
        "SkillsLoader",
        lambda **_kw: SkillsLoader(skills_path=root, install_builtins=False),
    )
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *_a: ("", "unsigned"))
    for verb in ("_get", "_post"):
        monkeypatch.setattr(
            mcp_core, verb, lambda *_a, **_kw: pytest.fail("an unsigned read reached the gateway")
        )
    return root


def _read(key: str, **paging: int) -> str:
    return _call_tool("skill_search", {"action": "read", "key": key, **paging})


class TestPagedRead:
    def test_a_body_over_the_capacity_is_read_whole_across_pages(self, skills_root: Path):
        path = _skill(skills_root, _KEY, _body_over(_LARGE))
        text = path.read_text(encoding="utf-8")
        size = len(text.encode("utf-8"))
        assert size > _CAPACITY

        whole = _read(_KEY)
        assert whole.startswith("Error:")
        assert f"{size:,} bytes" in whole
        # The capacity named is the ceiling less this tool's own framing.
        assert _CAPACITY - 2_000 < _named_capacity(whole) < _CAPACITY
        assert "offset" in whole and "limit" in whole
        assert "outside" not in whole and "could not be read" not in whole

        pages: list[str] = []
        offset: int | None = 0
        while offset is not None:
            out = _read(_KEY, offset=offset)
            assert not out.startswith("Error:"), out
            _fits_one_response(out)
            content, offset = _rendered_page(out)
            assert 0 < len(content.encode("utf-8")) < _CAPACITY
            pages.append(content)
        assert len(pages) >= 2
        assert "".join(pages) == text

    def test_a_page_states_its_lines_and_bytes_against_the_whole(self, skills_root: Path):
        path = _skill(skills_root, _KEY, _body_over(_LARGE))
        text = path.read_text(encoding="utf-8")
        total_lines = text.count("\n")
        out = _read(_KEY, offset=0)
        content, next_offset = _rendered_page(out)
        returned_lines = content.count("\n")
        assert next_offset == returned_lines
        assert f"lines 0-{returned_lines - 1} of {total_lines}" in out
        assert f"{len(content.encode('utf-8')):,} of {len(text.encode('utf-8')):,} bytes" in out
        # Navigation is in the header, ahead of the body, where tail truncation
        # cannot reach it.
        assert out.index("next page: offset=") < out.index("[Skill instructions")

    def test_limit_bounds_the_page_in_lines(self, skills_root: Path):
        path = _skill(skills_root, "small", "one\ntwo\nthree\nfour\n")
        # Four frontmatter lines and four body lines.
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        assert len(lines) == 8
        first = _read("small", offset=0, limit=3)
        content, next_offset = _rendered_page(first)
        assert content == "".join(lines[:3])
        assert next_offset == 3
        rest = _read("small", offset=3, limit=5)
        content, next_offset = _rendered_page(rest)
        assert content == "".join(lines[3:])
        assert next_offset is None
        assert "last page" in rest

    def test_a_read_without_paging_returns_a_fitting_body_whole(self, skills_root: Path):
        _skill(skills_root, "small", "one\ntwo\n")
        out = _read("small")
        assert not out.startswith("Error:")
        assert "one\ntwo\n" in out
        assert "next page" not in out and "last page" not in out

    def test_an_offset_past_the_end_is_an_empty_last_page_naming_the_count(self, skills_root: Path):
        _skill(skills_root, "small", "one\ntwo\n")
        out = _read("small", offset=99)
        assert not out.startswith("Error:")
        assert "Page: no lines of 6" in out and "last page" in out
        content, next_offset = _rendered_page(out)
        assert content == "" and next_offset is None


class TestRefusalMessages:
    def test_an_unknown_key_is_reported_as_outside_the_scope(self, skills_root: Path):
        _skill(skills_root, "present", "body\n")
        out = _read("absent")
        assert out.startswith("Error:")
        assert "`absent`" in out
        assert "outside this agent's scope" in out
        assert "capacity" not in out and "could not be read" not in out

    def test_a_file_the_fenced_reader_refuses_is_reported_as_unreadable(
        self, skills_root: Path, tmp_path: Path
    ):
        path = _skill(skills_root, "linked", "body\n")
        # A second name for the inode: the no-link reader refuses st_nlink > 1.
        os.link(path, tmp_path / "twin.md")
        out = _read("linked")
        assert out.startswith("Error:")
        assert "`linked`" in out
        assert "could not be read" in out
        assert "outside" not in out and "capacity" not in out

    @pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_an_over_capacity_body_names_size_cap_and_paging(self, skills_root: Path, newline: str):
        """The size named is the body's as delivered -- UTF-8 bytes of the decoded
        text, newlines folded -- because the capacity is a budget on what one
        response carries. A CRLF checkout is larger on disk than what is
        delivered; the refusal must not name that on-disk figure."""
        path = _skill(skills_root, _KEY, _body_over(_LARGE), newline=newline)
        size = len(path.read_text(encoding="utf-8").encode("utf-8"))
        out = _read(_KEY)
        assert out.startswith("Error:")
        assert f"`{_KEY}`" in out
        assert f"{size:,} bytes" in out
        if newline != "\n":
            on_disk = len(path.read_bytes())
            assert on_disk > size
            assert f"{on_disk:,} bytes" not in out
        assert 0 < _named_capacity(out) <= _CAPACITY
        assert "offset=0" in out and "limit" in out
        assert "outside" not in out and "could not be read" not in out

    def test_a_single_line_wider_than_the_capacity_is_refused_by_line(self, skills_root: Path):
        _skill(skills_root, "wide", "short\n" + "y" * (_CAPACITY + 10) + "\n")
        # Line 5: four frontmatter lines, then "short", then the wide line.
        out = _read("wide", offset=5)
        assert out.startswith("Error:")
        assert "line 5" in out
        # The line is measured as delivered, with its newline.
        assert f"{_CAPACITY + 11:,} bytes" in out
        assert "no page can hold it" in out

    def test_an_over_wide_line_names_the_offset_that_resumes_paging(self, skills_root: Path):
        """A paging loop that reaches an over-wide line must be told where to go
        next, or it has no documented way to the lines after it."""
        _skill(skills_root, "wide", "short\n" + "y" * (_CAPACITY + 10) + "\ntail line\n")
        out = _read("wide", offset=5)
        assert out.startswith("Error:")
        match = re.search(r"continue with offset=(\d+)", out)
        assert match, out
        assert int(match.group(1)) == 6
        rest = _read("wide", offset=int(match.group(1)))
        assert not rest.startswith("Error:"), rest
        content, next_offset = _rendered_page(rest)
        assert content == "tail line\n" and next_offset is None

    def test_a_miss_while_the_catalog_builds_is_not_reported_as_absent(
        self, skills_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """While the first walk is unfinished the confined project tier is
        withheld, so a correct project key misses; the answer must hedge rather
        than tell the agent no skill has that key."""
        monkeypatch.setattr(SkillsLoader, "catalog_status", lambda self, *_a, **_kw: "building")
        out = _read("team/project-skill")
        assert out.startswith("Error:")
        assert "`team/project-skill`" in out
        assert "not conclusive" in out and "still building" in out
        assert "no skill here" not in out and "outside this agent's scope" not in out

    def test_a_building_miss_carries_incomplete_on_the_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = tmp_path / "skills"
        root.mkdir()
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        try:
            complete = loader.read_scoped_skill_page("absent")
            assert isinstance(complete, SkillReadRefusal)
            assert complete.reason == "outside_scope" and not complete.incomplete
            monkeypatch.setattr(SkillsLoader, "catalog_status", lambda self, *_a, **_kw: "building")
            building = loader.read_scoped_skill_page("absent")
            assert isinstance(building, SkillReadRefusal)
            assert building.reason == "outside_scope" and building.incomplete
        finally:
            loader.close()


class TestGatewayContract:
    """The signed path: the gateway pages and diagnoses; the tool only renders."""

    def test_the_tool_forwards_paging_only_when_the_caller_gave_it(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        bodies: list[dict] = []
        monkeypatch.setattr(
            mcp_core, "require_strict_session_key", lambda *_a: ("dashboard:signed", None)
        )
        monkeypatch.setattr(
            mcp_core, "SkillsLoader", lambda **_kw: pytest.fail("signed read fell back")
        )

        def post(path, body, *, session_key):
            bodies.append(body)
            return {"matches": [{"key": "k", "name": "k", "content": "body\n"}]}

        monkeypatch.setattr(mcp_core, "_post", post)
        _read("k")
        assert "offset" not in bodies[0] and "limit" not in bodies[0]
        assert 0 < bodies[0]["capacity"] < _CAPACITY
        _read("k", offset=0)
        assert bodies[1]["offset"] == 0 and "limit" not in bodies[1]
        _read("k", offset=4, limit=2)
        assert bodies[2]["offset"] == 4 and bodies[2]["limit"] == 2

    def test_a_page_sized_to_the_requested_capacity_fits_the_response_for_any_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The framing repeats the key, so the capacity the tool asks for must
        already leave room for it: a page of exactly that many bytes, wrapped,
        stays under the response cap and loses no line to the tail cut."""
        key = "/".join(f"segment-{n:03d}" for n in range(90))
        monkeypatch.setattr(
            mcp_core, "require_strict_session_key", lambda *_a: ("dashboard:signed", None)
        )

        def post(path, body, *, session_key):
            capacity = body.get("capacity", _CAPACITY)
            assert capacity <= _CAPACITY
            line = "y" * 79 + "\n"
            content = line * (capacity // len(line))
            return {
                "matches": [
                    {
                        "key": key,
                        "name": key,
                        "content": content,
                        "page": {
                            "line_offset": body["offset"],
                            "line_count": content.count("\n"),
                            "total_lines": 10**7,
                            "total_bytes": 10**9,
                            "capacity": capacity,
                            "next_offset": body["offset"] + content.count("\n"),
                        },
                    }
                ],
                "next_offset": None,
            }

        monkeypatch.setattr(mcp_core, "_post", post)
        out = _read(key, offset=123_456_789)
        assert not out.startswith("Error:")
        _fits_one_response(out)
        content, next_offset = _rendered_page(out)
        assert next_offset == 123_456_789 + content.count("\n")

    @pytest.mark.parametrize(
        ("refusal", "expected", "absent"),
        [
            (
                {"reason": "outside_scope", "capacity": _CAPACITY},
                "outside this agent's scope",
                ("capacity", "could not be read"),
            ),
            (
                {"reason": "unreadable", "capacity": _CAPACITY},
                "could not be read",
                ("outside", "capacity"),
            ),
            (
                {
                    "reason": "over_capacity",
                    "capacity": _CAPACITY,
                    "size_bytes": 119_124,
                    "confined": False,
                },
                "119,124 bytes",
                ("outside", "could not be read"),
            ),
            (
                {
                    "reason": "over_capacity",
                    "capacity": PROJECT_SKILL_BODY_CAP,
                    "size_bytes": None,
                    "confined": True,
                },
                f"{PROJECT_SKILL_BODY_CAP:,}-byte confined project body bound",
                ("offset", "outside"),
            ),
        ],
    )
    def test_each_gateway_refusal_renders_its_own_message(
        self,
        monkeypatch: pytest.MonkeyPatch,
        refusal: dict,
        expected: str,
        absent: tuple[str, ...],
    ):
        monkeypatch.setattr(
            mcp_core, "require_strict_session_key", lambda *_a: ("dashboard:signed", None)
        )
        monkeypatch.setattr(
            mcp_core,
            "_post",
            lambda *_a, **_kw: {"matches": [], "next_offset": None, "refusal": refusal},
        )
        out = _read("team/k")
        assert out.startswith("Error:")
        assert "`team/k`" in out
        assert expected in out
        for word in absent:
            assert word not in out, (word, out)

    @pytest.mark.asyncio
    async def test_the_route_pages_and_diagnoses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened
    ):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import prompts

        root = tmp_path / "skills"
        large = _skill(root, _KEY, _body_over(_LARGE))
        text = large.read_text(encoding="utf-8")
        _skill(root, "small", "one\ntwo\n")
        loader = opened(SkillsLoader(skills_path=root, install_builtins=False))
        monkeypatch.setattr(prompts, "_read_session_key", lambda request: "dashboard:scoped")
        monkeypatch.setattr(prompts, "_deny_foreign_app_skill_slot", lambda *args: None)
        monkeypatch.setattr(prompts, "_get_skills", lambda state: loader)
        monkeypatch.setattr(prompts, "requesting_slot_project", lambda *args: None)
        monkeypatch.setattr(prompts, "_named_slot", lambda *args: SimpleNamespace(agent="custom"))
        monkeypatch.setattr(prompts, "session_skill_globs", lambda *args, **kw: None)
        app = web.Application()
        app["state"] = SimpleNamespace(sessions=None)

        async def get(**query):
            request = make_mocked_request(
                "GET", "/api/skills?" + urlencode({"q": "", "action": "read", **query}), app=app
            )
            return json.loads((await prompts.api_skills(request)).body)

        async def post(**body):
            payload = json.dumps({"scope": "installed", "action": "read", **body}).encode()
            request = make_mocked_request(
                "POST",
                "/api/skills/-/discover",
                headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
                payload=BodyStreamPayload(payload),
                app=app,
            )
            return json.loads((await prompts.api_skills(request)).body)

        whole = await get(key="small")
        assert (
            whole["matches"][0]["content"]
            == "---\nname: small\ndescription: desc for small\n---\none\ntwo\n"
        )
        assert "page" not in whole["matches"][0]

        refused = await get(key=_KEY)
        assert refused["matches"] == []
        assert refused["refusal"]["reason"] == "over_capacity"
        assert refused["refusal"]["size_bytes"] == len(text.encode("utf-8"))
        assert refused["refusal"]["capacity"] == _CAPACITY
        assert refused["refusal"]["confined"] is False

        pages: list[str] = []
        offset: int | None = 0
        while offset is not None:
            page = (await post(key=_KEY, offset=offset))["matches"][0]
            assert len(page["content"].encode("utf-8")) <= _CAPACITY
            assert page["page"]["line_offset"] == offset
            assert page["page"]["total_bytes"] == len(text.encode("utf-8"))
            pages.append(page["content"])
            offset = page["page"]["next_offset"]
        assert len(pages) >= 2
        assert "".join(pages) == text

        limited = (await get(key="small", offset=1, limit=1))["matches"][0]
        assert limited["content"] == "name: small\n"
        assert limited["page"]["next_offset"] == 2

        assert (await get(key="absent"))["refusal"]["reason"] == "outside_scope"
        past = (await get(key="small", offset=50))["matches"][0]
        assert past["content"] == ""
        assert past["page"] == {
            "line_offset": 50,
            "line_count": 0,
            "total_lines": 6,
            "total_bytes": past["page"]["total_bytes"],
            "next_offset": None,
        }

        # A caller may shrink the capacity to its own framing, never raise it.
        narrow = (await post(key=_KEY, offset=0, capacity=10_000))["matches"][0]
        assert 0 < len(narrow["content"].encode("utf-8")) <= 10_000
        assert "capacity" not in narrow["page"]
        wide = await get(key=_KEY, capacity=10**9)
        assert wide["refusal"]["capacity"] == _CAPACITY


class TestLoaderContract:
    def test_the_pager_copies_only_the_page_out_of_a_newline_dense_body(self):
        """A body may be as large as the file safety cap; splitting it into a
        list of lines would allocate one object per line, dozens of times the
        body's own size. Only the page's lines are copied; the count is a scan."""
        import tracemalloc

        from kiro_crew.skills import _page_skill_body

        body = "".join(f"{n}\n" for n in range(500_000))
        tracemalloc.start()
        try:
            whole = _page_skill_body(body, offset=None, limit=None, capacity=_CAPACITY)
            page = _page_skill_body(body, offset=499_990, limit=None, capacity=_CAPACITY)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # One UTF-8 copy for the byte count plus the page itself; a line list
        # for this body measures over ten times the body.
        assert peak < 3 * len(body)
        assert whole.reason == "over_capacity" and whole.size_bytes == len(body)
        assert page.content == "".join(f"{n}\n" for n in range(499_990, 500_000))
        assert page.total_lines == 500_000 and page.next_offset is None

    def test_the_whole_read_keeps_its_fits_or_none_contract(self, tmp_path: Path, opened):
        root = tmp_path / "skills"
        _skill(root, _KEY, _body_over(_LARGE))
        _skill(root, "small", "one\n")
        loader = opened(SkillsLoader(skills_path=root, install_builtins=False))
        assert loader.read_scoped_skill(_KEY) is None
        assert "one\n" in loader.read_scoped_skill("small")

    def test_a_confined_body_is_never_read_past_the_project_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened
    ):
        from kiro_crew import skill_trust

        project = tmp_path / "project"
        key = "release"
        path = _skill(project / ".kiro" / "skills", key, "z" * PROJECT_SKILL_BODY_CAP)
        loader = opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        if skill_trust.project_skill_traversal_supported():
            skill_trust.grant_project_trust(project)
        else:
            _admit_project_without_traversal(monkeypatch, loader, key, path, project)
        _assert_confined_read_stops_at_the_bound(loader, key, project)

    def test_the_confined_bound_holds_where_projects_cannot_be_walked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened
    ):
        """The Windows shape on every platform: no no-follow directory traversal,
        so the project is admitted by stub. The stub must cover BOTH gates the
        read passes -- the enumeration and the trust verdict ``load_skill``
        re-checks on its own -- or the body is refused before its size is ever
        measured and the reason reads ``unreadable`` instead of the bound."""
        from kiro_crew import skill_trust

        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: False)
        project = tmp_path / "project"
        key = "release"
        path = _skill(project / ".kiro" / "skills", key, "z" * PROJECT_SKILL_BODY_CAP)
        loader = opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        _admit_project_without_traversal(monkeypatch, loader, key, path, project)
        _assert_confined_read_stops_at_the_bound(loader, key, project)

    def test_a_confined_body_under_the_project_bound_pages_at_a_smaller_capacity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened
    ):
        """The project cap bounds the FILE; the capacity bounds one DELIVERY. A
        confined body that fits the project cap but not a caller's smaller
        capacity is served in pages, exactly like a global one, and the whole
        read is refused with its size and the paging hint -- never with the
        project-bound message, which would say the body exceeds a bound it does
        not exceed."""
        from kiro_crew import skill_trust

        project = tmp_path / "project"
        key = "release"
        body = "".join(f"step {n:04d}: " + "z" * 40 + "\n" for n in range(400))
        path = _skill(project / ".kiro" / "skills", key, body)
        text = path.read_text(encoding="utf-8")
        assert 10_000 < len(text.encode("utf-8")) < PROJECT_SKILL_BODY_CAP
        loader = opened(SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False))
        if skill_trust.project_skill_traversal_supported():
            skill_trust.grant_project_trust(project)
        else:
            _admit_project_without_traversal(monkeypatch, loader, key, path, project)

        page = loader.read_scoped_skill_page(key, project_dir=project, offset=0, capacity=10_000)
        assert isinstance(page, SkillBodyPage), page
        assert 0 < len(page.content.encode("utf-8")) <= 10_000
        assert page.next_offset is not None
        assert page.total_bytes == len(text.encode("utf-8"))

        whole = loader.read_scoped_skill_page(key, project_dir=project, capacity=10_000)
        assert isinstance(whole, SkillReadRefusal), whole
        assert whole.reason == "over_capacity"
        assert whole.capacity == 10_000
        assert whole.size_bytes == len(text.encode("utf-8"))
        assert whole.confined is False

        fits = loader.read_scoped_skill_page(key, project_dir=project)
        assert isinstance(fits, SkillBodyPage) and fits.content == text


def _admit_project_without_traversal(
    monkeypatch: pytest.MonkeyPatch, loader: SkillsLoader, key: str, path: Path, project: Path
) -> None:
    """Admit one project skill on a platform that cannot walk projects.

    Two gates stand between a project key and its bytes, and each is stubbed
    where it is asked: the scoped enumeration (``_iter``) lists the key, and the
    trust verdict (``_trusted_project_key``) lets ``load_skill`` reach the
    confined reader, which is the only place the project bound is enforced.
    """
    monkeypatch.setattr(loader, "_iter", lambda *args: [(key, path, str(project))])
    monkeypatch.setattr(loader, "_trusted_project_key", lambda project_dir: str(project))


def _assert_confined_read_stops_at_the_bound(loader: SkillsLoader, key: str, project: Path) -> None:
    outcome = loader.read_scoped_skill_page(key, project_dir=project, offset=0)
    assert outcome.reason == "over_capacity"
    assert outcome.confined is True
    assert outcome.capacity == PROJECT_SKILL_BODY_CAP
    assert outcome.size_bytes is None
    # The bound named is the project's, whatever capacity the caller asked for.
    narrow = loader.read_scoped_skill_page(key, project_dir=project, offset=0, capacity=10_000)
    assert narrow.reason == "over_capacity" and narrow.confined is True
    assert narrow.capacity == PROJECT_SKILL_BODY_CAP

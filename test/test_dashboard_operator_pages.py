"""An operator's own dashboard pages, served from the data home's ``panel-templates`` folder.

A page there is a directory ``panel-templates/<id>/`` holding ``manifest.json`` and
``template.html``, in the built-in format. The catalog serves it with origin ``user``.
The folder is the one the panel's own template overrides use, and every agent write
path is already refused on it, so a page there is one the person put there.

Every case runs on a scratch data home. Nothing here reads or writes the real one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest

from kiro_crew import agent_panel
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates import catalog, instance

MEMBER = "Fleet Conductor"
SLUG = "fleet-conductor"

PAGE = '<div><b data-dashboard-field="credits"></b>' '<i data-dashboard-field="phase"></i></div>'

REPO = Path(__file__).resolve().parent.parent


def _manifest(template_id: str, source: str) -> dict:
    return {
        "id": template_id,
        "version": 1,
        "title": f"{template_id} board",
        "description": "A template these tests own.",
        "source": source,
        "fields": {
            "credits": {"type": "number", "source": {"fold": "usage", "path": "credits"}},
            "phase": {"type": "string", "source": {"agentic": True}},
        },
    }


def _write(directory: Path, template_id: str, source: str, page: str = PAGE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(_manifest(template_id, source)), encoding="utf-8"
    )
    (directory / "template.html").write_text(page, encoding="utf-8")
    return directory


def _operator_root() -> Path:
    return agent_panel.data_home() / agent_panel.TEMPLATES_DIRNAME


def _operator_page(template_id: str, source: str = "user", page: str = PAGE) -> Path:
    return _write(_operator_root() / template_id, template_id, source, page)


def _problem(found: catalog.Catalog, name: str) -> str:
    reasons = [why for where, why in found.problems if where == name]
    assert reasons, f"no problem reported for {name!r}: {found.problems}"
    return " ".join(reasons)


def _can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    """A scratch data home, one built-in fixture page, and the member checks stubbed."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    builtin = _write(tmp_path / "builtin" / "fixture-board", "fixture-board", "builtin")
    _floor_monkeypatch.setattr(catalog, "builtin_dir", lambda: builtin.parent)

    cfg = SimpleNamespace(agents={MEMBER: SimpleNamespace(member_id="")})
    _floor_monkeypatch.setattr(routes.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    _floor_monkeypatch.setattr(routes.members_mod, "member_slug", lambda name, config=None: SLUG)
    _floor_monkeypatch.setattr(routes.members_mod, "validate_slug", lambda slug: slug)
    _floor_monkeypatch.setattr(routes, "_member_names_for_slug", lambda cfg, slug: [MEMBER])
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: "")
    assert str(agent_panel.data_home()).startswith(str(tmp_path)), "not a scratch home"
    yield


# --------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------


def test_an_operator_page_is_served_with_origin_user() -> None:
    _operator_page("my-board")
    found = catalog.list_templates()
    entry = found.by_id["my-board"]
    assert entry.origin == "user"
    assert entry.manifest.source == "user"
    assert entry.html == PAGE
    assert entry.listing()["origin"] == "user"
    # The built-in page is still there beside it.
    assert found.by_id["fixture-board"].origin == catalog.BUILTIN_SOURCE


def test_an_absent_operator_folder_is_an_empty_set_not_a_problem() -> None:
    assert not _operator_root().exists()
    found = catalog.list_templates()
    assert sorted(found.by_id) == ["fixture-board"]
    assert found.problems == ()


def test_an_operator_page_claiming_builtin_is_refused() -> None:
    """The existing ``source == origin`` check: a page here may say only ``user``."""
    _operator_page("my-board", source="builtin")
    found = catalog.list_templates()
    assert "my-board" not in found.by_id
    assert "'builtin' is not 'user'" in _problem(found, "my-board")


def test_a_built_in_page_claiming_user_is_still_refused() -> None:
    _write(catalog.builtin_dir() / "odd-board", "odd-board", "user")
    found = catalog.list_templates()
    assert "odd-board" not in found.by_id
    assert "'user' is not 'builtin'" in _problem(found, "odd-board")


def test_the_built_in_page_wins_an_id_clash_and_the_clash_is_reported() -> None:
    _operator_page(
        "fixture-board",
        page='<p data-dashboard-field="credits"></p>' '<p data-dashboard-field="phase"></p>',
    )
    found = catalog.list_templates()
    entry = found.by_id["fixture-board"]
    assert entry.origin == catalog.BUILTIN_SOURCE
    assert entry.html == PAGE
    assert "shipped" in _problem(found, "fixture-board")
    assert catalog.load_one("fixture-board").origin == catalog.BUILTIN_SOURCE


def test_a_linked_operator_folder_is_refused(tmp_path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("this host cannot create a symlink")
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "my-board", "my-board", "user")
    _operator_root().parent.mkdir(parents=True, exist_ok=True)
    _operator_root().symlink_to(elsewhere, target_is_directory=True)

    found = catalog.list_templates()
    assert "my-board" not in found.by_id
    assert "symlink" in _problem(found, agent_panel.TEMPLATES_DIRNAME)
    # One bad root does not hide the built-in pages.
    assert "fixture-board" in found.by_id


def test_a_linked_page_directory_is_refused(tmp_path) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("this host cannot create a symlink")
    elsewhere = _write(tmp_path / "elsewhere" / "my-board", "my-board", "user")
    _operator_root().mkdir(parents=True)
    (_operator_root() / "my-board").symlink_to(elsewhere, target_is_directory=True)

    found = catalog.list_templates()
    assert "my-board" not in found.by_id
    assert "link" in _problem(found, "my-board")


@pytest.mark.parametrize("leaf", ["manifest.json", "template.html"])
def test_a_linked_file_inside_a_page_is_refused(tmp_path, leaf) -> None:
    if not _can_symlink(tmp_path):
        pytest.skip("this host cannot create a symlink")
    page = _operator_page("my-board")
    outside = tmp_path / f"outside-{leaf}"
    outside.write_bytes((page / leaf).read_bytes())
    (page / leaf).unlink()
    (page / leaf).symlink_to(outside)

    found = catalog.list_templates()
    assert "my-board" not in found.by_id
    assert "link" in _problem(found, "my-board")


def test_a_page_directory_and_a_panel_template_file_never_collide() -> None:
    """Panel discovery reads only ``.html`` files; the catalog reads only directories."""
    _operator_page("my-board")
    (_operator_root() / "my-panel.html").write_text(agent_panel.DATA_MARKER, encoding="utf-8")
    assert "my-board" not in agent_panel.available_templates()
    assert "my-panel" in agent_panel.available_templates()
    found = catalog.list_templates()
    assert "my-panel" not in found.by_id
    assert all(name != "my-panel.html" for name, _ in found.problems)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_an_operator_page_is_renderable() -> None:
    assert "user" in instance.RENDERABLE_SOURCES
    assert catalog.BUILTIN_SOURCE in instance.RENDERABLE_SOURCES
    assert "shared" not in instance.RENDERABLE_SOURCES


def test_trusted_page_serves_the_operator_page() -> None:
    _operator_page("my-board")
    record = instance.adopt(SLUG, "my-board", session_id="")
    assert record.manifest["source"] == "user"
    assert routes._trusted_page(SLUG, instance.read(SLUG)) == PAGE


def test_trusted_page_reads_the_folder_not_the_record() -> None:
    """A tampered stored copy is not what runs, for a user page as for a built-in one."""
    _operator_page("my-board")
    instance.adopt(SLUG, "my-board", session_id="")
    path = instance.instance_dir(SLUG) / "instance.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["html"] = PAGE + "<p>appended</p>"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert routes._trusted_page(SLUG, instance.read(SLUG)) == PAGE


def test_trusted_page_refuses_when_a_shipped_page_takes_an_adopted_operator_id() -> None:
    """A release shipping the same id must not swap in different markup."""
    _operator_page("my-board")
    instance.adopt(SLUG, "my-board", session_id="")
    shipped = '<p data-dashboard-field="credits"></p><p data-dashboard-field="phase"></p>'
    _write(catalog.builtin_dir() / "my-board", "my-board", "builtin", page=shipped)
    assert catalog.load_one("my-board").origin == catalog.BUILTIN_SOURCE
    assert routes._trusted_page(SLUG, instance.read(SLUG)) is None


def test_trusted_page_refuses_an_operator_page_that_was_removed() -> None:
    page = _operator_page("my-board")
    instance.adopt(SLUG, "my-board", session_id="")
    for leaf in ("manifest.json", "template.html"):
        (page / leaf).unlink()
    page.rmdir()
    assert routes._trusted_page(SLUG, instance.read(SLUG)) is None


def test_the_served_page_is_framed_exactly_like_a_built_in_one() -> None:
    """Same composition, so the same frame head, CSP and sandbox apply.

    The gateway sends a BODY; the dashboard frame builds the head and its CSP and
    mounts it through one sandbox function. So "same CSP and sandbox" is two facts: the
    body composed for a user page is byte-identical to a built-in one with the same
    markup, and the frame never branches on where a page came from.

    The grant a page gets is not always the same STRING -- a page whose owner the admin
    ceiling denies egress is mounted with none of it -- so the pin is the function and
    its argument rather than the literal: every mount calls the one helper, and every
    mount hands it the server's own answer. A page cannot be wrapped more loosely than
    its sibling by naming a different spelling, nor by passing a literal that skips the
    server.
    """
    _operator_page("my-board")
    instance.adopt(SLUG, "fixture-board", session_id="")
    builtin_body = routes._render(SLUG, MEMBER, instance.read(SLUG))
    instance.adopt(SLUG, "my-board", session_id="")
    user_body = routes._render(SLUG, MEMBER, instance.read(SLUG))
    assert builtin_body is not None and user_body is not None
    assert user_body == builtin_body

    from kiro_crew import dashboard_frame

    document = dashboard_frame.compose(PAGE, dashboard_frame.read_payload({}))
    assert f'content="{dashboard_frame.FRAME_CSP}"' in document
    assert "connect-src 'none'" in dashboard_frame.FRAME_CSP

    frame = (REPO / "website/src/pages/members/CrewDynamicDashboard.tsx").read_text(
        encoding="utf-8"
    )
    # The grant is defined once, in lib/crewDashboardSandbox.ts, and the frame
    # imports exactly that definition.
    grant = (REPO / "website/src/lib/crewDashboardSandbox.ts").read_text(encoding="utf-8")
    assert "export const CREW_DASHBOARD_SANDBOX = 'allow-scripts'" in grant
    assert "import { CREW_DASHBOARD_SANDBOX } from '../../lib/crewDashboardSandbox'" in frame
    assert "export const CREW_DASHBOARD_SANDBOX_STATIC = ''" in frame
    # ONE function wraps every page. A mount that named anything else -- a constant, a
    # local, a ternary of its own -- could wrap an operator page differently from a
    # built-in one, which is the whole point of this test.
    mounts = re.findall(r"sandbox=\{([^}]*)\}", frame)
    assert len(mounts) == len(re.findall(r"sandbox=\{", frame))
    assert mounts, "the frame mounts no page at all"
    assert all(
        m.startswith("crewDashboardSandbox(") for m in mounts
    ), f"a mount wraps its page by some other means: {mounts}"
    # ... and that one function answers only with the frame's own two grant sets, so
    # going through it is the same as being wrapped in the constant when script is on.
    assert re.search(
        r"export function crewDashboardSandbox\(scripts: boolean\): string \{\s*"
        r"return scripts \? CREW_DASHBOARD_SANDBOX : CREW_DASHBOARD_SANDBOX_STATIC\s*\}",
        frame,
    ), "the sandbox helper answers with something other than the two declared grant sets"
    # Same AUTHORIZATION SOURCE at every mount: the server's answer, never a literal.
    # `mayRunScript` is what the server said about the owner agent's egress, and
    # `scripts` is what it said about this page; a mount passing `true` would license
    # itself.
    assert all(
        "mayRunScript" in m and "scripts" in m for m in mounts
    ), f"a mount decides its own grant instead of asking the server: {mounts}"
    assert not re.search(
        r"manifest\??\.source|\.origin\b|'user'", frame
    ), "the frame reads a page's provenance, so a user page could be framed differently"


# --------------------------------------------------------------------------
# the five fences, pinned for a page file one directory down
# --------------------------------------------------------------------------


def _page_file() -> str:
    return str(_operator_root() / "my-board" / "template.html")


def test_an_agent_file_tool_write_to_a_page_file_is_refused() -> None:
    from kiro_crew.security import is_sensitive_write_path

    assert is_sensitive_write_path(_page_file())
    assert is_sensitive_write_path(str(_operator_root() / "my-board" / "manifest.json"))
    for prefix in (".kiro/crew", ".kirocrew"):
        spelled = Path.home() / prefix / "panel-templates" / "my-board" / "template.html"
        assert is_sensitive_write_path(str(spelled)), spelled


def test_an_agent_file_tool_may_still_read_a_page_file() -> None:
    from kiro_crew.security import is_sensitive_path

    assert not is_sensitive_path(_page_file())


def test_the_sandbox_seals_the_folder_read_only_and_precreates_it() -> None:
    from kiro_crew import sandbox

    leaf = agent_panel.TEMPLATES_DIRNAME
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
    # The seal is a mount of the whole folder, so a page one level down is under it.
    sealed = [t for t in sandbox._CREW_READONLY_TARGETS if PurePath(t).name == leaf]
    assert sealed, "no read-only target names the folder"
    for target in sealed:
        below = PurePath(target, "my-board", "template.html")
        assert below.is_relative_to(PurePath(target))


def test_the_folder_name_refuses_an_alias_and_is_classified() -> None:
    from kiro_crew import sandbox

    leaf = agent_panel.TEMPLATES_DIRNAME
    assert leaf in sandbox._CREW_NO_ALIAS_LEAVES
    assert leaf in sandbox._CREW_CHILD_READABLE_LEAVES
    assert leaf not in sandbox._CREW_HIDDEN_LEAVES

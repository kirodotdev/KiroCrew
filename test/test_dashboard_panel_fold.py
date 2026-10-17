"""A dashboard page reads what its OWN crewmate published, and nothing else.

``panel_publish`` lands in the ``panel`` slot fold, which any page may bind as
``{"fold": "panel", "path": "data"}``. One slot can carry two crews, and the
fold's top level is whichever published last. So the page read resolves the
binding against the reading crewmate's own record, the panel read route's rule,
and never against the slot's newest.

These cases run the real crew log, the real fold and the real feed. Only the slot
lookup is stubbed, because which slot a crewmate maps to is the member routes'
own question and is tested there.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection
from kiro_crew.dashboard.handlers import member_dashboard as routes
from kiro_crew.dashboard_templates.manifest import parse_manifest

SLOT = "member-shared-crew"
UNIT = "acp-shared-crew"
SLUG = "shared-crew"
MINE = "Shared Crew"
THEIRS = "shared-crew"
NOBODY = "Quiet Crew"


@pytest.fixture(autouse=True)
def _env(tmp_path, _floor_monkeypatch):
    """An isolated home with the crew log on, and one slot every crewmate reads.

    On ``_floor_monkeypatch`` so a test body's ``monkeypatch.undo()`` cannot lift
    the isolation.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    projection.forget_slot_folds()
    _floor_monkeypatch.setattr(routes, "_dashboard_slot", lambda member, slug: SLOT)
    _floor_monkeypatch.setattr(routes, "_write_session", lambda slug, member: UNIT)
    CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent=THEIRS, slot=SLOT)
    yield
    crew_log_emit.reset_caches()
    projection.forget_slot_folds()


def _publish(crew: str, data: dict[str, Any], *, title: str = "board") -> None:
    """One ``panel/published`` line, keyed the way the publish route keys it."""
    crew_log_emit.on_panel_published(
        UNIT,
        {
            "template": "default",
            "data": data,
            "title": title,
            "crew": crew,
            "crew_key": agent_panel.crew_key(crew),
        },
    )
    assert crew_log_emit.flush(), "the publish line did not land"
    projection.forget_slot_folds()


def _manifest(**paths: str) -> Any:
    fields = {
        name: {"type": "object", "source": {"fold": "panel", "path": path}}
        for name, path in (paths or {"published": "data"}).items()
    }
    return parse_manifest(
        {
            "id": "panel-probe",
            "version": 1,
            "title": "Panel probe",
            "description": "Binds the panel fold.",
            "source": "builtin",
            "fields": fields,
        }
    )


def _page_values(member: str, manifest: Any = None) -> tuple[dict[str, Any], list[str]]:
    """What the page's script is handed: the read, masked and redacted."""
    read = routes.read_fields(SLUG, member, manifest or _manifest())
    return {k: routes._page_safe(v) for k, v in read.fields.items()}, read.missing


def test_a_publish_by_another_crew_on_a_shared_slot_never_reaches_this_page() -> None:
    _publish(MINE, {"mine": "first board"})
    # Published LAST, so it is the fold's top level: the record a naive binding reads.
    _publish(THEIRS, {"theirs": "second board"})

    fields, missing = _page_values(MINE)
    assert fields["published"] == {"mine": "first board"}, fields
    assert "second board" not in json.dumps(fields), "another crew's publish reached this page"
    assert not missing

    # The other crew still reads its own, so the rule is per owner, not "the oldest".
    theirs, _ = _page_values(THEIRS)
    assert theirs["published"] == {"theirs": "second board"}


def test_a_crewmate_that_never_published_reads_an_empty_object_not_the_newest() -> None:
    _publish(THEIRS, {"theirs": "second board"})

    fields, missing = _page_values(NOBODY)
    # Empty, and RESOLVED: an unresolved field raises the frame's stale band, and a
    # crewmate that has simply not published is not stale.
    assert fields["published"] == {}
    assert "published" not in missing


def test_a_root_session_page_reads_no_panel() -> None:
    """A session page is read with no crewmate, so no publish is its own."""
    _publish(THEIRS, {"theirs": "second board"})
    fields, _ = _page_values("")
    assert fields["published"] == {}


def test_the_other_owners_cannot_be_reached_through_another_path() -> None:
    """``owners`` holds every crew's record, so it must not resolve from a page."""
    _publish(MINE, {"mine": "first board"})
    _publish(THEIRS, {"theirs": "second board"})

    fields, missing = _page_values(MINE, _manifest(everyone="owners", published="data"))
    assert "everyone" in missing
    assert "second board" not in json.dumps(fields)


def test_the_ownership_digest_is_masked() -> None:
    digest = agent_panel.crew_key(MINE)
    assert "crew_key" in routes._MASKED_VALUE_KEYS
    assert routes._page_safe({"crew_key": digest, "data": {"n": 1}}) == {"data": {"n": 1}}

    _publish(MINE, {"mine": "first board"})
    fields, missing = _page_values(MINE, _manifest(digest="crew_key", published="data"))
    assert "digest" in missing, "a page could bind the ownership digest directly"
    assert digest not in json.dumps(fields)


def test_a_credential_in_a_published_line_never_reaches_the_page() -> None:
    """The publish route scrubs before it appends; this is the page's own pass.

    The line is appended straight to the log, past the publish route's scrubber,
    so the only thing between it and the page is the page read's redaction.
    """
    # Assembled at runtime so the diff carries no credential-shaped literal.
    token = "ghp" + "_" + "ZXAMPLEzxampleZXAMPLEzxampleZXAMPLE12"
    _publish(MINE, {"note": f"fetched with {token}", token: "a key that is the secret"})

    fields, _ = _page_values(MINE)
    blob = json.dumps(fields)
    assert token not in blob, f"a token reaches the page: {blob[:300]}"
    assert "fetched with" in fields["published"]["note"]


def test_the_page_shows_what_the_panel_route_shows_when_the_file_is_newer() -> None:
    """A publish whose crew-log line never landed is still in the file.

    The panel read route prefers this owner's file over the fold, so the page
    must too, or it shows an older publish than the route does.
    """
    _publish(MINE, {"cycle": "logged"})
    # The store's own write, with no crew-log line beside it.
    agent_panel.publish(SLUG, template="default", data={"cycle": "file only"}, crew=MINE)

    fields, _ = _page_values(MINE)
    assert fields["published"] == {"cycle": "file only"}


def test_another_crews_file_on_the_slug_never_reaches_this_page() -> None:
    """The file is keyed by slug alone, so its owner is checked like the route checks it."""
    agent_panel.publish(SLUG, template="default", data={"theirs": "file"}, crew=THEIRS)

    fields, _ = _page_values(MINE)
    assert fields["published"] == {}


def test_a_session_page_read_with_a_crew_name_shows_no_publish() -> None:
    """``dashboard_fields`` reads a root session's page with the caller's crew name.

    The page itself is read with no crew name, so the agent's view must match it.
    """
    _publish(MINE, {"mine": "first board"})
    agent_panel.publish(SLUG, template="default", data={"mine": "file"}, crew=MINE)

    read = routes.read_fields(SLUG, MINE, _manifest(), slot="chat-root-session")
    assert read.fields["published"] == {}

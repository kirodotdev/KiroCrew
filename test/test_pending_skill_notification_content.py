"""Content contract for the staged-skill bell notification.

``_pending_skill_notification`` is the payload builder behind the
``set_pending_staged_hook`` registration in the dashboard server. Its output is
user-facing (feed row, detail panel, action buttons), so the shape is pinned
here: the review deep-link must target the exact candidate, and it is the
notification's only action. The ``skills.approval_required`` setting is not
offered from here: updates and script-bearing candidates stage whatever it says,
so a shortcut to it on every staged candidate misled and nagged.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.server import _pending_skill_notification
from kiro_crew.notifications.bus import payload_from_legacy


def _info(**kw) -> dict:
    base = {
        "slug": "my-skill",
        "name": "My Skill",
        "kind": "new",
        "target": "",
        "description": "does a thing",
        "triggers": "when asked",
        "has_scripts": False,
    }
    base.update(kw)
    return base


def test_new_candidate_title_and_review_deep_link():
    title, body, review_url, actions = _pending_skill_notification(_info())
    assert title == "New skill awaiting review"
    assert body.startswith("**My Skill** — does a thing")
    assert review_url == "/capabilities?tab=skills&review=my-skill"
    assert actions[0] == {
        "id": "review-skill",
        "label": "Review skill",
        "url": review_url,
    }


def test_update_candidate_uses_target_and_update_label():
    title, body, review_url, actions = _pending_skill_notification(
        _info(kind="update", target="existing-skill")
    )
    assert title == "Skill update awaiting review"
    assert body.startswith("**existing-skill**")
    assert actions[0]["label"] == "Review update"


@pytest.mark.parametrize("has_scripts", [False, True])
@pytest.mark.parametrize("kind", ["new", "update"])
def test_review_link_is_the_only_action(kind: str, has_scripts: bool):
    """New prose-only, new with scripts, and updates (with or without scripts)
    all carry exactly one action: the review deep-link. No variant offers a
    shortcut to the approval setting."""
    is_update = kind == "update"
    _, _, review_url, actions = _pending_skill_notification(
        _info(kind=kind, target="existing-skill" if is_update else "",
              has_scripts=has_scripts)
    )
    assert actions == [
        {
            "id": "review-skill",
            "label": "Review update" if is_update else "Review skill",
            "url": review_url,
        }
    ]


def test_script_candidate_keeps_the_scripts_warning():
    _, body, _, _ = _pending_skill_notification(_info(has_scripts=True))
    assert "Bundles executable scripts" in body


def test_slug_is_percent_encoded_in_review_url():
    _, _, review_url, actions = _pending_skill_notification(_info(slug="a b&c"))
    assert review_url == "/capabilities?tab=skills&review=a%20b%26c"
    assert actions[0]["url"] == review_url


def test_triggers_line_included_when_present():
    _, body, _, _ = _pending_skill_notification(_info(triggers="deploy, release"))
    assert "**Triggers:** deploy, release" in body


@pytest.mark.parametrize("has_scripts", [False, True])
@pytest.mark.parametrize("kind", ["new", "update"])
def test_payload_passes_notification_validation(kind: str, has_scripts: bool):
    """The action must clear NotificationPayload.validate — the same gate
    state.notify() applies via payload_from_legacy — including the
    internal-URL check on every actions[].url."""
    title, body, review_url, actions = _pending_skill_notification(
        _info(kind=kind, target="existing-skill" if kind == "update" else "",
              has_scripts=has_scripts)
    )
    payload = payload_from_legacy(
        "skills", title, body, url=review_url, actions=actions
    )
    payload.validate()  # must not raise

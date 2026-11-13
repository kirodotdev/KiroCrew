"""A request no app token may make is refused with what to do instead.

``app_token_refusal`` builds the ``403 app_token_forbidden`` body for the owner's
question cards, a mobile sign-in link and two subagent-run actions. Its ``error``
must not read like the API scope refusal, which a ``permissions.api`` entry clears.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kiro_crew.dashboard.app_token_refusal import app_token_refusal
from kiro_crew.dashboard.handlers import ask_question


def test_the_refusal_names_the_action_and_says_no_permission_allows_it():
    response = app_token_refusal("do the thing", "Ask the owner.")

    assert response.status == 403
    body = json.loads(response.body)
    assert body == {
        "error": (
            "App tokens cannot do the thing, and no permission in app.json lets an app "
            "token do this. Ask the owner."
        ),
        "code": "app_token_forbidden",
    }


def test_the_question_card_refusal_points_at_the_per_slot_routes(monkeypatch):
    monkeypatch.setattr(ask_question, "sel", MagicMock())
    request = MagicMock()
    request.get = lambda key, default="": "ledger-lens" if key == "app" else default

    response = ask_question._deny_app_token(request, "ask_question_answer")

    assert response is not None
    body = json.loads(response.body)
    assert body["code"] == "app_token_forbidden"
    assert body["error"].startswith("App tokens cannot use the owner-wide /api/ask-question")
    assert "the /api/chat/slots/{slot}/question routes" in body["error"]
    assert "permissions.sessionApproval" in body["error"]
    assert "a permissions.api entry that covers them" in body["error"]
    # The old wording matched the API scope refusal word for word.
    assert "not permitted for this endpoint" not in body["error"]


def test_a_dashboard_caller_is_not_refused():
    request = MagicMock()
    request.get = lambda key, default="": default

    assert ask_question._deny_app_token(request, "ask_question") is None

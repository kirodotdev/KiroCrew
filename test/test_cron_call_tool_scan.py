"""``ctx.call_tool`` sends an argument exactly as the script wrote it, or not at all.

The argument scan judges every string in the arguments and refuses a call it would
change anywhere, naming the argument's path and never its value. An opaque id under an
id-named key and a resource name after a 12-digit account id, a CodeDeploy target's
CloudFormation-named application included, are identifiers, so they reach the server
byte-identical. ``kirocrew cron preview`` shares the scan; its leg is pinned in
``test_cron_preview_cmd.py``.
"""

from __future__ import annotations

import base64
import copy
import datetime
import json
from types import SimpleNamespace

import pytest

from kiro_crew.cron_script import ScriptContext
from kiro_crew.security import redact

# A Microsoft Graph message id: base64 of an Exchange id. The bare-secret pass reads the
# 99-character run before its first "-" as a key.
GRAPH_MESSAGE_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEUR"
    "BwAiIsqMbYjsT5e-T7KzowPTAAAAAAEMAAAiIsqMbYjsT5e-T7KzowPTAAAYbvZDAAA="
)
# A drive id: "b!" and base64url. The bare-secret pass reads the 64 characters after "b!".
DRIVE_ID = "b!kYWqRNAvL0OB7ByWOcmhn6cXgFRBPD9Kp0Ec0xRoqx3rH3oYKNrDTJVjFMwxR9zZ"
ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
# The Telegram rule reads "<digits>:<30 or more characters>" as a bot token.
ACCOUNT_RESOURCE = "123456789012:MyCompanyOrderProcessingServiceLambda:prod"
# A CodeDeploy target written as "<region>:<account>:<app>:<group>:CD". CloudFormation
# names the application and the group "<stack>-<logical id>-<suffix>".
CFN_APP = "ExampleAnonymizedMessagingService-CodeDeployApp-1ABCDEF2GHIJK"
CODEDEPLOY_TARGET = (
    f"eu-west-1:123456789012:{CFN_APP}:"
    "ExampleAnonymizedMessagingService-DeploymentGroup1-2KLMNOP3QRSTU:CD"
)
# Built from parts so secret scanners do not flag a literal token in the source.
GITHUB_TOKEN = "gh" + "p_" + "Zx9Qw" * 7 + "A"
BOT_SECRET = "AA" + "Hq7Tcv1vGWJxfSeofSAs0K5PALDs" + "aw3Xy"
# 40 characters of mixed case and digits, which the bare-secret pass reads as a key.
KEY_RUN = BOT_SECRET[2:] + "Q9mZt2B"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The arguments every call that reached a fake MCP server carried, in order."""
    received: list[dict] = []

    class FakeClient:
        def __init__(self, server: str, session_key: str = "") -> None:
            pass

        def call_tool(self, name: str, arguments: dict) -> str:
            received.append(arguments)
            return "ok"

        def is_running(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr("kiro_crew.cron_script.McpToolClient", FakeClient)
    return received


def _ctx() -> ScriptContext:
    return ScriptContext(job=SimpleNamespace(id="j1", message=""))


def test_the_fixtures_are_what_the_scan_rewrites() -> None:
    """Non-vacuity: redaction rewrites each identifier, so the scan must not judge by it."""
    for value in (GRAPH_MESSAGE_ID, DRIVE_ID, ACCOUNT_RESOURCE, CODEDEPLOY_TARGET):
        assert redact(value) != value, value
    assert redact(ITEM_ID) == ITEM_ID


@pytest.mark.parametrize(
    "args",
    [
        {"message_id": GRAPH_MESSAGE_ID},
        {"driveId": DRIVE_ID, "itemId": ITEM_ID},
        {"targetNames": [ACCOUNT_RESOURCE]},
        {"items": [{"id": GRAPH_MESSAGE_ID}], "messageIds": [GRAPH_MESSAGE_ID]},
        {"resource": ACCOUNT_RESOURCE, "MESSAGE_ID": GRAPH_MESSAGE_ID},
        {"targetNames": [CODEDEPLOY_TARGET, CODEDEPLOY_TARGET.replace("eu-", "us-gov-")]},
        {"resourceName": CODEDEPLOY_TARGET},
    ],
)
def test_identifiers_reach_the_server_byte_identical(sent: list[dict], args: dict) -> None:
    expected = copy.deepcopy(args)
    assert _ctx().call_tool("srv", "get", args) == "ok"
    assert sent == [expected]


@pytest.mark.parametrize(
    ("args", "path"),
    [
        ({"text": f"rotate deploy key {GITHUB_TOKEN} tonight"}, "args.text"),
        ({"cursor": GITHUB_TOKEN}, "args.cursor"),
        ({"page": {"items": ["ok", f"see {GITHUB_TOKEN}"]}}, r"args.page.items\[1\]"),
        ({"authorization": "Bearer " + "Zx9Qw" * 8}, "args.authorization"),
        # An opaque id is an identifier only under a key that names one.
        ({"body": GRAPH_MESSAGE_ID}, "args.body"),
        ({"text": f"see {GRAPH_MESSAGE_ID} here"}, "args.text"),
        # Under an id-named key only the bare-secret pass stands down.
        ({"message_id": "AAMk" + GITHUB_TOKEN}, "args.message_id"),
        ({"driveId": "b!" + GITHUB_TOKEN}, "args.driveId"),
        # A bot id is 6 to 10 digits, so these stay bot tokens whatever follows the ":".
        ({"text": f"bot 1234567890:{BOT_SECRET}"}, "args.text"),
        ({"text": f"bot 110201543:{BOT_SECRET}"}, "args.text"),
        ({"text": "bot 1234567890:MyCompanyOrderProcessingServiceLambda"}, "args.text"),
        # An account id is 12 digits that are not a bot id padded with zeros, and only a
        # resource name after one is an identifier: a token behind a padded, glued or real
        # account id, in an ARN or not, stays a token.
        ({"text": f"bot 000110201543:{BOT_SECRET}"}, "args.text"),
        ({"text": f"bot 999110201543:{BOT_SECRET}"}, "args.text"),
        ({"text": f"token 123456789012:{BOT_SECRET}"}, "args.text"),
        ({"topicArn": f"arn:aws:sns:us-west-2:000110201543:{BOT_SECRET}"}, "args.topicArn"),
        ({"topicArn": f"arn:aws:sns:us-west-2:123456789012:{BOT_SECRET}"}, "args.topicArn"),
        ({"name": "000110201543:MyCompanyOrderProcessingServiceLambda:prod"}, "args.name"),
        ({"name": "1234567890123:MyCompanyOrderProcessingServiceLambda:prod"}, "args.name"),
        # A CloudFormation name is a resource name only where a CodeDeploy target puts one:
        # after a region segment and an account id, with another ":" after it.
        ({"targetNames": [f"eu-west-1:999110201543:{BOT_SECRET}:CD"]}, r"args.targetNames\[0\]"),
        ({"target": f"eu-west-1:000110201543:{CFN_APP}:CD"}, "args.target"),
        ({"target": f"eu-west-1:1234567890123:{CFN_APP}:CD"}, "args.target"),
        ({"target": f"123456789012:{CFN_APP}:CD"}, "args.target"),
        ({"target": f"xeu-west-1:123456789012:{CFN_APP}:CD"}, "args.target"),
        ({"target": f"eu-west-1:123456789012:{CFN_APP}"}, "args.target"),
        ({"target": "eu-west-1:123456789012:ExampleMessagingApp-1ABCDEF2GHIJK:CD"}, "args.target"),
        ({"target": f"eu-west-1:123456789012:Ex-{KEY_RUN}-App-1ABCDEF2GHIJK:CD"}, "args.target"),
        # A credential behind an account id is judged as if the account id were not there.
        ({"name": "123456789012:" + GITHUB_TOKEN}, "args.name"),
        ({"name": "123456789012:" + base64.b64encode(GITHUB_TOKEN.encode()).decode()}, "args.name"),
    ],
)
def test_a_call_the_scan_would_change_is_refused_by_path_and_never_sent(
    sent: list[dict], monkeypatch: pytest.MonkeyPatch, args: dict, path: str
) -> None:
    ctx = _ctx()
    audited = []
    monkeypatch.setattr(ctx, "_audit_tool_call", lambda *record: audited.append(record))
    with pytest.raises(ValueError, match=rf"not sent: {path} reads as a credential") as refused:
        ctx.call_tool("srv", "post", args)
    message = str(refused.value)
    for secret in (GITHUB_TOKEN, BOT_SECRET, GRAPH_MESSAGE_ID, KEY_RUN):
        assert secret not in message
    assert audited == [("srv", "post", "error", message)]
    assert sent == []


def test_a_credential_shaped_key_is_refused_without_echoing_it(sent: list[dict]) -> None:
    with pytest.raises(ValueError, match="reads as a credential") as refused:
        _ctx().call_tool("srv", "post", {GITHUB_TOKEN: "value"})
    assert GITHUB_TOKEN not in str(refused.value)
    assert sent == []


@pytest.mark.parametrize("args", [{"since": datetime.date(2026, 11, 8)}, {"tags": {"a"}}])
def test_an_argument_json_cannot_hold_fails_before_the_kept_server_is_touched(
    monkeypatch: pytest.MonkeyPatch, args: dict
) -> None:
    """The error is the script's, so the server kept from the last call stays signed in."""
    lifecycle: list[str] = []

    class SerializingClient:
        def __init__(self, server: str, session_key: str = "") -> None:
            lifecycle.append("start")

        def call_tool(self, name: str, arguments: dict) -> str:
            json.dumps(arguments)
            return "ok"

        def is_running(self) -> bool:
            return True

        def close(self) -> None:
            lifecycle.append("close")

    monkeypatch.setattr("kiro_crew.cron_script.McpToolClient", SerializingClient)
    ctx = _ctx()
    assert ctx.call_tool("srv", "get", {"n": 1}) == "ok"
    with pytest.raises(TypeError, match="not JSON serializable"):
        ctx.call_tool("srv", "get", args)
    assert ctx.call_tool("srv", "get", {"n": 2}) == "ok"
    assert lifecycle == ["start"]

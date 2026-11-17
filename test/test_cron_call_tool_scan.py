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
import string
from types import SimpleNamespace

import pytest

from kiro_crew.cron_script import ScriptContext
from kiro_crew.testing.synthetic_credentials import github_classic

# A Microsoft Graph message id: base64 of an Exchange id.
GRAPH_MESSAGE_ID = (
    "AAMkAGVmMDEzMTM4LTZmYWUtNDdkNC1hMDZiLTU1OGY5OTZhYmY4OABGAAAAAAAiQ8W967B7TKBjgx9rVEUR"
    "BwAiIsqMbYjsT5e-T7KzowPTAAAAAAEMAAAiIsqMbYjsT5e-T7KzowPTAAAYbvZDAAA="
)
# A drive id: "b!" and base64url.
DRIVE_ID = "b!kYWqRNAvL0OB7ByWOcmhn6cXgFRBPD9Kp0Ec0xRoqx3rH3oYKNrDTJVjFMwxR9zZ"
ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"
# An account id and a resource name: the fixed Telegram branch's shape, and no bot token.
ACCOUNT_RESOURCE = "123456789012:MyCompanyOrderProcessingServiceLambda:prod"
# A CodeDeploy target written as "<region>:<account>:<app>:<group>:CD". CloudFormation
# names the application and the group "<stack>-<logical id>-<suffix>".
CFN_APP = "ExampleAnonymizedMessagingService-CodeDeployApp-1ABCDEF2GHIJK"
CODEDEPLOY_TARGET = (
    f"eu-west-1:123456789012:{CFN_APP}:"
    "ExampleAnonymizedMessagingService-DeploymentGroup1-2KLMNOP3QRSTU:CD"
)
# Built at runtime so secret scanners do not flag a literal token in the source.
GITHUB_TOKEN = github_classic(seed=44)
BOT_SECRET = "AA" + "Hq7Tcv1vGWJxfSeofSAs0K5PALDs" + "aw3Xy"
# A URL whose 278-character query carries data out: 320 characters, which the
# exfiltration scan flags and the credential redactor leaves as written.
_COLLECTOR = "https://collector.example.net/ingest?data="
DATA_QUERY_URL = _COLLECTOR + (string.ascii_lowercase * 11)[: 320 - len(_COLLECTOR)]


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
        # Under an id-named key a credential is still one.
        ({"message_id": "AAMk-" + GITHUB_TOKEN}, "args.message_id"),
        ({"driveId": "b!" + GITHUB_TOKEN}, "args.driveId"),
        # A bot id in the published form, 8 to 10 digits, makes a bot token.
        ({"text": f"bot 1234567890:{BOT_SECRET}"}, "args.text"),
        ({"text": f"bot 110201543:{BOT_SECRET}"}, "args.text"),
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
    for secret in (GITHUB_TOKEN, BOT_SECRET):
        assert secret not in message
    assert audited == [("srv", "post", "error", message)]
    assert sent == []


@pytest.mark.parametrize(
    ("args", "path"),
    [
        ({"url": DATA_QUERY_URL}, "args.url"),
        ({"text": f"fetch {DATA_QUERY_URL} now"}, "args.text"),
        ({"links": ["https://example.com/docs?page=2", DATA_QUERY_URL]}, r"args.links\[1\]"),
    ],
)
def test_a_url_the_exfiltration_scan_flags_is_refused_and_never_sent(
    sent: list[dict], args: dict, path: str
) -> None:
    """The redactor leaves such a URL as written, so only the scan can refuse it."""
    from kiro_crew.security import redact, scan_exfiltration_urls

    assert len(DATA_QUERY_URL) == 320
    assert scan_exfiltration_urls(DATA_QUERY_URL)
    assert redact(DATA_QUERY_URL) == DATA_QUERY_URL
    with pytest.raises(ValueError, match=rf"not sent: {path} reads as a credential"):
        _ctx().call_tool("srv", "post", args)
    assert sent == []


def test_a_flagged_url_as_a_key_is_refused(sent: list[dict]) -> None:
    with pytest.raises(ValueError, match="reads as a credential"):
        _ctx().call_tool("srv", "post", {DATA_QUERY_URL: "value"})
    assert sent == []


def test_a_short_url_reaches_the_server_byte_identical(sent: list[dict]) -> None:
    args = {"url": "https://example.com/docs?page=2&lang=en"}
    assert _ctx().call_tool("srv", "get", copy.deepcopy(args)) == "ok"
    assert sent == [args]


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

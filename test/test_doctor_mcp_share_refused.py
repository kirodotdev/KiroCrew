"""``kirocrew doctor`` names servers set to share whose launch is refused.

A server in ``mcp_gateway.stub_servers`` whose launch approval is refused runs one
copy per chat while config says it is shared. These tests pin that doctor says so,
names the reason and the approve path, and counts it as an issue -- and that it
stays quiet about a refusal for a server that is not on the share list. A changed
refusal whose approved launches all still run beside an added one is named as a
new launch; anything less complete, or an approved launch shown with a redaction
tag, keeps "command changed".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.doctor_checks import mcp
from kiro_crew.mcp_gateway import launch_approval
from kiro_crew.security import CREDENTIAL_REDACTION_TAGS
from kiro_crew.security.exfil import EXFILTRATION_REDACTION_TAG_PREFIX


def _cfg(*, enabled: bool = True, stub: tuple[str, ...] = ("alpha-mcp", "beta-mcp")):
    return SimpleNamespace(mcp_gateway=SimpleNamespace(enabled=enabled, stub_servers=list(stub)))


@pytest.fixture
def refused(monkeypatch):
    store: dict[str, dict] = {}
    monkeypatch.setattr(launch_approval, "refused_servers", lambda path=None: store)
    monkeypatch.setattr(launch_approval, "approvals_store_problem", lambda path=None: None)
    return store


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    """The approval store path, pointed at a tmp file the test writes."""
    path = tmp_path / "approvals.json"
    monkeypatch.setattr(launch_approval, "approvals_path", lambda home=None: path)
    return path


def test_refused_share_is_reported_as_an_issue_with_the_approve_path(refused, capsys):
    refused["alpha-mcp"] = {"reason": launch_approval.REFUSED_UNAPPROVED}
    refused["beta-mcp"] = {"reason": launch_approval.REFUSED_CHANGED}
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(), issues)

    out = capsys.readouterr().out
    assert "mcp sharing: ⚠ not shared, one copy per chat:" in out
    assert "alpha-mcp (not approved yet)" in out
    assert "beta-mcp (command changed since it was approved)" in out
    assert "/mcp?view=sharing" in out
    flat = " ".join(out.split())
    assert "Open MCP Servers, the Sharing view" in flat
    assert "Developer Mode" not in flat
    # Unflattened on purpose: the command must stay on one line to be pasteable.
    assert "kirocrew restart" in out
    assert issues == ["2 MCP server(s) set to share are not shared: launch not approved"]


def _changed(**over):
    """A complete changed-launch refusal whose approved launch is ``/opt/a --one``."""
    record = {
        "reason": launch_approval.REFUSED_CHANGED,
        "complete": True,
        "expected_launch": "pinned-identity",
        "commands": [["/opt/a", "--one"]],
        "envs": [[]],
        "approved_commands": [["/opt/a", "--one"]],
        "approved_envs": [[]],
    }
    record.update(over)
    return record


#: The approved launch still runs, and a second agent added its own.
_ADDED = {"commands": [["/opt/a", "--one"], ["/opt/a", "--two"]], "envs": [[], []]}


def test_a_launch_added_beside_the_approved_one_is_named_as_new(refused, capsys):
    refused["alpha-mcp"] = _changed(**_ADDED)
    refused["beta-mcp"] = _changed(commands=[["/opt/a", "--two"]])
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(), issues)

    out = capsys.readouterr().out
    assert "alpha-mcp (new launch not approved yet)" in out
    assert "beta-mcp (command changed since it was approved)" in out
    assert issues == ["2 MCP server(s) set to share are not shared: launch not approved"]


#: Display tags that read the same for two different values.
_TAG = "--token=[REDACTED: credential]"
_URL_TAG = "API_URL=[REDACTED: suspicious URL to example.invalid]"


def test_every_display_redaction_tag_starts_with_the_head_the_rule_reads():
    tags = (*CREDENTIAL_REDACTION_TAGS, EXFILTRATION_REDACTION_TAG_PREFIX)
    assert tags and all(tag.startswith(mcp._REDACTION_TAG_HEAD) for tag in tags)


#: Two approved launches, as after one Approve update on a row two agents declared.
_TWO_APPROVED = {
    "approved_commands": [["/opt/a", "--one"], ["/opt/a", "--two"]],
    "approved_envs": [[], []],
}


def test_a_launch_added_beside_two_approved_ones_is_named_as_new(refused, capsys):
    refused["alpha-mcp"] = _changed(
        **_TWO_APPROVED,
        commands=[["/opt/a", "--one"], ["/opt/a", "--two"], ["/opt/a", "--three"]],
        envs=[[], [], []],
    )

    mcp._doctor_mcp_share_refused(_cfg(stub=("alpha-mcp",)), [])

    assert "alpha-mcp (new launch not approved yet)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "over",
    [
        pytest.param(
            {"commands": [["/opt/a", "--two"], ["/opt/a", "--three"]], "envs": [[], []]},
            id="approved-changed-and-another-added",
        ),
        pytest.param(
            {
                **_TWO_APPROVED,
                "commands": [["/opt/a", "--one"], ["/opt/a", "--three"]],
                "envs": [[], []],
            },
            id="one-of-two-approved-changed",
        ),
        pytest.param(
            {**_ADDED, "envs": [["MODE=new"], []], "approved_envs": [["MODE=old"]]},
            id="approved-settings-changed-beside-an-added-launch",
        ),
        pytest.param({}, id="nothing-added"),
        pytest.param({**_ADDED, "approved_envs": None}, id="no-approved-env-displays"),
        pytest.param({**_ADDED, "envs": [[]]}, id="live-displays-do-not-pair-up"),
        pytest.param({**_ADDED, "complete": False}, id="partial"),
        pytest.param({**_ADDED, "expected_launch": None}, id="no-identity"),
        pytest.param(
            {**_ADDED, "commands": [["/opt/a", "--one"], "/opt/a --two"]},
            id="malformed-display-line",
        ),
        pytest.param(
            {
                "commands": [["/opt/a", _TAG], ["/opt/a", "--two"]],
                "envs": [[], []],
                "approved_commands": [["/opt/a", _TAG]],
            },
            id="approved-command-shows-a-redacted-credential",
        ),
        pytest.param(
            {**_ADDED, "envs": [[_URL_TAG], []], "approved_envs": [[_URL_TAG]]},
            id="approved-env-shows-a-redacted-url",
        ),
    ],
)
def test_anything_short_of_an_added_launch_keeps_command_changed(refused, capsys, over):
    record = _changed(**over)
    refused["alpha-mcp"] = {k: v for k, v in record.items() if v is not None}

    mcp._doctor_mcp_share_refused(_cfg(stub=("alpha-mcp",)), [])

    assert "alpha-mcp (command changed since it was approved)" in capsys.readouterr().out


def test_a_never_approved_server_with_an_extra_launch_stays_not_approved_yet(refused, capsys):
    refused["alpha-mcp"] = _changed(**_ADDED, reason=launch_approval.REFUSED_UNAPPROVED)

    mcp._doctor_mcp_share_refused(_cfg(stub=("alpha-mcp",)), [])

    assert "alpha-mcp (not approved yet)" in capsys.readouterr().out


def test_an_alias_matched_share_reads_its_own_refusal_record(refused, capsys):
    refused["playwright-mcp"] = _changed(**_ADDED)

    mcp._doctor_mcp_share_refused(_cfg(stub=("npm:@playwright/mcp",)), [])

    assert "npm:@playwright/mcp (new launch not approved yet)" in capsys.readouterr().out


def test_refusal_for_a_server_no_longer_shared_is_ignored(refused, capsys):
    refused["gone-mcp"] = {"reason": launch_approval.REFUSED_UNAPPROVED}
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(stub=("alpha-mcp",)), issues)

    out = capsys.readouterr().out
    assert "gone-mcp" not in out
    assert "mcp sharing: ✅ 1 server(s) set to share, none waiting for approval" in out
    assert issues == []


def test_slash_named_server_matches_the_refusal_recorded_under_its_alias(refused, capsys):
    # The rewriter admits `npm:@playwright/mcp` as `playwright-mcp`, so that is
    # the key its refusal is stored under.
    refused["playwright-mcp"] = {"reason": launch_approval.REFUSED_UNAPPROVED}
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(stub=("npm:@playwright/mcp",)), issues)

    assert "npm:@playwright/mcp (not approved yet)" in capsys.readouterr().out
    assert len(issues) == 1


def test_refused_shares_keys_by_configured_name():
    refused = {
        "playwright-mcp": {"reason": launch_approval.REFUSED_UNAPPROVED},
        "alpha-mcp": {"reason": launch_approval.REFUSED_CHANGED},
        "gone-mcp": {"reason": launch_approval.REFUSED_UNAPPROVED},
    }
    assert launch_approval.refused_shares(
        ["npm:@playwright/mcp", "alpha-mcp", "beta-mcp"], refused
    ) == {
        "npm:@playwright/mcp": launch_approval.REFUSED_UNAPPROVED,
        "alpha-mcp": launch_approval.REFUSED_CHANGED,
    }


def test_refused_share_records_keys_the_whole_record_by_configured_name():
    record = {"reason": launch_approval.REFUSED_CHANGED, "commands": [["/opt/a"]]}
    refused = {"playwright-mcp": record, "gone-mcp": {"reason": launch_approval.REFUSED_UNAPPROVED}}
    assert launch_approval.refused_share_records(["npm:@playwright/mcp", "beta-mcp"], refused) == {
        "npm:@playwright/mcp": record
    }


def test_unknown_reason_is_still_reported_by_its_raw_name(refused, capsys):
    refused["alpha-mcp"] = {"reason": "some_future_reason"}
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(), issues)

    assert "alpha-mcp (some_future_reason)" in capsys.readouterr().out
    assert len(issues) == 1


@pytest.mark.parametrize("cfg", [_cfg(enabled=False), _cfg(stub=())])
def test_silent_when_nothing_is_set_to_share(refused, capsys, cfg):
    refused["alpha-mcp"] = {"reason": launch_approval.REFUSED_UNAPPROVED}
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(cfg, issues)

    assert capsys.readouterr().out == ""
    assert issues == []


def test_unreadable_approval_store_is_named_not_raised(store_file, monkeypatch, capsys):
    def boom(path=None):
        raise OSError("denied")

    monkeypatch.setattr(launch_approval, "refused_servers", boom)
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(), issues)

    assert "could not read launch approvals (OSError)" in capsys.readouterr().out
    assert issues == []


@pytest.mark.parametrize(
    ("write", "reason"),
    [
        # The OS picks the error class for reading a directory (IsADirectoryError
        # on POSIX, PermissionError on Windows), so only the prefix is pinned.
        (lambda p: p.mkdir(), "unreadable ("),
        (lambda p: p.write_text("{not json", encoding="utf-8"), "not valid JSON"),
        (lambda p: p.write_bytes(b"\xff\xfe{}"), "not valid UTF-8"),
        (lambda p: p.write_text("[]", encoding="utf-8"), "not a JSON object"),
        (
            lambda p: p.write_text('{"version": -1}', encoding="utf-8"),
            "written in a format this version does not read",
        ),
    ],
)
def test_unreadable_or_corrupt_store_never_reports_healthy(store_file, capsys, write, reason):
    write(store_file)
    issues: list[str] = []

    mcp._doctor_mcp_share_refused(_cfg(), issues)

    out = capsys.readouterr().out
    assert f"mcp sharing: ⚠ cannot read launch approvals: {reason}" in out
    assert "none waiting for approval" not in out
    assert issues == ["MCP launch approvals cannot be read: sharing state unknown"]


def test_absent_store_is_not_a_read_problem(store_file):
    assert launch_approval.approvals_store_problem() is None
    store_file.write_text("  \n", encoding="utf-8")
    assert launch_approval.approvals_store_problem() is None


def test_store_problem_check_leaves_launch_loading_unchanged(store_file):
    store_file.write_text("{not json", encoding="utf-8")

    assert launch_approval.approvals_store_problem() == "not valid JSON"
    assert launch_approval.load_approvals().approved == {}

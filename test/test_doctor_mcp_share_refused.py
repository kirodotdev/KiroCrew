"""``kirocrew doctor`` names servers set to share whose launch is refused.

A server in ``mcp_gateway.stub_servers`` whose launch approval is refused runs one
copy per chat while config says it is shared. These tests pin that doctor says so,
names the reason and the approve path, and counts it as an issue -- and that it
stays quiet about a refusal for a server that is not on the share list.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.doctor_checks import mcp
from kiro_crew.mcp_gateway import launch_approval


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
    assert "/developer?tab=mcp-pool" in out
    assert "Developer Mode does not need" in out
    assert "kirocrew restart" in out
    assert issues == ["2 MCP server(s) set to share are not shared: launch not approved"]


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

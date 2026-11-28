"""``local_review.py`` briefs each lane on a model the account is served.

A review subagent spawned on a model the account is not served runs on the
account's default instead, and its verdict is read as the pinned model's. So the
script checks each profile pin against the account's model listing, moves an
unserved pin to the first served model in its ``model_tier``, and exits 2 when the
tier has none.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import test_prepare_pr_local_review as _lr

PROFILE_MODELS = _lr.PROFILE_MODELS
local_review = _lr.local_review
# The shared repo fixtures, bound under their own names so pytest finds them here.
_parity_repo_template = _lr._parity_repo_template
parity_repo = _lr.parity_repo


def test_model_drift_is_reported_not_swallowed():
    notes = local_review._model_note("openai.gpt-9.9-nova", "gpt-5.6-sol")
    assert notes and "MODEL DRIFT" in notes[0]


def test_model_drift_warns_on_a_truncated_local_pin():
    """CI ids gain provider prefixes, never trailing characters - so a prefix-
    tolerant test must still reject a local pin that stops short."""
    assert local_review._model_note("us.anthropic.claude-opus-4-8", "claude-opus-4.8") == []
    assert local_review._model_note("openai.gpt-5.6-sol", "gpt-5.6-sol") == []
    truncated = local_review._model_note("us.anthropic.claude-opus-4-8", "claude-opus-4")
    assert truncated and "MODEL DRIFT" in truncated[0]


@pytest.mark.parametrize(
    ("pin", "tier", "served", "expected", "note"),
    [
        ("gpt-6.1-sol", "gpt-6.x", ["claude-opus-5.5", "gpt-6.1-sol"], "gpt-6.1-sol", None),
        (
            "gpt-6.1-sol",
            "gpt-6.x",
            ["gpt-60-max", "gpt-6.2-astra", "gpt-6.0"],
            "gpt-6.2-astra",
            "MODEL FALLBACK",
        ),
        (
            "claude-opus-5.5",
            "claude-sonnet-5.5",
            ["claude-sonnet-5.5"],
            "claude-sonnet-5.5",
            "MODEL FALLBACK",
        ),
        ("gpt-6.1-sol", "gpt-6.x", None, "gpt-6.1-sol", "SERVED MODEL UNVERIFIED"),
    ],
    ids=["pin-served", "first-in-tier", "exact-tier", "no-listing"],
)
def test_a_lane_runs_on_its_pin_or_the_first_served_model_in_its_tier(
    pin, tier, served, expected, note
):
    model, notes = local_review.resolve_served_model("gpt", pin, tier, served)
    assert model == expected
    if note is None:
        assert notes == []
    else:
        assert len(notes) == 1 and notes[0].startswith(note)
        assert pin in notes[0]


def test_a_tier_with_no_served_model_fails_loudly_naming_what_was_served():
    with pytest.raises(local_review.UnservedModel) as exc:
        local_review.resolve_served_model(
            "gpt", "gpt-6.1-sol", "gpt-6.x", ["claude-opus-5.5", "gpt-5.6-sol"]
        )
    message = str(exc.value)
    for part in ("gpt-6.1-sol", "gpt-6.x", "gpt-5.6-sol"):
        assert part in message


def _listing(monkeypatch, *, rc=0, stdout="", raises=None):
    monkeypatch.setattr(local_review._tool_argv, "resolve", lambda args: (list(args), None))

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["kiro-cli", "chat", "--list-models"]
        assert kwargs["timeout"] == local_review._LIST_MODELS_TIMEOUT_SECS
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, rc, stdout, "")

    monkeypatch.setattr(local_review.subprocess, "run", fake_run)


def test_the_listing_reads_kiro_cli_ids_in_order_without_auto(monkeypatch):
    rows = [{"model_id": m} for m in ("auto", "claude-opus-5.5", "gpt-5.6-sol")]
    _listing(monkeypatch, stdout=json.dumps({"models": rows + ["junk"]}))
    assert local_review.list_served_models() == ["claude-opus-5.5", "gpt-5.6-sol"]


@pytest.mark.parametrize(
    "kw",
    [
        {"rc": 1},
        {"stdout": "not json"},
        {"stdout": json.dumps({"models": "x"})},
        {"stdout": json.dumps([1])},
        {"raises": subprocess.TimeoutExpired("kiro-cli", 60)},
        {"raises": OSError("gone")},
    ],
    ids=["exit", "bad-json", "bad-shape", "not-an-object", "timeout", "oserror"],
)
def test_an_unreadable_listing_is_none_never_a_guess(monkeypatch, kw):
    _listing(monkeypatch, **kw)
    assert local_review.list_served_models() is None


def test_a_missing_kiro_cli_is_an_unreadable_listing(monkeypatch):
    monkeypatch.setattr(
        local_review._tool_argv, "resolve", lambda args: (None, (127, "", "not found"))
    )
    assert local_review.list_served_models() is None


def _cli(parity_repo, tmp_path, served):
    return local_review.main(
        [
            "--worktree",
            str(parity_repo),
            "--base",
            "main",
            "--out-dir",
            str(tmp_path / "out"),
            "--stage-dir",
            str(tmp_path / "stage"),
            "--served-models",
            served,
            "--json",
        ]
    )


def test_cli_briefs_a_lane_on_the_served_tier_model_and_says_so(parity_repo, tmp_path, capsys):
    capsys.readouterr()
    assert _cli(parity_repo, tmp_path, "claude-opus-5.5,gpt-6.2-astra") == 0
    summary = json.loads(capsys.readouterr().out)
    gpt = next(lane for lane in summary["lanes"] if lane["name"] == "gpt")
    assert gpt["profile_model"] == PROFILE_MODELS["gpt"]["model"]
    assert gpt["local_model"] == "gpt-6.2-astra"
    assert any(w.startswith("MODEL FALLBACK") for w in gpt["warnings"])
    brief = Path(summary["tasks"]["gpt"]).read_text(encoding="utf-8")
    assert "- Model: `gpt-6.2-astra`" in brief
    assert "drop to" not in brief


def test_cli_exits_2_when_a_lane_has_no_served_model(parity_repo, tmp_path, capsys):
    capsys.readouterr()
    assert _cli(parity_repo, tmp_path, "claude-opus-5.5,gpt-5.6-sol") == local_review.EXIT_ENV
    err = capsys.readouterr().err
    assert PROFILE_MODELS["gpt"]["model"] in err and "gpt-5.6-sol" in err
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_cli_briefs_every_lane_on_the_chats_model_when_the_dispatch_cannot_pin(
    parity_repo, tmp_path, capsys
):
    """An engine sub-agent takes no model, so no pin is checked and none can stop the run."""
    capsys.readouterr()
    args = ["--worktree", str(parity_repo), "--base", "main", "--model-inherited", "--json"]
    args += ["--out-dir", str(tmp_path / "out"), "--stage-dir", str(tmp_path / "stage")]
    assert local_review.main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    for lane in summary["lanes"]:
        assert lane["model_inherited"] is True
        assert any(w.startswith("MODEL INHERITED") for w in lane["warnings"])
        brief = Path(summary["tasks"][lane["name"]]).read_text(encoding="utf-8")
        assert "- Model: the dispatching chat's own" in brief


def test_the_two_model_sources_cannot_be_combined(parity_repo, capsys):
    with pytest.raises(SystemExit):
        local_review.main(["--model-inherited", "--served-models", "gpt-6.1-sol"])

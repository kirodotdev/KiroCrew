"""Shared loaders and fixtures for the `pr_findings.py --rounds` tests."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
    / "pr_findings.py"
)
SCRIPTS_DIR = SCRIPT.parent
HEADS = ["1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40, "6" * 40, "7" * 40]
CLASS_A = "class: a hook can come from a source other than core.hooksPath\n"


def _load() -> ModuleType:
    return load_skill_script("prepare_pr_findings_rounds", SCRIPT)


def _contract() -> ModuleType:
    return load_skill_script("prepare_pr_contract_fences", SCRIPTS_DIR / "_review_contract.py")


def _disp(
    cid: int, target: str, head: str, span: str | None, extra: str = "", word: str = "fixed"
) -> dict:
    marker = "<!-- ai-review-disposition target={} head={} -->\n".format(target, head)
    claim = (
        "**{}** — span={}\n".format(word, span) if span else "**{}** — a watch item\n".format(word)
    )
    body = marker + claim + extra + "\n> rationale line naming span=deadbeef0000 as evidence only\n"
    return {"id": cid, "body": body, "user": {"login": "writer"}}


def _wire(
    module: ModuleType,
    monkeypatch,
    comments: list[dict],
    on_head: set[str] | None = None,
    pushes: int | None = 0,
) -> None:
    """Serve the dispositions, the spans on the current head (None: unreadable, so present)
    and the PR's pushes after a blocked head (None: unreadable)."""
    monkeypatch.setattr(module, "current_head_spans", lambda repo, number, head: on_head)
    monkeypatch.setattr(module, "pr_blocked_pushes", lambda repo, number, since="": pushes)
    monkeypatch.setattr(module, "fetch_disposition_comments", lambda repo, number: comments)
    monkeypatch.setattr(
        module,
        "writer_disposition_records",
        lambda repo, cs: [module.parse_disposition_record(c) for c in cs],
    )

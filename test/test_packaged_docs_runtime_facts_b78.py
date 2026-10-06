"""Pin runtime facts the packaged docs state against the code that owns them.

The docs under ``src/kiro_crew/docs`` ship with the package and are read by
running agents, so a stale sentence there is wrong runtime guidance. Each test
reads the owning source of truth and checks the doc agrees with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src" / "kiro_crew" / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_decisions_doc_states_every_local_model_download_size() -> None:
    from kiro_crew.decisions.local_models import LOCAL_MODELS

    text = _doc("decisions.md")
    for model in LOCAL_MODELS:
        gb = model.download_bytes / 1e9
        shown = f"{gb:.2f}" if gb < 1 else f"{gb:.1f}"
        assert f"about {shown} GB" in text, (model.id, shown)


def test_decisions_doc_documents_the_wake_judge_providers() -> None:
    from kiro_crew.config.sections import JUDGE_PROVIDERS

    text = _doc("decisions.md")
    assert "decisions.nudge_wake.provider" in text
    for provider in JUDGE_PROVIDERS:
        assert f"| `{provider}`" in text, provider


def test_feishu_doc_names_only_registered_security_subcommands() -> None:
    cli = (ROOT / "src" / "kiro_crew" / "cli.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'sec_sub\.add_parser\(\s*"([a-z-]+)"', cli))
    assert registered, "security subcommands not found in cli.py"
    named = set(re.findall(r"`kirocrew security ([a-z-]+)`", _doc("feishu-integration.md")))
    assert named and named <= registered, named - registered


def test_index_settings_table_uses_the_dashboard_tab_labels() -> None:
    locale = json.loads(
        (ROOT / "website" / "src" / "i18n" / "locales" / "en.manual.json").read_text(
            encoding="utf-8"
        )
    )
    labels = {tab["label"] for tab in locale["settings"]["tabs"].values()}
    text = _doc("index.md")
    section = text.split("## Settings reference", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line]
    names = {row.split("|")[1].strip() for row in rows[1:]}
    # Privacy's label lives outside settings.tabs, and Webhooks/Secrets carry
    # their own keys there; every other row must be a real tab label.
    unknown = names - labels - {"Privacy", "Webhooks", "Secrets"}
    assert not unknown, unknown
    for label in ("Import / Export", "Agent Harness", "OAuth Apps"):
        assert label in names, label

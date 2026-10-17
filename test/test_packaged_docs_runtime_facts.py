"""Pin packaged-doc claims that the agent reads at runtime to the code they describe.

``src/kiro_crew/docs`` ships with the package and is read by running agents, so a
claim there that drifts from the code misleads an agent, not only a human. Each test
here pairs one sentence or table cell with the code fact it states.
"""

from pathlib import Path

import pytest

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.computer_use.backend import UnsupportedBackend
from kiro_crew.computer_use.linux_driver import LinuxBackend
from kiro_crew.dashboard.upload_destination import DOCUMENT_CHANNELS
from kiro_crew.providers.mirrors import mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition

DOCS = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("backend", "disposition"),
    [
        (ACP_BACKEND_GOOSE, Disposition.TRANSLATED),
        (ACP_BACKEND_OPENCODE, Disposition.TRANSLATED),
        (ACP_BACKEND_CLAUDE, Disposition.NO_CHANNEL),
        (ACP_BACKEND_CODEX, Disposition.NO_CHANNEL),
    ],
)
def test_hooks_disposition_per_mirror_matches_the_spec_fields_page(backend, disposition):
    assert mirror_for(backend).rulings()[Concern.HOOKS].disposition is disposition
    doc = _read("agent-spec-fields.md")
    assert "`hooks` is `no-channel` on all four" not in doc
    assert (
        "`hooks` is `translated` on `goose` and `opencode`, where Crew's turn loop fires\n"
        "  it, and `no-channel` on `claude` and `codex`." in doc
    )


def test_thread_agent_examples_use_the_bare_name_form():
    block = _read("agents.md").split("### Per-Thread (Slack)", 1)[1].split("###", 1)[0]
    assert "!ta set" not in block and "!ta status" not in block
    assert "!ta code-reviewer" in block


def _matrix_row(doc: str, label: str) -> dict[str, str]:
    lines = [ln for ln in doc.splitlines() if ln.startswith("|")]
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells[0] == label:
            return dict(zip(header[1:], cells[1:]))
    raise AssertionError(f"row {label!r} not found")


def test_wecom_file_send_cell_follows_the_document_channels():
    row = _matrix_row(_read("channel-capabilities.md"), "Sends a file back to you")
    assert "wecom" in DOCUMENT_CHANNELS
    assert row["WeCom"] == "✅"


def test_linux_computer_use_is_documented_as_unsupported():
    assert issubclass(LinuxBackend, UnsupportedBackend)
    doc = _read("computer-use.md")
    assert "## Linux" in doc
    assert '"not\nsupported on this platform" refusal' in doc

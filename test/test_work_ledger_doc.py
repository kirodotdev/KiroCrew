"""The shipped work-ledger doc's vocabulary is pinned to the code it describes.

``scripts/docs_lint.py`` gates a doc's links, its reachability from an index, and the
symbols and paths it cites — never what it CLAIMS about them. So
``src/kiro_crew/docs/work-ledger.md`` could name three tools, or a fifth worker
status, and stay green while a dispatched worker read it at runtime and called
something that does not exist.

This module pins the vocabulary that a worker or a conductor ACTS on, against the
code that defines it: the four tool names on the work server, the four worker
statuses, the five acceptance verdicts, the terminal item states, and the two caps
the doc quotes as numbers. Each assertion reads the constant rather than a literal,
so widening either side without the other fails here.

Deliberately NOT pinned: the doc's prose about why a ``done`` is a claim, or which
field is an instruction. Those are judgements the page exists to make, and a test
that matched their wording would fail on every rewrite while catching nothing.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = _REPO_ROOT / "src" / "kiro_crew" / "docs" / "work-ledger.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_doc_names_every_work_tool_and_no_other(doc_text: str) -> None:
    """All four tool names appear, and the doc invents none.

    A worker reads this page at runtime, so a name that is not on the server is a
    call that fails. The reverse direction matters too: a tool added to the server
    and left out here leaves the page describing a smaller surface than it has.
    """
    from kiro_crew.mcp_work import WORK_TOOLS, _list_tools

    # Against the ADVERTISED surface, not against the tuple's own definition:
    # ``WORK_TOOLS`` is spelled ``WORKER_TOOLS + CONDUCTOR_TOOLS``, so comparing it
    # to that union can never red. What a caller sees is what ``_list_tools``
    # returns, and a tool defined there but left out of the tuple (or the reverse)
    # is real drift between the names the page teaches and the names the server serves.
    advertised = {tool["name"] for tool in _list_tools()}
    assert advertised == set(WORK_TOOLS), (
        f"the server advertises {sorted(advertised)} but WORK_TOOLS names " f"{sorted(WORK_TOOLS)}"
    )
    named = set(re.findall(r"`(work_[a-z_]+)`", doc_text))
    assert named == set(WORK_TOOLS), (
        f"the page names {sorted(named)} but the server serves {sorted(WORK_TOOLS)}; "
        "an invented name is a call that fails, and an omitted one understates the surface"
    )


def test_the_doc_pins_the_four_worker_statuses(doc_text: str) -> None:
    """The status table carries every value ``work_report`` accepts, and only those.

    Scoped to the table under the ``work_report`` heading rather than the whole page:
    'progress' and 'done' are ordinary English elsewhere in the prose, so a page-wide
    substring search would stay green with the table's own rows gone.
    """
    from kiro_crew.work_ledger import WORKER_STATUSES

    table = doc_text.split("## `work_report`", 1)[1].split("## Why `done` is a claim", 1)[0]
    rows = {line.split("|")[1].strip() for line in table.splitlines() if line.startswith("| `")} - {
        "`status`"
    }  # the table header, whose first cell names the field
    assert rows == {f"`{s}`" for s in WORKER_STATUSES}, (
        f"the work_report status table lists {sorted(rows)}, but the server accepts "
        f"{sorted(WORKER_STATUSES)}"
    )


def test_the_doc_pins_the_five_acceptance_verdicts(doc_text: str) -> None:
    """Every verdict the conductor may record is explained, none is invented."""
    from kiro_crew.work_ledger import VERDICTS

    section = doc_text.split("## Why `done` is a claim", 1)[1].split("\n## ", 1)[0]
    listed = {
        token
        for line in section.splitlines()
        if line.startswith("- `")
        for token in line.split("\u2014", 1)[0].split("`")[1::2]
    }
    assert VERDICTS <= listed, f"verdicts never explained: {sorted(VERDICTS - listed)}"
    assert listed <= VERDICTS, f"verdicts the evaluator cannot answer: {sorted(listed - VERDICTS)}"


def test_the_doc_pins_the_item_states(doc_text: str) -> None:
    """The item table's ``state`` row names the open state and all three terminal ones."""
    from kiro_crew.work_ledger import ITEM_STATES, TERMINAL_ITEM_STATES

    row = next(line for line in doc_text.splitlines() if line.startswith("| `state` |"))
    named = set(re.findall(r"`([a-z_]+)`", row)) - {"state"}
    assert (
        named == ITEM_STATES
    ), f"the state row documents {sorted(named)} but the store accepts {sorted(ITEM_STATES)}"
    for state in TERMINAL_ITEM_STATES:
        assert (
            f"`{state}`" in row.split("terminal:", 1)[1]
        ), f"{state!r} is a terminal state but the row does not present it as one"


def test_the_doc_quotes_the_real_caps(doc_text: str) -> None:
    """The two numbers the page states are read off the store's own constants."""
    from kiro_crew.work_ledger import MAX_DEPTH, MAX_ITEMS_PER_CONDUCTOR

    assert f"{MAX_ITEMS_PER_CONDUCTOR} items per conductor" in doc_text
    assert f"depth is capped at {MAX_DEPTH}" in doc_text


def _cap(tool: str, field: str) -> int:
    """The validated ``max_len`` for one field of one work tool."""
    from kiro_crew.validation import MCP_WORK_SCHEMAS

    spec = next(f for f in MCP_WORK_SCHEMAS[tool].fields if f.name == field)
    assert spec.max_len is not None, f"{tool}.{field} no longer carries a length cap"
    return spec.max_len


def test_the_doc_quotes_the_real_summary_cap(doc_text: str) -> None:
    """``summary``'s advertised cap matches the schema the tool validates against.

    The refusal wording is pinned beside the number: the schema deliberately does not
    clamp, and a page that said "truncated" would tell a worker its long summary
    landed when the call was rejected.
    """
    cap = _cap("work_report", "summary")
    assert (
        f"capped at {cap} characters" in doc_text
    ), f"work_report's summary cap is {cap}; the doc states a different number"
    assert "refused rather than truncated" in doc_text


def test_the_doc_quotes_the_real_title_cap(doc_text: str) -> None:
    """The item table's ``title`` row quotes the cap a ``create`` is validated against."""
    cap = _cap("work_ledger_record", "title")
    row = next(line for line in doc_text.splitlines() if line.startswith("| `title` |"))
    assert f"up to {cap} chars" in row, f"an item title is capped at {cap}; the row disagrees"


def test_the_doc_lists_every_acceptance_kind_the_evaluator_handles(doc_text: str) -> None:
    """The three kinds are named, and the removed ``cmd`` kind is not offered.

    Read out of the evaluator's own dispatch rather than from a list: the script is
    the thing a conductor runs, so its branches are the real vocabulary.
    """
    script = (
        _REPO_ROOT / "src/kiro_crew/builtin_skills/goal-conductor/scripts/accept_eval.py"
    ).read_text(encoding="utf-8")
    handled = {
        line.split('== "', 1)[1].split('"', 1)[0]
        for line in script.splitlines()
        if line.strip().startswith('if kind == "')
    }
    assert "cmd" in handled, "the cmd branch is what refuses a command-shaped spec"
    offered = handled - {"cmd"}
    paragraph = doc_text.split("It is one of", 1)[1].split("\n\n", 1)[0]
    advertised = set(re.findall(r"`([a-z_]+)`", paragraph))
    assert advertised == offered, (
        f"the page offers acceptance kinds {sorted(advertised)} but the evaluator "
        f"handles {sorted(offered)}; an invented kind returns 'unknown accept kind'"
    )
    assert 'no "run this command" kind' in doc_text

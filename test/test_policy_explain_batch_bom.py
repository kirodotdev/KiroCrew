"""``kirocrew policy explain --batch`` reads a UTF-8 BOM without failing line 1.

An editor that writes a UTF-8 BOM leaves a leading U+FEFF on the first line. The
batch reader splits on newlines and ``json.loads`` each line, so the BOM made
line 1's request a parse error. The reader now drops one leading BOM, so the
first request is answered like any other.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from kiro_crew import cli_commands_security as m
from kiro_crew.platform.governance import SCOPE_CATALOG


def _run_batch_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> list[dict]:
    monkeypatch.setattr(m.sys, "stdin", io.StringIO(text))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m._explain_batch(None, "-", "", "", "")
    except SystemExit:
        pass  # exit 1 only signals that some line errored; the records are on stdout
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_a_leading_bom_does_not_break_the_first_batch_request(monkeypatch):
    scope = sorted(SCOPE_CATALOG)[0]
    request = json.dumps({"scope": scope, "item": "x"})

    # With a BOM: line 1 must still be answered (not an error record).
    with_bom = _run_batch_stdin(monkeypatch, "\ufeff" + request + "\n")
    assert len(with_bom) == 1
    assert with_bom[0]["line"] == 1
    assert "error" not in with_bom[0]
    assert with_bom[0]["scope"] == scope

    # Without a BOM: identical answer, so the BOM strip changes nothing else.
    without_bom = _run_batch_stdin(monkeypatch, request + "\n")
    assert without_bom == with_bom


def test_a_bom_only_strips_one_leading_mark_not_interior_content(monkeypatch):
    # A U+FEFF inside a JSON string on a later line is real content, untouched.
    # ensure_ascii=False keeps a RAW U+FEFF in the line (the default would escape
    # it to the six chars "\\ufeff"), so this actually pins interior survival.
    scope = sorted(SCOPE_CATALOG)[0]
    line1 = json.dumps({"scope": scope, "item": "x"})
    line2 = json.dumps({"scope": scope, "item": "a\ufeffb"}, ensure_ascii=False)
    assert "\ufeff" in line2  # a raw interior BOM, not its escape
    records = _run_batch_stdin(monkeypatch, "\ufeff" + line1 + "\n" + line2 + "\n")
    assert [r["line"] for r in records] == [1, 2]
    assert all("error" not in r for r in records)
    assert records[1]["item"] == "a\ufeffb"

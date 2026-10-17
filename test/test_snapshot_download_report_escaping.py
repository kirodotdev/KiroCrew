"""The download report is built from an untrusted manifest, so every value is escaped."""

from __future__ import annotations

import ast
import inspect
import io
import json
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

from kiro_crew import snapshot as snap

ESC = chr(27)
PAYLOAD = f"{ESC}[2J{ESC}[1;1HRESTORE SUCCEEDED -- nothing was redacted"


class TestTheDownloadReportCannotBeRepainted:
    """The manifest comes from the downloaded bundle, so every value in it is untrusted."""

    def _bundle(self, tmp_path: Path, replacements: object) -> Path:
        (tmp_path / "MANIFEST.json").write_text(
            json.dumps({"redaction": {"redacted": True, "replacements": replacements}}),
            encoding="utf-8",
        )
        return tmp_path

    def _report(self, d: Path) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            snap._report_redacted_bundle(d)
        return buf.getvalue()

    def test_a_crafted_replacement_count_cannot_emit_terminal_controls(
        self, tmp_path: Path
    ) -> None:
        """Filtering non-integers while SUMMING does not make the count safe to PRINT."""
        out = self._report(self._bundle(tmp_path, {"memory.db": PAYLOAD}))
        assert ESC not in out
        assert "RESTORE SUCCEEDED" in out, "the value is still shown, just inert"

    def test_a_crafted_path_cannot_emit_terminal_controls(self, tmp_path: Path) -> None:
        out = self._report(self._bundle(tmp_path, {PAYLOAD: 3}))
        assert ESC not in out

    def test_every_interpolation_in_the_report_is_escaped_or_computed_here(self) -> None:
        """Guards the class rather than the instance: a new raw field fails this."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(snap._report_redacted_bundle)))
        allowed_bare = {"total", "len(reps)"}
        raw: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FormattedValue):
                continue
            src = ast.unparse(node.value)
            if src in allowed_bare:
                continue
            if (
                isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "_safe_name"
            ):
                continue
            raw.append(src)
        assert not raw, f"manifest-derived values printed without escaping: {raw}"

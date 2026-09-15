"""A name printed from disk goes through the terminal escaper, at every site.

Three rounds fixed this one site at a time -- a manifest count beside an escaped path, the
bidi controls, and now a staging warning -- so this pins the RULE structurally as well as
behaviourally: no `print` in the backup modules interpolates a `.name` raw.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kiro_crew import snapshot as snap

SRC = Path(snap.__file__).parent
MODULES = ("snapshot.py", "snapshot_redact.py", "snapshot_remote.py", "backup_cli.py")


class TestNoPrintInterpolatesARawName:
    def test_every_name_print_goes_through_the_escaper(self) -> None:
        offenders: list[str] = []
        pattern = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\.name\}")
        for module in MODULES:
            path = SRC / module
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    continue
                rendered = ast.dump(node)
                if "_safe_name" in rendered or "safe_for_terminal" in rendered:
                    continue
                # Reconstruct the f-string's literal text to spot a bare `{x.name}`.
                literal = "".join(
                    v.value
                    for v in ast.walk(node)
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                joined = literal + " ".join(
                    ast.unparse(v) for v in ast.walk(node) if isinstance(v, ast.Attribute)
                )
                if pattern.search(ast.unparse(node)) or re.search(r"\.name\b", joined):
                    if ".name" in ast.unparse(node):
                        offenders.append(f"{module}:{node.lineno}")
        assert offenders == [], (
            f"print site(s) {offenders} interpolate a name without _safe_name -- a "
            f"hostile filename would reach the terminal raw"
        )

    def test_the_escaper_still_neutralises_an_escape_sequence(self) -> None:
        """Behavioural half: the structural walk above proves routing, not effect."""
        assert "\x1b" not in snap._safe_name("\x1b[2Kfake.db")
        assert "\u202e" not in snap._safe_name("\u202egnp.db")

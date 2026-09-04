"""Two ways a credential reached the destination unscrubbed: an exemption matched by
basename, and a SQL identifier that closed its own quote.

Both are the same shape -- a rule written for one specific thing, applied to everything
that merely resembles it -- and both end with the pass reporting a clean scan of bytes it
never examined.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


def _ddl_quoted(name: str) -> str:
    """Quote *name* for DDL, escaping by hand rather than through the code under test."""
    return '"' + name.replace('"', '""') + '"'


class TestOnlyTheRootManifestIsExempt:
    """The manifest branch is for the ONE manifest this pass rewrites with its report."""

    def test_a_nested_manifest_does_not_ride_out_unscrubbed(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        nested = stage / "workspace" / "MANIFEST.json"
        nested.parent.mkdir(parents=True)
        nested.write_text(json.dumps({"note": f"key={KEY}"}), encoding="utf-8")

        redact.redact_bundle_for_egress(stage)

        assert KEY not in nested.read_text(encoding="utf-8")

    def test_the_root_manifest_takes_the_manifest_branch(self, tmp_path: Path) -> None:
        """It is rewritten with the report, which is why it is not scrubbed as content."""
        stage = _stage(tmp_path)
        root = stage / "MANIFEST.json"

        redact.redact_bundle_for_egress(stage)

        assert "redaction" in json.loads(root.read_text(encoding="utf-8"))


class TestAnIdentifierCannotRewriteTheScan:
    """Identifiers come from each database's own schema, not from this product."""

    def test_a_column_name_that_closes_its_quote_still_gets_scanned(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        hostile = _ddl_quoted('body" FROM decoy --')
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute(f"CREATE TABLE items(id INTEGER PRIMARY KEY, {hostile} TEXT)")
            conn.execute(f"INSERT INTO items({hostile}) VALUES(?)", (f"key={KEY}",))
            conn.execute("CREATE TABLE decoy(body TEXT)")
            conn.execute("INSERT INTO decoy(body) VALUES('nothing secret')")
            conn.commit()
        conn.close()

        redact.redact_bundle_for_egress(stage)

        assert KEY.encode("utf-8") not in db.read_bytes()

    def test_a_table_name_that_closes_its_quote_still_gets_scanned(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        hostile = _ddl_quoted('items" ; --')
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute(f"CREATE TABLE {hostile}(id INTEGER PRIMARY KEY, body TEXT)")
            conn.execute(f"INSERT INTO {hostile}(body) VALUES(?)", (f"key={KEY}",))
            conn.commit()
        conn.close()

        redact.redact_bundle_for_egress(stage)

        assert KEY.encode("utf-8") not in db.read_bytes()

    def test_quoting_doubles_an_embedded_quote_and_leaves_a_plain_name_alone(self) -> None:
        assert redact._quote_ident("body") == '"body"'
        assert redact._quote_ident('a"b') == '"a""b"'

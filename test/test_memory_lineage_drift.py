"""Neither lineage may drift out from under ``vector_memory``'s SQL.

``vector_memory`` drives two schema lineages through one statement set. Fifteen of
its write statements interpolate a per-lineage relation name
(``f"UPDATE {self._sem_rel} ... {self._sem_guard}"``) and four more route through a
``memory_schema`` builder; on the v1 lineage every one of them renders
byte-identically to the literal it replaced.

That byte-identity is exactly what makes the drift invisible. A twentieth write
spelled as a plain ``UPDATE semantic_memory SET ...`` literal is correct on every
v1 install — which is every file the rest of the suite exercises — and fails only
on a crew silo, at runtime, because ``semantic_memory`` is a VIEW there:
``sqlite3.OperationalError: cannot modify semantic_memory because it is a view``.
No other test is positioned to notice, so the guard lives here.

Three properties, one test each:

* every WRITE position names a relation that is writable in BOTH lineages;
* every relation the module names at all — reads included — exists in both;
* the two lineages present ``semantic_memory`` and ``episodic_memories`` with
  identical column names in identical ORDER, because ``SELECT *`` feeds
  ``sqlite3.Row`` and a reordered view silently changes what a positional read
  returns.

The scan walks ``vector_memory.py``'s AST and opens nothing but ``:memory:``
databases, so it has no filesystem side effects at all.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path
from typing import Final

import pytest

from kiro_crew import memory_record_metadata, memory_schema, vector_memory

# ── The relation scanner ─────────────────────────────────────────────────────

#: Stands in for an f-string placeholder once a literal is rendered, so
#: ``f"UPDATE {self._sem_rel} SET ..."`` reads as
#: ``UPDATE __interpolated__ SET ...``. Identifier-shaped on purpose: the same
#: relation-name pattern matches it, and it reads intelligibly in a failure
#: message. No relation is or could be called this.
_INTERPOLATED: Final = "__interpolated__"

_RELATION: Final = r"([A-Za-z_][A-Za-z0-9_]*)"

#: A write keyword followed by its target. Case-INSENSITIVE: a lowercase
#: ``update semantic_memory set`` is just as broken at runtime, and no
#: non-docstring string in this module matches a write keyword followed by an
#: identifier, so nothing is paid for the extra reach.
_WRITE_RE: Final = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+" + _RELATION,
    re.IGNORECASE,
)

#: The relation inventory first requires a SQL statement/fragment head, so prose
#: such as "Conflicting update saved for review" is not a query. Both keyword
#: cases then work without an ever-growing exemption list of English words.
_SQL_HEAD: Final = re.compile(
    r"(?:SELECT|WITH|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|PRAGMA|FROM|JOIN)\b",
    re.IGNORECASE,
)
_READ_RE: Final = re.compile(r"\b(?:FROM|JOIN)\s+" + _RELATION, re.IGNORECASE)

#: The two relations that are VIEWS on the crew lineage. Writable on v1, refused
#: by SQLite on a silo — which is the whole failure this module guards.
_VIEWS_ON_CREW: Final = ("semantic_memory", "episodic_memories")

#: Names that land in a relation position without being relations. An explicit
#: list, not a pattern, so every exemption is auditable at a glance.
_NOT_A_RELATION: Final = frozenset(
    {
        # ``ON CONFLICT(key) DO UPDATE SET value = excluded.value`` — the upsert
        # clause's own keyword sits where a table name otherwise would.
        "SET",
        # SQLite's catalog: readable in every database, declared by neither
        # lineage's DDL, and absent from its own contents.
        "the schema table",
    }
)


def _render(node: ast.Constant | ast.JoinedStr) -> str | None:
    """The text of *node* as a string, or ``None`` for a non-string constant.

    An f-string is rendered whole, with every ``{...}`` collapsed to
    :data:`_INTERPOLATED`. Inspecting an ``ast.JoinedStr``'s literal parts in
    isolation would be worse than useless here: the compliant
    ``f"UPDATE {self._sem_rel} SET x = ?"`` splits into ``"UPDATE "`` and
    ``" SET x = ?"``, neither of which carries a keyword-plus-target pair, so the
    scan would go quietly blind on precisely the statements it exists to police.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
        else:
            parts.append(_INTERPOLATED)
    return "".join(parts)


def _executable_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, text)`` for every string literal in *tree* that could be SQL.

    Two exclusions, both structural:

    * **Docstrings.** Prose, never executed, and this module's docstrings discuss
      ``UPDATE`` and ``FROM`` in English sentences ("an unconditional UPDATE per
      hit turns each read into a write transaction").
    * **Nodes inside an f-string**, which :func:`_render` already covered as part
      of the whole ``JoinedStr``; counting them again would double-report and
      would reintroduce the split-literal blind spot.
    """
    docstrings: set[int] = set()
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                inside_fstring.update(id(sub) for sub in ast.walk(part))

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        if id(node) in docstrings or id(node) in inside_fstring:
            continue
        text = _render(node)
        if text is not None:
            found.append((node.lineno, text))
    return found


def _write_violations(tree: ast.AST, label: str) -> list[str]:
    """One message per write statement naming a relation that is a view on a silo.

    Shared by the real guard and its seeded self-test, so a regression in the
    renderer cannot turn the guard vacuous while the self-test still passes.
    """
    violations: list[str] = []
    for lineno, text in _executable_literals(tree):
        for match in _WRITE_RE.finditer(text):
            target = match.group(1)
            if target not in _VIEWS_ON_CREW:
                continue
            rel, guard = (
                ("_sem_rel", "_sem_guard")
                if target == "semantic_memory"
                else ("_epi_rel", "_epi_guard")
            )
            # Everything the statement said before the relation name, so the
            # suggestion below keeps the multi-word verbs intact -- "DELETE FROM"
            # and "INSERT OR IGNORE INTO", not a truncated "DELETE".
            verb = text[match.start() : match.start(1)].strip().upper()
            violations.append(
                f"{label}:{lineno} writes to `{target}`, which is a VIEW on the crew "
                f'lineage. SQLite refuses it — "cannot modify {target} because it is a '
                'view" — so this statement is correct on every v1 install and fails '
                "only on a crew silo, at runtime.\n"
                f"    Fix: interpolate the per-lineage relation, e.g.\n"
                f'      f"{verb} {{self.{rel}}} ... {{self.{guard}}}"\n'
                "    If the COLUMN LIST also differs between lineages, add a builder "
                "to memory_schema instead (see semantic_insert / semantic_upsert / "
                "episodic_insert and their _params twins)."
            )
    return violations


def _unguarded_write_violations(source: str, label: str) -> list[str]:
    """One message per write that interpolates a per-lineage relation but no kind guard.

    The relation check alone is only half the contract. On the crew lineage
    ``_sem_rel`` and ``_epi_rel`` are THE SAME physical table, so a statement that
    swapped the relation correctly and dropped the guard reaches rows of the other
    kind -- a semantic tombstone landing on an episode. It is invisible on v1, where
    the two relations are distinct tables and the guard renders empty.

    Scanned over the raw SOURCE rather than the AST because the pairing is per
    STATEMENT and an implicitly concatenated statement spans several literals: the
    relation may appear in one fragment and the guard in the next.
    """
    violations: list[str] = []
    for rel, guard in (("_sem_rel", "_sem_guard"), ("_epi_rel", "_epi_guard")):
        # Only an INTERPOLATION counts: `{self._sem_rel}` inside an f-string is a SQL
        # statement, whereas a bare `self._sem_rel = ...` is the resolution in __init__
        # and init(), which has no guard to carry.
        for match in re.finditer(rf"\{{\s*self\.{rel}\s*\}}", source):
            # The statement is everything up to the next unescaped closing paren at
            # depth zero; a generous window is enough because a guard always follows
            # its relation within the same call.
            window = source[match.start() : match.start() + _GUARD_WINDOW]
            head = window.split(")", 1)[0]
            if f"self.{guard}" in head:
                continue
            lineno = source[: match.start()].count("\n") + 1
            violations.append(
                f"{label}:{lineno} interpolates `self.{rel}` with no `self.{guard}`. On "
                "the crew lineage both relations ARE the same table, so this statement "
                "reaches rows of the other kind; on v1 the guard renders empty, so the "
                "text is unchanged there. Append the guard."
            )
    return violations


#: How far past a relation interpolation to look for its guard. A write statement in this
#: module is at most a few hundred characters, and the guard always trails the relation
#: inside the same call.
_GUARD_WINDOW = 400


def _has_sql_head(text: str) -> bool:
    """Skip leading SQL comments with a forward-only scan."""
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
        elif text.startswith("--", position):
            end = text.find("\n", position + 2)
            if end < 0:
                return False
            position = end + 1
        elif text.startswith("/*", position):
            end = text.find("*/", position + 2)
            if end < 0:
                return False
            position = end + 2
        else:
            return _SQL_HEAD.match(text, position) is not None
    return False


def _named_relations(tree: ast.AST) -> set[str]:
    """Every relation name the module names, in a read or a write position."""
    names: set[str] = set()
    for _lineno, text in _executable_literals(tree):
        if not _has_sql_head(text):
            continue
        for pattern in (_WRITE_RE, _READ_RE):
            names.update(match.group(1) for match in pattern.finditer(text))
    return names - {_INTERPOLATED} - _NOT_A_RELATION


# ── The two lineages, built from the DDL each one actually applies ───────────

#: ``init()`` creates this on every file before it decides on a lineage, so it
#: belongs to neither migration list and both databases below need it.
_BOOKKEEPING_SQL: Final = (
    "CREATE TABLE IF NOT EXISTS schema_version "
    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def _v1_db() -> sqlite3.Connection:
    """A real v1 file: the frozen ``[1, 2, 3]`` migration list, in order."""
    db = sqlite3.connect(":memory:")
    db.executescript(_BOOKKEEPING_SQL)
    db.executescript(vector_memory._SCHEMA_V1)
    vector_memory._migrate_v2(db)
    db.executescript(vector_memory._MEMORY_META_TABLE)
    memory_record_metadata.ensure_schema(db)
    return db


def _crew_db() -> sqlite3.Connection:
    """A real crew silo: ``memory_items`` plus the two read-only views."""
    db = sqlite3.connect(":memory:")
    db.executescript(_BOOKKEEPING_SQL)
    db.executescript(memory_schema.CREW_SCHEMA_SQL)
    memory_record_metadata.ensure_schema(db)
    return db


def _relations_of(db: sqlite3.Connection) -> set[str]:
    """Table AND view names in *db*. A view counts: the read statements need it."""
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')"
        ).fetchall()
    }


def _columns_of(db: sqlite3.Connection, relation: str) -> list[str]:
    """Column names of *relation*, in declaration order."""
    return [row[1] for row in db.execute(f"PRAGMA table_info({relation})").fetchall()]


class TestLineageDrift:
    """The engine's SQL must hold on both lineages, not only the one it grew on."""

    @staticmethod
    def _module_tree() -> ast.Module:
        import kiro_crew

        source = Path(kiro_crew.__file__).resolve().with_name("vector_memory.py")
        return ast.parse(source.read_text(encoding="utf-8"))

    def test_a_write_to_a_view_is_refused_at_runtime(self) -> None:
        """The premise, pinned: SQLite really does refuse these four shapes.

        Without this the guard would rest on a claim about SQLite rather than on
        SQLite's behaviour, and a future SQLite that grew auto-updatable views
        would leave the other tests enforcing a rule with no consequence.
        """
        db = _crew_db()
        for statement, relation in (
            ("UPDATE semantic_memory SET is_deleted = 1 WHERE key = 'k'", "semantic_memory"),
            ("INSERT INTO semantic_memory (key) VALUES ('k')", "semantic_memory"),
            ("UPDATE episodic_memories SET is_deleted = 1 WHERE id = 'i'", "episodic_memories"),
            ("DELETE FROM episodic_memories WHERE id = 'i'", "episodic_memories"),
        ):
            with pytest.raises(sqlite3.OperationalError) as excinfo:
                db.execute(statement)
            assert f"cannot modify {relation} because it is a view" in str(excinfo.value)

    def test_the_detector_reads_f_strings_in_both_directions(self) -> None:
        """The scanner itself is under test, not merely run.

        An interpolated relation must PASS and a hardcoded one must FAIL — in a
        plain literal AND inside an f-string, which is where a naive
        literal-parts walk goes blind.
        """
        import textwrap

        sample = textwrap.dedent('''
            """A docstring may say UPDATE semantic_memory SET all it likes."""

            class Store:
                def compliant(self):
                    self.db.execute(f"UPDATE {self._sem_rel} SET x = 1{self._sem_guard}")
                    self.db.execute(f"DELETE FROM {self._epi_rel} WHERE id = ?")
                    self.db.execute("SELECT * FROM semantic_memory WHERE key = ?")
                    self.db.execute("INSERT INTO memory_events (source) VALUES (?)")

                def broken(self):
                    """Prose: DELETE FROM episodic_memories is fine to mention."""
                    self.db.execute(f"UPDATE semantic_memory SET x = {value}")
                    self.db.execute("DELETE FROM episodic_memories WHERE id = ?")
            ''')
        violations = _write_violations(ast.parse(sample), "<sample>")

        assert len(violations) == 2, f"expected exactly the two seeded writes, got {violations}"
        # The hardcoded f-string write, caught where a literal-parts walk sees
        # only "UPDATE semantic_memory SET x = " with no target of its own.
        assert "writes to `semantic_memory`" in violations[0]
        # A guard whose message omits the fix gets deleted by whoever trips it,
        # so the suggested spelling is part of the contract -- including the
        # multi-word verb, which a naive first-token split truncates.
        assert 'f"UPDATE {self._sem_rel} ... {self._sem_guard}"' in violations[0]
        assert "writes to `episodic_memories`" in violations[1]
        assert 'f"DELETE FROM {self._epi_rel} ... {self._epi_guard}"' in violations[1]

    def test_every_write_statement_names_a_writable_relation(self) -> None:
        violations = _write_violations(self._module_tree(), "vector_memory.py")
        assert not violations, "\n\n".join(violations)

    def test_every_per_lineage_write_also_carries_its_kind_guard(self) -> None:
        """The other half of the contract, and it needs its own detector.

        Verified in both directions on a seeded sample, so the detector cannot go
        vacuous: a guarded write passes and an unguarded one is reported.
        """
        import kiro_crew

        module = Path(kiro_crew.__file__).resolve().with_name("vector_memory.py")
        source = module.read_text(encoding="utf-8")
        assert _unguarded_write_violations(source, "vector_memory.py") == []

        sample = (
            'self.db.execute(f"UPDATE {self._sem_rel} SET a = 1 WHERE key = ?'
            '{self._sem_guard}", (k,))\n'
            'self.db.execute(f"UPDATE {self._epi_rel} SET b = 1 WHERE id = ?", (i,))\n'
        )
        seeded = _unguarded_write_violations(sample, "sample")
        assert len(seeded) == 1, seeded
        assert "_epi_guard" in seeded[0]

    def test_every_relation_the_module_names_exists_in_both_lineages(self) -> None:
        """A statement may only name a relation both lineages have.

        This is what catches the other half of the drift: not a write to a view,
        but a read (or write) of something only one lineage ever created.
        """
        named = _named_relations(self._module_tree())

        # Non-vacuity: a renderer regression that collected nothing would
        # otherwise satisfy every assertion below.
        assert {"semantic_memory", "episodic_memories", "memory_events"} <= named, (
            f"the scan collected {sorted(named)}, which is missing relations the module "
            "demonstrably names — the literal collector is broken, not the module"
        )

        for lineage, db in (("v1", _v1_db()), ("crew", _crew_db())):
            missing = sorted(named - _relations_of(db))
            assert not missing, (
                f"vector_memory names {missing}, which the {lineage} lineage does not "
                f"have. Either add the relation to that lineage's DDL, route the "
                f"statement through a memory_schema builder, or — if the name is not a "
                f"relation at all (a SQL keyword, a PRAGMA target, a CTE) — add it to "
                f"_NOT_A_RELATION with a comment saying which."
            )

    def test_relation_inventory_checks_sql_without_classifying_returned_prose(self) -> None:
        sample = ast.parse(
            '"""SELECT * FROM docstring_example"""\n'
            'db.execute("SELECT * FROM missing_static")\n'
            'db.execute(f"SELECT {column} FROM missing_dynamic")\n'
            'query = "-- retrieval query\\nselect * from missing_lowercase"\n'
            'result = f"Conflicting update saved for review (proposal {proposal_id})"\n'
            'logger.info("A result FROM conversational context")\n'
            "# SELECT * FROM commented_out\n"
        )
        named = _named_relations(sample)
        assert named == {"missing_static", "missing_dynamic", "missing_lowercase"}
        for db in (_v1_db(), _crew_db()):
            try:
                assert named - _relations_of(db) == named
                assert {"memory_record_meta", "memory_revisions"} <= _relations_of(db)
            finally:
                db.close()

    @pytest.mark.timeout(10)
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("/*" + "*//*" * 512 + "!", set()),
            ("/* note */" * 512 + "select * from memory_items", {"memory_items"}),
            ("-- SELECT * FROM ignored", set()),
            (" /* note */ -- next\n SELECT * FROM memory_items", {"memory_items"}),
        ],
    )
    def test_relation_inventory_handles_comment_runs_without_backtracking(
        self, query: str, expected: set[str]
    ) -> None:
        sample = ast.parse(f"db.execute({query!r})")
        assert _named_relations(sample) == expected

    def test_the_two_lineages_agree_on_the_v1_column_contract(self) -> None:
        """Same columns, same ORDER — order is the half that fails silently.

        ``SELECT *`` on either relation feeds ``sqlite3.Row``, and the engine
        indexes some of those rows positionally. A crew view that listed the same
        columns in a different order would return well-typed wrong values rather
        than raising, so the order is as load-bearing as the membership.
        """
        v1, crew = _v1_db(), _crew_db()
        for relation in _VIEWS_ON_CREW:
            v1_columns = _columns_of(v1, relation)
            crew_columns = _columns_of(crew, relation)
            assert v1_columns, f"{relation} has no columns on v1 — the DDL build is broken"
            assert crew_columns == v1_columns, (
                f"{relation} differs between lineages.\n"
                f"  v1:   {v1_columns}\n"
                f"  crew: {crew_columns}\n"
                "The crew view must present v1's columns in v1's order: `SELECT *` feeds "
                "sqlite3.Row and a reordered view changes what a positional read returns "
                "without raising. Fix the view's SELECT list in "
                "memory_schema.CREW_SCHEMA_SQL, never the reader."
            )

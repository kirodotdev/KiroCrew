#!/usr/bin/env python3
"""The security conductor's findings ledger — one SQLite database, five tables.

A round that does not remember the last one repeats its false positives. This
script is the harness's only programmatic writer for five kinds of row: findings,
the verdicts folded into them, the lessons a retrospective proposes, the rules of
engagement ``scope_check.py`` reads, and the golden paths whose committed export
``verify_fix.py`` re-checks after a fix.

Usage. ``--db PATH`` goes BEFORE the subcommand and defaults to
``<data home>/security-conductor/findings.db``:

    python3 ledger.py [--db PATH] init
    python3 ledger.py [--db PATH] add-finding --surface S --title T --severity SEV
                                  [--path P ...] [--poc CMD] [--round-id R]
    python3 ledger.py record-verdict --finding ID --role ROLE --verdict V
                                     [--reason WHY]
    python3 ledger.py propose-lesson --kind K --surface S --pattern P
                                     --guidance G --source-finding ID
    python3 ledger.py approve-lesson --id ID --approved-by WHO
    python3 ledger.py add-rule --field F --value V --reason WHY --approved-by WHO
    python3 ledger.py export-roe
    python3 ledger.py propose-golden-path --kind K --surface S --command CMD
                                         --reason WHY [--platform P]
                                         [--source-finding ID]
    python3 ledger.py approve-golden-path --id ID --approved-by WHO
    python3 ledger.py import-golden-paths FILE --approved-by WHO
    python3 ledger.py list {findings|lessons|rules|verdicts|golden-paths}
    python3 ledger.py seed-lessons --surface S --budget-bytes N

The data home is ``$KIROCREW_HOME``, else ``~/.kiro/crew``.

**This CLI is not an authentication boundary, and does not try to be.**
``--role human`` and ``--approved-by`` are UNVERIFIED caller assertions: nothing
here can tell a human at a terminal from an agent invoking the same command, and
a forged ``human`` verdict wins the fold exactly as a real one would. The boundary
is instead **filesystem ownership of the database**. Anything that can run this
script can run ``sqlite3`` against the same file and write any row directly, so
authenticating this CLI would move no boundary -- it would only make the weakest
path less obvious. A deployment that needs an enforced human gate has to own
``findings.db`` under a different uid than the agent, or put the approval step
behind something that is not a local CLI; that is a deployment property this
script cannot assert, and the RFC does not yet decide it.

What the four properties below therefore mean: given whatever writes reach this
script, it will not itself lose, reorder, or silently overwrite them. They are
storage guarantees, not authorization ones.

Four properties this script exists to hold, none of which a prompt can:

1. **``verdicts`` is append-only.** There is no UPDATE and no DELETE path to it
   anywhere in this CLI, so the disagreement between an auditor and a verifier
   stays readable instead of being overwritten by whoever wrote last.
   ``findings.final_verdict`` is therefore a FOLD over those rows —
   ``human > verifier > auditor`` — recomputed on every append, never assigned.
2. **A proposed lesson is inert, and its approval is recorded once.**
   ``propose-lesson`` writes ``active=0`` and takes no flag that could write
   anything else; only ``approve-lesson``, which requires a named approver, flips
   it. ``seed-lessons`` reads ``active=1`` only, so an unapproved lesson cannot
   reach an auditor's seed message. Approval is write-once: ``approve-lesson``
   updates only a row that is still ``active=0``, so a second approval is a named
   refusal rather than a silent replacement of who approved it.
3. **The seed is bounded.** ``seed-lessons`` emits at most ``--budget-bytes``
   bytes, because an unbounded lesson list becomes the seed and crowds out the
   brief.
4. **Every lesson is attributable, in both directions.**
   ``source_finding_id`` is NOT NULL and a real foreign key, enforced with
   ``PRAGMA foreign_keys=ON``, so guidance traces back to the finding that earned
   it; and ``approved_by`` cannot be overwritten once set -- not by a second
   approval, and not by deactivating the lesson and approving it again, since the
   write requires ``approved_by IS NULL`` and not merely ``active = 0``. Every
   required text
   argument is rejected when blank -- ``required=True`` only asserts a flag is
   PRESENT, so an empty approver would otherwise satisfy the parser and store no
   attribution at all.

Deactivating a rule, a lesson or a golden path is deliberately NOT a command
here: the RFC makes that the human's row edit (``UPDATE roe_rules SET active=0``,
``UPDATE golden_paths SET active=0``), so that reverting one is a flip with an
audit trail rather than a code change. For a golden path the gate does not read
this table at all -- ``verify_fix.py`` reads the committed ``golden-paths.json``
beside the skill and nothing else, as the RFC rules -- but the table is where that
export is edited from, and a CLI that retired rows would let the agent whose fix
broke an operation start shrinking the corpus its fix is judged against. The RFC
names that as the move the design exists to forbid, so the flip stays a human's
row edit. The append-only ban above covers ``verdicts`` specifically, not the whole
database.

Golden paths carry the same propose/approve split as lessons for the write that
DOES widen: ``propose-golden-path`` writes ``active=0`` and takes no approver,
and only ``approve-golden-path`` -- write-once, exactly as ``approve-lesson`` --
makes a row eligible for the committed export ``verify_fix.py`` reads; landing it
there is a reviewed file edit. There is no one-step verb that writes an active,
attributed row: that would be the approval without its write-once record. The RFC
rules the split and the committed file ("The gating corpus is a committed export");
the three verb names are this module's. ``import-golden-paths`` reads the committed
file into the table so a host's proposals dedupe against what is already agreed. Row
identity is (kind, command_or_flow, platform), enforced by a unique index, so
importing the shipped corpus twice is one corpus rather than two.

Exit codes: 0 on success; 2 on malformed arguments or a referenced row that does
not exist. Reads and writes one SQLite file; no network, no subprocess.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    # Preferred: the same shim every other store in this tree imports SQLite
    # through (memory, vector memory, knowledge). It resolves ``pysqlite3``
    # when present, which is the build guaranteed to carry FTS5.
    from kiro_crew._sqlite_compat import sqlite3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - exercised when the package is not importable
    # A skill's scripts are synced OUT of the package tree (into the skills
    # directory) and run as bare files, so ``kiro_crew`` is usually not on the
    # path. Falling back is safe here specifically because this ledger uses no
    # FTS5 — it is five ordinary tables — which is the only capability the shim
    # exists to secure. Nothing else about the shim's behaviour is relied on.
    import sqlite3  # type: ignore[no-redef]

SCHEMA_VERSION = 2

ROLES = ("auditor", "verifier", "human")
# Fold precedence, weakest first: a human overrules a verifier, who overrules an
# auditor. Derived from ROLES' order nowhere — spelled out, because the fold is
# the contract and reordering ROLES for any other reason must not change it.
FOLD_ORDER = ("auditor", "verifier", "human")
LESSON_KINDS = ("true-positive", "false-positive", "missed", "out-of-scope")
# What a golden path IS, which decides what ``verify_fix.py`` can say about it. A
# ``shell`` row is CHECKED: classified against the fixed worktree's own deny fence,
# and a refusal rejects the fix. A ``flow`` or ``cron`` row is RECORDED and reported
# for a human to exercise, and neither is ever executed -- this CLI is not an
# authentication boundary, so a row is untrusted text, and running one would turn a
# write to this database into a command with the operator's access.
GOLDEN_PATH_KINDS = ("shell", "flow", "cron")
# ``any`` is a stored value rather than a NULL so that the platform filter is one
# ``IN`` clause: a row that applies everywhere is selected by every host, and a
# NULL would need every reader to remember a second branch.
GOLDEN_PATH_PLATFORMS = ("any", "posix", "windows")

LIST_QUERIES = {
    "findings": "SELECT * FROM findings ORDER BY id ASC",
    "lessons": "SELECT * FROM lessons ORDER BY id ASC",
    "rules": "SELECT * FROM roe_rules ORDER BY id ASC",
    # verdicts has no surrogate key (the RFC's schema does not give it one), so
    # its append order is the implicit rowid -- surfaced under that name so a
    # reader can see which column the ordering came from.
    "verdicts": "SELECT rowid AS rowid, * FROM verdicts ORDER BY rowid ASC",
    "golden-paths": "SELECT * FROM golden_paths ORDER BY id ASC",
}
LIST_TABLES = tuple(LIST_QUERIES)

DDL = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    # The version row is a SINGLETON, and this index is what makes that a
    # property of the table rather than of the code that writes it: two
    # concurrent `init` runs (or any two commands, since every command calls
    # init_schema first) would otherwise both find the table empty and both
    # insert, leaving a migration ladder with two rows to branch on.
    "CREATE UNIQUE INDEX IF NOT EXISTS schema_version_single ON schema_version (version)",
    """CREATE TABLE IF NOT EXISTS findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        surface TEXT NOT NULL,
        severity TEXT NOT NULL,
        title TEXT NOT NULL,
        paths TEXT NOT NULL,
        poc TEXT,
        auditor_verdict TEXT,
        verifier_verdict TEXT,
        final_verdict TEXT,
        status TEXT NOT NULL,
        created TEXT NOT NULL,
        round_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS verdicts (
        finding_id INTEGER NOT NULL REFERENCES findings(id),
        role TEXT NOT NULL CHECK (role IN ('auditor', 'verifier', 'human')),
        verdict TEXT NOT NULL,
        reason TEXT,
        ts TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (
            kind IN ('true-positive', 'false-positive', 'missed', 'out-of-scope')
        ),
        surface TEXT NOT NULL,
        pattern TEXT NOT NULL,
        guidance TEXT NOT NULL,
        source_finding_id INTEGER NOT NULL REFERENCES findings(id),
        approved_by TEXT,
        ts TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS roe_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        reason TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        ts TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS golden_paths (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('shell', 'flow', 'cron')),
        surface TEXT NOT NULL,
        command_or_flow TEXT NOT NULL,
        platform TEXT NOT NULL CHECK (platform IN ('any', 'posix', 'windows'))
            DEFAULT 'any',
        reason TEXT NOT NULL,
        source_finding_id INTEGER REFERENCES findings(id),
        approved_by TEXT,
        ts TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    )""",
    # The identity of a golden path, for the same reason findings have one: the
    # shipped corpus is imported by whoever sets a target up, and an import that
    # is not idempotent turns "run it again to be sure" into a duplicated fence
    # check whose two rows can disagree after one of them is switched off.
    #
    # ``platform`` is part of the identity, not incidental to it: the same command
    # text is a different claim on POSIX than on Windows, and collapsing them would
    # make importing a Windows row silently skip because a POSIX one is present.
    "CREATE UNIQUE INDEX IF NOT EXISTS golden_paths_identity "
    "ON golden_paths (kind, command_or_flow, platform)",
    "CREATE INDEX IF NOT EXISTS golden_paths_active ON golden_paths (active, platform)",
    # The dedupe identity of a finding, enforced by the storage layer rather than
    # only by the read-then-write in add_finding: two auditors racing the same
    # surface would otherwise both miss and both insert.
    "CREATE UNIQUE INDEX IF NOT EXISTS findings_identity " "ON findings (surface, title, paths)",
    "CREATE INDEX IF NOT EXISTS verdicts_by_finding ON verdicts (finding_id)",
    "CREATE INDEX IF NOT EXISTS lessons_active ON lessons (active, surface)",
)


def data_home() -> Path:
    """Where Kiro Crew keeps its data, resolved without importing the package.

    Mirrors ``pipeline-conductor/scripts/credit_spend.py``: a skill script cannot
    call ``kiro_crew.config.paths.data_home`` because it runs as a bare file
    outside the package.
    """
    env = os.environ.get("KIROCREW_HOME")
    return Path(env) if env else Path.home() / ".kiro" / "crew"


def default_db_path() -> Path:
    return data_home() / "security-conductor" / "findings.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_paths(paths: Iterable[str]) -> str:
    """The stored, comparable form of a finding's affected paths.

    Sorted and de-duplicated, so ``paths`` is itself the dedupe key: the
    identity is (surface, title, sorted paths), and normalising at write time
    turns that into one exact-match lookup (and one UNIQUE index) instead of a
    scan that re-normalises every candidate row.
    """
    cleaned = sorted({item.strip() for item in paths if item and item.strip()})
    return json.dumps(cleaned, sort_keys=True)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # NOT NULL + REFERENCES on lessons.source_finding_id is only a real
    # constraint with this pragma on; SQLite defaults it OFF per connection.
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:  # pragma: no cover - filesystem without WAL support
        # A network filesystem can refuse WAL. The ledger is correct without it
        # (one writer at a time), so degrade rather than refuse to open.
        pass
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """Create every table and index if absent; return the schema version.

    Idempotent and versioned: the version row is written once, so a future
    migration ladder has a value to branch on.

    The insert carries its own precondition (``WHERE NOT EXISTS``) instead of
    being gated by a Python read. A read-then-write here was racy on the ORDINARY
    path, because every command calls this first -- two parallel auditors running
    ``add-finding`` both saw an empty table and both inserted. One statement
    cannot interleave with itself, and the unique index in :data:`DDL` is the
    backstop for a row written any other way.
    """
    with conn:
        for statement in DDL:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version)"
            " SELECT ? WHERE NOT EXISTS (SELECT 1 FROM schema_version)",
            (SCHEMA_VERSION,),
        )
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if int(row["version"]) < SCHEMA_VERSION:
            # The ladder step, and it is ADDITIVE by construction: every statement
            # in :data:`DDL` is ``CREATE ... IF NOT EXISTS``, so an existing
            # database gains the new table on this same pass and opens, and the
            # version bump is the LAST write. An upgrade interrupted between the
            # two therefore leaves the version behind the tables, which the next
            # open retries harmlessly, rather than ahead of them, which would
            # record a migration that never ran.
            #
            # UPDATE rather than INSERT, because ``schema_version_single`` makes
            # the row a singleton -- a second row would be a constraint violation,
            # not a newer version. And the guard is ``<`` rather than ``!=`` so a
            # database written by a newer checkout, opened by this one, is left
            # alone instead of being walked backwards to a version whose tables
            # this code cannot recreate.
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            row = conn.execute("SELECT version FROM schema_version").fetchone()
    return int(row["version"])


def add_finding(
    conn: sqlite3.Connection,
    *,
    surface: str,
    title: str,
    severity: str,
    paths: Sequence[str],
    poc: str | None,
    round_id: str | None,
) -> tuple[int, bool]:
    """Insert a finding, or return the existing one. Returns ``(id, created)``.

    Dedupe identity is (surface, title, sorted paths): one finding per real
    defect, so a surface re-audited in a later round does not re-file what is
    already recorded. A hit returns the original id untouched — the earlier
    row's severity, PoC and verdict history are the record, and a re-report is
    not evidence to overwrite them with.
    """
    key = canonical_paths(paths)
    found = _find_by_identity(conn, surface, title, key)
    if found is not None:
        return found, False
    try:
        with conn:
            # `status` is written 'open' rather than taken from a flag. The column
            # is part of the RFC's schema, but M1 ships no command that reads it
            # and none that transitions it, so a creation-time knob would be a
            # setting nothing can act on. The milestone that adds the reader adds
            # the transition, and the flag with it.
            cursor = conn.execute(
                "INSERT INTO findings (surface, severity, title, paths, poc, status, created,"
                " round_id) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (surface, severity, title, key, poc, now_iso(), round_id),
            )
    except sqlite3.IntegrityError:
        # The identity index fired, so another writer inserted between the read
        # above and this insert. That is the race the index exists to catch, and
        # catching it is what makes dedupe hold under it: without this branch a
        # second auditor on the same surface gets a traceback where the contract
        # promises the existing id. Re-read rather than trusting the lost value.
        found = _find_by_identity(conn, surface, title, key)
        if found is None:  # pragma: no cover - a violation of some OTHER constraint
            raise
        return found, False
    return int(cursor.lastrowid or 0), True


def _find_by_identity(conn: sqlite3.Connection, surface: str, title: str, key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM findings WHERE surface = ? AND title = ? AND paths = ?",
        (surface, title, key),
    ).fetchone()
    return None if row is None else int(row["id"])


def fold_verdicts(conn: sqlite3.Connection, finding_id: int) -> dict[str, str | None]:
    """Recompute a finding's verdict columns from its append-only verdict rows.

    Per role the latest APPENDED row wins, and ``final_verdict`` takes the
    strongest role present per :data:`FOLD_ORDER`.

    Ordering is ``rowid`` alone -- deliberately NOT ``ts``. The table is
    append-only, so rowid already records the true sequence, while ``ts`` records
    only what the wall clock claimed at the time. Sorting by ``ts`` first made a
    clock rollback (an NTP correction, a restored VM snapshot, a container with a
    bad clock) reorder the fold: a superseded verdict carries the LATER timestamp,
    so it sorted last and won, silently reinstating a call a human had already
    overruled. rowid cannot roll back.

    rowid is reusable only after a DELETE, and there is no delete path to this
    table -- that is the append-only guarantee, and
    ``test_the_cli_carries_no_update_or_delete_path_to_verdicts`` asserts its
    absence rather than describing it.
    """
    latest: dict[str, str] = {}
    rows = conn.execute(
        "SELECT role, verdict FROM verdicts WHERE finding_id = ? ORDER BY rowid ASC",
        (finding_id,),
    ).fetchall()
    for row in rows:
        latest[str(row["role"])] = str(row["verdict"])
    final: str | None = None
    for role in FOLD_ORDER:
        if role in latest:
            final = latest[role]
    return {
        "auditor_verdict": latest.get("auditor"),
        "verifier_verdict": latest.get("verifier"),
        "final_verdict": final,
    }


def record_verdict(
    conn: sqlite3.Connection,
    *,
    finding_id: int,
    role: str,
    verdict: str,
    reason: str | None,
) -> dict[str, str | None]:
    """Append one verdict and refresh the finding's folded columns.

    INSERT only. The three columns on ``findings`` are a materialised view of
    the fold, never an independent assignment, so they cannot disagree with the
    rows they summarise.
    """
    with conn:
        conn.execute(
            "INSERT INTO verdicts (finding_id, role, verdict, reason, ts) VALUES (?, ?, ?, ?, ?)",
            (finding_id, role, verdict, reason, now_iso()),
        )
        folded = fold_verdicts(conn, finding_id)
        conn.execute(
            "UPDATE findings SET auditor_verdict = ?, verifier_verdict = ?, final_verdict = ?"
            " WHERE id = ?",
            (
                folded["auditor_verdict"],
                folded["verifier_verdict"],
                folded["final_verdict"],
                finding_id,
            ),
        )
    return folded


def golden_path_key(command_or_flow: str) -> str:
    """The stored, comparable form of a golden path's command or flow text.

    Stripped only. A shell command's INTERIOR spacing is part of the shape the
    deny fence classifies -- ``gh pr view;gh run list`` and ``gh pr view ; gh run
    list`` are different strings to a textual classifier -- so normalising it
    would make the corpus assert a shape the fence never sees.
    """
    return command_or_flow.strip()


def _find_golden_path(conn: sqlite3.Connection, kind: str, key: str, platform: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM golden_paths WHERE kind = ? AND command_or_flow = ? AND platform = ?",
        (kind, key, platform),
    ).fetchone()
    return None if row is None else int(row["id"])


def _insert_golden_path(
    conn: sqlite3.Connection,
    *,
    kind: str,
    surface: str,
    key: str,
    platform: str,
    reason: str,
    source_finding_id: int | None,
    approved_by: str | None,
    active: bool,
) -> int:
    """The INSERT alone, owning NO transaction. Returns the new row's id.

    Split out so a caller decides the transaction boundary. One row at a time wants
    its own (:func:`propose_golden_path`); a whole corpus wants exactly one for the
    lot (:func:`import_golden_paths`), and a helper that committed internally made
    that impossible -- ``with conn`` does not nest, so an inner block commits the
    outer one's work early and an interruption mid-import leaves a subset behind.

    Takes ``key`` rather than raw text: normalising is the caller's job here,
    because the caller also has to LOOK the key up first.
    """
    cursor = conn.execute(
        "INSERT INTO golden_paths (kind, surface, command_or_flow, platform, reason,"
        " source_finding_id, approved_by, ts, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            surface,
            key,
            platform,
            reason,
            source_finding_id,
            approved_by,
            now_iso(),
            1 if active else 0,
        ),
    )
    return int(cursor.lastrowid or 0)


def propose_golden_path(
    conn: sqlite3.Connection,
    *,
    kind: str,
    surface: str,
    command_or_flow: str,
    platform: str,
    reason: str,
    source_finding_id: int | None,
) -> tuple[int, bool]:
    """Insert an inert (``active=0``, unattributed) golden path, or return the
    existing one. Returns ``(id, created)``.

    Identity is (kind, command_or_flow, platform), so re-proposing a row the
    corpus already carries returns the original id untouched. A hit is NOT an
    update: the stored row may have been approved by someone, or deliberately
    switched off, and a re-proposal is not evidence to overwrite either of those
    with. Activation is :func:`approve_golden_path`'s write and nothing else's.
    """
    key = golden_path_key(command_or_flow)
    found = _find_golden_path(conn, kind, key, platform)
    if found is not None:
        return found, False
    try:
        with conn:
            new_id = _insert_golden_path(
                conn,
                kind=kind,
                surface=surface,
                key=key,
                platform=platform,
                reason=reason,
                source_finding_id=source_finding_id,
                approved_by=None,
                active=False,
            )
    except sqlite3.IntegrityError:
        # The identity index fired: another writer inserted between the read above
        # and this insert. Same race, and same resolution, as :func:`add_finding` --
        # re-read rather than trusting the value this call lost.
        found = _find_golden_path(conn, kind, key, platform)
        if found is None:  # pragma: no cover - a violation of some OTHER constraint
            raise
        return found, False
    return new_id, True


def _read_golden_path_state(
    conn: sqlite3.Connection, path_id: int
) -> tuple[bool, str | None] | None:
    """``(is_active, approved_by)`` for one golden path, or None when absent."""
    row = conn.execute(
        "SELECT active, approved_by FROM golden_paths WHERE id = ?", (path_id,)
    ).fetchone()
    if row is None:
        return None
    return bool(int(row["active"])), row["approved_by"]


def approve_golden_path(
    conn: sqlite3.Connection, *, path_id: int, approved_by: str
) -> tuple[str, str | None]:
    """Activate a proposed golden path. Returns ``(outcome, approver_on_record)``.

    Outcomes match :func:`approve_lesson` exactly -- ``approved``, ``missing``,
    ``already``, ``deactivated``, ``raced`` -- because the guarantee is the same
    one and a second vocabulary for it would be a second thing to keep aligned.

    Approval is WRITE-ONCE for the reason it is on lessons: ``approved_by`` names
    the human who accepted that this operation must keep working, and a golden
    path is what makes a fix fail review, so the attribution is the record that
    matters when a fix is rejected. The UPDATE's WHERE clause is the guarantee
    (``active = 0`` AND ``approved_by IS NULL``); the read above it only buys a
    better message. ``active = 0`` alone is satisfied by a row that was approved
    and later retired by hand, and re-approving that row would replace its
    original approver.
    """
    state = _read_golden_path_state(conn, path_id)
    if state is None:
        return "missing", None
    was_active, holder = state
    if was_active:
        return "already", holder
    if holder is not None:
        return "deactivated", holder
    with conn:
        cursor = conn.execute(
            "UPDATE golden_paths SET active = 1, approved_by = ?"
            " WHERE id = ? AND active = 0 AND approved_by IS NULL",
            (approved_by, path_id),
        )
    if cursor.rowcount != 1:
        raced = _read_golden_path_state(conn, path_id)
        return "raced", None if raced is None else raced[1]
    return "approved", approved_by


def validate_golden_path_row(row: Any, index: int) -> dict[str, Any]:
    """One corpus entry, checked and normalised, or ``ValueError`` naming its index.

    Every field is checked HERE rather than at the insert, so
    :func:`import_golden_paths` can validate a whole file before writing any of
    it. A half-imported corpus is the failure that matters: the rows that landed
    are a fence check the reviewer did not choose, and the ones that did not are
    invisible.
    """
    if not isinstance(row, dict):
        raise ValueError(f"entry {index}: expected an object, got {type(row).__name__}")
    # Every text field that is PRESENT must be a JSON string -- a null included.
    # Coercing with ``str()`` would store the Python repr of a list or a number --
    # ``"['gh', 'pr']"`` -- as a golden path, and the gate would then classify a
    # command nobody runs instead of refusing the row. A present ``null`` is the same
    # authoring mistake, not an omission: ``"platform": null`` must not widen a row to
    # every host, so only an ABSENT platform key takes the ``any`` default.
    text_fields = ("kind", "surface", "command_or_flow", "reason", "platform")
    wrong_type = [f for f in text_fields if f in row and not isinstance(row[f], str)]
    if wrong_type:
        raise ValueError(
            f"entry {index}: {', '.join(wrong_type)} must be a string, got"
            f" {', '.join(type(row[f]).__name__ for f in wrong_type)}"
        )
    missing = [
        f
        for f in ("kind", "surface", "command_or_flow", "reason")
        if not (row.get(f) or "").strip()
    ]
    if missing:
        raise ValueError(f"entry {index}: blank or missing {', '.join(missing)}")
    kind = row["kind"].strip()
    if kind not in GOLDEN_PATH_KINDS:
        raise ValueError(f"entry {index}: unknown kind {kind!r}")
    platform = (row["platform"] if "platform" in row else "any").strip()
    if platform not in GOLDEN_PATH_PLATFORMS:
        raise ValueError(f"entry {index}: unknown platform {platform!r}")
    source = row.get("source_finding_id")
    # ``bool`` is checked FIRST and separately, because it subclasses ``int``: an
    # ``isinstance(source, int)`` test accepts ``true``, and SQLite then stores it as
    # 1 -- so a corpus written with a JSON boolean silently attributed its golden
    # path to finding 1, with nothing reported and no way to notice.
    if source is not None and (isinstance(source, bool) or not isinstance(source, int)):
        raise ValueError(f"entry {index}: source_finding_id must be an integer or null")
    return {
        "kind": kind,
        "surface": row["surface"].strip(),
        "command_or_flow": golden_path_key(row["command_or_flow"]),
        "platform": platform,
        "reason": row["reason"].strip(),
        "source_finding_id": source,
    }


def load_golden_path_corpus(text: str) -> list[dict[str, Any]]:
    """The rows in a corpus file: an object carrying a ``golden_paths`` list.

    ONE accepted shape, the one the committed export is written in -- an object,
    so the file has a place for its ``_about`` prose beside the rows. This is also
    the ONE validation of the file: ``verify_fix.py`` reads the export through it
    too, so the gate and the import cannot judge the same row by different rules.
    """
    payload = json.loads(text)
    rows = payload.get("golden_paths") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("corpus must be a JSON object with a 'golden_paths' list")
    return [validate_golden_path_row(row, index) for index, row in enumerate(rows)]


def import_golden_paths(
    conn: sqlite3.Connection, rows: Sequence[dict[str, Any]], *, approved_by: str
) -> dict[str, int]:
    """Load a validated corpus in ONE transaction, skipping rows already carried.

    All-or-nothing, and that is why this does not call :func:`propose_golden_path` in a
    loop: that function owns a transaction per row, so an import interrupted between
    two of them left a SUBSET of the corpus committed -- a fence nobody chose, and
    silent, since the rows that never landed are invisible. One enclosing
    transaction makes an interrupted import a no-op instead.

    Idempotent by identity, so the same file imported twice is one corpus. A row
    that is already present is SKIPPED rather than refreshed: it may have been
    retired on purpose, and re-importing the file it came from is not a decision to
    bring it back.
    """
    imported = 0
    skipped = 0
    with conn:
        for row in rows:
            key = golden_path_key(row["command_or_flow"])
            if _find_golden_path(conn, row["kind"], key, row["platform"]) is not None:
                skipped += 1
                continue
            try:
                _insert_golden_path(
                    conn,
                    kind=row["kind"],
                    surface=row["surface"],
                    key=key,
                    platform=row["platform"],
                    reason=row["reason"],
                    source_finding_id=row["source_finding_id"],
                    approved_by=approved_by,
                    active=True,
                )
            except sqlite3.IntegrityError:
                # A concurrent writer inserted the same identity between the lookup
                # above and this insert. SQLite's default conflict resolution rolls
                # back the STATEMENT, not the transaction, so the enclosing block
                # stays usable and the rest of the corpus still lands atomically.
                skipped += 1
                continue
            imported += 1
    return {"imported": imported, "skipped": skipped, "total": len(rows)}


def _read_lesson_state(conn: sqlite3.Connection, lesson_id: int) -> tuple[bool, str | None] | None:
    """``(is_active, approved_by)`` for one lesson, or None when it does not exist."""
    row = conn.execute(
        "SELECT active, approved_by FROM lessons WHERE id = ?", (lesson_id,)
    ).fetchone()
    if row is None:
        return None
    return bool(int(row["active"])), row["approved_by"]


def approve_lesson(
    conn: sqlite3.Connection, *, lesson_id: int, approved_by: str
) -> tuple[str, str | None]:
    """Activate a proposed lesson. Returns ``(outcome, approver_on_record)``.

    Outcomes: ``approved`` (this call did it), ``missing`` (no such lesson),
    ``already`` (it is approved and active), ``deactivated`` (it was approved
    before and later switched off by hand), ``raced`` (it was approved between
    this call's read and its write).

    Approval is WRITE-ONCE. ``approved_by`` names the approval that let a lesson
    into an auditor's seed, so replacing it destroys the audit trail the human
    gate exists to leave -- and an unconditional UPDATE did exactly that on any
    re-run of the command.

    The UPDATE's WHERE clause is the guarantee -- ``active = 0`` AND
    ``approved_by IS NULL`` -- and the read above it only buys a better message.
    Both conditions are load-bearing. ``active = 0`` alone is satisfied by a
    lesson that was approved and then switched off by hand, which is the RFC's own
    revert path: that row still names its original approver, so re-approving it
    overwrote the very attribution this function exists to keep. Requiring
    ``approved_by IS NULL`` narrows the write to a lesson that has never been
    approved at all.

    Reactivating a deactivated lesson is therefore NOT this command's job -- it
    reports ``deactivated`` and names the holder, because the revert was a row
    edit and so is undoing it. That keeps one approver per lesson for its whole
    life instead of one per activation.

    A concurrent approval landing between the read and the write makes the UPDATE
    match no row, so the loser reports ``raced`` rather than printing a success
    line for a write that did not happen. Same shape as :func:`add_finding`'s
    dedupe race.

    ``ts`` is deliberately untouched -- it is when the lesson was PROPOSED, which
    is the recency :func:`seed_lessons` ranks by.
    """
    state = _read_lesson_state(conn, lesson_id)
    if state is None:
        return "missing", None
    was_active, holder = state
    if was_active:
        return "already", holder
    if holder is not None:
        # Inactive but already attributed: approved once, then switched off by
        # hand. Undoing that is a row edit too, not a second approval.
        return "deactivated", holder
    with conn:
        cursor = conn.execute(
            "UPDATE lessons SET active = 1, approved_by = ?"
            " WHERE id = ? AND active = 0 AND approved_by IS NULL",
            (approved_by, lesson_id),
        )
    if cursor.rowcount != 1:
        raced = _read_lesson_state(conn, lesson_id)
        return "raced", None if raced is None else raced[1]
    return "approved", approved_by


def seed_lessons(
    conn: sqlite3.Connection, *, surface: str, budget_bytes: int
) -> tuple[list[sqlite3.Row], int]:
    """The approved lessons to inject, in priority order, within the budget.

    Priority is surface match first, then recency. Truncation takes the longest
    fitting PREFIX rather than packing best-fit: skipping a lesson that does not
    fit to admit a lower-priority one silently reorders the ranking the caller
    asked for, and "top-N" would stop meaning top.

    "Recency" is measured by ``id`` -- the proposal order -- not by ``ts``, for the
    same reason :func:`fold_verdicts` orders by rowid: a lesson's id is assigned
    when it is proposed and cannot move, while ``ts`` is only what the clock said.
    The two agree on a healthy host and disagree exactly when the clock has rolled
    back, and there the id is the one that is still right. A lesson therefore
    cannot be re-prioritised by editing its timestamp; if a human-settable
    priority is ever wanted, it belongs in a column that says so rather than in a
    field every reader also interprets as "when".
    """
    rows = conn.execute(
        "SELECT * FROM lessons WHERE active = 1 ORDER BY (surface = ?) DESC, id DESC",
        (surface,),
    ).fetchall()
    chosen: list[sqlite3.Row] = []
    used = 0
    for row in rows:
        cost = len(format_lesson(row).encode("utf-8")) + 1  # + the newline it is printed with
        if used + cost > budget_bytes:
            break
        chosen.append(row)
        used += cost
    return chosen, used


def format_lesson(row: sqlite3.Row) -> str:
    """One lesson as one seed line, carrying the finding that earned it."""
    return (
        f"- [{row['kind']}] {row['surface']}: {row['pattern']} -> {row['guidance']}"
        f" (finding #{row['source_finding_id']})"
    )


def export_roe(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """The active rules of engagement, grouped by field.

    An export, never the source of truth: ``scope_check.py`` reads the rows.
    Inactive rules are absent rather than present-and-flagged, so a consumer
    cannot mistake a reverted rule for a live one by ignoring a field.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT id, field, value, reason, approved_by, ts FROM roe_rules"
        " WHERE active = 1 ORDER BY field ASC, id ASC"
    ).fetchall()
    for row in rows:
        grouped.setdefault(str(row["field"]), []).append(
            {
                "id": int(row["id"]),
                "value": row["value"],
                "reason": row["reason"],
                "approved_by": row["approved_by"],
                "ts": row["ts"],
            }
        )
    return grouped


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _require_finding(conn: sqlite3.Connection, finding_id: int) -> bool:
    return conn.execute("SELECT 1 FROM findings WHERE id = ?", (finding_id,)).fetchone() is not None


def _emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def nonblank(value: str) -> str:
    """An argparse ``type`` for a required text argument: strip, refuse empty.

    ``required=True`` only asserts the FLAG is present, so ``--approved-by ""``
    satisfied it and wrote an active lesson (or an active rule) with no
    attribution at all -- defeating the guarantee that every approval names its
    approver, while still passing the argument parser.

    Applied at the declaration rather than at each write, because the next
    required argument someone adds would otherwise have to remember this. It
    strips too: a trailing space in an approver name is noise, not identity.
    """
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError("must not be blank or whitespace-only")
    return stripped


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Security conductor findings ledger")
    # ONE position, before the subcommand, and the usage block says so. Accepting
    # it on both sides was tried and removed: it needs a second declaration whose
    # default must be argparse.SUPPRESS (with `None` the subparser silently
    # overwrites a value given before the subcommand every time it is omitted
    # after it), which is a subtlety every later reader has to re-derive for a
    # flag whose normal case is to be omitted entirely.
    parser.add_argument("--db", default=None, help="ledger path (default: data home)")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text)

    command("init", "create or upgrade the schema")

    add = command("add-finding", "record a candidate finding (deduped)")
    add.add_argument("--surface", required=True, type=nonblank)
    add.add_argument("--title", required=True, type=nonblank)
    add.add_argument("--severity", required=True, type=nonblank)
    add.add_argument("--path", action="append", default=[], dest="paths")
    add.add_argument("--poc", default=None)
    add.add_argument("--round-id", default=None)

    verdict = command("record-verdict", "append one verdict (never overwrites)")
    verdict.add_argument("--finding", required=True, type=int)
    verdict.add_argument("--role", required=True, type=nonblank)
    verdict.add_argument("--verdict", required=True, type=nonblank)
    verdict.add_argument("--reason", default=None)

    propose = command("propose-lesson", "propose a lesson (inert until approved)")
    propose.add_argument("--kind", required=True, type=nonblank)
    propose.add_argument("--surface", required=True, type=nonblank)
    propose.add_argument("--pattern", required=True, type=nonblank)
    propose.add_argument("--guidance", required=True, type=nonblank)
    propose.add_argument("--source-finding", required=True, type=int)

    approve = command("approve-lesson", "activate a proposed lesson")
    approve.add_argument("--id", required=True, type=int)
    approve.add_argument("--approved-by", required=True, type=nonblank)

    rule = command("add-rule", "add a rules-of-engagement row")
    rule.add_argument("--field", required=True, type=nonblank)
    rule.add_argument("--value", required=True, type=nonblank)
    # Both required AND non-blank: the RFC makes every widening of what an auditor
    # may do attributable, so a rule with no reason or no approver is not a rule --
    # and `required=True` alone would accept an empty string for either.
    rule.add_argument("--reason", required=True, type=nonblank)
    rule.add_argument("--approved-by", required=True, type=nonblank)

    command("export-roe", "emit the active rules as JSON, grouped by field")

    listing = command("list", "dump a table as JSON")
    listing.add_argument("table", choices=LIST_TABLES)

    proposed = command("propose-golden-path", "propose a golden path (inert until approved)")
    proposed.add_argument("--kind", required=True, type=nonblank)
    proposed.add_argument("--surface", required=True, type=nonblank)
    # ``dest`` is explicit because ``args.command`` already holds the subcommand
    # name: the natural flag name for this column would shadow it.
    proposed.add_argument("--command", required=True, type=nonblank, dest="command_or_flow")
    proposed.add_argument("--platform", default="any", type=nonblank)
    proposed.add_argument("--reason", required=True, type=nonblank)
    proposed.add_argument("--source-finding", default=None, type=int)

    approve_path = command("approve-golden-path", "activate a proposed golden path")
    approve_path.add_argument("--id", required=True, type=int)
    approve_path.add_argument("--approved-by", required=True, type=nonblank)

    imported = command("import-golden-paths", "load a golden-path corpus file (idempotent)")
    imported.add_argument("file")
    imported.add_argument("--approved-by", required=True, type=nonblank)

    seed = command("seed-lessons", "approved lessons for a seed message")
    seed.add_argument("--surface", required=True, type=nonblank)
    seed.add_argument("--budget-bytes", required=True, type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()
    conn = connect(db_path)
    try:
        version = init_schema(conn)
        return _dispatch(conn, args, db_path, version)
    finally:
        conn.close()


def _dispatch(
    conn: sqlite3.Connection, args: argparse.Namespace, db_path: Path, version: int
) -> int:
    if args.command == "init":
        _emit({"db": str(db_path), "schema_version": version})
        return 0

    if args.command == "add-finding":
        finding_id, created = add_finding(
            conn,
            surface=args.surface,
            title=args.title,
            severity=args.severity,
            paths=args.paths,
            poc=args.poc,
            round_id=args.round_id,
        )
        _emit({"id": finding_id, "created": created})
        return 0

    if args.command == "record-verdict":
        if args.role not in ROLES:
            print(
                f"unknown role {args.role!r}; expected one of {', '.join(ROLES)}",
                file=sys.stderr,
            )
            return 2
        if not _require_finding(conn, args.finding):
            print(f"no finding with id {args.finding}", file=sys.stderr)
            return 2
        folded = record_verdict(
            conn,
            finding_id=args.finding,
            role=args.role,
            verdict=args.verdict,
            reason=args.reason,
        )
        _emit({"finding_id": args.finding, **folded})
        return 0

    if args.command == "propose-lesson":
        if args.kind not in LESSON_KINDS:
            print(
                f"unknown kind {args.kind!r}; expected one of {', '.join(LESSON_KINDS)}",
                file=sys.stderr,
            )
            return 2
        if not _require_finding(conn, args.source_finding):
            # Checked here as well as by the foreign key so the operator gets a
            # sentence instead of an IntegrityError traceback.
            print(
                f"no finding with id {args.source_finding}; every lesson must cite one",
                file=sys.stderr,
            )
            return 2
        with conn:
            cursor = conn.execute(
                "INSERT INTO lessons (kind, surface, pattern, guidance, source_finding_id, ts,"
                " active) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    args.kind,
                    args.surface,
                    args.pattern,
                    args.guidance,
                    args.source_finding,
                    now_iso(),
                ),
            )
        _emit({"id": int(cursor.lastrowid or 0), "active": 0})
        return 0

    if args.command == "approve-lesson":
        outcome, holder = approve_lesson(conn, lesson_id=args.id, approved_by=args.approved_by)
        if outcome == "missing":
            print(f"no lesson with id {args.id}", file=sys.stderr)
            return 2
        if outcome == "already":
            # Reporting the holder rather than a bare refusal is the point: the
            # operator asked who may approve this, and someone already did.
            print(
                f"lesson {args.id} was already approved by {holder!r};"
                " approval is recorded once and is never replaced",
                file=sys.stderr,
            )
            return 2
        if outcome == "deactivated":
            print(
                f"lesson {args.id} was approved by {holder!r} and later deactivated;"
                " approval is recorded once, so re-enable it with"
                f" 'UPDATE lessons SET active = 1 WHERE id = {args.id}'"
                " rather than approving it again",
                file=sys.stderr,
            )
            return 2
        if outcome == "raced":
            print(
                f"lesson {args.id} was approved concurrently by {holder!r};"
                " approval is recorded once",
                file=sys.stderr,
            )
            return 2
        _emit({"id": args.id, "active": 1, "approved_by": args.approved_by})
        return 0

    if args.command == "propose-golden-path":
        if args.kind not in GOLDEN_PATH_KINDS:
            print(
                f"unknown kind {args.kind!r}; expected one of {', '.join(GOLDEN_PATH_KINDS)}",
                file=sys.stderr,
            )
            return 2
        if args.platform not in GOLDEN_PATH_PLATFORMS:
            print(
                f"unknown platform {args.platform!r};"
                f" expected one of {', '.join(GOLDEN_PATH_PLATFORMS)}",
                file=sys.stderr,
            )
            return 2
        if args.source_finding is not None and not _require_finding(conn, args.source_finding):
            # Checked here as well as by the foreign key so the operator gets a
            # sentence instead of an IntegrityError traceback.
            print(f"no finding with id {args.source_finding}", file=sys.stderr)
            return 2
        path_id, created = propose_golden_path(
            conn,
            kind=args.kind,
            surface=args.surface,
            command_or_flow=args.command_or_flow,
            platform=args.platform,
            reason=args.reason,
            source_finding_id=args.source_finding,
        )
        # On a dedupe hit the row is returned untouched, so the state reported is the
        # STORED row's, read back -- not the inert state this call asked for. An
        # operator re-proposing an identity that is already approved, or one that was
        # retired by hand, is told what the table holds.
        state = _read_golden_path_state(conn, path_id)
        active = int(state[0]) if state is not None else 0
        _emit({"id": path_id, "created": created, "active": active})
        return 0

    if args.command == "approve-golden-path":
        outcome, holder = approve_golden_path(conn, path_id=args.id, approved_by=args.approved_by)
        if outcome == "missing":
            print(f"no golden path with id {args.id}", file=sys.stderr)
            return 2
        if outcome == "already":
            print(
                f"golden path {args.id} was already approved by {holder!r};"
                " approval is recorded once and is never replaced",
                file=sys.stderr,
            )
            return 2
        if outcome == "deactivated":
            print(
                f"golden path {args.id} was approved by {holder!r} and later retired;"
                " approval is recorded once, so re-enable it with"
                f" 'UPDATE golden_paths SET active = 1 WHERE id = {args.id}'"
                " rather than approving it again",
                file=sys.stderr,
            )
            return 2
        if outcome == "raced":
            print(
                f"golden path {args.id} was approved concurrently by {holder!r};"
                " approval is recorded once",
                file=sys.stderr,
            )
            return 2
        _emit({"id": args.id, "active": 1, "approved_by": args.approved_by})
        return 0

    if args.command == "import-golden-paths":
        corpus = Path(args.file)
        try:
            rows = load_golden_path_corpus(corpus.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"cannot read corpus {corpus}: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            # One member, because ``json.JSONDecodeError`` IS a ``ValueError`` --
            # both a bad JSON document and a row that fails
            # :func:`validate_golden_path_row` arrive here.
            #
            # Nothing is written on a malformed file: the whole corpus is
            # validated before the first insert, so a rejected file leaves the
            # ledger exactly as it was rather than half-loaded.
            print(f"malformed corpus {corpus}: {exc}", file=sys.stderr)
            return 2
        for row in rows:
            if row["source_finding_id"] is not None and not _require_finding(
                conn, row["source_finding_id"]
            ):
                print(
                    f"no finding with id {row['source_finding_id']}"
                    f" (cited by {row['command_or_flow']!r})",
                    file=sys.stderr,
                )
                return 2
        _emit(import_golden_paths(conn, rows, approved_by=args.approved_by))
        return 0

    if args.command == "add-rule":
        with conn:
            cursor = conn.execute(
                "INSERT INTO roe_rules (field, value, reason, approved_by, ts, active)"
                " VALUES (?, ?, ?, ?, ?, 1)",
                (args.field, args.value, args.reason, args.approved_by, now_iso()),
            )
        _emit({"id": int(cursor.lastrowid or 0), "field": args.field, "active": 1})
        return 0

    if args.command == "export-roe":
        _emit(export_roe(conn))
        return 0

    if args.command == "list":
        # A literal query per choice rather than an interpolated table name: the
        # argparse `choices` already bounds the value, but the safety of the SQL
        # should be readable AT the query, not inferred from an argument
        # declaration two hundred lines away.
        rows = conn.execute(LIST_QUERIES[args.table]).fetchall()
        _emit([row_to_dict(row) for row in rows])
        return 0

    if args.command == "seed-lessons":
        if args.budget_bytes < 0:
            print("--budget-bytes must not be negative", file=sys.stderr)
            return 2
        chosen, used = seed_lessons(conn, surface=args.surface, budget_bytes=args.budget_bytes)
        for row in chosen:
            print(format_lesson(row))
        if not chosen:
            # Report the shortfall on stderr so stdout stays exactly the seed
            # text a caller splices into a message, empty when nothing fits.
            # An empty ledger and a budget too small for the top lesson are
            # different operator problems, so they do not share a message.
            approved = conn.execute("SELECT COUNT(*) FROM lessons WHERE active = 1").fetchone()
            if int(approved[0]) == 0:
                print("no approved lesson to seed", file=sys.stderr)
            else:
                print(f"no approved lesson fits {args.budget_bytes} bytes", file=sys.stderr)
        print(f"{len(chosen)} lesson(s), {used} bytes", file=sys.stderr)
        return 0

    print(f"unknown command {args.command!r}", file=sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())

"""Neutralise credential material in a bundle that is about to leave the host.

An off-host copy crosses a trust boundary the local archive does not: it lands in object
storage, and every principal that can read the bucket can read it. This module rewrites a
STAGED COPY of a bundle so the bytes that leave carry no usable credentials, and it is the
only place in the backup path that does so.

**It is lossy on purpose, and the loss is the point.** A redacted bundle restores to a
working shape but not to a working credential: the token field is present and inert, so the
operator re-enters it rather than discovering an empty file. That tradeoff is a deliberate
product decision, and `backup.redact_uploads` exists to reverse it for an operator who
would rather have a bundle that restores complete.

Two properties make the difference between a redacted bundle and a broken one:

* **Structure survives.** The redactors substitute a tag for a match, so they CHANGE
  LENGTH. Running them over the bytes of a SQLite file does not produce a database with
  dead credentials, it produces a file SQLite cannot open — which the restore path then
  correctly refuses as corrupt. Databases are therefore redacted through SQL, value by
  value, so what is written back is still a database.
* **The bundle says so.** The manifest records that this copy was redacted and what was
  touched, so a restore can tell the operator their credentials are inert instead of
  letting them find out when something fails to authenticate.
"""

from __future__ import annotations

import json
import re
import shutil
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew.config.loader import config_dir
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

try:  # pragma: no cover - exercised by whichever binding is installed
    import pysqlite3 as sqlite3  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import sqlite3  # type: ignore[no-redef]

# Suffixes read as text. Everything else is either a database (handled through SQL) or
# opaque bytes, and guessing at an unknown binary format is how a bundle gets corrupted.
# Whether a file is text is decided by DECODING it, not by its name. A workspace holds
# whatever the operator put there — source files, csv, html, notes with no extension — and
# a suffix allowlist silently classified all of those as opaque.

# Files whose whole purpose is to be secret. There is no redacted form of a key that is
# still the key, so they are DROPPED from an outbound copy rather than blanked: a present
# but inert HMAC key would be indistinguishable from a rotated one.
# Bundle-RELATIVE paths, not basenames. A workspace holds whatever the operator put
# there, so matching on a bare name would delete their own `telemetry_salt` or
# `memory_index.db` from the off-host copy and make a restore quietly incomplete. These
# are the product's own files, and the product puts them at the bundle root.
_DROP_ENTIRELY = frozenset({"telemetry_salt", "sel_hmac.key"})

# Derived indexes. An FTS index mirrors content that is itself being redacted, so redacting
# it row by row would leave the index disagreeing with the table it indexes. Restore already
# handles an absent index by telling the operator to rebuild it, so absence is the honest
# state and the existing path carries it.
_DERIVED_INDEXES = frozenset({"memory_index.db"})

# Databases the product itself ships, by bundle-relative path. Only these may be DROPPED
# when they cannot be proven redacted: their absence is a state restore already reports.
# Any other `.db` is the operator's, so an unprovable one refuses the upload instead.
# A settled database needs two passes (one to clean, one to prove nothing moved). The
# rest of the budget is for chained triggers; past it, the database is not settling.
_MAX_SETTLE_PASSES = 10

_PRODUCT_DATABASES = frozenset({"memory.db", "workspace/knowledge/knowledge.db"})


class _SchemaCarriesCredential(Exception):
    """A credential sits in the schema itself, where no value rewrite can reach it.

    Rows can be rewritten in place. DDL cannot: changing it means rebuilding the object
    or enabling `writable_schema`, which risks leaving a file SQLite will not open. So a
    database in this state is one the pass cannot clean, and it is handled as such.
    """


class _Unprovable(Exception):
    """A file that is not the product's own and cannot be proven free of credentials."""


class _PayloadUnprovable(Exception):
    """A database this backup exists to carry, which cannot be proven clean."""


class OpaqueFilesPresent(Exception):
    """Files that are not text, so the pass cannot show them free of credentials.

    Raised instead of deleting them: an operator's own file missing from a restore that
    reported success is worse than an upload that refuses and says which files to deal
    with. Public because the upload path reports it to the operator.
    """

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(", ".join(paths))


class PayloadDatabaseUnprovable(Exception):
    """A database the backup EXISTS to carry cannot be shown free of credentials.

    Separate from `OpaqueFilesPresent` because the trade is not the operator's to make
    here: deleting `memory.db` and uploading the remainder produces an off-host copy that
    reports success and restores nothing, which is discovered only when the machine it
    came from is gone. Refusing names the database instead, and the local archive is
    untouched either way. Public because the upload path reports it to the operator.
    """

    def __init__(self, paths: list[str], details: list[str] | None = None) -> None:
        self.paths = paths
        # WHY each one could not be proven clean, not just which. A refusal the operator
        # cannot act on is a dead end: corruption, an unaddressable table and a schema
        # that carries the credential need different responses.
        self.details = details or list(paths)
        super().__init__(", ".join(self.details))


class _FileUnreadable(OSError):
    """A file that cannot be read at all, so nothing about it can be established.

    An `OSError` so the upload path's IO handler reports it as a refusal instead of
    letting it escape as a traceback -- the pass cannot prove a file it never read.
    """


class _TableNotInspectable(Exception):
    """A table this pass cannot read row-by-row, so its database cannot be cleared."""


@dataclass
class RedactionReport:
    """What the pass changed, so the operator can judge it rather than trust it."""

    replacements: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    rebuilt_indexes: list[str] = field(default_factory=list)
    skipped_unreadable: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.replacements.values())

    def as_manifest_entry(self) -> dict[str, object]:
        return {
            "redacted": True,
            "replacements": dict(sorted(self.replacements.items())),
            "dropped": sorted(self.dropped),
            "indexes_needing_rebuild": sorted(self.rebuilt_indexes),
            "skipped_unreadable": sorted(self.skipped_unreadable),
        }


# Credentials the shared redactors do not recognise, closed HERE rather than by widening
# them. Those run over live model output and tool results, where a false positive corrupts
# what the operator is reading; this pass runs over a throwaway copy on its way off the
# host, where a false positive costs one note's content and the complete local archive is
# untouched. So the two boundaries can afford different thresholds, and this is the one
# that can afford to guess.
#
# The shapes below are the ones the shared set misses. Two are vendor formats it has no
# pattern for. The third is the general case its assignment rule only half-covers: that
# rule fires on specific NAMES with vendor-shaped values, so `aws_secret_access_key = "…"`
# is caught while `bot_token = "…"` with an opaque value is not -- a name list only ever
# holds the names someone thought of, and an operator's own notes hold the rest.
_SENSITIVE_FIELD = (
    r"(?:api[-_ ]?key|secret[-_ ]?key|access[-_ ]?key|private[-_ ]?key|client[-_ ]?secret"
    r"|bot[-_ ]?token|auth[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|session[-_ ]?key"
    r"|signing[-_ ]?key|passwd|password|passphrase|credential|secret|token)"
)

# A quoted value long enough to be a real secret. The floor keeps `"password": ""` and
# obvious placeholders from being rewritten, which would make a diff of the outbound copy
# unreadable without protecting anything.
_MIN_SECRET_LEN = 12

# The value class excludes whitespace, and the replacement tag contains a space, so a value
# this pass has already replaced cannot match again. That is what makes re-running it a
# no-op, which the row scan depends on: it repeats until a pass changes nothing, so a rule
# that rewrote its own output would never settle and would refuse every database. The
# property is pinned by a test rather than left to whoever next edits the tag.

_STRUCTURED_CREDENTIAL = re.compile(
    # `"token": "value"` (JSON) and `token = "value"` / `token: value` (config, YAML).
    rf'(?i)(["\']?{_SENSITIVE_FIELD}["\']?\s*[:=]\s*)'
    rf'(["\']?)([^\s"\',}}\]]{{{_MIN_SECRET_LEN},}})(\2)'
)

# Three base64url segments with a dot between them: Discord bot tokens and JWTs both.
# Both ARE bearer credentials, so matching both is the intent, not collateral.
_DOTTED_BEARER = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{25,}\b")

_TAG = "[REDACTED: credential]"


def _scrub_unrecognised(text: str) -> tuple[str, int]:
    """The shapes the shared redactors do not match. Returns (cleaned, replacements)."""
    hits = 0

    def _field(m: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return f"{m.group(1)}{m.group(2)}{_TAG}{m.group(4)}"

    cleaned, _ = _STRUCTURED_CREDENTIAL.subn(_field, text)

    def _bearer(m: "re.Match[str]") -> str:
        nonlocal hits
        hits += 1
        return _TAG

    cleaned = _DOTTED_BEARER.sub(_bearer, cleaned)
    return cleaned, hits


def redaction_switch_path() -> "Path":
    """Where the outbound-redaction opt-in lives: inside the keystone backup directory.

    NOT in ``config.json``. That file is agent-readable and agent-WRITABLE -- an
    auto-approved shell can write it, which is why this repo already keeps its other
    security ceilings (the deny list, the computer-use enable) out of it. The backup
    directory is already fenced on account of the DESTINATION record, and the switch that
    decides what the uploader does with an operator's files belongs behind the same fence.

    The fence is kept even though redaction is no longer the security boundary, because it
    now decides whether the uploader REWRITES the operator's files, and an agent that could
    flip it on could corrupt an off-host copy just as surely as one flipping it off could
    publish a credential.

    It lives in this module rather than beside the uploader because the switch belongs with
    the thing it switches, and because this module is already a registered redaction sink.

    Absent means no rewriting, which is the common case and needs no file. The operator
    writes it out of band; no agent tool or shell form can.
    """
    return config_dir() / "backup" / "redaction.json"


class RedactionSwitchUnreadable(Exception):
    """The switch file exists but its contents cannot be read as a decision."""


def outbound_redaction_enabled() -> bool:
    """True only when the operator has explicitly opted IN.

    Off by default. What guards the bundle is that the destination is owner-only and
    re-verified at every upload -- every public-access block, default encryption, ACLs
    disabled, versioning, no bucket policy, and the object write pinned to the expected
    owner. Redaction sits on top of that, and it is not free: it REWRITES the operator's
    files, and a credential replacement is a variable-length edit, so any format whose
    structure depends on byte offsets comes out the other side invalid. Paying that by
    default to re-protect a copy only the owner can read is the wrong trade.

    Four cases, none of them a silent misreading:

    * absent -- off. The common case, and the reason the default needs no file.
    * exactly ``true`` -- on.
    * exactly ``false`` -- off, and saying so explicitly is allowed.
    * present but unreadable, the wrong shape, or some other value -- neither. The operator
      wrote this file on purpose, so both silent answers betray something they configured:
      off ignores a request to scrub, on rewrites files they may not have meant to touch.
      Raising here makes the upload refuse and name the file instead of guessing.
    """
    path = redaction_switch_path()
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise RedactionSwitchUnreadable(f"{path} could not be read as JSON ({e})") from e
    if not isinstance(raw, dict):
        raise RedactionSwitchUnreadable(f"{path} holds a JSON {type(raw).__name__}, not an object")
    value = raw.get("redact_uploads")
    if value is True:
        return True
    if value is False or value is None:
        return False
    # A string "true" or a 1 is a config mistake. Resolving it either way silently would
    # decide, on the operator's behalf, whether their files get rewritten.
    raise RedactionSwitchUnreadable(
        f"{path} sets redact_uploads to {value!r}; it must be true or false"
    )


def _scrub(text: str) -> tuple[str, int]:
    """Both mandatory outbound redactors, in the order the rest of the repo applies them.

    The shared pair runs first so its warnings stay the primary signal; the egress-only
    pass then closes the shapes it does not recognise.
    """
    cleaned, cred_warnings = redact_credentials(text)
    cleaned, url_warnings = redact_exfiltration_urls(cleaned)
    cleaned, extra = _scrub_unrecognised(cleaned)
    return cleaned, len(cred_warnings) + len(url_warnings) + extra


def _redact_text_file(path: Path, report: RedactionReport, rel: str) -> bool:
    """Redact *path* as UTF-8 text. Returns False when it cannot be rewritten as text.

    A `UnicodeDecodeError` is the answer to "is this text", not a failure: the caller
    refuses the upload for those rather than removing the file.

    Decoding is necessary evidence of text and not sufficient. NUL is a legal code point,
    so a NUL-padded binary whose other bytes are ASCII decodes cleanly -- a tar of text
    files is exactly that shape. Replacing a credential is a VARIABLE-LENGTH edit, so
    rewriting such a file moves every following byte and the structure is destroyed; the
    operator restores a file that is no longer a valid archive.

    Gated on `hits` rather than on NUL alone, because the hazard lives only on the branch
    that WRITES. A NUL-bearing file with no credential in it is never rewritten, so it is
    handed over byte-for-byte and refusing it would cost an upload for a corruption that
    cannot occur. What is refused is the pair: content this pass would have to rewrite,
    in a file it cannot rewrite safely.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    except OSError as e:
        raise _FileUnreadable(f"{rel}: {e}") from e
    cleaned, hits = _scrub(original)
    if hits:
        if "\x00" in original:
            return False
        path.write_text(cleaned, encoding="utf-8")
        report.replacements[rel] = report.replacements.get(rel, 0) + hits
    return True


# The pure index internals. `_content` is deliberately NOT here: for a standard fts5 table
# it holds the indexed PLAINTEXT and has a rowid, so it must be scanned like any other
# table. These four hold only index structure and are regenerated by the rebuild.
_FTS_INDEX_SUFFIXES = ("data", "idx", "docsize", "config")


def _fts_layout(conn: "sqlite3.Connection") -> tuple[list[str], set[str]]:
    """The FTS virtual tables, and the tables the row scan must not touch.

    Shadow tables are FTS5's private storage. Two of them (`_config`, `_idx`) are
    `WITHOUT ROWID`, so a row scan cannot address them — and refusing the database on that
    basis would delete every knowledge library from the outbound copy, which is how a
    fail-closed rule turns into data loss.

    They are also DERIVED: `_data`, `_idx` and `_docsize` hold the inverted index, so a
    credential survives there as index structure even after the text it came from is
    cleaned. Skipping them is therefore not enough on its own — the index is REBUILT from
    the redacted content afterwards, which is what actually removes the term.

    Identified positively from each virtual table's own name, not by pattern-matching
    every table that happens to end in `_idx`.
    """
    fts: list[str] = []
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table'"
    ).fetchall():
        if sql and "USING fts" in sql.replace("using fts", "USING fts"):
            fts.append(name)
    skip = {f"{v}_{suffix}" for v in fts for suffix in _FTS_INDEX_SUFFIXES}
    # The virtual tables themselves are skipped too. Writing through one is REFUSED for an
    # external-content table (`content=items`), which is the shape this product actually
    # uses, and redundant for a standard one whose text is in `_content`. Redacting the
    # table that owns the text and rebuilding from it works for both, with no dependence
    # on which table the scan reaches first.
    skip.update(fts)
    return fts, skip


def _refuse_credential_in_schema(conn: sqlite3.Connection, rel: str) -> None:
    """A row scan never sees the DDL, and a credential can be written into it.

    A column DEFAULT, a VIEW's select list and a TRIGGER body are all stored as schema
    text, so a key placed in any of them survives a pass that only rewrites values.
    """
    for (sql,) in conn.execute("SELECT sql FROM sqlite_schema WHERE sql IS NOT NULL").fetchall():
        _, hits = _scrub(str(sql))
        if hits:
            raise _SchemaCarriesCredential(rel)


def _quote_ident(name: str) -> str:
    """Return *name* as a SQLite quoted identifier, safe to interpolate.

    Wrapping an identifier in double quotes is not enough on its own: SQLite ends the
    identifier at the first unescaped quote, so a column named `x" FROM other --` closes
    the quote and the rest becomes syntax. The scanned tables and columns are read from
    each database's OWN schema, and this pass deliberately opens databases it does not
    own — an operator's tree can hold an imported `.db` whose identifiers were chosen by
    whoever built it — so the names reaching these statements are not this product's to
    assume well-formed. A rewritten SELECT reads a different table, so the pass reports a
    clean scan of rows it never examined and the credential it exists to remove ships.

    SQLite escapes an embedded double quote by doubling it, which is what this does. It is
    NOT a validity check: an identifier this product does not recognise is still perfectly
    legal SQLite, and refusing it would refuse databases the operator can legitimately
    hold. Quoting makes any name mean itself, which is the property the callers need.
    """
    return '"' + name.replace('"', '""') + '"'


# SQLite's row id answers to three names, and a DECLARED column may take any of them.
# Ordered by how likely a schema is to have taken the name for itself.
_ROWID_ALIASES = ("rowid", "_rowid_", "oid")


def _rowid_alias(cols: list[str]) -> str:
    """Return a row-id alias *cols* does not shadow, or refuse the table.

    `rowid` is this pass's update handle, and it is not a reserved word: a table may
    declare a column called `rowid`, and then `rowid` in a statement means THAT COLUMN.
    The read still succeeds and the UPDATE still applies, so nothing raises -- it just
    matches on the wrong thing. Where the shadowing column holds duplicate values, one
    row's replacement is written to every row sharing its value, so redacting a single
    credential silently overwrites unrelated rows. That is data loss inflicted by the
    component whose purpose is to protect the copy, and it lands in the archive that
    exists because the original may be gone.

    Shadowing is checked case-insensitively because SQLite identifiers are: a column
    named `RowID` shadows `rowid` just as completely as a lowercase one.

    Only tables that shadow ALL THREE aliases are refused, which keeps a database that
    merely happens to have a `rowid` column redactable. Refusal reuses this module's
    existing rule for a table it cannot read row-by-row -- the DATABASE is dropped and
    the upload refuses -- because a table that cannot be updated by a known-unique
    handle cannot be shown free of credentials either, and skipping it would upload the
    very rows this pass exists to clean.
    """
    taken = {c.casefold() for c in cols}
    for alias in _ROWID_ALIASES:
        if alias not in taken:
            return alias
    raise _TableNotInspectable(
        "every row-id alias (" + ", ".join(_ROWID_ALIASES) + ") is shadowed by a declared "
        "column, leaving no unique handle to update rows by"
    )


def _redact_database(path: Path, report: RedactionReport, rel: str, *, product: bool) -> None:
    """Rewrite credential-bearing VALUES in place, leaving a valid database behind.

    Every column is read and the decision is made on the VALUE's type, not the column's
    declared one. SQLite affinity is advisory — a column declared `BLOB`, or declared with
    no type at all, holds a Python `str` perfectly well — so filtering on the declaration
    would skip exactly the places a credential is least expected and most likely to sit.
    A value is written back only when it changed, so an untouched database keeps its pages.
    """
    try:
        with closing(sqlite3.connect(str(path))) as conn:
            _refuse_credential_in_schema(conn, rel)
            fts_tables, skip_tables = _fts_layout(conn)
            tables = [
                r[0]
                for r in conn.execute(
                    # `sqlite_schema` is the current name for the schema table, and the
                    # one this codebase already queries elsewhere.
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            hits = 0
            # Scanned to a FIXPOINT rather than once. An UPDATE fires this database's
            # triggers, and a trigger can copy the pre-update value somewhere the scan
            # has already been — so one pass can leave a credential behind in a table it
            # already cleaned. Each pass reports its own replacements; zero means nothing
            # moved and the database is settled. Refusing all triggers instead would
            # reject the product's own full-text schema, which is maintained by them.
            for _ in range(_MAX_SETTLE_PASSES):
                pass_hits = 0
                for table in tables:
                    if table in skip_tables:
                        # FTS5's private storage: derived from the content table, regenerated
                        # by the rebuild below. Two of these have no rowid, so scanning them
                        # is impossible as well as pointless.
                        continue
                    cols = [
                        r[1]
                        for r in conn.execute(
                            f"PRAGMA table_info({_quote_ident(table)})"
                        ).fetchall()
                    ]
                    if not cols:
                        # No enumerable columns means no way to read the table's values, which
                        # is the same position as a missing rowid: uninspectable, so refused.
                        raise _TableNotInspectable(f"{table}: no readable columns")
                    quoted = ", ".join(_quote_ident(c) for c in cols)
                    # The row id is the update handle. A `WITHOUT ROWID` table has none and
                    # raises below — and a table this pass cannot inspect cannot be shown free
                    # of credentials, so the DATABASE is dropped rather than the table skipped.
                    # Skipping would upload the very rows the pass exists to clean, which is
                    # the one outcome this module must never produce. A table that DECLARES a
                    # column named `rowid` is the quieter version of the same problem: the
                    # handle still resolves, just to the wrong thing, so the alias is chosen
                    # per table and the SELECT and the UPDATE must use the SAME one.
                    handle = _rowid_alias(cols)
                    try:
                        rows = conn.execute(
                            f"SELECT {handle}, {quoted} FROM {_quote_ident(table)}"
                        ).fetchall()
                    except sqlite3.DatabaseError as e:
                        raise _TableNotInspectable(f"{table}: {e}") from e
                    for row in rows:
                        handle_value, values = row[0], row[1:]
                        changes: dict[str, str | bytes] = {}
                        for col, value in zip(cols, values):
                            if not value:
                                continue
                            cleaned: str | bytes
                            if isinstance(value, str):
                                cleaned, n = _scrub(value)
                            elif isinstance(value, (bytes, bytearray)):
                                # A column's declared type does not decide what it holds, so a
                                # credential can arrive as bytes. latin-1 maps every byte to a
                                # codepoint and back without loss, so the patterns (ASCII) match
                                # inside binary too and a value with no hit is never rewritten —
                                # which is what keeps embeddings and other real blobs intact.
                                text, n = _scrub(bytes(value).decode("latin-1"))
                                cleaned = text.encode("latin-1")
                            else:
                                continue  # numbers and NULL carry no credential text
                            if n:
                                changes[col] = cleaned
                                pass_hits += n
                        if changes:
                            assignments = ", ".join(f"{_quote_ident(c)} = ?" for c in changes)
                            conn.execute(
                                f"UPDATE {_quote_ident(table)} SET {assignments} "
                                f"WHERE {handle} = ?",
                                (*changes.values(), handle_value),
                            )
                hits += pass_hits
                if not pass_hits:
                    break
            else:
                # Still moving after the cap: a trigger is feeding the scan faster than
                # it cleans. Unprovable, so it is handled as such rather than shipped.
                raise _TableNotInspectable(
                    "redaction did not settle; a trigger keeps reintroducing values"
                )
            if hits:
                for virtual in fts_tables:
                    # Regenerates `_data` / `_idx` / `_docsize` from the cleaned rows. A
                    # redacted content table with a stale index still answers a search
                    # for the credential, so this is part of the redaction, not tidying.
                    conn.execute(
                        f"INSERT INTO {_quote_ident(virtual)}({_quote_ident(virtual)}) "
                        f"VALUES('rebuild')"
                    )
                conn.commit()
                report.replacements[rel] = report.replacements.get(rel, 0) + hits
                if fts_tables:
                    report.rebuilt_indexes.extend(f"{rel}:{v}" for v in fts_tables)
            # REWRITE the file UNCONDITIONALLY, because reading every row clean does not
            # make the FILE clean. SQLite does not erase what it stops using: an UPDATE
            # leaves the old cell in the page's unused space, and a DELETE leaves the whole
            # row in a free page. So a credential the operator deleted from their memory
            # last week is still plaintext in the file, and the scan above finds nothing to
            # replace precisely because no live row holds it. Gating the rebuild on `hits`
            # would protect only the rows this pass rewrote and leave that case intact --
            # the same mistake as trusting the rows over the bytes.
            #
            # `VACUUM` rebuilds the database from its live content, so the freed bytes are
            # not carried over. Row VALUES are untouched, which keeps stored embeddings
            # exactly as they were. Safe because this is the throwaway egress copy, never
            # the operator's own archive.
            conn.commit()
            conn.execute("VACUUM")
    except (sqlite3.DatabaseError, _TableNotInspectable, _SchemaCarriesCredential) as e:
        # Shipping it is never an option: a database this pass cannot read end to end
        # cannot be proven redacted, whether the cause is corruption or a table it cannot
        # address. Neither is DELETING it and uploading the rest. `memory.db` is the
        # payload this backup exists to carry, so a bundle without it reports success and
        # restores nothing — and the only files dropped by name here are ones that are
        # pure secret or rebuildable, which a payload database is neither. So both cases
        # refuse and name the file; what differs is only the wording, because one is the
        # operator's own file and one is ours. The local archive is unaffected by either.
        report.skipped_unreadable.append(f"{rel} ({e})")
        if product:
            raise _PayloadUnprovable(rel) from e
        raise _Unprovable(rel) from e


def redact_bundle_for_egress(stage: Path) -> RedactionReport:
    """Redact a staged bundle IN PLACE. *stage* must be a throwaway copy, never the original.

    Walks the staged tree rather than a declared file list: what leaves the host is exactly
    what is on disk here, so the walk and the payload cannot disagree.
    """
    report = RedactionReport()
    opaque: list[str] = []
    unprovable_payload: list[str] = []
    # Directory members carry names too, and the archive stores them. A directory holding
    # files is already covered by those files' own paths, but an EMPTY one is a member no
    # file names -- so scanning only files let a credential written into a directory name
    # ride out in the archive's member list while every byte of content was clean. The
    # reason the file loop refuses on a path is not a property of files; it is a property
    # of member names.
    for directory in sorted(p for p in stage.rglob("*") if p.is_dir()):
        rel = directory.relative_to(stage).as_posix()
        if _scrub(rel)[1]:
            opaque.append(rel)
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        rel = path.relative_to(stage).as_posix()
        if _scrub(rel)[1]:
            # The archive stores its member names, so a credential written into a PATH
            # leaves the host even when every byte of content is clean. Renaming is not
            # the answer — the restore would then produce a file the operator never had —
            # so the upload refuses and names it.
            opaque.append(rel)
            continue
        if rel == "MANIFEST.json":
            # The ROOT manifest only. The caller rewrites that one once the report is
            # known, which is why skipping it here is safe; nothing rewrites a file that
            # merely shares its name. Matching on the basename would exempt every
            # `MANIFEST.json` anywhere in the bundle -- `workspace/` is copied wholesale,
            # so an operator's own tree can carry one -- and an exempted file is neither
            # scrubbed nor refused, so its contents would reach the destination verbatim.
            continue
        if rel in _DROP_ENTIRELY:
            path.unlink()
            report.dropped.append(rel)
            continue
        if rel in _DERIVED_INDEXES:
            path.unlink()
            report.dropped.append(rel)
            report.rebuilt_indexes.append(rel)
            continue
        if path.suffix == ".db":
            try:
                _redact_database(path, report, rel, product=rel in _PRODUCT_DATABASES)
            except _PayloadUnprovable:
                # Never deleted: an off-host copy missing the memory it was taken for is
                # the failure this whole path exists to prevent.
                unprovable_payload.append(rel)
            except _Unprovable:
                # Not a product database and not provably clean — commonly a `.db` that
                # is not SQLite at all. Refused rather than removed, for the same reason
                # an opaque file is.
                opaque.append(rel)
            continue
        if _redact_text_file(path, report, rel):
            continue
        # Genuinely not text: the redactors work on strings, so this file cannot be shown
        # free of credentials. It is neither redacted nor deleted — deleting an operator's
        # file would make a restore quietly incomplete, so the UPLOAD is refused and the
        # file is named, leaving the choice with the person whose data it is.
        opaque.append(rel)
    if unprovable_payload:
        # Raised before the opaque list: this one says the backup has no contents, which
        # is the more important thing for the operator to read first.
        raise PayloadDatabaseUnprovable(
            unprovable_payload,
            [s for s in report.skipped_unreadable if s.split(" (", 1)[0] in unprovable_payload],
        )
    if opaque:
        # Reported together so the operator sees the whole list, not the first offender.
        raise OpaqueFilesPresent(opaque)
    _stamp_manifest(stage, report)
    # LAST, because the stamp itself writes paths and error text into the manifest. The
    # manifest is the one file guaranteed to leave the host, so leaving it unscanned put
    # the only certain payload outside the pass.
    _redact_manifest(stage, report)
    return report


def _redact_manifest(stage: Path, report: RedactionReport) -> None:
    """Scrub the manifest after it is stamped, and prove it is still readable.

    The replacement tag carries no quote or backslash, so a scrubbed JSON string stays
    valid — but that is a property of the tag, not a guarantee, so it is checked instead
    of assumed. A manifest that no longer parses would make the bundle unrestorable.
    """
    mf = stage / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        original = mf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise _Unprovable(f"MANIFEST.json: {e}") from e
    cleaned, hits = _scrub(original)
    if not hits:
        return
    try:
        json.loads(cleaned)
    except ValueError as e:
        raise _Unprovable(f"MANIFEST.json would not survive redaction: {e}") from e
    mf.write_text(cleaned, encoding="utf-8")
    report.replacements["MANIFEST.json"] = report.replacements.get("MANIFEST.json", 0) + hits


def _stamp_manifest(stage: Path, report: RedactionReport) -> None:
    """Record the redaction in the manifest so a restore can say what it is holding."""
    mf = stage / "MANIFEST.json"
    if not mf.is_file():
        return
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data["redaction"] = report.as_manifest_entry()
    mf.write_text(json.dumps(data, indent=2), encoding="utf-8")


def stage_redacted_copy(source_stage: Path, dest: Path) -> RedactionReport:
    """Copy *source_stage* to *dest* and redact the copy, never the original."""
    shutil.copytree(source_stage, dest)
    return redact_bundle_for_egress(dest)

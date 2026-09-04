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
from typing import Iterator

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
        # An ABSENT key is not an attempt to set the switch, so the default applies and the
        # default is off -- redaction is opt-IN, and off is the fail-safe direction here
        # (no rewriting means no chance of corrupting the off-host copy; the destination is
        # owner-only either way). Review asked for this to raise, on the grounds that the
        # refusal contract covers "anything not exactly true or false". It does not: the
        # refusal below is for a value that IS set and cannot be interpreted -- `"true"`,
        # `1` -- where the operator plainly tried to set the switch and resolving it either
        # way would override their intent. Nothing was attempted here.
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


# A text file that records its own extents cannot survive a variable-length rewrite: the
# declaration keeps its old number while everything it describes moves. Keyed on the two
# ways such a declaration is spelled rather than on file types, so it covers PDF, WARC,
# HTTP archives, MIME multipart and mbox without a per-format entry for each.
_EXTENT_DECLARATIONS = ("startxref", "content-length:")

# How much text this pass will hold at once. `read_text` plus the scrubbed copy plus the
# intermediate each redactor returns means several multiples of the file are live at the
# same time, so an archive-sized member takes the process down before anything is uploaded.
# A refusal names the file and keeps the local archive intact, which is recoverable; an
# OOM mid-pass is not.
_MAX_REDACTABLE_TEXT_BYTES = 64 * 1024 * 1024


def _declares_its_own_extents(text: str) -> bool:
    """Does *text* record a byte length or offset that a rewrite would invalidate?"""
    lowered = text.lower()
    return any(marker in lowered for marker in _EXTENT_DECLARATIONS)


_WIDE_ENCODINGS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def _carries_wide_encoded_credential(raw: bytes) -> bool:
    """Does *raw* hold a credential once read as a WIDE encoding?

    The scanners match ASCII, so text stored two or four bytes per character hides a credential
    from them completely: UTF-16LE `AKIA...` is `A\\x00K\\x00I\\x00A\\x00...`, and nothing matches
    across the NULs. Both callers previously decoded in a way that preserved that spacing --
    the file path read UTF-8 (NUL is a legal codepoint, so the read SUCCEEDS) and the column path
    decodes latin-1 -- so each returned zero hits and reported the data clean.

    UTF-32 is here because covering only UTF-16 does not close it: a UTF-32LE credential read as
    UTF-16LE is still NUL-separated, so it survives a UTF-16-only scan exactly as it survived the
    original one.

    A hit here can only ever REFUSE, never rewrite: the real encoding is a guess, and a
    replacement of a different length shifts every byte after it. Used as a detector for that
    reason, returning a bool rather than a cleaned copy.
    """
    for encoding in _WIDE_ENCODINGS:
        try:
            wide = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
        if _scrub(wide)[1]:
            return True
    return False


def _is_text_shaped(raw: bytes) -> bool:
    """Is *raw* safe to rewrite with a replacement of a DIFFERENT length?

    The same two questions `_redact_text_file` asks of a file, asked of one column value,
    and deliberately the same answer: decodable as UTF-8 and free of NUL. Neither is proof
    that nothing depends on byte position -- it is the same bet the file path already makes,
    and the bet is what lets the feature clean plain text at all. What it does exclude is
    every shape that is structurally binary, which is where the corruption was measured.
    """
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


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

    The NUL test alone does not cover every offset-dependent container, and what is left was
    MEASURED rather than reasoned about: of ZIP, gzip, PNG, tar and a Flate-stream PDF, all
    five are already refused here, each being either not UTF-8 or NUL-bearing.

    That measurement was then over-read. It established which BINARY containers reach this
    branch and was written up as naming the one shape that does -- an uncompressed ASCII PDF.
    Review supplied the counterexample: a text WARC is UTF-8, NUL-free, and declares
    `Content-Length` for every record, so a variable-length edit leaves each length stale and
    every later record unreadable. Measuring one family and generalising to all of them is
    the same error this branch keeps making.

    So the test is the MECHANISM, not the format: a text file that declares its own extents
    internally cannot survive a variable-length rewrite, whatever it is called. `startxref`
    covers PDF's cross-reference offsets (keyed on the mechanism rather than on `%PDF-`), and
    `Content-Length` covers WARC, HTTP archives, MIME multipart and mbox in one rule. That is
    a list of ways to declare an extent rather than a list of file types, which is what makes
    it stop growing per format.

    Residual, stated rather than implied: a container that declares extents in some other
    spelling is still rewritten. The cost of the rule is also real and accepted -- an HTTP
    debug log that contains both a credential and `Content-Length` headers is refused rather
    than cleaned, because its structure cannot be told from a real archive's here, and
    refusing an upload is recoverable while corrupting one is not.
    """
    try:
        # Checked BEFORE the read, which is the only place it helps. `read_text` plus the
        # scrubbed copy plus each redactor's intermediate hold several multiples of the file
        # at once, so an archive-sized member exhausts memory and the process dies with
        # nothing uploaded. Refusing names the file and leaves the local archive intact.
        if path.stat().st_size > _MAX_REDACTABLE_TEXT_BYTES:
            raise _FileUnreadable(
                f"{rel}: {path.stat().st_size} bytes is larger than this pass can hold in "
                f"memory ({_MAX_REDACTABLE_TEXT_BYTES}); refusing rather than risking an "
                "out-of-memory kill mid-upload"
            )
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    except OSError as e:
        raise _FileUnreadable(f"{rel}: {e}") from e
    # A NUL in decoded text means the scanners cannot be trusted on this file: no credential
    # pattern can match across the NULs, so `hits == 0` is evidence of nothing.
    #
    # Reproduced with UTF-16LE and no BOM. Those bytes decode as valid UTF-8 -- NUL is a legal
    # codepoint -- so the read succeeds, the text reads `A\x00K\x00I\x00A...`, `_scrub` returns
    # 0 hits, and the file was reported handled while the credential rode out intact. The NUL
    # check that would have caught it sat one branch too deep, inside `if hits:`, where it only
    # ever guarded against CORRUPTING a NUL-bearing file that did match.
    #
    # Treating every NUL-bearing file as opaque was the prescribed remedy and is WRONG -- it was
    # measured against `test_a_nul_bearing_file_with_no_credential_still_rides`, which plants an
    # ordinary tar inside the workspace and asserts it rides. A workspace routinely holds binary
    # blobs, so that rule refuses real backups over files that carry nothing.
    #
    # So the wide-encoding INTERPRETATIONS are scanned instead, and only a credential actually
    # found there refuses. A hit cannot be rewritten -- the file's real encoding is unknown and a
    # replacement of a different length would shift every byte after it -- so `opaque` is the
    # honest outcome, which refuses the upload and names the file. UTF-32 is covered as well as
    # UTF-16: a UTF-32LE credential read as UTF-16LE is STILL NUL-separated, so a UTF-16-only
    # scan misses it exactly as the original scan did.
    if "\x00" in original and _carries_wide_encoded_credential(
        original.encode("utf-8", errors="surrogatepass")
    ):
        return False
    cleaned, hits = _scrub(original)
    if hits:
        if "\x00" in original:
            # Kept as well as the pre-scan above: that one answers "can this file hide a
            # credential from the scan", this one answers "can this file be rewritten at all".
            return False
        if _declares_its_own_extents(original):
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

    That reasoning holds only while a content table EXISTS to rebuild from. A contentless
    index (`content=''`) has none, so the skip would leave its storage the only copy of the
    text and nothing would ever examine it. Those refuse the upload instead: the rule was
    justified for one of this path's two shapes and is false for the other.

    Identified positively from each virtual table's own name, not by pattern-matching
    every table that happens to end in `_idx`.
    """
    fts: list[str] = []
    contentless: list[str] = []
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table'"
    ).fetchall():
        # Case-INSENSITIVE, because SQLite stores DDL verbatim and its own documentation
        # writes `USING FTS5(...)`. This used to fold only the all-lowercase spelling
        # (`"USING fts" in sql.replace("using fts", "USING fts")`), so a table created the
        # documented way was not recognised as full-text at all: its `WITHOUT ROWID` shadow
        # tables were then scanned as ordinary ones and the pager's `SELECT MAX(<handle>)`
        # raised `no such column: rowid`, refusing the ENTIRE backup of a sound database.
        # Reproduced both spellings -- lowercase passed, uppercase refused -- so this was a
        # legitimate backup lost to a spelling. The contentless probe below is lowered with
        # it, for the same reason.
        lowered = sql.lower() if sql else ""
        if "using fts" in lowered:
            fts.append(name)
            # `content=''` is a CONTENTLESS index: there is no content table, so the
            # storage tables are not derived from anything and the rebuild below has no
            # source to regenerate them from. The whole justification for skipping them --
            # cleaned text plus a rebuild removes the term -- is false for this shape, and
            # a credential in that storage would ride the bundle unexamined.
            #
            # Measured, not assumed: an AWS access key is a separator-free alphanumeric run,
            # so FTS5 stores it as ONE token rather than splitting it, the scanner matches
            # that shape, and it survived a full pass with `replacements={}`.
            if re.search(r"content\s*=\s*(''|\"\")", lowered):
                contentless.append(name)
    if contentless:
        # Refused rather than scanned. Cleaning FTS5's private storage in place is not
        # something this pass can do correctly -- the format is SQLite's, not ours -- and
        # dropping the database would delete the operator's memory from the off-host copy.
        # A payload that cannot be proven clean refuses the upload, which is the same rule
        # the rest of this module already follows.
        raise _TableNotInspectable(
            "contentless FTS5 index(es) " + ", ".join(sorted(contentless)) + ": their tokens "
            "live only in FTS storage, which has no content table to be cleaned and rebuilt "
            "from, so this pass cannot prove them free of credentials"
        )
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


# Each `DELETE FROM <table>` in a trigger body, so the exemption can be decided per
# DELETE rather than per trigger. Quoting styles sqlite accepts are all stripped, because a
# target spelled `"docs_content"` must match the plain name the schema reports.
_DELETE_TARGET_RE = re.compile(r"delete\s+from\s+[\"'`\[]?([A-Za-z0-9_]+)")

# A write that can DESTROY an existing row without saying `DELETE`. `INSERT OR REPLACE`
# and `REPLACE INTO` both delete the conflicting row and insert in its place, so a trigger
# whose body only ever "writes" can still lose a row this pass never looked at.
_REPLACING_WRITE_RE = re.compile(r"\b(?:insert\s+or\s+replace|replace\s+into)\b")

# A trigger body's own `UPDATE` overwrites the PREVIOUS values of every row it matches, in
# the same way a `DELETE` removes the row: the fixpoint can observe a settled database but
# cannot restore a value that is gone. Distinguished from a body that only INSERTs -- an
# inserted copy is exactly what the fixpoint exists to clean on the next pass, which is why
# `INSERT INTO audit VALUES(OLD.body)` stays allowed while `UPDATE audit SET ...` does not.
_UPDATE_TARGET_RE = re.compile(r"\bupdate\s+(?:or\s+\w+\s+)?[\"'`\[]?([A-Za-z0-9_]+)")


# Rows held in memory at once while scanning a table. Bounded so a large table cannot take
# the process down mid-pass; large enough that the extra SELECTs are not the cost.
_ROW_PAGE = 500


def _paged_rows(
    conn: "sqlite3.Connection", table: str, handle: str, quoted: str
) -> "Iterator[tuple]":
    """Yield the rows *table* held when this pass began, a page at a time, ordered by *handle*.

    The caller UPDATEs rows as it consumes them, which is why this pages on `handle > ?`
    instead of streaming a single cursor: a scan interleaved with writes to the same table
    has undefined results in SQLite. Each page is read whole before the caller touches it,
    and an UPDATE never changes the handle, so the next page stays disjoint and complete.

    An UPDATE can still make the table GROW, which is the case a page cursor alone does not
    survive. An `AFTER UPDATE` trigger that INSERTs -- copying the pre-update value into a
    new row -- hands every cleaned row back as a fresh one with a higher handle, so `handle
    > last` finds work forever and a single pass never terminates. The caller's fixpoint cap
    counts PASSES, so it never gets to fire: the divergence is inside the pass it is
    counting. The ceiling below is what makes each pass finite -- rows that appear mid-pass
    are beyond it and belong to the NEXT pass, which reports them as replacements, so a
    trigger that keeps reintroducing values is refused by the pass cap instead of hanging.
    """
    try:
        top = conn.execute(f"SELECT MAX({handle}) FROM {_quote_ident(table)}").fetchone()
    except sqlite3.DatabaseError as e:
        raise _TableNotInspectable(f"{table}: {e}") from e
    ceiling = top[0] if top else None
    if ceiling is None:
        return  # empty table: nothing this pass is responsible for
    # NO sentinel lower bound. This used to start at `last = -1` and always select
    # `handle > ?`, which silently skipped every row with a rowid <= -1 -- and SQLite lets
    # you set an explicit negative INTEGER PRIMARY KEY, so a credential sitting at rowid
    # -5 was never scanned and shipped in the redacted copy while the report still claimed
    # a successful replacement. Picking a smaller sentinel does not fix it either: the
    # range includes -2**63, so any floor at all can be the row that is missed. The first
    # page therefore has no floor, and only subsequent pages carry one.
    last: object = None
    while True:
        if last is None:
            sql = (
                f"SELECT {handle}, {quoted} FROM {_quote_ident(table)} "
                f"WHERE {handle} <= ? ORDER BY {handle} LIMIT ?"
            )
            params: tuple = (ceiling, _ROW_PAGE)
        else:
            sql = (
                f"SELECT {handle}, {quoted} FROM {_quote_ident(table)} "
                f"WHERE {handle} > ? AND {handle} <= ? ORDER BY {handle} LIMIT ?"
            )
            params = (last, ceiling, _ROW_PAGE)
        try:
            page = conn.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as e:
            raise _TableNotInspectable(f"{table}: {e}") from e
        if not page:
            return
        last = page[-1][0]
        yield from page


def _refuse_generated_columns_with_credentials(
    conn: "sqlite3.Connection", table: str, generated: list[str], rel: str
) -> None:
    """Refuse the database when a GENERATED column's value carries a credential.

    Only when it actually does. A generated column is ordinary in most schemas, so refusing
    every table that has one would turn this hardening into an outage -- the same trade the
    trigger refusal gets wrong when it is widened. The values are read and put through the
    same two scanners the row pass uses, and only a real match refuses.

    Read as a STREAM, not with `fetchall()`. A generated column can span as many rows as its
    table, and materialising all of them to answer an all-or-nothing question is how a backup
    turns into an OOM kill. The row pass already pages for the same reason; this one does not
    even need a page window, because it stops at the first match.

    BYTES are scanned, not skipped. sqlite stores plain UTF-8 text as a BLOB routinely, so an
    `isinstance(value, str)` filter would walk straight past a credential in a byte-valued
    generated column. Unlike the row pass there is no text-shaped/structured split to make
    here: that distinction exists to decide whether a value can be safely REWRITTEN, and a
    generated column is read-only, so every hit ends the same way -- refuse.
    """
    quoted = ", ".join(_quote_ident(c) for c in generated)
    try:
        cursor = conn.execute(f"SELECT {quoted} FROM {_quote_ident(table)}")
        for row in cursor:
            for value in row:
                if isinstance(value, (bytes, bytearray)):
                    text = bytes(value).decode("utf-8", errors="replace")
                elif isinstance(value, str):
                    text = value
                else:
                    continue
                if not text:
                    continue
                _scrubbed, hits = _scrub(text)
                if hits:
                    raise _SchemaCarriesCredential(
                        f"{rel}: table {table!r} has a GENERATED column among "
                        f"{', '.join(sorted(generated))} whose value carries a credential. A "
                        "generated column is derived and read-only, so it cannot be rewritten "
                        "in place -- refusing the upload rather than sending it. The local "
                        "archive is unaffected."
                    )
    except sqlite3.DatabaseError as e:
        # Unreadable is the same position as uninspectable elsewhere in this pass.
        raise _TableNotInspectable(f"{table}: {e}") from e


def _refuse_update_triggers_that_destroy_rows(
    conn: "sqlite3.Connection", rel: str, fts_names: set[str]
) -> None:
    """Refuse a database whose UPDATE triggers DELETE rows.

    The fixpoint scan below handles one thing a trigger does, and does it provably: copy a
    pre-update value somewhere the pass has already been, which the next pass then cleans.
    It cannot handle the other thing. A trigger that DELETES runs once, settles immediately,
    and the fixpoint sees a quiet database -- the rows are simply gone from the copy that
    leaves. Reproduced: an `AFTER UPDATE` trigger removed both rows of an unrelated table
    while the pass reported success.

    Scoped to DELETE deliberately, and NARROWER than the prescription that produced it.
    Review asked for every non-FTS UPDATE trigger to be refused; that would throw away the
    fixpoint, which is a deliberate, tested capability for exactly the copy-the-value case --
    and it would refuse the product's own external-content full-text index, which is
    MAINTAINED by update triggers. A trigger that only writes values is the schema doing
    what it says, and any credential it propagates is what the fixpoint is for.

    An FTS-maintenance trigger is exempt only for the deletions that ARE that maintenance:
    every `DELETE FROM` in its body must target FTS storage. The first version of this
    exempted the whole trigger as soon as any FTS name appeared anywhere in it, so a trigger
    that maintains the index AND deletes unrelated rows was waved through -- review's
    finding, and the same mistake this refusal exists to correct: a rule justified for one of
    a thing's statements, applied to all of them.

    The residual, stated rather than implied, and NARROWER than it was: `INSERT OR REPLACE`
    and `REPLACE INTO` used to be named here as uncaught, and they are now refused -- they
    are spellings a body either contains or does not, so text bounds them honestly. An
    `UPDATE` aimed at a table OTHER than the trigger's own is refused for the same reason:
    it overwrites rows this pass is not rewriting, so their prior values are gone from the
    copy that leaves, and the fixpoint can observe a settled database but never restore them.

    What genuinely remains: an `UPDATE` on the trigger's OWN table with no row bound can
    still overwrite other rows of that table. It stays allowed because the shape it cannot be
    told apart from by text -- a mirror column maintained by
    `UPDATE t SET mirror = NEW.body WHERE id = NEW.id` -- is ordinary, and the fixpoint
    provably cleans it. Separating those two needs the statement's effects.

    That is why the refusal keys on the TARGET rather than on the body containing an `UPDATE`
    at all. Both broader rules were measured and rejected: refusing every non-FTS
    UPDATE-writing trigger discards the fixpoint entirely (three tests in this suite assert
    the value-writing cases it handles -- a trigger propagating a credential is CLEANED by
    the next pass, not shipped) and refuses the product's own external-content index, whose
    maintenance is exactly such a trigger; refusing every body containing an `UPDATE` then
    fails the mirror-column case above. The trade is deliberate: cleaning a propagated value
    serves the operator, refusing to back up at all does not.
    """
    suspects: list[str] = []
    for name, tbl_name, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_schema WHERE type='trigger'"
    ).fetchall():
        body = (sql or "").lower()
        header, _, actions = body.partition("begin")
        if "update" not in header:
            continue  # not fired by what this pass does
        targets = _DELETE_TARGET_RE.findall(actions)
        # An `UPDATE` inside the body overwrites the PRIOR values of every row it matches, and
        # the fixpoint can observe a settled database but never restore a value that is gone --
        # the same reason a `DELETE` is refused. Reproduced twice, and the two cases need
        # different treatment, which is why this is not one flat rule:
        #
        #   * FOREIGN target (`UPDATE unrelated SET note='CLOBBERED'`): reaches rows this pass
        #     is not rewriting at all. Refused.
        #   * OWN table, UNBOUND (`UPDATE items SET note='CLOBBERED'`): no row bound, so it
        #     rewrites every row of the table it fires on. Refused.
        #   * OWN table, bound to the triggering row (`UPDATE t SET mirror = NEW.body WHERE
        #     id = NEW.id`): a mirror column. Ordinary shape, and the fixpoint provably cleans
        #     the propagated value. ALLOWED -- refusing it discards the fixpoint, which three
        #     tests in this suite assert, and refusing every non-FTS UPDATE-writing trigger
        #     additionally refuses the product's own external-content index.
        #
        # "References NEW or OLD" is the bound, and it is a spelling rather than an effect, so
        # text carries it honestly. Residual, stated: a statement can reference NEW and still
        # touch other rows deliberately (`WHERE id != NEW.id`). That is an adversarial
        # construction; the accidental corrupting trigger is the unbound form, now refused.
        for stmt in actions.split(";"):
            for target in _UPDATE_TARGET_RE.findall(stmt):
                if target == (tbl_name or "").lower() and re.search(r"\b(?:new|old)\.", stmt):
                    continue  # bound to the row this pass is already rewriting
                targets.append(target)
        if not targets:
            # A body with no `DELETE` can still destroy a row: `INSERT OR REPLACE` and
            # `REPLACE INTO` delete the conflicting row and insert in its place. Reproduced
            # both forms clobbering a row in an unrelated table that this pass never looked
            # at, so they are refused for the same reason an explicit DELETE is -- the
            # fixpoint can observe a settled database, never undo a row that is gone.
            #
            # Text is a legitimate bound for THESE TWO because they are spellings, not
            # effects: a body either contains one or it does not. That is why this is narrow
            # enough to keep, unlike refusing every non-FTS UPDATE trigger -- measured twice,
            # and it discards the fixpoint plus the product's own full-text index. Verified
            # this guard discriminates: both REPLACE forms refuse, while a plain
            # value-copying trigger still goes through the fixpoint and is cleaned.
            if _REPLACING_WRITE_RE.search(actions):
                suspects.append(name)
            continue  # writes values only: the fixpoint's case
        if all(t in fts_names for t in targets):
            continue  # every deletion is index maintenance, rebuilt after this pass anyway
        suspects.append(name)
    if suspects:
        raise _TableNotInspectable(
            f"{rel} has UPDATE trigger(s) that DELETE or OVERWRITE rows and are not full-text "
            f"maintenance ({', '.join(sorted(suspects))}). Redacting runs UPDATE statements, "
            "so these fire and the deletion is permanent in the copy that leaves -- the "
            "fixpoint scan cannot undo it, only observe a settled database. Refusing the "
            "upload; the local archive is unaffected."
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
            # BEFORE any UPDATE runs: a trigger's effects cannot be undone once fired.
            _refuse_update_triggers_that_destroy_rows(conn, rel, set(fts_tables) | skip_tables)
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
                    # GENERATED columns are invisible to `table_info` and READ-ONLY, so they
                    # can be neither scanned by the loop below nor rewritten by its UPDATE.
                    # Two existing guards already cover most of the exposure -- the schema
                    # scan refuses a credential written as a literal in the generation
                    # expression, and a STORED column is recomputed when its source is
                    # updated, so redacting the source propagates -- but neither covers a
                    # credential ASSEMBLED across columns. Reproduced: `a='AKIA'`,
                    # `b='IOSFODNN7EXAMPLE'`, `joined AS (a || b) STORED`. No single value
                    # matches, the DDL holds no credential, nothing refused, and the
                    # generated column materialised the whole key into the outbound copy.
                    #
                    # Refused rather than redacted, because there is no way to redact it: the
                    # column is derived, so the only honest answer is to stop the upload and
                    # name the table. Scoped to hidden flags 2 and 3 (VIRTUAL and STORED
                    # generated) so a virtual table's own hidden columns -- flag 1, which is
                    # what an FTS table presents -- are untouched and full-text backups keep
                    # working.
                    generated = [
                        r[1]
                        for r in conn.execute(
                            f"PRAGMA table_xinfo({_quote_ident(table)})"
                        ).fetchall()
                        if len(r) > 6 and r[6] in (2, 3)
                    ]
                    if generated:
                        _refuse_generated_columns_with_credentials(conn, table, generated, rel)
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
                    # PAGED, not `fetchall()`. Materialising a whole table held every row in
                    # memory at once, so a large table took the process down before anything
                    # was uploaded -- review's finding, the same hazard as reading an
                    # archive-sized file whole.
                    #
                    # A generator paging on `handle > ?` rather than one streamed cursor,
                    # because this loop UPDATEs the table it is reading and a live scan
                    # interleaved with writes has undefined results in SQLite. Each page is
                    # fully read before its rows are updated, and an UPDATE never changes the
                    # handle, so the next page's `handle > last` is still disjoint and
                    # complete.
                    for row in _paged_rows(conn, table, handle, quoted):
                        handle_value, values = row[0], row[1:]
                        changes: dict[str, str | bytes] = {}
                        for col, value in zip(cols, values):
                            if not value:
                                continue
                            cleaned: str | bytes
                            if isinstance(value, str):
                                cleaned, n = _scrub(value)
                                # The same hazard the FILE path refuses, at row scope. A
                                # TEXT value carrying NUL is binary wearing a text type, so
                                # a variable-length replacement shifts everything after the
                                # hit inside a value whose structure depends on position.
                                if n and "\x00" in value:
                                    raise _TableNotInspectable(
                                        f"{table}.{col}: a NUL-bearing text value carries a "
                                        "credential, and replacing it would shift every "
                                        "following byte of a value whose layout depends on "
                                        "position. Refusing rather than rewriting it."
                                    )
                            elif isinstance(value, (bytes, bytearray)):
                                # A column's declared type does not decide what it holds, so a
                                # credential can arrive as bytes. latin-1 maps every byte to a
                                # codepoint and back without loss, so the patterns (ASCII) match
                                # inside binary too and a value with no hit is never rewritten —
                                # which is what keeps embeddings and other real blobs intact.
                                text, n = _scrub(bytes(value).decode("latin-1"))
                                cleaned = text.encode("latin-1")
                                if not n and _carries_wide_encoded_credential(bytes(value)):
                                    # latin-1 preserves a wide encoding's NUL spacing, so the
                                    # miss above is not evidence of a clean value: a UTF-16 or
                                    # UTF-32 BLOB hides an ASCII credential from the scanners
                                    # completely. Refused rather than rewritten, for the reason
                                    # spelled out just below -- replacement is variable-length,
                                    # and here the value's real encoding is a guess on top of
                                    # that.
                                    raise _TableNotInspectable(
                                        f"{table}.{col}: a byte-valued field carries a "
                                        "credential encoded two or four bytes per character. "
                                        "The scanners cannot rewrite it -- the value's real "
                                        "encoding is unknown and a replacement of a different "
                                        "length would shift every byte after it. Refusing the "
                                        "upload; the local archive is unaffected."
                                    )
                                # A HIT is a different question from a miss, and the sentence
                                # above only answers the miss. Replacement is VARIABLE-LENGTH,
                                # so rewriting a byte value moves every byte after the hit:
                                # a length prefix then describes the wrong extent and every
                                # following field is read at the wrong offset. Measured on a
                                # length-prefixed record -- the prefix still said 46 while the
                                # payload had become 48, and the trailing field came back
                                # shifted.
                                #
                                # The test is the FILE path's test, deliberately: text-shaped
                                # bytes are rewritten, structured bytes are refused. A blanket
                                # refusal for every byte column was the prescribed remedy and
                                # is wrong -- sqlite stores plain UTF-8 text as a BLOB all the
                                # time, that case has no offsets to invalidate, and refusing it
                                # would turn a credential this pass can safely remove into a
                                # refused upload. Refusing is for bytes whose layout depends on
                                # position, which is the same pair the file path refuses:
                                # content that must be rewritten, in a container that cannot be
                                # rewritten safely.
                                #
                                # Repair is not on the table for the structured case: what the
                                # bytes MEAN is unknown, so no length fix-up can be trusted.
                                if n and not _is_text_shaped(bytes(value)):
                                    raise _TableNotInspectable(
                                        f"{table}.{col}: a byte-valued field carries a "
                                        "credential and is not text -- replacing it would "
                                        "change the value's length and shift every byte after "
                                        "it, and the bytes' meaning is unknown, so it cannot "
                                        "be repaired. Refusing the upload instead of shipping "
                                        "a corrupted copy. The local archive is unaffected."
                                    )
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
            # REBUILD every identified index UNCONDITIONALLY, for the same reason the
            # VACUUM below is unconditional: reading every live row clean does not make the
            # index clean. An external-content FTS table keeps its OWN tokenized copy and
            # does not auto-sync, so a base table that has moved on leaves the index holding
            # text no live row contains. The scan then finds `hits == 0` -- correctly, there
            # is nothing in the rows -- and gating the rebuild on `hits` skipped the one case
            # where the index is the ONLY copy.
            #
            # Reproduced: base row updated to drop the credential without syncing the index,
            # `hits == 0`, no refusal, and the egress copy still answered
            # `docs_fts MATCH '<key>'` with a hit afterwards. VACUUM does not help here --
            # it repacks live content and never re-tokenizes, and the stale doclist IS live
            # content of the shadow table.
            #
            # Safe to do unconditionally because a CONTENTLESS index -- the one shape with no
            # content table to rebuild from -- is refused before this point, so every name in
            # `fts_tables` has a source.
            for virtual in fts_tables:
                conn.execute(
                    f"INSERT INTO {_quote_ident(virtual)}({_quote_ident(virtual)}) "
                    f"VALUES('rebuild')"
                )
            conn.commit()
            if hits:
                report.replacements[rel] = report.replacements.get(rel, 0) + hits
                if fts_tables:
                    # Itemised only when the rebuild accompanied a real replacement, which is
                    # what this manifest field tells the operator about. The unconditional
                    # pass above is hygiene on the throwaway egress copy, like the VACUUM,
                    # which is likewise not itemised.
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

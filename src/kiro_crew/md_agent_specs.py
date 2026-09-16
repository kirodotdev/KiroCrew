"""Compile markdown agent definitions into the JSON specs kiro-cli loads.

WHY A COMPILER AND NOT A SECOND READER
======================================
The wider ecosystem writes an agent as ONE markdown file: YAML frontmatter for
the config fields, the markdown body as the system prompt. kiro-agent (the v3
engine) loads that shape natively. **kiro-cli does not** -- it globs
``*.json`` and parses whatever it is handed as JSON regardless of extension, so
a ``.md`` spec is not discoverable by it and cannot be activated by
``--agent <name>``.

Kiro Crew does not load the spec either: it hands kiro-cli a bare NAME and
kiro-cli resolves the file itself. So teaching Crew's own discovery to read
markdown would produce an agent that lists in the dashboard and then fails at
``session/set_mode`` -- the exact "visible but not dispatchable" inversion that
``agent_discovery.project_agent_files``' ``include_legacy=False`` default exists
to prevent. Worse, Crew reads the agents directory from ~45 places, and three of
them are not cosmetic: the MCP gateway rewriter (``mcp_gateway/rewriter.py``)
would never rewrite a markdown spec's ``mcpServers`` into broker stubs, so its
MCP servers would spawn DIRECT and bypass the tool gate entirely;
``agent_discovery._dir_signature`` fingerprints only ``*.json``, so roster caches
would not invalidate on a markdown edit; and ``connections/ownership`` would read
a markdown sharer as absent and tear down a live connection.

This module therefore treats markdown as a SOURCE FORMAT and compiles it to the
one on-disk shape every existing consumer already understands. kiro-cli can then
resolve it, the rewriter stubs its MCP servers, the roster lists it, the model
resolvers see it, and doctor checks it -- with no change to any of them.

TRUST BOUNDARY
==============
The agents DIRECTORY is the trust boundary, not the file extension. A ``.json``
dropped in ``~/.kiro/agents`` may already declare ``mcpServers``, ``allowedTools``
and lifecycle ``hooks`` -- shell commands the backend runs with no permission
request. ``security/paths.py`` write-protects the whole directory against the
agent's own file tools for exactly that reason. A ``.md`` in the same directory is
neither more nor less trusted, so this compiler passes those fields through
rather than inventing a second, weaker policy for the same directory. What it
does NOT do is widen the boundary: it reads only from the user-level agents
directory, and it writes only there.

WHY THE FRONTMATTER IS PARSED AS REAL YAML
==========================================
``kiro_crew.frontmatter`` is deliberately NOT reused. That module's contract is a
flat ``dict[str, str]`` -- it cannot express ``tools: [fs_read, grep]`` or a
nested ``mcpServers`` mapping, which is most of an agent spec -- and its
``split_frontmatter`` body is documented as "not a document body" for the column-0
extractions (it cuts immediately after the closer token, possibly mid-line),
while here the body IS the prompt. Its plain-scalar grammar also deliberately
accepts text a YAML parser rejects, which is right for SKILL.md and wrong here:
this format's grammar is defined by the ecosystem that already writes it, and
that grammar is YAML. So the fence is located here, the block goes through
``yaml.safe_load`` (never ``yaml.load`` -- the directory is user-writable and
shared with other tools, so arbitrary object construction is not on the table),
and the closer is strict: a ``---junk`` line is not a closer, and a document
whose fence does not close is refused rather than compiled with its frontmatter
leaking into the prompt.

OWNERSHIP, AND WHY IT LIVES OUTSIDE THE SPEC
============================================
The compiler must clean up its own output: a renamed or deleted ``.md`` leaves a
stale ``<name>.json`` behind, and a stale spec is an agent the user can still
select. So cleanup has to answer "did I write this file?" -- and kiro-cli
validates specs with ``deny_unknown_fields``, so an ownership marker cannot live
inside the spec (an extra key makes kiro-cli reject the whole file and the user
loses the agent). ``connections/alias_record`` hit this same wall and solved it
with an out-of-spec, generation-bound record; this module follows that design.

Shape cannot decide history, and re-derivation is not evidence: "the JSON I would
have written" is no proof that I DID write it, and claiming a hand-written
``code-reviewer.json`` because a ``code-reviewer.md`` now exists would delete a
user's file. The record is the only ownership oracle, and absence proves only
"not provably mine" -- which is the safe reading, since an unrecorded spec is
treated as the user's and survives untouched.

Two files means two writes, and no ORDERING of two writes is safe in both
directions, so each entry is written as a two-phase transaction carrying the
digest of the generation it describes::

    PENDING(previous=<digest on disk>, target=<digest about to be written>)
      -> spec write
    -> COMMITTED(<digest written>)

An entry authorizes overwriting or deleting ``<name>.json`` only when the file's
CURRENT digest matches one of the digests that entry names. Every crash boundary
is safe in both directions:

===  ================================  ==============  ==========================================
 #   interrupted at                    record          ``<name>.json`` resolves to
===  ================================  ==============  ==========================================
 0   before the pending write          unchanged       previous generation -- pass retries cleanly
 1   pending write FAILS               unchanged       unchanged; the spec MUST NOT advance
 2   kill after the pending write      PENDING         previous digest matches -> ours
 3   spec write FAILS                  PENDING         previous digest matches -> ours
 4   kill after the spec write         PENDING         target digest matches -> ours
 5   commit write FAILS                PENDING         target digest matches -> ours
 6   nothing (success)                 COMMITTED       committed digest matches -> ours
 7   record absent / unusable / vN     --              nothing -- never ours, file survives
 8   spec hand-edited                  either          neither digest matches -> the user's
===  ================================  ==============  ==========================================

Row 4/5 is why the pending entry names BOTH digests: without the target digest an
interrupted write would leave a file matching nothing, and the source would never
compile again without manual cleanup. Row 8 is the case that protects a user who
edited the generated JSON: the compiler refuses, says so, and leaves the edit
alone rather than reverting it on the next pass.

CONCURRENCY
===========
The whole pass runs under ``agent.agents_spec_lock`` -- the same cross-process
advisory lock the dashboard's fork/publish endpoints and the agent-detail PATCH
take. A read-modify-writer in this directory that skips it can silently revert
another writer's spec.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_crew.security.paths import is_sensitive_path
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: Source extension. Lowercase-only on purpose: ``glob`` is case-sensitive on
#: POSIX and not on macOS/Windows, so accepting ``.MD`` would make the compiled
#: set platform-dependent for the same directory.
MD_SUFFIX = ".md"

#: Record schema. Bumped when an entry's meaning changes for identical inputs;
#: an unrecognised version is treated as row 7 (no ownership), never migrated
#: in place, because a wrong migration authorizes deleting a user's file.
RECORD_SCHEMA = 1

_STATE_PENDING = "pending"
_STATE_COMMITTED = "committed"

# The frontmatter fence. Stricter than ``kiro_crew.frontmatter``'s column-0
# grammar in two ways that matter for THIS format:
#   * the closer must be a line of its own (trailing whitespace only), because a
#     ``---junk`` line treated as a closer would silently move frontmatter into
#     the prompt;
#   * the match consumes the closer's line ending, so group 2 is the body a
#     prompt can be built from rather than an arbitrary unconsumed remainder.
# The inner group is optional so an empty block (``---\n---``) is a match with no
# fields rather than "no fence at all".
_FENCE_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

#: Frontmatter keys carried into the compiled spec. An ALLOWLIST, because
#: kiro-cli rejects a spec wholesale on an unknown key -- so passing an
#: unrecognised frontmatter key through would not produce a partially-working
#: agent, it would produce no agent at all, with the failure surfacing far from
#: this file. An unknown key is dropped with a warning instead, which is also
#: what kiro-agent's own schemas do (Zod strips them).
#:
#: ``allowedTools`` and ``toolsSettings`` are kiro-cli-only fields that
#: kiro-agent silently strips, so the same document grants different authority on
#: the two engines. They are carried here because this compiler's output is FOR
#: kiro-cli; the divergence is the ecosystem's, not ours, and dropping them would
#: silently downgrade a ported file's declared permissions.
CARRIED_KEYS = frozenset(
    {
        "description",
        "model",
        "tools",
        "allowedTools",
        "toolsSettings",
        "mcpServers",
        "resources",
        "hooks",
        "includeMcpJson",
        "useLegacyMcpJson",
    }
)

#: Refused in frontmatter. ``name`` is handled separately (it decides the output
#: filename). ``prompt`` is refused rather than ignored: in this format the BODY
#: is the prompt, so a document declaring both is ambiguous, and silently picking
#: one would leave the author with an agent running a prompt they did not choose.
_REFUSED_KEYS = frozenset({"prompt"})


class MdAgentSpecError(RuntimeError):
    """A markdown agent definition could not be compiled.

    Carries a stable ``code`` so callers and tests can distinguish "the author
    must fix the document" from "another file owns this name".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompiledSpec:
    """One compiled definition: the agent name, its spec, and the exact bytes."""

    name: str
    spec: dict[str, Any]
    payload: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass
class CompileOutcome:
    """What one pass did, for logging and for the dashboard's own diagnostics."""

    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    refused: list[tuple[str, str, str]] = field(default_factory=list)

    def refuse(self, source: str, code: str, message: str) -> None:
        self.refused.append((source, code, message))


def _spec_payload(spec: dict[str, Any]) -> bytes:
    """Serialize *spec* to the EXACT bytes ``agent._atomic_json_write`` writes.

    Ownership is decided by comparing a file's digest to a recorded one, so this
    serialization and that writer's must not drift: ``json.dump(data, f,
    indent=2)`` followed by a trailing newline. If they diverge, every compiled
    spec reads as hand-edited on the next pass and the compiler stops touching
    its own output -- a silent, total failure of the feature rather than a loud
    one, which is why this lives in one function next to a test that pins it.
    """
    return (json.dumps(spec, indent=2) + "\n").encode("utf-8")


def _decode(raw: bytes) -> str:
    """Decode a markdown source to text, or raise ``MdAgentSpecError``.

    Strips a UTF-8 BOM and folds CRLF/CR to LF. Both are load-bearing rather
    than tidiness: the fence is anchored at position 0, so a BOM leaves
    ``\\ufeff---`` and the whole block reads as absent -- the document compiles
    to an agent with no config and its frontmatter as prose. A file exported from
    a Windows editor is the ordinary way to get one, and this format exists to
    accept files authored elsewhere.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MdAgentSpecError("not_utf8", "the file is not valid UTF-8") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def derive_spec(text: str, *, fallback_name: str) -> CompiledSpec:
    """Compile one markdown definition into a kiro-cli spec. Pure: no I/O.

    *fallback_name* is used when the frontmatter declares no ``name`` -- by
    convention the source file's stem, mirroring how kiro-cli falls back to a
    spec's filename.

    Raises ``MdAgentSpecError`` on anything that would produce an agent the
    author did not ask for: no closing fence, frontmatter that is not a mapping,
    a declared ``prompt`` competing with the body, or a name that cannot identify
    a spec. Refusal is the right outcome for all of them -- an unrunnable
    document is recoverable, an agent quietly running the wrong prompt or the
    wrong permissions is not.
    """
    match = _FENCE_RE.match(text)
    if match is None:
        raise MdAgentSpecError(
            "no_frontmatter",
            "no YAML frontmatter block: expected '---' on the first line and a "
            "closing '---' on a line of its own",
        )
    block = match.group(1) or ""
    body = text[match.end() :]

    try:
        loaded = yaml.safe_load(block) if block.strip() else {}
    except yaml.YAMLError as exc:
        # yaml.YAMLError is NOT a ValueError, so it would escape a caller
        # catching ValueError. Translated here so this function's contract is
        # "MdAgentSpecError or success" and no caller has to know it parses YAML.
        raise MdAgentSpecError("bad_yaml", f"the frontmatter is not valid YAML: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise MdAgentSpecError(
            "frontmatter_not_mapping",
            f"the frontmatter must be a mapping of fields, not {type(loaded).__name__}",
        )

    refused = sorted(k for k in _REFUSED_KEYS if k in loaded)
    if refused:
        raise MdAgentSpecError(
            "reserved_key",
            f"frontmatter may not declare {refused[0]!r}: in this format the "
            "markdown body is the prompt",
        )

    declared = loaded.get("name", fallback_name)
    if not isinstance(declared, str) or not _AGENT_NAME_RE.match(declared):
        raise MdAgentSpecError(
            "unsafe_name",
            f"{declared!r} is not a usable agent name (letters, digits, '_' and "
            "'-'; must start and end alphanumeric)",
        )
    if f"{declared.lower()}.json" in OWNED_KIRO_AGENT_FILES:
        raise MdAgentSpecError(
            "reserved_name",
            f"{declared!r} is a Kiro Crew-managed agent name and cannot be " "defined in markdown",
        )

    spec: dict[str, Any] = {"name": declared}
    for key in sorted(k for k in loaded if k in CARRIED_KEYS):
        value = loaded[key]
        if value is not None:
            spec[key] = value
    unknown = sorted(set(loaded) - CARRIED_KEYS - {"name"})
    if unknown:
        # Dropped, not refused: an unrecognised key is usually a field another
        # engine understands (kiro-agent has several kiro-cli does not), and
        # refusing the whole document for it would make a ported file unusable
        # for a field that changes nothing here.
        logger.warning(
            "markdown agent %r: dropping frontmatter key(s) kiro-cli does not " "accept: %s",
            declared,
            ", ".join(unknown),
        )

    prompt = body.strip()
    if prompt:
        spec["prompt"] = prompt

    return CompiledSpec(name=declared, spec=spec, payload=_spec_payload(spec))


def _record_path() -> Path:
    """Where the ownership record lives: OUTSIDE the agents directory.

    Deliberately not a dotfile beside the specs. ``pathlib.glob("*.json")``
    matches dotfiles, several sweeps in this repo enumerate that directory, and
    the rewriter's own prune pass learned this the hard way (its fingerprint file
    carries no ``.json`` suffix precisely so a prune cannot eat it). Keeping the
    record under the data home means no agents-directory consumer can see it at
    all.
    """
    return data_home() / "md-agents" / "compiled.json"


def _load_record() -> dict[str, dict[str, Any]]:
    """Read the ownership record; ``{}`` for anything that is not a usable one.

    Absent, unreadable, malformed, or written by a schema this version does not
    recognise all collapse to "no ownership claimed" (row 7). That degrades to
    leaving files alone, which is the safe direction: the cost is a stale spec
    the user removes by hand, and the alternative is authorizing a delete from a
    record we cannot read.
    """
    path = _record_path()
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("markdown agent record at %s is unreadable; claiming nothing", path)
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != RECORD_SCHEMA:
        return {}
    entries = doc.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)}


def _store_record(entries: dict[str, dict[str, Any]]) -> None:
    """Write the record atomically. Raises on failure -- the caller must stop.

    A pending entry that fails to land must abort the spec write it was meant to
    describe (row 1): advancing the spec anyway is what leaves a generation no
    record can recognise.
    """
    path = _record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": RECORD_SCHEMA, "entries": entries}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _owned(entry: dict[str, Any] | None, digest: str | None) -> bool:
    """Does *entry* authorize writing over a file whose current digest is *digest*?

    A committed entry names one generation; a pending one names two (the
    generation it was replacing and the one it was writing), because an
    interrupted transaction may have left either on disk.

    ``None`` means the file EXISTS but could not be read (oversized, sensitive
    resolved target, permission), and that is never owned. Absence is not
    expressed here at all -- callers decide that from ``exists`` before asking --
    because conflating "no bytes to compare" with "nothing is there" would
    authorize overwriting, and pruning, a file this compiler could not inspect.
    """
    if digest is None:
        return False
    if not entry:
        return False
    if entry.get("state") == _STATE_COMMITTED:
        return digest == entry.get("output_sha256")
    if entry.get("state") == _STATE_PENDING:
        return digest in (entry.get("target_sha256"), entry.get("previous_sha256"))
    return False


def _read_existing(path: Path) -> tuple[bool, str | None]:
    """Return ``(exists, digest)`` for a compiled spec already on disk.

    Reads through the same hardened gate ``agent_discovery._read_agent_spec``
    uses -- resolved-symlink sensitivity check, size cap -- because this
    directory is user-writable and shared: a ``foo.json`` symlinked at
    ``~/.aws/credentials`` must not be read here just because a ``foo.md``
    appeared next to it.

    A file that exists but cannot be read returns ``(True, None)``, which no
    entry can own, so the compiler refuses rather than overwriting bytes it could
    not inspect.
    """
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return (path.exists() or path.is_symlink(), None)
    if is_sensitive_path(str(real)):
        logger.warning("refusing to inspect a sensitive compiled-spec target: %s", path)
        return (True, None)
    try:
        raw = safe_read_file_bytes(str(real))
    except FileTooLargeError:
        return (True, None)
    if raw is None:
        return (True, None)
    return (True, hashlib.sha256(raw).hexdigest())


def _sources(agents_dir: Path) -> list[Path]:
    """Markdown definitions in *agents_dir*, AppleDouble sidecars excluded."""
    if not agents_dir.is_dir():
        return []
    return sorted(p for p in agents_dir.glob(f"*{MD_SUFFIX}") if not p.name.startswith("._"))


def compile_markdown_agents(agents_dir: Path | None = None) -> CompileOutcome:
    """Compile every ``*.md`` in the user-level agents directory to a JSON spec.

    Idempotent: a source whose compiled output already matches byte-for-byte is
    left alone, so repeated passes do not churn mtimes (which would invalidate
    every roster cache keyed on this directory's signature).

    Never raises for a bad document -- a single unparseable definition must not
    stop the others from compiling, and must not fail gateway startup. Per-source
    refusals are collected in the returned outcome and logged. An unwritable
    record IS fatal to the pass, because continuing would write specs no record
    describes.
    """
    from kiro_crew.agent import _atomic_json_write, agents_spec_lock

    target_dir = agents_dir or kiro_agents_dir()
    outcome = CompileOutcome()
    sources = _sources(target_dir)
    entries = _load_record()
    if not sources and not entries:
        return outcome
    if not target_dir.is_dir():
        # No directory means no sources and no outputs, but the record may still
        # claim specs from before it was removed. Retire those claims without
        # taking the spec lock -- its sidecar lockfile lives in the directory that
        # is not there, so acquiring it would fail and strand the record instead.
        for name in [n for n in entries if not (target_dir / f"{n}.json").exists()]:
            del entries[name]
        try:
            _store_record(entries)
        except OSError:
            logger.exception("markdown agent record write failed for a missing agents dir")
        return outcome

    with agents_spec_lock(target_dir):
        seen: dict[str, str] = {}
        for source in sources:
            try:
                raw = source.read_bytes()
            except OSError as exc:
                outcome.refuse(source.name, "unreadable", str(exc))
                continue
            try:
                compiled = derive_spec(_decode(raw), fallback_name=source.stem)
            except MdAgentSpecError as exc:
                outcome.refuse(source.name, exc.code, str(exc))
                logger.warning("markdown agent %s: %s", source.name, exc)
                continue

            if compiled.name in seen:
                # Two documents claiming one name would race for one output file,
                # and which won would depend on directory order. Refuse the
                # second and name the first, rather than compile whichever sorted
                # later.
                outcome.refuse(
                    source.name,
                    "duplicate_name",
                    f"agent name {compiled.name!r} is already declared by {seen[compiled.name]}",
                )
                continue
            seen[compiled.name] = source.name

            target = target_dir / f"{compiled.name}.json"
            exists, current = _read_existing(target)
            entry = entries.get(compiled.name)
            if exists and not _owned(entry, current):
                if current is None:
                    code = "unreadable_output"
                elif entry:
                    code = "derived_modified"
                else:
                    code = "name_taken"
                outcome.refuse(
                    source.name,
                    code,
                    f"{target.name} was not written by this compiler (or has been "
                    f"edited since); leaving it untouched",
                )
                logger.warning(
                    "markdown agent %s: refusing to overwrite %s (%s)",
                    source.name,
                    target.name,
                    code,
                )
                continue
            if exists and current == compiled.digest:
                outcome.unchanged.append(compiled.name)
                entries[compiled.name] = {
                    "source": source.name,
                    "state": _STATE_COMMITTED,
                    "output_sha256": compiled.digest,
                }
                continue

            entries[compiled.name] = {
                "source": source.name,
                "state": _STATE_PENDING,
                "previous_sha256": current,
                "target_sha256": compiled.digest,
            }
            try:
                _store_record(entries)
            except OSError:
                logger.exception("markdown agent record write failed; not advancing any spec")
                return outcome
            try:
                _atomic_json_write(target, compiled.spec)
            except OSError as exc:
                outcome.refuse(source.name, "write_failed", str(exc))
                logger.warning("markdown agent %s: spec write failed: %s", source.name, exc)
                continue
            entries[compiled.name] = {
                "source": source.name,
                "state": _STATE_COMMITTED,
                "output_sha256": compiled.digest,
            }
            try:
                _store_record(entries)
            except OSError:
                # Row 5: the spec is durable and the pending entry still names its
                # digest, so the next pass recognises it. Nothing is stranded.
                logger.exception("markdown agent record commit failed for %s", compiled.name)
            outcome.written.append(compiled.name)

        _prune(target_dir, entries, seen, outcome)
    return outcome


def _prune(
    agents_dir: Path,
    entries: dict[str, dict[str, Any]],
    live: dict[str, str],
    outcome: CompileOutcome,
) -> None:
    """Remove compiled specs whose markdown source is gone.

    Only ever unlinks a file the record still owns. A spec the user has since
    edited is left on disk and merely stops being claimed -- deleting it would
    destroy an edit, and continuing to claim it would authorize that delete on
    some later pass.
    """
    stale = [name for name in entries if name not in live]
    if not stale:
        return
    for name in stale:
        entry = entries[name]
        target = agents_dir / f"{name}.json"
        exists, current = _read_existing(target)
        if not exists:
            del entries[name]
            continue
        if current is None:
            # Existing but unreadable: a TRANSIENT condition (oversized, a
            # permission blip, a symlink that currently resolves somewhere
            # refused), not evidence the user took the file over. Keep the claim
            # so a later pass can still prune it -- dropping it here would
            # abandon our own output permanently the first time one read failed.
            logger.warning(
                "markdown agent %s: source is gone but %s could not be read; "
                "keeping the claim and retrying next pass",
                name,
                target.name,
            )
            continue
        if not _owned(entry, current):
            logger.warning(
                "markdown agent %s: source is gone but %s has been edited; leaving it in place",
                name,
                target.name,
            )
            del entries[name]
            continue
        try:
            target.unlink()
        except OSError as exc:
            outcome.refuse(f"{name}{MD_SUFFIX}", "prune_failed", str(exc))
            continue
        del entries[name]
        outcome.pruned.append(name)
    try:
        _store_record(entries)
    except OSError:
        logger.exception("markdown agent record write failed after prune")

"""Version ids, batched deletes, and the version listing and erasure behind retention.

A plain delete on the versioned drive writes a marker and leaves the bytes behind it as
noncurrent versions; :func:`list_object_versions` and :func:`delete_object_versions` are
the pair that erases them, and :func:`validate_version_id` checks a recorded id before
it becomes an argv element.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend.storage_drive import (
    _MAX_KEY_LEN,
    SECTION_PREFIXES,
    section_key,
)
from kiro_crew.deploy.engine import AWSError, _checked

#: Version ids are OPAQUE: S3 documents them as URL-ready strings with no internal
#: structure, and the ids it mints draw on the full base64 alphabet, so ``+``, ``/``
#: and ``=`` all occur in real ones. This admits any printable ASCII without
#: whitespace; the one character that cannot lead is handled separately in
#: :func:`validate_version_id`, so each rule states its own reason. Length is bounded
#: by :data:`_MAX_VERSION_ID_LEN` so one number governs it.
#:
#: Applied with ``fullmatch`` and carrying no anchors, because ``$`` also matches
#: BEFORE a trailing newline: anchored with ``$`` this pattern accepts ``"abc\n"``
#: and sends a whitespace-bearing id to the CLI, which is the one thing it exists to
#: prevent.
_VERSION_ID_RE = re.compile(r"[\x21-\x7e]+")


def validate_version_id(value: Any) -> Optional[str]:
    """Return an error string when ``value`` cannot be passed as a ``--version-id``.

    This is a question about SYNTAX, not about identity. Whether a syntactically
    valid id names bytes this install can claim is a different question, decided by
    the caller against its own upload record -- ``backup._is_provable_version_id``
    is the one that rejects ``"null"``, which is well-formed here and names a
    version SLOT rather than one version. Two checks because they are two
    questions; one of them passing says nothing about the other.

    What makes the syntax check load-bearing is where the value lands.
    :func:`get_file` passes it as its own argv element directly after
    ``--version-id``, and that is the first place in the storage engine a version id
    becomes a bare argument rather than a field inside a JSON document (the delete
    path puts it in ``{"Key": ..., "VersionId": ...}``, where nothing can read it
    as anything else). There is no shell involved -- ``engine.run_aws`` spawns a
    fixed argv -- so this is not about shell metacharacters, which an argv list
    already neutralises. It is about the AWS CLI's OWN option grammar: its parser
    reads a leading ``-`` as the start of another option, so a stored id of
    ``--profile`` would silently repoint the call instead of naming a version.
    Refusing a leading ``-`` is what closes that, and a quoted argv cannot.

    The id arrives from ``backup.json``, which is local state this install wrote and
    which is NOT agent-writable: ``apps/aws-control/data`` sits behind the agent
    file-tool floor (``security._CREW_SECRET_LEAVES``) and is bind-masked from every
    agent sandbox (``sandbox._CREW_HIDDEN_LEAVES``). So this check is not standing
    between an agent and the CLI. It is here because the value crosses into an argv
    element where a leading ``-`` changes what the command MEANS, and a stored id is
    read back long after it was written, by which time a truncated or partially
    rewritten file is the ordinary way it goes wrong.

    The shape is as WIDE as S3's own contract and no wider. Version ids are opaque
    URL-ready strings drawn from the full base64 alphabet, so ``+``, ``/`` and ``=``
    all appear in real ones and a narrower alphabet would refuse the recovery read
    for genuine ids -- silently turning this whole path back into the refusal it
    exists to avoid. Printable ASCII without whitespace is the bound, because a
    control character or a newline is not something S3 mints and has no business
    reaching a log line or an argv element. Everything past the first character is
    inert as argv data, so the leading ``-`` is the entire security question and it
    gets its own check below.
    """
    if not isinstance(value, str) or not value:
        return "version id must be a non-empty string"
    # Reuses the module's existing ceiling rather than restating a number in the
    # pattern, so there is exactly one value to change. `_MAX_VERSION_ID_LEN` is
    # defined further down this module, beside the row-length bounds its other two
    # callers use; a module-level name is resolved when this runs, not when it is
    # defined, so reading it from above is fine.
    if len(value) > _MAX_VERSION_ID_LEN:
        return f"version id must be at most {_MAX_VERSION_ID_LEN} characters"
    # Its own check rather than a clause in the pattern, because it is the one rule
    # here that is about safety rather than about shape, and a reader should not
    # have to decode a character class to find it.
    if value.startswith("-"):
        return "version id must not start with '-'"
    if not _VERSION_ID_RE.fullmatch(value):
        return "version id must be printable ASCII with no spaces"
    return None


#: ``delete-objects`` accepts at most 1000 keys per request (a hard S3 API
#: limit, not a tunable). A folder with more objects than that MUST be paged,
#: so the constant is the batch size the caller walks the listing in — never an
#: assumption that one call clears the whole prefix.
_DELETE_BATCH_MAX = 1000

#: Byte ceiling for the serialized ``--delete`` document, which travels as ONE
#: argv element. Two limits bound it, and the tighter one wins:
#:
#:   * Linux caps a single argument at MAX_ARG_STRLEN (128 KiB).
#:   * Windows caps the WHOLE command line near 32 KiB (32767 chars) - and
#:     ``subprocess`` builds that line with ``list2cmdline``, which escapes every
#:     ``"`` as ``\"``. An S3 key may legitimately contain quotes (nothing stops
#:     another tool writing one), and a JSON document is quote-dense by
#:     construction, so a batch can DOUBLE on the way to CreateProcess.
#:
#: So the budget is set for the worst case rather than the typical one:
#: 12 KiB * 2 (every byte escaped) + roughly 300 bytes of fixed argv is about
#: 25 KiB, comfortably inside 32767. ``_WINDOWS_CMDLINE_MAX`` and the test that
#: multiplies these together keep the relationship honest if the cap is ever
#: raised - 1000 keys of 1024 chars would otherwise serialize to ~1 MB and fail
#: to spawn at all, which is an OSError and a 500 rather than a delete.
_DELETE_PAYLOAD_MAX_BYTES = 12 * 1024

#: The ceiling the budget above is derived from. Not a tunable: it is the
#: documented CreateProcess command-line limit.
_WINDOWS_CMDLINE_MAX = 32767


def _delete_entry_batches(entries: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Split ``delete-objects`` entries into batches fitting S3's cap and argv limits.

    An entry is the document S3 itself receives -- ``{"Key": k}`` to remove the
    current version, ``{"Key": k, "VersionId": v}`` to erase one specific
    version -- so the budget is measured on the bytes that actually travel. That
    matters here rather than being pedantry: a version id is another ~32
    characters plus its field name, so a batch of version-pinned entries is
    roughly twice the size of the same keys alone, and a budget derived from the
    keys would under-count it.

    Order is preserved and every entry appears exactly once: a split that dropped
    or duplicated one would under-delete (leaving objects behind) or make the
    reported count a lie. A single entry that alone exceeds the budget still gets
    its own batch - refusing it here would silently skip an object the caller
    asked to remove, so the spawn is attempted and any failure surfaces.
    """
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    # {"Objects":[],"Quiet":true} plus each entry's own serialized wrapper.
    overhead = len(json.dumps({"Objects": [], "Quiet": True}, separators=(",", ":")))
    size = overhead
    for obj in entries:
        cost = len(json.dumps(obj, separators=(",", ":")).encode()) + 1
        too_big = size + cost > _DELETE_PAYLOAD_MAX_BYTES
        if current and (too_big or len(current) >= _DELETE_BATCH_MAX):
            batches.append(current)
            current, size = [], overhead
        current.append(obj)
        size += cost
    if current:
        batches.append(current)
    return batches


def _delete_batches(keys: list[str]) -> list[list[str]]:
    """Batch plain keys for ``delete-objects``. See :func:`_delete_entry_batches`."""
    wrapped = _delete_entry_batches([{"Key": key} for key in keys])
    return [[str(entry["Key"]) for entry in batch] for batch in wrapped]


class DeleteObjectsPartialFailure(AWSError):
    """``delete-objects`` answered 200 and named entries it could not remove.

    Carries how many it named. ``Quiet`` is set at every call site, so the
    response lists ONLY failures: a caller holding the batch can subtract and know
    exactly how many entries that batch DID erase. On the version-delete path that
    subtraction is the difference between auditing erased bytes and auditing zero.

    Still an :class:`AWSError`, so the folder sweep keeps catching it unchanged.
    """

    def __init__(self, message: str, failures: int) -> None:
        super().__init__(message)
        self.failures = failures


def _raise_on_delete_errors(out: str) -> None:
    """Turn a per-key DeleteObjects failure into an ``AWSError``.

    ``delete-objects`` answers 200 with an ``Errors`` array when it could not
    remove some keys, so the CLI exits 0 and the caller would otherwise count
    them as deleted.

    An EMPTY body is success: with ``Quiet`` a fully successful call returns
    nothing. A non-empty body that will not parse is NOT success - the call site
    pins ``--output json``, so unparseable output means something unexpected
    happened, and on a destructive path the honest answer is to report failure
    rather than to assume the objects are gone.
    """
    if not (out or "").strip():
        return
    try:
        parsed = json.loads(out) or {}
    except json.JSONDecodeError:
        raise AWSError(
            "delete-objects returned a response that could not be read as JSON; "
            "refusing to report the folder as deleted"
        ) from None
    errors = parsed.get("Errors") or []
    if not errors:
        return
    first = errors[0] if isinstance(errors[0], dict) else {}
    code = first.get("Code", "unknown")
    key = first.get("Key", "?")
    raise DeleteObjectsPartialFailure(
        f"delete-objects could not remove {len(errors)} object(s) — "
        f"first: {key} ({code}); the folder is only partially deleted",
        len(errors),
    )


#: One version-listing window per round-trip, the same client-side pagination the
#: drive's other walks use. This is what bounds the response the CLI builds in
#: memory for a single call; an auto-paginated listing bounds nothing, because the
#: CLI joins every page before this process sees a byte of it.
_VERSION_PAGE_ITEMS = 1000

#: The most version rows one folder listing will retain, as ten full delete
#: batches. A folder needing more than ten batches to clear is past what this app
#: should sweep object by object, and the answer there is a bucket lifecycle rule
#: rather than a larger buffer inside the gateway.
_VERSION_ROWS_MAX = 10 * _DELETE_BATCH_MAX

#: S3's own ceiling for a version id. A longer value cannot name a real version.
#: Two readers share it. For a retained row it pairs with :data:`_MAX_KEY_LEN` and
#: :data:`_MAX_MODIFIED_LEN` to bound the row's unbounded-length fields, and none is
#: ever shortened to fit: a truncated key or version id names a DIFFERENT object, so
#: an over-long row is dropped instead of trimmed. :func:`validate_version_id`
#: applies the same ceiling on the way OUT, to an id this install recorded earlier
#: and is about to pass to the CLI. It is one fact about S3 in both places, so it is
#: one number; the row-shape pairing above describes rows alone and does not
#: enumerate the callers.
_MAX_VERSION_ID_LEN = 1024

#: The third retained variable-length field. An ISO-8601 instant needs about 25
#: characters, so this is loose and still bounds the value; it exists because the
#: pairing above claimed to cover every field a row retains and did not.
_MAX_MODIFIED_LEN = 64


def list_object_versions(
    profile: str, region: str, bucket: str, section: str, subpath: str, *, account: str
) -> list[dict[str, Any]]:
    """Every version AND delete marker under ``section/subpath/``.

    The drive has versioning ENABLED (see the module docstring), and that one
    fact is why this listing exists beside :func:`list_section`.
    ``list-objects-v2`` answers only about CURRENT versions, so a caller that
    deletes what it returns reclaims nothing: :func:`delete_key` without a
    version id writes a delete MARKER, the bytes stay behind it as a noncurrent
    version, and the bucket goes on being billed for every one of them. This is
    the listing a caller needs in order to remove bytes rather than hide them,
    because it names the ``VersionId`` of each version -- the only form
    :func:`delete_object_versions` can actually erase.

    Delete markers come back TAGGED rather than filtered out. A key whose newest
    entry is a marker is not a live object, and a caller that could not see the
    marker would read the older version underneath it as live.

    Keys are section-RELATIVE, like :func:`list_section` and every key this
    package passes around, so a caller can compare one against a key it wrote.
    They are RAW, unlike :func:`list_section`: this is an identity read whose
    answers are compared against keys and then deleted, and a redacted name
    matches no key -- so a caller fed the display listing could read an archive
    that exists as absent.

    ``subpath`` must name a folder. Every caller of this is about to delete, and
    a whole-section version listing is not a blast radius this function hands
    out. The prefix is anchored on :data:`SECTION_PREFIXES` and closed with a
    trailing ``/``, so listing ``snapshots/abc`` cannot reach a sibling
    ``snapshots/abcdef/``.

    Paged with ``--max-items`` and walked to the end of the token chain, so the
    answer is still the COMPLETE set or a raised error, never a first page a
    caller could mistake for the whole prefix. Bounding the PAGE is what keeps the
    peak in memory bounded; bounding the ANSWER is not on offer here, so a prefix
    holding more than :data:`_VERSION_ROWS_MAX` versions RAISES rather than
    returning what fits. An unreadable response raises for the same reason, unlike
    :func:`usage`: an empty list here reads as "nothing worth keeping", and a
    caller acting on that would delete on a view it never had.
    """
    # Whitespace as well as slashes: `validate_key` requires a segment to START
    # alphanumeric, so no legitimate folder is changed by the strip, and a value
    # that is nothing but spaces would otherwise build the prefix `backup/   /`
    # and list a folder nobody named.
    leaf = subpath.strip().strip("/").strip()
    if not leaf:
        raise ValueError(
            "list_object_versions needs a folder; a whole-section version listing "
            "is not offered here"
        )
    prefix = f"{SECTION_PREFIXES[section]}{leaf}/"
    rows: list[dict[str, Any]] = []
    token = ""
    while True:
        args = [
            "s3api",
            "list-object-versions",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--max-items",
            str(_VERSION_PAGE_ITEMS),
            "--expected-bucket-owner",
            account,
            "--output",
            "json",
        ]
        if token:
            args += ["--starting-token", token]
        out = _checked(
            args,
            profile,
            action="s3:ListBucketVersions",
            timeout=60,
        )
        try:
            data = json.loads(out or "{}") or {}
        except json.JSONDecodeError:
            raise AWSError(
                "the version listing returned a response that could not be read as JSON; "
                "refusing to report the folder as empty"
            ) from None
        if not isinstance(data, dict):
            raise AWSError(
                "the version listing returned a document that is not an object; "
                "refusing to report the folder as empty"
            )
        for field, is_marker in (("Versions", False), ("DeleteMarkers", True)):
            page = data.get(field) or []
            if not isinstance(page, list):
                continue
            for obj in page:
                if not isinstance(obj, dict):
                    continue
                key, version = obj.get("Key"), obj.get("VersionId")
                # Both halves or nothing. A row missing either one cannot be deleted
                # by version, and filling in the missing half by guessing is how a
                # delete lands on an object the caller never named.
                if not isinstance(key, str) or not isinstance(version, str):
                    continue
                if not version or not key.startswith(prefix):
                    continue
                # Length is checked before the row is retained, and an over-long
                # field drops the row rather than being cut to fit. Nothing this
                # install wrote can reach either bound, because `validate_key`
                # holds every key it accepts under the same one, so the rows this
                # drops are rows retention could not have owned anyway.
                if len(key) > _MAX_KEY_LEN or len(version) > _MAX_VERSION_ID_LEN:
                    continue
                modified_raw = obj.get("LastModified")
                # `modified` is retained too, so the same rule reaches it: this is the
                # third field in this row, not a second mechanism. It DROPS rather than
                # emptying, because an empty timestamp sorts as oldest and would make
                # the row a likelier deletion candidate -- the unsafe direction for a
                # value that arrived malformed.
                if isinstance(modified_raw, str) and len(modified_raw) > _MAX_MODIFIED_LEN:
                    continue
                if len(rows) >= _VERSION_ROWS_MAX:
                    raise AWSError(
                        f"this folder holds more than {_VERSION_ROWS_MAX} object "
                        "versions; refusing to answer with the part that fits, "
                        "because a caller would delete on it. Clear the history "
                        "with a bucket lifecycle rule"
                    )
                modified = modified_raw
                size = obj.get("Size")
                rows.append(
                    {
                        "key": key[len(SECTION_PREFIXES[section]) :],
                        "versionId": version,
                        "modified": modified if isinstance(modified, str) else "",
                        "size": (
                            size if isinstance(size, int) and not isinstance(size, bool) else 0
                        ),
                        # S3's own answer about which version a plain GET would
                        # return, rather than one inferred from timestamps.
                        "latest": bool(obj.get("IsLatest")),
                        "deleteMarker": is_marker,
                    }
                )
        token = data.get("NextToken", "")
        if not isinstance(token, str) or not token:
            return rows


class PartialVersionDelete(RuntimeError):
    """A batched version delete that failed AFTER erasing some versions.

    :func:`delete_object_versions` reports its count by RETURNING it, and on this
    path that count is the only record that bytes are gone. A bare raise carries
    no count, so a caller auditing the failure would file "nothing was deleted"
    over versions that are already erased -- the one shape the retention audit
    exists to prevent. This class carries the count across the raise instead.

    Raised only when at least one batch has already completed. A first-batch
    failure erases nothing, so there the original error is the honest signal and
    is re-raised untouched rather than dressed up as a partial.

    ``removed`` counts VERSIONS, not keys: batches are filled to the API's limit
    without regard to key boundaries, so the erased set does not map to a clean
    number of keys and this class does not invent one.
    """

    def __init__(self, removed: int, cause: BaseException) -> None:
        super().__init__(f"{removed} version(s) erased before the failure: {cause}")
        self.removed = removed


def delete_object_versions(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    versions: list[tuple[str, str]],
    *,
    account: str,
) -> int:
    """Erase specific object VERSIONS. Returns the number removed.

    Unlike :func:`delete_key` and :func:`delete_prefix`, this removes bytes. A
    delete pinned to a ``VersionId`` erases that version and leaves NO delete
    marker behind, which on this versioned bucket is the whole difference between
    reclaiming storage and hiding an object that keeps billing.

    It is therefore unrecoverable at the S3 layer, and that is why WHICH versions
    is entirely the caller's decision: this function anchors the section prefix
    and does nothing else. There is deliberately no prefix argument that could
    widen to "every version under a folder" -- the caller passes the exact
    (key, version) pairs it means, having listed them with
    :func:`list_object_versions`.

    Owner-pinned like every other write, for the same bucket-name-reuse reason.
    Per-key failures arrive inside a 200 response, so the same
    :func:`_raise_on_delete_errors` check the folder sweep uses runs here: a
    count returned by this function means those versions are gone.

    A failure partway through a multi-batch delete raises
    :class:`PartialVersionDelete` instead, carrying the count already erased --
    the count is this function's only report, so discarding it would leave the
    caller auditing erased bytes as nothing.
    """
    entries = [
        {"Key": section_key(section, key), "VersionId": version}
        for key, version in versions
        if key and version
    ]
    if not entries:
        return 0
    removed = 0
    for batch in _delete_entry_batches(entries):
        payload = json.dumps({"Objects": batch, "Quiet": True}, separators=(",", ":"))
        try:
            out = _checked(
                [
                    "s3api",
                    "delete-objects",
                    "--bucket",
                    bucket,
                    "--delete",
                    payload,
                    "--expected-bucket-owner",
                    account,
                    # The error check below reads this as JSON; a user's
                    # `output = text` in ~/.aws/config would otherwise turn the
                    # check into a no-op on their machine only.
                    "--output",
                    "json",
                ],
                profile,
                action="s3:DeleteObjectVersion",
            )
            _raise_on_delete_errors(out)
        except Exception as exc:
            # A mixed batch answers 200 and names ONLY the entries it could not
            # remove, so the rest of that batch is erased and has to be counted --
            # otherwise a first batch that half succeeded reports zero. When the
            # CLI itself failed there is no response to subtract from, so that
            # batch contributes nothing rather than a guess.
            if isinstance(exc, DeleteObjectsPartialFailure):
                removed += max(0, len(batch) - exc.failures)
            # The count is this function's only report, so raising past it would
            # tell the caller nothing happened while bytes are already gone.
            # Nothing erased yet means there is no partial to report.
            if removed:
                raise PartialVersionDelete(removed, exc) from exc
            raise
        removed += len(batch)
    return removed

"""Where a redacted credential came from, recorded without keeping the credential.

The credential redactor replaces a secret with ``[REDACTED: credential]`` before
a message is saved. The reader then needs three facts the placeholder cannot
carry: that the value was removed before saving, where it still lives, and how
to see it there. This module produces the second and third.

The value itself is never retained. A tool result is fingerprinted as it is
redacted: every credential the redactor removes from it is reduced to a keyed
digest (the key is random per process and never leaves it) plus the ini-style
section it sat under. When an agent reply later carries a credential, its
digest is looked up among the turn's tool results. A hit names the tool call
that produced the value, which names its source: the file that was read, or
the command that was run. Only that description is stored in the message.

A command offered to the reader is either one that already ran or a fixed
template filled from non-secret parts (an AWS profile name). Never free text
the agent wrote: a command pre-filled into a terminal sits one keypress from
running, so a prompt-injected agent must not be able to choose it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from collections import deque
from typing import Any

from kiro_crew.security.exfil import redact_exfiltration_urls
from kiro_crew.security.redaction import (
    CREDENTIAL_REDACTION_TAGS,
    CredentialMatch,
    redact_credentials,
    redact_credentials_with_records,
)

_FINGERPRINT_KEY = secrets.token_bytes(32)

#: Tool results the evidence keeps for one turn. Older results are dropped
#: first; a credential they held then reads as "source not recorded".
MAX_EVIDENCE_RESULTS = 64
#: Fingerprints kept per tool result.
MAX_FINGERPRINTS_PER_RESULT = 64
#: Records one message may carry; the rest keep their placeholder unexplained.
MAX_CREDENTIAL_RECORDS_PER_MESSAGE = 64

MAX_PATH_CHARS = 512
MAX_SECTION_CHARS = 64
MAX_COMMAND_CHARS = 1000
MAX_LABEL_CHARS = 48

_SECTION_RE = re.compile(r"^[ \t]*\[([A-Za-z0-9 _./:@-]{1,64})\][ \t]*$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
_RULE_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_LABEL_RE = re.compile(r"^[A-Za-z_]{1,32}[\"']?\s{0,4}[:=]\s{0,4}[\"']?$")

#: Rules whose value is a session token: short-lived, so the card recommends
#: using the profile rather than viewing the token.
SESSION_TOKEN_RULES = frozenset({"aws_session_token"})
#: Rules the AWS credentials-file templates cover, with the key each reads.
_AWS_TEMPLATE_KEYS = {
    "aws_secret_access_key": "aws_secret_access_key",
    "bare_aws_secret": "aws_secret_access_key",
    "aws_session_token": "aws_session_token",
    "aws_access_key_id": "aws_access_key_id",
}

RECORD_KEYS = frozenset({"ordinal", "rule", "label", "source", "view_command", "profile_command"})


def fingerprint(value: str) -> str:
    """A keyed digest of ``value``; the key never leaves this process."""
    return hmac.new(
        _FINGERPRINT_KEY, value.encode("utf-8", "surrogatepass"), hashlib.sha256
    ).hexdigest()[:32]


def _section_before(text: str, index: int) -> str | None:
    """The nearest ``[section]`` header line above ``index``, if any."""
    for line in reversed(text[:index].splitlines()):
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip()
            return section if _is_clean(section) else None
    return None


def tool_output_fingerprints(text: str) -> tuple[tuple[str, str | None], ...]:
    """``(fingerprint, section)`` for every credential redacted from ``text``.

    Mirrors the order the ACP layer redacts in (exfiltration URLs, then
    credentials), so a credential it removed is one this sees.
    """
    if not text:
        return ()
    urls_done, _ = redact_exfiltration_urls(text)
    _, _, matches = redact_credentials_with_records(urls_done)
    out: list[tuple[str, str | None]] = []
    for m in matches[:MAX_FINGERPRINTS_PER_RESULT]:
        if not m.value:
            continue
        at = urls_done.find(m.value)
        out.append((fingerprint(m.value), _section_before(urls_done, at) if at >= 0 else None))
    return tuple(out)


def _is_clean(value: str) -> bool:
    """True when ``value`` carries no secret and reads the way it runs.

    Neither remover may change it, and it may hold no control, format or bidi
    character (Unicode category C): a card shows this text and pre-fills it into
    a terminal, and a character such as U+202E makes the shown text differ from
    the text that would run.
    """
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        return False
    if any(tag in value for tag in CREDENTIAL_REDACTION_TAGS) or "[REDACTED" in value:
        return False
    if redact_credentials(value)[0] != value:
        return False
    return redact_exfiltration_urls(value)[0] == value


def _tool_source(tool_input: str, *, is_shell: bool) -> dict[str, Any] | None:
    """The source a tool call names: the file it read or the command it ran.

    A ``command`` is a source only for a call the backend identified as a shell
    command: any other tool can carry a ``command`` field the agent chose, and
    offering that as a command that ran would let the agent pick what the card
    pre-fills.
    """
    try:
        parsed = json.loads(tool_input)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    command = parsed.get("command")
    if isinstance(command, str) and command.strip():
        if not is_shell:
            return None
        command = command.strip()
        if len(command) <= MAX_COMMAND_CHARS and "\n" not in command and _is_clean(command):
            return {"type": "command", "command": command}
        return None
    path: object = parsed.get("path") or parsed.get("file_path")
    ops = parsed.get("operations")
    if not path and isinstance(ops, list) and ops:
        # A batch read returns every file's content in one result, with no
        # boundary this can trust, so a value in it cannot be tied to one
        # file. Name a file only when the batch reads exactly one.
        # Only string paths count: the model writes these arguments, and a
        # list or dict here is unhashable and names no file.
        paths = {
            p for op in ops if isinstance(op, dict) and isinstance(p := op.get("path"), str) and p
        }
        if len(paths) == 1:
            path = paths.pop()
    if isinstance(path, str) and path and len(path) <= MAX_PATH_CHARS and _is_clean(path):
        return {"type": "file", "path": path, "section": None}
    return None


class CredentialEvidence:
    """One turn's tool results, reduced to sources and credential fingerprints."""

    def __init__(self) -> None:
        self._results: deque[tuple[dict[str, Any], dict[str, str | None]]] = deque(
            maxlen=MAX_EVIDENCE_RESULTS
        )

    def clear(self) -> None:
        self._results.clear()

    def record(
        self,
        tool_input: str,
        fingerprints: tuple[tuple[str, str | None], ...],
        *,
        is_shell: bool = False,
    ) -> None:
        if not fingerprints:
            return
        source = _tool_source(tool_input, is_shell=is_shell)
        if source is None:
            return
        self._results.append((source, dict(fingerprints[:MAX_FINGERPRINTS_PER_RESULT])))

    def locate(self, value: str) -> dict[str, Any] | None:
        """The newest source whose output held ``value``, or None."""
        if not value or not self._results:
            return None
        fp = fingerprint(value)
        for source, prints in reversed(self._results):
            if fp in prints:
                found = dict(source)
                if found["type"] == "file":
                    found["section"] = prints[fp]
                return found
        return None


def _aws_commands(rule: str, source: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """``(view_command, profile_command)`` from the AWS templates, or Nones."""
    if not source or source.get("type") != "file":
        return None, None
    path = str(source.get("path") or "")
    section = source.get("section")
    key = _AWS_TEMPLATE_KEYS.get(rule)
    if key is None or not isinstance(section, str) or not _PROFILE_RE.match(section):
        return None, None
    # Either separator: a recorded path may come from a Windows host.
    parts = re.split(r"[\\/]", path)
    if parts[-1] != "credentials" or ".aws" not in parts:
        return None, None
    view = f"aws configure get {key} --profile {section}"
    profile = (
        f"AWS_PROFILE={section} aws sts get-caller-identity"
        if rule in SESSION_TOKEN_RULES
        else None
    )
    return view, profile


def credential_record(
    match: CredentialMatch, evidence: CredentialEvidence | None
) -> dict[str, Any]:
    """The persisted description of one credential placeholder."""
    source = evidence.locate(match.value) if evidence is not None else None
    view, profile = _aws_commands(match.rule, source)
    if view is None and source is not None and source.get("type") == "command":
        view = str(source["command"])
    label = match.label if _LABEL_RE.match(match.label or "") else ""
    return {
        "ordinal": match.ordinal,
        "rule": match.rule if _RULE_RE.match(match.rule) else "credential_pattern",
        "label": label,
        "source": source,
        "view_command": view,
        "profile_command": profile,
    }


def credential_records(
    matches: list[CredentialMatch], evidence: CredentialEvidence | None
) -> list[dict[str, Any]]:
    return [credential_record(m, evidence) for m in matches[:MAX_CREDENTIAL_RECORDS_PER_MESSAGE]]


def _valid_source(source: object) -> bool:
    if source is None:
        return True
    if not isinstance(source, dict):
        return False
    kind = source.get("type")
    if kind == "command":
        command = source.get("command")
        return (
            set(source) == {"type", "command"}
            and isinstance(command, str)
            and 0 < len(command) <= MAX_COMMAND_CHARS
            and "\n" not in command
            and _is_clean(command)
        )
    if kind == "file":
        path = source.get("path")
        section = source.get("section")
        return (
            set(source) == {"type", "path", "section"}
            and isinstance(path, str)
            and 0 < len(path) <= MAX_PATH_CHARS
            and _is_clean(path)
            and (
                section is None
                or (
                    isinstance(section, str)
                    and 0 < len(section) <= MAX_SECTION_CHARS
                    and _is_clean(section)
                )
            )
        )
    return False


def revalidate_credential_record(record: object) -> dict[str, Any] | None:
    """``record`` when every field is well formed and secret-free, else None.

    A command field must equal what the record's own rule and source produce
    (the AWS template) or the recorded command itself, so a record edited at
    rest cannot plant a command the redactor never offered.
    """
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        return None
    ordinal = record["ordinal"]
    rule = record["rule"]
    label = record["label"]
    source = record["source"]
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 0 <= ordinal < 10_000:
        return None
    if not isinstance(rule, str) or not _RULE_RE.match(rule):
        return None
    if not isinstance(label, str) or (
        label and not (len(label) <= MAX_LABEL_CHARS and _LABEL_RE.match(label))
    ):
        return None
    if not _valid_source(source):
        return None
    view, profile = _aws_commands(rule, source)
    if view is None and isinstance(source, dict) and source.get("type") == "command":
        view = source["command"]
    if record["view_command"] != view or record["profile_command"] != profile:
        return None
    return record


def bounded_credential_records(raw: object) -> list[dict[str, Any]]:
    """The only way a record list read back from a row becomes one: bounded, then re-checked."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_CREDENTIAL_RECORDS_PER_MESSAGE]:
        ok = revalidate_credential_record(item)
        if ok is not None:
            out.append(ok)
    return out

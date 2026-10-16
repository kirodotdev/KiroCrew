"""What an artifact route sends back: ``_serialize`` and the redaction passes over it.

The record serializer, the session-title resolution it shows, and the redactors every
route applies to what it returns: free text (``_redact_text``), webapp metadata, SEL
audit metadata and a publish provider's raw response (``_redact_remote_response``).
"""

from __future__ import annotations

import base64
import copy
from typing import Any

from kiro_crew.dashboard.state import _normalize_slot_key
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import (
    _B64_CHUNK_RE,
    _HARD_CREDENTIAL_RE,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.validation import infer_use_case

#: Shown in the Source column when an artifact's originating session does not
#: exist (the artifact itself is kept — sessions and artifacts have independent
#: lifecycles).
_DELETED_SESSION_LABEL = "(deleted session)"


def _resolve_session_title(state: Any, session_key: str) -> str:
    """Live-resolve a session_key to its current chat title for the Source column.

    Returns the session's current display title while it exists (so renames are
    reflected), ``"(deleted session)"`` when the key referenced a session that
    is now gone, and ``""`` when there is no originating session at all (e.g. a
    non-chat origin or a legacy artifact) — the caller falls back to the origin
    label in that case.

    Two families of session have a chat title. A dashboard-born one is named by
    its own key. A channel-born one runs under the channel's key
    (``slack:<ts>``), so ``infer_use_case`` reports its origin rather than
    ``dashboard`` — it still has a tab, and a title, whenever the dashboard is
    displaying it. Either family reaches here in two spellings, the full session
    key from MCP callers and the bare slot name the browser create path stores,
    and ``_normalize_slot_key`` folds both onto the slot name the slot table is
    keyed by.
    """
    if not session_key:
        return ""
    # Every other origin (cron / subagent / cli / task-runner) has no chat
    # title; "" lets the caller show the origin label instead of mislabeling
    # them "(deleted session)".
    channel_born = is_channel_session_key(session_key)
    if not channel_born and infer_use_case(session_key) != "dashboard":
        return ""
    slot = state.get_slot(_normalize_slot_key(session_key)) if state is not None else None
    if slot is None:
        # A dashboard session with no slot is gone. A channel session with no
        # slot merely has no tab open — the conversation still lives on the
        # channel, so its origin label is the honest answer.
        return "" if channel_born else _DELETED_SESSION_LABEL
    try:
        return slot.display_title
    except Exception:  # pragma: no cover — never let title resolution break a list
        return _DELETED_SESSION_LABEL


def _redact_audit_metadata(obj: Any) -> Any:
    """Recursively redact every string leaf of SEL ``extra`` metadata.

    Provider-controlled values (e.g. ``external_id``) reach the audit log via
    ``extra`` and can be credential-shaped, so they pass the same
    seam-aware credential/exfil redaction as ``error``.
    """
    from kiro_crew.platform.context import redact_via_context

    if isinstance(obj, str):
        return redact_via_context(obj)
    if isinstance(obj, dict):
        return {
            (redact_via_context(k) if isinstance(k, str) else k): _redact_audit_metadata(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_audit_metadata(v) for v in obj]
    return obj


def _serialize(art: Any, *, include_content: bool = False, state: Any = None) -> dict[str, Any]:
    """Serialize an Artifact for response.

    All LLM-originated string fields (``name``, ``description``, ``tags``,
    the image block's ``alt`` / ``original_filename``, and — when
    ``include_content=True`` — ``content``) pass through
    ``redact_exfiltration_urls()`` + ``redact_credentials()`` per
    the ``security-controls`` rule. Artifact metadata is set
    by the agent via ``artifact_save`` / ``artifact_update``, so any
    field originating in LLM output must not reach the dashboard surface
    unredacted.
    """
    out = art.to_dict(include_content=include_content)
    for key in ("name", "description"):
        val = out.get(key)
        if isinstance(val, str) and val:
            cleaned, _ = redact_exfiltration_urls(val)
            cleaned, _ = redact_credentials(cleaned)
            out[key] = cleaned
    if isinstance(out.get("tags"), list):
        out["tags"] = [_redact_text(t) if isinstance(t, str) else t for t in out["tags"]]
    if include_content and out.get("content"):
        cleaned = out["content"]
        cleaned, _ = redact_exfiltration_urls(cleaned)
        cleaned, _ = redact_credentials(cleaned)
        out["content"] = cleaned
    # Live-resolve the originating session's current title for the Source column.
    # Only set when we can resolve (session present, or gone -> "(deleted
    # session)"); when there's no originating session the key is absent and the
    # frontend falls back to the origin label.
    if state is not None:
        title = _resolve_session_title(state, out.get("session_key") or "")
        if title:
            out["session_title"] = _redact_text(title)
    # Publication block is structural — view_url is an internal
    # CloudFront URL and aliases are user input — but ``last_error`` can echo
    # an arbitrary upstream error string, so redact it like other surfaced
    # text per the security-controls rule.
    pub = out.get("publication")
    if isinstance(pub, dict) and isinstance(pub.get("last_error"), str) and pub["last_error"]:
        pub["last_error"] = _redact_text(pub["last_error"])
    if isinstance(out.get("webapp_metadata"), dict):
        out["webapp_metadata"] = _redact_webapp_metadata(out["webapp_metadata"])
    # Image block: ``alt`` and ``original_filename`` are derived from markdown the
    # agent wrote, so they are LLM-originated exactly like ``name`` and must pass
    # the same gate. This matters more than it looks: the dashboard prefers
    # ``image.alt`` over ``name`` for the accessible description, so leaving it
    # raw would route unredacted text onto the surface that ``name``'s redaction
    # exists to protect. The numeric/structural leaves (mime, ext, size, sha256,
    # dimensions) are store-computed and left alone.
    img = out.get("image")
    if isinstance(img, dict):
        for key in ("alt", "original_filename"):
            val = img.get(key)
            if isinstance(val, str) and val:
                img[key] = _redact_text(val)
    return out


def _redact_text(text: str) -> str:
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _redact_webapp_metadata(obj: Any) -> Any:
    """Recursively redact every string leaf in a webapp_metadata sub-tree.

    webapp_metadata (deploy target, architecture, resource ids, cost note,
    teardown handle, origin session) is LLM-set like name / description,
    so it must pass the same exfiltration + credential redaction before reaching
    the dashboard surface.
    """
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, dict):
        return {
            (_redact_text(k) if isinstance(k, str) else k): _redact_webapp_metadata(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_webapp_metadata(v) for v in obj]
    return obj


#: Top-level keys ``_serialize`` has already passed through the redactors, so
#: ``_redact_remote_response`` need not rescan them (avoids a second full-content
#: regex pass over the ≤25 MiB ``content`` body on the event loop).
_SERIALIZE_REDACTED_KEYS = frozenset({"content", "name", "description", "tags"})

#: Defense-in-depth cap on how deep the remote-response redactor walks, matching
#: the Block Kit sanitizer in ``messaging.py`` — a pathologically nested provider
#: response can't drive a ``RecursionError`` (which would escape the handler's
#: 502 mapping as an unhandled 500). Nesting beyond this is truncated.
_MAX_REDACT_DEPTH = 10

#: Keys in a remote/provider response that hold an OPAQUE provider identifier —
#: a join key (matched against the local index) and the action handle the FE
#: sends back verbatim for clone/fork. These are never human-readable prose. For
#: them the redactor skips ONLY the entropy/exfil heuristic (which false-positives
#: on a benign high-entropy id — UUID / content hash — and would rewrite it to
#: ``[REDACTED]``, breaking clone/fork of that id) but STILL runs hard-credential
#: redaction, so a provider that embeds a literal AKIA/SSH/Slack token in an id
#: cannot reach the dashboard verbatim. Titles/snippets/owners are redacted in
#: full (both heuristic and hard-credential).
_REMOTE_ID_KEYS = frozenset({"external_id", "artifactId", "id"})

#: Replacement for a provider id that embeds a literal hard credential — the
#: whole id is dropped (a clone/fork of it fails rather than round-tripping the
#: token back to the browser and provider).
_REMOTE_ID_CRED_TAG = "[REDACTED: credential]"


def _id_embeds_hard_credential(value: str) -> bool:
    """True if an opaque provider id embeds a hard credential — literal OR
    base64-encoded.

    The id-key branch of ``_redact_remote_response`` deliberately skips the
    ENTROPY heuristic (a benign UUID / content hash is high-entropy and must
    survive so clone/fork can send it back), but a malicious provider could
    still smuggle a real token in the id. We therefore run the hard-credential
    floor two ways: on the raw id, and on any base64-shaped chunk decoded to
    text. A benign high-entropy id decodes to non-credential bytes (or fails to
    decode), so this preserves the exemption while closing the encoded-token
    hole. Only the hard floor is applied to the decoded bytes — NOT the entropy
    heuristic — so a benign id that merely *looks* base64 is not rewritten."""
    if _HARD_CREDENTIAL_RE.search(value):
        return True
    for m in _B64_CHUNK_RE.finditer(value):
        try:
            decoded = base64.b64decode(m.group(), validate=True).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if _HARD_CREDENTIAL_RE.search(decoded):
            return True
    return False


def _redact_remote_response(data: dict, *, already_redacted: frozenset[str] = frozenset()) -> dict:
    """Redact credential patterns and exfiltration URLs from a remote/provider
    response before it reaches the dashboard.

    Walks nested dicts AND lists — *including* lists nested inside lists, which a
    walker handling only dicts/strings inside a top-level list silently skips — up
    to ``_MAX_REDACT_DEPTH`` levels. A single ``deepcopy`` at entry isolates the
    caller's object; the recursion then rewrites in place instead of re-copying
    every subtree at each level, which would make redaction O(n·depth).

    ``already_redacted`` names top-level keys whose string values the caller has
    already passed through the same redactors (e.g. ``_serialize`` redacts an
    up-to-25 MiB ``content`` body), so they are not rescanned a second time.
    Strips ``localPath`` (a leaked local filesystem path) from the top level.

    The walk builds fresh containers as it goes (so it doubles as the copy — no
    separate unbounded ``copy.deepcopy`` of the input, which would itself
    ``RecursionError`` on a pathologically nested provider response before the
    depth cap could take effect).
    """

    def _walk(value: Any, depth: int, key: str = "") -> Any:
        # Opaque provider identifiers are join keys + action handles (the FE
        # sends external_id straight back as the clone/fork target), NOT prose.
        # The exfil/entropy heuristic false-positives on a benign high-entropy
        # id (UUID / content hash) and would rewrite it to [REDACTED], breaking
        # clone/fork of that id. So for these keys we skip ONLY the entropy
        # heuristic — but STILL run hard-credential redaction, so a provider that
        # smuggles a literal AKIA/SSH/Slack token in the id can't reach the
        # dashboard verbatim. A benign id passes through unchanged; an id that
        # actually contains a credential is redacted (and a clone of it would
        # legitimately fail rather than exfiltrate).
        if key in _REMOTE_ID_KEYS and isinstance(value, str):
            # Use the HARD-credential floor only (literal AKIA/ASIA/SSH/PEM/Slack
            # markers + base64-encoded variants), NOT redact_credentials — the
            # latter also runs the bare-secret ENTROPY heuristic, which
            # false-positives on a benign high-entropy id (UUID / content hash)
            # and would break clone/fork. If a hard credential IS embedded
            # (literal or base64), redact the whole id (a clone of it should
            # fail rather than exfiltrate the token).
            return _REMOTE_ID_CRED_TAG if _id_embeds_hard_credential(value) else value
        if depth > _MAX_REDACT_DEPTH:
            # Redact a boundary string; truncate deeper containers rather than
            # recurse further (defense-in-depth, mirrors messaging._sanitize_blocks).
            if isinstance(value, str):
                return _redact_text(value) if value else value
            if isinstance(value, dict):
                return {}
            if isinstance(value, list):
                return []
            return value
        if isinstance(value, str):
            return _redact_text(value) if value else value
        if isinstance(value, dict):
            return {k: _walk(v, depth + 1, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(item, depth + 1) for item in value]
        return value

    out: dict = {}
    for key, val in data.items():
        # An already-redacted top-level value is copied (deep) as-is rather than
        # rescanned. copy.deepcopy is bounded here — these values (e.g. the
        # serialized ``content`` string / ``tags`` list) are not adversarially
        # nested — and keeps the response independent of the caller's object.
        out[key] = copy.deepcopy(val) if key in already_redacted else _walk(val, 1)
    out.pop("localPath", None)
    return out

"""Amazon Bedrock Knowledge Base retrieval source (``source_type="bedrock_kb"``).

Unlike the ingesting connectors, a Bedrock KB source holds NO local items: the
KB already maintains its own index and embeddings in the user's AWS account,
so Kiro Crew queries it live at search time and merges the hits with local
results. ``fetch``/``detect_changes`` therefore describe a source with nothing
to sync, and the real work happens in :meth:`BedrockKBConnector.search` and
:func:`search_remote_sources`.

Design constraints:

- **Sync on purpose, one process evaluates consent.** The search itself is
  a sync function under a wall-clock budget (:func:`search_remote_sources_bounded`).
  The sandboxed MCP ``local_knowledge_search`` tool does not call it
  directly: its consent view is inode-pinned, so its remote leg POSTs the
  gateway's internal ``/api/knowledge/remote-search`` route, which runs the
  bounded search on a worker thread and returns raw results. Surfaces that
  already run in the gateway process (the dashboard) call
  :func:`augment_with_remote`. The boto client itself is bounded by
  connect/read timeouts so a dead endpoint cannot hang either path.
- **No stored secrets.** A source carries only ``kb_ids``, ``region``,
  ``profile``; credentials resolve through the standard AWS
  chain (named profile) at call time.
- **A row is not a registration; the attestation is.** ``knowledge.db`` is
  agent-writable in-sandbox (the MCP server ingests into it in-process), so
  a ``bedrock_kb`` row proves nothing about who put it there. The owner-gated
  ``add_source`` insert records each row it creates in the sealed consent
  store (:func:`aws_consent.record_source_attestation`, pinned to the row's
  id, uri, retained triple AND the account the grant confirms, since a
  re-pointed profile keeps its name), the owner-only ``delete_source``
  revokes that record before the row goes, a failed second write undoes or
  refuses the first, and :func:`search_remote_sources` queries only rows
  that still match a record made under the CURRENT grant's account,
  auditing a row that does not as ``denied``. A clone, a row whose
  ``kb_ids`` were widened in the database, or a registration made under an
  account the current grant does not confirm is never paid for. Every
  retained field is bounded where it is retained (:func:`_target_fields`).
- **MANAGED-type KBs reject ``vectorSearchConfiguration``.** Retrieval tries
  the vector key first and retries with ``managedSearchConfiguration`` when
  the ValidationException names it. ``implicitFilterConfiguration`` is not
  supported on the managed path and is never sent there.
- **Citations come from the document's own metadata.** Crawler-built KBs
  attach the original document URL as the ``source_uri`` metadata attribute
  (the reserved ``x-amz-bedrock-kb-source-uri`` and the raw storage location
  are fallbacks), so results link to the real page instead of an S3 object.

boto3 ships in the optional ``[bedrock]`` extra; without it the source
reports the missing extra instead of failing core imports.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NoReturn

# Optional-import contract: boto3 ships in the [bedrock] extra. Sentinels keep
# this module importable without it; every entry point checks and reports the
# missing extra instead of raising ImportError mid-request. The module itself
# is only imported on first Bedrock use (never on the gateway boot path).
try:
    import boto3
    from botocore.config import Config as _BotoConfig
except ImportError:  # pragma: no cover - exercised via the sentinel check
    boto3 = None  # type: ignore[assignment]
    _BotoConfig = None  # type: ignore[assignment,misc]

from kiro_crew import aws_consent

# The canonical egress shim, not ``security.redact`` directly: on a host whose
# platform profile composes a companion credential policy, the shim applies
# that policy's extra shapes on top of the OSS baseline (and refuses to run
# when the companion could not be composed); the Default policy delegates to
# ``security.redact`` byte for byte, so a standalone install redacts exactly
# as before. Both texts this module redacts leave the trust boundary (the
# query goes to AWS, the conflict rendering to the dashboard).
from kiro_crew.platform import redact_via_context as redact

from .base import BaseConnector

logger = logging.getLogger(__name__)

# Async callers give the whole remote leg this budget, then fail open to
# local-only results. It must absorb one uncached STS consent probe (an AWS
# CLI subprocess, ~0.5-1.5s cold -- authorize() bypasses the probe cache by
# design) PLUS the per-KB Retrieve round trips, so it is sized above both,
# but far below chat-turn patience.
REMOTE_SEARCH_TIMEOUT_SECS = 5.0

# Per-request boto budgets. One retry only: the remote leg is supplementary,
# so a flaky endpoint should degrade to local results, not stall the search.
_CONNECT_TIMEOUT_SECS = 2
_READ_TIMEOUT_SECS = 5
_MAX_ATTEMPTS = 2

# Bedrock relevance floor: Retrieve always returns top-K nearest neighbors
# even when nothing is relevant, so without a floor an off-topic KB would
# permanently occupy interleaved result slots and evict locally relevant
# hits. Live probes against a real ~45K-doc KB scored relevant hits
# 0.43-0.60; 0.25 keeps marginal-but-related results and drops noise.
MIN_REMOTE_SCORE = 0.25

# Per-KB result clamp for fan-out retrieval; the effective count comes from
# the caller's limit.
MAX_TOP_K = 20

# Bounds on ``kb_ids``, the one externally supplied field a source RETAINS
# and re-parses on every search: entry count (one Retrieve per entry per
# search, so this is also the fan-out cap), raw entry length (a KB ARN is
# under 80 characters), and the exact id shape the Retrieve API accepts.
_MAX_KB_IDS = 10
_MAX_KB_ID_ENTRY_CHARS = 128
_KB_ID_RE = re.compile(r"[0-9A-Za-z]{10}")

# Probe text for validate-time access checks (1 result, cheapest possible
# call that still exercises IAM + KB existence + the retrieval config shape).
_PROBE_QUERY = "access check"

# Result-URL metadata keys, in precedence order: the crawler-written custom
# attribute first, then Bedrock's reserved source attribute.
_SOURCE_URI_KEYS = ("source_uri", "x-amz-bedrock-kb-source-uri")

_MISSING_BOTO3_MSG = (
    "boto3 is not installed. Install the optional extra: pip install 'boto3>=1.34,<2' "
    "(the kirocrew [bedrock] extra)"
)

# Error codes that mean the source config itself is bad (fail validation).
# ValidationException/ParamValidationError are fatal because _retrieve_one has
# already exhausted the managed-config fallback by the time one escapes it: what
# remains is a malformed KB id or request, and saving it would create a source
# whose every search fails. Anything else service-side (throttling, internal
# errors) proves the KB exists and the caller may retrieve, mirroring how
# onboarding probes treat non-authorization errors as accessible.
_FATAL_PROBE_CODES = {
    "AccessDeniedException",
    "ResourceNotFoundException",
    "ValidationException",
    "ParamValidationError",
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "ExpiredTokenException",
    "ProfileNotFound",
    "NoCredentialsError",
    "EndpointConnectionError",
    "ConnectTimeoutError",
}

# Bounded admission for remote searches: at most this many searches may be
# submitted-and-unfinished at once; callers beyond the bound fail open
# immediately instead of queueing behind slow Bedrock calls, so a stall can
# never grow an unbounded executor queue.
_MAX_PENDING_SEARCHES = 4
_search_slots = threading.BoundedSemaphore(_MAX_PENDING_SEARCHES)

# One shared worker pool for bounded remote searches. Two workers so a search
# stuck on a dead endpoint (still bounded by the boto timeouts) does not
# serialize the next search behind it. Module-level state carries no
# per-caller data, so it is safe in the long-lived MCP server process.
_search_pool: Any = None
_search_pool_lock = threading.Lock()


def _get_search_pool() -> Any:
    global _search_pool
    with _search_pool_lock:
        if _search_pool is None:
            _search_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bedrock-kb")
        return _search_pool


_CONSENT_REFUSED_MSG = (
    "Amazon Bedrock retrieval requires operator consent for this AWS "
    "profile/region (use the confirmation card in the add-source form to grant it)"
)


def _consent_allows(profile: str, region: str) -> bool:
    """Account-bound authorization for one probe or retrieval. Fail-closed.

    Every path that would issue a Bedrock request runs off the event loop
    (validate via ``to_thread``, retrieval on the search pool, the MCP server
    on its own thread), so driving the async consent check with
    ``asyncio.run`` is safe here. If this ever executes on a running loop,
    the call is REFUSED rather than blocking the loop -- consent stays
    fail-closed in every failure mode, matching :mod:`kiro_crew.aws_consent`.
    """
    try:
        return asyncio.run(
            aws_consent.refuse_and_log(
                aws_consent.SERVICE_BEDROCK_KB, profile=profile, region=region
            )
        )
    except RuntimeError:
        logger.warning("bedrock_kb consent check refused: called on a running event loop")
        return False
    except Exception:
        logger.exception("bedrock_kb consent check failed (refusing)")
        return False


def _get_client(region: str, profile: str) -> tuple[Any, Any]:
    """A fresh (client, verified_grant) pair for (region, profile).

    Deliberately NOT cached: profile credentials rotate (e.g. a re-minted
    static export), and a cached client pins the credentials it resolved
    first, failing every retrieval after rotation until a restart. Client
    construction costs milliseconds against a multi-second network budget.

    ONE credential set for verify and retrieve: the session's credentials
    are frozen, the frozen set is verified against the recorded grant's
    account via STS, and the SAME frozen set is handed to the Bedrock
    client. Without the freeze, the consent gate's CLI probe and boto3's own
    resolver each invoke the profile's credential process independently -- a
    process that returns account A to one call and account B the next would
    let A be verified while B is billed.
    """
    if boto3 is None or _BotoConfig is None:
        raise RuntimeError(_MISSING_BOTO3_MSG)
    session = boto3.session.Session(profile_name=profile or None)
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError(f"no AWS credentials resolve for profile {profile or '(default)'}")
    frozen = creds.get_frozen_credentials()
    static = {
        "aws_access_key_id": frozen.access_key,
        "aws_secret_access_key": frozen.secret_key,
        "aws_session_token": frozen.token,
    }
    boto_cfg = _BotoConfig(
        connect_timeout=_CONNECT_TIMEOUT_SECS,
        read_timeout=_READ_TIMEOUT_SECS,
        retries={"total_max_attempts": _MAX_ATTEMPTS},
    )
    sts_account = str(
        session.client("sts", region_name=region, config=boto_cfg, **static)
        .get_caller_identity()
        .get("Account", "")
    )
    # Grant read AFTER the STS response, matched on the full target: a grant
    # revoked or replaced while the probe was in flight must not authorize
    # this retrieval on stale evidence, and an account-only match would
    # accept a grant recorded for a different (profile, region).
    grant = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
    if grant is None or not grant.account or not sts_account:
        mismatch: str | None = "no confirmed grant or no STS account"
    elif grant.account != sts_account:
        mismatch = "credentials resolve to an account other than the confirmed one"
    elif grant.profile != profile or grant.region != region:
        mismatch = "grant was confirmed for a different profile or region"
    else:
        mismatch = None
    if mismatch is not None:
        # Refused HERE, after the gate said yes and before any paid request:
        # like ``_refuse_withdrawn`` it writes its own ``denied`` line, so an
        # incident review sees every retrieval that did not happen, not only
        # the ones ``aws_consent.refuse_and_log`` turned away up front. The
        # account numbers stay out of the line: the grant record already
        # holds the confirmed one, and the question the line answers is
        # WHICH check refused, for which target.
        aws_consent.audit_decision(
            aws_consent.SERVICE_BEDROCK_KB,
            outcome="denied",
            detail=(
                f"retrieval refused before Retrieve: {mismatch} "
                f"(profile={profile or '(default)'}, region={region})"
            ),
        )
        raise RuntimeError(
            "the AWS account these credentials resolve to is not the one the "
            "operator confirmed; retrieval refused"
        )
    client = session.client(
        "bedrock-agent-runtime",
        region_name=region,
        config=boto_cfg,
        **static,
    )
    # The grant these frozen credentials were verified against travels with
    # the client: every Retrieve rechecks the store still holds exactly it.
    return client, grant


def _error_code(e: Exception) -> str:
    """Normalize botocore's two error shapes into one comparable code."""
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return e.__class__.__name__


def _grant_still_current(expected: Any) -> bool:
    """The withdrawal recheck run immediately before every paid Retrieve.

    A LOCAL read only (no STS): the account was frozen-verified when the
    client was built; what can change mid-fan-out is REVOCATION or
    REPLACEMENT, both of which live in the local consent store. The stored
    grant must still be byte-for-byte the one the frozen credentials were
    verified against -- a same-target re-confirmation for a different
    account (or any re-confirmation at all: grant_id is fresh) means the
    frozen credentials were never verified against the CURRENT grant, so
    the retrieve is refused and the next search re-freezes and re-verifies.
    """
    grant = aws_consent.read_grant(aws_consent.SERVICE_BEDROCK_KB)
    return grant is not None and grant == expected


def _registration_of(source: dict) -> tuple[str, str, dict] | None:
    """The (id, uri, properties) a stored row is attested under.

    ``None`` for a bare config with no ``id``: the validate-time probe runs
    before any row exists, so there is nothing registered to hold it to.
    Every row the paid enumeration hands to :meth:`BedrockKBConnector.search`
    carries its id.
    """
    sid = source.get("id")
    if sid is None or str(sid) == "":
        return None
    props = source.get("properties")
    if isinstance(props, str):
        try:
            props = json.loads(props or "{}")
        except (TypeError, ValueError):
            props = {}
    if not isinstance(props, dict):
        props = {}
    return str(sid), str(source.get("uri") or ""), props


def _registration_still_attested(registration: tuple[str, str, dict] | None) -> bool:
    """The row this search runs for is still attested under the current grant.

    A LOCAL read only, like :func:`_grant_still_current`. ``None`` (a
    validate-time probe, no row yet) has nothing to hold to and passes.
    """
    if registration is None:
        return True
    sid, uri, props = registration
    return aws_consent.source_matches_attestation(aws_consent.attested_sources(), sid, uri, props)


def _refusal_reason(expected_grant: Any, registration: tuple[str, str, dict] | None) -> str | None:
    """Why the next paid Retrieve must not go out, or ``None`` when it may.

    Run immediately before every paid request. Two local reads of the
    consent store, no STS: the grant must still be the one the frozen
    credentials were verified against (:func:`_grant_still_current`), and
    the row must still be registered under it
    (:func:`_registration_still_attested`). The enumeration checked the
    registration once when it started, but a delete landing during the
    search revokes the attestation and leaves the grant in place, so a
    recheck that read only the grant would let the rest of that search
    keep billing for a source the owner had just removed.
    """
    if not _grant_still_current(expected_grant):
        return "grant withdrawn or replaced"
    if not _registration_still_attested(registration):
        return "source registration withdrawn"
    return None


def _refuse_withdrawn(kb_id: str, reason: str) -> NoReturn:
    """Refuse one paid Retrieve whose authorization is gone, and audit that.

    The withdrawal itself is audited where it happens (``revoked``) and the
    next search is refused through :func:`aws_consent.refuse_and_log`
    (``denied``), but a retrieve already in flight is refused HERE, locally,
    so it has to write its own ``denied`` line: an incident review reading the
    Security Event Log must see every request that did not happen, not only
    the ones the gate turned away up front. ``reason`` is the
    :func:`_refusal_reason` text, so the line says which check refused.
    """
    aws_consent.audit_decision(
        aws_consent.SERVICE_BEDROCK_KB,
        outcome="denied",
        detail=f"retrieve refused mid-search: {reason} (kb={kb_id})",
    )
    raise RuntimeError("consent was withdrawn or replaced; retrieval refused")


def _retrieve_one(
    client: Any,
    kb_id: str,
    query: str,
    limit: int,
    *,
    expected_grant: Any,
    deadline: float | None = None,
    registration: tuple[str, str, dict] | None = None,
) -> list[dict]:
    """Retrieve from one KB, falling back to the managed retrieval config.

    MANAGED-type KBs (the kind KB-building crawlers create by default) reject
    ``vectorSearchConfiguration`` with a ValidationException that names
    ``managedSearchConfiguration``; only that specific rejection triggers the
    fallback, so authorization errors still propagate to the caller.
    ``deadline`` is the search budget (``time.monotonic()``): a rejection slow
    enough to spend it leaves the fallback unissued, since its result could
    only be abandoned by a waiter that has already failed open.
    ``registration`` is the stored row this search runs for
    (:func:`_registration_of`); each paid request first confirms the row is
    still attested, not only that the grant still stands.
    """
    refused = _refusal_reason(expected_grant, registration)
    if refused is not None:
        _refuse_withdrawn(kb_id, refused)
    try:
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": limit},
            },
        )
    except Exception as e:
        if "managedSearchConfiguration" not in str(e):
            raise
        # The fallback is its own paid request: it gets the same budget check
        # the per-KB loop applies, and the same re-assertion of the grant and
        # the registration.
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning("bedrock_kb %s: managed fallback skipped (budget spent)", kb_id)
            return []
        refused = _refusal_reason(expected_grant, registration)
        if refused is not None:
            _refuse_withdrawn(kb_id, refused)
        response = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "managedSearchConfiguration": {"numberOfResults": limit},
            },
        )
    results = response.get("retrievalResults") or []
    return [r for r in results if isinstance(r, dict)]


def _result_url(raw: dict) -> str:
    """The best citation URL a retrieval result carries.

    Precedence: the crawler-written ``source_uri`` metadata attribute (the
    document's real Quip/wiki/code URL), Bedrock's reserved source attribute,
    then whatever the storage location exposes (an s3:// or web URL).
    """
    metadata = raw.get("metadata") or {}
    for key in _SOURCE_URI_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    location = raw.get("location") or {}
    for loc in location.values():
        if isinstance(loc, dict):
            for key in ("uri", "url"):
                value = loc.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def _parse_kb_ids(value: Any, *, strict: bool = False) -> list[str]:
    """kb_ids as a bounded list of bare KB ids, from list or comma-separated input.

    A full KB ARN (``arn:aws:bedrock:<region>:<acct>:knowledge-base/<ID>``)
    is normalized to its trailing id so one knowledge base has one spelling
    here: the dedupe below, the ``bedrock:<src>:<kb>:<n>`` result ids and
    the audit rows all key on it (Retrieve itself accepts either form).

    Bounds, because ``kb_ids`` is RETAINED (``sources.properties``) and
    re-parsed on every search: at most ``_MAX_KB_IDS`` entries, each raw
    entry at most ``_MAX_KB_ID_ENTRY_CHARS`` long, and each normalized id
    exactly the ``_KB_ID_RE`` shape the Retrieve API accepts. ``strict``
    (validation at add time) raises ``ValueError`` naming the first
    violation, so nothing out of bounds is ever probed or stored; the
    default (search over a stored row) drops what fails and truncates.
    """
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        return []
    if len(parts) > _MAX_KB_IDS:
        if strict:
            raise ValueError(f"kb_ids lists more than {_MAX_KB_IDS} knowledge bases")
        parts = parts[:_MAX_KB_IDS]
    out: list[str] = []
    for raw in parts:
        p = raw.strip()
        if not p:
            continue
        if len(p) > _MAX_KB_ID_ENTRY_CHARS:
            if strict:
                raise ValueError(
                    "kb_ids has an entry longer than " f"{_MAX_KB_ID_ENTRY_CHARS} characters"
                )
            continue
        if p.startswith("arn:") and "/" in p:
            p = p.rsplit("/", 1)[-1].strip()
        if not _KB_ID_RE.fullmatch(p):
            if strict:
                raise ValueError(
                    "kb_ids has an entry that is not a knowledge base id "
                    "(10 letters or digits) or its ARN"
                )
            continue
        # First occurrence wins: a repeated id would double-bill the Retrieve
        # and emit colliding result ids.
        if p not in out:
            out.append(p)
    return out


class BedrockKBConnector(BaseConnector):
    """Live-retrieval connector for Amazon Bedrock Knowledge Bases."""

    def source_type(self) -> str:
        return "bedrock_kb"

    def validate_config(self, config: dict) -> tuple[bool, str]:
        # Strict: the bounds are enforced HERE, before the probe loop below
        # and before the caller's insert retains the field.
        try:
            kb_ids = _parse_kb_ids(config.get("kb_ids"), strict=True)
        except ValueError as e:
            return False, str(e)
        if not kb_ids:
            return False, "kb_ids is required (one or more KB ids or ARNs)"
        try:
            region, profile = _target_fields(config)
        except ValueError as e:
            return False, str(e)
        if not region:
            return False, "region is required (the AWS region the KB lives in)"

        # Account-bound authorization precedes ANY Bedrock request, the live
        # probe included: a repointed profile must not receive so much as an
        # access check without a confirmed grant for (service, profile, region).
        if not _consent_allows(profile, region):
            return False, _CONSENT_REFUSED_MSG

        try:
            client, verified_grant = _get_client(region, profile)
        except RuntimeError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Could not create a Bedrock client: {_error_code(e)}"

        # Live probe: cheapest call that exercises credentials, KB existence
        # and the retrieval-config shape, so a bad source is refused at add
        # time instead of failing silently at search time.
        for kb_id in kb_ids:
            try:
                _retrieve_one(client, kb_id, _PROBE_QUERY, 1, expected_grant=verified_grant)
            except RuntimeError as e:
                # The connector's own consent refusals (withdrawal mid-probe,
                # freeze-verify mismatch) raise RuntimeError. They are LOCAL
                # authorization failures, not transient AWS weather — saving
                # the source would record it without current authorization.
                return False, str(e)
            except Exception as e:
                code = _error_code(e)
                if code in _FATAL_PROBE_CODES:
                    return False, f"Cannot access knowledge base {kb_id}: {code}"
                logger.info(
                    "bedrock_kb probe for %s returned non-authorization error %s; "
                    "treating the KB as accessible",
                    kb_id,
                    code,
                )
        return True, ""

    async def detect_changes(self, source: dict) -> bool:
        # Nothing to sync: the KB is queried live and never ingested.
        return False

    async def fetch(self, source: dict) -> tuple[str, dict]:
        # Unreachable through SyncScheduler (detect_changes is always False);
        # defined because BaseConnector requires it.
        return "", {"remote": True}

    def search(
        self, source: dict, query: str, limit: int, deadline: float | None = None
    ) -> list[dict]:
        """Fan out one Retrieve per configured KB and merge by relevance.

        Returns results in the HybridRetriever result shape so downstream
        formatting (citations, dashboards) needs no special-casing. Raises on
        total failure -- callers own the fail-open decision.
        """
        props = source.get("properties")
        if isinstance(props, str):
            props = json.loads(props or "{}")
        props = {**(props or {}), **source}
        kb_ids = _parse_kb_ids(props.get("kb_ids"))
        region = str(props.get("region") or "").strip()
        profile = str(props.get("profile") or "").strip()
        if not kb_ids or not region:
            return []
        # Consent precedes retrieval: a source whose grant was revoked (or
        # whose profile now resolves to an unconfirmed account) contributes
        # nothing, logged by the consent layer, instead of billing that account.
        if not _consent_allows(profile, region):
            return []
        # The one leg of knowledge search that leaves the host: the query is
        # caller-supplied free text, so credential-shaped content is redacted
        # before it reaches the Retrieve API. A no-op for ordinary queries
        # (local search legs keep the raw text; only this egress redacts).
        # The composite runs the URL pass before the credential pass: a
        # credential inside a URL's query string blanked ahead of the URL
        # pass leaves a placeholder the URL matcher does not recognise, and
        # the URL's host and the rest of its query would go out with the
        # request.
        query = redact(query)
        per_kb = min(max(1, limit), MAX_TOP_K)
        client, verified_grant = _get_client(region, profile)
        registration = _registration_of(source)

        merged: list[dict] = []
        errors: list[str] = []
        for kb_id in kb_ids:
            # The pool future's result(timeout=...) only stops the WAITER --
            # this worker would keep issuing paid Retrieve calls for the
            # remaining KBs after the caller has already failed open. Stop
            # before each request once the budget is spent.
            if deadline is not None and time.monotonic() >= deadline:
                # A spent budget is fail-open silence, not a KB failure: it
                # must never trip the errors-and-nothing-merged raise below
                # (validate would read a slow round as a broken source).
                logger.warning("bedrock_kb %s: skipped (budget spent)", kb_id)
                continue
            try:
                raw_results = _retrieve_one(
                    client,
                    kb_id,
                    query,
                    per_kb,
                    expected_grant=verified_grant,
                    deadline=deadline,
                    registration=registration,
                )
            except Exception as e:
                # One unreachable KB must not sink the others' results.
                errors.append(f"{kb_id}: {_error_code(e)}")
                continue
            for i, raw in enumerate(raw_results):
                content = str((raw.get("content") or {}).get("text") or "")
                if not content:
                    continue
                score = float(raw.get("score") or 0.0)
                # Retrieve returns nearest neighbors unconditionally; below
                # the floor a hit is noise, not knowledge (see MIN_REMOTE_SCORE).
                if score < MIN_REMOTE_SCORE:
                    continue
                metadata = raw.get("metadata") or {}
                url = _result_url(raw)
                title = str(metadata.get("title") or "") or (
                    url.rstrip("/").rsplit("/", 1)[-1] if url else "Knowledge base result"
                )
                merged.append(
                    {
                        # Scoped by SOURCE as well as KB: two registered
                        # sources may legitimately list the same KB, and a
                        # bare kb_id+index would collide across them (the
                        # dashboard keys hit selection by this id).
                        "id": f"bedrock:{source.get('id') or 'src'}:{kb_id}:{i}",
                        "title": title,
                        "summary": content[:200],
                        "content": content,
                        "score": score,
                        "match_type": "remote",
                        "source_type": "bedrock_kb",
                        "source_name": source.get("name") or "Bedrock KB",
                        "source_uri": url,
                        "kb_id": kb_id,
                    }
                )
        if errors and not merged:
            raise RuntimeError("; ".join(errors))
        if errors:
            logger.warning("bedrock_kb partial retrieval failure: %s", "; ".join(errors))
        merged.sort(key=lambda r: r["score"], reverse=True)
        return merged[: max(1, limit)]


#: Serializes every bedrock-kb TARGET-CHANGING critical section: the consent
#: POST's (mismatch check + grant record) and add_source's (mismatch recheck +
#: insert). Both sections are sqlite/json writes (fast); the slow network
#: probes/validation run OUTSIDE it. Without this, a concurrent add could
#: land between the consent POST's check and its write -- the freshly
#: registered source's grant would be silently replaced.
target_change_lock = threading.Lock()


class TargetLookupError(RuntimeError):
    """The registered-source lookup FAILED (vs. found nothing).

    Callers must fail CLOSED: a broken store must not read as "no conflict"
    -- that would let a different grant replace a registered source's
    authorization exactly when the system is least able to notice.
    """


def _target_fields(config: dict) -> tuple[str, str]:
    """The stripped ``region`` and ``profile`` a row may retain, shape-checked.

    Both are bounded HERE, by the same two patterns a grant is confirmed under
    (:func:`aws_consent.target_is_well_formed`: profile at most 128 characters,
    region at most 64), not only by the grant-equality gates downstream: a
    retained field carries its own bound, so the row's size never depends on
    which gate happened to run first. Raises ``ValueError`` with the message
    the caller returns as its refusal.
    """
    region = str(config.get("region") or "").strip()
    profile = str(config.get("profile") or "").strip()
    if not aws_consent.target_is_well_formed(profile, region):
        raise ValueError(
            "profile or region has an unexpected shape (profile: up to 128 "
            "characters of the AWS profile alphabet; region: up to 64 of a-z, 0-9, -)"
        )
    return region, profile


def retained_properties(config: dict) -> dict[str, str]:
    """The ``properties`` a ``bedrock_kb`` row RETAINS, in validated form.

    Exactly the three keys :meth:`BedrockKBConnector.search` reads back, each
    holding the value the add-time gates actually checked rather than the
    caller's raw spelling of it: ``kb_ids`` is the canonical comma-joined
    list :func:`_parse_kb_ids` produced under strict bounds (at most
    ``_MAX_KB_IDS`` bare ids, ARNs reduced to their id), and ``region`` /
    ``profile`` are the stripped, shape-bounded strings of
    :func:`_target_fields`, the ones the grant-equality check matched.
    Every gate strips or parses before it judges, so a value padded with
    whitespace (or an ARN spelling) passes them all; retaining the raw copy
    would store what nothing validated, bounded only by the request-body
    cap. Retaining the validated copy makes the row's size follow the
    gates' own bounds and its spelling the one every later compare and
    result id uses. Raises ``ValueError`` on a field outside its bound.
    """
    region, profile = _target_fields(config)
    return {
        "kb_ids": ",".join(_parse_kb_ids(config.get("kb_ids"), strict=True)),
        "region": region,
        "profile": profile,
    }


def canonical_source_uri(retained: dict[str, str]) -> str:
    """The ``uri`` a ``bedrock_kb`` row is keyed by, from its retained triple.

    ``sources.uri`` is UNIQUE and is what dedupes an add, so it has to be the
    server's derivation from validated config, never the caller's spelling:
    a caller who could choose the handle could mint a new row per request
    for the same knowledge base, with a name up to the body cap on each.
    Keyed on the region and the SORTED full id set (``+``-joined; ids are
    ``[0-9A-Za-z]{10}`` so the separator is unambiguous), the handle is one
    per distinct set of knowledge bases: the same ids in any order dedupe to
    one row, so a search pays one Retrieve per KB rather than one per
    spelling, while a different set that shares a first id (``A`` vs
    ``A,B``) is a different source rather than a false ``409``. Bounded by
    the parts it is built from (at most ``_MAX_KB_IDS`` ids).
    """
    ids = "+".join(sorted(retained["kb_ids"].split(",")))
    return f"bedrock-kb://{retained['region']}/{ids}"


def registered_target_mismatch(store: Any, profile: str, region: str) -> str | None:
    """A registered bedrock_kb source whose (profile, region) differs, if any.

    The one-account-per-service v1 rule is enforced at BOTH gates with this
    single compare: add_source (a second source with a different target) and
    the consent POST (confirming a different target would overwrite the
    grant a registered source depends on). Returns a human-readable
    rendering of the held target for the refusal message, or None when no
    registered source disagrees. Raises :class:`TargetLookupError` when the
    lookup itself fails -- callers refuse rather than fail open. Sync
    (sqlite) -- run via asyncio.to_thread.

    The rendering is redacted here, once, because it is the only place the
    ``sources`` row's name is turned into operator-facing text: both callers
    put it in a refusal payload the dashboard shows verbatim, and the rows
    are agent-writable in-sandbox (``knowledge.db`` sits outside the
    protected tree), so a planted name can carry a credential or an
    exfiltration URL. The handler's name bound does not apply to a row that
    never went through the handler. The rendering goes through the platform
    shim (:func:`kiro_crew.platform.redact_via_context`), so a loaded
    companion's extra credential shapes apply too; its baseline,
    :func:`kiro_crew.security.redact`, runs
    the URL pass before the credential pass: a planted URL carries its
    payload in the query string, and a credential blanked ahead of the URL
    pass leaves a placeholder the URL matcher does not recognise, so the
    host and the rest of the query survive.
    """
    try:
        rows = store.db.execute(
            "SELECT name, properties FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchall()
    except Exception as e:
        raise TargetLookupError(f"bedrock_kb source lookup failed: {e}") from e
    for row in rows:
        try:
            props = json.loads(row["properties"] or "{}")
        except Exception:
            continue
        held = (
            str(props.get("profile") or "").strip(),
            str(props.get("region") or "").strip(),
        )
        if held != (profile.strip(), region.strip()):
            return redact(f"{row['name']} ({held[0] or 'default profile'}, {held[1]})")
    return None


def _row_is_attested(attested: dict[str, dict[str, Any]], source: dict) -> bool:
    """Whether the gateway registered exactly this ``bedrock_kb`` row.

    ``attested`` is one :func:`aws_consent.attested_sources` read for the whole
    enumeration; the per-Retrieve recheck (:func:`_refusal_reason`) reads it
    again for every paid request, so a delete landing after this check stops
    the search at its next request. A row that does not match writes its own
    ``denied`` line, the same shape as the other refusals in this module: an
    incident review must see every paid query that did not happen. The line
    names the row id, not its KB ids or the account.
    """
    registration = _registration_of(source)
    if registration is not None and aws_consent.source_matches_attestation(attested, *registration):
        return True
    sid = str(source.get("id") or "")
    aws_consent.audit_decision(
        aws_consent.SERVICE_BEDROCK_KB,
        outcome="denied",
        detail=f"unattested bedrock_kb source skipped before Retrieve (source={sid or '?'})",
    )
    return False


def search_remote_sources(
    store: Any,
    query: str,
    limit: int,
    source_id: str | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Search registered bedrock_kb sources and merge by score.

    ``store`` is a KnowledgeStore (only ``.db`` is used). ``source_id``
    narrows the search to one source (the scoped-search case); by default all
    bedrock_kb sources are queried. Per-source failures are logged and
    skipped so one broken KB cannot break knowledge search.

    Only rows the gateway ATTESTED are searched. ``knowledge.db`` is
    agent-writable in-sandbox (the MCP server ingests into it in-process),
    so a ``bedrock_kb`` row on its own proves nothing about who registered
    it: a prompt-injected agent could clone one, or point a clone at any KB
    the granted profile reaches, and this enumeration would pay to query
    it. ``add_source`` (owner-gated, consent-checked) records each row it
    inserts in the sealed consent store
    (:func:`aws_consent.record_source_attestation`), and a row whose id, uri
    and retained triple do not match that record is skipped here with a
    ``denied`` audit line, before any consent or credential work runs for it.
    """
    sql = "SELECT id, name, uri, properties FROM sources WHERE source_type = 'bedrock_kb'"
    params: tuple[Any, ...] = ()
    if source_id is not None:
        sql += " AND id = ?"
        params = (source_id,)
    try:
        rows = store.db.execute(sql, params).fetchall()
    except Exception:
        logger.exception("bedrock_kb: could not enumerate remote sources")
        return []
    if not rows:
        return []
    attested = aws_consent.attested_sources()
    connector = BedrockKBConnector()
    merged: list[dict] = []
    for row in rows:
        source = dict(row)
        if not _row_is_attested(attested, source):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning("bedrock_kb: budget spent before source %s", source.get("id"))
            break
        try:
            merged.extend(connector.search(source, query, limit, deadline=deadline))
        except Exception as e:
            logger.warning(
                "bedrock_kb source %s search failed (fail-open): %s",
                source.get("id"),
                _error_code(e),
            )
    merged.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return merged[: max(1, limit)]


def search_remote_sources_bounded(
    store: Any,
    query: str,
    limit: int,
    source_id: str | None = None,
    timeout: float = REMOTE_SEARCH_TIMEOUT_SECS,
) -> list[dict]:
    """:func:`search_remote_sources` under a hard wall-clock budget.

    The entry point both callers use: returns ``[]`` on timeout or any
    failure (fail-open -- knowledge search proceeds with local results).
    Admission is bounded by ``_MAX_PENDING_SEARCHES``: when every slot is
    held by an unfinished search, new callers fail open immediately instead
    of queueing, so slow Bedrock calls can never grow an unbounded executor
    queue. A timed-out future is cancelled; one that already started is
    bounded by the boto connect/read timeouts and releases its slot in its
    ``finally``.
    """
    has_remote = False
    try:
        row = store.db.execute(
            "SELECT 1 FROM sources WHERE source_type = 'bedrock_kb' LIMIT 1"
        ).fetchone()
        has_remote = row is not None
    except Exception:
        return []
    # The common case: no remote sources registered. Zero extra threads,
    # zero extra latency for every install that never adds one.
    if not has_remote:
        return []
    if not _search_slots.acquire(blocking=False):
        logger.warning("bedrock_kb remote search skipped: all slots busy (fail-open)")
        return []

    # The slot is released exactly once: by the worker's finally when it runs,
    # or below when cancel() confirms the worker never started.
    deadline = time.monotonic() + timeout

    def _run() -> list[dict]:
        try:
            return search_remote_sources(store, query, limit, source_id, deadline=deadline)
        finally:
            _search_slots.release()

    try:
        future = _get_search_pool().submit(_run)
    except Exception:
        _search_slots.release()
        logger.exception("bedrock_kb remote search could not be submitted (fail-open)")
        return []
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        if future.cancel():
            # Cancelled while still queued: _run never executed, so its
            # finally never released the slot.
            _search_slots.release()
        logger.warning("bedrock_kb remote search failed open: %s", _error_code(e))
        return []


def augment_with_remote(
    store: Any,
    query: str,
    limit: int,
    local_results: list[dict],
) -> list[dict]:
    """Bounded remote search + rank merge, as one call.

    The entry for surfaces that run IN the gateway process (currently the
    dashboard's ``search_for_context``). The sandboxed MCP server cannot use
    it -- its consent view is inode-pinned -- so it reaches the same bounded
    search through the gateway's internal remote-search route and merges
    with :func:`merge_by_rank`. Fail-open: with no remote hits the local
    results come back unchanged.
    """
    remote = search_remote_sources_bounded(store, query, limit)
    if not remote:
        return local_results
    return merge_by_rank(local_results, remote, limit)


def merge_by_rank(local: list[dict], remote: list[dict], limit: int) -> list[dict]:
    """Interleave local and remote results by rank, local first.

    Local RRF scores (~0.01-0.06) and Bedrock relevance scores (0-1) are not
    comparable, so ranking is positional: it preserves each leg's own order
    and guarantees remote hits are represented instead of always sorting
    below (or above) every local hit on raw score.
    """
    out: list[dict] = []
    for i in range(max(len(local), len(remote))):
        if i < len(local):
            out.append(local[i])
        if i < len(remote):
            out.append(remote[i])
        if len(out) >= limit:
            break
    return out[:limit]

"""Gerrit Code Review changes, read over the instance's REST API.

Gerrit has no CLI the gateway could lean on, so it calls the instance itself, as
it does for Jira and Bitbucket Server: HTTPS to the exact allowlisted host only,
no redirects followed, every response size-bounded, and the HTTP credential sent
only in the request header. The provider implements the registered-provider
protocol and is served through that seam (see ``plugins``), so the shared cache,
redaction and payload cap apply to it, and every write it does not implement is
refused there. It is read-only: overview, the patchset's commit, files with
their diffs, review threads (human and robot) and the label votes that Gerrit
uses where other forges have CI checks.

Gerrit specifics worth naming once, because every mapping below rests on them:

* **A change is one commit, revised in patchsets.** The panel's ``commits`` is
  the one commit of the patchset shown; ``headSha`` is that patchset's SHA and
  ``headBranch`` its ``refs/changes/..`` ref. A link that pins a patchset shows
  that patchset's commit, files and diffs. Label votes, and so the checks and
  review verdicts, belong to the CHANGE and are read from its current patchset
  whichever one is pinned -- exactly what Gerrit's own change page shows beside
  an older patchset's diff.
* **CI lives in labels.** Jenkins and its peers vote ``Verified``-style labels
  (``Verified``, ``K2-Core-Verified``, ``AutoSDE-Verified`` ...): a positive
  vote is a pass, a negative vote a failure, and no vote is no row. The sign
  decides, not the extreme: a -1 on a -2..+2 ``Verified`` label (Zuul's check
  pipeline on OpenDev) is a failure even though Gerrit files it as ``disliked``
  rather than ``rejected``. A build that has
  started on the current patchset but not yet voted is visible only through the
  trigger plugin's ``Build Started <job url>`` change messages. A job is still
  running until a later message names its url (the server's verdict message
  lists every job it ran), and the still-running jobs become one pending row --
  which is what lets the chip say "running", or carry ``ciPending`` beside a
  "failed" verdict while a re-run is still in flight.
* **Review verdicts are ``Code-Review`` votes**, read from the label's per-account
  list: ``+2`` approves, a negative vote requests changes, ``+1`` is a comment.
* **Threads carry their own resolved flag** on every comment; a thread's state is
  the flag of its LATEST comment, as the Gerrit UI computes it, never the root's.
* **Every REST body starts with the ``)]}'`` XSSI guard**, which is stripped
  before JSON parsing; the patch endpoint answers base64 of ``git format-patch``.
* **An anonymous read of a change the anonymous user may not see is a 404**, not
  a 401, so without a credential a 404 is treated as "store a credential".
* **The patch endpoint answers the ``Accept`` header**: asked for JSON it returns
  the patch as a JSON string, asked for text it returns the documented base64.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypeVar, cast
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.source_providers import (
    LOGGER_NAME,
    contract,
    hosts,
    plugins,
    projection,
    runner,
)
from kiro_crew.dashboard.source_providers.contract import (
    SourceChangePayload,
    SourceProviderError,
    SourceProviderNotConfigured,
    SourceRef,
)
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(LOGGER_NAME)

_ResultT = TypeVar("_ResultT")

_FETCH_TIMEOUT_SECS = 15
#: The credential used when exactly one Gerrit host is listed. Its value is
#: ``<username>:<HTTP password>``, the pair Gerrit's "HTTP Credentials" settings
#: page generates for REST and git-over-HTTP access.
GLOBAL_CREDENTIAL_NAME = "GERRIT_HTTP_CREDENTIAL"
_MAX_COMMENTS = 300
_MAX_THREAD_DEPTH = 64
#: The XSSI guard every Gerrit JSON response opens with.
_XSSI_PREFIX = b")]}'"
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_HTTPS_URL_RE = re.compile(r"https://[^\s<>\"']+")
_STATE_MAP = {"NEW": "OPEN", "MERGED": "MERGED", "ABANDONED": "CLOSED"}
_FILE_STATUS = {"A": "added", "D": "removed", "R": "renamed", "C": "copied", "W": "rewritten"}
#: The Gerrit Trigger (Jenkins) plugin posts ``Build Started <job url>`` per job
#: and, per Jenkins server, one verdict message listing every job url it ran.
_BUILD_STARTED_RE = re.compile(r"\bBuild Started\s+(https://\S+)")
#: The detail options one read needs. ``MERGEABLE`` is not among them: Gerrit
#: 3.x refuses it as a ``-o`` value, so mergeability is its own endpoint.
_DETAIL_OPTIONS = (
    "LABELS",
    "DETAILED_LABELS",
    "CURRENT_REVISION",
    "CURRENT_COMMIT",
    "DETAILED_ACCOUNTS",
    "MESSAGES",
    "SUBMITTABLE",
)


def host_credential_name(host: str) -> str:
    """The per-host vault name for *host*: ``GERRIT_HTTP_CREDENTIAL_<HEX>``."""
    normalized = config_loader.normalize_jira_host(host)
    return f"{GLOBAL_CREDENTIAL_NAME}_{normalized.encode().hex().upper()}"


def _vault_secret(name: str) -> str:
    """Return the vault secret *name*, or ``""`` when absent or unreadable."""
    try:
        secret = SecretVault(config_loader.config_dir()).get(name)
    except Exception:
        logger.debug("Gerrit credential unavailable", exc_info=True)
        return ""
    return secret.reveal() if secret is not None else ""


def _require_listed(host: str) -> None:
    """Refuse *host* unless it is in the current Gerrit allowlist."""
    if host not in hosts._allowed_gerrit_hosts():
        raise SourceProviderError(f"{host} is not listed in dashboard.gerrit_hosts.")


def _resolve_credential(host: str) -> tuple[str, str]:
    """``(credential, vault name)`` for *host*: its per-host entry, else the single-host one.

    BLOCKING (a vault read), so callers run it in a worker thread. The global
    name serves only when *host* is the one listed host, so adding a second
    instance never hands the first instance's credential to it. The name comes
    back with the value so that guidance to replace a credential names the
    entry that was actually used, not the one the host count would suggest:
    a lone host may still be served by its per-host entry.
    """
    name = host_credential_name(host)
    credential = _vault_secret(name)
    if credential:
        return credential, name
    if hosts._allowed_gerrit_hosts() == frozenset({host}):
        return _vault_secret(GLOBAL_CREDENTIAL_NAME), GLOBAL_CREDENTIAL_NAME
    return "", name


def _credential_secret_name(host: str) -> str:
    """The vault name to tell the user to CREATE: the lone-host name, else per-host."""
    if hosts._allowed_gerrit_hosts() == frozenset({host}):
        return GLOBAL_CREDENTIAL_NAME
    return host_credential_name(host)


def _setup_guidance(host: str) -> str:
    name = _credential_secret_name(host)
    if name == GLOBAL_CREDENTIAL_NAME:
        # A lone listed host: one secret to store, so name only that one.
        note = ""
    else:
        # The shared name serves only a lone listed host, so name it as the
        # likely cause when a second host was added after it was stored.
        note = f"; {GLOBAL_CREDENTIAL_NAME} stops applying once a second host is listed"
    return (
        f"Gerrit at {host} needs an HTTP credential to read changes. Generate an HTTP "
        f"password under the account's HTTP Credentials settings and store "
        f"<username>:<HTTP password> in Settings > Secrets as {name}{note}."
    )


def _replace_credential_guidance(secret_name: str) -> str:
    """Guidance naming the vault entry the failed credential was READ from."""
    return (
        f"Replace {secret_name} in Settings > Secrets with a "
        "<username>:<HTTP password> pair that can read this change."
    )


def _basic_auth(host: str, credential: str, secret_name: str) -> str:
    """The ``Authorization`` header value for a ``user:password`` credential."""
    user, sep, password = credential.partition(":")
    if not sep:
        reason = "has no ':' separating the username from the HTTP password"
    elif not user:
        reason = "has nothing before the ':' where the username belongs"
    elif not password:
        reason = "has nothing after the ':' where the HTTP password belongs"
    else:
        reason = ""
    if reason:
        raise SourceProviderError(
            f"The stored Gerrit credential for {host} {reason}; it must be "
            f"<username>:<HTTP password>. {_replace_credential_guidance(secret_name)}"
        )
    return "Basic " + base64.b64encode(credential.encode("utf-8")).decode("ascii")


def _iso(value: Any) -> str:
    """Render a Gerrit ``YYYY-MM-DD HH:MM:SS.nnnnnnnnn`` UTC stamp as ISO-8601, ``""`` if absent."""
    if not isinstance(value, str) or len(value) < 19:
        return ""
    try:
        moment = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return moment.isoformat().replace("+00:00", "Z")


def _person(value: Any) -> str:
    # Gerrit's own page shows `display_name` when an account set one and
    # falls back to the full `name`, then the username.
    account = projection._as_dict(value)
    return str(account.get("display_name") or account.get("name") or account.get("username") or "")


def _strip_xssi(body: bytes) -> bytes:
    return body[len(_XSSI_PREFIX) :] if body.startswith(_XSSI_PREFIX) else body


def _patchset_pin(ref: SourceRef) -> str:
    """The patchset a canonical URL pins (``"2"``), or ``""`` for the current one."""
    tail = ref.url.rsplit("/+/", 1)[-1]
    _number, _sep, patchset = tail.partition("/")
    return patchset if patchset.isdigit() else ""


class _NoSuchResource(SourceProviderError):
    """An authenticated 404: the change, patchset or endpoint is not there.

    Still a provider error for every caller that does not care, so the change
    read fails with the same message as before; the one leg whose absence is
    expected on some servers (robot comments) catches it and returns empty.
    """


class _Client:
    """One fetch's view of one instance: the base URL and an open session."""

    def __init__(
        self, ref: SourceRef, session: ClientSession, *, has_credential: bool, secret_name: str
    ) -> None:
        self.ref = ref
        self.session = session
        self.has_credential = has_credential
        # The vault entry the credential was read from, so a rejection names
        # the entry to replace rather than the one the host count suggests.
        self.secret_name = secret_name
        # Set once ``detail()`` has answered: from then on the change is known
        # to be visible to this request, so a 404 on a secondary leg is that
        # leg's absence (a removed endpoint, a patchset that does not exist),
        # never "store a credential".
        self.change_visible = False
        self.web = f"https://{ref.host}{ref.project}"
        # The authenticated REST tree is ``/a/``; an anonymous read uses the
        # bare one, since ``/a/`` answers 401 to a request carrying no credential.
        rest = f"{self.web}/a" if has_credential else self.web
        # ``<project>~<number>`` names the change unambiguously across projects.
        self.change_api = f"{rest}/changes/{quote(ref.repo, safe='')}~{ref.number}"
        patchset = _patchset_pin(ref)
        self.revision = patchset or "current"
        self.revision_api = f"{self.change_api}/revisions/{self.revision}"

    def freeze_revision(self, sha: str) -> None:
        """Point the revision-scoped reads at *sha* instead of the floating ``current``.

        A patchset uploaded between ``detail()`` and the files/patch reads would
        otherwise pair the earlier patchset's commit metadata with the newer
        one's diff; naming the SHA detail reported keeps one fetch on one
        patchset.
        """
        if sha and self.revision == "current":
            self.revision_api = f"{self.change_api}/revisions/{sha}"

    async def get(self, url: str, *, cap: int, raw: bool = False) -> Any:
        """GET one JSON document (or the raw body), mapping every failure to a provider error.

        The allowlist is re-read before each request, so once the operator
        removes the host no further request in this fetch reaches it.
        """
        host = self.ref.host
        _require_listed(host)
        # The session asks for JSON. The patch endpoint honours that by answering
        # a JSON STRING of the format-patch (XSSI guard, escapes and all), so a
        # raw read asks for text, which is what makes Gerrit send the base64 body
        # its REST documentation describes.
        headers = {"Accept": "text/plain"} if raw else None
        try:
            async with self.session.get(url, allow_redirects=False, headers=headers) as resp:
                if resp.status in {401, 403}:
                    if not self.has_credential:
                        raise SourceProviderNotConfigured(_setup_guidance(host))
                    if resp.status == 401:
                        raise SourceProviderError(
                            f"Gerrit at {host} rejected the configured credential. "
                            f"{_replace_credential_guidance(self.secret_name)}"
                        )
                    raise SourceProviderError(
                        f"The Gerrit credential for {host} cannot read this change. "
                        f"{_replace_credential_guidance(self.secret_name)}"
                    )
                if resp.status == 404:
                    if not self.has_credential and not self.change_visible:
                        # Gerrit answers an anonymous read of a change the
                        # anonymous user may not see with 404, not 401, so
                        # without a credential "not found" more often means
                        # "not visible to you" than "no such change".
                        raise SourceProviderNotConfigured(_setup_guidance(host))
                    raise _NoSuchResource(f"Gerrit at {host} has no such change or patchset.")
                if resp.status != 200:
                    raise SourceProviderError(
                        f"Gerrit at {host} returned HTTP {resp.status}. Try again later."
                    )
                body = await _shared.read_capped_response(resp, cap)
        except SourceProviderError:
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            raise SourceProviderError(
                f"Could not reach Gerrit at {host}: {type(exc).__name__}"
            ) from exc
        if len(body) > cap:
            raise SourceProviderError(f"Gerrit response from {host} is too large.")
        if raw:
            return body
        try:
            return json.loads(_strip_xssi(body))
        except (ValueError, RecursionError) as exc:
            raise SourceProviderError(
                f"Gerrit at {host} returned an unparseable response."
            ) from exc

    async def detail(self) -> dict[str, Any]:
        query = "&".join(f"o={option}" for option in _DETAIL_OPTIONS)
        data = await self.get(
            f"{self.change_api}/detail?{query}", cap=runner._METADATA_OUTPUT_BYTES
        )
        if not isinstance(data, dict):
            raise SourceProviderError("Gerrit returned an invalid change payload")
        self.change_visible = True
        return data

    async def files(self) -> dict[str, Any]:
        return projection._as_dict(
            await self.get(f"{self.revision_api}/files", cap=runner._METADATA_OUTPUT_BYTES)
        )

    async def patch(self) -> str:
        """The patchset as ``git format-patch`` text (the endpoint answers base64)."""
        body = await self.get(f"{self.revision_api}/patch", cap=runner._DIFF_OUTPUT_BYTES, raw=True)
        try:
            return base64.b64decode(body, validate=False).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            raise SourceProviderError("Gerrit returned an unreadable patch") from exc

    async def commit(self) -> dict[str, Any]:
        """The pinned patchset's commit; the current one travels in ``detail``."""
        return projection._as_dict(
            await self.get(f"{self.revision_api}/commit", cap=runner._METADATA_OUTPUT_BYTES)
        )

    async def mergeable(self) -> dict[str, Any]:
        return projection._as_dict(
            await self.get(
                f"{self.change_api}/revisions/current/mergeable", cap=runner._METADATA_OUTPUT_BYTES
            )
        )

    async def comments(self) -> dict[str, Any]:
        return projection._as_dict(
            await self.get(f"{self.change_api}/comments", cap=runner._DISCUSSION_OUTPUT_BYTES)
        )

    async def robot_comments(self) -> dict[str, Any]:
        """Robot comments, or ``{}`` on a Gerrit that has none to offer.

        Gerrit 3.12 disabled robot comments by default and 3.13 removed them, so
        the endpoint answers 404 there while every other leg of the same change
        serves. That is a complete discussion, not a degraded one: the panel
        must not carry a permanent "partial" banner for a feature the server no
        longer has.
        """
        try:
            return projection._as_dict(
                await self.get(
                    f"{self.change_api}/robotcomments", cap=runner._DISCUSSION_OUTPUT_BYTES
                )
            )
        except _NoSuchResource:
            return {}


# --- projection ----------------------------------------------------------------


def _is_ci_label(name: str) -> bool:
    return name == "Verified" or name.endswith("-Verified")


def _current_patchset_messages(detail: dict[str, Any]) -> list[dict[str, Any]]:
    current = projection._int_or_zero(detail.get("current_revision_number"))
    return [
        message
        for message in projection._as_list(detail.get("messages"))
        if projection._int_or_zero(message.get("_revision_number")) == current
    ]


def _label_vote_message(messages: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """The latest current-patchset message that cast a vote on *label*, or ``{}``.

    The trigger plugin writes ``Patch Set N: <Label>+1`` (or ``-1``) as the first
    line of its verdict message, followed by the build URL, so this is where a
    check row's link and timestamp come from.
    """
    # Anchored on the left against ``-`` as well as word characters: a search
    # for ``Verified`` must not match inside ``Post-Sil-Verified-1``.
    marker = re.compile(rf"(?<![\w-]){re.escape(label)}[+-]\d\b")
    for message in reversed(messages):
        if marker.search(str(message.get("message") or "")):
            return message
    return {}


def _first_url(text: str) -> str:
    match = _HTTPS_URL_RE.search(text)
    return projection._safe_https_url(match.group(0)) if match else ""


def _check_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Label votes as check rows, plus one pending row per open build count."""
    messages = _current_patchset_messages(detail)
    rows: list[dict[str, Any]] = []
    for name, info in projection._as_dict(detail.get("labels")).items():
        if not isinstance(name, str) or not _is_ci_label(name):
            continue
        label = projection._as_dict(info)
        # ``rejected``/``approved`` name the label's extreme votes only. A
        # ``Verified`` label spanning -2..+2 (OpenDev's Zuul votes -1 from its
        # check pipeline and -2 from gate) reports a -1 as ``disliked`` and a
        # +1 as ``recommended``, and those are a failure and a pass as much as
        # the extremes are: the sign of the vote is the verdict.
        if label.get("rejected") is not None or label.get("disliked") is not None:
            bucket, status = "failed", "FAILURE"
        elif label.get("approved") is not None or label.get("recommended") is not None:
            bucket, status = "passed", "SUCCESS"
        else:
            continue
        vote = _label_vote_message(messages, name)
        stamp = _iso(vote.get("date"))
        rows.append(
            {
                "name": name,
                "workflow": "",
                "status": status,
                "conclusion": status,
                "bucket": bucket,
                "url": _first_url(str(vote.get("message") or "")),
                "startedAt": stamp,
                "completedAt": stamp,
            }
        )
    running = _running_builds(messages)
    if running:
        last_url, last_date = running[-1]
        rows.append(
            {
                "name": f"CI build ({len(running)} running)" if len(running) > 1 else "CI build",
                "workflow": "",
                "status": "IN_PROGRESS",
                "conclusion": "",
                "bucket": "pending",
                "url": projection._safe_https_url(last_url),
                "startedAt": _iso(last_date),
                "completedAt": "",
            }
        )
    return rows


def _running_builds(messages: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """``(job url, started date)`` for every build started and not yet reported.

    A ``Build Started`` counts only on a message the server tagged
    ``autogenerated:`` (the trigger plugin's own notice); a reviewer quoting one
    starts nothing. It names one job url; a job is finished once a LATER CI
    report mentions that url (the server's verdict lists every job it ran with
    its result). A report is a message from the account that started the build,
    or one the server tagged ``autogenerated:``; a reviewer quoting the job url
    while the build is still running is neither and closes nothing. Messages
    arrive oldest first. A url that is started again is a re-trigger: it is
    running again until a report after THAT names it.
    """
    running: dict[str, tuple[Any, Any]] = {}
    for message in messages:
        text = str(message.get("message") or "")
        reporter = projection._as_dict(message.get("author")).get("_account_id")
        automated = str(message.get("tag") or "").startswith("autogenerated:")
        started = _BUILD_STARTED_RE.search(text) if automated else None
        if started:
            url = started.group(1).rstrip(").,")
            running[url] = (message.get("date"), reporter)
            continue
        for url, (_date, starter) in list(running.items()):
            if url in text and (automated or reporter == starter):
                del running[url]
    return [(url, date) for url, (date, _starter) in running.items()]


def _review_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """``Code-Review`` votes as review verdict rows."""
    label = projection._as_dict(projection._as_dict(detail.get("labels")).get("Code-Review"))
    rows: list[dict[str, Any]] = []
    for entry in projection._as_list(label.get("all")):
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or value == 0:
            continue
        if value >= 2:
            state = "APPROVED"
        elif value < 0:
            state = "CHANGES_REQUESTED"
        else:
            state = "COMMENTED"
        rows.append(
            {
                "id": f"code-review:{entry.get('_account_id', '')}",
                "kind": "review",
                "author": _person(entry),
                "body": f"Code-Review {value:+d}",
                "state": state,
                "createdAt": _iso(entry.get("date")),
                "url": "",
                "path": "",
                "line": None,
                "threadId": "",
                "resolvable": False,
                "resolved": False,
            }
        )
    return rows


def _thread_rows(
    client: _Client,
    by_path: dict[str, Any],
    robot_by_path: dict[str, Any],
    partial: list[str],
) -> list[dict[str, Any]]:
    """Flatten ``/comments`` and ``/robotcomments`` into threaded rows.

    Gerrit keys comments by file; ``/PATCHSET_LEVEL`` holds the change-level
    ones. Each comment names its parent in ``in_reply_to``, so the thread id is
    the root's id, found by walking up (bounded, since a malformed chain could
    loop). The thread's resolved state is the latest comment's ``unresolved``
    flag, which is how the Gerrit UI computes it. Both sets are threaded in ONE
    pass: a reviewer resolves a robot finding by replying to it from
    ``/comments`` with ``in_reply_to`` naming the robot comment, so threaded
    apart the reply would become a root of its own and the finding would stay
    unresolved. Each comment keeps its own author: the robot's id for a robot
    comment, the account for a human one.
    """
    comments: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    robots: set[str] = set()
    for source, robot in ((by_path, False), (robot_by_path, True)):
        for path, items in source.items():
            if not isinstance(path, str):
                continue
            for item in projection._as_list(items):
                comment_id = str(item.get("id") or "")
                if comment_id:
                    comments[comment_id] = item
                    paths[comment_id] = path
                    if robot:
                        robots.add(comment_id)

    def root_of(comment_id: str) -> str:
        seen = 0
        current = comment_id
        while seen < _MAX_THREAD_DEPTH:
            parent = str(comments.get(current, {}).get("in_reply_to") or "")
            if not parent or parent not in comments:
                return current
            current = parent
            seen += 1
        return current

    latest_flag: dict[str, tuple[str, bool]] = {}
    roots: dict[str, str] = {}
    for comment_id, item in comments.items():
        root = root_of(comment_id)
        roots[comment_id] = root
        stamp = str(item.get("updated") or "")
        if root not in latest_flag or stamp >= latest_flag[root][0]:
            latest_flag[root] = (stamp, bool(item.get("unresolved")))

    rows: list[dict[str, Any]] = []
    # Gerrit routes ``/c/<project>/+/<n>/comment/<id>/`` to the comment, but only
    # on the unpinned change path: with a patchset in the URL the same suffix
    # reads as a file named ``comment/<id>``. So the link is built from the
    # change, not from the (possibly pinned) ref URL.
    change_url = f"{client.web}/c/{client.ref.repo}/+/{client.ref.number}"
    ordered = sorted(comments.items(), key=lambda pair: str(pair[1].get("updated") or ""))
    for comment_id, item in ordered:
        if len(rows) >= _MAX_COMMENTS:
            projection._mark_partial(partial, "review discussions")
            break
        path = paths[comment_id]
        file_path = "" if path.startswith("/") else path
        line = item.get("line")
        root = roots[comment_id]
        author = str(item.get("robot_id") or "") if comment_id in robots else ""
        rows.append(
            {
                "id": comment_id,
                "kind": "inline" if file_path else "comment",
                "author": author or _person(item.get("author")),
                "body": str(item.get("message") or ""),
                "state": "",
                "createdAt": _iso(item.get("updated")),
                "url": (
                    f"{change_url}/comment/{quote(comment_id, safe='')}/" if comment_id else ""
                ),
                "path": file_path,
                "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
                "threadId": root,
                "resolvable": True,
                "resolved": not latest_flag[root][1],
            }
        )
    return rows


def _message_rows(detail: dict[str, Any], partial: list[str]) -> list[dict[str, Any]]:
    """Human change messages as top-level comment rows.

    Messages tagged ``autogenerated:`` (the trigger plugin's build notices, the
    server's own patchset notices) are the raw material of :func:`_check_rows`
    and are left out here, or the discussion would be mostly Jenkins.
    """
    rows: list[dict[str, Any]] = []
    for message in projection._as_list(detail.get("messages")):
        if str(message.get("tag") or "").startswith("autogenerated:"):
            continue
        if len(rows) >= _MAX_COMMENTS:
            projection._mark_partial(partial, "review discussions")
            break
        rows.append(
            {
                "id": str(message.get("id") or ""),
                "kind": "comment",
                "author": _person(message.get("author")),
                "body": str(message.get("message") or ""),
                "state": "",
                "createdAt": _iso(message.get("date")),
                "url": "",
                "path": "",
                "line": None,
                "threadId": "",
                "resolvable": False,
                "resolved": False,
            }
        )
    return rows


# Git C-quotes a path that carries a quote, a backslash, a control byte or a byte
# outside printable ASCII (``"a/caf\303\251.txt"``), and quotes each side on its
# own, so a rename may quote one side and not the other.
_DIFF_HEADER_RE = re.compile(
    r'^diff --git (?:"a/((?:[^"\\]|\\.)*)"|a/(.*?)) (?:"b/((?:[^"\\]|\\.)*)"|b/(.*))$',
    re.MULTILINE,
)
_C_ESCAPE_RE = re.compile(r"\\([0-7]{1,3}|.)")
_C_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
}


def _unquote_git_path(quoted: str) -> str:
    """The path inside a C-quoted ``diff --git`` side, octal escapes decoded as UTF-8 bytes."""
    out = bytearray()
    index = 0
    for match in _C_ESCAPE_RE.finditer(quoted):
        out += quoted[index : match.start()].encode("utf-8")
        escape = match.group(1)
        if escape.isdigit():
            out.append(int(escape, 8) & 0xFF)
        else:
            out += _C_ESCAPES.get(escape, escape.encode("utf-8"))
        index = match.end()
    out += quoted[index:].encode("utf-8")
    return out.decode("utf-8", errors="replace")


def _diff_header_destination(header: "re.Match[str]", metadata: str) -> str:
    """The section's path, read from its ``+++`` line (``---`` for a deletion).

    An unquoted ``diff --git`` header is ambiguous when a path holds `` b/``
    (``a/plan b/x b/plan b/x``), so the header is the fallback only. Git ends a
    ``---``/``+++`` name that holds a space with a tab, which is not the path.
    """
    sides = {
        line[:4]: line[4:].rstrip("\t")
        for line in metadata.splitlines()
        if line[:4] in ("--- ", "+++ ")
    }
    for prefix, side in (("+++ ", "b/"), ("--- ", "a/")):
        name = sides.get(prefix, "")
        if name.startswith('"') and name.endswith('"') and name[1:3] == side:
            return _unquote_git_path(name[3:-1])
        if name.startswith(side):
            return name[2:]
    quoted, plain = header.group(3), header.group(4)
    return _unquote_git_path(quoted) if quoted is not None else plain


def _patches(text: str) -> dict[str, str]:
    """Per-destination-path hunks (from the first ``@@``) out of a format-patch body."""
    out: dict[str, str] = {}
    headers = list(_DIFF_HEADER_RE.finditer(text))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        section = text[header.end() : end]
        hunk_start = section.find("\n@@")
        if hunk_start < 0:
            # A binary or mode-only entry has no hunks; the row still renders.
            continue
        body = section[hunk_start + 1 :].rstrip("\n")
        if index + 1 == len(headers):
            body = _strip_signature(body)
        out[_diff_header_destination(header, section[:hunk_start])] = body
    return out


def _strip_signature(body: str) -> str:
    """Drop the format-patch signature (``-- `` then the git version) off *body*.

    Only the final section can carry it, and a deleted line that was exactly
    ``- `` is a hunk line spelled the same way, so a match counts as the
    signature only when nothing after it is hunk content (hunk lines start with
    a space, ``+``, ``-``, ``\\`` or ``@``; the version trailer starts with a
    digit). Stripping inside a hunk would cut the file's diff short silently.
    """
    signature = body.rfind("\n-- \n")
    if signature < 0:
        return body
    trailer = body[signature + len("\n-- \n") :]
    if any(line[:1] in (" ", "+", "-", "\\", "@") for line in trailer.splitlines()):
        return body
    return body[:signature]


def _file_rows(files: dict[str, Any], patch_text: str) -> list[dict[str, Any]]:
    patches = _patches(patch_text)
    rows = []
    for path, info in files.items():
        # ``/COMMIT_MSG``, ``/MERGE_LIST`` and ``/PATCHSET_LEVEL`` are Gerrit's
        # magic files, not files in the tree.
        if not isinstance(path, str) or not path or path.startswith("/"):
            continue
        entry = projection._as_dict(info)
        rows.append(
            {
                "path": path,
                "status": _FILE_STATUS.get(str(entry.get("status") or ""), "modified"),
                "additions": projection._int_or_zero(entry.get("lines_inserted")),
                "deletions": projection._int_or_zero(entry.get("lines_deleted")),
                "patch": patches.get(path, ""),
            }
        )
    return rows


def _current_revision(detail: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    sha = str(detail.get("current_revision") or "")
    revision = projection._as_dict(projection._as_dict(detail.get("revisions")).get(sha))
    return sha, revision


def _commit_row(client: _Client, sha: str, patchset: int, commit: dict[str, Any]) -> dict[str, Any]:
    message = str(commit.get("message") or "")
    title, _, body = message.partition("\n")
    web_change = f"{client.web}/c/{client.ref.repo}/+/{client.ref.number}"
    return {
        "sha": sha,
        "title": str(commit.get("subject") or title),
        "body": body.strip(),
        "author": _person(commit.get("author")),
        "date": _iso(projection._as_dict(commit.get("author")).get("date")),
        "url": f"{web_change}/{patchset}" if patchset > 0 and _SHA_RE.fullmatch(sha) else "",
    }


def _lifecycle(detail: dict[str, Any]) -> str:
    return _STATE_MAP.get(str(detail.get("status") or "").upper(), "")


def _chip_lifecycle(detail: dict[str, Any]) -> str | None:
    return projection._project_state(_lifecycle(detail), draft=bool(detail.get("work_in_progress")))


def _merge_state(detail: dict[str, Any], mergeable: dict[str, Any] | None) -> tuple[str, str]:
    """``(mergeable, mergeStateStatus)`` in the shared vocabulary; ``("", "")`` once closed."""
    if _lifecycle(detail) != "OPEN":
        return "", ""
    verdict = mergeable.get("mergeable") if mergeable is not None else None
    # On a fast-forward-only project Gerrit answers ``false`` for any revision
    # whose parent is not the branch tip -- every child in a relation chain,
    # and any change the branch has moved past -- without attempting a merge,
    # so no ``conflicts`` list accompanies it. That is GitLab's ``need_rebase``
    # (a rebase is the only way forward), not a textual conflict. A ``conflicts``
    # list, or ``false`` under a merging submit type, is a real conflict.
    conflicts = mergeable.get("conflicts") if mergeable is not None else None
    fast_forward_only = (
        mergeable is not None
        and str(mergeable.get("submit_type") or "").upper() == "FAST_FORWARD_ONLY"
    )
    needs_rebase = verdict is False and fast_forward_only and not conflicts
    if verdict is True:
        merge = "mergeable"
    elif verdict is False and not needs_rebase:
        merge = "conflicting"
    else:
        merge = "unknown"
    if detail.get("work_in_progress"):
        status = "draft"
    elif detail.get("submittable") is True:
        status = "clean"
    elif needs_rebase:
        status = "need_rebase"
    elif verdict is False:
        status = "dirty"
    else:
        status = "blocked"
    return merge, status


def _log_degradation(leg: str, ref: SourceRef, exc: BaseException) -> None:
    """Record why a secondary leg degraded, at the right level.

    A provider failure (the instance refused or could not serve the leg) is
    expected and INFO. Anything else is a defect in this module -- it would
    otherwise be a permanently partial panel with no actionable signal -- so it
    is WARNING with the traceback, named as a defect rather than as Gerrit
    being down.
    """
    if isinstance(exc, SourceProviderError):
        logger.info("gerrit: %s for %s unavailable; degrading to partial: %s", leg, ref.url, exc)
        return
    logger.warning(
        "gerrit: unexpected %s in the %s leg for %s -- a defect in the provider, not a "
        "Gerrit failure; degrading to partial",
        type(exc).__name__,
        leg,
        ref.url,
        exc_info=exc,
    )


async def _with_client(ref: SourceRef, work: Callable[[_Client], Awaitable[_ResultT]]) -> _ResultT:
    """Run *work(client)* against *ref*'s instance, re-checking the allowlist first.

    The host was authorized when the URL was parsed; checking again here means a
    host the operator removed since then is not contacted, and its credential is
    not read, by a request already in flight.
    """
    _require_listed(ref.host)
    credential, secret_name = await asyncio.to_thread(_resolve_credential, ref.host)
    headers = {"Accept": "application/json"}
    if credential:
        headers["Authorization"] = _basic_auth(ref.host, credential, secret_name)
    timeout = ClientTimeout(total=_FETCH_TIMEOUT_SECS)
    async with ClientSession(timeout=timeout, headers=headers) as session:
        return await work(
            _Client(ref, session, has_credential=bool(credential), secret_name=secret_name)
        )


async def _fetch_change(ref: SourceRef) -> dict[str, Any]:
    async def work(client: _Client) -> dict[str, Any]:
        detail = await client.detail()
        partial: list[str] = []
        state = _lifecycle(detail)
        current_sha, current_revision = _current_revision(detail)
        pinned = client.revision != "current"
        client.freeze_revision(current_sha)

        async def nothing() -> None:
            return None

        files, patch_text, comments, robot_comments, mergeable, pinned_commit = (
            await asyncio.gather(
                client.files(),
                client.patch(),
                client.comments(),
                client.robot_comments(),
                client.mergeable() if state == "OPEN" else nothing(),
                client.commit() if pinned else nothing(),
                return_exceptions=True,
            )
        )
        # Secondary reads degrade to an empty, partial-marked section; the
        # change read above already carries the core fields.
        for value, section in (
            (files, "files"),
            (patch_text, "files"),
            (comments, "review discussions"),
            (robot_comments, "review discussions"),
            (pinned_commit, "commits"),
        ):
            if isinstance(value, BaseException):
                if isinstance(value, SourceProviderNotConfigured):
                    raise value
                _log_degradation(section, ref, value)
                projection._mark_partial(partial, section)
        if isinstance(mergeable, BaseException):
            if isinstance(mergeable, SourceProviderNotConfigured):
                raise mergeable
            # Not a section of its own: an unknown merge state costs one banner.
            _log_degradation("merge state", ref, mergeable)
            mergeable = None

        if pinned:
            # The pinned patchset's identity is the ref's; its commit metadata
            # comes from its own commit read. When that read failed, the fields
            # stay empty under the partial "commits" mark rather than borrowing
            # the current patchset's sha, message and link, which would show
            # this patchset's diff under another patchset's description.
            commit = pinned_commit if isinstance(pinned_commit, dict) else {}
            sha = str(commit.get("commit") or "")
            patchset = int(client.revision)
            head_ref = f"refs/changes/{ref.number % 100:02d}/{ref.number}/{patchset}"
        else:
            sha = current_sha
            patchset = projection._int_or_zero(current_revision.get("_number"))
            commit = projection._as_dict(current_revision.get("commit"))
            head_ref = str(current_revision.get("ref") or "")
        rows = _file_rows(
            projection._as_dict(projection._or_empty(files)),
            patch_text if isinstance(patch_text, str) else "",
        )
        discussion = [
            *_message_rows(detail, partial),
            *_thread_rows(
                client,
                projection._as_dict(projection._or_empty(comments)),
                projection._as_dict(projection._or_empty(robot_comments)),
                partial,
            ),
            *_review_rows(detail),
        ]
        discussion.sort(key=lambda row: row["createdAt"])
        merge, merge_status = _merge_state(
            detail, mergeable if isinstance(mergeable, dict) else None
        )
        return {
            "provider": contract.GERRIT_PROVIDER,
            # Identity comes from the validated ref, never from the instance's echo.
            "url": ref.url,
            "number": ref.number,
            "title": str(detail.get("subject") or ""),
            "description": str(commit.get("message") or ""),
            "state": state,
            "draft": bool(detail.get("work_in_progress")),
            "mergedAt": _iso(detail.get("submitted")) if state == "MERGED" else "",
            "mergeable": merge,
            "mergeStateStatus": merge_status,
            "autoMerge": False,
            "updatedAt": _iso(detail.get("updated")),
            "headBranch": head_ref,
            "baseBranch": str(detail.get("branch") or ""),
            "headSha": sha,
            "author": _person(detail.get("owner")),
            "additions": sum(row["additions"] for row in rows),
            "deletions": sum(row["deletions"] for row in rows),
            "changedFiles": len(rows),
            "commits": [_commit_row(client, sha, patchset, commit)] if sha else [],
            "checks": _check_rows(detail),
            "comments": discussion,
            "files": rows,
            "partialSections": partial,
        }

    return await _with_client(ref, work)


async def _fetch_checks(ref: SourceRef) -> list[dict[str, Any]]:
    async def work(client: _Client) -> list[dict[str, Any]]:
        return _check_rows(await client.detail())

    return await _with_client(ref, work)


async def _fetch_chip_status(ref: SourceRef) -> dict[str, str]:
    """The chip's ``{state, ci, ciPending, mergeable, mergeStateStatus}``.

    The merge pair is re-read on every refresh while the change is open. The
    full payload records it into the chip cache, and the chip refresh's
    keep-known rule carries a recorded pair forward whenever a refresh leaves
    it out, so a refresh that never answered it would pin a "conflicting" chip
    in the sidebar after a rebase cleared the conflict. A degraded mergeable
    read records nothing, which keep-known then fills from the last answer.
    """

    async def work(client: _Client) -> dict[str, str]:
        detail = await client.detail()
        status: dict[str, str] = {}
        state = _chip_lifecycle(detail)
        if state is not None:
            status["state"] = state
        projection._record_ci(status, _check_rows(detail))
        if _lifecycle(detail) == "OPEN":
            try:
                mergeable = await client.mergeable()
            except SourceProviderNotConfigured:
                raise
            except SourceProviderError as exc:
                _log_degradation("merge state", ref, exc)
            else:
                projection._record_merge_state(status, *_merge_state(detail, mergeable))
        return status

    return await _with_client(ref, work)


class GerritProvider:
    """The core Gerrit provider, served through the plugin seam.

    ``parse`` declines every URL: ``parse_source_url`` recognizes Gerrit links
    itself, from the operator's allowlist, before any plugin is asked.
    ``setup_message`` is empty so the seam shows the per-host guidance each
    not-configured error carries.
    """

    id = contract.GERRIT_PROVIDER

    def parse(self, raw_url: str) -> SourceRef | None:
        return None

    async def fetch_full(self, ref: SourceRef, *, refresh: bool = False) -> SourceChangePayload:
        return cast(SourceChangePayload, await _fetch_change(ref))

    async def fetch_checks(self, ref: SourceRef) -> list[dict[str, Any]]:
        return await _fetch_checks(ref)

    async def fetch_check_status(self, ref: SourceRef) -> dict[str, str]:
        return await _fetch_chip_status(ref)

    def setup_message(self) -> str:
        return ""


PROVIDER = GerritProvider()
plugins._install_core_seam_provider(PROVIDER)

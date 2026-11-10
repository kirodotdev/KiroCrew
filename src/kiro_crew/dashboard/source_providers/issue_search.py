"""Issue list and search for ``POST /api/source/issues``: GitHub and GitLab.

The browser sends a structured filter (or a provider-native query string) for
ONE scope: a GitHub ``owner/repo`` or a GitLab project path. Every field is
validated here before any provider call, and the scope is fixed by the gateway,
never by the query text: GitHub results are searched under a ``repo:`` qualifier
this module writes and are dropped unless they belong to that repository, and a
GitLab read is always the one project's issues endpoint.

Reads go through the same audited provider runner as the single-issue read, so
credentials stay inside ``gh``/``glab`` and the host is pinned from the
operator's allowlist. ``cache.search_issues`` applies the redaction, payload cap
and cache the single-issue read gets, so this module never returns provider
data itself.

Issue Radar lists issues through its own app-side client; its query rules
(``scope=all`` on GitLab, login shapes, the search paging bound) are mirrored
here rather than imported, because the gateway core must not depend on a
built-in app's backend.

Providers are entries in :data:`_PROVIDERS`: one ``validate`` and one ``search``
function each. Jira and Linear are named in :data:`_PLANNED_PROVIDERS` so a
request for them gets ``provider_not_supported`` instead of ``invalid_request``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

from kiro_crew.dashboard.source_providers import cache, hosts, projection, runner
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

# ── Request bounds ──────────────────────────────────────────────────────────
_MAX_SCOPE_CHARS = 512
_MAX_HOST_CHARS = 260
_MAX_LOGIN_CHARS = 255
_MAX_LABELS = 20
_MAX_LABEL_CHARS = 100
_MAX_TEXT_CHARS = 256
_MAX_RAW_CHARS = 512
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100
# An opaque page cursor. Today it is the next page number; six digits bounds it.
_CURSOR_RE = re.compile(r"[1-9][0-9]{0,5}")

_STATES = ("open", "closed", "all")
_SORTS = ("updated", "created")
_ORDERS = ("desc", "asc")

# ── Response bounds ─────────────────────────────────────────────────────────
_EXCERPT_CHARS = 280


class IssueQueryError(ValueError):
    """A request the gateway refuses before any provider call (HTTP 400).

    ``code`` is the machine-readable reason the response carries. Messages never
    echo the submitted value back.
    """

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IssueQuery:
    """One validated issue search. ``host`` and ``scope`` are provider-normalized."""

    provider: str
    scope: str
    host: str
    state: str
    assignee: str
    author: str
    labels: tuple[str, ...]
    text: str
    sort: str
    order: str
    raw: str
    limit: int
    page: int

    def cache_key(self) -> str:
        return json.dumps(
            [
                self.provider,
                self.host,
                self.scope,
                self.state,
                self.assignee,
                self.author,
                list(self.labels),
                self.text,
                self.sort,
                self.order,
                self.raw,
                self.limit,
                self.page,
            ],
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class IssueProvider:
    """``validate`` normalizes and checks the provider-specific fields (scope, host,
    logins, raw) and raises :class:`IssueQueryError`; ``fetch`` runs the read and
    returns the provider's rows unredacted, with no text cut or joined (it may
    pick whole rows or add paging headers); ``project`` reshapes those rows, after
    ``cache.search_issues`` has redacted them, into the response."""

    validate: Callable[[IssueQuery], IssueQuery]
    fetch: Callable[[IssueQuery], Awaitable[dict[str, Any]]]
    project: Callable[[IssueQuery, dict[str, Any]], dict[str, Any]]


# ── Common field validation ─────────────────────────────────────────────────


def _text_field(body: dict[str, Any], key: str, max_chars: int) -> str:
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise IssueQueryError(f"{key} must be a string.")
    value = value.strip()
    if len(value) > max_chars:
        raise IssueQueryError(f"{key} is longer than {max_chars} characters.")
    # isprintable() refuses every control character, newline and tab included.
    if value and not value.isprintable():
        raise IssueQueryError(f"{key} contains control characters.")
    return value


def _choice(body: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = body.get(key)
    if value is None or value == "":
        return choices[0]
    if value not in choices:
        raise IssueQueryError(f"{key} must be one of: {', '.join(choices)}.")
    return str(value)


def _limit(body: dict[str, Any]) -> int:
    value = body.get("limit")
    if value is None:
        return _DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        raise IssueQueryError(f"limit must be an integer from 1 to {_MAX_LIMIT}.")
    return value


def _page(body: dict[str, Any]) -> int:
    value = body.get("cursor")
    if value is None or value == "":
        return 1
    if not isinstance(value, str) or not _CURSOR_RE.fullmatch(value):
        raise IssueQueryError("cursor is not a cursor this endpoint returned.")
    return int(value)


def _labels(body: dict[str, Any]) -> tuple[str, ...]:
    value = body.get("labels")
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_LABELS:
        raise IssueQueryError(f"labels must be a list of at most {_MAX_LABELS} names.")
    labels: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise IssueQueryError("each label must be a string.")
        name = item.strip()
        if not name or len(name) > _MAX_LABEL_CHARS or not name.isprintable():
            raise IssueQueryError(
                f"each label must be 1 to {_MAX_LABEL_CHARS} printable characters."
            )
        # A quote would end GitHub's quoted label qualifier and a comma would
        # split GitLab's comma-joined list, so neither can carry one safely.
        if '"' in name or "," in name:
            raise IssueQueryError('a label name cannot contain " or ,.')
        if name not in labels:
            labels.append(name)
    return tuple(labels)


def parse_issue_query(body: Any) -> tuple[IssueProvider, IssueQuery]:
    """Validate a request body into its provider and normalized query."""
    if not isinstance(body, dict):
        body = {}
    provider_id = body.get("provider")
    if not isinstance(provider_id, str) or not provider_id:
        raise IssueQueryError("provider is required.")
    if provider_id in _PLANNED_PROVIDERS:
        raise IssueQueryError(
            f"Issue search for {provider_id} is not supported yet.", "provider_not_supported"
        )
    provider = _PROVIDERS.get(provider_id)
    if provider is None:
        raise IssueQueryError("provider must be one of: github, gitlab, jira, linear.")
    scope = _text_field(body, "scope", _MAX_SCOPE_CHARS)
    if not scope:
        raise IssueQueryError("scope is required.")
    query = IssueQuery(
        provider=provider_id,
        scope=scope,
        host=_text_field(body, "host", _MAX_HOST_CHARS).lower().rstrip("."),
        state=_choice(body, "state", _STATES),
        assignee=_text_field(body, "assignee", _MAX_LOGIN_CHARS),
        author=_text_field(body, "author", _MAX_LOGIN_CHARS),
        labels=_labels(body),
        text=_text_field(body, "text", _MAX_TEXT_CHARS),
        sort=_choice(body, "sort", _SORTS),
        order=_choice(body, "order", _ORDERS),
        raw=_text_field(body, "raw", _MAX_RAW_CHARS),
        limit=_limit(body),
        page=_page(body),
    )
    return provider, provider.validate(query)


# ── Projection shared by the providers ──────────────────────────────────────

_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,}).*$", re.MULTILINE)
# Neither class can cross the next "[" or "(", so each try stops at the next
# candidate and a body of repeated "![" or "[](" is stripped in linear time.
# shortcut: a link whose url holds parentheses is left as typed; upgrade to a
# balanced scan if excerpts show them.
_IMAGE_RE = re.compile(r"!\[([^\[\]\n]*)\]\([^()\n]*\)")
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\([^()\n]*\)")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>\n]{0,200}>")
_LINE_PREFIX_RE = re.compile(
    r"^[ \t]*(?:>[ \t]?)*(?:#{1,6}[ \t]+|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d{1,9}[.)][ \t]+)?",
    re.MULTILINE,
)
_RULE_RE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*|__|~~|`")
_SPACE_RE = re.compile(r"\s+")


def plain_excerpt(body: Any) -> str:
    """The first ~280 characters of a markdown body as plain text.

    shortcut: a line-level strip (comments, fences, links, images, tags, list and
    heading markers, bold/code marks), not a markdown parser; upgrade if excerpts
    need exact rendering of nested constructs.
    """
    if not isinstance(body, str) or not body:
        return ""
    # The body arrives redacted (``cache.search_issues`` redacts the raw rows
    # before projection). A secret the raw pass could not recognise (split by
    # ``**``, a link or a comment) only forms once the strip below joins it, and
    # the final pass needs it whole, so the strip runs on the WHOLE body and the
    # only cut is the word-boundary one below. Every pattern is linear and the
    # provider output cap bounds the total input.
    text = _HTML_COMMENT_RE.sub(" ", body)
    text = _FENCE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _RULE_RE.sub(" ", text)
    text = _LINE_PREFIX_RE.sub("", text)
    text = _EMPHASIS_RE.sub("", text).replace("|", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) <= _EXCERPT_CHARS:
        return text
    # Cut at a word boundary only: a hard cut inside a long unrecognised run
    # would keep a fragment the final redaction pass cannot judge whole.
    cut = _drop_partial_word(text[: _EXCERPT_CHARS + 1]).rstrip()
    return cut + "…" if cut else ""


def _drop_partial_word(text: str) -> str:
    """*text* without its trailing non-whitespace run (all of it if it has none)."""
    if not text or text[-1].isspace():
        return text
    match = re.search(r"\s\S*\Z", text)
    return text[: match.start()] if match else ""


def _person(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    login = str(value.get("login") or value.get("username") or "")
    if not login:
        return None
    return {
        "login": login,
        "name": str(value.get("name") or login),
        "avatarUrl": projection._safe_https_url(value.get("avatar_url")),
    }


def _people(value: Any) -> list[dict[str, str]]:
    return [person for person in (_person(item) for item in projection._as_list(value)) if person]


def _result(
    query: IssueQuery, issues: list[dict[str, Any]], next_page: int | None, total: int | None
) -> dict[str, Any]:
    return {
        "provider": query.provider,
        "scope": query.scope,
        "issues": issues,
        "nextCursor": str(next_page) if next_page else None,
        "total": total,
    }


def _total(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# ── GitHub ──────────────────────────────────────────────────────────────────

# Alphanumerics, hyphens and underscores (an Enterprise Managed User login is
# ``<handle>_<shortcode>``), so a login can never carry a space or a colon into
# the search string.
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}")
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_GITHUB_PUBLIC_HOSTS = frozenset({"github.com", "www.github.com"})
# The search API serves at most this many results for one query.
_GITHUB_SEARCH_MAX_RESULTS = 1000
# Qualifiers that name WHERE to search. Several of them OR together on GitHub, so
# one in the raw text would widen the gateway's own repo: scope.
_GITHUB_SCOPE_QUALIFIERS = frozenset({"repo", "org", "user", "owner"})
_GITHUB_BOOLEAN_WORDS = frozenset({"OR", "AND", "NOT"})
_QUOTED_RE = re.compile(r'"[^"]*"')
# The fields one search item is projected from.
_GITHUB_SEARCH_JQ = (
    "{total_count: .total_count, items: [.items[]? | {"
    "number, title, state, state_reason, repository_url, comments, created_at, updated_at, "
    "is_pr: (.pull_request != null), "
    "labels: [.labels[]? | {name, color, description}], "
    "assignees: [.assignees[]? | {login, avatar_url}], "
    "user: (if .user then {login: .user.login, avatar_url: .user.avatar_url} else null end), "
    # The body is NOT cut here: redaction runs on these rows, and a cut can split a
    # secret (a PEM key whose END line falls past it) so the redactor cannot see it.
    # The excerpt is cut only after redaction.
    'body: (.body // "")}]}'
)


def _github_login(value: str, field: str) -> str:
    if not value:
        return ""
    if value == "me":
        return "@me"
    if not _GITHUB_LOGIN_RE.fullmatch(value):
        raise IssueQueryError(f"{field} is not a valid GitHub login.")
    return value


def _validate_github(query: IssueQuery) -> IssueQuery:
    owner, sep, repo = query.scope.partition("/")
    if (
        not sep
        or not _GITHUB_LOGIN_RE.fullmatch(owner)
        or not _GITHUB_REPO_RE.fullmatch(repo)
        or repo in {".", ".."}
    ):
        raise IssueQueryError("scope must be a GitHub owner/repo.")
    host = query.host or "github.com"
    if host in _GITHUB_PUBLIC_HOSTS:
        host = "github.com"
    elif ":" in host or host not in hosts._allowed_github_hosts():
        raise IssueQueryError("host is not a GitHub host this gateway allows.")
    if query.raw:
        _github_raw_terms(query.raw)
    _github_login(query.assignee, "assignee")
    _github_login(query.author, "author")
    return replace(query, host=host)


def _split_terms(raw: str) -> list[str]:
    """Split search text on whitespace outside double quotes."""
    terms: list[str] = []
    current: list[str] = []
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
        if char.isspace() and not quoted:
            if current:
                terms.append("".join(current))
                current = []
            continue
        current.append(char)
    if quoted:
        raise IssueQueryError("raw has an unbalanced quote.")
    if current:
        terms.append("".join(current))
    return terms


def _github_raw_terms(raw: str) -> list[str]:
    """The raw query's terms, refusing any that could widen the repo scope."""
    terms = _split_terms(raw)
    for term in terms:
        bare = _QUOTED_RE.sub("", term)
        if term in _GITHUB_BOOLEAN_WORDS or "(" in bare or ")" in bare:
            raise IssueQueryError("raw cannot use OR, AND, NOT or parentheses.")
        key, colon, _value = term.lstrip("-").partition(":")
        if colon and not key.startswith('"') and key.lower() in _GITHUB_SCOPE_QUALIFIERS:
            raise IssueQueryError("raw cannot name a repo, org, user or owner; scope sets it.")
    return terms


def _github_text_terms(text: str) -> list[str]:
    """Free text as quoted words, so no word can act as a qualifier or operator."""
    return [f'"{word}"' for word in text.replace('"', " ").split()]


def github_search_string(query: IssueQuery) -> str:
    """The ``q`` the gateway sends: its own repo scope first, then the filters."""
    terms = [f"repo:{query.scope}", "is:issue"]
    if query.raw:
        return " ".join([*terms, *_github_raw_terms(query.raw)])
    if query.state != "all":
        terms.append(f"is:{query.state}")
    if query.assignee:
        terms.append(f"assignee:{_github_login(query.assignee, 'assignee')}")
    if query.author:
        terms.append(f"author:{_github_login(query.author, 'author')}")
    terms.extend(f'label:"{label}"' for label in query.labels)
    terms.extend(_github_text_terms(query.text))
    return " ".join(terms)


def _github_summary(query: IssueQuery, item: dict[str, Any]) -> dict[str, Any] | None:
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    state = "closed" if str(item.get("state") or "").lower() == "closed" else "open"
    if state == "open":
        status = "Open"
    elif item.get("state_reason") == "not_planned":
        status = "Not planned"
    else:
        status = "Closed"
    body = item.get("body")
    return {
        "provider": "github",
        # Built from the validated scope, never from the provider's html_url.
        "url": f"https://{query.host}/{query.scope}/issues/{number}",
        "key": f"#{number}",
        "number": number,
        "title": str(item.get("title") or ""),
        "state": state,
        "statusLabel": status,
        "labels": projection._issue_labels(item.get("labels")),
        "assignees": _people(item.get("assignees")),
        "author": _person(item.get("user")),
        "priority": None,
        "commentCount": projection._int_or_zero(item.get("comments")),
        "createdAt": str(item.get("created_at") or ""),
        "updatedAt": str(item.get("updated_at") or ""),
        "excerpt": plain_excerpt(body),
        "project": query.scope,
        "linkedChanges": [],
    }


def _github_window(query: IssueQuery) -> tuple[int, int, int] | None:
    """The ``(per_page, page, skip)`` that reads the query's page inside GitHub's
    1,000-result window, or ``None`` when the page starts past it.

    A page that would end past the window can be refused (422), so the last,
    partial page reads the window's final 100 results and skips to the offset;
    ``limit`` is at most 100, so that offset always lies in those 100.
    """
    offset = (query.page - 1) * query.limit
    if offset >= _GITHUB_SEARCH_MAX_RESULTS:
        return None
    if query.page * query.limit <= _GITHUB_SEARCH_MAX_RESULTS:
        return query.limit, query.page, 0
    last = _MAX_LIMIT
    return last, _GITHUB_SEARCH_MAX_RESULTS // last, offset - (_GITHUB_SEARCH_MAX_RESULTS - last)


async def _fetch_github_issues(query: IssueQuery) -> dict[str, Any]:
    window = _github_window(query)
    if window is None:
        return {"total_count": None, "items": []}
    per_page, page, skip = window
    path = (
        f"search/issues?q={quote(github_search_string(query), safe='')}"
        f"&sort={query.sort}&order={query.order}&per_page={per_page}&page={page}"
    )
    data = await runner._run_json(
        "gh",
        "api",
        path,
        "--jq",
        _GITHUB_SEARCH_JQ,
        # Same cap as the GitLab list. shortcut: a page whose full bodies pass
        # 4 MiB fails as a provider error; lower `limit` if real repos hit it.
        max_output_bytes=runner._DIFF_OUTPUT_BYTES,
        host=query.host,
    )
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SourceProviderError("GitHub returned an invalid issue search payload")
    if skip or per_page != query.limit:
        data["items"] = data["items"][skip : skip + query.limit]
    return data


def _project_github_issues(query: IssueQuery, data: dict[str, Any]) -> dict[str, Any]:
    offset = (query.page - 1) * query.limit
    if offset >= _GITHUB_SEARCH_MAX_RESULTS:
        return _result(query, [], None, None)
    rows = data.get("items")
    if not isinstance(rows, list):
        raise SourceProviderError("GitHub returned an invalid issue search payload")
    # The issues index mixes in pull requests and the search could, in principle,
    # answer outside the repo: only this repository's issues are kept.
    repo_suffix = f"/repos/{query.scope}".lower()
    issues = [
        summary
        for summary in (
            _github_summary(query, row)
            for row in projection._as_list(rows)
            if not row.get("is_pr")
            and str(row.get("repository_url") or "").lower().endswith(repo_suffix)
        )
        if summary
    ]
    total = _total(data.get("total_count"))
    reachable = min(total or 0, _GITHUB_SEARCH_MAX_RESULTS)
    has_next = len(rows) >= query.limit and offset + len(rows) < reachable
    return _result(query, issues, query.page + 1 if has_next else None, total)


# ── GitLab ──────────────────────────────────────────────────────────────────

# Mirrors Issue Radar's GitLab segment rule and its nesting ceiling (20 ancestor
# groups, the group itself, then the project).
_GITLAB_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")
_GITLAB_MAX_PROJECT_SEGMENTS = 22
_GITLAB_LOGIN_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254}")
_GITLAB_PUBLIC_HOSTS = frozenset({"gitlab.com", "www.gitlab.com"})
# Filter keys a raw GitLab query may set. Each only narrows the one project's
# issue list; paging, ordering, scope and label detail stay the gateway's.
_GITLAB_RAW_KEYS = frozenset(
    {
        "labels",
        "not[labels]",
        "milestone",
        "not[milestone]",
        "search",
        "in",
        "assignee_username",
        "not[assignee_username]",
        "author_username",
        "not[author_username]",
        "state",
        "confidential",
        "iids[]",
        "issue_type",
        "not[issue_type]",
        "weight",
        "my_reaction_emoji",
        "created_after",
        "created_before",
        "updated_after",
        "updated_before",
        "due_date",
        "health_status",
        "iteration_title",
        "epic_id",
    }
)
_GITLAB_MAX_RAW_PAIRS = 20
_HEADER_INT_RE = re.compile(r"[0-9]{1,9}")


def _validate_gitlab(query: IssueQuery) -> IssueQuery:
    segments = query.scope.strip("/").split("/")
    if (
        len(segments) < 2
        or len(segments) > _GITLAB_MAX_PROJECT_SEGMENTS
        or any(
            segment in {".", ".."} or not _GITLAB_SEGMENT_RE.fullmatch(segment)
            for segment in segments
        )
    ):
        raise IssueQueryError("scope must be a GitLab project path like group/project.")
    host = query.host or "gitlab.com"
    if host in _GITLAB_PUBLIC_HOSTS:
        host = "gitlab.com"
    elif host not in hosts._allowed_gitlab_hosts():
        raise IssueQueryError("host is not a GitLab host this gateway allows.")
    for field, value in (("assignee", query.assignee), ("author", query.author)):
        if value and value != "me" and not _GITLAB_LOGIN_RE.fullmatch(value):
            raise IssueQueryError(f"{field} is not a valid GitLab username.")
    if query.raw:
        _gitlab_raw_pairs(query.raw)
    return replace(query, host=host, scope="/".join(segments))


def _gitlab_raw_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse a raw ``key=value&...`` filter, keeping only allowed filter keys."""
    try:
        pairs = parse_qsl(raw.lstrip("?"), keep_blank_values=False, strict_parsing=True)
    except ValueError:
        raise IssueQueryError("raw must be a GitLab filter like labels=bug&milestone=v1.") from None
    if len(pairs) > _GITLAB_MAX_RAW_PAIRS:
        raise IssueQueryError(f"raw sets more than {_GITLAB_MAX_RAW_PAIRS} filters.")
    seen: set[str] = set()
    for key, value in pairs:
        if key not in _GITLAB_RAW_KEYS:
            raise IssueQueryError("raw uses a filter this endpoint does not allow.")
        if not value.isprintable():
            raise IssueQueryError("raw contains control characters.")
        if key in seen and not key.endswith("[]"):
            raise IssueQueryError("raw sets the same filter twice.")
        seen.add(key)
    return pairs


async def _gitlab_username(host: str) -> str:
    """The signed-in GitLab user, for ``assignee``/``author`` = ``me``.

    Resolved rather than left out: a filter for "me" that silently matched
    everyone would read as an empty "my issues" list being full.
    """
    user = await runner._run_json("glab", "api", "user", host=host)
    username = str(user.get("username") or "") if isinstance(user, dict) else ""
    if not _GITLAB_LOGIN_RE.fullmatch(username):
        raise SourceProviderError("GitLab did not report a signed-in user")
    return username


def _parse_gitlab_list(host: str) -> Callable[[int, bytes, bytes], tuple[dict[str, str], Any]]:
    """Parse ``glab api --include`` output into (lower-cased headers, JSON body)."""

    def parse(returncode: int, stdout: bytes, stderr: bytes) -> tuple[dict[str, str], Any]:
        if returncode != 0:
            raise runner._provider_failure("glab", stderr, host)
        text = stdout.decode("utf-8", errors="replace")
        head, sep, body = text.partition("\r\n\r\n")
        if not sep:
            head, sep, body = text.partition("\n\n")
        lines = head.splitlines()
        if not sep or not lines or not lines[0].upper().startswith("HTTP/"):
            raise SourceProviderError("GitLab returned an invalid issue list")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if colon:
                headers[name.strip().lower()] = value.strip()
        try:
            return headers, json.loads(body)
        except json.JSONDecodeError:
            raise SourceProviderError("GitLab returned an invalid issue list") from None

    return parse


def _gitlab_summary(query: IssueQuery, item: dict[str, Any]) -> dict[str, Any] | None:
    number = item.get("iid")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    state = "closed" if item.get("state") == "closed" else "open"
    return {
        "provider": "gitlab",
        # Built from the validated host and project, never from web_url.
        "url": f"https://{query.host}/{query.scope}/-/issues/{number}",
        "key": f"#{number}",
        "number": number,
        "title": str(item.get("title") or ""),
        "state": state,
        "statusLabel": "Closed" if state == "closed" else "Open",
        "labels": projection._issue_labels(item.get("labels")),
        "assignees": _people(item.get("assignees")),
        "author": _person(item.get("author")),
        "priority": None,
        "commentCount": projection._int_or_zero(item.get("user_notes_count")),
        "createdAt": str(item.get("created_at") or ""),
        "updatedAt": str(item.get("updated_at") or ""),
        "excerpt": plain_excerpt(item.get("description")),
        "project": query.scope,
        "linkedChanges": [],
    }


async def gitlab_params(query: IssueQuery) -> list[tuple[str, str]]:
    """The query string for the project's issues endpoint."""
    params: list[tuple[str, str]] = [
        # Without scope=all GitLab lists only issues the caller created.
        ("scope", "all"),
        ("with_labels_details", "true"),
        ("order_by", f"{query.sort}_at"),
        ("sort", query.order),
        ("per_page", str(query.limit)),
        ("page", str(query.page)),
    ]
    if query.raw:
        return params + _gitlab_raw_pairs(query.raw)
    if query.state != "all":
        params.append(("state", "opened" if query.state == "open" else "closed"))
    me = ""
    if "me" in (query.assignee, query.author):
        me = await _gitlab_username(query.host)
    if query.assignee:
        params.append(("assignee_username", me if query.assignee == "me" else query.assignee))
    if query.author:
        params.append(("author_username", me if query.author == "me" else query.author))
    if query.labels:
        params.append(("labels", ",".join(query.labels)))
    if query.text:
        params.append(("search", query.text))
    return params


async def _fetch_gitlab_issues(query: IssueQuery) -> dict[str, Any]:
    params = await gitlab_params(query)
    path = f"projects/{quote(query.scope, safe='')}/issues?{urlencode(params, quote_via=quote)}"
    headers, rows = await runner._run_provider(
        "glab",
        "api",
        path,
        "--include",
        max_output_bytes=runner._DIFF_OUTPUT_BYTES,
        host=query.host,
        parse=_parse_gitlab_list(query.host),
    )
    if not isinstance(rows, list):
        raise SourceProviderError("GitLab returned an invalid issue list")
    return {
        "rows": rows,
        "x-next-page": headers.get("x-next-page", ""),
        "x-total": headers.get("x-total", ""),
    }


def _project_gitlab_issues(query: IssueQuery, data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SourceProviderError("GitLab returned an invalid issue list")
    issues = [
        summary
        for summary in (_gitlab_summary(query, row) for row in projection._as_list(rows))
        if summary
    ]
    # ASCII digits only: str.isdigit() also accepts "²", which int() refuses, and
    # that ValueError would carry the header text into a 400 body.
    next_text = str(data.get("x-next-page") or "")
    next_page = int(next_text) if _HEADER_INT_RE.fullmatch(next_text) else None
    if next_page is not None and next_page <= query.page:
        next_page = None
    total_text = str(data.get("x-total") or "")
    # GitLab omits X-Total past 10,000 results; the total is then unknown.
    total = int(total_text) if _HEADER_INT_RE.fullmatch(total_text) else None
    return _result(query, issues, next_page, total)


_PROVIDERS: dict[str, IssueProvider] = {
    "github": IssueProvider(
        validate=_validate_github, fetch=_fetch_github_issues, project=_project_github_issues
    ),
    "gitlab": IssueProvider(
        validate=_validate_gitlab, fetch=_fetch_gitlab_issues, project=_project_gitlab_issues
    ),
}
# In the request contract, not implemented yet: each lands as one more entry above.
_PLANNED_PROVIDERS = frozenset({"jira", "linear"})


# ── Entry point ─────────────────────────────────────────────────────────────


async def search_issues(body: Any, *, refresh: bool = False) -> dict[str, Any]:
    """Validate *body*, then answer through ``cache.search_issues``.

    Raises :class:`IssueQueryError` (a ``ValueError``) for a refused request and
    :class:`SourceProviderError` for a failed read.
    """
    # Load the host allowlists off the loop before validation reads them.
    await hosts.ensure_gitlab_hosts_loaded()
    provider, query = parse_issue_query(body)
    return await cache.search_issues(
        query.cache_key(),
        lambda: provider.fetch(query),
        lambda rows: provider.project(query, rows),
        refresh=refresh,
    )

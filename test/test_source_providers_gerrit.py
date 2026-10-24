"""Gerrit changes: ``dashboard.gerrit_hosts`` end to end.

A host listed in config makes its ``/c/<project>/+/<number>`` links parse, and
the source API then reads the change over the instance's REST API -- here a
stubbed one shaped after Gerrit 3.10's answers -- with the vault credential sent
only to that host as HTTP basic auth and no redirect followed. An empty list
leaves every existing provider as it is, and every write is refused as
unsupported.
"""

from __future__ import annotations

import base64
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import loader as config_loader
from kiro_crew.config import sections as config_sections
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.source_providers import chip_refresh, gerrit, hosts, links, plugins
from kiro_crew.dashboard.source_providers.contract import SourceProviderError

HOST = "gerrit.acme.example:8443"
CHANGE_URL = f"https://{HOST}/r/c/platform/hal/+/466698"
REST = f"https://{HOST}/r/a/changes/platform%2Fhal~466698"
DETAIL_API = f"{REST}/detail?" + "&".join(f"o={o}" for o in gerrit._DETAIL_OPTIONS)
CURRENT_SHA = "a" * 40
PREVIOUS_SHA = "b" * 40
CREDENTIAL = "dev1:fixture-http-password"
BASIC = "Basic " + base64.b64encode(CREDENTIAL.encode()).decode()


@pytest.fixture(autouse=True)
def _cold_allowlists(_floor_monkeypatch):
    for name in (
        "_github_hosts_snapshot",
        "_gitlab_hosts_snapshot",
        "_jira_hosts_snapshot",
        "_bitbucket_server_hosts_snapshot",
        "_gerrit_hosts_snapshot",
    ):
        _floor_monkeypatch.setattr(hosts, name, frozenset())


@pytest.fixture(autouse=True)
def _isolated_caches(_floor_monkeypatch):
    _floor_monkeypatch.setattr(source, "_sel", lambda: MagicMock())
    for name in (
        "_CACHE",
        "_FULL_FETCH_INFLIGHT",
        "_FULL_FETCH_TASKS",
        "_FULL_FETCH_GENERATIONS",
        "_CHECKS_FETCH_INFLIGHT",
    ):
        _floor_monkeypatch.setattr(source, name, {})


@pytest.fixture
def listed(monkeypatch):
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A real config.json naming the instance, loaded by the real refresh."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"dashboard": {"gerrit_hosts": [HOST]}}), encoding="utf-8")
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


# --- a stubbed Gerrit ----------------------------------------------------------


def _stamp(day: int, hour: int = 0) -> str:
    return f"2026-01-{day:02d} {hour:02d}:00:00.000000000"


def _account(name: str, account_id: int) -> dict[str, Any]:
    return {"_account_id": account_id, "name": name, "username": name.lower().replace(" ", "")}


def test_an_account_renders_as_gerrit_shows_it() -> None:
    # Gerrit's page prefers the display name an account set over its full name
    # (`Paladox` for an account whose name is `Paladox none`), then the username.
    assert (
        gerrit._person({"name": "Paladox none", "display_name": "Paladox", "username": "paladox"})
        == "Paladox"
    )
    assert gerrit._person({"name": "Dev One", "username": "devone"}) == "Dev One"
    assert gerrit._person({"username": "devone"}) == "devone"
    assert gerrit._person(None) == ""


COMMIT_MESSAGE = (
    "hal eth [Bug Fix]: drop the phantom adapter\n\nIt never existed.\n\ndevices: al13ml"
)
DETAIL_DOC = {
    "_number": 466698,
    "project": "platform/hal",
    "branch": "dev",
    "status": "NEW",
    "subject": "hal eth [Bug Fix]: drop the phantom adapter",
    "created": _stamp(1),
    "updated": _stamp(3, 12),
    "insertions": 21,
    "deletions": 48,
    "submittable": False,
    "unresolved_comment_count": 1,
    "owner": _account("Dev One", 1001),
    "current_revision": CURRENT_SHA,
    "current_revision_number": 2,
    "revisions": {
        CURRENT_SHA: {
            "_number": 2,
            "ref": "refs/changes/98/466698/2",
            "created": _stamp(2),
            "commit": {
                "subject": "hal eth [Bug Fix]: drop the phantom adapter",
                "message": COMMIT_MESSAGE,
                "author": {"name": "Dev One", "email": "dev1@acme.example", "date": _stamp(2)},
            },
        }
    },
    "labels": {
        "Code-Review": {
            "all": [
                {**_account("Reviewer One", 2001), "value": 2, "date": _stamp(3)},
                {**_account("Reviewer Two", 2002), "value": -1, "date": _stamp(3, 1)},
                {**_account("Jenkins", 3001), "value": 0},
            ],
            "values": {"-2": "", "-1": "", " 0": "", "+1": "", "+2": ""},
        },
        "Verified": {"approved": _account("Jenkins", 3001), "all": []},
        "Post-Sil-Verified": {"rejected": _account("Jenkins", 3001), "all": []},
        "Graviton-Verified": {"all": []},
        "Priority": {"approved": _account("Dev One", 1001), "all": []},
    },
    "messages": [
        {
            "id": "m1",
            "tag": "autogenerated:gerrit:newPatchSet",
            "author": _account("Dev One", 1001),
            "date": _stamp(2),
            "message": "Uploaded patch set 2.",
            "_revision_number": 2,
        },
        {
            "id": "m2",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 1),
            "message": "Patch Set 2:\n\nBuild Started https://ci.acme.example/job/core/55/",
            "_revision_number": 2,
        },
        {
            "id": "m3",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 2),
            "message": "Patch Set 2:\n\nBuild Started https://ci.acme.example/job/post-sil/9/",
            "_revision_number": 2,
        },
        {
            "id": "m3b",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 2),
            "message": "Patch Set 2:\n\nBuild Started https://ci.acme.example/job/lint/12/",
            "_revision_number": 2,
        },
        {
            "id": "m4",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 3),
            "message": "Patch Set 2: Verified+1\n\nBuild Successful \n\nhttps://ci.acme.example/job/core/55/ : SUCCESS\n\nhttps://ci.acme.example/job/lint/12/ : SUCCESS",
            "_revision_number": 2,
        },
        {
            "id": "m5",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 4),
            "message": "Patch Set 2: Post-Sil-Verified-1\n\nBuild Failed \n\nhttps://ci.acme.example/job/post-sil/9/ : FAILURE",
            "_revision_number": 2,
        },
        {
            "id": "m6",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(2, 5),
            "message": "Patch Set 2:\n\nBuild Started https://ci.acme.example/job/graviton/3/",
            "_revision_number": 2,
        },
        {
            "id": "m7",
            "author": _account("Reviewer One", 2001),
            "date": _stamp(3),
            "message": "Patch Set 2: Code-Review+2\n\nLooks good.",
            "_revision_number": 2,
        },
        {
            "id": "m0",
            "tag": "autogenerated:jenkins-gerrit-trigger",
            "author": _account("Jenkins", 3001),
            "date": _stamp(1, 1),
            "message": "Patch Set 1:\n\nBuild Started https://ci.acme.example/job/core/54/",
            "_revision_number": 1,
        },
    ],
}
FILES_DOC = {
    "/COMMIT_MSG": {"status": "A", "lines_inserted": 7, "size_delta": 300, "size": 300},
    "src/cache.py": {"status": "A", "lines_inserted": 2, "size_delta": 11, "size": 11},
    "src/app.py": {"lines_inserted": 1, "lines_deleted": 1, "size_delta": 0, "size": 40},
    "src/old.py": {"status": "D", "lines_deleted": 3, "size_delta": -30, "size": 0},
}
PATCH_TEXT = (
    f"From {CURRENT_SHA} Mon Sep 17 00:00:00 2001\n"
    "From: Dev One <dev1@acme.example>\n"
    "Date: Fri, 02 Jan 2026 00:00:00 +0000\n"
    "Subject: [PATCH] hal eth [Bug Fix]: drop the phantom adapter\n"
    "\n"
    "It never existed.\n"
    "---\n"
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " import os\n"
    "-cache = None\n"
    "+from cache import CACHE\n"
    "diff --git a/src/cache.py b/src/cache.py\n"
    "new file mode 100644\n"
    "index 0000000..3333333\n"
    "--- /dev/null\n"
    "+++ b/src/cache.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+CACHE = {}\n"
    "+\n"
    "diff --git a/assets/logo.png b/assets/logo.png\n"
    "Binary files differ\n"
    "diff --git a/src/old.py b/src/old.py\n"
    "deleted file mode 100644\n"
    "--- a/src/old.py\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-a\n"
    "-b\n"
    "-c\n"
    "-- \n"
    "2.47.0\n"
)
COMMENTS_DOC = {
    "src/cache.py": [
        {
            "id": "c-root",
            "author": _account("Reviewer One", 2001),
            "patch_set": 2,
            "line": 1,
            "updated": _stamp(2, 6),
            "message": "Why a module-level dict?",
            "unresolved": True,
        },
        {
            "id": "c-reply",
            "author": _account("Dev One", 1001),
            "patch_set": 2,
            "line": 1,
            "in_reply_to": "c-root",
            "updated": _stamp(2, 7),
            "message": "It is per process by design.",
            "unresolved": False,
        },
    ],
    "/PATCHSET_LEVEL": [
        {
            "id": "c-level",
            "author": _account("Reviewer Two", 2002),
            "patch_set": 2,
            "updated": _stamp(3, 1),
            "message": "Please split the header change out.",
            "unresolved": True,
        }
    ],
}
ROBOT_COMMENTS_DOC = {
    "src/app.py": [
        {
            "id": "r-1",
            "robot_id": "AutoSDE",
            "robot_run_id": "run-7",
            "patch_set": 2,
            "line": 2,
            "updated": _stamp(2, 8),
            "message": "[AutoSDE] Prefer an explicit import.",
            "unresolved": True,
        }
    ]
}
MERGEABLE_DOC = {"submit_type": "FAST_FORWARD_ONLY", "mergeable": False}


def _routes(detail: dict[str, Any] | None = None) -> dict[str, tuple[int, Any]]:
    return {
        DETAIL_API: (200, detail or DETAIL_DOC),
        # An unpinned fetch freezes on the SHA ``detail`` reported, so the
        # revision-scoped reads name it, never the floating ``current``.
        f"{REST}/revisions/{CURRENT_SHA}/files": (200, FILES_DOC),
        f"{REST}/revisions/{CURRENT_SHA}/patch": (200, PATCH_TEXT),
        f"{REST}/comments": (200, COMMENTS_DOC),
        f"{REST}/robotcomments": (200, ROBOT_COMMENTS_DOC),
        f"{REST}/revisions/current/mergeable": (200, MERGEABLE_DOC),
    }


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for start in range(0, len(self._body), size):
            yield self._body[start : start + size]


class _Response:
    def __init__(self, status: int, document: Any) -> None:
        self.status = status
        if isinstance(document, str):
            # The patch endpoint answers base64 text, no XSSI guard.
            body = base64.b64encode(document.encode("utf-8"))
        else:
            # Every JSON answer carries Gerrit's XSSI guard.
            body = b")]}'\n" + json.dumps(document).encode("utf-8")
        self.content = _Content(body)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Instance:
    """Records every request a fetch makes and answers from canned documents."""

    def __init__(self, routes: dict[str, tuple[int, Any]]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []
        self.on_get: Any = None

    def session(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any):
        instance = self
        instance_headers = headers

        class _Session:
            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *exc: object) -> bool:
                return False

            def get(
                self,
                url: str,
                *,
                allow_redirects: bool = True,
                headers: dict[str, str] | None = None,
            ) -> _Response:
                sent = {**(instance_headers or {}), **(headers or {})}
                instance.requests.append(
                    {"url": url, "headers": sent, "redirects": allow_redirects}
                )
                if instance.on_get is not None:
                    instance.on_get(url)
                status, document = instance.routes.get(url, (404, {}))
                # The real patch endpoint answers a JSON string, not base64, to a
                # JSON Accept: a provider asking wrongly gets an undecodable body.
                if isinstance(document, str) and sent.get("Accept") != "text/plain":
                    return _Response(status, {"not": "base64"})
                return _Response(status, document)

        return _Session()


@pytest.fixture
def instance(monkeypatch):
    stub = _Instance(_routes())
    monkeypatch.setattr(gerrit, "ClientSession", stub.session)
    return stub


@pytest.fixture
def vault(monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(gerrit, "_vault_secret", lambda name: secrets.get(name, ""))
    return secrets


def _app() -> web.Application:
    @web.middleware
    async def owner(request, handler):
        request["user"] = "U_OWNER"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    state = MagicMock()
    state.owner_id = "U_OWNER"
    app["state"] = state
    app.router.add_post("/api/source/pull-request", source.api_pull_request_source)
    app.router.add_post("/api/source/pull-request/checks", source.api_pull_request_checks)
    app.router.add_post("/api/source/pull-request/comment", source.api_pull_request_comment)
    app.router.add_post("/api/source/pull-request/ready", source.api_pull_request_ready)
    return app


# --- config ------------------------------------------------------------------


def test_gerrit_hosts_defaults_to_empty() -> None:
    assert config_loader._build_dashboard_config(set(), {}).gerrit_hosts == []


def test_entries_are_normalized_like_gitlab_hosts_and_keep_their_port() -> None:
    raw = [
        " Gerrit.Acme.Example:8443 ",
        "gerrit.acme.example:8443",  # duplicate after normalization
        "review.acme.example:443",  # default port dropped
        "https://scheme.acme.example",
        "*.acme.example",
        "path.acme.example/r",
        42,
    ]
    assert config_sections._coerce_gerrit_hosts(raw) == [
        "gerrit.acme.example:8443",
        "review.acme.example",
    ]
    assert config_sections._coerce_gerrit_hosts("gerrit.acme.example") == []


def test_a_host_another_provider_list_claims_is_refused(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        dashboard = config_loader._build_dashboard_config(
            set(),
            {
                "gerrit_hosts": ["git.acme.example", "bb.acme.example", HOST],
                "gitlab_hosts": ["git.acme.example"],
                "bitbucket_server_hosts": ["bb.acme.example"],
            },
        )
    assert dashboard.gerrit_hosts == [HOST]
    # The claiming lists are untouched by the refusal.
    assert dashboard.gitlab_hosts == ["git.acme.example"]
    assert dashboard.bitbucket_server_hosts == ["bb.acme.example"]
    refused = [record.args for record in caplog.records if record.levelno == logging.WARNING]
    assert ("git.acme.example",) in refused and ("bb.acme.example",) in refused


# --- URL validation ----------------------------------------------------------


def test_an_unlisted_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(CHANGE_URL)


@pytest.mark.usefixtures("listed")
def test_a_listed_host_parses_and_the_ui_suffix_is_dropped() -> None:
    ref = links.parse_source_url(f"{CHANGE_URL}/2/src/cache.py")
    assert (ref.provider, ref.host, ref.owner, ref.repo, ref.number, ref.kind) == (
        "gerrit",
        HOST,
        "",
        "platform/hal",
        466698,
        "change",
    )
    # The patchset pin survives in the canonical URL; the file suffix does not.
    assert ref.url == f"{CHANGE_URL}/2"
    assert ref.project == "/r"
    assert links.parse_source_url(ref.url) == ref
    # Comment anchors, revision ranges and a trailing slash are UI state.
    assert links.parse_source_url(f"{CHANGE_URL}/comment/abc/").url == CHANGE_URL
    assert links.parse_source_url(f"{CHANGE_URL}/1..2").url == CHANGE_URL
    assert links.parse_source_url(f"{CHANGE_URL}/").url == CHANGE_URL


@pytest.mark.usefixtures("listed")
def test_a_pinned_and_an_unpinned_link_share_one_identity() -> None:
    pinned = links.parse_source_url(f"{CHANGE_URL}/2")
    unpinned = links.parse_source_url(CHANGE_URL)
    assert pinned.url != unpinned.url
    assert pinned.identity == unpinned.identity


@pytest.mark.usefixtures("listed")
@pytest.mark.parametrize(
    "url",
    [
        "https://gerrit.acme.example/r/c/platform/hal/+/1",  # port missing
        f"https://{HOST}.evil.example/c/p/+/1",
        f"https://{HOST}/466698",  # the numeric short form carries no project
        f"https://{HOST}/c/466698",
        f"https://{HOST}/#/c/466698/",
        f"https://{HOST}/q/466698",
        f"https://{HOST}/c/+/1",
        f"https://{HOST}/c/p/+/",
        f"https://{HOST}/c/p/+/0",
        f"https://{HOST}/c/p/+/\u0661",
        f"https://{HOST}/c/p/+/1234567890123456",
        f"https://{HOST}/../c/p/+/1",
        f"https://{HOST}/c/../+/1",
        f"https://{HOST}/c/p%2Fq/+/1",
        f"http://{HOST}/c/p/+/1",
        f"https://user@{HOST}/c/p/+/1",
    ],
)
def test_lookalike_and_malformed_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


@pytest.mark.usefixtures("listed")
def test_a_context_path_is_bounded() -> None:
    deep = "/".join(f"d{index}" for index in range(9))
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{HOST}/{deep}/c/p/+/1")
    ref = links.parse_source_url(f"https://{HOST}/a/b/c/p/+/1")
    assert (ref.project, ref.repo) == ("/a/b", "p")
    # The FIRST ``c`` opens the change route, so a project may contain one.
    assert links.parse_source_url(f"https://{HOST}/c/tools/c/lib/+/1").repo == "tools/c/lib"


def test_the_change_marker_is_offered_only_while_a_host_is_listed(monkeypatch) -> None:
    assert "/+/" not in source.source_link_path_markers()
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))
    assert "/+/" in source.source_link_path_markers()


def test_an_edition_cannot_register_the_core_id() -> None:
    class Shadow:
        id = "gerrit"

    with pytest.raises(ValueError, match="built-in"):
        source.register_source_provider(Shadow())  # type: ignore[arg-type]


@pytest.mark.usefixtures("listed")
def test_a_ref_never_falls_through_to_a_builtin_branch(monkeypatch) -> None:
    ref = links.parse_source_url(CHANGE_URL)
    assert plugins._plugin_for_change(ref) is gerrit.PROVIDER
    monkeypatch.setattr(plugins, "_CORE_SEAM_PROVIDERS", {})
    with pytest.raises(SourceProviderError, match="not loaded"):
        plugins._plugin_for_change(ref)


# --- the source API against the stubbed instance ----------------------------


@pytest.mark.asyncio
async def test_the_panel_reads_a_listed_instance_with_its_own_credential(
    config_file, instance, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": f"{CHANGE_URL}/"})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()

    assert payload["provider"] == "gerrit"
    assert payload["url"] == CHANGE_URL
    assert (payload["number"], payload["title"], payload["state"], payload["draft"]) == (
        466698,
        "hal eth [Bug Fix]: drop the phantom adapter",
        "OPEN",
        False,
    )
    assert payload["description"] == COMMIT_MESSAGE
    assert (payload["headBranch"], payload["baseBranch"], payload["headSha"]) == (
        "refs/changes/98/466698/2",
        "dev",
        CURRENT_SHA,
    )
    assert payload["author"] == "Dev One"
    assert payload["updatedAt"] == "2026-01-03T12:00:00Z"
    assert payload["mergedAt"] == ""
    # Not submittable and the fast-forward probe said no, without a conflicts
    # list: the change is not a fast-forward of dev (a chain child, or dev moved
    # on). A rebase is the only way forward, so it is GitLab's need_rebase, not
    # a conflict.
    assert (payload["mergeable"], payload["mergeStateStatus"]) == ("unknown", "need_rebase")
    assert payload["commits"] == [
        {
            "sha": CURRENT_SHA,
            "title": "hal eth [Bug Fix]: drop the phantom adapter",
            "body": "It never existed.\n\ndevices: al13ml",
            "author": "Dev One",
            "date": "2026-01-02T00:00:00Z",
            "url": f"{CHANGE_URL}/2",
        }
    ]
    files = {row["path"]: row for row in payload["files"]}
    # The magic /COMMIT_MSG entry is not a file in the tree.
    assert set(files) == {"src/cache.py", "src/app.py", "src/old.py"}
    assert files["src/cache.py"]["status"] == "added"
    assert files["src/cache.py"]["patch"] == "@@ -0,0 +1,2 @@\n+CACHE = {}\n+"
    assert files["src/app.py"]["status"] == "modified"
    assert files["src/app.py"]["patch"] == (
        "@@ -1,2 +1,2 @@\n import os\n-cache = None\n+from cache import CACHE"
    )
    # The last hunk does not swallow the format-patch signature.
    assert files["src/old.py"]["patch"] == "@@ -1,3 +0,0 @@\n-a\n-b\n-c"
    assert (payload["additions"], payload["deletions"], payload["changedFiles"]) == (3, 4, 3)
    comments = payload["comments"]
    assert [
        (row["kind"], row["author"], row["threadId"], row["resolved"], row["state"])
        for row in comments
    ] == [
        ("inline", "Reviewer One", "c-root", True, ""),
        ("inline", "Dev One", "c-root", True, ""),
        ("inline", "AutoSDE", "r-1", False, ""),
        ("comment", "Reviewer One", "", False, ""),
        ("review", "Reviewer One", "", False, "APPROVED"),
        ("comment", "Reviewer Two", "c-level", False, ""),
        ("review", "Reviewer Two", "", False, "CHANGES_REQUESTED"),
    ]
    assert comments[0]["path"] == "src/cache.py" and comments[0]["line"] == 1
    # The thread's state is its LATEST comment's flag, not the root's.
    assert comments[0]["resolvable"] is True
    assert comments[3]["body"] == "Patch Set 2: Code-Review+2\n\nLooks good."
    assert comments[4]["body"] == "Code-Review +2"
    assert [(row["name"], row["bucket"], row["url"]) for row in payload["checks"]] == [
        ("Verified", "passed", "https://ci.acme.example/job/core/55/"),
        ("Post-Sil-Verified", "failed", "https://ci.acme.example/job/post-sil/9/"),
        ("CI build", "pending", "https://ci.acme.example/job/graviton/3/"),
    ]
    assert payload["partialSections"] == []

    # Every request went to the listed instance's authenticated tree only,
    # carried its own credential, and followed no redirect.
    assert instance.requests
    for request in instance.requests:
        assert request["url"].startswith(f"{REST}")
        assert request["headers"]["Authorization"] == BASIC
        assert request["redirects"] is False
    # Only the patch read asks for text; every JSON read keeps the session's Accept.
    accepts = {
        request["url"].rsplit("/", 1)[-1]: request["headers"]["Accept"]
        for request in instance.requests
    }
    assert accepts["patch"] == "text/plain"
    assert accepts["files"] == "application/json"


@pytest.mark.asyncio
async def test_a_pinned_patchset_reads_that_patchset(config_file, instance, vault) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[f"{REST}/revisions/1/files"] = (
        200,
        {"src/app.py": {"lines_inserted": 1, "lines_deleted": 1}},
    )
    instance.routes[f"{REST}/revisions/1/patch"] = (200, PATCH_TEXT)
    instance.routes[f"{REST}/revisions/1/commit"] = (
        200,
        {
            "commit": PREVIOUS_SHA,
            "subject": "hal eth [Bug Fix]: drop the phantom adapter",
            "message": "hal eth [Bug Fix]: drop the phantom adapter\n\nFirst cut.",
            "author": {"name": "Dev One", "date": _stamp(1)},
        },
    )
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": f"{CHANGE_URL}/1"})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()
    assert payload["url"] == f"{CHANGE_URL}/1"
    assert payload["headSha"] == PREVIOUS_SHA
    assert payload["headBranch"] == "refs/changes/98/466698/1"
    assert payload["commits"][0]["url"] == f"{CHANGE_URL}/1"
    assert payload["commits"][0]["body"] == "First cut."
    assert [row["path"] for row in payload["files"]] == ["src/app.py"]
    requested = {request["url"] for request in instance.requests}
    assert f"{REST}/revisions/1/files" in requested
    assert f"{REST}/revisions/current/files" not in requested
    assert f"{REST}/revisions/{CURRENT_SHA}/files" not in requested
    # Mergeability is a property of the change, so it is always read on current.
    assert f"{REST}/revisions/current/mergeable" in requested


@pytest.mark.asyncio
async def test_an_unpinned_fetch_freezes_on_the_sha_detail_reported(
    config_file, instance, vault
) -> None:
    # A patchset uploaded between ``detail`` and the files/patch reads must not
    # pair the earlier patchset's commit metadata with the newer one's diff:
    # once detail has named the current SHA, every revision-scoped read uses it.
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[f"{REST}/revisions/current/files"] = (200, {"src/newer.py": {}})
    instance.routes[f"{REST}/revisions/current/patch"] = (200, PATCH_TEXT)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()
    assert payload["headSha"] == CURRENT_SHA
    assert "src/newer.py" not in [row["path"] for row in payload["files"]]
    requested = [request["url"] for request in instance.requests]
    assert f"{REST}/revisions/{CURRENT_SHA}/files" in requested
    assert f"{REST}/revisions/{CURRENT_SHA}/patch" in requested
    assert f"{REST}/revisions/current/files" not in requested
    # Mergeability is a property of the change, so it stays on ``current``.
    assert f"{REST}/revisions/current/mergeable" in requested


@pytest.mark.asyncio
async def test_a_pinned_patchset_whose_commit_read_failed_keeps_its_own_identity(
    config_file, instance, vault
) -> None:
    # Patchset 1's files and patch succeed but its commit read fails: the panel
    # must not show patchset 1's diff under the CURRENT patchset's sha, message
    # and commit link. The pinned identity stays, with the metadata empty under
    # the partial "commits" mark.
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[f"{REST}/revisions/1/files"] = (
        200,
        {"src/app.py": {"lines_inserted": 1, "lines_deleted": 1}},
    )
    instance.routes[f"{REST}/revisions/1/patch"] = (200, PATCH_TEXT)
    instance.routes[f"{REST}/revisions/1/commit"] = (503, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": f"{CHANGE_URL}/1"})
        assert resp.status == 200, await resp.text()
        payload = await resp.json()
    assert "commits" in payload["partialSections"]
    assert payload["headSha"] == ""
    assert payload["headSha"] != CURRENT_SHA
    assert payload["headBranch"] == "refs/changes/98/466698/1"
    assert payload["commits"] == []
    assert payload["description"] == ""
    assert [row["path"] for row in payload["files"]] == ["src/app.py"]


def test_patches_split_on_git_quoted_headers_and_decode_the_path() -> None:
    # Git C-quotes a path with a byte outside printable ASCII (or a space, quote
    # or backslash), each side on its own. A quoted header is a section boundary
    # like any other, and its decoded destination is the key the files response
    # uses; otherwise the file shows no diff and its hunks leak into the file
    # before it.
    text = (
        "---\n"
        "diff --git a/plain.txt b/plain.txt\n"
        "--- a/plain.txt\n"
        "+++ b/plain.txt\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        'diff --git "a/caf\\303\\251 \\"x\\".txt" "b/caf\\303\\251 \\"x\\".txt"\n'
        '--- "a/caf\\303\\251 \\"x\\".txt"\n'
        '+++ "b/caf\\303\\251 \\"x\\".txt"\n'
        "@@ -1 +1 @@\n"
        "-c\n"
        "+d\n"
        'diff --git a/old.txt "b/new\\ttab.txt"\n'
        "similarity index 90%\n"
        "rename from old.txt\n"
        'rename to "new\\ttab.txt"\n'
        "@@ -1 +1 @@\n"
        "-e\n"
        "+f\n"
    )
    patches = gerrit._patches(text)
    assert patches == {
        "plain.txt": "@@ -1 +1 @@\n-a\n+b",
        'café "x".txt': "@@ -1 +1 @@\n-c\n+d",
        "new\ttab.txt": "@@ -1 +1 @@\n-e\n+f",
    }


def test_patches_read_a_path_holding_b_slash_from_its_plus_plus_plus_line() -> None:
    # Git leaves a space unquoted, so ``plan b/notes.txt`` gives a header that
    # splits at the first `` b/``; the hunks would land under a wrong key and
    # the file would show no diff. A deletion has only its ``---`` side.
    text = (
        "diff --git a/plan b/notes.txt b/plan b/notes.txt\n"
        "--- a/plan b/notes.txt\t\n"
        "+++ b/plan b/notes.txt\t\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/old b/gone.txt b/old b/gone.txt\n"
        "deleted file mode 100644\n"
        "--- a/old b/gone.txt\t\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-c\n"
    )
    assert gerrit._patches(text) == {
        "plan b/notes.txt": "@@ -1 +1 @@\n-a\n+b",
        "old b/gone.txt": "@@ -1 +0,0 @@\n-c",
    }


@pytest.mark.asyncio
# Gerrit answers an anonymous read of a change the anonymous user may not see
# with 404 (observed on Gerrit 3.10), not 401, so both must name the secret.
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_without_a_credential_a_denied_read_names_the_secret_to_store(
    config_file, instance, vault, status
) -> None:
    anonymous = f"https://{HOST}/r/changes/platform%2Fhal~466698/detail?" + "&".join(
        f"o={o}" for o in gerrit._DETAIL_OPTIONS
    )
    instance.routes[anonymous] = (status, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status == 503
        error = (await resp.json())["error"]
    assert f"Settings > Secrets as {gerrit.GLOBAL_CREDENTIAL_NAME}." in error
    assert "<username>:<HTTP password>" in error
    assert gerrit.host_credential_name(HOST) not in error
    # An anonymous read uses the bare tree: ``/a/`` would refuse it outright.
    assert [request["url"] for request in instance.requests] == [anonymous]
    assert "Authorization" not in instance.requests[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_credential_names_the_secret_to_replace(
    config_file, instance, vault, status
) -> None:
    # The credential was read from the shared entry (lone host, no per-host
    # entry), so that is the entry the guidance names.
    vault[gerrit.GLOBAL_CREDENTIAL_NAME] = CREDENTIAL
    instance.routes[DETAIL_API] = (status, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        error = (await resp.json())["error"]
    assert f"Replace {gerrit.GLOBAL_CREDENTIAL_NAME} in Settings > Secrets" in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ("just-a-password", "no ':' separating"),
        (":password-only", "nothing before the ':'"),
        ("dev1:", "nothing after the ':'"),
    ],
)
async def test_a_malformed_credential_is_refused_before_any_request_and_says_why(
    config_file, instance, vault, stored, reason
) -> None:
    vault[gerrit.host_credential_name(HOST)] = stored
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status == 503
        error = (await resp.json())["error"]
    assert reason in error and "<username>:<HTTP password>" in error
    # The stored value itself is never echoed back.
    assert stored not in error
    assert instance.requests == []


@pytest.mark.asyncio
async def test_with_a_credential_a_missing_change_is_reported_as_such(
    config_file, instance, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[DETAIL_API] = (404, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status == 503
        assert "no such change" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_a_redirect_is_reported_not_followed(config_file, instance, vault) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[DETAIL_API] = (302, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status == 503
        assert "HTTP 302" in (await resp.json())["error"]
    assert [request["url"] for request in instance.requests] == [DETAIL_API]


@pytest.mark.asyncio
async def test_a_host_removed_mid_fetch_receives_no_further_request(
    config_file, instance, vault, monkeypatch
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL

    def revoke_on_primary_read(url: str) -> None:
        if url == DETAIL_API:
            monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset())

    instance.on_get = revoke_on_primary_read
    async with TestClient(TestServer(_app())) as client:
        await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
    assert [request["url"] for request in instance.requests] == [DETAIL_API]


@pytest.mark.asyncio
async def test_a_failed_secondary_read_marks_its_section_partial(
    config_file, instance, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    del instance.routes[f"{REST}/comments"]
    del instance.routes[f"{REST}/revisions/current/mergeable"]
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        payload = await resp.json()
    assert resp.status == 200
    # Robot threads, messages and verdicts still render; the human threads are gone.
    assert [row["id"] for row in payload["comments"] if row["kind"] == "inline"] == ["r-1"]
    assert payload["partialSections"] == ["review discussions"]
    # An unknown merge state is a banner, never a partial section.
    assert (payload["mergeable"], payload["mergeStateStatus"]) == ("unknown", "blocked")


@pytest.mark.asyncio
async def test_a_gerrit_without_robot_comments_is_complete_not_partial(
    config_file, instance, vault
) -> None:
    """Gerrit 3.13 removed robot comments, so ``/robotcomments`` is a 404 there.

    That is the whole discussion, not a degraded read: the human threads render
    and no "partial" banner is raised for a feature the server does not have.
    """
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    del instance.routes[f"{REST}/robotcomments"]
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        payload = await resp.json()
    assert resp.status == 200
    assert [row["id"] for row in payload["comments"] if row["kind"] == "inline"] == [
        "c-root",
        "c-reply",
    ]
    assert payload["partialSections"] == []


@pytest.mark.asyncio
async def test_an_anonymous_read_of_a_visible_change_survives_a_missing_endpoint(
    config_file, instance
) -> None:
    """Once ``detail`` has answered anonymously, a 404 on a secondary leg is that
    leg's absence, not "store a credential": a public change on Gerrit 3.13 (no
    ``/robotcomments``) renders rather than failing the whole panel."""
    anonymous = {
        url.replace(f"https://{HOST}/r/a/", f"https://{HOST}/r/"): value
        for url, value in instance.routes.items()
    }
    del anonymous[f"https://{HOST}/r/changes/platform%2Fhal~466698/robotcomments"]
    instance.routes.clear()
    instance.routes.update(anonymous)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        payload = await resp.json()
    assert resp.status == 200
    assert payload["partialSections"] == []
    assert [row["id"] for row in payload["comments"] if row["kind"] == "inline"] == [
        "c-root",
        "c-reply",
    ]
    assert all("Authorization" not in request["headers"] for request in instance.requests)


@pytest.mark.asyncio
async def test_thread_links_point_at_the_unpinned_change(config_file, instance, vault) -> None:
    """``/c/<p>/+/<n>/comment/<id>/`` is a comment link only without a patchset in
    the path, so a pinned view still links threads through the change URL."""
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    instance.routes[f"{REST}/revisions/1/files"] = (200, FILES_DOC)
    instance.routes[f"{REST}/revisions/1/patch"] = (200, PATCH_TEXT)
    instance.routes[f"{REST}/revisions/1/commit"] = (200, {"commit": "a" * 40})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": f"{CHANGE_URL}/1"})
        payload = await resp.json()
    assert resp.status == 200
    inline = [row for row in payload["comments"] if row["kind"] == "inline"]
    assert inline and all(row["url"] == f"{CHANGE_URL}/comment/{row['id']}/" for row in inline)


@pytest.mark.asyncio
async def test_a_discussion_the_comment_cap_drops_marks_it_partial(
    config_file, instance, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    filler = [
        {
            "id": f"c-{index}",
            "author": _account("Dev", 1),
            "updated": _stamp(2),
            "message": "note",
            "unresolved": False,
        }
        for index in range(gerrit._MAX_COMMENTS + 1)
    ]
    instance.routes[f"{REST}/comments"] = (200, {"src/app.py": filler})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        payload = await resp.json()
    assert resp.status == 200
    human = [row for row in payload["comments"] if row["id"].startswith("c-")]
    assert len(human) == gerrit._MAX_COMMENTS
    assert payload["partialSections"] == ["review discussions"]


@pytest.mark.asyncio
async def test_a_merged_change_reports_when_and_no_merge_state(
    config_file, instance, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    merged = {**DETAIL_DOC, "status": "MERGED", "submitted": _stamp(4), "submittable": True}
    instance.routes[DETAIL_API] = (200, merged)
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        payload = await resp.json()
    assert (payload["state"], payload["mergedAt"]) == ("MERGED", "2026-01-04T00:00:00Z")
    assert (payload["mergeable"], payload["mergeStateStatus"]) == ("", "")
    # A finished change's mergeability is not asked for.
    assert f"{REST}/revisions/current/mergeable" not in {r["url"] for r in instance.requests}


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ({"work_in_progress": True, "submittable": False}, ("unknown", "draft")),
        ({"submittable": True}, ("unknown", "clean")),
    ],
)
def test_merge_state_prefers_wip_then_submittable(detail, expected) -> None:
    assert gerrit._merge_state({**DETAIL_DOC, **detail}, MERGEABLE_DOC) == expected
    assert gerrit._merge_state({**DETAIL_DOC, "submittable": False}, {"mergeable": True}) == (
        "mergeable",
        "blocked",
    )


@pytest.mark.parametrize(
    ("mergeable", "expected"),
    [
        # Fast-forward-only and no conflicts list: not a fast-forward of the
        # target (a chain child, or the branch moved on). Only a rebase helps.
        ({"submit_type": "FAST_FORWARD_ONLY", "mergeable": False}, ("unknown", "need_rebase")),
        # Gerrit named the conflicting paths: a real conflict, whatever the submit type.
        (
            {"submit_type": "FAST_FORWARD_ONLY", "mergeable": False, "conflicts": ["a.c"]},
            ("conflicting", "dirty"),
        ),
        # A merging submit type attempted the merge and it failed: a conflict.
        ({"submit_type": "MERGE_IF_NECESSARY", "mergeable": False}, ("conflicting", "dirty")),
    ],
)
def test_merge_state_tells_a_needed_rebase_from_a_conflict(mergeable, expected) -> None:
    assert gerrit._merge_state({**DETAIL_DOC, "submittable": False}, mergeable) == expected


@pytest.mark.asyncio
async def test_the_checks_tab_reads_the_label_votes(config_file, instance, vault) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request/checks", json={"url": CHANGE_URL})
        assert resp.status == 200
        checks = (await resp.json())["checks"]
    assert [row["bucket"] for row in checks] == ["passed", "failed", "pending"]
    # One read: the votes travel in the change detail.
    assert [request["url"] for request in instance.requests] == [DETAIL_API]


def test_only_the_current_patchset_builds_count_as_pending() -> None:
    # Patchset 1's lone "Build Started" (m0) must not leave a phantom pending row.
    settled = {
        **DETAIL_DOC,
        "messages": [m for m in DETAIL_DOC["messages"] if m["id"] != "m6"],
    }
    assert [row["bucket"] for row in gerrit._check_rows(settled)] == ["passed", "failed"]


def test_the_sign_of_a_verified_vote_is_the_verdict_not_only_the_extremes() -> None:
    # On a -2..+2 ``Verified`` label (OpenDev's Zuul votes -1 from check and -2
    # from gate) a -1 is filed as ``disliked`` and a +1 as ``recommended``;
    # Gerrit reserves ``rejected``/``approved`` for the extremes. Both are
    # verdicts, so neither may read as "no vote" and leave the Checks tab empty.
    wide = {
        **DETAIL_DOC,
        "labels": {
            "Verified": {"disliked": _account("Zuul", 3002), "all": []},
            "Post-Sil-Verified": {"recommended": _account("Jenkins", 3001), "all": []},
            "Code-Review": DETAIL_DOC["labels"]["Code-Review"],
        },
    }
    rows = gerrit._check_rows(wide)
    assert {row["name"]: row["bucket"] for row in rows if row["bucket"] != "pending"} == {
        "Verified": "failed",
        "Post-Sil-Verified": "passed",
    }


def test_a_running_build_is_one_whose_url_no_later_message_names() -> None:
    # One server starts two jobs and reports both in ONE verdict message, so
    # counting starts against verdicts would leave a phantom running build.
    messages = [m for m in DETAIL_DOC["messages"] if m["_revision_number"] == 2]
    running = gerrit._running_builds(messages)
    assert [url for url, _date in running] == ["https://ci.acme.example/job/graviton/3/"]
    # A re-triggered job runs again until a message after the re-trigger names it.
    retrigger = {
        "author": _account("Jenkins", 3001),
        "tag": "autogenerated:jenkins-gerrit-trigger",
        "message": "Patch Set 2:\n\nBuild Started https://ci.acme.example/job/core/55/",
        "date": _stamp(3, 2),
    }
    assert len(gerrit._running_builds([*messages, retrigger])) == 2
    closed = {
        "author": _account("Jenkins", 3001),
        "message": "https://ci.acme.example/job/core/55/ : SUCCESS",
        "date": _stamp(3, 3),
    }
    assert len(gerrit._running_builds([*messages, retrigger, closed])) == 1


def test_a_reviewer_quoting_a_build_url_does_not_finish_the_build() -> None:
    # Jenkins starts a job; a reviewer discusses it by url while it still runs.
    # Only a CI report (the starting account, or a server-tagged message) ends
    # it, or the pending row would vanish and a leftover vote would read as
    # "All checks passed" with a build in flight.
    started = next(m for m in DETAIL_DOC["messages"] if m["id"] == "m6")
    human = {
        "author": _account("Reviewer One", 2001),
        "message": "Patch Set 2:\n\nhttps://ci.acme.example/job/graviton/3/ looks stuck?",
        "date": _stamp(3, 4),
    }
    assert len(gerrit._running_builds([started, human])) == 1
    report = {
        "author": _account("Jenkins", 3001),
        "message": "Patch Set 2: Graviton-Verified+1\n\nhttps://ci.acme.example/job/graviton/3/ : SUCCESS",
        "date": _stamp(3, 5),
    }
    assert gerrit._running_builds([started, human, report]) == []
    tagged = {
        "author": _account("Zuul", 3002),
        "tag": "autogenerated:zuul",
        "message": "https://ci.acme.example/job/graviton/3/ : SUCCESS",
        "date": _stamp(3, 5),
    }
    assert gerrit._running_builds([started, human, tagged]) == []


def test_a_reviewer_quoting_a_build_start_does_not_start_a_build() -> None:
    # CI has finished; a reviewer quotes the old notice. Counted as a start, it
    # would add a pending row that no report ever closes and pin the chip at
    # "running".
    messages = [m for m in DETAIL_DOC["messages"] if m["_revision_number"] == 2]
    before = gerrit._running_builds(messages)
    quote = {
        "author": _account("Reviewer One", 2001),
        "message": "The old log says Build Started https://ci.acme.example/job/core/55/ but that run finished.",
        "date": _stamp(3, 6),
    }
    assert gerrit._running_builds([*messages, quote]) == before


def test_a_human_reply_to_a_robot_comment_joins_and_resolves_its_thread(listed) -> None:
    # The reviewer's reply comes from ``/comments`` with ``in_reply_to`` naming a
    # ``/robotcomments`` id. Threaded apart, the reply would be a root of its own
    # and the finding would stay unresolved for every viewer.
    reply = {
        "id": "c-fixes-r1",
        "author": _account("Dev One", 1001),
        "patch_set": 2,
        "line": 2,
        "in_reply_to": "r-1",
        "updated": _stamp(2, 9),
        "message": "Done.",
        "unresolved": False,
    }
    human = {**COMMENTS_DOC, "src/app.py": [reply]}
    # ``_thread_rows`` reads only the instance's web root and the ref off the client.
    client = SimpleNamespace(web=f"https://{HOST}/r", ref=links.parse_source_url(CHANGE_URL))
    rows = gerrit._thread_rows(client, human, ROBOT_COMMENTS_DOC, [])
    by_id = {row["id"]: row for row in rows}
    assert by_id["c-fixes-r1"]["threadId"] == "r-1"
    assert by_id["r-1"]["resolved"] is True and by_id["c-fixes-r1"]["resolved"] is True
    assert by_id["r-1"]["author"] == "AutoSDE"
    assert by_id["c-fixes-r1"]["author"] == "Dev One"


def test_a_deleted_dash_line_is_never_read_as_the_patch_signature() -> None:
    # A Markdown file losing an empty bullet deletes a line that is exactly
    # ``- ``, which spells the format-patch signature. Only the final section's
    # trailing ``-- `` with nothing but the git version after it is one.
    text = (
        "---\n"
        "diff --git a/notes.md b/notes.md\n"
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -1,3 +1,2 @@\n"
        " # Notes\n"
        "-- \n"
        " - kept\n"
        "diff --git a/last.md b/last.md\n"
        "--- a/last.md\n"
        "+++ b/last.md\n"
        "@@ -1,2 +1,1 @@\n"
        "-- \n"
        " text\n"
        "-- \n"
        "2.43.0\n"
    )
    assert gerrit._patches(text) == {
        "notes.md": "@@ -1,3 +1,2 @@\n # Notes\n-- \n - kept",
        "last.md": "@@ -1,2 +1,1 @@\n-- \n text",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("/api/source/pull-request/comment", {"url": CHANGE_URL, "body": "looks good"}),
        ("/api/source/pull-request/ready", {"url": CHANGE_URL}),
    ],
)
async def test_writes_are_refused_as_unsupported(config_file, instance, vault, route, body) -> None:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post(route, json=body)
        assert resp.status == 400
        assert "gerrit" in (await resp.json())["error"]
    assert instance.requests == []


# --- credential routing -------------------------------------------------------


def test_the_single_host_credential_serves_only_a_lone_listed_host(monkeypatch, vault) -> None:
    vault[gerrit.GLOBAL_CREDENTIAL_NAME] = CREDENTIAL
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))
    assert gerrit._resolve_credential(HOST) == (CREDENTIAL, gerrit.GLOBAL_CREDENTIAL_NAME)
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST, "other.acme.example"}))
    assert gerrit._resolve_credential(HOST) == ("", gerrit.host_credential_name(HOST))
    vault[gerrit.host_credential_name(HOST)] = "dev1:per-host"
    assert gerrit._resolve_credential(HOST) == ("dev1:per-host", gerrit.host_credential_name(HOST))
    # A lone host served by its per-host entry: that entry wins, and it is the
    # one named back, since the global entry is not what was read.
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))
    assert gerrit._resolve_credential(HOST) == ("dev1:per-host", gerrit.host_credential_name(HOST))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "rejected"])
async def test_replacement_guidance_names_the_entry_the_credential_was_read_from(
    config_file, instance, vault, failure
) -> None:
    # One listed host, and the credential came from the PER-HOST entry (the
    # managed Settings row writes that one). Telling the operator to replace
    # the shared entry would send them to an entry the provider never read.
    per_host = gerrit.host_credential_name(HOST)
    vault[per_host] = "no-colon-here" if failure == "malformed" else CREDENTIAL
    vault[gerrit.GLOBAL_CREDENTIAL_NAME] = "stale:ignored"
    if failure == "rejected":
        instance.routes[DETAIL_API] = (401, {})
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/source/pull-request", json={"url": CHANGE_URL})
        assert resp.status != 200
        message = (await resp.json())["error"]
    assert f"Replace {per_host} in Settings > Secrets" in message
    assert f"Replace {gerrit.GLOBAL_CREDENTIAL_NAME} " not in message


def test_with_one_host_the_setup_guidance_names_only_the_shared_secret(monkeypatch) -> None:
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))
    guidance = gerrit._setup_guidance(HOST)
    assert f"Settings > Secrets as {gerrit.GLOBAL_CREDENTIAL_NAME}." in guidance
    assert gerrit.host_credential_name(HOST) not in guidance


def test_with_two_hosts_the_setup_guidance_says_the_shared_secret_stopped_applying(
    monkeypatch,
) -> None:
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST, "other.acme.example"}))
    guidance = gerrit._setup_guidance(HOST)
    assert gerrit.host_credential_name(HOST) in guidance
    assert (
        f"{gerrit.GLOBAL_CREDENTIAL_NAME} stops applying once a second host is listed" in guidance
    )


@pytest.mark.asyncio
async def test_a_host_removed_after_parsing_is_not_contacted(monkeypatch, instance, vault) -> None:
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset({HOST}))
    ref = links.parse_source_url(CHANGE_URL)
    monkeypatch.setattr(hosts, "_gerrit_hosts_snapshot", frozenset())
    with pytest.raises(SourceProviderError, match="not listed"):
        await gerrit.PROVIDER.fetch_full(ref)
    assert instance.requests == []


# --- sidebar chip ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        # The fixture has one failed vote and one build still running, so the
        # rollup is "failed" with ``ciPending`` beside it, as for GitHub. An
        # open change also carries the merge pair, re-read on every refresh:
        # the fixture's fast-forward ``false`` is ``need_rebase`` with the
        # mergeable verdict itself unknown, so only the detail is recorded.
        (
            {"status": "NEW"},
            {"state": "open", "ci": "failed", "ciPending": "1", "mergeStateStatus": "need_rebase"},
        ),
        (
            {"status": "NEW", "work_in_progress": True},
            {"state": "draft", "ci": "failed", "ciPending": "1", "mergeStateStatus": "draft"},
        ),
        ({"status": "MERGED"}, {"state": "merged", "ci": "failed", "ciPending": "1"}),
        (
            {"status": "ABANDONED", "work_in_progress": True},
            {"state": "closed", "ci": "failed", "ciPending": "1"},
        ),
    ],
)
async def test_the_chip_reads_lifecycle_and_label_rollup(
    config_file, monkeypatch, vault, detail, expected
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    stub = _Instance(_routes({**DETAIL_DOC, **detail}))
    monkeypatch.setattr(gerrit, "ClientSession", stub.session)
    assert await chip_refresh._fetch_check_status(CHANGE_URL) == expected
    # One detail read, plus the merge pair while the change is open.
    urls = [request["url"] for request in stub.requests]
    assert urls[0] == DETAIL_API
    assert (f"{REST}/revisions/current/mergeable" in urls) == (
        gerrit._lifecycle({**DETAIL_DOC, **detail}) == "OPEN"
    )


@pytest.mark.asyncio
async def test_the_chip_refresh_answers_the_merge_pair_so_keep_known_cannot_pin_a_conflict(
    config_file, monkeypatch, vault
) -> None:
    # The full payload records the merge pair into the chip cache and the chip
    # refresh's keep-known rule carries a recorded pair forward whenever a
    # refresh leaves it out. A refresh that never read mergeability would
    # therefore keep a "conflicting" chip after a rebase cleared it. The read
    # here is what lets the chip follow the server.
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    conflicting = {"mergeable": False, "submit_type": "MERGE_IF_NECESSARY", "conflicts": ["a.py"]}
    stub = _Instance({**_routes(), f"{REST}/revisions/current/mergeable": (200, conflicting)})
    monkeypatch.setattr(gerrit, "ClientSession", stub.session)
    before = await chip_refresh._fetch_check_status(CHANGE_URL)
    assert before["mergeable"] == "conflicting"
    assert before["mergeStateStatus"] == "dirty"
    cleared = {"mergeable": True, "submit_type": "MERGE_IF_NECESSARY"}
    stub.routes[f"{REST}/revisions/current/mergeable"] = (200, cleared)
    after = await chip_refresh._fetch_check_status(CHANGE_URL)
    assert after["mergeable"] == "mergeable"
    assert after.get("mergeStateStatus") != "dirty"
    # A degraded mergeable read records nothing rather than a guess.
    stub.routes[f"{REST}/revisions/current/mergeable"] = (503, {})
    degraded = await chip_refresh._fetch_check_status(CHANGE_URL)
    assert "mergeable" not in degraded and "mergeStateStatus" not in degraded
    assert degraded["state"] == "open"


@pytest.mark.asyncio
async def test_a_chip_with_no_votes_and_no_running_build_shows_no_ci(
    config_file, monkeypatch, vault
) -> None:
    vault[gerrit.host_credential_name(HOST)] = CREDENTIAL
    quiet = {**DETAIL_DOC, "labels": {"Code-Review": DETAIL_DOC["labels"]["Code-Review"]}}
    quiet["messages"] = [m for m in DETAIL_DOC["messages"] if "Build Started" not in m["message"]]
    stub = _Instance(_routes(quiet))
    monkeypatch.setattr(gerrit, "ClientSession", stub.session)
    # No CI glyph; the open change still carries its merge detail.
    assert await chip_refresh._fetch_check_status(CHANGE_URL) == {
        "state": "open",
        "mergeStateStatus": "need_rebase",
    }

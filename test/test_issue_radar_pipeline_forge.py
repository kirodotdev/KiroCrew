"""The triage pipeline serves more than public GitHub, one forge at a time.

A repository's identity is provider + host + owner/repo, but the pipeline's files are
keyed on ``owner/repo`` alone. Serving a second forge is only sound if a same-slug
repository on each forge can never read the other's events, queue shard or issue
cache -- so the files are split BY FORGE. These tests pin that split (fold layer) and
the request rules that select a forge (route layer).

Everything resolves under tmp_path; nothing reads the developer's real data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.issue_radar.backend import pipeline_fold as fold
from kiro_crew.apps.builtins.issue_radar.backend import pipeline_routes as routes

GITLAB = fold.Forge(provider="gitlab", host="code.aws.dev")


# ── fold layer: file naming ──────────────────────────────────────────────────


def test_github_file_names_are_unchanged(tmp_path: Path) -> None:
    """Existing data must stay exactly where the GitHub jobs already write it."""
    assert fold.audit_log_path(tmp_path).name == "gh-autofix-audit.jsonl"
    assert fold.queue_path(repo="o/r", root=tmp_path).name == "gh-autofix-dispatch-queue.o__r.jsonl"
    assert fold.GITHUB.is_github and fold.GITHUB.tag == ""


def test_another_forge_gets_its_own_files(tmp_path: Path) -> None:
    assert (
        fold.audit_log_path(tmp_path, forge=GITLAB).name
        == "autofix-audit.gitlab.code.aws.dev.jsonl"
    )
    assert (
        fold.queue_path(repo="gitlab/proj", root=tmp_path, forge=GITLAB).name
        == "autofix-dispatch-queue.gitlab.code.aws.dev.gitlab__proj.jsonl"
    )


def test_a_valid_gitlab_host_with_a_port_is_accepted() -> None:
    assert routes._valid_gitlab_host("gitlab.example:8443")
    assert routes._valid_gitlab_host("10.0.0.5")  # the allowlist coercer admits bare IPv4
    assert not routes._valid_gitlab_host("gitlab.example:0")
    assert not routes._valid_gitlab_host("gitlab.example:65536")


def test_two_gitlab_paths_cannot_share_a_queue_shard(tmp_path: Path) -> None:
    """``_`` in a nested group path must not alias ``/``: the adversarial case that
    let ``gitlab__proj/sub`` read ``gitlab/proj/sub``'s slots and credit costs."""
    a = fold.queue_path(repo="gitlab/proj/sub", root=tmp_path, forge=GITLAB)
    b = fold.queue_path(repo="gitlab__proj/sub", root=tmp_path, forge=GITLAB)
    c = fold.queue_path(repo="gitlab/proj_sub", root=tmp_path, forge=GITLAB)
    assert len({a, b, c}) == 3
    assert "/" not in a.name and "/" not in b.name


def test_the_forge_slug_is_injective_over_every_short_path() -> None:
    """Exhaustive over the alphabet that matters: every string of length <= 6 drawn
    from a plain letter, ``_``, ``/`` and one escaped byte maps to a distinct slug,
    and no slug carries a path separator. That is the whole collision argument."""
    from itertools import product

    alphabet = ("a", "_", "/", "!")
    seen: dict[str, str] = {}
    for length in range(1, 7):
        for chars in product(alphabet, repeat=length):
            repo = "".join(chars)
            slug = fold._forge_repo_slug(repo)
            assert "/" not in slug, repo
            assert slug not in seen, (repo, seen.get(slug))
            seen[slug] = repo
    # And the GitHub slug is left alone: it is the writers' on-disk contract.
    assert fold._repo_slug("o/re_po") == "o__re_po"


def test_the_forge_tag_cannot_carry_a_path_separator() -> None:
    hostile = fold.Forge(provider="gitlab", host="evil/../x:8443")
    assert "/" not in hostile.tag and ":" not in hostile.tag


def test_a_port_bearing_host_never_shares_a_tag_with_a_hyphenated_host() -> None:
    """``-`` is a legal hostname character, so ``:`` must not be written as one:
    ``gitlab.example:8443`` and ``gitlab.example-8443`` are two allowlistable hosts
    whose audit log and queue shards must stay apart."""
    with_port = fold.Forge(provider="gitlab", host="gitlab.example:8443")
    hyphenated = fold.Forge(provider="gitlab", host="gitlab.example-8443")
    assert with_port.tag != hyphenated.tag
    assert with_port.tag == "gitlab.gitlab.example_8443"
    assert fold.audit_log_path(forge=with_port) != fold.audit_log_path(forge=hyphenated)
    assert fold.queue_path(repo="g/p", forge=with_port) != fold.queue_path(
        repo="g/p", forge=hyphenated
    )


def test_a_gitlab_host_named_github_com_is_not_public_github() -> None:
    """Provider is part of the identity: only provider github AND host github.com is GitHub."""
    assert not fold.Forge(provider="gitlab", host="github.com").is_github


# ── fold layer: no mixing between forges ─────────────────────────────────────


def _event(name: str, issue: int, repo: str = "g/p", **extra: Any) -> dict[str, Any]:
    return {"ts": "2026-10-06T12:00:00Z", "event": name, "issue": issue, "repo": repo, **extra}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _write_issue_cache(base: Path, number: int, title: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / f"issue-{number}.json").write_text(
        json.dumps({"detail": {"title": title, "labels": [], "assignees": []}}), encoding="utf-8"
    )


@pytest.fixture()
def two_forges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Public GitHub and a GitLab host, BOTH with a repository slugged ``g/p``."""
    monkeypatch.setattr(fold, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(fold, "app_dir", lambda _name: tmp_path / "app")
    _write_jsonl(
        fold.audit_log_path(tmp_path),
        [_event("scan", 7), _event("scan", 9), _event("scan", 11)],
    )
    _write_jsonl(fold.audit_log_path(tmp_path, forge=GITLAB), [_event("scan", 9)])
    data = tmp_path / "app" / "data"
    _write_issue_cache(data / "repos" / "g" / "p", 9, "GITHUB title for 9")
    _write_issue_cache(
        data / "@providers" / "gitlab" / "code.aws.dev" / "repos" / "g" / "p", 9, "GITLAB title"
    )
    return tmp_path


def test_a_same_slug_repository_reads_only_its_own_forges_events(two_forges: Path) -> None:
    github = fold.fold_pipeline(repo="g/p", root=two_forges)
    gitlab = fold.fold_pipeline(repo="g/p", root=two_forges, forge=GITLAB)
    scan = {"github": github.steps[0], "gitlab": gitlab.steps[0]}
    assert scan["github"].key == "scan" and scan["gitlab"].key == "scan"
    assert scan["github"].distinct_entered == 3
    assert scan["gitlab"].distinct_entered == 1


def test_a_forge_with_no_log_is_empty_not_github(two_forges: Path) -> None:
    other = fold.Forge(provider="gitlab", host="other.example.com")
    result = fold.fold_pipeline(repo="g/p", root=two_forges, forge=other)
    assert result.total_events == 0
    assert all(step.entered == 0 for step in result.steps)


def test_the_step_list_takes_its_title_from_its_own_forges_cache(two_forges: Path) -> None:
    rows = fold.list_step_items("scan", owner="g", repo="p", root=two_forges, forge=GITLAB)
    assert [r.number for r in rows] == [9]
    assert rows[0].to_dict()["title"] == "GITLAB title"
    github_rows = fold.list_step_items("scan", owner="g", repo="p", root=two_forges)
    assert {r.number: r.to_dict()["title"] for r in github_rows}[9] == "GITHUB title for 9"


def test_issue_cache_lookup_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fold promises to write nothing, including directories."""
    monkeypatch.setattr(fold, "app_dir", lambda _name: tmp_path / "app")
    path = fold._issue_cache_dir("g", "p", GITLAB)
    assert (
        path
        == tmp_path
        / "app"
        / "data"
        / "@providers"
        / "gitlab"
        / "code.aws.dev"
        / "repos"
        / "g"
        / "p"
    )
    assert not (tmp_path / "app").exists()


def test_another_forge_is_never_told_the_queue_awaits_migration(tmp_path: Path) -> None:
    """The pre-sharding queue name only ever existed for GitHub."""
    fold.legacy_queue_path(tmp_path).write_text("{}\n", encoding="utf-8")
    with pytest.raises(fold.QueueMigrationPending):
        fold._read_queue(repo="g/p", root=tmp_path)
    assert fold._read_queue(repo="g/p", root=tmp_path, forge=GITLAB) == {}


def test_each_forge_reads_its_own_queue_shard(two_forges: Path) -> None:
    fold.queue_path(repo="g/p", root=two_forges).write_text(
        json.dumps({"issue": 9, "slot": "github-slot"}) + "\n", encoding="utf-8"
    )
    fold.queue_path(repo="g/p", root=two_forges, forge=GITLAB).write_text(
        json.dumps({"issue": 9, "slot": "gitlab-slot"}) + "\n", encoding="utf-8"
    )
    assert fold._read_queue(repo="g/p", root=two_forges)[9]["slot"] == "github-slot"
    assert fold._read_queue(repo="g/p", root=two_forges, forge=GITLAB)[9]["slot"] == "gitlab-slot"


# ── route layer: which forge a request names ─────────────────────────────────

#: (provider, host, owner, repo) tuples the host app considers connected.
CONNECTED = {
    ("github", "github.com", "g", "p"),
    ("gitlab", "code.aws.dev", "gitlab", "gitlab_codeawsdev"),
    ("gitlab", "code.aws.dev", "group/sub", "thing"),
}


@pytest.fixture(name="served")
def served_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Open the enable gate, connect ``CONNECTED`` only, and record what the fold saw."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(
        routes.store,
        "is_repo_connected",
        lambda owner, repo, **kw: (kw.get("provider"), kw.get("host"), owner, repo) in CONNECTED,
    )

    class _Row:
        def to_dict(self) -> dict[str, Any]:
            return {"steps": []}

    def fake_fold(*, recent_hours: int, repo: str, forge: fold.Forge) -> _Row:
        seen["overview"] = (repo, forge)
        return _Row()

    def fake_step(step: str, *, owner: str, repo: str, limit: int, forge: fold.Forge) -> list:
        seen["step"] = (owner, repo, forge)
        return []

    def fake_sessions(number: int, *, repo: str, forge: fold.Forge) -> list:
        seen["sessions"] = (repo, forge)
        return []

    monkeypatch.setattr(fold, "fold_pipeline", fake_fold)
    monkeypatch.setattr(fold, "list_step_items", fake_step)
    monkeypatch.setattr(fold, "list_item_sessions", fake_sessions)
    return seen


def _client() -> TestClient:
    app = web.Application()
    routes.register_routes(app)
    return TestClient(TestServer(app))


GL = "provider=gitlab&host=code.aws.dev"


@pytest.mark.asyncio
async def test_a_connected_gitlab_project_is_served_from_its_own_forge(
    served: dict[str, Any],
) -> None:
    async with _client() as client:
        q = f"owner=gitlab&repo=gitlab_codeawsdev&{GL}"
        assert (await client.get(f"{routes.PREFIX}/overview?{q}")).status == 200
        assert (await client.get(f"{routes.PREFIX}/step?step=scan&{q}")).status == 200
        assert (await client.get(f"{routes.PREFIX}/item/sessions?number=3&{q}")).status == 200
    assert served["overview"] == ("gitlab/gitlab_codeawsdev", GITLAB)
    assert served["step"] == ("gitlab", "gitlab_codeawsdev", GITLAB)
    assert served["sessions"] == ("gitlab/gitlab_codeawsdev", GITLAB)


@pytest.mark.asyncio
async def test_a_nested_gitlab_group_is_accepted(served: dict[str, Any]) -> None:
    async with _client() as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner=group/sub&repo=thing&{GL}")
        assert resp.status == 200
    assert served["overview"] == ("group/sub/thing", GITLAB)


@pytest.mark.asyncio
async def test_a_github_request_is_still_served_from_github(served: dict[str, Any]) -> None:
    async with _client() as client:
        assert (await client.get(f"{routes.PREFIX}/overview?owner=g&repo=p")).status == 200
    assert served["overview"] == ("g/p", fold.GITHUB)


@pytest.mark.asyncio
async def test_connection_is_checked_against_the_named_forge(served: dict[str, Any]) -> None:
    """A repo connected on GitHub does not authorize the same slug on GitLab, or back."""
    async with _client() as client:
        on_gitlab = await client.get(f"{routes.PREFIX}/overview?owner=g&repo=p&{GL}")
        on_github = await client.get(
            f"{routes.PREFIX}/overview?owner=gitlab&repo=gitlab_codeawsdev"
        )
        other_host = await client.get(
            f"{routes.PREFIX}/overview?owner=gitlab&repo=gitlab_codeawsdev"
            "&provider=gitlab&host=other.example.com"
        )
        for resp in (on_gitlab, on_github, other_host):
            assert resp.status == 404
            assert (await resp.json())["code"] == "repo_not_connected"
    assert "overview" not in served


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "provider=azure&host=dev.azure.com",
        "provider=gitlab",  # a GitLab request must name its host
        "provider=gitlab&host=",
        "provider=github&host=ghe.internal",
        "host=code.aws.dev",  # a host alone still names another forge
        "provider=gitlab&host=evil/../x",
        "provider=gitlab&host=a%20b",
        "provider=gitlab&host=h:1:2",
        "provider=gitlab&host=h:0",  # port range is the allowlist coercer's: 1-65535
        "provider=gitlab&host=h:65536",
        "provider=gitlab&host=h:",
        "provider=gitlab&host=h:%2B443",
        "provider=gitlab&host=" + "h" * 300,
    ],
)
async def test_an_unservable_forge_is_refused_before_anything_is_read(
    served: dict[str, Any], query: str
) -> None:
    async with _client() as client:
        for path in ("overview", "step?step=scan", "item/sessions?number=3"):
            sep = "&" if "?" in path else "?"
            resp = await client.get(f"{routes.PREFIX}/{path}{sep}owner=g&repo=p&{query}")
            assert resp.status == 400, (path, query)
            assert (await resp.json())["code"] == "repo_provider_unsupported", (path, query)
    assert served == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["..", "a/..", "a//b", ".hidden", "a/.b", "a/", "/a", "a b/c"])
async def test_a_hostile_gitlab_group_path_is_refused(served: dict[str, Any], owner: str) -> None:
    """Each segment must match the name rule, so a group path cannot escape its tree."""
    async with _client() as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner={owner}&repo=p&{GL}")
        assert resp.status == 400
        assert (await resp.json())["code"] == "repo_invalid"
    assert served == {}


@pytest.mark.asyncio
async def test_a_github_owner_still_cannot_contain_a_slash(served: dict[str, Any]) -> None:
    async with _client() as client:
        resp = await client.get(f"{routes.PREFIX}/overview?owner=a/b&repo=p")
        assert resp.status == 400
        assert (await resp.json())["code"] == "repo_invalid"

"""Unit coverage for Issue Radar's GitLab client (``gitlab_client``).

The module is a function-for-function mirror of ``github_client`` that shells out
to the user's own ``glab`` CLI, so almost everything worth testing sits between
two seams: the argv/query a call BUILDS, and the normalization it applies to the
JSON that comes back. Both are exercised here through a stubbed transport --
``_glab_run`` is replaced by a router that answers from a table keyed on the API
path -- so no subprocess is spawned, no network is touched, and the POSIX-only
binary resolution is only reached by the handful of tests that target it
directly.

Coverage is deliberately weighted toward the branches a happy-path test never
sees: pagination (short page, page cap, non-list payload), the error mapping
(auth marker -> setup error, 403 -> permission error, unparseable JSON -> CLI
error), the degrade-rather-than-fail paths (label colours, secondary timeline
streams), and the refusals the module makes on purpose (auto-merge, a
"request changes" review, an MR open-count probe).
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import gitlab_client as gl

# The autouse isolation fixture stubs ``allowed_hosts``, so the two tests that
# cover the real reader hold onto it here, before any patching.
_REAL_ALLOWED_HOSTS = gl.allowed_hosts

# ── test transport ───────────────────────────────────────────────────────────


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    """A stand-in for the ``CompletedProcess`` ``_glab_run`` returns."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode, args=[])


class Router:
    """Stub for ``_glab_run``: answers from an ordered path table, records calls.

    Each table entry is ``(substring, answer)`` where ``answer`` is either a
    JSON-able payload (returned as a successful stdout), a ``(returncode, stderr)``
    tuple (returned as a failure), or a callable taking the router (so a paginated
    read can answer differently per page).
    """

    def __init__(self, table):
        self.table = list(table)
        self.calls: list[dict] = []

    def __call__(self, argv, *, host, timeout, input_text=None):
        target = argv[2] if len(argv) > 2 else ""
        self.calls.append(
            {"argv": list(argv), "target": target, "host": host,
             "timeout": timeout, "input": input_text}
        )
        for pattern, answer in self.table:
            if pattern in target:
                if callable(answer):
                    answer = answer(self)
                if isinstance(answer, tuple):
                    return _proc(returncode=answer[0], stderr=answer[1])
                if isinstance(answer, str):
                    return _proc(stdout=answer)
                return _proc(stdout=json.dumps(answer))
        return _proc(stdout="{}")

    def targets(self, needle: str = "") -> list[str]:
        return [c["target"] for c in self.calls if needle in c["target"]]


@pytest.fixture
def route(monkeypatch):
    """Install a :class:`Router` as the module's only transport."""

    def _install(table):
        router = Router(table)
        monkeypatch.setattr(gl, "_glab_run", router)
        return router

    return _install


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No inherited state: KIROCREW_HOME under tmp_path, no cached binary, no SEL."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(gl, "_glab_bin_cache", None)
    monkeypatch.setattr(gl, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: None))
    monkeypatch.setattr(gl, "allowed_hosts", lambda: frozenset({"gitlab.acme.example"}))


ISSUE = {
    "iid": 7,
    "title": "Broken pipeline",
    "web_url": "https://gitlab.com/g/p/-/issues/7",
    "labels": ["bug", "", 3],
    "user_notes_count": 4,
    "upvotes": 2,
    "downvotes": 1,
    "updated_at": "2026-07-02T00:00:00Z",
    "created_at": "2026-07-01T00:00:00Z",
    "state": "opened",
    "author": {"username": "alice"},
    "assignees": [{"username": "bob"}, {"nope": 1}],
    "description": "body text",
    "discussion_locked": True,
    "closed_at": None,
    "closed_by": {"username": "carol"},
    "milestone": {"title": "v1", "state": "active", "due_date": "2026-08-01"},
}

MR = {
    "iid": 12,
    "title": "Add widget",
    "web_url": "https://gitlab.com/g/p/-/merge_requests/12",
    "state": "opened",
    "work_in_progress": True,
    "labels": ["bug"],
    "author": {"username": "alice"},
    "updated_at": "2026-07-02T00:00:00Z",
    "created_at": "2026-07-01T00:00:00Z",
    "closed_at": None,
    "merged_at": None,
    "assignees": [{"username": "bob"}],
    "reviewers": [{"username": "carol"}],
    "target_branch": "main",
    "source_branch": "feat/widget",
    "sha": "abc1234",
    "description": "why",
    "head_pipeline": {"status": "running"},
}

LABELS = [
    {"name": "bug", "color": "#d9534f", "description": "a defect"},
    {"name": "chore", "color": "#cccccc"},
    {"noname": True},
]


# ── URL parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "link",
    [
        "",
        "http://gitlab.com/g/p",
        "https://user:pw@gitlab.com/g/p",
        "https://gitlab.com/g",
        "https://gitlab.com/g/p/../x",
        "https://gitlab.com/g/p p",
        "https://gitlab.com/groups/g/p",
        "https://gitlab.com/api/p",
        "https://evil.example/g/p",
        "https://gitlab.com:notaport/g/p",
        "https://[oops/g/p",
        "https://gitlab.acme.example:8443/g/p",
    ],
)
def test_parse_rejects_unusable_links(link):
    with pytest.raises(gl.RepoUrlError):
        gl.parse_gitlab_repo_url(link)


def test_parse_rejects_a_non_string():
    with pytest.raises(gl.RepoUrlError):
        gl.parse_gitlab_repo_url(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://gitlab.com/g/p", ("gitlab.com", "g", "p")),
        ("https://www.gitlab.com/g/p/", ("gitlab.com", "g", "p")),
        ("https://gitlab.com/g/sub/p.git", ("gitlab.com", "g/sub", "p")),
        ("https://gitlab.com/g/p/-/merge_requests/7", ("gitlab.com", "g", "p")),
        ("https://gitlab.com:443/g/p", ("gitlab.com", "g", "p")),
        ("  https://gitlab.com/g/p  ", ("gitlab.com", "g", "p")),
    ],
)
def test_parse_accepts_project_urls(link, expected):
    assert gl.parse_gitlab_repo_url(link) == expected


def test_parse_matches_a_self_managed_host_from_the_allowlist():
    allowed = frozenset({"gitlab.acme.example"})
    assert gl.parse_gitlab_repo_url(
        "https://gitlab.acme.example./team/app", allowed_hosts=allowed
    ) == ("gitlab.acme.example", "team", "app")


def test_parse_requires_the_port_to_be_allowlisted_too():
    allowed = frozenset({"gitlab.acme.example:8443"})
    assert gl.parse_gitlab_repo_url(
        "https://gitlab.acme.example:8443/team/app", allowed_hosts=allowed
    ) == ("gitlab.acme.example:8443", "team", "app")


def test_project_path_encodes_the_namespace_separator():
    assert gl.project_path("g/sub", "p") == "g%2Fsub%2Fp"


# ── host resolution, env, allowlist ──────────────────────────────────────────


def test_resolve_host_requires_a_host():
    with pytest.raises(gl.ProviderCliError, match="host is required"):
        gl._resolve_host("")


@pytest.mark.parametrize("host", ["gitlab.com", "WWW.GitLab.com", "gitlab.acme.example."])
def test_resolve_host_accepts_public_and_allowlisted(host):
    assert gl._resolve_host(host) in {"gitlab.com", "gitlab.acme.example"}


def test_resolve_host_refuses_an_unlisted_host():
    with pytest.raises(gl.ProviderCliError, match="allowlist"):
        gl._resolve_host("gitlab.evil.example")


def test_allowed_hosts_reads_the_config(monkeypatch):
    monkeypatch.setattr(
        gl,
        "KiroCrewConfig",
        SimpleNamespace(
            load=lambda: SimpleNamespace(dashboard=SimpleNamespace(gitlab_hosts=["a.example"]))
        ),
    )
    assert _REAL_ALLOWED_HOSTS() == frozenset({"a.example"})


def test_allowed_hosts_fails_closed_on_a_broken_config(monkeypatch):
    def _boom():
        raise RuntimeError("unreadable")

    monkeypatch.setattr(gl, "KiroCrewConfig", SimpleNamespace(load=_boom))
    assert _REAL_ALLOWED_HOSTS() == frozenset()


def test_glab_env_pins_the_host_and_drops_the_token_off_gitlab_com(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    env = gl._glab_env("gitlab.acme.example")
    assert env["GITLAB_HOST"] == "gitlab.acme.example"
    assert env["GLAB_PAGER"] == "cat"
    assert env["NO_COLOR"] == "1"
    assert "GITLAB_TOKEN" not in env
    assert env["HTTPS_PROXY"] == "http://proxy.example:3128"


def test_glab_env_keeps_the_token_for_gitlab_com(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xyz")
    assert gl._glab_env("gitlab.com")["GITLAB_TOKEN"] == "glpat-xyz"


# ── the spawn chokepoint ─────────────────────────────────────────────────────


def test_glab_run_substitutes_the_trusted_binary(monkeypatch):
    monkeypatch.setattr(gl, "_glab_bin", lambda: "/trusted/bin/glab")
    seen: dict = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _proc(stdout="{}")

    monkeypatch.setattr(gl.subprocess, "run", _run)
    proc = gl._glab_run(["glab", "api", "user"], host="gitlab.com", timeout=5.0)
    assert proc.returncode == 0
    assert seen["argv"] == ["/trusted/bin/glab", "api", "user"]
    assert seen["kwargs"]["env"]["GITLAB_HOST"] == "gitlab.com"
    assert seen["kwargs"]["timeout"] == 5.0
    assert seen["kwargs"]["check"] is False


def test_glab_run_maps_a_timeout_to_a_cli_error(monkeypatch):
    monkeypatch.setattr(gl, "_glab_bin", lambda: "/trusted/bin/glab")

    def _run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd="glab", timeout=5.0)

    monkeypatch.setattr(gl.subprocess, "run", _run)
    with pytest.raises(gl.ProviderCliError, match="timed out"):
        gl._glab_run(["glab", "api", "user"], host="gitlab.com", timeout=5.0)


def test_glab_run_returns_a_failure_without_raising(monkeypatch):
    monkeypatch.setattr(gl, "_glab_bin", lambda: "/trusted/bin/glab")
    monkeypatch.setattr(gl.subprocess, "run", lambda argv, **kw: _proc(returncode=22))
    assert gl._glab_run(["glab", "api", "user"], host="gitlab.com", timeout=1.0).returncode == 22


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX resolution path")
def test_glab_bin_override_is_validated_and_cached(monkeypatch):
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setenv("KIROCREW_ISSUE_RADAR_GLAB", "/opt/glab")
    monkeypatch.setattr(source_providers, "_validate_provider_executable", lambda p: "/opt/glab")
    assert gl._glab_bin() == "/opt/glab"
    # Cached: a second call must not re-validate.
    monkeypatch.setattr(
        source_providers,
        "_validate_provider_executable",
        lambda p: (_ for _ in ()).throw(AssertionError("re-validated")),
    )
    assert gl._glab_bin() == "/opt/glab"


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX resolution path")
def test_glab_bin_override_failure_is_a_setup_error(monkeypatch):
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.setenv("KIROCREW_ISSUE_RADAR_GLAB", "/opt/glab")

    def _reject(path):
        raise ValueError("not trusted")

    monkeypatch.setattr(source_providers, "_validate_provider_executable", _reject)
    with pytest.raises(gl.ProviderSetupError) as excinfo:
        gl._glab_bin()
    assert excinfo.value.reason == "not_installed"
    assert "KIROCREW_ISSUE_RADAR_GLAB" in str(excinfo.value)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX resolution path")
def test_glab_bin_skips_untrusted_candidates_and_reports_the_last_check(monkeypatch, tmp_path):
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.delenv("KIROCREW_ISSUE_RADAR_GLAB", raising=False)
    present = tmp_path / "glab"
    present.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        source_providers,
        "provider_executable_candidates",
        lambda name: (str(tmp_path / "absent-glab"), str(present)),
    )

    def _reject(path):
        raise ValueError("world-writable")

    monkeypatch.setattr(source_providers, "_validate_provider_executable", _reject)
    with pytest.raises(gl.ProviderSetupError) as excinfo:
        gl._glab_bin()
    assert excinfo.value.reason == "not_installed"
    assert "world-writable" in str(excinfo.value)
    assert "glab auth login" in str(excinfo.value)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX resolution path")
def test_glab_bin_accepts_the_first_trusted_candidate(monkeypatch, tmp_path):
    from kiro_crew.dashboard.handlers import source_providers

    monkeypatch.delenv("KIROCREW_ISSUE_RADAR_GLAB", raising=False)
    present = tmp_path / "glab"
    present.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        source_providers, "provider_executable_candidates", lambda name: (str(present),)
    )
    monkeypatch.setattr(source_providers, "_validate_provider_executable", lambda p: p)
    assert gl._glab_bin() == str(present)


def test_glab_bin_refuses_windows(monkeypatch):
    monkeypatch.setattr(gl.sys, "platform", "win32")
    with pytest.raises(gl.ProviderCliError, match="POSIX"):
        gl._glab_bin()


# ── the API layer: pagination and error mapping ──────────────────────────────


def test_glab_api_walks_pages_until_a_short_one(route):
    router = route(
        [("issues", lambda r: [{"iid": len(r.calls)}] * (100 if len(r.calls) <= 2 else 3))]
    )
    out = gl._glab_api("projects/g%2Fp/issues", host="gitlab.com", paginate=True)
    assert len(out) == 203
    assert "?page=1&per_page=100" in router.calls[0]["target"]
    assert "?page=3&per_page=100" in router.calls[2]["target"]


def test_glab_api_stops_at_the_page_cap(route, monkeypatch):
    monkeypatch.setattr(gl, "_PAGE_SIZE", 1)
    monkeypatch.setattr(gl, "_MAX_PAGES", 3)
    router = route([("issues", [{"iid": 1}])])
    out = gl._glab_api("projects/g%2Fp/issues", host="gitlab.com", paginate=True)
    assert len(out) == 3
    assert len(router.calls) == 3


def test_glab_api_uses_an_ampersand_when_the_path_already_has_a_query(route):
    router = route([("issues", [])])
    gl._glab_api("projects/g%2Fp/issues?state=opened", host="gitlab.com", paginate=True)
    assert "?state=opened&page=1" in router.calls[0]["target"]


def test_glab_api_returns_a_non_list_payload_from_a_paginated_read(route):
    route([("issues", {"message": "nope"})])
    assert gl._glab_api("projects/g%2Fp/issues", host="gitlab.com", paginate=True) == {
        "message": "nope"
    }


@pytest.mark.parametrize("paginate,expected", [(False, {}), (True, [])])
def test_glab_api_treats_empty_output_as_an_empty_payload(route, paginate, expected):
    route([("user", "   ")])
    assert gl._glab_api("user", host="gitlab.com", paginate=paginate) == expected


def test_glab_api_sends_a_body_on_stdin_with_a_method(route):
    router = route([("issues/7", {"iid": 7})])
    gl._glab_api(
        "projects/g%2Fp/issues/7", host="gitlab.com", method="PUT", body={"state_event": "close"}
    )
    call = router.calls[0]
    assert call["argv"][3:] == ["--method", "PUT", "--input", "-"]
    assert json.loads(call["input"]) == {"state_event": "close"}


def test_glab_api_rejects_unparseable_output(route):
    route([("user", "not json")])
    with pytest.raises(gl.ProviderCliError, match="unexpected output"):
        gl._glab_api("user", host="gitlab.com")


def test_glab_api_maps_an_auth_marker_to_a_setup_error(route):
    route([("user", (1, "run `glab auth login` first"))])
    with pytest.raises(gl.ProviderSetupError) as excinfo:
        gl._glab_api("user", host="gitlab.com")
    assert excinfo.value.reason == "not_authenticated"
    assert "glab auth login --hostname gitlab.com" in str(excinfo.value)


@pytest.mark.parametrize("tail", ["HTTP 403", "Forbidden", "insufficient scope"])
def test_glab_api_maps_403_to_a_permission_error(route, tail):
    route([("members", (1, tail))])
    with pytest.raises(gl.ProviderPermissionError):
        gl._glab_api("projects/g%2Fp/members/all", host="gitlab.com")


def test_glab_api_maps_any_other_failure_to_a_cli_error(route):
    route([("user", (1, "HTTP 500 boom"))])
    with pytest.raises(gl.ProviderCliError, match="exit 1"):
        gl._glab_api("user", host="gitlab.com")


# ── normalization helpers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("opened", "open"), ("locked", "open"), ("closed", "closed"), ("", "open"), (None, "open")],
)
def test_norm_state(raw, expected):
    assert gl._norm_state(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("#d9534f", "d9534f"), ("d9534f", "d9534f"), ("", "888888"), (None, "888888")]
)
def test_hex_color(raw, expected):
    assert gl._hex_color(raw) == expected


def test_label_names_drops_non_strings_and_a_non_list():
    assert gl._label_names(["a", "", 3, None]) == ["a"]
    assert gl._label_names("nope") == []


def test_username_and_usernames_tolerate_junk():
    assert gl._username({"username": "alice"}) == "alice"
    assert gl._username({"username": ""}) is None
    assert gl._username("alice") is None
    assert gl._usernames([{"username": "a"}, {}, 3]) == ["a"]
    assert gl._usernames("nope") == []


@pytest.mark.parametrize(
    "level,role",
    [(50, "admin"), (40, "maintain"), (30, "write"), (20, "triage"), (10, "read"),
     (5, "read"), ("30", "write"), ("abc", "read"), (None, "read")],
)
def test_role_for_access_level(level, role):
    assert gl._role_for_access_level(level) == role


def test_permissions_for_access_level_is_conservative():
    assert gl._permissions_for_access_level(20) == {
        "admin": False, "maintain": False, "push": False, "triage": True, "pull": True
    }
    assert gl._permissions_for_access_level(50)["admin"] is True


def test_access_level_takes_the_best_of_project_and_group():
    details = {
        "permissions": {
            "project_access": {"access_level": 20},
            "group_access": {"access_level": "40"},
        }
    }
    assert gl._access_level(details) == 40
    assert gl._access_level({"permissions": {"project_access": {"access_level": "x"}}}) == 0
    assert gl._access_level({}) == 0


def test_rows_and_obj_coerce_junk():
    assert gl._rows([{"a": 1}, 3, None]) == [{"a": 1}]
    assert gl._rows("nope") == []
    assert gl._obj({"a": 1}) == {"a": 1}
    assert gl._obj(None) == {}


def test_norm_issue_maps_gitlab_fields_onto_the_github_vocabulary():
    row = gl._norm_issue(ISSUE)
    assert row["number"] == 7
    assert row["url"] == ISSUE["web_url"]
    assert row["labels"] == ["bug"]
    assert row["comments"] == 4
    assert row["reactions"] == 3
    assert row["thumbs_up"] == 2
    assert row["author_association"] is None
    assert row["state"] == "open"
    assert row["author"] == "alice"
    assert row["assignees"] == ["bob"]
    assert row["body"] == "body text"


def test_norm_issue_tolerates_a_bare_payload():
    row = gl._norm_issue({})
    assert row["title"] == "" and row["comments"] == 0 and row["reactions"] == 0


def test_norm_issue_detail_colours_labels_and_summarizes_reactions():
    detail = gl._norm_issue_detail(ISSUE, {"bug": {"color": "#ff0000", "description": "d"}})
    assert detail["labels"] == [{"name": "bug", "color": "ff0000", "description": "d"}]
    assert detail["locked"] is True
    assert detail["closed_by"] == "carol"
    assert detail["state_reason"] is None
    assert detail["milestone"] == {"title": "v1", "state": "active", "due_on": "2026-08-01"}
    assert detail["reactions"]["total"] == 3
    assert detail["reactions"]["plus1"] == 2


def test_norm_issue_detail_omits_reactions_and_milestone_when_absent():
    detail = gl._norm_issue_detail({"iid": 1, "labels": ["bug"]}, {})
    assert detail["reactions"] is None
    assert detail["milestone"] is None
    # An unknown label still renders, with the neutral default colour.
    assert detail["labels"] == [{"name": "bug", "color": "888888", "description": ""}]


def test_shape_labels_drops_nameless_rows():
    assert gl._shape_labels(LABELS) == [
        {"name": "bug", "color": "d9534f", "description": "a defect"},
        {"name": "chore", "color": "cccccc", "description": ""},
    ]


# ── project reads ────────────────────────────────────────────────────────────


def test_verify_repo_access_summarizes_the_project(route):
    route([("projects/", {
        "path_with_namespace": "g/p",
        "visibility": "internal",
        "open_issues_count": 9,
        "description": "d",
        "permissions": {"project_access": {"access_level": 40}},
    })])
    out = gl.verify_repo_access("g", "p", host="gitlab.com")
    assert out["full_name"] == "g/p"
    # internal is not public, so the UI's badge reads private.
    assert out["private"] is True
    assert out["open_issues_count"] == 9
    assert out["permissions"]["maintain"] is True


def test_verify_repo_access_falls_back_to_statistics_for_the_count(route):
    route([("projects/", {
        "visibility": "public",
        "statistics": {"open_issues_count": 4},
        "permissions": {},
    })])
    out = gl.verify_repo_access("g", "p", host="gitlab.com")
    assert out["open_issues_count"] == 4
    assert out["private"] is False
    assert out["full_name"] == "g/p"


def test_verify_repo_access_raises_on_an_empty_project(route):
    route([("projects/", {})])
    with pytest.raises(gl.ProviderCliError, match="could not read"):
        gl.verify_repo_access("g", "p", host="gitlab.com")


def test_get_repo_permissions_returns_the_permission_object(route):
    route([("projects/", {"permissions": {"project_access": {"access_level": 30}}})])
    assert gl.get_repo_permissions("g", "p", host="gitlab.com")["push"] is True


def test_get_repo_permissions_coerces_a_non_dict(route, monkeypatch):
    monkeypatch.setattr(gl, "verify_repo_access", lambda *a, **k: {"permissions": None})
    assert gl.get_repo_permissions("g", "p", host="gitlab.com") == {}


def test_list_open_issues_paginates_and_asks_for_every_author(route):
    router = route([("issues", [ISSUE])])
    rows = gl.list_open_issues("g", "p", host="gitlab.com")
    assert [r["number"] for r in rows] == [7]
    target = router.calls[0]["target"]
    assert "state=opened" in target and "scope=all" in target
    assert "order_by=updated_at" in target and "&page=1&" in target


def test_list_open_issues_first_page_asks_for_a_full_page_without_paginating(route):
    router = route([("issues", [ISSUE])])
    gl.list_open_issues_first_page("g", "p", host="gitlab.com")
    target = router.calls[0]["target"]
    assert "per_page=100" in target
    assert "&page=1&" not in target


def test_list_closed_issues_asks_for_the_closed_state(route):
    router = route([("issues", [dict(ISSUE, state="closed")])])
    rows = gl.list_closed_issues("g", "p", host="gitlab.com")
    assert rows[0]["state"] == "closed"
    assert "state=closed" in router.calls[0]["target"]


def test_list_recent_open_issues_orders_by_creation_and_caps_the_limit(route):
    router = route([("issues", [ISSUE, dict(ISSUE, iid=8)])])
    rows = gl.list_recent_open_issues("g", "p", limit=1, host="gitlab.com")
    assert [r["number"] for r in rows] == [7]
    assert "order_by=created_at" in router.calls[0]["target"]
    assert "per_page=1" in router.calls[0]["target"]


def test_list_recent_open_issues_clamps_an_absurd_limit(route):
    router = route([("issues", [])])
    gl.list_recent_open_issues("g", "p", limit=10_000, host="gitlab.com")
    assert "per_page=100" in router.calls[0]["target"]


def test_list_repo_labels_shapes_and_paginates(route):
    router = route([("labels", LABELS)])
    assert [lab["name"] for lab in gl.list_repo_labels("g", "p", host="gitlab.com")] == [
        "bug", "chore"
    ]
    assert "?page=1&per_page=100" in router.calls[0]["target"]


def test_list_repo_collaborators_uses_inherited_members(route):
    router = route([("members/all", [
        {"username": "alice", "access_level": 50},
        {"username": "bob", "access_level": 20},
        {"access_level": 30},
    ])])
    assert gl.list_repo_collaborators("g", "p", host="gitlab.com") == [
        {"login": "alice", "role_name": "admin"},
        {"login": "bob", "role_name": "triage"},
    ]
    assert "members/all" in router.calls[0]["target"]


def test_derive_members_is_always_empty():
    assert gl.derive_members([{"author": "alice", "author_association": "MEMBER"}]) == []


def test_get_current_login_reads_the_session_user(route):
    route([("user", {"username": "alice"})])
    assert gl.get_current_login(host="gitlab.com") == "alice"


def test_get_current_login_returns_none_on_a_cli_failure(route):
    route([("user", (1, "HTTP 500"))])
    assert gl.get_current_login(host="gitlab.com") is None


def test_list_contributed_repos_filters_by_activity_and_shape(route):
    route([("projects?membership=true", [
        {"path_with_namespace": "g/p", "last_activity_at": "2099-01-01T00:00:00Z",
         "visibility": "public", "description": "d"},
        {"path_with_namespace": "g/old", "last_activity_at": "2000-01-01T00:00:00Z"},
        {"path_with_namespace": "noslash"},
    ])])
    rows, truncated = gl.list_contributed_repos("alice", host="gitlab.com", within_days=30)
    assert rows == [
        {"owner": "g", "repo": "p", "pushed_at": "2099-01-01T00:00:00Z",
         "private": False, "description": "d"}
    ]
    assert truncated is False


def test_list_contributed_repos_reports_truncation_at_the_page_cap(route, monkeypatch):
    monkeypatch.setattr(gl, "_PAGE_SIZE", 1)
    monkeypatch.setattr(gl, "_MAX_PAGES", 1)
    route([("projects?membership=true", [{"path_with_namespace": "g/p"}])])
    rows, truncated = gl.list_contributed_repos("alice", host="gitlab.com", within_days=0)
    assert truncated is True
    assert rows[0]["private"] is True


@pytest.mark.parametrize("window", [0, -1, gl.MAX_WINDOW_DAYS, "abc", None])
def test_cutoff_iso_is_empty_for_an_unbounded_window(window):
    assert gl._cutoff_iso(window) == ""


def test_cutoff_iso_returns_an_iso_timestamp():
    assert gl._cutoff_iso(30).startswith("20")


def test_get_issue_detail_joins_the_label_colours(route):
    route([("labels", LABELS), ("issues/7", ISSUE)])
    detail = gl.get_issue_detail("g", "p", 7, host="gitlab.com")
    assert detail["labels"] == [{"name": "bug", "color": "d9534f", "description": "a defect"}]


def test_get_issue_detail_degrades_when_labels_cannot_be_read(route):
    route([("labels", (1, "HTTP 500")), ("issues/7", ISSUE)])
    detail = gl.get_issue_detail("g", "p", 7, host="gitlab.com")
    assert detail["labels"] == [{"name": "bug", "color": "888888", "description": ""}]


def test_get_issue_detail_raises_on_an_empty_issue(route):
    route([("issues/7", {})])
    with pytest.raises(gl.ProviderCliError, match="could not read"):
        gl.get_issue_detail("g", "p", 7, host="gitlab.com")


def test_get_issue_detail_coerces_the_number_before_it_reaches_argv(route):
    router = route([("issues", ISSUE)])
    with pytest.raises(ValueError):
        gl.get_issue_detail("g", "p", "7/../secret", host="gitlab.com")  # type: ignore[arg-type]
    assert router.calls == []


# ── reference summary ────────────────────────────────────────────────────────


def test_get_ref_summary_is_issue_only_and_colours_its_labels(route):
    route([("labels", LABELS), ("issues/7", ISSUE)])
    out = gl.get_ref_summary("g", "p", 7, host="gitlab.com")
    assert out["is_pr"] is False and out["merged_at"] is None and out["draft"] is False
    assert out["labels"] == [{"name": "bug", "color": "d9534f"}]


def test_get_ref_summary_skips_the_label_call_when_there_are_none(route):
    router = route([("issues/7", dict(ISSUE, labels=[]))])
    assert gl.get_ref_summary("g", "p", 7, host="gitlab.com")["labels"] == []
    assert router.targets("labels") == []


def test_get_ref_summary_degrades_when_labels_cannot_be_read(route):
    route([("labels", (1, "HTTP 500")), ("issues/7", ISSUE)])
    assert gl.get_ref_summary("g", "p", 7, host="gitlab.com")["labels"] == [
        {"name": "bug", "color": "888888"}
    ]


def test_get_ref_summary_raises_on_a_missing_issue(route):
    route([("issues/7", {})])
    with pytest.raises(gl.ProviderCliError, match="could not read"):
        gl.get_ref_summary("g", "p", 7, host="")


# ── timeline assembly ────────────────────────────────────────────────────────


def test_norm_note_keeps_a_human_comment():
    out = gl._norm_note({"body": "hi", "created_at": "t", "author": {"username": "alice"}})
    assert out == {
        "kind": "comment", "actor": "alice", "created_at": "t", "body": "hi",
        "author_association": None, "reactions": None,
    }


def test_norm_note_drops_an_unrecognized_system_note():
    assert gl._norm_note({"body": "did something odd", "system": True}) is None


@pytest.mark.parametrize(
    "body,expected",
    [
        ("assigned to @bob", ("assigned", "bob")),
        ("unassigned @bob", ("unassigned", "bob")),
        ("assigned to @", ("assigned", None)),
    ],
)
def test_norm_note_parses_assignment(body, expected):
    out = gl._norm_note({"body": body, "system": True})
    assert (out["kind"], out["assignee"]) == expected


def test_norm_note_parses_a_rename():
    out = gl._norm_note({"body": "changed title from **old** to **new**", "system": True})
    assert out["kind"] == "renamed"
    assert out["rename"] == {"from": "old", "to": "new"}


def test_norm_note_reports_an_unparseable_rename_as_unknown():
    out = gl._norm_note({"body": "changed title from old to new", "system": True})
    assert out["rename"] == {"from": None, "to": None}


@pytest.mark.parametrize(
    "body,number,is_pr",
    [
        ("mentioned in issue #42", 42, False),
        ("mentioned in merge request !42", 42, True),
        ("mentioned in issue somewhere", None, False),
    ],
)
def test_norm_note_parses_a_cross_reference(body, number, is_pr):
    out = gl._norm_note({"body": body, "system": True})
    assert out["kind"] == "cross-referenced"
    assert out["source"]["number"] == number
    assert out["source"]["is_pr"] is is_pr


@pytest.mark.parametrize(
    "body,commit",
    [("mentioned in commit abc1234", "abc1234"), ("mentioned in commit zz", None)],
)
def test_norm_note_parses_a_commit_reference(body, commit):
    out = gl._norm_note({"body": body, "system": True})
    assert (out["kind"], out["commit_id"]) == ("referenced", commit)


@pytest.mark.parametrize(
    "body,kind",
    [("changed milestone to %v1", "milestoned"), ("removed milestone", "demilestoned")],
)
def test_norm_note_parses_milestone_changes(body, kind):
    out = gl._norm_note({"body": body, "system": True})
    assert (out["kind"], out["milestone"]) == (kind, None)


def test_norm_note_drops_a_pattern_that_has_no_handler(monkeypatch):
    """The tail of ``_norm_note`` is the guard for extending the pattern table.

    Adding a prefix to ``_SYSTEM_NOTE_PATTERNS`` without also adding the branch
    that shapes it must DROP the note, not emit a half-built entry the UI cannot
    render. Only reachable by adding such a pattern, which is exactly the mistake
    it protects against.
    """
    monkeypatch.setattr(
        gl, "_SYSTEM_NOTE_PATTERNS", gl._SYSTEM_NOTE_PATTERNS + (("locked this", "locked"),)
    )
    assert gl._norm_note({"body": "locked this issue", "system": True}) is None


def test_norm_label_event_maps_add_and_remove():
    added = gl._norm_label_event(
        {"action": "add", "user": {"username": "alice"}, "created_at": "t",
         "label": {"name": "bug", "color": "#ff0000"}},
        {},
    )
    assert added["kind"] == "labeled"
    assert added["label"] == {"name": "bug", "color": "ff0000"}
    removed = gl._norm_label_event({"action": "remove", "label": {"name": "bug"}}, {})
    assert removed["kind"] == "unlabeled"


def test_norm_label_event_falls_back_to_the_project_colour():
    out = gl._norm_label_event(
        {"action": "add", "label": {"name": "bug"}}, {"bug": {"color": "#00ff00"}}
    )
    assert out["label"]["color"] == "00ff00"


@pytest.mark.parametrize(
    "event", [{"action": "moved", "label": {"name": "bug"}}, {"action": "add", "label": {}}]
)
def test_norm_label_event_drops_what_it_cannot_render(event):
    assert gl._norm_label_event(event, {}) is None


@pytest.mark.parametrize(
    "state,kind",
    [("closed", "closed"), ("reopened", "reopened"), ("opened", "reopened"), ("merged", None)],
)
def test_norm_state_event(state, kind):
    out = gl._norm_state_event({"state": state, "user": {"username": "alice"}})
    assert (out or {}).get("kind") == kind


def test_list_issue_timeline_merges_and_sorts_every_stream(route):
    route([
        ("issues/7/notes", [
            {"body": "second", "created_at": "2026-07-02T00:00:00Z",
             "author": {"username": "bob"}},
            {"body": "assigned to @bob", "system": True, "created_at": "2026-07-01T00:00:00Z"},
        ]),
        ("resource_label_events", [
            {"action": "add", "created_at": "2026-07-03T00:00:00Z", "label": {"name": "bug"}}
        ]),
        ("resource_state_events", [
            {"state": "closed", "created_at": "2026-07-04T00:00:00Z"}
        ]),
        ("labels", LABELS),
    ])
    events = gl.list_issue_timeline("g", "p", 7, host="gitlab.com")
    assert [e["kind"] for e in events] == ["assigned", "comment", "labeled", "closed"]


def test_assemble_timeline_degrades_when_a_secondary_stream_fails(route):
    route([
        ("issues/7/notes", [{"body": "hi", "created_at": "t"}]),
        ("resource_label_events", (1, "HTTP 404")),
        ("resource_state_events", (1, "HTTP 404")),
        ("labels", (1, "HTTP 500")),
    ])
    events = gl.list_issue_timeline("g", "p", 7, host="gitlab.com")
    assert [e["kind"] for e in events] == ["comment"]


def test_list_pr_timeline_promotes_positioned_notes_to_review_comments(route):
    route([
        ("merge_requests/12/notes", [
            {"body": "nit", "created_at": "2026-07-01T00:00:00Z",
             "author": {"username": "carol"},
             "position": {"new_path": "a.py", "new_line": 12}},
            {"body": "plain", "created_at": "2026-07-02T00:00:00Z"},
            {"body": "old file", "created_at": "2026-07-03T00:00:00Z",
             "position": {"old_path": "b.py", "old_line": 3}},
            {"body": "assigned to @bob", "system": True, "created_at": "2026-07-04T00:00:00Z",
             "position": {"new_path": "c.py"}},
        ]),
        ("resource_label_events", []),
        ("resource_state_events", []),
        ("labels", LABELS),
    ])
    events = gl.list_pr_timeline("g", "p", 12, host="gitlab.com")
    kinds = [e["kind"] for e in events]
    # The positioned notes appear ONCE, as the richer inline entry.
    assert kinds.count("review_comment") == 2
    assert kinds.count("comment") == 1
    inline = next(e for e in events if e["kind"] == "review_comment")
    assert (inline["path"], inline["line"], inline["actor"]) == ("a.py", 12, "carol")
    old = [e for e in events if e["kind"] == "review_comment"][1]
    assert (old["path"], old["line"]) == ("b.py", 3)


# ── write operations ─────────────────────────────────────────────────────────


def test_add_issue_labels_returns_the_full_shaped_set(route):
    router = route([("labels", LABELS), ("issues/7", {"labels": ["bug", "chore"]})])
    out = gl.add_issue_labels("g", "p", 7, ["bug", "chore"], host="gitlab.com")
    assert out == [
        {"name": "bug", "color": "d9534f", "description": "a defect"},
        {"name": "chore", "color": "cccccc", "description": ""},
    ]
    put = next(c for c in router.calls if "issues/7" in c["target"])
    assert json.loads(put["input"]) == {"add_labels": "bug,chore"}
    assert "--method" in put["argv"] and "PUT" in put["argv"]


def test_remove_issue_label_returns_the_remaining_labels(route):
    router = route([("labels", LABELS), ("issues/7", {"labels": ["chore"]})])
    out = gl.remove_issue_label("g", "p", 7, "bug", host="gitlab.com")
    assert [lab["name"] for lab in out] == ["chore"]
    put = next(c for c in router.calls if "issues/7" in c["target"])
    assert json.loads(put["input"]) == {"remove_labels": "bug"}


def test_resolve_label_details_degrades_to_the_neutral_colour(route):
    route([("labels", (1, "HTTP 500"))])
    assert gl._resolve_label_details(
        "g", "p", ["bug"], host="gitlab.com", timeout=1.0
    ) == [{"name": "bug", "color": "888888", "description": ""}]


@pytest.mark.parametrize(
    "state,event", [("closed", "close"), ("open", "reopen")]
)
def test_set_issue_state_sends_a_state_event(route, state, event):
    router = route([("issues/7", {"state": "closed" if state == "closed" else "opened"})])
    out = gl.set_issue_state("g", "p", 7, state, state_reason="completed", host="gitlab.com")
    assert out["state_reason"] is None
    assert json.loads(router.calls[0]["input"]) == {"state_event": event}


def test_create_label_adds_the_hash_gitlab_requires(route):
    router = route([("labels", {"name": "bug", "color": "#d9534f", "description": "d"})])
    out = gl.create_label("g", "p", "bug", "d9534f", "d", host="gitlab.com")
    assert out == {"name": "bug", "color": "d9534f", "description": "d"}
    assert json.loads(router.calls[0]["input"])["color"] == "#d9534f"


def test_create_label_returns_a_synthetic_row_when_the_response_is_not_a_label(route):
    route([("labels", [])])
    assert gl.create_label("g", "p", "bug", host="gitlab.com") == {
        "name": "bug", "color": "888888", "description": ""
    }


def test_create_label_is_idempotent_on_a_conflict(route):
    route([
        ("labels?search=", [{"name": "bug", "color": "#111111", "description": "existing"}]),
        ("labels", (1, "409 Conflict: label already exists")),
    ])
    assert gl.create_label("g", "p", "bug", host="gitlab.com") == {
        "name": "bug", "color": "111111", "description": "existing"
    }


def test_create_label_synthesizes_a_row_when_the_conflict_lookup_misses(route):
    route([
        ("labels?search=", []),
        ("labels", (1, "409 has already been taken")),
    ])
    assert gl.create_label("g", "p", "bug", "abcdef", host="gitlab.com")["color"] == "abcdef"


def test_create_label_propagates_a_permission_error(route):
    route([("labels", (1, "HTTP 403 Forbidden"))])
    with pytest.raises(gl.ProviderPermissionError):
        gl.create_label("g", "p", "bug", host="gitlab.com")


def test_create_label_propagates_an_unrelated_failure(route):
    route([("labels", (1, "HTTP 500 boom"))])
    with pytest.raises(gl.ProviderCliError):
        gl.create_label("g", "p", "bug", host="gitlab.com")


# ── merge requests ───────────────────────────────────────────────────────────


def test_norm_pull_maps_branches_votes_and_the_head_commit():
    row = gl._norm_pull(MR)
    assert row["number"] == 12
    assert row["state"] == "open"
    assert row["draft"] is True  # from work_in_progress
    assert row["base"] == "main" and row["head"] == "feat/widget"
    assert row["head_sha"] == "abc1234"
    assert row["requested_reviewers"] == ["carol"]
    # The card enrichment rides on the same payload.
    assert row["checks_state"] == "running"
    assert row["checks_counts"]["running"] == 1
    assert row["additions"] is None and row["changed_files"] is None


def test_norm_pull_prefers_diff_refs_and_the_explicit_draft_flag():
    row = gl._norm_pull(dict(MR, draft=False, diff_refs={"head_sha": "deadbee"}))
    assert row["draft"] is False
    assert row["head_sha"] == "deadbee"


def test_norm_pull_folds_merged_into_closed_but_keeps_merged_at():
    row = gl._norm_pull(dict(MR, state="merged", merged_at="2026-07-05T00:00:00Z"))
    assert row["state"] == "closed"
    assert row["merged_at"] == "2026-07-05T00:00:00Z"


def test_list_open_pulls_paginates(route):
    router = route([("merge_requests", [MR])])
    assert [r["number"] for r in gl.list_open_pulls("g", "p", host="gitlab.com")] == [12]
    assert "state=opened" in router.calls[0]["target"]
    assert "&page=1&" in router.calls[0]["target"]


def test_list_open_pulls_first_page_is_one_full_page(route):
    router = route([("merge_requests", [MR])])
    gl.list_open_pulls_first_page("g", "p", host="gitlab.com")
    assert "per_page=100" in router.calls[0]["target"]
    assert "&page=1&" not in router.calls[0]["target"]


def test_list_closed_pulls_asks_for_all_and_keeps_only_non_open(route):
    router = route([("merge_requests", [
        MR,
        dict(MR, iid=13, state="merged", merged_at="2026-07-05T00:00:00Z"),
        dict(MR, iid=14, state="closed"),
    ])])
    rows = gl.list_closed_pulls("g", "p", host="gitlab.com")
    assert [r["number"] for r in rows] == [13, 14]
    assert "state=all" in router.calls[0]["target"]


def test_get_pr_detail_adds_the_detail_only_fields(route):
    route([("merge_requests/12", dict(
        MR,
        changes_count="20+",
        commits_count=3,
        user_notes_count=5,
        merged_at="2026-07-05T00:00:00Z",
        detailed_merge_status="mergeable",
        merged_by={"username": "dave"},
        merge_when_pipeline_succeeds=True,
        squash=True,
        merge_user={"username": "erin"},
    ))])
    detail = gl.get_pr_detail("g", "p", 12, host="gitlab.com", resolve_mergeable=False)
    assert detail["changed_files"] == 20
    assert detail["commits"] == 3
    assert detail["comments"] == 5
    assert detail["merged"] is True
    assert detail["mergeable"] is True
    assert detail["mergeable_state"] == "mergeable"
    assert detail["merged_by"] == "dave"
    assert detail["auto_merge"] == {"method": "SQUASH", "enabled_by": "erin"}


def test_get_pr_detail_reports_unknown_sizes_and_no_auto_merge(route):
    route([("merge_requests/12", dict(MR, changes_count="many"))])
    detail = gl.get_pr_detail("g", "p", 12, host="gitlab.com")
    assert detail["changed_files"] is None
    assert detail["auto_merge"] is None
    assert detail["mergeable_state"] == "unknown"
    assert detail["review_comments"] is None


def test_get_pr_detail_raises_on_an_empty_merge_request(route):
    route([("merge_requests/12", {})])
    with pytest.raises(gl.ProviderCliError, match="could not read"):
        gl.get_pr_detail("g", "p", 12, host="gitlab.com")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"detailed_merge_status": "mergeable"}, True),
        ({"merge_status": "can_be_merged"}, True),
        ({"detailed_merge_status": "conflict"}, False),
        ({"merge_status": "checking"}, None),
        ({}, None),
    ],
)
def test_mergeable_reports_pending_as_unknown(raw, expected):
    assert gl._mergeable(raw) is expected


# ── pipelines as checks ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,allow_failure,bucket",
    [
        ("failed", False, "failure"),
        ("failed", True, "other"),
        ("running", False, "running"),
        ("waiting_for_resource", False, "running"),
        ("success", False, "success"),
        ("canceled", False, "other"),
        ("manual", False, "other"),
        ("something-new", False, "other"),
        ("", False, "other"),
    ],
)
def test_job_bucket(status, allow_failure, bucket):
    assert gl._job_bucket(status, allow_failure) == bucket


def test_norm_job_shapes_a_check_row():
    row = gl._norm_job({
        "name": "unit", "status": "failed", "stage": "test", "web_url": "u",
        "created_at": "c", "started_at": "s", "finished_at": "f",
    })
    assert row["status"] == "completed"
    assert row["conclusion"] == "failure"
    assert row["bucket"] == "failure"
    assert row["summary"] == "stage: test"
    assert row["source"] == "gitlab-ci"
    assert row["app"] == "GitLab CI"
    assert row["started_at"] == "s"


def test_norm_job_defaults_a_nameless_running_job():
    row = gl._norm_job({"status": "running", "created_at": "c"})
    assert row["name"] == "job"
    assert row["status"] == "in_progress"
    assert row["conclusion"] is None
    assert row["summary"] == ""
    assert row["started_at"] == "c"


def test_list_pr_checks_uses_the_newest_pipeline_for_the_sha(route):
    router = route([
        ("pipelines/99/jobs", [{"name": "unit", "status": "success"}]),
        ("pipelines?sha=", [
            {"id": 98, "created_at": "2026-07-01T00:00:00Z"},
            {"id": 99, "created_at": "2026-07-02T00:00:00Z"},
        ]),
    ])
    rows = gl.list_pr_checks("g", "p", "abc1234", host="gitlab.com")
    assert [r["name"] for r in rows] == ["unit"]
    assert router.targets("pipelines/99/jobs")


def test_list_pr_checks_rejects_a_non_sha(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="invalid commit sha"):
        gl.list_pr_checks("g", "p", "abc; rm -rf x", host="gitlab.com")
    assert router.calls == []


def test_list_pr_checks_is_empty_without_a_pipeline(route):
    route([("pipelines?sha=", [])])
    assert gl.list_pr_checks("g", "p", "abc1234", host="gitlab.com") == []


def test_list_pr_checks_is_empty_when_the_pipeline_has_no_usable_id(route):
    route([("pipelines?sha=", [{"id": "99"}])])
    assert gl.list_pr_checks("g", "p", "abc1234", host="gitlab.com") == []


def test_summarize_checks_lets_failure_dominate():
    out = gl.summarize_checks([
        {"bucket": "success"}, {"bucket": "running"}, {"bucket": "failure"},
        {"bucket": "invented"}, "not a dict",
    ])
    assert out["checks_state"] == "failure"
    assert out["checks_counts"] == {"failure": 1, "running": 1, "success": 1, "other": 1}
    assert out["checks_truncated"] is False


def test_summarize_checks_reports_no_state_without_checks():
    assert gl.summarize_checks([])["checks_state"] is None


def test_enrich_is_a_no_op_on_gitlab():
    pulls = [{"number": 1}]
    assert gl.enrich_pulls("g", "p", pulls, "open", host="gitlab.com") is pulls
    assert gl.enrich_pulls_by_number("g", "p", pulls, host="gitlab.com") is pulls


def test_enrichment_complete_reads_the_same_invariant_as_github():
    assert gl.enrichment_complete([{"checks_counts": {}}]) is True
    assert gl.enrichment_complete([{"checks_counts": None}]) is False


def test_pipeline_summary_falls_back_to_the_plain_pipeline_key():
    out = gl._pipeline_summary({"pipeline": {"status": "failed"}})
    assert out["checks_state"] == "failure"
    assert out["checks_counts"]["failure"] == 1


def test_pipeline_summary_reports_no_state_without_a_pipeline():
    out = gl._pipeline_summary({})
    assert out["checks_state"] is None
    assert out["checks_counts"] == {"failure": 0, "running": 0, "success": 0, "other": 0}
    # Never 0 for diff size: these rows are persisted, and a zero would read as
    # a confident "no changes".
    assert out["additions"] is None and out["deletions"] is None


# ── poll probe ───────────────────────────────────────────────────────────────


def test_probe_open_list_counts_open_issues_and_the_newest_update(route):
    route([
        ("issues_statistics", {"statistics": {"counts": {"opened": 12}}}),
        ("issues", [{"updated_at": "2026-07-02T00:00:00Z"}]),
    ])
    assert gl.probe_open_list("g", "p", "issue", host="gitlab.com") == {
        "total_count": 12, "top_updated_at": "2026-07-02T00:00:00Z"
    }


def test_probe_open_list_tolerates_an_empty_or_untyped_top_row(route):
    route([("issues_statistics", {"statistics": {"counts": {"opened": 0}}}), ("issues", [])])
    assert gl.probe_open_list("g", "p", "issue", host="gitlab.com")["top_updated_at"] is None
    route([
        ("issues_statistics", {"statistics": {"counts": {"opened": 1}}}),
        ("issues", [{"updated_at": 17}]),
    ])
    assert gl.probe_open_list("g", "p", "issue", host="gitlab.com")["top_updated_at"] is None


def test_probe_open_list_raises_without_a_count(route):
    route([("issues_statistics", {"statistics": {"counts": {}}})])
    with pytest.raises(gl.ProviderCliError, match="no open count"):
        gl.probe_open_list("g", "p", "issue", host="gitlab.com")


def test_probe_open_list_refuses_an_unknown_kind(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="unsupported probe kind"):
        gl.probe_open_list("g", "p", "release", host="gitlab.com")
    assert router.calls == []


def test_probe_open_list_refuses_merge_requests_rather_than_approximating(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="no cheap open merge-request count"):
        gl.probe_open_list("g", "p", "pr", host="gitlab.com")
    assert router.calls == []


# ── merge-request search ─────────────────────────────────────────────────────


def test_build_pr_search_query_maps_every_person_filter():
    query = gl.build_pr_search_query(
        "g", "p", state="merged", author="alice", assignee="bob", review_requested="carol"
    )
    assert query == (
        "state=merged&scope=all&author_username=alice"
        "&assignee_username=bob&reviewer_username=carol"
    )


@pytest.mark.parametrize("state,gl_state", [("open", "opened"), ("closed", "closed"), ("all", "all")])
def test_build_pr_search_query_translates_the_state(state, gl_state):
    query = gl.build_pr_search_query("g", "p", state=state, author="alice")
    assert query.startswith(f"state={gl_state}&scope=all")


def test_build_pr_search_query_rejects_an_unknown_state():
    with pytest.raises(gl.PrSearchError, match="unsupported state"):
        gl.build_pr_search_query("g", "p", state="draft", author="alice")


def test_build_pr_search_query_rejects_an_invalid_username():
    with pytest.raises(gl.PrSearchError, match="invalid GitLab username"):
        gl.build_pr_search_query("g", "p", author="alice&state=all")


def test_build_pr_search_query_requires_a_person_filter():
    with pytest.raises(gl.PrSearchError, match="at least one person filter"):
        gl.build_pr_search_query("g", "p")


def test_search_pulls_honours_the_limit(route):
    route([("merge_requests", [MR, dict(MR, iid=13)])])
    rows = gl.search_pulls("g", "p", host="gitlab.com", author="alice", limit=1)
    assert [r["number"] for r in rows] == [12]


def test_search_pulls_clamps_the_limit_to_one_above_the_cap(route):
    route([("merge_requests", [dict(MR, iid=n) for n in range(5)])])
    rows = gl.search_pulls("g", "p", host="gitlab.com", author="alice", limit=10_000)
    assert len(rows) == 5


def test_search_pulls_drops_merged_rows_from_a_closed_search(route):
    router = route([("merge_requests", [
        dict(MR, iid=13, state="closed"),
        dict(MR, iid=14, state="merged", merged_at="2026-07-05T00:00:00Z"),
    ])])
    rows = gl.search_pulls("g", "p", host="gitlab.com", state="closed", author="alice")
    assert [r["number"] for r in rows] == [13]
    assert "state=closed" in router.calls[0]["target"]


# ── merge-request actions ────────────────────────────────────────────────────


def test_set_pr_state_closes_and_reports_the_draft_flag(route):
    router = route([("merge_requests/12", {"state": "closed", "work_in_progress": True})])
    out = gl.set_pr_state("g", "p", 12, "closed", host="gitlab.com")
    assert out == {"state": "closed", "merged": False, "draft": True}
    assert json.loads(router.calls[0]["input"]) == {"state_event": "close"}


def test_set_pr_state_reopens(route):
    router = route([("merge_requests/12", {"state": "opened"})])
    out = gl.set_pr_state("g", "p", 12, "open", host="gitlab.com")
    assert out["state"] == "open" and out["draft"] is False
    assert json.loads(router.calls[0]["input"]) == {"state_event": "reopen"}


def test_set_pr_state_rejects_an_unknown_state(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="invalid MR state"):
        gl.set_pr_state("g", "p", 12, "merged", host="gitlab.com")
    assert router.calls == []


def test_submit_pr_review_refuses_request_changes(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="no 'request changes' review verb"):
        gl.submit_pr_review("g", "p", 12, "REQUEST_CHANGES", "no", "abc1234", host="gitlab.com")
    assert router.calls == []


def test_submit_pr_review_rejects_an_unknown_event(route):
    with pytest.raises(gl.ProviderCliError, match="invalid review event"):
        gl.submit_pr_review("g", "p", 12, "DISMISS", head_sha="abc1234", host="gitlab.com")


def test_submit_pr_review_requires_a_body_for_a_comment(route):
    with pytest.raises(gl.ProviderCliError, match="requires a comment body"):
        gl.submit_pr_review("g", "p", 12, "COMMENT", "  ", "abc1234", host="gitlab.com")


def test_submit_pr_review_requires_the_head_commit(route):
    with pytest.raises(gl.ProviderCliError, match="refusing to review"):
        gl.submit_pr_review("g", "p", 12, "APPROVE", head_sha="", host="gitlab.com")


def test_submit_pr_review_comment_posts_a_note(route):
    router = route([("notes", {"id": 5, "created_at": "t"})])
    out = gl.submit_pr_review("g", "p", 12, "comment", "looks fine", "abc1234", host="gitlab.com")
    assert out == {"id": None, "state": "COMMENTED", "submitted_at": None}
    assert "merge_requests/12/notes" in router.calls[0]["target"]


def test_submit_pr_review_approves_before_it_posts_the_note(route):
    router = route([
        ("approve", {"id": 3, "created_at": "c", "updated_at": "u"}),
        ("notes", {"id": 5, "created_at": "t"}),
    ])
    out = gl.submit_pr_review("g", "p", 12, "APPROVE", "ship it", "abc1234", host="gitlab.com")
    assert out == {"id": 3, "state": "APPROVED", "submitted_at": "u"}
    # The approval is idempotent, so it goes FIRST: a retry after a failed note
    # must not duplicate the prose.
    assert [t.rsplit("/", 1)[-1] for t in router.targets()] == ["approve", "notes"]
    assert json.loads(router.calls[0]["input"]) == {"sha": "abc1234"}


def test_submit_pr_review_approves_without_a_note(route):
    router = route([("approve", {"id": 3, "created_at": "c"})])
    out = gl.submit_pr_review("g", "p", 12, "APPROVE", "", "abc1234", host="gitlab.com")
    assert out["submitted_at"] == "c"
    assert router.targets("notes") == []


def test_add_note_requires_a_body(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="comment needs a body"):
        gl.add_issue_comment("g", "p", 7, "   ", host="gitlab.com")
    assert router.calls == []


def test_add_issue_comment_and_add_pr_comment_use_separate_collections(route):
    router = route([("notes", {"id": 5, "created_at": "t"})])
    assert gl.add_issue_comment("g", "p", 7, "hi", host="gitlab.com") == {
        "id": 5, "url": None, "created_at": "t"
    }
    gl.add_pr_comment("g", "p", 7, "hi", host="gitlab.com")
    assert "issues/7/notes" in router.calls[0]["target"]
    assert "merge_requests/7/notes" in router.calls[1]["target"]


def test_merge_pull_request_pins_the_squash_flag_and_the_sha(route):
    router = route([("merge", {"state": "merged", "merge_commit_sha": "feed123"})])
    out = gl.merge_pull_request("g", "p", 12, "SQUASH", "abc1234", host="gitlab.com")
    assert out == {"merged": True, "sha": "feed123", "message": ""}
    assert json.loads(router.calls[0]["input"]) == {"squash": True, "sha": "abc1234"}


def test_merge_pull_request_sends_squash_false_explicitly(route):
    router = route([("merge", {"state": "opened", "merge_error": "conflict"})])
    out = gl.merge_pull_request("g", "p", 12, "merge", "abc1234", host="gitlab.com")
    assert out == {"merged": False, "sha": None, "message": "conflict"}
    assert json.loads(router.calls[0]["input"])["squash"] is False


def test_merge_pull_request_refuses_a_method_gitlab_cannot_honour(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="invalid merge method"):
        gl.merge_pull_request("g", "p", 12, "REBASE", "abc1234", host="gitlab.com")
    assert "REBASE" not in gl.PR_MERGE_METHODS
    assert router.calls == []


def test_merge_pull_request_requires_the_reviewed_commit(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="refusing to merge"):
        gl.merge_pull_request("g", "p", 12, "SQUASH", "nope", host="gitlab.com")
    assert router.calls == []


def test_auto_merge_is_refused_in_both_directions(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="no separate auto-merge to arm"):
        gl.enable_auto_merge("g", "p", 12, host="gitlab.com")
    with pytest.raises(gl.ProviderCliError, match="not managed from this app"):
        gl.disable_auto_merge("g", "p", 12, host="gitlab.com")
    assert router.calls == []


# ── pipelines as workflow runs ───────────────────────────────────────────────


def test_list_pr_workflow_runs_normalizes_status_and_conclusion(route):
    route([("pipelines?sha=", [
        {"id": 1, "status": "running", "iid": 5, "web_url": "u", "source": "push",
         "created_at": "c"},
        {"id": 2, "status": "canceled", "name": "nightly"},
        {"id": 3, "status": "success"},
        {"id": 4, "status": "skipped"},
        {"no_id": True},
        "not a dict",
    ])])
    rows = gl.list_pr_workflow_runs("g", "p", "abc1234", host="gitlab.com")
    by_id = {row["id"]: row for row in rows}
    assert len(rows) == 4
    assert by_id[1]["status"] == "running"
    assert by_id[1]["conclusion"] is None
    assert by_id[1]["cancellable"] is True and by_id[1]["rerunnable"] is False
    assert by_id[1]["name"] == "pipeline #5"
    # GitLab spells it with one l; the shared UI compares the GitHub spelling.
    assert by_id[2]["conclusion"] == "cancelled"
    assert by_id[2]["status"] == "completed"
    assert by_id[2]["rerunnable"] is True
    assert by_id[3]["conclusion"] == "success" and by_id[3]["rerunnable"] is False
    assert by_id[4]["conclusion"] == "skipped" and by_id[4]["rerunnable"] is False


def test_list_pr_workflow_runs_rejects_a_non_sha(route):
    router = route([])
    with pytest.raises(gl.ProviderCliError, match="invalid commit sha"):
        gl.list_pr_workflow_runs("g", "p", "../etc", host="gitlab.com")
    assert router.calls == []


def test_cancel_workflow_run_posts_to_the_pipeline(route):
    router = route([("cancel", {})])
    assert gl.cancel_workflow_run("g", "p", 99, host="gitlab.com") == {
        "run_id": 99, "cancelled": True
    }
    assert "pipelines/99/cancel" in router.calls[0]["target"]
    assert "POST" in router.calls[0]["argv"]


def test_rerun_workflow_run_reports_what_gitlab_actually_does(route):
    router = route([("retry", {})])
    out = gl.rerun_workflow_run("g", "p", 99, failed_only=False, host="gitlab.com")
    # GitLab's /retry only retries failed and canceled jobs, so the answer is what
    # happened -- not what was asked for.
    assert out == {"run_id": 99, "rerun": True, "failed_only": True}
    assert "pipelines/99/retry" in router.calls[0]["target"]


# ── module-level parity constants ────────────────────────────────────────────


def test_the_historical_gh_aliases_are_the_same_classes():
    assert gl.GhCliError is gl.ProviderCliError
    assert gl.GhSetupError is gl.ProviderSetupError
    assert gl.GhPermissionError is gl.ProviderPermissionError
    assert gl.PR_REVIEW_EVENTS == ("APPROVE", "COMMENT")

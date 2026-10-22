"""The repository guards keyed to ``dashboard/server.py`` keep reaching its owners.

This file pins that each construct a guard reads by path stays in the file the guard
reads, that every review rule on the server also covers its owners (with the globstar
matcher read as the reviewers read it), that the container smoke lane fires on the
owners, that the async config-dir guard scans every owner coroutine, that the
redaction census classifies the owners, and that each route symbol the RFCs cite
resolves at its home. The suite is described in
``test_dashboard_server_composition_contract.py``.
"""

from __future__ import annotations

import ast
import importlib
import re

import pytest
from dashboard_server_composition_contract_helpers import (
    _FACADE_PATH,
    _OWNER_DIR,
    _owner_sources,
)
from source_corpus import repo_root

# ── the guards keep their reach ───────────────────────────────────────────────

#: Constructs repository guards read by path, with the count each guard asserts and
#: the file that holds each (``server`` is ``dashboard/server.py``, the other stems are
#: its ``server_runtime`` owners and its two pure-data modules): the strict
#: internal-path literal, the single heap-trim maintainer after the serving bind, the
#: diag recorder's off switch, the channel manager's broadcast hand-off, the
#: inherited-trust teardown and its wiring, the crew-log and browser-view drains, both
#: runner constructions, both Kiro prerequisite seeds, both skill-read observers, the
#: migrated-builtin sweep, the browser CLI override, the per-boot secret mint, the
#: dashboard contributor seam and both cron-folder loads. Each guard reads the file
#: named here, so a construct that moved anywhere else would leave the guard's sight.
_GUARDED_CONSTRUCTS = (
    (r"^_STRICT_INTERNAL_API_PATHS = frozenset\($", 1, {"server_internal_paths": 1}),
    (r"platform_compat\.HeapTrimMaintainer\(\)", 1, {"server": 1}),
    (
        r'os\.environ\.get\("KIROCREW_DIAG_RECORDER", ""\)\.strip\(\)\.lower\(\) in \(',
        1,
        {"server": 1},
    ),
    (r"broadcast_fn=state\.broadcast_ws", 1, {"server": 1}),
    (r"clear_trusted_sessions\(keep_policy=standing_trust\)", 1, {"inherited_trust": 1}),
    (r"functools\.partial\(_clear_override_derived_trust, state\)$", 1, {"server": 1}),
    (r"functools\.partial\(_suspend_override_derived_trust, state\)$", 1, {"server": 1}),
    (r"^    def _on_override_expired\(source: str\) -> None:$", 1, {"server": 1}),
    (r"app\.on_cleanup\.append\(_crew_log_drain\)", 1, {"instances_lifecycle": 1}),
    (r"app\.on_cleanup\.append\(_browser_view_shutdown\)", 1, {"browser_lifecycle": 1}),
    (r"build_hardened_runner\(app", 2, {"server": 1, "api_entrypoint": 1}),
    (r"KiroPrerequisiteService,$", 2, {"server": 1, "api_entrypoint": 1}),
    (r"register_skill_read_observer\(", 2, {"server": 1, "api_entrypoint": 1}),
    (r"^    from kiro_crew\.apps\.builtins import _MIGRATED_BUILTINS$", 1, {"server": 1}),
    (r"asyncio\.to_thread\(browser_cli_launch\.cli_env_overrides\)", 1, {"server": 1}),
    (r"_internal_secret = os\.urandom\(16\)\.hex\(\)$", 2, {"server": 1, "api_entrypoint": 1}),
    (r"current_context\(\)\.dashboard\.contribute_routes\(app\)", 1, {"server": 1}),
    (
        r"await asyncio\.to_thread\(state\.load_cron_folders\)",
        2,
        {"server": 1, "api_entrypoint": 1},
    ),
)


#: The server's two pure-data modules, beside it in ``dashboard/``.
_DATA_STEMS = ("server_internal_paths", "server_response_headers")


def _guarded_sources() -> dict[str, str]:
    """The server, its owners and its two pure-data modules, by stem."""
    data = {
        stem: (_FACADE_PATH.parent / f"{stem}.py").read_text(encoding="utf-8")
        for stem in _DATA_STEMS
    }
    return {"server": _FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources(), **data}


@pytest.mark.parametrize(("pattern", "count", "homes"), _GUARDED_CONSTRUCTS)
def test_a_construct_a_guard_reads_stays_where_the_guard_reads_it(
    pattern: str, count: int, homes: dict[str, int]
) -> None:
    assert sum(homes.values()) == count
    found = {
        stem: len(re.findall(pattern, source, re.MULTILINE))
        for stem, source in _guarded_sources().items()
    }
    assert {stem: n for stem, n in found.items() if n} == homes


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE or path-filter pattern as a regex: ``**/`` spans zero or more
    directories, ``*`` and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _owner_paths() -> list[str]:
    """The owner files and the server's two pure-data modules: every file the server's
    one-module body now lives in besides ``server.py`` itself."""
    root = repo_root()
    data = [_FACADE_PATH.parent / f"{stem}.py" for stem in _DATA_STEMS]
    return sorted(path.relative_to(root).as_posix() for path in [*_OWNER_DIR.glob("*.py"), *data])


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the server, {id: owner files those rules miss})``."""
    facade = "src/kiro_crew/dashboard/server.py"
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        if not any(p.match(facade) for p in patterns):
            continue
        matched.add(rule["id"])
        gaps = [o for o in _owner_paths() if not any(p.match(o) for p in patterns)]
        if gaps:
            missing[rule["id"]] = gaps
    return matched, missing


def _autosde_rules() -> list[dict]:
    import yaml

    root = repo_root()
    return [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((root / name).read_text(encoding="utf-8"))["custom-rules"]
    ]


def test_the_globstar_matcher_reads_patterns_as_the_reviewers_do() -> None:
    assert _globstar("src/a/**/*.py").match("src/a/b.py")
    assert _globstar("src/a/**/*.py").match("src/a/x/y/b.py")
    assert not _globstar("src/a/*.py").match("src/a/x/b.py")
    assert _globstar("src/a/**").match("src/a/x/b.py")


def test_every_review_rule_on_the_facade_also_covers_its_owners() -> None:
    """A rule that reviews the server reviews the code composed into it: an owner
    outside its patterns would take moved code out of that rule's sight."""
    rules = _autosde_rules()
    matched, missing = _rules_missing_owners(rules)
    assert "no-new-work-on-gateway-boot-path" in matched
    assert missing == {}
    narrowed = [
        (
            {
                **rule,
                "file-patterns": [p for p in rule["file-patterns"] if "server_runtime" not in p],
            }
            if rule["id"] == "no-new-work-on-gateway-boot-path"
            else rule
        )
        for rule in rules
    ]
    assert "no-new-work-on-gateway-boot-path" in _rules_missing_owners(narrowed)[1]


def test_the_container_smoke_lane_fires_on_the_owners() -> None:
    """The container contract's probe exemption is built by an owner, so the path
    filter that names the server names its owners too."""
    import yaml

    workflow = yaml.safe_load(
        (repo_root() / ".github/workflows/docker-smoke.yml").read_text(encoding="utf-8")
    )
    on = workflow.get("on", workflow.get(True))
    patterns = [_globstar(p) for p in on["pull_request"]["paths"]]
    assert any(p.match("src/kiro_crew/dashboard/server.py") for p in patterns)
    assert [o for o in _owner_paths() if not any(p.match(o) for p in patterns)] == []


def test_the_async_config_dir_guard_scans_every_owner_coroutine() -> None:
    """``test_no_config_dir_in_async`` checks only the files it lists, so an owner that
    defines a coroutine and is missing from the list would take it out of the guard."""
    import test_no_config_dir_in_async as guard

    listed = set(guard._ASYNC_CHECKED_FILES)
    with_coroutines = {
        f"dashboard/server_runtime/{stem}.py"
        for stem, source in _owner_sources().items()
        if any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(ast.parse(source)))
    }
    assert len(with_coroutines) >= 20
    assert "dashboard/server.py" in listed
    assert with_coroutines - listed == set()


def test_the_redaction_census_classifies_the_owners() -> None:
    """An owner that calls a redactor is classified where the server's entry was."""
    from kiro_crew import security_posture

    rel = {
        f"dashboard/server_runtime/{stem}.py"
        for stem, source in _owner_sources().items()
        if re.search(r"\bStreamRedactor\(|\b\w*redact\w*\(|\.redact\(", source)
    }
    assert rel == {
        "dashboard/server_runtime/owner_notices.py",
        "dashboard/server_runtime/workflow_startup.py",
    }
    assert rel <= security_posture.NON_EGRESS_REDACTION_MODULES
    assert "dashboard/server.py" not in security_posture.NON_EGRESS_REDACTION_MODULES


#: Route symbols the request-for-change documents cite by name rather than by line,
#: with the file each resolves in and a route the symbol registers there.
_RFC_CITED_SYMBOLS = (
    (
        "docs/request-for-change/rfc-resumable-subagent-sessions.md",
        "kiro_crew.dashboard.server_runtime.mcp_routes",
        "_register_mcp_routes",
        ("POST", "/api/spawn"),
    ),
    (
        "docs/request-for-change/rfc-resumable-subagent-sessions.md",
        "kiro_crew.dashboard.routes.chat",
        "register",
        ("POST", "/api/chat"),
    ),
)


@pytest.mark.parametrize(("doc", "module", "symbol", "route"), _RFC_CITED_SYMBOLS)
def test_a_route_symbol_an_rfc_cites_resolves_at_its_home(
    doc: str, module: str, symbol: str, route: tuple[str, str]
) -> None:
    """The cited name stands in the document beside its file, the file defines it, and
    it registers the route the document cites it for. The document cites no line of
    ``dashboard/server.py``, whose length the docs linter would otherwise hold it to."""
    from aiohttp import web

    text = (repo_root() / doc).read_text(encoding="utf-8")
    path = module.replace(".", "/").removeprefix("kiro_crew/") + ".py"
    assert f"`{symbol}` in `{path}`" in text
    assert re.search(r"dashboard/server\.py:\d", text) is None
    register = getattr(importlib.import_module(module), symbol, None)
    assert callable(register)
    app = web.Application()
    register(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
    assert route in routes

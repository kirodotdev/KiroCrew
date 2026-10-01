"""The shape of Issue Radar's route layer: ``backend.routes`` as the facade over
``backend/http_routes``.

``backend.routes`` keeps the request gates, ``/connect``, the list-poll probe
machinery and ``register_routes``; the handlers live in ``http_routes``, one module
per responsibility. What makes that split safe is structural, so it is pinned here:

  * the route table ``register_routes`` builds is unchanged -- every path, method,
    order, and the ``_require_enabled`` wrapper around the facade's own handler;
  * every name ``backend.routes`` has ever defined still resolves on it, and a name
    re-exported from ``http_routes`` is the SAME object, so identity-pinned state
    (the probe memo, the refresh-task ``AppKey``) and ``except`` classes agree;
  * no ``http_routes`` module binds a monkeypatch seam or a facade-owned gate
    itself -- each is looked up on ``backend.routes`` at call time, and the only
    route to it is a function-local import, so the module graph stays acyclic;
  * the lazily imported model/redaction helpers stay function-local and every
    module logs as ``kirocrew.app.issue-radar``.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins import issue_radar
from kiro_crew.apps.builtins.issue_radar.backend import crew_routes, http_routes, routes

LOGGER = "kirocrew.app.issue-radar"
BACKEND = Path(inspect.getfile(routes)).parent
HTTP_ROUTES = BACKEND / "http_routes"

#: The route table ``register_routes`` builds, in registration order: this app's own
#: routes, then the crew surface, then the pipeline dashboard. ``add_get`` also
#: registers a HEAD twin for every GET; those are left out.
MAIN_ROUTES = [
    ("POST", "/connect", "_handle_connect"),
    ("GET", "/issues", "_handle_issues"),
    ("GET", "/issue", "_handle_issue_detail"),
    ("GET", "/pulls", "_handle_pulls"),
    ("GET", "/pulls/search", "_handle_pulls_search"),
    ("GET", "/pull", "_handle_pull_detail"),
    ("GET", "/ref", "_handle_ref_summary"),
    ("GET", "/deps", "_handle_deps"),
    ("GET", "/labels", "_handle_labels"),
    ("GET", "/members", "_handle_members"),
    ("GET", "/repos", "_handle_repos"),
    ("GET", "/recent-repos", "_handle_recent_repos"),
    ("DELETE", "/repos", "_handle_disconnect"),
    ("GET", "/me", "_handle_me"),
    ("GET", "/settings", "_handle_get_settings"),
    ("PUT", "/settings", "_handle_put_settings"),
    ("POST", "/settings/role", "_handle_add_settings_label"),
    ("GET", "/issue-ai", "_handle_issue_ai"),
    ("GET", "/pull-ai", "_handle_pull_ai"),
    ("POST", "/labels/apply", "_handle_labels_apply"),
    ("POST", "/issue/state", "_handle_issue_state"),
    ("POST", "/issue/assignees", "_handle_issue_assignees"),
    ("POST", "/pull/state", "_handle_pull_state"),
    ("POST", "/pull/review", "_handle_pull_review"),
    ("POST", "/pull/comment", "_handle_pull_comment"),
    ("POST", "/pull/merge", "_handle_pull_merge"),
    ("POST", "/pull/auto-merge", "_handle_pull_auto_merge"),
    ("GET", "/pull/runs", "_handle_pull_runs"),
    ("POST", "/pull/run", "_handle_pull_run_action"),
    ("POST", "/pulls/bulk", "_handle_pulls_bulk"),
    ("GET", "/investigation", "_handle_get_investigation"),
    ("PUT", "/investigation", "_handle_put_investigation"),
    ("GET", "/recommendations", "_handle_get_recommendations"),
    ("POST", "/recommendations", "_handle_generate_recommendations"),
    ("POST", "/labels/create", "_handle_create_label"),
    ("GET", "/tagging", "_handle_get_tagging"),
    ("POST", "/tagging", "_handle_generate_tagging"),
    ("POST", "/labels/apply-bulk", "_handle_labels_apply_bulk"),
]
TAIL_ROUTES = [
    ("GET", "/crews"),
    ("POST", "/crews"),
    ("GET", "/crews/names"),
    ("GET", "/crews/settings"),
    ("PUT", "/crews/settings"),
    ("GET", "/crew"),
    ("GET", "/crew/fabric"),
    ("PUT", "/crew"),
    ("DELETE", "/crew"),
    ("PUT", "/crew/work"),
    ("POST", "/crew/pause"),
    ("POST", "/issue/comment"),
    ("GET", "/pipeline/overview"),
    ("GET", "/pipeline/step"),
    ("GET", "/pipeline/item/sessions"),
]

#: Every module-level name ``backend.routes`` defined before the handlers moved to
#: ``http_routes``. Tests, ``crew_routes`` and other modules' docs cite these as
#: ``routes.<name>``, so each one still resolves on the facade.
HISTORIC_NAMES = (
    "logger GhCliError GhPermissionError GhSetupError PrSearchError GhInvalidInputError "
    "_account_key _key_from_request _str_field _key_from_body _scope _st _identity _connected "
    "_require_enabled _audit MAX_ITEM_NUMBER _parse_item_number _load_members _handle_connect "
    "LIST_POLL_MAX_STALENESS_SEC _REPO_HEAL_CONCURRENCY _PROBE_COALESCE_SEC _ProbeKey _probe_memo "
    "_probe_inflight _probe_lock _remember_probe _coalesced_probe _poll_can_serve_cache "
    "_handle_issues _handle_issues_first_page _handle_labels _handle_members _handle_repos "
    "_handle_me _handle_recent_repos _handle_get_settings _handle_put_settings _handle_disconnect "
    "_handle_issue_detail _handle_pulls _handle_pulls_first_page _handle_pulls_search "
    "_handle_pull_detail _handle_ref_summary _deps_node_hints _DepsRefreshTasks "
    "_DEPS_REFRESH_TASKS_APP_KEY _DepsRebuildLocks _DEPS_REBUILD_LOCKS_APP_KEY _deps_reg_key "
    "_deps_refresh_registry _deps_rebuild_lock _DepsScopeUnavailable _rebuild_deps "
    "_schedule_deps_refresh _stop_deps_refreshes _handle_deps _has_write_access _repo_can_write "
    "_AI_BODY_MAX_CHARS _AI_MAX_SUGGESTIONS _ui_language _LANG_HINT_FIELD _hint_language "
    "_resolve_ui_language _language_directive _build_ai_prompt _run_oneshot_model "
    "_compute_issue_ai _load_detail_for_ai _load_labels_for_ai _handle_issue_ai "
    "_PR_AI_BODY_MAX_CHARS _PR_AI_COMMENT_MAX_CHARS _PR_AI_MAX_COMMENTS _PR_AI_MAX_VERDICTS "
    "_pr_ai_comment_rows _pr_ai_fingerprint _pr_lifecycle _build_pr_ai_prompt _compute_pr_ai "
    "_handle_pull_ai _apply_label_change _reread_labels_and_patch _handle_labels_apply "
    "_handle_issue_state MAX_ASSIGNEES _replace_assignees_checked _handle_issue_assignees "
    "_item_kind _handle_get_investigation _handle_put_investigation _RECO_ISSUE_SAMPLE "
    "_RECO_BODY_MAX_CHARS _RECO_MAX _RECO_CATEGORIES _RECO_MAX_EXAMPLES _DEFAULT_CATEGORY_COLOR "
    "_valid_hex6 _RATIONALE_MAX_CHARS _ISSUE_CITATION_RE _ISSUE_REF_RE _short_rationale "
    "_build_reco_prompt _compute_label_recommendations _load_open_issues_for_reco "
    "_handle_get_recommendations _handle_generate_recommendations _TAG_BATCH_MAX "
    "_TAG_BODY_MAX_CHARS _TAG_MAX_PER_ISSUE _TAG_BULK_MAX _untagged _build_tagging_prompt "
    "_compute_tagging_suggestions _handle_get_tagging _handle_generate_tagging "
    "_handle_labels_apply_bulk _handle_create_label _handle_add_settings_label MAX_RUN_ID "
    "_BULK_PR_ACTIONS _BULK_PR_MAX _PR_BODY_MAX_CHARS _HEAD_SHA_RE _PINNED_BULK_PR_ACTIONS "
    "_pr_numbers_field _pr_head_shas_field _pr_body_field _pr_action_preamble _pr_action_error "
    "_run_pr_action _pr_number_field _pr_head_sha_field _pr_merge_method_field _handle_pull_state "
    "_refuse_if_head_moved _handle_pull_review _handle_pull_comment _handle_pull_auto_merge "
    "_MERGE_ALLOWED_STATES _handle_pull_merge _handle_pull_runs _handle_pull_run_action "
    "_handle_pulls_bulk register_routes"
).split()

#: Names a caller patches on ``backend.routes`` and expects every call site to
#: honour, plus every gate ``backend.routes`` owns. An ``http_routes`` module must
#: reach each one as ``routes.<name>`` inside a function, never through a binding
#: of its own.
FACADE_OWNED = {
    "GhCliError",
    "GhPermissionError",
    "GhSetupError",
    "PrSearchError",
    "GhInvalidInputError",
    "_account_key",
    "_key_from_request",
    "_str_field",
    "_key_from_body",
    "_scope",
    "_st",
    "_identity",
    "_connected",
    "_require_enabled",
    "_audit",
    "MAX_ITEM_NUMBER",
    "_parse_item_number",
    "LIST_POLL_MAX_STALENESS_SEC",
    "_PROBE_COALESCE_SEC",
    "_probe_memo",
    "_probe_inflight",
    "_probe_lock",
    "_remember_probe",
    "_coalesced_probe",
    "_poll_can_serve_cache",
    "_has_write_access",
    "_repo_can_write",
    "_handle_connect",
    "is_app_enabled",
}
PATCH_SEAMS = {
    "_TAG_BATCH_MAX",
    "_apply_label_change",
    "_compute_issue_ai",
    "_compute_label_recommendations",
    "_compute_pr_ai",
    "_compute_tagging_suggestions",
    "_handle_issues_first_page",
    "_handle_pulls_first_page",
    "_load_detail_for_ai",
    "_load_labels_for_ai",
    "_load_members",
    "_load_open_issues_for_reco",
    "_rebuild_deps",
    "_refuse_if_head_moved",
    "_replace_assignees_checked",
    "_reread_labels_and_patch",
    "_run_oneshot_model",
    "_run_pr_action",
    "_schedule_deps_refresh",
    "_ui_language",
}

#: Lazily imported inside the functions that use them: the model helpers and the
#: redactor are heavy and the gateway boot path must not pay for them.
LAZY_MODULES = {"kiro_crew.llm_helpers", "kiro_crew.security", "uuid"}


def _modules():
    """``(name, path, tree)`` for ``routes.py`` and every ``http_routes`` module."""
    paths = [BACKEND / "routes.py", *sorted(HTTP_ROUTES.glob("*.py"))]
    return [(p.stem, p, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def _http_modules():
    return [m for m in _modules() if m[1].parent == HTTP_ROUTES]


def _owner_modules():
    """The ``http_routes`` owner modules themselves (the package ``__init__`` holds
    no code), imported by their dotted name."""
    return [
        (name, path, tree, __import__(f"{http_routes.__name__}.{name}", fromlist=["_"]))
        for name, path, tree in _http_modules()
        if name != "__init__"
    ]


def _top_level_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _registered(app: web.Application):
    return [
        (r.method, r.resource.canonical, r.handler)
        for r in app.router.routes()
        if r.method != "HEAD"
    ]


# ── the route table ──────────────────────────────────────────────────────────


def test_register_routes_builds_the_same_table_in_the_same_order():
    app = web.Application()
    with mock.patch.object(routes, "watch"):
        routes.register_routes(app)
    table = _registered(app)
    prefix = "/api/apps/issue-radar"
    expected = [(m, prefix + p) for m, p, _ in MAIN_ROUTES] + [
        (m, prefix + p) for m, p in TAIL_ROUTES
    ]
    assert [(m, p) for m, p, _ in table] == expected
    for (method, path, handler), (_m, _p, name) in zip(table, MAIN_ROUTES):
        assert handler.__wrapped__ is getattr(routes, name), (method, path)
        assert handler.__name__ == name


@pytest.mark.asyncio
async def test_every_registered_route_is_refused_while_the_app_is_disabled():
    app = web.Application()
    with mock.patch.object(routes, "watch"):
        routes.register_routes(app)
    # The crew surface is wrapped in this facade's gate too; the pipeline dashboard
    # carries its own copy of it, so it is not part of this table.
    gated = _registered(app)[: len(MAIN_ROUTES) + len(TAIL_ROUTES) - 3]
    with mock.patch.object(routes, "is_app_enabled", return_value=False) as enabled:
        for method, path, handler in gated:
            resp = await handler(make_mocked_request(method, path, app=app))
            assert resp.status == 403, (method, path)
    assert enabled.call_count == len(gated)


def test_the_package_and_the_manifest_reach_the_facade_registrar():
    assert issue_radar.register_routes is routes.register_routes
    manifest = (BACKEND.parent / "app.json").read_text(encoding="utf-8")
    assert '"routes": "backend.routes:register_routes"' in manifest


# ── the facade surface ───────────────────────────────────────────────────────


def test_every_historic_name_still_resolves_on_the_facade():
    missing = [name for name in HISTORIC_NAMES if not hasattr(routes, name)]
    assert missing == []


def test_re_exported_names_are_the_owning_modules_objects():
    owners: dict[str, str] = {}
    for name, _path, tree, module in _owner_modules():
        for symbol in _top_level_names(tree):
            if symbol == "logger":
                continue
            assert symbol not in owners, f"{symbol} defined in {owners[symbol]} and {name}"
            owners[symbol] = name
            assert getattr(routes, symbol) is getattr(module, symbol), symbol
    facade = set(_top_level_names(ast.parse((BACKEND / "routes.py").read_text(encoding="utf-8"))))
    # One definition per name: nothing lives both in the facade and in an owner, and
    # every historic name is defined in exactly one of the two.
    assert facade.isdisjoint(set(owners) - {"logger"})
    assert sorted(set(HISTORIC_NAMES) - facade - set(owners)) == []


def test_identity_pinned_state_is_shared():
    from kiro_crew.apps.builtins.issue_radar.backend import github_client
    from kiro_crew.apps.builtins.issue_radar.backend.http_routes import deps

    assert routes._DEPS_REFRESH_TASKS_APP_KEY is deps._DEPS_REFRESH_TASKS_APP_KEY
    assert routes._DEPS_REBUILD_LOCKS_APP_KEY is deps._DEPS_REBUILD_LOCKS_APP_KEY
    assert issubclass(routes._DepsScopeUnavailable, routes.GhCliError)
    for alias in (
        "GhCliError",
        "GhPermissionError",
        "GhSetupError",
        "PrSearchError",
        "GhInvalidInputError",
    ):
        assert getattr(routes, alias) is getattr(github_client, alias)


def test_every_facade_name_crew_routes_reads_exists():
    source = inspect.getsource(crew_routes)
    # ``routes.py`` in a comment is a file name, not an attribute read.
    used = set(re.findall(r"\broutes\.([A-Za-z_][A-Za-z0-9_]*)", source)) - {"py"}
    assert used, "crew_routes no longer reads the facade?"
    assert sorted(n for n in used if not hasattr(routes, n)) == []


# ── seam discipline and the import graph ──────────────────────────────────────


def _annotation_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        for ann in (getattr(node, "returns", None), getattr(node, "annotation", None)):
            if ann is not None:
                ids |= {id(x) for x in ast.walk(ann)}
    return ids


def test_no_http_routes_module_binds_a_seam_or_a_facade_gate():
    offenders: list[str] = []
    guarded = FACADE_OWNED | PATCH_SEAMS
    for name, _path, tree in _http_modules():
        skip = _annotation_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in guarded
                and id(node) not in skip
            ):
                offenders.append(f"{name}.py:{node.lineno} {node.id}")
            if isinstance(node, ast.ImportFrom) and any(a.name in guarded for a in node.names):
                offenders.append(f"{name}.py:{node.lineno} imports {[a.name for a in node.names]}")
    assert offenders == [], "reach these through backend.routes at call time:\n" + "\n".join(
        offenders
    )


def test_every_facade_attribute_an_owner_reads_exists():
    missing: list[str] = []
    for name, _path, tree in _http_modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "routes"
                and not hasattr(routes, node.attr)
            ):
                missing.append(f"{name}.py:{node.lineno} routes.{node.attr}")
    assert missing == []


def _module_level_imports(tree: ast.Module) -> list[ast.ImportFrom | ast.Import]:
    return [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]


def test_the_facade_is_imported_only_inside_functions():
    for name, _path, tree in _http_modules():
        for node in _module_level_imports(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.level == 2 and any(a.name == "routes" for a in node.names)), name
                assert not (node.module or "").endswith((".routes", "crew_routes")), name
        local = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and n.level == 2
            and any(a.name == "routes" for a in n.names)
        ]
        for node in local:
            assert node.module is None and [a.name for a in node.names] == ["routes"], name


def test_the_owner_modules_form_an_acyclic_graph():
    edges: dict[str, set[str]] = {}
    for name, _path, tree in _http_modules():
        deps: set[str] = set()
        for node in _module_level_imports(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                deps.add(node.module)
        edges[name] = deps
    done: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        assert node not in trail, " -> ".join((*trail, node))
        if node in done:
            return
        for dep in edges.get(node, ()):
            visit(dep, (*trail, node))
        done.add(node)

    for node in edges:
        visit(node, ())


def test_lazy_imports_stay_function_local():
    for name, _path, tree in _modules():
        for node in _module_level_imports(tree):
            imported = (
                {a.name for a in node.names} if isinstance(node, ast.Import) else {node.module}
            )
            assert not (imported & LAZY_MODULES), f"{name}.py hoists {imported & LAZY_MODULES}"
    for fn in (
        routes._run_oneshot_model,
        routes._compute_issue_ai,
        routes._compute_pr_ai,
        routes._compute_label_recommendations,
        routes._compute_tagging_suggestions,
        routes._short_rationale,
    ):
        body = ast.parse(inspect.getsource(fn).lstrip())
        local = {
            (n.module if isinstance(n, ast.ImportFrom) else n.names[0].name)
            for n in ast.walk(body)
            if isinstance(n, (ast.Import, ast.ImportFrom))
        }
        assert local & LAZY_MODULES, fn.__name__


def test_every_route_module_logs_to_the_app_logger():
    for name, _path, _tree, module in _owner_modules():
        if hasattr(module, "logger"):
            assert module.logger.name == LOGGER, name
    assert routes.logger.name == LOGGER


# ── the import arrangement, in a fresh interpreter ─────────────────────────────


def test_the_package_import_loads_every_owner_but_defers_crew_routes(tmp_path):
    """Importing the app package (what the gateway does at boot) loads the facade
    and every owner module but NOT ``crew_routes``: that import stays inside
    ``register_routes``, because ``crew_routes`` imports the facade back."""
    script = (
        "import sys\n"
        "import kiro_crew.apps.builtins.issue_radar as ir\n"
        "prefix = 'kiro_crew.apps.builtins.issue_radar.backend.'\n"
        "assert prefix + 'crew_routes' not in sys.modules\n"
        "owners = sorted(m[len(prefix) + len('http_routes.'):] for m in sys.modules\n"
        "                if m.startswith(prefix + 'http_routes.'))\n"
        "from aiohttp import web\n"
        "from kiro_crew.apps.builtins.issue_radar.backend import routes\n"
        "app = web.Application()\n"
        "ir.register_routes(app)\n"
        "crew = sys.modules[prefix + 'crew_routes']\n"
        "assert crew.routes is routes\n"
        "print(','.join(owners), len([r for r in app.router.routes() if r.method != 'HEAD']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    owners = sorted(p.stem for p in HTTP_ROUTES.glob("*.py") if p.stem != "__init__")
    assert result.stdout.split() == [",".join(owners), str(len(MAIN_ROUTES) + len(TAIL_ROUTES))]

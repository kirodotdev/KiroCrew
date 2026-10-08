"""The dashboard file handlers keep their surface and contracts while their owners move.

``kiro_crew.dashboard.handlers.files`` is the file API's import path and its patch
surface. Most of what it defined now lives in the modules of
``kiro_crew.dashboard.file_api``, one responsibility each, and ``file_api.compose``
runs every function they define on the facade's globals. These tests pin:

* the surface: every name the facade bound before the split still resolves on it,
  the routes and the package re-exports dispatch to the facade's objects, and the
  seams other modules import keep their identity;
* the composition: every owner function runs on the facade's globals, reads only
  names the facade binds, and captures no name a test rebinds on the facade;
* the guards: what repository guards read in ``files.py`` by path stays there, and
  each guard re-keyed or widened to the owners still sees the code it guards.
"""

from __future__ import annotations

import ast
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import re
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.handlers as handlers_pkg
import kiro_crew.dashboard.handlers.files as files
from kiro_crew.dashboard import file_api
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = files.__name__
_FACADE_PATH = Path(files.__file__).resolve()
_OWNER_PACKAGE = file_api.__name__
_OWNER_DIR = Path(file_api.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[3]

#: Every module-level name ``files`` bound at the base the split was cut from: what
#: it defined and what it imported, private names included, because routes, the
#: handlers package, sibling handlers and tests read private names off it too. A
#: name bound only by ``import <module>`` of a stdlib or third-party module is left
#: out: nothing reads ``json`` or ``os`` off the facade, and pinning one would fail
#: on the removal of an unused import.
_BASE_NAMES = frozenset("""
        BINARY_MIME_ALLOWLIST BinaryIO BodyPartReader Callable
        ClientConnectionResetError DashboardState FILE_READ_SCHEMA FileTooLargeError
        KiroCrewConfig LINK_PATTERNS_MAX LINK_PATTERN_PATTERN_MAX_LEN
        LINK_PATTERN_URL_MAX_LEN MODEL_ID_RE NamedTuple OutboundFile Path PdfExtraction
        PurePath SNIFF_BYTES TypeVar VALID_MEMORY_MODES ValidationError WorkspaceConfig
        WorkspaceDirUnusable ZipInventoryRejected _ALLOWED_AUDIO_EXT _ALLOWED_DOC_EXT
        _ALLOWED_IMAGE_EXT _ALLOWED_TEXT_EXT _ALLOWED_VIDEO_EXT _AUDIO_EXT_MIME
        _BLOCK_STRUCTURAL_KEYS _CONTROL_CHARS_RE _CheckedFile _DocSegments
        _FILE_READ_BINARY_EXTS _FILE_READ_CAP _FILE_READ_SNIFF_BYTES _GIT_FILTER_KEY_RE
        _GIT_PANEL_STDOUT_CAP _GIT_PROBE_STDERR_CAP _GIT_ROOT_WALK_LIMIT _GREP_DOC_EXTS
        _GREP_DOC_MAX_BYTES _GREP_DOC_MAX_CHARS _GREP_LABEL_CHARS _GREP_MAX_DIRS_VISITED
        _GREP_MAX_FILE_BYTES _GREP_MAX_QUERY_CHARS _GREP_MAX_RESULTS
        _GREP_MIN_QUERY_CHARS _GREP_PPTX_SLIDE_RE _GREP_PREVIEW_CHARS
        _GREP_RG_MAX_RECORD_BYTES _GREP_RG_OVERSIZE _GREP_RG_POLL_SECS
        _GREP_RG_QUEUE_LINES _GREP_RG_TEARDOWN_SECS _GREP_ROW_DEADLINE_STRIDE
        _GREP_TIME_BUDGET_SECS _HEAD_READ_LIMIT _HEIF_BRANDS _HIDDEN_TOOL_DIRS
        _INLINE_DISPOSITION_PREFIXES _MACOS_TEMP_PROJECT_PREFIX_RE _MAGIC_PREFIXES
        _MAX_UPLOAD_BYTES _MAX_UPLOAD_FILES _MAX_VIDEO_UPLOAD_BYTES _MEDIA_EXT_MIME
        _MEDIA_MAGIC _OFFICE_PREVIEWABLE_EXT _OFFICE_PREVIEW_CAP _OpenDenied
        _OpenRefusal _OpenedFile _PATH_COMPLETE_MAX_ENTRIES _PATH_COMPLETE_MAX_SCAN
        _PATH_PROBE_ADMIT_TIMEOUT_SECS _PATH_PROBE_EXEC_CEILING_SECS
        _PATH_TOKEN_SEPARATORS _PROJECT_TREE_MAX_ENTRIES _PROJECT_TREE_SCANDIR_TAKES_FD
        _PROJECT_TREE_SCAN_LIMIT _PROJECT_TREE_SKIP_DIRS
        _PathProbe _PathProbeBusy _PreviewUnsupported _ProbeT _RASTER_EXT_MIME
        _RASTER_MIME_EXT _READ_PATH_EXTRA_MAGIC _SCREENSHOT_DIR _SEARCH_LIMIT_CEILING
        _SHEET_MAX_CDIR_ENTRY_BYTES _SHEET_MAX_CELL_CHARS _SHEET_MAX_COLS
        _SHEET_MAX_EXPANDED_BYTES _SHEET_MAX_MEMBERS _SHEET_MAX_ROWS _SHEET_MAX_SHEETS
        _SHEET_MAX_TEXT_CHARS _STREAM_CHUNK_BYTES _STREAM_MAX_BYTES
        _STREAM_TEXT_PROBE_BYTES _SUBAGENT_SESSION_PREFIX _SheetRefusal _TextRead
        _UPLOAD_DIR _VIDEO_EXT_MIME _VIDEO_HINT_EXT _WALK_MAX_DIRS_VISITED
        _WALK_MAX_SCAN_SCOPED _WALK_MAX_SCAN_UNSCOPED _WALK_SKIP_DIRS _WIN_DRIVE_ROOT_RE
        _WorkspaceConflict _ZIP_CONTAINER_EXTS _audit_file_search_exit _audit_file_send
        _body_err_code _browse_dirs_sync _browse_drives_sync _browse_entry_is_dir
        _browse_files_sync _browse_parent _cap_slides _complete_path_listing
        _completion_segments _content_matches_ext _content_mismatch_message
        _file_write_blocking _fuzzy_score _gate_upload_file _git_head_path
        _grep_doc_segments _grep_docs _grep_hit _grep_pdf_segments _grep_python
        _grep_resolve_root _grep_rg _grep_rg_argv _grep_rg_executable _grep_rg_hit_of
        _grep_rg_teardown _grep_sensitive_globs _grep_xlsx_segments
        _is_not_a_repo_verdict _is_windows_drive_root _known_project_dirs
        _load_sheet_payload _match_known_project _match_known_project_for _open_checked
        _open_checked_file _open_completion_dir _open_rb_nofollow
        _owner_view_bypasses_credential_pass _parse_range_header _parse_workbook_grid
        _porcelain_unquote _probe_busy_response _probe_git_dir _probe_persisted_session
        _probe_request_path _project_directory_absent _project_git_branch
        _ProjectTreeFolderMoved _project_tree_allot _project_tree_body
        _project_tree_fence _project_tree_file_quotas _project_tree_git_layout
        _project_tree_identity _project_tree_is_link _project_tree_scandir
        _project_tree_scandir_entries _project_tree_walk _read_git_meta_prefix
        _read_outbox_file
        _read_request_path _redact_block _redact_blocks _redact_project_path
        _redact_value _repo_filter_refusal_cause _resolve_diff_path _resolve_project_git
        _resolve_project_relative _resolve_raster_ext _resolve_search_root
        _resolve_session_target _resolve_ws_dir _run_git_bounded _run_path_probe
        _scan_completion_dir _screenshot_dir _sel _sheet_cell_json _sheet_formula_text
        _slot_project_snapshot _sniff_media_type _stream_media_part
        _subagent_parent_session_key _subsequence_run _upload_dir
        _validate_dashboard_path _vet_zip_eocd _worktree_probe_failure_is_empty_scope
        _write_file_restricted annotations api_browse_dirs api_browse_files
        api_channel_upload_file api_dashboard_config api_file_diff api_file_download
        api_file_grep api_file_office_preview api_file_raw api_file_read api_file_search
        api_file_sheet api_file_stream api_file_watch api_file_write api_outbox_download
        api_outbox_list api_outbox_notify api_path_complete api_project_git
        api_project_git_log api_project_git_status api_project_tree api_reveal_path
        api_screenshot api_slack_upload_file api_upload api_upload_file api_workspaces
        api_workspaces_create api_workspaces_delete api_workspaces_update
        append_and_surface asdict atomic_write binary_content_is_flagged
        cgroup_scope_argv coerce_dict_section config_dir config_loader
        dashboard_slot_key data_home drained_to_thread executors extract_blocks
        extract_pdf_segments extract_slides extract_text file_delivery_consent
        is_direct_local_request is_sensitive_path is_sensitive_resolved_path
        is_tracked_channel is_unc_shape join_slides link_pattern_url_ok logger
        materialize_workspace_dir open_access_control_source part_stream
        path_contains_sensitive pinned_fs pinned_parent_replace_supported
        platform_compat popen_limited read_bounded_json redact redact_credentials
        redact_exfiltration_urls redact_for_display redact_log_via_context
        redact_owner_view_via_context redact_path_segments redaction_switch
        rehydrate_slot_from_history_async require_owner_dashboard_request
        run_config_write safe_read_file_bytes safe_read_prefix
        sandbox_credential_targets sandboxed_spawn_argv sniff_raster_mime
        update_config_locked upload_destination validate_provider_executable
        validate_tool_args vet_zip_inventory_bytes web wide_content_is_flagged
        worktree_probe_failure_is_empty_scope wrap_argv
    """.split())

#: The owners the facade composes. Adding or removing one changes the composition,
#: so the set is spelled out rather than globbed.
_OWNER_MODULES = (
    "uploads",
    "workspaces",
    "pinned_io",
    "transfer",
    "office_preview",
    "sheet",
    "search",
    "path_complete",
    "grep",
    "browse",
    "project_dirs",
    "git_panel",
    "project_tree",
    "dashboard_config",
)

#: Each moved definition and the owner its responsibility puts it in.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "uploads": tuple("""
        _content_matches_ext _content_mismatch_message _resolve_raster_ext _sniff_media_type
        _stream_media_part _upload_dir _write_file_restricted api_upload_file
        """.split()),
    "workspaces": tuple("""
        _WorkspaceConflict _resolve_ws_dir api_workspaces api_workspaces_create
        api_workspaces_delete api_workspaces_update
        """.split()),
    "pinned_io": tuple("""
        _CheckedFile _OpenDenied _OpenRefusal _OpenedFile _TextRead _file_write_blocking
        _open_checked _open_checked_file _open_rb_nofollow _read_request_path
        _resolve_project_relative
        """.split()),
    "transfer": tuple("""
        _parse_range_header api_file_download api_file_stream api_file_watch
        """.split()),
    "office_preview": tuple("""
        _PreviewUnsupported _cap_slides _redact_block _redact_blocks _redact_value
        api_file_office_preview
        """.split()),
    "sheet": tuple("""
        _SheetRefusal _load_sheet_payload _parse_workbook_grid _sheet_cell_json
        _sheet_formula_text _vet_zip_eocd api_file_sheet
        """.split()),
    "search": tuple("""
        _audit_file_search_exit _fuzzy_score _resolve_search_root _subsequence_run
        api_file_search
        """.split()),
    "path_complete": tuple("""
        _complete_path_listing _completion_segments _open_completion_dir
        _scan_completion_dir api_path_complete
        """.split()),
    "grep": tuple("""
        _grep_doc_segments _grep_docs _grep_hit _grep_pdf_segments _grep_python
        _grep_resolve_root _grep_rg _grep_rg_argv _grep_rg_executable _grep_rg_hit_of
        _grep_rg_teardown _grep_sensitive_globs _grep_xlsx_segments
        """.split()),
    "browse": tuple("""
        _browse_dirs_sync _browse_drives_sync _browse_entry_is_dir _browse_files_sync
        _browse_parent _is_windows_drive_root
        """.split()),
    "project_dirs": tuple("""
        _git_head_path _known_project_dirs _match_known_project _match_known_project_for
        _project_git_branch _read_git_meta_prefix _redact_project_path _resolve_project_git
        _slot_project_snapshot api_project_git
        """.split()),
    "git_panel": tuple("""
        _is_not_a_repo_verdict _porcelain_unquote _probe_git_dir _project_directory_absent
        _repo_filter_refusal_cause _run_git_bounded _worktree_probe_failure_is_empty_scope
        api_project_git_log api_project_git_status
        """.split()),
    "project_tree": tuple("""
        _ProjectTreeFolderMoved _project_tree_allot _project_tree_body
        _project_tree_fence _project_tree_file_quotas _project_tree_git_layout
        _project_tree_identity _project_tree_is_link _project_tree_scandir
        _project_tree_scandir_entries _project_tree_walk api_project_tree
        """.split()),
    "dashboard_config": ("api_dashboard_config",),
}

#: The one module-level value an owner defines: ``_run_git_bounded`` reads it as a
#: parameter default, which Python evaluates when the owner loads, so it lives
#: beside that function and the facade imports it.
_OWNER_CONSTANTS = {"git_panel": ("_GIT_PANEL_STDOUT_CAP",)}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every name in
#: ``_BASE_OWNERS``, captured from the one-module file before the split: each moved
#: name keeps the kind and signature it had there. Recaptured when the bounded
#: project-tree walk replaced the ``project_tree`` helpers.
_BASE_SHAPE_DIGEST = "a38dad08c49dfab78f74315fde5ad9471c9cd53df1463bdb75380ca0aa397b6d"

#: Definitions that stay in the facade file. The seams and the path-probe
#: chokepoint every owner and two sibling handlers call; file delivery, kept whole
#: (the outbox routes, which hold the security-posture census site, and the Slack
#: and channel uploads with their shared admission gate); and each route a guard
#: reads in ``files.py`` by path: the reveal and upload routes (the JSON-body
#: register; the upload routes' except block, which
#: ``test_cse_2026_08_07_fixes`` slices from this file), the two native-dialog
#: routes (``BENIGN_SPAWNS``), the file read and diff with their owner-view helper
#: (the credential-redaction switch's exact file set, the spawn key of the diff's
#: ``_run``, the git argv the desktop-binary audit reads), the raw route (the SVG
#: policy literal the appearance library compares against), and the write and grep
#: routes (the JSON-body register).
_FACADE_DEFS = (
    "is_tracked_channel",
    "_subagent_parent_session_key",
    "_sel",
    "_audit_file_send",
    "_body_err_code",
    "api_reveal_path",
    "_read_outbox_file",
    "api_outbox_notify",
    "_owner_grant_releases_flagged",
    "_audit_owner_grant_refusal",
    "_audit_consented_download",
    "api_outbox_download",
    "api_outbox_list",
    "_gate_upload_file",
    "api_slack_upload_file",
    "api_channel_upload_file",
    "api_upload",
    "_screenshot_dir",
    "api_screenshot",
    "_validate_dashboard_path",
    "_PathProbeBusy",
    "_run_path_probe",
    "_walk_monotonic",
    "_probe_busy_response",
    "_PathProbe",
    "_probe_request_path",
    "_owner_view_bypasses_credential_pass",
    "api_file_read",
    "api_file_raw",
    "_resolve_diff_path",
    "api_file_write",
    "api_file_grep",
    "api_file_diff",
    "api_browse_dirs",
    "api_browse_files",
)


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    """Routes, the handlers package, sibling handlers and tests read private names
    off the facade as well as public ones, so every module-level binding survives."""
    assert len(_BASE_NAMES) > 280
    assert sorted(name for name in _BASE_NAMES if not hasattr(files, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first, and the
    handlers package's re-exports are the facade's own objects there too."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 110
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers as pkg
        import kiro_crew.dashboard.handlers.files as files
        missing = [n for n in sys.argv[1:] if not hasattr(files, n)]
        assert missing == [], missing
        foreign = [n for n in sys.argv[1:] if hasattr(pkg, n) and n.startswith("api_")
                   and getattr(pkg, n) is not getattr(files, n)]
        assert foreign == [], foreign
        print("ok")
        """,
        *public,
    )


def _package_reexports() -> list[str]:
    tree = ast.parse(Path(handlers_pkg.__file__).read_text(encoding="utf-8"))
    return [
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE
        for alias in node.names
    ]


def test_the_routes_and_the_package_reach_the_facade_objects() -> None:
    """Every route the gateway registers for a file handler and every name the
    handlers package re-exports is the facade's object."""
    from kiro_crew.dashboard import routes

    app = web.Application()
    routes.register_all(app)
    served = [
        route.handler
        for route in app.router.routes()
        if getattr(files, getattr(route.handler, "__name__", ""), None) is not None
        and route.handler.__module__ == _FACADE
    ]
    assert len({id(h) for h in served}) == 33
    assert [h.__name__ for h in served if getattr(files, h.__name__) is not h] == []
    reexports = _package_reexports()
    assert len(reexports) == 35
    assert [n for n in reexports if getattr(handlers_pkg, n) is not getattr(files, n)] == []


def test_the_seams_other_modules_import_keep_their_identity() -> None:
    """Sibling handlers import names from the facade or read them off it at call
    time; each still resolves there, as the same object, and the facade still takes
    the session-target resolver from the messaging facade by name."""
    from kiro_crew.dashboard.handlers import knowledge, messaging, office_slides, terminal

    assert knowledge._ZIP_CONTAINER_EXTS is files._ZIP_CONTAINER_EXTS
    assert knowledge._content_matches_ext is files._content_matches_ext
    assert terminal._PathProbeBusy is files._PathProbeBusy
    assert terminal._run_path_probe is files._run_path_probe
    assert office_slides._files is files
    assert files._resolve_session_target is messaging._resolve_session_target
    read_by_attribute = set(
        re.findall(r"\b_files\.(\w+)", Path(office_slides.__file__).read_text(encoding="utf-8"))
    )
    assert {"_open_checked_file", "_OpenDenied", "_run_path_probe"} <= read_by_attribute
    assert sorted(name for name in read_by_attribute if not hasattr(files, name)) == []
    cited = {"_run_path_probe", "_gate_upload_file"}
    assert sorted(name for name in cited if not hasattr(files, name)) == []


#: Names the moved code imports in its own body, from the module that owns them. A
#: module-level import of any of them would put that module on the gateway's boot
#: path (or close an import cycle), so each stays function-local.
_IMPORTED_BY_NAME = {
    "_get_config_lock": "kiro_crew.dashboard.handlers.agents",
    "denied_sides": "kiro_crew.decisions.capability",
    "is_owner_dashboard_request": "kiro_crew.dashboard.handlers.source_providers",
    "owner_view_for_request": "kiro_crew.dashboard.handlers.source_providers",
    "publish_session_card_chips_now": "kiro_crew.dashboard.handlers.source_providers",
    "is_tracked_channel": "kiro_crew.slack.handler",
}


def test_the_lazy_imports_stay_inside_the_functions_that_need_them() -> None:
    sources = {"files": _FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources()}
    local: dict[str, set[str]] = {name: set() for name in _IMPORTED_BY_NAME}
    top: list[str] = []
    for stem, source in sources.items():
        tree = ast.parse(source)
        module_level = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name in local:
                    local[alias.name].add(node.module or "")
                    if id(node) in module_level:
                        top.append(f"{stem}:{node.lineno}:{alias.name}")
    assert local == {name: {module} for name, module in _IMPORTED_BY_NAME.items()}
    assert top == []


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__``, so every public binding goes out."""
    assert not hasattr(files, "__all__")
    probe = tmp_path / "files_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.handlers.files import *  # noqa: F401,F403\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("files_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("api_file_search", "api_project_tree", "api_dashboard_config", "api_file_read"):
        assert getattr(module, name) is getattr(files, name)


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(_OWNER_MODULES)
    assert set(_BASE_OWNERS) == set(_OWNER_MODULES)


def _shape(obj: object) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))  # type: ignore[arg-type]


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The facade's binding of a moved name is the owner's object, in the owner its
    responsibility names, and no name is defined by two owners."""
    strays = [
        f"{owner}:{name}"
        for owner, names in {**_BASE_OWNERS, **_OWNER_CONSTANTS}.items()
        for name in names
        if getattr(files, name) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []
    names = [name for group in _BASE_OWNERS.values() for name in group]
    assert len(names) == len(set(names)) == 103


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(
        f"{name} {_shape(getattr(files, name))}"
        for names in _BASE_OWNERS.values()
        for name in names
    )
    assert len(lines) == 103
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _module_assignments(source: str) -> set[str]:
    names = set()
    for node in ast.parse(source).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the facade's namespace,
    so a test that rebinds one there is the binding every function sees -- which
    holds only while no owner keeps a copy. The one owner constant is a parameter
    default, read once when its owner loads, and the facade binds that object."""
    state = _module_assignments(_FACADE_PATH.read_text(encoding="utf-8"))
    assert {"logger", "_UPLOAD_DIR", "_MAX_UPLOAD_BYTES", "_GREP_MAX_RESULTS"} <= state
    owned = {stem: sorted(_module_assignments(source)) for stem, source in _owner_sources().items()}
    assert {stem: names for stem, names in owned.items() if names} == {
        stem: list(names) for stem, names in _OWNER_CONSTANTS.items()
    }
    git_panel = _owner("git_panel")
    assert files._GIT_PANEL_STDOUT_CAP is git_panel._GIT_PANEL_STDOUT_CAP
    default = inspect.signature(files._run_git_bounded).parameters["cap"].default
    assert default is files._GIT_PANEL_STDOUT_CAP


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = getattr(files, name)
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_every_base_definition_is_in_exactly_one_place() -> None:
    """The facade keeps what ``_FACADE_DEFS`` names and the owners hold the rest:
    together they are the one-module file's definitions, each once."""
    defined = {
        node.name
        for node in ast.parse(_FACADE_PATH.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert defined == set(_FACADE_DEFS)
    moved = {name for names in _BASE_OWNERS.values() for name in names}
    assert len(defined | moved) == len(defined) + len(moved) == 138


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.handlers.files`` keeps seeing the
    moved sites: an owner function logs through the facade's ``logger``."""
    assert files.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 12


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.dashboard.handlers.files.<name>`` reaches an owner
    function only because the function reads the facade's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= 90
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(files) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_files_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_files_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from
    the facade surfaces only when its line runs -- often inside an ``except`` that
    turns the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(files)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for: the Office preview's block redaction
    (office_preview) reads the facade's ``redact``, and the grep hit row (grep)
    reads the facade's preview ceiling."""
    monkeypatch.setattr(files, "redact", lambda text: f"redacted:{text}")
    assert files._redact_value("secret") == "redacted:secret"
    monkeypatch.setattr(files, "_GREP_PREVIEW_CHARS", 3)
    hit = files._grep_hit("a.txt", 1, "abcdefgh")
    assert hit["preview"] == "abc"


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner
    function resolves back through its own ``__module__`` and ``__qualname__``. A
    class keeps its owner module, which is where ``inspect`` finds its source."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    assert files._OpenDenied.__module__ == f"{_OWNER_PACKAGE}.pinned_io"
    assert files._WorkspaceConflict.__module__ == f"{_OWNER_PACKAGE}.workspaces"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(files.api_file_search)
    assert source.startswith("async def api_file_search(")
    assert inspect.getsourcefile(files.api_file_search) == _owner("search").__file__


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way:
    ``import a.b``, ``from a import b``, relative imports resolved against
    *package*, and a string-literal ``import_module(...)`` / ``__import__(...)``."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not node.args[0].value.startswith(".")
        ):
            found.append((node, node.args[0].value))
    return found


def _within(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Nodes under a module-level ``if TYPE_CHECKING:`` body; its ``else`` runs."""
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports a project module outside ``TYPE_CHECKING``: the
    facade, a sibling owner, or anything else under ``kiro_crew``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _within(target, "kiro_crew") and id(node) not in guarded
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import search\n", True),
        ("from .grep import _grep_hit\n", True),
        ("from ..handlers import files\n", True),
        ("from kiro_crew.dashboard.handlers import files\n", True),
        ("import kiro_crew.dashboard.handlers.files as handlers\n", True),
        ("def f():\n    from kiro_crew.dashboard.handlers.files import _sel\n", True),
        ("def f():\n    from kiro_crew.decisions.capability import denied_sides\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.dashboard.handlers.files')\n",
            True,
        ),
        ("__import__('kiro_crew.dashboard.file_api.grep')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.handlers.files import _sel\n", False),
        ("import asyncio\nfrom aiohttp import web\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "file_api" not in text:
            continue
        tree = ast.parse(text)
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(tree, _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


def test_an_owner_imports_project_modules_only_for_type_checking() -> None:
    """An owner's runtime imports are stdlib and third-party only. Its project names
    come from the facade's globals, so an owner adds nothing to the gateway's boot
    import graph and cannot close a cycle with the facade. Lazy imports in function
    bodies are counted too, except the ones the moved code made at the base, which
    ``_IMPORTED_BY_NAME`` and this module's helpers pin."""
    lazy_at_base = {
        "kiro_crew.config.loader",
        "kiro_crew.dashboard",
        "kiro_crew.dashboard.handlers",
        "kiro_crew.dashboard.handlers._shared",
        "kiro_crew.hooks",
        "kiro_crew.security",
        "kiro_crew.sel",
        "kiro_crew.validation",
        *_IMPORTED_BY_NAME.values(),
    }
    offenders = []
    for stem, source in _owner_sources().items():
        tree = ast.parse(source)
        guarded = _type_checking_nodes(tree)
        module_level = {id(node) for node in tree.body}
        for node, target in _import_targets(tree, _OWNER_PACKAGE):
            if not _within(target, "kiro_crew") or id(node) in guarded:
                continue
            base = node.module if isinstance(node, ast.ImportFrom) else target
            if id(node) in module_level or base not in lazy_at_base:
                offenders.append(f"{stem}:{node.lineno}:{target}")
    assert offenders == []


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.files
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.dashboard.file_api.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_OWNER_MODULES,
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.files as first
        del sys.modules["kiro_crew.dashboard.handlers.files"]
        import kiro_crew.dashboard.handlers.files as second
        from kiro_crew.dashboard.file_api import search
        assert second is not first
        assert second.api_file_search.__globals__ is vars(second)
        assert search.api_file_search is second.api_file_search
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.handlers\.files\.(\w+)$""")

#: The patches whose attribute the scan cannot resolve from the source, keyed by
#: (test file, enclosing function), with the names each one rebinds.
_RESOLVED_DYNAMIC_PATCHES: dict[tuple[str, str], frozenset[str]] = {}


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard.handlers":
            aliases |= {a.asname or a.name for a in node.names if a.name == "files"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or _imports_the_facade(value):
                aliases.add(target.id)
                changed = True
    return aliases


def _imports_the_facade(node: ast.AST) -> bool:
    """``import_module(<facade>)``, or ``__import__(<facade>, fromlist=...)`` with a
    non-empty fromlist (which returns the facade itself, not its root package)."""
    if not (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE
    ):
        return False
    func = ast.unparse(node.func)
    if func.endswith("import_module"):
        return True
    fromlist = {k.arg: k.value for k in node.keywords}.get("fromlist")
    if fromlist is None and len(node.args) >= 4:
        fromlist = node.args[3]
    return (
        func == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)) and bool(fromlist.elts)
    )


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _facade_strings(tree: ast.Module, aliases: set[str]) -> set[str]:
    """Every name a test module binds to the facade's dotted path, to a fixed point."""
    strings: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(target, ast.Name) and target.id not in strings:
                if _string_text(value, strings, aliases) == _FACADE:
                    strings.add(target.id)
                    changed = True
    return strings


def _string_text(node: ast.AST, strings: set[str], aliases: set[str]) -> str | None:
    """The text one piece of a string spells: a constant, a name bound to the
    facade's dotted path, or ``<facade alias>.__name__``; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in strings:
        return _FACADE
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and ast.unparse(node.value) in aliases
    ):
        return _FACADE
    return None


def _string_pieces(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.FormattedValue) else v for v in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_pieces(node.left) + _string_pieces(node.right)
    return [node]


def _resolve_target(
    node: ast.AST, params: dict[str, set[str]], strings: set[str], aliases: set[str]
) -> set[str] | None:
    """The names a string patch target rebinds on the facade: an empty set when it
    names another module, None when it names the facade but the scan cannot tell
    which attribute."""
    pieces = _string_pieces(node)
    head = ""
    for index, piece in enumerate(pieces):
        text = _string_text(piece, strings, aliases)
        if text is None:
            break
        head += text
    else:
        match = _FACADE_STRING.match(head)
        return {match.group(1)} if match else set()
    if not head.startswith(f"{_FACADE}."):
        return set()
    if head == f"{_FACADE}." and index == len(pieces) - 1:
        return _resolve_name(pieces[index], params)
    return None


def _patched_names_in(text: str) -> tuple[set[str], set[str]]:
    """``(names, dynamic)``: first-level names one test source rebinds on the
    facade, and the enclosing functions of each patch whose name the scan cannot
    resolve -- which fails the reach test closed unless it is a resolved one."""
    # Every spelling below names the facade's dotted path or imports ``files`` from
    # the handlers package, so a source with neither holds no facade patch.
    if "handlers.files" not in text and not (
        "kiro_crew.dashboard.handlers import" in text and re.search(r"\bfiles\b", text)
    ):
        return set(), set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    strings = _facade_strings(tree, aliases)
    found: set[str] = set()
    dynamic: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    scopes = [(tree, {}, "<module>")] + [
        (fn, _parametrized_strings(fn), fn.name) for fn in functions
    ]
    for scope, params, label in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                target = node.args[0] if node.args else kw.get("target")
                if target is not None and (
                    ast.unparse(target) in aliases or _imports_the_facade(target)
                ):
                    if func.endswith(_PATCH_CALLS):
                        name = (
                            node.args[1]
                            if len(node.args) >= 2
                            else kw.get("attribute", kw.get("name"))
                        )
                        resolved = _resolve_name(name, params) if name is not None else None
                        if resolved is None:
                            dynamic.add(label)
                        else:
                            found |= resolved
                    elif func.endswith("patch.multiple"):
                        if any(k.arg is None for k in node.keywords):
                            dynamic.add(label)
                        found |= {k for k in kw if k not in _MULTIPLE_OPTIONS}
                elif target is not None and func.split(".")[-1] in ("patch", "setattr", "delattr"):
                    resolved = _resolve_target(target, params, strings, aliases)
                    if resolved is None:
                        dynamic.add(label)
                    else:
                        found |= resolved
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                        found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.handlers\.files\.(\w+)["']""", text))
    return found, dynamic


def _facade_patched_names() -> set[str]:
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    unresolved = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            names, dynamic = _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
            found |= names
            for label in dynamic:
                key = (path.name, label)
                if key in _RESOLVED_DYNAMIC_PATCHES:
                    found |= _RESOLVED_DYNAMIC_PATCHES[key]
                else:
                    unresolved.append(key)
    assert unresolved == [], "a test patches a facade name the scan cannot resolve"
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the facade cannot reach."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                for stmt in node.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and stmt.value is not None:
                        found.update(loads(stmt.value))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.handlers.files as handlers\n"
        "from kiro_crew.dashboard.handlers import files as fs\n"
        "facade = importlib.import_module('kiro_crew.dashboard.handlers.files')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.handlers.files']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(handlers, 'first', 1)\n"
        "    monkeypatch.setattr(fs, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    fs.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.handlers.files.fifth', 5)\n"
        "    monkeypatch.setattr(fs.Shared, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(fs, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.handlers.files.os.scandir', 9)\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.handlers.files.{which}')\n"
        "def test_dunder(monkeypatch):\n"
        "    mod = __import__('kiro_crew.dashboard.handlers.files', fromlist=['x'])\n"
        "    patch.object(mod, 'eleventh')\n"
        "    patch.object(__import__('kiro_crew.dashboard.handlers.files', fromlist=['y']), 'twelfth')\n"
        "    patch.object(__import__('kiro_crew.dashboard.handlers.files'), 'not_the_facade')\n"
        "FACADE = 'kiro_crew.dashboard.handlers.files'\n"
        "other = 'kiro_crew.dashboard.handlers.messaging'\n"
        "def test_strings(monkeypatch):\n"
        "    path = FACADE\n"
        "    patch(f'{path}.thirteenth')\n"
        "    patch(FACADE + '.fourteenth')\n"
        "    monkeypatch.setattr(f'{fs.__name__}.fifteenth', 15)\n"
        "    patch(f'{other}.not_the_facade')\n"
        "    patch(f'{unknown}.not_the_facade')\n"
        "    patch(f'{FACADE}_twin.not_the_facade')\n"
    )
    names, dynamic = _patched_names_in(planted)
    assert names == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
        "thirteenth",
        "fourteenth",
        "fifteenth",
    }
    assert dynamic == set()
    unresolvable = (
        "from kiro_crew.dashboard.handlers import files as fs\n"
        "def _drive(monkeypatch, name):\n"
        "    monkeypatch.setattr(fs, name, 1)\n"
    )
    assert _patched_names_in(unresolvable) == (set(), {"_drive"})
    unresolvable_string = (
        "FACADE = 'kiro_crew.dashboard.handlers.files'\n"
        "def _rebind(name):\n"
        "    patch(f'{FACADE}.{name}')\n"
        "    patch(FACADE + '.' + name)\n"
    )
    assert _patched_names_in(unresolvable_string) == (set(), {"_rebind"})


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.handlers.files import _sel\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return _sel(), is_sensitive_path\n"
        "class C(_Base):\n"
        "    attr: int = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"_sel", "is_sensitive_path"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_facade() -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the facade would silently stop applying
    there. An owner may DEFINE one: the facade's binding of it is the composed copy,
    and every caller reads it through the facade's globals."""
    patched = _facade_patched_names()
    assert {
        "is_sensitive_path",
        "_sel",
        "redact",
        "_run_path_probe",
        "_MAX_UPLOAD_BYTES",
        "_UPLOAD_DIR",
        "_run_git_bounded",
        "_redact_project_path",
        "_grep_rg_executable",
        "popen_limited",
        "_open_checked_file",
        "_resolve_search_root",
        "path_contains_sensitive",
    } <= patched
    assert len(patched) >= 60
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(getattr(files, name) is vars(_owner(stem))[name] for name in defined), stem


# ── the guards keep their reach ───────────────────────────────────────────────

#: Constructs repository guards read in ``dashboard/handlers/files.py`` by path or
#: as an exact file set: the SVG policy literal the appearance library compares
#: against, the owner-view seam the credential-redaction switch confines to this
#: file, the git argv the desktop-binary audit reads from the diff route, and the
#: two native-dialog spawns ``BENIGN_SPAWNS`` keys by this path. An owner that grew
#: one would move it out of the guard's sight, so each stays in the facade.
_STAYS_IN_THE_FACADE = (
    r"script-src 'none'; style-src 'unsafe-inline'",
    r"owner_view(?:_scope)?\(|redact_owner_view_via_context",
    r"(?m)^\s+_git = \[",
    r"create_subprocess_exec\(",
)


@pytest.mark.parametrize("pattern", _STAYS_IN_THE_FACADE)
def test_a_construct_a_guard_reads_in_the_facade_stays_there(pattern: str) -> None:
    assert re.search(pattern, _FACADE_PATH.read_text(encoding="utf-8"))
    holders = [stem for stem, source in _owner_sources().items() if re.search(pattern, source)]
    assert holders == []


def test_the_census_site_stays_in_the_facade() -> None:
    """``test_security_posture``'s census pins the one gate-side baseline log site
    (in ``api_outbox_notify``) to this path; it stays here, no owner grows one, and
    the census scan sees a site planted in an owner."""
    from test_security_posture import _BASELINE_LOG_SITE_CENSUS, _gate_side_baseline_log_sites

    sites = _gate_side_baseline_log_sites(_FACADE_PATH.read_text(encoding="utf-8"))
    assert len(sites) == _BASELINE_LOG_SITE_CENSUS["dashboard/handlers/files.py"] == 1
    sources = _owner_sources()
    grown = {
        stem: len(found)
        for stem, source in sources.items()
        if (found := _gate_side_baseline_log_sites(source))
    }
    assert grown == {}
    planted = sources["search"] + '\n\ndef _planted(x):\n    logger.error("%s", redact(x))\n'
    assert len(_gate_side_baseline_log_sites(planted)) == 1


def _mirror(tmp_path: Path) -> Path:
    """A checkout-shaped copy of the files the widened guards read."""
    root = tmp_path / "checkout"
    rels = [
        "dashboard/handlers/files.py",
        "platform/update_layout.py",
        *(f"dashboard/file_api/{path.name}" for path in _OWNER_DIR.glob("*.py")),
    ]
    for rel in rels:
        target = root / "src/kiro_crew" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_SRC / "kiro_crew" / rel, target)
    return root


def _red_green(path: Path, check: Any, old: str | None, new: str) -> None:
    """*check* passes, fails once *new* is planted in *path* (replacing every *old*,
    or appended), and passes again once the file is restored."""
    text = path.read_text(encoding="utf-8")
    check()
    if old is None:
        path.write_text(text + new, encoding="utf-8")
    else:
        assert old in text, old
        path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError):
        check()
    path.write_text(text, encoding="utf-8")
    check()


def test_every_owner_coroutine_file_is_checked_for_config_dir() -> None:
    import test_no_config_dir_in_async as guard

    with_async = {
        f"dashboard/file_api/{path.name}"
        for path in _OWNER_DIR.glob("[!_]*.py")
        if "async def " in path.read_text(encoding="utf-8")
    }
    assert len(with_async) >= 11
    assert with_async <= set(guard._ASYNC_CHECKED_FILES)
    assert "dashboard/handlers/files.py" in guard._ASYNC_CHECKED_FILES


def test_the_config_dir_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_no_config_dir_in_async as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(guard, "SRC", root / "src/kiro_crew")
    check = guard.TestNoConfigDirInAsync().test_update_layout_channel_helpers_never_maintain
    owner = root / "src/kiro_crew/dashboard/file_api/transfer.py"
    _red_green(owner, check, None, "\n\nasync def _planted():\n    config_dir()\n")


def test_the_on_loop_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The off-loop ratchet scans every owner, flags a blocking call planted in one,
    and fails when an endpoint the facade serves lives in a file it does not read."""
    import test_dashboard_files_onloop_fs as guard

    assert guard._owner_files() == sorted(_OWNER_DIR.glob("[!_]*.py"))
    mirror = _mirror(tmp_path) / "src/kiro_crew/dashboard/file_api"
    monkeypatch.setattr(guard, "_owner_files", lambda: sorted(mirror.glob("[!_]*.py")))
    check = guard.TestStaticRatchet().test_no_guarded_endpoint_touches_the_filesystem_on_the_loop
    planted = "\n\nasync def api_file_planted(request):\n    return os.path.isfile(request)\n"
    _red_green(mirror / "transfer.py", check, None, planted)
    monkeypatch.setattr(
        guard,
        "_owner_files",
        lambda: [p for p in sorted(mirror.glob("[!_]*.py")) if p.name != "sheet.py"],
    )
    with pytest.raises(AssertionError, match="served from a file the scan does not read"):
        check()


#: The JSON-body register rows the move re-keyed, by the owner each route now
#: lives in.
_REKEYED_REGISTER_ROWS = (
    "file_api/workspaces.py::api_workspaces_create",
    "file_api/workspaces.py::api_workspaces_update",
    "file_api/dashboard_config.py::api_dashboard_config",
)


def test_the_json_body_register_names_the_owners() -> None:
    import test_json_object_body_guard as guard

    sites = guard._call_sites()
    assert [row for row in _REKEYED_REGISTER_ROWS if row not in sites] == []
    assert [row for row in _REKEYED_REGISTER_ROWS if row not in guard._CAP_REGISTER] == []
    moved = {name for names in _BASE_OWNERS.values() for name in names}
    assert (
        sorted(
            key
            for key in guard._CAP_REGISTER
            if key.startswith("handlers/files.py::") and key.split("::")[1] in moved
        )
        == []
    )
    kept = sorted(key for key in sites if key.startswith("handlers/files.py::"))
    assert len(kept) == 6


def test_the_resolved_gate_dicts_name_the_owners() -> None:
    import test_pathres_loop_starvation as guard

    gate = guard._call_sites("is_sensitive_resolved_path")
    assert {k: v for k, v in gate.items() if "dashboard/file_api/" in k} == {
        "kiro_crew/dashboard/file_api/path_complete.py": 3,
        "kiro_crew/dashboard/file_api/project_tree.py": 2,
    }
    assert "kiro_crew/dashboard/handlers/files.py" not in gate
    containment = guard._containment_claim_sites()
    assert containment.get("kiro_crew/dashboard/file_api/project_tree.py") == 1
    assert "kiro_crew/dashboard/handlers/files.py" not in containment


def test_the_tree_route_holds_no_link_screen_in_either_file() -> None:
    """The bounded tree walk tells a link from the ``lstat`` its listing already
    holds, so the route's ``_run`` calls no link screen at its owner or in the
    facade, and the gate's baseline declares neither key -- a declared key with no
    site would be a stale one."""
    import test_link_screen_hold_pin as gate

    site = ("dashboard/file_api/project_tree.py", "api_project_tree._run")
    old = ("dashboard/handlers/files.py", "api_project_tree._run")
    found = gate.discovered_sites()
    assert site not in found and old not in found
    assert site not in gate.DECLARED_SITES and old not in gate.DECLARED_SITES
    gate.test_every_site_is_declared()
    gate.test_declared_sites_still_exist()


def test_the_bare_hop_guard_reads_the_file_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox bare-hop guard computes the chokepoint's reaching names over the
    facade and its owners as one module: a hop to ``_run_git_bounded`` (defined in
    git_panel) planted in project_tree, which never spells the chokepoint, is
    caught."""
    import test_sandbox_off_loop as guard

    cls = guard.TestNoBareSandboxedSpawnArgvHops
    root = _mirror(tmp_path) / "src/kiro_crew"
    monkeypatch.setattr(cls, "_src_root", staticmethod(lambda: root))
    monkeypatch.setattr(guard, "parsed_candidates", lambda *args, **kwargs: iter(()))
    check = cls().test_no_bare_hops_to_sandboxed_spawn_argv
    planted = (
        "\n\nasync def _planted():\n"
        "    await asyncio.to_thread(_run_git_bounded, [], cwd='', env={}, timeout=1)\n"
    )
    _red_green(root / "dashboard/file_api/project_tree.py", check, None, planted)


def test_the_error_code_rows_match_each_owner(tmp_path: Path) -> None:
    """The baseline's per-file rows for the facade and its owners are each one's
    measured count and sum to the one-module file's 82, and the scan counts a
    response planted in an owner against that owner."""
    import json

    import test_error_code_contract as guard

    baseline = json.loads((repo_root() / "error-code-baseline.json").read_text(encoding="utf-8"))
    rows = {
        path: counts
        for path, counts in baseline["files"].items()
        if path == "dashboard/handlers/files.py" or path.startswith("dashboard/file_api/")
    }
    live = guard.tally(guard.scan())
    assert rows == {path: live[path] for path in rows}
    assert sum(counts.get("missing_code", 0) for counts in rows.values()) == 82
    assert {path for path in live if path.startswith("dashboard/file_api/")} <= set(rows)
    root = _mirror(tmp_path) / "src/kiro_crew"
    owner = root / "dashboard/file_api/search.py"
    scan = guard._scan_uncached.__wrapped__
    before = guard.tally(scan(root))
    owner.write_text(
        owner.read_text(encoding="utf-8")
        + '\n\nasync def _planted():\n    return web.json_response({"error": "x"}, status=400)\n',
        encoding="utf-8",
    )
    after = guard.tally(scan(root))
    path = "dashboard/file_api/search.py"
    assert after[path]["missing_code"] == before.get(path, {}).get("missing_code", 0) + 1


# ── the review rules and the native lane keep their reach ─────────────────────


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
    root = repo_root()
    return sorted(path.relative_to(root).as_posix() for path in _OWNER_DIR.glob("*.py"))


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching the facade, {id: owner files those rules miss})``."""
    facade = "src/kiro_crew/dashboard/handlers/files.py"
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
    """A rule that reviews the facade reviews the code composed into it: an owner
    outside its patterns would take moved code out of that rule's sight."""
    rules = _autosde_rules()
    matched, missing = _rules_missing_owners(rules)
    assert {
        "memory-store-seam",
        "no-new-work-on-gateway-boot-path",
        "feature-map-correctness",
    } <= matched
    assert missing == {}
    narrowed = [
        (
            {**rule, "file-patterns": [p for p in rule["file-patterns"] if "file_api" not in p]}
            if rule["id"] == "memory-store-seam"
            else rule
        )
        for rule in rules
    ]
    assert _rules_missing_owners(narrowed)[1].keys() == {"memory-store-seam"}


def _darwin_filter(workflow: dict) -> list[str]:
    import yaml

    steps = workflow["jobs"]["decide"]["steps"]
    filters = next(step["with"]["filters"] for step in steps if step.get("id") == "filter")
    return yaml.safe_load(filters)["darwin"]


def test_the_native_macos_lane_selects_the_owners() -> None:
    """The descriptor and containment code the facade's darwin path entry exists to
    cover now lives in the owners, so a change to an owner alone selects the lane."""
    import yaml

    workflow = yaml.safe_load(
        (repo_root() / ".github/workflows/macos-on-demand.yml").read_text(encoding="utf-8")
    )
    darwin = _darwin_filter(workflow)
    assert "src/kiro_crew/dashboard/handlers/files.py" in darwin

    def uncovered(patterns: list[str]) -> list[str]:
        compiled = [_globstar(p) for p in patterns]
        return [o for o in _owner_paths() if not any(p.match(o) for p in compiled)]

    assert uncovered(darwin) == []
    assert uncovered([p for p in darwin if "file_api" not in p]) == _owner_paths()


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_and_class_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a nested function, a method, a
    static method and a property of an owner class all read the host namespace
    afterwards; a function the owner merely imported is left alone; the owner class
    keeps its own module; and a second compose onto a fresh namespace moves them."""
    owner = _write_module(
        tmp_path,
        "file_api_compose_owner_probe",
        """
        from os.path import join

        def helper():
            return VALUE

        def outer():
            def inner():
                return VALUE
            return inner

        class Tally:
            def method(self):
                return VALUE

            @staticmethod
            def static():
                return VALUE

            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "file_api_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    file_api.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    assert owner.Tally().method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally().value == "host"
    assert owner.join.__module__ != "file_api_compose_host_probe"
    assert owner.helper.__module__ == "file_api_compose_host_probe"
    assert owner.Tally.__module__ == "file_api_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"

    fresh = {"__name__": "file_api_compose_host_probe", "VALUE": "fresh"}
    file_api.compose(fresh, (owner,))
    assert owner.helper() == "fresh"

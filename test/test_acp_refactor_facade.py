"""``kiro_crew.acp.client`` and ``kiro_crew.acp.runtime`` stay the ACP patch seams after the split.

The module-level code moved out of the two transports lives in its owner modules:
``transport_framing``, ``transport_errors``, ``runtime_models``, ``runtime_process_tree``,
``runtime_start``, the launch tail ``launch``, and the per-process host adapters under
``harness`` (``claude``, ``opencode``, ``goose``, ``pi``, ``deepseek``). Each facade keeps
every moved name readable under its old path. A
name the facade's own code reads, and that nothing patches through it, is an ordinary
import; every other moved name is FORWARDED, so a patch through the facade lands on
the owner, where the owner's own callers read it. The modules the moved helpers probe
with stay bound on the facade their code came from, and each helper imports them from
that facade when it runs, so a test that rebinds one there still reaches the helper.

The seam rows patch ONE name on a facade with a stub that raises ``_Reached`` and then
drive an owner function that must read that name. ``_Reached`` derives from
``BaseException`` on purpose: several of these helpers are best-effort and swallow
``Exception``, and a stub such a handler could swallow would let a patch that MISSED
read as one that landed.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import functools
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable
from unittest import mock

import pytest
import test_acp_refactor_create_guard as create_guard

import kiro_crew.acp as acp_package
from kiro_crew import platform_compat, sandbox, subagent
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import (
    launch,
)
from kiro_crew.acp import runtime as acp_runtime
from kiro_crew.acp import (
    runtime_models,
    runtime_process_tree,
    runtime_start,
    transport_errors,
    transport_framing,
)
from kiro_crew.acp.harness import claude as claude_harness
from kiro_crew.acp.harness import deepseek as deepseek_harness
from kiro_crew.acp.harness import goose as goose_harness
from kiro_crew.acp.harness import opencode as opencode_harness
from kiro_crew.acp.harness import pi as pi_harness
from kiro_crew.credential_errors import is_credential_propagation_delay
from kiro_crew.start_priority import StartPriority

# The patch-target scan parses every test file once; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_acp_refactor_facade")

_FACADES: dict[str, ModuleType] = {m.__name__: m for m in (acp_client, acp_runtime)}
_OWNERS: dict[str, ModuleType] = {
    m.__name__: m
    for m in (
        transport_framing,
        transport_errors,
        runtime_models,
        runtime_process_tree,
        runtime_start,
        launch,
        claude_harness,
        opencode_harness,
        goose_harness,
        pi_harness,
        deepseek_harness,
    )
}
_SRC = Path(acp_client.__file__).resolve().parent

#: Every module-level name the split moved, by the facade that defined it before and the
#: owner that defines it now.
_MOVED: dict[str, dict[str, tuple[str, ...]]] = {
    "kiro_crew.acp.client": {
        "kiro_crew.acp.transport_framing": (
            "_STDOUT_BUFFER_LIMIT",
            "_OVERSIZE_DRAIN_MAX_BYTES",
            "OversizeLineUnrecoverable",
            "_drain_oversize_line",
            "_RESPONSE_WRITE_BOUND_SECS",
            "_RESPONSE_WRITE_MIN_PROGRESS_BYTES",
            "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS",
            "_is_proactor_loop",
            "_level_is_progress_signal",
            "_pending_write_bytes",
            "await_under_no_progress_bound",
            "_release_if_acquired",
            "write_response_frame_bounded",
            "response_write_window_secs",
            "write_notification_best_effort",
        ),
        "kiro_crew.acp.transport_errors": (
            "_COMPACTION_DETAIL_MAX_CHARS",
            "_COMPACTION_DETAIL_KEYS",
            "_COMPACTION_DETAIL_PLACEHOLDERS",
            "_COMPACTION_TRANSIENT_MARKERS",
            "_COMPACTION_WALK_MAX_DEPTH",
            "_walk_compaction_payload",
            "compaction_failure_detail",
            "compaction_failure_is_transient",
            "AcpError",
            "AcpTimeoutError",
            "AcpPermissionNeeded",
            "AcpProcessDied",
            "AcpAuthRequired",
            "AcpSandboxInitFailed",
            "AcpRegistrationRateLimited",
            "AcpToolGateUnroutable",
            "PiGateExtensionTampered",
            "AcpModelUnavailable",
            "AcpPromptBusy",
            "_RE_MODEL_UNAVAILABLE",
            "_RE_MODEL_TEMP_UNAVAILABLE",
            "_RE_INVALID_MODEL_ID",
            "_RE_THROTTLE_NAMED",
            "_RE_THROTTLE_GENERIC",
            "_RE_AUTH",
            "_5XX_SEP",
            "_RE_5XX_NAMED",
            "_RE_5XX_STATUS",
            "_RE_CONNECTION",
            "_RE_5XX_HINT",
            "_RE_AUTH_STATUS",
            "_RE_SESSION_EXPIRED",
            "_RE_INVALID_BEARER",
            "_is_session_expired",
            "is_auth_failure_output",
            "_RE_SANDBOX_INIT_FAILURE",
            "is_sandbox_init_failure_output",
            "sandbox_init_failure",
            "sandbox_init_failure_for_runtime",
            "_RE_REGISTRATION_FAILED",
            "_RE_REGISTRATION_THROTTLE_MARK",
            "registration_throttle_line",
            "is_registration_throttle_output",
            "registration_rate_limited_error",
            "_RE_USAGE_LIMIT",
            "_RE_GENERATE_FAILED",
            "_RE_PROCESS_FAILED",
            "_RE_MALFORMED_REQUEST",
            "_RE_IMAGE_FORMAT_UNSUPPORTED",
            "_PROMPT_BUSY_RE",
            "_RE_STREAM_ENVELOPE",
            "_RE_TRAILING_REQ_ID",
            "_provider_detail",
            "_model_is_unentitled",
            "_is_transient_raw_error",
            "PROVIDER_ERROR_USAGE_LIMIT",
            "PROVIDER_ERROR_MALFORMED_REQUEST",
            "PROVIDER_ERROR_MODEL_UNAVAILABLE",
            "PROVIDER_ERROR_THROTTLE",
            "PROVIDER_ERROR_CREDENTIAL_PROPAGATION",
            "PROVIDER_ERROR_AUTH",
            "PROVIDER_ERROR_SESSION_EXPIRED",
            "PROVIDER_ERROR_CONNECTION",
            "PROVIDER_ERROR_HTTP_5XX",
            "PROVIDER_ERROR_UNKNOWN",
            "ProviderErrorClass",
            "classify_provider_error",
            "_auto_remedy",
            "_format_acp_error",
            "_rejected_model_from_error",
            "_raise_acp_error",
        ),
        "kiro_crew.acp.runtime_models": (
            "DEFAULT_MODEL",
            "advertised_model_ids",
            "model_is_unusable",
            "resolve_pin_spelling",
            "catalog_row_would_drop",
            "resolve_usable_model",
            "pick_served_default",
            "_MODEL_SUBSTITUTION_ADVISORY_RE",
            "_extract_advisory_detail",
            "_is_model_substitution_advisory",
            "_MODEL_SUBSTITUTE_RE",
            "_substitute_model_from_advisory",
        ),
        "kiro_crew.acp.runtime_process_tree": (
            "_get_child_pids",
            "_direct_children",
            "_get_start_time",
            "_read_basename",
            "ChildRecord",
            "_capture_child_records",
            "_is_our_child",
            "_kill_escaped_children",
        ),
        "kiro_crew.acp.launch": (
            "_ADAPTER_INTERPRETERS",
            "_is_adapter_package_entry",
            "_named_by_override",
            "_adapter_spawn_label",
            "_normalize_exe_casing",
            "_mise_node_installs_dir",
            "_resolve_node_for_script",
            "_UNRESOLVED",
            "_vendored_acp_roots",
            "_resolve_node_adapter_argv",
            "_node_module_search_dirs",
            "_vendored_adapter_entry",
            "_self_served_bin_caches",
            "_resolution_generation",
            "_resolution_epoch",
            "bump_resolution_generation",
            "_resolve_self_served_bin",
            "_SANDBOX_PREFLIGHT_TIMEOUT",
            "_sandbox_preflight",
            "_run_preflight_bounded",
            "_unlink_readback_launcher",
            "_READBACK_STDERR_SCAN_CHARS",
            "_READBACK_FAULT_MAX_SHAPES",
            "_READBACK_FAULT_SHAPES",
            "_readback_stderr_diagnosis",
            "_readback_detail_with_diagnosis",
            "mise_data_dir",
            "RLIMIT_PROFILE_SESSION_HOST",
            "KIROCREW_SPAWNED_ENV",
            "KIROCREW_SPAWNED_VALUE",
        ),
        "kiro_crew.acp.harness.claude": (
            "CLAUDE_ACP_BIN",
            "CLAUDE_CODE_BIN",
            "CLAUDE_ACP_NPM_PKG",
            "_CLAUDE_ACP_PKG_ENTRY",
            "_CLAUDE_ACP_DEP_MARKER",
            "_claude_acp_argv_cache",
            "_resolve_vendored_claude_acp",
            "_resolve_claude_acp_bin",
            "_resolve_claude_code_executable",
            "_claude_adapter_installed_version",
        ),
        "kiro_crew.acp.harness.opencode": (
            "_OPENCODE_LAUNCH",
            "OPENCODE_BIN",
            "_ENV_OPENCODE_CONFIG_CONTENT",
            "OPENCODE_INSTALL_COMMAND",
            "_OPENCODE_CONFIG_READBACK_ARGS",
            "_OPENCODE_READBACK_TIMEOUT_S",
            "_opencode_readback_remedy",
            "_opencode_uniform_permission",
            "_opencode_rules_deny",
            "_opencode_config_mcp_server_names",
            "_opencode_config_mcp_servers_remedy",
            "_opencode_agent_permissions",
            "MAX_HARNESS_CONFIG_MCP_SERVERS",
            "MAX_HARNESS_TOOL_NAME_LEN",
            "opencode_rewrites_name",
        ),
        "kiro_crew.acp.harness.goose": (
            "_ENV_GOOSE_MODE",
            "_GOOSE_BUILTIN_ARG",
            "_GOOSE_BUILTIN_DEVELOPER",
        ),
        "kiro_crew.acp.harness.pi": (
            "PI_ACP_BIN",
            "PI_ACP_NPM_PKG",
            "_PI_ACP_PKG_ENTRY",
            "_PI_ACP_DEP_MARKER",
            "_ENV_PI_ACP_BIN",
            "PI_BIN",
            "PI_NPM_PKG",
            "_ENV_PI_ACP_PI_COMMAND",
            "PI_INSTALL_COMMAND",
            "_PI_RPC_ARGS",
            "_PI_EXTENSION_FLAG",
            "_PI_READBACK_REQUEST",
            "_PI_READBACK_TIMEOUT_S",
            "PI_MIN_VERSION",
            "_PI_NPM_PACKAGE_NAMES",
            "_PI_MANIFEST_SEARCH_DEPTH",
            "_ENV_PI_GATE_SESSION",
            "PI_GATE_EXTENSION_SHA256",
            "_pi_acp_argv_cache",
            "_pi_bin_cache",
            "_pi_gate_launcher_cache",
            "_resolve_pi_acp_bin",
            "_resolve_pi_bin",
            "_pi_installed_version",
            "_pi_version_issue",
            "pi_gate_extension_path",
            "_pi_gate_artifact_dir",
            "_pi_gate_extension_bytes",
            "_seal_pi_gate_extension",
            "_seal_gate_extension",
            "_publish_gate_artifact",
            "_pi_gate_launcher_body",
            "_ensure_pi_gate_launcher",
            "_pi_readback_remedy",
            "_same_file_spelling",
            "_same_file_spelling_all",
            "_pi_commands_from_readback",
            "agent_sdk",
        ),
        "kiro_crew.acp.harness.deepseek": (
            "_DSH_PATCH_FLAG",
            "_ENV_DSH_GATE_SESSION",
            "_ENV_DSH_GATE_MARKER",
            "_ENV_DSH_GATE_SCRUB_NAMES",
            "_DSH_GATE_SCRUB_CANARY_PREFIX",
            "_DSH_GATE_READBACK_TIMEOUT_S",
            "_DSH_GATE_MARKER_POLL_S",
            "_DSH_GATE_PROBE_EXIT_S",
            "_DSH_GATE_MARKER_MAX_BYTES",
            "DEEPSEEK_GATE_EXTENSION_SHA256",
            "_ENV_DEEPSEEK_PERMISSION_MODE",
            "DEEPSEEK_PERMISSION_MODE",
            "_DEEPSEEK_ENV_CHILD_SCRUB_CLASS",
            "_DEEPSEEK_ENV_NAME_GRAMMAR",
            "_DEEPSEEK_ENV_RESERVED_PREFIXES",
            "_DEEPSEEK_ENV_CREW_OWNED_NAMES",
            "deepseek_gate_extension_path",
            "_seal_deepseek_gate_extension",
            "_write_deepseek_gate_patch",
            "_validate_deepseek_env_mapping",
            "_deepseek_vault_env_names",
            "_deepseek_vault_env",
            "SECRET_URI_PREFIX",
            "resolve_secret_uris",
            "agent_env_scrub_prefixes",
        ),
    },
    "kiro_crew.acp.runtime": {
        "kiro_crew.acp.launch": (
            "KIROCREW_SPAWNED_ENV",
            "KIROCREW_SPAWNED_VALUE",
            "RLIMIT_PROFILE_SESSION_HOST",
        ),
        "kiro_crew.acp.runtime_process_tree": (
            "_get_rss_mb",
            "_own_children",
            "_iter_descendant_pids",
            "_ProcessTable",
            "_PS_TABLE_TTL_S",
            "_ps_table_lock",
            "_ps_table_cache",
            "_reset_ps_table_cache",
            "_ps_process_table",
            "_rss_tree_mb_for_pids",
            "_get_rss_tree_mb",
        ),
        "kiro_crew.acp.runtime_start": (
            "_COLD_START_MAX_CONCURRENT",
            "_ColdStartAdmission",
            "_cold_start_admissions",
            "_cold_start_admissions_lock",
            "_cold_start_admission",
            "_cold_start_counts",
            "_SESSION_START_CONCURRENCY_DEFAULT",
            "_SESSION_START_CONCURRENCY_FLOOR",
            "_COLLECTOR_PERMIT_HEADROOM",
            "_resolve_session_start_concurrency",
            "_record_session_start",
            "SessionStartGate",
            "StartPermit",
            "_session_start_gates",
            "_session_start_gates_lock",
            "session_start_gate",
            "StartAdopter",
            "START_OUTCOME_ADOPTED",
            "START_OUTCOME_TORN_DOWN",
            "START_OUTCOME_ABANDONED",
            "START_OUTCOME_RUNTIME_DEAD",
            "START_OUTCOME_ERROR",
            "StartCollector",
            "_START_COLLECT_TIMEOUT_DEFAULT",
            "_resolve_start_collect_timeout",
            "_resolve_session_start_timeout",
            "_INIT_NOTIFICATION_BUFFER_LIMIT",
            "_split_init_frames",
            "_SESSION_NEW_TIMEOUT",
        ),
    },
}


#: ``AcpClient`` methods the split turned into owner FUNCTIONS, each taking the client it
#: served as its first argument. Their code came from the facade too, so a name they read
#: there is a seam like any moved helper's.
_MOVED_METHODS: dict[str, dict[str, tuple[str, ...]]] = {
    "kiro_crew.acp.client": {
        "kiro_crew.acp.harness.opencode": ("_verify_opencode_routing",),
        "kiro_crew.acp.harness.pi": ("_verify_pi_gate",),
        "kiro_crew.acp.harness.deepseek": ("_verify_deepseek_gate",),
    },
}


@pytest.mark.parametrize(
    ("facade", "owner", "name"),
    [
        (facade, owner, name)
        for facade, owners in _MOVED_METHODS.items()
        for owner, names in owners.items()
        for name in names
    ],
)
def test_a_moved_method_is_its_owners_function_and_left_no_method_behind(
    facade: str, owner: str, name: str
) -> None:
    assert callable(vars(_OWNERS[owner])[name])
    assert not hasattr(_FACADES[facade].AcpClient, name)


class _Reached(BaseException):
    """Raised by a stub to prove the owner read the patched name."""


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


def _moved() -> list[tuple[str, str, str]]:
    return [
        (facade, owner, name)
        for facade, owners in _MOVED.items()
        for owner, names in owners.items()
        for name in names
    ]


# --------------------------------------------------------------------------- #
# Where each name lives.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("facade", "owner", "name"), _moved())
def test_every_moved_name_is_defined_by_its_owner_and_read_through_its_facade(
    facade: str, owner: str, name: str
) -> None:
    owner_module, facade_module = _OWNERS[owner], _FACADES[facade]
    assert name in vars(owner_module)
    assert getattr(facade_module, name) is vars(owner_module)[name]


def test_a_forwarded_name_is_absent_from_its_facade_and_a_bound_one_is_not_forwarded() -> None:
    for facade in _FACADES.values():
        assert [n for n in facade._EXPORTS if n in vars(facade)] == []
        moved = {n for owner in _MOVED[facade.__name__].values() for n in owner}
        bound = {n for n in moved if n in vars(facade)}
        assert bound & set(facade._EXPORTS) == set()
        assert moved <= bound | set(facade._EXPORTS)


def test_dir_lists_every_forwarded_name() -> None:
    for facade in _FACADES.values():
        assert set(facade._EXPORTS) <= set(dir(facade))


def test_the_forwarding_table_names_each_owner_by_its_dotted_name() -> None:
    for facade in _FACADES.values():
        assert set(facade._EXPORTS.values()) <= set(_OWNERS)
        assert all(isinstance(owner, str) for owner in facade._EXPORTS.values())
        assert {
            name: owner for owner, names in facade._EXPORTS_BY_OWNER.items() for name in names
        } == facade._EXPORTS


def test_an_import_a_test_reaches_through_the_facade_follows_its_reader() -> None:
    """Tests read or patch these through the facade and only moved code reads them, so
    the binding moved with its reader and the facade forwards to it."""
    assert acp_client._EXPORTS["corroborate_launcher_refusal"] == transport_errors.__name__
    assert acp_client.corroborate_launcher_refusal is sandbox.corroborate_launcher_refusal
    assert acp_client._EXPORTS["is_credential_propagation_delay"] == transport_errors.__name__
    assert acp_client.is_credential_propagation_delay is is_credential_propagation_delay


def test_a_module_the_moved_helpers_probe_with_stays_bound_on_its_facade() -> None:
    """``runtime.py``'s own code reads neither ``subprocess`` nor ``weakref``; the moved
    helpers read them through it, so the facade keeps both as its own bindings."""
    for name, module in (("subprocess", subprocess), ("weakref", weakref)):
        assert name not in acp_runtime._EXPORTS
        assert vars(acp_runtime)[name] is module


def test_the_package_exports_are_the_owners_objects() -> None:
    for name in (
        "AcpError",
        "AcpPermissionNeeded",
        "AcpProcessDied",
        "AcpRegistrationRateLimited",
        "AcpTimeoutError",
    ):
        assert getattr(acp_package, name) is vars(transport_errors)[name]
    assert acp_package.AcpClient is acp_client.AcpClient
    assert acp_package.AcpRuntime is acp_runtime.AcpRuntime


def test_the_transports_share_one_copy_of_each_moved_helper() -> None:
    """runtime.py imports the shared helpers from their owners, not a second copy."""
    for name in (
        "write_response_frame_bounded",
        "write_request_frame_bounded",
        "write_notification_best_effort",
        "response_write_window_secs",
        "_stall_window_phrase",
        "_drain_oversize_line",
        "OversizeLineUnrecoverable",
        "_RESPONSE_WRITE_BOUND_SECS",
        "_RESPONSE_WRITE_MIN_PROGRESS_BYTES",
    ):
        assert vars(acp_runtime)[name] is vars(transport_framing)[name]
    for name in ("_get_child_pids", "_capture_child_records", "ChildRecord"):
        assert vars(acp_runtime)[name] is vars(runtime_process_tree)[name]
    for name in ("is_auth_failure_output", "is_sandbox_init_failure_output"):
        assert vars(acp_runtime)[name] is vars(transport_errors)[name]


# --------------------------------------------------------------------------- #
# What a moved object still reports about itself.
# --------------------------------------------------------------------------- #

_RAISED_CLASSES = (
    "AcpError",
    "AcpTimeoutError",
    "AcpPermissionNeeded",
    "AcpProcessDied",
    "AcpAuthRequired",
    "AcpSandboxInitFailed",
    "AcpRegistrationRateLimited",
    "AcpToolGateUnroutable",
    "PiGateExtensionTampered",
    "AcpModelUnavailable",
    "AcpPromptBusy",
)


@pytest.mark.parametrize("name", _RAISED_CLASSES)
def test_a_moved_exception_is_named_by_the_path_callers_import_it_from(name: str) -> None:
    """An error chain renders each raised class as ``module.qualname`` and that text
    reaches the Subagents panel, so the owner module must not leak into it."""
    cls = vars(transport_errors)[name]
    assert cls.__module__ == acp_client.__name__
    assert cls.__qualname__ == name


def test_an_error_chain_names_the_client_path() -> None:
    error = transport_errors.AcpError("boom")
    assert subagent._describe_exception(error) == f"{acp_client.__name__}.AcpError: boom"


def test_oversize_line_unrecoverable_keeps_the_client_path_too() -> None:
    cls = transport_framing.OversizeLineUnrecoverable
    assert cls.__module__ == acp_client.__name__
    assert subagent._describe_exception(cls("x")) == f"{acp_client.__name__}.{cls.__qualname__}: x"


def test_the_owners_log_under_the_names_their_code_logged_under() -> None:
    assert transport_errors.logger is acp_client.logger
    assert runtime_process_tree.logger is acp_client.logger
    # The launch helpers and the per-process hosts were the client's code.
    for owner in (
        launch,
        claude_harness,
        opencode_harness,
        goose_harness,
        pi_harness,
        deepseek_harness,
    ):
        assert getattr(owner, "logger", acp_client.logger) is acp_client.logger, owner
    assert runtime_start.logger is acp_runtime.logger
    assert not hasattr(transport_framing, "logger")
    assert not hasattr(runtime_models, "logger")


# --------------------------------------------------------------------------- #
# A patch through a facade reaches the owner's own reader.
# --------------------------------------------------------------------------- #


def test_a_patched_child_walk_reaches_the_descendant_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp_client, "_direct_children", _raiser("_direct_children"))
    with pytest.raises(_Reached):
        acp_client._get_child_pids(1)


def test_a_patched_identity_read_reaches_the_record_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_client, "_read_basename", _raiser("_read_basename"))
    with pytest.raises(_Reached):
        acp_client._capture_child_records([os.getpid()])


def test_a_patched_ownership_check_reaches_the_escaped_child_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(acp_client, "_is_our_child", _raiser("_is_our_child"))
    with pytest.raises(_Reached):
        acp_client._kill_escaped_children({99_999_999_999: None})


@pytest.mark.asyncio
async def test_a_patched_proactor_probe_reaches_the_write_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Transport:
        def get_write_buffer_size(self) -> int:
            return 0

    stdin = SimpleNamespace(transport=_Transport())
    monkeypatch.setattr(acp_client, "_is_proactor_loop", _raiser("_is_proactor_loop"))
    with pytest.raises(_Reached):
        acp_client.response_write_window_secs(stdin, 5.0)


def test_a_patched_platform_window_reaches_the_write_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_client, "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS", 123.0)
    assert acp_client.response_write_window_secs(object(), 5.0) == 123.0


def test_a_patched_propagation_probe_reaches_the_provider_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_client, "is_credential_propagation_delay", _raiser("probe"))
    with pytest.raises(_Reached):
        acp_client.classify_provider_error("nothing the earlier patterns name")


def test_a_patched_child_list_reaches_the_descendant_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_runtime, "_own_children", _raiser("_own_children"))
    with pytest.raises(_Reached):
        acp_runtime._iter_descendant_pids(1)


def test_a_patched_rss_read_reaches_the_tree_sum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp_runtime, "_get_rss_mb", _raiser("_get_rss_mb"))
    with pytest.raises(_Reached):
        acp_runtime._rss_tree_mb_for_pids([1])


@pytest.mark.asyncio
async def test_a_patched_concurrency_resolver_reaches_the_session_start_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.config import live

    monkeypatch.setattr(live, "snapshot", lambda: None)
    monkeypatch.setattr(acp_runtime, "_resolve_session_start_concurrency", lambda: 7)
    loop = asyncio.get_running_loop()
    try:
        gate = await asyncio.wait_for(acp_runtime.session_start_gate(), timeout=5)
        assert gate.limit == 7
    finally:
        acp_runtime._session_start_gates.pop(loop, None)


@pytest.mark.asyncio
async def test_a_patched_admission_limit_reaches_the_cold_start_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
    monkeypatch.setattr(acp_runtime, "_cold_start_admissions", registry)
    monkeypatch.setattr(acp_runtime, "_COLD_START_MAX_CONCURRENT", 5)
    admission = acp_runtime._cold_start_admission()
    assert registry[asyncio.get_running_loop()]() is admission
    assert admission.semaphore._value == 5


# --------------------------------------------------------------------------- #
# A module rebound on a facade reaches the moved helper that probes with it.
# --------------------------------------------------------------------------- #


class _ReachedModule:
    """Stands in for a module: reading any attribute of it, or calling it, raises
    ``_Reached`` naming the facade binding the helper read."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, attr: str) -> Any:
        raise _Reached(f"{self._label}.{attr}")

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        raise _Reached(self._label)


_POSIX = SimpleNamespace(IS_WINDOWS=False)
# ``proc_phys_footprint_bytes_for_pid`` answers None so the macOS RSS helpers
# reach their ``ps`` fallback, the branch these rows steer onto.
_POSIX_PS = SimpleNamespace(
    IS_WINDOWS=False,
    trusted_system_bin=lambda _name: "/bin/ps",
    proc_phys_footprint_bytes_for_pid=lambda _pid: None,
)
_LINUX = SimpleNamespace(platform="linux")
_DARWIN = SimpleNamespace(platform="darwin")

#: ``(facade, rebound name, other bindings that steer the helper onto the branch that
#: reads it, helper, arguments)``. Each helper reads the name from the facade its code
#: came from, so the row fails if the helper ever binds its own copy again.
_MODULE_SEAMS: list[tuple[str, str, dict[str, object], str, tuple[object, ...]]] = [
    *[
        row
        for helper, ps in (
            ("_direct_children", _POSIX),
            ("_get_start_time", _POSIX_PS),
            ("_read_basename", _POSIX_PS),
        )
        for row in (
            ("kiro_crew.acp.client", "platform_compat", {}, helper, (1,)),
            ("kiro_crew.acp.client", "sys", {"platform_compat": _POSIX}, helper, (1,)),
            (
                "kiro_crew.acp.client",
                "subprocess_mod",
                {"platform_compat": ps, "sys": _DARWIN},
                helper,
                (1,),
            ),
        )
    ],
    # ``_get_start_time`` reads its Linux stat through ``platform_compat.read_proc_stat``,
    # so only the other two reach ``Path`` on the facade.
    *[
        (
            "kiro_crew.acp.client",
            "Path",
            {"platform_compat": _POSIX, "sys": _LINUX},
            helper,
            (1,),
        )
        for helper in ("_direct_children", "_read_basename")
    ],
    ("kiro_crew.acp.client", "platform_compat", {}, "_capture_child_records", ([1],)),
    ("kiro_crew.acp.client", "platform_compat", {}, "_is_our_child", (1, "start", b"node")),
    ("kiro_crew.acp.client", "platform_compat", {}, "_kill_escaped_children", ({1: None},)),
    ("kiro_crew.acp.runtime", "sys", {}, "_get_rss_mb", (1,)),
    ("kiro_crew.acp.runtime", "platform_compat", {"sys": _DARWIN}, "_get_rss_mb", (1,)),
    (
        "kiro_crew.acp.runtime",
        "subprocess",
        {"sys": _DARWIN, "platform_compat": _POSIX_PS},
        "_get_rss_mb",
        (1,),
    ),
    ("kiro_crew.acp.runtime", "os", {}, "_own_children", (1,)),
    ("kiro_crew.acp.runtime", "platform_compat", {}, "_ps_process_table", ()),
    (
        "kiro_crew.acp.runtime",
        "subprocess",
        {"platform_compat": _POSIX_PS},
        "_ps_process_table",
        (),
    ),
    ("kiro_crew.acp.runtime", "sys", {}, "_get_rss_tree_mb", (1,)),
    ("kiro_crew.acp.runtime", "platform_compat", {"sys": _DARWIN}, "_get_rss_tree_mb", (1,)),
]

#: Inert stand-ins for the process-tree helpers a row's helper may call next, so a row
#: that missed its seam falls through to an answer instead of the host's processes.
_INERT_CALLEES: dict[str, Callable[..., object]] = {
    "_direct_children": lambda _pid: [],
    "_read_basename": lambda _pid: None,
    "_is_our_child": lambda *_args, **_kwargs: False,
    "_get_rss_mb": lambda _pid: None,
    "_own_children": lambda _pid: [],
    "_iter_descendant_pids": lambda *_args, **_kwargs: [],
    "_rss_tree_mb_for_pids": lambda _pids: None,
    "_ps_process_table": lambda: None,
}


@pytest.mark.parametrize(
    ("facade", "name", "steer", "helper", "args"),
    _MODULE_SEAMS,
    ids=[f"{row[0].rsplit('.', 1)[1]}.{row[1]}->{row[3]}" for row in _MODULE_SEAMS],
)
def test_a_module_rebound_on_the_facade_reaches_the_moved_helper(
    monkeypatch: pytest.MonkeyPatch,
    facade: str,
    name: str,
    steer: dict[str, object],
    helper: str,
    args: tuple[object, ...],
) -> None:
    module = _FACADES[facade]
    for callee, inert in _INERT_CALLEES.items():
        if callee != helper:
            monkeypatch.setattr(runtime_process_tree, callee, inert)
    # A cached process table would answer before the helper probes anything.
    monkeypatch.setattr(runtime_process_tree, "_ps_table_cache", None)
    for other, value in steer.items():
        monkeypatch.setattr(module, other, value)
    label = f"{facade}.{name}"
    monkeypatch.setattr(module, name, _ReachedModule(label))
    with pytest.raises(_Reached) as reached:
        getattr(runtime_process_tree, helper)(*args)
    assert str(reached.value).startswith(label)
    # The helper itself read the stand-in, not a callee it reached first.
    frames = [entry.name for entry in reached.traceback]
    assert frames[-2:] in ([helper, "__getattr__"], [helper, "__call__"]), frames


@pytest.mark.parametrize(
    ("helper", "platform"),
    [("_direct_children", _POSIX), ("_get_start_time", _POSIX_PS), ("_read_basename", _POSIX_PS)],
)
def test_a_patched_spawn_call_reaches_the_client_s_process_probes(
    monkeypatch: pytest.MonkeyPatch, helper: str, platform: SimpleNamespace
) -> None:
    """The owner's own ``subprocess_mod``, bound for the spawn audit, is the module the
    client binds under that name, so a patch of one of its attributes reaches the
    probes as a rebinding of the whole name does."""
    assert runtime_process_tree.subprocess_mod is acp_client.subprocess_mod is subprocess
    monkeypatch.setattr(acp_client, "platform_compat", platform)
    monkeypatch.setattr(acp_client, "sys", _DARWIN)
    monkeypatch.setattr(acp_client.subprocess_mod, "check_output", _raiser("check_output"))
    with pytest.raises(_Reached):
        getattr(runtime_process_tree, helper)(1)


def test_a_clock_rebound_on_the_runtime_reaches_the_process_table_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_process_tree, "_ps_table_cache", (1.0, "cached"))
    monkeypatch.setattr(acp_runtime, "time", SimpleNamespace(monotonic=lambda: 1.0))
    assert runtime_process_tree._ps_process_table() == "cached"
    monkeypatch.setattr(acp_runtime, "time", SimpleNamespace(monotonic=lambda: 1e9))
    monkeypatch.setattr(acp_runtime, "platform_compat", _ReachedModule("runtime.platform_compat"))
    with pytest.raises(_Reached):
        runtime_process_tree._ps_process_table()


class _Clock:
    """A ``time`` stand-in whose ``monotonic`` answers each queued reading in turn."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def monotonic(self) -> float:
        return self._readings.pop(0)


@pytest.mark.asyncio
async def test_a_clock_rebound_on_the_runtime_reaches_the_cold_start_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_runtime, "time", _Clock(10.0, 10.25))
    admission = runtime_start._ColdStartAdmission(1)
    assert await admission.acquire(StartPriority.BACKGROUND) == 250.0
    admission.release(StartPriority.BACKGROUND)


@pytest.mark.asyncio
async def test_a_clock_rebound_on_the_runtime_reaches_the_session_start_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_runtime, "time", _Clock(3.0, 3.5))
    permit = await runtime_start.SessionStartGate(1).acquire()
    assert permit.queue_wait_ms == 500.0
    permit.release()


def test_a_clock_rebound_on_the_runtime_reaches_the_start_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.adaptive import controller

    samples: list[float] = []
    recorder = SimpleNamespace(record_start=lambda elapsed, **_kw: samples.append(elapsed))
    monkeypatch.setattr(controller, "current", lambda: recorder)
    monkeypatch.setattr(acp_runtime, "time", _Clock(2.0))
    runtime_start._record_session_start(1.5, ok=True)
    assert samples == [500.0]


def _collector_runtime() -> SimpleNamespace:
    async def _teardown(_session_id: str) -> None:
        return None

    return SimpleNamespace(
        _pending_requests={}, _dead=False, _start_collectors={}, _teardown_late_session=_teardown
    )


@pytest.mark.asyncio
async def test_a_clock_rebound_on_the_runtime_reaches_the_start_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_runtime, "time", _Clock(7.0, 9.0))
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    collector = runtime_start.StartCollector(
        _collector_runtime(), 1, future, permit=None, timeout=5
    )
    assert collector.created_at == 7.0
    future.set_result({})
    await asyncio.wait_for(collector.start().settled.wait(), timeout=5)
    assert collector.outcome == runtime_start.START_OUTCOME_ERROR


@pytest.mark.asyncio
async def test_a_death_class_rebound_on_the_runtime_reaches_the_start_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the rebinding the collector's catch-all arm would take this error."""

    class _Death(Exception):
        pass

    monkeypatch.setattr(acp_runtime, "AcpRuntimeDead", _Death)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    future.set_exception(_Death())
    collector = runtime_start.StartCollector(
        _collector_runtime(), 2, future, permit=None, timeout=5
    ).start()
    await asyncio.wait_for(collector.settled.wait(), timeout=5)
    assert collector.outcome == runtime_start.START_OUTCOME_RUNTIME_DEAD


@pytest.mark.asyncio
async def test_a_handle_class_rebound_on_the_runtime_reaches_the_transcript_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaned: list[str] = []
    monkeypatch.setattr(
        acp_runtime, "AcpSessionHandle", SimpleNamespace(cleanup_transcript_files=cleaned.append)
    )
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    future.set_result({"sessionId": "late-1"})
    collector = runtime_start.StartCollector(
        _collector_runtime(), 3, future, permit=None, timeout=5, memory_mode="incognito"
    ).start()
    await asyncio.wait_for(collector.settled.wait(), timeout=5)
    assert collector.outcome == runtime_start.START_OUTCOME_TORN_DOWN
    assert cleaned == ["late-1"]


@pytest.mark.asyncio
async def test_a_weakref_rebound_on_the_runtime_reaches_the_admission_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_runtime, "_cold_start_admissions", weakref.WeakKeyDictionary())
    monkeypatch.setattr(acp_runtime, "weakref", _ReachedModule("runtime.weakref"))
    with pytest.raises(_Reached, match="runtime.weakref.ref"):
        acp_runtime._cold_start_admission()


# --------------------------------------------------------------------------- #
# The forwarding round-trips, whichever patching tool is used.
# --------------------------------------------------------------------------- #

_ROUND_TRIP = [
    (acp_client, runtime_process_tree, "_is_our_child"),
    (acp_client, transport_errors, "_is_transient_raw_error"),
    (acp_client, transport_framing, "_RESPONSE_WRITE_BOUND_SECS"),
    (acp_runtime, runtime_start, "_resolve_session_start_concurrency"),
    (acp_runtime, runtime_process_tree, "_get_rss_mb"),
]


@pytest.mark.parametrize(("facade", "owner", "name"), _ROUND_TRIP)
def test_a_monkeypatch_through_the_facade_lands_on_the_owner_and_is_undone(
    facade: ModuleType, owner: ModuleType, name: str
) -> None:
    original = vars(owner)[name]
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "stub")
        assert vars(owner)[name] == "stub" and getattr(facade, name) == "stub"
        assert name not in vars(facade)
    assert vars(owner)[name] is original and name not in vars(facade)


@pytest.mark.parametrize(("facade", "owner", "name"), _ROUND_TRIP)
def test_mock_patch_by_object_and_by_dotted_name_round_trip(
    facade: ModuleType, owner: ModuleType, name: str
) -> None:
    original = vars(owner)[name]
    with mock.patch.object(facade, name, "outer"):
        with mock.patch(f"{facade.__name__}.{name}", "inner"):
            assert vars(owner)[name] == "inner"
        assert vars(owner)[name] == "outer"
    assert vars(owner)[name] is original and name not in vars(facade)


@pytest.mark.parametrize(("facade", "owner", "name"), _ROUND_TRIP)
def test_monkeypatch_and_mock_nest_either_way_through_the_facade(
    facade: ModuleType, owner: ModuleType, name: str
) -> None:
    original = vars(owner)[name]
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(facade, name, "mp")
        with mock.patch.object(facade, name, "mock"):
            assert vars(owner)[name] == "mock"
        assert vars(owner)[name] == "mp"
    with mock.patch.object(facade, name, "mock"):
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, name, "mp")
            assert vars(owner)[name] == "mp"
        assert vars(owner)[name] == "mock"
    assert vars(owner)[name] is original and name not in vars(facade)


@pytest.mark.parametrize(("facade", "owner", "name"), _ROUND_TRIP)
def test_a_delete_through_the_facade_reaches_the_owner_and_is_undone(
    facade: ModuleType, owner: ModuleType, name: str
) -> None:
    original = vars(owner)[name]
    with pytest.MonkeyPatch.context() as patched:
        patched.delattr(facade, name)
        assert name not in vars(owner) and not hasattr(facade, name)
    assert vars(owner)[name] is original


def test_a_write_of_a_facade_name_stays_on_the_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name the facade binds itself -- a module it imports included, which tests
    rebind on purpose -- is an ordinary attribute write."""
    fake = ModuleType("fake_platform_compat")
    monkeypatch.setattr(acp_runtime, "platform_compat", fake)
    assert vars(acp_runtime)["platform_compat"] is fake
    assert "platform_compat" not in vars(runtime_process_tree)
    assert vars(acp_client)["platform_compat"] is platform_compat
    monkeypatch.setattr(acp_client, "_mise_which", "stub")
    assert vars(acp_client)["_mise_which"] == "stub"


# --------------------------------------------------------------------------- #
# How a read resolves the owner.
# --------------------------------------------------------------------------- #


def test_every_read_resolves_the_owner_through_the_import_system() -> None:
    """Each read asks ``importlib`` for the owner, so nothing here can go stale: it
    answers from ``sys.modules`` and waits on the import lock while an owner's body
    is still running, which a mapping held here could do neither of."""
    calls: list[str] = []
    real_import = importlib.import_module

    def counting(target: str, package: str | None = None) -> ModuleType:
        calls.append(target)
        return real_import(target, package)

    with mock.patch.object(importlib, "import_module", counting):
        first = acp_client.resolve_usable_model
        second = acp_runtime.SessionStartGate
    assert calls == [runtime_models.__name__, runtime_start.__name__]
    assert first is runtime_models.resolve_usable_model
    assert second is runtime_start.SessionStartGate


def test_a_loaded_owner_is_read_without_a_fresh_import() -> None:
    """Once loaded, an owner answers from ``sys.modules``: reading, writing and
    undoing a forwarded name never re-executes an import."""
    original = vars(runtime_process_tree)["_is_our_child"]
    loaded = set(sys.modules)
    with mock.patch.object(acp_client, "_is_our_child") as stub:
        assert acp_client._is_our_child is stub
    assert acp_client._is_our_child is original
    assert set(sys.modules) == loaded


@pytest.mark.parametrize("facade", list(_FACADES.values()), ids=list(_FACADES))
def test_a_reader_waits_for_an_owner_another_thread_is_still_importing(
    facade: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read of a forwarded name imports its owner. A second thread reading the name
    meanwhile must wait for that import: the half-built module it would find in
    ``sys.modules`` has no attribute yet, so reading it raises ``AttributeError``."""
    module_name, name = "_acp_facade_slow_owner_probe", "_slow_owner_probe"
    outcome: dict[str, object] = {}

    def second_reader() -> None:
        try:
            outcome["value"] = getattr(facade, name)
        except AttributeError as exc:
            outcome["error"] = repr(exc)

    reader = threading.Thread(target=second_reader, daemon=True)

    class _SlowOwner(importlib.abc.Loader):
        def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
            return None

        def exec_module(self, module: ModuleType) -> None:
            reader.start()
            # A reader that waits on this import's lock cannot finish before it does.
            reader.join(timeout=0.5)
            outcome["waited"] = reader.is_alive()
            setattr(module, name, "ready")

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(
            self, fullname: str, path: object, target: object = None
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname != module_name:
                return None
            return importlib.util.spec_from_loader(fullname, _SlowOwner())

    monkeypatch.setattr(sys, "meta_path", [_Finder(), *sys.meta_path])
    monkeypatch.setitem(facade._EXPORTS, name, module_name)
    try:
        assert getattr(facade, name) == "ready"
    finally:
        if reader.is_alive():
            reader.join(timeout=10)
        sys.modules.pop(module_name, None)
    assert not reader.is_alive()
    assert outcome == {"waited": True, "value": "ready"}


def test_an_unknown_name_is_an_attribute_error() -> None:
    for facade in _FACADES.values():
        with pytest.raises(AttributeError, match="no_such_name"):
            facade.no_such_name  # noqa: B018
        assert getattr(facade, "no_such_name", None) is None


# --------------------------------------------------------------------------- #
# Star import. ``import *`` consults ``__all__`` and never ``__getattr__``.
# --------------------------------------------------------------------------- #


def _star_import(tmp_path: Path, facade: str) -> dict[str, object]:
    """Run ``from <facade> import *`` for real, in a probe module the import
    machinery loads from a file, and return what it bound."""
    probe = tmp_path / "acp_star_probe.py"
    probe.write_text(f"from {facade} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("acp_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def test_a_client_star_import_binds_the_moved_public_names(tmp_path: Path) -> None:
    bound = _star_import(tmp_path, acp_client.__name__)
    public_moved = {
        n for owner in _MOVED[acp_client.__name__].values() for n in owner if not n.startswith("_")
    }
    assert sorted(public_moved - set(bound)) == []
    assert bound["resolve_usable_model"] is runtime_models.resolve_usable_model
    assert bound["AcpError"] is transport_errors.AcpError
    assert [n for n in acp_client.__all__ if n.startswith("_")] == []


def test_the_runtime_star_import_is_the_declared_list(tmp_path: Path) -> None:
    assert acp_runtime.__all__ == [
        "AcpRuntime",
        "AcpRuntimeError",
        "AcpSessionStartTimeout",
        "AcpToolSurfaceBindingError",
        "AcpWorkspaceBindingError",
        "AcpRuntimeDead",
        "AcpRequestTimeout",
        "AcpRuntimeOverloaded",
        "SessionStartGate",
        "StartCollector",
        "AcpRuntimeProtocol",
        "AcpSessionHandle",
        "KIRO_CLI_SUBCMD",
        "PROTOCOL_VERSION",
        "PROTOCOL_VERSION_KAS",
    ]
    bound = _star_import(tmp_path, acp_runtime.__name__)
    assert bound["SessionStartGate"] is runtime_start.SessionStartGate
    assert bound["StartCollector"] is runtime_start.StartCollector


# --------------------------------------------------------------------------- #
# Static shape of each facade.
# --------------------------------------------------------------------------- #


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _bare_loads(tree: ast.Module, forwarded: set[str]) -> list[tuple[int, str]]:
    """Every bare read of a forwarded name outside an import statement."""
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sorted(
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in forwarded
        and node.lineno not in import_lines
    )


@pytest.mark.parametrize("facade", list(_FACADES.values()), ids=list(_FACADES))
def test_the_facade_reads_no_forwarded_name_as_a_bare_global(facade: ModuleType) -> None:
    """A function defined in a facade resolves a bare global through the facade's own
    namespace, which ``__getattr__`` never sees, so such a read would need the facade
    to bind the name -- a second copy no patch of the owner reaches. Every line counts,
    the ``TYPE_CHECKING`` block's included."""
    assert _bare_loads(_tree(facade), set(facade._EXPORTS)) == []


def test_the_bare_global_scan_can_fail() -> None:
    tree = ast.parse("from x import y\n\ndef f():\n    return _get_child_pids\n")
    assert _bare_loads(tree, {"_get_child_pids"}) == [(4, "_get_child_pids")]
    assert (
        _bare_loads(ast.parse("from x import (\n    _get_child_pids,\n)\n"), {"_get_child_pids"})
        == []
    )


@pytest.mark.parametrize("facade", list(_FACADES.values()), ids=list(_FACADES))
def test_the_module_getattr_is_hidden_from_type_checkers(facade: ModuleType) -> None:
    tree = _tree(facade)
    defined = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"
    ]
    hidden = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "not TYPE_CHECKING"
    ]
    assert len(defined) == 1 and len(hidden) == 1
    assert defined[0] in hidden[0].body
    assert callable(vars(facade).get("__getattr__"))  # still the resolver at run time


@pytest.mark.parametrize("facade", list(_FACADES.values()), ids=list(_FACADES))
def test_the_type_checking_names_are_exactly_the_forwarded_ones(facade: ModuleType) -> None:
    """mypy sees a forwarded name only through a ``TYPE_CHECKING`` import, so each one
    is imported there, from the owner the table names, and nothing else is."""
    tree = _tree(facade)
    blocks = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
    ]
    typed: dict[str, str] = {}
    for block in blocks:
        for node in ast.walk(block):
            if isinstance(node, ast.ImportFrom) and node.module in _OWNERS:
                for alias in node.names:
                    typed[alias.asname or alias.name] = node.module
    assert typed == facade._EXPORTS


_MODULE_ITSELF = "<module>"


def _facade_imports(tree: ast.Module, package: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(enclosing function, facade, names)`` for each import of a facade in *tree*, a
    module of *package*; ``"<module>"`` for one at module level. An import of the
    facade module itself (``from kiro_crew.acp import client``, ``from . import
    client``, ``import kiro_crew.acp.client``) names ``("<module>",)``. A
    ``TYPE_CHECKING`` import is not one."""
    found: list[tuple[str, str, tuple[str, ...]]] = []

    def visit(node: ast.AST, where: str) -> None:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            where = node.name if where == "<module>" else f"{where}.{node.name}"
        if isinstance(node, ast.ImportFrom):
            spelled = "." * node.level + (node.module or "")
            module = importlib.util.resolve_name(spelled, package) if node.level else spelled
            if module in _FACADES:
                found.append((where, module, tuple(a.name for a in node.names)))
            found.extend(
                (where, f"{module}.{a.name}", (_MODULE_ITSELF,))
                for a in node.names
                if f"{module}.{a.name}" in _FACADES
            )
        if isinstance(node, ast.Import):
            found.extend(
                (where, a.name, (_MODULE_ITSELF,)) for a in node.names if a.name in _FACADES
            )
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, "<module>")
    return found


def _owner_facade_imports(owner: ModuleType) -> list[tuple[str, str, tuple[str, ...]]]:
    return _facade_imports(_tree(owner), owner.__name__.rpartition(".")[0])


def test_the_facade_import_scan_reads_every_spelling() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew.acp import client\n"
        "from . import runtime as rt\n"
        "import kiro_crew.acp.client\n"
        "from kiro_crew.acp import types\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.acp.runtime import AcpRuntime\n"
        "def f():\n"
        "    from kiro_crew.acp import runtime\n"
        "    from .client import sys\n"
        "class C:\n"
        "    def m(self):\n"
        "        from kiro_crew.acp.runtime import time\n"
    )
    assert _facade_imports(ast.parse(source), "kiro_crew.acp") == [
        ("<module>", "kiro_crew.acp.client", ("<module>",)),
        ("<module>", "kiro_crew.acp.runtime", ("<module>",)),
        ("<module>", "kiro_crew.acp.client", ("<module>",)),
        ("f", "kiro_crew.acp.runtime", ("<module>",)),
        ("f", "kiro_crew.acp.client", ("sys",)),
        ("C.m", "kiro_crew.acp.runtime", ("time",)),
    ]


#: Every function in an owner that imports from a facade: what it reads there, and
#: from which facade -- always the one its code came from.
_SEAM_IMPORTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    **{
        ("kiro_crew.acp.runtime_process_tree", helper): (
            "kiro_crew.acp.client",
            ("Path", "platform_compat", "subprocess_mod", "sys"),
        )
        for helper in ("_direct_children", "_read_basename")
    },
    ("kiro_crew.acp.runtime_process_tree", "_get_start_time"): (
        "kiro_crew.acp.client",
        ("platform_compat", "subprocess_mod", "sys"),
    ),
    **{
        ("kiro_crew.acp.runtime_process_tree", helper): (
            "kiro_crew.acp.client",
            ("platform_compat",),
        )
        for helper in ("_capture_child_records", "_is_our_child", "_kill_escaped_children")
    },
    ("kiro_crew.acp.runtime_process_tree", "_get_rss_mb"): (
        "kiro_crew.acp.runtime",
        ("platform_compat", "subprocess", "sys"),
    ),
    ("kiro_crew.acp.runtime_process_tree", "_own_children"): ("kiro_crew.acp.runtime", ("os",)),
    ("kiro_crew.acp.runtime_process_tree", "_ps_process_table"): (
        "kiro_crew.acp.runtime",
        ("platform_compat", "subprocess", "time"),
    ),
    ("kiro_crew.acp.runtime_process_tree", "_get_rss_tree_mb"): (
        "kiro_crew.acp.runtime",
        ("platform_compat", "sys"),
    ),
    **{
        ("kiro_crew.acp.runtime_start", function): ("kiro_crew.acp.runtime", ("time",))
        for function in (
            "_ColdStartAdmission.acquire",
            "_record_session_start",
            "SessionStartGate.acquire",
            "StartCollector.__init__",
        )
    },
    ("kiro_crew.acp.runtime_start", "_cold_start_admission"): (
        "kiro_crew.acp.runtime",
        ("weakref",),
    ),
    ("kiro_crew.acp.runtime_start", "StartCollector._run"): (
        "kiro_crew.acp.runtime",
        ("AcpRuntimeDead", "AcpSessionHandle", "time"),
    ),
    # The binary ladders read the client's mise probe and PATH augmentation, which its
    # unit tests rebind there.
    **{
        (owner, ladder): ("kiro_crew.acp.client", ("_mise_which", "augmented_path"))
        for owner, ladder in (
            ("kiro_crew.acp.launch", "_resolve_node_adapter_argv"),
            ("kiro_crew.acp.launch", "_resolve_self_served_bin"),
            ("kiro_crew.acp.harness.claude", "_resolve_claude_code_executable"),
            ("kiro_crew.acp.harness.pi", "_resolve_pi_bin"),
        )
    },
    # A routing read-back builds its child's environment with the session's own
    # credential repair and PATH augmentation, which stayed on the client.
    **{
        (owner, readback): ("kiro_crew.acp.client", ("_resolve_spawn_env", "augmented_path"))
        for owner, readback in (
            ("kiro_crew.acp.harness.pi", "_verify_pi_gate"),
            ("kiro_crew.acp.harness.deepseek", "_verify_deepseek_gate"),
            ("kiro_crew.acp.harness.opencode", "_verify_opencode_routing"),
        )
    },
}


def test_no_owner_imports_a_facade_at_module_level() -> None:
    """Owners are the lower layer: a facade imports them at load, never the reverse."""
    for owner in _OWNERS.values():
        assert [row for row in _owner_facade_imports(owner) if row[0] == "<module>"] == []


def test_an_owner_reads_a_facade_only_for_the_listed_seams() -> None:
    """A function-local import of a facade is the one way an owner reads it, and each
    is listed: the helper reads there what a test may rebind there."""
    found = {
        (owner.__name__, where): (facade, names)
        for owner in _OWNERS.values()
        for where, facade, names in _owner_facade_imports(owner)
    }
    assert found == _SEAM_IMPORTS


@pytest.mark.parametrize(("site", "seam"), list(_SEAM_IMPORTS.items()), ids=repr)
def test_each_seam_is_a_binding_of_the_facade_the_helper_came_from(
    site: tuple[str, str], seam: tuple[str, tuple[str, ...]]
) -> None:
    """The facade holds each name itself -- a forwarded one would be read back from the
    owner, which binds none of them -- and the helper's code came from that facade."""
    owner, function = site
    facade, names = seam
    came_from_the_facade = _MOVED[facade].get(owner, ()) + _MOVED_METHODS.get(facade, {}).get(
        owner, ()
    )
    assert function.split(".", 1)[0] in came_from_the_facade
    module = _FACADES[facade]
    for name in names:
        assert name in vars(module) and name not in module._EXPORTS, name
        if (owner, name) not in _LOAD_TIME_BINDINGS:
            assert name not in vars(_OWNERS[owner]), name


#: An owner's own binding of a seam name, made when the owner loads and read by no
#: function -- each function that reads the name imports the facade's.
#: ``runtime_start`` builds its two loop registries with its ``weakref`` at import;
#: ``runtime_process_tree`` binds ``subprocess_mod`` for the spawn audit, which finds a
#: spawn through the modules a file imports.
_LOAD_TIME_BINDINGS = frozenset(
    {
        ("kiro_crew.acp.runtime_start", "weakref"),
        ("kiro_crew.acp.runtime_process_tree", "subprocess_mod"),
    }
)


def test_each_function_that_reads_a_load_time_binding_imports_it_from_the_facade() -> None:
    for owner, name in _LOAD_TIME_BINDINGS:
        for function in ast.walk(_tree(_OWNERS[owner])):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            reads = any(
                isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
                for n in ast.walk(function)
            )
            shadowed = any(
                isinstance(n, ast.ImportFrom)
                and n.module in _FACADES
                and name in {a.name for a in n.names}
                for n in function.body
            )
            assert shadowed or not reads, function.name


def test_the_owners_depend_on_each_other_one_way() -> None:
    """The owners form layers, read downward only.

    ``transport_errors`` reads the model catalog. The launch tail reads the error
    family and the stdout bound. Each per-process host reads the launch helpers and
    the error family, and the DeepSeek host shares pi's sealed artifact directory.
    Nothing reads a host adapter except another host, and no owner reads back up.
    Both import spellings are read -- ``from <owner> import name`` and ``from
    <package> import <owner>`` -- so an edge cannot hide behind the second.
    """
    edges: set[tuple[str, str]] = set()
    for owner in _OWNERS.values():
        for node in ast.walk(_tree(owner)):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module in _OWNERS:
                edges.add((owner.__name__, node.module))
            for alias in node.names:
                if f"{node.module}.{alias.name}" in _OWNERS:
                    edges.add((owner.__name__, f"{node.module}.{alias.name}"))
    errors, framing, the_launch = (
        transport_errors.__name__,
        transport_framing.__name__,
        launch.__name__,
    )
    hosts = {claude_harness, opencode_harness, goose_harness, pi_harness, deepseek_harness}
    expected = {
        (errors, runtime_models.__name__),
        (the_launch, errors),
        (the_launch, framing),
        (deepseek_harness.__name__, pi_harness.__name__),
        *((host.__name__, the_launch) for host in hosts),
        *((host.__name__, errors) for host in hosts - {goose_harness}),
    }
    assert edges == expected


# --------------------------------------------------------------------------- #
# Every moved name a test patches through a facade is one the facade forwards.
# --------------------------------------------------------------------------- #

_SETTERS = frozenset({"setattr", "delattr"})
_DYNAMIC = create_guard._DYNAMIC
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)

#: Every module-level name either facade holds or forwards. A patch of a name neither
#: has cannot be a facade patch: ``monkeypatch.setattr`` and ``mock.patch`` refuse a
#: missing name unless told to create it, and every moved name is one of these.
_FACADE_ATTRIBUTES = frozenset().union(
    *(set(vars(module)) | set(module._EXPORTS) for module in _FACADES.values())
)

#: What the file's names mean at one patch site: the scope, and the names whose value
#: the file cannot say there -- a parameter, or a loop or comprehension target that
#: takes something other than a literal's elements.
_Binding = tuple[create_guard._Scope, frozenset[str]]


def _reads(node: ast.AST | None, unknown: frozenset[str]) -> bool:
    return node is not None and any(
        isinstance(n, ast.Name) and n.id in unknown for n in ast.walk(node)
    )


def _bind(binding: _Binding, name: str, value: ast.expr, source: _Binding) -> _Binding:
    """*binding* with *name* bound to what *value* names in *source*, or unknown."""
    scope, unknown = binding
    module = source[0].module(value) if not isinstance(value, ast.Constant) else None
    text = source[0].text(value)
    view = scope.without(frozenset({name}))
    if _reads(value, source[1]) or (module is None and text is None):
        return view, unknown | {name}
    view = copy.copy(view)
    view.names = {**view.names}
    view.strings = {**view.strings}
    view._views = {}
    if module is not None:
        view.names[name] = module
    if text is not None:
        view.strings[name] = text
    return view, unknown - {name}


def _unknown(binding: _Binding, names: frozenset[str]) -> _Binding:
    return binding[0].without(names), binding[1] | names


def _iterate(bindings: list[_Binding], target: ast.expr, iterable: ast.expr) -> list[_Binding]:
    """The bindings a loop body sees: one per element of a literal tuple, list or set,
    unpacked into a tuple target of the same length; the target unknown otherwise."""
    names = frozenset(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
    expanded: list[_Binding] = []
    for binding in bindings:
        literal = isinstance(iterable, (ast.Tuple, ast.List, ast.Set))
        if not literal or _reads(iterable, binding[1]):
            expanded.append(_unknown(binding, names))
            continue
        for element in iterable.elts:
            if isinstance(target, ast.Name):
                expanded.append(_bind(binding, target.id, element, binding))
            elif (
                isinstance(target, (ast.Tuple, ast.List))
                and isinstance(element, (ast.Tuple, ast.List))
                and len(element.elts) == len(target.elts)
                and all(isinstance(part, ast.Name) for part in target.elts)
            ):
                unpacked = binding
                for part, value in zip(target.elts, element.elts):
                    assert isinstance(part, ast.Name)
                    unpacked = _bind(unpacked, part.id, value, binding)
                expanded.append(unpacked)
            else:
                expanded.append(_unknown(binding, names))
    return expanded


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> frozenset[str]:
    # pytest-mock's ``mocker`` stays the patch provider it is everywhere.
    return create_guard._parameters(node) - {"mocker"}


def _patches_at(call: ast.Call, binding: _Binding) -> set[tuple[str, str]]:
    """``(facade, name)`` for each facade attribute *call* patches under *binding*: the
    name ``<dynamic>`` when the file does not spell it, and both ``<dynamic>`` when the
    patched object is one the site cannot name but a facade might be."""
    scope, unknown = binding
    form = create_guard._PATCH_FORMS.get(scope.module(call.func) or "")
    if form is None:
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr in _SETTERS and call.args):
            return set()
        form = "setattr"
    target = create_guard._argument(call, 0, "target")
    if form == "setattr":
        # ``setattr("pkg.mod.name", value)`` / ``delattr("pkg.mod.name")`` spell the
        # target as one dotted string; the object form passes the name second.
        dotted = len(call.args) == (2 if call.func.attr == "setattr" else 1) and not any(
            k.arg in ("value", "name") for k in call.keywords
        )
        if dotted:
            text = scope.text(target) if target is not None else None
            if text is not None and not _reads(target, unknown):
                facade, _, attribute = text.rpartition(".")
                return {(facade, attribute)} if facade in _FACADES else set()
            return {(_DYNAMIC, _DYNAMIC)} if _reads(target, unknown) else set()
        name_node: ast.expr | None = call.args[1]
    else:
        name_node = create_guard._argument(call, 1, "attribute") if form == "object" else None
    name = scope.text(name_node) if name_node is not None else None
    if target is not None and _reads(target, unknown):
        if form == "multiple":
            spelled = {k.arg for k in call.keywords if k.arg} - create_guard._MULTIPLE_PARAMETERS
            possible = not spelled or bool(spelled & _FACADE_ATTRIBUTES)
        else:
            possible = form == "patch" or name is None or name in _FACADE_ATTRIBUTES
        return {(_DYNAMIC, _DYNAMIC)} if possible else set()
    if form != "setattr":
        return {
            (facade, _DYNAMIC if _reads(name_node, unknown) else patched)
            for facade, patched in create_guard._patched_names(call, form, scope)
            if facade in _FACADES
        }
    module = scope.module(target) if target is not None else None
    if module in _FACADES and name_node is not None:
        return {(module, _DYNAMIC if name is None or _reads(name_node, unknown) else name)}
    return set()


def _monkeypatch_targets(tree: ast.Module) -> list[tuple[int, str, str, str]]:
    """``(line, enclosing function, facade, name)`` for each ``monkeypatch.setattr`` /
    ``delattr`` and ``mock.patch*`` of a facade attribute.

    A patch runs once per binding of every loop and comprehension around it: a target
    over a literal takes each element, a tuple target unpacks each element. A patch
    whose object reads a name the site cannot resolve -- a parameter, or a target over
    anything else -- is ``(<dynamic>, <dynamic>)`` when a facade could be that object,
    and a facade patch whose name the site cannot spell names ``<dynamic>``. A patch of
    an object the file names as something else -- an instance, a class -- is none."""
    found: list[tuple[int, str, str, str]] = []

    def visit(node: ast.AST, bindings: list[_Binding], where: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            outer: list[ast.AST] = [*getattr(node, "decorator_list", []), *node.args.defaults]
            outer += [d for d in node.args.kw_defaults if d is not None]
            for child in outer:
                visit(child, bindings, where)
            inner = [_unknown(binding, _parameters(node)) for binding in bindings]
            if not isinstance(node, ast.Lambda):
                where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body if isinstance(node.body, list) else [node.body]:
                visit(child, inner, where)
            return
        if isinstance(node, ast.ClassDef):
            for child in node.decorator_list:
                visit(child, bindings, where)
            where = node.name if where == "<module>" else f"{where}.{node.name}"
            for child in node.body:
                visit(child, bindings, where)
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            visit(node.iter, bindings, where)
            for child in node.body:
                visit(child, _iterate(bindings, node.target, node.iter), where)
            for child in node.orelse:
                visit(child, bindings, where)
            return
        if isinstance(node, _COMPREHENSIONS):
            current = bindings
            for generator in node.generators:
                visit(generator.iter, current, where)
                current = _iterate(current, generator.target, generator.iter)
                for condition in generator.ifs:
                    visit(condition, current, where)
            parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            for part in parts:
                visit(part, current, where)
            return
        if isinstance(node, ast.Call):
            hits: set[tuple[str, str]] = set()
            for binding in bindings:
                hits |= _patches_at(node, binding)
            found.extend((node.lineno, where, facade, name) for facade, name in sorted(hits))
        for child in ast.iter_child_nodes(node):
            visit(child, bindings, where)

    visit(tree, [(create_guard._Scope(tree), frozenset())], "<module>")
    return found


@functools.lru_cache(maxsize=1)
def _facade_patches() -> tuple[tuple[str, int, str, str, str], ...]:
    """``(file, line, enclosing function, facade, name)`` for every facade patch under
    ``test/`` and each ``src/**/tests``, read once for all the census tests."""
    repo = _SRC.parents[2]
    roots = [repo / "test", *sorted((repo / "src" / "kiro_crew").rglob("tests"))]
    patches: list[tuple[str, int, str, str, str]] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not create_guard._worth_parsing(text):
                continue
            relative = path.relative_to(repo).as_posix()
            patches.extend(
                (relative, line, where, facade, name)
                for line, where, facade, name in _monkeypatch_targets(ast.parse(text))
            )
    return tuple(patches)


def test_every_moved_name_a_test_patches_through_a_facade_is_forwarded() -> None:
    """A moved name the facade merely imports would take the patch in the facade's
    namespace only, and the owner's own callers would never see it."""
    moved = {
        (facade, name)
        for facade, owners in _MOVED.items()
        for names in owners.values()
        for name in names
    }
    patches = _facade_patches()
    missed = [
        f"{path}:{line} {facade}.{name}"
        for path, line, _where, facade, name in patches
        if (facade, name) in moved and name not in _FACADES[facade]._EXPORTS
    ]
    assert len(patches) > 100, f"the scan saw only {len(patches)} facade patches"
    assert missed == []


_THIS_FILE = Path(__file__).resolve().relative_to(_SRC.parents[2]).as_posix()

#: Patches that may be of a facade but whose facade or name the scan cannot read from
#: the file, by ``(file, enclosing function)`` and count. Each is a helper that takes
#: its module or name from its caller, or a loop over a table: the launch capture's
#: pass-through tables (``test_the_launch_capture_patches_no_moved_name`` reads them),
#: this file's own facade-parametrized round trips, and ``self`` or fixture modules.
_UNRESOLVED_FACADE_PATCHES: dict[tuple[str, str], int] = {
    ("test/acp_launch_capture.py", "_capture_runtime_served"): 2,
    ("test/acp_launch_capture.py", "_stub_common"): 4,
    ("test/test_acp_pod_home_remap.py", "_remap_calls"): 1,
    ("test/test_harness_parity.py", "test_is_kiro_cli_is_positive"): 2,
    **{
        ("test/test_acp_dynamic_config.py", f"TestUpdateReasoningEffortValues.{test}"): 1
        for test in (
            "test_concurrent_marker_writes_share_the_durable_cap",
            "test_durable_marker_count_uses_retention_cap",
            "test_linked_gateway_parent_cannot_authorize_effort",
            "test_marked_restore_survives_peer_levels_filling_the_cap",
            "test_selected_dynamic_level_survives_cold_restore",
            "test_windows_reserved_level_uses_portable_marker_name",
        )
    },
    (_THIS_FILE, "test_a_delete_through_the_facade_reaches_the_owner_and_is_undone"): 1,
    (_THIS_FILE, "test_a_monkeypatch_through_the_facade_lands_on_the_owner_and_is_undone"): 1,
    (_THIS_FILE, "test_mock_patch_by_object_and_by_dotted_name_round_trip"): 2,
    (_THIS_FILE, "test_monkeypatch_and_mock_nest_either_way_through_the_facade"): 4,
    ("test/test_acp_runtime.py", "_track_untracks"): 1,
    ("test/test_agent_spec_preflight.py", "TestGatewayInstallVerification._run_init_services"): 1,
    ("test/test_channel_session_trust_parity.py", "_run_turn"): 1,
    (
        "test/test_cli_doctor_refactor_facade.py",
        "test_a_patch_here_lands_on_the_family_and_is_undone",
    ): 1,
    (
        "test/test_mcp_oauth_banner.py",
        "TestAnnotationFailsOpen.test_either_lookup_raising_fails_open",
    ): 1,
    ("test/test_pid_lifecycle.py", "TestBrowserSessionOwnerFakeProc._linux_fixture"): 1,
    ("test/test_session_core_audit.py", "test_resolve_agent_model_serves_cache_within_ttl"): 1,
    ("test/test_subagent_shared_scratch.py", "TestSpawnersMountTheTreeWindow._install_capture"): 5,
    ("test/test_taskrunner.py", "_at_workflow_checkpoint"): 1,
}


def test_every_facade_patch_the_scan_cannot_resolve_is_a_listed_one() -> None:
    unresolved: dict[tuple[str, str], int] = {}
    for path, _line, where, facade, name in _facade_patches():
        if _DYNAMIC in (facade, name):
            unresolved[(path, where)] = unresolved.get((path, where), 0) + 1
    assert unresolved == _UNRESOLVED_FACADE_PATCHES


def test_the_launch_capture_patches_no_moved_name() -> None:
    import acp_launch_capture

    names = set(acp_launch_capture._PASSTHROUGH_STUBS) | set(
        acp_launch_capture._ASYNC_PASSTHROUGH_STUBS
    )
    moved = {name for owners in _MOVED.values() for group in owners.values() for name in group}
    assert names and names.isdisjoint(moved)


#: Names a test patches through a facade that the facade AND an owner both bind, each
#: importing its own: such a patch reaches only the facade's own code. Every entry is
#: patched for code that stayed in the facade -- the client's stderr drain and session
#: init (``logger``), ``AcpClient.send_command`` (``redact_exfiltration_urls``), the
#: runtime's response bound and descendant snapshot -- or for the launch the driver
#: starts: each driver hands the launch tail its OWN sandbox wrap, scrub and scratch
#: module (``_launch_tools``), so those patches reach the launch they drive, while a
#: host's routing read-back wraps and scrubs with its adapter module's binding, which
#: that host's tests patch there. ``launch`` on the runtime is the runtime's own call of
#: the tail, which a test wraps to observe the runtime's steps. A new entry is a
#: decision that the patch needs no forwarding.
_SHARED_BINDINGS_PATCHED: frozenset[tuple[str, str]] = frozenset(
    {
        ("kiro_crew.acp.client", "logger"),
        ("kiro_crew.acp.client", "redact_exfiltration_urls"),
        ("kiro_crew.acp.client", "scrub_agent_subprocess_env"),
        ("kiro_crew.acp.client", "wrap_argv"),
        ("kiro_crew.acp.client", "wrap_argv_async"),
        ("kiro_crew.acp.runtime", "_RESPONSE_WRITE_BOUND_SECS"),
        ("kiro_crew.acp.runtime", "_get_child_pids"),
        ("kiro_crew.acp.runtime", "agent_scratch"),
        ("kiro_crew.acp.runtime", "launch"),
        ("kiro_crew.acp.runtime", "wrap_argv"),
        ("kiro_crew.acp.runtime", "wrap_argv_async"),
    }
)


def test_a_patched_name_bound_by_a_facade_and_an_owner_is_a_listed_one() -> None:
    """A name both a facade and an owner bind takes a patch through the facade in the
    facade's namespace only; the owner's readers keep their own binding. Each such
    patch in the test tree is listed, so a new one cannot miss the moved reader
    silently. A seam name is not one: the helper imports the facade's binding when it
    runs."""
    seams = {(facade, name) for facade, names in _SEAM_IMPORTS.values() for name in names}
    shared: set[tuple[str, str]] = set()
    for _path, _line, _where, facade, name in _facade_patches():
        if _DYNAMIC in (facade, name) or (facade, name) in seams:
            continue
        module = _FACADES[facade]
        if name in module._EXPORTS or name not in vars(module):
            continue
        if any(name in vars(owner) for owner in _OWNERS.values()):
            shared.add((facade, name))
    assert shared == _SHARED_BINDINGS_PATCHED


def test_the_patch_scan_reads_every_binding_of_a_patch_site() -> None:
    source = (
        "from unittest import mock\n"
        "from kiro_crew.acp import client, runtime\n"
        "def test_x(monkeypatch, attr):\n"
        '    monkeypatch.setattr(client, "_is_our_child", 1)\n'
        '    monkeypatch.setattr("kiro_crew.acp.runtime._get_rss_mb", 1)\n'
        '    monkeypatch.setattr(other, "_is_our_child", 1)\n'
        "    for module in (runtime, client, other):\n"
        '        monkeypatch.setattr(module, "platform_compat", 1)\n'
        "    for module in MODULES:\n"
        '        mock.patch.object(module, "sys")\n'
        '    for name in ("sys", "time"):\n'
        "        monkeypatch.setattr(runtime, name, 1)\n"
        "    monkeypatch.setattr(client, attr, 1)\n"
        "    for module in (client, runtime):\n"
        '        for name in ("os", "time"):\n'
        "            monkeypatch.setattr(module, name, 1)\n"
        '    for module, name in ((client, "Path"), (runtime, "weakref")):\n'
        "        monkeypatch.setattr(module, name, 1)\n"
        '    [mock.patch.object(m, "sys") for m in (client, runtime)]\n'
        "    [mock.patch.object(client, n) for n in TABLE]\n"
        "def _helper(monkeypatch, module, client):\n"
        '    monkeypatch.setattr(module, "platform_compat", 1)\n'
        '    monkeypatch.setattr(module, "_send_and_await", 1)\n'
        '    monkeypatch.setattr(client, "sys", 1)\n'
        "class TestY:\n"
        "    def test_z(self, monkeypatch):\n"
        '        monkeypatch.setattr(self.module, "config_dir", 1)\n'
        '        monkeypatch.setattr(f"kiro_crew.acp.client.{self.name}", 1)\n'
    )
    assert _monkeypatch_targets(ast.parse(source)) == [
        (4, "test_x", "kiro_crew.acp.client", "_is_our_child"),
        (5, "test_x", "kiro_crew.acp.runtime", "_get_rss_mb"),
        (8, "test_x", "<dynamic>", "<dynamic>"),
        (8, "test_x", "kiro_crew.acp.client", "platform_compat"),
        (8, "test_x", "kiro_crew.acp.runtime", "platform_compat"),
        (10, "test_x", "<dynamic>", "<dynamic>"),
        (12, "test_x", "kiro_crew.acp.runtime", "sys"),
        (12, "test_x", "kiro_crew.acp.runtime", "time"),
        (13, "test_x", "kiro_crew.acp.client", "<dynamic>"),
        (16, "test_x", "kiro_crew.acp.client", "os"),
        (16, "test_x", "kiro_crew.acp.client", "time"),
        (16, "test_x", "kiro_crew.acp.runtime", "os"),
        (16, "test_x", "kiro_crew.acp.runtime", "time"),
        (18, "test_x", "kiro_crew.acp.client", "Path"),
        (18, "test_x", "kiro_crew.acp.runtime", "weakref"),
        (19, "test_x", "kiro_crew.acp.client", "sys"),
        (19, "test_x", "kiro_crew.acp.runtime", "sys"),
        (20, "test_x", "kiro_crew.acp.client", "<dynamic>"),
        (22, "_helper", "<dynamic>", "<dynamic>"),
        (24, "_helper", "<dynamic>", "<dynamic>"),
        (27, "TestY.test_z", "<dynamic>", "<dynamic>"),
        (28, "TestY.test_z", "<dynamic>", "<dynamic>"),
    ]

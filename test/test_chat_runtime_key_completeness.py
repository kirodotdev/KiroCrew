"""Structural lock on ``ChatRuntimeKey``'s completeness.

The key decides WHICH process a chat session may join, so a process-level spawn
input missing from it widens the key silently: two sessions that differ on that
input land on one process and one of them runs under the other's value. The key's
own ``build`` is keyword-only and exhaustive so the mistake cannot be made by
accident at the call site -- but exhaustive is not self-enforcing, because nothing
stops a new input being added somewhere else and never reaching ``build`` at all.

So the shape is locked from outside, over TWO populations, because a process-level
input reaches ``kiro-cli`` through either of two channels:

* a constructor parameter of the runtime that owns the process, and
* a key written into the workspace ``cli.json`` the process reads at startup.

One population alone is not enough: the argv/parameter channel does not carry the
effort level or the Tool Search setting, which travel as files, and a file-only
scan does not carry the model or the sandbox tier. A name in either population
must be either KEYED or listed with the REASON it is not a compatibility
property. An unknown name in either direction fails, so the next person adding a
process-level input has to decide rather than remember.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from kiro_crew.acp.chat_runtime_pool import ChatRuntimeKey
from kiro_crew.acp.runtime import AcpRuntime

#: Runtime constructor parameter -> the key field that carries it.
_SPAWN_INPUT_KEYED = {
    "work_dir": "work_dir",
    "agent": "agent",
    "sandbox_mode": "sandbox_mode",
    "extra_env": "extra_env",
    "mcp_gateway_overlay": "mcp_gateway_overlay",
    "mcp_gateway_socket": "mcp_gateway_socket",
    "model": "model",
    "acp_backend": "acp_backend",
    "member_context": "member_context",
    "memory_mode": "memory_mode",
    "tool_search": "tool_search",
    "shared_scratch": "shared_scratch",
}

#: Runtime constructor parameter -> why it is NOT a compatibility property.
#: A reason, never a boolean: the next reader needs to know why without
#: re-deriving it, and a boolean invites flipping.
_SPAWN_INPUT_NOT_KEYED = {
    "max_age_secs": (
        "A recycle threshold, not an input the child behaves differently under. "
        "A hard module default with no config path, so every chat slot passes the "
        "same value; and the harness narrows it AFTER a key would be built, so a "
        "keyed copy would name a threshold the process is not governed by. The "
        "harness is selected by acp_backend, which IS keyed."
    ),
    "max_rss_mb": (
        "Same as max_age_secs: a hard module default, no config path, and narrowed "
        "per harness after the key is built."
    ),
    "expect_mcp_reports": (
        "Whether sessions wait for the MCP readiness ceiling. A hard default with "
        "no config path; the one caller that passes a different value is the "
        "background session path, which does not share a chat runtime."
    ),
    "crew_agent": (
        "The canonical crew identity for telemetry context and watchdog settings. "
        "Per session, not per process, and it selects no spawn behaviour."
    ),
}

#: Key field -> the workspace ``cli.json`` writer that produces it, for the
#: fields that reach the process as a FILE rather than as a parameter.
_SETTINGS_KEYED = {
    "reasoning_effort": "_write_cli_overlay",
}

#: Every function that takes the workspace ``cli.json`` lock -> what accounts for
#: it. Either the key field it produces, or the reason it needs none.
_CLI_JSON_WRITERS = {
    "_write_cli_overlay": (
        "Writes chat.modelDefaults.<model>.<family key>.effort. Keyed twice: "
        "reasoning_effort for the level, and model because the row is named by it."
    ),
    "_write_tool_search_overlay": ("Writes the flat toolSearch keys. Keyed as tool_search."),
    "_clear_cli_overlay_effort": (
        "Removes an effort entry rather than contributing a spawn input, so it "
        "adds no compatibility property of its own."
    ),
    "prepare_native_skill_projection": (
        "Writes the skill-discovery inheritance keys and rewrites the projected "
        "agent alias. Its inputs are the work directory and a host environment "
        "switch, and the alias follows the agent -- work_dir and agent are both "
        "keyed, so two sessions with one key project identically. The sharing "
        "path also declines to re-project on join."
    ),
}

#: Modules scanned for workspace ``cli.json`` writers: the ones the chat spawn
#: path itself goes through. The Code Review Sage pool holds the same lock for a
#: work directory of its own and never founds or joins a chat runtime, so it is
#: deliberately out of scope rather than silently missing.
_SCANNED_MODULES = (
    "src/kiro_crew/providers/acp.py",
    "src/kiro_crew/acp/skill_projection.py",
)

_LOCK_CALL = "workspace_cli_settings_lock"

#: The module holding the one ``ChatRuntimeKey.build`` call site in the tree.
_BUILD_CALL_SITE = "src/kiro_crew/providers/acp.py"


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def _functions_taking_the_settings_lock(rel_path: str) -> set[str]:
    """Names of functions whose body calls the workspace settings lock."""
    source = (_repo_root() / rel_path).read_text(encoding="utf-8")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called == _LOCK_CALL:
                names.add(node.name)
                break
    return names


def _runtime_spawn_inputs() -> set[str]:
    params = inspect.signature(AcpRuntime.__init__).parameters
    return {name for name in params if name != "self"}


def _key_fields() -> set[str]:
    return set(ChatRuntimeKey.__dataclass_fields__)


class TestEveryProcessLevelInputIsKeyedOrExplained:
    def test_no_spawn_input_is_unaccounted_for(self) -> None:
        """A new runtime parameter must be keyed or given a reason."""
        accounted = set(_SPAWN_INPUT_KEYED) | set(_SPAWN_INPUT_NOT_KEYED)
        unknown = _runtime_spawn_inputs() - accounted
        assert not unknown, (
            "process-level spawn input(s) neither keyed nor explained: "
            f"{sorted(unknown)}. Add each to _SPAWN_INPUT_KEYED with the "
            "ChatRuntimeKey field that carries it, or to _SPAWN_INPUT_NOT_KEYED "
            "with the reason it is per-session or host-level. Leaving it out lets "
            "two sessions that differ on it share one process."
        )

    def test_no_mapping_entry_is_stale(self) -> None:
        """A parameter that goes away must not leave its mapping row behind."""
        accounted = set(_SPAWN_INPUT_KEYED) | set(_SPAWN_INPUT_NOT_KEYED)
        stale = accounted - _runtime_spawn_inputs()
        assert not stale, f"mapping names parameters that do not exist: {sorted(stale)}"

    def test_a_parameter_is_not_claimed_both_ways(self) -> None:
        both = set(_SPAWN_INPUT_KEYED) & set(_SPAWN_INPUT_NOT_KEYED)
        assert not both, f"claimed as keyed AND not keyed: {sorted(both)}"

    def test_every_keyed_parameter_names_a_real_field(self) -> None:
        fields = _key_fields()
        wrong = {p: f for p, f in _SPAWN_INPUT_KEYED.items() if f not in fields}
        assert not wrong, f"mapped to key fields that do not exist: {wrong}"

    def test_every_reason_is_a_real_reason(self) -> None:
        """A reason has to explain; a placeholder does not lock anything."""
        thin = {
            name: reason
            for name, reason in _SPAWN_INPUT_NOT_KEYED.items()
            if len(reason.split()) < 8
        }
        assert not thin, f"reasons too thin to be reasons: {sorted(thin)}"


class TestEveryKeyFieldHasAProducer:
    def test_no_key_field_is_orphaned(self) -> None:
        """A field nothing produces cannot discriminate, so it must be justified.

        This is the other direction of the same lock: a key field with no spawn
        input and no settings writer behind it is dead weight, and dead weight in
        a compatibility key reads as protection that is not there.
        """
        produced = set(_SPAWN_INPUT_KEYED.values()) | set(_SETTINGS_KEYED)
        orphaned = _key_fields() - produced
        assert not orphaned, (
            f"key field(s) no spawn input or settings writer produces: "
            f"{sorted(orphaned)}. Either pass the value at the build call site or "
            "drop the field."
        )

    def test_settings_channel_fields_name_a_real_writer(self) -> None:
        wrong = {f: w for f, w in _SETTINGS_KEYED.items() if w not in _CLI_JSON_WRITERS}
        assert not wrong, f"named writers that are not in the writer mapping: {wrong}"


class TestEverySettingsWriterIsAccountedFor:
    def test_no_cli_json_writer_is_unaccounted_for(self) -> None:
        """A new workspace cli.json writer must say what it contributes.

        The lock is the choke point: every in-product writer of that file takes
        it, so scanning for the lock finds the whole population without a list of
        key names to keep in sync.
        """
        discovered: set[str] = set()
        for rel in _SCANNED_MODULES:
            discovered |= _functions_taking_the_settings_lock(rel)
        unknown = discovered - set(_CLI_JSON_WRITERS)
        assert not unknown, (
            f"function(s) writing the workspace cli.json with nothing said about "
            f"them: {sorted(unknown)}. kiro-cli reads that file once at startup, so "
            "a setting written there is process-level: add the key field it needs, "
            "or record why it needs none."
        )

    def test_the_scan_finds_the_writers_it_is_supposed_to_find(self) -> None:
        """Guard the scan itself: an empty scan would pass the test above."""
        discovered: set[str] = set()
        for rel in _SCANNED_MODULES:
            discovered |= _functions_taking_the_settings_lock(rel)
        assert "_write_cli_overlay" in discovered
        assert "prepare_native_skill_projection" in discovered

    def test_no_writer_mapping_entry_is_stale(self) -> None:
        discovered: set[str] = set()
        for rel in _SCANNED_MODULES:
            discovered |= _functions_taking_the_settings_lock(rel)
        stale = set(_CLI_JSON_WRITERS) - discovered
        assert not stale, f"writer mapping names functions the scan does not see: {sorted(stale)}"


def _build_call_keywords() -> set[str]:
    """The keyword names the one ``ChatRuntimeKey.build`` call site passes."""
    source = (_repo_root() / _BUILD_CALL_SITE).read_text(encoding="utf-8")
    passed: set[str] = set()
    calls = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "attr", None) != "build":
            continue
        owner = getattr(func, "value", None)
        if getattr(owner, "id", None) != "ChatRuntimeKey":
            continue
        calls += 1
        passed |= {kw.arg for kw in node.keywords if kw.arg}
    assert calls == 1, f"expected exactly one ChatRuntimeKey.build call site, found {calls}"
    return passed


class TestTheBuildCallSitePassesEveryField:
    """The field existing is not the same as the call site filling it.

    ``build`` gives every optional field a default, so a field the call site never
    passes is silently constant for every pooled chat -- the key looks like it
    protects that property while it cannot tell any two sessions apart on it. With
    one call site in the tree, that is a permanent hole rather than an edge case,
    which is why this is pinned structurally instead of by value.
    """

    def test_every_key_field_is_passed_by_the_call_site(self) -> None:
        missing = _key_fields() - _build_call_keywords()
        assert not missing, (
            f"the build call site does not pass: {sorted(missing)}. Each of those "
            "fields is therefore at its default for every pooled chat and cannot "
            "discriminate. Pass the value the spawn will really use, read through "
            "the same call the writer of that value uses."
        )

    def test_the_call_site_passes_nothing_the_key_does_not_hold(self) -> None:
        extra = _build_call_keywords() - _key_fields()
        assert not extra, f"call site passes keywords the key has no field for: {sorted(extra)}"

"""``kiro_crew.agent`` stays the agent-materialization patch seam after the split.

The materialization is composed from the ``kiro_crew.agent_materialization`` owners,
and ``kiro_crew.agent`` re-exports every moved name. Tests and src keep reading and
patching ``kiro_crew.agent.<name>``; whichever module defines a name, a patch there has
to reach the code that reads it.

The seam rows patch ONE name on the facade with a stub that raises ``_Reached`` and
then drive a consumer that must read that name. ``_Reached`` derives from
``BaseException`` on purpose: several consumers wrap their collaborators in ``except
Exception`` (the SEL audit, the guest spec's model lookup), and a stub those handlers
could swallow would let a patch that MISSED read as one that landed.
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import os
import pkgutil
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator
from unittest import mock

import pytest
import test_agent_refactor_create_guard as create_guard

from kiro_crew import agent, agent_materialization
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_AGENT_FILE = Path(agent.__file__)
_OWNER_DIR = Path(agent_materialization.__file__).parent
_SRC = _AGENT_FILE.parent
_REPO = _SRC.parents[1]


class _Reached(BaseException):
    """Raised by a stub to prove the consumer read the patched name."""


def _raiser(label: str) -> Callable[..., object]:
    def _stub(*_args: object, **_kwargs: object) -> object:
        raise _Reached(label)

    return _stub


def _value(result: object) -> Callable[..., object]:
    return lambda *_args, **_kwargs: result


def _owners() -> dict[str, ModuleType]:
    return {name: importlib.import_module(name) for name in agent._EXPORTS_BY_OWNER}


def _owner_trees() -> dict[str, ast.Module]:
    return {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_OWNER_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


def _agent_tree() -> ast.Module:
    return ast.parse(_AGENT_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Seam reach. Each case: (patched name, setup(tmp_path, monkeypatch) -> consumer).
# The setup pins every EARLIER collaborator so the consumer reaches the patched name
# without touching the host; the name itself is patched by the test body.
# --------------------------------------------------------------------------- #


def _guest(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return agent._install_guest_agent


def _guest_after_dir(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    mp.setattr(agent, "kiro_agents_dir_path", _value(tmp_path))
    return _guest(tmp_path, mp)


def _research(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return agent._install_research_agent


def _hook_rejected(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return lambda: agent._sel_hook_rejected("trigger", "command", "reason")


def _crew_owned(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return agent.crew_owned_mcp_servers


def _opt_in_entry(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return lambda: agent._managed_opt_in_entry("mcp-work")


def _conductor_servers(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    mp.setattr(agent, "_kirocrew_mcp_invocation", _value(("kirocrew", ["mcp-dashboard"])))
    return lambda: agent._conductor_mcp_servers({})


def _app_spec(tmp_path: Path, mp: pytest.MonkeyPatch) -> Callable[[], object]:
    return lambda: agent._ceiling_filtered_spec("app:srv", {"autoApprove": ["a"]})


SEAMS = [
    # A core name an owner reads as ``agent_mod.<name>``.
    pytest.param("kiro_agents_dir_path", _guest, id="guest<-kiro_agents_dir_path"),
    pytest.param("_atomic_json_write", _guest_after_dir, id="guest<-_atomic_json_write"),
    pytest.param("build_agent_config", _research, id="research<-build_agent_config"),
    pytest.param("sel", _hook_rejected, id="hook_rejected<-sel"),
    pytest.param("_extra_mcp_servers", _crew_owned, id="crew_owned<-_extra_mcp_servers"),
    pytest.param(
        "_kirocrew_mcp_invocation", _opt_in_entry, id="opt_in_entry<-_kirocrew_mcp_invocation"
    ),
    # A moved name another owner reads as ``<owner>.<name>``.
    pytest.param(
        "_mcp_registry_mode", _conductor_servers, id="conductor_servers<-_mcp_registry_mode"
    ),
    # A moved name its own owner reads as a bare global.
    pytest.param("_may_auto_approve", _app_spec, id="ceiling_filtered_spec<-_may_auto_approve"),
]


@pytest.mark.parametrize(("seam", "setup"), SEAMS)
def test_a_patch_on_the_facade_reaches_its_reader(
    seam: str,
    setup: Callable[[Path, pytest.MonkeyPatch], Callable[[], object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, seam, _raiser(seam))
    with pytest.raises(_Reached) as reached:
        consumer()
    assert reached.value.args == (seam,)


def test_the_logger_patched_on_the_facade_reaches_an_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every owner logs through ``agent_mod.logger``, so a patch of the facade's
    logger is seen by moved code exactly as it was when the code lived there."""
    stub = mock.Mock()
    stub.info.side_effect = _Reached("logger.info")
    monkeypatch.setattr(agent, "logger", stub)
    disabled = {"enabled": False, "trigger": "preToolUse", "action": {"type": "command"}}
    with pytest.raises(_Reached) as reached:
        agent.hook_documents_to_object_form([disabled])
    assert reached.value.args == ("logger.info",)


def test_a_constant_patched_on_the_facade_reaches_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(agent, "kiro_agents_dir_path", _value(tmp_path))
    monkeypatch.setattr(agent, "_atomic_json_write", lambda _path, spec: written.append(spec))
    monkeypatch.setattr(agent, "GUEST_AGENT_PROMPT", "patched prompt")
    agent._install_guest_agent()
    assert [spec["prompt"] for spec in written] == ["patched prompt"]

    monkeypatch.setattr(agent, "_MANAGED_MCP_SERVERS", {"only-this": {}})
    monkeypatch.setattr(agent, "_extra_mcp_servers", _value({}))
    assert agent.crew_owned_mcp_servers() == frozenset({"only-this"})


# --------------------------------------------------------------------------- #
# The facade contract: where each name lives and how the facade reaches it.
# --------------------------------------------------------------------------- #


def test_a_reexported_name_is_the_owners_object_and_absent_from_the_core() -> None:
    owners = _owners()
    assert set(agent._EXPORTS.values()) == set(owners)
    for name, owner_name in agent._EXPORTS.items():
        owner = owners[owner_name]
        assert name in vars(owner), f"{owner_name} does not define {name}"
        assert getattr(agent, name) is vars(owner)[name], name
        assert name not in vars(agent), f"{name} is bound in the core and would shadow its owner"
    assert set(agent._EXPORTS) <= set(dir(agent))


def test_the_owner_table_is_the_package() -> None:
    """Every module of the package is an owner the facade loads, and nothing else is."""
    modules = {
        f"{agent_materialization.__name__}.{info.name}"
        for info in pkgutil.iter_modules(agent_materialization.__path__)
    }
    assert set(agent._EXPORTS_BY_OWNER) == modules


def test_every_prompt_is_listed_by_dir() -> None:
    """A read-only suite finds the spec prompts by scanning ``dir(agent)``."""
    prompts = {name for name in dir(agent) if name.endswith("_SYSTEM_PROMPT")}
    assert prompts == {
        "_ASSISTANT_SYSTEM_PROMPT",
        "_CONDUCTOR_SYSTEM_PROMPT",
        "_HEARTBEAT_SYSTEM_PROMPT",
        "_KNOWLEDGE_SYSTEM_PROMPT",
        "_PIPELINE_CONDUCTOR_SYSTEM_PROMPT",
        "_RESEARCH_SYSTEM_PROMPT",
        "_SECURITY_CONDUCTOR_SYSTEM_PROMPT",
        "_WORKER_SYSTEM_PROMPT",
    }


def test_install_agent_is_still_the_rebuild() -> None:
    assert agent.install_agent is agent.rebuild_agent_config


def test_a_write_through_the_facade_lands_on_the_owner_and_is_restored() -> None:
    from kiro_crew.agent_materialization import service_agents, worker_agent

    original = service_agents._install_guest_agent
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(agent, "_install_guest_agent", _value("patched"))
        assert service_agents._install_guest_agent() == "patched"
        assert "_install_guest_agent" not in vars(agent)
    assert service_agents._install_guest_agent is original

    # mock.patch finds no local binding on the facade, so it restores by delattr
    # followed by setattr -- both of which must reach the owner.
    rederive = worker_agent.rederive_worker_agent
    with mock.patch.object(agent, "rederive_worker_agent") as stub:
        assert worker_agent.rederive_worker_agent is stub
    assert worker_agent.rederive_worker_agent is rederive
    assert "rederive_worker_agent" not in vars(agent)


def test_a_write_of_a_core_name_stays_on_the_core(tmp_path: Path) -> None:
    before = agent.KIRO_AGENTS_DIR
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(agent, "KIRO_AGENTS_DIR", tmp_path)
        assert vars(agent)["KIRO_AGENTS_DIR"] == tmp_path
    assert agent.KIRO_AGENTS_DIR == before


# --------------------------------------------------------------------------- #
# Round trips. mock restores a name the facade does not hold by deleting it and
# then setting it back; monkeypatch restores by writing the saved value. Forwarded
# to the owner, each leaves it holding what it held before, however they nest.
# ``mock.patch(..., create=True)`` skips that set and is refused by
# ``test_agent_refactor_create_guard.py`` instead.
# --------------------------------------------------------------------------- #


@pytest.fixture
def owner_guard() -> Iterator[Callable[[ModuleType, str], object]]:
    """Save an owner's binding, and put it back directly on the owner afterwards, so
    an undo that fails in one test cannot leave a later test without the name."""
    saved: list[tuple[ModuleType, str, object]] = []

    def guard(owner: ModuleType, name: str) -> object:
        saved.append((owner, name, vars(owner)[name]))
        return vars(owner)[name]

    yield guard
    for owner, name, value in reversed(saved):
        setattr(owner, name, value)


def test_nested_mock_patches_unwind_one_level_at_a_time(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    from kiro_crew.agent_materialization import managed_mcp

    original = owner_guard(managed_mcp, "crew_owned_mcp_servers")
    outer, inner = _value("outer"), _value("inner")
    with mock.patch.object(agent, "crew_owned_mcp_servers", outer):
        with mock.patch.object(agent, "crew_owned_mcp_servers", inner):
            assert managed_mcp.crew_owned_mcp_servers is inner
        assert managed_mcp.crew_owned_mcp_servers is outer
    assert managed_mcp.crew_owned_mcp_servers is original
    assert "crew_owned_mcp_servers" not in vars(agent)


def test_mock_patch_of_a_dotted_target_lands_on_the_owner_and_is_restored(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    from kiro_crew.agent_materialization import auto_approve

    original = owner_guard(auto_approve, "_may_auto_approve")
    with mock.patch("kiro_crew.agent._may_auto_approve") as stub:
        assert auto_approve._may_auto_approve is stub
    assert auto_approve._may_auto_approve is original
    assert "_may_auto_approve" not in vars(agent)


def test_monkeypatch_and_mock_nest_either_way_through_the_facade(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    from kiro_crew.agent_materialization import conductor_agents

    original = owner_guard(conductor_agents, "_install_conductor_agent")
    by_monkeypatch, by_mock = _value("monkeypatch"), _value("mock")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(agent, "_install_conductor_agent", by_monkeypatch)
        with mock.patch.object(agent, "_install_conductor_agent", by_mock):
            assert conductor_agents._install_conductor_agent is by_mock
        assert conductor_agents._install_conductor_agent is by_monkeypatch
    assert conductor_agents._install_conductor_agent is original

    with mock.patch.object(agent, "_install_conductor_agent", by_mock):
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(agent, "_install_conductor_agent", by_monkeypatch)
            assert conductor_agents._install_conductor_agent is by_monkeypatch
        assert conductor_agents._install_conductor_agent is by_mock
    assert conductor_agents._install_conductor_agent is original
    assert "_install_conductor_agent" not in vars(agent)


def test_monkeypatch_delattr_through_the_facade_removes_and_restores(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    from kiro_crew.agent_materialization import mcp_aliases

    original = owner_guard(mcp_aliases, "_set_tool_aliases")
    with pytest.MonkeyPatch.context() as patched:
        patched.delattr(agent, "_set_tool_aliases")
        assert not hasattr(agent, "_set_tool_aliases")
        assert "_set_tool_aliases" not in vars(mcp_aliases)
    assert mcp_aliases._set_tool_aliases is original


def test_deleting_a_name_through_the_facade_deletes_it_on_the_owner(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    from kiro_crew.agent_materialization import fork_refresh

    owner_guard(fork_refresh, "_FORK_REFRESH_WAIT_SECS")
    del agent._FORK_REFRESH_WAIT_SECS
    assert "_FORK_REFRESH_WAIT_SECS" not in vars(fork_refresh)
    assert not hasattr(agent, "_FORK_REFRESH_WAIT_SECS")


def test_every_read_resolves_the_owner_through_the_import_system() -> None:
    """Each read asks ``importlib`` for the owner, so nothing here can go stale: it
    answers from ``sys.modules`` and waits on the import lock while an owner's body
    is still running, which a mapping held here could do neither of."""
    from kiro_crew.agent_materialization import worker_agent

    calls: list[str] = []
    real_import = importlib.import_module

    def counting(target: str, package: str | None = None) -> ModuleType:
        calls.append(target)
        return real_import(target, package)

    with mock.patch.object(importlib, "import_module", counting):
        first = agent.require_fresh_derived_spec
        second = agent.require_fresh_derived_spec
    assert calls == [worker_agent.__name__] * 2
    assert first is second is worker_agent.require_fresh_derived_spec


def test_a_reader_waits_for_an_owner_another_thread_is_still_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read of a moved name imports its owner. A second thread reading the name
    meanwhile must wait for that import: the half-built module it would find in
    ``sys.modules`` has no attribute yet, so reading it raises ``AttributeError``."""
    module_name, name = "_agent_facade_slow_owner_probe", "_slow_owner_probe"
    outcome: dict[str, object] = {}

    def second_reader() -> None:
        try:
            outcome["value"] = getattr(agent, name)
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
    monkeypatch.setitem(agent._EXPORTS, name, module_name)
    try:
        assert getattr(agent, name) == "ready"
    finally:
        if reader.is_alive():
            reader.join(timeout=10)
        sys.modules.pop(module_name, None)
    assert not reader.is_alive()
    assert outcome == {"waited": True, "value": "ready"}


# --------------------------------------------------------------------------- #
# Star import. ``import *`` consults ``__all__`` and never ``__getattr__``, so the
# re-exported names reach a star importer only through the declared list.
# --------------------------------------------------------------------------- #


def test_every_public_reexport_is_declared_for_a_star_import() -> None:
    public = {name for name in agent._EXPORTS if not name.startswith("_")}
    assert sorted(public - set(agent.__all__)) == []
    assert sorted(name for name in agent.__all__ if name.startswith("_")) == []
    assert [name for name in agent.__all__ if not hasattr(agent, name)] == []


def test_a_star_import_binds_the_moved_public_names(tmp_path: Path) -> None:
    """Run for real, from a probe module file loaded by the import machinery."""
    probe = tmp_path / "agent_star_probe.py"
    probe.write_text("from kiro_crew.agent import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("agent_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "GUEST_AGENT_PROMPT",
        "rederive_worker_agent",
        "crew_owned_mcp_servers",
        "normalize_spec_hooks",
        "rebuild_agent_config",
    ):
        assert vars(module)[name] is getattr(agent, name), name


# --------------------------------------------------------------------------- #
# One home per name. A copy of a core seam, or of another owner's name, would miss
# every patch of ``kiro_crew.agent.<name>``.
# --------------------------------------------------------------------------- #

#: Names an owner binds by import although the core or another owner binds them
#: too: typing constructs, ``Path``, agent-file names and collaborator helpers. None
#: of them is ever patched through the facade, which the next test proves.
_SHARED_IMPORTS = frozenset(
    {
        "AGENT_FILENAME",
        "Any",
        "DERIVED_KEY",
        "Literal",
        "OWNED_KIRO_AGENT_FILES",
        "Path",
        "annotations",
        "config_dir",
        "datetime",
        "is_markdown_spec",
        "mcp_server_alias",
        "safe_context_call",
        "timezone",
    }
)


def test_an_owner_binds_no_name_another_module_owns() -> None:
    core = vars(agent)
    offenders: list[str] = []
    for owner_name, owner in _owners().items():
        for name, value in vars(owner).items():
            if name.startswith("__") or isinstance(value, ModuleType) or name in _SHARED_IMPORTS:
                continue
            if name in core:
                offenders.append(f"{owner_name}.{name} copies the core binding")
            elif agent._EXPORTS.get(name, owner_name) != owner_name:
                offenders.append(f"{owner_name}.{name} copies {agent._EXPORTS[name]}'s binding")
    assert offenders == []


def _facade_writes(tree: ast.Module) -> set[str]:
    """Every name a file patches or monkeypatches on the facade, when it can be read."""
    scope = create_guard._Scope(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        form = create_guard._PATCH_FORMS.get(scope.module(node.func) or "")
        if form is not None:
            names.update(create_guard._patched_names(node, form, scope))
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("setattr", "delattr")):
            continue
        if not node.args:
            continue
        spelled = scope.text(node.args[0])
        if spelled is not None:
            owner, _, name = spelled.rpartition(".")
            if owner in create_guard._FACADE_PATHS:
                names.add(name)
        elif scope.module(node.args[0]) in create_guard._FACADE_PATHS and len(node.args) > 1:
            name_text = scope.text(node.args[1])
            if name_text is not None:
                names.add(name_text)
    names.discard(create_guard._DYNAMIC)
    return names


def _names_patched_on_the_facade() -> set[str]:
    patched: set[str] = set()
    for path in sorted((_REPO / "test").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if create_guard._worth_parsing(text):
            patched |= _facade_writes(ast.parse(text))
    return patched


def test_every_name_patched_on_the_facade_has_one_home() -> None:
    """Whatever the suite patches on ``kiro_crew.agent`` is bound by exactly one
    module, so the patch reaches every reader; and no shared import is patched. A
    module-valued name is refused by the facade instead (see below)."""
    patched = _names_patched_on_the_facade()
    assert {"KIRO_AGENTS_DIR", "_atomic_json_write", "_may_auto_approve"} <= patched, patched
    modules = {agent.__name__: agent, **_owners()}
    homes = {
        name: [label for label, module in modules.items() if name in vars(module)]
        for name in patched
        if not name.startswith("__") and name not in agent._MODULE_NAMES
    }
    assert {name: where for name, where in homes.items() if len(where) > 1} == {}
    assert sorted(patched & _SHARED_IMPORTS) == []


def test_the_write_scan_can_fail() -> None:
    """The scan sees each patch spelling, so an empty offender list means absence."""
    source = (
        "from kiro_crew import agent\n"
        "from unittest import mock\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(agent, "a_name", 1)\n'
        '    monkeypatch.setattr("kiro_crew.agent.b_name", 1)\n'
        '    monkeypatch.delattr(agent, "c_name")\n'
        '    mock.patch.object(agent, "d_name")\n'
        '    mock.patch("kiro_crew.agent.e_name")\n'
        '    monkeypatch.setattr(other, "f_name", 1)\n'
    )
    assert _facade_writes(ast.parse(source)) == {"a_name", "b_name", "c_name", "d_name", "e_name"}


def test_owners_read_the_core_only_for_names_the_core_defines() -> None:
    """``agent_mod.<name>`` must name a core binding. A re-exported name read through
    the facade would work, but it would hide which module the dependency is on."""
    core = vars(agent)
    reached: list[str] = []
    for stem, tree in _owner_trees().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "agent_mod"
                and node.attr not in core
            ):
                reached.append(f"{stem}: agent_mod.{node.attr}")
    assert reached == []


def test_owners_import_the_core_and_their_siblings_only_as_modules() -> None:
    offenders: list[str] = []
    for stem, tree in _owner_trees().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == agent.__name__ or module.startswith(f"{agent_materialization.__name__}."):
                offenders.append(f"{stem}: from {module} import ...")
            elif module == "kiro_crew":
                for alias in node.names:
                    if alias.name == "agent" and alias.asname != "agent_mod":
                        offenders.append(f"{stem}: agent imported as {alias.asname}")
            elif module == agent_materialization.__name__:
                for alias in node.names:
                    if not (_OWNER_DIR / f"{alias.name}.py").is_file():
                        offenders.append(f"{stem}: from {module} import {alias.name}")
    assert offenders == []


#: Imports an owner may run only inside a function: the boot path stays as light as
#: it was (the connections registry, the apps subsystem, the SDK driver and the CLI
#: probe load on use), and the packages agent materialization must not depend on
#: (the harnesses, providers, dashboard and session) stay out of it altogether.
_FUNCTION_LOCAL_ONLY = (
    "kiro_crew.acp",
    "kiro_crew.agent_sdk",
    "kiro_crew.apps",
    "kiro_crew.connections",
    "kiro_crew.dashboard",
    "kiro_crew.kiro_cli",
    "kiro_crew.providers",
    "kiro_crew.secrets",
    "kiro_crew.session",
)


def test_owners_keep_boot_path_imports_function_local() -> None:
    offenders: list[str] = []
    for stem, tree in _owner_trees().items():
        for node in create_guard._module_level(tree.body):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in _FUNCTION_LOCAL_ONLY):
                    offenders.append(f"{stem}: {name}")
    assert offenders == []


def test_the_seams_that_need_a_call_time_import_still_import_at_call_time() -> None:
    """Tests patch ``kiro_crew.kiro_cli.installed_kiro_cli_version`` and the SDK
    driver's permission helper at their source; that reaches the permission writer
    only while it imports them when it runs."""
    from kiro_crew.agent_materialization import auto_approve

    src = inspect.getsource(auto_approve._write_derived_permissions)
    assert "from kiro_crew.kiro_cli import" in src
    assert "installed_kiro_cli_version" in src
    assert "from kiro_crew.agent_sdk.drivers.acp import" in src


def test_no_other_src_module_imports_an_owner() -> None:
    """Callers reach the materialization through ``kiro_crew.agent``; the owners are
    its implementation, so moving a name between them stays invisible to src."""
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _AGENT_FILE or _OWNER_DIR in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        if agent_materialization.__name__ in text:
            offenders.append(path.relative_to(_SRC).as_posix())
    assert offenders == []


# --------------------------------------------------------------------------- #
# Load order.
# --------------------------------------------------------------------------- #


def test_the_core_imports_no_owner_before_its_own_names_are_bound() -> None:
    """Every owner imports the core, so the core may import them only once its own
    names are bound: the one import is a tail statement, and no function body or
    other statement imports an owner."""
    tree = _agent_tree()
    found: list[int] = []
    for index, node in enumerate(tree.body):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue  # never executes; it only names the re-exports for type checkers
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith(
                agent_materialization.__name__
            ):
                found.append(index)
    assert len(found) == 1
    (index,) = found
    later = tree.body[index + 1 :]
    assert all(not isinstance(n, (ast.FunctionDef, ast.ClassDef)) for n in later)


def test_the_owners_load_after_the_cores_names_and_before_the_forwarding() -> None:
    """The owners load once every name of the core's own is bound, and the forwarding
    class goes in only after they have, so neither runs half-way through the other."""
    *_, exports, hidden, dir_, load, module_names, install, declared = _agent_tree().body
    assert isinstance(exports, ast.AnnAssign) and ast.unparse(exports.target) == "_EXPORTS"
    assert isinstance(hidden, ast.If) and ast.unparse(hidden.test) == "not TYPE_CHECKING"
    assert isinstance(dir_, ast.FunctionDef) and dir_.name == "__dir__"
    assert isinstance(load, ast.ImportFrom) and load.module == agent_materialization.__name__
    assert sorted(f"{load.module}.{a.name}" for a in load.names) == sorted(agent._EXPORTS_BY_OWNER)
    assert isinstance(module_names, ast.Assign)
    assert ast.unparse(module_names.targets[0]) == "_MODULE_NAMES"
    assert ast.unparse(install) == "sys.modules[__name__].__class__ = _ReExportModule"
    assert isinstance(declared, ast.Assign) and ast.unparse(declared.targets[0]) == "__all__"


def _fresh(code: str) -> list[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC.parent) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=True, env=env, **UTF8_TEXT
    )
    return out.stdout.splitlines()


def test_importing_the_facade_loads_every_owner_and_nothing_heavier() -> None:
    """Every owner is bound when the facade finishes importing, so a by-name import in
    an owner takes its value then, never inside a later test's patch of its source;
    and the connections registry stays off the import path, as it was."""
    out = _fresh(
        "import sys\n"
        "import kiro_crew.agent as agent\n"
        "owners = sorted(agent._EXPORTS_BY_OWNER)\n"
        "print(','.join(m for m in owners if m not in sys.modules))\n"
        "print('kiro_crew.connections.registry' in sys.modules)\n"
    )
    assert out == ["", "False"]


def test_importing_an_owner_first_loads_the_whole_facade() -> None:
    out = _fresh(
        "import sys\n"
        "import kiro_crew.agent_materialization.worker_agent as worker\n"
        "import kiro_crew.agent as agent\n"
        "print(agent.rederive_worker_agent is worker.rederive_worker_agent)\n"
        "print(sorted(set(agent._EXPORTS_BY_OWNER) - set(sys.modules)))\n"
    )
    assert out == ["True", "[]"]


# --------------------------------------------------------------------------- #
# Type checking, bare globals and module rebinding.
# --------------------------------------------------------------------------- #


def test_the_type_checking_names_are_exactly_the_reexports() -> None:
    """mypy cannot see ``__getattr__``, so every re-export is named there once, from
    its owner, or ``agent.<name>`` in src would not type-check."""
    tree = _agent_tree()
    guarded = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
    ]
    assert len(guarded) == 1
    declared: dict[str, str] = {}
    for node in ast.walk(guarded[0]):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.asname is None, alias.name
                declared[alias.name] = node.module or ""
    assert declared == agent._EXPORTS


def _bare_loads(tree: ast.Module) -> list[tuple[int, str]]:
    """Each Load of a re-exported name as a bare global, outside import lines."""
    import_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return [
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in agent._EXPORTS
        and node.lineno not in import_lines
    ]


def test_the_core_reads_no_reexported_name_as_a_bare_global() -> None:
    """A function defined in the core resolves a bare global through the core's own
    namespace, which ``__getattr__`` never sees, so such a read would need the core to
    bind the name -- a second copy no patch of the owner reaches. Every line counts,
    the ``TYPE_CHECKING`` block's included."""
    assert _bare_loads(_agent_tree()) == []


def test_the_bare_global_scan_can_fail() -> None:
    sample = "rederive_worker_agent"
    assert sample in agent._EXPORTS
    tree = ast.parse(f"from x import y\n\ndef f():\n    return {sample}\n")
    assert _bare_loads(tree) == [(4, sample)]
    assert _bare_loads(ast.parse(f"from x import (\n    {sample},\n)\n")) == []


def test_the_module_getattr_is_hidden_from_type_checkers() -> None:
    tree = _agent_tree()
    defined = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"
    ]
    hidden = [
        n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test) == "not TYPE_CHECKING"
    ]
    assert len(defined) == 1 and len(hidden) == 1
    assert defined[0] in hidden[0].body
    assert callable(vars(agent).get("__getattr__"))  # still the resolver at run time


def test_a_loaded_owner_is_read_without_a_fresh_import(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    """Once loaded, an owner answers from ``sys.modules``: reading, writing and
    undoing a forwarded name never re-executes an import."""
    from kiro_crew.agent_materialization import service_agents

    owner_guard(service_agents, "_install_lite_agent_fallback")
    loaded = set(sys.modules)
    with mock.patch.object(agent, "_install_lite_agent_fallback") as stub:
        assert agent._install_lite_agent_fallback is stub
    assert (
        agent._install_lite_agent_fallback is vars(service_agents)["_install_lite_agent_fallback"]
    )
    assert set(sys.modules) == loaded


@pytest.mark.parametrize("name", ["json", "os", "agent_state", "kiro_hooks", "worker_agent"])
def test_rebinding_a_shared_module_through_the_facade_is_refused(name: str) -> None:
    """Each module holds its own binding of a module it imports, so a replacement
    written here would reach one reader; the facade refuses it."""
    before = getattr(agent, name)
    with pytest.raises(AttributeError, match="shared module"):
        setattr(agent, name, object())
    with pytest.raises(AttributeError, match="shared module"):
        delattr(agent, name)
    assert getattr(agent, name) is before
    setattr(agent, name, before)  # re-binding the same object is a no-op, as undo does
    assert getattr(agent, name) is before


def test_the_refused_names_are_the_ones_bound_to_modules() -> None:
    bound_to_modules = {
        name
        for name in set(vars(agent)) | set(agent._EXPORTS)
        if not name.startswith("__") and isinstance(getattr(agent, name), ModuleType)
    }
    assert agent._MODULE_NAMES == bound_to_modules
    owners = {name.rpartition(".")[2] for name in agent._EXPORTS_BY_OWNER}
    assert owners | {"json", "os", "agent_state"} <= agent._MODULE_NAMES


def test_the_refusal_goes_by_name_so_a_module_stub_is_still_undone(
    owner_guard: Callable[[ModuleType, str], object],
) -> None:
    """A forwarded function patched with a module object is an ordinary patch: it is
    written and undone. A module-valued name stays refused meanwhile, both ways."""
    from kiro_crew.agent_materialization import conductor_agents

    original = owner_guard(conductor_agents, "_install_pipeline_conductor_agent")
    stub = ModuleType("installer_stub")
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(agent, "_install_pipeline_conductor_agent", stub)
        assert conductor_agents._install_pipeline_conductor_agent is stub
        with pytest.raises(AttributeError, match="shared module"):
            patched.setattr(agent, "json", ModuleType("json_stub"))
        with pytest.raises(AttributeError, match="shared module"):
            patched.delattr(agent, "json")
    assert conductor_agents._install_pipeline_conductor_agent is original
    assert agent.json is conductor_agents.agent_mod.json

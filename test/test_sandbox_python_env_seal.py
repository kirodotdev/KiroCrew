"""The gateway's own Python environment is read-only to every sandboxed child but a
provisioning one.

A packaged install runs from an environment (a venv, the desktop bundle's embedded
interpreter, or a ``site-packages`` directory) whose contents the gateway loads on its
next start. The planner seals that environment beside the package directory for every
spawn, except the few gateway-run steps that install Python packages into it for the
gateway: the memory extra's ``ensurepip`` and ``faiss-cpu`` install and an app's Python
build step. Those plan with the environment writable and the package directory sealed.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib.util
import ntpath
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew
from kiro_crew import sandbox, sandbox_plan, sandbox_python_env, sandbox_seatbelt, sandbox_tiers
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    PlanHost,
    SandboxRequest,
    plan_confinement,
)
from kiro_crew.sandbox_python_env import (
    provisioning_spawn_requested,
    python_env_targets_for,
    run_as_provisioning,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

HOME = "/srv/u"
ENV = "/opt/kirocrew"
SITE = f"{ENV}/lib/python3.12/site-packages"
INSTALL = f"{SITE}/kiro_crew"
_BACKENDS = [BACKEND_NAMESPACE, BACKEND_SEATBELT]


def _host(**overrides: object) -> PlanHost:
    fields: dict[str, object] = {
        "home": HOME,
        "cwd": "/work",
        "tier_dirs": (".aws",),
        "crew_readonly_targets": (".kiro/crew/config.json",),
        "install_root_targets": (INSTALL,),
        "python_env_targets": (ENV,),
        "uid": 1000,
        "gid": 1000,
    }
    fields.update(overrides)
    return PlanHost(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Which environment a packaged install runs from.
# --------------------------------------------------------------------------- #


def _env(package_dir: str, prefix: str, base_prefix: str, *, bundled: bool = False) -> list[str]:
    return python_env_targets_for(
        package_dir, prefix=prefix, base_prefix=base_prefix, bundled=bundled, home=HOME
    )


@_POSIX_ONLY
def test_a_venv_install_seals_the_whole_venv() -> None:
    assert _env(INSTALL, ENV, "/usr") == [ENV]
    assert _env(
        f"{HOME}/.kiro/crew-venv/lib/python3.12/site-packages/kiro_crew/",
        f"{HOME}/.kiro/crew-venv",
        "/usr",
    ) == [f"{HOME}/.kiro/crew-venv"]


@_POSIX_ONLY
def test_the_bundled_interpreter_seals_its_whole_runtime() -> None:
    runtime = "/Apps/KiroCrew/resources/backend-dist/kirocrew-backend"
    package = f"{runtime}/lib/python3.12/site-packages/kiro_crew"
    assert _env(package, runtime, runtime, bundled=True) == [runtime]


@_POSIX_ONLY
@pytest.mark.parametrize(
    "package_dir",
    [
        "/usr/lib/python3/dist-packages/kiro_crew",
        f"{HOME}/.local/lib/python3.12/site-packages/kiro_crew",
    ],
)
def test_a_shared_interpreter_seals_only_the_site_packages(package_dir: str) -> None:
    # A system or --user install: the interpreter beside it serves other programs.
    assert _env(package_dir, "/usr", "/usr") == [os.path.dirname(package_dir)]


@_POSIX_ONLY
def test_a_venv_that_does_not_hold_the_package_seals_only_its_site_packages() -> None:
    assert _env(INSTALL, "/elsewhere/venv", "/usr") == [SITE]


@_POSIX_ONLY
@pytest.mark.parametrize("prefix", [HOME, "/srv", "/"])
def test_a_prefix_holding_the_home_directory_is_never_sealed_whole(prefix: str) -> None:
    package = f"{prefix.rstrip('/')}/lib/python3.12/site-packages/kiro_crew"
    assert _env(package, prefix, "/usr") == [os.path.dirname(package)]


@_POSIX_ONLY
@pytest.mark.parametrize(
    "package_dir",
    [f"{HOME}/KiroCrew/src/kiro_crew", f"{HOME}/KiroCrew/.worktrees/feature/src/kiro_crew"],
)
def test_an_editable_or_source_checkout_seals_nothing(package_dir: str) -> None:
    assert _env(package_dir, f"{HOME}/KiroCrew/.venv", "/usr") == []


def test_windows_layouts_are_classified() -> None:
    venv = r"C:\Users\u\kc-venv"
    package = venv + r"\Lib\site-packages\kiro_crew"
    with_paths = sandbox_python_env._python_env_targets_with
    assert with_paths(package, venv, r"C:\Python312", False, r"C:\Users\u", ntpath) == [venv]
    assert with_paths(package, r"D:\other", r"C:\Python312", False, r"C:\Users\u", ntpath) == [
        venv + r"\Lib\site-packages"
    ]


def test_this_source_checkout_seals_no_environment() -> None:
    # The suite runs from a source checkout, which is exactly the developer case.
    if sandbox.install_root_targets_for(sandbox._gateway_package_dir()):
        pytest.skip("the suite is running from a packaged install")
    assert sandbox_python_env.resolved_python_env_targets() == []


@_POSIX_ONLY
def test_the_resolved_targets_carry_every_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "crew-venv-2.0"
    (real / "lib" / "python3.12" / "site-packages" / "kiro_crew").mkdir(parents=True)
    link = tmp_path / "crew-venv-current"
    link.symlink_to(real, target_is_directory=True)
    package = str(link / "lib" / "python3.12" / "site-packages" / "kiro_crew")
    monkeypatch.setattr(sandbox_python_env, "_managed_stable_link_spellings", lambda: [])
    sandbox_python_env._python_env_targets_cached.cache_clear()
    try:
        spellings = sandbox_python_env._python_env_targets_cached(
            package, str(link), "/usr", sys.executable, HOME
        )
    finally:
        sandbox_python_env._python_env_targets_cached.cache_clear()
    assert spellings == (str(link), str(real.resolve()))


@_POSIX_ONLY
def test_the_managed_stable_link_is_sealed_when_the_process_runs_from_that_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.platform import wheel_engine

    real = tmp_path / "crew-venv-2.0"
    real.mkdir()
    link = tmp_path / "crew-venv-current"
    link.symlink_to(real, target_is_directory=True)
    layout = wheel_engine.ManagedVenvLayout(legacy=tmp_path / "crew-venv", stable_link=link)
    monkeypatch.setattr(wheel_engine, "managed_venv_layout", lambda: layout)
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda _layout: True)
    assert sandbox_python_env._managed_stable_link_spellings() == [str(link), str(real.resolve())]
    monkeypatch.setattr(wheel_engine, "running_from_managed_venv", lambda _layout: False)
    assert sandbox_python_env._managed_stable_link_spellings() == []

    def _boom() -> object:
        raise OSError("no data home")

    monkeypatch.setattr(wheel_engine, "managed_venv_layout", _boom)
    assert sandbox_python_env._managed_stable_link_spellings() == []


def test_the_resolver_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise RuntimeError("no package")

    monkeypatch.setattr(sandbox_tiers, "_gateway_package_dir", _boom)
    assert sandbox_python_env.resolved_python_env_targets() == []


# --------------------------------------------------------------------------- #
# What the plan seals.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("tier", ["strict", "cc", "standard"])
def test_every_tier_seals_the_environment(backend: str, tier: str) -> None:
    plan = plan_confinement(SandboxRequest(tier=tier, backend=backend), _host())
    assert ENV in plan.readonly
    assert INSTALL in plan.readonly


@_POSIX_ONLY
@pytest.mark.parametrize("backend", _BACKENDS)
@pytest.mark.parametrize("tier", ["strict", "cc", "standard"])
def test_a_provisioning_spawn_keeps_the_environment_writable(backend: str, tier: str) -> None:
    host = _host(python_env_ancestor_guards=("/opt",))
    plan = plan_confinement(SandboxRequest(tier=tier, backend=backend, provisioning=True), host)
    assert ENV not in plan.readonly
    # The package directory inside it stays sealed for every spawn.
    assert INSTALL in plan.readonly
    # Exactly the plan the same host gets without an environment to seal.
    bare = dataclasses.replace(host, python_env_targets=(), python_env_ancestor_guards=())
    assert plan == plan_confinement(SandboxRequest(tier=tier, backend=backend), bare)


@_POSIX_ONLY
def test_the_namespace_launcher_receives_it_as_a_readonly_dir() -> None:
    plan = plan_confinement(SandboxRequest(tier="standard"), _host())
    assert ENV in sandbox_plan.namespace_payload(plan)["readonly_dirs"]


@_POSIX_ONLY
def test_the_seatbelt_profile_denies_every_write_below_it() -> None:
    host = _host(python_env_ancestor_guards=("/opt",))
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), host)
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    assert f'(deny file-write* (subpath "{ENV}"))' in profile
    assert f'(deny file-link (subpath "{ENV}"))' in profile
    assert '(deny file-write* (literal "/opt"))' in profile


@_POSIX_ONLY
def test_the_seatbelt_profile_of_a_provisioning_spawn_leaves_it_writable() -> None:
    host = _host(python_env_ancestor_guards=("/opt",))
    request = SandboxRequest(tier="standard", backend=BACKEND_SEATBELT, provisioning=True)
    profile = sandbox_seatbelt.render_seatbelt_profile(plan_confinement(request, host))
    assert f'(deny file-write* (subpath "{ENV}"))' not in profile
    assert '(deny file-write* (literal "/opt"))' not in profile
    assert f'(deny file-write* (subpath "{INSTALL}"))' in profile


@_POSIX_ONLY
@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_visibility_lift_does_not_unseal_it(backend: str) -> None:
    request = SandboxRequest(tier="standard", backend=backend, extra_visible_dirs=(ENV,))
    assert ENV in plan_confinement(request, _host()).readonly


@_POSIX_ONLY
@pytest.mark.parametrize("backend", _BACKENDS)
def test_a_writable_carveout_inside_it_is_refused(backend: str) -> None:
    inside = f"{SITE}/numpy"
    probe = sandbox_plan.CarveoutProbe(raw=inside, lexical=inside, canonical=inside, is_dir=True)
    request = SandboxRequest(tier="standard", backend=backend, extra_writable_dirs=(inside,))
    plan = plan_confinement(request, _host(carveout_probes=(probe,)))
    assert inside not in plan.writable


@_POSIX_ONLY
def test_an_editable_host_plans_exactly_as_before() -> None:
    sealed = plan_confinement(SandboxRequest(tier="standard"), _host())
    editable = plan_confinement(SandboxRequest(tier="standard"), _host(python_env_targets=()))
    assert tuple(p for p in sealed.readonly if p != ENV) == editable.readonly


@_POSIX_ONLY
@pytest.mark.parametrize(
    ("backend", "guards"),
    [(BACKEND_NAMESPACE, ()), (BACKEND_SEATBELT, ("/opt",))],
)
def test_the_live_adapter_reads_the_resolver(
    monkeypatch: pytest.MonkeyPatch, backend: str, guards: tuple[str, ...]
) -> None:
    # The namespace bind is on the directory itself and moves with a renamed parent;
    # Seatbelt matches paths and needs a guard on every ancestor.
    monkeypatch.setattr(sandbox, "_resolved_python_env_targets", lambda: [ENV])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=backend))
    assert host.python_env_targets == (ENV,)
    assert host.python_env_ancestor_guards == guards


# --------------------------------------------------------------------------- #
# Which spawns are provisioning ones.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
def test_the_spawn_plan_follows_the_provisioning_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_live_plan_host", lambda _request: _host())
    plain = sandbox._spawn_plan(BACKEND_NAMESPACE, "standard")
    provisioning = run_as_provisioning(lambda: sandbox._spawn_plan(BACKEND_NAMESPACE, "standard"))
    assert ENV in plain.readonly
    assert ENV not in provisioning.readonly
    assert INSTALL in provisioning.readonly


def test_the_provisioning_scope_ends_with_its_preparation() -> None:
    assert run_as_provisioning(provisioning_spawn_requested) is True
    assert provisioning_spawn_requested() is False

    def _fails() -> None:
        raise ValueError("prepare failed")

    with pytest.raises(ValueError):
        run_as_provisioning(_fails)
    assert provisioning_spawn_requested() is False


@pytest.mark.parametrize("provisioning", [True, False])
def test_wrap_argv_async_prepares_inside_the_scope_it_is_asked_for(provisioning: bool) -> None:
    seen: list[bool] = []

    def _prepare(argv: list[str], **_options: object) -> tuple[list[str], None]:
        seen.append(provisioning_spawn_requested())
        return list(argv), None

    wrapped, cleanup = asyncio.run(
        sandbox.wrap_argv_async(
            ["true"], mode="standard", provisioning=provisioning, _prepare=_prepare
        )
    )
    assert (wrapped, cleanup, seen) == (["true"], None, [provisioning])
    assert provisioning_spawn_requested() is False


#: Every source file allowed to ask for a provisioning spawn, with how many such calls
#: it makes. A new caller is a new writer of the gateway's own environment.
_PROVISIONING_CALLERS = {
    "dashboard/handlers/memory.py": 2,
    "apps/registry.py": 1,
}


def _provisioning_calls() -> dict[str, list[ast.Call]]:
    root = Path(kiro_crew.__file__).resolve().parent
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "provisioning" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            if any(kw.arg == "provisioning" for kw in node.keywords):
                found.setdefault(rel, []).append(node)
            elif rel not in ("sandbox_argv.py", "sandbox_spawn.py", "sandbox_python_env.py") and (
                (isinstance(node.func, ast.Name) and node.func.id == "run_as_provisioning")
                or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "run_as_provisioning"
                )
            ):
                found.setdefault(rel, []).append(node)
    return found


def test_only_the_known_steps_ask_for_a_provisioning_spawn() -> None:
    found = _provisioning_calls()
    # The planner's own request construction is the one in-package use besides them.
    found.pop("sandbox_argv.py", None)
    assert {rel: len(calls) for rel, calls in found.items()} == _PROVISIONING_CALLERS
    memory_argvs = [ast.unparse(call.args[0]) for call in found["dashboard/handlers/memory.py"]]
    assert any("ensurepip" in argv for argv in memory_argvs)
    assert any("faiss-cpu" in argv for argv in memory_argvs)
    (build,) = found["apps/registry.py"]
    (flag,) = [kw.value for kw in build.keywords if kw.arg == "provisioning"]
    assert ast.unparse(flag) == "cmd is python_install"


# --------------------------------------------------------------------------- #
# The provisioning steps themselves.
# --------------------------------------------------------------------------- #


class _Spawned(Exception):
    pass


def _record_preparations(monkeypatch: pytest.MonkeyPatch, module: object) -> list[tuple]:
    seen: list[tuple] = []

    def _wrap(argv: list[str], mode: str = "standard", **_kw: object) -> tuple[list[str], None]:
        seen.append((list(argv), provisioning_spawn_requested()))
        return list(argv), None

    monkeypatch.setattr(module, "wrap_argv", _wrap)
    monkeypatch.setattr(module, "cgroup_scope_argv", lambda argv: list(argv))
    return seen


def test_the_pip_bootstrap_is_a_provisioning_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory

    monkeypatch.setitem(sys.modules, "pip", None)
    seen = _record_preparations(monkeypatch, memory)

    async def _spawn(*_argv: object, **_kwargs: object) -> None:
        raise _Spawned

    monkeypatch.setattr(memory, "create_subprocess_limited", _spawn)
    with pytest.raises(_Spawned):
        asyncio.run(memory._ensure_pip_available())
    assert len(seen) == 1
    argv, provisioning = seen[0]
    assert "ensurepip" in argv and provisioning is True


def test_the_faiss_install_is_a_provisioning_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory

    monkeypatch.setitem(sys.modules, "pip", MagicMock())
    monkeypatch.setitem(sys.modules, "faiss", None)
    seen = _record_preparations(monkeypatch, memory)
    monkeypatch.setattr(memory, "model_file_present", lambda *a, **kw: True)
    monkeypatch.setattr(memory, "get_shared_embedder", lambda *a, **kw: MagicMock(dim=1024))

    async def _spawn(*_argv: object, **_kwargs: object) -> None:
        raise _Spawned

    monkeypatch.setattr(memory, "create_subprocess_limited", _spawn)
    request = MagicMock()
    request.app = {"state": MagicMock(consolidator=None, owner_id="")}
    claims = {"app": "", "user": "local-app"}
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    request.get.side_effect = lambda key, *default: claims.get(key, *default)
    memory._embedding_setup_status = {"step": "idle", "error": ""}
    try:
        with pytest.raises(_Spawned):
            asyncio.run(memory.api_memory_enable_embeddings(request))
    finally:
        memory._embedding_setup_status = {"step": "idle", "error": ""}
    assert len(seen) == 1
    argv, provisioning = seen[0]
    assert "faiss-cpu" in argv and provisioning is True


class _EmptyStdout:
    def __aiter__(self) -> "_EmptyStdout":
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


class _Done:
    returncode = 0
    stdout = _EmptyStdout()

    async def wait(self) -> int:
        return 0


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> list[tuple]:
    from kiro_crew.apps import registry
    from kiro_crew.apps.manifest import AppManifest

    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    seen = _record_preparations(monkeypatch, registry)

    async def _spawn(*_argv: object, **_kwargs: object) -> _Done:
        return _Done()

    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(registry.platform_compat, "is_bundled_interpreter", lambda: False)
    monkeypatch.setattr(registry.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = asyncio.run(
        registry._run_app_build(
            tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False
        )
    )
    assert result == {"ok": True}
    return seen


@pytest.mark.parametrize(
    ("files", "verb"),
    [
        ({"pyproject.toml": "[project]\nname='x'\nversion='0'\n"}, "."),
        ({"requirements.txt": "six\n"}, "requirements.txt"),
    ],
)
def test_an_app_python_build_is_a_provisioning_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str], verb: str
) -> None:
    if importlib.util.find_spec("pip") is None:
        pytest.skip("the build plans no pip step without pip")
    seen = _build(tmp_path, monkeypatch, files)
    assert len(seen) == 1
    argv, provisioning = seen[0]
    assert argv[-1] == verb and provisioning is True


def test_an_app_javascript_build_is_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"package.json": '{"scripts": {"build": "tsc"}}'}
    seen = _build(tmp_path, monkeypatch, files)
    assert [provisioning for _argv, provisioning in seen] == [False, False]


# --------------------------------------------------------------------------- #
# Delegated spawns: the workspace must not expose it.
# --------------------------------------------------------------------------- #


@pytest.fixture
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env = tmp_path / "project" / "kc-venv"
    install = env / "Lib" / "site-packages" / "kiro_crew"
    install.mkdir(parents=True)
    (env / "Scripts").mkdir()
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [str(install)])
    monkeypatch.setattr(sandbox, "_resolved_python_env_targets", lambda: [str(env)])
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "crew")
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    return env


def test_a_delegated_workspace_inside_the_environment_is_refused(env_dir: Path) -> None:
    for workspace in (env_dir, env_dir / "Scripts"):
        reason = sandbox.delegated_workspace_exposes_sealed_target(workspace)
        assert reason is not None, workspace
        assert "Python environment" in reason
        assert f"Choose a workspace outside '{env_dir}'." in reason


def test_a_delegated_workspace_inside_the_package_names_the_install_directory(
    env_dir: Path,
) -> None:
    reason = sandbox.delegated_workspace_exposes_sealed_target(
        env_dir / "Lib" / "site-packages" / "kiro_crew"
    )
    assert reason is not None and "install directory" in reason


def test_a_delegated_project_holding_the_environment_is_allowed(
    env_dir: Path, tmp_path: Path
) -> None:
    # A project holding its own venv is a common layout; the environment refuses only
    # a workspace at or inside it.
    assert sandbox.delegated_workspace_exposes_sealed_target(env_dir.parent) is None
    assert sandbox.delegated_workspace_exposes_sealed_target(tmp_path / "workspace") is None

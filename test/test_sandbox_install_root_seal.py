"""The gateway's packaged install tree is sealed read-only for every sandboxed child.

A packaged install (a wheel, the desktop bundle's embedded interpreter) imports
``kiro_crew`` from below ``site-packages``; every byte under that package directory --
the sources and the served ``static/dist`` bundle with its ``.br``/``.gz`` siblings -- is
what the gateway executes on its next start. The seal rides the same read-only floor as
the crew-home ceilings and the kiro agents tree, so it holds for every write primitive a
sandboxed child has, not just one tool.

Two things must stay writable and are pinned here: an editable or from-source checkout
(a developer's working copy), and the gateway's own updater, which runs in the gateway
process and is never wrapped.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox, sandbox_plan, sandbox_seatbelt
from kiro_crew.sandbox_plan import (
    BACKEND_NAMESPACE,
    BACKEND_SEATBELT,
    PlanHost,
    SandboxRequest,
    plan_confinement,
)

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths only")

HOME = "/srv/u"
INSTALL = "/opt/kirocrew/lib/python3.12/site-packages/kiro_crew"


def _host(**overrides: object) -> PlanHost:
    fields: dict[str, object] = {
        "home": HOME,
        "cwd": "/work",
        "tier_dirs": (".aws",),
        "crew_readonly_targets": (".kiro/crew/config.json",),
        "install_root_targets": (INSTALL,),
        "uid": 1000,
        "gid": 1000,
    }
    fields.update(overrides)
    return PlanHost(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Which installs are packaged.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.parametrize(
    "package_dir",
    [
        INSTALL,
        "/usr/lib/python3/dist-packages/kiro_crew",
        # The desktop bundle's layout: an embedded interpreter's own site-packages.
        "/Apps/KiroCrew/resources/backend-dist/kirocrew-backend/Lib/site-packages/kiro_crew",
        f"{HOME}/.venv/lib/python3.12/site-packages/kiro_crew/",
    ],
)
def test_a_package_below_site_packages_is_sealed(package_dir: str) -> None:
    assert sandbox.install_root_targets_for(package_dir) == [os.path.normpath(package_dir)]


@_POSIX_ONLY
@pytest.mark.parametrize(
    "package_dir",
    [
        # ``pip install -e`` / install.sh: the package is imported from the checkout.
        f"{HOME}/KiroCrew/src/kiro_crew",
        # A Dev Fleet worktree of the same checkout.
        f"{HOME}/KiroCrew/.worktrees/feature/src/kiro_crew",
        # A directory merely NAMED like one is not the package's parent.
        f"{HOME}/site-packages-notes/kiro_crew",
        # A source checkout that happens to live below a site-packages directory.
        "/x/site-packages/proj/src/kiro_crew",
        "/",
    ],
)
def test_an_editable_or_source_checkout_stays_writable(package_dir: str) -> None:
    assert sandbox.install_root_targets_for(package_dir) == []


def test_this_source_checkout_is_not_sealed() -> None:
    # The suite runs from a source checkout, which is exactly the developer case.
    if sandbox.install_root_targets_for(sandbox._gateway_package_dir()):
        pytest.skip("running from a packaged install")
    assert sandbox._resolved_install_root_targets() == []


@pytest.mark.parametrize(
    "package_dir, sealed",
    [
        (
            r"C:\Users\u\AppData\Local\Programs\KiroCrew\resources\backend-dist"
            r"\kirocrew-backend\Lib\site-packages\kiro_crew",
            True,
        ),
        (r"C:\Users\u\src\KiroCrew\src\kiro_crew", False),
    ],
)
def test_windows_layouts_are_classified(package_dir: str, sealed: bool) -> None:
    import ntpath

    assert bool(sandbox._install_root_targets_with(package_dir, ntpath)) is sealed


@_POSIX_ONLY
def test_the_resolved_targets_carry_both_spellings(tmp_path: Path) -> None:
    real = tmp_path / "real" / "site-packages" / "kiro_crew"
    real.mkdir(parents=True)
    link_parent = tmp_path / "link" / "site-packages"
    link_parent.parent.mkdir()
    link_parent.symlink_to(real.parent, target_is_directory=True)
    sandbox._install_root_targets_cached.cache_clear()
    try:
        targets = sandbox._install_root_targets_cached(str(link_parent / "kiro_crew"))
    finally:
        sandbox._install_root_targets_cached.cache_clear()
    assert targets == (str(link_parent / "kiro_crew"), os.path.realpath(real))


def test_the_resolver_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise RuntimeError("no package")

    monkeypatch.setattr(sandbox, "_gateway_package_dir", _boom)
    assert sandbox._resolved_install_root_targets() == []


# --------------------------------------------------------------------------- #
# The plan seals it on both backends.
# --------------------------------------------------------------------------- #


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
@pytest.mark.parametrize("tier", ["strict", "cc", "standard"])
def test_every_tier_seals_the_install_root(backend: str, tier: str) -> None:
    plan = plan_confinement(SandboxRequest(tier=tier, backend=backend), _host())
    assert INSTALL in plan.readonly


@_POSIX_ONLY
def test_the_namespace_launcher_receives_it_as_a_readonly_dir() -> None:
    plan = plan_confinement(SandboxRequest(tier="standard"), _host())
    assert INSTALL in sandbox_plan.namespace_payload(plan)["readonly_dirs"]


@_POSIX_ONLY
def test_the_seatbelt_profile_denies_every_write_below_it() -> None:
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), _host())
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    assert f'(deny file-write* (subpath "{INSTALL}"))' in profile
    assert f'(deny file-link (subpath "{INSTALL}"))' in profile


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_visibility_lift_does_not_unseal_it(backend: str) -> None:
    request = SandboxRequest(tier="standard", backend=backend, extra_visible_dirs=(INSTALL,))
    assert INSTALL in plan_confinement(request, _host()).readonly


@_POSIX_ONLY
@pytest.mark.parametrize("backend", [BACKEND_NAMESPACE, BACKEND_SEATBELT])
def test_a_writable_carveout_inside_it_is_refused(backend: str) -> None:
    inside = f"{INSTALL}/static/dist"
    probe = sandbox_plan.CarveoutProbe(raw=inside, lexical=inside, canonical=inside, is_dir=True)
    request = SandboxRequest(tier="standard", backend=backend, extra_writable_dirs=(inside,))
    plan = plan_confinement(request, _host(carveout_probes=(probe,)))
    assert inside not in plan.writable


@_POSIX_ONLY
def test_an_editable_host_plans_exactly_as_before() -> None:
    sealed = plan_confinement(SandboxRequest(tier="standard"), _host())
    editable = plan_confinement(SandboxRequest(tier="standard"), _host(install_root_targets=()))
    assert tuple(p for p in sealed.readonly if p != INSTALL) == editable.readonly


@_POSIX_ONLY
def test_the_live_adapter_reads_the_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [INSTALL])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert host.install_root_targets == (INSTALL,)


_INSTALL_ANCESTORS = (
    "/opt/kirocrew/lib/python3.12/site-packages",
    "/opt/kirocrew/lib/python3.12",
    "/opt/kirocrew/lib",
    "/opt/kirocrew",
    "/opt",
)


@_POSIX_ONLY
def test_the_seatbelt_adapter_guards_every_ancestor_of_the_install_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [INSTALL])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT))
    assert host.install_root_ancestor_guards == _INSTALL_ANCESTORS


@_POSIX_ONLY
def test_the_namespace_adapter_needs_no_ancestor_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    # The read-only bind is on the directory itself and moves with a renamed parent.
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [INSTALL])
    host = sandbox._live_plan_host(SandboxRequest(tier="standard"))
    assert host.install_root_ancestor_guards == ()


@_POSIX_ONLY
def test_the_seatbelt_profile_denies_renaming_a_parent_of_the_install_root() -> None:
    host = _host(install_root_ancestor_guards=_INSTALL_ANCESTORS)
    plan = plan_confinement(SandboxRequest(tier="standard", backend=BACKEND_SEATBELT), host)
    profile = sandbox_seatbelt.render_seatbelt_profile(plan)
    for parent in _INSTALL_ANCESTORS:
        assert f'(deny file-write* (literal "{parent}"))' in profile
    # A literal guard covers the directory entry only, so the rest of site-packages
    # stays writable for provisioning steps.
    assert f'(deny file-write* (subpath "{_INSTALL_ANCESTORS[0]}"))' not in profile


# --------------------------------------------------------------------------- #
# Delegated spawns: the workspace must not expose it.
# --------------------------------------------------------------------------- #


@pytest.fixture
def install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install = tmp_path / "Lib" / "site-packages" / "kiro_crew"
    install.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "_resolved_install_root_targets", lambda: [str(install)])
    monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
    monkeypatch.setattr(sandbox, "config_dir", lambda: tmp_path / "crew")
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    return install


def test_a_delegated_workspace_inside_the_install_root_is_refused(install_dir: Path) -> None:
    for workspace in (install_dir, install_dir / "static"):
        reason = sandbox.delegated_workspace_exposes_sealed_target(workspace)
        assert reason is not None, workspace
        assert "install directory" in reason
        # The workspace sits at or inside the install directory, so the advice is to
        # move it outside, not to pick one that does not contain it.
        assert f"Choose a workspace outside '{install_dir}'." in reason
        assert "does not contain" not in reason


def test_a_delegated_sibling_workspace_is_allowed(install_dir: Path, tmp_path: Path) -> None:
    assert sandbox.delegated_workspace_exposes_sealed_target(tmp_path / "workspace") is None


def test_a_delegated_project_holding_its_own_venv_is_allowed(install_dir: Path) -> None:
    # A project that installed the package into its own venv is a common layout; the
    # install directory refuses only a workspace at or inside it.
    for workspace in (install_dir.parent, install_dir.parent.parent):
        assert sandbox.delegated_workspace_exposes_sealed_target(workspace) is None


@_POSIX_ONLY
def test_a_delegated_workspace_reaching_the_install_root_through_a_link_is_refused(
    install_dir: Path, tmp_path: Path
) -> None:
    link = tmp_path / "innocent"
    link.symlink_to(install_dir, target_is_directory=True)
    assert sandbox.delegated_workspace_exposes_sealed_target(link) is not None


@_POSIX_ONLY
def test_a_delegated_workspace_whose_spelling_resolves_elsewhere_is_refused_by_identity(
    install_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A case-insensitive volume reaches the install directory under a spelling that
    # neither ``normcase`` nor ``realpath`` rewrites; a link with ``realpath`` held
    # lexical stands in for it here.
    link = tmp_path / "KIRO_CREW"
    link.symlink_to(install_dir, target_is_directory=True)
    monkeypatch.setattr(os.path, "realpath", lambda p, **_: os.path.abspath(p))
    for workspace in (link, link / "static", link / "not-yet-created" / "deeper"):
        reason = sandbox.delegated_workspace_exposes_sealed_target(workspace)
        assert reason is not None, workspace
        assert "install directory" in reason
    assert sandbox.delegated_workspace_exposes_sealed_target(install_dir.parent) is None


# --------------------------------------------------------------------------- #
# The updater keeps working: it runs in the gateway process, never wrapped.
# --------------------------------------------------------------------------- #

_SANDBOX_WRAPPERS = frozenset(
    {"wrap_argv", "wrap_argv_async", "sandboxed_spawn_argv", "sandboxed_spawn_argv_async"}
)

#: The modules on the update path that write the install tree.
_UPDATER_MODULES = (
    "dashboard/handlers/updates.py",
    "platform/wheel_engine.py",
    "platform/update_provider.py",
    "platform/update_layout.py",
    "platform/wheel_apply.py",
    "dep_sync.py",
    "frontend.py",
)


@pytest.mark.parametrize("relative", _UPDATER_MODULES)
def test_the_updater_never_routes_its_writes_through_the_sandbox(relative: str) -> None:
    """A tripwire on names: the update path runs in the gateway process, which no seal
    reaches, so none of its modules may name a sandbox wrapper."""
    source = Path(sandbox.__file__).resolve().parent / relative
    tree = ast.parse(source.read_text(encoding="utf-8"))
    named = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not (named | imported) & _SANDBOX_WRAPPERS, relative


def test_the_sandbox_import_does_not_touch_the_install_root(tmp_path: Path) -> None:
    """Planning the seal reads paths and nothing else: resolving the targets of a real
    packaged layout leaves it writable for the planning process, which is the process
    the updater runs in."""
    install = tmp_path / "site-packages" / "kiro_crew"
    (install / "static" / "dist").mkdir(parents=True)
    sandbox._install_root_targets_cached.cache_clear()
    try:
        targets = sandbox._install_root_targets_cached(str(install))
    finally:
        sandbox._install_root_targets_cached.cache_clear()
    plan = plan_confinement(SandboxRequest(tier="strict"), _host(install_root_targets=targets))
    assert str(install) in plan.readonly
    replacement = install / "static" / "dist" / "index.html"
    staged = replacement.with_suffix(".new")
    staged.write_text("updated", encoding="utf-8")
    os.replace(staged, replacement)
    assert replacement.read_text(encoding="utf-8") == "updated"

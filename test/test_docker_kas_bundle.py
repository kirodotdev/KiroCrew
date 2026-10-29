"""The Docker image's baked KAS engine: build step, hand-off, docs, smoke.

``BUNDLE_KAS=1`` makes ``docker/Dockerfile`` stage the engine this release pins
with the backend's own installer, the way ``packaging/build-desktop.sh`` stages
the desktop app's copy, and an image ``ENV`` names the staged root as
``KIROCREW_BUNDLED_KAS_DIR`` so every process in the container (the gateway and
``docker exec`` commands alike) finds it. These tests read the recipe, the docs
and the smoke workflow as text (no Docker daemon, no network), so they are
offline and deterministic. The image build itself runs in
``.github/workflows/docker-smoke.yml``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from kiro_crew.config.sections import AgentConfig
from kiro_crew.constants import BUNDLED_KAS_DIR_ENV

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DOCKER_GUIDE = ROOT / "docs" / "guides" / "docker.md"
SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "docker-smoke.yml"
KAS_ROOT = "/usr/local/share/kirocrew/kas"


def _instructions() -> list[str]:
    """Logical Dockerfile instructions, read by the repository's one reader."""
    path = ROOT / "scripts" / "crew_image_build_plan.py"
    spec = importlib.util.spec_from_file_location("crew_image_build_plan_kas", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.logical_instructions(DOCKERFILE.read_text(encoding="utf-8"))


def _kas_run() -> tuple[int, str, list[str]]:
    instructions = _instructions()
    hits = [
        (i, inst)
        for i, inst in enumerate(instructions)
        if inst.upper().startswith("RUN") and "install_bundle" in inst
    ]
    assert len(hits) == 1, f"expected exactly one KAS-staging RUN, found {len(hits)}"
    index, run = hits[0]
    return index, run, instructions


def test_bundle_kas_arg_defaults_off_while_the_route_does() -> None:
    """The image bakes the engine by default only once the route is on by default."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    declared = re.findall(r"^ARG BUNDLE_KAS=(\S*)$", text, flags=re.MULTILINE)
    assert declared, "docker/Dockerfile must declare ARG BUNDLE_KAS with a default"
    expected = "1" if AgentConfig().kas_bundle else "0"
    assert declared == [expected], (
        f"BUNDLE_KAS defaults to {declared}, but agent.kas_bundle defaults to "
        f"{AgentConfig().kas_bundle}: the image bakes the engine by default exactly "
        "when the route it serves is on by default"
    )


def test_the_root_is_an_image_env_so_docker_exec_sees_it() -> None:
    """``docker exec`` bypasses the entrypoint, so only an image ENV reaches it."""
    envs = [i for i in _instructions() if i.startswith("ENV ") and BUNDLED_KAS_DIR_ENV in i]
    assert envs == [f"ENV {BUNDLED_KAS_DIR_ENV}={KAS_ROOT}"]
    assert BUNDLED_KAS_DIR_ENV not in (ROOT / "docker" / "entrypoint.sh").read_text(
        encoding="utf-8"
    ), "the image ENV is the one hand-off; a second export in the entrypoint drifts"


def test_the_root_exists_on_every_build() -> None:
    """A BUNDLE_KAS=0 image still names the root, so the directory must exist."""
    _, run, _ = _kas_run()
    mkdir = run.index(f'mkdir -p "${{{BUNDLED_KAS_DIR_ENV}}}"')
    off = run.index('if [ "${BUNDLE_KAS}" = "0" ]; then')
    assert mkdir < off, "the empty root must be created before the BUNDLE_KAS=0 exit"


def test_staging_runs_the_backend_installer_against_the_pin() -> None:
    _, run, _ = _kas_run()
    for token in (
        "kas_bundle.fetch_manifest(KAS_BUNDLE_DEFAULT_URL,",
        "version=KAS_PIN_VERSION",
        "sha256=KAS_PIN_MANIFEST_SHA256",
        "kas_bundle.install_bundle(",
        "kas_bundle.installed_bundle(root=dest, version=KAS_PIN_VERSION)",
    ):
        assert token in run, f"the KAS-staging RUN lost {token!r}"
    assert "python -I -c" in run, "the installer must run isolated from PYTHON* env"


def test_staging_rejects_values_other_than_0_and_1() -> None:
    _, run, _ = _kas_run()
    assert 'case "${BUNDLE_KAS}" in' in run
    assert "0|1) ;;" in run
    assert "BUNDLE_KAS must be 0 or 1" in run


def test_staged_tree_is_readable_by_the_runtime_user() -> None:
    """install_bundle publishes a mkdtemp (0700) tree; the gateway runs as kirocrew."""
    _, run, _ = _kas_run()
    chmod = run.index(f'chmod -R u=rwX,go=rX "${{{BUNDLED_KAS_DIR_ENV}}}"')
    readback = run.index("runuser -u kirocrew -- python -I -c")
    assert chmod < readback, "the tree must be opened up before the kirocrew read-back"


def test_staging_follows_the_wheel_install_and_precedes_the_user_switch() -> None:
    index, _, instructions = _kas_run()
    wheel = [
        i for i, inst in enumerate(instructions) if "pip install" in inst and "/tmp/wheels" in inst
    ]
    useradd = [i for i, inst in enumerate(instructions) if "useradd" in inst]
    user = [i for i, inst in enumerate(instructions) if inst.startswith("USER ")]
    assert wheel and useradd and user
    assert wheel[0] < index, "the installer imports kiro_crew, so the wheel goes in first"
    assert useradd[0] < index, "the read-back runs as the kirocrew user"
    assert index < user[0], "staging writes a root-owned tree, so it runs before USER"


def test_staged_root_sits_outside_the_home_volume() -> None:
    assert not KAS_ROOT.startswith("/home/"), "a mounted home must not shadow the engine"


def test_staging_python_compiles() -> None:
    _, run, _ = _kas_run()
    snippets = re.findall(r"-c '([^']*)'", run)
    assert len(snippets) == 2, snippets
    for code in snippets:
        compile(code, "<docker kas staging>", "exec")


def test_docker_guide_documents_the_bake() -> None:
    text = DOCKER_GUIDE.read_text(encoding="utf-8")
    for token in ("BUNDLE_KAS=1", BUNDLED_KAS_DIR_ENV, KAS_ROOT, "agent.kas_bundle"):
        assert token in text, f"docs/guides/docker.md does not mention {token}"


def test_docker_smoke_builds_the_baked_image() -> None:
    text = SMOKE_WORKFLOW.read_text(encoding="utf-8")
    assert "--build-arg BUNDLE_KAS=1" in text
    assert "docker exec smoke-kas kirocrew kas status" in text
    assert "(built into the app)" in text

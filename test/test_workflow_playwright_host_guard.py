"""Every ``playwright install`` in a workflow carries the host-platform guard.

The pinned Playwright (``website/package.json``) has no ubuntu26.04 host entry,
so on a runner image of ubuntu 26.04 or later ``npx playwright install
chromium`` stops with ``Playwright does not support chromium on
ubuntu26.04-x64`` and the job never reaches the code it tests. The guard in
``ci.yml`` sets ``PLAYWRIGHT_HOST_PLATFORM_OVERRIDE`` to the ubuntu24.04 build
before the install and writes it to ``$GITHUB_ENV`` so the later steps that
launch the browser resolve the same build. A new install step without it goes
red on the next runner-image move, so the guard is checked per step here
(docs/ci/ci-and-reviews.md, "A runner image move and `playwright install`").

The guard counts when the step's ``run`` assigns the variable before the
install line and writes it to ``$GITHUB_ENV``, or when the job or the workflow
sets it in ``env``. Drop this test together with the guard once Playwright is
bumped to a release that knows ubuntu 26.04 natively.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

_VAR = "PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"
_INSTALL = re.compile(r"\bplaywright(?:@\S+)?\s+install\b")


def _guarded(run: str, install_line: int) -> bool:
    before = run.splitlines()[:install_line]
    # The GITHUB_ENV write reaches only LATER steps, so this step's own
    # install needs the variable assigned in its shell as well.
    assigns = any(re.search(rf"\b{_VAR}=", line) and "GITHUB_ENV" not in line for line in before)
    exported = any(_VAR in line and "GITHUB_ENV" in line for line in before)
    return assigns and exported


def _unguarded(workflows: Path = WORKFLOWS) -> list[tuple[str, str, str]]:
    """(workflow file, job id, step name) of each install without the guard."""
    found: list[tuple[str, str, str]] = []
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        workflow_env = doc.get("env") or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            env_guard = _VAR in workflow_env or _VAR in (job.get("env") or {})
            for index, step in enumerate(job.get("steps") or []):
                run = step.get("run") if isinstance(step, dict) else None
                if not isinstance(run, str):
                    continue
                for number, line in enumerate(run.splitlines()):
                    if line.lstrip().startswith("#") or not _INSTALL.search(line):
                        continue
                    if not (env_guard or _guarded(run, number)):
                        found.append((path.name, job_id, str(step.get("name") or index)))
                    break
    return found


def _installs(workflows: Path = WORKFLOWS) -> set[str]:
    """Workflow files holding at least one ``playwright install`` line."""
    return {
        path.name
        for path in workflows.glob("*.y*ml")
        if any(
            _INSTALL.search(line) and not line.lstrip().startswith("#")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }


def test_the_scanner_still_sees_the_ci_installs() -> None:
    # Without this, a changed step shape would make the guard check pass
    # vacuously. It names a file, not a count, so adding or removing an
    # install step in another PR cannot conflict with it.
    assert "ci.yml" in _installs()


def test_every_playwright_install_carries_the_host_platform_guard() -> None:
    assert _unguarded() == [], (
        "These steps run `playwright install` without the host-platform guard, so "
        "they fail on an ubuntu 26.04 runner image. Copy the guard from the "
        "`Install Playwright browser (Chromium)` steps in ci.yml, or set "
        f"{_VAR} in the job's env (docs/ci/ci-and-reviews.md)."
    )


_GUARD = (
    'if [ -r /etc/os-release ] && . /etc/os-release && [ "${VERSION_ID%%.*}" -ge 26 ]; then\n'
    f'  export {_VAR}="ubuntu24.04-x64"\n'
    f'  echo "{_VAR}=${_VAR}" >> "$GITHUB_ENV"\n'
    "fi\n"
)


def _workflow(tmp_path: Path, run: str, job_env: str = "") -> Path:
    body = "".join(f"          {line}\n" for line in run.splitlines())
    (tmp_path / "w.yml").write_text(
        f"jobs:\n  j:\n{job_env}    steps:\n      - name: install\n        run: |\n{body}",
        encoding="utf-8",
    )
    return tmp_path


def test_an_install_without_the_guard_is_flagged(tmp_path: Path) -> None:
    workflows = _workflow(tmp_path, "npx playwright install --with-deps chromium\n")
    assert _unguarded(workflows) == [("w.yml", "j", "install")]


def test_a_version_pinned_install_without_the_guard_is_flagged(tmp_path: Path) -> None:
    workflows = _workflow(tmp_path, "npx -y playwright@1.58.2 install chromium\n")
    assert _unguarded(workflows) == [("w.yml", "j", "install")]


def test_a_guard_after_the_install_is_flagged(tmp_path: Path) -> None:
    workflows = _workflow(tmp_path, "npx playwright install chromium\n" + _GUARD)
    assert _unguarded(workflows) == [("w.yml", "j", "install")]


def test_an_override_not_carried_to_later_steps_is_flagged(tmp_path: Path) -> None:
    run = f'export {_VAR}="ubuntu24.04-x64"\nnpx playwright install chromium\n'
    assert _unguarded(_workflow(tmp_path, run)) == [("w.yml", "j", "install")]


def test_an_override_written_only_to_github_env_is_flagged(tmp_path: Path) -> None:
    run = f'echo "{_VAR}=ubuntu24.04-x64" >> "$GITHUB_ENV"\nnpx playwright install chromium\n'
    assert _unguarded(_workflow(tmp_path, run)) == [("w.yml", "j", "install")]


def test_the_inline_guard_passes(tmp_path: Path) -> None:
    workflows = _workflow(tmp_path, _GUARD + "npx playwright install chromium\n")
    assert _unguarded(workflows) == []


def test_a_job_env_override_passes(tmp_path: Path) -> None:
    env = f"    env:\n      {_VAR}: ubuntu24.04-x64\n"
    workflows = _workflow(tmp_path, "npx playwright install chromium\n", job_env=env)
    assert _unguarded(workflows) == []


def test_a_commented_install_is_not_an_install(tmp_path: Path) -> None:
    workflows = _workflow(tmp_path, "# npx playwright install chromium\necho ok\n")
    assert _unguarded(workflows) == []

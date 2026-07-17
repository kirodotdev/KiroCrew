"""KiroCrew packaging — plain setuptools build.

Package metadata, dependencies, and entry points live in ``setup.cfg`` and
``pyproject.toml``; this file only adds a custom ``build_py`` step that copies
the pre-built frontend assets from ``src/kiro_crew/static/dist`` into the
package.

The frontend is built separately with npm/Vite in the ``website/`` directory
and the resulting ``dist/`` is copied into ``src/kiro_crew/static/dist`` before
packaging. Vite emits content-hashed filenames that change on every build, so
``static/dist/`` is intentionally excluded from the ``package_data`` globs in
``setup.cfg``; we copy the directory tree directly here instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from setuptools import Command, setup
from setuptools.command.build_py import build_py


class E2eTestCommand(Command):
    """Run the gated E2E smoke suite (``test/test_e2e_smoke.py``).

    Invoked as ``python setup.py test_e2e``. Distinct from the regular test run
    (the Makefile ``test`` target / plain ``pytest``) on purpose:
      * sets ``KIROCREW_E2E=1`` to lift the ``skipif`` gate in
        test_e2e_smoke.py (those tests spawn a real gateway subprocess);
      * runs only that one file, serially, clearing the default
        ``[tool:pytest]`` addopts (``-n auto`` + ``--cov`` + ``--timeout=120``)
        -- xdist would spawn one gateway per worker and coverage of a
        subprocess gateway is meaningless.
    """

    description = "Run the E2E suite (smoke + Playwright dashboard specs)"
    user_options: list = []

    def initialize_options(self) -> None:
        pass

    def finalize_options(self) -> None:
        pass

    def run(self) -> None:
        base = os.path.dirname(os.path.abspath(__file__))
        env = dict(os.environ)
        env["KIROCREW_E2E"] = "1"
        cmd = [
            sys.executable, "-m", "pytest",
            os.path.join("test", "test_e2e_smoke.py"),
            # Folded in: the dashboard Playwright suite boots the same harness
            # gateway (wired to the packaged fake ACP backend via
            # KIROCREW_KIRO_BIN) and shells `playwright test` against it, so
            # `test_e2e` is the single offline pre-release E2E gate.
            os.path.join("test", "test_playwright_e2e.py"),
            # Drop the heavy unit-test addopts (-n auto, --cov, --timeout=120);
            # the e2e suite runs serially with a longer per-test timeout. 1800s
            # gives the Playwright fold headroom: the browser suite runs ~4-5
            # min per interpreter leg and retries:2 under box contention can
            # push a retry-heavy run past a 600s cap, which would kill it as a
            # generic pytest timeout and hide the actual failing specs. Smoke
            # tests finish in seconds, so the larger cap costs them nothing.
            "-o", "addopts=",
            "-p", "no:cacheprovider",
            "-v",
            "--timeout=1800",
        ]
        print("[test_e2e] KIROCREW_E2E=1 " + " ".join(cmd))
        rc = subprocess.call(cmd, cwd=base, env=env)
        if rc != 0:
            raise SystemExit(rc)


class BuildWithFrontend(build_py):
    """Custom build_py that copies the pre-built frontend dist/ into the package.

    Expects ``src/kiro_crew/static/dist`` to already exist in-tree (built by
    ``npm run build`` in the ``website/`` directory and copied in by the build
    step). If it is missing we print a warning telling the user to build the
    frontend, but do not fail — the backend is still usable without the bundled
    web UI assets.
    """

    def run(self) -> None:
        super().run()
        base = os.path.dirname(os.path.abspath(__file__))
        src_dist = os.path.join(base, "src", "kiro_crew", "static", "dist")
        if os.path.isdir(src_dist):
            build_dist = os.path.join(
                self.build_lib, "kiro_crew", "static", "dist"
            )
            if os.path.isdir(build_dist):
                shutil.rmtree(build_dist)
            shutil.copytree(src_dist, build_dist)
        else:
            print(
                "WARNING: frontend assets not found at "
                f"{src_dist}\n"
                "         The bundled web UI will be missing from this build.\n"
                "         Build the frontend first:\n"
                "             cd website && npm install && npm run build\n"
                "         then copy website/dist into src/kiro_crew/static/dist."
            )
        self._copy_changelog(base)

    def _copy_changelog(self, base: str) -> None:
        """Bundle the repo-root CHANGELOG.md into the package.

        Pip-wheel installs ship no source tree, so the dashboard's
        ``/api/changelog`` endpoint (handlers/updates.py:_changelog_path) falls
        back to a bundled ``kiro_crew/CHANGELOG.md``. Copy it in here rather than
        via package_data because it lives at the repo root, outside the
        ``src/kiro_crew`` package tree that setuptools globs.
        """
        src_changelog = os.path.join(base, "CHANGELOG.md")
        if os.path.isfile(src_changelog):
            pkg_dir = os.path.join(self.build_lib, "kiro_crew")
            if os.path.isdir(pkg_dir):
                shutil.copy2(src_changelog, os.path.join(pkg_dir, "CHANGELOG.md"))


setup(
    cmdclass={
        "build_py": BuildWithFrontend,
        "test_e2e": E2eTestCommand,
    },
)

"""The test files that read a changed prose doc or frontend file as data.

SHADOW ONLY, like its callers (`scripts/ci_test_map.py select`,
`scripts/ci_test_selection.py`): no CI lane reads what they print.

The test map records the `.py` files a test ran, so a doc or a frontend file a
test reads is invisible to it. CI already answers "which tests read these?" for
two kinds of path, and acts on the answer:

* a prose doc: `.github/scripts/docs_only.py` says which paths are prose docs
  (`is_prose_doc`) and which test files could read one (`readers`), the set the
  `docs-contract-tests` job runs for a docs-only pull request;
* a frontend path: ci.yml's `changes` job files it under its `frontend` path
  filter, and `scripts/ci-surface-tests.py` lists the backend test files that
  must still run for a diff of such paths alone.

`DataRule` asks those two and holds no list of its own: the prose-doc test and
the reader scan are `docs_only`'s, the frontend globs are read from ci.yml, and
the must-run list is `ci-surface-tests.py`'s. A path neither covers has no
readers here and keeps its fall-back in `ci_test_map.select`.

One kind of prose doc is left uncovered: a doc under `src/`. Those ship in the
package, product code reads them at run time (`mcp_guide.search_docs`, the tips
catalog), and `docs_only.readers` scans test files only, so a test that reaches
such a doc through product code is not in its answer.

Each scan reads the whole test tree, so an instance keeps what it scanned: one
diff is asked about more than once (the Linux verdict, then its Windows block).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_test_selection_windows as windows  # noqa: E402  (path set immediately above)

DOCS_ONLY = ".github/scripts/docs_only.py"
SURFACE_TESTS = "scripts/ci-surface-tests.py"
# The ci.yml path filter a frontend path falls under, and the surface whose
# must-run tests `ci-surface-tests.py` lists for a diff confined to it.
FRONTEND_BUCKET = "frontend"
BACKEND_SURFACE = "backend"
# Package data: read by product code, which the reader scan does not follow.
PACKAGE_TREE = "src/"


def load_script(name: str, path: Path) -> ModuleType:
    """The module in the file at `path`, which need not be importable by name."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{path}: cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DataRule:
    """Which changed paths have known readers in the tree at `root`, and who they are."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = load_script("docs_only", REPO_ROOT / DOCS_ONLY)
        self.surface = load_script("ci_surface_tests", REPO_ROOT / SURFACE_TESTS)
        self._frontend: list[re.Pattern[str]] | None = None
        self._doc_readers: dict[frozenset[str], set[str]] = {}
        self._surface_tests: set[str] | None = None

    def is_doc(self, path: str) -> bool:
        return bool(self.docs.is_prose_doc(path)) and not path.startswith(PACKAGE_TREE)

    def is_frontend(self, path: str) -> bool:
        if self._frontend is None:
            try:
                globs = windows.path_bucket(self.root, FRONTEND_BUCKET)
            except (OSError, ValueError):
                # A filter that cannot be read covers no path: each one then
                # keeps its fall-back.
                globs = []
            self._frontend = [windows.glob_pattern(glob) for glob in globs]
        return any(pattern.fullmatch(path) for pattern in self._frontend)

    def covers(self, path: str) -> bool:
        """Whether `tests` can name the test files that read `path`."""
        return self.is_doc(path) or self.is_frontend(path)

    def tests(self, paths: Iterable[str]) -> set[str]:
        """The backend test files that could read one of `paths`, each of which `covers`."""
        found: set[str] = set()
        docs = frozenset(path for path in paths if self.is_doc(path))
        if docs:
            if docs not in self._doc_readers:
                listed = self.docs.readers(sorted(docs), str(self.root))
                # `readers` lists frontend specs as well; the backend suite runs none.
                self._doc_readers[docs] = {t for t in listed if not self.is_frontend(t)}
            found |= self._doc_readers[docs]
        if any(self.is_frontend(path) for path in paths):
            if self._surface_tests is None:
                # `collect` reads the tree its own file sits in.
                if self.root.resolve() != self.surface._repo_root().resolve():
                    raise ValueError(f"{SURFACE_TESTS} reads its own tree, not {self.root}")
                self._surface_tests = set(self.surface.collect(BACKEND_SURFACE))
            found |= self._surface_tests
        return found

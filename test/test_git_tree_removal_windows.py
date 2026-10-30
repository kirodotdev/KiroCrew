"""A tree git produced is removed with ``rmtree_force``, never ``ignore_errors``.

git writes every object, pack and index under ``.git`` read-only. Windows refuses to
unlink such a file whatever its directory allows, ``shutil.rmtree(..., ignore_errors=True)``
swallows each refusal, and the caller carries on as if the tree were gone. POSIX asks the
directory's write bit instead, so the same call succeeds there and the residue never shows
locally.

Rule 1: removing a tree git produced clears the read-only bit and retries.
Rule 2: a restore into a path whose replacement could not be removed still lands.
Rule 3: every site that removes a git-produced tree routes through the one helper.

Rule 1 runs against ``windows_sim.readonly_unlink_refused`` on every OS and, for the
``real`` parameter, against the real file attribute on a Windows runner. Rule 2 holds a
file back with ``windows_sim.unlink_sharing_violation``. Rule 3 reads the source.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Iterator

import pytest
from windows_sim import readonly_unlink_refused, unlink_sharing_violation

import kiro_crew
from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.builtins.auto_improvement.backend import pr_watchers as W

#: The real read-only attribute exists only on Windows, so that parameter is collected
#: only there. The simulated one runs everywhere.
_READ_ONLY_PARAMS = ["simulated"] if platform_compat.IS_POSIX else ["simulated", "real"]

_PACK = "pack-0123456789abcdef0123456789abcdef01234567.pack"


@pytest.fixture(params=_READ_ONLY_PARAMS)
def read_only_objects(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run where a read-only file cannot be unlinked: simulated anywhere, for real on Windows."""
    if request.param == "real":
        yield
        return
    with readonly_unlink_refused():
        yield


def _plant_checkout(root: Path) -> Path:
    """What ``git clone`` leaves at *root*: a pack and a loose object, both read-only."""
    pack = root / ".git" / "objects" / "pack" / _PACK
    loose = root / ".git" / "objects" / "ab" / ("c" * 38)
    for blob in (pack, loose):
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"PACK")
        os.chmod(blob, 0o444)
    (root / "app.json").write_text("{}", encoding="utf-8")
    return pack


class TestRule1RemovingAGitTreeClearsTheReadOnlyBit:
    def test_the_retention_sweep_retires_an_aged_checkout(
        self, tmp_path: Path, read_only_objects: None
    ) -> None:
        sources = tmp_path / "app-sources"
        aside = sources / "demoapp.stale-0123abcd"
        _plant_checkout(aside)
        aged = aside.stat().st_mtime + registry._STALE_CHECKOUT_RETENTION_DAYS * 86400 + 3600

        assert registry._sweep_stale_checkouts_sync(sources, aged) == [aside.name]
        assert not aside.exists()

    @pytest.mark.asyncio
    async def test_a_rejected_fresh_clone_is_removed_and_the_previous_checkout_restored(
        self, tmp_path: Path, read_only_objects: None
    ) -> None:
        pkg = tmp_path / "demo"
        _plant_checkout(pkg)
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()
        (stale / "mine.txt").write_text("local edit", encoding="utf-8")
        log: list[str] = []

        await registry._unpoison_rejected_checkout(
            "demo",
            pkg,
            log,
            checkout_preexisted=False,
            pre_pull_commit="",
            restore_from=stale,
        )

        assert (pkg / "mine.txt").read_text(encoding="utf-8") == "local edit"
        assert not (pkg / ".git").exists()
        assert not stale.exists()
        # Removed, not merely set aside: a `.partial-*` sibling is what a survivor becomes.
        assert [p.name for p in tmp_path.iterdir() if ".partial-" in p.name] == []
        assert any("Restored the previous checkout" in line for line in log)

    @pytest.mark.asyncio
    async def test_a_refused_vault_clone_leaves_no_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_only_objects: None
    ) -> None:
        from kiro_crew.apps.builtins.md_notebook import server

        monkeypatch.setattr(server, "_HOME", tmp_path / "home")

        async def _clone(*, dir_: str, vault_id: str, **_kwargs: object) -> dict:
            _plant_checkout(Path(dir_))
            return {"id": vault_id, "localPath": dir_}

        async def _refuse(_vault: dict, _rel: object = None) -> Path:
            raise server.ApiError("subfolder escapes the vault", 400, code="bad_subfolder")

        async def _no_auth() -> None:
            return None

        class _Request:
            async def read(self) -> bytes:
                return b'{"url": "https://example.invalid/notes.git"}'

        monkeypatch.setattr(server.git_ops, "clone_vault", _clone)
        monkeypatch.setattr(server, "vault_path", _refuse)
        monkeypatch.setattr(server, "resolve_auth", _no_auth)

        with pytest.raises(server.ApiError):
            await server.api_vault_clone(_Request())  # type: ignore[arg-type]

        clone_root = server._clone_root()
        assert not clone_root.exists() or list(clone_root.iterdir()) == []

    def test_a_watchers_clone_is_removed_with_its_objects(
        self, tmp_path: Path, read_only_objects: None
    ) -> None:
        reg = W.PRWatcherRegistry(
            autostart=False, isolate_clone=False, clones_root=str(tmp_path / "clones")
        )
        mine = Path(reg._clone_dir("fp1"))
        _plant_checkout(mine)

        reg._cleanup_clone(W.WatcherState(fp="fp1", pr="x"), str(mine))

        assert not mine.exists()

    def test_an_unowned_clone_is_reclaimed_with_its_objects(
        self, tmp_path: Path, read_only_objects: None
    ) -> None:
        reg = W.PRWatcherRegistry(
            autostart=False, isolate_clone=False, clones_root=str(tmp_path / "clones")
        )
        orphan = tmp_path / "orphan"
        _plant_checkout(orphan)

        assert W._delete_clone_if_unowned(reg, orphan) is True
        assert not orphan.exists()


class TestRule2TheRestoreLandsWhenTheReplacementSurvives:
    """A file another process holds open still refuses to unlink after the read-only bit
    is cleared, so the rejected checkout can outlive the removal. The previous checkout
    is restored regardless: the survivor is renamed aside, not collided with."""

    @pytest.mark.asyncio
    async def test_the_previous_checkout_is_restored_beside_the_survivor(
        self, tmp_path: Path
    ) -> None:
        pkg = tmp_path / "demo"
        pkg.mkdir()
        (pkg / "held.bin").write_bytes(b"open elsewhere")
        stale = tmp_path / "demo.stale-0123abcd"
        stale.mkdir()
        (stale / "mine.txt").write_text("local edit", encoding="utf-8")
        log: list[str] = []

        with unlink_sharing_violation(match="held.bin", times=10**6):
            await registry._unpoison_rejected_checkout(
                "demo",
                pkg,
                log,
                checkout_preexisted=False,
                pre_pull_commit="",
                restore_from=stale,
            )

        assert (pkg / "mine.txt").read_text(encoding="utf-8") == "local edit"
        survivors = [p for p in tmp_path.iterdir() if p.name.startswith("demo.partial-")]
        assert len(survivors) == 1
        assert (survivors[0] / "held.bin").exists()
        assert any("Restored the previous checkout" in line for line in log)


#: (file under src/kiro_crew, function, the tree it removes as written at the call).
#: Each tree is one git produced: a clone, a ``--local`` clone, or a ``.git`` directory.
_GIT_TREE_SITES = [
    ("apps/builtins/md_notebook/server.py", "api_vault_clone", "str(clone_dir)"),
    ("apps/registry_pipeline/recovery.py", "_sweep_stale_checkouts_sync", "child"),
    ("apps/registry_pipeline/install_rollback.py", "_unpoison_rejected_checkout", "pkg_dir"),
    ("apps/registry_pipeline/install_clone.py", "_clone_build_app_locked", "pkg_dir"),
    ("apps/routes_uninstall.py", "_run_uninstall", "ws_dir"),
    ("apps/builtins/auto_improvement/backend/pr_watchers.py", "setup_isolated_clone", "dest"),
    (
        "apps/builtins/auto_improvement/backend/pr_watchers.py",
        "PRWatcherRegistry._cleanup_clone",
        "clone",
    ),
    ("apps/builtins/auto_improvement/backend/pr_watchers.py", "_delete_clone_if_unowned", "child"),
    ("apps/builtins/auto_improvement/spine/gate.py", "Gate.run_bug", "base_src.parent"),
    ("apps/builtins/auto_improvement/spine/gate.py", "Gate._stage_test_only_base", "base_tree"),
    ("apps/builtins/auto_improvement/spine/gate.py", "Gate._stage_test_only_base", "gitlink"),
    ("apps/builtins/design_critique/backend/routes.py", "_sweep_clones", "child"),
    ("apps/builtins/design_critique/backend/routes.py", "_discover_repo_job", "target"),
    ("dashboard/handlers/themes.py", "_do_install", "tmp_root"),
]

_HELPERS = frozenset({"rmtree_force", "_rmtree_force_settled"})


def _names(node: ast.AST, wanted: frozenset[str] | set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in wanted
    return isinstance(node, ast.Name) and node.id in wanted


def _find_function(tree: ast.AST, qualname: str) -> ast.AST | None:
    def walk(node: ast.AST, prefix: str) -> ast.AST | None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if name == qualname:
                    return child
                found = walk(child, f"{name}.")
            else:
                found = walk(child, prefix)
            if found is not None:
                return found
        return None

    return walk(tree, "")


def _removed_trees(scope: ast.AST) -> tuple[list[str], list[str]]:
    """(trees ``shutil.rmtree`` removes, trees the helper removes) written inside *scope*.

    A reference counts whether it is called (``rmtree(x)``) or handed to a thread
    (``to_thread(rmtree, x)``, ``run_in_executor(None, rmtree, x)``).
    """
    bare: list[str] = []
    routed: list[str] = []
    for call in (n for n in ast.walk(scope) if isinstance(n, ast.Call)):
        references = [(call.func, call.args)]
        references += [(arg, call.args[i + 1 :]) for i, arg in enumerate(call.args)]
        for reference, rest in references:
            if not rest:
                continue
            if _names(reference, {"rmtree"}):
                bare.append(ast.unparse(rest[0]))
            elif _names(reference, _HELPERS):
                routed.append(ast.unparse(rest[0]))
    return bare, routed


class TestRule3EverySiteUsesTheOneHelper:
    @pytest.mark.parametrize(
        ("rel", "qualname", "tree"),
        _GIT_TREE_SITES,
        ids=[f"{rel.rsplit('/', 1)[-1]}::{qualname}" for rel, qualname, _ in _GIT_TREE_SITES],
    )
    def test_a_git_tree_is_never_removed_by_shutil_rmtree(
        self, rel: str, qualname: str, tree: str
    ) -> None:
        source = (Path(kiro_crew.__file__).parent / rel).read_text(encoding="utf-8")
        scope = _find_function(ast.parse(source), qualname)
        assert scope is not None, f"{rel} no longer defines {qualname}; update _GIT_TREE_SITES"

        bare, routed = _removed_trees(scope)

        assert tree not in bare, f"{qualname} removes {tree} with shutil.rmtree"
        assert tree in routed, f"{qualname} does not remove {tree} through rmtree_force"

    def test_the_scan_sees_each_spelling(self) -> None:
        scope = ast.parse(
            "async def f(a, b, c, d):\n"
            "    shutil.rmtree(a, ignore_errors=True)\n"
            "    await asyncio.to_thread(shutil.rmtree, b, True)\n"
            "    platform_compat.rmtree_force(c)\n"
            "    await loop.run_in_executor(None, _rmtree_force_settled, d)\n"
        )

        assert _removed_trees(scope) == (["a", "b"], ["c", "d"])

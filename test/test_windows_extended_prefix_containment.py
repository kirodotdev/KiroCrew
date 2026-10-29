"""Path-containment checks fold Windows' extended-length prefix off both operands.

On Windows ``Path.resolve()`` / ``os.path.realpath()`` on a file another thread is
replacing can come back as ``\\\\?\\C:\\...`` while a directory resolved separately
comes back plain. A containment check comparing the two read the lone prefix as an
escape and refused a path genuinely inside its root.

``platform_compat.strip_extended_length_prefix`` folds a leading ``\\\\?\\`` off
whatever string it is handed, on every platform. So these cases run anywhere: the
"prefixed" operand is the real path with ``\\\\?\\`` glued on the front, which the
fold turns back into the real path. Each site is checked both ways -- the prefixed
spelling of an inside path is accepted, and an outside path is still refused.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PREFIX = "\\\\?\\"


def _prefixed(path: Path | str) -> str:
    return _PREFIX + str(path)


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A root with one file inside it, and a sibling directory outside it."""
    root = (tmp_path / "root").resolve()
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("x", encoding="utf-8")
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (outside / "f.txt").write_text("x", encoding="utf-8")
    return root, inside, outside / "f.txt"


def _prefix_resolve_of(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make ``Path.resolve()`` of *target* come back prefixed, as the race does."""
    real_resolve = Path.resolve

    def fake(self: Path, strict: bool = False) -> Path:
        out = real_resolve(self, strict=strict)
        return Path(_prefixed(out)) if out == target else out

    monkeypatch.setattr(Path, "resolve", fake)


def _prefix_realpath_of(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make ``os.path.realpath()`` of *target* come back prefixed."""
    real_realpath = os.path.realpath

    def fake(path, *a, **k):  # type: ignore[no-untyped-def]
        out = real_realpath(path, *a, **k)
        return _prefixed(out) if out == str(target) else out

    monkeypatch.setattr(os.path, "realpath", fake)


def test_the_fold_is_lexical_and_only_strips_the_prefix() -> None:
    from kiro_crew.platform_compat import strip_extended_length_prefix

    assert strip_extended_length_prefix(_prefixed("/a/b")) == "/a/b"
    assert strip_extended_length_prefix("/a/b") == "/a/b"


def test_file_explorer_is_within(tree) -> None:
    from kiro_crew.apps.builtins.file_explorer import server

    root, inside, outside = tree
    assert server._is_within(Path(_prefixed(inside)), root)
    assert server._is_within(inside, Path(_prefixed(root)))
    assert not server._is_within(Path(_prefixed(outside)), root)


def test_skill_root_containment(tree) -> None:
    from kiro_crew.dashboard.handlers._shared_skill_roots import _path_at_or_under

    root, inside, outside = tree
    assert _path_at_or_under(Path(_prefixed(inside)), root)
    assert _path_at_or_under(Path(_prefixed(root)), root)
    assert not _path_at_or_under(Path(_prefixed(outside)), root)


def test_worktree_allowed_root(tree) -> None:
    from kiro_crew.dashboard.handlers.worktree import _match_allowed_root

    root, inside, outside = tree
    assert _match_allowed_root(str(inside), [_prefixed(root)]) == _prefixed(root)
    assert _match_allowed_root(_prefixed(inside), [str(root)]) == str(root)
    assert _match_allowed_root(str(outside), [_prefixed(root)]) is None


def test_steering_contained(tree, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers.steering import _contained

    root, inside, outside = tree
    _prefix_resolve_of(monkeypatch, inside)
    assert _contained(inside, root)
    assert not _contained(outside, root)


def test_pptx_contained(tree, monkeypatch) -> None:
    from kiro_crew.apps.builtins.pptx_maker.backend.paths import _contained

    root, inside, outside = tree
    _prefix_resolve_of(monkeypatch, inside)
    assert _contained(inside, root) is not None
    assert _contained(outside, root) is None


def test_papyrus_safe_child(tree, monkeypatch) -> None:
    from kiro_crew.apps.builtins.papyrus.backend.store import PathRejected, safe_child

    root, inside, _outside = tree
    _prefix_resolve_of(monkeypatch, inside)
    assert safe_child(root, "inside.txt")
    with pytest.raises(PathRejected):
        safe_child(root, "../outside/f.txt")


def test_spec_builder_contained(tree, monkeypatch) -> None:
    from kiro_crew.apps.builtins.spec_builder.backend import repository

    root, inside, outside = tree
    _prefix_realpath_of(monkeypatch, inside)
    assert repository._contained(inside, root)
    assert not repository._contained(outside, root)


def test_design_tweak_contain_path(tree, monkeypatch) -> None:
    from kiro_crew.apps.builtins.design_tweak.backend.request_state import (
        PathEscape,
        contain_path,
    )

    root, inside, outside = tree
    _prefix_realpath_of(monkeypatch, inside)
    assert contain_path(root, "inside.txt")
    with pytest.raises(PathEscape):
        contain_path(root, str(outside))


def test_subagent_validate_cwd(tree, monkeypatch) -> None:
    from kiro_crew.subagent import validate_cwd

    root, _inside, outside = tree
    sub = root / "sub"
    sub.mkdir()
    # The ROOT comes back prefixed here: the cwd itself must stay a real
    # directory for the ``isdir`` check that runs before the compare.
    _prefix_realpath_of(monkeypatch, root)
    resolved, error = validate_cwd(str(sub), [str(root)])
    assert error == "" and resolved
    _resolved, error = validate_cwd(str(outside.parent), [str(root)])
    assert error


@pytest.mark.parametrize(
    ("path", "verbatim_only"),
    [
        (_PREFIX + "C:\\Users\\u\\report.md.", True),
        (_PREFIX + "C:\\Users\\u\\report.md ", True),
        (_PREFIX + "C:\\Users\\u\\CON.txt", True),
        (_PREFIX + "C:\\Users\\nul\\a.txt", True),
        (_PREFIX + "UNC\\host\\share\\x.", True),
        (_PREFIX + "C:\\Users\\u\\report.md", False),
        (_PREFIX + "UNC\\host\\share\\report.md", False),
        (_PREFIX + "UNC\\server\\aux\\report.md", False),
        (_PREFIX + "UNC\\con\\share\\report.md", False),
        (_PREFIX + "C:\\Users\\u\\com10.txt", False),
        ("C:\\Users\\u\\report.md.", False),
        ("/home/u/CON", False),
    ],
)
def test_a_verbatim_only_name_is_named(path: str, verbatim_only: bool) -> None:
    from kiro_crew.platform_compat import names_a_verbatim_only_file

    assert names_a_verbatim_only_file(path) is verbatim_only
    assert names_a_verbatim_only_file(Path(path)) is verbatim_only


def _resolve_to(monkeypatch: pytest.MonkeyPatch, spelled: str, name: str) -> None:
    """Make ``Path.resolve()`` of a path named *name* come back as *spelled*.

    Every other path resolves for real. A blanket patch also redirects paths the
    code under test resolves for its own bookkeeping (an audit log, a config
    file), and on POSIX the ``\\\\?\\`` spelling is a relative name, so a write
    to one of those would land a file in the working directory.
    """
    real_resolve = Path.resolve

    def fake(self: Path, strict: bool = False) -> Path:
        if self.name == name:
            return Path(spelled)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake)


_SIBLING_DOT = _PREFIX + "C:\\Users\\u\\report.md."


def test_the_source_link_read_refuses_a_verbatim_only_name(monkeypatch) -> None:
    from kiro_crew import artifacts_source_link

    _resolve_to(monkeypatch, _SIBLING_DOT, "report.md.")
    assert artifacts_source_link._try_read_source_path(None, "report.md.") is None  # type: ignore[arg-type]


def test_the_source_link_write_refuses_a_verbatim_only_name(monkeypatch) -> None:
    from kiro_crew import artifacts_source_link

    _resolve_to(monkeypatch, _SIBLING_DOT, "report.md.")
    assert (
        artifacts_source_link._try_write_source_path(None, "report.md.", "x")  # type: ignore[arg-type]
        is False
    )


def test_the_workspace_dir_check_refuses_a_verbatim_only_name(tree, monkeypatch) -> None:
    from kiro_crew import cli_commands_workspace

    root, _inside, _outside = tree
    # An absolute dir, so composing it reads no data home under the patched resolve.
    _resolve_to(monkeypatch, _PREFIX + "C:\\Users\\u\\.kiro\\ws.", "ws.")
    assert cli_commands_workspace._ws_dir_resolves_inside_home(str(root / "ws.")) is None


def test_the_channel_upload_gate_refuses_a_verbatim_only_name(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import files

    denials: list[str] = []

    class _Sel:
        def log_tool_invocation(self, **kwargs: object) -> None:
            denials.append(str(kwargs.get("error")))

    monkeypatch.setattr(files, "_sel", lambda: _Sel())
    _resolve_to(monkeypatch, _SIBLING_DOT, "report.md.")
    response, resolved, raw = files._gate_upload_file(
        "report.md.", "report.md", tool_kind="channel"
    )
    assert response is not None and response.status == 403
    assert resolved is None and raw is None
    assert denials and denials[-1].startswith("verbatim_only_name:")


def test_papyrus_keeps_the_main_document_when_one_realpath_is_prefixed(tree, monkeypatch) -> None:
    from kiro_crew.apps.builtins.papyrus.backend import store

    root, _inside, _outside = tree
    main = root / "main.tex"
    main.write_text("x", encoding="utf-8")
    real_realpath = os.path.realpath
    calls: list[str] = []

    def fake(path, *a, **k):  # type: ignore[no-untyped-def]
        out = real_realpath(path, *a, **k)
        if sys._getframe(1).f_code.co_name != "delete_file":
            return out  # ``Path.resolve`` reads ``realpath`` too; leave those plain
        calls.append(out)
        # Only the first operand (the delete target) keeps the prefix, as in the race.
        return _prefixed(out) if len(calls) == 1 else out

    monkeypatch.setattr(os.path, "realpath", fake)
    with pytest.raises(ValueError, match="main document"):
        store.delete_file(root, "main.tex")
    assert len(calls) == 2
    assert main.exists()

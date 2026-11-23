"""Contract tests for scripts/ci_test_os_tables.py, the map's Windows tables.

Pinned on a fixture tree: one src file per tier, a pattern only a comment or a
docstring carries (not matched), each `windows_targeting` rule, a file Windows
never collects, and the absent-table case; plus `bundle` writing the tables.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import ci_test_map as ctm
from scripts import ci_test_os_tables as ost
from scripts import ci_test_selection as cts

SRC = {
    "w1.py": "import sys\n\nif sys.platform == 'win32':\n    pass\n",
    "w1_exe.py": "BIN = 'kiro-cli.exe'\n",
    "w2.py": "import fcntl\n",
    "w3.py": "import subprocess\n",
    "both.py": "import fcntl\nimport sys\nX = sys.platform\n",
    "comment_only.py": "# sys.platform and fcntl and subprocess\nX = 1\n",
    "docstring_only.py": '"""Uses sys.platform, fcntl, subprocess."""\n\n\ndef f():\n    """os.name too."""\n    return 1\n',
    "plain.py": "X = 1\n",
    "_vendor/lib.py": "import fcntl\n",
    "tests/test_inner.py": "import fcntl\n",
}

TESTS = {
    "test_windows_paths.py": "def test_x():\n    pass\n",
    "test_skip.py": (
        "import sys\nimport pytest\n\n"
        "@pytest.mark.skipif(\n    sys.platform == 'win32', reason='posix'\n)\ndef test_x():\n    pass\n"
    ),
    "test_marker.py": "import pytest\n\n@pytest.mark.posix_only\ndef test_x():\n    pass\n",
    "test_faked.py": "import sys\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(sys, 'platform', 'win32')\n",
    "test_faked_flag.py": (
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr('kiro_crew.platform_compat.IS_WINDOWS', True)\n"
    ),
    "test_ignored_skip.py": (
        "import sys\nimport pytest\n\n@pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
        "def test_x():\n    pass\n"
    ),
    "test_comment_only.py": "# pytest.mark.skipif(sys.platform == 'win32')\ndef test_x():\n    pass\n",
    "test_plain.py": "import subprocess\n\ndef test_x():\n    pass\n",
    "helper_windows.py": "X = 1\n",
}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name, body in SRC.items():
        path = tmp_path / "src" / "kiro_crew" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for name, body in TESTS.items():
        (tmp_path / "test").mkdir(exist_ok=True)
        (tmp_path / "test" / name).write_text(body, encoding="utf-8")
    (tmp_path / "test" / "windows-collect-ignore.txt").write_text(
        "# not collected on Windows\ntest_ignored_skip.py  # posix only\n\n", encoding="utf-8"
    )
    return tmp_path


def test_each_src_file_takes_its_strongest_tier_and_the_table_holds_w1_and_w2(
    tree: Path,
) -> None:
    tiers = {name: ost.file_tier(body) for name, body in SRC.items()}
    assert {tiers[name] for name in ("w1.py", "w1_exe.py", "both.py")} == {"W1"}
    assert (tiers["w2.py"], tiers["w3.py"], tiers["plain.py"]) == ("W2", "W3", None)
    # Every W1 and W2 file outside `_vendor` and `tests`, and no other.
    assert ost.os_sensitive(tree) == {
        f"src/kiro_crew/{name}": tier
        for name, tier in tiers.items()
        if tier in ost.STORED_TIERS and "/" not in name
    }
    assert set(ost.os_sensitive(tree).values()) == set(ost.STORED_TIERS)


def test_a_pattern_in_a_comment_or_docstring_is_not_matched() -> None:
    assert ost.tier_of(ost.code_only(SRC["comment_only.py"])) is None
    assert ost.tier_of(ost.code_only(SRC["docstring_only.py"])) is None
    # A string that is an operand, not a statement, is still code.
    assert ost.tier_of(ost.code_only("X = 'a.cmd'\n")) == "W1"


# Where a string can sit alone on its line. `{}` takes the string.
OPERAND_SHAPES = [
    "run(\n    {}\n)\n",
    "run(\n    # why\n    {}\n)\n",
    "run(a,  # why\n    {}\n)\n",
    "x = (a or\n    {}\n)\n",
    "x = (a if b else\n    {}\n)\n",
    "x = (a +\n    {}\n)\n",
    "x = (a -\n    {}\n)\n",
    "x = (a *\n    {}\n)\n",
    "x = (a |\n    {}\n)\n",
    "ok = (name ==\n    {}\n)\n",
    "ok = (name in\n    {}\n)\n",
    "d = {{'k':\n    {}\n}}\n",
    "f = (lambda:\n    {}\n)\n",
    "x = [\n    {}\n]\n",
    "x = y[\n    {}\n]\n",
    "x = (\n    'a'\n    {}\n)\n",
    "x = a + \\\n    {}\n",
    "def f(a=(\n    {}\n)):\n    pass\n",
]
STATEMENT_SHAPES = [
    "{}\n",
    "{}\nimport json\n",
    "def f():\n    {}\n",
    "def f():\n    # why\n    {}\n    return 1\n",
    "class C:\n\n    {}\n",
    "def f(a=(1, 2)): {}\n",
    "async def f():\n    {}\n",
    "x = (1,\n     2)\n{}\n",
    "def f():\n    x = [1]\n    {}  # why\n",
]
LITERALS = [
    "'tool.exe'",
    '"tool.exe"',
    '"""tool.exe"""',
    "r'tool.exe'",
    '"""a\ntool.exe"""',
]


def _string_statements(source: str) -> list[str]:
    """The string literals `ast` says are statements of their own."""
    return [
        ast.get_source_segment(source, node.value) or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]


@pytest.mark.parametrize("shape", OPERAND_SHAPES + STATEMENT_SHAPES)
@pytest.mark.parametrize("literal", LITERALS)
def test_a_string_is_blanked_only_where_python_reads_it_as_a_statement(
    shape: str, literal: str
) -> None:
    source = shape.format(literal)
    is_statement = _string_statements(source) == [literal]
    assert is_statement == (shape in STATEMENT_SHAPES)
    # An operand keeps its signal, whatever line it is on; a docstring loses it.
    assert ost.file_tier(source) == (None if is_statement else "W1")
    assert (ost.code_only(source).count("tool.exe") == 0) == is_statement
    assert len(ost.code_only(source)) == len(source)


def test_brackets_that_do_not_balance_keep_every_string_as_code() -> None:
    assert ost.file_tier("x = (\n'tool.exe'\n") == "W1"
    assert ost.file_tier(")\n'tool.exe'\n") is None


def test_a_standalone_f_string_runs_code_so_it_stays_code() -> None:
    assert ost.file_tier('def f():\n    f"{os.fork()}"\n') == "W2"


def test_read_text_without_an_encoding_is_w3_whatever_else_it_passes() -> None:
    assert ost.tier_of("p.read_text(errors='replace')\n") == "W3"
    assert ost.tier_of("p.read_text(encoding='utf-8', errors='replace')\n") is None


def test_a_targeting_call_is_judged_by_its_own_arguments() -> None:
    long_skip = (
        "@pytest.mark.skipif(\n    "
        + " and ".join(["flag"] * 80)
        + "\n    or sys.platform == 'win32',\n"
        "    reason='x',\n)\ndef test_x():\n    pass\n"
    )
    assert ost.targets_windows("test/test_a.py", long_skip)
    patched = "def test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'IS_WINDOWS', True)\n"
    assert ost.targets_windows("test/test_a.py", patched)
    qualified_fakes = (
        "patch('kiro_crew.mod.sys.platform', 'win32')",
        "setattr(mod.os, 'name', 'nt')",
    )
    for qualified in qualified_fakes:
        assert ost.targets_windows("test/test_a.py", f"def test_x():\n    {qualified}\n")
    darwin = "@pytest.mark.skipif(sys.platform != 'darwin', reason='x')\ndef test_x():\n    pass\n"
    assert ost.targets_windows("test/test_a.py", darwin)
    # A platform reference beside an unrelated patch call is not a faked platform.
    unrelated = (
        "def test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'x', 1)\n    assert os.name\n"
    )
    assert not ost.targets_windows("test/test_a.py", unrelated)
    nearby = "with patch('mod.f'):\n    pass\nprint(sys.platform)\n"
    assert not ost.targets_windows("test/test_a.py", nearby)


def test_a_one_argument_replace_is_w3_and_a_string_replace_is_not() -> None:
    assert ost.tier_of("def move(src, dst):\n    return src.replace(dst)\n") == "W3"
    assert ost.tier_of("os.replace(a, b)\n") == "W3"
    assert ost.tier_of("name = text.replace('a', 'b')\n") is None
    assert ost.tier_of("stamp = now.replace(tzinfo=None)\n") is None


def test_a_docstring_on_its_def_or_class_line_is_not_matched() -> None:
    assert ost.file_tier('def f(): """Uses win32."""\n') is None
    assert ost.file_tier('class C: """Uses fcntl."""\n') is None
    assert ost.file_tier("if ok: 'run.exe'\n") == "W1"


def test_quoted_brackets_do_not_move_a_call_boundary() -> None:
    early_close = (
        "@pytest.mark.skipif(')' in label or sys.platform == 'win32', reason='x')\n"
        "def test_x():\n    pass\n"
    )
    assert ost.targets_windows("test/test_a.py", early_close)
    late_close = (
        "@pytest.mark.skipif('(' in label, reason='x')\ndef test_x():\n    assert sys.platform\n"
    )
    assert not ost.targets_windows("test/test_a.py", late_close)


def test_an_unclosed_string_stays_code() -> None:
    broken = "x = '''\nimport fcntl\n"
    assert ost.code_only(broken) == broken
    assert ost.file_tier(broken) == "W2"


def test_each_targeting_rule_and_the_collect_ignore_list(tree: Path) -> None:
    assert ost.windows_targeting(tree) == [
        "test/test_faked.py",
        "test/test_faked_flag.py",
        "test/test_marker.py",
        "test/test_skip.py",
        "test/test_windows_paths.py",
    ]


def test_bundle_writes_the_tables_of_its_root_or_leaves_them_out(
    tmp_path: Path, tree: Path
) -> None:
    test_map = ctm.merge_parts(
        [{"test/test_skip.py": {"nodes": 1, "files": ["src/kiro_crew/w1.py"]}}], commit="c"
    )
    tables = ("os_sensitive", "windows_targeting")
    doc, absent = ctm.bundle(test_map, tree)
    assert doc["os_sensitive"] == ost.os_sensitive(tree)
    assert doc["windows_targeting"] == ost.windows_targeting(tree)
    assert not set(tables) & set(absent)
    # A missing tree, and a tree whose scan finds nothing, leave the table out with
    # the reason rather than writing it empty.
    quiet = tmp_path / "quiet"
    (quiet / "src" / "kiro_crew").mkdir(parents=True)
    (quiet / "src" / "kiro_crew" / "plain.py").write_text("X = 1\n", encoding="utf-8")
    (quiet / "test").mkdir()
    (quiet / "test" / "test_plain.py").write_text("X = 1\n", encoding="utf-8")
    for root in (tmp_path / "nothing", quiet):
        doc, absent = ctm.bundle(test_map, root)
        assert not set(tables) & set(doc)
        assert all("found no" in absent[table] for table in tables)


def test_the_tables_change_no_selection_because_nothing_reads_them_yet(tree: Path) -> None:
    test_map = ctm.merge_parts(
        [{"test/test_skip.py": {"nodes": 1, "files": ["src/kiro_crew/w1.py"]}}], commit="c"
    )
    with_tables, _ = ctm.bundle(test_map, tree)
    tables = ("os_sensitive", "windows_targeting")
    assert set(tables) <= set(with_tables)
    without = {key: value for key, value in with_tables.items() if key not in tables}
    changes = [("M", ["src/kiro_crew/w1.py"])]
    guard = ["test/test_marker.py"]
    universe = [f"test/{name}" for name in TESTS if name.startswith("test_")]

    def answers(bundle: dict) -> tuple:
        cts.check_tables(bundle)
        verdict = cts.score(
            changes,
            bundle,
            universe,
            guard,
            skippable={"G"},
            narrow_reach_max=1,
            grep=lambda m: set(),
        )
        return ctm.select(changes, bundle, guard), verdict

    assert answers(with_tables) == answers(without)

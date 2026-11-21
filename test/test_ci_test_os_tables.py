"""Contract tests for scripts/ci_test_os_tables.py, the map's Windows tables.

Pinned on a fixture tree: one src file per tier, a pattern only a comment or a
docstring carries (not matched), each `windows_targeting` rule, a file Windows
never collects, and the absent-table case; plus `build` writing the tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ci_test_map as ctm
from scripts import ci_test_os_tables as ost

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


def test_each_src_file_takes_its_strongest_tier(tree: Path) -> None:
    assert ost.os_sensitive(tree) == {
        "src/kiro_crew/both.py": "W1",
        "src/kiro_crew/w1.py": "W1",
        "src/kiro_crew/w1_exe.py": "W1",
        "src/kiro_crew/w2.py": "W2",
        "src/kiro_crew/w3.py": "W3",
    }


def test_a_pattern_in_a_comment_or_docstring_is_not_matched() -> None:
    assert ost.tier_of(ost.code_only(SRC["comment_only.py"])) is None
    assert ost.tier_of(ost.code_only(SRC["docstring_only.py"])) is None
    # A string that is an operand, not a statement, is still code.
    assert ost.tier_of(ost.code_only("X = 'a.cmd'\n")) == "W1"


def test_a_string_beside_code_stays_code() -> None:
    # An argument on its own line, an implicit concatenation, a list item.
    source = "run(\n    'tool.exe'\n)\nX = (\n    'a'\n    'b.bat'\n)\nY = [\n    'c.cmd'\n]\n"
    assert ost.code_only(source) == source


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
    # A platform reference beside an unrelated patch call is not a faked platform.
    unrelated = (
        "def test_x(monkeypatch):\n    monkeypatch.setattr(mod, 'x', 1)\n    assert os.name\n"
    )
    assert not ost.targets_windows("test/test_a.py", unrelated)
    nearby = "with patch('mod.f'):\n    pass\nprint(sys.platform)\n"
    assert not ost.targets_windows("test/test_a.py", nearby)


def test_a_one_argument_replace_is_w3_and_a_string_replace_is_not() -> None:
    assert ost.tier_of("def move(src, dst):\n    return src.replace(dst)\n") == "W3"
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


def test_a_table_that_cannot_be_built_is_absent(tmp_path: Path, tree: Path) -> None:
    assert ost.os_tables(tmp_path / "nothing") == {}
    tables = ost.os_tables(tree)
    assert set(tables) == {"os_sensitive", "windows_targeting"}
    # A tree whose scan finds nothing leaves the table out rather than writing it empty.
    quiet = tmp_path / "quiet"
    (quiet / "src" / "kiro_crew").mkdir(parents=True)
    (quiet / "src" / "kiro_crew" / "plain.py").write_text("X = 1\n", encoding="utf-8")
    assert ost.os_tables(quiet) == {}


def test_build_writes_the_tables_of_its_root(tmp_path: Path, tree: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "part-gw0.json").write_text(
        json.dumps({"test/test_skip.py": {"nodes": 1, "files": ["src/kiro_crew/w1.py"]}}),
        encoding="utf-8",
    )
    out = tmp_path / "map.json"
    argv = ["--root", str(tree), "build", "--parts", str(parts), "--out", str(out)]
    assert ctm.main(argv) == 0
    built = ctm.load_map(out)
    assert built["os_sensitive"]["src/kiro_crew/w2.py"] == "W2"
    assert "test/test_skip.py" in built["windows_targeting"]

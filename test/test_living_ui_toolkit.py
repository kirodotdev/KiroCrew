"""Pin the living-ui skill's helper blocks to the toolkit file they are copied from.

``toolkit/living-ui.js`` is the one place the toolkit's code is edited. A crewmate
with no file tools reads the same code from SKILL.md, which carries each marked
helper as its own block, so a block that drifts from the file teaches an author
code that nothing tests. Each check here reads both sides through the block-copy
script, ``scripts/copy_living_ui_blocks.py``, the same code that writes the blocks.

What the helpers do, their minified size and their style neutrality are pinned in
``website/src/test/livingUiToolkit.test.ts``, which can run JavaScript.
"""

from __future__ import annotations

import configparser
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "copy_living_ui_blocks.py"
HELPERS = (
    "loop",
    "follow",
    "spring",
    "settle",
    "smooth",
    "burst",
    "tour",
    "readout",
    "focusOne",
    "explode",
    "css",
)
_REMEDY = "Run `python3 scripts/copy_living_ui_blocks.py` and commit SKILL.md."


@pytest.fixture(scope="module")
def copier() -> Any:
    return load_skill_script("copy_living_ui_blocks_under_test", SCRIPT)


@pytest.fixture(scope="module")
def blocks(copier: Any) -> list[tuple[str, str]]:
    return copier.read_blocks(copier.TOOLKIT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def region(copier: Any) -> str:
    return copier.split_region(copier.SKILL_MD.read_text(encoding="utf-8"))[1]


def test_the_file_marks_every_helper(blocks: list[tuple[str, str]]) -> None:
    names = [name for name, _ in blocks]
    missing = [name for name in HELPERS if name not in names]
    assert not missing, f"toolkit/living-ui.js marks no block for {missing}"


def test_each_skill_block_equals_its_marked_helper(
    copier: Any, blocks: list[tuple[str, str]], region: str
) -> None:
    copied = copier.copied_blocks(region)
    assert [name for name, _ in copied] == [name for name, _ in blocks], (
        "SKILL.md does not carry one block per marked helper, in file order. " + _REMEDY
    )
    for (name, code), (_, copy) in zip(blocks, copied):
        assert (
            copy == code
        ), f"the SKILL.md block for {name} differs from its marked helper. {_REMEDY}"


def test_the_region_is_exactly_what_the_script_writes(copier: Any) -> None:
    drifted = copier.check()
    assert not drifted, f"SKILL.md blocks are stale: {drifted}. {_REMEDY}"


def test_the_check_sees_a_one_character_drift(
    copier: Any, blocks: list[tuple[str, str]], region: str
) -> None:
    """Without this, a comparison that always passed would read the same as a clean tree."""
    name, code = blocks[1]
    edited = [(name, code.replace("=", "= ", 1)), *blocks[2:]]
    assert copier.drift([blocks[0], *edited], region) == [
        f"{name}: SKILL.md block differs from toolkit/living-ui.js"
    ]
    assert copier.drift(blocks[:-1], region) == [
        f"{blocks[-1][0]}: in SKILL.md but not marked in the file"
    ]


def test_a_block_is_copied_byte_for_byte(copier: Any) -> None:
    """A JavaScript string may hold U+2028 or a form feed; neither ends a copied line."""
    code = "const s = 'a\u2028b\x0cc';\n"
    source = f"// living-ui:begin a\n{code}// living-ui:end a\n"
    assert copier.read_blocks(source) == [("a", code)]
    assert copier.copied_blocks(copier.render([("a", code)])) == [("a", code)]


@pytest.mark.parametrize(
    ("source", "complaint"),
    [
        ("var x = 1;\n", "code outside a marked block"),
        ("// living-ui:begin a\nx();\n", "never closed"),
        ("// living-ui:begin a\nx();\n// living-ui:end b\n", "closes block 'a'"),
        ("// living-ui:begin a\n// living-ui:begin b\n", "inside block 'a'"),
        ("// living-ui:begin a\n\n// living-ui:end a\n", "is empty"),
        ("// living-ui:begin a\nx();\n// living-ui:end a\n" * 2, "more than once"),
        ("// only a comment\n", "no marked helper"),
    ],
    ids=["code-outside", "never-closed", "wrong-end", "nested", "empty", "twice", "none"],
)
def test_a_file_the_script_cannot_copy_whole_is_refused(
    copier: Any, source: str, complaint: str
) -> None:
    with pytest.raises(copier.BlockError, match=complaint):
        copier.read_blocks(source)


def test_the_toolkit_ships_in_the_package(copier: Any) -> None:
    """The wheel and sdist take ``builtin_skills`` files from setup.cfg's package data."""
    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "setup.cfg", encoding="utf-8")
    raw = cfg.get("options.package_data", "kiro_crew", fallback="")
    globs = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    rel = copier.TOOLKIT.relative_to(ROOT / "src" / "kiro_crew").as_posix()
    assert any(fnmatch(rel, glob) for glob in globs), f"no package-data glob ships {rel}"

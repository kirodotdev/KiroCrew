"""Every doc that enumerates ``DIRECTIVE_TOOLS`` must agree with the set — twice over.

Every member must be NAMED in one block. A list that omits a name lets real drift through,
and that is the drift this file exists for.

Nothing else reported it: ``Docs Lint`` checks index links only. A wheel-bundled copy
carried the enumeration too until the docs refresh removed that file, so the two repo docs
are what remains to keep in step.

Scoped to the ENUMERATION BLOCK rather than the whole file, because these docs also
discuss the tools in prose -- a whole-file check passes on a doc whose list omits a name
that appears in a nearby paragraph, which is the drift that matters. The block is found by
content, a maximal run of consecutive lines each naming a member, so there are no line
numbers to rot.

Membership is read off ``DIRECTIVE_TOOLS`` rather than re-spelled, so a member added later
is required everywhere without touching this file.
"""

import pathlib
import re

from kiro_crew.session_directive import DIRECTIVE_TOOLS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _enumerating_docs() -> dict[str, pathlib.Path]:
    return {
        "session spec": _REPO_ROOT / "docs" / "system-specs" / "modules" / "session.md",
        "mcp architecture": _REPO_ROOT / "docs" / "architecture" / "mcp.md",
    }


def _enumeration_blocks(text: str) -> list[str]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if any(name in line for name in DIRECTIVE_TOOLS):
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


class TestDirectiveToolsDocParity:
    def test_a_spelled_count_beside_an_enumeration_matches_the_set(self) -> None:
        """A restated count is the half of this drift a membership check cannot see: a
        list can name every member while the sentence introducing it still says seven.
        Silent when a doc spells no number, so a doc may drop the count entirely, but a
        doc that keeps one must keep it true."""
        words = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
        }
        expected = len(DIRECTIVE_TOOLS)
        checked = 0
        for label, path in _enumerating_docs().items():
            text = path.read_text(encoding="utf-8")
            for spelled in re.findall(
                r"\b([A-Za-z]+)\s+session-bound MCP tools", text, flags=re.IGNORECASE
            ):
                claimed = words.get(spelled.lower())
                if claimed is None:
                    continue
                checked += 1
                assert claimed == expected, (
                    f"{label} says {spelled!r} session-bound MCP tools while "
                    f"DIRECTIVE_TOOLS holds {expected}"
                )
        assert checked, (
            "no doc spells a count beside the phrase, so this test asserted nothing -- "
            "if the count was deliberately dropped, delete this test with it"
        )

    def test_the_spec_names_every_provenance_gated_directive(self) -> None:
        """The gate and the sentence describing it drifted apart once already: a member
        was added to the set while the spec still called it deliberately excluded, so the
        commit contradicted itself and the document of record was the wrong half. A
        membership check cannot see that; only reading the sentence can."""
        from kiro_crew.dashboard.session_directive_apply import _USER_ORIGIN_DIRECTIVES

        spec = _REPO_ROOT / "docs" / "system-specs" / "modules" / "session.md"
        text = spec.read_text(encoding="utf-8")
        sentences = [
            s for s in text.split("\n") if "additionally require structural user-turn" in s
        ]
        assert len(sentences) == 1, f"{len(sentences)} candidate sentences, expected 1"
        # The list ITSELF, captured immediately before the phrase: splitting the line
        # sweeps in earlier prose naming these tools, which passes while the list is short.
        run_of_names = re.search(
            r"((?:`[a-z_]+`(?:, | and ))+`[a-z_]+`) additionally require structural user-turn",
            sentences[0],
        )
        assert run_of_names, "could not find the provenance list in the spec sentence"
        claim = run_of_names.group(1)
        missing = sorted(name for name in _USER_ORIGIN_DIRECTIVES if f"`{name}`" not in claim)
        assert not missing, (
            f"the spec's provenance list omits {missing}; it names "
            f"{sorted(n for n in _USER_ORIGIN_DIRECTIVES if f'`{n}`' in claim)}"
        )
        # The other direction: a name dropped from the gate must leave the list too, or the
        # spec promises a refusal the code does not perform.
        stale = sorted(
            name
            for name in DIRECTIVE_TOOLS
            if f"`{name}`" in claim and name not in _USER_ORIGIN_DIRECTIVES
        )
        assert not stale, f"the spec claims {stale} are provenance-gated but the gate omits them"

    def test_the_shipped_prompt_offers_every_directive_tool_to_the_agent(self) -> None:
        # This file ships in the wheel and is what tells the agent a capability
        # exists, so a tool missing from it is unreachable, not just undocumented.
        prompt = _REPO_ROOT / "src" / "kiro_crew" / "config" / "prompt.md"
        assert prompt.is_file(), f"shipped prompt not found at {prompt}"
        text = prompt.read_text(encoding="utf-8")
        absent = sorted(name for name in DIRECTIVE_TOOLS if name not in text)
        assert not absent, (
            f"the shipped prompt never names {absent}, so the agent is not told "
            f"they exist; {len(DIRECTIVE_TOOLS) - len(absent)} siblings are named"
        )

    def test_every_provenance_gated_directive_says_so_in_the_shipped_prompt(self) -> None:
        """A gate the agent is not told about costs a refused call, not a defence.

        ``_USER_ORIGIN_DIRECTIVES`` refuses a headless turn even when it inherited an
        open tab, so a bullet that promises only "Dashboard sessions only" invites a
        call this build rejects. Membership is read off the applier rather than
        re-spelled, so a directive added to the gate later is required to say so here.
        """
        from kiro_crew.dashboard.session_directive_apply import _USER_ORIGIN_DIRECTIVES

        prompt = _REPO_ROOT / "src" / "kiro_crew" / "config" / "prompt.md"
        lines = prompt.read_text(encoding="utf-8").split("\n")
        silent: list[str] = []
        checked = 0
        for name in sorted(_USER_ORIGIN_DIRECTIVES):
            bullet = next((ln for ln in lines if ln.startswith(f"- `{name}`")), None)
            if bullet is None:
                continue
            checked += 1
            if "headless" not in bullet.lower():
                silent.append(name)
        # Positive control: the sweep has to actually find the bullets it grades.
        assert checked == len(_USER_ORIGIN_DIRECTIVES), (
            f"only {checked} of {len(_USER_ORIGIN_DIRECTIVES)} gated directives have a "
            f"bullet to grade, so this guard is not reading what it claims to"
        )
        assert not silent, (
            f"{silent} are refused for a headless caller but their prompt bullets never "
            f"say so, so an agent on an inherited tab is told to call and then refused"
        )

    def test_every_enumerating_doc_names_every_directive_tool_in_one_block(self) -> None:
        shortfall: dict[str, list[str]] = {}
        for label, path in _enumerating_docs().items():
            assert path.is_file(), f"{label} not found at {path}"
            blocks = _enumeration_blocks(path.read_text(encoding="utf-8"))
            assert blocks, f"{label} names no directive tool at all"
            best = min(
                blocks,
                key=lambda b: sum(1 for name in DIRECTIVE_TOOLS if name not in b),
            )
            absent = sorted(name for name in DIRECTIVE_TOOLS if name not in best)
            if absent:
                shortfall[label] = absent
        assert not shortfall, f"no single enumeration block names every directive: {shortfall}"

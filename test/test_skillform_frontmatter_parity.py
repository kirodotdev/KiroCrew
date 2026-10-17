"""Cross-language parity between the skill editor's block-scalar mirror and the
reader it simulates.

``website/src/components/SkillForm.tsx`` carries ``backendFoldsLiteral`` (and its
caller ``backendReadsValue``), a TypeScript reimplementation of the backend's
block-scalar fold PLUS the read path's block-collection boundary walk. The
structured skill editor runs it to decide whether a managed field is safe to
edit: it simulates what the Python reader returns for that field and keeps the
field editable only when the simulation matches what the YAML parser decodes. The
docstring and a hand-picked case list are the only other checks that the two
agree, and both pass just as happily once the two diverge -- a divergence in the
mirror that OVER-predicts what the reader keeps lets the form edit a field it then
corrupts on save. This guard makes the agreement executable:

1. It runs the REAL ``backendReadsValue`` exported from ``SkillForm.tsx`` -- which
   calls ``backendFoldsLiteral`` for the bare ``|`` family. The TypeScript is
   transpiled and executed under node at test time, never retyped here. A copy
   would be a second thing to keep in sync, the same defect one level up.
2. It compares against the READ PATH, ``parse_frontmatter(text, SKILL_LOADER)``,
   not ``fold_block_scalar`` alone. The mirror does its own boundary walk, so
   comparing folds only would let a collection mismatch pass. ``parse_frontmatter``
   runs the extractor, the boundary walk (``_parse_block_lines``) and the fold, so
   its output is the whole read path's idea of the value.
3. It GENERATES the corpus from named line kinds rather than storing a frozen
   blob or enumerating cases. A hand list covers only shapes someone thought of;
   generating from kinds surfaces an interaction the moment two kinds combine.
4. It pins the CAPABILITY BOUNDARY, not just the arithmetic. The mirror handles
   only the bare ``|`` family; ``>`` and explicit indents fall through to a
   refusal, which is safe. So it asserts AGREEMENT for bare literals and REFUSAL
   for the rest.

The harness crosses the node/Python boundary, so it needs both runtimes AND the
website node dependency tree (the ``yaml`` package the mirror imports, and esbuild
to transpile the component). It runs only where both are present: when a runtime
or the website deps are absent, it skips with a named reason rather than
half-guarding against a partial environment. Setting
``KIROCREW_SKILLFORM_PARITY_REQUIRE=1`` turns such a skip into a hard failure, so a
lane that is meant to run this guard cannot report green having run nothing. The
CI lane that sets that variable and runs this file is the ``e2e`` job -- the one
pull-request lane that installs both runtimes -- wired by a companion workflow
change; where that wiring is absent, this file runs locally and skips in lanes
that lack a runtime.

The generated corpus is a fixed product over the enumerated inputs (see
``_build_corpus``): deterministic, the same set on every run, and small enough to
run in a few seconds on a shared host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from itertools import product
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _REPO_ROOT / "website"
_COMPONENT = _WEBSITE / "src" / "components" / "SkillForm.tsx"
_NODE = shutil.which("node")

# When set (the e2e CI job sets it), a missing prerequisite is a HARD failure
# instead of a skip -- the lane that is meant to run this guard must not report
# green having skipped it. Everywhere else a genuine absence skips with a reason.
_REQUIRE = os.environ.get("KIROCREW_SKILLFORM_PARITY_REQUIRE") == "1"


def _prereqs_missing() -> str | None:
    """Return a human reason a prerequisite is absent, or ``None`` when all present."""
    if _NODE is None:
        return "node not on PATH"
    if not (_WEBSITE / "node_modules" / "esbuild").exists():
        return "website/node_modules/esbuild absent (run `npm ci` in website/)"
    if not (_WEBSITE / "node_modules" / "yaml").exists():
        return "website/node_modules/yaml absent (run `npm ci` in website/)"
    return None


def _skip_or_fail(reason: str) -> None:
    if _REQUIRE:
        pytest.fail(f"parity harness prerequisite missing but REQUIRE is set: {reason}")
    pytest.skip(reason)


# ── Corpus generation from NAMED LINE KINDS ──────────────────────────────────
#
# The corpus is a product of a block-scalar header times a sequence of body
# lines, where every body line is drawn from a named set of shapes that the fold
# and the boundary walk treat differently. Enumerating the KINDS -- rather than
# the documents -- is what makes the corpus cover shapes no one enumerated by
# hand: a new interaction appears the moment two kinds are combined.
#
# The block sits at indent 2 (``  ``), so each kind is described relative to that
# depth. The names are the vocabulary these tests describe cases by.
_BLOCK_INDENT = "  "  # two spaces: the block's content indent for the inferred case
_LINE_KINDS: dict[str, str] = {
    "empty": "",
    # Whitespace short of / at / past the block indent, judged in SPACES.
    "ws_short": " ",  # one space: shallower than the 2-space block
    "ws_at": "  ",  # exactly the block indent
    "ws_past": "    ",  # deeper than the block indent
    # A tab past the indent is two columns of space then a TAB OF CONTENT.
    "tab_past": "  \t",
    # A bare tab has no space indent at all, so it ends the scalar.
    "bare_tab": "\t",
    # Ordinary content at the block indent.
    "content": "  body",
    # Content carrying trailing spaces (content, not a break, once dedented).
    "content_trail": "  body   ",
    # More-indented content (nested structure the fold must keep).
    "more_indented": "    nested",
    # More-indented content with trailing spaces.
    "more_indented_trail": "    nested  ",
}

# The bare ``|`` family the mirror reproduces -> AGREEMENT is asserted.
_LITERAL_HEADERS = ["|", "|-", "|+"]
# Forms the mirror deliberately does NOT reproduce -> REFUSAL is asserted. ``>``
# and its chomps are the folded family, whose blank-line and indentation rules the
# mirror does not duplicate; ``|2``/``|2-`` are explicit indicators the backend
# resolves but the mirror refuses, declining an edit rather than guessing.
_REFUSED_HEADERS = ["|2", "|2-", ">", ">-", ">+"]

_MANAGED_KEY = "description"  # a managed field, so the form's read path applies


def _document(header: str, body_lines: list[str]) -> str:
    """Assemble a SKILL.md frontmatter document: one managed block-scalar field,
    a following ordinary key (so the boundary between the scalar and the next key
    is exercised), and a body."""
    lines = ["---", "name: s", f"{_MANAGED_KEY}: {header}"]
    lines.extend(body_lines)
    lines.extend(["repo_scope: x", "---", "", "# Body"])
    return "\n".join(lines) + "\n"


def _body_variants() -> list[list[str]]:
    """Body-line sequences drawn from the named kinds.

    One-line bodies cover every kind alone; two-line bodies cover every ordered
    pair, which is where a boundary decision (does line 2 end the scalar, or is it
    content?) actually bites. Two lines is enough to expose every fold/boundary
    interaction the mirror and the reader can disagree on while keeping the corpus
    small and the runtime in seconds; three-line growth is combinatorial and adds
    no new decision the pair does not already force.
    """
    kinds = list(_LINE_KINDS.values())
    singles = [[k] for k in kinds]
    pairs = [[a, b] for a, b in product(kinds, repeat=2)]
    return singles + pairs


def _build_corpus() -> list[dict]:
    """Every (header, body) combination, tagged with the expected disposition.

    ``expect`` is ``"agree"`` for the bare ``|`` family (the mirror reproduces it,
    so its value must equal the reader's) and ``"refuse"`` for the rest (the mirror
    declines, so the field must be non-editable and its value irrelevant).

    SIZE. The corpus is the fixed full product: the 8 headers times the body
    variants (each line kind alone, plus every ordered pair of kinds). That is the
    smallest set that forces every pairwise boundary decision -- line 2 either ends
    the scalar or is content, and which one depends on the kinds of BOTH lines. It
    is enumerated the same way on every run -- no sampling, no RNG -- and runs in a
    few seconds.
    """
    bodies = _body_variants()
    groups = [(h, "agree") for h in _LITERAL_HEADERS] + [(h, "refuse") for h in _REFUSED_HEADERS]

    corpus: list[dict] = []
    for header, expect in groups:
        for body in bodies:
            corpus.append({"header": header, "body": body, "expect": expect})
    return corpus


# ── The node driver: runs the REAL exported mirror over each document ─────────
#
# esbuild bundles a tiny driver that imports the exported functions from the real
# component and tree-shakes everything else (React, the UI kit, i18n) away, so the
# bundle depends only on the ``yaml`` package the functions use. Per document the
# driver reports two things, both from real component code:
#
#   value    -- what ``backendReadsValue`` (the read-side mirror, which runs
#               ``backendFoldsLiteral`` for the bare ``|`` family) takes the managed
#               field to be, or null. This is "the backend's idea of the value" as
#               the TypeScript reimplements it, and it is what the cross-language
#               comparison holds against the Python read path.
#   editable -- ``canEditStructured(raw)``, the actual product decision the form
#               makes. This is what pins the CAPABILITY BOUNDARY: a form that refuses
#               a shape it cannot faithfully simulate is safe; one that edits it is
#               the corrupting bug.
#
# The block split mirrors ``SkillForm.splitBlock`` so the pair handed to
# ``backendReadsValue`` is the exact one the form would build.
_DRIVER_TS = r"""
import { parseDocument, isMap, isScalar } from 'yaml'
import { backendReadsValue, canEditStructured } from __COMPONENT_SPEC__

// The same fence split SkillForm.splitBlock performs: block runs from just after
// the opening `---\n` to the newline that opens the closing fence.
function splitBlock(raw) {
  if (!raw.startsWith('---')) return null
  const end = raw.indexOf('\n---', 3)
  if (end === -1) return null
  return raw.slice(4, end)
}

// The TS reader's idea of the managed field's value: null when the mirror
// declines to simulate the shape (a `null` return from backendReadsValue), else
// the string it read.
function backendValueFor(raw, key) {
  const block = splitBlock(raw)
  if (block === null) return null
  const doc = parseDocument(block)
  if (!isMap(doc.contents)) return null
  for (const pair of doc.contents.items) {
    const k = isScalar(pair.key) ? String(pair.key.value ?? '') : ''
    if (k !== key) continue
    return backendReadsValue(block, pair)
  }
  return null
}

const input = JSON.parse(require('fs').readFileSync(0, 'utf8'))
const key = input.key
const out = input.docs.map(raw => ({
  value: backendValueFor(raw, key),
  editable: canEditStructured(raw),
}))
process.stdout.write(JSON.stringify(out))
"""


def _run_node_mirror(docs: list[str], tmp_path: Path) -> list[dict]:
    """Transpile the driver + real component with the website esbuild and run it
    once over the whole corpus, returning one result dict per document.

    The driver is written under pytest's ``tmp_path`` with a ``node_modules``
    symlink to ``website/node_modules`` beside it, so esbuild resolves the bare
    ``yaml`` import by walking up from the driver's own directory -- the way any
    website source file does -- while the scratch stays entirely outside the
    repository tree. The component is imported by its absolute path, JSON-encoded
    into the driver as a module specifier, so the real ``SkillForm.tsx`` is the
    thing bundled -- never a copy -- and the path is a valid string literal on any
    platform."""
    work = tmp_path / "parity-harness"
    work.mkdir(exist_ok=True)
    # esbuild resolves `yaml` by walking up from the driver's directory; a
    # node_modules directory link beside the driver points that walk at the
    # website deps without placing anything under website/ itself. make_dir_link
    # uses a junction on Windows (no elevation) and a symlink on POSIX.
    link = work / "node_modules"
    if not link.exists():
        make_dir_link(link, _WEBSITE / "node_modules")
    driver = work / "parity_driver.ts"
    driver.write_text(
        _DRIVER_TS.replace("__COMPONENT_SPEC__", json.dumps(str(_COMPONENT))),
        encoding="utf-8",
    )
    bundle = tmp_path / "parity_driver.cjs"

    # Resolve the esbuild launcher through shutil.which so Windows selects the
    # PATHEXT variant (esbuild.cmd / esbuild.exe) that npm installs in .bin rather
    # than the extensionless shell shim, which subprocess cannot execute there.
    bin_dir = _WEBSITE / "node_modules" / ".bin"
    esbuild = shutil.which("esbuild", path=str(bin_dir))
    if esbuild is None:
        _skip_or_fail("esbuild launcher not found in website/node_modules/.bin")
    # Bundle for the node platform: resolve `yaml` from website/node_modules and
    # tree-shake the component's React/UI imports (the driver references only
    # backendReadsValue/canEditStructured). --format=cjs so the driver's
    # require('fs') works.
    #
    # `--define:import.meta.env=...` supplies the Vite build-time constant that a
    # transitively-imported module reads at load (`import.meta.env.DEV`); Vite
    # replaces it at build, but a plain esbuild bundle run under node has no such
    # global, so it is defined here as a production-shaped object. This changes no
    # code under test -- the block-scalar mirror never reads it -- it only lets the
    # module graph load.
    try:
        build = subprocess.run(
            [
                str(esbuild),
                str(driver),
                "--bundle",
                "--platform=node",
                "--format=cjs",
                '--define:import.meta.env={"DEV":false,"PROD":true,"MODE":"production"}',
                f"--outfile={bundle}",
                "--log-level=warning",
            ],
            cwd=str(work),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - toolchain stall
        pytest.fail("esbuild timed out bundling the parity driver")
    if build.returncode != 0:
        pytest.fail(
            "esbuild failed to bundle the parity driver:\n"
            f"stdout: {build.stdout[-3000:]}\nstderr: {build.stderr[-3000:]}"
        )

    payload = json.dumps({"key": _MANAGED_KEY, "docs": docs})
    run = subprocess.run(
        [_NODE or "node", str(bundle)],
        input=payload,
        cwd=str(_WEBSITE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if run.returncode != 0:
        pytest.fail(
            "the node parity driver exited non-zero:\n"
            f"stdout: {run.stdout[-3000:]}\nstderr: {run.stderr[-3000:]}"
        )
    try:
        return json.loads(run.stdout)
    except ValueError:
        pytest.fail("the node parity driver produced no JSON:\n" + run.stdout[-3000:])
    raise AssertionError("unreachable")


# ── The Python oracle: the READ PATH's value ─────────────────────────────────


def _reader_value(raw: str) -> str | None:
    """What ``parse_frontmatter`` (the whole read path) takes the managed field to
    be, or ``None`` when the block does not parse to that field at all.

    ``parse_frontmatter`` runs the extractor, the block-collection boundary walk
    (``_parse_block_lines``) and the fold (``fold_block_scalar``), so its output is
    the entire read path's idea of the value -- which is why the comparison is
    against it and not against ``fold_block_scalar`` alone (the mirror does its own
    boundary walk, so a fold-only comparison would let a boundary mismatch pass).

    The mirror's ``backendReadsValue`` returns the fold WITH its terminating
    newline for a block scalar (the form strips one trailing ``\n`` only later, when
    it compares against the parsed YAML node's text). ``parse_frontmatter`` keeps
    that terminator too, so the two are already in the same space and compare
    directly.
    """
    fields = parse_frontmatter(raw, SKILL_LOADER)
    return fields.get(_MANAGED_KEY)


@pytest.fixture(scope="module")
def _mirror_results(tmp_path_factory) -> dict[str, dict]:
    """Run the node mirror once over the whole generated corpus; index the results
    by document text so each test case can look up its own."""
    reason = _prereqs_missing()
    if reason is not None:
        _skip_or_fail(reason)
    corpus = _build_corpus()
    docs = [_document(c["header"], c["body"]) for c in corpus]
    tmp_path = tmp_path_factory.mktemp("skillform-parity")
    results = _run_node_mirror(docs, tmp_path)
    assert len(results) == len(docs), "driver returned a different count than sent"
    return {doc: res for doc, res in zip(docs, results)}


def _case_id(case: dict) -> str:
    kind_names = {v: k for k, v in _LINE_KINDS.items()}
    body = "+".join(kind_names.get(line, repr(line)) for line in case["body"])
    return f"{case['header']}::{body}::{case['expect']}"


@pytest.mark.parametrize("case", _build_corpus(), ids=_case_id)
def test_mirror_matches_the_read_path(case: dict, _mirror_results: dict[str, dict]) -> None:
    """For every generated document, the TypeScript mirror agrees with the Python
    read path on the bare ``|`` family, and declines (so the form refuses to edit)
    everything else -- the capability boundary the mirror deliberately keeps."""
    raw = _document(case["header"], case["body"])
    mirror = _mirror_results[raw]
    reader = _reader_value(raw)

    if case["expect"] == "agree":
        # The mirror must read the SAME value the Python read path does. This is
        # the parity claim, and the ONLY thing this guard asserts on the agree
        # side: two independent implementations of "what the backend reads" must
        # not drift. If this fails on a bare literal, the mirror over- or
        # under-predicts what the reader keeps, and the dangerous direction (an
        # over-prediction the form then edits and corrupts) shows up here as a
        # value that diverges from the read path.
        #
        # It deliberately does NOT assert ``canEditStructured`` here. Editability
        # is gated by several rules ORTHOGONAL to the fold -- a bare tab in the
        # block, a comment on a managed line, a root-indented mapping -- any of
        # which can refuse a document whose managed field the mirror read
        # perfectly. Folding those in would make this guard fail for reasons that
        # are not a mirror/reader disagreement, which is not what it is for.
        assert mirror["value"] == reader, (
            "TS mirror and Python read path disagree on a bare literal.\n"
            f"document: {raw!r}\nmirror:   {mirror['value']!r}\nreader:   {reader!r}"
        )
    else:
        # The capability boundary: the mirror does NOT reproduce the folded family
        # or explicit indicators, so its read differs from the Python read path
        # (which resolves them), and the form must REFUSE to edit the field rather
        # than rewrite a value it did not faithfully simulate.
        assert mirror["editable"] is False, (
            "the form allowed editing a block-scalar form the mirror does not "
            f"reproduce (capability boundary breached): {raw!r} -> "
            f"mirror value {mirror['value']!r}, reader value {reader!r}"
        )
        assert mirror["value"] != reader, (
            "the mirror is supposed to DECLINE this shape, yet its value equals the "
            f"read path's, so the refusal is not conservative: {raw!r}"
        )

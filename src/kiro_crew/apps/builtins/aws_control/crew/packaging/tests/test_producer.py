"""Tests for the crew bundle producer.

Two things need proving: the four-entry bundle comes out in the right shape, and
the deny-by-default guards actually refuse. The guards are the part whose failure
ships a credential, so each one is MUTATION-tested: the guard's source line is
disabled in an exec-loaded copy of the module and the same scenario is shown to
leak, proving the guard is load-bearing rather than decorative.

The module is loaded by exec-ing its file under a throwaway name rather than
``import packaging`` -- the environment also carries the unrelated PyPA ``packaging``
distribution, and a top-level ``import packaging`` would collide. The CLI end-to-end
test runs ``python -m packaging.build`` in a subprocess whose cwd is the crew root,
where this directory's ``packaging`` shadows the site-packages one for that child
only. That cwd is the driver's contract, not a test convenience: see
``smc-deploy.sh``'s ``cd "$CREW_ROOT" && "$py" -m packaging.build``.

Run only this file:
    python -m pytest \
        src/kiro_crew/apps/builtins/aws_control/crew/packaging/tests/test_producer.py -q
"""

from __future__ import annotations

import ast
import functools
import gc
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)

# The crew root: the directory `python -m packaging.build` must run in.
CREW_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = CREW_ROOT / "packaging" / "build.py"
PIPELINE_DIR = CREW_ROOT / "packaging" / "pipeline"


def builder_sources() -> tuple[Path, ...]:
    """Every file the builder is made of: the ``build.py`` facade and each pipeline owner.

    A source rule about "the builder" reads all of them. ``build.py`` alone was the whole
    builder once, and a rule still reading only it would pass while the code it is about
    lived next door.
    """
    return (BUILD_PY, *sorted(PIPELINE_DIR.glob("*.py")))


def builder_source_text() -> str:
    """The builder's source, every file of it, for a presence or count rule."""
    return "\n".join(path.read_text(encoding="utf-8") for path in builder_sources())


def transaction_source() -> str:
    """The source of the owner that defines the bundle transaction, for an ordering rule."""
    return source_defining("build_bundle").read_text(encoding="utf-8")


def called_name(call: ast.Call) -> str:
    """The name a call invokes, bare (``f(...)``) or through its owner (``_owner.f(...)``).

    An owner calls a function another owner defines through that owner's module, so a
    source rule looking for a call has to accept both spellings of it.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def builder_trees() -> list[tuple[Path, ast.Module]]:
    """Each builder file with its parsed tree."""
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), str(path))) for path in builder_sources()
    ]


def source_defining(name: str) -> Path:
    """The builder file whose top level defines the function or class *name*."""
    for path in builder_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
                return path
    raise AssertionError(f"no builder file defines {name}")


def _child_env() -> dict:
    """Environment for the CLI subprocess that makes ``packaging`` importable
    WITHOUT running the child in the source tree.

    The child runs ``python -m packaging.build`` and needs the crew root on the
    import path. Running with ``cwd=CREW_ROOT`` would give it that too, which
    made the interpreter write ``__pycache__`` directories into the source tree
    (the residue outlived the test). Putting CREW_ROOT on ``PYTHONPATH`` resolves
    the module identically while letting the child run from a temp cwd, so any
    bytecode it writes lands under that temp dir and is reclaimed with it.

    CREW_ROOT is PREPENDED so this directory's ``packaging`` shadows the unrelated
    PyPA ``packaging`` distribution for the child, the same precedence the old
    cwd gave. ``PYTHONDONTWRITEBYTECODE`` is a belt-and-braces second guard: even
    the temp-cwd imports write no ``.pyc`` at all.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(CREW_ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# A synthetic AWS key shape -- not a real credential, built to match the pattern
# and nothing else, so the scanner has something to fire on.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"[4:] + "ABCD"

_variant_counter = 0


@functools.lru_cache(maxsize=128)
def _compiled(path: str, text: bytes) -> types.CodeType:
    """One compile per distinct source text: an unmutated owner is shared across variants.

    A code object is immutable, so running the same one as two modules gives each its own
    functions and globals; only the compile is shared.
    """
    return compile(text, path, "exec", dont_inherit=True)


class _VariantLoader(importlib.abc.SourceLoader):
    """Serve one builder file's (possibly mutated) text as that file's source.

    The import system compiles and runs it, as it runs any source file; this loader only
    decides what the source IS. That is the mechanism a mutation test needs, and its input
    is not attacker-reachable: the text is this repository's own builder source, read from
    paths derived from ``__file__``, optionally with one substring swapped by a literal pair
    written in this suite. Importing the builder normally cannot replace its constant
    strings, and patching the functions afterwards would test the patch rather than the
    guard, so a mutation test of a module-level guard has to load a variant of the source.

    ``get_filename`` answers the REAL path, so tracebacks and coverage name the builder's own
    files, and no bytecode is read or written: ``path_stats`` is not implemented, which is
    how :class:`importlib.abc.SourceLoader` is told there is no cache to use.
    """

    def __init__(self, path: Path, text: str, is_package: bool) -> None:
        self._path = path
        self._source = text.encode("utf-8")
        self._is_package = is_package

    def get_filename(self, fullname: str) -> str:
        return str(self._path)

    def get_data(self, path: str) -> bytes:
        if path != str(self._path):
            raise OSError(f"{path} is not this variant's source")
        return self._source

    def is_package(self, fullname: str) -> bool:
        return self._is_package

    def source_to_code(self, data, path, *, _optimize=-1):  # type: ignore[override]
        return _compiled(str(path), bytes(data))


class _VariantFinder(importlib.abc.MetaPathFinder):
    """Serve one variant of the builder package under its own throwaway root name."""

    def __init__(self, root: str, files: dict[str, tuple[Path, str, bool]]) -> None:
        self._root = root
        self._files = files

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: Any = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname == self._root:
            key = ""
        elif fullname.startswith(self._root + "."):
            key = fullname[len(self._root) + 1 :]
        else:
            return None
        entry = self._files.get(key)
        if entry is None:
            return None
        file, text, is_package = entry
        spec = importlib.util.spec_from_loader(
            fullname,
            _VariantLoader(file, text, is_package),
            origin=str(file),
            is_package=is_package,
        )
        assert spec is not None
        spec.has_location = True
        return spec


#: ``(root, test)`` for every builder copy still registered: its ``smc_build_vN`` root and the
#: test that loaded it, as ``PYTEST_CURRENT_TEST`` names it (empty outside pytest).
_REGISTERED_COPIES: list[tuple[str, str]] = []


def _current_test() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "").rpartition(" ")[0]


def _drop_variant(root: str) -> None:
    for name in [n for n in list(sys.modules) if n == root or n.startswith(root + ".")]:
        sys.modules.pop(name, None)


def release_builder_copies(*, finished_only: bool = False) -> None:
    """Drop builder copies' modules from ``sys.modules``: every copy, or only finished tests'.

    Called by ``load_build`` itself with ``finished_only``, so a copy lives exactly as long as
    the test that loaded it plus the gap until the next copy is loaded, and its entries are
    removed at that deterministic point -- never from a garbage-collection callback, which
    would change ``sys.modules`` under whatever test happened to be running then.
    """
    current = _current_test()
    keep = []
    for root, test in _REGISTERED_COPIES:
        if finished_only and test == current:
            keep.append((root, test))
        else:
            _drop_variant(root)
    _REGISTERED_COPIES[:] = keep


def load_build(
    mutate: "tuple[str, str] | list[tuple[str, str]] | None" = None,
) -> types.ModuleType:
    """Load a throwaway copy of the builder package and return its ``build`` facade.

    ``mutate`` is an ``(old, new)`` substring pair -- or a list of them applied in order --
    swapped into the source before it runs, so a test can disable one guard (or a set of
    guards that must fall together) and observe the leak they prevent. An anchor has to
    appear in exactly ONE builder file, and its first occurrence there is replaced: an
    anchor two owners share would mutate whichever one this loader happened to search first.

    The copy is a whole package under a unique ``smc_build_vN`` root -- the facade, the
    ``pipeline`` owners and the package ``__init__`` files -- because the facade resolves
    each name from its owner through ``sys.modules`` and an owner reaches another through
    its package. So the copy stays registered while the test that loaded it runs, and the
    next ``load_build`` call from another test removes it: a mutated copy never reaches a
    later test through ``sys.modules``.
    """
    release_builder_copies(finished_only=True)
    global _variant_counter
    _variant_counter += 1
    root = f"smc_build_v{_variant_counter}"
    files: dict[str, tuple[Path, str, bool]] = {
        "": (CREW_ROOT / "packaging" / "__init__.py", "", True),
        "build": (BUILD_PY, "", False),
        "pipeline": (PIPELINE_DIR / "__init__.py", "", True),
    }
    owners = sorted(p.stem for p in PIPELINE_DIR.glob("*.py") if p.stem != "__init__")
    for leaf in owners:
        files[f"pipeline.{leaf}"] = (PIPELINE_DIR / f"{leaf}.py", "", False)
    files = {
        key: (path, path.read_text(encoding="utf-8"), is_package)
        for key, (path, _text, is_package) in files.items()
    }
    if mutate is not None:
        pairs = [mutate] if isinstance(mutate, tuple) else list(mutate)
        for old, new in pairs:
            holders = [key for key, (_p, text, _pkg) in files.items() if old in text]
            assert holders, f"mutation anchor not found: {old!r}"
            assert len(holders) == 1, (
                f"mutation anchor {old!r} appears in {holders}; extend it until it names one "
                f"builder file"
            )
            path, text, is_package = files[holders[0]]
            files[holders[0]] = (path, text.replace(old, new, 1), is_package)
    finder = _VariantFinder(root, files)
    sys.meta_path.insert(0, finder)
    try:
        mod = importlib.import_module(f"{root}.build")
        # Every owner, while this finder can still serve it: the facade imports each one as
        # it loads, and importing them here too keeps the copy whole if a mutation stops it.
        for leaf in owners:
            importlib.import_module(f"{root}.pipeline.{leaf}")
    except BaseException:
        _drop_variant(root)
        raise
    finally:
        sys.meta_path.remove(finder)
    _REGISTERED_COPIES.append((root, _current_test()))
    return mod


def patch_builder_global(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType, name: str, value: object
) -> None:
    """Replace a global the builder's modules each import for themselves, in every one.

    The facade forwards a write only for a name an owner DEFINES. A standard-library
    module such as ``os`` is bound separately in each owner that imports it, so replacing
    "the builder's ``os``" is a write into each of those modules, undone by *monkeypatch*.
    """
    for module in (mod, *builder_owners(mod)):
        if name in vars(module):
            monkeypatch.setattr(module, name, value)


def builder_owners(mod: types.ModuleType) -> list[types.ModuleType]:
    """The pipeline owners of the builder copy *mod* is the facade of."""
    prefix = f"{mod.__package__}.pipeline."
    return [module for name, module in sorted(sys.modules.items()) if name.startswith(prefix)]


# ---------------------------------------------------------------------------
# fixtures: a crew source (agents/<name>.json + skills/) the producer reads
# ---------------------------------------------------------------------------
def make_crew(
    root: Path,
    name: str = "frontdesk",
    *,
    prompt: str = "You are the front desk. Answer questions about hours and location.",
    tools: list | None = None,
    allowed_tools: list | None = None,
    mcp_servers: dict | None = None,
    skills: dict[str, dict[str, str]] | None = None,
) -> Path:
    """Write a crew home and return it. ``skills`` maps skill id -> {filename: text}."""
    spec: dict = {"name": name, "prompt": prompt}
    if tools is not None:
        spec["tools"] = tools
    if allowed_tools is not None:
        spec["allowedTools"] = allowed_tools
    if mcp_servers is not None:
        spec["mcpServers"] = mcp_servers
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    skills_root = root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for sid, files in (skills or {}).items():
        d = skills_root / sid
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (d / fname).write_text(text, encoding="utf-8")
    return root


def sign_plan(
    mod: types.ModuleType,
    # The crew source the exec-loaded module builds; `Any` because its class is
    # defined inside that throwaway module and has no name to annotate against.
    crew: Any,
    agent_spec: dict,
    out: Path,
    *,
    select: dict[str, set[str]] | None = None,
    reviewed_by: str = "someone",
    reviewed_at: str = "2026-09-03T00:00:00+00:00",
) -> Path:
    """Write a fresh plan, flip the chosen ids to include, sign it, return its path."""
    candidates = mod.enumerate_all(crew, agent_spec)
    plan_path = out / mod.PLAN_FILENAME
    # ``write_plan`` now claims the name exclusively and will NOT regenerate over an existing
    # plan (the no-replace-on-creation rule). This fixture rebuilds a fresh signed plan on
    # every call -- often over the same ``out`` across two builds -- so it clears any prior
    # plan first rather than relying on ``write_plan`` to overwrite.
    if plan_path.exists() or plan_path.is_symlink():
        plan_path.unlink()
    mod.write_plan(plan_path, crew.name, candidates)
    doc = json.loads(plan_path.read_text())
    doc["reviewed_by"] = reviewed_by
    doc["reviewed_at"] = reviewed_at
    for kind, ids in (select or {}).items():
        for entry in doc.get(kind, []):
            if entry["id"] in ids:
                entry["include"] = True
    plan_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# shape: the four-entry layout
# ---------------------------------------------------------------------------
@_posix_only
def test_empty_bundle_is_valid_and_well_shaped(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    report = mod.build_bundle(crew, spec, cands, None, out)  # no plan => deny-all

    assert (out / "manifest.json").is_file()
    assert (out / "agent.json").is_file()
    assert (out / "mcp.json").is_file()
    assert (out / "skills").is_dir()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["crew_name"] == "frontdesk"
    assert manifest["bundle_version"] == mod.BUNDLE_VERSION
    assert manifest["digest"].startswith("sha256:")
    assert report.skill_count == 0
    # the skill did not ship, and the owner can see why
    assert any(d["id"] == "faq" and "deny-by-default" in d["reason"] for d in report.denied)


@_posix_only
def test_agent_name_forced_to_crew_name(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", name="frontdesk")
    # spec on disk claims a different name
    spec_path = src / "agents" / "frontdesk.json"
    doc = json.loads(spec_path.read_text())
    doc["name"] = "my-local-crew"
    spec_path.write_text(json.dumps(doc))
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)
    assert json.loads((out / "agent.json").read_text())["name"] == "frontdesk"


@_posix_only
def test_digest_matches_source_algorithm(tmp_path):
    """Digest is sha256 over sorted [rel, sha256(bytes)] rows, manifest excluded,
    'sha256:'-prefixed -- the algorithm ported from crew_export/bundle.py."""
    mod = load_build()
    src = make_crew(tmp_path / "home")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)

    import hashlib

    rows = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.relative_to(out).as_posix() != "manifest.json":
            rows.append([p.relative_to(out).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    expected = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    assert json.loads((out / "manifest.json").read_text())["digest"] == expected


# ---------------------------------------------------------------------------
# GUARD 1: deny-by-default -- a fresh (even signed) plan ships nothing
# ---------------------------------------------------------------------------
@_posix_only
def test_signed_plan_selecting_nothing_ships_nothing(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    plan_path = sign_plan(mod, crew, spec, out, select=None)  # signed, nothing chosen
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    report = mod.build_bundle(crew, spec, cands, plan, out)
    assert report.skill_count == 0


@_posix_only
def test_MUTATION_deny_by_default(tmp_path):
    """Disable the include filter in Plan.included; a signed-but-empty plan now
    leaks every skill. Mutation: drop the `if on` filter so all entries count as
    included."""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"

    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    cands = good.enumerate_all(crew, spec)
    plan_path = sign_plan(good, crew, spec, out, select=None)
    assert (
        good.build_bundle(
            crew, spec, cands, good.merge_plans([plan_path], "frontdesk"), out
        ).skill_count
        == 0
    )

    bad = load_build(
        mutate=(
            "return {cid for cid, on in self.selections.get(kind, {}).items() if on}",
            "return {cid for cid, on in self.selections.get(kind, {}).items()}",
        )
    )
    out2 = tmp_path / "bundle2"
    plan_path2 = sign_plan(bad, crew, spec, out2, select=None)
    leaked = bad.build_bundle(
        crew, spec, bad.enumerate_all(crew, spec), bad.merge_plans([plan_path2], "frontdesk"), out2
    )
    assert leaked.skill_count == 1, "mutation must leak the unselected skill"


# ---------------------------------------------------------------------------
# GUARD 2: the signature -- an unsigned plan that selects is refused
# ---------------------------------------------------------------------------
@_posix_only
def test_unsigned_plan_that_selects_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(
        mod, crew, spec, out, select={"skills": {"faq"}}, reviewed_by="", reviewed_at=""
    )
    with pytest.raises(mod.ExportRefused, match="unreviewed"):
        mod.merge_plans([plan_path], "frontdesk")


@_posix_only
def test_MUTATION_signature(tmp_path):
    """Disable the is_signed check in verify; an unsigned selection now passes.
    (merge_plans also gates unsigned selections, so the mutation targets both the
    verify signature line and the merge signature line.)"""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    cands = good.enumerate_all(crew, spec)
    plan_path = sign_plan(
        good, crew, spec, out, select={"skills": {"faq"}}, reviewed_by="", reviewed_at=""
    )
    with pytest.raises(good.ExportRefused):
        good.merge_plans([plan_path], "frontdesk")

    bad = load_build(
        mutate=(
            "if plan.selects_anything() and not plan.is_signed():",
            "if False and plan.selects_anything() and not plan.is_signed():",
        )
    )
    # merge stops refusing; verify would still catch it -- unless verify's own
    # signature line is also disabled, which is the real guard under test here.
    bad2 = load_build(mutate=("if not plan.is_signed():", "if False:"))
    plan = bad.merge_plans([plan_path], "frontdesk")  # no raise now
    # feed the (unsigned) merged plan through the verify whose signature check is off
    drift = bad2.verify(plan, "frontdesk", cands)
    assert drift is not None, "mutation must let an unsigned plan pass verify"


# ---------------------------------------------------------------------------
# GUARD 3: the content pin -- a skill edited after approval is refused
# ---------------------------------------------------------------------------
@_posix_only
def test_changed_selected_skill_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ v1"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select={"skills": {"faq"}})
    # edit the skill AFTER it was reviewed
    (src / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2 (tampered)", encoding="utf-8")
    plan = mod.merge_plans([plan_path], "frontdesk")
    with pytest.raises(mod.ExportRefused, match="changed after it was approved"):
        mod.verify(plan, "frontdesk", mod.enumerate_all(crew, spec))


@_posix_only
def test_MUTATION_content_pin(tmp_path):
    """Disable the pin comparison in verify; a laundered (edited) skill now passes."""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ v1"}})
    out = tmp_path / "bundle"
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    plan_path = sign_plan(good, crew, spec, out, select={"skills": {"faq"}})
    (src / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2 (tampered)", encoding="utf-8")
    plan = good.merge_plans([plan_path], "frontdesk")
    with pytest.raises(good.ExportRefused, match="changed after it was approved"):
        good.verify(plan, "frontdesk", good.enumerate_all(crew, spec))

    bad = load_build(
        mutate=(
            "if pinned != candidate.content_hash:",
            "if False and pinned != candidate.content_hash:",
        )
    )
    plan2 = bad.merge_plans([plan_path], "frontdesk")
    drift = bad.verify(plan2, "frontdesk", bad.enumerate_all(crew, spec))  # no raise
    assert drift is not None, "mutation must let laundered content pass verify"


# ---------------------------------------------------------------------------
# GUARD 4: credential content scan -- a secret refuses the build
# ---------------------------------------------------------------------------
@_posix_only
def test_skill_with_credential_is_blocked_and_refused(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"leaky": {"SKILL.md": f"# Leaky\nkey = {FAKE_AWS_KEY}\n"}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    faq = next(c for c in cands["skills"] if c.id == "leaky")
    assert faq.blocked, "a skill carrying a credential must be blocked"
    plan_path = sign_plan(mod, crew, spec, out, select={"skills": {"leaky"}})
    with pytest.raises(mod.ExportRefused, match="cannot be included"):
        mod.verify(mod.merge_plans([plan_path], "frontdesk"), "frontdesk", cands)


@_posix_only
def test_MUTATION_credential_scan(tmp_path):
    """Disable scan_text; nothing then blocks the credential skill and it would ship."""
    src = make_crew(
        tmp_path / "home",
        skills={"leaky": {"SKILL.md": f"# Leaky\nkey = {FAKE_AWS_KEY}\n"}},
    )
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    assert next(c for c in good.enumerate_all(crew, spec)["skills"] if c.id == "leaky").blocked

    # Anchored on the scan LOOP, so one edit disables the whole function -- which is
    # what this test's name claims. The original mutation flipped the `if m:` inside
    # the `_HARD_PATTERNS` loop, and once a second layer (the canonical detector) was
    # added the scanner kept blocking through it. That is the layering working, so the
    # mutation grew instead of the layer being dropped to keep an old test green.
    #
    # It grew a second time for the same reason, when the encoded-credential layer arrived:
    # the redactor runs over the whole text rather than per line, so emptying the line loop
    # stops reaching it. Each growth is evidence the layers are independent, which is the
    # property that makes them worth having.
    bad = load_build(
        mutate=(
            "    for lineno, line in enumerate(text.splitlines(), start=1):",
            "    return leaks\n    for lineno, line in enumerate(text.splitlines(), start=1):",
        )
    )
    leaky = next(c for c in bad.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert not leaky.blocked, "mutation must stop the scanner blocking a credential skill"


# ---------------------------------------------------------------------------
# GUARD 5: credential-store filename refusal
# ---------------------------------------------------------------------------
@_posix_only
def test_skill_with_env_file_is_blocked(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"withenv": {"SKILL.md": "# ok", ".env": "SECRET=hunter2"}},
    )
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "withenv")
    assert leaky.blocked and "credential store" in leaky.blocked


@_posix_only
def test_MUTATION_credential_filename(tmp_path):
    """Disable refused_by_name; nothing then blocks a skill carrying a .env."""
    src = make_crew(
        tmp_path / "home",
        skills={"withenv": {"SKILL.md": "# ok", ".env": "SECRET=hunter2"}},
    )
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    assert next(c for c in good.enumerate_all(crew, spec)["skills"] if c.id == "withenv").blocked

    bad = load_build(
        mutate=(
            "return bool(_CREDENTIAL_NAME_RE.match(path.name))",
            "return False",
        )
    )
    leaky = next(c for c in bad.enumerate_all(crew, spec)["skills"] if c.id == "withenv")
    assert not leaky.blocked, "mutation must stop the name gate blocking a .env skill"


# ---------------------------------------------------------------------------
# GUARD 5b: credential-store LOCATION refusal (nested credential directory).
#
# refused_by_name only fires on a FILE whose basename looks like a credential.
# A skill carrying `.aws/config` or `.ssh/known_hosts` has an innocent basename
# (`config`, `known_hosts`) and, before this fix, sailed through the name-only
# gate at both the enumeration site (skill_candidates) and the copy site
# (_copy_skill) and would be written into a bundle handed to an untrusted agent.
# Both sites must apply refused_by_location too.
# ---------------------------------------------------------------------------
def _add_nested_cred(src: Path, skill_id: str, relpath: str, body: str) -> Path:
    """Write a file at skills/<skill_id>/<relpath> under a crew source. Returns it."""
    p = src / "skills" / skill_id / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@_posix_only
def test_skill_with_nested_aws_config_is_blocked_by_location(tmp_path):
    # `.aws/config` -- basename `config` is innocent, so only the LOCATION half
    # catches it. Content is deliberately benign so the scan_text pass cannot be
    # what blocks it; the location gate must.
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok"}})
    _add_nested_cred(src, "leaky", ".aws/config", "[default]\nregion = us-east-1\n")
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert leaky.blocked, "a skill with a nested .aws/ dir must be blocked"
    assert ".aws/config" in leaky.blocked


@_posix_only
def test_nested_credential_file_is_absent_from_the_output_bundle(tmp_path):
    # The property that matters: the credential file does not reach the bundle.
    # The skill is blocked, so selecting it is refused and no bundle is written;
    # assert on the OUTPUT, not merely that a refusal was raised.
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok"}})
    _add_nested_cred(src, "leaky", ".ssh/known_hosts", "example.com ssh-rsa AAAA...\n")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    plan_path = sign_plan(mod, crew, spec, out, select={"skills": {"leaky"}})
    with pytest.raises(mod.ExportRefused, match="cannot be included"):
        mod.verify(mod.merge_plans([plan_path], "frontdesk"), "frontdesk", cands)
    # No bundle was written, so the credential file is nowhere under the output.
    leaked = [p for p in out.rglob("known_hosts")] if out.exists() else []
    assert not leaked, f"credential file leaked into the bundle: {leaked}"


def test_copy_skill_refuses_a_nested_credential_directory(tmp_path):
    # Site 938 directly: even if a skill reached the copy step, _copy_skill must
    # refuse a file inside a credential directory before reading it.
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok"}})
    _add_nested_cred(src, "leaky", ".aws/config", "[default]\nregion = us-east-1\n")
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(mod.ExportRefused, match="credential directory"):
        mod._copy_skill(src / "skills" / "leaky", "leaky", dest)
    # Nothing from the skill was written on the way to the refusal.
    assert not [p for p in dest.rglob("config")]


@_posix_only
def test_MUTATION_credential_location_enumeration(tmp_path):
    """Disable the location half in skill_candidates; the nested-cred skill is no
    longer blocked and would become selectable."""
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok"}})
    _add_nested_cred(src, "leaky", ".aws/config", "[default]\nregion = us-east-1\n")
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    assert next(c for c in good.enumerate_all(crew, spec)["skills"] if c.id == "leaky").blocked

    bad = load_build(
        mutate=(
            "and (_sensitive.refused_by_name(p) or _sensitive.refused_by_location(p))",
            "and (_sensitive.refused_by_name(p))",
        )
    )
    leaky = next(c for c in bad.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert not leaky.blocked, "mutation must stop the location gate blocking a nested-cred skill"


def test_MUTATION_credential_location_copy(tmp_path):
    """Disable the location half in _copy_skill; the nested credential file would
    be copied into the bundle instead of refused."""
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok"}})
    _add_nested_cred(src, "leaky", ".aws/config", "[default]\nregion = us-east-1\n")
    dest = tmp_path / "dest"
    dest.mkdir()

    bad = load_build(mutate=("        if _sensitive.refused_by_location(p):", "        if False:"))
    # With the guard disabled the innocent-content file is copied through
    # _write_guarded (its bytes match no _HARD_PATTERNS entry), proving the
    # location gate is the only thing standing between it and the bundle.
    bad._copy_skill(src / "skills" / "leaky", "leaky", dest)
    assert [p for p in dest.rglob("config")], "mutation must let the nested cred file ship"


# ---------------------------------------------------------------------------
# spec normalisation
# ---------------------------------------------------------------------------
@_posix_only
def test_file_prompt_without_target_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file:///gone/persona.md")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused, match="persona"):
        mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


@_posix_only
def test_missing_prompt_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="   ")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused, match="no prompt"):
        mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


@_posix_only
def test_orphan_tool_ref_dropped_when_server_not_selected(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        tools=["@internal-tools/query", "@builtin", "fs_read"],
        allowed_tools=["@internal-tools/query", "fs_read"],
        mcp_servers={"internal-tools": {"command": "/usr/local/bin/internal", "args": []}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    # do not select the MCP server -> its @ref is an orphan and must be dropped
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)
    agent = json.loads((out / "agent.json").read_text())
    assert "@internal-tools/query" not in agent["tools"]
    assert "@builtin" in agent["tools"]  # native group survives
    assert "@internal-tools/query" not in agent["allowedTools"]
    assert json.loads((out / "mcp.json").read_text()) == {"mcpServers": {}}


@_posix_only
def test_selected_mcp_server_ships_secret_stripped(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        mcp_servers={"weather": {"command": "weather-mcp", "env": {"TOKEN": "abc123"}}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select={"mcp": {"weather"}})
    report = mod.build_bundle(
        crew, spec, mod.enumerate_all(crew, spec), mod.merge_plans([plan_path], "frontdesk"), out
    )
    mcp = json.loads((out / "mcp.json").read_text())["mcpServers"]
    assert "weather" in mcp
    # env is supplementary and dropped wholesale on export (see _clean_mcp_server)
    assert "env" not in mcp["weather"]
    assert mcp["weather"]["command"] == "weather-mcp"
    assert any("dropped env" in n for n in report.notes)
    assert report.mcp_servers == ["weather"]


@_posix_only
def test_container_owned_mcp_is_blocked(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        mcp_servers={"kirocrew-core": {"command": "/abs/path/kirocrew", "args": ["core"]}},
    )
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    c = next(x for x in mod.enumerate_all(crew, spec)["mcp"] if x.id == "kirocrew-core")
    assert c.blocked


@_posix_only
def test_plan_for_another_crew_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", name="frontdesk")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select=None)
    doc = json.loads(plan_path.read_text())
    doc["crew"] = "someone-else"
    plan_path.write_text(json.dumps(doc))
    with pytest.raises(mod.ExportRefused, match="written for crew"):
        mod.merge_plans([plan_path], "frontdesk")


# ---------------------------------------------------------------------------
# CLI end-to-end via subprocess: SMC_BUNDLE_JSON is the LAST line
# ---------------------------------------------------------------------------
@_posix_only
def test_cli_build_prints_bundle_json_last_line(tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "packaging.build",
            "--crew",
            "frontdesk",
            "--out",
            str(out),
            "--source",
            str(src),
        ],
        cwd=str(tmp_path),
        env=_child_env(),
        capture_output=True,
        text=True,
        # Pinned: text mode without this decodes with the Windows ANSI code
        # page, and the bundle JSON this asserts on carries UTF-8.
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last.startswith("SMC_BUNDLE_JSON="), proc.stdout
    payload = json.loads(Path(last.split("=", 1)[1]).read_text())
    assert payload["crew_name"] == "frontdesk"
    assert payload["bundle_dir"] == str(out)
    assert payload["digest"].startswith("sha256:")
    assert payload["skill_count"] == 0
    assert payload["mcp_servers"] == []
    assert any(d["id"] == "faq" for d in payload["denied"])
    assert set(payload) == {
        # An exact set, so a key added or removed is a deliberate change to a machine
        # contract rather than something a consumer discovers in production. report_version
        # identifies the writer, which is what lets the build refuse to overwrite a file at
        # this path that it did not produce.
        "report_version",
        "crew_name",
        "bundle_dir",
        "digest",
        "skill_count",
        "mcp_servers",
        "denied",
    }


@_posix_only
def test_cli_plan_writes_template_without_bundle(tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "work"
    out.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "packaging.build",
            "plan",
            "--crew",
            "frontdesk",
            "--out",
            str(out),
            "--source",
            str(src),
        ],
        cwd=str(tmp_path),
        env=_child_env(),
        capture_output=True,
        text=True,
        # Pinned: text mode without this decodes with the Windows ANSI code
        # page, and the bundle JSON this asserts on carries UTF-8.
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "curation-plan.json").is_file()
    assert not (out / "manifest.json").exists()  # no bundle written
    doc = json.loads((out / "curation-plan.json").read_text())
    assert doc["reviewed_by"] == "" and doc["reviewed_at"] == ""
    assert all(e["include"] is False for e in doc["skills"])


# ---------------------------------------------------------------------------
# The CLI subprocess must resolve packaging.build WITHOUT writing bytecode into
# the source tree. Running with cwd=CREW_ROOT would make the child's imports
# dropped __pycache__ dirs under the crew tree and the residue outlived the test.
# ---------------------------------------------------------------------------
def _pyc_files_under(root: Path) -> set[Path]:
    return {p for p in root.rglob("*.pyc")}


@_posix_only
def test_cli_subprocess_leaves_no_pycache_in_the_source_tree(tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"

    # The child must be forced to (re)compile on import, or the assertion is
    # vacuous: an up-to-date cached .pyc means even a badly configured child
    # writes nothing. The obvious way to force that is to delete the checkout's
    # cached bytecode, and an earlier version of this test did -- which made a
    # test about not touching the source tree itself touch the source tree, and
    # deleted a file outside tmp_path that the run never restored.
    #
    # Copying the package into tmp_path gets the same cold cache with no such
    # cost. The copy is a faithful stand-in because the property under test is a
    # property of the CHILD'S ENVIRONMENT (no bytecode written next to the module
    # it imports), not of one particular directory: a fresh tree has no cache by
    # construction, so the child compiles either way.
    pkg_copy_root = tmp_path / "pkgroot"
    shutil.copytree(
        CREW_ROOT / "packaging",
        pkg_copy_root / "packaging",
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    assert not _pyc_files_under(pkg_copy_root), "the copy must start with a cold cache"
    before_real = _pyc_files_under(CREW_ROOT)

    env = _child_env()
    env["PYTHONPATH"] = str(pkg_copy_root) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "packaging.build",
            "--crew",
            "frontdesk",
            "--out",
            str(out),
            "--source",
            str(src),
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # The module still resolves: cwd is a temp dir, so this proves PYTHONPATH,
    # not cwd, is what makes `python -m packaging.build` importable.
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1].startswith("SMC_BUNDLE_JSON="), proc.stdout

    # The cold copy the child actually imported from: any bytecode here is
    # bytecode the child would have written beside the real module.
    written = _pyc_files_under(pkg_copy_root)
    assert (
        not written
    ), "the CLI subprocess wrote bytecode beside the module it imported: " + ", ".join(
        str(p) for p in sorted(written)
    )
    # And the real checkout gained nothing, which is the property this test is
    # named for. A set difference rather than an emptiness check, because the
    # checkout legitimately has cached bytecode from every other test in this
    # file and deleting it to get a clean baseline is what this test was fixed
    # for not doing.
    new = _pyc_files_under(CREW_ROOT) - before_real
    assert not new, "the CLI subprocess wrote bytecode into the source tree: " + ", ".join(
        str(p) for p in sorted(new)
    )


# ---------------------------------------------------------------------------
# Default curation home: with KIROCREW_HOME unset the skills root must be
# ~/.kiro/crew/skills (the repo convention), NOT ~/.kirocrew, which appeared
# nowhere else in the tree and made curation scan a nonexistent directory and
# silently omit skills.
# ---------------------------------------------------------------------------
def test_default_config_dir_is_kiro_crew_not_kirocrew(tmp_path, monkeypatch):
    mod = load_build()
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    # BOTH spellings. Windows ``expanduser`` reads ``USERPROFILE``, so setting only
    # ``HOME`` left this test resolving the CI runner's real home instead of the
    # fixture: it asserted against that account's own ``.kiro/crew`` and failed,
    # having also let the code under test reach a directory outside its fixture.
    # Same pairing as test_bench_cli.py.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    got = mod._default_config_dir()
    assert got == tmp_path / ".kiro" / "crew", got
    assert got != tmp_path / ".kirocrew"


def test_default_config_dir_honours_kirocrew_home_override(tmp_path, monkeypatch):
    mod = load_build()
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "custom"))
    assert mod._default_config_dir() == tmp_path / "custom"


def test_resolve_crew_without_source_puts_skills_under_kiro_crew(tmp_path, monkeypatch):
    mod = load_build()
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser reads this
    crew = mod.resolve_crew("frontdesk", None)
    assert crew.skills_root == tmp_path / ".kiro" / "crew" / "skills", crew.skills_root


def test_missing_skills_root_warns_loudly(tmp_path, capsys):
    # A missing skills root is the silent-omission trap: warn on stderr, name the
    # path, and still return [] (a persona-only crew is legitimate).
    mod = load_build()
    missing = tmp_path / "nope" / "skills"
    assert mod.skill_candidates(missing) == []
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert str(missing) in err


def test_a_skills_root_that_is_a_file_is_refused_not_shipped_empty(tmp_path):
    """A directory's SHAPE is author-supplied input: a non-directory skills root is malformed.

    ``not is_dir()`` is true both when the root is ABSENT (persona-only, legitimate -> warn +
    empty) and when it EXISTS as a plain file (malformed layout). The second must REFUSE, not
    ship a plausible-looking empty bundle -- the silent-omission trap the author-supplied-
    structure rule closes (absent -> empty, wrong-type -> refuse, unreadable -> fail closed).
    """
    mod = load_build()
    root_as_file = tmp_path / "home" / "skills"
    root_as_file.parent.mkdir(parents=True)
    root_as_file.write_text("this should have been a directory\n", encoding="utf-8")
    with pytest.raises(mod.ExportRefused) as caught:
        mod.skill_candidates(root_as_file)
    msg = str(caught.value)
    assert "not a directory" in msg and "malformed" in msg


# ---------------------------------------------------------------------------
# The loader itself: an exec-loaded variant must not outlive the test.
#
# ``load_build`` registers the throwaway package under a unique ``smc_build_vN`` root,
# because the facade resolves every name from its owner through ``sys.modules`` and the
# owners reach each other through their package. Those entries have a reader only while
# the test that loaded them runs. Left behind, a copy -- and most of this suite loads
# MUTATED copies -- would stay importable by its name and hold its mutated guards for the
# rest of the worker, so the next test's first ``load_build`` removes it.
# ---------------------------------------------------------------------------
def test_load_build_leaves_no_synthetic_module_in_sys_modules(monkeypatch) -> None:
    """A copy leaves ``sys.modules`` when the next test loads one, and never before.

    A first warm-up call caches the real imports the builder pulls in, so the snapshot
    below measures only the synthetic copies rather than those first-time imports.
    """
    # Stands in for a pin the shared fixture holds for the whole test.
    monkeypatch.setenv("SMC_LOADER_TEST_SHARED_PIN", "held")
    load_build()  # warm the import caches so the snapshot measures only the variant
    release_builder_copies()
    before = set(sys.modules)

    # A copy another test loaded: registered under that test's id.
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setenv("PYTEST_CURRENT_TEST", "test_elsewhere.py::test_earlier (call)")
        earlier = load_build(mutate=("def resolve_crew", "def resolve_crew"))
        earlier_root = earlier.__package__
    assert (
        os.environ.get("SMC_LOADER_TEST_SHARED_PIN") == "held"
    ), "the scoped override must not clear the shared fixture's pins"

    mod = load_build()
    root = mod.__package__
    assert callable(mod.resolve_crew)
    assert f"{root}.pipeline.crew" in sys.modules, "a live copy's owners must be resolvable"
    assert not [
        k for k in sys.modules if k.split(".")[0] == earlier_root
    ], "a finished test's copy -- a mutated one -- is still importable by its name"

    # Released by this test, the copy stays registered until the next load: nothing
    # removes it from a garbage-collection callback in the middle of another test.
    del mod, earlier
    gc.collect()
    assert f"{root}.pipeline.crew" in sys.modules

    release_builder_copies()
    after = set(sys.modules)
    assert not [k for k in after if k.startswith("smc_build_")], (
        "load_build leaked a synthetic module into sys.modules; a later import by that "
        "name would resolve this variant"
    )
    assert after == before, "load_build changed the sys.modules key set"

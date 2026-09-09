"""Two defects a Linux-only run cannot see, and one the shape of a count hid.

All three came from review, not from this suite. For the newline pair the reason is the
same in both directions: the suite wrote its fixtures through the very call that was
wrong, so the fixture and the output were corrupted together and the comparison stayed
green.

**Newline translation.** ``Path.write_text(text, encoding="utf-8")`` leaves ``newline`` at
``None``, which translates every ``"\\n"`` to ``os.linesep`` on write. On Windows that adds
a ``"\\r"`` to every line of every file the builder stages, and two things break. The
content pin compares ``_tree_hash`` (SOURCE bytes) with ``_staged_tree_hash`` (SHIPPED
bytes), so an ordinary LF-authored skill hashes differently once staged and the build
refuses with "changed while the bundle was being written" -- fail-closed, but it aborts
every Windows build of a normal crew. And ``bundle_digest`` runs over those same staged
bytes, so one crew reports different digests depending on the platform that built it.

Reproducing that on Linux needs the translation itself, which no argument to the builder
can turn on. ``load_build(mutate=...)`` is how this suite already substitutes one
construct to observe what a guard prevents, so the test below swaps the pinned writer for
a translating one and asserts the build then refuses. That is the actual Windows failure,
observable on the platform CI mostly runs.

**Nested skill ids.** A skill id is ``relative_to(skills_root).as_posix()`` and may contain
a separator, so ``aws/ec2`` and ``aws/s3`` are two skills under one top-level ``aws``
directory. Counting top-level directories reported ``skill_count == 1`` for that pair, in
the printed summary and in ``SMC_BUNDLE_JSON`` alike.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib

import pytest

from .test_producer import BUILD_PY, load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _is_text_file_call(node: ast.Call) -> bool:
    """True for a call that opens or writes a TEXT file, so ``newline`` applies.

    Three exclusions, each for a different reason, and each one a real call in this module:

    * ``os.open`` is the SYSCALL. Its second argument is a flag bitmask, not a mode string,
      and it has no ``newline`` -- the module calls it eleven times for the ``O_NOFOLLOW``
      work. Told apart by its receiver, because ``os.open`` and ``Path.open`` share an
      attribute name.
    * a binary mode. ``os.fdopen(fd, "rb")`` on the prompt read path takes no ``newline``
      at all, so demanding it there would be demanding a TypeError.
    * nothing else. An absent mode means text, which is Python's default, so the
      conservative reading and the correct one agree.
    """
    attr = getattr(node.func, "attr", "")
    if attr not in {"write_text", "fdopen", "open"}:
        return False
    receiver = getattr(node.func, "value", None)
    if attr == "open" and isinstance(receiver, ast.Name) and receiver.id == "os":
        return False
    for arg in node.args[1:2]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return "b" not in arg.value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return "b" not in str(kw.value.value)
    return True


def _text_write_calls() -> list[ast.Call]:
    """Every call in the module that moves str to or from disk in TEXT mode.

    ``write_text`` was the only such call when this rule was written. The staging marker
    then moved to an ``os.fdopen`` write, to get the ``O_NOFOLLOW`` and ``O_EXCL`` that
    ``write_text`` cannot pass, and ``_read_text`` moved to ``Path.open`` because
    ``read_text`` only grew ``newline`` in 3.13. All three take ``newline`` for the same
    reason, so the rule covers all three -- which is what stops the fix from being routed
    around by changing how the file is opened.
    """
    tree = ast.parse(BUILD_PY.read_text(encoding="utf-8"), str(BUILD_PY))
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_text_file_call(node)
    ]


def _build(mod, home: pathlib.Path, out: pathlib.Path, select: dict[str, set[str]]):
    """Resolve, enumerate, sign a plan selecting *select*, verify, build."""
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, out, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out / "bundle")


def test_every_write_text_in_the_builder_pins_newline() -> None:
    """The RULE, not one call site: no unpinned ``write_text`` in the module.

    Stated over the whole module because the bug is a property of the DEFAULT, so the
    next call written without thinking about it is the next occurrence. Two of the
    current calls do not feed a hashed artifact and pin it anyway: a rule with exceptions
    is one nobody can apply from the call site.
    """
    unpinned = [
        f"build.py:{node.lineno}"
        for node in _text_write_calls()
        if not any(kw.arg == "newline" for kw in node.keywords)
    ]
    assert not unpinned, (
        "write_text with newline unpinned translates \\n to os.linesep, corrupting every "
        f'staged byte on Windows: {unpinned}. Pass newline="".'
    )


def test_the_newline_rule_is_scanning_real_calls() -> None:
    """Non-vacuity: a rule asserted over an empty set passes while holding nothing.

    The failure mode that matters is the rule going quiet without anyone editing it,
    which is what happens if the writes move somewhere this walk does not look.
    """
    # THREE, because two of the builder's reads take bytes and decode them afterwards: the
    # prompt ceiling is named in BYTES, and ``read(n)`` on a text stream bounds CHARACTERS,
    # so a 1048576-character three-byte-per-character persona measured 3145728 bytes while
    # reporting itself within the limit. A binary ``os.fdopen`` takes no ``newline`` at all,
    # which this module's own contract above calls out as demanding a TypeError -- so those
    # two are outside this rule by construction rather than by having escaped it. The floor
    # exists to catch the rule going quiet, and three calls still hold it to something.
    found = len(_text_write_calls())
    assert found >= 3, (
        f"expected the builder's text read/write calls to be in scope, found {found} -- "
        "if the writes moved, re-point this walk"
    )


@_posix_only
def test_a_crlf_authored_skill_ships_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """The other direction, and the one that reddens on LINUX.

    Pinning only the WRITE moved this bug instead of fixing it. ``read_text`` defaults to
    universal-newlines decoding, so a CRLF file arrives as a string holding "\\n"; the
    pinned write then emits LF while ``_tree_hash`` pinned the CRLF source, and the build
    refuses with the same message as before. That happens on every platform, because the
    translation is in the DECODE, not in the OS.

    A Windows-authored skill in a shared repository is an ordinary thing, so this is not
    a hypothetical. The property both pins exist to give is here: the bytes that ship are
    the bytes that were hashed, whatever the file holds.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "placeholder"}})
    body = b"# FAQ\r\nline one\r\nline two\r\n"
    (home / "skills" / "faq" / "SKILL.md").write_bytes(body)

    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"faq"}})
    out = work / "bundle"

    shipped = (out / "skills" / "faq" / "SKILL.md").read_bytes()
    assert shipped == body, (
        "a CRLF-authored skill was not shipped verbatim: the read translated it to LF "
        "while the content pin was taken over the CRLF source"
    )
    assert b"\r\n" in shipped, "the fixture must stay CRLF, or this proves nothing"
    assert report.digest == mod.bundle_digest(out)


@_posix_only
def test_MUTATION_translating_reader_aborts_the_build(tmp_path: pathlib.Path) -> None:
    """Restore universal-newlines decoding and a CRLF skill is refused.

    The companion to the writer mutation below. Together they pin that BOTH ends are
    needed: either one alone leaves the round trip lossy for one of the two line endings.
    """
    mod = load_build(
        mutate=(
            # The skill copy reads RAW BYTES through the shared file-read guard and decodes
            # them separately, so newline translation has nowhere to happen; the only way to
            # reintroduce it is at that decode. ``raw.decode`` is unique to the skill path --
            # the other readers bind their bytes to ``data`` -- so the bare decode line is a
            # safe anchor and mutating it refuses on the skill the content pin protects.
            '            text = raw.decode("utf-8")',
            '            text = raw.decode("utf-8").replace("\\r\\n", "\\n")',
        )
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "placeholder"}})
    (home / "skills" / "faq" / "SKILL.md").write_bytes(b"# FAQ\r\nline one\r\n")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, tmp_path / "work", {"skills": {"faq"}})
    assert "changed while the bundle was being written" in str(caught.value)


@_posix_only
def test_MUTATION_translating_writer_aborts_the_build(tmp_path: pathlib.Path) -> None:
    """Restore a translating write and the content pin refuses, as it would on Windows.

    Staged leaves are written through a descriptor-relative no-follow open that writes RAW
    BYTES (``os.fdopen(fd, "wb")``), so a byte authored as ``\\n`` lands as ``\\n`` -- CRLF
    translation is structurally impossible on that branch. This mutation reintroduces the
    translation at the write itself (``data`` -> ``data.replace(...)``) to prove the pin is
    what catches a staged tree whose bytes diverge from the source: ``_tree_hash`` hashes the
    SOURCE bytes and ``_staged_tree_hash`` the SHIPPED bytes, so a CRLF-mangled staged file
    reddens with "changed while the bundle was being written" rather than shipping.

    The anchor is the byte write inside the no-follow primitive; ``load_build`` asserts it is
    present and unique before mutating, because an anchor that silently stops matching is how
    this test once passed while proving nothing.
    """
    anchor = "                fh.write(bytes(data))"
    assert BUILD_PY.read_text(encoding="utf-8").count(anchor) == 1, (
        "the mutation anchor is not unique, so replace(..., 1) may target the wrong call "
        "and this test would pass without exercising the guarded write"
    )
    mod = load_build(
        mutate=(anchor, '                fh.write(bytes(data).replace(b"\\n", b"\\r\\n"))')
    )
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    (home / "skills" / "faq" / "SKILL.md").write_bytes(b"# FAQ\nline one\nline two\n")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, tmp_path / "work", {"skills": {"faq"}})
    assert "changed while the bundle was being written" in str(caught.value)


@_posix_only
def test_a_skill_with_lf_content_ships_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """The positive half: a normal LF skill is accepted and shipped verbatim.

    ``write_bytes`` for the source is the point. ``write_text`` would translate the
    fixture on Windows exactly as the builder translated the copy, and the two errors
    would cancel into a green test -- which is how the defect survived review.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "placeholder"}})
    body = b"# FAQ\nline one\nline two\n"
    (home / "skills" / "faq" / "SKILL.md").write_bytes(body)

    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"faq"}})
    out = work / "bundle"

    assert (out / "skills" / "faq" / "SKILL.md").read_bytes() == body, (
        "the staged bytes differ from the source bytes, so the content pin comparing "
        "their hashes cannot hold and the build refuses on this platform"
    )
    assert report.digest == mod.bundle_digest(out)


@_posix_only
def test_nested_skill_ids_are_counted_individually(tmp_path: pathlib.Path) -> None:
    """``aws/ec2`` and ``aws/s3`` are two skills, not one ``aws`` directory.

    The count drives the printed summary and ``SMC_BUNDLE_JSON``'s ``skill_count``, which
    a deploy step reads to decide whether a bundle carries what was approved. Reporting 1
    for a two-skill bundle is a wrong answer to that question.
    """
    mod = load_build()
    home = make_crew(
        tmp_path / "home",
        skills={
            "aws/ec2": {"SKILL.md": "# EC2\n"},
            "aws/s3": {"SKILL.md": "# S3\n"},
            "faq": {"SKILL.md": "# FAQ\n"},
        },
    )
    work = tmp_path / "work"
    report = _build(mod, home, work, {"skills": {"aws/ec2", "aws/s3", "faq"}})
    out = work / "bundle"

    top_level = len([p for p in (out / "skills").iterdir() if p.is_dir()])
    assert top_level == 2, "the fixture must actually nest, or this proves nothing"
    assert (
        report.skill_count == 3
    ), f"nested ids collapsed into their shared top-level directory ({top_level} dirs)"
    assert (out / "skills" / "aws" / "ec2" / "SKILL.md").is_file()
    assert (out / "skills" / "aws" / "s3" / "SKILL.md").is_file()


@_posix_only
def test_the_shipped_prompt_is_the_authored_prompt(tmp_path: pathlib.Path) -> None:
    """No verification block is prepended to the persona this bundle ships.

    The builder does NOT prepend a ``[deployment verification]`` section to a deployed
    prompt, for a gate that lives in the track that deploys. It went with the gate. What
    is pinned here is what is left: the prompt in the bundle is the prompt the operator
    wrote, so a reviewer reading ``agent.json`` sees what will run.
    """
    mod = load_build()
    prompt = "You are the front desk. Answer questions about hours and location."
    home = make_crew(tmp_path / "home", prompt=prompt)
    work = tmp_path / "work"
    _build(mod, home, work, {})
    out = work / "bundle"

    shipped = json.loads((out / "agent.json").read_text(encoding="utf-8"))
    assert shipped["prompt"] == prompt
    assert "[deployment verification]" not in shipped["prompt"]

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "fingerprint" not in manifest, "the deferred field is back in the manifest"
    assert manifest["digest"] == mod.bundle_digest(out)


def test_the_builder_source_carries_no_prompt_injection() -> None:
    """The deferral must be real, not merely unreachable.

    Left in the module but uncalled, the block would still be reviewed here and would
    still be one edit away from shipping, which is the situation the reviewer objected to.
    """
    source = BUILD_PY.read_text(encoding="utf-8")
    for token in ("deployment verification", "SMC-FINGERPRINT", "fingerprint_challenge"):
        assert token not in source, f"{token} is still in build.py"

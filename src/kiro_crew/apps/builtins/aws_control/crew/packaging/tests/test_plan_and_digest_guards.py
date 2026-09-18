"""Guards on the curation plan and the manifest digest.

``is_dir()`` and ``is_file()`` follow links, so they answer about the TARGET when what
matters is the ENTRY. ``_is_shape_this_build_never_writes`` answers about shape when what
matters is origin. The reparse walk ran after ``resolve()``, so it answered about the
resolved path when what matters is the one that was written down. And the encoded-credential
detector answered "nothing found" when the truth was "nothing looked".

R1 the chain check ran too late -- ``_resolve_prompt_path`` resolved before checking, and
   resolve IS the traversal: on Windows following a reparse point that names a share is the
   outbound SMB probe with its NTLM exchange, and resolve also COLLAPSES the links, so a walk
   placed after it can never see one. The previous version passed its own tests only because
   they called it directly with an unresolved path, which is not what the call site passes.

R2 the redactor fallback -- encoded detection vanished silently when ``kiro_crew`` was not
   importable, which is the documented standalone mode.

R3 empty directories -- verified by neither the top-level name check, the shape check, nor
   the file digest, then removed by the recursive delete.

R4 a symlinked output root -- ``is_dir()`` accepted a link to a directory, so the build
   created and deleted inside the link's target.

R6 (Opus) ``read_plan`` -- ``UnicodeDecodeError`` is a ``ValueError``, neither an ``OSError``
   nor a ``JSONDecodeError``, so a plan that is not valid UTF-8 escaped the handler.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)

_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
_NO_REDACTOR = (
    "    _CANONICAL_REDACTOR: Callable[[str], tuple[str, list[str]]] | None = redact_credentials",
    "    _CANONICAL_REDACTOR = None",
)


def _build(mod, home: pathlib.Path, work: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    work.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, work, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, work / "bundle")


# ---------------------------------------------------------------------------
# R1
# ---------------------------------------------------------------------------
@_posix_only
def test_a_linked_parent_is_refused_through_the_real_build(tmp_path: pathlib.Path) -> None:
    """Driven end to end, because the previous version passed a UNIT test and did nothing.

    The refusal must name the LINK, which is what distinguishes the chain check from the
    containment check that had been carrying this case. Containment compares resolved paths,
    so it would refuse with 'escapes the agents directory' while the walk saw nothing.
    """
    mod = load_build()
    secret = tmp_path / "secrets"
    secret.mkdir()
    (secret / "persona.md").write_bytes(b"PRIVATE KEY MATERIAL\n")
    home = make_crew(tmp_path / "home", prompt="file://sub/persona.md")
    (home / "agents" / "sub").symlink_to(secret, target_is_directory=True)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "link or junction" in str(caught.value), str(caught.value)


@_posix_only
def test_the_check_runs_before_any_resolution(tmp_path: pathlib.Path) -> None:
    """The ordering IS the fix, so it is asserted rather than assumed.

    A link whose target does not exist cannot be resolved at all in strict terms, and cannot
    be probed. If the refusal still names the link, the check ran on the path as written --
    which is the only place a redirect is visible.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://sub/persona.md")
    (home / "agents" / "sub").symlink_to(tmp_path / "nowhere", target_is_directory=True)

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "link or junction" in str(caught.value), str(caught.value)


@_posix_only
def test_a_parent_reference_is_refused_rather_than_normalised(tmp_path: pathlib.Path) -> None:
    """``a/../b`` is not ``b`` when ``a`` is a link, so it is not normalised here."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://sub/../persona.md")
    (home / "agents" / "sub").mkdir()
    (home / "agents" / "persona.md").write_bytes(b"content\n")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "parent directory" in str(caught.value)


@_posix_only
def test_an_ordinary_nested_prompt_still_inlines(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the chain check must not refuse a plain subdirectory."""
    mod = load_build()
    home = make_crew(tmp_path / "home", prompt="file://sub/persona.md")
    (home / "agents" / "sub").mkdir()
    (home / "agents" / "sub" / "persona.md").write_bytes(b"a nested persona\n")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["prompt"] == "a nested persona\n"


# ---------------------------------------------------------------------------
# R2
# ---------------------------------------------------------------------------
def test_encoded_credentials_are_found_without_the_canonical_redactor() -> None:
    """Standalone mode must not silently stop looking."""
    mod = load_build(mutate=_NO_REDACTOR)
    assert mod._CANONICAL_REDACTOR is None, "the mutation did not take"
    encoded = base64.b64encode(f"aws_secret_access_key = {_DOC_SECRET}".encode()).decode()
    kinds = [leak.kind for leak in mod.scan_text(encoded, "t")]
    assert kinds, "the standalone fallback found nothing"
    assert any(k.startswith("encoded-") for k in kinds), kinds


def test_the_standalone_fallback_does_not_flag_ordinary_text() -> None:
    """Non-vacuity: a decoder that reported everything would pass the test above.

    The long alphanumeric strings here are the false positives that matter -- a digest, a
    token-shaped id -- because a scanner that refuses those makes the build unusable.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    for text in (
        "# FAQ\nStore hours are 9 to 6.\n",
        "digest: 9f8c2b1e4a7d6f3b8e2c5a9d1f4b7e0c3a6d9f2b5e8c1a4d7f0b3e6c9a2d5f8b\n",
        "# Encoding\nUse base64 for binary payloads.\n",
    ):
        assert not mod.scan_text(text, "t"), text


def test_the_literal_pass_is_unaffected_by_the_fallback() -> None:
    """A literal credential is still found, with or without the redactor."""
    mod = load_build(mutate=_NO_REDACTOR)
    assert mod.scan_text(f"aws_secret_access_key = {_DOC_SECRET}", "t")


def test_a_bare_unlabelled_secret_is_flagged_in_standalone_mode() -> None:
    """The parity gap: the canonical redactor catches a bare 40-char AWS secret by shape.

    The standalone path had only labelled patterns and a decode pass, and a bare secret carries
    no label and decodes to non-UTF-8 bytes -- so without the structural detector it shipped.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    assert mod._CANONICAL_REDACTOR is None, "the mutation did not take"
    # No label, no assignment -- the secret sits bare in a skill body.
    kinds = [leak.kind for leak in mod.scan_text(f"see {_DOC_SECRET} for access", "t")]
    assert "bare-secret" in kinds, kinds


def test_a_bare_secret_glued_to_adjacent_base64_is_still_flagged() -> None:
    """A real key glued to neighbouring base64 chars is a 41+ run; the sliding window finds it."""
    mod = load_build(mutate=_NO_REDACTOR)
    kinds = [leak.kind for leak in mod.scan_text(f"X{_DOC_SECRET}ABC", "t")]
    assert "bare-secret" in kinds, kinds


def test_the_bare_secret_detector_does_not_flag_benign_40_char_shapes() -> None:
    """Non-vacuity: the detector rejects a git sha, prose, and an encoded-text blob.

    A detector that flagged these would refuse ordinary crew content and make the build unusable.
    """
    mod = load_build(mutate=_NO_REDACTOR)
    encoded_text = base64.b64encode(
        b"this is a perfectly ordinary sentence of readable text, encoded once"
    ).decode()
    for benign in (
        "commit 0123456789abcdef0123456789abcdef01234567",  # 40-char git sha (hex only)
        "the quick brown fox jumped over the lazy dogs again and again ok",  # prose
        encoded_text,  # decodes to printable text -> an encoded blob, not a bare key
    ):
        assert not any(leak.kind == "bare-secret" for leak in mod.scan_text(benign, "t")), benign


# ---------------------------------------------------------------------------
# R3
# ---------------------------------------------------------------------------
@_posix_only
def test_the_empty_directory_guard_names_the_directory(tmp_path: pathlib.Path) -> None:
    """Rebuilding over a bundle with an extra empty directory refuses and says which."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    _build(mod, home, work, {"skills": {"faq"}})
    (work / "bundle" / "skills" / "notes").mkdir()

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    plan = mod.merge_plans(
        [sign_plan(mod, crew, spec, tmp_path / "w2", select={"skills": {"faq"}})], "frontdesk"
    )
    mod.verify(plan, "frontdesk", cands)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_bundle(crew, spec, cands, plan, work / "bundle")
    assert "no file this build would have written" in str(caught.value)
    assert "skills/notes" in str(caught.value)


@_posix_only
def test_the_builders_own_empty_skills_directory_is_accepted(tmp_path: pathlib.Path) -> None:
    """A bundle with no skills selected leaves an empty ``skills/``, and must rebuild.

    Measured, not assumed: the first version of this guard refused it and reddened 13 tests.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    _build(mod, home, work, {"skills": set()})
    assert (work / "bundle" / "skills").is_dir()
    assert not any((work / "bundle" / "skills").iterdir())
    _build(mod, home, work, {"skills": set()})


# ---------------------------------------------------------------------------
# R4
# ---------------------------------------------------------------------------
@_posix_only
def test_a_symlinked_output_root_is_refused(tmp_path: pathlib.Path) -> None:
    """``is_dir()`` follows the link, so the entry has to be judged first.

    The target is EMPTY on purpose. A target holding the operator's own files trips the older
    "holds files this build does not own" check, which would make this test pass with the new
    guard removed -- measured: it did. Empty, and a valid previous bundle, are the cases only
    this guard covers, and they are the ordinary ones for a deliberately placed link.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    real = tmp_path / "somewhere-else"
    real.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "bundle").symlink_to(real, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work, {"skills": {"faq"}})
    assert "symlink" in str(caught.value)
    assert (work / "bundle").is_symlink(), "the operator's link was replaced"


@_posix_only
def test_a_symlinked_root_is_refused_even_without_the_outer_redirect_guard(
    tmp_path: pathlib.Path,
) -> None:
    """Defense in depth: the ownership verifier checks the ANCHOR itself, not only the outer
    staging/out redirect guard.

    With the outer ``_is_redirecting_entry(candidate)`` guard mutated off, a symlinked ``--out``
    that already holds a bundle-shaped tree still cannot pass ownership verification, because
    ``_refuse_unless_this_build_wrote_it`` now refuses a reparse-point root before it verifies
    anything relative to it. The anchor is the subject the recursive delete is relative to, so
    a verdict about a root that was never verified is a verdict about the wrong tree.
    """
    mod = load_build(mutate=("        if _is_redirecting_entry(candidate):", "        if False:"))
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    # Build a real bundle elsewhere, then point --out at a symlink to it: the ownership check
    # would otherwise follow the link and validate the target as "ours", then delete through it.
    real = tmp_path / "somewhere-else"
    real.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "bundle").symlink_to(real, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work, {"skills": {"faq"}})
    assert "symlink or reparse point" in str(caught.value)
    assert (work / "bundle").is_symlink(), "the link is left intact; nothing was deleted through it"
    assert (real / "manifest.json").exists() is False and list(
        real.iterdir()
    ) == [], "the target behind the link is untouched"


# ---------------------------------------------------------------------------
# R6
# ---------------------------------------------------------------------------
@_posix_only
def test_a_plan_that_is_not_utf8_is_refused_cleanly(tmp_path: pathlib.Path) -> None:
    """A non-UTF-8 plan is refused cleanly rather than escaping as a traceback.

    It is now caught at the no-follow read (which returns None for a body it cannot decode)
    rather than at ``json.loads``; either way the contract is a clean ``ExportRefused``.
    """
    mod = load_build()
    bad = tmp_path / "curation-plan.json"
    bad.write_bytes(b'{"plan_version": 1, "note": "\xff\xfe not utf-8"}')
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(bad)
    assert "not valid JSON" in str(caught.value) or "could not be read as UTF-8" in str(
        caught.value
    )


def test_a_plan_that_is_valid_utf8_but_bad_json_is_still_refused(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the wider tuple must still cover what the narrower one did."""
    mod = load_build()
    bad = tmp_path / "curation-plan.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(mod.ExportRefused):
        mod.read_plan(bad)


# ---------------------------------------------------------------------------
# Round-22 GPT family: a non-string sha256 pin or entry id in the plan must be
# refused, not str()-coerced. A coerced pin is a fabricated integrity claim in a
# signed plan; a coerced id fabricates what the plan selects.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_non_string_sha256_pin_is_refused_not_coerced(tmp_path: pathlib.Path) -> None:
    mod = load_build()
    bad = tmp_path / "curation-plan.json"
    bad.write_text(
        json.dumps(
            {
                "plan_version": 1,
                "skills": [{"id": "faq", "include": True, "sha256": {"not": "a string"}}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(bad)
    msg = str(caught.value)
    assert "sha256" in msg and "dict" in msg, "the refusal names the field and its actual type"


@_posix_only
def test_a_non_string_entry_id_is_refused_not_coerced(tmp_path: pathlib.Path) -> None:
    mod = load_build()
    bad = tmp_path / "curation-plan.json"
    bad.write_text(
        json.dumps(
            {"plan_version": 1, "skills": [{"id": ["not", "a", "string"], "include": True}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(bad)
    assert "id" in str(caught.value) and "list" in str(caught.value)


# ---------------------------------------------------------------------------
# Round-24 GPT: plan provenance fields (crew/reviewed_by/reviewed_at) feed the
# signed-plan guard, so a non-string is refused, not str()-coerced.
# ---------------------------------------------------------------------------
@_posix_only
@pytest.mark.parametrize("field", ["crew", "reviewed_by", "reviewed_at"])
def test_a_non_string_plan_provenance_field_is_refused_not_coerced(
    field, tmp_path: pathlib.Path
) -> None:
    mod = load_build()
    bad = tmp_path / "curation-plan.json"
    bad.write_text(
        json.dumps({"plan_version": 1, "skills": [], field: {"forged": True}}),
        encoding="utf-8",
    )
    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_plan(bad)
    assert field in str(caught.value) and "dict" in str(caught.value)


# ---------------------------------------------------------------------------
# The ownership gate reads ``curation-plan.json`` and ``manifest.json`` to decide
# whether ``--out`` is a prior bundle it may recursively replace. A symlink planted
# at either name must not be FOLLOWED into a foreign file whose contents satisfy the
# plan_version+crew or manifest-digest check and authorise the delete. The gate's
# shape scan runs FIRST: ``_walk_no_reparse`` yields the leaf entry and
# ``_is_shape_this_build_never_writes`` refuses any reparse point, so the reads are
# unreachable through a redirect. These pin that a planted symlink is refused with the
# foreign target intact, and the mutation pins the shape scan as what refuses it.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_symlinked_plan_only_ownership_read_is_refused(tmp_path: pathlib.Path) -> None:
    """A symlink at ``curation-plan.json`` pointing at a satisfying plan is refused.

    The plan-only branch reads ``curation-plan.json`` to prove ownership. A symlink there
    aimed at a foreign JSON that carries this crew's plan_version would authorise a recursive
    replace of ``--out`` if it were followed; the shape scan refuses the redirect first, so
    the foreign target is never read and stays intact.
    """
    mod = load_build()
    out = tmp_path / "bundle"
    out.mkdir()
    foreign = tmp_path / "foreign_plan.json"
    foreign.write_text(
        json.dumps({"plan_version": mod.PLAN_VERSION, "crew": "frontdesk"}), encoding="utf-8"
    )
    (out / mod.PLAN_FILENAME).symlink_to(foreign)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_this_build_wrote_it(out, "--out", "frontdesk")
    assert "a shape this build never writes" in str(caught.value)
    assert foreign.is_file(), "the planted symlink was followed and its target read"


@_posix_only
def test_a_symlinked_manifest_ownership_read_is_refused(tmp_path: pathlib.Path) -> None:
    """A symlink at ``manifest.json`` pointing at a satisfying manifest is refused.

    The bundle branch reads ``manifest.json`` for the recorded digest. A symlink there aimed
    at a foreign manifest is refused by the shape scan before the read, so the digest check
    never runs against the link target and the foreign file is untouched.
    """
    mod = load_build()
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "agent.json").write_text("{}\n", encoding="utf-8")
    foreign = tmp_path / "foreign_manifest.json"
    foreign.write_text(json.dumps({"digest": "sha256:deadbeef"}), encoding="utf-8")
    (out / "manifest.json").symlink_to(foreign)

    with pytest.raises(mod.ExportRefused) as caught:
        mod._refuse_unless_this_build_wrote_it(out, "--out", "frontdesk")
    assert "a shape this build never writes" in str(caught.value)
    assert foreign.is_file(), "the planted symlink was followed and its target read"


@_posix_only
def test_a_genuine_prior_bundle_still_passes_the_ownership_gate(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a real prior bundle this tool wrote clears the gate.

    The refusal above must be the redirect, not a blanket refusal -- a bundle this build
    produced, read at its own real files, is accepted so a rebuild over it can proceed.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    work = tmp_path / "work"
    _build(mod, home, work, {"skills": {"faq"}})

    mod._refuse_unless_this_build_wrote_it(work / "bundle", "--out", "frontdesk")


@_posix_only
def test_MUTATION_without_the_shape_scan_a_symlinked_plan_read_is_followed(
    tmp_path: pathlib.Path,
) -> None:
    """Drop the reparse-point verdict and the symlinked plan read is followed into the target.

    Reddens the guard: with ``_is_shape_this_build_never_writes`` mutated to NOT answer True
    for a reparse point, the leaf symlink is not refused by the shape scan, the plan-only branch
    reads ``curation-plan.json`` through the link, and the foreign plan's plan_version+crew
    satisfy the ownership check -- so the gate returns without refusing. The real verdict is
    what stops the read.
    """
    mod = load_build(
        mutate=(
            "    if _is_redirecting_entry(p):\n"
            "        # ``is_symlink()`` was the test here and it is too narrow: a Windows JUNCTION is a\n"
            "        # reparse point that is not reported as a symlink, and ``shutil.rmtree`` traverses one\n"
            "        # on Windows rather than unlinking it as it does a symlink. So a junction planted\n"
            "        # inside the output directory turned the recursive delete loose on its target.\n"
            "        return True\n"
            "    return not p.is_file() and not p.is_dir()",
            "    return not p.is_file() and not p.is_dir()",
        )
    )
    out = tmp_path / "bundle"
    out.mkdir()
    foreign = tmp_path / "foreign_plan.json"
    foreign.write_text(
        json.dumps({"plan_version": mod.PLAN_VERSION, "crew": "frontdesk"}), encoding="utf-8"
    )
    (out / mod.PLAN_FILENAME).symlink_to(foreign)

    mod._refuse_unless_this_build_wrote_it(out, "--out", "frontdesk")

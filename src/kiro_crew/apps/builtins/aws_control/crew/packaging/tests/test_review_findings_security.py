"""The five findings the GPT lane raised once its adjudication could run.

All five are the same shape of defect and it is worth naming: the builder exists to stop
untrusted crew content reaching a place it should not, and each of these was a path or a
field it trusted on the way. Each test below reddens if its fix is reverted, and each
mutation is pointed at the exact construct rather than at a substring that also appears
elsewhere.

F1 ``_write_marker_exclusive`` -- the staging marker was written with ``write_text``, so a
   symlink pre-planted at ``<out>.staging.owned`` was followed and its target truncated.

F2 ``_validated_crew_name`` -- ``source / "agents" / f"{name}.json"`` let ``--crew`` carry
   separators, ``..`` or an absolute path, so the spec read came from outside the source.
   Operator-supplied rather than attacker-supplied, so hardening rather than a breach.

F3 ``_open_root_nofollow`` -- the anchor root of the per-component ``O_NOFOLLOW`` walk was
   itself opened following links, so swapping ``<source>/agents`` for a link made every
   check below verify the wrong tree carefully.

F4 ``_marker_is_ours`` -- ownership was ``staging_marker.is_file()``, true of any plain
   file, and it authorised ``shutil.rmtree``. The aside-directory path accepted a
   plan-only directory on the FILENAME alone.

F5 ``build_spec`` -- a non-list ``tools`` skipped the isinstance branch and then hit
   ``set(spec.get("tools") or [])``, raising an uncaught ``TypeError``.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from kiro_crew import credential_patterns

from .test_producer import BUILD_PY, load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _crew(mod, home: pathlib.Path, name: str = "frontdesk"):
    return mod.resolve_crew(name, home)


def _build(mod, home: pathlib.Path, work: pathlib.Path, select=None):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    work.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, work, select=select or {})
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, work / "bundle")


# ---------------------------------------------------------------------------
# F1: the marker write must not follow a planted link
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_a_planted_marker_symlink_is_refused_and_the_target_survives(
    tmp_path: pathlib.Path,
) -> None:
    """A link at the marker path stops the build, and the victim keeps its bytes.

    The refusal is the part that changed. An earlier version of the fix quietly wrote
    somewhere else and let the build finish, which leaves the operator with a green build
    and an attacker-chosen path in their directory. A link at a path derived from ``--out``
    is a signal, not an obstacle to route around.

    Both halves matter: the surviving bytes are the security property, and the refusal is
    what makes the situation visible to whoever ran the build.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    victim = tmp_path / "precious.txt"
    victim.write_bytes(b"do not truncate me\n")
    (work / "bundle.staging.owned").symlink_to(victim)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work)
    assert "symlink" in str(caught.value).lower(), str(caught.value)
    assert victim.read_bytes() == b"do not truncate me\n", "the planted link was followed"


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_MUTATION_a_write_text_marker_truncates_the_link_target(tmp_path: pathlib.Path) -> None:
    """Restore ``write_text`` for the marker and the victim is destroyed.

    This is the defect reproduced. It pins that the fd-based write is what protects the
    target, not something else in the surrounding checks.
    """
    anchor = "    _write_nofollow(path, _STAGING_MARKER_BODY, exclusive=not ours)"
    assert (
        BUILD_PY.read_text(encoding="utf-8").count(anchor) == 1
    ), "the mutation anchor moved or is not unique; re-point it at the marker write"
    mod = load_build(
        mutate=(anchor, '    path.write_text(_STAGING_MARKER_BODY, encoding="utf-8", newline="")')
    )
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    victim = tmp_path / "precious.txt"
    victim.write_bytes(b"do not truncate me\n")
    (work / "bundle.staging.owned").symlink_to(victim)

    _build(mod, home, work)

    assert (
        victim.read_bytes() != b"do not truncate me\n"
    ), "the mutation did not reach the marker write, so this test proves nothing"


@_posix_only
def test_a_successful_build_still_leaves_no_marker(tmp_path: pathlib.Path) -> None:
    """The exclusive write must not break the cleanup the old write had.

    A marker left behind is a licence for the NEXT run to delete whatever is at that path,
    so this property is why the marker exists at all.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    _build(mod, home, work)
    assert not (work / "bundle.staging.owned").exists()


# ---------------------------------------------------------------------------
# F2: a crew name is a name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "..", "a/b", "a\\b", "/absolute", "", "sub/../../out"],
)
def test_a_crew_name_that_can_address_a_path_is_refused(name, tmp_path: pathlib.Path) -> None:
    """Every rejected shape, so a partial fix cannot pass.

    ``..`` and ``a/b`` are the two the join actually resolved: ``Path.__truediv__`` treats
    an absolute segment as a new root and ``..`` as a parent step, so the read left the
    source the operator named.
    """
    mod = load_build()
    with pytest.raises(mod.ExportRefused):
        mod.resolve_crew(name, tmp_path)


def test_an_ordinary_crew_name_still_resolves(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the check must not have become a blanket refusal.

    Names with dots, dashes and unicode are legal filenames and legal crew names; only the
    path-addressing shapes are refused.
    """
    for name in ["frontdesk", "front.desk", "front-desk_2", "cafe-brulee"]:
        crew = mod_resolve(tmp_path, name)
        assert crew.agent_spec_path.name == f"{name}.json"
        assert crew.agent_spec_path.parent.name == "agents"


def mod_resolve(root: pathlib.Path, name: str):
    return load_build().resolve_crew(name, root)


# ---------------------------------------------------------------------------
# F3: the anchor root itself must not be a link
# ---------------------------------------------------------------------------
@_posix_only
def test_a_prompt_inside_a_real_agents_directory_still_inlines(
    tmp_path: pathlib.Path,
) -> None:
    """The end-to-end path the root check sits on must still work."""
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file://persona.md")
    (src / "agents" / "persona.md").write_text("the real persona\n", encoding="utf-8")
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert "the real persona" in result.spec["prompt"]


# ---------------------------------------------------------------------------
# F4: ownership must be more than "a file is here"
# ---------------------------------------------------------------------------
def test_a_foreign_file_at_the_marker_path_does_not_authorise_deletion(
    tmp_path: pathlib.Path,
) -> None:
    """An operator's own note must not license a recursive delete of their own directory.

    This is the forged-token case the old ``is_file()`` accepted. The refusal is what keeps
    ``their_work.txt`` on disk.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    theirs = work / "bundle.staging"
    (theirs / "skills").mkdir(parents=True)
    (theirs / "skills" / "their_work.txt").write_text("hours of it\n", encoding="utf-8")
    (work / "bundle.staging.owned").write_text("a note of mine\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work)
    msg = str(caught.value)
    if os.name == "posix":
        assert "did not create it" in msg
    else:
        assert "POSIX-only" in msg
    assert (theirs / "skills" / "their_work.txt").is_file(), "their file was deleted"


def test_a_plan_only_directory_must_carry_a_plan_this_tool_wrote(tmp_path: pathlib.Path) -> None:
    """The name ``curation-plan.json`` is not proof of origin.

    A plan-only directory is the normal state between the two verbs, so it has to be
    accepted -- which is why the check is on the plan's own ``plan_version`` rather than a
    blanket refusal of the shape.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    out = work / "bundle"
    out.mkdir(parents=True)
    (out / mod.PLAN_FILENAME).write_text("not our plan at all\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work)
    msg = str(caught.value)
    if os.name == "posix":
        assert "did not write" in msg
    else:
        assert "POSIX-only" in msg


def test_the_marker_this_build_writes_is_recognised_as_its_own(tmp_path: pathlib.Path) -> None:
    """Non-vacuity for the token: the writer and the reader must agree.

    If they disagreed, every resume would refuse and the crash-cleanup path this marker
    exists for would be dead -- passing tests, dead feature.
    """
    mod = load_build()
    marker = tmp_path / "bundle.staging.owned"
    mod._write_marker_exclusive(marker)
    assert mod._marker_is_ours(marker)
    assert marker.read_text(encoding="utf-8").startswith(mod._STAGING_MARKER_TOKEN)


# ---------------------------------------------------------------------------
# F5: a malformed tools field gets a reason, not a traceback
# ---------------------------------------------------------------------------
@_posix_only
@pytest.mark.parametrize("field", ["tools", "allowedTools"])
@pytest.mark.parametrize("value", [3, "fs_read", {"a": 1}, True])
def test_a_non_list_tool_field_is_refused_not_crashed(field, value, tmp_path: pathlib.Path) -> None:
    """``ExportRefused`` naming the field, rather than ``TypeError`` from a set().

    Both fields and several shapes, because the old guard was ``isinstance(list)`` on one
    of them: a truthy non-iterable skipped that branch and reached the set() below it.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    spec_path = home / "agents" / "frontdesk.json"
    body = json.loads(spec_path.read_text(encoding="utf-8"))
    body[field] = value
    spec_path.write_text(json.dumps(body), encoding="utf-8")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert field in str(caught.value)


@_posix_only
def test_a_list_tool_field_still_works(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: the shape check must not refuse the normal spec."""
    mod = load_build()
    home = make_crew(tmp_path / "home", tools=["fs_read"], allowed_tools=["fs_read"])
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["tools"] == ["fs_read"]


# ---------------------------------------------------------------------------
# Round-11 GPT F1: a redirected skills ROOT must be refused, not traversed
#
# The per-entry guard already blocks a redirected SKILL.md and the chain guard
# blocks redirected out/staging/previous paths, but the skills root itself was an
# uncovered variant: a symlinked ``<source>/skills`` makes ``rglob`` enumerate a
# tree outside ``--source`` while every id still reads in-bounds, so files sourced
# elsewhere ship in the bundle.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_a_symlinked_skills_root_is_refused(tmp_path: pathlib.Path) -> None:
    """A skills root that redirects outside --source is refused before enumeration.

    The refusal is what keeps ``outside/secret_skill/SKILL.md`` -- a file the operator
    never placed under the crew source -- out of the candidate list and the bundle.
    """
    mod = load_build()
    # A tree OUTSIDE the crew source, carrying a skill that must never be enumerable.
    outside = tmp_path / "outside"
    (outside / "secret_skill").mkdir(parents=True)
    (outside / "secret_skill" / "SKILL.md").write_text("# not from this crew\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    (home / "agents").mkdir()
    skills_root = home / "skills"
    skills_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.skill_candidates(skills_root)
    assert "link or junction" in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="needs symlink semantics the fix relies on")
def test_MUTATION_a_symlinked_skills_root_would_leak_without_the_guard(
    tmp_path: pathlib.Path,
) -> None:
    """Revert the root-redirect guard and the out-of-source skill becomes enumerable.

    Reddens the fix: with the guard stripped, ``skill_candidates`` follows the link,
    ``rglob`` finds ``secret_skill/SKILL.md`` in the redirected tree, and it appears as a
    selectable candidate whose bytes live outside ``--source``.
    """
    mod = load_build(mutate=("if _is_redirecting_entry(skills_root):", "if False:"))
    outside = tmp_path / "outside"
    (outside / "secret_skill").mkdir(parents=True)
    (outside / "secret_skill" / "SKILL.md").write_text("# not from this crew\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    skills_root = home / "skills"
    skills_root.symlink_to(outside, target_is_directory=True)

    cands = mod.skill_candidates(skills_root)
    assert any(c.id == "secret_skill" for c in cands), (
        "guard stripped: the out-of-source skill should leak into the candidate list, "
        "proving the guard is what blocks it"
    )


# ---------------------------------------------------------------------------
# Round-22 GPT: a non-string ELEMENT of tools/allowedTools must be refused, not
# str()-coerced (tools) or silently dropped (allowedTools). Coercion fabricates a
# capability grant in a SIGNED bundle; a dropped grant is a silent capability
# change. Both invent/alter information -- invalid input gets ExportRefused.
# ---------------------------------------------------------------------------
@_posix_only
@pytest.mark.parametrize("field", ["tools", "allowedTools"])
@pytest.mark.parametrize("bad", [{"name": "fs_read"}, 7, ["nested"], True])
def test_a_non_string_tool_entry_is_refused_not_coerced(field, bad, tmp_path: pathlib.Path) -> None:
    mod = load_build()
    home = make_crew(tmp_path / "home")
    spec_path = home / "agents" / "frontdesk.json"
    body = json.loads(spec_path.read_text(encoding="utf-8"))
    body[field] = ["fs_read", bad]
    spec_path.write_text(json.dumps(body), encoding="utf-8")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    msg = str(caught.value)
    assert field in msg
    assert type(bad).__name__ in msg, "the refusal names the entry's actual type"


@_posix_only
def test_MUTATION_str_coercing_a_tool_entry_would_fabricate_a_grant(tmp_path: pathlib.Path) -> None:
    """Revert to str()-coercion and a dict tool entry becomes a fabricated tool id, not refused."""
    mod = load_build(
        mutate=(
            '        for e in tools:\n            if not isinstance(e, str):\n                raise ExportRefused(\n                    f"\'tools\' contains a {type(e).__name__} entry ({e!r}), not a string. A "\n                    f"tool grant is computed from it and would be fabricated by coercion; the "\n                    f"bundle is signed, so an invented capability cannot be allowed. Fix the "\n                    f"spec."\n                )\n        kept = [e for e in tools if not _is_orphan(e)]\n        orphans = [e for e in tools if _is_orphan(e)]',
            "        kept = [str(e) for e in tools if not _is_orphan(str(e))]\n        orphans = [str(e) for e in tools if _is_orphan(str(e))]",
        )
    )
    home = make_crew(tmp_path / "home")
    spec_path = home / "agents" / "frontdesk.json"
    body = json.loads(spec_path.read_text(encoding="utf-8"))
    body["tools"] = [{"name": "fs_read"}]
    spec_path.write_text(json.dumps(body), encoding="utf-8")

    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    # With the guard mutated off, the dict is str()-coerced into a fabricated tool id and the
    # build does NOT refuse it -- proving the type check is what prevents the invented grant.
    result = mod.build_spec(crew, spec, set(), crew.agent_spec_path.parent)
    assert result.spec["tools"] == ["{'name': 'fs_read'}"], (
        "mutated: a non-string tool entry is coerced into a fabricated tool id in the signed "
        "spec, which the real type-refusal prevents"
    )


#: The shared vendor/token spellings, imported from the single home both the scrubber
#: and this standalone subset read. The test below asks the CANONICAL scrubber for the
#: shortest token it accepts per prefix, then requires the standalone side to accept that
#: same token -- so a standalone bound HIGHER than canonical (a GitLab body of 16..19, an
#: npm body of 24..35 the scrubber redacts and a tighter subset ships) fails the test. A
#: fixed-string sample would not catch that: it passes at any bound at or below its length.
_B62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _prefix_of(fragment: str) -> str:
    """The literal lead of a fragment (``glpat-``, ``sk-proj-``, ...), up to its class."""
    return fragment[: fragment.index("[")]


def _body(length: int) -> str:
    """``length`` base62 characters -- valid for every class these fragments use."""
    return (_B62 * ((length // len(_B62)) + 1))[:length]


def _canonical_min_body_length(prefix: str, canonical, ceiling: int = 80) -> int | None:
    """Shortest base62 body length canonical accepts after ``prefix``, or None if never.

    Probes upward, so it measures canonical's OWN floor rather than trusting a restated
    one. The standalone side is then required to catch a token at exactly this length.
    """
    for n in range(1, ceiling + 1):
        token = prefix + _body(n)
        if any(rx.search(token) for rx in canonical):
            return n
    return None


@pytest.mark.parametrize("label,fragment", credential_patterns.VENDOR_TOKEN_PATTERNS)
def test_the_standalone_scan_catches_each_vendor_token_at_canonical_minimum(
    label: str, fragment: str
) -> None:
    """Per-format, at CANONICAL's minimum, so a standalone bound above canonical is caught.

    The standalone fallback is the REAL scan path in the deployment venv where the scrubber
    is not importable, so a token the scrubber redacts but the subset misses ships unscanned.
    The length is measured FROM canonical (probed, not restated), and the standalone side
    must catch a token at that length: a subset bound tighter than canonical reddens here,
    naming the exact format. This is the smaller-order form of the missed-format finding.
    """
    from kiro_crew.security import get_credential_patterns

    mod = load_build()
    canonical = get_credential_patterns()
    prefix = _prefix_of(fragment)
    min_len = _canonical_min_body_length(prefix, canonical)
    if min_len is None:
        pytest.skip(f"canonical scrubber does not carry a {label!r}-prefixed pattern")
    token = prefix + _body(min_len)
    assert any(rx.search(token) for _, rx in mod._HARD_PATTERNS), (
        f"the standalone scan misses a {label} token at canonical's own minimum length "
        f"({len(token)} chars); its bound has drifted ABOVE canonical and would ship a "
        "token the scrubber redacts"
    )


def test_an_ordinary_dotted_identifier_is_not_a_false_vendor_token() -> None:
    """Non-vacuity: the vendor patterns must not paint every hyphenated word a secret."""
    mod = load_build()
    for benign in ("just-a-normal-identifier", "sk-short", "npm-run-build"):
        assert not any(
            rx.search(benign) for _, rx in mod._HARD_PATTERNS
        ), f"{benign!r} is not a credential but the standalone scan flagged it"


# ---------------------------------------------------------------------------
# The ``already_resolved=True`` pinned open at the _inline_prompt anchor site
# (build.py:3013) refuses a component swapped for a symlink between the caller's
# resolve and this open.
#
# ``already_resolved=True`` skips only the re-resolution -- it does NOT skip the
# per-component ``O_NOFOLLOW`` walk, which opens EVERY component of the passed
# value descriptor-relative with ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``. A component
# that becomes a symlink after the value was computed fails its OWN open, and no
# path string is re-resolved once the walk starts. The two tests below prove the
# refusal and, by mutation, that ``O_NOFOLLOW`` is what enforces it.
# ---------------------------------------------------------------------------
@_posix_only
def test_already_resolved_pinned_open_refuses_a_post_resolve_component_swap(
    tmp_path: pathlib.Path,
) -> None:
    """A parent swapped for a symlink AFTER the resolve is refused, not followed.

    Reproduces the finding's exact scenario: a caller resolves ``<base>/mid/leaf`` while
    every component is a real directory, then ``mid`` is replaced with a symlink to an
    attacker directory before the anchor open runs. ``_open_dir_nofollow_pinned`` is called
    with ``already_resolved=True`` -- the flag the finding names -- and must refuse.
    """
    mod = load_build()
    base = tmp_path / "base"
    (base / "mid" / "leaf").mkdir(parents=True)
    victim = tmp_path / "victim"
    (victim / "leaf").mkdir(parents=True)

    # The value a caller resolved BEFORE the swap, all real directories at that instant.
    resolved = (base / "mid" / "leaf").resolve()

    # The swap the finding describes: an intermediate component becomes a link out of tree.
    (base / "mid" / "leaf").rmdir()
    (base / "mid").rmdir()
    (base / "mid").symlink_to(victim, target_is_directory=True)

    # Confirm the swap DID redirect the name, so a naive open-by-string would land in victim.
    assert (base / "mid" / "leaf").resolve() == (victim / "leaf").resolve()

    with pytest.raises(OSError):
        fd = mod._open_dir_nofollow_pinned(resolved, already_resolved=True)
        os.close(fd)  # unreachable if the refusal holds; closes the leak if it does not


@_posix_only
def test_a_clean_resolved_anchor_still_opens(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: an untouched resolved anchor opens, so the refusal above is the swap."""
    mod = load_build()
    anchor = tmp_path / "base" / "mid" / "leaf"
    anchor.mkdir(parents=True)
    fd = mod._open_dir_nofollow_pinned(anchor.resolve(), already_resolved=True)
    try:
        assert os.fstat(fd).st_ino == os.stat(anchor).st_ino
    finally:
        os.close(fd)


@_posix_only
def test_MUTATION_dropping_O_NOFOLLOW_would_follow_the_swapped_parent(
    tmp_path: pathlib.Path,
) -> None:
    """Strip ``O_NOFOLLOW`` from the pinned walk and the swapped parent is followed.

    Reddens the guard: with the flag gone, the open of ``mid`` follows the link into
    ``victim`` and the walk reaches ``victim/leaf`` and returns a descriptor -- exactly the
    hole the per-component ``O_NOFOLLOW`` closes. The mutation anchor pins the two-line block
    inside ``_open_dir_nofollow_pinned`` (the ``resolved =`` line is unique to that function),
    so it cannot land on the identically-worded ``dir_flags`` line elsewhere in the module.
    """
    mod = load_build(
        mutate=(
            "    resolved = dir_path if already_resolved else dir_path.resolve()\n"
            '    dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)',
            "    resolved = dir_path if already_resolved else dir_path.resolve()\n"
            "    dir_flags = os.O_RDONLY | os.O_DIRECTORY",
        )
    )
    base = tmp_path / "base"
    (base / "mid" / "leaf").mkdir(parents=True)
    victim = tmp_path / "victim"
    (victim / "leaf").mkdir(parents=True)
    resolved = (base / "mid" / "leaf").resolve()
    (base / "mid" / "leaf").rmdir()
    (base / "mid").rmdir()
    (base / "mid").symlink_to(victim, target_is_directory=True)

    fd = mod._open_dir_nofollow_pinned(resolved, already_resolved=True)
    try:
        # The descriptor is victim/leaf, reached by following the swapped link -- the leak
        # the real O_NOFOLLOW walk refuses.
        assert os.fstat(fd).st_ino == os.stat(victim / "leaf").st_ino, (
            "mutated: without O_NOFOLLOW the walk should follow the swapped parent into the "
            "attacker directory, proving the flag is what blocks the swap"
        )
    finally:
        os.close(fd)


@_posix_only
def test_a_plan_only_directory_for_another_crew_is_not_owned(tmp_path: pathlib.Path) -> None:
    """A version field is not an ownership claim; the crew the plan names has to match.

    The plan-only directory is the state between the two verbs, and it is deleted
    recursively if it is treated as this build's own staging tree. A curation-plan.json
    that carries the right ``plan_version`` but names a different crew is a foreign file, so
    it must not license that delete, and the foreign file survives the refusal.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home")
    work = tmp_path / "work"
    out = work / "bundle"
    out.mkdir(parents=True)
    (out / mod.PLAN_FILENAME).write_text(
        json.dumps({"plan_version": mod.PLAN_VERSION, "crew": "someone-elses-crew"}),
        encoding="utf-8",
    )
    before = (out / mod.PLAN_FILENAME).read_text(encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, work)
    assert "did not write" in str(caught.value)
    assert (out / mod.PLAN_FILENAME).is_file(), "the foreign plan-only directory was deleted"
    assert (out / mod.PLAN_FILENAME).read_text(encoding="utf-8") == before


@_posix_only
def test_bundle_digest_refuses_a_staged_leaf_swapped_for_a_symlink(
    tmp_path: pathlib.Path,
) -> None:
    """A staged leaf swapped for a symlink is refused at hashing, not hashed through.

    ``bundle_digest`` signs the manifest and is re-derived to prove ownership before a
    recursive delete, so a leaf that becomes a symlink between the file-shape check and the
    read must not fold the target's bytes into the digest. The read is held through one
    no-follow descriptor, so the redirect fails the open and the digest refuses rather than
    pinning bytes from wherever the link points. A tree of only regular files still hashes.
    """
    mod = load_build()
    root = tmp_path / "bundle"
    (root / "skills").mkdir(parents=True)
    (root / "agent.json").write_text('{"name": "frontdesk"}\n', encoding="utf-8")
    leaf = root / "skills" / "SKILL.md"
    leaf.write_text("# real\n", encoding="utf-8")

    good = mod.bundle_digest(root)
    assert good.startswith("sha256:"), "an all-regular-file tree must still hash"

    outside = tmp_path / "outside.txt"
    outside.write_text("ATTACKER BYTES\n", encoding="utf-8")
    leaf.unlink()
    leaf.symlink_to(outside)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.bundle_digest(root)
    msg = str(caught.value)
    assert "skills/SKILL.md" in msg, "the refusal must name the redirecting leaf"
    assert (
        "link or junction" in msg or "following a redirect" in msg or "no-follow descriptor" in msg
    ), "the refusal must be about a redirect, not a hash of the link target"


# ---------------------------------------------------------------------------
# A skill file that is a HARD LINK to a file outside the skill must be refused,
# not copied into the signed bundle. The name and location checks clear a file
# by its PATH; a hard link gives an outside file a second innocent name inside
# the skill, so its bytes ship while the path reads clean. ``st_nlink > 1`` on
# the opened descriptor is the identity a name check cannot see, which is why the
# read routes through ``hooks.safe_read_file_bytes_nolink``.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_hard_linked_skill_file_is_refused_and_names_the_file(
    tmp_path: pathlib.Path,
) -> None:
    """A skill file hard-linked to a file outside the skill is refused, not copied.

    The content of the outside file is deliberately benign, so the refusal cannot come from
    the credential scan -- it is the hard-link identity (``st_nlink > 1``) that stops it. The
    refusal names the file, so an operator sees which member and why rather than a silent
    omission, and nothing from the skill's hard-linked member reaches ``dest``.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"

    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    hard_link = skill_dir / "notes.md"
    os.link(outside, hard_link)
    assert hard_link.stat().st_nlink > 1, "test setup: the member must be a hard link"

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(mod.ExportRefused) as caught:
        mod._copy_skill(skill_dir, "leaky", dest)
    assert "notes.md" in str(caught.value), "the refusal must name the offending file"
    assert not [p for p in dest.rglob("notes.md")], "the hard-linked member reached the bundle"


@_posix_only
def test_a_special_file_in_a_skill_is_refused_at_copy_not_silently_omitted(
    tmp_path: pathlib.Path,
) -> None:
    """A FIFO (or socket/device) member is refused and named, not dropped from the bundle.

    The copy walks every entry; a non-regular, non-directory entry cannot be read as text,
    scanned for credentials, or certified clean, so omitting it makes ``unshippable`` look
    like ``not there`` -- the cannot-be-judged-means-not-present substitution. The refusal
    names the member so an operator sees which one and why, rather than shipping a skill whose
    curation plan showed it selectable.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"
    fifo = skill_dir / "pipe"
    os.mkfifo(fifo)

    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(mod.ExportRefused) as caught:
        mod._copy_skill(skill_dir, "leaky", dest)
    assert "pipe" in str(caught.value), "the refusal must name the special-file member"
    assert not [p for p in dest.rglob("pipe")], "the special file reached the bundle"


@_posix_only
def test_an_ordinary_regular_skill_file_still_copies(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a plain single-name skill file copies byte-for-byte.

    The guard must not have become a blanket refusal -- an ordinary regular file with one
    name is read and written unchanged, so the hard-link refusal above is the hard link.
    """
    mod = load_build()
    body = "# faq\nhours are 9 to 5\n"
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": body}})
    dest = tmp_path / "dest"
    dest.mkdir()

    written = mod._copy_skill(src / "skills" / "faq", "faq", dest)

    assert "SKILL.md" in written
    staged = dest / "faq" / "SKILL.md"
    assert staged.read_bytes() == body.encode("utf-8"), "bytes must survive the read unchanged"


@_posix_only
def test_MUTATION_a_by_name_read_ships_a_hard_linked_skill_file(
    tmp_path: pathlib.Path,
) -> None:
    """Revert to a by-name read and the hard-linked member ships instead of being refused.

    Reddens the fix: ``_read_bytes_openat`` reads the leaf ``O_NOFOLLOW`` but never fstats
    for ``st_nlink``, so a hard link passes and its bytes are copied into the bundle. The
    mutation anchor is the guarded-read call, unique to the skill-copy site.
    """
    mod = load_build(
        mutate=(
            "safe_read_file_bytes_nolink(str(p), str(skill_dir), max_bytes=_MAX_PROMPT_BYTES)",
            "_read_bytes_openat(skill_dir, p.relative_to(skill_dir))",
        )
    )
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"
    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    os.link(outside, skill_dir / "notes.md")

    dest = tmp_path / "dest"
    dest.mkdir()
    mod._copy_skill(skill_dir, "leaky", dest)
    assert [p for p in dest.rglob("notes.md")], (
        "mutated: a by-name read with no st_nlink check ships the hard-linked file, proving "
        "the guard's fstat is what refuses it"
    )


# ---------------------------------------------------------------------------
# The SAME defect at the ENUMERATION site: ``skill_candidates`` scans each skill
# file for credentials on a path that never asked the authority. The name and
# location checks clear a file by its PATH and ``scan_text`` reads content that a
# hard-linked credential need not match, so a hard link to an outside file passed
# the scan and the skill was marked SELECTABLE -- the copy then refuses it, but the
# curation plan already showed it as includable. Routing the scan read through
# ``hooks.safe_read_file_bytes_nolink`` blocks the candidate here, where the plan is
# written, because ``st_nlink > 1`` on the opened descriptor is the identity neither
# a name check nor ``scan_text`` can see.
# ---------------------------------------------------------------------------
@_posix_only
def test_a_hard_linked_skill_file_blocks_the_candidate_at_enumeration(
    tmp_path: pathlib.Path,
) -> None:
    """A skill carrying a hard-linked outside file is blocked, not marked selectable.

    The outside file's content is deliberately benign, so the refusal cannot come from
    ``scan_text`` -- it is the hard-link identity (``st_nlink > 1``) that stops it. The
    blocked candidate names the offending file and carries no content hash, so the skill
    cannot be included in a plan rather than looking includable and only failing at copy.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"

    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    hard_link = skill_dir / "notes.md"
    os.link(outside, hard_link)
    assert hard_link.stat().st_nlink > 1, "test setup: the member must be a hard link"

    cands = mod.skill_candidates(src / "skills")
    leaky = next(c for c in cands if c.id == "leaky")
    assert leaky.blocked, "a hard-linked member must block the candidate at enumeration"
    assert "notes.md" in leaky.blocked, "the block reason must name the offending file"
    assert leaky.content_hash == "", "a blocked candidate must carry no content pin"


@_posix_only
def test_a_hard_linked_SKILL_md_itself_blocks_at_the_probe_read(tmp_path: pathlib.Path) -> None:
    """A SKILL.md hard-linked to an outside file is blocked at the UTF-8 probe, not decoded.

    The probe that decides whether SKILL.md is readable UTF-8 reads through
    ``safe_read_file_bytes_nolink``, so a SKILL.md that is itself a hard link to a file
    with a second name (a credential given the innocent name ``SKILL.md``) is refused at
    that read on ``st_nlink > 1`` -- the identity a no-follow open alone cannot see. The
    bytes are never decoded into the process through the second name, and the skill is
    blocked at enumeration rather than looking selectable. The outside content is valid
    UTF-8 and benign, so a pass here would be the hard-link blindness, not a decode failure.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_md = src / "skills" / "leaky" / "SKILL.md"

    outside = tmp_path / "outside_secret"
    outside.write_text("valid utf-8 bytes that live outside the skill\n", encoding="utf-8")
    skill_md.unlink()
    os.link(outside, skill_md)
    assert skill_md.stat().st_nlink > 1, "test setup: SKILL.md must be a hard link"

    cands = mod.skill_candidates(src / "skills")
    leaky = next(c for c in cands if c.id == "leaky")
    assert leaky.blocked, "a hard-linked SKILL.md must block the candidate at the probe read"
    assert leaky.content_hash == "", "a blocked candidate must carry no content pin"


@_posix_only
def test_MUTATION_a_bare_probe_read_would_decode_a_hard_linked_SKILL_md(
    tmp_path: pathlib.Path,
) -> None:
    """Revert the probe to the bare descriptor read and the probe stops catching the hard link.

    Reddens the fix from the probe's side. With the probe back on ``_read_text_openat`` (no
    ``st_nlink`` check), the hard-linked SKILL.md decodes cleanly and PASSES the probe -- so the
    block, if any, comes from a later guard rather than the probe. The candidate is still
    blocked, because the later unconditional scan through ``safe_read_file_bytes_nolink`` catches
    ``st_nlink > 1`` (this is why the credential never ships either way), but the probe stops
    being the guard that refuses the read. The fix keeps the refusal AT the probe so the read
    itself is authority-consistent; this asserts that with the bare probe the block reason is
    the SCAN's wording, not the probe's, proving which line does the catching.
    """
    mod = load_build(
        mutate=(
            "        try:\n            _probe = safe_read_file_bytes_nolink(\n"
            "                str(skill_md), str(skills_root), max_bytes=_MAX_PROMPT_BYTES\n"
            "            )\n        except FileTooLargeError:\n            _probe = None",
            "        _probe = (\n"
            "            None\n"
            "            if _read_text_openat(skills_root, skill_md.relative_to(skills_root))\n"
            "            is None\n"
            "            else b'ok'\n"
            "        )",
        )
    )
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_md = src / "skills" / "leaky" / "SKILL.md"
    outside = tmp_path / "outside_secret"
    outside.write_text("valid utf-8 bytes that live outside the skill\n", encoding="utf-8")
    skill_md.unlink()
    os.link(outside, skill_md)

    cands = mod.skill_candidates(src / "skills")
    leaky = next(c for c in cands if c.id == "leaky")
    # Still blocked -- by the later scan, not the probe. The probe's own wording ("UTF-8 text
    # the guard can certify") is ABSENT, and the scan's ("file-read guard refuses") is present.
    assert leaky.blocked, "the later scan still catches the hard link, so it never ships"
    assert "the guard can certify" not in (leaky.blocked or ""), (
        "with the bare probe read the hard link is NOT caught at the probe -- proving the "
        "safe_read_file_bytes_nolink probe is what refuses st_nlink>1 at the read itself"
    )


@_posix_only
def test_an_ordinary_skill_still_enumerates_with_a_content_hash(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a skill of ordinary single-name files enumerates selectable.

    The guard must not have become a blanket refusal -- a plain regular file is scanned,
    clears, and the skill gets a content hash, so the hard-link refusal above is the hard
    link and not the read.
    """
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"faq": {"SKILL.md": "# faq\nhours are 9 to 5\n", "extra.md": "no secrets here\n"}},
    )

    cands = mod.skill_candidates(src / "skills")
    faq = next(c for c in cands if c.id == "faq")
    assert not faq.blocked, "an ordinary skill must not be blocked"
    assert faq.content_hash.startswith("sha256:") or faq.content_hash, "a clean skill is pinned"


@_posix_only
def test_MUTATION_a_by_name_enumeration_scan_marks_a_hard_linked_skill_selectable(
    tmp_path: pathlib.Path,
) -> None:
    """Revert both enumeration-time reads to a by-name read and the hard-linked skill goes selectable.

    Reddens the fix: two reads clear a skill at enumeration -- the credential scan and the
    content-pin read in ``_tree_hash`` -- and both open the leaf through the authority that
    fstats for ``st_nlink``. A by-name read (``_read_bytes_openat`` for the scan, ``read_bytes``
    for the pin) never fstats, so a hard link passes, its benign bytes clear ``scan_text``, and
    the candidate is enumerated with a content hash instead of blocked. Reverting either read
    alone leaves the other blocking the hard link, so both are reverted here to observe the
    leak; the two anchors are distinct by their ``str(skill_dir)`` scan form and the ``str(root)``
    pin form.
    """
    mod = load_build(
        mutate=[
            (
                "scanned = safe_read_file_bytes_nolink(\n"
                "                    str(p), str(skill_dir), max_bytes=_MAX_PROMPT_BYTES\n"
                "                )",
                "scanned = _read_bytes_openat(skill_dir, p.relative_to(skill_dir))",
            ),
            (
                "safe_read_file_bytes_nolink(str(p), str(root), max_bytes=_MAX_PROMPT_BYTES)",
                "p.read_bytes()",
            ),
        ]
    )
    src = make_crew(tmp_path / "home", skills={"leaky": {"SKILL.md": "# ok\n"}})
    skill_dir = src / "skills" / "leaky"
    outside = tmp_path / "outside_secret"
    outside.write_text("shared bytes that live outside the skill\n", encoding="utf-8")
    os.link(outside, skill_dir / "notes.md")

    cands = mod.skill_candidates(src / "skills")
    leaky = next(c for c in cands if c.id == "leaky")
    assert not leaky.blocked and leaky.content_hash, (
        "mutated: a by-name enumeration scan with no st_nlink check marks the hard-linked "
        "skill selectable, proving the guard's fstat is what blocks it"
    )


# ---------------------------------------------------------------------------
# The vendor-token bounds and word boundaries, answered with a test rather than
# a regex change.
#
# A real PyPI token's base64 body is never shorter than 85 characters (PyPI's own
# docs, docs.pypi.org/api/secrets), so the ``pypi-...{16,}`` floor matches every
# real token; a 15-character body is a truncated non-token, below the floor by
# design, and its miss is not evidence of a wrong bound. Separately, the ``\b``
# word boundary drops no supported token to a trailing hyphen: for the formats
# whose character class excludes ``-`` the boundary sits between the last body
# character and the hyphen, so a token followed by a hyphen matches exactly as the
# bare token does. Replacing ``\b`` with a token-char lookaround would instead
# treat the hyphen as a body character and MASK it, so the boundary form is kept.
# ---------------------------------------------------------------------------
def test_the_standalone_scan_catches_a_realistic_length_pypi_token() -> None:
    """A PyPI token at a real body length is caught; a truncated 15-char shape is not.

    A real PyPI token body is >=85 base64 characters per PyPI docs, so the ``{16,}`` floor
    matches every real token -- the body built here is 90 characters, comfortably above that
    minimum. The 15-character shape is under the floor by design and is a truncated
    non-token, so its miss says nothing about the bound.
    """
    mod = load_build()
    realistic = "pypi-" + _body(90)
    assert any(rx.search(realistic) for _, rx in mod._HARD_PATTERNS), (
        "the standalone scan misses a realistic-length PyPI token; its body is 90 chars, "
        "well above the >=85 a real PyPI token carries"
    )
    truncated = "pypi-" + _body(15)
    assert not any(
        rx.search(truncated) for _, rx in mod._HARD_PATTERNS
    ), "a 15-char body is below the {16,} floor and is a truncated non-token"


@pytest.mark.parametrize(
    "label,body_len",
    [("vendor-key", 24), ("github-fine-grained-pat", 44), ("npm-token", 28)],
)
def test_a_trailing_hyphen_still_catches_a_supported_vendor_token(
    label: str, body_len: int
) -> None:
    """For the formats whose class excludes ``-``, a trailing hyphen leaves the token caught.

    This answers the word-boundary concern directly: the ``\\b`` sits between the last body
    character and a trailing hyphen, so a token followed by ``-`` matches exactly as the bare
    token does. The body length used is above each format's own floor so the bare token is a
    valid match to begin with. A token-char lookaround would treat the hyphen as a body
    character and mask it, so the boundary form is the safer one.
    """
    mod = load_build()
    fragment = dict(credential_patterns.VENDOR_TOKEN_PATTERNS)[label]
    token = _prefix_of(fragment) + _body(body_len)
    assert any(
        rx.search(token) for _, rx in mod._HARD_PATTERNS
    ), f"the standalone scan misses a bare {label} token at a valid length"
    assert any(rx.search(token + "-") for _, rx in mod._HARD_PATTERNS), (
        f"a trailing hyphen dropped a {label} token whose class excludes '-'; the word "
        "boundary should still match it"
    )


# ---------------------------------------------------------------------------
# The digest walk must REFUSE any entry it cannot judge as a readable regular
# file, not skip it: a skipped entry still SHIPS, so a redirect or a special
# file left out of the walk is content the signed manifest never covered. Only a
# genuine directory is passed over. The existing
# ``test_bundle_digest_refuses_a_staged_leaf_swapped_for_a_symlink`` covers a
# leaf that is a symlink to a FILE; these cover a symlink to a DIRECTORY and a
# special file, the two shapes a bare ``is_file()`` skip let ship unsigned.
# ---------------------------------------------------------------------------
def _digest_tree(root: pathlib.Path) -> None:
    (root / "skills").mkdir(parents=True)
    (root / "agent.json").write_text('{"name": "frontdesk"}\n', encoding="utf-8")
    (root / "skills" / "SKILL.md").write_text("# real\n", encoding="utf-8")


@_posix_only
def test_bundle_digest_refuses_a_symlink_to_a_directory(tmp_path: pathlib.Path) -> None:
    """A staged entry that is a symlink to a directory is refused, not silently skipped."""
    mod = load_build()
    root = tmp_path / "bundle"
    _digest_tree(root)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "loot.txt").write_text("ATTACKER\n", encoding="utf-8")
    (root / "skills" / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.bundle_digest(root)
    assert "skills/linked" in str(caught.value), "the refusal must name the redirecting entry"


@_posix_only
def test_bundle_digest_refuses_a_special_file(tmp_path: pathlib.Path) -> None:
    """A FIFO in the staged tree is refused (and does not hang: the read is non-blocking)."""
    mod = load_build()
    root = tmp_path / "bundle"
    _digest_tree(root)
    os.mkfifo(root / "skills" / "pipe")

    with pytest.raises(mod.ExportRefused) as caught:
        mod.bundle_digest(root)
    assert "skills/pipe" in str(caught.value), "the refusal must name the special file"


@_posix_only
def test_bundle_digest_still_hashes_a_directory_and_regular_files(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: a tree of genuine directories and regular files hashes as before."""
    mod = load_build()
    root = tmp_path / "bundle"
    _digest_tree(root)
    (root / "skills" / "faq").mkdir()
    (root / "skills" / "faq" / "SKILL.md").write_text("# faq\n", encoding="utf-8")

    digest = mod.bundle_digest(root)
    assert digest.startswith("sha256:")


@_posix_only
def test_MUTATION_a_redirect_ships_unsigned_without_the_digest_refuse(
    tmp_path: pathlib.Path,
) -> None:
    """Flip the redirect refuse to a skip and a redirect drops out of the signed digest.

    With the redirect ``raise`` turned into a ``continue``, a symlink to a directory is passed
    over before the shape check ever sees it, so the digest over a tree carrying it equals the
    digest of the same tree with that entry gone -- the link is signed by nothing. The real
    guard refuses instead.
    """
    real = load_build()
    mut = load_build(
        mutate=(
            "raise ExportRefused(\n"
            '                f"the bundle file {rel} is a link or junction; refusing to sign '
            'a digest that "\n'
            '                f"would leave it out of the signed set or fold in bytes reached '
            'by following "\n'
            '                f"it. Re-run the build."\n'
            "            )",
            "continue",
        )
    )
    root = tmp_path / "bundle"
    _digest_tree(root)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "loot.txt").write_text("ATTACKER\n", encoding="utf-8")
    (root / "skills" / "extra").symlink_to(outside, target_is_directory=True)

    with pytest.raises(real.ExportRefused):
        real.bundle_digest(root)

    with_link = mut.bundle_digest(root)
    (root / "skills" / "extra").unlink()
    without_link = mut.bundle_digest(root)
    assert with_link == without_link, "the redirect was skipped, so it shipped outside the digest"

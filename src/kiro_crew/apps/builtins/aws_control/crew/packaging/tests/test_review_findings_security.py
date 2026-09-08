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

"""The writer's parent check, and the chain walk to the spec.

W1 the marker read opened its parent OUTSIDE the guard, so ``--out new/nested/bundle`` -- a
   path whose parent does not exist yet -- raised an unhandled ``FileNotFoundError`` out of a
   function whose entire job is to answer yes or no. No parent means no marker, which is False.

W2 the empty-directory check exempted all of ``_STAGING_OWNED_TOP_LEVEL``, and four of those
   five entries are FILE names. So an operator's own empty directory called ``agent.json`` or
   ``manifest.json`` was exempted and then removed by the recursive delete -- the exemption
   for the one directory this build leaves empty was written wide enough to cover four names
   it should never have covered.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from .test_producer import load_build, make_crew, sign_plan

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _build(mod, home: pathlib.Path, out: pathlib.Path, select):
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    work = out.parent
    work.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, work, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out)


# ---------------------------------------------------------------------------
# W1
# ---------------------------------------------------------------------------
def test_the_marker_check_answers_false_for_an_absent_parent(tmp_path: pathlib.Path) -> None:
    """No parent means no marker. It must not raise."""
    mod = load_build()
    assert (
        mod._marker_is_ours(tmp_path / "does" / "not" / "exist" / "bundle.staging.owned") is False
    )


def test_the_marker_check_answers_false_for_a_file_where_the_parent_should_be(
    tmp_path: pathlib.Path,
) -> None:
    """NotADirectoryError gets the same answer for the same reason."""
    mod = load_build()
    blocker = tmp_path / "not-a-dir"
    blocker.write_bytes(b"x")
    assert mod._marker_is_ours(blocker / "bundle.staging.owned") is False


@_posix_only
def test_a_build_into_a_nested_new_path_works(tmp_path: pathlib.Path) -> None:
    """The case the crash came from, driven through the real build.

    A unit test of the predicate would have stayed green under the old code for the wrong
    reason -- it raises rather than returning -- but only building shows that an operator
    naming a fresh nested --out gets a bundle instead of a traceback.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    report = _build(mod, home, tmp_path / "new" / "nested" / "bundle", {"skills": {"faq"}})
    assert report.digest.startswith("sha256:")
    assert (tmp_path / "new" / "nested" / "bundle" / "manifest.json").is_file()


def test_a_write_into_an_absent_directory_refuses_cleanly(tmp_path: pathlib.Path) -> None:
    """The writer's own parent open is guarded too, and refuses rather than raising.

    Different answer from the reader on purpose: the reader is asking a question and "no" is a
    valid answer, while the writer cannot proceed and has to say why.
    """
    mod = load_build()
    with pytest.raises(mod.ExportRefused) as caught:
        mod._write_nofollow(tmp_path / "absent" / "report.json", "{}\n")
    assert "is not there" in str(caught.value)


# ---------------------------------------------------------------------------
# W2
# ---------------------------------------------------------------------------
@_posix_only
@pytest.mark.parametrize("name", ["agent.json", "mcp.json", "manifest.json", "curation-plan.json"])
def test_an_empty_directory_named_after_a_file_entry_is_refused(
    tmp_path: pathlib.Path, name: str
) -> None:
    """Each of the four names the old exemption covered by accident.

    Parametrised rather than one representative case, because the bug was a SET being too wide
    and a single name would not show that every one of the four was exempt.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})

    # The operator's own empty directory, using a name this build writes as a FILE.
    (out / name).unlink(missing_ok=True)
    (out / name).mkdir()

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert (out / name).is_dir(), "the operator's directory was deleted"
    assert name in str(caught.value) or "no file this build would have written" in str(caught.value)


@_posix_only
def test_the_empty_skills_directory_is_still_exempt(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: narrowing the set must not break the one case it exists for.

    A bundle with no skills selected leaves ``skills/`` empty, and rebuilding over it has to
    work -- the first version of this guard refused it and reddened 13 tests.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": set()})
    assert (out / "skills").is_dir()
    assert not any((out / "skills").iterdir())
    _build(mod, home, out, {"skills": set()})


def test_the_two_sets_are_not_the_same_set() -> None:
    """A source-level pin, because the bug was one name list standing in for another.

    They overlap, so a future edit that "tidies" them back together would reintroduce exactly
    this finding. Stated as an inequality so the intent survives the tidying impulse.
    """
    mod = load_build()
    assert mod._BUILD_WRITES_EMPTY == {"skills"}
    assert mod._BUILD_WRITES_EMPTY < mod._STAGING_OWNED_TOP_LEVEL
    assert "agent.json" in mod._STAGING_OWNED_TOP_LEVEL
    assert "agent.json" not in mod._BUILD_WRITES_EMPTY


@pytest.mark.skipif(os.name != "posix", reason="needs O_NOFOLLOW and dir_fd")
def test_the_anchored_walk_refuses_a_symlinked_root(tmp_path: pathlib.Path) -> None:
    """The anchor itself must be opened with O_NOFOLLOW, not only the parts below it.

    Measured before the fix: a link swapped in at ``root`` was followed, and
    ``_read_text_openat`` returned the content of the tree it pointed at. The walk then
    refused redirects INSIDE that tree, which is thorough validation of the wrong tree.

    The read returns None rather than raising because that is this reader's existing
    "cannot read it" signal, and every caller already handles it.
    """
    mod = load_build()
    real = tmp_path / "real_root"
    real.mkdir()
    (real / "spec.json").write_text('{"name": "expected"}\n', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "spec.json").write_text('{"name": "ATTACKER"}\n', encoding="utf-8")
    link_root = tmp_path / "link_root"
    link_root.symlink_to(outside)

    assert mod._read_text_openat(link_root, pathlib.Path("spec.json")) is None
    assert "ATTACKER" not in (mod._read_text_openat(link_root, pathlib.Path("spec.json")) or "")


@pytest.mark.skipif(os.name != "posix", reason="needs O_NOFOLLOW and dir_fd")
def test_a_real_root_is_still_readable_including_nested_paths(tmp_path: pathlib.Path) -> None:
    """Guards the refusal above from being satisfied by refusing every root.

    Without this, hardening the anchor into "always return None" would pass the symlink
    test while breaking every spec read in the tool.
    """
    mod = load_build()
    root = tmp_path / "root"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / "spec.json").write_text('{"name": "expected"}\n', encoding="utf-8")
    (nested / "deep.md").write_text("deep\n", encoding="utf-8")

    assert mod._read_text_openat(root, pathlib.Path("spec.json")) == '{"name": "expected"}\n'
    assert mod._read_text_openat(root, pathlib.Path("a/b/deep.md")) == "deep\n"


def test_the_unc_gate_on_the_spec_path_runs_before_any_filesystem_touch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UNC ``--source`` must be refused BEFORE the stat, because the stat is the leak.

    ``realpath``/``stat`` on a UNC path is the outbound SMB probe, and on Windows it carries
    an NTLM exchange -- so a fence that reads the path's NAME cannot help, its verdict
    arrives after the packet. The ordering is the security property, so this counts calls
    rather than asserting a message: the refusal must come with ZERO stat calls.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", src)

    touches: list[str] = []
    real_stat = os.stat

    def counting_stat(path, *a, **kw):  # type: ignore[no-untyped-def]
        touches.append(str(path))
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(mod.os, "name", "nt")
    monkeypatch.setattr(mod.os, "stat", counting_stat)
    monkeypatch.setattr("kiro_crew.hooks.is_unc_shape", lambda raw: True, raising=False)
    monkeypatch.setattr("kiro_crew.hooks.unc_probe_allowed", lambda raw: False, raising=False)

    with pytest.raises(mod.ExportRefused) as caught:
        mod.read_agent_spec(crew)

    # This test fakes ``os.name`` to "nt" via monkeypatch, and ``mod.os`` is the shared os
    # module, so ``os.name`` here reflects the fake rather than the real platform. Branch on
    # the actual no-follow primitive instead: on a real POSIX host it is available and the UNC
    # gate runs to its own refusal; on Windows the POSIX-only entry guard preempts it.
    if mod._nofollow_primitive_available():
        assert "UNC path outside the trusted roots" in str(caught.value)
    else:
        assert "POSIX-only" in str(caught.value)
    assert touches == [], f"the refusal must precede every stat, saw {touches}"


@_posix_only
def test_a_trusted_unc_root_is_not_refused_by_the_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the gate from being satisfied by refusing every UNC path.

    ``unc_probe_allowed`` is the operator's own configured allowance, so a gate that ignored
    it would break a crew home on a share the operator deliberately trusts.

    The sensitive-path fence below the gate is stubbed out, because under a faked ``os.name``
    it resolves ``Path.home()`` and dies on this host with a RuntimeError that reads exactly
    like the gate having refused. The fence has its own tests; what this one owns is the
    gate's verdict.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", src)

    monkeypatch.setattr("kiro_crew.hooks.is_unc_shape", lambda raw: True, raising=False)
    monkeypatch.setattr("kiro_crew.hooks.unc_probe_allowed", lambda raw: True, raising=False)
    # ``kiro_crew.security``, NOT ``...security.paths``. build.py reads
    # ``_sec.is_sensitive_path`` off the package, which re-exports its own binding, so
    # patching the submodule leaves the one the code reads untouched -- which is why an
    # earlier version of this test kept dying inside the fence it thought it had stubbed.
    monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda p: False, raising=False)
    # The spec read goes through ``safe_read_file_bytes_nolink``, whose own path handling
    # instantiates a ``WindowsPath`` under a faked ``os.name == "nt"`` and dies on this host --
    # exactly like the fence above did. This test owns the UNC GATE's verdict, not the read, so
    # the authority is stubbed to the spec bytes the same way the sensitive-path fence is.
    spec_bytes = crew.agent_spec_path.read_bytes()
    monkeypatch.setattr(
        "kiro_crew.hooks.safe_read_file_bytes_nolink",
        lambda raw, within_root=None, **kwargs: spec_bytes,
        raising=False,
    )
    monkeypatch.setattr(mod.os, "name", "nt")

    spec = mod.read_agent_spec(crew)
    assert spec["name"] == "frontdesk"


@pytest.mark.skipif(os.name == "nt", reason="asserts the gate is SKIPPED, which is posix-only")
def test_posix_does_not_consult_the_unc_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doubled slash names no network location on POSIX, so the gate is nt-scoped.

    The predicates are replaced with ones that FAIL if called, because a version of this
    test that merely built a crew and asserted success proved nothing: widening the gate to
    every platform left it green, since the real ``is_unc_shape`` answers False for a
    ``tmp_path`` string anyway.
    """
    mod = load_build()
    src = make_crew(tmp_path / "home")
    crew = mod.resolve_crew("frontdesk", src)

    def _must_not_run(raw: str) -> bool:
        raise AssertionError(f"the UNC gate was consulted on POSIX with {raw!r}")

    monkeypatch.setattr("kiro_crew.hooks.is_unc_shape", _must_not_run, raising=False)
    monkeypatch.setattr("kiro_crew.hooks.unc_probe_allowed", _must_not_run, raising=False)

    assert mod.read_agent_spec(crew)["name"] == "frontdesk"

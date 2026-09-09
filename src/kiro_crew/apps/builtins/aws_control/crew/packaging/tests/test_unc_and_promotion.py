"""Two ways the bundle builder could reach past its own fences.

* A UNC prompt path was resolved before any check ran. On Windows resolving a UNC path IS
  the outbound SMB probe, so `file:////attacker/share/persona.md` touched the attacker's
  host -- and a Windows SMB touch carries an NTLM exchange. This repo already owns the
  rule (`hooks.is_unc_shape` + `hooks.unc_probe_allowed`, gated before resolution by
  `hooks.validate_file_path`); the builder simply did not consult it.

* Promotion was `rmtree(out_dir)` then `staging.rename(out_dir)`. A failure BETWEEN the two
  left nothing: the previous bundle was already deleted, and the `except BaseException`
  handler then removed staging as well, so the new bundle and the carried signed plan went
  with it. The comment above the swap called it "the last thing that happens", which was
  true of the ordering and false of the atomicity.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from .test_producer import load_build, make_crew

_posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="the crew bundle builder is POSIX-only; guarded off on platforms without an "
    "atomic no-follow primitive (Windows). See the POSIX-only entry guard.",
)


def _crew(mod, tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    return mod.resolve_crew("frontdesk", src)


def _build(mod, crew, out):
    spec = mod.read_agent_spec(crew)
    return mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


# --- the UNC gate ------------------------------------------------------------


class _OsThatSaysWindows:
    """`os` as build.py sees it, reporting nt.

    Patching the real ``os.name`` is too blunt: ``pathlib`` reads it to choose its flavour
    and then refuses with "cannot instantiate 'WindowsPath' on your system", and
    ``Path.home()`` stops working. Replacing only the module-global keeps pathlib real
    while the platform branch takes the Windows path, which is the branch under test.
    """

    name = "nt"

    def __getattr__(self, attr):  # everything else is the genuine module
        return getattr(os, attr)


def _as_windows(monkeypatch, mod):
    monkeypatch.setattr(mod, "os", _OsThatSaysWindows())


@pytest.mark.parametrize(
    "raw",
    [
        "file:////attacker/share/persona.md",
        "file://\\\\attacker\\share\\persona.md",
        "file:////10.0.0.1/public/p.md",
    ],
)
def test_a_unc_prompt_is_refused_on_windows(monkeypatch, tmp_path, raw):
    mod = load_build()
    _as_windows(monkeypatch, mod)
    with pytest.raises(mod.ExportRefused, match="UNC"):
        mod._resolve_prompt_path(raw, tmp_path)


def test_the_refusal_happens_before_any_resolution(monkeypatch, tmp_path):
    """`resolve()` on a UNC path is the probe, so the gate must run before it."""
    mod = load_build()
    _as_windows(monkeypatch, mod)
    touched: list[str] = []
    real_resolve = pathlib.Path.resolve

    def _spy(self, *a, **kw):
        touched.append(str(self))
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "resolve", _spy)
    with pytest.raises(mod.ExportRefused):
        mod._resolve_prompt_path("file:////attacker/share/persona.md", tmp_path)
    assert not any(
        "attacker" in t for t in touched
    ), f"the UNC target was resolved before it was refused: {touched}"


def test_an_ordinary_prompt_is_unaffected_on_windows(monkeypatch, tmp_path):
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "persona.md").write_text("# P\nbody\n", encoding="utf-8")
    _as_windows(monkeypatch, mod)
    got = mod._resolve_prompt_path("file://persona.md", agents)
    assert got.name == "persona.md"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "On Windows a leading `//` IS a UNC path, so the gate refuses it and that is the "
        "correct answer. The property under test is POSIX-only: an earlier version of this "
        "test asserted `os.name != 'nt'` instead of skipping, which turned a platform fact "
        "into a failing Windows shard."
    ),
)
def test_a_doubled_slash_still_works_on_posix(tmp_path):
    """POSIX has no network meaning for a leading `//`, so refusing it protects nothing."""
    mod = load_build()
    agents = tmp_path / "agents"
    agents.mkdir()
    p = agents / "persona.md"
    p.write_text("# P\nbody\n", encoding="utf-8")
    got = mod._resolve_prompt_path("file://" + "/" + str(p), agents)
    assert got.read_text(encoding="utf-8").startswith("# P")


# --- promotion keeps one bundle at all times ---------------------------------


@_posix_only
def test_a_failed_promotion_keeps_the_previous_bundle(monkeypatch, tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    first = (out / "manifest.json").read_text(encoding="utf-8")

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError("the promotion failed here")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, crew, out)

    assert (out / "manifest.json").is_file(), "the previous bundle was destroyed"
    assert (out / "manifest.json").read_text(encoding="utf-8") == first
    assert not (out.parent / (out.name + ".previous")).exists(), "aside copy left behind"


@_posix_only
def test_a_failed_promotion_keeps_the_carried_plan(monkeypatch, tmp_path):
    """The signed plan is the part that cannot be rebuilt from the crew."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    (out / mod.PLAN_FILENAME).write_bytes(b'{"signed": "plan"}')

    real_rename = os.rename

    def _fail_the_promotion(src, dst, *args, **kwargs):
        if str(src).endswith(".staging"):
            raise OSError("the promotion failed here")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", _fail_the_promotion)
    with pytest.raises(OSError):
        _build(mod, crew, out)

    assert (out / mod.PLAN_FILENAME).read_bytes() == b'{"signed": "plan"}'


@_posix_only
def test_a_successful_build_leaves_no_aside_copy(tmp_path):
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    _build(mod, crew, out)
    assert (out / "manifest.json").is_file()
    assert not (out.parent / (out.name + ".previous")).exists()
    assert not (out.parent / (out.name + ".staging")).exists()


@_posix_only
def test_a_leftover_bundle_at_the_aside_path_does_not_block_a_build(tmp_path):
    """A crash between the two renames leaves a REAL bundle there; the next build proceeds.

    The leftover is produced by the build rather than hand-written, because the aside path
    is verified against its manifest's own digest now: a directory with a hand-made
    ``manifest.json`` is correctly refused, since that is what an operator's own directory
    using bundle names looks like.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    # A genuine bundle, built and then moved to where a crash would have left it.
    spare = tmp_path / "spare"
    _build(mod, crew, spare)
    stale = out.parent / (out.name + ".previous")
    spare.rename(stale)
    _build(mod, crew, out)
    assert (out / "manifest.json").is_file()
    assert not stale.exists()


@_posix_only
def test_a_hand_written_manifest_at_the_aside_path_is_refused(tmp_path):
    """Owned names and plain shapes are both satisfied by a directory someone else made."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    theirs = out.parent / (out.name + ".previous")
    theirs.mkdir()
    (theirs / "manifest.json").write_text('{"notes": "mine"}\n', encoding="utf-8")
    (theirs / "agent.json").write_text('{"mine": true}\n', encoding="utf-8")
    with pytest.raises(mod.ExportRefused, match="does not match the bundle"):
        _build(mod, crew, out)
    assert (theirs / "manifest.json").read_text(encoding="utf-8") == '{"notes": "mine"}\n'


@_posix_only
def test_the_aside_path_holding_someone_elses_files_is_refused(tmp_path):
    """The name is derived from --out, so that directory can be the operator's own."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    theirs = out.parent / (out.name + ".previous")
    theirs.mkdir()
    (theirs / "quarterly-report.xlsx").write_bytes(b"not mine to delete")
    with pytest.raises(mod.ExportRefused, match="does not own"):
        _build(mod, crew, out)
    assert (theirs / "quarterly-report.xlsx").read_bytes() == b"not mine to delete"
    assert (out / "manifest.json").is_file(), "the refusal must not disturb --out either"


@_posix_only
def test_a_shape_this_build_never_writes_at_the_aside_path_is_refused(tmp_path):
    """An owned NAME is not enough: the delete is recursive."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    theirs = out.parent / (out.name + ".previous")
    (theirs / "skills").mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere", theirs / "skills" / "link")
    with pytest.raises(mod.ExportRefused, match="shape this build never writes"):
        _build(mod, crew, out)
    assert (theirs / "skills" / "link").is_symlink()


@_posix_only
def test_a_file_at_the_aside_path_is_refused_not_crashed_on(tmp_path):
    """`exists()` is true for a file and `iterdir()` would raise NotADirectoryError."""
    mod = load_build()
    crew = _crew(mod, tmp_path)
    out = tmp_path / "bundle"
    _build(mod, crew, out)
    stray = out.parent / (out.name + ".previous")
    stray.write_text("someone's note\n", encoding="utf-8")
    with pytest.raises(mod.ExportRefused, match="not a directory"):
        _build(mod, crew, out)
    assert stray.read_text(encoding="utf-8") == "someone's note\n"


# --- the --out UNC gate (a screen that must touch its subject cannot be outermost) ---
@_posix_only
def test_a_unc_shaped_out_is_refused_before_any_filesystem_touch(monkeypatch, tmp_path):
    """--out is author-supplied, the same class as the spec/plan paths that already gate.

    On Windows an ``lstat`` on ``\\\\host\\share`` reaches the host over SMB with an NTLM
    exchange, so the purely-local shape screen must run before the first ``_is_redirecting_entry``.

    POSIX-only, and the reason is subtle enough to state: this drives ``build_bundle``, whose
    FIRST line is ``_refuse_without_nofollow_primitive()``. On a real platform with no atomic
    no-follow primitive (Windows) that entry guard refuses before the UNC gate is reached, so on
    the Windows shard the test would meet the wrong ``ExportRefused`` -- "the builder refuses
    everywhere without the primitive" is the same fact that makes this look broken when it is not.
    Here ``os.name`` is faked to ``nt`` while the primitive stays real, so the UNC branch runs and
    the gate is what answers; on Windows the entry guard already covers the whole builder.
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    spec = mod.read_agent_spec(crew)
    _as_windows(monkeypatch, mod)
    # If the shape screen did NOT run first, the first path touch would try to lstat this
    # UNC path. The refusal names --out and a UNC path, distinguishing it from any later check.
    with pytest.raises(mod.ExportRefused) as caught:
        mod.build_bundle(
            crew,
            spec,
            mod.enumerate_all(crew, spec),
            None,
            pathlib.Path(r"\\attacker\share\bundle"),
        )
    msg = str(caught.value)
    assert "--out" in msg and "UNC" in msg


@_posix_only
def test_a_local_out_is_not_refused_by_the_unc_gate(monkeypatch, tmp_path):
    """Non-vacuity: the gate refuses a UNC shape, not every path -- a local --out still builds.

    POSIX-only for a sharper reason than its sibling. This test exists to show the gate refuses
    a UNC SHAPE rather than every path. On Windows ``build_bundle`` DOES refuse a local --out --
    but from ``_refuse_without_nofollow_primitive()`` at its entry, a different guard entirely --
    so a Windows run would observe a blanket refusal that has nothing to do with the UNC gate and
    prove nothing about it. Marking it POSIX-only keeps the proof where the primitive is real and
    the only refusal that can fire is the UNC gate's. (To keep an equivalent on Windows one would
    neutralise the primitive gate first -- the ``_dir_fd_supported`` / ``_no_dir_fd`` shape -- then
    assert the UNC gate did not fire; that is a different test, not this one.)
    """
    mod = load_build()
    crew = _crew(mod, tmp_path)
    spec = mod.read_agent_spec(crew)
    _as_windows(monkeypatch, mod)
    # A local path is not UNC-shaped, so the gate is a no-op; the build reaches its real work.
    # (It may later refuse for a Windows no-follow-primitive reason, but NOT for a UNC --out.)
    try:
        mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, tmp_path / "bundle")
    except mod.ExportRefused as e:
        assert "UNC" not in str(e), f"local --out wrongly refused by the UNC gate: {e}"


def test_the_out_unc_screen_precedes_the_first_out_touch_in_source():
    """Source rule: the UNC screen cannot be reproduced on POSIX, so pin its position.

    ``_refuse_unc_out(out_dir)`` must appear before the first thing that touches a path derived
    from --out in ``build_bundle`` -- the ``_refuse_unusable_parent(out_dir, ...)`` call.
    """
    src = (pathlib.Path(__file__).parent.parent / "build.py").read_text(encoding="utf-8")
    build_at = src.index("def build_bundle(")
    body = src[build_at:]
    screen_at = body.index("_refuse_unc_out(out_dir)")
    touch_at = body.index("_refuse_unusable_parent(out_dir")
    assert 0 <= screen_at < touch_at, (
        "the UNC screen on --out does not run before the first filesystem touch of a "
        "path derived from --out, so a UNC-shaped --out reaches its host before any check"
    )

"""A transient OSError in build_bundle's post-marker setup refuses and cleans up,
and a symlink loop reaching ``_open_dir_nofollow_pinned`` refuses instead of crashing.

Two class-level defects, each proven to fail at CALL phase on the untouched base.

D1 build_bundle writes the staging tree and its ownership marker, then runs setup probes --
   the report baseline read, ``out_dir.exists()``, and the ``plan_file.is_file()`` read --
   BEFORE the main transaction opens. A raw OSError (a permission change, EIO, ESTALE) from
   any of them escaped every handler and stranded the staging tree AND the marker; the next
   run reads that marker as another build's claim and refuses permanently. The setup is now
   inside an OSError-to-ExportRefused boundary that purges while the retained staging
   descriptor is still open, requires the captured tree's device/inode to match that
   descriptor, closes it exactly once, and removes this run's marker. A swapped-in tree is
   restored untouched even when its shape looks build-owned; a transient fault leaves no
   owned staging claim, and a second build succeeds.

D2 ``_open_dir_nofollow_pinned`` calls ``Path.resolve()`` when ``already_resolved=False``;
   CPython 3.12 reports a symlink loop there as ``RuntimeError``, while 3.13 can defer it
   to the component walk as ``OSError(ELOOP)`` or a confirmed-link ``ENOTDIR``. All loop
   shapes are normalised to ``OSError(ELOOP)`` at that one boundary, while an ordinary
   non-directory remains ``ENOTDIR``, so callers fail closed with a consistent refusal.
"""

from __future__ import annotations

import errno
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
    """The ordinary resolve -> read -> plan -> verify -> build flow, mirroring the sibling tests."""
    crew = mod.resolve_crew("frontdesk", home)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    out.parent.mkdir(parents=True, exist_ok=True)
    plan_path = sign_plan(mod, crew, spec, out.parent, select=select)
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    return mod.build_bundle(crew, spec, cands, plan, out)


def _staging_paths(out: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return (
        out.parent / f"{out.name}.staging",
        out.parent / f"{out.name}.staging.owned",
    )


# ---------------------------------------------------------------------------
# D1: a transient OSError in each post-marker/pre-transaction probe
# ---------------------------------------------------------------------------
#: One row per probe class the setup runs after the marker exists. ``predicate`` names the
#: ``pathlib.Path`` method to make raise, and ``match`` selects the exact path so nothing else
#: in the build is disturbed. Together these cover all four probes named in the defect: the
#: report ``is_file``/redirect check and its baseline read (the report row), ``out_dir.exists``
#: (the out row), and ``plan_file.is_file`` (the plan row).
_PROBES = [
    ("report", "is_file", lambda out, p: p.name == f"{out.name}.smc-bundle.json"),
    ("out_dir", "exists", lambda out, p: p == out),
    ("plan", "is_file", lambda out, p: p.name == "curation-plan.json" and p.parent == out),
]


@_posix_only
@pytest.mark.parametrize("label,predicate,selects", _PROBES)
def test_a_transient_fault_in_a_post_marker_probe_refuses_and_cleans_up(
    tmp_path: pathlib.Path, monkeypatch, label, predicate, selects
) -> None:
    """The probe raises OSError once; the build refuses AND releases staging + marker.

    On the untouched base the raw OSError escapes ``build_bundle`` (not an ``ExportRefused``),
    so the ``pytest.raises(ExportRefused)`` fails at CALL phase, and the staging tree and
    marker are left on disk.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    # A first successful build so the report and (carried) plan exist for the probes to hit.
    first = _build(mod, home, out, {"skills": {"faq"}})
    report_path = out.parent / f"{out.name}.smc-bundle.json"
    report_before = report_path.read_bytes()
    manifest_before = (out / "manifest.json").read_bytes()

    real = getattr(mod.Path, predicate)
    real_open = mod._open_dir_nofollow_pinned
    fired = {"n": 0}
    opened_fds = []
    _, marker = _staging_paths(out)

    def _track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened_fds.append(fd)
        return fd

    def _boom(self, *a, **k):
        # Fire only once this run's ownership marker is on disk, so the fault lands on the
        # POST-marker probe named in the defect, not an identically-spelled pre-marker check
        # (e.g. the shape guard's ``out_dir.exists()`` that runs before staging is claimed).
        # ``os.path.lexists`` reads the real filesystem, unaffected by this patch.
        if selects(out, self) and os.path.lexists(marker):
            fired["n"] += 1
            raise OSError(errno.EIO, f"injected transient fault on {predicate}")
        return real(self, *a, **k)

    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _track_open)
    monkeypatch.setattr(mod.Path, predicate, _boom)

    with pytest.raises(mod.ExportRefused):
        _build(mod, home, out, {"skills": {"faq"}})
    assert fired["n"] >= 1, f"the {label} probe was never reached; fixture is inert"
    assert opened_fds
    for fd in opened_fds:
        with pytest.raises(OSError) as closed:
            os.fstat(fd)
        assert closed.value.errno == errno.EBADF

    staging, marker = _staging_paths(out)
    assert not staging.exists(), f"{label}: staging tree stranded after a transient fault"
    assert (
        not marker.exists()
    ), f"{label}: ownership marker stranded, which blocks every later build permanently"
    # The prior bundle and its report are byte-for-byte untouched: nothing was promoted.
    assert (out / "manifest.json").read_bytes() == manifest_before
    assert report_path.read_bytes() == report_before

    # And with the fault gone, a second build succeeds -- the marker did not poison the path.
    monkeypatch.setattr(mod.Path, predicate, real)
    again = _build(mod, home, out, {"skills": {"faq"}})
    assert again.digest == first.digest
    assert (out / "manifest.json").is_file()
    assert not staging.exists() and not marker.exists()


@_posix_only
def test_a_failed_purge_sweep_is_not_reported_as_released(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A private-aside sweep that fails leaves staging undeleted, so it must not read as released.

    The purge ``settle`` is a no-op: the verified staging tree sits in the ``.smc-purge-*``
    aside until the pinned sweep deletes it. Before the fix a failed sweep was swallowed,
    ``_purge_staging_best_effort`` returned ``True`` and the refusal told the operator the
    staging tree "was released" while it was still on disk.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    staging, marker = _staging_paths(out)
    real_exists = mod.Path.exists
    real_rmtree = mod._rmtree_pinned
    swept = {"n": 0}

    def _boom(self, *args, **kwargs):
        if self == out and os.path.lexists(marker):
            raise OSError(errno.EIO, "injected post-marker fault before transaction")
        return real_exists(self, *args, **kwargs)

    def _fail_purge_sweep(parent_fd, name):
        if name.startswith(".smc-purge-"):
            swept["n"] += 1
            raise OSError(errno.EIO, "injected private-aside sweep failure")
        return real_rmtree(parent_fd, name)

    monkeypatch.setattr(mod.Path, "exists", _boom)
    monkeypatch.setattr(mod, "_rmtree_pinned", _fail_purge_sweep)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})

    assert swept["n"] == 1, "the private-aside sweep was never reached; fixture is inert"
    message = str(caught.value)
    assert "was released" not in message
    assert "could not be released safely" in message
    assert ".smc-purge-*" in message, "the refusal must name where the undeleted tree now sits"
    leftovers = [p for p in out.parent.iterdir() if p.name.startswith(".smc-purge-")]
    assert len(leftovers) == 1
    assert (leftovers[0] / staging.name).is_dir(), "the undeleted staging tree should remain"
    assert not marker.exists()


@_posix_only
def test_post_marker_fault_preserves_a_swapped_bundle_shaped_staging_tree(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Cleanup refuses a replacement even when its contents look build-owned."""
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    staging, marker = _staging_paths(out)
    displaced = staging.with_name(staging.name + ".displaced")
    real_exists = mod.Path.exists
    real_open = mod._open_dir_nofollow_pinned
    real_purge = mod._purge_staging_best_effort
    opened_fds = []
    swapped = {"done": False}

    def _track_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if path == staging:
            opened_fds.append(fd)
        return fd

    def _boom(self, *args, **kwargs):
        if self == out and os.path.lexists(marker):
            raise OSError(errno.EIO, "injected post-marker fault before transaction")
        return real_exists(self, *args, **kwargs)

    def _swap_then_purge(path, resolved_parent, *args, **kwargs):
        if path == staging and not swapped["done"]:
            path.rename(displaced)
            path.mkdir()
            (path / "agent.json").write_text("victim bytes\n", encoding="utf-8")
            swapped["done"] = True
        return real_purge(path, resolved_parent, *args, **kwargs)

    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _track_open)
    monkeypatch.setattr(mod.Path, "exists", _boom)
    monkeypatch.setattr(mod, "_purge_staging_best_effort", _swap_then_purge)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})

    assert swapped["done"], "the cleanup swap fixture was never reached"
    assert "could not be released safely" in str(caught.value)
    assert str(staging) in str(caught.value)
    assert "remove it before retrying" in str(caught.value)
    assert (staging / "agent.json").read_text(encoding="utf-8") == "victim bytes\n"
    assert displaced.is_dir(), "the original pinned staging inode should remain as safe residue"
    assert not marker.exists()
    assert len(opened_fds) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened_fds[0])
    assert closed.value.errno == errno.EBADF


@_posix_only
def test_post_marker_fault_with_staging_moved_away_does_not_claim_where_it_is(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A staging tree moved by another process before capture is reported as unconfirmed.

    The purge returns False when the tree vanished before its capture rename, not only when
    the sweep failed. A refusal asserting the tree is "still there or under a .smc-purge-*
    directory" is false here: it is wherever the other process put it.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    staging, marker = _staging_paths(out)
    elsewhere = tmp_path / "moved-by-someone-else"
    real_exists = mod.Path.exists
    real_purge = mod._purge_staging_best_effort
    moved = {"done": False}

    def _boom(self, *args, **kwargs):
        if self == out and os.path.lexists(marker):
            raise OSError(errno.EIO, "injected post-marker fault before transaction")
        return real_exists(self, *args, **kwargs)

    def _move_then_purge(path, resolved_parent, *args, **kwargs):
        if path == staging and not moved["done"]:
            path.rename(elsewhere)
            moved["done"] = True
        return real_purge(path, resolved_parent, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "exists", _boom)
    monkeypatch.setattr(mod, "_purge_staging_best_effort", _move_then_purge)

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})

    assert moved["done"], "the move-away fixture was never reached"
    message = str(caught.value)
    assert "deletion was not confirmed" in message
    assert "wherever another process moved it" in message
    assert "it is still there" not in message
    assert elsewhere.is_dir(), "the moved tree is not this build's to delete"
    assert not staging.exists()
    assert not marker.exists()


@_posix_only
def test_the_boundary_does_not_swallow_a_deliberate_refusal(tmp_path: pathlib.Path) -> None:
    """Non-vacuity: an ExportRefused raised inside the boundary keeps its own message.

    The boundary catches OSError only; ExportRefused is a RuntimeError, so a deliberate
    refusal (here: --out holding a stranger file this build does not own) passes through with
    its own wording rather than the generic filesystem-fault message.
    """
    mod = load_build()
    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"
    _build(mod, home, out, {"skills": {"faq"}})
    (out / "stranger.txt").write_text("not ours\n", encoding="utf-8")

    with pytest.raises(mod.ExportRefused) as caught:
        _build(mod, home, out, {"skills": {"faq"}})
    assert "does not own" in str(caught.value)
    assert "filesystem fault" not in str(caught.value)


# ---------------------------------------------------------------------------
# D2: a symlink loop reaching _open_dir_nofollow_pinned
# ---------------------------------------------------------------------------
@_posix_only
def test_open_dir_nofollow_pinned_converts_a_symlink_loop_to_oserror(
    tmp_path: pathlib.Path,
) -> None:
    """resolve() on a loop raises RuntimeError; the helper must re-raise OSError(ELOOP).

    On the untouched base the RuntimeError escapes uncaught, so ``pytest.raises(OSError)``
    fails at CALL phase (a RuntimeError is not an OSError). Only the errno is asserted: on
    CPython 3.12 the loop surfaces from ``resolve()`` and is chained as ``__cause__``, while
    3.13 can defer it to the component walk, so the cause's shape is version-dependent.
    """
    mod = load_build()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    loop = a / "leaf"  # resolving through a <-> b is a symlink loop

    with pytest.raises(OSError) as caught:
        mod._open_dir_nofollow_pinned(loop)
    assert not isinstance(caught.value, mod.ExportRefused)
    assert caught.value.errno == errno.ELOOP


@_posix_only
def test_open_dir_nofollow_pinned_normalizes_component_walk_eloop(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A non-strict resolve that defers the loop still returns ``OSError(ELOOP)``."""
    mod = load_build()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    loop = a / "leaf"
    real_resolve = mod.Path.resolve

    def _defer_loop(self, *args, **kwargs):
        if self == loop:
            return self
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "resolve", _defer_loop)

    with pytest.raises(OSError) as caught:
        mod._open_dir_nofollow_pinned(loop)
    assert caught.value.errno == errno.ELOOP

    ordinary_file = tmp_path / "ordinary-file"
    ordinary_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError) as ordinary:
        mod._open_dir_nofollow_pinned(ordinary_file / "leaf")
    assert ordinary.value.errno == errno.ENOTDIR


@_posix_only
def test_a_component_swapped_for_a_link_is_named_not_called_a_loop(
    tmp_path: pathlib.Path,
) -> None:
    """A plain (non-cyclic) link in the walk is reported by name, not as a symlink loop.

    ``O_DIRECTORY | O_NOFOLLOW`` on an ordinary link fails with ``ENOTDIR`` on Linux, the
    same shape a post-resolution swap produces. The refusal keeps ``ELOOP`` so callers fail
    closed, and its message names the component that is a link.
    """
    mod = load_build()
    real = tmp_path / "real"
    (real / "leaf").mkdir(parents=True)
    swapped = tmp_path / "swapped"
    swapped.symlink_to(real)

    with pytest.raises(OSError) as caught:
        mod._open_dir_nofollow_pinned(swapped / "leaf", already_resolved=True)
    assert caught.value.errno == errno.ELOOP
    message = str(caught.value)
    assert "'swapped'" in message
    assert "changed to a link since resolution" in message
    assert "symlink loop resolving" not in message


@_posix_only
def test_open_dir_nofollow_pinned_already_resolved_never_resolves(
    tmp_path: pathlib.Path,
) -> None:
    """Non-vacuity: the normalization only wraps the resolve the helper itself performs.

    A caller that passes ``already_resolved=True`` hands in a value the helper must not
    re-resolve, so a loop planted at that path is opened no-follow (and refused as a
    redirect), not collapsed by resolve().
    """
    mod = load_build()
    real = tmp_path / "real"
    real.mkdir()
    fd = mod._open_dir_nofollow_pinned(real, already_resolved=True)
    try:
        assert isinstance(fd, int)
    finally:
        os.close(fd)


@_posix_only
def test_a_symlink_loop_at_a_derived_path_refuses_rather_than_crashes(
    tmp_path: pathlib.Path,
) -> None:
    """Caller-level proof: a loop the pinned open meets surfaces as ExportRefused, not RuntimeError.

    ``build_bundle`` opens the staging tree it just created through the pinned helper. Making
    that open's resolve meet a loop -- by pointing the helper at a two-link cycle -- must fail
    closed as an OSError the caller converts, never as a bare RuntimeError. On the untouched
    base the RuntimeError escapes the caller's ``except OSError`` and reaches the operator as a
    traceback.
    """
    mod = load_build()
    real = mod._open_dir_nofollow_pinned  # capture before patching

    a = tmp_path / "cyc_a"
    b = tmp_path / "cyc_b"
    a.symlink_to(b)
    b.symlink_to(a)
    looped = a / "staging"

    home = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\n"}})
    out = tmp_path / "work" / "bundle"

    def _loop_on_staging(dir_path, *, already_resolved=False):
        if dir_path.name.endswith(".staging"):
            # Force this call down the resolve() path against a genuine symlink loop.
            return real(looped, already_resolved=False)
        return real(dir_path, already_resolved=already_resolved)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_open_dir_nofollow_pinned", _loop_on_staging)
    try:
        with pytest.raises(mod.ExportRefused):
            _build(mod, home, out, {"skills": {"faq"}})
    finally:
        monkeypatch.undo()

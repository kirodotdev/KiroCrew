"""A crew's unit-order file is recorded and read without resolving through a link.

This file pins the order a read applies a crew's units in, the precedence only a landed
append earns, and the boundaries around the order file: nothing is written through a
link at its name or its directory, the by-name fallback holds every component of the
parent open across the read and the write, the walk is depth-bounded and creates
nothing, and the leaf is settled by a no-follow open that leaks no descriptor. A refusal
anywhere on that path answers header order instead of raising into a read or the
recorder, while a deep but legitimate data home still records; and where a
descriptor-relative rename exists, neither the read nor the write takes the hold. It
also pins the derived store paths: the crews directory under ``store.repo_data_dir``
and the app root a provider subtree strips back to. The writes refused against a record
that cannot be proven live in ``test_issue_radar_crew_store_write_refusal.py``.
"""

import os
import shutil
import signal
from pathlib import Path

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _crew,
    _isolated_crew_log,
    _item,
    _live_crew,
    _record,
    _unit,
)

from conftest import make_dir_link
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection


def test_reads_apply_units_in_the_order_they_recorded_not_header_order(tmp_path, monkeypatch):
    """A read has no caller inside a unit to pin the live one last, so it orders the
    units by the order the crew RECORDED into them. Header clock order is what a
    backward clock step inverts; here the store is made to list the units in that
    inverted order, and the read still applies the later write last."""
    from kiro_crew.crew_log import store as crew_log_store

    crew = _crew(tmp_path)
    cid = crew["id"]
    first = _unit(cid)
    _item(tmp_path, cid, first, 7, {"phase": "claimed"}, "claim")
    second = _unit(cid)
    _item(tmp_path, cid, second, 7, {"phase": "implementing"}, "implement")
    real = crew_log_store.session_units_for_slot
    assert real(cs.slot_key_for(cid)) == (first, second), "the control: header order agrees"

    monkeypatch.setattr(
        crew_log_store,
        "session_units_for_slot",
        lambda slot, **kw: tuple(reversed(real(slot, **kw))),
    )
    crew_log_projection.forget_slot_folds()
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path) == (first, second)
    item = cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)
    assert item is not None and item["phase"] == "implementing"
    # The order file lives beside the crew record, not in the legacy items dir.
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    assert order.read_text().split() == [first, second]
    assert order.parent == cs.crews_dir(OWNER, REPO, tmp_path)


def test_the_app_root_strips_only_the_provider_subtree_it_appended(tmp_path):
    """``_app_root`` strips the EXACT trailing subtree, not the first reserved segment.

    The id-uniqueness check globs every repository of the data home from this root, and
    the mint lock lives under it. Matching the first ``@providers`` component anywhere in
    the path truncated an operator base path that carried that segment, moving both above
    the data home -- where the glob matches no crew record at all, so ``_crew_id_in_use``
    answers False for an id that IS held and two crews fold one ledger.
    """
    # NEGATIVE -- the reserved segment sits in the operator's own base path. Nothing is
    # stripped: this is the data home, not a subtree of one.
    operator_base = tmp_path / "@providers" / "radar-home"
    operator_base.mkdir(parents=True, exist_ok=True)
    assert cs._app_root(operator_base) == operator_base

    # POSITIVE -- the exact three components ``provider_root`` appends ARE stripped, even
    # though the base above them also carries the segment.
    subtree = cs.store.provider_root(
        root=operator_base, provider="gitlab", host="gitlab.internal:8443"
    )
    assert subtree != operator_base, "the fixture did not build a provider subtree"
    assert cs._app_root(subtree) == operator_base


@pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "plants a REAL file symlink, which needs a privilege an unelevated Windows shell "
        "does not have. Windows is not left without the boundary: the NAME screen this "
        "exercises is pinned there by "
        "test_the_name_screen_refuses_a_linked_order_file_on_every_platform, and the "
        "DIRECTORY screen by test_the_unit_order_write_refuses_a_linked_directory, which "
        "plants a junction."
    ),
)
def test_the_unit_order_write_never_follows_a_planted_link(tmp_path):
    """The unit-order file is written under the gateway's data home, where a sandboxed
    agent may be able to plant a link. A link at the file's NAME is replaced, not
    written through: the file is staged under a unique name and renamed over the
    link. A link at the crews DIRECTORY is refused (the parent is pinned before the
    write), and the crew falls back to header order -- the write itself still goes
    through. In neither case does a byte land in the file the link points at."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    victim = tmp_path / "operator-file.txt"
    victim.write_text("operator data\n", encoding="utf-8")
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    # Not guarded: on POSIX this must succeed, and a skip here would score as a pass
    # for the very branch the test exists to check.
    os.symlink(victim, order)

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert (
        victim.read_text(encoding="utf-8") == "operator data\n"
    ), "nothing written through the link"
    from kiro_crew import atomic_write as atomic_write_module

    if atomic_write_module.pinned_parent_replace_supported():
        # The staged file is renamed over the link: the link is replaced, never followed.
        assert not order.is_symlink() and order.read_text().split() == [sid], "link replaced"
    else:
        # No descriptor-relative rename here: a link at the name is refused and left
        # alone, and the crew falls back to header order.
        assert order.is_symlink(), "the link was refused, not written through"

    # A link at the DIRECTORY has its own test below, which reaches Windows too, and
    # the NAME screen's own refusal is pinned on every platform by the test that follows.


def test_the_name_screen_refuses_a_linked_order_file_on_every_platform(tmp_path, monkeypatch):
    """The NAME half of the fallback screen refuses the write, on every platform.

    The test above plants a REAL symlink, so it is POSIX-only -- and Windows is exactly
    the platform that TAKES this fallback, having no descriptor-relative rename. A skip
    there would score as a pass for the one boundary that platform has, so the screen's
    own decision is driven here instead: the fallback is forced, and
    ``is_link_or_junction`` answers True for the order path ALONE (the real function
    still answers for every other path, so a linked ancestor cannot be what refuses).

    What the POSIX test proves is that a real link makes that function answer True; what
    this pins is that the writer ACTS on the answer rather than writing through it.
    """
    from kiro_crew import atomic_write as atomic_write_module
    from kiro_crew import platform_compat

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text("operator data\n", encoding="utf-8")

    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)
    real_screen = platform_compat.is_link_or_junction
    monkeypatch.setattr(
        platform_compat,
        "is_link_or_junction",
        lambda p, _real=real_screen, _target=order: True if Path(p) == _target else _real(p),
    )

    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert (
        order.read_text(encoding="utf-8") == "operator data\n"
    ), "the writer wrote through a name the screen refused"


def test_the_unit_order_write_refuses_a_linked_directory(tmp_path):
    """A link at the crews DIRECTORY is refused and the crew falls back to header
    order; the write itself still goes through and nothing lands in the target.

    The link is made with the conftest helper, which uses a JUNCTION on Windows: a
    directory symlink needs a privilege an unelevated shell does not have, so a test
    that planted one with ``os.symlink`` skipped on the one platform where this
    matters -- it is the platform without descriptor-relative writes, so the explicit
    screen is the only boundary there."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    victim_dir = tmp_path / "operator-dir"
    victim_dir.mkdir()
    crews = cs.crews_dir(OWNER, REPO, tmp_path)
    keep = crews.with_name(crews.name + ".keep")
    crews.rename(keep)
    make_dir_link(crews, victim_dir)
    try:
        crew_log_projection.forget_slot_folds()
        _record(tmp_path, cid, sid, 7, {"next": "read it"}, "claim", "progress")
        assert list(victim_dir.iterdir()) == [], "nothing written into the linked directory"
    finally:
        cs.platform_compat.unlink_link_or_junction(crews)
        keep.rename(crews)
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["next"] == "read it"


def test_the_unit_order_fallback_screen_answers_for_a_junction_not_only_a_symlink(
    tmp_path, monkeypatch
):
    """The fallback screen consults the junction-aware predicate, not ``is_symlink``.

    ``os.path.islink`` is False for a Windows directory junction, so an
    ``islink``-only screen leaves the platform that HAS junctions -- the one taking
    this fallback, since it has no descriptor-relative rename -- with no boundary.
    Pinned by reporting a link through the predicate while the filesystem holds a
    plain directory: a screen that consulted ``is_symlink`` would write anyway."""
    from kiro_crew import atomic_write as atomic_write_module

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)

    monkeypatch.setattr(
        cs.platform_compat, "is_link_or_junction", lambda p: os.fspath(p) == os.fspath(order)
    )
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))

    monkeypatch.setattr(cs.platform_compat, "is_link_or_junction", lambda p: False)
    monkeypatch.setattr(cs.platform_compat, "first_linked_ancestor", lambda p: str(order.parent))
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert not order.exists(), "neither refusal wrote the file"

    monkeypatch.setattr(cs.platform_compat, "first_linked_ancestor", lambda p: None)
    cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert order.read_text(encoding="utf-8").split() == [sid], "the control: it writes otherwise"


def test_an_append_that_landed_after_the_flush_budget_publishes_its_unit_precedence(
    tmp_path, monkeypatch
):
    """Precedence is earned by an append that LANDED, so the append path notes it only
    for an entry it saw inside the flush budget. An append the writer still held when
    that budget expired lands later, and by then its unit can be retired -- it has no
    next write of its own to make the claim. The crew's next write does: the receipt
    that proves the late append landed is where the precedence is published, before
    that write's own append moves the live unit after it."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    from kiro_crew.crew_log import emit as crew_log_emit

    real_flush = crew_log_emit.flush
    monkeypatch.setattr(crew_log_emit, "flush", lambda *a, **k: False)
    # The append is made and the flush budget expires: the write answers "not durable"
    # and leaves the crew marked undrained with a receipt for that exact entry.
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert (str(cs.data_home()), cid) in cs._undrained, "the control: a receipt is held"
    assert sid not in cs._recorded_unit_order(
        OWNER, REPO, cid, tmp_path
    ), "the control: precedence was NOT published, because nothing had landed"

    # The append lands, late -- and the crew's slot has meanwhile moved to a SUCCESSOR
    # unit, which is the case with no self-correction: the old unit is retired and has
    # no next write of its own to claim precedence.
    monkeypatch.setattr(crew_log_emit, "flush", real_flush)
    assert real_flush(timeout=10.0)
    crew_log_projection.forget_slot_folds()
    successor = _unit(cid)
    assert successor != sid
    _record(tmp_path, cid, successor, 8, {"phase": "claimed"}, "claim", "took #8")

    order = cs._recorded_unit_order(OWNER, REPO, cid, tmp_path)
    assert sid in order, "the retired unit's late-landed append published its precedence"
    # And in the right place: the retired unit's entry landed BEFORE the successor's, so
    # it must sort first. Publishing it AFTER the successor would be the very inversion
    # that has the retired unit's phase and next step read as the current ones.
    assert order.index(sid) < order.index(successor)
    assert cs._unit_order_path(OWNER, REPO, cid, tmp_path).is_file()
    assert (str(cs.data_home()), cid) not in cs._undrained, "the receipt is released"


def test_a_write_that_never_landed_does_not_claim_the_newest_entry(tmp_path, monkeypatch):
    """The order file's claim is "among the units that recorded, this one holds the
    newest entry", so a unit whose append never reached the file must not be moved
    ahead of one whose did.

    A unit pinned ahead of its successor is the inversion the order exists to
    prevent: a later unit resuming with no entries of its own folds the pinned
    unit's phase and next step as current and acts on them. The note therefore
    belongs AFTER the append, gated on this unit's own log having grown -- not in
    ``_prepare_write``, which runs before anything is appended at all.
    """
    crew, first = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, first, 7, {"phase": "claimed"}, "claim", "took #7")
    second = _unit(cid)
    _record(tmp_path, cid, second, 7, {"next": "read it"}, "claim", "progress")
    before = cs._recorded_unit_order(OWNER, REPO, cid, tmp_path)
    assert before == (first, second), "a landed write orders its unit last"

    # The writer never drains, so nothing this write appends is in the file.
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)
    answer = _record(tmp_path, cid, first, 7, {"next": "queued"}, "claim", "not durable")
    assert answer["durable"] is False
    assert (
        cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == before
    ), "a write that did not land earned no precedence"


# ── the unit-order fallback holds its chain open across the write ───────────────
#
# The fallback branch is taken where there is no descriptor-relative rename, which
# is Windows -- and Windows is also where a junction can be planted. It writes by
# NAME, so a screen that answers about a name and a write that resolves that name
# again are two separate resolutions. These pin that the components the screen
# inspected are held open for as long as the write takes.
#
# What a POSIX host can prove is here: the walk opens each component no-follow,
# refuses a real link on disk rather than consulting a stubbed verdict, holds every
# descriptor across the write, each held descriptor is the component the walk
# screened, and every pin the walk takes asks for the share mode that withholds write
# access. What that share mode then makes the kernel do -- a handle without
# FILE_SHARE_DELETE blocks a rename of that directory and of everything above it, and
# one without FILE_SHARE_WRITE keeps every other handle on it from carrying write
# access, so its reparse data cannot be set -- belongs to the Windows kernel and is
# not observable on a POSIX host, so none of these claims it.


def _fallback_only(monkeypatch):
    """Take the by-name branch on a host that would otherwise write through a fd."""
    from kiro_crew import atomic_write as atomic_write_module

    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)


def test_the_unit_order_fallback_holds_every_component_open_across_the_write(tmp_path, monkeypatch):
    """Every component of the parent is still open, and is still the object the walk
    screened, at the moment the write runs.

    Two separate claims, asserted from inside the write rather than after it, because
    each fails for its own reason. LIFETIME: ``os.fstat`` on each descriptor the walk
    returned must succeed -- descriptors closed at the end of the walk would satisfy an
    after-the-fact check and protect nothing. IDENTITY: the ``st_dev``/``st_ino`` off
    the descriptor must equal the pair read from the NAME, which is the comparison a
    component swapped after the walk would break, and a descriptor that is open but
    refers to some other object is not a hold of the path that was screened."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    parent = order.parent
    components = [*reversed(parent.parents), parent]
    handed: list[int] = []
    real_walk = cs._hold_chain_no_follow

    def remember(directory, *, max_depth):
        fds = real_walk(directory, max_depth=max_depth)
        handed.extend(fds)
        return fds

    checked: list[str] = []
    real_write = cs.atomic_write

    def write_with_the_chain_still_held(path, content, **kw):
        assert len(handed) == len(components), "the walk held one descriptor per component"
        for component, fd in zip(components, handed, strict=True):
            try:
                info = os.fstat(fd)
            except OSError as exc:  # pragma: no cover - the mutation's path
                raise AssertionError(f"{component} was not held open during the write") from exc
            on_disk = os.stat(component)
            assert (info.st_dev, info.st_ino) == (
                on_disk.st_dev,
                on_disk.st_ino,
            ), f"the descriptor held for {component} is not that component"
            checked.append(os.fspath(component))
        return real_write(path, content, **kw)

    monkeypatch.setattr(cs, "_hold_chain_no_follow", remember)
    monkeypatch.setattr(cs, "atomic_write", write_with_the_chain_still_held)
    cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))

    assert order.read_text(encoding="utf-8").split() == [sid], "the write still landed"
    assert checked == [os.fspath(c) for c in components], "every component was checked while held"
    for fd in handed:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert parent.is_dir(), "the components outlive their descriptors"


def test_the_unit_order_fallback_refuses_a_real_link_at_a_component(tmp_path, monkeypatch):
    """A link planted at a component of the parent refuses the write, and nothing lands
    in what the link points at.

    The link is real and on disk, and the refusal comes from the OPEN rather than from
    a separate verdict: the walk never takes an ``lstat`` answer and then trusts it,
    which is what closes the window between the two. ``make_dir_link`` plants a
    junction on Windows, so the case stays exercised on the platform that takes this
    branch at all."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    victim = tmp_path / "operator-dir"
    victim.mkdir()
    crews = order.parent
    keep = crews.with_name(crews.name + ".keep")
    crews.rename(keep)
    make_dir_link(crews, victim)
    try:
        with pytest.raises(OSError, match="could not be held open"):
            cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
        assert list(victim.iterdir()) == [], "nothing was written into the link's target"
    finally:
        cs.platform_compat.unlink_link_or_junction(crews)
        keep.rename(crews)


def test_the_unit_order_fallback_refuses_a_missing_tail_and_creates_nothing(tmp_path, monkeypatch):
    """A parent that is not there refuses the write, and no component is created.

    Creating one would open a window the hold cannot close: a directory just created is
    empty, Windows accepts reparse data only on a directory with no children, and the
    child is opened BY NAME on the next step, so an outside writer can re-point it in
    between and that open resolves through whatever it points at. Refusing is the
    fail-closed answer. Asserted with TWO levels missing and with ``os.mkdir`` recorded,
    because a walk that created only the innermost component would still look like a
    refusal from the outside."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    crews = order.parent
    repo_dir = crews.parent
    shutil.rmtree(repo_dir)
    assert not repo_dir.exists(), "two levels really are missing"

    created: list[str] = []
    real_mkdir = os.mkdir

    def record(path, *a, **kw):
        created.append(os.fspath(path))
        return real_mkdir(path, *a, **kw)

    monkeypatch.setattr(cs.os, "mkdir", record)
    with pytest.raises(FileNotFoundError):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))

    assert created == [], f"the walk created something: {created}"
    assert not repo_dir.exists(), "and nothing is on disk"
    assert not order.exists(), "so no order file landed either"


def test_a_crew_whose_chain_is_absent_folds_in_header_order_without_raising(tmp_path, monkeypatch):
    """The refusal reaches the caller as header order, not as an exception.

    `_record_unit_order` is called under the crew's write lock on a real recording path,
    so a refusal escaping it would fail the recording itself rather than degrade the
    fold. Its contract is best-effort: a crew whose chain is gone folds its units by
    header clock until an ordinary crew-record write puts the directory back."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    crews = order.parent
    shutil.rmtree(crews)
    assert not crews.exists(), "the chain really is absent"

    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)

    assert not crews.exists(), "recording created nothing"
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "the fold reads header order"


def test_the_unit_order_fallback_refuses_a_component_it_cannot_open(tmp_path, monkeypatch):
    """A component that EXISTS and cannot be opened refuses; it does not become the
    boundary the walk stops at.

    Stopping there would leave the write resolving the rest of the path through an
    object nothing proved, which is the shape being removed -- so the refusal is the
    correct answer even though it is stricter than the by-name write was. The failure
    is injected at the pin for the ONE component under test, so the rest of the walk
    is the real one."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    crews = order.parent
    order.parent.mkdir(parents=True, exist_ok=True)
    real_pin = cs.platform_compat.pin_directory

    def refuse_that_one(component, **kwargs):
        if Path(component) == crews:
            raise PermissionError(13, "permission denied", os.fspath(component))
        return real_pin(component, **kwargs)

    monkeypatch.setattr(cs.platform_compat, "pin_directory", refuse_that_one)
    with pytest.raises(OSError, match="could not be held open"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert not order.exists(), "an unopenable component wrote nothing"


def test_the_unit_order_fallback_writes_an_ordinary_file_and_replaces_it(tmp_path, monkeypatch):
    """The clean case: an ordinary parent and an ordinary file still write, and a
    second write REPLACES the first rather than appending to it.

    A hardening change that widened the refusal surface would show up here, and the
    replacement half is what the fold depends on -- a unit recorded twice must appear
    once."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    other = _unit(cid)
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert order.read_text(encoding="utf-8") == f"{sid}\n"
    first = order.stat()

    cs._write_unit_order(order, (other, sid), max_depth=_generous(order.parent))
    assert order.read_text(encoding="utf-8") == f"{other}\n{sid}\n", "replaced, not appended"
    assert order.stat().st_ino != first.st_ino, "replaced by rename, not written in place"


def test_the_unit_order_fallback_keeps_the_name_screens_on_top_of_the_hold(tmp_path, monkeypatch):
    """The junction-aware name screens still refuse, and still run inside the hold.

    The hold settles the components; it does not make the existing fail-closed screens
    redundant, and removing one of them would be a security regression dressed as a
    simplification. Pinned by reporting a link through each predicate in turn while
    the filesystem holds plain directories."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)

    monkeypatch.setattr(
        cs.platform_compat, "is_link_or_junction", lambda p: os.fspath(p) == os.fspath(order)
    )
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))

    monkeypatch.setattr(cs.platform_compat, "is_link_or_junction", lambda p: False)
    monkeypatch.setattr(cs.platform_compat, "first_linked_ancestor", lambda p: str(order.parent))
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    assert not order.exists(), "neither refusal wrote the file"


def test_the_pinned_parent_branch_is_unchanged_on_a_posix_host(tmp_path):
    """Where a descriptor-relative rename exists, the write goes through the pinned
    parent and the held walk is not used at all.

    POSIX production takes this branch, so this is the pin that says POSIX behaviour
    did not move: the walk is reached only through the by-name fallback."""
    from kiro_crew import atomic_write as atomic_write_module

    if not atomic_write_module.pinned_parent_replace_supported():
        pytest.skip("this host has no descriptor-relative rename; the fallback is the only path")

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)

    walked: list[str] = []
    real_walk = cs._hold_chain_no_follow

    def note(directory, *, max_depth):
        walked.append(os.fspath(directory))
        return real_walk(directory, max_depth=max_depth)

    cs._hold_chain_no_follow = note  # noqa: B010 - restored below
    try:
        cs._write_unit_order(order, (sid,), max_depth=_generous(order.parent))
    finally:
        cs._hold_chain_no_follow = real_walk
    assert walked == [], "the pinned-parent branch must not take the by-name walk"
    assert order.read_text(encoding="utf-8").split() == [sid]


def test_the_unit_order_record_holds_the_chain_before_it_reads_the_name(tmp_path, monkeypatch):
    """Recording holds every component of the parent BEFORE the read resolves the name,
    and is still holding them when the write runs.

    The read names the whole path, exactly as the write does, so a hold that began only
    at the write would leave that resolution unscreened -- and on the platform with
    junctions, resolving one aimed at a UNC share is itself an outbound authentication.
    Asserted from inside the read, because a hold taken and released around the walk
    would satisfy an after-the-fact check. The write walks the chain a second time under
    the hold, which is why ``hold`` appears again between the read and the write."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text(f"{_unit(cid)}\n", encoding="utf-8")

    parent = order.parent
    components = [*reversed(parent.parents), parent]
    trace: list[str] = []
    handed: list[int] = []

    real_walk = cs._hold_chain_no_follow

    def walk(directory, *, max_depth):
        fds = real_walk(directory, max_depth=max_depth)
        handed.extend(fds)
        trace.append("hold")
        return fds

    real_read = cs._recorded_unit_order_at

    def read(path):
        trace.append("read")
        assert len(handed) == len(components), "the whole chain is held before the read"
        for component, fd in zip(components, handed, strict=True):
            try:
                info = os.fstat(fd)
            except OSError as exc:  # pragma: no cover - the mutation's path
                raise AssertionError(f"{component} was not held during the read") from exc
            on_disk = os.stat(component)
            assert (info.st_dev, info.st_ino) == (
                on_disk.st_dev,
                on_disk.st_ino,
            ), f"the descriptor held for {component} is not that component"
        return real_read(path)

    real_write = cs.atomic_write

    def write(path, content, **kw):
        trace.append("write")
        for fd in handed:
            os.fstat(fd)
        return real_write(path, content, **kw)

    monkeypatch.setattr(cs, "_hold_chain_no_follow", walk)
    monkeypatch.setattr(cs, "_recorded_unit_order_at", read)
    monkeypatch.setattr(cs, "atomic_write", write)
    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)

    assert trace[0] == "hold", "the hold is taken first"
    assert trace[1] == "read", "the read runs under it"
    assert trace[-1] == "write", "the write runs last, still under it"
    assert order.read_text(encoding="utf-8").split()[-1] == sid, "the record landed"
    for fd in handed:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_a_record_that_changes_nothing_still_releases_the_chain(tmp_path, monkeypatch):
    """A unit already newest is a bare read -- and the hold that read ran under is
    released, not leaked, on that early return.

    The common case by far: a crew recording repeatedly into the unit it is already in.
    A descriptor leaked once per cycle would exhaust the process, and on the platform
    the hold is for it would also keep a directory unrenamable for the gateway's life."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    _fallback_only(monkeypatch)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text(f"{sid}\n", encoding="utf-8")

    handed: list[int] = []
    real_walk = cs._hold_chain_no_follow

    def walk(directory, *, max_depth):
        fds = real_walk(directory, max_depth=max_depth)
        handed.extend(fds)
        return fds

    wrote: list[str] = []
    real_write = cs.atomic_write

    def write(path, content, **kw):
        wrote.append(os.fspath(path))
        return real_write(path, content, **kw)

    monkeypatch.setattr(cs, "_hold_chain_no_follow", walk)
    monkeypatch.setattr(cs, "atomic_write", write)
    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)

    assert wrote == [], "a unit already newest is not written again"
    assert handed, "the read still ran under a hold"
    for fd in handed:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_the_reads_walk_creates_nothing_under_the_directory_it_holds(tmp_path, monkeypatch):
    """Neither walk creates anything: an absent component refuses on the read path and on
    the write path alike.

    A read runs on a crew's every cycle, so a read whose walk created its own chain would
    write state on every read of a store that holds none. The write refuses for a second
    reason: a component it created would be empty, and an empty directory is the one that
    can take reparse data before the next step opens its child by name. The fold's answer
    for a crew that recorded nothing is header order -- which ``()`` is."""
    _fallback_only(monkeypatch)
    nowhere = tmp_path / "nowhere"
    assert not nowhere.exists(), "the directory starts absent"

    with pytest.raises(FileNotFoundError):
        cs._hold_chain_for_by_name_use(nowhere, max_depth=_generous(nowhere))
    assert not nowhere.exists(), "the walk created nothing"

    assert (
        cs._read_unit_order_held(
            nowhere / f"c_00000000{cs._UNIT_ORDER_SUFFIX}", max_depth=_generous(nowhere)
        )
        == ()
    )
    assert not nowhere.exists(), "and the read answered without creating it"

    with pytest.raises(FileNotFoundError):
        cs._hold_chain_no_follow(nowhere, max_depth=_generous(nowhere))
    assert not nowhere.exists(), "the write's walk does not create it either"


def test_a_component_that_cannot_be_held_stops_the_read_before_the_name(tmp_path, monkeypatch):
    """A component that refuses to open without following a link stops the read, and
    the name is never resolved through it.

    A reparse point at a component is what that refusal stands for: on the platform
    with junctions the open is the screen, so a read that fell back to resolving the
    name anyway would perform the traversal the refusal exists to prevent. The answer
    is ``()`` -- header order -- not an exception into a crew's read path."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text(f"{sid}\n", encoding="utf-8")
    _fallback_only(monkeypatch)

    real_pin = cs.platform_compat.pin_directory

    def refuse_that_component(component, **kwargs):
        if os.fspath(component) == os.fspath(order.parent):
            raise OSError("a reparse point sits at this component")
        return real_pin(component, **kwargs)

    resolved: list[str] = []
    real_read = cs._recorded_unit_order_at

    def read(path):
        resolved.append(os.fspath(path))
        return real_read(path)

    monkeypatch.setattr(cs.platform_compat, "pin_directory", refuse_that_component)
    monkeypatch.setattr(cs, "_recorded_unit_order_at", read)

    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "the read refuses"
    assert resolved == [], "the name was never resolved through the refused component"


def test_the_posix_read_path_takes_no_hold(tmp_path):
    """Where a descriptor-relative rename exists, the read takes no chain hold either.

    The hold's value is the platform's: a handle that blocks renaming and deleting, and
    an open that refuses a reparse point. POSIX has neither, writes through a pinned
    descriptor instead, and a walk here would refuse components that POSIX today
    resolves -- so this is the pin that says POSIX behaviour did not move."""
    from kiro_crew import atomic_write as atomic_write_module

    if not atomic_write_module.pinned_parent_replace_supported():
        pytest.skip("this host has no descriptor-relative rename; the fallback is the only path")

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)

    assert (
        cs._hold_chain_for_by_name_use(tmp_path, max_depth=_generous(tmp_path)) == []
    ), "no hold is taken"
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (sid,), "the read still works"


def test_the_derived_crews_path_is_exactly_where_crews_dir_lives(tmp_path, monkeypatch):
    """The derived path is the same place `store.repo_data_dir` would put it, with an
    explicit root and with the app's own.

    The derivation states that layout a second time, so this is the pin that stops the two
    drifting: a layout change in the store that the derivation did not follow would
    silently point every held read and write at a directory nothing else uses. Asserted
    against `store.repo_data_dir` rather than against `crews_dir`, because `crews_dir`
    delegates to the derivation -- comparing those two would move both sides at once and
    catch nothing. The `crews_dir` line is kept for the day it stops delegating."""
    assert (
        cs._crews_path(OWNER, REPO, tmp_path)
        == cs.store.repo_data_dir(OWNER, REPO, tmp_path) / "crews"
    )
    assert cs._crews_path(OWNER, REPO, tmp_path) == cs.crews_dir(OWNER, REPO, tmp_path)

    home = tmp_path / "default-home"
    monkeypatch.setattr(cs.store, "app_data_dir", lambda name: home / name)
    assert cs._crews_path(OWNER, REPO) == cs.store.repo_data_dir(OWNER, REPO) / "crews"
    assert home in cs._crews_path(OWNER, REPO).parents, "the default root is the app's own"


def test_deriving_the_order_path_touches_nothing(tmp_path):
    """Deriving the order file's path creates no directory and probes no component.

    The hold its callers take has to be the FIRST thing that resolves this chain. A
    `mkdir(parents=True)` while deriving would resolve the missing tail by name one line
    earlier, which is exactly the resolution the hold is there to prevent -- and it would
    make a read create the store it is reading."""
    nowhere = tmp_path / "nowhere"
    order = cs._unit_order_path(OWNER, REPO, "c_00000000", nowhere)

    assert not nowhere.exists(), "no component of the chain was created"
    assert order.name == f"c_00000000{cs._UNIT_ORDER_SUFFIX}"
    assert order.parent == cs._crews_path(OWNER, REPO, nowhere)
    assert not nowhere.exists(), "and reading the path back created nothing either"


def test_a_failure_to_derive_the_order_path_answers_header_order(tmp_path, monkeypatch):
    """A read whose path cannot be derived answers `()`, and does not raise.

    `()` means "this crew recorded no order", and the fold then applies header order --
    the answer every read gave before the order file existed. Raising instead would reach
    `crew_log_units`' broad guard, which answers `()` for EVERY unit, so a crew whose
    store directory is unusable would render an empty ledger rather than an unordered
    one; under `strict` it would refuse the write outright."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (sid,), "the order is there"

    def refuse(*_args, **_kwargs):
        raise OSError("the store directory is not usable")

    monkeypatch.setattr(cs, "_unit_order_path", refuse)
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "answered, did not raise"
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path) is not None, "the fold still answers"


def test_the_order_files_own_name_is_settled_by_the_open_not_by_a_stat(tmp_path, monkeypatch):
    """Reading the order file never stats its name: the open alone settles the leaf.

    An `os.stat` FOLLOWS a reparse point, so a probe before the open would traverse a
    junction planted at the order file's own name -- the outbound authentication this
    module screens for -- before the no-follow open could refuse it. The open answers
    every case a probe answered, as an `OSError` the read turns into `()`: absent file,
    directory, or link."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    assert order.is_file(), "the order file is there to be read"

    probed: list[str] = []
    real_is_file = Path.is_file

    def note(self):
        probed.append(os.fspath(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", note)
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (sid,), "the read still works"
    assert os.fspath(order) not in probed, "the leaf was never stat'ed by name"

    order.unlink()
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "an absent file answers ()"
    assert os.fspath(order) not in probed, "and answers it without a stat"

    order.mkdir()
    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "a directory answers ()"
    assert os.fspath(order) not in probed, "and that answer needs no stat either"


def test_a_link_at_the_order_files_own_name_reveals_nothing_of_its_target(tmp_path):
    """A link planted at the order file's name answers `()`; the target is not read.

    The lines behind such a link would be taken for this crew's unit ids and, because the
    writer re-states what it read, copied into the crew's own order file. The refusal is
    the open itself, so the target is never opened and never resolved."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)

    elsewhere = tmp_path / "somebody-elses.unit-order"
    elsewhere.write_text(f"{_unit(cid)}\n{_unit(cid)}\n", encoding="utf-8")
    order.symlink_to(elsewhere)

    assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "nothing was read"
    assert cs._recorded_unit_order_at(order) == (), "and the parse refuses it directly"
    assert order.is_symlink(), "the link is still there; the refusal did not remove it"
    assert elsewhere.read_text(encoding="utf-8").strip(), "its target is untouched"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_a_fifo_at_the_order_files_name_is_refused_without_waiting(tmp_path):
    """A FIFO planted at the order file's name answers `()` at once; it does not wait.

    `O_RDONLY` on a FIFO waits for a writer, and a planted one never has a writer. This
    read runs on a crew's every cycle, and on the recording path it runs inside the crew's
    write lock -- so one FIFO would stop every update of that crew, permanently. The alarm
    turns a wait into a failure instead of letting the suite hang."""
    crew, _sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(order)

    def complain(_signum, _frame):
        raise AssertionError("the read waited on the FIFO instead of refusing it")

    previous = signal.signal(signal.SIGALRM, complain)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        assert cs._recorded_unit_order_at(order) == (), "the FIFO is refused"
        assert cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == (), "and through the read"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc to count descriptors")
def test_a_directory_at_the_order_files_name_leaks_no_descriptor(tmp_path):
    """Repeated reads through a directory at the order file's name leak no descriptor.

    A directory opens successfully on POSIX, and `os.fdopen` does not close a descriptor
    it was handed when its own construction fails -- so the answer can be right while one
    descriptor is lost per read, and a crew page that runs this every cycle walks into
    `EMFILE`. Counted rather than reasoned about, because the answer looks identical
    either way."""
    crew, _sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.mkdir(parents=True, exist_ok=True)

    def open_descriptors():
        return len(os.listdir("/proc/self/fd"))

    before = open_descriptors()
    for _ in range(20):
        assert cs._recorded_unit_order_at(order) == (), "a directory answers ()"
    assert open_descriptors() <= before, "twenty reads left no descriptor behind"


def test_a_failed_fdopen_is_not_closed_a_second_time(tmp_path, monkeypatch):
    """`os.fdopen` owns the descriptor from the call, so a raise out of it leaves nothing
    to close here.

    `io.open` closes a descriptor handed to it when its own construction fails, and so
    does `FileIO.__init__` before that. A second `os.close` on the same number is not a
    harmless no-op: between the two closes another thread can open a file and be given
    that number, and the second close would take its descriptor away."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text(f"{sid}\n", encoding="utf-8")

    real_close = os.close
    closed: list[int] = []
    owned: list[int] = []

    def counting_close(fd):
        closed.append(fd)
        return real_close(fd)

    def fdopen_that_owns_then_fails(fd, *args, **kwargs):
        owned.append(fd)
        real_close(fd)  # what os.fdopen itself does on its own failure
        raise ValueError("unknown encoding")

    monkeypatch.setattr(cs.os, "fdopen", fdopen_that_owns_then_fails)
    monkeypatch.setattr(cs.os, "close", counting_close)

    assert cs._recorded_unit_order_at(order) == (), "the failure answers header order"

    assert owned, "os.fdopen was reached"
    assert owned[0] not in closed, "the module closed a descriptor os.fdopen already owned"


def test_a_leaf_that_is_not_a_regular_file_still_closes_its_descriptor(tmp_path, monkeypatch):
    """The kind check runs before the handoff, so that path closes the descriptor itself.

    The two halves of the ownership rule are separate claims: before `os.fdopen` this
    function owns the descriptor and must close it, after the call it must not. A
    directory at the order file's name is the case that opens successfully and is refused
    on kind, and a crew reads on every cycle, so a leak there is one per cycle."""
    crew, _ = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.mkdir(parents=True, exist_ok=True)

    real_close = os.close
    closed: list[int] = []

    def counting_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(cs.os, "close", counting_close)

    assert cs._recorded_unit_order_at(order) == (), "a directory answers header order"
    assert closed, "the descriptor was closed by this function, not leaked"


def _generous(path: Path) -> int:
    """A bound that always admits *path*, for a test not probing depth itself.

    Mirrors :func:`crew_store._chain_depth_bound`'s shape -- the base's own depth plus
    the suffix allowance -- so a test that is not about the bound cannot trip it, and
    cannot pass by accident either if the allowance shrinks to zero.
    """
    return len(path.parts) + cs._MAX_SUFFIX_DEPTH


def _past_the_bound(start: Path, bound: int) -> Path:
    """*start* extended until it is one component past *bound*.

    The depth is MEASURED off the path rather than written down here. A fixture built
    to a literal number agrees with the bound only until one of them moves, and the
    one that moves silently is the literal.
    """
    deep = start
    while len(deep.parts) <= bound:
        deep = deep / "g"
    return deep


def _pin_spy(monkeypatch) -> list[str]:
    """Record every component the walk opens, and keep the real open underneath."""
    opened: list[str] = []
    real_pin = cs.platform_compat.pin_directory

    def counting_pin(component, **kwargs):
        opened.append(os.fspath(component))
        return real_pin(component, **kwargs)

    monkeypatch.setattr(cs.platform_compat, "pin_directory", counting_pin)
    return opened


def test_a_relative_chain_is_refused_before_any_component_is_opened(tmp_path, monkeypatch):
    """A relative path is refused, and refused before the walk opens anything.

    Its components resolve against a current directory the walk never inspects, so the
    chain it would hold is not the chain the caller named. The test runs from INSIDE a
    real directory whose children exist, so the walk genuinely could have resolved
    these paths -- a refusal measured somewhere nothing would have resolved anyway
    proves only that the fixture was empty.

    ``.`` and a multi-component relative path are both checked because they fail
    differently without the guard: ``.`` resolves to the current directory and would be
    held successfully, while the deeper one dies on its second component. Only the
    first of those looks like a hold of the wrong object.
    """
    inside = tmp_path / "inside"
    (inside / "child").mkdir(parents=True)
    monkeypatch.chdir(inside)
    opened = _pin_spy(monkeypatch)

    for relative in (Path("."), Path("child"), Path("child/deeper")):
        with pytest.raises(ValueError, match="relative"):
            cs._hold_chain_no_follow(relative, max_depth=_generous(inside))

    assert opened == [], "no component of a relative path is opened"


def test_a_chain_past_the_depth_bound_is_refused_before_any_component_is_opened(
    tmp_path, monkeypatch
):
    """A path deeper than the bound is refused, ahead of the first open.

    The timing is the claim. One open per component is the walk's whole cost, so a
    depth nothing bounds is work performed INSIDE the hold before any of the work the
    hold protects begins -- and a bound enforced after the walk would raise the same
    exception having already paid for it. The open count is what tells those apart.

    The refusal names the depth it measured, so a reader of the log learns which path
    was too deep rather than only that some path was.
    """
    opened = _pin_spy(monkeypatch)
    bound = _generous(tmp_path)
    deep = _past_the_bound(tmp_path, bound)
    measured = len(deep.parts)
    assert measured > bound, "the fixture is past the bound"

    with pytest.raises(ValueError) as refusal:
        cs._hold_chain_no_follow(deep, max_depth=bound)

    assert str(measured) in str(refusal.value), "the refusal reports the depth it measured"
    assert opened == [], "the bound refused before the walk opened anything"


def test_the_depth_bound_refuses_a_chain_the_walk_would_otherwise_hold(tmp_path):
    """The bound refuses a chain that EXISTS all the way down, and holds the one below it.

    An absent component refuses on its own, so a fixture that does not exist cannot
    tell a depth bound from a missing directory: both raise, and only one of them is
    this guard. This chain exists to its last component, so without the bound the walk
    holds it and returns descriptors -- which makes the refusal the bound's alone.

    The shallower control is the other half. A guard that refused every deep-looking
    path would satisfy the assertion above while breaking every real store, so the
    chain one component inside the bound has to come back fully held, with the
    descriptor count measured off the path rather than written down.
    """
    bound = _generous(tmp_path)
    deep = _past_the_bound(tmp_path, bound)
    deep.mkdir(parents=True)
    measured = len(deep.parts)

    with pytest.raises(ValueError) as refusal:
        cs._hold_chain_no_follow(deep, max_depth=bound)
    assert str(measured) in str(refusal.value), "the refusal reports the depth it measured"

    at_the_bound = deep
    while len(at_the_bound.parts) > bound:
        at_the_bound = at_the_bound.parent
    assert at_the_bound.is_dir(), "the control chain exists too"

    held = cs._hold_chain_no_follow(at_the_bound, max_depth=bound)
    try:
        assert len(held) == len(at_the_bound.parts), (
            "a chain inside the bound is held component for component, so the bound "
            "refuses depth rather than refusing this shape of path"
        )
    finally:
        cs._release_held(held)


def test_a_chain_the_walk_refuses_reads_as_header_order(tmp_path, monkeypatch):
    """A chain the walk will not chase answers ``()``, instead of raising into a read.

    The read's contract is that an absent or unholdable component falls back to header
    order, and these two refusals are ``ValueError`` where every earlier one was an
    ``OSError``. A read that let them through would turn a crew's ordinary listing into
    an exception, which is the one outcome the fallback exists to avoid.
    """
    _fallback_only(monkeypatch)
    bound = _generous(tmp_path)
    deep = _past_the_bound(tmp_path, bound)
    deep.mkdir(parents=True)

    with pytest.raises(ValueError):
        cs._hold_chain_for_by_name_use(deep, max_depth=bound)
    over_deep = cs._read_unit_order_held(
        deep / f"c_00000000{cs._UNIT_ORDER_SUFFIX}", max_depth=bound
    )
    assert over_deep == (), "an over-deep chain reads as header order"

    monkeypatch.chdir(tmp_path)
    relative = cs._read_unit_order_held(Path(f"c_00000000{cs._UNIT_ORDER_SUFFIX}"), max_depth=bound)
    assert relative == (), "and so does a relative one"


def test_a_walk_refusal_does_not_escape_the_recorder(tmp_path, monkeypatch):
    """A ``ValueError`` from the walk is contained by the recorder, not raised at it.

    The recorder's contract is that a unit order it cannot write is logged and dropped,
    never raised into the caller that was recording a crew's unit. The walk's two
    refusals are ``ValueError`` where every earlier failure was an ``OSError``, so a
    guard admitting only ``OSError`` would turn a deep store path into an exception in
    ordinary use.

    Asserted BEHAVIOURALLY: the condition that makes the real walk refuse is built, the
    recorder is called, and nothing may escape. Not asserted structurally -- a check that
    the call sits inside some ``try`` verifies the SHAPE of the code, which a refactor can
    preserve while destroying the behaviour (a guard around a call that does not reach the
    walk) or destroy while preserving it (the guard moved into a helper). Only the escape
    itself tells those apart.

    The control matters more than the call here: without it this test also passes when the
    fixture is not actually past the bound, because then the walk never refuses and there
    is nothing to escape. So the refusal is demonstrated on the same path first.
    """
    _fallback_only(monkeypatch)
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]

    # The refusal must come from the UNTRUSTED suffix, not from a deep data root: after
    # the suffix bound replaced the total-depth cap, a deep root cannot refuse at all,
    # which is the whole point. So the depth is grown in `owner`, the provider namespace.
    # Measured off the constructed path, never written down as a literal.
    owner = "g"
    while len(cs._crews_path(owner, REPO, tmp_path).parts) <= cs._chain_depth_bound(tmp_path):
        owner = f"{owner}/g"
    chain = cs._crews_path(owner, REPO, tmp_path)

    with pytest.raises(ValueError):
        cs._hold_chain_for_by_name_use(chain, max_depth=cs._chain_depth_bound(tmp_path))

    cs._record_unit_order(owner, REPO, cid, sid, tmp_path)

    written = cs._unit_order_path(owner, REPO, cid, tmp_path)
    assert not written.exists(), "the refusal dropped the write instead of writing unheld"


def test_a_deep_data_home_still_records_its_unit_order(tmp_path, monkeypatch):
    """A legitimately deep data root records and reads its order file, ordering intact.

    This is the ORDERING property, pinned directly rather than through the cap's value.
    An operator's data root is configuration, not untrusted input, so spending one budget
    on it and on the provider namespace made a deep-but-valid home refuse every write --
    which drops the order file, leaves the header-clock fallback permanently in force for
    that home, and lets a fold apply a retired unit's phases over the current ones after
    a backward clock step (see :data:`crew_store._UNIT_ORDER_SUFFIX`).

    So the assertion is that the recorded order survives, not that some number is below
    some other number: a later refactor fails here because ordering broke, not because a
    bound moved.
    """
    _fallback_only(monkeypatch)

    deep_root = tmp_path
    while len(deep_root.parts) < len(tmp_path.parts) + 40:
        deep_root = deep_root / "d"
    deep_root.mkdir(parents=True, exist_ok=True)

    crew, sid = _live_crew(deep_root)
    cid = crew["id"]
    cs._record_unit_order(OWNER, REPO, cid, sid, deep_root)

    assert cs._recorded_unit_order(OWNER, REPO, cid, deep_root) == (
        sid,
    ), "a deep but legitimate data home keeps its recorded unit order"


def test_a_failure_resolving_the_bound_does_not_escape_either_entry(tmp_path, monkeypatch):
    """An ``OSError`` from resolving the bound is contained, on the write AND the read.

    The bound resolves through :func:`store.app_data_dir`, which mkdirs, so it is fallible
    for the same reasons the path derivation is -- a non-directory planted at the store, or
    a read-only volume. Computed outside the containment ``try`` it would escape, turning
    the documented best-effort write ("a crew whose order cannot be written folds its units
    in header order instead") into a raised one on every production call, where ``root`` is
    ``None``.

    Asserted behaviourally on both entry points, because they guard separately and a fix
    applied to one says nothing about the other. The failure is injected at the bound
    helper itself rather than at a filesystem, so the test names the contained call instead
    of depending on what a given host lets a test plant.
    """

    def refuse(_root):
        raise OSError("the store's data dir could not be resolved")

    monkeypatch.setattr(cs, "_chain_depth_bound", refuse)

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]

    cs._record_unit_order(OWNER, REPO, cid, sid, tmp_path)

    assert (
        cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == ()
    ), "the read falls back to header order instead of raising"


def test_the_walk_will_not_run_without_a_declared_bound(tmp_path):
    """Omitting the bound is refused, so a missing cap can never mean an unbounded walk.

    The bound is the caller's declared intent and the walk cannot infer it -- a default
    would be a policy the walk chose for callers it cannot see. Making it a REQUIRED
    keyword turns an omission into a failure at the call, which is the fail-closed
    direction; a defaulted parameter would make the same omission silently permissive.
    """
    with pytest.raises(TypeError):
        cs._hold_chain_no_follow(tmp_path)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        cs._hold_chain_for_by_name_use(tmp_path)  # type: ignore[call-arg]

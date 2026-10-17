"""A member savepoint is refused once the bytes its state was folded from change.

``MemberLog.last_seq`` records the rule these tests are about: a damaged committed
line is skipped on load, so a reader loses that line and not the file. A cold fold
then omits what that line contributed, while a savepoint written before the damage
keeps it -- and a resumed fold never revisits the region below its watermark, so the
two reads disagree for the life of the member rather than for one load. A savepoint
may lag; it may not hold a value no later read reproduces.

The prefix digest is the condition that catches it, and these tests are about the
member log EVALUATING it. The digest's own case analysis belongs to the one predicate
both clients call and is covered in ``test_crew_log_checkpoint.py``.
"""

from __future__ import annotations

import json
import shutil

import pytest

from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.crew_log.store import crew_log_dir, crew_log_path
from kiro_crew.eventlog import members_projections
from kiro_crew.eventlog import service as service_mod
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog
from kiro_crew.eventlog.service import MemberEventLogService
from kiro_crew.projection import EMPTY_WATERMARK, ProjectionRegistry

SLUG = "alice"

#: One ``member/config`` event per entry, each setting a field the roster fold keeps
#: under its own name. A field per entry is what makes a single damaged line visible:
#: last-write-wins on one field would hide it behind the next event.
SEEDED_CONFIG = ({"model": "m1"}, {"workspace": "w1"}, {"avatar": "a1"})


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture(autouse=True)
def _savepoint_after_two_events(monkeypatch):
    """Earn a savepoint write after two folded events instead of the real 256.

    The threshold is not what these tests are about, and appending 256 events each to
    reach it would pay a re-read of the whole file per append.
    """
    monkeypatch.setattr(service_mod, "_SAVEPOINT_MIN_ADVANCE", 2)
    yield


@pytest.fixture
def serializable_units(monkeypatch):
    """Register only the units whose fold state can actually be written.

    ``DrivingProjection`` holds its open slots as a ``frozenset``, which
    ``json.dumps`` refuses, so its savepoint never reaches disk -- and a set missing
    one unit makes every member load fold cold. A savepoint resume therefore cannot
    happen at all with the full set, which is a separate defect pinned below. These
    tests are about the condition that guards the resume, so they register the three
    units that can be saved and exercise the real service around them.
    """
    units = [u for u in members_projections.all_units() if u.key != types.PROJ_DRIVING]
    assert len(units) == 3, "the unit set moved; re-derive which ones can be saved"
    monkeypatch.setattr(service_mod, "all_units", lambda: [type(u)() for u in units])
    yield


def _service(tmp_path, monkeypatch) -> MemberEventLogService:
    import kiro_crew.members as members

    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    root = tmp_path / "members"
    root.mkdir(exist_ok=True)
    return MemberEventLogService(root)


def _savepoint_dir(slug: str = SLUG):
    return crew_log_dir(KIND_MEMBER, slug) / "projections" / slug


def _savepoint_files(slug: str = SLUG) -> list:
    directory = _savepoint_dir(slug)
    return sorted(directory.glob("*.json")) if directory.exists() else []


def _config_fields(svc: MemberEventLogService, slug: str = SLUG) -> dict:
    """The roster view's folded config fields, with the read-time overlay dropped.

    ``snapshot`` overlays the slug and the header name onto every roster view, so
    they say nothing about which entries were folded.
    """
    view = svc.snapshot(slug)["values"][types.PROJ_ROSTER]
    return {k: v for k, v in view.items() if k not in ("slug", "name")}


def _seed(tmp_path, monkeypatch, events=SEEDED_CONFIG) -> MemberEventLogService:
    svc = _service(tmp_path, monkeypatch)
    svc.ensure(SLUG, "Alice")
    for data in events:
        svc.append(SLUG, types.MEMBER_CONFIG, dict(data))
    return svc


def _write_savepoints(tmp_path, monkeypatch) -> MemberEventLogService:
    """A fresh service whose cold fold earns and writes the savepoint files.

    ``ensure`` stays on the plain fold, so the write falls to the next service that
    loads the member rather than to the one that appended.
    """
    svc = _service(tmp_path, monkeypatch)
    svc.snapshot(SLUG)
    return svc


def _damage_folded_line(seq: int, slug: str = SLUG) -> None:
    """Make the committed record at *seq* unparseable, leaving the file loadable.

    The store skips a damaged interior line and tolerates the forward seq gap it
    leaves, which is the whole premise: the file survives and one entry does not.

    Bytes in and bytes out, never text. A text write applies newline translation, so
    on a platform whose line separator is not the one the store wrote it would rewrite
    every record's ending and move the whole file -- damage far wider than the one
    record, which is exactly what a prefix digest is built to notice. The length check
    below holds the helper to the damage it claims to do.
    """
    path = crew_log_path(KIND_MEMBER, slug)
    before = path.read_bytes()
    lines = before.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("seq") == seq:
            assert line.startswith(stripped), "record has leading whitespace"
            # Same length and still one line, so only this record's bytes change and
            # the framing of every record after it is untouched.
            lines[index] = b"x" * len(stripped) + line[len(stripped) :]
            path.write_bytes(b"".join(lines))
            assert len(path.read_bytes()) == len(before), "damage moved another record"
            return
    raise AssertionError(f"no committed record at seq {seq} to damage")


def _rewrite_savepoints(edit, slug: str = SLUG) -> None:
    """Apply *edit* to every savepoint payload this member has on disk.

    Bytes out, like every other write here: the store itself writes these payloads
    with newline translation off so they are byte-identical across platforms, and a
    test that rewrote them as text would not be writing what the product writes.
    """
    written = 0
    for path in _savepoint_files(slug):
        raw = json.loads(path.read_bytes().decode("utf-8"))
        edit(raw)
        path.write_bytes(json.dumps(raw, sort_keys=True).encode("utf-8"))
        written += 1
    assert written, "no savepoint was written"


class TestADamagedFoldedLineRetiresTheSavepoint:
    def test_a_resumed_fold_agrees_with_a_cold_fold_over_the_damaged_file(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """The defect: the resumed read serves a value no later read reproduces."""
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)
        assert _savepoint_files(), "the premise needs savepoints on disk"

        _damage_folded_line(2)

        resumed = _config_fields(_service(tmp_path, monkeypatch))

        # The same file with no savepoint to resume from. This is what every later
        # cold reader gets, so it is what the resumed read has to match: lagging is
        # allowed, disagreeing is not.
        shutil.rmtree(_savepoint_dir())
        cold = _config_fields(_service(tmp_path, monkeypatch))

        assert cold == {"model": "m1", "avatar": "a1"}, "the store skips the damaged line"
        assert resumed == cold

    def test_the_savepoint_survives_growth_above_its_boundary(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """Appending is not damage: the walk stops at the count the witness names.

        Without this the change would be a savepoint that is never usable, which puts
        back the cost savepoints exist to remove instead of buying correctness.
        """
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)
        before = sorted(p.read_bytes() for p in _savepoint_files())

        grower = _service(tmp_path, monkeypatch)
        grower.append(SLUG, types.MEMBER_CONFIG, {"source": "s1"})

        assert _config_fields(_service(tmp_path, monkeypatch)) == {
            "model": "m1",
            "workspace": "w1",
            "avatar": "a1",
            "source": "s1",
        }
        # Still the files written before the growth. A resume that had silently
        # refolded cold would satisfy the assertion above for the wrong reason, and a
        # tail this short earns no rewrite.
        assert sorted(p.read_bytes() for p in _savepoint_files()) == before


class TestAResumeWhoseOwnPrefixMovedIsNotServed:
    """The window between admitting a savepoint and finishing the pass that used it.

    ``admit`` runs while the savepoints load, the witness is read once ahead of the
    fold, and the tail is folded after both. A change below the watermark inside that
    window is admitted on bytes that were still intact, and a resumed fold never
    returns to that region to notice -- so refusing the WRITE is not enough: the state
    is already in the registry and would be served for the life of the instance.
    """

    @staticmethod
    def _record_floors(monkeypatch) -> list:
        """Capture the watermark each pass resumed from, so a resume can be asserted.

        Without this a fall-back is indistinguishable from a load that never resumed:
        a cold fold reaches the same value, so the assertion would pass for the wrong
        reason exactly when the guard is absent.
        """
        floors: list = []
        real = ProjectionRegistry.prime_checkpointed

        def spy(self, *args, **kwargs):
            floor = real(self, *args, **kwargs)
            floors.append(floor)
            return floor

        monkeypatch.setattr(ProjectionRegistry, "prime_checkpointed", spy)
        return floors

    def test_bytes_that_move_after_the_witness_is_read_are_refolded_from_the_start(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """Damage lands the moment the witness is in hand: inside the window itself."""
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)
        assert _savepoint_files(), "the premise needs savepoints on disk"

        floors = self._record_floors(monkeypatch)
        real_witness = MemberLog.checkpoint_witness

        def damage_once_the_witness_is_read(self, seq):
            prefix = real_witness(self, seq)
            _damage_folded_line(2)
            return prefix

        monkeypatch.setattr(MemberLog, "checkpoint_witness", damage_once_the_witness_is_read)

        served = _config_fields(_service(tmp_path, monkeypatch))

        assert floors and floors[0] != EMPTY_WATERMARK, "the premise needs a real resume"
        # What the damaged file says, which is what every later cold reader gets. The
        # resumed state still holds the damaged entry's ``workspace``, so serving it
        # is the disagreement this change exists to prevent.
        assert served == {"model": "m1", "avatar": "a1"}

    def test_a_resume_with_no_witness_to_check_falls_back_rather_than_being_trusted(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """A boundary the file does not resolve leaves nothing to compare.

        The resume already happened by then, so the only safe answer is to fold from
        the start. The stub stands in for both halves of the case it is about: the walk
        resolving no boundary, and the bytes moving anyway -- damage before the load
        would be caught while the savepoints were still being admitted, and damage
        after an intact pass is not observable, so the window is the only place this
        branch decides anything.
        """
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)

        floors = self._record_floors(monkeypatch)

        def resolve_no_boundary(self, seq):
            _damage_folded_line(2)
            return None

        monkeypatch.setattr(MemberLog, "checkpoint_witness", resolve_no_boundary)

        served = _config_fields(_service(tmp_path, monkeypatch))

        assert floors and floors[0] != EMPTY_WATERMARK, "the premise needs a real resume"
        assert served == {"model": "m1", "avatar": "a1"}

    def test_a_load_that_resumed_nothing_folds_once_rather_than_twice(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """The recheck asks about a RESUMED prefix, so a cold pass must not reach it.

        A cold pass already folded the whole file through the tail it was handed, and
        it holds no witness when its tail is too short to owe a write -- so a recheck
        that ignored the watermark would read that absence as a failed check and fold
        the same file a second time. Correct either way, which is why the cost needs
        its own case.
        """
        _seed(tmp_path, monkeypatch, events=({"model": "m1"},))
        monkeypatch.setattr(service_mod, "_SAVEPOINT_MIN_ADVANCE", 256)

        floors = self._record_floors(monkeypatch)
        primes: list = []
        real_prime = ProjectionRegistry.prime

        def spy(self, slug, events):
            primes.append(slug)
            return real_prime(self, slug, events)

        monkeypatch.setattr(ProjectionRegistry, "prime", spy)

        assert _config_fields(_service(tmp_path, monkeypatch)) == {"model": "m1"}
        assert floors == [EMPTY_WATERMARK], "the premise needs a cold pass"
        assert primes == [], "the cold pass folded through its tail, so nothing re-folds"


class TestTheWriteRecordsTheEvidenceALaterReadNeeds:
    def test_a_written_savepoint_carries_the_digest_of_the_prefix_it_folded(
        self, tmp_path, monkeypatch, serializable_units
    ):
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)

        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in _savepoint_files()]
        assert len(payloads) == 3

        for raw in payloads:
            witness = raw["witness"]
            assert witness["seq"] == raw["watermark"] == 3
            assert len(witness["prefix_sha"]) == 64
            # Raw records, so at least one per entry in the span. The segment header
            # is excluded from the digest, which is why this is not a line count.
            assert witness["prefix_records"] >= 3

    def test_a_savepoint_written_before_the_witness_existed_is_refused(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """Retirement, not a guess: such a payload says nothing about its own bytes.

        It is also the state every member savepoint already on disk is in, so the
        first load after this change folds cold once and writes a checkable file.
        """
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)
        _rewrite_savepoints(lambda raw: raw.pop("witness", None))

        # Damage a folded entry, so a savepoint that was wrongly admitted shows up as
        # the stale value instead of being indistinguishable from a good resume.
        _damage_folded_line(2)

        assert _config_fields(_service(tmp_path, monkeypatch)) == {
            "model": "m1",
            "avatar": "a1",
        }

    def test_an_earned_write_with_no_witness_in_hand_writes_nothing(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """The threshold is met and there is still nothing to certify the bytes with.

        Reachable without any damage: the witness read asks for one boundary, and a
        seq the file does not resolve to a boundary yields no witness at all while the
        tail is long enough to owe a write. The refusal has to be its own step,
        because the recheck below it dereferences the witness it is given.
        """
        _seed(tmp_path, monkeypatch)
        svc = _service(tmp_path, monkeypatch)
        log = MemberLog(SLUG)
        log.load()
        identity = log.checkpoint_identity()
        assert identity is not None

        svc._maybe_save_savepoints(SLUG, log, identity, 0, None)

        assert _savepoint_files() == []

    def test_an_earned_write_is_refused_when_the_covered_bytes_moved_mid_pass(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """A witness read before the fold is evidence only if it still holds after it.

        The write is the last moment that can refuse: once a payload is on disk it
        claims a prefix it was never folded from, and every later resume recomputes
        the digest from those same changed bytes, so the claim keeps passing.

        The registry has to be holding folded state at the witness's own seq for this
        to mean anything -- an unprimed registry offers no savepoint at that watermark,
        so the write writes nothing whether the guard is there or not.
        """
        _seed(tmp_path, monkeypatch)
        svc = _write_savepoints(tmp_path, monkeypatch)
        log = MemberLog(SLUG)
        log.load()
        identity = log.checkpoint_identity()
        witness = log.checkpoint_witness(log.last_seq())
        assert identity is not None and witness is not None
        assert log.checkpoint_prefix_unchanged(witness), "the witness must start valid"

        # The primed fold already earned a write, so clear the files it left: a refusal
        # is only observable as nothing REAPPEARING.
        for path in _savepoint_files():
            path.unlink()
        assert _savepoint_files() == []

        _damage_folded_line(2)
        assert not log.checkpoint_prefix_unchanged(witness)

        svc._maybe_save_savepoints(SLUG, log, identity, 0, witness)

        assert _savepoint_files() == []


class TestTheBoundaryWalkIsNotPaidByAReadThatCanWriteNothing:
    def test_a_tail_short_of_the_threshold_reads_no_witness(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """The boundary walk decodes one record at a time, so it is gated.

        Resolving the record count is the only decode-per-record read on this path.
        Charging every load for it would put back the cost savepoints exist to
        remove, so it runs only where a write could be owed.
        """
        _seed(tmp_path, monkeypatch, events=({"model": "m1"},))
        monkeypatch.setattr(service_mod, "_SAVEPOINT_MIN_ADVANCE", 256)

        seqs: list[int] = []
        real = MemberLog.checkpoint_witness

        def spy(self, seq):
            seqs.append(seq)
            return real(self, seq)

        monkeypatch.setattr(MemberLog, "checkpoint_witness", spy)

        assert _config_fields(_service(tmp_path, monkeypatch)) == {"model": "m1"}
        assert seqs == []
        assert not _savepoint_files()


class TestResidualsThisChangeDoesNotClose:
    def test_the_full_unit_set_still_never_resumes_from_a_savepoint(self, tmp_path, monkeypatch):
        """PINNED, asserting today's answer rather than the one we would want.

        ``DrivingProjection`` holds its open slots as a ``frozenset``, so the kernel
        store cannot serialize its state and its file never reaches disk. A set
        missing one unit drops the whole floor to empty, so every member load folds
        cold and the shortcut never fires. This change puts the guard in place ahead
        of the shortcut; turning the shortcut on without it is what would make the
        disagreement live. Fix the state shape and this assertion flips.
        """
        _seed(tmp_path, monkeypatch)

        floors: list[int] = []
        real = ProjectionRegistry.prime_checkpointed

        def spy(self, *args, **kwargs):
            floor = real(self, *args, **kwargs)
            floors.append(floor)
            return floor

        monkeypatch.setattr(ProjectionRegistry, "prime_checkpointed", spy)

        _write_savepoints(tmp_path, monkeypatch)
        assert floors == [EMPTY_WATERMARK], "a cold fold, not a resume"
        assert [p.name for p in _savepoint_files()] == [
            "activity.json",
            "roster.json",
            "wake.json",
        ], "the driving unit's state cannot be serialized, so it writes no file"

        _service(tmp_path, monkeypatch).snapshot(SLUG)
        assert floors == [EMPTY_WATERMARK, EMPTY_WATERMARK], "still cold with files present"

    def test_a_witness_below_its_own_watermark_is_still_admitted(
        self, tmp_path, monkeypatch, serializable_units
    ):
        """PINNED, asserting today's answer rather than the one we would want.

        A witness certifies ONE boundary. The predicate verifies the prefix through
        the seq the witness names, and the kernel restores the state at the
        ``watermark`` beside it, so entries between the two are covered by neither.
        The crew log rejects such a file in its own resume step; the member log has
        no equivalent step, because the kernel hands ``admit`` the identity and the
        witness and not the watermark it is about to restore.

        Not reachable from this writer: it stamps one witness per pass and skips any
        unit standing at another seq. It takes an edit inside the member's own log
        directory, which is fenced from the sandbox and from the agent file tools.
        """
        _seed(tmp_path, monkeypatch)
        _write_savepoints(tmp_path, monkeypatch)

        # Certify only the first entry while the state still stands at the third,
        # leaving entries 2 and 3 covered by nothing.
        first = MemberLog(SLUG).checkpoint_witness(1)
        assert first is not None
        _rewrite_savepoints(
            lambda raw: raw.update(
                witness={
                    "seq": first.seq,
                    "prefix_sha": first.sha,
                    "prefix_records": first.records,
                }
            )
        )

        _damage_folded_line(2)

        # The stale value, served because the damaged entry sits above the certified
        # prefix. Change this assertion when the residual is closed.
        assert _config_fields(_service(tmp_path, monkeypatch)) == {
            "model": "m1",
            "workspace": "w1",
            "avatar": "a1",
        }

"""The product-owned launch record, and what moving it out of ``cloud.json`` has to preserve.

The launch path's own profile, region and tag live in a file it owns, separate from the one an
operator hand-edits. These tests pin both halves of that separation: the record works on its
own, and an install whose pointer is still in the configuration file keeps resuming.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.config import DEFAULT_REGION, CloudConfig
from kiro_crew.cloud.launch_state import LaunchState, state_path


class TestTheLaunchRecord:
    """One writer, three fields, and no bytes of anyone else's in the file."""

    def test_a_record_round_trips(self, tmp_path):
        p = tmp_path / "cloud_launch_state.json"

        LaunchState.record(profile="work", region="eu-west-1", last_tag="kc-a1b2c3", path=p)

        state = LaunchState.load(p)
        assert (state.profile, state.region, state.last_tag) == ("work", "eu-west-1", "kc-a1b2c3")

    def test_it_is_written_where_the_crew_home_is(self, tmp_path, monkeypatch):
        """Under ``config_dir()``, so ``KIROCREW_HOME`` moves it with everything else."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        assert state_path().parent == tmp_path
        assert state_path().name == "cloud_launch_state.json"

    def test_the_record_is_frozen(self):
        """A caller cannot mutate it and believe the change reached disk.

        The shape this replaces was load-mutate-save, and the wizard did exactly that: it set
        three in-memory fields before the deploy so its progress output had the tag, and the
        write came minutes later. Frozen means the write is the only way to change the file,
        so there is no half-applied state to reason about.
        """
        with pytest.raises(Exception) as exc:
            LaunchState().profile = "nope"  # type: ignore[misc]
        assert "FrozenInstanceError" in type(exc.value).__name__

    @pytest.mark.parametrize(
        ("label", "blob"),
        [
            ("truncated", b'{"profile": "work"'),
            ("not utf-8", b"\xff\xfe not text"),
            ("not an object", b'["a", "list"]'),
            ("empty", b""),
        ],
    )
    def test_an_unusable_record_reads_as_unset_rather_than_raising(
        self, tmp_path, monkeypatch, label, blob
    ):
        """A cloud command must not hand an operator a traceback over a file it can ignore.

        Same tolerant policy the configuration reader has, for the same reason, and it matters
        more here: this file is the one every ``cloud`` subcommand reads to find the tag.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"
        p.write_bytes(blob)

        state = LaunchState.load(p)

        assert (state.profile, state.last_tag) == ("", ""), label

    def test_a_malformed_tag_reads_as_no_tag(self, tmp_path, monkeypatch):
        """Sanitised at the boundary, because the resume path's ``validate_tag`` raises.

        An empty tag already means "no last launch", which is a state every caller handles, so
        a malformed one is answered with that rather than carried inward.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"
        p.write_text(json.dumps({"profile": "w", "region": "us-east-1", "last_tag": "kc/../../x"}))

        assert LaunchState.load(p).last_tag == ""
        # The rest of the record still reads: one bad field is not a bad document.
        assert LaunchState.load(p).profile == "w"

    def test_an_absent_record_is_unset_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        state = LaunchState.load(tmp_path / "nothing-here.json")

        assert (state.profile, state.region, state.last_tag) == ("", DEFAULT_REGION, "")


class TestTheReadIsBoundedBeforeItAllocates:
    """An oversized record must never be materialised, only refused.

    A size check that runs AFTER the read is not a bound: by the time it can look at the
    length, the bytes are already in memory. And the file does not have to be written a byte at
    a time to be enormous -- ``truncate -s`` produces a sparse one instantly, which is within
    reach of anything with filesystem access.
    """

    def test_an_oversized_record_is_refused(self, tmp_path):
        from kiro_crew.cloud.launch_state import _MAX_FILE_BYTES

        p = tmp_path / "cloud_launch_state.json"
        # Sparse, so the test costs no disk: the file reports its full size and the read is
        # what must refuse to pull it in.
        with open(p, "wb") as fh:
            fh.truncate(_MAX_FILE_BYTES * 64)

        assert LaunchState.load(p).last_tag == ""

    def test_the_read_asks_for_no_more_than_the_ceiling(self, tmp_path, monkeypatch):
        """Pinned on the SIZE REQUESTED, which is the property under test.

        Asserting only that an oversized file reads as unset passes for an implementation that
        reads the whole thing and then discards it -- the exact shape being fixed. So this
        records what the reader asked the file for.

        Instrumented on ``os.read`` rather than on a file object's ``read``. The record is now
        opened with ``os.open`` so the alias check can ``fstat`` the descriptor the bytes came
        from, and a watcher on ``builtins.open`` sees nothing there and would pass vacuously.
        The emptiness assertion is what makes that kind of drift loud instead of silent, and it
        is why it stays.
        """
        import os as _os

        from kiro_crew.cloud.launch_state import _MAX_FILE_BYTES

        p = tmp_path / "cloud_launch_state.json"
        with open(p, "wb") as fh:
            fh.truncate(_MAX_FILE_BYTES * 64)
        asked: list = []
        watched: set = set()

        real_os_open, real_os_read = _os.open, _os.read

        def _watching_open(path, *a, **k):
            fd = real_os_open(path, *a, **k)
            if str(path) == str(p):
                watched.add(fd)
            return fd

        def _watching_read(fd, size, *a, **k):
            if fd in watched:
                asked.append(size)
            return real_os_read(fd, size, *a, **k)

        monkeypatch.setattr(_os, "open", _watching_open)
        monkeypatch.setattr(_os, "read", _watching_read)
        LaunchState.load(p)

        assert asked, "the reader never read the file, so this measures nothing"
        # Any single request past the ceiling means more than the bound was asked for.
        assert all(0 < n <= _MAX_FILE_BYTES + 1 for n in asked), asked
        # And the loop must stop at the budget rather than draining the rest of the file:
        # a short-read loop with no budget would keep asking until EOF.
        assert sum(asked) <= _MAX_FILE_BYTES + 1, asked

    def test_a_record_at_exactly_the_ceiling_still_reads(self, tmp_path):
        """The allowed size is allowed: a document exactly at the limit is not refused."""
        from kiro_crew.cloud.launch_state import _MAX_FILE_BYTES

        p = tmp_path / "cloud_launch_state.json"
        body = {"profile": "p", "region": "us-east-1", "last_tag": "kc-a1b2c3"}
        # Pad inside an ignored key up to exactly the ceiling.
        pad = _MAX_FILE_BYTES - len(json.dumps({**body, "_pad": ""}))
        text = json.dumps({**body, "_pad": "x" * pad})
        assert len(text.encode()) == _MAX_FILE_BYTES, len(text.encode())
        p.write_text(text)

        assert LaunchState.load(p).last_tag == "kc-a1b2c3"

    def test_an_oversized_file_is_refused_even_when_its_prefix_parses(self, tmp_path):
        """What the read's EXTRA byte is actually for.

        Reading exactly the ceiling cannot tell "the file is that long" from "the file is
        longer", so the length check passes on a truncated prefix and the prefix gets parsed.
        Usually a cut document fails to parse and the outcome looks the same -- which is why
        asserting on a file at the limit does not discriminate. Here the first ``_MAX_FILE_BYTES``
        bytes are a COMPLETE, valid record and the file continues past them, so reading one byte
        further is the only thing that refuses it instead of adopting the prefix.
        """
        from kiro_crew.cloud.launch_state import _MAX_FILE_BYTES

        p = tmp_path / "cloud_launch_state.json"
        body = {"profile": "p", "region": "us-east-1", "last_tag": "kc-prefix"}
        pad = _MAX_FILE_BYTES - len(json.dumps({**body, "_pad": ""}))
        prefix = json.dumps({**body, "_pad": "x" * pad})
        assert len(prefix.encode()) == _MAX_FILE_BYTES
        assert json.loads(prefix)["last_tag"] == "kc-prefix", "the prefix must be a valid record"
        p.write_bytes(prefix.encode() + b'\n{"more": "bytes past the ceiling"}\n')

        assert LaunchState.load(p).last_tag == "", "an oversized file's prefix was adopted"


class TestResumeStillReattachesAfterTheMove:
    """The one thing moving the pointer must not break.

    An install that launched before this file existed has its tag in ``cloud.json``. If the
    read stopped looking there, every such install would answer "no previous launch" and the
    operator's running instance would look gone.
    """

    def test_a_legacy_pointer_is_still_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        # Exactly what an install that launched under the old code has on disk: the pointer in
        # cloud.json, and no launch record at all.
        (tmp_path / "cloud.json").write_text(
            json.dumps({"profile": "work", "region": "eu-west-1", "last_tag": "kc-legacy"})
        )
        assert not (tmp_path / "cloud_launch_state.json").exists()

        state = LaunchState.load()

        assert (state.profile, state.region, state.last_tag) == ("work", "eu-west-1", "kc-legacy")

    def test_the_fallback_writes_nothing(self, tmp_path, monkeypatch):
        """Read-through, not a migration.

        Migrating by writing would reintroduce the write this change removes -- and it would
        write on a READ, so any ``cloud`` subcommand would touch the operator's file.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        legacy = json.dumps({"profile": "work", "region": "eu-west-1", "last_tag": "kc-legacy"})
        (tmp_path / "cloud.json").write_text(legacy)

        LaunchState.load()
        LaunchState.load()

        assert (tmp_path / "cloud.json").read_text() == legacy
        assert sorted(q.name for q in tmp_path.iterdir()) == ["cloud.json"]

    def test_the_record_wins_over_the_legacy_fields(self, tmp_path, monkeypatch):
        """Once a launch has written here, this file is the answer.

        Otherwise a stale pointer in ``cloud.json`` -- which nothing clears now, because
        nothing writes it -- would outrank the tag of the launch that actually happened.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "cloud.json").write_text(json.dumps({"last_tag": "kc-stale-legacy"}))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-current")

        assert LaunchState.load().last_tag == "kc-current"

    def test_a_cleared_record_does_not_fall_back_to_the_legacy_tag(self, tmp_path, monkeypatch):
        """``destroy`` clearing the tag must STAY cleared.

        The fallback keys on the document being unusable, not on the tag being empty. Keying
        it on the tag would make a cleared pointer reappear from ``cloud.json`` and send the
        next command at a stack that was just deleted.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "cloud.json").write_text(json.dumps({"last_tag": "kc-legacy"}))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1")

        assert LaunchState.clear_tag("kc-1") is True

        assert LaunchState.load().last_tag == ""


class TestClearingThePointerIsOwnedByTheStackItNames:
    """``destroy`` owns the pointer for the stack it deleted, and for no other."""

    def test_a_matching_tag_is_cleared(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1")

        assert LaunchState.clear_tag("kc-1") is True
        assert LaunchState.load().last_tag == ""

    def test_a_tag_that_moved_on_is_left_alone(self, tmp_path, monkeypatch):
        """A launch that recorded its own tag between the delete and the clear keeps it.

        Cleared unconditionally, this command would wipe the pointer of a launch it never saw
        -- and the operator's new instance would look unlaunched.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-2-launched-since")

        assert LaunchState.clear_tag("kc-1-being-destroyed") is False
        assert LaunchState.load().last_tag == "kc-2-launched-since"

    def test_the_other_fields_survive_the_clear(self, tmp_path, monkeypatch):
        """Only the tag is this command's to clear: the profile and region still name where
        the operator's other stacks live, and ``cloud list`` needs them."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="work", region="eu-west-1", last_tag="kc-1")

        LaunchState.clear_tag("kc-1")

        state = LaunchState.load()
        assert (state.profile, state.region) == ("work", "eu-west-1")


class TestTheOperatorsFileIsNeverWritten:
    """The point of the whole move, asserted on the module rather than on one path."""

    def test_the_config_module_exposes_no_writer(self):
        """No ``save``, no ``apply_update``, no lock: there is nothing to call.

        A writer left in place with no caller is a writer the next change reaches for, and the
        two review findings this replaces were both about what that writer had to do when the
        operator's file was not in a state it could use.
        """
        import kiro_crew.cloud.config as config_mod

        assert not hasattr(CloudConfig, "save")
        assert not hasattr(CloudConfig, "apply_update")
        assert not hasattr(CloudConfig, "_replace_file")
        assert not hasattr(CloudConfig, "_merge_once")
        writers = [n for n in vars(config_mod) if "lock" in n.lower() or "writer" in n.lower()]
        assert writers == [], writers

    def test_nothing_in_the_module_can_write_a_file(self):
        """Pinned on the call expressions, not on a name search.

        ``"atomic_write" not in source`` passes for any spelling built at runtime. This asserts
        that no function in the module calls a writing primitive at all, which is the property
        that makes the operator's bytes safe without a seal.
        """
        import ast
        import inspect

        import kiro_crew.cloud.config as config_mod

        tree = ast.parse(inspect.getsource(config_mod))
        called = set()
        write_opens = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rendered = ast.unparse(node.func)
            called.add(rendered)
            if rendered.endswith("open"):
                # `open` is legitimate here -- the reader uses it -- so the mode decides. A
                # missing mode is "r", and any mode carrying w, a, x or + writes.
                mode = ast.unparse(node.args[1]) if len(node.args) > 1 else "'r'"
                if any(ch in mode for ch in "wax+"):
                    write_opens.append(ast.unparse(node))
        forbidden = {
            "atomic_write",
            "os.replace",
            "os.rename",
            "os.remove",
            "os.unlink",
            "p.write_text",
            "p.write_bytes",
            "path.write_text",
            "shutil.move",
        }
        assert not (called & forbidden), sorted(called & forbidden)
        assert write_opens == [], write_opens

    def test_a_launch_and_a_destroy_leave_the_config_byte_identical(self, tmp_path, monkeypatch):
        """End to end over the record's own API, including a hand-edited block.

        The block is deliberately one the old writer would have had to make a decision about:
        it is valid JSON the operator is still filling in, which is the state that forced the
        choice between overwriting it and refusing.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        config = tmp_path / "cloud.json"
        hand_written = json.dumps(
            {"fargate": {"cluster": "crews", "subnets": ["subnet-a"]}}, indent=4
        )
        config.write_text(hand_written)

        LaunchState.record(profile="work", region="eu-west-1", last_tag="kc-1")
        LaunchState.clear_tag("kc-1")

        assert config.read_text() == hand_written
        # And the block is still readable as the operator wrote it.
        assert CloudConfig.load().fargate == {"cluster": "crews", "subnets": ["subnet-a"]}

    def test_an_unreadable_config_does_not_stop_a_launch_being_recorded(
        self, tmp_path, monkeypatch
    ):
        """A malformed ``cloud.json`` cannot stop a launch being recorded.

        The record's write path does not read that file at all, so there is nothing for a
        hand-edit to fail on -- which is the property that keeps a billed deploy from ending
        before sign-in over a document the launch never needed."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        broken = b"{ this is hand-edited and unparseable"
        (tmp_path / "cloud.json").write_bytes(broken)

        LaunchState.record(profile="work", region="eu-west-1", last_tag="kc-spent")

        assert LaunchState.load().last_tag == "kc-spent"
        assert (tmp_path / "cloud.json").read_bytes() == broken

    @pytest.mark.parametrize(
        "path",
        ("~/.kiro/crew/cloud_launch_state.json", "~/.kirocrew/cloud_launch_state.json"),
    )
    def test_an_agent_may_not_write_the_record(self, path):
        """Its tag is what ``cloud destroy`` resolves without ``--tag``.

        So an agent that could write this file could choose which of the owner's stacks a
        ``destroy --yes`` deletes. Interactive ``destroy`` describes the instance and asks
        first; ``--yes`` is exactly the path that does not.
        """
        from kiro_crew.security.paths import is_sensitive_write_path

        assert is_sensitive_write_path(path) is True

    def test_the_record_is_sealed_at_the_os_layer_and_precreated(self):
        """All three layers ``cloud.json`` uses, for the reason the write gate alone is not
        enough: it covers an agent's file-edit tool, while a sandboxed shell's
        ``open(..., "w")`` reaches the path however the write is spelled, and pre-creation is
        what gives the mount seal a file to bind to when none exists yet."""
        from kiro_crew.sandbox import (
            _CREW_PRECREATE_READONLY_FILE_LEAVES,
            _CREW_READONLY_LEAVES,
        )

        assert "cloud_launch_state.json" in _CREW_READONLY_LEAVES
        assert "cloud_launch_state.json" in _CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_the_record_is_not_in_the_strict_no_alias_list(self):
        """Deliberately absent from that one list, which is not the same as unguarded.

        That list is walked where a SPAWN is prepared, so a leaf in it refuses every sandboxed
        spawn on a host whose files legitimately carry a second name -- the whole box for one
        command's exposure, and a regression a review already blocked once. This file's alias
        refusal lives at its own consume seam instead (``require_unaliased_launch_state``,
        called from ``LaunchState.load``), where the cost is the command that would have acted
        on a forged tag.
        """
        from kiro_crew.sandbox import _CREW_NOFOLLOW_READONLY_FILE_LEAVES

        assert "cloud_launch_state.json" not in _CREW_NOFOLLOW_READONLY_FILE_LEAVES

    def test_a_precreated_empty_stub_still_finds_the_legacy_pointer(self, tmp_path, monkeypatch):
        """The interaction pre-creation creates, and the reason the fallback is keyed on KEYS.

        The sandbox pre-creates ``{}`` so its seal has something to bind to. Read as a record,
        that stub would answer "no previous launch" for every install whose pointer still lives
        in ``cloud.json`` -- so ``cloud resume`` would break the first time an agent spawned.
        """
        import json as _json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "cloud.json").write_text(_json.dumps({"last_tag": "kc-legacy"}))
        (tmp_path / "cloud_launch_state.json").write_text("{}")

        assert LaunchState.load().last_tag == "kc-legacy"

    def test_a_cleared_record_is_still_not_a_stub(self, tmp_path, monkeypatch):
        """The other side of that key: ``destroy`` writes a record whose tag is ``""``, and it
        must NOT fall back -- otherwise the pointer to the stack it just deleted comes back."""
        import json as _json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "cloud.json").write_text(_json.dumps({"last_tag": "kc-legacy"}))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1")
        LaunchState.clear_tag("kc-1")

        assert LaunchState.load().last_tag == ""


class TestTheTwoProductWritersAreSerialised:
    """One lock, and only because ``clear_tag`` is a read-modify-write.

    Not the machinery this module replaced: that guarded the OPERATOR's file against a person
    in a text editor, a writer that takes no lock. Both writers here are Crew's own and both
    take this one.
    """

    def test_the_compare_and_the_write_happen_under_one_hold(self, tmp_path, monkeypatch):
        """A launch that records its tag between ``clear_tag``'s read and its write must not be
        overwritten. Forced deterministically: the record lands from inside the lock's own
        critical section, which is where an unlocked implementation leaves the window."""
        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1", path=p)

        real_read = ls._read_document
        fired = {"n": 0}

        def _racing_read(q, **kw):
            out = real_read(q, **kw)
            # Exactly once, and only for clear_tag's own read: a launch commits here.
            if fired["n"] == 0 and str(q) == str(p):
                fired["n"] = 1
                ls._write_record(p, profile="p", region="us-east-1", last_tag="kc-2-newer")
            return out

        monkeypatch.setattr(ls, "_read_document", _racing_read)
        LaunchState.clear_tag("kc-1", p)

        assert fired["n"] == 1, "the race was never forced, so this measures nothing"
        # Under one hold the clear writes what it compared; the newer tag is what survives on
        # disk only if the write is serialised against it.
        assert LaunchState.load(p).last_tag in ("", "kc-2-newer")

    def test_both_writers_take_the_same_lock(self):
        """Structural, over the two methods that WRITE: a writer skipping the lock makes the
        other's hold meaningless, and no behavioural test catches the one that does not take it.

        ``clear_tag`` is checked differently because it does not write: it delegates to
        ``try_clear_tag``, which is where the lock is. So the ratchet asserts it has no write of
        its own -- a second, unlocked read-modify-write added there is exactly the regression
        this test exists for, and a ``with _writer_lock`` check on it would now pass by accident
        of delegation.
        """
        import ast
        import inspect
        import textwrap

        import kiro_crew.cloud.launch_state as ls

        for method in (LaunchState.record, LaunchState.try_clear_tag):
            tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
            withs = [
                ast.unparse(item.context_expr)
                for node in ast.walk(tree)
                if isinstance(node, ast.With)
                for item in node.items
            ]
            assert any("_writer_lock" in w for w in withs), (method.__name__, withs)
        assert callable(ls._writer_lock)

        thin = textwrap.dedent(inspect.getsource(LaunchState.clear_tag))
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(thin))
            if isinstance(node, ast.Call)
        }
        assert "_write_record" not in calls, calls
        assert any("try_clear_tag" in c for c in calls), calls

    def test_a_lock_that_cannot_be_taken_raises_instead_of_writing_unlocked(
        self, tmp_path, monkeypatch
    ):
        """Fail closed. Yielding anyway put the read-modify-write back into three steps.

        The point of the lock is that ``clear_tag``'s compare and write cannot be split. A
        wrapper that logged and continued left exactly that split in place whenever the lock
        could not be taken, so a launch recording its tag in between had its pointer erased by
        a destroy that never saw it -- the failure the lock was added for.
        """
        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"

        def _boom(*a, **k):
            raise OSError("no locks on this filesystem")

        monkeypatch.setattr(ls.platform_compat, "file_lock", _boom)

        with pytest.raises(OSError):
            LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1", path=p)

    def test_the_writers_callers_absorb_that_failure(self, tmp_path, monkeypatch):
        """Why strict is safe HERE, measured rather than asserted.

        Strictness is only affordable because both wizard callers already answer `OSError`, and
        they answer it differently on purpose: after a confirmed deploy the launch continues
        with a warning, and before provisioning it aborts with nothing billed. So an unlockable
        mount costs a pointer, never a command. The spawn-path refusal this PR downgraded had no
        caller in that position, which is the difference rather than a preference.
        """
        import kiro_crew.cloud.launch_state as ls
        from kiro_crew.cloud import wizard

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        warnings: list[str] = []
        failures: list[str] = []
        monkeypatch.setattr(wizard.ui, "warn", lambda msg, *a, **k: warnings.append(str(msg)))
        monkeypatch.setattr(wizard.ui, "fail", lambda msg, *a, **k: failures.append(str(msg)))
        monkeypatch.setattr(wizard.ui, "detail", lambda *a, **k: None)

        def _boom(*a, **k):
            raise OSError("no locks on this filesystem")

        monkeypatch.setattr(ls.platform_compat, "file_lock", _boom)

        # After the deploy: warn, and let the launch finish.
        wizard._record_launch(profile="p", region="us-east-1", tag="kc-new")
        assert any("launch record" in w for w in warnings), warnings

        # Before provisioning: refuse, so nothing is created on a pointer that did not clear.
        assert wizard._clear_prior_pointer("kc-old") is False
        assert any("kc-old" in f for f in failures), failures

    def test_the_lock_is_taken_through_the_cross_platform_helper(self, tmp_path, monkeypatch):
        """``flock_compat.flock`` is a NO-OP on Windows, and Windows runs this command.

        That shim exists to keep the import graph loadable for the Windows cloud client, so on
        the one platform it does nothing, both of this file's writers are live: a destroy and a
        launch completing together let the clear overwrite the new tag. Observed by running the
        lock and watching which helper it calls, because the platform where the difference
        shows cannot be the platform this test runs on.
        """
        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"
        seen = []
        real = ls.platform_compat.file_lock

        def _watched(fd, **kw):
            seen.append((fd, kw))
            return real(fd, **kw)

        monkeypatch.setattr(ls.platform_compat, "file_lock", _watched)
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-1", path=p)

        assert len(seen) == 1, seen
        fd, kw = seen[0]
        assert kw.get("exclusive") is True, kw
        assert isinstance(fd, int)

    def test_the_lock_is_actually_held_during_the_critical_section(self, tmp_path, monkeypatch):
        """Not just taken: held, so the window the read-modify-write opens is really closed.

        Proved against the same file from a second file description -- a non-blocking acquire
        inside the hold must be refused. That is the exclusion itself rather than a call to
        something named like a lock.
        """
        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = tmp_path / "cloud_launch_state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_path = p.with_name(p.name + ".lock")

        with ls._writer_lock(p):
            with open(lock_path, "a+") as rival:
                with pytest.raises(OSError):
                    with ls.platform_compat.file_lock(rival.fileno(), exclusive=True, wait=False):
                        pass


class TestAnAliasedRecordIsRefusedWhereItsTagIsConsumed:
    """Three layers seal this file's PATH; a second name for its inode is outside all three.

    The write gate, the kernel read-only seal and pre-creation each name a path, so a symlink
    at the name or a second hardlink to the inode reaches it by a name none of them covers. The
    tag is what ``cloud destroy --yes`` deletes, so a forged one is an unrecoverable action on
    the wrong stack, and ``cloud launch`` re-attaching to it is the same substitution quieter.

    Every test here EXECUTES the read or the command. The three regressions this run all
    survived a test that inspected source text instead: the AST test on ``wrap_argv`` passed
    while the identical behaviour ran one function over, and a call-site grep passed while the
    call was wrapped. A property about what happens at runtime is only pinned by running it.
    """

    @staticmethod
    def _aliased_record(tmp_path, shape: str):
        """A record reachable under a second name, holding a tag the operator never launched."""
        import os

        real = tmp_path / "somewhere-else.json"
        real.write_text(
            json.dumps({"profile": "p", "region": "us-east-1", "last_tag": "kc-theirs"}),
            encoding="utf-8",
        )
        p = tmp_path / "cloud_launch_state.json"
        if shape == "symlink":
            p.symlink_to(real)
        else:
            os.link(real, p)
            assert p.stat().st_nlink > 1, "the fixture did not produce a second link"
        return p

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_the_read_that_every_verb_goes_through_refuses(self, tmp_path, monkeypatch, shape):
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_record(tmp_path, shape)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

    def test_a_lone_regular_record_still_reads(self, tmp_path, monkeypatch):
        """Positive control: the refusal is about the NAME, so the ordinary case is untouched.

        Without this, deleting the whole read would satisfy the two tests above.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")

        assert LaunchState.load().last_tag == "kc-mine"

    def test_an_alias_planted_after_the_by_name_check_is_still_refused(self, tmp_path, monkeypatch):
        """The window itself, forced: the check must answer about the inode that was READ.

        A by-name ``lstat`` followed by a separate ``open`` of the same name is a
        check-then-use: the judged inode and the consumed inode are two resolutions, so an
        alias appearing in between is judged by nobody and the record is adopted anyway.

        The fault is injected at the real seam and keyed on the BY-NAME call (``fd is None``),
        which is precisely the gap between the check and the read -- not on a call ordinal,
        which the fix itself changes. With the read pinned to one descriptor and re-checked on
        it, the second call sees the alias and refuses.
        """
        import os

        import kiro_crew.cloud.launch_state as ls
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")
        p = ls.state_path()
        alias = tmp_path / "planted-in-the-window.json"
        real_check = ls.require_unaliased_launch_state
        planted = {"n": 0}

        def _plant_in_the_window(path, *, fd=None):
            real_check(path, fd=fd)
            if fd is None and planted["n"] == 0:
                planted["n"] = 1
                os.link(p, alias)

        monkeypatch.setattr(ls, "require_unaliased_launch_state", _plant_in_the_window)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

        assert planted["n"] == 1, "the window was never forced, so this measures nothing"
        assert p.stat().st_nlink > 1, "the fixture did not produce a second link"

    def test_a_symlink_planted_in_the_window_refuses_instead_of_reading_as_absent(
        self, tmp_path, monkeypatch
    ):
        """The OTHER alias shape in the same window, which must not degrade to "no record".

        ``O_NOFOLLOW`` reports a symlinked leaf as ``ELOOP``, so a blanket
        ``except OSError: return None`` would turn the kernel's own alias refusal into the
        absent answer. Absent is not neutral on this path: it is what makes a pre-provision
        clear read as "nothing saved", which is the forged-empty result that lets the wizard
        provision where it was supposed to abort.

        Same window as the hardlink case and keyed the same way, on the BY-NAME call, because
        a symlink already present is refused by that call before the open is ever reached.

        Gated on the CAPABILITY, not on a platform name, because the capability is the whole
        mechanism: Windows defines no ``O_NOFOLLOW``, so the pinned open follows a link there
        and this shape is not refused at the open. That boundary is declared on
        ``_O_NOFOLLOW`` and in the PR body rather than left implicit, and what Windows retains
        is the by-name refusal for a link that is already present --
        ``test_the_read_that_every_verb_goes_through_refuses[symlink]``, which runs everywhere.
        """
        import os

        import kiro_crew.cloud.launch_state as ls
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        if not getattr(os, "O_NOFOLLOW", 0):
            pytest.skip(
                "the in-window symlink refusal IS O_NOFOLLOW; this platform has no such flag, "
                "and the pre-existing-symlink refusal is covered by "
                "test_the_read_that_every_verb_goes_through_refuses[symlink]"
            )

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")
        p = ls.state_path()
        elsewhere = tmp_path / "theirs.json"
        elsewhere.write_text(
            json.dumps({"profile": "p", "region": "us-east-1", "last_tag": "kc-theirs"}),
            encoding="utf-8",
        )
        real_check = ls.require_unaliased_launch_state
        planted = {"n": 0}

        def _plant_in_the_window(path, *, fd=None):
            real_check(path, fd=fd)
            if fd is None and planted["n"] == 0:
                planted["n"] = 1
                p.unlink()
                p.symlink_to(elsewhere)

        monkeypatch.setattr(ls, "require_unaliased_launch_state", _plant_in_the_window)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

        assert planted["n"] == 1, "the window was never forced, so this measures nothing"
        assert p.is_symlink(), "the fixture did not leave a symlink at the record's name"

    def test_a_pinned_read_that_fails_after_the_open_refuses_too(self, tmp_path, monkeypatch):
        """The READ side of the same rule, which a first version of this fix left fail-open.

        The open and the read were separate handlers and only the open had the rule, so an
        ``os.read`` failure still answered "no record" on the pinned path. It is reachable
        rather than theoretical: a directory swapped in at the name OPENS fine under
        ``O_RDONLY``, and the read then fails with ``EISDIR`` -- and a swap at that name is the
        aliasing race this function exists to defend. Both handlers now go through
        ``_refuse_or_absent``, so there is one rule and no second copy to forget.
        """
        import errno as _errno
        import os as _os

        import kiro_crew.cloud.launch_state as ls
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")
        p = ls.state_path()
        real_os_read = _os.read
        watched: set = set()
        real_os_open = _os.open
        fired = {"n": 0}

        def _watching_open(path, *a, **k):
            fd = real_os_open(path, *a, **k)
            if str(path) == str(p):
                watched.add(fd)
            return fd

        def _failing_read(fd, size, *a, **k):
            if fd in watched:
                fired["n"] += 1
                raise OSError(_errno.EISDIR, "simulated directory swapped in at the name")
            return real_os_read(fd, size, *a, **k)

        monkeypatch.setattr(_os, "open", _watching_open)
        monkeypatch.setattr(_os, "read", _failing_read)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

        assert fired["n"] >= 1, "the read never failed, so this measures nothing"

    def test_the_post_destroy_clear_still_degrades_quietly_on_an_unreadable_record(
        self, tmp_path, monkeypatch
    ):
        """The exempt caller keeps its no-raise contract, so the refusal is scoped not blanket.

        Without this, making every non-ENOENT open failure refuse would also make the clear that
        runs AFTER a stack is deleted raise, which is the failure shape this module exists to
        have removed: the irreversible work is done and there is nothing left to protect.

        The fault is ``ELOOP``, injected at the record's own ``os.open``. A real symlink LOOP
        produces exactly that errno on POSIX with or without ``O_NOFOLLOW`` (measured), and it
        is not ``ENOENT``, so it is the shape that reaches the branch under test. Injected
        rather than built on disk so the branch is measured on Windows too, where a loop is not
        the same fixture -- a platform skip here would score as a pass on a platform where the
        property was never checked.

        A plain symlinked record would NOT do: the unpinned open carries no ``O_NOFOLLOW``, so
        it follows the link and succeeds, the error branch never runs, and the test passes for a
        reason that has nothing to do with the guard. That is what an earlier version of this
        test did, and a mutation making the refusal blanket survived it.
        """
        import errno as _errno
        import os as _os

        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-gone")
        p = ls.state_path()
        real_os_open = _os.open
        fired = {"n": 0}

        def _looping_open(path, *a, **k):
            if str(path) == str(p):
                fired["n"] += 1
                raise OSError(_errno.ELOOP, "simulated symlink loop")
            return real_os_open(path, *a, **k)

        monkeypatch.setattr(_os, "open", _looping_open)

        # Declines by RETURNING rather than raising, which is the property under test.
        assert LaunchState.clear_tag("kc-gone", p) is False
        assert (
            fired["n"] >= 1
        ), "the fault never reached the record's open, so this measures nothing"

    def test_both_the_name_and_the_descriptor_are_checked(self, tmp_path, monkeypatch):
        """The allow direction, and proof the descriptor check is actually wired.

        Two assertions rather than one, because they fail for different reasons. The record
        still loading is what makes the pin a CONDITIONAL refusal instead of a blanket break.
        Seeing both a ``fd is None`` call and a ``fd is not None`` call is what stops this
        passing against an implementation that kept only the by-name check -- which is the
        state being fixed, and which reads as green on every other test in this class.
        """
        import kiro_crew.cloud.launch_state as ls

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-ok")
        real_check = ls.require_unaliased_launch_state
        by_descriptor: list = []

        def _watch(path, *, fd=None):
            by_descriptor.append(fd is not None)
            return real_check(path, fd=fd)

        monkeypatch.setattr(ls, "require_unaliased_launch_state", _watch)

        assert LaunchState.load().last_tag == "kc-ok"
        assert True in by_descriptor, "the descriptor check never ran, so the read is unpinned"
        assert False in by_descriptor, "the by-name check never ran, so the symlink shape is open"

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_the_pre_provision_clear_refuses_an_aliased_record(self, tmp_path, monkeypatch, shape):
        """The second consume point, which read the tag with no alias check at all.

        ``try_clear_tag`` reads the record and ``wizard._clear_prior_pointer`` provisions or
        aborts on what it reports, so the tag is an input to a security decision here exactly
        as it is in ``load``. The refusal is affordable because nothing has been provisioned
        and nothing is billing -- the same reason that caller's own abort is affordable.
        """
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = self._aliased_record(tmp_path, shape)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.try_clear_tag("kc-theirs", path=p)

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_the_wizard_does_not_provision_on_an_aliased_record(self, tmp_path, monkeypatch, shape):
        """What the refusal buys, at the caller whose answer decides the irreversible step.

        The hazard is not the raise, it is a forged pointer reading as "nothing saved": that
        turns a deliberate abort into a launch which leaves a live stack named by a pointer the
        operator never wrote, and a later ``destroy`` with no ``--tag`` resolves it. So the
        property pinned is that this caller does not answer True.
        """
        from kiro_crew.cloud import wizard
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_record(tmp_path, shape)

        with pytest.raises(SandboxCeilingUnsealable):
            wizard._clear_prior_pointer("kc-theirs")

    @pytest.mark.parametrize(("shape", "word"), (("symlink", "SYMLINK"), ("hardlink", "hardlink")))
    def test_the_refusal_names_the_file_the_shape_and_one_command(
        self, tmp_path, monkeypatch, shape, word
    ):
        """What the operator needs, because this is a HOST setup they chose and can undo.

        A dotfile manager or a hardlinking backup leaves exactly this shape, so the message
        has to say which file, which shape, and the single command that clears it -- not just
        that something was refused. The command is asserted in THIS platform's spelling: the
        first version of this test asserted a POSIX ``rm`` with POSIX quoting and reddened the
        Windows shard, because ``shlex.quote`` treats a backslash as unsafe and wrapped
        ``C:\\Users\\...`` in single quotes, which ``cmd`` does not use for quoting at all.
        """
        import shlex

        from kiro_crew import platform_compat
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        p = self._aliased_record(tmp_path, shape)

        with pytest.raises(SandboxCeilingUnsealable) as caught:
            LaunchState.load()

        message = str(caught.value)
        assert str(p) in message
        assert word in message
        assert "destroy" in message
        if platform_compat.IS_WINDOWS:  # pragma: no cover - asserted on the Windows shards
            assert f'del "{p}"' in message
        else:
            assert f"rm {shlex.quote(str(p))}" in message

    def test_the_remedy_is_the_platforms_own_command(self):
        """Both renderings from one host, which is the whole reason the platform is an argument.

        This is the fourth platform defect in this area and they all had one shape: a string
        written once, verified where it was written, asserted as general. A renderer that only
        reads ``IS_WINDOWS`` leaves its Windows branch measurable on Windows alone, which is how
        a `rm 'C:\\Users\\...'` remedy reached CI.
        """
        from kiro_crew.sandbox import _delete_file_command

        win = _delete_file_command(r"C:\Users\me\AppData\cloud_launch_state.json", windows=True)
        assert win == 'del "C:\\Users\\me\\AppData\\cloud_launch_state.json"'
        # Not POSIX-quoted: cmd does not treat ' as a quote, so a single-quoted path is passed
        # through literally and names a file that does not exist.
        assert "'" not in win
        assert not win.startswith("rm ")

        assert (
            _delete_file_command("/home/me/.kirocrew/x.json", windows=False)
            == "rm /home/me/.kirocrew/x.json"
        )
        # POSIX quoting is still applied where POSIX needs it.
        assert _delete_file_command("/home/a b/x.json", windows=False) == "rm '/home/a b/x.json'"

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_a_windows_operator_is_handed_a_windows_command(self, tmp_path, monkeypatch, shape):
        """The failing shard's own case, reproduced here: the MESSAGE, not just the renderer.

        The spelling is chosen when the refusal is built, so forcing the flag exercises the
        exact string a Windows operator reads. It is as close to that shard as this host gets --
        the path separators are still POSIX here -- and it is what turns "the renderer has a
        Windows branch" into "the refusal uses it".
        """
        import kiro_crew.sandbox as sandbox_mod
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr(sandbox_mod.platform_compat, "IS_WINDOWS", True)
        p = self._aliased_record(tmp_path, shape)

        with pytest.raises(SandboxCeilingUnsealable) as caught:
            LaunchState.load()

        message = str(caught.value)
        assert f'del "{p}"' in message
        assert "rm " not in message

    def test_an_explicit_path_is_the_path_that_gets_checked(self, tmp_path, monkeypatch):
        """Checked and consumed must be the same file, or the check answers about another one.

        The refusal takes the path being read rather than deriving it from the crew home. With
        a derived path, a caller reading one file would be cleared by the shape of a different
        one -- and under a test that sets ``KIROCREW_HOME`` the two coincide, so nothing would
        notice. Here the crew home holds a perfectly ordinary record and the file actually
        read is the aliased one.
        """
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        aliased = self._aliased_record(elsewhere, "symlink")

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load(aliased)

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_clearing_the_pointer_after_a_destroy_does_not_refuse(
        self, tmp_path, monkeypatch, shape
    ):
        """The one read that must never raise, because its command's damage is already done.

        ``clear_tag`` runs after the stack has been deleted. A refusal there would abort a
        command whose irreversible work is finished -- the same shape as the post-deploy write
        this module was created to remove -- so it uses the unguarded read. The guard still
        happened: it ran at the consume point that produced the tag being cleared.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_record(tmp_path, shape)

        assert LaunchState.clear_tag("kc-theirs") is True

    @pytest.mark.parametrize("shape", ("symlink", "hardlink"))
    def test_a_sandboxed_spawn_still_starts_with_an_aliased_record(
        self, tmp_path, monkeypatch, shape
    ):
        """The blast-radius half, and the regression a review already blocked once.

        The strict refusal must not reach the spawn path: this runs on every sandboxed spawn,
        so refusing there costs a stow / chezmoi / ``rsync --link-dest`` host every chat, cron
        and subagent for an exposure that is one command's. The warn stays a warn.
        """
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_record(tmp_path, shape)

        created = sandbox_mod._materialize_sealable_ceilings()

        assert isinstance(created, list)


class TestTheLegacyFallbackIsGuardedToo:
    """The same alias hole, through the OLD file, on the same command.

    The record's read refuses an aliased record -- and then falls through to `cloud.json` when
    the record holds nothing, which is the state the sandbox PRE-CREATES: an empty `{}` is the
    default on any install that has ever spawned an agent. So an aliased `cloud.json` plus that
    default reached `cloud destroy` with a forged tag and no alias check anywhere between the
    file and the deletion, because `cloud.json`'s own refusal sits on the LAUNCH seam
    (`defaults.engine_for`), which a teardown never runs.
    """

    @staticmethod
    def _aliased_config(tmp_path, tag: str = "kc-theirs"):
        real = tmp_path / "dotfiles-cloud.json"
        real.write_text(json.dumps({"last_tag": tag}), encoding="utf-8")
        target = tmp_path / "cloud.json"
        target.symlink_to(real)
        return target

    def test_an_aliased_config_is_refused_when_the_record_is_the_precreated_stub(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_config(tmp_path)
        # Exactly what the sandbox writes so its read-only seal has a file to bind to.
        (tmp_path / "cloud_launch_state.json").write_text("{}", encoding="utf-8")

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

    def test_an_aliased_config_is_refused_when_the_record_is_absent(self, tmp_path, monkeypatch):
        """The other way the fallback is reached, so the fix cannot depend on the stub existing."""
        from kiro_crew.sandbox import SandboxCeilingUnsealable

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_config(tmp_path)

        with pytest.raises(SandboxCeilingUnsealable):
            LaunchState.load()

    def test_a_lone_config_still_supplies_the_legacy_pointer(self, tmp_path, monkeypatch):
        """Positive control: refusing everything would satisfy both tests above and break resume."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "cloud.json").write_text(
            json.dumps({"last_tag": "kc-legacy"}), encoding="utf-8"
        )
        (tmp_path / "cloud_launch_state.json").write_text("{}", encoding="utf-8")

        assert LaunchState.load().last_tag == "kc-legacy"

    def test_a_real_record_does_not_consult_the_aliased_config_at_all(self, tmp_path, monkeypatch):
        """No fallback, no legacy read, so an aliased config cannot fail a command it has no part in.

        An install that has launched since the record existed keeps working even on a host whose
        `cloud.json` is managed by stow or chezmoi -- the refusal only fires where the file is
        actually the source of the tag.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_config(tmp_path)
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-mine")

        assert LaunchState.load().last_tag == "kc-mine"

    def test_the_post_destroy_clear_still_does_not_refuse(self, tmp_path, monkeypatch):
        """The exception that has to survive this fix.

        `clear_tag` runs after the stack is deleted, so it uses the unguarded read -- for the
        legacy fields as much as for the record. A refusal here would abort a command whose
        irreversible work is done, which is the shape this module exists to have removed.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        self._aliased_config(tmp_path, tag="kc-gone")

        assert LaunchState.clear_tag("kc-gone") is True


class TestADeclinedClearReportsWhatIsSaved:
    """The value the launch's decision is made on, measured against the real implementation.

    A decline has to say WHICH tag is saved, or its caller cannot tell "another launch owns this
    pointer" from "there is no pointer" -- and only the first makes provisioning unsafe. The
    wizard test for that decision stubs this method, so without these the reported tag itself was
    unpinned: a mutation returning `""` for a declined compare passed the whole suite.
    """

    def test_a_decline_reports_the_tag_that_is_there(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-b")

        assert LaunchState.try_clear_tag("kc-a") == (False, "kc-b")
        # A decline writes nothing: the other launch's pointer is left exactly as it was.
        assert LaunchState.load().last_tag == "kc-b"

    def test_a_match_clears_and_reports_no_tag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-b")

        assert LaunchState.try_clear_tag("kc-b") == (True, "")
        assert LaunchState.load().last_tag == ""

    def test_an_absent_pointer_declines_with_no_tag(self, tmp_path, monkeypatch):
        """The safe decline, at the level the value is produced rather than through the wizard."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

        assert LaunchState.try_clear_tag("kc-a") == (False, "")

    def test_the_thin_wrapper_still_answers_the_same_bool(self, tmp_path, monkeypatch):
        """`clear_tag`'s contract is unchanged, so its existing callers are unaffected."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        LaunchState.record(profile="p", region="us-east-1", last_tag="kc-b")

        assert LaunchState.clear_tag("kc-a") is False
        assert LaunchState.clear_tag("kc-b") is True

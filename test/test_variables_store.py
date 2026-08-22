"""Tests for the dedicated crew-variables store.

The store's central asymmetry is that READ is tolerant and WRITE is strict, and
both halves are pinned here. A suite that only proved tolerance would stay green
on a build that swallowed writes -- an empty read is the *correct* answer for a
broken store, so tolerance alone cannot tell a working store from an inert one.

Every case redirects ``config_path()`` at a tmp dir, so nothing touches the real
data home; ``store_path()`` is derived from it, which is exactly what the first
class asserts.
"""

from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from kiro_crew.config import loader as loader_mod
from kiro_crew.config import variables_store as vs
from kiro_crew.config.loader import ConfigReadError

_LOGGER = "kiro_crew.config.variables_store"

posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits; os.chmod does not model 0o600 on Windows",
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Redirect the config root at a tmp dir and hand back the store path."""
    cfg_dir = tmp_path / "crew-home"
    cfg_dir.mkdir()
    monkeypatch.setattr(loader_mod, "config_path", lambda: cfg_dir / "config.json", raising=True)
    # Derived from the implementation, never rebuilt here: the store's
    # location has moved once already and every hand-built path went stale.
    path = vs.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _doc(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestStorePath:
    """The location is DERIVED, not hardcoded, so a redirected root carries it."""

    def test_it_sits_in_its_own_directory_under_the_config_root(self, store):
        """A DIRECTORY, not a file beside config.json.

        The store is never written alone — a lock sidecar and an atomic-write temp
        inode land next to it — so it lives in a directory that security.py fences as
        a whole. Fencing only the target file would leave both writable.
        """
        assert vs.store_path() == store
        assert vs.store_path().parent.name == "variables"
        assert vs.store_path().parent.parent == loader_mod.config_path().parent
        assert vs.store_path().name == "variables.json"

    def test_a_second_redirect_moves_the_store_with_it(self, tmp_path, monkeypatch):
        first = tmp_path / "a"
        second = tmp_path / "b"
        monkeypatch.setattr(loader_mod, "config_path", lambda: first / "config.json")
        assert vs.store_path() == first / "variables" / "variables.json"
        monkeypatch.setattr(loader_mod, "config_path", lambda: second / "config.json")
        assert vs.store_path() == second / "variables" / "variables.json"

    def test_it_is_not_config_json(self, store):
        """The whole point of the file: a config write cannot reach these bytes."""
        assert vs.store_path() != loader_mod.config_path()


class TestReadIsTolerant:
    """Every failure resolves to ``{}``. A broken store must not break a boot."""

    def test_a_missing_file_is_empty_and_silent(self, store, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert caplog.text == "", "an absent store is the normal first-run state"

    def test_invalid_json_is_empty_and_warns(self, store, caplog):
        _write(store, "{not json at all")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert "unreadable" in caplog.text
        assert "variables.json" in caplog.text, "the warning must name the file to repair"

    def test_a_top_level_array_is_empty_and_warns(self, store, caplog):
        _write(store, '["global", "workspaces"]')
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert "not a JSON object" in caplog.text

    def test_a_top_level_scalar_is_empty(self, store, caplog):
        _write(store, '"just a string"')
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert "not a JSON object" in caplog.text

    def test_an_unreadable_path_is_empty_and_warns(self, store, caplog):
        # A directory where the file should be: read_text raises OSError, which is
        # the same class of failure as a permission denial or a bad mount.
        store.mkdir()
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert "unreadable" in caplog.text

    def test_invalid_utf8_is_empty_and_warns(self, store, caplog):
        # UnicodeDecodeError is a ValueError, not an OSError, so it needs its own
        # arm in the except clause or it escapes as a crash on boot.
        store.write_bytes(b'{"global": {"A": "\xff\xfe"}}')
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.read_store() == {}
        assert "unreadable" in caplog.text

    def test_a_valid_document_comes_back_verbatim(self, store):
        raw = {"global": {"A": "1"}, "workspaces": {"ops": {"B": "2"}}, "unknown": 3}
        _write(store, json.dumps(raw))
        assert vs.read_store() == raw, "an unknown key must survive a read/write cycle"


class TestGlobalValues:
    def test_an_invalid_pair_is_dropped_and_the_scope_survives(self, store, caplog):
        _write(store, json.dumps({"global": {"good": "v", "1bad": "x", "also": "w"}}))
        with caplog.at_level(logging.WARNING):
            assert vs.global_values() == {"good": "v", "also": "w"}
        assert "1bad" in caplog.text

    def test_a_reserved_name_is_dropped(self, store):
        _write(store, json.dumps({"global": {"MAX_SUBAGENTS": "9", "ok": "1"}}))
        assert vs.global_values() == {"ok": "1"}

    def test_scalars_are_coerced_like_a_hand_edit(self, store):
        _write(store, json.dumps({"global": {"n": 3, "b": True}}))
        assert vs.global_values() == {"n": "3", "b": "true"}

    def test_a_non_object_global_is_ignored(self, store):
        _write(store, json.dumps({"global": ["nope"]}))
        assert vs.global_values() == {}

    def test_a_missing_global_is_empty(self, store):
        _write(store, json.dumps({"workspaces": {"ops": {"A": "1"}}}))
        assert vs.global_values() == {}

    def test_a_passed_document_is_used_instead_of_the_file(self, store):
        _write(store, json.dumps({"global": {"FROM": "file"}}))
        assert vs.global_values({"global": {"FROM": "arg"}}) == {"FROM": "arg"}


class TestScopedValues:
    def _seed(self, store, doc):
        _write(store, json.dumps(doc))

    def test_each_named_map_is_cleaned_independently(self, store):
        self._seed(
            store,
            {"workspaces": {"ops": {"A": "1", "bad-name": "x"}, "dev": {"B": "2"}}},
        )
        assert vs.scoped_values(vs.SCOPE_WORKSPACE) == {"ops": {"A": "1"}, "dev": {"B": "2"}}

    def test_crew_scope_reads_the_crews_container(self, store):
        self._seed(store, {"crews": {"reviewer": {"A": "1"}}, "workspaces": {"ops": {"B": "2"}}})
        assert vs.scoped_values(vs.SCOPE_CREW) == {"reviewer": {"A": "1"}}
        assert vs.scoped_values(vs.SCOPE_WORKSPACE) == {"ops": {"B": "2"}}

    def test_a_non_object_container_is_ignored_and_warns(self, store, caplog):
        self._seed(store, {"workspaces": ["ops"]})
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.scoped_values(vs.SCOPE_WORKSPACE) == {}
        assert "workspaces" in caplog.text

    def test_an_absent_container_is_silent(self, store, caplog):
        self._seed(store, {"global": {"A": "1"}})
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            assert vs.scoped_values(vs.SCOPE_WORKSPACE) == {}
        assert caplog.text == ""

    def test_a_non_object_named_map_does_not_cost_its_siblings(self, store):
        self._seed(store, {"workspaces": {"broken": "oops", "ops": {"A": "1"}}})
        out = vs.scoped_values(vs.SCOPE_WORKSPACE)
        assert out["ops"] == {"A": "1"}
        assert out["broken"] == {}, "a broken entry resolves to no variables, not a raise"

    def test_a_non_string_name_is_skipped(self, store):
        # Unreachable through JSON, reachable through a hand-built document.
        doc = {"workspaces": {1: {"A": "1"}, "ops": {"B": "2"}}}
        assert vs.scoped_values(vs.SCOPE_WORKSPACE, doc) == {"ops": {"B": "2"}}


class TestPatchAppliesNamedKeysOnly:
    """Per-KEY semantics: an unnamed key is never read and never rewritten, so
    two writers touching different keys cannot lose each other's edits."""

    def test_set_and_delete_in_one_patch_leave_unnamed_keys_alone(self, store):
        _write(store, json.dumps({"global": {"A": "1", "B": "2", "KEEP": "mine"}}))
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "9"}, removals=["B"])
        assert _doc(store)["global"] == {"A": "9", "KEEP": "mine"}

    def test_deleting_an_absent_key_is_not_an_error(self, store):
        _write(store, json.dumps({"global": {"A": "1"}}))
        vs.patch_store(scope=vs.SCOPE_GLOBAL, removals=["nope"])
        assert _doc(store)["global"] == {"A": "1"}

    def test_a_workspace_patch_touches_only_that_name(self, store):
        _write(
            store,
            json.dumps({"workspaces": {"ops": {"A": "1"}, "dev": {"A": "keep"}}}),
        )
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"A": "2"})
        assert _doc(store)["workspaces"] == {"ops": {"A": "2"}, "dev": {"A": "keep"}}

    def test_other_scopes_are_untouched(self, store):
        _write(
            store,
            json.dumps(
                {
                    "global": {"G": "1"},
                    "workspaces": {"ops": {"W": "1"}},
                    "crews": {"reviewer": {"C": "1"}},
                }
            ),
        )
        vs.patch_store(scope=vs.SCOPE_CREW, name="reviewer", values={"C": "2"})
        doc = _doc(store)
        assert doc["global"] == {"G": "1"}
        assert doc["workspaces"] == {"ops": {"W": "1"}}
        assert doc["crews"] == {"reviewer": {"C": "2"}}

    def test_the_return_value_is_the_document_on_disk(self, store):
        result = vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert result == _doc(store)

    def test_an_unrelated_top_level_key_survives_a_write(self, store):
        _write(store, json.dumps({"global": {"A": "1"}, "future_key": {"x": 1}}))
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "2"})
        assert _doc(store)["future_key"] == {"x": 1}


class TestPatchCreatesAnAbsentContainer:
    """The legitimate first write. Absent is created; present-and-wrong is not."""

    def test_the_first_global_write_creates_the_file(self, store):
        assert not store.exists()
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert _doc(store) == {"global": {"A": "1"}}

    def test_the_first_workspace_write_creates_container_and_name(self, store):
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"A": "1"})
        assert _doc(store) == {"workspaces": {"ops": {"A": "1"}}}

    def test_the_first_crew_write_creates_the_crews_container(self, store):
        vs.patch_store(scope=vs.SCOPE_CREW, name="reviewer", values={"A": "1"})
        assert _doc(store) == {"crews": {"reviewer": {"A": "1"}}}

    def test_a_second_name_joins_an_existing_container(self, store):
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"A": "1"})
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="dev", values={"B": "2"})
        assert _doc(store)["workspaces"] == {"ops": {"A": "1"}, "dev": {"B": "2"}}

    def test_no_config_bookkeeping_keys_are_added(self, store):
        """stamp_meta=False: this is not a config document and must not grow one's
        keys, or the shape stops being exactly the three scope containers."""
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert set(_doc(store)) == {"global"}


class TestPatchRefusesAMalformedContainer:
    """A present-but-wrong container is the operator's only copy. Refuse it."""

    def _assert_bytes_survive(self, store, raw: str, **kwargs) -> vs.MalformedStore:
        _write(store, raw)
        before = store.read_bytes()
        with pytest.raises(vs.MalformedStore) as exc:
            vs.patch_store(values={"A": "1"}, **kwargs)
        assert store.read_bytes() == before, "the operator's value was not left intact"
        return exc.value

    def test_a_non_object_global_is_refused_with_its_path(self, store):
        err = self._assert_bytes_survive(
            store, json.dumps({"global": "oops"}), scope=vs.SCOPE_GLOBAL
        )
        assert err.path == "global"

    def test_a_non_object_workspaces_container_is_refused(self, store):
        err = self._assert_bytes_survive(
            store,
            json.dumps({"workspaces": ["ops"]}),
            scope=vs.SCOPE_WORKSPACE,
            name="ops",
        )
        assert err.path == "workspaces"

    def test_a_non_object_named_map_is_refused_with_a_dotted_path(self, store):
        err = self._assert_bytes_survive(
            store,
            json.dumps({"workspaces": {"ops": "oops"}}),
            scope=vs.SCOPE_WORKSPACE,
            name="ops",
        )
        assert err.path == "workspaces.ops", "the operator needs to be told what to repair"

    def test_a_non_object_crew_map_is_refused(self, store):
        err = self._assert_bytes_survive(
            store,
            json.dumps({"crews": {"reviewer": 7}}),
            scope=vs.SCOPE_CREW,
            name="reviewer",
        )
        assert err.path == "crews.reviewer"

    def test_a_malformed_sibling_does_not_block_an_unrelated_scope(self, store):
        """Refusal is scoped to the container the write would have to replace."""
        _write(store, json.dumps({"global": "oops", "workspaces": {}}))
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"A": "1"})
        doc = _doc(store)
        assert doc["global"] == "oops", "an unrelated broken value is preserved, not repaired"
        assert doc["workspaces"] == {"ops": {"A": "1"}}

    def test_a_corrupt_store_is_refused_rather_than_reset(self, store):
        """on_corrupt="fail": resetting to ``{}`` would delete every variable at
        every scope to service one patch."""
        _write(store, "{ truncated")
        before = store.read_bytes()
        with pytest.raises(ConfigReadError):
            vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert store.read_bytes() == before


class TestWriteIsStrictWhereReadIsTolerant:
    def test_the_same_document_reads_empty_but_refuses_a_write(self, store):
        """Both directions on one file, which is the property that matters: a
        build that silently ate writes would satisfy the tolerance half alone."""
        _write(store, json.dumps({"global": "not-a-map"}))

        assert vs.read_store() == {"global": "not-a-map"}
        assert vs.global_values() == {}, "read resolves to no variables"

        with pytest.raises(vs.MalformedStore):
            vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})

    def test_a_good_write_is_readable_through_the_scope_accessors(self, store):
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"B": "2"})
        vs.patch_store(scope=vs.SCOPE_CREW, name="reviewer", values={"C": "3"})

        assert vs.global_values() == {"A": "1"}
        assert vs.scoped_values(vs.SCOPE_WORKSPACE) == {"ops": {"B": "2"}}
        assert vs.scoped_values(vs.SCOPE_CREW) == {"reviewer": {"C": "3"}}


class TestPatchArgumentValidation:
    def test_an_unknown_scope_is_rejected(self, store):
        with pytest.raises(ValueError, match="unknown variables scope"):
            vs.patch_store(scope="session", values={"A": "1"})

    def test_a_workspace_scope_requires_a_name(self, store):
        with pytest.raises(ValueError, match="requires a name"):
            vs.patch_store(scope=vs.SCOPE_WORKSPACE, values={"A": "1"})

    def test_a_crew_scope_requires_a_name(self, store):
        with pytest.raises(ValueError, match="requires a name"):
            vs.patch_store(scope=vs.SCOPE_CREW, name="", values={"A": "1"})

    def test_a_rejected_call_writes_nothing(self, store):
        """Validation runs before the lock, so a bad call cannot create the file."""
        for kwargs in ({"scope": "nope"}, {"scope": vs.SCOPE_CREW}):
            with pytest.raises(ValueError):
                vs.patch_store(values={"A": "1"}, **kwargs)
        assert not store.exists()
        assert not (store.parent / "variables.json.lock").exists()

    def test_a_patch_with_neither_values_nor_removals_is_a_no_op_write(self, store):
        _write(store, json.dumps({"global": {"A": "1"}}))
        vs.patch_store(scope=vs.SCOPE_GLOBAL)
        assert _doc(store)["global"] == {"A": "1"}


class TestStoreFileMode:
    @posix_only
    def test_a_new_store_is_owner_only(self, store):
        """End state on a first write. Note this one does NOT pin the chmod --
        ``write_config_atomically`` already creates a new file 0o600 -- so the
        test below is the one that proves the store tightens the mode itself."""
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert stat.S_IMODE(store.stat().st_mode) == 0o600

    @posix_only
    def test_a_widened_existing_mode_is_tightened(self, store):
        """A tmp+rename preserves the EXISTING mode, so an operator (or a umask)
        that left the store 0o644 would keep it without the explicit chmod."""
        _write(store, json.dumps({"global": {"A": "1"}}))
        os.chmod(store, 0o644)
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "2"})
        assert stat.S_IMODE(store.stat().st_mode) == 0o600

    def test_a_filesystem_that_refuses_chmod_still_completes_the_write(self, store, monkeypatch):
        """Mode is defence in depth, not the boundary -- values are non-secret."""
        real_chmod = os.chmod

        def _refuse(path, mode, *a, **kw):
            if str(path) == str(store):
                raise OSError("read-only filesystem")
            return real_chmod(path, mode, *a, **kw)

        monkeypatch.setattr(vs.os, "chmod", _refuse)
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert _doc(store)["global"] == {"A": "1"}


class TestTheReadCache:
    """read_store() runs on every config load, and config loads run on the event
    loop, so it is cached on the file's signature.

    Both directions are asserted. A cache tested only for "it caches" passes on one
    that never invalidates, which turns a slow read into a silently stale one -- a
    strictly worse defect than the one caching fixed.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        vs.invalidate_cache()
        yield
        vs.invalidate_cache()

    def _counted_reads(self, monkeypatch) -> dict:
        """Count real reads of the store, so a cache HIT is observable."""
        calls = {"n": 0}
        original = vs._read_uncached

        def counting(path):
            calls["n"] += 1
            return original(path)

        monkeypatch.setattr(vs, "_read_uncached", counting)
        return calls

    def test_an_unchanged_file_is_read_once(self, store, monkeypatch):
        _write(store, json.dumps({"global": {"A": "1"}}))
        calls = self._counted_reads(monkeypatch)
        assert vs.read_store() == {"global": {"A": "1"}}
        assert vs.read_store() == {"global": {"A": "1"}}
        assert vs.read_store() == {"global": {"A": "1"}}
        assert calls["n"] == 1, f"cache miss: the file was read {calls['n']} times"

    def test_an_edit_busts_the_cache(self, store, monkeypatch):
        _write(store, json.dumps({"global": {"A": "1"}}))
        assert vs.global_values() == {"A": "1"}
        # A distinct size guarantees a different signature regardless of mtime
        # granularity, so this asserts cache-busting rather than clock resolution.
        _write(store, json.dumps({"global": {"A": "22222", "B": "3"}}))
        assert vs.global_values() == {"A": "22222", "B": "3"}

    def test_creating_the_file_busts_the_cache(self, store):
        """The absent-file sentinel must not pin an empty document forever."""
        assert vs.read_store() == {}
        _write(store, json.dumps({"global": {"A": "1"}}))
        assert vs.global_values() == {"A": "1"}

    def test_a_write_is_visible_to_the_next_read(self, store):
        """The invalidation half. An atomic replace can land inside the same mtime
        granularity as the read before it, so patch_store invalidates explicitly
        rather than trusting the signature to differ."""
        _write(store, json.dumps({"global": {"A": "1"}}))
        assert vs.global_values() == {"A": "1"}
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"B": "2"})
        assert vs.global_values() == {
            "A": "1",
            "B": "2",
        }, "a completed write was not visible to the next read"

    def test_a_returned_document_cannot_poison_the_cache(self, store):
        """Callers get a copy: _apply_variables_store hands these maps straight to
        config objects, and an alias would leak one session's edit to every reader."""
        _write(store, json.dumps({"global": {"A": "1"}}))
        first = vs.read_store()
        first["global"]["A"] = "mutated"
        first["global"]["INJECTED"] = "x"
        assert vs.read_store() == {"global": {"A": "1"}}

    def test_invalidation_does_not_rely_on_the_signature_changing(self, store, monkeypatch):
        """The DISTINCT property of the explicit invalidate_cache() call.

        Normally a write also changes the file's size or mtime, so the signature
        busts the cache on its own and removing the explicit invalidation looks
        harmless. It is not: an atomic replace can land within the same mtime
        granularity at the same size, and then the signature is identical and a
        reader would be served the pre-write document.

        Pinning the fingerprint to a constant models exactly that case and isolates
        the invalidation from the signature check that usually masks it.
        """
        _write(store, json.dumps({"global": {"A": "1"}}))
        monkeypatch.setattr(vs, "_fingerprint", lambda _path: ("frozen",))

        assert vs.global_values() == {"A": "1"}  # populates the cache
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"B": "2"})
        assert vs.global_values() == {"A": "1", "B": "2"}, (
            "with the signature frozen, only the explicit invalidation can make a "
            "completed write visible -- the cache served stale data"
        )


class TestTheStoreIsNotAgentWritable:
    """Values expand into OPERATOR-authored text, so an agent must not write them.

    Every value lands in the agent system prompt, a cron message, a monitor
    instruction or the composer. An agent that could write the store would be
    choosing text that arrives as instructions in its own prompt next turn, and in
    every scheduled turn after -- prompt injection with persistence.

    Note this was NOT fixed by moving the data: config.json is not write-protected
    either. It was made FIXABLE by moving it, because config.json cannot go behind
    the floor (a dozen handlers write it) while a dedicated store can.
    """

    def test_the_shell_gate_refuses_writes_to_the_store(self):
        from kiro_crew.security import is_sensitive_bash_command

        for form in (
            "echo x > ~/.kiro/crew/variables/variables.json",
            "tee ~/.kiro/crew/variables/variables.json",
            "cat ~/.kiro/crew/variables/variables.json",
            # The sidecars are the whole reason this is a DIRECTORY entry:
            # update_config_locked writes the lock and
            # write_config_atomically stages a temp inode beside the target.
            "echo x > ~/.kiro/crew/variables/variables.json.lock",
            "echo x > ~/.kiro/crew/variables/tmpab12cd.json",
        ):
            assert is_sensitive_bash_command(form), f"the gate allows: {form}"

    def test_the_tool_path_treats_it_as_sensitive(self):
        from kiro_crew.security import is_sensitive_path

        for probe in (
            "~/.kiro/crew/variables",
            "~/.kiro/crew/variables/variables.json",
            "~/.kiro/crew/variables/variables.json.lock",
            "~/.kiro/crew/variables/tmpab12cd.json",
        ):
            assert is_sensitive_path(probe), f"unfenced: {probe}"

    def test_the_endpoints_own_writer_still_works(self, store):
        """The floor must not break the Settings panel. patch_store opens the path
        directly rather than through the gate, like every other protected leaf."""
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"A": "1"})
        assert vs.global_values() == {"A": "1"}


class TestTheCachePublishRace:
    """A read already in flight when a write lands must not publish its stale document.

    Clearing the cache does nothing about a reader that is ABOUT TO assign to it. If
    such a reader publishes, its pre-write document lands under a signature that
    matches the post-write file, so every later reader is served stale values
    indefinitely -- not just once.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        vs.invalidate_cache()
        yield
        vs.invalidate_cache()

    def test_a_read_racing_a_write_does_not_publish(self, store, monkeypatch):
        _write(store, json.dumps({"global": {"A": "old"}}))
        original = vs._read_uncached

        def read_then_write(path):
            doc = original(path)
            # The write lands while this read is in flight.
            _write(store, json.dumps({"global": {"A": "new"}}))
            vs.invalidate_cache()
            return doc

        monkeypatch.setattr(vs, "_read_uncached", read_then_write)
        stale = vs.global_values()
        assert stale == {"A": "old"}, "the in-flight read should still return what it read"

        # The stale document must NOT have been cached: a fresh read sees the write.
        monkeypatch.setattr(vs, "_read_uncached", original)
        assert vs.global_values() == {"A": "new"}, "a stale document was published to the cache"

    def test_an_unraced_read_still_publishes(self, store, monkeypatch):
        """Positive control: the generation guard must not disable caching outright."""
        _write(store, json.dumps({"global": {"A": "1"}}))
        calls = {"n": 0}
        original = vs._read_uncached

        def counting(path):
            calls["n"] += 1
            return original(path)

        monkeypatch.setattr(vs, "_read_uncached", counting)
        vs.read_store()
        vs.read_store()
        assert calls["n"] == 1, "the generation guard broke ordinary caching"


class TestPresentNullIsNotAbsent:
    """``{"global": null}`` is present operator data, not a missing key.

    ``.get()`` returning None conflates the two, so an explicit null was treated as
    absent and overwritten -- the exact loss the malformed refusal exists to prevent.
    Checked at all three container levels, because the conflation was at all three.
    """

    @pytest.mark.parametrize(
        "doc,expected_path,body",
        [
            ({"global": None}, "global", {"scope": "global", "set": {"q": "1"}}),
            (
                {"workspaces": None},
                "workspaces",
                {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}},
            ),
            (
                {"workspaces": {"ops": None}},
                "workspaces.ops",
                {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}},
            ),
        ],
    )
    def test_a_present_null_container_is_refused(self, store, doc, expected_path, body):
        _write(store, json.dumps(doc))
        with pytest.raises(vs.MalformedStore) as excinfo:
            if body["scope"] == "global":
                vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"q": "1"})
            else:
                vs.patch_store(scope=vs.SCOPE_WORKSPACE, name="ops", values={"q": "1"})
        assert excinfo.value.path == expected_path
        # The operator's value survives byte-for-byte.
        assert json.loads(store.read_text(encoding="utf-8")) == doc

    def test_a_genuinely_missing_key_is_still_created(self, store):
        """The other direction. Refusing a missing key would break the first write."""
        _write(store, json.dumps({}))
        vs.patch_store(scope=vs.SCOPE_GLOBAL, values={"q": "1"})
        assert vs.global_values() == {"q": "1"}

"""Coverage tests for Spec Builder's backend route module.

``src/kiro_crew/apps/builtins/spec_builder/backend/routes.py`` had no test file at
all: every helper and all thirteen handlers were unexercised. This file covers it
at three levels.

  * the **pure predicates and coercions** -- ``_known_status``, ``_numeric``,
    ``_valid_name`` / ``_usable_name`` / ``_owns_slot_key``, ``_opted_in``,
    ``_client_identity_mismatch``, ``_normalize_spec_state``. Each one is the
    single place a bound, an allowlist or a fail-closed default lives, and a
    deleted bound is invisible without a test that names the refusal.
  * the **app-owned state files** -- settings, the spec index and the deletion
    tombstones. All three are read back as UNTRUSTED (an agent writes into them),
    so the shape guards are the behaviour under test: a list where an object was
    expected, an entry missing ``spec_dir``, a credential parked in an index KEY,
    a delete reservation abandoned by a dead process.
  * the **filesystem chokepoints** -- ``_safe_dir``, ``_spec_file``,
    ``_verified_spec_dir`` and the STOP-sentinel pair, which are what keep a
    symlink planted inside a spec directory from redirecting a read or a write.

Nothing here spawns a process or touches a network: ``_git`` is exercised with
``_prepare_git_spawn`` and ``create_subprocess_limited`` replaced, so neither the
OS sandbox nor a real ``git`` binary is required (a GitHub runner has neither the
sandbox backend nor this desk's paths). Every write lands under ``tmp_path`` or
the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from conftest import requires_symlinks
from kiro_crew.apps.builtins.spec_builder.backend import routes as r

# A name that satisfies _NAME_RE yet does NOT survive _redact -- the exact shape
# _usable_name exists to reject (a description can slugify into one).
CRED_NAME = "ghp_" + "a" * 36


@pytest.fixture(autouse=True)
def _pin_state_dir(tmp_path: Path):
    """Point the app's three state files at tmp_path, and clear module globals.

    ``_SLOT_KEYS`` is process-global and rebuilt from every index read, so a key
    left behind by one test would make ``_slot_key`` resolve another test's spec.
    """
    state = tmp_path / "app-state"
    saved = dict(r._SLOT_KEYS)
    with (
        mock.patch.object(r, "_STATE_DIR", state),
        mock.patch.object(r, "_INDEX_PATH", state / "index.json"),
        mock.patch.object(r, "_DELETED_PATH", state / "deleted.json"),
        mock.patch.object(r, "_SETTINGS_PATH", state / "settings.json"),
    ):
        r._SLOT_KEYS.clear()
        yield state
    r._SLOT_KEYS.clear()
    r._SLOT_KEYS.update(saved)


@pytest.fixture(autouse=True)
def _quiet_sel():
    """A stub SEL so audits are observable and ``_audit_tool`` reports success.

    Left as the real object, ``_audit_tool(critical=True)`` would do a synchronous
    HMAC log write on every ``_git`` call; set to ``None`` it would return False
    and make ``_git`` fail closed in every test. A stub keeps both honest.
    """
    log = mock.MagicMock()
    with mock.patch.object(r, "sel", lambda: log):
        yield log


def _write_index(entries: dict) -> None:
    r._index_path().parent.mkdir(parents=True, exist_ok=True)
    r._index_path().write_text(json.dumps(entries))


def _entry(spec_dir: Path, **over) -> dict:
    base = {
        "working_dir": str(spec_dir.parent),
        "spec_dir": str(spec_dir),
        "spec_type": "feature",
        "status": "planning",
        "slot_key": "spec-builder-demo-0123abcd",
        "created_at": 100.0,
        "updated_at": 200.0,
    }
    base.update(over)
    return base


# -- pure predicates and coercions -------------------------------------------


class TestKnownStatus:
    """index.json is agent-writable, so the stored status is untrusted: an
    unrecognised one is REPORTED as planning rather than echoed back."""

    @pytest.mark.parametrize("value", ["planning", "executing"])
    def test_recognised_statuses_pass_through(self, value):
        assert r._known_status(value) == value

    @pytest.mark.parametrize("value", ["", None, 0, "EXECUTING", "deleted", ["executing"]])
    def test_anything_else_reads_as_planning(self, value):
        assert r._known_status(value) == "planning"


class TestNumeric:
    def test_numbers_survive(self):
        assert r._numeric(12.5) == 12.5
        assert r._numeric("7") == 7.0

    @pytest.mark.parametrize("value", [None, "later", [], {}, object()])
    def test_non_numbers_become_zero(self, value):
        assert r._numeric(value) == 0.0

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_infinities_become_zero(self, value):
        # float() accepts these and json.dumps writes them as bare NaN/Infinity,
        # which is not JSON -- one poisoned timestamp would break the whole list.
        assert r._numeric(value) == 0.0
        json.dumps({"t": r._numeric(value)})


class TestNames:
    @pytest.mark.parametrize("name", ["a", "demo", "my-spec_1", "A" * 64])
    def test_grammar_accepts(self, name):
        assert r._valid_name(name)

    @pytest.mark.parametrize(
        "name", ["", "-lead", "_lead", "has space", "dot.name", "sl/ash", "..", "A" * 65]
    )
    def test_grammar_refuses(self, name):
        assert not r._valid_name(name)

    def test_a_credential_shaped_name_passes_the_grammar_but_is_not_usable(self):
        # _usable_name is the admission predicate _load_index applies, so a name
        # that _redact would rewrite must be refused BEFORE it is stored --
        # otherwise the next load drops the entry it was just written into.
        assert r._valid_name(CRED_NAME)
        assert r._redact(CRED_NAME) != CRED_NAME
        assert not r._usable_name(CRED_NAME)


class TestOwnsSlotKey:
    def test_the_legacy_name_derived_key_is_owned(self):
        assert r._owns_slot_key("demo", "spec-builder-demo")

    def test_a_per_creation_key_is_owned(self):
        assert r._owns_slot_key("demo", "spec-builder-demo-0123abcd")

    @pytest.mark.parametrize(
        "key",
        [
            "spec-builder-other-0123abcd",  # another spec's key
            "spec-builder-demo-0123abc",  # 7 hex, not 8
            "spec-builder-demo-0123ABCD",  # uppercase suffix
            "spec-builder-demo-zzzzzzzz",  # not hex
            "spec-builder-demo-",  # empty suffix
            "demo",  # no prefix
        ],
    )
    def test_foreign_or_malformed_keys_are_not_owned(self, key):
        assert not r._owns_slot_key("demo", key)

    def test_an_unusable_name_owns_nothing(self):
        assert not r._owns_slot_key("has space", "spec-builder-has space")


class TestSlotKeyResolution:
    def test_falls_back_to_the_name_derived_key(self):
        assert r._slot_key("demo") == "spec-builder-demo"

    def test_prefers_the_persisted_key(self):
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"
        assert r._slot_key("demo") == "spec-builder-demo-0123abcd"

    def test_a_persisted_key_that_fails_the_grammar_is_ignored(self):
        r._SLOT_KEYS["demo"] = "not-a-slot-key"
        assert r._slot_key("demo") == "spec-builder-demo"

    def test_new_slot_key_is_unique_and_owned(self):
        first, second = r._new_slot_key("demo"), r._new_slot_key("demo")
        assert first != second
        assert r._owns_slot_key("demo", first)


class TestRedact:
    def test_non_strings_and_empties_come_back_as_empty_strings(self):
        assert r._redact(None) == ""  # type: ignore[arg-type]
        assert r._redact("") == ""

    def test_a_credential_is_scrubbed(self):
        assert CRED_NAME not in r._redact(f"use {CRED_NAME} to auth")

    def test_it_fails_closed_when_the_security_module_is_missing(self):
        # Everything through _redact is agent- or user-authored on its way to the
        # browser, so with no way to scrub it the text must be WITHHELD, not served.
        with mock.patch.object(r, "_HAS_SECURITY", False):
            assert r._redact("plain text") == r._UNSCRUBBABLE


class TestOptedIn:
    def test_only_the_json_true_opts_in(self):
        assert r._opted_in({"use_worktree": True}, "use_worktree")

    @pytest.mark.parametrize("value", ["true", "false", 1, "1", [1], {"a": 1}, "0"])
    def test_truthy_non_booleans_do_not_opt_in(self, value):
        # Both flags this guards cause side effects a caller cannot undo by
        # retrying (a git worktree; adopting documents already on disk).
        assert not r._opted_in({"use_worktree": value}, "use_worktree")

    def test_a_missing_field_does_not_opt_in(self):
        assert not r._opted_in({}, "import_existing")


class TestClientIdentityMismatch:
    def test_an_unpinned_claim_never_mismatches(self):
        claim = r._ClientClaim("", "")
        assert not r._client_identity_mismatch(claim, "/spec", "spec-builder-demo-0123abcd")

    def test_a_different_directory_mismatches(self):
        claim = r._ClientClaim("/other", "")
        assert r._client_identity_mismatch(claim, "/spec", "spec-builder-demo-0123abcd")

    def test_a_different_slot_key_mismatches_even_on_the_same_directory(self):
        # Two specs CAN share a directory across a delete + re-import; they can
        # never share a per-creation slot key. That is what makes it decisive.
        claim = r._ClientClaim("/spec", "spec-builder-demo-ffffffff")
        assert r._client_identity_mismatch(claim, "/spec", "spec-builder-demo-0123abcd")

    def test_a_claimed_key_against_no_actual_key_is_not_compared(self):
        claim = r._ClientClaim("/spec", "spec-builder-demo-ffffffff")
        assert not r._client_identity_mismatch(claim, "/spec", "")


class TestNormalizeSpecState:
    """``.spec-state.json`` is LLM output, so every field is treated as hostile."""

    @pytest.mark.parametrize("raw", [None, [], "text", 3])
    def test_a_non_object_payload_is_rejected_outright(self, raw):
        assert r._normalize_spec_state(raw) is None

    def test_unknown_keys_are_dropped_and_the_schema_is_always_complete(self):
        out = r._normalize_spec_state({"surprise": "x"})
        assert out == {"decisions": [], "blocking": "", "context": {"template": ""}}

    def test_a_well_formed_decision_is_projected(self):
        out = r._normalize_spec_state(
            {
                "decisions": [
                    {
                        "id": "d1",
                        "title": "Which store?",
                        "options": ["A", "B", 7, ""],
                        "recommended": "A",
                        "answer": None,
                    }
                ],
                "blocking": "waiting on review",
                "context": {"template": "issue_radar"},
            }
        )
        assert out is not None
        assert out["decisions"] == [
            {
                "id": "d1",
                "title": "Which store?",
                # non-strings and empties dropped, not coerced
                "options": ["A", "B"],
                "recommended": "A",
                "answer": "",
            }
        ]
        assert out["blocking"] == "waiting on review"
        assert out["context"] == {"template": "issue_radar"}

    @pytest.mark.parametrize(
        "decisions",
        [
            [None],  # crashed SpecStatePanel before it was dropped here
            ["a string"],
            [{"title": ""}],  # no id and no title
            [{"id": "d1"}],  # no title
        ],
    )
    def test_malformed_decisions_are_dropped(self, decisions):
        out = r._normalize_spec_state({"decisions": decisions})
        assert out is not None and out["decisions"] == []

    def test_a_decision_without_an_id_falls_back_to_its_title(self):
        out = r._normalize_spec_state({"decisions": [{"title": "Which store?"}]})
        assert out is not None and out["decisions"][0]["id"] == "Which store?"

    def test_lists_are_capped(self):
        out = r._normalize_spec_state(
            {
                "decisions": [
                    {"id": f"d{i}", "title": "t", "options": ["o"] * (r._MAX_OPTIONS + 5)}
                    for i in range(r._MAX_DECISIONS + 10)
                ]
            }
        )
        assert out is not None
        assert len(out["decisions"]) == r._MAX_DECISIONS
        assert len(out["decisions"][0]["options"]) == r._MAX_OPTIONS

    def test_fields_are_length_capped(self):
        out = r._normalize_spec_state({"blocking": "x" * (r._MAX_FIELD + 50)})
        assert out is not None and len(out["blocking"]) == r._MAX_FIELD

    def test_a_non_object_context_yields_an_empty_template(self):
        out = r._normalize_spec_state({"context": "issue_radar"})
        assert out is not None and out["context"] == {"template": ""}

    def test_a_credential_in_a_value_is_scrubbed(self):
        out = r._normalize_spec_state({"blocking": f"waiting on {CRED_NAME}"})
        assert out is not None and CRED_NAME not in out["blocking"]


class TestCleanStr:
    def test_non_strings_become_empty(self):
        for value in (None, 5, [], {}):
            assert r._clean_str(value) == ""


# -- settings store -----------------------------------------------------------


class TestSettingsStore:
    def test_a_missing_file_reads_as_the_default(self):
        assert r._load_settings() == {"base_path": "", "model": ""}

    @pytest.mark.parametrize("text", ["not json", "[1, 2]", "null", '"a string"'])
    def test_a_malformed_or_wrongly_shaped_file_reads_as_the_default(self, text):
        r._settings_path().parent.mkdir(parents=True, exist_ok=True)
        r._settings_path().write_text(text)
        assert r._load_settings() == {"base_path": "", "model": ""}

    def test_a_non_string_base_path_is_normalized_at_the_read_chokepoint(self):
        # {"base_path": []} is a dict, so an outer-shape-only guard passed it and
        # every reader then called .strip() on a list -- a 500 on spec creation.
        r._settings_path().parent.mkdir(parents=True, exist_ok=True)
        r._settings_path().write_text(json.dumps({"base_path": [], "other": 1}))
        assert r._load_settings() == {"base_path": "", "other": 1, "model": ""}

    def test_save_then_load_round_trips_and_creates_the_state_dir(self, tmp_path):
        r._save_settings({"base_path": str(tmp_path)})
        assert r._load_settings()["base_path"] == str(tmp_path)


# -- deletion tombstones ------------------------------------------------------


class TestTombstones:
    def test_a_missing_file_is_an_empty_list(self):
        assert r._load_deleted() == []

    @pytest.mark.parametrize("text", ["not json", '{"a": 1}', "null"])
    def test_a_malformed_or_wrongly_shaped_file_is_an_empty_list(self, text):
        r._deleted_path().parent.mkdir(parents=True, exist_ok=True)
        r._deleted_path().write_text(text)
        assert r._load_deleted() == []

    def test_non_string_and_blank_entries_are_discarded(self):
        r._deleted_path().parent.mkdir(parents=True, exist_ok=True)
        r._deleted_path().write_text(json.dumps(["/a", 7, None, "   ", "/b"]))
        assert r._load_deleted() == ["/a", "/b"]

    def test_remembering_appends_and_dedupes_keeping_the_newest_position(self):
        r._remember_deleted("/a")
        r._remember_deleted("/b")
        r._remember_deleted("/a")
        assert r._load_deleted() == ["/b", "/a"]

    def test_an_empty_directory_is_not_remembered(self):
        r._remember_deleted("")
        assert not r._deleted_path().exists()

    def test_the_file_is_bounded_so_it_cannot_grow_without_limit(self):
        r._deleted_path().parent.mkdir(parents=True, exist_ok=True)
        r._deleted_path().write_text(json.dumps([f"/d{i}" for i in range(r._MAX_TOMBSTONES + 20)]))
        r._remember_deleted("/newest")
        current = r._load_deleted()
        assert len(current) == r._MAX_TOMBSTONES
        assert current[-1] == "/newest"

    def test_forgetting_removes_only_the_named_directory(self):
        r._remember_deleted("/a")
        r._remember_deleted("/b")
        r._forget_deleted("/a")
        assert r._load_deleted() == ["/b"]

    def test_forgetting_an_untombstoned_directory_writes_nothing(self):
        r._forget_deleted("/never-deleted")
        assert not r._deleted_path().exists()

    def test_forgetting_nothing_is_a_no_op(self):
        r._forget_deleted("")
        assert not r._deleted_path().exists()

    def test_a_failed_write_is_swallowed_because_the_delete_already_committed(self):
        with mock.patch.object(r, "atomic_write", side_effect=OSError("read-only")):
            r._remember_deleted("/a")  # must not raise
            r._deleted_path().parent.mkdir(parents=True, exist_ok=True)
            r._deleted_path().write_text(json.dumps(["/a"]))
            r._forget_deleted("/a")  # must not raise
        assert r._load_deleted() == ["/a"]


# -- index store --------------------------------------------------------------


class TestIndexStore:
    def test_a_missing_file_is_an_empty_index(self):
        assert r._load_index() == {}

    @pytest.mark.parametrize("text", ["not json", "[1]", "null"])
    def test_a_malformed_or_wrongly_shaped_file_is_an_empty_index(self, text):
        r._index_path().parent.mkdir(parents=True, exist_ok=True)
        r._index_path().write_text(text)
        assert r._load_index() == {}

    def test_an_entry_missing_spec_dir_is_dropped(self, tmp_path):
        # Handlers index meta["spec_dir"] directly, which is what turned a
        # shapeless entry into a 500 on the whole endpoint.
        _write_index(
            {
                "good": _entry(tmp_path / "good"),
                "empty": {},
                "blank": {"spec_dir": "   "},
                "wrong-type": {"spec_dir": 7},
                "not-an-object": "nope",
            }
        )
        assert list(r._load_index()) == ["good"]

    def test_an_entry_without_a_working_dir_still_lists(self, tmp_path):
        # working_dir is deliberately NOT required: it is re-validated at the slot
        # chokepoint, so such a spec lists and reads -- it just cannot be run.
        _write_index({"demo": {"spec_dir": str(tmp_path / "demo")}})
        assert list(r._load_index()) == ["demo"]

    def test_a_key_that_fails_the_grammar_is_dropped(self, tmp_path):
        _write_index({"has space": _entry(tmp_path / "a"), "ok": _entry(tmp_path / "b")})
        assert list(r._load_index()) == ["ok"]

    def test_a_credential_shaped_key_is_dropped_rather_than_scrubbed(self, tmp_path):
        # GET /specs returns the key as "name"; scrubbing it would produce a name
        # that no longer matches the directory the entry points at.
        _write_index({CRED_NAME: _entry(tmp_path / "a"), "ok": _entry(tmp_path / "b")})
        assert list(r._load_index()) == ["ok"]

    def test_reading_rebuilds_the_slot_key_map_only_for_owned_keys(self, tmp_path):
        _write_index(
            {
                "mine": _entry(tmp_path / "mine", slot_key="spec-builder-mine-0123abcd"),
                "stolen": _entry(tmp_path / "stolen", slot_key="spec-builder-mine-0123abcd"),
                "untyped": _entry(tmp_path / "untyped", slot_key=7),
            }
        )
        r._load_index()
        assert r._SLOT_KEYS == {"mine": "spec-builder-mine-0123abcd"}

    def test_saving_also_refreshes_the_slot_key_map(self, tmp_path):
        # Read-only refresh was not enough: a create commits through _mutate_index,
        # whose re-read rebuilt the map from the PRE-insert snapshot.
        r._save_index({"demo": _entry(tmp_path / "demo", slot_key="spec-builder-demo-abcdef01")})
        assert r._SLOT_KEYS["demo"] == "spec-builder-demo-abcdef01"

    def test_a_reservation_left_by_a_dead_process_is_released_on_read(self, tmp_path):
        _write_index(
            {"demo": _entry(tmp_path / "demo", deleting={"owner": "999:deadbeef", "at": 1.0})}
        )
        assert r._DELETING not in r._load_index()["demo"]

    def test_a_pre_existing_bare_timestamp_reservation_reads_as_foreign(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", deleting=1.0)})
        assert r._DELETING not in r._load_index()["demo"]

    def test_a_reservation_this_process_still_owns_is_left_alone(self, tmp_path):
        # This is what keeps an in-flight delete's own concurrent reads from
        # cancelling its reservation underneath it.
        _write_index(
            {"demo": _entry(tmp_path / "demo", deleting={"owner": r._PROCESS_ID, "at": 1.0})}
        )
        assert r._load_index()["demo"][r._DELETING]["owner"] == r._PROCESS_ID

    def test_reservation_is_ours_needs_a_mapping_with_our_owner(self):
        assert r._reservation_is_ours({r._DELETING: {"owner": r._PROCESS_ID}})
        assert not r._reservation_is_ours({r._DELETING: {"owner": "1:other"}})
        assert not r._reservation_is_ours({r._DELETING: 12.0})
        assert not r._reservation_is_ours({})

    def test_the_process_id_carries_more_than_a_reusable_pid(self):
        pid, _, unique = r._PROCESS_ID.partition(":")
        assert pid == str(os.getpid())
        assert len(unique) == 32


# -- path resolution ----------------------------------------------------------


class TestSafeDir:
    @pytest.mark.parametrize("raw", ["", "   "])
    def test_an_empty_value_is_unusable(self, raw):
        assert r._safe_dir(raw) is None

    def test_a_relative_path_is_refused_before_realpath_makes_it_absolute(self, tmp_path):
        # realpath resolves a relative value against the gateway's own cwd and
        # always returns an absolute path, so testing afterwards can never fail.
        assert r._safe_dir(".") is None
        assert r._safe_dir("relative/dir") is None

    def test_an_existing_directory_comes_back_fully_resolved(self, tmp_path):
        assert r._safe_dir(str(tmp_path)) == Path(os.path.realpath(tmp_path))

    def test_a_missing_directory_is_unusable_by_default(self, tmp_path):
        assert r._safe_dir(str(tmp_path / "nope")) is None

    def test_a_file_is_not_a_directory(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x")
        assert r._safe_dir(str(target)) is None

    def test_a_sensitive_location_is_refused(self, tmp_path):
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            assert r._safe_dir(str(tmp_path)) is None

    def test_must_exist_false_accepts_a_destination_the_app_will_create(self, tmp_path):
        dest = tmp_path / "a" / "b" / "c"
        assert r._safe_dir(str(dest), must_exist=False) == Path(os.path.realpath(dest))

    def test_must_exist_false_still_tests_the_nearest_existing_ancestor(self, tmp_path):
        dest = tmp_path / "not-yet"

        def _sensitive(path: str) -> bool:
            return path == os.path.realpath(tmp_path)

        with mock.patch.object(r, "is_sensitive_path", side_effect=_sensitive):
            # Naming a not-yet-created subdirectory of a credential directory must
            # not slip through on a stat miss.
            assert r._safe_dir(str(dest), must_exist=False) is None

    def test_safe_dir_optional_is_the_positional_only_form(self, tmp_path):
        dest = tmp_path / "later"
        assert r._safe_dir_optional(str(dest)) == r._safe_dir(str(dest), must_exist=False)

    @requires_symlinks
    def test_symlinks_are_resolved_before_the_sensitivity_test(self, tmp_path):
        secret = tmp_path / "secret"
        secret.mkdir()
        link = tmp_path / "benign"
        link.symlink_to(secret, target_is_directory=True)

        def _sensitive(path: str) -> bool:
            return path == os.path.realpath(secret)

        with mock.patch.object(r, "is_sensitive_path", side_effect=_sensitive):
            assert r._safe_dir(str(link)) is None


class TestContained:
    def test_a_directory_contains_itself_and_its_children(self, tmp_path):
        assert r._contained(tmp_path, tmp_path)
        assert r._contained(tmp_path / "a" / "b", tmp_path)

    def test_a_sibling_is_not_contained(self, tmp_path):
        assert not r._contained(tmp_path.parent / "elsewhere", tmp_path)


class TestResolveSpecDir:
    def test_the_default_is_the_kiro_standard_location(self, tmp_path):
        assert r._resolve_spec_dir(str(tmp_path), "demo") == (
            tmp_path / ".kiro" / "specs" / "demo"
        ).resolve()

    def test_an_absolute_base_path_override_stays_per_spec(self, tmp_path):
        r._save_settings({"base_path": str(tmp_path / "store")})
        assert r._resolve_spec_dir(str(tmp_path), "demo") == (tmp_path / "store" / "demo").resolve()


class TestScanSubdirs:
    def test_build_and_vcs_noise_and_hidden_entries_are_skipped(self, tmp_path):
        for name in ("src", "docs", "node_modules", "__pycache__", "venv", "env", ".git"):
            (tmp_path / name).mkdir()
        (tmp_path / "a-file").write_text("x")
        assert [d["name"] for d in r._scan_subdirs(str(tmp_path))] == ["docs", "src"]

    def test_the_listing_is_bounded(self, tmp_path):
        for i in range(r._BROWSE_MAX_DIRS + 5):
            (tmp_path / f"d{i:04d}").mkdir()
        assert len(r._scan_subdirs(str(tmp_path))) == r._BROWSE_MAX_DIRS

    def test_a_missing_base_scans_to_nothing_rather_than_raising(self, tmp_path):
        assert r._scan_subdirs(str(tmp_path / "gone")) == []

    def test_each_entry_carries_its_full_path(self, tmp_path):
        (tmp_path / "src").mkdir()
        assert r._scan_subdirs(str(tmp_path)) == [
            {"name": "src", "path": str(tmp_path / "src")}
        ]

    @requires_symlinks
    def test_a_link_pointing_at_a_sensitive_directory_is_not_listed(self, tmp_path):
        secret = tmp_path / "secret"
        secret.mkdir()
        (tmp_path / "looks-fine").symlink_to(secret, target_is_directory=True)
        (tmp_path / "src").mkdir()

        def _sensitive(path: str) -> bool:
            return path == os.path.realpath(secret)

        with mock.patch.object(r, "is_sensitive_path", side_effect=_sensitive):
            assert [d["name"] for d in r._scan_subdirs(str(tmp_path))] == ["src"]


# -- spec files ---------------------------------------------------------------


class TestSpecFile:
    def test_a_plain_file_inside_the_spec_dir_resolves(self, tmp_path):
        (tmp_path / "tasks.md").write_text("- [ ] one")
        assert r._spec_file(tmp_path, "tasks.md") == tmp_path / "tasks.md"

    def test_a_name_that_does_not_exist_yet_still_resolves(self, tmp_path):
        # The write path needs a resolvable target for a file it is about to create.
        assert r._spec_file(tmp_path, "STOP") == tmp_path / "STOP"

    @requires_symlinks
    def test_a_symlinked_spec_file_is_refused(self, tmp_path):
        # Outside the guarded root (which is `spec`, below) but still INSIDE this
        # test's own tmp_path, so the file dies with the test. tmp_path.parent is
        # the shared pytest run directory -- writing there leaks files across
        # tests and is how a scratch dir accumulates hundreds of thousands of
        # entries until /tmp runs out of inodes.
        outside = tmp_path / "credentials"
        outside.write_text("secret")
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / "requirements.md").symlink_to(outside)
        assert r._spec_file(spec, "requirements.md") is None

    def test_a_sensitive_target_is_refused(self, tmp_path):
        (tmp_path / "tasks.md").write_text("x")
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            assert r._spec_file(tmp_path, "tasks.md") is None

    def test_a_name_that_escapes_the_spec_dir_is_refused(self, tmp_path):
        spec = tmp_path / "spec"
        spec.mkdir()
        assert r._spec_file(spec, os.path.join("..", "outside.md")) is None


class TestReadSpecText:
    def test_a_present_file_is_read(self, tmp_path):
        (tmp_path / "tasks.md").write_text("- [x] done\n", newline="\n")
        assert r._read_spec_text(tmp_path, "tasks.md") == "- [x] done\n"

    def test_a_missing_file_is_none(self, tmp_path):
        assert r._read_spec_text(tmp_path, "tasks.md") is None

    def test_it_fails_closed_when_the_safe_reader_is_unavailable(self, tmp_path):
        (tmp_path / "tasks.md").write_text("x")
        with mock.patch.object(r, "safe_read_file_bytes_nolink", None):
            assert r._read_spec_text(tmp_path, "tasks.md") is None

    def test_a_raising_reader_fails_closed(self, tmp_path):
        with mock.patch.object(
            r, "safe_read_file_bytes_nolink", side_effect=RuntimeError("boom")
        ):
            assert r._read_spec_text(tmp_path, "tasks.md") is None

    def test_undecodable_bytes_are_replaced_rather_than_raising(self, tmp_path):
        (tmp_path / "tasks.md").write_bytes(b"ok \xff\xfe")
        text = r._read_spec_text(tmp_path, "tasks.md")
        assert text is not None and text.startswith("ok ")

    def test_the_read_is_capped_at_the_documented_size(self, tmp_path):
        reader = mock.Mock(return_value=b"x")
        with mock.patch.object(r, "safe_read_file_bytes_nolink", reader):
            r._read_spec_text(tmp_path, "tasks.md")
        assert reader.call_args.kwargs["max_bytes"] == r._MAX_SPEC_BYTES
        assert reader.call_args.kwargs["within_root"] == str(tmp_path)


class TestDerivePhase:
    def test_no_documents_is_the_new_phase(self, tmp_path):
        assert r._derive_phase(tmp_path) == "new"

    @pytest.mark.parametrize(
        "files,expected",
        [
            (["requirements.md"], "requirements"),
            (["requirements.md", "design.md"], "design"),
            (["requirements.md", "design.md", "tasks.md"], "tasks"),
            (["tasks.md"], "tasks"),
        ],
    )
    def test_the_furthest_document_written_names_the_phase(self, tmp_path, files, expected):
        for name in files:
            (tmp_path / name).write_text("x")
        assert r._derive_phase(tmp_path) == expected


class TestCollectSpecDocuments:
    def test_absent_documents_read_as_none_and_the_state_file_as_none(self, tmp_path):
        phase, files, state, meta = r._collect_spec_documents(tmp_path)
        assert phase == "new"
        assert files == {"tasks.md": None, "design.md": None, "requirements.md": None}
        assert state is None
        # An absent document carries no metadata at all: there is no hash to edit
        # against, so the editor has nothing to base a write on. And an unwritten
        # tasks.md is zero of zero rather than a missing progress reading.
        assert meta == {"docs": {}, "tasks": [], "task_progress": {"done": 0, "total": 0}}

    def test_documents_are_redacted_on_their_way_out(self, tmp_path):
        (tmp_path / "requirements.md").write_text(f"token is {CRED_NAME}")
        phase, files, _state, meta = r._collect_spec_documents(tmp_path)
        assert phase == "requirements"
        assert files["requirements.md"] is not None
        assert CRED_NAME not in files["requirements.md"]
        # Approval hashes always name the bytes as stored, while the rendered
        # document remains redacted.
        assert set(meta["docs"]["requirements.md"]) == {"hash"}

    def test_a_valid_state_file_is_normalized(self, tmp_path):
        (tmp_path / ".spec-state.json").write_text(
            json.dumps({"blocking": "review", "surprise": 1})
        )
        _phase, _files, state, _docs = r._collect_spec_documents(tmp_path)
        assert state == {"decisions": [], "blocking": "review", "context": {"template": ""}}

    def test_a_malformed_state_file_is_none_rather_than_an_error(self, tmp_path):
        (tmp_path / ".spec-state.json").write_text("{ not json")
        _phase, _files, state, _docs = r._collect_spec_documents(tmp_path)
        assert state is None

    def test_an_unredacted_document_carries_its_stored_hash(self, tmp_path):
        (tmp_path / "requirements.md").write_text("plain prose, nothing secret")
        _phase, _files, _state, meta = r._collect_spec_documents(tmp_path)
        doc = meta["docs"]["requirements.md"]
        # Approval still names the file AS STORED even though direct dashboard
        # writes are disabled: it records the exact revision the user reviewed.
        assert doc["hash"] == r._sha256_text("plain prose, nothing secret")
        assert set(doc) == {"hash"}


# -- the STOP sentinel --------------------------------------------------------


class TestVerifiedSpecDir:
    def test_a_directory_that_is_exactly_itself_verifies(self, tmp_path):
        assert r._verified_spec_dir(tmp_path) == tmp_path

    def test_a_relative_path_never_verifies(self):
        assert r._verified_spec_dir(Path("spec")) is None

    def test_a_missing_directory_does_not_verify(self, tmp_path):
        assert r._verified_spec_dir(tmp_path / "gone") is None

    def test_a_sensitive_directory_does_not_verify(self, tmp_path):
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            assert r._verified_spec_dir(tmp_path) is None

    @requires_symlinks
    def test_a_directory_replaced_by_a_symlink_does_not_verify(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "indexed"
        link.symlink_to(real, target_is_directory=True)
        # realpath disagrees with the path the index recorded -> REPLACED.
        assert r._verified_spec_dir(link) is None


class TestStopSentinel:
    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_writing_lands_the_sentinel_in_the_verified_directory(self, tmp_path):
        assert r._write_stop_sentinel(tmp_path) is True
        assert (tmp_path / r._STOP_FILE).is_file()
        # No temp file left behind.
        assert [p.name for p in tmp_path.iterdir()] == [r._STOP_FILE]

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    @requires_symlinks
    def test_a_planted_stop_symlink_is_destroyed_rather_than_written_through(self, tmp_path):
        # Outside `spec` but inside this test's tmp_path -- see the note on
        # _spec_file's symlink test about not writing to tmp_path.parent.
        elsewhere = tmp_path / "victim"
        elsewhere.write_text("original")
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / r._STOP_FILE).symlink_to(elsewhere)
        assert r._write_stop_sentinel(spec) is True
        assert not (spec / r._STOP_FILE).is_symlink()
        assert elsewhere.read_text() == "original"

    def test_writing_refuses_a_directory_that_does_not_verify(self, tmp_path):
        assert r._write_stop_sentinel(tmp_path / "gone") is False

    def test_without_pinning_the_write_fails_closed(self, tmp_path):
        # A path-based write can be redirected into ANOTHER spec by a directory
        # swapped underneath it, so no sentinel is better than the wrong one.
        with mock.patch.object(r, "_CAN_PIN_DIR", False):
            assert r._write_stop_sentinel(tmp_path) is False
        assert not (tmp_path / r._STOP_FILE).exists()

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_clearing_removes_our_own_stale_sentinel(self, tmp_path):
        (tmp_path / r._STOP_FILE).write_text("1")
        r._clear_stop_sentinel(tmp_path)
        assert not (tmp_path / r._STOP_FILE).exists()

    def test_clearing_an_absent_sentinel_is_a_no_op(self, tmp_path):
        r._clear_stop_sentinel(tmp_path)  # must not raise

    def test_clearing_refuses_a_directory_that_does_not_verify(self, tmp_path):
        marker = tmp_path / r._STOP_FILE
        marker.write_text("1")
        with mock.patch.object(r, "_verified_spec_dir", return_value=None):
            r._clear_stop_sentinel(tmp_path)
        assert marker.exists()

    def test_without_pinning_clearing_does_nothing(self, tmp_path):
        marker = tmp_path / r._STOP_FILE
        marker.write_text("1")
        with mock.patch.object(r, "_CAN_PIN_DIR", False):
            r._clear_stop_sentinel(tmp_path)
        assert marker.exists()

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_arming_clears_a_stale_sentinel_and_returns_its_path(self, tmp_path):
        (tmp_path / r._STOP_FILE).write_text("1")
        assert r._arm_stop_sentinel(tmp_path) == str(tmp_path / r._STOP_FILE)
        assert not (tmp_path / r._STOP_FILE).exists()

    def test_arming_returns_an_empty_string_when_the_directory_fails_verification(self, tmp_path):
        assert r._arm_stop_sentinel(tmp_path / "gone") == ""


class TestStopSentinelIdentityGate:
    def test_a_caller_with_no_identity_still_gets_the_plain_write(self, tmp_path):
        with mock.patch.object(r, "_write_stop_sentinel", return_value=True) as write:
            assert r._write_stop_sentinel_for_spec(tmp_path) is True
        write.assert_called_once_with(tmp_path)

    def test_a_matching_identity_is_written(self, tmp_path):
        key = "spec-builder-demo-0123abcd"
        _write_index({"demo": _entry(tmp_path, slot_key=key)})
        with mock.patch.object(r, "_write_stop_sentinel", return_value=True):
            assert r._write_stop_sentinel_for_spec(tmp_path, "demo", key) is True

    def test_a_replacement_creation_is_refused(self, tmp_path):
        # A same-name delete + re-import between the caller's check and this write
        # would otherwise halt a run the user has only just started.
        _write_index({"demo": _entry(tmp_path, slot_key="spec-builder-demo-ffffffff")})
        with mock.patch.object(r, "_write_stop_sentinel", return_value=True) as write:
            assert (
                r._write_stop_sentinel_for_spec(tmp_path, "demo", "spec-builder-demo-0123abcd")
                is False
            )
        write.assert_not_called()


class TestPrepareHandoff:
    def test_an_unverifiable_spec_dir_is_not_ready(self, tmp_path):
        assert r._prepare_handoff(tmp_path / "gone") == (False, "")

    def test_a_replacement_creation_is_refused_before_the_sentinel_is_touched(self, tmp_path):
        marker = tmp_path / r._STOP_FILE
        marker.write_text("1")
        _write_index({"demo": _entry(tmp_path, slot_key="spec-builder-demo-ffffffff")})
        assert r._prepare_handoff(tmp_path, "demo", "spec-builder-demo-0123abcd") == (False, "")
        # Arming is destructive -- it removes the STOP a Pause wrote.
        assert marker.exists()

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_missing_tasks_file_is_not_ready_but_still_arms(self, tmp_path):
        ready, sentinel = r._prepare_handoff(tmp_path)
        assert ready is False
        assert sentinel == str(tmp_path / r._STOP_FILE)

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_real_tasks_file_is_ready(self, tmp_path):
        (tmp_path / "tasks.md").write_text("- [ ] one")
        assert r._prepare_handoff(tmp_path) == (True, str(tmp_path / r._STOP_FILE))

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    @requires_symlinks
    def test_a_symlinked_tasks_file_does_not_satisfy_the_gate(self, tmp_path):
        # Outside `spec` but inside this test's tmp_path -- see the note on
        # _spec_file's symlink test about not writing to tmp_path.parent.
        outside = tmp_path / "other-tasks.md"
        outside.write_text("- [ ] not ours")
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / "tasks.md").symlink_to(outside)
        # is_file() FOLLOWS the link, so the gate used to pass and the autonomous
        # run then edited a file outside the spec directory.
        ready, _sentinel = r._prepare_handoff(spec)
        assert ready is False


# -- prompts ------------------------------------------------------------------


class TestSeedPrompt:
    def test_a_feature_spec_asks_for_all_three_documents(self, tmp_path):
        text = r._seed_prompt("feature", "demo", tmp_path, str(tmp_path.parent), "")
        for fname in ("requirements.md", "design.md", "tasks.md"):
            assert str(tmp_path / fname) in text
        assert "FEATURE spec" in text

    def test_a_quick_spec_deliberately_skips_design(self, tmp_path):
        text = r._seed_prompt("quick", "demo", tmp_path, str(tmp_path.parent), "")
        assert str(tmp_path / "design.md") not in text
        assert "Do NOT write design.md" in text

    def test_an_unknown_type_falls_back_to_the_feature_plan(self, tmp_path):
        text = r._seed_prompt("nonsense", "demo", tmp_path, str(tmp_path.parent), "")
        assert "FEATURE spec" in text
        assert str(tmp_path / "design.md") in text

    def test_the_description_is_appended_only_when_it_has_content(self, tmp_path):
        assert "initial description" not in r._seed_prompt(
            "bug", "demo", tmp_path, str(tmp_path.parent), "   "
        )
        assert "flaky on retry" in r._seed_prompt(
            "bug", "demo", tmp_path, str(tmp_path.parent), " flaky on retry "
        )

    def test_it_begins_with_the_first_deliverable_for_the_type(self, tmp_path):
        text = r._seed_prompt("quick", "demo", tmp_path, str(tmp_path.parent), "")
        assert "Begin with requirements.md" in text

    def test_the_state_file_is_named_as_plumbing_not_a_deliverable(self, tmp_path):
        text = r._seed_prompt("feature", "demo", tmp_path, str(tmp_path.parent), "")
        assert str(tmp_path / ".spec-state.json") in text


class TestExecPrompt:
    def test_it_names_the_tasks_file_and_the_working_dir(self, tmp_path):
        text = r._exec_prompt("demo", tmp_path, str(tmp_path.parent))
        assert str(tmp_path / "tasks.md") in text
        assert str(tmp_path.parent) in text
        assert "no cd needed" in text


# -- misc small helpers -------------------------------------------------------


class TestUnlinkQuietly:
    def test_it_removes_a_file(self, tmp_path):
        target = tmp_path / "env"
        target.write_text("x")
        r._unlink_quietly(str(target))
        assert not target.exists()

    def test_a_missing_file_and_an_oserror_are_both_swallowed(self, tmp_path):
        r._unlink_quietly(str(tmp_path / "gone"))
        # A directory raises on unlink; the helper must not propagate it.
        r._unlink_quietly(str(tmp_path))
        assert tmp_path.is_dir()


class TestDiscardQueuedWork:
    def test_all_three_relaunch_sources_are_dropped(self):
        slot = SimpleNamespace(
            _queue=["next"], _pending_steers=["steer"], _pending_synthesis=True
        )
        r._discard_queued_work(slot)
        assert slot._queue == []
        assert slot._pending_steers == []
        assert slot._pending_synthesis is False

    def test_a_slot_missing_those_attributes_is_tolerated(self):
        slot = SimpleNamespace()
        r._discard_queued_work(slot)  # failing to discard must not break teardown
        assert slot._pending_synthesis is False

    def test_a_sequence_that_refuses_to_clear_does_not_stop_the_rest(self):
        angry = mock.Mock()
        angry.clear.side_effect = RuntimeError("nope")
        slot = SimpleNamespace(_queue=angry, _pending_steers=["steer"])
        r._discard_queued_work(slot)
        assert slot._pending_steers == []


class TestAudit:
    def test_an_api_audit_reaches_sel(self, _quiet_sel):
        r._audit("spec_create", "demo")
        assert _quiet_sel.log_api_access.call_args.kwargs["caller"] == r.APP_NAME
        assert _quiet_sel.log_api_access.call_args.kwargs["operation"] == "spec_create"

    def test_a_raising_sel_never_breaks_the_request(self, _quiet_sel):
        _quiet_sel.log_api_access.side_effect = RuntimeError("log down")
        r._audit("spec_create", "demo")  # must not raise

    def test_no_sel_means_no_audit_and_no_error(self):
        with mock.patch.object(r, "sel", None):
            r._audit("spec_create", "demo")

    def test_a_tool_audit_records_the_subcommand_and_redacts_the_cwd(self, _quiet_sel):
        assert r._audit_tool("invoked", "worktree", f"/repo/{CRED_NAME}", critical=True) is True
        kwargs = _quiet_sel.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "git"
        assert kwargs["metadata"] == {"subcommand": "worktree"}
        assert kwargs["critical"] is True
        assert CRED_NAME not in kwargs["resources"]

    def test_a_tool_audit_carries_the_return_code_when_there_is_one(self, _quiet_sel):
        r._audit_tool("failure", "rev-parse", "/repo", rc=128)
        assert _quiet_sel.log_tool_invocation.call_args.kwargs["metadata"]["rc"] == 128

    def test_a_tool_audit_that_could_not_be_recorded_reports_false(self, _quiet_sel):
        # The "invoked" event is a PRECONDITION for spawning git, not a
        # nice-to-have: False here is what makes _git refuse to run.
        _quiet_sel.log_tool_invocation.side_effect = OSError("log unwritable")
        assert r._audit_tool("invoked", "worktree", "/repo", critical=True) is False

    def test_no_sel_means_a_tool_audit_cannot_be_recorded(self):
        with mock.patch.object(r, "sel", None):
            assert r._audit_tool("invoked", "worktree", "/repo", critical=True) is False


class TestModuleContracts:
    def test_the_state_files_default_under_the_data_home(self):
        # Resolved per call, never bound at import: config_dir() reads
        # KIROCREW_HOME every time, and freezing it breaks pod + test isolation.
        with (
            mock.patch.object(r, "_STATE_DIR", None),
            mock.patch.object(r, "_INDEX_PATH", None),
            mock.patch.object(r, "_DELETED_PATH", None),
            mock.patch.object(r, "_SETTINGS_PATH", None),
        ):
            state = r._state_dir()
            assert state.name == r.APP_NAME
            assert state.parent.name == "workspace"
            assert r._index_path() == state / "index.json"
            assert r._deleted_path() == state / "deleted.json"
            assert r._settings_path() == state / "settings.json"

    def test_the_autonomous_loop_and_the_arming_window_are_both_bounded(self):
        assert r._EXEC_MAX_CYCLES > 0
        assert isinstance(r._ARMING_GRACE_SECS, float) and r._ARMING_GRACE_SECS > 0

    def test_git_unavailable_is_distinct_from_gits_own_exit_codes(self):
        assert r._GIT_UNAVAILABLE == 127


class TestSecurityModuleUnavailable:
    """The module's import-time fallbacks, exercised by importing it for real
    with ``kiro_crew.security`` unimportable -- the only way those lines run."""

    @staticmethod
    def _load_without_security():
        import importlib.util

        saved = sys.modules.get("kiro_crew.security")
        # ``None`` in sys.modules makes the import statement raise ImportError,
        # which is exactly the condition the try/except in the module guards.
        sys.modules["kiro_crew.security"] = None  # type: ignore[assignment]
        try:
            spec = importlib.util.spec_from_file_location("_sb_nosec", r.__file__)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            if saved is not None:
                sys.modules["kiro_crew.security"] = saved
            else:  # pragma: no cover - security is always importable in prod
                del sys.modules["kiro_crew.security"]

    def test_every_path_reads_as_sensitive_when_it_cannot_be_judged(self, tmp_path):
        module = self._load_without_security()
        assert module._HAS_SECURITY is False
        # Fail CLOSED: with no way to make the judgement, treat every path as
        # sensitive rather than waving them all through.
        assert module.is_sensitive_path(str(tmp_path)) is True
        assert module._safe_dir(str(tmp_path)) is None

    def test_text_is_withheld_rather_than_served_unscrubbed(self):
        module = self._load_without_security()
        assert module._redact("plain text") == module._UNSCRUBBABLE


# -- index transactions (async) -----------------------------------------------


class TestIndexTransactions:
    @pytest.mark.asyncio
    async def test_aload_index_reads_off_the_event_loop(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert list(await r._aload_index()) == ["demo"]

    @pytest.mark.asyncio
    async def test_a_mutation_returning_true_commits(self, tmp_path):
        def _insert(index: dict) -> bool:
            index["demo"] = _entry(tmp_path / "demo")
            return True

        assert await r._mutate_index(_insert) is True
        assert list(r._load_index()) == ["demo"]

    @pytest.mark.asyncio
    async def test_a_mutation_returning_false_aborts_without_writing(self, tmp_path):
        def _abort(index: dict) -> bool:
            index["demo"] = _entry(tmp_path / "demo")
            return False

        assert await r._mutate_index(_abort) is False
        assert not r._index_path().exists()

    @pytest.mark.asyncio
    async def test_the_mutation_sees_a_freshly_read_index_not_the_callers_snapshot(self, tmp_path):
        _write_index({"first": _entry(tmp_path / "first")})
        seen: list[list[str]] = []

        def _look(index: dict) -> bool:
            seen.append(sorted(index))
            return False

        await r._mutate_index(_look)
        assert seen == [["first"]]


class TestTouchSpec:
    @pytest.mark.asyncio
    async def test_a_missing_spec_returns_none_so_the_caller_aborts(self):
        assert await r._touch_spec("gone", status="executing") is None

    @pytest.mark.asyncio
    async def test_fields_are_stamped_with_a_fresh_updated_at(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        fresh = await r._touch_spec("demo", status="executing")
        assert fresh is not None
        assert fresh["status"] == "executing"
        assert fresh["updated_at"] > 200.0
        assert r._load_index()["demo"]["status"] == "executing"

    @pytest.mark.asyncio
    async def test_an_entry_reserved_for_deletion_is_treated_as_already_gone(self, tmp_path):
        # A message landing mid-delete used to stamp the doomed entry and get a
        # non-None return, which every caller reads as "the spec is live".
        _write_index(
            {
                "demo": _entry(
                    tmp_path / "demo", deleting={"owner": r._PROCESS_ID, "at": time.time()}
                )
            }
        )
        assert await r._touch_spec("demo", status="executing") is None

    @pytest.mark.asyncio
    async def test_a_different_spec_dir_refuses(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert await r._touch_spec("demo", expect_spec_dir="/elsewhere") is None

    @pytest.mark.asyncio
    async def test_a_different_slot_key_refuses(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert await r._touch_spec("demo", expect_slot_key="spec-builder-demo-ffffffff") is None

    @pytest.mark.asyncio
    async def test_a_matching_pair_of_pins_is_accepted(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        fresh = await r._touch_spec(
            "demo",
            expect_spec_dir=str(tmp_path / "demo"),
            expect_slot_key="spec-builder-demo-0123abcd",
            status="executing",
        )
        assert fresh is not None and fresh["status"] == "executing"


class TestDeleteReservation:
    @pytest.mark.asyncio
    async def test_marking_reserves_the_name_and_stamps_our_ownership(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert await r._mark_deleting(
            "demo",
            expect_spec_dir=str(tmp_path / "demo"),
            expect_slot_key="spec-builder-demo-0123abcd",
        )
        held = json.loads(r._index_path().read_text())["demo"][r._DELETING]
        assert held["owner"] == r._PROCESS_ID

    @pytest.mark.asyncio
    async def test_marking_refuses_a_spec_that_moved(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert not await r._mark_deleting(
            "demo", expect_spec_dir="/elsewhere", expect_slot_key=""
        )
        assert not await r._mark_deleting(
            "demo",
            expect_spec_dir=str(tmp_path / "demo"),
            expect_slot_key="spec-builder-demo-ffffffff",
        )

    @pytest.mark.asyncio
    async def test_marking_a_missing_spec_refuses(self):
        assert not await r._mark_deleting("gone", expect_spec_dir="/x", expect_slot_key="")

    @pytest.mark.asyncio
    async def test_releasing_leaves_the_entry_exactly_as_it_was(self, tmp_path):
        entry = _entry(tmp_path / "demo")
        _write_index({"demo": dict(entry)})
        await r._mark_deleting(
            "demo", expect_spec_dir=str(tmp_path / "demo"), expect_slot_key=""
        )
        assert await r._unmark_deleting("demo", expect_spec_dir=str(tmp_path / "demo"))
        assert r._load_index()["demo"] == entry

    @pytest.mark.asyncio
    async def test_releasing_a_reservation_that_is_not_there_reports_false(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        assert not await r._unmark_deleting("demo", expect_spec_dir=str(tmp_path / "demo"))
        assert not await r._unmark_deleting("demo", expect_spec_dir="/elsewhere")


class TestClaimExecution:
    @pytest.mark.asyncio
    async def test_a_missing_spec_reports_gone(self):
        reason, entry = await r._claim_execution(
            "gone", expect_spec_dir="/x", expect_slot_key="", live_running=False
        )
        assert reason == r._CLAIM_GONE and entry == {}

    @pytest.mark.asyncio
    async def test_an_identity_that_moved_reports_gone(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        reason, _ = await r._claim_execution(
            "demo",
            expect_spec_dir=str(tmp_path / "demo"),
            expect_slot_key="spec-builder-demo-ffffffff",
            live_running=False,
        )
        assert reason == r._CLAIM_GONE

    @pytest.mark.asyncio
    async def test_planning_is_claimed_exactly_once(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        pins = {
            "expect_spec_dir": str(tmp_path / "demo"),
            "expect_slot_key": "spec-builder-demo-0123abcd",
        }
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            first, entry = await r._claim_execution("demo", live_running=False, **pins)
            second, _ = await r._claim_execution("demo", live_running=False, **pins)
        assert first == r._CLAIM_OK
        assert entry["status"] == "executing"
        # The pre-arm window is stamped so a concurrent poll cannot reconcile the
        # state away before the loop exists.
        assert entry["exec_arming_at"] > 0
        assert second == r._CLAIM_TAKEN

    @pytest.mark.asyncio
    async def test_a_live_running_slot_blocks_the_claim(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            reason, _ = await r._claim_execution(
                "demo",
                expect_spec_dir=str(tmp_path / "demo"),
                expect_slot_key="",
                live_running=True,
            )
        assert reason == r._CLAIM_TAKEN

    @pytest.mark.asyncio
    async def test_a_live_nudge_loop_blocks_the_claim(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_exec_loop_active", return_value=True):
            reason, _ = await r._claim_execution(
                "demo",
                expect_spec_dir=str(tmp_path / "demo"),
                expect_slot_key="",
                live_running=False,
            )
        assert reason == r._CLAIM_TAKEN


class TestEffectiveStatus:
    @pytest.mark.asyncio
    async def test_planning_passes_straight_through(self):
        assert await r._effective_status("demo", {"status": "planning"}, None) == "planning"

    @pytest.mark.asyncio
    async def test_an_unrecognised_stored_status_reads_as_planning(self):
        assert await r._effective_status("demo", {"status": "nonsense"}, None) == "planning"

    @pytest.mark.asyncio
    async def test_a_live_nudge_loop_means_still_executing(self):
        with mock.patch.object(r, "_exec_loop_active", return_value=True):
            assert await r._effective_status("demo", {"status": "executing"}, None) == "executing"

    @pytest.mark.asyncio
    async def test_a_running_turn_means_still_executing(self):
        slot = SimpleNamespace(running=True)
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", {"status": "executing"}, slot) == "executing"

    @pytest.mark.asyncio
    async def test_the_pre_arm_window_is_not_reconciled_away(self, tmp_path):
        # The handoff records "executing" BEFORE it arms the loop, so a poll
        # landing there legitimately sees no loop and no running turn.
        meta = _entry(tmp_path / "demo", status="executing", exec_arming_at=time.time())
        _write_index({"demo": dict(meta)})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", meta, None) == "executing"
        assert r._load_index()["demo"]["status"] == "executing"

    @pytest.mark.asyncio
    async def test_a_stale_arming_stamp_no_longer_masks_the_reconciliation(self, tmp_path):
        meta = _entry(
            tmp_path / "demo",
            status="executing",
            exec_arming_at=time.time() - r._ARMING_GRACE_SECS - 5,
        )
        _write_index({"demo": dict(meta)})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", meta, None) == "planning"

    @pytest.mark.asyncio
    async def test_an_unparseable_arming_stamp_does_not_grant_a_window(self, tmp_path):
        meta = _entry(tmp_path / "demo", status="executing", exec_arming_at="soon")
        _write_index({"demo": dict(meta)})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", meta, None) == "planning"

    @pytest.mark.asyncio
    async def test_a_finished_run_is_settled_back_to_planning_and_persisted(self, tmp_path):
        # A capped loop that ran out of cycles left "executing" in the index
        # forever: the UI offered Pause on a run that had already finished.
        meta = _entry(tmp_path / "demo", status="executing")
        _write_index({"demo": dict(meta)})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", meta, None) == "planning"
        assert r._load_index()["demo"]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_the_settle_is_identity_pinned_so_a_replacement_is_not_stamped(self, tmp_path):
        # Same name AND path, different creation: without the slot_key pin the
        # stamp lands on the replacement and hides Pause for its whole run.
        stale = _entry(tmp_path / "demo", status="executing")
        replacement = _entry(
            tmp_path / "demo", status="executing", slot_key="spec-builder-demo-ffffffff"
        )
        _write_index({"demo": replacement})
        with mock.patch.object(r, "_exec_loop_active", return_value=False):
            assert await r._effective_status("demo", stale, None) == "planning"
        assert r._load_index()["demo"]["status"] == "executing"


# -- the nudge loop -----------------------------------------------------------


def _autonudge(loop=None, *, svc_missing: bool = False):
    """Patch the autonudge accessor to a stub service holding ``loop``."""
    if svc_missing:
        return mock.patch.object(r, "_autonudge_instance", lambda: None)
    svc = mock.MagicMock()
    svc.get_by_slot.return_value = loop
    svc.remove = mock.AsyncMock()
    patcher = mock.patch.object(r, "_autonudge_instance", lambda: svc)
    patcher.svc = svc  # type: ignore[attr-defined]
    return patcher


class TestExecLoopLookup:
    def test_no_autonudge_module_means_no_loop_id(self):
        with mock.patch.object(r, "_autonudge_instance", None):
            assert r._exec_loop_id("demo") is None
            assert r._exec_loop_active("demo") is False

    def test_no_running_service_means_no_loop_id(self):
        with _autonudge(svc_missing=True):
            assert r._exec_loop_id("demo") is None
            assert r._exec_loop_active("demo") is False

    def test_no_loop_for_this_slot(self):
        with _autonudge(None):
            assert r._exec_loop_id("demo") is None
            assert r._exec_loop_active("demo") is False

    def test_a_live_loop_reports_its_id(self):
        with _autonudge(SimpleNamespace(id="loop-7", active=True)):
            assert r._exec_loop_id("demo") == "loop-7"
            assert r._exec_loop_active("demo") is True

    def test_a_loop_with_no_id_is_reported_as_none(self):
        with _autonudge(SimpleNamespace(id="", active=True)):
            assert r._exec_loop_id("demo") is None

    def test_a_deactivated_loop_is_not_active(self):
        # The loop is CAPPED, and the service deactivates it without telling this
        # app -- so the index's status cannot be trusted by itself.
        with _autonudge(SimpleNamespace(id="loop-7", active=False)):
            assert r._exec_loop_active("demo") is False

    def test_a_raising_lookup_is_swallowed(self):
        svc = mock.MagicMock()
        svc.get_by_slot.side_effect = RuntimeError("store down")
        with mock.patch.object(r, "_autonudge_instance", lambda: svc):
            assert r._exec_loop_id("demo") is None
            assert r._exec_loop_active("demo") is False


class TestRemoveNudgeLoop:
    @pytest.mark.asyncio
    async def test_a_pinned_caller_that_captured_nothing_removes_nothing(self):
        patcher = _autonudge(SimpleNamespace(id="loop-7"))
        with patcher:
            await r._remove_nudge_loop("demo", only_loop_id=None)
        patcher.svc.remove.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_an_unpinned_removal_takes_whatever_loop_is_there(self):
        patcher = _autonudge(SimpleNamespace(id="loop-7"))
        with patcher:
            await r._remove_nudge_loop("demo")
        patcher.svc.remove.assert_awaited_once_with("loop-7")  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_pin_that_does_not_match_the_live_loop_removes_nothing(self):
        # The lookup is by slot key, derived from the name, so an unpinned removal
        # on an abort path would cancel a same-name spec's loop.
        patcher = _autonudge(SimpleNamespace(id="loop-NEW"))
        with patcher:
            await r._remove_nudge_loop("demo", only_loop_id="loop-OLD")
        patcher.svc.remove.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_removal_failure_propagates_rather_than_reporting_success(self):
        patcher = _autonudge(SimpleNamespace(id="loop-7"))
        patcher.svc.remove.side_effect = OSError("store unwritable")  # type: ignore[attr-defined]
        with patcher, pytest.raises(OSError):
            await r._remove_nudge_loop("demo", only_loop_id="loop-7")

    @pytest.mark.asyncio
    async def test_no_service_is_a_no_op(self):
        with _autonudge(svc_missing=True):
            await r._remove_nudge_loop("demo")


# -- git ----------------------------------------------------------------------


class _FakeProc:
    """The slice of ``asyncio.subprocess.Process`` that ``_git`` touches."""

    def __init__(self, rc: int = 0, out: bytes = b"", err: bytes = b""):
        self.returncode: int | None = None
        self._rc, self._out, self._err = rc, out, err
        self.killed = False

    async def communicate(self):
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        self.returncode = -9
        return self.returncode


def _git_env(proc=None, *, cleanup: str = ""):
    """Replace the sandbox spawn seam so no subprocess and no real git is needed.

    ``sandboxed_spawn_argv`` probes the OS sandbox host and writes a scrubbed-env
    temp file; a GitHub runner has neither that backend nor a guarantee of git, so
    both this and ``create_subprocess_limited`` are injected.
    """
    spawn = mock.Mock(return_value=(["git", "--version"], {"PATH": "/usr/bin"}, cleanup))
    create = mock.AsyncMock(return_value=proc if proc is not None else _FakeProc())
    return spawn, create, mock.patch.multiple(
        r, _prepare_git_spawn=spawn, create_subprocess_limited=create
    )


class TestGit:
    @pytest.mark.asyncio
    async def test_a_successful_command_returns_trimmed_streams(self, tmp_path):
        proc = _FakeProc(0, b"  /repo/root\n", b"")
        _spawn, _create, patch = _git_env(proc)
        with patch:
            rc, out, err = await r._git(str(tmp_path), "rev-parse", "--show-toplevel")
        assert (rc, out, err) == (0, "/repo/root", "")

    @pytest.mark.asyncio
    async def test_a_nonzero_return_code_is_reported_not_raised(self, tmp_path, _quiet_sel):
        proc = _FakeProc(128, b"", b"fatal: not a git repository")
        _spawn, _create, patch = _git_env(proc)
        with patch:
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == 128 and "not a git repository" in err
        outcomes = [c.kwargs["outcome"] for c in _quiet_sel.log_tool_invocation.call_args_list]
        assert outcomes == ["invoked", "failure"]

    @pytest.mark.asyncio
    async def test_it_refuses_to_spawn_when_the_invocation_cannot_be_audited(self, tmp_path):
        # Audit-or-deny: a process this app runs on the user's repository must be
        # reconstructable from the audit log, so no record means no spawn.
        _spawn, create, patch = _git_env()
        with patch, mock.patch.object(r, "_audit_tool", return_value=False):
            rc, out, err = await r._git(str(tmp_path), "worktree", "add")
        assert rc == r._GIT_UNAVAILABLE
        assert out == "" and "audit unavailable" in err
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_sandbox_that_cannot_build_an_argv_degrades_instead_of_500ing(self, tmp_path):
        with mock.patch.object(r, "_prepare_git_spawn", side_effect=RuntimeError("no backend")):
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == r._GIT_UNAVAILABLE and "RuntimeError" in err

    @pytest.mark.asyncio
    async def test_no_git_on_the_host_degrades_instead_of_500ing(self, tmp_path):
        # Browsing a folder calls _repo_info, so letting this propagate turned the
        # project picker's first request into a 500 on a machine without git.
        _spawn, create, patch = _git_env()
        create.side_effect = FileNotFoundError("git")
        with patch:
            rc, _out, err = await r._git(str(tmp_path), "rev-parse")
        assert rc == r._GIT_UNAVAILABLE and err == "git is not installed"

    @pytest.mark.asyncio
    async def test_a_cancelled_spawn_kills_the_child_and_re_raises(self, tmp_path):
        proc = _FakeProc()
        _spawn, create, patch = _git_env(proc)
        create.side_effect = KeyboardInterrupt("cancelled")
        with patch, mock.patch.object(r, "_halt_git", mock.AsyncMock()) as halt:
            with pytest.raises(KeyboardInterrupt):
                await r._git(str(tmp_path), "worktree", "add")
        halt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_scrubbed_env_temp_file_is_always_removed(self, tmp_path):
        leftover = tmp_path / "scrubbed-env"
        leftover.write_text("x")
        _spawn, _create, patch = _git_env(cleanup=str(leftover))
        with patch:
            await r._git(str(tmp_path), "rev-parse")
        assert not leftover.exists()

    @pytest.mark.asyncio
    async def test_a_command_with_no_arguments_still_audits_a_blank_subcommand(
        self, tmp_path, _quiet_sel
    ):
        _spawn, _create, patch = _git_env()
        with patch:
            await r._git(str(tmp_path))
        assert _quiet_sel.log_tool_invocation.call_args.kwargs["metadata"]["subcommand"] == ""


class TestHaltGit:
    @pytest.mark.asyncio
    async def test_nothing_to_do_for_a_missing_or_finished_process(self):
        await r._halt_git(None, "worktree")
        done = _FakeProc()
        done.returncode = 0
        await r._halt_git(done, "worktree")
        assert done.killed is False

    @pytest.mark.asyncio
    async def test_a_live_process_is_killed_and_reaped(self):
        proc = _FakeProc()
        await r._halt_git(proc, "worktree")
        assert proc.killed is True
        assert proc.returncode == -9

    @pytest.mark.asyncio
    async def test_a_process_that_is_already_gone_needs_no_reap(self):
        proc = _FakeProc()
        proc.kill = mock.Mock(side_effect=ProcessLookupError())  # type: ignore[method-assign]
        proc.wait = mock.AsyncMock(side_effect=AssertionError("must not reap"))  # type: ignore
        await r._halt_git(proc, "worktree")

    @pytest.mark.asyncio
    async def test_a_process_stuck_after_the_kill_is_logged_rather_than_hung_on(self):
        proc = _FakeProc()

        async def _never():
            # asyncio.TimeoutError, NOT the builtin: `_halt_git` catches
            # `asyncio.TimeoutError`, and on Python 3.10 that is
            # concurrent.futures.TimeoutError -- a DIFFERENT class from
            # builtins.TimeoutError. The two only became the same class in 3.11,
            # so raising the builtin here passes on 3.11/3.12 and escapes the
            # handler uncaught on 3.10, which is exactly what CI caught.
            raise asyncio.TimeoutError

        proc.wait = _never  # type: ignore[method-assign]
        with mock.patch.object(r, "_GIT_HALT_SECS", 0.01):
            await r._halt_git(proc, "worktree")  # must return, not raise
        assert proc.killed is True


class TestRepoInfo:
    @pytest.mark.asyncio
    async def test_a_path_outside_a_repository(self):
        with mock.patch.object(r, "_git", mock.AsyncMock(return_value=(128, "", "fatal"))):
            assert await r._repo_info("/tmp/x") == {"is_git": False}

    @pytest.mark.asyncio
    async def test_a_repository_with_an_origin_main_base(self):
        calls = {
            ("rev-parse", "--show-toplevel"): (0, "/repo", ""),
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "feature/x", ""),
            ("rev-parse", "--verify", "--quiet", "origin/main"): (0, "sha", ""),
        }

        async def _fake(cwd, *args):
            return calls.get(tuple(args), (1, "", ""))

        with mock.patch.object(r, "_git", _fake):
            assert await r._repo_info("/repo") == {
                "is_git": True,
                "root": "/repo",
                "branch": "feature/x",
                "default_base": "origin/main",
            }

    @pytest.mark.asyncio
    async def test_with_no_known_remote_base_it_falls_back_to_the_current_branch(self):
        async def _fake(cwd, *args):
            if args[:2] == ("rev-parse", "--show-toplevel"):
                return 0, "/repo", ""
            if args == ("rev-parse", "--abbrev-ref", "HEAD"):
                return 0, "trunk", ""
            return 1, "", ""

        with mock.patch.object(r, "_git", _fake):
            info = await r._repo_info("/repo")
        assert info["default_base"] == "trunk"

    @pytest.mark.asyncio
    async def test_an_empty_toplevel_is_not_a_repository(self):
        with mock.patch.object(r, "_git", mock.AsyncMock(return_value=(0, "", ""))):
            assert await r._repo_info("/tmp/x") == {"is_git": False}


class TestWorktreeCreation:
    @pytest.mark.asyncio
    async def test_it_refuses_to_reuse_an_existing_path(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (tmp_path / "repo-wt-demo").mkdir()
        result = await r._create_worktree(str(root), "demo")
        assert isinstance(result, str) and "already exists" in result

    @pytest.mark.asyncio
    async def test_a_successful_creation_returns_the_sibling_path_and_branch(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "/repo", ""

        with (
            mock.patch.object(r, "_git", _fake),
            mock.patch.object(
                r, "_repo_info", mock.AsyncMock(return_value={"default_base": "origin/main"})
            ),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert result == (str(tmp_path / "repo-wt-demo"), "spec/demo")
        assert seen[-1] == (
            "worktree",
            "add",
            str(tmp_path / "repo-wt-demo"),
            "-b",
            "spec/demo",
            "origin/main",
        )

    @pytest.mark.asyncio
    async def test_a_failed_git_call_reports_its_last_line_redacted(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with (
            mock.patch.object(
                r, "_git", mock.AsyncMock(return_value=(128, "", f"noise\nfatal: {CRED_NAME}"))
            ),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert isinstance(result, str)
        assert result.startswith("fatal: ") and CRED_NAME not in result

    @pytest.mark.asyncio
    async def test_a_failure_with_no_stderr_still_reports_the_return_code(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with (
            mock.patch.object(r, "_git", mock.AsyncMock(return_value=(3, "", ""))),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
        ):
            result = await r._create_worktree(str(root), "demo")
        assert result == "git worktree add failed (rc=3)"


class TestWorktreeRemoval:
    @pytest.mark.asyncio
    async def test_it_prunes_before_deleting_the_branch(self):
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "", ""

        with mock.patch.object(r, "_git", _fake):
            await r._remove_worktree("/repo", "/repo-wt-demo", "spec/demo")
        # A leftover registration keeps the branch checked-out from git's view.
        assert seen == [
            ("worktree", "remove", "--force", "/repo-wt-demo"),
            ("worktree", "prune"),
            ("branch", "-D", "spec/demo"),
        ]

    @pytest.mark.asyncio
    async def test_without_a_branch_only_the_worktree_is_removed(self):
        seen: list[tuple] = []

        async def _fake(cwd, *args):
            seen.append(args)
            return 0, "", ""

        with mock.patch.object(r, "_git", _fake):
            await r._remove_worktree("/repo", "/repo-wt-demo")
        assert ("branch", "-D", "") not in seen
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_missing_arguments_make_it_a_no_op(self):
        git = mock.AsyncMock()
        with mock.patch.object(r, "_git", git):
            await r._remove_worktree("", "/wt")
            await r._remove_worktree("/repo", "")
        git.assert_not_awaited()


class TestRollbackWorktreeIfOurs:
    @pytest.mark.asyncio
    async def test_nothing_was_created_so_nothing_is_removed(self):
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo", was_ours=True, repo_root="/repo", created_worktree="",
                    worktree_branch="spec/demo",
                )
                is False
            )
        remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_entry_that_is_no_longer_ours_is_left_alone(self):
        # remove --force plus branch -D would discard the REPLACEMENT spec's
        # uncommitted work; an orphaned worktree is recoverable by hand.
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo", was_ours=False, repo_root="/repo",
                    created_worktree="/repo-wt-demo", worktree_branch="spec/demo",
                )
                is False
            )
        remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_our_own_worktree_is_removed(self):
        remove = mock.AsyncMock()
        with mock.patch.object(r, "_remove_worktree", remove):
            assert (
                await r._rollback_worktree_if_ours(
                    "demo", was_ours=True, repo_root="/repo",
                    created_worktree="/repo-wt-demo", worktree_branch="spec/demo",
                )
                is True
            )
        remove.assert_awaited_once_with("/repo", "/repo-wt-demo", "spec/demo")


# -- worker slots -------------------------------------------------------------


class _Slot:
    """The slice of the gateway's ``_ChatSlot`` this module touches."""

    def __init__(self, key: str, *, app: str | None = r.APP_NAME, running: bool = False):
        self.key = key
        self._app = app
        self.running = running
        self.project: str | None = None
        self.title = ""
        self._titled = False
        self.messages: list[dict] = []
        self.task = None
        self._queue: list[str] = []
        self._pending_steers: list[str] = []
        self._pending_synthesis = False
        self.queued: list[str] = []

    def queue_append(self, message: str) -> None:
        self._queue.append(message)

    def append(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content, "ts": ""})


class _State:
    """A stand-in dashboard state: a slot registry plus the hooks this app calls."""

    def __init__(self, **slots: _Slot):
        self._slots: dict[str, _Slot] = dict(slots)
        self.titles: list[tuple[str, str]] = []
        self.pushes = 0
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(stop_turn=mock.AsyncMock())
        self.conversation_log = None

    def get_slot(self, key: str):
        return self._slots.get(key)

    def get_or_create_slot(self, *, name: str, app: str | None = None):
        slot = self._slots.get(name)
        if slot is None:
            slot = _Slot(name, app=app)
            self._slots[name] = slot
        return slot

    def push_slot_title(self, key: str, title: str) -> None:
        self.titles.append((key, title))

    def push_slots_update(self) -> None:
        self.pushes += 1


def _no_rehydrate():
    return mock.patch.object(r, "rehydrate_slot_from_history_async", mock.AsyncMock(return_value=None))


class TestEnsureWorkerSlot:
    @pytest.mark.asyncio
    async def test_no_state_yields_no_slot(self, tmp_path):
        assert await r._ensure_worker_slot(None, "demo", _entry(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_a_name_that_fails_the_admission_predicate_is_refused(self, tmp_path, _quiet_sel):
        # The name becomes a slot key and then a history key, so an unbounded
        # value would flow into core's session-key parsing.
        state = _State()
        assert await r._ensure_worker_slot(state, CRED_NAME, _entry(tmp_path)) is None
        assert state._slots == {}
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_name_denied" in ops

    @pytest.mark.asyncio
    async def test_a_cold_slot_is_created_scoped_and_titled(self, tmp_path):
        state = _State()
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is not None
        assert slot._app == r.APP_NAME
        # cwd for the worker's CLI process; without it every tool pill is cd-noise.
        assert slot.project == str(Path(os.path.realpath(tmp_path)))
        assert slot.title == "Spec: demo"
        assert state.titles == [("spec-builder-demo", "Spec: demo")]

    @pytest.mark.asyncio
    async def test_the_persisted_transcript_is_pulled_back_before_a_slot_exists(self, tmp_path):
        state = _State()
        rehydrate = mock.AsyncMock(return_value=None)
        with mock.patch.object(r, "rehydrate_slot_from_history_async", rehydrate):
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        rehydrate.assert_awaited_once()
        assert rehydrate.await_args.kwargs["adopt_closed"] is True

    @pytest.mark.asyncio
    async def test_a_creating_caller_must_not_adopt_a_closed_transcript(self, tmp_path):
        # A delete leaves the old spec's archived transcript on disk under a
        # name-derived key, so a fresh spec must not be handed that conversation.
        state = _State()
        rehydrate = mock.AsyncMock(return_value=None)
        with mock.patch.object(r, "rehydrate_slot_from_history_async", rehydrate):
            await r._ensure_worker_slot(
                state, "demo", _entry(tmp_path / "spec"), adopt_closed=False
            )
        assert rehydrate.await_args.kwargs["adopt_closed"] is False

    @pytest.mark.asyncio
    async def test_a_restored_transcript_is_audited(self, tmp_path, _quiet_sel):
        state = _State()
        restored = _Slot("spec-builder-demo")
        with mock.patch.object(
            r, "rehydrate_slot_from_history_async", mock.AsyncMock(return_value=restored)
        ):
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_transcript_restored" in ops

    @pytest.mark.asyncio
    async def test_a_failing_restore_leaves_the_app_working(self, tmp_path):
        state = _State()
        with mock.patch.object(
            r,
            "rehydrate_slot_from_history_async",
            mock.AsyncMock(side_effect=RuntimeError("bad transcript")),
        ):
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is not None

    @pytest.mark.asyncio
    async def test_a_slot_owned_by_another_app_is_refused_not_taken_over(self, tmp_path):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")})
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None
        # Its ownership and project must be untouched.
        assert state._slots["spec-builder-demo"]._app == "issue-radar"
        assert state._slots["spec-builder-demo"].project is None

    @pytest.mark.asyncio
    async def test_an_unscoped_slot_under_our_key_is_somebody_elses_conversation(self, tmp_path):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app=None)})
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None

    @pytest.mark.asyncio
    async def test_our_own_existing_slot_is_adopted_and_rescoped(self, tmp_path):
        existing = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": existing})
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is existing and existing.project is not None

    @pytest.mark.asyncio
    async def test_an_already_titled_slot_keeps_its_title(self, tmp_path):
        existing = _Slot("spec-builder-demo")
        existing.title = "Renamed by the user"
        existing._titled = True
        state = _State(**{"spec-builder-demo": existing})
        with _no_rehydrate():
            await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert existing.title == "Renamed by the user"
        assert state.titles == []

    @pytest.mark.asyncio
    async def test_an_indexed_working_dir_that_no_longer_validates_refuses_the_slot(
        self, tmp_path, _quiet_sel
    ):
        state = _State()
        meta = _entry(tmp_path / "spec", working_dir=str(tmp_path / "deleted-project"))
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", meta) is None
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_working_dir_denied" in ops

    @pytest.mark.asyncio
    async def test_a_missing_working_dir_key_counts_as_unusable(self, tmp_path):
        # An unscoped slot is worse than a mis-scoped one: chat_runner passes
        # cwd=slot.project, so the worker would inherit the GATEWAY's cwd.
        state = _State()
        meta = {"spec_dir": str(tmp_path / "spec")}
        with _no_rehydrate():
            assert await r._ensure_worker_slot(state, "demo", meta) is None

    @pytest.mark.asyncio
    async def test_a_spec_replaced_while_the_slot_was_acquired_is_refused(self, tmp_path):
        state = _State()
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"

        async def _swap_identity(*_a, **_kw):
            r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
            return None

        with mock.patch.object(r, "rehydrate_slot_from_history_async", _swap_identity):
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_a_replacement_landing_after_the_working_dir_check_is_refused(self, tmp_path):
        state = _State()
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"
        real_safe_dir = r._safe_dir

        def _safe_then_swap(raw, **kw):
            r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
            return real_safe_dir(raw, **kw)

        with _no_rehydrate(), mock.patch.object(r, "_safe_dir", _safe_then_swap):
            assert await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec")) is None

    @pytest.mark.asyncio
    async def test_a_slot_that_rejects_scoping_is_still_returned(self, tmp_path):
        # Scoping failures are logged, not fatal -- a partially-built slot must not
        # take the whole request down.
        class _Frozen(_Slot):
            _ready = False

            def __setattr__(self, name, value):
                if name == "project" and self._ready:
                    raise RuntimeError("slot is frozen")
                super().__setattr__(name, value)

        frozen = _Frozen("spec-builder-demo")
        frozen._ready = True
        state = _State(**{"spec-builder-demo": frozen})
        with _no_rehydrate():
            slot = await r._ensure_worker_slot(state, "demo", _entry(tmp_path / "spec"))
        assert slot is frozen
        assert frozen.title == ""  # the scoping block aborted before titling


class TestSlotIdentityMoved:
    def test_an_unchanged_mapping_has_not_moved(self):
        r._SLOT_KEYS["demo"] = "spec-builder-demo-0123abcd"
        assert r._slot_identity_moved("demo", "spec-builder-demo-0123abcd") is False

    def test_a_rewritten_mapping_has_moved_and_is_audited(self, _quiet_sel):
        r._SLOT_KEYS["demo"] = "spec-builder-demo-ffffffff"
        assert r._slot_identity_moved("demo", "spec-builder-demo-0123abcd") is True
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_replaced_midflight" in ops


class TestTeardownWorkerSlot:
    @pytest.mark.asyncio
    async def test_no_state_is_reported_as_nothing_at_risk(self):
        assert await r._teardown_worker_slot(None, "demo") is True

    @pytest.mark.asyncio
    async def test_a_pinned_caller_that_captured_nothing_tears_nothing_down(self):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo")})
        assert await r._teardown_worker_slot(state, "demo", only_slot=None) is True
        assert "spec-builder-demo" in state._slots

    @pytest.mark.asyncio
    async def test_an_absent_slot_is_nothing_at_risk(self):
        assert await r._teardown_worker_slot(_State(), "demo") is True

    @pytest.mark.asyncio
    async def test_a_slot_replaced_since_capture_is_left_alone(self):
        live = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": live})
        captured = _Slot("spec-builder-demo")
        assert await r._teardown_worker_slot(state, "demo", only_slot=captured) is True
        assert state._slots["spec-builder-demo"] is live

    @pytest.mark.asyncio
    async def test_a_foreign_slot_is_never_deleted_by_name_collision(self):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")})
        assert await r._teardown_worker_slot(state, "demo") is True
        assert "spec-builder-demo" in state._slots

    @pytest.mark.asyncio
    async def test_a_captured_slot_with_a_malformed_key_falls_back_to_the_name(self):
        slot = _Slot("spec-builder-demo")
        slot.key = "not a slot key"  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
        ):
            assert await r._teardown_worker_slot(state, "demo", only_slot=slot) is True
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_the_slot_is_popped_queued_work_dropped_and_the_turn_cancelled(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot._queue = [{"id": "q1", "content": "next prompt"}]
        slot._pending_synthesis = True
        task = mock.Mock()
        task.cancel = mock.Mock()
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        with (
            mock.patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
            ) as save,
            mock.patch.object(asyncio, "wait_for", _cancelled),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True
        assert state._slots == {}
        # _run_chat's end-of-turn block would otherwise start the next prompt.
        assert slot._queue == [] and slot._pending_synthesis is False
        task.cancel.assert_called_once()
        assert save.await_args.kwargs["closed"] is True

    @pytest.mark.asyncio
    async def test_a_failed_archive_is_tolerated_when_it_was_not_required(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True
        assert state._slots == {}

    @pytest.mark.asyncio
    async def test_a_required_archive_that_fails_puts_the_slot_back(self, _quiet_sel):
        # The transcript is the user's data: reporting success would discard a
        # conversation that was never written.
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert (
                await r._teardown_worker_slot(state, "demo", require_archive=True) is False
            )
        assert state._slots["spec-builder-demo"] is slot
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_slot_archive_failed" in ops


class TestHaltActiveTurn:
    @pytest.mark.asyncio
    async def test_a_pinned_caller_that_captured_nothing_stops_nothing(self):
        assert await r._halt_active_turn(_State(), "demo", only_slot=None) is False

    @pytest.mark.asyncio
    async def test_no_slot_or_no_running_turn_means_nothing_to_stop(self):
        assert await r._halt_active_turn(_State(), "demo") is False
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", running=False)})
        assert await r._halt_active_turn(state, "demo") is False

    @pytest.mark.asyncio
    async def test_a_slot_replaced_since_capture_is_not_stopped(self):
        live = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": live})
        assert await r._halt_active_turn(state, "demo", only_slot=_Slot("x")) is False

    @pytest.mark.asyncio
    async def test_an_unscoped_slot_sharing_our_key_is_not_cancelled(self):
        # A plain POST /api/chat conversation that merely shares the key must not
        # lose its turn to this app's Pause button.
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app=None, running=True)})
        assert await r._halt_active_turn(state, "demo") is False

    @pytest.mark.asyncio
    async def test_the_turn_is_stopped_cooperatively_then_cancelled(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot._queue = [{"id": "q1", "content": "next"}]
        task = mock.Mock()
        task.done.return_value = False
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError

        with mock.patch.object(asyncio, "wait_for", _cancelled):
            assert await r._halt_active_turn(state, "demo") is True
        state.sessions.stop_turn.assert_awaited_once()
        assert state.sessions.stop_turn.await_args.kwargs["force"] is False
        task.cancel.assert_called_once()
        assert slot._queue == []
        # Pause must LEAVE the slot so the user can resume.
        assert state._slots["spec-builder-demo"] is slot

    @pytest.mark.asyncio
    async def test_a_failing_cooperative_stop_still_cancels(self):
        slot = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": slot})
        state.sessions.stop_turn.side_effect = RuntimeError("no session")
        assert await r._halt_active_turn(state, "demo") is True


class TestHaltExecution:
    @pytest.mark.asyncio
    async def test_it_sentinels_then_removes_the_loop_then_stops_the_turn(self, tmp_path):
        order: list[str] = []
        with (
            mock.patch.object(
                r,
                "_write_stop_sentinel_for_spec",
                lambda *a, **k: order.append("sentinel") or True,
            ),
            mock.patch.object(
                r,
                "_remove_nudge_loop",
                mock.AsyncMock(side_effect=lambda *a, **k: order.append("loop")),
            ),
            mock.patch.object(
                r,
                "_halt_active_turn",
                mock.AsyncMock(side_effect=lambda *a, **k: order.append("turn")),
            ),
        ):
            await r._halt_execution(_State(), "demo", tmp_path, reason="user stop")
        assert order == ["sentinel", "loop", "turn"]

    @pytest.mark.asyncio
    async def test_a_missing_sentinel_is_not_fatal_because_the_two_stops_are(self, tmp_path):
        with (
            mock.patch.object(r, "_write_stop_sentinel_for_spec", return_value=False),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()) as loop,
            mock.patch.object(r, "_halt_active_turn", mock.AsyncMock()) as turn,
        ):
            await r._halt_execution(_State(), "demo", tmp_path, reason="user stop")
        loop.assert_awaited_once()
        turn.assert_awaited_once()


# -- transcript relay ---------------------------------------------------------


class TestSerializeMessages:
    @pytest.mark.asyncio
    async def test_the_live_slot_wins_and_system_turns_are_hidden(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [
            {"role": "system", "content": "internal", "ts": "1"},
            {"role": "user", "content": "hello", "ts": "2"},
            {"role": "assistant", "content": "hi", "ts": "3"},
        ]
        state = _State(**{"spec-builder-demo": slot})
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "hello", "ts": "2"},
            {"role": "assistant", "content": "hi", "ts": "3"},
        ]

    @pytest.mark.asyncio
    async def test_a_tool_turn_is_compacted_to_its_first_line(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": "fs_write tasks.md\nmore\nlines", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert out == [{"role": "tool", "content": "fs_write tasks.md", "ts": "1"}]

    @pytest.mark.asyncio
    async def test_an_empty_tool_turn_does_not_raise_on_the_first_line(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "tool", "content": "", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "tool", "content": "", "ts": "1"}
        ]

    @pytest.mark.asyncio
    async def test_content_is_redacted_before_it_leaves_the_backend(self):
        slot = _Slot("spec-builder-demo")
        slot.messages = [{"role": "assistant", "content": f"use {CRED_NAME}", "ts": "1"}]
        state = _State(**{"spec-builder-demo": slot})
        out = await r._serialize_messages(state, "spec-builder-demo")
        assert CRED_NAME not in out[0]["content"]

    @pytest.mark.asyncio
    async def test_a_cold_slot_falls_back_to_the_persisted_transcript_off_loop(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(return_value=[{"role": "user", "content": "old", "ts": "9"}])
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "old", "ts": "9"}
        ]

    @pytest.mark.asyncio
    async def test_object_shaped_messages_are_read_by_attribute(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(
                return_value=[SimpleNamespace(role="user", content="old", ts="9")]
            )
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == [
            {"role": "user", "content": "old", "ts": "9"}
        ]

    @pytest.mark.asyncio
    async def test_a_failing_history_read_yields_an_empty_transcript(self):
        state = _State()
        state.conversation_log = SimpleNamespace(  # type: ignore[assignment]
            read_messages=mock.Mock(side_effect=OSError("gone"))
        )
        assert await r._serialize_messages(state, "spec-builder-demo") == []

    @pytest.mark.asyncio
    async def test_no_conversation_log_yields_an_empty_transcript(self):
        assert await r._serialize_messages(_State(), "spec-builder-demo") == []


class TestDispatchTurn:
    @pytest.mark.asyncio
    async def test_an_idle_slot_starts_a_turn(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        with mock.patch("kiro_crew.dashboard.chat_runner._run_chat", mock.AsyncMock()):
            r._dispatch_turn(state, slot, "do the thing")
            assert slot.messages == [{"role": "user", "content": "do the thing", "ts": ""}]
            assert slot.task is not None
            assert slot.task in state._background_tasks
            assert state.pushes == 1
            await slot.task

    @pytest.mark.asyncio
    async def test_the_turn_is_bounded_by_the_shared_chat_timeout(self):
        slot = _Slot("spec-builder-demo")
        state = _State(**{"spec-builder-demo": slot})
        seen: dict = {}

        def _capture(coro, timeout=None):
            seen["timeout"] = timeout
            coro.close()

            async def _noop():
                return None

            return _noop()

        with (
            mock.patch("kiro_crew.dashboard.chat_runner._run_chat", mock.AsyncMock()),
            mock.patch.object(asyncio, "wait_for", _capture),
        ):
            r._dispatch_turn(state, slot, "do the thing")
            await slot.task
        assert seen["timeout"] == r.CHAT_TURN_TIMEOUT

    def test_a_busy_slot_queues_the_turn_and_shows_it_redacted(self):
        slot = _Slot("spec-builder-demo", running=True)
        state = _State(**{"spec-builder-demo": slot})
        r._dispatch_turn(state, slot, f"use {CRED_NAME}")
        assert slot._queue == [f"use {CRED_NAME}"]
        # "queued" is NOT a role the slot suppresses the global SSE push for, so
        # this text reaches every connected dashboard client.
        assert slot.messages[0]["role"] == "queued"
        assert CRED_NAME not in slot.messages[0]["content"]
        assert state.pushes == 1

    def test_a_slot_that_cannot_queue_or_echo_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot.queue_append = mock.Mock(side_effect=RuntimeError("full"))  # type: ignore
        slot.append = mock.Mock(side_effect=RuntimeError("closed"))  # type: ignore
        state = _State(**{"spec-builder-demo": slot})
        r._dispatch_turn(state, slot, "text")
        assert state.pushes == 1


# -- discovery + spec dir preparation ----------------------------------------


def _spec_tree(root: Path, name: str, *files: str) -> Path:
    spec = root / ".kiro" / "specs" / name
    spec.mkdir(parents=True)
    for fname in files:
        (spec / fname).write_text("x")
    return spec


class TestDiscoverFolderSpecs:
    def test_a_spec_shaped_directory_under_a_known_root_is_adopted(self, tmp_path):
        found = _spec_tree(tmp_path, "from-cli", "requirements.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is True
        assert index["from-cli"]["spec_dir"] == str(found)
        assert index["from-cli"]["discovered"] is True
        assert r._owns_slot_key("from-cli", index["from-cli"]["slot_key"])

    def test_nothing_to_add_reports_false(self, tmp_path):
        (tmp_path / ".kiro" / "specs").mkdir(parents=True)
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_directory_without_kiro_markdown_is_not_a_spec(self, tmp_path):
        _spec_tree(tmp_path, "just-notes", "notes.txt")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_deleted_directory_is_not_adopted_back(self, tmp_path):
        found = _spec_tree(tmp_path, "deleted-one", "tasks.md")
        r._remember_deleted(str(found))
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        # Deleting is a decision; the tombstone is what remembers it.
        assert r._discover_folder_specs(index) is False
        assert "deleted-one" not in index

    def test_an_already_indexed_directory_is_not_re_added(self, tmp_path):
        found = _spec_tree(tmp_path, "known", "tasks.md")
        index = {"known": _entry(found, working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_a_name_that_fails_the_admission_predicate_is_skipped(self, tmp_path):
        _spec_tree(tmp_path, CRED_NAME, "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        # Admitting on the grammar alone would re-add an entry the next load
        # drops, rediscovering it on every call.
        assert r._discover_folder_specs(index) is False

    def test_a_file_where_a_spec_directory_would_be_is_skipped(self, tmp_path):
        base = tmp_path / ".kiro" / "specs"
        base.mkdir(parents=True)
        (base / "a-file.md").write_text("x")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_an_unusable_indexed_root_is_not_enumerated(self, tmp_path):
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path / "gone"))}
        assert r._discover_folder_specs(index) is False

    def test_a_sensitive_indexed_root_is_not_enumerated(self, tmp_path):
        _spec_tree(tmp_path, "inside", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            assert r._discover_folder_specs(index) is False

    def test_a_root_with_no_specs_directory_is_skipped(self, tmp_path):
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        assert r._discover_folder_specs(index) is False

    def test_an_unreadable_specs_directory_is_skipped(self, tmp_path):
        _spec_tree(tmp_path, "one", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        with mock.patch.object(Path, "iterdir", side_effect=OSError("denied")):
            assert r._discover_folder_specs(index) is False

    def test_a_failing_stat_falls_back_to_now(self, tmp_path):
        _spec_tree(tmp_path, "one", "tasks.md")
        index = {"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))}
        real_stat = Path.stat
        seen: list[str] = []

        def _boom(self, *a, **kw):
            # The product stats the candidate twice: once through is_dir(), then
            # again for created_at. Only the second one is the guarded call.
            if self.name == "one":
                seen.append(str(self))
                if len(seen) > 1:
                    raise OSError("stat failed")
            return real_stat(self, *a, **kw)

        with mock.patch.object(Path, "stat", _boom):
            assert r._discover_folder_specs(index) is True
        assert index["one"]["created_at"] > 0
        assert index["one"]["created_at"] == index["one"]["updated_at"]


class TestLoadIndexWithDiscovery:
    def test_it_returns_the_index_and_every_derived_phase_in_one_hop(self, tmp_path):
        found = _spec_tree(tmp_path, "found", "requirements.md", "design.md")
        _write_index({"seed": _entry(tmp_path / "seed", working_dir=str(tmp_path))})
        index, phases = r._load_index_with_discovery()
        assert set(index) == {"seed", "found"}
        assert phases["found"] == "design"
        assert phases["seed"] == "new"
        # Discovery persists, so the next read does not have to rescan.
        assert "found" in json.loads(r._index_path().read_text())
        assert found.is_dir()


class TestPrepareSpecDir:
    def test_it_creates_the_spec_directory(self, tmp_path):
        spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == ""
        assert spec_dir == (tmp_path / ".kiro" / "specs" / "demo").resolve()
        assert spec_dir.is_dir()

    def test_a_resolved_path_outside_the_declared_root_is_refused(self, tmp_path):
        outside = tmp_path.parent / "elsewhere"
        with mock.patch.object(r, "_resolve_spec_dir", return_value=outside / "demo"):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "escape"

    def test_the_settings_base_path_becomes_the_declared_root(self, tmp_path):
        store = tmp_path / "store"
        store.mkdir()
        r._save_settings({"base_path": str(store)})
        spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == ""
        assert spec_dir == (store / "demo").resolve()

    def test_a_destination_that_resolves_somewhere_sensitive_is_refused(self, tmp_path):
        real_sensitive = r.is_sensitive_path
        target = str((tmp_path / ".kiro" / "specs" / "demo").resolve())

        def _sensitive(path: str, *a, **kw) -> bool:
            # Containment only says "under the declared root"; if that root grows a
            # link into a credential tree, BOTH paths resolve through it.
            return path == target or real_sensitive(path, *a, **kw)

        with mock.patch.object(r, "is_sensitive_path", _sensitive):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "escape"

    def test_existing_kiro_documents_are_not_adopted_by_overwrite(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md", "tasks.md")
        _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal == "existing:requirements.md, tasks.md"

    def test_opting_in_adopts_them(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md")
        _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", True)
        assert refusal == ""

    def test_a_failing_mkdir_is_reported_rather_than_raised(self, tmp_path):
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            _spec_dir, refusal = r._prepare_spec_dir(str(tmp_path), tmp_path, "demo", False)
        assert refusal.startswith("mkdir:") and "read-only fs" in refusal


# -- HTTP: request plumbing ---------------------------------------------------


BASE = f"/api/apps/{r.APP_NAME}"


def _readable_payload():
    """A payload stub whose ``at_eof()`` is False, so ``can_read_body`` is True.

    ``make_mocked_request`` defaults to an empty stream, which reads as "no body"
    -- and ``_client_claim`` only consults the body when it can be read.
    """
    payload = mock.Mock()
    payload.at_eof.return_value = False
    return payload


def _mk(
    method: str,
    path: str,
    *,
    state=None,
    match: dict | None = None,
    query: str = "",
    body=...,
    authed: bool = True,
):
    """A mocked aiohttp request for a handler under test."""
    full = f"{BASE}/{path}" + (f"?{query}" if query else "")
    app = {"state": state} if state is not None else {}
    kwargs = {"app": app, "match_info": match or {}}
    if body is not ...:
        kwargs["payload"] = _readable_payload()
    req = make_mocked_request(method, full, **kwargs)  # type: ignore[arg-type]
    if authed:
        req["user"] = "test-user"
    if body is not ...:
        if body is None:
            req.json = mock.AsyncMock(side_effect=ValueError("bad json"))  # type: ignore
        else:
            req.json = mock.AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


@pytest.fixture(autouse=True)
def _no_autonudge():
    """Default to "no autonudge service" so nothing reaches a live loop registry."""
    with mock.patch.object(r, "_autonudge_instance", lambda: None):
        yield


class TestRequireAuth:
    def test_a_request_the_auth_middleware_never_touched_is_401(self):
        denied = r._require_auth(_mk("GET", "specs", authed=False))
        assert denied is not None and denied.status == 401
        assert _body(denied)["code"] == "unauthorized"

    def test_an_authenticated_request_passes(self):
        assert r._require_auth(_mk("GET", "specs")) is None


class TestReadJson:
    @pytest.mark.asyncio
    async def test_a_payload_that_does_not_decode_is_400(self):
        out = await r._read_json(_mk("POST", "specs", body=None))
        assert isinstance(out, web.Response) and out.status == 400
        assert _body(out)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_a_json_value_that_is_not_an_object_is_400(self):
        out = await r._read_json(_mk("POST", "specs", body=[1, 2]))
        assert isinstance(out, web.Response) and out.status == 400
        assert _body(out)["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_an_object_is_returned_as_a_dict(self):
        assert await r._read_json(_mk("POST", "specs", body={"a": 1})) == {"a": 1}


class TestRequireEnabled:
    @pytest.mark.asyncio
    async def test_a_disabled_app_is_denied_even_though_the_route_is_registered(self):
        # Routes are wired once at gateway startup, so a default-disabled app
        # would otherwise stay callable.
        inner = mock.AsyncMock(return_value=web.json_response({"ok": True}))
        wrapped = r._require_enabled(inner)
        recovery = mock.AsyncMock()
        with (
            mock.patch.object(r, "is_app_enabled", return_value=False),
            mock.patch.object(r, "_ensure_duplicate_recovery", recovery),
        ):
            out = await wrapped(_mk("GET", "specs"))
        assert out.status == 403 and _body(out)["code"] == "app_disabled"
        inner.assert_not_awaited()
        recovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_enabled_app_reaches_the_handler(self):
        inner = mock.AsyncMock(return_value=web.json_response({"ok": True}))
        wrapped = r._require_enabled(inner)
        request = _mk("GET", "specs")
        recovery = mock.AsyncMock()
        with (
            mock.patch.object(r, "is_app_enabled", return_value=True),
            mock.patch.object(r, "_ensure_duplicate_recovery", recovery),
        ):
            out = await wrapped(request)
        assert _body(out) == {"ok": True}
        recovery.assert_awaited_once_with(request.app)

    def test_the_wrapper_keeps_the_handlers_identity(self):
        assert r._require_enabled(r._handle_list).__name__ == "_handle_list"


class TestClientClaim:
    @pytest.mark.asyncio
    async def test_a_delete_carries_its_claim_in_the_query_string(self):
        req = _mk("DELETE", "specs/demo", query="spec_dir=/s&slot_key=spec-builder-demo-0123abcd")
        assert await r._client_claim(req) == r._ClientClaim("/s", "spec-builder-demo-0123abcd")

    @pytest.mark.asyncio
    async def test_a_post_can_carry_it_in_the_body(self):
        req = _mk(
            "POST",
            "specs/demo/stop",
            body={"spec_dir": " /s ", "slot_key": " spec-builder-demo-0123abcd "},
        )
        assert await r._client_claim(req) == r._ClientClaim("/s", "spec-builder-demo-0123abcd")

    @pytest.mark.asyncio
    async def test_a_request_with_neither_is_unpinned_rather_than_refused(self):
        # An older tab predates these fields, so "" must keep working.
        assert await r._client_claim(_mk("POST", "specs/demo/stop")) == r._ClientClaim("", "")

    @pytest.mark.asyncio
    async def test_a_body_that_does_not_decode_leaves_the_claim_empty(self):
        assert await r._client_claim(_mk("POST", "specs/demo/stop", body=None)) == r._ClientClaim(
            "", ""
        )

    @pytest.mark.asyncio
    async def test_a_non_object_body_leaves_the_claim_empty(self):
        assert await r._client_claim(
            _mk("POST", "specs/demo/stop", body=["nope"])
        ) == r._ClientClaim("", "")

    @pytest.mark.asyncio
    async def test_the_query_string_is_not_overridden_by_the_body(self):
        req = _mk(
            "POST",
            "specs/demo/stop",
            query="spec_dir=/from-query&slot_key=spec-builder-demo-0123abcd",
            body={"spec_dir": "/from-body", "slot_key": "spec-builder-demo-ffffffff"},
        )
        assert (await r._client_claim(req)).spec_dir == "/from-query"


# -- HTTP: repo info, browse, settings ---------------------------------------


class TestHandleRepoInfo:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_repo_info(_mk("GET", "repo-info", authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_no_path_answers_not_a_repo_without_running_git(self):
        git = mock.AsyncMock()
        with mock.patch.object(r, "_repo_info", git):
            out = await r._handle_repo_info(_mk("GET", "repo-info"))
        assert _body(out) == {"is_git": False}
        git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_path_that_fails_the_chokepoint_answers_not_a_repo(self, tmp_path):
        # The hand-rolled is_absolute()/is_dir() pair skipped the sensitive-path
        # denial _safe_dir applies, and statted on the event loop.
        out = await r._handle_repo_info(
            _mk("GET", "repo-info", query=f"path={tmp_path}/missing")
        )
        assert _body(out) == {"is_git": False}

    @pytest.mark.asyncio
    async def test_a_real_directory_is_probed(self, tmp_path):
        with mock.patch.object(
            r, "_repo_info", mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)})
        ):
            out = await r._handle_repo_info(_mk("GET", "repo-info", query=f"path={tmp_path}"))
        assert _body(out)["is_git"] is True


class TestHandleBrowse:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_browse(_mk("GET", "browse", authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_a_denied_path_is_403_and_audited(self, tmp_path, _quiet_sel):
        out = await r._handle_browse(_mk("GET", "browse", query=f"path={tmp_path}/gone"))
        assert out.status == 403 and _body(out)["code"] == "access_denied"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_browse_denied" in ops

    @pytest.mark.asyncio
    async def test_a_named_path_lists_its_subdirectories_and_its_parent(self, tmp_path):
        (tmp_path / "src").mkdir()
        with mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={"is_git": True})):
            out = await r._handle_browse(_mk("GET", "browse", query=f"path={tmp_path}"))
        payload = _body(out)
        assert payload["path"] == str(Path(os.path.realpath(tmp_path)))
        assert payload["parent"] == os.path.dirname(os.path.realpath(tmp_path))
        assert [d["name"] for d in payload["dirs"]] == ["src"]
        assert payload["is_git"] is True
        # recents are only attached to the INITIAL empty-path call.
        assert "recents" not in payload

    @pytest.mark.asyncio
    async def test_the_initial_call_defaults_to_home_and_attaches_recents(self, tmp_path):
        with (
            mock.patch.object(Path, "home", return_value=tmp_path),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
            mock.patch.object(r, "_read_recent_projects", return_value=["/p1"]),
        ):
            out = await r._handle_browse(_mk("GET", "browse"))
        payload = _body(out)
        assert payload["recents"] == ["/p1"]
        assert payload["is_git"] is False


class TestReadRecentProjects:
    def test_a_missing_file_is_an_empty_list(self):
        assert r._read_recent_projects() == []

    def test_only_existing_directories_survive_and_the_list_is_bounded(self, tmp_path):
        from kiro_crew.config.paths import config_dir

        real = [str(tmp_path / f"p{i}") for i in range(12)]
        for path in real:
            Path(path).mkdir()
        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "recent_projects.json").write_text(
            json.dumps([*real, str(tmp_path / "gone"), 7, None])
        )
        assert r._read_recent_projects() == real[:10]

    def test_a_malformed_or_wrongly_shaped_file_is_an_empty_list(self):
        from kiro_crew.config.paths import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "recent_projects.json").write_text('{"not": "a list"}')
        assert r._read_recent_projects() == []


class TestHandleSettings:
    @pytest.mark.asyncio
    async def test_unauthenticated_on_both_verbs(self):
        assert (await r._handle_get_settings(_mk("GET", "settings", authed=False))).status == 401
        assert (await r._handle_put_settings(_mk("PUT", "settings", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_the_default_is_an_empty_base_path(self):
        assert _body(await r._handle_get_settings(_mk("GET", "settings"))) == {
            "base_path": "",
            "model": "",
        }

    @pytest.mark.asyncio
    async def test_a_stored_base_path_is_redacted_on_its_way_out(self):
        # settings.json is agent-writable, so a credential parked in base_path
        # would otherwise be rendered verbatim in the dashboard.
        r._save_settings({"base_path": f"/srv/{CRED_NAME}"})
        assert CRED_NAME not in _body(await r._handle_get_settings(_mk("GET", "settings")))[
            "base_path"
        ]

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self):
        out = await r._handle_put_settings(_mk("PUT", "settings", body=None))
        assert out.status == 400

    @pytest.mark.asyncio
    async def test_a_relative_base_path_is_refused(self):
        out = await r._handle_put_settings(_mk("PUT", "settings", body={"base_path": "specs"}))
        assert out.status == 400 and _body(out)["code"] == "base_path_not_absolute"

    @pytest.mark.asyncio
    async def test_a_base_path_that_fails_the_chokepoint_is_refused(self, tmp_path):
        # Without this, spec storage could be repointed at a credential directory
        # and every subsequent spec would write into it.
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            out = await r._handle_put_settings(
                _mk("PUT", "settings", body={"base_path": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "base_path_not_a_directory"

    @pytest.mark.asyncio
    async def test_a_usable_base_path_is_stored_fully_resolved(self, tmp_path):
        out = await r._handle_put_settings(
            _mk("PUT", "settings", body={"base_path": f" {tmp_path} "})
        )
        assert _body(out) == {
            "ok": True,
            "base_path": str(Path(os.path.realpath(tmp_path))),
            "model": "",
        }
        assert r._load_settings()["base_path"] == str(Path(os.path.realpath(tmp_path)))

    @pytest.mark.asyncio
    async def test_an_empty_base_path_clears_the_override(self, tmp_path):
        r._save_settings({"base_path": str(tmp_path)})
        out = await r._handle_put_settings(_mk("PUT", "settings", body={"base_path": ""}))
        assert _body(out) == {"ok": True, "base_path": "", "model": ""}
        assert r._load_settings()["base_path"] == ""


# -- HTTP: list ---------------------------------------------------------------


class TestHandleList:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        assert (await r._handle_list(_mk("GET", "specs", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_an_empty_index_still_reports_the_default_location(self):
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert payload == {"specs": [], "default_base": ".kiro/specs"}

    @pytest.mark.asyncio
    async def test_every_string_from_the_index_is_redacted(self, tmp_path):
        _write_index(
            {
                "demo": _entry(
                    tmp_path / f"specs/{CRED_NAME}",
                    working_dir=str(tmp_path),
                    spec_type=CRED_NAME,
                )
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert CRED_NAME not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_a_delete_in_flight_is_not_a_spec_the_user_still_has(self, tmp_path):
        _write_index(
            {
                "doomed": _entry(
                    tmp_path / "doomed", deleting={"owner": r._PROCESS_ID, "at": time.time()}
                ),
                "kept": _entry(tmp_path / "kept"),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["kept"]

    @pytest.mark.asyncio
    async def test_the_status_is_reconciled_rather_than_echoed(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        # A capped nudge loop that ran out of cycles leaves "executing" forever.
        assert payload["specs"][0]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_the_live_running_flag_comes_from_the_slot(self, tmp_path):
        state = _State(
            **{"spec-builder-demo-0123abcd": _Slot("spec-builder-demo-0123abcd", running=True)}
        )
        _write_index({"demo": _entry(tmp_path / "demo")})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=state)))
        assert payload["specs"][0]["running"] is True

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_state_still_lists(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        payload = _body(await r._handle_list(_mk("GET", "specs")))
        assert payload["specs"][0]["running"] is False

    @pytest.mark.asyncio
    async def test_the_derived_phase_is_reported(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "requirements.md").write_text("x")
        _write_index({"demo": _entry(spec)})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert payload["specs"][0]["phase"] == "requirements"

    @pytest.mark.asyncio
    async def test_agent_written_timestamps_cannot_break_the_sort(self, tmp_path):
        # Mixing a str and a float in one sort key raises TypeError, which turned a
        # single malformed entry into a 500 on EVERY list request.
        _write_index(
            {
                "newest": _entry(tmp_path / "a", updated_at=900.0),
                "broken": _entry(tmp_path / "b", updated_at="soon", created_at="also-soon"),
                "middle": _entry(tmp_path / "c", updated_at=500.0),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["newest", "middle", "broken"]
        assert payload["specs"][-1]["updated_at"] == 0.0

    @pytest.mark.asyncio
    async def test_an_entry_with_no_updated_at_orders_by_created_at(self, tmp_path):
        _write_index(
            {
                "older": _entry(tmp_path / "a", updated_at=0, created_at=100.0),
                "newer": _entry(tmp_path / "b", updated_at=0, created_at=800.0),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["newer", "older"]


# -- HTTP: create -------------------------------------------------------------


@pytest.fixture
def _no_dispatch():
    """Replace the turn relay: creating a real task needs a live chat runner."""
    with mock.patch.object(r, "_dispatch_turn") as dispatch:
        yield dispatch


class TestHandleCreate:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        assert (await r._handle_create(_mk("POST", "specs", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self):
        assert (await r._handle_create(_mk("POST", "specs", body=None))).status == 400

    @pytest.mark.asyncio
    async def test_a_name_that_fails_the_grammar_is_400(self):
        out = await r._handle_create(_mk("POST", "specs", body={"name": "has space"}))
        assert out.status == 400 and _body(out)["code"] == "invalid_name"

    @pytest.mark.asyncio
    async def test_a_credential_shaped_name_is_400_not_a_spec_the_loader_will_drop(self):
        out = await r._handle_create(_mk("POST", "specs", body={"name": CRED_NAME}))
        assert out.status == 400 and _body(out)["code"] == "invalid_name"

    @pytest.mark.asyncio
    async def test_an_unknown_spec_type_is_400(self, tmp_path):
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "spec_type": "epic"})
        )
        assert out.status == 400 and _body(out)["code"] == "invalid_spec_type"

    @pytest.mark.asyncio
    async def test_a_relative_working_dir_is_400(self):
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": "project"})
        )
        assert out.status == 400 and _body(out)["code"] == "working_dir_not_absolute"

    @pytest.mark.asyncio
    async def test_a_missing_or_sensitive_working_dir_gets_one_indistinguishable_400(
        self, tmp_path
    ):
        # One response for "missing", "not a directory" and "sensitive" so the
        # endpoint cannot be used to probe the filesystem.
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path / "gone")})
        )
        assert out.status == 400 and _body(out)["code"] == "working_dir_not_a_directory"
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert _body(out)["code"] == "working_dir_not_a_directory"

    @pytest.mark.asyncio
    async def test_a_duplicate_name_is_409(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
        )
        assert out.status == 409 and _body(out)["code"] == "spec_exists"

    @pytest.mark.asyncio
    async def test_a_successful_create_returns_201_and_seeds_the_agent(
        self, tmp_path, _no_dispatch, _quiet_sel
    ):
        state = _State()
        with _no_rehydrate():
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={
                        "name": "demo",
                        "working_dir": str(tmp_path),
                        "spec_type": "quick",
                        "description": "add a picker",
                    },
                )
            )
        payload = _body(out)
        assert out.status == 201
        assert payload["name"] == "demo" and payload["spec_type"] == "quick"
        assert payload["status"] == "planning" and payload["worktree_branch"] == ""
        spec_dir = Path(payload["spec_dir"])
        assert spec_dir.is_dir()
        # A fresh per-creation key, so a reused name never appends to the previous
        # spec's transcript.
        stored = r._load_index()["demo"]
        assert r._owns_slot_key("demo", stored["slot_key"])
        assert stored["slot_key"] != "spec-builder-demo"
        seed = _no_dispatch.call_args.args[2]
        assert "add a picker" in seed and "QUICK spec" in seed
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_create" in ops

    @pytest.mark.asyncio
    async def test_creating_clears_an_earlier_tombstone_for_the_same_directory(
        self, tmp_path, _no_dispatch
    ):
        spec_dir = (tmp_path / ".kiro" / "specs" / "demo").resolve()
        r._remember_deleted(str(spec_dir))
        with _no_rehydrate():
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 201
        # Creating is an explicit decision that outranks an earlier delete.
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_existing_kiro_documents_are_409_with_the_opt_in_named(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md")
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
        )
        assert out.status == 409
        payload = _body(out)
        assert payload["code"] == "spec_files_exist"
        assert "import_existing" in payload["error"]

    @pytest.mark.asyncio
    async def test_a_resolved_path_outside_its_root_is_400_and_audited(
        self, tmp_path, _quiet_sel
    ):
        with mock.patch.object(
            r, "_prepare_spec_dir", return_value=(tmp_path / "elsewhere", "escape")
        ):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "spec_path_outside_root"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_path_escape_denied" in ops

    @pytest.mark.asyncio
    async def test_a_directory_that_cannot_be_created_is_400(self, tmp_path):
        with mock.patch.object(
            r, "_prepare_spec_dir", return_value=(tmp_path / "x", "mkdir:read-only fs")
        ):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "spec_dir_creation_failed"
        assert "read-only fs" in _body(out)["error"]

    @pytest.mark.asyncio
    async def test_a_concurrent_create_that_wins_the_index_makes_this_one_409(self, tmp_path):
        # The duplicate check at the top is stale by the time the awaits are done,
        # so the insert is the arbitration and the loser touches no slot state.
        with mock.patch.object(r, "_mutate_index", mock.AsyncMock(return_value=False)):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "spec_exists"

    @pytest.mark.asyncio
    async def test_a_slot_owned_by_another_app_unwinds_the_insert(self, tmp_path):
        state = _State(
            **{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")}
        )
        with _no_rehydrate(), mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        assert r._load_index() == {}

    @pytest.mark.asyncio
    async def test_a_spec_replaced_during_slot_setup_is_409_and_unwound(
        self, tmp_path, _no_dispatch, _quiet_sel
    ):
        state = _State()

        async def _steal(*_a, **_kw):
            # A concurrent delete + re-import lands in the slot-setup window; the
            # seed prompt must not drive the replacement's agent.
            index = r._load_index()
            index["demo"]["slot_key"] = "spec-builder-demo-ffffffff"
            r._save_index(index)
            return _Slot("spec-builder-demo")

        with mock.patch.object(r, "_ensure_worker_slot", _steal):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_create"
        _no_dispatch.assert_not_called()
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_create_aborted" in ops

    @pytest.mark.asyncio
    async def test_the_unwind_leaves_a_replacements_entry_alone(self, tmp_path, _no_dispatch):
        """The pop keys off the NAME, so an unpinned unwind would delete a
        same-name spec created while we were validating."""
        replacement = _entry(tmp_path / "other", slot_key="spec-builder-demo-ffffffff")

        async def _replace(*_a, **_kw):
            r._save_index({"demo": dict(replacement)})
            return None

        with mock.patch.object(r, "_ensure_worker_slot", _replace):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409
        assert r._load_index()["demo"]["slot_key"] == "spec-builder-demo-ffffffff"

    @pytest.mark.asyncio
    async def test_a_title_that_cannot_be_set_does_not_fail_the_create(
        self, tmp_path, _no_dispatch
    ):
        class _NoTitle(_Slot):
            def __setattr__(self, name, value):
                if name == "title" and getattr(self, "_ready", False):
                    raise RuntimeError("frozen")
                super().__setattr__(name, value)

        slot = _NoTitle("spec-builder-demo")
        slot._ready = True
        with mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=slot)):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 201


class TestHandleCreateWithWorktree:
    @pytest.mark.asyncio
    async def test_a_non_repository_is_400(self, tmp_path):
        with mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={"is_git": False})):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={
                        "name": "demo",
                        "working_dir": str(tmp_path),
                        "use_worktree": True,
                    },
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_requires_git"

    @pytest.mark.asyncio
    async def test_a_failed_creation_is_400(self, tmp_path):
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value="path already exists")
            ),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_creation_failed"

    @pytest.mark.asyncio
    async def test_a_worktree_that_is_not_a_usable_directory_is_rolled_back(self, tmp_path):
        remove = mock.AsyncMock()
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r,
                "_create_worktree",
                mock.AsyncMock(return_value=(str(tmp_path / "wt-gone"), "spec/demo")),
            ),
            mock.patch.object(r, "_remove_worktree", remove),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_unusable"
        remove.assert_awaited_once_with(str(tmp_path), str(tmp_path / "wt-gone"), "spec/demo")

    @pytest.mark.asyncio
    async def test_the_worktree_becomes_the_containment_root_for_the_spec_files(
        self, tmp_path, _no_dispatch
    ):
        # Without re-validating the worktree, containment is still measured against
        # the ORIGINAL checkout and every worktree-mode create fails.
        worktree = tmp_path / "repo-wt-demo"
        worktree.mkdir()
        with (
            _no_rehydrate(),
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value=(str(worktree), "spec/demo"))
            ),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        payload = _body(out)
        assert out.status == 201
        assert payload["worktree_branch"] == "spec/demo"
        assert payload["working_dir"] == str(Path(os.path.realpath(worktree)))
        assert payload["spec_dir"].startswith(str(Path(os.path.realpath(worktree))))

    @pytest.mark.asyncio
    async def test_a_refusal_after_the_worktree_exists_removes_it(self, tmp_path):
        worktree = tmp_path / "repo-wt-demo"
        worktree.mkdir()
        remove = mock.AsyncMock()
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value=(str(worktree), "spec/demo"))
            ),
            mock.patch.object(r, "_remove_worktree", remove),
            mock.patch.object(r, "_prepare_spec_dir", return_value=(worktree, "escape")),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400
        remove.assert_awaited_once()


# -- HTTP: detail, transcript, message ----------------------------------------


class TestHandleGet:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}, authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        assert out.status == 404 and _body(out)["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_the_documents_state_and_context_counters_are_returned(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "requirements.md").write_text("the requirements")
        (spec / "tasks.md").write_text("- [ ] one")
        (spec / ".spec-state.json").write_text(json.dumps({"blocking": "review"}))
        _write_index({"demo": _entry(spec, worktree_branch="spec/demo")})
        slot = _Slot("spec-builder-demo-0123abcd", running=True)
        slot.messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "fs_write"},
            {"role": "tool", "content": "fs_read"},
            {"role": "assistant", "content": "done"},
        ]
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _no_rehydrate():
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=state, match={"name": "demo"})
            )
        payload = _body(out)
        assert payload["phase"] == "tasks"
        assert payload["files"]["requirements.md"] == "the requirements"
        assert payload["files"]["design.md"] is None
        assert payload["state"]["blocking"] == "review"
        assert payload["running"] is True
        assert payload["slot_key"] == "spec-builder-demo-0123abcd"
        assert payload["context"] == {
            "worktree_branch": "spec/demo",
            "turns": 1,
            "tool_calls": 2,
        }

    @pytest.mark.asyncio
    async def test_without_a_live_slot_the_key_is_resolved_from_the_index(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})
        # The SPA must NOT derive the key from the name: keys are per-creation, so a
        # reused name would mount the embed against the previous transcript.
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        payload = _body(out)
        assert payload["slot_key"] == "spec-builder-demo-0123abcd"
        assert payload["running"] is False

    @pytest.mark.asyncio
    async def test_a_spec_deleted_while_the_documents_were_read_is_404(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})

        def _collect_then_delete(_spec_dir):
            r._save_index({})
            return "new", {}, None, {}

        with mock.patch.object(r, "_collect_spec_documents", _collect_then_delete):
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_spec_recreated_elsewhere_during_the_read_is_409(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})

        def _collect_then_replace(_spec_dir):
            r._save_index({"demo": _entry(tmp_path / "somewhere-else")})
            return "new", {}, None, {}

        with mock.patch.object(r, "_collect_spec_documents", _collect_then_replace):
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_read"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_the_whole_detail_read(self, tmp_path):
        # Returning 200 anyway let the user read, message into and approve tool
        # calls in an unrelated session from this app.
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec)})
        state = _State(
            **{
                "spec-builder-demo-0123abcd": _Slot(
                    "spec-builder-demo-0123abcd", app="issue-radar"
                )
            }
        )
        with _no_rehydrate():
            out = await r._handle_get(
                _mk("GET", "specs/demo", state=state, match={"name": "demo"})
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"

    @pytest.mark.asyncio
    async def test_index_strings_are_redacted(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        _write_index({"demo": _entry(spec, spec_type=CRED_NAME, worktree_branch=CRED_NAME)})
        out = await r._handle_get(_mk("GET", "specs/demo", match={"name": "demo"}))
        assert CRED_NAME not in json.dumps(_body(out))


class TestHandleMessages:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_messages(
            _mk("GET", "specs/demo/messages", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_messages(
            _mk("GET", "specs/demo/messages", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_the_transcript_and_the_running_flag_are_returned(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        slot = _Slot("spec-builder-demo-0123abcd", running=True)
        slot.messages = [{"role": "user", "content": "hello", "ts": "1"}]
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _no_rehydrate():
            out = await r._handle_messages(
                _mk("GET", "specs/demo/messages", state=state, match={"name": "demo"})
            )
        assert _body(out) == {
            "messages": [{"role": "user", "content": "hello", "ts": "1"}],
            "running": True,
        }

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_rather_than_leaking_the_conversation(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        state = _State(
            **{
                "spec-builder-demo-0123abcd": _Slot(
                    "spec-builder-demo-0123abcd", app="issue-radar"
                )
            }
        )
        with _no_rehydrate():
            out = await r._handle_messages(
                _mk("GET", "specs/demo/messages", state=state, match={"name": "demo"})
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk("POST", "specs/demo/message", state=_State(), match={"name": "demo"}, body=None)
        )
        assert out.status == 400

    @pytest.mark.asyncio
    async def test_an_empty_text_is_400(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk(
                "POST",
                "specs/demo/message",
                state=_State(),
                match={"name": "demo"},
                body={"text": "   "},
            )
        )
        assert out.status == 400 and _body(out)["code"] == "text_required"

    @pytest.mark.asyncio
    async def test_a_claim_naming_a_different_directory_is_409(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_message(
            _mk(
                "POST",
                "specs/demo/message",
                state=_State(),
                match={"name": "demo"},
                body={"text": "go", "spec_dir": "/somewhere-else"},
            )
        )
        assert out.status == 409 and _body(out)["code"] == "stale_client"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_refuses_the_dispatch(self, tmp_path, _no_dispatch):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)):
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={"text": "go"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_delete_that_lands_during_slot_acquisition_is_409(
        self, tmp_path, _no_dispatch
    ):
        # _ensure_worker_slot awaits, so a delete can start AND finish between the
        # first check and the dispatch.
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _slot_then_delete(*_a, **_kw):
            r._save_index({})
            return _Slot("spec-builder-demo-0123abcd")

        with mock.patch.object(r, "_ensure_worker_slot", _slot_then_delete):
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={"text": "go"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_turn_is_relayed_and_the_spec_stamped(self, tmp_path, _no_dispatch):
        _write_index({"demo": _entry(tmp_path / "demo", working_dir=str(tmp_path))})
        with _no_rehydrate():
            out = await r._handle_message(
                _mk(
                    "POST",
                    "specs/demo/message",
                    state=_State(),
                    match={"name": "demo"},
                    body={
                        "text": " draft it ",
                        "spec_dir": str(tmp_path / "demo"),
                        "slot_key": "spec-builder-demo-0123abcd",
                    },
                )
            )
        assert _body(out) == {"ok": True}
        assert _no_dispatch.call_args.args[2] == "draft it"
        assert r._load_index()["demo"]["updated_at"] > 200.0


# -- HTTP: handoff ------------------------------------------------------------


@pytest.fixture
def _armed():
    """A stub autonudge service plus a successful authorization."""
    svc = mock.MagicMock()
    svc.get_by_slot.return_value = None
    svc.remove = mock.AsyncMock()
    authz = mock.AsyncMock(return_value=(SimpleNamespace(id="loop-1"), None, 0))
    with (
        mock.patch.object(r, "_autonudge_instance", lambda: svc),
        mock.patch.object(r, "authorize_and_add_nudge", authz),
        mock.patch.object(r, "_exec_loop_active", return_value=False),
    ):
        yield SimpleNamespace(svc=svc, authz=authz)


def _ready_handoff(sentinel: str = "/spec/STOP"):
    return mock.patch.object(r, "_prepare_handoff", return_value=(True, sentinel))


def _handoff_request(tmp_path, state, **body):
    _write_index({"demo": _entry(tmp_path / "demo", working_dir=str(tmp_path))})
    return _mk(
        "POST",
        "specs/demo/handoff",
        state=state,
        match={"name": "demo"},
        body=body or {},
    )


class TestHandleHandoff:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_handoff(
            _mk("POST", "specs/demo/handoff", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_handoff(
            _mk("POST", "specs/demo/handoff", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_before_the_sentinel_is_disarmed(self, tmp_path):
        prepare = mock.Mock(return_value=(True, "/spec/STOP"))
        with mock.patch.object(r, "_prepare_handoff", prepare):
            out = await r._handle_handoff(
                _handoff_request(tmp_path, _State(), spec_dir="/somewhere-else")
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        # _prepare_handoff CLEARS the STOP sentinel a Pause wrote.
        prepare.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_missing_tasks_file_is_409(self, tmp_path):
        with mock.patch.object(r, "_prepare_handoff", return_value=(False, "")):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "tasks_missing"

    @pytest.mark.asyncio
    async def test_a_spec_replaced_during_the_filesystem_hop_is_409(self, tmp_path):
        def _prepare_then_replace(*_a, **_kw):
            r._save_index({"demo": _entry(tmp_path / "demo", slot_key="spec-builder-demo-ffffffff")})
            return True, "/spec/STOP"

        with mock.patch.object(r, "_prepare_handoff", _prepare_then_replace):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_no_autonudge_service_fails_closed_with_503(self, tmp_path, _quiet_sel):
        # This used to swallow the failure and run an autonomous turn WITHOUT
        # passing the authorization chokepoint at all.
        with _ready_handoff():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "autonudge_unavailable"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_handoff_denied" in ops

    @pytest.mark.asyncio
    async def test_a_missing_authorization_helper_also_fails_closed(self, tmp_path):
        svc = mock.MagicMock()
        with (
            _ready_handoff(),
            mock.patch.object(r, "_autonudge_instance", lambda: svc),
            mock.patch.object(r, "authorize_and_add_nudge", None),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "autonudge_unavailable"

    @pytest.mark.asyncio
    async def test_a_second_handoff_is_409_rather_than_a_second_dispatch(
        self, tmp_path, _armed, _no_dispatch
    ):
        with _ready_handoff(), _no_rehydrate():
            first = await r._handle_handoff(_handoff_request(tmp_path, _State()))
            assert first.status == 200
            second = await r._handle_handoff(
                _mk(
                    "POST",
                    "specs/demo/handoff",
                    state=_State(),
                    match={"name": "demo"},
                    body={},
                )
            )
        assert second.status == 409 and _body(second)["code"] == "already_executing"

    @pytest.mark.asyncio
    async def test_a_claim_that_cannot_be_recorded_is_500_and_starts_nothing(
        self, tmp_path, _armed, _no_dispatch
    ):
        # Pause keys off the recorded state, so the run must not proceed without it.
        with (
            _ready_handoff(),
            mock.patch.object(r, "_claim_execution", mock.AsyncMock(side_effect=OSError("disk"))),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 500 and _body(out)["code"] == "exec_state_write_failed"
        _no_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_spec_that_disappears_at_the_claim_is_409(self, tmp_path, _armed):
        with (
            _ready_handoff(),
            mock.patch.object(
                r, "_claim_execution", mock.AsyncMock(return_value=(r._CLAIM_GONE, {}))
            ),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_a_foreign_slot_gives_the_claim_back(self, tmp_path, _armed, _no_dispatch):
        with (
            _ready_handoff(),
            mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        # Otherwise the spec stays marked executing with nothing running.
        stored = r._load_index()["demo"]
        assert stored["status"] == "planning" and stored["exec_arming_at"] == 0.0

    @pytest.mark.asyncio
    async def test_an_authorization_that_raises_is_503_and_unwinds(
        self, tmp_path, _armed, _no_dispatch, _quiet_sel
    ):
        _armed.authz.side_effect = RuntimeError("authz exploded")
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 503 and _body(out)["code"] == "authorization_failed"
        assert r._load_index()["demo"]["status"] == "planning"
        _no_dispatch.assert_not_called()
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_handoff_aborted" in ops

    @pytest.mark.asyncio
    async def test_a_refused_authorization_is_403_and_unwinds(
        self, tmp_path, _armed, _no_dispatch
    ):
        _armed.authz.return_value = (None, "message limit reached", 0)
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 403 and _body(out)["code"] == "authorization_refused"
        assert "message limit reached" in _body(out)["error"]
        assert r._load_index()["demo"]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_a_pre_existing_slot_is_not_torn_down_by_the_unwind(
        self, tmp_path, _armed, _no_dispatch
    ):
        # A pre-existing slot carries the user's conversation; destroying it
        # because a later write failed loses work the handoff never owned.
        _armed.authz.return_value = (None, "refused", 0)
        slot = _Slot("spec-builder-demo-0123abcd")
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with _ready_handoff(), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, state))
        assert out.status == 403
        assert state._slots["spec-builder-demo-0123abcd"] is slot

    @pytest.mark.asyncio
    async def test_a_delete_landing_during_authorization_is_409_and_removes_the_loop(
        self, tmp_path, _armed, _no_dispatch
    ):
        async def _authz_then_delete(**_kw):
            r._save_index({})
            return SimpleNamespace(id="loop-1"), None, 0

        _armed.authz.side_effect = _authz_then_delete
        remove = mock.AsyncMock()
        with _ready_handoff(), _no_rehydrate(), mock.patch.object(r, "_remove_nudge_loop", remove):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"
        # Ours arrives after the delete's own by-name teardown, so it must be
        # removed here or it nudges a spec that no longer exists.
        assert remove.await_args.kwargs["only_loop_id"] == "loop-1"

    @pytest.mark.asyncio
    async def test_a_successful_handoff_arms_a_bounded_loop_and_dispatches(
        self, tmp_path, _armed, _no_dispatch
    ):
        with _ready_handoff("/spec/STOP"), _no_rehydrate():
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert _body(out) == {"ok": True, "status": "executing"}
        kwargs = _armed.authz.await_args.kwargs
        # Bounded: svc.add was called directly once, with max_cycles=0 -- an
        # unbounded loop. The shared chokepoint is what enforces the cap.
        assert kwargs["max_cycles"] == r._EXEC_MAX_CYCLES
        assert kwargs["stop_sentinel_path"] == "/spec/STOP"
        assert kwargs["source"] == "app:spec-builder"
        assert kwargs["caller"] == "test-user"
        stored = r._load_index()["demo"]
        assert stored["status"] == "executing"
        # The pre-arm exemption ends once the reconciler can see the loop.
        assert stored["exec_arming_at"] == 0.0
        prompt = _no_dispatch.call_args.args[2]
        assert "EXECUTION HANDOFF" in prompt and "tasks.md" in prompt


# -- HTTP: stop ---------------------------------------------------------------


class TestHandleStopExecution:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_stop_execution(
            _mk("POST", "specs/demo/stop", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_stop_execution(
            _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_before_anything_is_halted(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        halt = mock.AsyncMock()
        with mock.patch.object(r, "_halt_execution", halt):
            out = await r._handle_stop_execution(
                _mk(
                    "POST",
                    "specs/demo/stop",
                    state=_State(),
                    match={"name": "demo"},
                    body={"slot_key": "spec-builder-demo-ffffffff"},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        halt.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_halt_is_503_rather_than_a_false_stopped(self, tmp_path, _quiet_sel):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        with mock.patch.object(
            r, "_halt_execution", mock.AsyncMock(side_effect=OSError("store down"))
        ):
            out = await r._handle_stop_execution(
                _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "stop_failed"
        # Saying "stopped" would be false and the user would not retry.
        assert r._load_index()["demo"]["status"] == "executing"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_stop_failed" in ops

    @pytest.mark.asyncio
    async def test_a_spec_deleted_while_halting_is_404_not_a_resurrected_entry(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})

        async def _halt_then_delete(*_a, **_kw):
            r._save_index({})

        with mock.patch.object(r, "_halt_execution", _halt_then_delete):
            out = await r._handle_stop_execution(
                _mk("POST", "specs/demo/stop", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404
        assert r._load_index() == {}

    @pytest.mark.asyncio
    async def test_a_successful_stop_records_planning(self, tmp_path, _quiet_sel):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        halt = mock.AsyncMock()
        with mock.patch.object(r, "_halt_execution", halt):
            out = await r._handle_stop_execution(
                _mk(
                    "POST",
                    "specs/demo/stop",
                    state=_State(),
                    match={"name": "demo"},
                    body={
                        "spec_dir": str(tmp_path / "demo"),
                        "slot_key": "spec-builder-demo-0123abcd",
                    },
                )
            )
        assert _body(out) == {"ok": True, "status": "planning"}
        assert r._load_index()["demo"]["status"] == "planning"
        assert halt.await_args.kwargs["expect_slot_key"] == "spec-builder-demo-0123abcd"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_stop_execution" in ops


# -- HTTP: delete -------------------------------------------------------------


class TestHandleDelete:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_delete(
            _mk("DELETE", "specs/demo", match={"name": "demo"}, authed=False)
        )
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_an_unknown_spec_is_404(self):
        out = await r._handle_delete(
            _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
        )
        assert out.status == 404

    @pytest.mark.asyncio
    async def test_a_stale_claim_is_refused_and_leaves_no_tombstone(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_delete(
            _mk(
                "DELETE",
                "specs/demo",
                state=_State(),
                match={"name": "demo"},
                query="spec_dir=/somewhere-else",
            )
        )
        assert out.status == 409 and _body(out)["code"] == "stale_client"
        assert "demo" in r._load_index()

    @pytest.mark.asyncio
    async def test_a_reservation_that_cannot_be_taken_is_404_and_clears_the_tombstone(
        self, tmp_path
    ):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(r, "_mark_deleting", mock.AsyncMock(return_value=False)):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 404
        # Leaving it would hide a spec the user still has from their own list.
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_a_failed_loop_removal_aborts_the_delete_and_releases_everything(
        self, tmp_path, _quiet_sel
    ):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with mock.patch.object(
            r, "_remove_nudge_loop", mock.AsyncMock(side_effect=OSError("store unwritable"))
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "loop_removal_failed"
        stored = r._load_index()["demo"]
        assert r._DELETING not in stored
        assert r._load_deleted() == []
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_delete_aborted" in ops

    @pytest.mark.asyncio
    async def test_a_failed_archive_aborts_the_delete(self, tmp_path):
        # The conversation is the user's data; a failed history write used to be
        # logged at DEBUG while the delete returned 200.
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "archive_failed"
        assert "nothing was deleted" in _body(out)["error"]
        assert r._DELETING not in r._load_index()["demo"]
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_an_archive_failure_whose_reservation_also_sticks_says_so(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=False)),
            mock.patch.object(r, "_unmark_deleting", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503
        assert "may need a reload" in _body(out)["error"]

    @pytest.mark.asyncio
    async def test_an_archived_spec_whose_record_survives_is_503_not_an_undelete(self, tmp_path):
        # Un-deleting would be the lie the ordering exists to prevent: the
        # conversation is ALREADY archived. The reservation stays so a retry is
        # idempotent.
        _write_index({"demo": _entry(tmp_path / "demo")})
        with (
            mock.patch.object(r, "_mark_deleting", mock.AsyncMock(return_value=True)),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=True)),
            mock.patch.object(r, "_mutate_index", mock.AsyncMock(return_value=False)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"

    @pytest.mark.asyncio
    async def test_a_successful_delete_tombstones_the_directory_and_drops_the_entry(
        self, tmp_path, _quiet_sel
    ):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "tasks.md").write_text("- [x] done")
        _write_index({"demo": _entry(spec)})
        slot = _Slot("spec-builder-demo-0123abcd")
        state = _State(**{"spec-builder-demo-0123abcd": slot})
        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
            ),
        ):
            out = await r._handle_delete(
                _mk(
                    "DELETE",
                    "specs/demo",
                    state=state,
                    match={"name": "demo"},
                    query=f"spec_dir={spec}&slot_key=spec-builder-demo-0123abcd",
                )
            )
        assert _body(out) == {"ok": True}
        assert r._load_index() == {}
        assert r._load_deleted() == [str(spec)]
        # The .md files are the user's own project files -- they stay.
        assert (spec / "tasks.md").is_file()
        assert state._slots == {}
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_delete" in ops

    @pytest.mark.asyncio
    async def test_the_runtime_is_captured_only_after_the_name_is_reserved(self, tmp_path):
        """With the marker set first, a concurrent message cannot materialize a new
        slot that this capture has already passed."""
        _write_index({"demo": _entry(tmp_path / "demo")})
        order: list[str] = []
        real_mark = r._mark_deleting

        async def _mark(*a, **kw):
            order.append("reserved")
            return await real_mark(*a, **kw)

        def _loop_id(name):
            order.append("captured")
            return None

        with (
            mock.patch.object(r, "_mark_deleting", _mark),
            mock.patch.object(r, "_exec_loop_id", _loop_id),
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", mock.AsyncMock(return_value=True)),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 200
        assert order == ["reserved", "captured"]


# -- route registration -------------------------------------------------------


class TestRegisterRoutes:
    def test_every_documented_route_is_registered_and_gated(self):
        app = web.Application()
        r.register_routes(app)
        registered = {
            (route.method, route.resource.canonical)  # type: ignore[union-attr]
            for route in app.router.routes()
            # aiohttp registers a HEAD companion for every GET on its own.
            if route.method != "HEAD"
        }
        base = f"/api/apps/{r.APP_NAME}"
        assert registered == {
            ("GET", f"{base}/settings"),
            ("PUT", f"{base}/settings"),
            # POST alias: the SPA page uses POST for settings writes.
            ("POST", f"{base}/settings"),
            ("GET", f"{base}/repo-info"),
            ("GET", f"{base}/browse"),
            ("GET", f"{base}/specs"),
            ("POST", f"{base}/specs"),
            ("GET", f"{base}/specs/{{name}}"),
            ("GET", f"{base}/specs/{{name}}/messages"),
            ("POST", f"{base}/specs/{{name}}/message"),
            ("POST", f"{base}/specs/{{name}}/handoff"),
            # Alias: the SPA page calls this "execute".
            ("POST", f"{base}/specs/{{name}}/execute"),
            ("POST", f"{base}/specs/{{name}}/stop"),
            ("DELETE", f"{base}/specs/{{name}}"),
            # The user's own authority over the artifacts, rather than asking the
            # agent for every change: record an approval of the version on screen,
            # run ONE task from tasks.md, and rename/archive/duplicate the spec.
            ("POST", f"{base}/specs/{{name}}/approve"),
            ("POST", f"{base}/specs/{{name}}/task"),
            ("POST", f"{base}/specs/{{name}}/title"),
            ("POST", f"{base}/specs/{{name}}/archive"),
            ("POST", f"{base}/specs/{{name}}/duplicate"),
        }

    def test_registration_creates_nothing_on_disk(self, tmp_path):
        # This runs during start_dashboard ON THE EVENT LOOP, so a KIROCREW_HOME on
        # stalled network storage would freeze gateway startup on a directory the
        # app may never need.
        r.register_routes(web.Application())
        assert not r._state_dir().exists()

    @pytest.mark.asyncio
    async def test_the_handoff_and_execute_aliases_are_the_same_handler(self):
        app = web.Application()
        r.register_routes(app)
        handlers = {
            route.resource.canonical: route.handler  # type: ignore[union-attr]
            for route in app.router.routes()
            if route.method == "POST"
        }
        base = f"/api/apps/{r.APP_NAME}"
        assert (
            handlers[f"{base}/specs/{{name}}/handoff"].__name__
            == handlers[f"{base}/specs/{{name}}/execute"].__name__
            == "_handle_handoff"
        )


# -- failure branches the happy paths never reach -----------------------------


class TestFilesystemErrorsAreSwallowed:
    def test_an_entry_that_cannot_be_probed_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "src").mkdir()
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._scan_subdirs(str(tmp_path)) == []

    def test_a_spec_file_that_cannot_be_probed_is_refused(self, tmp_path):
        (tmp_path / "tasks.md").write_text("x")
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._spec_file(tmp_path, "tasks.md") is None

    def test_a_spec_dir_that_cannot_be_probed_does_not_verify(self, tmp_path):
        with mock.patch.object(r, "is_sensitive_path", side_effect=OSError("stale mount")):
            assert r._verified_spec_dir(tmp_path) is None

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_directory_that_cannot_be_pinned_fails_the_sentinel_write(self, tmp_path):
        with mock.patch.object(os, "open", side_effect=OSError("EACCES")):
            assert r._write_stop_sentinel(tmp_path) is False

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_failed_sentinel_write_cleans_up_its_temp_file(self, tmp_path):
        real_open = os.open
        calls: list[int] = []

        def _fail_the_temp(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:  # the directory pin succeeded; the temp create fails
                raise OSError("ENOSPC")
            return real_open(*args, **kwargs)

        with mock.patch.object(os, "open", _fail_the_temp):
            assert r._write_stop_sentinel(tmp_path) is False
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.skipif(
        not r._CAN_PIN_DIR,
        reason="directory pinning needs O_DIRECTORY + dir_fd, which Windows lacks",
    )
    def test_a_directory_that_cannot_be_pinned_leaves_the_sentinel_alone(self, tmp_path):
        marker = tmp_path / r._STOP_FILE
        marker.write_text("1")
        with mock.patch.object(os, "open", side_effect=OSError("EACCES")):
            r._clear_stop_sentinel(tmp_path)
        assert marker.exists()


class TestPrepareGitSpawn:
    def test_it_passes_the_sandbox_triple_straight_through(self):
        argv = ["git", "-C", "/repo", "rev-parse"]
        with mock.patch.object(
            r, "sandboxed_spawn_argv", return_value=(["wrapped"], {"PATH": "/bin"}, "/tmp/env")
        ) as wrap:
            assert r._prepare_git_spawn(argv) == (["wrapped"], {"PATH": "/bin"}, "/tmp/env")
        wrap.assert_called_once_with(argv)


class TestHaltGitReapRace:
    @pytest.mark.asyncio
    async def test_a_process_that_dies_during_the_reap_is_not_an_error(self):
        proc = _FakeProc()
        proc.wait = mock.AsyncMock(side_effect=ProcessLookupError())  # type: ignore
        await r._halt_git(proc, "worktree")
        assert proc.killed is True


class TestTeardownFailureBranches:
    @pytest.mark.asyncio
    async def test_a_registry_lookup_that_raises_reads_as_no_slot(self):
        state = _State()
        state.get_slot = mock.Mock(side_effect=RuntimeError("registry broken"))  # type: ignore
        assert await r._teardown_worker_slot(state, "demo") is True

    @pytest.mark.asyncio
    async def test_a_registry_pop_that_raises_does_not_stop_the_teardown(self):
        class _StubbornRegistry(dict):
            def pop(self, *_a, **_kw):
                raise RuntimeError("frozen registry")

        slot = _Slot("spec-builder-demo")
        state = _State()
        state._slots = _StubbornRegistry({"spec-builder-demo": slot})  # type: ignore[assignment]
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
        ) as save:
            assert await r._teardown_worker_slot(state, "demo") is True
        save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_worker_task_that_raises_while_being_cancelled_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        slot.task = mock.Mock()  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _explode(*_a, **_kw):
            raise RuntimeError("teardown blew up")

        with (
            mock.patch(
                "kiro_crew.dashboard.chat_persistence.save_slot_off_loop", mock.AsyncMock()
            ),
            mock.patch.object(asyncio, "wait_for", _explode),
        ):
            assert await r._teardown_worker_slot(state, "demo") is True

    @pytest.mark.asyncio
    async def test_a_slot_that_cannot_be_put_back_after_a_failed_archive_still_reports_false(self):
        class _NoWrites(dict):
            def __setitem__(self, *_a, **_kw):
                raise RuntimeError("frozen registry")

            def pop(self, key, default=None):
                return dict.pop(self, key, default)

        slot = _Slot("spec-builder-demo")
        state = _State()
        state._slots = _NoWrites({"spec-builder-demo": slot})  # type: ignore[assignment]
        with mock.patch(
            "kiro_crew.dashboard.chat_persistence.save_slot_off_loop",
            mock.AsyncMock(side_effect=OSError("disk full")),
        ):
            assert await r._teardown_worker_slot(state, "demo", require_archive=True) is False

    @pytest.mark.asyncio
    async def test_a_worker_task_that_raises_while_pausing_is_tolerated(self):
        slot = _Slot("spec-builder-demo", running=True)
        task = mock.Mock()
        task.done.return_value = False
        slot.task = task  # type: ignore[assignment]
        state = _State(**{"spec-builder-demo": slot})

        async def _explode(*_a, **_kw):
            raise RuntimeError("pause blew up")

        with mock.patch.object(asyncio, "wait_for", _explode):
            assert await r._halt_active_turn(state, "demo") is True

    def test_a_slot_that_rejects_the_synthesis_reset_is_tolerated(self):
        class _NoSynthesis:
            def __setattr__(self, name, value):
                raise RuntimeError("frozen slot")

        r._discard_queued_work(_NoSynthesis())  # must not raise


class TestDeleteRaceBranches:
    @pytest.mark.asyncio
    async def test_an_entry_that_moved_under_the_reservation_is_not_popped(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _teardown_then_move(*_a, **_kw):
            index = r._load_index()
            index["demo"]["spec_dir"] = str(tmp_path / "moved")
            r._save_index(index)
            return True

        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", _teardown_then_move),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"
        # The reservation stays, which keeps the spec hidden and the retry idempotent.
        assert r._load_index()["demo"][r._DELETING]["owner"] == r._PROCESS_ID

    @pytest.mark.asyncio
    async def test_an_entry_whose_creation_changed_is_not_popped(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})

        async def _teardown_then_rekey(*_a, **_kw):
            index = r._load_index()
            index["demo"]["slot_key"] = "spec-builder-demo-ffffffff"
            r._save_index(index)
            return True

        with (
            mock.patch.object(r, "_remove_nudge_loop", mock.AsyncMock()),
            mock.patch.object(r, "_teardown_worker_slot", _teardown_then_rekey),
        ):
            out = await r._handle_delete(
                _mk("DELETE", "specs/demo", state=_State(), match={"name": "demo"})
            )
        assert out.status == 503 and _body(out)["code"] == "index_write_failed"


class TestHandoffUnwindFailureBranches:
    @pytest.mark.asyncio
    async def test_a_loop_that_cannot_be_removed_while_unwinding_is_logged_not_raised(
        self, tmp_path, _armed, _no_dispatch
    ):
        # This is already an abort path -- the reason that brought us here is the
        # story worth surfacing, so the removal stays best-effort HERE only.
        async def _authz_then_delete(**_kw):
            r._save_index({})
            return SimpleNamespace(id="loop-1"), None, 0

        _armed.authz.side_effect = _authz_then_delete
        with (
            _ready_handoff(),
            _no_rehydrate(),
            mock.patch.object(
                r, "_remove_nudge_loop", mock.AsyncMock(side_effect=OSError("store down"))
            ),
        ):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_start"

    @pytest.mark.asyncio
    async def test_an_execution_state_that_cannot_be_cleared_while_unwinding_is_logged(
        self, tmp_path, _armed, _no_dispatch
    ):
        _armed.authz.return_value = (None, "refused", 0)
        real_touch = r._touch_spec
        calls: list[int] = []

        async def _touch(*a, **kw):
            calls.append(1)
            if kw.get("status") == "planning":
                raise OSError("index unwritable")
            return await real_touch(*a, **kw)

        with _ready_handoff(), _no_rehydrate(), mock.patch.object(r, "_touch_spec", _touch):
            out = await r._handle_handoff(_handoff_request(tmp_path, _State()))
        # The refusal the user needs to see survives the failed unwind.
        assert out.status == 403 and _body(out)["code"] == "authorization_refused"

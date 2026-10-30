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

The layers above live in sibling modules under the same isolation:
``test_spec_builder_routes_coverage_workers`` (``_git`` and the worktrees, worker slots,
the transcript relay, discovery), ``test_spec_builder_routes_coverage_http`` (request
plumbing and the handlers up to create) and ``test_spec_builder_routes_coverage_lifecycle``
(the handlers for an existing spec, route registration, the failure branches). Their
shared state pins, fakes and request builders are in
``spec_builder_routes_coverage_helpers``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import spec_builder_routes_coverage_helpers
from spec_builder_routes_coverage_helpers import CRED_NAME, _entry, _write_index

from conftest import requires_symlinks
from kiro_crew.apps.builtins.spec_builder.backend import parsers as parser_module
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_autonudge = spec_builder_routes_coverage_helpers._no_autonudge
_pin_state_dir = spec_builder_routes_coverage_helpers._pin_state_dir
_quiet_sel = spec_builder_routes_coverage_helpers._quiet_sel


# -- pure predicates and coercions -------------------------------------------


class TestNumeric:
    @pytest.mark.parametrize("value", [None, "later", [], {}, object()])
    def test_non_numbers_become_zero(self, value):
        assert r._numeric(value) == 0.0


class TestNames:
    @pytest.mark.parametrize("name", ["a", "demo", "my-spec_1", "A" * 64])
    def test_grammar_accepts(self, name):
        assert r._valid_name(name)

    @pytest.mark.parametrize("name", ["_lead", "dot.name", "sl/ash", "..", "A" * 65])
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
    @pytest.mark.parametrize(
        "key",
        [
            "spec-builder-demo-0123ABCD",  # uppercase suffix
            "spec-builder-demo-",  # empty suffix
            "demo",  # no prefix
        ],
    )
    def test_foreign_or_malformed_keys_are_not_owned(self, key):
        assert not r._owns_slot_key("demo", key)


class TestSlotKeyResolution:
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


class TestNormalizeSpecState:
    """``.spec-state.json`` is LLM output, so every field is treated as hostile."""

    @pytest.mark.parametrize("raw", ["text", 3])
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
                # This backend's field, never the agent's: normalization always
                # reports False and only the recorded-answer overlay sets it.
                "locked": False,
            }
        ]
        assert out["blocking"] == "waiting on review"
        assert out["context"] == {"template": "issue_radar"}

    @pytest.mark.parametrize(
        "decisions",
        [
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

    def test_an_unparseable_file_reads_as_the_default(self):
        r._settings_path().parent.mkdir(parents=True, exist_ok=True)
        r._settings_path().write_text("not json")
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
        # that would not match the directory the entry points at.
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


class TestResolveSpecDir:
    def test_the_default_is_the_kiro_standard_location(self, tmp_path):
        assert (
            r._resolve_spec_dir(str(tmp_path), "demo")
            == (tmp_path / ".kiro" / "specs" / "demo").resolve()
        )

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
        assert r._scan_subdirs(str(tmp_path)) == [{"name": "src", "path": str(tmp_path / "src")}]

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
        with mock.patch.object(r, "safe_read_file_bytes_nolink", side_effect=RuntimeError("boom")):
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
        assert meta == {
            "docs": {},
            "tasks": [],
            "task_progress": {"done": 0, "total": 0},
            "decision_recovery_pending": False,
        }

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
        # is_file() FOLLOWS the link, so a naive gate passes and the autonomous
        # run edits a file outside the spec directory.
        ready, _sentinel = r._prepare_handoff(spec)
        assert ready is False


# -- prompts ------------------------------------------------------------------


class TestSeedPrompt:
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
            spec = importlib.util.spec_from_file_location("_sb_nosec", parser_module.__file__)
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
        with mock.patch.object(r, "is_sensitive_path", module.is_sensitive_path):
            assert r._safe_dir(str(tmp_path)) is None

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
        # A message landing mid-delete must not stamp the doomed entry and return
        # non-None, which every caller reads as "the spec is live".
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
        assert not await r._mark_deleting("demo", expect_spec_dir="/elsewhere", expect_slot_key="")
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
        await r._mark_deleting("demo", expect_spec_dir=str(tmp_path / "demo"), expect_slot_key="")
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
            await r._remove_nudge_loop("demo", stop_reason="spec_deleted")
        patcher.svc.remove.assert_awaited_once_with("loop-7", stop_reason="spec_deleted")  # type: ignore[attr-defined]

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

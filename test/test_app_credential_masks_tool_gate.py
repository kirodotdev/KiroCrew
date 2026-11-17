"""The agent tool gate honours the ``credentialMasks`` of INSTALLED app records.

The shipped built-ins' declarations are static rows of the gate
(``security._CREW_SECRET_LEAVES``). An installed tree's, a retained record's and an
adopted former built-in's are listed by the gate itself
(``credential_masks.tool_gate_crew_leaves``, ``paths_targets._app_credential_mask_rows``),
so an app that leaves the wheel keeps the file-tool fence its built-in had. These cases
pin: the read and the write gate refuse the declared leaf, beneath it and its publish
artifacts, at both places the data can be; a record that outlives the manifest keeps
the fence; a declaration that cannot be read fails closed over the app's own data only;
an install, an adoption and an uninstall are seen without a restart; the listing is
reused rather than taken per check, an inline caller makes no pool submission, and a
check resolves the path once, an installed mask adding one anchor lookup; and the
negative half, that a declaration cannot reach outside its own data directory nor
widen what the static view lists.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.apps import credential_masks
from kiro_crew.security import paths_targets
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_PREFIXES = (".kiro/crew", ".kirocrew")


@pytest.fixture(name="home")
def home_fixture(tmp_path, monkeypatch):
    """A scratch ``$HOME`` whose ``.kiro/crew`` is the live data home, with a fresh listing."""
    crew = tmp_path / ".kiro" / "crew"
    (crew / "apps").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # ``~`` expands through the variable
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # and through this one on Windows
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setattr(credential_masks, "config_dir", lambda: crew)
    monkeypatch.setattr(security.paths, "_home_targets_cache", {})
    credential_masks.clear_cache()
    paths_targets._APP_CREDENTIAL_MASK_ROWS.reset()
    yield crew
    paths_targets._APP_CREDENTIAL_MASK_ROWS.reset()
    credential_masks.clear_cache()


def _install(home: Path, directory: str, masks: object, *, name: str | None = None) -> Path:
    """Write ``apps/<directory>/app.json`` declaring *masks*; return the app's data dir."""
    app = home / "apps" / directory
    (app / "data").mkdir(parents=True, exist_ok=True)
    manifest = {"name": name or directory, "version": "1.0.0", "credentialMasks": masks}
    (app / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    return app / "data"


def _fenced_both_ways(path: Path | str) -> bool:
    read, write = security.is_sensitive_path(str(path)), security.is_sensitive_write_path(str(path))
    assert read == write, f"the read and the write gate disagree on {path}"
    return read


class TestAnInstalledDeclarationIsFenced:
    def test_a_file_mask_its_artifacts_and_its_staged_copy(self, home):
        data = _install(home, "demo", ["secrets.json"])

        assert _fenced_both_ways(data / "secrets.json")
        assert _fenced_both_ways(data / "tmpab12cd.tmp"), "the atomic-write temp beside it"
        assert _fenced_both_ways(data / "secrets.json.lock")
        assert _fenced_both_ways(home / "apps" / ".demo-data-tmp" / "secrets.json")
        assert not _fenced_both_ways(data / "notes.md"), "an undeclared sibling stays open"
        assert not _fenced_both_ways(home / "apps" / "demo" / "app.json")

    def test_a_directory_mask_covers_everything_beneath_it(self, home):
        data = _install(home, "demo", ["secrets/"])

        assert _fenced_both_ways(data / "secrets")
        assert _fenced_both_ways(data / "secrets" / "secrets.json")
        assert _fenced_both_ways(data / "secrets" / "tmpab12cd.tmp")
        assert _fenced_both_ways(data / "index.lock"), "beside the directory, as a built-in's"
        assert not _fenced_both_ways(data / "incidents" / "index.md")

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_at_both_data_home_prefixes(self, home, prefix):
        _install(home, "demo", ["secrets/"])
        assert security.is_sensitive_path(f"~/{prefix}/apps/demo/data/secrets/secrets.json")

    def test_an_adopted_former_built_in_gets_its_installed_declaration(self, home):
        """An installed tree under a shipped built-in's name: the adopted record."""
        builtin = sorted(name for name, _ in credential_masks._builtin_manifests())[0]
        data = _install(home, builtin, ["secrets/"])

        assert _fenced_both_ways(data / "secrets" / "secrets.json")

    def test_a_link_into_the_mask_is_refused(self, home, tmp_path):
        data = _install(home, "demo", ["secrets/"])
        (data / "secrets").mkdir()
        (data / "secrets" / "secrets.json").write_text("{}", encoding="utf-8")
        link = tmp_path / "work" / "innocent"
        link.parent.mkdir()
        make_dir_link(link, data / "secrets")  # a junction on Windows, needing no privilege

        assert _fenced_both_ways(link / "secrets.json")

    def test_a_bulk_root_above_the_mask_contains_it(self, home):
        data = _install(home, "demo", ["secrets/"])
        assert security.path_contains_sensitive(str(data))
        assert not security.path_contains_sensitive(str(data / "incidents"))

    def test_the_static_view_is_unchanged(self, home):
        """``sensitive_home_dirs`` feeds other fences (the ENFORCED harness mask); not widened."""
        before = security.sensitive_home_dirs()
        _install(home, "demo", ["secrets/"])
        assert security.is_sensitive_path(str(home / "apps/demo/data/secrets/s.json"))
        assert security.sensitive_home_dirs() == before


class TestARecordOutlivesTheManifest:
    def test_data_kept_by_an_uninstall_stays_fenced(self, home):
        data = _install(home, "demo", ["secrets/"])
        credential_masks.remember_declaration("demo")
        (home / "apps" / "demo" / "app.json").unlink()

        assert _fenced_both_ways(data / "secrets" / "secrets.json")


class TestADeclarationThatCannotBeReadFailsClosed:
    @pytest.mark.parametrize(
        "body",
        ["{not json", json.dumps({"name": "other", "version": "1.0.0"}), "[]"],
        ids=["unparseable", "another-apps-name", "not-an-object"],
    )
    def test_a_malformed_manifest_fences_only_its_own_data(self, home, body):
        _install(home, "demo", [])
        _install(home, "neighbour", [])
        (home / "apps" / "demo" / "app.json").write_text(body, encoding="utf-8")

        assert _fenced_both_ways(home / "apps" / "demo" / "data" / "anything.json")
        assert _fenced_both_ways(home / "apps" / "demo" / "data" / "tmpab12cd.tmp")
        assert _fenced_both_ways(home / "apps" / ".demo-data-tmp" / "anything.json")
        assert not _fenced_both_ways(home / "apps" / "neighbour" / "data" / "anything.json")
        assert not _fenced_both_ways(home / "apps" / "demo" / "app.json")
        for beside in ("apps/demo/app.json.tmp", "apps/demo/x.lock", "apps/x.tmp", "apps/.a.lock"):
            assert not _fenced_both_ways(home / beside), f"{beside} is not the app's data"

    def test_an_invalid_entry_fences_its_own_data(self, home):
        _install(home, "demo", ["ok.json", "../escape.json"])
        assert _fenced_both_ways(home / "apps" / "demo" / "data" / "notes.md")

    def test_a_corrupt_record_fences_its_own_data(self, home):
        _install(home, "demo", ["secrets/"])
        credential_masks.remember_declaration("demo")
        (home / "apps" / "demo" / "app.json").unlink()
        credential_masks.record_path("demo").write_text("{", encoding="utf-8")

        assert _fenced_both_ways(home / "apps" / "demo" / "data" / "anything.json")

    def test_an_unlistable_apps_dir_fences_apps(self, home):
        (home / "apps").rmdir()
        (home / "apps").write_text("", encoding="utf-8")  # scandir raises NotADirectoryError

        assert credential_masks.tool_gate_crew_leaves() == ((), ("apps",))
        assert _fenced_both_ways(home / "apps" / "x" / "data" / "y.json")
        assert not _fenced_both_ways(home / "notes.md")

    def test_a_missing_apps_dir_declares_nothing(self, home):
        (home / "apps").rmdir()
        assert credential_masks.tool_gate_crew_leaves() == ((), ())


class TestADeclarationCannotReachOutsideItsDataDir:
    def test_no_spelling_names_another_apps_data_or_the_home(self, home):
        """The negative case: the only leaves are under the declaring app's own data."""
        victim = _install(home, "victim", [])
        (victim / "secret.json").write_text("{}", encoding="utf-8")
        _install(home, "evil", ["../../victim/data/secret.json", "/etc/passwd", "../../../x"])
        _install(home, "impostor", ["secret.json"], name="victim")
        _install(home, "honest", ["mine.json"])

        assert credential_masks.tool_gate_crew_leaves() == credential_masks.ToolGateLeaves(
            declared=("apps/.honest-data-tmp/mine.json", "apps/honest/data/mine.json"),
            unreadable=(
                "apps/.evil-data-tmp",
                "apps/.impostor-data-tmp",
                "apps/evil/data",
                "apps/impostor/data",
            ),
        )
        assert not _fenced_both_ways(victim / "secret.json")
        assert not _fenced_both_ways(home / "x")
        assert not security.is_sensitive_path(str(home / "apps" / "victim" / "app.json"))

    def test_rows_are_only_ever_data_home_rows(self, home):
        _install(home, "demo", ["secrets/", "a/b.json"])
        rows = [row for listed in credential_masks.tool_gate_home_rows() for row in listed]
        assert rows and all(
            row.startswith(tuple(f"{prefix}/apps/" for prefix in _PREFIXES)) for row in rows
        )


class TestTheListingIsCurrentAndCheap:
    @pytest.fixture(autouse=True)
    def _counted(self, home, _floor_monkeypatch):
        self.now = 1000.0
        _floor_monkeypatch.setattr(paths_targets, "_app_rows_clock", lambda: self.now)
        listed = paths_targets._list_app_credential_mask_rows
        self.threads: list[int] = []

        def listing():
            self.threads.append(threading.get_ident())
            return listed()

        _floor_monkeypatch.setattr(paths_targets, "_list_app_credential_mask_rows", listing)

    def test_a_recorded_install_and_uninstall_are_seen_at_once(self, home):
        inside = home / "apps" / "late" / "data" / "secrets" / "s.json"
        assert not security.is_sensitive_path(str(inside))

        _install(home, "late", ["secrets/"])
        credential_masks.remember_declaration("late")  # what recording the install does
        assert security.is_sensitive_path(str(inside)), "no restart and no expiry needed"

        for leftover in (home / "apps" / "late" / "app.json", credential_masks.record_path("late")):
            leftover.unlink()
        credential_masks.remember_declaration("late")
        assert not security.is_sensitive_path(str(inside))

    def test_an_unrecorded_change_is_seen_once_the_listing_expires(self, home):
        inside = home / "apps" / "late" / "data" / "secrets" / "s.json"
        assert not security.is_sensitive_path(str(inside))
        _install(home, "late", ["secrets/"])
        self.now += paths_targets._APP_ROWS_TTL_SECS
        assert security.is_sensitive_path(str(inside))

    def test_one_listing_serves_many_checks_on_and_off_the_loop(self, home):
        data = _install(home, "demo", ["secrets/"])
        paths = [str(data / "secrets" / f"{n}.json") for n in range(20)]

        async def on_the_loop():
            return [security.is_sensitive_path(p) for p in paths]

        assert all(security.is_sensitive_path(p) for p in paths)
        assert all(asyncio.run(on_the_loop()))
        assert len(self.threads) == 1
        assert self.threads[0] != threading.get_ident(), "listed on the bounded pool"

    def test_an_inline_caller_lists_on_its_own_thread(self, home):
        data = _install(home, "demo", ["secrets/"])
        real = os.path.realpath(data / "secrets" / "s.json")

        assert security.is_sensitive_resolved_path(real)
        assert self.threads == [threading.get_ident()], "no pool submission"

    def test_a_check_never_joins_a_listing_begun_before_a_recorded_change(self, home, monkeypatch):
        """A listing that read the apps before an install is not what a later check gets."""
        inside = str(home / "apps" / "late" / "data" / "secrets" / "s.json")
        listed, release = threading.Event(), threading.Event()
        counted = paths_targets._list_app_credential_mask_rows

        def first_one_held():
            rows = counted()
            if not listed.is_set():  # the first listing has read apps/ and then waits
                listed.set()
                assert release.wait(FALLBACK_TIMEOUT_SECS)
            return rows

        monkeypatch.setattr(paths_targets, "_list_app_credential_mask_rows", first_one_held)
        earlier: list[str | None] = []
        reader = threading.Thread(
            target=lambda: earlier.append(security.sensitive_path_refusal(inside))
        )
        reader.start()
        try:
            assert listed.wait(FALLBACK_TIMEOUT_SECS)
            _install(home, "late", ["secrets/"])
            credential_masks.remember_declaration("late")

            refusal = security.sensitive_path_refusal(inside)
        finally:
            release.set()
            reader.join(FALLBACK_TIMEOUT_SECS)
        assert not reader.is_alive()
        # Refused on a fresh listing's rows, not as a stall spent waiting on the held one.
        assert refusal and not security.is_unverifiable_path_refusal(refusal)
        assert earlier and earlier[0], "the held listing's answer was discarded too"
        assert not security.is_unverifiable_path_refusal(earlier[0])

    def test_a_listing_a_recorded_change_overtook_is_taken_again(self, home, monkeypatch):
        inside = str(home / "apps" / "late" / "data" / "secrets" / "s.json")
        counted = paths_targets._list_app_credential_mask_rows

        def overtaken_once():
            rows = counted()
            if len(self.threads) == 1:  # an install is recorded while the first listing runs
                _install(home, "late", ["secrets/"])
                credential_masks.remember_declaration("late")
            return rows

        monkeypatch.setattr(paths_targets, "_list_app_credential_mask_rows", overtaken_once)

        assert security.sensitive_path_refusal(inside)
        assert len(self.threads) == 2

    def test_a_listing_overtaken_twice_refuses_as_a_stall(self, home, monkeypatch):
        counted = paths_targets._list_app_credential_mask_rows

        def always_overtaken():
            rows = counted()
            credential_masks.remember_declaration("demo")  # bumps the generation every time
            return rows

        monkeypatch.setattr(paths_targets, "_list_app_credential_mask_rows", always_overtaken)
        refusal = security.sensitive_path_refusal(str(home / "apps" / "demo" / "data" / "a.md"))

        assert refusal and security.is_unverifiable_path_refusal(refusal)
        assert len(self.threads) == 2

    def test_a_pooled_listing_lists_only_the_home_it_was_taken_for(self, home):
        """A listing queued under one ``KIROCREW_HOME`` never lists the home set after it."""
        called: list[int] = []
        with pytest.raises(security.PathResolutionStalled):
            paths_targets._list_for_home(lambda: called.append(1), str(home.parent / "other"))
        assert not called
        assert paths_targets._list_for_home(lambda: "rows", str(home)) == "rows"


class TestWhatAMaskCostsACheck:
    """The path is resolved once per check, and an installed mask adds one anchor lookup."""

    _GATES = (
        security.sensitive_path_refusal,
        security.sensitive_write_path_refusal,
        security.is_sensitive_resolved_path,
    )

    @pytest.fixture(autouse=True)
    def _counted(self, _floor_monkeypatch):
        self.calls: list[str] = []
        for name in ("_candidate_forms", "_home_dir_targets"):
            real = getattr(security.paths, name)

            def counted(*args, _real=real, _name=name, **kwargs):
                self.calls.append(_name)
                return _real(*args, **kwargs)

            _floor_monkeypatch.setattr(security.paths, name, counted)

    def _costs(self, path: Path) -> list[tuple[int, int]]:
        """``(path resolutions, anchor lookups)`` of one check by each gate."""
        costs = []
        for gate in self._GATES:
            self.calls.clear()
            assert not gate(str(path))
            costs.append(
                (self.calls.count("_candidate_forms"), self.calls.count("_home_dir_targets"))
            )
        return costs

    def test_an_installed_mask_adds_one_lookup_and_no_resolution(self, home):
        workspace = home.parent.parent / "work" / "file.py"
        bare = self._costs(workspace)
        data = _install(home, "demo", ["state/", "token.json"])
        credential_masks.remember_declaration("demo")
        assert _fenced_both_ways(data / "state" / "s.json")

        assert self._costs(workspace) == [(paths, lookups + 1) for paths, lookups in bare]
        assert bare[0] == (1, 2), "the read gate resolves the path once, for every list"

    def test_a_built_ins_own_record_costs_nothing(self, home):
        """A built-in's record repeats its shipped declaration, which is static rows already."""
        declaring = [
            (name, path)
            for name, path in credential_masks._builtin_manifests()
            if credential_masks._read_entries(path, name)
        ]
        if not declaring:
            pytest.skip("no shipped built-in declares credentialMasks")
        workspace = home.parent.parent / "work" / "file.py"
        bare = self._costs(workspace)
        name, manifest = declaring[0]
        (home / "apps" / name / "data").mkdir(parents=True)
        shutil.copy(manifest, home / "apps" / name / "app.json")
        credential_masks.remember_declaration(name)

        assert credential_masks.tool_gate_crew_leaves().declared, "the record declares masks"
        assert paths_targets._list_app_credential_mask_rows() == ((), ())
        assert self._costs(workspace) == bare

    def test_the_listed_lists_keep_their_tier_names(self, home):
        """The expiry-pin line names the list an installed mask adds."""
        _install(home, "demo", ["state/"])
        rows, parents = paths_targets._app_credential_mask_rows()
        assert rows and parents
        artifacts = security.paths._KEYSTONE_ARTIFACT_PARENTS + list(parents)
        assert paths_targets._home_targets_tier(list(rows)) == "app-credential-masks"
        assert (
            paths_targets._home_targets_tier(artifacts)
            == "keystone-artifact + app-credential-masks"
        )


class TestAWedgedListingRefuses:
    def test_the_read_gate_refuses_as_a_stall(self, home, monkeypatch):
        inside = str(_install(home, "demo", ["secrets/"]) / "secrets" / "s.json")
        sibling = str(home / "apps" / "demo" / "data" / "notes.md")
        monkeypatch.setattr(
            paths_targets,
            "_APP_ROWS_BUDGET_SECS",
            0.05,  # flake-ok: the listing is held until the test ends, so it always runs out
        )
        gate = threading.Event()
        listed = paths_targets._list_app_credential_mask_rows

        def held():
            assert gate.wait(FALLBACK_TIMEOUT_SECS)
            return listed()

        monkeypatch.setattr(paths_targets, "_list_app_credential_mask_rows", held)
        try:
            refusals = [security.sensitive_path_refusal(p) for p in (inside, sibling)]
        finally:
            gate.set()
        pending = paths_targets._APP_CREDENTIAL_MASK_ROWS.pending
        if pending is not None:
            pending[2].result(FALLBACK_TIMEOUT_SECS)
        assert all(r and security.is_unverifiable_path_refusal(r) for r in refusals)

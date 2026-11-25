"""A whole-install export carries each roster Bot's own stores (``portability_bot_export``).

What rides: ``members/<slug>/`` through the ``carried_member_file`` allowlist, and the
data-home ``artifacts/<P>/`` package that Bot's dashboard pins, archived as
``bot-artifacts/<P>/``, minus what ``_keep_for_export`` drops. What never rides: ``member-records/``, ``crew-panels/`` and the crewmate files
under the Kiro home; the manifest names which Bots had rules, goals or a customised
crewmate file instead. A memory-only export is byte-identical with or without any of it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import portability_helpers
import pytest

from conftest import make_dir_link
from kiro_crew import platform_compat, portability, portability_bot_export, portability_stream
from kiro_crew.config import paths as paths_mod
from kiro_crew.mate_config import minimal_mate_spec
from kiro_crew.portability import create_export_zip
from kiro_crew.testing.clock import ManualClock

fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir

DIGEST = "0123456789abcdef0123456789abcdef"

#: Every file the allowlist admits below ``members/<slug>/``.
CARRIED = {
    "dashboard/instance.json",
    "dashboard/history.json",
    "dashboard/versions/1.json",
    "dashboard/versions/12.json",
    "briefing.md",
    "welcome_claimed.json",
}

#: Host state and Bot-written history that must stay behind.
NOT_CARRIED = {
    "activity.jsonl",
    "dm.json",
    "welcome_owed.json",
    "create_request.json",
    "dashboard/.lock",
    "dashboard/preview.json",
    "dashboard/versions/x.json",
    "notes/scratch.md",
}


@pytest.fixture
def mates_dir(tmp_path, monkeypatch):
    mates = tmp_path / "kiro" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "_mate_agents_dir_override", lambda: mates)
    return mates


def _write_roster(mc: Path, agents: dict[str, dict]) -> None:
    config = json.loads((mc / "config.json").read_text())
    config["agents"] = agents
    (mc / "config.json").write_text(json.dumps(config, indent=2))


def _bot(name: str, slug: str) -> dict:
    return {"member_id": slug, "kiro_agent": name}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _member_store(mc: Path, slug: str, package: str | None) -> None:
    base = mc / "members" / slug
    for rel in CARRIED | NOT_CARRIED:
        _write(base / rel, f"{slug}:{rel}")
    record: dict = {"state": "ok"}
    if package is not None:
        record["package"] = {"slug": package, "version": 1, "digest": DIGEST}
    _write(base / "dashboard" / "instance.json", json.dumps(record))


def _artifact(mc: Path, package: str) -> None:
    base = mc / "artifacts" / package
    _write(base / "current.html", "<p>page</p>")
    _write(base / "versions.json", json.dumps([{"version": 1}]))
    _write(base / "versions" / "1.html", "<p>page v1</p>")
    # Dropped by ``_keep_for_export``: a credential basename and a pid file.
    _write(base / ".env", "TOKEN=x")
    _write(base / "kiro_pids.txt", "1:2\n")


def _names(zip_bytes: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.namelist()


def _under(names: list[str], top: str) -> set[str]:
    out = set()
    for n in names:
        parts = n.split("/", 1)[1]
        if parts.startswith(top + "/"):
            out.add(parts[len(top) + 1 :])
    return out


class TestCarriedStores:
    def test_members_carry_exactly_the_allowlist(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", None)

        zip_bytes, manifest = create_export_zip()

        assert _under(_names(zip_bytes), "bot-members/atlas") == CARRIED
        assert manifest["contents"]["bot_store_files"] == len(CARRIED)
        assert manifest["bot_stores"] == 1

    def test_only_the_dashboard_versions_folder_is_listed(
        self, patched_config_dir, mates_dir, monkeypatch
    ):
        # A40: the rest of members/<slug>/ (host state, Bot-written history, however
        # large) is never listed; the carried single files are opened by name.
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", None)
        listed: list[str] = []
        real_scandir = os.scandir

        def scandir(path):
            with contextlib.suppress(TypeError, ValueError):
                listed.append(Path(path).relative_to(mc).as_posix())
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)

        files = list(portability_bot_export.iter_bot_store_files(str(mc), [("Atlas", "atlas")]))
        try:
            assert {rel.as_posix() for rel, _fd in files} == {
                f"bot-members/atlas/{r}" for r in CARRIED
            }
        finally:
            for _rel, fd in files:
                os.close(fd)
        assert [p for p in listed if p.startswith("members/")] == [
            "members/atlas/dashboard/versions"
        ]

    def test_a_member_directory_outside_the_roster_is_not_carried(
        self, patched_config_dir, mates_dir
    ):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", None)
        _member_store(mc, "stray", None)

        names = _names(create_export_zip()[0])

        assert _under(names, "bot-members/stray") == set()

    def test_the_pinned_package_rides_minus_export_drops(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")
        _artifact(mc, "library-page")

        zip_bytes, manifest = create_export_zip()
        names = _names(zip_bytes)

        assert _under(names, "bot-artifacts/atlas-dash") == {
            "current.html",
            "versions.json",
            "versions/1.html",
        }
        assert _under(names, "bot-artifacts/library-page") == set()
        assert manifest["contents"]["bot_store_files"] == len(CARRIED) + 3

    def test_a_record_over_the_page_cap_still_carries_its_package(
        self, patched_config_dir, mates_dir
    ):
        # Between the page cap (3 MiB) and the record cap the import reads with
        # (8 MiB): the export must carry the package the import will look for (A22).
        from kiro_crew.dashboard_frame import MAX_PAGE_BYTES
        from kiro_crew.portability_bot_types import MAX_RECORD_BYTES

        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")
        record_path = mc / "members" / "atlas" / "dashboard" / "instance.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["pad"] = "x" * (MAX_PAGE_BYTES + 1024 * 1024)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        assert MAX_PAGE_BYTES < record_path.stat().st_size < MAX_RECORD_BYTES

        names = _names(create_export_zip()[0])

        assert _under(names, "bot-artifacts/atlas-dash") == {
            "current.html",
            "versions.json",
            "versions/1.html",
        }

    def test_two_bots_pinning_one_package_carry_it_once(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas"), "Bea": _bot("Bea", "bea")})
        _member_store(mc, "atlas", "shared-dash")
        _member_store(mc, "bea", "shared-dash")
        _artifact(mc, "shared-dash")

        names = _names(create_export_zip()[0])

        assert len(names) == len(set(names))
        assert _under(names, "bot-artifacts/shared-dash") == {
            "current.html",
            "versions.json",
            "versions/1.html",
        }

    @pytest.mark.parametrize(
        "pointer",
        [
            None,  # released
            {"slug": "../members", "version": 1, "digest": DIGEST},
            {"slug": "Atlas-Dash", "version": 1, "digest": DIGEST},
            {"slug": "atlas-dash", "version": 1, "digest": "short"},
        ],
    )
    def test_a_released_or_malformed_pin_carries_no_artifact(
        self, patched_config_dir, mates_dir, pointer
    ):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", None)
        _write(mc / "members/atlas/dashboard/instance.json", json.dumps({"package": pointer}))
        _artifact(mc, "atlas-dash")

        names = _names(create_export_zip()[0])

        assert not [n for n in names if "artifacts/" in n]
        assert _under(names, "bot-members/atlas") == CARRIED

    def test_nothing_under_member_records_crew_panels_or_the_kiro_home(
        self, patched_config_dir, mates_dir
    ):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")
        _write(mc / "member-records/member-bindings/atlas.json", "{}")
        _write(mc / "member-records/member-rules/atlas.json", "{}")
        _write(mc / f"crew-panels/atlas.dashboard-page.{DIGEST}.json", "{}")
        _write(mc / "crew-panels/atlas.json", "{}")
        (mates_dir / "Atlas.json").write_text(json.dumps(minimal_mate_spec("Atlas")))

        names = _names(create_export_zip()[0])

        tails = [n.split("/", 1)[1] for n in names]
        assert not [t for t in tails if t.startswith(("member-records/", "crew-panels/"))]
        assert not [t for t in tails if t.endswith("Atlas.json") or "mates/" in t]

    def test_a_linked_member_directory_is_not_followed(
        self, patched_config_dir, mates_dir, tmp_path
    ):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        outside = tmp_path / "outside"
        _write(outside / "dashboard" / "instance.json", "{}")
        _write(outside / "briefing.md", "planted")
        (mc / "members").mkdir()
        make_dir_link(mc / "members" / "atlas", outside)

        zip_bytes, manifest = create_export_zip()

        assert _under(_names(zip_bytes), "bot-members/atlas") == set()
        assert manifest["contents"]["bot_store_files"] == 0

    def test_a_linked_pinned_package_is_not_followed(self, patched_config_dir, mates_dir, tmp_path):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        outside = tmp_path / "outside-artifact"
        _write(outside / "current.html", "planted")
        (mc / "artifacts").mkdir()
        make_dir_link(mc / "artifacts" / "atlas-dash", outside)

        names = _names(create_export_zip()[0])

        assert not [n for n in names if "artifacts/" in n]


class TestFootprint:
    def test_footprint_counts_exactly_what_is_written(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")

        def _measure() -> tuple[int, int]:
            return portability_stream._measure_nonsession_footprint(
                os.path.realpath(mc), memory_only=False, include_agent_templates=False
            )

        def _written() -> tuple[int, int]:
            with zipfile.ZipFile(io.BytesIO(create_export_zip()[0])) as zf:
                infos = [i for i in zf.infolist() if not i.filename.endswith("/MANIFEST.json")]
            return len(infos), sum(i.file_size for i in infos)

        # The export checkpoints the databases' WAL first, which changes their logical
        # size; the streaming planner runs that checkpoint before it measures, too.
        create_export_zip()
        with_bots = (_measure(), _written())
        shutil.rmtree(mc / "members")
        shutil.rmtree(mc / "artifacts")
        without_bots = (_measure(), _written())

        assert with_bots[0] == with_bots[1]
        assert without_bots[0] == without_bots[1]
        assert with_bots[0][0] - without_bots[0][0] == len(CARRIED) + 3


class TestManifestLists:
    def test_records_are_named_by_kind_without_being_read(
        self, patched_config_dir, mates_dir, monkeypatch
    ):
        mc = patched_config_dir
        _write_roster(
            mc,
            {"Atlas": _bot("Atlas", "atlas"), "Bea": _bot("Bea", "bea"), "Cy": _bot("Cy", "cy")},
        )
        _write(mc / "member-records/member-rules/atlas.json", "not json at all")
        _write(mc / "member-records/member-goals/atlas.json", "{}")
        _write(mc / "member-records/member-goals/bea.json", "{}")
        _write(mc / "member-records/member-rules/ghost.json", "{}")
        _write(mc / "member-records/member-bindings/cy.json", "{}")

        opened: list[str] = []
        real_open = os.open

        def _spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", _spy)
        records, more = portability_bot_export.member_records_not_carried(
            [("Atlas", "atlas"), ("Bea", "bea"), ("Cy", "cy")]
        )

        assert records == {"atlas": ["member-rules", "member-goals"], "bea": ["member-goals"]}
        assert more == 0
        assert not [p for p in opened if "member-records" in p]

    def test_customised_and_unreadable_crewmate_files_are_listed(
        self, patched_config_dir, mates_dir
    ):
        bots = [
            ("Minimal", "minimal"),
            ("Absent", "absent"),
            ("Tooled", "tooled"),
            ("Broken", "broken"),
            ("Huge", "huge"),
            ("Linked", "linked"),
            ("Folder", "folder"),
        ]
        (mates_dir / "Minimal.json").write_text(json.dumps(minimal_mate_spec("Minimal")))
        tooled = minimal_mate_spec("Tooled")
        tooled["tools"] = ["shell"]
        (mates_dir / "Tooled.json").write_text(json.dumps(tooled))
        (mates_dir / "Broken.json").write_text("{not json")
        huge = minimal_mate_spec("Huge")
        huge["prompt"] = "x" * (portability_bot_export.MAX_CREWMATE_FILE_BYTES + 1)
        (mates_dir / "Huge.json").write_text(json.dumps(huge))
        target = mates_dir.parent / "elsewhere.json"
        target.write_text(json.dumps(minimal_mate_spec("Linked")))
        try:
            os.symlink(target, mates_dir / "Linked.json")
        except OSError:
            pytest.skip("this host cannot create a file symlink")
        (mates_dir / "Folder.json").mkdir()

        names, more = portability_bot_export.crewmate_files_customised(bots)

        assert names == ["Broken", "Folder", "Huge", "Linked", "Tooled"]
        assert more == 0

    def test_a_file_written_for_another_name_is_customised(self, patched_config_dir, mates_dir):
        (mates_dir / "Atlas.json").write_text(json.dumps(minimal_mate_spec("Other")))

        assert portability_bot_export.crewmate_files_customised([("Atlas", "atlas")]) == (
            ["Atlas"],
            0,
        )

    @pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "nul\0name", "back\\slash"])
    def test_an_unsafe_name_is_never_opened(self, patched_config_dir, mates_dir, name):
        _write(mates_dir.parent / "escape.json", "{not json")

        assert portability_bot_export.crewmate_files_customised([(name, "x")]) == ([], 0)

    def test_manifest_keys_and_both_caps(self, patched_config_dir, mates_dir, monkeypatch):
        mc = patched_config_dir
        monkeypatch.setattr(portability_bot_export, "MAX_LISTED_BOTS", 2)
        agents = {}
        for name in ("Dee", "Cy", "Bea", "Atlas"):
            slug = name.lower()
            agents[name] = _bot(name, slug)
            _write(mc / f"member-records/member-rules/{slug}.json", "{}")
            spec = minimal_mate_spec(name)
            spec["skills"] = ["extra"]
            (mates_dir / f"{name}.json").write_text(json.dumps(spec))
        _write_roster(mc, agents)

        manifest = create_export_zip()[1]

        assert manifest["bot_stores"] == 1
        assert manifest["contents"]["bot_store_files"] == 0
        assert manifest["member_records_not_carried"] == {
            "atlas": ["member-rules"],
            "bea": ["member-rules"],
        }
        assert manifest["member_records_not_carried_more"] == 2
        assert manifest["crewmate_files_customised"] == ["Atlas", "Bea"]
        assert manifest["crewmate_files_customised_more"] == 2

    def test_uncapped_lists_carry_no_more_keys(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        (mates_dir / "Atlas.json").write_text(json.dumps(minimal_mate_spec("Atlas")))

        manifest = create_export_zip()[1]

        assert manifest["member_records_not_carried"] == {}
        assert manifest["crewmate_files_customised"] == []
        assert "member_records_not_carried_more" not in manifest
        assert "crewmate_files_customised_more" not in manifest

    def test_the_streaming_export_writes_the_same_bot_members(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")

        chunks: list[bytes] = []
        plan = portability.plan_export(include_sessions=True)
        manifest = portability.stream_export_zip(chunks.append, plan)

        names = _names(b"".join(chunks))
        assert _under(names, "bot-members/atlas") == CARRIED
        assert manifest["contents"]["bot_store_files"] == len(CARRIED) + 3


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return datetime(2026, 11, 15, 12, 0, 0, tzinfo=tz or timezone.utc)


class TestMemoryOnlyUnchanged:
    def test_memory_only_is_byte_identical_and_does_no_bot_work(
        self, patched_config_dir, mates_dir, monkeypatch
    ):
        mc = patched_config_dir
        monkeypatch.setattr(portability, "datetime", _FrozenDatetime)
        frozen = _FrozenDatetime.now().timestamp()
        ManualClock(frozen).install(monkeypatch, portability_stream)

        def _archive() -> tuple[dict[str, bytes], dict]:
            zip_bytes, manifest = create_export_zip(memory_only=True)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                return {n: zf.read(n) for n in zf.namelist()}, manifest

        # The base path: a home with no Bot and no Bot store.
        base_members, base_manifest = _archive()

        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", "atlas-dash")
        _artifact(mc, "atlas-dash")
        _write(mc / "member-records/member-rules/atlas.json", "{}")
        (mates_dir / "Atlas.json").write_text("{customised")

        def _no_bot_work(*_args, **_kwargs):
            raise AssertionError("a memory-only export did Bot work")

        for fn in (
            "load_roster",
            "iter_bot_store_files",
            "bot_manifest_fields",
            "member_records_not_carried",
            "crewmate_files_customised",
        ):
            monkeypatch.setattr(portability_bot_export, fn, _no_bot_work)

        new_members, new_manifest = _archive()

        assert sorted(new_members) == sorted(base_members)
        assert new_members == base_members
        assert new_manifest == base_manifest
        assert "bot_stores" not in new_manifest
        assert "bot_store_files" not in new_manifest["contents"]


class TestRoster:
    def test_raw_config_slugs_follow_member_slug(self):
        doc = {
            "agents": {
                "Atlas": {"member_id": "atlas-7"},
                "Old Timer": {},
                "Bad": {"member_id": "../x"},
                "Twin": {"member_id": "atlas-7"},
            }
        }

        assert portability_bot_export.roster(doc) == [
            ("Atlas", "atlas-7"),
            ("Old Timer", "old-timer"),
        ]

    def test_the_reserved_default_agent_is_no_bot(self):
        doc = {"agents": {"default": {}, "Atlas": {"member_id": "atlas"}}}

        assert portability_bot_export.roster(doc) == [("Atlas", "atlas")]

    def test_export_bots_reads_the_roster_and_its_fields_once(
        self, patched_config_dir, mates_dir, monkeypatch
    ):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        reads: list[str] = []
        real = portability_bot_export.read_config_document

        def counted(mc_real):
            reads.append(mc_real)
            return real(mc_real)

        monkeypatch.setattr(portability_bot_export, "read_config_document", counted)
        bots = portability_bot_export.export_bots(str(mc))

        assert reads == [str(mc)]
        assert bots.roster == (("Atlas", "atlas"),)
        assert bots.fields == portability_bot_export.bot_manifest_fields([("Atlas", "atlas")])

    def test_a_loaded_config_goes_through_member_slug(self, patched_config_dir):
        from kiro_crew.config.loader import KiroCrewConfig

        _write_roster(patched_config_dir, {"Atlas": _bot("Atlas", "atlas-7")})

        assert portability_bot_export.roster(KiroCrewConfig.load()) == [("Atlas", "atlas-7")]

    @pytest.mark.parametrize(
        "body",
        ["{not json", "[]", '{"agents": []}', '{"agents": ' + "[" * 100_000 + "]" * 100_000 + "}"],
        ids=["not-json", "not-an-object", "agents-not-an-object", "nested-past-the-decoder-limit"],
    )
    def test_an_unreadable_config_means_no_roster(self, tmp_path, body):
        (tmp_path / "config.json").write_text(body)

        assert portability_bot_export.load_roster(str(tmp_path)) == []

    def test_the_export_never_rewrites_config_json(self, patched_config_dir, mates_dir):
        mc = patched_config_dir
        _write_roster(mc, {"Atlas": _bot("Atlas", "atlas")})
        _member_store(mc, "atlas", None)
        before = (mc / "config.json").read_bytes()

        create_export_zip()
        portability.plan_export(include_sessions=True)

        assert (mc / "config.json").read_bytes() == before


# The carried package of a Bot whose dashboard runs its template: real Bots on a real
# artifact store, exported through create_export_zip.


@pytest.fixture
def homes(tmp_path, _floor_monkeypatch):
    from portability_bot_helpers import Homes, install_fixture_catalog

    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


def _bot_artifacts(archive: Path) -> set[str]:
    from portability_bot_helpers import archive_members

    return {
        rel.split("/")[1] for rel in archive_members(archive) if rel.startswith("bot-artifacts/")
    }


def _atlas_on_its_template():
    """Atlas applies package ``atlas-board``, then rolls back to its template version."""
    from portability_bot_helpers import apply_dashboard, create_bot

    from kiro_crew.dashboard_templates import instance

    atlas = create_bot("Atlas")
    applied = apply_dashboard(atlas, '<p data-dashboard-field="goal">board</p>')
    assert not instance.rollback(atlas.slug, 1).package_slug
    return atlas, applied


def _rebind_away(package: str, slug: str) -> None:
    """Rebind *package* to Bot *slug* the ordinary way: same layout, no new version."""
    from portability_bot_helpers import dashboard_package

    from kiro_crew.artifact_store import dashboard_package as dp
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.config.paths import data_home

    store = ArtifactStore(data_home() / "artifacts")
    before = store.get(package, assign_token=True)
    html = dp._decode_package(before.content or "")["view"]["html"]
    store.update(
        package,
        content=dp.dump_package(dashboard_package(slug, html)),
        expected_token=before.content_token,
    )
    assert store.get(package).version == before.version


def test_a_bot_on_its_template_carries_the_package_its_retained_versions_pin(homes, tmp_path):
    from portability_bot_helpers import export_archive

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == {applied.package}


def test_a_template_bot_does_not_carry_a_package_rebound_to_another_bot(homes, tmp_path):
    from portability_bot_helpers import export_archive

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()
    _rebind_away(applied.package, "other")

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()


def test_the_carried_pin_reads_the_highest_numbered_versions_by_number(homes, tmp_path):
    from portability_bot_helpers import dashboard_package, export_archive

    from kiro_crew.artifact_store import dashboard_package as dp
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.config.paths import data_home
    from kiro_crew.dashboard_templates import instance
    from kiro_crew.portability_bot_types import carried_pin

    homes.use("a")
    atlas, applied = _atlas_on_its_template()
    store = ArtifactStore(data_home() / "artifacts")
    beta = store.create(
        name="beta board",
        content=dp.dump_package(dashboard_package(atlas.slug, "<p>beta</p>")),
        kind=dp.DASHBOARD_KIND,
        source="dashboard",
    )
    assert beta.slug == "beta-board"
    beta_digest = instance._page_digest(
        dp.canonical_package_content(store.get(beta.slug).content or "")
    )
    _rebind_away(applied.package, "other")
    versions = data_home() / "members" / atlas.slug / "dashboard" / "versions"
    pinned = json.loads((versions / "2.json").read_text())
    assert pinned["package"]["slug"] == applied.package
    docs: dict[int, dict] = {}
    for n in range(1, 31):
        doc = dict(pinned)
        if n == 30:
            doc["package"] = {"slug": beta.slug, "version": 1, "digest": beta_digest}
        docs[n] = doc
        (versions / f"{n}.json").write_text(json.dumps(doc))
    record = json.loads((versions.parent / "instance.json").read_text())

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == {beta.slug}
    expected = (beta.slug, 1, beta_digest)
    by_name = {n: docs[n] for n in sorted(docs, key=str)}
    for order in (docs, dict(reversed(list(docs.items()))), by_name):
        assert carried_pin(record, order) == expected
    assert carried_pin(record, {n: docs[n] for n in range(1, 30)}) == (
        applied.package,
        applied.version,
        applied.digest,
    )
    assert carried_pin(pinned, {}) == (applied.package, applied.version, applied.digest)
    assert carried_pin(record, {}) is None


def test_a_non_ascii_digit_name_is_no_version_name() -> None:
    assert portability_bot_export._newest_version_names(["².json", "1.json"]) == {"1.json"}


def test_a_leading_zero_name_is_no_version_name() -> None:
    from kiro_crew.dashboard_templates.instance import MAX_RETAINED_VERSIONS

    canonical = [f"{n}.json" for n in range(1, MAX_RETAINED_VERSIONS + 2)]
    newest = set(canonical[-MAX_RETAINED_VERSIONS:])
    names = canonical + ["07.json", "00.json", f"0{MAX_RETAINED_VERSIONS + 1}.json"]
    assert portability_bot_export._newest_version_names(names) == newest


def test_a_version_file_with_a_leading_zero_is_neither_carried_nor_read_for_the_pin(
    homes, tmp_path
):
    # The import reads version n from "<n>.json" only, so a hand-named "07.json" is no
    # version on either side: it is not archived, not held, and pins no package.
    from portability_bot_helpers import archive_members, export_archive

    from kiro_crew.config.paths import data_home

    homes.use("a")
    atlas, _applied = _atlas_on_its_template()
    versions = data_home() / "members" / atlas.slug / "dashboard" / "versions"
    names = sorted(p.name for p in versions.iterdir())
    # The one version that pins the package, renamed by hand.
    (versions / "2.json").rename(versions / "07.json")
    held: dict[str, int] = {}
    unheld: set[str] = set()
    fd = os.open(versions / "07.json", os.O_RDONLY)
    try:
        portability_bot_export._hold_if_newest(held, unheld, "07.json", fd)
    finally:
        os.close(fd)
        for dup in held.values():
            os.close(dup)
    assert held == {} and unheld == set()

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()
    prefix = f"bot-members/{atlas.slug}/dashboard/versions/"
    carried = {rel[len(prefix) :] for rel in archive_members(archive) if rel.startswith(prefix)}
    assert carried == set(names) - {"2.json"}


def test_the_export_parses_only_the_newest_version_files(homes, monkeypatch):
    from kiro_crew.config.paths import data_home
    from kiro_crew.dashboard_templates.instance import MAX_RETAINED_VERSIONS

    homes.use("a")
    atlas, applied = _atlas_on_its_template()
    versions = data_home() / "members" / atlas.slug / "dashboard" / "versions"
    pinned = (versions / "2.json").read_text()
    for n in range(1, 26):
        (versions / f"{n}.json").write_text(pinned)
    parsed: list[int] = []
    real_read = portability_bot_export._read_record_doc

    def read(fd: int):
        parsed.append(fd)
        return real_read(fd)

    monkeypatch.setattr(portability_bot_export, "_read_record_doc", read)

    files = list(
        portability_bot_export.iter_bot_store_files(
            os.path.realpath(data_home()), [(atlas.name, atlas.slug)]
        )
    )
    try:
        names = {rel.as_posix() for rel, _fd in files}
    finally:
        for _rel, fd in files:
            os.close(fd)

    # instance.json, then the newest MAX_RETAINED_VERSIONS version files and no other.
    assert len(parsed) == 1 + MAX_RETAINED_VERSIONS
    prefix = f"bot-members/{atlas.slug}/dashboard/versions"
    assert {f"{prefix}/{n}.json" for n in range(1, 26)} <= names
    assert {n.split("/")[1] for n in names if n.startswith("bot-artifacts/")} == {applied.package}


# The template Bot's live-binding read: a read of the source, never a write, and one
# failure set with the import's read of the same metadata.


def _package_meta(package: str) -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / "artifacts" / package / "meta.json"


def _edit_meta(package: str, change: dict, drop: tuple[str, ...] = ()) -> None:
    path = _package_meta(package)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(change)
    for key in drop:
        raw.pop(key, None)
    path.write_text(json.dumps(raw), encoding="utf-8")


#: What a record written before the event log existed lacks.
_LEGACY_DROP = ("events", "events_backfilled")


@pytest.mark.parametrize("version", [None, [], {}], ids=["null", "list", "object"])
def test_a_package_version_the_store_cannot_read_carries_nothing_and_the_export_completes(
    homes, tmp_path, version
):
    # records.decode_meta raises TypeError for these: the read answers "not bound".
    from portability_bot_helpers import archive_members, export_archive

    homes.use("a")
    atlas, applied = _atlas_on_its_template()
    _edit_meta(applied.package, {"version": version})

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()
    assert f"bot-members/{atlas.slug}/dashboard/instance.json" in archive_members(archive)


def test_the_live_binding_read_leaves_a_legacy_meta_byte_identical(homes, tmp_path):
    from portability_bot_helpers import export_archive

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()
    _edit_meta(applied.package, {}, drop=_LEGACY_DROP)
    before = _package_meta(applied.package).read_bytes()

    archive = export_archive(tmp_path / "a.zip")

    assert _package_meta(applied.package).read_bytes() == before
    assert _bot_artifacts(archive) == {applied.package}


def test_the_export_does_not_create_a_missing_artifacts_directory(homes, tmp_path):
    from portability_bot_helpers import export_archive

    from kiro_crew.config.paths import data_home

    homes.use("a")
    _atlas_on_its_template()
    shutil.rmtree(data_home() / "artifacts")

    archive = export_archive(tmp_path / "a.zip")

    assert not (data_home() / "artifacts").exists()
    assert _bot_artifacts(archive) == set()


@pytest.mark.skipif(
    not platform_compat.IS_POSIX or platform_compat.effective_uid() == 0,
    reason="needs POSIX permission bits that bind this process (not root)",
)
def test_a_read_only_package_with_a_legacy_meta_is_carried(homes, tmp_path):
    import stat

    from portability_bot_helpers import export_archive

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()
    _edit_meta(applied.package, {}, drop=_LEGACY_DROP)
    meta = _package_meta(applied.package)
    package_dir = meta.parent
    platform_compat.chmod_safe(meta, stat.S_IRUSR)
    platform_compat.chmod_safe(package_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        archive = export_archive(tmp_path / "a.zip")
    finally:
        platform_compat.chmod_safe(package_dir, stat.S_IRWXU)
        platform_compat.chmod_safe(meta, stat.S_IRUSR | stat.S_IWUSR)

    assert _bot_artifacts(archive) == {applied.package}


def test_a_live_pointer_package_names_no_binding_for_a_template_bot(homes, tmp_path):
    # The store's own live-pointer rule, as scan_bound_dashboards and the import read it:
    # a live pointer's bytes are another file's, so it is no Bot's package to carry.
    from portability_bot_helpers import export_archive

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()
    gone = tmp_path / "moved-away.html"
    _edit_meta(applied.package, {"source_path": str(gone), "source_copy_only": False})

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()


def test_a_package_of_another_kind_names_no_binding_for_a_template_bot(homes, tmp_path):
    # scan_bound_dashboards lists only kind="dashboard" records, so a record of another
    # kind whose page is a bound envelope binds no Bot there, and the export carries none.
    from portability_bot_helpers import archive_members, export_archive

    from kiro_crew.artifact_store.dashboard_package import scan_bound_dashboards
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.config.paths import data_home

    homes.use("a")
    atlas, applied = _atlas_on_its_template()
    _edit_meta(applied.package, {"kind": "widget"})
    assert scan_bound_dashboards(ArtifactStore(data_home() / "artifacts")).bound == {}

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()
    assert f"bot-members/{atlas.slug}/dashboard/instance.json" in archive_members(archive)


#: Package metadata the import's staged-bytes gate always refuses, so the export must not
#: carry it: a retained version of another kind, and a copy with provenance (its
#: ``source_path`` is set, which a carried package never sets).
_REFUSED_BY_THE_GATE = {
    "version_kinds": {"version_kinds": {"1": "widget"}},
    "provenance_copy": {"source_path": "elsewhere.html", "source_copy_only": True},
}


@pytest.mark.parametrize(
    "change", list(_REFUSED_BY_THE_GATE.values()), ids=list(_REFUSED_BY_THE_GATE)
)
def test_a_template_bot_does_not_carry_a_package_the_import_would_refuse(homes, tmp_path, change):
    from portability_bot_helpers import archive_members, export_archive

    from kiro_crew.artifact_store.dashboard_package import scan_bound_dashboards
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.config.paths import data_home

    homes.use("a")
    atlas, applied = _atlas_on_its_template()
    if "source_path" in change:
        change = {**change, "source_path": str(tmp_path / change["source_path"])}
    _edit_meta(applied.package, change)
    # Still bound to Atlas by the scan's rules: only the gate's meta rule refuses it.
    assert scan_bound_dashboards(ArtifactStore(data_home() / "artifacts")).bound == {
        f"crewmate:{atlas.slug}": [applied.package]
    }

    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()
    members = archive_members(archive)
    assert f"bot-members/{atlas.slug}/dashboard/instance.json" in members
    assert f"bot-members/{atlas.slug}/dashboard/versions/2.json" in members


def test_the_export_and_the_gate_share_one_package_meta_rule(homes) -> None:
    from kiro_crew.artifacts import read_stored_artifact
    from kiro_crew.config.paths import data_home
    from kiro_crew.portability_bot_stores import is_carriable_dashboard_meta

    homes.use("a")
    _atlas, applied = _atlas_on_its_template()
    root = data_home() / "artifacts"
    assert is_carriable_dashboard_meta(read_stored_artifact(root, applied.package))
    for change in (
        {"kind": "widget"},
        {"version_kinds": {"1": "widget"}},
        {"source_path": "elsewhere.html", "source_copy_only": True},
        {"source_path": "elsewhere.html", "source_copy_only": False},
        {"source_root": "/"},
        {"source_copy_only": True},
    ):
        before = _package_meta(applied.package).read_text(encoding="utf-8")
        _edit_meta(applied.package, change)
        try:
            assert not is_carriable_dashboard_meta(read_stored_artifact(root, applied.package))
        finally:
            _package_meta(applied.package).write_text(before, encoding="utf-8")


class _DupFailsWhileArmed:
    """``os`` for ``portability_bot_export``, whose ``dup`` raises EMFILE while armed."""

    def __init__(self) -> None:
        self.armed = False

    def dup(self, fd: int) -> int:
        if self.armed:
            import errno

            raise OSError(errno.EMFILE, "Too many open files")
        return os.dup(fd)

    def __getattr__(self, name: str):
        return getattr(os, name)


def test_a_newest_version_the_export_cannot_hold_carries_no_template_package(
    homes, tmp_path, monkeypatch
):
    # The import parses every archived version file, so a pin chosen from the rest
    # could name a package the import never uses: the export carries none instead.
    from portability_bot_helpers import (
        apply_dashboard,
        archive_members,
        create_bot,
        export_archive,
    )

    from kiro_crew.dashboard_templates import instance

    homes.use("a")
    atlas = create_bot("Atlas")
    older = apply_dashboard(atlas, '<p data-dashboard-field="goal">older</p>')
    newer = apply_dashboard(atlas, '<p data-dashboard-field="goal">newer</p>')
    assert newer.package != older.package
    assert not instance.rollback(atlas.slug, 1).package_slug
    numbers = [int(name.removesuffix(".json")) for name in instance._list_versions(atlas.slug)]
    newest_pin = max(
        n
        for n in numbers
        if instance._package_field(instance._read_version(atlas.slug, n))[0] == newer.package
    )
    assert _bot_artifacts(export_archive(tmp_path / "whole.zip")) == {newer.package}

    fake_os = _DupFailsWhileArmed()
    real_hold = portability_bot_export._hold_if_newest

    def hold(*args):
        # (held, name, fd), or (held, unheld, name, fd): the name is second to last.
        fake_os.armed = args[-2] == f"{newest_pin}.json"
        try:
            return real_hold(*args)
        finally:
            fake_os.armed = False

    monkeypatch.setattr(portability_bot_export, "os", fake_os)
    monkeypatch.setattr(portability_bot_export, "_hold_if_newest", hold)
    archive = export_archive(tmp_path / "a.zip")

    assert _bot_artifacts(archive) == set()
    members = archive_members(archive)
    assert f"bot-members/{atlas.slug}/dashboard/versions/{newest_pin}.json" in members

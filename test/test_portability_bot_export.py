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
from kiro_crew import portability, portability_bot_export, portability_stream
from kiro_crew.config import paths as paths_mod
from kiro_crew.mate_config import minimal_mate_spec
from kiro_crew.portability import create_export_zip

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

        assert _under(_names(zip_bytes), "members/atlas") == CARRIED
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
            assert {rel.as_posix() for rel, _fd in files} == {f"members/atlas/{r}" for r in CARRIED}
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

        assert _under(names, "members/stray") == set()

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
        assert _under(names, "members/atlas") == CARRIED

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

        assert _under(_names(zip_bytes), "members/atlas") == set()
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
        assert _under(names, "members/atlas") == CARRIED
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
        monkeypatch.setattr(portability_stream.time, "time", lambda: frozen)

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

    @pytest.mark.parametrize("body", ["{not json", "[]", '{"agents": []}'])
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

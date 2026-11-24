"""The instance import's per-Bot orchestration (`kiro_crew.portability_bots`).

The units it calls (``portability_bot_bindings``, ``_crewmates``, ``_roster`` and
``_stores``) are replaced here by recording fakes of their documented interfaces, so
these tests pin the order, the error capture and the summary, not the units' work.
"""

from __future__ import annotations

import io
import json
import types
import zipfile
from pathlib import Path

import pytest

from kiro_crew import portability, portability_bot_bindings, portability_bots
from kiro_crew.portability_bot_types import ImportedBot, PartOutcome
from kiro_crew.snapshot_components import COMPONENTS

PREFIX = "kirocrew-export"


def _bot(name: str, slug: str | None = None) -> ImportedBot:
    slug = slug or name.lower()
    store = f"{slug}-store"
    return ImportedBot(
        name=name,
        slug=slug,
        member_id=slug,
        memory_store=store,
        generation=store,
        slot_key=f"member-{slug}.memory-{store}",
        kiro_agent=name,
    )


def _chat(text: str = "hi") -> bytes:
    header = json.dumps({"_type": "metadata", "title": "chat", "memory_mode": "persistent"})
    row = json.dumps({"role": "user", "content": text, "ts": "2026-01-01T00:00:00"})
    return f"{header}\n{row}\n".encode()


def _archive(tmp_path: Path, members: dict[str, bytes | str], name: str = "a.zip") -> Path:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in members.items():
            zf.writestr(f"{PREFIX}/{rel}", data)
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return path


def _manifest(**extra) -> str:
    return json.dumps({"version": 1, "bot_stores": 1, **extra})


class Units:
    """Recording fakes of the four units, installed as their modules."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, bots: list[ImportedBot]) -> None:
        self.calls: list[str] = []
        self.bots = bots
        self.args: dict[str, object] = {}
        self.raise_in: set[str] = set()
        self.extra: dict[str, list[PartOutcome]] = {}
        self.added: list[str] = [b.name for b in bots]
        self.skipped: list[PartOutcome] = []
        self.kept: list[str] = []
        self.orphans: list[dict] = []
        self.claims: object = {}
        self.destination: object = None
        self.crewmate_status = "written"
        self.refusals: dict[str, str] = {}
        self.archive_stems: set[str] = set()
        self.modules: dict[str, types.ModuleType] = {}
        for unit, funcs in {
            "portability_bot_bindings": (
                "read_archive_binding_claims",
                "rebuild_bindings",
                "orphan_transcripts",
                "member_stem",
                "is_member_stem",
                "binding_ladder",
                "archive_member_stems",
                "sessions_listing",
                "stem_slug",
            ),
            "portability_bot_crewmates": ("restore_crewmate_files", "crewmate_file_notices"),
            "portability_bot_roster": ("merge_roster", "imported_bots"),
            "portability_bot_stores": ("install_bot_stores",),
            "portability_bot_export": ("read_config_document",),
        }.items():
            module = types.ModuleType(f"kiro_crew.{unit}")
            for func in funcs:
                setattr(module, func, getattr(self, func))
            self.modules[unit] = module
            monkeypatch.setattr(portability_bots, unit, module)

    def _enter(self, name: str, **args: object) -> None:
        self.calls.append(name)
        self.args.setdefault(name, args)
        if name in self.raise_in:
            raise RuntimeError(f"{name} broke")

    def read_archive_binding_claims(self, snap):
        self._enter("read_archive_binding_claims", snap=snap)
        return self.claims

    def merge_roster(self, archive_config, mc, *, stores_added, teams_installed):
        self._enter(
            "merge_roster",
            archive_config=archive_config,
            stores_added=stores_added,
            teams_installed=teams_installed,
        )
        return types.SimpleNamespace(
            added=list(self.added), skipped=list(self.skipped), kept=list(self.kept)
        )

    def imported_bots(self, mc, names, *, config):
        names = list(names)
        self._enter("imported_bots", names=names, config=config)
        return [b for b in self.bots if b.name in names], []

    def binding_ladder(self, mc, bot, claims, config, *, owned_slugs):
        self.args.setdefault(
            "binding_ladder", {"owned_slugs": owned_slugs, "claims": claims, "config": config}
        )
        reason = self.refusals.get(bot.name, "")
        if reason:
            return PartOutcome(bot.name, "binding", "not_rebuilt", reason), ""
        return PartOutcome(bot.name, "binding", "rebuilt"), bot.generation

    def sessions_listing(self, snap):
        self.calls.append("sessions_listing")
        listing = portability_bot_bindings.sessions_listing(snap)
        self.args.setdefault("sessions_listing", listing)
        return listing

    def archive_member_stems(self, snap, *, listing):
        self.calls.append("archive_member_stems")
        self.args.setdefault("archive_member_stems", {"listing": listing})
        return frozenset(self.archive_stems)

    def member_stem(self, slot_key):
        self.calls.append("member_stem")
        return f"dashboard_{slot_key}"

    def is_member_stem(self, stem):
        return stem.startswith("dashboard_member-")

    def read_config_document(self, mc_real):
        self.calls.append("read_config_document")
        return self.destination

    def install_bot_stores(self, snap, mc, bots, *, allow_unpinned, owned_slugs):
        self._enter("install_bot_stores", bots=bots, owned_slugs=owned_slugs)
        return [PartOutcome(b.name, "dashboard", "restored") for b in bots] + [
            o for b in bots for o in self.extra.get(f"dashboard:{b.name}", [])
        ]

    def restore_crewmate_files(self, bots, *, owned_slugs, fresh_home, minimal=None):
        self._enter(
            "restore_crewmate_files", bots=bots, owned_slugs=owned_slugs, fresh_home=fresh_home
        )
        return [PartOutcome(b.name, "crewmate_file", self.crewmate_status) for b in bots]

    def crewmate_file_notices(self, bots, customised, *, written):
        self.calls.append("crewmate_file_notices")
        self.args.setdefault(
            "crewmate_file_notices", {"customised": customised, "written": written}
        )
        return {
            b.name: ["starting_abilities_only"]
            for b in bots
            if b.name in customised and b.name in written
        }

    def rebuild_bindings(self, mc, bots, claims, installed_stems, *, owned_slugs, config, verdicts):
        self._enter(
            "rebuild_bindings",
            bots=list(bots),
            claims=claims,
            installed_stems=installed_stems,
            owned_slugs=owned_slugs,
            config=config,
            verdicts=verdicts,
        )
        return [PartOutcome(b.name, "binding", "rebuilt") for b in bots]

    def stem_slug(self, stem):
        return portability_bot_bindings.stem_slug(stem)

    def orphan_transcripts(self, snap, bots, config, *, mc, listing, passthrough_slugs):
        self._enter(
            "orphan_transcripts",
            bots=list(bots),
            config=config,
            mc=mc,
            listing=listing,
            passthrough_slugs=passthrough_slugs,
        )
        return list(self.orphans)


@pytest.fixture
def dest(tmp_path, monkeypatch) -> Path:
    mc = tmp_path / "dest"
    mc.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    return mc


@pytest.fixture
def fake_replace(monkeypatch):
    """Record `_do_replace`'s components and install the archive's config.json only."""
    seen: list[object] = []

    def _do_replace(snap, mc, components, *, allow_unpinned=False):
        seen.append(components)
        (mc / "config.json").write_bytes((snap / "config.json").read_bytes())

    monkeypatch.setattr(portability, "_do_replace", _do_replace)
    return seen


def _config(*names: str) -> str:
    return json.dumps({"agents": {n: {"kiro_agent": n} for n in names}})


def _replace(tmp_path, units_members: dict[str, bytes | str]) -> dict:
    return portability.apply_import_zip(_archive(tmp_path, units_members), mode="replace")


class TestOrder:
    def test_replace_runs_every_step_in_order(self, tmp_path, dest, monkeypatch, fake_replace):
        units = Units(monkeypatch, [_bot("Atlas")])
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        quiet = ("member_stem", "crewmate_file_notices", "read_config_document")
        assert [c for c in units.calls if c not in quiet] == [
            "sessions_listing",
            "read_archive_binding_claims",
            "archive_member_stems",
            "imported_bots",
            "install_bot_stores",
            "restore_crewmate_files",
            "rebuild_bindings",
            "orphan_transcripts",
        ]
        assert units.args["imported_bots"]["names"] == ["Atlas"]
        assert summary["bots"] == [
            {
                "bot": "Atlas",
                "slug": "atlas",
                "roster": "imported",
                "binding": "rebuilt",
                "crewmate_file": "written",
                "dashboard": "restored",
                "briefing": "none",
                "part_reasons": {
                    "roster": "",
                    "binding": "",
                    "crewmate_file": "",
                    "dashboard": "",
                    "briefing": "",
                },
                "reasons": [],
                "notices": [],
                "in_roster": True,
            }
        ]
        assert "refused_merges" not in summary
        assert not [line for line in summary["items"] if line.startswith("bot")]

    def test_crewmate_notices_merge_into_the_row(self, tmp_path, dest, monkeypatch, fake_replace):
        units = Units(monkeypatch, [_bot("Atlas")])
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(
                    crewmate_files_customised=["Atlas", "Gone"],
                    member_records_not_carried={"atlas": ["member-goals"]},
                ),
                "config.json": _config("Atlas"),
            },
        )
        assert units.args["crewmate_file_notices"] == {
            "customised": frozenset({"Atlas", "Gone"}),
            "written": frozenset({"Atlas"}),
        }
        assert summary["bots"][0]["notices"] == ["starting_abilities_only", "goals_not_carried"]
        assert "refused_merges" not in summary

    def test_no_notice_for_what_is_still_here(self, tmp_path, dest, monkeypatch, fake_replace):
        # The crewmate file was already here (kept) and the goals record is too: the
        # Bot lost neither, so the summary claims no loss (A13).
        units = Units(monkeypatch, [_bot("Atlas")])
        units.crewmate_status = "kept"
        from kiro_crew.member_records import records_dir

        goals = records_dir("member-goals")
        goals.mkdir(parents=True, exist_ok=True)
        (goals / "atlas.json").write_text("{}")
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(
                    crewmate_files_customised=["Atlas"],
                    member_records_not_carried={"atlas": ["member-goals", "member-rules"]},
                ),
                "config.json": _config("Atlas"),
            },
        )
        assert summary["bots"][0]["notices"] == ["rules_not_carried"]
        assert not [line for line in summary["items"] if "notice: goals" in line]

    @pytest.mark.parametrize(
        ("here", "owned"),
        [
            ({"member_id": "atlas", "memory_store": "atlas-store"}, frozenset({"atlas"})),
            ({"member_id": "atlas", "memory_store": "other-store"}, frozenset()),
            ({"member_id": "other", "memory_store": "atlas-store"}, frozenset()),
            (None, frozenset()),
        ],
    )
    def test_only_a_bot_that_was_here_owns_its_existing_entries(
        self, tmp_path, dest, monkeypatch, fake_replace, here, owned
    ):
        units = Units(monkeypatch, [_bot("Atlas")])
        units.destination = {"agents": {"Atlas": here}} if here else None
        _replace(tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")})
        for step in ("install_bot_stores", "restore_crewmate_files", "rebuild_bindings"):
            assert units.args[step]["owned_slugs"] == owned, step
        # Read before the Replace installed the archive's config over it.
        assert units.calls.index("read_config_document") < units.calls.index("imported_bots")

    def test_replace_passes_the_instance_components(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        Units(monkeypatch, [])
        _replace(tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config()})
        assert fake_replace == [list(portability_bots.INSTANCE_REPLACE_COMPONENTS)]
        assert set(portability_bots.INSTANCE_REPLACE_COMPONENTS) == set(COMPONENTS) - {
            "artifacts",
            "uploads",
        }

    def test_claims_are_read_before_the_replace_strip(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        units = Units(monkeypatch, [])
        seen: list[bool] = []

        def claims(snap):
            seen.append((snap / "member-records" / "member-bindings" / "atlas.json").is_file())
            return {}

        units.modules["portability_bot_bindings"].read_archive_binding_claims = claims
        _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(),
                "config.json": _config(),
                "member-records/member-bindings/atlas.json": "{}",
            },
        )
        assert seen == [True]
        assert units.calls.count("read_archive_binding_claims") == 0


class TestMerge:
    def test_roster_merge_runs_after_the_store_merge(self, tmp_path, dest, monkeypatch):
        (dest / "config.json").write_text(_config("Local"))
        (dest / "memory_stores" / "kept-store").mkdir(parents=True)
        units = Units(monkeypatch, [_bot("Atlas")])
        units.skipped = [PartOutcome("Local", "roster", "skipped", "name_taken")]
        stub_merge = units.merge_roster

        def merge_adding_atlas(archive_config, mc, **kwargs):
            # A real roster merge writes the Bots it adds into the installed config.
            (mc / "config.json").write_text(_config("Local", "Atlas"))
            return stub_merge(archive_config, mc, **kwargs)

        units.modules["portability_bot_roster"].merge_roster = merge_adding_atlas
        summary = portability.apply_import_zip(
            _archive(
                tmp_path,
                {
                    "MANIFEST.json": _manifest(),
                    "config.json": json.dumps(
                        {"agents": {"Atlas": {"member_id": "atlas"}, "Local": {}}}
                    ),
                    "memory_stores/atlas-store/manifest.json": "{}",
                    "memory_stores/kept-store/manifest.json": "{}",
                },
            ),
            mode="merge",
        )
        args = units.args["merge_roster"]
        assert args["stores_added"] == frozenset({"atlas-store"})
        # The store the roster merge was told about is on disk by then.
        assert (dest / "memory_stores" / "atlas-store" / "manifest.json").is_file()
        assert units.args["imported_bots"]["names"] == ["Atlas"]
        rows = {row["bot"]: row for row in summary["bots"]}
        assert rows["Atlas"]["roster"] == "added"
        assert rows["Local"]["roster"] == "skipped"
        assert rows["Local"]["reasons"] == ["name_taken"]
        assert rows["Local"]["slug"] == "local"
        assert "bot Local (not whole: roster skipped: name_taken)" in summary["items"]
        assert summary["refused_merges"] == ["bots"]
        # A41: the skipped Bot is not in the installed roster; the added one is.
        assert (rows["Local"]["in_roster"], rows["Atlas"]["in_roster"]) == (False, True)
        assert rows["Local"]["part_reasons"]["roster"] == "name_taken"

    def test_a_bot_the_roster_step_names_is_in_the_roster_only_when_the_config_holds_it(
        self, tmp_path, dest, monkeypatch
    ):
        # The roster step names Atlas as added, but the installed config the import
        # loads holds no Atlas: no row may claim it is in the Bot list.
        (dest / "config.json").write_text(_config("Local"))
        units = Units(monkeypatch, [_bot("Atlas")])
        summary = portability.apply_import_zip(
            _archive(
                tmp_path,
                {
                    "MANIFEST.json": _manifest(),
                    "config.json": json.dumps({"agents": {"Atlas": {"member_id": "atlas"}}}),
                },
            ),
            mode="merge",
        )
        assert units.args["imported_bots"]["names"] == ["Atlas"]
        [row] = [row for row in summary["bots"] if row["bot"] == "Atlas"]
        assert row["in_roster"] is False

    def test_in_roster_reads_the_loaded_config_names_only(self):
        config = types.SimpleNamespace(agents={"Atlas": object(), "Local": object()})
        assert portability_bots._in_roster(config, ["Atlas", "Ghost"]) == frozenset({"Atlas"})
        assert portability_bots._in_roster(None, ["Atlas"]) == frozenset()

    def test_merge_without_a_config_imports_every_installed_agent(
        self, tmp_path, dest, monkeypatch
    ):
        units = Units(monkeypatch, [_bot("Atlas"), _bot("Bea")])
        portability.apply_import_zip(
            _archive(
                tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")}
            ),
            mode="merge",
        )
        assert "merge_roster" not in units.calls
        assert units.args["imported_bots"]["names"] == ["Atlas", "Bea"]

    def test_a_bot_already_here_is_kept_and_imported(self, tmp_path, dest, monkeypatch):
        (dest / "config.json").write_text(_config("Atlas"))
        units = Units(monkeypatch, [_bot("Atlas")])
        units.added, units.kept = [], ["Atlas"]
        summary = portability.apply_import_zip(
            _archive(tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}),
            mode="merge",
        )
        assert units.args["imported_bots"]["names"] == ["Atlas"]
        assert "install_bot_stores" in units.calls
        [row] = summary["bots"]
        assert (row["roster"], row["binding"], row["reasons"]) == ("kept", "rebuilt", [])
        assert "refused_merges" not in summary

    @pytest.mark.parametrize("dest_config", [True, False])
    def test_a_merge_that_neither_kept_nor_installed_a_config_imports_no_bot(
        self, tmp_path, dest, monkeypatch, dest_config
    ):
        # With a config here the archive carries none; without one, the archive's is
        # refused by the settings vetting. Either way no config is the roster (A9).
        if dest_config:
            (dest / "config.json").write_text(_config("Local"))
        members = {"MANIFEST.json": _manifest()}
        if not dest_config:
            members["config.json"] = "[1]"
        units = Units(monkeypatch, [_bot("Local")])
        summary = portability.apply_import_zip(_archive(tmp_path, members), mode="merge")
        assert "merge_roster" not in units.calls
        assert units.args["imported_bots"]["names"] == []
        assert summary["bots"] == []


class TestTemplateWarning:
    def test_the_template_lookup_sees_crewmate_files_as_the_loader_does(
        self, tmp_path, monkeypatch
    ):
        # A Bot running on its crewmate file under mates/agents/ (imported, or one
        # the destination had) has its template installed; only a template neither
        # folder holds is named. No per-import list decides it.
        from kiro_crew import portability_templates as pt
        from kiro_crew.config import paths

        agents, mates = tmp_path / "agents", tmp_path / "mates"
        agents.mkdir()
        mates.mkdir()
        (agents / "builder.json").write_text(json.dumps({"name": "builder"}), encoding="utf-8")
        for name in ("Atlas", "Nova"):
            (mates / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        monkeypatch.setattr(pt, "kiro_agents_dir", lambda: agents)
        monkeypatch.setattr(paths, "mate_agents_dir", lambda: mates)
        cfg = tmp_path / "config.json"
        crews = {
            "Atlas": {"kiro_agent": "Atlas"},
            "Nova": {"kiro_agent": "Nova"},
            "Ada": {"kiro_agent": "builder"},
            "Hand": {"kiro_agent": "hand-made"},
        }
        cfg.write_text(json.dumps({"agents": crews}), encoding="utf-8")
        assert pt.missing_crew_templates(cfg) == ([{"crew": "Hand", "kiro_agent": "hand-made"}], 0)


class TestTranscripts:
    def test_member_thread_of_a_non_imported_slot_is_withheld(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        atlas = _bot("Atlas")
        units = Units(monkeypatch, [atlas])
        own = f"dashboard_{atlas.slot_key}"
        ghost = "dashboard_member-ghost.memory-ghost-store"
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(),
                "config.json": _config("Atlas"),
                f"sessions/{own}.jsonl": _chat(),
                f"sessions/{ghost}.jsonl": _chat(),
                "sessions/dashboard_plain.jsonl": _chat(),
            },
        )
        sessions = dest / "sessions"
        assert (sessions / f"{own}.jsonl").is_file()
        assert (sessions / "dashboard_plain.jsonl").is_file()
        assert not (sessions / f"{ghost}.jsonl").exists()
        assert summary["sessions_member_withheld"] == 1
        assert summary["sessions_added"] == 2
        assert units.args["rebuild_bindings"]["installed_stems"] == frozenset(
            {own, "dashboard_plain"}
        )

    def _refused_atlas(self, monkeypatch, *, reason: str = "no_member_id"):
        atlas = _bot("Atlas")
        units = Units(monkeypatch, [atlas, _bot("Bea")])
        units.refusals["Atlas"] = reason
        units.archive_stems = {f"dashboard_{atlas.slot_key}"}
        return atlas, units

    def test_a_thread_whose_binding_verdict_refuses_is_withheld_and_orphaned(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        atlas, units = self._refused_atlas(monkeypatch)
        own = f"dashboard_{atlas.slot_key}"
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(),
                "config.json": _config("Atlas", "Bea"),
                f"sessions/{own}.jsonl": _chat(),
            },
        )
        assert not (dest / "sessions" / f"{own}.jsonl").exists()
        assert summary["sessions_member_withheld"] == 1
        # The rebuild and the orphan list run without Atlas, on the config loaded once.
        assert [b.name for b in units.args["rebuild_bindings"]["bots"]] == ["Bea"]
        assert [b.name for b in units.args["orphan_transcripts"]["bots"]] == ["Bea"]
        config = units.args["imported_bots"]["config"]
        assert config is not None
        assert units.args["rebuild_bindings"]["config"] is config
        assert units.args["orphan_transcripts"]["config"] is config
        [row] = [r for r in summary["bots"] if r["bot"] == "Atlas"]
        assert row["binding"] == "not_rebuilt" and row["reasons"] == ["no_member_id"], row

    def test_a_withheld_thread_already_here_counts_as_already_here(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        atlas, _units = self._refused_atlas(monkeypatch, reason="slug_collision")
        own = f"dashboard_{atlas.slot_key}"
        (dest / "sessions").mkdir()
        (dest / "sessions" / f"{own}.jsonl").write_bytes(_chat("mine"))
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(),
                "config.json": _config("Atlas", "Bea"),
                f"sessions/{own}.jsonl": _chat("theirs"),
            },
        )
        assert (dest / "sessions" / f"{own}.jsonl").read_bytes() == _chat("mine")
        assert summary["sessions_member_withheld"] == 0
        assert summary["sessions_skipped_existing"] == 1

    def test_a_refused_verdict_withholds_nothing_the_archive_does_not_carry(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        _atlas, units = self._refused_atlas(monkeypatch)
        units.archive_stems = set()
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")}
        )
        assert [b.name for b in units.args["rebuild_bindings"]["bots"]] == ["Atlas", "Bea"]
        [row] = [r for r in summary["bots"] if r["bot"] == "Atlas"]
        assert row["binding"] == "rebuilt", row

    def test_merge_sessions_without_a_predicate_is_unchanged(self, tmp_path):
        snap = tmp_path / "work" / PREFIX
        (snap / "sessions").mkdir(parents=True)
        (snap / "sessions" / "dashboard_member-x.jsonl").write_bytes(_chat())
        mc = tmp_path / "mc"
        mc.mkdir()
        summary: dict = {"items": []}
        installed = portability._merge_sessions(snap, mc, summary, allow_unpinned=True)
        assert installed == frozenset({"dashboard_member-x"})
        assert "sessions_member_withheld" not in summary
        assert summary["items"] == [
            "sessions (merged: 1 added, 0 already here, 0 withheld, 0 failed)"
        ]


class TestFailures:
    @pytest.mark.parametrize(
        ("step", "parts"),
        [
            ("install_bot_stores", {"dashboard": "not_restored", "briefing": "not_restored"}),
            ("restore_crewmate_files", {"crewmate_file": "not_written"}),
            ("rebuild_bindings", {"binding": "not_rebuilt"}),
        ],
    )
    def test_a_part_that_raises_is_reported_and_the_import_returns(
        self, tmp_path, dest, monkeypatch, fake_replace, step, parts
    ):
        units = Units(monkeypatch, [_bot("Atlas"), _bot("Bea")])
        units.raise_in = {step}
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")}
        )
        # One call with both Bots and no retry: what it wrote before raising is
        # unknown, so every Bot's part is reported write_failed.
        assert units.calls.count(step) == 1
        assert "orphan_transcripts" in units.calls
        # EVERY part the unit owns: the stores unit owns the dashboard and the briefing.
        for row in summary["bots"]:
            for part, status in parts.items():
                assert row[part] == status, (part, row)
            assert row["reasons"] == ["write_failed"]
        assert summary["refused_merges"] == ["bots"]

    def test_a_unit_missing_its_call_is_reported_per_bot(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        Units(monkeypatch, [_bot("Atlas")])
        monkeypatch.setattr(
            portability_bots, "portability_bot_stores", types.ModuleType("portability_bot_stores")
        )
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        row = summary["bots"][0]
        assert (row["dashboard"], row["binding"]) == ("not_restored", "rebuilt")

    def test_imported_bots_raising_becomes_unreadable_rows(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        units = Units(monkeypatch, [_bot("Atlas")])
        units.raise_in = {"imported_bots"}
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        assert summary["bots"][0]["roster"] == "skipped"
        assert summary["bots"][0]["reasons"] == ["unreadable_record"]
        assert "install_bot_stores" not in units.calls

    def test_unreadable_claims_rebuild_no_binding(self, tmp_path, dest, monkeypatch, fake_replace):
        units = Units(monkeypatch, [_bot("Atlas")])
        units.raise_in = {"read_archive_binding_claims"}
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        # The rebuild is handed the unread claim set (``None``); its own ladder
        # refuses the write (``test_portability_bot_bindings``).
        assert units.args["rebuild_bindings"]["claims"] is None
        assert summary["bots"][0]["binding"] == "rebuilt"

    def test_restore_bots_never_raises(self, tmp_path, monkeypatch):
        def broken(*_a, **_k):
            raise RuntimeError("summary broke")

        monkeypatch.setattr(portability_bots, "_restore_bots", broken)
        summary: dict = {"items": []}
        portability_bots.restore_bots(
            tmp_path,
            tmp_path,
            summary,
            bots=portability_bots.PreparedBots(),
            installed_stems=(),
            allow_unpinned=True,
            manifest={},
        )
        assert summary["items"] == ["bots (failed: RuntimeError)"]
        assert summary["report_items"] == ["bots (failed: RuntimeError)"]
        assert summary["bots_failed"] == "RuntimeError"
        assert summary["refused_merges"] == ["bots"]

    def test_report_items_lists_only_the_bot_lines_of_an_import(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        units = Units(monkeypatch, [_bot("Atlas")])
        units.raise_in = {"install_bot_stores"}
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        assert summary["report_items"] == [
            "bot Atlas (not whole: dashboard not restored: write_failed; "
            "briefing not restored: write_failed)"
        ]
        # Each is in items too, after the restored items it does not count as.
        assert set(summary["report_items"]) <= set(summary["items"])
        assert "full replace" in summary["items"]
        assert "full replace" not in summary["report_items"]

    def test_the_archive_chats_are_listed_once_and_shared(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        atlas = _bot("Atlas")
        units = Units(monkeypatch, [atlas])
        listed: list[Path] = []
        real_listing = portability._listing

        def listing(directory):
            listed.append(Path(directory))
            return real_listing(directory)

        monkeypatch.setattr(portability, "_listing", listing)
        own = f"dashboard_{atlas.slot_key}"
        summary = _replace(
            tmp_path,
            {
                "MANIFEST.json": _manifest(),
                "config.json": _config("Atlas"),
                f"sessions/{own}.jsonl": _chat(),
            },
        )
        assert units.calls.count("sessions_listing") == 1
        shared = units.args["sessions_listing"]
        assert set(shared) == {f"{own}.jsonl"}
        assert units.args["archive_member_stems"]["listing"] is shared
        assert units.args["orphan_transcripts"]["listing"] is shared
        # That one listing (taken through this module's lister) is the only one: the
        # chat merge lists the archive's sessions/ tree no further.
        assert len([d for d in listed if d.name == "sessions" and d.parent.name == PREFIX]) == 1
        assert summary["sessions_added"] == 1

    def test_the_binding_verdict_judges_on_the_owned_slugs(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        atlas = _bot("Atlas")
        units = Units(monkeypatch, [atlas])
        units.archive_stems = {f"dashboard_{atlas.slot_key}"}
        units.refusals["Atlas"] = "held_by_other_bot"
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas")}
        )
        assert units.args["binding_ladder"]["owned_slugs"] == frozenset()
        [row] = summary["bots"]
        assert row["binding"] == "not_rebuilt" and row["reasons"] == ["held_by_other_bot"], row
        assert [b.name for b in units.args["orphan_transcripts"]["bots"]] == []

    def test_the_verdicts_judged_before_the_chat_merge_reach_the_rebuild(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        # A44: the binding ladder runs once, before the chat merge; the rebuild acts on
        # those verdicts (with their generation) instead of judging again.
        atlas, bea = _bot("Atlas"), _bot("Bea")
        units = Units(monkeypatch, [atlas, bea])
        units.archive_stems = {f"dashboard_{atlas.slot_key}"}
        _replace(tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")})
        verdicts = units.args["rebuild_bindings"]["verdicts"]
        assert verdicts == {"Atlas": (PartOutcome("Atlas", "binding", "rebuilt"), atlas.generation)}

    def test_each_unit_is_called_once_with_every_bot(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        units = Units(monkeypatch, [_bot("Atlas"), _bot("Bea")])
        _replace(tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")})
        for step in ("install_bot_stores", "restore_crewmate_files"):
            assert units.calls.count(step) == 1
            assert [b.name for b in units.args[step]["bots"]] == ["Atlas", "Bea"]
        assert units.calls.count("rebuild_bindings") == 1
        assert units.args["orphan_transcripts"]["mc"] == dest

    def test_an_unreadable_installed_config_still_gives_every_bot_a_row(
        self, tmp_path, dest, monkeypatch, fake_replace
    ):
        units = Units(monkeypatch, [])

        def broken():
            raise RuntimeError("config unreadable")

        def unreadable(mc, names, *, config):
            units.args["imported_bots"] = {"names": list(names)}
            return [], [PartOutcome(n, "roster", "skipped", "unreadable_record") for n in names]

        monkeypatch.setattr(portability_bots, "_installed_config", broken)
        units.modules["portability_bot_roster"].imported_bots = unreadable
        summary = _replace(
            tmp_path, {"MANIFEST.json": _manifest(), "config.json": _config("Atlas", "Bea")}
        )
        assert units.args["imported_bots"]["names"] == ["Atlas", "Bea"]
        assert [(r["bot"], r["slug"]) for r in summary["bots"]] == [
            ("Atlas", "atlas"),
            ("Bea", "bea"),
        ]
        # Whether the installed config holds a Bot is unknown: no row claims it does.
        assert [r["in_roster"] for r in summary["bots"]] == [False, False]


def _summary_for(
    bots: list[ImportedBot],
    outcomes: list[PartOutcome],
    *,
    archive_bots: bool = True,
    **manifest,
) -> dict:
    summary: dict = {"items": []}
    portability_bots._write_summary(
        summary,
        portability_bots.PreparedBots(bots=bots, slugs={b.name: b.slug for b in bots}),
        outcomes,
        [],
        {
            name: ["starting_abilities_only"]
            for name in portability_bots._manifest_names(manifest, "crewmate_files_customised")
        },
        portability_bots._manifest_records(manifest),
        manifest,
        archive_bots=archive_bots,
    )
    return summary


class TestSummary:
    def test_rows_are_capped_with_a_more_count(self):
        bots = [_bot(f"Bot{i:03d}", f"bot{i:03d}") for i in range(203)]
        summary = _summary_for(bots, [], bot_stores=1)
        assert len(summary["bots"]) == portability_bots.MAX_BOT_ROWS
        assert summary["bots_more"] == 3
        assert "bots (3 more not listed)" in summary["items"]

    def test_rows_past_the_cap_are_only_counted(self, monkeypatch):
        cap = portability_bots.MAX_BOT_ROWS
        bots = [_bot(f"Bot{i:03d}", f"bot{i:03d}") for i in range(cap + 3)]
        built: list[str] = []
        noticed: list[str] = []
        real_row, real_notices = portability_bots._row, portability_bots._notices_for

        def row(name, *a, **kw):
            built.append(name)
            return real_row(name, *a, **kw)

        def notices(name, *a, **kw):
            noticed.append(name)
            return real_notices(name, *a, **kw)

        monkeypatch.setattr(portability_bots, "_row", row)
        monkeypatch.setattr(portability_bots, "_notices_for", notices)
        late = bots[-1].name
        summary = _summary_for(
            bots,
            [PartOutcome(late, "dashboard", "not_restored", "write_failed")],
            bot_stores=1,
        )
        assert built == noticed == [b.name for b in bots[:cap]]
        assert summary["bots_more"] == 3
        # A Bot past the cap still counts: it is not whole, so the import says so.
        assert summary["refused_merges"] == ["bots"]
        assert not [line for line in summary["items"] if late in line]

    def test_every_report_line_is_listed_in_report_items(self):
        atlas, bea = _bot("Atlas"), _bot("Bea")
        summary = _summary_for(
            [atlas, bea],
            [PartOutcome("Bea", "dashboard", "not_restored", "write_failed")],
            crewmate_files_customised=["Atlas"],
            crewmate_files_customised_more=2,
        )
        assert summary["report_items"] == summary["items"]
        assert summary["report_items"] == [
            "bot Atlas (notice: starting_abilities_only)",
            "bot Bea (not whole: dashboard not restored: write_failed)",
            "bot dashboards (not carried: the archive was made before Bot stores)",
            "bots (2 more had added abilities that were not carried)",
        ]

    def test_notices_add_lines_without_making_a_bot_not_whole(self):
        atlas, bea = _bot("Atlas"), _bot("Bea")
        summary = _summary_for(
            [atlas, bea],
            [],
            bot_stores=1,
            crewmate_files_customised=["Atlas"],
            member_records_not_carried={"bea": ["member-rules", "member-goals"]},
        )
        rows = {row["bot"]: row for row in summary["bots"]}
        assert rows["Atlas"]["notices"] == ["starting_abilities_only"]
        assert rows["Bea"]["notices"] == ["rules_not_carried", "goals_not_carried"]
        assert summary["items"] == [
            "bot Atlas (notice: starting_abilities_only)",
            "bot Bea (notice: rules_not_carried)",
            "bot Bea (notice: goals_not_carried)",
        ]
        assert "refused_merges" not in summary

    def test_not_whole_bot_names_what_is_missing(self):
        atlas = _bot("Atlas")
        summary = _summary_for(
            [atlas],
            [
                PartOutcome("Atlas", "dashboard", "page_not_rebuilt", "digest_mismatch"),
                PartOutcome("Atlas", "briefing", "not_restored", "write_failed"),
                PartOutcome("Atlas", "binding", "rebuilt"),
            ],
            bot_stores=1,
        )
        assert summary["items"] == [
            "bot Atlas (not whole: dashboard page not rebuilt: digest_mismatch; "
            "briefing not restored: write_failed)"
        ]
        assert summary["refused_merges"] == ["bots"]
        assert summary["bots"][0]["dashboard"] == "page_not_rebuilt"
        assert summary["bots"][0]["reasons"] == ["digest_mismatch", "write_failed"]

    def test_each_row_carries_the_reason_of_each_part_and_whether_it_is_in_the_roster(self):
        # A41: the witness row: the agent file failed to write, the binding is another
        # Bot's. Each part names its own reason, next to the merged list.
        atlas = _bot("Atlas")
        summary: dict = {"items": []}
        portability_bots._write_summary(
            summary,
            portability_bots.PreparedBots(
                bots=[atlas], slugs={"Atlas": "atlas", "Bea": "bea"}, in_roster=frozenset({"Atlas"})
            ),
            [
                PartOutcome("Atlas", "crewmate_file", "not_written", "write_failed"),
                PartOutcome("Atlas", "binding", "not_rebuilt", "held_by_other_bot"),
                PartOutcome("Bea", "roster", "skipped", "unreadable_record"),
            ],
            [],
            {},
            {},
            {"bot_stores": 1},
            archive_bots=True,
        )
        rows = {row["bot"]: row for row in summary["bots"]}
        assert rows["Atlas"]["part_reasons"] == {
            "roster": "",
            "binding": "held_by_other_bot",
            "crewmate_file": "write_failed",
            "dashboard": "",
            "briefing": "",
        }
        assert rows["Atlas"]["reasons"] == ["write_failed", "held_by_other_bot"]
        assert rows["Atlas"]["in_roster"] is True
        # Bea is not in the installed config: its unreadable record means not added.
        assert rows["Bea"]["in_roster"] is False
        assert rows["Bea"]["part_reasons"]["roster"] == "unreadable_record"

    def test_old_archive_and_more_count_lines(self):
        summary = _summary_for(
            [],
            [],
            member_records_not_carried_more=4,
            crewmate_files_customised_more=7,
        )
        assert summary["items"] == [
            "bot dashboards (not carried: the archive was made before Bot stores)",
            "bots (4 more had rules or goals that were not carried)",
            "bots (7 more had added abilities that were not carried)",
        ]
        assert summary["bots"] == []

    def test_every_row_has_a_briefing_status(self):
        atlas, bea = _bot("Atlas"), _bot("Bea")
        summary = _summary_for(
            [atlas, bea],
            [PartOutcome("Atlas", "briefing", "not_restored", "write_failed")],
            bot_stores=1,
        )
        rows = {row["bot"]: row for row in summary["bots"]}
        assert rows["Atlas"]["briefing"] == "not_restored"
        assert rows["Bea"]["briefing"] == "none"
        assert summary["refused_merges"] == ["bots"]

    def test_an_old_archive_without_bots_lost_no_dashboard(self):
        summary = _summary_for([], [], archive_bots=False)
        assert "bot_dashboards_not_carried" not in summary
        assert summary["items"] == []

    @pytest.mark.parametrize(("agents", "flagged"), [((), False), (("Atlas",), True)])
    def test_dashboards_not_carried_needs_a_bot_in_the_archive_config(
        self, tmp_path, dest, monkeypatch, fake_replace, agents, flagged
    ):
        Units(monkeypatch, [])
        config = json.dumps({"agents": {"default": {}, **{n: {} for n in agents}}})
        summary = _replace(
            tmp_path, {"MANIFEST.json": json.dumps({"version": 1}), "config.json": config}
        )
        assert summary.get("bot_dashboards_not_carried", False) is flagged
        line = "bot dashboards (not carried: the archive was made before Bot stores)"
        assert (line in summary["items"]) is flagged

    def test_archive_text_is_clipped(self):
        long = "x" * 300
        summary: dict = {"items": []}
        portability_bots._write_summary(
            summary,
            portability_bots.PreparedBots(
                outcomes=[PartOutcome(long, "roster", "skipped", "name_taken")],
                slugs={long: long},
            ),
            [PartOutcome(long, "roster", "skipped", "name_taken")],
            portability_bots._orphan_rows(
                [{"slug": long, "generation": long, "candidates": [long] * 50}]
            ),
            {},
            {},
            {"bot_stores": 1},
            archive_bots=True,
        )
        row = summary["bots"][0]
        assert len(row["bot"]) <= 64 and len(row["slug"]) <= 64
        orphan = summary["bot_transcripts_orphaned"][0]
        assert len(orphan["slug"]) <= 64
        assert len(orphan["candidates"]) == portability_bots.MAX_ORPHAN_CANDIDATES
        assert all(len(line) < 200 for line in summary["items"])


class TestMemoryOnly:
    def test_memory_only_makes_no_bot_call(self, tmp_path, dest, monkeypatch):
        units = Units(monkeypatch, [_bot("Atlas")])
        summary = portability.apply_import_zip(
            _archive(
                tmp_path,
                {
                    "MANIFEST.json": json.dumps({"version": 1, "components": ["memory"]}),
                    "memory_stores/atlas-store/manifest.json": "{}",
                    "sessions/dashboard_member-atlas.jsonl": _chat(),
                },
            ),
            mode="merge",
        )
        assert units.calls == []
        assert not {"bots", "sessions_member_withheld", "bot_transcripts_orphaned"} & set(summary)


def test_only_a_member_thread_the_predicate_refuses_is_withheld():
    from kiro_crew import portability_bots as pb

    allowed = {"dashboard_member-vega"}.__contains__
    assert pb.member_thread_withheld(None, "dashboard_member-atlas") is False
    assert pb.member_thread_withheld(allowed, "dashboard_chat-1") is False
    assert pb.member_thread_withheld(allowed, "dashboard_member-vega") is False
    assert pb.member_thread_withheld(allowed, "dashboard_member-atlas") is True


def test_a_merge_whose_config_write_fails_reports_no_config_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install decision is made under the config lock, but only a landed write counts.

    A write that raises after ``mutate`` chose to install must not report ``config.json``
    as installed, or the roster step would import every agent of a config that is not
    on disk.
    """
    from kiro_crew import portability

    def write_fails_after_deciding(target: Path, *, mutate):  # type: ignore[no-untyped-def]
        assert mutate({}) is not None  # the lock-held decision: install
        raise OSError("disk full")

    monkeypatch.setattr(portability, "update_config_locked", write_fails_after_deciding)
    summary: dict = {"items": []}

    installed = portability._merge_settings(
        tmp_path, {"config.json": {"agents": {}}}, summary, None
    )

    assert installed is False
    assert not (tmp_path / "config.json").exists()
    assert not any(item == "config (restored)" for item in summary["items"])


def test_reserved_slugs_are_read_from_the_destinations_own_config_before_the_import():
    # A45 / CODE-3: the reserved names' slugs come from the pre-import document only.
    from kiro_crew import portability_bots as pb

    doc = {"agents": {"default": {"member_id": "home-default"}, "Atlas": {}}}
    assert pb._destination_reserved_slugs(doc) == frozenset({"home-default"})
    # No agent at all: the loader gives the install its ``default``.
    assert pb._destination_reserved_slugs({"agents": {}}) == frozenset({"default"})
    # No readable config (absent or unreadable): the loader seeds ``default`` as well.
    assert pb._destination_reserved_slugs(None) == frozenset({"default"})
    assert pb._destination_reserved_slugs({"version": 2}) == frozenset({"default"})
    # Agents but no ``default`` entry, or a degraded table: the loader still gives the
    # install a ``default`` agent, so its slug is reserved all the same.
    assert pb._destination_reserved_slugs({"agents": {"Atlas": {}}}) == frozenset({"default"})
    assert pb._destination_reserved_slugs({"agents": {"x": 5}}) == frozenset({"default"})
    assert pb._destination_reserved_slugs({"agents": []}) == frozenset({"default"})


def test_a_reserved_slug_the_installed_config_gives_is_never_one():
    from kiro_crew import portability_bots as pb

    # The import installed a config whose ``default`` names slug "planted"; the
    # destination's own ``default`` had slug "default", which a Bot here does not use.
    installed = types.SimpleNamespace(
        agents={"default": types.SimpleNamespace(member_id="planted")}
    )
    pre = pb.PreImport(reserved_slugs=frozenset({"default"}))
    prepared = pb.PreparedBots(pre=pre)
    assert pb._reserved_slugs(installed, prepared) == frozenset({"default"})
    # A slug a Bot uses (one this install had, here) is never a reserved one.
    pre.destination_slugs = frozenset({"default"})
    assert pb._reserved_slugs(installed, prepared) == frozenset()


def test_before_import_reads_the_reserved_slugs_before_the_mode_branch(tmp_path):
    from kiro_crew import portability_bots as pb

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "config.json").write_text(
        json.dumps({"agents": {"default": {"member_id": "own-default"}}}), encoding="utf-8"
    )
    snap = tmp_path / "snap"
    snap.mkdir()
    pre = pb.before_import(snap, mc, {"config.json": {"agents": {"default": {"member_id": "x"}}}})
    assert pre.reserved_slugs == frozenset({"own-default"})


def test_a_skipped_or_listed_archive_bots_slug_is_never_a_reserved_one():
    # A Bot the roster step skipped, or any archive entry that is not a reserved
    # name, uses its slug: a thread there is a Bot's and is withheld, never passed
    # through as the reserved agent's chat.
    from kiro_crew import portability_bots as pb

    pre = pb.PreImport(reserved_slugs=frozenset({"default", "mate-conductor"}))
    stem = "dashboard_member-default"
    prepared = pb.PreparedBots(pre=pre, slugs={"Zed": "default"})
    prepared.reserved_slugs = pb._reserved_slugs(None, prepared)
    assert prepared.reserved_slugs == frozenset({"mate-conductor"})
    assert pb.member_thread_withheld(pb.allowed_member_stems(prepared), stem)
    # Without the skipped Bot the reserved agent's thread passes through.
    free = pb.PreparedBots(pre=pre)
    free.reserved_slugs = pb._reserved_slugs(None, free)
    assert not pb.member_thread_withheld(pb.allowed_member_stems(free), stem)
    pre.archive_config = {"agents": {"default": {}, "Ivy": {"member_id": "mate-conductor"}}}
    assert pb._reserved_slugs(None, pb.PreparedBots(pre=pre)) == frozenset({"default"})


def test_the_minimal_file_bots_come_from_the_crewmate_steps_own_classification(monkeypatch):
    # The step said which kept files were minimal; nothing re-reads a file to decide.
    from kiro_crew import portability_bot_crewmates as crewmates
    from kiro_crew import portability_bot_types
    from kiro_crew import portability_bots as pb

    def no_reread(_name):
        raise AssertionError("the crewmate file was read again")

    outcomes = [
        PartOutcome("Atlas", "crewmate_file", "written"),
        PartOutcome("Bea", "crewmate_file", "kept"),
        PartOutcome("Cy", "crewmate_file", "kept"),
        PartOutcome("Dora", "crewmate_file", "not_written", "write_failed"),
    ]
    monkeypatch.setattr(portability_bot_types, "crewmate_file_state", no_reread)
    assert pb._minimal_file_bots(outcomes, {"Bea", "Dora"}) == frozenset({"Atlas", "Bea"})

    # The step fills the set for a Bot new here whose kept file is the minimal one.
    monkeypatch.setattr(crewmates, "safe_crewmate_name", lambda _n: True)
    monkeypatch.setattr(crewmates.mate_config, "user_agent_named", lambda _n: False)
    monkeypatch.setattr(crewmates.mate_config, "write_mate_config", lambda *_a, **_k: None)
    monkeypatch.setattr(crewmates, "_owner_of", lambda _n: "none")
    monkeypatch.setattr(crewmates, "crewmate_file_state", lambda _n: "minimal")
    bot = _bot("Bea")
    found: set[str] = set()
    [outcome] = crewmates.restore_crewmate_files([bot], owned_slugs=frozenset(), minimal=found)
    assert (outcome.status, found) == ("kept", {"Bea"})
    # The same file for a Bot this install had is its own, not "minimal".
    found.clear()
    crewmates.restore_crewmate_files([bot], owned_slugs=frozenset({bot.slug}), minimal=found)
    assert found == set()


def test_a_rows_part_status_and_reason_come_from_one_chosen_outcome():
    outcomes = [
        PartOutcome("Atlas", "dashboard", "kept"),
        PartOutcome("Atlas", "dashboard", "page_not_rebuilt", "write_failed"),
        PartOutcome("Atlas", "briefing", "kept"),
    ]
    chosen = portability_bots._part_outcome(outcomes, "dashboard")
    assert chosen == outcomes[1]
    assert portability_bots._part_status(outcomes, "dashboard", "none") == chosen.status
    assert portability_bots._part_reason(outcomes, "dashboard") == chosen.reason
    assert portability_bots._part_outcome(outcomes, "binding") is None
    assert portability_bots._part_status(outcomes, "binding", "none") == "none"
    assert portability_bots._part_reason(outcomes, "binding") == ""

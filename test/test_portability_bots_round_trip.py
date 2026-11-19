"""An instance export and import keep every Bot whole, end to end (spec kc-37418 U9).

Each test builds real Bots on a source home through the create path (a private
version-2 store, its own workspace, its crewmate file), gives them a DM transcript
and a dashboard package applied through ``instance.apply_package``, exports through
``create_export_zip`` and imports with ``apply_import_zip`` into a second home. The
imported Bot is then used the way a person uses it: its DM thread is opened through
the thread route and its dashboard read through the dashboard route.

Hostile archives (a forged binding, a stray crewmate file, an escaping name) are
built in Python from a real export and asserted on; nothing reaches a shell.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import types
from pathlib import Path
from typing import Any

import pytest
from portability_bot_helpers import (
    AppliedDashboard,
    Homes,
    MadeBot,
    apply_dashboard,
    archive_members,
    boot_first_crewmate,
    chat_in_dm,
    create_bot,
    export_archive,
    get_dashboard,
    install_fixture_catalog,
    open_thread,
    rewrite_archive,
    tree_bytes,
)

from kiro_crew import (
    crew_teams,
    mate_config,
    portability,
    portability_bot_export,
    portability_bots,
    portability_stream,
)
from kiro_crew.agent_panel import panel_dir
from kiro_crew.config.loader import _invalidate_config_cache
from kiro_crew.config.paths import data_home
from kiro_crew.member_records import records_dir
from kiro_crew.members import DM_SLOT_MODE, dm_binding_path, read_dm_binding_for_slot
from kiro_crew.portability import apply_import_zip
from kiro_crew.portability_bot_types import NOT_WHOLE_STATUSES, is_minimal_crewmate_file
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

ATLAS_PAGE = '<p data-dashboard-field="goal">atlas</p>'


@pytest.fixture
def homes(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Homes:
    install_fixture_catalog(tmp_path / "catalog", _floor_monkeypatch)
    return Homes(tmp_path / "homes", _floor_monkeypatch)


async def _source(homes: Homes, tmp_path: Path) -> tuple[MadeBot, AppliedDashboard, Path]:
    """Bot Atlas on home ``a``, with a DM transcript and a dashboard, exported."""
    homes.use("a")
    atlas = create_bot("Atlas")
    await chat_in_dm(atlas, "hello from the first install")
    board = apply_dashboard(atlas, ATLAS_PAGE)
    return atlas, board, export_archive(tmp_path / "a.zip")


def _row(summary: dict[str, Any], name: str) -> dict[str, Any]:
    [row] = [r for r in summary.get("bots", []) if r["bot"] == name]
    return row


def _assert_whole(summary: dict[str, Any], name: str) -> None:
    row = _row(summary, name)
    assert row["notices"] == [], row
    assert row["binding"] == "rebuilt", row
    assert row["crewmate_file"] == "written", row
    assert row["dashboard"] == "restored", row
    assert row["roster"] in ("imported", "added"), row
    assert row["reasons"] == [], row
    assert not [line for line in summary["items"] if f"bot {name} " in line], summary["items"]


def _assert_crewmate_minimal(name: str) -> None:
    path = mate_config.mate_config_path(name)
    assert is_minimal_crewmate_file(json.loads(path.read_text(encoding="utf-8")), name)


async def _assert_bot_usable(bot: MadeBot, board: AppliedDashboard, *transcript: str) -> None:
    status, body, messages = await open_thread(bot.slug)
    assert status == 200, body
    assert body["slot_key"] == bot.slot_key
    for line in transcript:
        assert line in messages, messages
    binding = read_dm_binding_for_slot(bot.slot_key)
    assert binding is not None and binding["member"] == bot.name
    status, page = await get_dashboard(bot)
    assert status == 200, page
    assert page["dashboard"] == board.package, page
    assert page["html"] == board.html, page
    # The page in force is the gateway's own rebuilt snapshot of the pinned package.
    assert _page_snapshots(bot.slug) == [f"{bot.slug}.dashboard-page.{board.digest}.json"]
    _assert_crewmate_minimal(bot.name)


def _page_snapshots(slug: str) -> list[str]:
    directory = panel_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p.name for p in directory.iterdir() if p.name.startswith(f"{slug}.dashboard-page.")
    )


def _record_trees(mc: Path) -> dict[str, bytes | None]:
    """Everything under the destination's gateway-owned ``member-records/`` and ``crew-panels/``."""
    return {
        f"{top}/{rel}": data
        for top in ("member-records", "crew-panels")
        for rel, data in tree_bytes(mc / top).items()
    }


# --------------------------------------------------------------------------- #
# S1: Replace into a fresh install
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1_replace_into_a_fresh_home_keeps_the_bot_whole(homes, tmp_path) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    names = set(archive_members(archive))
    assert not any(n.startswith(("member-records/", "crew-panels/")) for n in names), names
    assert f"members/{atlas.slug}/dashboard/instance.json" in names
    assert any(n.startswith(f"bot-artifacts/{board.package}/") for n in names)
    assert not any(n.startswith("artifacts/") for n in names), names
    homes.use("b")

    summary = apply_import_zip(archive, mode="replace")

    _assert_whole(summary, "Atlas")
    assert summary["bot_transcripts_orphaned"] == []
    assert "bots" not in summary.get("refused_merges", []), summary
    await _assert_bot_usable(atlas, board, "hello from the first install")


def _destination_with_own_data(homes: Homes) -> tuple[Path, dict[str, dict[str, bytes | None]]]:
    """Home ``b`` with an artifact, an upload and a member folder of its own, and their bytes."""
    from kiro_crew.artifacts import ArtifactStore

    mc = homes.use("b")
    ArtifactStore(mc / "artifacts").create(
        name="my notes", content="<p>mine</p>", kind="html", source="dashboard"
    )
    (mc / "uploads").mkdir(parents=True)
    (mc / "uploads" / "photo.txt").write_text("my upload", encoding="utf-8")
    (mc / "members" / "zed").mkdir(parents=True)
    (mc / "members" / "zed" / "briefing.md").write_text("Zed's own briefing", encoding="utf-8")
    return mc, _own_trees(mc)


def _own_trees(mc: Path) -> dict[str, dict[str, bytes | None]]:
    return {top: tree_bytes(mc / top) for top in ("artifacts", "uploads", "members")}


@pytest.mark.asyncio
async def test_s1_replace_keeps_every_artifact_upload_and_member_folder_here(
    homes, tmp_path
) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    mc, before = _destination_with_own_data(homes)

    summary = apply_import_zip(archive, mode="replace")

    _assert_whole(summary, "Atlas")
    after = _own_trees(mc)
    assert after["uploads"] == before["uploads"]
    for top in ("artifacts", "members"):
        assert before[top].items() <= after[top].items(), top
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
async def test_merge_skips_a_bot_whose_name_has_an_earlier_bots_file_with_grants(
    homes, tmp_path
) -> None:
    # Home b once had its own Atlas, gave it an ability and deleted it; the file stayed,
    # on record for b. A Merge must not add the archive's Atlas onto those grants (A24).
    atlas, _board, archive = await _source(homes, tmp_path)
    mc = homes.use("b")
    create_bot("Nova")
    spec = {**mate_config.minimal_mate_spec("Atlas"), "allowedTools": ["*"]}
    path = mate_config.write_mate_spec("Atlas", spec, only_if_absent=True)
    assert path is not None and mate_config.crewmate_file_on_record("Atlas")
    before = path.read_bytes()

    summary = apply_import_zip(archive, mode="merge")

    row = _row(summary, "Atlas")
    assert row["roster"] == "skipped" and "held_by_other_bot" in row["reasons"], row
    assert "Atlas" not in _config(mc)["agents"]
    assert path.read_bytes() == before
    assert not (mc / "members" / atlas.slug).exists()


# --------------------------------------------------------------------------- #
# S1b: the previous release imports a head-built archive (amendment A23)
# --------------------------------------------------------------------------- #

#: The release this branch is built on: its importer has no Bot stores.
BASE_RELEASE = "99dc8daacfb"

#: Run in the base release: import argv[1] in mode argv[2], print what ran it.
_BASE_IMPORT = """
import json, sys
from pathlib import Path
import kiro_crew
from kiro_crew.portability import apply_import_zip
summary = apply_import_zip(Path(sys.argv[1]), mode=sys.argv[2])
print(json.dumps({"module": kiro_crew.__file__, "items": summary.get("items", [])}))
"""


@pytest.fixture(scope="module")
def base_release_src(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The ``src`` directory of :data:`BASE_RELEASE`, extracted from this clone's history."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    out = tmp_path_factory.mktemp("base-release")
    tar_path = out / "base.tar"
    with tar_path.open("wb") as sink:
        done = subprocess.run(
            [
                git,
                "archive",
                "--format=tar",
                BASE_RELEASE,
                "--",
                "src/kiro_crew",
                ":(exclude)src/kiro_crew/static",
                ":(exclude)src/kiro_crew/_vendor",
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=sink,
            stderr=subprocess.PIPE,
            timeout=FALLBACK_TIMEOUT_SECS,
            check=False,
        )
    if done.returncode != 0:
        pytest.skip(f"the base release {BASE_RELEASE} is not in this clone's history")
    with tarfile.open(tar_path) as tar:
        tar.extractall(out, filter="data")
    tar_path.unlink()
    return out / "src"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_the_previous_release_importing_this_archive_keeps_everything_here(
    homes, tmp_path, base_release_src, mode
) -> None:
    _atlas, _board, archive = await _source(homes, tmp_path)
    mc, before = _destination_with_own_data(homes)
    run_dir = tmp_path / "base-run"
    run_dir.mkdir()

    done = subprocess.run(
        [sys.executable, "-s", "-c", _BASE_IMPORT, str(archive), mode],
        cwd=run_dir,
        env={**os.environ, "PYTHONPATH": str(base_release_src)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert Path(report["module"]).resolve().is_relative_to(base_release_src.resolve()), report
    # The older importer neither clears nor copies a Bot store: every artifact, upload
    # and member folder here is byte-identical, and nothing of the archive's was added.
    assert _own_trees(mc) == before, report["items"]


# --------------------------------------------------------------------------- #
# S2: Merge into an install that has Bots of its own (amendment A6)
# --------------------------------------------------------------------------- #


def _config(mc: Path) -> dict[str, Any]:
    return dict(json.loads((mc / "config.json").read_text(encoding="utf-8")))


def _destination_bot(mc: Path, kiro: Path, bot: MadeBot) -> dict[str, Any]:
    """Every store of destination Bot *bot*, as bytes or parsed config entries."""
    config = _config(mc)
    return {
        "agent": config["agents"][bot.name],
        "store": config["memory_stores"][bot.store],
        "workspace": config["workspaces"][bot.workspace],
        "member": tree_bytes(mc / "members" / bot.slug),
        "binding": (
            dm_binding_path(bot.slug).read_bytes() if dm_binding_path(bot.slug).exists() else None
        ),
        "crewmate": (kiro / "mates" / "agents" / f"{bot.name}.json").read_bytes(),
    }


@pytest.mark.asyncio
async def test_s2_merge_adds_the_free_bot_whole_and_leaves_the_destination_unchanged(
    homes, tmp_path
) -> None:
    homes.use("a")
    atlas = create_bot("Atlas")
    await chat_in_dm(atlas, "atlas on the first install")
    atlas_board = apply_dashboard(atlas, ATLAS_PAGE)
    vega = create_bot("Vega")
    await chat_in_dm(vega, "vega on the first install")
    vega_board = apply_dashboard(vega, '<p data-dashboard-field="goal">vega</p>')
    archive = export_archive(tmp_path / "a.zip")

    mc = homes.use("b")
    nova = create_bot("Nova")
    await chat_in_dm(nova, "nova lives here")
    nova_board = apply_dashboard(nova, '<p data-dashboard-field="goal">nova</p>')
    # Shares the archive Atlas's slug under another name, and was never chatted with.
    twin = create_bot("Atlas!")
    assert twin.slug == atlas.slug
    kiro = homes.kiro("b")
    before = {bot.name: _destination_bot(mc, kiro, bot) for bot in (nova, twin)}
    sessions_before = tree_bytes(mc / "sessions")
    artifacts_before = tree_bytes(mc / "artifacts" / nova_board.package)

    summary = apply_import_zip(archive, mode="merge")

    after = {bot.name: _destination_bot(mc, kiro, bot) for bot in (nova, twin)}
    assert after == before
    sessions_after = tree_bytes(mc / "sessions")
    for rel, data in sessions_before.items():
        assert sessions_after.get(rel) == data, rel
    new_sessions = sorted(set(sessions_after) - set(sessions_before))
    assert new_sessions and all(
        rel.startswith(f"dashboard_{vega.slot_key}") for rel in new_sessions
    ), new_sessions
    assert tree_bytes(mc / "artifacts" / nova_board.package) == artifacts_before
    assert not (mc / "artifacts" / atlas_board.package).exists()
    config = _config(mc)
    assert "Atlas" not in config["agents"]
    assert atlas.store not in config["memory_stores"]

    skipped = _row(summary, "Atlas")
    assert skipped["roster"] == "skipped" and skipped["reasons"] == ["slug_taken"], skipped
    assert [o["slug"] for o in summary["bot_transcripts_orphaned"]] == [atlas.slug]
    assert "Atlas!" in summary["bot_transcripts_orphaned"][0]["candidates"]
    assert summary["sessions_member_withheld"] == 1
    assert "bots" in summary["refused_merges"]

    row = _row(summary, "Vega")
    assert row["roster"] == "added", row
    assert row["binding"] == "rebuilt" and row["crewmate_file"] == "written", row
    assert row["dashboard"] == "restored" and row["reasons"] == [] and row["notices"] == [], row
    assert not [line for line in summary["items"] if line.startswith("bot Vega ")]
    await _assert_bot_usable(vega, vega_board, "vega on the first install")

    # The twin still opens as a fresh thread of its own, with no transcript.
    status, body, messages = await open_thread(twin.slug)
    assert status == 200, body
    assert body["member"] == "Atlas!" and messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_no_missing_template_warning_for_a_bot_on_its_own_crewmate_file(
    homes, tmp_path, mode
) -> None:
    # F2: a Bot runs on its own crewmate file under mates/agents/, which the
    # template lookup reads as the loader does, so the warning to copy a template
    # into the agents folder names neither an imported Bot nor one the destination
    # already had on its own crewmate file. A row naming a template that is truly
    # absent keeps the warning (the base release names it too).
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

    _atlas, _board, archive = await _source(homes, tmp_path)
    homes.use("b")
    create_bot("Nova")
    cfg = KiroCrewConfig.load()
    cfg.agents["Hand"] = KiroCrewAgentConfig(kiro_agent="hand-made-template")
    cfg.save()

    summary = apply_import_zip(archive, mode=mode)

    assert _row(summary, "Atlas")["crewmate_file"] == "written"
    named = [row["crew"] for row in summary.get("missing_agent_templates", [])]
    assert "Atlas" not in named, summary.get("missing_agent_templates")
    assert "Nova" not in named, summary.get("missing_agent_templates")
    if mode == "merge":
        assert "Hand" in named, summary.get("missing_agent_templates")
        assert "Nova" in _config(data_home())["agents"]
    # The lookup itself, with no per-import list, sees each Bot's crewmate file.
    unfiltered, _more = portability.missing_crew_templates(data_home() / "config.json")
    assert {"Atlas", "Nova"}.isdisjoint(row["crew"] for row in unfiltered)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_a_fresh_install_gets_the_default_agents_dm_chat_as_the_base_does(
    homes, tmp_path, mode
) -> None:
    # A home with no config.json still has the loader's ``default`` agent, so its
    # slug is reserved: the default agent's DM chat is installed like any chat,
    # never withheld or listed as orphaned.
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_slug, member_thread_session_alias

    homes.use("a")
    create_bot("Atlas")
    slug = member_slug("default", KiroCrewConfig.load())
    status, body, _messages = await open_thread(slug)
    assert status == 200, body
    ConversationLog(base_dir=data_home() / "sessions").append(
        member_thread_session_alias(slug, ""), "user", "hello default"
    )
    source = {
        p.name: p.read_bytes()
        for p in (data_home() / "sessions").iterdir()
        if p.name.startswith(f"dashboard_member-{slug}") and p.name.endswith(".jsonl")
    }
    assert source
    archive = export_archive(tmp_path / "a.zip")
    mc = homes.use("b")
    assert not (mc / "config.json").exists()

    summary = apply_import_zip(archive, mode=mode)

    assert not summary.get("sessions_member_withheld"), summary["items"]
    assert not [o for o in summary.get("bot_transcripts_orphaned", []) if o["slug"] == slug]
    for name, data in source.items():
        assert (mc / "sessions" / name).read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "doc",
    [{"agents": {"Local": {"kiro_agent": "local-agent"}}}, {"agents": {"x": 5}}],
    ids=["agents-without-default", "degraded-agents"],
)
async def test_a_replace_onto_a_config_without_default_installs_its_dm_chat(
    homes, tmp_path, doc
) -> None:
    # The loader always gives the install a ``default`` agent, so its slug is reserved
    # even when the destination's own document names none: the archive's default-agent
    # DM chat is installed like any chat, as the base does.
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_slug, member_thread_session_alias

    homes.use("a")
    create_bot("Atlas")
    slug = member_slug("default", KiroCrewConfig.load())
    status, body, _messages = await open_thread(slug)
    assert status == 200, body
    ConversationLog(base_dir=data_home() / "sessions").append(
        member_thread_session_alias(slug, ""), "user", "hello default"
    )
    source = {
        p.name: p.read_bytes()
        for p in (data_home() / "sessions").iterdir()
        if p.name.startswith(f"dashboard_member-{slug}") and p.name.endswith(".jsonl")
    }
    assert source
    archive = export_archive(tmp_path / "a.zip")
    mc = homes.use("b")
    mc.mkdir(parents=True, exist_ok=True)
    (mc / "config.json").write_text(json.dumps(doc), encoding="utf-8")

    summary = apply_import_zip(archive, mode="replace")

    assert not summary.get("sessions_member_withheld"), summary["items"]
    assert not [o for o in summary.get("bot_transcripts_orphaned", []) if o["slug"] == slug]
    for name, data in source.items():
        assert (mc / "sessions" / name).read_bytes() == data


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["merge", "replace"])
async def test_a_self_restore_after_losing_only_the_page_brings_it_back(
    homes, tmp_path, mode
) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    for name in _page_snapshots(atlas.slug):
        (panel_dir() / name).unlink()

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "Atlas")
    assert row["dashboard"] == "restored", row
    assert row["part_reasons"]["dashboard"] == "", row
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
async def test_a_replace_onto_another_bots_binding_withholds_the_thread(homes, tmp_path) -> None:
    # The destination's binding for the slug belongs to another Bot that had it
    # (A33): the archive Bot's transcript is withheld and listed, never installed
    # behind that binding, which is left byte for byte.
    atlas, _board, archive = await _source(homes, tmp_path)

    mc = homes.use("b")
    twin = create_bot("Atlas!")
    assert twin.slug == atlas.slug
    await chat_in_dm(twin, "the twin's own words")
    binding_before = dm_binding_path(twin.slug).read_bytes()

    summary = apply_import_zip(archive, mode="replace")

    assert dm_binding_path(twin.slug).read_bytes() == binding_before
    assert not [
        p for p in (mc / "sessions").iterdir() if p.name.startswith(f"dashboard_{atlas.slot_key}")
    ]
    assert summary["sessions_member_withheld"] == 1
    assert [o["slug"] for o in summary["bot_transcripts_orphaned"]] == [atlas.slug]
    row = _row(summary, "Atlas")
    assert row["binding"] == "not_rebuilt" and "held_by_other_bot" in row["reasons"], row
    assert "bots" in summary["refused_merges"]
    status, body, messages = await open_thread(atlas.slug)
    assert "the twin's own words" not in messages
    assert "hello from the first install" not in messages, (status, body)


# --------------------------------------------------------------------------- #
# S2b: an install imports its own export (amendments A7, A8)
# --------------------------------------------------------------------------- #


def _assert_whole_row(summary: dict[str, Any], name: str) -> dict[str, Any]:
    row = _row(summary, name)
    assert not {row[part] for part in portability_bots.PARTS} & NOT_WHOLE_STATUSES, row
    assert row["reasons"] == [], row
    assert "bots" not in summary.get("refused_merges", []), summary
    assert not [line for line in summary["items"] if line.startswith(f"bot {name} ")]
    return row


@pytest.mark.asyncio
async def test_s2b_replace_onto_the_install_that_made_the_archive(homes, tmp_path) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    binding_before = dm_binding_path(atlas.slug).read_bytes()

    summary = apply_import_zip(archive, mode="replace")

    row = _assert_whole_row(summary, "Atlas")
    assert row["binding"] == "kept", row
    assert dm_binding_path(atlas.slug).read_bytes() == binding_before
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
async def test_s2b_merge_onto_itself_keeps_the_bot_whole(homes, tmp_path) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    mc = data_home()
    config_before = (mc / "config.json").read_bytes()

    summary = apply_import_zip(archive, mode="merge")

    row = _assert_whole_row(summary, "Atlas")
    assert (row["roster"], row["binding"]) == ("kept", "kept"), row
    assert [r["bot"] for r in summary["bots"]] == ["Atlas"]
    assert summary["bot_transcripts_orphaned"] == []
    assert not summary.get("sessions_member_withheld")
    assert (mc / "config.json").read_bytes() == config_before
    await _assert_bot_usable(atlas, board, "hello from the first install")


MATE_PAGE = '<p data-dashboard-field="goal">mate</p>'


async def _used_mate(homes: Homes, tmp_path: Path) -> tuple[MadeBot, AppliedDashboard, Path]:
    """Home ``a``'s boot-time ``mate``, used: a DM chat, a dashboard and a briefing; exported."""
    from kiro_crew.members import member_dir

    homes.use("a")
    mate = boot_first_crewmate()
    await chat_in_dm(mate, "hello mate")
    board = apply_dashboard(mate, MATE_PAGE)
    (member_dir(mate.slug) / "briefing.md").write_text("# mate's briefing\n", encoding="utf-8")
    return mate, board, export_archive(tmp_path / "a.zip")


def _thread_files(mc: Path, bot: MadeBot) -> dict[str, bytes]:
    sessions = mc / "sessions"
    if not sessions.is_dir():
        return {}
    return {
        p.name: p.read_bytes()
        for p in sessions.iterdir()
        if p.is_file() and p.name.startswith(f"dashboard_{bot.slot_key}")
    }


async def _assert_mate_usable(mate: MadeBot, board: AppliedDashboard) -> None:
    from kiro_crew.members import member_dir

    status, body, messages = await open_thread(mate.slug)
    assert status == 200 and body["slot_key"] == mate.slot_key, body
    assert "hello mate" in messages, messages
    binding = read_dm_binding_for_slot(mate.slot_key)
    assert binding is not None and binding["member"] == "mate"
    status, page = await get_dashboard(mate)
    assert status == 200 and page["html"] == board.html, page
    assert (member_dir(mate.slug) / "briefing.md").read_text(encoding="utf-8") == (
        "# mate's briefing\n"
    )


def test_the_first_crewmate_is_a_bot_of_the_export() -> None:
    # A42: only ``default`` and the mate service names are reserved.
    from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME, MATE_SERVICE_AGENT_NAMES
    from kiro_crew.portability_bot_types import is_reserved_bot_name

    assert not is_reserved_bot_name(ASSISTANT_MEMBER_NAME)
    assert is_reserved_bot_name("default")
    assert all(is_reserved_bot_name(n) for n in MATE_SERVICE_AGENT_NAMES)


def _mate_here(mc: Path, mate: MadeBot) -> dict[str, Any]:
    """Every part of destination Bot *mate*, as bytes or parsed entries, and the teams."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.member_workspace import workspace_dir_name
    from kiro_crew.memory_stores import memory_stores_root

    config = _config(mc)
    return {
        "agent": config["agents"].get(mate.name),
        "store": config["memory_stores"].get(mate.store),
        "workspace": config["workspaces"].get(mate.workspace),
        "store_dir": tree_bytes(memory_stores_root() / mate.store),
        "workspace_dir": tree_bytes(config_dir() / workspace_dir_name(mate.workspace)),
        "member": tree_bytes(mc / "members" / mate.slug),
        "crewmate": mate_config.mate_config_path(mate.name).read_bytes(),
        "teams": [(t.name, list(t.members)) for t in crew_teams.read_teams()],
    }


@pytest.mark.asyncio
async def test_a_replace_onto_a_fresh_install_brings_the_archives_first_crewmate_whole(
    homes, tmp_path
) -> None:
    # A45: a Replace installs the archive config as given; the second install's own
    # boot-time crewmate file adds nothing to the worker, so it serves the imported
    # mate (kept), which arrives whole with its chat, dashboard, briefing and binding.
    mate_a, board, archive = await _used_mate(homes, tmp_path)
    members = archive_members(archive)
    assert f"members/{mate_a.slug}/dashboard/instance.json" in members
    assert f"members/{mate_a.slug}/briefing.md" in members

    mc = homes.use("b")
    mate_b = boot_first_crewmate()
    assert mate_b.store != mate_a.store
    boot_file = mate_config.mate_config_path("mate").read_bytes()
    # The boot migration's file, not the bytes of the create path's minimal file,
    # yet one that grants nothing more.
    assert json.loads(boot_file) != mate_config.minimal_mate_spec("mate")
    assert is_minimal_crewmate_file(json.loads(boot_file), "mate")

    summary = apply_import_zip(archive, mode="replace")

    row = _assert_whole_row(summary, "mate")
    assert row["roster"] == "imported", row
    assert (row["binding"], row["dashboard"], row["briefing"]) == (
        "rebuilt",
        "restored",
        "restored",
    ), row
    assert row["crewmate_file"] == "kept", row
    assert "bots" not in summary.get("refused_merges", [])
    assert _config(mc)["agents"]["mate"]["memory_store"] == mate_a.store
    assert mate_config.mate_config_path("mate").read_bytes() == boot_file
    await _assert_mate_usable(mate_a, board)


@pytest.mark.asyncio
@pytest.mark.parametrize("used", [False, True])
async def test_a_merge_never_takes_the_destinations_first_crewmate(homes, tmp_path, used) -> None:
    # A45: no heuristic can see all a Bot holds, so a Merge skips the archive's mate
    # with name_taken whatever the destination's mate looks like: one never used
    # (only on a team), or one used (a memory in its private store, and on a team).
    # Nothing of the destination's mate changes: its config, store and workspace
    # entries, their folders, its member folder, its crewmate file, its team.
    from kiro_crew.memory_stores import memory_stores_root

    _mate_a, _board, archive = await _used_mate(homes, tmp_path)
    mc = homes.use("b")
    mate_b = boot_first_crewmate()
    if used:
        memory = memory_stores_root() / mate_b.store / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (memory / "preferences.md").write_text("The owner's sister is Ana.\n", encoding="utf-8")
    crew_teams.create_team("Home", ["mate"], known=lambda: {"mate"})
    before = _mate_here(mc, mate_b)
    assert before["teams"] == [("Home", ["mate"])]

    summary = apply_import_zip(archive, mode="merge")

    row = _row(summary, "mate")
    assert (row["roster"], row["reasons"]) == ("skipped", ["name_taken"]), row
    assert row["in_roster"] is False, row
    assert "bots" in summary["refused_merges"]
    assert _mate_here(mc, mate_b) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_an_installs_own_used_first_crewmate_is_kept_and_reported(
    homes, tmp_path, mode
) -> None:
    # A45: a ``mate`` someone used here (its chat and binding) is another Bot of that
    # name: Merge reports name_taken and Replace held_by_other_bot, as for any Bot,
    # and the destination's mate is left byte-for-byte.
    _mate_a, _board, archive = await _used_mate(homes, tmp_path)
    mc = homes.use("b")
    mate_b = boot_first_crewmate()
    await chat_in_dm(mate_b, "the second install's own words")
    kept = {
        "binding": dm_binding_path(mate_b.slug).read_bytes(),
        "thread": _thread_files(mc, mate_b),
        "crewmate": mate_config.mate_config_path("mate").read_bytes(),
    }
    entry = _config(mc)["agents"]["mate"]

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "mate")
    if mode == "merge":
        assert (row["roster"], row["reasons"]) == ("skipped", ["name_taken"]), row
        assert row["in_roster"] is False
        assert _config(mc)["agents"]["mate"] == entry
    else:
        # Its file grants nothing, so it serves the imported mate; its binding does not.
        assert row["binding"] == "not_rebuilt", row
        assert row["part_reasons"]["binding"] == "held_by_other_bot", row
    assert "bots" in summary["refused_merges"]
    assert dm_binding_path(mate_b.slug).read_bytes() == kept["binding"]
    assert _thread_files(mc, mate_b) == kept["thread"]
    assert mate_config.mate_config_path("mate").read_bytes() == kept["crewmate"]


@pytest.mark.asyncio
async def test_the_first_crewmates_added_abilities_are_named_on_the_way_over(
    homes, tmp_path
) -> None:
    # A42/A45: the archive's mate had abilities added to its file; the import does
    # not carry them, so its row says it starts from the starting abilities only.
    homes.use("a")
    mate = boot_first_crewmate()
    await chat_in_dm(mate, "hello mate")
    spec = json.loads(mate_config.mate_config_path("mate").read_text(encoding="utf-8"))
    spec["skills"] = ["file://./my-skill"]
    mate_config.write_mate_spec("mate", spec)
    archive = export_archive(tmp_path / "a.zip")
    manifest = json.loads(archive_members(archive)["MANIFEST.json"])
    assert "mate" in manifest["crewmate_files_customised"]

    homes.use("b")
    boot_first_crewmate()
    # A Replace: the boot-time file it keeps grants nothing, as the minimal file.
    summary = apply_import_zip(archive, mode="replace")

    assert _row(summary, "mate")["crewmate_file"] == "kept"
    assert _row(summary, "mate")["notices"] == ["starting_abilities_only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_a_self_restore_brings_back_the_first_crewmates_lost_dm_chat(
    homes, tmp_path, mode
) -> None:
    # A42: the first crewmate is an ordinary Bot; restoring onto the install that made
    # the archive keeps it whole and puts back the chat it lost.
    mc = homes.use("a")
    mate = boot_first_crewmate()
    await chat_in_dm(mate, "hello mate")
    binding_before = dm_binding_path(mate.slug).read_bytes()
    archive = export_archive(tmp_path / "a.zip")
    thread = _thread_files(mc, mate)
    assert thread
    for name in thread:
        (mc / "sessions" / name).unlink()

    summary = apply_import_zip(archive, mode=mode)

    assert _thread_files(mc, mate) == thread
    assert dm_binding_path(mate.slug).read_bytes() == binding_before
    row = _assert_whole_row(summary, "mate")
    assert row["binding"] == "kept", row
    status, body, messages = await open_thread(mate.slug)
    assert status == 200 and "hello mate" in messages, (status, body, messages)


@pytest.mark.asyncio
async def test_the_first_crewmates_chat_onto_another_installs_mate_is_listed_not_installed(
    homes, tmp_path
) -> None:
    # A45: the archive's mate is skipped with name_taken like any Bot whose name is
    # taken here, so its chat is withheld and listed as an orphan, and the
    # destination's own mate thread and binding stay as they were.
    homes.use("a")
    mate_a = boot_first_crewmate()
    await chat_in_dm(mate_a, "hello mate")
    archive = export_archive(tmp_path / "a.zip")
    mc = homes.use("b")
    mate_b = boot_first_crewmate()
    await chat_in_dm(mate_b, "the second install's own words")
    kept = (_thread_files(mc, mate_b), dm_binding_path(mate_b.slug).read_bytes())

    summary = apply_import_zip(archive, mode="merge")

    row = _row(summary, "mate")
    assert (row["roster"], row["reasons"]) == ("skipped", ["name_taken"]), row
    assert _thread_files(mc, mate_a) == {}
    assert summary["sessions_member_withheld"] == 1
    assert [o["slug"] for o in summary["bot_transcripts_orphaned"]] == [mate_a.slug]
    assert (_thread_files(mc, mate_b), dm_binding_path(mate_b.slug).read_bytes()) == kept


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
@pytest.mark.parametrize("claimant", ["default", "mate"])
async def test_an_archive_entry_naming_another_bots_slug_opens_no_thread_on_it(
    homes, tmp_path, mode, claimant
) -> None:
    # A42: reserved slugs come from the destination's reserved names only, and a slug
    # a Bot uses is never one; so an archive entry (reserved or not) that names the
    # slug of a Bot here never lets the archive's thread install under that Bot.
    homes.use("a")
    atlas = create_bot("Atlas")
    await chat_in_dm(atlas, "hello from the first install")
    archive = rewrite_archive(
        export_archive(tmp_path / "a.zip"),
        tmp_path / "hostile.zip",
        edit={
            "config.json": lambda doc: {
                **doc,
                "agents": {**doc["agents"], claimant: {"member_id": atlas.slug}},
            }
        },
    )
    mc = homes.use("b")
    twin = create_bot("Atlas!")
    assert twin.slug == atlas.slug
    await chat_in_dm(twin, "the twin's own words")
    binding = dm_binding_path(twin.slug).read_bytes()

    summary = apply_import_zip(archive, mode=mode)

    assert dm_binding_path(twin.slug).read_bytes() == binding
    assert _thread_files(mc, atlas) == {}
    assert summary["sessions_member_withheld"] == 1, summary


@pytest.mark.asyncio
async def test_s2b_merge_of_its_own_export_restores_a_lost_dm_transcript(homes, tmp_path) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    sessions = data_home() / "sessions"
    lost = sorted(p for p in sessions.iterdir() if p.name.startswith(f"dashboard_{atlas.slot_key}"))
    assert lost
    for path in lost:
        path.unlink()

    summary = apply_import_zip(archive, mode="merge")

    assert all(path.exists() for path in lost), lost
    row = _assert_whole_row(summary, "Atlas")
    assert row["roster"] == "kept", row
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["merge", "replace"])
async def test_s2b_self_restore_claims_no_loss_for_what_is_still_here(
    homes, tmp_path, mode
) -> None:
    # A Bot with added abilities and a rules record restores onto the install that
    # made the archive: both are still here, so no notice says they were lost (A13).
    homes.use("a")
    atlas = create_bot("Atlas")
    await chat_in_dm(atlas, "hello")
    apply_dashboard(atlas, ATLAS_PAGE)
    spec = mate_config.minimal_mate_spec("Atlas")
    spec["skills"] = ["file://./my-skill"]
    mate_config.write_mate_spec("Atlas", spec)
    rules = records_dir("member-rules")
    rules.mkdir(parents=True, exist_ok=True)
    (rules / f"{atlas.slug}.json").write_text(json.dumps({"rules": ["be brief"]}))
    archive = export_archive(tmp_path / "a.zip")
    crewmate = mate_config.mate_config_path("Atlas")
    before = crewmate.read_bytes()

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "Atlas")
    assert row["crewmate_file"] == "kept", row
    assert crewmate.read_bytes() == before
    assert (rules / f"{atlas.slug}.json").is_file()
    assert row["notices"] == [], row
    assert not [line for line in summary["items"] if "notice:" in line], summary["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_a_crewmate_file_another_bot_left_is_reported_and_the_config_installed_as_given(
    homes, tmp_path, mode
) -> None:
    # A38: the destination has no Atlas, but a crewmate file with grants at Atlas's
    # name (another Bot's). The import installs the archive config as given (Replace,
    # or a Merge into an install with no config.json) and reports the file as not
    # written, held by another Bot, without rewriting it or the config.
    _atlas, _board, archive = await _source(homes, tmp_path)
    mc = homes.use("b")
    crewmate = mate_config.mate_config_path("Atlas")
    crewmate.parent.mkdir(parents=True, exist_ok=True)
    grants = json.dumps({"name": "Atlas", "allowedTools": ["*"]}).encode()
    crewmate.write_bytes(grants)

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "Atlas")
    assert (row["crewmate_file"], row["reasons"]) == ("not_written", ["held_by_other_bot"]), row
    assert crewmate.read_bytes() == grants
    installed = json.loads((mc / "config.json").read_text(encoding="utf-8"))
    assert installed["agents"]["Atlas"]["kiro_agent"] == "Atlas"
    assert any(line.startswith("bot Atlas (not whole: crewmate file") for line in summary["items"])


@pytest.mark.asyncio
async def test_s2c_replace_onto_a_home_with_another_bot_of_that_name(homes, tmp_path) -> None:
    # Both installs made their own "Atlas" (same name and slug, different store). The
    # Replace installs the archive's Atlas, and every entry the other Atlas left at
    # the shared paths is reported as held by that Bot, never adopted (A12).
    atlas_a, board_a, archive = await _source(homes, tmp_path)
    homes.use("b")
    atlas_b = create_bot("Atlas")
    board_b = apply_dashboard(atlas_b, '<p data-dashboard-field="goal">BBB</p>')
    assert atlas_b.slug == atlas_a.slug and atlas_b.store != atlas_a.store
    crewmate = mate_config.mate_config_path("Atlas")
    crewmate_before = crewmate.read_bytes()

    summary = apply_import_zip(archive, mode="replace")

    row = _row(summary, "Atlas")
    assert row["dashboard"] == "not_restored", row
    # The other Atlas's crewmate file is exactly the minimal file: it grants nothing,
    # so it serves the archive's Atlas as well (A24).
    assert row["crewmate_file"] == "kept", row
    assert "held_by_other_bot" in row["reasons"], row
    assert crewmate.read_bytes() == crewmate_before
    status, page = await get_dashboard(atlas_a)
    # The other Atlas's dashboard is left as it was, and is not passed off as restored.
    assert (status, page.get("html")) == (200, board_b.html), page
    assert any(line.startswith("bot Atlas (not whole:") for line in summary["items"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["replace", "merge"])
async def test_s2e_restore_into_a_fresh_data_home_on_the_same_kiro_home(
    homes, tmp_path, mode
) -> None:
    # The data home is lost and made again at the same path; the Kiro home kept the
    # crewmate file this instance wrote, which the owner record names (A20).
    atlas, board, archive = await _source(homes, tmp_path)
    crewmate = mate_config.mate_config_path("Atlas")
    before = crewmate.read_bytes()
    mc = data_home()
    shutil.rmtree(mc)
    mc.mkdir(parents=True)
    _invalidate_config_cache()

    summary = apply_import_zip(archive, mode=mode)

    row = _assert_whole_row(summary, "Atlas")
    assert row["crewmate_file"] == "kept", row
    assert crewmate.read_bytes() == before
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["merge", "replace"])
async def test_a_crewmate_file_another_data_home_owns_is_never_adopted(
    homes, tmp_path, mode
) -> None:
    # A29: a second data home on the same machine (one shared Kiro home) imports the
    # first one's export. Atlas's minimal crewmate file is the first home's by the
    # owner record, so a Merge never adds Atlas onto it, and a Replace reports it.
    _atlas, _board, archive = await _source(homes, tmp_path)
    crewmate = mate_config.mate_config_path("Atlas")
    before = crewmate.read_bytes()
    first_home = str(data_home().resolve())
    homes.mp.setenv("KIROCREW_HOME", str(tmp_path / "homes" / "b" / "crew"))
    _invalidate_config_cache()
    data_home().mkdir(parents=True, exist_ok=True)
    create_bot("Nova")

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "Atlas")
    assert "foreign_owner" in row["reasons"], row
    _invalidate_config_cache()
    from kiro_crew.config.loader import KiroCrewConfig

    agents = KiroCrewConfig.load().agents
    if mode == "merge":
        assert row["roster"] == "skipped", row
        assert "Atlas" not in agents
    else:
        assert row["crewmate_file"] == "not_written", row
    assert crewmate.read_bytes() == before
    owners = json.loads(mate_config._owners_path().read_text(encoding="utf-8"))
    assert owners["Atlas"] == first_home


@pytest.mark.asyncio
async def test_s2e_merge_keeps_the_archive_teams_of_the_bots_it_adds(homes, tmp_path) -> None:
    # The Merge installs the archive's team list (this home had none): its memberships
    # are the added Bots' own, so adding them releases no name from a team (A21).
    homes.use("a")
    create_bot("Atlas")
    create_bot("Nova")
    crew_teams.create_team("T", ["Atlas", "Nova"], known=lambda: {"Atlas", "Nova"})
    archive = export_archive(tmp_path / "a.zip")
    homes.use("b")
    create_bot("Bea")
    assert crew_teams.read_teams() == []

    summary = apply_import_zip(archive, mode="merge")

    assert "crew-teams (copied)" in summary["items"]
    assert {_row(summary, n)["roster"] for n in ("Atlas", "Nova")} == {"added"}, summary["bots"]
    [team] = crew_teams.read_teams()
    assert (team.name, team.members) == ("T", ["Atlas", "Nova"])


# --------------------------------------------------------------------------- #
# S2d: a version-1 Bot restores onto itself (A17), a lost binding (A18), and
# honest accounting (A19)
# --------------------------------------------------------------------------- #


def _v1_bot(name: str = "Legacy") -> Path:
    """A version-1 Bot (no member_id, no store of its own) on the current home."""
    mc = data_home()
    mc.mkdir(parents=True, exist_ok=True)
    (mc / "config.json").write_text(
        json.dumps({"agents": {name: {"kiro_agent": name, "workspace": "default"}}}),
        encoding="utf-8",
    )
    from kiro_crew.config.loader import _invalidate_config_cache

    _invalidate_config_cache()
    return mc


def _v1_transcripts(mc: Path) -> list[Path]:
    return sorted(
        p
        for p in (mc / "sessions").iterdir()
        if p.name.startswith("dashboard_member-") and p.name.endswith(".jsonl")
    )


@pytest.mark.asyncio
async def test_s2d_v1_bot_merge_of_its_own_export_restores_its_lost_transcript(
    homes, tmp_path
) -> None:
    """A17: a self-restore puts back what this install had; A30 withholds only for a
    Bot the destination did not have."""
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_thread_session_alias

    homes.use("a")
    mc = _v1_bot()
    ConversationLog(base_dir=mc / "sessions").append(
        member_thread_session_alias("legacy"), "user", "hello legacy"
    )
    lost = _v1_transcripts(mc)
    assert lost
    archive = export_archive(tmp_path / "a.zip")
    for path in lost:
        path.unlink()

    summary = apply_import_zip(archive, mode="merge")

    assert _v1_transcripts(mc) == lost
    row = _row(summary, "Legacy")
    assert row["roster"] == "kept", row
    # Never given a binding it did not have (R5 a).
    assert row["binding"] == "not_rebuilt" and "no_member_id" in row["reasons"], row
    assert not dm_binding_path("legacy").exists()
    assert not summary.get("sessions_member_withheld")
    assert summary["bot_transcripts_orphaned"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["merge", "replace"])
async def test_s2d_v1_bot_self_restore_adopts_its_own_binding(homes, tmp_path, mode) -> None:
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_thread_session_alias

    homes.use("a")
    mc = _v1_bot()
    status, body, _messages = await open_thread("legacy")
    assert status == 200, body
    ConversationLog(base_dir=mc / "sessions").append(
        member_thread_session_alias("legacy"), "user", "hello legacy"
    )
    binding_before = dm_binding_path("legacy").read_bytes()
    archive = export_archive(tmp_path / "a.zip")
    for path in _v1_transcripts(mc):
        path.unlink()

    summary = apply_import_zip(archive, mode=mode)

    row = _row(summary, "Legacy")
    assert row["binding"] == "kept", row
    assert "held_by_other_bot" not in row["reasons"], row
    if mode == "merge":
        assert row["roster"] == "kept", row
    assert dm_binding_path("legacy").read_bytes() == binding_before
    status, body, messages = await open_thread("legacy")
    assert status == 200, body
    assert "hello legacy" in messages, messages


@pytest.mark.asyncio
async def test_s2d_merge_of_its_own_export_rebuilds_a_lost_binding(homes, tmp_path) -> None:
    # The transcript is still at the Bot's own slot; only the binding was lost (A18).
    atlas, board, archive = await _source(homes, tmp_path)
    dm_binding_path(atlas.slug).unlink()

    summary = apply_import_zip(archive, mode="merge")

    row = _row(summary, "Atlas")
    assert (row["roster"], row["binding"]) == ("kept", "rebuilt"), row
    assert "transcript_predates_import" not in row["reasons"], row
    await _assert_bot_usable(atlas, board, "hello from the first install")


@pytest.mark.asyncio
async def test_s2d_a_withheld_thread_the_install_still_holds_is_already_here(
    homes, tmp_path
) -> None:
    # Atlas left the roster but its store and transcript are still here: the
    # archive's Atlas is skipped and its thread withheld, and nothing was lost (A19).
    atlas, _board, archive = await _source(homes, tmp_path)
    mc = data_home()
    from kiro_crew.config.loader import _invalidate_config_cache, update_config_locked

    def _drop(doc: dict[str, Any]) -> dict[str, Any]:
        doc["agents"].pop("Atlas")
        doc["memory_stores"].pop(atlas.store)
        return doc

    update_config_locked(mc / "config.json", mutate=_drop)
    _invalidate_config_cache()

    summary = apply_import_zip(archive, mode="merge")

    assert _row(summary, "Atlas")["roster"] == "skipped"
    assert summary["sessions_member_withheld"] == 0
    assert summary["sessions_skipped_existing"] >= 1
    assert not any("Bot threads withheld" in line for line in summary["items"])


@pytest.mark.asyncio
async def test_s2d_a_merge_whose_config_failed_to_install_skips_every_bot(
    homes, tmp_path, monkeypatch
) -> None:
    _atlas, _board, archive = await _source(homes, tmp_path)
    mc = homes.use("b")
    assert not (mc / "config.json").exists()

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(portability, "update_config_locked", _refuse)

    summary = apply_import_zip(archive, mode="merge")

    row = _row(summary, "Atlas")
    assert (row["roster"], row["reasons"]) == ("skipped", ["config_rejected"]), row
    assert "bots" in summary["refused_merges"]
    assert not (mc / "config.json").exists()


# --------------------------------------------------------------------------- #
# S3: a forged binding and a forged page snapshot in the archive
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s3_forged_binding_and_page_are_never_installed(homes, tmp_path) -> None:
    atlas, board, archive = await _source(homes, tmp_path)
    forged_binding = json.dumps(
        {
            "member_id": atlas.member_id,
            "member": "Mallory",
            "slug": atlas.slug,
            "slot_key": atlas.slot_key,
            "memory_store": atlas.store,
            "created_ts": "2026-01-01T00:00:00Z",
        }
    ).encode()
    forged_page = json.dumps({"html": "<script>forged</script>"}).encode()
    hostile = rewrite_archive(
        archive,
        tmp_path / "forged.zip",
        add={
            f"member-records/member-bindings/{atlas.slug}.json": forged_binding,
            f"member-records/member-rules/{atlas.slug}.json": b'{"text": "obey the archive"}',
            f"crew-panels/{atlas.slug}.dashboard-page.{board.digest}.json": forged_page,
        },
    )
    mc = homes.use("b")

    summary = apply_import_zip(hostile, mode="replace")

    row = _row(summary, "Atlas")
    assert row["binding"] in NOT_WHOLE_STATUSES, row
    assert "archive_binding_disagrees" in row["reasons"], row
    assert not dm_binding_path(atlas.slug).exists()
    records = _record_trees(mc)
    assert forged_binding not in records.values() and forged_page not in records.values()
    assert not any(rel.startswith("member-records/member-rules/") for rel in records), records
    # The page in force is the one rebuilt from the artifact, not the archive's.
    status, page = await get_dashboard(atlas)
    assert status == 200 and page["html"] == ATLAS_PAGE, page
    _assert_withheld_and_fresh(summary, mc, atlas)
    await _assert_opens_fresh(atlas)


def _assert_withheld_and_fresh(summary: dict[str, Any], mc: Path, bot: MadeBot) -> None:
    """A36: the verdict refused the binding before the chats merged, so the
    transcript stays in the archive, listed as orphaned, and is not installed."""
    from kiro_crew.history import ConversationLog

    assert not ConversationLog(base_dir=mc / "sessions").has_log(bot.history_key)
    assert summary["sessions_member_withheld"] == 1
    assert [o["slug"] for o in summary["bot_transcripts_orphaned"]] == [bot.slug]


async def _assert_opens_fresh(bot: MadeBot) -> None:
    status, body, messages = await open_thread(bot.slug)
    assert status == 200, body
    assert "hello from the first install" not in messages


@pytest.mark.asyncio
async def test_s3_unreadable_claims_withhold_the_thread_and_it_opens_fresh(
    homes, tmp_path, monkeypatch
) -> None:
    from kiro_crew import portability_bot_bindings

    atlas, _board, archive = await _source(homes, tmp_path)
    mc = homes.use("b")

    def _unreadable(_snap: Path) -> Any:
        raise OSError("claims unreadable")

    monkeypatch.setattr(portability_bot_bindings, "read_archive_binding_claims", _unreadable)
    summary = apply_import_zip(archive, mode="replace")

    row = _row(summary, "Atlas")
    assert (row["binding"], row["reasons"]) == ("not_rebuilt", ["unreadable_record"]), row
    assert not dm_binding_path(atlas.slug).exists()
    _assert_withheld_and_fresh(summary, mc, atlas)
    await _assert_opens_fresh(atlas)


# --------------------------------------------------------------------------- #
# S4: a Bot with no member_id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s4_bot_without_member_id_gets_no_binding_and_opens_fresh(homes, tmp_path) -> None:
    """A30: the refusal is known before the chats merge, so the transcript is withheld
    (listed as orphaned, still in the archive) and the thread opens empty, not 409."""
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_thread_session_alias

    key = member_thread_session_alias("old")

    def _old_bot_with_a_thread() -> None:
        """A version-1 Bot (no member_id) with a DM transcript at its derived key."""
        cfg = KiroCrewConfig.load()
        cfg.agents["Old"] = KiroCrewAgentConfig(kiro_agent="Old")
        cfg.save()
        assert KiroCrewConfig.load().agents["Old"].member_id == ""
        log = ConversationLog(base_dir=data_home() / "sessions")
        log.append(key, "user", "old words")
        log.update_metadata(key, {"agent": "Old", "mode": DM_SLOT_MODE})

    homes.use("a")
    await asyncio.to_thread(_old_bot_with_a_thread)
    archive = export_archive(tmp_path / "a.zip")
    mc = homes.use("b")

    summary = apply_import_zip(archive, mode="replace")

    row = _row(summary, "Old")
    assert row["binding"] in NOT_WHOLE_STATUSES and "no_member_id" in row["reasons"], row
    assert not dm_binding_path("old").exists()
    assert not ConversationLog(base_dir=mc / "sessions").has_log(key)
    assert summary["sessions_member_withheld"] == 1
    assert [o["slug"] for o in summary["bot_transcripts_orphaned"]] == ["old"]
    status, body, messages = await open_thread("old")
    assert status == 200, body
    assert "old words" not in messages


# --------------------------------------------------------------------------- #
# S5: stray and escaping crewmate files
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s5_no_archive_member_becomes_a_crewmate_file(homes, tmp_path) -> None:
    atlas, _board, archive = await _source(homes, tmp_path)
    grants = json.dumps({"name": "Atlas", "allowedTools": ["*"], "tools": ["*"]}).encode()

    def _escaping_names(config: dict[str, Any]) -> dict[str, Any]:
        for name in ("../escape", ".hidden", "a/b"):
            config["agents"][name] = {"kiro_agent": name}
        return config

    hostile = rewrite_archive(
        archive,
        tmp_path / "stray.zip",
        add={
            "mates/agents/Atlas.json": grants,
            "mates/agents/Stray.json": grants,
            "kiro/mates/agents/Stray.json": grants,
            f"members/{atlas.slug}/Atlas.json": grants,
        },
        edit={"config.json": _escaping_names},
    )
    homes.use("b")
    kiro = homes.kiro("b")
    kiro_before = tree_bytes(kiro)

    summary = apply_import_zip(hostile, mode="replace")

    written = {
        rel: data for rel, data in tree_bytes(kiro).items() if kiro_before.get(rel, b"") != data
    }
    # Only the minimal file for the safe imported Bot, and the owner record it claims.
    assert set(written) - {"mates", "mates/agents"} == {
        "mates/agents/Atlas.json",
        "mates/owners.json",
    }, sorted(written)
    _assert_crewmate_minimal("Atlas")
    assert grants not in written.values()
    assert _row(summary, "Atlas")["crewmate_file"] == "written"
    for name in ("../escape", ".hidden", "a/b"):
        row = _row(summary, name)
        assert (row["crewmate_file"], row["reasons"]) == ("not_written", ["unsafe_name"]), row
    assert not (tmp_path / "homes" / "b" / "escape.json").exists()
    assert not list((tmp_path / "homes").rglob("Stray.json"))


# --------------------------------------------------------------------------- #
# S6: an archive from the previous release
# --------------------------------------------------------------------------- #


def _previous_release(manifest: dict[str, Any]) -> dict[str, Any]:
    for key in list(manifest):
        if key.startswith(("bot_", "member_records_", "crewmate_files_")):
            manifest.pop(key)
    manifest.get("contents", {}).pop("bot_store_files", None)
    return manifest


@pytest.mark.asyncio
async def test_s6_previous_release_archive_imports_and_says_dashboards_were_not_carried(
    homes, tmp_path
) -> None:
    atlas, _board, archive = await _source(homes, tmp_path)
    old = rewrite_archive(
        archive,
        tmp_path / "old.zip",
        drop=lambda rel: rel.startswith(("members/", "bot-artifacts/")),
        edit={"MANIFEST.json": _previous_release},
    )
    assert "bot_stores" not in json.loads(archive_members(old)["MANIFEST.json"])
    mc = homes.use("b")

    summary = apply_import_zip(old, mode="replace")

    assert any("not carried" in line and "dashboards" in line for line in summary["items"])
    row = _row(summary, "Atlas")
    assert row["binding"] == "rebuilt" and row["crewmate_file"] == "written", row
    assert not (mc / "members" / atlas.slug / "dashboard").exists()
    assert not (mc / "artifacts").exists() or not any((mc / "artifacts").iterdir())
    status, body, messages = await open_thread(atlas.slug)
    assert status == 200, body
    assert "hello from the first install" in messages
    _assert_crewmate_minimal("Atlas")


# --------------------------------------------------------------------------- #
# S7: memory only
# --------------------------------------------------------------------------- #


class _FrozenDatetime:
    """``portability.datetime`` with ``now`` fixed, so two exports can be compared."""

    real = portability.datetime

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        return cls.real(2026, 1, 2, 3, 4, 5, tzinfo=tz)


def _no_bot_work(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every Bot step record its call and raise, as if the code were not there."""
    calls: list[str] = []

    def _refuse(name: str) -> Any:
        def _call(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} ran for a memory-only archive")

        return _call

    for module, names in (
        (portability_bot_export, ("load_roster", "iter_bot_store_files", "bot_manifest_fields")),
        (
            portability_bots,
            ("collect_claims", "merge_roster_step", "prepare_bots", "restore_bots"),
        ),
    ):
        for name in names:
            monkeypatch.setattr(module, name, _refuse(name))
    return calls


@pytest.mark.asyncio
async def test_s7_memory_only_does_no_bot_work(homes, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(portability, "datetime", _FrozenDatetime)
    # The module's own clock bindings: the manifest's stamp and its member time.
    frozen = types.SimpleNamespace(time=lambda: 1767323045.0, localtime=time.gmtime)
    monkeypatch.setattr(portability_stream, "time", frozen)
    _atlas, _board, _full = await _source(homes, tmp_path)
    with_bots = export_archive(tmp_path / "memory.zip", memory_only=True)
    homes.use("b")
    with_bots_summary = apply_import_zip(with_bots, mode="merge", memory_only=True)

    calls = _no_bot_work(monkeypatch)
    homes.use("a")
    without_bots = export_archive(tmp_path / "memory-base.zip", memory_only=True)
    homes.use("c")
    without_bots_summary = apply_import_zip(without_bots, mode="merge", memory_only=True)

    assert calls == []
    assert archive_members(with_bots) == archive_members(without_bots)
    members = archive_members(with_bots)
    assert not any(
        n.startswith(("members/", "artifacts/", "bot-artifacts/", "sessions/")) for n in members
    )
    manifest = json.loads(members["MANIFEST.json"])
    assert not [k for k in manifest if k.startswith(("bot_", "member_records", "crewmate_files"))]
    assert with_bots_summary == without_bots_summary
    for key in ("bots", "bot_transcripts_orphaned", "sessions_member_withheld"):
        assert key not in with_bots_summary

"""On-demand minting: the ephemeral agent-spec files a mint writes, and their cleanup.

Cleanup only ever deletes a path this gateway recorded creating; a name's shape
authorizes nothing. A row inside the TTL survives the self-heal, a failed unlink
keeps its row for a later sweep, and a planted absolute, traversal or symlinked
row is never followed. A spec is written under a name unique to its flow and
never over an existing path, and a mint's PID is shielded while its readiness
still stalls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import connections_mint_helpers
import pytest
from connections_mint_helpers import _URL

from conftest import requires_symlinks
from kiro_crew import mcp_grant
from kiro_crew.connections import mint
from kiro_crew.runtime_ownership import RUNTIME_TENANCY, release_runtime_tenancy
from kiro_crew.testing.wait import async_wait_until, await_until

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_mint = connections_mint_helpers._isolated_mint


# ── ephemeral spec lifecycle ──
#
# The invariant under test: cleanup only ever deletes a path this gateway
# RECORDED CREATING. Name shape authorizes nothing.


def test_cleanup_never_touches_a_file_it_did_not_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manifest = tmp_path / "mint-specs.json"
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    old = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 60
    # Aged files that LOOK exactly like ours, including the pid-token shape a
    # previous round matched on. None of them is in the manifest.
    lookalikes = [
        tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json",
        tmp_path / "kirocrew-mint-notion-1234-deadbeef.json",
        tmp_path / "kirocrew-mint-notion.json",
    ]
    for path in lookalikes:
        path.write_text("USER FILE", encoding="utf-8")
        os.utime(path, (old, old))

    mint._sweep_mint_specs()

    for path in lookalikes:
        assert path.read_text(encoding="utf-8") == "USER FILE", path.name


def test_the_self_heal_leaves_rows_still_inside_the_ttl_window(_isolated_mint: Path):
    live = _mint_shaped(_isolated_mint, alias="linear")
    live.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(live): time.time()})

    mint._sweep_mint_specs()

    # Age is the trigger; a row inside the window belongs to a mint that may still
    # be running, so it is left alone even though every conjunct holds.
    assert live.is_file()
    assert str(live) in mint._read_mint_manifest()


def test_releasing_a_spec_drops_its_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    manifest = tmp_path / "mint-specs.json"
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    mine = tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json"
    mine.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(mine): time.time()})

    mint._remove_mint_agent_spec(str(mine))

    assert not mine.exists()
    assert mint._read_mint_manifest() == {}


def test_an_unreadable_manifest_reads_as_empty_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    manifest = tmp_path / "mint-specs.json"
    manifest.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: manifest)
    bystander = tmp_path / "bystander.json"
    bystander.write_text("{}", encoding="utf-8")

    assert mint._read_mint_manifest() == {}
    mint._sweep_mint_specs()

    assert bystander.is_file()


AGED = -(mint._MINT_SPEC_ORPHAN_SECONDS + 60)


def _plant_row(path: Path) -> None:
    """Record ``path`` as a manifest row old enough for the self-heal to consider."""
    mint._write_mint_manifest({str(path): time.time() + AGED})


def _mint_shaped(name_dir: Path, alias: str = "notion") -> Path:
    return name_dir / f"kirocrew-mint-{alias}-{os.getpid()}-abcdef12.json"


@pytest.mark.asyncio
async def test_the_mint_pid_is_protected_while_readiness_is_still_stalled(monkeypatch):
    # The child is spawned partway THROUGH ensure_ready. Nothing claims it as a
    # session, so until it is registered the orphan sweep can kill a mint that is
    # still initializing -- waiting for readiness leaves that whole window open.
    protected: list[int] = []
    monkeypatch.setattr(mint, "register_protected_pid", protected.append)
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url: False)
    monkeypatch.setattr(mint, "_write_mint_agent_spec", lambda slug: ("agent", "/tmp/s.json"))

    ready = asyncio.Event()

    class _SlowReady:
        def __init__(self, **kwargs):
            self._pid = 0

        async def ensure_ready(self):
            # The spawn assigns the PID, then initialization drags on.
            self._pid = 4242
            # flake-ok: pre-existing unbounded wait in the stubbed readiness; the finally sets ready itself; moved verbatim, tracked for a de-flake pass
            await ready.wait()

        def pop_pending_oauth_requests(self):
            return []

    monkeypatch.setattr(mint, "_acp_client_factory", lambda: _SlowReady)

    async def _fake_dispose(holdings):
        # The real dispose is the ONLY thing that releases the tenancy the pid
        # claim above took on the process-global ``RUNTIME_TENANCY`` table, and
        # ``_SlowReady`` answers neither ``is_alive`` nor ``is_process_alive``, so
        # the table would hold pid 4242 "alive" for the rest of the worker: every
        # later test on it that asks the kill gate about 4242 (``test_cron_reaper_failed_kill``
        # fabricates that pid) is then refused with "runtime still leased by
        # another tenant". Stubbing the releaser means owing the release.
        release_runtime_tenancy(holdings.pop("tenancy", "") or None)

    monkeypatch.setattr(mint, "_dispose_mint", _fake_dispose)
    flow = asyncio.get_running_loop().create_task(mint.start_oauth_mint("notion", _URL))
    try:
        # Readiness has NOT completed, yet the PID is already shielded.
        await async_wait_until(lambda: protected == [4242])
    finally:
        ready.set()
        await await_until(flow)
        mint._mints.pop("notion", None)
    assert RUNTIME_TENANCY.claims_on_pid(4242) == 0, "the mint's tenancy outlived the mint"


async def _until(predicate, interval: float = 0.01) -> None:
    """Poll until the predicate holds. Bounded by the caller's wait_for."""
    while not predicate():
        # flake-ok: pre-existing 10 ms poll interval of the _until helper, which no test calls; its docstring leaves the bound to the caller; moved verbatim, tracked for a de-flake pass
        await asyncio.sleep(interval)


def test_a_failed_release_unlink_keeps_its_manifest_row(monkeypatch):
    # The row is the only thing authorizing a delete of this file, so dropping it
    # on a failed unlink strands a real spec that no later sweep can see.
    agents_dir = mint._agent.kiro_agents_dir_path()
    spec = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    spec.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(spec): time.time()})

    real_unlink = Path.unlink

    def _refuse(self, *a, **k):
        if self == spec:
            raise OSError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _refuse)
    mint._remove_mint_agent_spec(str(spec))

    # Row retained and the file still there: a retry is still possible.
    assert str(spec) in mint._read_mint_manifest()
    assert spec.exists()


def test_a_successful_release_drops_its_manifest_row():
    agents_dir = mint._agent.kiro_agents_dir_path()
    spec = agents_dir / "kirocrew-mint-notion-4243-abcdef02.json"
    spec.write_text("{}", encoding="utf-8")
    mint._write_mint_manifest({str(spec): time.time()})

    mint._remove_mint_agent_spec(str(spec))

    assert not spec.exists()
    assert str(spec) not in mint._read_mint_manifest()


def test_a_hard_stop_between_the_row_and_the_file_leaves_only_a_harmless_row(monkeypatch):
    # Row-before-file: if the process dies after the row lands and before the spec
    # is published, what survives is bookkeeping, not a selectable agent. The
    # opposite order leaves a file no sweep can ever see.
    agents_dir = mint._agent.kiro_agents_dir_path()
    (agents_dir / mint.AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {mint.mcp_server_alias("notion"): {"url": _URL}}}),
        encoding="utf-8",
    )

    class _HardStop(BaseException):
        pass

    # Both writers are patched: the manifest row goes through the guarded
    # primitive and the spec through the authorized entry point, and this test
    # needs the first to land and the second to die.
    real_write = mint._agent._atomic_json_write
    real_spec_write = mint._agent.write_owner_derived_spec

    def _die_on_the_spec(path, data, *a, **k):
        # Only the spec publish dies; the manifest write must still land, which is
        # the whole point of doing it first.
        if Path(path).name.startswith("kirocrew-mint-"):
            raise _HardStop()
        return real_write(path, data, *a, **k)

    def _die_on_the_spec_write(path, data, *a, **k):
        if Path(path).name.startswith("kirocrew-mint-"):
            raise _HardStop()
        return real_spec_write(path, data, *a, **k)

    monkeypatch.setattr(mint._agent, "_atomic_json_write", _die_on_the_spec)
    monkeypatch.setattr(mint._agent, "write_owner_derived_spec", _die_on_the_spec_write)
    with pytest.raises(_HardStop):
        mint._write_mint_agent_spec("notion")

    # The row exists; the file does not.
    rows = mint._read_mint_manifest()
    assert len(rows) == 1
    recorded = next(iter(rows))
    assert not Path(recorded).exists()

    # Age it past the orphan window and sweep: the row goes, nothing is stranded.
    mint._write_mint_manifest({recorded: time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1})
    mint._sweep_mint_specs()
    assert recorded not in mint._read_mint_manifest()


def test_a_row_survives_a_failed_unlink_so_a_later_sweep_retries(monkeypatch):
    # Dropping the row on a failed unlink would abandon a real file that nothing
    # else is authorized to delete -- the row IS the authorization.
    agents_dir = mint._agent.kiro_agents_dir_path()
    orphan = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    orphan.write_text("{}", encoding="utf-8")
    aged = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1
    mint._write_mint_manifest({str(orphan): aged})

    real_unlink = Path.unlink
    refusing = [True]

    def _refuse(self, *a, **k):
        if refusing[0] and self == orphan:
            raise OSError("busy")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", _refuse)
    mint._sweep_mint_specs()
    # Row retained, file still there: the sweep gave up on the file, not the row.
    assert str(orphan) in mint._read_mint_manifest()
    assert orphan.exists()

    # The next sweep retries, because the row was kept as its authorization.
    refusing[0] = False
    mint._sweep_mint_specs()
    assert not orphan.exists()
    assert str(orphan) not in mint._read_mint_manifest()


def test_a_spec_whose_row_cannot_be_recorded_is_not_left_behind(monkeypatch, tmp_path):
    # A swallowed manifest-write error would leave the file with no row: invisible
    # to the sweep forever, and still selectable, since agent discovery globs every
    # JSON in the agents dir. The file must not outlive its row.
    agents_dir = mint._agent.kiro_agents_dir_path()
    (agents_dir / mint.AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {mint.mcp_server_alias("notion"): {"url": _URL}}}),
        encoding="utf-8",
    )
    # A real write failure, not a stubbed return: the manifest's parent cannot be
    # created because a regular file occupies the path.
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(mint, "_mint_manifest_path", lambda: blocker / "sub" / "m.json")

    with pytest.raises(OSError):
        mint._write_mint_agent_spec("notion")

    # Nothing of ours is left in the agents dir.
    assert not list(agents_dir.glob("kirocrew-mint-*.json"))


@pytest.mark.asyncio
async def test_a_reconnect_that_short_circuits_still_reaps_aged_orphans(monkeypatch):
    # An orphan too young at its last sweep stays behind. Once a grant exists every
    # future reconnect returns before any spec write, so unless the sweep runs
    # first the row is never looked at again.
    agents_dir = mint._agent.kiro_agents_dir_path()
    orphan = agents_dir / "kirocrew-mint-notion-4242-abcdef01.json"
    orphan.write_text("{}", encoding="utf-8")
    aged = time.time() - mint._MINT_SPEC_ORPHAN_SECONDS - 1
    mint._write_mint_manifest({str(orphan): aged})
    monkeypatch.setattr(mcp_grant, "grant_presence", lambda url: True)

    await mint.start_oauth_mint("notion", _URL)

    # The short-circuit still ran the sweep, so the orphan and its row are gone.
    assert not orphan.exists()
    assert str(orphan) not in mint._read_mint_manifest()
    assert mint._mints["notion"]["state"] == "granted"
    mint._mints.pop("notion", None)


def test_a_planted_absolute_victim_path_is_never_unlinked(_isolated_mint: Path, tmp_path: Path):
    victim = tmp_path / "precious.json"
    victim.write_text("VICTIM", encoding="utf-8")
    _plant_row(victim)

    mint._sweep_mint_specs()

    # Conjunct 2: the resolved path is not inside the agents dir.
    assert victim.read_text(encoding="utf-8") == "VICTIM"
    assert str(victim) not in mint._read_mint_manifest()


def test_a_traversal_row_is_never_unlinked(_isolated_mint: Path, tmp_path: Path):
    victim = _mint_shaped(tmp_path)
    victim.write_text("VICTIM", encoding="utf-8")
    _plant_row(_isolated_mint / ".." / victim.name)

    mint._sweep_mint_specs()

    # Conjunct 2: resolve() collapses the `..` before containment is checked, so a
    # mint-shaped name one level up is still outside.
    assert victim.read_text(encoding="utf-8") == "VICTIM"


@requires_symlinks
def test_a_symlink_redirecting_outside_the_agents_dir_is_never_followed(
    _isolated_mint: Path, tmp_path: Path
):
    victim = tmp_path / "outside.json"
    victim.write_text("VICTIM", encoding="utf-8")
    link = _mint_shaped(_isolated_mint)
    link.symlink_to(victim)
    _plant_row(link)

    mint._sweep_mint_specs()

    # Conjunct 2: resolve() follows the link first, so the target's real location is
    # what containment sees -- neither the link nor its target is unlinked.
    assert victim.read_text(encoding="utf-8") == "VICTIM"
    assert link.is_symlink()


def test_a_planted_row_naming_a_real_agent_spec_is_never_unlinked(_isolated_mint: Path):
    handwritten = _isolated_mint / "my-own-agent.json"
    handwritten.write_text("VICTIM", encoding="utf-8")
    _plant_row(handwritten)

    mint._sweep_mint_specs()

    # Conjunct 3: inside the agents dir, but not this module's name shape.
    assert handwritten.read_text(encoding="utf-8") == "VICTIM"


def test_a_planted_row_naming_one_of_our_managed_specs_is_never_unlinked(
    monkeypatch: pytest.MonkeyPatch, _isolated_mint: Path
):
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    owned = _isolated_mint / OWNED_KIRO_AGENT_FILES[0]
    owned.write_text("VICTIM", encoding="utf-8")
    # Force the name-shape conjunct to pass so conjunct 4 is what refuses.
    monkeypatch.setattr(mint, "_MINT_NAME_RE", re.compile(r".*"))
    _plant_row(owned)

    mint._sweep_mint_specs()

    # Conjunct 4: our own long-lived managed specs are never mint leftovers.
    assert owned.read_text(encoding="utf-8") == "VICTIM"


def test_a_genuine_stale_mint_spec_is_still_reaped(_isolated_mint: Path):
    stranded = _mint_shaped(_isolated_mint)
    stranded.write_text("{}", encoding="utf-8")
    _plant_row(stranded)

    mint._sweep_mint_specs()

    # All four conjuncts hold, so the orphan self-heal still works -- the point of
    # not adopting "drop rows, never unlink".
    assert not stranded.exists()
    assert str(stranded) not in mint._read_mint_manifest()


def test_an_unrecorded_mint_shaped_file_in_the_agents_dir_is_never_unlinked(
    _isolated_mint: Path,
):
    # Satisfies conjuncts 2, 3 and 4 -- inside the agents dir, our name shape, not
    # an owned spec -- and is still refused, because it is in no manifest of ours.
    # This is a SIBLING GATEWAY's live mint spec: its manifest is not ours to read,
    # so unlinking it would break a mint that is mid-activation elsewhere.
    sibling = _mint_shaped(_isolated_mint, alias="linear")
    sibling.write_text("SIBLING", encoding="utf-8")
    old = time.time() + AGED
    os.utime(sibling, (old, old))

    mint._sweep_mint_specs()

    assert sibling.read_text(encoding="utf-8") == "SIBLING"


@requires_symlinks
def test_an_owned_name_symlinked_to_a_mint_shaped_file_is_never_unlinked(
    _isolated_mint: Path,
):
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

    # The check/act attack: every name-based conjunct would pass on the RESOLVED
    # target (in-dir, mint-shaped, not owned) while unlink acts on the owned
    # lexical path. Judging the lexical path -- and refusing symlinks outright --
    # is what keeps the owned spec.
    target = _mint_shaped(_isolated_mint)
    target.write_text("{}", encoding="utf-8")
    owned = _isolated_mint / OWNED_KIRO_AGENT_FILES[0]
    owned.unlink(missing_ok=True)  # the fixture wrote a real one; swap in the attack
    owned.symlink_to(target)
    _plant_row(owned)

    mint._sweep_mint_specs()

    assert owned.is_symlink()
    assert target.is_file()


@requires_symlinks
def test_a_symlinked_mint_shaped_name_is_also_refused(_isolated_mint: Path):
    # Even when the link's OWN name is mint-shaped: a name and its target disagree
    # by construction, so there is no safe way to check one and unlink the other.
    target = _isolated_mint / "real-agent.json"
    target.write_text("VICTIM", encoding="utf-8")
    link = _mint_shaped(_isolated_mint, alias="vercel")
    link.symlink_to(target)
    _plant_row(link)

    mint._sweep_mint_specs()

    assert target.read_text(encoding="utf-8") == "VICTIM"
    assert link.is_symlink()


def test_a_spec_name_is_unique_per_flow(tmp_path: Path):
    first = mint._mint_spec_name("notion")
    second = mint._mint_spec_name("notion")

    assert first != second
    assert first.startswith(f"kirocrew-mint-notion-{os.getpid()}-")


def test_removal_deletes_only_the_exact_path_it_is_given(tmp_path: Path):
    mine = tmp_path / f"kirocrew-mint-notion-{os.getpid()}-abcdef12.json"
    sibling = tmp_path / f"kirocrew-mint-notion-{os.getpid() + 1}-beefcafe.json"
    for path in (mine, sibling):
        path.write_text("{}", encoding="utf-8")

    mint._remove_mint_agent_spec(str(mine))

    assert not mine.exists()
    assert sibling.is_file()


def test_removal_is_a_no_op_without_a_path(tmp_path: Path):
    handmade = tmp_path / "kirocrew-mint-notion.json"
    handmade.write_text("{}", encoding="utf-8")

    # The main-agent fallback records no path, so there is nothing to delete.
    mint._remove_mint_agent_spec("")

    assert handmade.is_file()


def test_a_provider_absent_from_the_main_spec_falls_back_to_the_managed_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    assert mint._write_mint_agent_spec("notion") == ("kirocrew", "")


def test_writing_a_spec_lands_a_uniquely_named_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )

    name, path = mint._write_mint_agent_spec("notion")

    assert name.startswith(f"kirocrew-mint-notion-{os.getpid()}-")
    assert Path(path) == tmp_path / f"{name}.json"
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    assert list(body["mcpServers"]) == ["notion"]
    # Single-server spec: no rename can be needed, so the key is absent.
    assert "toolAliases" not in body


def test_writing_refuses_to_overwrite_an_existing_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / "kirocrew.json").write_text(
        json.dumps({"mcpServers": {"notion": {"url": _URL}}}), encoding="utf-8"
    )
    monkeypatch.setattr(mint, "_mint_spec_name", lambda alias: "kirocrew-mint-fixed-1-aaaaaaaa")
    (tmp_path / "kirocrew-mint-fixed-1-aaaaaaaa.json").write_text("PRECIOUS", encoding="utf-8")

    with pytest.raises(FileExistsError):
        mint._write_mint_agent_spec("notion")

    assert (tmp_path / "kirocrew-mint-fixed-1-aaaaaaaa.json").read_text(
        encoding="utf-8"
    ) == "PRECIOUS"

"""Warm-mint tests: the spec plan and the files it writes, judges and sweeps.

Tool-alias keys and collision renames, the sweep that never unlinks a live cold mint's spec or
a file this module did not write, the private generation scope the specs live in, ownership
judged by content rather than by name or generic defaults, the hardened reads of every spec
path, and a refused or missing planned spec aborting warming instead of being activated by name.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import connections_warm_helpers
import pytest
from connections_warm_helpers import _mimic_spec, _ours_shaped_spec_text, _provider

from conftest import make_dir_link, requires_symlinks
from kiro_crew import security
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.connections import tool_aliases, warm

# The shared fixtures, bound here by name so pytest registers them for this module.
_agents_dir = connections_warm_helpers._agents_dir
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime
_private_warm_home = connections_warm_helpers._private_warm_home


# ── defect: tool-alias key shape ──
#
# The resolver de-collides by registry SLUG and keys ``@slug/tool``, but a warm spec mounts
# servers under ``mcp_server_alias(slug)``. Where the two differ a slug-keyed entry names a
# server the spec never mounted, so kiro-cli applies no rename and the collision returns.


@pytest.fixture
def _slash_bearing_registry(monkeypatch: pytest.MonkeyPatch):
    """Two providers whose slugs contain a slash, so slug and mounted alias differ."""
    declared = {
        "ns/alpha": {"shared_tool": "alpha_shared_tool"},
        "ns/beta": {"shared_tool": "beta_shared_tool"},
    }
    monkeypatch.setattr(tool_aliases, "declared_tool_aliases", lambda: declared)
    monkeypatch.setattr(warm, "declared_tool_aliases", lambda: declared)
    return declared


def test_alias_keys_name_the_server_the_spec_actually_mounts(_slash_bearing_registry):
    """RED before the re-key: the emitted keys were ``@ns/alpha/...``, a server the
    spec -- which mounts ``ns-alpha`` -- never declared, so no rename applied."""
    aliases = warm.connections_tool_aliases(["ns-alpha", "ns-beta"])
    assert aliases == {
        "@ns-alpha/shared_tool": "alpha_shared_tool",
        "@ns-beta/shared_tool": "beta_shared_tool",
    }
    mounted = {"ns-alpha", "ns-beta"}
    assert {key.lstrip("@").rpartition("/")[0] for key in aliases} == mounted


def test_the_spec_a_warm_plan_writes_only_mounts_aliased_servers(_slash_bearing_registry):
    body = warm._warm_spec_body(
        "probe", {"ns-alpha": {"url": "https://a"}, "ns-beta": {"url": "https://b"}}, "probe"
    )
    assert set(body["toolAliases"]) <= {f"@{alias}/shared_tool" for alias in body["mcpServers"]}


# ── defect: alias semantics rename EVERY claimant, not just later ones ──
#
# Alias handling renames EVERY claimant, slug-keyed: when two mounted servers claim a
# tool, both are renamed and neither keeps the bare name — the FIRST does not keep it.


def test_every_claimant_of_a_collision_is_renamed_not_just_the_later_one():
    aliases = warm.connections_tool_aliases(["linear", "vercel"])
    assert aliases == {
        "@linear/get_project": "linear_get_project",
        "@linear/list_projects": "linear_list_projects",
        "@linear/list_teams": "linear_list_teams",
        "@vercel/get_project": "vercel_get_project",
        "@vercel/list_projects": "vercel_list_projects",
        "@vercel/list_teams": "vercel_list_teams",
    }
    # Tools only one of the mounted pair declares keep their natural names.
    assert not any(key.endswith(("list_issues", "get_issue")) for key in aliases)


def test_a_single_mounted_provider_needs_no_aliases():
    assert warm.connections_tool_aliases(["linear"]) == {}
    assert warm.connections_tool_aliases(["vercel"]) == {}


def test_a_warm_spec_declares_tool_aliases_only_when_a_collision_is_mounted():
    single = warm._warm_spec_body("m", {"vercel": {"url": "https://v"}}, "probe")
    assert "toolAliases" not in single
    both = warm._warm_spec_body(
        "m", {"linear": {"url": "https://l"}, "vercel": {"url": "https://v"}}, "probe"
    )
    assert both["toolAliases"]["@linear/list_teams"] == "linear_list_teams"
    assert both["toolAliases"]["@vercel/list_teams"] == "vercel_list_teams"


# ── spec sweep: never unlink a live COLD mint's spec ──


def test_the_warm_sweep_refuses_a_cold_mint_spec_that_shares_the_prefix():
    """A cold spec for a server named ``warm-*`` matches the warm prefix. Deleting it
    would strand a user mid-consent, so the cold ``-<pid>-<8hex>`` shape is refused --
    including a MIXED-CASE alias, which only a shared character class catches."""
    for cold in ("kirocrew-mint-warm-foo-4821-9ab3c1de", "kirocrew-mint-warm-Foo-4821-9ab3c1de"):
        assert warm._is_stale_warm_spec(cold, frozenset()) is False


def test_the_warm_sweep_drops_a_warm_spec_absent_from_the_plan_and_keeps_the_rest():
    assert warm._is_stale_warm_spec("kirocrew-mint-warm-notion", frozenset()) is True
    assert (
        warm._is_stale_warm_spec(
            "kirocrew-mint-warm-notion", frozenset({"kirocrew-mint-warm-notion"})
        )
        is False
    )
    assert warm._is_stale_warm_spec("some-user-agent", frozenset()) is False


# ── defect: the sweep trusted a NAME, so it deleted and overwrote files it never wrote ──
#
# Warm spec names are FIXED and predictable (``kirocrew-mint-warm-<alias>``), and they live in
# the user's own agents directory alongside the agents they hand-write. Name shape alone made
# every such path fair game: a user's agent spec sitting at one was unlinked by the sweep and
# clobbered by the write. Ownership is now proved from the file's CONTENTS, and a path that
# cannot be proved ours is left exactly as it is -- audited and skipped, never raised.


def test_generation_specs_live_only_in_the_private_project_scope(
    _agents_dir: Path, _private_warm_home: Path
):
    """The warm helper must never publish its internal modes into the user's agent list."""
    work_dir = warm._create_warm_generation_dir()
    private_agents = warm._warm_generation_agents_dir(work_dir)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), private_agents)

    assert not list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))
    assert (private_agents / f"{warm._WARM_BASE_AGENT}.json").is_file()
    assert work_dir.parent == warm._warm_generations_dir()


def test_private_generation_specs_are_not_offered_by_agent_discovery(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The picker refuses the sensitive run tree even when it is passed as a project."""
    from kiro_crew import agent_discovery

    work_dir = warm._create_warm_generation_dir()
    private_agents = warm._warm_generation_agents_dir(work_dir)
    warm._write_warm_mint_specs(warm._warm_spec_plan([]), private_agents)
    monkeypatch.setattr(
        agent_discovery,
        "is_sensitive_path",
        lambda value: str(value).startswith(str(_private_warm_home / "run")),
    )
    agent_discovery.clear_list_agents_cache()

    names = {
        item.name
        for item in agent_discovery.list_agents(
            agents_dir=_agents_dir,
            project_dir=work_dir,
        )
    }

    assert not names & {warm._WARM_BASE_AGENT, warm._WARM_ALL_AGENT}


@pytest.mark.asyncio
async def test_spawn_and_session_mode_both_resolve_from_one_private_generation(
    _agents_dir: Path,
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The process starts on the private base mode and session/new switches to its all mode."""
    built: list[dict[str, Any]] = []
    activated: list[str] = []

    class _Handle:
        def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
            return []

        async def drain_init(self, **kwargs: Any) -> None:
            return None

        async def destroy(self) -> None:
            return None

    class _Spawnable:
        pid = 4242

        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

        async def spawn(self, start_priority=None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs: Any) -> _Handle:
            activated.append(str(kwargs.get("agent")))
            return _Handle()

    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)
    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)

    plan = await warm._warm_mint._ensure_locked([_provider("linear")])
    assert plan is not None
    activation, _requests = await warm._warm_mint._activate_locked(
        plan.all_agent,
        frozenset(),
    )

    work_dir = Path(built[0]["work_dir"])
    assert built[0]["agent"] == warm._WARM_BASE_AGENT
    assert (work_dir / ".kiro" / "agents" / f"{warm._WARM_BASE_AGENT}.json").is_file()
    assert (work_dir / ".kiro" / "agents" / f"{warm._WARM_ALL_AGENT}.json").is_file()
    assert activated == [warm._WARM_ALL_AGENT]
    assert activation > 0


@pytest.mark.asyncio
async def test_generation_directory_is_removed_only_after_a_confirmed_kill(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class _Process:
        pid = 5151

        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.kill_attempts = 0

        async def kill(self, *, expected: bool = False, reason: str = "") -> None:
            self.kill_attempts += 1
            if self.failures:
                self.failures -= 1
                raise TimeoutError("still alive")

    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm, "_WARM_KILL_TIMEOUT_SECONDS", 1)
    work_dir = warm._create_warm_generation_dir()
    runtime = _Process(failures=1)

    def _liveness(pid: int) -> str:
        """Report the runtime dead only once its kill has actually taken.

        Stated rather than left to the host: releasing the tree requires positive proof the
        runtime is gone, and an arbitrary fake PID can collide with a real process on the
        machine running the suite -- which would otherwise make this test's outcome depend
        on who happens to own PID 5151.
        """
        if pid != runtime.pid:
            return warm.platform_compat.PID_ALIVE
        return (
            warm.platform_compat.PID_DEAD
            if runtime.kill_attempts > 1
            else warm.platform_compat.PID_ALIVE
        )

    monkeypatch.setattr(warm.platform_compat, "pid_liveness", _liveness)
    warm._bind_warm_generation(runtime, work_dir)

    assert await warm._warm_mint._kill_generation(3, runtime) is False
    assert work_dir.is_dir(), "a process that may still live still owns its specs"

    assert await warm._warm_mint._kill_generation(3, runtime) is True
    assert not work_dir.exists(), "a confirmed kill releases the generation tree"


def test_generation_creation_refuses_a_dir_whose_acl_tighten_failed(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A silently loose generation dir must be refused, not handed agent specs.

    ``make_owner_only_dir`` documents its tighten step as best-effort, so on a filesystem
    where the ACL write fails the directory exists but is not provably owner-only --
    contradicting the private-scope guarantee this module exists to provide. Creation must
    fail loud (and clean up) rather than return a loose directory that will be handed
    injectable agent specs.
    """

    def _failing_restrict(path):  # noqa: ANN001 — mirrors platform_compat's signature
        raise PermissionError(f"ACL write refused for {path}")

    monkeypatch.setattr(warm.platform_compat, "restrict_dir_to_owner", _failing_restrict)

    with pytest.raises(PermissionError):
        warm._create_warm_generation_dir()

    generations = warm._warm_generations_dir()
    leftovers = [p for p in generations.iterdir()] if generations.is_dir() else []
    assert leftovers == [], "a refused generation dir must not survive on disk"


def test_a_provisional_marker_does_not_prove_the_runtime_dead(
    _private_warm_home: Path,
):
    """A marker still carrying pid 0 keeps the tree: the bind rewrite may have failed.

    ``_create_warm_generation_dir`` writes a provisional owner marker with no runtime
    identity, and ``_bind_warm_generation`` rewrites it after spawn. If that rewrite fails,
    the marker says ``runtime_pid: 0`` while the spawned process lives -- so reading pid 0
    as proof of death would delete a surviving runtime's cwd and private specs. Unproved
    must keep, exactly as the scavenger's tri-state treats the same marker.
    """

    class _Process:
        pid = 7373

    work_dir = warm._create_warm_generation_dir()
    runtime = _Process()
    # Attach the directory WITHOUT _bind_warm_generation: this is the failed-rewrite
    # state, where the provisional pid-0 marker from _create_warm_generation_dir is
    # all that exists on disk.
    setattr(runtime, warm._WARM_RUNTIME_DIR_ATTR, str(work_dir))

    assert warm._release_runtime_generation(runtime) is False
    assert work_dir.is_dir(), "a provisional marker (pid 0) must keep the tree"


def test_a_never_bound_provisional_generation_is_released(_private_warm_home: Path):
    """A spawn that never produced a process leaves nothing that could read its tree.

    ``_bind_warm_generation`` publishes the runtime identity only after ``spawn()`` returns,
    so a spawn refused outright leaves the provisional ``runtime_pid: 0`` marker on a runtime
    that never reached a PID. Both the abandon path's ``_recorded_runtime_is_dead`` and the
    scavenger's ``_process_identity_live`` read that marker as UNPROVED and keep the tree, so
    nothing ever released it and every failed spawn added another directory.

    The sibling test above pins the case this must NOT widen into: the same provisional marker
    on a runtime that DOES carry a PID still keeps its tree, because that child may be alive.
    """

    class _NeverSpawned:
        pid = 0

    work_dir = warm._create_warm_generation_dir()
    runtime = _NeverSpawned()
    setattr(runtime, warm._WARM_RUNTIME_DIR_ATTR, str(work_dir))

    assert warm._release_runtime_generation(runtime) is True
    assert not work_dir.exists(), "a spawn that never ran must not leave its tree behind"
    assert not hasattr(runtime, warm._WARM_RUNTIME_DIR_ATTR)


def test_a_bound_generation_is_untouched_until_its_identity_proves_dead(
    _private_warm_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A BOUND marker keeps the identity proof: pid 0 is the only accepted stand-in."""

    class _Process:
        pid = 4242

    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm, "_process_identity_live", lambda pid, started: True)
    work_dir = warm._create_warm_generation_dir()
    runtime = _Process()
    warm._bind_warm_generation(runtime, work_dir)

    assert warm._spawn_left_no_process(runtime, work_dir) is False
    assert warm._release_runtime_generation(runtime) is False
    assert work_dir.is_dir(), "a live bound runtime's tree must survive"


def test_startup_scavenging_deletes_only_proven_dead_owned_generations(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    dead = warm._create_warm_generation_dir()
    live = warm._create_warm_generation_dir()
    unproved = warm._warm_generations_dir() / "generation-unproved"
    unproved.mkdir()

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: f"start-{pid}")
    warm._bind_warm_generation(_Process(101), dead)
    warm._bind_warm_generation(_Process(202), live)
    monkeypatch.setattr(
        warm,
        "_process_identity_live",
        lambda pid, started: {101: False, 202: True}.get(pid, False),
    )

    assert warm._scavenge_warm_generation_dirs() == 1
    assert not dead.exists()
    assert live.is_dir()
    assert unproved.is_dir(), "absence of an ownership marker must fail closed"


def test_recorded_gateway_identity_reads_back_as_live(_private_warm_home: Path):
    """The gateway token the writer stores must round-trip through the reader.

    Writer and reader have to agree on ONE token format, and it must be one
    whose readers never act on a drifted render: both sides go through
    ``_marker_process_start_id`` (``get_process_start_id``-first), so the
    locale-rendered ``ps`` spelling is neither written nor compared. Storing a
    token from one helper and comparing against another -- e.g. the
    reboot-unique ``own_process_start_time()`` against the reader -- classifies
    this very much alive gateway as dead, and only coincidentally agreed on
    Windows, where both helpers return the same creation ``FILETIME``.
    """
    owner = warm._warm_generation_owner()

    assert owner["gateway_pid"] == os.getpid()
    assert owner["gateway_started"], "a readable host must record a gateway token"
    assert warm._process_identity_live(owner["gateway_pid"], owner["gateway_started"]) is True


def test_writer_never_emits_a_locale_rendered_identity(_private_warm_home: Path):
    """A persisted marker must never carry the retired ``ps`` spelling.

    The writer and the reader both go through ``_marker_process_start_id``, so
    whatever lands in ``gateway_started``/``runtime_started`` is in the current
    representation by construction. This pins that property directly: a future
    change that routes the writer back through ``process_start_time``'s
    locale-rendered ``ps`` leg would still round-trip on a same-locale host and
    only delete trees after a ``TZ``/``LC_TIME`` change, which no round-trip
    test can observe.
    """

    class _Process:
        pid = os.getpid()

    owner = warm._warm_generation_owner(_Process())
    for key in ("gateway_started", "runtime_started"):
        recorded = owner[key]
        assert recorded, "a readable host must record both identities"
        assert warm._CURRENT_START_ID_RE.match(
            recorded
        ), f"{key} is not in the current representation: {recorded!r}"


def test_start_ids_comparable_allows_only_the_current_representation():
    """The migration guard compares kinds, not values.

    Two current-representation values are always comparable -- even when they
    differ, in which case the mismatch verdict stands. Anything else on either
    side is "unknown", never "different".
    """
    assert warm._start_ids_comparable("12345", "12345") is True
    assert warm._start_ids_comparable("12345", "67890") is True
    assert warm._start_ids_comparable("1730.000042", "1730.000043") is True
    assert warm._start_ids_comparable("Wed Sep  3 10:00:00 2026", "12345") is False
    assert warm._start_ids_comparable("12345", "Wed Sep  3 10:00:00 2026") is False
    assert warm._start_ids_comparable("", "12345") is False
    assert warm._start_ids_comparable("12345", "") is False


def test_scavenging_keeps_a_generation_with_a_legacy_ps_marker(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A pre-migration marker must read as unknown, not dead.

    RED before the fix: the marker below carries the retired ``ps -o lstart=``
    spelling for a gateway that is still running. The old reader compared that
    render against the freshly read one, called the mismatch death, and the
    scavenger removed the live gateway's cwd and private agent scope. The
    runtime half is pinned ALIVE so the comparability guard is the only thing
    standing between this tree and the ``rmtree`` -- the PID-liveness shortcut
    must not be what saves it.
    """
    legacy_render = "Wed Sep  3 10:00:00 2026"
    runtime_pid = 747474
    current_rep = "99999"
    monkeypatch.setattr(warm.platform_compat, "process_start_time", lambda pid: current_rep)
    monkeypatch.setattr(warm.platform_compat, "get_process_start_id", lambda pid: current_rep)
    monkeypatch.setattr(
        warm.platform_compat, "pid_liveness", lambda pid: warm.platform_compat.PID_ALIVE
    )

    work_dir = warm._create_warm_generation_dir()
    marker = {
        "sentinel": warm._WARM_GENERATION_SENTINEL,
        "version": warm._WARM_GENERATION_MARKER_VERSION,
        "gateway_pid": os.getpid(),
        "gateway_started": legacy_render,
        "runtime_pid": runtime_pid,
        "runtime_started": legacy_render,
    }
    warm._warm_generation_marker_path(work_dir).write_text(json.dumps(marker), encoding="utf-8")

    assert warm._process_identity_live(os.getpid(), legacy_render) is None
    assert warm._process_identity_live(runtime_pid, legacy_render) is None
    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir(), "a legacy marker must keep the tree it cannot judge"


def test_scavenging_keeps_a_generation_whose_gateway_is_still_alive(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A live gateway must veto scavenging even once its runtime has exited.

    This is the consequence of the format mismatch rather than a restatement of
    it: removal needs BOTH identities proven dead, so a gateway that cannot
    prove its own liveness silently forfeits its half of that guard, and the
    directory it is still using is deleted underneath it as soon as its
    short-lived runtime goes away. Neither identity is stubbed here -- the
    existing scavenging test replaces ``_process_identity_live`` outright, which
    is why it cannot observe a wrong token reaching that function.
    """
    real_start_time = warm.platform_compat.process_start_time
    real_start_id = warm.platform_compat.get_process_start_id
    dead_pid = 424242

    monkeypatch.setattr(
        warm.platform_compat,
        "process_start_time",
        lambda pid: "runtime-token" if pid == dead_pid else real_start_time(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat,
        "get_process_start_id",
        lambda pid: "runtime-token" if pid == dead_pid else real_start_id(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat,
        "pid_liveness",
        lambda pid: (
            warm.platform_compat.PID_DEAD if pid == dead_pid else warm.platform_compat.PID_ALIVE
        ),
    )

    class _Process:
        pid = dead_pid

    work_dir = warm._create_warm_generation_dir()
    warm._bind_warm_generation(_Process(), work_dir)

    assert warm._scavenge_warm_generation_dirs() == 0
    assert work_dir.is_dir(), "the generation its live gateway still owns must survive"


def test_release_keeps_the_tree_when_the_killed_runtime_survived(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A kill that REPORTED success but left the PID alive must not delete the specs.

    ``AcpRuntime._kill_inner`` handles its own survivor by logging and returning: both
    ``kill_process_tree`` calls swallow ``OSError`` by design, so a signal-delivery failure
    (EPERM through a launcher wrapper, pgid drift) is indistinguishable from success at that
    layer, and it deliberately leaves the PID tracked for a sweep instead of raising.
    ``_kill_quietly`` therefore answers True while the child is still running, and releasing
    on that word alone rmtree's the cwd and the private agent scope out from under a process
    still reading them. The recorded runtime identity is the proof that exists; consult it.
    """
    survivor_pid = 525252
    real_start_time = warm.platform_compat.process_start_time
    real_start_id = warm.platform_compat.get_process_start_id

    monkeypatch.setattr(
        warm.platform_compat,
        "process_start_time",
        lambda pid: "runtime-token" if pid == survivor_pid else real_start_time(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat,
        "get_process_start_id",
        lambda pid: "runtime-token" if pid == survivor_pid else real_start_id(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat, "pid_liveness", lambda pid: warm.platform_compat.PID_ALIVE
    )

    class _Process:
        pid = survivor_pid

    runtime = _Process()
    work_dir = warm._create_warm_generation_dir()
    warm._bind_warm_generation(runtime, work_dir)

    assert warm._release_runtime_generation(runtime) is False
    assert work_dir.is_dir(), "a runtime that outlived its kill still owns its specs"


def test_release_removes_the_tree_once_the_runtime_is_provably_dead(
    _private_warm_home: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The ordinary confirmed kill must still reclaim the tree.

    Paired with the survivor case on purpose: a gate that refused whenever liveness was
    merely unproven would trade a deletion bug for a directory that accumulates on every
    activation, so this pins that a dead runtime is still released promptly rather than
    waiting for the next gateway start to scavenge it.
    """
    gone_pid = 636363
    real_start_time = warm.platform_compat.process_start_time
    real_start_id = warm.platform_compat.get_process_start_id

    monkeypatch.setattr(
        warm.platform_compat,
        "process_start_time",
        lambda pid: "runtime-token" if pid == gone_pid else real_start_time(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat,
        "get_process_start_id",
        lambda pid: "runtime-token" if pid == gone_pid else real_start_id(pid),
    )
    monkeypatch.setattr(
        warm.platform_compat,
        "pid_liveness",
        lambda pid: (
            warm.platform_compat.PID_DEAD if pid == gone_pid else warm.platform_compat.PID_ALIVE
        ),
    )

    class _Process:
        pid = gone_pid

    runtime = _Process()
    work_dir = warm._create_warm_generation_dir()
    warm._bind_warm_generation(runtime, work_dir)

    assert warm._release_runtime_generation(runtime) is True
    assert not work_dir.exists()


def test_startup_removes_owned_global_legacy_specs_but_keeps_unsentinelled_files(
    _agents_dir: Path,
    _private_warm_home: Path,
):
    ours = _agents_dir / f"{warm._WARM_BASE_AGENT}.json"
    ours.write_text(
        json.dumps(warm._warm_spec_body(warm._WARM_BASE_AGENT, {}, "legacy")),
        encoding="utf-8",
    )
    foreign, body = _mimic_spec(_agents_dir, f"{warm._WARM_AGENT_PREFIX}manual")

    assert warm.scavenge_warm_mint_artifacts() == 1
    assert not ours.exists()
    assert foreign.read_text(encoding="utf-8") == body


def _foreign_spec(agents: Path, stem: str) -> tuple[Path, str]:
    """A user's OWN agent spec, planted at a path a warm plan would claim."""
    path = agents / f"{stem}.json"
    body = json.dumps(
        {
            "name": "my-own-research-agent",
            "description": "hand-written by the user",
            "prompt": "You are my research agent.",
            "mcpServers": {"private": {"command": "my-server"}},
            "allowedTools": ["@private"],
        },
        indent=2,
    )
    path.write_text(body, encoding="utf-8")
    return path, body


def test_the_sweep_refuses_to_unlink_a_foreign_file_at_a_warm_spec_path(_agents_dir: Path):
    """RED before the ownership check: ``_is_stale_warm_spec`` matched the NAME, so the
    write-time sweep unlinked a user's own agent spec that happened to sit there."""
    planted, body = _foreign_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    assert planted.is_file(), "the sweep deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


# ── defect: the ownership marks alone are GENERIC DEFAULTS, so a mimic passed as ours ──
#
# `model: "auto"`, `includeMcpJson: false`, `prompt: ""` and `allowedTools: []` are stock
# values any hand-written or scaffolded agent plausibly carries, so name-plus-marks judged a
# wholly user-authored spec at a warm path as ours and clobbered it. That falsified the claim
# that CONTENTS prove ownership. A sentinel prefix on the description is what discriminates.


def test_a_mimic_carrying_only_our_generic_defaults_survives_the_write(_agents_dir: Path):
    """RED before the sentinel: name plus four stock defaults read as ours, so the write
    clobbered a spec whose description, mcpServers and tools were entirely the user's."""
    planted, body = _mimic_spec(_agents_dir, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a mimic"


def test_a_mimic_carrying_only_our_generic_defaults_survives_sweep_and_teardown(
    _agents_dir: Path,
):
    """RED before the sentinel: the same mimic at a path no plan wants was unlinked by both
    the write-time sweep and teardown."""
    planted, body = _mimic_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)
    assert planted.is_file(), "the sweep deleted a mimic"

    warm._remove_warm_mint_specs(_agents_dir)

    assert planted.is_file(), "teardown deleted a mimic"
    assert planted.read_text(encoding="utf-8") == body


def test_the_judge_reads_a_mimic_as_foreign_and_our_own_writer_output_as_ours(
    _agents_dir: Path,
):
    """The predicate itself, so the discriminator is pinned independently of the callers."""
    mimic, _ = _mimic_spec(_agents_dir, "kirocrew-mint-warm-mimic")
    assert warm._warm_spec_is_foreign(mimic) is True

    ours = _agents_dir / "kirocrew-mint-warm-ours.json"
    ours.write_text(
        json.dumps(warm._warm_spec_body("kirocrew-mint-warm-ours", {}, "probe")),
        encoding="utf-8",
    )
    assert warm._warm_spec_is_foreign(ours) is False


@requires_symlinks
def test_a_dangling_symlink_at_the_spec_path_reads_as_foreign(_agents_dir: Path):
    """A dangling link resolves to nothing, so ``exists()`` alone reports the path free and
    the writer would replace the link (the sweep would unlink it) -- destroying a path
    occupant this module does not own. The occupied verdict keeps every write and unlink
    away from it."""
    planted = _agents_dir / "kirocrew-mint-warm-notion.json"
    planted.symlink_to(_agents_dir / "nowhere" / "target.json")

    assert warm._warm_spec_is_foreign(planted) is True


def test_a_junction_at_the_spec_path_reads_as_foreign(_agents_dir: Path):
    """A directory JUNCTION is the only directory link an unprivileged Windows writer can
    plant, and it answers ``is_symlink()`` False (and ``exists()`` False when its target is
    absent), so an ``is_symlink() or exists()`` guard read a junction at a warm-spec name as
    a free path -- the writer replaced it and the sweep unlinked it, destroying an occupant
    this module does not own. make_dir_link plants a junction on Windows and a directory
    symlink on POSIX, so the occupied verdict is pinned on every shard."""
    outside = _agents_dir / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not ours", encoding="utf-8")
    planted = _agents_dir / "kirocrew-mint-warm-notion.json"
    make_dir_link(planted, outside)

    assert warm._warm_spec_is_foreign(planted) is True
    # The refusal never followed the link: its target is untouched.
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "not ours"


def test_every_spec_a_warm_plan_writes_carries_the_ownership_sentinel(_agents_dir: Path):
    """Whole-plan coverage: the base spec and the all-providers spec alike."""
    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    written = list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))
    assert written
    for path in written:
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["description"].startswith(warm._WARM_SPEC_SENTINEL)
        assert warm._warm_spec_is_foreign(path) is False


def test_the_write_refuses_to_clobber_a_foreign_file_at_a_planned_spec_path(_agents_dir: Path):
    """RED before the ownership check: the write was unconditional, so a user's file at a
    path the CURRENT plan wants was overwritten rather than skipped."""
    planted, body = _foreign_spec(_agents_dir, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a foreign file"


def test_the_teardown_refuses_to_unlink_a_foreign_file_at_a_warm_spec_path(_agents_dir: Path):
    """RED before the ownership check: teardown swept the whole warm glob by name."""
    planted, body = _foreign_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._remove_warm_mint_specs(_agents_dir)

    assert planted.is_file(), "teardown deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


def test_the_sweep_still_unlinks_a_stale_spec_a_warm_plan_did_write(_agents_dir: Path):
    """The refusal must not cost the sweep its job: our own leftovers still go."""
    stale = _agents_dir / "kirocrew-mint-warm-gone.json"
    stale.write_text(
        json.dumps(warm._warm_spec_body("kirocrew-mint-warm-gone", {}, "stale")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    assert not stale.exists(), "a spec this module wrote survived the sweep"
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()


def test_teardown_unlinks_every_spec_a_warm_plan_did_write(_agents_dir: Path):
    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()

    warm._remove_warm_mint_specs(_agents_dir)

    assert not list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))


def test_a_spec_this_module_wrote_is_rewritten_in_place(_agents_dir: Path):
    """Ownership must be recognized across a plan change, or the process gets a stale spec."""
    path = _agents_dir / f"{warm._WARM_BASE_AGENT}.json"
    path.write_text(
        json.dumps(warm._warm_spec_body(warm._WARM_BASE_AGENT, {}, "an older description")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)

    description = json.loads(path.read_text(encoding="utf-8"))["description"]
    assert description.startswith(warm._WARM_SPEC_SENTINEL)
    assert "Zero-server" in description and "an older description" not in description


def test_an_unreadable_file_at_a_warm_spec_path_is_left_alone(_agents_dir: Path):
    """Fail closed: a path we cannot prove is ours is not ours. The cost is clutter."""
    path = _agents_dir / "kirocrew-mint-warm-notion.json"
    path.write_text("{ this is not json", encoding="utf-8")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), _agents_dir)
    warm._remove_warm_mint_specs(_agents_dir)

    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_a_refusal_is_audited_rather_than_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The caller sees no exception, and the refusal is not silent either."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(warm._agent, "KIRO_AGENTS_DIR", agents)
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        warm,
        "_log_warm_event",
        lambda operation, resources, outcome="ok": events.append((operation, resources, outcome)),
    )
    _foreign_spec(agents, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]), agents)

    assert events, "a refusal to touch a user's file was silent"
    assert all(outcome == "refused" for _, _, outcome in events)
    # The audit names the spec, never the file's contents.
    assert all(warm._WARM_AGENT_PREFIX in resources for _, resources, _ in events)


# ── the plan's own read of the user's spec goes through the hardened reader ──
#
# ``_warm_spec_plan`` read ``kirocrew.json`` with a raw ``_load_json``, which follows a
# symlink and parses whatever it lands on -- so a link planted at the agent-spec path
# pointed the planner at a file outside the agents dir, and that file's contents then
# DECIDED the plan: a configured entry whose auth shape differs from the registry's vetoes
# the provider (``_warm_mintable_entry``). No size cap, no sensitive-target refusal, no SEL
# denial. The other ``kirocrew.json`` readers were migrated; this one was added after them.


@requires_symlinks
def test_a_sensitive_symlink_at_the_spec_path_never_reaches_the_plan(
    _agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """RED before the migration: the linked file's veto excluded the provider."""
    from kiro_crew import agent_discovery

    target = tmp_path / "protected.json"
    # A DIVERGENT auth shape, so being read is observable: it would veto the provider.
    target.write_text(
        json.dumps({"mcpServers": {"acme": {"url": "https://attacker.example/mcp"}}}),
        encoding="utf-8",
    )
    (_agents_dir / AGENT_FILENAME).symlink_to(target)
    monkeypatch.setattr(
        agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
    )

    plan = warm._warm_spec_plan([_provider("acme")])

    assert plan.entries["acme"]["url"] == "https://acme.example/mcp"
    assert plan.all_agent == warm._WARM_ALL_AGENT
    assert "attacker.example" not in json.dumps(plan.specs)


def test_a_refused_spec_plans_as_though_nothing_were_configured(
    _agents_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Refusal degrades exactly like an absent file -- it never fails the planner."""
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
    (_agents_dir / AGENT_FILENAME).write_text(
        json.dumps(
            {"mcpServers": {"acme": {"url": "https://attacker.example/mcp"}}, "pad": "x" * 1024}
        ),
        encoding="utf-8",
    )

    plan = warm._warm_spec_plan([_provider("acme")])

    assert plan.entries["acme"]["url"] == "https://acme.example/mcp"


# ── the OWNERSHIP judge reads a warm spec path, and it decides an unlink ──
#
# ``_warm_spec_is_foreign`` was the second raw ``_load_json`` read, and the more dangerous
# one: its verdict is what licenses the sweep to unlink that path and the writer to overwrite
# it. A symlink planted at a warm spec path was FOLLOWED, so a file outside the agents dir --
# a sensitive one included -- was read uncapped and unaudited, and if its contents happened to
# be ours-shaped it was judged OURS and destroyed through the link. Both cases below are
# differential: under the raw read the planted body parses and reads as ours (False).


@requires_symlinks
def test_a_sensitive_symlink_at_a_warm_spec_path_is_never_judged_ours(
    _agents_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """RED before the migration: the link was followed, read as ours, and swept."""
    from kiro_crew import agent_discovery

    target = tmp_path / "protected.json"
    stem = f"{warm._WARM_AGENT_PREFIX}notion"
    target.write_text(_ours_shaped_spec_text(stem), encoding="utf-8")
    link = _agents_dir / f"{stem}.json"
    link.symlink_to(target)
    monkeypatch.setattr(
        agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
    )

    assert warm._warm_spec_is_foreign(link) is True

    # The verdict's whole purpose: the sweep must leave both the link and its target alone.
    warm._remove_warm_mint_specs(_agents_dir)

    assert link.is_symlink(), "the sweep unlinked a symlink it was refused"
    assert target.is_file(), "the sweep reached a file outside the agents dir"


def test_an_oversized_spec_at_a_warm_spec_path_is_never_judged_ours(
    _agents_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """The cap is consulted here too: past it, ours-shaped contents still read as foreign."""
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
    stem = f"{warm._WARM_AGENT_PREFIX}notion"
    planted = _agents_dir / f"{stem}.json"
    planted.write_text(_ours_shaped_spec_text(stem, pad=1024), encoding="utf-8")

    assert warm._warm_spec_is_foreign(planted) is True

    warm._remove_warm_mint_specs(_agents_dir)

    assert planted.is_file(), "the sweep unlinked a file it could not read"


def test_a_readable_spec_of_ours_is_still_judged_ours_under_the_same_cap(_agents_dir: Path):
    """The refusal must not cost the judge its job -- guards the vacuous pass above."""
    stem = f"{warm._WARM_AGENT_PREFIX}notion"
    planted = _agents_dir / f"{stem}.json"
    planted.write_text(_ours_shaped_spec_text(stem), encoding="utf-8")

    assert warm._warm_spec_is_foreign(planted) is False


# ── the work dir: an agent-writable cwd is a second spec path, unguarded ──
#
# Every ownership check in this module guards the USER's agents dir. kiro-cli also resolves
# PROJECT-LOCAL specs from ``<cwd>/.kiro/agents``, and the spawn activates by NAME, so a spec
# planted under the process's own cwd shadows the guarded one and its ``mcpServers`` is what
# gets initialized and authorized against. The cwd therefore has to be a tree an agent file
# tool cannot write.


def test_the_warm_process_cwd_is_inside_the_agent_write_protected_tree():
    """RED before the fix: the cwd was ``<data home>/connections/warm-mint``.

    Asserted through ``security``'s own predicate rather than a path literal, because the
    property is "the gate refuses an agent write here" -- which a later rename must keep and a
    path literal would not notice losing.
    """
    planted = warm._warm_work_dir() / ".kiro" / "agents" / f"{warm._WARM_ALL_AGENT}.json"

    assert security.is_sensitive_write_path(str(planted)) is True


def test_the_gate_that_test_relies_on_is_not_true_of_every_crew_home_path():
    """Guards the vacuous pass above: the old cwd's tree really is agent-writable."""
    writable = warm.data_home() / "connections" / "warm-mint" / ".kiro" / "agents" / "all.json"

    assert security.is_sensitive_write_path(str(writable)) is False


# ── defect: a refused spec is still activated BY NAME ──
#
# The writer audits and SKIPS a foreign file at a planned spec path, which protects the
# file. It does not protect the ACTIVATION: the runtime is handed `agent=<fixed name>` and
# kiro-cli resolves that name off the same directory, so a hand-written agent sitting at
# the name we declined to own gets executed and its `mcpServers` commands initialize.


@pytest.mark.asyncio
async def test_a_global_same_name_agent_never_blocks_the_private_generation(
    monkeypatch: pytest.MonkeyPatch,
    _agents_dir: Path,
):
    """A user-owned global file survives while the helper activates its private twin."""
    planted, body = _foreign_spec(_agents_dir, warm._WARM_BASE_AGENT)
    constructed: list[dict[str, Any]] = []

    class _Spawnable:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        async def spawn(self, start_priority=None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)

    served = await warm._warm_mint._ensure_locked([_provider("linear")])

    assert served is not None
    assert constructed and constructed[0]["agent"] == warm._WARM_BASE_AGENT
    assert planted.read_text(encoding="utf-8") == body
    private = warm._warm_generation_agents_dir(Path(constructed[0]["work_dir"]))
    assert (private / f"{warm._WARM_BASE_AGENT}.json").is_file()


@pytest.mark.asyncio
async def test_a_missing_planned_spec_also_aborts_warming(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Absence and foreignness are the same answer here: an unreadable spec path is not a
    spec of ours, and `_warm_spec_is_foreign` answers False for an absent file, so the
    verification has to test existence as well as ownership."""
    monkeypatch.setattr(
        warm, "_write_warm_mint_specs", lambda plan, agents_dir: None
    )  # writes nothing
    constructed: list[str] = []

    def _factory():
        def _build(**kwargs):
            constructed.append(str(kwargs.get("agent")))
            raise AssertionError("a missing spec must never be activated")

        return _build

    monkeypatch.setattr(warm, "_acp_runtime_factory", _factory)

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None
    assert constructed == []


@pytest.mark.asyncio
async def test_a_spec_set_this_module_owns_is_activated_normally(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The verification must not refuse the ordinary case it exists to let through."""
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    built: list[str] = []

    class _Spawnable:
        def __init__(self, **kwargs):
            built.append(str(kwargs.get("agent")))

        async def spawn(self, start_priority=None):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)

    served = await warm._warm_mint._ensure_locked([_provider("linear")])
    assert served is not None and served.all_agent == warm._WARM_ALL_AGENT
    assert built == [warm._WARM_BASE_AGENT]
    reaper = warm._warm_mint._reaper
    assert reaper is not None
    reaper.cancel()

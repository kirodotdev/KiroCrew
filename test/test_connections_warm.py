"""Warm-mint tests: the spec plan and its files, the row-liveness registry, the chokepoint."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from test_connections_mint import _FS_ATTRS, _FS_NAMES, _called_names

from conftest import requires_symlinks
from kiro_crew import security
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.connections import tool_aliases, warm
from kiro_crew.connections.mint import _mints
from kiro_crew.connections.registry import Provider


@pytest.fixture(autouse=True)
def _clean_mint_table():
    _mints.clear()
    yield
    _mints.clear()


@pytest.fixture(autouse=True)
def _clean_warm_runtime():
    """Restore the module-level ``_warm_mint`` singleton around every test.

    The engine is a module singleton, so a test that drives ``_ensure_locked`` all the way to
    a successful spawn leaves a live runtime, a matching spec digest and a sleeping reaper on
    it -- and ``monkeypatch`` cannot undo state it never patched. A later test then reads its
    neighbour's process, which is one leak wearing three faces: the digest fast path returns
    before the test's own factory is ever constructed (no ``CancelledError`` raised, no kill
    recorded), a stand-down kills the neighbour's runtime first and inflates the kill count,
    and ``_park_or_kill_locked`` cancels a reaper task whose loop has since closed, which is
    a ``RuntimeError: Event loop is closed``. Which face appears depends only on which tests
    share a worker, so it moves whenever the file gains or loses a test.

    The containers are copied, not aliased: a test that appends to ``_retiring`` in place
    would otherwise leave the mutation behind in the very object being restored.

    ``_lock`` is reset to a FRESH lock rather than restored, because an ``asyncio.Lock``
    binds to the loop that first has to wait on it: the uncontended fast path never calls
    ``_get_loop``, so today's tests pass, but the first CONTENDED acquire in a later test's
    loop raises ``RuntimeError: ... is bound to a different event loop``. A new lock per test
    cannot carry a dead loop into the next one.
    """
    tracked = (
        "_runtime",
        "_plan",
        "_digest",
        "_generation",
        "_retiring",
        "_sessions",
        "_activation_seq",
        "_reaper",
        "_supervision_armed",
        "_rearms_remaining",
        "_rearm_task",
    )
    saved: dict[str, Any] = {name: getattr(warm._warm_mint, name) for name in tracked}
    saved["_retiring"] = list(saved["_retiring"])
    saved["_sessions"] = dict(saved["_sessions"])
    warm._warm_mint._lock = asyncio.Lock()
    yield
    for task_name in ("_reaper", "_rearm_task"):
        task = getattr(warm._warm_mint, task_name)
        if task is None or task is saved[task_name] or task.done():
            continue
        try:
            # Cancelled HERE, while the loop that owns it is still this test's. Cancelling it
            # from a later test's loop is the "Event loop is closed" face above, not the cure.
            task.cancel()
        except RuntimeError:
            # The loop is already gone; dropping the reference below is what matters.
            pass
    for name, value in saved.items():
        setattr(warm._warm_mint, name, value)
    # Fresh again on the way out: a lock left bound to THIS test's loop is exactly what the
    # next test must not inherit.
    warm._warm_mint._lock = asyncio.Lock()


class _Runtime:
    """A stand-in for one kiro-cli process, with the liveness answer we choose."""

    def __init__(self, alive: bool | BaseException) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        if isinstance(self._alive, BaseException):
            raise self._alive
        return self._alive


# ── redeemability takes TWO questions, and they die independently ──


def test_a_row_stamped_with_no_holder_at_all_is_never_alive():
    assert warm._warm_mint.generation_is_live(0) is False
    assert warm._warm_mint.activation_is_live(0) is False


def test_the_current_generation_is_live_exactly_while_its_process_is(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    assert warm._warm_mint.generation_is_live(4) is True
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    assert warm._warm_mint.generation_is_live(4) is False


def test_a_parked_generation_stays_live_while_its_own_process_can_still_redeem(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked process still holds its peers' verifiers, so answering False for it would
    withdraw a URL the user can still redeem."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(3, _Runtime(True)), (4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(3) is True
    assert warm._warm_mint.generation_is_live(4) is False
    assert warm._warm_mint.generation_is_live(5) is False


def test_a_liveness_probe_that_raises_reads_as_dead_rather_than_failing_the_scan():
    """``expire_dead_mints`` runs on every status request, so a raising probe must not
    take the request down with it."""
    assert warm._runtime_alive(_Runtime(OSError("no such process"))) is False
    assert warm._runtime_alive(None) is False


def test_a_live_process_with_a_dead_session_does_not_keep_a_row_alive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Process liveness alone passed a terminated-session row -- the observed failure that
    put the session question into the predicate at all."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 2)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    row = {"state": "waiting", "shared": True, "generation": 2, "activation": 6}
    assert warm._warm_row_alive(row) is False  # type: ignore[arg-type]
    monkeypatch.setattr(warm._warm_mint, "_sessions", {6: object()})
    assert warm._warm_row_alive(row) is True  # type: ignore[arg-type]


# ── withdrawal is keyed on the FACT that the holder is gone ──


@pytest.mark.asyncio
async def test_a_row_whose_generation_is_gone_is_withdrawn():
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 99,
        "activation": 1,
    }
    assert await warm.expire_dead_mints() == ["linear"]
    assert _mints["linear"]["state"] == "expired"
    assert _mints["linear"]["reason"] == "mint_process_gone"


@pytest.mark.asyncio
async def test_a_cold_row_is_left_to_the_cold_engine():
    """The two chokepoints partition the table, and each must leave the other's rows alone.

    A cold row owns a ``client`` and carries no warm mark at all, so ``_warm_table_row``
    excludes it and its verdict stays ``_mint_holder_alive``'s. The converse -- that a
    warm-held row's verdict is THIS chokepoint's, because the cold judge abstains on one
    -- is pinned in ``test_connections_handoff.py``.
    """
    _mints["linear"] = {"state": "waiting", "oauth_url": "https://cold", "client": object()}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_a_shared_row_not_yet_serving_a_url_is_left_alone():
    """Only a row actually SERVING a URL can be serving a dead one; a claim still minting
    is the activation's to fill or release."""
    _mints["linear"] = {"state": "minting", "shared": True, "generation": 99}
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "minting"


@pytest.mark.asyncio
async def test_a_row_whose_process_and_session_both_live_keeps_its_url(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: object()})
    _mints["linear"] = {
        "state": "waiting",
        "oauth_url": "https://l/consent",
        "shared": True,
        "generation": 5,
        "activation": 2,
    }
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


def _provider(slug: str, url: str = "") -> Provider:
    return {  # type: ignore[typeddict-item]
        "slug": slug,
        "mcp_url": url or f"https://{slug}.example/mcp",
        "l0_expectations": {"dcr": True},
    }


# ── the loop/filesystem invariant (mirrors the mint engine's own guard) ──
#
# Reuses the mint guard's primitive sets so the two cannot drift apart, plus the names that
# reach the filesystem only from THIS module: the MCP inventory read, the grant stat, and the
# credential gate -- which consults the operator's on-disk OAuth-endpoint extension for any
# host outside the builtin set, so it is an unbounded stat like the rest.
_WARM_FS_NAMES = _FS_NAMES | {"list_servers", "grant_present", "oauth_url_contains_credential"}


def test_no_coroutine_in_the_warm_module_touches_the_filesystem_directly():
    tree = ast.parse(inspect.getsource(warm))
    sync: dict[str, Any] = {}
    coros: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            sync[node.name] = node
        elif isinstance(node, ast.AsyncFunctionDef):
            coros[node.name] = node
    assert sync and coros, "module shape changed; this guard is reading the wrong tree"

    touches = {
        name: bool(_called_names(node) & (_FS_ATTRS | _WARM_FS_NAMES))
        for name, node in sync.items()
    }
    changed = True
    while changed:
        changed = False
        for name, node in sync.items():
            if touches[name]:
                continue
            if any(touches.get(callee) for callee in _called_names(node)):
                touches[name] = changed = True
    fs_helpers = {name for name, hit in touches.items() if hit}
    # The known set, so a helper silently losing its filesystem work -- and with it
    # this guard's coverage -- is visible rather than a quietly weaker test.
    # `_audited_mintable_providers` couples that filesystem scan to its SEL event.
    assert fs_helpers == {
        "_log_warm_event",
        "_disabled_provider_slugs",
        "warm_spec_providers",
        "_warm_activation_candidates",
        "_warm_candidate_scan",
        "_current_warm_redrain_entry",
        "mintable_providers",
        "_audited_mintable_providers",
        "_warm_spec_plan",
        "_warm_spec_is_foreign",
        "_unowned_plan_specs",
        "_write_warm_mint_specs",
        "_remove_warm_mint_specs",
        "_warm_work_dir",
        "_credential_bearing_slugs",
    }

    offenders = {
        f"{coro} -> {callee}"
        for coro, node in coros.items()
        for callee in _called_names(node) & (fs_helpers | _FS_ATTRS | _WARM_FS_NAMES)
    }
    assert not offenders, (
        "filesystem work on the event loop: "
        + ", ".join(sorted(offenders))
        + " -- route it through asyncio.to_thread"
    )


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


# ── defect: alias semantics are #3260's, not the pre-#3260 first-server rule ──
#
# The draft asserted that the FIRST mounted server keeps the bare name and only later ones are
# renamed. #3260 shipped rename-EVERY-claimant, slug-keyed: when two mounted servers claim a
# tool, both are renamed and neither keeps the bare name.


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


@pytest.fixture
def _agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An isolated agents directory, through the module's own override hook."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(warm._agent, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *a, **k: None)
    return agents


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

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.is_file(), "the sweep deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


# ── defect: the ownership marks alone are GENERIC DEFAULTS, so a mimic passed as ours ──
#
# `model: "auto"`, `includeMcpJson: false`, `prompt: ""` and `allowedTools: []` are stock
# values any hand-written or scaffolded agent plausibly carries, so name-plus-marks judged a
# wholly user-authored spec at a warm path as ours and clobbered it. That falsified the claim
# that CONTENTS prove ownership. A sentinel prefix on the description is what discriminates.


def _mimic_spec(agents: Path, stem: str) -> tuple[Path, str]:
    """A user's own spec at a warm path that COPIES every generic default we fix.

    Everything the user actually authored -- description, mcpServers, tools -- is theirs;
    only the four stock marks and the name coincide with ours.
    """
    path = agents / f"{stem}.json"
    body = json.dumps(
        {
            "name": stem,
            "description": "my own scratch agent that happens to sit here",
            "model": "auto",
            "includeMcpJson": False,
            "prompt": "",
            "mcpServers": {"private": {"command": "my-server"}},
            "tools": ["@private"],
            "allowedTools": [],
        },
        indent=2,
    )
    path.write_text(body, encoding="utf-8")
    return path, body


def test_a_mimic_carrying_only_our_generic_defaults_survives_the_write(_agents_dir: Path):
    """RED before the sentinel: name plus four stock defaults read as ours, so the write
    clobbered a spec whose description, mcpServers and tools were entirely the user's."""
    planted, body = _mimic_spec(_agents_dir, warm._WARM_BASE_AGENT)

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a mimic"


def test_a_mimic_carrying_only_our_generic_defaults_survives_sweep_and_teardown(
    _agents_dir: Path,
):
    """RED before the sentinel: the same mimic at a path no plan wants was unlinked by both
    the write-time sweep and teardown."""
    planted, body = _mimic_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    assert planted.is_file(), "the sweep deleted a mimic"

    warm._remove_warm_mint_specs()

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


def test_every_spec_a_warm_plan_writes_carries_the_ownership_sentinel(_agents_dir: Path):
    """Whole-plan coverage: the base spec and the all-providers spec alike."""
    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

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

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert planted.read_text(encoding="utf-8") == body, "the write clobbered a foreign file"


def test_the_teardown_refuses_to_unlink_a_foreign_file_at_a_warm_spec_path(_agents_dir: Path):
    """RED before the ownership check: teardown swept the whole warm glob by name."""
    planted, body = _foreign_spec(_agents_dir, "kirocrew-mint-warm-notion")

    warm._remove_warm_mint_specs()

    assert planted.is_file(), "teardown deleted a file no warm plan ever wrote"
    assert planted.read_text(encoding="utf-8") == body


def test_the_sweep_still_unlinks_a_stale_spec_a_warm_plan_did_write(_agents_dir: Path):
    """The refusal must not cost the sweep its job: our own leftovers still go."""
    stale = _agents_dir / "kirocrew-mint-warm-gone.json"
    stale.write_text(
        json.dumps(warm._warm_spec_body("kirocrew-mint-warm-gone", {}, "stale")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    assert not stale.exists(), "a spec this module wrote survived the sweep"
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()


def test_teardown_unlinks_every_spec_a_warm_plan_did_write(_agents_dir: Path):
    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    assert (_agents_dir / f"{warm._WARM_BASE_AGENT}.json").is_file()

    warm._remove_warm_mint_specs()

    assert not list(_agents_dir.glob(f"{warm._WARM_AGENT_PREFIX}*.json"))


def test_a_spec_this_module_wrote_is_rewritten_in_place(_agents_dir: Path):
    """Ownership must be recognized across a plan change, or the process gets a stale spec."""
    path = _agents_dir / f"{warm._WARM_BASE_AGENT}.json"
    path.write_text(
        json.dumps(warm._warm_spec_body(warm._WARM_BASE_AGENT, {}, "an older description")),
        encoding="utf-8",
    )

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

    description = json.loads(path.read_text(encoding="utf-8"))["description"]
    assert description.startswith(warm._WARM_SPEC_SENTINEL)
    assert "Zero-server" in description and "an older description" not in description


def test_an_unreadable_file_at_a_warm_spec_path_is_left_alone(_agents_dir: Path):
    """Fail closed: a path we cannot prove is ours is not ours. The cost is clutter."""
    path = _agents_dir / "kirocrew-mint-warm-notion.json"
    path.write_text("{ this is not json", encoding="utf-8")

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))
    warm._remove_warm_mint_specs()

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

    warm._write_warm_mint_specs(warm._warm_spec_plan([]))

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
# denial. #6736 migrated the other ``kirocrew.json`` readers; this one was added after.


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
    monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))

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


def _ours_shaped_spec_text(stem: str, *, pad: int = 0) -> str:
    """A body the ownership judge accepts as ours: right name, marks and sentinel.

    ``pad`` grows the file through a key the judge does NOT inspect. Padding one of the four
    marks (``prompt`` is one) would make the body read as foreign on its SHAPE, so the
    oversized test would pass without the cap ever being consulted.
    """
    body = warm._warm_spec_body(stem, {}, "planted")
    if pad:
        body["pad"] = "x" * pad
    return json.dumps(body)


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
    monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))

    assert warm._warm_spec_is_foreign(link) is True

    # The verdict's whole purpose: the sweep must leave both the link and its target alone.
    warm._remove_warm_mint_specs()

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

    warm._remove_warm_mint_specs()

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


# ── servability: a set that SHRANK is still servable ──


def _plan(entries: dict[str, dict[str, Any]]) -> warm._WarmSpecPlan:
    return warm._WarmSpecPlan(
        all_agent="all" if entries else "",
        specs={},
        entries=entries,
        digest=repr(sorted(entries.items())),
    )


def test_a_shrunk_candidate_set_is_served_by_the_running_process():
    resident = _plan({"linear": {"url": "https://l"}, "vercel": {"url": "https://v"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://l"}})) is True


def test_a_changed_authorization_ask_is_not_servable():
    resident = _plan({"linear": {"url": "https://l"}})
    assert warm._plan_is_servable(resident, _plan({"linear": {"url": "https://other"}})) is False
    assert warm._plan_is_servable(resident, _plan({"notion": {"url": "https://n"}})) is False


def test_a_process_that_enumerated_nothing_serves_nothing():
    assert warm._plan_is_servable(_plan({}), _plan({"linear": {"url": "https://l"}})) is False


# ── candidates: a granted provider is warmed into the spec but asked for no URL ──


def test_a_granted_provider_is_not_an_activation_candidate(monkeypatch: pytest.MonkeyPatch):
    universe = [_provider("granted"), _provider("fresh")]
    monkeypatch.setattr(warm, "grant_present", lambda url: "granted" in url)
    assert [p["slug"] for p in warm._warm_activation_candidates(universe)] == ["fresh"]


def test_an_unreadable_inventory_warms_nothing_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        warm, "warm_spec_providers", lambda: (_ for _ in ()).throw(OSError("config unreadable"))
    )
    assert warm._warm_candidate_scan() == ([], [])


def test_a_warm_session_never_injects_mcp_servers():
    """A remote server through ``session/new`` kills the process and every verifier."""
    assert warm._warm_session_mcp_servers() == []


# ── one activation fills the whole table ──


@pytest.fixture
def _stub_activation(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the process, the audit log and the grant watcher."""

    async def _no_watcher(slug: str, mcp_url: str, token: str) -> None:
        return None

    async def _no_settle(activation: int, in_use: set[int]) -> None:
        return None

    monkeypatch.setattr(warm, "_mint_watcher", _no_watcher)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *a, **k: None)
    monkeypatch.setattr(warm._warm_mint, "settle_activation", _no_settle)


def _result(
    providers: list[Provider],
    requests: list[dict[str, str]],
    *,
    generation: int = 7,
    activation: int = 3,
) -> warm._WarmMintResult:
    return warm._WarmMintResult(
        generation=generation, activation=activation, providers=providers, requests=requests
    )


def _returns(result: warm._WarmMintResult | None):
    async def _activate(*, slug: str = ""):
        return result

    return _activate


async def _claim(*slugs: str) -> dict[str, str]:
    """Claim helper for the tests that do not exercise the displaced-row half."""
    claims, displaced = await warm._claim_shared_mints(list(slugs))
    assert not displaced, "these tests claim into empty slots"
    return claims


@pytest.mark.asyncio
async def test_one_activation_stamps_every_row_with_its_generation_and_activation(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    providers = [_provider("linear"), _provider("vercel")]
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                providers,
                [
                    {"serverName": "linear", "oauthUrl": "https://l/consent"},
                    {"serverName": "vercel", "oauthUrl": "https://v/consent"},
                ],
            )
        ),
    )

    minted = await warm.warm_mint_all(providers)

    assert sorted(minted) == ["linear", "vercel"]
    for slug in ("linear", "vercel"):
        row = _mints[slug]
        assert row["state"] == "waiting"
        assert row["generation"] == 7 and row["activation"] == 3
        assert row["shared"] is True
        # Every row carries the cold engine's row identity, which is what the grant
        # watcher re-checks before it writes a verdict.
        assert row["token"]
    assert len({_mints["linear"]["token"], _mints["vercel"]["token"]}) == 2


@pytest.mark.asyncio
async def test_a_claim_the_activation_did_not_cover_is_released_not_left_minting(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    claimed = [_provider("linear"), _provider("stranded")]
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result([claimed[0]], [{"serverName": "linear", "oauthUrl": "https://l/consent"}])
        ),
    )

    assert await warm.warm_mint_all(claimed) == ["linear"]
    assert "stranded" not in _mints, "an uncovered claim must not sit in the table forever"


@pytest.mark.asyncio
async def test_a_failed_activation_releases_every_claim(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    monkeypatch.setattr(warm, "_warm_activate", _returns(None))
    assert await warm.warm_mint_all([_provider("linear")]) == []
    assert _mints == {}


@pytest.mark.asyncio
async def test_a_cold_held_row_is_never_replaced_by_a_warm_claim():
    _mints["linear"] = {"state": "waiting", "client": object(), "oauth_url": "https://cold"}
    assert await warm._claim_shared_mints(["linear"]) == ({}, [])
    assert _mints["linear"]["oauth_url"] == "https://cold"


@pytest.mark.asyncio
async def test_expiry_is_narrowed_to_the_one_generation_whose_verifier_died():
    """Withdrawal follows the verifier, so it follows the generation. A row on any OTHER
    generation is redeemable on a process this expiry is not about."""
    _mints["a"] = {"state": "waiting", "shared": True, "generation": 1, "token": "tok-a"}
    _mints["b"] = {"state": "waiting", "shared": True, "generation": 2, "token": "tok-b"}
    # A claim not yet stamped with a generation belongs to an activation still in flight.
    _mints["c"] = {"state": "minting", "shared": True, "token": "tok-c"}
    assert await warm._expire_shared_mints("mint_process_gone", generation=1) == ["a"]
    assert _mints["b"]["state"] == "waiting"
    assert _mints["c"]["state"] == "minting"


# ── defect #6110: a batch timestamp is not a row identity ──
#
# The fence separating one activation's rows from the next at the SAME slug was
# ``entry["started"] == started``, a ``time.monotonic()`` reading taken once per
# ``warm_mint_all`` call. ``_new_mint_token``'s own docstring records why that is not a row
# identity: the clock has ~15.6ms granularity on Windows, so two Connects for one provider
# inside a single tick read as the same row and every guard built on it fails OPEN.


@pytest.mark.asyncio
async def test_a_late_absorb_does_not_write_its_url_over_a_newer_claim(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """Two overlapping activations at one slug. The first one's absorb lands last, and it
    must not replace the second's claim -- the URL it carries belongs to a session the
    second Connect is not waiting on."""
    # A coarse clock, as Windows has: both claims are taken inside one tick, so a fence
    # reading ``started`` cannot tell the two rows apart.
    monkeypatch.setattr(warm.time, "monotonic", lambda: 100.0)
    provider = _provider("linear")

    first = await _claim("linear")
    second, displaced = await warm._claim_shared_mints(["linear"])
    assert first and second
    assert first["linear"] != second["linear"], "each claim needs its own row identity"
    assert [row["token"] for row in displaced] == [
        first["linear"]
    ], "the row the second claim replaced comes back for the caller to dispose"

    stale = _result(
        [provider], [{"serverName": "linear", "oauthUrl": "https://stale/consent"}], activation=1
    )
    assert await warm._absorb_warm_requests(stale, first) == []
    row = _mints["linear"]
    assert row.get("oauth_url") != "https://stale/consent"
    assert row["state"] == "minting", "the newer claim is still the activation's to fill"
    assert row["token"] == second["linear"]


@pytest.mark.asyncio
async def test_a_release_leaves_a_newer_claim_at_the_same_slug_alone(
    monkeypatch: pytest.MonkeyPatch,
):
    """The release path needs the same identity: dropping the row a newer activation is
    filling would strand that Connect on a card with no claim and no URL."""
    monkeypatch.setattr(warm.time, "monotonic", lambda: 100.0)
    first = await _claim("linear")
    second, _ = await warm._claim_shared_mints(["linear"])

    await warm._release_shared_claims(first)

    assert _mints["linear"]["token"] == second["linear"]


# ── defect: a credential-bearing URL must be refused before it is stored ──


@pytest.mark.asyncio
async def test_a_url_carrying_a_credential_is_refused_rather_than_stored(_stub_activation):
    """The same gate the cold mint and the chat consent banner apply. A refused URL is
    never written to the row, so nothing can serve it to a card."""
    provider = _provider("linear")
    claims = await _claim("linear")
    assert claims

    bearing = _result(
        [provider],
        [
            {
                "serverName": "linear",
                "oauthUrl": "https://linear.example/authorize?state=AKIA" + "IOSFODNN7EXAMPLE1",
            }
        ],
    )
    assert await warm._absorb_warm_requests(bearing, claims) == []
    assert "linear" not in _mints, "the claim is released so the card asks for a fresh mint"


@pytest.mark.asyncio
async def test_the_screen_is_the_shared_security_gate_not_a_local_copy(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """A second implementation of the predicate would drift from the one the rest of the
    product enforces, so the module must call through to security's."""
    assert warm.oauth_url_contains_credential is security.oauth_url_contains_credential

    monkeypatch.setattr(warm, "oauth_url_contains_credential", lambda url: True)
    claims = await _claim("linear")
    plain = _result(
        [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
    )
    assert await warm._absorb_warm_requests(plain, claims) == []


@pytest.mark.asyncio
async def test_a_clean_url_still_lands_on_the_row(_stub_activation):
    """The screen must not refuse the ordinary case it exists to let through."""
    claims = await _claim("linear")
    clean = _result(
        [_provider("linear")],
        [{"serverName": "linear", "oauthUrl": "https://linear.example/oauth/authorize?state=xyz"}],
    )
    assert await warm._absorb_warm_requests(clean, claims) == ["linear"]
    assert _mints["linear"]["state"] == "waiting"


# ── defect: a cancel between claim and activation strands every claimed row ──


@pytest.mark.asyncio
async def test_a_cancel_between_claim_and_activation_releases_every_row(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """``minting`` rows are invisible to ``expire_dead_mints`` (it judges ``waiting`` only)
    and they keep ``_shared_mints_pending`` true, so a row left behind here is never
    withdrawn AND the process is never retired."""

    async def _cancelled(*, slug: str = ""):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_warm_activate", _cancelled)

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("linear"), _provider("vercel")])

    assert _mints == {}, "a cancelled activation must not leave a claim minting with no watcher"
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_a_real_task_cancel_mid_activation_still_completes_the_release(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """The stronger form: not a raised ``CancelledError`` but a real ``Task.cancel()`` while
    the coroutine is suspended. The cleanup awaits a lock and a dispose, so this is what
    proves those awaits run rather than being interrupted a second time."""
    entered = asyncio.Event()

    async def _hangs(*, slug: str = ""):
        entered.set()
        await asyncio.Event().wait()  # never completes; the cancel lands here

    monkeypatch.setattr(warm, "_warm_activate", _hangs)

    task = asyncio.get_running_loop().create_task(warm.warm_mint_all([_provider("linear")]))
    await entered.wait()
    assert _mints["linear"]["state"] == "minting", "the claim is taken before the activation"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _mints == {}
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_a_cancel_after_the_activation_lands_releases_the_rows_too(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """The window does not close when the activation returns: an absorb interrupted
    mid-flight leaves the same orphaned claims."""

    async def _cancelling_absorb(result, claims):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
            )
        ),
    )
    monkeypatch.setattr(warm, "_absorb_warm_requests", _cancelling_absorb)

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("linear")])

    assert _mints == {}


# ── defect: the claim loop's own await is a cancel window OUTSIDE the protected region ──
#
# ``warm_mint_all`` takes its claims BEFORE entering the try whose ``except BaseException``
# rolls them back, so any await inside the claim loop is unprotected. Replacing an existing
# row awaits ``_dispose_mint``, which suspends on a client teardown and again on the
# shielded spec removal in its ``finally`` -- so a cancellation there unwinds with earlier
# slugs already installed as ``minting`` and no caller holding their tokens.


@pytest.mark.asyncio
async def test_a_cancel_while_replacing_a_row_does_not_strand_the_earlier_claims(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """``vercel`` is claimed first and needs no dispose; ``linear`` already holds a row, so
    replacing it is the await the cancellation lands in. ``vercel`` must not survive it."""
    real_dispose = warm._dispose_mint

    async def _cancel_on_the_displaced_row(entry):
        # Only the row being REPLACED carries a URL; our own fresh claims do not, so the
        # rollback's own disposes still run for real.
        if entry.get("oauth_url"):
            raise asyncio.CancelledError()
        await real_dispose(entry)

    monkeypatch.setattr(warm, "_dispose_mint", _cancel_on_the_displaced_row)
    _mints["linear"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 5,
        "activation": 1,
        "token": "older-linear-row",
    }

    with pytest.raises(asyncio.CancelledError):
        await warm.warm_mint_all([_provider("vercel"), _provider("linear")])

    assert _mints == {}, "an interrupted claim must not leave a row minting with no owner"
    # `minting` rows are invisible to expire_dead_mints and keep the pending check true, so
    # a survivor here is never withdrawn AND stops the process from ever being retired.
    assert warm._shared_mints_pending() is False


@pytest.mark.asyncio
async def test_the_claim_loop_itself_contains_no_await():
    """The structural invariant behind the test above: with no await between the first row
    being installed and the loop ending, there is no window to be cancelled in."""
    body = ast.parse(inspect.getsource(warm._claim_shared_mints)).body[0]
    loops = [node for node in ast.walk(body) if isinstance(node, ast.For)]
    assert loops, "guard is reading the wrong tree"
    offenders = [
        type(node).__name__
        for loop in loops
        for node in ast.walk(loop)
        if isinstance(node, ast.Await)
    ]
    assert not offenders, f"awaits inside the claim loop: {offenders} -- move them out"


# ── defect: the retry's expiry pass is unscoped ──


@pytest.mark.asyncio
async def test_a_retry_does_not_withdraw_a_parked_generations_live_url(
    monkeypatch: pytest.MonkeyPatch,
):
    """A parked generation's process still holds its verifier and its session still answers
    the redirect, so its URL is redeemable. An expiry pass not scoped to the DEAD generation
    withdraws a working link."""
    _mints["parked"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://p/consent",
        "generation": 3,
        "activation": 1,
        "token": "tok-parked",
    }

    attempts: list[str] = []

    async def _dies_then_declines(*, slug: str = ""):
        attempts.append(slug)
        if len(attempts) == 1:
            raise warm._WarmMintDied("ProcessGone")
        return None

    monkeypatch.setattr(warm._warm_mint, "mint_for", _dies_then_declines)

    assert await warm._warm_activate() is None
    assert len(attempts) == 2, "the death is retried once"
    assert _mints["parked"]["state"] == "waiting"
    assert _mints["parked"]["oauth_url"] == "https://p/consent"


@pytest.mark.asyncio
async def test_a_dead_generations_rows_are_expired_by_the_kill_itself(
    monkeypatch: pytest.MonkeyPatch,
):
    """Why the retry needs no expiry pass of its own: the stand-down that precedes
    ``_WarmMintDied`` already withdrew exactly the rows whose verifier died, scoped to that
    generation and leaving every other generation alone."""
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    _mints["dead"] = {"state": "waiting", "shared": True, "generation": 4, "token": "t4"}
    _mints["parked"] = {"state": "waiting", "shared": True, "generation": 3, "token": "t3"}

    await warm._warm_mint._park_or_kill_locked()

    assert _mints["dead"]["state"] == "expired"
    assert _mints["dead"]["reason"] == "mint_process_gone"
    assert _mints["parked"]["state"] == "waiting"


# ── defect: a parked generation is stranded when the current process dies ──


def _parked_session(generation: int) -> warm._WarmSession:
    """An unsettled session, so the sweep holds it exactly as a live activation's would be."""
    return warm._WarmSession(
        generation=generation, handle=object(), expires_at=time.monotonic() + 600
    )


@pytest.mark.asyncio
async def test_the_reaper_drains_parked_generations_after_the_current_process_dies(
    monkeypatch: pytest.MonkeyPatch,
):
    """Nothing else on any path reaches a parked generation once the current process is gone:
    a new mint would sweep it, and the leak is exactly the case where none arrives. So the
    process and its sessions stay resident forever after its last card completes."""
    parked_runtime = _Runtime(True)
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {9: _parked_session(5)})
    # Generation 5 is parked because this card still points at it.
    _mints["parked"] = {
        "state": "waiting",
        "shared": True,
        "generation": 5,
        "activation": 9,
        "token": "t5",
    }

    sweeps = 0
    real_sweep = warm._warm_mint.sweep_retiring

    async def _counting_sweep():
        nonlocal sweeps
        sweeps += 1
        if sweeps == 2:
            # The card completes, so nothing needs generation 5 any more.
            _mints.pop("parked", None)
        await real_sweep()

    monkeypatch.setattr(warm._warm_mint, "sweep_retiring", _counting_sweep)

    await asyncio.wait_for(warm._warm_mint_reaper(6), timeout=5)

    assert killed == [parked_runtime], "the parked generation's process was never retired"
    assert warm._warm_mint._retiring == []
    assert warm._warm_mint._sessions == {}, "its sessions own the loopback servers"


# ── a dead shared process is re-armed once during the page-owned lifecycle ──


@pytest.mark.asyncio
async def test_the_reaper_rearms_a_dead_generation_once(monkeypatch: pytest.MonkeyPatch):
    """The page already paid for warming, so a mid-visit death is replaced without a click."""
    rearmed: list[int] = []

    async def _record_rearm(generation: int) -> list[str]:
        rearmed.append(generation)
        return ["linear"]

    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _record_rearm)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    warm._warm_mint.arm_supervision()

    await asyncio.wait_for(warm._warm_mint_reaper(6), timeout=5)

    assert rearmed == [6]
    assert warm._warm_mint._rearms_remaining == 0


@pytest.mark.asyncio
async def test_reaper_audits_the_grant_scan_before_replacement_warm(
    monkeypatch: pytest.MonkeyPatch,
):
    """A page-owned re-arm acts on the same credential observation as premint."""
    from kiro_crew import hooks

    assert warm._GRANT_PRESENCE_READ_ID in hooks._AUDIT_ONLY_READ_IDS
    provider = _provider("linear")
    events: list[tuple[Any, ...]] = []

    def _audit(read_id: str, outcome: str) -> bool:
        events.append(("audit", read_id, outcome))
        return True

    async def _record_warm(
        providers: list[Provider] | None = None, *, arm_supervision: bool = True
    ) -> list[str]:
        events.append(("warm", [item["slug"] for item in providers or []], arm_supervision))
        warm._warm_mint._runtime = _Runtime(True)
        return ["linear"]

    monkeypatch.setattr(hooks, "emit_internal_read_audit", _audit)
    monkeypatch.setattr(warm, "mintable_providers", lambda: [provider])
    monkeypatch.setattr(warm, "warm_mint_all", _record_warm)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 6)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    warm._warm_mint.arm_supervision()

    await asyncio.wait_for(warm._warm_mint_reaper(6), timeout=5)

    assert events == [
        ("audit", "connections_premint.oauth_grant_presence", "success"),
        ("warm", ["linear"], False),
    ]


@pytest.mark.asyncio
async def test_rearm_is_single_flight_and_cannot_form_a_restart_loop(
    monkeypatch: pytest.MonkeyPatch,
):
    """Concurrent observers share one replacement, and its death spends no hidden retry."""
    gate = asyncio.Event()
    calls: list[int] = []

    async def _gated_rearm(generation: int) -> list[str]:
        calls.append(generation)
        await gate.wait()
        return []

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _gated_rearm)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    warm._warm_mint.arm_supervision()

    first = warm._warm_mint.start_rearm(3)
    second = warm._warm_mint.start_rearm(3)

    assert first is not None and second is first
    await asyncio.sleep(0)
    assert calls == [3]
    gate.set()
    await first
    await asyncio.sleep(0)

    assert warm._warm_mint.start_rearm(3) is None
    assert calls == [3], "one automatic replacement must not recursively earn another"


@pytest.mark.asyncio
async def test_rearm_skips_connected_and_indeterminate_providers(
    monkeypatch: pytest.MonkeyPatch, _stub_activation: None
):
    """Recovery initiates consent only for a grant whose absence was confirmed."""
    universe = [_provider("fresh"), _provider("granted"), _provider("unreadable")]
    reads: list[str] = []

    def _presence(url: str) -> bool | None:
        reads.append(url)
        if "unreadable" in url:
            return None
        return "granted" in url

    monkeypatch.setattr(warm, "warm_spec_providers", lambda: universe)
    monkeypatch.setattr(warm, "grant_present", _presence)
    monkeypatch.setattr(
        warm,
        "_warm_activate",
        _returns(
            _result(
                [universe[0]],
                [{"serverName": "fresh", "oauthUrl": "https://fresh.example/consent"}],
                generation=5,
                activation=7,
            )
        ),
    )
    monkeypatch.setattr(warm._warm_mint, "_supervision_armed", True)
    monkeypatch.setattr(warm._warm_mint, "_rearms_remaining", 0)

    assert await warm._rearm_dead_warm_mint(4) == ["fresh"]

    assert list(_mints) == ["fresh"]
    assert _mints["fresh"]["state"] == "waiting"
    assert _mints["fresh"]["generation"] == 5
    assert warm._warm_mint._rearms_remaining == 0
    assert sorted(reads) == sorted(provider["mcp_url"] for provider in universe)


@pytest.mark.asyncio
async def test_an_explicit_page_warm_arms_one_replacement(
    monkeypatch: pytest.MonkeyPatch, _stub_activation: None
):
    monkeypatch.setattr(warm, "_warm_activate", _returns(None))

    assert await warm.warm_mint_all([_provider("linear")]) == []
    assert warm._warm_mint._supervision_armed is True
    assert warm._warm_mint._rearms_remaining == warm._WARM_REARM_ATTEMPTS


@pytest.mark.asyncio
async def test_shutdown_cancels_an_inflight_rearm_and_disarms_recovery(
    monkeypatch: pytest.MonkeyPatch,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _blocked_rearm(generation: int) -> list[str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def _kill(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_rearm_dead_warm_mint", _blocked_rearm)
    monkeypatch.setattr(warm, "_kill_quietly", _kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_generation", 8)
    warm._warm_mint.arm_supervision()
    task = warm._warm_mint.start_rearm(8)
    assert task is not None
    await started.wait()

    await warm._warm_mint.shutdown()

    assert cancelled.is_set()
    assert task.cancelled()
    assert warm._warm_mint._rearm_task is None
    assert warm._warm_mint._supervision_armed is False
    assert warm._warm_mint._rearms_remaining == 0
    assert warm._warm_mint.start_rearm(8) is None


@pytest.mark.asyncio
async def test_a_failed_respawn_still_drains_the_generation_it_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The same leak by the other route: the stand-down cancels the old reaper, so a spawn
    that then fails leaves a parked generation with no reaper at all."""
    parked_runtime = _Runtime(True)
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    def _explode(*a, **k):
        raise OSError("kiro-cli is not installed")

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan: None)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _explode)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None

    drain = warm._warm_mint._reaper
    assert drain is not None, "a parked generation with no process needs a drain task"
    await asyncio.wait_for(drain, timeout=5)
    assert killed == [parked_runtime]
    assert warm._warm_mint._retiring == []


# ── defect: a refused spec is still activated BY NAME ──
#
# The writer audits and SKIPS a foreign file at a planned spec path, which protects the
# file. It does not protect the ACTIVATION: the runtime is handed `agent=<fixed name>` and
# kiro-cli resolves that name off the same directory, so a hand-written agent sitting at
# the name we declined to own gets executed and its `mcpServers` commands initialize.


@pytest.mark.asyncio
async def test_a_foreign_spec_at_a_planned_path_aborts_warming_instead_of_being_activated(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The refusal has to reach the SPAWN, not just the write."""
    _foreign_spec(_agents_dir, warm._WARM_BASE_AGENT)
    constructed: list[str] = []

    def _factory():
        def _build(**kwargs):
            constructed.append(str(kwargs.get("agent")))
            raise AssertionError("a refused spec must never be activated")

        return _build

    monkeypatch.setattr(warm, "_acp_runtime_factory", _factory)

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is None
    assert constructed == [], "the runtime was constructed on a spec we refused to own"


@pytest.mark.asyncio
async def test_a_missing_planned_spec_also_aborts_warming(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Absence and foreignness are the same answer here: an unreadable spec path is not a
    spec of ours, and `_warm_spec_is_foreign` answers False for an absent file, so the
    verification has to test existence as well as ownership."""
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan: None)  # writes nothing
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

        async def spawn(self):
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


# ── defect: the stand-down -> spawn transition is not BaseException-safe ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_spec_write_still_arms_the_drain(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Third route into the parked-generation leak. The park has already cancelled the old
    reaper, so a cancellation before a replacement exists leaves the parked process with
    nothing that will ever sweep it."""
    parked_runtime = _Runtime(True)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 0)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, parked_runtime)])

    def _cancel(plan):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_write_warm_mint_specs", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._ensure_locked([_provider("linear")])

    drain = warm._warm_mint._reaper
    assert drain is not None, "a park with no replacement reaper needs a drain armed"
    drain.cancel()


@pytest.mark.asyncio
async def test_a_cancel_during_the_spawn_kills_the_partial_runtime(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """A runtime that was constructed and may have forked a child must not be dropped on
    the floor when the spawn await is cancelled."""
    killed: list[Any] = []

    async def _record_kill(runtime):
        killed.append(runtime)
        return True

    class _CancelsOnSpawn:
        def __init__(self, **kwargs):
            pass

        async def spawn(self):
            raise asyncio.CancelledError()

        def is_alive(self):
            return False

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _CancelsOnSpawn)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._ensure_locked([_provider("linear")])

    assert len(killed) == 1, "the constructed runtime was leaked"


# ── defect: settlement is not reached on every post-activation exit ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_credential_screen_still_settles_the_session(
    monkeypatch: pytest.MonkeyPatch,
):
    """The screen's `to_thread` await sits between the session's creation and its
    settlement. A session left `settled=False` is permanently ineligible for the sweep, so
    it and its loopback callback servers accumulate for the life of the process."""
    settled: list[int] = []

    async def _record_settle(activation: int, in_use: set[int]) -> None:
        settled.append(activation)

    def _cancel(urls):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm._warm_mint, "settle_activation", _record_settle)
    monkeypatch.setattr(warm, "_credential_bearing_slugs", _cancel)

    claims = await _claim("linear")
    result = _result(
        [_provider("linear")],
        [{"serverName": "linear", "oauthUrl": "https://l/consent"}],
        activation=11,
    )

    with pytest.raises(asyncio.CancelledError):
        await warm._absorb_warm_requests(result, claims)

    assert settled == [11], "an unsettled session can never be collected"


@pytest.mark.asyncio
async def test_a_failing_absorb_still_settles_the_session(monkeypatch: pytest.MonkeyPatch):
    """Not only cancellation: any raise after the activation exists has the same
    consequence, so the settlement belongs in a finally rather than on one branch."""
    settled: list[int] = []

    async def _record_settle(activation: int, in_use: set[int]) -> None:
        settled.append(activation)

    def _explode(urls):
        raise OSError("the operator endpoint file is unreadable")

    monkeypatch.setattr(warm._warm_mint, "settle_activation", _record_settle)
    monkeypatch.setattr(warm, "_credential_bearing_slugs", _explode)

    claims = await _claim("linear")
    result = _result(
        [_provider("linear")], [{"serverName": "linear", "oauthUrl": "https://l/consent"}]
    )

    with pytest.raises(OSError):
        await warm._absorb_warm_requests(result, claims)

    assert settled == [3]


# ── defect (found by the systematic audit, not the lane): the retiring sweep drops
# generations from its own list before killing them ──


@pytest.mark.asyncio
async def test_a_cancel_mid_sweep_leaves_the_unkilled_generations_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_sweep_retiring_locked` assigns the keep-list before it kills the drop-list, so a
    cancellation partway through used to remove BOTH generations from `_retiring` while only
    one was actually killed -- `parked_count()` then reads zero and the drain exits, so
    nothing ever retries. A generation whose kill completed is gone; one whose kill was
    interrupted stays parked for a later sweep."""
    first, second = _Runtime(False), _Runtime(False)
    killed: list[Any] = []

    async def _kill_then_cancel(runtime):
        killed.append(runtime)
        if len(killed) == 2:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kill_then_cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(1, first), (2, second)])

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint.sweep_retiring()

    assert killed == [first, second]
    assert warm._warm_mint.parked_count() == 1, "the ungathered generation must stay parked"
    assert warm._warm_mint._retiring == [(2, second)]


# ── a kill that TIMES OUT is not a kill ──
#
# ``_kill_quietly`` swallowed every ``Exception`` and returned None, so a kill that timed out
# was indistinguishable from one that worked: each caller then dropped the only reference to
# a child that is still running. ``asyncio.TimeoutError`` IS an ``Exception``, so none of the
# three retention paths above -- all built for a ``CancelledError``, which is not -- ever saw
# it. Repeated activations therefore accumulated live kiro-cli processes, their sessions and
# their loopback listeners, with nothing left that could ever retire them.


class _UnkillableRuntime:
    """A process whose kill fails the way a timeout does: a plain ``Exception``.

    Fails ``failures`` times and then succeeds, so one test can pin both halves of the
    contract -- retained while the kill does not take, released once it does.
    """

    def __init__(self, failures: int = 1) -> None:
        self._failures = failures
        self.kill_attempts = 0

    def is_alive(self) -> bool:
        return True

    async def kill(self) -> None:
        self.kill_attempts += 1
        if self.kill_attempts <= self._failures:
            raise TimeoutError("kill timed out")


@pytest.fixture
def _no_drain(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the drain task and the spec sweep, keeping the arming observable."""

    async def _noop() -> None:
        return None

    monkeypatch.setattr(warm, "_drain_parked_generations", _noop)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)


@pytest.mark.asyncio
async def test_a_sweep_whose_kill_times_out_keeps_the_generation_parked(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """RED before the fix: the pair was popped and the surviving child forgotten."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(5, doomed)])

    await warm._warm_mint.sweep_retiring()

    assert doomed.kill_attempts == 1
    assert warm._warm_mint._retiring == [(5, doomed)], "a surviving child must stay tracked"
    assert warm._warm_mint.parked_count() == 1

    # The next pass is what retires it -- and only then does the list clear.
    await warm._warm_mint.sweep_retiring()

    assert doomed.kill_attempts == 2
    assert warm._warm_mint._retiring == []


@pytest.mark.asyncio
async def test_a_stand_down_whose_kill_times_out_re_parks_the_process(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """``_park_or_kill_locked`` clears ``_runtime`` first, so this list is the last reference."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 7)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint._park_or_kill_locked()

    assert warm._warm_mint._retiring == [(7, doomed)]
    assert warm._warm_mint._reaper is not None, "nothing was scheduled to retry the kill"


@pytest.mark.asyncio
async def test_a_hard_teardown_whose_kill_times_out_keeps_the_child_tracked(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """``shutdown()`` empties both lists up front; an unkilled child must come back."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint.shutdown()

    assert warm._warm_mint._retiring == [(9, doomed)]
    assert warm._warm_mint._reaper is not None


@pytest.mark.asyncio
async def test_an_abandoned_spawn_whose_kill_times_out_is_tracked_not_dropped(
    monkeypatch: pytest.MonkeyPatch, _no_drain: None
):
    """The one path with no generation of its own: tracked under a key no row can carry, so
    every sweep reads it as needed by nobody and retries the kill until it takes."""
    doomed = _UnkillableRuntime(failures=1)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    await warm._warm_mint._abandon_spawn_locked(doomed)

    assert warm._warm_mint._retiring == [(warm._WARM_UNKEYED_GENERATION, doomed)]
    assert warm._WARM_UNKEYED_GENERATION < 0, "a real generation would collide with its rows"
    assert not warm._generation_holds_live_rows(warm._WARM_UNKEYED_GENERATION)


@pytest.mark.asyncio
async def test_a_spec_sweep_is_withheld_while_an_unkilled_child_still_needs_its_spec():
    """A spec removed under a process that is still running strands it -- the same rule the
    parked path follows, and the reason the sweep is gated on the kill having taken."""
    removed: list[bool] = []
    doomed = _UnkillableRuntime(failures=1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(warm, "_remove_warm_mint_specs", lambda: removed.append(True))
        patch.setattr(warm._warm_mint, "_runtime", None)
        patch.setattr(warm._warm_mint, "_retiring", [])

        assert await warm._warm_mint._kill_generation(3, doomed) is False
        assert not removed, "the surviving child's spec was unlinked underneath it"

        assert await warm._warm_mint._kill_generation(3, doomed) is True
        assert removed == [True]


# ── the invariant itself, pinned where it is mechanically checkable ──


def _await_protections(func: Any) -> list[tuple[str, list[str]]]:
    """Every await in ``func`` as ``(source text, enclosing try protections)``.

    A protection reads ``finally`` or ``except BaseException`` -- the only two shapes a
    ``CancelledError`` cannot walk past. An ``except Exception`` is recorded so the guard can
    say what WAS there rather than only that nothing qualified.
    """
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    found: list[tuple[str, list[str]]] = []

    def label(node: ast.Try) -> str:
        kinds = ["finally"] if node.finalbody else []
        for handler in node.handlers:
            if handler.type is None:
                kinds.append("except BaseException")
            elif isinstance(handler.type, ast.Name):
                kinds.append(f"except {handler.type.id}")
            else:
                kinds.append("except <expr>")
        return "+".join(kinds)

    def walk(node: ast.AST, stack: list[str]) -> None:
        if isinstance(node, ast.Try):
            here = label(node)
            for child in node.body:
                walk(child, stack + [here])
            for handler in node.handlers:
                for child in handler.body:
                    walk(child, stack)
            for child in node.finalbody + node.orelse:
                walk(child, stack)
            return
        if isinstance(node, ast.Await):
            found.append((ast.get_source_segment(source, node) or "", list(stack)))
        for child in ast.iter_child_nodes(node):
            walk(child, stack)

    for child in ast.iter_child_nodes(tree):
        walk(child, [])
    return found


#: The awaits that sit between a state mutation and that mutation's settlement or cleanup.
#: Named individually rather than "this function has SOME protected try", because the coarse
#: form passed while a sibling await in the same function was still bare.
_MUST_BE_PROTECTED = [
    ("warm_mint_all", "_warm_activate", warm.warm_mint_all),
    ("warm_mint_all", "_absorb_warm_requests", warm.warm_mint_all),
    ("warm_mint_all", "_dispose_displaced_rows", warm.warm_mint_all),
    ("_absorb_warm_requests", "_credential_bearing_slugs", warm._absorb_warm_requests),
    (
        "_activate_locked",
        "asyncio.shield(create)",
        warm._WarmMintRuntime._activate_locked,
    ),
    (
        "_activate_locked",
        "_collect_oauth_requests",
        warm._WarmMintRuntime._activate_locked,
    ),
    (
        "_recover_redrained_requests",
        "_dispose_displaced_rows",
        warm._recover_redrained_requests,
    ),
    (
        "_recover_redrained_requests",
        "_absorb_warm_requests",
        warm._recover_redrained_requests,
    ),
    (
        "_abandon_session_creation_locked",
        "_destroy_session_quietly",
        warm._WarmMintRuntime._abandon_session_creation_locked,
    ),
    (
        "_sweep_sessions_locked",
        "_destroy_session_quietly",
        warm._WarmMintRuntime._sweep_sessions_locked,
    ),
    ("_ensure_locked", "_write_warm_mint_specs", warm._WarmMintRuntime._ensure_locked),
    ("_ensure_locked", "runtime.spawn()", warm._WarmMintRuntime._ensure_locked),
    ("shutdown", "asyncio.gather(rearm", warm._WarmMintRuntime.shutdown),
    ("_sweep_retiring_locked", "_kill_generation", warm._WarmMintRuntime._sweep_retiring_locked),
    ("_park_or_kill_locked", "_kill_generation", warm._WarmMintRuntime._park_or_kill_locked),
    ("_retire_locked", "_kill_quietly", warm._WarmMintRuntime._retire_locked),
]

_PROTECTING = ("finally", "except BaseException")


def test_every_post_mutation_await_is_individually_protected():
    """Both review rounds found the same class: an await between a state mutation and its
    settlement or cleanup, guarded only by an `except Exception` that a CancelledError walks
    straight past. Bound to the SPECIFIC await rather than the function, so a newly-added
    sibling await in an already-protected function cannot pass by association."""
    for name, target, func in _MUST_BE_PROTECTED:
        awaits = _await_protections(func)
        matching = [(text, stack) for text, stack in awaits if target in text]
        assert matching, (
            f"{name}: no await matching {target!r} -- the guard is reading the wrong code, "
            "so update this table rather than deleting the entry"
        )
        for text, stack in matching:
            assert any(any(kind in entry for kind in _PROTECTING) for entry in stack), (
                f"{name}: `{text}` is guarded only by {stack or ['nothing']} -- a "
                "CancelledError walks past every `except Exception`, which is exactly the "
                "bug class three review rounds found"
            )


# ── two more of the same class, found by the systematic audit ──


@pytest.mark.asyncio
async def test_a_cancel_during_the_stand_down_kill_re_parks_the_process(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_park_or_kill_locked` clears `_runtime` and cancels the reaper BEFORE it awaits the
    kill, so a cancellation inside that kill used to drop the only reference to a process
    that is still running -- neither registered, nor parked, nor dead."""
    doomed = _Runtime(False)

    async def _cancel(runtime):
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_kill_quietly", _cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", doomed)
    monkeypatch.setattr(warm._warm_mint, "_generation", 7)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._park_or_kill_locked()

    assert warm._warm_mint._retiring == [(7, doomed)], "an unkilled process must stay tracked"
    drain = warm._warm_mint._reaper
    assert drain is not None, "and something must be scheduled to retire it"
    drain.cancel()


@pytest.mark.asyncio
async def test_a_cancel_during_the_hard_teardown_re_parks_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_retire_locked` empties `_retiring` and clears `_runtime` up front, so a
    cancellation partway through its kill loop used to leak every remaining process with no
    reference left anywhere."""
    parked, current = _Runtime(False), _Runtime(False)
    killed: list[Any] = []

    async def _kill_then_cancel(runtime):
        killed.append(runtime)
        if len(killed) == 2:
            raise asyncio.CancelledError()
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kill_then_cancel)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm._warm_mint, "_runtime", current)
    monkeypatch.setattr(warm._warm_mint, "_generation", 9)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(8, parked)])
    monkeypatch.setattr(warm._warm_mint, "_reaper", None)

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._retire_locked()

    # The parked one was killed first and is gone; the current one was interrupted.
    assert killed == [parked, current]
    assert warm._warm_mint._retiring == [(9, current)]
    drain = warm._warm_mint._reaper
    assert drain is not None
    drain.cancel()


# ── defect: the CURRENT generation number can name a PARKED process ──
#
# Only a successful spawn bumps `_generation`, so a stand-down leaves the live process in
# `_retiring` under the number that is still `self._generation`, with `self._runtime`
# cleared. The equality branch answered `is_alive()` for that number and so reported the
# parked process dead -- withdrawing redeemable URLs, and then letting the next sweep kill
# the very process the park existed to preserve.


@pytest.mark.asyncio
async def test_a_generation_parked_mid_respawn_keeps_its_rows(monkeypatch: pytest.MonkeyPatch):
    """A status scan runs without the runtime lock, so it sees the window between the
    stand-down and its replacement spawn."""
    live = _Runtime(True)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    # Exactly the mid-respawn state: stood down, replacement not yet spawned, so the
    # parked entry still carries the CURRENT number.
    monkeypatch.setattr(warm._warm_mint, "_runtime", None)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(4, live)])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {2: _parked_session(4)})
    _mints["linear"] = {
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 4,
        "activation": 2,
        "token": "t4",
    }

    assert warm._warm_mint.generation_is_live(4) is True
    assert await warm.expire_dead_mints() == []
    assert _mints["linear"]["state"] == "waiting"


def test_a_dead_current_runtime_is_still_dead_when_nothing_is_parked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The fall-through must not turn a genuinely dead generation live."""
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(False))
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    assert warm._warm_mint.generation_is_live(4) is False
    monkeypatch.setattr(warm._warm_mint, "_retiring", [(4, _Runtime(False))])
    assert warm._warm_mint.generation_is_live(4) is False


# ── defect: session-handle ownership transfers are not cancellation-safe ──


class _SessionHandle:
    """A backend session. Records that it exists, and whether it was terminated."""

    def __init__(self, created: list[Any]) -> None:
        self.destroyed = False
        created.append(self)

    async def destroy(self) -> None:
        self.destroyed = True

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        return []

    async def drain_init(self, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_an_abandoned_create_still_destroys_the_session_the_backend_made(
    monkeypatch: pytest.MonkeyPatch,
):
    """The backend's session/new has already succeeded when our wait is abandoned. The
    handle is the ONLY way to terminate that session and its loopback callback children, so
    dropping it leaks them until the whole runtime is retired."""
    created: list[Any] = []
    exists = asyncio.Event()

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)  # the backend session now EXISTS
            exists.set()
            await asyncio.sleep(0.2)  # ...and our wait is abandoned during this
            return handle

    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    task = asyncio.get_running_loop().create_task(
        warm._warm_mint._activate_locked("agent", frozenset())
    )
    await exists.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(created) == 1
    assert created[0].destroyed is True, "an abandoned session must still be terminated"
    assert warm._warm_mint._sessions == {}


@pytest.mark.asyncio
async def test_a_cancel_mid_session_destroy_keeps_the_record_for_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    """The sweep popped the record before awaiting the destroy, so a cancellation there lost
    the only reference to a session that is still listening."""

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    record = warm._WarmSession(generation=1, handle=object(), expires_at=0.0, settled=True)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {5: record})

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint.sweep_sessions(set())

    assert warm._warm_mint._sessions == {5: record}, "the only retry record must survive"


@pytest.mark.asyncio
async def test_a_cancel_destroying_an_unstamped_session_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The activation's own failure path has the same shape: it popped the record, so a
    cancellation mid-destroy lost it. Retained instead -- and marked settled and expired, so
    the next sweep is the retry rather than nothing being."""
    created: list[Any] = []

    class _PollExplodes:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return _SessionHandle(created)

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _PollExplodes())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    # The oauth poll raises, which is what sends the activation down its teardown path.
    monkeypatch.setattr(
        _SessionHandle,
        "pop_pending_oauth_requests",
        lambda self: (_ for _ in ()).throw(RuntimeError("frame decode failed")),
    )

    with pytest.raises(asyncio.CancelledError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "an undestroyed session must stay tracked"
    record = next(iter(sessions.values()))
    assert (
        record.settled is True and record.expires_at <= time.monotonic()
    ), "and must be eligible for the sweep, or retaining it just moves the leak"


@pytest.mark.asyncio
async def test_a_cancel_destroying_an_abandoned_session_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The reaped handle enters the SAME ownership rule as every other session: registered
    settled-and-expired before the destroy, so an interrupted destroy is retried by the
    ordinary sweep rather than losing the only reference."""
    created: list[Any] = []
    exists = asyncio.Event()

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)
            exists.set()
            await asyncio.sleep(0.2)
            return handle

    async def _cancels(handle: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(warm, "_destroy_session_quietly", _cancels)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    task = asyncio.get_running_loop().create_task(
        warm._warm_mint._activate_locked("agent", frozenset())
    )
    await exists.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "an undestroyed reaped session must stay tracked"
    record = next(iter(sessions.values()))
    assert record.settled is True and record.expires_at <= time.monotonic()


# ── defect: an unaddressable abandoned session accumulated without bound ──
#
# The no-handle abandon path's acceptance was "reaped when the runtime is retired". But
# retirement is not guaranteed to arrive: any card holding a URL keeps `_shared_mints_pending`
# true, which resets the reaper's idle clock every cycle, while `_ensure_locked`'s
# digest-equality fast path keeps the SAME generation reusable -- so every repetition parked
# another orphan session and its callback children on one live process.


@pytest.mark.asyncio
async def test_an_unaddressable_abandoned_session_quarantines_its_generation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """Quarantining bounds the residual to ONE generation's worth: the next activation finds
    the resident plan unservable, stands the generation down, and the orphan dies with the
    process."""
    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm, "_WARM_SESSION_REAP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    monkeypatch.setattr(warm, "_write_warm_mint_specs", lambda plan: None)
    monkeypatch.setattr(warm, "_unowned_plan_specs", lambda plan: [])

    class _NeverReturnsAHandle:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            # No handle ever becomes reachable to us, so nothing is addressable to destroy.
            await asyncio.Event().wait()

    stale = _NeverReturnsAHandle()
    # The REAL resident plan, so the digest fast path is genuinely reachable -- otherwise the
    # test would respawn for an unrelated reason and prove nothing.
    resident = warm._warm_spec_plan([_provider("linear")])
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])

    with pytest.raises(BaseException):
        await warm._warm_mint._activate_locked("agent", frozenset())

    assert warm._warm_mint._plan is None, "the generation must be quarantined"
    assert warm._warm_mint._digest == ""

    # A subsequent activation must stand the quarantined generation DOWN, not reuse it.
    killed: list[Any] = []

    async def _record_kill(runtime: Any) -> bool:
        killed.append(runtime)
        return True

    class _Spawnable:
        def __init__(self, **kwargs) -> None:
            pass

        async def spawn(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_acp_runtime_factory", lambda: _Spawnable)

    assert await warm._warm_mint._ensure_locked([_provider("linear")]) is not None
    assert killed == [stale], "the process holding the orphan session was reused instead"
    reaper = warm._warm_mint._reaper
    if reaper is not None:
        reaper.cancel()


@pytest.mark.asyncio
async def test_a_recovered_handle_does_not_quarantine_the_generation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The opposite failure mode: the RECOVERED-handle path registers and destroys cleanly,
    so it needs no stand-down. Quarantining there would cost a respawn on every transient
    timeout that the reap already fully repaired."""
    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    created: list[Any] = []

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            handle = _SessionHandle(created)
            await asyncio.sleep(0.05)
            return handle

    resident = warm._warm_spec_plan([_provider("linear")])
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 5)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    with pytest.raises(BaseException):
        await warm._warm_mint._activate_locked("agent", frozenset())

    assert created and created[0].destroyed is True, "the reap must still have worked"
    assert warm._warm_mint._plan is resident, "a fully repaired reap must not force a respawn"
    assert warm._warm_mint._digest == resident.digest


# ── defect: the settle loop never CONSUMED the session queue ──
#
# `pop_pending_oauth_requests()` reads a list that only `drain_init` appends to, and
# `create_session` runs exactly one drain before returning the handle. So `asyncio.sleep`
# between pops moved nothing: a frame arriving after that create-time drain's idle exit was
# unreachable no matter how many rounds elapsed. The budget was never the binding
# constraint -- the loop had no mechanism to absorb a late frame at all.


class _QueuedOauthHandle:
    """A session handle with the real contract: a frame is poppable only after a DRAIN.

    ``available_after_drains`` is how many ``drain_init`` calls must have happened before
    that provider's frame moves onto the poppable list -- 0 models a frame staged during
    ``session/new`` and already drained by ``create_session``.
    """

    def __init__(self, schedule: dict[str, int]) -> None:
        self._schedule = dict(schedule)
        self._poppable: list[dict[str, str]] = []
        self.drains = 0
        self.destroyed = False
        self._promote()

    def _promote(self) -> None:
        for name, due in sorted(self._schedule.items()):
            if due <= self.drains:
                self._poppable.append({"serverName": name, "oauthUrl": f"https://{name}/consent"})
                del self._schedule[name]

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        out, self._poppable = list(self._poppable), []
        return out

    async def drain_init(self, **kwargs) -> None:
        self.drains += 1
        self._promote()

    async def destroy(self) -> None:
        self.destroyed = True


def _queued_runtime(handle: Any) -> Any:
    class _Runtime:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return handle

    return _Runtime()


@pytest.mark.asyncio
async def test_a_frame_arriving_after_the_create_drain_is_still_absorbed(
    monkeypatch: pytest.MonkeyPatch,
):
    """`linear`'s frame was staged during session/new; `vercel`'s lands three windows later.
    Sleeping past it consumed nothing, so it was never poppable."""
    handle = _QueuedOauthHandle({"linear": 0, "vercel": 3})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    activation, requests = await warm._warm_mint._activate_locked(
        "agent", frozenset({"linear", "vercel"})
    )

    names = sorted(str(request["serverName"]) for request in requests)
    assert names == ["linear", "vercel"], "a late frame must be consumed, not slept past"
    assert activation in warm._warm_mint._sessions
    assert handle.destroyed is False


@pytest.mark.asyncio
async def test_each_new_frame_renews_the_quiet_budget_past_the_old_fixed_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    handle = _QueuedOauthHandle({"linear": 5, "vercel": 10})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", frozenset({"linear", "vercel"}))

    assert sorted(str(request["serverName"]) for request in requests) == ["linear", "vercel"]
    assert handle.drains == 10


@pytest.mark.asyncio
async def test_progress_is_still_bounded_by_the_expected_roster(
    monkeypatch: pytest.MonkeyPatch,
):
    wanted = frozenset({"linear", "vercel", "missing"})
    handle = _QueuedOauthHandle({"linear": 6, "vercel": 12})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", wanted)

    assert sorted(str(request["serverName"]) for request in requests) == ["linear", "vercel"]
    assert handle.drains == len(wanted) * warm._WARM_OAUTH_SETTLE_ROUNDS


@pytest.mark.asyncio
async def test_the_settle_loop_stops_as_soon_as_every_wanted_frame_is_in(
    monkeypatch: pytest.MonkeyPatch,
):
    """Consuming must not cost latency when the frames are already staged: the loop still
    short-circuits on the first pop and opens no drain window at all."""
    handle = _QueuedOauthHandle({"linear": 0})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    _, requests = await warm._warm_mint._activate_locked("agent", frozenset({"linear"}))

    assert [str(r["serverName"]) for r in requests] == ["linear"]
    assert handle.drains == 0, "an already-satisfied activation must open no drain window"


@pytest.mark.asyncio
async def test_the_settle_loop_consumes_on_every_round_it_waits(
    monkeypatch: pytest.MonkeyPatch,
):
    """The budget is only meaningful if every waiting round is a CONSUMING round -- one
    bare sleep anywhere in the loop is a window a frame can land in unseen."""
    handle = _QueuedOauthHandle({"linear": 0, "never": 99})
    monkeypatch.setattr(warm._warm_mint, "_runtime", _queued_runtime(handle))
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    await warm._warm_mint._activate_locked("agent", frozenset({"linear", "never"}))

    assert (
        handle.drains == warm._WARM_OAUTH_SETTLE_ROUNDS
    ), "every quiet round must consume before the inactivity budget is exhausted"


@pytest.mark.asyncio
async def test_a_frame_past_the_whole_budget_releases_its_claim_cleanly(
    monkeypatch: pytest.MonkeyPatch, _stub_activation
):
    """Beyond the budget the cold path is the correct answer, so the claim must be RELEASED
    -- no row left minting, and the token fence still refuses a late absorb."""
    provider = _provider("slowpoke")
    claims = await _claim("slowpoke")
    token = claims["slowpoke"]

    # The activation produced no frame for it at all.
    empty = _result([provider], [])
    assert await warm._absorb_warm_requests(empty, claims) == []
    assert "slowpoke" not in _mints, "an unfulfilled claim must not sit in the table"

    # A later activation re-claims the slug; the first claim's token must not resurrect it.
    fresh = await _claim("slowpoke")
    assert fresh["slowpoke"] != token
    late = _result([provider], [{"serverName": "slowpoke", "oauthUrl": "https://s/consent"}])
    assert await warm._absorb_warm_requests(late, claims) == []
    assert _mints["slowpoke"]["state"] == "minting"
    assert _mints["slowpoke"]["token"] == fresh["slowpoke"]


# ── defect: reuse activated the RESIDENT plan, which still names excluded providers ──
#
# Servability deliberately reuses a process whose spec set is a SUPERSET of what the current
# scan wants, so a shrink does not strand a peer's listeners. But the bulk activation names
# the resident ALL-AGENT mode, and a mode's mounted server set is fixed by the spec it
# carried at spawn -- specs are enumerated ONCE, and the wanted subset cannot travel through
# `session/new` because injected servers kill the process. So `set_mode` initializes the
# excluded provider's MCP server and an authorization request goes out for exactly the
# provider `_warm_mintable_entry` vetoed. Filtering the activation's RESULT leaves that
# request made, which is why these tests assert what the process MOUNTS, not what the mint
# returns.


def _divergent_config(agents: Path, alias: str) -> None:
    """The user's own agent spec, asking for a DIFFERENT endpoint than the registry does."""
    (agents / AGENT_FILENAME).write_text(
        json.dumps({"mcpServers": {alias: {"url": "https://elsewhere.example/mcp"}}}),
        encoding="utf-8",
    )


class _SpecEnumeratingRuntime:
    """A process that mounts what kiro-cli mounts: the specs on disk AT SPAWN, by name.

    kiro-cli enumerates the agents directory once at spawn and ``set_mode`` then activates one
    of THOSE modes, so the servers an activation initializes are the ones the named spec listed
    when the process started -- not whatever the file says later. Modelling that is the whole
    point: a fake that re-read the spec at ``create_session`` time would make the defect these
    tests exist for invisible.
    """

    def __init__(self, agents_dir: Path, mounted: list[tuple[str, frozenset[str]]], **kwargs: Any):
        self._agents_dir = agents_dir
        self._mounted = mounted
        self._modes: dict[str, frozenset[str]] = {}

    async def spawn(self) -> None:
        for path in self._agents_dir.glob("*.json"):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self._modes[path.stem] = frozenset((body.get("mcpServers") or {}))

    def is_alive(self) -> bool:
        return True

    async def create_session(self, *, agent: str, mcp_servers: list[Any]) -> Any:
        assert mcp_servers == [], "a warm session must inject no servers"
        # What `set_mode` would initialize: the roster this mode carried at spawn.
        self._mounted.append((agent, self._modes.get(agent, frozenset())))
        return _SessionHandle([])


@pytest.fixture
def _mount_spy(monkeypatch: pytest.MonkeyPatch, _agents_dir: Path):
    """Every activation's ``(mode, mounted servers)``, observed at the spawn/set_mode seam."""
    mounted: list[tuple[str, frozenset[str]]] = []

    def _factory():
        def _build(**kwargs: Any) -> _SpecEnumeratingRuntime:
            return _SpecEnumeratingRuntime(_agents_dir, mounted, **kwargs)

        return _build

    monkeypatch.setattr(warm, "_acp_runtime_factory", _factory)
    monkeypatch.setattr(warm, "_MINT_GRANT_POLL_SECONDS", 3600)
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    monkeypatch.setattr(warm, "_remove_warm_mint_specs", lambda: None)
    return mounted


async def _spawned_on(agents_dir: Path, plan: warm._WarmSpecPlan, mounted: list[Any]) -> Any:
    """A live process that enumerated ``plan``'s specs, the way a resident one did."""
    warm._write_warm_mint_specs(plan)
    runtime = _SpecEnumeratingRuntime(agents_dir, mounted)
    await runtime.spawn()
    return runtime


@pytest.mark.asyncio
async def test_an_excluded_provider_is_never_mounted_by_a_bulk_activation(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path, _mount_spy: list[Any]
):
    """THE test: assert what the activation INITIALIZES, not what the mint returns.

    RED before the fix -- the resident all-agent mode was reused, so `notion`'s MCP server
    was still mounted and challenged for even though its URL was discarded."""
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    assert set(resident.entries) == {"linear", "notion"}
    stale = await _spawned_on(_agents_dir, resident, _mount_spy)
    _mount_spy.clear()

    # notion's configured entry now diverges, which vetoes it in every fresh plan.
    _divergent_config(_agents_dir, "notion")

    async def _kills(runtime: Any) -> bool:
        return True

    monkeypatch.setattr(warm, "_kill_quietly", _kills)
    monkeypatch.setattr(warm, "_warm_candidate_scan", lambda: ([linear, notion], [linear, notion]))
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    result = await warm._warm_mint.mint_for()

    assert result is not None
    assert len(_mount_spy) == 1, "exactly one activation"
    mode, servers = _mount_spy[0]
    assert mode == warm._WARM_ALL_AGENT
    assert servers == frozenset({"linear"}), "the vetoed provider's server was initialized"
    assert [provider["slug"] for provider in result.providers] == ["linear"]


@pytest.mark.asyncio
async def test_a_shrink_parks_a_process_a_card_still_needs_rather_than_killing_it(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path, _mount_spy: list[Any]
):
    """The other half of the constraint: re-mounting an exact roster must not strand a peer.

    A process holding a redeemable code is PARKED, keeps its generation live, and the drain
    retires it once its rows are gone -- so the excluded provider stops being mounted without
    any card losing its URL."""
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    stale = await _spawned_on(_agents_dir, resident, _mount_spy)
    _mount_spy.clear()
    _divergent_config(_agents_dir, "notion")

    killed: list[Any] = []

    async def _record_kill(runtime: Any) -> bool:
        killed.append(runtime)
        return True

    # A peer is mid-consent on the resident generation, so killing it would strand its URL.
    _mints["vercel"] = {"state": "waiting", "shared": True, "generation": 4, "token": "t"}

    monkeypatch.setattr(warm, "_kill_quietly", _record_kill)
    monkeypatch.setattr(warm, "_warm_candidate_scan", lambda: ([linear, notion], [linear, notion]))
    monkeypatch.setattr(warm._warm_mint, "_runtime", stale)
    monkeypatch.setattr(warm._warm_mint, "_generation", 4)
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)
    monkeypatch.setattr(warm._warm_mint, "_retiring", [])
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    assert await warm._warm_mint.mint_for() is not None

    assert killed == [], "a process a card is still mid-consent on must be parked, not killed"
    assert [generation for generation, _ in warm._warm_mint._retiring] == [4]
    assert warm._warm_mint.generation_is_live(4) is True, "the parked URL must stay redeemable"
    assert _mount_spy[0][1] == frozenset({"linear"})


@pytest.mark.asyncio
async def test_a_roster_that_only_changed_ORDER_reuses_the_process_instead_of_parking_it(
    monkeypatch: pytest.MonkeyPatch, _agents_dir: Path
):
    """The reuse branch's surviving case, now that the slug-scoped modes are gone.

    The digest is order-sensitive (a spec's ``tools`` is a LIST built in scan order), so a
    registry that hands the same providers back in a different order produces a different
    digest for an IDENTICAL roster. Only the two predicates catch that: servability says the
    resident process can answer for everything wanted, and
    ``_resident_roster_is_asked_for`` says it mounts nothing extra. Without them a bare
    reorder would park a live process and respawn for no gain.

    This is also the guard on the tempting simplification: collapsing the pair into the
    digest comparison alone.
    """
    linear, notion = _provider("linear"), _provider("notion")
    resident = warm._warm_spec_plan([linear, notion])
    reordered = warm._warm_spec_plan([notion, linear])
    assert resident.entries == reordered.entries, "same roster"
    assert resident.digest != reordered.digest, "a reorder must not be caught by the digest"

    def _never_respawn():
        raise AssertionError("an identical roster in a new order must reuse the process")

    monkeypatch.setattr(warm, "_acp_runtime_factory", _never_respawn)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _Runtime(True))
    monkeypatch.setattr(warm._warm_mint, "_plan", resident)
    monkeypatch.setattr(warm._warm_mint, "_digest", resident.digest)

    served = await warm._warm_mint._ensure_locked([notion, linear])

    assert served is not None
    assert set(served.entries) == {"linear", "notion"}
    # The enumerated superset is what the next scan is judged against, so it must survive
    # a reuse untouched.
    assert warm._warm_mint._plan is resident


# ── a session destroy that TIMES OUT is not a destroy ──
#
# `_destroy_session_quietly` swallowed every `Exception` and returned None, so a destroy that
# timed out was indistinguishable from one that worked. The record each caller then dropped
# is the ONLY reference to the handle, so a session that is still listening -- and the
# loopback callback children it owns -- became unaddressable, with nothing that could retry.
# `asyncio.TimeoutError` IS an `Exception`, so the retention every call site already had --
# each written for a `CancelledError`, which is not -- never covered the case. The same class
# as `_kill_quietly` above, one layer down.


class _UndestroyableHandle:
    """A session whose destroy fails the way a timeout does: a plain ``Exception``.

    Fails ``failures`` times and then succeeds, so one test pins both halves of the
    contract -- retained while the destroy does not take, forgotten once it does.
    """

    def __init__(self, failures: int = 1) -> None:
        self._failures = failures
        self.destroy_attempts = 0

    async def destroy(self) -> None:
        self.destroy_attempts += 1
        if self.destroy_attempts <= self._failures:
            raise TimeoutError("session destroy timed out")

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        return []


@pytest.mark.asyncio
async def test_a_session_destroy_that_times_out_reports_false():
    """The verdict itself: without it no caller can tell the two outcomes apart."""
    handle = _UndestroyableHandle(failures=1)

    assert await warm._destroy_session_quietly(handle) is False
    assert await warm._destroy_session_quietly(handle) is True


@pytest.mark.asyncio
async def test_a_session_sweep_whose_destroy_times_out_keeps_the_record(
    monkeypatch: pytest.MonkeyPatch,
):
    """RED before the fix: the record was popped and the live session forgotten."""
    handle = _UndestroyableHandle(failures=1)
    record = warm._WarmSession(generation=1, handle=handle, expires_at=0.0, settled=True)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {5: record})

    await warm._warm_mint.sweep_sessions(set())

    assert handle.destroy_attempts == 1
    assert warm._warm_mint._sessions == {5: record}, "the last reference left must survive"

    # The next sweep is the retry -- and only then is the record forgotten.
    await warm._warm_mint.sweep_sessions(set())

    assert handle.destroy_attempts == 2
    assert warm._warm_mint._sessions == {}


@pytest.mark.asyncio
async def test_an_activation_teardown_whose_destroy_times_out_leaves_it_sweepable(
    monkeypatch: pytest.MonkeyPatch,
):
    """The activation's own failure path popped unconditionally, so the sweep it marks the
    record for could never run."""
    handle = _UndestroyableHandle(failures=99)

    class _PollExplodes:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            return handle

    monkeypatch.setattr(warm._warm_mint, "_runtime", _PollExplodes())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})
    monkeypatch.setattr(warm, "_WARM_OAUTH_SETTLE_ROUNDS", 1)
    # The oauth poll raises, which is what sends the activation down its teardown path.
    monkeypatch.setattr(
        _UndestroyableHandle,
        "pop_pending_oauth_requests",
        lambda self: (_ for _ in ()).throw(RuntimeError("frame decode failed")),
    )

    with pytest.raises(RuntimeError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "a session that may still be listening must stay tracked"
    record = next(iter(sessions.values()))
    assert record.handle is handle
    assert (
        record.settled is True and record.expires_at <= time.monotonic()
    ), "and must be eligible for the sweep, or retaining it just moves the leak"


@pytest.mark.asyncio
async def test_a_reaped_session_whose_destroy_times_out_stays_tracked(
    monkeypatch: pytest.MonkeyPatch,
):
    """The recovered-handle path registers the reaped session settled-and-expired for exactly
    this reason, so a destroy that does not take must leave the record for the sweep."""
    handle = _UndestroyableHandle(failures=99)

    class _SlowToReturn:
        def is_alive(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            await asyncio.sleep(0.05)
            return handle

    monkeypatch.setattr(warm, "_WARM_SESSION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(warm._warm_mint, "_runtime", _SlowToReturn())
    monkeypatch.setattr(warm._warm_mint, "_generation", 3)
    monkeypatch.setattr(warm._warm_mint, "_sessions", {})

    with pytest.raises(asyncio.TimeoutError):
        await warm._warm_mint._activate_locked("agent", frozenset())

    sessions = warm._warm_mint._sessions
    assert len(sessions) == 1, "the reaped session may still be listening; keep it tracked"
    record = next(iter(sessions.values()))
    assert record.handle is handle
    assert record.settled is True and record.expires_at <= time.monotonic()


# ── an unreadable grant cache is not an absent grant ──
#
# `grant_present` is tri-state and `None` means the cache could not be read at all, so
# `not grant_present(...)` selected an indeterminate provider for a warm mint: consent was
# initiated on an absence nobody confirmed, and the card was flipped to waiting behind an
# approval URL for a provider that may already be connected. L1 collapses the same third
# answer on purpose because it never initiates consent; this path does.


def test_an_indeterminate_grant_is_not_read_as_an_absent_one(monkeypatch: pytest.MonkeyPatch):
    """RED before the fix: `not None` is True, so `unreadable` was warmed."""
    universe = [_provider("fresh"), _provider("unreadable"), _provider("granted")]

    def _presence(url: str) -> bool | None:
        if "unreadable" in url:
            return None
        return "granted" in url

    monkeypatch.setattr(warm, "grant_present", _presence)

    assert [p["slug"] for p in warm._warm_activation_candidates(universe)] == ["fresh"]


def test_an_indeterminate_provider_is_named_and_read_exactly_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Skipped is not dropped: the provider is named, so its absence from the batch is
    accountable. Read ONCE -- a re-stat to classify the skip is the two-pass race
    `grant_presence` refuses, where a failure clearing between passes reads as absence."""
    reads: list[str] = []

    def _unreadable(url: str) -> bool | None:
        reads.append(url)
        return None

    monkeypatch.setattr(warm, "grant_present", _unreadable)

    with caplog.at_level(logging.WARNING, logger=warm.logger.name):
        assert warm._warm_activation_candidates([_provider("notion")]) == []

    assert len(reads) == 1, "presence was read more than once per provider"
    assert "notion" in caplog.text

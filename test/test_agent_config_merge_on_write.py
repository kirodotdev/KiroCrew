"""Merge-on-write semantics for the agent-config PUT (#6664).

``PUT /api/agent/config`` persists a whole-file snapshot the client read earlier,
and ``apps/bridges.py::_register_mcp_servers`` writes app MCP bridges into that
same file under its own flock. A registration landing between the client's read
and its PUT was therefore silently clobbered.

The rule under test: **preservation requires positive evidence of app or host
ownership.** An on-disk ``mcpServers`` entry absent from the submission is kept
only when its owner can be named -- a host-managed server, or one of the exact
``<app>:<server>`` names an installed, ENABLED app's manifest declares; every
other absent entry is the client's and is deleted. The ambiguity that makes the
policy load-bearing is that a snapshot taken before an app registered its bridge
is byte-identical to one where the user deliberately removed that entry, so
ownership is the only disambiguator -- and an unclassifiable entry falls to
DELETE, because preserving without evidence makes a directly-added entry
permanently undeletable, while an ownership source that cannot be READ refuses
the PUT rather than guessing in either direction.
"""

from __future__ import annotations

import contextlib
import errno
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from conftest import requires_symlinks
from kiro_crew.dashboard.handlers import api_agent_config


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: the owner boundary on this endpoint has its
    own enumerate-the-invariant coverage in test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@contextlib.asynccontextmanager
async def _neutral_lock():
    """Stand in for the transaction lock without touching the host's mcp.lock."""
    yield


async def _put(
    tmp_path: Path,
    *,
    on_disk: dict | str,
    submitted: dict,
    scope_names: frozenset[str] = frozenset(),
    unreadable_scope: bool = False,
    apps: dict[str, tuple[str, ...]] | None = None,
    apps_unreadable: bool = False,
    installed_corrupt: bool = False,
    installed_absent: bool = False,
    enabled: bool | None = True,
    installed_shape: str | None = None,
    apps_root_is_file: bool = False,
    apps_dir_unreadable: bool = False,
    apps_child_unstattable: bool = False,
    managed: tuple[str, ...] = (),
    managed_specs: dict[str, dict] | None = None,
    extra_managed: dict[str, dict] | None = None,
    lock=_neutral_lock,
    file_lock=None,
    govern: bool = False,
) -> tuple[web.Response, dict]:
    """PUT *submitted* over an installed spec holding *on_disk*.

    ``on_disk`` as a ``str`` is written verbatim, for the corrupt-spec case.
    ``scope_names`` is what a readable mcp.json scope declares and
    ``unreadable_scope`` reports the scope as unreadable instead; both are
    injected at the census seam and BOTH must leave every verdict unchanged --
    proven ownership decides, so re-introducing any scope-based exclusion ahead of
    the ownership test trips the tests that set them. ``apps`` maps an INSTALLED
    app name to the server names its manifest DECLARES, so ``{"demo": ("notes",)}``
    makes ``demo:notes`` app-owned while ``demo:custom`` is not; ``apps_unreadable``
    makes one manifest unreadable. ``managed`` are host-managed server names,
    carrying no ``opt_in``/``spec_gate`` (the always-emitted shape);
    ``managed_specs`` adds host-managed names with their REAL spec dicts, which is
    how the opt-in and gated rows are exercised. ``extra_managed`` is what the
    edition's ``_extra_mcp_servers`` contributes.
    ``file_lock`` replaces bridges' flock, which is how the deregistration
    interleaving is injected. ``govern=True`` leaves the real governance filter
    in place instead of neutralizing it.
    """
    if apps is None:
        apps = {"demo": ("notes",)}
    installed = tmp_path / "kirocrew.json"
    installed.write_text(
        on_disk if isinstance(on_disk, str) else json.dumps(on_disk), encoding="utf-8"
    )
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": submitted}

    request.json = mock_json

    # Injected at spec_census, the seam any scope-derived rule would have to read
    # through, so a re-introduced exclusion is caught wherever it is spelled.
    def _census(project_dirs=()):
        if unreadable_scope:
            # The scope exists but could not be read: it declares nothing and is
            # reported unreadable, so no name is proven client-declared.
            return ({"kirocrew": {}}, ("kirocrew",))
        return ({"kirocrew": {name: {"url": "scope"} for name in scope_names}}, ())

    if file_lock is None:

        @contextlib.contextmanager
        def file_lock(*, exclusive: bool = True, target=None):  # noqa: ANN001
            yield

    # A real installed-apps tree, so enumeration walks directories the way the
    # production helper does rather than consuming a pre-baked name list, and so
    # the absent-vs-corrupt discrimination reads real files.
    _apps_root = tmp_path / "apps"
    if apps_root_is_file:
        # The apps root PRESENT but not a directory: is_dir() answers False for
        # this exactly as it does for genuine absence.
        _apps_root.write_text("not a directory", encoding="utf-8")
    else:
        _apps_root.mkdir(exist_ok=True)
    for _app in apps:
        if apps_root_is_file:
            break
        _app_root = _apps_root / _app
        _app_root.mkdir(exist_ok=True)
        if installed_absent:
            continue  # a directory under apps/ carrying no installed.json
        _meta = _app_root / "installed.json"
        if installed_shape == "broken_symlink":
            # A DANGLING link: lstat succeeds (the link exists), stat raises.
            _meta.symlink_to(_app_root / "no-such-target.json")
            continue
        if installed_shape == "directory":
            _meta.mkdir()
            continue
        if installed_corrupt:
            _body = "{ not json at all"
        else:
            _record: dict[str, object] = {"name": _app}
            # Omitted entirely when ``enabled`` is None, so the REAL
            # ``InstalledApp.from_dict`` default is what decides -- the absent-field
            # row of the table is exercised rather than stubbed.
            if enabled is not None:
                _record["enabled"] = enabled
            _body = json.dumps(_record)
        _meta.write_text(_body, encoding="utf-8")

    def _reg_source(app_name):  # noqa: ANN001 -- mirrors bridges._registration_source
        root = _apps_root / app_name
        if apps_unreadable:
            # What bridges returns for a manifest it could not parse.
            return None, root
        return SimpleNamespace(mcpServers={s: {"command": s} for s in apps[app_name]}), root

    _handler_apps_root: object = _apps_root
    if apps_dir_unreadable:
        # A REAL directory that stats as one but refuses enumeration. It has to be
        # a genuine path, not a mock: the shape screen calls ``os.lstat``/``os.stat``
        # on it, which a MagicMock cannot satisfy. Subclassing keeps every other
        # path operation real and fails only the listing, so no chmod has to be
        # restored before ``tmp_path`` cleanup.
        class _UnlistableDir(type(_apps_root)):  # type: ignore[misc]
            def iterdir(self):
                raise OSError("permission denied")

        _handler_apps_root = _UnlistableDir(_apps_root)
    if apps_child_unstattable:
        # A child the listing RETURNS but whose stat faults. ``ELOOP`` (a symlink
        # loop) is the decisive errno rather than an arbitrary one: pathlib's
        # ``_ignore_error`` swallows exactly ENOENT/ENOTDIR/EBADF/ELOOP, so those
        # are the faults ``Path.is_dir()`` turns into a silent False -- an EACCES
        # happens to propagate already, which is why it would not reproduce the
        # defect. Subclassed rather than a real loop so the row runs on every
        # platform (a real symlink needs privilege on Windows), and rather than a
        # mock because the shape screen stats sibling paths in the same tree.
        class _UnstattableChild(type(_apps_root)):  # type: ignore[misc]
            def stat(self, *, follow_symlinks: bool = True):
                raise OSError(errno.ELOOP, "symbolic link loop")

        class _RootWithUnstattableChild(type(_apps_root)):  # type: ignore[misc]
            def iterdir(self):
                for child in super().iterdir():
                    yield _UnstattableChild(child)

        _handler_apps_root = _RootWithUnstattableChild(_apps_root)

    _managed_map: dict[str, dict] = {n: {} for n in managed}
    if managed_specs:
        _managed_map.update(managed_specs)

    patches = [
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": [], "allowedTools": []},
        ),
        # Patched at the AGENTS binding, not at bridges: the handler imports
        # ``_mcp_lock as _agent_file_lock`` at module scope, so a patch on the
        # source module would not reach the already-bound name.
        patch("kiro_crew.dashboard.handlers.agents._agent_file_lock", file_lock),
        # ``apps_dir`` is patched in BOTH modules on purpose: the handler binds it
        # at module scope, while the real ``app_enabled_state`` resolves the
        # metadata path through manager's own copy.
        patch(
            "kiro_crew.dashboard.handlers.agents.apps_dir",
            return_value=_handler_apps_root,
        ),
        patch("kiro_crew.apps.manager.apps_dir", return_value=_apps_root),
        patch("kiro_crew.dashboard.handlers.agents._registration_source", _reg_source),
        patch("kiro_crew.agent._MANAGED_MCP_SERVERS", _managed_map),
        patch("kiro_crew.agent._extra_mcp_servers", return_value=dict(extra_managed or {})),
        patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock", lock),
        patch("kiro_crew.connections.ownership.spec_census", _census),
    ]
    if not govern:
        patches.append(
            patch(
                "kiro_crew.platform.governance.sanitize_agent_config_governance",
                lambda config: None,
            )
        )

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        response = await api_agent_config(request)

    written = json.loads(installed.read_text(encoding="utf-8"))
    return response, written


# ── (a) the concurrent register-vs-PUT race from the original finding ──────────


@pytest.mark.asyncio
async def test_app_bridge_registered_during_the_lock_wait_survives_the_put(tmp_path):
    """The race the GPT round-6 finding reported, closed.

    The transaction lock is a cross-process flock whose wait is unbounded, so an
    app enable queued ahead of this PUT commits its ``mcpServers`` entry strictly
    after the client's read and strictly before this PUT's write. Pre-fix the
    snapshot was persisted as read and the bridge was gone, with no error and no
    log line — the app's tools simply stopped resolving.

    Modelled deterministically rather than by timing: the fake lock registers the
    bridge as it is acquired, which is exactly that window.
    """
    installed = tmp_path / "kirocrew.json"

    @contextlib.asynccontextmanager
    async def _registering_lock():
        # An app enable lands here: whole-file RMW of the installed spec, the
        # shape apps/bridges.py::_register_mcp_servers performs under its flock.
        data = json.loads(installed.read_text(encoding="utf-8"))
        data.setdefault("mcpServers", {})["demo:notes"] = {"command": "notes-mcp"}
        installed.write_text(json.dumps(data), encoding="utf-8")
        yield

    response, written = await _put(
        tmp_path,
        # The client read this — no app bridge in it yet.
        on_disk={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
        submitted={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
        scope_names=frozenset({"weather"}),
        lock=_registering_lock,
    )

    assert response.status == 200
    assert written["mcpServers"]["demo:notes"] == {"command": "notes-mcp"}, (
        "an app bridge registered between the client's read and this PUT was "
        "clobbered by the snapshot: the merge did not re-read on-disk state "
        "inside the locked commit unit"
    )
    # The client's own entry still lands.
    assert written["mcpServers"]["weather"] == {"url": "w"}


@pytest.mark.asyncio
async def test_app_deregistered_during_the_put_is_not_resurrected(tmp_path):
    """FINDING 2: a bridge removed while the PUT runs must stay removed.

    App disable / uninstall / health demotion calls ``_deregister_mcp_servers``,
    which read-modify-writes the installed spec under bridges' OWN flock. Pre-fix
    the merge read ran BEFORE that flock was taken, so a deregistration landing
    between the read and the final locked write had its entry re-inserted -- and
    unlike the registration direction this does NOT self-heal, because
    ``reconcile_enabled_app_resources`` only re-registers ENABLED apps and skips
    the disabled or uninstalled one whose bridge just came back.

    Deterministic interleaving, no sleeps: the fake flock performs the
    deregistration at ACQUISITION. Pre-fix the merge has already read the file by
    then and the entry is preserved; with the read moved inside the same lock
    hold, the read happens after and sees the entry gone.
    """
    installed = tmp_path / "kirocrew.json"

    @contextlib.contextmanager
    def _deregistering_lock(*, exclusive: bool = True, target=None):  # noqa: ANN001
        # An app disable commits here: the same whole-file RMW
        # apps/bridges.py::_deregister_mcp_servers performs under this flock.
        data = json.loads(installed.read_text(encoding="utf-8"))
        data.get("mcpServers", {}).pop("demo:notes", None)
        installed.write_text(json.dumps(data), encoding="utf-8")
        yield

    response, written = await _put(
        tmp_path,
        # The client read this while the app was still enabled.
        on_disk={
            "name": "kirocrew",
            "mcpServers": {"demo:notes": {"command": "notes-mcp"}, "weather": {"url": "w"}},
        },
        submitted={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
        scope_names=frozenset({"weather"}),
        file_lock=_deregistering_lock,
    )

    assert response.status == 200
    assert "demo:notes" not in written["mcpServers"], (
        "the PUT resurrected a bridge that was deregistered while it ran: the "
        "merge read must happen inside the same bridge-file lock hold as the "
        "spec write, not before it"
    )
    assert written["mcpServers"]["weather"] == {"url": "w"}


# ── (b) the ownership grid ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_client_entry_deletes_on_a_sequential_add_then_remove(tmp_path):
    """FINDING 1: a server the user typed into the raw editor must stay deletable.

    The ordinary lifecycle, with no race at all: the user adds ``myserver``
    through this same editor (so it exists ONLY in the installed spec -- no
    mcp.json scope declares it), then in a later, race-free PUT removes it.

    Pre-fix the preserve test was "no scope declares it", and the installed spec
    is not a scope, so this entry was re-inserted on every deletion attempt --
    permanently undeletable, breaking #6664's own requirement 2 for the most
    ordinary kind of client entry. Preservation now requires POSITIVE evidence of
    app or host ownership, which a direct entry has none of.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"myserver": {"command": "mine"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset(),
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a direct client-created entry was re-inserted on deletion: preservation "
        "must require positive evidence of app/host ownership, not merely the "
        "absence of a scope declaration"
    )


@pytest.mark.asyncio
async def test_app_owned_entry_absent_from_the_snapshot_is_preserved(tmp_path):
    """No scope declares an app bridge, so its absence carries no instruction."""
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset(),
    )

    assert response.status == 200
    assert written["mcpServers"] == {"demo:notes": {"command": "notes-mcp"}}, (
        "an app-owned entry absent from the submission was deleted; absence of an "
        "entry the client never spoke for is not a deletion instruction"
    )


@pytest.mark.asyncio
async def test_client_owned_entry_absent_from_the_snapshot_is_deleted(tmp_path):
    """A scope-declared entry is the client's own, so its absence IS a deletion.

    The other half of the rule, and the one an over-broad fix breaks: preserving
    every absent entry would make the editor unable to remove anything.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset({"weather"}),
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a client-owned entry the user removed was resurrected: merge-on-write "
        "must preserve only entries the client does not own"
    )


@pytest.mark.asyncio
async def test_entry_of_unknown_ownership_is_deleted(tmp_path):
    """THE PINNED UNKNOWN-KEY POLICY: unclassifiable ⇒ client-owned ⇒ delete.

    This is the INVERSE of the policy the first cut shipped, and the inversion is
    the point. Preservation now requires positive evidence of app or host
    ownership, so an entry with no such evidence -- including one whose census
    source could not be read -- is treated as the client's and deleted when
    absent from the submission.

    The direction matters because the two errors are not the ones the first cut
    weighed. Preserving without evidence does not merely risk keeping something
    the user wanted gone: for a direct entry it makes deletion IMPOSSIBLE, since
    every retry re-reads the same unowned-looking entry and re-inserts it. An
    app bridge, by contrast, is positively identifiable, so requiring evidence
    costs it nothing.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        unreadable_scope=True,
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "an entry with no evidence of app or host ownership was preserved; that "
        "is what made direct client entries permanently undeletable"
    )


@pytest.mark.asyncio
async def test_a_scope_declaration_does_not_defeat_proven_ownership(tmp_path):
    """ROUND 6: a scope declaration must not delete a bridge the app really owns.

    The scope census used to subtract every declared name from the candidates
    BEFORE ownership was tested, as a precedence rule inherited from the
    prefix-matching era. Once ownership became the EXACT set of manifest-declared
    names, that subtraction could only ever remove a name that IS provably owned:
    app ``demo`` genuinely registers ``demo:notes``, so a user who also declares
    ``demo:notes`` in their own mcp.json made every stale PUT delete the live
    bridge -- the exact clobber #6664 exists to prevent, reachable through a
    collision the user cannot see.

    The census seam stays injected here on purpose: re-introducing any
    scope-based exclusion ahead of the ownership test trips this test.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset({"demo:notes"}),
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"demo:notes": {"command": "notes-mcp"}}, (
        "a scope declaration deleted a bridge an installed, enabled app declares "
        "by exact name; positive ownership outranks the declaration"
    )


@pytest.mark.asyncio
async def test_a_scope_declared_name_with_no_proven_owner_is_deleted(tmp_path):
    """The other half of the row: a declaration adds nothing to an unowned name.

    ``demo:custom`` is squatting an installed app's namespace and the manifest
    never declares it, so it has no proven owner and is deleted -- with or
    without the user's own scope declaration. Pins that letting proven ownership
    win did not turn a scope declaration into a preserve reason.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:custom": {"command": "mine"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset({"demo:custom"}),
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a scope-declared name with no app or host owner was preserved; only "
        "positive ownership preserves anything"
    )


@pytest.mark.asyncio
async def test_a_namespaced_entry_of_an_uninstalled_app_is_deleted(tmp_path):
    """``foo:bar`` is only a bridge when ``foo`` is an INSTALLED app.

    Otherwise the namespace test would let any hand-typed colon name become
    undeletable -- the same defect as Finding 1, reachable through a different
    spelling.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"nosuchapp:thing": {"command": "x"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}


@pytest.mark.asyncio
async def test_host_managed_entry_is_preserved(tmp_path):
    """An ALWAYS-EMITTED host-managed server is positively owned, so it survives.

    Not a new restriction, and the "always-emitted" qualifier is the whole of the
    justification: a fresh rebuild re-adds ``kirocrew-core`` unconditionally, so
    removing it through this editor never stuck. The two managed entries that are
    NOT always emitted are covered by the three rows below, where preservation
    would instead resurrect something the rebuild would leave out.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"kirocrew-core": {"command": "c"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        managed=("kirocrew-core",),
    )

    assert response.status == 200
    assert written["mcpServers"] == {"kirocrew-core": {"command": "c"}}


@pytest.mark.asyncio
async def test_an_opt_in_managed_server_omitted_from_the_snapshot_is_deleted(tmp_path):
    """ROUND 7: an opt-in grant must be REVOCABLE through this editor.

    ``kirocrew-dashboard`` carries ``opt_in``, which makes it an assignable set
    rather than an always-on capability: ``build_agent_config`` never emits it and
    ``_refresh_dynamic_fields`` keeps an EXISTING entry current without ever
    re-introducing one. So the preserve rationale on the row above -- "the rebuild
    re-adds it anyway, removing it here never stuck" -- is simply false for this
    entry: nothing re-adds it. Preserving it made the grant UNDELETABLE through the
    only surface that can revoke it, because each retry re-read the entry it had
    just refused to drop.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"kirocrew-dashboard": {"command": "d"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        managed_specs={"kirocrew-dashboard": {"opt_in": True}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "an opt-in managed server was preserved: the grant cannot be revoked "
        "through this editor, and no rebuild re-adds it"
    )


@pytest.mark.asyncio
async def test_a_gate_closed_managed_server_omitted_from_the_snapshot_is_deleted(tmp_path):
    """ROUND 7: a CLOSED ``spec_gate`` means the rebuild would not re-add it.

    ``kirocrew-computer``'s gate is consulted at emission time, and both spec
    writers ``pop`` the entry while it is closed -- emitting it is what makes
    kiro-cli spawn a backend for a capability that is off or has no driver on this
    OS. Preserving such an entry here RESURRECTS exactly what the gate exists to
    withhold, and the next rebuild would remove it again: the two surfaces
    disagreed about the same config.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"kirocrew-computer": {"command": "u"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        managed_specs={"kirocrew-computer": {"spec_gate": lambda: False}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a gate-closed managed server was preserved: the merge resurrected an "
        "entry the rebuild withholds"
    )


@pytest.mark.asyncio
async def test_a_gate_open_managed_server_is_still_preserved(tmp_path):
    """The overshoot guard for the two rows above: an OPEN gate still preserves.

    Narrowing the host set to emission-eligible entries must not stop protecting
    the ones a rebuild really does re-add -- an open gate emits, so deleting here
    would be undone on the next rebuild and the two surfaces would disagree in the
    other direction.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"kirocrew-computer": {"command": "u"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        managed_specs={"kirocrew-computer": {"spec_gate": lambda: True}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"kirocrew-computer": {"command": "u"}}


@pytest.mark.asyncio
async def test_a_managed_server_whose_gate_raises_is_deleted(tmp_path):
    """A gate that RAISES is ineligible, mirroring ``_gated_off_servers`` exactly.

    That function catches every exception and adds the name to the closed set, so
    the emitter withholds the entry. Reading a raising gate as "eligible" here
    would preserve an entry the rebuild pops -- the same resurrection as the
    gate-closed row, reached through the fault path instead of the verdict.
    """

    def _explode() -> bool:
        raise RuntimeError("keystone unreadable")

    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"kirocrew-computer": {"command": "u"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        managed_specs={"kirocrew-computer": {"spec_gate": _explode}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a raising spec gate was read as eligible: the merge preserves what the "
        "emitter withholds"
    )


@pytest.mark.asyncio
async def test_an_edition_contributed_server_is_preserved(tmp_path):
    """An edition extra is host-owned and always emitted, so it is preserved.

    ``build_agent_config`` merges every ``_extra_mcp_servers`` entry with no gate
    and no opt-in test, so the rebuild re-adds it unconditionally -- the same
    justification as the always-emitted managed row. The eligibility predicate is
    applied to these entries UNIFORMLY rather than special-cased, so an extra that
    ever did carry ``opt_in`` or a closed gate would fall to DELETE with the
    managed rows instead of silently keeping the old blanket preserve.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"internal-mcp": {"command": "i"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        extra_managed={"internal-mcp": {"command": "i", "args": []}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"internal-mcp": {"command": "i"}}


@pytest.mark.asyncio
async def test_a_namespaced_edition_extra_is_preserved_by_host_ownership(tmp_path):
    """The one shape that is BOTH host-set and namespaced: host ownership decides.

    An edition may contribute a name containing ``:``, which is the only way a
    host-owned entry also looks app-owned (a managed name never contains one).
    Host ownership is proven from the contributed map alone, so ``vendor:tools``
    survives although the installed app ``demo`` declares nothing of the sort --
    the union does not require both sources to agree, and the app-declared set
    being empty for this name cannot veto it.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"vendor:tools": {"command": "v"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        extra_managed={"vendor:tools": {"command": "v", "args": []}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"vendor:tools": {"command": "v"}}


@pytest.mark.asyncio
async def test_a_malformed_host_spec_does_not_fail_the_put(tmp_path):
    """A non-object host spec keeps its pre-fix verdict instead of crashing the PUT.

    Reading ``opt_in``/``spec_gate`` off the spec introduces a shape the old
    keys-only union never touched, and the edition adapter behind
    ``_extra_mcp_servers`` is the reachable producer of it. Only the HOST can
    produce it at all, the name is host-owned either way, and this editor is the
    user's repair path -- so it is treated as eligible, which is exactly what the
    old code did, rather than raising ``AttributeError`` out of a commit unit whose
    contract is that step (0a) leaves all three targets byte-identical.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"internal-mcp": {"command": "i"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        extra_managed={"internal-mcp": "not-a-mapping"},  # type: ignore[dict-item]
    )

    assert response.status == 200
    assert written["mcpServers"] == {"internal-mcp": {"command": "i"}}


@pytest.mark.asyncio
async def test_client_entry_under_an_installed_apps_namespace_is_deleted(tmp_path):
    """ROUND 2: squatting an installed app's namespace must not confer ownership.

    App ``demo`` is installed and declares only ``notes``. The client adds
    ``demo:custom`` through this editor -- a name the app never registered -- and
    later omits it. A prefix test (``name.split(":")[0] in installed``) read that
    as app-owned and re-inserted it forever: F1's original defect, one branch
    narrower. Ownership is now the EXACT ``<app>:<server>`` set the manifests
    declare.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:custom": {"command": "mine"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a client entry squatting an installed app's namespace was preserved: "
        "prefix matching confers ownership the app never claimed"
    )


@pytest.mark.asyncio
async def test_a_declared_app_server_is_still_preserved(tmp_path):
    """The overshoot guard: a name the app genuinely declares still survives.

    Tightening ownership to exact names must not stop protecting real bridges --
    that is the defect #6664 exists to fix.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"demo:notes": {"command": "notes-mcp"}}, (
        "a server the app declares was deleted: the exact-name tightening "
        "overshot and stopped protecting real bridges"
    )


@pytest.mark.asyncio
async def test_unreadable_app_manifest_fails_the_put_and_writes_nothing(tmp_path):
    """An unreadable ownership source FAILS the PUT rather than guessing.

    Both guesses are wrong, and each reintroduces one of the two defects this
    span has already produced: preserving everything namespaced makes entries
    permanently undeletable, and deleting everything namespaced clobbers live app
    bridges under a fault that may be transient. So the PUT refuses.

    The refusal is cheap and correct: step (0a) runs before every durable write,
    so all three targets stay byte-identical and the client can simply retry.
    """
    installed = tmp_path / "kirocrew.json"
    on_disk = {"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}}

    response, written = await _put(
        tmp_path,
        on_disk=on_disk,
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        apps_unreadable=True,
    )

    assert response.status == 500
    assert (
        json.loads(response.text)["code"] == "app_ownership_unreadable"
    ), "a non-2xx body must carry a machine-readable code (AGENTS.md)"
    assert (
        json.loads(installed.read_text(encoding="utf-8")) == on_disk
    ), "the refused PUT still rewrote the agent spec"
    assert written == on_disk


@pytest.mark.asyncio
async def test_corrupt_installed_metadata_fails_the_put_and_writes_nothing(tmp_path):
    """ROUND 3: a malformed ``installed.json`` must refuse, not silently skip.

    ``manager._read_installed`` returns None for BOTH a missing file and a parse
    failure, so a bare ``is None: continue`` dropped a CORRUPT app out of the
    ownership scan entirely -- its live bridges then classified as client-owned
    and were deleted. Same asymmetric-failure class as the unreadable manifest,
    one branch over: absence is a real "not installed" answer, corruption is
    "cannot know".
    """
    installed = tmp_path / "kirocrew.json"
    on_disk = {"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}}

    response, written = await _put(
        tmp_path,
        on_disk=on_disk,
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        installed_corrupt=True,
    )

    assert response.status == 500
    assert json.loads(response.text)["code"] == "app_ownership_unreadable", (
        "corrupt installed metadata must refuse with the same machine-readable "
        "code as an unreadable manifest"
    )
    assert (
        json.loads(installed.read_text(encoding="utf-8")) == on_disk
    ), "the refused PUT still rewrote the agent spec"
    assert written == on_disk
    assert "demo:notes" in written["mcpServers"], "the live bridge was deleted"


@pytest.mark.asyncio
async def test_absent_installed_metadata_is_still_skipped(tmp_path):
    """The overshoot guard: no ``installed.json`` at all is a real answer.

    A directory under ``apps/`` with no installed metadata is not an installed
    app -- an ordinary uninstall race leaves exactly that. Skipping is correct,
    and the refusal above must not widen into refusing this.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        installed_absent=True,
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "an app with no installed metadata was treated as installed; absence is "
        "a real 'not installed' answer and its namespace confers no ownership"
    )


@pytest.mark.asyncio
async def test_disabled_app_bridge_is_deleted(tmp_path):
    """ROUND 4: a DISABLED app's stale bridge must be cleanable, not protected.

    A disabled app is still installed, so an installed-only ownership test keeps
    its declared names app-owned. That protects the exact entry the disable
    lifecycle was supposed to remove: when ``_deregister_mcp_servers`` FAILS
    during disable the entry survives, and startup reconciliation only
    re-registers ENABLED apps so it never revisits it. Every subsequent PUT would
    then re-insert it, leaving a disabled app's code launchable through the
    retained bridge forever. Ownership requires installed AND enabled.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        enabled=False,
    )

    assert response.status == 200
    assert written["mcpServers"] == {}, (
        "a disabled app's stale bridge was preserved: the disable lifecycle owns "
        "bridge removal, so a failed removal must be cleanable by the next PUT"
    )


@pytest.mark.asyncio
async def test_absent_enabled_field_counts_as_enabled(tmp_path):
    """A legacy record with no ``enabled`` field is ENABLED, matching the manager.

    ``InstalledApp.from_dict`` reads ``bool(data.get("enabled", True))``
    (manager.py:170) over a dataclass defaulting to ``enabled: bool = True``
    (manager.py:114), so every other reader in the tree treats such a record as
    running. Disagreeing here would delete the live bridges of an app the rest of
    the system considers enabled.

    The record is written WITHOUT the key and parsed by the real
    ``app_enabled_state``, so this pins the manager's own default rather than a
    stub's.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        enabled=None,
    )

    assert response.status == 200
    assert written["mcpServers"] == {"demo:notes": {"command": "notes-mcp"}}, (
        "a record with no 'enabled' field was treated as disabled, deleting the "
        "bridges of an app the manager considers enabled"
    )


@pytest.mark.asyncio
async def test_unreadable_apps_directory_fails_the_put(tmp_path):
    """An unenumerable apps directory refuses rather than reading as 'no apps'.

    Treating it as empty would classify every app bridge on the host as
    client-owned and delete all of them in one PUT.
    """
    installed = tmp_path / "kirocrew.json"
    on_disk = {"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}}

    response, written = await _put(
        tmp_path,
        on_disk=on_disk,
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        apps_dir_unreadable=True,
    )

    assert response.status == 500
    assert json.loads(response.text)["code"] == "app_ownership_unreadable"
    assert json.loads(installed.read_text(encoding="utf-8")) == on_disk
    assert written == on_disk


async def _assert_refused_and_intact(tmp_path, **kwargs):
    """PUT refuses with the ownership code and leaves the spec byte-identical."""
    installed = tmp_path / "kirocrew.json"
    on_disk = {"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}}

    response, written = await _put(
        tmp_path,
        on_disk=on_disk,
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        **kwargs,
    )

    assert response.status == 500
    assert json.loads(response.text)["code"] == "app_ownership_unreadable"
    assert json.loads(installed.read_text(encoding="utf-8")) == on_disk
    assert written == on_disk
    assert "demo:notes" in written["mcpServers"], "the live bridge was deleted"


@requires_symlinks
@pytest.mark.asyncio
async def test_installed_metadata_as_a_broken_symlink_fails_the_put(tmp_path):
    """ROUND 5: a DANGLING installed.json symlink is unreadable, not absent.

    ``Path.is_file()`` follows the link, finds nothing, and answers False -- the
    same False it gives for genuine absence -- so the app read as not installed
    and its live bridges became deletable. Absence is proven only by ``lstat``
    raising ``FileNotFoundError``; the link itself exists, so this is the
    cannot-read case and must refuse.
    """
    await _assert_refused_and_intact(tmp_path, installed_shape="broken_symlink")


@pytest.mark.asyncio
async def test_installed_metadata_as_a_directory_fails_the_put(tmp_path):
    """A directory where ``installed.json`` belongs is present, so unreadable.

    Needs no symlink privilege, which is why it carries the same row on every
    platform the broken-symlink test may skip on.
    """
    await _assert_refused_and_intact(tmp_path, installed_shape="directory")


@pytest.mark.asyncio
async def test_apps_root_as_a_regular_file_fails_the_put(tmp_path):
    """The apps root present but not a directory is unreadable, not empty.

    Reading it as empty would classify every app bridge on the host as
    client-owned and delete all of them in one PUT.
    """
    await _assert_refused_and_intact(tmp_path, apps_root_is_file=True)


@pytest.mark.asyncio
async def test_an_unstattable_apps_root_child_fails_the_put(tmp_path):
    """ROUND 6: a child the listing returns but cannot stat is unreadable.

    The enumeration screened its children with ``Path.is_dir()``, which routes
    the fault through pathlib's ``_ignore_error`` and answers a plain False for
    ENOENT, ENOTDIR, EBADF and ELOOP alike -- the same False it gives for a
    regular file. So a child that is a symlink LOOP was skipped as "not an app",
    and the absent bridge of the app living under that name was deleted: the
    cannot-read-becomes-not-owned defect one level inside the shapes round 5's
    screen already covers, and the same loop shape that screen refuses for
    ``installed.json``. Only a resolved stat may exclude a child, and only by
    PROVING it is not a directory.
    """
    await _assert_refused_and_intact(tmp_path, apps_child_unstattable=True)


@pytest.mark.asyncio
async def test_genuinely_absent_installed_metadata_still_skips(tmp_path):
    """The overshoot guard: real absence stays a real "not installed" answer.

    ``lstat`` raising ``FileNotFoundError`` is the ONLY proof of absence, and it
    must keep taking the skip branch -- an ordinary uninstall race leaves exactly
    this state, and refusing it would turn a routine PUT into a 500.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": {}},
        apps={"demo": ("notes",)},
        installed_absent=True,
    )

    assert response.status == 200
    assert written["mcpServers"] == {}


# ── (c) regressions: full replace still behaves for client-owned content ───────


@pytest.mark.asyncio
async def test_client_owned_entry_present_in_the_snapshot_is_updated(tmp_path):
    """A submitted entry always wins — merge adds, it never shadows."""
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"weather": {"url": "old"}}},
        submitted={"name": "kirocrew", "mcpServers": {"weather": {"url": "new"}}},
        scope_names=frozenset({"weather"}),
    )

    assert response.status == 200
    assert written["mcpServers"] == {"weather": {"url": "new"}}


@pytest.mark.asyncio
async def test_app_owned_entry_present_in_the_snapshot_is_updated(tmp_path):
    """Preservation must not shadow a submitted edit to the same name either."""
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "old"}}},
        submitted={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "new"}}},
        scope_names=frozenset(),
    )

    assert response.status == 200
    assert written["mcpServers"] == {"demo:notes": {"command": "new"}}, (
        "a preserved on-disk entry overwrote the client's own edit to the same "
        "name: the merge must yield to the submission where both carry a name"
    )


@pytest.mark.asyncio
async def test_non_mcp_keys_are_still_replaced_wholesale(tmp_path):
    """The merge is scoped to ``mcpServers``; every other key still replaces.

    ``mcpServers`` is the whole app-owned surface in this file — all three
    bridges writers of it touch that key and nothing else — so widening the merge
    would preserve state no app owns.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "prompt": "old", "tools": ["a"], "mcpServers": {}},
        submitted={"name": "kirocrew", "tools": ["b"]},
        scope_names=frozenset(),
    )

    assert response.status == 200
    assert written["tools"] == ["b"]
    assert "prompt" not in written, "a non-mcpServers key survived the snapshot replace"


@pytest.mark.asyncio
async def test_unreadable_installed_spec_still_accepts_the_snapshot(tmp_path):
    """A corrupt spec must stay repairable through this editor.

    Best-effort by design: a corrupt file has no parseable entries to preserve,
    and this endpoint is the user's repair path for exactly that state. Failing
    closed would leave a broken agent with no way to fix it from the dashboard;
    enabled apps re-register their servers on the next gateway start, so the loss
    self-heals.
    """
    response, written = await _put(
        tmp_path,
        on_disk="{ not json at all",
        submitted={"name": "kirocrew", "mcpServers": {"weather": {"url": "w"}}},
    )

    assert response.status == 200
    assert written["mcpServers"] == {"weather": {"url": "w"}}


@pytest.mark.asyncio
async def test_non_object_mcp_servers_submission_is_left_untouched(tmp_path):
    """A non-object ``mcpServers`` is a shape kiro-cli rejects outright.

    Merging into it would mean inventing a map the client never sent, so the
    submission is persisted as-is and its rejection is unchanged.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        submitted={"name": "kirocrew", "mcpServers": []},
        scope_names=frozenset(),
    )

    assert response.status == 200
    assert written["mcpServers"] == []


# ── the preserved entries are governed, not smuggled past the filter ──────────


@pytest.mark.asyncio
async def test_preserved_entries_go_through_the_governance_filter(tmp_path, monkeypatch):
    """Merge runs BEFORE the governance filter, so what it re-adds is governed.

    ``autoApprove`` is the one path that never reaches the permission gate, so an
    entry re-injected AFTER step (0) would carry a ceiling-denied auto-approve
    straight to disk — reopening the bypass that filter exists to close. Ordering
    the merge ahead of the filter makes that unrepresentable.
    """
    import kiro_crew.platform.governance as gov

    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@demo:notes")

    response, written = await _put(
        tmp_path,
        on_disk={
            "name": "kirocrew",
            "mcpServers": {"demo:notes": {"command": "notes-mcp", "autoApprove": ["write"]}},
        },
        submitted={"name": "kirocrew", "mcpServers": {}},
        scope_names=frozenset(),
        govern=True,
    )

    assert response.status == 200
    # Preserved at all — the merge ran.
    assert "demo:notes" in written["mcpServers"]
    # ...and governed, which only holds if it was merged before the filter.
    assert "autoApprove" not in written["mcpServers"]["demo:notes"], (
        "a preserved entry kept a ceiling-denied autoApprove: the merge ran "
        "AFTER the governance filter, so what it re-added was never governed"
    )


# ── (d) the stale-snapshot axis: entries PRESENT in the submission (#7089) ─────
#
# The mirror of section (b). There the submission OMITS a name and the question is
# whether to KEEP it; here the submission CONTAINS a namespaced name absent from
# disk and the question is whether the client may CREATE one. It may not: only
# ``_register_mcp_servers`` writes the ``<app>:<server>`` region, so absence from
# disk is a verdict the platform reached (uninstall, disable, or a deliberate
# skip) rather than a gap for a snapshot to fill.
#
# Where the name is on disk AND submitted, the submitted row still wins untouched
# -- see ``test_app_owned_entry_present_in_the_snapshot_is_updated`` in section
# (c). This axis deletes; it never rewrites a submitted value.


@pytest.mark.asyncio
async def test_a_stale_snapshot_cannot_resurrect_an_uninstalled_apps_bridge(tmp_path):
    """The headline case: the app went away, the editor tab did not.

    The tab loaded while app ``demoapp`` was installed and running, so its
    snapshot holds ``demoapp:tool``. The app was then uninstalled --
    ``_deregister_mcp_servers`` removed the bridge and the app directory is gone,
    so nothing on disk or under apps/ mentions it. Pre-fix the snapshot was
    persisted verbatim and the bridge came back live, with nothing logged; and it
    stayed back, because ``reconcile_enabled_app_resources`` only re-registers
    ENABLED apps and there is no longer an app here at all.

    The user's own plain entry in the same submission is untouched, which is what
    makes this specifically the app-namespace axis rather than a blanket refusal.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"plainuser": {"url": "u"}}},
        submitted={
            "name": "kirocrew",
            "mcpServers": {
                "demoapp:tool": {"url": "http://127.0.0.1:9100/mcp"},
                "plainuser": {"url": "u"},
            },
        },
        apps={},  # nothing installed: the app was uninstalled
    )

    assert response.status == 200
    assert "demoapp:tool" not in written["mcpServers"], (
        "a stale editor snapshot resurrected app-owned server 'demoapp:tool' that "
        "the installed spec does not hold: the PUT trusted the snapshot's copy of "
        "a name in the app-namespace region instead of on-disk state"
    )
    assert written["mcpServers"]["plainuser"] == {"url": "u"}


@pytest.mark.asyncio
async def test_a_stale_snapshot_cannot_resurrect_a_disabled_apps_bridge(tmp_path):
    """Installed but DISABLED, the case reconciliation provably never repairs.

    Disable calls ``_deregister_mcp_servers``, so the bridge is off disk while the
    app directory and its manifest remain. Resurrecting it keeps a disabled app's
    code launchable through the retained bridge indefinitely -- startup
    reconciliation skips disabled apps, so nothing ever removes it again.

    ``scope_names`` declares the same name in the user's own mcp.json to pin that a
    scope declaration does not rescue it, matching the absent-axis rule where
    proven ownership -- not a declaration -- is what decides.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {}},
        submitted={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        apps={"demo": ("notes",)},
        enabled=False,
        scope_names=frozenset({"demo:notes"}),
    )

    assert response.status == 200
    assert "demo:notes" not in written["mcpServers"], (
        "a stale snapshot resurrected the bridge of a DISABLED app: nothing "
        "removes it again, because startup reconciliation only re-registers "
        "enabled apps"
    )


@pytest.mark.asyncio
async def test_a_stale_snapshot_cannot_rewrite_a_deliberately_skipped_http_bridge(tmp_path):
    """ENABLED and DECLARED, yet absent from disk on purpose -- still dropped.

    ``_register_mcp_servers`` refuses to write an HTTP server with no resolvable
    live port and scrubs any stale entry for it, because a manifest's illustrative
    port is a reachable-LOOKING dead URL that kiro-cli dials on every request:
    a connect failure there breaks EVERY kiro session, not just this app's.

    So this row also pins that the declared-name census is NOT consulted on this
    axis. The app is installed, enabled, and declares ``notes`` -- every ingredient
    the absent-axis ownership test needs -- and the entry is still dropped, because
    the only question here is whether the platform PUT IT ON DISK, which it
    deliberately did not.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {}},
        submitted={
            "name": "kirocrew",
            # The manifest's illustrative port, which is what a snapshot taken
            # before the scrub carries.
            "mcpServers": {"demo:notes": {"url": "http://127.0.0.1:9100/mcp"}},
        },
        apps={"demo": ("notes",)},
        enabled=True,
    )

    assert response.status == 200
    assert "demo:notes" not in written["mcpServers"], (
        "the PUT wrote back an HTTP bridge the registration path had scrubbed for "
        "having no live backend: that dead URL breaks every kiro session, and the "
        "declared-name census must not rescue an entry absent from disk"
    )


@pytest.mark.asyncio
async def test_both_directions_decide_from_one_baseline_in_a_single_put(tmp_path):
    """The two rules compose, and they read the same on-disk map to do it.

    One realistic save carries both faults at once: the tab is old enough that its
    snapshot still holds a bridge of an app that has since gone away
    (``gone:tool``), and old enough that it predates a bridge that has since been
    registered (``demo:notes``). The stale one must be dropped and the new one
    preserved in the same PUT, off a single read -- two reads could only agree by
    luck, and a rule pair disagreeing about its baseline would show up here as a
    name both preserved and dropped.

    ``apps`` installs only ``demo``, so ``gone`` has no owner to name while
    ``demo:notes`` has one.
    """
    response, written = await _put(
        tmp_path,
        on_disk={
            "name": "kirocrew",
            "mcpServers": {"demo:notes": {"command": "notes-mcp"}, "plainuser": {"url": "u"}},
        },
        submitted={
            "name": "kirocrew",
            "mcpServers": {"gone:tool": {"command": "t"}, "plainuser": {"url": "u"}},
        },
        apps={"demo": ("notes",)},
    )

    assert response.status == 200
    assert "gone:tool" not in written["mcpServers"], (
        "the stale namespaced addition survived alongside a preserved bridge: the "
        "two rules did not both act on the same baseline"
    )
    assert written["mcpServers"]["demo:notes"] == {"command": "notes-mcp"}, (
        "the absent-axis merge stopped preserving an owned bridge once the "
        "stale-snapshot rule ran ahead of it"
    )
    assert written["mcpServers"]["plainuser"] == {"url": "u"}


@pytest.mark.asyncio
async def test_a_plain_client_entry_the_client_adds_is_still_persisted(tmp_path):
    """The rule is scoped to the namespace shape, and this is the guard on that.

    A server the user types into this raw editor has no ``:`` in its name and is
    absent from disk by definition on the PUT that creates it. Reading "absent
    from disk" as a verdict for THAT entry would make the editor unable to add
    anything at all -- the opposite failure, and a much louder one.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {}},
        submitted={"name": "kirocrew", "mcpServers": {"myserver": {"command": "mine"}}},
    )

    assert response.status == 200
    assert written["mcpServers"]["myserver"] == {"command": "mine"}, (
        "the client could not add its own server: the stale-snapshot rule reached "
        "past the app-namespace region it is scoped to"
    )


@pytest.mark.asyncio
async def test_a_namespaced_edition_extra_the_client_resubmits_is_left_alone(tmp_path):
    """A ``:`` in the key does not always mean an app owns it.

    An edition's ``_extra_mcp_servers`` may contribute a key containing ``:``, and
    that key is the HOST's. The absent-axis rule already preserves such an entry by
    host ownership; this axis must not delete the same entry when the client
    resubmits it while the rebuild has yet to write it, so host-owned names are
    excluded here and the host contract is left exactly as it was.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {}},
        submitted={"name": "kirocrew", "mcpServers": {"edition:extra": {"command": "x"}}},
        extra_managed={"edition:extra": {"command": "x"}},
    )

    assert response.status == 200
    assert written["mcpServers"]["edition:extra"] == {"command": "x"}, (
        "a host-owned entry whose name happens to contain ':' was dropped as if an "
        "app owned it: this axis must exclude host-owned names"
    )


@pytest.mark.asyncio
async def test_a_readable_spec_with_no_servers_still_drops_a_namespaced_addition(tmp_path):
    """``{}`` on disk is an ANSWER, and this is the row that makes it one.

    A spec that reads cleanly and holds no ``mcpServers`` is definite: the platform
    has written no bridge, so a namespaced name in the submission is an addition to
    a region the client does not author. Collapsing this state with "could not
    read" would silently reopen the whole axis for the commonest shape of all -- a
    spec whose app bridges have just been deregistered.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {}},
        submitted={"name": "kirocrew", "mcpServers": {"demoapp:tool": {"command": "t"}}},
        apps={},
    )

    assert response.status == 200
    assert "demoapp:tool" not in written["mcpServers"], (
        "an empty-but-readable mcpServers map was treated as unreadable, so the "
        "stale entry was persisted"
    )


@pytest.mark.asyncio
async def test_a_spec_with_no_mcpservers_key_still_drops_a_namespaced_addition(tmp_path):
    """A KEYLESS spec is ``{}``, not "unknown" -- the GPT round-1 finding on #7465.

    Reading a missing ``mcpServers`` key as unreadable hands this rule a reason to
    stand down and lets the resurrection straight through. The state is reachable
    from this handler: a PUT whose submission omits ``mcpServers`` is persisted
    verbatim, and ``_deregister_mcp_servers`` pops entries out of
    ``get("mcpServers", {})`` without ever adding the key back -- so the spec can sit
    keyless while an old editor tab still holds the bridge.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew"},  # readable, and carries no mcpServers at all
        submitted={"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "notes-mcp"}}},
        apps={"demo": ("notes",)},
        enabled=False,
    )

    assert response.status == 200
    assert "demo:notes" not in written["mcpServers"], (
        "a spec with no mcpServers key was treated as unreadable rather than as "
        "holding no bridge, so the stale snapshot resurrected an executable bridge"
    )


@pytest.mark.asyncio
async def test_a_malformed_mcpservers_value_on_disk_decides_nothing(tmp_path):
    """Present but not an object stays ``None``: we do not delete on a value we cannot read.

    The file parsed, but that value cannot be interpreted -- the same
    cannot-interpret state the submitted-side guard in ``_merge_unowned_servers``
    refuses to act on. Deleting the client's entries on the strength of it would be
    the guess this span exists to avoid, so the submission lands as it did pre-fix.
    """
    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": "not an object"},
        submitted={"name": "kirocrew", "mcpServers": {"demoapp:tool": {"command": "t"}}},
        apps={},
    )

    assert response.status == 200
    assert written["mcpServers"]["demoapp:tool"] == {"command": "t"}, (
        "an uninterpretable on-disk mcpServers value was read as an empty map, so "
        "the client's submission was deleted on evidence we could not read"
    )


@pytest.mark.asyncio
async def test_an_unreadable_spec_still_persists_a_namespaced_entry(tmp_path):
    """BEST-EFFORT holds on this axis too, for the same reason as the merge's.

    A corrupt spec is precisely the state this editor exists to repair. Refusing
    the PUT, or deleting what the user submitted, would leave a broken agent with
    no way to fix it from the dashboard -- so an unreadable baseline decides
    nothing and the snapshot lands as it did pre-fix. Enabled apps re-register on
    the next gateway start, so the residue self-heals.
    """
    response, written = await _put(
        tmp_path,
        on_disk="{ not json at all",
        submitted={"name": "kirocrew", "mcpServers": {"demoapp:tool": {"command": "t"}}},
        apps={},
    )

    assert response.status == 200
    assert written["mcpServers"]["demoapp:tool"] == {"command": "t"}, (
        "an unreadable spec deleted what the client submitted: this axis must be "
        "best-effort so the raw editor stays the repair path for a corrupt spec"
    )


# ── (g) the region's axis matrix, and the one cell still open (#7470) ──────────


@pytest.mark.asyncio
async def test_the_app_namespace_region_decides_every_axis_it_claims_to(tmp_path):
    """Every axis of the app-namespace region in ONE table, so a missing one shows.

    THE FAILURE THIS EXISTS TO CATCH is a rule set that reads as complete and is
    not. #6975 shipped the ABSENT axis; the PRESENT axis was not missing from that
    review's conclusions so much as never enumerated, and it survived review to
    become #7089 months later. A per-axis test cannot prevent that on its own --
    each one passes in isolation -- so the axes are gathered here, and a region
    whose behaviour changes on any axis has to come through this table.

    The three cells and who decides each:

    * EXISTENCE, name ABSENT from the submission -> ON DISK decides
      (``_merge_unowned_servers``, #6664): an owned bridge is kept.
    * EXISTENCE, name PRESENT in the submission with no row on disk -> ON DISK
      decides (``_drop_unbacked_app_entries``, #7089): the addition is dropped.
    * CONTENT, name on BOTH sides -> the SUBMISSION decides. **This cell is
      OPEN** (#7470): a stale editor snapshot reverts a definition the platform
      had already corrected. It is asserted here as it BEHAVES, not as it should,
      because reversing it reverses the editor-snapshot-wins contract kept in
      #5899 and re-affirmed for #6664 -- a maintainer ruling, not a review-time
      call. When that ruling lands, this is the assertion that changes.
    """
    submitted_only = {"name": "kirocrew", "mcpServers": {"demo:ghost": {"command": "ghost"}}}
    matrix = [
        (
            "existence / absent from the submission -> on disk decides",
            {"demo:notes": {"command": "live"}},
            {"name": "kirocrew", "mcpServers": {}},
            {"demo:notes": {"command": "live"}},
            "an owned bridge the submission omits was not preserved",
        ),
        (
            "existence / present in the submission, absent from disk -> on disk decides",
            {},
            submitted_only,
            {},
            "a namespaced name with no row on disk was created through this PUT",
        ),
        (
            "content / on both sides -> the submission decides (OPEN, #7470)",
            {"demo:notes": {"command": "live"}},
            {"name": "kirocrew", "mcpServers": {"demo:notes": {"command": "stale"}}},
            {"demo:notes": {"command": "stale"}},
            "the content axis changed verdict without the ruling #7470 is waiting on",
        ),
    ]

    for axis, on_disk, submitted, expected, why in matrix:
        response, written = await _put(
            tmp_path,
            on_disk={"name": "kirocrew", "mcpServers": on_disk},
            submitted=submitted,
        )

        assert response.status == 200, f"{axis}: PUT failed"
        assert written["mcpServers"] == expected, f"{axis}: {why}"


@pytest.mark.asyncio
async def test_a_stale_snapshot_reverts_a_live_http_bridge_to_a_dead_url(tmp_path):
    """What the open content cell costs, in the instance that makes it load-bearing.

    A ``backend.port:"auto"`` app gets a free port at spawn time and
    ``_register_mcp_servers`` rewrites its manifest url to that live port
    (``_resolve_live_mcp_url``). An editor tab opened before that rewrite still
    holds the manifest's ILLUSTRATIVE port, and saving it writes that port back.

    The reverted value is therefore the exact artefact the registration path
    refuses to write and scrubs on sight -- a reachable-LOOKING dead URL -- whose
    cost ``_drop_unbacked_app_entries`` states as breaking every kiro session,
    not just this app's. Two things narrow it and neither removes it: the PUT's
    own tail drains every session and the warm pool, so the next cold start reads
    the reverted row; and the only writer that restores the live port is
    ``reconcile_enabled_app_resources``, whose single call site is the gateway
    boot path -- so the window is until the next restart.

    Asserted as it BEHAVES. This is the same open cell as the content row of
    :func:`test_the_app_namespace_region_decides_every_axis_it_claims_to`, kept
    separate because the severity, not the verdict, is what it records.
    """
    live = "http://127.0.0.1:9137/mcp"  # the port the backend actually got
    illustrative = "http://127.0.0.1:9100/mcp"  # what the manifest, and the tab, hold

    response, written = await _put(
        tmp_path,
        on_disk={"name": "kirocrew", "mcpServers": {"demo:notes": {"url": live}}},
        submitted={"name": "kirocrew", "mcpServers": {"demo:notes": {"url": illustrative}}},
    )

    assert response.status == 200
    assert written["mcpServers"]["demo:notes"]["url"] == illustrative, (
        "the live url survived a stale snapshot: the content axis now decides from "
        "on-disk state, which is the reversal #7470 is waiting on a ruling for -- "
        "update this test and the decision table in "
        "docs/system-specs/modules/app-kit-platform.md together with it"
    )

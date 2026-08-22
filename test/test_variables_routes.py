"""Tests for the /api/variables dashboard routes.

Hermetic: every case redirects ``config_path`` at a ``tmp_path`` file and hands the
handler a config object directly, so nothing reads or writes the real data home and
the loader's fingerprint cache never participates.

The redirect is on ``config_path`` and NOT on ``variables_store.store_path``, because
the store's location is DERIVED from the config directory. Patching the derived path
would still pass if the store were hardcoded somewhere else, which is the one thing
the derivation exists to prevent.

Variables no longer live in ``config.json``: they have their own store document,
``{"global": {...}, "workspaces": {name: {...}}, "crews": {name: {...}}}``, with this
endpoint as its only writer. So the write-side cases here assert against the STORE,
and the whole overlay half of this suite is gone — there is no second layer over the
store, hence no key this endpoint can read but not write.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config import loader as cfg_loader
from kiro_crew.config import variables_store as vstore
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.dashboard.handlers import variables as vh

_NOT_POSIX = os.name == "nt"

# Every test here awaits a handler directly.
pytestmark = pytest.mark.asyncio


def _request(method: str, body: Any = ...):
    """A mocked request. ``body=None`` models a malformed payload, which is what
    the handler's ``except Exception -> 400`` branch is written for."""
    req = make_mocked_request(method, "/api/variables")
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _config() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = {"baseUrl": "https://global.test", "orgName": "Acme"}
    cfg.workspaces = {
        "default": WorkspaceConfig(dir="workspace"),
        "ops": WorkspaceConfig(dir="workspace-ops", variables={"baseUrl": "https://ops.test"}),
    }
    cfg.default_workspace = "default"
    cfg.agents = {
        "crew1": KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="ops", variables={"queue": "oncall"}
        )
    }
    cfg.default_agent = "crew1"
    return cfg


def _seed(store: Path, doc: dict) -> str:
    """Write a store document and return the exact bytes, for survival checks."""
    text = json.dumps(doc)
    store.write_text(text, encoding="utf-8")
    return text


def _doc(store: Path) -> dict:
    return json.loads(store.read_text(encoding="utf-8"))


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    """Redirect the store into ``tmp_path`` and pin a fixed config object.

    The store file starts ABSENT, which is the fresh-install state: reads resolve to
    no variables and the first write creates it. A test that cares about reported or
    stored pairs seeds it explicitly, because the config OBJECT no longer implies an
    editable pair — the store says what this endpoint can edit, the config says what
    a session resolves, and keeping those apart is the point of the read side.
    """
    cfg = _config()
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"workspaces": {"ops": {"dir": "workspace-ops"}}}), "utf-8")
    monkeypatch.setattr(cfg_loader, "config_path", lambda: config_file)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    vstore.invalidate_cache()
    # Derived from the implementation, not rebuilt: the store lives in its own fenced
    # directory (so its lock and atomic-write temp files are fenced too), and the
    # directory has to exist before a test seeds the file by hand.
    store = vstore.store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    return cfg, store


async def test_the_store_sits_beside_the_config_file(wired):
    """Pins the derivation the whole fixture depends on."""
    _, store = wired
    assert vstore.store_path() == store


async def test_view_inputs_returns_the_config_and_the_store_document(wired):
    """Two values, unpacked straight into ``_view``. A third would silently shift the
    handler's arguments by one."""
    _, store = wired
    _seed(store, {"global": {"a": "1"}})
    inputs = vh._view_inputs()
    assert len(inputs) == 2
    cfg, doc = inputs
    assert isinstance(cfg, KiroCrewConfig)
    assert doc == {"global": {"a": "1"}}


async def test_view_blocking_renders_the_whole_view_not_just_its_inputs(wired):
    """The offload is only worth anything if the thread does the RENDER too.

    A ``_view_blocking`` that returned the inputs, or an empty projection, would keep
    the handler's ``to_thread`` call looking right while the resolution it exists to
    move stayed on the loop.
    """
    _, store = wired
    _seed(store, {"global": {"a": "1"}})
    view = vh._view_blocking()
    assert view["global"] == {"a": "1"}
    # The resolved half is what ``_view`` adds over ``_view_inputs``; its presence is
    # what proves the render ran inside the offloaded call.
    assert view["effective"]["a"] == "1"
    assert view["winning_scope"]["a"] == vh.SCOPE_GLOBAL


async def test_get_reports_every_scope(wired):
    """Each scope reports what the STORE holds, since that is what a PUT can change.

    There is no ``overlay_owned`` key any more: it named the pairs a second config
    layer owned, and the store has no second layer.
    """
    _, store = wired
    _seed(
        store,
        {
            "global": {"baseUrl": "https://global.test", "orgName": "Acme"},
            "workspaces": {"ops": {"baseUrl": "https://ops.test"}},
            "crews": {"crew1": {"queue": "oncall"}},
        },
    )
    resp = await vh.api_variables(_request("GET"))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["global"] == {"baseUrl": "https://global.test", "orgName": "Acme"}
    assert payload["workspaces"]["ops"] == {"baseUrl": "https://ops.test"}
    assert payload["crews"]["crew1"] == {"queue": "oncall"}
    assert "overlay_owned" not in payload


async def test_get_reports_stored_pairs_and_skips_stale_names(wired):
    """The per-scope maps come from the store, keyed off the CONFIG's names.

    Reporting resolved values as editable is what let the panel show a pair it did not
    own; advertising a store entry for a workspace the config does not define would
    offer an edit the loader can never apply.
    """
    _, store = wired
    _seed(
        store,
        {
            "global": {"only_in_store": "yes"},
            "workspaces": {"ops": {"a": "1"}, "ghost": {"b": "2"}},
        },
    )
    payload = json.loads((await vh.api_variables(_request("GET"))).text)
    # The config object carries baseUrl/orgName; the editable map does not.
    assert payload["global"] == {"only_in_store": "yes"}
    assert payload["workspaces"] == {"default": {}, "ops": {"a": "1"}}
    assert "ghost" not in payload["workspaces"]


async def test_get_reports_resolution_and_provenance(wired):
    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    # crew1 binds workspace ops, so the workspace value wins over global.
    assert payload["effective"]["baseUrl"] == "https://ops.test"
    assert payload["winning_scope"]["baseUrl"] == "workspace"
    assert payload["shadowed"]["baseUrl"] == ["global"]
    assert payload["effective"]["queue"] == "oncall"
    assert payload["winning_scope"]["queue"] == "crew"
    assert payload["active_workspace"] == "ops"
    assert payload["active_agent"] == "crew1"


async def test_put_global_persists(wired):
    _, store = wired
    resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1", "b": ""}}))
    assert resp.status == 200
    assert json.loads(resp.text)["ok"] is True
    assert _doc(store)["global"] == {"a": "1", "b": ""}


async def test_a_set_leaves_unnamed_keys_alone(wired):
    """The property the whole-scope form could not offer, and the reason it went.

    Under the replace contract, a second write that did not re-list ``b`` deleted it
    — which is how one tab's save discarded another tab's edit. A patch touches only
    what it names.
    """
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1", "b": "2"}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "9"}}))
    assert _doc(store)["global"] == {"a": "9", "b": "2"}


async def test_a_workspace_set_leaves_unnamed_keys_alone(wired):
    """The workspace branch has its own copy of the patch logic, so it needs its
    own proof. A mutation that made only this branch replace the whole scope passed
    the suite until this test existed — the global-scope test above says nothing
    about it.
    """
    _, store = wired
    await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"a": "1", "b": "2"}})
    )
    await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"a": "9"}})
    )
    assert _doc(store)["workspaces"]["ops"] == {"a": "9", "b": "2"}


async def test_a_patch_leaves_the_other_scopes_alone(wired):
    """Scope-level version of the same property: a global write must not read, and so
    cannot rewrite, the workspace or crew containers."""
    _, store = wired
    _seed(store, {"workspaces": {"ops": {"a": "1"}}, "crews": {"crew1": {"queue": "oncall"}}})
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"g": "1"}}))
    doc = _doc(store)
    assert doc["global"] == {"g": "1"}
    assert doc["workspaces"] == {"ops": {"a": "1"}}
    assert doc["crews"] == {"crew1": {"queue": "oncall"}}


async def test_a_workspace_delete_removes_only_the_named_key(wired):
    _, store = wired
    await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"a": "1", "b": "2"}})
    )
    await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "delete": ["a"]})
    )
    assert _doc(store)["workspaces"]["ops"] == {"b": "2"}


async def test_delete_is_an_explicit_verb(wired):
    """Removal is named rather than implied by absence, which is what keeps the
    empty string unambiguous: it stays a legal value that overrides a broader
    scope, instead of colliding with 'unset'."""
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1", "b": ""}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "delete": ["b"]}))
    assert _doc(store)["global"] == {"a": "1"}


async def test_an_empty_string_survives_a_later_patch(wired):
    """An empty value is not absence: a patch that does not name it must keep it."""
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"blank": ""}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"other": "x"}}))
    assert _doc(store)["global"] == {"blank": "", "other": "x"}


async def test_set_and_delete_apply_together(wired):
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1", "b": "2"}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"c": "3"}, "delete": ["a"]}))
    assert _doc(store)["global"] == {"b": "2", "c": "3"}


async def test_setting_and_deleting_one_key_is_refused(wired):
    """Ambiguous by construction, so it is refused rather than resolved by ordering
    — whichever the server applied second would be silently arbitrary."""
    resp = await vh.api_variables(
        _request("PUT", {"scope": "global", "set": {"a": "1"}, "delete": ["a"]})
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "variables_conflicting_change"


async def test_a_body_naming_neither_set_nor_delete_is_refused(wired):
    resp = await vh.api_variables(_request("PUT", {"scope": "global"}))
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "variables_invalid_values"


async def test_deleting_a_key_that_is_not_there_is_not_an_error(wired):
    """Idempotent: two tabs can both delete the same row without the second seeing
    a failure for work that is already done."""
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1"}}))
    resp = await vh.api_variables(_request("PUT", {"scope": "global", "delete": ["ghost"]}))
    assert resp.status == 200
    assert _doc(store)["global"] == {"a": "1"}


async def test_put_workspace_persists_under_that_workspace(wired):
    _, store = wired
    resp = await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"queue": "tier2"}})
    )
    assert resp.status == 200
    assert _doc(store)["workspaces"] == {"ops": {"queue": "tier2"}}


@pytest.mark.skipif(_NOT_POSIX, reason="POSIX file modes")
async def test_the_store_is_written_with_a_tight_mode(wired):
    """Defence in depth rather than the security boundary — values are declared
    non-secret — but a store this endpoint creates must not land world-readable."""
    _, store = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1"}}))
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


class TestRejections:
    """Every non-2xx body carries a machine-readable ``code`` — the dashboard
    renders server prose verbatim, so the identifier is what a client switches on."""

    async def test_malformed_json(self, wired):
        resp = await vh.api_variables(_request("PUT", None))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_json"

    async def test_non_object_body(self, wired):
        resp = await vh.api_variables(_request("PUT", ["not", "an", "object"]))
        assert json.loads(resp.text)["code"] == "variables_invalid_body"

    async def test_unknown_scope(self, wired):
        """``crew`` is a real scope in the store but not a writable one here."""
        resp = await vh.api_variables(_request("PUT", {"scope": "crew", "set": {}}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_scope"

    async def test_missing_values(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global"}))
        assert json.loads(resp.text)["code"] == "variables_invalid_values"

    async def test_a_non_object_set_is_refused(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": ["a"]}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_values"

    async def test_a_non_array_delete_is_refused(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global", "delete": "a"}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_values"

    async def test_unknown_workspace(self, wired):
        """Refused for the caller's sake: a store entry naming a workspace the config
        does not define is inert, so accepting it would report a save that can never
        take effect."""
        resp = await vh.api_variables(
            _request("PUT", {"scope": "workspace", "workspace": "nope", "set": {}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_unknown_workspace"

    async def test_a_workspace_scope_with_no_workspace_named_is_refused(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "workspace", "set": {"a": "1"}}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_unknown_workspace"

    async def test_invalid_name_names_the_key(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": {"1bad": "x"}}))
        assert resp.status == 400
        payload = json.loads(resp.text)
        assert payload["code"] == "variables_invalid_pair"
        assert payload["key"] == "1bad"

    async def test_an_invalid_name_in_delete_names_the_key(self, wired):
        """A delete carries no value, so it is validated by the same grammar with a
        throwaway one: an unparseable name could not have been stored here."""
        resp = await vh.api_variables(_request("PUT", {"scope": "global", "delete": ["1bad"]}))
        assert resp.status == 400
        payload = json.loads(resp.text)
        assert payload["code"] == "variables_invalid_pair"
        assert payload["key"] == "1bad"

    async def test_reserved_name_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "set": {"MAX_SUBAGENTS": "9"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_control_character_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "set": {"a": "one\ntwo"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_a_rejected_write_persists_nothing(self, wired):
        _, store = wired
        await vh.api_variables(_request("PUT", {"scope": "global", "set": {"1bad": "x"}}))
        assert not store.exists(), "a refused write created the store"

    async def test_a_rejected_write_leaves_an_existing_store_untouched(self, wired):
        _, store = wired
        before = _seed(store, {"global": {"a": "1"}})
        await vh.api_variables(_request("PUT", {"scope": "global", "set": {"1bad": "x"}}))
        assert store.read_text(encoding="utf-8") == before


async def test_payload_satisfies_the_frontend_interface(wired):
    """The panel reads a typed `VariablesView`; a field renamed on one side and
    not the other type-checks fine and fails only in a browser.

    The parser is brace-aware and strips the optional marker. Both matter: the first
    version stopped at the first ``}``, which a nested object type closes early, and
    it kept the ``?`` from an optional field, so a declared ``name?`` was compared
    against a payload key spelled ``name`` and reported missing. Optional fields are
    still required to be PRESENT here — the handler is their only producer, so absence
    would mean the field was renamed or dropped.
    """
    client = (
        Path(__file__).resolve().parents[1] / "website" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    start = client.index("export interface VariablesView")
    body_start = client.index("{", start)
    depth = 0
    declared: set[str] = set()
    for line in client[body_start:].splitlines():
        stripped = line.strip()
        if stripped.startswith(("/*", "*", "//")):
            continue
        # Collect only depth-1 members: a nested object's own fields belong to that
        # inner type, not to VariablesView.
        if depth == 1 and ":" in stripped:
            name = stripped.split(":")[0].strip().rstrip("?")
            if name:
                declared.add(name)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    assert declared, "could not parse VariablesView — the interface moved or was renamed"

    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    missing = declared - set(payload)
    assert not missing, f"handler payload is missing fields the panel reads: {sorted(missing)}"
    # The retired overlay layer must not come back through the type either.
    assert "overlay_owned" not in declared


class TestTheWriteIsLockedAndOffLoop:
    """The store write must not race another config writer, and must not block the
    gateway's event loop."""

    async def test_the_write_is_dispatched_through_run_config_write(self, wired):
        """``run_config_write`` is the one entry point that holds BOTH locks: the
        loop-side asyncio lock, and — inside the worker thread — the store's own
        advisory flock. Holding one of the two let a variables PUT and a settings PUT
        interleave and revert each other."""
        _, store = wired
        seen: dict[str, Any] = {}

        async def _fake(fn, /, *args, **kwargs):
            seen["fn"] = fn
            seen["kwargs"] = kwargs
            return fn(*args, **kwargs)

        with patch.object(vh, "run_config_write", _fake):
            resp = await vh.api_variables(
                _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"a": "1"}})
            )
        assert resp.status == 200
        assert seen["fn"] is vstore.patch_store
        assert seen["kwargs"] == {
            "scope": "workspace",
            "name": "ops",
            "values": {"a": "1"},
            "removals": [],
        }
        assert _doc(store)["workspaces"]["ops"] == {"a": "1"}

    async def test_the_patch_applies_to_the_document_the_lock_handed_it(self, wired):
        """Two writers on different keys must not clobber each other.

        The callback must patch the dict read INSIDE the lock, not a copy read before
        it: another writer's key landing in the same scope in the meantime survives,
        because a key nobody named is never read and never rewritten.
        """
        _, store = wired
        seen: dict[str, Any] = {}

        def _fake(target, *, mutate, **kwargs):
            seen["target"] = target
            seen["kwargs"] = kwargs
            data = {"global": {"other": "kept"}, "crews": {"crew1": {"queue": "oncall"}}}
            seen["result"] = mutate(data)
            return data

        with patch.object(cfg_loader, "update_config_locked", _fake):
            resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": {"a": "1"}}))
        assert resp.status == 200
        assert seen["target"] == store
        # Not a config document: it must not grow config's bookkeeping keys, and a
        # corrupt store must not be reset to {} to service one patch.
        assert seen["kwargs"]["stamp_meta"] is False
        assert seen["kwargs"]["on_corrupt"] == "fail"
        assert seen["result"]["global"] == {"other": "kept", "a": "1"}
        assert seen["result"]["crews"] == {"crew1": {"queue": "oncall"}}

    async def test_the_blocking_calls_run_off_the_event_loop(self):
        """The store write reads, locks and fsyncs; on the loop that freezes every
        task. Scoped to the async handler — it is the only place running on the loop,
        and a module-wide scan would flag a nested call that is already off-loop."""
        handler = inspect.getsource(vh.api_variables)
        assert "run_config_write(" in handler
        assert "vstore.patch_store" in handler
        assert (
            "asyncio.to_thread(vstore.patch_store" not in handler
        ), "the bare to_thread form holds only the flock, not the loop-side lock"
        assert "asyncio.to_thread(_view_blocking)" in handler
        assert "asyncio.to_thread(KiroCrewConfig.load)" in handler
        # Match the CALL form: prose explaining the write names these functions, and a
        # guard that cannot tell a mention from a call fails on a comment while a real
        # call stays invisible.
        #
        # ``_view(`` is in this list because offloading only ``_view_inputs`` was not
        # enough: ``_view`` calls ``resolve_variables``, whose per-scope
        # ``_store_layer`` reads the store on a cache miss. Rendering on the loop put
        # that read back on it, reachable whenever a concurrent PUT invalidates the
        # cache between the two calls.
        for on_loop in (
            "_view_inputs()",
            "_view(",
            "vstore.patch_store(",
            "vstore.read_store(",
            "KiroCrewConfig.load()",
        ):
            assert on_loop not in handler, f"{on_loop} runs on the event loop"

    async def test_the_unlocked_and_retired_write_paths_are_gone(self):
        """``config.json``'s writers are not this endpoint's business any more.

        Checked as module ATTRIBUTES rather than as source text: the module docstring
        explains what the move deleted and therefore names several of them.
        """
        source = inspect.getsource(vh)
        assert "write_config_atomically(" not in source
        assert "update_config_locked(" not in source, "the store's own writer owns the lock"
        for gone in (
            "_read_overlay",
            "_overlay_keys",
            "_base_pairs",
            "_WorkspaceMalformed",
            "_WorkspaceVanished",
            "_MalformedContainer",
            "config_local_path",
            "config_path",
            "update_config_locked",
        ):
            assert not hasattr(vh, gone), f"{gone} came back"

    async def test_a_corrupt_store_fails_closed_without_writing(self, wired):
        _, store = wired
        before = _seed(store, {"global": {"a": "1"}})

        def _raise(*_args, **_kwargs):
            raise vh.ConfigReadError("bad json")

        with patch.object(cfg_loader, "update_config_locked", _raise):
            resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": {"b": "2"}}))
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "config_corrupt"
        assert store.read_text(encoding="utf-8") == before

    async def test_a_malformed_stored_container_is_refused_at_every_site(self, wired):
        """Every container this write would replace refuses when it cannot read it.

        Each site used to coerce a non-mapping to {} and assign it back, destroying
        whatever the operator hand-wrote. The refusal moved into the store with the
        data — "do not replace a value whose shape you cannot interpret" is a property
        of the data, not of where it is kept — and the handler surfaces it as one code.
        """
        _, store = wired
        cases = [
            ("global", {"global": "oops"}, {"scope": "global", "set": {"q": "1"}}),
            ("global", {"global": [1, 2]}, {"scope": "global", "set": {"q": "1"}}),
            ("global", {"global": 42}, {"scope": "global", "delete": ["q"]}),
            (
                "workspaces",
                {"workspaces": "oops"},
                {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}},
            ),
            (
                "workspaces.ops",
                {"workspaces": {"ops": "oops"}},
                {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}},
            ),
            (
                "workspaces.ops",
                {"workspaces": {"ops": ["a"], "other": {"k": "v"}}},
                {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}},
            ),
        ]
        for expected_path, doc, body in cases:
            before = _seed(store, doc)
            resp = await vh.api_variables(_request("PUT", body))
            assert resp.status == 400, f"{expected_path} produced {resp.status}"
            payload = json.loads(resp.text)
            assert payload["code"] == "variables_malformed_container"
            assert expected_path in payload["error"], "the refusal must name what to repair by hand"
            # The operator's value survives byte-for-byte; there is no copy to
            # restore it from if this endpoint replaces it.
            assert store.read_text(encoding="utf-8") == before

    async def test_an_absent_container_is_created_not_refused(self, wired):
        """ABSENT is not malformed. A missing key is the legitimate first write, and
        refusing it would make the endpoint unable to store the very first variable.
        """
        _, store = wired
        assert not store.exists(), "the fixture must start from no store at all"
        resp = await vh.api_variables(_request("PUT", {"scope": "global", "set": {"q": "1"}}))
        assert resp.status == 200, "an absent store must be created"
        assert _doc(store)["global"] == {"q": "1"}

        _seed(store, {"global": {"g": "1"}})
        resp = await vh.api_variables(
            _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}})
        )
        assert resp.status == 200, "an absent workspaces container must be created"
        doc = _doc(store)
        assert doc["workspaces"] == {"ops": {"q": "1"}}
        assert doc["global"] == {"g": "1"}, "creating a container must not disturb another scope"

        _seed(store, {"workspaces": {"other": {"k": "v"}}})
        resp = await vh.api_variables(
            _request("PUT", {"scope": "workspace", "workspace": "ops", "set": {"q": "1"}})
        )
        assert resp.status == 200, "an absent per-workspace map must be created"
        assert _doc(store)["workspaces"] == {"other": {"k": "v"}, "ops": {"q": "1"}}

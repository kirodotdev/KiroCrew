"""Fixtures, request stubs and chat-slot doubles shared by the ``test_prompts*.py`` modules.

``_isolate_home`` and ``_no_turn_tail`` are autouse, so every module imports both by name.
The other fixtures are declared ``@pytest.fixture(name=...)`` and are imported under
their function names.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.handlers import api_prompts
from kiro_crew.security.credential_sources import CredentialEvidence

# ── Shared fixtures ──


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the prompt modules' autouse home isolation, moved verbatim; tracked for a de-flake pass with the one marked bare undo in test_prompts_link_gates.py
def _isolate_home(tmp_path, monkeypatch):
    """All tests get an isolated $HOME and no project dir."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.agent._project_dir", lambda: None)
    # Clear prompt cache between tests
    import kiro_crew.dashboard.handlers as h

    h._prompt_cache = None
    h._prompt_cache_ts = 0


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the prompt modules' autouse turn-tail stub, moved verbatim; tracked for a de-flake pass with the one marked bare undo in test_prompts_link_gates.py
def _no_turn_tail(monkeypatch):
    """Skip the turn's tail, which needs far more of a slot than ``_Slot`` models.

    The tail (and the done row the cycle's end appends) is pinned with a real
    slot in ``test/test_dashboard_chat_refusal.py::TestRunChatEarlyExitHandOff``; here
    only what prompt expansion writes is asserted.
    """

    async def _skip(*_args, **_kwargs):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._end_turn_tail", _skip)


@pytest.fixture(name="aim_dir")
def aim_dir_fixture(tmp_path, monkeypatch):
    """Base dir whose child package dirs are exposed via the prompt_source_roots seam.

    Each child directory becomes one edition prompt root; SOPs placed under it
    (at any depth) are discovered by ``_list_aim_prompts`` via ``rglob('*.sop.md')``
    with ``package = <root.name>``.
    """
    base = tmp_path / "prompt_pkgs"
    base.mkdir()
    from kiro_crew.platform.defaults import DefaultPromptSourceProvider

    monkeypatch.setattr(
        DefaultPromptSourceProvider,
        "prompt_source_roots",
        lambda self: [d for d in sorted(base.iterdir()) if d.is_dir()],
    )
    return base


@pytest.fixture(name="mock_sel")
def mock_sel_fixture(monkeypatch):
    """Patch sel() in both chat and handlers modules."""
    m = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: m)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: m)
    return m


@pytest.fixture(name="block_sensitive_reads")
def block_sensitive_reads_fixture(monkeypatch):
    """Refuse every path at the two gates that SERVE a prompt's bytes.

    Discovery (``handlers.is_sensitive_path``) is deliberately left alone, so an
    entry still exists and the refusal under test is the reading one: the
    ``@mention`` expander's own check and ``hooks.validate_file_path`` behind the
    unscoped detail read. Blanket-patching all three instead would make the prompt
    undiscoverable and each test would then be asserting the discovery refusal
    while claiming to assert the read one.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.is_sensitive_path", lambda p: True)
    monkeypatch.setattr("kiro_crew.hooks.is_sensitive_path", lambda p: True)


@pytest.fixture(name="block_sensitive_discovery")
def block_sensitive_discovery_fixture(monkeypatch):
    """Refuse every path at the DISCOVERY gate — the one `_prompt_dir_entry` applies."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.is_sensitive_path", lambda p: True)


# ── Helpers ──


def _aim_pkg(base, pkg_name, event_id, sops):
    """Create a package root under *base* exposing SOP files.

    ``event_id`` is retained for call-site compatibility but unused — the seam
    model has no eventId layout; SOPs are placed directly under the package root
    and found via ``rglob('*.sop.md')``.
    """
    pkg_dir = base / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for name, content in sops.items():
        (pkg_dir / f"{name}.sop.md").write_text(content)
    return pkg_dir


def _user_prompt(tmp_path, name, content="# Placeholder"):
    """Create a user prompt in ~/.kiro/prompts/.

    Written byte-faithfully (UTF-8, no newline translation): the frontmatter
    tests assert exact line endings and a BOM, and Windows' default text mode
    would rewrite ``\\n`` to ``\\r\\n`` and choke on the BOM under cp1252.
    """
    d = tmp_path / ".kiro" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8", newline="")
    return p


# The slot key every single-slot request stub binds under; the stubs send it as
# the ``X-Session-Key`` header, the header the handlers resolve "local" from.
_SLOT_KEY = "default"


def _slot_state(project=None, owner="owner-1", slots=None, slot_app=""):
    """A MagicMock DashboardState carrying real chat slots.

    ``_prompt_local_project`` reads ``state._slots`` and the named slot's
    ``.project``, so the state has to carry a real ``_slots`` dict (a bare
    ``MagicMock`` would make the resolver iterate a mock and blow up). Single-slot
    form: pass ``project`` to bind the lone ``_SLOT_KEY`` slot (empty/None → no
    project, the "no active project" path the refusal tests exercise). Multi-slot
    form: pass ``slots={key: project_or_"", ...}`` to build several named slots so
    a test can prove which one a request resolves. ``slot_app`` sets every slot's
    owning app (``_ChatSlot._app``), which the app-isolation branch compares
    against the request's own app claim.
    """
    if slots is None:
        slots = {_SLOT_KEY: str(project) if project else ""}
    slot_objs = {
        key: MagicMock(project=str(proj) if proj else "", _app=slot_app)
        for key, proj in slots.items()
    }
    state = MagicMock(_slots=slot_objs)
    state.owner_id = owner
    return state


def _claim_store(request, app=""):
    """Wire ``request.get("app")`` on a MagicMock stub.

    A bare ``MagicMock.get`` answers a truthy mock for every key, which would
    make ``_prompt_local_project`` treat a dashboard request as an app caller. The
    stubs must therefore carry a real claim store, defaulting to the
    present-and-empty claim that means "dashboard".
    """
    store = {"app": app}
    request.get = MagicMock(side_effect=lambda k, d=None: store.get(k, d))
    return request


def _list_request(project=None, session_key=_SLOT_KEY, state=None, app=""):
    """GET /api/prompts request stub. ``api_prompts`` resolves the local project
    from ``request.app["state"]`` plus the ``X-Session-Key`` header, so the stub
    needs a real ``_slots`` state and (usually) that header. ``project`` binds the
    lone slot so its local prompts are listed; pass an explicit ``state`` +
    ``session_key`` to drive a multi-slot scenario, and ``app`` for an
    app-token caller."""
    r = MagicMock()
    r.headers = {"X-Session-Key": session_key} if session_key else {}
    r.app = {"state": state if state is not None else _slot_state(project)}
    return _claim_store(r, app)


def _api_request(name, project=None, session_key=_SLOT_KEY, state=None, app=""):
    """GET /api/prompts/{name} (unscoped) request stub.

    The unscoped detail branch resolves the local project through the same
    ``_prompt_local_project`` seam the lister uses, so the stub carries a real
    ``_slots`` state and an ``X-Session-Key`` header. ``project`` binds the lone
    slot so a bare (unscoped) local prompt resolves against it.
    """
    r = MagicMock()
    r.match_info = {"name": name}
    r.headers = {"X-Session-Key": session_key} if session_key else {}
    r.app = {"state": state if state is not None else _slot_state(project)}
    return _claim_store(r, app)


class _Slot:
    """Minimal slot/state stub for prompt tests."""

    def __init__(self, project=""):
        self.messages = []
        self.key = "t"
        self.agent = "kirocrew"
        self.model = None
        self._queue = []
        self._stop_generation = 0
        self._stopping = False
        # Mirrors _ChatSlot's model-access / fallback defaults: _run_chat's
        # per-turn reset writes the one-shot flag on EVERY turn, including the
        # slash-command turns these tests drive.
        self._model_access_fallback_used = False
        self._active_fallback_model = ""
        # Mirrors _ChatSlot._chunk_seq: the per-slot chunk counter _run_chat continues.
        self._chunk_seq = 0
        # Mirrors _ChatSlot._refusal_retry_text/_refusal_fallback_attempted: the
        # dispatch gate reads both on EVERY turn to tell a refusal replay apart
        # from a genuine message, prompt turns included.
        self._refusal_retry_text = ""
        self._refusal_fallback_attempted = False
        # Mirrors _ChatSlot's per-turn redaction state: _run_chat clears both
        # at every turn start.
        self.credential_evidence = CredentialEvidence()
        self.segment_raw_text: str | None = ""
        self.segment_shown_sha = hashlib.sha256()
        self.segment_removed: dict[str, str] = {}
        self.linked_session_key = ""
        # Mirrors _ChatSlot.project: the per-slot local project @mention/​/prompts
        # resolve against. "" means no project (global prompts only), matching
        # the fail-closed default of the real slot.
        self.project = project
        # One ``meta`` per row of ``messages``, kept apart so the row tuples the
        # prompt tests index stay three wide.
        self.metas: list[dict | None] = []
        # Mirrors _ChatSlot's steer take-back state: a turn's start reads all three
        # before withdrawing a take-back offer an ended turn left behind.
        self._pending_steers: list[str] = []
        self._steer_retract = None
        self._steer_clear = None

    def append(self, role, text, cls, *, meta=None):
        self.messages.append((role, text, cls))
        self.metas.append(meta)


class _State:
    _hook_store = None
    _yolo = False

    def push_refresh(self, *a):
        pass

    def __init__(self):
        self.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
        self.sessions = type(
            "_MockSessions",
            (),
            {
                "get_slack_link": lambda self, k: ("", ""),
                "set_slack_link": lambda self, k, t, c: None,
                "get_or_create": None,
                "get_pid": lambda self, k: None,
                "set_approval_policy": lambda self, k, v: None,
                "check_context_usage": lambda self, k, c: None,
            },
        )()

    def push_slots_update(self):
        pass

    def broadcast_ws(self, *a, **kw):
        pass


def _ss():
    """Fresh state + slot pair."""
    return _State(), _Slot()


# ── Prompt authoring (create / update / delete) ──


def _create_request(
    body, app="", user="owner-1", owner="owner-1", project=None, session_key=_SLOT_KEY, state=None
):
    """POST /api/prompts request stub. ``body`` of None simulates unparseable JSON.

    ``app`` is the auth middleware's app claim: "" (default) is a dashboard
    user; a name simulates an app-token caller; None simulates the claim being
    absent (middleware did not run), which the write gate must fail closed on.
    ``user``/``owner`` shape the owner gate: the default is an owner match
    (``is_owner_dashboard_request`` requires the claim present-and-empty AND
    the caller to equal the configured owner); pass a mismatched ``user`` to
    exercise the non-owner refusal, or ``owner=""`` for the no-owner install.
    ``project`` binds the request's chat slot to a project so a ``local`` scope
    resolves against it (per-slot resolution); the default is no slot project.
    Pass an explicit ``state`` + ``session_key`` (as ``_list_request`` takes them)
    to drive a multi-slot scenario or a key that names no chat.
    """
    r = MagicMock()
    r.method = "POST"
    store = {"app": app, "user": user}
    r.get = MagicMock(side_effect=lambda k, d=None: store.get(k, d))
    r.__contains__ = lambda _self, key: key in store
    r.__getitem__ = lambda _self, key: store[key]
    # Name the slot so requesting_slot_project selects it (step 1); a "local"
    # scope resolves against THIS slot's project, the same seam create/list share.
    r.headers = {"X-Session-Key": session_key} if session_key else {}
    r.app = {"state": state if state is not None else _slot_state(project, owner)}
    if body is None:
        r.json = AsyncMock(side_effect=ValueError("no json"))
    else:
        r.json = AsyncMock(return_value=body)
    return r


def _write_request(
    method,
    name,
    scope="global",
    body=None,
    app="",
    user="owner-1",
    owner="owner-1",
    project=None,
    session_key=_SLOT_KEY,
    state=None,
):
    """PUT/DELETE /api/prompts/{name}?scope= request stub.

    ``app`` mirrors ``_create_request``: "" dashboard user, a name for an
    app-token caller, None for an absent claim (fails closed). ``user``/
    ``owner`` mirror it too: the default is an owner match. ``project`` binds
    the request's chat slot to a project so a ``local`` scope resolves against
    it (per-slot resolution); the default is no slot project. ``state`` +
    ``session_key`` mirror ``_create_request``'s, for a multi-slot scenario or a
    key that names no chat.
    """
    r = MagicMock()
    r.method = method
    store = {"app": app, "user": user}
    r.get = MagicMock(side_effect=lambda k, d=None: store.get(k, d))
    r.__contains__ = lambda _self, key: key in store
    r.__getitem__ = lambda _self, key: store[key]
    # Name the slot so requesting_slot_project selects it (step 1); a "local"
    # scope resolves against THIS slot's project, the same seam create/list share.
    r.headers = {"X-Session-Key": session_key} if session_key else {}
    r.app = {"state": state if state is not None else _slot_state(project, owner)}
    r.match_info = {"name": name}
    r.query = {"scope": scope} if scope is not None else {}
    r.json = (
        AsyncMock(return_value=body)
        if body is not None
        else AsyncMock(side_effect=ValueError("no json"))
    )
    return r


def _sha(text: str) -> str:
    """The edit base a PUT must present: sha256 of the pre-state's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Format-valid hash for PUTs whose refusal fires before the compare-and-swap
#: (missing file, confinement, unresolvable scope) — the value is never compared.
_ANY_HASH = "0" * 64


def _listed_names(project=None):
    """Names currently visible through the list endpoint.

    ``project`` binds the listing request's chat slot to a project so its
    ``source: "local"`` prompts are included (per-slot resolution)."""
    return [p["name"] for p in json.loads(asyncio.run(api_prompts(_list_request(project))).body)]


def _outcomes(mock_sel):
    return [c[1]["outcome"] for c in mock_sel.log_tool_invocation.call_args_list]

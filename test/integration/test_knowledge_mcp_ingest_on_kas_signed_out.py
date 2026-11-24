"""A document added over MCP on KAS, with Kiro CLI signed out, is extracted and found.

The Knowledge Library's extraction pool runs each chunk on the gateway's shared
KAS background runtime when KAS is the picked harness (``SessionWorker``,
``kiro_crew.knowledge.llm_pool_session``; the selection is
``llm_pool._get_acp_backend`` / ``_session_path_serves``). This test drives that
path end to end through the real MCP tools on a real in-process gateway:

* the isolated home picks ``agent.acp_backend: kas`` and turns on
  ``knowledge.auto_add_documents``;
* Kiro Crew's own vault holds a usable identity, written by the vault's own
  writer (``auth.store.TokenStore.save``), while kiro-cli is signed out by the
  same reads production uses: no ``KIRO_API_KEY`` and no identity store under
  the test ``HOME`` (``acp.kas_route.kiro_cli_signed_out`` is asserted True);
* ``KIROCREW_KIRO_BIN`` is ``kiro_cli_signed_out_launcher.py``, which serves the
  KAS relay (``acp --agent-engine v3``) through the fake backend and answers
  every kiro-cli engine start (``v2`` or no engine flag) with kiro-cli's
  signed-out text, recording each start;
* the fake backend's opt-in host credential check
  (``KIROCREW_FAKE_ACP_EXPECT_HOST_TOKEN``) asks the host for
  ``_kiro/auth/getAccessToken`` before each prompt and fails it unless the
  answer is the seeded token, and the extraction reply is scripted
  (``KIROCREW_FAKE_ACP_SCRIPT``).

``knowledge_add_document`` and then ``local_knowledge_search`` run off the event
loop, as a managed MCP server would call them, with the port, local secret and
caller block the harness's ``mcp_headers`` publishes. Asserted: the relay argv
carries ``--agent-engine v3`` and ``--auth-method client``; the extraction pool
built a ``SessionWorker``; the background runtime served the seeded token to the
engine's callback; the scripted extraction was stored; the search returns the
document with its ``**Source:**`` and ``**Link:**`` lines; and no kiro-cli engine
start was recorded from the start of the ingest to the end of the search.

What this does NOT cover: ``KIROCREW_KIRO_BIN`` is an operator binary override,
which pins the relay (``acp.kas_route`` never second-guesses it), so the spawn is
recorded as the ``kiro_cli_launcher`` route with Crew auth. Neither the switch to
Kiro Crew's own engine bundle nor the launcher-to-``kiro-cli-chat`` swap that a
real signed-out host takes is exercised here. It is not a live sign-in either:
the token never leaves the test.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from kiro_crew import mcp_core
from kiro_crew.acp import kas_route, kas_transport
from kiro_crew.acp.kas_host_auth import token_digest
from kiro_crew.auth.bridge import default_token_store
from kiro_crew.auth.store import KasToken
from kiro_crew.knowledge import llm_pool
from kiro_crew.knowledge.extractor import EXTRACTION_PROMPT
from kiro_crew.knowledge.llm_pool_session import SessionWorker
from kiro_crew.mcp_caller import CallerContext, set_current_caller
from kiro_crew.mcp_tools import knowledge as knowledge_tools
from kiro_crew.testing import fake_acp_backend
from kiro_crew.testing.scripted_replies import SCRIPT_ENV

from . import kiro_cli_signed_out_launcher as launcher
from .conftest import booted_gateway

pytestmark = pytest.mark.integration

#: The access token the vault holds and the fake demands. Synthetic: nothing
#: outside this test ever sees it.
SEEDED_TOKEN = "kas-e2e-seeded-access-token"
#: Far enough ahead that the stored token is never inside the refresh margin.
_TOKEN_EXPIRES = datetime(2099, 1, 1, tzinfo=timezone.utc)
#: A word the document carries and the search asks for.
SEARCH_WORD = "zanthoxylum"
DOC_TITLE = "Spice cabinet inventory"
DOC_SOURCE_URI = "https://docs.example.com/kitchen/spice-cabinet"
DOC_CONTENT = (
    "# Spice cabinet\n\n"
    f"The {SEARCH_WORD} peppercorns sit on the top shelf, next to the star anise.\n"
    "Restock them every spring.\n"
)
#: What the scripted model answers to every extraction prompt.
SCRIPTED_SUMMARY = "Where the peppercorns are kept and when to restock them."
SCRIPTED_EXTRACTION = {
    "title": "Peppercorn shelf",
    "entities": [
        {
            "name": "Zanthoxylum peppercorns",
            "type": "concept",
            "description": "Peppercorns kept on the top shelf.",
            "aliases": [],
        }
    ],
    "relations": [],
    "category": "personal_notes",
    "summary": SCRIPTED_SUMMARY,
}
#: The opening line of ``extractor.EXTRACTION_PROMPT``: the script answers it.
_EXTRACTION_PROMPT_OPENING = "Extract structured information from this text chunk."
#: The session the MCP calls are made on behalf of.
SESSION_KEY = "dashboard:knowledge-kas-e2e"
#: The real ``mcp_core._post``, bound at import: the test conftest replaces it with
#: a recorder for every test, and a test that drives the real transport puts it
#: back itself (``test/conftest.py``, the ``_post`` recorder's docstring).
_REAL_POST = mcp_core._post


def _merge_local_config(home: Path, patch: dict[str, dict[str, Any]]) -> None:
    """Merge *patch* into the operator override file, keeping the harness's consent."""
    path = home / "config.local.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    for section, values in patch.items():
        doc.setdefault(section, {}).update(values)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _seed_crew_sign_in() -> None:
    """Crew's own sign-in, written through the vault's own writer."""
    default_token_store().save(
        KasToken(
            access_token=SEEDED_TOKEN,
            expires_at=_TOKEN_EXPIRES,
            provider="BuilderId",
            identity="builder_id",
        )
    )


def _launches(log: Path) -> list[dict[str, Any]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _has_pair(argv: list[str], flag: str, value: str) -> bool:
    return any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))


def _as_mcp_caller(headers: dict[str, str], call: Callable[[], str]) -> str:
    """Run *call* with the caller block gatewayd injects for a pooled backend.

    Runs on a worker thread (``asyncio.to_thread`` copies the context), so the
    block is set in that thread's copy only.
    """
    set_current_caller(
        CallerContext(
            session_key=headers["X-Session-Key"],
            session_type="dashboard",
            from_gateway=True,
            session_token=headers["X-Session-Token"],
        )
    )
    try:
        return call()
    finally:
        set_current_caller(None)


@pytest.fixture
def kas_signed_out_home(
    integration_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """The harness home, on KAS, Crew signed in, kiro-cli signed out."""
    _merge_local_config(
        integration_home,
        {"agent": {"acp_backend": "kas"}, "knowledge": {"auto_add_documents": True}},
    )
    monkeypatch.delenv("KIRO_API_KEY", raising=False)
    monkeypatch.setenv("KIROCREW_KIRO_BIN", str(launcher.__file__))
    monkeypatch.setenv(launcher.LAUNCH_LOG_ENV, str(tmp_path / "kiro-launches.jsonl"))
    monkeypatch.setenv(fake_acp_backend.HOST_TOKEN_ENV, SEEDED_TOKEN)
    script = tmp_path / "extraction-script.json"
    script.write_text(
        json.dumps(
            {
                "replies": [
                    {
                        "when": _EXTRACTION_PROMPT_OPENING,
                        "text": json.dumps(SCRIPTED_EXTRACTION),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SCRIPT_ENV, str(script))
    _seed_crew_sign_in()
    return integration_home


def test_the_launcher_speaks_the_relay_spelling_production_renders():
    """The stand-in's copies of the engine flag and value are production's."""
    assert launcher.ENGINE_FLAG == kas_transport.KAS_RELAY_ENGINE_FLAG
    assert launcher.KAS_ENGINE == kas_transport.KAS_RELAY_ENGINE
    assert fake_acp_backend.HOST_TOKEN_METHOD == kas_transport.METHOD_KAS_AUTH_GET_ACCESS_TOKEN
    assert EXTRACTION_PROMPT.startswith(_EXTRACTION_PROMPT_OPENING)


@pytest.mark.asyncio
async def test_a_document_added_over_mcp_is_extracted_on_kas_and_found_with_its_source(
    kas_signed_out_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    launch_log = tmp_path / "kiro-launches.jsonl"
    built: list[Any] = []
    real_create_worker = llm_pool.LLMPool._create_worker

    async def _recording_create_worker(self: llm_pool.LLMPool) -> Any:
        worker = await real_create_worker(self)
        built.append(worker)
        return worker

    monkeypatch.setattr(llm_pool.LLMPool, "_create_worker", _recording_create_worker)

    async with booted_gateway(kas_signed_out_home) as gw:
        headers = gw.mcp_headers(SESSION_KEY)
        monkeypatch.setattr(mcp_core, "_post", _REAL_POST)
        monkeypatch.setattr(mcp_core, "_API", gw.base_url)
        monkeypatch.setattr(mcp_core, "_API_UNIX_SOCKET", "")
        monkeypatch.setattr(mcp_core, "_API_PORT", gw.port)
        monkeypatch.setattr(
            mcp_core, "read_local_secret", lambda *_a, **_kw: headers["X-Internal-Secret"]
        )

        signed_out = await asyncio.to_thread(
            kas_route.kiro_cli_signed_out,
            environ=dict(os.environ),
            home=Path(os.environ["HOME"]),
        )
        assert signed_out is True

        before_ingest = len(_launches(launch_log))
        added = await asyncio.to_thread(
            _as_mcp_caller,
            headers,
            lambda: knowledge_tools.knowledge_add_document(
                "knowledge_add_document",
                {
                    "title": DOC_TITLE,
                    "content": DOC_CONTENT,
                    "reason": "the user keeps their pantry notes here",
                    "source_uri": DOC_SOURCE_URI,
                },
            ),
        )
        print("add_document answered:", added)
        assert not added.startswith("Could not add the document"), added
        # The search caches its store in a module global: close it on every thread.
        monkeypatch.setattr(mcp_core, "_KNOWLEDGE_CACHE", None)
        try:
            found = await asyncio.to_thread(
                _as_mcp_caller,
                headers,
                lambda: knowledge_tools.local_knowledge_search(
                    "local_knowledge_search", {"query": SEARCH_WORD}
                ),
            )
        finally:
            if mcp_core._KNOWLEDGE_CACHE is not None:
                mcp_core._KNOWLEDGE_CACHE[1]._close_all_for_tests()
        window = _launches(launch_log)[before_ingest:]

        source_lines = [line for line in found.splitlines() if line.startswith("**Source:**")]
        link_lines = [line for line in found.splitlines() if line.startswith("**Link:**")]
        print("kiro-cli launches:", [e["argv"] for e in _launches(launch_log)])
        print("search source lines:", source_lines)
        print("search link lines:", link_lines)
        assert SEARCH_WORD in found, found
        assert source_lines, found
        assert link_lines == [f"**Link:** {DOC_SOURCE_URI}"], found

        # Nothing reached kiro-cli's own engine, which needs kiro-cli's sign-in.
        kiro_cli_starts = [e for e in window if e["route"] != launcher.ROUTE_KAS_RELAY]
        assert kiro_cli_starts == [], kiro_cli_starts

        # The engine's credential came from Crew's vault: the background runtime
        # answered the fake's callback, and with the seeded token.
        runtime = gw.state.sessions._bg_runtime
        assert runtime is not None
        assert runtime._kas_host_auth is True
        assert runtime._kas_served_token_digest == token_digest(SEEDED_TOKEN)

        relays = [e for e in _launches(launch_log) if e["route"] == launcher.ROUTE_KAS_RELAY]
        assert relays, _launches(launch_log)
        for entry in relays:
            assert _has_pair(entry["argv"], "--agent-engine", "v3"), entry
            assert _has_pair(entry["argv"], "--auth-method", "client"), entry

        assert built and all(isinstance(w, SessionWorker) for w in built), built

        # The scripted reply was served, so the fake accepted the token.
        store = gw.state.knowledge_store
        rows = await asyncio.to_thread(
            lambda: store.db.execute("SELECT summary FROM items").fetchall()
        )
        assert SCRIPTED_SUMMARY in [row[0] for row in rows], rows

"""Manifest and hooks contract tests.

Pins the parts of the app that only fail at runtime otherwise: the manifest
must validate against the real ``AppManifest`` rules (a rejected cron entry
is SKIPPED silently, not errored), and the health pass must resolve tag
names through the vocabulary rather than assuming ids.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew.apps.builtins.chat_status_tags import hooks, logic, settings
from kiro_crew.apps.builtins.chat_status_tags.prompts import DEFAULT_RECONCILE_PROMPT
from kiro_crew.apps.manifest import AppManifest

_APP_DIR = Path(__file__).resolve().parent.parent


def _load_manifest() -> AppManifest:
    data = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    return AppManifest.from_dict(data)


def test_manifest_validates() -> None:
    manifest = _load_manifest()
    errors = manifest.validate()
    assert errors == []


def test_manifest_shape() -> None:
    data = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    assert data["name"] == "chat-status-tags"
    assert data["defaultEnabled"] is False
    # Permission grants must be literal JSON booleans: a string "true"
    # silently DENIES the service (apps/context.py builds it as None).
    for key in ("storage", "network", "cron"):
        assert data["permissions"][key] is True
    # The reconcile cron is an LLM message cron — silent, fresh session.
    (cron,) = data["crons"]
    assert cron["silent"] is True
    assert cron["persistent_session"] is False
    assert cron["message"]
    # Backend now declares BOTH the routes entrypoint and the lifecycle hooks.
    backend = data["backend"]
    assert backend["routes"] == "backend.routes:register_routes"
    assert backend["hooks"]["on_startup"] == "hooks:on_startup"
    assert backend["hooks"]["on_shutdown"] == "hooks:on_shutdown"
    # UI page declaration for the reconcile-prompt editor.
    (page,) = data["ui"]["pages"]
    assert page["route"] == "/chat-status-tags"
    assert page["label"] == "Chat Status Tags"
    assert page["icon"] == "Tags"


def test_reconcile_prompt_does_not_mint_a_token() -> None:
    """Regression guard: the default reconcile prompt MUST NOT tell the agent to
    mint a dashboard token. `kirocrew token` is refused by the shipped
    `credential-exfil-kirocrew-token` deny rule for every agent, so a mint
    instruction would make the hourly reconciler silently no-op every run. The
    credentialed `chat_status_tags_api` MCP tool is the only path it may use.

    The prompt may still NAME the mint command in order to forbid it, so this
    guards the imperative form (the old ``run `kirocrew token` `` step and the
    token-in-URL construction it fed), not the mere co-occurrence of the words."""
    prompt = DEFAULT_RECONCILE_PROMPT.lower()
    mint = "kirocrew" + " token"  # built from parts; the literal never appears here
    # The old step (1) imperative: "run `kirocrew token --ttl 10m`".
    assert f"run `{mint}" not in prompt
    assert f"{mint} --ttl" not in prompt
    # No token-in-URL query construction survives from the old mint flow.
    assert "token=" not in prompt
    assert "{base}" not in prompt
    # The MCP tool is named as the credentialed path.
    assert "chat_status_tags_api" in DEFAULT_RECONCILE_PROMPT
    # And the prompt tells the agent plainly that a mint is refused by policy.
    assert "refused by security policy" in prompt


def test_reconcile_prompt_keeps_decision_logic() -> None:
    """The rewrite changed only the transport, not the reconciler's judgement:
    owned-PR detection via `gh`, one-way promotions, and the health-tag guard
    must all survive."""
    prompt = DEFAULT_RECONCILE_PROMPT
    assert "gh pr view" in prompt
    assert "planned < todo < implementation < review < done" in prompt
    assert "Promotions only" in prompt or "promotions only" in prompt.lower()
    # The `gh`-unavailable stop guard is retained.
    assert "produce NO output" in prompt


def test_manifest_cron_message_is_the_default_prompt() -> None:
    """The reconcile prompt reaches the agent as the cron's OWN message, so the
    manifest cron message MUST be exactly ``DEFAULT_RECONCILE_PROMPT``. Asserting
    byte-for-byte equality prevents the manifest text and the Python default from
    drifting — a drift would ship one default to a fresh install (the manifest)
    and a different one to the app page's reset (the constant)."""
    data = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    (cron,) = data["crons"]
    assert cron["message"] == DEFAULT_RECONCILE_PROMPT


def test_manifest_cron_message_has_no_file_read_bootstrap() -> None:
    """The old design told the agent to READ its instructions from a file, which
    routed operator config through an untrusted-data + approval-gated channel and
    broke on both counts in live pod runs. The message must now be instructions,
    not a fetch directive."""
    data = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    (cron,) = data["crons"]
    msg = cron["message"].lower()
    assert "reconcile-prompt.md" not in msg
    assert "read your reconcile instructions from the file" not in msg
    assert "kirocrew_home" not in msg


def test_default_prompt_has_no_file_read_or_token_mint() -> None:
    """The default prompt (== the manifest message) must not instruct the agent
    to read an instructions file, nor to mint a dashboard token (refused by the
    shipped deny floor). Its only credentialed path is the MCP tool."""
    prompt = DEFAULT_RECONCILE_PROMPT.lower()
    assert "reconcile-prompt.md" not in prompt
    assert "read your reconcile instructions from" not in prompt
    mint = "kirocrew" + " token"
    assert f"run `{mint}" not in prompt
    assert "chat_status_tags_api" in DEFAULT_RECONCILE_PROMPT


def test_skill_files_ship_with_the_app() -> None:
    skill_dir = _APP_DIR / "skills" / "self-tag-chat"
    assert (skill_dir / "SKILL.md").is_file()
    # The manifest must declare the skill so the bridge links it.
    data = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    assert data["skills"] == ["skills/self-tag-chat"]


def test_skill_is_credential_free() -> None:
    """The skill must drive the chat_status_tags_api MCP tool and NEVER mint,
    print, or handle a dashboard token. The predecessor shipped a tag.sh that
    minted one (a nested mint bypasses PreToolUse matching and `bash -x`
    exposes the token), so pin the whole skill directory to the tool-driven
    design: no scripts that reach the chat API, no token handling anywhere."""
    skill_dir = _APP_DIR / "skills" / "self-tag-chat"
    files = sorted(p.name for p in skill_dir.iterdir())
    assert files == ["SKILL.md"], f"unexpected skill payload: {files}"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    mint = "kirocrew" + " token"
    assert mint not in text
    assert "chat_status_tags_api" in text
    # Slot resolution must key on the env var this runtime actually injects.
    assert "KIROCREW_SESSION_KEY" in text
    assert "KIRO_SESSION_ID" not in text


def test_seed_vocabulary_resolves_ids_from_server() -> None:
    """Ids are server-assigned: seeding must read back what exists and only
    create what is missing."""
    client = MagicMock()
    client.list_tags.return_value = [
        {"id": "aaa", "name": "review", "status": True},
        {"id": "bbb", "name": "error", "status": False},
    ]
    created: list[tuple[str, bool]] = []

    def _create(name: str, color: str, *, status: bool) -> dict:
        created.append((name, status))
        return {"id": f"id-{name}", "name": name, "status": status}

    client.create_tag.side_effect = _create
    ids = hooks._seed_vocabulary(client)

    # Existing tags keep their server ids; missing ones are created.
    assert ids["review"] == "aaa"
    assert ids["error"] == "bbb"
    assert ids["planned"] == "id-planned"
    assert ("review", True) not in created
    status_created = {n for n, s in created if s}
    assert status_created == {"planned", "todo", "implementation", "done"}
    plain_created = {n for n, s in created if not s}
    assert plain_created == {"stuck", "network"}


def test_health_pass_tags_a_stuck_slot_and_clears_a_recovered_one() -> None:
    client = MagicMock()
    client.list_tags.return_value = [
        {"id": f"id-{n}", "name": n, "status": n in logic.STATUS_ORDER}
        for n in list(logic.STATUS_ORDER) + list(logic.HEALTH_TAGS)
    ]
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    client.list_slots.return_value = [
        # Running and silent past the threshold -> stuck.
        {"key": "s1", "running": True, "last_ts": old, "tags": ["id-review"]},
        # Idle, carries a stale health tag, latest message is healthy -> clear.
        {"key": "s2", "running": False, "last_ts": fresh, "tags": ["id-network"]},
    ]
    client.slot_messages.return_value = [{"role": "assistant", "content": "ok"}]
    client.merge_slot_tags.return_value = True

    changes = hooks._health_pass(client, stuck_min=30)

    assert len(changes) == 2
    merges = {call.args[0]: call.args[2] for call in client.merge_slot_tags.call_args_list}
    # Stuck slot: the stuck health tag is wanted (the merge preserves the
    # non-managed "id-review" status tag on the live list by construction).
    assert merges["s1"] == {"id-stuck"}
    # Recovered slot: no health tag wanted -> managed subset cleared.
    assert merges["s2"] == set()


class TestSeedOnStartup(unittest.IsolatedAsyncioTestCase):
    """on_startup seeds the reconcile-prompt file so the cron can always read it."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _run_startup(self) -> None:
        ctx = SimpleNamespace(config={}, data_dir=Path(self.tmp))
        await hooks.on_startup(ctx)
        try:
            # Let the two background tasks reach their first await, then stop them.
            await asyncio.sleep(0)
        finally:
            await hooks.on_shutdown(ctx)

    async def test_startup_seeds_prompt_when_absent(self) -> None:
        self.assertFalse(settings.prompt_path().exists())
        await self._run_startup()
        self.assertTrue(settings.prompt_path().is_file())
        self.assertEqual(
            settings.prompt_path().read_text(encoding="utf-8"), DEFAULT_RECONCILE_PROMPT
        )

    async def test_startup_does_not_clobber_existing_prompt(self) -> None:
        settings.set_prompt("operator's own instructions")
        await self._run_startup()
        self.assertEqual(settings.get_prompt(), "operator's own instructions")
        self.assertFalse(settings.is_default())

    async def test_startup_syncs_stored_prompt_into_cron_message(self) -> None:
        """A custom prompt must survive restart: on_startup pushes the stored
        prompt into the live reconcile cron's message, because the manifest
        rebuild that runs each boot would otherwise reset it to the default."""
        settings.set_prompt("operator's custom reconcile prompt")

        job = SimpleNamespace(
            id="cron-1",
            name="chat-status-tags/sdlc-tag-reconcile",
            message=DEFAULT_RECONCILE_PROMPT,  # manifest rebuild left the default
        )
        messaged: list[tuple[str, str]] = []

        class _StubSDK:
            def raise_if_store_unreadable(self) -> None:
                """Models the probe the real service exposes (healthy store)."""

            def list_jobs(self) -> list[SimpleNamespace]:
                return [job]

            async def update_job_async(self, job_id: str, **kwargs: object) -> object:
                if "message" in kwargs:
                    job.message = kwargs["message"]
                    messaged.append((job_id, str(kwargs["message"])))
                return job

        ctx = SimpleNamespace(config={}, data_dir=Path(self.tmp), cron=_StubSDK())
        await hooks.on_startup(ctx)
        try:
            await asyncio.sleep(0)
        finally:
            await hooks.on_shutdown(ctx)

        self.assertEqual(job.message, "operator's custom reconcile prompt")
        self.assertEqual(messaged, [("cron-1", "operator's custom reconcile prompt")])

    async def test_prompt_sync_probes_store_and_skips_write_when_unreadable(self) -> None:
        """An unreadable cron store loads as an EMPTY job list without raising, so
        without a probe the sync would 'find nothing to do' and report success over
        a corrupt store. This pins that the probe actually fires: a raising probe
        must prevent the write (mutation-style — the guard is proven by tripping it,
        not by its presence)."""
        settings.set_prompt("operator's custom reconcile prompt")
        wrote: list[object] = []

        class _CorruptStoreSDK:
            def raise_if_store_unreadable(self) -> None:
                raise RuntimeError("cron store unreadable")

            def list_jobs(self) -> list[SimpleNamespace]:
                return []  # exactly how a corrupt store presents

            async def update_job_async(self, job_id: str, **kwargs: object) -> object:
                wrote.append((job_id, kwargs))
                return None

        ctx = SimpleNamespace(config={}, data_dir=Path(self.tmp), cron=_CorruptStoreSDK())
        with self.assertLogs("kiro_crew.apps.builtins.chat_status_tags.hooks", "WARNING"):
            await hooks._sync_reconcile_prompt(ctx)
        self.assertEqual(wrote, [])


class TestPackageContract(unittest.TestCase):
    """The gateway loads the PACKAGE and checks ``hasattr(pkg, "register_routes")``.

    The manifest's ``backend.routes`` string is documentation of the entry point;
    dispatch happens off the package attribute (same as every other builtin). If
    this re-export is dropped, the routes silently never register and the app
    page's API 404s — exactly the failure seen in the 2026-08-31 pod test.
    """

    def test_package_re_exports_register_routes(self) -> None:
        import kiro_crew.apps.builtins.chat_status_tags as pkg

        self.assertTrue(hasattr(pkg, "register_routes"))
        self.assertTrue(callable(pkg.register_routes))

    def test_listed_in_builtin_names(self) -> None:
        """The route loop iterates BUILTIN_NAMES — absence means routes never register."""
        from kiro_crew.apps.builtins import BUILTIN_NAMES

        self.assertIn("chat_status_tags", BUILTIN_NAMES)


class TestLoopTransportIsInProcess(unittest.TestCase):
    """Regression guard for the 403-every-cycle bug (2026-08-31 pod).

    The in-gateway health/auto-resume loops must reach the chat tags/slots
    state IN-PROCESS. An earlier revision dialed the gateway's own loopback
    HTTP surface with ``X-Internal-Secret`` and 403'd on every 60 s cycle,
    because the loop read the shared local secret while a different listener
    generation owned the port it dialed. The fix deletes the HTTP hop, so the
    transport module must carry NO loopback HTTP, NO secret read, and NO port
    resolution — and ``client.py`` must be gone.
    """

    def test_store_transport_has_no_http_secret_or_port(self) -> None:
        store_src = (_APP_DIR / "store.py").read_text(encoding="utf-8")
        for forbidden in (
            "urllib",
            "loopback_urlopen",
            "read_local_secret",
            "X-Internal-Secret",
            "KIROCREW_BOUND_PORT",
            "KIROCREW_PORT",
        ):
            self.assertNotIn(
                forbidden,
                store_src,
                f"in-process transport must not reference {forbidden!r} — that "
                "is the loopback-HTTP path this fix removed",
            )

    def test_hooks_use_the_in_process_store_not_the_http_client(self) -> None:
        hooks_src = (_APP_DIR / "hooks.py").read_text(encoding="utf-8")
        self.assertIn("from kiro_crew.apps.builtins.chat_status_tags.store import", hooks_src)
        self.assertNotIn(".client import", hooks_src)
        self.assertNotIn("TagsClient", hooks_src)

    def test_the_loopback_http_client_is_deleted(self) -> None:
        self.assertFalse(
            (_APP_DIR / "client.py").exists(),
            "client.py (the loopback HTTP client) must be deleted — the loops "
            "reach state in-process now",
        )

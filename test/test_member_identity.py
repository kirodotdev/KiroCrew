"""Crew-member identity: the ``id`` / ``display_name`` split (rollout step 1).

Pins the contract of :mod:`kiro_crew.member_identity` and the three places it
lands:

* the config LOADER re-keys an ``agents`` row whose key is outside the
  member-id grammar to a minted id, keeps the typed string as ``display_name``,
  and writes that back as a delta migration (idempotent, default_agent follows);
* the CREATE route treats whatever the user typed as a display name and mints
  the id, so the create/list mismatch class of bug is structurally gone;
* the RENAME path (``PUT /api/agents/{id}`` with ``display_name``) never moves
  the key;
* ``GET /api/members`` renders the migrated member -- the step-1 GATE from the
  design doc: "Roster shows the migrated ``case-competition`` member with its
  original display name".
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from member_memory_helpers import patch_private_memory_supported

from kiro_crew import member_identity as mi
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.validation import _AGENT_NAME_RE

# The row from the incident: accepted by the create flow, invisible to the roster.
LEGACY_NAME = "case competition"
MINTED_ID = "case-competition"


# ---------------------------------------------------------------------------
# The leaf module
# ---------------------------------------------------------------------------


class TestGrammar:
    def test_id_grammar_is_the_shared_agent_name_grammar(self):
        """One grammar. The module spells it rather than importing it (cycle),
        so this pin is what keeps the two from drifting."""
        assert mi.MEMBER_ID_RE.pattern == _AGENT_NAME_RE.pattern

    @pytest.mark.parametrize("good", ["a", "Docs_Writer", "case-competition", "x" * 64])
    def test_accepts_grammar(self, good):
        assert mi.is_valid_member_id(good)

    @pytest.mark.parametrize(
        "bad", ["", " ", "case competition", "-lead", "trail-", "_", "x" * 65, None, 3]
    )
    def test_rejects_outside_grammar(self, bad):
        assert not mi.is_valid_member_id(bad)


class TestSanitizeAndMint:
    def test_valid_name_is_its_own_id(self):
        """The property every pre-split consumer relies on."""
        assert mi.sanitize_member_id("Docs_Writer") == "Docs_Writer"
        assert mi.mint_member_id("Docs_Writer", ()) == "Docs_Writer"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (LEGACY_NAME, MINTED_ID),
            ("  Café  Crew!! ", "Cafe-Crew"),
            ("-lead", "lead"),
            ("trail-", "trail"),
            ("a  b--c!", "a-b-c"),
            ("!!!", "member"),
            ("", "member"),
            ("日本語", "member"),
        ],
    )
    def test_sanitizes_into_grammar(self, raw, expected):
        out = mi.sanitize_member_id(raw)
        assert out == expected
        assert mi.is_valid_member_id(out)

    def test_long_name_is_capped_inside_grammar(self):
        out = mi.sanitize_member_id("x" * 100 + "-")
        assert len(out) <= mi.MEMBER_ID_MAX_LEN
        assert mi.is_valid_member_id(out)

    def test_collision_suffixes_deterministically(self):
        taken = {MINTED_ID, MINTED_ID + "-2"}
        assert mi.mint_member_id(LEGACY_NAME, taken) == MINTED_ID + "-3"
        assert mi.mint_member_id(LEGACY_NAME, taken) == MINTED_ID + "-3"

    def test_collision_suffix_stays_inside_length_cap(self):
        long = "x" * 64
        out = mi.mint_member_id(long, {long})
        assert out.endswith("-2")
        assert len(out) <= mi.MEMBER_ID_MAX_LEN
        assert mi.is_valid_member_id(out)


class TestDisplayName:
    def test_normalizes_whitespace(self):
        assert mi.normalize_display_name("  case   competition ") == LEGACY_NAME
        assert mi.collapse_display_name("  case   competition ") == LEGACY_NAME

    def test_over_long_is_unset_never_truncated(self):
        """A cut label is a value no scanner saw whole (a credential in its tail
        would survive, one byte short of the pattern). So: refused on write,
        unset on read, never a prefix."""
        long = "y" * (mi.DISPLAY_NAME_MAX_LEN + 1)
        assert mi.normalize_display_name(long) == ""
        assert mi.display_name_too_long(long)
        assert mi.display_name_too_long("  " + long + "  ")
        assert not mi.display_name_too_long("y" * mi.DISPLAY_NAME_MAX_LEN)
        assert (
            mi.normalize_display_name("y" * mi.DISPLAY_NAME_MAX_LEN)
            == "y" * mi.DISPLAY_NAME_MAX_LEN
        )
        # Whitespace runs collapse BEFORE the length is judged.
        padded = " ".join(["y"] * 39) + "   " + "y"  # 79 chars collapsed, 81 raw
        assert not mi.display_name_too_long(padded)
        # The whole value is what a scanner gets, not a prefix of it.
        assert mi.collapse_display_name(long) == long

    @pytest.mark.parametrize("junk", [None, 7, {"a": 1}, "   "])
    def test_non_text_reads_as_unset(self, junk):
        assert mi.normalize_display_name(junk) == ""

    def test_effective_falls_back_to_id(self):
        assert mi.effective_display_name("triage", "") == "triage"
        assert mi.effective_display_name("triage", None) == "triage"
        assert mi.effective_display_name("triage", "Oncall Triage") == "Oncall Triage"


# ---------------------------------------------------------------------------
# The loader migration
# ---------------------------------------------------------------------------


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _enroll(*names: str, generation: str = "default") -> None:
    """Give rows the crewmate ENROLLMENT record the roster reads (what a confirmed
    hire writes): a row in ``config.agents`` alone is a session agent, not a
    crewmate, so roster tests enroll the rows they expect to see."""
    from kiro_crew import agent_state

    for name in names:
        agent_state.set_crewmate_record(name, generation=generation, template="t", hired_at="")


def _legacy_config() -> dict:
    return {
        "agents": {
            "default": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            },
            LEGACY_NAME: {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
                "description": "judges the case competition",
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
    }


@pytest.fixture
def cfg_path(tmp_path: Path):
    path = tmp_path / "config.json"
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


class TestLoaderMigration:
    def test_over_long_stored_label_reads_as_the_id(self, cfg_path: Path):
        secret = "AKIAIOSFODNN7EXAMPLE"
        data = _legacy_config()
        long = "x" * (mi.DISPLAY_NAME_MAX_LEN + 1 - len(secret)) + secret
        data["agents"]["default"].update({"display_name": long, "role": long})
        _write(cfg_path, data)
        row = KiroCrewConfig.load().agents["default"]
        assert row.display_name == "" and row.role == ""

    def test_new_fields_parse_and_round_trip(self, cfg_path: Path):
        data = _legacy_config()
        data["agents"]["default"].update({"display_name": "  Kiro  ", "role": "your assistant"})
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        row = cfg.agents["default"]
        assert row.display_name == "Kiro"
        assert row.role == "your assistant"
        out = cfg.to_dict()["agents"]["default"]
        assert out["display_name"] == "Kiro"
        assert out["role"] == "your assistant"

    def test_junk_field_values_collapse_rather_than_crash(self, cfg_path: Path):
        data = _legacy_config()
        data["agents"]["default"].update({"display_name": 5, "role": None})
        _write(cfg_path, data)
        row = KiroCrewConfig.load().agents["default"]
        assert (row.display_name, row.role) == ("", "")

    def test_malformed_key_is_rekeyed_in_memory_and_on_disk(self, cfg_path: Path):
        _write(cfg_path, _legacy_config())
        cfg = KiroCrewConfig.load()
        # In memory: addressable id, typed string kept as the label, row intact.
        assert LEGACY_NAME not in cfg.agents
        row = cfg.agents[MINTED_ID]
        assert row.display_name == LEGACY_NAME
        assert row.description == "judges the case competition"
        # On disk: the delta migration moved the row and wrote the label.
        stored = _read(cfg_path)["agents"]
        assert LEGACY_NAME not in stored
        assert stored[MINTED_ID]["display_name"] == LEGACY_NAME
        assert stored[MINTED_ID]["description"] == "judges the case competition"
        # Untouched rows keep their bytes (the migration is a delta).
        assert "display_name" not in stored["default"]

    @pytest.mark.asyncio
    async def test_a_load_on_the_event_loop_defers_the_ownership_io_off_the_loop(
        self, cfg_path: Path
    ):
        """The disk half of the re-key renames private-store ownership records
        (a SQLite commit and a manifest fsync per store). ``load()`` is reached
        inline from async handlers, so on the loop thread that half must not
        run inline: it is handed to the default executor, the in-memory re-key
        stands, and the document lands from the worker."""
        import asyncio
        import threading

        from kiro_crew.config import loader

        _write(cfg_path, _legacy_config())
        loop_thread = threading.get_ident()
        ran_on: list[int] = []
        real = loader._reattribute_moved_members

        def observing(*args, **kwargs):
            ran_on.append(threading.get_ident())
            return real(*args, **kwargs)

        with patch.object(loader, "_reattribute_moved_members", observing):
            cfg = KiroCrewConfig.load()  # inline, on the running loop
            # In memory the row is already addressable by its minted id.
            assert MINTED_ID in cfg.agents and LEGACY_NAME not in cfg.agents
            # Nothing blocking ran on this thread.
            assert loop_thread not in ran_on
            # The deferred pass lands off the loop.
            for _ in range(200):
                if LEGACY_NAME not in _read(cfg_path)["agents"]:
                    break
                await asyncio.sleep(0.02)
        stored = _read(cfg_path)["agents"]
        assert LEGACY_NAME not in stored and stored[MINTED_ID]["display_name"] == LEGACY_NAME
        assert ran_on and all(t != loop_thread for t in ran_on)
        # A second load, off the loop, sees the migrated document and moves nothing.
        again = await asyncio.to_thread(KiroCrewConfig.load)
        assert set(again.agents) == {"default", MINTED_ID}

    def test_migration_is_idempotent(self, cfg_path: Path):
        _write(cfg_path, _legacy_config())
        KiroCrewConfig.load()
        first = cfg_path.read_bytes()
        cfg = KiroCrewConfig.load()
        assert cfg_path.read_bytes() == first
        assert set(cfg.agents) == {"default", MINTED_ID}

    def test_default_agent_follows_the_moved_key(self, cfg_path: Path):
        data = _legacy_config()
        data["default_agent"] = LEGACY_NAME
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        assert cfg.default_agent == MINTED_ID
        assert _read(cfg_path)["default_agent"] == MINTED_ID

    def test_collision_with_an_existing_id_suffixes_never_overwrites(self, cfg_path: Path):
        data = _legacy_config()
        data["agents"][MINTED_ID] = {"kiro_agent": "kirocrew", "description": "the real one"}
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        assert cfg.agents[MINTED_ID].description == "the real one"
        moved = cfg.agents[MINTED_ID + "-2"]
        assert moved.display_name == LEGACY_NAME
        assert moved.description == "judges the case competition"
        stored = _read(cfg_path)["agents"]
        assert stored[MINTED_ID]["description"] == "the real one"
        assert stored[MINTED_ID + "-2"]["display_name"] == LEGACY_NAME

    def test_a_prior_rename_survives_the_rekey(self, cfg_path: Path):
        data = _legacy_config()
        data["agents"][LEGACY_NAME]["display_name"] = "Judges"
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        assert cfg.agents[MINTED_ID].display_name == "Judges"

    def test_well_formed_keys_are_never_touched(self, cfg_path: Path):
        data = _legacy_config()
        del data["agents"][LEGACY_NAME]
        data["agents"]["Docs_Writer"] = {"kiro_agent": "kirocrew"}
        _write(cfg_path, data)
        before = cfg_path.read_bytes()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "Docs_Writer"}
        assert cfg_path.read_bytes() == before


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.fixture
def _owner(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    patch_private_memory_supported(monkeypatch)


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture
def seeded_cfg():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {
                "agents": {
                    "default": {
                        "kiro_agent": "kirocrew",
                        "workspace": "default",
                        "memory_store": "default",
                    }
                },
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
            },
            f,
        )
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            yield tmp
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_name(f"{tmp.name}.lock").unlink(missing_ok=True)


class TestSensitiveLegacyKeys:
    """A legacy key the redactors would alter must not become the id: the id is
    rendered verbatim on every surface that masks the display name."""

    def test_a_credential_shaped_key_mints_an_opaque_id(self, cfg_path: Path):
        from kiro_crew.member_identity import opaque_member_id

        key = (
            "AKIAIOSFODNN7EXAMPLE oncall"  # an AWS key id, plus a space to fall outside the grammar
        )
        _write(
            cfg_path,
            {
                "agents": {"default": {"kiro_agent": "kirocrew"}, key: {"kiro_agent": "kirocrew"}},
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
            },
        )
        cfg = KiroCrewConfig.load()
        assert key not in cfg.agents
        (minted,) = [k for k in cfg.agents if k != "default"]
        assert minted == opaque_member_id(key)
        assert minted.startswith("member-") and "AKIA" not in minted
        # Deterministic, inside the grammar, and the same id lands on disk.
        assert opaque_member_id(key) == minted
        assert _read(cfg_path)["agents"][minted]["legacy_key"] == key
        # The typed key is still the display name, which the read routes mask.
        assert cfg.agents[minted].display_name == key

    def test_a_grammar_valid_credential_key_is_migrated_too(self, cfg_path: Path):
        """An AWS access key id is INSIDE the member-id grammar (20 alphanumerics),
        so a grammar-only migration would leave it as the key and render it on
        every id surface. The migration decides on shape AND sensitivity: such
        a key moves to an opaque id like a malformed one, in the base document
        and in the overlay alike, and the raw value never reaches the roster."""
        from kiro_crew.member_identity import opaque_member_id

        key = "AKIAIOSFODNN7EXAMPLE"
        assert mi.is_valid_member_id(key)
        _write(
            cfg_path,
            {
                "agents": {"default": {"kiro_agent": "kirocrew"}, key: {"kiro_agent": "kirocrew"}},
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
            },
        )
        cfg = KiroCrewConfig.load()
        assert key not in cfg.agents
        (minted,) = [k for k in cfg.agents if k != "default"]
        assert minted == opaque_member_id(key) and "AKIA" not in minted
        on_disk = _read(cfg_path)["agents"]
        assert key not in on_disk
        assert on_disk[minted]["legacy_key"] == key
        assert cfg.agents[minted].display_name == key
        # Settled on the next load: nothing moves again.
        assert set(KiroCrewConfig.load().agents) == {"default", minted}

    def test_a_key_whose_sanitized_form_is_the_secret_is_opaque_too(self):
        from kiro_crew.config.loader import _rekey_base
        from kiro_crew.member_identity import opaque_member_id

        assert _rekey_base("https://evil.example/x?token=abc123 ") == opaque_member_id(
            "https://evil.example/x?token=abc123 "
        )
        # A benign malformed key still re-keys to its readable candidate.
        assert _rekey_base("case competition") == "case-competition"


class TestMembersNamed:
    """``members_named`` — the display-name fallback a stale handle resolves through."""

    ROWS = {
        "case-competition": "case competition",
        "triage": "",
        "judge-a": "Judge",
        "judge-b": "Judge",
    }

    def test_old_free_text_handle_resolves_to_the_minted_id(self):
        assert mi.members_named("case competition", self.ROWS) == ["case-competition"]

    def test_handle_is_normalized_like_a_stored_display_name(self):
        assert mi.members_named("  case   competition ", self.ROWS) == ["case-competition"]

    def test_empty_display_name_means_the_id_is_the_name(self):
        assert mi.members_named("triage", self.ROWS) == ["triage"]

    def test_match_is_exact_and_case_sensitive(self):
        assert mi.members_named("Case Competition", self.ROWS) == []
        assert mi.members_named("case", self.ROWS) == []

    def test_shared_display_name_returns_every_holder(self):
        assert mi.members_named("Judge", self.ROWS) == ["judge-a", "judge-b"]

    def test_blank_handle_matches_nothing(self):
        assert mi.members_named("", self.ROWS) == []
        assert mi.members_named("   ", self.ROWS) == []


@pytest.mark.usefixtures("_owner")
class TestCreateMintsTheId:
    @pytest.mark.asyncio
    async def test_typed_name_becomes_display_name_and_a_minted_id(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": LEGACY_NAME, "kiro_agent": "kirocrew", "role": "Judge"}
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["name"] == MINTED_ID  # the minted id: the key every route addresses
            assert data["display_name"] == LEGACY_NAME
            resp = await client.get("/api/agents")
            rows = {a["name"]: a for a in (await resp.json())["agents"]}
            assert rows[MINTED_ID]["display_name"] == LEGACY_NAME
            assert rows[MINTED_ID]["role"] == "Judge"
        stored = _read(seeded_cfg)["agents"]
        assert LEGACY_NAME not in stored
        assert stored[MINTED_ID]["display_name"] == LEGACY_NAME
        assert stored[MINTED_ID]["role"] == "Judge"

    @pytest.mark.asyncio
    async def test_display_name_field_is_the_new_spelling(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"display_name": "Oncall Triage", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["name"] == "Oncall-Triage"

    @pytest.mark.asyncio
    async def test_well_formed_name_stores_no_redundant_display_name(self, seeded_cfg: Path):
        """A plain create leaves the row byte-compatible with a pre-split one."""
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "triage", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "triage" and data["display_name"] == "triage"
        assert _read(seeded_cfg)["agents"]["triage"]["display_name"] == ""

    @pytest.mark.asyncio
    async def test_collision_on_the_minted_id_is_still_409(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "Case-Competition", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200
            resp = await client.post(
                "/api/agents", json={"name": "Case Competition", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "agent_exists"
            # The user typed a name they never saw collide: the message names
            # BOTH the typed name and the id it shortened to.
            assert "Case Competition" in data["error"]
            assert "Case-Competition" in data["error"]
            # The duplicate guard is not the client's to switch off: a stray
            # ``named_by_user: false`` in the body is not read, and the create
            # still refuses.
            resp = await client.post(
                "/api/agents",
                json={"name": "Case Competition", "kiro_agent": "kirocrew", "named_by_user": False},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_exists"
            roster = await (await client.get("/api/agents")).json()
            assert [a["name"] for a in roster["agents"] if a["name"].startswith("Case")] == [
                "Case-Competition"
            ]

    @pytest.mark.asyncio
    async def test_collision_on_the_typed_id_keeps_the_classic_message(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "default", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 409
            assert (await resp.json())["error"] == "Agent 'default' already exists"

    @pytest.mark.asyncio
    async def test_over_long_label_is_refused_not_cut(self, seeded_cfg: Path):
        """A label one byte past the cap whose tail is a credential: truncating
        would have let nineteen of its twenty characters past the scanner and
        into every roster. It is refused whole, and nothing is stored."""
        secret = "AKIAIOSFODNN7EXAMPLE"
        label = "x" * (mi.DISPLAY_NAME_MAX_LEN + 1 - len(secret)) + secret
        assert len(label) == mi.DISPLAY_NAME_MAX_LEN + 1
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post("/api/agents", json={"name": label, "kiro_agent": "kirocrew"})
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "display_name_too_long"
            assert secret[:-1] not in data["error"]
            resp = await client.post(
                "/api/agents",
                json={"name": "triage", "kiro_agent": "kirocrew", "role": label},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "role_too_long"
            resp = await client.put("/api/agents/default", json={"display_name": label})
            assert resp.status == 400
            assert (await resp.json())["code"] == "display_name_too_long"
            resp = await client.put("/api/agents/default", json={"role": label})
            assert resp.status == 400
            assert (await resp.json())["code"] == "role_too_long"
            # The same tail INSIDE the cap is caught by the credential rule.
            inside = "x" * (mi.DISPLAY_NAME_MAX_LEN - len(secret)) + secret
            resp = await client.post("/api/agents", json={"name": inside, "kiro_agent": "kirocrew"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "credential_shaped_name"
            body = await (await client.get("/api/agents")).text()
        assert secret[:-1] not in body
        assert set(_read(seeded_cfg)["agents"]) == {"default"}
        assert _read(seeded_cfg)["agents"]["default"].get("display_name", "") == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body", [{"name": 5}, {"display_name": ["x"]}, {"name": "ok", "role": 3}]
    )
    async def test_non_string_identity_fields_are_400(self, seeded_cfg: Path, body):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post("/api/agents", json={**body, "kiro_agent": "kirocrew"})
            assert resp.status == 400


@pytest.mark.usefixtures("_owner")
class TestRenameMutatesDisplayNameOnly:
    @pytest.mark.asyncio
    async def test_rename_keeps_the_key(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put("/api/agents/default", json={"display_name": "Kiro"})
            assert resp.status == 200, await resp.text()
            resp = await client.get("/api/agents")
            rows = {a["name"]: a for a in (await resp.json())["agents"]}
            assert rows["default"]["display_name"] == "Kiro"
        stored = _read(seeded_cfg)
        assert set(stored["agents"]) == {"default"}
        assert stored["agents"]["default"]["display_name"] == "Kiro"
        assert stored["default_agent"] == "default"

    @pytest.mark.asyncio
    async def test_renaming_back_to_the_id_stores_empty(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            assert (
                await client.put("/api/agents/default", json={"display_name": "Kiro"})
            ).status == 200
            assert (
                await client.put("/api/agents/default", json={"display_name": " default "})
            ).status == 200
        assert _read(seeded_cfg)["agents"]["default"]["display_name"] == ""

    @pytest.mark.asyncio
    async def test_role_updates(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put("/api/agents/default", json={"role": " your  assistant "})
            assert resp.status == 200
        assert _read(seeded_cfg)["agents"]["default"]["role"] == "your assistant"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body, code",
        [
            ({"display_name": ""}, "invalid_display_name"),
            ({"display_name": "   "}, "invalid_display_name"),
            ({"display_name": 42}, "invalid_display_name"),
            ({"display_name": "AKIAIOSFODNN7EXAMPLE"}, "credential_shaped_name"),
            ({"role": ["x"]}, "invalid_role"),
        ],
    )
    async def test_refused_labels_leave_the_row_untouched(self, seeded_cfg: Path, body, code):
        before = seeded_cfg.read_bytes()
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put("/api/agents/default", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == code
        assert seeded_cfg.read_bytes() == before


# ---------------------------------------------------------------------------
# The gate: GET /api/members renders the migrated member
# ---------------------------------------------------------------------------


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


class TestExplicitEnrollment:
    """A crewmate exists only after a confirmed hire wrote its enrollment record.
    Presence in ``config.agents`` -- a built-in, ``default``, a file or app the
    agent sync registered, a plain crew -- never puts a row on the roster."""

    def _fresh_config(self) -> dict:
        return {
            "agents": {
                "default": {"kiro_agent": "kirocrew", "memory_store": "default"},
                "kirocrew-worker": {"kiro_agent": "kirocrew-worker", "source": "builtin"},
                "release-notes-writer": {"kiro_agent": "release-notes-writer", "source": "local"},
                "oncall-pack--triage": {
                    "kiro_agent": "oncall-pack--triage",
                    "source": "package:oncall-pack",
                },
                "my-plain-crew": {
                    "kiro_agent": "reviewer",
                    "display_name": "My plain crew",
                    "memory_store": "default",
                },
            },
            "default_agent": "default",
            "workspaces": {"default": {"dir": "workspace"}},
        }

    @pytest.mark.asyncio
    async def test_fresh_state_lists_no_crewmates_however_many_templates_exist(
        self, tmp_path: Path
    ):
        """Templates of every source are available (they are rows, dispatchable
        and usable in sessions) and the roster is EMPTY -- on the first read and
        on a re-read."""
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import members as handlers

        cfg_file = tmp_path / "config.json"
        _write(cfg_file, self._fresh_config())
        state = _make_state(tmp_path)
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_members_app(state))) as client:
                for _ in range(2):
                    resp = await client.get("/api/members")
                    assert resp.status == 200
                    assert (await resp.json())["members"] == []
            cfg = KiroCrewConfig.load()
        # Every row is still there for sessions; none is a crewmate.
        assert set(cfg.agents) == set(self._fresh_config()["agents"])
        assert agent_state.all_crewmate_records() == {}
        assert handlers.enrolled_member_ids(cfg) == []

    @pytest.mark.asyncio
    async def test_a_record_for_another_generation_is_not_this_row(self, tmp_path: Path):
        """The record names the private store the hire minted; a row recreated
        under the same id with another store is not the crewmate that record
        enrolled, and a record with no row is inert."""
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import members as handlers

        cfg_file = tmp_path / "config.json"
        data = self._fresh_config()
        data["agents"]["Nia"] = {"kiro_agent": "Nia", "memory_store": "member-nia-2"}
        _write(cfg_file, data)
        _enroll("Nia", generation="member-nia-1")
        agent_state.set_crewmate_record("Gone", generation="member-gone", template="t", hired_at="")
        state = _make_state(tmp_path)
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            cfg = KiroCrewConfig.load()
            assert handlers.enrolled_member_ids(cfg) == []
            agent_state.set_crewmate_record(
                "Nia", generation="member-nia-2", template="t", hired_at=""
            )
            assert handlers.enrolled_member_ids(cfg) == ["Nia"]
            async with TestClient(TestServer(_members_app(state))) as client:
                rows = [
                    r["name"] for r in (await (await client.get("/api/members")).json())["members"]
                ]
        assert rows == ["Nia"]

    @pytest.mark.asyncio
    async def test_an_upgrade_enrolls_nothing_a_forked_private_copy_on_a_member_store_included(
        self, tmp_path: Path
    ):
        """Upgrade path: no build before this one left a mark only a hire could
        have written. A row whose ``kiro_agent`` is a private copy with lineage
        naming the row AND a ``member-`` store is exactly what the crew editor's
        fork plus private-memory provisioning give a plain crew, so it is NOT
        enrolled, and neither is any other pre-existing row. Nothing is deleted
        or rebound."""
        from kiro_crew import agent_state
        from kiro_crew.dashboard.handlers import members as handlers

        cfg_file = tmp_path / "config.json"
        data = self._fresh_config()
        data["agents"]["Nia"] = {"kiro_agent": "Nia", "memory_store": "member-nia-1"}
        data["agents"]["forked-crew"] = {"kiro_agent": "forked-crew", "memory_store": "default"}
        _write(cfg_file, data)
        agent_state.set_fork_info("Nia", "reviewer", "Nia")
        agent_state.set_fork_info("forked-crew", "reviewer", "forked-crew")
        state = _make_state(tmp_path)
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_members_app(state))) as client:
                for _ in range(2):
                    resp = await client.get("/api/members")
                    assert resp.status == 200
                    assert (await resp.json())["members"] == []
            cfg = KiroCrewConfig.load()
        assert agent_state.all_crewmate_records() == {}
        assert handlers.enrolled_member_ids(cfg) == []
        assert set(cfg.agents) >= set(self._fresh_config()["agents"]) | {"Nia", "forked-crew"}
        assert cfg.agents["Nia"].kiro_agent == "Nia"
        assert cfg.agents["Nia"].memory_store == "member-nia-1"

    @pytest.mark.asyncio
    async def test_an_unreadable_record_is_a_503_not_an_empty_roster(self, tmp_path: Path):
        """The roster GET fails closed the way every member write does: a sidecar
        that cannot be read answers 503 ``members_unavailable`` (the page renders
        its load error), never a clean empty list that reads as a fresh
        install. The record itself is left as it is for repair."""
        from kiro_crew import agent_state

        cfg_file = tmp_path / "config.json"
        data = self._fresh_config()
        data["agents"]["Nia"] = {"kiro_agent": "Nia", "memory_store": "member-nia-1"}
        _write(cfg_file, data)
        _enroll("Nia", generation="member-nia-1")
        sidecar = agent_state._state_path()
        sidecar.write_text("{not json", encoding="utf-8")
        state = _make_state(tmp_path)
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 503, await resp.text()
                body = await resp.json()
                assert body["code"] == "members_unavailable"
                assert "members" not in body
        assert sidecar.read_text(encoding="utf-8") == "{not json"

    def test_a_staged_generation_covers_the_row_on_either_store(self):
        """The two-phase store move (``stage`` before the config write, ``move``
        after): the record covers the old store and the new one in between, a
        move never finalized is finalized by the next stage, an unlanded write
        is unstaged, and a record of another generation is left alone."""
        from kiro_crew import agent_state

        agent_state.set_crewmate_record("Nia", generation="s1", template="t", hired_at="")
        assert agent_state.stage_crewmate_generation("Nia", old="other", new="s2") is False
        assert agent_state.stage_crewmate_generation("Nia", old="s1", new="s2") is True
        rec = agent_state.get_crewmate_record("Nia", strict=True)
        assert rec is not None and rec["generation"] == "s1" and rec["pending_generation"] == "s2"
        assert agent_state.record_covers_store(rec, "s1") and agent_state.record_covers_store(
            rec, "s2"
        )
        assert not agent_state.record_covers_store(rec, "s3")
        # The write did not land: unstaged, the record names s1 alone.
        assert agent_state.unstage_crewmate_generation("Nia", new="s2") is True
        assert "pending_generation" not in agent_state.get_crewmate_record("Nia", strict=True)
        # Landed but never finalized (the process died): the next change from
        # s2 owns s2 first, then stages s3 -- the row on s2 stays covered.
        assert agent_state.stage_crewmate_generation("Nia", old="s1", new="s2") is True
        assert agent_state.stage_crewmate_generation("Nia", old="s2", new="s3") is True
        rec = agent_state.get_crewmate_record("Nia", strict=True)
        assert rec["generation"] == "s2" and rec["pending_generation"] == "s3"
        assert agent_state.move_crewmate_generation("Nia", old="s2", new="s3") is True
        rec = agent_state.get_crewmate_record("Nia", strict=True)
        assert rec["generation"] == "s3" and "pending_generation" not in rec
        assert rec["template"] == "t"
        # Another row's record: neither phase touches it.
        assert agent_state.move_crewmate_generation("Nia", old="zzz", new="s9") is False
        assert agent_state.clear_crewmate_record("Nia", generation="s3") is True


class TestRosterRendersIdentity:
    @pytest.mark.asyncio
    async def test_gate_migrated_member_shows_its_original_display_name(self, tmp_path: Path):
        """Step-1 gate: the incident row is listed, under its minted id, with
        the typed string as its label -- end to end through the real loader."""
        cfg_file = tmp_path / "config.json"
        _write(cfg_file, _legacy_config())
        state = _make_state(tmp_path)
        # Enrolled under the typed key: the migration re-keys the record with
        # the row, so the hired member stays on the roster under its id.
        _enroll(LEGACY_NAME)
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
            async with TestClient(TestServer(_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                rows = {r["name"]: r for r in (await resp.json())["members"]}
        assert LEGACY_NAME not in rows
        row = rows[MINTED_ID]
        assert "id" not in row  # `name` IS the id; no second spelling of it
        assert row["slug"] == MINTED_ID
        assert row["display_name"] == LEGACY_NAME
        assert row["role"] == ""
        assert "provenance" not in row  # origin is the row's normalized `source`
        assert row["source"] == "kirocrew"

    @pytest.mark.asyncio
    async def test_rows_carry_role_and_provenance(self, tmp_path: Path):
        state = _make_state(tmp_path)
        cfg = SimpleNamespace(
            agents={
                "default": KiroCrewAgentConfig(
                    kiro_agent="kirocrew", source="builtin", role="your assistant"
                ),
                "triage": KiroCrewAgentConfig(
                    kiro_agent="triage",
                    display_name="Checkout triage",
                    role="Oncall Triage Engineer",
                ),
            },
            default_agent="default",
            memory_stores={},
            workspaces={"default": SimpleNamespace(dir="workspace")},
            default_workspace="default",
        )
        _enroll("default", "triage")
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_members_app(state))) as client:
                rows = {
                    r["name"]: r
                    for r in (await (await client.get("/api/members")).json())["members"]
                }
        assert rows["default"]["display_name"] == "default"
        assert rows["default"]["role"] == "your assistant"
        assert rows["default"]["source"] == "builtin"
        assert rows["triage"]["display_name"] == "Checkout triage"
        assert rows["triage"]["role"] == "Oncall Triage Engineer"


# ---------------------------------------------------------------------------
# The migration follows the member into its ownership records
# ---------------------------------------------------------------------------


class TestMigrationFollowsOwnership:
    """A re-keyed row must stay the OWNER of what it owned.

    Private V2 memory is attributed by member id in three places (the config
    record, the store manifest, the database meta row). Moving the key without
    moving those would list the member (the fix) while refusing its memory (a
    regression). The DM binding, the rules
    payload and a session's ``agent`` cannot exist for an out-of-grammar name
    (their writers validate it first), so they have nothing to move.
    """

    def _provision_legacy_member(self, monkeypatch, cfg_path: Path) -> str:
        from kiro_crew.memory_stores import persist_member_config, provision_member_memory

        patch_private_memory_supported(monkeypatch)
        # Created BEFORE the split, through the same provisioning the create
        # route runs: the row and its store both carry the typed name.
        _write(
            cfg_path,
            {
                "agents": {"default": {"kiro_agent": "kirocrew"}},
                "default_agent": "default",
                "workspaces": {"default": {"dir": "workspace"}},
            },
        )
        cfg = KiroCrewConfig.load()
        cfg.agents[LEGACY_NAME] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        # The loader would re-key this row on the next load; write the
        # pre-split shape by hand, exactly as an old install left it.
        store = provision_member_memory(cfg, LEGACY_NAME)
        doc = _read(cfg_path)
        doc["agents"][LEGACY_NAME] = {"kiro_agent": "kirocrew", "memory_store": store}
        doc["memory_stores"] = {store: {"owner_member": LEGACY_NAME, "memory_version": 2}}
        _write(cfg_path, doc)
        del persist_member_config  # the manual write above is the pre-split fixture
        return store

    def test_a_private_copys_lineage_follows_the_new_id(self, monkeypatch, cfg_path: Path):
        """A legacy member that had already forked its template owns a private
        copy whose sidecar lineage names the OLD key. The migration renames that
        owner in the same locked pass as the config, so the copy still reads as
        the member's own (a save, a publish or a reset on it is not refused as
        another crew's), and a third crew's lineage is untouched."""
        from kiro_crew import agent_state

        self._provision_legacy_member(monkeypatch, cfg_path)
        agent_state.set_fork_info("case-competition-copy", "reviewer", LEGACY_NAME)
        agent_state.set_fork_info("other-copy", "reviewer", "Somebody-Else")
        cfg = KiroCrewConfig.load()
        assert MINTED_ID in cfg.agents
        assert agent_state.get_fork_info("case-competition-copy") == {
            "forked_from": "reviewer",
            "private_to": MINTED_ID,
        }
        assert agent_state.get_fork_info("other-copy")["private_to"] == "Somebody-Else"
        # Idempotent: a second load (nothing left to migrate) changes nothing.
        KiroCrewConfig.load()
        assert agent_state.get_fork_info("case-competition-copy")["private_to"] == MINTED_ID

    def test_private_store_manifest_db_and_config_follow_the_new_id(self, monkeypatch, cfg_path):
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            require_member_memory_store,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        cfg = KiroCrewConfig.load()  # runs the migration + the write-back
        assert cfg.agents[MINTED_ID].memory_store == store
        # Config record.
        assert cfg.memory_stores[store].owner_member == MINTED_ID
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        # Manifest.
        manifest = json.loads(
            (memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text(encoding="utf-8")
        )
        assert manifest["owner_member"] == MINTED_ID
        # Database meta row.
        conn = sqlite3.connect(memory_stores_root() / store / MEMORY_DB_FILE)
        try:
            row = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (memory_schema.OWNER_MEMBER_META_KEY,)
            ).fetchone()
        finally:
            conn.close()
        assert row == (MINTED_ID,)
        # And the verifier that reads all three agrees: the member still OWNS it.
        assert require_member_memory_store(cfg, MINTED_ID) == store

    def test_a_store_the_overlay_selects_follows_the_key_too(self, monkeypatch, cfg_path: Path):
        """The overlay (never written back) binds the malformed member to a
        private store the base row does not name. The migration fires once, so
        that store's manifest and owner row must be renamed in the same pass or
        the member's memory is refused as an ownership mismatch for good."""
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            require_member_memory_store,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        # The base row binds `default`; only the overlay selects the private store.
        doc = _read(cfg_path)
        doc["agents"][LEGACY_NAME]["memory_store"] = "default"
        _write(cfg_path, doc)
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"agents": {LEGACY_NAME: {"memory_store": store}}}), encoding="utf-8"
        )
        cfg = KiroCrewConfig.load()
        assert cfg.agents[MINTED_ID].memory_store == store
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        manifest = json.loads((memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text())
        assert manifest["owner_member"] == MINTED_ID
        with sqlite3.connect(memory_stores_root() / store / MEMORY_DB_FILE) as conn:
            (owner,) = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (memory_schema.OWNER_MEMBER_META_KEY,)
            ).fetchone()
        assert owner == MINTED_ID
        assert require_member_memory_store(cfg, MINTED_ID) == store

    def test_a_store_record_only_the_overlay_declares_follows_the_key_too(
        self, monkeypatch, cfg_path: Path
    ):
        """The private store's config RECORD lives in config.local.json alone
        (the base has neither the record nor the binding). The overlay is never
        rewritten, so its record keeps the old owner string; the in-memory
        re-key follows it on every load, and the on-disk half must still rename
        the manifest and the database owner row in the same pass."""
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            require_member_memory_store,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        doc = _read(cfg_path)
        record = doc["memory_stores"].pop(store)
        doc["agents"][LEGACY_NAME]["memory_store"] = "default"
        _write(cfg_path, doc)
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps(
                {
                    "agents": {LEGACY_NAME: {"memory_store": store}},
                    "memory_stores": {store: record},
                }
            ),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert cfg.agents[MINTED_ID].memory_store == store
        assert cfg.memory_stores[store].owner_member == MINTED_ID
        manifest = json.loads((memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text())
        assert manifest["owner_member"] == MINTED_ID
        with sqlite3.connect(memory_stores_root() / store / MEMORY_DB_FILE) as conn:
            (owner,) = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (memory_schema.OWNER_MEMBER_META_KEY,)
            ).fetchone()
        assert owner == MINTED_ID
        assert require_member_memory_store(cfg, MINTED_ID) == store
        # The overlay is untouched; a second load agrees with the first.
        assert (
            json.loads(overlay.read_text())["memory_stores"][store]["owner_member"] == LEGACY_NAME
        )
        again = KiroCrewConfig.load()
        assert require_member_memory_store(again, MINTED_ID) == store

    def test_an_overlay_only_store_is_renamed_when_the_base_has_no_store_map(
        self, monkeypatch, cfg_path: Path
    ):
        """A base document without ``memory_stores`` at all still has stores to
        re-attribute: the overlay's record and the overlay's binding name one
        the base never mentions. Requiring a base store map skipped it, and its
        disk owner stayed legacy while its record said otherwise."""
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            require_member_memory_store,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        doc = _read(cfg_path)
        record = doc["memory_stores"].pop(store)
        del doc["memory_stores"]
        doc["agents"][LEGACY_NAME]["memory_store"] = "default"
        _write(cfg_path, doc)
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps(
                {
                    "agents": {LEGACY_NAME: {"memory_store": store}},
                    "memory_stores": {store: record},
                }
            ),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert "memory_stores" not in _read(cfg_path)
        manifest = json.loads((memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text())
        assert manifest["owner_member"] == MINTED_ID
        with sqlite3.connect(memory_stores_root() / store / MEMORY_DB_FILE) as conn:
            (owner,) = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (memory_schema.OWNER_MEMBER_META_KEY,)
            ).fetchone()
        assert owner == MINTED_ID
        assert require_member_memory_store(cfg, MINTED_ID) == store

    def test_an_overlay_only_member_keeps_its_memory_ownership(self, monkeypatch, cfg_path: Path):
        """The malformed ROW itself lives only in config.local.json (the base
        never had it). The merged view re-keys it on every load, but there is
        no base row to move, so the on-disk pass must mint the same id for it
        and rename its store's manifest and database owner itself -- a pass
        that recorded no move for it would leave the records on the old key and
        the member's memory reading as an ownership mismatch for good."""
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.config.loader import config_local_path
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            require_member_memory_store,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        doc = _read(cfg_path)
        # Move the whole row into the overlay; the base keeps only the store record.
        row = doc["agents"].pop(LEGACY_NAME)
        _write(cfg_path, doc)
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"agents": {LEGACY_NAME: row}}), encoding="utf-8")
        cfg = KiroCrewConfig.load()
        assert MINTED_ID in cfg.agents and LEGACY_NAME not in cfg.agents
        assert cfg.memory_stores[store].owner_member == MINTED_ID
        # The base record (the maps merge) follows; the overlay is untouched.
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        assert LEGACY_NAME not in _read(cfg_path)["agents"]
        assert LEGACY_NAME in json.loads(overlay.read_text())["agents"]
        manifest = json.loads((memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text())
        assert manifest["owner_member"] == MINTED_ID
        with sqlite3.connect(memory_stores_root() / store / MEMORY_DB_FILE) as conn:
            (owner,) = conn.execute(
                "SELECT value FROM memory_meta WHERE key=?", (memory_schema.OWNER_MEMBER_META_KEY,)
            ).fetchone()
        assert owner == MINTED_ID
        assert require_member_memory_store(cfg, MINTED_ID) == store
        # The id is DURABLE: the row is materialized into the base under it,
        # carrying the old key as legacy_key, so the next load reads the
        # overlay row under this id instead of minting again -- and a same-id
        # row cannot land later and shift this member to a ``-2`` while the
        # ownership records keep the first id.
        base_row = _read(cfg_path)["agents"][MINTED_ID]
        assert base_row["legacy_key"] == LEGACY_NAME
        # Identity only: the overlay's settings stay the overlay's, so removing
        # config.local.json later removes them instead of leaving copies live.
        assert set(base_row) == {"display_name", "legacy_key"}
        assert base_row["display_name"] == LEGACY_NAME
        again = KiroCrewConfig.load()
        assert MINTED_ID in again.agents and MINTED_ID + "-2" not in again.agents
        assert require_member_memory_store(again, MINTED_ID) == store
        # A save keeps the base record (the overlay's leaves are subtracted,
        # the mapping stays), and a create under the same id is a collision
        # rather than a silent shift.
        again.save()
        assert _read(cfg_path)["agents"][MINTED_ID]["legacy_key"] == LEGACY_NAME
        third = KiroCrewConfig.load()
        assert MINTED_ID in third.agents and MINTED_ID + "-2" not in third.agents
        assert require_member_memory_store(third, MINTED_ID) == store

    def test_reattribution_is_idempotent_and_leaves_a_third_owner_alone(
        self, monkeypatch, cfg_path
    ):
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            memory_stores_root,
            rename_private_owner,
        )

        store = self._provision_legacy_member(monkeypatch, cfg_path)
        KiroCrewConfig.load()
        manifest_path = memory_stores_root() / store / MEMBER_MEMORY_MANIFEST
        before = manifest_path.read_bytes()
        # Retry: nothing names the old id any more, nothing changes.
        rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        assert manifest_path.read_bytes() == before
        # Someone else's store: untouched.
        manifest_path.write_text(json.dumps({"owner_member": "other", "memory_version": 2}))
        rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        assert json.loads(manifest_path.read_text())["owner_member"] == "other"


class TestMigrationSeesTheOverlay:
    """``config.local.json`` rows are part of the merged view the in-memory half
    mints against; the on-disk half must not hand a base row an id the overlay
    already occupies (otherwise base and merged view would mint inconsistent ids)."""

    def test_base_row_never_takes_an_id_the_overlay_holds(self, cfg_path: Path):
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps(
                {"agents": {MINTED_ID: {"kiro_agent": "kirocrew", "description": "overlay"}}}
            ),
            encoding="utf-8",
        )
        _write(cfg_path, _legacy_config())
        cfg = KiroCrewConfig.load()
        # Merged view: the overlay owns `case-competition`, so the base row minted -2.
        assert cfg.agents[MINTED_ID].description == "overlay"
        assert cfg.agents[MINTED_ID + "-2"].display_name == LEGACY_NAME
        # On disk the base document agrees with the merged view.
        stored = _read(cfg_path)["agents"]
        assert MINTED_ID not in stored
        assert stored[MINTED_ID + "-2"]["display_name"] == LEGACY_NAME
        # And a second load is stable.
        again = KiroCrewConfig.load()
        assert set(again.agents) == set(cfg.agents)

    def test_a_deferred_write_retries_with_the_overlay_in_view(self, cfg_path: Path):
        """The cache hit reads no overlay. A re-key whose write was deferred must
        not be retried FROM the cache, or the on-disk half mints with an empty
        collision set and takes the id the overlay row owns."""
        from kiro_crew.config import loader
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"agents": {MINTED_ID: {"kiro_agent": "kirocrew"}}}), encoding="utf-8"
        )
        _write(cfg_path, _legacy_config())
        real = loader._persist_config_migration
        seen: list[frozenset] = []

        def deferred_once(*args, **kwargs):
            seen.append(kwargs["overlay_agent_keys"])
            if len(seen) == 1:
                return False  # a contended lock: the write is deferred
            return real(*args, **kwargs)

        with unittest.mock.patch.object(loader, "_persist_config_migration", deferred_once):
            first = KiroCrewConfig.load()
            assert MINTED_ID + "-2" in first.agents
            assert LEGACY_NAME in _read(cfg_path)["agents"]  # nothing written yet
            second = KiroCrewConfig.load()
        # The retry was a real read: it saw the overlay's key both times.
        assert seen == [(MINTED_ID,), (MINTED_ID,)]
        stored = _read(cfg_path)["agents"]
        assert MINTED_ID + "-2" in stored and MINTED_ID not in stored
        assert set(second.agents) == {"default", MINTED_ID, MINTED_ID + "-2"}

        """Base AND overlay hold ``case competition``. The overlay cannot be
        rewritten, so once the base is migrated its row must be read under the
        minted id -- not merged back as a second member minting ``-2``."""
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"agents": {LEGACY_NAME: {"model": "opus", "description": "overlay"}}}),
            encoding="utf-8",
        )
        _write(cfg_path, _legacy_config())
        first = KiroCrewConfig.load()
        assert set(first.agents) == {"default", MINTED_ID}
        assert first.agents[MINTED_ID].model == "opus"
        assert first.agents[MINTED_ID].display_name == LEGACY_NAME
        assert MINTED_ID in _read(cfg_path)["agents"]
        # Second load: the base is migrated, the overlay still says `case competition`.
        second = KiroCrewConfig.load()
        assert set(second.agents) == {"default", MINTED_ID}
        assert MINTED_ID + "-2" not in second.agents
        assert second.agents[MINTED_ID].model == "opus"
        assert second.agents[MINTED_ID].description == "overlay"
        # Nothing is pending: the document did not change on the second load.
        before = cfg_path.read_bytes()
        KiroCrewConfig.load()
        assert cfg_path.read_bytes() == before
        # save() subtracts the overlay's leaves under the id, so they do not
        # become the base row's own values.
        second.save()
        stored = _read(cfg_path)["agents"][MINTED_ID]
        assert stored.get("model", "") == ""
        assert stored.get("description", "") != "overlay"
        assert json.loads(overlay.read_text())["agents"] == {
            LEGACY_NAME: {"model": "opus", "description": "overlay"}
        }

    def test_migration_warnings_name_ids_never_the_typed_keys(self, cfg_path: Path, caplog):
        """A key is operator-typed free text that can be credential-shaped, and
        loader warnings reach the dashboard's log surface: both migration
        warnings name the minted ids only."""
        import logging

        from kiro_crew.config.loader import config_local_path

        secret = "AKIAIOSFODNN7EXAMPLE key"
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"agents": {secret: {"model": "opus"}}}), encoding="utf-8")
        data = _legacy_config()
        data["agents"][secret] = data["agents"].pop(LEGACY_NAME)
        _write(cfg_path, data)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            KiroCrewConfig.load()  # re-keys the base row
            KiroCrewConfig.load()  # reads the overlay row under the id
        text = caplog.text
        assert "re-keyed 1 crew member" in text and "keyed by a pre-migration" in text
        assert "AKIAIOSFODNN7EXAMPLE" not in text

    def test_legacy_key_survives_a_save_byte_for_byte(self, cfg_path: Path):
        """The overlay is keyed by the OLD string exactly as typed (a repeated
        space included); the row's legacy_key must round-trip through load and
        save unchanged, or the mapping breaks on the first full config write."""
        from kiro_crew.config.loader import config_local_path

        odd = "case  competition"  # two spaces: a display-name normalizer would collapse it
        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"agents": {odd: {"model": "opus"}}}), encoding="utf-8")
        data = _legacy_config()
        data["agents"][odd] = data["agents"].pop(LEGACY_NAME)
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        assert cfg.agents[MINTED_ID].legacy_key == odd
        cfg.save()
        assert _read(cfg_path)["agents"][MINTED_ID]["legacy_key"] == odd
        again = KiroCrewConfig.load()
        assert set(again.agents) == {"default", MINTED_ID}
        assert again.agents[MINTED_ID].model == "opus"

    @pytest.mark.parametrize("junk", [7, ["Judge!"], {"k": "v"}, True, ""])
    def test_a_junk_legacy_key_on_a_malformed_row_is_replaced_by_the_old_key(
        self, cfg_path: Path, junk
    ):
        """A malformed base row carrying a non-string (or empty) ``legacy_key``
        is re-keyed with ``legacy_key = <old key>``: preserved as junk, the next
        load could not follow the overlay under the old key, minted a suffixed
        twin and detached the operator's overrides."""
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"agents": {LEGACY_NAME: {"model": "opus"}}}), encoding="utf-8"
        )
        data = _legacy_config()
        data["agents"][LEGACY_NAME]["legacy_key"] = junk
        _write(cfg_path, data)
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", MINTED_ID}
        assert cfg.agents[MINTED_ID].legacy_key == LEGACY_NAME
        assert cfg.agents[MINTED_ID].model == "opus"
        assert _read(cfg_path)["agents"][MINTED_ID]["legacy_key"] == LEGACY_NAME
        again = KiroCrewConfig.load()
        assert set(again.agents) == {"default", MINTED_ID}
        assert again.agents[MINTED_ID].model == "opus"

    def test_overlay_follows_the_member_through_a_rename(self, cfg_path: Path):
        """The mapping is the row's `legacy_key`, not its display name: renaming
        the member (the most ordinary operation this feature ships) must not
        detach the overlay row into a phantom `-2`."""
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"agents": {LEGACY_NAME: {"model": "opus"}}}), encoding="utf-8"
        )
        _write(cfg_path, _legacy_config())
        KiroCrewConfig.load()
        stored = _read(cfg_path)
        assert stored["agents"][MINTED_ID]["legacy_key"] == LEGACY_NAME
        # Rename on disk, the way PUT /api/agents/{id} does.
        stored["agents"][MINTED_ID]["display_name"] = "Judging Panel"
        _write(cfg_path, stored)
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", MINTED_ID}
        assert cfg.agents[MINTED_ID].display_name == "Judging Panel"
        assert cfg.agents[MINTED_ID].model == "opus"
        assert cfg.agents[MINTED_ID].legacy_key == LEGACY_NAME
        # And save() still keeps the override out of the base file.
        cfg.save()
        assert _read(cfg_path)["agents"][MINTED_ID].get("model", "") == ""

    def test_overlay_default_agent_by_the_old_key_follows_too(self, cfg_path: Path):
        from kiro_crew.config.loader import config_local_path

        overlay = config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"default_agent": LEGACY_NAME}), encoding="utf-8")
        _write(cfg_path, _legacy_config())
        first = KiroCrewConfig.load()
        assert first.default_agent == MINTED_ID
        second = KiroCrewConfig.load()
        assert second.default_agent == MINTED_ID
        assert set(second.agents) == {"default", MINTED_ID}

    def test_overlay_row_stays_put_without_a_single_legacy_key_holder(self, cfg_path: Path):
        from kiro_crew.config.loader import _follow_migrated_member_ids_in_overlay

        base = {
            "judge-a": {"legacy_key": "Judge!"},
            "judge-b": {"legacy_key": "Judge!"},
            "case-competition": {"legacy_key": LEGACY_NAME, "display_name": "Renamed"},
            "plain": {"display_name": "team lead"},
        }
        local = {
            "agents": {
                "Judge!": {"model": "x"},  # two holders: ambiguous, left alone
                LEGACY_NAME: {"model": "y"},  # one holder: follows, rename or not
                "team lead": {"model": "z"},  # display name is NOT a mapping
                "ok": {},
            }
        }
        moved = _follow_migrated_member_ids_in_overlay(local, base)
        assert moved == {LEGACY_NAME: "case-competition"}
        assert set(local["agents"]) == {"Judge!", "case-competition", "team lead", "ok"}
        # A key the base still holds is left for the merge + re-key pass.
        local = {"agents": {LEGACY_NAME: {"model": "y"}}}
        assert _follow_migrated_member_ids_in_overlay(local, {LEGACY_NAME: {}, **base}) == {}
        assert set(local["agents"]) == {LEGACY_NAME}
        # Garbage in either document is a no-op, not a crash.
        assert _follow_migrated_member_ids_in_overlay(["x"], base) == {}
        assert _follow_migrated_member_ids_in_overlay({"agents": 3}, base) == {}
        assert _follow_migrated_member_ids_in_overlay(local, None) == {}


class TestRosterIdentityIsRedacted:
    """The wrapper's free-text fields ship through the same redactors as the
    roster's preview -- a hand-edited config must not turn the members list
    into a credential read path."""

    @pytest.mark.asyncio
    async def test_credential_shaped_identity_values_are_redacted(self, tmp_path: Path):
        secret = "AKIAIOSFODNN7EXAMPLE"
        state = _make_state(tmp_path)
        cfg = SimpleNamespace(
            agents={
                "triage": KiroCrewAgentConfig(
                    kiro_agent="triage",
                    display_name=f"call {secret}",
                    role=f"role {secret}",
                ),
            },
            default_agent="triage",
            memory_stores={},
            workspaces={"default": SimpleNamespace(dir="workspace")},
            default_workspace="default",
        )
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_members_app(state))) as client:
                body = await (await client.get("/api/members")).text()
        assert secret not in body

    @pytest.mark.asyncio
    async def test_non_string_identity_values_render_as_empty(self, tmp_path: Path):
        state = _make_state(tmp_path)
        row = KiroCrewAgentConfig(kiro_agent="triage")
        object.__setattr__(row, "role", {"nested": "x"})
        cfg = SimpleNamespace(
            agents={"triage": row},
            default_agent="triage",
            memory_stores={},
            workspaces={"default": SimpleNamespace(dir="workspace")},
            default_workspace="default",
        )
        _enroll("triage")
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=cfg):
            async with TestClient(TestServer(_members_app(state))) as client:
                rows = {
                    r["name"]: r
                    for r in (await (await client.get("/api/members")).json())["members"]
                }
        assert rows["triage"]["role"] == ""
        assert "nested" not in json.dumps(rows)


@pytest.mark.usefixtures("_owner")
class TestRoleIsRefusedWhenCredentialShaped:
    @pytest.mark.asyncio
    async def test_create_and_rename_refuse_a_credential_shaped_role(self, seeded_cfg: Path):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "triage", "kiro_agent": "kirocrew", "role": "AKIAIOSFODNN7EXAMPLE"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "credential_shaped_role"
            resp = await client.put("/api/agents/default", json={"role": "AKIAIOSFODNN7EXAMPLE"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "credential_shaped_role"
        assert set(_read(seeded_cfg)["agents"]) == {"default"}
        assert _read(seeded_cfg)["agents"]["default"].get("role", "") == ""


class TestMigrationOrderingAndFailures:
    """The ownership rename runs INSIDE the locked pass, before the config write,
    on the mapping that pass minted; a failure aborts the write."""

    def _legacy_with_store(self, monkeypatch, cfg_path: Path) -> str:
        return TestMigrationFollowsOwnership()._provision_legacy_member(monkeypatch, cfg_path)

    def test_a_crash_between_the_write_and_the_renames_cannot_strand_the_store(
        self, monkeypatch, cfg_path: Path
    ):
        """The window the old order left open: ownership renamed, config not yet
        written, and a same-stem row claiming the minted id before the retry,
        which then minted a suffixed id while the records kept the first. With
        the id written FIRST, a pass that dies right after the write leaves the
        id claimed in the document and the pair in the marker; a competing row
        cannot take that id, and the next load replays the rename onto the
        very id the document holds."""
        from kiro_crew.config import loader
        from kiro_crew.memory_stores import require_member_memory_store

        store = self._legacy_with_store(monkeypatch, cfg_path)

        def die(*args, **kwargs):
            raise RuntimeError("process died")

        with patch.object(loader, "_reattribute_moved_members", die):
            KiroCrewConfig.load()
        stored = _read(cfg_path)
        assert stored["agents"][MINTED_ID]["legacy_key"] == LEGACY_NAME
        assert loader._read_member_moves_marker() == {LEGACY_NAME: MINTED_ID}
        # The id is taken in the document: a concurrent "config set" of a row
        # under it is a collision on an existing key, not a fresh claim.
        assert MINTED_ID in stored["agents"]
        # The next load replays the rename onto THAT id and clears the marker.
        cfg = KiroCrewConfig.load()
        assert require_member_memory_store(cfg, MINTED_ID) == store
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        assert loader._read_member_moves_marker() == {}

    def test_a_finished_pass_drops_only_its_own_pairs(self, monkeypatch, cfg_path: Path):
        """Two passes can overlap (another gateway, or a replay in another
        process). A pass that finished ITS renames removes exactly the pairs it
        renamed, each only while the record still holds the id it renamed to --
        never the whole record -- so a pair another pass recorded in between,
        whose renames may still be pending, survives for that pass's replay. An
        unvouched pair is dropped only by a LATER pass, under the config lock,
        where the document is authoritative."""
        from kiro_crew import agent_state
        from kiro_crew.config import loader
        from kiro_crew.memory_stores import require_member_memory_store

        store = self._legacy_with_store(monkeypatch, cfg_path)
        real = loader.__dict__["_reattribute_moved_members"]
        seen: list[dict[str, str]] = []

        def another_pass_records_then_rename(*args, **kwargs):
            # After this pass's locked write, before its renames finish: a pass
            # in another process records a pair of its own.
            seen.append(loader._read_member_moves_marker())
            agent_state.set_pending_member_moves({"Other Legacy": "other-legacy"})
            return real(*args, **kwargs)

        with patch.object(loader, "_reattribute_moved_members", another_pass_records_then_rename):
            cfg = KiroCrewConfig.load()
        assert seen == [{LEGACY_NAME: MINTED_ID}]
        assert require_member_memory_store(cfg, MINTED_ID) == store
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        # This pass's pair is gone; the other pass's pair is untouched.
        assert loader._read_member_moves_marker() == {"Other Legacy": "other-legacy"}
        # The pair-specific discard is also value-checked: a pair whose stored id
        # moved on (re-minted by another pass) is not this pass's to drop.
        assert agent_state.discard_pending_member_moves({"Other Legacy": "someone-else"}) == 0
        assert loader._read_member_moves_marker() == {"Other Legacy": "other-legacy"}
        # The NEXT locked pass sees the document does not vouch for it (that
        # write never landed) and drops it there, under the lock.
        renamed: list = []
        with patch.object(loader, "_reattribute_moved_members", lambda *a, **k: renamed.append(a)):
            KiroCrewConfig.load()
        assert renamed == []
        assert loader._read_member_moves_marker() == {}

    @pytest.mark.asyncio
    async def test_a_load_on_the_event_loop_probes_the_pending_record_off_the_loop(
        self, cfg_path: Path
    ):
        """The pending-moves record is a sidecar read of up to 8 MiB. A load on
        the loop thread with no malformed key of its own must not read it
        inline: the probe goes to the executor, which replays a pair the record
        holds (marker cleared off the loop) and, for an empty record, writes
        nothing and leaves the cache alone."""
        import asyncio
        import threading

        from kiro_crew import agent_state
        from kiro_crew.config import loader

        _write(
            cfg_path,
            {"agents": {"default": {"kiro_agent": "kirocrew"}}, "default_agent": "default"},
        )
        loop_thread = threading.get_ident()
        read_on: list[int] = []
        real_read = agent_state.get_pending_member_moves

        def observing():
            read_on.append(threading.get_ident())
            return real_read()

        # An empty record: probed off the loop, no pass, cache untouched.
        with patch.object(agent_state, "get_pending_member_moves", observing):
            KiroCrewConfig.load()
            for _ in range(100):
                if read_on:
                    break
                await asyncio.sleep(0.02)
        assert read_on and loop_thread not in read_on
        assert loader._read_member_moves_marker() == {}
        # A record holding a pair: still never read on the loop; the executor
        # pass replays what the document vouches for and clears the record.
        loader._write_member_moves_marker({"ghost member": "default"})
        read_on.clear()
        loader._invalidate_config_cache()
        with patch.object(agent_state, "get_pending_member_moves", observing):
            KiroCrewConfig.load()
            for _ in range(200):
                # Polled through the UNPATCHED reader: the test's own reads are
                # not the loader's.
                if real_read() == {} and read_on:
                    break
                await asyncio.sleep(0.02)
        assert read_on and loop_thread not in read_on
        assert loader._read_member_moves_marker() == {}
        # Off the loop the probe is inline, as before.
        loader._write_member_moves_marker({"ghost member": "default"})
        read_on.clear()
        loader._invalidate_config_cache()
        with patch.object(agent_state, "get_pending_member_moves", observing):
            await asyncio.to_thread(KiroCrewConfig.load)
        assert read_on and loop_thread not in read_on
        assert loader._read_member_moves_marker() == {}

    def test_a_marker_pair_the_document_does_not_vouch_for_renames_nothing(
        self, monkeypatch, cfg_path: Path
    ):
        """A stale or forged marker (a mint that never landed) is inert: only a
        pair whose ``agents[new].legacy_key == old`` is replayed; the rest is
        dropped with the marker."""
        from kiro_crew.config import loader

        _write(
            cfg_path,
            {"agents": {"default": {"kiro_agent": "kirocrew"}}, "default_agent": "default"},
        )
        loader._write_member_moves_marker({"ghost member": "default"})
        assert loader._read_member_moves_marker() == {"ghost member": "default"}
        # The record lives in the SEALED agent-state sidecar (read-only to
        # sandboxed agents, refused to the agent file tools), under a key outside
        # the agent-name grammar -- gateway-only state, like the fork lineage
        # whose ownership it moves; no per-agent reader sees it as an agent.
        from kiro_crew import agent_state

        stored = json.loads(agent_state._state_path().read_text(encoding="utf-8"))
        assert stored[loader.MEMBER_ID_MOVES_MARKER] == {"moves": {"ghost member": "default"}}
        assert agent_state.all_fork_info() == {}
        renamed: list = []
        with patch.object(loader, "_reattribute_moved_members", lambda *a, **k: renamed.append(a)):
            KiroCrewConfig.load()
        assert renamed == []
        assert loader._read_member_moves_marker() == {}

    def test_a_locked_database_lands_the_id_and_the_next_load_replays_the_rename(
        self, monkeypatch, cfg_path: Path
    ):
        """Marker -> config write -> ownership renames -> marker gone. A database
        a live session holds refuses the rename (no lock wait); by then the
        minted id is on disk with its ``legacy_key`` -- nobody can claim it --
        and the marker keeps the pair, so the next load replays exactly that
        rename instead of minting anew."""
        import sqlite3

        from kiro_crew.config import loader
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
        )

        store = self._legacy_with_store(monkeypatch, cfg_path)
        db = memory_stores_root() / store / MEMORY_DB_FILE
        # Hold an exclusive write lock across the load: the rename must fail
        # at once (no lock wait), and the FAILURE must abort the config write.
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            cfg = KiroCrewConfig.load()
            # In memory the row is re-keyed, and the DOCUMENT already holds the
            # minted id (the durable half landed first)...
            assert MINTED_ID in cfg.agents
            stored = _read(cfg_path)["agents"]
            assert LEGACY_NAME not in stored and stored[MINTED_ID]["legacy_key"] == LEGACY_NAME
            # ...while the ownership records stayed whole under the OLD owner
            # (the row is renamed before the manifest, and the locked database
            # refused at once), and the marker names the pair still to replay.
            manifest = json.loads(
                (memory_stores_root() / store / MEMBER_MEMORY_MANIFEST).read_text(encoding="utf-8")
            )
            assert manifest["owner_member"] == LEGACY_NAME
            assert loader._read_member_moves_marker() == {LEGACY_NAME: MINTED_ID}
            # A same-stem row claiming the minted id is now impossible: the id is
            # taken in the document, so a create would collide rather than shift
            # the migration's suffix away from the records.
            assert MINTED_ID in stored
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        # Released: the next load replays the rename from the marker and clears it.
        cfg = KiroCrewConfig.load()
        assert loader._read_member_moves_marker() == {}
        assert _read(cfg_path)["agents"][MINTED_ID]["display_name"] == LEGACY_NAME
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID
        from kiro_crew.memory_stores import require_member_memory_store

        assert require_member_memory_store(cfg, MINTED_ID) == store

    def test_a_failed_manifest_write_puts_the_database_row_back(self, monkeypatch, cfg_path: Path):
        """The database is renamed first; if the manifest then cannot be
        written, the row is reverted so neither record moved."""
        import sqlite3

        from kiro_crew import memory_schema
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            rename_private_owner,
        )

        store = self._legacy_with_store(monkeypatch, cfg_path)
        root = memory_stores_root() / store

        def owner() -> str:
            with sqlite3.connect(root / MEMORY_DB_FILE) as conn:
                (value,) = conn.execute(
                    "SELECT value FROM memory_meta WHERE key=?",
                    (memory_schema.OWNER_MEMBER_META_KEY,),
                ).fetchone()
            return value

        assert owner() == LEGACY_NAME
        import kiro_crew.atomic_write as atomic_write_module

        def refuse(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic_write_module, "atomic_write", refuse)
        with pytest.raises(OSError):
            rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        assert owner() == LEGACY_NAME
        manifest = json.loads((root / MEMBER_MEMORY_MANIFEST).read_text(encoding="utf-8"))
        assert manifest["owner_member"] == LEGACY_NAME
        monkeypatch.undo()
        rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        assert owner() == MINTED_ID
        manifest = json.loads((root / MEMBER_MEMORY_MANIFEST).read_text(encoding="utf-8"))
        assert manifest["owner_member"] == MINTED_ID

    def test_a_store_being_replaced_defers_the_rename_with_nothing_touched(
        self, monkeypatch, cfg_path: Path
    ):
        """A snapshot restore holds the store's lifetime lock exclusively while
        it swaps the directory. The rename takes that lock shared for both of
        its writes, never waits for it, and a replace in progress refuses before
        either record moved; the document already holds the minted id and the
        marker keeps the pair, so the next load replays the rename."""
        import sqlite3

        from kiro_crew import memory_schema, platform_compat
        from kiro_crew.member_memory_backup import hold_stores_for_replace
        from kiro_crew.memory_stores import (
            MEMBER_MEMORY_MANIFEST,
            MEMORY_DB_FILE,
            memory_stores_root,
            rename_private_owner,
        )

        if not platform_compat.IS_POSIX:
            pytest.skip("the lifetime lock is POSIX-only; Windows denies the swap itself")
        store = self._legacy_with_store(monkeypatch, cfg_path)
        root = memory_stores_root() / store

        def owner() -> str:
            with sqlite3.connect(root / MEMORY_DB_FILE) as conn:
                (value,) = conn.execute(
                    "SELECT value FROM memory_meta WHERE key=?",
                    (memory_schema.OWNER_MEMBER_META_KEY,),
                ).fetchone()
            return value

        with hold_stores_for_replace(memory_stores_root(), [store]):
            with pytest.raises(OSError, match="being replaced"):
                rename_private_owner(store, LEGACY_NAME, MINTED_ID)
            assert owner() == LEGACY_NAME
            manifest = json.loads((root / MEMBER_MEMORY_MANIFEST).read_text(encoding="utf-8"))
            assert manifest["owner_member"] == LEGACY_NAME
            # Through the loader: the id lands, the records wait for the replay.
            from kiro_crew.config import loader

            cfg = KiroCrewConfig.load()
            assert MINTED_ID in cfg.agents
            assert LEGACY_NAME not in _read(cfg_path)["agents"]
            assert owner() == LEGACY_NAME
            assert loader._read_member_moves_marker() == {LEGACY_NAME: MINTED_ID}
        # Released: the whole migration lands on the next load.
        KiroCrewConfig.load()
        assert owner() == MINTED_ID
        assert _read(cfg_path)["memory_stores"][store]["owner_member"] == MINTED_ID

    def test_the_manifest_is_read_with_the_lifetime_lock_already_held(
        self, monkeypatch, cfg_path: Path
    ):
        """Read before the lock, a replace landing between the read and the
        acquisition would have the pass write a stale owner over the directory
        the restore just put there. The read happens inside the hold."""
        import os

        from kiro_crew import memory_stores, platform_compat
        from kiro_crew.member_memory_backup import _open_store_use_lock
        from kiro_crew.memory_stores import MEMORY_DB_FILE, memory_stores_root

        if not platform_compat.IS_POSIX:
            pytest.skip("the lifetime lock is POSIX-only")
        store = self._legacy_with_store(monkeypatch, cfg_path)
        real_read = memory_stores._member_manifest
        exclusive_available: list[bool] = []

        def read_and_probe(name):
            # An exclusive acquisition must FAIL here: the rename holds it shared.
            fd = _open_store_use_lock(memory_stores_root() / store / MEMORY_DB_FILE)
            try:
                taken = platform_compat.try_acquire_lock(fd, exclusive=True)
                if taken:
                    platform_compat.release_lock(fd)
                exclusive_available.append(taken)
            finally:
                os.close(fd)
            return real_read(name)

        monkeypatch.setattr(memory_stores, "_member_manifest", read_and_probe)
        memory_stores.rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        assert exclusive_available == [False]

    def test_only_a_missing_meta_table_is_tolerated(self, monkeypatch, cfg_path: Path):
        import sqlite3

        from kiro_crew.memory_stores import MEMORY_DB_FILE, memory_stores_root, rename_private_owner

        store = self._legacy_with_store(monkeypatch, cfg_path)
        db = memory_stores_root() / store / MEMORY_DB_FILE
        # A read-only database: the UPDATE fails with an OperationalError that is
        # NOT "no such table", so it must propagate rather than be swallowed.
        db.chmod(0o444)
        try:
            with pytest.raises(sqlite3.OperationalError):
                rename_private_owner(store, LEGACY_NAME, MINTED_ID)
        finally:
            db.chmod(0o600)

    def test_in_memory_follows_the_id_the_locked_pass_minted(self, monkeypatch, cfg_path: Path):
        """A writer that takes the candidate id between this load's read and the
        locked write: the document mints `-2`, and the parsed config must serve
        `-2` too -- never an id the disk does not hold."""
        from kiro_crew.config import loader as loader_mod

        _write(cfg_path, _legacy_config())
        real = loader_mod._apply_document_migrations

        def racing(data, pending, **kw):
            # Simulate the concurrent create landing before the locked pass ran.
            data["agents"].setdefault(MINTED_ID, {"kiro_agent": "kirocrew", "description": "racer"})
            return real(data, pending, **kw)

        monkeypatch.setattr(loader_mod, "_apply_document_migrations", racing)
        cfg = KiroCrewConfig.load()
        stored = _read(cfg_path)["agents"]
        assert stored[MINTED_ID]["description"] == "racer"
        assert stored[MINTED_ID + "-2"]["display_name"] == LEGACY_NAME
        # The in-memory view agrees with the document on the re-keyed row. (The
        # racer itself is not in this load's snapshot -- a concurrent write is
        # picked up by the next load, as for any other field.)
        assert cfg.agents[MINTED_ID + "-2"].display_name == LEGACY_NAME
        assert MINTED_ID not in cfg.agents

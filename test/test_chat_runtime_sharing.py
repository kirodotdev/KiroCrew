"""Chat runtime sharing: who may share, which process, and the cap that bounds it.

The lease table itself is ``kiro_crew.runtime_ownership`` and is tested in
``test_runtime_ownership.py``. What is tested HERE is the three things sharing
adds on top of it: the eligibility rule, the compatibility key, and the cap that
turns a table of one-lease entries into a table of shared ones -- plus the two
places where a JOINING session must behave differently from a founding one.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from chat_runtime_sharing_helpers import confirming, make_placement

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp.chat_runtime_sharing import (
    ChatRuntimeKey,
    chat_runtime_cap,
    chat_sharing_ineligible_reason,
    eligible_for_chat_sharing,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_CHAT_RUNTIME_SHARING,
    ACP_BACKENDS_SESSION_SHARING,
)
from kiro_crew.runtime_ownership import RuntimeOwnership, authorize_runtime_kill


class FakeRuntime:
    """Stands in for ``AcpRuntime``: the table only probes ``is_alive``/``pid``."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def die(self) -> None:
        self._alive = False


def spawner(*runtimes: FakeRuntime):
    """An ``acquire`` spawn callback handing back *runtimes* in order."""
    pending = list(runtimes)
    calls: list[FakeRuntime] = []

    async def spawn() -> FakeRuntime:
        rt = pending.pop(0)
        calls.append(rt)
        return rt

    spawn.calls = calls  # type: ignore[attr-defined]
    return spawn


def a_key(**overrides) -> ChatRuntimeKey:
    base = dict(
        work_dir="/home/u/.kirocrew/workspace",
        agent="kirocrew",
        sandbox_mode="auto",
        extra_env={"A": "1", "B": "2"},
        acp_backend="kiro",
        tool_search=None,
        memory_mode="persistent",
        shared_scratch=None,
        mcp_gateway_overlay=None,
        mcp_gateway_socket=None,
    )
    base.update(overrides)
    return ChatRuntimeKey.build(**base)


# ── Who may share ──


class TestChatSharingEligibility:
    """Only a dashboard chat slot in persistent memory mode may share."""

    def test_dashboard_chat_slot_is_eligible(self):
        assert eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )

    def test_bare_chat_slot_key_is_eligible(self):
        assert eligible_for_chat_sharing(
            session_key="chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_non_persistent_never_shares(self, mode):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode=mode,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )

    def test_member_session_is_refused_and_keeps_its_own_process(self):
        """A crew-member session is refused chat sharing and keeps its own
        process. Member memory and workspace are isolated per member, so sharing
        one runtime across member sessions is a boundary this change does not
        cross -- the safe default wins. A non-member dashboard slot still shares.
        """
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=True,
        )
        assert eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )

    def test_flag_off_disables_sharing(self):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=False,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )

    def test_kas_shares_only_on_an_engine_that_admits_one_turn_per_session(self):
        """KAS is a member, but a KAS start shares only when its engine is ready.

        ``kas_engine_ready`` is ``kas_chat_sharing_ready()``: the kiro-cli relay
        at or above 3.1.0, whose engine admits one turn per session itself. An
        older engine, or Crew's own pinned bundle, keeps one process per chat.
        """
        assert ACP_BACKEND_KAS in ACP_BACKENDS_CHAT_RUNTIME_SHARING
        common = dict(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KAS,
            member_context=False,
        )
        assert chat_sharing_ineligible_reason(**common) == "kas_engine_not_shareable"
        assert eligible_for_chat_sharing(**common, kas_engine_ready=True)
        # kiro-cli does not ask the KAS readiness at all.
        assert eligible_for_chat_sharing(**{**common, "backend": ACP_BACKEND_KIRO})

    @pytest.mark.parametrize(
        "version,bundle,ready",
        [
            ((3, 1, 0), False, True),
            ((3, 2, 4), False, True),
            ((3, 0, 0), False, False),
            ((2, 37, 1), False, False),
            (None, False, False),
            ((3, 1, 0), True, False),
        ],
    )
    def test_kas_readiness_reads_the_relay_version_and_route(
        self, monkeypatch, version, bundle, ready
    ):
        import kiro_crew.acp.kas_route as kas_route
        import kiro_crew.kiro_cli as kiro_cli
        from kiro_crew.acp.chat_runtime_sharing import kas_chat_sharing_ready

        monkeypatch.setattr(kiro_cli, "installed_kiro_cli_version", lambda: version)
        monkeypatch.setattr(
            kas_route, "resolve_kas_route", lambda: kas_route.KasRoute(bundle=bundle, reason="")
        )
        assert kas_chat_sharing_ready() is ready

    def test_a_session_environment_keeps_its_own_process(self):
        """A remote workspace or cloud sandbox is attached to the PROCESS."""
        assert (
            chat_sharing_ineligible_reason(
                session_key="dashboard:chat-12-1790000000",
                memory_mode="persistent",
                sharing_enabled=True,
                backend=ACP_BACKEND_KIRO,
                member_context=False,
                remote_workspace=True,
            )
            == "session_environment"
        )

    def test_chat_sharing_is_not_inherited_from_subagent_sharing(self):
        """H6: sharing SUBAGENT sessions on a process does not grant a top-level
        chat slot the right to share one -- the two teardowns differ, and it is
        the chat teardown the chat set asserts is safe.

        codex is in the subagent ``ACP_BACKENDS_SESSION_SHARING`` but NOT in
        ``ACP_BACKENDS_CHAT_RUNTIME_SHARING``: its chat teardown's
        resume-preservation is unmeasured, so a shared chat session on it could
        lose its resume record on an ordinary close. It must therefore keep a
        dedicated process, and the eligibility gate must refuse it even with every
        other condition satisfied.
        """
        assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_SHARING
        assert ACP_BACKEND_CODEX not in ACP_BACKENDS_CHAT_RUNTIME_SHARING
        # Neither set is derived from the other: KAS shares chats (its chat
        # teardown evicts with ``session/close``) and not subagents.
        assert ACP_BACKENDS_CHAT_RUNTIME_SHARING != ACP_BACKENDS_SESSION_SHARING
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_CODEX,
            member_context=False,
        )

    @pytest.mark.parametrize(
        "key",
        [
            "cron:nightly-digest",
            "hook:pre-commit",
            "task:runner-7",
            "subagent:abc123",
            "telegram:kirocrew:direct:8743158320",
            None,
            "",
        ],
    )
    def test_non_chat_origins_keep_their_own_runtime(self, key):
        assert not eligible_for_chat_sharing(
            session_key=key,
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
            member_context=False,
        )


class TestIsSharedChatRuntimeTellsTheSharedPathApart:
    """``is_shared_chat_runtime`` is the guard the ownerless ``_run_chat`` gates use.

    The ``/clear`` and ``/agent`` ownerless gates fire ONLY on a chat-SHARED
    runtime, because only one fans a frame to a peer. A founder marks its own
    frame ownerless whenever a subagent is registered -- true on the shipped
    default (chat sharing OFF) too -- so the ownerless flag alone would change
    the default path. A runtime LEASE does not separate the two either: the
    unshared path also takes a lease at registration (`acquire_session_lease`),
    so every registered chat holds a lease string whether or not it shares.
    `_shared_runtime` (set from `chat_share_key is not None`) is the flag that
    does separate them, and the one `_chat_turn_gate` already uses. These pin
    that the guard answers True only for an actual shared-runtime provider, so
    the default path stays byte-identical to base.
    """

    class _Shared:
        def __init__(self, shared: bool, lease=None):
            self._shared_runtime = shared
            self._runtime_lease = lease

    class _Outer:
        def __init__(self, inner):
            self._client = inner

    def test_a_shared_runtime_provider_answers_true(self):
        assert ro.is_shared_chat_runtime(self._Shared(True)) is True
        # Reached through the outer provider's ``_client`` too.
        assert ro.is_shared_chat_runtime(self._Outer(self._Shared(True))) is True

    def test_an_unshared_provider_answers_false_even_holding_a_lease(self):
        # The exact case a lease-based guard got wrong: a default-path chat is
        # registered, so it HOLDS a lease string, but it does not share.
        unshared = self._Shared(False, lease="lease-default")
        assert ro.holds_runtime_lease(unshared) is True
        assert ro.is_shared_chat_runtime(unshared) is False

    def test_a_non_acp_or_test_double_answers_false(self):
        # No ``_shared_runtime`` attribute at all: a non-ACP provider, a stub.
        assert ro.is_shared_chat_runtime(object()) is False
        # Outer provider with no client swapped in yet.
        assert ro.is_shared_chat_runtime(self._Outer(None)) is False


class TestTheRefusalNamesItsBranch:
    """The reason is what an operator reads in the log, so it is pinned.

    A refused placement looks exactly like a gateway where sharing was never
    turned on: one process per session. The branch name is the only thing that
    separates the four reasons, so a renamed or collapsed branch has to fail here.
    """

    def _ask(self, **overrides) -> str:
        base = dict(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )
        base.update(overrides)
        return chat_sharing_ineligible_reason(
            **base,
            member_context=False,
        )

    def test_an_eligible_session_has_no_reason(self):
        assert self._ask() == ""

    @pytest.mark.parametrize(
        "overrides,reason",
        [
            ({"sharing_enabled": False}, "sharing_disabled"),
            ({"backend": ACP_BACKEND_CODEX}, "backend_not_chat_shareable"),
            ({"memory_mode": "incognito"}, "memory_mode_not_shareable"),
            ({"session_key": "cron:nightly-digest"}, "origin_not_dashboard"),
        ],
    )
    def test_each_refusal_names_its_own_branch(self, overrides, reason):
        assert self._ask(**overrides) == reason

    def test_a_member_session_is_refused(self):
        """A crew-member session keeps its own process: member memory and
        workspace isolation is a boundary chat sharing does not cross, so the
        safe default refuses it. The refusal lives in this function, named."""
        import inspect

        params = inspect.signature(chat_sharing_ineligible_reason).parameters
        assert "member_context" in params
        assert "member_context" in inspect.signature(eligible_for_chat_sharing).parameters
        assert (
            chat_sharing_ineligible_reason(
                session_key="dashboard:chat-12-1790000000",
                memory_mode="persistent",
                sharing_enabled=True,
                backend=ACP_BACKEND_KIRO,
                member_context=True,
            )
            == "member_session"
        )

    @pytest.mark.parametrize("enabled", [True, False])
    @pytest.mark.parametrize("mode", ["persistent", "incognito"])
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX])
    def test_the_boolean_and_the_reason_never_disagree(self, enabled, mode, backend):
        """One decision, two spellings: a reason means refused, and no reason means
        eligible. A branch added to one and not the other would split them."""
        args = dict(
            session_key="dashboard:chat-12-1790000000",
            memory_mode=mode,
            sharing_enabled=enabled,
            backend=backend,
            member_context=False,
        )
        assert eligible_for_chat_sharing(**args) == (chat_sharing_ineligible_reason(**args) == "")


class TestTheCapIsGovernedByTheSwitch:
    """G1: sharing off is cap 1, whatever the stored number says."""

    def test_sharing_off_forces_one_session_per_process(self):
        assert chat_runtime_cap(sharing_enabled=False, configured=25) == 1
        assert chat_runtime_cap(sharing_enabled=False, configured=10) == 1

    def test_sharing_on_takes_the_configured_value(self):
        assert chat_runtime_cap(sharing_enabled=True, configured=25) == 25
        assert chat_runtime_cap(sharing_enabled=True, configured=2) == 2

    @pytest.mark.parametrize("nonsense", [0, -1, -100])
    def test_a_cap_below_one_reads_as_one_rather_than_zero(self, nonsense):
        """A zero cap would make every acquisition spawn AND refuse to serve.

        The loader clamps this already; the floor here is the second line of the
        same defence, and it lands on the behaviour that shipped before sharing.
        """
        assert chat_runtime_cap(sharing_enabled=True, configured=nonsense) == 1

    def test_the_default_config_ships_sharing_off(self):
        from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        assert chat_runtime_sharing_enabled(cfg) is False
        assert cfg.agent.chat_runtime_sharing_max_sessions == 10
        assert (
            chat_runtime_cap(
                sharing_enabled=chat_runtime_sharing_enabled(cfg),
                configured=cfg.agent.chat_runtime_sharing_max_sessions,
            )
            == 1
        )

    @pytest.mark.parametrize(
        "previews,on",
        [
            ({}, False),
            ({"chat_runtime_sharing": False}, False),
            ({"chat_runtime_sharing": "true"}, False),
            ({"chat_runtime_sharing": 1}, False),
            ({"chat_runtime_sharing": True}, True),
        ],
    )
    def test_only_a_true_preview_turns_sharing_on(self, previews, on):
        from types import SimpleNamespace

        from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled

        cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
        assert chat_runtime_sharing_enabled(cfg) is on

    def test_an_lts_build_reads_a_stored_true_as_off(self, monkeypatch):
        # An LTS build locks every preview off; a true carried over from another
        # build must not share a process there. Reading the raw mapping fails this.
        from types import SimpleNamespace

        import kiro_crew.edition_locks as edition_locks
        from kiro_crew.acp.chat_runtime_sharing import chat_runtime_sharing_enabled

        cfg = SimpleNamespace(
            dashboard=SimpleNamespace(feature_previews={"chat_runtime_sharing": True})
        )
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        edition_locks.reset_logged()
        assert chat_runtime_sharing_enabled(cfg) is False
        monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")
        assert chat_runtime_sharing_enabled(cfg) is True


# ── Which process ──


class TestChatRuntimeKey:
    """The key is every process-level spawn input, and nothing per-session."""

    def test_identical_inputs_compare_equal_and_hash_equal(self):
        assert a_key() == a_key()
        assert len({a_key(), a_key()}) == 1

    def test_env_order_does_not_fragment_the_table(self):
        assert a_key(extra_env={"A": "1", "B": "2"}) == a_key(extra_env={"B": "2", "A": "1"})

    @pytest.mark.parametrize(
        "field,value",
        [
            ("work_dir", "/home/u/oss/other-worktree"),
            ("agent", "kirocrew-lite"),
            ("sandbox_mode", "strict"),
            ("extra_env", {"A": "9"}),
            ("acp_backend", "kas"),
            ("memory_mode", "incognito"),
            ("shared_scratch", Path("/scratch/tree-a")),
            ("mcp_gateway_overlay", "/overlay/a.json"),
            ("mcp_gateway_socket", "/run/gw.sock"),
            ("model", "some-model-id"),
        ],
    )
    def test_every_process_level_field_splits_the_key(self, field, value):
        assert a_key() != a_key(**{field: value})

    def test_path_and_string_spellings_of_work_dir_agree(self):
        assert a_key(work_dir=Path("/home/u/.kirocrew/workspace")) == a_key()

    def test_a_trailing_separator_is_the_same_directory(self):
        assert a_key(work_dir="/home/u/.kirocrew/workspace/") == a_key()

    @pytest.mark.parametrize(
        "spelling",
        ["/home/u/.kirocrew/workspace", "/home/u/.kirocrew/workspace/"],
    )
    def test_freeze_path_normalizes_every_spelling_of_one_directory(self, spelling):
        from kiro_crew.acp.chat_runtime_sharing import _freeze_path

        assert _freeze_path(spelling) == _freeze_path(Path(spelling))
        assert _freeze_path(spelling) == _freeze_path("/home/u/.kirocrew/workspace")

    @pytest.mark.parametrize("unset", [None, ""])
    def test_freeze_path_reports_an_unset_field_as_empty(self, unset):
        from kiro_crew.acp.chat_runtime_sharing import _freeze_path

        assert _freeze_path(unset) == ""

    def test_different_directories_still_split_the_key(self):
        from kiro_crew.acp.chat_runtime_sharing import _freeze_path

        assert _freeze_path("/home/u/a") != _freeze_path("/home/u/b")

    def test_a_member_session_is_refused_and_founds_its_own_process(self):
        """A crew-member session keeps its own process: it is refused chat
        sharing, so even four dashboard slots of ONE member each found their own
        runtime. Member memory and workspace are isolated per member, so sharing
        one runtime across member sessions is a boundary this change does not
        cross -- the safe default wins. Driven through the registry, the path a
        real start takes.
        """
        ro._reset_for_tests()
        try:
            table = ro.RUNTIME_OWNERSHIP
            spawn = spawner(*[FakeRuntime(pid=9400 + n) for n in range(4)])
            for n in range(4):
                session = f"dashboard:chat-{n}-1790000000"
                assert not eligible_for_chat_sharing(
                    session_key=session,
                    memory_mode="persistent",
                    sharing_enabled=True,
                    backend=ACP_BACKEND_KIRO,
                    member_context=True,
                ), "a member session must be refused chat sharing"
                # Refused -> each founds its own runtime at cap 1.
                asyncio.run(table.acquire(a_key(), session, spawn, cap=1))
            assert len(spawn.calls) == 4, "member slots were allowed to share a process"
        finally:
            ro._reset_for_tests()

    def test_reasoning_effort_splits_the_key(self):
        """Two slots asking for different effort cannot share one process.

        The level is written into the work directory's cli.json overlay, one file
        per work directory keyed by model, read once at startup -- so sharing
        would make the last writer decide for everyone on that process.
        """
        assert a_key(reasoning_effort="high") != a_key(reasoning_effort="low")
        assert a_key(reasoning_effort="high") != a_key()
        assert a_key(reasoning_effort="high") == a_key(reasoning_effort="high")
        assert a_key(reasoning_effort=None) == a_key()

    def test_the_account_era_splits_the_key(self):
        """A process authenticates once, at spawn, and holds that credential.

        Every other field is what a session ASKS to spawn with, and those are
        identical before and after a `kiro login` to another account -- so without
        this field two eras compare equal and a session starting after the switch
        joins a process holding the old credential.
        """
        assert a_key(spawn_identity="acct-A") != a_key(spawn_identity="acct-B")
        assert a_key(spawn_identity="acct-A") != a_key()
        assert a_key(spawn_identity="acct-A") == a_key(spawn_identity="acct-A")
        assert a_key(spawn_identity=None) == a_key()
        # ``start()`` never keys on an EMPTY identity: an empty read (a genuine
        # failure on a dashboard slot, where a reader is always wired) is replaced
        # with a unique ``unverified-<uuid>`` token, so two starts whose identity
        # reads both failed carry DIFFERENT eras and found their own processes
        # rather than joining across unproven accounts. Modelled here as two
        # distinct tokens splitting the key.
        assert a_key(spawn_identity="unverified-aaaa") != a_key(spawn_identity="unverified-bbbb")
        assert a_key(spawn_identity="unverified-aaaa") != a_key()

    def test_the_ssh_forwarding_consent_splits_the_key(self):
        """SSH_AUTH_SOCK forwarding grants USE of the operator's ssh-agent keys for
        the process's whole life, decided once at spawn. A joiner inheriting a
        founder's forwarding (or being denied its own) is a credential-scope
        mismatch the running process cannot shed, so two consents are two keys."""
        assert a_key(forward_ssh_auth_sock=True) != a_key(forward_ssh_auth_sock=False)
        assert a_key(forward_ssh_auth_sock=True) != a_key()
        assert a_key(forward_ssh_auth_sock=True) == a_key(forward_ssh_auth_sock=True)
        assert a_key(forward_ssh_auth_sock=False) == a_key()

    def test_an_unverified_era_is_substituted_at_the_start_site(self):
        """The assertions above only show what two DIFFERENT tokens do to a key.

        What makes the era trustworthy is that ``start()`` never keys on an empty
        one, and that substitution is a single expression on a start path needing a
        live registry, a real spawn and an identity service to reach. Pinned
        structurally, the way this file pins the placement: delete the substitution
        and two failed reads are compatible again, which no key-level assertion
        notices.
        """
        first = make_placement(era="")
        second = make_placement(era="")
        assert first.key.spawn_identity.startswith("unverified-")
        # A CONSTANT fallback would read as a fix and share the same defect: every
        # unverified start would carry one era and they would all be compatible.
        assert first.key != second.key, (
            "an empty identity read is keyed on directly again, so two failed reads "
            "carry equal eras and a chat can join a process authenticated to another account"
        )
        assert make_placement(era="acct-A").key == make_placement(era="acct-A").key

    def test_the_spec_generation_splits_the_key(self):
        """The --agent spec is read once at startup and governs the process.

        A joining session skips the projection rebuild because the founder's is
        live, so an edit that revokes a grant leaves the process on the
        pre-revocation surface and a later joiner inherits it.
        """
        assert a_key(spec_generation="gen-1") != a_key(spec_generation="gen-2")
        assert a_key(spec_generation="gen-1") != a_key()
        assert a_key(spec_generation="gen-1") == a_key(spec_generation="gen-1")
        assert a_key(spec_generation=None) == a_key()

    def test_tool_search_settings_are_part_of_the_key(self):
        class TS:
            def __init__(self, enabled, min_pct, min_tokens):
                self.enabled = enabled
                self.min_pct = min_pct
                self.min_tokens = min_tokens

        on = a_key(tool_search=TS(True, 5, 50000))
        off = a_key(tool_search=TS(False, 5, 50000))
        assert on != off
        assert on != a_key(tool_search=None)


class TestTheSpecGenerationIsObservedNotGuessed:
    """``agent_spec_generation`` has to move on an edit and hold otherwise."""

    def test_the_same_files_give_the_same_token(self, tmp_path):
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        (d / "kirocrew.json").write_text('{"name": "kirocrew"}')
        first = agent_spec_generation(tmp_path, "kirocrew")
        assert first == agent_spec_generation(tmp_path, "kirocrew")

    def test_an_edit_moves_the_token(self, tmp_path):
        """The whole point: a revoked grant must not be joinable."""
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        spec = d / "kirocrew.json"
        spec.write_text('{"name": "kirocrew", "mcpServers": {"a": {}}}')
        before = agent_spec_generation(tmp_path, "kirocrew")
        spec.write_text('{"name": "kirocrew"}')
        assert agent_spec_generation(tmp_path, "kirocrew") != before

    def test_a_same_size_rewrite_with_a_restored_mtime_still_moves_the_token(self, tmp_path):
        """A revocation made to look like no change at all.

        Every field of a stat triple -- mtime, size, inode -- survives a rewrite
        in place to the same byte length with the timestamps put back. Only the
        content differs, so only a content hash answers, and answering "same" here
        would let a session join a process whose granted surface is not its own.
        """
        import os

        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        spec = d / "kirocrew.json"
        spec.write_text('{"name":"kirocrew","tools":["read","write","shell_x"]}')
        st = spec.stat()
        before = agent_spec_generation(tmp_path, "kirocrew")

        replacement = '{"name":"kirocrew","tools":["read","write","shell_y"]}'
        assert len(replacement) == st.st_size, "the test must hold the byte length equal"
        with open(spec, "r+b") as fh:  # in place, so the inode is unchanged too
            fh.write(replacement.encode())
        os.utime(spec, ns=(st.st_atime_ns, st.st_mtime_ns))

        after_st = spec.stat()
        assert (after_st.st_mtime_ns, after_st.st_size, after_st.st_ino) == (
            st.st_mtime_ns,
            st.st_size,
            st.st_ino,
        ), "precondition: the stat triple must be identical, or this proves nothing"
        assert agent_spec_generation(tmp_path, "kirocrew") != before

    def test_a_spec_past_the_read_cap_fragments(self, tmp_path, monkeypatch):
        """A file too large to hash is not observed, so it must not merge.

        Hashing a prefix would answer "same" to a rewrite past the cap, which is
        the evasion the hash exists to catch.

        The size check that produces this is a COST guard rather than an
        independent safety one: with it removed the read returns the cap's worth of
        bytes, the short-read check sees fewer bytes than the descriptor's size and
        fragments anyway. What the check buys is not reading a pathological file at
        all on the placement path of every eligible start.
        """
        import kiro_crew.acp.chat_runtime_sharing as mod

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        (d / "kirocrew.json").write_text('{"name": "kirocrew"}')
        monkeypatch.setattr(mod, "_SPEC_READ_CAP_BYTES", 4)
        one = mod.agent_spec_generation(tmp_path, "kirocrew")
        two = mod.agent_spec_generation(tmp_path, "kirocrew")
        assert one != two
        assert one.startswith("unobservable-")

    def test_the_read_is_bracketed_so_unstable_bytes_are_not_an_observation(self):
        """The second fstat is what turns the read into a check.

        Without it a write landing during the read yields bytes that are half of
        one spec and half of another, hashed as though they had been verified.

        Structural, and named as such rather than dressed up. Reproducing the
        window needs a hook into ``os.read``, and a behavioural version of this
        test was ORDER-DEPENDENT: the sensitive-path resolver is bounded and
        refuses under load, so the read sometimes never happens and the assertion
        that it did fails while the contract still holds. A flaky test of this
        guard is worse than a structural one -- it reds a board at random and
        teaches the next reader to ignore it.
        """
        import re
        from pathlib import Path

        import kiro_crew.acp.chat_runtime_sharing as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        start = source.index("def _observe_spec_bytes(")
        region = source[start : source.index("def agent_spec_generation(", start)]
        assert region.count("os.fstat(fd)") == 2, (
            "the observation no longer brackets its read with two fstats of the same "
            "descriptor, so a write landing during the read is hashed as though it had "
            "been checked"
        )
        assert re.search(r"st_mtime_ns,\s*\n\s*after\.st_size,\s*\n\s*after\.st_ino,", region), (
            "the bracket no longer compares mtime, size and inode, so a same-size write "
            "landing mid-read is invisible to it"
        )
        assert (
            "return None" in region.split("after.st_dev,")[1][:200]
        ), "the bracket detects the mismatch but does not fragment on it"
        # A write delivered by RENAME (atomic replace) is invisible to the fd
        # bracket -- the fd keeps the old inode -- so the observation ALSO re-stats
        # by NAME and fragments when the name's identity moved. Pinned structurally
        # for the same reason the fd bracket is: reproducing a rename mid-read is
        # order-dependent under the bounded resolver.
        assert "os.stat(path)" in region, (
            "the observation no longer re-stats by name after the read, so a write "
            "delivered by rename (which leaves the fd on the old inode) is hashed as "
            "though the name had not moved"
        )

    def test_an_absent_spec_is_a_real_answer_not_a_refusal(self, tmp_path):
        """Both sides agree the file is not there, and the child falls back alike."""
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        one = agent_spec_generation(tmp_path, "kirocrew")
        two = agent_spec_generation(tmp_path, "kirocrew")
        assert one == two, "an absent spec must not fragment the key"

    def test_an_unregistered_or_path_shaped_name_fragments_not_shares(self, tmp_path):
        """A name the resolver cannot register is a refusal, not an absence.

        A path-shaped or otherwise unregistered name resolves to no path in every
        scope, which would fold to the same ``-`` a genuinely-absent spec
        contributes -- so a start whose agent name cannot be resolved would share
        a runtime with one whose spec is really gone. It fragments to a unique
        token instead, both so two such starts do not join each other and so
        neither joins a legitimately-absent registered spec.
        """
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        first = agent_spec_generation(tmp_path, "../../escape")
        second = agent_spec_generation(tmp_path, "../../escape")
        assert first != second, "an unregistered name must fragment, not fold to '-'"
        absent_registered = agent_spec_generation(tmp_path, "kirocrew")
        assert (
            first != absent_registered and second != absent_registered
        ), "a refused name must not collide with a genuinely-absent spec's token"

    def test_the_read_goes_through_the_sensitive_path_gate(self, tmp_path, monkeypatch):
        """An agents directory is agent-writable, so a by-name read is a window.

        The name can be re-pointed at a credential file between the resolution and
        the read, so the bytes are taken through ``open_fenced_for_read``: it
        refuses a link at the final component, refuses a non-regular or hardlinked
        file, and asks the sensitive-path fence about the kernel's own path for the
        descriptor. Hashing a spec must not become a way to hash a credential.
        """
        import kiro_crew.pinned_fs as pinned
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        (d / "kirocrew.json").write_text('{"name": "kirocrew"}')

        seen: list[tuple[str, object]] = []
        real = pinned.open_fenced_for_read

        def recording(resolved, *, fence, **kwargs):
            seen.append((str(resolved), fence))
            return real(resolved, fence=fence, **kwargs)

        monkeypatch.setattr(pinned, "open_fenced_for_read", recording)
        agent_spec_generation(tmp_path, "kirocrew")

        assert seen, "the spec bytes were read without the fenced reader"
        assert any(str(d / "kirocrew.json") == path for path, _ in seen)
        assert all(callable(fence) for _, fence in seen), "the reader was given no fence"

    def test_a_refused_read_fragments_rather_than_merging(self, tmp_path, monkeypatch):
        """Absence of evidence must not admit a join.

        A start that cannot prove the generation founds its own process, so two such
        starts get two tokens and neither joins the other. Merging instead would let
        one refused read hand a session a process whose spec it never checked.
        """
        import kiro_crew.pinned_fs as pinned
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        d = tmp_path / ".kiro" / "agents"
        d.mkdir(parents=True)
        (d / "kirocrew.json").write_text('{"name": "kirocrew"}')

        def refuse(resolved, *, fence, **kwargs):
            raise OSError("refusing to read sensitive path")

        monkeypatch.setattr(pinned, "open_fenced_for_read", refuse)
        one = agent_spec_generation(tmp_path, "kirocrew")
        two = agent_spec_generation(tmp_path, "kirocrew")
        assert one != two
        assert one.startswith("unobservable-")


class TestThePlacementIsConfirmedAgainstTheSpecAfterwards:
    """The key is built BEFORE the acquisition, and the acquisition is not instant.

    A founding ``acquire`` holds the registry lock across a whole subprocess
    launch, so a concurrently starting session waits it out carrying a key
    observed before the wait. A revocation landing inside that window would place
    the session on a process founded under the older generation, and nothing later
    on the kiro path ends such a session.

    Driven on the retry contract rather than through ``AcpProvider.start``: what
    can go wrong is the release-and-rebuild, and the structural pin below holds
    the wiring.
    """

    def test_a_generation_that_moved_releases_the_lease_it_took(self):
        """The retry must not leave the first placement's lease outstanding."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9300)
            live = ro.RUNTIME_OWNERSHIP
            stale = a_key(spec_generation="gen-1")
            acq = asyncio.run(live.acquire(stale, "dashboard:chat-1-1", spawner(rt), cap=10))
            assert live.leases_on_runtime(rt) == 1

            # What the provider does when the confirming read disagrees.
            stranded = asyncio.run(live.release(acq.lease))
            assert stranded is rt, "the only lease was this one, so the process is stranded"
            asyncio.run(rt.kill(expected=True, reason="spec generation moved"))

            fresh_rt = KillableRuntime(pid=9301)
            fresh = a_key(spec_generation="gen-2")
            again = asyncio.run(
                live.acquire(fresh, "dashboard:chat-1-1", spawner(fresh_rt), cap=10)
            )
            assert again.runtime is fresh_rt
            assert again.joined is False, "the fresh key must not match the stale entry"
            assert live.leases_on_runtime(rt) == 0, "the stale entry outlived its release"
            assert rt.kills, "the stranded process was not ended"
        finally:
            ro._reset_for_tests()

    def test_an_account_era_that_moved_releases_the_lease_it_took(self):
        """The era re-read is symmetric with the generation one.

        A credential change landing during the acquisition wait moves the account
        era exactly as an edit moves the spec generation, and the placement's
        response is the same: release the lease it took and retry on the fresh
        era, so the session never keeps a lease on a process authenticated to an
        account other than the one it currently observes.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9310)
            live = ro.RUNTIME_OWNERSHIP
            stale = a_key(spawn_identity="acct-A")
            acq = asyncio.run(live.acquire(stale, "dashboard:chat-1-1", spawner(rt), cap=10))
            assert live.leases_on_runtime(rt) == 1

            # What the provider does when the era re-read disagrees with the era it
            # placed on: the same release-and-retry the generation path takes.
            stranded = asyncio.run(live.release(acq.lease))
            assert stranded is rt, "the only lease was this one, so the process is stranded"
            asyncio.run(rt.kill(expected=True, reason="account era moved"))

            fresh_rt = KillableRuntime(pid=9311)
            fresh = a_key(spawn_identity="acct-B")
            again = asyncio.run(
                live.acquire(fresh, "dashboard:chat-1-1", spawner(fresh_rt), cap=10)
            )
            assert again.runtime is fresh_rt
            assert again.joined is False, "the fresh-era key must not match the stale entry"
            assert live.leases_on_runtime(rt) == 0, "the stale entry outlived its release"
            assert rt.kills, "the stranded process was not ended"
        finally:
            ro._reset_for_tests()

    def test_a_forwarding_consent_that_moved_releases_the_lease_it_took(self):
        """The SSH_AUTH_SOCK-forwarding consent is confirmed like the era.

        Forwarding grants USE of the operator's ssh-agent keys for the process's
        whole life, decided once at spawn and unsheddable after, so a consent the
        operator revokes (or grants) during the acquisition wait moves the key's
        forwarding field exactly as a credential change moves the era. The
        placement's response is the same release-and-retry on the fresh consent,
        so a joiner never lands on a process founded under the opt-out it bypassed.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9320)
            live = ro.RUNTIME_OWNERSHIP
            stale = a_key(forward_ssh_auth_sock=True)
            acq = asyncio.run(live.acquire(stale, "dashboard:chat-1-1", spawner(rt), cap=10))
            assert live.leases_on_runtime(rt) == 1

            stranded = asyncio.run(live.release(acq.lease))
            assert stranded is rt, "the only lease was this one, so the process is stranded"
            asyncio.run(rt.kill(expected=True, reason="forwarding consent moved"))

            fresh_rt = KillableRuntime(pid=9321)
            fresh = a_key(forward_ssh_auth_sock=False)
            again = asyncio.run(
                live.acquire(fresh, "dashboard:chat-1-1", spawner(fresh_rt), cap=10)
            )
            assert again.runtime is fresh_rt
            assert again.joined is False, "the revoked-consent key must not match the stale entry"
            assert live.leases_on_runtime(rt) == 0, "the stale entry outlived its release"
            assert rt.kills, "the stranded process was not ended"
        finally:
            ro._reset_for_tests()

    def test_a_stale_key_cannot_join_an_entry_founded_on_a_fresh_one(self):
        """The mirror hazard, which is why a FOUNDER retries too.

        Leaving a stale-keyed entry in the table would let the next start on the
        older generation join a process it should not.
        """
        ro._reset_for_tests()
        try:
            live = ro.RUNTIME_OWNERSHIP
            fresh_rt, stale_rt = KillableRuntime(pid=9302), KillableRuntime(pid=9303)
            asyncio.run(
                live.acquire(
                    a_key(spec_generation="gen-2"), "dashboard:chat-1-1", spawner(fresh_rt), cap=10
                )
            )
            landed = asyncio.run(
                live.acquire(
                    a_key(spec_generation="gen-1"), "dashboard:chat-2-1", spawner(stale_rt), cap=10
                )
            )
            assert landed.runtime is stale_rt
            assert landed.joined is False
        finally:
            ro._reset_for_tests()

    def test_every_placement_decision_is_logged_at_warning(self):
        """Structural: one WARNING line per placement, in the two places a
        placement is decided.

        A refusal and a join are both invisible at WARNING without these, which is
        how a gateway ran 24 processes for 14 sessions with nothing in the log
        saying why. Pinned by source because reaching either line needs the whole
        ``AcpProvider.start`` preamble, and what a later edit silently does is drop
        the level to INFO or delete the call.
        """
        rt = FakeRuntime(pid=7001)
        spawn = spawner(rt)

        async def scenario():
            import kiro_crew.providers.chat_share as share_mod

            share_mod.RUNTIME_OWNERSHIP = RuntimeOwnership()
            try:
                # Two CHATS, so the second one joins: two starts of ONE chat go
                # on separate processes (``_Entry.admits``).
                for n in (1, 2):
                    p = make_placement(session_key=f"dashboard:chat-{n}-1790000000")
                    p._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
                    await p.place(spawn)
            finally:
                share_mod.RUNTIME_OWNERSHIP = ro.RUNTIME_OWNERSHIP

        with self._caplog_at_warning() as records:
            asyncio.run(scenario())
        lines = [r.getMessage() for r in records]
        assert any(
            m.startswith("chat-runtime-sharing spawned runtime=7001 tenants=1") for m in lines
        )
        assert any(
            m.startswith("chat-runtime-sharing joined runtime=7001 tenants=2") for m in lines
        )

    @staticmethod
    def _caplog_at_warning():
        import contextlib

        @contextlib.contextmanager
        def capture():
            records: list[logging.LogRecord] = []

            class _H(logging.Handler):
                def emit(self, record):
                    records.append(record)

            handler = _H(level=logging.WARNING)
            lg = logging.getLogger("kiro_crew.providers.chat_share")
            lg.addHandler(handler)
            try:
                yield records
            finally:
                lg.removeHandler(handler)

        return capture()

    def test_the_confirmation_is_wired_into_the_placement(self):
        """Structural: the helper being right is not the placement calling it.

        Driving ``AcpProvider.start`` needs its whole preamble; what a later edit
        breaks is the confirming read, the comparison, and the release that makes
        the retry safe.
        """
        from pathlib import Path

        import kiro_crew.providers.acp_runtime_start as start_mod
        import kiro_crew.providers.chat_share as share_mod

        start_src = Path(start_mod.__file__).read_text(encoding="utf-8")
        # BOTH placements go through the one confirming body: the cold start and
        # the rebuild after the runtime died during resume.
        assert "await share.place(_found_shared_runtime)" in start_src
        assert "await share.replace_dead(_found_shared_runtime)" in start_src
        source = Path(share_mod.__file__).read_text(encoding="utf-8")
        replace = source[
            source.index("async def replace_dead(") : source.index("async def abandon(")
        ]
        assert "await self.place(spawn)" in replace, "the rebuild skips the confirmation"
        read = source[
            source.index("async def _read_confirmation(") : source.index("async def _admit_joiner(")
        ]
        for reread in (
            "agent_spec_generation, self._work_dir",
            "pre_spawn_identity(self._identity_reader)",
            "_forward_ssh_auth_sock, self._sandbox_mode",
            "effective_sandbox_mode, self._sandbox_mode",
        ):
            assert reread in read, f"the placement no longer re-reads {reread!r} after acquiring"
        # The comparison and its fail-closed era rule live in ``place``; their
        # behaviour is driven in TestChatSharePlacement.
        place = source[source.index("async def place(") : source.index("def _accept(")]
        assert "bool(era) and era == self._raw_era" in place
        region = place
        # Every release in the placement settles through the shielded helper.
        assert region.count("_settle_release(placed.lease)") == 2
        assert "self.key = self._build_key()" in region, "the retry does not rebuild the key"

        # Text cannot tell live code from dead, so no confirmation in the
        # placement may be gated on a literal false.
        import ast

        tree = ast.parse(source)
        placement = next(
            n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "place"
        )
        for node in ast.walk(placement):
            if not isinstance(node, ast.If):
                continue
            for sub in ast.walk(node.test):
                assert not (isinstance(sub, ast.Constant) and sub.value is False), (
                    "a confirmation in the placement is gated on a literal false, so the "
                    f"read it guards decides nothing (line {node.lineno} of providers/acp.py)"
                )


class TestNoPlacementReadsWhenIneligible:
    """A sharing-off or ineligible start makes NO placement reads beyond config.

    The eligibility check (``chat_sharing_ineligible_reason``) decides whether a
    start may share from the config alone. The placement reads -- the spec
    generation (``agent_spec_generation``, a two-directory stat), the governed
    sandbox tier (``effective_sandbox_mode``, a possible profile walk) and the
    SSH_AUTH_SOCK-forwarding consent (``_forward_ssh_auth_sock``) -- are needed
    only to BUILD and key a placement, which only an eligible start does. So they
    must sit behind the eligibility gate: a default install (sharing off) refuses
    at the gate and reaches none of them, founding its own process exactly as it
    did before this feature. The reads live in ``providers.chat_share.resolve_chat_share``,
    pinned here by its source; ``TestChatSharePlacement`` drives it.
    """

    def _start_source(self):
        import inspect

        import kiro_crew.providers.chat_share as share_mod

        return inspect.getsource(share_mod.resolve_chat_share)

    def test_the_placement_reads_sit_after_the_eligibility_gate(self):
        src = self._start_source()
        gate = src.index("chat_sharing_ineligible_reason(")
        # Each placement read is called at least once, and NO call of it precedes
        # the eligibility gate. ``agent_spec_generation`` is excepted from the
        # "called" check only in the sense that it may appear as a bare reference
        # passed to a thread; its CALL sites are what must sit after the gate.
        for symbol in (
            "agent_spec_generation(",
            "effective_sandbox_mode(",
            "_forward_ssh_auth_sock(",
        ):
            assert symbol in src, f"{symbol} no longer appears in start()"
            assert src.index(symbol) > gate, (
                f"{symbol} is read BEFORE the eligibility gate, so a sharing-off start "
                "pays a placement read it never needs"
            )

    def test_only_config_is_read_before_the_gate(self):
        # Before the gate the ONLY blocking read is the config load (which the
        # gate itself consumes, via chat_runtime_sharing). No spec-generation
        # stat, sandbox-tier walk or ssh-forwarding read precedes it.
        src = self._start_source()
        gate = src.index("chat_sharing_ineligible_reason(")
        head = src[:gate]
        assert "KiroCrewConfig.load" in head, "the config load must precede the gate (it feeds it)"
        for symbol in (
            "agent_spec_generation(",
            "effective_sandbox_mode(",
            "_forward_ssh_auth_sock(",
        ):
            assert (
                symbol not in head
            ), f"{symbol} is read before the eligibility gate; only config may be"

    def test_a_refusal_returns_before_the_placement_reads(self):
        # The refusal branch (``if refusal:``) and the preview-off branch both
        # return before the first placement read, so neither reaches one.
        src = self._start_source()
        off = src.index("if not enabled:")
        refused = src.index("if refusal:")
        first_read = min(
            src.index(s)
            for s in (
                "agent_spec_generation(",
                "effective_sandbox_mode(",
                "_forward_ssh_auth_sock(",
            )
        )
        assert off < refused < first_read
        assert "return None" in src[off:refused]
        assert "return None" in src[refused:first_read]


class TestAJoinerPassesForkGovernance:
    """A session that JOINS a live shared runtime must pass the same fork-governance
    admission gate the spawn path runs.

    ``_spawn_chat_runtime`` runs ``require_fork_governance`` inside ``spawn()`` (the
    harness spawn path), so a FOUNDER is already admitted. A JOINER lands on an
    existing runtime WITHOUT spawning, so without an explicit check it never passes
    the gate. The hole is reachable in ordinary operation: a fork whose governance
    refresh FAILED leaves the readable spec bytes -- and so the share key's
    ``agent_spec_generation`` -- unchanged, which is exactly the state in which a
    joiner matches the live entry and would attach to a runtime running grants the
    admission gate rejects. The fix runs ``require_fork_governance`` for a joiner on
    the same confirmation bracket and fails closed to the spawn path's own refusal.
    """

    def test_require_fork_governance_refuses_a_failed_refresh(self, monkeypatch):
        """The trigger condition: a recorded failed refresh refuses, with the spec
        bytes (and so the share key) unchanged.

        This is the state the join path must not attach on -- the same refusal a
        fresh spawn gets, so a joiner that re-runs the gate is refused identically.
        """
        from kiro_crew import agent as agent_mod
        from kiro_crew.agent import ForkGovernanceUnresolved, require_fork_governance
        from kiro_crew.agent_materialization import fork_refresh

        # The agent reads as a private-template fork, and shadowing is clear so the
        # refusal reached is the failed-refresh one, not the shadow one.
        monkeypatch.setattr(
            agent_mod.agent_state,
            "get_fork_info",
            lambda declared, strict=False: object(),
        )
        monkeypatch.setattr(agent_mod, "project_agent_names", lambda *_a, **_k: frozenset())
        # The refresh has SETTLED (so the wait does not time out) but FAILED for
        # every agent -- the readable spec bytes are untouched by a failed refresh.
        monkeypatch.setattr(fork_refresh._fork_refresh_settled, "wait", lambda timeout=None: True)
        monkeypatch.setattr(fork_refresh, "_fork_refresh_failed", frozenset({"*"}))

        with pytest.raises(ForkGovernanceUnresolved, match="refresh failed"):
            require_fork_governance("kirocrew-forked", "/work")

    def test_the_join_path_runs_the_gate_and_fails_closed(self):
        """Structural + reachability: the placement runs ``require_fork_governance``
        for a JOINER and, on refusal, releases the lease and raises rather than
        returning ``placed``.

        Driving ``AcpProvider.start`` needs its whole preamble (the sibling
        re-reads are pinned the same way); what a later edit breaks is the gate
        call, its ``placed.joined`` guard, and the release-and-raise on refusal.
        """
        import ast
        from pathlib import Path

        import kiro_crew.providers.chat_share as provider_mod

        source = Path(provider_mod.__file__).read_text(encoding="utf-8")
        start = source.index("async def _admit_joiner(")
        region = source[start : source.index("def _accept(", start)]

        assert "require_fork_governance" in region, (
            "the placement no longer runs the fork-governance admission gate, so a "
            "joiner attaches to a live fork runtime the spawn path's gate would refuse"
        )
        assert "placed.joined" in region, (
            "the gate is not guarded on placed.joined, so it either re-runs on a "
            "founder (already admitted inside spawn) or runs on neither"
        )
        assert "ForkGovernanceUnresolved" in region, (
            "the placement does not catch the gate's refusal, so a refused joiner is "
            "not failed closed"
        )
        # On refusal the lease it took must be released AND the refusal re-raised as
        # the spawn path's own AcpRuntimeError -- a joiner that cannot be admitted
        # must not attach.
        assert "raise AcpRuntimeError(str(exc)) from exc" in region, (
            "a refused join does not re-raise the spawn path's refusal, so the joiner "
            "silently attaches or fails with the wrong error"
        )

        # TEXT cannot tell live code from dead: the gate must be reachable, not
        # gated on a literal false and not guarded by ``if False and placed.joined``.
        tree = ast.parse(source)
        placement = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_admit_joiner"
        )
        # ``require_fork_governance`` is handed to ``asyncio.to_thread`` as an arg.
        to_thread_gate = [
            node
            for node in ast.walk(placement)
            if isinstance(node, ast.Call)
            and any(
                isinstance(a, ast.Name) and a.id == "require_fork_governance" for a in node.args
            )
        ]
        assert to_thread_gate, "the gate is not invoked off-loop via asyncio.to_thread"
        for node in ast.walk(placement):
            if not isinstance(node, ast.If):
                continue
            for sub in ast.walk(node.test):
                assert not (isinstance(sub, ast.Constant) and sub.value is False), (
                    "a confirmation in the placement is gated on a literal false "
                    f"(line {node.lineno} of providers/acp.py)"
                )

    def test_a_cancel_inside_the_gate_releases_the_joiners_lease(self):
        """Behavioural: a start CANCELLED while the joiner waits inside the gate
        must release ``placed.lease``, or the founder's last release returns
        ``None`` and the runtime is never handed back to be killed.

        ``require_fork_governance`` blocks on the fork-refresh wait for up to
        ``_FORK_REFRESH_WAIT_SECS`` -- the longest await in the confirmation
        bracket -- so a cancellation (a restart timeout, a slot deletion) is what
        actually reaches this code. This reproduces the exact ``if placed.joined:``
        block against a real ``RuntimeOwnership`` with a founder and a joiner on
        one runtime, with the gate patched to block, and cancels the placement
        task while it waits.

        It is run BOTH ways to pin that the ``except BaseException`` arm is what
        fixes it: with only the ``ForkGovernanceUnresolved`` arm (the head before
        this fix), the cancel propagates with the lease outstanding and the
        founder's last release returns ``None``; with the ``BaseException`` arm,
        the lease is released and the founder's release hands the runtime back.
        """
        from typing import cast as _cast

        def _run(with_base_exception_arm: bool):
            ro._reset_for_tests()
            try:
                rt = KillableRuntime(pid=9500)
                live = ro.RUNTIME_OWNERSHIP
                # A founder holds the runtime alive; the joiner lands on it.
                founder = asyncio.run(
                    live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10)
                )

                reached_gate = asyncio.Event()

                async def _blocking_gate() -> None:
                    # Stands in for the up-to-60s fork-refresh wait inside
                    # ``require_fork_governance``: the longest await in the
                    # bracket, which is exactly where the cancellation lands. A
                    # real ``to_thread`` await resumes the coroutine with
                    # CancelledError the same way.
                    reached_gate.set()
                    await asyncio.Event().wait()

                async def _run_the_block() -> None:
                    placed = await live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10)
                    assert placed.joined, "the second acquisition should JOIN"
                    from kiro_crew.agent import ForkGovernanceUnresolved  # noqa: F401

                    if placed.joined:
                        try:
                            await _blocking_gate()
                        except ForkGovernanceUnresolved:  # pragma: no cover
                            await live.release(placed.lease)
                            raise
                        except BaseException:
                            if not with_base_exception_arm:
                                # The head before this fix had NO such arm: the
                                # cancel propagates with the lease outstanding.
                                raise
                            # The fix: release under a shield loop (a second
                            # cancel cannot strand it), kill any handed-back
                            # runtime, then re-raise -- as the bracket above does.
                            release_task = asyncio.ensure_future(live.release(placed.lease))
                            while not release_task.done():
                                try:
                                    await asyncio.shield(release_task)
                                except asyncio.CancelledError:
                                    continue
                            stranded = release_task.result()
                            if stranded is not None:
                                await _cast(KillableRuntime, stranded).kill(
                                    expected=True,
                                    reason=(
                                        "chat runtime sharing: join cancelled "
                                        "inside fork governance"
                                    ),
                                )
                            raise

                async def _drive() -> None:
                    task = asyncio.create_task(_run_the_block())
                    # Reach the gate, then cancel the task there, exactly as a
                    # restart timeout would during the real fork-refresh wait.
                    # Bounded: if ``_run_the_block`` ever ends early (e.g. the
                    # ``placed.joined`` setup assert fails) ``reached_gate`` is
                    # never set, so this wait must not hang the run -- it surfaces
                    # the real failure from the task instead.
                    try:
                        await asyncio.wait_for(
                            reached_gate.wait(), timeout=5
                        )  # flake-ok: awaits the Event the gated task sets, bounded only to surface the task's real failure
                    except asyncio.TimeoutError:
                        task.cancel()
                        # Re-raise the task's own failure if it has one.
                        await task
                        raise AssertionError("the placement task never reached the gate")
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

                asyncio.run(_drive())
                leases_after = live.leases_on_runtime(rt)
                last_release = asyncio.run(live.release(founder.lease))
                return leases_after, last_release, rt
            finally:
                ro._reset_for_tests()

        # Without the fix's arm, the cancelled join STRANDS its lease: the runtime
        # keeps 2 leases, so the founder's release is NOT the last one and hands
        # back nothing -- the leaked process is never killed. (Pins the defect.)
        leases, last_release, _rt = _run(with_base_exception_arm=False)
        assert leases == 2, "without the BaseException arm the joiner lease should strand"
        assert last_release is None, (
            "the stranded lease makes the founder's release non-terminal, so the "
            "runtime is never handed back to be killed -- the defect"
        )

        # With the fix's arm, the join releases its lease, so the founder's release
        # is the LAST one and hands the runtime back to be killed.
        leases, last_release, rt = _run(with_base_exception_arm=True)
        assert leases == 1, "the BaseException arm did not release the joiner lease"
        assert last_release is rt, (
            "the founder's last release did not hand the runtime back, so the "
            "joiner's lease was still stranded"
        )

    def test_the_join_path_releases_on_a_cancel_inside_the_gate(self):
        """Source-pin for the fix's ``except BaseException`` arm on the joiner
        gate: it must release the lease via a shielded release task (so a second
        cancel cannot strand it), kill any handed-back runtime, and re-raise.
        """
        from pathlib import Path

        import kiro_crew.providers.chat_share as provider_mod

        source = Path(provider_mod.__file__).read_text(encoding="utf-8")
        block = source[source.index("async def _admit_joiner(") : source.index("async def place(")]
        assert "except BaseException:" in block, (
            "the joiner gate no longer has a BaseException arm, so a cancel inside "
            "require_fork_governance propagates with placed.lease outstanding"
        )
        arm = block[block.index("except BaseException:") :]
        assert "_settle_release(placed.lease)" in arm, (
            "the BaseException arm does not release through the shielded settle, so "
            "a second cancel can strand the lease the first was unwinding"
        )
        assert arm.rstrip().endswith("raise"), "the cancellation is swallowed"
        settle = source[source.index("async def _settle_release(") :]
        settle = settle[: settle.index("\n\n\n")]
        assert "asyncio.ensure_future(" in settle and "asyncio.shield(task)" in settle


class TestAnUnkeyedSpawnRereadsTheForwardingConsent:
    """The frozen consent travels with a KEY, and only with a key.

    Freezing the SSH_AUTH_SOCK-forwarding consent is what lets a shared process
    match the key it was founded under. The unshared branch founds no key, so
    handing it the frozen value there swaps the fresh read inside ``spawn()``
    for an older one and protects nothing: an operator who revokes forwarding
    after this start's early read would still see the new process receive the
    agent socket, and a running process cannot shed it.
    """

    def test_the_founding_spawn_takes_the_override_from_the_placement(self):
        """The founding runtime's override is ``share.spawn_forward_ssh()``.

        That answers the frozen consent only on a joinable key and ``None``
        (resolve at spawn) once placement gave up; both answers are driven in
        ``TestChatSharePlacement``. The unshared start builds its runtime without
        the argument, so it resolves the consent at spawn as before.
        """
        import ast
        from pathlib import Path

        import kiro_crew.providers.acp_runtime_start as start_mod

        source = Path(start_mod.__file__).read_text(encoding="utf-8")
        found = next(
            n
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_found_shared_runtime"
        )
        overrides = [
            ast.unparse(kw.value)
            for node in ast.walk(found)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "forward_ssh_auth_sock"
        ]
        assert overrides == ["share.spawn_forward_ssh()"]
        outside = [
            kw
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "forward_ssh_auth_sock"
        ]
        assert len(outside) == 1, "an unshared AcpRuntime start passes a frozen consent"

    def test_giving_up_on_placement_drops_the_frozen_consent(self):
        p = make_placement(forward_ssh=True)
        assert p.spawn_forward_ssh() is True
        p.joinable = False
        assert p.spawn_forward_ssh() is None

    def test_the_runtime_treats_none_as_resolve_it_yourself(self):
        """``None`` is the contract the unkeyed branch relies on.

        The provider's fix is only a fix because the launch tail reads a ``None``
        override as "no frozen answer, resolve the consent at spawn". If that
        consumer ever treated ``None`` as a value, the unkeyed branch would be
        passing an answer of "off" rather than asking for a fresh read. The launch
        tail lives in ``acp.launch.launch``; the runtime threads its
        ``_forward_ssh_auth_sock_override`` into the ``LaunchRequest`` and the
        None-distinction lives there.
        """
        from pathlib import Path

        import kiro_crew.acp.launch as launch_mod
        import kiro_crew.acp.runtime_spawn_launch as spawn_launch_mod
        from kiro_crew.acp.runtime import AcpRuntime

        launch_src = Path(launch_mod.__file__).read_text(encoding="utf-8")
        assert "if request.forward_ssh_auth_sock_override is not None:" in launch_src, (
            "the launch tail no longer distinguishes an absent override from a false "
            "one, so an unkeyed spawn's None is read as a decision instead of a "
            "request to resolve the consent"
        )
        # The runtime must still carry the frozen value through to that tail.
        spawn_src = Path(spawn_launch_mod.__file__).read_text(encoding="utf-8")
        assert (
            'forward_ssh_auth_sock_override=getattr(self, "_forward_ssh_auth_sock_override", None)'
            in spawn_src
        ), "the runtime no longer threads its frozen forwarding consent into the launch request"
        assert AcpRuntime()._forward_ssh_auth_sock_override is None
        assert AcpRuntime(forward_ssh_auth_sock=True)._forward_ssh_auth_sock_override is True


# ── Two sessions on one process ──


class TestTwoSessionsShareOneProcess:
    """The behaviour the cap exists for, and the one it must not break.

    A cap of 2 is enough to prove both directions, and small enough that the
    third session's spawn is part of the assertion rather than a detail: what
    goes wrong with a cap is either that nobody joins, or that the joiner's
    arrival lets one session's close kill the other's process.
    """

    def test_a_second_eligible_session_lands_on_the_first_process(self):
        reg = RuntimeOwnership()
        rt = FakeRuntime(pid=9001)
        spawn = spawner(rt)

        first = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=2))
        second = asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=2))

        assert first.joined is False, "the first session founds the process"
        assert second.joined is True, "the second session must JOIN, not spawn"
        assert second.runtime is rt
        assert second.leases_on_runtime == 2
        assert len(spawn.calls) == 1, "one process served both sessions"

    def test_closing_one_session_leaves_the_other_running(self):
        reg = RuntimeOwnership()
        rt = FakeRuntime(pid=9002)
        spawn = spawner(rt)
        first = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=2))
        second = asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=2))

        # The first session closes: its release must hand back NOTHING, because
        # handing the runtime back is what tells the caller to kill it.
        assert asyncio.run(reg.release(first.lease)) is None
        assert reg.leases_on_runtime(rt) == 1
        assert rt.is_alive(), "the surviving session's process must still be up"

        # And the last one out does get it back, so the process is not leaked.
        assert asyncio.run(reg.release(second.lease)) is rt
        assert reg.leases_on_runtime(rt) == 0

    def test_the_cap_is_a_ceiling_the_third_session_spawns_past(self):
        reg = RuntimeOwnership()
        first_rt, second_rt = FakeRuntime(pid=9003), FakeRuntime(pid=9004)
        spawn = spawner(first_rt, second_rt)

        a = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=2))
        b = asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=2))
        c = asyncio.run(reg.acquire(a_key(), "dashboard:chat-3-1", spawn, cap=2))

        assert (a.runtime, b.runtime) == (first_rt, first_rt)
        assert c.runtime is second_rt, "a full process must not take a third session"
        assert c.joined is False
        assert len(spawn.calls) == 2

    def test_a_session_with_a_different_key_does_not_join(self):
        reg = RuntimeOwnership()
        mine, theirs = FakeRuntime(pid=9005), FakeRuntime(pid=9006)
        spawn = spawner(mine, theirs)

        asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=25))
        other = asyncio.run(
            reg.acquire(a_key(agent="kirocrew-lite"), "dashboard:chat-2-1", spawn, cap=25)
        )

        assert other.runtime is theirs, "an incompatible key must not share a process"
        assert len(spawn.calls) == 2

    def test_the_kill_gate_refuses_a_co_tenants_process(self):
        """The guard that makes the close above safe, read at the gate itself.

        A caller that signals without releasing is refusing its own teardown, and
        that is deliberate: with two sessions on one process, a kill that skipped
        the release would end the other session mid-turn.
        """
        reg = RuntimeOwnership()
        rt = FakeRuntime(pid=9007)
        spawn = spawner(rt)
        first = asyncio.run(reg.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=2))
        asyncio.run(reg.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=2))

        # The real gate reads the module singleton, so the entries have to be
        # there rather than in a private registry.
        ro._reset_for_tests()
        try:
            live = ro.RUNTIME_OWNERSHIP
            held_a = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=2))
            asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=2))

            assert (
                authorize_runtime_kill(rt, reason="close", caller="test") is False
            ), "a process two sessions hold must not be killable"

            asyncio.run(live.release(held_a.lease))
            assert (
                authorize_runtime_kill(rt, reason="close", caller="test") is False
            ), "one release of two is still a co-tenant's live process"
        finally:
            ro._reset_for_tests()
        assert first.lease  # the private registry above is untouched by the reset

    def test_the_last_release_makes_the_process_killable(self):
        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=9008)
            live = ro.RUNTIME_OWNERSHIP
            a = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=2))
            b = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=2))

            asyncio.run(live.release(a.lease))
            assert asyncio.run(live.release(b.lease)) is rt
            assert authorize_runtime_kill(rt, reason="close", caller="test") is True
        finally:
            ro._reset_for_tests()

    def test_at_cap_one_nobody_ever_joins(self):
        """G1 stated as a test: the default cap is the unshared behaviour.

        Same keys, same sessions, same table -- only the cap differs, and at 1
        every acquisition founds its own process and every release is a last
        release.
        """
        reg = RuntimeOwnership()
        runtimes = [FakeRuntime(pid=9100 + i) for i in range(3)]
        spawn = spawner(*runtimes)

        acquisitions = [
            asyncio.run(reg.acquire(a_key(), f"dashboard:chat-{i}-1", spawn, cap=1))
            for i in range(3)
        ]

        assert [a.joined for a in acquisitions] == [False, False, False]
        assert [a.leases_on_runtime for a in acquisitions] == [1, 1, 1]
        assert len(spawn.calls) == 3, "one process per session, as before the cap existed"
        for acq, rt in zip(acquisitions, runtimes):
            assert asyncio.run(reg.release(acq.lease)) is rt


# ── The teardown of a session that shares its process ──


class KillableRuntime(FakeRuntime):
    """A runtime that records whether anything killed it."""

    def __init__(self, pid: int = 4242) -> None:
        super().__init__(pid)
        self.kills: list[str] = []
        self.acp_backend = ACP_BACKEND_KIRO
        # Written by the provider's memory_mode setter for a non-persistent mode.
        self.recording_allowed = True

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)
        self._alive = False


class FakeHandle:
    """The handle methods the shared shutdown arm reaches."""

    def __init__(self) -> None:
        self.destroyed = 0
        self.cancelled = 0
        self.is_turn_active = False
        self.keep_transcript = False
        # The provider's ``memory_mode`` is a view onto the handle's.
        self.memory_mode = "persistent"

    async def destroy(self) -> None:
        self.destroyed += 1

    async def cancel(self) -> None:
        self.cancelled += 1


def shared_provider(runtime, session_key: str, lease: str):
    """A provider that releases exactly the lease it was handed."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = FakeHandle()
    provider = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,
        owns_runtime=True,
        runtime_lease=lease,
        shared_runtime=True,
        session_key=session_key,
    )
    provider.memory_mode = "persistent"
    return provider, handle

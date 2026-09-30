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

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp.chat_runtime_sharing import (
    ChatRuntimeKey,
    chat_runtime_cap,
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
        member_context=False,
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
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_bare_chat_slot_key_is_eligible(self):
        assert eligible_for_chat_sharing(
            session_key="chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_non_persistent_never_shares(self, mode):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode=mode,
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_member_session_never_shares(self):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=True,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_flag_off_disables_sharing(self):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=False,
            backend=ACP_BACKEND_KIRO,
        )

    def test_a_backend_without_multiplexed_sessions_is_not_eligible(self):
        """A chat slot is necessary but not sufficient: the host must multiplex.

        Such a host is still served by AcpRuntime, but its destroy() is an
        irreversible server-side session delete, and the shared teardown must
        destroy the handle to leave a process it may not kill -- so sharing there
        would discard the session's own resume record on every ordinary close.
        """
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_CHAT_RUNTIME_SHARING
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KAS,
        )
        # Everything else identical, on a host that DOES multiplex.
        assert eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
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
        # The chat set is a SUBSET of the subagent set -- a chat-shareable host
        # must first be subagent-shareable, but not the reverse.
        assert ACP_BACKENDS_CHAT_RUNTIME_SHARING <= ACP_BACKENDS_SESSION_SHARING
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_CODEX,
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
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )


# ── The cap ──


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
        from kiro_crew.config.sections import AgentConfig

        cfg = AgentConfig()
        assert cfg.chat_runtime_sharing is False
        assert (
            chat_runtime_cap(
                sharing_enabled=cfg.chat_runtime_sharing,
                configured=cfg.chat_runtime_sharing_max_sessions,
            )
            == 1
        )


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
            ("member_context", True),
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
        from pathlib import Path

        import kiro_crew.providers.acp as provider_mod

        source = Path(provider_mod.__file__).read_text(encoding="utf-8")
        line = next(
            ln for ln in source.splitlines() if ln.strip().startswith("chat_share_identity =")
        )
        assert "uuid.uuid4()" in line, (
            "an empty identity read is keyed on directly again, so two failed reads "
            "carry equal eras and a chat can join a process authenticated to another account"
        )
        # A CONSTANT fallback would read as a fix and share the same defect: every
        # unverified start would carry one era and they would all be compatible.
        assert 'or f"unverified' in line, "the unverified era is no longer unique per start"

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

    def test_a_path_shaped_name_is_refused_by_the_resolver_not_by_this(self, tmp_path):
        """A traversal resolves to nothing, which is the same answer on both sides.

        The resolver refuses such a name outright, and the child refuses it the same
        way, so this is a real "no spec" rather than a failure to observe one.
        """
        from kiro_crew.acp.chat_runtime_sharing import agent_spec_generation

        assert agent_spec_generation(tmp_path, "../../escape") == agent_spec_generation(
            tmp_path, "../../escape"
        )

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

    def test_the_confirmation_is_wired_into_the_placement(self):
        """Structural: the helper being right is not the placement calling it.

        Driving ``AcpProvider.start`` needs its whole preamble; what a later edit
        breaks is the confirming read, the comparison, and the release that makes
        the retry safe.
        """
        import re
        from pathlib import Path

        import kiro_crew.providers.acp as provider_mod

        source = Path(provider_mod.__file__).read_text(encoding="utf-8")
        start = source.index("async def _place_chat_runtime()")
        region = source[start : source.index("async def _spawn_chat_runtime()", start)]
        # BOTH placements go through the one body: the cold start and the rebuild
        # after the runtime died during resume. A second placement that re-acquired
        # without confirming would reintroduce the hole on the rarer path.
        assert (
            source.count("await _place_chat_runtime()") == 2
        ), "a placement no longer goes through the confirming helper"
        assert re.search(r"agent_spec_generation, work_dir", region), (
            "the placement no longer re-reads the spec generation after acquiring, so a "
            "revocation landing during the acquisition places this session on a process "
            "founded under the older generation"
        )
        # The account era is re-read on the SAME bracket as the spec generation:
        # a credential change landing during the acquisition wait is exactly the
        # hole the spec re-read closes for the spec, and it is open for the
        # account until the era is re-read and compared too.
        assert "pre_spawn_identity(chat_share_identity_reader)" in region, (
            "the placement no longer re-reads the account era after acquiring, so a "
            "credential change landing during the acquisition places this session on a "
            "process that authenticated under the older era"
        )
        assert (
            "if confirmed == chat_share_spec_generation and era_holds and forward_ssh_holds:"
            in region
        ), "the placement returns without confirming the spec generation, the era, AND the consent"
        assert (
            "confirmed_era == chat_share_pre_spawn_era" in region
        ), "the era is re-read but not compared to the era this start observed"
        assert (
            "bool(confirmed_era)" in region
        ), "an empty (failed) era re-read is not fenced, so it can pass as a match"
        # The SSH_AUTH_SOCK-forwarding consent is a RESOURCE opt-out: a joiner
        # landing on a process founded under the older consent bypasses an opt-out
        # the operator updated during the acquisition wait. Re-read and compared on
        # the same bracket as the era and the generation.
        assert "_forward_ssh_auth_sock)" in region, (
            "the placement no longer re-reads the SSH_AUTH_SOCK-forwarding consent after "
            "acquiring, so a joiner bypasses a resource opt-out that landed during the wait"
        )
        assert (
            "confirmed_forward_ssh == chat_share_forward_ssh" in region
        ), "the forwarding consent is re-read but not compared to the one this start placed on"
        # COUNTED, not merely present. The retry and the exhausted-attempts path
        # each release and each gate on the handback, so an assertion that one
        # occurrence exists passes while the other is gutted.
        assert (
            region.count("RUNTIME_OWNERSHIP.release(placed.lease)") == 2
        ), "a placement path no longer releases the lease it took, so the retry strands it"
        assert (
            region.count("stranded is not None") == 2
        ), "a release that hands back the runtime is ignored, so a retry leaks a process"
        assert (
            "asyncio.shield(release_task)" in region
        ), "the retry's release is no longer shielded, so a cancellation orphans that lease"
        assert "_build_chat_share_key()" in region, "the retry does not rebuild the key"

        # Every assertion above reads TEXT, and text cannot tell live code from
        # dead: switching a confirming guard to ``if False and ...`` leaves each
        # string it looks for exactly where it was. So the guards are also checked
        # for reachability, by parsing rather than matching -- no confirmation in
        # this placement may be gated on a literal false.
        import ast

        tree = ast.parse(source)
        placement = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_place_chat_runtime"
        )
        for node in ast.walk(placement):
            if not isinstance(node, ast.If):
                continue
            for sub in ast.walk(node.test):
                assert not (isinstance(sub, ast.Constant) and sub.value is False), (
                    "a confirmation in the placement is gated on a literal false, so the "
                    f"read it guards decides nothing (line {node.lineno} of providers/acp.py)"
                )


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


class TestSharedShutdownAsksTheTableWhoIsLeft:
    """The lease table, not the provider, decides whether the process dies.

    Driven through the real ``shutdown`` because the bug this arm exists to
    prevent -- a session's close ending its co-tenants' process -- lives in the
    branch choice, not in the table.
    """

    def test_a_joining_session_leaving_does_not_kill_the_shared_process(self):
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9200)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-2-1", two.lease)
            asyncio.run(provider.shutdown())

            assert rt.kills == [], "a joining session killed its co-tenant's process"
            assert rt.is_alive()
            assert handle.destroyed == 1, "the joiner must still be evicted from the process"
            # The remaining lease still holds it, so ITS release is the one that
            # hands the runtime back to be killed.
            assert asyncio.run(live.release(one.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_the_founder_leaving_first_does_not_kill_it_either(self):
        """The founder cannot be told apart at teardown time, and must not try."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9201)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", one.lease)
            asyncio.run(provider.shutdown())

            assert rt.kills == []
            assert rt.is_alive()
            assert handle.destroyed == 1
            assert asyncio.run(live.release(two.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_the_last_session_out_kills_the_process(self):
        """The other direction: a shared process must not be leaked either."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9202)
            live = ro.RUNTIME_OWNERSHIP
            one = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            two = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            first, _ = shared_provider(rt, "dashboard:chat-1-1", one.lease)
            asyncio.run(first.shutdown())
            second, second_handle = shared_provider(rt, "dashboard:chat-2-1", two.lease)
            asyncio.run(second.shutdown())

            assert len(rt.kills) == 1, "the last holder must end the process"
            assert not rt.is_alive()
            assert second_handle.destroyed == 1
        finally:
            ro._reset_for_tests()

    def test_a_same_key_race_loser_does_not_kill_the_winners_process(self):
        """Two starts for ONE session key; the loser tears down; the winner lives.

        The allocator produces this shape on purpose -- it carries a race budget
        for it -- so the loser's shutdown must not end the registered winner's
        process. Every acquisition minting its own lease is what makes that work,
        and this is the pin that catches a table keyed by session instead.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9203)
            live = ro.RUNTIME_OWNERSHIP
            winner = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            loser = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            assert winner.lease != loser.lease

            losing, losing_handle = shared_provider(rt, "dashboard:chat-1-1", loser.lease)
            asyncio.run(losing.shutdown())

            assert rt.kills == [], "the race loser killed the winner's process"
            assert rt.is_alive()
            assert losing_handle.destroyed == 1
            assert asyncio.run(live.release(winner.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_an_in_flight_turn_is_cancelled_before_the_handle_goes(self):
        """An abandoned prompt would keep running on a process nothing may kill."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9204)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            handle.is_turn_active = True
            asyncio.run(provider.shutdown())

            assert handle.cancelled == 1
            assert handle.destroyed == 1
            assert rt.kills == []
        finally:
            ro._reset_for_tests()

    def test_the_transcript_survives_the_destroy(self):
        """A shared session HAS to destroy its handle, so it must ask to keep it.

        The sole-owner arm keeps the transcript by never reaching ``destroy`` for
        a persistent session. This arm reaches it every time.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9205)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)

            asyncio.run(provider.shutdown())

            assert handle.keep_transcript is True
            assert handle.destroyed == 1
        finally:
            ro._reset_for_tests()

    def test_a_non_persistent_shared_session_does_not_keep_its_transcript(self):
        """The one mode whose files are meant to go. Guards the arm above."""
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9206)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            provider.memory_mode = "incognito"

            asyncio.run(provider.shutdown())

            assert handle.keep_transcript is False
        finally:
            ro._reset_for_tests()

    def test_the_lease_is_released_even_when_the_cancel_is_cancelled(self):
        """A lease never returned keeps its entry alive and strands the process.

        The restart path awaits ``shutdown`` under a timeout larger than the
        cancel budget, so a slow cancel is exactly where a ``CancelledError``
        lands -- and ``CancelledError`` is a ``BaseException``, which is why the
        release is in a ``finally`` rather than after an ``except Exception``.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9207)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)
            handle.is_turn_active = True

            async def cancel_forever() -> None:
                raise asyncio.CancelledError()

            handle.cancel = cancel_forever  # type: ignore[method-assign]

            with pytest.raises(asyncio.CancelledError):
                asyncio.run(provider.shutdown())

            assert live.leases_on_runtime(rt) == 1, "the lease was not released"
            assert handle.destroyed == 1, "the session was not evicted from the process"
            assert asyncio.run(live.release(other.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_a_failed_start_releases_a_shared_runtime_rather_than_killing_it(self):
        """Structural, because the behavioural reach is the whole of ``start()``.

        The cleanup arm in ``AcpProvider.start`` runs when session setup fails
        after the spawn. On a shared process a kill there would end co-tenants'
        sessions over a failure that is not theirs -- and would be refused by the
        gate anyway, which turns the leak the arm exists to prevent into a
        permanent one. What is pinned is that the arm has a shared branch, that
        the branch releases, and that it evicts the session it may have created.

        A source assertion is weaker than driving the path, and it is named as
        such rather than dressed up: it catches the branch being deleted or
        stripped, which is what a later edit does, and not a subtly wrong ordering
        inside it.
        """
        import re
        from pathlib import Path

        import kiro_crew.providers.acp as provider_mod

        source = Path(provider_mod.__file__).read_text(encoding="utf-8")
        arm = source.split("failed session setup cleanup", 1)
        assert len(arm) > 1, "the cleanup arm's own marker string is gone"
        # The cleanup arm's SHARED branch alone. Anchored on the arm's own comment
        # rather than on the branch condition, which the respawn path spells the
        # same way -- matching that one instead would let this pin pass while the
        # branch it is about is gutted. Bounded by the ``else`` that begins the
        # sole-owner branch, whose kill is correct and must not be read as this
        # branch's.
        arm_start = source.index("# A SHARED runtime is released rather than killed:")
        start = source.index("if chat_share_lease is not None:", arm_start)
        region = source[start : source.index("\n                else:", start)]
        assert "RUNTIME_OWNERSHIP.release(chat_share_lease)" in region, (
            "the shared branch of the failed-setup cleanup no longer releases the "
            "lease; a kill without a release is refused by the gate and leaks"
        )
        assert "terminate_session" in region, (
            "the shared branch no longer evicts this session from the process it "
            "is leaving, so its context stays allocated for the process's life"
        )
        # The kill inside the shared branch is reached only through the release's
        # return value, never on the runtime this start was handed.
        assert not re.search(r"await runtime\.kill\(", region), (
            "the shared branch kills the runtime it was handed; only the runtime "
            "the release HANDS BACK may be killed"
        )

    def test_a_cancellation_during_the_release_cannot_orphan_the_lease(self):
        """The release is an await, so a cancellation can land inside it.

        Clearing the slot before the release completes would leave the lease
        neither released nor recoverable: the slot is empty, so no retry can find
        it, and the gate then refuses every kill of that pid for the gateway's
        life. That is the leak this arm exists to prevent, made permanent by the
        bookkeeping -- so the release is shielded and the slot is cleared only once
        it returns.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9209)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))
            provider, handle = shared_provider(rt, "dashboard:chat-1-1", acq.lease)

            async def drive() -> None:
                task = asyncio.create_task(provider.shutdown())
                # Let the shutdown reach its awaits, then cancel it there.
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            asyncio.run(drive())

            assert live.leases_on_runtime(rt) == 1, (
                "the cancelled shutdown left its lease outstanding, so the gate will "
                "refuse every kill of this pid"
            )
            assert provider._runtime_lease is None, "the slot still names a released lease"
            # And the surviving session's own release still ends the process.
            assert asyncio.run(live.release(other.lease)) is rt
            assert handle is not None
        finally:
            ro._reset_for_tests()

    def test_a_sole_owner_session_is_untouched_by_this_arm(self):
        """G1: with sharing off nothing sets the flag, so the old arm still runs.

        The distinguishing observation is the DESTROY: the sole-owner arm keeps a
        persistent session's handle and kills the process, which is the behaviour
        that shipped before a cap existed.
        """
        from kiro_crew.acp.session_provider import AcpSessionProvider

        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9208)
            handle = FakeHandle()
            provider = AcpSessionProvider(
                handle,  # type: ignore[arg-type]
                rt,  # type: ignore[arg-type]
                owns_runtime=True,
                session_key="dashboard:chat-1-1",
            )
            provider.memory_mode = "persistent"

            asyncio.run(provider.shutdown())

            assert handle.destroyed == 0, "the sole-owner arm must not destroy the handle"
            assert len(rt.kills) == 1
        finally:
            ro._reset_for_tests()


# ── What a joining session must not do ──


class TestAFailedStartReleasesBeforeItKills:
    """Where a cleanup owes a release, given that the lease precedes the spawn.

    An eligible chat-share start takes its lease before the spawn, so the window
    between ``start()`` returning and registration holds one. A cancellation
    landing in that window -- the spawn-identity stamp awaits a reader, a real
    suspension point -- reaches a cleanup arm that kills. A hard-kill site earlier
    than the start holds no lease and needs none of this.
    """

    def test_the_gate_refuses_a_kill_while_the_lease_is_outstanding(self):
        """Why killing without releasing does not merely leak the process.

        The gate refuses, so the signal never goes out, and the reconciler's
        unowned sweep skips a leased pid -- nothing reclaims it later.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9400)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))

            assert (
                authorize_runtime_kill(rt, reason="failed start", caller="test") is False
            ), "a kill with the lease still held must be refused"

            assert asyncio.run(live.release(acq.lease)) is rt, "the last release strands it"
            assert (
                authorize_runtime_kill(rt, reason="failed start", caller="test") is True
            ), "released first, the same kill is authorized"
        finally:
            ro._reset_for_tests()

    def test_the_post_start_cleanup_releases_before_it_kills(self):
        """Structural, and deliberately ORDER-sensitive.

        Driving the real arm needs a live registry, a real spawn and an identity
        reader that raises mid-await. What a later edit breaks is the order and the
        shielding, both of which read off the source.
        """
        import re
        from pathlib import Path

        import kiro_crew.session_allocation as alloc_mod

        source = Path(alloc_mod.__file__).read_text(encoding="utf-8")
        # Anchored on the comment that names THIS arm: there are three stamp call
        # sites in the file and only this one is past a chat-share acquisition.
        anchor = "suspension point between start() and PID registration"
        assert source.count(anchor) == 1, "the post-start stamp arm's anchor moved"
        start = source.index(anchor)
        # A STANDALONE ``raise`` line ends the arm. Plain ``index("raise")`` would
        # stop inside the prose above it, which says "re-raise".
        end = re.search(r"^[ \t]*raise[ \t]*$", source[start:], re.MULTILINE)
        assert end is not None, "the post-start stamp arm no longer re-raises"
        region = source[start : start + end.start()]
        assert "release_session_lease(provider)" in region, (
            "the post-start cleanup kills without releasing the lease its own "
            "placement took, so the gate refuses the kill and the runtime is "
            "left leased and unreapable"
        )
        # Order, not mere presence: killing first is refused by the gate.
        assert region.index("release_session_lease(provider)") < region.index(
            "_dispatch_hard_kill"
        ), "the cleanup kills before it releases, which the gate refuses"
        # The exception in flight is usually CancelledError, so a bare await would
        # take the next one and never reach the kill.
        assert re.search(r"await asyncio\.shield\(\s*_release\s*\)", region), (
            "the release is unshielded, so a cancellation lands mid-release and "
            "strands the lease this arm exists to give back"
        )


class TestProjectionSkipOnJoin:
    """The one branch that differs for a joining session, driven directly.

    ``_activate_mode_bracketed`` is where the process-wide skill projection is
    rebuilt, and a joining session must not rebuild it. Reached here rather than
    through a whole ``create_session``, so the assertion is about this branch and
    nothing else, and both directions are checked -- a test that only proved the
    skip would pass on an implementation that never projected at all.
    """

    @staticmethod
    def _runtime(monkeypatch, calls):
        import kiro_crew.acp.skill_projection as proj_mod
        from kiro_crew.acp.runtime import AcpRuntime

        class _FakeProjection:
            """Enough of a native skill projection for the set_mode send path:
            ``agent`` translates the mode to its wire alias and ``recognise`` is
            the merge the bracket calls to carry prior aliases forward."""

            def __init__(self, tag):
                self.tag = tag

            def agent(self, mode_agent):
                return f"{mode_agent}@{self.tag}"

            def recognise(self, _other):
                return None

        def fake_prepare(work_dir, *, enabled=None):
            calls.append((str(work_dir), enabled))
            return _FakeProjection("fresh")

        monkeypatch.setattr(proj_mod, "prepare_native_skill_projection", fake_prepare)

        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: "snap")
        monkeypatch.setattr(agent_mod, "require_unchanged_derived_spec", lambda *a, **k: None)

        rt = AcpRuntime.__new__(AcpRuntime)
        rt._work_dir = Path("/home/u/.kirocrew/workspace")
        rt._native_skill_projection = _FakeProjection("already-live")
        rt._spawn_skill_projection = None
        sent: list = []

        async def fake_send(method, params, timeout=None, **kwargs):
            sent.append(method)
            return {}

        rt._send_and_await = fake_send  # type: ignore[method-assign]
        rt.sent = sent  # type: ignore[attr-defined]

        # This branch's error arm tears the session down, and that path walks far
        # more runtime state than a hand-built instance carries. Recorded rather
        # than stubbed attribute by attribute: each test asserts it stayed empty,
        # so a real failure inside the branch reads as "the error arm ran" instead
        # of as whichever teardown attribute the stub happened to lack first.
        torn: list = []

        async def fake_terminate(session_id):
            torn.append(session_id)

        rt.terminate_session = fake_terminate  # type: ignore[method-assign]
        rt.torn = torn  # type: ignore[attr-defined]
        return rt

    def test_a_joining_session_does_not_rebuild_the_projection(self, monkeypatch):
        calls: list = []
        rt = self._runtime(monkeypatch, calls)

        asyncio.run(
            rt._activate_mode_bracketed(
                "sid-1",
                "kirocrew",
                budget=1.0,
                payload_snapshot="snap",
                wire_registered=True,
                skip_projection_refresh=True,
            )
        )

        assert calls == [], "a joining session rewrote the process-wide projection"
        assert not rt.torn, f"the branch failed and tore the session down: {rt.torn}"
        assert rt.sent, "set_mode was not sent, so the branch under test was not reached"

    def test_a_founding_session_does_rebuild_it(self, monkeypatch):
        calls: list = []
        rt = self._runtime(monkeypatch, calls)

        asyncio.run(
            rt._activate_mode_bracketed(
                "sid-1",
                "kirocrew",
                budget=1.0,
                payload_snapshot="snap",
                wire_registered=True,
                skip_projection_refresh=False,
            )
        )

        assert len(calls) == 1, "the founding session must still refresh the projection"
        assert calls[0][1] is True

    def test_the_default_is_to_refresh(self, monkeypatch):
        """An unwired caller keeps the behaviour that shipped, not the skip."""
        calls: list = []
        rt = self._runtime(monkeypatch, calls)

        asyncio.run(
            rt._activate_mode_bracketed(
                "sid-1",
                "kirocrew",
                budget=1.0,
                payload_snapshot="snap",
                wire_registered=True,
            )
        )

        assert len(calls) == 1


# ── The reset path must not reap a co-tenant's process ──


class TestResetLeavesACoTenantsProcessAlone:
    """A shared process outliving one session's shutdown is not a survivor.

    ``session_lifecycle`` SIGKILLs a pid that is still alive after shutdown and
    sweeps its escaped children. Under a cap that pid can be a live process other
    sessions are mid-turn on, and the sweep would take the MCP servers they are
    using with it.
    """

    def test_a_lease_with_no_registered_session_still_holds_the_pid(self):
        """A joiner holds its lease before it is a registered session.

        ``provider.start`` takes the lease and only RETURNS afterwards, so a
        co-tenant that is still starting is invisible to the live session table
        for the whole of a multi-second cold start. The guard has to see it
        anyway, or it force-kills a shared process out from under a session that
        is mid-start -- which is why it asks the registry, where a starting
        session is already recorded, rather than the table, where it is not.
        """
        from kiro_crew.session_lifecycle import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48271)
            acq = asyncio.run(
                ro.RUNTIME_OWNERSHIP.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=25)
            )
            assert _pid_is_still_held(48271) is True
            assert _pid_is_still_held(48272) is False

            # Once the last lease goes the pid stops being held, so a process
            # that really did outlive its only session is still reapable.
            asyncio.run(ro.RUNTIME_OWNERSHIP.release(acq.lease))
            assert _pid_is_still_held(48271) is False
        finally:
            ro._reset_for_tests()

    def test_a_dead_runtimes_leases_do_not_shield_its_pid(self):
        """The opposite hole: a crashed process must still be reaped and swept."""
        from kiro_crew.session_lifecycle import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48273)
            asyncio.run(
                ro.RUNTIME_OWNERSHIP.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=25)
            )
            rt.die()
            assert (
                _pid_is_still_held(48273) is False
            ), "a dead runtime's stale leases would strand its escaped children"
        finally:
            ro._reset_for_tests()

    def test_a_subagent_mid_turn_on_the_pid_also_holds_it(self):
        """A tenancy is a holder too: the gate refuses a kill for one.

        A subagent sharing the process holds no lease -- a turn tenancy neither
        consumes the cap nor moves a session -- so a guard that counted leases
        alone would reap a process a subagent is streaming on.
        """
        from kiro_crew.session_lifecycle import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48274)
            handle = ro.claim_runtime_tenancy(rt, holder="subagent:test")
            assert handle is not None
            assert _pid_is_still_held(48274) is True

            ro.release_runtime_tenancy(handle)
            assert _pid_is_still_held(48274) is False
        finally:
            ro._reset_for_tests()

    def test_an_unreadable_registry_leaves_the_pid_reapable(self, caplog):
        """A bookkeeping failure must not turn into a permanent leak."""
        from kiro_crew.session_lifecycle import _pid_is_still_held

        def explode(_pid):
            raise RuntimeError("registry unreadable")

        with caplog.at_level(logging.DEBUG):
            assert _pid_is_still_held(5153, holders=explode) is False

    def test_the_guard_is_wired_into_the_reset_path(self):
        """The helper being right is not the same as the reset path calling it.

        Structural, and named as such: driving a whole reset needs the lifecycle
        service's entire dependency set, while what a later edit breaks is the
        call and the two things it gates -- the force-kill and the child sweep.
        """
        import re
        from pathlib import Path

        import kiro_crew.session_lifecycle as lifecycle_mod

        source = Path(lifecycle_mod.__file__).read_text(encoding="utf-8")
        assert "shared_with_others = _pid_is_still_held(" in source
        assert re.search(r"if shared_with_others:", source), "the force-kill is not gated"
        assert re.search(
            r"if child_pids and not shared_with_others:", source
        ), "the child sweep is not gated, so it would take a co-tenant's MCP servers"


class _GateRuntime(FakeRuntime):
    """A runtime whose ``chat_turn_gate`` is the real one-lock serializer.

    Records how many gate holders are inside at once so a test can prove the
    lock actually serializes rather than merely being awaited.
    """

    def __init__(self, pid: int = 7000) -> None:
        super().__init__(pid=pid)
        self._chat_turn_lock = asyncio.Lock()
        self.max_concurrent = 0
        self._inside = 0

    def chat_turn_gate(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def _gate():
            async with self._chat_turn_lock:
                self._inside += 1
                self.max_concurrent = max(self.max_concurrent, self._inside)
                try:
                    yield
                finally:
                    self._inside -= 1

        return _gate()


def _provider_for_gate(runtime, *, shared_runtime: bool):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider(
        FakeHandle(),  # type: ignore[arg-type]
        runtime,
        owns_runtime=True,
        runtime_lease="lease" if shared_runtime else None,
        shared_runtime=shared_runtime,
        session_key="k",
    )


class TestChatTurnsAreSerializedOnASharedProcess:
    """An ownerless control frame is unambiguous only while ONE chat turn runs.

    The reader loop routes a no-sessionId compaction / clear / agent-switch to
    the sessions with an active turn; with two chat turns overlapping it would
    fan to both and a peer's control event corrupts a co-tenant. So a shared
    chat session holds the per-process turn gate for its whole turn, and a
    sub-agent (shared_runtime=False) is kept OUT of it — contending for the lock
    the principal holds would deadlock the principal→sub-agent await.
    """

    @pytest.mark.asyncio
    async def test_two_shared_chat_turns_do_not_overlap(self):
        rt = _GateRuntime()
        a = _provider_for_gate(rt, shared_runtime=True)
        b = _provider_for_gate(rt, shared_runtime=True)

        order: list[str] = []
        b_may_finish = asyncio.Event()

        async def run_a():
            async with a._chat_turn_gate():
                order.append("a-in")
                # Hold the gate until the test releases it, so b can only be
                # waiting — never inside — while a holds it.
                await b_may_finish.wait()
                order.append("a-out")

        async def run_b():
            # Let a acquire first.
            await asyncio.sleep(0)
            async with b._chat_turn_gate():
                order.append("b-in")

        ta = asyncio.create_task(run_a())
        tb = asyncio.create_task(run_b())
        await asyncio.sleep(0.02)
        # a is inside and holding; b is blocked on the lock, not inside.
        assert order == ["a-in"], order
        assert rt.max_concurrent == 1
        b_may_finish.set()
        await asyncio.gather(ta, tb)
        assert order == ["a-in", "a-out", "b-in"], order
        assert rt.max_concurrent == 1, "two shared chat turns were inside the gate at once"

    @pytest.mark.asyncio
    async def test_a_subagent_turn_does_not_contend_for_the_gate(self):
        """A non-shared (sub-agent) session never touches the lock.

        If it did, a sub-agent whose principal already holds the gate could
        never enter — the deadlock the exemption exists to prevent. Here the
        principal holds the gate and the sub-agent's gate still enters freely.
        """
        rt = _GateRuntime()
        principal = _provider_for_gate(rt, shared_runtime=True)
        subagent = _provider_for_gate(rt, shared_runtime=False)

        async with principal._chat_turn_gate():
            assert rt._chat_turn_lock.locked(), "principal did not take the shared lock"
            # The sub-agent's gate must not block on the principal's held lock.
            entered = False
            async with subagent._chat_turn_gate():
                entered = True
            assert entered, "a sub-agent turn was blocked by the principal's turn gate"
        assert not rt._chat_turn_lock.locked()

"""Tests for kiro_crew.security: keystone leaves and the stores the agent may not write.

A keystone leaf's atomic-write temp and lock sibling, ``~/.kiro/agents`` and
``~/.kiro/settings/mcp.json``, the denied-command opt-out file, the cron store, downloaded model
weights and the identity/auth SQLite store, each on the read or write fence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import cron_inflight, platform_compat, security
from kiro_crew.security import is_sensitive_bash_command, is_sensitive_path


class TestKeystonePublishArtifacts:
    """A keystone leaf's atomic-write temp and lock sibling are on the floor too.

    ``atomic_write`` publishes every keystone leaf through a
    ``tempfile.mkstemp(dir=path.parent, suffix=".tmp")`` sibling and renames it over the
    target, and several stores take a lock file beside the leaf they guard. The temp
    holds the leaf's FULL payload for the duration of the write, so the path gate must
    refuse it -- fencing the final name alone left the publish path outside the fence.
    """

    # ── the real shapes, on the tool path ──

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_mkstemp_temp_in_the_crew_root_is_fenced(self, prefix: str) -> None:
        """The shape atomic_write ACTUALLY produces: a random name, no leaf in it."""
        assert is_sensitive_path(f"~/{prefix}/tmpAB12CD34.tmp") is True

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_lock_siblings_in_the_crew_root_are_fenced(self, prefix: str) -> None:
        # .policy.lock guards the ops autonomy ceiling; the *.json.lock form is the
        # ops secrets store's; .crons.lock is the cron store's.
        assert is_sensitive_path(f"~/{prefix}/.policy.lock") is True
        assert is_sensitive_path(f"~/{prefix}/ops_mission_control_secrets.json.lock") is True
        assert is_sensitive_path(f"~/{prefix}/.crons.lock") is True

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_leaf_suffixed_temp_is_fenced(self, prefix: str) -> None:
        """The shape measured earlier, kept even though no writer emits it.

        Covered by the same suffix rule at no extra cost, and a hand-rolled writer
        adopting this convention later inherits the protection.
        """
        assert is_sensitive_path(f"~/{prefix}/computer_use.json.tmp") is True
        assert is_sensitive_path(f"~/{prefix}/security_policy.json.tmp") is True
        assert is_sensitive_path(f"~/{prefix}/token_signing.key.tmp") is True
        assert is_sensitive_path(f"~/{prefix}/.env.tmp") is True

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_artifact_beside_a_nested_leaf_is_fenced(self, prefix: str) -> None:
        """The rule follows the leaf, so a leaf outside the root is covered as well."""
        assert is_sensitive_path(f"~/{prefix}/workspace/md-notebook/One.md.abcd.tmp") is True

    def test_the_write_gate_stays_a_superset(self) -> None:
        """is_sensitive_write_path is documented as a superset, so it must agree."""
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/crew/tmpAB12CD34.tmp") is True
        assert is_sensitive_write_path("~/.kiro/crew/.policy.lock") is True

    def test_the_dollar_addition_does_not_over_block(self) -> None:
        """A ``$`` in a command is not a verdict; the shell gate matches no paths."""
        assert is_sensitive_bash_command("echo $HOME") is None
        assert is_sensitive_bash_command("cat ~/project/notes.txt") is None
        assert is_sensitive_bash_command("VAR=$HOME cat ~/project/notes.txt") is None
        assert is_sensitive_bash_command("cd $HOME && ls") is None
        assert is_sensitive_bash_command("cat ~/.kiro/crew/config.json") is None

    def test_the_alias_tolerance_still_rejects_a_different_file(self) -> None:
        """The lookahead admits a trailing separator or dot, never a longer NAME.

        This is the boundary that keeps the tolerance from becoming a wildcard: a file
        whose name merely starts with an artifact name is a different file.
        """
        assert is_sensitive_bash_command("cat ~/.kiro/crew/tmpAB12CD34.tmpx") is None
        assert is_sensitive_bash_command("cat ~/.kiro/crew/tmpAB12CD34.tmp-old") is None
        assert is_sensitive_bash_command("cat ~/project/build.tmpl") is None
        assert is_sensitive_bash_command("cat ~/project/yarn.lock") is None

    # ── it must not over-block ──

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_routine_crew_root_reads_still_allowed(self, prefix: str) -> None:
        """The reason the crew root cannot simply be fenced wholesale."""
        assert is_sensitive_path(f"~/{prefix}/config.json") is False
        assert is_sensitive_path(f"~/{prefix}/sessions.db") is False
        assert is_sensitive_path(f"~/{prefix}/notes.txt") is False

    def test_the_users_own_home_is_not_swept(self) -> None:
        """The parent set is derived from the CREW leaves, not from every sensitive path.

        ``_SENSITIVE_HOME_DIRS`` also carries ``.aws``, ``.ssh`` and the identity
        stores, whose parent is ``$HOME`` itself -- deriving the artifact parents from
        that list would fence every ``*.tmp`` and ``*.lock`` in the user's home.
        """
        assert is_sensitive_path("~/scratch.tmp") is False
        assert is_sensitive_path("~/yarn.lock") is False
        assert is_sensitive_path("~/project/yarn.lock") is False
        assert is_sensitive_bash_command("cat ~/project/yarn.lock") is None
        assert is_sensitive_bash_command("npm ci --prefer-offline") is None

    def test_the_parent_is_matched_by_equality_not_prefix(self) -> None:
        """An artifact is a DIRECT child of the leaf's directory.

        A prefix test would sweep every descendant of the crew home whose name ends in
        ``.tmp`` -- much wider than this needs, in a directory that must stay readable.
        """
        assert is_sensitive_path("~/.kiro/crew/sub/deeper/x.tmp") is False
        assert is_sensitive_bash_command("cat ~/.kiro/crew/sub/deeper/x.tmp") is None

    def test_a_directory_without_a_keystone_leaf_is_out_of_scope(self) -> None:
        """``deploy/`` takes a lock but holds no keystone leaf.

        There is no keystone payload beside it for the fence to protect, so it is
        deliberately excluded rather than swept in by proximity.
        """
        assert is_sensitive_path("~/.kiro/crew/deploy/pending-deploys.lock") is False

    def test_the_leaves_themselves_are_still_fenced(self) -> None:
        """No regression: the artifact clause is additive."""
        assert is_sensitive_path("~/.kiro/crew/computer_use.json") is True
        assert is_sensitive_path("~/.kiro/crew/security_policy.json") is True
        assert is_sensitive_path("~/.kiro/crew/.env") is True
        assert is_sensitive_path("~/.kiro/crew/webhooks/tokens.json") is True

    def test_a_relocated_crew_home_is_covered(self, tmp_path, monkeypatch) -> None:
        """KIROCREW_HOME re-anchoring is inherited, not reimplemented.

        The keystone leaves live directly under a custom ``KIROCREW_HOME``, so the
        artifact rule has to follow them there or the fence is bypassed by setting the
        env var. Covered because a ``<crew-prefix>``-rooted entry hits the
        prefix-stripping arm in ``_home_dir_targets_uncached``.
        """
        relocated = tmp_path / "custom-crew-home"
        relocated.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(relocated))
        assert is_sensitive_path(str(relocated / "tmpAB12CD34.tmp")) is True
        assert is_sensitive_path(str(relocated / ".policy.lock")) is True
        # ...and the over-block guard holds there too.
        assert is_sensitive_path(str(relocated / "config.json")) is False

    def test_a_symlink_aimed_at_a_live_temp_is_caught(self, tmp_path, monkeypatch) -> None:
        """The resolved candidate form is checked, so a benign link name does not help."""
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        temp = crew / "tmpAB12CD34.tmp"
        temp.write_text("secret-payload-mid-write\n")
        # Path.home() reads HOME on POSIX and USERPROFILE on Windows, and the gate anchors
        # its targets on Path.home() -- so setting only HOME leaves the Windows anchor on
        # the real profile and the fake home below is never recognised. Set both.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        ws = tmp_path / "workspace"
        ws.mkdir()
        link = ws / "notes.txt"
        link.symlink_to(temp)
        assert is_sensitive_path(str(link)) is True


class TestKiroAgentsDirWriteProtection:
    """``~/.kiro/agents`` is WRITE-protected on the file-edit tool gate.

    A spec planted there names a ``command`` the MCP gateway execs — a pooled
    backend runs OUTSIDE the per-session sandbox, as the user — so an agent write
    is a persistent, unsandboxed code-exec vector. WRITES are refused. Tool-path
    READS stay allowed (the dir is on the write-only tier, NOT in
    ``_SENSITIVE_HOME_DIRS``), so spec discovery / the dashboard MCP rows work.
    The shell is not matched on command text; the OS sandbox is the shell-side
    control, as for every other write-protected entry.
    """

    def test_directory_is_tail_of_kiro_agents_dir(
        self, monkeypatch, unpinned_agent_spec_home
    ) -> None:
        # Drift guard: the literal in security.py must stay the home-relative tail
        # of config.paths.kiro_agents_dir() (kept a literal only to avoid a
        # config->security import cycle). If kiro-cli's layout moves, this fails
        # loudly instead of silently un-fencing the dir.
        #
        # Resolve under the DEFAULT home: KIRO_HOME can point outside $HOME (the
        # override case), and ``relative_to(Path.home())`` raises ValueError then.
        # The literal is the home-relative default tail, so the assertion is about
        # the default home; clear the overrides to make it deterministic.
        #
        # ``unpinned_agent_spec_home`` for the same reason: the rootdir floor points
        # the resolver at a per-test tmp dir, which has no home-relative tail to
        # compare. The claim under test is about the REAL default layout.
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        from kiro_crew.config.paths import kiro_agents_dir

        rel = kiro_agents_dir().relative_to(Path.home()).as_posix()
        assert security._KIRO_AGENTS_DIR == rel

    def test_file_edit_write_into_agents_dir_is_denied(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        home = str(Path.home())
        # Any filename (specs can be named anything), any depth, and the dir itself.
        assert is_sensitive_write_path("~/.kiro/agents/pwn.json") is True
        assert is_sensitive_write_path("~/.kiro/agents/anything.json") is True
        assert is_sensitive_write_path("~/.kiro/agents/sub/deep.json") is True
        assert is_sensitive_write_path("~/.kiro/agents") is True
        assert is_sensitive_write_path(f"{home}/.kiro/agents/pwn.json") is True

    def test_reads_of_agents_dir_stay_allowed(self) -> None:
        # WRITE-protection only: the read+write gate (is_sensitive_path) must NOT
        # fence the agents dir, or spec discovery / the dashboard MCP rows break.
        assert is_sensitive_path("~/.kiro/agents/pwn.json") is False
        assert is_sensitive_path("~/.kiro/agents") is False

    def test_sibling_dirs_are_not_over_blocked(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        # ``agents-backup`` shares a prefix but is a different directory.
        assert is_sensitive_write_path("~/.kiro/agents-backup/x.json") is False
        # A kiro-cli settings leaf this entry does not reason about stays writable;
        # the registry leaf beside it has its own entry and its own test class.
        assert is_sensitive_write_path("~/.kiro/settings/amazon-internal.json") is False
        assert is_sensitive_write_path("~/notes.txt") is False

    def test_tool_gate_canonicalizes_relative_writes_into_agents_dir(self) -> None:
        # The control is the file-edit tool gate, which CANONICALIZES the
        # destination: a relative target that resolves into the fenced dir is
        # refused regardless of spelling, and one that resolves elsewhere is not
        # over-blocked.
        from kiro_crew.security import is_sensitive_write_path

        home = str(Path.home())
        # Relative target anchored at ~/.kiro resolves to ~/.kiro/agents/pwn.json.
        assert is_sensitive_write_path("agents/pwn.json", base_dir=f"{home}/.kiro") is True
        assert is_sensitive_write_path("./agents/pwn.json", base_dir=f"{home}/.kiro") is True
        # A relative write whose canonical destination is NOT the user-level agents
        # dir (e.g. a project checkout) must stay allowed — no false fence.
        assert is_sensitive_write_path("agents/pwn.json", base_dir="/tmp/project") is False

    def test_kiro_home_override_is_covered_on_the_tool_gate(self, tmp_path, monkeypatch) -> None:
        # kiro_agents_dir() honours KIRO_HOME; the override moves the specs the
        # gateway execs, so the write gate must follow it (re-anchored the same way
        # KIROCREW_HOME re-anchors the crew secrets). The default ~/.kiro/agents
        # stays covered regardless.
        from kiro_crew.security import is_sensitive_write_path

        custom = tmp_path / "customkiro"
        monkeypatch.setenv("KIRO_HOME", str(custom))
        security._home_targets_cache.clear()
        target = str(custom / "agents" / "pwn.json")
        assert is_sensitive_write_path(target) is True
        # Reads under the override stay allowed (write-only tier).
        assert is_sensitive_path(target) is False

    def test_kiro_home_unset_does_not_protect_the_override_location(
        self, tmp_path, monkeypatch
    ) -> None:
        # The re-anchoring is keyed on the resolved KIRO_HOME, so clearing it must
        # invalidate the cached target set — otherwise a stale override would keep
        # fencing an unrelated path.
        from kiro_crew.security import is_sensitive_write_path

        custom = tmp_path / "customkiro"
        monkeypatch.delenv("KIRO_HOME", raising=False)
        security._home_targets_cache.clear()
        assert is_sensitive_write_path(str(custom / "agents" / "pwn.json")) is False


class TestKiroSettingsMcpJsonWriteProtection:
    """``~/.kiro/settings/mcp.json`` is WRITE-protected on the file-edit tool gate.

    An ``autoApprove`` on an entry there is honoured by default
    (``mcp.honour_auto_approve``), and kiro-cli approves an autoApproved MCP tool
    locally without emitting a permission request — so those verbs skip Kiro Crew's
    tool gate. ``governance._is_owner_written`` admits an entry on its NAME SHAPE,
    not on who wrote the file, so a plainly-named entry an agent appended is
    honoured exactly like one the owner typed. WRITES are refused; tool-path READS
    stay allowed, because the registry is read by the app MCP-policy merge, the
    doctor and the deregistration scrub.
    """

    def test_file_edit_write_to_the_registry_is_denied(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        home = str(Path.home())
        assert is_sensitive_write_path("~/.kiro/settings/mcp.json") is True
        assert is_sensitive_write_path(f"{home}/.kiro/settings/mcp.json") is True

    def test_reads_of_the_registry_stay_allowed(self) -> None:
        # WRITE-protection only: the read+write gate must NOT fence the registry, or
        # the policy merge and the doctor's report break.
        assert is_sensitive_path("~/.kiro/settings/mcp.json") is False

    def test_neighbours_are_not_over_blocked(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        # A LEAF, not the directory: ``settings`` holds other kiro-cli files whose
        # protection this entry has not reasoned about.
        assert is_sensitive_write_path("~/.kiro/settings") is False
        assert is_sensitive_write_path("~/.kiro/settings/amazon-internal.json") is False
        # Suffix and prefix neighbours are different files.
        assert is_sensitive_write_path("~/.kiro/settings/mcp.json.bak") is False
        assert is_sensitive_write_path("~/.kiro/settings-backup/mcp.json") is False

    def test_tool_gate_canonicalizes_relative_writes_to_the_registry(self) -> None:
        from kiro_crew.security import is_sensitive_write_path

        home = str(Path.home())
        assert is_sensitive_write_path("mcp.json", base_dir=f"{home}/.kiro/settings") is True
        assert is_sensitive_write_path("./settings/mcp.json", base_dir=f"{home}/.kiro") is True
        # A relative write resolving somewhere else must stay allowed.
        assert is_sensitive_write_path("settings/mcp.json", base_dir="/tmp/project") is False

    def test_a_leaf_added_to_the_tuple_gets_both_halves(self, tmp_path, monkeypatch) -> None:
        # The tuple drives the KIRO_HOME re-anchoring, and its $HOME half is the
        # resolve loop over every home_dirs entry, so a third kiro-cli leaf is one
        # tuple entry and not a third arm. Pinned by adding one, because the failure
        # mode is silent: the leaf would be fenced under $HOME and writable under
        # the override.
        from kiro_crew.security import is_sensitive_write_path

        home = tmp_path / "home"
        override = tmp_path / "kirohome"
        (override / "settings").mkdir(parents=True)
        home.mkdir()
        probe_leaf = ".kiro/settings/probe.json"

        monkeypatch.setattr(
            security.paths,
            "_KIRO_CLI_WRITE_TIER_LEAVES",
            (*security.paths._KIRO_CLI_WRITE_TIER_LEAVES, probe_leaf),
        )
        monkeypatch.setattr(
            security.paths,
            "_WRITE_PROTECTED_HOME_PATHS",
            [*security.paths._WRITE_PROTECTED_HOME_PATHS, probe_leaf],
        )
        # Path.home() reads HOME on POSIX and USERPROFILE on Windows; set both.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("KIRO_HOME", str(override))
        security._home_targets_cache.clear()

        assert is_sensitive_write_path(str(home / probe_leaf)) is True
        assert is_sensitive_write_path(str(override / "settings" / "probe.json")) is True

    @pytest.mark.parametrize(
        ("linked", "target_tail"),
        [
            # A dotfile-managed home links the directory, not the file.
            (".kiro/settings", "mcp.json"),
            # And the whole kiro dir is the other shape of the same case.
            (".kiro", "settings/mcp.json"),
        ],
    )
    def test_a_symlink_below_the_home_root_does_not_move_the_registry_out(
        self, linked, target_tail, tmp_path, monkeypatch
    ) -> None:
        # Anchoring the leaf lexically covers a symlinked ``$HOME`` ITSELF but not
        # one further down the path. Without resolving the ``$HOME``-rooted form the
        # ``~``-spelled path stays fenced while the REAL file is writable, and an
        # agent simply writes the destination instead.
        from kiro_crew.security import is_sensitive_write_path

        home = tmp_path / "home"
        store = tmp_path / "dotfiles" / "kirostore"
        # The store holds whatever the link stands in for, so the real file exists
        # at ``<store>/<target_tail>`` in both shapes.
        real = store / target_tail
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("{}\n", encoding="utf-8")

        link = home / linked
        link.parent.mkdir(parents=True, exist_ok=True)
        # A DIRECTORY redirect, so this stays ordinary cross-platform coverage
        # instead of joining ``test/requires-real-symlinks.txt``: a plain
        # ``os.symlink`` needs a privilege the non-admin Windows runner lacks,
        # while a junction needs none and is followed by ``realpath`` -- which is
        # exactly the resolution under test.
        platform_compat.symlink_or_junction(store, link)

        # Path.home() reads HOME on POSIX and USERPROFILE on Windows, and the
        # gate anchors its targets on Path.home() -- so set both or the Windows
        # anchor stays on the real profile and the fake home is never recognised.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        security._home_targets_cache.clear()

        # The spelling the owner sees stays protected...
        assert is_sensitive_write_path(str(home / ".kiro/settings/mcp.json")) is True
        # ...and so does the real file the symlink points at.
        assert is_sensitive_write_path(str(real)) is True

    def test_kiro_home_override_is_covered_on_the_tool_gate(self, tmp_path, monkeypatch) -> None:
        # ``KIRO_HOME`` moves kiro-cli's whole user directory including ``settings``,
        # so the registry the override makes live must be fenced too. The default
        # ``$HOME``-rooted location stays covered regardless.
        from kiro_crew.security import is_sensitive_write_path

        custom = tmp_path / "customkiro"
        monkeypatch.setenv("KIRO_HOME", str(custom))
        security._home_targets_cache.clear()
        target = str(custom / "settings" / "mcp.json")
        assert is_sensitive_write_path(target) is True
        # Reads under the override stay allowed (write-only tier).
        assert is_sensitive_path(target) is False
        assert is_sensitive_write_path(f"{Path.home()}/.kiro/settings/mcp.json") is True

    def test_kiro_home_unset_does_not_protect_the_override_location(
        self, tmp_path, monkeypatch
    ) -> None:
        # The re-anchoring is keyed on the resolved override, so clearing it must
        # invalidate the cached target set.
        from kiro_crew.security import is_sensitive_write_path

        custom = tmp_path / "customkiro"
        monkeypatch.delenv("KIRO_HOME", raising=False)
        security._home_targets_cache.clear()
        assert is_sensitive_write_path(str(custom / "settings" / "mcp.json")) is False


class TestDeniedCommandsKeystone:
    """The denied-command opt-out file is a KEYSTONE trust root.

    The opt-out state (``{disable_all, disabled_ids, user_added}``) lives in
    ``~/.kirocrew/denied_commands.json`` on ``_SENSITIVE_HOME_DIRS`` — a full
    read+write block — NOT in config.json. So the agent's file tools can neither
    read nor write its own deny ceiling, inheriting the mature
    ``is_sensitive_path`` gate (the same protection level as
    ``security_policy.json``), and the OS sandbox mounts it read-only for the
    shell. This replaces the bespoke bash write-matcher that was needed while the
    state lived in the agent-readable config.json.
    """

    def test_keystone_path_is_sensitive(self) -> None:
        from kiro_crew.security import is_sensitive_path

        assert is_sensitive_path("~/.kirocrew/denied_commands.json") is True


class TestCronStoreProtection:
    """The cron store is a keystone leaf.

    ``crons.json`` holds access-control state, not just scheduling data:
    ``session_key`` decides which session may manage a job (and where its output
    goes), ``approval_mode`` is a per-job auto-approval decision, and
    ``command``/``script`` is scheduled host execution. The MCP cron tools
    deliberately cannot write ``session_key`` and ``self-protection-cron-adopt``
    blocks the CLI spelling of that write — but while the store sat outside the
    protected leaves, an auto-approved shell could bypass both with an ordinary
    file edit. It is on ``_CREW_SECRET_LEAVES`` with its ``cron-history``
    sidecar directory (per-job records plus the index), read+write-blocked on
    the tool path and hidden from the shell by the OS sandbox. The gateway's own
    writers open the
    store directly, not through this gate, so the cron service keeps working;
    the cost is that a human hand-edit through an agent shell is refused, the
    same trade-off every other keystone leaf makes.
    """

    def test_leaf_membership(self) -> None:
        # Drift guard: a rename of the store or sidecar dir in cron_service/store.py /
        # cron_history.py without a matching entry here would silently
        # un-fence them.
        from kiro_crew.security import _CREW_SECRET_LEAVES

        assert "crons.json" in _CREW_SECRET_LEAVES
        assert "cron-history" in _CREW_SECRET_LEAVES
        # The in-flight markers are the evidence the boot-time loop-stall breaker
        # pauses a job on, so they are fenced for a sharper reason than the store
        # itself: a marker the agent could write is an unauthorized "pause this
        # job", and one it could delete disables the breaker.
        assert cron_inflight.RUNNING_DIR_NAME in _CREW_SECRET_LEAVES

    @pytest.mark.parametrize("prefix", [".kiro/crew", ".kirocrew"])
    def test_store_and_history_sensitive_under_every_home_prefix(self, prefix: str) -> None:
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_path(f"~/{prefix}/crons.json") is True
        assert is_sensitive_path(f"~/{prefix}/cron-history/_index.jsonl") is True
        assert is_sensitive_path(f"~/{prefix}/cron-history/job123.jsonl") is True
        assert is_sensitive_path(f"~/{prefix}/cron-running/a1b2c3d4.json") is True
        # The write gate is a superset of the read gate; assert it directly so
        # the file-edit tool path is pinned too.
        assert is_sensitive_write_path(f"~/{prefix}/crons.json") is True
        assert is_sensitive_write_path(f"~/{prefix}/cron-history/_index.jsonl") is True
        assert is_sensitive_write_path(f"~/{prefix}/cron-running/a1b2c3d4.json") is True
        assert (
            is_sensitive_write_path(f"~/{prefix}/cron-running/{cron_inflight.BREAKER_CLAIM_FILE}")
            is True
        )

    def test_sibling_cron_names_are_not_over_blocked(self) -> None:
        from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

        # Shared-prefix names a shell might legitimately touch elsewhere.
        assert is_sensitive_path("~/projects/crontab.txt") is False
        assert is_sensitive_write_path("~/projects/crontab.txt") is False
        assert is_sensitive_path("~/.kiro/crew/workspace/crons.json.bak") is False


class TestModelWeightsAreWriteProtected:
    """Downloaded weights are an input to a trust decision, so the agent cannot write them.

    Each store verifies its file against a pinned sha256 and then hands the PATH to a
    native loader, so a writable directory leaves a window between the digest and the
    open in which the bytes can be swapped. Re-hashing does not close it, because the
    loader re-opens by name; removing the writability does. A poisoned model is
    persistent and invisible, and for speech it means the user's own words reaching the
    agent as something they did not say.

    Paths are spelled ``~``-relative rather than derived from ``models_dir()``: the
    conftest pins ``KIROCREW_HOME`` to a per-test temp directory, which is deliberately
    NOT under the fenced home, so a derived path would test the fixture instead of the
    fence.
    """

    #: Both stores land under the same parent, so one directory entry covers them.
    MODEL_PATHS = (
        "~/.kiro/crew/models/whisper/ggml-base.bin",
        "~/.kiro/crew/models/qwen3-embedding-0.6b.gguf",
        "~/.kirocrew/models/whisper/ggml-base.bin",
    )

    @pytest.mark.parametrize("path", MODEL_PATHS)
    def test_the_file_tool_gate_refuses_a_write(self, path: str) -> None:
        assert security.is_sensitive_write_path(path) is True, path

    @pytest.mark.parametrize("path", MODEL_PATHS)
    def test_reads_stay_allowed_at_the_tool_gate(self, path: str) -> None:
        """Write-protected, NOT read+write sensitive: the settings surface and
        `kirocrew doctor` both read the directory to report what is installed, and the
        weights hold no secret."""
        assert security.is_sensitive_path(path) is False, path

    @pytest.mark.parametrize(
        "command",
        (
            # A name that merely ENDS with a weight name stays allowed, the same
            # boundary rule the alias record documents.
            "cp my-ggml-base.bin /tmp/",
            # An unrelated `.bin`, and an unrelated directory called `models`.
            "cp firmware.bin /tmp/",
            "cp /tmp/e models/a.bin",
            "cd models && ls",
            "grep -r models src/",
            # Ordinary punctuation-separated commands, so widening the terminator class
            # did not turn every `;` into a refusal.
            "cd ~/Documents; ls",
            "git status; git diff",
        ),
    )
    def test_the_widened_boundary_does_not_refuse_ordinary_commands(self, command: str) -> None:
        """The cost of the two widenings, pinned. Both are deny-list widenings, so the
        only way they can be wrong is by refusing something ordinary."""
        assert security.is_sensitive_bash_command(command) is None, command

    def test_an_unrelated_path_named_models_is_not_fenced(self) -> None:
        """Scoped to the crew home, so an ordinary project directory is unaffected."""
        assert security.is_sensitive_write_path("~/code/myproject/models/weights.bin") is False


class TestIdentityAuthStoreFence:
    """The identity/auth SQLite store is a keystone leaf under the crew data home.

    ``data.sqlite3`` holds live bearer tokens. The kiro-cli and amazon-q copies are
    fenced by DIRECTORY, which covers their sidecars for free, but the crew data home
    cannot be fenced wholesale (``config.json`` and ``sessions.db`` are routine reads),
    so the store is named as a leaf and its WAL/SHM/journal sidecars are named beside
    it -- a file leaf matches its exact name only, and a sidecar carries the store's
    credential bytes.

    The fence is scoped to the crew data-home prefixes, NOT matched by basename:
    ``data.sqlite3`` is a generic filename, so a basename rule would refuse an
    unrelated application database anywhere under the home directory.
    """

    PREFIXES = (".kiro/crew", ".kirocrew")

    def test_leaf_membership_uses_the_canonical_filename_constant(self) -> None:
        # Drift guard: the leaf is the constant the identity-store readers resolve,
        # so renaming the store cannot un-fence it while the readers keep working.
        from kiro_crew.identity_stores import (
            AUTH_SQLITE_DB,
            AUTH_SQLITE_SIDECAR_SUFFIXES,
        )
        from kiro_crew.security import _CREW_SECRET_LEAVES

        assert AUTH_SQLITE_DB in _CREW_SECRET_LEAVES
        for suffix in AUTH_SQLITE_SIDECAR_SUFFIXES:
            assert f"{AUTH_SQLITE_DB}{suffix}" in _CREW_SECRET_LEAVES

    @pytest.mark.parametrize("prefix", PREFIXES)
    def test_store_and_sidecars_sensitive_under_every_home_prefix(self, prefix: str) -> None:
        from kiro_crew.security import is_sensitive_write_path

        for leaf in (
            "data.sqlite3",
            "data.sqlite3-wal",
            "data.sqlite3-shm",
            "data.sqlite3-journal",
        ):
            assert is_sensitive_path(f"~/{prefix}/{leaf}") is True, leaf
            # The write gate is a superset of the read gate; assert it directly so
            # the file-edit tool path is pinned too.
            assert is_sensitive_write_path(f"~/{prefix}/{leaf}") is True, leaf

    def test_unrelated_databases_are_not_over_blocked(self) -> None:
        """The cost of a basename rule, which this fence deliberately does not pay."""
        from kiro_crew.security import is_sensitive_write_path

        assert is_sensitive_path("~/project/data.sqlite3") is False
        assert is_sensitive_write_path("~/project/data.sqlite3") is False
        for cmd in (
            "cat ~/project/data.sqlite3",
            "sqlite3 ~/src/app/data.sqlite3 .dump",
            # Routine crew-home reads the fence must leave alone.
            "cat ~/.kiro/crew/config.json",
            "cat ~/.kiro/crew/sessions.db",
            "cat ~/.kiro/crew/memory.db",
        ):
            assert is_sensitive_bash_command(cmd) is None, cmd

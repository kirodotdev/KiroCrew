"""The OS-level sandbox mask covers the crew data home's governance tree.

``security.sensitive_home_dirs()`` is the agent-TOOL gate: it is what
``is_sensitive_path`` refuses for a file_read/file_write tool call. The dir lists in
``sandbox.py`` are a separate, OS-level gate, and a spawned shell command reaches a path
fenced only by the first one. These tests pin the reconciliation between them:

* every crew-home entry on the tool gate has one of three sandbox dispositions,
* the ceilings are exposed READ-ONLY rather than hidden, in every mode,
* the deliberate read-write exceptions are exactly the declared set, in every mode.

The third is the one worth failing loudly: an entry that quietly moves from "masked" to
"exception" is a ceiling the agent can rewrite again.
"""

from __future__ import annotations

import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

from kiro_crew import sandbox, security

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher only")

_MODES = ("standard", "cc", "strict")
_CREW_PREFIXES = (".kiro/crew", ".kirocrew")


@pytest.fixture(autouse=True)
def _no_host_ssh_probe(monkeypatch):
    """``_build_launcher_script`` asks the HOST's ``ssh -V`` for accept-new support.

    The mask lists read out of the launcher do not depend on that answer, and a
    real ssh spawned from the test process is a host dependency this module is not
    about. Pinned so no binary runs.
    """
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)


def _home() -> str:
    return os.path.expanduser("~")


def _crew_path(prefix: str, leaf: str) -> str:
    """Spell a crew-home target the way the production builders do.

    They join a SINGLE relative string onto the home, so the forward slashes inside it
    survive and Windows gains exactly one native separator. Joining prefix and leaf as
    separate components instead adds a second one, and the resulting mixed-separator
    string matches nothing the builders emit.
    """
    return os.path.join(_home(), f"{prefix}/{leaf}")


def _launcher_sets(mode: str) -> tuple[set[str], set[str], set[str]]:
    """``(hidden_dirs, readonly, hidden_files)`` as the generated launcher declares them."""
    script = sandbox._build_launcher_script(mode)

    def _grab(name: str) -> set[str]:
        match = re.search(rf"{name} = (\[.*?\])\n", script, re.S)
        assert match, f"{name} missing from the launcher"
        return set(json.loads(match.group(1)))

    return _grab("SENSITIVE_DIRS"), _grab("READONLY_DIRS"), _grab("SENSITIVE_FILES")


def _crew_sensitive_paths() -> list[str]:
    """Absolute crew-data-home paths the tool gate declares sensitive."""
    home = _home()
    return [
        os.path.join(home, rel)
        for rel in security.sensitive_home_dirs()
        if rel.startswith(_CREW_PREFIXES)
    ]


def _expected_exceptions() -> set[str]:
    home = _home()
    return {
        os.path.join(home, f"{prefix}/{leaf}")
        for prefix in _CREW_PREFIXES
        for leaf in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    }


@pytest.mark.parametrize(
    ("leaf", "read_refused"),
    (
        # The canonical run records. Write-protected so an agent cannot rewrite the app
        # owner a cold continuation restores, and READABLE because a run's results are
        # the product working.
        ("subagents", False),
        # Its retained V1 companion, on the read+write floor instead: a legacy binding
        # record carries a raw session key, so the agent may not open one. The edit
        # refusal below is identical either way, which is why both leaves stay in one
        # test and only the read expectation is per-leaf.
        ("member-memory-bindings", True),
    ),
)
def test_run_authority_file_edits_are_refused_on_both_run_roots(
    tmp_path, monkeypatch, leaf, read_refused
):
    from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    target = tmp_path / leaf / "run-one" / "state.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"app":"original-app"}', encoding="utf-8")
    assert security.is_sensitive_path(str(target)) is read_refused
    assert security.is_sensitive_write_path(str(target))
    gate = HookManager(HooksConfig.from_dict({}))
    decision = gate.on_tool_call(
        "Edit run ownership",
        session_key="cli_chat",
        tool_kind="edit",
        raw_params={"path": str(target)},
    )
    assert decision.action == TOOL_DENY
    assert target.read_text(encoding="utf-8") == '{"app":"original-app"}'
    # The gateway's direct writer remains usable; this is not a host chmod.
    target.write_text('{"app":"gateway-app"}', encoding="utf-8")
    assert target.read_text(encoding="utf-8") == '{"app":"gateway-app"}'
    assert not security.is_sensitive_write_path(str(tmp_path / (leaf + "-notes") / "x"))


@pytest.mark.parametrize("mode", _MODES)
def test_member_memory_is_readable_but_not_writable_in_the_sandbox(mode: str) -> None:
    """Named-store integrity leaves reads, credentials and governance unchanged."""
    profile = sandbox._build_seatbelt_profile(mode)
    for prefix in _CREW_PREFIXES:
        memory = _crew_path(prefix, "memory_stores")
        token = _crew_path(prefix, "token_signing.key")
        policy = _crew_path(prefix, "security_policy.json")
        assert f'(deny file-read* (subpath "{memory}"))' not in profile
        assert f'(deny file-write* (literal "{memory}"))' in profile
        assert f'(deny file-write* (subpath "{memory}"))' in profile
        assert f'(deny file-link (subpath "{memory}"))' in profile
        assert security.is_sensitive_path(memory + "/member-one/memory.db")
        assert f'(deny file-read* (subpath "{token}"))' in profile
        assert f'(deny file-write* (subpath "{policy}"))' in profile


@_POSIX_ONLY
@pytest.mark.parametrize("mode", _MODES)
def test_linux_member_memory_is_sealed_without_a_hidden_view(mode: str) -> None:
    hidden, readonly, _files = _launcher_sets(mode)
    for prefix in _CREW_PREFIXES:
        memory = _crew_path(prefix, "memory_stores")
        assert memory not in hidden
        assert memory in readonly
        assert _crew_path(prefix, "token_signing.key") in hidden
        assert _crew_path(prefix, "security_policy.json") in readonly


class TestKeystonesAreSealedInEveryMode:
    """The ceiling files the issue named, on every backend the launcher feeds."""

    #: Named individually rather than looped from the module tuple: the point is that
    #: THESE paths are covered, so a test derived from the same tuple the production
    #: code reads would pass just as happily after someone emptied it.
    KEYSTONES = (
        "subagents",
        "member-memory-bindings",
        "security_policy.json",
        "admission_policy.json",
        "app_admission.json",
        "profiles",
        "denied_commands.json",
        "computer_use.json",
        "oauth_endpoints.json",
        "aws_service_consent.json",
        # Recorded consent to send conversation state to the external decision
        # provider. Same class: the seal is what makes "the owner consents to
        # the egress, never the agent" true.
        "decisions_consent.json",
        # Recorded consent to deliver a scanner-flagged file. Sealing it
        # is the load-bearing half of the whole design: the deny-list tiers can
        # be evaded by runtime path construction, so only a kernel write denial
        # makes "the owner consents, never the agent" true rather than merely
        # intended.
        "file_delivery_consent.json",
        # Gateway-executed browser launcher. Agent shells must read and run it,
        # but a write would choose the binary the unsandboxed gateway executes.
        "playwright-cli",
        # The app dev-mode authorization record: sealing it is what
        # makes the operator-attestation flag unforgeable from an agent shell
        # — a sandboxed process cannot mint a grant however the toggle was
        # spelled.
        "apps/.dev-grants.json",
        # The cloud launcher's config. Its `fargate.image` names the container a launch
        # runs and the task's execution role delivers the model credential into that
        # container, so a write here selects WHO RECEIVES the credential -- which is as
        # powerful as holding it. The file-tool gate alone is not enough: a sandboxed
        # shell's `open(..., "w")` never passes through it, so only the kernel denial
        # this list asserts makes "the owner chooses the image, never the agent" true
        # rather than intended.
        "cloud.json",
        # Downloaded model weights and the speech decoder. Each loader checks a pinned
        # digest and then reopens the file by name, so a sandboxed shell able to write
        # here could swap the bytes in between. The file-edit gate refuses the edit;
        # only the kernel denial asserted here refuses the shell's.
        "models",
    )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", KEYSTONES)
    def test_linux_seals_the_ceiling_read_only(self, mode: str, prefix: str, leaf: str) -> None:
        hidden, readonly, _files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        assert target in readonly, f"{leaf} is writable through the {mode} sandbox"
        # Hiding a ceiling inverts its effect: an absent policy file resolves to the
        # permissive standalone default, and a script cron's ``boot_platform()`` runs
        # inside this namespace.
        assert target not in hidden, f"{leaf} must stay READABLE, not be masked"

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", KEYSTONES)
    def test_macos_denies_writes_to_the_ceiling(self, mode: str, prefix: str, leaf: str) -> None:
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-write* (literal "{target}"))' in profile
        assert f'(deny file-write* (subpath "{target}"))' in profile
        # A hardlink at a non-denied path would otherwise reach the same inode.
        assert f'(deny file-link (subpath "{target}"))' in profile
        assert f'(deny file-read* (subpath "{target}"))' not in profile

    def test_the_model_weights_seal_has_a_target_on_a_fresh_install(self) -> None:
        """Linux binds only a path that exists, and ``models`` is absent until the first
        download finishes, so without pre-creation the seal above would be skipped for
        every sandbox started before then -- the window it exists to close.
        """
        assert "models" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES

    @_POSIX_ONLY
    def test_a_symlinked_models_leaf_refuses_the_spawn(self, tmp_path, monkeypatch):
        target = tmp_path / "models"
        outside = tmp_path / "outside"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([str(target)], []))

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox._materialize_sealable_ceilings()

        assert target.is_symlink(), "the operator must replace the refused link"

    def test_a_real_models_directory_is_accepted(self, tmp_path, monkeypatch):
        target = tmp_path / "models"
        target.mkdir()
        weights = target / "weights.gguf"
        weights.write_bytes(b"gateway-owned weights")
        monkeypatch.setattr(sandbox, "_sealable_absent_ceilings", lambda: ([str(target)], []))

        assert sandbox._materialize_sealable_ceilings() == []
        assert weights.read_bytes() == b"gateway-owned weights"

    @pytest.mark.parametrize("platform", ("darwin", "win32"))
    def test_delegated_models_overlap_uses_its_named_reason(self, tmp_path, monkeypatch, platform):
        home = tmp_path / "crew"
        target = home / "models"
        target.mkdir(parents=True)
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox, "_resolved_kiro_agents_targets", lambda: [])
        monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
        monkeypatch.setattr(sandbox.sys, "platform", platform)

        assert "models" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert "models" in sandbox._DELEGATED_OVERLAP_LEAF_REASONS
        reason = sandbox.delegated_workspace_exposes_sealed_target(str(target))
        assert reason is not None
        assert "sealed model weights" in reason
        assert "digest-pinned loader" in reason
        assert sandbox.delegated_workspace_exposes_sealed_target(str(tmp_path / "project")) is None

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_seal_survives_a_file_shaped_ceiling(self, mode: str) -> None:
        """The read-only loop must not require a directory.

        ``security_policy.json`` is a plain file. Requiring a directory skips it
        silently -- no error, and the ceiling stays writable. The loop pins its
        target by descriptor and accepts ANY kind of object there, which is what
        ``_any_kind`` names.
        """
        script = sandbox._build_launcher_script(mode)
        loop = script.split("for d in READONLY_DIRS:", 1)[1].split("\n\n", 1)[0]

        assert "_pin_mount_path(target, _any_kind)" in loop
        assert "stat.S_ISDIR" not in loop
        assert "_MS_REMOUNT | _MS_BIND | _MS_RDONLY" in loop


@_POSIX_ONLY
class TestModelLoaderPaths:
    @pytest.fixture
    def crew(self, tmp_path, monkeypatch):
        from kiro_crew import embeddings
        from kiro_crew.stt import models

        root = tmp_path / "crew home"
        root.mkdir()
        for module in (sandbox, embeddings, models):
            monkeypatch.setattr(module, "config_dir", lambda: root)
        monkeypatch.setattr(sandbox, "_masked_crew_home_roots", lambda: [str(root)])
        return root

    @pytest.fixture
    def loader_files(self, crew):
        from kiro_crew import embeddings
        from kiro_crew.stt import decoder, models

        paths = [embeddings.default_model_path()]
        paths.extend(models.model_path(model) for model in models.CATALOG)
        installed = decoder.installed_path()
        if installed is not None:
            paths.append(installed)
        return paths

    @pytest.mark.parametrize("loader_index", range(6))
    def test_loader_file_symlink_refuses_without_reading_or_removing_it(
        self, crew, loader_files, tmp_path, monkeypatch, loader_index
    ):
        from pathlib import Path

        if loader_index >= len(loader_files):
            pytest.skip("no pinned decoder for this platform")
        target = tmp_path / "outside"
        target.write_bytes(b"original")
        real_lstat = os.lstat
        link = loader_files[loader_index]
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

        def lstat(path, *args, **kwargs):
            assert os.fspath(path) != str(target), "inspected the link's target"
            return real_lstat(path, *args, **kwargs)

        def no_open(*args, **kwargs):
            pytest.fail("read model bytes during metadata-only check")

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            patcher.setattr(Path, "open", no_open)
            patcher.setattr("builtins.open", no_open)
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                sandbox._refuse_aliased_model_descendants()
        assert str(link) in str(exc.value)
        assert str(target) in str(exc.value)
        assert "SYMLINK" in str(exc.value)
        assert link.is_symlink()
        assert target.read_bytes() == b"original"

    @pytest.mark.parametrize("component", ("models", "whisper", "ffmpeg"))
    def test_intermediate_symlink_refuses(self, crew, tmp_path, monkeypatch, component):
        from kiro_crew.stt import decoder

        # Pin a supported platform so the decoder directory is always a loader ancestor.
        artifact = decoder.ARTIFACTS[0]
        monkeypatch.setattr(decoder, "artifact_for", lambda: artifact)
        target = tmp_path / "outside"
        target.mkdir()
        link = crew / "models"
        if component != "models":
            link.mkdir()
            link /= component
        link.symlink_to(target, target_is_directory=True)
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            assert not os.fspath(path).startswith(str(link) + os.sep), "followed a link"
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                sandbox._refuse_aliased_model_descendants()
        assert str(link) in str(exc.value)
        assert str(target) in str(exc.value)
        assert link.is_symlink()

    @pytest.mark.parametrize("component", ("models", "whisper", "weights"))
    def test_name_surrogate_refuses_without_reading_or_removing_it(
        self, crew, loader_files, monkeypatch, component
    ):
        from pathlib import Path

        weights = loader_files[1]
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"original")
        linked = {"models": crew / "models", "whisper": weights.parent, "weights": weights}[
            component
        ]
        before = linked.lstat()
        real_lstat = os.lstat
        inspected = []

        def lstat(path, *args, **kwargs):
            inspected.append(os.fspath(path))
            return real_lstat(path, *args, **kwargs)

        def no_open(*args, **kwargs):
            pytest.fail("read model bytes during metadata-only check")

        with monkeypatch.context() as patcher:
            # A junction's lstat mode is a directory, not S_IFLNK. Select just
            # this entry by identity so the same test runs on a POSIX host.
            patcher.setattr(
                sandbox.platform_compat,
                "lstat_is_name_surrogate",
                lambda info: (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino),
            )
            patcher.setattr(sandbox.os, "lstat", lstat)
            patcher.setattr(sandbox.os, "open", no_open)
            patcher.setattr(Path, "open", no_open)
            patcher.setattr("builtins.open", no_open)
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                sandbox._refuse_aliased_model_descendants()
        message = str(exc.value)
        assert str(linked) in message
        assert "JUNCTION" in message
        assert "KIROCREW_HOME" in message
        assert "bind mount" not in message
        assert str(linked) in inspected
        assert not any(path.startswith(str(linked) + os.sep) for path in inspected)
        after = linked.lstat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert weights.read_bytes() == b"original"

    def test_non_surrogate_loader_paths_pass(self, crew, loader_files, monkeypatch):
        from unittest.mock import patch

        for path in loader_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original")
        with patch.object(
            sandbox.platform_compat, "lstat_is_name_surrogate", return_value=False
        ) as surrogate:
            sandbox._refuse_aliased_model_descendants()
        assert surrogate.call_count >= len(loader_files)
        assert all(path.read_bytes() == b"original" for path in loader_files)

    def test_non_loader_symlink_passes(self, crew):
        link = crew / "models" / "whisper" / "scratch"
        link.parent.mkdir(parents=True)
        link.symlink_to("missing")

        sandbox._refuse_aliased_model_descendants()

        assert link.is_symlink()

    @pytest.mark.parametrize("alias_kind", ("hardlink", "symlink"))
    def test_an_aliased_gguf_fails_the_embedder_load_not_the_spawn(
        self, crew, loader_files, tmp_path, monkeypatch, alias_kind
    ):
        # The GGUF has no digest at load, so a second name in a writable tree is the
        # whole exposure. The embedder refuses to hand llama.cpp the path; the spawn path
        # still admits the hard link (the symlink is refused there, at the component).
        from kiro_crew import embeddings

        gguf = loader_files[0]
        gguf.parent.mkdir(parents=True)
        payload = b"\0" * (embeddings._GGUF_MIN_BYTES + 1)
        outside = tmp_path / "workspace-alias.gguf"
        if alias_kind == "hardlink":
            gguf.write_bytes(payload)
            os.link(gguf, outside)
        else:
            outside.write_bytes(payload)
            gguf.symlink_to(outside)
        opened: list = []

        class _Tripwire:
            def __init__(self, **kwargs):
                opened.append(kwargs.get("model_path"))

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: _Tripwire)
        embedder = embeddings.LlamaCppEmbedder(model_path=gguf, dim=1024, model_id="x:1")
        embedder.wait_ready(timeout=5)

        assert embedder.is_ready() is False
        assert opened == [], "llama.cpp must never be handed an aliased GGUF"
        assert outside.read_bytes() == payload
        if alias_kind == "hardlink":
            sandbox._refuse_aliased_model_descendants()
        else:
            with pytest.raises(sandbox.SandboxCeilingUnsealable):
                sandbox._refuse_aliased_model_descendants()

    def test_a_lone_gguf_still_reaches_the_loader(self, crew, loader_files, monkeypatch):
        from kiro_crew import embeddings

        gguf = loader_files[0]
        gguf.parent.mkdir(parents=True)
        gguf.write_bytes(b"\0" * (embeddings._GGUF_MIN_BYTES + 1))
        opened: list = []

        class _Tripwire:
            def __init__(self, **kwargs):
                opened.append(kwargs.get("model_path"))
                raise RuntimeError("stop after the handoff")

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: _Tripwire)
        monkeypatch.setattr(
            embeddings,
            "_model_context_policy",
            lambda path: SimpleNamespace(n_ctx=embeddings._N_CTX, n_batch=512, n_ubatch=512),
        )
        embedder = embeddings.LlamaCppEmbedder(model_path=gguf, dim=1024, model_id="x:1")
        embedder.wait_ready(timeout=5)

        assert opened == [str(gguf)]

    @pytest.mark.parametrize("loader_index", range(6))
    def test_hardlinked_loader_file_does_not_refuse_the_spawn(
        self, crew, loader_files, tmp_path, loader_index
    ):
        # A second name on a loader file is judged at the LOADER, on the descriptor the
        # digest was computed from, so the spawn path must let it through: refusing here
        # costs every sandboxed spawn on a host whose weights legitimately carry a second
        # name (stow, chezmoi, ``rsync --link-dest``, ``jdupes -L``).
        if loader_index >= len(loader_files):
            pytest.skip("no pinned decoder for this platform")
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[loader_index]
        linked.parent.mkdir(parents=True, exist_ok=True)
        os.link(outside, linked)
        before = linked.stat()

        sandbox._refuse_aliased_model_descendants()

        assert linked.samefile(outside)
        assert linked.stat().st_nlink == before.st_nlink
        assert linked.read_bytes() == outside.read_bytes() == b"original"

    def test_non_loader_hardlink_passes(self, crew, tmp_path):
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = crew / "models" / "whisper" / "scratch"
        linked.parent.mkdir(parents=True)
        os.link(outside, linked)

        sandbox._refuse_aliased_model_descendants()

        assert linked.samefile(outside)
        assert linked.read_bytes() == b"original"

    def test_non_live_loader_symlink_passes(self, crew, loader_files, tmp_path, monkeypatch):
        unused = tmp_path / "unused home"
        monkeypatch.setattr(sandbox, "_masked_crew_home_roots", lambda: [str(crew), str(unused)])
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = unused / loader_files[0].relative_to(crew)
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside)

        sandbox._refuse_aliased_model_descendants()

        assert linked.is_symlink()
        assert outside.read_bytes() == b"original"

    @pytest.fixture
    def wrap_branch(self, crew, tmp_path, monkeypatch):
        from pathlib import Path
        from unittest.mock import MagicMock

        host_home = tmp_path / "host"
        host_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: host_home)
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "")
        monkeypatch.setattr(sandbox, "_warn_aliased_strict_leaves", lambda: None)
        monkeypatch.setattr(sandbox, "_resolve_agent_executable", lambda path: path)
        monkeypatch.setattr(sandbox, "_materialize_sealable_ceilings", lambda *args: [])
        monkeypatch.setattr(sandbox, "_sandbox_env_unset_args", lambda *args: [])
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        monkeypatch.setattr(sandbox, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(sandbox, "cgroup_scope_bus_env", lambda env: (env, ()))

        def configure(branch):
            platform = "win32" if branch == "windows-delegated" else "darwin"
            if branch in ("namespace", "off-linux", "off-nonkiro", "nested", "none"):
                platform = "linux"
            monkeypatch.setattr(sandbox.sys, "platform", platform)
            monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: branch == "nested")
            monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: True)
            backend = {"namespace": "namespace", "seatbelt": "sandbox-exec"}.get(branch, "none")
            monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: backend)
            off = branch in ("off-linux", "off-nonkiro", "darwin-off-delegated")
            # off-nonkiro is a non-kiro argv so even on macOS it stays unconfined;
            # every other branch names kiro-cli so delegation can engage.
            argv = ["python3", "-m", "worker"] if branch == "off-nonkiro" else ["kiro-cli", "acp"]
            return argv, {
                "mode": "off" if off else "standard",
                "is_kiro_cli": "delegated" in branch,
            }

        return configure

    # Branches that apply a Kiro Crew seal or delegate to a Kiro sandbox, so the
    # model loader paths are checked; and the unconfined branches that are not.
    _CHECKED_BRANCHES = (
        "darwin-delegated",
        "windows-delegated",
        "darwin-off-delegated",
        "namespace",
        "seatbelt",
    )
    _UNCHECKED_BRANCHES = ("off-linux", "off-nonkiro", "none", "nested")

    @pytest.mark.parametrize("branch", _CHECKED_BRANCHES)
    def test_every_sealed_branch_refuses_loader_symlink(
        self, crew, loader_files, tmp_path, wrap_branch, branch
    ):
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[0]
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside)
        argv, options = wrap_branch(branch)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox.wrap_argv(argv, **options)

        assert str(linked) in str(exc.value)
        assert linked.is_symlink()
        assert linked.read_bytes() == outside.read_bytes() == b"original"

    @pytest.mark.parametrize("branch", _CHECKED_BRANCHES)
    def test_every_sealed_branch_admits_a_hardlinked_loader_file(
        self, crew, loader_files, tmp_path, wrap_branch, branch
    ):
        # The load-time guard is what judges a second name, so a sealed spawn must still
        # be buildable on a host whose weights carry one.
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[0]
        linked.parent.mkdir(parents=True)
        os.link(outside, linked)
        argv, options = wrap_branch(branch)

        _, cleanup = sandbox.wrap_argv(argv, **options)

        if cleanup is not None:
            os.unlink(cleanup)
        assert linked.samefile(outside)
        assert linked.read_bytes() == outside.read_bytes() == b"original"

    @pytest.mark.parametrize("branch", _UNCHECKED_BRANCHES)
    @pytest.mark.parametrize("alias_kind", ("symlink", "hardlink"))
    def test_unconfined_branches_do_not_refuse_loader_alias(
        self, crew, loader_files, tmp_path, wrap_branch, branch, alias_kind
    ):
        # No Kiro Crew seal and no delegated sandbox: nothing confines a models
        # write, so a symlinked or hard-linked loader file must not newly refuse
        # the spawn (that would brick sandbox-off / no-backend hosts).
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[0]
        linked.parent.mkdir(parents=True)
        if alias_kind == "symlink":
            linked.symlink_to(outside)
        else:
            os.link(outside, linked)
        argv, options = wrap_branch(branch)

        argv_out, cleanup = sandbox.wrap_argv(argv, **options)

        assert cleanup is None
        assert argv_out[-len(argv) :] == argv
        assert linked.is_symlink() if alias_kind == "symlink" else linked.samefile(outside)

    @pytest.mark.parametrize("branch", _CHECKED_BRANCHES)
    def test_every_sealed_branch_checks_models_once(self, crew, wrap_branch, monkeypatch, branch):
        from unittest.mock import patch

        argv, options = wrap_branch(branch)
        with patch.object(
            sandbox,
            "_refuse_aliased_model_descendants",
            wraps=sandbox._refuse_aliased_model_descendants,
        ) as checked:
            _, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        checked.assert_called_once_with()

    @pytest.mark.parametrize("branch", _UNCHECKED_BRANCHES)
    def test_unconfined_branches_never_check_models(self, crew, wrap_branch, branch):
        from unittest.mock import patch

        argv, options = wrap_branch(branch)
        with patch.object(sandbox, "_refuse_aliased_model_descendants") as checked:
            sandbox.wrap_argv(argv, **options)
        checked.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry", ("wrap_argv_async", "sandboxed_spawn_argv", "sandboxed_spawn_argv_async")
    )
    async def test_spawn_entry_points_check_models_once(self, crew, wrap_branch, entry):
        import asyncio
        from unittest.mock import patch

        _, options = wrap_branch("namespace")
        # sandboxed_spawn_argv_async has no Kiro classification argument.
        options.pop("is_kiro_cli")
        with patch.object(
            sandbox,
            "_refuse_aliased_model_descendants",
            wraps=sandbox._refuse_aliased_model_descendants,
        ) as checked:
            prepare = getattr(sandbox, entry)
            if entry == "sandboxed_spawn_argv":
                result = await asyncio.to_thread(prepare, ["agent"], **options)
            else:
                result = await prepare(["agent"], **options)
        if result[-1] is not None:
            os.unlink(result[-1])
        checked.assert_called_once_with()

    def test_failed_darwin_delegation_checks_models_before_audit_and_on_seatbelt_fallback(
        self, crew, wrap_branch, monkeypatch
    ):
        # The hand-off checks before its audit; when the audit then fails, macOS
        # falls back to its own Seatbelt, whose builder checks the model paths
        # again. Both are metadata-only, so the rare fallback costs one extra pass.
        from unittest.mock import MagicMock, patch

        argv, options = wrap_branch("darwin-delegated")
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = RuntimeError("audit unavailable")
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        with patch.object(
            sandbox,
            "_refuse_aliased_model_descendants",
            wraps=sandbox._refuse_aliased_model_descendants,
        ) as checked:
            _, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        assert checked.call_count == 2

    def test_failed_windows_delegation_checks_models_then_fails_closed(
        self, crew, wrap_branch, monkeypatch
    ):
        # Windows has no Kiro Crew backend, so a failed delegation audit falls
        # through to the no-backend policy and fail-closes. The model check runs
        # once, before the audit, as on every delegated hand-off.
        from unittest.mock import MagicMock, patch

        argv, options = wrap_branch("windows-delegated")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = RuntimeError("audit unavailable")
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        with patch.object(sandbox, "_refuse_aliased_model_descendants") as checked:
            with pytest.raises(sandbox.SandboxUnavailableError):
                sandbox.wrap_argv(argv, **options)
        checked.assert_called_once_with()

    @pytest.mark.parametrize("branch", ("darwin-delegated", "windows-delegated"))
    def test_refused_delegation_writes_no_delegated_audit_record(
        self, crew, loader_files, tmp_path, wrap_branch, monkeypatch, branch
    ):
        # A refused hand-off never happened, so the tamper-evident SEL log must not
        # assert that it did: the model check runs before the "delegated" record.
        from unittest.mock import MagicMock

        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[0]
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside)
        argv, options = wrap_branch(branch)
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)

        with pytest.raises(sandbox.SandboxCeilingUnsealable):
            sandbox.wrap_argv(argv, **options)

        delegated = [
            call
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("outcome") == "delegated"
        ]
        assert delegated == []

    def test_bounded_metadata_only_check(self, crew, loader_files, monkeypatch):
        for path in loader_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original")
        real_lstat = os.lstat
        inspected = []

        def lstat(path, *args, **kwargs):
            inspected.append(os.fspath(path))
            return real_lstat(path, *args, **kwargs)

        def no_enumeration(*args, **kwargs):
            pytest.fail("model check must not enumerate directories")

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            patcher.setattr(sandbox.os, "scandir", no_enumeration)
            patcher.setattr(sandbox.os, "walk", no_enumeration)
            sandbox._refuse_aliased_model_descendants()
        expected = {
            str(crew.joinpath(*path.relative_to(crew).parts[:depth]))
            for path in loader_files
            for depth in range(1, len(path.relative_to(crew).parts) + 1)
        }
        assert set(inspected) == expected
        assert len(inspected) <= sum(len(path.relative_to(crew).parts) for path in loader_files)

    def test_missing_tree_passes_without_creating_it(self, crew):
        sandbox._refuse_aliased_model_descendants()
        assert not (crew / "models").exists()

    @pytest.mark.parametrize("failure", (PermissionError, FileNotFoundError))
    @pytest.mark.parametrize("intermediate", (False, True))
    def test_lstat_failure_skips_file(
        self, crew, loader_files, monkeypatch, caplog, failure, intermediate
    ):
        import logging

        weights = loader_files[1]
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"original")
        failing = weights.parent if intermediate else weights
        real_lstat = os.lstat
        inspected = []

        def lstat(path, *args, **kwargs):
            inspected.append(os.fspath(path))
            if os.fspath(path) == str(failing):
                raise failure("cannot inspect\nmodel\x1b[31m")
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            with caplog.at_level(logging.WARNING, logger=sandbox.__name__):
                sandbox._refuse_aliased_model_descendants()
        assert str(failing) in inspected
        if intermediate:
            assert str(weights) not in inspected
        messages = [
            record.getMessage() for record in caplog.records if record.name == sandbox.__name__
        ]
        if failure is FileNotFoundError:
            assert not messages
        else:
            assert any(str(failing) in message for message in messages)
            assert all("\n" not in message and "\x1b" not in message for message in messages)

    def test_symlink_diagnostic_escapes_terminal_controls(self, crew, loader_files, monkeypatch):
        unusual = crew / "line\nbreak\x1b[31m"
        monkeypatch.setattr(sandbox, "config_dir", lambda: unusual)
        link = unusual / loader_files[0].relative_to(crew)
        link.parent.mkdir(parents=True)
        link.symlink_to("missing\n\x1b[31mtarget")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox._refuse_aliased_model_descendants()

        assert "\n" not in str(exc.value)
        assert "\x1b" not in str(exc.value)
        assert "target" in str(exc.value)
        assert link.is_symlink()

    @pytest.mark.parametrize("backend", ("namespace_argv", "sandbox_exec_argv"))
    @pytest.mark.parametrize("live", (False, True), ids=("non-live", "live"))
    def test_both_backends_check_only_live_home(
        self, crew, loader_files, tmp_path, monkeypatch, backend, live
    ):
        from pathlib import Path

        host_home = tmp_path / "host"
        host_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: host_home)
        legacy = host_home / ".kirocrew"
        monkeypatch.setattr(sandbox, "_masked_crew_home_roots", lambda: [str(crew), str(legacy)])
        monkeypatch.setattr(sandbox, "_resolve_agent_executable", lambda path: path)
        monkeypatch.setattr(sandbox, "_materialize_sealable_ceilings", lambda *args: [])
        root = crew if live else legacy
        alias = root / loader_files[1].relative_to(crew)
        alias.parent.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.write_bytes(b"original")
        alias.symlink_to(outside)

        if live:
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                getattr(sandbox, backend)(["agent"])
            assert str(alias) in str(exc.value)
        else:
            assert getattr(sandbox, backend)(["agent"])
        assert alias.is_symlink()
        assert outside.read_bytes() == b"original"


@_POSIX_ONLY
class TestTheWhisperLoaderRefusesAnAliasedFile:
    """The hard-link half of the models rule, judged where the loader verifies bytes.

    ``ModelStore._verified_on_disk`` hashes a file already on disk and then hands its
    PATH to a native loader that reopens it by name. A second hard link is a writable
    handle onto the inode that just passed the digest, and the ``models`` seal covers
    paths, so no mount or Seatbelt rule names the alias. The refusal therefore lands
    here rather than on the spawn path: one load fails instead of every sandboxed spawn
    on a host whose weights legitimately carry a second name.
    """

    @pytest.fixture
    def pinned(self, tmp_path, monkeypatch):
        """A crew home holding one whisper file whose digest matches its catalogue pin."""
        import dataclasses
        import hashlib

        from kiro_crew.stt import models

        root = tmp_path / "crew home"
        monkeypatch.setattr(models, "config_dir", lambda: root)
        payload = b"pinned whisper weights"
        model = dataclasses.replace(
            models.CATALOG[0],
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        path = models.model_path(model)
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        return models.ModelStore(), model, path, payload

    @pytest.mark.asyncio
    async def test_a_lone_regular_file_verifies(self, pinned):
        store, model, path, payload = pinned

        assert await store._verified_on_disk(model) is True
        assert path.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_ensure_reports_the_refusal_without_raising_or_downloading(
        self, pinned, tmp_path, monkeypatch
    ):
        # ``ensure`` promises None, not an exception into a websocket handler. The
        # refusal surfaces as a failed status the settings page shows, naming the file,
        # and no download replaces the operator's file.
        from kiro_crew.stt import models

        store, model, path, payload = pinned
        os.link(path, tmp_path / "alias")
        monkeypatch.setattr(
            models,
            "_download_blocking",
            lambda *a, **k: pytest.fail("a refused file must not be downloaded over"),
        )

        assert await store.ensure(model) is None

        assert store.status["step"] == "failed"
        assert store.status.get("refused") is True
        assert str(path) in str(store.status["error"])
        assert path.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_symlinked_file_is_reported_as_refused_not_raised(
        self, pinned, tmp_path, monkeypatch
    ):
        # A correct-size symlink passes every following stat, so only the O_NOFOLLOW open
        # sees it. That refusal must come back as the documented refused status rather
        # than an OSError out of a websocket handler, and the link must be left in place.
        from kiro_crew.stt import models

        store, model, path, payload = pinned
        target = tmp_path / "shared-cache-weights.bin"
        target.write_bytes(payload)
        path.unlink()
        path.symlink_to(target)
        monkeypatch.setattr(
            models,
            "_download_blocking",
            lambda *a, **k: pytest.fail("a refused file must not be downloaded over"),
        )

        assert await store.ensure(model) is None

        assert store.status["step"] == "failed"
        assert store.status.get("refused") is True
        assert str(path) in str(store.status["error"])
        assert "SYMLINK" in str(store.status["error"])
        assert path.is_symlink()
        assert target.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_plain_status_carries_no_refused_key(self, pinned):
        store, model, _, _ = pinned
        store._set(step="downloading", model=model.name)

        assert "refused" not in store.status

    @pytest.mark.asyncio
    async def test_a_hardlinked_file_refuses_the_load_and_keeps_the_bytes(self, pinned, tmp_path):
        store, model, path, payload = pinned
        alias = tmp_path / "alias"
        os.link(path, alias)
        before = path.stat()

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            await store._verified_on_disk(model)

        message = str(exc.value)
        assert str(path) in message, "the refusal must name the file the operator has to fix"
        assert "plain copy" in message
        # Refusing is not a failed digest, so the file keeps its bytes, its inode and
        # both of its names: a hard link is ordinary for a dotfile manager or a
        # deduplicating backup, and deleting one name of the operator's own file is
        # not this gate's call.
        assert path.is_file()
        assert path.read_bytes() == alias.read_bytes() == payload
        assert (path.stat().st_ino, path.stat().st_nlink) == (before.st_ino, before.st_nlink)

    @pytest.mark.asyncio
    async def test_without_the_guard_the_aliased_file_is_trusted(
        self, pinned, tmp_path, monkeypatch
    ):
        # Negative control: with the descriptor check stubbed out, the digest alone
        # accepts the aliased file. That is what the assertion above is worth.
        store, model, path, _ = pinned
        os.link(path, tmp_path / "alias")
        monkeypatch.setattr(sandbox, "require_unaliased_model_file", lambda path, *, fd: None)

        assert await store._verified_on_disk(model) is True

    @pytest.mark.asyncio
    async def test_the_digest_is_read_from_the_descriptor_that_is_judged(self, pinned):
        # The point of the fd form: one ``open`` answers both questions, so the inode
        # hashed and the inode judged cannot be two resolutions with a swap between.
        store, model, path, _ = pinned
        seen = {}

        real = sandbox.require_unaliased_model_file

        def record(target, *, fd):
            seen["judged"] = os.fstat(fd)[:4]
            return real(target, fd=fd)

        from kiro_crew.stt import models

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(models, "_sha256_file", _unreachable)
            patcher.setattr(sandbox, "require_unaliased_model_file", record)
            assert await store._verified_on_disk(model) is True

        opened = path.stat()
        assert seen["judged"][:4] == (opened.st_mode, opened.st_ino, opened.st_dev, opened.st_nlink)


def _unreachable(*args, **kwargs):
    raise AssertionError("the load-time check must hash the descriptor it judges, not a name")


def _sandbox_spawn_unavailable() -> str:
    """Why a real sandboxed spawn cannot run here, or ``""`` when one can.

    Asked inside the test rather than at module scope: resolving the governance floor
    needs a composed platform context, which the gateway installs and a bare test
    collection does not, and a module-level probe would turn that into a collection
    error for every test in this file.
    """
    try:
        if not sandbox.credential_mask_applies("standard"):
            return "this host has no OS sandbox backend that carries a caller's masks"
    except Exception as exc:  # noqa: BLE001 - any failure here means "cannot spawn for real"
        return f"cannot resolve the sandbox floor outside a composed platform context: {exc}"
    return ""


_CHILD_LINK_PROBE = """
import errno
import os
import sys

sealed, cross, inside = sys.argv[1], sys.argv[2], sys.argv[3]
for label, alias in (("cross", cross), ("inside", inside)):
    try:
        os.link(sealed, alias)
    except OSError as exc:
        print(label + "=" + errno.errorcode.get(exc.errno, str(exc.errno)))
    else:
        print(label + "=CREATED")
"""


@_POSIX_ONLY
class TestASandboxedChildCannotMintAnAliasOfASealedLoaderFile:
    """What the kernel answers a sandboxed ``link(2)`` on a sealed loader file.

    The load-time refusal is a backstop for an alias that already exists -- one an
    operator's own backup or dotfile manager left. This pins the prior question: whether
    a sandboxed shell can CREATE one. Both answers come from the real backend, so this
    runs only on a host that has one and skips with its reason elsewhere.
    """

    def test_the_backend_denies_both_destinations(self, tmp_path, monkeypatch):
        import subprocess
        import sys as _sys

        reason = _sandbox_spawn_unavailable()
        if reason:
            pytest.skip(reason)
        from kiro_crew import embeddings
        from kiro_crew.stt import models

        root = tmp_path / "crew home"
        sealed = root / "models" / embeddings._GGUF_FILENAME
        sealed.parent.mkdir(parents=True)
        sealed.write_bytes(b"pinned weights")
        for module in (sandbox, embeddings, models):
            monkeypatch.setattr(module, "config_dir", lambda: root)
        monkeypatch.setenv("KIROCREW_HOME", str(root))
        cross = tmp_path / "cross-mount-alias"
        inside = sealed.parent / "inside-seal-alias"

        argv, env, cleanup = sandbox.sandboxed_spawn_argv(
            [_sys.executable, "-c", _CHILD_LINK_PROBE, str(sealed), str(cross), str(inside)],
            mode="standard",
            strip_python_env=True,
        )
        try:
            done = subprocess.run(
                argv, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120
            )
        finally:
            if cleanup is not None:
                os.unlink(cleanup)

        answers = dict(line.split("=", 1) for line in done.stdout.split() if line.count("=") == 1)
        assert answers, f"the child printed no verdict: {done.stdout!r} {done.stderr!r}"
        # A destination on another mount cannot hold a link to this inode at all; one
        # inside the seal is refused by the read-only mount (or by the path rule, which
        # reports a permission errno instead).
        assert answers.get("cross") == "EXDEV", answers
        assert answers.get("inside") in {"EROFS", "EPERM", "EACCES"}, answers
        assert not cross.exists()
        assert not inside.exists()


class TestSecretsAreMaskedInEveryMode:
    """Crew-home leaves with no in-sandbox reader are bind-masked, not merely sealed."""

    MASKED = (
        "token_signing.key",
        "refresh_chains.json",
        "kas",
        "mcp-apps",
        "ledger",
        # Same model as ledger, two parties: the worker agent carries the full file
        # toolset, so an unmasked work-ledger lets it reach any conductor's records.
        "work-ledger",
        "backup",
        "browser-cookies.txt",
        "playwright-storage-state.json",
        "playwright-extension-token",
        "ops_mission_control_secrets.json",
        "whatsapp",
        "apps/aws-control/data",
        "aws-control-staging",
        "workspace/md-notebook/pat",
        "data.sqlite3",
        "data.sqlite3-wal",
        "data.sqlite3-shm",
        "data.sqlite3-journal",
    )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_linux_masks_it(self, mode: str, prefix: str, leaf: str) -> None:
        hidden, _readonly, files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        # Both lists, because the child classifies by kind: a file entry reaching only
        # the directory loop is skipped by its ``isdir`` guard and stays readable.
        assert target in hidden, f"{leaf} readable through the {mode} sandbox"
        assert target in files, f"{leaf} would be skipped if it is a file"

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_macos_denies_reads(self, mode: str, prefix: str, leaf: str) -> None:
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-read* (subpath "{target}"))' in profile
        assert f'(deny file-link (subpath "{target}"))' in profile

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", MASKED)
    def test_macos_denies_writes_too(self, mode: str, prefix: str, leaf: str) -> None:
        """A read deny alone leaves the secret OVERWRITABLE.

        The Linux launcher binds an empty dir/file over the target, which blocks both
        directions in one rule. Seatbelt does not, and forging ``token_signing.key``
        needs no read at all — so the read deny on its own is not the control it looks
        like. Both spellings, because a leaf may be a plain file and no subpath rule
        addresses one.
        """
        profile = sandbox._build_seatbelt_profile(mode)
        target = _crew_path(prefix, leaf)

        assert f'(deny file-write* (subpath "{target}"))' in profile
        assert f'(deny file-write* (literal "{target}"))' in profile


class TestTheReconciliationIsComplete:
    """No crew-home entry on the tool gate is left with no sandbox disposition."""

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_every_crew_sensitive_path_is_masked_sealed_or_a_declared_exception(
        self, mode: str
    ) -> None:
        hidden, readonly, _files = _launcher_sets(mode)
        exceptions = _expected_exceptions()

        unaccounted = [
            path
            for path in _crew_sensitive_paths()
            if path not in hidden and path not in readonly and path not in exceptions
        ]
        assert not unaccounted, (
            "crew-home paths the tool gate fences but the OS sandbox does not, and that "
            f"are not declared exceptions either: {unaccounted}"
        )

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_read_write_exceptions_are_exactly_the_declared_set(self, mode: str) -> None:
        """A path drifting into the exception set is a ceiling the agent can rewrite.

        ``run`` is expected on the read-only list rather than fully read-write: it holds
        the launcher itself, so it must stay readable, and the voice-runtime rules
        already seal it. Every other exception is genuinely unrestricted.
        """
        hidden, readonly, _files = _launcher_sets(mode)
        unrestricted = {
            path for path in _crew_sensitive_paths() if path not in hidden and path not in readonly
        }
        declared = _expected_exceptions()

        assert (
            unrestricted <= declared
        ), f"undeclared read-write crew paths in {mode}: {sorted(unrestricted - declared)}"
        for path in declared - unrestricted:
            assert (
                path in readonly
            ), f"{path} is declared an exception but is neither read-write nor sealed"

    def test_the_exception_set_names_only_paths_the_tool_gate_fences(self) -> None:
        """An exception for a path nothing fences is dead weight that reads as a hole."""
        home = _home()
        fenced = {os.path.join(home, rel) for rel in security.sensitive_home_dirs()}

        for path in _expected_exceptions():
            assert path in fenced, f"{path} is exempted from a gate that never covered it"

    def test_the_three_dispositions_do_not_overlap(self) -> None:
        hidden = set(sandbox._CREW_HIDDEN_LEAVES)
        readonly = set(sandbox._CREW_READONLY_LEAVES)
        visible = set(sandbox._CREW_SANDBOX_VISIBLE_LEAVES)

        assert not hidden & readonly
        assert not hidden & visible
        assert not readonly & visible

    def test_every_non_hidden_leaf_is_classified_for_a_foreign_child(self) -> None:
        """A new VISIBLE/READONLY leaf must be classified before it can ship.

        ``adapter_hidden_credential_dirs`` masks the whole read-gate floor for an
        ENFORCED harness and then subtracts ``crew_host_runtime_leaves()``. So every
        leaf this module lets a sandboxed process read is one of two things to a
        FOREIGN harness's child: safe to hand over, or a credential to keep back.

        The two lists are written out rather than one being the complement of the
        other, and this is the pin that makes that pay. Under a complement an
        unclassified leaf would default to READABLE and reach a self-approving
        third-party binary with nothing reading as wrong. Here it appears in neither
        list, this fails, and the author has to choose a side.

        Both directions are asserted. Completeness catches the leaf nobody
        classified; disjointness catches the leaf classified twice, where the mask's
        contents would otherwise depend on which list won.
        """
        source = set(sandbox._CREW_SANDBOX_VISIBLE_LEAVES) | set(sandbox._CREW_READONLY_LEAVES)
        readable = set(sandbox._CREW_CHILD_READABLE_LEAVES)
        withheld = set(sandbox._CREW_CHILD_WITHHELD_LEAVES)

        assert not readable & withheld, (
            "a crew leaf is both child-readable and withheld: " f"{sorted(readable & withheld)}"
        )
        assert source - (readable | withheld) == set(), (
            "unclassified crew leaf(es) -- a sandboxed process may read them, so each "
            "must be declared either safe for a foreign harness's child or withheld "
            f"as credential-bearing: {sorted(source - (readable | withheld))}"
        )
        assert (readable | withheld) - source == set(), (
            "classified leaf(es) that no disposition list declares, so the "
            f"classification covers nothing: {sorted((readable | withheld) - source)}"
        )

    def test_the_child_readable_set_is_exactly_what_the_accessor_publishes(self) -> None:
        """The pin above governs the accessor, not just the constant beside it.

        ``crew_host_runtime_leaves()`` is what ``tool_gate`` actually subtracts from
        the mask. If it ever stopped returning the pinned list -- recomputing a
        complement, filtering, or reordering into a different set -- the pin would go
        on passing while the mask changed underneath it.
        """
        assert set(sandbox.crew_host_runtime_leaves()) == set(sandbox._CREW_CHILD_READABLE_LEAVES)
        assert not set(sandbox.crew_host_runtime_leaves()) & set(
            sandbox._CREW_CHILD_WITHHELD_LEAVES
        )

    def test_the_gateway_launcher_is_a_top_level_readonly_leaf(self) -> None:
        """A nested leaf can be bypassed by renaming its writable parent.

        The Linux bind mount follows the moved parent, leaving the original path
        free for an agent-planted replacement. A top-level leaf has no extra
        agent-writable ancestor inside the data home.
        """
        assert "playwright-cli" in sandbox._CREW_READONLY_LEAVES
        assert "playwright-cli" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert "tools/playwright-cli" not in sandbox._CREW_READONLY_LEAVES

    @pytest.mark.parametrize("mode", _MODES)
    def test_every_mode_carries_the_derived_set(self, mode: str) -> None:
        """The governance tree is masked at every level, the way ``.vault`` already is.

        A per-mode carve-out here would mean ``standard`` — the default — leaves the
        ceiling exposed, which is the configuration almost every install runs.
        """
        listing = {
            "standard": sandbox._STANDARD_DIRS,
            "cc": sandbox._CC_DIRS,
            "strict": sandbox._STRICT_DIRS,
        }[mode]

        for entry in sandbox._CREW_HIDDEN_DIRS:
            assert entry in listing, f"{entry} missing from the {mode} dir list"


class TestARelocatedDataHomeIsCoveredToo:
    """``KIROCREW_HOME`` outside ``$HOME`` must not escape the mask.

    Every dir-list entry is ``$HOME``-relative and joined with ``Path.home()``, so a
    managed fleet that relocates the data home would otherwise get no rule at all for the
    real governance tree — the ceiling left writable on exactly the installs most likely
    to have one.

    The expected target comes from ``config_dir()`` rather than from ``tmp_path`` spelled
    by hand: the resolver creates the directory and can resolve through a symlink, which
    is the ordinary case on macOS.
    """

    @staticmethod
    def _relocate(monkeypatch, tmp_path) -> str:
        from kiro_crew.config.paths import config_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-crew"))
        return str(config_dir())

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    def test_the_launcher_masks_the_resolved_secret_leaves(self, mode, tmp_path, monkeypatch):
        root = self._relocate(monkeypatch, tmp_path)
        hidden, _readonly, files = _launcher_sets(mode)

        target = os.path.join(root, "token_signing.key")
        assert target in hidden, f"a relocated data home is unmasked in {mode}"
        # Both lists, because the child classifies by kind and this leaf is a file.
        assert target in files

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize(
        "leaf", ("security_policy.json", "subagents", "member-memory-bindings")
    )
    def test_the_launcher_seals_the_resolved_ceiling(self, mode, tmp_path, monkeypatch, leaf):
        root = self._relocate(monkeypatch, tmp_path)
        hidden, readonly, _files = _launcher_sets(mode)

        target = os.path.join(root, leaf)
        assert target in readonly, f"a relocated ceiling is writable in {mode}"
        assert target not in hidden, "it must stay readable — masking a ceiling removes it"

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize(
        "leaf", ("security_policy.json", "subagents", "member-memory-bindings")
    )
    def test_the_seatbelt_profile_covers_the_resolved_paths(
        self, mode, tmp_path, monkeypatch, leaf
    ):
        root = self._relocate(monkeypatch, tmp_path)
        profile = sandbox._build_seatbelt_profile(mode)

        ceiling = os.path.join(root, leaf)
        secret = os.path.join(root, "token_signing.key")
        assert f'(deny file-write* (literal "{ceiling}"))' in profile
        assert f'(deny file-write* (subpath "{ceiling}"))' in profile
        assert f'(deny file-read* (subpath "{ceiling}"))' not in profile
        assert f'(deny file-read* (subpath "{secret}"))' in profile

    def test_the_default_layout_adds_no_duplicate_rule(self, monkeypatch, tmp_path):
        """De-duplication: where the two spellings match textually, no second rule."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "x" / ".kiro" / "crew"))
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path / "x"))

        assert sandbox._relocated_crew_targets(("security_policy.json",)) == []

    def test_a_resolution_failure_never_breaks_a_spawn(self, monkeypatch):
        """A spawn must not fail because the data home could not be resolved."""

        def _boom() -> object:
            raise RuntimeError("no home")

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(_boom))
        assert sandbox._relocated_crew_targets(("security_policy.json",)) == []


class TestThirdPartyCredentialsKeepTheirExistingTiering:
    """The reconciliation must not quietly re-tier the non-crew credential entries."""

    def test_standard_still_exposes_the_developer_workflow_dirs(self) -> None:
        """``standard`` deliberately leaves ``.aws``/``.kube`` readable.

        Masking them here would break an ordinary build running in the default mode; the
        crew governance tree is masked at every level precisely because nothing
        legitimate reads it.
        """
        assert ".aws" not in sandbox._STANDARD_DIRS
        assert ".kube" not in sandbox._STANDARD_DIRS
        assert ".aws" in sandbox._CC_DIRS
        assert ".aws" in sandbox._STRICT_DIRS

    @pytest.mark.parametrize("mode", _MODES)
    def test_the_macos_write_deny_does_not_reach_the_refreshable_dirs(self, mode: str) -> None:
        """``.aws`` is masked but must stay WRITABLE where it is exposed.

        A tool refreshing a cached token rewrites it, so the crew-home write deny is
        scoped to the crew leaves rather than applied to every hidden entry. Without this
        the scoping is free to erode into a blanket rule.
        """
        profile = sandbox._build_seatbelt_profile(mode)

        for leaf in (".aws", ".gnupg", ".docker"):
            target = os.path.join(_home(), leaf)
            assert f'(deny file-write* (subpath "{target}"))' not in profile

    def test_the_sso_cookie_store_is_masked_at_the_credential_tiers(self) -> None:
        """``.midway`` is a live bearer credential, the same class as ``.aws``."""
        assert ".midway" in sandbox._STRICT_DIRS
        assert ".midway" in sandbox._CC_DIRS
        assert ".midway" not in sandbox._STANDARD_DIRS

    def test_the_agent_runtime_auth_stores_stay_visible(self) -> None:
        """kiro-cli / amazon-q identity stores are fenced at the tool gate only.

        The agent runtime is itself spawned inside this sandbox and resolves its own
        access token from that store, so masking it would break the agent's model auth
        rather than protect anything. ``security.py`` states this explicitly.
        """
        for listing in (sandbox._STRICT_DIRS, sandbox._CC_DIRS, sandbox._STANDARD_DIRS):
            assert ".local/share/kiro-cli" not in listing
            assert ".local/share/amazon-q" not in listing


class TestAppBackendOwnedLeaves:
    """An app's OWN backend gets its state leaves back; nothing else changes.

    The md-notebook leaves are masked to fence agent subprocesses, but the Notes
    backend is itself a sandboxed spawn and is those files' only legitimate
    reader/writer. Masking it from itself made every attach/clone fail on the
    registry's final atomic rename. The spawn passes the resolved leaf paths as
    ``extra_visible_dirs``; these tests pin that the exemption lifts exactly those
    targets, on both platform builders, and that a default build keeps the mask.
    """

    LEAVES = (
        "workspace/md-notebook/pat",
        "workspace/md-notebook/vaults.json",
        "workspace/md-notebook/settings.json",
    )

    def test_the_helper_resolves_both_home_spellings(self) -> None:
        targets = sandbox.app_backend_visible_targets("md-notebook")

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                assert _crew_path(prefix, leaf) in targets

    def test_an_app_with_no_owned_leaves_gets_no_exemption(self) -> None:
        assert sandbox.app_backend_visible_targets("meetings") == ()
        assert sandbox.app_backend_visible_targets("no-such-app") == ()

    @_POSIX_ONLY
    def test_linux_unhides_the_owned_leaves_for_this_spawn(self) -> None:
        script = sandbox._build_launcher_script(
            "standard", extra_visible_dirs=sandbox.app_backend_visible_targets("md-notebook")
        )
        match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
        assert match
        hidden = set(json.loads(match.group(1)))

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                assert _crew_path(prefix, leaf) not in hidden, f"{leaf} still masked"

    def test_macos_drops_every_rule_for_the_owned_leaves(self) -> None:
        profile = sandbox._build_seatbelt_profile(
            "standard", extra_visible_dirs=sandbox.app_backend_visible_targets("md-notebook")
        )

        for prefix in _CREW_PREFIXES:
            for leaf in self.LEAVES:
                target = _crew_path(prefix, leaf)
                # Read AND write, because the EPERM the issue reports is the write
                # side: the staged sibling temp is written fine and the rename onto
                # the masked literal is what Seatbelt refuses.
                assert f'"{target}"' not in profile, f"{leaf} still carries a deny rule"

    @_POSIX_ONLY
    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("prefix", _CREW_PREFIXES)
    @pytest.mark.parametrize("leaf", LEAVES)
    def test_a_default_build_keeps_the_mask(self, mode: str, prefix: str, leaf: str) -> None:
        """No exemption without the spawn asking: agent subprocesses stay fenced."""
        hidden, _readonly, files = _launcher_sets(mode)
        target = _crew_path(prefix, leaf)

        assert target in hidden
        assert target in files

    def test_the_backend_spawn_passes_the_owned_leaves(self) -> None:
        """`apps/backend.py` must thread the helper's result into ``wrap_argv``.

        Asserted structurally rather than by a full spawn, which needs a manifest, a
        reserved port, and a live interpreter: the call site must derive its visible
        set from the helper, keyed by the app being spawned — and only behind the
        immutable-provenance gate, so a third-party install that claimed the builtin's
        name cannot spawn with the builtin's credential leaves unmasked.
        """
        import inspect

        from kiro_crew.apps import backend as backend_mod

        src = inspect.getsource(backend_mod._start_app_backend_body)
        assert "app_backend_visible_targets(app_name)" in src
        assert "is_builtin_app(app_root=execution_path, app_name=app_name)" in src


class TestForeignMaskShadowGuard:
    """A carve-out spelling beneath a FOREIGN mask is refused, not carved.

    ``_hidden_path_contains_visible_path`` cancels any hidden mask entry that
    CONTAINS a visible path, so an ``extra_visible_dirs`` spelling planted beneath
    an independently masked directory (a data home relocated under a credential
    tree) would take that whole foreign mask down with it. Both crew-home carve-out
    producers — the policy-cache site in ``apps/backend.py`` and
    ``app_backend_visible_targets`` — must refuse such a spelling and keep the mask;
    their consumers fail closed on the masked path, which is strictly safer.
    """

    def test_a_path_beneath_a_masked_credential_tree_is_shadowed(self) -> None:
        # ``.gnupg`` is masked at EVERY tier, standard included, so a data home
        # planted beneath it shadows at the tier the app-backend spawn asks for.
        planted = os.path.join(_home(), ".gnupg", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted)

    def test_standard_mode_ignores_a_strict_only_mask(self) -> None:
        """``standard`` deliberately leaves ``~/.aws`` visible; there is no mask
        to take down, so an ungoverned standard-mode carve-out must be allowed
        (refusing would break md-notebook and cache-only backends for nothing)."""
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert not sandbox.carveout_shadowed_by_foreign_mask(planted, mode="standard")

    def test_strict_mode_refuses_beneath_a_strict_mask(self) -> None:
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="strict")

    def test_cc_mode_refuses_beneath_a_cc_mask(self) -> None:
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="cc")

    def test_a_sibling_prefix_is_not_an_ancestor(self) -> None:
        """``~/.gnupg-backup`` shares ``~/.gnupg``'s string prefix but is not
        beneath it — a ``startswith`` misimplementation reds here."""
        planted = os.path.join(_home(), ".gnupg-backup", "crew", "policy_cache")
        assert not sandbox.carveout_shadowed_by_foreign_mask(planted)

    def test_the_producer_threads_its_mode_to_the_guard(self, monkeypatch, tmp_path) -> None:
        """``app_backend_visible_targets(mode=...)`` must reach the guard: the
        same relocated spelling under a strict-only mask survives a standard
        spawn and is dropped from a strict one."""
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".aws" / "relocated-crew"))
        reloc = str(tmp_path / ".aws" / "relocated-crew" / "workspace" / "md-notebook" / "pat")

        assert reloc in sandbox.app_backend_visible_targets("md-notebook")
        assert reloc not in sandbox.app_backend_visible_targets("md-notebook", mode="strict")

    def test_a_governance_floor_clamps_the_check_up(self, monkeypatch) -> None:
        """A ``sandbox.min_level`` floor raises the tier the masks are built for,
        so the shadow check must judge against the clamped tier, not the ask."""
        monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "strict")
        planted = os.path.join(_home(), ".aws", "crew", "policy_cache")
        assert sandbox.carveout_shadowed_by_foreign_mask(planted, mode="standard")

    def test_a_masked_entry_itself_is_not_shadowed(self) -> None:
        """Equality is the carve-out's whole job; only a PROPER ancestor is foreign."""
        for target in (
            _crew_path(".kiro/crew", "workspace/md-notebook/pat"),
            # The default policy-cache spelling IS a masked entry (every tier list
            # carries ``.kiro/crew/policy_cache``); the cache-only carve-out must
            # keep working on a default layout.
            _crew_path(".kiro/crew", "policy_cache"),
        ):
            assert not sandbox.carveout_shadowed_by_foreign_mask(target), target

    def test_the_staging_roots_own_mask_entry_is_not_a_foreign_ancestor(
        self, monkeypatch, tmp_path
    ) -> None:
        """An ANCESTOR-LIFT producer asks about the mask entry it lifts.

        Its per-call directory is a proper DESCENDANT of that entry, so asking
        about the directory refuses on every layout, the default one included --
        which is exactly why the aws-control staging site asks about the root.
        Both are asserted, so the reason the site is shaped that way is pinned
        rather than only its verdict.
        """
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-crew"))
        staging_root = str(tmp_path / "relocated-crew" / "aws-control-staging")

        assert staging_root in sandbox._relocated_crew_targets(("aws-control-staging",))
        assert not sandbox.carveout_shadowed_by_foreign_mask(staging_root)
        assert sandbox.carveout_shadowed_by_foreign_mask(
            os.path.join(staging_root, "drive-preview-abc")
        )

    def test_a_staging_root_beneath_a_masked_tree_is_shadowed(self, monkeypatch, tmp_path) -> None:
        """A data home relocated beneath ``~/.gnupg`` keeps that mask.

        The producer's own entry is exempt by the equality rule; the credential
        tree above it is not, and that is the layout the staging site must
        refuse rather than lift.
        """
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))
        staging_root = str(tmp_path / ".gnupg" / "relocated-crew" / "aws-control-staging")

        assert sandbox.carveout_shadowed_by_foreign_mask(staging_root)

    @pytest.mark.skipif(os.name != "posix", reason="the mask is a POSIX mechanism")
    def test_the_staging_site_refuses_to_spawn_under_a_foreign_mask(
        self, monkeypatch, tmp_path
    ) -> None:
        """The aws-control preview transfer fails closed instead of spawning.

        The third carve-out producer: on a data home relocated beneath
        ``~/.gnupg`` the staging carve-out would cancel that credential tree's
        mask for the CLI child, so the transfer must raise before ``_checked``
        runs -- and the per-call directory must still be cleaned up.
        """
        from kiro_crew.apps.builtins.aws_control.backend import storage

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))
        staging_root = tmp_path / ".gnupg" / "relocated-crew" / "aws-control-staging"
        staging_root.mkdir(parents=True)
        monkeypatch.setattr(storage, "_preview_staging_parent", lambda: staging_root)
        # Pinned, not inherited: an ``off`` tier skips the check by design, and a
        # governed host can clamp the tier either way.
        monkeypatch.setattr(storage, "effective_sandbox_mode", lambda _m: "standard")

        def _never_spawn(*args: object, **kwargs: object) -> str:
            raise AssertionError("the CLI must not be spawned under a foreign mask")

        monkeypatch.setattr(storage, "_checked", _never_spawn)

        with pytest.raises(ValueError, match="independently masked"):
            storage.get_object_head_bytes(
                "p",
                "us-east-1",
                "bucket",
                "drive",
                "key",
                account="111122223333",
                max_bytes=64,
            )

        assert not list(staging_root.iterdir()), "the per-call directory must be removed"

    @pytest.mark.skipif(os.name != "posix", reason="the mask is a POSIX mechanism")
    def test_a_host_with_no_mask_still_serves_the_preview(self, monkeypatch, tmp_path) -> None:
        """No mask can exist, nothing to unmask -- so no refusal either.

        An ``off`` tier makes ``wrap_argv`` ignore ``extra_visible_dirs``
        outright, and a non-POSIX host has no backend to apply one, so the grant
        lifts nothing and refusing on the same shadowed layout would cost a
        preview for no security gain. Driven through the tier arm because the
        platform arm would send the rest of the call down its other OS's
        branches. The shadowed layout is asserted through the guard first, so
        this cannot pass by the layout being safe.
        """
        from kiro_crew.apps.builtins.aws_control.backend import storage

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))
        staging_root = tmp_path / ".gnupg" / "relocated-crew" / "aws-control-staging"
        staging_root.mkdir(parents=True)
        assert sandbox.carveout_shadowed_by_foreign_mask(str(staging_root))

        monkeypatch.setattr(storage, "_preview_staging_parent", lambda: staging_root)
        monkeypatch.setattr(storage, "effective_sandbox_mode", lambda _m: "off")

        def _fake_checked(argv, profile, **kwargs):
            with open(argv[-1], "wb") as fh:
                fh.write(b"head")
            return json.dumps({"ContentRange": "bytes 0-3/4"})

        monkeypatch.setattr(storage, "_checked", _fake_checked)

        data, size = storage.get_object_head_bytes(
            "p",
            "us-east-1",
            "bucket",
            "drive",
            "key",
            account="111122223333",
            max_bytes=64,
        )

        assert (data, size) == (b"head", 4)

    @pytest.mark.skipif(os.name != "posix", reason="the mask is a POSIX mechanism")
    def test_a_transient_backend_probe_still_refuses(self, monkeypatch, tmp_path) -> None:
        """An uncached ``"none"`` must not be read as "no mask applies".

        ``detect_backend`` deliberately does NOT cache a transient probe failure,
        so a momentary fork or fd failure answers ``"none"`` once and the spawn's
        own re-probe answers with a backend. A check keyed on that answer would
        skip the refusal for a spawn that then applies the lift, which is the one
        direction this path must never fail in.
        """
        from kiro_crew.apps.builtins.aws_control.backend import storage

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))
        staging_root = tmp_path / ".gnupg" / "relocated-crew" / "aws-control-staging"
        staging_root.mkdir(parents=True)
        monkeypatch.setattr(storage, "_preview_staging_parent", lambda: staging_root)
        monkeypatch.setattr(storage, "effective_sandbox_mode", lambda _m: "standard")
        # The probe every backend question goes through, answering as it does on a
        # transient failure: uncached "none".
        monkeypatch.setattr(sandbox, "detect_backend", lambda **_kw: "none")
        monkeypatch.setattr(sandbox, "_backend", None)

        def _never_spawn(*args: object, **kwargs: object) -> str:
            raise AssertionError("a transient probe must not unmask the tree")

        monkeypatch.setattr(storage, "_checked", _never_spawn)

        with pytest.raises(ValueError, match="independently masked"):
            storage.get_object_head_bytes(
                "p",
                "us-east-1",
                "bucket",
                "drive",
                "key",
                account="111122223333",
                max_bytes=64,
            )

    def test_every_crew_home_carveout_producer_asks_the_guard(self) -> None:
        """The guard's worth is the SET of producers that call it.

        Three crew-home carve-out producers exist, and each asks the guard before
        handing a spelling to a spawn. A fourth that forgets is the defect this
        test catches. Structural, like the two sibling tests in this class, so
        deleting a call reds here instead of silently unmasking a tree.

        NOT a closed set over every ``extra_visible_dirs`` producer: three of the
        six in ``src/`` name a workspace or clone root, and
        ``monitoring/provider_cli.py`` hands over ``AZURE_CONFIG_DIR`` while
        ``.azure`` is itself a ``_STANDARD_DIRS`` entry. Whether that one is a
        crew-home producer is a separate question carrying its own tracked issue,
        so this test neither guards it nor lists it as exempt.
        """
        import inspect

        from kiro_crew.apps import backend as backend_mod
        from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod

        for name, obj in (
            ("app_backend_visible_targets", sandbox.app_backend_visible_targets),
            ("policy-cache spawn", backend_mod._start_app_backend_body),
            ("aws-control preview staging", storage_mod.get_object_head_bytes),
        ):
            assert "carveout_shadowed_by_foreign_mask(" in inspect.getsource(obj), name

    def test_an_unmasked_location_is_not_shadowed(self) -> None:
        assert not sandbox.carveout_shadowed_by_foreign_mask(
            os.path.join(_home(), "projects", "notes")
        )

    def test_an_unresolvable_home_fails_toward_refusal(self, monkeypatch) -> None:
        """No mask universe to check means no carve-out, never a carve-anyway."""

        def _boom() -> object:
            raise RuntimeError("no home")

        monkeypatch.setattr(sandbox.Path, "home", staticmethod(_boom))
        assert sandbox.carveout_shadowed_by_foreign_mask("/anywhere/at/all")

    def test_the_md_notebook_carveout_drops_a_shadowed_spelling(
        self, monkeypatch, tmp_path
    ) -> None:
        """A data home relocated beneath a masked tree keeps that tree's mask.

        ``.gnupg`` is masked at the ``standard`` tier the app-backend spawn asks
        for. ``Path.home`` is pinned to ``tmp_path`` so the resolver (which
        CREATES the relocated directory) never writes beneath the real home.
        """
        monkeypatch.setattr(sandbox.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / ".gnupg" / "relocated-crew"))

        # Positive first, so the absence loop below cannot pass vacuously: the
        # relocated spelling really is minted by the resolver and really does
        # trip the guard.
        shadowed_leaf = str(
            tmp_path / ".gnupg" / "relocated-crew" / "workspace" / "md-notebook" / "pat"
        )
        assert shadowed_leaf in sandbox._relocated_crew_targets(("workspace/md-notebook/pat",))
        assert sandbox.carveout_shadowed_by_foreign_mask(shadowed_leaf)

        targets = sandbox.app_backend_visible_targets("md-notebook")

        shadowed_root = str(tmp_path / ".gnupg") + os.sep
        assert targets, "the default home spellings must survive the refusal"
        for target in targets:
            assert not target.startswith(shadowed_root), f"{target} would unmask ~/.gnupg"
        # The refusal is per-spelling: the default-home entries are still carved.
        assert os.path.join(str(tmp_path), ".kiro/crew/workspace/md-notebook/pat") in targets

    def test_the_cache_site_guards_its_carveout(self) -> None:
        """`apps/backend.py` must thread the cache path through the shadow guard.

        Asserted structurally rather than by a full spawn (which needs a manifest, a
        reserved port, and a live interpreter), mirroring the sibling test above: the
        cache-only carve-out must consult the guard before widening ``_visible``.
        """
        import inspect

        from kiro_crew.apps import backend as backend_mod

        src = inspect.getsource(backend_mod._start_app_backend_body)
        assert "carveout_shadowed_by_foreign_mask(_cache_target)" in src


class TestAPodChildsRemappedHomeIsMasked:
    """``acp.client._apply_pod_home_remap`` gives a pod's
    kiro-cli child a pod-owned ``HOME`` (``KIROCREW_OS_HOME``) and
    ``pod.runtime._seed_pod_os_home`` stages the host's SSO tokens into it -- but
    every entry in the tier lists is ``$HOME``-relative joined against the GATEWAY's
    home, so none of them named the remapped tree.

    Both ACP transports freeze their sandbox BEFORE applying the remap, so the mask
    was computed against the original home and the child's own ``$HOME`` resolved to
    an UNMASKED copy of the credential. Re-anchoring inside the mask builder (rather
    than feeding ``extra_hidden_dirs`` from each transport) makes the mask correct
    regardless of that call order, and covers both transports from one place."""

    # The production helper builds each target with ``os.path.normpath(os.path.join(
    # os_home, leaf))``, so an expected value spelled with a literal "/" matches only
    # on POSIX -- on Windows the same call yields backslashes and the assertion failed
    # on shard 3 even though the mask contained the right paths. Building the expected
    # value through the SAME two calls is platform-correct by construction, and keeps
    # the assertion an exact-membership check rather than a weaker substring test.
    _OS_HOME = "/pods/x/os-home"

    @staticmethod
    def _expected(leaf: str) -> str:
        return os.path.normpath(os.path.join(TestAPodChildsRemappedHomeIsMasked._OS_HOME, leaf))

    def test_the_remapped_home_is_re_anchored_when_the_pod_marker_is_set(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", self._OS_HOME)

        out = sandbox._pod_os_home_targets((".aws", ".ssh"))

        assert self._expected(".ssh") in out
        # ``.aws`` is the pod's OWN grant store, carved out on purpose -- see
        # ``_pod_os_home_targets``. The credential FILES under it stay masked.
        assert self._expected(".aws") not in out
        assert self._expected(".aws/config") in out
        assert self._expected(".aws/credentials") in out

    def test_the_seeded_sso_token_directory_stays_reachable_in_tiers_that_mask_aws(
        self, monkeypatch
    ) -> None:
        """The concrete credential: `_seed_pod_os_home` writes
        `<os-home>/.aws/sso/cache/kiro-auth-token*.json`, and the pod's kiro-cli
        child WRITES its own MCP OAuth grants into that same directory.

        Bind-masking `<os-home>/.aws` empty breaks both
        directions -- the child cannot read the token it was seeded, and its grant
        writes land in the overlay rather than the pod tree, so `mcp_grant`'s stat
        answers "no grant" forever. On Linux `is_kiro_cli` does not skip Crew's
        launcher (`delegate_to_kiro` is darwin/win32 only), so this mask really
        does reach the pod child. A read-only per-file re-expose cannot substitute:
        the child needs WRITE access, and the grant filenames are sha256 keys that
        do not exist at launcher-build time.

        `_STANDARD_DIRS` is deliberately EXCLUDED: standard mode leaves `.aws`
        visible so `credential_process` can reach Bedrock auth, so the carve-out is
        a no-op there and that tier's output is unchanged."""
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("KIROCREW_OS_HOME", self._OS_HOME)

        for listing in (sandbox._STRICT_DIRS, sandbox._CC_DIRS):
            assert ".aws" in listing, "the tier list no longer masks .aws at all"
            out = sandbox._pod_os_home_targets(tuple(listing))
            assert self._expected(".aws") not in out
            # Carving out the store must not reopen the file-credential leg.
            assert self._expected(".aws/config") in out
            assert self._expected(".aws/credentials") in out
            # A sibling credential leaf is still empty-masked.
            assert self._expected(".gnupg") in out
        # A tier that never masked .aws gains nothing, so standard-mode pods keep
        # byte-identical masks -- asserted rather than assumed.
        assert ".aws" not in sandbox._STANDARD_DIRS
        standard_out = sandbox._pod_os_home_targets(tuple(sandbox._STANDARD_DIRS))
        assert self._expected(".aws/config") not in standard_out

    def test_a_non_pod_session_mask_is_unchanged(self, monkeypatch) -> None:
        """Gated on the pod marker exactly as ``config.paths`` gates the resolver, so
        an ordinary session gains no rule."""
        monkeypatch.delenv("KIROCREW_POD", raising=False)
        monkeypatch.setenv("KIROCREW_OS_HOME", "/pods/x/os-home")

        assert sandbox._pod_os_home_targets((".aws",)) == []

    def test_the_marker_must_be_exactly_one(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD", "false")
        monkeypatch.setenv("KIROCREW_OS_HOME", "/pods/x/os-home")

        assert sandbox._pod_os_home_targets((".aws",)) == []

    def test_no_os_home_yields_nothing(self, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.delenv("KIROCREW_OS_HOME", raising=False)

        assert sandbox._pod_os_home_targets((".aws",)) == []

    def test_a_remap_target_equal_to_the_real_home_adds_no_duplicate(
        self, monkeypatch, tmp_path
    ) -> None:
        """Mirrors ``_relocated_crew_targets``: only paths that DIFFER from the
        ``$HOME``-relative spelling are returned."""
        monkeypatch.setenv("KIROCREW_POD", "1")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("KIROCREW_OS_HOME", str(tmp_path))

        assert sandbox._pod_os_home_targets((".aws",)) == []

    def test_both_mask_builders_consume_the_helper(self) -> None:
        """One helper, both builders. The launcher script (Linux) and the seatbelt
        profile (macOS) each join the tier list against ``home`` separately, so a
        fix in only one of them would be silently platform-specific."""
        import inspect

        launcher = inspect.getsource(sandbox._build_launcher_script)
        assert "_pod_os_home_targets(" in launcher
        seatbelt = inspect.getsource(sandbox._build_seatbelt_profile)
        assert "_pod_os_home_targets(" in seatbelt


class TestTheModelsSealRefusesAWritableCarveout:
    """``models`` is READONLY, so no spawn may carve a writable window under it.

    The seal's whole point is that a sandboxed spawn needing a writable directory
    of its own cannot get one beneath this leaf -- a private window
    (``extra_private_dirs``) opens only inside a HIDDEN tree, and
    ``extra_writable_dirs`` may punch through the runtime parent's seal and
    nothing else. The local decision runtime is the caller that learned this: its
    work root moved to ``run/decisions/<id>`` (``local_runtime.WORK_SUBDIR``)
    because ``models/decisions/<id>`` met ``EROFS`` under this seal. These two
    cases pin both halves of that answer, so a future move back is caught here
    rather than by a model that silently stops activating.
    """

    def _relocated_home(self, monkeypatch, tmp_path):
        home = tmp_path / "crew-home"
        home.mkdir()
        monkeypatch.setattr(sandbox, "config_dir", lambda: home)
        return home

    @staticmethod
    def _carved(profile: str, path) -> bool:
        lexical = os.path.normpath(str(path))
        spellings = dict.fromkeys((lexical, os.path.realpath(lexical)))
        return all(f'(allow file-write* (subpath "{s}"))' in profile for s in spellings)

    def test_the_decision_work_dir_under_run_is_approved(self, monkeypatch, tmp_path) -> None:
        home = self._relocated_home(monkeypatch, tmp_path)
        own = home / "run" / "decisions" / "laya"
        own.mkdir(parents=True)

        profile = sandbox._build_seatbelt_profile("strict", extra_writable_dirs=(str(own),))

        assert self._carved(profile, own)

    def test_the_same_dir_under_the_models_seal_is_refused(self, monkeypatch, tmp_path) -> None:
        home = self._relocated_home(monkeypatch, tmp_path)
        under_models = home / "models" / "decisions" / "laya"
        under_models.mkdir(parents=True)

        profile = sandbox._build_seatbelt_profile(
            "strict", extra_writable_dirs=(str(under_models),)
        )

        assert not self._carved(profile, under_models)
        # Nothing else gained a window either: the validator skips the candidate
        # rather than degrading it to a narrower carve-out.
        assert "(allow file-write*" not in profile

    @_POSIX_ONLY
    @pytest.mark.parametrize(
        ("leaf", "approved"),
        [(os.path.join("run", "decisions"), True), (os.path.join("models", "decisions"), False)],
    )
    def test_the_linux_launcher_agrees_with_seatbelt(
        self, monkeypatch, tmp_path, leaf: str, approved: bool
    ) -> None:
        home = self._relocated_home(monkeypatch, tmp_path)
        own = home / leaf / "laya"
        own.mkdir(parents=True)

        script = sandbox._build_launcher_script("strict", extra_writable_dirs=(str(own),))

        match = re.search(r"WRITABLE_DIRS = (\[.*?\])\n", script, re.S)
        assert match, "WRITABLE_DIRS missing from the launcher"
        assert (os.path.normpath(str(own)) in json.loads(match.group(1))) is approved

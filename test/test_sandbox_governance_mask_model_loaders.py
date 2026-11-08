"""The models seal at the loaders: a link or a second name on a loader file fails its load.

Covers each loader's ``sandbox.require_real_model_path`` check on the way to a model
file, the Whisper loader's refusal of a hard-linked alias of the file it verified,
and what the kernel answers a sandboxed ``link(2)`` on a sealed loader file. The
reconciliation between the tool gate and the OS-level mask is in
``test_sandbox_governance_mask.py``.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from sandbox_governance_mask_helpers import _no_host_ssh_probe  # noqa: F401
from sandbox_governance_mask_helpers import _POSIX_ONLY

from kiro_crew import sandbox


@_POSIX_ONLY
class TestModelLoaderPaths:
    """A link on the way to a loader file fails that model's LOAD; only the ``models``
    name itself refuses a sealed spawn.

    A step below ``models`` sits inside the read-only seal, so only the host can have made
    a link there, and it threatens only the bytes the loader reads. Each loader therefore
    asks ``sandbox.require_real_model_path`` before it opens the file, and a sealed spawn is
    built over such a link. The ``models`` name lives in the writable data home, so a link
    there still refuses the spawn on both backends.
    """

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
        from kiro_crew.stt import models

        paths = [embeddings.default_model_path()]
        paths.extend(models.model_path(model) for model in models.CATALOG)
        return paths

    @staticmethod
    def _check(crew, path) -> None:
        sandbox.require_real_model_path(str(path), models_root=str(crew / "models"))

    @pytest.mark.parametrize("loader_index", range(5))
    def test_loader_file_symlink_refuses_without_reading_or_removing_it(
        self, crew, loader_files, tmp_path, monkeypatch, loader_index
    ):
        from pathlib import Path

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
                self._check(crew, link)
        assert str(link) in str(exc.value)
        assert str(target) in str(exc.value)
        assert "SYMLINK" in str(exc.value)
        assert "not loaded" in str(exc.value)
        assert link.is_symlink()
        assert target.read_bytes() == b"original"

    @pytest.mark.parametrize("component", ("models", "whisper"))
    def test_intermediate_symlink_refuses(
        self, crew, loader_files, tmp_path, monkeypatch, component
    ):
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
                self._check(crew, loader_files[1])
        assert str(link) in str(exc.value)
        assert str(target) in str(exc.value)
        assert "KIROCREW_HOME" in str(exc.value)
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
                self._check(crew, weights)
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

    def test_real_loader_paths_pass(self, crew, loader_files):
        from unittest.mock import patch

        for path in loader_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original")
        with patch.object(
            sandbox.platform_compat, "lstat_is_name_surrogate", return_value=False
        ) as surrogate:
            for path in loader_files:
                self._check(crew, path)
        assert surrogate.call_count >= len(loader_files)
        assert all(path.read_bytes() == b"original" for path in loader_files)

    def test_a_hardlinked_loader_file_passes_the_walk(self, crew, loader_files, tmp_path):
        # A second hard link is judged on the loader's descriptor
        # (``require_unaliased_model_file``), not by this name walk.
        outside = tmp_path / "weights"
        outside.write_bytes(b"original")
        linked = loader_files[1]
        linked.parent.mkdir(parents=True)
        os.link(outside, linked)

        self._check(crew, linked)

        assert linked.samefile(outside)

    def test_a_path_outside_the_models_tree_is_not_checked(self, crew, tmp_path):
        custom = tmp_path / "custom" / "model.gguf"
        custom.parent.mkdir()
        custom.symlink_to(tmp_path / "missing")

        self._check(crew, custom)

        assert custom.is_symlink()

    def test_bounded_metadata_only_check(self, crew, loader_files, monkeypatch):
        weights = loader_files[1]
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"original")
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
            self._check(crew, weights)
        assert inspected == [str(crew / "models"), str(weights.parent), str(weights)]

    def test_missing_tree_passes_without_creating_it(self, crew, loader_files):
        self._check(crew, loader_files[1])
        assert not (crew / "models").exists()

    @pytest.mark.parametrize("intermediate", (False, True))
    def test_an_unreadable_name_refuses_the_load(
        self, crew, loader_files, monkeypatch, intermediate
    ):
        weights = loader_files[1]
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"original")
        failing = weights.parent if intermediate else weights
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if os.fspath(path) == str(failing):
                raise PermissionError("cannot inspect\nmodel\x1b[31m")
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                self._check(crew, weights)
        message = str(exc.value)
        assert str(failing) in message
        assert "not loaded" in message
        assert "\n" not in message and "\x1b" not in message

    def test_a_vanishing_name_ends_the_walk(self, crew, loader_files, monkeypatch):
        weights = loader_files[1]
        weights.parent.mkdir(parents=True)
        real_lstat = os.lstat
        inspected = []

        def lstat(path, *args, **kwargs):
            inspected.append(os.fspath(path))
            if os.fspath(path) == str(weights.parent):
                raise FileNotFoundError(path)
            return real_lstat(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(sandbox.os, "lstat", lstat)
            self._check(crew, weights)
        assert str(weights) not in inspected

    def test_symlink_diagnostic_escapes_terminal_controls(self, crew, loader_files, monkeypatch):
        unusual = crew / "line\nbreak\x1b[31m"
        link = unusual / loader_files[0].relative_to(crew)
        link.parent.mkdir(parents=True)
        link.symlink_to("missing\n\x1b[31mtarget")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox.require_real_model_path(str(link), models_root=str(unusual / "models"))

        assert "\n" not in str(exc.value)
        assert "\x1b" not in str(exc.value)
        assert "target" in str(exc.value)
        assert link.is_symlink()

    @pytest.mark.parametrize("alias_kind", ("hardlink", "symlink", "linked-models"))
    def test_an_aliased_gguf_fails_the_embedder_load_not_the_spawn(
        self, crew, loader_files, tmp_path, monkeypatch, alias_kind
    ):
        # The GGUF has no digest at load, so a second name in a writable tree is the
        # whole exposure. The embedder refuses to hand llama.cpp the path; a second hard
        # link, a symlinked file and a linked models directory each fail that load.
        from kiro_crew import embeddings

        gguf = loader_files[0]
        payload = b"\0" * (embeddings._GGUF_MIN_BYTES + 1)
        outside = tmp_path / "workspace-alias.gguf"
        if alias_kind == "linked-models":
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            (elsewhere / gguf.name).write_bytes(payload)
            gguf.parent.symlink_to(elsewhere, target_is_directory=True)
        else:
            gguf.parent.mkdir(parents=True)
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
                raise RuntimeError("stop after the handoff")

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: _Tripwire)
        # Without it a refusal would be indistinguishable from a context-policy failure
        # on the stub payload: the lone-GGUF test below reaches the tripwire this way.
        monkeypatch.setattr(
            embeddings,
            "_model_context_policy",
            lambda path: SimpleNamespace(n_ctx=embeddings._N_CTX, n_batch=512, n_ubatch=512),
        )
        embedder = embeddings.LlamaCppEmbedder(model_path=gguf, dim=1024, model_id="x:1")
        embedder.wait_ready(timeout=5)

        assert embedder.is_ready() is False
        assert opened == [], "llama.cpp must never be handed an aliased GGUF"
        if alias_kind != "linked-models":
            assert outside.read_bytes() == payload
            # Only the models NAME refuses a spawn; an alias below it does not.
            sandbox._refuse_linked_models_leaf()

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

    # The branches that apply a Kiro Crew seal, and those that apply none (a delegated
    # Kiro hand-off or an unconfined spawn), where nothing of Kiro Crew's seals models.
    _SEALED_BRANCHES = ("namespace", "seatbelt")
    _DELEGATED_BRANCHES = ("darwin-delegated", "windows-delegated", "darwin-off-delegated")
    _UNSEALED_BRANCHES = ("off-linux", "off-nonkiro", "none", "nested", *_DELEGATED_BRANCHES)

    @staticmethod
    def _plant(crew, loader_files, tmp_path, alias_kind):
        """Put an alias of *alias_kind* on the whisper weights' way; return the alias."""
        weights = loader_files[1]
        outside = tmp_path / "outside"
        if alias_kind == "linked-step":
            outside.mkdir()
            (outside / weights.name).write_bytes(b"original")
            weights.parent.parent.mkdir(parents=True)
            weights.parent.symlink_to(outside, target_is_directory=True)
            return weights.parent
        weights.parent.mkdir(parents=True)
        outside.write_bytes(b"original")
        if alias_kind == "linked-models":
            weights.parent.rmdir()
            (crew / "models").rmdir()
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            (crew / "models").symlink_to(elsewhere, target_is_directory=True)
            return crew / "models"
        if alias_kind == "symlink":
            weights.symlink_to(outside)
        else:
            os.link(outside, weights)
        return weights

    @pytest.mark.parametrize("branch", _SEALED_BRANCHES)
    @pytest.mark.parametrize("alias_kind", ("linked-step", "symlink", "hardlink"))
    def test_an_alias_below_the_models_name_does_not_refuse_a_sealed_spawn(
        self, crew, loader_files, tmp_path, wrap_branch, branch, alias_kind
    ):
        # The loader that reads the file refuses that one load; the spawn is built.
        alias = self._plant(crew, loader_files, tmp_path, alias_kind)
        argv, options = wrap_branch(branch)

        argv_out, cleanup = sandbox.wrap_argv(argv, **options)

        if cleanup is not None:
            os.unlink(cleanup)
        assert argv_out[-len(argv) :] == argv
        assert alias.is_symlink() if alias_kind != "hardlink" else alias.stat().st_nlink == 2

    def test_a_linked_models_name_refuses_the_seatbelt_spawn(
        self, crew, loader_files, tmp_path, wrap_branch
    ):
        # The namespace builder refuses the same link in _materialize_sealable_ceilings
        # (see test_a_symlinked_models_leaf_refuses_the_spawn in
        # test_sandbox_governance_mask.py); Seatbelt asks here.
        alias = self._plant(crew, loader_files, tmp_path, "linked-models")
        argv, options = wrap_branch("seatbelt")

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            sandbox.wrap_argv(argv, **options)

        assert str(alias) in str(exc.value)
        assert "KIROCREW_HOME" in str(exc.value)
        assert alias.is_symlink()

    @pytest.mark.parametrize("live", (False, True), ids=("non-live", "live"))
    def test_seatbelt_checks_only_the_live_models_name(self, crew, tmp_path, monkeypatch, live):
        from pathlib import Path

        host_home = tmp_path / "host"
        host_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: host_home)
        legacy = host_home / ".kirocrew"
        legacy.mkdir()
        monkeypatch.setattr(sandbox, "_masked_crew_home_roots", lambda: [str(crew), str(legacy)])
        monkeypatch.setattr(sandbox, "_resolve_agent_executable", lambda path: path)
        root = crew if live else legacy
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        alias = root / "models"
        alias.symlink_to(elsewhere, target_is_directory=True)

        if live:
            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                sandbox.sandbox_exec_argv(["agent"])
            assert str(alias) in str(exc.value)
        else:
            _, cleanup = sandbox.sandbox_exec_argv(["agent"])
            if cleanup is not None:
                os.unlink(cleanup)
        assert alias.is_symlink()

    @pytest.mark.parametrize("branch", _UNSEALED_BRANCHES)
    @pytest.mark.parametrize("alias_kind", ("linked-models", "linked-step", "symlink", "hardlink"))
    def test_unsealed_branches_do_not_refuse_a_model_alias(
        self, crew, loader_files, tmp_path, wrap_branch, branch, alias_kind
    ):
        # No Kiro Crew seal: nothing of Kiro Crew's confines a models write, so an
        # alias must not newly refuse the spawn (that would brick sandbox-off,
        # no-backend and delegated hosts).
        alias = self._plant(crew, loader_files, tmp_path, alias_kind)
        argv, options = wrap_branch(branch)

        argv_out, cleanup = sandbox.wrap_argv(argv, **options)

        assert cleanup is None
        assert argv_out[-len(argv) :] == argv
        assert alias.is_symlink() if alias_kind != "hardlink" else alias.stat().st_nlink == 2

    @pytest.mark.parametrize("branch", _SEALED_BRANCHES)
    def test_no_sealed_branch_walks_the_model_steps(self, crew, wrap_branch, branch):
        from unittest.mock import patch

        argv, options = wrap_branch(branch)
        with patch.object(sandbox, "require_real_model_path") as walked:
            wrapped, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        walked.assert_not_called()
        assert wrapped[-len(argv) :] == argv

    def test_the_seatbelt_branch_checks_the_models_name_once(self, crew, wrap_branch):
        from unittest.mock import patch

        argv, options = wrap_branch("seatbelt")
        with patch.object(
            sandbox, "_refuse_linked_models_leaf", wraps=sandbox._refuse_linked_models_leaf
        ) as checked:
            wrapped, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        checked.assert_called_once_with()
        assert wrapped[-len(argv) :] == argv

    @pytest.mark.parametrize("branch", ("namespace", *_UNSEALED_BRANCHES))
    def test_other_branches_never_ask_the_seatbelt_question(self, crew, wrap_branch, branch):
        from unittest.mock import patch

        argv, options = wrap_branch(branch)
        with patch.object(sandbox, "_refuse_linked_models_leaf") as checked:
            wrapped, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        checked.assert_not_called()
        assert wrapped[-len(argv) :] == argv

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry", ("wrap_argv_async", "sandboxed_spawn_argv", "sandboxed_spawn_argv_async")
    )
    async def test_spawn_entry_points_build_over_a_linked_step(
        self, crew, loader_files, tmp_path, wrap_branch, entry
    ):
        import asyncio

        alias = self._plant(crew, loader_files, tmp_path, "linked-step")
        _, options = wrap_branch("namespace")
        # sandboxed_spawn_argv_async has no Kiro classification argument.
        options.pop("is_kiro_cli")
        prepare = getattr(sandbox, entry)
        if entry == "sandboxed_spawn_argv":
            result = await asyncio.to_thread(prepare, ["agent"], **options)
        else:
            result = await prepare(["agent"], **options)
        if result[-1] is not None:
            os.unlink(result[-1])
        assert alias.is_symlink()

    def test_failed_darwin_delegation_checks_the_models_name_once_on_the_seatbelt_fallback(
        self, crew, wrap_branch, monkeypatch
    ):
        # The hand-off itself does not check; when its audit fails, macOS falls
        # back to its own Seatbelt, whose builder checks the models name once.
        from unittest.mock import MagicMock, patch

        argv, options = wrap_branch("darwin-delegated")
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = RuntimeError("audit unavailable")
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        with patch.object(
            sandbox, "_refuse_linked_models_leaf", wraps=sandbox._refuse_linked_models_leaf
        ) as checked:
            wrapped, cleanup = sandbox.wrap_argv(argv, **options)
        if cleanup is not None:
            os.unlink(cleanup)
        checked.assert_called_once_with()
        assert wrapped[-len(argv) :] == argv
        assert cleanup is not None, "the Seatbelt fallback writes a profile to clean up"

    def test_failed_windows_delegation_fails_closed_without_a_model_check(
        self, crew, wrap_branch, monkeypatch
    ):
        # Windows has no Kiro Crew backend, so a failed delegation audit falls
        # through to the no-backend policy and fail-closes without a model check.
        from unittest.mock import MagicMock, patch

        argv, options = wrap_branch("windows-delegated")
        monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
        audit = MagicMock()
        audit.log_tool_invocation.side_effect = RuntimeError("audit unavailable")
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
        with patch.object(sandbox, "_refuse_linked_models_leaf") as checked:
            with pytest.raises(sandbox.SandboxUnavailableError):
                sandbox.wrap_argv(argv, **options)
        checked.assert_not_called()

    @pytest.mark.parametrize("branch", ("darwin-delegated", "windows-delegated"))
    def test_delegated_hand_off_proceeds_over_a_symlinked_loader_file(
        self, crew, loader_files, tmp_path, wrap_branch, monkeypatch, branch
    ):
        # A delegated hand-off applies no Kiro Crew seal over models, so a symlinked
        # loader file must not refuse it: the hand-off is audited and the link kept.
        from unittest.mock import MagicMock

        linked = self._plant(crew, loader_files, tmp_path, "symlink")
        argv, options = wrap_branch(branch)
        audit = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)

        argv_out, cleanup = sandbox.wrap_argv(argv, **options)

        assert cleanup is None
        assert argv_out[-len(argv) :] == argv
        assert linked.is_symlink()
        delegated = [
            call
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("outcome") == "delegated"
        ]
        assert len(delegated) == 1


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
    @pytest.mark.parametrize("step", ("models", "whisper"))
    async def test_a_linked_step_refuses_the_load_without_downloading(
        self, pinned, tmp_path, monkeypatch, step
    ):
        # Every following stat passes and the O_NOFOLLOW open sees a real leaf, so only
        # the walk from models down sees the link. It refuses this model's load, with the
        # documented refused status naming the link, and downloads nothing through it.
        from kiro_crew.stt import models

        store, model, path, payload = pinned
        linked = path.parent if step == "whisper" else path.parent.parent
        elsewhere = tmp_path / "elsewhere"
        linked.rename(elsewhere)
        linked.symlink_to(elsewhere, target_is_directory=True)
        monkeypatch.setattr(
            models,
            "_download_blocking",
            lambda *a, **k: pytest.fail("a refused path must not be downloaded through"),
        )

        assert await store.ensure(model) is None

        assert store.status["step"] == "failed"
        assert store.status.get("refused") is True
        assert str(linked) in str(store.status["error"])
        assert "JUNCTION" in str(store.status["error"])
        assert linked.is_symlink()
        assert path.read_bytes() == payload

    @pytest.mark.asyncio
    async def test_a_linked_step_refuses_before_the_model_is_downloaded(
        self, pinned, tmp_path, monkeypatch
    ):
        # The same link with no file behind it yet: the download would write the weights
        # into a tree the seal does not cover, so the load refuses before it starts.
        from kiro_crew.stt import models

        store, model, path, _ = pinned
        path.unlink()
        elsewhere = tmp_path / "elsewhere"
        path.parent.rename(elsewhere)
        path.parent.symlink_to(elsewhere, target_is_directory=True)
        monkeypatch.setattr(
            models,
            "_download_blocking",
            lambda *a, **k: pytest.fail("a refused path must not be downloaded through"),
        )

        assert await store.ensure(model) is None

        assert store.status.get("refused") is True
        assert not any(elsewhere.iterdir())

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

    @pytest.fixture
    def racing_read(self, monkeypatch):
        """Patch the hash loop's ``os.read`` to run *race* once, after the first chunk.

        A small chunk size makes the pinned payload take several reads, so the race lands
        in a chunk that is ALREADY hashed: the digest of what was read stays the pinned
        one while the file itself changes.
        """
        from kiro_crew.stt import models

        monkeypatch.setattr(models, "_CHUNK_BYTES", 4)
        real_read = os.read
        calls = {"reads": 0}

        def arm(race):
            def read(fd, size):
                calls["reads"] += 1
                if calls["reads"] == 2:
                    race()
                return real_read(fd, size)

            monkeypatch.setattr(models.os, "read", read)
            return calls

        return arm

    @pytest.mark.asyncio
    async def test_an_alias_written_and_unlinked_mid_hash_refuses_before_any_read(
        self, pinned, tmp_path, racing_read
    ):
        # The race a judgement only at the END of the read loses: write through the alias
        # into a hashed chunk, unlink the alias, and the final fstat sees one link while
        # the digest still matches the pin. Judged before the first read, the alias that
        # exists when verification starts refuses, and no byte is read through it.
        store, model, path, payload = pinned
        alias = tmp_path / "alias"
        os.link(path, alias)

        def race():
            with open(alias, "r+b") as handle:
                handle.write(b"FORG")
            alias.unlink()

        calls = racing_read(race)

        with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
            await store._verified_on_disk(model)

        assert str(path) in str(exc.value)
        assert calls["reads"] == 0
        assert alias.exists(), "the race never ran, so the operator's alias is untouched"
        assert path.read_bytes() == payload

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX change times; Windows has no Kiro Crew seal"
    )
    @pytest.mark.asyncio
    async def test_a_write_through_a_held_descriptor_mid_hash_refuses_the_load(
        self, pinned, racing_read
    ):
        # No alias exists when verification starts, but a descriptor opened through one
        # earlier is still held. A write through it into a hashed chunk leaves the digest
        # of what was read equal to the pin; the inode's stamp moved, so the load refuses
        # and the file is left for the operator rather than deleted or downloaded over.
        store, model, path, payload = pinned
        old = 1_000_000_000 * 1_000_000_000
        os.utime(path, ns=(old, old))
        held = os.open(path, os.O_RDWR)
        try:
            calls = racing_read(lambda: os.pwrite(held, b"FORG", 0))

            with pytest.raises(sandbox.SandboxCeilingUnsealable) as exc:
                await store._verified_on_disk(model)
        finally:
            os.close(held)

        assert calls["reads"] >= 2
        assert str(path) in str(exc.value)
        assert "changed while it was being verified" in str(exc.value)
        assert path.is_file()
        assert path.read_bytes() == b"FORG" + payload[4:]


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
                argv,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
                cwd=tmp_path,
            )
        finally:
            if cleanup is not None:
                os.unlink(cleanup)

        answers = dict(line.split("=", 1) for line in done.stdout.split() if line.count("=") == 1)
        assert answers, f"the child printed no verdict: {done.stdout!r} {done.stderr!r}"
        # A destination on another mount cannot hold a link to this inode at all; one
        # inside the seal is refused by the read-only mount. Seatbelt's path rule
        # denies ``file-link`` before either, and reports a permission errno instead.
        cross_denials = {"EPERM", "EACCES"} if _sys.platform == "darwin" else {"EXDEV"}
        assert answers.get("cross") in cross_denials, answers
        assert answers.get("inside") in {"EROFS", "EPERM", "EACCES"}, answers
        assert not cross.exists()
        assert not inside.exists()

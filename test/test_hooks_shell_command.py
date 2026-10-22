"""The shell command an ACP event hands the gate, the hooks config, and ``safe_read_file``.

Pins ``AcpEvent.shell_command`` for both event shapes and the ``aws`` command it builds
from a structured ``use_aws`` call (operation casing folded, whitespace refused, judged end
to end by the gate), ``HooksConfig.from_dict`` with its two subagent flags and
``HookManager.reload``, and ``safe_read_file``: its sensitive-path and symlink refusals,
the size ceiling, line endings, and the opener and descriptor checks around the read.
Tool matching, the message and tool hooks, ``hook_gate_kwargs`` and the shell-title tiers
live in ``test_hooks.py``; the spawn rung, the read-only proofs and identity governance in
``test_hooks_trusted_identity.py``; the app-own MCP server auto-approve and the path tier's
target paths and exemptions in ``test_hooks_path_tier.py``.
"""

from __future__ import annotations

import pytest

from conftest import requires_symlinks
from kiro_crew.hooks import (
    HOOK_PASSTHROUGH,
    HOOK_REPLY,
    TOOL_DENY,
    AutoReplyHook,
    HookManager,
    HooksConfig,
    safe_read_file,
)


class TestShellCommandProperty:
    """The AcpEvent.shell_command property is the single source that feeds the
    raw command into the security gate. It must recover the command from BOTH
    event shapes, or the gate silently degrades to title-only (a no-op fix).
    """

    def test_from_raw_tool_params(self):
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(kind="tool_call", is_shell=True, raw_tool_params={"command": "ls -la"})
        assert ev.shell_command == "ls -la"

    def test_from_tool_input_json_permission_event(self):
        """permission_request events set tool_input (JSON), NOT raw_tool_params
        — this is the dashboard's primary gate path, so the fallback is
        load-bearing: reading only raw_tool_params is a no-op on permission
        events."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="permission_request",
            is_shell=True,
            tool_input='{"command": "cr --all --yes"}',
        )
        assert ev.shell_command == "cr --all --yes"

    def test_non_shell_returns_none(self):
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(kind="tool_call", is_shell=False, raw_tool_params={"command": "ls"})
        assert ev.shell_command is None

    def test_missing_or_empty_command_returns_none(self):
        from kiro_crew.acp.types import AcpEvent

        assert AcpEvent(kind="tool_call", is_shell=True, raw_tool_params={}).shell_command is None
        assert (
            AcpEvent(kind="tool_call", is_shell=True, raw_tool_params={"command": ""}).shell_command
            is None
        )

    def test_malformed_tool_input_returns_none(self):
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(kind="permission_request", is_shell=True, tool_input="not json{")
        assert ev.shell_command is None


class TestShellCommandUseAws:
    """kiro-cli reports ``use_aws`` with the shell tool kind but its params are
    the structured {service_name, operation_name, ...} shape with NO "command"
    key. Before the fix, extraction returned None and the deny-by-default
    backstop rejected EVERY use_aws call ("shell command could not be verified
    for security policy") — the v3.3.x regression that fully broke SSM.
    """

    def test_use_aws_from_raw_tool_params(self):
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "ssm",
                "operation_name": "send-command",
                "region": "us-east-1",
                "parameters": {
                    "document-name": "AWS-RunShellScript",
                    "parameters": {"commands": ["systemctl is-active clickhouse-server"]},
                },
            },
        )
        cmd = ev.shell_command
        assert cmd is not None
        assert cmd.startswith("aws ssm send-command")
        assert "--region us-east-1" in cmd
        # The serialized parameters tail must carry the embedded shell payload
        # so the exfiltration / sensitive-path checks can scan it.
        assert "systemctl is-active clickhouse-server" in cmd

    def test_use_aws_from_tool_input_json_permission_event(self):
        """permission_request events carry the params via tool_input JSON —
        the dashboard's primary gate path, where the v3.3.x SSM block fired."""
        import json as _json

        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="permission_request",
            is_shell=True,
            tool_input=_json.dumps({"service_name": "s3api", "operation_name": "list-buckets"}),
        )
        assert ev.shell_command == "aws s3api list-buckets"

    def test_use_aws_not_denied_by_default_gate(self):
        """End-to-end through the gate: a benign use_aws call must NOT hit the
        deny-by-default backstop once the command is recoverable."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="permission_request",
            is_shell=True,
            tool_input='{"service_name": "s3api", "operation_name": "list-buckets"}',
        )
        result = HookManager().on_tool_call(
            "AWS: s3api list-buckets", command=ev.shell_command, is_shell=True
        )
        assert result.action != TOOL_DENY

    def test_use_aws_destructive_operation_still_denied(self):
        """The synthesized command must keep the built-in deny globs armed:
        destructive AWS operations stay blocked."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "cloudformation",
                "operation_name": "delete-stack",
                "parameters": {"stack-name": "prod-stack"},
            },
        )
        result = HookManager().on_tool_call(
            "AWS: cloudformation delete-stack", command=ev.shell_command, is_shell=True
        )
        assert result.action == TOOL_DENY

    def test_use_aws_credential_read_payload_still_denied(self):
        """A shell payload smuggled inside ssm send-command parameters must be
        visible to the sensitive-path checks via the serialized tail."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "ssm",
                "operation_name": "send-command",
                "parameters": {"commands": ["cat ~/.aws/credentials"]},
            },
        )
        result = HookManager().on_tool_call(
            "AWS: ssm send-command", command=ev.shell_command, is_shell=True
        )
        assert result.action == TOOL_DENY

    def test_missing_operation_name_still_returns_none(self):
        """Half a structured shape is not verifiable — deny-by-default must
        stay armed for it."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(kind="tool_call", is_shell=True, raw_tool_params={"service_name": "ssm"})
        assert ev.shell_command is None

    # ── Casing normalization hardening ──

    def test_pascal_case_operation_normalized_to_kebab(self):
        """PascalCase operation_name (the AWS API name, e.g. 'DeleteStack')
        must be normalized to kebab-case so deny globs still match."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "cloudformation",
                "operation_name": "DeleteStack",
                "parameters": {"stack-name": "prod-stack"},
            },
        )
        cmd = ev.shell_command
        assert cmd is not None
        assert "delete-stack" in cmd, f"PascalCase not normalized: {cmd}"
        assert "DeleteStack" not in cmd

    def test_pascal_case_destructive_op_denied(self):
        """End-to-end: PascalCase 'DeleteStack' must hit the deny gate
        identically to kebab 'delete-stack'."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "cloudformation",
                "operation_name": "DeleteStack",
                "parameters": {"stack-name": "prod-stack"},
            },
        )
        result = HookManager().on_tool_call(
            "AWS: cloudformation DeleteStack", command=ev.shell_command, is_shell=True
        )
        assert result.action == TOOL_DENY, f"PascalCase 'DeleteStack' bypassed deny gate: {result}"

    def test_camel_case_operation_normalized(self):
        """camelCase operation_name must also normalize (e.g. 'deleteStack')."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "cloudformation",
                "operation_name": "deleteStack",
            },
        )
        cmd = ev.shell_command
        assert cmd is not None
        assert "delete-stack" in cmd

    def test_kebab_case_operation_unchanged(self):
        """Already-kebab operation_name must pass through unchanged."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "s3api",
                "operation_name": "list-buckets",
            },
        )
        cmd = ev.shell_command
        assert cmd == "aws s3api list-buckets"

    def test_service_name_passed_through_verbatim(self):
        """service_name is NOT normalized (AWS services are already lowercase
        single tokens like 'cloudformation'). PascalCase service_name is passed
        through as-is because normalizing it would break deny regex matches
        (e.g. 'CloudFormation' → 'cloud-formation' ≠ 'cloudformation')."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "cloudformation",
                "operation_name": "DeleteStack",
            },
        )
        cmd = ev.shell_command
        assert cmd is not None
        # service_name passes through verbatim, operation normalized
        assert cmd == "aws cloudformation delete-stack"
        # The gate denies
        result = HookManager().on_tool_call("title", command=cmd, is_shell=True)
        assert result.action == TOOL_DENY

    # ── Whitespace fail-closed ──

    def test_whitespace_in_service_name_returns_none(self):
        """service_name with whitespace is rejected (fail-closed) — a
        multi-token service could confuse regex-based deny rules."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "s3api ; rm -rf /",
                "operation_name": "list-buckets",
            },
        )
        assert ev.shell_command is None

    def test_whitespace_in_operation_name_returns_none(self):
        """operation_name with whitespace is rejected (fail-closed)."""
        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="tool_call",
            is_shell=True,
            raw_tool_params={
                "service_name": "ssm",
                "operation_name": "send-command ; cat /etc/shadow",
            },
        )
        assert ev.shell_command is None

    def test_permission_event_pascal_case_denied(self):
        """permission_request path (tool_input JSON) also normalizes casing."""
        import json as _json

        from kiro_crew.acp.types import AcpEvent

        ev = AcpEvent(
            kind="permission_request",
            is_shell=True,
            tool_input=_json.dumps(
                {
                    "service_name": "dynamodb",
                    "operation_name": "DeleteTable",
                    "parameters": {"table-name": "users-prod"},
                }
            ),
        )
        result = HookManager().on_tool_call(
            "AWS: dynamodb DeleteTable", command=ev.shell_command, is_shell=True
        )
        assert result.action == TOOL_DENY


class TestHooksConfigFromDict:
    def test_empty(self):
        cfg = HooksConfig.from_dict({})
        assert cfg.auto_approve_subagent_spawn is False
        assert cfg.auto_approve_subagent_tools is False
        assert cfg.auto_replies == []

    def test_full(self):
        cfg = HooksConfig.from_dict(
            {
                "auto_approve_tools": ["ReadFile"],
                "auto_deny_tools": ["Danger"],
                "auto_replies": [{"pattern": "ping", "reply": "pong", "exact": True}],
                "transforms": [{"pattern": "deploy", "prefix": "[DEPLOY]"}],
                "auto_approve_subagent_spawn": True,
                "context_rules": [{"triggers": ["pipeline"], "context": "Use pipeline tool."}],
            }
        )
        assert "ReadFile" in cfg.auto_approve_tools
        assert len(cfg.auto_replies) == 1
        assert cfg.auto_replies[0].exact is True
        assert len(cfg.context_rules) == 1
        assert cfg.auto_approve_subagent_spawn is True
        assert cfg.auto_approve_subagent_tools is False  # independent flag, not inherited

    def test_subagent_tools_independent_of_spawn(self):
        cfg = HooksConfig.from_dict(
            {
                "auto_approve_subagent_spawn": True,
                "auto_approve_subagent_tools": False,
            }
        )
        assert cfg.auto_approve_subagent_spawn is True
        assert cfg.auto_approve_subagent_tools is False

    def test_subagent_tools_explicit_true(self):
        cfg = HooksConfig.from_dict(
            {
                "auto_approve_subagent_spawn": False,
                "auto_approve_subagent_tools": True,
            }
        )
        assert cfg.auto_approve_subagent_spawn is False
        assert cfg.auto_approve_subagent_tools is True

    def test_hook_manager_auto_approve_subagent_tools_property(self):
        from kiro_crew.hooks import HookManager

        cfg = HooksConfig.from_dict({"auto_approve_subagent_tools": True})
        mgr = HookManager(cfg)
        assert mgr.auto_approve_subagent_tools is True

    def test_hook_manager_auto_approve_subagent_tools_default(self):
        from kiro_crew.hooks import HookManager

        cfg = HooksConfig.from_dict({})
        mgr = HookManager(cfg)
        assert mgr.auto_approve_subagent_tools is False


class TestHookReload:
    def test_reload(self):
        mgr = HookManager()
        assert mgr.on_message("ping").action == HOOK_PASSTHROUGH

        mgr.reload(
            HooksConfig(auto_replies=[AutoReplyHook(pattern="ping", reply="pong", exact=True)])
        )
        assert mgr.on_message("ping").action == HOOK_REPLY


class TestSafeReadFile:
    def test_blocks_sensitive_path(self):
        with pytest.raises(PermissionError, match="sensitive path"):
            safe_read_file("~/.aws/credentials")

    def test_allows_normal_file(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        assert safe_read_file(str(f)) == '{"key": "value"}'

    def test_blocks_symlink_to_sensitive_path(self, tmp_path, monkeypatch):
        """A workspace symlink into ~/.aws must be refused through the link."""
        from kiro_crew.hooks import safe_read_file_bytes

        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        cred = home / ".aws" / "credentials"
        cred.write_text("[default]\nsecret\n")
        monkeypatch.setenv("HOME", str(home))
        ws = tmp_path / "workspace"
        ws.mkdir()
        link = ws / "cfg.ini"
        link.symlink_to(cred)
        with pytest.raises(PermissionError, match="sensitive path") as info:
            safe_read_file(str(link))
        # Named by the store the link reaches, not by the link's innocuous name.
        assert ".aws" in str(info.value)
        # bytes variant returns None (rejected) rather than leaking content
        assert safe_read_file_bytes(str(link)) is None

    @requires_symlinks
    def test_allows_benign_symlink(self, tmp_path):
        """A symlink to a non-sensitive file is still readable via its target.

        A FILE symlink, so a junction cannot stand in for it — the marker skips
        this only where symlink creation needs a privilege the shell lacks.
        """
        from kiro_crew.hooks import safe_read_file_bytes

        real = tmp_path / "real.txt"
        real.write_text("hello")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        assert safe_read_file(str(link)) == "hello"
        assert safe_read_file_bytes(str(link)) == b"hello"

    def test_blocks_symlinked_ancestor_dir_into_sensitive(self, tmp_path, monkeypatch):
        """A symlinked ANCESTOR directory pointing into ~/.aws is caught, not
        just a symlinked final file."""
        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        (home / ".aws" / "credentials").write_text("[default]\n")
        monkeypatch.setenv("HOME", str(home))
        ws = tmp_path / "workspace"
        ws.mkdir()
        # workspace/awslink -> ~/.aws ; read awslink/credentials
        (ws / "awslink").symlink_to(home / ".aws")
        with pytest.raises(PermissionError, match="sensitive path"):
            safe_read_file(str(ws / "awslink" / "credentials"))

    @requires_symlinks
    @pytest.mark.parametrize(
        ("protected_dir", "leaf"),
        [(".aws", "credentials"), (".ssh", "id_ed25519")],
        ids=["aws", "ssh"],
    )
    def test_blocks_a_protected_dir_that_is_itself_a_symlink(
        self, tmp_path, monkeypatch, protected_dir, leaf
    ):
        """``~/.aws`` can itself be a symlink to a directory OUTSIDE the home. The
        target set carries the store that entry resolves to, so the store is
        refused through every spelling -- absolute, ``~``, relative to a working
        directory of the home, and the ``realpath`` most callers hold -- by the
        reader, by the path screen every dashboard reader goes through, and by the
        gate itself on both tiers."""
        import os

        from kiro_crew import hooks as hooks_mod
        from kiro_crew import security

        home = tmp_path / "home"
        home.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / leaf).write_text("dummy secret\n")
        (home / protected_dir).symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(home)
        store = os.path.realpath(home / protected_dir / leaf)
        assert not (home / protected_dir / leaf).resolve().is_relative_to(home)

        for spelling in (
            str(home / protected_dir / leaf),
            f"~/{protected_dir}/{leaf}",
            f"{protected_dir}/{leaf}",
            store,
        ):
            with pytest.raises(PermissionError, match="sensitive path") as info:
                safe_read_file(spelling)
            # Named by the store it reaches: that is the form the fence matched.
            assert repr(store) in str(info.value)
            assert hooks_mod.validate_file_path(spelling) is None
            assert hooks_mod.screen_file_path(spelling) is hooks_mod.PathDecline.REFUSED
        assert security.is_sensitive_path(store) is True
        assert security.is_sensitive_write_path(store) is True
        # The build that fenced the store reports the link, so its cached set keeps
        # the short expiry and a repointed link is picked up at the floor.
        assert security._home_dir_targets(security._SENSITIVE_HOME_DIRS).resolution_differed

    def test_missing_file_raises_natural_error(self, tmp_path):
        """A missing (non-sensitive) file raises FileNotFoundError, not a
        security PermissionError — accurate error messages for callers."""
        with pytest.raises(FileNotFoundError):
            safe_read_file(str(tmp_path / "does-not-exist.txt"))

    def test_refuses_a_file_already_past_the_ceiling(self, tmp_path, monkeypatch):
        """A file whose open-time size exceeds MAX_FILE_BYTES is refused unread.

        EFBIG as an OSError, not a bespoke type: most callers already wrap this
        read in ``except OSError`` (``json.loads(safe_read_file(...))`` sites),
        so the refusal degrades there instead of escaping as a crash.
        """
        import errno

        from kiro_crew import hooks

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 16)
        f = tmp_path / "big.json"
        f.write_bytes(b"x" * 17)
        with pytest.raises(OSError) as caught:
            safe_read_file(str(f))
        assert caught.value.errno == errno.EFBIG
        assert not isinstance(caught.value, PermissionError)

    def test_refuses_a_file_that_grows_after_the_open(self, tmp_path, monkeypatch):
        """Growth between the size check and the read is refused mid-read.

        ``fstat`` describes the file as it was at open time, so a writer holding
        the same path can append while it is consumed. The append is driven from
        the identity check because that is the real seam between the open and the
        read — without the per-read charge this returns the grown content.
        """
        import errno

        from kiro_crew import hooks

        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 16)
        f = tmp_path / "grows.json"
        f.write_bytes(b"x" * 8)
        real_check = hooks._opened_file_matches_validated_path

        def append_then_check(fd: int, path: str) -> bool:
            with open(f, "ab") as sink:
                sink.write(b"y" * 64)
            return real_check(fd, path)

        monkeypatch.setattr(hooks, "_opened_file_matches_validated_path", append_then_check)
        with pytest.raises(OSError) as caught:
            safe_read_file(str(f))
        assert caught.value.errno == errno.EFBIG

    def test_collapses_crlf_like_the_text_handle_it_replaced(self, tmp_path):
        """CRLF still arrives as "\\n".

        The read moved from a text-mode handle to bytes plus a decode, and text
        mode was collapsing line endings on the way out; a caller comparing
        against a "\\n" literal would otherwise start seeing carriage returns.
        """
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"one\r\ntwo\rthree\n")
        assert safe_read_file(str(f)) == "one\ntwo\nthree\n"

    def test_identity_check_still_sees_the_opened_file_and_still_refuses(
        self, tmp_path, monkeypatch
    ):
        """The authorization hook gets the REAL fd, and refusing it still raises.

        The hook runs on the raw descriptor before any content reader is built.
        A hook handed some other descriptor would leave this check comparing the
        wrong file and passing everything, so the fd is pinned to the file's
        identity rather than only asserting that the refusal fires.
        """
        import os

        from kiro_crew import hooks

        f = tmp_path / "watched.txt"
        f.write_bytes(b"payload")
        expected = os.stat(f)
        seen: list[os.stat_result] = []

        def refuse(fd: int, path: str) -> bool:
            seen.append(os.fstat(fd))
            return False

        monkeypatch.setattr(hooks, "_opened_file_matches_validated_path", refuse)
        with pytest.raises(PermissionError, match="no longer matches safe path"):
            safe_read_file(str(f))
        assert [(s.st_dev, s.st_ino) for s in seen] == [(expected.st_dev, expected.st_ino)]

    def test_a_refused_descriptor_never_reaches_a_content_reader(self, tmp_path, monkeypatch):
        """Authorization runs before the handle is built, not after it is yielded.

        Routing the read through a context manager that opens AND wraps before
        yielding puts the wrapping first, which is how this ordering was lost
        once already.
        """
        import os

        from kiro_crew import hooks

        f = tmp_path / "refused.txt"
        f.write_bytes(b"must not be read")

        def forbidden(*args, **kwargs):
            pytest.fail("Refused descriptor reached the content reader")

        monkeypatch.setattr(hooks, "_opened_file_matches_validated_path", lambda _fd, _p: False)
        monkeypatch.setattr(os, "fdopen", forbidden)
        with pytest.raises(PermissionError, match="no longer matches safe path"):
            safe_read_file(str(f))

    def test_the_opener_receives_the_resolved_path_as_a_str(self, tmp_path, monkeypatch):
        """The leaf opener is a seam other suites stub, and those stubs match on ``str``.

        Handing it a ``Path`` silently bypasses such a stub, which reads as the
        stubbed refusal never firing rather than as a type mismatch.
        """
        import os

        from kiro_crew import platform_compat

        f = tmp_path / "plain.txt"
        f.write_bytes(b"content")
        real = platform_compat.open_file_no_reparse
        seen: list[object] = []

        def record(path, **kwargs):
            seen.append(path)
            return real(path, **kwargs)

        monkeypatch.setattr(platform_compat, "open_file_no_reparse", record)
        assert safe_read_file(str(f)) == "content"
        assert seen == [os.path.realpath(f)]
        assert all(isinstance(p, str) for p in seen)

    def test_an_unrelated_einval_is_not_renamed_into_a_safety_refusal(self, tmp_path, monkeypatch):
        """EINVAL is overloaded, so only the opener's own non-regular refusal may be remapped.

        A filesystem raising EINVAL for its own reason describes a legitimate file
        that could not be opened; reporting it as a safe-path mismatch tells the
        caller its file was refused on security grounds, which is a false claim.
        """
        import errno

        from kiro_crew import platform_compat

        f = tmp_path / "plain.txt"
        f.write_bytes(b"content")

        def einval(path, **kwargs):
            raise OSError(errno.EINVAL, "invalid argument for this filesystem", str(path))

        monkeypatch.setattr(platform_compat, "open_file_no_reparse", einval)
        with pytest.raises(OSError) as caught:
            safe_read_file(str(f))
        assert not isinstance(caught.value, PermissionError)
        assert caught.value.errno == errno.EINVAL
        assert "invalid argument for this filesystem" in str(caught.value)

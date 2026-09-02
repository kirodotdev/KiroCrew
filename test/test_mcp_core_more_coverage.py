"""Coverage tests for previously-untested ``kiro_crew.mcp_core`` surfaces.

Focus areas, all confirmed uncovered before this file existed:

* the parent-PID ladder (``_get_ppid`` / ``_ppid_via_libproc``) — every OS
  branch is driven with an injected fake, so the macOS libproc path and the
  ``ps`` last-resort fallback are exercised on any platform without ever
  spawning a real process or loading a real dylib,
* the four governance chokepoint helpers (``_deny_channel_agent_messaging``,
  ``_vet_messaging_governance``, ``_vet_channel_governance``,
  ``_vet_memory_writes_governance``) plus ``_audit_governance_deny`` — deny,
  allow, evaluation-error/degrade and fail-closed branches,
* the ``wait`` tool's whole sleep loop, driven by a fake clock (no real
  sleeping) — unidentified vs identified pings, the dashboard's early-end
  handshake, keepalive failure, and cancellation,
* the ``spawn_status`` / ``learn_add`` / ``learn_list`` / ``learn_remove`` /
  ``task_run`` / ``ops_mission_control_api`` tool bodies,
* small helpers: ``_redact_json_strings``, ``_autonudge_binding_key``,
  ``_casefold_match_span``'s expanding-fold fallbacks, ``_crew_machine_markers``,
  ``_crew_public_text`` and ``_crew_identity``.

Every HTTP call is mocked at mcp_core's own ``_get`` / ``_post`` / ``_delete``
seams; nothing here touches the network, a gateway, a subprocess, the sandbox,
git, or a path outside ``tmp_path``.
"""

from __future__ import annotations

import ctypes
import struct
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_core import (
    _audit_governance_deny,
    _autonudge_binding_key,
    _call_tool_inner,
    _casefold_match_span,
    _crew_identity,
    _crew_machine_markers,
    _crew_public_text,
    _deny_channel_agent_messaging,
    _get_ppid,
    _governance_app,
    _ppid_via_libproc,
    _redact_json_strings,
    _resolve_artifact_folder_id,
    _vet_channel_governance,
    _vet_memory_writes_governance,
    _vet_messaging_governance,
)
from kiro_crew.mcp_shared import ToolCancelled

_GOV = "kiro_crew.platform.governance_profiles"


def _learn_add_properties() -> dict[str, Any]:
    """The ``learn_add`` tool's advertised input properties."""
    from kiro_crew.mcp_tools.learn import schemas

    spec = next(s for s in schemas() if s["name"] == "learn_add")
    props: dict[str, Any] = spec["inputSchema"]["properties"]
    return props


class _RecordingSel:
    """Stand-in for ``sel()`` that records instead of writing the SEL log."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.governance: list[dict[str, Any]] = []

    def log_tool_invocation(self, **kw: Any) -> None:
        self.tools.append(kw)

    def log_governance_decision(self, **kw: Any) -> None:
        self.governance.append(kw)


class _FakeClock:
    """Monotonic clock advanced only by ``sleep`` — no real waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs


def _fake_libproc(ppid: int, ret: int = 232, expect_pid: int | None = None):
    """Build a ``ctypes.CDLL`` replacement whose ``proc_pidinfo`` fills a buffer.

    ``struct proc_bsdinfo`` opens with five uint32s; ``pbi_ppid`` is index 4,
    which is what the product unpacks.
    """

    def cdll(name: str, use_errno: bool = False):
        assert name == "libproc.dylib"

        def proc_pidinfo(pid, flavor, arg, buf, size):
            if expect_pid is not None:
                assert pid == expect_pid
            # flavor 3 == PROC_PIDTBSDINFO
            assert flavor == 3
            buf[0:20] = struct.pack("<5I", 0, 0, 0, pid, ppid)
            return ret

        return SimpleNamespace(proc_pidinfo=proc_pidinfo)

    return cdll


# ── _ppid_via_libproc ────────────────────────────────────────────────────


class TestPpidViaLibproc:
    def test_unpacks_pbi_ppid_from_the_libproc_buffer(self) -> None:
        with patch.object(ctypes, "CDLL", _fake_libproc(4242, expect_pid=1234)):
            assert _ppid_via_libproc(1234) == 4242

    def test_short_read_is_rejected_rather_than_unpacked(self) -> None:
        # n <= 16 means pbi_ppid (offset 16..20) was never written; a real
        # unpack there would return whatever the zeroed buffer held (0) and
        # look like a legitimate "parent is pid 0".
        with patch.object(ctypes, "CDLL", _fake_libproc(4242, ret=16)):
            assert _ppid_via_libproc(1234) == 0

    def test_missing_dylib_returns_zero_so_the_caller_can_fall_back(self) -> None:
        def boom(*_a: Any, **_kw: Any):
            raise OSError("libproc.dylib not found")

        with patch.object(ctypes, "CDLL", boom):
            assert _ppid_via_libproc(1234) == 0


# ── _get_ppid ───────────────────────────────────────────────────────────


class _FakeProcStatus:
    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self) -> str:
        return self._text


class TestGetPpid:
    def test_windows_delegates_to_platform_compat(self) -> None:
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Windows")):
            with patch.object(mcp_core.platform_compat, "get_ppid", return_value=77) as gp:
                assert _get_ppid(5) == 77
        gp.assert_called_once_with(5)

    def test_windows_zero_is_normalized_and_never_falls_through_to_ps(self) -> None:
        spawned: list[Any] = []
        fake_sub = SimpleNamespace(check_output=lambda *a, **k: spawned.append(a))
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Windows")):
            with patch.object(mcp_core.platform_compat, "get_ppid", return_value=0):
                with patch.object(mcp_core, "subprocess", fake_sub):
                    assert _get_ppid(5) == 0
        assert spawned == []

    def test_linux_parses_ppid_out_of_proc_status(self) -> None:
        status = "Name:\tpython3\nState:\tS (sleeping)\nPPid:\t9931\nTracerPid:\t0\n"
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Linux")):
            with patch.object(mcp_core, "Path", lambda p: _FakeProcStatus(status)):
                assert _get_ppid(1) == 9931

    def test_linux_status_without_a_ppid_line_falls_back_to_ps(self) -> None:
        fake_sub = SimpleNamespace(check_output=lambda *a, **k: " 4004\n")
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Linux")):
            with patch.object(mcp_core, "Path", lambda p: _FakeProcStatus("Name:\tx\n")):
                with patch.object(mcp_core, "subprocess", fake_sub):
                    assert _get_ppid(1) == 4004

    def test_darwin_uses_libproc_when_it_answers(self) -> None:
        fake_sub = SimpleNamespace(check_output=lambda *a, **k: pytest.fail("ps was spawned"))
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Darwin")):
            with patch.object(mcp_core, "_ppid_via_libproc", return_value=515):
                with patch.object(mcp_core, "subprocess", fake_sub):
                    assert _get_ppid(9) == 515

    def test_darwin_libproc_miss_falls_back_to_ps(self) -> None:
        calls: list[Any] = []

        def check_output(argv, **kw):
            calls.append(argv)
            return "  808 \n"

        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Darwin")):
            with patch.object(mcp_core, "_ppid_via_libproc", return_value=0):
                with patch.object(mcp_core, "subprocess", SimpleNamespace(check_output=check_output)):
                    assert _get_ppid(9) == 808
        assert calls == [["ps", "-o", "ppid=", "-p", "9"]]

    def test_blocked_ps_on_an_unknown_platform_returns_zero(self) -> None:
        def check_output(*_a: Any, **_kw: Any):
            raise PermissionError("Operation not permitted")

        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Plan9")):
            with patch.object(mcp_core, "subprocess", SimpleNamespace(check_output=check_output)):
                assert _get_ppid(9) == 0

    def test_only_the_first_ppid_line_is_read(self) -> None:
        # Every OS branch here is driven by injected fakes, so this runs on the
        # Windows runners too even though /proc is Linux-only in production.
        with patch.object(mcp_core, "platform", SimpleNamespace(system=lambda: "Linux")):
            with patch.object(mcp_core, "Path", lambda p: _FakeProcStatus("PPid:\t11\nPPid:\t22\n")):
                assert _get_ppid(1) == 11


# ── channel-agent containment + governance audit ─────────────────────────


class TestDenyChannelAgentMessaging:
    def test_non_channel_caller_is_not_denied(self) -> None:
        assert _deny_channel_agent_messaging("dashboard:chat-1-9", "send_message") is None

    def test_channel_caller_is_denied_and_audited(self) -> None:
        rec = _RecordingSel()
        with patch("kiro_crew.sel.sel", lambda: rec):
            out = _deny_channel_agent_messaging("channel:C123:agent-1", "send_message")
        assert out is not None
        assert "send_message is not available to channel agents" in out
        assert rec.tools[0]["outcome"] == "rejected_blocked_tool"
        assert rec.tools[0]["session_key"] == "channel:C123:agent-1"
        assert rec.tools[0]["tool_kind"] == "kirocrew-core"

    def test_audit_failure_never_unblocks_the_deny(self) -> None:
        def boom() -> Any:
            raise RuntimeError("SEL file unwritable")

        with patch("kiro_crew.sel.sel", boom):
            out = _deny_channel_agent_messaging("channel:C1:a", "send_notification")
        assert out is not None and "not available to channel agents" in out


class TestAuditGovernanceDeny:
    def test_records_the_decision_fields(self) -> None:
        rec = _RecordingSel()
        decision = SimpleNamespace(rule="no-messaging", layer="policy", reason="ceiling")
        with patch("kiro_crew.sel.sel", lambda: rec):
            _audit_governance_deny("dashboard:chat-1-9", "send_message", "channels", decision)
        assert rec.governance == [
            {
                "session_key": "dashboard:chat-1-9",
                "tool_name": "send_message",
                "scope": "channels",
                "outcome": "denied",
                "rule": "no-messaging",
                "layer": "policy",
                "reason": "ceiling",
            }
        ]

    def test_a_sel_failure_is_swallowed(self) -> None:
        def boom() -> Any:
            raise RuntimeError("no disk")

        with patch("kiro_crew.sel.sel", boom):
            assert _audit_governance_deny("s", "t", "scope", object()) is None


class TestGovernanceApp:
    def test_reads_the_app_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_APP_NAME", "ops-mission-control")
        assert _governance_app() == "ops-mission-control"

    def test_absent_outside_an_app_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
        assert _governance_app() == ""


class TestVetMessagingGovernance:
    def test_permitted_decision_returns_none(self) -> None:
        with patch(f"{_GOV}.vet_and_audit", return_value=SimpleNamespace(permitted=True)):
            assert _vet_messaging_governance("dashboard:chat-1-9") is None

    def test_denied_decision_returns_the_reason(self) -> None:
        with patch(f"{_GOV}.vet_and_audit", return_value=SimpleNamespace(permitted=False)) as va:
            out = _vet_messaging_governance("dashboard:chat-1-9", tool_name="send_notification")
        assert out == "outbound messaging blocked by governance policy"
        # The audit must be attributed to the REAL calling tool, not the default.
        assert va.call_args.kwargs["tool_name"] == "send_notification"
        assert va.call_args.kwargs["log_warning"] is False

    def test_evaluation_error_degrades_open_for_send_message(self) -> None:
        with patch(f"{_GOV}.vet_and_audit", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded") as degraded:
                assert _vet_messaging_governance("dashboard:chat-1-9") is None
        assert degraded.call_args.kwargs["scope"] == "capabilities.messaging"

    def test_evaluation_error_denies_when_fail_closed(self) -> None:
        with patch(f"{_GOV}.vet_and_audit", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded") as degraded:
                out = _vet_messaging_governance(
                    "dashboard:chat-1-9", tool_name="send_notification", fail_closed=True
                )
        assert out == "governance evaluation failed; denying (fail-closed)"
        # The degrade is audited on the fail-closed path too.
        assert degraded.call_count == 1

    def test_a_failing_degrade_audit_does_not_escape(self) -> None:
        with patch(f"{_GOV}.vet_and_audit", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded", side_effect=RuntimeError("no disk")):
                assert _vet_messaging_governance("dashboard:chat-1-9") is None

    def test_platform_composition_error_propagates(self) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with patch(f"{_GOV}.vet_and_audit", side_effect=PlatformCompositionError("unbooted")):
            with pytest.raises(PlatformCompositionError):
                _vet_messaging_governance("dashboard:chat-1-9")


class TestVetChannelGovernance:
    def test_permitted_transport_returns_none(self) -> None:
        with patch(f"{_GOV}.governance_permits", return_value=SimpleNamespace(permitted=True)):
            assert _vet_channel_governance("dashboard:chat-1-9", "slack") is None

    def test_denied_transport_names_the_transport_and_audits(self) -> None:
        rec = _RecordingSel()
        decision = SimpleNamespace(permitted=False, rule="channels", layer="policy", reason="off")
        with patch(f"{_GOV}.governance_permits", return_value=decision) as gp:
            with patch("kiro_crew.sel.sel", lambda: rec):
                out = _vet_channel_governance("dashboard:chat-1-9", "discord")
        assert out == "messaging via transport 'discord' blocked by governance policy"
        # A bare member id queries the ScopedMap ``members`` ruleset.
        assert gp.call_args.args == ("channels", "discord")
        assert rec.governance[0]["tool_name"] == "send_message:discord"
        assert rec.governance[0]["scope"] == "channels"

    def test_evaluation_error_degrades_open_and_is_audited(self) -> None:
        with patch(f"{_GOV}.governance_permits", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded") as degraded:
                assert _vet_channel_governance("dashboard:chat-1-9", "slack") is None
        assert degraded.call_args.args == ("send_message:slack",)
        assert degraded.call_args.kwargs["scope"] == "channels"

    def test_a_failing_degrade_audit_does_not_escape(self) -> None:
        with patch(f"{_GOV}.governance_permits", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded", side_effect=RuntimeError("no disk")):
                assert _vet_channel_governance("dashboard:chat-1-9", "slack") is None

    def test_platform_composition_error_propagates(self) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with patch(f"{_GOV}.governance_permits", side_effect=PlatformCompositionError("unbooted")):
            with pytest.raises(PlatformCompositionError):
                _vet_channel_governance("dashboard:chat-1-9", "slack")


class TestVetMemoryWritesGovernance:
    def test_permitted_returns_none(self) -> None:
        with patch(f"{_GOV}.governance_permits", return_value=SimpleNamespace(permitted=True)):
            assert _vet_memory_writes_governance("dashboard:chat-1-9") is None

    def test_denied_write_is_reported_and_audited_as_learn_add(self) -> None:
        rec = _RecordingSel()
        decision = SimpleNamespace(
            permitted=False, rule="memory_writes", layer="profile", reason="sandboxed app"
        )
        with patch(f"{_GOV}.governance_permits", return_value=decision) as gp:
            with patch("kiro_crew.sel.sel", lambda: rec):
                out = _vet_memory_writes_governance("dashboard:chat-1-9")
        assert out == "durable memory writes blocked by governance policy"
        assert gp.call_args.args == ("capabilities.memory_writes", "")
        assert rec.governance[0]["tool_name"] == "learn_add"
        assert rec.governance[0]["scope"] == "capabilities.memory_writes"

    def test_evaluation_error_degrades_open(self) -> None:
        with patch(f"{_GOV}.governance_permits", side_effect=RuntimeError("no context")):
            with patch(f"{_GOV}.audit_governance_degraded") as degraded:
                assert _vet_memory_writes_governance("dashboard:chat-1-9") is None
        assert degraded.call_args.kwargs["scope"] == "capabilities.memory_writes"

    def test_platform_composition_error_propagates(self) -> None:
        from kiro_crew.platform.context import PlatformCompositionError

        with patch(f"{_GOV}.governance_permits", side_effect=PlatformCompositionError("x")):
            with pytest.raises(PlatformCompositionError):
                _vet_memory_writes_governance("dashboard:chat-1-9")


# ── small helpers ───────────────────────────────────────────────────────


class TestRedactJsonStrings:
    def test_redacts_keys_and_values_recursively_and_passes_scalars_through(self) -> None:
        with patch.object(mcp_core, "redact", lambda s: s.replace("SECRET", "[REDACTED]")):
            out = _redact_json_strings(
                {
                    "SECRET-key": ["SECRET-item", 3, None, True],
                    "nested": {"inner": "keep SECRET here"},
                    "num": 7,
                }
            )
        assert out == {
            "[REDACTED]-key": ["[REDACTED]-item", 3, None, True],
            "nested": {"inner": "keep [REDACTED] here"},
            "num": 7,
        }

    def test_a_bare_scalar_is_returned_unchanged(self) -> None:
        assert _redact_json_strings(12) == 12
        assert _redact_json_strings(None) is None


class TestAutonudgeBindingKey:
    @pytest.mark.parametrize(
        "session_key,expected",
        [
            ("dashboard:chat-3-1712345678", "chat-3-1712345678"),
            ("slack:C123:1712345678.1", "slack:C123:1712345678.1"),
            ("discord:99:1", "discord:99:1"),
            ("cron:nightly", None),
            ("subagent:abc123", None),
            ("", None),
        ],
    )
    def test_maps_only_nudge_able_sessions(self, session_key: str, expected: str | None) -> None:
        assert _autonudge_binding_key(session_key) == expected


class TestCasefoldMatchSpanExpandingFolds:
    """``ß`` casefolds to ``ss``, so a match offset can land mid-expansion."""

    def test_start_offset_inside_an_expansion_snaps_to_the_enclosing_char(self) -> None:
        # "aßb".casefold() == "assb"; needle "sb" starts at cf offset 2, which is
        # no source-char boundary (bounds are 0,1,3,4) -> snap back to the 'ß'.
        span = _casefold_match_span("aßb", "sb")
        assert span == (1, 3)
        assert "aßb"[span[0] : span[1]] == "ßb"

    def test_end_offset_inside_an_expansion_snaps_outward(self) -> None:
        # needle "as" ends at cf offset 2, mid-'ß' -> snap out to include it.
        span = _casefold_match_span("aßb", "as")
        assert span == (0, 2)
        assert "aßb"[span[0] : span[1]] == "aß"

    def test_no_match_and_empty_needle_return_none(self) -> None:
        assert _casefold_match_span("abc", "zz") is None
        assert _casefold_match_span("abc", "") is None


class TestResolveArtifactFolderId:
    def test_root_and_blank_refs_resolve_to_the_root_folder(self) -> None:
        assert _resolve_artifact_folder_id("") == ("", None)
        assert _resolve_artifact_folder_id("  RooT ") == ("", None)

    def test_a_backend_error_is_propagated_not_swallowed(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"error": "HTTP 403"}) as g:
            assert _resolve_artifact_folder_id("Designs") == ("", "HTTP 403")
        g.assert_called_once_with("/api/artifact-folders")

    def test_a_ref_of_only_separators_resolves_to_root(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"folders": []}):
            assert _resolve_artifact_folder_id("///") == ("", None)

    def test_a_nested_human_path_resolves_case_insensitively(self) -> None:
        folders = [
            {"id": "f1", "name": "Designs", "parent_id": None},
            {"id": "f2", "name": "Mocks", "parent_id": "f1"},
        ]
        with patch.object(mcp_core, "_get", return_value={"folders": folders}):
            assert _resolve_artifact_folder_id("designs/MOCKS") == ("f2", None)
            assert _resolve_artifact_folder_id("f2") == ("f2", None)
            fid, err = _resolve_artifact_folder_id("designs/nope")
            assert fid == "" and err == "folder not found: designs/nope"


class TestCrewMachineMarkers:
    def test_a_root_temp_dir_is_never_used_as_a_marker(self) -> None:
        # A marker of "/" would rewrite every slash in a public comment.
        with patch.object(mcp_core, "tempfile", SimpleNamespace(gettempdir=lambda: "/")):
            values = [value for value, _ in _crew_machine_markers()]
        assert "/" not in values

    def test_markers_are_longest_first(self) -> None:
        markers = _crew_machine_markers()
        lengths = [len(value) for value, _ in markers]
        assert lengths == sorted(lengths, reverse=True)

    def test_a_short_hostname_is_not_scrubbed(self) -> None:
        with patch.object(mcp_core.socket, "gethostname", return_value="dev"):
            assert "dev" not in [value for value, _ in _crew_machine_markers()]

    def test_a_distinctive_hostname_is_scrubbed(self) -> None:
        with patch.object(mcp_core.socket, "gethostname", return_value="dev-dsk-example-2b"):
            assert ("dev-dsk-example-2b", "<host>") in _crew_machine_markers()

    def test_an_unavailable_hostname_is_tolerated(self) -> None:
        with patch.object(mcp_core.socket, "gethostname", side_effect=OSError("no dns")):
            assert all(placeholder != "<host>" for _, placeholder in _crew_machine_markers())


class TestCrewPublicText:
    def test_a_windows_marker_is_scrubbed_in_both_slash_forms(self) -> None:
        markers = [("C:\\Users\\alice", "<home>")]
        with patch.object(mcp_core, "_crew_machine_markers", return_value=markers):
            with patch.object(mcp_core, "redact", lambda s: s):
                out = _crew_public_text("saw C:\\Users\\alice and C:/Users/alice")
        assert out == "saw <home> and <home>"
        assert "alice" not in out

    def test_a_posix_marker_is_scrubbed_once(self) -> None:
        with patch.object(mcp_core, "_crew_machine_markers", return_value=[("/home/bob", "<home>")]):
            with patch.object(mcp_core, "redact", lambda s: s):
                assert _crew_public_text("cwd=/home/bob/x") == "cwd=<home>/x"


class TestCrewIdentity:
    def test_identity_is_taken_from_the_top_level_when_present(self) -> None:
        payload = {"owner": "o", "repo": "r", "crew": {"id": "c1"}}
        assert _crew_identity(payload) == ("o", "r", "c1")

    def test_identity_falls_back_to_the_crew_record_then_a_work_item(self) -> None:
        assert _crew_identity({"crew": {"owner": "o", "repo": "r", "id": "c2"}}) == ("o", "r", "c2")
        payload = {
            "crew": {},
            "items": ["not-a-dict", {"owner": "o", "repo": "r", "crew_id": "c3"}],
        }
        assert _crew_identity(payload) == ("o", "r", "c3")

    def test_missing_owner_repo_or_crew_id_yields_none(self) -> None:
        assert _crew_identity({"crew": {"id": "c1"}}) is None
        # owner/repo present but no crew id anywhere: a write cannot be addressed.
        assert _crew_identity({"owner": "o", "repo": "r", "crew": {}, "items": []}) is None
        # Malformed shapes must not raise.
        assert _crew_identity({"crew": "nope", "items": "nope"}) is None


# ── the ``wait`` tool ───────────────────────────────────────────────────


class TestWaitTool:
    def _run(self, args: dict[str, Any], *, strict_key: str, post):
        clock = _FakeClock()
        rec = _RecordingSel()
        with patch.object(mcp_core, "time", clock):
            with patch.object(mcp_core, "_resolve_session_key_strict", return_value=strict_key):
                with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
                    with patch("kiro_crew.mcp_tools.control.is_tool_cancelled", return_value=False):
                        with patch.object(mcp_core, "sel", lambda: rec):
                            with patch.object(mcp_core, "_post", post) as p:
                                out = _call_tool_inner("wait", dict(args))
        return out, clock, rec, p

    def test_an_unidentified_sleep_publishes_nothing_and_pings_slowly(self) -> None:
        posts: list[tuple[str, dict]] = []

        def post(path, body=None, **_kw):
            posts.append((path, body))
            return {}

        out, clock, rec, _ = self._run(
            {"seconds": 120, "reason": "waiting on CI"}, strict_key="", post=post
        )
        assert out == "Waited 120s. Resuming: waiting on CI"
        # No wait_id is published without an authoritative identity, and no
        # retirement POST is sent because nothing was ever published.
        assert [body for _, body in posts] == [{}, {}]
        # Unidentified sleeps revert to the 60s staleness cadence, not 5s.
        assert clock.slept == [60.0, 60.0]
        assert rec.tools[0]["tool_name"] == "wait"
        assert rec.tools[0]["outcome"] == "success"

    def test_an_identified_sleep_publishes_its_deadline_and_retires_the_card(self) -> None:
        posts: list[tuple[str, dict]] = []

        def post(path, body=None, **_kw):
            posts.append((path, body))
            return {}

        out, clock, _rec, _ = self._run(
            {"seconds": 60, "reason": "deploy"}, strict_key="dashboard:chat-1-9", post=post
        )
        assert out == "Waited 60s. Resuming: deploy"
        first = posts[0][1]
        assert first["seconds"] == 60
        assert first["remaining"] == 60
        assert first["interval"] == mcp_core.WAIT_PING_SECS
        wait_id = first["wait_id"]
        assert posts[-1][1] == {"wait_id": wait_id, "wait_done": True}
        # 5s cadence while identified -> 12 sleeps over a 60s wait.
        assert clock.slept == [5.0] * 12

    def test_only_a_reply_naming_this_wait_ends_it_early(self) -> None:
        seen: list[dict] = []

        def post(path, body=None, **_kw):
            body = body or {}
            seen.append(body)
            if len(seen) == 1:
                # A stale reply about a DIFFERENT sleep must be ignored.
                return {"end_wait": "some-other-wait-id"}
            return {"end_wait": body.get("wait_id")}

        out, clock, _rec, _ = self._run(
            {"seconds": 300, "reason": "review"}, strict_key="dashboard:chat-1-9", post=post
        )
        assert out.startswith("Wait ended early by the user after 5s of 300s.")
        assert out.endswith("Resuming: review")
        # One sleep happened between the ignored reply and the matching one.
        assert clock.slept == [5.0]
        assert seen[-1] == {"wait_id": seen[0]["wait_id"], "wait_done": True}

    def test_an_unidentified_sleep_ignores_an_end_wait_reply(self) -> None:
        def post(path, body=None, **_kw):
            # Backend answering about somebody else's wait; we sent no wait_id.
            return {"end_wait": "whatever"}

        out, clock, _rec, _ = self._run(
            {"seconds": 60, "reason": "x"}, strict_key="", post=post
        )
        assert out == "Waited 60s. Resuming: x"
        assert clock.slept == [60.0]

    def test_a_failing_keepalive_is_best_effort(self) -> None:
        def post(path, body=None, **_kw):
            raise OSError("gateway down")

        out, clock, _rec, _ = self._run(
            {"seconds": 60, "reason": "x"}, strict_key="dashboard:chat-1-9", post=post
        )
        assert out == "Waited 60s. Resuming: x"
        assert clock.slept == [5.0] * 12

    def test_a_failing_retirement_post_does_not_break_the_result(self) -> None:
        calls: list[dict] = []

        def post(path, body=None, **_kw):
            body = body or {}
            calls.append(body)
            if body.get("wait_done"):
                raise OSError("gateway down")
            return {}

        out, _clock, _rec, _ = self._run(
            {"seconds": 60, "reason": "x"}, strict_key="dashboard:chat-1-9", post=post
        )
        assert out == "Waited 60s. Resuming: x"
        assert calls[-1]["wait_done"] is True

    def test_cancellation_raises_tool_cancelled_with_elapsed_seconds(self) -> None:
        clock = _FakeClock()
        rec = _RecordingSel()
        with patch.object(mcp_core, "time", clock):
            with patch.object(mcp_core, "_resolve_session_key_strict", return_value=""):
                with patch("kiro_crew.mcp_tools.control.is_tool_cancelled", return_value=True):
                    with patch.object(mcp_core, "sel", lambda: rec):
                        with patch.object(mcp_core, "_post", lambda *a, **k: {}):
                            with pytest.raises(ToolCancelled) as ei:
                                _call_tool_inner("wait", {"seconds": 60, "reason": "x"})
        assert "wait cancelled after 0s" in str(ei.value)
        # A cancelled sleep never reports success.
        assert rec.tools == []

    def test_the_reason_is_redacted_before_it_reaches_the_transcript(self) -> None:
        out, _clock, _rec, _ = self._run(
            {"seconds": 60, "reason": "creds AKIAIOSFODNN7EXAMPLE in the log"},
            strict_key="",
            post=lambda *a, **k: {},
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert out.startswith("Waited 60s. Resuming: ")


# ── spawn_status / learn_* / task_run ───────────────────────────────────


class TestSpawnStatusTool:
    def test_a_non_alphanumeric_agent_id_is_refused_before_any_request(self) -> None:
        with patch.object(mcp_core, "_get", side_effect=AssertionError("no request expected")):
            assert _call_tool_inner("spawn_status", {"agent_id": "../etc"}) == (
                "Error: invalid agent_id"
            )
            assert _call_tool_inner("spawn_status", {}) == "Error: invalid agent_id"

    def test_paging_and_grep_arguments_become_query_parameters(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"result": "ok"}) as g:
            _call_tool_inner(
                "spawn_status",
                {"agent_id": "abc123", "offset": 10, "limit": 5, "grep": " ERROR "},
            )
        path = g.call_args.args[0]
        assert path.startswith("/api/spawn/abc123?")
        assert "offset=10" in path and "limit=5" in path and "grep=" in path

    def test_non_positive_paging_values_are_dropped(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"result": "ok"}) as g:
            _call_tool_inner(
                "spawn_status",
                {"agent_id": "abc123", "offset": 0, "limit": -1, "grep": "   "},
            )
        assert g.call_args.args[0] == "/api/spawn/abc123"

    def test_a_transport_error_is_surfaced(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"error": "HTTP 404"}):
            out = _call_tool_inner("spawn_status", {"agent_id": "abc123"})
        assert out == "Error: HTTP 404"

    def test_a_grep_error_from_the_backend_is_surfaced(self) -> None:
        payload = {"result": "x", "result_meta": {"grep_error": "bad regex"}}
        with patch.object(mcp_core, "_get", return_value=payload):
            assert _call_tool_inner("spawn_status", {"agent_id": "a1"}) == "Error: bad regex"

    def test_an_empty_result_is_reported_as_such(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"result": ""}):
            assert _call_tool_inner("spawn_status", {"agent_id": "a1"}) == "_No result._"

    def test_a_paged_read_is_prefixed_with_a_continuation_header(self) -> None:
        payload = {
            "result": "line-a\nline-b",
            "result_meta": {
                "total_lines": 90,
                "matched_lines": 4,
                "offset": 10,
                "returned_lines": 2,
                "has_more": True,
            },
        }
        with patch.object(mcp_core, "_get", return_value=payload):
            out = _call_tool_inner("spawn_status", {"agent_id": "a1"})
        header, body = out.split("\n", 1)
        assert "4 line(s) matched grep of 90 total" in header
        assert "showing lines 10-12 of 90" in header
        assert "call again with offset=12" in header
        assert body == "line-a\nline-b"

    def test_the_transcript_is_redacted(self) -> None:
        payload = {"result": "token AKIAIOSFODNN7EXAMPLE leaked"}
        with patch.object(mcp_core, "_get", return_value=payload):
            out = _call_tool_inner("spawn_status", {"agent_id": "a1"})
        assert "AKIAIOSFODNN7EXAMPLE" not in out


class TestLearnAddTool:
    def test_a_missing_rule_is_refused(self) -> None:
        assert _call_tool_inner("learn_add", {}) == "Error: rule is required"

    def test_a_governance_denial_blocks_the_write(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with patch.object(mcp_core, "_vet_memory_writes_governance", return_value="blocked"):
                with patch.object(mcp_core, "_post", side_effect=AssertionError("no write")):
                    out = _call_tool_inner("learn_add", {"rule": "always X"})
        assert out == "Error: blocked"

    def _allowed(self):
        return patch.object(mcp_core, "_vet_memory_writes_governance", return_value=None)

    def test_the_tool_no_longer_offers_a_workspace_scope(self) -> None:
        # The workspace tier never reached a prompt, so advertising it told the
        # model it could restrict a correction when the save changed nothing.
        #
        # A stale client still holding that schema is REFUSED, not silently
        # converted. This test previously pinned the opposite ("ignored rather than
        # honoured"), which was wrong in the dangerous direction: forcing the
        # payload to global takes a correction meant for one workspace and applies
        # it in every session, which is worse than the inert tier it replaced.
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with self._allowed():
                with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
                    out = _call_tool_inner(
                        "learn_add",
                        {"rule": "r", "category": "tool", "scope": "workspace", "workspace": "w"},
                    )
        assert out.startswith("Error:")
        assert "repo_scope" in out
        p.assert_not_called()
        props = _learn_add_properties()
        assert "workspace" not in props
        assert "scope" not in props

    def test_an_absent_scope_still_saves_globally(self) -> None:
        # The refusal above must not catch the normal path: no scope key at all is
        # the ordinary global save, and that is what the payload still carries.
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with self._allowed():
                with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
                    out = _call_tool_inner("learn_add", {"rule": "r", "category": "tool"})
        assert out == "Saved lesson: r"
        assert p.call_args.args[1] == {"rule": "r", "category": "tool", "scope": "global"}

    def test_the_negative_clause_and_repo_scope_are_forwarded(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with self._allowed():
                with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
                    out = _call_tool_inner(
                        "learn_add",
                        {
                            "rule": "always X",
                            "category": "preference",
                            "negative": "never Y",
                            "repo_scope": "src/kiro_crew",
                        },
                    )
        assert out == "Saved lesson (applies only in src/kiro_crew): always X"
        assert p.call_args.args == (
            "/api/lessons",
            {
                "rule": "always X",
                "category": "preference",
                "scope": "global",
                "negative": "never Y",
                "repo_scope": "src/kiro_crew",
            },
        )

    def test_the_advertised_repo_scope_cap_tracks_the_enforced_one(self) -> None:
        # The hint must be derived from the field the validator enforces, so a
        # future cap change cannot leave the model told an obsolete limit.
        from kiro_crew.validation import LEARN_ADD_SCHEMA

        enforced = next(f.max_len for f in LEARN_ADD_SCHEMA.fields if f.name == "repo_scope")
        assert _learn_add_properties()["repo_scope"]["maxLength"] == enforced

    def test_an_unknown_session_error_becomes_an_actionable_message(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with self._allowed():
                with patch.object(mcp_core, "_post", return_value={"error": "unknown session"}):
                    out = _call_tool_inner("learn_add", {"rule": "r"})
        assert out.startswith("Lesson was NOT saved:")
        assert "re-state the lesson" in out

    def test_any_other_backend_error_is_passed_through(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with self._allowed():
                with patch.object(mcp_core, "_post", return_value={"error": "HTTP 500"}):
                    assert _call_tool_inner("learn_add", {"rule": "r"}) == "Error: HTTP 500"


class TestLearnListAndRemoveTools:
    def test_a_transport_error_is_not_rendered_as_an_empty_memory(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"error": "HTTP 403"}):
            assert _call_tool_inner("learn_list", {}) == "Error: HTTP 403"

    def test_an_empty_store_says_so(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"lessons": []}):
            assert _call_tool_inner("learn_list", {}) == "No lessons saved."

    def test_lessons_are_rendered_with_their_category(self) -> None:
        lessons = [{"category": "tool", "rule": "use X"}, {"rule": "no category"}]
        with patch.object(mcp_core, "_get", return_value={"lessons": lessons}):
            out = _call_tool_inner("learn_list", {})
        assert out == "[tool] use X\n[?] no category"

    def test_learn_remove_reports_the_query_it_deleted_by(self) -> None:
        with patch.object(mcp_core, "_delete", return_value={"removed": 2}) as d:
            out = _call_tool_inner("learn_remove", {"query": "always X"})
        assert out == "Removed lessons matching: always X"
        assert d.call_args.args == ("/api/lessons", {"rule": "always X"})

    def test_learn_remove_surfaces_a_backend_error(self) -> None:
        with patch.object(mcp_core, "_delete", return_value={"error": "HTTP 500"}):
            assert _call_tool_inner("learn_remove", {"query": "q"}) == "Error: HTTP 500"


class TestTaskRunTool:
    def test_a_cron_caller_is_attributed_as_cron(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="cron:nightly"):
            with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
                out = _call_tool_inner("task_run", {"spec": "do the thing", "name": "Nightly"})
        assert out == "Task runner started: Nightly"
        assert p.call_args.args[1]["source"] == "cron"

    def test_an_unnamed_task_is_labelled_from_the_spec_head(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
                out = _call_tool_inner("task_run", {"spec": "s" * 200})
        assert p.call_args.args[1]["source"] == "mcp"
        assert out == "Task runner started: " + "s" * 80

    def test_a_backend_error_is_surfaced(self) -> None:
        with patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:c"):
            with patch.object(mcp_core, "_post", return_value={"error": "no runner"}):
                assert _call_tool_inner("task_run", {"spec": "x"}) == "Error: no runner"


# ── ops_mission_control_api ─────────────────────────────────────────────


class TestOpsMissionControlApiTool:
    def test_a_pair_outside_the_allowlist_is_refused_at_the_handler(self) -> None:
        # Defense in depth: the schema checks the same pair, so this branch is
        # only reachable by calling the handler directly — which is exactly the
        # property it exists to guarantee.
        with patch.object(mcp_core, "_get", side_effect=AssertionError("no request")):
            out = _call_tool_inner(
                "ops_mission_control_api", {"method": "GET", "path": "/dispatch"}
            )
        assert out == (
            "Error: GET /dispatch is not part of the ops-mission-control agent surface."
        )

    def test_a_get_is_prefixed_with_the_app_route_and_carries_the_query(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"incidents": []}) as g:
            out = _call_tool_inner(
                "ops_mission_control_api",
                {"method": "GET", "path": "/incidents", "query": "status=open"},
            )
        assert g.call_args.args == ("/api/apps/ops-mission-control/incidents?status=open",)
        assert out == '{"incidents": []}'

    def test_a_malformed_body_is_refused(self) -> None:
        with patch.object(mcp_core, "_post", side_effect=AssertionError("no request")):
            out = _call_tool_inner(
                "ops_mission_control_api",
                {"method": "POST", "path": "/ledger", "body_json": "{not json"},
            )
        assert out == "Error: body_json is not valid JSON."

    def test_a_non_object_body_is_refused(self) -> None:
        with patch.object(mcp_core, "_post", side_effect=AssertionError("no request")):
            out = _call_tool_inner(
                "ops_mission_control_api",
                {"method": "POST", "path": "/ledger", "body_json": "[1, 2]"},
            )
        assert out == "Error: body_json must encode a JSON object."

    def test_a_post_body_is_sanitized_and_redacted_on_the_way_in(self) -> None:
        body_json = '{"note": "key AKIAIOSFODNN7EXAMPLE", "zw": "a\\u200bb"}'
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
            out = _call_tool_inner(
                "ops_mission_control_api",
                {"method": "POST", "path": "/ledger", "body_json": body_json},
            )
        sent = p.call_args.args[1]
        assert "AKIAIOSFODNN7EXAMPLE" not in sent["note"]
        assert "\u200b" not in sent["zw"]
        assert out == '{"ok": true}'

    def test_an_empty_body_posts_an_empty_object(self) -> None:
        with patch.object(mcp_core, "_post", return_value={"ok": True}) as p:
            _call_tool_inner(
                "ops_mission_control_api", {"method": "POST", "path": "/ledger/hygiene"}
            )
        assert p.call_args.args == ("/api/apps/ops-mission-control/ledger/hygiene", {})

    def test_an_oversized_response_is_truncated_with_a_narrowing_hint(self) -> None:
        with patch.object(mcp_core, "_get", return_value={"blob": "x" * 70_000}):
            out = _call_tool_inner(
                "ops_mission_control_api", {"method": "GET", "path": "/state"}
            )
        assert len(out) < 70_000
        assert "truncated (" in out
        assert "Narrow the" in out

    def test_the_response_is_redacted_before_truncation(self) -> None:
        payload = {"signal": "creds AKIAIOSFODNN7EXAMPLE here"}
        with patch.object(mcp_core, "_get", return_value=payload):
            out = _call_tool_inner(
                "ops_mission_control_api", {"method": "GET", "path": "/signals"}
            )
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_a_non_serializable_response_value_still_renders(self) -> None:
        # ``default=str`` keeps a stray object from raising out of the tool.
        with patch.object(mcp_core, "_get", return_value={"when": object()}):
            out = _call_tool_inner(
                "ops_mission_control_api", {"method": "GET", "path": "/rotation"}
            )
        assert out.startswith('{"when": "<object object at')


# ── resource_status ─────────────────────────────────────────────────────


class TestResourceStatusTool:
    def _run(self, posture: str, *, cap: Any = 3):
        rstatus = SimpleNamespace(
            posture=posture, summary_lines=lambda: ["Memory: 19G free", "Load: 1.2"]
        )
        cfg = SimpleNamespace(load=staticmethod(lambda: object()))
        resolver = (
            (lambda _c: (_ for _ in ()).throw(RuntimeError("unreadable config")))
            if cap == "raise"
            else (lambda _c: cap)
        )
        with patch("kiro_crew.resource_status.probe", return_value=rstatus):
            with patch("kiro_crew.mcp_tools.spawn.KiroCrewConfig", cfg):
                with patch("kiro_crew.mcp_tools.spawn.resolve_max_subagents", resolver):
                    return _call_tool_inner("resource_status", {})

    def test_the_probe_summary_and_cap_are_reported(self) -> None:
        out = self._run("ample")
        assert out.startswith("Memory: 19G free\nLoad: 1.2\n")
        assert "  Concurrent sub-agent cap: 3" in out

    def test_an_unreadable_config_omits_the_cap_line_instead_of_failing(self) -> None:
        out = self._run("ample", cap="raise")
        assert "Concurrent sub-agent cap" not in out
        assert "ample headroom" in out

    def test_a_zero_cap_is_not_advertised(self) -> None:
        assert "Concurrent sub-agent cap" not in self._run("ample", cap=0)

    @pytest.mark.parametrize(
        "posture,needle",
        [
            ("critical", "do NOT start heavy work"),
            ("tight", "prefer the lighter path"),
            ("ample", "ample headroom — heavy work is fine"),
            # An unmeasurable host must not silently read as "fine".
            ("unknown", "headroom could not be measured"),
        ],
    )
    def test_each_posture_gets_its_own_guidance(self, posture: str, needle: str) -> None:
        assert needle in self._run(posture)


# ── issue_radar_record_investigation ────────────────────────────────────


class TestIssueRadarRecordInvestigation:
    _BASE = {"owner": "o", "repo": "r", "number": 7}

    def test_identity_fields_are_sent_explicitly_with_defaults(self) -> None:
        with patch.object(mcp_core, "_put", return_value={"investigation": {}}) as p:
            out = _call_tool_inner("issue_radar_record_investigation", dict(self._BASE))
        assert p.call_args.args == (
            "/api/apps/issue-radar/investigation",
            {
                "owner": "o",
                "repo": "r",
                "number": 7,
                "provider": "github",
                "host": "github.com",
                "kind": "issue",
                "status": "resolved",
            },
        )
        assert out.startswith("Recorded status `resolved` for o/r#7")
        assert "status badge only" in out

    def test_empty_finding_fields_are_dropped_so_a_partial_update_keeps_prior_text(self) -> None:
        args = dict(self._BASE, verdict="real bug", root_cause="", summary="", next_action="fix")
        saved = {"investigation": {"findings": {"verdict": "real bug"}}}
        with patch.object(mcp_core, "_put", return_value=saved) as p:
            out = _call_tool_inner("issue_radar_record_investigation", args)
        findings = p.call_args.args[1]["findings"]
        assert set(findings) == {"verdict", "next_action"}
        assert "verdict `real bug`" in out

    def test_findings_and_labels_are_redacted_on_the_way_in(self) -> None:
        args = dict(
            self._BASE,
            verdict="leaks AKIAIOSFODNN7EXAMPLE",
            suggested_labels=["", "bug AKIAIOSFODNN7EXAMPLE"],
        )
        with patch.object(mcp_core, "_put", return_value={"investigation": {}}) as p:
            _call_tool_inner("issue_radar_record_investigation", args)
        findings = p.call_args.args[1]["findings"]
        assert "AKIAIOSFODNN7EXAMPLE" not in findings["verdict"]
        # The falsy label is dropped, the redacted one survives.
        assert len(findings["suggested_labels"]) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in findings["suggested_labels"][0]

    def test_a_gitlab_merge_request_is_referenced_with_a_bang(self) -> None:
        args = dict(
            self._BASE, provider="gitlab", host="gitlab.com", kind="pull", verdict="ok"
        )
        saved = {"investigation": {"findings": {"verdict": "ok"}}}
        with patch.object(mcp_core, "_put", return_value=saved):
            out = _call_tool_inner("issue_radar_record_investigation", args)
        assert "for o/r!7:" in out

    def test_a_github_pull_request_keeps_the_hash_form(self) -> None:
        args = dict(self._BASE, kind="pull", verdict="ok")
        saved = {"investigation": {"findings": {"verdict": "ok"}}}
        with patch.object(mcp_core, "_put", return_value=saved):
            assert "for o/r#7:" in _call_tool_inner("issue_radar_record_investigation", args)

    def test_saved_findings_without_a_verdict_are_labelled(self) -> None:
        saved = {"investigation": {"findings": {"summary": "s"}}}
        with patch.object(mcp_core, "_put", return_value=saved):
            out = _call_tool_inner(
                "issue_radar_record_investigation", dict(self._BASE, summary="s")
            )
        assert "verdict `(no verdict)`" in out

    def test_a_backend_error_is_surfaced(self) -> None:
        with patch.object(mcp_core, "_put", return_value={"error": "HTTP 409"}):
            out = _call_tool_inner("issue_radar_record_investigation", dict(self._BASE))
        assert out == "Error: HTTP 409"

"""Tests for the auto-discovery of executable hook scripts in ``~/.kiro/hooks/``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from agent_helpers import _pinned_kiro_cli_version  # noqa: F401

from conftest import requires_symlinks


class TestKiroHooksAutoimport:
    """Tests for auto-discovery of executable hook scripts in ~/.kiro/hooks/."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, _floor_monkeypatch):
        """Point Path.home() at tmp_path so hooks_dir validation accepts tmp dirs.

        The production rule rejects kiro_hooks_dir values that do not resolve
        under Path.home(), to prevent an LLM-writable config from pointing
        autoimport at /tmp or similar.  Tests legitimately use tmp_path for
        isolation, so we fake HOME = tmp_path for every test in this class.
        """
        _floor_monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _make_script(
        self,
        hooks_dir: Path,
        name: str,
        body: str = "exit 0\n",
        executable: bool = True,
    ) -> Path:
        """Write a hook script with the given body; mark it executable by default."""
        hooks_dir.mkdir(parents=True, exist_ok=True)
        p = hooks_dir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755 if executable else 0o644)
        return p

    def test_kiro_hooks_autoimport_loads_executable_scripts(self, tmp_path: Path):
        """Two executable scripts both land under preToolUse with their absolute paths."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        s1 = self._make_script(hooks_dir, "a.sh")
        s2 = self._make_script(hooks_dir, "b.sh")

        result = _autoimport_kiro_hooks(hooks_dir)

        commands = sorted(e["command"] for e in result["preToolUse"])
        assert commands == sorted([str(s1), str(s2)])
        assert list(result.keys()) == ["preToolUse"]

    def test_kiro_hooks_autoimport_parses_event_header(self, tmp_path: Path):
        """A ``# event: PostToolUse`` header routes the script to postToolUse."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "audit.sh", body="# event: PostToolUse\nexit 0\n")

        result = _autoimport_kiro_hooks(hooks_dir)

        assert "postToolUse" in result
        assert "preToolUse" not in result
        assert len(result["postToolUse"]) == 1

    def test_kiro_hooks_autoimport_parses_matcher_header(self, tmp_path: Path):
        """A ``# matcher:`` header is preserved on the resulting entry."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "guard.sh", body="# matcher: shell\nexit 0\n")

        result = _autoimport_kiro_hooks(hooks_dir)

        assert result["preToolUse"][0]["matcher"] == "shell"

    def test_kiro_hooks_autoimport_skips_non_executable(self, tmp_path: Path, caplog):
        """Non-executable ``.sh`` files are skipped; executable siblings still load."""
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "ok.sh")
        self._make_script(hooks_dir, "disabled.sh", executable=False)

        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        if sys.platform == "win32":
            # No execute bit on Windows: a known script extension counts as
            # runnable via platform_compat.is_executable_file, so the
            # chmod -x sibling loads too instead of being skipped.
            assert len(result["preToolUse"]) == 2
            return
        assert len(result["preToolUse"]) == 1
        assert Path(result["preToolUse"][0]["command"]).name == "ok.sh"
        assert any("not executable" in rec.message for rec in caplog.records)

    @requires_symlinks
    def test_kiro_hooks_autoimport_skips_sensitive_path(self, tmp_path: Path, monkeypatch):
        """Scripts resolving into a sensitive path (~/.ssh) are rejected."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        # Pretend HOME is tmp_path so ~/.ssh is fabricated and isolated.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        sensitive = tmp_path / ".ssh" / "evil.sh"
        sensitive.parent.mkdir(parents=True, exist_ok=True)
        sensitive.write_text("#!/bin/sh\nexit 0\n")
        sensitive.chmod(0o755)

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        symlink = hooks_dir / "evil.sh"
        symlink.symlink_to(sensitive)

        result = _autoimport_kiro_hooks(hooks_dir)
        assert result == {}

    def test_kiro_hooks_autoimport_dedupes_with_explicit_config(self, tmp_path: Path):
        """A script listed both explicitly and in the autoimport dir yields one entry."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        script = self._make_script(hooks_dir, "shared.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": {"preToolUse": [{"command": str(script)}]},
                "kiro_hooks_dir": str(hooks_dir),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        entries = config["hooks"]["preToolUse"]
        assert len(entries) == 1
        assert entries[0]["command"] == str(script)

    def test_kiro_hooks_autoimport_respects_disable_flag(self, tmp_path: Path):
        """``agent.kiro_hooks_autoimport=False`` skips the scan even when scripts exist."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "a.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks_autoimport": False,
                "kiro_hooks_dir": str(hooks_dir),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}

    def test_kiro_hooks_autoimport_honors_custom_dir(self, tmp_path: Path):
        """``agent.kiro_hooks_dir`` overrides the default ~/.kiro/hooks path."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        custom = tmp_path / "custom-hooks"
        self._make_script(custom, "only.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(custom)}}

        _apply_user_kiro_hooks(config, mc_cfg)

        assert len(config["hooks"]["preToolUse"]) == 1
        assert Path(config["hooks"]["preToolUse"][0]["command"]).name == "only.sh"

    def test_kiro_hooks_autoimport_respects_total_limit(self, tmp_path: Path, caplog):
        """More scripts than ``_MAX_TOTAL_USER_HOOKS`` get capped; one WARNING logged."""
        import logging

        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Spread across events so the per-event cap (10) does not fire first.
        # Filename suffixes are used so the scripts route to different events
        # without needing to write headers.
        hooks_dir = tmp_path / "hooks"
        suffixes = ["-pre.sh", "-post.sh", "-prompt.sh", "-spawn.sh", "-stop.sh"]
        total = _MAX_TOTAL_USER_HOOKS + 5
        for i in range(total):
            self._make_script(hooks_dir, f"h{i:02d}{suffixes[i % len(suffixes)]}")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(hooks_dir)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS
        cap_warnings = [r for r in caplog.records if "global limit" in r.message.lower()]
        # `_merge_kiro_hooks` re-checks the cap per event, so the WARNING count
        # depends on how scripts spread across events; only its presence is pinned.
        assert cap_warnings, (
            "expected at least one global-limit WARNING when scripts exceed "
            "_MAX_TOTAL_USER_HOOKS; the merged_total cap is the real invariant"
        )

    def test_kiro_hooks_total_limit_shared_across_explicit_and_autoimport(
        self, tmp_path: Path, caplog
    ):
        """``_MAX_TOTAL_USER_HOOKS`` caps explicit and autoimport hooks combined.

        Each source alone fits the cap, so two separate ``_merge_kiro_hooks`` passes (each
        resetting ``total_added``) would yield twice the cap; one shared pass yields the cap.
        """
        import logging

        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Build explicit kiro_hooks with _MAX_TOTAL_USER_HOOKS entries,
        # spread across events so the per-event cap (10) is not what
        # limits the explicit source.  These scripts are real files on
        # disk so ``_validate_hook_command`` accepts them.
        explicit_dir = tmp_path / "explicit-scripts"
        explicit_dir.mkdir(parents=True, exist_ok=True)
        explicit_events = ["preToolUse", "postToolUse", "userPromptSubmit"]
        explicit_hooks: dict[str, list[dict[str, str]]] = {ev: [] for ev in explicit_events}
        for i in range(_MAX_TOTAL_USER_HOOKS):
            script = explicit_dir / f"e{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks[explicit_events[i % len(explicit_events)]].append(
                {"command": str(script)}
            )

        # Autoimport dir: _MAX_TOTAL_USER_HOOKS more scripts, also spread
        # across events via filename suffix so per-event cap doesn't fire
        # within the autoimport source alone.
        autoimport_dir = tmp_path / "hooks"
        autoimport_suffixes = [
            "-pre.sh",
            "-post.sh",
            "-prompt.sh",
            "-spawn.sh",
            "-stop.sh",
        ]
        for i in range(_MAX_TOTAL_USER_HOOKS):
            self._make_script(
                autoimport_dir,
                f"a{i:02d}{autoimport_suffixes[i % len(autoimport_suffixes)]}",
            )

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_dir": str(autoimport_dir),
                # Default is True, but set explicitly so the test does
                # not silently become a single-source test if the
                # default ever flips.
                "kiro_hooks_autoimport": True,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS, (
            f"regression: combined explicit + autoimport hooks exceeded "
            f"_MAX_TOTAL_USER_HOOKS ({_MAX_TOTAL_USER_HOOKS}); got "
            f"{merged_total}.  Both sources must be merged in a single "
            f"_merge_kiro_hooks pass so the total cap is enforced across "
            f"the combined set, not per-source."
        )
        # The warning can fire once per event visited after the cap; only its
        # presence matters, the merged total above is the invariant.
        cap_warnings = [r for r in caplog.records if "global limit" in r.message.lower()]
        assert cap_warnings, (
            "expected at least one global-limit WARNING from the single "
            "merge pass when combined input exceeds _MAX_TOTAL_USER_HOOKS"
        )

    def test_kiro_hooks_per_event_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Hooks dropped by the per-event cap emit a SEL audit.

        Without it an auditor cannot tell "15 configured, 5 cap-dropped" from
        "10 configured, all loaded".
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _MAX_USER_HOOKS_PER_EVENT, _apply_user_kiro_hooks

        # Configure more scripts on a single event than the per-event cap.
        # All scripts route to preToolUse so the per-event cap fires before
        # the total cap (which is higher).
        explicit_dir = tmp_path / "scripts"
        explicit_dir.mkdir()
        over_cap = _MAX_USER_HOOKS_PER_EVENT + 3
        explicit_hooks: dict[str, list[dict[str, str]]] = {"preToolUse": []}
        for i in range(over_cap):
            script = explicit_dir / f"h{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks["preToolUse"].append({"command": str(script)})

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Exactly _MAX_USER_HOOKS_PER_EVENT scripts should have been
        # merged for preToolUse (the cap); the other 3 must be dropped.
        assert len(config["hooks"]["preToolUse"]) == _MAX_USER_HOOKS_PER_EVENT
        cap_sel = [c for c in sel_calls if "per-event limit exceeded" in c[2].lower()]
        assert cap_sel, (
            f"regression: per-event cap must emit _sel_hook_rejected; got "
            f"zero calls with reason 'per-event limit exceeded'.  All SEL "
            f"calls: {sel_calls!r}"
        )
        # `_merge_kiro_hooks` tags SEL with the event name, not "autoimport".
        assert cap_sel[0][0] == "preToolUse"

    def test_kiro_hooks_global_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Hooks dropped by the global cap ``_MAX_TOTAL_USER_HOOKS`` emit a SEL audit."""
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Spread over-cap scripts across events so the per-event cap (10)
        # does not fire first; only the global cap (20) gates.
        explicit_dir = tmp_path / "scripts"
        explicit_dir.mkdir()
        over_cap = _MAX_TOTAL_USER_HOOKS + 3
        events = ["preToolUse", "postToolUse", "userPromptSubmit"]
        explicit_hooks: dict[str, list[dict[str, str]]] = {e: [] for e in events}
        for i in range(over_cap):
            script = explicit_dir / f"g{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks[events[i % len(events)]].append({"command": str(script)})

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS
        # Global-cap SEL audit must fire at least once.  The reason
        # string is the contract; the exact count depends on how many
        # events the loop visits after the cap is reached.
        cap_sel = [c for c in sel_calls if "global limit exceeded" in c[2].lower()]
        assert cap_sel, (
            f"regression: global cap must emit _sel_hook_rejected; got "
            f"zero calls with reason 'global limit exceeded'.  All SEL "
            f"calls: {sel_calls!r}"
        )

    def test_kiro_hooks_unknown_event_type_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """An event name outside ``_VALID_HOOK_EVENTS`` drops its bucket with a SEL audit.

        Dropping the bucket is a permission decision, so it must be auditable.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # "bogusEvent" is not in _VALID_HOOK_EVENTS; bucket must be
        # dropped with SEL audit.
        mc_cfg = {
            "agent": {
                "kiro_hooks": {
                    "bogusEvent": [{"command": "/bin/true"}],
                },
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        unknown_sel = [c for c in sel_calls if "unknown event" in c[2].lower()]
        assert unknown_sel, (
            f"regression: unknown event type must emit _sel_hook_rejected; " f"got {sel_calls!r}"
        )
        event_tag, _command, reason = unknown_sel[0]
        assert event_tag == "bogusEvent"
        assert "unknown event type" in reason.lower()

    def test_kiro_hooks_non_list_entries_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """A non-list bucket for a valid event is dropped with a SEL audit."""
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # ``preToolUse`` is a valid event, but a string is not a list;
        # bucket must be dropped with SEL audit.
        mc_cfg = {
            "agent": {
                "kiro_hooks": {
                    "preToolUse": "not-a-list-but-a-string",
                },
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        non_list_sel = [c for c in sel_calls if "not a list" in c[2].lower()]
        assert non_list_sel, (
            f"regression: non-list entries must emit _sel_hook_rejected; " f"got {sel_calls!r}"
        )
        event_tag, _command, reason = non_list_sel[0]
        assert event_tag == "preToolUse"
        assert "not a list" in reason.lower()

    def test_kiro_hooks_autoimport_missing_dir_is_noop(self, tmp_path: Path, caplog):
        """Missing directory returns empty dict with only a DEBUG log (no WARNINGs)."""
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        missing = tmp_path / "does-not-exist"

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(missing)

        assert result == {}
        # Scope to the kiro_crew.agent logger: under `pytest -n auto`, a leaked
        # asyncio task exception from an unrelated test (e.g. "Task exception was
        # never retrieved" on the root `asyncio` logger) can land in caplog during
        # this window and falsely trip a bare records scan. We only care that THIS
        # code path emits no WARNING+.
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "kiro_crew.agent"
        ]
        assert warnings == []

    def test_kiro_hooks_autoimport_invalid_matcher_skips_script(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """An invalid matcher header skips the whole script.

        Demoting a tool-scoped hook to an unscoped one would widen it to every tool call.
        The SEL audit uses the literal ``"autoimport"`` tag, like every other rejection
        branch in ``_autoimport_kiro_hooks``, so filters on that tag see it.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        # Matcher with a space is rejected by _SAFE_MATCHER_RE.
        entry = self._make_script(
            hooks_dir, "bad.sh", body="# matcher: tool name with spaces\nexit 0\n"
        )

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert any(
            "matcher" in rec.message.lower() and "invalid" in rec.message.lower()
            for rec in caplog.records
        )
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected for invalid matcher; "
            f"got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport", (
            f"regression: invalid-matcher SEL audit used tag {event_tag!r}; "
            f"must be literal 'autoimport' for audit-trail consistency with "
            f"every other rejection branch in _autoimport_kiro_hooks."
        )
        assert command == str(entry)
        assert "invalid matcher" in reason.lower()

    def test_kiro_hooks_autoimport_unknown_event_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A script whose ``# event:`` header is unknown is rejected with a SEL audit.

        Every permission decision must emit SEL (AUTOSDE.yaml security-controls).
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        # ``NoSuchEvent`` is not in _HOOK_EVENT_CANONICAL, so _infer_hook_event
        # returns None and this rejection branch fires.  The filename has no
        # known suffix so fallback inference does not rescue it either.
        script = self._make_script(hooks_dir, "bogus.sh", body="# event: NoSuchEvent\nexit 0\n")

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when a "
            f"script's '# event:' header is unknown; got {len(sel_calls)}: "
            f"{sel_calls!r}.  Every rejection branch in _autoimport_kiro_hooks "
            f"must emit a SEL audit event per AUTOSDE.yaml security-controls."
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(script)
        assert "unknown event" in reason.lower()

    def test_kiro_hooks_autoimport_cannot_resolve_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A script whose ``entry.resolve()`` raises ``OSError`` is rejected with a SEL audit."""
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "broken.sh")

        # Patch Path.resolve to raise OSError ONLY for the entry path,
        # letting hooks_dir.resolve() (the first resolve() call in
        # _autoimport_kiro_hooks) succeed.  Otherwise the function
        # returns early before the loop even starts.
        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            if self == entry:
                raise OSError("simulated resolve failure")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"entry.resolve() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot resolve" in reason.lower()

    def test_kiro_hooks_autoimport_cannot_stat_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A script whose ``resolved_entry.stat()`` raises is rejected with a SEL audit."""
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "broken.sh")

        # Arm the stat failure only AFTER ``entry.is_file()`` has been
        # observed on this path.  The production loop calls ``is_file()``
        # first (internally a stat), then later ``resolved_entry.stat()``
        # explicitly.  We want only the explicit call to fail.  A stat-count
        # threshold would break across Python versions, since the number of
        # stats inside ``is_file()`` differs between 3.10 and 3.13.
        real_stat = Path.stat
        real_is_file = Path.is_file
        armed = False

        def _arming_is_file(self, *args, **kwargs):
            # Arm by Path identity: matching ``self.name`` would also fire on
            # any other path named "broken.sh".
            nonlocal armed
            rv = real_is_file(self, *args, **kwargs)
            if self == entry:
                armed = True
            return rv

        def _raising_stat(self, *args, **kwargs):
            if self == entry and armed:
                raise OSError("simulated stat failure")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", _arming_is_file)
        monkeypatch.setattr(Path, "stat", _raising_stat)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"resolved_entry.stat() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot stat" in reason.lower()

    def test_kiro_hooks_autoimport_non_executable_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Skipping a non-executable ``.sh`` file emits a SEL audit.

        Not loading a discovered script is a permission decision, so it must be auditable
        like every other rejection branch in ``_autoimport_kiro_hooks``.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "disabled.sh", executable=False)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        if sys.platform == "win32":
            # Same platform rule as above: the script loads, so there is no
            # rejection to audit.
            assert len(result.get("preToolUse", [])) == 1
            assert sel_calls == []
            return
        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"a non-executable .sh is skipped; got {len(sel_calls)}: "
            f"{sel_calls!r}.  Every rejection branch in "
            f"_autoimport_kiro_hooks must emit a SEL audit event per "
            f"AUTOSDE.yaml security-controls."
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "not executable" in reason.lower()

    def test_kiro_hooks_autoimport_rejects_dir_equal_to_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """A ``kiro_hooks_dir`` that resolves to HOME itself is rejected.

        Containment is strict (under HOME, not equal to it): accepting HOME would
        auto-register every executable ``*.sh`` anywhere under ``$HOME``.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # ``_isolate_home`` fixture already sets Path.home() -> tmp_path.
        # Re-route the default fallback so failure does not touch the real
        # ~/.kiro/hooks.  Place an executable script directly at HOME root
        # to prove that home-root scanning would pick it up.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / ".kiro" / "hooks",
        )
        evil = tmp_path / "evil.sh"
        evil.write_text("#!/bin/sh\nexit 0\n")
        evil.chmod(0o755)

        config: dict = {"hooks": {}}
        # ``kiro_hooks_dir: "~"`` expands to HOME, which equals tmp_path
        # after the _isolate_home fixture runs.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(tmp_path)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Critical invariant: nothing from HOME-root got auto-registered.
        # The "evil" script must NOT appear in config["hooks"].
        assert config["hooks"] == {}, (
            f"regression: kiro_hooks_dir resolving to HOME itself was accepted, "
            f"causing home-root scan; expected empty hooks dict, got "
            f"{config['hooks']!r}.  The containment check must reject "
            f"resolved == home."
        )
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        ), "expected 'kiro_hooks_dir ... rejected' WARNING from containment check"

    def test_kiro_hooks_autoimport_rejects_dir_outside_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """kiro_hooks_dir resolving outside HOME is rejected with a fallback warning.

        config.json is LLM-writable.  A malicious override
        pointing autoimport at /tmp or /var could auto-register any executable
        script an attacker lands there.  The code must fall back to the
        default ~/.kiro/hooks and log a WARNING + SEL audit.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # Stage a fake HOME so our default hooks dir doesn't exist and so
        # tmp_path's /private/var/folders path is *outside* HOME.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        # Also re-route the default hooks dir into the fake HOME so fallback
        # does not hit the caller's real ~/.kiro/hooks directory.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            fake_home / ".kiro" / "hooks",
        )

        # This dir is genuinely outside fake_home, since tmp_path itself is
        # the parent directory of fake_home.
        outside = tmp_path / "outside-hooks"
        outside.mkdir()
        self._make_script(outside, "evil.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(outside)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Fallback is fake_home/.kiro/hooks (doesn't exist), so nothing gets
        # merged.  Critically: the "evil" script is not merged.
        assert config["hooks"] == {}
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        )

    @requires_symlinks
    def test_kiro_hooks_autoimport_rejects_symlink_escaping_dir(self, tmp_path: Path, caplog):
        """A symlink inside hooks_dir pointing at an outside script is rejected.

        ``entry.is_file()`` follows symlinks and is_sensitive_path covers few $HOME
        subdirs, so only the resolved-path containment check catches this.
        """
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        # Outside target, inside HOME so the is_sensitive_path check alone
        # wouldn't reject it - only the containment check does.
        outside_target = tmp_path / "elsewhere" / "attacker.sh"
        outside_target.parent.mkdir(parents=True, exist_ok=True)
        outside_target.write_text("#!/bin/sh\nexit 0\n")
        outside_target.chmod(0o755)
        (hooks_dir / "guard.sh").symlink_to(outside_target)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert any("resolves outside" in rec.message for rec in caplog.records)

    def test_kiro_hooks_autoimport_validates_before_parsing_headers(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """``_validate_hook_command`` runs before header parsing, so rejected paths are never read.

        Validation is forced to reject so the containment check cannot be what stops
        parsing; the rejection must also be SEL-audited.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        # One legitimate script inside hooks_dir (passes containment).
        hooks_dir = tmp_path / "hooks"
        script = self._make_script(hooks_dir, "ok.sh")

        # Force _validate_hook_command to reject everything.  This isolates
        # the reorder: if header parsing ran before validation, we'd still
        # see a call; with the reorder, validation rejects first.
        monkeypatch.setattr(_agent_mod, "_validate_hook_command", lambda *_a, **_kw: None)

        header_calls: list[str] = []

        def _record_header_call(path: Path) -> tuple:
            header_calls.append(str(path))
            return None, None

        monkeypatch.setattr(_agent_mod, "_parse_hook_script_headers", _record_header_call)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert header_calls == [], (
            "regression: _parse_hook_script_headers was called before "
            "_validate_hook_command rejected the script. Validation must "
            "run first so no file reads happen on rejected paths."
        )
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected call for the rejected "
            f"script; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(script)
        assert "failed validation" in reason

    @requires_symlinks
    def test_kiro_hooks_dir_stored_as_resolved_path(self, tmp_path: Path, monkeypatch):
        """``_autoimport_kiro_hooks`` receives the *resolved* hooks dir, not the symlink.

        Forwarding the symlinked path leaves a TOCTOU window where a swapped component
        lets autoimport scan outside HOME. The race cannot be reproduced, and per-entry
        canonicalization hides it in the command paths, so the call argument is pinned.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Real hooks directory plus a user-facing symlink that points at it.
        real_hooks = tmp_path / "real" / "hooks"
        real_hooks.mkdir(parents=True)
        link_hooks = tmp_path / "link-hooks"
        link_hooks.symlink_to(real_hooks)

        captured: list[Path] = []

        # Spy on the `hooks_dir` value _apply_user_kiro_hooks forwards.
        def _spy(hooks_dir: Path) -> dict:
            captured.append(hooks_dir)
            return {}

        monkeypatch.setattr(_agent_mod, "_autoimport_kiro_hooks", _spy)

        config: dict = {"hooks": {}}
        # Explicit ``kiro_hooks_autoimport: True`` so the test does not
        # silently become a no-op if the default ever flips.
        mc_cfg = {
            "agent": {
                "kiro_hooks_autoimport": True,
                "kiro_hooks_dir": str(link_hooks),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        assert len(captured) == 1, "autoimport should have been invoked exactly once"
        forwarded = captured[0]
        # The forwarded path must equal the real (resolved) directory, NOT
        # the symlink requested by the user.  Comparing by resolve() on
        # both sides would mask the bug (since resolve(link) == real);
        # comparing the raw Path confirms the resolved form was stored.
        assert forwarded == real_hooks, (
            f"regression: _autoimport_kiro_hooks received {forwarded!r}; "
            f"expected the resolved path {real_hooks!r}. _apply_user_kiro_hooks "
            "must store the resolved path so the downstream resolve() is a "
            "no-op even under adversarial symlink swaps."
        )
        # Precondition: it really is NOT the symlink form (would be the bug).
        assert forwarded != link_hooks

    def test_kiro_hooks_dir_null_byte_does_not_crash(self, tmp_path: Path, monkeypatch):
        """A ``kiro_hooks_dir`` with a null byte falls back without crashing.

        ``Path.resolve`` raises ``ValueError`` on it, which would crash ``install_agent()``
        from LLM-writable config.
        """
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route the default so fallback doesn't touch caller's HOME.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": "\x00"}}

        # Must not raise ``ValueError: embedded null byte``.
        _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}

    def test_kiro_hooks_autoimport_cannot_read_dir_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """An unreadable hooks dir (``iterdir()`` raises) emits a SEL audit.

        The merge summary shows ``requested_autoimport=0`` for both an unreadable dir
        and an empty one, so only SEL tells them apart.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        # Force iterdir() to raise OSError (simulating permission
        # denial).  Narrow the patch to Path.iterdir only.
        def _raising_iterdir(self):
            raise OSError("simulated permission denied")

        monkeypatch.setattr(Path, "iterdir", _raising_iterdir)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"iterdir() raises OSError; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(hooks_dir)
        assert "cannot read" in reason.lower()

    def test_kiro_hooks_dir_non_string_ignored_without_sel(self, tmp_path: Path, monkeypatch):
        """A non-string ``kiro_hooks_dir`` falls back to the default without a SEL record.

        Nothing was rejected, so a SEL rejection would be a false positive.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route default so fallback is empty (hooks dir doesn't exist).
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        for bogus in (None, [], 42, {"foo": "bar"}):
            config: dict = {"hooks": {}}
            mc_cfg = {"agent": {"kiro_hooks_dir": bogus}}
            # Must not raise.
            _apply_user_kiro_hooks(config, mc_cfg)
            assert config["hooks"] == {}, (
                f"non-string kiro_hooks_dir={bogus!r} produced hooks: " f"{config['hooks']!r}"
            )

        assert sel_calls == [], (
            f"non-string kiro_hooks_dir should NOT emit SEL "
            f"(nothing was rejected); got: {sel_calls!r}"
        )

    @requires_symlinks
    def test_kiro_hooks_autoimport_rejects_dir_equal_to_symlinked_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """HOME containment holds when ``$HOME`` is a symlink.

        ``Path.home()`` returns the symlink and the config names its target; both sides
        must be resolved for the equal-to-HOME rejection to fire.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # Construct a real directory and a symlink to it.
        real_home = tmp_path / "real_home"
        real_home.mkdir()
        symlink_home = tmp_path / "link_home"
        symlink_home.symlink_to(real_home)

        # Path.home() returns the symlink; .resolve() inside the code
        # should canonicalize it to real_home.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: symlink_home))
        # Re-route default so fallback doesn't touch caller's real HOME.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            symlink_home / ".kiro" / "hooks",
        )

        # Plant an executable at the canonical HOME root to prove it
        # would be scanned under a buggy containment check.
        evil = real_home / "evil.sh"
        evil.write_text("#!/bin/sh\nexit 0\n")
        evil.chmod(0o755)

        config: dict = {"hooks": {}}
        # User points kiro_hooks_dir directly at the canonical HOME
        # (bypassing the symlink) -- should still be rejected because
        # resolved == canonical HOME.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(real_home)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}, (
            f"regression: kiro_hooks_dir resolving to canonical HOME "
            f"(via symlink'd Path.home()) was accepted; expected "
            f"empty hooks, got {config['hooks']!r}.  The containment "
            f"check must .resolve() both sides."
        )
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        )

    def test_kiro_hooks_autoimport_hooks_dir_resolve_oserror_emits_sel(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """An ``OSError`` from ``hooks_dir.resolve()`` emits a SEL audit."""
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            # Fire only on the hooks_dir, not on entries or Path.home().
            if self == hooks_dir:
                raise OSError("simulated resolve failure on hooks_dir")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"hooks_dir.resolve() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(hooks_dir)
        assert "cannot resolve hooks_dir" in reason.lower()

    def test_kiro_hooks_autoimport_handles_valueerror_on_entry_resolve(
        self, tmp_path: Path, monkeypatch
    ):
        """A ``ValueError`` from ``entry.resolve()`` is rejected with a SEL audit, not raised.

        A null byte in a filename raises ``ValueError``, not ``OSError``.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "bad.sh")

        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            if self == entry:
                raise ValueError("embedded null byte")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        # Must not raise.
        result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: ValueError on entry.resolve() must trigger the "
            f"same SEL-audited rejection branch as OSError; got "
            f"{len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot resolve" in reason.lower()

    def test_kiro_hooks_dir_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch, caplog):
        """An ``OSError`` from ``Path.resolve()`` (ELOOP, EACCES) falls back cleanly.

        The rejection branch logs a warning and emits a SEL audit; hooks stay empty.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route default so fallback is inert.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        # Force Path.resolve to raise OSError.  Narrow the patch so only
        # resolve() on the "requested" path fails; Path.home() still
        # works so home=None comes from resolved=None cascade.
        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            # Raise only on the user-supplied path (a custom fake-home
            # target we pass below).  Leave other resolve() calls alone.
            if self.name == "too-long":
                raise OSError("ENAMETOOLONG simulated")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # A path whose name triggers our resolve-fail shim.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(tmp_path / "too-long")}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            # Must not raise.
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        # Exactly one SEL call for the rejection.
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected on OSError resolve; "
            f"got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, _command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        # Reason currently says "outside HOME or sensitive" which is
        # broadly accurate (resolved=None does land in that branch),
        # but a future refinement may split OSError into its own
        # reason string -- either shape is acceptable here.
        assert (
            "kiro_hooks_dir" in reason.lower()
            or "hooks_dir" in reason.lower()
            or "home" in reason.lower()
            or "sensitive" in reason.lower()
        )
        # A warning mentioning "rejected" must be logged.
        assert any("rejected" in rec.message.lower() for rec in caplog.records)

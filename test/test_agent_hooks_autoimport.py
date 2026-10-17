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
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the HOME fake every test of this class needs, moved verbatim; none of them calls monkeypatch.undo()
    def _isolate_home(self, tmp_path: Path, monkeypatch):
        """Point Path.home() at tmp_path so hooks_dir validation accepts tmp dirs.

        The production rule rejects kiro_hooks_dir values that do not resolve
        under Path.home(), to prevent an LLM-writable config from pointing
        autoimport at /tmp or similar.  Tests legitimately use tmp_path for
        isolation, so we fake HOME = tmp_path for every test in this class.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

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
        # review-bot rev 8 fix: `_merge_kiro_hooks` re-checks the cap at the
        # start of each event's inner loop, so the number of WARNINGs
        # depends on how scripts are distributed across events -- which
        # shifts with dict ordering changes or minor test edits.  The
        # invariant we actually care about is `merged_total == cap`
        # (asserted above); at least one cap WARNING must fire as
        # evidence the branch was exercised, but the exact count is not
        # the contract.  This matches the sibling test
        # `test_kiro_hooks_total_limit_shared_across_explicit_and_autoimport`.
        assert cap_warnings, (
            "expected at least one global-limit WARNING when scripts exceed "
            "_MAX_TOTAL_USER_HOOKS; the merged_total cap is the real invariant"
        )

    def test_kiro_hooks_total_limit_shared_across_explicit_and_autoimport(
        self, tmp_path: Path, caplog
    ):
        """Regression: ``_MAX_TOTAL_USER_HOOKS`` caps combined explicit + autoimport.

        review-bot finding (agent.py:778, importance=0): the
        original code in ``_apply_user_kiro_hooks`` called
        ``_merge_kiro_hooks`` twice — once for explicit entries, once for
        auto-discovered scripts.  Because ``_merge_kiro_hooks`` initializes
        ``total_added = 0`` on each call, the per-call cap of
        ``_MAX_TOTAL_USER_HOOKS`` (20) applied to each source independently,
        yielding up to 40 total user hooks instead of the intended 20.  The
        fix merges both sources in a single pass so the total cap is
        enforced across the combined set.

        This test stages enough scripts in BOTH sources that each source
        alone would fit under the cap (each has ``_MAX_TOTAL_USER_HOOKS``
        entries, which equals the cap), but together they exceed it.  The
        merged result must land at exactly ``_MAX_TOTAL_USER_HOOKS``, not
        ``2 * _MAX_TOTAL_USER_HOOKS``.  Under the old code this test
        observed ``merged_total == 2 * _MAX_TOTAL_USER_HOOKS``; under the
        new single-pass code it observes exactly the cap.
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
        # The cap warning should fire at least once because the single
        # merge pass trips the global-limit branch when the combined input
        # exceeds the cap.  It can fire multiple times because
        # ``_merge_kiro_hooks`` iterates events and re-checks the cap per
        # event after it is reached; the count is not the invariant here,
        # the total-merged count above is.
        cap_warnings = [r for r in caplog.records if "global limit" in r.message.lower()]
        assert cap_warnings, (
            "expected at least one global-limit WARNING from the single "
            "merge pass when combined input exceeds _MAX_TOTAL_USER_HOOKS"
        )

    def test_kiro_hooks_per_event_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: per-event cap break must emit SEL audit.

        review-bot rev 8 finding (agent.py:682, importance=1,
        security-controls): when ``_merge_kiro_hooks`` hits the per-event
        cap ``_MAX_USER_HOOKS_PER_EVENT``, remaining entries for that
        event were silently dropped with only a ``logger.warning`` and
        no ``_sel_hook_rejected`` call.  Every other rejection branch
        in ``_merge_kiro_hooks`` (missing command, failed validation,
        non-string matcher, invalid matcher) correctly emits SEL audit.
        Closing this audit-trail gap so auditors can distinguish "user
        configured 15 preToolUse hooks and 5 were cap-dropped" from
        "user configured 10 and all loaded".
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
        # The per-event cap path must have emitted at least one SEL
        # audit tagged with the event (preToolUse) and the
        # "per-event limit exceeded" reason.  Under the pre-fix code
        # this assertion failed with zero SEL calls tagged that reason.
        cap_sel = [c for c in sel_calls if "per-event limit exceeded" in c[2].lower()]
        assert cap_sel, (
            f"regression: per-event cap must emit _sel_hook_rejected; got "
            f"zero calls with reason 'per-event limit exceeded'.  All SEL "
            f"calls: {sel_calls!r}"
        )
        # The tag should be the event name, not the literal "autoimport",
        # since this is inside _merge_kiro_hooks which uses the inferred
        # event as the SEL tag (consistent with other branches in this
        # function).
        assert cap_sel[0][0] == "preToolUse"

    def test_kiro_hooks_global_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: global cap break must emit SEL audit.

        review-bot rev 8 finding (agent.py:688, importance=1,
        security-controls): sibling to the per-event cap gap.  When the
        global cap ``_MAX_TOTAL_USER_HOOKS`` is hit across all events,
        remaining hooks are silently dropped.  An auditor cannot
        distinguish "25 configured, 5 cap-dropped" from "20 configured,
        all loaded" without a SEL signal.
        """
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
        """Regression: unknown event type in user_hooks must emit SEL audit.

        review-bot rev 9 follow-up: when ``_merge_kiro_hooks``
        encounters an event name not in ``_VALID_HOOK_EVENTS`` (e.g.
        typo or future-event-name from a newer kiro-cli), the entire
        event-bucket is dropped.  This is a permission decision per
        AUTOSDE.yaml security-controls and must emit SEL audit so an
        auditor can distinguish "user configured 0 hooks for event X"
        from "user configured 5 and the whole bucket was dropped for
        invalid event name".
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
        """Regression: non-list entries for a valid event must emit SEL audit.

        review-bot rev 9 finding (agent.py:669, importance=1,
        security-controls): when ``_merge_kiro_hooks`` sees
        ``user_hooks["preToolUse"]`` is e.g. a string or dict (not a
        list), the whole bucket is dropped silently.  Auditors need a
        SEL signal to distinguish "malformed config dropped all hooks
        for this event" from "no hooks configured".
        """
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
        """An invalid matcher header must skip the script entirely.

        Regression guard: silently demoting a tool-scoped hook to an unscoped
        hook (firing on every tool call) would be a privilege expansion.  The
        whole script must be rejected so the user notices and fixes the
        matcher instead of getting a silently-broader hook.

        Also asserts (rev 7 review-bot fix): the SEL audit event uses the
        literal ``"autoimport"`` tag for consistency with every other
        rejection branch in ``_autoimport_kiro_hooks``.  The pre-fix code
        passed the variable ``event`` (e.g. ``"preToolUse"``), which broke
        audit-trail consistency -- auditors filtering on
        ``event="autoimport"`` would miss invalid-matcher rejections.
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
        # Rev 7 review-bot fix: SEL tag must be the literal "autoimport",
        # not the variable ``event`` (e.g. "preToolUse").  Under the
        # pre-fix code this assertion failed with event_tag == "preToolUse".
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
        """Regression: unknown ``# event:`` rejection must emit a SEL audit event.

        review-bot finding (agent.py:586, importance=1,
        security-controls): when ``_infer_hook_event`` returns ``None``
        (the header declares an event name outside ``_HOOK_EVENT_CANONICAL``),
        the script is rejected.  Before this fix, the rejection only emitted
        a ``logger.warning`` — it did NOT call ``_sel_hook_rejected``.  Every
        other rejection branch in ``_autoimport_kiro_hooks`` (symlink-escape,
        failed-validation, invalid-matcher) correctly emits a SEL audit event
        per AUTOSDE.yaml's security-controls rule: "All tool invocations and
        permission decisions must emit SEL audit events via sel.py."

        This test stages a script whose ``# event:`` header is a bogus event
        name (not in ``_HOOK_EVENT_CANONICAL``), spies on
        ``_sel_hook_rejected``, and asserts the audit call was made with the
        ``"autoimport"`` source tag, the script path, and an
        ``"unknown event header"`` reason.  Under the pre-fix code, the spy
        observed zero calls and this test failed with the designed error
        message; under the fix, it observes exactly one.
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
        # The SEL audit must fire exactly once for this one rejected script,
        # tagged with the ``"autoimport"`` log label and the script path.
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
        """Regression: ``entry.resolve()`` failure rejection must emit SEL audit.

        review-bot finding (agent.py:556, rev 4, importance=1,
        security-controls): the ``cannot resolve entry`` rejection branch
        (when ``entry.resolve()`` raises ``OSError``) was missing the
        ``_sel_hook_rejected`` call.  Every other rejection branch in
        ``_autoimport_kiro_hooks`` emits a SEL audit event per
        AUTOSDE.yaml's security-controls rule.  Without this call, an
        auditor reconstructing agent-install activity from SEL would not
        see scripts dropped due to resolve() failures.

        This test forces ``Path.resolve`` to raise ``OSError`` for the
        entry and asserts a SEL audit is recorded with the
        ``"autoimport"`` source tag and a ``"cannot resolve entry"``
        reason.
        """
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
        """Regression: ``resolved_entry.stat()`` failure must emit SEL audit.

        review-bot finding (agent.py:556, rev 4, importance=1,
        security-controls): the ``cannot stat entry`` rejection branch
        (when ``resolved_entry.stat()`` raises ``OSError``) was missing
        the ``_sel_hook_rejected`` call.  Same audit-completeness class
        of bug as the ``cannot resolve`` gap.

        This test forces ``Path.stat`` to raise ``OSError`` for the
        resolved entry and asserts a SEL audit is recorded with the
        ``"autoimport"`` source tag and a ``"cannot stat entry"`` reason.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "broken.sh")

        # Arm the stat failure only AFTER ``entry.is_file()`` has been
        # observed on this path.  The production loop calls ``is_file()``
        # first (internally a stat), then later ``resolved_entry.stat()``
        # explicitly.  We want only the explicit call to fail.
        #
        # Previous version used ``call_count >= 3`` which worked on
        # CPython 3.12 but is fragile: pathlib's internal stat-usage per
        # ``is_file()`` varies between 3.10, 3.11, 3.12, 3.13 (3.12
        # rewrote pathlib internals), so a hard-coded threshold is a
        # time bomb.  Gating on ``is_file`` being called instead is
        # stable across versions — no matter how many stats ``is_file``
        # makes internally, we only raise on stats that happen *after*
        # it completes, which is when ``resolved_entry.stat()`` runs.
        real_stat = Path.stat
        real_is_file = Path.is_file
        armed = False

        def _arming_is_file(self, *args, **kwargs):
            # Arm by Path identity (self == entry) rather than by
            # self.name, which would also fire on any path whose
            # basename happens to be "broken.sh" (e.g. a sibling
            # fixture in a future multi-script variant of this test).
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
        """Regression: non-executable ``.sh`` skip must emit SEL audit.

        review-bot rev 6 follow-up (agent.py:581,
        importance=1, security-controls): the non-executable skip
        branch was the last rejection path in ``_autoimport_kiro_hooks``
        that logged at INFO only and did NOT call
        ``_sel_hook_rejected``.  Every other rejection branch
        (symlink-escape, cannot-resolve, cannot-stat, failed-validation,
        unknown-event, invalid-matcher, cannot-read-dir) emits a SEL
        audit event per AUTOSDE.yaml's security-controls rule: "All
        tool invocations and permission decisions must emit SEL audit
        events via sel.py."  The non-executable skip is also a
        permission decision (a discovered ``.sh`` file will NOT be
        loaded as a hook), so an auditor reconstructing agent-install
        activity from SEL must see it.

        This test drops a non-executable ``.sh`` file in the hooks dir,
        spies on ``_sel_hook_rejected``, and asserts exactly one audit
        call with the ``"autoimport"`` tag and a ``"not executable"``
        reason.  Under the pre-fix code, the spy observed zero calls
        and this test failed with the designed error message.
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
        """Regression: ``kiro_hooks_dir: "~"`` (resolved == HOME) must be rejected.

        review-bot finding (agent.py:746, rev 4, importance=0,
        default): the original containment check allowed
        ``resolved == home`` to pass because the condition was
        ``(resolved != home and home not in resolved.parents)``.  When
        ``resolved == home``, the left side of the ``and`` was ``False``,
        so the whole clause was ``False`` and the path was *accepted*.

        Impact: an LLM-writable config setting ``kiro_hooks_dir: "~"``
        would cause ``_autoimport_kiro_hooks`` to scan the *entire* home
        directory for executable ``*.sh`` files, auto-registering any
        executable script anywhere under ``$HOME``.

        The fix is strict containment: require ``resolved`` to be *under*
        HOME, not equal to it.  ``Path.parents`` of e.g. ``/home/user``
        is ``(/, /home)`` and does not include ``/home/user`` itself, so
        ``home not in resolved.parents`` rejects ``resolved == home``.

        This test sets ``kiro_hooks_dir`` to a path that resolves to HOME
        (the fake home we monkeypatch to ``tmp_path``) and asserts no
        scripts get merged — the "evil" script at HOME root is not
        auto-registered — and that the rejection is logged + SEL-audited.
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
        # after the _isolate_home fixture runs.  Under the pre-fix code
        # this passed validation; under the fix it's rejected.
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

        Regression guard: config.json is LLM-writable.  A malicious override
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

        Regression guard: entry.is_file() follows symlinks, and
        is_sensitive_path only matches a small set of $HOME subdirs.  A
        symlink named ``guard.sh`` inside ~/.kiro/hooks/ whose target is
        ~/elsewhere/attacker.sh would otherwise pass every other check.
        The resolved-path containment check must catch it.
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
        """Regression: ``_validate_hook_command`` must run BEFORE header parsing.

        review-bot finding (agent.py:562, importance=1,
        security-controls): the original ordering parsed the script's
        ``# event:`` / ``# matcher:`` headers first and only then called
        ``_validate_hook_command``.  Defense-in-depth: even though the
        containment check above already rejects sensitive-path symlinks,
        the no-file-reads-on-rejected-paths invariant is worth keeping
        explicit.

        To prove the reorder (rather than the pre-existing containment
        check) is what stops header parsing, we force a rejection from
        ``_validate_hook_command`` specifically: monkeypatch it to return
        ``None`` for any path, then assert ``_parse_hook_script_headers``
        was never invoked.  Under the OLD code, headers were parsed first
        and this test would observe a call; under the NEW code, validation
        rejects before the parser runs.

        We also assert the rejection is SEL-audited via
        ``_sel_hook_rejected`` so a future refactor that silently drops
        the audit call is caught.
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

        # Spy on the SEL audit sink so we can assert a rejection was
        # recorded with the ``"autoimport"`` source tag.  Without this,
        # a future refactor that silently drops the audit call would
        # still let the monkeypatched validate-to-None path pass.
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
        # Exactly one SEL rejection recorded for the autoimport rejection
        # branch, tagged with the ``"autoimport"`` log label (a log-only
        # tag; see agent.py:562-570 note).
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
        """Regression: ``_autoimport_kiro_hooks`` receives the *resolved* hooks dir.

        review-bot finding (agent.py:744, TOCTOU): the original
        code did ``hooks_dir = requested`` after validating ``resolved``.
        If a path component of ``requested`` was a symlink, it could be
        swapped between the validate-in-HOME check in
        ``_apply_user_kiro_hooks`` and the resolve()-for-containment check
        inside ``_autoimport_kiro_hooks``, letting autoimport scan a
        directory outside HOME.  Storing the already-resolved path makes
        the downstream resolve() a no-op on an already-canonical path,
        eliminating the named symlink-swap window.

        This test does not (and cannot deterministically) reproduce the
        race itself; it verifies the observable contract that proves the
        mitigation is in place: the caller stores and forwards the
        resolved form of ``kiro_hooks_dir``.  The per-entry containment
        check already canonicalizes each entry, so asserting on the
        resulting command path cannot tell the fix apart from the bug -
        we instead intercept the call into ``_autoimport_kiro_hooks``
        and assert it was invoked with the *resolved* path, not the
        symlinked ``requested`` path.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Real hooks directory plus a user-facing symlink that points at it.
        real_hooks = tmp_path / "real" / "hooks"
        real_hooks.mkdir(parents=True)
        link_hooks = tmp_path / "link-hooks"
        link_hooks.symlink_to(real_hooks)

        captured: list[Path] = []

        # Spy on _autoimport_kiro_hooks to capture what `hooks_dir` value
        # _apply_user_kiro_hooks stores and forwards.  Under the old
        # `hooks_dir = requested` code, we would see `link_hooks`; under
        # the new `hooks_dir = resolved` code, we see `real_hooks`.
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
        """Regression: ``kiro_hooks_dir`` with a null byte must not crash.

        deep-review adversarial finding: ``Path("\\x00")``
        raises ``ValueError: embedded null byte`` -- uncaught, this
        propagates up through ``install_agent()`` and crashes agent
        bootstrap (denial of service via LLM-writable config).

        Fix: ``except (OSError, ValueError)`` around the resolve() pair.
        This test feeds a null-byte ``kiro_hooks_dir`` and asserts the
        code neither raises nor registers any hooks (falls back to the
        default which we re-route away from the caller's real
        ``~/.kiro/hooks``).  Under the pre-fix code, ``ValueError`` would
        escape and the test body would fail with an unhandled exception.
        """
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route the default so fallback doesn't touch caller's HOME.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": "\x00"}}

        # Must not raise.  Under pre-fix code this line propagates
        # ``ValueError: embedded null byte`` from Path.resolve().
        _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}

    def test_kiro_hooks_autoimport_cannot_read_dir_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``iterdir()`` OSError on hooks_dir must emit SEL audit.

        deep-review coverage finding: the ``cannot read
        <hooks_dir>`` rejection branch in ``_autoimport_kiro_hooks``
        (when ``hooks_dir.iterdir()`` raises ``OSError``, e.g. EACCES)
        was missing the ``_sel_hook_rejected`` call.  Without it, an
        auditor reconstructing agent-install activity cannot
        distinguish "hooks dir unreadable" from "no scripts configured"
        -- both show ``requested_autoimport=0`` in the merge summary.
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
        """Regression: non-string ``kiro_hooks_dir`` reverts to default silently.

        deep-review coverage finding: an LLM-writable
        ``"kiro_hooks_dir": null`` or ``[]`` silently skips the
        containment check (the ``isinstance(custom_dir, str) and
        custom_dir`` guard) and uses the default ``~/.kiro/hooks``.
        A malicious config that intentionally sets the value to a
        non-string should NOT emit a false-positive SEL rejection
        (nothing was actually rejected), but it also should not
        crash or scan an unintended directory.

        This test asserts: (a) fallback to default, (b) no SEL call,
        (c) no crash.
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
        """Regression: HOME-containment survives HOME-as-symlink topology.

        deep-review coverage finding: managed workstations and
        macOS laptops often have ``$HOME`` as a symlink (e.g.
        ``/home/user -> /mnt/fast-disk/user``).  The strict-containment
        check uses ``home = Path.home().resolve()`` and ``resolved =
        requested.resolve()`` -- both canonicalize, so the check should
        survive.  This test proves that contract: user points
        ``kiro_hooks_dir`` at the canonical (resolved) HOME target
        directly, while ``Path.home()`` returns the symlink; both
        must canonicalize to the same path and get rejected.

        Under a hypothetical regression where one side stops calling
        ``.resolve()``, the paths would mismatch and the test would
        accept the equal-to-HOME config, failing the assertion.
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
        """Regression: ``hooks_dir.resolve()`` OSError must emit SEL audit.

        review-bot rev 5 follow-up (agent.py:509,
        security-controls): the initial ``hooks_dir.resolve()`` failure
        branch returned early with only a ``logger.debug`` -- no
        ``_sel_hook_rejected`` call.  Same audit-completeness gap class
        as rev 5 fixed for the per-entry cannot-resolve-entry branch,
        missed on the directory-level resolve.

        This test forces ``Path.resolve`` to raise ``OSError`` for the
        hooks_dir specifically and asserts a SEL audit is recorded with
        the ``"autoimport"`` source tag and a ``"cannot resolve
        hooks_dir"`` reason.
        """
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
        """Regression: ``ValueError`` from ``entry.resolve()`` must not crash.

        review-bot rev 5 follow-up (agent.py:540, default):
        the two inner ``resolve()`` calls in ``_autoimport_kiro_hooks``
        only caught ``OSError``, not ``ValueError``.  A filename from
        ``iterdir()`` containing a null byte (FS shenanigans, adversarial
        filenames) would propagate ``ValueError: embedded null byte``
        uncaught and crash agent bootstrap.

        Fix: ``except (OSError, ValueError)`` on both inner resolve
        calls.  This test forces ``Path.resolve`` on an entry to raise
        ``ValueError`` and asserts the code rejects cleanly (no crash,
        SEL audited).
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
        """Regression: OSError from ``Path.resolve()`` falls back cleanly.

        deep-review coverage finding: if
        ``requested.resolve()`` or ``Path.home().resolve()`` raises
        ``OSError`` (ENAMETOOLONG, ELOOP, EACCES on a path component),
        the code sets ``resolved = home = None`` and falls through to
        the rejection branch, logs a warning, and emits SEL audit.

        This test forces the OSError path and asserts: (a) no crash,
        (b) hooks empty (fallback), (c) SEL audit emitted, (d) warning
        logged.  Under a hypothetical regression where the except is
        narrowed back to only ``OSError`` without catching ValueError,
        this test would still pass -- it specifically exercises the
        ``OSError`` arm of the ``except (OSError, ValueError)`` block.
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

"""A refused boot records why, no init system restarts it, and ``pod ls`` shows the reason.

An unverifiable OS home refuses the boot, a link planted at the pod home, the refusal note or
``config.json`` is not followed, a terminal return or a ``PodError`` in the boot body is
recorded, launchd gets exit 0 only when that record exists, and the record cannot be aimed
outside the pod plane. The fixtures come from ``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import stat
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
    requires_posix_pod_lifecycle,
)

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import launchd
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import EXIT_REFUSED_UNRECOVERABLE, TERMINAL_BOOT_EXIT_CODES, PodConfig


class TestBootRefusesAnUnverifiableOsHome:
    """The pod's ``os-home`` becomes the kiro-cli child's ``HOME``
    (``build_pod_env`` exports ``KIROCREW_OS_HOME``,
    ``acp.client._apply_pod_home_remap`` assigns it). If a component is a planted
    symlink, the seeding refuses to write through it -- but an earlier revision
    then BOOTED ANYWAY with the refused path, so the child received the link's
    target as its HOME and wrote its own MCP OAuth grants into the real host
    tree: the exact machine-level grant writer this mechanism exists to prevent.
    A refusal must therefore stop the boot, not just the seed."""

    def _boot(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[int, list[list[str]], PodConfig]:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(root / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(root / "pods"))
        c = PodConfig.load()
        rt.pin_checkout(c, "x", _ready_worktree(root, "x"))
        seen: list[list[str]] = []
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(os, "execve", lambda path, argv, e: seen.append(argv))
        return rt.boot(c, "x"), seen, c

    @requires_posix_pod_lifecycle
    def test_a_symlinked_os_home_aborts_the_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        c = PodConfig.load()
        home = c.home_dir("x")
        home.mkdir(parents=True)
        exfil = tmp_path / "agent-readable"
        exfil.mkdir()
        (home / "os-home").symlink_to(exfil, target_is_directory=True)

        code, seen, _ = self._boot(tmp_path, monkeypatch)

        assert code == EXIT_REFUSED_UNRECOVERABLE, code
        assert seen == [], "the gateway was exec'd despite an unverifiable OS home"

    @requires_posix_pod_lifecycle
    def test_the_refusal_exit_is_exempt_from_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Restart=on-failure`` + ``RestartSec=5`` would otherwise turn the
        refusal into a 5s loop that re-runs the same refusal forever and buries
        the FATAL line -- the cost that made refusing look unavailable before."""
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        rendered = unit_mod.render_unit(PodConfig.load())

        directive = [
            ln for ln in rendered.splitlines() if ln.startswith("RestartPreventExitStatus=")
        ]
        assert len(directive) == 1, rendered
        exempt = {int(tok) for tok in directive[0].split("=", 1)[1].split()}
        assert EXIT_REFUSED_UNRECOVERABLE in exempt
        # The pre-existing standing refusals belong to the same class: none of
        # them is healed by trying again five seconds later.
        assert exempt == set(TERMINAL_BOOT_EXIT_CODES)

    @requires_posix_pod_lifecycle
    def test_a_normal_os_home_still_boots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of the contract -- without this the refusal above
        passes vacuously if boot started refusing everything."""
        # flake-ok: boot runs the real child probe only where PATH has kiro-cli -- moved verbatim
        code, seen, c = self._boot(tmp_path, monkeypatch)

        assert code == 0, code
        assert len(seen) == 1, "a normal pod did not exec the gateway exactly once"
        assert (c.home_dir("x") / "os-home").is_dir()


class TestLaunchdTerminalRefusalIsNotRestartLooped:
    """launchd has NO per-exit-code exemption -- no ``RestartPreventExitStatus``
    analogue. Its only restart discriminator is the success/failure axis, and this
    backend renders ``KeepAlive = {"SuccessfulExit": False}``: restart on NON-ZERO
    (``launchd.plist(5)``: "If true, the job will be restarted as long as the
    program exits and with an exit status of zero. If false, the job will be
    restarted in the inverse condition.").

    So the systemd fix -- exit 78 and let the unit exempt it -- is exactly what
    LOOPS here: launchd relaunches every ``ThrottleInterval`` (5s) and re-runs the
    identical refusal. The terminal exit code is translated to 0 instead."""

    def test_a_terminal_refusal_exits_zero_so_launchd_does_not_relaunch(self) -> None:
        assert launchd.launchd_exit_code(EXIT_REFUSED_UNRECOVERABLE) == 0

    @pytest.mark.parametrize("code", list(TERMINAL_BOOT_EXIT_CODES))
    def test_every_terminal_code_is_translated_not_just_the_new_one(self, code: int) -> None:
        """The provisioning (3) and live-port (70) refusals are the same class: no
        retry heals them, so on macOS they must not loop either."""
        assert launchd.launchd_exit_code(code) == 0

    @pytest.mark.parametrize("code", [1, 2, 69, 71, 77, 79, 130, 137, 255])
    def test_an_ordinary_crash_still_exits_non_zero_so_it_still_restarts(self, code: int) -> None:
        """The half that must NOT be lost to stopping the loop: any other failure
        keeps its code, stays non-zero, and KeepAlive still self-heals it."""
        assert launchd.launchd_exit_code(code) == code

    def test_a_clean_exit_is_untouched(self) -> None:
        assert launchd.launchd_exit_code(0) == 0

    def test_the_keepalive_policy_the_terminal_exit_relies_on_is_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Couples the two halves. Exiting 0 only makes a refusal terminal while
        the policy is "restart on non-zero". Flipping this to ``True``, or to a
        bare ``KeepAlive: True``, silently restores the 5s loop with every test
        above still green -- so the plist key is asserted here rather than assumed."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        body = launchd.render_plist(PodConfig.load(), "x")

        assert body["KeepAlive"] == {
            "SuccessfulExit": False
        }, "launchd_exit_code's exit-0 mechanism depends on this exact policy"
        # And the loop interval it would otherwise retry on, so the cost being
        # avoided stays visible in the test record.
        assert body["ThrottleInterval"] == 5

    def test_no_plist_key_is_needed_so_there_is_no_stale_plist_upgrade_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The systemd side needed ``_REQUIRED_DIRECTIVES`` because its unit is
        written once by ``pod install`` and an ADDED directive would never reach an
        existing machine. launchd needs no analogue for two independent reasons,
        both pinned here: the mechanism adds no key at all (it reuses the policy
        this backend has always rendered), and plists are re-rendered on every
        ``up``, so they cannot go stale."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()

        first = set(launchd.render_plist(cfg, "x"))
        launchd.write_plist(cfg, "x")
        rewritten = set(launchd.render_plist(cfg, "x"))

        assert first == rewritten
        # No terminal-refusal key was introduced; the mechanism is the exit code.
        assert not [k for k in first if "Restart" in str(k) or "Refus" in str(k)]


class TestATerminalRefusalStaysLegibleOnBothPlatforms:
    """Once launchd's copy of the refusal exits 0, ``launchctl`` cannot tell it
    from a clean exit -- so the host-side note is what carries the fact. systemd
    does not need it (the unit sits ``failed`` with the FATAL line in the journal)
    but writes it anyway, so both platforms report the same thing."""

    def test_a_refused_boot_records_the_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        assert rt.refusal_reason(cfg, "x") is None

        rt._record_refusal(cfg, "x", "pod OS home /p/os-home is a symlink")

        assert "symlink" in (rt.refusal_reason(cfg, "x") or "")

    def test_a_later_clean_boot_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The note must mean "the LAST boot refused", not "a boot once did", or a
        healed pod reports a refusal forever."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt._record_refusal(cfg, "x", "boom")

        rt._clear_refusal(cfg, "x")

        assert rt.refusal_reason(cfg, "x") is None

    def test_an_empty_note_still_reports_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The file EXISTING is the signal; a truncated write must not read as
        clean, which would be the fail-open direction."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        target = cfg.refusal_file("x")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

        assert rt.refusal_reason(cfg, "x") is not None

    @requires_posix_pod_lifecycle
    def test_a_provisioning_refusal_records_too_not_just_the_os_home_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_run_internal` prints "recorded at <path>" for EVERY translated terminal
        exit, so every one of them must actually write it. Exits 3 and 70 did not
        (found in review) -- they are translated to 0 on macOS exactly like 78, so
        they carry the same legibility problem. Exercised through the no-pinned-
        checkout path, the earliest terminal return in `boot`."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        code = rt.boot(cfg, "x")

        assert code == 3, code
        assert (
            rt.refusal_reason(cfg, "x") is not None
        ), "a terminal exit whose path _run_internal advertises must record it"
        assert rt.launchd.launchd_exit_code(code) == 0

    def test_the_note_lives_outside_the_pod_home_that_down_nukes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal is about that tree, and ``down`` deletes it, so the evidence
        cannot live inside it."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "root"))
        cfg = PodConfig.load()

        note = cfg.refusal_file("x").resolve()
        home = cfg.home_dir("x").resolve()

        assert home not in note.parents and note != home

    @requires_posix_pod_lifecycle
    def test_boot_records_the_refusal_it_returns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: the refusal path writes the note AND returns the terminal
        code, so macOS's exit-0 translation still has something to point at."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.pin_checkout(cfg, "x", _ready_worktree(tmp_path, "x"))
        home = cfg.home_dir("x")
        home.mkdir(parents=True)
        exfil = tmp_path / "agent-readable"
        exfil.mkdir()
        (home / "os-home").symlink_to(exfil, target_is_directory=True)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        code = rt.boot(cfg, "x")

        assert code == EXIT_REFUSED_UNRECOVERABLE
        assert rt.refusal_reason(cfg, "x") is not None
        # And that is the code launchd would have looped on.
        assert launchd.launchd_exit_code(code) == 0


@requires_posix_pod_lifecycle
class TestEveryBootPathWriteRefusesAPlantedLink:
    """CLASS 1 of the round-8 restructure. Every write/mkdir the boot path performs
    lands on an agent-influenced path, so a by-name `mkdir(parents=True)` or
    `write_text` is a truncation primitive aimed at whatever the name resolves to.
    Each is now routed through `pinned_fs.write_file_pinned` /
    `_ensure_pod_dir`, which pin the ancestor chain and refuse a link at the
    final component."""

    def test_a_link_at_the_pod_home_is_refused_not_followed(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        home = tmp_path / "pods" / "x"
        home.parent.mkdir(parents=True)
        home.symlink_to(elsewhere, target_is_directory=True)

        with pytest.raises(rt.PodError):
            rt._ensure_pod_dir(home, what="pod home")

        assert not (elsewhere / "workspace").exists()

    def test_a_link_at_the_refusal_note_does_not_truncate_its_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The note is HOST-side by round-6 design, so following a link here would
        let a pod agent make the gateway truncate an arbitrary host file."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        victim = tmp_path / "host-secret"
        victim.write_text("do not truncate me")
        note = cfg.refusal_file("x")
        note.parent.mkdir(parents=True, exist_ok=True)
        note.symlink_to(victim)

        rt._record_refusal(cfg, "x", "boom")  # best-effort: must not raise

        assert victim.read_text() == "do not truncate me"

    def test_a_link_at_the_seed_config_is_refused(self, tmp_path: Path) -> None:
        """`config.json` carries provider tokens. The create-only guard is an lstat,
        not `exists()`: `exists()` FOLLOWS a link, so a link pointing at any existing
        host file would read as "already configured" and boot the pod on the
        attacker's file with no write and no error. A link is refused."""
        victim = tmp_path / "host-secret"
        victim.write_text("keep me")
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.json").symlink_to(victim)

        with pytest.raises(rt.PodError, match="symbolic link"):
            rt.write_pod_config(home, "")

        assert victim.read_text() == "keep me"

    def test_a_directory_link_at_the_seed_config_is_refused(self, tmp_path: Path) -> None:
        """Same guard, with the shape a Windows writer can actually plant.

        A junction is directory-only, so it cannot alias a host FILE -- but a
        live one at ``config.json`` answered True to ``exists()`` and False to
        ``is_symlink()``, so the create-only guard read it as "already
        configured" and the pod booted with a directory where its config should
        be. Built with the product's own link helper so each platform exercises
        its own shape (junction on an unelevated Windows shard)."""
        somewhere = tmp_path / "somewhere"
        somewhere.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        platform_compat.symlink_or_junction(str(somewhere), str(home / "config.json"))
        assert platform_compat.is_link_or_junction(home / "config.json")

        with pytest.raises(rt.PodError, match="symbolic link or junction"):
            rt.write_pod_config(home, "")

        assert not any(somewhere.iterdir()), "wrote through the link"

    def test_a_junction_shaped_seed_config_is_refused_on_every_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The junction SHAPE, simulated so POSIX shards pin the guard too: the
        OS junction oracle ``platform_compat._ISJUNCTION`` recognises one real,
        empty directory while every ``pathlib`` predicate keeps its true answer
        (``is_symlink()`` False, ``exists()`` True). The old guard returned
        silently here."""
        home = tmp_path / "home"
        home.mkdir()
        entry = home / "config.json"
        entry.mkdir()
        monkeypatch.setattr(platform_compat, "_ISJUNCTION", lambda p: Path(p) == entry)
        assert platform_compat.is_link_or_junction(entry)
        assert not entry.is_symlink()

        with pytest.raises(rt.PodError, match="junction"):
            rt.write_pod_config(home, "")

    def test_a_normal_home_still_gets_its_config_and_workspace(self, tmp_path: Path) -> None:
        """The other half: hardening must not break the ordinary path."""
        home = tmp_path / "home"

        rt.write_pod_config(home, "")

        assert (home / "workspace").is_dir()
        assert json.loads((home / "config.json").read_text())["tunnel"]["enabled"] is False
        assert stat.S_IMODE(home.stat().st_mode) == 0o700

    def test_the_shared_chokepoint_is_what_both_publishers_use(self) -> None:
        """One implementation, so a third hand-rolled copy cannot diverge. `unit.py`
        had its own and the pod boot path grew a second; both now call
        `pinned_fs.write_file_pinned`."""
        import inspect

        from kiro_crew import pinned_fs as pfs

        assert "write_file_pinned" in inspect.getsource(unit_mod._write_unit_file_atomic_nofollow)
        assert "atomic_write_at" in inspect.getsource(pfs.write_file_pinned)


class TestEveryTerminalReturnOnTheBootPathIsRecorded:
    """Every terminal return on the boot path is recorded, including the
    scenario-seed path, which prints its own FATAL and returns 3. Enumerating the
    returns by AST is what makes a miss structurally impossible: a new bare
    `return <terminal code>` fails this test rather than shipping."""

    def _boot_returns(self) -> list[ast.Return]:
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(rt._boot_unguarded)))
        return [n for n in ast.walk(tree) if isinstance(n, ast.Return)]

    def test_no_terminal_code_is_returned_without_going_through_refuse(self) -> None:
        offenders: list[str] = []
        for node in self._boot_returns():
            value = node.value
            # A bare integer literal return. `return 0` is the success sentinel and
            # is not a refusal; anything in the terminal set must be recorded.
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                if value.value in TERMINAL_BOOT_EXIT_CODES:
                    offenders.append(f"line {node.lineno}: bare return {value.value}")
            # `return EXIT_REFUSED_UNRECOVERABLE` — a name, not a _refuse() call.
            elif isinstance(value, ast.Name) and value.id.startswith("EXIT_"):
                offenders.append(f"line {node.lineno}: bare return {value.id}")

        assert not offenders, (
            "every terminal-code return in boot() must go through _refuse so the "
            "refusal is recorded; found: " + "; ".join(offenders)
        )

    def test_boot_still_has_refuse_calls_so_the_scan_is_not_vacuous(self) -> None:
        """Guards the test above from passing because boot() stopped refusing at all."""
        refuse_returns = [
            n
            for n in self._boot_returns()
            if isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "_refuse"
        ]
        assert len(refuse_returns) >= 5, len(refuse_returns)

    @requires_posix_pod_lifecycle
    def test_the_scenario_seed_refusal_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scenario-seed refusal is recorded, pinned end to end."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.pin_checkout(cfg, "x", _ready_worktree(tmp_path, "x"))
        rt.write_env_file(cfg, "x", {"SEED": "scenario:does-not-exist"})
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(rt, "is_scenario_ref", lambda s: True)
        monkeypatch.setattr(
            rt,
            "seed_home_from_scenario",
            lambda c, n, s: (_ for _ in ()).throw(rt.PodError("no such scenario")),
        )
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        code = rt.boot(cfg, "x")

        assert code == 3, code
        assert rt.refusal_reason(cfg, "x") is not None


class TestTheExitTranslationRequiresTheRecord:
    """A terminal code translated to 0 tells launchd the boot ended cleanly, so the
    host-side note is the ONLY thing left carrying the refusal. Translating when the
    note failed to land produces a pod that looks cleanly stopped with no record of
    why -- worse than the restart loop the translation prevents."""

    def _cfg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        monkeypatch.setattr(rt, "IS_MACOS", True)
        return PodConfig.load()

    @pytest.mark.parametrize("code", list(TERMINAL_BOOT_EXIT_CODES))
    def test_a_terminal_code_without_a_record_keeps_its_honest_non_zero(
        self, code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._cfg(tmp_path, monkeypatch)
        assert rt.refusal_reason(cfg, "x") is None

        assert rt.terminal_exit_code(cfg, "x", code) == code

    @pytest.mark.parametrize("code", list(TERMINAL_BOOT_EXIT_CODES))
    def test_a_terminal_code_with_a_record_translates_to_zero(
        self, code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._cfg(tmp_path, monkeypatch)
        rt._record_refusal(cfg, "x", "recorded")

        assert rt.terminal_exit_code(cfg, "x", code) == 0

    @pytest.mark.parametrize("code", [1, 2, 69, 71, 130, 255])
    def test_an_ordinary_crash_is_never_translated(
        self, code: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = self._cfg(tmp_path, monkeypatch)
        rt._record_refusal(cfg, "x", "recorded")

        assert rt.terminal_exit_code(cfg, "x", code) == code

    def test_systemd_keeps_the_honest_code_even_with_a_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RestartPreventExitStatus already exempts these on systemd, so the honest
        code is also the non-looping one there."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        monkeypatch.setattr(rt, "IS_MACOS", False)
        cfg = PodConfig.load()
        rt._record_refusal(cfg, "x", "recorded")

        assert rt.terminal_exit_code(cfg, "x", EXIT_REFUSED_UNRECOVERABLE) == (
            EXIT_REFUSED_UNRECOVERABLE
        )

    def test_the_cli_uses_the_conditional_gate_not_the_raw_translation(self) -> None:
        """`launchd_exit_code` states the platform semantics but knows nothing about
        the record, so calling it directly from the CLI would reopen this."""
        import inspect

        source = inspect.getsource(pod_cli._run_internal)
        assert "terminal_exit_code(" in source
        assert "launchd_exit_code(" not in source


class TestNoRefusalEscapesBootWithANonTerminalExit:
    """A `raise PodError` must not escape boot to the CLI's generic handler, which
    exits 1 -- a code NOT in TERMINAL_BOOT_EXIT_CODES, so systemd retries and
    launchd's KeepAlive restarts it every ThrottleInterval. `boot` is a wrapper
    that converts any refusal into a recorded terminal exit, so escape is
    impossible by construction rather than by enumeration."""

    def test_boot_is_a_wrapper_that_converts_refusals(self) -> None:
        import inspect

        source = inspect.getsource(rt.boot)
        assert "_boot_unguarded(" in source
        assert "except PodError" in source
        assert "EXIT_REFUSED_UNRECOVERABLE" in source

    def test_a_refusal_raised_anywhere_in_the_body_becomes_a_terminal_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumerating raises could not close this: PodError is raised from ~40
        sites under pod/, many transitively reachable from boot. The wrapper covers
        every one, including any added later -- which is what the stall rule asks
        for.

        Raised from `read_env_file`, the first thing the body does AFTER the name is
        validated. Validation is OUTSIDE the guard on purpose: recording a refusal
        for an unvalidated name would make
        the refusal machinery a host-write primitive (see
        `TestTheRefusalRecordIsNotAHostWritePrimitive`). So the guard is proved with a
        post-validation site, which is the region it is actually responsible for."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        monkeypatch.setattr(
            rt,
            "read_env_file",
            lambda c, n: (_ for _ in ()).throw(rt.PodError("a future refusal nobody routed")),
        )
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        code = rt.boot(cfg, "x")

        assert code == EXIT_REFUSED_UNRECOVERABLE, code
        assert code in TERMINAL_BOOT_EXIT_CODES
        assert "future refusal" in (rt.refusal_reason(cfg, "x") or "")

    @requires_posix_pod_lifecycle
    def test_a_symlinked_config_exits_terminal_with_a_record_not_exit_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported instance, end to end: the config-link lstat refusal now
        lands as a recorded terminal exit instead of escaping as exit 1."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt.pin_checkout(cfg, "x", _ready_worktree(tmp_path, "x"))
        home = cfg.home_dir("x")
        home.mkdir(parents=True)
        victim = tmp_path / "host-secret"
        victim.write_text("keep me")
        (home / "config.json").symlink_to(victim)
        monkeypatch.setattr(rt, "derive_port", lambda c, n: 7811)
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        code = rt.boot(cfg, "x")

        assert code == EXIT_REFUSED_UNRECOVERABLE, code
        assert "symbolic link" in (rt.refusal_reason(cfg, "x") or "")
        assert victim.read_text() == "keep me"

    def test_both_init_systems_treat_that_exit_as_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """systemd exempts it via RestartPreventExitStatus; launchd needs the
        record-conditional translation to 0. Neither may restart-loop."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        rt._record_refusal(cfg, "x", "recorded")

        assert EXIT_REFUSED_UNRECOVERABLE in set(TERMINAL_BOOT_EXIT_CODES)
        exempt = unit_mod.render_unit(cfg).splitlines()
        directive = [ln for ln in exempt if ln.startswith("RestartPreventExitStatus=")]
        assert str(EXIT_REFUSED_UNRECOVERABLE) in directive[0]
        monkeypatch.setattr(rt, "IS_MACOS", True)
        assert rt.terminal_exit_code(cfg, "x", EXIT_REFUSED_UNRECOVERABLE) == 0


class TestTheRefusalRecordIsNotAHostWritePrimitive:
    """The wrapper records every refusal it catches, and `_record_refusal` derives
    its path from the NAME -- so catching a name-VALIDATION failure would let
    `pod _run /tmp/important` atomically overwrite `/tmp/important.refused` outside
    the pod plane. Two independent controls prevent that: validation happens before
    the guard, and the record refuses to write outside `pods_dir` regardless of
    caller."""

    @pytest.mark.parametrize(
        "bad_name",
        [
            "/tmp/important",
            "../escape",
            "../../etc/passwd",
            "sub/dir",
            "",
            ".",
            "..",
        ],
    )
    def test_a_path_shaped_name_writes_nothing_and_exits_non_zero(
        self, bad_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "root"))
        cfg = PodConfig.load()
        victim = tmp_path / "important"
        victim.write_text("keep me")
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        before = {p for p in tmp_path.rglob("*") if p.is_file()}
        code = rt.boot(cfg, bad_name)
        after = {p for p in tmp_path.rglob("*") if p.is_file()}

        assert code != 0, code
        assert after == before, f"boot created files for an invalid name: {after - before}"
        assert victim.read_text() == "keep me"

    def test_the_name_validation_failure_records_nothing_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no legitimate pod to record against, so the honest error exits
        WITHOUT a note -- the round-9 wrapper would have written one."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        monkeypatch.setattr(os, "execve", lambda p, a, e: pytest.fail("gateway was exec'd"))

        rt.boot(cfg, "/tmp/important")

        assert not (tmp_path / "pods").exists() or not list((tmp_path / "pods").glob("*.refused"))

    def test_validation_runs_before_the_guarded_region(self) -> None:
        """Ordering is the primary control, so it is asserted rather than assumed:
        `validate_name` must appear before the `try` that calls the body."""
        import inspect

        source = inspect.getsource(rt.boot)
        assert source.index("validate_name(name)") < source.index("_boot_unguarded(cfg, name)")

    @pytest.mark.parametrize("bad_name", ["/tmp/important", "../escape", "sub/dir"])
    def test_record_refusal_refuses_an_invalid_name_on_its_own(
        self, bad_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defense in depth: the floor is a property of the function, not of one
        caller's ordering, so a future caller that records before validating cannot
        reopen this."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()
        victim = tmp_path / "important"
        victim.write_text("keep me")

        rt._record_refusal(cfg, bad_name, "boom")

        assert victim.read_text() == "keep me"
        assert not list(tmp_path.glob("*.refused"))

    def test_a_valid_name_still_records_in_the_pod_plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor must not be passing by refusing everything."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        cfg = PodConfig.load()

        rt._record_refusal(cfg, "x", "a real refusal")

        assert "real refusal" in (rt.refusal_reason(cfg, "x") or "")
        assert cfg.refusal_file("x").parent == cfg.pods_dir


class TestLsSurfacesARefusedPod:
    """`PodConfig.refusal_file`'s justification is that `pod ls` reports a terminal
    refusal the same way on both service managers. A refused pod is not running, so
    without this it falls out of the listing entirely and `ls` prints "no pods
    running" -- the note has no reader, and a pod that silently vanishes from `ls`
    is exactly how a boot failure hides."""

    def test_a_refused_pod_appears_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        c = PodConfig.load()
        rt._record_refusal(c, "x", "pod OS home is a symbolic link")
        monkeypatch.setattr(rt, "active_names", lambda cfg: set())
        monkeypatch.setattr(rt, "orphan_homes", lambda cfg: [])

        pod_cli._ls(c, argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert "1 pod(s) REFUSED to boot" in out
        assert "symbolic link" in out
        assert "kirocrew pod down x" in out

    def test_no_refusal_section_when_nothing_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The section must not appear for an ordinary empty plane."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        c = PodConfig.load()
        monkeypatch.setattr(rt, "active_names", lambda cfg: set())
        monkeypatch.setattr(rt, "orphan_homes", lambda cfg: [])

        pod_cli._ls(c, argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert "REFUSED" not in out
        assert "no pods running" in out

    def test_a_cleared_refusal_stops_appearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """`_clear_refusal` runs on a boot that gets past the refusal, so the section
        must describe the LAST boot rather than every boot that ever refused."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "pods"))
        c = PodConfig.load()
        rt._record_refusal(c, "x", "boom")
        rt._clear_refusal(c, "x")
        monkeypatch.setattr(rt, "active_names", lambda cfg: set())
        monkeypatch.setattr(rt, "orphan_homes", lambda cfg: [])

        pod_cli._ls(c, argparse.Namespace(json=False))

        assert "REFUSED" not in capsys.readouterr().out

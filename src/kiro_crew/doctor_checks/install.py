"""Install rows of ``kirocrew doctor``: what is installed here and where it runs from.

The project directory, the data home and a leftover legacy home, deployed cron
scripts and installed skills against their sources, the ``kirocrew`` launcher on
``PATH``, the program the installed service starts, an editable install's source
checkout, the Python import path, and the timezone data the runtime resolves zones
from.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import render

# Top-level entries that hold a Python virtual environment rather than user
# data. An older wheel install could nest its managed venv INSIDE the legacy
# ``~/.kirocrew`` home, so a leftover legacy dir may still contain the running
# interpreter — deleting it would break the live install.
_LEGACY_VENV_DIR_NAMES = ("venv", ".venv", "venvs")


def _legacy_venv_entries(home: Path) -> list[str]:
    """Names of virtual-environment entries at the top of *home* (best-effort)."""
    try:
        return sorted(name for name in _LEGACY_VENV_DIR_NAMES if (home / name).is_dir())
    except OSError:  # pragma: no cover - defensive
        return []


def _doctor_data_home_owner(issues: list[str]) -> None:
    """Report data-home entries another account owns, and where the docs explain them.

    Joins ``issues``: a home this account cannot read fails every config read and
    every Bot create, which is exactly what the doctor exists to catch.
    """
    from kiro_crew.data_home_owner import HELP_URL, check_data_home

    access = check_data_home(Path(cli_doctor.config_dir()))
    if access.ok:
        print("  owner:       ✅ this account owns its files")
        return
    whose = f"belong to {access.owner}" if access.owner else "can't be used by this account"
    print(f"  owner:       ❌ some files {whose}, so Kiro Crew can't read them:")
    for path in access.foreign:
        print(f"               {path}")
    print("               Kiro Crew was probably started as root once (for example with sudo).")
    print("               Stop running it as root, then give the files back to your account:")
    print(f"               {HELP_URL}")
    issues.append(f"data home files {whose}; see {HELP_URL}")


def _doctor_data_home() -> None:
    """Report the data home and any leftover top-level ``~/.kirocrew`` directory.

    The data root is ``~/.kiro/crew`` (or a valid ``KIROCREW_HOME`` override). A
    leftover top-level ``~/.kirocrew`` is not the data home unless an override
    points at it; a leftover that still holds a virtual environment is flagged as
    UNSAFE to delete (it may be the live interpreter), otherwise it is reported as
    an unused directory. Purely informational — doctor never deletes it itself.
    """
    print("\nData Home")
    home = cli_doctor.config_dir()
    # Print the canonical (symlink-resolved) spelling, the same form the PATH
    # launcher row prints: one install reached through a ``current`` symlink and
    # through its versioned directory must not read as two installs.
    print(f"  location:    ✅ {os.path.realpath(home)}")
    _doctor_browser_leftover(Path(home))

    legacy = Path.home() / cli_doctor.LEGACY_CONFIG_DIR_NAME
    if not legacy.is_dir():
        return
    override_home = cli_doctor._valid_override_home()
    if override_home is not None:
        try:
            points_at_legacy = override_home == legacy.resolve()
        except OSError:  # pragma: no cover - defensive
            points_at_legacy = override_home == legacy
        if points_at_legacy:
            # The override points AT the legacy dir, so it IS the active data
            # home — don't mislabel the home the process is actually using.
            print(
                f"  legacy:      ✅ {legacy} is the ACTIVE data home "
                f"(KIROCREW_HOME override points to it)"
            )
            return
    venvs = _legacy_venv_entries(legacy)
    if venvs:
        # A wheel install could nest its managed venv here; the dir survives to
        # hold it. Never advise deleting it — removing it takes the running
        # interpreter with it (`which kirocrew` may resolve through it).
        print(
            f"  legacy:      ✅ {legacy} retained to hold a Kiro Crew "
            f"virtual environment ({', '.join(venvs)})"
        )
        print(
            "               Do NOT delete it while it is your active install "
            "— removing it would delete the running interpreter."
        )
        return
    print(
        f"  legacy:      ⏹ {legacy} present but not the data home — safe to "
        f"delete once you have confirmed it holds nothing you need"
    )


def _doctor_cron_script_sources(issues: list[str]) -> None:
    """Report deployed cron scripts that disagree with their skill-asset source.

    The installed-to-``crons/`` hop is a hand-made copy that nothing else compares,
    so a deploy can run superseded code while looking healthy. Divergence has no
    direction: a cron script body is LLM-writeable by design, so it can be a stale
    deploy or a deliberate edit, and doctor leaves the reconciliation to the owner.
    Silent when nothing deployed has a source.
    """
    from kiro_crew.skills import (
        CRON_SOURCE_DIVERGED,
        CRON_SOURCE_IN_SYNC,
        deployed_cron_script_sources,
    )

    states = deployed_cron_script_sources()
    if not states:
        return

    print("\nCron Script Sources")
    for state in states:
        # Both halves are read off disk and BOTH go through _safe_display. The
        # crons dir is agent-writeable by design (see cron_script.py), so a
        # deployed script's name is not merely untrusted in the abstract -- the
        # design deliberately lets an agent choose it. A diagnostic that reads
        # those names and prints them raw is exactly the wrong consumer for such
        # a directory: an OSC/ANSI sequence or a newline in a filename would
        # drive the terminal or forge the surrounding verdict lines.
        name = render._safe_display(state.name)
        source = render._safe_display(str(state.source))
        if state.state == CRON_SOURCE_IN_SYNC:
            print(f"  {name}:  ✅ agrees with {source}")
        elif state.state == CRON_SOURCE_DIVERGED:
            print(f"  {name}:  ❌ DIVERGED from {source}")
        else:
            print(f"  {name}:  ⏹ could not be compared against {source}")

    if any(state.state == CRON_SOURCE_DIVERGED for state in states):
        issues.append("deployed cron script diverged from its skill source")
        print(
            "               Reconcile by hand: the path after DIVERGED is the "
            "installed skill's copy, the crons/ file is what the cron runs. "
            "A diverged copy may be a stale deploy OR an intentional local edit "
            "-- doctor cannot tell which, so it does not overwrite either one."
        )


def _doctor_skill_currency(issues: list[str]) -> None:
    """Report installed skills that do not match the package this build ships.

    The sync's update gate compares mtimes, so an installed copy newer than anything
    the package ships is skipped, and the operator keeps running superseded code.
    A shipped file cannot report its own currency (a hardcoded version goes stale
    the first time someone edits without bumping it); doctor sees both sides.

    "Behind" means the install does not match the source tree the sync selects for
    it, not a remote revision: doctor makes no network call. A skill no source root
    ships is absent from the result.

    POSIX only for now. On Windows the comparison would have to read each
    install by name, where losing the race against a substituted junction costs
    an outbound authenticated connection rather than a wrong answer, so this
    prints the boundary instead of a verdict there.
    """
    from kiro_crew.skills import (
        SKILL_INSTALL_BEHIND,
        SKILL_INSTALL_EDITED,
        SKILL_INSTALL_IN_SYNC,
        installed_skill_currency,
    )

    if os.name == "nt":
        # Said out loud: an absent section would read as "all installs current".
        print("\nInstalled Skill Currency")
        print(
            "  not checked on this platform yet: the comparison reads each "
            "installed directory by name, and the pinned read that makes that "
            "safe here does not exist yet, so no verdict is printed rather than "
            "one taken unsafely"
        )
        return

    states = installed_skill_currency()
    if not states:
        return

    behind = [state for state in states if state.state == SKILL_INSTALL_BEHIND]
    edited = [state for state in states if state.state == SKILL_INSTALL_EDITED]
    unverifiable = [
        state
        for state in states
        if state.state not in (SKILL_INSTALL_IN_SYNC, SKILL_INSTALL_BEHIND, SKILL_INSTALL_EDITED)
    ]

    print("\nInstalled Skill Currency")
    print(
        f"  {len(states) - len(behind) - len(edited) - len(unverifiable)} in step with this build"
    )
    # Skill names and source paths come off disk, out of a packaged tree or a
    # KIROCREW_PROJECT_DIR tree, so a name is untrusted text: printed raw, an
    # OSC/ANSI sequence or an embedded newline in a directory name would drive
    # the terminal or forge a verdict line. The source path is displayed for the
    # same reason the cron section displays its own: it names which tree the
    # verdict was reached against, which is what makes a project skill
    # shadowing a builtin legible rather than surprising.
    for state in behind:
        print(
            f"  {render._safe_display(state.name)}:  ❌ does not match "
            f"{render._safe_display(str(state.source))}"
        )
    for state in edited:
        print(
            f"  {render._safe_display(state.name)}:  ⚠ edited since install, so it is not "
            f"compared against {render._safe_display(str(state.source))}"
        )
    for state in unverifiable:
        print(
            f"  {render._safe_display(state.name)}:  ⏹ could not be compared against "
            f"{render._safe_display(str(state.source))}"
        )

    if behind:
        # "Source tree", not "package": a project skill is judged against the
        # project tree that shadows the builtin.
        issues.append("installed skill does not match the source tree the sync selects for it")
        print(
            "               Restart the gateway to re-run the skill sync. A name "
            "that persists after a restart carries an mtime NO OLDER than "
            "anything that source tree holds, so the sync reads it as up to date "
            "and skips it: "
            "move that installed directory OUT of the skills directory (a "
            "rename in place keeps a readable SKILL.md, which discovery "
            "publishes as a second copy of the same skill) and restart again, "
            "and the sync reinstalls it from that source. A behind copy still "
            "matches the marker the sync wrote, so it holds no local edits to "
            "lose."
        )
    if edited:
        print(
            "               An edited copy is not overwritten in place. While no "
            "update is due it stays on its current code; when an update IS due "
            "the sync moves the edited tree aside to a dot-prefixed backup "
            "beside it and installs the packaged version, so the edit stops "
            "taking effect until it is reconciled."
        )


#: Basename of Toolbox's shared dispatcher. Every Toolbox tool name on PATH is a
#: link to it, and it picks the tool from the name it was invoked as.
_TOOLBOX_DISPATCHER = "toolbox-exec"


def _toolbox_launcher_runs_this_version(on_path: str) -> bool:
    """Whether *on_path* is the Toolbox dispatcher and runs this install's version.

    A path comparison cannot answer this for Toolbox: ``on_path`` resolves to the
    one dispatcher every tool shares, never to this install's own binary. So when
    THIS install is Toolbox-managed and ``on_path`` resolves to that dispatcher,
    ask it: ``--version`` run through the PATH spelling (the dispatcher reads its
    argv[0]) must print exactly this install's version line. Any other answer --
    another version, a failed or timed-out run, unreadable output, a host that
    cannot build the sandbox -- is False, so the caller keeps its warning.

    The launcher is whatever ``which`` found first on PATH, so a file an agent
    wrote into a PATH directory (an activated worktree venv, say) can reach this
    point. It is run the way an agent-influenced command is: through the sandbox
    chokepoint with a credential-scrubbed environment and the kernel resource
    ceiling, never bare with this process's privileges.
    """
    import subprocess

    from kiro_crew import env, version_line

    if os.path.basename(os.path.realpath(on_path)) != _TOOLBOX_DISPATCHER:
        return False
    if not env.is_toolbox_install():
        return False
    sandbox = cli_doctor.sandbox
    try:
        wrapped, child_env, cleanup = sandbox.sandboxed_spawn_argv(
            [on_path, "--version"], mode="strict", strip_python_env=True
        )
    except (RuntimeError, OSError):
        return False
    try:
        result = sandbox.run_limited(
            wrapped,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    return result.returncode == 0 and result.stdout.strip() == version_line()


def _doctor_path_launcher() -> None:
    """Report which install the ``kirocrew`` command on PATH actually belongs to.

    A gateway never takes the name from another install's working launcher (see
    ``agent.ensure_kirocrew_on_path``). With a cli.sh wheel and a deb/rpm desktop
    install on one machine, ``kirocrew`` can run a different version or channel
    than the running app, and the desktop app logs that where nobody reads it.

    Read-only: it resolves and compares paths, and never writes or relinks.
    """
    from kiro_crew.agent import _resolve_kirocrew_bin

    on_path = shutil.which("kirocrew")
    if not on_path:
        # Not an error on its own: the desktop app runs its bundled backend
        # directly, and a user who never wanted a terminal command is fine.
        print("  kirocrew CLI: ⏹ not on PATH (run `kirocrew setup` to link it)")
        return
    running = _resolve_kirocrew_bin()
    if not os.path.isabs(running) or os.path.realpath(on_path) == os.path.realpath(running):
        # Canonical spelling, matching the Data Home row and the comparison above.
        print(f"  kirocrew CLI: ✅ {os.path.realpath(on_path)}")
        return
    if _toolbox_launcher_runs_this_version(on_path):
        # The realpath is Toolbox's shared dispatcher, so the PATH spelling is the
        # one that names the tool.
        print(f"  kirocrew CLI: ✅ {on_path} (Toolbox launcher, same version)")
        return
    print("  ⚠ kirocrew CLI on PATH belongs to a different install than this one.")
    # Paths are printed UNWRAPPED, one per line: a wrapped path cannot be copied
    # or pasted into a command, which is the first thing someone does with it.
    print(f"{render._INDENT}on PATH:      {os.path.realpath(on_path)}")
    print(f"{render._INDENT}this install: {os.path.realpath(running)}")
    render._print_wrapped(
        "Both can coexist — the wheel keeps its own updates — but `kirocrew` in a "
        "terminal runs the one on PATH, which may be a different version or "
        "channel. Run `kirocrew setup` from the install you want to own the name."
    )


#: The prefix an earlier version's browser CLI installer wrote under the data
#: home. Nothing reads it any more; doctor names it and leaves it alone.
_LEFTOVER_BROWSER_CLI_LEAF = "playwright-cli"


def _doctor_service_launch() -> None:
    """Report which install the installed service starts, and warn on a source checkout.

    The service definition persists the path of the ``kirocrew`` it launches. When
    that path is, or leads through, the shared ``~/.local/bin/kirocrew`` launcher,
    a source clone's ``install.sh`` or ``kirocrew setup`` can repoint it, and the
    service runs the clone from its next restart on. On a host that also has the
    managed wheel install, updates then run inside that checkout and anything done
    in it reaches the running service, with nothing else saying so. This follows
    the definition's own program (the systemd unit's ``ExecStart``, or the launcher
    the launchd plist runs) to the file it finally executes.

    Dev Fleet's "Make live" pointer is a deliberate checkout, so a service that
    runs one while the pointer is set is reported, not warned on. A host with no
    managed wheel install is a development host and runs its checkout on purpose.

    Read-only and advisory: it reads the definition and never rewrites it.
    """
    from kiro_crew.service import controller, live_target

    unit = controller.installed_unit_path()
    if unit is None:
        return
    program = _service_program(unit)
    if program is None:
        print(f"  service runs: ⚠️  could not read the program the service starts ({unit})")
        return
    real = os.path.realpath(program)
    checkout = _kirocrew_checkout_of(Path(real))
    # A link target is whatever its writer chose, so a path that carries a
    # control character is shown escaped; an ordinary one stays copyable.
    shown = real if real.isprintable() else render._safe_display(real)
    if checkout is None:
        print(f"  service runs: ✅ {shown}")
        return
    if live_target.read_target() is not None:
        print(f"  service runs: ℹ️  {shown} (source checkout, Dev Fleet live target is set)")
        return
    wheel = _managed_wheel_launcher()
    if wheel is None:
        print(f"  service runs: ✅ {shown} (source checkout)")
        return
    print("  ⚠️  The installed service runs a source checkout, not the wheel install.")
    print(f"{render._INDENT}service runs:  {shown}")
    print(f"{render._INDENT}wheel install: {wheel}")
    render._print_wrapped(
        "Updates run inside that checkout, and anything done in it reaches the "
        "running service. To run the wheel install again, run `service install` "
        "with the wheel install's kirocrew shown above, then restart the service. "
        "To run a checkout on purpose, use Dev Fleet's Make live instead."
    )


def _service_program(unit: Path) -> str | None:
    """The program an installed service definition finally executes, or ``None``.

    A systemd unit names it as the first word of its last ``ExecStart=``; only the
    unit file is read, not a drop-in an operator added beside it. The launchd
    plist runs a generated launcher whose ``exec`` line names it.
    """
    if unit.suffix == ".plist":
        from kiro_crew.gateway_restart import _Inconclusive, _launcher_target
        from kiro_crew.service import macos

        payload = macos._plist_payload(unit)
        argv = payload.get("ProgramArguments") if payload else None
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            return None
        if argv[0] != str(macos.LIVE_PROGRAM):
            return argv[0]
        try:
            return _launcher_target(Path(argv[0]))
        except (_Inconclusive, OSError, ValueError):
            # ValueError: an ``exec`` line shlex cannot split (an unterminated quote).
            return None
    try:
        text = unit.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    program = None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != "ExecStart":
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            words = []
        # An empty ``ExecStart=`` resets the list; a leading ``-@:+!`` is a
        # systemd prefix, not part of the path.
        program = words[0].lstrip("-@:+!").replace("%%", "%") if words else None
    return program or None


def _kirocrew_checkout_of(path: Path) -> Path | None:
    """The Kiro Crew source checkout *path* lies in, or ``None``.

    A checkout is a directory holding both a ``.git`` entry (a directory, or the
    file a linked worktree has) and the ``src/kiro_crew`` package.
    """
    for parent in path.parents:
        try:
            if (parent / ".git").exists() and (parent / "src" / "kiro_crew").is_dir():
                return parent
        except OSError:
            return None
    return None


def _managed_wheel_launcher() -> Path | None:
    """The managed wheel install's ``kirocrew``, when this host has one."""
    from kiro_crew.platform.wheel_engine_layout import managed_venv_layout

    layout = managed_venv_layout()
    for tree in (layout.stable_link, layout.legacy):
        launcher = tree / "bin" / "kirocrew"
        try:
            if launcher.is_file():
                return launcher
        except OSError:
            continue
    return None


def _doctor_browser_leftover(home: Path) -> None:
    """Report a leftover browser CLI prefix from an earlier version, if any.

    Informational only: the directory is never deleted or modified here, and
    nothing is appended to the issues list. Silent when it is absent.
    """
    leftover = home / _LEFTOVER_BROWSER_CLI_LEAF
    try:
        present = leftover.is_dir()
    except OSError:
        return
    if not present:
        return
    print(
        f"  browser:     ℹ  {render._safe_display(str(leftover))} is left over from an "
        "earlier version and is no longer used; it can be deleted"
    )


def _doctor_source_checkout(repo: Path) -> None:
    """Report whether an editable install's source tree is current.

    An editable install (``pip install -e``) runs whatever the checkout is at
    process start, so one parked on a stale feature branch silently lacks every fix
    merged since, security gates included. Doctor names the branch and how far
    behind the default branch it is.

    Advisory only (never appended to ``issues``): a feature branch is a legitimate
    developer state. Offline by design (no ``git fetch``), so "behind" means behind
    the LAST-FETCHED default branch; the case caught is a parked checkout while
    fetches happen around it, not a host that never fetches.
    """
    print("\nSource Checkout")
    if not (repo / ".git").exists():
        print(f"  source:      ⏹ not a git checkout ({repo})")
        return

    branch = cli_doctor._git_line(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        print("  branch:      ⚠️  could not check (git failed)")
        return

    # Default branch as recorded at clone time (refs/remotes/origin/HEAD).
    # `git remote show` would be authoritative but hits the network.
    default_ref = cli_doctor._git_line(repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    default_branch = default_ref.split("/", 1)[1] if default_ref and "/" in default_ref else None

    if default_branch is None:
        # Fresh clones always have origin/HEAD; only manual remote surgery
        # loses it. Report the branch we ARE on and stop — guessing "main"
        # could mislabel a repo whose default genuinely differs.
        print(f"  branch:      ⚠️  {branch} (could not determine default branch)")
        return

    behind = cli_doctor._git_line(repo, "rev-list", "--count", f"HEAD..origin/{default_branch}")

    if branch == default_branch:
        if behind is None or not behind.isdigit():
            # A failed count must not masquerade as a verified-fresh checkout:
            # "up to date" is a claim this probe could not actually establish.
            print(f"  branch:      ⚠️  {default_branch} (could not count commits behind origin)")
            return
        if int(behind) > 0:
            print(
                f"  branch:      ⚠️  {default_branch}, {behind} commit(s) behind origin (as of last fetch)"
            )
            print("               The running gateway predates those commits until an")
            print("               update + restart.")
        else:
            print(f"  branch:      ✅ {default_branch} (up to date as of last fetch)")
        return

    detail = (
        f", {behind} commit(s) behind origin/{default_branch}"
        if behind and behind.isdigit() and int(behind) > 0
        else ""
    )
    print(f"  branch:      ⚠️  on '{branch}' — not the default branch{detail}")
    print("               The gateway runs this checkout as-is: fixes merged to")
    print(f"               {default_branch} since divergence are NOT active, and update")
    print(f"               pulls this branch, not {default_branch}.")
    # Remediation stays prose, never a rendered command: branch and path come
    # from the repository (agent-writable), and a ref named e.g.
    # ``$(touch${IFS}/tmp/pwn)`` pasted from a suggested command line would
    # execute in the operator's shell.
    print("               Fix: check out the default branch in the source checkout,")
    print("               then update + restart.")


def _doctor_import_path(issues: list[str]) -> None:
    """Report where the standard library resolves from, and whether the launch
    directory can shadow it.

    The process entries already refuse a shadowed stdlib; this row also shows an
    install that LETS the launch directory onto ``sys.path`` (no ``-P``), where a
    ``~/concurrent/`` harmless from one directory breaks the gateway from another.
    A shadow is an issue; a launch entry is a note, because a console script's own
    ``bin/`` is the ordinary case for a pip install.
    """
    shadows = cli_doctor.stdlib_shadow.find_shadowed_stdlib()
    if shadows:
        for s in shadows:
            entry = s.path_entry or os.getcwd()
            # ascii(): the path is caller-chosen bytes; escape control characters
            # rather than write them to the terminal.
            print(f"  import path: ❌ {s.name} shadowed by {ascii(s.resolved)}")
            print(f"               sys.path entry {ascii(entry)} ({s.entry_kind})")
        remedy = cli_doctor.stdlib_shadow.remedy_command(shadows[0])
        if remedy is None:
            print(
                "               Fix: move or rename the shadowed path above, or run from another directory"
            )
        else:
            print(f"               Fix: {remedy}, or run from another directory")
        issues.append("stdlib shadowed")
        return
    launch = None
    if not getattr(sys.flags, "safe_path", False) and sys.path:
        launch = sys.path[0]
    if launch is None:
        print("  import path: ✅ stdlib intact (launch directory kept off sys.path: -P)")
    else:
        print(
            f"  import path: ✅ stdlib intact; launch entry on sys.path: {launch or os.getcwd()!r}"
        )
        print("               A stdlib-named directory placed there would shadow the stdlib")


def _doctor_project(issues: list[str]) -> str:
    """Render the ``Project`` section and return the project directory it settled on.

    ``KIROCREW_PROJECT_DIR`` wins; otherwise the ``project_dir`` file ``kirocrew
    setup`` saved. The value is returned as found -- even one that names no
    directory -- because the model-pin audit and the Model section read it the
    same way.
    """
    print("\nProject")
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    stale_project = False
    if not proj:
        saved_proj = cli_doctor.config_dir() / "project_dir"
        if saved_proj.is_file():
            try:
                saved = saved_proj.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                # A file left root-owned by one ``sudo kirocrew setup``, or not
                # UTF-8: report it as this row, so the sections after it still
                # print (Data Home's owner row is the one that explains it).
                print(f"  source dir:  ❌ {saved_proj} is unreadable ({type(exc).__name__})")
                print(
                    f"               Fix: rm {shlex.quote(str(saved_proj))}, then run kirocrew setup"
                )
                issues.append("unreadable project_dir")
                return ""
            if saved and Path(saved).is_dir():
                proj = saved
            else:
                print(f"  source dir:  ❌ stale — points to deleted {saved}")
                print(f"               Fix: rm {shlex.quote(str(saved_proj))}")
                issues.append("stale project_dir")
                stale_project = True
    if proj and Path(proj).is_dir():
        # Only a directory carrying cli.py's ``_PROJECT_MARKERS`` is a Kiro
        # Crew source checkout — claim it only when measured, so an explicit
        # ``KIROCREW_PROJECT_DIR`` naming an unrelated repository is not
        # mislabelled as the checkout.
        from kiro_crew.cli import _PROJECT_MARKERS  # deferred: cli imports the doctor

        is_checkout = all((Path(proj) / m).is_dir() for m in _PROJECT_MARKERS)
        if is_checkout:
            print(f"  source dir:  ✅ {proj} (Kiro Crew source checkout)")
        else:
            print(f"  source dir:  ✅ {proj}")
        # A git worktree or submodule stores ``.git`` as a FILE holding a
        # ``gitdir:`` pointer, not a directory, so accept both forms.
        git_marker = Path(proj) / ".git"
        if git_marker.exists():
            print("  git repo:    ✅")
        elif is_checkout:
            print("  git repo:    ⚠️  source checkout is not a git repo")
        else:
            print("  git repo:    ⚠️  not a git repo")
    elif not stale_project:
        print(
            "  source dir:  ⚠️  not set (set from a Kiro Crew checkout by"
            " kirocrew setup; not needed for wheel installs)"
        )
    return proj


def _doctor_sqlite_fts5(issues: list[str]) -> None:
    """Report whether this interpreter's SQLite carries FTS5.

    Memory and knowledge full-text search need it. On macOS and Linux aarch64 the
    host sqlite3 build is what runs (pysqlite3-binary is x86_64-Linux only), and a
    build without FTS5 breaks memory init.
    """
    try:
        from kiro_crew._sqlite_compat import PYSQLITE3_REQUIREMENT, fts5_available

        if fts5_available():
            print("  sqlite fts5: ✅ available")
        else:
            print("  sqlite fts5: ❌ missing (memory/knowledge search will fail)")
            # Only the pip half is gated. Where that command cannot run, using a
            # different Python IS the remaining fix, so it stays visible in
            # exactly the case the gate hides the command.
            if cli_doctor.pip_install_channel_available():
                print(
                    f"{render._INDENT}Fix: {cli_doctor.pip_install_command_for(PYSQLITE3_REQUIREMENT)}"
                )
            print("               Or use a Python whose SQLite was built with FTS5.")
            issues.append("sqlite fts5")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  sqlite fts5: ⚠️  could not check ({exc})")


# ── timezone data ─────────────────────────────────────────────────────────────
# Cron expressions and clock-time ``at_time`` values resolve through stdlib
# ``zoneinfo``, which uses whatever data it finds: a system zoneinfo tree on
# macOS and most Linux hosts, the ``tzdata`` package where no tree exists
# (Windows, and Linux hosts without the OS tzdata package). Stale
# data still resolves every zone name, so nothing at save or fire time can tell
# it apart from current data; a job just runs at the hour the old rule gives.
#
# The probe is behavioural because a system tree exposes no version the stdlib
# can read. IANA tzdb 2026b keeps America/Vancouver on UTC-7 all year; older
# data still moves it back to UTC-8 each November. A December instant in the
# first winter under the new rule therefore separates data from 2026b on from
# everything older.
_TZ_PROBE_ZONE = "America/Vancouver"
_TZ_PROBE_INSTANT = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
_TZ_PROBE_OFFSET = timedelta(hours=-7)


def _tz_probe_offset() -> timedelta | None:
    """The probe zone's UTC offset at the probe instant, read fresh from disk.

    ``ZoneInfo.no_cache`` bypasses the process-wide zone cache, so the answer
    describes the data installed now. Raises ``ZoneInfoNotFoundError`` when no
    installed data resolves the zone.
    """
    zone = zoneinfo.ZoneInfo.no_cache(_TZ_PROBE_ZONE)
    return _TZ_PROBE_INSTANT.astimezone(zone).utcoffset()


def _tz_data_from_system() -> bool:
    """Whether the probe zone resolves from a system zoneinfo tree.

    ``zoneinfo`` searches ``TZPATH`` first and falls back to the ``tzdata``
    package only when no tree holds the zone, so a file on ``TZPATH`` is the
    data in use and the system package is what needs updating.
    """
    parts = PurePosixPath(_TZ_PROBE_ZONE).parts
    return any(Path(root, *parts).is_file() for root in zoneinfo.TZPATH)


def _doctor_tz_data(issues: list[str]) -> None:
    """Report whether the timezone data in use carries current rules.

    A ``tz data`` row is printed either way. Out-of-date or missing data is an
    issue, and the remedy names the source that supplies the data: the
    operating system's timezone package, or the ``tzdata`` Python package. A
    running gateway keeps every zone it already loaded until it restarts, so
    the remedy ends with a restart.
    """
    try:
        offset = _tz_probe_offset()
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        print("  tz data:     ❌ no timezone data resolves America/Vancouver")
        print("               A named timezone does not resolve, so a job cannot use one.")
        if cli_doctor.pip_install_channel_available():
            print(f"{render._INDENT}Fix: {cli_doctor.pip_install_command_for('tzdata')}")
        print("               Or install the operating system's timezone data (tzdata).")
        issues.append("timezone data missing")
        return
    if offset == _TZ_PROBE_OFFSET:
        print("  tz data:     ✅ current (IANA tzdb 2026b or later)")
        print("               Sample check on America/Vancouver, not your configured zone.")
        return
    print("  tz data:     ⚠️  out of date (older than IANA tzdb 2026b)")
    print("               It still moves America/Vancouver back to UTC-8 on")
    print("               2026-11-01, so a job in that zone runs an hour late.")
    if _tz_data_from_system():
        print("               Fix: update the operating system's timezone data")
        print("               (the tzdata package), then restart the gateway.")
    elif cli_doctor.pip_install_channel_available():
        print(f"{render._INDENT}Fix: {cli_doctor.pip_install_command_for('-U', 'tzdata')}")
        print("               then restart the gateway.")
    else:
        print("               Fix: update Kiro Crew, then restart the gateway.")
    issues.append("timezone data out of date")


def _doctor_tls_trust(issues: list[str]) -> None:
    """Report, on macOS, whether HTTPS clients trust the system trust store.

    A CA installed in the Keychain (a corporate or MDM root) is trusted only
    through the system trust store. When it could not be enabled, HTTPS falls back
    to file-based CAs and a download behind such a root fails certificate
    verification, so this is an issue. An operator's ``SSL_CERT_FILE`` is their
    choice and only reported. No row off macOS.
    """
    from kiro_crew import _ssl_compat

    state, detail = _ssl_compat.macos_trust_state()
    if state == _ssl_compat.TRUST_NOT_MACOS:
        return
    if state == _ssl_compat.TRUST_SYSTEM:
        print("  tls trust:   ✅ macOS system trust store")
        return
    if state == _ssl_compat.TRUST_OPERATOR_BUNDLE:
        print(
            f"  tls trust:   ✅ SSL_CERT_FILE={render._safe_display(detail)} (Keychain CAs not used)"
        )
        return
    print("  tls trust:   ❌ macOS system trust store not in use")
    print(f"               {render._safe_display(detail)}")
    print("               CAs installed in the Keychain are not trusted, so an HTTPS")
    print("               download behind a corporate root fails verification.")
    print("               Fix: update or reinstall Kiro Crew, then restart the gateway.")
    issues.append("tls trust")

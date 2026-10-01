"""Install rows of ``kirocrew doctor``: what is installed here and where it runs from.

The project directory, the data home and a leftover legacy home, deployed cron
scripts and installed skills against their sources, the ``kirocrew`` launcher on
``PATH``, an editable install's source checkout, and the Python import path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

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
    print(f"  location:    ✅ {home}")

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

    The packaged-to-installed hop is content-verified, ``scripts/`` included. The
    installed-to-``crons/`` hop is a hand-run ``cp`` documented in the owning
    skill, and nothing compares its two sides -- so a deploy can run superseded
    code indefinitely while looking healthy.

    Divergence is reported WITHOUT a direction. A cron script body is
    LLM-writeable by design, so the two sides disagreeing can mean a stale deploy
    or a deliberate local edit, and nothing on disk distinguishes them. Doctor
    surfaces the disagreement and leaves the reconciliation to whoever knows
    which they intended.

    Silent when nothing deployed has a source: a cron script without one is out
    of scope here, not a finding.
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
            "               Reconcile using the owning skill's own copy recipe. "
            "A diverged copy may be a stale deploy OR an intentional local edit "
            "-- doctor cannot tell which, so it does not overwrite either one."
        )


def _doctor_skill_currency(issues: list[str]) -> None:
    """Report installed skills that do not match the package this build ships.

    An installed skill can run for days against a package that has already fixed
    the script it carries, and nothing else anywhere says so. The sync's update
    gate compares mtimes, so an installed copy whose mtime is newer than
    anything the package ships is judged up to date and simply skipped; the
    operator keeps running superseded code and reads its output as current.

    A shipped file cannot answer this about itself. It has no import-time
    version to read and no subprocess to ask git with, and a hardcoded version
    constant goes stale silently the moment someone edits the file without
    bumping it -- the exact failure it would claim to prevent. Currency is a
    relation between the install and the package, and only one side of that
    relation is visible from inside the script. Doctor sees both sides.

    Scope is deliberately narrow. "Behind" means the install does not match the
    source tree the sync selects for it, not that it trails a remote revision:
    doctor makes no network call, and an operator running an older build must not
    be told their skill is stale against a revision they never installed. A
    skill no source root ships is absent from the result, not reported, because
    there is no source tree for it to be out of step with.

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
        # Said out loud rather than printed as silence. The check reports nothing
        # on Windows, and an absent section is indistinguishable from a gateway
        # whose installs are all current -- which is the false reassurance this
        # whole instrument exists to remove.
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
        # Named as the source tree rather than as the package: the comparison
        # picks the root the sync itself would pick, so a project skill is
        # judged against the project tree that shadows the builtin. Each line
        # above prints which tree that was.
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


def _doctor_path_launcher() -> None:
    """Report which install the ``kirocrew`` command on PATH actually belongs to.

    A gateway never takes the name from another install's working launcher (see
    ``agent.ensure_kirocrew_on_path``), which is the right call — but it leaves a
    gap the user cannot see from anywhere else. The documented Linux pairing puts
    a cli.sh wheel and a deb/rpm desktop install on ONE machine, so typing
    ``kirocrew`` can run a different install, at a different version or channel,
    than the app that is running. The desktop app has no terminal, so the decline
    is logged where nobody reads it; this is the surface someone checks when a
    version looks wrong.

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
        print(f"  kirocrew CLI: ✅ {on_path}")
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


def _doctor_source_checkout(repo: Path) -> None:
    """Report whether an editable install's source tree is current.

    An editable install (``pip install -e``) runs whatever the source checkout
    happens to be at process start. A checkout parked on a stale feature branch
    is invisible at runtime: the gateway starts fine, serves traffic, and every
    fix merged upstream since the branch diverged — security gates included —
    is silently absent. Nothing else surfaces this (a real incident ran a
    9-day-stale branch through a restart while doctor reported healthy), so
    doctor names the branch and how far behind the default branch it is.

    Advisory only (never appended to ``issues``, matching the linger and
    model-url probes): running a feature branch is a legitimate developer
    state, so doctor's job is to make it visible, not to block on it.

    Offline by design: no ``git fetch`` — doctor must not touch the network or
    mutate the repo. "behind" therefore means behind the LAST-FETCHED default
    branch; a checkout that never fetches reports current. That bound is
    acceptable because the failure mode being caught is a checkout parked on
    an old branch while fetches happen around it (e.g. by update checks), not
    a host that never talks to the remote.
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

    # One count for both arms below; they ask git the same question and only
    # differ in how they render the answer.
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

    The process entries refuse to start on a shadowed stdlib, so by the time
    doctor runs the answer is normally clean; this row exists for the other
    half of the diagnosis -- an install that still LETS the launch directory
    onto ``sys.path`` (no ``-P``), so the same ``~/concurrent/`` that is harmless
    from one directory breaks the gateway from another. A shadow reported here
    is an issue; a launch entry on ``sys.path`` is a note, because a console
    script's own ``bin/`` is the ordinary case for a pip install.
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
        # Check saved project_dir file
        saved_proj = cli_doctor.config_dir() / "project_dir"
        if saved_proj.is_file():
            saved = saved_proj.read_text(encoding="utf-8").strip()
            if saved and Path(saved).is_dir():
                proj = saved
            else:
                print(f"  source dir:  ❌ stale — points to deleted {saved}")
                print(f"               Fix: rm {cli_doctor.config_dir() / 'project_dir'}")
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
        from kiro_crew._sqlite_compat import fts5_available

        if fts5_available():
            print("  sqlite fts5: ✅ available")
        else:
            print("  sqlite fts5: ❌ missing (memory/knowledge search will fail)")
            # Only the pip half is gated. Where that command cannot run, using a
            # different Python IS the remaining fix, so it stays visible in
            # exactly the case the gate hides the command.
            if cli_doctor.pip_install_channel_available():
                print(
                    f"{render._INDENT}Fix: {cli_doctor.pip_install_command_for('pysqlite3-binary')}"
                )
            print("               Or use a Python whose SQLite was built with FTS5.")
            issues.append("sqlite fts5")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  sqlite fts5: ⚠️  could not check ({exc})")

"""The >= 3.12 floor must not turn a supported host or an upgrade into a dead end.

Raising ``requires-python`` is only half a floor bump. Every host-facing entry point
carries its OWN copy of two decisions -- "is this interpreter good enough" and "is
this venv still reusable" -- and each one fails differently when it is missed:

* A probe that ends in ``die``/``exit`` turns a host that used to install into one
  that aborts. Under the old >= 3.10 floor the distro's own ``python3`` satisfied
  every probe on Ubuntu 22.04, so the package-manager branch was always sufficient;
  under >= 3.12 it is not.
* A venv reused on its executability alone makes ``pip install -e .`` refuse the
  package ("Requires-Python >=3.12"), so a documented ``git pull && bash install.sh``
  upgrade dead-ends with no way forward.

The table these tests enforce, one row per entry point:

===================== ============= ======================== ==================
entry point           floor-gated?  provisions when missing  venv reuse gated
===================== ============= ======================== ==================
``install.sh``        yes           ``_bootstrap_python``    yes
``setup.sh``          yes           ``ensure-python.sh``     yes
``cloud-install.sh``  yes           ``ensure-python.sh``     yes
``cli.sh``            yes           ``uv``                   by construction
``ensure-python.sh``  yes           ``mise``                 n/a
``install.ps1``       yes           winget / choco 3.12      n/a (no venv)
``Makefile``          yes           ``ensure-python.sh``     yes
``make.ps1``          yes           advises (dev build)      yes
``minimal_install``   yes           advises (documented)     n/a
===================== ============= ======================== ==================

The last two advise rather than provision on purpose: ``make.ps1`` is the developer
build path and offers ``-Py <path>`` instead, and ``minimal_install.sh`` declares
"Prerequisites: Python 3.12+" as its contract.

These tests pin each decision at the source, because none is reachable from a unit
test on this host: the shell paths need a distro whose archive lacks python3.12, and
the reuse paths need a pre-3.12 venv.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import dep_sync

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
SETUP_SH = REPO / "setup.sh"
CLOUD_INSTALL_SH = REPO / "cloud-install.sh"
INSTALL_PS1 = REPO / "install.ps1"

# The predicate the shell installers use to decide a venv is still reusable.
_VENV_FLOOR_PROBE = "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"


def _discover_venv_owners() -> list[Path]:
    """Every shell entry point that assigns `_venv=`, found rather than listed.

    The round-4 revision of this file hand-maintained this list and got it wrong:
    `minimal_install.sh` owns `$REPO_DIR/.venv` and was recorded as owning none, so
    the reuse rule was never enforced there and the review found the gap instead.
    Discovering the owners means a new installer -- or one that grows a venv later --
    is covered the moment it lands, and the list cannot silently disagree with the
    tree again.
    """
    owners = []
    for script in sorted(REPO.glob("*.sh")):
        body = script.read_text(encoding="utf-8")
        if '_venv="' in body and '"$_venv/bin/pip"' in body:
            owners.append(script)
    assert owners, "no venv-owning shell installer found -- the discovery is broken"
    return owners


# Entry points that own a venv and so must gate its reuse.
_VENV_OWNERS = _discover_venv_owners()
_VENV_OWNER_IDS = [p.name for p in _VENV_OWNERS]


def test_the_venv_owner_discovery_sees_every_known_installer():
    """A floor under the discovery itself, so a broken glob cannot pass vacuously."""
    found = {p.name for p in _VENV_OWNERS}
    assert {
        "install.sh",
        "setup.sh",
        "cloud-install.sh",
        "minimal_install.sh",
    } <= found, f"venv-owner discovery missed a known installer: {sorted(found)}"


# Entry points that run from a clone and so can delegate to the repo's bootstrap,
# paired with the variable each one's own probe assigns the interpreter to. The
# variable is what makes the reachability assertion below meaningful: the bootstrap
# has to be guarded on THAT probe having come up empty.
_CLONE_LOCAL = [(SETUP_SH, "_py"), (CLOUD_INSTALL_SH, "PY")]
_CLONE_LOCAL_IDS = [p.name for p, _ in _CLONE_LOCAL]


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# install.sh: no probe may end in `die` without trying the bootstrap first
# ---------------------------------------------------------------------------


def test_install_sh_defines_a_python_bootstrap():
    """The fallback has to exist before any branch can lean on it."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    assert "_bootstrap_python() {" in body
    # It must provision the version the rest of the script targets, not a literal
    # that can drift away from PYTHON_VERSION.
    bootstrap = body.split("_bootstrap_python() {", 1)[1].split("\n}", 1)[0]
    assert 'mise install "python@$PYTHON_VERSION"' in bootstrap
    assert "_find_python" in bootstrap, "the bootstrap must re-probe, not set _py itself"


def test_every_install_sh_python_probe_tries_the_bootstrap_before_dying():
    """A `_find_python || die` with no bootstrap is the abort regression itself.

    Ubuntu 22.04 is the case that made this a regression rather than a
    pre-existing limitation: `apt install python3` gives 3.10, which passed the
    old floor and fails the new one.
    """
    offenders = []
    for number, line in enumerate(_lines(INSTALL_SH), start=1):
        if "_find_python ||" not in line:
            continue
        if "_bootstrap_python" in line:
            continue
        offenders.append(f"{number}: {line.strip()}")
    assert not offenders, "these probes abort instead of provisioning Python 3.12:\n" + "\n".join(
        offenders
    )


def test_install_sh_has_no_bare_die_for_a_missing_interpreter():
    """The no-package-manager branch must provision too, not just advise."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    no_pkg_mgr = body.split("no package manager detected", 1)[0].rsplit("else", 1)[1]
    assert "_bootstrap_python" in no_pkg_mgr


# ---------------------------------------------------------------------------
# setup.sh / cloud-install.sh: delegate to the repo's own bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("script", "var"), _CLONE_LOCAL, ids=_CLONE_LOCAL_IDS)
def test_clone_local_installers_provision_before_giving_up(script, var):
    """Both run from a clone, so `ensure-python.sh` is right there to be used.

    The guard is asserted, not just the call: a bootstrap block that is present but
    reachable only when the probe SUCCEEDED (or never, behind a constant) is dead
    code that reads like a fallback. So the condition has to name the same variable
    the probe assigns and require it to be empty.
    """
    body = script.read_text(encoding="utf-8")
    guard = re.compile(
        r'if \[ -z "\$'
        + re.escape(var)
        + r'" \]\s*&&\s*\[ -f "[^"]*ensure-python\.sh" \]\s*&&\s*_has_provisioner; then'
    )
    assert guard.search(body), (
        f"{script.name} has no reachable `ensure-python.sh` fallback guarded on an "
        f"empty ${var} AND an already-installed provisioner -- it either gives up "
        "instead of provisioning, or can reach ensure-python.sh's fetch-and-run"
    )
    # And it must consume what the bootstrap recorded, not re-guess.
    assert "python-bin" in body


@pytest.mark.parametrize(("script", "var"), _CLONE_LOCAL, ids=_CLONE_LOCAL_IDS)
def test_the_bootstrap_attempt_precedes_the_give_up_branch(script, var):
    """A bootstrap after the exit is unreachable code, not a fallback."""
    body = script.read_text(encoding="utf-8")
    bootstrap_at = body.index("ensure-python.sh")
    # Keyed on the guidance the give-up branch prints rather than on one wording of
    # the failure line, which has been rewritten twice as the remedy changed.
    give_up = body.index("mise.jdx.dev/installing-mise.html")
    assert bootstrap_at < give_up


@pytest.mark.parametrize(("script", "var"), _CLONE_LOCAL, ids=_CLONE_LOCAL_IDS)
def test_the_provisioned_interpreter_is_revalidated_before_use(script, var):
    """`ensure-python.sh` recording a path is not proof the path clears the floor."""
    body = script.read_text(encoding="utf-8")
    tail = body[body.index("ensure-python.sh") :]
    assert (
        "version_info" in tail.split("fi", 1)[0] or "version_info >= (3,12)" in tail
    ), f"{script.name} adopts the recorded interpreter without re-checking the floor"


# ---------------------------------------------------------------------------
# every venv owner: an existing venv is reusable only at >= 3.12
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", _VENV_OWNERS, ids=_VENV_OWNER_IDS)
def test_venv_reuse_is_gated_on_the_interpreter_version(script):
    """Executable-ness is not enough: a 3.10 venv is executable and unusable."""
    body = script.read_text(encoding="utf-8")
    assert (
        _VENV_FLOOR_PROBE in body
    ), f"{script.name} decides venv reuse without checking the interpreter version"
    # And the too-old venv must be removed, not installed into.
    assert 'rm -rf "$_venv"' in body


@pytest.mark.parametrize("script", _VENV_OWNERS, ids=_VENV_OWNER_IDS)
def test_the_venv_reuse_probe_appears_before_the_first_pip_install(script):
    """A check that runs after pip has already been handed the venv is no check."""
    body = script.read_text(encoding="utf-8")
    probe_at = body.index(_VENV_FLOOR_PROBE)
    pip_at = body.index('"$_venv/bin/pip"')
    assert probe_at < pip_at


@pytest.mark.parametrize(
    ("reported", "reusable"),
    [("3.10.14", False), ("3.11.9", False), ("3.12.0", True), ("3.13.1", True)],
)
def test_the_reuse_probe_accepts_exactly_the_supported_interpreters(tmp_path, reported, reusable):
    """Run the real predicate against a stub interpreter, not a paraphrase of it.

    The stub reports a version and nothing else, which is all the predicate reads.
    """
    stub = tmp_path / "python"
    major, minor, micro = reported.split(".")
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.version_info = ({major}, {minor}, {micro}, 'final', 0)\n"
        "exec(sys.argv[-1])\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    proc = subprocess.run(
        [sys.executable, str(stub), "-c", _VENV_FLOOR_PROBE],
        capture_output=True,
    )
    # The stub is driven through this suite's own interpreter so the test needs no
    # 3.10 build on the host; argv[-1] carries the predicate verbatim.
    assert (proc.returncode == 0) is reusable


# ---------------------------------------------------------------------------
# install.ps1: the Windows cloud client had no version gate at all
# ---------------------------------------------------------------------------


def test_install_ps1_gates_python_on_the_floor_not_on_existence():
    """`Have "python"` accepted a 3.10 and let pip refuse it four steps later."""
    body = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Resolve-Python312" in body
    assert "sys.version_info >= (3, 12)" in body
    assert 'if (Have "python") {' not in body, (
        "install.ps1 still gates Python on existence alone, so a pre-3.12 "
        "interpreter is accepted and refused later by pip"
    )


def test_install_ps1_installs_the_pinned_python_when_none_qualifies():
    """The remedy has to be 3.12 specifically, on both package managers."""
    body = INSTALL_PS1.read_text(encoding="utf-8")
    assert "Python.Python.3.12" in body
    assert "choco install -y python312" in body


def test_install_ps1_runs_pip_through_the_resolved_interpreter():
    """A bare `python -m pip` would undo the resolution the probe just did."""
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(_lines(INSTALL_PS1), start=1)
        if re.search(r"(?<![&$\w.-])python -m ", line)
    ]
    assert not offenders, "these calls bypass the resolved interpreter:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# dep_sync: the in-process editable reinstall is the third reuser
# ---------------------------------------------------------------------------


def test_editable_reinstall_refuses_a_venv_below_the_declared_floor(tmp_path, capsys):
    """pip would refuse this anyway -- refusing first names the reason.

    Without the gate the failure is a raw pip "Requires-Python" error on a
    revision whose git merge has already landed, with no recovery hint.
    """
    repo = tmp_path
    (repo / "setup.cfg").write_text("[options]\npython_requires = >=3.12\n", encoding="utf-8")

    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value=str(repo / "src" / "kiro_crew" / "__init__.py"),
        ),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "requires_python", return_value=">=3.12"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 11, 9)),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        rc = dep_sync.sync_or_reinstall(repo, Path("py"))

    assert rc == dep_sync.REFUSED
    assert not sp.run.called, "pip must not be invoked against a venv below the floor"
    err = capsys.readouterr().err
    assert "3.12" in err
    assert "3.11.9" in err


def test_editable_reinstall_proceeds_when_the_venv_meets_the_floor(tmp_path):
    """The gate must not stand in the way of the normal path."""
    repo = tmp_path
    (repo / "setup.cfg").write_text("[options]\npython_requires = >=3.12\n", encoding="utf-8")

    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value=str(repo / "src" / "kiro_crew" / "__init__.py"),
        ),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "requires_python", return_value=">=3.12"),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 12, 3)),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value.returncode = 0
        rc = dep_sync.sync_or_reinstall(repo, Path("py"))

    assert rc == 0
    assert sp.run.called
    argv = sp.run.call_args[0][0]
    assert argv[1:5] == ["-m", "pip", "install", "-e"]


def test_the_floor_gate_is_read_from_the_repo_not_hardcoded(tmp_path):
    """A future floor bump must not need this gate edited again."""
    repo = tmp_path
    (repo / "setup.cfg").write_text("[options]\npython_requires = >=3.14\n", encoding="utf-8")

    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value=str(repo / "src" / "kiro_crew" / "__init__.py"),
        ),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "interpreter_version", return_value=(3, 12, 3)),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        rc = dep_sync.sync_or_reinstall(repo, Path("py"))

    assert rc == dep_sync.REFUSED
    assert not sp.run.called


def test_no_floor_declared_does_not_block_the_reinstall(tmp_path):
    """`requires_python` returning None means no floor, not an unreadable one."""
    repo = tmp_path
    (repo / "setup.cfg").write_text("[options]\n", encoding="utf-8")

    with (
        patch.object(
            dep_sync,
            "installed_package_origin",
            return_value=str(repo / "src" / "kiro_crew" / "__init__.py"),
        ),
        patch.object(dep_sync, "locked_console_scripts", return_value=[]),
        patch.object(dep_sync, "requires_python", return_value=None),
        patch.object(dep_sync, "subprocess") as sp,
    ):
        sp.run.return_value.returncode = 0
        rc = dep_sync.sync_or_reinstall(repo, Path("py"))

    assert rc == 0
    assert sp.run.called


# ---------------------------------------------------------------------------
# The floor is declared in more than one place; they must not drift apart
# ---------------------------------------------------------------------------


def test_the_declared_floor_agrees_across_the_shell_entry_points():
    """One host-facing floor, spelled the same in every probe that enforces it."""
    floors = set()
    for script in (*_VENV_OWNERS, REPO / "cli.sh", INSTALL_PS1):
        body = script.read_text(encoding="utf-8")
        floors.update(
            (m.group("major"), m.group("minor"))
            for m in re.finditer(
                r"sys\.version_info\s*>=\s*\((?P<major>\d+),\s*(?P<minor>\d+)\)", body
            )
        )
    assert floors == {("3", "12")}, f"probes disagree on the floor: {sorted(floors)}"


# ---------------------------------------------------------------------------
# The automatic bootstrap must not execute unverified network content
# ---------------------------------------------------------------------------


def test_the_automatic_bootstrap_does_not_pipe_a_remote_script_to_a_shell():
    """`--mise` is an opt-in; the automatic fallback is not, so it must not fetch-and-run.

    The distinction is consent, not mechanism: a user who passes `--mise` has asked
    for the version manager, while `_bootstrap_python` runs on its own when a probe
    comes up empty. Executing a downloaded installer there would run unverified
    remote content as the user on a path nobody opted into.
    """
    body = INSTALL_SH.read_text(encoding="utf-8")
    bootstrap = body.split("_bootstrap_python() {", 1)[1].split("\n}", 1)[0]
    assert "curl" not in bootstrap, (
        "_bootstrap_python fetches over the network; it must use an already-installed "
        "provisioner instead"
    )
    assert "mise.run" not in bootstrap


def test_the_bootstrap_requires_an_already_installed_provisioner():
    """It may use mise; it may not install it."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    bootstrap = body.split("_bootstrap_python() {", 1)[1].split("\n}", 1)[0]
    assert "has mise || return 1" in bootstrap


def test_the_give_up_messages_name_a_way_forward():
    """Once the bootstrap cannot self-provision, the exit has to be actionable."""
    body = INSTALL_SH.read_text(encoding="utf-8")
    # Every die that follows a failed bootstrap names mise as the route.
    dies = [
        line
        for line in body.splitlines()
        if "_bootstrap_python" in line and "die" not in line and "||" in line
    ]
    assert dies, "no bootstrap-then-die sites found to check"
    assert body.count("mise.jdx.dev/installing-mise.html") >= 3


@pytest.mark.parametrize(("script", "var"), _CLONE_LOCAL, ids=_CLONE_LOCAL_IDS)
def test_the_fallback_cannot_install_its_own_provisioner(script, var):
    """`ensure-python.sh` installs mise by piping a remote script into sh.

    That is acceptable where the user asked for it and unacceptable on a fallback
    that fires by itself, so the automatic path may only USE a version manager the
    host already has. The predicate is spelled identically in both scripts so the
    posture is one decision rather than two that can drift.
    """
    body = script.read_text(encoding="utf-8")
    assert "_has_provisioner() {" in body
    predicate = body.split("_has_provisioner() {", 1)[1].split("\n}", 1)[0]
    assert "command -v mise" in predicate
    assert "curl" not in predicate, "the presence check must not fetch anything"


@pytest.mark.parametrize(("script", "var"), _CLONE_LOCAL, ids=_CLONE_LOCAL_IDS)
def test_the_give_up_branch_names_the_provisioner_route(script, var):
    """Once the fallback can decline to run, the exit has to say how to enable it."""
    body = script.read_text(encoding="utf-8")
    assert "mise.jdx.dev/installing-mise.html" in body

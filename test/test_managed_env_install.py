"""Install + boot hook over a fake ssh runner. No network, no ssh, no sandbox.

The fake box interprets only the commands in ``install.COMMANDS_*``; any other command is
recorded in ``unknown`` and fails with rc 127, so "no command outside the list" is a plain
assert. ``mkdir``/``chown``/``chmod``/``rm -f`` inside those commands are applied literally
(owner and mode of each path, under the box's umask), so a missing chmod shows up as a wrong
mode.

``install-public`` is a script on the stdin of ``sh -s``. :class:`FakeImage` is a temp dir
standing for the image's ``/`` (pyenv under an image link, its shims, the image pipx, a fake
one-liner), and the shipped script runs on it under real ``sh`` through the test seam (a root
prefix and the fetch command). Each case runs under an inherited PATH that puts the fake
tree's pyenv shims and ``usr/local/bin`` ahead of its base dirs, as the image PATH does, and
asserts what the fake one-liner saw, not only the return value.

The one-liner this models is the served ``cli.sh`` at sha256 ``cca9aa36dfb5d670…0462e6c9``:
``:6``/``:35`` the documented ``| sh -s -- --system-python``
form; ``:654-659`` ``_resolve_python`` (python3.12, python3.13, python3, each by ``_py_usable``'s
5 s ``sys.version_info >= (3,12)`` run); ``:1043`` the pipx branch whenever ``command -v pipx``
answers; ``:1142-1145`` the venv at ``${KIROCREW_HOME:-$HOME/.kiro/crew}-venv``; ``:1171-1174``
no ``venv``/``ensurepip`` -> managed Python; ``:1305`` the ``-venv-current`` link;
``:1328-1330`` ``$HOME/.local/bin/kirocrew``; ``:1464-1473``, ``:1522-1525``, ``:1640`` exit 3
(installed, no KAS engine on this host; first install only).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401

from kiro_crew.managed_env import engine as eng
from kiro_crew.managed_env import install as ins

#: Bound at import, before conftest's autouse guard replaces them for every test (A2 below).
_GENUINE_SSH_RUN = ins.SshRunner.run
_GENUINE_ENGINE_SSH_RUN = eng.InstallSshRunner.run

HOST = "crew-sbx-abc123"
#: The image openssl a test configures (`instances.managed_env_box_openssl`); a placeholder path.
IMAGE_OPENSSL = "/opt/example/bin/openssl"
LISTED = {cmd: cid for cid, (_, cmd) in {**ins.COMMANDS_A, **ins.COMMANDS_E}.items()}
assert ins.COMMANDS_A[ins.CMD_INSTALL_PUBLIC][1] == "sh -s"
#: ``machine-config``'s text is built per launch from the lane's values: one shape, its prefix.
MACHINE_CONFIG_PREFIX = f"{ins.WRAPPER} config set agent.kas_bundle true "


def command_id(command: str, stdin: bytes | None) -> str | None:
    """The closed list's id for one exec, or None for a command outside it."""
    if command == "sh -s":
        return ins.CMD_INSTALL_PUBLIC if (stdin or b"").startswith(b"# install-public") else None
    if command.startswith(MACHINE_CONFIG_PREFIX) and command.endswith(
        f"echo {ins.MACHINE_CONFIG_DONE}"
    ):
        return ins.CMD_MACHINE_CONFIG
    return LISTED.get(command)


#: What the fake box's install-public prints before the one-liner (today's image, rung a1).
STEP_FACTS = (
    "python_source=pyenv\npython=/root/.pyenv/versions/3.12.13/bin/python3.12\npipx=absent\n"
    "image_pipx=broken\nplace=/projects/.crew/home\nladder=/projects/.crew/bin/python3.12\n"
)


class FakeBox:
    """A managed environment as a user finds it: no Crew until the install puts it there."""

    def __init__(
        self,
        *,
        signed_in=True,
        umask=0o022,
        gateway_up_after=1,
        install_rc=0,
        installs_kirocrew=True,
        step_stop="",
        route=ins.ROUTE_KAS_BUNDLE,
        fetch_rc=0,
        machine_config_rc=0,
    ):
        self.tools: set[str] = set()
        #: the installed launcher's ``kas route`` answer (kas-bundle: the probe's have:kas-bundle)
        self.route = route
        self.fetch_rc = fetch_rc  # install-public's download fails with this exit
        self.signed_in = signed_in
        self.umask = umask
        self.gateway_up_after = (
            gateway_up_after  # gateway probe answers "up" from this many misses on
        )
        self.install_rc = install_rc
        self.installs_kirocrew = installs_kirocrew
        self.step_stop = step_stop  # install-public stops before the one-liner with this reason
        self.machine_config_rc = machine_config_rc
        self.machine_config: list[tuple[str, str]] = []  # the (key, value) pairs config set wrote
        self.calls: list[tuple[str, str, bytes | None]] = []  # (id, command, stdin)
        self.unknown: list[str] = []
        self.ops: list[str] = []  # literal chmod/chown/rm segments applied
        self.files: dict[str, dict] = {}  # path -> {"owner","mode","data"}
        self.probes = 0
        self.gateway = False
        self.hook_runs = 0
        #: per command id, the answers that replace its next runs' own (rc, keep_output, extra)
        self.answers: dict[str, list[tuple[int, bool, str]]] = {}

    # -- runner ---------------------------------------------------------------------------
    def run(self, host, command, stdin=None):
        assert host == HOST
        cid = command_id(command, stdin)
        self.calls.append((cid or "?", command, stdin))
        if cid is None:
            self.unknown.append(command)
            return ins.RunResult(127, "sh: not on the list\n")
        res = getattr(self, "_" + cid.replace("-", "_"))(command, stdin)
        queued = self.answers.get(cid)
        if queued:
            rc, keep_output, extra = queued.pop(0)
            res = ins.RunResult(rc, (res.out if keep_output else "") + extra)
        return res

    def answer_next(self, cid, rc=255, *, times=1, keep_output=True, extra=""):
        """The next ``times`` runs of ``cid`` answer ``rc``. The command still runs on the box;
        ssh hands back its own output (``keep_output``) or none (a silent 255), then ``extra``
        (a transport line: ``mint-and-attach: …``, an ssh message)."""
        self.answers.setdefault(cid, []).extend([(rc, keep_output, extra)] * times)

    def ids(self):
        return [c[0] for c in self.calls]

    # -- tiny fs --------------------------------------------------------------------------
    def _mkdir(self, path):
        # like mkdir -p: every missing ancestor under /projects is made too, owned by the ssh user
        parts = path.strip("/").split("/")
        for i in range(2, len(parts) + 1):
            self.files.setdefault(
                "/" + "/".join(parts[:i]),
                {"owner": "ssh-user", "mode": 0o777 & ~self.umask, "data": None},
            )

    def _apply(self, command):
        for seg in command.split("&&"):
            w = seg.split()
            if w[:2] == ["mkdir", "-p"]:
                for p in w[2:]:
                    self._mkdir(p)
            elif w and w[0] == "chown":
                self.ops.append(seg.strip())
                for p in w[2:]:
                    self.files[p]["owner"] = w[1].split(":")[0]
            elif w and w[0] == "chmod":
                self.ops.append(seg.strip())
                for p in w[2:]:
                    self.files[p]["mode"] = int(w[1], 8)
            elif w[:2] == ["rm", "-f"]:
                self.ops.append(seg.strip())
                for p in w[2:]:
                    self.files.pop(p, None)

    # -- commands -------------------------------------------------------------------------
    def _have(self):
        names = ("kirocrew", "crew-wrapper", ins.HAVE_KAS_BUNDLE)
        return {t for t in names if t in self.tools}

    def _probe(self, command, stdin):
        out = "".join(f"have:{t}\n" for t in sorted(self._have()))
        return ins.RunResult(0, out + "probe=done\n")

    def _install_public(self, command, stdin):
        if self.step_stop:
            facts = STEP_FACTS.replace("ladder=/projects/.crew/bin/python3.12\n", "")
            return ins.RunResult(21, facts + f"stop={self.step_stop}\n")
        if self.fetch_rc:  # the download failed: its own exit, curl's stderr last
            why = "The requested URL returned error: 403" if self.fetch_rc == 22 else "failed"
            return ins.RunResult(
                25,
                STEP_FACTS + f"stop=fetch_failed\nfetch_rc={self.fetch_rc}\n"
                f"curl: ({self.fetch_rc}) {why}\n",
            )
        if self.install_rc not in (0, 3):
            return ins.RunResult(
                23,
                STEP_FACTS + "kirocrew-install: openssl is required to verify "
                "the signed manifest\n"
                f"install_rc={self.install_rc}\n",
            )
        if self.installs_kirocrew:
            self.tools |= {
                "kirocrew",
                "crew-wrapper",
            }  # cli.sh's ~/.local/bin link, and the launcher
        if self.installs_kirocrew and self.route == ins.ROUTE_KAS_BUNDLE:
            self.tools.add(ins.HAVE_KAS_BUNDLE)
        wrapper = "ok" if self.installs_kirocrew else "failed"
        return ins.RunResult(
            0 if self.installs_kirocrew else 24,
            STEP_FACTS + f"install_rc={self.install_rc}\nwrapper={wrapper}\n",
        )

    def _machine_config(self, command, stdin):
        if self.machine_config_rc:
            return ins.RunResult(self.machine_config_rc, "❌ agent.kas_auth: unknown key\n")
        out = []
        for call in command.split(" && ")[:-1]:
            words = shlex.split(call)
            key, value = words[3], words[4]
            self.machine_config.append((key, value))
            out.append(f"✅ {key} = {value}")
        return ins.RunResult(0, "\n".join(out) + f"\n{ins.MACHINE_CONFIG_DONE}\n")

    def _hook_dirs(self, command, stdin):
        self._apply(command)
        return ins.RunResult(0)

    def _hook_write(self, command, stdin):
        tmp, final = ins.HOOK_TMP, ins.HOOK
        self.files[tmp] = {"owner": "ssh-user", "mode": 0o666 & ~self.umask, "data": stdin}
        self._apply(command.split("cat >" + tmp + " && ", 1)[1].rsplit(" && mv", 1)[0])
        self.files[final] = self.files.pop(tmp)
        return ins.RunResult(0)

    def _hook_run(self, command, stdin):
        f = self.files.get(ins.HOOK)
        assert (
            f and f["owner"] == "root" and f["mode"] & 0o111
        ), "hook must be root-owned and executable"
        self.hook_runs += 1
        # the hook's order: a live gateway, the launcher runs, the bundle route, then the sign-in
        if self.gateway:
            return ins.RunResult(0, "50-kirocrew-gateway: gateway alive (pid 7)\n")
        have = self._have()
        if not {"crew-wrapper", "kirocrew"} & have:
            return ins.RunResult(ins.LAUNCHER_FAILED_EXIT, "launcher_failed\n")
        if ins.HAVE_KAS_BUNDLE not in have:
            return ins.RunResult(ins.NOT_KAS_BUNDLE_EXIT, "not_kas_bundle\n")
        if not self.is_signed_in():
            return ins.RunResult(3, "needs_signin\n")
        self.gateway_probes = 0
        self.gateway = True
        return ins.RunResult(0, "50-kirocrew-gateway: started gateway pid 7\n")

    def is_signed_in(self) -> bool:
        return self.signed_in

    def _gateway_probe(self, command, stdin):
        self.probes += 1
        up = self.probes >= self.gateway_up_after + 1 if self.gateway_up_after else True
        return ins.RunResult(0, "gateway=up\n" if up else "gateway=down\n")

    def _crew_alive(self, command, stdin):
        return ins.RunResult(0, (ins.CREW_UP if self.gateway else ins.CREW_DOWN) + "\n")


def no_sleep(_):
    pass


def path_a(runner, host, **kw):
    """Crew's install with the configured image openssl, as the engine runs it."""
    return ins.install_path_a(runner, host, openssl=IMAGE_OPENSSL, **kw)


def install_and_start(runner, host, *, sleep=no_sleep):
    """The engine's install order without the engine: Crew's install, then the hook written and run."""
    a = path_a(runner, host, sleep=sleep)
    out = ins.Outcome(install=a, lines=[a.line()])
    if a.status in ("blocked", "failed"):
        return out  # reported as is
    ins.write_hook(runner, host, sleep=sleep)
    out.hook = ins.run_hook(runner, host, sleep=sleep)
    out.lines.append(out.hook.line())
    return out


# ---- commands.md ----------------------------------------------------------------------------


def _fence_lines(text: str) -> set[str]:
    return {ln for blk in re.findall(r"```\n(.*?)\n```", text, re.S) for ln in blk.split("\n")}


def test_verification_reads_are_never_run_by_the_install():
    box = FakeBox()
    install_and_start(box, HOST, sleep=no_sleep)
    sent = {cmd for _, cmd, _ in box.calls}
    assert not sent & {cmd for _, cmd in ins.COMMANDS_C.values()}


# ---- Path A: install once, no-op the second time ---------------------------------------------


def test_public_installs_once_on_a_clean_box_then_noops():
    box = FakeBox()
    first = path_a(box, HOST)
    assert first.status == "installed" and first.path == "A"
    assert "python_source=pyenv" in first.line() and "image_pipx=broken" in first.line()
    assert box.ids().count("install-public") == 1
    second = path_a(box, HOST)
    assert second.status == "already"
    assert box.ids().count("install-public") == 1


def test_public_only_cli_sh_own_link_is_not_already_installed():
    """cli.sh's /root/.local/bin/kirocrew answers ``command -v kirocrew`` with no launcher on
    /projects (and a restart loses it): the install step runs and writes the launcher."""
    box = FakeBox()
    box.tools |= {"kirocrew", "kiro-cli"}
    r = path_a(box, HOST)
    assert box.ids().count("install-public") == 1
    assert r.status == "installed" and "crew-wrapper" in box.tools


# ---- LF rows: what a sandbox cannot do is reported, never patched ------------------------------


def test_lf6_lf8_public_download_failure_is_reported():
    box = FakeBox(install_rc=6)
    r = path_a(box, HOST)
    assert (r.status, r.why) == ("failed", ins.WHY_INSTALL_FAILED)
    assert "exit 6" in r.detail and "tail=" in r.detail


def test_installed_nothing_on_path_is_failed_not_installed():
    box = FakeBox(installs_kirocrew=False)
    r = path_a(box, HOST)
    assert (r.status, r.why) == ("failed", ins.WHY_INSTALL_FAILED) and ins.WRAPPER in r.detail


def test_pipx_on_base_path_is_blocked_with_its_lf_and_runs_no_hook():
    box = FakeBox(step_stop="pipx_on_base_path")
    out = install_and_start(box, HOST, sleep=no_sleep)
    assert (out.install.status, out.install.why) == ("blocked", ins.WHY_PIPX_ON_BASE_PATH)
    assert "reason=pipx_on_base_path" in out.install.line()
    assert out.hook is None and "hook-write" not in box.ids()


def test_ladder_mismatch_is_blocked_by_name():
    box = FakeBox(step_stop="ladder_mismatch")
    r = path_a(box, HOST)
    assert r.status == "blocked" and "reason=ladder_mismatch" in r.detail


def _hook_box(**kw):
    box = FakeBox(**kw)
    out = install_and_start(box, HOST, sleep=no_sleep)
    return box, out


def test_hook_content_root_owned_0755_and_sandbox_root():
    box, out = _hook_box()
    f = box.files[ins.HOOK]
    assert ins.HOOK == "/projects/.sandbox/boot.d/50-kirocrew-gateway"
    assert (f["owner"], f["mode"]) == ("root", 0o755)
    text = f["data"].decode()
    assert "export KIRO_CREW_SANDBOX_ROOT=/projects\n" in text
    assert "export XDG_DATA_HOME=/projects/.xdg-data\n" in text
    assert "exit 3" in text and f"setsid nohup {ins.WRAPPER} gateway" in text


def test_public_hook_starts_the_launcher_on_projects_by_absolute_path():
    text = ins.hook_script()
    assert f"setsid nohup {ins.WRAPPER} gateway --no-open --port {ins.GATEWAY_PORT} " in text
    assert f"\nexport PATH={ins.CREW_BIN}:{ins.PATH_PREFIX}:$PATH\n" in text  # Crew's bin dir first


def test_umask_077_still_ends_0755_root_for_dirs_and_hook():
    box, _ = _hook_box(umask=0o077)
    for p in (ins.SANDBOX_DIR, ins.BOOT_D, ins.HOOK):
        assert (box.files[p]["owner"], box.files[p]["mode"]) == ("root", 0o755), p
    # recorded literally: chown before chmod, on the dirs and on the hook
    assert any(o.startswith("chmod 0755") and ins.BOOT_D in o for o in box.ops)
    assert any(o.startswith("chown root:root") and ins.HOOK_TMP in o for o in box.ops)


def test_hook_survives_stop_resume_rerun_is_idempotent():
    box, out = _hook_box()
    assert out.hook.state == "started" and out.hook.gateway == "up"
    box.tools |= set()  # Stop -> Resume keeps /projects: files stay, process is "alive" per pidfile
    again = install_and_start(box, HOST, sleep=no_sleep)
    assert again.install.status == "already"
    assert again.hook.state == "alive"
    assert box.files[ins.HOOK]["mode"] == 0o755


def test_gateway_down_first_probe_retries_and_is_not_failure():
    box, out = _hook_box(gateway_up_after=2)
    assert out.hook.gateway == "up"
    assert box.ids().count("gateway-probe") == 3


def test_gateway_never_up_reports_down_after_bounded_probes():
    box = FakeBox(gateway_up_after=10**6)
    sleeps = []
    out = install_and_start(box, HOST, sleep=sleeps.append)
    assert out.hook.gateway == "down" and box.ids().count("gateway-probe") == 6
    assert len(sleeps) == 5


def test_exit_3_maps_to_needs_signin_and_no_probe():
    box, out = _hook_box(signed_in=False)
    assert out.hook.state == "needs_signin"
    assert "gateway-probe" not in box.ids()


# ---- path= label ---------------------------------------------------------------------------------


def test_every_result_line_names_its_path():
    box, out = _hook_box()
    assert out.lines and all(re.search(r"\bpath=A\b", ln) for ln in out.lines)
    blocked = install_and_start(FakeBox(), HOST, sleep=no_sleep)
    assert all("path=A" in ln for ln in blocked.lines)


# ---- Path B ---------------------------------------------------------------------------------------


def test_texts_carry_no_secret_shapes_and_no_configured_value():
    text = ins.commands_text()
    # The hook's first command clears the AWS credential names; the configured ones come
    # from the settings, so the shipped text names only the fixed list.
    unset_line = "\nunset " + " ".join(ins.HOOK_UNSET_ENV) + "\n"
    assert text.count(unset_line) == 1
    text = text.replace(unset_line, "\n")
    for needle in ("Authorization", "Bearer", "device_code", "AKIA", "kiro-cli"):
        assert needle not in text
    # machine-config is shown with placeholders: no endpoint, Region or token path of its own
    assert "<runtime endpoint>" in text and "<token file>" in text


def test_the_hook_clears_the_configured_names_and_refuses_a_bad_one():
    text = ins.hook_script(unset=("EXAMPLE_IMAGE_TOKEN",))
    first = next(ln for ln in text.splitlines() if ln.startswith("unset "))
    assert first.split()[1:] == [*ins.HOOK_UNSET_ENV, "EXAMPLE_IMAGE_TOKEN"]
    for bad in ("A B", "1ABC", "X;rm", ""):
        with pytest.raises(ins.InstallError) as ei:
            ins.hook_script(unset=(bad,))
        assert ei.value.code == "bad_env_name"


def test_the_hook_never_runs_kiro_cli_and_refuses_off_the_bundle_route():
    text = ins.hook_script()
    assert "kiro-cli" not in text
    assert f'if [ "$route" != {ins.ROUTE_KAS_BUNDLE} ]; then' in text
    assert f"exit {ins.NOT_KAS_BUNDLE_EXIT}" in text and "kas whoami" in text


def _mc(**kw):
    from kiro_crew.managed_env.settings import MachineCell

    base = dict(
        cell=MachineCell(
            "https://runtime.example.test:443", "https://management.example.test", "zz-example-1"
        ),
        token_path="/run/example/access_token",
    )
    base.update(kw)
    return ins.MachineConfig(**base)


def test_machine_config_writes_the_bundle_first_and_machine_auth_last():
    keys = [k for k, _ in ins.machine_config_args(_mc())]
    assert keys == [
        "agent.kas_bundle",
        "agent.kas_endpoint",
        "agent.kas_control_plane_endpoint",
        "agent.kas_region",
        "agent.kas_token_path",
        "agent.kas_auth",
    ]
    full = ins.machine_config_args(
        _mc(token_env="EXAMPLE_TOKEN", env_drop=("A_ENDPOINT", "B_ENDPOINT"), registry_mode=True)
    )
    keys = [k for k, _ in full]
    assert keys[-3:] == ["agent.kas_machine_env_drop", ins.REGISTRY_MODE_KEY, "agent.kas_auth"]
    assert ("agent.kas_machine_token_env", "EXAMPLE_TOKEN") in full
    drop = dict(full)["agent.kas_machine_env_drop"]
    import base64
    import json as _json

    assert drop.startswith("--value-base64=")
    assert _json.loads(base64.b64decode(drop.split("=", 1)[1])) == ["A_ENDPOINT", "B_ENDPOINT"]


def test_machine_config_is_judged_by_its_done_line_and_tries_a_lone_255_again():
    box = FakeBox()
    box.answer_next(ins.CMD_MACHINE_CONFIG, 255, keep_output=False)
    ins.machine_config(box, HOST, _mc(), sleep=no_sleep)
    assert box.ids().count(ins.CMD_MACHINE_CONFIG) == 2
    assert ("agent.kas_auth", "machine") in box.machine_config
    refused = FakeBox(machine_config_rc=1)
    with pytest.raises(ins.InstallError) as ei:
        ins.machine_config(refused, HOST, _mc(), sleep=no_sleep)
    assert ei.value.code == "machine_config_failed"


@pytest.mark.parametrize(
    "script",
    [ins.hook_script(), ins.install_env_script(), ins.wrapper_script()],
)
def test_scripts_parse_as_posix_sh(script, tmp_path):
    f = tmp_path / "s.sh"
    f.write_text(script)
    assert subprocess.run(["sh", "-n", str(f)], capture_output=True).returncode == 0


def test_the_shipped_install_env_script_has_no_test_seam_left_in_it():
    text = ins.install_env_script()
    assert text == ins.install_env_script(prefix="", fetch="")
    # downloaded to a file with its own exit, then run (never `curl | sh`, whose status is sh's)
    assert f"  curl -fsSL {ins.PUBLIC_URL} >{ins.CLI_SH_FILE} || {{" in text
    assert f'  sh {ins.CLI_SH_FILE} "$@" </dev/null\n' in text
    assert "  _kc_install --system-python\nelse\n  _kc_install\nfi\n" in text
    assert "| sh" not in text
    assert "export PATH=/projects/.crew/bin:/usr/bin:/bin:/usr/sbin:/sbin\n" in text
    assert "/projects/.crew/bin/openssl" not in text  # no image openssl configured: none linked
    assert f"ln -sfn {IMAGE_OPENSSL} /projects/.crew/bin/openssl" in ins.install_env_script(
        openssl=IMAGE_OPENSSL
    )
    assert "export KIROCREW_HOME=/projects/.crew/home\n" in text
    assert "shims" not in text and "PYENV_VERSION" not in text and "readlink" not in text


def _stub(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _image_supervision(sandbox: Path) -> dict[str, tuple[bool, bool]]:
    """A model of the image's two pidfile rules (IMG ``cd12378``
    ``src/big_weaver_execution_env/core/boot_hooks.py``): ``run/<name>.pid`` is supervised only
    when ``<name>`` is a hook in ``boot.d`` (``:522-527``, ``:626-628``), and its daemon is alive
    iff ``/proc/<pid>/cmdline``, NULs read as spaces, contains line 2 (``:607-625``,
    ``:759-769``). name -> (supervised, alive)."""
    seen = {}
    for entry in sorted(os.listdir(sandbox / "run")):
        if not entry.endswith(".pid"):
            continue
        name = entry[: -len(".pid")]
        lines = (sandbox / "run" / entry).read_text().splitlines()
        pid, token = int(lines[0].strip()), lines[1].strip()
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                alive = token in fh.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            alive = False
        seen[name] = ((sandbox / "boot.d" / name).is_file(), alive)
    return seen


def test_pidfile_is_named_for_the_hook():
    assert ins.PIDFILE == f"{ins.SANDBOX_DIR}/run/{ins.HOOK_NAME}.pid"
    assert ins.PIDFILE == "/projects/.sandbox/run/50-kirocrew-gateway.pid"
    text = ins.hook_script()
    assert f"\nPIDFILE={ins.PIDFILE}\n" in text and "/run/gateway.pid" not in text
    assert f"\nTOKEN='{ins.GATEWAY_TOKEN}'\n" in text


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc (Linux only)")
def test_hook_runs_under_sh_exit_4_exit_3_started_alive_and_the_image_supervises_it(tmp_path):
    root = tmp_path / "proj"
    boot_d = root / ".sandbox" / "boot.d"
    boot_d.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    hook = boot_d / ins.HOOK_NAME  # where hook-write puts it, so the model finds it in boot.d
    hook.write_text(ins.hook_script(root=str(root)))
    counter = tmp_path / "n"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    run_dir = root / ".sandbox" / "run"
    pidfile = run_dir / f"{ins.HOOK_NAME}.pid"

    def run():
        p = subprocess.run(
            ["sh", str(hook)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        return p.returncode, p.stdout

    # no launcher on /projects: exit 5, nothing started, never "started"
    rc, out = run()
    assert rc == ins.LAUNCHER_FAILED_EXIT and "launcher_failed" in out
    assert "started gateway" not in out
    assert not pidfile.exists()

    # the launcher runs and its route is not the KAS bundle route (no route answer); it just
    # sleeps as the gateway (a shell: its cmdline carries the token)
    _stub(
        root / ".crew" / "bin" / "kirocrew",
        'case "$1" in --version) exit 0 ;; kas) exit 2 ;; esac\nsleep 10\n',
    )
    _stub(bindir / "kirocrew", "exit 99\n")  # a kirocrew on PATH must not be the one started
    # off the bundle route: exit 4, never "sign in", and no kiro-cli is asked
    rc, out = run()
    assert rc == ins.NOT_KAS_BUNDLE_EXIT and "not_kas_bundle" in out and "needs_signin" not in out
    assert not pidfile.exists()

    # on the bundle route; `kas whoami` exits 1 on the first call, 0 after
    _stub(
        root / ".crew" / "bin" / "kirocrew",
        'case "$1 $2" in\n'
        '"--version ") exit 0 ;;\n'
        '"kas route") echo kas-bundle; exit 0 ;;\n'
        f'"kas whoami") n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) >{counter}; '
        '[ "$n" -ge 1 ]; exit $? ;;\n'
        "esac\nsleep 10\n",
    )
    pid = None
    try:
        rc, out = run()
        assert rc == 3 and "needs_signin" in out
        assert not pidfile.exists()

        rc, out = run()
        assert rc == 0 and "started gateway pid" in out
        pid_s, token = pidfile.read_text().splitlines()
        pid = int(pid_s)
        assert token == ins.GATEWAY_TOKEN
        assert not (run_dir / "gateway.pid").exists()
        assert os.listdir(run_dir) == [f"{ins.HOOK_NAME}.pid"]
        deadline = time.monotonic() + 5
        while _image_supervision(root / ".sandbox") != {ins.HOOK_NAME: (True, True)}:
            assert time.monotonic() < deadline, _image_supervision(root / ".sandbox")
            time.sleep(0.05)

        rc, out = run()  # idempotent: the live gateway is recognised by its cmdline
        assert rc == 0 and "gateway alive" in out and f"pid {pid}" in out

        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _image_supervision(root / ".sandbox") != {ins.HOOK_NAME: (True, False)}:
            assert time.monotonic() < deadline, _image_supervision(root / ".sandbox")
            time.sleep(0.05)
        pid = None
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_the_old_pidfile_name_is_not_supervised_by_the_model(tmp_path):
    """The model's control: a pidfile named for no hook in boot.d is never supervised."""
    sandbox = tmp_path / ".sandbox"
    (sandbox / "boot.d").mkdir(parents=True)
    (sandbox / "run").mkdir()
    (sandbox / "boot.d" / ins.HOOK_NAME).write_text("#!/bin/sh\n")
    (sandbox / "run" / "gateway.pid").write_text(f"{os.getpid()}\npython\n")
    assert _image_supervision(sandbox) == {"gateway": (False, True)}


# ---- the install env on a fake image tree, under real sh -------------------------------------

#: Host tools the fake image's base dirs link to (a real image has them in /usr/bin).
HOST_TOOLS = (
    "sh",
    "cat",
    "timeout",
    "sort",
    "ln",
    "mkdir",
    "chmod",
    "mv",
    "rm",
    "sleep",
    "readlink",
    "tr",
    "grep",
    "sed",
    "head",
    "tail",
    "dirname",
    "env",
    "setsid",
    "nohup",
)

FAKE_CLI = r"""#!/bin/sh
# fake one-liner (tests only), modelled on cli.sh cca9aa36: _resolve_python (:654-659) under
# --system-python; the pipx branch whenever `command -v pipx` answers (:1043); else the venv at
# ${KIROCREW_HOME:-$HOME/.kiro/crew}-venv (:1142-1145), its -current link (:1305) and
# $HOME/.local/bin/kirocrew (:1328-1330). Exits with the code the test sets.
REC=@R@/cli.log
MP=""
for a in "$@"; do case "$a" in --system-python) MP=0 ;; --managed-python) MP=1 ;; esac; done
echo "ran=1" >>"$REC"
echo "args=$*" >>"$REC"
echo "kirocrew_home=${KIROCREW_HOME-unset}" >>"$REC"
echo "openssl=$(command -v openssl 2>/dev/null)" >>"$REC"
if [ "$MP" = 0 ]; then
  PY=""
  for c in python3.12 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && timeout 5 "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then PY="$c"; break; fi
  done
  echo "ladder=$(command -v "$PY" 2>/dev/null)" >>"$REC"
fi
if command -v pipx >/dev/null 2>&1; then
  echo "branch=pipx" >>"$REC"
  pipx install --force kirocrew.whl </dev/null
  exit @CODE@
fi
echo "branch=venv" >>"$REC"
D="${KIROCREW_HOME:-$HOME/.kiro/crew}"
VENV="${D%/}-venv"
echo "venv=$VENV" >>"$REC"
echo "Installing into managed venv at $VENV ..."
if [ @CODE@ != 0 ] && [ @CODE@ != 3 ]; then echo "ERROR: installing the wheel failed" >&2; exit @CODE@; fi
mkdir -p "$VENV/bin" "$HOME/.local/bin"
cat >"$VENV/bin/kirocrew" <<'EOF'
#!/bin/sh
echo "argv=$* home=${KIROCREW_HOME-unset} xdg=${XDG_DATA_HOME-unset}" >>@R@/kirocrew.log
[ "$1" = --version ] && echo "kirocrew 0.0.0-test"
exit 0
EOF
chmod 0755 "$VENV/bin/kirocrew"
ln -sfn "$VENV" "${VENV%/}-current"
ln -sf "$VENV/bin/kirocrew" "$HOME/.local/bin/kirocrew"
[ @CODE@ = 3 ] && echo "Kiro Crew is installed, but this host cannot run the built-in KAS engine" >&2
exit @CODE@
"""


def _py_body(version, venv=True, broken=False):
    """A stand-in interpreter: answers the lane's run check (it imports ``ensurepip``) and
    cli.sh's version-only check by exit code."""
    if broken:
        return 'echo "Fatal Python error: failed to get the Python codec" >&2\nexit 1\n'
    ge = tuple(version) >= (3, 12)
    v = ".".join(map(str, version))
    return (
        'case "$*" in\n'
        f"  *ensurepip*) exit {0 if ge and venv else 1} ;;\n"
        f"  *version_info*) exit {0 if ge else 1} ;;\n"
        f'  --version) echo "Python {v}"; exit 0 ;;\n'
        "esac\nexit 2\n"
    )


@dataclass
class Step:
    rc: int
    out: str
    lines: dict
    cli: dict  # what the fake one-liner recorded (empty: it never ran)
    pipx_calls: list
    stdout: str


class FakeImage:
    """A temp dir standing for the image's ``/``. ``root/.pyenv`` is an image
    link into an inner tree (as ``/root/.local`` is on the real image); ``usr/bin/python3`` is
    3.9; ``root/.pyenv/shims/python3.12`` exits 127 unless ``PYENV_VERSION`` lets 3.12 through;
    ``chown`` records (the box runs as root)."""

    def __init__(self, base: Path):
        self.r = base / "img"
        for d in (
            "usr/bin",
            "bin",
            "usr/sbin",
            "sbin",
            "usr/local/bin",
            "projects",
            "root/.local/bin",
            "inner/pyenv/shims",
            "inner/pyenv/versions",
        ):
            (self.r / d).mkdir(parents=True, exist_ok=True)
        (self.r / "root" / ".pyenv").symlink_to(self.r / "inner" / "pyenv")
        for t in HOST_TOOLS:
            src = shutil.which(t, path="/usr/bin:/bin")
            assert src, t
            (self.r / "usr" / "bin" / t).symlink_to(src)
        _stub(self.r / "usr" / "bin" / "chown", f'echo "chown $*" >>{self.r}/chown.log\n')
        self.python("/usr/bin/python3", (3, 9, 25))
        self.python("/usr/bin/python3.9", (3, 9, 25))
        self.shim("3.12")

    def p(self, path: str) -> str:
        return str(self.r) + path

    def python(self, path, version=(3, 12, 13), *, venv=True, broken=False):
        _stub(Path(self.p(path)), _py_body(version, venv=venv, broken=broken))

    def shim(self, minor):
        _stub(
            self.r / "inner" / "pyenv" / "shims" / f"python{minor}",
            f'case ":${{PYENV_VERSION:-}}:" in *{minor}*) ;; *)\n'
            f'  echo "pyenv: python{minor}: command not found" >&2; exit 127 ;; esac\n'
            + _py_body(tuple(int(x) for x in minor.split(".")) + (13,)),
        )

    def pipx(self, path=ins.IMAGE_PIPX, *, works=False):
        body = f'echo "$*" >>{self.r}/pipx.log\n'
        if works:
            body += '[ "$1" = --version ] && echo 1.7.1\nexit 0\n'
        else:  # the image's pipx today: shebang /usr/bin/python3 (3.9), the module is not there
            body += "echo \"ModuleNotFoundError: No module named 'pipx'\" >&2\nexit 1\n"
        _stub(Path(self.p(path)), body)

    def inherited_path(self) -> str:
        """The image PATH's order: pyenv shims, ~/.local/bin, /usr/local/bin, then the base dirs."""
        dirs = ("/root/.pyenv/shims", "/root/.local/bin", "/usr/local/bin", "/usr/bin", "/bin")
        return ":".join(self.p(d) for d in dirs)

    def script(self, code=0, fetch=None) -> str:
        cli = self.r / "fake-cli.sh"
        cli.write_text(FAKE_CLI.replace("@R@", str(self.r)).replace("@CODE@", str(code)))
        return ins.install_env_script(
            prefix=str(self.r), fetch=fetch or f"cat {cli}", openssl=IMAGE_OPENSSL
        )

    def run(self, code=0, *, pyenv_version=None, umask=0o022, fetch=None) -> Step:
        for f in ("cli.log", "pipx.log"):
            (self.r / f).unlink(missing_ok=True)
        env = {"PATH": self.inherited_path(), "HOME": self.p("/root")}
        if pyenv_version is not None:
            env["PYENV_VERSION"] = pyenv_version
        p = subprocess.run(
            ["/bin/sh", "-s"],
            input=self.script(code, fetch),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            umask=umask,
        )
        cli = {}
        if (self.r / "cli.log").exists():
            for ln in (self.r / "cli.log").read_text().splitlines():
                k, _, v = ln.partition("=")
                cli[k] = v
        pipx = (
            (self.r / "pipx.log").read_text().splitlines() if (self.r / "pipx.log").exists() else []
        )
        out = p.stdout + p.stderr
        self.last = Step(p.returncode, out, ins.parse_step(p.stdout), cli, pipx, p.stdout)
        return self.last

    def link(self, minor) -> str:
        return os.readlink(self.p(f"{ins.CREW_BIN}/python{minor}"))


@pytest.fixture
def img(tmp_path):
    return FakeImage(tmp_path)


def _pyenv(minor, patch):
    return f"/root/.pyenv/versions/{minor}.{patch}/bin/python{minor}"


def _assert_venv_install_on_projects(img, st, minor="3.12"):
    assert st.cli.get("ran") == "1", st.out
    assert st.cli["branch"] == "venv"  # not pipx
    assert st.cli["args"] == "--system-python"
    assert st.cli["kirocrew_home"] == img.p(ins.CREW_HOME)
    assert st.cli["venv"] == img.p(ins.CREW_VENV)
    assert st.cli["ladder"] == img.p(f"{ins.CREW_BIN}/python{minor}")
    assert (st.lines["pipx"], st.lines["place"]) == ("absent", img.p(ins.CREW_HOME))
    assert (st.lines["install_rc"], st.lines["wrapper"]) == ("0", "ok")
    assert st.rc == 0


def test_broken_image_pipx_and_pyenv_312_installs_into_the_venv_on_projects(img):
    img.python(_pyenv("3.12", 13))
    img.pipx(works=False)
    st = img.run()
    _assert_venv_install_on_projects(img, st)
    assert (st.lines["python_source"], st.lines["image_pipx"]) == ("pyenv", "broken")
    assert st.lines["python"] == img.p(_pyenv("3.12", 13))
    assert img.link("3.12") == img.p(_pyenv("3.12", 13))
    assert st.pipx_calls == ["--version"]  # the record read only: never run for the install
    facts = [f"{k}={st.lines[k]}" for k in ins.STEP_FACTS]
    assert st.stdout.splitlines()[: len(facts)] == facts


def test_working_image_pipx_is_recorded_and_never_installs(img):
    img.python(_pyenv("3.12", 13))
    img.pipx(works=True)
    st = img.run()
    _assert_venv_install_on_projects(img, st)
    assert st.lines["image_pipx"] == "works"
    assert not any(c.startswith(("install", "environment")) for c in st.pipx_calls)


def test_no_image_pipx(img):
    img.python(_pyenv("3.12", 13))
    st = img.run()
    _assert_venv_install_on_projects(img, st)
    assert st.lines["image_pipx"] == "absent" and st.pipx_calls == []


@pytest.mark.parametrize("works", [False, True])
def test_pipx_on_a_base_dir_stops_before_the_installer(img, works):
    img.python(_pyenv("3.12", 13))
    img.pipx("/usr/bin/pipx", works=works)
    st = img.run()
    assert st.lines["stop"] == "pipx_on_base_path" and st.lines["pipx"] == img.p("/usr/bin/pipx")
    assert st.cli == {} and st.rc != 0  # the one-liner never ran
    assert "install_rc" not in st.lines
    assert not any(c.startswith("install") for c in st.pipx_calls)
    box = ShellBox(img)
    r = path_a(box, HOST)
    assert (r.status, r.why) == (
        "blocked",
        ins.WHY_PIPX_ON_BASE_PATH,
    ) and "reason=pipx_on_base_path" in r.line()


def test_two_312_patches_take_the_newest_that_runs(img):
    img.python(_pyenv("3.12", 9))
    img.python(_pyenv("3.12", 13))  # 13 > 9 as a number, < as a string
    st = img.run()
    assert st.lines["python"] == img.p(_pyenv("3.12", 13))
    img.python(_pyenv("3.12", 13), broken=True)  # exit 1 on any run
    st = img.run()
    assert st.lines["python"] == img.p(_pyenv("3.12", 9)) and img.link("3.12") == img.p(
        _pyenv("3.12", 9)
    )
    _assert_venv_install_on_projects(img, st)


def test_no_real_path_rung_at_all_is_managed(img):
    st = img.run()
    assert (st.lines["python_source"], st.lines["python"]) == ("managed", "none")
    assert st.cli["args"] == "" and "ladder" not in st.lines
    assert not os.path.lexists(img.p(f"{ins.CREW_BIN}/python3.12"))


def test_only_a_system_312_is_system(img):
    img.python("/usr/bin/python3.12")
    st = img.run()
    assert (st.lines["python_source"], st.lines["python"]) == (
        "system",
        img.p("/usr/bin/python3.12"),
    )
    assert img.link("3.12") == img.p("/usr/bin/python3.12")
    _assert_venv_install_on_projects(img, st)


def test_no_312_anywhere_and_pyenv_313_is_taken_by_the_ladder_through_the_link(img):
    img.python(_pyenv("3.13", 13), (3, 13, 13))
    st = img.run()
    assert (st.lines["python_source"], st.lines["python"]) == ("pyenv", img.p(_pyenv("3.13", 13)))
    assert img.link("3.13") == img.p(_pyenv("3.13", 13))
    assert not os.path.lexists(img.p(f"{ins.CREW_BIN}/python3.12"))
    _assert_venv_install_on_projects(img, st, minor="3.13")


def test_the_link_names_the_path_as_globbed_not_its_real_path(img):
    img.python(_pyenv("3.12", 13))
    img.run()
    globbed = img.p(_pyenv("3.12", 13))
    assert img.link("3.12") == globbed
    assert os.path.realpath(globbed) != globbed  # the fake root/.pyenv is itself a link
    assert img.link("3.12") != os.path.realpath(globbed)


def test_a_system_312_without_ensurepip_ahead_of_a_chosen_313_stops_ladder_mismatch(img):
    img.python("/usr/bin/python3.12", venv=False)  # >= 3.12, but no venv/ensurepip
    img.python(_pyenv("3.13", 13), (3, 13, 13))
    st = img.run()
    assert st.lines["python"] == img.p(_pyenv("3.13", 13))
    assert st.lines["ladder"] == img.p("/usr/bin/python3.12")
    assert st.lines["stop"] == "ladder_mismatch" and st.cli == {}  # the one-liner never ran
    box = ShellBox(img)
    r = path_a(box, HOST)
    assert r.status == "blocked" and "reason=ladder_mismatch" in r.detail


def test_nothing_usable_is_managed_without_system_python_and_names_its_needs(img):
    img.python(_pyenv("3.12", 13), broken=True)
    img.python("/usr/bin/python3.12", venv=False)
    img.python(_pyenv("3.13", 13), (3, 13, 13), broken=True)
    img.python("/usr/bin/python3.13", (3, 13, 1), venv=False)
    box = ShellBox(img)
    r = path_a(box, HOST)
    st = img.last
    assert st.lines["python_source"] == "managed" and st.cli["args"] == ""  # no --system-python
    assert "python_source=managed" in r.line() and r.status == "installed"
    assert "managed_needs=github.com,python-build-standalone" in r.line()


def test_a_shim_that_pyenv_version_lets_through_is_never_chosen(img):
    st = img.run(pyenv_version="3.12")  # only the shim answers for 3.12
    assert st.lines["python_source"] == "managed"
    assert "shims" not in st.out


@pytest.mark.parametrize("pyenv_version", [None, "3.11", "3.12"])
def test_pyenv_version_does_not_change_the_choice(img, pyenv_version):
    img.python(_pyenv("3.12", 13))
    st = img.run(pyenv_version=pyenv_version)
    assert (st.lines["python_source"], st.lines["python"]) == ("pyenv", img.p(_pyenv("3.12", 13)))
    _assert_venv_install_on_projects(img, st)


def test_a_real_interpreter_passes_the_run_check(img):
    import sys

    if sys.version_info < (3, 12):
        pytest.skip("needs a >= 3.12 interpreter to link")
    real = Path(img.p(_pyenv("3.12", 99)))
    real.parent.mkdir(parents=True)
    # A real CPython that runs through a link (a launcher script that finds its binary from $0
    # does not): this interpreter, else a python3.12 on the host PATH.
    tried = []
    for cand in (
        sys.executable,
        os.path.realpath(sys.executable),
        shutil.which("python3.12") or "",
    ):
        if not cand or cand in tried:
            continue
        tried.append(cand)
        real.unlink(missing_ok=True)
        real.symlink_to(cand)
        check = [str(real), "-c", "import sys, venv, ensurepip; assert sys.version_info >= (3, 12)"]
        if (
            subprocess.run(check, env={"PATH": "/usr/bin:/bin"}, capture_output=True).returncode
            == 0
        ):
            break
    else:
        pytest.skip(f"no >= 3.12 interpreter with venv/ensurepip runs through a link: {tried}")
    st = img.run()
    assert st.lines["python"] == str(real)
    _assert_venv_install_on_projects(img, st)


def test_the_launcher_exports_crew_home_and_execs_the_current_venv_root_0755_any_umask(img):
    img.python(_pyenv("3.12", 13))
    st = img.run(umask=0o077)
    assert st.lines["wrapper"] == "ok"
    w = Path(img.p(ins.WRAPPER))
    assert w.stat().st_mode & 0o7777 == 0o755
    assert (
        f"chown root:root {img.p(ins.CREW_BIN)}/.kirocrew.tmp" in (img.r / "chown.log").read_text()
    )
    text = w.read_text()
    assert text == ins.wrapper_script(prefix=str(img.r))
    assert f"export KIROCREW_HOME={img.p(ins.CREW_HOME)}\n" in text
    assert f"export XDG_DATA_HOME={img.p(ins.XDG_DATA)}\n" in text
    assert text.rstrip("\n").endswith(f'exec {img.p(ins.CREW_VENV_CURRENT)}/bin/kirocrew "$@"')
    (img.r / "kirocrew.log").unlink()
    p = subprocess.run(
        [str(w), "--version"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert p.returncode == 0 and "kirocrew 0.0.0-test" in p.stdout
    seen = (img.r / "kirocrew.log").read_text()
    assert f"home={img.p(ins.CREW_HOME)} xdg={img.p(ins.XDG_DATA)}" in seen
    assert ins.wrapper_script().startswith("#!/bin/sh\n")
    assert "export KIROCREW_HOME=/projects/.crew/home\n" in ins.wrapper_script()


# ---- the Python side over the real script (ShellBox) ----------------------------------------


class ShellBox(FakeBox):
    """FakeBox whose ``install-public`` runs the shipped install-env script under ``sh`` on a
    FakeImage (through the test seam), and whose ``have:crew-wrapper`` is the launcher's own
    ``--version`` run there."""

    def __init__(self, image: FakeImage, *, code=0, fetch=None, **kw):
        super().__init__(**kw)
        self.img, self.code, self.fetch = image, code, fetch

    def _have(self):
        have = {t for t in ("kirocrew", ins.HAVE_KAS_BUNDLE) if t in self.tools} | {
            ins.HAVE_KAS_BUNDLE
        }
        w = self.img.p(ins.WRAPPER)
        if os.access(w, os.X_OK):
            p = subprocess.run(
                [w, "--version"],
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=ins.WRAPPER_CHECK_SECS,
            )
            if p.returncode == 0:
                have.add("crew-wrapper")
        return have

    def _install_public(self, command, stdin):
        assert stdin == ins.install_env_script(openssl=IMAGE_OPENSSL).encode()  # the shipped text
        st = self.img.run(self.code, fetch=self.fetch)
        return ins.RunResult(st.rc, st.out)


@pytest.fixture
def pyenv_img(img):
    img.python(_pyenv("3.12", 13))
    img.pipx(works=False)
    return img


def test_installer_exit_3_writes_the_launcher_reports_kas_unavailable_and_the_hook_follows(
    pyenv_img,
):
    box = ShellBox(pyenv_img, code=3)
    out = install_and_start(box, HOST, sleep=no_sleep)
    assert os.access(pyenv_img.p(ins.WRAPPER), os.X_OK)
    assert out.install.status == "installed" and out.install.why == ins.WHY_KAS_UNAVAILABLE
    assert "kas=unavailable" in out.install.line() and "install_rc" not in out.install.line()
    assert "hook-write" in box.ids() and "hook-run" in box.ids() and out.hook.state == "started"


def test_installer_exit_1_is_lf6_with_the_tail_no_launcher_no_hook(pyenv_img):
    box = ShellBox(pyenv_img, code=1)
    out = install_and_start(box, HOST, sleep=no_sleep)
    assert (out.install.status, out.install.why) == ("failed", ins.WHY_INSTALL_FAILED)
    assert "install command exit 1" in out.install.detail
    assert (
        "tail=" in out.install.detail and "ERROR: installing the wheel failed" in out.install.detail
    )
    assert not os.path.lexists(pyenv_img.p(ins.WRAPPER))
    assert out.hook is None and "hook-write" not in box.ids()


def test_only_cli_sh_own_link_present_runs_the_install_and_writes_the_launcher(pyenv_img):
    _stub(Path(pyenv_img.p("/root/.local/bin/kirocrew")), "echo kirocrew 0.0.0\n")
    box = ShellBox(pyenv_img)
    box.tools |= {"kirocrew", "kiro-cli"}  # `command -v kirocrew` and kiro-cli answer, no launcher
    r = path_a(box, HOST)
    assert box.ids().count("install-public") == 1
    assert r.status == "installed" and os.access(pyenv_img.p(ins.WRAPPER), os.X_OK)


def test_a_launcher_whose_venv_is_gone_is_not_already_and_installs_again(pyenv_img):
    box = ShellBox(pyenv_img)
    assert path_a(box, HOST).status == "installed"
    shutil.rmtree(pyenv_img.p(ins.CREW_VENV))  # its interpreter and launcher gone; the link dangles
    assert "crew-wrapper" not in box._have()
    assert path_a(box, HOST).status == "installed"
    assert box.ids().count("install-public") == 2


def test_a_launcher_that_runs_is_already(pyenv_img):
    box = ShellBox(pyenv_img)
    assert path_a(box, HOST).status == "installed"
    assert path_a(box, HOST).status == "already"
    assert box.ids().count("install-public") == 1


def test_the_public_result_line_carries_the_step_facts(pyenv_img):
    box = ShellBox(pyenv_img)
    line = path_a(box, HOST).line()
    for want in (
        "status=installed",
        "python_source=pyenv",
        f"python={pyenv_img.p(_pyenv('3.12', 13))}",
        "pipx=absent",
        "image_pipx=broken",
        f"place={pyenv_img.p(ins.CREW_HOME)}",
    ):
        assert want in line


def test_parse_step_reads_the_leading_block_and_the_last_post_lines():
    out = (
        "python_source=pyenv\npython=/p\npipx=absent\nimage_pipx=broken\nplace=/h\nladder=/l\n"
        "Installing into managed venv at /h-venv ...\npython=/not-ours\nstop=not-ours\n"
        "install_rc=0\nwrapper=ok\n"
    )
    d = ins.parse_step(out)
    assert (d["python"], d["ladder"], "stop" in d) == ("/p", "/l", False)
    assert (d["install_rc"], d["wrapper"]) == ("0", "ok")


# ---- the local bed's findings on the pool image: the download's own exit, the launcher check,
# ---- openssl, and the KAS bundle route --------------------------------------------------------

#: A fetch that fails the way curl -fsSL does on a proxy's or host's 403.
CURL_403 = "sh -c 'echo \"curl: (22) The requested URL returned error: 403\" >&2; exit 22'"


@pytest.mark.parametrize("fetch,rc", [("false", "1"), (CURL_403, "22")])
def test_a_failed_download_is_its_own_exit_and_lf15_and_leaves_no_launcher(pyenv_img, fetch, rc):
    st = pyenv_img.run(fetch=fetch)
    assert st.rc == 25 and (st.lines.get("stop"), st.lines.get("fetch_rc")) == ("fetch_failed", rc)
    assert "install_rc" not in st.lines and st.cli == {}  # the installer never ran
    for path in (ins.WRAPPER, ins.CLI_SH_FILE, f"{ins.CREW_BIN}/.kirocrew.tmp"):
        assert not os.path.lexists(pyenv_img.p(path)), path
    box = ShellBox(pyenv_img, fetch=fetch)
    r = path_a(box, HOST)
    assert (r.status, r.why) == ("failed", ins.WHY_FETCH_FAILED)
    assert f"reason=fetch_failed fetch_rc={rc}" in r.detail
    if rc == "22":
        assert "returned error: 403" in r.detail  # curl's own line, in the tail
    assert box.ids() == ["probe", "install-public"]  # no second probe, no hook


def test_an_installer_that_fails_after_a_good_download_keeps_its_exit(pyenv_img):
    st = pyenv_img.run(fetch="printf 'exit 7\\n'")
    assert (st.rc, st.lines.get("install_rc"), "stop" in st.lines) == (23, "7", False)
    r = path_a(ShellBox(pyenv_img, fetch="printf 'exit 7\\n'"), HOST)
    assert (r.status, r.why) == (
        "failed",
        ins.WHY_INSTALL_FAILED,
    ) and "install command exit 7" in r.detail


def test_an_install_that_exits_0_but_left_no_venv_leaves_no_launcher(pyenv_img):
    """The launcher is checked on its temp file before the move, so a failed install never leaves
    a launcher that does not run (the probe's have:crew-wrapper would then never be true)."""
    st = pyenv_img.run(fetch="printf 'exit 0\\n'")
    assert (st.rc, st.lines.get("install_rc"), st.lines.get("wrapper")) == (24, "0", "failed")
    assert not os.path.lexists(pyenv_img.p(ins.WRAPPER))
    assert not os.path.lexists(pyenv_img.p(f"{ins.CREW_BIN}/.kirocrew.tmp"))


def _openssl(img, path, *, works=True):
    body = (
        '[ "$1" = version ] && echo "OpenSSL 1.1.1zj"\nexit 0\n'
        if works
        else 'echo "openssl: error while loading shared libraries" >&2\nexit 127\n'
    )
    _stub(Path(img.p(path)), body)


def test_no_openssl_on_the_path_links_the_images_own_and_the_installer_finds_it(pyenv_img):
    _openssl(pyenv_img, IMAGE_OPENSSL)
    r = path_a(ShellBox(pyenv_img), HOST)
    st = pyenv_img.last
    link = pyenv_img.p(f"{ins.CREW_BIN}/openssl")
    assert st.lines["openssl"] == pyenv_img.p(IMAGE_OPENSSL)
    assert os.readlink(link) == pyenv_img.p(IMAGE_OPENSSL)
    assert st.cli["openssl"] == link  # the one-liner's `command -v openssl`
    assert (st.lines["install_rc"], st.lines["wrapper"]) == ("0", "ok")
    assert r.status == "installed" and f"openssl={pyenv_img.p(IMAGE_OPENSSL)}" in r.line()


def test_an_openssl_on_the_base_path_is_used_and_nothing_is_linked(pyenv_img):
    _openssl(pyenv_img, "/usr/bin/openssl")
    _openssl(pyenv_img, IMAGE_OPENSSL)
    st = pyenv_img.run()
    assert st.lines["openssl"] == st.cli["openssl"] == pyenv_img.p("/usr/bin/openssl")
    assert not os.path.lexists(pyenv_img.p(f"{ins.CREW_BIN}/openssl"))


@pytest.mark.parametrize("image_openssl", ["absent", "broken"])
def test_no_openssl_that_runs_is_none_and_nothing_is_linked(pyenv_img, image_openssl):
    if image_openssl == "broken":
        _openssl(pyenv_img, IMAGE_OPENSSL, works=False)
    st = pyenv_img.run()
    assert st.lines["openssl"] == "none" and st.cli["openssl"] == ""
    assert not os.path.lexists(pyenv_img.p(f"{ins.CREW_BIN}/openssl"))


def test_on_the_kas_bundle_route_crew_installs_without_kiro_cli_and_the_hook_starts():
    box = FakeBox(route=ins.ROUTE_KAS_BUNDLE)
    r = path_a(box, HOST)
    assert (r.status, r.why) == ("installed", "") and f"route={ins.ROUTE_KAS_BUNDLE}" in r.detail
    assert "kiro-cli" not in box.tools
    assert path_a(box, HOST).status == "already"
    ins.write_hook(box, HOST)
    assert ins.run_hook(box, HOST, sleep=no_sleep).state == "started"


@pytest.mark.parametrize("answer,have", [("kas-bundle", True), ("kiro-cli", False), ("", False)])
def test_the_probe_reads_the_launchers_kas_route(tmp_path, answer, have):
    """The shipped probe command under sh, /projects mapped into a temp dir."""
    root = tmp_path / "box"
    out = f"echo {answer}; exit 0" if answer else "exit 2"
    _stub(
        root / ins.WRAPPER.lstrip("/"),
        f'case "$1 $2" in\n"--version ") exit 0 ;;\n"kas route") {out} ;;\nesac\nexit 9\n',
    )
    cmd = ins.COMMANDS_A[ins.CMD_PROBE][1].replace(ins.ROOT, f"{root}{ins.ROOT}")
    p = subprocess.run(
        ["sh", "-c", cmd],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    lines = p.stdout.splitlines()
    assert p.returncode == 0 and lines[-1] == "probe=done" and "have:crew-wrapper" in lines
    assert ("have:" + ins.HAVE_KAS_BUNDLE in lines) is have


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc (Linux only)")
def test_the_hook_on_the_kas_bundle_route_needs_no_kiro_cli_and_gates_on_kas_whoami(tmp_path):
    root = tmp_path / "proj"
    hook = root / ".sandbox" / "boot.d" / ins.HOOK_NAME
    hook.parent.mkdir(parents=True)
    hook.write_text(ins.hook_script(root=str(root)))
    counter = tmp_path / "n"
    _stub(
        root / ".crew" / "bin" / "kirocrew",
        'case "$1 $2" in\n'
        '"--version ") exit 0 ;;\n'
        '"kas route") echo kas-bundle; exit 0 ;;\n'
        f'"kas whoami") n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) >{counter}; '
        '[ "$n" -ge 1 ]; exit $? ;;\n'
        "esac\nsleep 10\n",
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}  # no kiro-cli anywhere

    def run():
        p = subprocess.run(
            ["sh", str(hook)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60
        )
        return p.returncode, p.stdout

    rc, out = run()
    assert rc == ins.NEEDS_SIGNIN_EXIT and "kirocrew kas whoami failed" in out
    assert "kiro_cli_missing" not in out
    pidfile = root / ".sandbox" / "run" / f"{ins.HOOK_NAME}.pid"
    pid = None
    try:
        rc, out = run()
        assert rc == 0 and "started gateway pid" in out, out
        pid = int(pidfile.read_text().splitlines()[0])
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_a_hook_whose_launcher_does_not_run_is_a_failed_hook_not_started():
    box = FakeBox()
    ins.write_hook(box, HOST)  # the hook is there; Crew's launcher is not
    h = ins.run_hook(box, HOST, sleep=no_sleep)
    assert (h.state, h.gateway) == ("failed", "") and "launcher_failed" in h.detail
    assert "gateway-probe" not in box.ids()


# ---- a lone ssh exit 255 ------------------------------------------------------
#
# The box's ssh server can send a command's whole output and then exit 255 with no message. A
# 255 whose output holds a line the command does not print itself is a connect that failed with a
# reason, and is never run again: so a failed mint never costs a second StartSshSession.

#: The alias's ProxyCommand on a sandbox that is not RUNNING (``mint_and_attach._fail`` and
#: ``_http_reason``): one line on stderr, exit 255.
MINT_409 = (
    "mint-and-attach: the sandbox is not RUNNING (not ready); resume it first, "
    "this does not resume\n"
)
SSH_RESET = "ssh: connect to host: Connection reset\n"  # ssh's own message (LaunchBox's)
LOOP_TIMEOUT = "timeout\n"  # EXP-64's loop runner's answer for a subprocess.TimeoutExpired
CHECKED = {"kirocrew", "crew-wrapper", ins.HAVE_KAS_BUNDLE}


def _installed_box(**kw):
    box = FakeBox(**kw)
    box.tools |= CHECKED
    return box


def test_the_install_check_ends_in_its_done_line_and_the_bounds_are_named():
    assert ins.COMMANDS_A[ins.CMD_PROBE][1].endswith(f"; echo {ins.CHECK_DONE}")
    assert (ins.SSH_EXIT, ins.LONE_255_TRIES, ins.LONE_255_DELAY) == (255, 3, 3.0)


@pytest.mark.parametrize("rc", [255, 1])
def test_an_install_check_with_its_done_line_is_accepted_on_try_1_whatever_the_rc(rc):
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, rc)  # the race's shape: the full output, then the rc
    sleeps: list[float] = []
    assert ins.probe(box, HOST, sleep=sleeps.append) == CHECKED  # ``have`` as printed
    assert box.ids() == [ins.CMD_PROBE] and sleeps == []


def test_a_silent_255_on_the_install_check_is_tried_again_3_s_later():
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, keep_output=False)
    sleeps: list[float] = []
    assert ins.probe(box, HOST, sleep=sleeps.append) == CHECKED
    assert box.ids() == [ins.CMD_PROBE] * 2 and sleeps == [3.0]


def test_have_lines_with_a_255_and_no_done_line_are_the_checks_own_and_tried_again():
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, keep_output=False, extra="have:kirocrew\nhave:kiro-cli\n")
    assert ins.probe(box, HOST, sleep=no_sleep) == CHECKED
    assert box.ids() == [ins.CMD_PROBE] * 2


def test_a_lasting_silent_255_on_the_install_check_raises_after_exactly_3_tries():
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, times=3, keep_output=False)
    sleeps: list[float] = []
    with pytest.raises(ins.InstallError) as ei:
        ins.probe(box, HOST, sleep=sleeps.append)
    assert ei.value.code == "probe_failed"
    assert str(ei.value) == "the box did not answer the install check (ssh exit 255, 3 tries)"
    assert box.ids() == [ins.CMD_PROBE] * 3 and sleeps == [3.0, 3.0]


@pytest.mark.parametrize("line", [MINT_409, SSH_RESET, LOOP_TIMEOUT])
def test_an_install_check_255_with_a_reason_line_raises_after_one_exec(line):
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, keep_output=False, extra="have:kirocrew\n" + line)
    sleeps: list[float] = []
    with pytest.raises(ins.InstallError) as ei:
        ins.probe(box, HOST, sleep=sleeps.append)
    assert ei.value.code == "probe_failed" and "tries" not in str(ei.value)
    assert box.ids() == [ins.CMD_PROBE] and sleeps == []


@pytest.mark.parametrize("rc", [1, 0])
def test_an_install_check_without_its_done_line_and_not_255_raises_after_one_exec(rc):
    box = _installed_box()
    box.answer_next(ins.CMD_PROBE, rc, keep_output=False, extra="have:kirocrew\n")
    with pytest.raises(ins.InstallError) as ei:
        ins.probe(box, HOST, sleep=no_sleep)
    assert ei.value.code == "probe_failed" and box.ids() == [ins.CMD_PROBE]


def test_a_timeout_on_the_install_check_is_raised_at_once():
    class Hangs(FakeBox):
        def _probe(self, command, stdin):
            raise subprocess.TimeoutExpired(["ssh"], 900)

    box = Hangs()
    with pytest.raises(subprocess.TimeoutExpired):
        ins.probe(box, HOST, sleep=no_sleep)
    assert box.ids() == [ins.CMD_PROBE]


def test_the_install_passes_its_sleep_to_the_install_check():
    box = FakeBox()
    box.answer_next(ins.CMD_PROBE, keep_output=False)  # the first check on a clean box
    sleeps: list[float] = []
    r = path_a(box, HOST, sleep=sleeps.append)
    assert r.status == "installed" and sleeps == [3.0]
    assert box.ids() == [ins.CMD_PROBE, ins.CMD_PROBE, ins.CMD_INSTALL_PUBLIC, ins.CMD_PROBE]


def test_a_silent_255_on_hook_dirs_is_tried_again():
    box = FakeBox()
    box.answer_next(ins.CMD_HOOK_DIRS, keep_output=False)
    sleeps: list[float] = []
    ins.write_hook(box, HOST, sleep=sleeps.append)
    assert box.ids() == [ins.CMD_HOOK_DIRS] * 2 + [ins.CMD_HOOK_WRITE] and sleeps == [3.0]
    assert (box.files[ins.HOOK]["owner"], box.files[ins.HOOK]["mode"]) == ("root", 0o755)


def test_hook_dirs_255_with_a_mint_line_fails_after_one_exec():
    box = FakeBox()
    box.answer_next(ins.CMD_HOOK_DIRS, extra=MINT_409)
    with pytest.raises(ins.InstallError) as ei:
        ins.write_hook(box, HOST, sleep=no_sleep)
    assert ei.value.code == "hook_dirs_failed" and box.ids() == [ins.CMD_HOOK_DIRS]


def test_a_silent_255_on_hook_write_sends_the_same_bytes_again():
    box = FakeBox()
    box.answer_next(ins.CMD_HOOK_WRITE, keep_output=False)
    ins.write_hook(box, HOST, sleep=no_sleep)
    writes = [stdin for cid, _, stdin in box.calls if cid == ins.CMD_HOOK_WRITE]
    assert len(writes) == 2 and writes[0] == writes[1] == ins.hook_script().encode()
    assert box.files[ins.HOOK]["data"] == ins.hook_script().encode()


def test_a_lasting_silent_255_on_hook_write_raises_its_code_after_3_tries():
    box = FakeBox()
    box.answer_next(ins.CMD_HOOK_WRITE, times=3, keep_output=False)
    with pytest.raises(ins.InstallError) as ei:
        ins.write_hook(box, HOST, sleep=no_sleep)
    assert ei.value.code == "hook_write_failed" and box.ids().count(ins.CMD_HOOK_WRITE) == 3


def test_hook_run_255_with_only_its_own_started_line_runs_again_and_finds_it_alive():
    box = _installed_box(gateway_up_after=0)
    ins.write_hook(box, HOST, sleep=no_sleep)
    box.answer_next(ins.CMD_HOOK_RUN)  # "50-kirocrew-gateway: started gateway pid 7", then 255
    sleeps: list[float] = []
    h = ins.run_hook(box, HOST, sleep=sleeps.append)
    assert (h.state, h.gateway) == ("alive", "up") and sleeps == [3.0]
    assert box.ids().count(ins.CMD_HOOK_RUN) == 2 and box.hook_runs == 2


def test_a_lasting_silent_255_on_hook_run_is_hook_exit_255():
    box = _installed_box()
    ins.write_hook(box, HOST, sleep=no_sleep)
    box.answer_next(ins.CMD_HOOK_RUN, times=3, keep_output=False)
    h = ins.run_hook(box, HOST, sleep=no_sleep)
    assert (h.state, h.detail) == ("failed", "hook exit 255")
    assert box.ids().count(ins.CMD_HOOK_RUN) == 3 and ins.CMD_GATEWAY_PROBE not in box.ids()


def test_hook_run_255_with_a_mint_line_is_failed_after_one_exec():
    box = _installed_box()
    ins.write_hook(box, HOST, sleep=no_sleep)
    box.answer_next(ins.CMD_HOOK_RUN, keep_output=False, extra=MINT_409)
    h = ins.run_hook(box, HOST, sleep=no_sleep)
    assert (h.state, h.detail) == ("failed", "hook exit 255")
    assert box.ids().count(ins.CMD_HOOK_RUN) == 1


def test_install_public_255_after_a_complete_install_is_installed_with_one_exec():
    box = FakeBox()
    box.answer_next(ins.CMD_INSTALL_PUBLIC)  # install_rc=0 and wrapper=ok, then 255
    r = path_a(box, HOST, sleep=no_sleep)
    assert r.status == "installed" and box.ids().count(ins.CMD_INSTALL_PUBLIC) == 1


def test_install_public_255_cut_before_install_rc_is_lf6_with_one_exec():
    box = FakeBox()
    box.answer_next(ins.CMD_INSTALL_PUBLIC, keep_output=False, extra=STEP_FACTS)
    r = path_a(box, HOST, sleep=no_sleep)
    assert (r.status, r.why) == (
        "failed",
        ins.WHY_INSTALL_FAILED,
    ) and "install-public exit 255" in r.detail
    assert box.ids().count(ins.CMD_INSTALL_PUBLIC) == 1


def test_a_gateway_check_255_with_gateway_up_is_up():
    box = _installed_box(gateway_up_after=0)
    ins.write_hook(box, HOST, sleep=no_sleep)
    box.answer_next(ins.CMD_GATEWAY_PROBE)
    h = ins.run_hook(box, HOST, sleep=no_sleep)
    assert (h.state, h.gateway) == ("started", "up") and box.ids().count(ins.CMD_GATEWAY_PROBE) == 1


# ---- A2: the same over a real subprocess (a fake ``ssh`` first on PATH; no network) ------------


class _Counted:
    """A runner that records each command's id, then runs the genuine runner's ``run``."""

    def __init__(self, inner, genuine):
        self.inner, self.genuine, self.ids = inner, genuine, []

    def run(self, host, command, stdin=None):
        self.ids.append(LISTED[command])
        return self.genuine(self.inner, host, command, stdin)


def _fake_ssh(tmp_path):
    """``ssh -o BatchMode=yes <host> <command>``: runs the command under local ``sh`` with
    ``/projects`` mapped into ``box/``, prints its real output, and exits 255 on the first run of
    each command (one state file per command). With ``state/mint-fails`` present it runs nothing
    and prints the ProxyCommand's line on stderr, exit 255. ``chown`` is a no-op (not root here).
    Returns (root, bin dir, state dir)."""
    root, bindir, state = tmp_path / "box", tmp_path / "bin", tmp_path / "state"
    for d in (root, bindir, state):
        d.mkdir()
    sed_root = str(root).replace("\\", "\\\\").replace("&", "\\&").replace("#", "\\#")
    mapping = shlex.quote(f"s#{ins.ROOT}#{sed_root}{ins.ROOT}#g")
    st, path = shlex.quote(str(state)), shlex.quote(f"{bindir}:/usr/bin:/bin")
    _stub(
        bindir / "ssh",
        (
            '[ "$1 $2" = "-o BatchMode=yes" ] && [ $# = 4 ] '
            '|| { echo "fake ssh: argv $*" >&2; exit 2; }\n'
            f'echo "$3" >>{st}/hosts\n'
            f"if [ -e {st}/mint-fails ]; then printf %s {shlex.quote(MINT_409)} >&2; exit 255; fi\n"
            f'cmd="$(printf \'%s\' "$4" | sed -e {mapping})"\n'
            "key=\"$(printf '%s' \"$4\" | cksum | cut -d' ' -f1)\"\n"
            f'first=0; [ -e {st}/seen."$key" ] || {{ : >{st}/seen."$key"; first=1; }}\n'
            f'PATH={path} sh -c "$cmd"\n'
            'rc=$?; [ "$first" = 1 ] && exit 255; exit "$rc"\n'
        ),
    )
    _stub(bindir / "chown", "exit 0\n")
    _stub(root / ins.WRAPPER.lstrip("/"), 'case "$1" in --version) exit 0 ;; esac\nexit 2\n')
    return root, bindir, state


@pytest.mark.parametrize("kind", ["install", "engine"])
def test_a2_a_255_on_each_commands_first_run_over_a_real_ssh_subprocess(
    tmp_path, monkeypatch, kind
):
    root, bindir, state = _fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")  # the core resolves ssh from PATH
    runner = (
        _Counted(eng.InstallSshRunner(), _GENUINE_ENGINE_SSH_RUN)
        if kind == "engine"
        else _Counted(ins.SshRunner(), _GENUINE_SSH_RUN)
    )
    sleeps: list[float] = []
    assert "crew-wrapper" in ins.probe(runner, HOST, sleep=sleeps.append)
    assert runner.ids == [ins.CMD_PROBE] and sleeps == []  # its done line arrived: 1 exec
    ins.write_hook(runner, HOST, sleep=sleeps.append)
    assert runner.ids[1:] == [ins.CMD_HOOK_DIRS] * 2 + [ins.CMD_HOOK_WRITE] * 2
    assert sleeps == [3.0, 3.0] and (state / "hosts").read_text().split() == [HOST] * 5
    hook = root / ins.HOOK.lstrip("/")
    assert hook.read_bytes() == ins.hook_script().encode()
    assert hook.stat().st_mode & 0o777 == 0o755
    assert not (root / ins.HOOK_TMP.lstrip("/")).exists()


def test_a2_a_mint_line_on_stderr_is_read_and_never_tried_again(tmp_path, monkeypatch):
    root, bindir, state = _fake_ssh(tmp_path)
    (state / "mint-fails").touch()
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    runner = _Counted(ins.SshRunner(), _GENUINE_SSH_RUN)
    sleeps: list[float] = []
    with pytest.raises(ins.InstallError) as ei:
        ins.write_hook(runner, HOST, sleep=sleeps.append)
    assert ei.value.code == "hook_dirs_failed" and runner.ids == [ins.CMD_HOOK_DIRS]
    assert sleeps == [] and not (root / ins.BOOT_D.lstrip("/")).exists()

"""A read-only seal's parent directory stays put inside the product's own Linux sandbox.

The seal on ``~/.kiro/agents`` is a bind on that path, and a bind moves with a renamed
parent, which would free the sealed name for the agent to fill; a link at the sealed
name, or one met inside another link's target, frees it the same way. The child reports
the launcher's marker, so a run that never entered the sandbox fails instead of passing
on the host's own view.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.config import paths
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import default_timeout

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
]

_AGENT = r"""
import errno, json, os, sys

kiro = os.path.join(os.environ["HOME"], ".kiro")
held = sys.argv[1]

def attempt(step):
    try:
        step()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

report = {
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "sibling": attempt(lambda: open(os.path.join(kiro, "sibling.txt"), "w").close()),
    "rename": attempt(lambda: os.rename(held, held + ".moved")),
    "unlink": attempt(lambda: os.unlink(held)),
}
print("REPORT " + json.dumps(report), flush=True)
"""


@pytest.fixture(params=["directory", "symlink", "sealed-symlink", "chained-symlink"])
def home(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    home = tmp_path.resolve() / "home"
    kiro = home / ".kiro"
    if request.param == "symlink":
        # A supported layout: ``~/.kiro`` is a link, and moving the link frees the name too.
        home.mkdir()
        kiro.symlink_to(tmp_path.resolve() / "elsewhere")
        kiro = kiro.resolve()
    if request.param == "chained-symlink":
        # ``~/.kiro`` leads through a second link, ``~/links/current``, as a dotfiles
        # manager's "current" link does: retargeting that one moves the tree as well.
        (home / "links").mkdir(parents=True)
        (home / "links" / "current").symlink_to(tmp_path.resolve() / "dotfiles-kiro")
        kiro.symlink_to(home / "links" / "current")
        kiro = kiro.resolve()
    if request.param == "sealed-symlink":
        # The sealed ``~/.kiro/agents`` is itself a link: the seal lands on its target.
        (tmp_path.resolve() / "dotfiles-agents").mkdir()
        (kiro / "crew").mkdir(parents=True)
        (kiro / "agents").symlink_to(tmp_path.resolve() / "dotfiles-agents")
    else:
        for leaf in ("agents", "crew"):
            (kiro / leaf).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    # A passing test's tmp path is removed and its name reused, so two layouts can share
    # one ``KIROCREW_HOME`` string, and the memo would hand the second the first's home.
    monkeypatch.setattr(paths, "_config_dir_memo", None)
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


@pytest.mark.parametrize("mode", ["standard", "strict"])
def test_a_sandboxed_process_cannot_move_the_names_holding_a_sealed_tree(
    home: Path, mode: str
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    agents = home / ".kiro" / "agents"
    current = home / "links" / "current"
    held = next((p for p in (agents, current) if p.is_symlink()), home / ".kiro")
    proc = subprocess.run(
        sandbox.namespace_argv([sys.executable, "-c", _AGENT, str(held)], mode),
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["marker"] == "1", output
    assert report["rename"] == "EBUSY", output
    assert report["unlink"] == ("EBUSY" if held.is_symlink() else "EISDIR"), output
    # Only the directory itself is held: its other entries stay writable.
    assert report["sibling"] == "ok", output
    assert (home / ".kiro" / "agents").is_dir()

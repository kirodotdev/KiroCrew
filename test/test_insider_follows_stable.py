"""A stable release reaches the insider pip index and the ``:insider`` image.

Insider is never behind stable on any install path. The CLI feed and the
desktop feeds already copy a stable release to insider behind the insider
feed guard (``test_check_feed_advance.py``); these tests run the ``run:``
blocks of the two remaining pointer writers, unedited, against stub ``curl``,
``aws``, ``git`` and ``docker`` commands that only record what they were asked
to do:

* ``publish-cli.yml`` "Publish PEP 503 simple index" must list a stable wheel
  in ``feed/insider/simple/`` too.
* ``publish-docker.yml`` "Mirror stable alias to insider" must move
  ``:insider`` to a stable release's digest, unless a newer insider release
  is already tagged.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from installer_test_helpers import run_bounded

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CDN_BASE = "https://cdn.example.invalid"
WHEEL_VERSION = "0.12.1"
WHEEL_NAME = f"kirocrew-{WHEEL_VERSION}-py3-none-any.whl"
SHA256 = "ab" * 32
IMAGE = "ghcr.io/kirodotdev/kirocrew"
DIGEST = "sha256:" + "cd" * 32

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the publish steps run in bash on the ubuntu runner",
)


def _step(workflow: str, job: str, name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    steps = {s.get("name"): s for s in doc["jobs"][job]["steps"]}
    assert name in steps, f"{workflow}: step {name!r} is missing"
    return steps[name]


def _write_tool(tools: Path, name: str, body: str) -> None:
    tools.mkdir(exist_ok=True)
    path = tools / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(script: str, tmp_path: Path, env: dict[str, str]) -> None:
    # The step's python3 is this interpreter, not whatever version shim PATH holds.
    _write_tool(tmp_path / "tools", "python3", f'exec "{sys.executable}" "$@"\n')
    full = {
        **os.environ,
        "PATH": f"{tmp_path / 'tools'}{os.pathsep}{os.environ.get('PATH', '')}",
        "LOG": str(tmp_path / "calls.log"),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        **env,
    }
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    result = run_bounded(["bash", "-e", "-c", script], full, 60.0, cwd=str(work))
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _index_writes(tmp_path: Path, channel: str) -> dict[str, str]:
    """Run the simple-index step for ``channel``; map each S3 key to its body."""
    # Every index page reads as absent, so each run writes a fresh one.
    _write_tool(
        tmp_path / "tools",
        "curl",
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\nprintf 404\n',
    )
    _write_tool(
        tmp_path / "tools",
        "aws",
        'key=; body=\nwhile [ $# -gt 0 ]; do case "$1" in\n'
        '  --key) key="$2"; shift ;;\n  --body) body="$2"; shift ;;\nesac; shift; done\n'
        'echo "$key" >> "$LOG"\ncp "$body" "$LOG.$(printf %s "$key" | tr / _)"\n',
    )
    step = _step("publish-cli.yml", "publish-cli", "Publish PEP 503 simple index")
    _run(
        step["run"],
        tmp_path,
        {
            "CHANNEL": channel,
            "BUCKET": "bucket",
            "CDN_BASE": CDN_BASE,
            "WHEEL_NAME": WHEEL_NAME,
            "WHEEL_VERSION": WHEEL_VERSION,
            "SHA256": SHA256,
            "PYREQ": ">=3.12",
        },
    )
    keys = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    return {
        key: (tmp_path / f"calls.log.{key.replace('/', '_')}").read_text(encoding="utf-8")
        for key in keys
    }


def test_stable_release_lists_its_wheel_in_the_insider_index(tmp_path: Path) -> None:
    writes = _index_writes(tmp_path, "stable")
    link = f'href="{CDN_BASE}/cli/stable/{WHEEL_VERSION}/{WHEEL_NAME}#sha256={SHA256}"'
    for channel in ("stable", "insider"):
        page = writes.get(f"feed/{channel}/simple/kirocrew/")
        assert page is not None, f"the {channel} index page was never written"
        assert link in page, f"the {channel} index does not list the stable wheel"
        assert f"feed/{channel}/simple/" in writes
    keys = list(writes)
    assert keys.index("feed/stable/simple/") < keys.index(
        "feed/insider/simple/kirocrew/"
    ), "the insider index must be written after the stable one"


@pytest.mark.parametrize("channel", ["insider", "nightly", "lts"])
def test_other_channels_write_only_their_own_index(tmp_path: Path, channel: str) -> None:
    writes = _index_writes(tmp_path, channel)
    assert writes, "nothing was written"
    assert all(key.startswith(f"feed/{channel}/simple/") for key in writes), sorted(writes)


def _insider_alias_calls(tmp_path: Path, tags: list[str]) -> list[str]:
    """Run the docker insider-mirror step for a stable release; return docker calls."""
    tags_file = tmp_path / "tags.txt"
    tags_file.write_text(
        "".join(f"{'0' * 40}\trefs/tags/{tag}\n" for tag in tags)
        + f"{'1' * 40}\trefs/tags/{tags[0]}^{{}}\n",
        encoding="utf-8",
    )
    _write_tool(tmp_path / "tools", "git", f'cat "{tags_file}"\n')
    _write_tool(tmp_path / "tools", "docker", 'echo "$*" >> "$LOG"\n')
    work = tmp_path / "work" / "scripts"
    work.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "check_feed_advance.py", work / "check_feed_advance.py")
    step = _step("publish-docker.yml", "publish-docker", "Mirror stable alias to insider")
    assert step["if"] == "env.CHANNEL == 'stable'"
    script = (
        step["run"]
        .replace("${{ steps.image.outputs.name }}", IMAGE)
        .replace("${{ steps.digest.outputs.value }}", DIGEST)
    )
    assert "${{" not in script, "the step reads an expression this test does not fill in"
    _run(
        script,
        tmp_path,
        {"VERSION": WHEEL_VERSION, "CHANNEL": "stable", "GITHUB_REF_NAME": f"v{WHEEL_VERSION}"},
    )
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_stable_release_moves_the_insider_image_alias(tmp_path: Path) -> None:
    calls = _insider_alias_calls(tmp_path, ["v0.12.1-rc.3", "v0.12.1", "v0.12.0"])
    assert calls == [f"buildx imagetools create -t {IMAGE}:insider {IMAGE}@{DIGEST}"]


def test_insider_already_ahead_keeps_its_image_alias(tmp_path: Path) -> None:
    calls = _insider_alias_calls(tmp_path, ["v0.12.1", "v0.13.0-rc.1"])
    assert calls == []


def test_insider_alias_moves_after_every_stable_alias_write() -> None:
    doc = yaml.safe_load((WORKFLOWS / "publish-docker.yml").read_text(encoding="utf-8"))
    names = [s.get("name") for s in doc["jobs"]["publish-docker"]["steps"]]
    mirror = names.index("Mirror stable alias to insider")
    for earlier in (
        "Update channel alias (and latest for stable)",
        "Reconcile aliases (re-run)",
        "Verify promoted stable alias reconciliation",
    ):
        assert names.index(earlier) < mirror, f"{earlier!r} must run before the insider mirror"

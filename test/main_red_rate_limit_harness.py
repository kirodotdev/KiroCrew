"""The rate-limit side of the main-red step tests: a fixed clock, a `sleep` and the wrapper.

`main-red-watch.yml` runs its step behind `.github/actions/gh-rate-limit`, whose
`gh` wrapper waits for a spent limit's reset and retries. The step tests run
the step the same way: the action's own install script puts the wrapper in
front of the `gh` stub, `date +%s` answers a fixed clock and `sleep` records the
wait and stands in for the reset, so nothing sleeps and a wait is an exact
number.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

RATE_LIMIT_ACTION = "./.github/actions/gh-rate-limit"
# The reset every stubbed `/rate_limit` read reports, and how far before it the
# clock stands unless a test says otherwise: past the watcher's wait budget, so
# a refused call is not waited for.
RESET = 1795040400
RESET_FAR = 3000

# Records the wait, then stands in for the reset: nothing is refused after it.
_SLEEP_STUB = r"""#!/usr/bin/env bash
echo "$1" >> "$STUB_DIR/sleeps.log"
rm -f "$STUB_DIR/spent" "$STUB_DIR"/rate-limited-* "$STUB_DIR/runs-rate-limited"
"""

# `date +%s` is what a wait is computed from; any other use goes to the host's `date`.
_DATE_STUB = """#!/usr/bin/env bash
if [ "$#" -eq 1 ] && [ "$1" = "+%s" ]; then
  echo {now}
  exit 0
fi
exec {real} "$@"
"""


def install_clock(bin_dir: Path, reset_in: int) -> None:
    """Put `sleep` and `date` on ``bin_dir``; the clock stands ``reset_in`` s before RESET."""
    date = _DATE_STUB.format(now=RESET - reset_in, real=shutil.which("date"))
    for name, body in (("sleep", _SLEEP_STUB), ("date", date)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8", newline="\n")
        tool.chmod(0o755)


def install_wrapper(job: dict, root: Path, tmp_path: Path, env: dict[str, str]) -> list[str]:
    """Run the rate-limit action's install script when ``job`` uses it.

    Returns the directories it adds to PATH, which the runner puts first for
    every later step. A job without the action gets none.
    """
    uses = [s for s in job["steps"] if s.get("uses") == RATE_LIMIT_ACTION]
    if not uses:
        return []
    action = yaml.safe_load((root / RATE_LIMIT_ACTION / "action.yml").read_text(encoding="utf-8"))
    (install,) = action["runs"]["steps"]
    budget = (uses[0].get("with") or {}).get("max-wait", action["inputs"]["max-wait"]["default"])
    added = tmp_path / "github-path"
    done = subprocess.run(
        ["bash", "-c", install["run"]],
        cwd=root,
        env={**env, "GH_RATE_LIMIT_MAX_WAIT": str(budget), "GITHUB_PATH": str(added)},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    return [line for line in added.read_text(encoding="utf-8").splitlines() if line]


def sleeps(stub: Path) -> list[int]:
    """The waits the step asked for, in seconds."""
    log = stub / "sleeps.log"
    return [int(line) for line in log.read_text(encoding="utf-8").split()] if log.exists() else []

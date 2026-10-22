#!/usr/bin/env python3
"""pre_push_gate.py - an optional, advisory pre-push gate runner.

Runs the cheap CI gates in ``gate_map.json`` against the diff, prints every failure
at once, and records a proof for the exact tree it checked: ``<git-dir>/
prepare-pr-gate-proof.json``. It prints the ``Gate proof:`` line a PR body may carry.

Nothing here refuses a push. No hook calls it, no CI job fails when its line is
absent. CI stays the authority: a local run cannot reproduce CI on every machine (a
tool that is not installed, another operating system, no network), so a skip is a
recorded fact with a code, never a refusal. The one check that can fail is a ``Gate proof:``
line that contradicts the recorded proof (``diff_signals.py --check-body``, exit 25).

Skip codes: ``tool-missing:<tool>`` (written by this runner), ``unsupported-os``
(written by this runner), ``no-network``, ``red-on-base:<sha>``, and
``other:<30 or more characters>``. A skip is given with ``--skip GATE=CODE``.

The proof is written only when no gate failed and every gate that did not run has a
skip code. It names a committed tree, so the run refuses (51) while the working tree has
uncommitted changes, and writes nothing if HEAD or the tree changes during the run.
Without a merge base every gate runs, not only those the diff touches.

Usage:  python3 pre_push_gate.py [--base B] [--skip GATE=CODE ...]
        python3 pre_push_gate.py --verify | --test
Exit:   0 proof written (or found) | 50 a gate failed | 51 a gate could not run and no
        skip covers it, or the tree is dirty or changed during the run | 52 --verify found
        no proof for this tree | 2 environment / usage
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(HERE, "gate_map.json")
PROOF_FILENAME = "prepare-pr-gate-proof.json"
PROOF_VERSION = 1

EXIT_OK = 0
EXIT_ENV = 2
EXIT_FAILED = 50
EXIT_UNRUNNABLE = 51
EXIT_NO_PROOF = 52

# One gate may run this long; a hung tool is a failure to report, not a wait.
GATE_TIMEOUT_S = 1800
# Lines of a failed gate's output kept in the report.
_TAIL_LINES = 25

SKIP_CODE = re.compile(
    r"^(?:tool-missing:[\w.+-]+|no-network|unsupported-os|red-on-base:[0-9a-f]{7,40}"
    r"|other:\S.{28,}\S)$"
)
PROOF_LINE = re.compile(
    r"^Gate proof:\s*tree\s+(?P<tree>[0-9a-f]{12});\s*ran\s+(?P<ran>\d+);"
    r"\s*skipped\s+(?P<skipped>.+?)\s*$"
)


def run(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
    """Run a command; return (rc, stdout, stderr). Never raises."""
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            timeout=GATE_TIMEOUT_S,
        )
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after {} s".format(GATE_TIMEOUT_S)
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


# Git config this runner must not inherit. Listing keys one by one leaks: the next key is
# always missing. So the answers do not rest on the overrides alone:
#   * every call runs ``git -C <toplevel>``, so the current directory (and with it
#     ``diff.relative``, which strips a subdirectory from paths) never matters;
#   * the changed paths come from ``diff-tree``, a plumbing command that reads no porcelain
#     display config, and the dirty check from ``status --porcelain=v1 -z``, whose format is
#     fixed (root-relative, never colored or quoted);
#   * ``-c`` on the command line beats every config file and an ancestor's ``git -c``, and
#     covers the keys that still reach those commands.
# test_prepare_pr_pre_push_gate.py (HOSTILE_GIT_CONFIG) pins this against the ``diff.*``,
# ``status.*`` and ``core.*`` keys of ``git help --config`` that can change the output. ``core.autocrlf`` is left alone on purpose: it decides whether a
# line-ending-only difference is a change, and git's own commit and status already agree.
# ``.git/info/exclude`` cannot be overridden: a file it hides is not seen as untracked.
_GIT_NEUTRAL = [
    "-c",
    "status.showUntrackedFiles=all",
    "-c",
    "status.renames=false",
    "-c",
    "status.relativePaths=false",
    "-c",
    "diff.renames=false",
    "-c",
    "diff.relative=false",
    "-c",
    "diff.ignoreSubmodules=none",
    "-c",
    "color.ui=never",
    "-c",
    "core.quotepath=true",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.ignoreStat=false",
    "-c",
    "core.excludesFile=" + os.devnull,
]
# Variables that point git at a different repository, index or object store. A pre-push hook
# sets some of them; a proof about the repository in the current directory must not use them.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


def clean_environ() -> dict[str, str]:
    """This process's environment without the variables that retarget git."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def git_argv(args: list[str], top: str | None = None) -> list[str]:
    """``git -C <top>`` with the config overrides above, then ``args``."""
    return ["git", *(["-C", top] if top else []), *_GIT_NEUTRAL, *args]


def _git_stdout(args: list[str], top: str | None) -> bytes:
    try:
        done = subprocess.run(
            git_argv(args, top), capture_output=True, timeout=GATE_TIMEOUT_S, env=clean_environ()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("git {} could not run: {}".format(args[0], exc)) from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()
        raise ValueError("git {} failed ({}): {}".format(args[0], done.returncode, detail))
    return done.stdout


def toplevel() -> str:
    """The work tree's root, asked from the current directory: the one call without ``-C``."""
    return os.fsdecode(_git_stdout(["rev-parse", "--show-toplevel"], None)).removesuffix("\n")


def git_bytes(args: list[str]) -> bytes:
    """Stdout of ``git -C <toplevel> <args>`` as bytes; a failing git is an error.

    Run from the work tree's root whatever the current directory is. Paths come back as bytes
    because a file name is not text: git quotes a non-ASCII name unless the listing is
    NUL-separated (``-z``), and a name may hold spaces or newlines.
    """
    return _git_stdout(args, toplevel())


def git_line(args: list[str]) -> str:
    """One value git prints (a sha, a directory): only the final newline is removed."""
    return os.fsdecode(git_bytes(args)).removesuffix("\n")


def git_paths(args: list[str]) -> list[str]:
    """Paths from a ``-z`` listing, split on NUL and decoded the way the OS names files."""
    return [os.fsdecode(name) for name in git_bytes(args).split(b"\0") if name]


def load_map(path: str | None = None) -> dict[str, Any]:
    """The gate map, checked for the fields the runner and the CI pin rely on."""
    with open(path or MAP_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    ids = set()
    for gate in data["gates"]:
        for key in ("id", "cmd", "ci"):
            if key not in gate:
                raise ValueError("gate {!r} has no {!r}".format(gate.get("id"), key))
        if gate["id"] in ids:
            raise ValueError("gate id {!r} is listed twice".format(gate["id"]))
        ids.add(gate["id"])
    for entry in data["ci_only"]:
        if not entry.get("reason") or not entry.get("needle"):
            raise ValueError("ci_only {!r} needs a needle and a reason".format(entry.get("id")))
    return data


def select(gates: list[dict[str, Any]], paths: list[str] | None) -> list[dict[str, Any]]:
    """Gates whose path patterns match a changed path; a gate with none always runs.

    ``paths`` of None means the changed paths are unknown: every gate is chosen.
    """
    chosen = []
    for gate in gates:
        patterns = gate.get("paths")
        if (
            paths is None
            or not patterns
            or any(re.search(p, path) for p in patterns for path in paths)
        ):
            chosen.append(gate)
    return chosen


def unavailable(gate: dict[str, Any]) -> str:
    """A skip code this runner writes for a gate it cannot run here, or ''."""
    systems = gate.get("os")
    if systems and not any(sys.platform.startswith(s) for s in systems):
        return "unsupported-os"
    module = gate.get("module")
    if module and importlib.util.find_spec(module) is None:
        return "tool-missing:" + module
    tool = gate.get("tool")
    if tool and shutil.which(tool) is None:
        return "tool-missing:" + tool
    return ""


def _argv(tokens: list[str]) -> list[str]:
    return [sys.executable if t == "{py}" else t for t in tokens]


def _tail(text: str) -> str:
    return "\n".join(text.rstrip().splitlines()[-_TAIL_LINES:])


def run_gate(gate: dict[str, Any], base_sha: str, top: str) -> tuple[str, str]:
    """Run a gate (its self-test first); return ("pass" | "fail" | "cannot", output tail)."""
    env = clean_environ()
    if gate.get("base_env"):
        env[gate["base_env"]] = base_sha
    steps = []
    test = gate.get("self_test")
    if test:
        steps.append(_argv(gate["cmd"] + ["--test"] if test is True else test))
    steps.append(_argv(gate["cmd"]))
    for argv in steps:
        rc, out, err = run(argv, env=env, cwd=top)
        if rc == 127:
            return "cannot", _tail(err)
        if rc != 0:
            return "fail", _tail(out + err)
    return "pass", ""


def proof_line(proof: dict[str, Any]) -> str:
    skipped = ", ".join("{}={}".format(s["gate"], s["code"]) for s in proof["skipped"])
    return "Gate proof: tree {}; ran {}; skipped {}".format(
        proof["tree"][:12], len(proof["ran"]), skipped or "none"
    )


def proof_path(git_dir: str) -> str:
    return os.path.join(git_dir, PROOF_FILENAME)


def head_tree() -> str:
    return git_line(["rev-parse", "HEAD^{tree}"])


def read_proof(git_dir: str, tree: str) -> dict[str, Any] | None:
    """The recorded proof, only when it is a well-formed one for ``tree``."""
    try:
        with open(proof_path(git_dir), encoding="utf-8") as handle:
            proof = json.load(handle)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(proof, dict)
        or proof.get("version") != PROOF_VERSION
        or proof.get("tree") != tree
        or not isinstance(proof.get("ran"), list)
        or not isinstance(proof.get("skipped"), list)
    ):
        return None
    return proof


def contradiction(body: str, git_dir: str, tree: str) -> str:
    """Why a ``Gate proof:`` line in ``body`` is not true, or '' when it is or is absent.

    An absent line is never a problem: the proof is optional. A present line is a
    claim, and only a claim that disagrees with the recorded proof is reported.
    """
    lines = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("Gate proof:")]
    if not lines:
        return ""
    proof = read_proof(git_dir, tree)
    if proof is None:
        return "the body has a Gate proof: line, but there is no recorded proof for this tree"
    expected = proof_line(proof)
    for line in lines:
        if PROOF_LINE.match(line) is None:
            return "the Gate proof: line is not in the form `{}`".format(expected)
        if line != expected:
            return "the Gate proof: line says `{}`; the recorded proof says `{}`".format(
                line, expected
            )
    return ""


def _check_skips(values: list[str], known: set[str]) -> dict[str, str]:
    skips = {}
    for value in values:
        gate, _, code = value.partition("=")
        if gate not in known:
            raise ValueError("--skip {!r}: no gate with that id".format(value))
        if not SKIP_CODE.match(code):
            raise ValueError(
                "--skip {!r}: the code must be tool-missing:<tool>, no-network, "
                "unsupported-os, red-on-base:<sha> or other:<30+ characters>".format(value)
            )
        skips[gate] = code
    return skips


def _base_sha(base: str) -> str:
    top = toplevel()
    if not base:
        symbolic = git_argv(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], top)
        sym = run(symbolic, env=clean_environ())[1]
        base = sym.strip()[len("origin/") :] if sym.strip().startswith("origin/") else "main"
    merge_base = git_argv(["merge-base", "HEAD", "origin/" + base], top)
    return run(merge_base, env=clean_environ())[1].strip()  # no merge base: ""


def _changed(base_sha: str) -> list[str] | None:
    """Paths changed since the merge base, or None when git cannot say."""
    if not base_sha:
        return None
    # diff-tree: plumbing, root-relative, no porcelain display config. -z: NUL-separated, never
    # quoted. --no-renames: a rename lists both its old and new path. The work tree is clean
    # here (a run refuses while it is not), so HEAD's tree is the tree the gates checked.
    try:
        return sorted(
            git_paths(["diff-tree", "-r", "--name-only", "--no-renames", "-z", base_sha, "HEAD"])
        )
    except ValueError:
        return None


def _worktree_state() -> tuple[str, str, list[str]]:
    """(HEAD sha, HEAD tree, dirty entries): what a proof can be about.

    A dirty entry is one ``git status --porcelain=v1 -z`` record, "XY path" with the path
    unquoted. A git that fails raises, so a broken status is never read as a clean tree.
    """
    head = git_line(["rev-parse", "HEAD"])
    dirty = git_paths(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"]
    )
    return head, head_tree(), dirty


def _verify(git_dir: str) -> int:
    proof = read_proof(git_dir, head_tree())
    if proof is None:
        print("NO PROOF: nothing recorded for this tree. Optional: python3 pre_push_gate.py")
        return EXIT_NO_PROOF
    print(proof_line(proof))
    return EXIT_OK


def _gate_run(args: argparse.Namespace, git_dir: str, top: str) -> int:
    gate_map = load_map()
    known = {g["id"] for g in gate_map["gates"]}
    skips = _check_skips(args.skip, known)
    head, tree, dirty = _worktree_state()
    if dirty:
        print(
            "REFUSED: uncommitted changes. The gates check the working tree and a proof names "
            "a committed tree, so a proof would certify code that was never checked. Commit "
            "(or stash) first, then run again."
        )
        print("\n".join("    " + entry for entry in dirty[:_TAIL_LINES]))
        return EXIT_UNRUNNABLE
    base_sha = _base_sha(args.base)
    changed = _changed(base_sha)
    if changed is None:
        print(
            "NOTE: no merge base with origin/<base>; running every gate, "
            "not only those the diff touches"
        )
    chosen = select(gate_map["gates"], changed)
    ran: list[str] = []
    failed: list[str] = []
    unrunnable: list[str] = []
    skipped: list[dict[str, str]] = []
    for gate in chosen:
        gid = gate["id"]
        code = skips.get(gid) or unavailable(gate)
        if code:
            skipped.append({"gate": gid, "code": code})
            print("SKIP  {} ({})".format(gid, code))
        elif gate.get("base_env") and not base_sha:
            unrunnable.append(gid)
            print("CANNOT RUN  {} (no merge base with origin; skip it with a code)".format(gid))
        else:
            status, tail = run_gate(gate, base_sha, top)
            {"pass": ran, "fail": failed, "cannot": unrunnable}[status].append(gid)
            print("{}  {}".format(status.upper(), gid))
            if tail:
                print("\n".join("    " + line for line in tail.splitlines()))
    print(
        "{} ran, {} skipped, {} failed, {} could not run".format(
            len(ran), len(skipped), len(failed), len(unrunnable)
        )
    )
    if failed:
        return EXIT_FAILED
    if unrunnable:
        return EXIT_UNRUNNABLE
    if _worktree_state() != (head, tree, []):
        print("REFUSED: HEAD or the working tree changed while the gates ran; no proof written.")
        return EXIT_UNRUNNABLE
    proof = {
        "version": PROOF_VERSION,
        "tree": tree,
        "base": base_sha,
        "ran": ran,
        "skipped": skipped,
    }
    with open(proof_path(git_dir), "w", encoding="utf-8") as handle:
        json.dump(proof, handle, indent=2)
        handle.write("\n")
    print(proof_line(proof))
    return EXIT_OK


def _selftest() -> int:
    """The pure parts, against a throwaway git directory."""
    tree = "a" * 40
    proof = {"version": 1, "tree": tree, "ran": ["x", "y"], "skipped": []}
    line = proof_line(proof)
    assert PROOF_LINE.match(line), line
    assert SKIP_CODE.match("tool-missing:uv") and not SKIP_CODE.match("other:too short")
    assert [g["id"] for g in select([{"id": "a"}, {"id": "b", "paths": [r"\.md$"]}], ["x.py"])] == [
        "a"
    ]
    with tempfile.TemporaryDirectory() as git_dir:
        assert contradiction("no line here", git_dir, tree) == ""
        assert contradiction(line, git_dir, tree), "a line with no proof must contradict"
        with open(proof_path(git_dir), "w", encoding="utf-8") as handle:
            json.dump(proof, handle)
        assert (
            contradiction("Gate proof: tree " + tree[:12] + "; ran 2; skipped none", git_dir, tree)
            == ""
        )
        assert contradiction(
            "Gate proof: tree " + tree[:12] + "; ran 9; skipped none", git_dir, tree
        )
    load_map()
    print("pre_push_gate.py self-test: ok")
    return EXIT_OK


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Optional, advisory pre-push gate runner.")
    parser.add_argument("--base", default="", help="Base branch name (default: origin/HEAD).")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="GATE=CODE",
        help="Do not run GATE; record CODE (tool-missing:<tool>, no-network, "
        "unsupported-os, red-on-base:<sha>, other:<30+ characters>).",
    )
    parser.add_argument(
        "--verify", action="store_true", help="Exit 52 unless this tree has a proof."
    )
    parser.add_argument("--test", action="store_true", help="Run the self-test.")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return EXIT_ENV if exc.code else 0
    if args.test:
        return _selftest()
    try:
        # Two calls, and only the final newline removed: a directory name may end in a space.
        git_dir = git_line(["rev-parse", "--absolute-git-dir"])
        top = toplevel()
    except ValueError:
        sys.stderr.write("ERROR: not inside a git repository.\n")
        return EXIT_ENV
    try:
        if args.verify:
            return _verify(git_dir)
        return _gate_run(args, git_dir, top)
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write("ERROR: {}\n".format(exc))
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main(sys.argv))

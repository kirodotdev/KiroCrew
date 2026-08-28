"""Pre-merge installability probe for Dev Fleet's Pull+Build frontend half.

``npm ci`` DELETES ``node_modules`` before it installs. So a registry that
refuses one package turns a sync into damage rather than a no-op: the tree is
emptied, the run aborts mid-reify, and the checkout is left with new source, a
new lockfile, and no frontend dependencies at all. Re-pressing Pull+Build
repeats the deletion.

This module answers the one question that prevents that, BEFORE the merge
lands: *can the incoming lockfile be installed at all?* It reads the lockfile
out of the fetched ref (``git show``) rather than the working tree, so it can
run between ``fetch`` and ``merge`` — the only point where the new lockfile is
already knowable and nothing has been applied yet. A refusal there costs
nothing, because ``fetch`` only moves remote refs.

Two things it deliberately is NOT:

* It is NOT a registry auth check. A dead registry token is invisible to a
  build whose packages are all in npm's local cache, because cacache retrieval
  is integrity-addressed: a tarball already on disk satisfies its lockfile
  entry with no network and no credentials. A ``npm ping``-style probe would
  therefore fail while the very install it guards would have succeeded — it
  would block good syncs and teach operators to ignore it. Asking
  "is this installable" instead is both narrower and correct, and it is
  registry-agnostic: it holds for a public registry, a private mirror, or an
  air-gapped cache alike.
* It does NOT run the package tree's lifecycle scripts (``--ignore-scripts``).
  The probe exists to answer a question, not to execute the worktree's install
  hooks a second time; skipping them also keeps the probe strictly less
  privileged than the step it guards.

It performs a REAL install into a disposable directory rather than
``npm ci --dry-run``, and that is not a preference. A dry run does not attempt
retrieval at all: against a lockfile pinning a tarball that 404s, measured,
``npm ci --dry-run --ignore-scripts`` exits 0 and reports "added 1 package"
while the same command without ``--dry-run`` exits 1 on the missing tarball. A
dry run would therefore pass exactly the case this module exists to catch, so
the probe has to fetch. The cost of that honesty is one script-free install per
sync -- seconds against a warm cache -- which is cheap next to the emptied
``node_modules`` it prevents.

The flags otherwise MIRROR the real step exactly. A probe that resolves
differently from the install is worse than no probe: it either passes what will
fail, or fails what would have worked. ``--no-audit``/``--no-fund`` are the only
additions, and neither participates in resolution.

The distinct exit codes do NOT drive a cross-process protocol: the runner only
tests non-zero, and nothing outside this module reads which code came back. What
the classification is for is :func:`explain` -- turning a failure into one
registry-neutral sentence the dashboard can show instead of npm's log-file
pointer. Keeping the codes separate is what makes that sentence specific, and
what lets a caller tell a host condition (a full scratch filesystem) from a
lockfile that genuinely cannot be installed.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import subprocess  # nosec B404 - probing npm/git is this module's purpose
import sys
import tempfile
from pathlib import Path

#: Line prefix the probe uses for a human-readable detail line in the run log.
#: This is LOG TEXT ONLY -- it is never promoted into the authoritative failure
#: diagnosis. That distinction is a security boundary: the run's stdout also
#: carries worktree-controlled build output, so any in-band marker there can be
#: forged by an install script printing the same prefix and then failing. The
#: diagnosis therefore travels as an EXIT CODE, which a child of a step cannot
#: forge, and the gateway maps it to text through :func:`explain`.
DETAIL_PREFIX = "preflight: "

#: The probe found the incoming lockfile installable.
EXIT_OK = 0
#: The registry refused to authenticate us (npm ``E401``/``E403``) -- the one
#: failure an operator can act on directly, by refreshing their credential.
EXIT_AUTH = 41
#: Something else went wrong that we could not classify.
EXIT_FAILED = 42
#: A network-shaped failure -- the one class where a later retry can differ.
EXIT_TRANSIENT = 43
#: A package version the lockfile pins is not obtainable (npm ``E404``). On a
#: curated mirror this is what a blocked version looks like, so it is NOT an
#: auth problem, and refreshing a credential would not make the version appear.
EXIT_UNAVAILABLE = 44
#: The scratch filesystem ran out of room. Because the probe performs a REAL
#: install it needs about as much space as a ``node_modules`` tree, and it takes
#: that from ``TMPDIR`` (which the build environment's allowlist passes
#: through). Its own class so a full temp filesystem reads as a host condition
#: rather than a lockfile that cannot be installed.
EXIT_NO_SPACE = 45
#: The sync runner found a dependency tree AND a leftover backup of one, and
#: cannot tell which is complete. Owned by the runner rather than the probe, but
#: numbered here so ONE table maps every code the sync can exit with to text.
EXIT_TREE_AMBIGUOUS = 46
#: The runner could not put a stashed dependency tree back after a failed step.
EXIT_RESTORE_FAILED = 47

#: The checkout subdirectory holding the frontend half. A ``probe()`` parameter
#: once carried this, but only ``main()`` ever called it and it never passed one
#: -- the same reason the CLI's own ``--subdir`` flag was removed.
_FRONTEND_SUBDIR = "website"

#: Files the probe needs from the incoming ref to resolve the same way the real
#: step will. ``.npmrc`` matters as much as the lockfile: it carries settings
#: that change resolution (a minimum-release-age gate, for one), so omitting it
#: would make the probe answer a different question than the install.
_PROBE_FILES = ("package-lock.json", "package.json", ".npmrc")

#: Ordered classification. First match wins, so the specific auth and
#: not-found signals are tested before the generic network ones.
_SIGNALS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (
        EXIT_AUTH,
        re.compile(
            r"\bE401\b|\bE403\b|\bEAUTHUNKNOWN\b|\bENEEDAUTH\b"
            r"|unable to authenticate|401 unauthorized|403 forbidden"
            r"|authentication token seems to be invalid",
            re.I,
        ),
    ),
    (EXIT_UNAVAILABLE, re.compile(r"\bE404\b|404 not found", re.I)),
    (EXIT_NO_SPACE, re.compile(r"\bENOSPC\b|no space left on device", re.I)),
    (
        EXIT_TRANSIENT,
        re.compile(
            r"\bETIMEDOUT\b|\bENOTFOUND\b|\bECONNRESET\b|\bECONNREFUSED\b"
            r"|\bEAI_AGAIN\b|\bERR_SOCKET_TIMEOUT\b|network timeout|socket hang up",
            re.I,
        ),
    ),
)

#: Human-facing, registry-neutral explanations. These are what the dashboard
#: shows instead of npm's last output line, which is its "a complete log of this
#: run can be found in ..." pointer — the least informative line it prints.
_EXPLAIN = {
    EXIT_AUTH: (
        "the package registry rejected our credentials, so the incoming "
        "lockfile cannot be installed — refresh the registry credential and "
        "press Pull + Build again"
    ),
    EXIT_UNAVAILABLE: (
        "a package version the incoming lockfile pins is not available from "
        "the configured registry"
    ),
    EXIT_TRANSIENT: (
        "the package registry could not be reached, so the incoming lockfile "
        "could not be verified — try again in a moment"
    ),
    EXIT_NO_SPACE: (
        "not enough room in the scratch directory to verify the incoming "
        "lockfile — free space in the temporary directory and press Pull + "
        "Build again"
    ),
    EXIT_TREE_AMBIGUOUS: (
        "a previous sync left a dependency-tree backup beside the tree, and "
        "which one is complete cannot be told from disk — remove whichever you "
        "do not want to keep (the log lists both paths), then press Pull + "
        "Build again"
    ),
    EXIT_RESTORE_FAILED: (
        "the dependency tree could not be restored automatically; both the "
        "partial tree and its backup were left in place — see the log for both "
        "paths"
    ),
    EXIT_FAILED: "the incoming lockfile could not be installed",
}


def classify(output: str) -> int:
    """Map npm's own diagnostics onto one of the ``EXIT_*`` codes.

    Reads npm's error CODES rather than guessing from the registry URL, so the
    result is the same whichever registry is configured. Unrecognized failures
    are ``EXIT_FAILED``, never ``EXIT_TRANSIENT``: calling an unknown failure
    "transient" invites a retry that cannot help and hides the real cause.
    """
    for code, pattern in _SIGNALS:
        if pattern.search(output):
            return code
    return EXIT_FAILED


#: Every code this module can explain. The sync runner needs this set because an
#: exit code is only trustworthy from a step whose binary is OURS: a step running
#: worktree-controlled code (an npm lifecycle script, a vite config) can exit any
#: number it likes, and a forged 41 would make the dashboard assert a registry
#: credential failure -- with a remedy -- for what was actually a build error. So
#: the runner remaps a reserved code coming from any step other than the probe.
RESERVED_EXIT_CODES = frozenset(_EXPLAIN)


def explain_exit(rc: int) -> str:
    """The sentence for a run's exit code, or ``""`` when we own no diagnosis.

    Deliberately NOT a fallback: :func:`explain` answers ``EXIT_FAILED`` for
    anything it does not recognise, which is right when a probe has already
    decided the failure is its own. Here the input is the exit code of an
    arbitrary step -- ``npm ci`` exiting 1, a compile error, a killed process --
    and inventing "the incoming lockfile could not be installed" for those would
    state a cause that was never established. Only codes this module assigns get
    a sentence; everything else leaves the dashboard on its existing fallback.
    """
    return _EXPLAIN[rc] if rc in _EXPLAIN else ""


def _os_error_code(exc: OSError) -> int:
    """Classify an OSError from the probe's own filesystem work.

    Every write the probe makes lands in ``TMPDIR``, and because the probe
    performs a REAL install that directory can fill. An uncaught OSError would
    kill the step with a traceback and no classified cause -- which puts the
    dashboard back to showing whatever the last output line happened to be, the
    exact defect this module exists to remove. So the probe's own IO is mapped
    to a code here, in ONE place, rather than guarded a site at a time.
    """
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return EXIT_NO_SPACE
    return EXIT_FAILED


def _extract(git: str, repo: str, ref: str, subdir: str, dest: Path) -> tuple[int, str] | None:
    """Copy the probe files out of *ref* into *dest*.

    Reads from the fetched ref, NOT the working tree — that is what lets this
    run before the merge. ``package-lock.json`` is required; the others are
    optional because a checkout may legitimately not carry them.

    Returns ``(code, detail)`` on failure, or ``None`` on success. It returns a
    CODE rather than only a message because one of its failure modes is a full
    scratch filesystem, which is a host condition and not a lockfile that cannot
    be installed -- the caller must be able to tell those apart.
    """
    for name in _PROBE_FILES:
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no shell
                [git, "-C", repo, "show", f"{ref}:{subdir}/{name}"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return EXIT_TRANSIENT, f"reading {subdir}/{name} from {ref} timed out"
        except OSError as exc:
            return _os_error_code(exc), f"could not run git: {exc}"
        if proc.returncode != 0:
            if name == "package-lock.json":
                return EXIT_FAILED, (
                    f"cannot read {subdir}/{name} from {ref} "
                    f"({(proc.stderr or b'').decode(errors='replace').strip()})"
                )
            continue
        try:
            (dest / name).write_bytes(proc.stdout)
        except OSError as exc:
            return _os_error_code(exc), f"could not write {name} to the scratch dir: {exc}"
    return None


def probe(
    *,
    git: str,
    npm: str,
    repo: str,
    ref: str,
    timeout: int = 900,
) -> tuple[int, str]:
    """Report whether *ref*'s lockfile is installable. Returns (code, detail).

    Runs a REAL script-free install in a scratch directory, so it neither reads
    nor writes the checkout's own ``node_modules``. Both halves of that matter:
    a dry run never fetches, so it cannot answer the question at all; and an
    already-populated tree would make even a real install report only the delta
    against it, passing a lockfile that a delete-first ``npm ci`` cannot
    install.
    """
    try:
        tmp = Path(tempfile.mkdtemp(prefix="kirocrew-npm-preflight-"))
    except OSError as exc:
        # Creating the scratch directory is the FIRST thing that can fail on a
        # full or unwritable TMPDIR, and an uncaught OSError here would kill the
        # step with a traceback and no classified cause -- so the dashboard would
        # be back to showing whatever the last output line happened to be, which
        # is the defect this module exists to remove.
        return _os_error_code(exc), f"could not create a scratch directory: {exc}"
    try:
        failure = _extract(git, repo, ref, _FRONTEND_SUBDIR, tmp)
        if failure:
            return failure
        try:
            proc = subprocess.run(  # nosec B603 - argv list, no shell
                [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=str(tmp),
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "npm_config_update_notifier": "false"},
            )
        except subprocess.TimeoutExpired:
            return EXIT_TRANSIENT, f"probe timed out after {timeout}s"
        except OSError as exc:
            return EXIT_FAILED, f"could not run npm: {exc}"
        if proc.returncode == 0:
            return EXIT_OK, ""
        blob = "\n".join(
            (
                (proc.stdout or b"").decode(errors="replace"),
                (proc.stderr or b"").decode(errors="replace"),
            )
        )
        code = classify(blob)
        return code, _first_error_line(blob) or f"npm exited {proc.returncode}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _first_error_line(blob: str) -> str:
    """The first line that names the failure, for the operator-facing detail.

    npm prints its diagnosis FIRST and its log-file pointer LAST, which is
    exactly why the dashboard's "last output line" was uninformative. Taking
    the first error-ish line inverts that. The log-pointer line is skipped
    explicitly so it can never win when it is the only match.
    """
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or "complete log of this run" in line:
            continue
        low = line.lower()
        if low.startswith(("npm error", "npm err!", "error:")) or " error " in low:
            return line[:400]
    return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: the sync runs this as one step of the Pull+Build run."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--git", required=True)
    ap.add_argument("--npm", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", required=True)
    # --subdir and --timeout were CLI flags no caller passed. The subdir is now
    # _FRONTEND_SUBDIR and the timeout is probe()'s own default, so the surface
    # matches the one real invocation.
    args = ap.parse_args(argv)
    code, detail = probe(
        git=args.git,
        npm=args.npm,
        repo=args.repo,
        ref=args.ref,
    )
    if code == EXIT_OK:
        print(f"{DETAIL_PREFIX}incoming lockfile is installable", flush=True)
        return EXIT_OK
    # The DIAGNOSIS travels as the exit code, which the gateway maps through
    # explain_exit(). Only a human-readable detail goes to stdout, and it is log
    # text -- deliberately NOT an in-band marker the gateway promotes, because
    # this same stream carries worktree-controlled build output that could print
    # any marker it liked and then fail.
    print(f"{DETAIL_PREFIX}{explain_exit(code)}", flush=True)
    if detail:
        print(f"{DETAIL_PREFIX}{detail}", flush=True)
    return code


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())

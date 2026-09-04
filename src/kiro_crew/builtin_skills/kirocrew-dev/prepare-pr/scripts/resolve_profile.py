#!/usr/bin/env python3
"""resolve_profile.py - resolve the prepare-pr project profile for a repo.

Emits the resolved profile as JSON on stdout so the prepare-pr skill can read
setup / gates / reviewers / conventions from DATA instead of hardcoding one
project's conventions in prose.

Resolution order (most-specific-wins):
  1. .prepare-pr.toml at the repo root   -> explicit config
  2. KiroCrew markers present            -> bundled profiles/kirocrew.json
  3. a detectable stack                  -> auto-detected gates + reviewers
     (pyproject / package.json /            globbed from .github/workflows
      Cargo.toml / go.mod / Makefile)
  4. nothing detectable                  -> generic fallback (empty profile;
                                            the other scripts still work)

Every resolved profile has the SAME shape:
  {
    "source":        "config" | "kirocrew" | "auto-detect" | "generic",
    "base_branch":   str | null,
    "single_commit": bool,
    "setup":         [str, ...],
    "gates":         [str, ...],
    "rule_files":    [str, ...],
    "reviewers":     [{"name","model","model_tier","contract","rubric"}, ...],
    "readiness":     {"status_context": str | null, "defer_label": str | null}
  }

Stdlib only; Python 3.10+ (the package floor), no 3.11-only syntax. Parsing an external .prepare-pr.toml needs
tomllib (Python 3.11+) or an importable `tomli`; on older interpreters without
either, a PRESENT .prepare-pr.toml is a hard error (exit 2) rather than being
silently ignored (so the profile is never quietly wrong).

Usage:  python3 resolve_profile.py [repo_root] [base_ref]
        (repo defaults to git toplevel or CWD; base_ref pins every profile
        input. When omitted, the remote default branch is used if one exists;
        only a repo with no remote is read from the working tree.)
Exit:   0 resolved (JSON on stdout) - 2 env / parse error
"""

import fnmatch
import importlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(os.path.dirname(HERE), "profiles")

# Files whose combined presence identifies the KiroCrew repo (or a faithful
# fork) with a very low false-positive rate. All must be present to match.
_KIROCREW_MARKERS = (
    "AUTOSDE.yaml",
    ".github/workflows/codex-review.yml",
    ".github/workflows/claude-review.yml",
)


def err(msg):
    sys.stderr.write(msg + "\n")


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError:
        return 127, "", ""


def find_repo_root(start):
    """Return the git toplevel for ``start``, else ``start`` itself."""
    rc, out, _ = run(["git", "-C", start, "rev-parse", "--show-toplevel"])
    if rc == 0 and out:
        return out
    return start


def _as_bool(value):
    """Coerce a profile value to bool WITHOUT the ``bool("false") is True`` trap.

    A TOML/JSON author who writes ``single_commit = "false"`` (a string) must not
    silently enable the destructive squash + force-push path.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _safe_regular_file(path):
    """True only for an existing, non-symlink regular file.

    Profile inputs (.prepare-pr.toml, package.json) are read as data; refusing
    symlinks stops a symlinked path from redirecting the read at a file outside
    the repo (e.g. a credential file). Stdlib-only, so the script stays portable
    to repos without kiro_crew installed (it cannot import the gateway helpers).
    """
    return os.path.isfile(path) and not os.path.islink(path)


def load_toml(path):
    """Parse a TOML file to a dict. Raises RuntimeError if no parser exists."""
    toml = None
    # tomllib is stdlib on 3.11+; tomli is an optional backport for older ones.
    # Probe via import_module so there is no redefined bare `import X as _toml`.
    for name in ("tomllib", "tomli"):
        try:
            toml = importlib.import_module(name)
            break
        except ImportError:
            continue
    if toml is None:
        raise RuntimeError(
            "found {} but this Python has no TOML parser "
            "(need 3.11+ tomllib or the `tomli` package)".format(path)
        )
    with open(path, "rb") as fh:
        return toml.load(fh)


def load_toml_bytes(data, source):
    """Parse TOML bytes obtained from a pinned git object."""
    toml = None
    for name in ("tomllib", "tomli"):
        try:
            toml = importlib.import_module(name)
            break
        except ImportError:
            continue
    if toml is None:
        raise RuntimeError(
            "found {} but this Python has no TOML parser "
            "(need 3.11+ tomllib or the `tomli` package)".format(source)
        )
    return toml.loads(data.decode("utf-8"))


def normalize(raw, source):
    """Coerce a raw profile dict into the canonical shape with defaults.

    Accepts both the JSON bundled-profile shape (top-level ``setup`` / ``gates`` /
    ``reviewers`` / ``rule_files`` / ``readiness``) and the TOML
    ``.prepare-pr.toml`` shape ([project], [setup].commands, [gates].commands,
    [review] with [[review.reviewers]], [readiness]).
    """
    proj = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    base_branch = raw.get("base_branch", proj.get("base_branch"))
    single_commit = _as_bool(raw.get("single_commit", proj.get("single_commit", False)))

    setup = raw.get("setup")
    if isinstance(setup, dict):  # TOML [setup].commands
        setup = setup.get("commands")
    setup = list(setup or [])

    gates = raw.get("gates")
    if isinstance(gates, dict):  # TOML [gates].commands
        gates = gates.get("commands")
    gates = list(gates or [])

    review = raw.get("review") if isinstance(raw.get("review"), dict) else None
    if review is not None:
        rule_files = list(review.get("rule_files") or [])
        reviewers_raw = review.get("reviewers") or []
    else:
        rule_files = list(raw.get("rule_files") or [])
        reviewers_raw = raw.get("reviewers") or []

    reviewers = []
    for r in reviewers_raw:
        reviewers.append(
            {
                "name": r.get("name"),
                "model": r.get("model"),
                "model_tier": r.get("model_tier"),
                "contract": r.get("contract"),
                "rubric": r.get("rubric"),
            }
        )

    rd = raw.get("readiness") if isinstance(raw.get("readiness"), dict) else {}
    readiness = {
        "status_context": rd.get("status_context"),
        "defer_label": rd.get("defer_label"),
    }

    return {
        "source": source,
        "base_branch": base_branch,
        "single_commit": single_commit,
        "setup": setup,
        "gates": gates,
        "rule_files": rule_files,
        "reviewers": reviewers,
        "readiness": readiness,
    }


def _ref_has(root, base_ref, rel):
    """True iff ``rel`` exists as a blob at ``base_ref`` (repo-relative, '/')."""
    rc, _, _ = run(["git", "-C", root, "cat-file", "-e", "{}:{}".format(base_ref, rel)])
    return rc == 0


def _ref_read(root, base_ref, rel):
    """Blob content at ``base_ref:rel``, or None when absent."""
    rc, out, _ = run(["git", "-C", root, "show", "{}:{}".format(base_ref, rel)])
    return out if rc == 0 else None


def _ref_ls(root, base_ref, reldir):
    """Repo-relative blob paths under ``reldir`` at ``base_ref`` ([] when absent)."""
    rc, out, _ = run(
        ["git", "-C", root, "ls-tree", "--name-only", base_ref, reldir.rstrip("/") + "/"]
    )
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


class TreeReader:
    """Read-only view of repository files, either from a pinned git ref or the worktree."""

    def has(self, rel: str) -> bool:
        """True iff ``rel`` exists (repo-relative path, '/')."""
        raise NotImplementedError

    def read(self, rel: str) -> str | None:
        """Text content of ``rel`` (repo-relative path), or None when absent."""
        raise NotImplementedError

    def ls(self, reldir: str) -> list[str]:
        """Repo-relative paths directly under ``reldir`` ([] when absent)."""
        raise NotImplementedError


class PinnedTreeReader(TreeReader):
    """Read files from a pinned git ref (e.g. ``origin/main``)."""

    def __init__(self, root: str, base_ref: str):
        self.root = root
        self.base_ref = base_ref

    def has(self, rel: str) -> bool:
        return _ref_has(self.root, self.base_ref, rel)

    def read(self, rel: str) -> str | None:
        return _ref_read(self.root, self.base_ref, rel)

    def ls(self, reldir: str) -> list[str]:
        return _ref_ls(self.root, self.base_ref, reldir)


class WorktreeReader(TreeReader):
    """Read files from the local filesystem checkout."""

    def __init__(self, root: str):
        self.root = root

    def has(self, rel: str) -> bool:
        return os.path.exists(os.path.join(self.root, rel))

    def read(self, rel: str) -> str | None:
        path = os.path.join(self.root, rel)
        if _safe_regular_file(path):
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        return None

    def ls(self, reldir: str) -> list[str]:
        d = os.path.join(self.root, reldir)
        if not os.path.isdir(d):
            return []
        paths = []
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                paths.append(os.path.relpath(p, self.root).replace("\\", "/"))
        return paths


def detect_kirocrew(reader: TreeReader) -> bool:
    """True iff all Kiro Crew marker files are present at the given tree reader."""
    return all(reader.has(m) for m in _KIROCREW_MARKERS)


def load_bundled(name):
    """Load a bundled profile (profiles/<name>.json) as a dict."""
    path = os.path.join(PROFILES_DIR, name + ".json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def detect_gates(reader: TreeReader) -> list[str]:
    """Infer local gate commands from ecosystem marker files."""
    gates = []
    if reader.has("pyproject.toml") or reader.has("setup.cfg"):
        gates.append("python -m pytest -q")
    scripts: dict = {}
    try:
        pkg_text = reader.read("package.json")
        if pkg_text is not None:
            scripts = (json.loads(pkg_text) or {}).get("scripts") or {}
    except (OSError, ValueError):
        scripts = {}
    if "build" in scripts:
        gates.append("npm run build")
    if "test" in scripts:
        gates.append("npm test")
    if reader.has("Cargo.toml"):
        gates.append("cargo test")
    if reader.has("go.mod"):
        gates.append("go test ./...")
    if not gates and reader.has("Makefile"):
        gates.append("make test")
    return gates


def detect_reviewers(reader: TreeReader) -> list[dict]:
    """One contract-backed reviewer per .github/workflows/*review*.{yml,yaml}."""
    reviewers = []
    for rel in reader.ls(".github/workflows"):
        name = os.path.basename(rel)
        if not (fnmatch.fnmatch(name, "*review*.yml") or fnmatch.fnmatch(name, "*review*.yaml")):
            continue
        reviewers.append(
            {
                "name": os.path.splitext(name)[0],
                "model": None,
                "model_tier": None,
                "contract": rel,
                "rubric": None,
            }
        )
    return reviewers


def resolve(root, base_ref=None):
    """Apply the resolution order and return a normalized profile dict.

    With ``base_ref``, EVERY profile input -- the config file, the Kiro Crew
    markers, and gate/reviewer auto-detection -- is read from that pinned ref,
    never the branch checkout, so a branch under review cannot edit its own
    review authority. Without it (standalone CLI use), the working tree is
    read as before.
    """
    if base_ref:
        # An unresolvable ref is a hard error, never a silent fallback that
        # would quietly hand resolution back to the branch checkout.
        rc, _, _ = run(["git", "-C", root, "rev-parse", "--verify", base_ref + "^{commit}"])
        if rc != 0:
            raise RuntimeError("cannot resolve base ref {!r}".format(base_ref))
        reader: TreeReader = PinnedTreeReader(root, base_ref)
    else:
        reader = WorktreeReader(root)

    raw = reader.read(".prepare-pr.toml")
    raw_config = None
    if raw is not None:
        source_name = (
            "{}:.prepare-pr.toml".format(base_ref)
            if base_ref
            else os.path.join(root, ".prepare-pr.toml")
        )
        raw_config = load_toml_bytes(raw.encode("utf-8"), source_name)

    if raw_config is not None:
        profile = normalize(raw_config, "config")
        # A partial config must not silently blank out gates/reviewers (which
        # would make the Phase 2 local gate a no-op) — fill any omitted section
        # from auto-detection.
        if not profile["gates"]:
            profile["gates"] = detect_gates(reader)
        if not profile["reviewers"]:
            profile["reviewers"] = detect_reviewers(reader)
        return profile
    if detect_kirocrew(reader):
        return normalize(load_bundled("kirocrew"), "kirocrew")
    gates = detect_gates(reader)
    reviewers = detect_reviewers(reader)
    if gates or reviewers:
        return normalize({"gates": gates, "reviewers": reviewers}, "auto-detect")
    return normalize({}, "generic")


def default_base_ref(root):
    """Trusted default base for CLI use: git's own record of the remote default
    branch, else origin/main when it resolves, else None (no remote to pin to,
    so the working tree is the only source there is)."""
    rc, out, _ = run(["git", "-C", root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out:
        return out
    rc, _, _ = run(["git", "-C", root, "rev-parse", "--verify", "origin/main^{commit}"])
    if rc == 0:
        return "origin/main"
    return None


def main(argv):
    start = argv[1] if len(argv) > 1 else os.getcwd()
    base_ref = argv[2] if len(argv) > 2 else None
    root = find_repo_root(start)
    if base_ref is None:
        # Never default to the branch checkout when there is a remote base to
        # pin to -- the checkout is the input profile resolution distrusts.
        base_ref = default_base_ref(root)
    try:
        profile = resolve(root, base_ref=base_ref)
    except RuntimeError as exc:
        err("ERROR: " + str(exc))
        return 2
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        err("ERROR: could not resolve profile: " + str(exc))
        return 2
    print(json.dumps(profile, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

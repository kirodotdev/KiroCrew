#!/usr/bin/env python3
"""resolve_profile.py - resolve the prepare-pr project profile for a repo.

Emits the resolved profile as JSON on stdout so the prepare-pr skill can read
gates / reviewers / conventions from DATA instead of hardcoding one project's
conventions in prose.

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
    "gates":         [str, ...],
    "rule_files":    [str, ...],
    "reviewers":     [{"name","model","model_tier","contract","rubric"}, ...],
    "readiness":     {"status_context": str | null, "defer_label": str | null}
  }

Stdlib only; Python 3.10+ (the package floor), no 3.11-only syntax. Parsing an external .prepare-pr.toml needs
tomllib (Python 3.11+) or an importable `tomli`; on older interpreters without
either, a PRESENT .prepare-pr.toml is a hard error (exit 2) rather than being
silently ignored (so the profile is never quietly wrong).

Usage:  python3 resolve_profile.py [repo_root]   (default: git toplevel or CWD)
Exit:   0 resolved (JSON on stdout) - 2 env / parse error
"""
import glob
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
        p = subprocess.run(args, capture_output=True, text=True)
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


def normalize(raw, source):
    """Coerce a raw profile dict into the canonical shape with defaults.

    Accepts both the JSON bundled-profile shape (top-level ``gates`` /
    ``reviewers`` / ``rule_files`` / ``readiness``) and the TOML
    ``.prepare-pr.toml`` shape ([project], [gates].commands, [review] with
    [[review.reviewers]], [readiness]).
    """
    proj = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    base_branch = raw.get("base_branch", proj.get("base_branch"))
    single_commit = _as_bool(raw.get("single_commit", proj.get("single_commit", False)))

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
        "gates": gates,
        "rule_files": rule_files,
        "reviewers": reviewers,
        "readiness": readiness,
    }


def detect_kirocrew(root):
    """True iff all KiroCrew marker files are present at ``root``."""
    return all(os.path.exists(os.path.join(root, m)) for m in _KIROCREW_MARKERS)


def load_bundled(name):
    """Load a bundled profile (profiles/<name>.json) as a dict."""
    path = os.path.join(PROFILES_DIR, name + ".json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def detect_gates(root):
    """Infer local gate commands from ecosystem marker files."""

    def has(rel):
        return os.path.exists(os.path.join(root, rel))

    gates = []
    if has("pyproject.toml") or has("setup.cfg"):
        gates.append("python -m pytest -q")
    pkg = os.path.join(root, "package.json")
    if _safe_regular_file(pkg):
        try:
            with open(pkg, "r", encoding="utf-8") as fh:
                scripts = (json.load(fh) or {}).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        if "build" in scripts:
            gates.append("npm run build")
        if "test" in scripts:
            gates.append("npm test")
    if has("Cargo.toml"):
        gates.append("cargo test")
    if has("go.mod"):
        gates.append("go test ./...")
    if not gates and has("Makefile"):
        gates.append("make test")
    return gates


def detect_reviewers(root):
    """One contract-backed reviewer per .github/workflows/*review*.{yml,yaml}."""
    wf = os.path.join(root, ".github", "workflows")
    reviewers = []
    if os.path.isdir(wf):
        paths = glob.glob(os.path.join(wf, "*review*.yml"))
        paths += glob.glob(os.path.join(wf, "*review*.yaml"))
        for path in sorted(set(paths)):
            reviewers.append(
                {
                    "name": os.path.splitext(os.path.basename(path))[0],
                    "model": None,
                    "model_tier": None,
                    "contract": os.path.relpath(path, root),
                    "rubric": None,
                }
            )
    return reviewers


def resolve(root):
    """Apply the resolution order and return a normalized profile dict."""
    toml_path = os.path.join(root, ".prepare-pr.toml")
    if _safe_regular_file(toml_path):
        profile = normalize(load_toml(toml_path), "config")
        # A partial config must not silently blank out gates/reviewers (which
        # would make the Phase 2 local gate a no-op) — fill any omitted section
        # from auto-detection.
        if not profile["gates"]:
            profile["gates"] = detect_gates(root)
        if not profile["reviewers"]:
            profile["reviewers"] = detect_reviewers(root)
        return profile
    if detect_kirocrew(root):
        return normalize(load_bundled("kirocrew"), "kirocrew")
    gates = detect_gates(root)
    reviewers = detect_reviewers(root)
    if gates or reviewers:
        return normalize({"gates": gates, "reviewers": reviewers}, "auto-detect")
    return normalize({}, "generic")


def main(argv):
    start = argv[1] if len(argv) > 1 else os.getcwd()
    root = find_repo_root(start)
    try:
        profile = resolve(root)
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

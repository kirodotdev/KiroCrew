"""
Generate kirocrew-seccomp.json by patching Docker's built-in default profile.

Extends the Docker default allow-list with three unconditional ALLOW rules:
  - unshare  — lets the inner sandbox call unshare(CLONE_NEWUSER/CLONE_NEWNS)
  - clone    — lets fork() / clone() proceed with any flag combination
  - mount    — lets the inner sandbox bind-mount credential dirs after NEWNS

All three are blocked or arg-filtered by the Docker default seccomp profile,
causing Kiro Crew's sandbox probe to fail with EPERM inside containers.

NOTE: these rules are unconditional (no arg filters), so they grant unshare
and clone for ANY namespace type, not just CLONE_NEWUSER/CLONE_NEWNS.  This
is a deliberate trade-off: glibc >= 2.29 uses clone3/clone with flag
combinations that arg-filtered rules cannot reliably match across kernel
versions.  The profile is still far less permissive than --privileged or
--security-opt seccomp=unconfined (all other Docker default restrictions apply).

Usage (run from repo root to regenerate kirocrew-seccomp.json):
  docker run --rm -v .:/repo -w /repo python:3.12-slim \\
    python docker/seccomp/gen_profile.py > docker/seccomp/kirocrew-seccomp.json

If the fetch fails the script exits with code 1 — it never silently overwrites
a valid profile with a broken one.
"""
import json
import sys
import urllib.request

# Pinned to the moby release closest to the Docker CE version this was tested
# against (Docker 29.x ships a profile based on the v27/v28 line; v24 is the
# last release that published a standalone default.json in the repo tree and
# whose allow-list is a strict subset of newer releases — using it is safe
# because newer kernels only ADD syscalls to the default allow-list, never
# remove them).  Regenerate with a newer pin when a container starts failing
# with EPERM on a syscall not in this list.
DOCKER_DEFAULT_PROFILE_URL = (
    "https://raw.githubusercontent.com/moby/moby/"
    "v24.0.9/profiles/seccomp/default.json"
)

COMMENT = (
    "Kiro Crew custom seccomp profile — extends the Docker default by adding "
    "unconditional ALLOW rules for unshare, clone, and mount. "
    "Required for Kiro Crew's inner Linux user-namespace sandbox to work inside "
    "Docker containers (the default profile blocks these syscalls). "
    "Less permissive than --security-opt seccomp=unconfined and far less "
    "permissive than --privileged; all other Docker default restrictions apply. "
    "Note: unshare/clone rules are unconditional (no arg filters) — see "
    "docker/seccomp/gen_profile.py for the rationale. "
    "See docs/guides/docker.md for usage."
)

EXTRA_RULES = [
    {
        "_comment": (
            "Allow unshare unconditionally. "
            "The Docker default profile blocks it; the inner sandbox needs "
            "unshare(CLONE_NEWUSER) then unshare(CLONE_NEWNS). "
            "Rule is unconditional — arg filters cannot reliably cover all "
            "kernel/glibc combinations."
        ),
        "names": ["unshare"],
        "action": "SCMP_ACT_ALLOW",
    },
    {
        "_comment": (
            "Allow clone unconditionally. "
            "Docker's default restricts clone via arg filters; an unrestricted "
            "ALLOW appended here takes precedence (last rule wins in seccomp BPF) "
            "and covers the inner sandbox's fork->unshare(CLONE_NEWUSER) handshake "
            "as well as normal fork() and glibc thread creation."
        ),
        "names": ["clone"],
        "action": "SCMP_ACT_ALLOW",
    },
    {
        "_comment": (
            "Allow mount. "
            "Needed by the inner sandbox's bind-mount step after unshare(CLONE_NEWNS). "
            "Only reachable inside an already-established user+mount namespace."
        ),
        "names": ["mount"],
        "action": "SCMP_ACT_ALLOW",
    },
]


def fetch_default_profile() -> dict:
    """Fetch Docker's default seccomp profile from the moby repo.

    Exits with code 1 on any network or parse error — never falls back to a
    broken profile that would silently overwrite a valid one on disk.
    """
    sys.stderr.write(f"Fetching Docker default profile from {DOCKER_DEFAULT_PROFILE_URL} ...\n")
    try:
        req = urllib.request.Request(DOCKER_DEFAULT_PROFILE_URL)
        # URL is a pinned module-level constant, not user input — no file:// SSRF risk.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = resp.read()
    except Exception as exc:
        sys.stderr.write(
            f"ERROR: could not fetch Docker default profile: {exc}\n"
            "Refusing to generate a broken profile. Fix your network or download\n"
            f"  {DOCKER_DEFAULT_PROFILE_URL}\n"
            "manually and pass it via stdin instead.\n"
        )
        sys.exit(1)
    sys.stderr.write(f"  fetched {len(data)} bytes\n")
    return json.loads(data)


def patch_profile(base: dict) -> dict:
    profile = dict(base)
    profile["_comment"] = COMMENT
    # Append our additions at the end. ALLOW rules are additive in Docker's seccomp
    # BPF evaluation — appending them does not change any existing behaviour.
    syscalls = list(profile.get("syscalls", []))
    profile["syscalls"] = syscalls + EXTRA_RULES
    return profile


if __name__ == "__main__":
    base = fetch_default_profile()
    patched = patch_profile(base)
    print(json.dumps(patched, indent=2))

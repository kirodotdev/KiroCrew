#!/usr/bin/env python3
"""Detach a per-app backend from the shared KiroCrew CloudFront distribution.

Inverse of attach_backend.py: removes the '<slug>/api/*' cache behavior and the
origin it targeted (only if no other behavior / the default still uses it).
Append-only-safe; preserves everything else. Use for cleanup after a backend
stack is deleted, or from the reaper.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile


def aws(profile, region, *args):
    cmd = ["aws"] + (["--profile", profile] if profile else []) + ["--region", region, *args]
    # Every AWS spawn from these LLM-facing helpers MUST route through the
    # sandbox chokepoint. An ImportError fallback that ran unsandboxed when
    # kiro_crew wasn't importable is exactly the environment an attacker would
    # arrange, so fail closed instead: standalone operators must run via the
    # package venv (pip install -e / the skill's documented invocation), never
    # bare python3 without kiro_crew on sys.path.
    try:
        from kiro_crew.sandbox import resource_limit_preexec, sandboxed_spawn_argv
    except ImportError:
        sys.stderr.write(
            "error: kiro_crew package not importable — refusing to spawn AWS "
            "commands unsandboxed. Run this script with the KiroCrew venv "
            "python (see skills/artifact-deploy/SKILL.md).\n"
        )
        sys.exit(1)
    wrapped_argv, env, cleanup = sandboxed_spawn_argv(cmd)
    _preexec = resource_limit_preexec()
    # Kernel RLIMIT ceiling on the child (fork bomb / FD / mem / CPU) — the
    # spawn-audit rule requires this on every sandbox-routed spawn.
    r = subprocess.run(  # noqa: S603
        wrapped_argv, capture_output=True, text=True, env=env, preexec_fn=_preexec)
    if cleanup:
        import os as _os
        try:
            _os.unlink(cleanup)
        except OSError:
            pass
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    return r.stdout


def _validate_args(profile: str, region: str, dist_id: str, slug: str) -> None:
    """Validate all argv before any aws call. Exit 2 on mismatch."""
    import re as _re
    _PROFILE_RE = _re.compile(r"^[a-zA-Z0-9._:/-]+$")
    _REGION_RE = _re.compile(r"^[a-z]{2}-[a-z]+-\d+$")
    _DIST_ID_RE = _re.compile(r"^[A-Z0-9]{13,14}$")
    _SLUG_RE = _re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

    errors = []
    if profile and not _PROFILE_RE.match(profile):
        errors.append(f"--profile: invalid format: {profile!r}")
    if not _REGION_RE.match(region):
        errors.append(f"--region: not a valid AWS region: {region!r}")
    if not _DIST_ID_RE.match(dist_id):
        errors.append(f"--dist-id: must match CloudFront distribution ID pattern (13-14 uppercase alphanumeric): {dist_id!r}")
    if not _SLUG_RE.match(slug):
        errors.append(f"--slug: must be lowercase alphanumeric + hyphens, 1-63 chars: {slug!r}")
    if errors:
        for e in errors:
            sys.stderr.write(f"error: {e}\n")
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--region", required=True)
    ap.add_argument("--dist-id", required=True)
    ap.add_argument("--slug", required=True)
    a = ap.parse_args()
    profile = a.profile or ""

    _validate_args(profile, a.region, a.dist_id, a.slug)
    path_pattern = f"{a.slug}/api/*"

    gdc = json.loads(aws(profile, a.region, "cloudfront", "get-distribution-config",
                         "--id", a.dist_id, "--output", "json"))
    etag = gdc["ETag"]
    cfg = gdc["DistributionConfig"]

    cbs = cfg.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    items = cbs.get("Items", [])
    target = None
    kept = []
    for b in items:
        if b.get("PathPattern") == path_pattern:
            target = b.get("TargetOriginId")
        else:
            kept.append(b)
    if target is None:
        print(f"no behavior {path_pattern}; nothing to detach")
        return
    cbs["Items"] = kept
    cbs["Quantity"] = len(kept)
    cfg["CacheBehaviors"] = cbs

    # drop the origin only if nothing else references it
    still_used = cfg["DefaultCacheBehavior"].get("TargetOriginId") == target or any(
        b.get("TargetOriginId") == target for b in kept
    )
    if not still_used:
        origins = cfg["Origins"]
        origins["Items"] = [o for o in origins["Items"] if o.get("Id") != target]
        origins["Quantity"] = len(origins["Items"])

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(cfg, tmp)
    try:
        aws(profile, a.region, "cloudfront", "update-distribution", "--id", a.dist_id,
            "--distribution-config", f"file://{tmp.name}", "--if-match", etag, "--output", "json")
    finally:
        os.unlink(tmp.name)
    print(f"detached: {path_pattern} (origin {target}{'' if still_used else ' removed'})")


if __name__ == "__main__":
    main()

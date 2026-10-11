#!/usr/bin/env python3
"""Build the MicroVM lane's code artifact: the Fargate recipes, zipped for Lambda.

    python scripts/build_microvm_image_zip.py --bundle <dir> --out <file.zip>

A CLI and nothing more. Every decision about what goes in the zip lives in
``kiro_crew.cloud.microvm.recipe``, which is also what the launcher calls on a
cache miss -- so an operator building by hand and a launch building for itself
produce the same bytes. Two assemblers would be two answers to "what is in a crew
image", and the looser one would be the one that shipped the wrong content.

What this script owns is the one thing a launch does not do: staging the wheel.
A launch runs from an installed Kiro Crew and the wheel is already beside the
recipes; an operator building from a checkout has to make it first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from kiro_crew.cloud.microvm import recipe as recipe_mod  # noqa: E402


def build_wheel(vendor: Path) -> Path:
    """Build the Kiro Crew wheel the base recipe installs, into ``runtime/vendor``.

    The same staging ``scripts/build_crew_base_image.sh`` does, and emptied first
    for the same reason: the base's install step refuses a context holding more
    than one wheel, which is what keeps its glob version-agnostic, so a stale
    wheel would fail the build with a confusing message instead of being
    replaced.

    setuptools' own ``build/lib`` copy of the staging directory is emptied too.
    Packaging rules no longer reach a staged wheel (see ``MANIFEST.in`` and
    ``[options.package_data]``), but ``build/lib`` is a cache that nothing
    prunes, so a copy left there by a build made BEFORE those rules were
    narrowed is archived into the next wheel all the same -- and the resulting
    failure names a byte ceiling rather than a stale directory.
    """
    vendor.mkdir(parents=True, exist_ok=True)
    for old in vendor.glob("*.whl"):
        old.unlink()
    src_root = REPO_ROOT / "src"
    if vendor.is_relative_to(src_root):
        cached = REPO_ROOT / "build" / "lib" / vendor.relative_to(src_root)
        for stale in cached.glob("*.whl"):
            stale.unlink()
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(vendor)],
        cwd=REPO_ROOT,
        check=True,
        encoding="utf-8",
    )
    wheels = sorted(vendor.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel in {vendor}, found {len(wheels)}")
    wheel = wheels[0]
    # The dashboard assets go, if this checkout has any. A crew in this lane serves
    # one turn route and a liveness answer, so they are dead weight in the guest --
    # and a wheel carrying them exceeds the recipe ceiling on its own, which makes
    # this the difference between a lane that launches and one that cannot.
    freed = recipe_mod.strip_dashboard(wheel)
    if freed:
        print(f"removed {freed} bytes of dashboard assets from {wheel.name}")
    return wheel


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path, help="packaging.build output dir")
    parser.add_argument("--out", required=True, type=Path, help="zip to write")
    parser.add_argument(
        "--base-image-arn",
        default="",
        help="the AWS-managed base image the build runs on; recorded in the recipe",
    )
    parser.add_argument("--skip-wheel", action="store_true", help="reuse the wheel already staged")
    args = parser.parse_args()

    vendor = recipe_mod.RUNTIME_DIR / "vendor"
    staged = sorted(vendor.glob("*.whl"))
    if args.skip_wheel:
        if not staged:
            raise SystemExit(f"--skip-wheel was passed and no wheel is staged in {vendor}")
        wheel = staged[0]
    else:
        wheel = build_wheel(vendor)

    try:
        assembled = recipe_mod.assemble(
            bundle_dir=args.bundle.resolve(),
            wheel=wheel,
            base_image_arn=args.base_image_arn or "arn:aws:lambda:::microvm-image/unset",
            out_zip=args.out,
        )
    except recipe_mod.RecipeRefused as exc:
        raise SystemExit(str(exc)) from exc

    print(
        json.dumps(
            {
                "zip": str(assembled.path),
                "zip_bytes": assembled.size_bytes,
                "zip_sha256": "sha256:" + hashlib.sha256(assembled.path.read_bytes()).hexdigest(),
                "base_recipe_digest": assembled.recipe.base_digest,
                "bundle_digest": assembled.recipe.bundle_digest,
                "recipe_digest": assembled.recipe.digest(),
                "image_name": assembled.recipe.image_name(),
                "wheel": wheel.name,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

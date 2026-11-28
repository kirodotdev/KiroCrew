#!/usr/bin/env python3
"""KAS Node watch: does nodejs.org have a newer Node than the KAS pin bundles?

`.github/workflows/kas-node-watch.yml` runs this weekly. The pinned Node is the
part of `KAS_PIN_VERSION` after `-node`, read from `kas_pin.py` with `ast`, never
by importing it. The newest release of the same major in nodejs.org's
`index.json` is compared as an integer tuple; a newer major does not count.

When the pin is behind, print the title of the issue that tracks it; write its
body too unless this watch already filed that title. When the pin is current,
print and write nothing. Both exit 0. A malformed pin or index exits 1.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PIN_NAME = "KAS_PIN_VERSION"
PIN_PATH = "src/kiro_crew/acp/kas_pin.py"
WORKFLOW_PATH = ".github/workflows/kas-node-watch.yml"


def parse_version(text: str) -> tuple[int, int, int]:
    """`22.23.4` or `v22.23.4` as an integer tuple; anything else is an error."""
    parts = text.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a release version: {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def pinned_node(pin_source: str) -> str:
    """The Node after `-node` in the top-level `KAS_PIN_VERSION` string."""
    for node in ast.parse(pin_source).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == PIN_NAME
        ):
            value = ast.literal_eval(node.value)
            if not isinstance(value, str) or "-node" not in value:
                raise ValueError(f"{PIN_NAME} has no -node<N> part: {value!r}")
            node_version = value.split("-node", 1)[1]
            parse_version(node_version)
            return node_version
    raise ValueError(f"no top-level {PIN_NAME} string assignment")


def newest_of_major(index: object, major: int) -> str:
    """The newest `X.Y.Z` of `major` in an index.json list, by integer tuple."""
    if not isinstance(index, list):
        raise ValueError("index is not a list")
    best: tuple[int, int, int] | None = None
    for entry in index:
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
            raise ValueError(f"index entry without a version string: {entry!r}")
        version = entry["version"]
        if not version.startswith(f"v{major}."):
            continue
        parsed = parse_version(version)
        if best is None or parsed > best:
            best = parsed
    if best is None:
        raise ValueError(f"index lists no Node {major}.x release")
    return ".".join(str(part) for part in best)


def issue_title(pinned: str, newest: str) -> str:
    return f"KAS pin bundles Node {pinned}; Node {newest} is out"


def issue_body(pinned: str, newest: str) -> str:
    major = pinned.split(".", 1)[0]
    return (
        f"The KAS pin bundles Node {pinned}, and nodejs.org now lists Node {newest}"
        f" as the newest {major}.x release.\n"
        "\n"
        f"The pin is `{PIN_NAME}` in `{PIN_PATH}`; its `-node<N>` part is the"
        ' bundled Node. To move it, follow the "Node patch release" row of'
        ' "When to rebuild" in `packaging/kas-al2/README.md`.\n'
        "\n"
        "This watch closes this issue itself once the pin is current, or when a newer"
        f" {major}.x release supersedes it. Closing it by hand without moving the pin"
        f" keeps the watch quiet about Node {newest} until the next {major}.x release.\n"
        "\n"
        f"<sub>Filed by `{WORKFLOW_PATH}`.</sub>\n"
    )


def due(pin_source: str, index: object) -> tuple[str, str] | None:
    """`(title, body)` of the issue that tracks a pin behind its major, else None."""
    pinned = pinned_node(pin_source)
    newest = newest_of_major(index, parse_version(pinned)[0])
    if parse_version(newest) <= parse_version(pinned):
        return None
    return issue_title(pinned, newest), issue_body(pinned, newest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pin-file", required=True, type=Path, help=PIN_PATH)
    parser.add_argument("--index", required=True, type=Path, help="a downloaded index.json")
    parser.add_argument(
        "--existing", required=True, type=Path, help="this watch's issue titles, one a line"
    )
    parser.add_argument("--body-out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        pin_source = args.pin_file.read_text(encoding="utf-8")
        index = json.loads(args.index.read_text(encoding="utf-8"))
        existing = [line.strip() for line in args.existing.read_text(encoding="utf-8").splitlines()]
        result = due(pin_source, index)
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"kas_node_watch: {exc}", file=sys.stderr)
        return 1
    if result is not None:
        title, body = result
        if title not in existing:
            args.body_out.write_text(body, encoding="utf-8")
        print(title)
    return 0


if __name__ == "__main__":
    sys.exit(main())

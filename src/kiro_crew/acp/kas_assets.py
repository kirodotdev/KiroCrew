"""Locate the KAS (kiro-agent) assets kiro-cli extracted.

KAS ships inside kiro-cli, which unpacks it on first run. Kiro Crew drives that
copy rather than distributing its own: the `kiro-team/kiro-agent` repository is
restricted-read, and an open-source release cannot hide its contents. Reading
files already on the user's disk distributes nothing.

Layout, as kiro-cli 2.18.0 actually lays it down::

    {data_dir}/node                      the extracted Node runtime (+ node.sha256)
    {data_dir}/kas/{ver}-{hash}/         one directory per bundle version
      node_modules/@kiro/agent/dist/server/acp-server.js

The bundle is staged as a trimmed ``node_modules`` tree, so the entry script sits
at the package's normal npm path rather than at the top of the version directory.

That layout is kiro-cli's INTERNAL detail and may change, so both halves accept
an environment override — a user whose kiro-cli moved the files can point Kiro
Crew at them instead of being hard-blocked.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Override for the Node interpreter used to run KAS.
ENV_KAS_NODE = "KIROCREW_KAS_NODE"
#: Override for the KAS server entry script.
ENV_KAS_SCRIPT = "KIROCREW_KAS_SCRIPT"

#: Node refuses to load KAS's web-tree-sitter grammar without this, and the
#: shell-command policy parser depends on it. kiro-cli passes it too.
KAS_NODE_FLAGS = ("--experimental-wasm-modules",)
#: KAS defaults to stdio, but state it so the transport is visible in ps output.
KAS_TRANSPORT_ARG = "--transport=stdio"

#: Auth mode: KAS keeps no refresh token and calls back over ACP
#: (``_kiro/auth/getAccessToken``) whenever it needs an access token. The
#: runtime answers that callback by shelling out to kiro-cli (see
#: :mod:`kas_auth`). The alternative -- omitting ``--auth`` -- makes KAS use its
#: file auth provider, which reads a ``~/.aws/sso/cache`` token that
#: ``kiro-cli login`` does NOT write, so a cli-only machine has no token there.
KAS_AUTH_ACP_CALLBACK_ARG = "--auth=acp-callback"

#: Where the entry script sits inside a version directory. Checked before any
#: recursive walk, because the staged tree holds hundreds of packages.
_SCRIPT_RELATIVE_PATHS = (
    Path("node_modules/@kiro/agent/dist/server/acp-server.js"),
    Path("node_modules/@kiro/agent/dist/server/acp-server.cjs"),
    Path("dist/server/acp-server.js"),
)
_SERVER_SCRIPT_NAMES = ("acp-server.js", "acp-server.cjs", "acp-server.standalone.cjs")


class KasAssetsMissing(RuntimeError):
    """Raised when neither an override nor an extracted bundle can be found."""


def _kiro_data_dirs() -> list[Path]:
    """Candidate kiro-cli data directories, most specific first.

    ``KIRO_DATA_DIR`` is kiro-cli's own override; the rest mirror where its
    ``data_dir()`` resolves per platform. ``kiro-cli`` comes before ``kiro``
    because that is the directory 2.18.0 writes.
    """
    candidates: list[Path] = []
    env_dir = os.environ.get("KIRO_DATA_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    home = Path.home()
    candidates += [
        home / ".local" / "share" / "kiro-cli",
        home / ".local" / "share" / "kiro",
        home / "Library" / "Application Support" / "kiro-cli",
        home / "Library" / "Application Support" / "kiro",
        home / ".kiro",
    ]
    return candidates


def find_kas_node() -> Path | None:
    """The Node interpreter to run KAS with, or None when unresolvable."""
    override = os.environ.get(ENV_KAS_NODE, "").strip()
    if override:
        return Path(override)
    for data_dir in _kiro_data_dirs():
        for name in ("node", "node.exe"):
            candidate = data_dir / name
            if candidate.is_file():
                return candidate
    return None


def find_kas_server_script() -> Path | None:
    """The KAS entry script, or None when unresolvable.

    Bundles are extracted one directory per version; pick the most recently
    modified so a kiro-cli upgrade takes effect without any bookkeeping here.
    """
    override = os.environ.get(ENV_KAS_SCRIPT, "").strip()
    if override:
        return Path(override)
    for data_dir in _kiro_data_dirs():
        kas_root = data_dir / "kas"
        if not kas_root.is_dir():
            continue
        try:
            versions = sorted(
                (p for p in kas_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for version_dir in versions:
            for relative in _SCRIPT_RELATIVE_PATHS:
                candidate = version_dir / relative
                if candidate.is_file():
                    return candidate
            # The staged tree holds hundreds of packages, so only walk it when
            # the package moved and the known paths above all missed.
            for name in _SERVER_SCRIPT_NAMES:
                for candidate in version_dir.rglob(name):
                    if candidate.is_file():
                        return candidate
    return None


def resolve_kas_entry() -> tuple[Path, Path]:
    """``(node, server_script)`` for spawning KAS.

    Raises :class:`KasAssetsMissing` with actionable guidance rather than
    degrading to a different backend — a silent fallback to kiro-cli would look
    like KAS working.
    """
    node = find_kas_node()
    script = find_kas_server_script()
    if node and script:
        return node, script
    missing = []
    if not node:
        missing.append(f"the Node runtime (set {ENV_KAS_NODE} to override)")
    if not script:
        missing.append(f"the KAS server script (set {ENV_KAS_SCRIPT} to override)")
    raise KasAssetsMissing(
        "Cannot locate " + " and ".join(missing) + ". KAS ships inside kiro-cli: "
        "install kiro-cli and run it once so it unpacks its bundle, then sign in "
        "with `kiro-cli login` so KAS can read the token."
    )


def build_kas_argv(node: Path, server_script: Path) -> list[str]:
    """argv for a KAS stdio session.

    Launched with ``--auth=acp-callback``: KAS keeps no refresh token and asks
    this host for an access token over ACP whenever it needs one. The runtime
    fulfils that ``_kiro/auth/getAccessToken`` callback by shelling out to
    ``kiro-cli chat _ get-kas-token`` (see :mod:`kas_auth`), so the refresh token
    never leaves kiro-cli's own store and this process only ever handles a
    short-lived access token in transit. This works on any machine where
    ``kiro-cli login`` succeeded -- unlike the file auth provider (omit
    ``--auth``), which needs a ``~/.aws/sso/cache`` token that the cli does not
    write.
    """
    return [
        str(node),
        *KAS_NODE_FLAGS,
        str(server_script),
        KAS_TRANSPORT_ARG,
        KAS_AUTH_ACP_CALLBACK_ARG,
    ]

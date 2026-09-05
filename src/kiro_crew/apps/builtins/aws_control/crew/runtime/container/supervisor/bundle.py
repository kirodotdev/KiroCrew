"""Install the crew bundle into the paths Kiro Crew actually reads, or refuse.

The crew rides in the image at ``/app/crew-bundle`` (PACKAGING-CONTRACT.md, T3).
The previous design uploaded it to S3 and nothing in the container ever read it,
so ten gates went green while the deployment served a default agent. This module
closes that by construction: the supervisor installs the bundle BEFORE the
backend starts, and refuses to boot unless the named crew is the one installed.

WHERE EACH ENTRY GOES -- verified against the Kiro Crew source at
a Kiro Crew source checkout (0.6.0), not inferred from a plausible
name. ``config_dir()`` and ``data_home()`` resolve to the SAME directory, so a
``<home>/config/`` guess would land two of these where nothing reads them:

* ``agent.json`` -> ``<kiro home>/agents/<crew_name>.json``.
  ``agent_discovery.list_agents`` (``agent_discovery.py:42`` ``_kiro_agents_dir``
  -> ``kiro_agents_dir()``; ``:55`` "reads and JSON-parses every
  ``~/.kiro/agents/*.json`` on each call") is THE reader of installed agent
  specs, keyed by the spec's ``name``. ``kiro_agents_dir()``
  (``config/paths.py:604``) delegates to ``ambient_agents_dir()``
  (``:588``) = ``kiro_home() / "agents"``, and ``kiro_home()`` (``:510``) is
  ``$KIRO_HOME`` or ``~/.kiro``. This is NOT under the data home and is NOT
  governed by ``KIROCREW_HOME``: the backend is launched with
  ``KIROCREW_HOME=data_home`` but no ``KIRO_HOME`` (``supervisor/backend.py``),
  so the spec lands under the process HOME. Resolved here the same way rather
  than imported, so this module needs no ``kiro_crew`` install (matching
  ``apply_sandbox_posture``'s minimal-read style).

* ``mcp.json`` -> ``<data home>/mcp.json``. The Kiro Crew-scope MCP config is
  ``data_home()/"mcp.json"`` -- the highest-priority source in
  ``mcp_discovery._mcp_sources`` (``mcp_discovery.py:297``,
  ``SCOPE_KIROCREW``) and what ``dashboard/handlers/mcp.py:1620``
  (``_kirocrew_mcp_json`` -> ``data_home()/"mcp.json"``) reads for its
  ``mcpServers`` (``:704``).

* ``skills/`` -> ``<data home>/skills/``. ``skills.py:1422`` ``skills_dir()`` is
  ``config_dir() / SKILLS_DIR_NAME`` and ``SKILLS_DIR_NAME == "skills"``
  (``skills.py:49``); ``config_dir()`` (``config/paths.py:265``) honours
  ``KIROCREW_HOME``, so in the container this is ``<data home>/skills``.

THE DIGEST is recomputed here byte-identically to the producer
(``share-my-crew/build/export/crew_export/bundle.py:78`` ``_bundle_digest``) and
its independent verifier (``build/export/tools/verify_bundle.py:50``
``digest_of``), which agree exactly. A different serialisation would fail every
valid bundle, so ``_content_digest`` is copied verbatim rather than reinvented.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import common
from ..common import Settings

log = logging.getLogger("container.supervisor")

#: The four entries PACKAGING-CONTRACT.md freezes, with the on-disk kind each
#: must be. ``skills`` is a directory (may be empty but MUST exist); the rest are
#: files (``mcp.json`` may be ``{}`` but MUST exist).
BUNDLE_ENTRIES: tuple[tuple[str, str], ...] = (
    ("manifest.json", "file"),
    ("agent.json", "file"),
    ("mcp.json", "file"),
    ("skills", "dir"),
)

#: Written at the data-home root on a successful install. For a human reading the
#: logs; T4's gate proves the crew from the image digest, not from this file.
INSTALLED_MARKER = ".smc-crew-installed.json"


def default_kiro_agents_dir() -> Path:
    """Where kiro-cli reads agent specs: ``<kiro home>/agents``.

    Mirrors ``kiro_crew.config.paths.kiro_home`` (``config/paths.py:510``):
    ``$KIRO_HOME`` if set, else ``~/.kiro``, then ``/agents``. Deliberately NOT
    under the data home -- see the module docstring. The one behaviour not
    mirrored is ``kiro_home``'s rejection of a system-directory ``$KIRO_HOME``;
    that guards a pathological override the container never sets, and copying it
    would only widen this module's surface.
    """
    override = os.environ.get("KIRO_HOME")
    home = Path(override).expanduser() if override else Path.home() / ".kiro"
    return home / "agents"


def _content_digest(root: Path) -> str:
    """Recompute the bundle content digest, byte-identical to the producer.

    Copied verbatim from ``crew_export/bundle.py:78`` and ``verify_bundle.py:50``
    (which agree): sha256 over sorted ``[rel_posix, sha256(bytes)]`` rows for
    every file except the top-level ``manifest.json`` (it carries the digest),
    then sha256 of the compact-JSON payload, prefixed ``"sha256:"``. The
    ``sorted(root.rglob("*"))`` over ``Path`` objects and the
    ``separators=(",", ":")`` compaction are both load-bearing: change either and
    the digest of a valid bundle stops matching.
    """
    rows: list[list[str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise common.ConfigError(
            f"bundle check failed [{label} is readable JSON]: {path} could not be "
            f"read as JSON ({exc})."
        ) from exc
    if not isinstance(data, dict):
        raise common.ConfigError(
            f"bundle check failed [{label} is a JSON object]: {path} parsed to a "
            f"{type(data).__name__}, not an object."
        )
    return data


def install_bundle(settings: Settings, *, agents_dir: Path | None = None) -> dict:
    """Verify the bundle, then lay it out where Kiro Crew reads it. Fail CLOSED.

    Called from ``run()`` before the backend starts, alongside ``verify_layout``
    / ``require_api_key`` / ``apply_sandbox_posture``. Every refusal names the
    check that failed and both values, because a container that boots with the
    wrong crew is the exact failure this change exists to prevent.

    ``agents_dir`` is injected only by tests, so the agent spec never lands in the
    real ``~/.kiro/agents`` during a test run; production resolves it via
    :func:`default_kiro_agents_dir`. Returns the marker payload on success.
    """
    bundle_dir = settings.bundle_dir

    # 1. The bundle dir and each of its four entries must exist, with the right
    #    kind. The crew rides in an image layer, which cannot be absent -- so a
    #    missing one means the image was built wrong, not a runtime blip.
    if not bundle_dir.is_dir():
        raise common.ConfigError(
            f"bundle check failed [bundle dir present]: SMC_BUNDLE_DIR="
            f"{bundle_dir} is not a directory (exists={bundle_dir.exists()}). The "
            f"crew rides in the image at this path; booting without it would "
            f"serve a default agent."
        )
    for entry, kind in BUNDLE_ENTRIES:
        p = bundle_dir / entry
        ok = p.is_dir() if kind == "dir" else p.is_file()
        if not ok:
            raise common.ConfigError(
                f"bundle check failed [entry present]: expected a {kind} at {p}, "
                f"but exists={p.exists()} is_file={p.is_file()} "
                f"is_dir={p.is_dir()}."
            )

    manifest = _read_json_object(bundle_dir / "manifest.json", "manifest.json")
    agent_spec = _read_json_object(bundle_dir / "agent.json", "agent.json")

    # 2. manifest crew_name must equal SMC_CREW_NAME. An empty SMC_CREW_NAME is
    #    refused too: "it started" must mean "the NAMED crew is installed", and
    #    an unnamed crew cannot satisfy that even if the manifest also omits it.
    manifest_crew = str(manifest.get("crew_name") or "")
    if not settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"SMC_CREW_NAME is empty (manifest crew_name={manifest_crew!r}). "
            "The deployment must name the crew it intends to serve."
        )
    if manifest_crew != settings.crew_name:
        raise common.ConfigError(
            "bundle check failed [manifest crew_name == SMC_CREW_NAME]: "
            f"manifest crew_name={manifest_crew!r} != SMC_CREW_NAME="
            f"{settings.crew_name!r}. The image does not carry the crew this "
            "task was configured to serve."
        )

    # 3. agent.json name must equal crew_name, or kiro-cli resolves a different
    #    agent (or none) -- surfacing later as a mode error, not the naming bug.
    agent_name = str(agent_spec.get("name") or "")
    if agent_name != manifest_crew:
        raise common.ConfigError(
            "bundle check failed [agent.json name == crew_name]: agent.json "
            f"name={agent_name!r} != manifest crew_name={manifest_crew!r}. The "
            f"spec is installed at <agents>/{manifest_crew}.json and read back by "
            "its own name, so a mismatch serves nothing."
        )

    # 4. The recomputed content digest must equal the manifest's. This is what
    #    proves the bytes in the image are the bytes that were reviewed.
    manifest_digest = str(manifest.get("digest") or "")
    recomputed = _content_digest(bundle_dir)
    if recomputed != manifest_digest:
        raise common.ConfigError(
            "bundle check failed [content digest == manifest digest]: recomputed="
            f"{recomputed!r} != manifest digest={manifest_digest!r}. The bundle "
            "content does not match what the manifest was signed over."
        )

    # All checks passed -- install into the read paths verified above.
    agents = agents_dir if agents_dir is not None else default_kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    agent_dst = agents / f"{manifest_crew}.json"
    # Copy the validated bytes rather than re-serialising, so what kiro-cli reads
    # is exactly what the digest covered.
    shutil.copyfile(bundle_dir / "agent.json", agent_dst)

    settings.data_home.mkdir(parents=True, exist_ok=True)
    mcp_dst = settings.data_home / "mcp.json"
    shutil.copyfile(bundle_dir / "mcp.json", mcp_dst)

    skills_dst = settings.data_home / "skills"
    shutil.copytree(bundle_dir / "skills", skills_dst, dirs_exist_ok=True)

    payload = {
        "crew_name": manifest_crew,
        "bundle_digest": manifest_digest,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    marker = settings.data_home / INSTALLED_MARKER
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log.info(
        "crew %r installed: agent=%s mcp=%s skills=%s digest=%s",
        manifest_crew,
        agent_dst,
        mcp_dst,
        skills_dst,
        manifest_digest,
    )
    return payload

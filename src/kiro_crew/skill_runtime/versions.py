"""The version history of a live auto-skill.

The ``version`` frontmatter field, the ``.versions/v<N>-SKILL.md`` snapshots an
update approval writes, their pruning, and the frontmatter rewrite that makes an
update candidate the new live body. The approval itself stays in the facade.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


def _auto_slug_from_name(name: str) -> str:
    """Return the bare slug for an auto-skill *name*, accepting either
    ``auto/<slug>`` or a bare ``<slug>``. Non-auto namespaces (any name with
    a slash after stripping the ``auto/`` prefix) fall through and are caught
    by the ``_is_pending_slug_safe`` guard at the call sites."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if name.startswith(f"{sk.AUTO_SKILL_NAMESPACE}/"):
        return name.split("/", 1)[1]
    return name


def get_auto_skill_version(loader: SkillsLoader, name: str) -> int:
    """Return the ``version`` frontmatter of a live auto-skill (default 1).

    Accepts ``auto/<slug>`` or a bare ``<slug>``. Returns 1 when the skill
    is missing, has no ``version`` line, or the value is unparseable — so a
    pre-versioning skill reads as version 1.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    slug = loader._auto_slug_from_name(name)
    if not loader._is_pending_slug_safe(slug):
        return 1
    skill_file = loader._dir / sk.AUTO_SKILL_NAMESPACE / slug / "SKILL.md"
    if not skill_file.exists():
        return 1
    raw = loader._cached_frontmatter(skill_file, within=None).get("version", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 1
    return v if v >= 1 else 1


def read_auto_skill_body(loader: SkillsLoader, name: str) -> str | None:
    """Return the full live ``SKILL.md`` text for an auto-skill, or ``None``.

    Accepts ``auto/<slug>`` or a bare ``<slug>``; refuses any non-auto
    namespace (a multi-segment name). Returns ``None`` when the skill is
    missing or unreadable. Used by the API to render an old-vs-new diff for
    update candidates.

    Refuses to follow a symlink anywhere on the path. This body is fed to the
    update-merge turn UNREDACTED (redaction runs on the merge OUTPUT), so a
    swapped ``SKILL.md`` symlink pointing at credential storage would put
    those bytes into an LLM prompt. Resolve, then verify the real path is
    still inside the skills tree and is not a sensitive location.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    slug = loader._auto_slug_from_name(name)
    if not loader._is_pending_slug_safe(slug):
        return None
    base = loader._dir / sk.AUTO_SKILL_NAMESPACE / slug
    skill_file = base / "SKILL.md"
    if not skill_file.exists():
        return None
    # No symlink on the skill dir or the file itself.
    if os.path.islink(str(base)) or os.path.islink(str(skill_file)):
        logger.warning("Refusing to read %s: symlink on the live skill path", name)
        return None
    real = os.path.realpath(str(skill_file))
    # The resolved path must still live under the skills root, and must never
    # be a credential/sensitive location.
    try:
        Path(real).relative_to(os.path.realpath(str(loader._dir)))
    except ValueError:
        logger.warning("Refusing to read %s: resolves outside the skills tree", name)
        return None
    if sk.is_sensitive_path(real):
        logger.warning("Refusing to read %s: resolves to a sensitive path", name)
        return None
    try:
        # Read the RESOLVED path through the hardened primitive, not the
        # original one: the checks above vet ``real``, so reading
        # ``skill_file`` again would validate one path and read another.
        # safe_read_file re-checks is_sensitive_path and opens with
        # O_NOFOLLOW, closing a swap of the final component after our check.
        return sk.safe_read_file(real)
    except (OSError, PermissionError):
        return None


def _rewrite_update_frontmatter(
    candidate_content: str,
    *,
    target_name: str,
    created_at: str,
    version: int,
    pinned: bool = False,
    pointer_only: bool = False,
) -> str:
    """Rebuild an update candidate's body as the new live SKILL.md.

    Keeps the candidate's description/triggers/source/body (the merged new
    content) but forces ``name`` to the live target, preserves the live
    ``created_at``, and stamps ``version``. Any ``name`` / ``created_at`` /
    ``version`` / ``pinned`` / ``inject_on_trigger`` lines from the candidate
    are dropped and re-emitted so the live skill's identity + history are
    authoritative, not the candidate's. ``pinned`` is carried from the LIVE
    skill: a candidate never sets it, and losing it would drop the target's
    lifecycle exemption and expose a user-pinned skill to archival.
    ``pointer_only`` is carried the same way and for the same reason: a
    candidate never sets ``inject_on_trigger``, so dropping it would silently
    re-enable full-body injection on a skill the user had opted out — a
    setting reverting itself behind an unrelated approval.
    """
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", candidate_content, re.DOTALL)
    if m:
        fm_lines = m.group(1).split("\n")
        body = m.group(2)
    else:
        fm_lines = []
        body = candidate_content
    kept: list[str] = []
    for ln in fm_lines:
        if not ln.strip():
            continue
        key = ln.split(":", 1)[0].strip() if ":" in ln else ""
        if key in ("name", "created_at", "version", "pinned", "inject_on_trigger"):
            continue
        kept.append(ln)
    new_fm = [f"name: {target_name}"]
    new_fm.extend(kept)
    if created_at:
        new_fm.append(f"created_at: {created_at}")
    new_fm.append(f"version: {version}")
    if pinned:
        new_fm.append("pinned: true")
    if pointer_only:
        new_fm.append("inject_on_trigger: false")
    return "---\n" + "\n".join(new_fm) + "\n---\n\n" + body.strip() + "\n"


def _versions_root(loader: SkillsLoader, target_slug: str) -> Path:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    return loader._dir / sk.AUTO_SKILL_NAMESPACE / target_slug / sk.VERSIONS_DIRNAME


def _prune_versions(loader: SkillsLoader, versions_dir: Path) -> None:
    """Keep only the newest ``MAX_SKILL_VERSIONS`` ``v<N>-SKILL.md``
    snapshots in *versions_dir*, deleting the lowest-numbered excess."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not versions_dir.is_dir():
        return
    snaps: list[tuple[int, Path]] = []
    for p in versions_dir.iterdir():
        mm = re.match(r"^v(\d+)-SKILL\.md$", p.name)
        if p.is_file() and mm:
            snaps.append((int(mm.group(1)), p))
    snaps.sort(key=lambda t: t[0])
    excess = len(snaps) - sk.MAX_SKILL_VERSIONS
    for _n, p in snaps[:excess] if excess > 0 else []:
        try:
            p.unlink()
        except OSError:
            pass


def _resolve_snapshot_version(loader: SkillsLoader, versions_dir: Path, fm_version: int) -> int:
    """Return the version number to snapshot the CURRENT live body under.

    Normally the live frontmatter's ``version`` is authoritative. But if a
    snapshot already exists at that number the numbering has drifted (e.g. an
    older refine stripped the ``version`` line, so the live skill reads as v1
    again) — writing there would DESTROY the earlier snapshot. In that case
    continue above the highest snapshot on disk instead, so history is only
    ever appended to.
    """
    if not (versions_dir / f"v{fm_version}-SKILL.md").exists():
        return fm_version
    highest = fm_version
    for p in versions_dir.iterdir():
        mm = re.match(r"^v(\d+)-SKILL\.md$", p.name)
        if p.is_file() and mm:
            highest = max(highest, int(mm.group(1)))
    logger.warning(
        "Version numbering drifted for %s: snapshot v%d exists; continuing at v%d",
        versions_dir.parent.name,
        fm_version,
        highest + 1,
    )
    return highest + 1

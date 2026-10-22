"""Bringing the specs already on disk up to date.

The one-time migrations of bookkeeping keys and relocated skill URIs, the model pin a
rebuild propagates and the commands that clear or reset one, and the startup repair of
the hooks in the specs Kiro Crew owns. New work on upgrading an existing spec goes here.

Composed onto ``kiro_crew.agent``, the import path and patch target of every name
here; see :mod:`kiro_crew.agent_parts`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, MutableMapping

if TYPE_CHECKING:
    from kiro_crew.agent import (
        OWNED_KIRO_AGENT_FILES,
        SKILL_URI_PREFIX,
        _atomic_json_write,
        _conflicting_spec_for,
        _load_json,
        _mc_config_path,
        _read_agent_spec,
        _read_spec_capped,
        _refresh_dynamic_fields,
        _spec_path_is_safe,
        agent_spec_path,
        agent_state,
        agents_spec_lock,
        build_agent_config,
        is_markdown_spec,
        kiro_agents_dir_path,
        kiro_hooks,
        loads_user_json,
        logger,
        sel,
        worker_agent,
    )


def _claim_propagated_model(config: dict, name: str) -> None:
    """Record *name*'s model as managed once a published spec carries the global.

    Runs only after the spec write returned, so a write that fails leaves the
    sidecar as it was. When the spec on disk holds the concrete global model,
    that value is the propagation's, not a legacy pin: recording it managed is
    what lets returning the global to "auto" clear it. An explicit pick
    (``False``) is never claimed, and an unreadable sidecar claims nothing.
    """
    from kiro_crew.config.loader import coerce_config_field, normalize_agent_model

    mc_cfg = _load_json(_mc_config_path()) or {}
    global_model = normalize_agent_model(
        coerce_config_field(mc_cfg, "agent", dict, {}).get("model")
    )
    if not global_model or config.get("model") != global_model:
        return
    try:
        agent_state.claim_model_managed_if_unset(name)
    except (OSError, ValueError):
        logger.warning(
            "Agent state sidecar unreadable; not recording model ownership for %s",
            name,
            exc_info=True,
        )


def _load_existing_config(
    path: Path, *, gated_off: "frozenset[str] | None" = None
) -> tuple[dict, bool]:
    """Load and refresh an existing kirocrew.json.

    Returns (config, fresh_install).  Falls back to build_agent_config()
    when the file is corrupt or refresh fails.

    *gated_off* is the caller's spec-gate snapshot, forwarded so whichever branch
    runs reads the same decision the caller's audit will report.
    """
    try:
        config = loads_user_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = None
    if not isinstance(config, dict):
        return build_agent_config(gated_off=gated_off), True
    try:
        _refresh_dynamic_fields(config, gated_off=gated_off)
    except (AttributeError, TypeError, RuntimeError) as exc:
        logger.error("Refresh failed, rebuilding from defaults: %s", exc)
        return build_agent_config(gated_off=gated_off), True
    return config, False


def migrate_agent_specs() -> int:
    """Strip Kiro Crew bookkeeping keys from kiro agent specs into the sidecar.

    kiro-cli validates ``~/.kiro/agents/*.json`` with ``deny_unknown_fields``
    and rejects the entire spec on any unknown field (``model_managed`` /
    ``cc_model``), then silently falls back to the default agent. This lifts
    those values into ``agent_state`` and removes them from each spec so every
    agent loads. Idempotent and cheap (a handful of small JSON files); safe to
    run on every gateway start. Returns the number of spec files cleaned.
    """
    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return 0
    cleaned = 0
    # JSON only, deliberately: this is a rewrite pass, and a markdown spec is
    # never rewritten by Kiro Crew. A bookkeeping key in a markdown
    # frontmatter is the author's to remove.
    for spec_path in sorted(agents_dir.glob("*.json")):
        # This read is followed by a rewrite, so the hardened reader's
        # sensitive-target refusal is not sufficient on its own: refuse every
        # symlink, escape and sensitive path before reading to prevent copy-out.
        if not _spec_path_is_safe(spec_path, agents_dir):
            continue
        # The hardened reader (size cap, AppleDouble/sensitive-symlink and
        # non-object refusal). This site also WRITES below: a spec the reader
        # refuses is now never rewritten at all, whereas the old read_text
        # path read -- and then rewrote -- whatever the file or link named.
        data = _read_agent_spec(
            spec_path,
            operation="migrate_agent_specs",
            source="unknown",
        )
        if data is None:
            continue
        if "model_managed" not in data and "cc_model" not in data:
            continue
        name = data.get("name") or spec_path.stem
        agent_state.lift_and_strip_bookkeeping(data, name)
        try:
            _atomic_json_write(spec_path, data)
            cleaned += 1
        except OSError as exc:
            logger.warning("Could not rewrite cleaned agent spec %s: %s", spec_path, exc)
    if cleaned:
        logger.info(
            "Cleaned %d kiro agent spec(s) of KiroCrew bookkeeping keys", cleaned  # brand-ok
        )
    return cleaned


def _relocated_skill_uri(uri: str, moves: dict[Path, Path]) -> str | None:
    """The new ``skill://`` URI for *uri* when it names a relocated skill, else ``None``.

    Only a ``~/`` or absolute URI is matched: a workspace-relative one points into
    a project tree, never at the builtin skills home. The rewrite keeps the URI's
    form, so a ``~/`` mapping stays portable across machines.
    """
    if not uri.startswith(SKILL_URI_PREFIX):
        return None
    raw = uri[len(SKILL_URI_PREFIX) :]
    home_form = raw.startswith("~/")
    if home_form:
        path = Path.home() / raw[2:]
    elif Path(raw).is_absolute():
        path = Path(raw)
    else:
        return None
    new = moves.get(Path(os.path.normpath(path)))
    if new is None:
        return None
    if home_form:
        try:
            return f"{SKILL_URI_PREFIX}~/{new.relative_to(Path.home()).as_posix()}"
        except ValueError:
            pass
    return f"{SKILL_URI_PREFIX}{new.as_posix()}"


def migrate_relocated_skill_uris() -> int:
    """Point agent specs that map a relocated builtin skill at its new path.

    A builtin skill that moves (``skills._RELOCATED_SKILLS``) leaves its old
    ``SKILL.md`` quarantined, so an agent spec that maps the old path by
    ``skill://`` would silently load nothing. This rewrites each such resource to
    the new path, in place and in order, and drops it instead when the spec
    already maps the new path. A move counts only once the new ``SKILL.md`` is
    installed and the old one is gone, so a mapping is never pointed at a file
    that does not exist, and a skill still loadable at its old path keeps its
    mapping. Idempotent and safe to run on every rebuild. Returns the number of
    spec files rewritten.
    """
    from kiro_crew.skills import _RELOCATED_SKILLS, skills_dir  # noqa: PLC0415

    agents_dir = kiro_agents_dir_path()
    if not agents_dir.is_dir():
        return 0
    base = skills_dir()
    moves: dict[Path, Path] = {}
    for old_name, new_name in _RELOCATED_SKILLS.items():
        old_md = base / old_name / "SKILL.md"
        new_md = base / new_name / "SKILL.md"
        if new_md.is_file() and not old_md.exists():
            moves[Path(os.path.normpath(old_md))] = new_md
    if not moves:
        return 0
    rewritten = 0
    # Every template-spec writer holds this lock, and the read sits INSIDE it:
    # a snapshot taken before a concurrent PATCH saved would otherwise be
    # written back over that edit.
    try:
        with agents_spec_lock(agents_dir):
            # JSON only, for the same reason as migrate_agent_specs: a markdown spec is
            # never rewritten by Kiro Crew.
            for spec_path in sorted(agents_dir.glob("*.json")):
                if not _spec_path_is_safe(spec_path, agents_dir):
                    continue
                data = _read_agent_spec(
                    spec_path,
                    operation="migrate_relocated_skill_uris",
                    source="unknown",
                )
                if data is None:
                    continue
                name = data.get("name") or spec_path.stem
                try:
                    enrolled = agent_state.get_capabilities(str(name)) is not None
                except (ValueError, OSError):
                    enrolled = True
                if enrolled:
                    # An enrolled member's spec is a saved generation whose
                    # digest its capability intent records; rewriting it in
                    # place would make reconcile refuse new sessions. It is
                    # left for a re-save in Capabilities.
                    continue
                resources = data.get("resources")
                if not isinstance(resources, list):
                    continue
                present = {r for r in resources if isinstance(r, str)}
                updated: list[object] = []
                changed = False
                for resource in resources:
                    new_uri = (
                        _relocated_skill_uri(resource, moves) if isinstance(resource, str) else None
                    )
                    if new_uri is None:
                        updated.append(resource)
                        continue
                    changed = True
                    if new_uri not in present:
                        updated.append(new_uri)
                        present.add(new_uri)
                if not changed:
                    continue
                # The skill-URI migration is a managed WRITER of this spec. For the
                # dashboard-author stem, decide ownership from the PRE-rewrite bytes and renew
                # the digest after the write only when they were our confirmed managed write
                # -- otherwise the installer would read its own migrated spec as foreign and
                # stop re-filtering its grants against a tightened ceiling. A user file (no
                # recorded digest) is never stamped. ``worker_agent`` is the module global
                # bound by the core's tail import, resolved at call time.
                pre_migration_confirmed = (
                    str(name) == worker_agent._MANAGED_OWNED_NAME
                    and spec_path.stem == (worker_agent._MANAGED_OWNED_NAME)
                    and worker_agent._is_confirmed_managed_dashboard_author(data)
                )
                pre_migration_digest = (
                    agent_state.spec_digest(data) if pre_migration_confirmed else None
                )
                data["resources"] = updated
                try:
                    if pre_migration_confirmed:
                        # Two-phase: keep the current on-disk digest in the finalized slot,
                        # record the migrated bytes' digest as pending, write, finalize -- so
                        # an interruption leaves the file matching a recorded slot.
                        agent_state.begin_managed_write(
                            spec_path.stem,
                            agent_state.spec_digest(data),
                            current=pre_migration_digest,
                        )
                        _atomic_json_write(spec_path, data)
                        agent_state.finalize_managed_write(spec_path.stem)
                    else:
                        _atomic_json_write(spec_path, data)
                    rewritten += 1
                except OSError as exc:
                    logger.warning(
                        "Could not rewrite relocated skill mapping in %s: %s", spec_path, exc
                    )
    except OSError:
        # The lock already logged why it could not be taken; try again on
        # the next rebuild rather than write unserialized.
        return rewritten
    if rewritten:
        logger.info("Pointed %d agent spec(s) at relocated builtin skills", rewritten)
    return rewritten


def clear_model_pin(config: MutableMapping[str, object], name: str) -> None:
    """Drop *config*'s ``model`` pin and resume tracking the shipped default.

    The in-place half of "return this agent to the default model", shared by
    every caller that offers it, so the dashboard's Agent Templates editor and
    the CLI cannot drift on what clearing a model means (the same reason
    :func:`agent_state.lift_and_strip_bookkeeping` is shared by four writers).
    The caller persists *config* itself.

    Deliberately the ONLY way a spec's ``model`` becomes managed after install:
    ownership cannot be inferred from a spec's value, because a model an older
    build's propagation wrote and one the user typed in by hand are identical on
    disk. So this is driven by an explicit user action -- clearing the model in
    the editor, or ``kirocrew agent reset-model`` -- and never by a heuristic
    running behind the user's back on refresh.

    Ordering is benign in both directions: if the sidecar write lands and the
    caller's spec write does not, the next refresh resolves the still-pinned
    spec to the shipped default, which is what the user asked for; if the spec
    write lands and the sidecar write does not, the pin is gone and the resolver
    falls through to the global.
    """
    config.pop("model", None)
    agent_state.set_model_managed(name, True)


def reset_agent_model(name: str) -> tuple[Path, str]:
    """Clear *name*'s spec model pin on disk; return (spec path, previous model).

    The explicit, narrow counterpart to ``kirocrew setup --clean``, which also
    resumes default-model tracking but regenerates the whole spec and discards
    every user customization with it. Raises ``FileNotFoundError`` when the
    agent has no user-level spec.
    """
    spec_path = agent_spec_path(name)
    if spec_path is None:
        raise FileNotFoundError(f"no kiro agent spec for {name!r} in {kiro_agents_dir_path()}")
    if is_markdown_spec(spec_path):
        # A markdown spec is one hand-authored document; serializing a JSON
        # object over it would destroy the prompt and every field this
        # writer does not model. The pin is edited in the frontmatter.
        raise ValueError(
            f"agent {name!r} is defined in markdown ({spec_path}); edit its frontmatter "
            f"'model' field directly -- Kiro Crew does not rewrite markdown agent specs"
        )
    conflict = _conflicting_spec_for(name, spec_path, kiro_agents_dir_path())
    if conflict is not None:
        raise ValueError(
            f"two specs claim {name!r}: {str(spec_path)!r} declares it, and {str(conflict)!r} "
            f"carries the filename. The runtime accepts either, in unordered directory order, "
            f"so which one is live is undefined -- rename or remove one before resetting."
        )
    # The COMPLETE read-modify-write sits under the shared spec lock, and the
    # spec is read INSIDE it: a pre-lock snapshot can go stale against a
    # concurrent fork refresh, and writing it back would re-persist the very
    # allowedTools/autoApprove grants the refresh's governance pass just
    # stripped — while the refresh reports success.
    with agents_spec_lock(kiro_agents_dir_path()):
        from kiro_crew.agent_capabilities import require_unmanaged_template

        require_unmanaged_template(name)
        try:
            data = _read_spec_capped(spec_path)
        except (OSError, ValueError) as exc:
            raise FileNotFoundError(f"could not read agent spec {spec_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise FileNotFoundError(f"agent spec {spec_path} is not readable as a JSON object")
        # Snapshot the PRE-edit bytes to decide ownership BEFORE this writer mutates them:
        # the digest is renewed below only when the file WAS already our confirmed managed
        # write, never for a user file at the once-user-creatable stem.
        pre_write_confirmed = name == worker_agent._MANAGED_OWNED_NAME and (
            worker_agent._is_confirmed_managed_dashboard_author(data)
        )
        # Digest of the CURRENT on-disk bytes (pre-edit), captured before mutation so
        # ``begin_managed_write`` can keep the file confirmable if this write is interrupted.
        pre_write_digest = agent_state.spec_digest(data) if pre_write_confirmed else None
        previous = data.get("model") or ""
        clear_model_pin(data, name)
        # Same strip every spec writer runs: kiro-cli validates with
        # deny_unknown_fields and drops the whole agent on an unknown key.
        agent_state.lift_and_strip_bookkeeping(data, name)
        # An authorized reset of the managed dashboard-author spec legitimately rewrites its
        # bytes. Ownership is the installer-recorded DIGEST, renewed ONLY when the PRE-edit
        # content was already our confirmed managed write (``pre_write_confirmed``) -- a user
        # file at the stem is never stamped. Two-phase so a crash cannot leave bytes and
        # record out of step: record the new bytes' digest as pending, write, finalize.
        if pre_write_confirmed:
            agent_state.begin_managed_write(
                name, agent_state.spec_digest(data), current=pre_write_digest
            )
            _atomic_json_write(spec_path, data)
            agent_state.finalize_managed_write(name)
        else:
            _atomic_json_write(spec_path, data)
    return spec_path, str(previous)


def repair_agent_configs() -> None:
    """Remove legacy Kiro Crew hook keys from agent configs owned by Kiro Crew."""
    _sanitize_agent_hooks()


_hooks_sanitized_mtimes: dict[str, float] = {}


def _sanitize_agent_hooks() -> None:
    """Remove legacy Kiro Crew hook keys from agent configs owned by Kiro Crew.

    Kiro-cli rejects unknown variants in the ``hooks`` field (e.g.
    ``auto_approve_tools``), causing it to silently fall back to the
    default agent — losing kirocrew-core, kirocrew-cron.

    Auto-repairs configs carrying keys Kiro Crew wrote in prior versions. Files
    outside :data:`OWNED_KIRO_AGENT_FILES` and unrecognized hook keys are left
    untouched because Kiro Crew does not own their schema or contents.
    """
    agents_dir = kiro_agents_dir_path()
    for filename in OWNED_KIRO_AGENT_FILES:
        f = agents_dir / filename
        # Refuse a symlink or out-of-dir resolution BEFORE touching the file: the sweep
        # reads every owned name, and following a link at an owned stem (e.g. one planted at
        # kirocrew-dashboard-author.json pointing at ~/.docker/config.json) would read a
        # file outside the agents dir. ``_spec_path_is_safe`` is the same no-follow fence the
        # install and reset paths use; an absent file simply is not safe-and-present, so it
        # is skipped here as before.
        if not os.path.lexists(f) or not _spec_path_is_safe(f, agents_dir):
            continue
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if _hooks_sanitized_mtimes.get(str(f)) == mtime:
            continue
        # Capped, no-follow read (the hardened spec reader) rather than ``_load_json``,
        # which follows symlinks and is uncapped: a sweep over owned names must never slurp
        # or launder a file the reader may not open.
        data = _read_spec_capped(f)
        if not data:
            continue
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        removed_keys = [key for key in hooks if key in kiro_hooks._LEGACY_KIROCREW_HOOK_KEYS]
        if not removed_keys:
            _hooks_sanitized_mtimes[str(f)] = mtime
            continue
        # Decide ownership from the PRE-sweep bytes: the digest is renewed below only when
        # the file as READ already reproduced the recorded digest, i.e. it was our confirmed
        # managed write. Keying on "a digest exists" (a stale record from a managed install
        # the user later replaced at the stem) would stamp the user's swept bytes as managed
        # and let the next rebuild overwrite them. ``worker_agent`` is a module global bound
        # by ``kiro_crew.agent``'s tail import (like ``kiro_hooks`` above), resolved at
        # call time.
        stem = filename.removesuffix(".json")
        pre_sweep_confirmed = stem == worker_agent._MANAGED_OWNED_NAME and (
            worker_agent._is_confirmed_managed_dashboard_author(data)
        )
        pre_sweep_digest = agent_state.spec_digest(data) if pre_sweep_confirmed else None
        data["hooks"] = {
            key: value
            for key, value in hooks.items()
            if key not in kiro_hooks._LEGACY_KIROCREW_HOOK_KEYS
        }
        # The sweep changes the bytes the ownership digest was recorded against. Renew it to
        # match -- but ONLY when the pre-sweep file was already our confirmed managed write
        # (``pre_sweep_confirmed``); a user file is never stamped. Two-phase so a crash leaves
        # bytes and record in step: record the swept bytes' digest as pending, write, finalize.
        if pre_sweep_confirmed:
            agent_state.begin_managed_write(
                stem, agent_state.spec_digest(data), current=pre_sweep_digest
            )
            _atomic_json_write(f, data)
            agent_state.finalize_managed_write(stem)
        else:
            _atomic_json_write(f, data)
        _hooks_sanitized_mtimes[str(f)] = f.stat().st_mtime
        logger.info("Removed legacy Kiro Crew hook keys %s from %s", removed_keys, f.name)
        sel().log_api_access(
            caller="system",
            operation="sanitize_agent_hooks",
            outcome="ok",
            source="agent",
            resources=f"{f.name}: removed {removed_keys}",
        )

"""The first crewmate every install creates once.

The first crewmate (config key ``mate``, shown as Mate) is an
ordinary crewmate: it runs the same template a crewmate the user creates gets,
carries no tool, memory or prompt of its own, and can be renamed and deleted
like any other. :func:`create_first_crewmate_once` writes its row once, behind a
one-time marker, so a deleted first crewmate is never re-created. A fresh
install and an upgrade both get it, once. The only thing that sets it apart is
its longer first welcome, which the dashboard's greeting turn carries in its
kickoff (:mod:`kiro_crew.dashboard.mate_welcome`).
"""

from __future__ import annotations

from kiro_crew import agent as agent_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME

#: One-time marker for :func:`create_first_crewmate_once`, under the data home.
FIRST_CREWMATE_MARKER = "mate_member_created.json"

#: The template a crewmate gets when the config names no default one.
_ORDINARY_TEMPLATE = "kirocrew"


def _default_template(*sections: object) -> str:
    """The configured ``agent.default_agent`` among *sections*, else ``kirocrew``.

    The last section that names one wins, so the ``config.local.json`` overlay,
    passed last, outranks ``config.json`` the way the loader merges them.
    """
    template = ""
    for section in sections:
        if isinstance(section, dict) and isinstance(section.get("default_agent"), str):
            template = section["default_agent"].strip()
    return template or _ORDINARY_TEMPLATE


def _marker(name: str):
    return agent_mod.config_dir() / name


def _mark(name: str, *, created: bool = False) -> None:
    import json

    from kiro_crew.atomic_write import atomic_write

    atomic_write(_marker(name), json.dumps({"version": 1, "created": created}) + "\n")


def first_crewmate_was_created() -> bool:
    """Whether the ``mate`` row is the one :func:`create_first_crewmate_once` wrote.

    False when the step skipped because a row under that key already existed
    (the user's own crewmate), and before the step has run. Blocking IO.
    """
    import json

    try:
        record = json.loads(_marker(FIRST_CREWMATE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("created") is True


def create_first_crewmate_once() -> None:
    """Create the first crewmate once, bound to the ordinary crewmate template.

    It is created the way every crewmate is (the CLI's ``agent create``, an
    app's ``ensure_team``): the row is built in memory, given its own private V2
    store with an ordinary, collision-reserving identity, and published with
    ``persist_member_config(create=True)``. Nothing is ever written on Global
    memory, so there is nothing to move later. The reserved ``default`` member is
    never changed. The marker makes creation one-time: a first crewmate the user
    deleted is not created again. A creation that fails leaves no row and no
    marker, so the next start tries again.

    Skipped, and marked done, when the key already exists in ``config.json`` or
    in the ``config.local.json`` overlay, or when the overlay supplies the
    roster while the base has none (adding a base row there could change which
    member the loader resolves as the default). When the base roster is empty it
    is first seeded with exactly the ``default`` row the loader's one-time
    migration would write, so adding the first crewmate never suppresses the
    implicit default member.
    """
    from kiro_crew.config.loader import config_local_path, update_config_locked

    if _marker(FIRST_CREWMATE_MARKER).exists():
        return

    overlay: dict = {}
    template: list[str] = []

    def read_overlay(doc: dict) -> None:
        overlay.update(doc)

    def prepare(data: dict) -> dict | None:
        if _marker(FIRST_CREWMATE_MARKER).exists():
            return None
        overlay.clear()
        update_config_locked(config_local_path(), mutate=read_overlay)
        overlay_rows = overlay.get("agents")
        rows = data.get("agents")
        if (
            (overlay_rows is not None and not isinstance(overlay_rows, dict))
            or (isinstance(overlay_rows, dict) and ASSISTANT_MEMBER_NAME in overlay_rows)
            or (rows is not None and not isinstance(rows, dict))
            or (isinstance(rows, dict) and ASSISTANT_MEMBER_NAME in rows)
            or (not rows and overlay_rows)
        ):
            _mark(FIRST_CREWMATE_MARKER)
            return None
        template.append(_default_template(data.get("agent"), overlay.get("agent")))
        if rows:
            return None
        data["agents"] = {
            "default": {
                "kiro_agent": template[0],
                "workspace": "default",
                "memory_store": "default",
            }
        }
        return data

    update_config_locked(mutate=prepare)
    if template and _create_private(template[0]):
        _mark(FIRST_CREWMATE_MARKER, created=True)
        _owe_first_welcome()


def _create_private(template: str) -> bool:
    """Publish the first crewmate with its own private store; False on failure.

    A failure is logged and leaves nothing behind: an allocated store that was
    never published is retired, exactly as on every other create path.
    """
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.config.sections import KiroCrewAgentConfig
    from kiro_crew.member_memory_auth import require_member_memory_creation
    from kiro_crew.memory_stores import (
        persist_member_config,
        provision_member_memory,
        retire_unpublished_allocation,
    )

    try:
        _invalidate_config_cache()
        config = KiroCrewConfig.load()
    except Exception:
        agent_mod.logger.warning("The first crewmate was not created", exc_info=True)
        return False
    if ASSISTANT_MEMBER_NAME in config.agents:
        _mark(FIRST_CREWMATE_MARKER)
        return False
    member = KiroCrewAgentConfig(kiro_agent=template, workspace="default", source="builtin")
    config.agents[ASSISTANT_MEMBER_NAME] = member
    previous_store, previous_id = member.memory_store, member.member_id
    try:
        require_member_memory_creation(ASSISTANT_MEMBER_NAME)
        provision_member_memory(config, ASSISTANT_MEMBER_NAME)
        persist_member_config(config, ASSISTANT_MEMBER_NAME, create=True)
    except Exception:
        if member.memory_store != previous_store:
            try:
                retire_unpublished_allocation(
                    config,
                    ASSISTANT_MEMBER_NAME,
                    member.memory_store,
                    previous_store=previous_store,
                    previous_member_id=previous_id,
                )
            except Exception:
                agent_mod.logger.warning("An unpublished store was not retired", exc_info=True)
        agent_mod.logger.warning(
            "The first crewmate was not created; it is tried again at next start", exc_info=True
        )
        return False
    return True


def _owe_first_welcome() -> None:
    """Record the first welcome the row just created owes its user.

    A failure there is logged by :func:`kiro_crew.members.mark_welcome_owed` and
    only costs the welcome.
    """
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.members import mark_welcome_owed

    try:
        _invalidate_config_cache()
        config = KiroCrewConfig.load()
    except Exception:
        agent_mod.logger.warning("The first crewmate's welcome was not recorded", exc_info=True)
        return
    mark_welcome_owed(ASSISTANT_MEMBER_NAME, config=config)

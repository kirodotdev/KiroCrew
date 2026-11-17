"""Whether Task Runner may start a new run: the ``taskrunner.enabled`` switch.

An install that still holds a ``projects`` app record (the built-in app Task Runner
was) has that record's off state carried into the setting once by the dashboard's
boot cleanup (:mod:`kiro_crew.apps.builtin_fold`) before the record is retired. Until
then the record itself is read too, while no config file holds the setting: a
gateway started without the dashboard never runs that cleanup, and a cleanup whose
write failed keeps the record, so an off record still refuses the start until the
setting is written.

The operator's ``apps`` governance allowlist decides it under the name ``projects``,
so a policy that does not list ``projects`` keeps Task Runner off whatever the local
setting says.

Only new starts read it: a run already going keeps going, and its page stays open.
"""

from __future__ import annotations

import json

#: The built-in app Task Runner was, and the name the ``apps`` allowlist knows it by.
TASK_RUNNER_FORMER_APP = "projects"

#: The answer every start path gives while the setting is off.
TASK_RUNNER_OFF_MESSAGE = (
    "Task Runner is turned off. Turn it on in Settings > Display > View to start tasks."
)


def task_runner_off() -> str | None:
    """Why a new Task Runner start is refused, or ``None`` when it may go ahead.

    Blocking (it reads the config files and the governance policy), so an async
    caller runs it in a worker thread.
    """
    # Call-time imports: the config loader and the activation gate are heavy, and
    # every caller already runs this off the event loop.
    from kiro_crew.apps.manager import _app_activation_denied, _read_installed
    from kiro_crew.config.loader import KiroCrewConfig

    if not KiroCrewConfig.load().taskrunner.enabled:
        return TASK_RUNNER_OFF_MESSAGE
    # The old record speaks only while no config file holds the setting itself, the
    # rule the carry follows too: a written key is a choice and wins. And only the
    # built-in's own record: an app a user installed under the same name is not it.
    if not _switch_written():
        record = _read_installed(TASK_RUNNER_FORMER_APP)
        if record is not None and record.origin == "builtin" and not record.enabled:
            return TASK_RUNNER_OFF_MESSAGE
    denied = _app_activation_denied(TASK_RUNNER_FORMER_APP)
    return f"Task Runner is turned off by policy: {denied}" if denied else None


def _switch_written() -> bool:
    """Whether ``config.json`` or ``config.local.json`` holds ``taskrunner.enabled`` itself.

    Read as the load reads them (:func:`read_endorsed_config_text`), so a key the
    gateway has not endorsed yet counts no more here than in the loaded setting.
    """
    from kiro_crew.config.loader import (
        config_local_path,
        config_path,
        read_endorsed_config_text,
    )

    for path in (config_path(), config_local_path()):
        try:
            data = json.loads(read_endorsed_config_text(path))
        except (OSError, ValueError):
            continue
        section = data.get("taskrunner") if isinstance(data, dict) else None
        if isinstance(section, dict) and "enabled" in section:
            return True
    return False

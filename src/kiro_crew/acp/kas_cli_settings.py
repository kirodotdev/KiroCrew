"""kiro-cli's telemetry opt-out and ``chat.defaultModel``, read for a KAS session.

kiro-cli honours its own ``cli.json`` ``telemetry.enabled`` opt-out and the
``KIRO_DISABLE_TELEMETRY`` / ``Q_DISABLE_TELEMETRY`` environment switches. KAS reads no
``cli.json``: it takes the opt-out only as ``_meta.kiro.telemetryEnabled`` in
``initialize`` (kiro-cli's own relay leaves that signal to the client it relays). So
Crew reads the setting here and hands it to KAS itself.

The files are the ones kiro-cli reads: ``<kiro home>/settings/cli.json`` and the
workspace's ``.kiro/settings/cli.json``. kiro-cli lets the workspace file override the
global one; for the opt-out this module takes the stricter answer instead (see
:func:`kiro_cli_telemetry_enabled`).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew.acp.skill_projection import (
    _UNREADABLE_SETTINGS_ERRORS,
    _settings,
)
from kiro_crew.config.paths import kiro_home

logger = logging.getLogger(__name__)

#: The model ids ``chat.defaultModel`` may carry; anything else is skipped.
_MODEL_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/\[\]@-]{0,127}\Z")

#: kiro-cli's environment switches that turn its telemetry off when set to any value.
TELEMETRY_OFF_ENV = ("KIRO_DISABLE_TELEMETRY", "Q_DISABLE_TELEMETRY")

#: Set once the "KAS may not honour the opt-out" warning has been logged.
_OPT_OUT_WARNED = False

#: The session-start notice when the opt-out was sent: Crew cannot confirm KAS applied it.
TELEMETRY_UNCONFIRMED_NOTICE = (
    "ℹ Your kiro-cli telemetry is off, so Kiro Crew asked the KAS engine to turn its "
    "telemetry off too. KAS does not confirm this, so Kiro Crew cannot check that it "
    "took effect."
)

#: The session-start notice when this folder's kiro-cli telemetry is off but the KAS
#: process the session runs on was started from another folder with it on.
TELEMETRY_NOT_APPLIED_NOTICE = (
    "ℹ This folder's kiro-cli settings turn telemetry off, but this chat runs on a KAS "
    "engine that was started with telemetry on, so the opt-out was not applied to "
    "this chat."
)


def session_telemetry_notice(sent_enabled: bool | None, session_dir: Path | str | None) -> str:
    """The notice a new session on a KAS process gets about telemetry, else ``""``.

    *sent_enabled* is what the process's ``initialize`` carried (``None``: not KAS).
    KAS takes the setting once per process, so a session in a folder whose own
    ``cli.json`` opts out, on a process started with telemetry on, is told so.
    """
    if sent_enabled is None:
        return ""
    if not sent_enabled:
        return TELEMETRY_UNCONFIRMED_NOTICE
    return "" if kiro_cli_telemetry_enabled(session_dir) else TELEMETRY_NOT_APPLIED_NOTICE


def _settings_files(work_dir: Path | str | None) -> list[Path]:
    """Global first, then the workspace file, which kiro-cli lets override it."""
    files = [kiro_home() / "settings" / "cli.json"]
    if work_dir:
        files.append(Path(work_dir) / ".kiro" / "settings" / "cli.json")
    return files


def _read(path: Path) -> dict[str, Any]:
    """*path* through the credential gate kiro-cli's other ``cli.json`` readers use.

    ``{}`` when there is no such file. Raises one of ``_UNREADABLE_SETTINGS_ERRORS``
    for a file that is refused, too large, not JSON (a blank one included) or not an
    object.
    """
    return _settings(path)


def kiro_cli_telemetry_enabled(
    work_dir: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether the user left kiro-cli's telemetry on, failing closed.

    False when either environment switch is set, when either ``cli.json`` says
    ``telemetry.enabled`` is anything but ``true``, or when a ``cli.json`` that exists
    cannot be read. True only when nothing says off -- kiro-cli's own default.
    A workspace ``true`` does not undo a global ``false`` here, unlike in kiro-cli:
    an opt-out is kept whichever file holds it.
    """
    env = os.environ if environ is None else environ
    if any(name in env for name in TELEMETRY_OFF_ENV):
        return False
    try:
        files = _settings_files(work_dir)
    except Exception:  # no home directory: nothing proves the user left it on
        logger.warning("KAS telemetry: kiro-cli settings path unknown; sending telemetry off")
        return False
    for path in files:
        try:
            data = _read(path)
        except _UNREADABLE_SETTINGS_ERRORS as exc:
            logger.warning(
                "KAS telemetry: %s is unreadable (%s); sending telemetry off",
                path,
                type(exc).__name__,
            )
            return False
        if "telemetry.enabled" in data and data["telemetry.enabled"] is not True:
            return False
    return True


def with_telemetry_opt_out(capabilities: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """A copy of *capabilities* whose ``_meta.kiro`` carries ``telemetryEnabled``.

    Always sent, as kiro-cli's own KAS client does: KAS keeps telemetry on unless the
    client says otherwise. An opt-out is also logged once per process, because Crew
    cannot see whether the engine applied it (see :func:`_warn_opt_out_unconfirmed`).
    """
    meta = dict(capabilities.get("_meta") or {})
    kiro = dict(meta.get("kiro") or {})
    kiro["telemetryEnabled"] = enabled
    meta["kiro"] = kiro
    if not enabled:
        _warn_opt_out_unconfirmed()
    return {**capabilities, "_meta": meta}


def _warn_opt_out_unconfirmed() -> None:
    global _OPT_OUT_WARNED
    if _OPT_OUT_WARNED:
        return
    _OPT_OUT_WARNED = True
    logger.warning(
        "KAS telemetry: your kiro-cli telemetry is off, so Kiro Crew tells the KAS "
        "engine to turn its telemetry off. KAS does not confirm it, so Kiro Crew cannot "
        "check that it took effect."
    )


def kiro_cli_default_model(work_dir: Path | str | None = None) -> str:
    """kiro-cli's ``chat.defaultModel`` for *work_dir*, or ``""`` when none is usable.

    The workspace file wins over the global one, as in kiro-cli. A value that is not
    a plain model id, or a file that cannot be read, is skipped. Reads files: call it
    off the event loop.
    """
    try:
        files = _settings_files(work_dir)
    except Exception:  # no home directory: nothing to name
        return ""
    model = ""
    for path in files:
        try:
            value = _read(path).get("chat.defaultModel")
        except _UNREADABLE_SETTINGS_ERRORS:
            continue
        if isinstance(value, str) and _MODEL_ID_RE.match(value.strip()):
            model = value.strip()
    # ``auto`` is what KAS starts every new session on already: nothing to name.
    return "" if model.lower() == "auto" else model


def default_model_notice(model: str) -> str:
    """The session-start notice when kiro-cli's ``chat.defaultModel`` *model* did not land.

    Crew applies the setting to an unpinned KAS session itself; this names it
    when the session could not start on it (the account does not serve it, or
    KAS refused it), so the chat runs on KAS's own default instead.
    """
    if not model:
        return ""
    return (
        f"ℹ Your kiro-cli settings choose {model} as the default model "
        "(chat.defaultModel), but this chat could not start on it, so it "
        f"started on KAS's own default model. To use {model}, pick it in the "
        "model picker, or set it as Kiro Crew's default model in Settings."
    )

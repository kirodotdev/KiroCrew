"""Setup step: COMMON_DEPENDENCIES for the signed-in user, only with their yes, with an undo.

The network setting is per USER INSTANCE, not per sandbox: the write covers all of the user's
sandboxes. So the step never flips it silently:

1. read ``GetNetworkConfiguration``; a failed read stops with no write;
2. ``COMMON_DEPENDENCIES`` / ``OPEN_INTERNET`` already: nothing to do (never narrowed, never
   widened); any mode other than ``INTEGRATIONS_ONLY`` is left alone;
3. ask (a callable the dashboard route or the CLI supplies; ``None`` = non-interactive = no
   write; there is no default yes);
4. on yes, read AGAIN right before the write; if it differs from the first read stop with no
   write; save the prior (0600) first, then ``UpdateNetworkConfiguration`` with the prior
   ``customDomains`` carried verbatim (the call replaces the list), then read back;
5. undo: write the saved prior after a fresh read; refuse when the current setting is not
   what this step set.

No retry anywhere. This module adds no host of its own and never sends a mode other than
COMMON_DEPENDENCIES (apply) or the saved prior (undo).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from kiro_crew.managed_env.client import BffClient, SandboxError
from kiro_crew.managed_env.guard import GuardRefused

log = logging.getLogger(__name__)


class NetcfgRefused(Exception):
    """The answer could not be read as a network setting; nothing was written."""


@dataclass(frozen=True)
class NetConfig:
    access: str
    domains: tuple  # as the read returned them (order kept)
    row: bool  # True when the answer carried a customDomains member


def parse(out: dict) -> NetConfig:
    cfg = out.get("networkConfig") if isinstance(out, dict) else None
    if not isinstance(cfg, dict):
        raise NetcfgRefused("the answer has no networkConfig")
    access = cfg.get("networkAccess")
    if not isinstance(access, str) or not access:
        raise NetcfgRefused("the answer has no networkAccess")
    doms = cfg.get("customDomains")
    if doms is None:
        return NetConfig(access, (), False)
    if not isinstance(doms, list) or not all(isinstance(d, str) for d in doms):
        raise NetcfgRefused("customDomains is not a list of strings")
    return NetConfig(access, tuple(doms), True)


MODE_FROM = "INTEGRATIONS_ONLY"
MODE_TO = "COMMON_DEPENDENCIES"
SETTINGS_PATH = "Kiro Settings > Network"
PRIOR_FILE = "netcfg-prior.json"

DIALOG_TEXT = (
    "Crew needs to install its own runtime in your sandbox, and your network setting does not "
    "allow the download hosts yet. Agreeing opens about 80 well-known package and code hosts "
    "(GitHub, PyPI, npm, Node) for ALL of your sandboxes, not only this one, until you restore "
    "it. You can restore the old setting from Crew at any time, "
    f"or from any machine in {SETTINGS_PATH}."
)
#: The mode is snapshotted onto an environment when it is CREATED and Start reuses it, so a
#: stop/resume does not pick up the change.
APPLIES_NOTE = (
    "This applies to sandboxes you create after this; restarting an existing sandbox does not "
    "change it."
)

# result codes
APPLIED = "applied"
ALREADY = "already"
DECLINED = "declined"
NOT_INTERACTIVE = "not_interactive"
READ_FAILED = "read_failed"
CHANGED = "changed_meanwhile"
UNSUPPORTED_MODE = "unsupported_mode"
WRITE_FAILED = "write_failed"
READBACK_DIFFERS = "readback_differs"
RESTORED = "restored"
NO_PRIOR = "no_prior"
NOT_OURS = "not_ours"

Ask = Callable[[str], bool]


@dataclass
class StepResult:
    code: str
    message: str
    #: the settings shown to the user when two reads (or a read and what we set) disagree
    shown: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.code in (APPLIED, ALREADY, RESTORED)

    def as_dict(self) -> dict:
        return {"code": self.code, "ok": self.ok, "message": self.message, "shown": self.shown}


def _view(cfg: NetConfig) -> dict:
    return {"networkAccess": cfg.access, "customDomains": list(cfg.domains)}


def same(a: NetConfig, b: NetConfig) -> bool:
    """Equal as settings: the mode and the list as a set (the service may reorder it; an absent
    row reads as no list)."""
    return a.access == b.access and set(a.domains) == set(b.domains)


def _read(client: BffClient) -> NetConfig:
    return parse(client.netcfg_get())


def _read_failed(exc: Exception) -> StepResult:
    return StepResult(
        READ_FAILED, f"Could not read your network setting ({_why(exc)}); nothing was changed."
    )


def _why(exc: Exception) -> str:
    return str(exc)[:200]


# -- the prior file --------------------------------------------------------------------------------
def prior_path(state_dir: Path) -> Path:
    return Path(state_dir) / PRIOR_FILE


def _save_prior(
    state_dir: Path, prior: NetConfig, instance_id: str, now: Callable[[], float]
) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    body = {
        "networkAccess": prior.access,
        "customDomains": list(prior.domains),
        "row": prior.row,
        "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())),
        "instance_id": instance_id,
        "set": {"networkAccess": MODE_TO, "customDomains": list(prior.domains)},
    }
    tmp = d / (PRIOR_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, prior_path(d))


def _load_prior(state_dir: Path) -> Optional[tuple[NetConfig, NetConfig]]:
    """(prior, what this step set), or None when absent or unreadable."""
    try:
        raw = json.loads(prior_path(state_dir).read_text())
        prior = NetConfig(
            str(raw["networkAccess"]), tuple(str(x) for x in raw["customDomains"]), bool(raw["row"])
        )
        s = raw["set"]
        applied = NetConfig(
            str(s["networkAccess"]), tuple(str(x) for x in s["customDomains"]), True
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return prior, applied


# -- apply -----------------------------------------------------------------------------------------
def ensure_common_dependencies(
    client: BffClient,
    state_dir: Path,
    ask: Optional[Ask],
    *,
    instance_id: str = "",
    seen: Optional[NetConfig] = None,
    now: Callable[[], float] = time.time,
) -> StepResult:
    """Run the step. ``ask(DIALOG_TEXT)`` returns True only for an explicit yes; ``None`` =
    non-interactive. ``seen`` is the setting the user was shown (dashboard): if the first read
    differs, stop with no write.
    """
    try:
        first = _read(client)
    except (SandboxError, GuardRefused, NetcfgRefused) as exc:
        return _read_failed(exc)
    if first.access in (MODE_TO, "OPEN_INTERNET"):
        return StepResult(
            ALREADY, f"Your network setting is already {first.access}; nothing to change."
        )
    if first.access != MODE_FROM:
        return StepResult(
            UNSUPPORTED_MODE,
            f"Your network setting is {first.access!r}, which this step does not change.",
        )
    if seen is not None and not same(seen, first):
        return StepResult(
            CHANGED,
            "Your network setting changed after the question was shown; nothing was changed.",
            {"shown_to_you": _view(seen), "now": _view(first)},
        )
    if ask is None:
        return StepResult(
            NOT_INTERACTIVE,
            f"Your network setting is {first.access}, which blocks the download hosts Crew's "
            f"install needs (GitHub, PyPI); no yes was possible here, so nothing was changed. The "
            f"install may fail at those hosts. Agree in "
            f"the Crew dashboard, or set it in {SETTINGS_PATH}.",
        )
    if ask(DIALOG_TEXT) is not True:
        return StepResult(
            DECLINED,
            "Not changed. The install continues and may fail at the blocked download hosts.",
        )

    # the user may have taken minutes: read again immediately before the write
    try:
        second = _read(client)
    except (SandboxError, GuardRefused, NetcfgRefused) as exc:
        return _read_failed(exc)
    if not same(first, second) or second.access != MODE_FROM:
        return StepResult(
            CHANGED,
            "Your network setting changed while you were deciding; nothing was changed.",
            {"first_read": _view(first), "second_read": _view(second)},
        )
    try:
        _save_prior(state_dir, second, instance_id, now)
    except OSError as exc:
        return StepResult(
            WRITE_FAILED,
            f"Could not save your previous setting ({type(exc).__name__}); nothing was changed.",
        )
    try:
        client.netcfg_set(MODE_TO, list(second.domains))
    except (SandboxError, GuardRefused) as exc:
        status = getattr(exc, "status", None)
        definite = status is not None and 400 <= status < 500
        text = (
            "The service refused the change; nothing was changed."
            if definite
            else "The change may or may not have been applied: read your setting again."
        )
        return StepResult(
            WRITE_FAILED, f"{text} ({_why(exc)}) Your previous setting is saved on this machine."
        )
    want = NetConfig(MODE_TO, second.domains, True)
    try:
        back = _read(client)
    except (SandboxError, GuardRefused, NetcfgRefused) as exc:
        return StepResult(
            READBACK_DIFFERS,
            f"Could not read the setting back ({_why(exc)}); check it, and restore from Crew "
            f"or {SETTINGS_PATH} if needed. Your previous setting is saved on this machine.",
        )
    if not same(back, want):
        return StepResult(
            READBACK_DIFFERS,
            "The setting read back is not what was sent; stopped. Your previous setting is "
            "saved on this machine.",
            {"sent": _view(want), "read_back": _view(back)},
        )
    return StepResult(
        APPLIED,
        f"COMMON_DEPENDENCIES is on for all of your sandboxes. {APPLIES_NOTE} "
        f"Restore it from Crew or "
        f"{SETTINGS_PATH}.",
    )


# -- undo ------------------------------------------------------------------------------------------
def restore_network_setting(client: BffClient, state_dir: Path) -> StepResult:
    loaded = _load_prior(state_dir)
    if loaded is None:
        return StepResult(
            NO_PRIOR,
            f"Nothing saved on this machine to restore (the change may have been made on another "
            f"machine). Restore it in {SETTINGS_PATH}.",
        )
    prior, applied = loaded
    try:
        now = _read(client)
    except (SandboxError, GuardRefused, NetcfgRefused) as exc:
        return _read_failed(exc)
    if same(now, prior):
        prior_path(state_dir).unlink(missing_ok=True)
        return StepResult(
            RESTORED, "Your network setting is already the saved previous one; nothing to write."
        )
    if not same(now, applied):
        return StepResult(
            NOT_OURS,
            "Your network setting is no longer the one this step set (it was changed since); "
            "nothing was changed.",
            {
                "current": _view(now),
                "set_by_this_step": _view(applied),
                "saved_previous": _view(prior),
            },
        )
    try:
        client.netcfg_set(prior.access, list(prior.domains), ignore_stop=True)
    except (SandboxError, GuardRefused) as exc:
        return StepResult(
            WRITE_FAILED,
            f"The restore may not have been applied ({_why(exc)}); read your setting again, or "
            f"restore it in {SETTINGS_PATH}. The saved previous setting is kept.",
        )
    try:
        back = _read(client)
    except (SandboxError, GuardRefused, NetcfgRefused) as exc:
        return StepResult(
            READBACK_DIFFERS,
            f"Could not read the setting back ({_why(exc)}); the saved previous setting is kept.",
        )
    if not same(back, prior):
        return StepResult(
            READBACK_DIFFERS,
            "The setting read back is not the saved previous one; the saved file is kept.",
            {"read_back": _view(back), "saved_previous": _view(prior)},
        )
    prior_path(state_dir).unlink(missing_ok=True)
    return StepResult(
        RESTORED,
        "Your previous network setting is restored. A host that was allowed can stay "
        "reachable for up to 5 minutes.",
    )

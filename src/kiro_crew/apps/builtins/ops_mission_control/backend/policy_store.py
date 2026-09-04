"""The autonomy ceiling — app mode and act-rules — on the keystone floor.

Why this is a separate file from ``config.json`` and not just another key in it:

``mode`` (observe/propose/act) and ``autonomy_rules`` are the app's SECURITY CEILING, not a
preference. ``effective = min(app_mode, rule_mode)`` is only a ceiling if the thing being
minimised cannot be raised by the party it constrains — and the party it constrains is the
agent. In ``config.json`` it could be raised by exactly that party: an app's
``data/config.json`` is served over ``/api/apps/<name>/config`` WITHOUT session auth, and the
file is writable by any auto-approved agent shell. So a prompt-injected agent could set
``mode=act`` plus a matching rule and unlock a write against the user's production incident
tooling the operator never granted. Found in review.

This mirrors ``secrets.py`` exactly: one owner-only JSON file under ``config_dir()``, named
``ops_mission_control_policy.json``, which ``security._CREW_SECRET_LEAVES`` places on the
read+write keystone floor — the agent can neither read nor overwrite it. The authenticated
dashboard PUT handler is the sole writer and opens the path directly (via ``set_mode`` /
``set_rules`` here), bypassing the agent gate, so Settings still works.

Provider config that the agent legitimately reads (regions, prefixes, channel ids) stays in
``config.json``. Only the ceiling moves.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.ops_mission_control.backend.models import CorruptDocumentError
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Keystone leaf. MUST match the entry in ``security._CREW_SECRET_LEAVES`` — a test pins
#: the two equal, because a rename here without the fence entry silently un-protects the
#: ceiling, which is the whole failure this file exists to prevent.
POLICY_FILENAME = "ops_mission_control_policy.json"

_MODE_KEY = "mode"
_RULES_KEY = "autonomy_rules"

#: This operator's identity on the rotation. Namespaced by provider id, because it is a
#: provider config field everywhere else in the UI — it just cannot LIVE in provider config.
SCHEDULE_LOGIN_KEY = "schedule-file.github_login"

#: Whether an indeterminate schedule DISARMS this instance. Same fence, same reason: setting
#: it false restores fail-open gating, so an unreadable/unparseable schedule reports
#: ``on_shift=True`` and the off-shift refusal stops firing — the identity forgery's effect
#: without needing to know anyone's login.
SCHEDULE_STRICT_KEY = "schedule-file.strict_gating"

#: This operator's PagerDuty user id — the same authorization input as the GitHub login, one
#: adapter over. Review found it after the schedule-file fence went in, which is the useful
#: part: the class is "every input the off-shift refusal depends on", and identity is that
#: input for EVERY rotation source, not just the committed-file one.
PAGERDUTY_USER_KEY = "pagerduty.user_id"

#: Whether this instance claims the ``primary`` tier — i.e. may prune the SHARED ledger.
#:
#: The same fence for a different decision. The two identity keys above defeat the off-shift
#: refusal; this one defeats the ``not_primary`` gate directly, and needs no identity at all:
#: when the committed schedule names no ``leader:``, ``is_primary()`` falls back to this local
#: flag, so an agent that writes ``{"primary_instance": true}`` into ``config.json`` passes the
#: 409 gate on ``POST /ledger/hygiene`` and prunes and force-pushes a ledger it does not own.
#: A concurrent prune deletes the team's accumulated incident knowledge, which no later run
#: recovers. Fencing the identity inputs while leaving the flag they back-stop writable just
#: moves the same bypass one branch over. Found in review (GPT 5.6).
PRIMARY_KEY = "primary_instance"

#: Every OPERATOR-ONLY key, i.e. every value the agent must not be able to set.
#:
#: Two kinds live here and the second was found by auditing for the first:
#:
#: 1. **The authorization ceiling** — ``mode`` and ``autonomy_rules``. Raising these unlocks a
#:    provider write.
#: 2. **Outbound DESTINATIONS** — where this app sends things. ``ledger_sync_remote`` is the
#:    git remote the shared knowledge ledger is PUSHED to, and ``slack_channel`` is where every
#:    incident title, diagnosis and resource name is mirrored. An agent that can rewrite either
#:    redirects the app's own output to a place it controls: the ledger remote is straight data
#:    exfiltration of the team's accumulated incident knowledge, and the channel leaks the live
#:    incident stream. Verified before fixing: with ``config.json`` writable,
#:    ``ledger_sync.remote()`` returned ``https://attacker.example/exfil.git`` and
#:    ``slack_out.channel()`` returned the attacker's channel, and ``POST /ledger/hygiene`` —
#:    which the agent's own hygiene cron calls — pushes to exactly that remote.
#:
#: The ``*_enabled`` flags come along because a disabled destination is a safety state: being
#: able to turn the exchange ON is most of the way to redirecting it.
#:
#: ``branch`` is deliberately NOT here. It selects a ref inside a remote the operator already
#: chose, is already shape-validated at the write path, and cannot move data off-box.
#:
#: ``schedule-file.github_login`` IS here, because it is the THIRD input to the same
#: authorization decision as ``mode`` and ``autonomy_rules`` — and the only one that had been
#: left behind in ``config.json``. It answers "who am I on the rotation?", so an agent that
#: writes the current on-call member's login into config becomes, for authorization purposes,
#: that person: ``authorize_action`` -> ``_definitely_off_shift`` -> ``resolve_now`` matches the
#: live ``rotation.yaml`` window and the off-shift refusal is defeated, letting an off-shift
#: instance perform a real ack/resolve/silence against tooling the on-call instance owns. The
#: same forgery makes ``is_primary()`` true against a ``leader:`` this instance does not hold,
#: which bypasses the 409 ``not_primary`` gate on ``POST /ledger/hygiene`` and lets a non-leader
#: prune the SHARED ledger. Fencing two thirds of an authorization decision is not fencing it.
#: Found in review.
#:
#: ``primary_instance`` closes the last edge of that same bypass: forging identity is only one
#: way to reach the ledger-prune gate, and the local flag reaches it with no identity at all
#: whenever the schedule names no leader. The general rule these four keys share: **a security
#: refusal must not depend on an input the constrained party can write.**
OPERATOR_ONLY_KEYS: tuple[str, ...] = (
    _MODE_KEY,
    _RULES_KEY,
    "ledger_sync_remote",
    "ledger_sync_enabled",
    "slack_channel",
    "slack_enabled",
    SCHEDULE_LOGIN_KEY,
    SCHEDULE_STRICT_KEY,
    PAGERDUTY_USER_KEY,
    PRIMARY_KEY,
)


def policy_path() -> Path:
    """Absolute path to the keystone policy file (honors ``KIROCREW_HOME``)."""
    return config_dir() / POLICY_FILENAME


def _read() -> dict[str, Any]:
    """The stored ceiling, or ``{}`` when there is nothing readable.

    A DISPLAY/GATE read: it must degrade to the caller's DEFAULT rather than
    raise. Every caller left on this path has a RESTRICTIVE default -- an
    unreadable ceiling reads as ``observe`` with no act-rules, the destination
    and identity keys read as unset -- so degrading is the safe direction. The
    one key whose default is permissive (``PRIMARY_KEY``, see
    ``rotation.is_primary``) does NOT read through here: authority decisions
    go through :func:`read_authority`, which refuses what this read degrades.
    Found in review (GPT 5.6), two rounds: swallowing more here widened the
    fail-open door, and scoping it out as pre-existing was rightly rejected --
    a corrupt file granting prune authority is the corruption-enables-
    destruction failure this change exists to remove.

    An absent file is silent -- no ceiling has been stored yet, not a fault.
    A degraded read is logged: the state is restrictive but it is also
    silent, and nothing else would tell an operator their configuration has
    stopped applying.
    """
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "ops-mission-control: keystone policy file unreadable; every key will "
            "read as its restrictive default",
            exc_info=True,
        )
        return {}
    if not isinstance(raw, dict):
        # The same degradation reached without a parse failure -- same silence
        # problem, same log line.
        logger.warning(
            "ops-mission-control: keystone policy file root is not an object; "
            "every key will read as its restrictive default"
        )
        return {}
    return raw


def _read_for_update() -> dict[str, Any]:
    """The ceiling a read-modify-write is allowed to publish over.

    ``_PolicyLock`` documents that every writer here is a read-modify-write and
    that ``atomic_write`` REPLACES the whole file. So an empty base is not
    "nothing to carry forward" -- it is "discard every other operator-only key",
    and this file holds ALL of them: the autonomy ceiling, the outbound ledger
    remote and Slack channel, the rotation identity, the primary-instance flag.
    Only a MISSING file makes that base true.

    Losing them is not a preference reset. Each key is fenced onto the keystone
    floor precisely because the agent must not be able to set it, and every one
    reverts to a value the agent CAN influence: ``mode`` and ``autonomy_rules``
    fall back to a default the route re-derives, and the destination and identity
    keys fall back to absent, which is how the off-shift refusal and the
    ``not_primary`` gate get their inputs. Silently dropping the operator's
    ceiling on one transient EACCES is the failure this file exists to prevent,
    arriving by accident instead of by attack. The error propagates and the
    write is abandoned instead.

    Corruption propagates too (#7805, mirroring #7794): "cannot merge into" is
    not "safe to destroy", and for THIS file silent replacement re-opens the
    exact bypass the keystone floor exists to prevent -- a truncated document
    rewritten from empty reverts every fenced key to a value the constrained
    party can influence. Every corruption door raises the one named type,
    :class:`CorruptDocumentError`: a parse failure, a byte stream that is not
    UTF-8 (a ``ValueError`` but NOT a ``JSONDecodeError``, so unwrapped it
    would slip past every corruption clause at the callers), and valid JSON
    whose root is not an object (which parses without raising, so coercing it
    to ``{}`` would destroy a document nobody could read). No per-entry check
    beyond the root: values here are arbitrary JSON passed through verbatim,
    so a parsed document survives a read-write cycle by construction.
    """
    try:
        raw = json.loads(policy_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise CorruptDocumentError(exc.msg, exc.doc, exc.pos) from exc
    except UnicodeDecodeError as exc:
        raise CorruptDocumentError(
            f"policy file is not valid UTF-8: {exc.reason}",
            exc.object.decode("utf-8", "replace")[:120],
            0,
        ) from exc
    if not isinstance(raw, dict):
        raise CorruptDocumentError("policy file root is not a JSON object", str(raw)[:120], 0)
    return raw


#: Lock filename beside the policy file. Not the policy file itself: locking the file being
#: atomically REPLACED is useless, because `atomic_write` renames a new inode over it and the
#: lock travels with the old one.
_LOCK_FILENAME = ".policy.lock"


class _PolicyLock:
    """Exclusive lock around a read-modify-write of the keystone policy file.

    Every writer here is a read-modify-write (`_read`, mutate one key, `_write`), and
    `atomic_write` REPLACES the whole file — so two concurrent settings PUTs each write their
    own key onto a stale snapshot and the later one silently restores the other's old value.
    On this file that is a security defect, not a lost-update annoyance: an operator disabling
    `act` concurrently with any other settings change could have `act` restored by the second
    write, with both requests returning 200. Found in review.

    Third instance of one class in this app — `store._IndexLock` and `ledger._LedgerLock` came
    first, and the keystone was the one file left without a lock, which is the one where it
    matters most. Mirrors both: same `platform_compat.file_lock` routing so it works on Windows
    where `fcntl` does not exist, same lock-beside-the-file placement.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def __enter__(self) -> _PolicyLock:
        lock_file = config_dir() / _LOCK_FILENAME
        self._fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        platform_compat.acquire_lock(self._fd, exclusive=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                platform_compat.release_lock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None


def _write(data: dict[str, Any]) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True)
    # Fail-loud lockdown BEFORE any content lands, same as the secret store:
    # ``restrict_to_owner=True`` applies the owner-only DACL to the temp file
    # before the payload reaches it (a post-rename lockdown left the ceiling
    # readable under the inherited DACL on Windows for the write window, issue
    # #5285) and implies the owner-only POSIX mode. The default
    # ``restrict_on_error="raise"`` refuses to publish a ceiling it cannot
    # protect.
    #
    # No cleanup on failure: every failure inside ``atomic_write`` happens
    # BEFORE the final path is touched, so an unprotectable file never exists
    # at ``policy_path()`` at all. The unlink the old code ran on lockdown
    # failure existed to remove a NEW file already PUBLISHED at a wide DACL;
    # that state is unreachable now, and keeping the unlink would instead
    # delete the PREVIOUS, healthy governance ceiling on one transient lockdown
    # failure — silently resetting the operator's autonomy policy.
    atomic_write(policy_path(), payload, restrict_to_owner=True)


def read_mode(default: str) -> str:
    """The stored app mode, or ``default`` when unset. Validation is the caller's job."""
    value = _read().get(_MODE_KEY, default)
    return str(value) if isinstance(value, str) else default


def read_rules_raw() -> list[Any]:
    """The stored rule dicts, unparsed. ``rotation.load_rules`` validates each one."""
    raw = _read().get(_RULES_KEY)
    return raw if isinstance(raw, list) else []


def set_ceiling(*, mode: str | None = None, rules: list[Any] | None = None) -> None:
    """Persist the mode and/or the act-rules under ONE lock acquisition.

    The two values are ONE authorization decision — the gate computes
    ``effective = min(app_mode, rule_mode)`` — so a caller supplying both must commit both
    together. Writing them through two separately-locked calls made a concurrent pair of
    settings PUTs interleavable: request A's ``act`` could land between request B's read and
    write and be paired with B's broader rules, authorizing a provider write neither operator
    asked for. Each call was individually atomic, which is exactly the kind of gap that reads
    as safe. Found in review (GPT 5.6).

    The route already applies the two-phase validate-all-then-write-all discipline; this closes
    the remaining window BETWEEN the two writes, which no amount of ordering inside one request
    could fix because the interleaving comes from another request.

    ``None`` means "leave unchanged", so this is also the single-field writer.

    Raises ``OSError`` when the existing ceiling could not be read; see
    :func:`_read_for_update` for why that is not collapsed to an empty document
    here.
    """
    if mode is None and rules is None:  # pragma: no cover — programming error, not input
        raise ValueError("set_ceiling requires at least one of mode/rules")
    with _PolicyLock():
        data = _read_for_update()
        if mode is not None:
            data[_MODE_KEY] = mode
        if rules is not None:
            data[_RULES_KEY] = rules
        _write(data)


def set_mode(mode: str) -> None:
    """Persist the app mode. Dashboard-PUT only — see the module docstring.

    Thin wrapper over ``set_ceiling``: a caller writing BOTH halves of the ceiling must use
    ``set_ceiling`` directly so they commit atomically.
    """
    set_ceiling(mode=mode)


def set_rules(rules: list[Any]) -> None:
    """Persist the act-rules. Dashboard-PUT only. See ``set_mode`` on writing both."""
    set_ceiling(rules=rules)


def get(key: str, default: Any = None) -> Any:
    """Read one operator-only value off the fenced floor.

    The generic accessor for the destination keys. ``mode``/``autonomy_rules`` keep their own
    typed readers because they have validation the caller must not skip.

    Reads ONLY the keystone file, never ``config.json``. There is deliberately no migration
    from the agent-writable config: promoting a value found there onto the fenced floor would
    let the constrained party set its own ceiling — an agent shell writes ``config.json``,
    the next read lifts it to the keystone, and ``act`` is authoritative. See the module
    docstring; the migration this used to call was removed for exactly that.
    """
    if key not in OPERATOR_ONLY_KEYS:  # pragma: no cover — programming error, not input
        raise KeyError(f"{key!r} is not an operator-only key; use config.json for it")
    data = _read()
    return data.get(key, default)


def read_authority(key: str, default: Any = None) -> Any:
    """Read one operator-only value for an AUTHORITY decision -- strict, never lenient.

    :func:`get` degrades an unreadable or corrupt file to the caller's default, which is
    safe exactly when the default is the restrictive answer -- true for every key here
    except one. ``PRIMARY_KEY`` defaults to TRUE (``rotation.is_primary``), so the lenient
    read turns a truncated policy file into GRANTED ledger-prune authority: the corrupt
    file becomes the key that unlocks destroying shared knowledge, which is the exact
    corruption-enables-destruction failure #7805 exists to remove. Found in review
    (GPT 5.6), which correctly rejected scoping this out as pre-existing.

    Reuses :func:`_read_for_update`'s read, deliberately: one strict reader, one set of
    corruption doors (parse failure, non-UTF-8, non-object root), no second copy to
    drift. Only a MISSING file reads as the default -- a fresh install is genuinely the
    default state, while an unreadable or corrupt file is an UNKNOWN state, and an
    authority check that cannot read its input must refuse rather than guess. Callers
    whose failure posture is "deny" catch the raise and answer False.

    Not folded into :func:`get` wholesale because the restrictive-default keys WANT the
    lenient read: failing ``read_mode`` on a transient EACCES would wedge every
    authorization gate on a condition that clears by itself, to protect keys whose
    degraded answer is already the safe one.
    """
    if key not in OPERATOR_ONLY_KEYS:  # pragma: no cover — programming error, not input
        raise KeyError(f"{key!r} is not an operator-only key; use config.json for it")
    return _read_for_update().get(key, default)


def put(key: str, value: Any) -> None:
    """Write one operator-only value. Dashboard-PUT only — see the module docstring.

    Raises ``OSError`` when the existing ceiling could not be read, for the reason
    :func:`_read_for_update` gives.
    """
    if key not in OPERATOR_ONLY_KEYS:  # pragma: no cover — programming error, not input
        raise KeyError(f"{key!r} is not an operator-only key; use config.json for it")
    with _PolicyLock():
        data = _read_for_update()
        data[key] = value
        _write(data)


# There is intentionally NO `migrate_from_config_if_needed`. An earlier revision had one:
# on first read, if no keystone file existed yet, it lifted `mode`/`autonomy_rules` (and the
# destination keys) out of `config.json` and onto the fenced floor, to spare a "pre-fence
# install" a shadowed agent-writable copy. That migration WAS the hole it claimed to close.
#
# `config.json` is on no sensitive-path list, so an auto-approved agent shell can write
# `{"mode":"act","autonomy_rules":[{"source":"pagerduty","mode":"act","resource_glob":"*"}]}`
# to it. The next `app_mode()`/`load_rules()` — reached from `authorize_action` on every
# `POST /incident/action` — ran the migration, which promoted those values to the keystone
# and made them authoritative: `effective` resolved to `act`, the gate granted a real
# resolve/snooze against production paging the operator never authorized, and the same write
# redirected `ledger_sync_remote`/`slack_channel`. The constrained party could set its own
# ceiling, which is the one thing the keystone exists to prevent. Found in review (Opus 5).
#
# There is also no install to migrate: this app is new in this PR, so no `config.json` ever
# legitimately held the ceiling. The keystone is written ONLY by the authenticated dashboard
# PUT (`set_mode`/`set_rules`/`put`), and read ONLY from itself.

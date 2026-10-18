"""The ``config.json`` document on disk: reads, locked writes, meta stamp, cache keys.

Every config writer goes through this module, and it holds the unsandboxed-exec
policy read from the raw document. An owner of the :mod:`kiro_crew.config.loader`
facade; it never imports the facade.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib as _hashlib
import json
import logging
import os
import stat as _stat
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from kiro_crew import __version__, platform_compat, windows_acl
from kiro_crew.atomic_write import atomic_write, on_event_loop
from kiro_crew.aws_project_profiles import PROJECT_PROFILES_KEY, project_profile_refusals
from kiro_crew.config import migration as _migration
from kiro_crew.config import sections as _sections
from kiro_crew.config.loader_locations import config_local_path, config_path, read_config_text
from kiro_crew.config.migration import CONNECTIONS_UI_MIGRATION_MARKER
from kiro_crew.config.paths import config_dir
from kiro_crew.config.validation import _CONFIG_CACHE
from kiro_crew.owner_only_files import GROUP_OTHER_BITS, is_owner_only_target

logger = logging.getLogger("kiro_crew.config.loader")


def unsandboxed_exec_platform_default() -> bool:
    """What an UNDECLARED ``agent.sandbox_allow_unsandboxed_exec`` resolves to here.

    True on Windows, where Kiro Crew has no native OS wrapper to apply — no Linux
    user namespace, no macOS ``sandbox-exec``, and nothing installable that would
    produce one, so fail-closing there refuses every script cron, hook, app
    backend, MCP probe and provider CLI on the platform in perpetuity, with no
    operator action able to satisfy the check.

    False everywhere else. A backend-less Linux or macOS host is one whose backend
    is BROKEN or one AppArmor profile away from working, so it keeps fail-closing
    and ``sandbox``'s guidance names the profile that restores isolation; running
    unconfined there would hide a repairable host.

    Resolved HERE, into the dataclass value, rather than downstream from whether
    the key is present. Presence cannot carry the meaning safely:
    :meth:`KiroCrewConfig.save` publishes the whole in-memory snapshot through
    ``to_dict()`` -> ``asdict(self.agent)``, so every full-document write (the boot
    default-config write, CLI one-shots) materializes this key. Were the effective
    policy keyed on presence, that write would silently turn "never decided" into a
    declared lockdown and re-brick every Windows spawn. Resolving into the value
    makes such a round-trip write ``true`` on Windows, which changes nothing.

    A function rather than a module constant so tests can pin the verdict without
    patching ``sys.platform`` process-wide.
    """
    return sys.platform == "win32"


def unsandboxed_exec_declared() -> bool:
    """Whether the operator explicitly DECLARED ``agent.sandbox_allow_unsandboxed_exec``.

    Reports key PRESENCE, in either state, in ``config.json`` or the
    ``config.local.json`` overlay that deep-merges over it.

    DIAGNOSTIC ONLY — it must never gate execution. The effective policy is the
    resolved dataclass value (see :func:`unsandboxed_exec_platform_default`); this
    exists so a message can tell "you set this to false" apart from "you never set
    it", and so the audit event can name an operator grant rather than a platform
    one. Because a full-document ``save()`` materializes the key, a host that never
    answered can read as declared afterwards — acceptable for a label, and the
    reason this must not decide anything.

    An unreadable or non-object document contributes nothing: a corrupt config is
    not a declaration.
    """
    return _agent_key_declared("sandbox_allow_unsandboxed_exec")


def acp_backend_declared() -> bool:
    """Whether the config documents hold a harness CHOICE in ``agent.acp_backend``.

    The value that counts is the one a load would read: the ``config.local.json``
    overlay's when it names the key, else ``config.json``'s. Absent and ``""`` are
    "no choice" and resolve to the product default; any other stored value, valid
    or not, is a choice. Read from disk, like :func:`unsandboxed_exec_declared`,
    so every caller answers the same question about one host instead of trusting
    an in-memory value that already holds the RESOLVED harness.
    """
    value = _stored_agent_value("acp_backend")
    return value is not _ABSENT_AGENT_KEY and value != ""


#: What :func:`_stored_agent_value` returns for a key neither document names, so a
#: stored JSON ``null`` (a malformed choice) stays distinct from "absent".
_ABSENT_AGENT_KEY = object()


def _raw_agent_section(path: Path) -> dict | None:
    """The raw ``agent`` object of the config document at *path*, or ``None``.

    An unreadable or non-object document contributes nothing: a corrupt config is
    not a declaration.
    """
    try:
        doc = json.loads(read_config_text(path))
    except (OSError, ValueError):
        return None
    agent = doc.get("agent") if isinstance(doc, dict) else None
    return agent if isinstance(agent, dict) else None


def _stored_agent_value(key: str) -> object:
    """The raw stored ``agent.<key>`` a load would read: overlay first, then base."""
    for path in (config_local_path(), config_path()):
        agent = _raw_agent_section(path)
        if agent is not None and key in agent:
            return agent[key]
    return _ABSENT_AGENT_KEY


def _agent_key_declared(key: str) -> bool:
    """Whether ``config.json`` or the ``config.local.json`` overlay names ``agent.<key>``."""
    return _stored_agent_value(key) is not _ABSENT_AGENT_KEY


def _lock_target(path: Path) -> Path:
    """The path ``update_config_locked`` would put its lock sidecar beside.

    It resolves a symlinked config before locking, so that -- not *path* -- is
    what the containment question has to be asked about. Returns *path* unchanged
    when it is not a symlink or cannot be resolved, which is the conservative
    answer either way: an unresolvable path fails the containment check.
    """
    try:
        return path.resolve() if path.is_symlink() else path
    except OSError:
        return path


def _raw_config() -> dict:
    """Load raw config.json as a dict, re-reading the file on every call.

    Uncached on purpose: callers want the bytes on disk right now, and the
    validated-config cache is keyed for :meth:`KiroCrewConfig.load`, not for
    this raw view. An absent, unreadable or non-object file reads as ``{}``, so
    the annotated ``dict`` holds for every caller.
    """
    p = config_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(read_config_text(p))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


class ConfigWriteRefused(ValueError):
    """A config document was refused at the publish floor and nothing was written.

    Raised by :func:`write_config_atomically` before any byte reaches disk, for
    content that must never be persisted: a provider key in plaintext under
    ``agent.deepseek_env``, where the only admissible value is a
    ``secret://<vault name>`` reference (``sections.deepseek_env_plaintext_keys``),
    an ``aws.project_profiles`` entry the load would ignore, and an ``agent.env``
    entry no agent process would receive (``sections.agent_env_write_refusals``).
    The message names env-var KEYS only, never the value, so it is safe to print
    on a terminal and to log. A ``ValueError`` so the CLI's existing refusal shape
    applies, and so a caller cannot mistake it for the unreadable-file case
    :class:`ConfigReadError` names.
    """


class ConfigReadError(Exception):
    """``config.json`` exists but could not be read as a config object.

    Raised by :func:`read_config_for_update`, whose callers are about to write
    the value back, and by :meth:`KiroCrewConfig.save` when the instance was
    loaded while the base file could not be read. It deliberately does NOT
    inherit from ``OSError`` or ``ValueError`` so an existing broad ``except
    OSError`` around a write cannot swallow it and resume the clobbering path.
    """


def read_config_for_update(path: Path | None = None) -> dict:
    """Read ``config.json`` for a read-modify-write, failing CLOSED.

    Every partial config update (flip one toggle, persist one channel) has to
    read the whole file, mutate one key, and write it all back. The obvious
    ``try: json.loads(...) except Exception: data = {}`` is a **data-loss bug**
    in that shape: the fallback is indistinguishable from "the user has no
    settings", so the write-back replaces a fully populated config with a
    single-key one. Every setting the user ever chose is gone, silently, and
    the endpoint still reports success.

    The read fails for mundane reasons — most commonly a *torn read*: a
    truncate-then-write writer (a hand edit, another tool, an older build; no
    product writer, see ``TestNoRawOpenWriteOfConfig``) leaves a window in which
    a concurrent reader observes a half-written file. That window is small,
    which is exactly what makes the resulting loss so hard to reproduce and
    report.

    So: an **absent** file returns ``{}`` (a genuine empty starting point), and
    an unreadable or non-object file raises :class:`ConfigReadError`. Callers
    must let that abort the update — leaving the existing file untouched is
    always better than overwriting it with defaults.

    Pair this with :func:`kiro_crew.atomic_write.atomic_write` on the way out so
    the write cannot create the torn window for the next reader.
    """
    p = path if path is not None else config_path()
    try:
        if not p.exists():
            return {}
        raw = json.loads(read_config_text(p))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # UnicodeDecodeError is a ValueError, NOT an OSError, so it needs naming
        # explicitly: a config containing invalid UTF-8 (a truncated multi-byte
        # sequence from a torn write, or a mojibake'd hand edit) would otherwise
        # escape this controlled path and crash the caller instead of returning
        # the clean "config unreadable" refusal.
        raise ConfigReadError(f"could not read config at {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigReadError(f"config at {p} is not a JSON object (got {type(raw).__name__})")
    return raw


def _refuse_unpublishable(data: dict, path: Path) -> None:
    """Refuse *data* if it carries content this write must not land in ``config.json``.

    The publish floor for every writer that lands a config document -- the locked
    read-modify-write (``update_config_locked``, behind ``config set`` and every
    dashboard PUT) and the whole-document :meth:`KiroCrewConfig.save` alike -- so
    each rule holds however a value arrived. Two rules, both on mappings of
    environment variables an agent process receives: a provider key in plaintext
    under ``agent.deepseek_env`` (:func:`_refuse_deepseek_env_plaintext`), and an
    ``agent.env`` entry no spawn would ever apply (:func:`_refuse_agent_env_entries`).
    Keyed on the document's SHAPE rather than on the path, so an agent spec written
    through the same function is untouched -- it has neither mapping.
    """
    _refuse_deepseek_env_plaintext(data, path)
    _refuse_agent_env_entries(data, path)


def _refuse_deepseek_env_plaintext(data: dict, path: Path) -> None:
    """Refuse a plaintext provider key under ``agent.deepseek_env``.

    That mapping takes ``secret://`` references only, and a plaintext that reaches
    disk is already the defect the spawn-time validator would later name.

    What is refused is what THIS write does: a plaintext it introduces, or a
    mapping it changes while a plaintext entry stays in it. A plaintext an older
    build or a hand edit already landed, left exactly as the write found it, is not
    this write's doing -- every writer in the product reaches this function, most
    of them for fields nothing to do with this one and with no handler for the
    refusal, so refusing them all would turn one stale value into an unhandled
    exception on every Slack allowlist change and every unrelated dashboard write.
    Such a write publishes, and the stale value is named on the log (keys only,
    never the value) so it is not silent; the DeepSeek spawn that would use it is
    still refused by the validator, and a write that does touch the mapping has to
    remove the plaintext first. The prior document is read from *path*; when it
    cannot be read the plaintext cannot be shown pre-existing and is refused, so an
    absent or unparseable file fails closed.
    """
    agent = data.get("agent") if isinstance(data, dict) else None
    mapping = agent.get("deepseek_env") if isinstance(agent, dict) else None
    plaintext = _sections.deepseek_env_plaintext_keys(mapping)
    if not plaintext:
        return
    names = ", ".join(repr(key) for key in plaintext)
    if _deepseek_env_on_disk(path) == mapping:
        logger.warning(
            "config.json already holds a literal value under agent.deepseek_env entry %s; "
            "this write left that mapping as it found it. That mapping takes a "
            "'secret://<vault name>' reference only, and a DeepSeek session is refused "
            "while it stands: save the key under Settings > Secrets, then map it as "
            "'secret://<vault name>'.",
            names,
        )
        return
    raise ConfigWriteRefused(
        f"agent.deepseek_env entry {names} holds a literal value, so the config "
        "write was refused: this mapping takes a 'secret://<vault name>' reference "
        "only, and no provider key is ever stored in config.json. Save the key "
        "under Settings > Secrets, then map it as 'secret://<vault name>'."
    )


def _deepseek_env_on_disk(path: Path) -> object:
    """The ``agent.deepseek_env`` value the document at *path* holds, or ``None``.

    Read for :func:`_refuse_deepseek_env_plaintext` alone, to tell a plaintext this
    write introduces from one it merely re-writes unchanged. ``None`` for an absent,
    unreadable or unparseable file -- and for a document with no such mapping --
    which the caller treats as "not shown pre-existing".
    """
    return _agent_key_on_disk(path, "deepseek_env")


def _agent_key_on_disk(path: Path, key: str) -> object:
    """The ``agent.<key>`` value the document at *path* holds, or ``None``.

    ``None`` for an absent, unreadable or unparseable file and for a document
    without that key, which every caller treats as "not shown pre-existing".
    """
    try:
        document = json.loads(read_config_text(path))
    except (OSError, ValueError):
        return None
    agent = document.get("agent") if isinstance(document, dict) else None
    return agent.get(key) if isinstance(agent, dict) else None


def _refuse_agent_env_entries(data: dict, path: Path) -> None:
    """Refuse an ``agent.env`` entry that no agent spawn would ever apply.

    ``agent.env`` drops such an entry at load (``sections.coerce_agent_env``) and
    at spawn (``acp.agent_env``) with only a gateway-log line, so a write that
    stored it would succeed, the entry would sit in the file, and the variable
    would never reach the agent. Refusing it here names the key and the reason at
    the moment it is saved -- ``kirocrew config set`` prints it, a dashboard write
    answers 400 with it -- and nothing is written. The rule is
    ``sections.agent_env_write_refusals``; the message carries keys and reasons
    only, never a value.

    Scoped to what THIS write does, exactly like
    :func:`_refuse_deepseek_env_plaintext`: a refused entry a hand edit or an older
    build already landed, in a mapping this write leaves exactly as it found it,
    publishes with a warning naming the key, because every writer in the product
    reaches this function and most of them have nothing to do with ``agent.env``.
    A write that changes the mapping has to leave it with no refused entry. An
    unreadable prior file cannot show the entry pre-existing, so it is refused.
    """
    agent = data.get("agent") if isinstance(data, dict) else None
    mapping = agent.get("env") if isinstance(agent, dict) else None
    refusals = _sections.agent_env_write_refusals(mapping)
    if not refusals:
        return
    detail = "; ".join(refusals)
    if _agent_key_on_disk(path, "env") == mapping:
        logger.warning(
            "config.json already holds agent.env entries no agent process will receive "
            "(%s); this write left that mapping as it found it.",
            detail,
        )
        return
    raise ConfigWriteRefused(
        f"agent.env refuses {detail}. Nothing was written, because an agent "
        "process would never receive that variable: remove or rename the entry, "
        "and keep credentials under Settings > Secrets."
    )


def _refuse_aws_project_profiles(data: dict, path: Path) -> None:
    """Refuse an ``aws.project_profiles`` entry the load would drop.

    The load ignores such an entry with only a gateway-log line
    (``aws_project_profiles.coerce_project_profiles``), so a write that stored it
    would succeed while the project silently kept the default credential chain.
    Refusing it names the directory and the rule when it is saved -- ``kirocrew
    config set`` prints it, a dashboard write answers 400 -- and nothing is
    written. The message never carries a profile value.

    Scoped to what THIS write does, like :func:`_refuse_unpublishable`: a bad
    entry a hand edit already landed, in a mapping this write leaves exactly as it
    found it, publishes with a warning, because every writer in the product
    reaches this function. A write that changes the mapping must leave it clean.
    An unreadable prior file cannot show the entry pre-existing, so it is refused.
    """
    aws = data.get("aws") if isinstance(data, dict) else None
    mapping = aws.get("project_profiles") if isinstance(aws, dict) else None
    refusals = project_profile_refusals(mapping)
    if not refusals:
        return
    detail = "; ".join(refusals)
    if _aws_project_profiles_on_disk(path) == mapping:
        logger.warning(
            "config.json already holds %s entries the load ignores (%s); this write "
            "left that mapping as it found it.",
            PROJECT_PROFILES_KEY,
            detail,
        )
        return
    raise ConfigWriteRefused(
        f"{PROJECT_PROFILES_KEY} refuses {detail}. Nothing was written: fix or remove the entry."
    )


def _aws_project_profiles_on_disk(path: Path) -> object:
    """The ``aws.project_profiles`` value the document at *path* holds, or ``None``."""
    try:
        document = json.loads(read_config_text(path))
    except (OSError, ValueError):
        return None
    aws = document.get("aws") if isinstance(document, dict) else None
    return aws.get("project_profiles") if isinstance(aws, dict) else None


def write_config_atomically(path: Path, data: dict, *, fsync: bool = False) -> None:
    """Write a config dict to *path* atomically, PRESERVING its permissions.

    The companion to :func:`read_config_for_update`. Two properties matter:

    * **Atomic** (tmp+rename) so a concurrent reader can never observe a
      half-written file. A truncate-then-write leaves a window in which a reader
      sees invalid JSON; a reader that mistakes that for "no settings" will write
      the emptiness back and destroy the user's config.
    * **Mode-preserving.** Because tmp+rename creates a NEW inode, the umask
      default (typically ``0644``) would silently replace an operator's tightened
      ``0600``. ``config.json`` can hold inline credentials, so a settings write
      must never widen who can read it. An existing file's mode is carried over
      (minus group/other access inside the data home); a newly created one
      defaults to owner-only.

    ``atomic_write``'s ``mode`` routes through ``fchmod_safe``, which applies the
    mode on POSIX and is a documented no-op on Windows.

    **Windows gets a real owner-only DACL, not just the inert mode.** The lockdown
    is applied in-process through ``advapi32`` (measured at 0.24 ms, against 313 ms
    for the ``icacls`` subprocess), so it is safe on the event loop even though this
    function is called from ``async`` request handlers and from
    ``KiroCrewConfig.save()``. Since ``config.json`` can carry inline provider
    tokens and API keys, applying it is the correct default rather than a duty
    pushed onto each caller.

    The two guarantees do not collide, because they apply on different platforms:
    mode preservation is a POSIX concept (Windows has no bits to preserve), and
    the DACL is a Windows concept. Hence the platform branch below rather than
    passing both to ``atomic_write``, which refuses ``restrict_to_owner=True``
    alongside a wider explicit ``mode``.

    **On a network-homed data home the DACL turns on the CALLER, not the volume.**
    The in-process lockdown costs 0.24 ms on a local volume but is bounded only by
    SMB on a UNC or mapped-drive path, which a write running inline on the event
    loop cannot afford. That is a fact about the calling thread, so it is asked as
    one, via :func:`kiro_crew.atomic_write.on_event_loop`. A caller that has
    offloaded this write -- ``dashboard/chat_utils.run_config_write``, any
    ``asyncio.to_thread`` wrapper, and every CLI and startup path, which have no
    loop at all -- blocks only its own thread and therefore gets the owner-only
    DACL on **any** volume. Only a write still inline on the loop falls back to
    classifying the volume and skipping when it is remote.

    **Symlinks are followed, not replaced.** ``os.replace`` renames over the link
    itself, turning a symlinked ``config.json`` into a regular file and orphaning
    its target — whereas the ``write_text`` this replaced followed the link and
    updated the target. Symlinking the config into a dotfiles repo is a normal
    setup, so the target is resolved first to preserve that behavior.
    """
    _refuse_unpublishable(data, path)
    _refuse_aws_project_profiles(data, path)
    # Resolve BEFORE stat/write so a symlinked config keeps pointing at its
    # target (and the mode preserved is the target's, not the link's).
    try:
        if path.is_symlink():
            path = path.resolve()
    except OSError:
        pass
    # Decide the Windows lockdown HERE, before the stat and the mkdir below and
    # before anything atomic_write does -- every one of those is a round-trip on a
    # network-homed data home. A DACL write to a UNC or mapped-drive path is an
    # unbounded SMB round-trip, so when it cannot be afforded it has to be ruled
    # out before the work starts rather than part way through.
    #
    # But whether it can be afforded is a question about the CALLING THREAD, not
    # about the volume. The volume was only ever a proxy: this function is
    # synchronous and async dashboard handlers reach it inline, where an unbounded
    # wait stalls the one loop the whole gateway shares. Off the loop there is
    # nothing to stall -- a worker started by ``run_config_write`` /
    # ``asyncio.to_thread``, a CLI invocation, a startup path all block only
    # themselves -- so the same predicate ``atomic_write`` already gates its own
    # unbounded-on-Windows step on decides here too, and a network-homed data home
    # gets the DACL whenever its caller has offloaded the write.
    #
    # This sits just AFTER the symlink resolve rather than at the very top of the
    # function, and deliberately: a config symlinked into a dotfiles repo (which
    # the docstring above calls a normal setup) can point at a DIFFERENT volume
    # than the link, so classifying before resolving would classify the wrong one.
    # The resolve is two stats; the earliest CORRECT point is here.
    lock_down = platform_compat.IS_POSIX
    if not platform_compat.IS_POSIX:
        if not on_event_loop():
            # Nothing to stall, so the volume does not decide -- and is not even
            # classified, because its answer could only weaken the outcome.
            lock_down = True
        else:
            try:
                lock_down = windows_acl.volume_is_local(path)
            except Exception:
                # A descriptor API that cannot be loaded cannot tell us the volume
                # is local, and the lockdown would have failed on this host anyway.
                lock_down = False
            if not lock_down:
                logger.warning(
                    "config write: %s is on a non-local volume and this write is "
                    "running on the event loop, so the owner-only DACL was "
                    "SKIPPED to avoid stalling the loop on SMB; the file may be "
                    "readable by other local users. Offloading the write "
                    "(dashboard/chat_utils.run_config_write) applies the DACL "
                    "here too",
                    path,
                )
    try:
        mode = _stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError:
        mode = 0o600
    if is_owner_only_target(path):
        # Preserving never means keeping it readable by others: inside the data
        # home every file is owner-only (kiro_crew.owner_only_files), so a 0644
        # left by an older version narrows to 0600 here. A config symlinked out of
        # the home resolved to its target above and keeps the target's mode.
        mode &= ~GROUP_OTHER_BITS
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    if platform_compat.IS_POSIX:
        atomic_write(path, payload, fsync=fsync, mode=mode)
    elif lock_down:
        # Windows: the mode bits above are inert (fchmod_safe is a documented
        # no-op), so there is nothing to preserve and no conflict with
        # restrict_to_owner's implied 0600. Taking the lockdown here rather than
        # leaving it to callers also closes the window a post-write lockdown
        # would leave: atomic_write applies the DACL to the temp file BEFORE any
        # content reaches it, so an inline credential never exists in a file
        # readable by other local accounts.
        #
        # restrict_on_error="warn", not the default "raise": config.json must not
        # become unwritable because a DACL could not be applied. Same trade-off
        # sel.py and dashboard/refresh_tokens.py already take, and strictly
        # better than the previous behavior, which applied no DACL at all.
        atomic_write(
            path,
            payload,
            fsync=fsync,
            restrict_to_owner=True,
            restrict_on_error="warn",
        )
    else:
        # Reached only by a write still INLINE ON THE LOOP whose volume is not
        # local: exactly the write this branch did before the lockdown was added,
        # so such a data home is no worse off than before. The residual is real and
        # declared -- the file keeps the ACL it inherits from its parent -- but it
        # is now per CALLER rather than per platform: offloading a caller moves it
        # to the branch above and it gets the DACL with no change needed here.
        atomic_write(path, payload, fsync=fsync, mode=mode)


def coerce_dict_section(doc: dict, key: str) -> dict:
    """Return ``doc[key]`` as a dict, REPLACING a degraded (non-dict) value.

    The validated loader treats a malformed top-level section (``"workspaces":
    []``, ``"agents": "oops"``) as absent and supplies defaults, so the
    dataclass view never sees the damage. A raw delta mutator working on the
    document as read (``update_config_locked``'s ``mutate``) must take the
    same posture: a plain ``doc.setdefault(key, {})`` hands the degraded value
    straight back, and the mutator then crashes on ``.values()``/``[name]``
    with an HTTP 500 for the caller. Replacing the degraded section with a
    fresh dict matches what the validated load already presents everywhere
    else, and happens inside the same locked write, so the repair is atomic
    with the mutation that needed it.
    """
    section = doc.get(key)
    if not isinstance(section, dict):
        section = {}
        doc[key] = section
    return section


def coerce_config_field(
    section: dict,
    key: str,
    expected: type | tuple[type, ...],
    default: Any,
) -> Any:
    """Return ``section[key]`` when it is ``expected``-typed, else ``default``.

    The sibling of :func:`coerce_dict_section` for the NESTED values a raw
    reader consumes, not the whole section. The validated loader degrades a
    wrong-typed field to its default (see ``config.resolution._coerced_section``
    for sections), so the dataclass view never raises on a hand-edited
    ``config.json`` with a mistyped field. A raw reader that pulls the value
    straight out of the parsed document has no such guard, so a non-numeric
    ``knowledge.fetch_top_n`` reaches ``int()`` and a non-string
    ``slack.command`` reaches a char loop — each a loud ``TypeError`` on input
    the loader itself degrades gracefully. Routing those reads through here
    gives them the loader's posture in one place: a wrong-typed value falls
    back to the default and is logged, instead of taking the request down.

    An ABSENT key takes the default silently — that is the ordinary
    unconfigured state, not a degradation. ``bool`` is deliberately NOT a
    stand-in for ``int`` here: a reader asking for an ``int`` field that finds a
    ``bool`` gets the default, matching JSON's own type distinction.
    """
    if key not in section:
        return default
    value = section[key]
    # ``bool`` is a subclass of ``int``; keep them distinct so an ``int``
    # field does not silently accept ``true``/``false`` (and vice versa).
    want_int = expected is int or (
        isinstance(expected, tuple) and int in expected and bool not in expected
    )
    if want_int and isinstance(value, bool):
        logger.warning(
            "config: '%s' is a boolean, expected %s — using default", key, _type_names(expected)
        )
        return default
    if isinstance(value, expected):
        return value
    logger.warning(
        "config: '%s' is not %s (got %s) — using default; the configured value is NOT in effect",
        key,
        _type_names(expected),
        type(value).__name__,
    )
    return default


def _type_names(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__


@contextlib.contextmanager
def _config_write_lock(p: Path, *, wait: bool = True) -> Iterator[None]:
    """Hold the sidecar advisory lock that serializes config-file writers.

    THE one lock path for a config write: :func:`update_config_locked` and
    :meth:`KiroCrewConfig.save` both come through here, so a second, subtly
    different lock file can never reappear — the sidecar is always
    ``<p>.lock`` beside the file being written (callers resolve a symlinked
    config first, so it lands beside the TARGET; see :func:`_lock_target`).

    The sidecar is opened (created if absent) and the advisory lock is taken
    via :func:`platform_compat.file_lock` — ``fcntl.flock`` on POSIX, a bounded
    ``msvcrt.locking`` spin on Windows, both fail-closed. The sidecar file
    itself is deliberately left in place on exit: unlinking a lockfile that a
    concurrent waiter has already opened re-introduces the race the lock
    exists to prevent (the waiter would hold a lock on an unlinked inode while
    a third writer creates a fresh sidecar and locks that instead), and on
    Windows deleting a file another process holds open fails outright. One
    stable sidecar per config file is the whole lifecycle.

    **Lock ordering.** A caller that also holds the loop-side asyncio config
    lock (``dashboard/handlers/agents._get_config_lock``) must acquire that
    FIRST and this flock second, from a worker thread — the order
    ``dashboard/chat_utils.run_config_write`` documents. Taking them in the
    other order can deadlock against ``run_config_write`` holders. A POSIX
    ``flock`` wait blocks its thread, so this must never be entered on the
    event-loop thread with ``wait=True``; async callers offload (see
    :meth:`KiroCrewConfig.save`).
    """
    lock_path = p.parent / (p.name + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with platform_compat.file_lock(fd, exclusive=True, wait=wait):
            yield
    finally:
        os.close(fd)


def update_config_locked(
    path: Path | None = None,
    *,
    mutate: Callable[[dict], dict | None],
    fsync: bool = False,
    stamp_meta: bool = True,
    on_corrupt: Literal["fail", "reset"] = "fail",
    wait_for_lock: bool = True,
    after_write: Callable[[], None] | None = None,
) -> dict:
    """Perform an atomic read-modify-write of a config file under an advisory lock.

    ``after_write`` runs INSIDE the lock, only after the rename has committed the
    new document (never when ``mutate`` returned ``None``): the hook for a
    companion record that must follow the registry change and must not be
    interleaved with another writer's -- the crew-teams drop that accompanies a
    crew delete. Its exceptions propagate; the config write has already landed.

    The locked primitive for every DIRECT
    ``write_config_atomically(config_path())`` caller outside this module, and
    the required path for new ``config.json`` mutations.  **No such caller
    remains** -- the dashboard agents endpoint, ``security.py``, the apps manager
    and the CLI setup wizard all route through the locked path, and ``memory.py``
    reaches this function through ``dashboard/chat_utils.run_config_write``.
    ``TestEveryConfigWriterIsLocked`` in
    ``test/test_config_rmw_preserves_settings.py`` is the ratchet that keeps the
    list from regrowing.

    Read "direct caller outside this module" strictly: it is the exact set the
    ratchet checks, and it is NOT the same as "every writer that reaches
    ``config.json``".  :meth:`KiroCrewConfig.save` calls
    :func:`write_config_atomically` and does NOT come through here -- but it holds
    the SAME sidecar lock (via :func:`_config_write_lock`), so its rename cannot
    land inside this function's read-modify-write.
    ``save`` is still a whole-document publish of in-memory state, not an RMW:
    a stale snapshot saved after a locked update overwrites it with older
    values.  The lock fixes interleaving, not staleness.

    A SECOND way to bypass this lock is to reach ``config_path()`` through
    ``kiro_crew.agent._atomic_json_write``, which takes no sidecar lock at all.
    ``TestEveryConfigWriterIsLocked`` does not see such a writer -- it matches
    calls to :func:`write_config_atomically`, and that one makes none -- so
    ``TestTheAtomicJsonWriteConfigFamilyIsRatcheted`` in the same file scans for
    that spelling separately and pins its population to an EMPTY baseline: the
    dashboard's per-channel savers (``messaging._LockedSectionWrite``), the STT
    PUT and the MCP gateway-enable toggle all come through here.

    Such a writer would rely on the in-process asyncio ``_get_config_lock()``
    only, which serializes same-loop callers and nothing else, so it could still
    interleave with a holder of this lock; ``api_feishu_config_save`` is the
    shape to copy for a new handler.  Note that an ALIASED import of
    :func:`write_config_atomically` would evade the sibling ratchet for the same
    matching reason.

    Contract:

    * **Isolation.** An advisory file lock is held for the entire
      read-modify-write, so two concurrent callers are serialized: neither can
      land between the other's read and write.
    * **Sidecar lockfile.** The lock lives on ``<path>.lock``, NOT on the
      config file's own fd.  ``write_config_atomically`` replaces the inode
      (tmp + rename), so a lock taken on the config file's fd would not
      serialize against the rename — a second opener after the rename gets a
      NEW fd on the NEW inode and takes the lock instantly, defeating the
      purpose.
    * **Fail-closed read (default).** :func:`read_config_for_update` is used
      inside the critical section; with ``on_corrupt="fail"`` (the default), an
      unreadable or malformed config raises :class:`ConfigReadError`, aborts
      the update, and the lockfile is released.  The existing file is never
      overwritten with defaults.
    * **Reset-on-corrupt (opt-in).** With ``on_corrupt="reset"``, a
      :class:`ConfigReadError` inside the critical section is caught WHILE THE
      LOCK IS STILL HELD and the *mutate* callback is invoked with ``{}``.
      The caller's write therefore happens in the same lock hold as the read
      attempt, closing any window for a concurrent writer to land between.
      The resulting file is written with mode ``0o600`` (no existing mode to
      preserve from a corrupt file).
    * **Mode-preserving write.** :func:`write_config_atomically` preserves the
      existing file's permission bits, so a tightened ``0600`` is not widened.
    * **Cross-platform.** Locking goes through
      :func:`platform_compat.file_lock`, which uses ``fcntl.flock`` on POSIX
      and a bounded ``msvcrt.locking`` spin on Windows.
    * **Symlink-safe.** The target path is resolved before locking, so a
      symlinked config is updated in place (matching
      ``write_config_atomically``'s behavior).

    Parameters
    ----------
    path : Path | None
        Config file path; defaults to :func:`config_path`.
    mutate : (dict) -> dict | None
        Called with the current config data (possibly ``{}`` for a new file).
        Must return the updated dict to write, or ``None`` to skip the write
        (useful when the mutate discovers no change is needed).
    fsync : bool
        Passed through to :func:`write_config_atomically`.
    stamp_meta : bool
        If True (default), stamps the ``meta`` block via
        :func:`stamp_config_meta` before writing.
    on_corrupt : "fail" | "reset"
        Behavior when :func:`read_config_for_update` raises
        :class:`ConfigReadError`.  ``"fail"`` (default) re-raises, aborting the
        update.  ``"reset"`` catches the error inside the lock hold and invokes
        *mutate* with ``{}``; the caller's write proceeds in the same critical
        section so no concurrent writer can land between.
    wait_for_lock : bool
        If True (default), block until the advisory lock is free -- correct for
        a caller whose update must happen.  If False, the acquire is single-shot
        and raises :class:`OSError` when another writer holds the lock; for a
        caller whose write is OPTIONAL and retried later, and which may run on
        the event-loop thread, where a POSIX ``flock`` wait would stall the
        gateway for as long as the holder keeps it.  It never relaxes the
        serialization -- a contended acquire declines instead of proceeding.

    Returns
    -------
    dict
        The final config dict (after mutation), whether or not a write occurred.

    Raises
    ------
    ConfigReadError
        If the existing config is unreadable or malformed and
        ``on_corrupt="fail"``.
    ConfigWriteRefused
        If the mutated document carries content the publish floor never
        persists (a plaintext value under ``agent.deepseek_env``). Raised
        before any byte is written; the existing file is untouched.
    OSError
        If the lockfile cannot be opened/created or the lock cannot be acquired
        (including a contended ``wait_for_lock=False`` acquire).
    """
    p = path if path is not None else config_path()
    # Resolve symlinks before locking (same logic as write_config_atomically)
    # so the sidecar sits beside the ACTUAL file, not the symlink.
    try:
        if p.is_symlink():
            p = p.resolve()
    except OSError:
        pass
    with _config_write_lock(p, wait=wait_for_lock):
        try:
            data = read_config_for_update(p)
        except ConfigReadError:
            if on_corrupt == "fail":
                raise
            # on_corrupt="reset": treat as empty inside the same lock hold.
            data = {}
        result = mutate(data)
        if result is None:
            return data
        if stamp_meta:
            result = stamp_config_meta(result)
        write_config_atomically(p, result, fsync=fsync)
        # A same-size replacement can retain an indistinguishable fingerprint
        # on a coarse-timestamp filesystem. Clear eagerly before reporting the
        # write complete so the next load cannot serve the pre-write snapshot,
        # and wake the process watcher so the hot-apply path runs now rather
        # than on its next poll.
        _invalidate_config_cache()
        _notify_live_watch()
        if after_write is not None:
            after_write()
        return result


def _notify_live_watch() -> None:
    """Wake the process config watcher after a write landed on ``config.json``.

    Every writer in this module ends here so the dashboard, ``kirocrew config
    set`` and a boot-time migration all reach the hot-apply path the same way.
    The import is lazy because ``config.live`` imports this module for its
    loads; a no-op in a process that never started a watcher (the CLI).
    """
    from kiro_crew.config import live

    live.notify_config_written()


def stamp_config_meta(data: dict) -> dict:
    """Return *data* with a freshly stamped ``meta`` block in front.

    ``meta.lastTouchedVersion`` names the build that wrote the bytes now on
    disk, which is the first thing to check when a ``config.json`` looks like
    it came from an older schema. An existing stamp is therefore replaced
    rather than merged.

    Every writer that rebuilds the whole file from a dataclass round-trip has
    to stamp through here: ``to_dict()`` models only the schema, so such a
    write drops any top-level key the dataclass does not carry — ``meta``
    among them. Writers that mutate the raw dict they read keep the block
    without help.

    Only ``config.json`` carries the block. ``config.local.json``, agent
    specs, and the other JSON that shares :func:`write_config_atomically` do
    not, so the stamping is deliberately separate from that function.
    """
    return {
        "meta": {
            "lastTouchedVersion": __version__,
            "lastTouchedAt": datetime.now(timezone.utc).isoformat(),
        },
        **{k: v for k, v in data.items() if k != "meta"},
    }


def refresh_config_meta_stamp() -> bool:
    """Re-stamp ``config.json``'s ``meta`` block when it names another build.

    The stamp is only ever written as a side effect of a config write, so an
    upgrade that never touches ``config.json`` leaves ``lastTouchedVersion``
    naming the *previous* build indefinitely. That contradicts the field's
    documented meaning ("the build that wrote the bytes now on disk") and
    sends anyone debugging a version question chasing a build that is not
    installed. Called once per gateway start, off the boot
    path: a version check on one small file, a rewrite only when it differs.

    Deliberately a plain field refresh, not a migration hook: the stamp is
    replaced, every other key is preserved, and nothing else changes. When
    the stored version already matches, the file is not rewritten at all
    (no mtime churn, no ``lastTouchedAt`` bump). Nor is it rewritten while
    the one-shot legacy ``skills.lazy_load`` rewrite is still due
    (``migration.legacy_lazy_load_rewrite_due``): the old stamp is that
    rewrite's proof, and a degraded first load leaves it pending.

    The read-modify-write goes through :func:`update_config_locked` — the
    required path for new ``config.json`` mutations — so the refresh holds
    the sidecar advisory lock and can never revert a concurrent settings
    write with its own earlier snapshot. Callers that run while the
    dashboard serves requests must ALSO hold the in-process asyncio config
    lock (``_get_config_lock``) around the call, because the legacy writers
    serialize on that lock alone.

    Best-effort by design — a stale stamp is a diagnostic blemish, never
    worth failing a boot over. Returns ``True`` when a refresh was written,
    ``False`` when nothing needed doing (absent/empty file, current stamp)
    or the file could not be safely read (an unreadable/torn config must
    never be replaced with a stamped-but-empty one).
    """
    path = config_path()
    if not path.exists():
        return False

    wrote = False

    def _stamp_if_stale(data: dict) -> dict | None:
        nonlocal wrote
        if not data:
            # Absent or emptied between the exists() check and the lock hold:
            # there is nothing to refresh, and writing would CREATE a config
            # holding only a meta block.
            return None
        meta = data.get("meta")
        stored = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
        if stored == __version__:
            return None  # current: skip the write entirely
        if (
            _migration.legacy_lazy_load_rewrite_due(
                data, connections_marker=config_dir() / CONNECTIONS_UI_MIGRATION_MARKER
            )
            is not None
        ):
            # The old stamp is the proof the pending one-shot rewrite reads, and a
            # load that could not write (a degraded section) has not used it yet.
            # It is still the build that wrote these bytes, so keeping it is true.
            return None
        wrote = True
        return data  # update_config_locked stamps the meta block itself

    try:
        update_config_locked(path, mutate=_stamp_if_stale)
    except ConfigReadError:
        logger.debug(
            "config meta stamp refresh skipped: %s unreadable; leaving it untouched",
            path,
            exc_info=True,
        )
        return False
    except OSError:
        logger.debug(
            "config meta stamp refresh failed: could not lock or write %s",
            path,
            exc_info=True,
        )
        return False
    if wrote:
        _invalidate_config_cache()
        _notify_live_watch()
    return wrote


def _config_fingerprint() -> tuple:
    """Cheap signature of the config files — changes whenever either is edited.

    Includes replacement identity (device + inode) and change time in addition
    to mtime, size, and mode. Dashboard/CLI writers publish with atomic rename;
    on a coarse-timestamp filesystem an Incognito→Temporary replacement can
    otherwise keep the same mtime and size, making another process's cache look
    current. A missing file contributes a sentinel so create/delete also busts.
    """
    sig: list = []
    for p in (config_path(), config_local_path()):
        try:
            st = p.stat()
            sig.append(
                (
                    str(p),
                    st.st_dev,
                    st.st_ino,
                    st.st_ctime_ns,
                    st.st_mtime_ns,
                    st.st_size,
                    st.st_mode,
                )
            )
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _content_digest_of(parts: "list[bytes | None]") -> str:
    """Digest *parts* -- one entry per config file, ``None`` for a file that is absent.

    Shared by :func:`config_content_stamp`, which reads the files, and by the load
    path, which already holds what it parsed. One framing so the two answers are
    comparable: each part is length-prefixed, so moving bytes from one file to the
    other cannot produce the same digest, and an absent file gets its own token so
    creating or deleting the overlay changes the answer.

    Both sides hand over the file's DECODED text re-encoded, not the raw bytes, so
    that they hash the same thing: the loader reads through ``read_text``, whose
    newline translation the raw bytes would not survive, and a digest that disagreed
    with itself across the two callers would refuse every comparison forever. What
    this identifies is therefore the document, which is what the comparison is about.
    """
    hasher = _hashlib.sha256()
    for blob in parts:
        if blob is None:
            hasher.update(b"absent\x00")
            continue
        hasher.update(b"present\x00")
        hasher.update(str(len(blob)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(blob)
    return hasher.hexdigest()


def config_content_stamp() -> str | None:
    """A digest of the config files' BYTES, or ``None`` when one cannot be read.

    :func:`_config_fingerprint` answers a cheaper and different question -- has
    either file been REPLACED -- from stat metadata. For a rename that lands the
    same byte count, the device, both timestamps, the size and the mode can all be
    identical, leaving the inode as the only field that differs; where inodes are
    not stable across a replacement, two different contents share a fingerprint. A
    caller asking "is the live config still the bytes I derived this answer from"
    is asking about content, so this reads the content.

    ``None`` does NOT mean unchanged: it means the question could not be answered,
    and a caller comparing stamps must treat it as a mismatch rather than a match.
    The two paths are framed with their lengths so that moving bytes from one file
    to the other cannot produce the same digest.
    """
    parts: list[bytes | None] = []
    for p in (config_path(), config_local_path()):
        try:
            parts.append(read_config_text(p).encode("utf-8"))
        except FileNotFoundError:
            # A file that does not exist is a real state, not a failure.
            parts.append(None)
        except (OSError, UnicodeDecodeError):
            return None
    return _content_digest_of(parts)


def _bind_content_digest(cfg: object, digest: str | None) -> None:
    """Record on a loaded config which bytes it was parsed from.

    The digest rides on the instance rather than in the dataclass, because it does not
    describe the user's configuration: a field here would enter the constructor, the
    generated settings surface and the field walk :func:`_adopt_in_memory` performs, all
    of which speak for the document. Attaching it outside the field set keeps those
    surfaces unchanged, at the cost that an object produced any other way -- a bare
    ``KiroCrewConfig()``, a test double standing in for a load -- carries no such
    attribute. :func:`load_config_with_content_stamp` therefore asks with a default, and
    reads that absence as "provenance unknown".
    """
    setattr(cfg, "_content_digest", digest)


def _cached_validated_data(fp: tuple | None = None) -> dict | None:
    """Return a deep copy of the cached validated config dict, or None on miss.

    Thin wrapper over the :class:`~kiro_crew.config.validation.ConfigCache`.
    ``_config_fingerprint`` stays in this module because it reads
    ``config_path()``/``config_local_path()``, which the test suite patches as
    ``kiro_crew.config.loader.config_path``.

    Pass *fp* when the caller has already computed the fingerprint, so one load
    costs a single stat pass instead of one per consumer of it. Omitting it
    stats, which suits a caller that has no fingerprint in hand.
    """
    return _CONFIG_CACHE.get(fp if fp is not None else _config_fingerprint())


def _store_validated_data(
    data: dict,
    fp: tuple,
    sidecar: dict | None = None,
    *,
    expected_generation: int | None = None,
    content_digest: str | None = None,
) -> None:
    """Cache validated data unless a write invalidated its disk-read generation.

    *content_digest* names the bytes *data* was parsed from, so a later hit on this
    entry can say which content it represents rather than only which stat signature
    it was filed under.
    """
    _CONFIG_CACHE.store(
        data,
        fp,
        sidecar,
        expected_generation=expected_generation,
        content_digest=content_digest,
    )


#: Sidecar key under which the loader caches the pre-overlay base values of the
#: top-level sections config.local.json touches. See ``_shadowed_base_sections``.
_SIDECAR_BASE_SHADOW = "base_shadow"

#: Whether the load that filled this cache entry found ``config.json`` present but
#: unparseable. A cache hit carries it forward so :meth:`KiroCrewConfig.save` can
#: refuse to publish a snapshot built without the base file.
_SIDECAR_BASE_UNREADABLE = "base_unreadable"

#: Whether the load that filled this cache entry found ``config.local.json`` present
#: but unparseable, carried forward the same way for ``_overlay_unreadable``.
_SIDECAR_OVERLAY_UNREADABLE = "overlay_unreadable"


def _shadowed_base_sections(base: dict, overlay: dict) -> dict:
    """The base document's copy of every top-level section the overlay touches.

    ``_deep_merge`` lets an overlay leaf replace a base leaf, and after the merge
    nothing remembers what the base held. That is fine for modelled keys — they
    are parsed into fields and ``save()`` re-emits the field, while
    ``_subtract_overlay`` keeps the overlay's value out of the base file. It is
    NOT fine for the unknown keys ``_extra_sections`` / ``_extra_keys`` round-trip
    verbatim: captured from the MERGED document they hold the overlay's value, so
    ``save()`` emits it, the subtraction removes it (it equals the raw overlay
    value), and the base file loses a key it had. Removing the overlay later then
    reveals nothing — the base value is gone for good.

    Capturing from the base instead makes the round-trip honest: ``save()`` emits
    the base value, it differs from the overlay leaf so subtraction leaves it, and
    the overlay still wins on the next load exactly as before.

    Only sections the overlay names are kept (an overlay normally touches a few),
    and only their base copy — the loader already has the merged copy. This rides
    in the cache sidecar so a cache hit, where the overlay is not in scope,
    can still capture correctly.
    """
    return {k: copy.deepcopy(base[k]) for k in overlay if k in base}


def _invalidate_config_cache() -> None:
    """Drop the cached validated config (called after save()/write-back)."""
    _CONFIG_CACHE.clear()

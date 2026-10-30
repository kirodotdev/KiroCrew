"""Live config: one poll, one reload, one dispatch.

The gateway holds many long-lived objects that copy a config value at
construction (the session manager's idle timeout, the subagent manager's
concurrency cap, a channel transport's allow-list). A write to ``config.json``
reaches those copies only if something pushes the new value at them, and only
the writer that happens to know about a copy can push -- which left every
``kirocrew config set`` and every hand edit silently inert for those fields.

This module is the single mechanism that closes the gap:

* **One poll.** :class:`ConfigWatch` runs a single background task that
  compares the loader's file fingerprints (a few ``stat`` calls, off the event
  loop) on an interval. It watches ``config.json`` AND the credential ``.env``
  file: the two are separate files with separate writers, so each has its own
  fingerprint, but one cycle reads both. Nothing else in the gateway polls
  either.
* **One reload.** When the config fingerprint moves -- or an in-process writer
  calls :func:`notify_config_written` -- the watcher performs ONE
  :meth:`KiroCrewConfig.load` off the loop. That load is the same call the rest
  of the tree makes, so the ``publish_*`` snapshots the loader maintains
  (compaction threshold, timezone, alias table, MCP path dirs) ride along for
  free rather than needing a second reader. When the ``.env`` fingerprint moves,
  the watcher re-reads the raw credential map (also off the loop) and diffs it
  against the last one: the changed keys are pushed into ``os.environ`` (set/pop,
  overriding an inherited value because an operator editing ``.env`` means the
  edit) so a hand edit or a non-dashboard writer reaches a long-running gateway
  without a restart.
* **One dispatch.** The old and new documents are flattened to dotted leaf paths
  and diffed; every subscriber whose prefixes intersect the changed set is
  called, sequentially, on the loop, each guarded so one failing applier cannot
  starve the rest. A ``.env`` credential change joins the SAME dispatch as a
  synthetic ``credentials.<KEY>`` path (a namespace no config section owns),
  which the channel-restart applier maps onto the channels whose credentials
  moved -- so a rotated token restarts its channel exactly as a changed
  ``config.json`` connection parameter does. Subscribers receive a
  :class:`ConfigChange` carrying both configs and the exact changed paths, so an
  applier can be as narrow as one field or as wide as a section.

Design rules this module keeps (see ``docs/system-specs/modules/config.md``):

* No filesystem I/O on the event loop: the fingerprints, the config load and the
  ``.env`` credential read all run in ``asyncio.to_thread``; the credential diff
  and the ``os.environ`` apply are pure CPU on the loop. :meth:`snapshot` is a
  plain attribute read.
* Loads are serialized by the watcher, so the diff is always old-vs-newer and
  an applier never sees an out-of-order pair. Other ``load()`` callers are
  unaffected; the loader's own ticket ordering still governs its snapshots.
* Values are never logged, only changed PATHS -- ``to_dict()`` carries channel
  tokens and the diff sees them.
* A subscriber bound to an object holds it weakly, so a manager that is
  discarded (tests, provider reloads) falls out of the registry on its own.
* Failure is loud, never silent: a load that raises is logged at WARNING and the
  previous snapshot is kept; the next tick retries.

Anything a gateway does in response to a config write -- from the dashboard,
the CLI or ``$EDITOR`` -- belongs behind :func:`subscribe`, not in a request
handler. A handler that mutates ``config.json`` should end with
:func:`notify_config_written` (the loader's writers do this themselves) and
let the watcher apply, so all three writers behave identically.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import threading
import weakref
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

if TYPE_CHECKING:  # pragma: no cover - import cycle with the loader at runtime
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: Default seconds between fingerprint checks. Two ``stat`` calls per tick on a
#: worker thread; low enough that a CLI write lands before the operator has
#: switched windows, high enough to be invisible in CPU profiles.
DEFAULT_POLL_INTERVAL_SECS = 2.0

#: Floor for the interval so a misconfigured test or embedder cannot spin the
#: worker pool on stats.
MIN_POLL_INTERVAL_SECS = 0.05

#: Ceiling on one applier's own call, on top of the poll interval. Appliers run
#: sequentially under the cycle lock (see ``ConfigWatch._dispatch``), so a
#: synchronous or hung applier with no bound would stall every applier after it
#: and any concurrent ``refresh_now()`` caller indefinitely -- a raise is caught
#: and logged, but a call that never returns is not. This is a backstop, not a
#: per-applier tuning knob: a well-behaved applier never approaches it.
APPLIER_TIMEOUT_SECS = 10.0

#: Synthetic dotted-path prefix for a ``.env`` credential change. The watcher
#: does not fingerprint credentials into the config document (``.env`` is a
#: separate file with its own writer, and the document fingerprint is the parsed
#: config cache key), so a credential change cannot surface as a real config
#: leaf. It is dispatched as ``credentials.<KEY>`` instead -- a path no config
#: section owns -- which the channel-restart applier maps onto the channels
#: whose ``credentials`` tuple names that key. ``ConfigChange.touched`` matching
#: is prefix-based, so an applier can watch the whole namespace with
#: ``"credentials"`` or one key with ``"credentials.SLACK_BOT_TOKEN"``.
CREDENTIAL_CHANGE_PREFIX = "credentials"

Applier = Callable[["ConfigChange"], Awaitable[None] | None]


def flatten_config(doc: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested config document to ``{dotted.leaf.path: value}``.

    Non-empty dicts recurse; everything else (scalars, lists, empty dicts) is a
    leaf. Lists are leaves because every list-typed field in the schema is a
    whole value (an allow-list, a set of roots) whose consumers rebuild from the
    full list, so a per-index diff would only fragment one change into many.
    """
    out: dict[str, Any] = {}
    for key, value in doc.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping) and value:
            out.update(flatten_config(value, path))
        else:
            out[path] = value
    return out


def diff_config_docs(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> frozenset[str]:
    """Return the dotted leaf paths whose value differs between *old* and *new*.

    A path present on only one side counts as changed. ``old=None`` (no prior
    document) reports every leaf of *new* -- the shape a first-time subscriber
    wants when it asks to be brought up to date.
    """
    new_flat = flatten_config(new)
    if old is None:
        return frozenset(new_flat)
    old_flat = flatten_config(old)
    changed = {
        path
        for path in set(old_flat) | set(new_flat)
        if old_flat.get(path, _MISSING) != new_flat.get(path, _MISSING)
    }
    return frozenset(changed)


_MISSING = object()


def _refuse_restart_marked(prefixes: "Iterable[str]") -> None:
    """Refuse a registration at or under a ``restart=True`` schema path.

    ``restart=True`` is the admission that no applier exists for a field. An
    applier registered there would make the field hot while the UI still
    promises a restart, or hot for one consumer and boot-only for the others
    (``agent.approval_mode``: every channel dispatcher resolves it once at
    start). A section-wide registration (``"whatsapp"``) stays allowed -- its
    applier adopts the section's live fields and ignores the marked leaf. The
    check runs at construction, so the owner's own tests trip it.
    """
    from kiro_crew.config.schema import requires_restart

    for pre in prefixes:
        if requires_restart(pre):
            raise ValueError(
                f"config applier registered at/under restart-marked path {pre!r}: "
                "mark the field live (drop restart=True) or read it at boot only"
            )


def _path_matches(path: str, prefix: str) -> bool:
    """Whether dotted *path* is *prefix* itself or lies under it."""
    return path == prefix or path.startswith(prefix + ".")


@dataclass(frozen=True)
class ConfigChange:
    """What one reload observed.

    A real reload always supplies ``old``: the watcher diffs the prior document
    against the new one. ``old`` is ``None`` only when a caller synthesizes a
    change by hand (tests do) and has no prior document to offer.
    """

    old: "KiroCrewConfig | None"
    new: "KiroCrewConfig"
    changed: frozenset[str]

    def touched(self, *prefixes: str) -> bool:
        """Whether any changed path is one of *prefixes* or lies under one."""
        return any(_path_matches(p, pre) for p in self.changed for pre in prefixes)

    def under(self, prefix: str) -> frozenset[str]:
        """The changed paths that are *prefix* or lie under it."""
        return frozenset(p for p in self.changed if _path_matches(p, prefix))


@dataclass
class Subscription:
    """A registered applier. ``cancel()`` removes it; a dead weak target removes itself."""

    name: str
    prefixes: tuple[str, ...]
    _ref: Any = field(repr=False)
    _watch: "ConfigWatch | None" = field(default=None, repr=False)

    def callback(self) -> Applier | None:
        """Resolve the applier, or ``None`` if its bound object was collected."""
        ref = self._ref
        if isinstance(ref, (weakref.WeakMethod, weakref.ref)):
            return ref()
        if isinstance(ref, _OwnedApplier):
            return ref if ref.alive() else None
        return ref

    def cancel(self) -> None:
        if self._watch is not None:
            self._watch._remove(self)
            self._watch = None


def _hold(callback: Applier) -> Any:
    """Hold a bound method weakly so a subscriber object can be collected."""
    if inspect.ismethod(callback):
        return weakref.WeakMethod(callback)
    return callback


class _OwnedApplier:
    """An applier that acts on behalf of an *owner* object it holds weakly.

    The three shapes built on this -- :func:`watch_section`, :func:`watch_object`
    and :func:`bind` -- are what turn "make a new setting hot" into one line in
    the owning object's constructor. Each resolves the owner at dispatch time,
    so a discarded owner drops out of the registry exactly like a weakly held
    bound method does, and the subscription never keeps it alive.
    """

    __slots__ = ("_owner", "_apply")

    def __init__(self, owner: object, apply: Callable[[object, ConfigChange], Any]) -> None:
        self._owner = weakref.ref(owner)
        self._apply = apply

    def alive(self) -> bool:
        return self._owner() is not None

    def __call__(self, change: ConfigChange) -> Any:
        owner = self._owner()
        if owner is None:
            return None
        return self._apply(owner, change)


class ConfigDeferred(Exception):
    """An applier declined to adopt a change for now and asks to be retried.

    Two appliers raise it: a fail-closed one refusing a DEGRADED document, and
    the channel-restart applier before its transports have started. ``paths``
    are the changed paths it did not apply (empty means every changed path under
    the subscription's prefixes). The watcher records them as stale and re-runs
    the applier on later ticks against the CURRENT snapshot -- including a tick
    whose reload produced no diff -- so a document repaired back to the values
    the degraded snapshot already held is still applied. Without this a skip was
    a silent success: the degraded defaults became the snapshot, the repair
    diffed empty, and the previous authorization state stayed live for good.
    """

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = frozenset(paths)
        super().__init__(f"{len(self.paths)} path(s) deferred; retried on later ticks")


def _section_applier(
    section: str, target: str | None, *, fail_closed: bool, method: str
) -> Callable[[object, ConfigChange], Any]:
    def apply(owner: object, change: ConfigChange) -> Any:
        holder: Any = owner if target is None else getattr(owner, target, None)
        if holder is None:
            # The reconfigurable is not up yet (a transport before connect): the
            # section it boots from is read fresh at that point, so nothing is lost.
            return None
        if fail_closed and section in change.new.degraded_sections:
            # Authorization state must never be rebuilt from a section the loader
            # discarded: the previous values stay in force, and the skipped paths
            # are retried once the document validates. A document that is torn as
            # a whole never reaches an applier -- the watcher keeps the previous
            # snapshot while the file does not parse -- so the whole-config flag,
            # which the loader keeps for the life of the process, is not consulted
            # here: on a dispatched change it can only be the memory of a tear
            # that has since been repaired.
            raise ConfigDeferred(change.under(section))
        return getattr(holder, method)(getattr(change.new, section))

    return apply


def _object_applier(
    method: str, prefixes: tuple[str, ...]
) -> Callable[[object, ConfigChange], Any]:
    sections = frozenset(p.split(".", 1)[0] for p in prefixes)

    def apply(owner: Any, change: ConfigChange) -> Any:
        if sections & change.new.degraded_sections:
            # Same rule as the section applier: a section the loader discarded
            # holds DEFAULTS, and handing those to ``reconfigure`` would reset an
            # approval mode or a limit to its default. The owner keeps what it
            # last adopted; the skipped paths are retried once the document
            # validates.
            raise ConfigDeferred(p for pre in prefixes for p in change.under(pre))
        return getattr(owner, method)(change.new)

    return apply


def _value_at(cfg: Any, path: str) -> Any:
    for part in path.split("."):
        cfg = getattr(cfg, part)
    return cfg


def _keep_last_valid(
    prev: "KiroCrewConfig | None", cfg: "KiroCrewConfig", *, torn_now: bool
) -> "KiroCrewConfig":
    """Keep *prev*'s values while the document on disk cannot be read.

    An unparseable ``config.json`` loads as DEFAULTS, and the snapshot is read
    directly -- ``bind``, ``current()``, every per-turn point-of-use read -- so
    adopting that load would widen a gate for the length of the tear (a forum
    activation of ``always``, an empty allow-list). While the file does not
    parse, the previous snapshot stays the snapshot; only the degradation flag
    is taken from the new load, because the consumers that fail closed on it
    (the tailnet gate, the session-control bypass) must still see it.

    *torn_now* is decided from the file itself, not from ``degraded_sections``:
    the loader remembers a degradation for the life of the process by design, so
    a repaired document is still flagged, and keying on the flag would freeze
    every later reload until a restart. With no previous snapshot the load is
    adopted as it is: the process booted on those defaults, and the snapshot
    reports what is in force.
    """
    if prev is None or not torn_now:
        return cfg
    return replace(prev, _degraded_sections=cfg.degraded_sections)


class ConfigWatch:
    """The process's config poller and hot-apply dispatcher.

    Construct once per gateway (``watch()`` hands out the process singleton),
    :meth:`start` it after the event loop is running, and :meth:`stop` it on
    shutdown. Subscribers may register before ``start`` -- a boot-time
    constructor is the natural place -- and are dispatched only after it.
    """

    def __init__(self, *, poll_interval_secs: float = DEFAULT_POLL_INTERVAL_SECS) -> None:
        self._interval = max(MIN_POLL_INTERVAL_SECS, float(poll_interval_secs))
        self._subs: list[Subscription] = []
        self._subs_lock = threading.Lock()
        self._cfg: KiroCrewConfig | None = None
        self._doc: dict[str, Any] | None = None
        self._fingerprint: tuple | None = None
        # The ``.env`` file is tracked separately from ``config.json``: its own
        # fingerprint (``loader.env_fingerprint``) and the last raw credential
        # map read from it. ``config.json`` and ``.env`` are two different files
        # with two different writers, and the document fingerprint is also the
        # parsed-config cache key, so folding ``.env`` into it would bust that
        # cache on every credential edit. ``_creds`` is ``None`` until primed, so
        # the first cycle diffs against the real boot credentials rather than
        # replaying every key as a change.
        self._env_fingerprint: tuple | None = None
        self._creds: dict[str, str] | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._hold_depth = 0
        self._hold_gen = 0
        self._force = False
        # Paths an applier failed to adopt, keyed by subscription: the watcher
        # re-dispatches them on the next tick instead of leaving that consumer
        # stale until its fields happen to change again.
        self._stale: dict[int, tuple[Subscription, frozenset[str]]] = {}
        self._cycle_lock: asyncio.Lock | None = None
        self.last_error: str | None = None
        # The ``.env`` fingerprint a credential apply last FAILED on. A failed
        # apply keeps the fingerprint unadvanced so the next tick retries, which
        # means the same bad file re-fails every ~2s; this dedups the WARNING to
        # once per distinct failing file state (like ``_warned_undecodable_env``
        # for the undecodable case) and logs the retries at DEBUG, so a malformed
        # ``.env`` line does not spew a full traceback into the log every tick.
        self._apply_failed_fingerprint: tuple | None = None

    # ── registry ──────────────────────────────────────────────────────

    def subscribe(
        self,
        *prefixes: str,
        callback: Applier,
        name: str | None = None,
    ) -> Subscription:
        """Register *callback* for changes at or under any of *prefixes*.

        A prefix is a dotted config path (``"agent.max_subagents"``,
        ``"session"``, ``"agents"``). With no prefixes the applier fires on every
        reload. *callback* may be sync or async; it receives a
        :class:`ConfigChange`. A bound method is held weakly. Order of dispatch is
        order of registration.
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        _refuse_restart_marked(prefixes)
        sub = Subscription(
            name=name or str(getattr(callback, "__qualname__", repr(callback))),
            prefixes=tuple(prefixes),
            _ref=_hold(callback),
            _watch=self,
        )
        with self._subs_lock:
            self._subs.append(sub)
        return sub

    def _subscribe_owned(
        self, owner: object, prefixes: tuple[str, ...], apply: Any, name: str
    ) -> Subscription:
        _refuse_restart_marked(prefixes)
        sub = Subscription(
            name=name, prefixes=prefixes, _ref=_OwnedApplier(owner, apply), _watch=self
        )
        with self._subs_lock:
            self._subs.append(sub)
        return sub

    def watch_section(
        self,
        owner: object,
        section: str,
        *extra_prefixes: str,
        target: str | None = None,
        fail_closed: bool = True,
        method: str = "reconfigure",
        name: str | None = None,
    ) -> Subscription:
        """Hot-apply one top-level config *section* through ``reconfigure(section_cfg)``.

        The one-line form for a subsystem that owns a section: fires when
        anything under *section* (or an *extra_prefixes* entry) changes and calls
        ``holder.reconfigure(getattr(change.new, section))``, where the holder is
        *owner* itself or, with *target*, the object in ``owner.<target>`` (a
        dispatcher's transport). A ``None`` holder is a no-op, so registering in a
        constructor before the transport connects is safe. With *fail_closed*
        (the default, and mandatory for anything carrying authorization) a
        reload whose *section* the loader marked degraded is skipped and logged,
        so an allow-list is never rebuilt from an unparseable document. *owner*
        is held weakly. *method* names the receiving method when ``reconfigure``
        is taken by something else on that object.
        """
        return self._subscribe_owned(
            owner,
            (section, *extra_prefixes),
            _section_applier(section, target, fail_closed=fail_closed, method=method),
            name or f"{type(owner).__name__}.{target or method}[{section}]",
        )

    def watch_object(
        self,
        owner: object,
        *prefixes: str,
        method: str = "reconfigure",
        name: str | None = None,
    ) -> Subscription:
        """Hot-apply the whole config through ``owner.reconfigure(cfg)``.

        For an object whose settings span sections (or that normalizes several
        fields together): fires when anything under any of *prefixes* changes and
        hands it the reloaded :class:`KiroCrewConfig`. *owner* is held weakly.
        *method* names the receiving method when ``reconfigure`` is taken. An owner
        that applied a config it loaded itself calls :meth:`replay` on the returned
        subscription to be brought up to the adopted config at once.
        """
        return self._subscribe_owned(
            owner,
            tuple(prefixes),
            _object_applier(method, prefixes),
            name or f"{type(owner).__name__}.{method}",
        )

    def replay(self, sub: Subscription) -> None:
        """Bring *sub*, which has just registered, up to the adopted config.

        A reload adopts its config and only then snapshots the registry to
        dispatch, so a reload that snapshotted before *sub* was appended never
        reaches it. The owner keeps the config it loaded itself, which may be
        older than the one adopted in between, and nothing corrects it until the
        same paths change again. A ``memory.episodic_max_count`` raise lost that
        way leaves a store evicting to the old cap.

        Every reload that can have missed *sub* adopted its config BEFORE it read
        the registry, so the snapshot read here, after registration, is at least as
        new as any of them, and every later reload includes *sub*. The snapshot is
        re-read after each apply so a reload landing mid-replay is not undone by
        the older one. When the file has moved past the snapshot (or the watcher
        has not fingerprinted it yet), the snapshot may be OLDER than what the
        owner loaded, so it is not applied; *sub* is marked stale for its prefixes
        instead, and the next tick delivers the new document to it even where that
        document leaves them unchanged. A no-op on a watcher that has adopted
        nothing (an unstarted process: the CLI, most tests).
        """
        applied: KiroCrewConfig | None = None
        while True:
            cfg = self._cfg
            if cfg is None or cfg is applied:
                return
            paths = frozenset(sub.prefixes)
            try:
                current = self._fingerprint is not None and (
                    self._fingerprint == self._current_fingerprint()
                )
            except Exception:  # noqa: BLE001 - an unreadable file: let the tick decide
                current = False
            if not current:
                self._mark_stale(sub, paths)
                return
            cb = sub.callback()
            if cb is None:
                return
            try:
                result = cb(ConfigChange(old=cfg, new=cfg, changed=paths))
            except ConfigDeferred as deferred:
                self._mark_stale(sub, deferred.paths or paths)
                return
            except Exception:  # noqa: BLE001 - same containment as a dispatch
                logger.error(
                    "config applier %r failed on replay; retrying on the next tick",
                    sub.name,
                    exc_info=True,
                )
                self._mark_stale(sub, paths)
                return
            if inspect.isawaitable(result):
                # Nothing here can await it; the loop's retry delivers it instead.
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                self._mark_stale(sub, paths)
                return
            applied = cfg

    def _mark_stale(self, sub: Subscription, paths: frozenset[str]) -> None:
        """Record *paths* as not yet adopted by *sub*, for the next tick to deliver."""
        prior = self._stale.get(id(sub))
        self._stale[id(sub)] = (sub, paths | (prior[1] if prior else frozenset()))

    def bind(
        self, path: str, setter: Callable[[Any], Any], *, name: str | None = None
    ) -> Subscription:
        """Map one dotted leaf *path* onto *setter*: ``setter(new value)`` on change.

        The one-line form for a single scalar with an existing setter
        (``bind("agent.max_channels", mgr.set_max_channels)``). A bound-method
        setter is held weakly, so a discarded object drops out. Fails closed
        like :meth:`watch_object`: while the leaf's section is degraded the
        document holds its DEFAULT, and handing that to a setter would reset a
        cap or a ceiling; the change is deferred until the document validates.
        """
        held = _hold(setter)
        section = path.split(".", 1)[0]

        def apply(change: ConfigChange) -> Any:
            fn = held() if isinstance(held, weakref.WeakMethod) else held
            if fn is None:
                return None
            if section in change.new.degraded_sections:
                raise ConfigDeferred((path,))
            return fn(_value_at(change.new, path))

        if isinstance(held, weakref.WeakMethod):
            # Route liveness through the setter's owner so the subscription dies with it.
            owner = setter.__self__  # type: ignore[attr-defined]
            return self._subscribe_owned(
                owner, (path,), lambda _o, ch: apply(ch), name or f"bind[{path}]"
            )
        return self.subscribe(path, callback=apply, name=name or f"bind[{path}]")

    def _remove(self, sub: Subscription) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def _live_subscriptions(self) -> list[Subscription]:
        """Snapshot the registry, dropping entries whose target was collected."""
        with self._subs_lock:
            alive = [s for s in self._subs if s.callback() is not None]
            if len(alive) != len(self._subs):
                self._subs = alive
            return list(alive)

    def subscriptions(self) -> Iterator[Subscription]:
        return iter(self._live_subscriptions())

    # ── state ─────────────────────────────────────────────────────────

    def snapshot(self) -> "KiroCrewConfig | None":
        """The config as of the last applied reload -- a plain attribute read.

        ``None`` before :meth:`start` (or :meth:`prime`) has run. Callers on the
        event loop should prefer this over ``KiroCrewConfig.load()`` when they
        only need the value the rest of the gateway has already adopted.
        """
        return self._cfg

    @property
    def started(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def poll_interval_secs(self) -> float:
        return self._interval

    def prime(self, cfg: "KiroCrewConfig", fingerprint: tuple | None = None) -> None:
        """Adopt *cfg* as the current snapshot without dispatching.

        Boot calls this with the config it already loaded, so the first tick
        after :meth:`start` diffs against what the gateway actually booted with
        rather than replaying every leaf. Safe from any thread; nothing here
        touches the filesystem.
        """
        self._cfg = cfg
        self._doc = cfg.to_dict()
        if fingerprint is not None:
            self._fingerprint = fingerprint

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self, initial: "KiroCrewConfig | None" = None) -> None:
        """Arm the poll task on the running loop.

        *initial* is the config the caller booted with. It is primed as the
        baseline but deliberately WITHOUT the file's current fingerprint: the
        gateway loaded it some time before this call, and an edit made in that
        window (a CLI ``config set`` while the gateway was booting) would
        otherwise be paired with a fingerprint that already reflects it and
        never be applied. Leaving the fingerprint unset makes the first cycle
        reload the file and diff it against the boot config, so that edit is
        dispatched like any other. Without *initial* the watcher loads once
        (off-loop) and that load IS the baseline, fingerprint included.
        Idempotent: a second call while running is a no-op.
        """
        if self.started:
            return
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._wake = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        if initial is not None:
            self.prime(initial)
            self._fingerprint = None
            # The boot credentials are the diff baseline. Take them from what
            # boot's own ``load_credentials()`` actually read (recorded raw,
            # before the environment-wins override), NOT a fresh disk read here:
            # the gateway loads credentials well before this post-bind start, and
            # a ``.env`` rotation in that window would otherwise be read off disk
            # NOW and adopted as the baseline -- so the first cycle re-reads the
            # same rotated value, sees no diff, and never pushes it into
            # ``os.environ``, leaving the channel on the revoked credential until
            # the next gateway restart. Seeding with the boot map makes that
            # window edit a real diff the first cycle applies. The env
            # fingerprint is left unset for the same reason the config one is
            # above. If no boot map was recorded (a test driving the watcher
            # directly), fall back to the off-loop read; a read failure there
            # leaves the baseline empty and the first cycle sets it.
            if self._creds is None:
                from kiro_crew.config import loader
                from kiro_crew.config.loader_credentials import take_boot_raw_env

                # Adopt the boot map only if it was read from the ``.env`` this
                # watcher is about to watch (resolved off-loop: ``env_path`` can
                # touch the filesystem). A record from another data home is not
                # this watcher's baseline.
                try:
                    watched = await asyncio.to_thread(loader.env_path)
                except Exception:  # noqa: BLE001 - fall back to an unchecked take
                    watched = None
                booted = take_boot_raw_env(path=watched)
                if booted is not None:
                    self._creds = booted
                else:
                    await self._prime_creds_baseline()
            self._env_fingerprint = None
        elif self._cfg is None:
            fingerprint = await asyncio.to_thread(self._current_fingerprint)
            cfg = await asyncio.to_thread(self._load)
            self.prime(cfg, fingerprint)
            await self._prime_creds_baseline()
            try:
                self._env_fingerprint = await asyncio.to_thread(self._current_env_fingerprint)
            except Exception:  # noqa: BLE001 - the first cycle re-reads it
                self._env_fingerprint = None
        self._task = loop.create_task(self._run(), name="config-watch")
        self._task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def notify_written(self) -> None:
        """Wake the poll immediately after an in-process config write.

        Safe from any thread (writers run in ``asyncio.to_thread``). Forces a
        reload on the next cycle even if the fingerprint reads equal, because
        the fingerprint is mtime-based and a coarse filesystem clock can make a
        write invisible to it. A no-op before :meth:`start`.
        """
        self._force = True
        loop, wake = self._loop, self._wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            # Loop shut down between the check and the call: nothing to wake.
            pass

    @contextlib.contextmanager
    def hold(self) -> Iterator[None]:
        """Defer reload-and-dispatch while a multi-file transaction is in flight.

        A channel save writes ``config.json`` and then ``.env``, and rolls the
        config back when the credential write fails. Without a hold, the poll
        (or the write hook's wake) could apply the intermediate config -- a
        widened allow-list paired with the old credentials -- for the length of
        the failing write, and only then apply the rollback. Under a hold the
        cycle records that a reload is owed and returns without loading; the
        exit releases it with a forced wake, so the state that is dispatched is
        the one the transaction COMMITTED (or restored). Holds nest. Sync on
        purpose: the body awaits, the guard itself never does.
        """
        self._hold_depth += 1
        self._hold_gen += 1
        try:
            yield
        finally:
            self._hold_depth -= 1
            if self._hold_depth == 0:
                self.notify_written()

    async def refresh_now(self) -> ConfigChange | None:
        """Run one reload-and-dispatch cycle and return what it applied.

        For request handlers that must answer only after the new value is in
        force, and for tests. Forces the load regardless of fingerprint.
        Returns ``None`` when nothing changed or the load failed (the failure is
        logged and kept in :attr:`last_error`).
        """
        self._force = True
        return await self._cycle()

    # ── internals ─────────────────────────────────────────────────────

    def _on_task_done(self, task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("config watch task exited unexpectedly", exc_info=exc)

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # _cycle contains its own failures; this is the last line of
                # defence so the poll never dies on an unexpected error.
                logger.exception("config watch cycle failed")

    async def _cycle(self) -> ConfigChange | None:
        if self._cycle_lock is None:
            # Not started: a direct refresh_now() from a test or a CLI path.
            self._cycle_lock = asyncio.Lock()
        async with self._cycle_lock:
            if self._hold_depth > 0:
                # A transaction is mid-flight; the release re-arms the wake.
                self._force = True
                return None
            force, self._force = self._force, False
            hold_gen = self._hold_gen
            fingerprint = await asyncio.to_thread(self._current_fingerprint)
            env_fingerprint = await asyncio.to_thread(self._current_env_fingerprint)
            config_changed = force or fingerprint != self._fingerprint
            env_changed = force or env_fingerprint != self._env_fingerprint
            if not config_changed and not env_changed:
                if self._stale and self._cfg is not None:
                    await self._retry_stale(self._cfg)
                return None
            # Read the raw ``.env`` map off the loop whenever ``.env`` moved, so
            # the diff and the ``os.environ`` apply below are pure CPU work. The
            # second element says whether the read was definitive.
            env_read = await asyncio.to_thread(self._read_env_credentials) if env_changed else None
            # Expand/validate any PATH line OFF the loop too: a ``~otheruser``
            # entry makes expansion call pwd.getpwnam / the SAM database, which
            # an LDAP/SSSD/AD lookup can stall. Done here so only the os.environ
            # mutation in the applier runs on the loop. A non-definitive read is
            # not applied, so skip the prep for it.
            env_path_additions = (
                await asyncio.to_thread(self._prepare_path_additions, env_read[0])
                if env_read is not None and env_read[1]
                else None
            )
            if self._hold_depth > 0 or self._hold_gen != hold_gen:
                # A transaction began during one of the off-loop reads above (a
                # dashboard channel save writes ``config.json`` then ``.env``, both
                # under ``live.hold()``). Applying now would pair the just-written
                # credential with the pre-save authorization roster for the length
                # of the hold. Adopt and dispatch nothing -- not even the credential
                # diff, which mutates ``os.environ`` and advances the ``.env``
                # fingerprint as a side effect -- and force a fresh cycle; the hold
                # release re-runs this against whatever the transaction committed.
                # The config-load path below keeps the same guard for its own read.
                self._force = True
                return None
            if not config_changed:
                # Only ``.env`` moved: ``config.json`` is unchanged, so the
                # document snapshot stays as it is and only the credential diff
                # is applied and dispatched. A synthetic change carries the
                # ``credentials.*`` paths to the channel-restart applier, old and
                # new being the same live config.
                cred_paths = self._apply_credential_diff(
                    env_fingerprint, env_read, env_path_additions
                )
                if not cred_paths or self._cfg is None:
                    if self._stale and self._cfg is not None:
                        await self._retry_stale(self._cfg)
                    return None
                change = ConfigChange(old=self._cfg, new=self._cfg, changed=cred_paths)
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the arguments are credentials.<KEY> path names, never a credential value
                logger.info(
                    ".env credential key(s) changed and applied to the process "
                    "environment: %d (%s). A channel whose credential changed is "
                    "restarted; already-running children (ACP sessions, MCP "
                    "servers) keep their environment until they respawn.",
                    len(cred_paths),
                    ", ".join(sorted(cred_paths)),
                )
                await self._dispatch(change)
                return change
            try:
                cfg, doc, torn = await asyncio.to_thread(self._load_with_doc)
            except Exception as e:  # noqa: BLE001 - keep the previous snapshot
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("config reload failed; keeping previous snapshot: %s", e)
                return None
            if not torn and DEGRADED_WHOLE_CONFIG in cfg.degraded_sections:
                # The probe found the file whole, yet the load carries the
                # whole-config flag. Usually that flag is the loader's memory of an
                # earlier, since-repaired tear (it keeps it for the life of the
                # process) and the document is good. But when the file moved while
                # it was being read, the load and the probe may describe two
                # different documents -- a write landing between them -- and the
                # load may be the torn one. Treat that read as torn: the previous
                # snapshot stays, and the next tick reads the settled file.
                torn = await asyncio.to_thread(self._current_fingerprint) != fingerprint
            kept = _keep_last_valid(self._cfg, cfg, torn_now=torn)
            if kept is not cfg:
                # The values in force stay; the diff below is taken against them,
                # so a repair back to them dispatches nothing and a repair to new
                # values dispatches the new values.
                logger.warning(
                    "config.json does not parse; keeping the previous settings until it does"
                )
                cfg, doc = kept, kept.to_dict()
            if self._hold_depth > 0 or self._hold_gen != hold_gen:
                # A transaction began while the file was being read, so this
                # document may be its uncommitted intermediate state (a widened
                # allow-list ahead of a credential write that can still roll
                # back). Nothing from it is adopted or dispatched; the release
                # forces a fresh load of whatever the transaction committed.
                self._force = True
                return None
            changed = diff_config_docs(self._doc, doc)
            # ``.env`` moved in the same tick as ``config.json`` (the dashboard's
            # two-file save, or two edits landing together): apply the credential
            # diff too and fold its synthetic ``credentials.*`` paths in, so one
            # dispatch covers both and a channel whose credential changed restarts
            # alongside one whose config did.
            if env_changed:
                changed = changed | self._apply_credential_diff(
                    env_fingerprint, env_read, env_path_additions
                )
            old = self._cfg
            # Adopt before dispatch so an applier that reads snapshot() sees the
            # new config, and record the PRE-load fingerprint: a write landing
            # mid-read leaves it unequal to the file, so the next tick reloads.
            self._cfg, self._doc, self._fingerprint = cfg, doc, fingerprint
            self.last_error = None
            if not changed:
                if self._stale:
                    await self._retry_stale(cfg)
                return None
            change = ConfigChange(old=old, new=cfg, changed=changed)
            logger.info(
                "config reloaded: %d field(s) changed (%s)",
                len(changed),
                ", ".join(sorted(changed)[:12]) + (" …" if len(changed) > 12 else ""),
            )
            await self._dispatch(change)
            return change

    async def _dispatch(self, change: ConfigChange) -> None:
        for sub in self._live_subscriptions():
            stale = self._stale.get(id(sub))
            missed = stale[1] if stale else None
            # Fold in what THIS applier failed to adopt last time, so one
            # dispatch brings it fully current -- a per-subscriber view, so the
            # missed paths never reach the appliers after it.
            view = (
                ConfigChange(old=change.old, new=change.new, changed=change.changed | missed)
                if missed
                else change
            )
            if sub.prefixes and not view.touched(*sub.prefixes):
                continue
            await self._apply_one(sub, view)

    async def _apply_one(self, sub: Subscription, change: ConfigChange) -> None:
        cb = sub.callback()
        if cb is None:
            self._stale.pop(id(sub), None)
            return
        try:
            result = cb(change)
            if inspect.isawaitable(result):
                # Bounds an async applier that hangs rather than raises -- a
                # synchronous applier still blocks the loop for however long it
                # runs, since nothing here suspends until ``result`` is
                # awaitable. The bound is a backstop against an unbounded
                # stall, not protection from every misbehaving applier.
                await asyncio.wait_for(result, timeout=APPLIER_TIMEOUT_SECS)
        except asyncio.CancelledError:
            raise
        except ConfigDeferred as deferred:
            # The applier declined for now -- a fail-closed one refusing a torn
            # document, or a consumer that is not up yet. Not an error, but not a
            # success either: the paths stay stale so the applier runs again on
            # later ticks, even when a repaired document diffs empty against the
            # snapshot.
            prior = self._stale.get(id(sub))
            missed = deferred.paths or (
                frozenset(p for pre in sub.prefixes for p in change.under(pre))
                if sub.prefixes
                else change.changed
            )
            reason = (
                "the reloaded document is degraded"
                if getattr(change.new, "degraded_sections", None)
                else "the applier is not ready to adopt it yet"
            )
            logger.log(
                logging.DEBUG if prior else logging.WARNING,
                "config applier %r deferred %d path(s): %s; keeping the previous "
                "settings and retrying each tick until it accepts them",
                sub.name,
                len(missed),
                reason,
            )
            self._stale[id(sub)] = (sub, missed | (prior[1] if prior else frozenset()))
        except asyncio.TimeoutError:
            prior = self._stale.get(id(sub))
            level = logging.DEBUG if prior else logging.ERROR
            logger.log(
                level,
                "config applier %r exceeded %.0fs; retrying on the next tick",
                sub.name,
                APPLIER_TIMEOUT_SECS,
            )
            missed = (
                frozenset(p for pre in sub.prefixes for p in change.under(pre))
                if sub.prefixes
                else change.changed
            )
            self._stale[id(sub)] = (sub, missed | (prior[1] if prior else frozenset()))
        except Exception:
            prior = self._stale.get(id(sub))
            level = logging.DEBUG if prior else logging.ERROR
            logger.log(
                level,
                "config applier %r failed; retrying on the next tick",
                sub.name,
                exc_info=True,
            )
            missed = (
                frozenset(p for pre in sub.prefixes for p in change.under(pre))
                if sub.prefixes
                else change.changed
            )
            self._stale[id(sub)] = (sub, missed | (prior[1] if prior else frozenset()))
        else:
            self._stale.pop(id(sub), None)

    async def _retry_stale(self, cfg: "KiroCrewConfig") -> None:
        """Re-run every applier that failed, against the current snapshot."""
        live = {id(sub) for sub in self._live_subscriptions()}
        for key, (sub, missed) in list(self._stale.items()):
            if key not in live:
                self._stale.pop(key, None)
                continue
            await self._apply_one(sub, ConfigChange(old=cfg, new=cfg, changed=missed))

    @staticmethod
    def _current_fingerprint() -> tuple:
        from kiro_crew.config import loader

        return loader._config_fingerprint()

    @staticmethod
    def _current_env_fingerprint() -> tuple:
        from kiro_crew.config import loader

        return loader.env_fingerprint()

    @staticmethod
    def _read_env_credentials() -> "tuple[dict[str, str], bool]":
        """The raw ``.env`` credential map and whether the read was DEFINITIVE.

        ``definitive`` is ``False`` for a file that could not be read or is
        UTF-16/UTF-32: an empty map then means "unknown", not "every key gone",
        so the diff must not read it as every credential cleared.
        """
        from kiro_crew.config import loader

        definitive: list[bool] = []
        creds = loader.read_env_credentials_raw(definitive=definitive)
        return creds, bool(definitive and definitive[0])

    @staticmethod
    def _prepare_path_additions(creds: "dict[str, str]") -> "dict[str, tuple[str, ...]]":
        """Expand/validate the PATH line(s) in *creds* OFF the event loop.

        A ``PATH`` ``.env`` line holding a ``~otheruser`` entry makes expansion
        call ``pwd.getpwnam`` / the SAM database synchronously, which an
        LDAP/SSSD/AD lookup can stall. :meth:`_cycle` runs this in a worker
        thread and hands the result to :meth:`_apply_credential_diff`, so the
        applier only mutates ``os.environ`` on the loop. Keyed by the PATH key
        name as it appears in ``.env`` (``PATH``, or a case variant on Windows),
        matching what the applier's PATH carve-out iterates.
        """
        from kiro_crew import env, platform_compat

        out: dict[str, tuple[str, ...]] = {}
        for key, val in creds.items():
            if val and platform_compat.env_key_allowed(key, ("PATH",)):
                out[key] = env.prepare_env_path_additions(val)
        return out

    async def _prime_creds_baseline(self) -> None:
        """Read the boot credential map off-loop as the diff baseline.

        Only a DEFINITIVE read becomes the baseline; an unreadable or
        wide-encoded file leaves :attr:`_creds` ``None`` so the first cycle
        re-reads it rather than baselining on an empty map that a later readable
        save would diff as every key added.
        """
        try:
            creds, definitive = await asyncio.to_thread(self._read_env_credentials)
        except Exception:  # noqa: BLE001 - the first cycle re-reads it
            self._creds = None
            return
        self._creds = creds if definitive else None

    def _apply_credential_diff(
        self,
        env_fingerprint: tuple,
        read: "tuple[dict[str, str], bool] | None",
        path_additions: "dict[str, tuple[str, ...]] | None" = None,
    ) -> frozenset[str]:
        """Diff ``.env`` credentials against the last read, apply them, return change paths.

        Pure CPU work: *read* is ``(raw map, definitive)`` already read off the
        loop (``None`` means re-read here, for the test/``refresh_now`` path).
        *path_additions* is the PATH directories expanded and validated off the
        loop (:meth:`_prepare_path_additions`), passed to the applier so the
        blocking ``~otheruser`` expansion never runs on the loop; ``None`` lets
        the applier expand inline (the test/``refresh_now`` path).
        Diffs the map against :attr:`_creds`, pushes the changed keys into
        ``os.environ`` through the loader's live applier (set/pop per key,
        honouring the Docker scrub and PATH carve-outs), and returns one
        synthetic dotted path per changed key -- ``credentials.<KEY>`` -- for the
        channel-restart applier to map onto the channels whose credentials
        moved. The baseline and the ``.env`` fingerprint advance ONLY after the
        apply succeeds (or when there was nothing to apply): a failed apply
        leaves both untouched and dispatches nothing, so the keys it did not
        install are retried on the next tick rather than recorded as in force --
        see the ``except`` below for why advancing on failure strands a channel.

        A NON-definitive read (the file could not be decoded -- UTF-16/UTF-32 or
        an invalid byte -- or could not be read) is NOT a diff: its empty map
        would otherwise read as every credential cleared and tear down every
        channel that depends on one. The ``.env`` FINGERPRINT IS NOT ADVANCED --
        unlike a definitive read, which is cached by fingerprint, a non-definitive
        one must be retried: leaving the fingerprint stale means the next tick
        still sees ``env_changed`` and re-reads, so a one-off transient failure
        (e.g. a Windows sharing violation mid-rotation) is retried on the very
        next cycle instead of being silently and permanently skipped the moment
        the file's mtime stops moving. A persistently bad file pays a repeated
        but harmless re-read each tick (the undecodable warning still fires only
        once, via ``_warned_undecodable_env``). The baseline is left as it was,
        so the next readable save diffs against the last values that actually
        loaded.

        On the FIRST EVER definitive observation (:attr:`_creds` is ``None``) it
        diffs against an EMPTY baseline rather than adopting silently: ``_creds``
        is only ``None`` when no prior read ever became a baseline (boot's own
        credential load failed the same way -- see :meth:`_prime_creds_baseline`
        -- so nothing from ``.env`` ever reached ``os.environ``), so a key already
        present in this first readable map was never actually applied and must be
        diffed in, not assumed to already be in force. A map that happens to
        equal the boot propagation (nothing to apply) costs one no-op diff, which
        is cheap and only happens once.
        Values are never logged, here or by the applier.
        """
        from kiro_crew.config import loader

        if read is None:
            read = self._read_env_credentials()
        new_creds, definitive = read
        if not definitive:
            # Unreadable / wide-encoded / undecodable: keep the baseline and the
            # fingerprint, change nothing, and let the next tick retry.
            return frozenset()
        old_creds = self._creds if self._creds is not None else {}
        changed_keys = loader.changed_env_keys(old_creds, new_creds)
        if not changed_keys:
            # Nothing to apply, so nothing can fail: adopt the (identical) map
            # and advance the fingerprint, the no-diff steady state.
            self._env_fingerprint = env_fingerprint
            self._creds = new_creds
            return frozenset()
        try:
            # Pass the previous baseline so the apply can pop only what ``.env``
            # itself propagated (never an inherited value) and warn only on a
            # genuine inherited-value collision, not on a routine rotation. The
            # PATH additions were expanded off the loop (see the caller): hand
            # them in so the one call that can block -- a ``~otheruser``
            # directory-service lookup -- is not run here on the event loop.
            loader.apply_env_credential_changes(
                changed_keys, new_creds, old_creds, path_additions=path_additions
            )
        except Exception:  # noqa: BLE001 - a bad apply must not kill the poll
            # Leave the baseline AND the fingerprint exactly as they were, the
            # same retry contract as a non-definitive read above: advancing
            # either here would record a rotation that never reached
            # ``os.environ`` as already in force, so a later repair that reverts
            # the offending line would diff only that line and never re-apply the
            # rotation -- stranding the channel after the fix. Unadvanced, the
            # next tick re-reads the file (its fingerprint still looks changed)
            # and retries the whole diff, and nothing is dispatched now so no
            # restart is run against a credential the apply did not install.
            #
            # That retry means the SAME bad file re-fails every tick, so the
            # WARNING (with its traceback) fires ONCE per distinct failing file
            # state and the retries log at DEBUG -- the same warn-once shape the
            # undecodable path uses -- rather than spamming ~43k tracebacks/day
            # while a malformed line sits in ``.env``.
            if self._apply_failed_fingerprint != env_fingerprint:
                self._apply_failed_fingerprint = env_fingerprint
                logger.warning("applying .env credential changes failed", exc_info=True)
            else:
                logger.debug("applying .env credential changes still failing", exc_info=True)
            return frozenset()
        # Apply succeeded: only now is the new map really in force, so advance
        # the baseline and fingerprint and dispatch the restart for the keys that
        # moved. Clear any prior apply-failure signature so a later failure warns
        # afresh.
        self._apply_failed_fingerprint = None
        self._env_fingerprint = env_fingerprint
        self._creds = new_creds
        return frozenset(f"{CREDENTIAL_CHANGE_PREFIX}.{k}" for k in changed_keys)

    @staticmethod
    def _load() -> "KiroCrewConfig":
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()

    @classmethod
    def _load_with_doc(cls) -> tuple["KiroCrewConfig", dict[str, Any], bool]:
        cfg = cls._load()
        return cfg, cfg.to_dict(), cls._document_is_torn()

    @staticmethod
    def _document_is_torn() -> bool:
        """Whether a config file present on disk fails to parse as a JSON object NOW.

        The loader answers such a file with defaults and a degradation flag that
        stays for the life of the process, so the flag cannot say whether THIS
        read hit a torn file; the file can. Missing files are not torn.
        """
        from kiro_crew.config.loader import config_local_path, config_path, read_config_text

        for path in (config_path(), config_local_path()):
            try:
                if not path.exists():
                    continue
                data = json.loads(read_config_text(path))
            except Exception:  # noqa: BLE001 - unreadable or unparseable: torn
                return True
            if not isinstance(data, dict):
                return True
        return False


# ── process singleton ────────────────────────────────────────────────

_WATCH: ConfigWatch | None = None
_WATCH_LOCK = threading.Lock()


def watch() -> ConfigWatch:
    """The process-global watcher, created on first use (never started here)."""
    global _WATCH
    with _WATCH_LOCK:
        if _WATCH is None:
            _WATCH = ConfigWatch()
        return _WATCH


def subscribe(*prefixes: str, callback: Applier, name: str | None = None) -> Subscription:
    """Register an applier on the process watcher. See :meth:`ConfigWatch.subscribe`."""
    return watch().subscribe(*prefixes, callback=callback, name=name)


def watch_section(
    owner: object,
    section: str,
    *extra_prefixes: str,
    target: str | None = None,
    fail_closed: bool = True,
    method: str = "reconfigure",
    name: str | None = None,
) -> Subscription:
    """Module-level :meth:`ConfigWatch.watch_section` on the process watcher."""
    return watch().watch_section(
        owner,
        section,
        *extra_prefixes,
        target=target,
        fail_closed=fail_closed,
        method=method,
        name=name,
    )


def watch_object(
    owner: object, *prefixes: str, method: str = "reconfigure", name: str | None = None
) -> Subscription:
    """Module-level :meth:`ConfigWatch.watch_object` on the process watcher."""
    return watch().watch_object(owner, *prefixes, method=method, name=name)


def bind(path: str, setter: Callable[[Any], Any], *, name: str | None = None) -> Subscription:
    """Module-level :meth:`ConfigWatch.bind` on the process watcher."""
    return watch().bind(path, setter, name=name)


def hold() -> "contextlib.AbstractContextManager[None]":
    """Module-level :meth:`ConfigWatch.hold` on the process watcher."""
    return watch().hold()


def snapshot() -> "KiroCrewConfig | None":
    """The last applied config on the process watcher, or ``None`` if unstarted."""
    w = _WATCH
    return w.snapshot() if w is not None else None


def current(fallback: "KiroCrewConfig", *, log_prefix: str) -> "KiroCrewConfig":
    """The config in force NOW, for a per-turn read.

    The watcher's snapshot when it is armed, else a disk ``KiroCrewConfig.load()``,
    else *fallback*. Falling back to *fallback* rather than raising keeps a turn
    running when the config file is momentarily unreadable -- a threshold or a
    render toggle is not an authorization decision, and *fallback* is the value
    the caller booted with. *log_prefix* names the caller in the warning
    (``"whatsapp"``, ``"discord"``) so a torn file shows which subsystem noticed.
    """
    snap = snapshot()
    if snap is not None:
        return snap
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "%s: could not read live config; using the boot copy", log_prefix, exc_info=True
        )
        return fallback


def notify_config_written() -> None:
    """Tell the process watcher a config write just landed.

    Called by the loader's own writers, so every in-process writer wakes the
    watcher without knowing it exists. A no-op in processes with no watcher
    (the CLI, tests that never started one).
    """
    w = _WATCH
    if w is not None:
        w.notify_written()


def reset_for_tests() -> None:
    """Drop the process watcher so a test gets a fresh, unstarted one.

    Deliberately does NOT clear the once-per-process boot ``.env`` record
    (``loader.reset_boot_raw_env``): the integration second-boot witness reuses
    this between two boots of ONE process, where that record must survive
    unchanged (it is recorded on the first ``load_credentials`` and never
    rewritten) rather than be reset and re-recorded -- a reset here would make
    it read as a second-boot change. A unit test that needs the record cleared
    calls ``reset_boot_raw_env`` itself.
    """
    global _WATCH
    with _WATCH_LOCK:
        w = _WATCH
        _WATCH = None
    if w is not None and w._task is not None:
        w._task.cancel()


__all__ = [
    "Applier",
    "APPLIER_TIMEOUT_SECS",
    "ConfigChange",
    "ConfigDeferred",
    "ConfigWatch",
    "DEFAULT_POLL_INTERVAL_SECS",
    "Subscription",
    "current",
    "diff_config_docs",
    "flatten_config",
    "notify_config_written",
    "reset_for_tests",
    "snapshot",
    "subscribe",
    "watch",
]

"""Application discovery and OS-resolved identity, from the window list ONLY.

Two jobs, and the second is security-critical:

1. :func:`list_apps` / :func:`resolve_app` answer "which applications have a
   visible window, and which pid owns the one the agent named".
2. :func:`resolve_identity` produces the **OS-resolved** bundle id and display
   name that ``computer_use/gate.py`` queries governance on. The gate never
   queries the agent-supplied ``app`` string: if it did, an allow-listed
   ``com.apple.finder`` could be satisfied by a model claiming "finder" while the
   automation actually drove something else. Everything here answers "what did we
   really touch", not "what were we asked for".

**Never a process-name search.** Not ``pgrep``, not ``ps``, not a name scan of
any kind — the string does not appear in this module and a test asserts that.
The reason is a reproduced failure, not a style preference: ``pgrep -n "Google
Chrome"`` returned 47492, a short-lived helper process that answered
``kAXErrorCannotComplete`` to every accessibility read and then exited, while the
real browser was 637. Slack's helper 1614 likewise shadowed the real 942. The pid
that owns a visible, layer-0 window is the one whose accessibility tree is
populated, so the CoreGraphics window list is the only source of truth.

Bundle-id resolution is a **live finding that contradicts the obvious design.**
Neither source you would reach for first works:

* the window-list dictionary carries **no bundle-id key at all** (verified: the
  dict is exactly ``{Alpha, Bounds, IsOnscreen, Layer, MemoryUsage, Name, Number,
  OwnerName, OwnerPID, SharingState, StoreType}``);
* ``AXBundleIdentifier`` on the application element returns
  ``kAXErrorAttributeUnsupported`` (-25205) for **every** app on this macOS —
  Chrome, Slack, Notes, Obsidian, Outlook, System Settings, all of them. It is
  not an Electron quirk and not a permissions artifact.

So the bundle id comes from ``proc_pidpath`` plus the enclosing bundle's
``Info.plist`` — one syscall and one small plist read, measured under 1ms per pid,
verified correct for all eight apps on screen (``com.google.Chrome``,
``com.tinyspeck.slackmacgap``, ``md.obsidian``, ``com.apple.Notes``,
``com.apple.systempreferences``, …). An unbundled binary yields ``""`` and the
process name carries the identity instead; the gate treats a fully unresolved
identity as a DENY, which is the correct posture for a target we cannot name.
"""

from __future__ import annotations

import logging
import os
import plistlib
import threading
import time
from dataclasses import dataclass

from kiro_crew.computer_use import macos_ffi
from kiro_crew.computer_use.policy import title_is_denied as _denied_title
from kiro_crew.computer_use.types import (
    ERR_APP_NOT_FOUND,
    TOOL_LIST_APPS,
    AppRef,
    ComputerUseError,
)

logger = logging.getLogger(__name__)

# Info.plist keys, in the order :func:`_bundle_metadata` prefers them for a
# display name. ``CFBundleName`` before ``CFBundleDisplayName`` because the former
# is the short canonical name ("Chrome") users and governance patterns type, while
# the latter is often a marketing string.
PLIST_BUNDLE_ID_KEY = "CFBundleIdentifier"
PLIST_BUNDLE_NAME_KEYS: tuple[str, ...] = ("CFBundleName", "CFBundleDisplayName")
BUNDLE_SUFFIX = ".app"
BUNDLE_INFO_RELPATH = os.path.join("Contents", "Info.plist")

# Largest Info.plist we will parse. A bundle's Info.plist is a few KB; anything
# far larger is either not what we think it is or a resource-exhaustion attempt
# through a hand-crafted bundle, and the identity is not worth an unbounded read.
MAX_INFO_PLIST_BYTES = 512 * 1024

# Identity cache. A pid's bundle identity cannot change while the process lives,
# so caching it turns a per-action plist read into a dict hit. The TTL bounds pid
# reuse: a recycled pid belonging to a different program must not inherit the
# previous occupant's identity, since that identity is what governance is queried
# on. 60s is far shorter than any realistic reuse window on macOS (pids advance
# monotonically through a large space) while still covering a whole turn.
IDENTITY_CACHE_TTL_SECS = 60.0
MAX_CACHED_IDENTITIES = 64


@dataclass(frozen=True)
class AppIdentity:
    """The OS-resolved identity of one process.

    ``bundle_id`` is the stable OS identity and ``display_name`` is the
    human-facing one. ``policy.check_app`` matches them SEPARATELY (never
    pipe-joined against one pattern), because an app's display name is
    attacker-chooseable and must not be able to satisfy a bundle-id rule.

    ``executable`` is kept for diagnostics: when both identifiers come back empty
    it is the only thing that can tell an operator what the sidecar was looking
    at.
    """

    bundle_id: str = ""
    display_name: str = ""
    executable: str = ""

    @property
    def resolved(self) -> bool:
        """True when at least one governable identifier is known.

        The gate refuses a call with no identity at all: under a deny-mode
        ``apps`` ruleset an empty item matches no pattern and would therefore
        PERMIT, so governance cannot bound a target it cannot name.
        """
        return bool(self.bundle_id or self.display_name)


_identity_cache: dict[int, tuple[float, AppIdentity]] = {}
_identity_lock = threading.Lock()


def list_apps() -> tuple[AppRef, ...]:
    """Applications with at least one on-screen, layer-0 window.

    One :class:`AppRef` per application (not per window), keyed by pid: an app
    with six windows is one entry.

    **The window we keep is the frontmost one that has a TITLE**, not simply the
    first layer-0 entry. Window-list order is CoreGraphics' front-to-back z-order,
    so "first" is usually right — but verified live on Notes, the frontmost layer-0
    window was an untitled ``AXDialog`` exposing 3 accessibility nodes, sitting in
    front of the real 103-node document window. Taking it verbatim would make
    ``computer_get_state`` on Notes return a near-empty tree while the app clearly
    had content, with nothing in the result explaining why. An untitled window is
    still kept when the app has no titled one at all (a splash screen, a bare
    alert) — the app is genuinely showing that.

    Non-zero layers are dropped: menu bars, the Dock, tooltips, notification
    banners and window shadows all live there, they are not addressable
    application windows, and their owning pid is frequently a system agent whose
    tree is empty.
    """
    seen: dict[int, AppRef] = {}
    for info in macos_ffi.window_list():
        if info.layer != macos_ffi.CG_WINDOW_LAYER_NORMAL:
            continue
        if info.pid <= 0 or not info.owner_name:
            continue
        existing = seen.get(info.pid)
        if existing is not None and _denied_title(existing.window_title):
            # Already holding a window whose TITLE trips the denylist. Never
            # replace it: input is delivered per-PID (``CGEventPostToPid``), so if
            # ANY window of this process is our own dashboard the whole process must
            # refuse. Keeping the innocuous first window would let a second Chrome
            # window hosting KiroCrew slip past the title rule entirely.
            continue
        if existing is not None and not _denied_title(info.title):
            if existing.window_title or not info.title:
                # Already have a window for this app, and either it is titled (good
                # enough) or this candidate is not an improvement.
                continue
        identity = resolve_identity(info.pid)
        seen[info.pid] = AppRef(
            # The window list's owner name is the process name; the bundle's
            # CFBundleName is usually the nicer one ("Chrome" vs "Google
            # Chrome"), but the process name is what the operator sees in
            # Activity Monitor and what the denylist's name substrings match, so
            # it stays authoritative for ``name``.
            name=info.owner_name,
            pid=info.pid,
            bundle_id=identity.bundle_id,
            window_id=info.window_id,
            window_title=info.title,
        )
    return tuple(seen.values())


def resolve_app(query: str) -> AppRef:
    """Resolve *query* (process name, bundle id, or a fragment) to one app.

    Raises :class:`ComputerUseError` when nothing matches, naming
    ``computer_list_apps`` so the model's next move is obvious.

    Match order, most specific first, so a precise query is never captured by a
    loose one:

    1. exact bundle id;
    2. exact process name (case-insensitive);
    3. bundle-id substring;
    4. process-name substring.

    Within a tier the frontmost window wins (window-list order). The tiers matter
    for a real ambiguity: a query of ``"notes"`` should reach ``com.apple.Notes``
    rather than an app whose window title happens to contain the word.
    """
    needle = (query or "").strip().lower()
    if not needle:
        raise ComputerUseError(ERR_APP_NOT_FOUND.format(query=query, tool=TOOL_LIST_APPS))
    apps = list_apps()

    for app in apps:
        if app.bundle_id and app.bundle_id.lower() == needle:
            return app
    for app in apps:
        if app.name and app.name.lower() == needle:
            return app
    for app in apps:
        if app.bundle_id and needle in app.bundle_id.lower():
            return app
    for app in apps:
        if app.name and needle in app.name.lower():
            return app
    raise ComputerUseError(ERR_APP_NOT_FOUND.format(query=query, tool=TOOL_LIST_APPS))


def resolve_identity(pid: int) -> AppIdentity:
    """The OS-resolved identity of *pid*. Never raises.

    This is the function ``gate.require_computer_use`` is fed from, so it must
    answer "what is this process, according to the OS" and nothing else — no
    agent-supplied string reaches it.

    Resolution path and why it is not the obvious one:

    * ``AXBundleIdentifier`` is **not** consulted. Verified to return
      ``kAXErrorAttributeUnsupported`` for every application on this macOS, so a
      code path built on it would resolve nothing while looking correct.
    * The window-list dictionary carries no bundle-id key either.
    * So: ``proc_pidpath(pid)`` gives the absolute executable path, the nearest
      enclosing ``*.app`` directory gives the bundle, and its ``Info.plist``
      gives ``CFBundleIdentifier`` + ``CFBundleName``. Verified correct for every
      app on screen, sub-millisecond per pid.

    Returns an empty-but-``resolved``-False identity for an unbundled binary or a
    dead pid. That is deliberately NOT patched up with a guess: the gate denies an
    unidentifiable target, which is the right answer, and inventing an identity
    would create a governable name the operator never approved.
    """
    if pid <= 0:
        return AppIdentity()
    now = time.monotonic()
    with _identity_lock:
        cached = _identity_cache.get(pid)
        if cached is not None and now - cached[0] <= IDENTITY_CACHE_TTL_SECS:
            return cached[1]

    identity = _resolve_identity_uncached(pid)

    with _identity_lock:
        # Evict expired entries opportunistically, then bound the map. Only a
        # handful of apps are in play per turn; the cap exists so a long-lived
        # sidecar cannot accumulate an entry per pid it ever saw.
        for key, (stamp, _) in list(_identity_cache.items()):
            if now - stamp > IDENTITY_CACHE_TTL_SECS:
                _identity_cache.pop(key, None)
        while len(_identity_cache) >= MAX_CACHED_IDENTITIES:
            _identity_cache.pop(next(iter(_identity_cache)), None)
        _identity_cache[pid] = (now, identity)
    return identity


def reset_identity_cache() -> None:
    """Drop the identity cache (driver close, tests)."""
    with _identity_lock:
        _identity_cache.clear()


def identity_for(app: AppRef) -> AppIdentity:
    """Identity for an already-resolved :class:`AppRef`.

    Re-resolves from the pid rather than trusting the ``AppRef``'s own fields:
    the ref may have been built from a cached snapshot, and the gate must be fed
    a freshly observed identity. Falls back to the ref's process name for
    ``display_name`` so an unbundled app is still governable by name.
    """
    identity = resolve_identity(app.pid)
    if identity.display_name or not app.name:
        return identity
    return AppIdentity(
        bundle_id=identity.bundle_id,
        display_name=app.name,
        executable=identity.executable,
    )


def _resolve_identity_uncached(pid: int) -> AppIdentity:
    """Do the real resolution work for one pid. Never raises."""
    try:
        executable = macos_ffi.executable_path(pid)
    except Exception:
        # A dead pid, a sandbox refusal, or an FFI problem: identity unknown is a
        # valid answer here and the gate handles it (by denying).
        logger.debug("proc_pidpath failed for pid %s", pid, exc_info=True)
        return AppIdentity()
    if not executable:
        return AppIdentity()
    bundle_id, bundle_name = _bundle_metadata(executable)
    return AppIdentity(bundle_id=bundle_id, display_name=bundle_name, executable=executable)


def _bundle_metadata(executable: str) -> tuple[str, str]:
    """``(bundle_id, display_name)`` from the bundle enclosing *executable*.

    Walks UP from the executable looking for the nearest ``*.app`` directory,
    because the binary always sits at ``Foo.app/Contents/MacOS/Foo`` and a helper
    can be nested deeper still. Returns ``("", "")`` for a plain binary.
    """
    bundle = _enclosing_bundle(executable)
    if not bundle:
        return "", ""
    info = _read_info_plist(os.path.join(bundle, BUNDLE_INFO_RELPATH))
    if not info:
        return "", ""
    bundle_id = info.get(PLIST_BUNDLE_ID_KEY)
    bundle_id = bundle_id.strip() if isinstance(bundle_id, str) else ""
    display = ""
    for key in PLIST_BUNDLE_NAME_KEYS:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            display = value.strip()
            break
    return bundle_id, display


def _enclosing_bundle(executable: str) -> str:
    """The nearest ancestor directory ending in ``.app``, or ``""``.

    Bounded by the path's own depth: the loop terminates when ``dirname`` stops
    changing, so a malformed or relative path cannot spin.
    """
    current = os.path.abspath(executable)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return ""
        current = parent
        if current.endswith(BUNDLE_SUFFIX):
            return current


def _read_info_plist(path: str) -> dict:
    """Parse a bundle ``Info.plist``, or return ``{}``. Never raises.

    Size-capped and fully defensive. This reads a file path derived from a
    process the AGENT chose to target, so it must tolerate a hand-crafted bundle
    with a malformed, enormous, or hostile plist — and the consequence of failing
    is only "identity unknown", which the gate already handles by denying.

    ``plistlib`` is stdlib and parses both the binary and XML formats macOS uses.

    Read through ``hooks.safe_read_prefix``, which is the repo's hardened path for
    any read of an agent-influenced location: it canonicalizes with ``realpath``,
    re-checks the RESOLVED target against ``is_sensitive_path``, and opens with
    ``O_NOFOLLOW``.

    All three matter here. The path comes from the OS (the target process's
    executable, walked up to its ``.app`` ancestor) rather than from the agent, but
    the agent DOES choose which process to target — and therefore can arrange the
    bundle. So a bundle planted under a protected directory (``~/.ssh/evil.app``)
    must not have its ``Info.plist`` read, and — the part an earlier revision got
    wrong — checking the path and then opening it separately is check-then-open: a
    final-component symlink swapped in between would read a protected file on a path
    that never consulted the floor. ``O_NOFOLLOW`` on the canonical path is what
    closes that window.

    The floor is about the file being opened, not about who computed the string, and
    the cost of honouring it is only "identity unknown" for a bundle in a place no
    application belongs — which the gate already handles by denying.
    """
    try:
        from kiro_crew.hooks import safe_read_prefix

        # Through ``safe_read_prefix``, not a bare ``open`` — GPT 5.6 BLOCKING, and
        # correct. The hand-rolled version checked ``is_sensitive_path`` and then
        # opened the path separately, which is check-then-open: the agent chooses
        # which process to target, so it can also arrange the bundle, and a
        # final-component symlink swapped between the check and the open would have
        # read a protected file's bytes on a path that never touched the hardened
        # gate. The helper canonicalizes with ``realpath``, re-checks the RESOLVED
        # target, and opens with ``O_NOFOLLOW`` — which is the TOCTOU defence this
        # code was missing, and the repo's stated requirement for any read of an
        # agent-influenced path.
        #
        # Reading ``MAX_INFO_PLIST_BYTES + 1`` rather than the cap: the extra byte is
        # what distinguishes "exactly at the limit" from "larger than the limit"
        # without a second ``getsize`` call, and the size check has to be on the bytes
        # actually READ rather than on a stat of a path that may no longer be the same
        # file — the same race, one step further along.
        raw = safe_read_prefix(path, MAX_INFO_PLIST_BYTES + 1)
        if raw is None:
            logger.debug("refusing to read an Info.plist on the sensitive-path floor")
            return {}
        if len(raw) > MAX_INFO_PLIST_BYTES:
            logger.debug("Info.plist too large to parse: %s", path)
            return {}
        parsed = plistlib.loads(raw)
    except Exception:
        logger.debug("Info.plist unreadable: %s", path, exc_info=True)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def window_bounds(window_id: int, pid: int) -> "tuple[float, float, float, float] | None":
    """Screen rect of *window_id*, or ``None`` if it is gone or not owned by *pid*.

    ``(left, top, width, height)`` in the package's top-left convention. Needed by
    ``sky_click``, which routes by a WINDOW-LOCAL point and therefore has to convert
    the caller's screen coordinate — the window server ignores the screen point on
    that path and reads the local one.

    The *pid* is verified rather than trusted: window ids are recycled, so a stale
    id can name a different app's window, and sky_click's whole purpose is clicking
    something the operator cannot see (a mis-aimed click there is invisible).
    Returns ``None`` on any error so the caller refuses rather than guessing a rect.
    """
    try:
        for info in macos_ffi.window_list():
            if info.window_id != int(window_id):
                continue
            if info.pid != int(pid) or info.bounds is None:
                return None
            return info.bounds
    except Exception:
        logger.debug("window_bounds lookup failed for %s", window_id, exc_info=True)
    return None


def pid_owns_point(pid: int, x: float, y: float) -> bool:
    """Does *pid* own the topmost on-screen window containing ``(x, y)``?

    THE confinement check for the real-pointer click path, and the reason it has
    to exist: ``CGWarpMouseCursorPosition`` + a global ``CGEventPost`` deliver to
    whatever is under that pixel, with no pid in the call. Every other input path
    is app-scoped (``CGEventPostToPid``), so the resolved app identity IS the
    authorization boundary — ``policy.check_app`` ran the denylist against THAT
    app. Without this check, naming an allowed app and passing coordinates over a
    denied one (KiroCrew's own window) would sail through: the policy check passes
    on app A while the click lands on app B.

    Topmost, not merely "inside": the window list is front-to-back z-order, so the
    FIRST layer-0 entry whose rect contains the point is the window that would
    actually receive the click. Testing "any window of *pid* contains the point"
    would wrongly permit a click that visually lands on a denied app overlapping
    the allowed one.

    Fails CLOSED (``False``) when the point belongs to nobody, when the owning
    window carries no readable bounds, or on any error — refusing a legitimate
    click costs the model one clear refusal, while permitting a mis-aimed one is
    an irreversible action in an app the operator never authorized.
    """
    try:
        for info in macos_ffi.window_list():
            bounds = info.bounds
            if bounds is None:
                # Cannot place this window, so cannot prove it does NOT cover the
                # point. Refuse rather than look past it.
                return False
            left, top, width, height = bounds
            if not (left <= x < left + width and top <= y < top + height):
                continue
            # FIRST containing window in z-order wins, whatever its layer. A
            # non-normal window is NOT skipped here, which is the opposite of what
            # ``list_apps`` does and deliberately so: this function answers "what
            # would a physical click at this pixel hit?", and a notification banner,
            # a menu-bar extra or an open menu sitting above the authorized app
            # really would receive that click. Skipping those layers let the app
            # UNDERNEATH grant permission for a click the operator's own overlay was
            # about to swallow — the very confinement this function exists to
            # provide. ``list_apps`` skips them for the unrelated reason that they
            # are not addressable TARGETS.
            return info.pid == pid and info.layer == macos_ffi.CG_WINDOW_LAYER_NORMAL
        return False
    except Exception:
        logger.debug("point ownership check failed; refusing", exc_info=True)
        return False


__all__ = [
    "AppIdentity",
    "IDENTITY_CACHE_TTL_SECS",
    "MAX_INFO_PLIST_BYTES",
    "identity_for",
    "list_apps",
    "pid_owns_point",
    "reset_identity_cache",
    "resolve_app",
    "resolve_identity",
]

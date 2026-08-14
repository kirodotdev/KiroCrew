"""Anonymous daily-heartbeat beacon — product analytics, stdlib-only.

Answers questions no local signal can: how many installations are actually
RUNNING (Daily Active Instances), which VERSIONS they run, which PYTHON minor
version they run on, which DISTRIBUTION CHANNEL they came from, and what share of
installs are launched only once. Download counts cannot answer these — an install
downloaded and never launched is indistinguishable from an active one, and
self-hosted distribution links have no download telemetry.

DATA MINIMIZATION IS THE DESIGN CONSTRAINT. The payload is FIVE fields, and it
is deliberately smaller than what this module once sent. ``chan`` (release
channel), ``os``, ``arch`` and ``gov`` (governance posture) were removed: each
was individually low-cardinality and defensible, but *collectively* they made the
row narrower — a stable id plus OS plus architecture plus channel plus governance
posture partitions the population far more finely than any one field suggests,
and correlated attributes on a stable id are exactly the fingerprinting hazard
this allowlist exists to prevent. When adding a field, the question is not "is
this value low-cardinality?" but "how much smaller does this make the crowd this
install hides in?" The answer for all four was "too much for what it bought".

DELIBERATELY SEPARATE FROM ``kiro_crew.metrics`` (the OTEL trunk). Four reasons,
each independently disqualifying:

  1. ``MetricsRecorder._guard()`` runs EVERY attribute through
     ``schema.redact()``. A 64-char sha256 install id is replaced with
     ``"[REDACTED]"`` (it trips the 40+-hex rule), so DAU would silently
     compute as 1. Values that merely *survive* redaction are no better: the
     recorder caches one instrument per name forever and ``schema.py`` requires
     low-cardinality ENUM-like values, so a per-machine id is exactly the
     "cardinality bomb" that contract exists to prevent. Making it pass means
     weakening the repo's only privacy-enforcement chokepoint for a product
     metric.
  2. OTLP egress lives in the ``kirocrew[otlp]`` package extra, NOT the default
     dependency set, so routing through it would measure only users who
     installed an optional extra.
  3. ``telemetry.enabled`` is documented (config help, spec, dashboard panel)
     as LOCAL collection with "nothing leaves this machine". Hanging an
     outbound heartbeat off it would turn an already-published no-egress
     promise into an egress switch.
  4. Shape mismatch: OTEL metrics carry pre-AGGREGATED data points (DELTA
     histograms / counter sums). DAU needs one row per install per day, deduped
     at QUERY time; aggregating away the id makes DAU uncomputable.

So this module shares exactly one thing with the metrics trunk: the atomic
create-once file pattern proven by ``handlers_system.py::_get_telemetry_salt``
(``os.link`` for atomicity, owner-only mode, in-memory fallback). It reuses
NONE of its state, config, or dependencies — ``urllib.request`` only (already
used by ``embeddings.py``), so no new dependency.

PRIVACY:
  * The install id is a random UUID4 generated locally. It is derived from
    NOTHING — not hostname, username, MAC, IP, account id, repo path, or
    serial. It means "the same installation", never "who".
  * Deliberately NOT ``handlers_system._get_owner_hash()``: that is
    ``HMAC(salt, hostname + ":" + username)``. It never leaves the host today,
    and sending it would change its character entirely.
  * The payload is a fixed FIVE-key ALLOWLIST built by :func:`payload`. There
    is no free-form field and no caller-supplied pass-through, so no prompt,
    path, repo name, credential, or model output can reach the wire — not by
    accident and not via a future call site.
  * Every value is a low-cardinality constant or coarse bucket. ``py`` is
    minor-only (``3.12``, never ``3.12.13``), ``v`` is release-only (``0.1.2``,
    never the ``-nightly.20260731t065756`` build stamp), ``dist`` is one of five
    values, and ``first_seen`` is a single bit — specifically so the field set
    cannot become a fingerprint when combined. A per-build timestamp is the
    clearest example of why: it is near-unique, so combined with a stable id it
    would pick out individual machines.
  * The server persists NO client IP: the beacon distribution's log delivery
    selects only ``date``/``time``/``cs-uri-stem``/``cs-uri-query``/
    ``c-country``/``sc-status``. ``c-ip`` is never among the delivered fields,
    so it is not written to storage at all.

DEFAULT-ON, with four suppressions and a one-command opt-out. Off entirely
when: an enterprise governance ceiling pins ``capabilities.telemetry`` off,
``KIROCREW_TELEMETRY_DISABLED`` is truthy, ``telemetry.beacon_enabled`` is
false, the process looks like CI, or ``KIROCREW_HOME`` is non-default (dev
homes, pods, worktree previews — one operator's own extra instances, which
would inflate the count).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME, config_dir
from kiro_crew.platform.governance_profiles import (
    GOVERNANCE_ERROR_REASON,
    governance_permits,
    vet_and_audit,
)

logger = logging.getLogger(__name__)

# The default endpoint lives in ``config/loader.py`` next to the other config
# defaults (and so this module adds no import edge into the config package).
# Every function here takes the endpoint as a parameter; an empty endpoint
# disables sending entirely.

# Filenames under the data home. They must NOT ride an export/snapshot onto a
# second machine (two hosts sharing one id would collapse to a single Daily
# Active Instance). That is guaranteed by NON-SELECTION rather than by a
# basename filter: root-level export copies a hard-coded allowlist and snapshot
# staging copies an explicit per-component file list, and neither names a beacon
# file. A basename exclusion would ALSO drop any user file sharing the name from
# the workspace/ tree, so deliberately none is registered.
INSTALL_ID_FILE = "beacon_install_id"
STAMP_FILE = "beacon_last_sent"

# Schema version in the path, so a future payload change is a NEW route rather
# than an ambiguous reinterpretation of historical rows.
BEACON_SCHEMA = "1"

# Total wall-clock budget. Deliberately short: a heartbeat must never be
# something a user notices, and losing a day's beacon is worth far less than a
# slow start.
HTTP_TIMEOUT_SECS = 5.0

# Opt-out env var. Truthy disables; mirrors the KIROCREW_TELEMETRY convention
# in metrics/provider.py so operators only learn one spelling.
DISABLE_ENV = "KIROCREW_TELEMETRY_DISABLED"
_ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Env vars marking an automated environment. CI installs are not humans using
# the product; counting them would inflate DAU with every pipeline run.
_CI_ENV_VARS = (
    "CI",
    "CONTINUOUS_INTEGRATION",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "JENKINS_URL",
    "TEAMCITY_VERSION",
)

# Distribution channel: which install path a running copy came from. Clamped to
# this fixed set so the field can never carry a free-form value.
#
# Resolution order is BAKED MODULE first, then env var, then "source".
#
# The baked module (``_build_info.py``, written into the package tree by each
# packaging path) is authoritative because it is the only source a running
# install cannot change. ``KIROCREW_DISTRIBUTION`` is inherited by every child
# process and settable by anyone with a shell, so a stray export in a user's
# profile would silently relabel that host's daily count. The env var is kept
# BELOW the baked value as a build/test override for packaging paths that have
# no staging step of their own.
#
# "source" is the git-clone path and the correct answer for an unstamped tree,
# so it is the default rather than an "unknown" bucket.
DIST_ENV = "KIROCREW_DISTRIBUTION"
KNOWN_DISTRIBUTIONS = frozenset({"dmg", "appimage", "wheel", "source", "docker"})
DEFAULT_DISTRIBUTION = "source"

# Optional dependency: ``_build_info`` exists only in a packaged artifact, so
# ImportError is the normal case in a checkout, not an error. Resolved once at
# import and held in a module-level binding, which is also the seam tests patch;
# writing a real file into the installed package would be shared mutable state
# across xdist workers.
try:
    from ._build_info import DISTRIBUTION as _BAKED_DISTRIBUTION  # type: ignore[import-not-found]
except ImportError:
    _BAKED_DISTRIBUTION = ""

# Fallback when a version string carries no parseable release number.
UNKNOWN_VERSION = "unknown"

# ``major.minor.patch`` and nothing else.
_RELEASE_RE = re.compile(r"^\D*(\d+)\.(\d+)(?:\.(\d+))?")

# Fallback id when the data home is unwritable (read-only container, etc).
# Process-local, so such a host contributes at most one count per process and
# never crashes the caller. Eager: trivial cost, removes a race.
_IN_MEMORY_ID: str = uuid.uuid4().hex


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ENV_TRUTHY


def is_env_opted_out() -> bool:
    """Return whether ``KIROCREW_TELEMETRY_DISABLED`` pins the beacon off.

    Public because the dashboard's privacy panel must distinguish "off because
    the stored flag is false" (a toggle can flip it) from "off because the
    environment says so" (a config write would be accepted and then have no
    effect — so the UI disables the control and says why instead).
    """
    return _env_truthy(DISABLE_ENV)


def is_ci() -> bool:
    """Return whether this looks like an automated/CI environment."""
    return any(os.environ.get(v) for v in _CI_ENV_VARS)


def is_default_home() -> bool:
    """Return whether the data home is the user's real one.

    A non-default ``KIROCREW_HOME`` means a dev home (``.kirocrew-dev``), a pod,
    or a worktree preview — one operator's own extra instances, so counting them
    would inflate DAU.

    Compared against ``~/.kiro/crew`` directly rather than against
    ``config_dir()``: ``config_dir()`` *honors* ``KIROCREW_HOME``, so comparing
    the two would always match and this suppression would never fire. Resolved
    on both sides so a symlinked or trailing-slash spelling of the real home
    still counts as default.
    """
    raw = os.environ.get("KIROCREW_HOME", "").strip()
    if not raw:
        return True
    try:
        default = Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF
        return Path(raw).expanduser().resolve() == default.resolve()
    except (OSError, RuntimeError):
        # RuntimeError as well as OSError: Path.home() raises RuntimeError (not
        # OSError) when the UID has no passwd entry, which is normal in a
        # container. Fail closed — an unverifiable home is treated as
        # non-default, so an odd environment is never counted.
        return False


def baked_distribution() -> str:
    """Return the distribution stamped into the package tree, or "".

    Reads the module-level :data:`_BAKED_DISTRIBUTION`, so tests set that
    binding rather than writing a real file into the installed package. A value
    outside :data:`KNOWN_DISTRIBUTIONS` yields "" so the caller falls through
    instead of putting an unclamped value on the wire.
    """
    raw = str(_BAKED_DISTRIBUTION or "").strip().lower()
    return raw if raw in KNOWN_DISTRIBUTIONS else ""


def distribution() -> str:
    """Return the build's distribution channel, clamped to the known set.

    Baked module wins over the environment: see :data:`DIST_ENV` for why a
    running install must not be able to relabel its own count.
    """
    baked = baked_distribution()
    if baked:
        return baked
    raw = (os.environ.get(DIST_ENV, "") or "").strip().lower()
    return raw if raw in KNOWN_DISTRIBUTIONS else DEFAULT_DISTRIBUTION


def release(app_version: str) -> str:
    """Return ``major.minor.patch`` only, dropping every build stamp.

    ``__version__`` is not low-cardinality in the field. Dev and nightly builds
    carry a per-build timestamp (``0.1.2-nightly.20260731t065756``,
    ``0.1.2.dev20260731065756``), so sending it raw mints a NEW CloudWatch
    metric per build and fragments the one number this field exists to produce:
    a real 54-install 0.1.2 population reported as 35 + a fringe of one-install
    series, which reads as adoption decay when nothing changed.

    Worse than the noise, it is LOSSY. The aggregator caps each breakdown at
    ``LIMIT 25`` per day, so once the distinct-version count crosses that, real
    low-install releases fall below the cut and vanish from BOTH CloudWatch and
    the permanent Athena rollup — silently, since only the surviving row count
    is reported. A long-tail release is exactly what drops first, and that is
    the one you most need to know is still running.

    The channel is DISCARDED rather than moved to a field of its own. It was once
    its own three-value ``chan`` key; the data-minimization pass removed it,
    because release channel is one of the attributes that most sharply narrows the
    crowd a stable id hides in — a nightly install is a small population by
    definition, which is precisely what makes it identifying.

    Be honest about the cost: this function strips the prerelease label too, so a
    nightly ``0.1.2-nightly.<stamp>`` and a stable ``0.1.2`` both send ``v=0.1.2``
    and are **indistinguishable on the wire**. Channel-split adoption is therefore
    NOT recoverable from the beacon at all — it is a capability that was given up,
    not relocated. The pre-release population is observable from the release feeds
    and CDN fetch counts (`feed/<channel>/latest-cli.json`), which are per-artifact
    and carry no install id, so the question has a home; it just is not this one.

    Returns ``UNKNOWN_VERSION`` when no release number can be parsed, so a
    malformed stamp becomes one bounded bucket instead of a new metric.
    """
    match = _RELEASE_RE.match((app_version or "").strip())
    if not match:
        return UNKNOWN_VERSION
    major, minor, patch = match.group(1), match.group(2), match.group(3)
    return f"{major}.{minor}.{patch or '0'}"


def python_minor() -> str:
    """Return ``major.minor`` only (e.g. ``3.12``).

    Never the patch level: ``3.12.13`` would add cardinality without answering
    anything the minor version does not. The actionable question is "when can
    the floor move off 3.10", and minor answers it.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _valid_id(value: str) -> bool:
    """Return whether *value* is a well-formed 32-char lowercase hex id."""
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


# Hard ceiling on any beacon state read. Both files hold a fixed short token (a
# 32-char hex id, a 10-char date), so this is ~2 orders of magnitude of slack
# while still bounding a hostile or corrupt file.
_MAX_STATE_BYTES = 4096


def _read_state(path: Path) -> str:
    """Read a small beacon state file safely, or return "".

    THREE guards, each closing a distinct failure mode:

    * **Regular files only.** ``path.read_text()`` FOLLOWS symlinks, so a link at
      ``beacon_install_id`` pointing at ``/dev/zero`` turns this into an infinite
      read — verified to allocate unboundedly until OOM, inside the gateway's
      beacon thread. ``lstat`` + ``S_ISREG`` rejects links, FIFOs (which would
      block forever), and device nodes without ever opening them.
    * **Bounded length.** Even a regular file can be enormous (a log rotated onto
      this name), so read at most ``_MAX_STATE_BYTES`` rather than the whole file.
    * **Lenient decode.** ``errors="replace"`` — a strict decode raises
      ``UnicodeDecodeError``, which is a ``ValueError`` and NOT an ``OSError``, so
      it escaped the callers' handlers. Mojibake simply fails ``_valid_id`` and
      takes the existing corrupt-state path.

    Returns "" for anything unreadable, which every caller already treats as
    absent/corrupt.
    """
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            logger.debug("beacon state %s is not a regular file; ignoring", path.name)
            return ""
        with path.open("rb") as fh:
            raw = fh.read(_MAX_STATE_BYTES)
    except (OSError, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace").strip()


def install_id(*, create: bool = True) -> str:
    """Return this installation's random anonymous id.

    Persisted owner-only under the data home. Mirrors
    ``handlers_system._get_telemetry_salt``'s atomic create: write a temp file
    then ``os.link`` it into place, so two processes racing the first send
    converge on ONE id rather than overwriting each other (two ids for one
    install would double-count it for a day).

    With ``create=False`` the file is only read, never generated — used by
    ``kirocrew telemetry status`` so merely inspecting status cannot
    materialize an id on a host that has opted out.
    """
    try:
        path = config_dir() / INSTALL_ID_FILE
        if path.exists():
            existing = _read_state(path)
            if _valid_id(existing):
                return existing
            # Corrupt/truncated — remove before regenerating so a malformed key
            # is never sent (the server would have to reject it).
            path.unlink(missing_ok=True)
        if not create:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = uuid.uuid4().hex
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
        try:
            os.write(tmp_fd, fresh.encode("utf-8"))
            os.close(tmp_fd)
            tmp_fd = -1
            # restrict_to_owner, NOT os.chmod under `if IS_POSIX` — the raw call
            # is a silent no-op on Windows, leaving the id world-readable.
            with contextlib.suppress(OSError):
                platform_compat.restrict_to_owner(tmp_path)
            os.link(tmp_path, str(path))
            return fresh
        except FileExistsError:
            # Lost the race — adopt the winner's id.
            existing = _read_state(path)
            return existing if _valid_id(existing) else _IN_MEMORY_ID
        finally:
            if tmp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(tmp_fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
    except (OSError, RuntimeError, KeyError):
        # A read-only probe must never substitute the process-local fallback:
        # a caller inspecting state (``kirocrew telemetry status``) needs "the
        # persistent id or nothing", not an ephemeral stand-in.
        return _IN_MEMORY_ID if create else ""


def _today() -> str:
    """Today's UTC date, used ONLY for local send-once throttling.

    The statistical date is decided server-side from the log's own timestamp —
    a client clock is neither trustworthy nor timezone-consistent.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _stamp_path() -> Path:
    return config_dir() / STAMP_FILE


def already_sent_today() -> bool:
    """Return whether a beacon was already sent for today's UTC date."""
    return _read_state(_stamp_path()) == _today()


def is_first_send() -> bool:
    """Return whether this host has never successfully sent a beacon.

    Drives the ``first_seen`` bit, which yields the "installed but never used
    again" rate — the share of installs that ping exactly once and never
    return. One bit, so it adds no fingerprinting surface.

    Resolving the stamp path can itself fail (unwritable data home, or a
    container whose UID has no passwd entry so ``Path.home()`` raises), so treat
    an unreadable state as "first send" rather than letting it escape into a
    caller that documents itself as silent.
    """
    try:
        return not _stamp_path().exists()
    except (OSError, RuntimeError):
        return True


def _mark_sent() -> None:
    """Record today's date so later starts today skip the send."""
    try:
        path = _stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic_write, never path.write_text: write_text FOLLOWS a symlink, so a
        # symlink planted at beacon_last_sent would have its TARGET truncated and
        # overwritten with today's date. atomic_write renames a temp file over the
        # path, replacing the symlink itself and leaving the target untouched.
        atomic_write(path, _today())
    except OSError as exc:
        # Worst case the beacon is re-sent later today. Query-time
        # COUNT(DISTINCT) makes that harmless for correctness (this throttle is
        # politeness, not a correctness requirement), so never raise.
        logger.debug("beacon stamp write failed: %s", exc)


def _fields(app_version: str) -> dict[str, str]:
    """Build every payload field EXCEPT the install id.

    Split out so :func:`payload` and :func:`status`'s preview share one
    definition of the wire format: a status command that describes a different
    shape than the one actually sent is worse than no status command. The id is
    excluded because the two callers resolve it differently — ``payload`` mints
    one, ``status`` must not (``create=False``).

    FOUR fields, and adding a fifth is a privacy decision, not a plumbing one.
    ``chan``, ``os``, ``arch`` and ``gov`` were deliberately removed (see the
    module docstring): the test suite pins this exact key set, so a re-addition
    fails a test rather than shipping silently.
    """
    return {
        "v": release(app_version),
        "py": python_minor(),
        "dist": distribution(),
        "first_seen": "1" if is_first_send() else "0",
    }


def payload(app_version: str) -> dict[str, str]:
    """Build the exact five-key allowlist that goes on the wire.

    Every value is a random id, a low-cardinality platform constant, a coarse
    bucket, or a single bit. There is no caller-supplied field, so the payload
    shape is fixed here and cannot be widened from a call site.

    ``v`` is CLAMPED by :func:`release`, and the release channel, OS,
    architecture and governance posture are not sent at all. Together these send
    strictly LESS information than the nine-key version they replace, which in
    turn sent less than the raw ``__version__`` before it — every revision of
    this payload has been a narrowing.
    """
    return {"id": install_id(), **_fields(app_version)}


def is_governance_pinned_off(*, audit_tool: str = "") -> bool:
    """Return whether an enterprise ceiling pins ``capabilities.telemetry`` off.

    Pass ``audit_tool`` (a tool name) from an ENFORCEMENT call site to route the
    decision through the audited ``vet_and_audit`` seam, which writes a
    ``governance_decision`` SEL record for the grant or the denial. ``should_send``
    does this, so a suppressed heartbeat leaves a forensic record.

    It is deliberately NOT the default. This probe is also a pure READ from
    ``status()`` → ``GET /api/telemetry/beacon``, which the Privacy panel refetches;
    auditing there would append HMAC-chained SEL rows on mere inspection, at a
    multiple of the one decision per boot that actually governs anything. Same
    reasoning the channels gate uses to skip its ungoverned default-permit on a hot
    path (see ``governance.md`` → Audit disposition): audit the decision that
    *does* something, not the question.

    The Level-1 POLICY answer, resolved through the standard chokepoint helper so
    this decision comes from the same evaluator as every other governed surface.
    Public because the dashboard's privacy panel must distinguish "off because the
    user flipped the toggle" (flippable) from "off because an administrator pinned
    it" (not flippable, and a config write would be refused) — see
    :func:`is_env_opted_out` for the same reasoning applied to the env var.

    FAIL-CLOSED on an evaluation error, and audited. This reverses an earlier
    revision of this function, and the reasoning matters because the two
    dispositions look symmetric and are not:

    * The wrong-permit is an **egress on a fleet that explicitly forbade egress**.
      "It only loses a heartbeat" describes the wrong-DENY; the wrong-PERMIT
      breaks the exact promise the administrator was given, on a payload that
      leaves the machine. That asymmetry is what puts this with
      ``capabilities.theme_install`` / ``capabilities.publish``
      (``fail_closed=True``) rather than with the advisory probes.
    * ``fail_closed=True`` also makes ``governance_permits`` audit the degrade as
      a critical SEL event, so an operator can see that a ceiling stopped being
      evaluable — which is precisely the condition under which a silent
      degrade-to-permit would be indefensible.

    Two failure sources are distinguished, because conflating them produces a
    different bug in each direction:

    * A **degrade** (``rule == "default"``) means the evaluator could not answer.
      Treated as pinned — fail closed, per above.
    * A **profile-layer deny** means the evaluator answered, but from Level 2.
      NOT treated as a pin: ``resolve_active_scope`` returns a synthetic deny-all
      profile (``_deny_all_unloaded:…``) when the profile store is unprimed and
      another thread holds its non-blocking reload lock, so on a host with **no
      policy at all** that transient race would otherwise make the CLI, the 403,
      and ``governanceOverrideNote`` all blame an administrator who does not
      exist. It arrives as an ordinary ``Decision``, so no ``except`` can catch
      it. Level-2 profiles are also per-surface and narrow-only, while this probe
      is process-wide and carries no session — so a profile denial is not the
      question being asked either way.

    ``TestGovernancePin`` pins all three: a real policy pin, the transient
    profile race, and the fail-closed degrade.
    """
    try:
        if audit_tool:
            # The audited seam: evaluate + write the governance_decision SEL row
            # from ONE code path, so this chokepoint's audit shape cannot drift
            # from the messaging chokepoints that already use it.
            decision = vet_and_audit(
                "capabilities.telemetry",
                "",
                session_key="",
                tool_name=audit_tool,
                log_warning=False,
                fail_closed=True,
            )
        else:
            decision = governance_permits(
                "capabilities.telemetry", "", log_warning=False, fail_closed=True
            )
    except Exception:
        # governance_permits swallows its own errors, so reaching here means the
        # import or the call itself failed — the ceiling is unevaluable, which is
        # the same condition as a degrade. Fail closed for the same reason.
        logger.debug("telemetry governance probe failed; treating as pinned", exc_info=True)
        return True
    if getattr(decision, "permitted", True):
        return False
    # The fail-closed degrade above is identified by its own reason prefix, NOT by
    # rule="default" alone: ``_PERMIT_NOT_GOVERNED`` also carries that rule, so a
    # rule-only test would be ambiguous. A degrade means no level decided, which is
    # the unevaluable case — pinned, per the fail-closed rationale.
    if str(getattr(decision, "reason", "")).startswith(GOVERNANCE_ERROR_REASON):
        return True
    return getattr(decision, "layer", "") == "policy"


class Verdict(NamedTuple):
    """A send decision plus both renderings of its reason.

    ``code`` is the STABLE machine-readable discriminant (one of :data:`REASONS`);
    ``reason`` is the English operator-facing sentence for CLI output and logs.
    The dashboard renders the code through its own translation catalog, so the
    prose here never reaches a user's screen: a raw diagnostic string
    interpolated into the UI is untranslatable and reads as a developer leak.
    """

    ok: bool
    reason: str
    code: str


# Every code :func:`status` can report. The dashboard's Privacy panel owns one
# translated string per entry, so adding a code here without a catalog key leaves
# the UI with nothing to render. Routes that never reach that panel may use codes
# outside this tuple (``install_receipt.should_send`` returns ``"unofficial"``).
REASONS = (
    "ready",
    "env_opt_out",
    "governance",
    "disabled",
    "ci",
    "non_default_home",
    "awaiting_privacy_ack",
    "already_sent_today",
    "no_endpoint",
    "unreadable_home",
)


def telemetry_permitted(
    *, enabled: bool, acked: bool, audit_tool: str = ""
) -> Verdict:
    """Return the send verdict for ANY outbound telemetry from this install.

    The consent gate, factored out of :func:`should_send` so a SECOND outbound
    signal cannot drift from the first. ``apps/install_receipt.py`` calls this
    rather than re-deriving the ladder: a duplicated gate is the failure mode
    where a user flips the toggle off and one of two routes keeps sending,
    which is worse than either route existing at all.

    Ordered cheapest-and-most-authoritative first: an opted-out host must not
    even stat the data home.

    The governance check sits ABOVE the config flag deliberately. It is the one
    suppression the user cannot undo, so when a fleet is pinned off the reported
    reason must name the policy rather than whatever the local flag happens to
    say — an admin debugging a managed host needs to see "policy", not
    "beacon_enabled is false".

    ``acked`` is ``dashboard.privacy_acked``: whether the user has actually been
    SHOWN the disclosure and its opt-out (the mandatory first-run Privacy
    chapter, or an explicit ``kirocrew telemetry enable|disable``). It gates the
    FIRST egress only (see :func:`is_first_send`). Sending before the offer is
    made would reduce the opt-out to a formality: by the time the user could
    decline, the ping they were declining had already gone. It is checked LAST
    among the suppressions so a more specific and more actionable reason (a CI
    host, a pod's data home) still wins the reported ``code``; ordering cannot
    weaken the gate, because every check must pass to permit a send.

    Pass ``audit_tool`` from an ENFORCEMENT call site so the governance decision
    lands a ``governance_decision`` SEL record naming which control refused;
    leave it empty for a read-only diagnostic probe (see
    :func:`is_governance_pinned_off`).

    Deliberately does NOT include the beacon's once-per-day throttle. That
    throttle is specific to a heartbeat: an install receipt is a per-EVENT
    signal, so a shared day stamp would silently drop the second app a user
    installs on any given day.
    """
    if _env_truthy(DISABLE_ENV):
        return Verdict(False, f"opted out via {DISABLE_ENV}", "env_opt_out")
    if is_governance_pinned_off(audit_tool=audit_tool):
        return Verdict(
            False, "disabled by governance policy (capabilities.telemetry)", "governance"
        )
    if not enabled:
        return Verdict(False, "disabled (telemetry.beacon_enabled is false)", "disabled")
    if is_ci():
        return Verdict(False, "CI environment detected", "ci")
    if not is_default_home():
        return Verdict(
            False,
            "non-default KIROCREW_HOME (dev home / pod / preview)",
            "non_default_home",
        )
    # First-egress only: once this install has sent once, the disclosure is
    # behind the user and re-gating on the flag would silence an established
    # install whose config predates the field.
    if not acked and is_first_send():
        return Verdict(
            False,
            "the first-run privacy disclosure has not been shown yet",
            "awaiting_privacy_ack",
        )
    return Verdict(True, "ready", "ready")


def should_send(*, enabled: bool, acked: bool, audit: bool = True) -> Verdict:
    """Return the heartbeat's send verdict; the reason explains a skip.

    The shared consent ladder plus the heartbeat's OWN once-per-day throttle,
    which no other route wants (see :func:`telemetry_permitted`).

    ``audit`` distinguishes the ENFORCEMENT call (from :func:`send`, whose verdict
    decides whether a heartbeat leaves the machine — routed through the audited
    seam so it lands a ``governance_decision`` SEL record either way) from the
    read-only diagnostic call (from :func:`status`, which the Privacy panel
    refetches; auditing an inspection would flood the SEL trail).
    """
    verdict = telemetry_permitted(
        enabled=enabled, acked=acked, audit_tool="beacon_send" if audit else ""
    )
    if not verdict.ok:
        return verdict
    if already_sent_today():
        return Verdict(
            False, f"already sent today ({_today()})", "already_sent_today"
        )
    return Verdict(True, "ready", "ready")


def beacon_url(endpoint: str, fields: dict[str, str]) -> str:
    """Compose the beacon URL: id in the PATH, everything else in the query.

    Raises ``ValueError`` unless *endpoint* is https — an anonymous id is not a
    secret, but a plaintext heartbeat would still reveal which hosts run this
    software to any on-path observer.
    """
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme != "https":
        raise ValueError("beacon endpoint must be https://")
    ident = fields.get("id", "")
    if not _valid_id(ident):
        raise ValueError("refusing to send a malformed install id")
    base = endpoint.rstrip("/")
    query = urllib.parse.urlencode({k: v for k, v in fields.items() if k != "id" and v})
    return f"{base}/b/{BEACON_SCHEMA}/{ident}?{query}"


def send(endpoint: str, app_version: str, *, enabled: bool, acked: bool) -> bool:
    """Send at most one heartbeat for today. Returns whether one was sent.

    Fully best-effort and SILENT on failure: an offline user, a firewall, a DNS
    failure, or a 5xx must never surface as an error or a delay the user
    notices. Telemetry that can break the product is worse than no telemetry.

    ``acked`` is required rather than defaulted: this is the enforcement path for
    the first-egress privacy gate, and a default would let a future call site
    silently opt out of it (see :func:`telemetry_permitted`).
    """
    if not endpoint:
        return False
    try:
        # should_send + payload probe the filesystem (stamp file, data home), so
        # they belong INSIDE the guard: an unwritable data home raises
        # PermissionError from config_dir(), and a container with no passwd entry
        # for its UID makes Path.home() raise RuntimeError. Outside a handler
        # those propagate into the caller — for the gateway that means
        # threading.excepthook printing a traceback on every boot, and the
        # module's documented in-memory fallback never engaging.
        if not should_send(enabled=enabled, acked=acked).ok:
            return False
        url = beacon_url(endpoint, payload(app_version))
    except (ValueError, OSError, RuntimeError) as exc:
        logger.debug("beacon skipped (%s)", exc)
        return False
    try:
        req = urllib.request.Request(url, method="GET")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- beacon_url enforces https:// and the payload is a fixed five-key allowlist
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECS):
            pass
        # Stamp only after a delivered request, so a failed send retries later
        # rather than silently losing the day.
        _mark_sent()
        return True
    except (
        urllib.error.URLError,
        # http.client.HTTPException is NOT an OSError or ValueError subclass, so
        # it needs naming explicitly. An endpoint that passes the https:// check
        # but is still malformed (e.g. a space in the host) makes urlopen raise
        # http.client.InvalidURL; without this the exception escapes into the
        # detached daemon thread and threading.excepthook dumps a traceback to
        # gateway stderr on every boot — breaking this function's "silent on
        # failure" contract even though the gateway itself keeps running.
        http.client.HTTPException,
        OSError,
        TimeoutError,
        ValueError,
    ) as exc:
        logger.debug("beacon send failed (ignored): %s", exc)
        return False


def status(
    endpoint: str, *, enabled: bool, app_version: str, acked: bool
) -> dict[str, object]:
    """Return the exact state for ``kirocrew telemetry status``.

    Uses ``create=False`` so inspecting status never materializes an id.

    A diagnostic command must never traceback: on an unwritable data home the
    filesystem probes raise, and the whole point of `telemetry status` is to be
    readable exactly when something is wrong.

    ``audit=False``: this is a pure READ, reached from ``GET /api/telemetry/beacon``
    (which the Privacy panel refetches) and from ``kirocrew telemetry status``.
    Auditing here would write a governance SEL row per inspection; the enforcement
    call in ``send`` is the one that carries the audit.
    """
    try:
        ok, reason, code = should_send(enabled=enabled, acked=acked, audit=False)
    except (OSError, RuntimeError) as exc:
        ok, reason, code = (
            False,
            f"could not read the data home ({exc.__class__.__name__})",
            "unreadable_home",
        )
    # send() returns early on an empty endpoint, so reporting would_send=True
    # here would have the diagnostic contradict the code path it describes —
    # including after __post_init__ clears a non-https value, which is exactly
    # when an operator runs this command.
    if ok and not endpoint:
        ok, reason, code = (
            False,
            "no endpoint configured (telemetry.beacon_endpoint is empty)",
            "no_endpoint",
        )
    try:
        ident = install_id(create=False)
    except (OSError, RuntimeError):
        ident = ""
    # Shares _fields() with payload() so the preview cannot drift from what is
    # actually sent. The id is filled in separately from the create=False lookup
    # above, so merely inspecting status never materializes one.
    preview = {"id": ident or "(generated on first send)", **_fields(app_version)}
    # Probed separately from should_send()'s verdict so the CLI can say WHY the
    # local flag is irrelevant on a managed host: with a ceiling pinned off,
    # ``beacon_enabled: true`` is a stored value with no effect, and a status
    # command that showed only the flag would read as a contradiction.
    try:
        pinned = is_governance_pinned_off()
    except Exception:  # pragma: no cover - is_governance_pinned_off is itself guarded
        pinned = False
    return {
        "beacon_enabled": enabled,
        "endpoint_configured": bool(endpoint),
        "install_id": ident or "(not yet generated)",
        "would_send": ok,
        "reason": reason,
        "reason_code": code,
        "governance_pinned_off": pinned,
        "payload_preview": preview,
    }


def format_status(info: dict[str, object]) -> str:
    """Render :func:`status` as human-readable CLI output."""
    enabled = "yes" if info["beacon_enabled"] else "no"
    endpoint = "configured" if info["endpoint_configured"] else "not set"
    verdict = "will send" if info["would_send"] else "will NOT send"
    lines = [
        "Anonymous usage beacon (Daily Active Instances)",
        "",
        f"  Enabled:     {enabled}",
        f"  Endpoint:    {endpoint}",
        f"  Install ID:  {info['install_id']}",
        f"  Next start:  {verdict} — {info['reason']}",
    ]
    if info.get("governance_pinned_off"):
        lines += [
            "",
            "  Pinned OFF by your administrator's security policy",
            "  (capabilities.telemetry). The setting above cannot re-enable it.",
        ]
    lines += [
        "",
        "  Exactly these fields are sent, and nothing else:",
        f"    {json.dumps(info['payload_preview'], sort_keys=True)}",
        "",
        "  Never sent: prompts, model output, file contents, paths, repo names,",
        "  credentials, hostname, username, IP address, operating system, CPU",
        "  architecture, release channel, or governance posture.",
        "",
        "  To opt out, choose one:",
        "",
        "    1. Kiro Crew CLI (recommended)",
        "       kirocrew telemetry disable",
        "",
        "    2. Environment variable (choose your shell)",
        "       macOS / Linux",
        f"         export {DISABLE_ENV}=1",
        "       Windows PowerShell",
        f"         $env:{DISABLE_ENV} = '1'",
        "       Windows Command Prompt",
        f"         set {DISABLE_ENV}=1",
        "",
        "    3. Configuration file",
        "       Set telemetry.beacon_enabled to false",
    ]
    return "\n".join(lines)

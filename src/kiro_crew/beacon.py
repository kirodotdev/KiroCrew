"""Anonymous daily-heartbeat beacon — product analytics, stdlib-only.

Answers questions no local signal can: how many installations are actually
RUNNING (Daily Active Instances), which VERSIONS they run, which
OS/ARCH/Python they run on, which DISTRIBUTION CHANNEL they came from, what share
run under a governance ceiling (and how many of those verify its signature), and
what share of installs are launched only once. Download counts cannot answer
these — an install downloaded and never launched is indistinguishable from an
active one, and self-hosted distribution links have no download telemetry.

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
  * The payload is a fixed eight-key ALLOWLIST built by :func:`payload`. There
    is no free-form field and no caller-supplied pass-through, so no prompt,
    path, repo name, credential, or model output can reach the wire — not by
    accident and not via a future call site.
  * Every value is a low-cardinality constant or coarse bucket. ``py`` is
    minor-only (``3.12``, never ``3.12.13``) and ``first_seen`` is a single
    bit, specifically so the field set cannot become a fingerprint when
    combined.
  * The server persists NO client IP: the beacon distribution's log delivery
    selects only ``date``/``time``/``cs-uri-stem``/``cs-uri-query``/
    ``c-country``/``sc-status``. ``c-ip`` is never among the delivered fields,
    so it is not written to storage at all.

DEFAULT-ON, with three suppressions and a one-command opt-out. Off entirely
when: ``KIROCREW_TELEMETRY_DISABLED`` is truthy, ``telemetry.beacon_enabled``
is false, the process looks like CI, or ``KIROCREW_HOME`` is non-default (dev
homes, pods, worktree previews — one operator's own extra instances, which
would inflate the count).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import platform
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import CONFIG_DIR_LEAF, KIRO_BASE_DIR_NAME, config_dir

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

# Distribution channel, stamped at build time by the packaging scripts. An
# un-stamped build reports "source" (the git-clone path). Clamped to this fixed
# set so the field can never carry a free-form value from the environment.
DIST_ENV = "KIROCREW_DISTRIBUTION"
KNOWN_DISTRIBUTIONS = frozenset({"dmg", "appimage", "wheel", "source", "docker"})
DEFAULT_DISTRIBUTION = "source"

# Governance posture — a STATE, never an identity. Derived from the composed
# ceiling (see :func:`governance_posture`), not from the environment, so unlike
# ``dist`` there is nothing for a host to spoof. Four constants, so the field
# stays low-cardinality by construction.
POSTURE_NONE = "none"  # no Level-1 ceiling loaded
POSTURE_UNSIGNED = "unsigned"  # ceiling enforced, integrity rests on file modes
POSTURE_SIGNED = "signed"  # signature present but unproven / unchecked
POSTURE_VERIFIED = "verified"  # signature verified against a trust key
KNOWN_POSTURES = frozenset(
    {POSTURE_NONE, POSTURE_UNSIGNED, POSTURE_SIGNED, POSTURE_VERIFIED}
)

# Fallback id when the data home is unwritable (read-only container, etc).
# Process-local, so such a host contributes at most one count per process and
# never crashes the caller. Eager: trivial cost, removes a race.
_IN_MEMORY_ID: str = uuid.uuid4().hex


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ENV_TRUTHY


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


def distribution() -> str:
    """Return the build's distribution channel, clamped to the known set."""
    raw = (os.environ.get(DIST_ENV, "") or "").strip().lower()
    return raw if raw in KNOWN_DISTRIBUTIONS else DEFAULT_DISTRIBUTION


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
        return _IN_MEMORY_ID


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


def governance_posture() -> str:
    """Return this install's governance POSTURE, clamped to a fixed vocabulary.

    Answers a question ``dist`` cannot: what share of Daily Active Instances run
    under a Level-1 security ceiling at all, and of those, how many have a ceiling
    whose signature actually VERIFIES. Today a governed enterprise fleet and an
    ungoverned laptop are one undifferentiated number, so "is governance being
    adopted, and is it being adopted correctly" has no answer.

    Reports the POSTURE, never the identity. Deliberately NOT:

      * the active PROFILE NAME — profile names are free-form file stems
        (``governance_profiles`` derives them from ``path.stem``), so they are
        unbounded operator-authored strings: exactly the cardinality bomb the
        payload contract forbids, and they routinely encode an internal team,
        app, or surface name.
      * the ``identity.issuer`` — that names the ORGANIZATION that signed the
        ceiling. Correlated with a stable install id it de-anonymizes the whole
        row, which is the one thing this payload is built to prevent.

    So the values are four constants that describe a STATE, and an outside
    observer learns "some ceiling is present and verified", never whose:

      * ``none``      — no ceiling loaded (the default standalone posture)
      * ``unsigned``  — a ceiling is enforced, integrity rests on file permissions
      * ``signed``    — a ceiling carries a signature that did NOT verify (or was
                        never checked against a trust root) — advisory only
      * ``verified``  — a ceiling whose signature verified against a trust key

    Best-effort and import-cycle-safe: the context is read lazily inside the
    function (``platform.context`` is a leaf that must not import this module's
    dependents), and ANY failure reports ``none``. That fail direction is the
    honest one — an install whose posture cannot be established is not evidence
    of governance.
    """
    try:
        from kiro_crew.platform.context import current_context

        ceiling = getattr(current_context(), "governance", None)
        if ceiling is None:
            return POSTURE_NONE
        state = getattr(ceiling, "signature_state", "")
        if state == "verified":
            return POSTURE_VERIFIED
        if state == "unsigned":
            return POSTURE_UNSIGNED
        # "unverified" (present but unproven) and "unchecked" (parsed with no
        # trust root) collapse to one bucket: both mean "a signature exists but
        # nothing established it", and splitting them would report a distinction
        # that carries no adoption meaning.
        return POSTURE_SIGNED
    except Exception:  # pragma: no cover - defensive; posture is never load-bearing
        return POSTURE_NONE


def payload(app_version: str) -> dict[str, str]:
    """Build the exact eight-key allowlist that goes on the wire.

    Every value is a random id, a low-cardinality platform constant, or a
    single bit. There is no caller-supplied field, so the payload shape is
    fixed here and cannot be widened from a call site.
    """
    return {
        "id": install_id(),
        "v": app_version,
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "py": python_minor(),
        "dist": distribution(),
        "gov": governance_posture(),
        "first_seen": "1" if is_first_send() else "0",
    }


def should_send(*, enabled: bool) -> tuple[bool, str]:
    """Return ``(send, reason)``; *reason* explains a skip, for the CLI.

    Ordered cheapest-and-most-authoritative first: an opted-out host must not
    even stat the data home.
    """
    if _env_truthy(DISABLE_ENV):
        return False, f"opted out via {DISABLE_ENV}"
    if not enabled:
        return False, "disabled (telemetry.beacon_enabled is false)"
    if is_ci():
        return False, "CI environment detected"
    if not is_default_home():
        return False, "non-default KIROCREW_HOME (dev home / pod / preview)"
    if already_sent_today():
        return False, f"already sent today ({_today()})"
    return True, "ready"


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
    query = urllib.parse.urlencode(
        {k: v for k, v in fields.items() if k != "id" and v}
    )
    return f"{base}/b/{BEACON_SCHEMA}/{ident}?{query}"


def send(endpoint: str, app_version: str, *, enabled: bool) -> bool:
    """Send at most one heartbeat for today. Returns whether one was sent.

    Fully best-effort and SILENT on failure: an offline user, a firewall, a DNS
    failure, or a 5xx must never surface as an error or a delay the user
    notices. Telemetry that can break the product is worse than no telemetry.
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
        ok, _reason = should_send(enabled=enabled)
        if not ok:
            return False
        url = beacon_url(endpoint, payload(app_version))
    except (ValueError, OSError, RuntimeError) as exc:
        logger.debug("beacon skipped (%s)", exc)
        return False
    try:
        req = urllib.request.Request(url, method="GET")
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- beacon_url enforces https:// and the payload is a fixed eight-key allowlist
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


def status(endpoint: str, *, enabled: bool, app_version: str) -> dict[str, object]:
    """Return the exact state for ``kirocrew telemetry status``.

    Uses ``create=False`` so inspecting status never materializes an id.

    A diagnostic command must never traceback: on an unwritable data home the
    filesystem probes raise, and the whole point of `telemetry status` is to be
    readable exactly when something is wrong.
    """
    try:
        ok, reason = should_send(enabled=enabled)
    except (OSError, RuntimeError) as exc:
        ok, reason = False, f"could not read the data home ({exc.__class__.__name__})"
    # send() returns early on an empty endpoint, so reporting would_send=True
    # here would have the diagnostic contradict the code path it describes —
    # including after __post_init__ clears a non-https value, which is exactly
    # when an operator runs this command.
    if ok and not endpoint:
        ok, reason = False, "no endpoint configured (telemetry.beacon_endpoint is empty)"
    try:
        ident = install_id(create=False)
    except (OSError, RuntimeError):
        ident = ""
    preview = {
        "id": ident or "(generated on first send)",
        "v": app_version,
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "py": python_minor(),
        "dist": distribution(),
        "first_seen": "1" if is_first_send() else "0",
    }
    return {
        "beacon_enabled": enabled,
        "endpoint_configured": bool(endpoint),
        "install_id": ident or "(not yet generated)",
        "would_send": ok,
        "reason": reason,
        "payload_preview": preview,
    }


def format_status(info: dict[str, object]) -> str:
    """Render :func:`status` as human-readable CLI output."""
    enabled = "yes" if info["beacon_enabled"] else "no"
    endpoint = "configured" if info["endpoint_configured"] else "not set"
    verdict = "will send" if info["would_send"] else "will NOT send"
    return "\n".join(
        [
            "Anonymous usage beacon (Daily Active Instances)",
            "",
            f"  Enabled:     {enabled}",
            f"  Endpoint:    {endpoint}",
            f"  Install ID:  {info['install_id']}",
            f"  Next start:  {verdict} — {info['reason']}",
            "",
            "  Exactly these fields are sent, and nothing else:",
            f"    {json.dumps(info['payload_preview'], sort_keys=True)}",
            "",
            "  Never sent: prompts, model output, file contents, paths, repo",
            "  names, credentials, hostname, username, or IP address.",
            "",
            f"  To opt out:  export {DISABLE_ENV}=1",
            "               (or set telemetry.beacon_enabled false in config)",
        ]
    )

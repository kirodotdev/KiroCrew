"""The clamp bounds the loader and the dashboard's write gate share, and ``resource_limits``.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta
from kiro_crew.config.sections_agent import AgentConfig

logger = logging.getLogger("kiro_crew.config.loader")


# ---------------------------------------------------------------------------
# Security-relevant resource-limit ceilings
# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH for the upper bounds on the config knobs that govern
# host resource consumption. These same ceilings are enforced by the dashboard
# config API (``dashboard/handlers/core_config_view.py`` for the agent knobs,
# ``session.py`` for ``pool_size``); they live HERE so the API-write gate and
# the loader's load-time clamp cannot drift apart.
#
# Why the loader must also clamp: the
# REST API rejects out-of-range writes, but a direct edit of ``config.json``
# (any process running as the same OS user — including a prompt-injected agent
# with file-write access) bypassed that gate entirely. Each of these knobs
# controls a resource-consumption dimension — concurrent subagent processes
# (each a separate kiro-cli process), per-agent turn budget (unbounded LLM
# calls + context growth), and pre-warmed pool processes spawned at startup —
# so an inflated on-disk value can exhaust host memory / CPU / the process
# table (denial of service). Clamping at load time makes the on-disk value
# untrusted above range no matter which consumer reads it, and also means the
# GET /api/config/kirocrew response (which serializes a freshly loaded config)
# reports the clamped value rather than the tampered one.
SUBAGENT_AUTO_MAX_CEILING = 64  # agent.subagent_auto_max — concurrent subagent ceiling
SUBAGENT_MAX_TURNS_CEILING = 1000  # agent.subagent_max_turns — per-subagent turn budget
POOL_SIZE_MAX = 10  # session.pool_size — pre-warmed process pool

# agent.chat_turn_timeout_secs — wall-clock ceiling for one chat turn. The ACP
# transport's per-prompt wait follows this value (acp/client_timeouts.py
# ``resolve_prompt_timeout``, which adds a margin so the dashboard's visible
# card fires before the transport cut), so the max is not pinned to the
# transport's default. The default is 4h: the longest single turn the shipped
# budgets can legitimately produce is a 90-minute test command plus a fix and a
# re-run, or a blocking subagent wave at its 2h wait cap plus synthesis, and
# the watchdog's UNKNOWN-verdict windows must sit well inside the ceiling so its
# non-lethal recovery is reachable before the turn is cut. It is bounded at 24h
# because the ceiling is a runaway backstop, not a scheduler: a single
# prompt→response turn longer than a day is pathological, and multi-day
# unattended operation belongs to the loop mechanisms (monitor/goal loops,
# crons), which end the turn between cycles and survive restarts — a marathon
# turn does not. The floor keeps the backstop from being set so low it cuts
# ordinary work.
CHAT_TURN_TIMEOUT_MIN = 300
CHAT_TURN_TIMEOUT_MAX = 86400

# agent.session_start_timeout_secs — budget for ACP session/new + session/load
# on the shared runtime (acp/runtime_start.py ``_SESSION_NEW_TIMEOUT`` is the built-in
# default). kiro-cli blocks the session/new response while it initializes the
# session's MCP servers, so start time scales with the agent's server count and
# per-server cold-start cost (observed: a 71-server agent with no pending OAuth
# completes in ~14s; a 17-server agent behind a sandboxed per-server launcher on
# a loaded host takes ~50s). The floor IS the default: the budget must stay
# comfortably ABOVE the backend's 30s OAuth authorization wait —
# a lower value recreates the session-start race the dedicated budget exists to
# prevent, so out-of-range values clamp UP to it. The max bounds a typo'd
# value: a session start slower than 15 minutes is pathological and should
# surface as a timeout, not wait forever.
SESSION_START_TIMEOUT_MIN = 90
SESSION_START_TIMEOUT_MAX = 900

# agent.tool_approval_timeout_secs — how long a chat turn parks waiting for a
# human to answer a tool-approval prompt. The floor keeps the window long enough
# for a human who is actually present to reach the dashboard. The max is pinned
# at 7200 and deliberately DECOUPLED from CHAT_TURN_TIMEOUT_MAX (24h): the
# approval suites hold their own flat 2h runtime window
# (``DashboardState._APPROVAL_TIMEOUT``), so a larger configured window would
# pass validation here and then silently never be honoured at runtime. The
# binding limit below the static max is the cross-field clamp in
# ``_clamp_security_bounds``, which pulls the window APPROVAL_TURN_MARGIN_SECS
# under the configured turn ceiling.
TOOL_APPROVAL_TIMEOUT_MIN = 30
TOOL_APPROVAL_TIMEOUT_MAX = 7200

# The turn ceiling assumed when config omits ``agent.chat_turn_timeout_secs``.
# Read from the dataclass default so the two cannot drift apart.
_DEFAULT_CHAT_TURN_TIMEOUT_SECS = int(
    AgentConfig.__dataclass_fields__["chat_turn_timeout_secs"].default  # type: ignore[arg-type]
)

# Minimum slack between the approval window and the turn ceiling. Two things
# need it: the approval deadline must land inside the turn so its own "nobody
# approved, resend" card renders instead of the generic turn-timeout card, and a
# late approval must leave the turn some time to actually run the tool. A window
# flush against the ceiling satisfies neither.
APPROVAL_TURN_MARGIN_SECS = 60


# agent.max_subagents fixed-pin floor. 0 is the "auto-size" sentinel; any other
# (explicit) value must be >= this floor. A pin of 1 or 2 would silently DISABLE
# auto-sizing and run below today's default of 3, so such values are normalized
# UP to the floor at load time (see _clamp_security_bounds) and rejected by the
# dashboard API. Mirrors ``subagent._LEGACY_DEFAULT_MAX`` (kept as a local
# constant to avoid a config→subagent import cycle).
MAX_SUBAGENTS_FIXED_FLOOR = 3

# session.autocompact_pct — context-usage percentage at which the backend
# autocompactor fires. SINGLE SOURCE OF TRUTH for the documented 5-90 range:
# the dashboard config API (``dashboard/handlers/core_config_schema.py``) validates writes
# against these same constants, and the load read clamps a hand-edited
# config.json value into them, so the two ranges cannot drift as separate
# literals. The autocompactor is the backstop that keeps a session's context
# window from overflowing — above the ceiling the trigger
# (``pct >= autocompact_pct``) never fires before the window overflows, and
# at/below zero it fires on every turn. Floats are outside the int-only
# ``_SECURITY_BOUNDED_FIELDS`` sweep, so the clamp lives on the ``_safe_float``
# read instead.
AUTOCOMPACT_PCT_MIN = 5.0
AUTOCOMPACT_PCT_MAX = 90.0


class AutocompactPctError(ValueError):
    """A per-session threshold refused by :func:`parse_autocompact_pct`.

    ``code`` is ``pct_not_finite`` or ``pct_out_of_range``, the codes both
    routes that set a session's override answer with.
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def parse_autocompact_pct(value: int | float, *, name: str = "pct") -> float:
    """``value`` as a per-session threshold, or :class:`AutocompactPctError`.

    The one finite and range check shared by the slot's "Auto-compact at" route
    and ``session_create``. The caller has already refused a non-number (and a
    bool) with its own code. Refused rather than clamped: a caller that asked
    for 95 learns it instead of silently running at 90.
    """
    try:
        pct = float(value)
    except OverflowError:
        # An int too large for a float; out of range by definition.
        raise AutocompactPctError(f"{name} must be a finite number", "pct_not_finite") from None
    if pct != pct:  # NaN fails every comparison below
        raise AutocompactPctError(f"{name} must be a finite number", "pct_not_finite")
    if not (AUTOCOMPACT_PCT_MIN <= pct <= AUTOCOMPACT_PCT_MAX):
        raise AutocompactPctError(
            f"{name} must be between {AUTOCOMPACT_PCT_MIN:g} and {AUTOCOMPACT_PCT_MAX:g}",
            "pct_out_of_range",
        )
    return pct


# ``session.compact_wait_secs``: 0 is the sentinel for "use the built-in
# budget"; any positive value is lifted to at least ``COMPACT_WAIT_SECS_MIN``
# and capped at ``COMPACT_WAIT_SECS_MAX`` so a hand-edited typo cannot arm a
# near-zero budget (which would restart every compaction) or an unbounded
# wait. The load path applies the sentinel-preserving floor
# (``value if value == 0 else max(value, MIN)``); the resolver treats <= 0 as
# unset and falls back to the built-in default.
COMPACT_WAIT_SECS_MIN = 60.0
COMPACT_WAIT_SECS_MAX = 3600.0

# ── Load/write bound parity ────────────────────────────────────────────────────
# Ranges for bounded numeric fields the LOAD path clamps, while `_EDITABLE_CONFIG`
# rejects the same values at write time. A hand-edited config.json goes nowhere
# near the dashboard API, so without this every one of these would load verbatim --
# the same load/write asymmetry the security-relevant knobs also close.
#
# Defined HERE and imported by `_EDITABLE_CONFIG` rather than spelled twice, so
# the write gate and the load clamp cannot drift. Three fields already clamped on
# load but duplicated their literals across the two files; those now read from
# these names too, which is the "two-literal drift" half of the same problem.
#
# Bounds are the ones the write path already declared. This change does not
# re-litigate any range; it makes the load path honour what the API promised.
COMPLETION_KEEP_CHARS_MIN = 0
# Mirrors ``context_management.RESULT_FILE_MAX_BYTES`` (500 KB) rather than importing
# it: ``context_management`` does ``from kiro_crew.config.loader import config_dir``, so
# importing it here is a genuine circular import, not a style preference. The value is
# therefore spelled in both places and pinned equal by
# ``test_the_completion_keep_ceiling_matches_its_owner`` -- a test can import both
# without the cycle, which is the only place the two spellings can be held together.
COMPLETION_KEEP_CHARS_MAX = 512_000
# ``dashboard.upload_max_mb``: the per-file ceiling for chat-composer and
# Knowledge uploads. The ceiling bounds memory: a non-media chat upload is
# buffered whole while its signature is checked. MAX must not exceed the video
# ceiling (``handlers.files._MAX_VIDEO_UPLOAD_BYTES``, 512 MB): both composers
# exempt video from their pre-check on the premise that video's cap is the
# higher one. Pinned by ``test_video_ceiling_stays_above_the_document_cap``.
UPLOAD_MAX_MB_MIN = 1
UPLOAD_MAX_MB_MAX = 512
MCP_PROBE_TIMEOUT_MIN = 5
MCP_PROBE_TIMEOUT_MAX = 120
# ``dashboard.title_refresh_every_turns``: 0 is "built-in schedule"; any other
# value is at least MIN, so a typo of 1 cannot spend an LLM call on every turn.
TITLE_REFRESH_EVERY_TURNS_MIN = 4
TITLE_REFRESH_EVERY_TURNS_MAX = 1000
RECENT_TINT_COUNT_MIN = 0
RECENT_TINT_COUNT_MAX = 10
# The sidebar's folder sort modes, spelled once for the same reason as the bounds
# above: the loader normalizes to this set, the PATCH allowlist accepts exactly
# it, and the ``kirocrew-dashboard`` MCP server reads the stored value back
# through it. ``custom`` is the stored ``order`` positions (today's behaviour and
# the default), ``name`` an ASCII-case-insensitive natural order, ``created`` newest
# first. The frontend's ``readFolderSortMode`` mirrors this list.
FOLDER_SORT_MODES: tuple[str, ...] = ("custom", "name", "created")
FOLDER_SORT_DEFAULT = "custom"
# The sidebar's session sort keys: last activity, creation time or title, each
# both ways, and ``manual``, the person's own drag order (kept by
# ``dashboard.pinned_session_order``). The loader normalizes to this set and the
# PATCH allowlist accepts exactly it; the frontend's ``SortKey`` mirrors this
# list. A build without ``manual`` reads it as the default, ``date-desc``.
SESSION_SORT_MODES: tuple[str, ...] = (
    "date-desc",
    "date-asc",
    "created-desc",
    "created-asc",
    "name-asc",
    "name-desc",
    "manual",
)
SESSION_TIMEOUT_MIN = 0
SESSION_TIMEOUT_MAX = 86400
POOL_TTL_SECS_MIN = 0
POOL_TTL_SECS_MAX = 7200
SOFT_STOP_BUDGET_MIN = 0.5
SOFT_STOP_BUDGET_MAX = 60.0
EXTRACTION_POOL_SIZE_MIN = 1
EXTRACTION_POOL_SIZE_MAX = 10
# Load-only bounds. Unlike the parity block above these are NOT consumed by
# `_EDITABLE_CONFIG` — the field is config-file-only (no dashboard write path),
# so the only clamp site is the loader. Kept out of the shared block so its
# "every bound is shared with the write gate" claim stays true.
EMPTY_RESPONSE_MAX_CONTINUES_MIN = 1
EMPTY_RESPONSE_MAX_CONTINUES_MAX = 10
# Also load-only, and a floor only: 0 keeps every turn's copies, so a negative
# value reads as 0, and any larger count is a real choice.
FILE_CHANGES_KEEP_TURNS_MIN = 0
# A hand-edited ``session.idle_exempt_keys`` can be any size, and every idle sweep copies it.
# Each listed key can also keep a whole kiro-cli process and its MCP servers running, the
# per-entry cost ``POOL_SIZE_MAX`` bounds for the warm pool, so the count shares that ceiling.
IDLE_EXEMPT_KEYS_MAX = POOL_SIZE_MAX
IDLE_EXEMPT_KEY_MAX_CHARS = 512
# Ceiling on ``session.reconcile_max_kills``, equal to the arm's own shipped budget
# (``runtime_reconcile.DEFAULT_MAX_KILLS``, pinned equal by
# ``test_the_configured_ceiling_cannot_exceed_the_shipped_budget``). Equal rather
# than higher is what makes the field subtractive: every reachable value is at or
# below what the product already does, so a write to this agent-writable file can
# withhold signals and cannot authorize one the arm would not already send.
#
# 0 is meaningful (observe-only) and is the floor, so a negative clamps DOWN to it
# and disables the arm rather than enabling it.
RECONCILE_MAX_KILLS_MAX = 5
# knowledge.* budgets. These share a floor of 0, but 0 is MEANINGFUL for several
# of them (a zero budget disables that sweep), so the floor is deliberately not
# enforced by clamping a negative up to 0 -- see `_safe_nonnegative_int`, which
# keeps returning the default for a negative value. Only the missing CEILING is
# added here, which is where the actual exposure was: an absurd hand-edited
# budget was loaded verbatim and became real work.
FOLDER_INGEST_CHUNK_BUDGET_MAX = 10000
DEDUP_EVERY_N_SWEEPS_MAX = 288
SWEEP_CHUNK_BUDGET_MAX = 50000
EMBED_RATE_LIMIT_MAX = 10000
# Ceiling on the explicit-import cross-file chunk budget, same rationale as the
# folder/sweep budgets above: clamp a negative to 0 (0 == unbounded) and cap an
# absurd hand-edited value so it cannot load verbatim into real work.
IMPORT_CHUNK_BUDGET_MAX = 50000

# Keys whose out-of-domain value has already been reported, so a knob read once
# per spawn warns once per process instead of once per agent launch. Same shape
# as ``_OBSERVED_DEGRADED_SECTIONS``; exposed for tests to reset.
_WARNED_RESOURCE_LIMIT_KEYS: set[str] = set()


def _limit_int(value: object, key: str, *, lo: int, hi: int | None = None) -> int | None:
    """Coerce one ``resource_limits`` value, or ``None`` when it is out of domain.

    ``None`` means "no usable value here" and is deliberately NOT a number: each
    mechanism's fallback is its own documented default (``_RLIMIT_DEFAULTS`` for
    the rlimit path, ``_CGROUP_DEFAULT_*`` for the cgroup paths), and those must
    stay where they are rather than being copied into this dataclass as a third
    default set.

    The coercion rules, and why each one is what it is:

    - ``bool`` is not a number here. ``True`` would otherwise coerce to ``1`` and
      set a one-process / one-MB ceiling, which kills the child it limits.
    - A non-integral float TRUNCATES toward zero (``512.5`` -> ``512``), matching
      what every pre-existing reader did, so tightening the parse cannot loosen
      an operator's ceiling.
    - EXCEPT when it truncates to ``0``, either sign: ``0.5`` is not a request to
      disable the limit, but ``int(0.5)`` is exactly the value that means
      "disabled" on the rlimit path and "use the default" on the cgroup path.
      That silent reinterpretation is the trap, so it is refused.
    - NaN and +/-Infinity are refused before ``int()`` sees them. ``json.loads``
      accepts both literals, and ``int(inf)`` raises ``OverflowError`` --
      uncaught on the rlimit path, which turned a typo into a failure of every
      spawn.
    - Out of range REFUSES rather than clamps, and is checked on the value AS
      WRITTEN rather than on the truncated result. A clamp would silently move a
      confinement ceiling away from the number the operator can read in their own
      file; checking after truncation would let a value below the floor land back
      inside it (``int(-0.5) == 0`` passes a ``>= 0`` floor and then reads as
      "leave inherited", removing the ceiling entirely).

    Every refusal is logged once per key per process: the value is security
    relevant, so an operator must not have to infer it was dropped.
    """

    def _refuse(reason: str) -> None:
        if key in _WARNED_RESOURCE_LIMIT_KEYS:
            return
        _WARNED_RESOURCE_LIMIT_KEYS.add(key)
        logger.warning(
            "config: resource_limits.%s = %r %s — ignoring it and using the "
            "documented default for that mechanism",
            key,
            value,
            reason,
        )

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("is not a number")
        return None
    if isinstance(value, float) and not math.isfinite(value):
        _refuse("is not a finite number")
        return None
    # Range-check the value AS WRITTEN, before any truncation. Checking the
    # truncated result instead lets a value BELOW the floor land back inside it:
    # ``int(-0.5) == 0`` satisfies a ``>= 0`` floor and then reads as this
    # block's "leave inherited" sentinel, REMOVING the ceiling the operator was
    # trying to set.
    if value < lo or (hi is not None and value > hi):
        _refuse(f"is outside the accepted range [{lo}, {hi if hi is not None else 'unbounded'}]")
        return None
    if isinstance(value, float) and not value.is_integer():
        # A fraction that truncates to zero is refused whatever its sign. Zero
        # is meaningful to every consumer of this block -- "leave inherited",
        # "use the default", "disabled" -- so truncating would silently swap the
        # operator's request for one of those.
        if int(value) == 0:
            _refuse("is a fraction that would truncate to 0, which means something else")
            return None
        logger.debug("config: resource_limits.%s = %r truncated to %d", key, value, int(value))
    return int(value)


@dataclass
class ResourceLimitsConfig:
    """Kernel confinement ceilings for spawned agent processes.

    THREE mechanisms read this one block, and a key shared between two of them
    does NOT mean the same thing on both. That is the whole reason this section
    has a schema: without it every consumer parses the raw dict itself, leaving
    the incompatible domains written down nowhere and free to drift apart.

    - ``POSIX rlimits`` (``security.apply_resource_limits``, via ``preexec_fn``
      or the exec shim's ``--rlimits=``). Here ``0`` is a MEANINGFUL, documented
      value: "leave the inherited limit unchanged". Absent falls back to
      ``security._RLIMIT_DEFAULTS``.
    - ``cgroup v2 scope`` (``sandbox.cgroup_scope_argv``, ``TasksMax`` /
      ``MemoryMax`` / ``CPUWeight`` on a transient ``systemd-run --user
      --scope``). Here ``0`` is ILLEGAL -- systemd rejects the property and the
      scope never starts -- so ``0``, absent, or anything out of domain falls
      back to the module default and the ceiling is never left unset. The one
      exception is ``max_cpu_percent``, which is opt-in: unset emits no
      ``CPUQuota`` property at all.
    - ``pytest-xdist worker cap`` (``resource_status``), where ``xdist_auto_cap``
      carries its own three-way sentinel.

    Every field is ``int | None``, and ``None`` means "not configured" -- kept
    distinct from ``0`` precisely because ``0`` is a real value on the rlimit
    path. Values are coerced by :func:`_limit_int`, the ONLY parse site for this
    block; a second one is a defect, and ``test_resource_limits_schema.py``
    fails if one appears.
    """

    max_open_files: int | None = field(
        default=None,
        metadata=_meta(
            "Max open files",
            "RLIMIT_NOFILE: open file descriptors per spawned process. Caps fd "
            "leaks. 0 leaves the inherited limit unchanged; unset uses the "
            "built-in default (1024). Not used by the cgroup path.",
            nullable=True,
        ),
    )
    max_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max processes",
            "READ BY TWO MECHANISMS with different meanings for 0. As "
            "RLIMIT_NPROC it caps processes for the child's real UID, and 0 "
            "leaves the inherited limit unchanged (the default -- see the "
            "per-UID caveat in security._RLIMIT_DEFAULTS). As the cgroup "
            "TasksMax it counts TASKS (threads) in the scope, where 0 is "
            "rejected by systemd, so 0 or unset means the module default.",
            nullable=True,
        ),
    )
    max_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max memory (MB)",
            "READ BY TWO MECHANISMS with different meanings for 0. As RLIMIT_AS "
            "it caps virtual address space, and 0 leaves the inherited limit "
            "unchanged (the default -- Node/V8 reserve huge VSZ, see the caveat "
            "in security._RLIMIT_DEFAULTS). As the cgroup MemoryMax it is the "
            "per-scope resident ceiling, where 0 is rejected by systemd, so 0 "
            "or unset means the host-proportional module default.",
            nullable=True,
        ),
    )
    max_cpu_seconds: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU seconds",
            "RLIMIT_CPU: CPU-seconds per spawned process. 0 leaves the "
            "inherited limit unchanged (the default). Not used by the cgroup "
            "path, which throttles with CPUWeight/CPUQuota instead of killing.",
            nullable=True,
        ),
    )
    cpu_weight: int | None = field(
        default=None,
        metadata=_meta(
            "CPU weight",
            "cgroup CPUWeight for the agent scope: relative CPU share under "
            "contention, not a cap. Accepted range 1-10000; unset or out of "
            "range uses the module default. Emitted only when the cpu "
            "controller is delegated to the user manager.",
            nullable=True,
        ),
    )
    max_cpu_percent: int | None = field(
        default=None,
        metadata=_meta(
            "Max CPU percent",
            "cgroup CPUQuota: a HARD CPU cap, opt-in. Unset or 0 emits no "
            "CPUQuota property at all, because a hard cap slows legitimate "
            "builds. May exceed 100 on a multi-core host (150 = 1.5 cores).",
            nullable=True,
        ),
    )
    max_total_memory_mb: int | None = field(
        default=None,
        metadata=_meta(
            "Max total memory (MB)",
            "cgroup MemoryMax for the whole agents SLICE -- how much every "
            "agent tree may claim together, independent of the per-scope "
            "ceiling. 0 or unset uses the host-proportional module default; "
            "the aggregate ceiling is never left unset where a systemd user "
            "manager is available (Linux); not enforced on Windows/macOS.",
            nullable=True,
        ),
    )
    max_total_processes: int | None = field(
        default=None,
        metadata=_meta(
            "Max total processes",
            "cgroup TasksMax for the whole agents SLICE, counting tasks "
            "(threads) across every agent tree. 0 or unset uses the module "
            "default; the aggregate ceiling is never left unset where a "
            "systemd user manager is available (Linux); not enforced on "
            "Windows/macOS.",
            nullable=True,
        ),
    )
    xdist_auto_cap: int | None = field(
        default=None,
        metadata=_meta(
            "pytest-xdist worker cap",
            "Ceiling for auto-computed pytest-xdist worker counts. -1 (the "
            "default) computes it from available memory, 0 disables the "
            "injection entirely and defers to xdist, and N > 0 pins a fixed "
            "cap.",
            nullable=True,
        ),
    )

    @classmethod
    def from_raw(cls, section: object) -> "ResourceLimitsConfig":
        """Build from a raw ``resource_limits`` dict -- the ONE parse site.

        Accepts whatever ``json.loads`` produced, including ``None`` and a
        non-dict, because the callers are spawn-path readers that must never
        raise: a malformed config has to degrade to defaults, not stop the agent
        from starting. Consumers keep their own interpretation of ``0`` and of
        ``None``; this method only decides what is a usable integer.
        """
        if not isinstance(section, dict):
            return cls()
        return cls(
            max_open_files=_limit_int(section.get("max_open_files"), "max_open_files", lo=0),
            max_processes=_limit_int(section.get("max_processes"), "max_processes", lo=0),
            max_memory_mb=_limit_int(section.get("max_memory_mb"), "max_memory_mb", lo=0),
            max_cpu_seconds=_limit_int(section.get("max_cpu_seconds"), "max_cpu_seconds", lo=0),
            cpu_weight=_limit_int(section.get("cpu_weight"), "cpu_weight", lo=1, hi=10000),
            max_cpu_percent=_limit_int(section.get("max_cpu_percent"), "max_cpu_percent", lo=0),
            max_total_memory_mb=_limit_int(
                section.get("max_total_memory_mb"), "max_total_memory_mb", lo=0
            ),
            max_total_processes=_limit_int(
                section.get("max_total_processes"), "max_total_processes", lo=0
            ),
            xdist_auto_cap=_limit_int(section.get("xdist_auto_cap"), "xdist_auto_cap", lo=-1),
        )

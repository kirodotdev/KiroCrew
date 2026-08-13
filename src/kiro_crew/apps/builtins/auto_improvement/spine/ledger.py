"""Finding ledger — the cross-restart de-duplication guarantee (spine).

The content-fingerprint dedup store: every candidate the loop discovers is
reduced to a stable FINGERPRINT, and the ledger persists every fingerprint we
have ever acted on (filed a CR, discarded as noise, failed the gate, ...) so the
loop NEVER re-files or re-investigates the same issue across cycles or restarts.
State is an append-only JSONL file on disk — crash-safe and human-greppable.

Ported from ``planning/kiro_crew/auto_improvement/autoloop/ledger.py`` and kept
**target-agnostic**: a fingerprint is keyed on ``(kind, code locus)`` ONLY, where
``kind`` is the only profile-relevant input (perf/bug). The locus string format is
opaque to the ledger — the spine never parses target paths.

Docs: 02_architecture.md §3.5 / §6.8 (dedup ledger is SPINE durable state),
06_cr_generation_and_dedup.md (fingerprint inputs), 08_safety_isolation §6.3,
10_implementation_roadmap.md M0 ("ledger — lifted from autoloop/ledger.py") + M5.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# The outcome statuses a finding can land on. Mirrors the source ledger so the
# morning-collection step (``grep '"status": "filed"'``) is unchanged (M5).
STATUS_SEEN = "seen"
STATUS_FILED = "filed"
# F10 direct-commit mode: a verified change PUSHED straight to the operator-authorized
# feature branch instead of filed as a CR (the operator opted in per-project; never a
# protected branch). A push is a decision — hard-terminal, never re-committed — exactly
# like ``filed``; it carries the resulting sha in the ledger ``note``/``cr`` field.
STATUS_COMMITTED = "committed"
STATUS_DISCARDED_NOISE = "discarded_noise"  # PERF-track only (a delta inside the band)
STATUS_FAILED_GATE = "failed_gate"
STATUS_FAILED_VERIFY = "failed_verify"
# The operator DELETED a finding from the Changes screen (data_store.purge_finding appends
# this). Unlike a gate verdict, a purge is an explicit "forget this so the agent can
# re-discover it" — so it must make the locus RETRYABLE (NOT known), not block it like a
# hard-terminal status. read_findings already drops a purged row from the UI; known() must
# treat it as unknown so the next discovery cycle can surface the locus again.
STATUS_PURGED = "purged"
STATUS_DUPLICATE = "duplicate"  # bug-track dedup skip (05_*.md §5.3); never re-filed
STATUS_ERROR = "error"  # a REAL tooling/harness failure (exception, crash, timeout)
# An HONEST "investigated this surface, found no real defect" (the agent reproduced
# nothing → left no diff). Distinct from ``error`` so it does NOT pollute the error
# stats AND so it is treated as transient (a surface with no defect today may grow one
# as the code evolves) — see SOFT_TERMINAL below. This is the dominant outcome for the
# bug track's speculative failure-surface seeds, which often have no actual defect.
STATUS_NO_DEFECT = "no_defect"

VALID_STATUSES = frozenset(
    {
        STATUS_SEEN,
        STATUS_FILED,
        STATUS_COMMITTED,
        STATUS_DISCARDED_NOISE,
        STATUS_FAILED_GATE,
        STATUS_FAILED_VERIFY,
        STATUS_DUPLICATE,
        STATUS_ERROR,
        STATUS_NO_DEFECT,
        STATUS_PURGED,
    }
)

# Soft-terminal statuses block re-discovery only for a COOLDOWN window, then the locus
# becomes retryable again. Rationale: an honest "no defect found", a transient tooling
# error, or a fix ATTEMPT that didn't verify must NOT permanently poison the ledger (the
# bug bringing this in: 3 speculative seeds recorded as ``error`` blocked the loop forever,
# so it idled with "0 fresh"; and a real defect whose FIRST fix attempt failed verification
# was never retried even though a different fix might pass — observed 2026-06-17: a scoped
# run re-discovered 5 real surfaces, all deduped as terminal, kept=0 filed=0).
#   * ``error`` / ``no_defect`` — transient miss; retry after cooldown.
#   * ``failed_verify`` — the reproducing test was written but THIS fix attempt didn't make
#     it pass; that's "this attempt didn't work", NOT "there's no bug here", so a later
#     cycle (different fix) should retry it after the cooldown.
# A real gate DECISION (filed / committed / failed_gate / discarded_noise / duplicate)
# stays hard-terminal — re-filing/re-deciding it churns. ``failed_gate`` differs from
# ``failed_verify``: the candidate's reproducing test could not even be made to GO RED (the
# premise was wrong), so retrying the same surface is unproductive.
SOFT_TERMINAL = frozenset({STATUS_ERROR, STATUS_NO_DEFECT, STATUS_FAILED_VERIFY})

# How long a soft-terminal locus stays deduped before it is retryable. Long enough that
# a steady-cadence run still QUIESCES (cycles are seconds apart; this is hours), short
# enough that an overnight/multi-day run re-examines evolving surfaces. Overridable.
DEFAULT_RETRY_COOLDOWN_S = 86400.0  # 24h


# Bug-track gate reason → shared ledger status mapping (05_improvement_loop_bugfix.md
# §5.3). The bug track folds its granular reasons onto the SAME outcome vocabulary the
# perf track uses, so one ledger serves both tracks (distinguished by ``kind``).
# ``discarded_noise`` is deliberately ABSENT — it is a perf-track outcome for a candidate
# inside the noise band; the bug track has no band (§5.3 note), so a bug candidate is
# never "discarded as noise". It either passes the boolean gate, fails it, or is a dup.
def map_bug_reason_to_status(reason: str) -> str:
    """Map a :mod:`.contracts` ``BUG_*`` gate reason onto a ledger status (§5.3).

    static triage (build/lint/collect) → ``failed_gate``;
    RED/GREEN/STAYGREEN/flake          → ``failed_verify``;
    harness/tooling error              → ``error``;
    accepted (RED ∧ GREEN ∧ STAYGREEN) → ``filed``.
    """
    from .contracts import (
        BUG_ERROR,
        BUG_FAILED_BUILD,
        BUG_FAILED_LINT,
        BUG_FILED,
        BUG_NOT_GREEN,
        BUG_NOT_RED,
        BUG_REGRESSED,
        BUG_TEST_FLAKY,
        BUG_TEST_INVALID,
    )

    if reason == BUG_FILED:
        return STATUS_FILED
    if reason in (BUG_FAILED_BUILD, BUG_FAILED_LINT, BUG_TEST_INVALID):
        return STATUS_FAILED_GATE
    if reason in (BUG_NOT_RED, BUG_TEST_FLAKY, BUG_NOT_GREEN, BUG_REGRESSED):
        return STATUS_FAILED_VERIFY
    if reason == BUG_ERROR:
        return STATUS_ERROR
    return STATUS_ERROR  # unknown reason → conservative error bucket


def map_perf_discard_to_status(reason: str) -> str:
    """Map a :mod:`.keeper` perf ``DISCARD_*`` reason onto a ledger status.

    Only a delta genuinely inside the noise band is ``discarded_noise`` (hard-terminal —
    the lever was measured and is not a win). The OTHER discard reasons are NOT noise and
    must not be filed as such:
      - guardrail / tests / RH-capability / RH-functional failures are VERIFICATION
        failures → ``failed_verify`` (the change measured as a win but failed a safety
        check; a future re-measure or a different fix may pass);
      - a measurement error is a harness failure → ``error`` (SOFT_TERMINAL → retryable).

    Before this mapping the driver hard-coded EVERY non-kept survivor to ``discarded_noise``,
    demoting the real reason to a note AND making a transient RH-probe failure permanently
    dedup-blocked (``discarded_noise`` is hard-terminal). Found by the app's own discovery.
    """
    from .keeper import (
        DISCARD_GUARDRAIL,
        DISCARD_MEASURE_ERROR,
        DISCARD_NOISE,
        DISCARD_RH_CAPABILITY,
        DISCARD_RH_FUNCTIONAL,
        DISCARD_TESTS,
    )

    if reason == DISCARD_NOISE:
        return STATUS_DISCARDED_NOISE
    if reason == DISCARD_MEASURE_ERROR:
        return STATUS_ERROR
    # keeper.evaluate_one emits the guardrail reason SUFFIXED with the offending metric
    # (``f"{DISCARD_GUARDRAIL}_{metric}"`` — e.g. ``discard_guardrail_rss``), so match by
    # prefix; the other verification reasons are emitted bare and match by equality.
    if (
        reason == DISCARD_GUARDRAIL
        or reason.startswith(f"{DISCARD_GUARDRAIL}_")
        or reason in (DISCARD_TESTS, DISCARD_RH_CAPABILITY, DISCARD_RH_FUNCTIONAL)
    ):
        return STATUS_FAILED_VERIFY
    return STATUS_DISCARDED_NOISE  # unknown discard reason → conservative (hard-terminal)


def _norm(s: str) -> str:
    """Normalize free text for fingerprinting: lowercase, collapse non-alnum."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fingerprint(*, kind: str, target: str, signature: str = "") -> str:
    """Stable id for a finding — keyed on ``(kind, code locus)`` ONLY.

    Args:
        kind: ``"perf"`` | ``"bug"`` | ... — perf and bug findings on the same
            locus dedup independently so both can be filed (02_architecture §6.9).
        target: the code locus — an opaque ``"<path>::<symbol>"`` string supplied
            by the profile's discovery step. Reduced to ``basename + symbol`` so
            the same locus fingerprints identically across clones/branches.
        signature: IGNORED for the fingerprint (kept in the API for caller
            metadata). Deliberately excluded so reworded re-discoveries of the
            same symbol collide and are skipped (autoloop/ledger.py rationale;
            06_cr_generation_and_dedup.md).

    The fingerprint is intentionally coarse: better to occasionally skip a real
    variant on the same symbol than to spam duplicate CRs overnight.
    """
    tgt = _norm(target.split("/")[-1])  # basename + symbol — clone/branch independent
    raw = f"{kind}|{tgt}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class LedgerEntry:
    """One recorded outcome for a finding fingerprint."""

    fp: str
    kind: str
    target: str
    status: str  # one of VALID_STATUSES
    cr: str = ""  # CR id once filed
    note: str = ""
    ts: float = 0.0


class Ledger:
    """Append-only JSONL ledger of every finding fingerprint we've acted on.

    Reloads from disk on construction so the dedup guarantee survives a driver
    restart (04_improvement_loop, 08_safety §6.3). Thread-safe for the append.
    """

    def __init__(self, path: Path, *, retry_cooldown_s: float = DEFAULT_RETRY_COOLDOWN_S):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen: dict[str, LedgerEntry] = {}
        # Soft-terminal loci (error / no_defect) become retryable after this window so a
        # transient miss never permanently poisons the ledger. 0 → retry immediately
        # (never dedup a soft-terminal); a huge value → effectively hard-terminal.
        self.retry_cooldown_s = float(retry_cooldown_s)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                self._seen[d["fp"]] = LedgerEntry(**d)
            except Exception:  # noqa: BLE001
                continue  # tolerate a torn final line (crash mid-append)

    def known(self, fp: str, *, now: float | None = None) -> bool:
        """True if this fingerprint is currently deduped (skip re-discovery).

        Three tiers:
        - ``seen`` (discovered + in progress): NOT known — if a run was interrupted
          before a terminal status (clean stop, crash, missing agent runner) the locus
          stays retryable, so a stop mid-fan_out never poisons the ledger.
        - HARD-terminal (filed / discarded_noise / failed_gate / failed_verify /
          duplicate): always known — a gate VERDICT is a decision; a filed CR is never
          re-filed, a failed_gate is never retried in the same form.
        - SOFT-terminal (error / no_defect): known only within ``retry_cooldown_s`` of
          the recorded ``ts``; after that the locus becomes retryable again. A transient
          tooling error or an honest "no defect today" must NOT block the surface
          forever (the bug this fixes: 3 speculative seeds recorded ``error`` idled the
          loop permanently at "0 fresh")."""
        e = self._seen.get(fp)
        if e is None:
            return False
        if e.status == STATUS_SEEN:
            return False
        # An operator PURGED this finding from the Changes screen ("forget it so the agent
        # can re-discover"): treat it as unknown so the next discovery cycle surfaces the
        # locus again. Without this, a purged fp's status falls through to the hard-terminal
        # branch and stays deduped — the opposite of what a delete intends.
        if e.status == STATUS_PURGED:
            return False
        if e.status in SOFT_TERMINAL:
            if self.retry_cooldown_s <= 0:
                return False  # always retryable
            now = time.time() if now is None else now
            return (now - e.ts) < self.retry_cooldown_s
        return True  # hard-terminal

    def status_of(self, fp: str) -> str | None:
        e = self._seen.get(fp)
        return e.status if e else None

    def record(self, entry: LedgerEntry) -> None:
        if entry.ts == 0.0:
            entry.ts = time.time()
        with self._lock:
            self._seen[entry.fp] = entry
            with self.path.open("a") as f:
                f.write(json.dumps(asdict(entry)) + "\n")

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._seen.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out

    def filed_crs(self) -> list[str]:
        return [e.cr for e in self._seen.values() if e.status == STATUS_FILED and e.cr]

    def terminal_targets(self, *, kind: str | None = None, now: float | None = None) -> list[str]:
        """Human-readable ``target`` strings (file::symbol) of loci that are currently KNOWN
        (deduped) — for a discovery SKIP-LIST so the agent doesn't waste reads re-proposing
        surfaces that are already terminal (operator: discovery re-emits already-terminal
        candidates every cycle = wasted LLM cost). Uses ``known()`` so it honors the
        soft-terminal cooldown (an error/no_defect past its window is retryable → NOT skipped)
        and excludes ``seen`` (in-progress) + ``purged`` (deliberately re-discoverable).
        Optionally filter by ``kind`` (e.g. only 'bug' loci for a bug run). Deduped, sorted."""
        out: set[str] = set()
        for e in self._seen.values():
            if kind is not None and e.kind != kind:
                continue
            if e.target and self.known(e.fp, now=now):
                out.add(e.target)
        return sorted(out)

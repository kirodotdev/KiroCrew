"""Results archive — the candidate population store (spine durable state).

One of the two durable stores (the other is the git working branch). The archive
carries the WHOLE candidate population including near-misses, run metadata,
anchors and drift, while git carries only kept commits. This separation is what
lets "revert" be "don't apply the diff to the branch" while a discarded diff
still survives as evolutionary memory for the next cycle's proposer (top-K).

Layout (02_architecture.md §3.3 — spine schema; the metric columns of
``results.tsv`` are profile-provided):

    results/
      run.meta.json        # base sha, anchors, noise band, profile id, host pinning
      results.tsv          # one row per MEASURED candidate (append-only, greppable)
      candidates.jsonl     # one rich JSON object per measured candidate
      candidates/<id>.diff # unified diff vs cycle base, for EVERY survivor
      candidates/<id>.json # per-candidate detail (stages, all A/B reps)
      anchors/             # frozen anchors + provenance
      drift/               # periodic re-measure of current best (§4.3)

Docs: 02_architecture.md §3, §4.2 (resume reads this), 10_roadmap M0
("durable state = git branch + results/ archive").
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path

# The spine fixes these control columns; the ruler contributes the primary-metric,
# per-stage and guardrail columns (02_arch §3.3). For M0 we write the control set
# plus a JSON blob of the profile-provided metric so the schema is honoured without
# the spine hard-coding any metric name.
#
# ``secondary`` is the LAST column: a compact ``key=value;…`` rendering of the
# Measurement.secondary dict (the NON-BLOCKING resource/throughput metrics the ruler
# sampled — the extra results.tsv columns beyond the primary; METRICS.md §6). It is the
# last column so the fixed positional control columns keep their offsets and the greppable
# TSV stays append-compatible while carrying the profile's secondary metric names; the
# full per-metric dict is also stored in the JSONL row (and the per-candidate JSON).
CONTROL_COLUMNS = (
    "cycle",
    "cand_id",
    "commit",
    "status",
    "tests_pass",
    "reps",
    "primary_delta",
    "noise_band",
    "description",
    "diff_ref",
    "secondary",
)


def _render_secondary(value) -> str:
    """Render the ``secondary`` row value into the TSV cell: a compact ``k=v;…`` string
    (tab-/newline-free, greppable). Accepts the dict the driver passes; anything else is
    coerced to ``str`` so the column is always present (an empty dict -> empty cell)."""
    if isinstance(value, dict):
        return ";".join(f"{k}={v}" for k, v in sorted(value.items()))
    return "" if value is None else str(value)


def _jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [_jsonable(v) for v in obj]
    return obj


class Archive:
    """The on-disk ``results/`` archive. Append-only; reconstructable on restart."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.tsv = self.root / "results.tsv"
        self.jsonl = self.root / "candidates.jsonl"
        self.candidates_dir = self.root / "candidates"
        self.anchors_dir = self.root / "anchors"
        self.drift_dir = self.root / "drift"
        self.meta_path = self.root / "run.meta.json"
        for d in (self.root, self.candidates_dir, self.anchors_dir, self.drift_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._ensure_tsv()

    def _ensure_tsv(self) -> None:
        if not self.tsv.exists():
            self.tsv.write_text("\t".join(CONTROL_COLUMNS) + "\n")

    # ── run metadata ────────────────────────────────────────────────────

    def write_meta(self, meta: dict) -> None:
        self.meta_path.write_text(json.dumps(_jsonable(meta), indent=2))

    def read_meta(self) -> dict:
        if not self.meta_path.exists():
            return {}
        try:
            return json.loads(self.meta_path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    # ── per-candidate detail + the append-only TSV row ──────────────────

    def save_candidate(self, *, cand_id: str, diff: str, detail: dict) -> str:
        """Write ``<id>.diff`` + ``<id>.json`` for a survivor. Returns the diff
        ref recorded in the TSV row."""
        diff_path = self.candidates_dir / f"{cand_id}.diff"
        json_path = self.candidates_dir / f"{cand_id}.json"
        # The run archive is intentionally local plaintext under the app's scratch
        # directory (`~/.autoimprove-scratch`), not a credential store — it IS the
        # evidence an operator inspects to judge a candidate, so an encrypted or elided
        # copy would defeat its only purpose. Nothing here is served raw: the read side
        # (`backend/routes.py:_redact_for_display`) credential-scans before the diff
        # reaches a browser, and every push path scans before anything leaves the host.
        diff_path.write_text(diff or "")  # lgtm[py/clear-text-storage-sensitive-data]
        json_path.write_text(  # lgtm[py/clear-text-storage-sensitive-data]
            json.dumps(_jsonable(detail), indent=2)
        )
        return diff_path.name

    def append_row(self, row: dict) -> None:
        """Append one MEASURED candidate to ``results.tsv`` + ``candidates.jsonl``.

        The control columns are written positionally; the full (incl. metric +
        secondary) row is stored as a JSON object so no metric name is hard-coded in the
        spine. The ``secondary`` column is rendered compactly (``k=v;…``) so the
        sampled resource/throughput metrics are greppable in the TSV while the full
        per-metric dict survives in the JSONL row (METRICS.md §6).
        """

        def _cell(c: str) -> str:
            if c == "secondary":
                text = _render_secondary(row.get(c, {}))
            else:
                text = str(row.get(c, ""))
            # A TSV cell must contain no TAB (column separator) or newline (row
            # separator). An agent-authored value — a candidate's ``description`` /
            # signature — can carry either, which would shift every later column or split
            # one row into several and corrupt the append-only archive that results.tsv
            # readers parse positionally. Collapse them to a space; the full, unaltered
            # value still survives in the JSONL row below. Raised by the GPT review.
            return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")

        line = "\t".join(_cell(c) for c in CONTROL_COLUMNS)
        with self.tsv.open("a") as f:
            f.write(line + "\n")
        with self.jsonl.open("a") as f:
            # Local plaintext archive row — same reasoning as `save_candidate` above.
            f.write(json.dumps(_jsonable(row)) + "\n")  # lgtm[py/clear-text-storage-sensitive-data]

    # ── reads used for resume + top-K evolutionary memory (§3.4, §4.2) ──

    def cycle_count(self) -> int:
        """Highest cycle number recorded (recomputed from the archive, not held
        in memory — survives restarts, 02_arch §4.2)."""
        n = 0
        if not self.jsonl.exists():
            return 0
        for line in self.jsonl.read_text().splitlines():
            try:
                n = max(n, int(json.loads(line).get("cycle", 0)))
            except Exception:  # noqa: BLE001
                continue
        return n

    def top_k(self, k: int = 8) -> list[dict]:
        """Top-K archived candidates by primary delta (most-improving first),
        excluding kept ones — the proposer's memory of strong near-misses."""
        rows: list[dict] = []
        if self.jsonl.exists():
            for line in self.jsonl.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue

        def _delta(r) -> float | None:
            v = r.get("primary_delta")
            if v is None or v == "":  # no measurement / bug-track row (driver writes "")
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        scored = [
            (r, d) for r in rows if r.get("status") != "kept" and (d := _delta(r)) is not None
        ]
        scored.sort(key=lambda rd: rd[1])  # most negative = best
        return [r for r, _ in scored[:k]]

    def write_drift(self, cycle: int, payload: dict) -> None:
        (self.drift_dir / f"rebest-{cycle}.json").write_text(
            json.dumps(_jsonable({"cycle": cycle, "ts": time.time(), **payload}), indent=2)
        )

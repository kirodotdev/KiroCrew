"""Credit Usage backend for Kiro Crew.

A small stdlib-only HTTP server that reads the per-turn token/credit usage log
the gateway already writes and serves it, aggregated, to the credit-usage
frontend. Bound to localhost; Kiro Crew proxies requests from
``/apps/credit-usage/api/*`` to this process.

The usage log lives at ``<data home>/usage/tokens/<YYYY-MM-DD>.jsonl`` — the
same shards written by ``kiro_crew.dashboard.handlers.usage.persist_token_record``.
Each line is a JSON object with ``"_type": "tokens"`` and (among others) the
fields ``ts, slot, provider, model, credits, turns, surface, agent,
context_used, context_window, phase, stop_reason``. Credits are provider-
reported (not derived from tokens), so we simply sum the ``credits`` field.

This app is READ-ONLY: it opens and parses the usage shards and never writes,
edits, or deletes anything, and it reaches no path other than the usage-tokens
directory.

Endpoints (the server sees them at the root — Kiro Crew strips the ``/api``
prefix before proxying):

  GET /health                       -> {"status": "ok", "app", "version", "hasData"}
  GET /summary?days=<n>&tz=<offset> -> rolled-up totals, trend, breakdowns, top sessions
  GET /recent?limit=<n>             -> the most recent per-turn rows (live feed)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kiro_crew.apps.proxy_auth import verify_proxy_request
from kiro_crew.config.paths import data_home
from kiro_crew.sel import sel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 9120))
APP_NAME = os.environ.get("KIROCREW_APP_NAME", "credit-usage")
VERSION = "1.0.0"

# How many days back the /summary endpoint scans by default, and the hard cap.
DEFAULT_DAYS = 30
MAX_DAYS = 180
# How many recent rows the live feed returns by default, and the hard cap.
DEFAULT_RECENT = 40
MAX_RECENT = 200
# Cap on rows parsed from disk per request to bound memory on a huge history.
MAX_ROWS_SCANNED = 200_000

logger = logging.getLogger(APP_NAME)
logging.basicConfig(
    level=logging.INFO,
    format=f"[{APP_NAME}] %(asctime)s %(levelname)s %(message)s",
)


def _tokens_dir() -> Path:
    """The per-turn usage-log directory, resolved the same way the writer does."""
    return data_home() / "usage" / "tokens"


def _sessions_dir() -> Path:
    """The chat-session transcript directory (one <safe_key>.jsonl per session)."""
    return data_home() / "sessions"


# ---------------------------------------------------------------------------
# Session-title resolution (slot -> human-readable name)
# ---------------------------------------------------------------------------
#
# Usage rows key spend by ``slot`` (e.g. "chat-12-...", "dashboard:chat-12-...",
# "subagent:5e240c42"). The human title lives in the session transcript's first
# line: {"_type": "metadata", "title": ...}. When no explicit title is set the
# first user message (truncated) is the fallback, mirroring
# history.ConversationLog.list_sessions. subagent:* slots have no transcript, so
# they keep their slot as the label.

_title_sig: tuple | None = None
_title_map: dict[str, str] = {}


def _sessions_signature(sdir: Path) -> tuple:
    sig: list[tuple[str, int]] = []
    try:
        for p in sdir.glob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            sig.append((p.name, int(st.st_mtime)))
    except OSError:
        pass
    return tuple(sig)


def _title_from_file(path: Path) -> str | None:
    """Extract a title: explicit metadata title, else first user message (<=80c)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = []
            for i, ln in enumerate(f):
                if i > 20:
                    break
                lines.append(ln.strip())
    except OSError:
        return None
    if not lines:
        return None
    # Line 1 may be a metadata record carrying an explicit title.
    try:
        first = json.loads(lines[0]) if lines[0] else {}
    except ValueError:
        first = {}
    if isinstance(first, dict) and first.get("_type") == "metadata" and first.get("title"):
        return str(first["title"])[:120]
    # Fallback: first user message anywhere in the scanned window (INCLUDING
    # line 1 — a transcript with no metadata header starts with the user turn).
    for ln in lines:
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("role") == "user" and d.get("content"):
            return str(d["content"])[:80]
    return None


def _normalize_slot(slot: str) -> str:
    """Reduce a usage slot to the session-key core used to match a transcript.

    Strips the ``dashboard:`` prefix (usage normalizes bare dashboard slots to
    ``dashboard:chat-...``) and the ``dashboard_`` filename prefix so both forms
    of the same conversation collapse to one lookup key.
    """
    s = slot.split(":", 1)[1] if ":" in slot and not slot.startswith("subagent:") else slot
    if s.startswith("dashboard_"):
        s = s[len("dashboard_") :]
    return s


def _load_title_map() -> dict[str, str]:
    """Build {normalized-session-key: title}, memoized by the sessions-dir signature."""
    global _title_sig, _title_map
    sdir = _sessions_dir()
    sig = _sessions_signature(sdir)
    if sig == _title_sig and _title_map:
        return _title_map
    out: dict[str, str] = {}
    try:
        for p in sdir.glob("*.jsonl"):
            if p.is_symlink():
                continue
            key = p.stem  # e.g. "dashboard_chat-12-1787734279"
            title = _title_from_file(p)
            if not title:
                continue
            norm = _normalize_slot(key)
            # Prefer the first non-empty title seen; dedupe of stacked prefixes
            # is not critical for a label.
            out.setdefault(norm, title)
    except OSError:
        pass
    _title_sig = sig
    _title_map = out
    return out


def _title_for_slot(slot: str) -> str:
    """Human label for a slot: session title if known, else the slot itself."""
    if slot.startswith("subagent:"):
        return slot  # subagents have no transcript/title
    return _load_title_map().get(_normalize_slot(slot), slot)


# ---------------------------------------------------------------------------
# Reading + caching
# ---------------------------------------------------------------------------

# Cache of parsed rows keyed on the shard-set signature (name, mtime, size) so a
# new turn (which changes the current shard's mtime+size) invalidates exactly
# when it lands, and repeated polls in between are free.
_cache_sig: tuple | None = None
_cache_rows: list[dict[str, Any]] = []


def _shard_signature(shard_dir: Path) -> tuple:
    sig: list[tuple[str, int, int]] = []
    try:
        for p in sorted(shard_dir.glob("*.jsonl")):
            try:
                st = p.stat()
            except OSError:
                continue
            sig.append((p.name, int(st.st_mtime), int(st.st_size)))
    except OSError:
        pass
    return tuple(sig)


def _load_rows() -> list[dict[str, Any]]:
    """Return all token rows across shards, newest last, memoized by signature."""
    global _cache_sig, _cache_rows
    shard_dir = _tokens_dir()
    sig = _shard_signature(shard_dir)
    if sig == _cache_sig and _cache_rows:
        return _cache_rows
    rows: list[dict[str, Any]] = []
    scanned = 0
    for p in sorted(shard_dir.glob("*.jsonl")):
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    scanned += 1
                    if scanned > MAX_ROWS_SCANNED:
                        break
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                        continue
                    rows.append(obj)
        except OSError:
            continue
        if scanned > MAX_ROWS_SCANNED:
            break
    # Sort by timestamp ascending so "newest last" holds even across shards.
    rows.sort(key=lambda r: str(r.get("ts", "")))
    _cache_sig = sig
    _cache_rows = rows
    return rows


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    # Tolerate a trailing Z (UTC) that fromisoformat historically rejected.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _credits(row: dict[str, Any]) -> float:
    try:
        return float(row.get("credits", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _model_label(row: dict[str, Any]) -> str:
    m = str(row.get("model", "") or "").strip()
    return m or "auto"


def _surface_label(row: dict[str, Any]) -> str:
    return str(row.get("surface", "") or "unknown").strip() or "unknown"


def _agent_label(row: dict[str, Any]) -> str:
    return str(row.get("agent", "") or "unknown").strip() or "unknown"


def _summary(rows: list[dict[str, Any]], days: int, tz_offset_min: int) -> dict[str, Any]:
    """Roll rows up into totals, a daily trend, breakdowns and top sessions.

    ``tz_offset_min`` is the client's UTC offset in minutes (JS
    ``-getTimezoneOffset()``); day bucketing and the today/week/month windows
    are computed in the client's local time so the dashboard matches the wall
    clock the user sees.
    """
    tz = timezone(timedelta(minutes=tz_offset_min))
    now = datetime.now(tz)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_start = today.replace(day=1)
    window_start = today - timedelta(days=days - 1)

    total = 0.0
    total_turns = 0
    today_c = week_c = month_c = 0.0
    by_day: dict[str, float] = {}
    by_model: dict[str, dict[str, float]] = {}
    by_surface: dict[str, dict[str, float]] = {}
    by_agent: dict[str, dict[str, float]] = {}
    by_session: dict[str, dict[str, Any]] = {}
    latest_ts: str = ""

    for row in rows:
        dt = _parse_ts(str(row.get("ts", "")))
        if dt is None:
            continue
        local = dt.astimezone(tz)
        d = local.date()
        c = _credits(row)
        total += c
        total_turns += 1
        if str(row.get("ts", "")) > latest_ts:
            latest_ts = str(row.get("ts", ""))

        if d == today:
            today_c += c
        if d >= week_start:
            week_c += c
        if d >= month_start:
            month_c += c

        # Trend only within the requested window.
        if d >= window_start:
            key = d.isoformat()
            by_day[key] = by_day.get(key, 0.0) + c

            ml = _model_label(row)
            m = by_model.setdefault(ml, {"credits": 0.0, "turns": 0})
            m["credits"] += c
            m["turns"] += 1

            sl = _surface_label(row)
            s = by_surface.setdefault(sl, {"credits": 0.0, "turns": 0})
            s["credits"] += c
            s["turns"] += 1

            al = _agent_label(row)
            a = by_agent.setdefault(al, {"credits": 0.0, "turns": 0})
            a["credits"] += c
            a["turns"] += 1

            slot = str(row.get("slot", "") or "unknown")
            sess = by_session.setdefault(
                slot,
                {"slot": slot, "credits": 0.0, "turns": 0, "surface": sl, "last_ts": ""},
            )
            sess["credits"] += c
            sess["turns"] += 1
            if str(row.get("ts", "")) > sess["last_ts"]:
                sess["last_ts"] = str(row.get("ts", ""))
                sess["surface"] = sl

    # Fill trend gaps with 0 so the chart shows every day in the window.
    trend: list[dict[str, Any]] = []
    for i in range(days):
        day_str = (window_start + timedelta(days=i)).isoformat()
        trend.append({"date": day_str, "credits": round(by_day.get(day_str, 0.0), 4)})

    def _breakdown(m: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [
            {
                "name": k,
                "credits": round(v["credits"], 4),
                "turns": int(v["turns"]),
            }
            for k, v in m.items()
        ]
        out.sort(key=lambda x: float(x["credits"]), reverse=True)
        return out

    top_sessions: list[dict[str, Any]] = [
        {
            "slot": v["slot"],
            "title": _title_for_slot(str(v["slot"])),
            "credits": round(float(v["credits"]), 4),
            "turns": int(v["turns"]),
            "surface": v["surface"],
            "lastTs": v["last_ts"],
        }
        for v in by_session.values()
    ]
    top_sessions.sort(key=lambda x: float(x["credits"]), reverse=True)

    window_credits = round(sum(float(x["credits"]) for x in trend), 4)

    return {
        "windowDays": days,
        "generatedAt": now.isoformat(),
        "latestTs": latest_ts,
        "totals": {
            "allTimeCredits": round(total, 4),
            "allTimeTurns": total_turns,
            "windowCredits": window_credits,
            "today": round(today_c, 4),
            "thisWeek": round(week_c, 4),
            "thisMonth": round(month_c, 4),
        },
        "trend": trend,
        "byModel": _breakdown(by_model),
        "bySurface": _breakdown(by_surface),
        "byAgent": _breakdown(by_agent),
        "topSessions": top_sessions[:25],
        "sessionCount": len(by_session),
    }


def _recent(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    tail = rows[-limit:]
    out = []
    for row in reversed(tail):  # newest first
        slot = str(row.get("slot", "") or "unknown")
        out.append(
            {
                "ts": str(row.get("ts", "")),
                "slot": slot,
                "title": _title_for_slot(slot),
                "model": _model_label(row),
                "surface": _surface_label(row),
                "agent": _agent_label(row),
                "credits": round(_credits(row), 4),
                "contextUsed": int(row.get("context_used", 0) or 0),
                "contextWindow": int(row.get("context_window", 0) or 0),
                "stopReason": str(row.get("stop_reason", "") or ""),
                "phase": str(row.get("phase", "") or ""),
            }
        )
    return out


def _sel_audit(operation: str, resources: str, outcome: str = "granted") -> None:
    """Emit a SEL audit event for usage-log reads."""
    try:
        sel().log_api_access(
            caller="credit-usage",
            operation=operation,
            outcome=outcome,
            source="builtin-app",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 — auditing must never break the request
        pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class CreditUsageHandler(BaseHTTPRequestHandler):
    server_version = "KiroCrew-CreditUsage/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _authorized_or_health(self, method: str) -> bool:
        """Verify the gateway's X-KiroCrew-Proxy HMAC before dispatch (CWE-306).

        ``/health`` stays unauthenticated because the gateway's own liveness
        probe hits the backend directly, unsigned. A read-only GET carries no
        body, so the signed body hash is sha256(b"").
        """
        route = urllib.parse.urlparse(self.path).path.rstrip("/")
        if route in ("", "/health", "/api", "/api/health"):
            return True
        if verify_proxy_request(
            self.headers.get("X-KiroCrew-Proxy", ""),
            method=method,
            target=self.path,
            body=b"",
        ):
            return True
        _sel_audit("proxy_auth_failed", self.path, outcome="denied")
        self._json(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized_or_health("GET"):
            return
        try:
            self._dispatch("GET")
        except Exception:  # noqa: BLE001
            corr = uuid.uuid4().hex[:12]
            logger.exception("GET %s failed [%s]", self.path, corr)
            # Generic body + correlation id — do not echo raw exception text to
            # the (reverse-proxied, browser-facing) client (CWE-209).
            self._json(500, {"error": "internal error", "id": corr})

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        route = url.path.rstrip("/") or "/"
        # Kiro Crew's reverse proxy forwards requests as /api/<path>; strip it
        # so both direct and proxied requests resolve.
        if route.startswith("/api/"):
            route = route[4:] or "/"
        elif route == "/api":
            route = "/"

        if method == "GET" and route in ("/", "/health"):
            has_data = bool(_shard_signature(_tokens_dir()))
            return self._json(
                200,
                {"status": "ok", "app": APP_NAME, "version": VERSION, "hasData": has_data},
            )
        if method == "GET" and route == "/summary":
            return self._h_summary(qs)
        if method == "GET" and route == "/recent":
            return self._h_recent(qs)
        return self._json(404, {"error": f"{method} {route} not found"})

    def _h_summary(self, qs: dict[str, list[str]]) -> None:
        days = _clamp_int((qs.get("days") or [str(DEFAULT_DAYS)])[0], 1, MAX_DAYS, DEFAULT_DAYS)
        tz_off = _clamp_int((qs.get("tz") or ["0"])[0], -14 * 60, 14 * 60, 0)
        rows = _load_rows()
        _sel_audit("usage_summary", f"days={days} rows={len(rows)}")
        return self._json(200, _summary(rows, days, tz_off))

    def _h_recent(self, qs: dict[str, list[str]]) -> None:
        limit = _clamp_int(
            (qs.get("limit") or [str(DEFAULT_RECENT)])[0], 1, MAX_RECENT, DEFAULT_RECENT
        )
        rows = _load_rows()
        _sel_audit("usage_recent", f"limit={limit}")
        return self._json(200, {"rows": _recent(rows, limit), "totalRows": len(rows)})

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _clamp_int(raw: str, lo: int, hi: int, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(v, hi))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), CreditUsageHandler)
    logger.info("listening on http://127.0.0.1:%d  tokens=%s", PORT, _tokens_dir())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

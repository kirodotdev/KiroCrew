"""Read-only symptom probes behind ``diagnose_settings``.

Each probe answers one question users have asked about a misbehaving install
("why can't I see this model", "why did my job stop", "why does every turn
fail") from state readable on this host, and returns one finding::

    {id, status: ok|warn|problem|unknown, summary, evidence, fix?}

``fix`` is ``{"card": {"kind", "params"}}`` when a change card can make the
change, or ``{"steps": [...]}`` for something only a person can do.

Every probe is strictly read-only: it never calls a ``_doctor_*`` function or
anything with a repair or atomic-write path, opens SQLite read-only and
immutable, and never returns a secret value, a token or a whole MCP spec. Each
runs under its own deadline on a worker thread, and a probe that raises or
overruns reports ``unknown`` instead of failing the diagnosis.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
PROBLEM = "problem"
UNKNOWN = "unknown"

#: Seconds one probe may run before it reports ``unknown``.
PROBE_TIMEOUT_SECS = 4.0
#: Seconds the whole probe pass may take.
PROBES_TOTAL_SECS = 8.0
#: Longest string any finding field carries.
_TEXT_MAX = 240
#: Most list items any evidence field carries.
_LIST_MAX = 10
#: A slot whose turn has run longer than this is reported.
LONG_TURN_SECS = 20 * 60
#: The crew-log window the failure probes read.
_CREW_LOG_WINDOW_MS = 24 * 3600 * 1000
_CREW_LOG_UNITS_MAX = 15
_CREW_LOG_ENTRIES_PER_UNIT = 400
#: A recurring failure group at or above this count is a problem, not a warning.
_FAILURE_PROBLEM_COUNT = 3
_REMOTE_CONNECT_TIMEOUT_SECS = 0.8
_REMOTE_INSTANCES_MAX = 5
_SPECS_MAX = 200

_executor_lock = threading.Lock()
_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _pool() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="diagnose-probe"
            )
        return _executor


def _redact(text: str) -> str:
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text)


def _clip(value: Any, depth: int = 0) -> Any:
    """*value* with strings redacted and capped and lists cut to :data:`_LIST_MAX`."""
    if isinstance(value, str):
        text = _redact(value)
        return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1] + "…"
    if isinstance(value, dict) and depth < 4:
        return {str(k)[:64]: _clip(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)) and depth < 4:
        return [_clip(v, depth + 1) for v in list(value)[:_LIST_MAX]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _clip(str(value), depth)


def finding(
    probe_id: str,
    status: str,
    summary: str,
    evidence: Any = None,
    fix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": probe_id,
        "status": status,
        "summary": _clip(summary),
        "evidence": _clip(evidence if evidence is not None else {}),
    }
    if fix:
        out["fix"] = _clip(fix)
    return out


def _steps(*steps: str) -> dict[str, Any]:
    return {"steps": list(steps)}


def _iso(epoch: float | int | None) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


class ProbeContext:
    """What the probes share: the loaded config, the gateway app, one crew-log scan."""

    def __init__(self, app: Any = None, topic: str = "") -> None:
        from kiro_crew.config.loader import KiroCrewConfig

        self.app = app
        self.topic = (topic or "").strip()
        self.cfg = KiroCrewConfig.load()
        self.now = time.time()
        self._scan_lock = threading.Lock()
        self._scan: list[dict[str, Any]] | None = None

    @property
    def state(self) -> Any:
        app = self.app
        if app is None:
            return None
        try:
            return app.get("state")
        except Exception:
            return getattr(app, "state", None)

    def crew_log(self) -> list[dict[str, Any]]:
        """The last day's session units, newest first, read once per pass."""
        with self._scan_lock:
            if self._scan is None:
                self._scan = _crew_log_units(int(self.now * 1000))
            return self._scan


def _crew_log_units(now_ms: int) -> list[dict[str, Any]]:
    """``[{unit, slot, open, entries: [{type, time, data}]}]`` for recent units.

    Reads through the same reader the crew-log routes serve from
    (:mod:`kiro_crew.crew_log.read`), which opens logs for reading only.
    """
    from kiro_crew.crew_log import read as crew_read

    listing = crew_read.list_session_units(
        active_within_ms=_CREW_LOG_WINDOW_MS, now_ms=now_ms, limit=_CREW_LOG_UNITS_MAX
    )
    units: list[dict[str, Any]] = []
    for row in listing.get("units") or listing.get("sessions") or []:
        unit = row.get("unit")
        last = int(row.get("last_seq") or 0)
        if not unit or last <= 0:
            continue
        page = crew_read.read_page(unit, max(1, last - _CREW_LOG_ENTRIES_PER_UNIT + 1), last)
        units.append(
            {
                "unit": unit,
                "slot": row.get("slot") or "",
                "open": bool(row.get("open")),
                "entries": [
                    {"type": e.get("type"), "time": e.get("time"), "data": e.get("data") or {}}
                    for e in page.get("entries") or []
                ],
            }
        )
    return units


def _recent(ctx: ProbeContext, *types: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    cutoff = int(ctx.now * 1000) - _CREW_LOG_WINDOW_MS
    out = []
    for unit in ctx.crew_log():
        for entry in unit["entries"]:
            if entry.get("type") in types and int(entry.get("time") or 0) >= cutoff:
                out.append((unit, entry))
    return out


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def probe_hidden_models(ctx: ProbeContext) -> dict[str, Any]:
    pid = "hidden_models"
    hidden = [str(m) for m in (ctx.cfg.dashboard.model_picker_hidden_models or [])]
    if not hidden:
        return finding(pid, OK, "No model is hidden from the chat model picker.")
    needle = ctx.topic.lower()
    matched = [m for m in hidden if needle and (needle in m.lower() or m.lower() in needle)]
    if matched:
        # The same one-item unhide the Selectable Models checkbox sends.
        fix: dict[str, Any] = {
            "card": {
                "kind": "setting.change",
                "params": {
                    "setting_id": "chat.selectable-models",
                    "op": "remove",
                    "item": matched[0],
                },
            }
        }
        return finding(
            pid,
            PROBLEM,
            f"{', '.join(matched)} is hidden from the model picker in Selectable Models.",
            {"hidden": hidden, "matched": matched, "setting_id": "chat.selectable-models"},
            fix,
        )
    return finding(
        pid,
        WARN,
        f"{len(hidden)} model(s) are hidden from the model picker.",
        {"hidden": hidden, "setting_id": "chat.selectable-models"},
        _steps(
            "Ask which model the user is missing, then propose a setting.change card on "
            "'chat.selectable-models' with op 'remove' and that model as the item.",
        ),
    )


def _spec_files() -> list[Path]:
    from kiro_crew.agent_spec_format import iter_agent_spec_files
    from kiro_crew.config.paths import kiro_agents_dir

    directory = kiro_agents_dir()
    if not directory.is_dir():
        return []
    return iter_agent_spec_files(directory)[:_SPECS_MAX]


def probe_agent_picker_stale(ctx: ProbeContext) -> dict[str, Any]:
    """Agent specs on disk the agent list does not offer.

    The list re-reads the agents directory whenever a spec is added, removed or
    edited, so a spec written after the gateway started is offered on the next
    read with no restart. What stays missing after a fresh read is a spec the
    scan could not use (unreadable, invalid, or shadowed by a project agent);
    a chat opened earlier keeps the agent list it loaded.
    """
    pid = "agent_picker_stale"
    state = ctx.state
    started = getattr(state, "start_time", None) if state is not None else None
    if not isinstance(started, (int, float)) or started <= 0:
        return finding(pid, UNKNOWN, "The gateway start time is not readable from here.")
    from kiro_crew import agent_discovery
    from kiro_crew.config.paths import kiro_agents_dir

    try:
        listed = {a.name for a in agent_discovery.list_agents(agents_dir=kiro_agents_dir())}
    except Exception:  # noqa: BLE001 -- a probe reports, it never raises
        return finding(pid, UNKNOWN, "The agent list could not be read from here.")
    newer: list[dict[str, Any]] = []
    for path in _spec_files():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > started:
            newer.append({"name": path.stem, "changed": _iso(mtime), "listed": path.stem in listed})
    if not newer:
        return finding(pid, OK, "Every agent spec on disk predates the gateway's start.")
    evidence = {"gateway_started": _iso(started), "specs": newer}
    missing = [n for n in newer if not n["listed"]]
    if missing:
        return finding(
            pid,
            PROBLEM,
            f"{len(missing)} agent spec(s) changed since the gateway started are not offered "
            "in the agent list, so the scan could not use them: "
            + ", ".join(n["name"] for n in missing),
            evidence,
            _steps(
                "Check each named spec is valid JSON with a name, and that no project agent "
                "uses the same name.",
            ),
        )
    return finding(
        pid,
        OK,
        "Agent specs changed since the gateway started are already in its agent list; a "
        "chat opened before then shows them after you open a new chat.",
        evidence,
    )


def _model_pins(ctx: ProbeContext) -> list[dict[str, Any]]:
    cfg = ctx.cfg
    pins = [{"holder": "agent.model", "kind": "config", "model": cfg.agent.model or ""}]
    for role, model in (cfg.agent.role_models or {}).items():
        pins.append({"holder": f"agent.role_models.{role}", "kind": "config", "model": model})
    for name, member in (cfg.agents or {}).items():
        pins.append({"holder": str(name), "kind": "member", "model": member.model or ""})
    from kiro_crew.agent_discovery import parsed_agent_specs

    # The cached hardened snapshot: an unreadable spec is skipped (it pins
    # nothing a session could use), so the failure class is not needed here.
    for data, path in parsed_agent_specs(operation="diagnose", source="dashboard")[:_SPECS_MAX]:
        spec_model = data.get("model") if isinstance(data, dict) else None
        if isinstance(spec_model, str):
            pins.append({"holder": path.name, "kind": "spec", "model": spec_model})
    return [p for p in pins if isinstance(p["model"], str) and p["model"] not in ("", "auto")]


def probe_model_pin_unavailable(ctx: ProbeContext) -> dict[str, Any]:
    pid = "model_pin_unavailable"
    from kiro_crew import model_registry
    from kiro_crew.agent_sdk.backends import model_registry_namespace
    from kiro_crew.agent_sdk.drivers.acp import resolve_usable_model
    from kiro_crew.config.sections import DEFAULT_MODEL

    backend = getattr(ctx.cfg.agent, "acp_backend", "") or ""
    advertised = model_registry.advertised_models(model_registry_namespace(backend))
    if not advertised:
        return finding(
            pid,
            UNKNOWN,
            "No session has reported this account's available models yet, so pins cannot "
            "be checked.",
        )
    bad = [p for p in _model_pins(ctx) if not resolve_usable_model(p["model"], advertised)]
    if not bad:
        return finding(pid, OK, "Every pinned model is one this account can run.")
    first = bad[0]
    fix: dict[str, Any]
    if first["kind"] == "config" and first["holder"] == "agent.model":
        fix = {
            "card": {
                "kind": "setting.change",
                "params": {"path": "agent.model", "value": DEFAULT_MODEL},
            }
        }
    elif first["kind"] == "member":
        fix = {
            "card": {
                "kind": "crewmate.update",
                "params": {"name": first["holder"], "fields": {"model": DEFAULT_MODEL}},
            }
        }
    else:
        fix = _steps(
            f"Change the model in {first['holder']} to '{DEFAULT_MODEL}' or a model the "
            "account offers in the chat model picker."
        )
    return finding(
        pid,
        PROBLEM,
        f"{len(bad)} pin(s) name a model this account does not offer; those turns fall "
        "back to the default model or fail.",
        {"pins": bad, "available_count": len(advertised)},
        fix,
    )


def probe_deprecated_agent_spec(ctx: ProbeContext) -> dict[str, Any]:
    """The detection of ``doctor_checks.agents._doctor_deprecated_agent_specs``, reporting only."""
    pid = "deprecated_agent_spec"
    from kiro_crew.agent import DEPRECATED_AGENT_SPECS
    from kiro_crew.cron import job_agent_names_from_disk
    from kiro_crew.doctor_checks.agents import _open_slot_agent_names

    cfg = ctx.cfg
    crews = set(cfg.agents)
    found: list[dict[str, str]] = []

    def _add(holder: str, name: object, *, skip_crews: bool = True) -> None:
        if not isinstance(name, str) or not name or (skip_crews and name in crews):
            return
        replacement = DEPRECATED_AGENT_SPECS.get(name)
        if replacement:
            found.append({"holder": holder, "name": name, "replacement": replacement})

    for crew_name, crew in cfg.agents.items():
        _add(f"crew {crew_name}", crew.kiro_agent, skip_crews=False)
    _add("agent.default_agent", cfg.agent.default_agent)
    _add("session.pool_agent", cfg.session.pool_agent)
    for channel_id, channel in cfg.slack_channels.items():
        _add(f"slack channel {channel_id}", channel.agent)
    for holder, name in job_agent_names_from_disk():
        _add(f"cron job {holder}", name)
    for slot_key, name in _open_slot_agent_names():
        _add(f"chat slot {slot_key}", name)
    if not found:
        return finding(pid, OK, "Nothing names a deprecated agent spec.")
    return finding(
        pid,
        PROBLEM,
        "A deprecated agent spec still resolves this release and fails with 'Mode not "
        "found' once it is removed.",
        {"uses": found},
        _steps(
            *(f"Change {f['holder']} from '{f['name']}' to '{f['replacement']}'." for f in found)
        ),
    )


def probe_cron_failing(ctx: ProbeContext) -> dict[str, Any]:
    pid = "cron_failing"
    from kiro_crew.config.loader import config_dir
    from kiro_crew.cron_service.store import _CRONS_FILE, _read_job_records

    records, loadable = _read_job_records(config_dir() / _CRONS_FILE)
    if not records and not loadable:
        return finding(
            pid,
            PROBLEM,
            "The schedule store exists but cannot be loaded, so no scheduled job runs.",
            {"store": _CRONS_FILE},
            _steps("Open the Schedule page; a store it cannot read shows no jobs."),
        )
    failing: list[dict[str, Any]] = []
    for job in records:
        if not isinstance(job, dict) or job.get("user_paused"):
            continue
        if job.get("last_status") != "error" and not job.get("auto_paused"):
            continue
        error = str(job.get("last_error") or "").strip().splitlines()
        failing.append(
            {
                "id": str(job.get("id") or ""),
                "name": str(job.get("name") or ""),
                "error": error[0] if error else "",
                "when": _iso(job.get("last_run_ts")),
                "auto_paused": bool(job.get("auto_paused")),
                "timezone": str(job.get("timezone") or ""),
            }
        )
    if not failing:
        return finding(pid, OK, "No scheduled job's last run failed.")
    first = failing[0]
    fix: dict[str, Any]
    if first["timezone"] and "timezone" in first["error"].lower():
        fix = {
            "card": {
                "kind": "schedule.update",
                "params": {"id": first["id"], "fields": {"timezone": ""}},
            }
        }
    else:
        fix = _steps(
            "Open the Schedule page and read the job's last error.",
            "When the cause is the job's name, message, cron or timezone, propose "
            "schedule.update for that job id with the corrected field.",
        )
    for job in failing:
        job.pop("timezone", None)
    return finding(
        pid,
        PROBLEM,
        f"{len(failing)} scheduled job(s) failed on their last run.",
        {"jobs": failing},
        fix,
    )


def probe_agent_spec_dead_paths(ctx: ProbeContext) -> dict[str, Any]:
    pid = "agent_spec_dead_paths"
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.doctor_deadpath import _report_only, check_dead_paths

    # The same directory the other spec probes read; report-only, so no rebuild.
    report = check_dead_paths(agents_dir=kiro_agents_dir(), repair=_report_only)
    dead = [
        {"spec": r.spec, "managed": r.managed, "server": d.server, "where": d.where, "path": d.path}
        for r in report.results
        for d in r.dead
    ]
    unreadable = [r.spec for r in report.unreadable]
    if not dead and not unreadable:
        return finding(pid, OK, "Every MCP command and path in the agent specs exists.")
    steps = []
    if any(d["managed"] for d in dead):
        steps.append("Restart the gateway; it rewrites the specs it manages.")
    if any(not d["managed"] for d in dead):
        steps.append("Reinstall the MCP server or package that wrote the other specs.")
    if unreadable:
        steps.append("Fix or remove the unreadable spec files.")
    return finding(
        pid,
        PROBLEM,
        "An agent spec points an MCP server at a command or path that no longer exists; "
        "that server fails to start.",
        {"dead": dead, "unreadable": unreadable},
        _steps(*steps),
    )


def _kiro_service(ctx: ProbeContext) -> Any:
    app = ctx.app
    service = None
    if app is not None:
        try:
            service = app.get("kiro_prerequisite_service")
        except Exception:
            service = None
    if service is None and ctx.state is not None:
        service = getattr(ctx.state, "kiro_prerequisite_service", None)
    return service


def probe_kiro_cli_auth(ctx: ProbeContext) -> dict[str, Any]:
    pid = "kiro_cli_auth"
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

    backend = getattr(ctx.cfg.agent, "acp_backend", "") or ""
    if backend not in (ACP_BACKEND_KIRO, ACP_BACKEND_KAS):
        return finding(pid, OK, "The configured backend does not sign in through kiro-cli.")
    service = _kiro_service(ctx)
    status = getattr(service, "_status", None)
    if status is None or not getattr(service, "_has_probed", False):
        return finding(pid, UNKNOWN, "The kiro-cli sign-in has not been checked since start.")
    failed = [
        e for _u, e in _recent(ctx, "turn/completed") if e["data"].get("stop_reason") == "failed"
    ]
    evidence = {
        "installed": bool(status.installed),
        "authenticated": bool(status.authenticated),
        "failed_turns_24h": len(failed),
    }
    if status.installed and not status.authenticated:
        return finding(
            pid,
            PROBLEM,
            "kiro-cli is signed out or its sign-in expired, so every turn fails (often as "
            "repeated server errors).",
            evidence,
            _steps(
                f"On the gateway host run: {status.login_command}",
                "Start a new chat after signing in.",
            ),
        )
    if not status.installed:
        return finding(
            pid,
            PROBLEM,
            "kiro-cli is not installed on the gateway host.",
            evidence,
            _steps(f"Install kiro-cli on the gateway host: {status.docs_url}"),
        )
    return finding(pid, OK, "kiro-cli was signed in at the last check.", evidence)


def probe_embedding_coverage(ctx: ProbeContext) -> dict[str, Any]:
    pid = "embedding_coverage"
    from kiro_crew.config.loader import config_dir
    from kiro_crew.memory_stores import MEMORY_DB_FILE

    path = config_dir() / MEMORY_DB_FILE
    if not path.is_file():
        return finding(pid, OK, "There is no vector memory yet.")
    dim = int(ctx.cfg.memory.embedding_dim)
    want = dim * 4
    # immutable=1: SQLite takes no lock and writes no -shm/-wal beside the file.
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        tables: dict[str, dict[str, Any]] = {}
        for table in ("episodic_memories", "semantic_memory"):
            cols = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "embedding" not in cols:
                continue
            total, missing, wrong = db.execute(
                f"SELECT COUNT(*), SUM(embedding IS NULL), "  # noqa: S608 -- fixed table names
                f"SUM(embedding IS NOT NULL AND length(embedding) != ?) "
                f"FROM {table} WHERE is_deleted = 0",
                (want,),
            ).fetchone()
            sizes = [
                int(r[0]) // 4
                for r in db.execute(
                    f"SELECT DISTINCT length(embedding) FROM {table} "  # noqa: S608
                    "WHERE is_deleted = 0 AND embedding IS NOT NULL LIMIT 5"
                )
            ]
            tables[table] = {
                "rows": int(total or 0),
                "without_embedding": int(missing or 0),
                "wrong_dimension": int(wrong or 0),
                "stored_dimensions": sizes,
            }
    finally:
        db.close()
    evidence = {"configured_dim": dim, "tables": tables}
    wrong = sum(t["wrong_dimension"] for t in tables.values())
    missing = sum(t["without_embedding"] for t in tables.values())
    if wrong:
        return finding(
            pid,
            PROBLEM,
            f"{wrong} memory row(s) store vectors of a different size than "
            f"memory.embedding_dim ({dim}); semantic recall skips them.",
            evidence,
            _steps(
                "The embedding model or memory.embedding_dim changed after these rows were "
                "written. Set them back to match the stored size, or accept that the old "
                "rows are found by keyword only. Nothing here deletes or rewrites memory."
            ),
        )
    if missing:
        return finding(
            pid,
            WARN,
            f"{missing} memory row(s) have no embedding yet; they are found by keyword "
            "until one is computed.",
            evidence,
        )
    return finding(pid, OK, "Every memory row has an embedding of the configured size.", evidence)


def probe_remote_crew_unreachable(ctx: ProbeContext) -> dict[str, Any]:
    pid = "remote_crew_unreachable"
    from kiro_crew.config.loader import config_dir

    path = config_dir() / "instances.json"
    if not path.is_file():
        return finding(pid, OK, "No Remote Crew is configured.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("instances") if isinstance(raw, dict) else None
    instances = [i for i in items or [] if isinstance(i, dict) and i.get("id")]
    if not instances:
        return finding(pid, OK, "No Remote Crew is configured.")
    down, unchecked = [], []
    for inst in instances[:_REMOTE_INSTANCES_MAX]:
        name = str(inst.get("name") or inst.get("id"))
        port = inst.get("local_port")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            unchecked.append(name)
            continue
        try:
            with socket.create_connection(("127.0.0.1", port), _REMOTE_CONNECT_TIMEOUT_SECS):
                pass
        except OSError:
            down.append({"name": name, "local_port": port})
    if down:
        return finding(
            pid,
            PROBLEM,
            f"{len(down)} Remote Crew connection(s) do not answer on their local port.",
            {"unreachable": down, "not_connected": unchecked},
            _steps(
                "Open Settings > Remote Crew and reconnect that crew.",
                "If it still fails, check the remote host is running and its gateway is up.",
            ),
        )
    if unchecked and len(unchecked) == len(instances[:_REMOTE_INSTANCES_MAX]):
        return finding(
            pid,
            UNKNOWN,
            "No Remote Crew has an open connection to check.",
            {"not_connected": unchecked},
        )
    return finding(pid, OK, "Every connected Remote Crew answers.", {"not_connected": unchecked})


def _slot_matches(unit_slot: str, key: str) -> bool:
    return bool(unit_slot) and (unit_slot == key or unit_slot.endswith(":" + key))


def probe_long_running_slot(ctx: ProbeContext) -> dict[str, Any]:
    pid = "long_running_slot"
    state = ctx.state
    slots = getattr(state, "_slots", None) if state is not None else None
    if not isinstance(slots, dict):
        return finding(pid, UNKNOWN, "The gateway's chat sessions are not readable from here.")
    running = [(k, s) for k, s in list(slots.items()) if getattr(s, "turn_running", False)]
    if not running:
        return finding(pid, OK, "No chat has a turn running.")
    now_ms = int(ctx.now * 1000)
    long_ones, undated = [], []
    for key, slot in running:
        started = None
        for unit in ctx.crew_log():
            if not _slot_matches(unit["slot"], key):
                continue
            open_turns: dict[Any, int] = {}
            for e in unit["entries"]:
                turn = e["data"].get("turn")
                if e["type"] == "turn/started":
                    open_turns[turn] = int(e.get("time") or 0)
                elif e["type"] == "turn/completed":
                    open_turns.pop(turn, None)
            if open_turns:
                started = max(open_turns.values())
        if started is None:
            undated.append(str(getattr(slot, "title", "") or key))
            continue
        age = (now_ms - started) / 1000
        if age > LONG_TURN_SECS:
            long_ones.append(
                {
                    "session": str(getattr(slot, "title", "") or key),
                    "key": key,
                    "minutes": int(age // 60),
                }
            )
    if long_ones:
        return finding(
            pid,
            PROBLEM,
            f"{len(long_ones)} chat(s) have had one turn running for over "
            f"{LONG_TURN_SECS // 60} minutes, which can make the dashboard feel frozen.",
            {"sessions": long_ones},
            _steps(
                "Stop the turn from that chat's stop button in the sidebar.",
                "If the dashboard does not respond, open it in a private window and stop it there.",
            ),
        )
    if undated:
        return finding(
            pid, UNKNOWN, "Some running turns have no recorded start time.", {"sessions": undated}
        )
    return finding(pid, OK, "No turn has run unusually long.")


def probe_recent_tool_failures(ctx: ProbeContext) -> dict[str, Any]:
    pid = "recent_tool_failures"
    groups: dict[tuple[str, str, str], int] = {}
    for _u, e in _recent(ctx, "tool/completed"):
        d = e["data"]
        status = str(d.get("status") or "")
        if d.get("is_error") is True or status not in ("completed", "unknown", ""):
            what = "/".join(str(x) for x in (d.get("server"), d.get("name")) if x)
            key = ("tool", what or "(unnamed tool)", status or "error")
            groups[key] = groups.get(key, 0) + 1
    for _u, e in _recent(ctx, "turn/refused"):
        key = ("turn_refused", "", str(e["data"].get("reason") or ""))
        groups[key] = groups.get(key, 0) + 1
    for _u, e in _recent(ctx, "turn/completed"):
        d = e["data"]
        if d.get("stop_reason") in ("failed", "interrupted"):
            key = ("turn_failed", str(d.get("error") or ""), str(d.get("stop_reason")))
            groups[key] = groups.get(key, 0) + 1
    if not groups:
        return finding(pid, OK, "No tool failure or refused turn in the last 24 hours.")
    rows: list[dict[str, Any]] = sorted(
        ({"kind": k[0], "what": k[1], "outcome": k[2], "count": n} for k, n in groups.items()),
        key=lambda r: -int(r["count"]),
    )
    worst = int(rows[0]["count"])
    return finding(
        pid,
        PROBLEM if worst >= _FAILURE_PROBLEM_COUNT else WARN,
        "Recent tool failures and refused or failed turns, grouped. The crew log keeps "
        "the outcome and exception class only, never JSON-RPC error codes; read the "
        "session with crew_log_projection for the step.",
        {"window_hours": 24, "groups": rows},
    )


def _usage_cache() -> dict[str, Any]:
    from kiro_crew.dashboard.handlers import sessions

    cache = getattr(sessions, "_usage_cache", None)
    return dict(cache) if isinstance(cache, dict) else {}


def probe_usage_limit(ctx: ProbeContext) -> dict[str, Any]:
    pid = "usage_limit"
    cache = _usage_cache()
    failed = len(
        [e for _u, e in _recent(ctx, "turn/completed") if e["data"].get("stop_reason") == "failed"]
    )
    if not cache:
        return finding(
            pid,
            UNKNOWN,
            "The account balance has not been read since the gateway started.",
            {"failed_turns_24h": failed},
        )
    evidence = {
        k: cache.get(k)
        for k in ("available", "reason", "credits_used", "credits_plan", "percentage", "stale")
        if k in cache
    }
    evidence["failed_turns_24h"] = failed
    if cache.get("available") is False:
        return finding(
            pid,
            WARN,
            "The account balance cannot be read, so a spent usage limit cannot be ruled out.",
            evidence,
            _steps("Open the usage pill's details in the chat header and press Refresh."),
        )
    pct = cache.get("percentage")
    overage = cache.get("credits_overage") or 0
    if isinstance(pct, (int, float)) and pct >= 100 and not overage:
        return finding(
            pid,
            PROBLEM,
            "The plan's usage limit is used up; turns are refused until it resets or the "
            "plan allows overage.",
            evidence,
            _steps("Check the plan's reset date or overage setting with the account owner."),
        )
    return finding(pid, OK, "The account has usage left.", evidence)


#: ``(id, probe)`` in the order findings are reported.
PROBES: tuple[tuple[str, Callable[[ProbeContext], dict[str, Any]]], ...] = (
    ("hidden_models", probe_hidden_models),
    ("agent_picker_stale", probe_agent_picker_stale),
    ("model_pin_unavailable", probe_model_pin_unavailable),
    ("deprecated_agent_spec", probe_deprecated_agent_spec),
    ("cron_failing", probe_cron_failing),
    ("agent_spec_dead_paths", probe_agent_spec_dead_paths),
    ("kiro_cli_auth", probe_kiro_cli_auth),
    ("embedding_coverage", probe_embedding_coverage),
    ("remote_crew_unreachable", probe_remote_crew_unreachable),
    ("long_running_slot", probe_long_running_slot),
    ("recent_tool_failures", probe_recent_tool_failures),
    ("usage_limit", probe_usage_limit),
)

_RANK = {PROBLEM: 0, WARN: 1, UNKNOWN: 2, OK: 3}


def run_probes(
    app: Any = None,
    topic: str = "",
    *,
    timeout: float = PROBE_TIMEOUT_SECS,
    total: float = PROBES_TOTAL_SECS,
) -> list[dict[str, Any]]:
    """Every probe's finding, problems first. Never raises."""
    try:
        ctx = ProbeContext(app, topic)
    except Exception:
        logger.warning("diagnose probes: config unreadable", exc_info=True)
        return [finding(pid, UNKNOWN, "The configuration could not be read.") for pid, _ in PROBES]
    pool = _pool()
    futures = [(pid, pool.submit(fn, ctx)) for pid, fn in PROBES]
    deadline = time.monotonic() + total
    out: list[dict[str, Any]] = []
    for pid, fut in futures:
        wait = max(0.0, min(timeout, deadline - time.monotonic()))
        try:
            result = fut.result(timeout=wait)
            out.append(result if isinstance(result, dict) else finding(pid, UNKNOWN, "No answer."))
        except concurrent.futures.TimeoutError:
            fut.cancel()
            out.append(finding(pid, UNKNOWN, "This check did not finish in time."))
        except Exception as exc:  # noqa: BLE001 -- a probe must never fail the diagnosis
            logger.debug("diagnose probe %s failed", pid, exc_info=True)
            out.append(finding(pid, UNKNOWN, f"This check could not run ({type(exc).__name__})."))
    out.sort(key=lambda f: _RANK.get(f["status"], 9))
    return out

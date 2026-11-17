"""``kirocrew chat --headless``: one unattended run on the local gateway, with a result.

The interactive ``kirocrew chat`` runs its own backend in this process and asks the
person at the terminal about every tool. A headless run has nobody to ask, and a
conductor needs what only the gateway runs (the work ledger, patrol wakes, worker
sessions), so this path drives the running gateway of the current data home the way
the dashboard does, as its owner:

1. mint a short-lived owner token the way ``kirocrew token`` does (``/api/token/local``
   over the peer-verified transport) and exchange it for the session cookie;
2. open one chat slot -- in conductor mode on the agent ``crew_mode.crew_mode_agent``
   resolves, so slot create stamps it the way a Crew Mode chat is stamped;
3. with ``--approval-mode trust``, set Trust on THAT slot only (``POST
   /api/chat/mode``, which the gateway audits as a mode change); workers it creates
   inherit it at ``session_create``;
4. send the seed, then poll until the run is done, asks a person, or hits
   ``--timeout`` / ``--budget``.

Design: docs/request-for-change/rfc-builtin-eval.md §12. Nothing on the gateway
side changes: every request is one the dashboard owner can already make.
"""

from __future__ import annotations

import argparse
import http.cookies
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Exit codes, one per way a run ends (rfc-builtin-eval.md §12.5).
EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_ASKED = 3
EXIT_TIMEOUT = 4
EXIT_BUDGET = 5
EXIT_NOT_SIGNED_IN = 6
EXIT_SANDBOX = 7
EXIT_NO_GATEWAY = 8
EXIT_INCOMPLETE = 9
EXIT_INTERRUPTED = 130

STATUS_EXIT = {
    "done": EXIT_DONE,
    "error": EXIT_ERROR,
    "asked": EXIT_ASKED,
    "timeout": EXIT_TIMEOUT,
    "budget": EXIT_BUDGET,
    "not_signed_in": EXIT_NOT_SIGNED_IN,
    "sandbox_refused": EXIT_SANDBOX,
    "no_gateway": EXIT_NO_GATEWAY,
    "incomplete": EXIT_INCOMPLETE,
    "interrupted": EXIT_INTERRUPTED,
}

#: The flags that only mean something with ``--headless``.
HEADLESS_ONLY = (
    "mode",
    "approval_mode",
    "until_done",
    "cwd",
    "timeout",
    "budget",
    "json",
    "idle_grace",
)

#: Seconds between status reads. Overridable for tests.
POLL_SECS = float(os.environ.get("KIROCREW_HEADLESS_POLL_SECS") or 5.0)
#: How long a finished background agent's result can be held before it reaches its
#: parent: ``subagent_parts.run_limits._RESET_TIMEOUT + _TEARDOWN_REPORT_GRACE``.
_DELIVERY_GRACE_SECS = 60.0

#: A turn that ends with an option list is asking the person to pick.
_OPTIONS_MARK = "[OPTIONS:"
#: shortcut: sign-in and sandbox failures are read from the text of an ``error`` row,
#: not from a typed error code; upgrade when the row carries one.
_NOT_SIGNED_IN_MARKS = ("not signed in", "not logged in")
_SANDBOX_MARKS = (
    "sandbox backend unavailable",
    "no os-level sandbox backend",
    "sandbox is unavailable",
    "sandbox unavailable",
    "no sandbox backend",
)


class UsageError(Exception):
    """A flag combination the run refuses before touching the gateway."""


class NoGateway(Exception):
    """The gateway for this data home cannot be reached."""


@dataclass
class Options:
    message: str
    mode: str = "chat"
    agent: str = ""
    model: str = ""
    approval_mode: str = ""
    until_done: bool = False
    idle_grace: float = 900.0
    cwd: str = ""
    timeout: float = 0.0
    budget: float = 0.0
    json: bool = False


@dataclass
class Result:
    status: str = "error"
    completed: bool = False
    asks: int = 0
    ask_events: list[dict[str, Any]] = field(default_factory=list)
    wall_s: float = 0.0
    credits: float = 0.0
    mode: str = "chat"
    agent: dict[str, str] = field(default_factory=dict)
    approval_mode: str = "none"
    slot: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    transcript: str = ""
    items: dict[str, int] = field(default_factory=dict)
    cwd: str = ""
    error: str = ""
    unclosed: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["exit_code"] = STATUS_EXIT.get(self.status, EXIT_ERROR)
        out["wall_s"] = round(self.wall_s, 1)
        out["credits"] = round(self.credits, 4)
        return out


# ---------- options ----------
def options_from_args(args: argparse.Namespace) -> Options:
    """Validate the flags of one headless run; raise :class:`UsageError` on a bad set."""
    message = args.message or ""
    if getattr(args, "message_file", None):
        if message:
            raise UsageError("give -m or --message-file, not both")
        from kiro_crew.hooks import safe_read_file

        try:
            # The sensitive-path gate: a credential file (or a link to one) is
            # refused before its text could be sent to the model.
            message = safe_read_file(args.message_file)
        except PermissionError as exc:
            raise UsageError(f"cannot read --message-file: {exc}") from None
        except OSError as exc:
            raise UsageError(f"cannot read --message-file: {exc.strerror}") from None
        except UnicodeDecodeError:
            raise UsageError("--message-file must be UTF-8 text") from None
    if not message.strip():
        raise UsageError("--headless needs a seed: -m TEXT or --message-file PATH")
    if message.lstrip()[:1] in ("/", "@"):
        # The gateway refuses it too (headless_marker.apply): its marker opens
        # the turn, so a command or saved prompt would not run.
        raise UsageError("the seed cannot open with / or @ (commands and saved prompts)")
    mode = args.mode or "chat"
    if mode == "conductor" and args.agent:
        # Conductor mode runs the agent `agent.crew_mode_agent` resolves; a hand-picked
        # template passed as --agent would not be the conductor mode under test.
        raise UsageError("--agent cannot be combined with --mode conductor")
    cwd = args.cwd or ""
    if cwd:
        p = Path(cwd)
        if not p.is_absolute() or not p.is_dir():
            raise UsageError("--cwd must be an absolute path to an existing directory")
    for flag in ("timeout", "budget", "idle_grace"):
        value = getattr(args, flag, None)
        if value is not None and value < 0:
            raise UsageError(f"--{flag.replace('_', '-')} must not be negative")
    return Options(
        message=message,
        mode=mode,
        agent=args.agent or "",
        model=args.model or "",
        approval_mode=args.approval_mode or "",
        until_done=bool(args.until_done),
        idle_grace=900.0 if args.idle_grace is None else float(args.idle_grace),
        cwd=cwd,
        timeout=float(args.timeout or 0),
        budget=float(args.budget or 0),
        json=bool(args.json),
    )


def refuse_headless_flags_without_headless(args: argparse.Namespace) -> str:
    """The first headless-only flag set without ``--headless``, or ``""``."""
    defaults = {"mode": None, "idle_grace": None}
    for name in HEADLESS_ONLY:
        value = getattr(args, name, None)
        if value not in (None, False, "", 0) and value != defaults.get(name, None):
            return "--" + name.replace("_", "-")
    if getattr(args, "message_file", None):
        return "--message-file"
    return ""


# ---------- conductor agent ----------
def resolve_conductor_agent(cfg: Any, gateway_resolved: str = "") -> dict[str, str]:
    """The agent conductor mode runs, and where that name came from.

    The name is the one the GATEWAY resolves (``crew_mode_agent`` in
    ``GET /api/agents/catalog``, which is ``crew_mode.crew_mode_agent`` in that
    process): a configured name the migration found unusable is replaced by the
    conductor last applied only in the gateway's memory (``set_unusable``), so this
    process alone would send the unusable name. Without a gateway answer it falls back
    to ``crew_mode.crew_mode_agent(cfg)`` here. Source: ``default`` (nothing
    configured), ``config`` (the configured name), ``fallback`` (it was replaced).
    """
    from kiro_crew import crew_mode

    configured = crew_mode.configured_crew_mode_agent(cfg)
    resolved = gateway_resolved or crew_mode.crew_mode_agent(cfg)
    raw = getattr(getattr(cfg, "agent", None), "crew_mode_agent", "")
    raw = raw.strip() if isinstance(raw, str) else ""
    if resolved != configured:
        source = "fallback"
    elif not raw or raw == crew_mode.DEFAULT_CREW_MODE_AGENT:
        source = "default"
    else:
        source = "config"
    return {"name": resolved, "source": source, "configured": configured}


# ---------- gateway client ----------
class GatewayClient:
    """Owner requests to the local gateway over the peer-verified transport."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._cookie = ""
        self._token = ""

    def _open(self, req: urllib.request.Request, timeout: float, operation: str):
        from kiro_crew import cli_server_peer as peer

        return peer._owner_verified_secret_urlopen(req, self.port, timeout, operation=operation)

    @property
    def base(self) -> str:
        from kiro_crew import cli_server_peer as peer

        return f"http://{peer._CLI_LOOPBACK}:{self.port}"

    def sign_in(self) -> None:
        """Mint a one-minute owner link token and trade it for the session cookie."""
        from kiro_crew import cli_server_peer as peer
        from kiro_crew.config.loader import read_local_secret

        secret = read_local_secret(self.port, dial_host=peer._CLI_LOOPBACK)
        if not secret:
            raise NoGateway(f"no gateway secret for port {self.port}")
        try:
            mint = urllib.request.Request(
                f"{self.base}/api/token/local?ttl=1m", headers={"X-Local-Secret": secret}
            )
            with self._open(mint, 10, "cli_headless_token") as resp:
                self._token = json.loads(resp.read())["token"]
            prime = urllib.request.Request(
                f"{self.base}/api/status?token={urllib.parse.quote(self._token, safe='')}"
            )
            with self._open(prime, 10, "cli_headless_session") as resp:
                jar = http.cookies.SimpleCookie()
                for raw in resp.headers.get_all("Set-Cookie") or []:
                    jar.load(raw)
                name = f"mc_token_{self.port}"
                self._cookie = f"{name}={jar[name].value}" if name in jar else ""
        except urllib.error.HTTPError as exc:
            # A gateway that answered and refused is a real failure, not an absent one.
            raise RuntimeError(
                f"gateway on port {self.port} refused sign-in: HTTP {exc.code}"
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            raise NoGateway(f"gateway on port {self.port} not reachable: {exc}") from None

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> Any:
        """One owner request; JSON answer. Re-signs once when the session lapsed."""
        for attempt in (0, 1):
            url = self.base + path
            if not self._cookie:
                url += (
                    ("&" if "?" in path else "?")
                    + "token="
                    + urllib.parse.quote(self._token, safe="")
                )
            req = urllib.request.Request(
                url,
                method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    **({"Cookie": self._cookie} if self._cookie else {}),
                },
            )
            try:
                with self._open(req, timeout, "cli_headless") as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self.sign_in()
                    continue
                detail = exc.read(2000).decode(errors="replace")
                raise RuntimeError(f"{method} {path}: HTTP {exc.code} {detail[:300]}") from None
            except (urllib.error.URLError, OSError) as exc:
                raise NoGateway(f"lost the gateway on port {self.port}: {exc}") from None
        raise RuntimeError(f"{method} {path}: session could not be renewed")


# ---------- reading the run ----------
def transcript_path(slot: str) -> Path:
    from kiro_crew.config.paths import data_home

    return Path(data_home()) / "sessions" / f"dashboard_{slot}.jsonl"


def transcript_credits(path: Path) -> float:
    """Sum of ``meta.turn_stats.credits`` over a slot's transcript; 0 when absent."""
    total = 0.0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        meta = entry.get("meta") if isinstance(entry, dict) else None
        stats = meta.get("turn_stats") if isinstance(meta, dict) else None
        value = stats.get("credits") if isinstance(stats, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    return total


def ledger_items(client: "GatewayClient", slot: str) -> list[dict[str, Any]]:
    """The work-ledger items of a conductor slot, from the gateway's board read.

    ``GET /api/crew-board?conductor=KEY`` is the owner's read of a conductor's ledger
    (the Crew page's), so the store is never opened from this process. Before the
    conductor writes its first item the gateway answers ``no_ledger``: no items yet.
    """
    try:
        board = client.call("GET", f"/api/crew-board?conductor={urllib.parse.quote(slot)}")
    except RuntimeError as exc:
        if "no_ledger" in str(exc):
            return []
        raise
    items = (board or {}).get("items") if isinstance(board, dict) else None
    return [i for i in items or [] if isinstance(i, dict)]


def item_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    from kiro_crew.dashboard.conductor_mode_run import item_counts as counts

    open_items, _accepted, _not_done = counts(i.get("state") for i in items)
    return {"total": len(items), "terminal": len(items) - open_items, "open": open_items}


def worker_slots(items: list[dict[str, Any]]) -> set[str]:
    out = set()
    for item in items:
        key = item.get("worker_session_key")
        if isinstance(key, str) and key:
            out.add(key.split(":", 1)[1] if key.startswith("dashboard:") else key)
    return out


def turn_busy(row: dict[str, Any]) -> bool:
    """Whether the chat's own turn is in flight (its background agents aside).

    The ask checks read this: a turn that ended on a choice list asks a person even
    while an agent it started still runs.
    """
    return bool(row.get("running") or row.get("preparing") or row.get("compacting"))


def busy(row: dict[str, Any]) -> bool:
    """Whether the chat or its background work is still going; the run is not done."""
    return bool(
        turn_busy(row)
        or row.get("subagents_running")
        # Background work the gateway accepted but holds for capacity is still the run's.
        or row.get("subagents_queued")
    )


def last_agent_turn(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """``(role, text)`` of the last row after the newest user row, else ``("", "")``.

    A harness advisory notice (``is_harness_advisory_row``) is how a turn that a
    harness hook ended with only an advisory shows its output, so it reads as the
    turn's answer. The runner's empty-response notice (``EMPTY_TURN_NOTICE_KIND``)
    reads as an error: once the chat is idle, it is how a turn that gave up on an
    empty reply ends. Other notices are not turn output.
    """
    from kiro_crew.dashboard.chat_utils_injected_rows import EMPTY_TURN_NOTICE_KIND
    from kiro_crew.dashboard.system_notices import is_harness_advisory_row

    for msg in reversed(messages or []):
        role = msg.get("role")
        meta = msg.get("meta")
        if role == "notice" and is_harness_advisory_row(msg):
            role = "assistant"
        elif role == "notice" and isinstance(meta, dict):
            role = "error" if meta.get("kind") == EMPTY_TURN_NOTICE_KIND else role
        if role in ("assistant", "error", "system"):
            content = msg.get("content")
            return role, content if isinstance(content, str) else json.dumps(content)
        if role == "user":
            return "", ""
    return "", ""


def classify_failure(text: str) -> str:
    """For a turn that ended in an ``error`` row: which failure it names.

    ``not_signed_in`` / ``sandbox_refused`` when the error says so, else ``error``.
    Only error rows are read, so an agent reply that merely quotes these words is not
    a failure. shortcut: the gateway's error text is matched, not a typed code;
    switch to the code when the error row carries one.
    """
    low = text.lower()
    if any(m in low for m in _NOT_SIGNED_IN_MARKS):
        return "not_signed_in"
    if any(m in low for m in _SANDBOX_MARKS):
        return "sandbox_refused"
    return "error"


def asks_now(
    rows: dict[str, dict[str, Any]],
    run_slots: set[str],
    cards: list[dict[str, Any]],
    root_text: str,
    root_idle: bool,
    root: str = "",
) -> list[dict[str, str]]:
    """Every way the run is waiting for a person at this read."""
    events = []
    for key in sorted(run_slots):
        row = rows.get(key) or {}
        if row.get("pending_approval"):
            info = row.get("pending_approval_info")
            tool = info.get("tool") if isinstance(info, dict) else ""
            events.append({"kind": "approval", "slot": key, "text": str(tool or "")})
        if row.get("needs_input"):
            events.append({"kind": "question", "slot": key, "text": ""})
        if key != root and not turn_busy(row) and row.get("has_options"):
            # A child chat's idle turn that ends in a choice list, as the gateway
            # projects it (the root's own text is parsed below).
            choices = " | ".join(str(o) for o in row.get("options") or [])
            events.append({"kind": "options", "slot": key, "text": choices[:300]})
    for card in cards:
        key = card.get("slot") or card.get("slot_key") or ""
        if key in run_slots and not any(
            e["slot"] == key and e["kind"] == "question" for e in events
        ):
            questions = card.get("questions")
            first = questions[0] if isinstance(questions, list) and questions else {}
            text = first.get("question") if isinstance(first, dict) else ""
            events.append({"kind": "question", "slot": key, "text": str(text or "")[:300]})
    if root_idle and _parse_options(root_text):
        events.append({"kind": "options", "slot": "", "text": root_text[-300:]})
    return events


def _parse_options(text: str) -> list[str]:
    """The choices of the turn's last ``[OPTIONS: ...]`` line, via the dashboard's parser."""
    if _OPTIONS_MARK not in text:
        return []
    from kiro_crew.dashboard.state import _parse_options as parse

    return parse(text)


def descendant_slots(rows: dict[str, dict[str, Any]], roots: set[str]) -> set[str]:
    """*roots* plus every chat the gateway saw one of them create, transitively.

    ``created_by`` names the creating slot; only rows whose lineage this gateway minted
    (``lineage_minted``) count, so a transcript that names a creator is not trusted.
    """
    out = set(roots)
    grew = True
    while grew:
        grew = False
        for key, row in rows.items():
            parent = str(row.get("created_by") or "")
            parent = parent.split(":", 1)[1] if parent.startswith("dashboard:") else parent
            if key not in out and row.get("lineage_minted") and parent in out:
                out.add(key)
                grew = True
    return out


@dataclass
class Spawns:
    """The run's finished background agents: credits, and when each was first seen done."""

    credits: dict[str, float] = field(default_factory=dict)
    done_at: dict[str, float] = field(default_factory=dict)

    def delivering(self, now: float, grace: float) -> bool:
        """Whether an agent finished so recently that its result may still be on its way.

        The gateway holds a finished run's report while its session teardown runs
        (``subagent_manager.terminal``, at most ``_RESET_TIMEOUT +
        _TEARDOWN_REPORT_GRACE``), and only then wakes the parent with it; closing the
        parent in that window drops the result. shortcut: a fixed window from first
        seeing the run done; read a pending-delivery flag if the gateway publishes one.
        """
        return any(now - t < grace for t in self.done_at.values())


def spawn_credits(client: GatewayClient, slots: set[str], settled: Spawns) -> float:
    """Credits of finished background agents (``spawn_run``) the run's chats started.

    ``GET /api/spawn`` names each run's parent: a chat, or another run
    (``subagent:<id>``), so a run's own runs count too. A finished run's credits come
    from ``GET /api/spawn/{id}``, read once and kept in *settled*, because a
    delivered run leaves the list after its grace window. A run's credits are its
    own process's metering, so they do not overlap the parent's.
    shortcut: a run's credits are known when it ends (and a chat turn's when that
    turn ends), so a budget can be passed by what is still running; count live
    spend when the gateway publishes a running total.
    """
    try:
        agents = (client.call("GET", "/api/spawn") or {}).get("agents") or []
        parents = {str(a.get("id") or ""): _spawn_parent(a) for a in agents if a.get("id")}
        # A run's own runs (`subagent:<id>` parent) are the run's too, transitively.
        owned: set[str] = set()
        grew = True
        while grew:
            grew = False
            for agent_id, parent in parents.items():
                mine = parent in slots or parent in owned or parent in settled.credits
                if agent_id not in owned and mine:
                    owned.add(agent_id)
                    grew = True
        for agent in agents:
            agent_id = str(agent.get("id") or "")
            if agent.get("done") and agent_id in owned and agent_id not in settled.credits:
                credits = (client.call("GET", f"/api/spawn/{agent_id}") or {}).get("credits")
                if isinstance(credits, bool) or not isinstance(credits, (int, float)):
                    credits = 0.0
                settled.credits[agent_id] = float(credits)
                settled.done_at[agent_id] = time.time()
    except Exception:  # noqa: BLE001 - a failed read keeps what is already counted
        pass
    return sum(settled.credits.values())


def run_spend(client: GatewayClient, slots: set[str], since: float, settled: Spawns) -> float:
    """The run's credits: its chats' saved turns plus its finished background agents.

    Both halves use the same chat set, so the agents of a chat closed between two
    polls (found only from its transcript) count as well.
    """
    chats = spend_slots(slots, since)
    spent = sum(transcript_credits(transcript_path(k)) for k in chats)
    return spent + spawn_credits(client, chats, settled)


def _spawn_parent(agent: dict[str, Any]) -> str:
    """A ``GET /api/spawn`` row's parent as a slot key or a run id."""
    parent = str(agent.get("parent") or "")
    for prefix in ("dashboard:", "subagent:"):
        if parent.startswith(prefix):
            return parent[len(prefix) :]
    return parent


def spend_slots(roots: set[str], since: float) -> set[str]:
    """*roots* plus every chat whose saved transcript names one of them as creator.

    For the spend total only: a worker created and closed between two polls is gone
    from the live slot list, but its transcript stays in the data home with
    ``created_by`` on its metadata line. Only transcripts written since the run began
    are read, and only their first lines. ``created_by`` there is a claim, so it can
    only add spend, which stops a run sooner, never later.
    """
    sessions = transcript_path("x").parent
    try:
        files = [f for f in sessions.glob("dashboard_*.jsonl") if f.stat().st_mtime >= since]
    except OSError:
        return set(roots)
    creators: dict[str, str] = {}
    for f in files:
        key = f.name[len("dashboard_") : -len(".jsonl")]
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                head = [fh.readline() for _ in range(3)]
        except OSError:
            continue
        for line in head:
            try:
                meta = json.loads(line)
            except ValueError:
                continue
            if isinstance(meta, dict) and meta.get("_type") == "metadata":
                parent = str(meta.get("created_by") or "")
                creators[key] = (
                    parent.split(":", 1)[1] if parent.startswith("dashboard:") else parent
                )
                break
    out = set(roots)
    grew = True
    while grew:
        grew = False
        for key, parent in creators.items():
            if key not in out and parent in out:
                out.add(key)
                grew = True
    return out


def ledger_asks(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Items the board reads as waiting on the person (``awaits_person``), or with a recorded ask."""
    out = []
    for item in items:
        ask = item.get("ask")
        if item.get("awaits_person") or (ask and item.get("state") == "open"):
            text = (ask.get("what") or "") if isinstance(ask, dict) else ""
            out.append(
                {
                    "kind": "needs_human",
                    "slot": "",
                    "text": str(text or item.get("title", ""))[:300],
                }
            )
    return out


def _progress(poll_n: int, res: Result, any_busy: bool) -> None:
    """One stderr line per status read, so a caller can see the run is alive."""
    print(
        f"headless: poll={poll_n} busy={int(any_busy)} items_open={res.items.get('open', 0)} "
        f"items_total={res.items.get('total', 0)} wall_s={round(res.wall_s, 1)}",
        file=sys.stderr,
        flush=True,
    )


# ---------- the run ----------
def run(
    opts: Options,
    client: GatewayClient,
    cfg: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_secs: float | None = None,
) -> Result:
    poll = POLL_SECS if poll_secs is None else poll_secs
    res = Result(mode=opts.mode, approval_mode=opts.approval_mode or "none", cwd=opts.cwd)
    t0 = clock()
    if getattr(getattr(cfg, "work_ledger", None), "remote_dispatch", False) is True:
        # A worker placed on a remote crew is reachable only through the conductor's
        # own session tools, so this CLI could not stop it on timeout or budget.
        # shortcut: refused while `work_ledger.remote_dispatch` is on (read once, at
        # start); stop peer workers through the hub when headless runs need crews.
        res.error = (
            "headless runs need work_ledger.remote_dispatch off: remote workers cannot be stopped"
        )
        return res
    client.sign_in()
    if opts.mode == "conductor":
        catalog = client.call("GET", "/api/agents/catalog") or {}
        res.agent = resolve_conductor_agent(cfg, str(catalog.get("crew_mode_agent") or ""))
        agent, agent_kind = res.agent["name"], "template"
    else:
        agent, agent_kind = opts.agent, ""
        res.agent = {
            "name": opts.agent,
            "source": "flag" if opts.agent else "default",
            "configured": "",
        }

    create: dict[str, Any] = {
        "title": "headless " + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    if agent:
        create["agent"] = agent
    if agent_kind:
        create["agent_kind"] = agent_kind
    if opts.cwd:
        # The run sets the project itself right after the create (`--cwd`), so the
        # owner's `session_folder_root` must not mint a folder it would abandon.
        create["no_session_folder"] = True
    slot = client.call("POST", "/api/chat/slots", create)
    res.slot = str(slot.get("key") or "")
    if not res.slot:
        res.error = "slot create returned no key"
        return res
    res.transcript = str(transcript_path(res.slot))
    run_slots = {res.slot}
    # Wall clock, for the transcripts' mtimes (``clock`` is monotonic); a minute of slack.
    since = time.time() - 60
    settled = Spawns()
    # From here the run owns a chat: any way out stops what it started, so a trusted
    # chat or its workers never keep running and spending after the CLI exits.
    try:
        res = _seed_and_poll(
            opts, client, res, slot, run_slots, t0, clock, sleep, poll, since, settled
        )
    except KeyboardInterrupt:
        res = _stop(client, run_slots, res, "interrupted")
    except NoGateway as exc:
        res.error = str(exc)
        res = _stop(client, run_slots, res, "no_gateway")
    except Exception as exc:  # noqa: BLE001 - every failure ends the run cleanly
        res.error = f"{type(exc).__name__}: {exc}"[:500]
        res = _stop(client, run_slots, res, "error")
    # Closing a chat saves the stopped turn's usage, and `_stop` may have found
    # more chats: count the spend again now that it is all on disk.
    res.credits = max(res.credits, run_spend(client, run_slots, since, settled))
    return res


def _seed_and_poll(
    opts: Options,
    client: GatewayClient,
    res: Result,
    slot: dict[str, Any],
    run_slots: set[str],
    t0: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll: float,
    since: float,
    settled: Spawns,
) -> Result:
    created_agent = str(slot.get("agent") or "")
    if opts.mode == "conductor":
        if not slot.get("crew_mode_agent"):
            res.error = f"slot {res.slot} did not open in conductor mode (agent {created_agent!r})"
            return _stop(client, run_slots, res, "error")
        if created_agent and created_agent != res.agent["name"]:
            res.agent = {
                "name": created_agent,
                "source": "fallback",
                "configured": res.agent["configured"],
            }
    elif created_agent:
        res.agent["name"] = created_agent
    if opts.cwd:
        # The chat's own project, not a folder: a folder per run would outlive it
        # and fill the owner's folder cap.
        client.call("POST", f"/api/chat/slots/{res.slot}/project", {"project": opts.cwd})
    if opts.approval_mode == "trust":
        client.call("POST", "/api/chat/mode", {"slot": res.slot, "mode": "trust"})
    if opts.model:
        client.call("POST", f"/api/chat/slots/{res.slot}/model", {"model": opts.model})
    client.call(
        "POST",
        "/api/chat?ws=1",
        {
            "message": opts.message,
            "slot": res.slot,
            "agent": created_agent,
            # The gateway opens this turn with its own `[headless run cli:<id>]`
            # marker: nobody is at the dashboard, so a conductor skips its plan gate.
            "unattended": True,
            "run_id": res.run_id,
        },
    )
    return _poll(opts, client, res, run_slots, t0, clock, sleep, poll, since, settled)


def _poll(
    opts: Options,
    client: GatewayClient,
    res: Result,
    run_slots: set[str],
    t0: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll: float,
    since: float,
    settled: Spawns,
) -> Result:
    idle_since: float | None = None
    poll_n = 0
    while True:
        sleep(poll)
        poll_n += 1
        now = clock()
        res.wall_s = now - t0
        # crew=all: an owner's plain slot list hides Crew Mode workers (crew_mode.list_view).
        rows = _slot_rows(client)
        items = ledger_items(client, res.slot) if opts.mode == "conductor" else []
        res.items = item_counts(items) if opts.mode == "conductor" else {}
        # Ledger workers, and every chat the run's chats created (session_create),
        # in both modes: their activity, asks, credits and stops are the run's.
        run_slots |= descendant_slots(rows, run_slots | worker_slots(items))
        any_busy = any(busy(rows.get(k) or {}) for k in run_slots)
        detail = client.call("GET", f"/api/chat/slots/{res.slot}")
        role, root_text = last_agent_turn((detail or {}).get("messages") or [])
        root_idle = not turn_busy(rows.get(res.slot) or {})
        cards = (client.call("GET", "/api/ask-question/pending") or {}).get("pending") or []
        res.credits = run_spend(client, run_slots, since, settled)
        # A background agent's result may still be on its way to its parent.
        any_busy = any_busy or settled.delivering(time.time(), _DELIVERY_GRACE_SECS + poll)
        _progress(poll_n, res, any_busy)

        events = asks_now(
            rows, run_slots, cards, root_text, root_idle and bool(root_text), res.slot
        )
        events += ledger_asks(items)
        if events:
            res.asks += len(events)
            res.ask_events.extend(dict(e, t_s=str(round(res.wall_s, 1))) for e in events)
            return _stop(client, run_slots, res, "asked")
        if opts.budget and res.credits >= opts.budget:
            return _stop(client, run_slots, res, "budget")
        if opts.timeout and res.wall_s >= opts.timeout:
            return _stop(client, run_slots, res, "timeout")
        if any_busy or not root_text:
            idle_since = None
            continue
        if role in ("error", "system"):
            # An error row, or a system row the gateway writes when it refuses the
            # turn before dispatch (a blocked @prompt): the seed did not run to the end.
            res.error = root_text[:300]
            return _stop(client, run_slots, res, classify_failure(root_text))
        if not opts.until_done:
            res.completed = True
            return _stop(client, run_slots, res, "done")
        idle_since = now if idle_since is None else idle_since
        if opts.mode == "conductor" and res.items.get("total") and not res.items.get("open"):
            res.completed = True
            return _stop(client, run_slots, res, "done")
        if now - idle_since >= opts.idle_grace:
            # Quiet for the whole grace window: no patrol, monitor or worker woke it.
            # A conductor that went quiet with items still open did not finish.
            res.completed = opts.mode != "conductor" or not res.items.get("open")
            return _stop(client, run_slots, res, "done" if res.completed else "incomplete")


def _slot_rows(client: GatewayClient) -> dict[str, dict[str, Any]]:
    """Every chat row by key, Crew Mode workers included."""
    raw = client.call("GET", "/api/chat/slots?crew=all")
    rows = raw.get("slots", []) if isinstance(raw, dict) else raw or []
    return {str(r.get("key")): r for r in rows if isinstance(r, dict)}


def _halt(client: GatewayClient, key: str) -> None:
    """Delete one chat's automation loop, then stop its turn (forced when declined)."""
    try:
        loop = (client.call("GET", f"/api/autonudge/slot/{key}") or {}).get("loop")
        loop_id = loop.get("id") if isinstance(loop, dict) else None
        if loop_id:
            client.call("DELETE", f"/api/autonudge/{loop_id}")
    except Exception:  # noqa: BLE001 - best effort, the stop below still runs
        pass
    try:
        answer = client.call("POST", f"/api/chat/slots/{key}/stop", {}) or {}
        if answer.get("compacting") or not answer.get("ok", True):
            client.call("POST", f"/api/chat/slots/{key}/stop?force=true", {})
    except Exception:  # noqa: BLE001 - a slot already idle may refuse a stop
        pass
    try:
        # A turn stop leaves background agents running; cancel them here, apart
        # from the close below, which a pending history write can refuse.
        client.call("POST", "/api/spawn/stop-all", {"slot": key})
    except Exception:  # noqa: BLE001 - a chat with no agents (or gone) may refuse
        pass


# shortcut: a chat that keeps creating children faster than this many re-reads
# could leave one; raise it if a real run ever shows that.
_STOP_ROUNDS = 5


def _stop(client: GatewayClient, slots: set[str], res: Result, status: str) -> Result:
    """End the run as *status*: disarm each chat's automation, stop its turn, close it.

    Every exit after the chat exists comes here, a clean ``done`` included, so nothing
    the run started (an armed monitor, a running turn) outlives it. The automation
    loop bound to a chat is deleted first so it cannot start another turn. A stop the
    gateway declines (the session is compacting) or leaves pending is pressed once
    more with ``force``, the dashboard's own second-press escape to a hard kill. The
    chat's Trust is left as is: the owner route's ``normal`` would also end a
    process-wide YOLO grant, and a stopped chat with no automation starts no turn.
    *slots* is the last poll's view, so after each round of stops (root first) the
    slot list is read again and any chat created since then is stopped too; *slots*
    grows in place, so the caller's final spend count covers those chats.
    Best effort: a chat that is already gone or idle may refuse any of these calls.
    """
    res.status = status
    halted: set[str] = set()
    for _ in range(_STOP_ROUNDS):
        todo = sorted(slots - halted, key=lambda k: (k != res.slot, k))
        if not todo:
            break
        for key in todo:
            _halt(client, key)
            halted.add(key)
        try:
            slots |= descendant_slots(_slot_rows(client), slots)
        except Exception:  # noqa: BLE001 - no fresh list: close what is known
            break
    # Then close the chats, root last: a turn stop leaves background subagents
    # running, and the close (`DELETE /api/chat/slots/{key}`) tears them down, takes
    # a conductor's workers with it, and saves the history the transcript path names.
    for key in sorted(slots - {res.slot}) + ([res.slot] if res.slot in slots else []):
        try:
            client.call("DELETE", f"/api/chat/slots/{key}")
        except Exception as exc:  # noqa: BLE001 - a chat already closed refuses (404)
            if "HTTP 404" not in str(exc):
                # Its automation, turn and agents were stopped above; the chat
                # itself stays open, so the result names it.
                res.unclosed.append(key)
    if res.unclosed:
        note = "not closed: " + ", ".join(res.unclosed)
        res.error = f"{res.error}; {note}" if res.error else note
    return res


# ---------- entry ----------
def main(args: argparse.Namespace) -> int:
    """Run ``kirocrew chat --headless``; print the result; return the exit code."""
    try:
        opts = options_from_args(args)
    except UsageError as exc:
        print(f"kirocrew chat --headless: {exc}", file=sys.stderr)
        return EXIT_USAGE
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.port_resolution import resolve_client_port

    cfg = KiroCrewConfig.load()
    res = Result(mode=opts.mode, approval_mode=opts.approval_mode or "none", cwd=opts.cwd)
    try:
        res = run(opts, GatewayClient(resolve_client_port(getattr(args, "port", None))), cfg)
    except NoGateway as exc:
        res.status, res.error = "no_gateway", str(exc)
    except KeyboardInterrupt:
        res.status = "interrupted"
    except Exception as exc:  # noqa: BLE001 - --json always prints one object
        res.status, res.error = "error", f"{type(exc).__name__}: {exc}"[:500]
    payload = res.to_json()
    if opts.json:
        print(json.dumps(payload))
    else:
        print(
            f"{res.status}: asks={res.asks} wall={payload['wall_s']}s credits={payload['credits']} "
            f"slot={res.slot} transcript={res.transcript}"
            + (f" error={res.error}" if res.error else ""),
            file=sys.stderr,
        )
    return payload["exit_code"]

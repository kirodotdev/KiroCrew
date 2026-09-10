"""Opt-in, fixed-identity App Kit consumer for approved UCAM projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

APP_NAME = "ucam-synthetic-consumer"
AGENT_NAME = "ucam-synthetic-reader"
ADAPTER_VERSION = "kirocrew-ucam/7"
CONFIG_PATH = Path("/opt/ucam-consumer/config.json")
MAX_JSON_BYTES = 1048576
MAX_PAYLOAD_BYTES = 262144
MAX_TASK_BYTES = 16384
MAX_RESULT_BYTES = 65536
TURN_TIMEOUT = 120
RESERVATION_TIMEOUT = 180
MAX_REQUESTS = 32
IO_TIMEOUT = 2.5
LEASE_SECONDS = 900
LEASE_MARGIN = 1.0
SAFE_INTEGER = 9007199254740991
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
_active_run: ContextVar[ConsumerRun | None] = ContextVar("ucam_consumer_run", default=None)


class ConsumerError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ConsumerError(code)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(text: str):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            _require(name not in result, "ucam_duplicate_key")
            result[name] = value
        return result

    def nonfinite(value):
        raise ConsumerError("ucam_nonfinite_number")

    try:
        _require(len(text.encode("utf-8")) <= MAX_JSON_BYTES, "ucam_response_limit")
        return json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ConsumerError("ucam_invalid_json") from error


def _same_json(left, right, depth=0) -> bool:
    if depth > 32:
        return False
    if type(left) in (int, float) and type(right) in (int, float):
        return (
            all(not isinstance(value, float) or math.isfinite(value) for value in (left, right))
            and left == right
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_json(value, right[name], depth + 1) for name, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_json(value, right[index], depth + 1) for index, value in enumerate(left)
        )
    return (left is None or type(left) in (str, bool)) and left == right


def _integer(value, minimum=0) -> bool:
    return type(value) is int and minimum <= value <= SAFE_INTEGER


@dataclass(frozen=True)
class Binding:
    api_url: str
    scope: str
    owner_sub: str
    workspace: str
    generation: str
    region: str
    credentials_file: str
    state_dir: str

    @classmethod
    def read(cls, path: Path = CONFIG_PATH) -> Binding:
        data = _json(path.read_text(encoding="utf-8"))
        names = set(cls.__dataclass_fields__)
        _require(isinstance(data, dict) and data.keys() == names | {"enabled"}, "ucam_config")
        _require(data["enabled"] is True, "ucam_disabled")
        _require(all(isinstance(data[name], str) and data[name] for name in names), "ucam_config")
        binding = cls(**{name: data[name] for name in names})
        url = urlsplit(binding.api_url)
        _require(
            url.scheme == "https"
            and bool(url.hostname)
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            "ucam_api_url",
        )
        _require(bool(IDENTIFIER.fullmatch(binding.scope)), "ucam_scope")
        _require(0 < len(binding.generation) <= 256, "ucam_generation")
        _require(Path(binding.credentials_file).is_absolute(), "ucam_credentials_path")
        _require(Path(binding.state_dir).is_absolute(), "ucam_state_path")
        return binding


async def load_binding() -> Binding:
    try:
        return await asyncio.wait_for(asyncio.to_thread(Binding.read, CONFIG_PATH), IO_TIMEOUT)
    except (OSError, ValueError, TypeError, asyncio.TimeoutError) as error:
        raise ConsumerError("ucam_config_unavailable") from error


class RunStore:
    """Durable app-bound at-most-once reservations; ambiguous dispatch is never retried."""

    def __init__(self, binding):
        self.path = Path(binding.state_dir) / "runs.sqlite3"
        self.namespace = _sha(
            json.dumps(
                [APP_NAME, binding.scope, binding.owner_sub, binding.workspace, binding.generation]
            )
        )

    def _call(self, operation, *args):
        with sqlite3.connect(self.path, timeout=IO_TIMEOUT) as database:
            database.row_factory = sqlite3.Row
            database.execute(
                "CREATE TABLE IF NOT EXISTS runs (namespace TEXT, request_id TEXT, "
                "task_hash TEXT, run_id TEXT, phase TEXT, text TEXT, outcome TEXT, "
                "created REAL, evidence TEXT, PRIMARY KEY(namespace, request_id), "
                "UNIQUE(namespace, run_id))"
            )
            database.execute("BEGIN IMMEDIATE")
            if operation == "reserve":
                request_id, task = args
                row = database.execute(
                    "SELECT * FROM runs WHERE namespace=? AND request_id=?",
                    (self.namespace, request_id),
                ).fetchone()
                if row:
                    _require(row["task_hash"] == _sha(task), "ucam_request_conflict")
                    return dict(row), False
                count = database.execute(
                    "SELECT count(*) FROM runs WHERE namespace=?", (self.namespace,)
                ).fetchone()[0]
                _require(count < MAX_REQUESTS, "ucam_request_limit")
                database.execute(
                    "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        self.namespace,
                        request_id,
                        _sha(task),
                        None,
                        "queued",
                        "",
                        "",
                        time.time(),
                        "{}",
                    ),
                )
                return {"run_id": None, "phase": "queued"}, True
            if operation == "bind":
                request_id, run_id = args
                changed = database.execute(
                    "UPDATE runs SET run_id=? WHERE namespace=? AND request_id=? AND run_id IS NULL",
                    (run_id, self.namespace, request_id),
                ).rowcount
                _require(changed == 1, "ucam_run_registration")
                return None
            if operation == "finish":
                run_id, phase, text, outcome, evidence = args
                existing = database.execute(
                    "SELECT phase,outcome,evidence FROM runs WHERE namespace=? AND run_id=?",
                    (self.namespace, run_id),
                ).fetchone()
                if (
                    existing
                    and existing["phase"] == "failed"
                    and existing["outcome"] == "ucam_run_deadline"
                ):
                    prior = json.loads(existing["evidence"])
                    merged = {**prior, **evidence}
                    for name in (
                        "native_sent",
                        "fetched_ack",
                        "injected_ack",
                        "turn_result_ack",
                        "ack_failed",
                    ):
                        if prior.get(name) is True:
                            merged[name] = True
                    database.execute(
                        "UPDATE runs SET evidence=? WHERE namespace=? AND run_id=?",
                        (json.dumps(merged), self.namespace, run_id),
                    )
                    return None
                changed = database.execute(
                    "UPDATE runs SET phase=?,text=?,outcome=?,evidence=? "
                    "WHERE namespace=? AND run_id=?",
                    (phase, text, outcome, json.dumps(evidence), self.namespace, run_id),
                ).rowcount
                _require(changed == 1, "ucam_run_registration")
                return None
            row = database.execute(
                "SELECT * FROM runs WHERE namespace=? AND run_id=?", (self.namespace, args[0])
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            if (
                result["phase"] in ("queued", "running")
                and time.time() > result["created"] + RESERVATION_TIMEOUT
            ):
                database.execute(
                    "UPDATE runs SET phase='failed',outcome='ucam_run_deadline' "
                    "WHERE namespace=? AND run_id=?",
                    (self.namespace, args[0]),
                )
                result.update(phase="failed", outcome="ucam_run_deadline")
            return result

    async def call(self, operation, *args):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._call, operation, *args), IO_TIMEOUT + 0.5
            )
        except (OSError, sqlite3.Error, asyncio.TimeoutError) as error:
            raise ConsumerError("ucam_registry_unavailable") from error

    async def registered(self, run_id):
        deadline = time.monotonic() + IO_TIMEOUT
        while time.monotonic() < deadline:
            row = await self.call("get", run_id)
            if row is not None:
                _require(row["phase"] == "queued", "ucam_run_registration")
                return row
            await asyncio.sleep(0.025)
        raise ConsumerError("ucam_run_registration")


def verify_projection(projection, binding: Binding, now: float):
    names = {"scope", "generation", "epoch", "records"}
    _require(
        isinstance(projection, dict)
        and projection.keys() == names | {"digest", "canonical_payload", "valid_until"},
        "ucam_projection_fields",
    )
    text = projection["canonical_payload"]
    digest = projection["digest"]
    _require(isinstance(text, str) and isinstance(digest, str), "ucam_projection_digest")
    _require(len(text.encode("utf-8")) <= MAX_PAYLOAD_BYTES, "ucam_projection_limit")
    _require(bool(HASH.fullmatch(digest)) and _sha(text) == digest, "ucam_projection_digest")
    material = _json(text)
    _require(
        isinstance(material, dict)
        and material.keys() == names
        and _same_json(material, {name: projection[name] for name in names}),
        "ucam_projection_material",
    )
    _require(material["scope"] == binding.scope, "ucam_scope")
    _require(material["generation"] == binding.generation, "ucam_generation")
    _require(_integer(material["epoch"]), "ucam_epoch")
    valid_until = projection["valid_until"]
    _require(
        _integer(valid_until) and now + LEASE_MARGIN < valid_until <= now + LEASE_SECONDS,
        "ucam_lease",
    )
    _require(isinstance(material["records"], list), "ucam_records")
    record_ids = set()
    for record in material["records"]:
        _require(isinstance(record, dict), "ucam_record")
        _require(
            record.get("scope") == binding.scope
            and record.get("status") == "approved"
            and record.get("quarantined") is False
            and record.get("delivery") == "standing"
            and _integer(record.get("revision"), 1),
            "ucam_record_policy",
        )
        context = record.get("scope_context")
        _require(
            isinstance(context, dict)
            and context.get("user") == binding.owner_sub
            and context.get("workspace") == binding.workspace
            and record.get("synthetic") is True,
            "ucam_owner_workspace_binding",
        )
        for expiry in (record.get("expires_at"), record.get("revalidate_at", valid_until)):
            _require(_integer(expiry) and expiry >= valid_until, "ucam_record_expiry")
        exchange = record.get("exchange")
        _require(isinstance(exchange, dict), "ucam_exchange")
        identifier = exchange.get("id")
        _require(
            isinstance(identifier, str)
            and bool(IDENTIFIER.fullmatch(identifier))
            and identifier not in record_ids
            and isinstance(exchange.get("claim"), str),
            "ucam_exchange",
        )
        record_ids.add(identifier)
    return material


class SignedAPI:
    def __init__(self, binding: Binding):
        self.binding = binding

    async def call(self, path: str, body=None, key: str = ""):
        async def request():
            import aiohttp
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials

            credentials = await asyncio.to_thread(
                lambda: _json(Path(self.binding.credentials_file).read_text(encoding="utf-8"))
            )
            _require(
                credentials.keys() == {"access_key", "secret_key", "token", "expires_at"}
                and _integer(credentials["expires_at"])
                and credentials["expires_at"] > time.time() + IO_TIMEOUT
                and all(
                    isinstance(credentials[name], str) and credentials[name]
                    for name in ("access_key", "secret_key", "token")
                ),
                "ucam_credentials",
            )
            url = self.binding.api_url.rstrip("/") + "/iam/v1/" + self.binding.scope + path
            method = "POST" if body is not None else "GET"
            payload = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
            headers = {"Content-Type": "application/json"}
            if body is not None:
                headers["Idempotency-Key"] = key
            signed = AWSRequest(method=method, url=url, data=payload, headers=headers)
            SigV4Auth(
                Credentials(
                    credentials["access_key"], credentials["secret_key"], credentials["token"]
                ),
                "execute-api",
                self.binding.region,
            ).add_auth(signed)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=IO_TIMEOUT)
            ) as session:
                async with session.request(
                    method,
                    url,
                    data=payload,
                    headers=dict(signed.headers),
                    allow_redirects=False,
                ) as response:
                    _require(200 <= response.status < 300, "ucam_api_rejected")
                    chunks = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        chunks.extend(chunk)
                        _require(len(chunks) <= MAX_JSON_BYTES, "ucam_response_limit")
                    return _json(chunks.decode("utf-8"))

        try:
            return await asyncio.wait_for(request(), IO_TIMEOUT)
        except ConsumerError:
            raise
        except Exception as error:
            raise ConsumerError("ucam_api_unavailable") from error


def synthetic_app(info) -> bool:
    return getattr(info, "app", "") == APP_NAME


async def consumer_for(info, is_new: bool, resumed: bool, is_cc: bool):
    if not synthetic_app(info):
        return None
    _require(getattr(info, "agent", "") == AGENT_NAME, "ucam_agent")
    _require(
        is_new
        and not resumed
        and not is_cc
        and not getattr(info, "keep", False)
        and not getattr(info, "conversation_key", "")
        and not getattr(info, "_cancel_retry_used", False),
        "ucam_fresh_session_required",
    )
    binding = await load_binding()
    run = ConsumerRun(binding, str(info.id), SignedAPI(binding))
    run.store = RunStore(binding)
    registered = await run.store.registered(run.run_id)
    run.registration_expires_at = registered["created"] + RESERVATION_TIMEOUT
    return run


class ConsumerRun:
    def __init__(self, binding: Binding, run_id: str, api, clock=time.time):
        self.binding = binding
        self.run_id = run_id
        self.store: RunStore | None = None
        self.run_hash = _sha(APP_NAME + ":" + run_id)
        self.api = api
        self.clock = clock
        self.projection: dict | None = None
        self.transport = None
        self.native_session_id = ""
        self.sent = False
        self.ack_failed = False
        self.used = False
        self.prompt_hash = ""
        self.acked_phases: list[str] = []
        self.registration_expires_at: float | None = None

    def evidence(self):
        projection = self.projection or {}
        return {
            "adapter": ADAPTER_VERSION,
            "run_hash": self.run_hash,
            "digest": projection.get("digest", ""),
            "generation": projection.get("generation", ""),
            "epoch": projection.get("epoch"),
            "prompt_hash": self.prompt_hash,
            "native_sent": self.sent,
            "fetched_ack": "fetched" in self.acked_phases,
            "injected_ack": "injected" in self.acked_phases,
            "turn_result_ack": "turn_result" in self.acked_phases,
            "ack_failed": self.ack_failed,
        }

    def _reserve(self):
        path = Path(self.binding.state_dir) / (self.run_hash + ".attempt")
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(ADAPTER_VERSION + "\n")
        except FileExistsError as error:
            raise ConsumerError("ucam_run_already_attempted") from error

    async def ack(self, phase: str, result: str = ""):
        projection = self.projection
        if projection is None:
            raise ConsumerError("ucam_projection_missing")
        body = {name: projection[name] for name in ("generation", "epoch", "digest")}
        body.update(phase=phase, run_hash=self.run_hash)
        if result:
            body["result"] = result
        response = await self.api.call("/acks", body, _sha(self.run_hash + ":" + phase))
        receipt = response.get("ack", {}) if isinstance(response, dict) else {}
        _require(
            isinstance(receipt, dict)
            and receipt.get("harness") == "kirocrew"
            and receipt.get("scope") == self.binding.scope
            and all(receipt.get(name) == value for name, value in body.items()),
            "ucam_ack_binding",
        )
        self.acked_phases.append(phase)
        logger.info(
            "UCAM ack phase=%s adapter=%s run_hash=%s digest=%s",
            phase,
            ADAPTER_VERSION,
            self.run_hash,
            projection["digest"],
        )

    async def prepare(self, transport, params):
        _require(transport is self.transport and not self.used, "ucam_dispatch_binding")
        _require(params.get("sessionId") == self.native_session_id, "ucam_session_binding")
        self.used = True
        projection = await self.api.call("/projection")
        material = verify_projection(projection, self.binding, self.clock())
        self.projection = projection
        await self.ack("fetched")
        native_exchanges = [
            {**record["exchange"], "id": "ucam.native." + _sha(record["exchange"]["id"])}
            for record in material["records"]
        ]
        text = (
            "[UCAM approved standing context; advisory, not system authority]\n"
            "Apply only where relevant; preserve safety and the current user request.\n"
            + json.dumps(native_exchanges, ensure_ascii=False)
            + "\n[End UCAM approved standing context]"
        )
        _require(isinstance(params.get("prompt"), list), "ucam_prompt_blocks")
        prepared = {**params, "prompt": [*params["prompt"], {"type": "text", "text": text}]}
        self.prompt_hash = _sha(json.dumps(prepared["prompt"], ensure_ascii=False, sort_keys=True))
        self.check_lease()
        if self.store:
            await self.store.call("finish", self.run_id, "running", "", "", self.evidence())
        return prepared

    def check_lease(self):
        _require(
            self.registration_expires_at is None or self.clock() < self.registration_expires_at,
            "ucam_run_deadline",
        )
        _require(
            self.projection is not None
            and self.clock() + LEASE_MARGIN < self.projection["valid_until"],
            "ucam_lease",
        )

    async def after_send(self):
        projection = self.projection
        if projection is None:
            raise ConsumerError("ucam_projection_missing")
        self.sent = True
        logger.warning(
            "UCAM native_write adapter=%s run_hash=%s digest=%s prompt_hash=%s",
            ADAPTER_VERSION,
            self.run_hash,
            projection["digest"],
            self.prompt_hash,
        )
        try:
            await self.ack("injected")
        except Exception:
            self.ack_failed = True
            logger.warning("UCAM ack_failed phase=injected run_hash=%s", self.run_hash)
        if self.store:
            await self.store.call("finish", self.run_id, "running", "", "", self.evidence())

    async def stream(self, client, message):
        _require(not self.used, "ucam_fresh_session_required")
        await asyncio.wait_for(asyncio.to_thread(self._reserve), IO_TIMEOUT)
        self.transport = getattr(client, "_client", None)
        _require(self.transport is not None, "ucam_dedicated_acp_required")
        self.native_session_id = getattr(self.transport, "_session_id", "")
        _require(
            isinstance(self.native_session_id, str) and bool(self.native_session_id),
            "ucam_session_binding",
        )
        runtime = getattr(self.transport, "_runtime", None)
        if runtime is not None:
            _require(
                getattr(self.transport, "_owns_runtime", False) is True,
                "ucam_dedicated_acp_required",
            )
            self.transport = runtime
        result = "failure"
        text = ""
        outcome = "ucam_native_failed"
        finalized = False
        deadline = time.monotonic() + TURN_TIMEOUT
        iterator = client.stream(message)

        async def finish():
            nonlocal outcome, finalized
            if finalized:
                return
            if self.sent:
                try:
                    await self.ack("turn_result", result)
                except Exception:
                    self.ack_failed = True
                    logger.warning("UCAM ack_failed phase=turn_result run_hash=%s", self.run_hash)
            if result == "success" and self.ack_failed:
                outcome = "degraded"
            if self.store:
                await self.store.call(
                    "finish",
                    self.run_id,
                    "completed" if result in ("success", "degraded") else "failed",
                    text.encode("utf-8")[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore"),
                    outcome,
                    self.evidence(),
                )
            finalized = True

        try:
            if self.store:
                await self.store.call("finish", self.run_id, "running", "", "", {})
            while True:
                token = _active_run.set(self)
                try:
                    remaining = deadline - time.monotonic()
                    _require(remaining > 0, "ucam_native_timeout")
                    event = await asyncio.wait_for(iterator.__anext__(), remaining)
                except StopAsyncIteration:
                    break
                finally:
                    _active_run.reset(token)
                if getattr(event, "kind", "") == "text_chunk":
                    text += getattr(event, "text", "") or ""
                    _require(len(text.encode("utf-8")) <= MAX_RESULT_BYTES, "ucam_result_limit")
                if getattr(event, "kind", "") == "complete":
                    _require(self.sent, "ucam_native_receipt_missing")
                    _require(
                        getattr(event, "stop_reason", "") == "end_turn",
                        "ucam_native_terminal_failure",
                    )
                    result = "degraded" if self.ack_failed else "success"
                    outcome = result
                    await finish()
                    yield event
                    return
                yield event
            _require(self.sent, "ucam_native_receipt_missing")
            _require(runtime is None, "ucam_native_terminal_missing")
            result = "degraded" if self.ack_failed else "success"
            outcome = result
        except ConsumerError as error:
            outcome = error.code
            raise
        except asyncio.TimeoutError:
            outcome = "ucam_native_timeout"
            raise
        finally:
            try:
                await iterator.aclose()
            finally:
                await finish()


async def before_prompt(transport, method: str, params):
    run = _active_run.get()
    if run is None or method != "session/prompt":
        return params, None
    return await run.prepare(transport, params), run

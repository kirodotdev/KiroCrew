#!/usr/bin/env python3
"""Drive a harness through ``/compact`` and MEASURE whether its context shrank.

``ACP_BACKENDS_COMPACT`` in ``src/kiro_crew/agent_sdk/backends.py`` admits a harness
on a DRIVEN capture rather than on its source: the bar opencode met is a live session
whose ``usage_update.used`` was seen to fall below its pre-compact peak. pi and goose
both finish a compaction inline according to their own code and neither is a member,
because nobody had driven them -- pi answered ``Authentication required`` and goose
``Failed to resolve provider: GOOSE_PROVIDER`` where that note was written.

This script is that drive, as one command. It spawns the harness the way Crew spawns
it, sends N ordinary turns, sends ``/compact``, sends one more ordinary turn, and
prints the ``used`` series with a verdict. The JSONL it writes is the evidence a
membership rests on, in the corpus's own fixture shape.

    python3 scripts/capture_acp_compaction.py --harness goose
    python3 scripts/capture_acp_compaction.py --harness pi --turns 6
    python3 scripts/capture_acp_compaction.py --harness opencode   # re-measure the member

The runbook -- which credential each harness needs, how to install it, and which files
to edit once a capture is in hand -- is
``docs/guides/acp-compaction-capture.md``.

What it does NOT do
-------------------
It does not authenticate anything. No harness here takes a credential from Crew: each
resolves its own provider from its own store, which is why
``agent_sdk/backend_install.py`` probes none and why this script cannot either. A
harness that is not signed in answers the first prompt with its own error, and that
error is reported with the repository's own sign-in remedy for that harness beside it.

It does not write into the committed corpus. The default output is under ``build/``,
which is git-ignored, because a stray ``.jsonl`` inside ``test/fixtures/acp_frames/``
is collected by ``test/test_acp_frame_replay.py`` and would go red for want of a
snapshot the moment it existed. Reviewing a capture by hand and MOVING it in is a
separate, deliberate step.

It does not decide membership. It measures one session and says what it measured.

Exit codes
----------
* ``0`` -- COMPACTED: ``used`` fell below the pre-compact peak. The capture is the
  evidence for a membership.
* ``1`` -- NOT_COMPACTED: the drive worked and ``used`` did not fall. Also evidence,
  and the more interesting kind: it contradicts what the harness's source says.
* ``2`` -- a usage or environment error: bad arguments, an unknown harness, a harness
  that is not installed.
* ``3`` -- the harness refused to work: an auth or provider error rather than a
  measurement.
* ``4`` -- UNPROVEN: the turns ran and no ``usage_update`` frame carried ``used``, so
  this harness does not report the number the bar is written in.
* ``5`` -- the capture was written and is not commit-ready: a recording-host marker
  survived the sweep, or ``--keep-ids`` kept the harness's own ids. The frames are on
  disk either way; nothing is thrown away for failing a sweep.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# The parsers are not driven here, but importing ``kiro_crew`` pulls the metrics
# provider, which resolves consent on its first build. Pinned before the import for the
# reason ``update_acp_frame_snapshots.py`` pins it: a script is not pytest, so nothing
# else sets it, and a host with telemetry enabled would otherwise record this drive.
os.environ["KIROCREW_TELEMETRY"] = "0"

sys.path.insert(0, str(REPO_ROOT / "src"))

from kiro_crew.acp._dispatch import redact_text  # noqa: E402

#: The harness ids this script can spawn, and nothing else. kiro-cli, KAS, claude and
#: codex are absent deliberately: their argv is bespoke (an agent spec, a relay, a
#: vendored entry with its own ladder), so a capture of one is not this script's shape
#: even though the measurement would be the same.
GOOSE = "goose"
PI = "pi"

#: The token a turn's number is substituted for. Replaced literally rather than through
#: ``str.format``, which reads every brace in an operator's prompt as a field name and
#: raises on the ones that are not.
TURN_PLACEHOLDER = "{n}"

#: One turn's prompt. Short, boring and numbered: the series has to GROW the context
#: monotonically for a peak to mean anything, and a prompt whose answer is one word
#: keeps the growth in the transcript rather than in the model's prose. This is the
#: prompt the opencode member was driven with.
DEFAULT_PROMPT = "Reply with the single word ok and nothing else. (probe turn {n})"

#: What Crew sends for a manual compaction, verbatim -- ``AcpSessionHandle.compact()``
#: sends this as a ``session/prompt`` text block. A capture driven with a different
#: spelling would not be evidence about the path Crew takes.
COMPACT_PROMPT = "/compact"

#: Frame classes dropped from the written capture. Both are inventories of what the
#: RECORDING HOST has rather than shapes the product parses, and the corpus refuses
#: them: ``available_commands_update`` is the harness's own command list and
#: ``session_info_update`` carries a run id and wall-clock times. The goose corpus was
#: reduced the same way, and its ``_meta.note`` says so.
DROPPED_UPDATES = ("available_commands_update", "session_info_update")

#: Also dropped: the summariser's thinking. It is model prose, it carries no class any
#: parser reads, and on a compaction turn it is the bulk of the capture.
DROPPED_THOUGHTS = ("agent_thought_chunk",)

#: The smallest per-frame wait worth accepting. ``not timeout >= MIN`` rather than
#: ``timeout < MIN`` so a NaN, which compares false to everything, is refused too.
MIN_TIMEOUT_SECS = 1.0

VERDICT_COMPACTED = "COMPACTED"
VERDICT_NOT_COMPACTED = "NOT_COMPACTED"
VERDICT_UNPROVEN = "UNPROVEN"

EXIT_COMPACTED = 0
EXIT_NOT_COMPACTED = 1
EXIT_USAGE = 2
EXIT_HARNESS_REFUSED = 3
EXIT_UNPROVEN = 4
EXIT_MARKERS = 5

#: Substrings that mean "this harness will not talk to a model", as each harness
#: actually spells it. Matched case-insensitively against a JSON-RPC error message so a
#: missing credential is reported as the environment problem it is, with the
#: repository's own remedy, instead of as a failed measurement.
REFUSAL_MARKERS = (
    "authentication required",
    "failed to resolve provider",
    "no provider",
    "not authenticated",
    "unauthorized",
    "api key",
    "no credentials",
)


class CaptureError(Exception):
    """Something about the host or the arguments, not about the harness's behaviour."""


class HarnessRefused(Exception):
    """The harness answered with an auth or provider error rather than a turn."""


# ── what to spawn ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Spawn:
    """Everything needed to start one harness, resolved the way Crew resolves it."""

    backend: str
    argv: tuple[str, ...]
    env: dict[str, str]
    protocol_version: int
    label: str


def _acp_client():
    """Crew's own spawn module, or a CaptureError naming what could not be imported."""
    try:
        from kiro_crew.acp import client as acp_client
    except Exception as exc:  # pragma: no cover - exercised only on a broken checkout
        raise CaptureError(
            f"cannot import kiro_crew.acp.client from {REPO_ROOT / 'src'}: {exc}. "
            "Run this from a checkout with the package importable (a venv with "
            "`pip install -e .`, or just the repo root on a host with the deps)."
        ) from exc
    return acp_client


def supported_harnesses() -> tuple[str, ...]:
    """The ids ``--harness`` accepts: every self-served harness, plus pi.

    Derived from ``ACP_BACKEND_LAUNCH`` rather than listed, so a harness onboarded into
    that table is drivable here without a second edit -- which is the property that
    keeps this script from being the thing that decides which harnesses can be
    measured.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_LAUNCH

    return tuple(sorted(set(ACP_BACKEND_LAUNCH) | {PI}))


def resolve_spawn(backend: str) -> Spawn:
    """Resolve *backend*'s argv and environment seeds, or raise CaptureError.

    The resolvers are Crew's, imported rather than re-implemented: a capture is
    evidence about the harness CREW SPAWNS, so a second ladder here -- even a correct
    one -- would be measuring a binary chosen by a different rule than the product's.
    They are private names in ``acp.client`` because nothing above the ACP layer needed
    a PATH before; this script needs the path itself, not the boolean the public driver
    seam answers.
    """
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_LAUNCH,
        ACP_BACKEND_PERMISSION_SETTING,
        launch_for,
    )
    from kiro_crew.env import describe_search_path

    acp_client = _acp_client()
    env = dict(os.environ)

    if backend == PI:
        adapter_argv, adapter_searched = acp_client._resolve_pi_acp_bin()
        if not adapter_argv:
            raise CaptureError(
                f"pi-acp not found ({describe_search_path(adapter_searched)}). "
                "Install both halves "
                f"with '{acp_client.PI_INSTALL_COMMAND}', or set PI_ACP_BIN to the "
                "adapter's entry script. The 'pi' CLI alone does not serve ACP."
            )
        pi_bin, pi_searched = acp_client._resolve_pi_bin()
        if not pi_bin:
            raise CaptureError(
                f"the pi agent was not found ({describe_search_path(pi_searched)}). "
                "The adapter is "
                "installed but the agent it spawns is not: install it with "
                f"'{acp_client.PI_INSTALL_COMMAND}', or set PI_ACP_PI_COMMAND to the "
                "executable."
            )
        return Spawn(
            backend=PI,
            argv=tuple(adapter_argv),
            env=env,
            protocol_version=1,
            label="pi-acp",
        )

    if backend not in ACP_BACKEND_LAUNCH:
        raise CaptureError(
            f"--harness {backend} is not one this script can spawn. Supported: "
            f"{', '.join(supported_harnesses())}. kiro, kas, claude and codex build "
            "their argv from an agent spec, a relay or a vendored entry, so driving "
            "one is a different spawn than this."
        )

    launch = launch_for(backend)
    binary, searched = acp_client._resolve_self_served_bin(backend)
    if not binary:
        raise CaptureError(
            f"{launch.binary} not found ({describe_search_path(searched)}). "
            "Install it with "
            f"'{launch.install_command}', or set {launch.bin_env_var} to the "
            "executable."
        )

    # goose resolves its permission mode from the ENVIRONMENT, above its own config
    # file, and Crew seeds it at spawn. Seeded here from the same table so the captured
    # session is shaped like a Crew session rather than like a default one. The table
    # holds config-field pairs too (opencode's ``permission``/``ask`` travels on
    # ``session/new``), so only the environment-keyed harness reads it here.
    if backend == GOOSE:
        key, value = ACP_BACKEND_PERMISSION_SETTING[GOOSE]
        env[key] = value

    return Spawn(
        backend=backend,
        argv=(binary, *launch.acp_args),
        env=env,
        protocol_version=launch.protocol_version,
        label=launch.label,
    )


def sign_in_remedy(backend: str) -> str:
    """The repository's own remedy text for *backend*, or an empty string.

    Read from ``agent_sdk/host_auth`` rather than written again: the operator who hits
    an auth refusal here and the operator who hits it in the dashboard should be told
    the same thing, and a second copy is a second thing to keep true.
    """
    try:
        from kiro_crew.agent_sdk.host_auth import declaration_for

        declaration = declaration_for(backend)
    except Exception:
        return ""
    return getattr(declaration, "sign_in_remedy", "") or ""


# ── driving ─────────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    """One ``session/prompt`` and what came back on the wire for it."""

    kind: str  # "ordinary" or "compact"
    prompt: str
    used: list[int] = field(default_factory=list)
    stop_reason: str = ""


class StdioDriver:
    """A minimal ACP client over a harness's stdio: enough to prompt and to listen.

    Deliberately not ``AcpClient``. That class brings a session pool, a sandbox
    preflight, an MCP projection and a permission gate, none of which a compaction
    measurement needs, and all of which would put Crew's behaviour between the harness
    and the number being read. What is reused instead is the part a capture must not
    get wrong: WHICH binary is spawned and with what environment (``resolve_spawn``).
    """

    def __init__(self, spawn: Spawn, cwd: Path, timeout: float) -> None:
        self._spawn = spawn
        self._cwd = cwd
        self._timeout = timeout
        self._next_id = 0
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self.frames: list[dict[str, Any]] = []
        self.stderr_tail: list[str] = []
        self.agent_version = "unmeasured"
        self.session_id = ""
        self._proc: subprocess.Popen[str] | None = None

    # -- process lifecycle --

    def __enter__(self) -> "StdioDriver":
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv is resolved, never a shell
                list(self._spawn.argv),
                cwd=str(self._cwd),
                env=self._spawn.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise CaptureError(f"cannot start {' '.join(self._spawn.argv)}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        proc = self._proc
        if proc is None:
            return
        for step in (proc.terminate, proc.kill):
            if proc.poll() is not None:
                break
            step()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            # Bounded: a harness that logs every token would otherwise hold the whole
            # run in memory, and only the tail is ever useful in a failure report.
            self.stderr_tail.append(line.rstrip())
            del self.stderr_tail[:-40]

    # -- wire --

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise CaptureError("the harness process is gone")
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CaptureError(
                f"{self._spawn.label} closed its input after "
                f"{len(self.frames)} frame(s): {exc}. {self._stderr_hint()}"
            ) from exc

    def _stderr_hint(self) -> str:
        if not self.stderr_tail:
            return "It wrote nothing to stderr."
        tail = "\n  ".join(self.stderr_tail[-8:])
        return f"Its last stderr lines were:\n  {tail}"

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request and pump the wire until its response arrives."""
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return self._pump_until(request_id, method)

    def _pump_until(self, request_id: int, method: str) -> dict[str, Any]:
        while True:
            try:
                line = self._lines.get(timeout=self._timeout)
            except queue.Empty:
                raise CaptureError(
                    f"{self._spawn.label} sent nothing for {self._timeout:.0f}s while "
                    f"answering {method}. Raise --timeout for a slow model, or run the "
                    f"harness by hand to see what it is waiting for. {self._stderr_hint()}"
                ) from None
            if line is None:
                raise CaptureError(
                    f"{self._spawn.label} exited while answering {method}. "
                    f"{self._stderr_hint()}"
                )
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # Not every harness keeps its stdout clean; a non-JSON line is log
                # noise, not a frame, and a capture must not record it as one.
                continue
            if not isinstance(frame, dict):
                continue
            self.frames.append(frame)
            if frame.get("id") == request_id and ("result" in frame or "error" in frame):
                error = frame.get("error")
                if error:
                    self._raise_for_error(method, error)
                result = frame.get("result")
                return result if isinstance(result, dict) else {}
            if "method" in frame and "id" in frame:
                self._answer(frame)

    def _raise_for_error(self, method: str, error: Any) -> None:
        message = ""
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        blob = json.dumps(error) if not isinstance(error, str) else error
        lowered = f"{message} {blob}".lower()
        if any(marker in lowered for marker in REFUSAL_MARKERS):
            raise HarnessRefused(f"{method} was refused: {message or blob}")
        raise CaptureError(f"{method} failed: {message or blob}. {self._stderr_hint()}")

    def _answer(self, frame: dict[str, Any]) -> None:
        """Answer an agent-to-client REQUEST so the turn is not stalled by this client.

        Every request is REFUSED, a permission request included, and that is a safety
        property rather than an omission. This driver spawns the harness raw -- no
        sandbox, no permission gate, the operator's own environment -- because a capture
        has to be evidence about the harness rather than about Crew's wrapping. A client
        that also approved tool calls in that process would run whatever the model asked
        for on the operator's machine, so the one thing it must not do is say yes.

        Nothing the measurement needs is lost: the drive's prompts ask for a single word,
        and a compaction is the harness summarizing its own transcript, not a tool call.
        A refused call still lands in the frames, where it is visible.

        The harness's OWN builtin tools are a separate matter -- a harness that runs them
        without asking never reaches this method -- which is why the drive belongs in a
        scratch directory.
        """
        method = frame.get("method") or ""
        request_id = frame.get("id")
        if method == "session/request_permission":
            option_id = _first_reject_option(frame.get("params"))
            if option_id:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
                    }
                )
                return
            # No reject-shaped option offered: cancel, which refuses without selecting
            # an option whose meaning this client cannot read.
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                }
            )
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"{method} is not implemented by this capture client",
                },
            }
        )

    # -- the drive --

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": self._spawn.protocol_version,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )
        info = result.get("agentInfo")
        if isinstance(info, dict) and info.get("version"):
            self.agent_version = str(info["version"])

    def new_session(self) -> None:
        result = self.request(
            "session/new",
            {"cwd": str(self._cwd), "mcpServers": []},
        )
        session_id = result.get("sessionId")
        if not session_id:
            raise CaptureError(
                "session/new returned no sessionId, so there is no session to drive: "
                f"{json.dumps(result)[:400]}"
            )
        self.session_id = str(session_id)

    def prompt(self, text: str, *, kind: str) -> Turn:
        turn = Turn(kind=kind, prompt=text)
        before = len(self.frames)
        result = self.request(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )
        turn.stop_reason = str(result.get("stopReason") or "")
        turn.used = used_series(self.frames[before:])
        return turn


def _first_reject_option(params: Any) -> str:
    """The id of the first reject-shaped option offered, or an empty string.

    ``reject_once`` before ``reject_always``: a capture refuses this call, and has no
    standing to answer for calls it has not seen.
    """
    if not isinstance(params, dict):
        return ""
    options = params.get("options")
    if not isinstance(options, list):
        return ""
    for wanted in ("reject_once", "reject_always"):
        for option in options:
            if not isinstance(option, dict) or not option.get("optionId"):
                continue
            if str(option.get("kind") or "") == wanted:
                return str(option["optionId"])
    return ""


# ── measurement ─────────────────────────────────────────────────────────────────


def used_series(frames: Iterable[dict[str, Any]]) -> list[int]:
    """Every ``usage_update.used`` in *frames*, in order.

    One frame class and one field, because that is the field the membership bar is
    written in. A harness that reports its context some other way reads as UNPROVEN
    here rather than as a pass on a number nobody agreed to.
    """
    out: list[int] = []
    for frame in frames:
        params = frame.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "usage_update":
            continue
        used = update.get("used")
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        out.append(int(used))
    return out


@dataclass(frozen=True)
class Measurement:
    """What the drive establishes about one session's context."""

    verdict: str
    reason: str
    pre_peak: int | None
    compact_turn_used: int | None
    after: int | None


def measure(turns: Sequence[Turn]) -> Measurement:
    """Read a verdict off the turns, on the bar ``ACP_BACKENDS_COMPACT`` holds.

    The bar has two halves and both matter. The peak is taken BEFORE the ``/compact``
    turn, because a number read during a compaction is the harness mid-summary. The
    comparison value is read from the ORDINARY turn after it, because that is the turn
    that proves the smaller context was carried forward rather than reported once.
    """
    # Located by KIND rather than by ``list.index``: two turns carrying the same prompt
    # and the same readings compare equal, so an index lookup could name the wrong one.
    index = next((i for i, turn in enumerate(turns) if turn.kind == "compact"), len(turns))
    any_used = [used for turn in turns for used in turn.used]
    compact_used = [used for turn in turns[index : index + 1] for used in turn.used]
    pre_peak_values = [used for turn in turns[:index] for used in turn.used]
    after_values = [used for turn in turns[index + 1 :] for used in turn.used]

    pre_peak = max(pre_peak_values) if pre_peak_values else None
    compact_turn_used = compact_used[-1] if compact_used else None
    after = after_values[-1] if after_values else None

    if not any_used:
        return Measurement(
            VERDICT_UNPROVEN,
            "no usage_update frame carried a `used` value, so this harness does not "
            "report the number this bar is written in",
            pre_peak,
            compact_turn_used,
            after,
        )
    if pre_peak is None:
        return Measurement(
            VERDICT_UNPROVEN,
            "no usage_update arrived BEFORE the /compact turn, so there is no peak to "
            "compare against",
            pre_peak,
            compact_turn_used,
            after,
        )
    if after is None:
        return Measurement(
            VERDICT_UNPROVEN,
            "the ordinary turn after /compact reported no `used`, so nothing shows the "
            "smaller context was carried forward",
            pre_peak,
            compact_turn_used,
            after,
        )
    if after < pre_peak:
        return Measurement(
            VERDICT_COMPACTED,
            f"used fell to {after} after /compact, below the pre-compact peak of {pre_peak}",
            pre_peak,
            compact_turn_used,
            after,
        )
    return Measurement(
        VERDICT_NOT_COMPACTED,
        f"used was {after} after /compact, not below the pre-compact peak of {pre_peak}",
        pre_peak,
        compact_turn_used,
        after,
    )


# ── reduction and provenance ────────────────────────────────────────────────────


def reduce_frames(
    frames: Sequence[dict[str, Any]],
    *,
    backend: str,
    session_id: str,
    cwd: Path,
    keep_ids: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop host inventories and replace host-minted ids, reporting what was done.

    The corpus's rule: a live frame is host data until proved otherwise, and the two
    marker classes that cannot be re-recorded away -- a session or permission uuid --
    are normalized AT CAPTURE to a fixed synthetic value rather than edited later. The
    returned notes go into ``_meta.note``, because a reduction nobody can see is a
    reduction a reader has to take on trust.
    """
    notes: list[str] = []
    dropped_updates = 0
    dropped_thoughts = 0
    kept: list[dict[str, Any]] = []

    for frame in frames:
        params = frame.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind in DROPPED_UPDATES:
            dropped_updates += 1
            continue
        if kind in DROPPED_THOUGHTS:
            dropped_thoughts += 1
            continue
        kept.append(frame)

    if dropped_updates:
        notes.append(
            f"{dropped_updates} {'/'.join(DROPPED_UPDATES)} frame(s) dropped: they carry "
            "the recording host's own command inventory and run id"
        )
    if dropped_thoughts:
        notes.append(
            f"{dropped_thoughts} agent_thought_chunk frame(s) dropped: model prose, no "
            "class any parser reads"
        )

    # Substituted on the frame DATA, never on its JSON text. A Windows path arrives in
    # a serialized frame with every separator doubled, so a plain replace over the text
    # cannot match the path the process actually ran in -- and the host path would
    # survive into the fixture on exactly the platform whose paths the marker patterns
    # name.
    substitutions: list[tuple[str, str, str]] = []
    # The credential and exfiltration-URL scrub the in-product recorder applies, run on
    # the same frames for the same reason: a frame is agent-written text, so a secret the
    # model echoed is a secret in the capture, and the marker sweep reads host identity
    # rather than secrets. Applied over the frame data, where the reduction already works.
    scrubbed = 0

    def _scrub(text: str) -> str:
        nonlocal scrubbed
        cleaned = redact_text(text)
        if cleaned != text:
            scrubbed += 1
        return cleaned

    kept = [_map_strings(frame, _scrub) for frame in kept]
    if scrubbed:
        notes.append(
            f"{scrubbed} string(s) were scrubbed through the product's own redact_text, "
            "which is the same scrub the in-product frame recorder applies"
        )
    # The working directory goes FIRST and the home directory second, because the two
    # overlap: a scratch directory inside the operator's home contains the home path,
    # and replacing the shorter string first leaves a mangled path that the longer
    # replacement then fails to match -- so the scratch path would survive the sweep.
    cwd_text = str(cwd)
    if cwd_text and not is_filesystem_root(cwd):
        substitutions.append(
            (cwd_text, "<cwd>", "the scratch working directory is replaced with <cwd>")
        )
    home = Path.home()
    # Skipped for a root HOME for the same reason the working directory is: the root's
    # text is the separator every path contains, so substituting it would rewrite
    # ``session/update`` into ``session~update`` and the fixture would no longer carry
    # the methods the wire carried.
    if str(home) and not is_filesystem_root(home):
        substitutions.append(
            (str(home), "~", "the recording user's home directory is replaced with ~")
        )
    if not keep_ids:
        if session_id:
            substitutions.append(
                (
                    session_id,
                    f"{backend}-session-1",
                    f"the session id is replaced with the synthetic {backend}-session-1",
                )
            )
        for index, value in enumerate(_permission_ids(kept), start=1):
            substitutions.append(
                (value, f"perm-{index}", "permission id(s) replaced with synthetic perm-N values")
            )

    reduced = kept
    applied: list[str] = []
    for needle, replacement, note in substitutions:
        hits = 0

        def _swap(text: str, needle: str = needle, replacement: str = replacement) -> str:
            nonlocal hits
            if needle not in text:
                return text
            hits += 1
            return text.replace(needle, replacement)

        reduced = [_map_strings(frame, _swap) for frame in reduced]
        if hits and note not in applied:
            applied.append(note)
    notes.extend(applied)

    return reduced, notes


#: Frame keys whose value is a permission or request id the harness minted.
_PERMISSION_ID_KEYS = ("permissionId", "requestId")


def _permission_ids(frames: Sequence[Any]) -> list[str]:
    """Distinct permission and request ids carried by *frames*, in first-seen order."""
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _PERMISSION_ID_KEYS and isinstance(value, str) and value:
                    if value not in found:
                        found.append(value)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for frame in frames:
        _walk(frame)
    return found


def _map_strings(node: Any, swap: Any) -> Any:
    """*node* rebuilt with *swap* applied to every string it carries, key or value."""
    if isinstance(node, str):
        return swap(node)
    if isinstance(node, dict):
        return {
            swap(key) if isinstance(key, str) else key: _map_strings(value, swap)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_map_strings(item, swap) for item in node]
    return node


def meta_header(
    *,
    backend: str,
    agent_version: str,
    measurement: Measurement,
    turns: Sequence[Turn],
    notes: Sequence[str],
) -> dict[str, Any]:
    """The provenance line the corpus requires, plus what this capture is FOR.

    ``backend``, ``recorded``, ``date`` and ``agent_version`` are the four keys
    ``test/fixtures/acp_frames/README.md`` asks of every fixture. The note carries the
    series and the verdict, because the reason this file exists is a number that a
    reader cannot recompute from the frames once the growth turns are trimmed.
    """
    series = " -> ".join(
        str(used) for turn in turns if turn.kind == "ordinary" for used in turn.used
    )
    reductions = "; ".join(notes) if notes else "none"
    return {
        "_meta": {
            "backend": backend,
            "recorded": "live",
            "date": _datetime.date.today().isoformat(),
            "agent_version": agent_version,
            "note": (
                "Captured by scripts/capture_acp_compaction.py: the harness driven over "
                "stdio through ordinary turns, then a manual /compact prompt, then one "
                "more ordinary turn. This is the evidence class ACP_BACKENDS_COMPACT "
                f"holds its members to. usage_update.used on the ordinary turns: {series}; "
                f"during the /compact turn: {measurement.compact_turn_used}; on the "
                f"ordinary turn after it: {measurement.after}. Verdict: "
                f"{measurement.verdict} -- {measurement.reason}. Reductions: {reductions}. "
                "The working directory was a scratch project."
            ),
        }
    }


def host_markers(text: str) -> list[tuple[str, str]]:
    """Recording-host markers surviving in *text*, as ``(match, reason)``.

    The patterns are ``scripts/check_acp_frame_host_data.py``'s, loaded by path because
    ``scripts/`` is not a package. Reusing them is the point: a capture is swept with
    the same eyes the repository gate will use on it, so "commit-ready" means the same
    thing in both places.
    """
    gate_path = REPO_ROOT / "scripts" / "check_acp_frame_host_data.py"
    spec = importlib.util.spec_from_file_location("acp_frame_host_data_gate", gate_path)
    if spec is None or spec.loader is None:
        raise CaptureError(f"cannot load {gate_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    found: list[tuple[str, str]] = []
    for pattern, reason in module.PATTERNS:
        match = re.search(pattern, text)
        if match:
            found.append((match.group(0), reason))
    return found


# ── CLI ─────────────────────────────────────────────────────────────────────────


def is_filesystem_root(path: Path) -> bool:
    """Whether *path* is a filesystem root (``/``, a drive root, a UNC share root).

    A root is refused as ``--cwd`` rather than substituted. Its text is a separator every
    other path contains, so replacing it would rewrite ``session/update`` into
    ``session<cwd>update`` and corrupt the evidence the capture exists to be.
    """
    resolved = path.resolve()
    return resolved.parent == resolved


def turn_prompt(template: str, number: int) -> str:
    """*template* with the literal ``{n}`` replaced by *number*."""
    return template.replace(TURN_PLACEHOLDER, str(number))


def default_out(backend: str) -> Path:
    """Where a capture lands unless ``--out`` says otherwise.

    Under ``build/`` -- git-ignored -- and NOT in the corpus, because
    ``test/test_acp_frame_replay.py`` collects every ``*.jsonl`` under
    ``test/fixtures/acp_frames/`` and would go red for a candidate with no snapshot
    beside it. Moving a reviewed capture in is the deliberate step the runbook
    describes.
    """
    return REPO_ROOT / "build" / "acp-capture" / backend / "compact-live.jsonl"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="capture_acp_compaction.py",
        description=(
            "Drive a harness through /compact and report whether usage_update.used "
            "really fell below its pre-compact peak."
        ),
    )
    parser.add_argument(
        "--harness",
        required=True,
        help="Harness id to drive (goose, pi, opencode, deepseek).",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=4,
        help="Ordinary turns to drive before /compact (default 4, the opencode drive).",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Per-turn prompt; the literal {n} is replaced with the turn number.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write the JSONL capture (default build/acp-capture/<harness>/).",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Working directory for the session (default a fresh scratch directory).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for one frame before giving up (default 180).",
    )
    parser.add_argument(
        "--keep-ids",
        action="store_true",
        help="Keep the harness's real session and permission ids (not commit-ready).",
    )
    return parser.parse_args(list(argv))


def _print_report(
    *,
    spawn: Spawn,
    turns: Sequence[Turn],
    measurement: Measurement,
    out: Path,
    frame_count: int,
) -> None:
    ordinary = [turn for turn in turns if turn.kind == "ordinary"]
    print("")
    print(f"harness        {spawn.label} ({spawn.backend})")
    print(f"drive          {len(ordinary) - 1} ordinary turn(s), /compact, 1 ordinary turn")
    for number, turn in enumerate(turns, start=1):
        used = ", ".join(str(value) for value in turn.used) or "-"
        print(
            f"  turn {number:<2} {turn.kind:<8} used {used:<28} stopReason {turn.stop_reason or '-'}"
        )
    print(f"peak before    {measurement.pre_peak if measurement.pre_peak is not None else '-'}")
    print(
        f"during compact {measurement.compact_turn_used if measurement.compact_turn_used is not None else '-'}"
    )
    print(f"after compact  {measurement.after if measurement.after is not None else '-'}")
    print(f"verdict        {measurement.verdict} -- {measurement.reason}")
    print(f"frames         {frame_count} written to {out}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        supported = supported_harnesses()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.harness not in supported:
        print(
            f"error: --harness {args.harness} is not supported. Choose one of: "
            f"{', '.join(supported)}.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.turns < 1:
        print("error: --turns must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    # A floor rather than a bare positivity check: the value is a wait for a model's
    # whole turn, and a sub-second one turns every drive into a timeout report.
    if not args.timeout >= MIN_TIMEOUT_SECS:
        print(
            f"error: --timeout must be at least {MIN_TIMEOUT_SECS} seconds "
            f"(got {args.timeout}); a model turn does not finish faster",
            file=sys.stderr,
        )
        return EXIT_USAGE

    cwd = args.cwd
    if cwd is None:
        cwd = Path(tempfile.mkdtemp(prefix="acp-compact-"))
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd {cwd} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    if is_filesystem_root(cwd):
        print(
            f"error: --cwd {cwd} is a filesystem root. Pass a scratch project directory: "
            "a root cannot be substituted out of the frames, and a harness driven at one "
            "has the whole disk in front of its own tools.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        spawn = resolve_spawn(args.harness)
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    out = args.out or default_out(args.harness)
    turns: list[Turn] = []

    try:
        with StdioDriver(spawn, cwd, args.timeout) as driver:
            driver.initialize()
            driver.new_session()
            for number in range(1, args.turns + 1):
                turns.append(driver.prompt(turn_prompt(args.prompt, number), kind="ordinary"))
            turns.append(driver.prompt(COMPACT_PROMPT, kind="compact"))
            turns.append(driver.prompt(turn_prompt(args.prompt, args.turns + 1), kind="ordinary"))
            frames = list(driver.frames)
            agent_version = driver.agent_version
            session_id = driver.session_id
    except HarnessRefused as exc:
        print(f"error: {spawn.label} will not talk to a model -- {exc}", file=sys.stderr)
        remedy = sign_in_remedy(args.harness)
        if remedy:
            print(f"\n{remedy}", file=sys.stderr)
        print(
            "\nNothing here can sign the harness in for you: each one resolves its own "
            "provider from its own store. See docs/guides/acp-compaction-capture.md.",
            file=sys.stderr,
        )
        return EXIT_HARNESS_REFUSED
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    measurement = measure(turns)
    reduced, notes = reduce_frames(
        frames,
        backend=args.harness,
        session_id=session_id,
        cwd=cwd,
        keep_ids=args.keep_ids,
    )
    header = meta_header(
        backend=args.harness,
        agent_version=agent_version,
        measurement=measurement,
        turns=turns,
        notes=notes,
    )

    lines = [json.dumps(header, ensure_ascii=False)]
    lines += [json.dumps(frame, ensure_ascii=False) for frame in reduced]
    body = "\n".join(lines) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")

    _print_report(
        spawn=spawn,
        turns=turns,
        measurement=measurement,
        out=out,
        frame_count=len(reduced),
    )

    markers = host_markers(body)
    if markers:
        print("\nhost markers survived, so this capture is NOT commit-ready:", file=sys.stderr)
        for match, reason in markers:
            print(f"  {reason}: {match!r}", file=sys.stderr)
        print(
            "Re-record with a scratch model and a scratch directory, or prune the field "
            "in the capture step -- never by editing the committed frame.",
            file=sys.stderr,
        )
        return EXIT_MARKERS
    # ``--keep-ids`` keeps the harness's own session and permission ids, and the sweep
    # cannot answer for them: an id shaped like a uuid is a pattern the marker set names,
    # while opencode's ``ses_...`` is not, so a clean sweep over a kept id means only that
    # this particular id looks harmless. The flag's whole purpose is a capture that is not
    # commit-ready, so it reports that in the exit code rather than only in prose.
    if args.keep_ids:
        print(
            "\n--keep-ids was set, so the harness's own session and permission ids are "
            "still in this capture. It is a debugging read, not a fixture: re-run without "
            "the flag to produce one that can be committed.",
            file=sys.stderr,
        )
        return EXIT_MARKERS

    if measurement.verdict == VERDICT_COMPACTED:
        return EXIT_COMPACTED
    if measurement.verdict == VERDICT_NOT_COMPACTED:
        return EXIT_NOT_COMPACTED
    return EXIT_UNPROVEN


if __name__ == "__main__":
    sys.exit(main())

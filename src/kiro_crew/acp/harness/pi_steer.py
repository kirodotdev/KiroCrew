"""The pi steer channel: a user's mid-turn message, carried into a running pi turn.

pi-acp has no steer verb, and a second ``session/prompt`` lands in its turn queue as
a turn of its own, after the running one. pi itself has a steering queue its agent
loop reads before each model call, and Crew's gate extension runs inside pi with the
extension API's ``pi.sendMessage(message, {deliverAs: "steer"})`` onto that queue.
So the host hands each steer to the extension directly, bypassing the adapter:

* **Who can write.** Each steer is one file in the owner-only pi gate artifact
  directory (:func:`kiro_crew.acp.harness.pi._pi_gate_artifact_dir`), which the
  sandbox exposes to pi children READ-ONLY. Only the gateway, outside the sandbox,
  writes there. The file is ``0400`` and lands by an atomic rename, so the extension
  never reads a half-written one.
* **How it is authenticated.** The text is sealed with AES-256-GCM under a key minted
  per launch (:func:`new_pi_steer_channel`) and placed in that launch's environment
  alone (``KIROCREW_PI_STEER_KEY``). The associated data binds the steer to the
  launch's gate nonce, the pi session id the adapter answered ``session/new`` with,
  and the steer's sequence number, which the file name repeats. A sibling session
  that can read the directory reads ciphertext, and a file renamed, copied to another
  session or sealed under another key does not open.
* **Replay and order.** Sequence numbers are strictly increasing per launch. The
  extension delivers the oldest unseen steer at each ``turn_end`` and never again
  delivers one at or below the highest it has delivered or retired.
* **No running turn.** The host writes only while a prompt turn of its own is open
  and removes every file it wrote when that turn closes (:meth:`PiSteerChannel.discard`),
  under one lock, so no steer file outlives its turn. The extension in turn retires
  every steer still on disk when pi settles a run, and refuses to deliver at a
  ``turn_end`` that aborted or failed, so a steer is never read into a later turn.
* **Consumed.** When pi starts the steered message the extension notifies an
  envelope carrying the gate nonce and the sequence number, which pi-acp forwards as
  an ``agent_message_chunk``; :func:`steer_echo_seq` reads it back.

Leaf module: no import of the client or the dashboard.
"""

from __future__ import annotations

import base64
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field

__all__ = [
    "PiSteerChannel",
    "new_pi_steer_channel",
    "steer_echo_seq",
]

# The environment names the extension reads (``kiro_crew_tool_gate.ts``).
_ENV_PI_STEER = "KIROCREW_PI_STEER"
_ENV_PI_STEER_KEY = "KIROCREW_PI_STEER_KEY"

# The marker a steer file and its consumed notification carry.
_PI_STEER_MARKER = "kiro-crew-steer"

# Ten digits, zero-padded, so the extension parses one fixed spelling.
_SEQ_WIDTH = 10


def _aad(nonce: str, session_id: str, seq: int) -> bytes:
    """A steer's associated data, spelled as the extension spells it (``steerAad``)."""
    return "\0".join((_PI_STEER_MARKER, nonce, session_id, str(seq))).encode("utf-8")


@dataclass
class PiSteerChannel:
    """One pi launch's steer channel: its nonce, its directory, its key, its sequence.

    *artifact_dir* is the owner-only pi gate artifact directory the launch already
    resolved and verified when it sealed the extension there; a channel whose launch
    never steers writes nothing into it.
    """

    nonce: str
    artifact_dir: str
    key: bytes = field(repr=False)
    seq: int = 0

    def _stem(self) -> str:
        """The path every steer file of this launch starts with."""
        name = f"kirocrew_pi_gate_{os.getpid()}_steer_{self.nonce}_"
        return os.path.join(self.artifact_dir, name)

    def env(self) -> dict[str, str]:
        """The child's environment entries that name the channel and carry its key."""
        return {_ENV_PI_STEER: self._stem(), _ENV_PI_STEER_KEY: self.key.hex()}

    def seal(self, session_id: str, text: str) -> tuple[int, bytes]:
        """The next sequence number, and the steer file's bytes for *text*."""
        # Imported here, as the MCP handoff does: a module-level import would make
        # every pi import depend on the crypto library's load.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self.seq += 1
        seq = self.seq
        iv = os.urandom(12)
        # UTF-8 rather than ``\\u`` escapes, so a steer at the text bound stays within
        # the size the extension reads (an escaped astral character is 12 bytes).
        plaintext = json.dumps(
            {"seq": seq, "session": session_id, "text": text}, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        sealed = AESGCM(self.key).encrypt(iv, plaintext, _aad(self.nonce, session_id, seq))
        body = {
            _PI_STEER_MARKER: 1,
            "nonce": self.nonce,
            "seq": seq,
            "iv": base64.b64encode(iv).decode("ascii"),
            "sealed": base64.b64encode(sealed).decode("ascii"),
        }
        return seq, json.dumps(body, sort_keys=True).encode("utf-8")

    def publish(self, session_id: str, text: str) -> int:
        """Seal *text* for *session_id* and land it as this launch's next steer file.

        Returns its sequence number. The directory is re-checked as a real owner-only
        leaf first, the check every other gate-artifact writer runs, and a directory
        that does not resolve to the one the launch verified is refused.
        Blocking; callers run it off the loop.
        """
        from kiro_crew.acp.harness.pi import _pi_gate_artifact_dir, _publish_gate_artifact

        if _pi_gate_artifact_dir() != self.artifact_dir:
            raise OSError("the pi gate artifact directory moved since this launch")
        seq, payload = self.seal(session_id, text)
        name = os.path.basename(self._stem()) + f"{seq:0{_SEQ_WIDTH}d}.json"
        _publish_gate_artifact(
            self.artifact_dir, name, payload, stage_prefix=f"kirocrew_pi_gate_{os.getpid()}_"
        )
        return seq

    def remove(self, seq: int) -> None:
        """Remove steer *seq*'s file; absent already is the same. Blocking."""
        path = self._stem() + f"{seq:0{_SEQ_WIDTH}d}.json"
        with suppress(FileNotFoundError):
            os.remove(path)

    def discard(self) -> None:
        """Remove every steer file of this launch; absent already is the same.

        Every file in the directory carrying this launch's stem, so a write that
        landed after its caller stopped waiting is removed too. Blocking; callers
        run it off the loop.
        """
        stem = os.path.basename(self._stem())
        try:
            names = os.listdir(self.artifact_dir)
        except FileNotFoundError:
            names = []
        for name in names:
            if name.startswith(stem):
                with suppress(FileNotFoundError):
                    os.remove(os.path.join(self.artifact_dir, name))


def new_pi_steer_channel(nonce: str, artifact_dir: str) -> PiSteerChannel:
    """A fresh channel, with its own key, for the launch whose gate nonce is *nonce*."""
    return PiSteerChannel(nonce=nonce, artifact_dir=artifact_dir, key=os.urandom(32))


def steer_echo_seq(text: str, nonce: str) -> int | None:
    """The sequence number a consumed notification for *nonce* names, else ``None``.

    Exactly the extension's spelling: a JSON object with the marker, the nonce and a
    positive integer ``seq``, and nothing else. Anything that merely resembles it --
    another nonce, extra keys, model text -- is not one, and stays the text it is.
    """
    if not nonce or not text.startswith("{"):
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict) or set(body) != {_PI_STEER_MARKER, "nonce", "seq"}:
        return None
    seq, marker = body.get("seq"), body.get(_PI_STEER_MARKER)
    if type(marker) is not int or marker != 1 or body.get("nonce") != nonce:
        return None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        return None
    return seq

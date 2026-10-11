"""The run-hook payload: the one channel into a MicroVM at boot.

The payload is the ONLY channel into a MicroVM at boot. The platform delivers it
once, to a hook whose own budget is sixty seconds, and nothing else reaches the
guest until it has registered itself as a managed node. So what goes in it is
decided by what the guest cannot discover for itself:

- its SSM activation, because an agent registered at image-build time would bake
  one identity and one private key into the snapshot and share them with every VM
  launched from that image;
- a reference to its control secret, never the value;
- a reference to its model credential, for the same reason;
- the crew generation, so a readiness answer from the previous VM cannot satisfy
  this one.

Nothing about keeping the crew's home is in here. On this lane the home lives on
the VM's own disk and goes away with the VM, so there is no archive to point the
guest at and no wall edge for it to pack itself at.

Nothing in this module may put a secret VALUE in the payload. The payload is an
argument to ``RunMicrovm``, and AWS does not document whether ``runHookPayload``
is marked sensitive -- so it may sit in that account's CloudTrail request history.
A reference costs the guest one extra call and costs the owner nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.cloud.microvm.api import MAX_RUN_HOOK_PAYLOAD_BYTES


@dataclass(frozen=True)
class RunHookPayload:
    """What the platform hands the guest once, at boot.

    ``control_secret_ref`` is a REFERENCE. There is no field on this class for a
    secret value, so a caller cannot put one in by passing the wrong argument.
    """

    tag: str
    activation_id: str
    activation_code: str
    region: str
    control_secret_ref: str
    #: The crew's MODEL credential, by reference. Carried explicitly rather than
    #: derived from ``control_secret_ref``: the launch tag is minted by the
    #: launcher rather than chosen by the operator, so a name built from it names
    #: a secret nobody could have created, and the guest's read of it ends boot at
    #: its secrets stage. A reference, never a value, for the same reason as the
    #: control secret.
    identity_secret_ref: str
    #: Bumped on every launch and reopen, and echoed back by the guest, so a
    #: readiness answer from the previous VM cannot satisfy this one.
    generation: int

    def to_dict(self) -> dict[str, Any]:
        """The payload as the guest reads it.

        Short keys, because the budget is 4,096 bytes and the activation code and
        the two ARNs already take most of it. Measured in
        ``test_cloud_microvm_payload.py`` against that bound with worst-case
        values rather than asserted here.
        """
        return {
            "v": 1,
            "tag": self.tag,
            "gen": self.generation,
            "region": self.region,
            "ssm": {"id": self.activation_id, "code": self.activation_code},
            "secretRef": self.control_secret_ref,
            "identityRef": self.identity_secret_ref,
            # The guest must not spend ten minutes and 581 MB fetching an
            # embedding model it will not use before it can answer. Carried in the
            # payload rather than baked into the image so the decision is visible
            # at the launch that makes it.
            "env": {"KIROCREW_SKIP_MODEL_DOWNLOAD": "1", "KIROCREW_ALLOW_UNSANDBOXED": "1"},
        }

    def encode(self) -> str:
        """The payload as one compact JSON string, refused if it is over budget.

        Separators without spaces, because the only reason this is compact is the
        4,096-byte ceiling and pretty-printing one of these costs roughly a fifth
        of it.
        """
        text = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        size = len(text.encode("utf-8"))
        if size > MAX_RUN_HOOK_PAYLOAD_BYTES:
            raise ValueError(
                f"run hook payload is {size} bytes, over the {MAX_RUN_HOOK_PAYLOAD_BYTES}-byte "
                "limit; shorten the secret references"
            )
        return text


def decode_payload(text: "str | bytes") -> dict[str, Any]:
    """Read a payload back, as the guest does.

    Here rather than in the guest's own source so the two shapes are pinned by one
    test: a payload the launcher can write and the guest cannot read is a VM that
    boots, registers nothing and bills for eight hours. Which is exactly what
    happened live, three launches in a row, before this function knew
    the shape the guest is actually handed.

    **The platform does not deliver what the launcher wrote.** ``RunMicrovm``
    takes ``runHookPayload`` as a JSON string, and the run hook receives that
    string BASE64-ENCODED. Measured: a 434-byte payload arrived at the hook as a
    576-byte body, and 576 is the base64 length of 432 bytes. Nothing in the API
    reference says so, and a guest that calls ``json.loads`` on the body gets a
    decode error with no hint of why -- which reads exactly like a launcher bug.

    So three forms are accepted, in order, and the one that worked is recorded by
    :func:`payload_encoding` for a caller that wants to log it:

    1. raw JSON, which is what the launcher wrote and what a local harness
       delivers;
    2. base64 of that JSON, which is what the real platform delivers;
    3. a JSON envelope with the payload under ``payload`` or ``runHookPayload``,
       in either of the two forms above -- accepted defensively rather than
       because it was observed, since a platform that wraps once may wrap again.

    Being liberal here is the right direction: every form is checked for
    ``v == 1`` before it is believed, so a body this function cannot read is
    refused rather than guessed at.
    """
    data, _ = _decode_with_encoding(text)
    return data


def payload_encoding(text: "str | bytes") -> str:
    """Which of the accepted forms *text* is: ``json``, ``base64``, ``envelope``.

    Exists so a guest can LOG the form it received without logging the payload
    itself -- the payload carries a single-use activation code, so its content
    must not reach a log, and "which encoding arrived" is the one fact about it
    that is both safe and diagnostic.
    """
    return _decode_with_encoding(text)[1]


def _decode_with_encoding(text: "str | bytes") -> tuple[dict[str, Any], str]:
    raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    candidate = raw.strip()

    def _as_v1(value: object) -> Optional[dict[str, Any]]:
        return value if isinstance(value, dict) and value.get("v") == 1 else None

    direct: object = None
    try:
        direct = json.loads(candidate)
    except (ValueError, TypeError):
        direct = None
    found = _as_v1(direct)
    if found is not None:
        return found, "json"

    import base64
    import binascii

    try:
        # ``validate=True``: without it, base64 silently discards any character
        # outside the alphabet, so a JSON document that failed to parse above
        # would be "decoded" into bytes that are not it.
        decoded = base64.b64decode(candidate, validate=True).decode("utf-8")
        found = _as_v1(json.loads(decoded))
        if found is not None:
            return found, "base64"
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        pass

    if isinstance(direct, dict):
        for key in ("payload", "runHookPayload"):
            inner = direct.get(key)
            if isinstance(inner, (str, bytes)):
                try:
                    nested, _ = _decode_with_encoding(inner)
                except ValueError:
                    continue
                return nested, "envelope"

    raise ValueError(
        "not a version-1 MicroVM run hook payload: the body is neither JSON, nor "
        "base64 of JSON, nor an envelope carrying either. The platform delivers "
        "the launcher's string base64-encoded, so a raw-JSON-only reader fails here "
        "on every real launch"
    )

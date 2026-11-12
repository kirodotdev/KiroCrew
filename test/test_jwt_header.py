"""The shared JWT header check fails closed at every site that applies it."""

from __future__ import annotations

import base64
import types

import pytest

from kiro_crew.decisions import gate
from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security.redaction import redact_credentials

#: A JWT-shaped hit whose header does not decode to JSON: every site lets it through.
_LOOKALIKE = "token " + "ey" + "Jhbm.a.b"

#: Whether each site treats a text as carrying a credential.
_SITES = {
    "scrubber": lambda text: bool(redact_credentials(text)[1]),
    "log floor": lambda text: SecretRedactionFilter([]).redact(text) != text,
    "decisions gate": lambda text: gate.scrub_reason(text, []) == gate.ERROR_SCRUBBED_CREDENTIAL,
}


def _raise_recursion(_data: bytes, **_kwargs: object) -> object:
    raise RecursionError("maximum recursion depth exceeded while decoding a JSON object")


@pytest.mark.parametrize("site", sorted(_SITES))
def test_a_json_object_python_cannot_parse_stays_a_credential(monkeypatch, site) -> None:
    """An integer past Python's digit limit, or ``RecursionError``, does not open a path past it."""
    is_credential = _SITES[site]
    assert not is_credential(_LOOKALIKE)
    huge_int = base64.urlsafe_b64encode(b'{"n":' + b"1" * 5000 + b"}").decode().rstrip("=")
    assert is_credential(f"token {huge_int}.a.b")
    monkeypatch.setattr("kiro_crew.jwt_header.json", types.SimpleNamespace(loads=_raise_recursion))
    assert is_credential(_LOOKALIKE)

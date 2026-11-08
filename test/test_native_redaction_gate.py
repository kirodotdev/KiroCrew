"""The native credential gate (``kc_native``) answers exactly as the Python gate.

Skipped when the optional extension is not built. Set KC_REDACTION_CORPUS to a
JSON list of real strings to also diff a recorded corpus.
"""

from __future__ import annotations

import json
import os
import random
import sys

import pytest

from kiro_crew.security import redaction as R

kc_native = pytest.importorskip("kc_native")

PY = R._might_contain_credential_py
NATIVE = kc_native.might_contain_credential
_CODEPOINTS = [c for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]


@pytest.mark.skipif(
    os.environ.get("KIROCREW_NATIVE_REDACTION") == "0", reason="native gate switched off"
)
def test_fingerprint_matches_so_the_native_gate_is_live():
    assert kc_native.GATE_FINGERPRINT == R._native_gate_fingerprint()
    assert R._NATIVE_GATE is NATIVE


@pytest.mark.parametrize(
    "template",
    [
        "x://{c}:p@h",  # URI user class  [^\s:/@]
        "x://u:{c}@h",  # URI password class [^\s/]
        "Author{c}zation",  # case-folded i
        "{c}uthorization",  # case-folded a
        "AUTHORIZATIO{c}",
        "gh{c}_",
        "1234567:{c}" + "a" * 29,
        "M{c}" + "a" * 21 + ".",
    ],
)
def test_every_codepoint_at_each_class_position(template):
    mismatches = [hex(c) for c in _CODEPOINTS if PY(s := template.format(c=chr(c))) != NATIVE(s)]
    assert mismatches == []


_PIECES = [
    *R._CREDENTIAL_PREFILTER_LITERALS,
    *(lit[:-1] for lit in R._CREDENTIAL_PREFILTER_LITERALS),
    "gh",
    "_",
    "o",
    "p",
    "s",
    "u",
    "r",
    "ghx_",
    "://",
    ":",
    "@",
    "/",
    " ",
    "\t",
    "\x1c",
    "\x85",
    "\u3000",
    "\u2028",
    "1234567",
    "12345",
    "0",
    "aB9-_",
    "abcdefghij" * 3,
    "M",
    "N",
    "O",
    "Q",
    ".",
    "Authorization",
    "authorızation",
    "AUTHORİZATION",
    "authoriza",
    "tion",
    "é",
    "😀",
    "\u0130",
    "\u0131",
    "\u017f",
    "\u212a",
    "\n",
]


def _random_strings(n: int, seed: int) -> list[str]:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        out.append("".join(rnd.choice(_PIECES) for _ in range(rnd.randint(0, 12))))
    return out


def test_random_differential():
    strs = _random_strings(200_000, 20261010)
    bad = [s for s in strs if PY(s) != NATIVE(s)]
    assert bad == []
    # The generator reaches both answers often enough to mean something.
    assert 0.2 < sum(map(PY, strs)) / len(strs) < 0.95


def test_lone_surrogate_falls_back_to_python():
    s = "token \ud800 AKIA"
    with pytest.raises(UnicodeEncodeError):
        NATIVE(s)
    assert R._might_contain_credential(s) is True
    assert R._might_contain_credential("plain \udfff text") is False


@pytest.mark.parametrize(
    "text",
    ["中" * 100_000, "x" * 99_999 + "é", "😀" * 50_000, "a" * 100_000 + "\u3000"],
    ids=["ucs2-cjk", "latin1", "ucs4-emoji", "ascii-plus-wide-space"],
)
def test_scanning_does_not_grow_the_string(text):
    # PyUnicode_AsUTF8AndSize caches a UTF-8 copy inside the str for its whole life;
    # reading through it doubled-to-quadrupled every non-ASCII transcript row the
    # gateway keeps. The gate must leave the object exactly as large as it was.
    s = (text + "!")[:-1]  # a fresh object, never encoded before
    before = sys.getsizeof(s)
    NATIVE(s)
    NATIVE(s)
    assert sys.getsizeof(s) == before


def test_redact_credentials_is_byte_identical(monkeypatch):
    strs = _random_strings(20_000, 7) + [
        "Authorization: Bearer abc.def-ghi",
        "aws_secret_access_key = " + "Ab1/" * 10,
        "postgres://user:pa@ss@db/x",
        "ghp_" + "a" * 36,
        "1234567890:" + "A" * 35,
    ]
    native = [R.redact_credentials(s) for s in strs]
    monkeypatch.setattr(R, "_NATIVE_GATE", None)
    python = [R.redact_credentials(s) for s in strs]
    assert native == python


@pytest.mark.skipif(not os.environ.get("KC_REDACTION_CORPUS"), reason="no corpus given")
def test_recorded_corpus(monkeypatch):
    with open(os.environ["KC_REDACTION_CORPUS"], encoding="utf-8") as fh:
        strs = json.load(fh)
    gate_bad = [i for i, s in enumerate(strs) if PY(s) != R._might_contain_credential(s)]
    assert gate_bad == []
    native = [R.redact_credentials(s) for s in strs]
    monkeypatch.setattr(R, "_NATIVE_GATE", None)
    assert native == [R.redact_credentials(s) for s in strs]
    print(f"corpus: {len(strs)} strings identical", file=sys.stderr)

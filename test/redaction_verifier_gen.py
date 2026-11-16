"""Seeded generators for the verifier corpus (:mod:`redaction_verifier_corpus`).

Every value is fake and correctly shaped; one ``random.Random`` drives them all, so
the corpus is the same on every run.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import struct
import zlib

R = random.Random(37408)
B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
B64S = B62 + "+/"
B64U = B62 + "-_"
HEX = "0123456789abcdef"
UPD = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def pick(alpha, n):
    return "".join(R.choice(alpha) for _ in range(n))


def hexs(n):
    return pick(HEX, n)


def b62(n):
    return pick(B62, n)


def b64u(n):
    return pick(B64U, n)


def b64s(n):
    return pick(B64S, n)


def uuid():
    h = hexs(32)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:]}"


def key_id(prefix="AKIA"):
    return prefix + pick(UPD, 16)


def aws_secret():
    while True:
        s = b64s(40)
        if any(c in s for c in "+/") and any(c.isdigit() for c in s):
            return s


def _b62enc(n):
    out = ""
    while n:
        n, r = divmod(n, 62)
        out = B62[r] + out
    return out or "0"


def ghp(prefix="ghp_"):
    body = b62(30)
    return prefix + body + _b62enc(zlib.crc32(body.encode())).rjust(6, "0")


def b64url_json(obj):
    return (
        base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def jwt(payload, secret=None, header=None):
    h = b64url_json(header or {"alg": "HS256", "typ": "JWT"})
    p = b64url_json(payload)
    if secret is None:
        sig = b64u(43)
    else:
        sig = (
            base64.urlsafe_b64encode(
                hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
            )
            .decode()
            .rstrip("=")
        )
    return f"{h}.{p}.{sig}"


def png_data_uri(w=24, h=24):
    raw = b"".join(b"\x00" + bytes(R.randrange(256) for _ in range(w * 3)) for _ in range(h))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def pem_body(nlines=25, head="MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"):
    s = head + b64s(64 * nlines - len(head) - 20) + "=="
    return "\n".join(s[i : i + 64] for i in range(0, len(s), 64))

"""Synthetic credentials of the real shapes, built at run time for tests.

The redactor keeps identifiers visible (an AWS key ID, the AWS documentation
example key) and removes a value after an AWS secret label only in the real
shape, so a test that checks redaction needs a credential like these. Built
here, not written as literals, so no credential-shaped text lands in the repo.
Importable from every test tree, including the built-in apps' own tests.
"""

from __future__ import annotations

import string

from kiro_crew.security.redaction_structured.checks import base62_crc32

ALNUM = string.ascii_letters + string.digits
B64 = ALNUM + "+/"


def synth(n: int, alphabet: str = ALNUM, seed: int = 0) -> str:
    """A deterministic string of ``n`` characters that mixes the alphabet's classes."""
    step = 7 if len(alphabet) % 7 else 11
    return "".join(alphabet[(seed * 13 + i * step) % len(alphabet)] for i in range(n))


def github_classic(prefix: str = "ghp_", seed: int = 1) -> str:
    """A GitHub classic token with a valid checksum."""
    body = synth(30, ALNUM, seed)
    return prefix + body + base62_crc32(body)


def aws_secret(seed: int = 3) -> str:
    """A 40-character AWS secret access key."""
    return synth(40, B64, seed)


# Fixed sample values the tests share, joined from pieces so no source line carries
# a whole credential. None is a real credential.

#: A 40-character AWS secret access key, fully synthetic (no documentation example
#: prefix), with ``/`` at the same two places as a real key's shape.
SAMPLE_AWS_SECRET = "Qh4Tm2Vx9KpRc/Z8Nw3Hd/" + "Jf1Lb5Gs0Ye9Ua2WkE"
#: A Slack bot token.
SAMPLE_SLACK_BOT_TOKEN = "xoxb-1234567890-" + "0987654321-AbCdEfGh"

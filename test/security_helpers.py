"""Fake JOSE tokens shared by two redaction test modules.

``TestRedactCredentials`` (test_security.py) binds them as class attributes and
``TestStreamRedactor`` (test_security_stream_redaction.py) reads the same objects, so
each sample token is written once. ``_JWT`` is a three-segment JWS; ``_JWE_DIR`` is a
compact JWE whose Encrypted Key segment is empty (``alg: dir``).
"""

_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)

_JWE_DIR = (
    "eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4R0NNIn0"
    "."  # empty Encrypted Key segment (dir / ECDH-ES)
    ".48V1_ALb6US04U3b"
    ".5eym8TW_c8SuK0ltJ3rpYIz"
    "OeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji"
    ".XFBoMYUZodetZdvTiFvSkQ"
)

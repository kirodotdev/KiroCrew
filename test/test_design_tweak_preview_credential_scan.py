"""The design_tweak preview file scan reads the redactor's live matches.

``preview_files.contains_credential`` decides whether a file the preview would
serve carries a credential. It read the raw ``get_credential_patterns()`` with
``finditer``; once the redactor keeps the key that names a value and replaces
the value alone, a file holding text the redactor already cleaned
(``aws_secret_access_key=[REDACTED: credential]``) matched again and was refused
as a secret. The scan now iterates ``security.credential_matches`` through the
server runtime, which applies the redactor's own rule for a tag standing as the
value, and keeps its own exception for a quoted PEM marker without key material.
"""

from __future__ import annotations

import base64

from kiro_crew.apps.builtins.design_tweak.backend import preview_files, server
from kiro_crew.security import REDACTED_CREDENTIAL_TAG, credential_matches, redact_credentials

SECRET = b"aws_secret_access_key=test-secret-not-a-credential-0123\n"


def test_the_runtime_carries_the_live_match_iterator() -> None:
    assert server.credential_matches is credential_matches
    assert server._contains_credential(SECRET) is True


def test_text_the_redactor_already_cleaned_is_served() -> None:
    cleaned, warnings = redact_credentials(SECRET.decode())
    assert warnings and cleaned == f"aws_secret_access_key={REDACTED_CREDENTIAL_TAG}\n"
    assert preview_files.contains_credential(server, cleaned.encode()) is False
    quoted = f'{{"SecretAccessKey": "{REDACTED_CREDENTIAL_TAG}"}}\n'
    assert preview_files.contains_credential(server, quoted.encode()) is False


def test_a_secret_and_a_tag_with_glued_bytes_are_refused() -> None:
    assert preview_files.contains_credential(server, SECRET) is True
    glued = f"aws_secret_access_key={REDACTED_CREDENTIAL_TAG}test-secret-not-a-credential-0123\n"
    assert preview_files.contains_credential(server, glued.encode()) is True


def test_a_quoted_pem_marker_without_key_material_is_still_served() -> None:
    header = "-----" + "BEGIN RSA " + "PRIVATE " + "KEY-----"
    footer = "-----" + "END RSA " + "PRIVATE " + "KEY-----"
    assert (
        preview_files.contains_credential(server, f"see {header} in the docs\n".encode()) is False
    )
    body = base64.b64encode(b"not key material, a test fixture, long enough").decode()
    armoured = f"{header}\n{body}\n{footer}\n"
    assert preview_files.contains_credential(server, armoured.encode()) is True


def test_binary_content_is_never_a_credential() -> None:
    assert preview_files.contains_credential(server, b"\xff\xfe\x00binary") is False

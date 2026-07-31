"""Regression tests for the `pr_findings.py` credential redactor.

`pr_findings.py` prints UNTRUSTED CI-log and review-comment text, so it redacts
credentials first. It carries its OWN stdlib-only copy of the patterns because
the script is documented as portable and cannot import `kiro_crew.security`.
That copy required THREE `.`-separated segments, so the two-segment dashboard
link token (`base64url(payload).base64url(hmac_sig)`) never matched it.

Every case below uses the token in BARE PROSE, not as `token=<value>`. The
labelled form was already covered by `_KV_RE`, so a `?token=` case would pass
before the fix and prove nothing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "src" / "kiro_crew" / "builtin_skills" / "kirocrew-dev" / "prepare-pr" / "scripts" / "pr_findings.py"
)

# Same token shape the backend tests pin (`test_security.py`), so all three
# copies of the pattern are locked to one generator.
_LINK_PAYLOAD = (
    "eyJzdWIiOiJsb2NhbC1hcHAiLCJleHAiOjE3ODU0MTc2MDYsInNlc3Npb25fZXhwIjoxNzg1NDg5MzA2"
    "LCJpYXQiOjE3ODU0MTczMDYsIm5vbmNlIjoiOTM5YzE3MGQ5ZjBiNmEyMiIsImdlbiI6MH0"
)
_SIG = "gVhM4aKLA8dyFH-oZlQx6SpYSNPkXA07kpDhWd6UhZI"  # 43 chars, base64url


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_pr_findings", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCredentialRedaction:
    def test_redacts_bare_two_segment_link_token(self) -> None:
        """A link token in prose must be replaced whole, payload included."""
        module = _load_script()
        token = f"{_LINK_PAYLOAD}.{_SIG}"

        result = module.redact(f"open the dashboard with {token} before it expires")

        assert token not in result
        assert "eyJzdWIi" not in result, "the payload carries sub/exp/nonce claims"
        assert _SIG not in result

    def test_redacts_freshly_minted_link_token(self) -> None:
        """Tie the pattern to the real generator, not to a pasted sample.

        A hard-coded token cannot notice that `generate_token` changed shape.
        This mints one and fails if the copied pattern stops covering it.
        """
        module = _load_script()
        from kiro_crew.dashboard.token_auth import generate_token

        token = generate_token("local-app", 300, register_nonce=False)

        result = module.redact(f"link: {token}")

        assert token not in result
        assert token.split(".")[0] not in result

    def test_redacts_three_segment_jwt_whole(self) -> None:
        """A signed JWT must not be left with a dangling signature."""
        module = _load_script()
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
            ".dQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXcQdQw4w9WgXc"
        )

        result = module.redact(f"leaked in the log: {jwt}")

        assert jwt not in result
        for segment in jwt.split("."):
            assert segment not in result

    def test_keeps_signature_of_a_jws_matching_the_link_token_shape(self) -> None:
        """The one case where alternative ORDER is load-bearing.

        A conventional JWS header is 33 chars past `eyJ`, far below the
        link-token alternative's first-segment floor, so it cannot match a real
        JWS at all and the test above passes in either order. Order matters only
        when the header clears that floor AND the payload is exactly 43 chars,
        because the right boundary is satisfied by a `.`, so running the
        link-token alternative first leaves `.signature` in the printed log.
        """
        module = _load_script()
        sig = "C" * 43
        crafted = f"eyJ{'A' * 100}.{'B' * 43}.{sig}"

        result = module.redact(f"log: {crafted}")

        assert sig not in result
        assert crafted not in result

    def test_eyj_identifiers_not_redacted(self) -> None:
        """Ordinary code containing `eyJ` must survive verbatim.

        A left boundary alone cannot help at offset 0, so the corpus includes
        statement-initial identifiers as well as attribute access.
        """
        module = _load_script()
        for text in (
            "eyJsonSerializer.deserializeFromStringValue(x)",
            "eyJsonSerializerConfigurationFactoryBuilder.deserializeFromStringValue(x)",
            "obj.eyJsonReader.readValueFromInputStream(x)",
            "keyJson.get(raw)",
            "surveyJson.title",
            "eyJargonized.intercontinentalization",
        ):
            assert module.redact(text) == text, text

"""The run-hook payload: its budget, its round trip, and what it refuses to carry."""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm.api import MAX_RUN_HOOK_PAYLOAD_BYTES
from kiro_crew.cloud.microvm.payload import RunHookPayload, decode_payload


def _payload(**overrides) -> RunHookPayload:
    base = dict(
        tag="kc-a1b2c3",
        activation_id="0d4a4a6a-1111-2222-3333-444455556666",
        activation_code="abcdefghijklmnopqrstuvwx",
        region="us-east-1",
        control_secret_ref="kirocrew/crew/kc-a1b2c3/CONTROL_SECRET",
        identity_secret_ref="kirocrew/identity/demo-crew",
        generation=3,
    )
    base.update(overrides)
    return RunHookPayload(**base)  # type: ignore[arg-type]


class TestPayload:
    def test_a_worst_case_payload_fits_the_budget(self):
        """4,096 is the API reference's constraint and the number to budget against."""
        encoded = _payload().encode()
        assert len(encoded.encode("utf-8")) < MAX_RUN_HOOK_PAYLOAD_BYTES

    def test_an_oversized_payload_is_refused_before_the_launch(self):
        """A payload refused at RunMicrovm fails a launch that already minted one."""
        with pytest.raises(ValueError, match="over the"):
            _payload(control_secret_ref="kirocrew/crew/" + "x" * 5000 + "/CONTROL_SECRET").encode()

    def test_the_payload_round_trips(self):
        data = decode_payload(_payload().encode())
        assert data["tag"] == "kc-a1b2c3"
        assert data["gen"] == 3
        assert data["ssm"]["id"].startswith("0d4a4a6a")

    def test_an_unversioned_document_is_refused(self):
        with pytest.raises(ValueError, match="version-1"):
            decode_payload('{"tag": "a"}')

    def test_the_payload_says_nothing_about_keeping_the_home(self):
        """On this lane the home is on the VM's disk and goes away with it, so there
        is no archive to point the guest at and no wall edge to pack itself at. A
        payload that still carried either would be a promise nothing keeps."""
        data = decode_payload(_payload().encode())
        assert "archive" not in data
        assert "wall" not in data

    def test_the_payload_carries_a_reference_and_never_a_secret_value(self):
        """The payload is an API argument and may persist in CloudTrail history."""
        data = decode_payload(_payload().encode())
        assert data["secretRef"] == "kirocrew/crew/kc-a1b2c3/CONTROL_SECRET"
        flat = _payload().encode()
        assert "CONTROL_SECRET" in flat
        assert data["identityRef"] == "kirocrew/identity/demo-crew"
        # No field on the dataclass could hold a VALUE, which is the structural
        # half of the guarantee. Stated as a naming rule rather than a list, so a
        # field added later is covered: every secret-related field is a reference
        # and says so in its name, and one that did hold a value could not be
        # named ``_ref`` without lying.
        secret_fields = [n for n in RunHookPayload.__dataclass_fields__ if "secret" in n]
        assert secret_fields, "the guarantee is vacuous if no field matches"
        assert all(n.endswith("_ref") for n in secret_fields), secret_fields

    def test_the_embedding_model_download_is_turned_off_at_boot(self):
        """639 MB the crew will not use, fetched before it can answer anything."""
        env = decode_payload(_payload().encode())["env"]
        assert env["KIROCREW_SKIP_MODEL_DOWNLOAD"] == "1"

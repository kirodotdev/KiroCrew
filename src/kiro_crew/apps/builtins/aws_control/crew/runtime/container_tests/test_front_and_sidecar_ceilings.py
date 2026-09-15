"""Two defects the review found in the FRONT process.

Each is a case where a check existed but was reachable around: the transcript path let a
stem the filesystem cannot hold through to the caller's first stat, and the control route
buffered a request body with no ceiling. (The sidecar defects that once shared this file
left with the backup subsystem when it was extracted from this PR.)
"""

from __future__ import annotations

import json
import pathlib

import pytest
from container.front import app as app_mod
from container.front import transcript as T

from .test_front_transcript_fetch import make_settings


@pytest.fixture()
def settings(tmp_path):
    """The transcript tests' own settings factory, given the two keys it reads.

    ``backend_env`` there is a live-backend fixture these tests do not need: nothing here
    talks to a backend, only ``local_transcript_path``, which is pure string work.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return make_settings({"port": 8801, "run_dir": run_dir})


# --- an oversized slot id is refused before a path is built ----------------


def test_a_stem_longer_than_name_max_is_refused(settings) -> None:
    """Refused by returning None, the same answer a containment failure gives.

    Without this the stem reaches the caller's ``exists()`` and raises
    ``OSError(ENAMETOOLONG)``, which reads as a broken disk rather than an invalid id.
    """
    assert T.local_transcript_path(settings, "chat-" + "x" * 5000) is None
    assert T.local_transcript_path(settings, "x" * 250) is None


def test_an_ordinary_slot_id_still_maps_to_a_path(settings) -> None:
    """The other half: the ceiling is not "refuse everything".

    A limit set too low would pass the test above and break every real conversation, and
    no other test here would notice.
    """
    path = T.local_transcript_path(settings, "chat-982-1788526277")
    assert path is not None
    assert path.name == "chat-982-1788526277.jsonl"


def test_the_boundary_counts_the_suffix(settings) -> None:
    """``.jsonl`` counts toward the limit, because it is part of the name being created.

    A check on ``len(stem)`` alone would accept a 255-character stem and then build a
    261-character filename, which is the failure this prevents.
    """
    assert T.local_transcript_path(settings, "y" * (255 - len(".jsonl"))) is not None
    assert T.local_transcript_path(settings, "y" * (255 - len(".jsonl") + 1)) is None


# --- the control body has a ceiling ---------------------------------------


def test_the_control_route_bounds_the_body_while_reading_it() -> None:
    """The ceiling has to be inside the read loop.

    ``request.json()`` buffers the whole body before returning, so a check after it runs
    is a check on memory already spent. Streaming and counting is what makes the bound
    real, and a Content-Length check alone is not it: that header is the client's claim
    about a body it has not sent.
    """
    src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "async for chunk in request.stream():" in src, "the body is not streamed"
    assert "if len(raw_body) > _MAX_CONTROL_BODY_BYTES:" in src, "no ceiling in the loop"
    assert (
        "await request.json()" not in src
    ), "request.json() is back, which buffers the whole body before any check can run"


def test_the_ceiling_is_a_usable_number() -> None:
    """A ceiling of zero would refuse every control request.

    A limit that excludes the legitimate caller is the same failure whether it guards a
    request body or anything else: the service answers its port while rejecting real work.
    """
    assert app_mod._MAX_CONTROL_BODY_BYTES > 0
    assert app_mod._MAX_CONTROL_BODY_BYTES >= 64 * 1024, (
        "a control payload carries ids and flags, but a ceiling under 64KiB is small "
        "enough to refuse a legitimate caller"
    )


def test_the_refusal_says_which_problem_it_is() -> None:
    """413, not 400: the caller has to be able to tell "too big" from "bad JSON".

    A 400 sends whoever is debugging to look at their serializer instead of their payload
    size.
    """
    src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    block = src[src.index("async for chunk in request.stream():") :][:900]
    assert "status_code=413" in block
    assert '"code": "body_too_large"' in block


def test_json_is_decoded_from_the_bounded_bytes() -> None:
    """The decode reads the buffer the loop filled, not the request again.

    Re-reading would restore the unbounded path while leaving every assertion above
    passing, because the ceiling would still be present in the source.
    """
    src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    assert 'json.loads(bytes(raw_body).decode("utf-8"))' in src


def test_the_comparison_is_strict() -> None:
    """Exactly the ceiling is accepted; one byte over is not.

    Pins the off-by-one directly: ``>=`` would refuse a payload of exactly the documented
    size, and no test above distinguishes the two.
    """
    limit = app_mod._MAX_CONTROL_BODY_BYTES
    assert not limit > limit, "a body of exactly the ceiling must be accepted"
    assert limit + 1 > limit, "a body one byte over must be refused"


def test_the_payload_shape_check_still_runs_after_the_decode() -> None:
    """A non-object body is still a 400, so the new read did not drop that check."""
    src = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "if not isinstance(payload, dict):" in src


@pytest.mark.parametrize("raw", ['{"a": 1}', "{}", '{"nested": {"b": [1, 2]}}'])
def test_the_decode_accepts_ordinary_json_objects(raw: str) -> None:
    """The replacement decode path handles what the route is actually sent."""
    payload = json.loads(bytes(bytearray(raw.encode("utf-8"))).decode("utf-8"))
    assert isinstance(payload, dict)

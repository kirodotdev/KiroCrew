"""The personal drive uploads the bytes it already read and digested, never a re-read name.

``_put``'s callers read the artifact once (``_read_payload``), take its sha256 and enforce
the size limit, and then make several AWS round trips before the upload. Uploading by
re-reading the artifact's NAME would reach whatever that name points at after those round
trips, so a same-user process that replaced the file in that window would have its bytes
shipped under the original's digest -- off-host, under the user's own key, with no recall.
``_put`` instead uploads the in-memory payload directly, so no name is resolved a second
time: on POSIX from an anonymous temp file streamed as ``--body /dev/stdin``.

These tests assert that contract. On ``origin/main`` ``_put`` takes a path and passes
``--body <path>`` with no descriptor, so both fail there; on the branch they pass.
"""

from __future__ import annotations

import os

import pytest
from publish_personal_drive_helpers import _clean_registry  # noqa: F401 - fixture
from publish_personal_drive_helpers import payload_fixture  # noqa: F401 - fixture
from publish_personal_drive_helpers import _EngineSpy, _publish, _stub_profiles

from kiro_crew import platform_compat
from kiro_crew.publish import personal_drive as pd

_posix_only = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="the descriptor-as-body seam is POSIX-only (Windows has no /dev/stdin)",
)


@_posix_only
def test_publish_hands_the_body_over_as_a_descriptor_not_a_name(monkeypatch, payload):
    """The upload reaches the CLI as ``--body /dev/stdin`` over a real descriptor.

    Fails on main, where ``_put`` passes ``--body <path>`` with no ``stdin_fd``.
    """
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)

    _publish(pd.PersonalDriveProvider(), payload)

    # The body argument the CLI was given is the descriptor sentinel, not a name.
    assert spy.arg("put-object", "--body") == "/dev/stdin"
    # And a real descriptor was handed over as the child's stdin.
    assert spy.put_stdin_fds, "no put-object recorded"
    fd = spy.put_stdin_fds[-1]
    assert isinstance(fd, int) and fd >= 0


@_posix_only
def test_a_file_swapped_after_the_read_cannot_change_the_uploaded_bytes(monkeypatch, tmp_path):
    """The bytes S3 receives are the ones the caller read, not a later re-read of the name.

    This stages the exact exposure the issue names: the file is replaced AFTER
    ``_read_payload`` has read and digested it, in the window where ``publish`` is making
    its AWS round trips, and BEFORE the upload. The stub then plays the AWS CLI and reads
    what actually reaches ``--body``. Because ``_put`` uploads the in-memory payload rather
    than re-reading the name, the original bytes are what leave; re-reading the name would
    ship the substituted bytes, which is why this fails on main.
    """
    original = b"<h1>the bytes the product produced</h1>"
    swapped = b"<h1>bytes a same-user process substituted</h1>"
    artifact = tmp_path / "index.html"
    artifact.write_bytes(original)

    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)

    # Swap the file the instant the caller has finished reading it -- i.e. during the
    # round trips that precede the upload, which is the real window.
    real_read = pd.PersonalDriveProvider._read_payload

    def _read_then_swap(self, file_path):
        data = real_read(self, file_path)
        poison = tmp_path / "poison.html"
        poison.write_bytes(swapped)
        os.replace(poison, artifact)  # the name now resolves to different bytes
        return data

    monkeypatch.setattr(pd.PersonalDriveProvider, "_read_payload", _read_then_swap)

    uploaded: dict[str, bytes] = {}
    real_checked = pd.engine._checked

    def _reading_checked(args, profile, action="", timeout=0, stdin_fd=None, extra_visible_dirs=()):
        if args[:2] == ["s3api", "put-object"]:
            assert stdin_fd is not None, "no descriptor was handed over -- body passed by name"
            os.lseek(stdin_fd, 0, os.SEEK_SET)
            uploaded["body"] = os.read(stdin_fd, 1 << 20)
        return real_checked(args, profile, action=action, timeout=timeout, stdin_fd=stdin_fd)

    monkeypatch.setattr(pd.engine, "_checked", _reading_checked)

    _publish(pd.PersonalDriveProvider(), str(artifact))

    assert uploaded["body"] == original


def test_windows_pins_the_named_body_with_a_checksum(monkeypatch, tmp_path):
    """On the no-``/dev/stdin`` path the body is a name, so it is pinned by digest.

    Windows cannot hand the CLI a descriptor, so ``_put`` writes the payload to a private
    temp and uploads it by name -- a name the CLI re-opens. ``--checksum-sha256`` (base64
    of the payload's sha256) makes S3 reject a body a same-user process swapped in that
    window, which is the fail-closed equivalent of the descriptor path. This drives the
    Windows arm from any OS by forcing ``IS_POSIX`` false.
    """
    import base64
    import hashlib

    original = b"<h1>windows body</h1>"
    artifact = tmp_path / "index.html"
    artifact.write_bytes(original)

    monkeypatch.setattr(pd.platform_compat, "IS_POSIX", False)
    _stub_profiles(monkeypatch, ["alpha"])
    spy = _EngineSpy()
    spy.install(monkeypatch)

    _publish(pd.PersonalDriveProvider(), str(artifact))

    # The body is a named temp this method minted, never /dev/stdin and never the caller's
    # path, and it carries the checksum pin computed from the payload we read.
    body = spy.arg("put-object", "--body")
    assert body != "/dev/stdin"
    assert "kirocrew-drive-body-" in body
    expected = base64.b64encode(hashlib.sha256(original).digest()).decode("ascii")
    assert spy.arg("put-object", "--checksum-sha256") == expected

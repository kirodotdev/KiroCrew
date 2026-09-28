"""Unit tests for kiro_crew.asset_downloader — the shared verified-transfer engine.

No network: ``build_opener`` is replaced with a fake opener that streams bytes from
memory and honours (or deliberately ignores) a ``Range`` header, which is the only
way to exercise resume without a real partial-content server. The seam is the
opener rather than ``urlopen`` because that is what the module calls -- every
request goes through an opener carrying the same-host redirect handler, and a test
that patched ``urlopen`` would bypass the thing under test.

The properties under test are the ones the callers depend on: nothing reaches the
final path unverified, a partial is a staging file nobody serves, and a failure
answers with a reason rather than raising into a background thread.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import asset_downloader as dl

#: The real ``build_opener``, captured before the autouse no-network fixture
#: replaces it -- the handler-wiring test is about the real one.
_REAL_BUILD_OPENER = dl.build_opener

_PAYLOAD = b"".join(bytes([i % 251]) for i in range(4096))
_SHA = hashlib.sha256(_PAYLOAD).hexdigest()
_URL = "https://cdn.example.com/feature-videos/0.6.0/clip.mp4"


class _FakeResponse:
    def __init__(self, data: bytes, *, status: int, total: int) -> None:
        self._data = data
        self._pos = 0
        self.status = status
        self.headers = {"Content-Length": str(total)}

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _EndlessResponse:
    """A response whose body never ends -- the disk-fill shape."""

    status = 200
    headers: dict = {}

    def read(self, n: int) -> bytes:
        return b"\x00" * n

    def __enter__(self) -> "_EndlessResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(
    payload: bytes = _PAYLOAD,
    *,
    honour_range: bool = True,
    fail_first: bool = False,
    endless: bool = False,
):
    """Build a ``build_opener`` replacement, plus a state object recording what it saw.

    *endless* streams forever, which is how the disk-fill ceiling is tested: there
    is no other way to reach it, since a bounded payload always ends first.
    """
    state = SimpleNamespace(calls=0, ranges=[], urls=[])

    def _open(request, timeout=None):  # noqa: ANN001 - OpenerDirector.open signature
        state.calls += 1
        state.urls.append(getattr(request, "full_url", str(request)))
        rng = request.get_header("Range") if hasattr(request, "get_header") else None
        state.ranges.append(rng)
        if fail_first and state.calls == 1:
            raise urllib.error.URLError("fake network unreachable")
        if endless:
            return _EndlessResponse()
        if rng and honour_range:
            offset = int(str(rng).split("=", 1)[1].rstrip("-"))
            return _FakeResponse(payload[offset:], status=206, total=len(payload) - offset)
        return _FakeResponse(payload, status=200, total=len(payload))

    return SimpleNamespace(open=_open), state


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any test that forgets to install a fake must fail, not reach the network."""

    def _blocked(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("blocked by test fixture")

    monkeypatch.setattr(
        "kiro_crew.asset_downloader.build_opener",
        lambda *a, **k: SimpleNamespace(open=_blocked),
    )
    monkeypatch.setattr("kiro_crew.asset_downloader.urllib.request.urlopen", _blocked)


class TestHappyPath:
    def test_installs_the_verified_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clips" / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.calls == 1
        # Nothing left staged: the install is one os.replace off a temp name.
        assert [p.name for p in target.parent.iterdir()] == ["clip.mp4"]

    def test_reports_progress_and_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        seen: list[tuple[int, int]] = []
        verifying: list[bool] = []
        ok, _err = dl.download_to(
            tmp_path / "clip.mp4",
            _URL,
            sha256=_SHA,
            chunk_bytes=512,
            progress_every_bytes=1024,
            on_progress=lambda done, total: seen.append((done, total)),
            on_verifying=lambda: verifying.append(True),
        )
        assert ok is True
        assert verifying == [True]
        assert seen and seen[-1][0] <= len(_PAYLOAD)
        assert all(total == len(_PAYLOAD) for _done, total in seen)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
    def test_restrict_to_owner_locks_the_installed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        assert dl.download_to(target, _URL, sha256=_SHA, restrict_to_owner=True)[0] is True
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


class TestRefusals:
    def test_a_non_https_url_is_refused_without_a_request(self, tmp_path: Path) -> None:
        ok, err = dl.download_to(
            tmp_path / "clip.mp4", "http://cdn.example.com/clip.mp4", sha256=_SHA
        )
        assert ok is False
        assert "non-https" in err
        assert not (tmp_path / "clip.mp4").exists()

    def test_a_file_url_is_refused(self, tmp_path: Path) -> None:
        ok, _err = dl.download_to(tmp_path / "clip.mp4", "file:///etc/passwd", sha256=_SHA)
        assert ok is False

    def test_no_sha_pin_is_refused(self, tmp_path: Path) -> None:
        ok, err = dl.download_to(tmp_path / "clip.mp4", _URL, sha256="")
        assert ok is False
        assert "no sha256 pin" in err

    def test_sha_mismatch_installs_nothing_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256="0" * 64)
        assert ok is False
        assert "sha256 mismatch" in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_body_longer_than_the_declared_size_is_abandoned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest states the size; a longer body is a lie, not a bigger file."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=100, chunk_bytes=64)
        assert ok is False
        assert "ceiling" in err
        assert not target.exists()

    def test_a_too_small_payload_names_the_real_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, min_bytes=len(_PAYLOAD) + 1)
        assert ok is False
        assert "too small" in err
        assert not target.exists()

    def test_a_transport_failure_reports_it_and_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(fail_first=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert ok is False
        assert "HTTPS download failed" in err
        assert list(tmp_path.iterdir()) == []


class TestResume:
    def _part(self, target: Path, prefix: int) -> Path:
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(_PAYLOAD[:prefix])
        return part

    def test_continues_from_a_partial_and_still_verifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        self._part(target, 1000)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == ["bytes=1000-"]

    def test_a_server_ignoring_the_range_restarts_rather_than_appending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 200 answer to a ranged request must not be appended to the prefix."""
        open_fn, state = _fake_urlopen(honour_range=False)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        self._part(target, 1000)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert (ok, err) == (True, "")
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == ["bytes=1000-"]

    def test_a_partial_at_or_past_the_declared_size_is_discarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Those bytes are not a PREFIX of the wanted file, so resuming them is wrong."""
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"x" * (len(_PAYLOAD) + 10))
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == [None]

    def test_a_transport_failure_keeps_the_partial_for_the_next_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(fail_first=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = self._part(target, 1000)
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert ok is False
        assert part.is_file() and part.stat().st_size == 1000

    def test_a_corrupt_partial_fails_verification_rather_than_installing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest is end to end, so wrong bytes in the prefix cannot slip through."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = target.parent / f"{target.name}{dl.PART_SUFFIX}"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"\x00" * 1000)  # right length, wrong content
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert ok is False
        assert "sha256 mismatch" in err
        assert not target.exists()

    def test_a_non_resuming_caller_ignores_and_replaces_a_stale_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        staging = tmp_path / ".clip.mp4.tmp"
        staging.write_bytes(b"junk from a dead process")
        ok, _err = dl.download_to(target, _URL, sha256=_SHA, staging=staging, resume=False)
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert state.ranges == [None]


class TestRateLimit:
    def test_pacing_sleeps_and_the_payload_is_unaffected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        slept: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda s: slept.append(s))
        target = tmp_path / "clip.mp4"
        # 1 KiB/s over 4 KiB of payload: every chunk owes time it has not spent.
        ok, _err = dl.download_to(
            target, _URL, sha256=_SHA, rate_limit_bytes_per_s=1024, chunk_bytes=512
        )
        assert ok is True
        assert target.read_bytes() == _PAYLOAD
        assert slept and sum(slept) > 0

    def test_no_limit_means_no_sleeping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        slept: list[float] = []
        monkeypatch.setattr(dl.time, "sleep", lambda s: slept.append(s))
        assert dl.download_to(tmp_path / "clip.mp4", _URL, sha256=_SHA, chunk_bytes=512)[0] is True
        assert slept == []


class TestRedaction:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://user:pw@cdn.example.com/a/b.mp4", "https://cdn.example.com"),
            ("https://cdn.example.com/a.mp4?X-Amz-Signature=deadbeef", "https://cdn.example.com"),
            ("https://cdn.example.com/a.mp4#frag", "https://cdn.example.com"),
            ("https://cdn.example.com:8443/a.mp4", "https://cdn.example.com:8443"),
            ("https://cdn.example.com/t0ken-in-a-path/a.mp4", "https://cdn.example.com"),
        ],
    )
    def test_only_scheme_and_host_survive(self, raw: str, expected: str) -> None:
        """The PATH is dropped too -- a mirror can put a token in a path segment.

        Userinfo and a query string are the obvious credential carriers; the path
        is the one that looks safe and is not, and this string reaches the gateway
        log on every transfer. What a reader needs instead is the caller's *label*.
        """
        assert dl.redact_url(raw) == expected


class TestSameHostRedirects:
    """A url is authorized against ONE host, so a redirect may not change it."""

    def _handler_verdict(self, origin: str, target: str) -> object:
        handler = dl._SameHostRedirectHandler()
        request = urllib.request.Request(origin)
        # redirect_request returns a new Request to follow, or raises to refuse.
        try:
            return handler.redirect_request(request, None, 302, "Found", {}, target)
        except dl.RedirectRefused:
            return None

    def test_a_same_host_redirect_is_followed(self) -> None:
        verdict = self._handler_verdict(
            "https://cdn.example.com/a/clip.mp4", "https://cdn.example.com/b/clip.mp4"
        )
        assert verdict is not None

    def test_an_explicit_default_port_is_the_same_origin(self) -> None:
        """`:443` spelled out is not a different service."""
        assert (
            self._handler_verdict(
                "https://cdn.example.com/a/clip.mp4", "https://cdn.example.com:443/b/clip.mp4"
            )
            is not None
        )
        assert (
            self._handler_verdict(
                "https://cdn.example.com:443/a/clip.mp4", "https://cdn.example.com/b/clip.mp4"
            )
            is not None
        )

    @pytest.mark.parametrize(
        "target",
        [
            "https://attacker.example/clip.mp4",
            "https://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1:5476/api/sessions",
            "http://cdn.example.com/clip.mp4",
            "https://cdn.example.com.attacker.example/clip.mp4",
            # Same hostname, different port: a different service on the trusted
            # name, which is the SSRF one port over.
            "https://cdn.example.com:8443/clip.mp4",
            "https://cdn.example.com:not-a-port/clip.mp4",
        ],
    )
    def test_a_cross_host_or_plaintext_redirect_is_refused(self, target: str) -> None:
        assert self._handler_verdict("https://cdn.example.com/clip.mp4", target) is None

    def test_a_refusal_names_the_hosts_and_the_remedy_but_nothing_else(self) -> None:
        """The reason reaches the dashboard's status readout, so it must be safe AND useful."""
        handler = dl._SameHostRedirectHandler()
        request = urllib.request.Request("https://user:secretpw@cdn.example.com/a/clip.mp4?sig=1")
        with pytest.raises(dl.RedirectRefused) as excinfo:
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://other.example/b/clip.mp4?token=2"
            )
        reason = str(excinfo.value.reason)
        assert reason.startswith("redirect from cdn.example.com to other.example refused")
        assert "environment override" in reason
        for leaked in ("secretpw", "sig=1", "token=2", "/a/clip", "/b/clip"):
            assert leaked not in reason

    def test_the_opener_carries_the_handler(self) -> None:
        """Every request goes through this opener; urlopen's default would not."""
        opener = _REAL_BUILD_OPENER()
        assert any(isinstance(h, dl._SameHostRedirectHandler) for h in opener.handlers)
        assert not any(isinstance(h, dl._HttpsOnlyRedirectHandler) for h in opener.handlers)

    def test_the_relaxed_opener_is_opt_in_and_still_https_only(self) -> None:
        """For the operator's own env url: another https host is allowed, plaintext is not."""
        opener = _REAL_BUILD_OPENER(allow_cross_host_redirects=True)
        assert any(isinstance(h, dl._HttpsOnlyRedirectHandler) for h in opener.handlers)
        assert not any(isinstance(h, dl._SameHostRedirectHandler) for h in opener.handlers)
        handler = dl._HttpsOnlyRedirectHandler()
        request = urllib.request.Request("https://mirror.example/model.gguf")
        followed = handler.redirect_request(
            request, None, 302, "Found", {}, "https://storage.example/blob/model.gguf"
        )
        assert followed is not None
        for bad in ("http://mirror.example/model.gguf", "file:///etc/passwd", "https://x:nope/"):
            with pytest.raises(dl.RedirectRefused):
                handler.redirect_request(request, None, 302, "Found", {}, bad)


class TestCeiling:
    """Bytes are written as they arrive, so an endless body must be abandoned."""

    def test_an_endless_body_is_abandoned_at_the_explicit_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, max_bytes=4096, chunk_bytes=512)
        assert ok is False
        assert "ceiling" in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_the_ceiling_refusal_survives_a_locked_staging_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the ceiling, not the OS.

        The staging file is unlinked only after the write handle closes. Unlinking it
        while open raises WinError 32 on Windows, and the transport handler would then
        answer with that OS error instead of the ceiling refusal. Asserting on the
        MESSAGE is what makes this test fail on Windows if the unlink moves back
        inside the open block.
        """
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, max_bytes=64, chunk_bytes=32)
        assert ok is False
        assert "ceiling" in err
        assert "WinError" not in err
        assert "being used by another process" not in err
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_caller_that_declares_nothing_still_has_a_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No transfer is unbounded -- DEFAULT_MAX_BYTES applies when nothing is given."""
        open_fn, _state = _fake_urlopen(endless=True)
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        monkeypatch.setattr(dl, "DEFAULT_MAX_BYTES", 2048)
        ok, err = dl.download_to(tmp_path / "clip.mp4", _URL, sha256=_SHA, chunk_bytes=512)
        assert ok is False
        assert "ceiling" in err

    def test_max_bytes_does_not_discard_a_resumable_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is a BOUND, not a declared length, so it cannot mean "already complete"."""
        open_fn, state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        part = tmp_path / f"clip.mp4{dl.PART_SUFFIX}"
        part.write_bytes(_PAYLOAD[:1000])
        ok, _err = dl.download_to(
            target, _URL, sha256=_SHA, max_bytes=len(_PAYLOAD) * 4, resume=True
        )
        assert ok is True
        assert state.ranges == ["bytes=1000-"]


class TestStagingSymlink:
    """The staging path sits wherever the target does, so it may be plantable."""

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_the_descriptor_is_checked_against_the_name_not_the_other_way_round(
        self, tmp_path: Path
    ) -> None:
        """The Windows half of the guard, exercised where a symlink can be made.

        A descriptor opened on the real file passes. The same descriptor, with the
        NAME now a link (what a follow-on-open platform would have handed us), is
        refused: the link has its own identity, and it is not the file's. A second
        regular file under the name is refused the same way.
        """
        real = tmp_path / "real.part"
        real.write_bytes(b"partial")
        here = dl.TargetDir(tmp_path)
        fd = os.open(real, os.O_WRONLY | os.O_APPEND)
        try:
            dl._refuse_unless_named_file(fd, here, "real.part")
            planted = tmp_path / "planted.part"
            planted.symlink_to(tmp_path / "victim.json")
            with pytest.raises(dl.StagingRefused):
                dl._refuse_unless_named_file(fd, here, "planted.part")
            other = tmp_path / "other.part"
            other.write_bytes(b"different file")
            with pytest.raises(dl.StagingRefused):
                dl._refuse_unless_named_file(fd, here, "other.part")
        finally:
            os.close(fd)

    def test_resume_hashes_and_appends_through_one_descriptor(self, tmp_path: Path) -> None:
        """The digest is built from the descriptor that will be appended to -- not from
        a name that is re-opened later. Removing the name after the open changes
        nothing: the bytes land in the inode that was hashed."""
        staging = tmp_path / "clip.mp4.part"
        staging.write_bytes(b"0123456789")
        resumed = dl._resume_partial(dl.TargetDir(tmp_path), "clip.mp4.part", size=0)
        assert resumed is not None
        out, offset, digest = resumed
        try:
            assert offset == 10
            assert digest.hexdigest() == hashlib.sha256(b"0123456789").hexdigest()
            hashed = dl._identity(os.fstat(out.fileno()))
            if sys.platform.startswith("win"):
                out.write(b"x")
                out.flush()
                assert staging.read_bytes() == b"0123456789x"
            else:
                staging.unlink()
                out.write(b"x")
                out.flush()
                assert dl._identity(os.fstat(out.fileno())) == hashed
                assert os.fstat(out.fileno()).st_size == 11
        finally:
            out.close()

    def test_nothing_resumable_means_none_and_the_fresh_path_replaces_it(
        self, tmp_path: Path
    ) -> None:
        here = dl.TargetDir(tmp_path)
        assert dl._resume_partial(here, "clip.mp4.part", size=0) is None
        staging = tmp_path / "clip.mp4.part"
        staging.write_bytes(b"")
        assert dl._resume_partial(here, "clip.mp4.part", size=0) is None
        staging.write_bytes(b"0123456789")
        assert dl._resume_partial(here, "clip.mp4.part", size=10) is None
        assert dl._resume_partial(here, "clip.mp4.part", size=5) is None
        with dl._open_staging_nofollow(here, "clip.mp4.part") as out:
            out.write(b"fresh")
        assert staging.read_bytes() == b"fresh"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX: replace an open file")
    def test_a_same_size_partial_swapped_in_after_the_hash_is_neither_appended_to_nor_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The GPT-found shape: the prefix is hashed, then a same-size file with other
        bytes replaces the partial at its name. The append goes to the hashed inode
        (held open), never to the replacement, and the install refuses the
        replacement because it is not the inode the digest was computed on."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        staging = tmp_path / f"clip.mp4{dl.PART_SUFFIX}"
        staging.write_bytes(_PAYLOAD[:1000])
        real_resume = dl._resume_partial

        def _hash_then_swap(target_dir, name, **kwargs):  # noqa: ANN001
            resumed = real_resume(target_dir, name, **kwargs)
            assert resumed is not None
            swapped = tmp_path / "swapped.part"
            swapped.write_bytes(b"\x00" * 1000)  # same size, other bytes
            os.replace(swapped, staging)
            return resumed

        monkeypatch.setattr(dl, "_resume_partial", _hash_then_swap)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, size=len(_PAYLOAD), resume=True)
        assert ok is False
        assert "replaced" in err
        assert not target.exists()
        # The replacement was never written to before the install removed it.
        assert not staging.exists()

    def test_a_hard_linked_partial_is_refused_before_a_byte_is_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second name for the staging inode: not a link, the same inode, so every
        other rule passes. The link count on the descriptor is what refuses it, and
        the user's other name keeps exactly the bytes it had."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        victim = tmp_path / "notes.txt"
        victim.write_bytes(_PAYLOAD[:4])
        staging = tmp_path / f"clip.mp4{dl.PART_SUFFIX}"
        try:
            os.link(victim, staging)
        except OSError as exc:  # pragma: no cover - a filesystem without hard links
            pytest.skip(f"hard links unavailable here: {exc}")
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert ok is False
        assert "hard-linked" in err
        assert victim.read_bytes() == _PAYLOAD[:4]
        assert not target.exists()

    def test_a_file_swapped_in_at_the_staging_name_after_the_hash_is_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest covers the bytes written to ONE inode. A different file put at
        the staging name between the last write and the rename must not be installed
        under that digest: the installed name is compared to the verified inode and
        removed when it is not the same file. Unlink-and-recreate is the hard shape:
        a freed inode number is handed straight back on ext4 and tmpfs, so the
        comparison only holds because the hashed descriptor is still open."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        target = tmp_path / "clip.mp4"
        staging = tmp_path / f"clip.mp4{dl.PART_SUFFIX}"
        real_install = dl._install

        def _swap_then_install(target_dir, staging_name, name, **kwargs):  # noqa: ANN001
            staging.unlink()
            staging.write_bytes(b"not the bytes that were hashed")
            if sys.platform != "win32":
                # The hashed descriptor is still open, so its inode is still
                # allocated and the recreated file cannot have landed on it.
                assert dl._identity(os.stat(staging)) != kwargs["verified"]
            return real_install(target_dir, staging_name, name, **kwargs)

        monkeypatch.setattr(dl, "_install", _swap_then_install)
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert ok is False
        assert "replaced during the transfer" in err
        assert not target.exists()
        assert not staging.exists()

    def test_the_installed_name_is_the_inode_that_was_hashed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        seen: list[tuple[int, int]] = []
        real_install = dl._install

        def _record(target_dir, staging_name, name, *, verified, **kwargs):  # noqa: ANN001
            seen.append(verified)
            return real_install(target_dir, staging_name, name, verified=verified, **kwargs)

        monkeypatch.setattr(dl, "_install", _record)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA)
        assert (ok, err) == (True, "")
        assert seen == [dl._identity(os.stat(target))]

    def test_the_link_count_is_judged_on_the_descriptor(self, tmp_path: Path) -> None:
        real = tmp_path / "real.part"
        real.write_bytes(b"partial")
        try:
            os.link(real, tmp_path / "other-name.part")
        except OSError as exc:  # pragma: no cover - a filesystem without hard links
            pytest.skip(f"hard links unavailable here: {exc}")
        fd = os.open(real, os.O_WRONLY | os.O_APPEND)
        try:
            with pytest.raises(dl.StagingRefused, match="hard-linked"):
                dl._refuse_unless_named_file(fd, dl.TargetDir(tmp_path), "real.part")
        finally:
            os.close(fd)

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_fresh_open_destroys_a_planted_link_and_creates_a_real_file(
        self, tmp_path: Path
    ) -> None:
        """The removal is of the LINK; the create is exclusive; the victim is never opened."""
        victim = tmp_path / "victim.json"
        victim.write_bytes(b"do not touch")
        staging = tmp_path / "clip.mp4.part"
        staging.symlink_to(victim)
        with dl._open_staging_nofollow(dl.TargetDir(tmp_path), "clip.mp4.part") as out:
            out.write(b"fresh")
        assert victim.read_bytes() == b"do not touch"
        assert not staging.is_symlink() and staging.read_bytes() == b"fresh"

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_link_planted_between_the_removal_and_the_create_fails_the_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The window the exclusive create closes: a name that exists at create time refuses.

        The removal is stubbed out to stand for an adversary who re-plants the
        link the instant it is removed; ``O_EXCL`` then fails on the name itself,
        on every platform, and nothing is written through it.
        """
        victim = tmp_path / "victim.json"
        victim.write_bytes(b"do not touch")
        staging = tmp_path / "clip.mp4.part"
        staging.symlink_to(victim)
        monkeypatch.setattr(dl, "_remove_stale_staging", lambda _d, _n: None)
        with pytest.raises(dl.StagingRefused):
            dl._open_staging_nofollow(dl.TargetDir(tmp_path), "clip.mp4.part")
        assert victim.read_bytes() == b"do not touch"

    def test_a_directory_at_the_staging_name_is_refused_not_removed(self, tmp_path: Path) -> None:
        staging = tmp_path / "clip.mp4.part"
        staging.mkdir()
        (staging / "keep").write_bytes(b"x")
        with pytest.raises(dl.StagingRefused):
            dl._open_staging_nofollow(dl.TargetDir(tmp_path), "clip.mp4.part")
        assert (staging / "keep").exists()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_symlinked_staging_path_is_refused_without_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        victim = tmp_path / "protected.json"
        victim.write_bytes(b"do not touch")
        target = tmp_path / "clip.mp4"
        (tmp_path / f"clip.mp4{dl.PART_SUFFIX}").symlink_to(victim)
        ok, err = dl.download_to(target, _URL, sha256=_SHA, resume=True)
        assert ok is False
        assert "symlink" in err.lower()
        assert victim.read_bytes() == b"do not touch"
        assert not target.exists()

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_fresh_download_discards_a_planted_symlink_instead_of_following_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-resuming caller unlinks the staging path first, which is the safe act.

        ``unlink`` removes the LINK, never its target, so the plant is destroyed
        rather than written through and the transfer proceeds normally. Pinned
        because the opposite -- opening the stale path -- is the bug, and because
        this branch cannot be reached by the resume test above.
        """
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        victim = tmp_path / "protected.json"
        victim.write_bytes(b"do not touch")
        staging = tmp_path / "staged.tmp"
        staging.symlink_to(victim)
        target = tmp_path / "clip.mp4"
        ok, err = dl.download_to(target, _URL, sha256=_SHA, staging=staging, resume=False)
        assert (ok, err) == (True, "")
        assert victim.read_bytes() == b"do not touch"
        assert target.read_bytes() == _PAYLOAD


class TestPinnedTargetDir:
    """The directory half of the no-follow guarantee: nothing above the leaf is re-resolved."""

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_transfer_lands_in_the_pinned_directory_after_its_name_is_swapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ancestor swap: the checked folder is renamed away and a link takes its name.

        Every by-name step of the old transfer — the staging open, the install
        rename, the lockdown — would have followed the link into ``victim``. With
        the directory held open the bytes land in the folder that was pinned, under
        its new name, and the victim directory receives nothing.
        """
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        cache = tmp_path / "cache"
        cache.mkdir()
        victim = tmp_path / "victim"
        victim.mkdir()
        with dl.pin_target_dir(cache) as pinned:
            cache.rename(tmp_path / "moved")
            cache.symlink_to(victim, target_is_directory=True)
            ok, err = dl.download_to(
                cache / "clip.mp4", _URL, sha256=_SHA, restrict_to_owner=True, target_dir=pinned
            )
        assert (ok, err) == (True, "")
        assert (tmp_path / "moved" / "clip.mp4").read_bytes() == _PAYLOAD
        assert list(victim.iterdir()) == []
        assert stat.S_IMODE((tmp_path / "moved" / "clip.mp4").stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
    def test_a_link_at_the_directory_name_cannot_be_pinned(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.mkdir()
        (tmp_path / "cache").symlink_to(victim, target_is_directory=True)
        with pytest.raises(dl.TargetDirRefused):
            with dl.pin_target_dir(tmp_path / "cache"):
                pass

    def test_a_file_at_the_directory_name_cannot_be_pinned(self, tmp_path: Path) -> None:
        (tmp_path / "cache").write_bytes(b"not a directory")
        with pytest.raises(OSError):
            with dl.pin_target_dir(tmp_path / "cache"):
                pass

    def test_the_by_name_fallback_still_completes_a_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Where ``dir_fd`` is not available the pin is the held handle, and every
        operation is by name under it. Exercised here by taking that branch on
        purpose, since the other platform cannot be tested from this one."""
        open_fn, _state = _fake_urlopen()
        monkeypatch.setattr("kiro_crew.asset_downloader.build_opener", lambda *a, **k: open_fn)
        monkeypatch.setattr(dl.pinned_fs, "supports_pinned_walk", lambda: False)
        cache = tmp_path / "cache"
        cache.mkdir()
        with dl.pin_target_dir(cache) as pinned:
            assert pinned._relative is False
            ok, err = dl.download_to(
                cache / "clip.mp4", _URL, sha256=_SHA, restrict_to_owner=True, target_dir=pinned
            )
            pinned.write_text("note.txt", "hello")
        assert (ok, err) == (True, "")
        assert (cache / "clip.mp4").read_bytes() == _PAYLOAD
        assert (cache / "note.txt").read_text() == "hello"

    def test_a_staging_file_elsewhere_is_refused_up_front(self, tmp_path: Path) -> None:
        """The staging file is addressed through the target's directory, so it must be there."""
        ok, err = dl.download_to(
            tmp_path / "a" / "clip.mp4", _URL, sha256=_SHA, staging=tmp_path / "b" / "x.tmp"
        )
        assert ok is False
        assert "beside" in err

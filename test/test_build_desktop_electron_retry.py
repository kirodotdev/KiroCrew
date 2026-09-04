"""Regression guard for the electron-builder retry loop in
``packaging/build-desktop.sh`` (#3088).

Background
----------
``Build Desktop`` used to abort on the FIRST failure of any kind unless the
electron-builder log matched the literal string ``ENOTEMPTY`` (the macOS
``.DS_Store`` temp-dir race, electron-builder#6890). Every other transient,
per-execution failure — a dropped connection ("socket hang up") or a
TLS-intercepted response ("self-signed certificate") from electron-builder's
own mid-build fetches (the AppImage/NSIS/Squirrel tooling, pulled AFTER the
electron zip itself has already downloaded) — fell straight through to
``exit 1`` with zero retries, even though the same commit reliably passed on
a plain re-run.

The same hole then reopened one layer up (#6795): every pattern in the
network class was a socket-level errno, so a fetch the CDN answered with an
HTTP ``504`` matched none of them and aborted on attempt 1 with the
three-attempt budget unspent. Retryable statuses (``5xx`` and ``429``) now
land in that class too; every other ``4xx`` deliberately does not, because a
401/403/404 is a configuration fault that fails identically on all three
attempts.

``run_electron_builder_with_retry`` classifies a failure into one of two
transient classes (each with its own cleanup/backoff) or "unknown", and only
the two known classes get a bounded retry; anything else — or the budget
being exhausted — still fails the build, exactly as before.

Why this test exists
---------------------
The real failures are per-execution network/TLS events and can't be
reproduced deterministically. What CAN be tested deterministically is the
retry/classification logic itself: shadow ``_eb_invoke`` (the sole point
where the real ``electron-builder`` binary is invoked) with a stub that fails
N times with a chosen error string, and assert the real loop above it
retries the right number of times and reaches the right outcome.

The function body is extracted from the shipped script (not copied), so the
test tracks the real source of truth, mirroring
``test_build_desktop_rm_resilient.py``'s approach to ``rm_rf_resilient``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="run_electron_builder_with_retry is a POSIX bash helper"
)

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract_function(name: str) -> str:
    text = SCRIPT.read_text()
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name}() not found in packaging/build-desktop.sh"
    return m.group(0)


def _run(stub_calls: str, harness: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run a bash snippet with a stubbed ``_eb_invoke`` and the real retry
    function sourced above it. ``sleep`` is a no-op so the real backoff
    (``attempt * 10`` seconds for the network class) doesn't slow the suite.
    """
    script = (
        "set -uo pipefail\n"  # not -e: the harness itself checks $? after the call
        "sleep() { :; }\n"
        f"{stub_calls}\n"
        f"{_extract_function('run_electron_builder_with_retry')}\n"
        f"{harness}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        # The child is the bash snippet assembled above, so its output encoding
        # is ours to know: pin UTF-8 rather than inheriting the locale (the
        # Windows ANSI code page) via bare text=True.
        encoding="utf-8",
        timeout=30,
    )


def _counting_stub(tmp_path: Path, fail_times: int, error_line: str) -> tuple[str, Path]:
    """A ``_eb_invoke`` stub that fails ``fail_times`` times with
    ``error_line`` on stderr, then succeeds."""
    counter = tmp_path / "calls"
    counter.write_text("")
    stub = f"""
_eb_invoke() {{
  printf 'x' >> "{counter}"
  n=$(wc -c < "{counter}" | tr -d ' ')
  if [ "$n" -le {fail_times} ]; then
    echo "{error_line}" >&2
    return 1
  fi
  return 0
}}
"""
    return stub, counter


class TestElectronBuilderRetryWiring:
    def test_packaging_step_calls_the_retry_function(self) -> None:
        """A revert to a bare/inline electron-builder invocation must fail
        here, not just silently drop the retry+classification logic."""
        text = SCRIPT.read_text()
        assert "run_electron_builder_with_retry() {" in text
        assert re.search(
            r'(?m)^\s*run_electron_builder_with_retry\s+"\$\{EB_ARGS\[@\]\}"\s*$', text
        ), "packaging step must call run_electron_builder_with_retry with EB_ARGS"
        # And the old bare-loop call site must be gone.
        assert './node_modules/.bin/electron-builder "${EB_ARGS[@]}" 2>&1 | tee' not in text


class TestTransientClassesRetry:
    def test_retries_on_ds_store_enotempty(self, tmp_path: Path) -> None:
        (tmp_path / "dist").mkdir()
        stub, counter = _counting_stub(
            tmp_path, fail_times=1, error_line="Error: ENOTEMPTY: directory not empty"
        )
        proc = _run(stub, 'run_electron_builder_with_retry; echo "EXIT=$?"', tmp_path)
        assert "EXIT=0" in proc.stdout, proc.stdout + proc.stderr
        assert len(counter.read_text()) == 2, "must retry exactly once then succeed"

    @pytest.mark.parametrize(
        "error_line",
        [
            "RequestError: socket hang up",
            "self-signed certificate; if the root CA is installed locally, try running Node.js with --use-system-ca",
            "Error: connect ECONNRESET",
            "Error: connect ETIMEDOUT",
            # HTTP-level failures from the SAME `got` fetches. Every case above
            # is a socket-level errno; a CDN that answers with a 5xx/429
            # instead of dropping the connection is the same per-execution
            # event one layer up, and used to fall through to a hard abort on
            # attempt 1 with the 3-attempt budget unspent. The first case is
            # the verbatim line from PR #6795, Build Desktop (ubuntu-22.04).
            "⨯ Response code 504 (Gateway Time-out)  failedTask=build "
            "stackTrace=HTTPError: Response code 504 (Gateway Time-out)",
            "HTTPError: Response code 500 (Internal Server Error)",
            "HTTPError: Response code 502 (Bad Gateway)",
            "HTTPError: Response code 503 (Service Unavailable)",
            "HTTPError: Response code 429 (Too Many Requests)",
        ],
    )
    def test_retries_on_transient_network_failure(self, tmp_path: Path, error_line: str) -> None:
        (tmp_path / "dist").mkdir()
        stub, counter = _counting_stub(tmp_path, fail_times=1, error_line=error_line)
        proc = _run(stub, 'run_electron_builder_with_retry; echo "EXIT=$?"', tmp_path)
        assert "EXIT=0" in proc.stdout, proc.stdout + proc.stderr
        assert len(counter.read_text()) == 2

    def test_gives_up_after_max_attempts_on_known_transient_class(self, tmp_path: Path) -> None:
        """A PERSISTENT outage (every attempt fails) must still fail the
        build once the retry budget (3 attempts) is exhausted -- this is not
        a mask, only a bounded number of extra tries."""
        (tmp_path / "dist").mkdir()
        stub, counter = _counting_stub(
            tmp_path, fail_times=99, error_line="RequestError: socket hang up"
        )
        proc = _run(stub, 'run_electron_builder_with_retry; echo "EXIT=$?"', tmp_path)
        assert "EXIT=1" in proc.stdout, proc.stdout + proc.stderr
        assert len(counter.read_text()) == 3, "must stop at max_attempts, not retry forever"

    @pytest.mark.parametrize(
        "error_line",
        [
            "Error: Cannot find module 'some-broken-config-key'",
            # A 4xx that is NOT 429 is a configuration/authorisation fault, not
            # a transient one: retrying a bad URL or a missing credential just
            # spends the budget and delays the real verdict by two backoffs.
            # These pin that widening the class to HTTP did not widen it to
            # ALL of HTTP -- without them the 5xx pattern could be a bare
            # "Response code" match and every test above would still pass.
            "HTTPError: Response code 404 (Not Found)",
            "HTTPError: Response code 403 (Forbidden)",
            "HTTPError: Response code 401 (Unauthorized)",
            "HTTPError: Response code 400 (Bad Request)",
        ],
    )
    def test_does_not_retry_an_unrecognised_failure(self, tmp_path: Path, error_line: str) -> None:
        """A genuine build error (e.g. a real packaging/config mistake) must
        abort on its FIRST occurrence with no retries -- only the two known
        transient classes get a second chance."""
        (tmp_path / "dist").mkdir()
        stub, counter = _counting_stub(tmp_path, fail_times=99, error_line=error_line)
        proc = _run(stub, 'run_electron_builder_with_retry; echo "EXIT=$?"', tmp_path)
        assert "EXIT=1" in proc.stdout, proc.stdout + proc.stderr
        assert len(counter.read_text()) == 1, "an unrecognised error must not be retried at all"

    def test_forwards_eb_args_to_the_real_invocation(self, tmp_path: Path) -> None:
        """EB_ARGS must reach electron-builder unmangled through the retry
        wrapper."""
        seen = tmp_path / "seen_args"
        stub = f"""
_eb_invoke() {{
  printf '%s\\n' "$@" > "{seen}"
  return 0
}}
"""
        (tmp_path / "dist").mkdir()
        proc = _run(
            stub,
            'run_electron_builder_with_retry --mac "-c.extraMetadata.version=1.2.3"; echo "EXIT=$?"',
            tmp_path,
        )
        assert "EXIT=0" in proc.stdout, proc.stdout + proc.stderr
        assert seen.read_text().splitlines() == ["--mac", "-c.extraMetadata.version=1.2.3"]

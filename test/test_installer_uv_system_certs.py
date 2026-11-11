"""cli.sh's managed-Python download behind a TLS-intercepting proxy.

uv checks TLS against its own bundled CA roots, so a proxy whose CA only the
system store trusts fails the interpreter download with ``UnknownIssuer`` while
curl, which reads the system store, got through. ``_provision_python_via_uv``
runs alone here, with a stub standing in for the pinned uv tarball, so no
network and no real uv are involved.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded

CLI_SH = Path(__file__).resolve().parents[1] / "cli.sh"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="cli.sh is POSIX shell (macOS + Linux)")

# Every target cli.sh can pick, so the stub is found whatever host runs this.
_UV_TARGETS = (
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
)
_MANAGED_PY = "/managed/bin/python3.12"

# Records each install attempt's UV_NATIVE_TLS, and fails the way uv does when
# its bundled roots do not know the proxy's CA unless told to use the system store.
_UV_STUB = f"""#!/bin/sh
case "$1 $2" in
  "python install")
    echo "native=${{UV_NATIVE_TLS:-}}" >> "$ATTEMPTS"
    echo "$*" >> "$ATTEMPTS.args"
    # The real uv would link python3.12 into ~/.local/bin without --no-bin.
    [ "$3" = "--no-bin" ] || {{ echo "stub: python install without --no-bin: $*" >&2; exit 3; }}
    [ "${{UV_NATIVE_TLS:-0}}" != 0 ] && exit 0
    echo "  Caused by: invalid peer certificate: UnknownIssuer" >&2
    exit 2 ;;
  "python find") echo {_MANAGED_PY} ;;
esac
"""

_CURL_STUB = """#!/bin/sh
while [ "$#" -gt 0 ]; do
  case "$1" in -o) cp "$UV_TARBALL" "$2"; shift 2 ;; *) shift ;; esac
done
"""


def _uv_tarball() -> bytes:
    buf = io.BytesIO()
    body = _UV_STUB.encode()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for target in _UV_TARGETS:
            info = tarfile.TarInfo(f"uv-{target}/uv")
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _provision(tmp_path: Path, extra_env: dict[str, str]) -> tuple[str, list[str]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(_CURL_STUB)
    curl.chmod(0o755)
    tarball = tmp_path / "uv.tar.gz"
    tarball.write_bytes(_uv_tarball())
    sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    function = re.search(
        r"^_provision_python_via_uv\(\) \{\n.*?^\}\n", CLI_SH.read_text(), re.M | re.S
    )
    assert function is not None, "cli.sh no longer defines _provision_python_via_uv()"
    work = tmp_path / "work"
    work.mkdir()
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -eu\n"
        f'TMP="{work}"; CURL_PROGRESS=-s; UV_VERSION=0.0.0; UV_PYTHON_SERIES=cpython-3.12\n'
        f"UV_SHA_LINUX_X64={sha}; UV_SHA_LINUX_ARM64={sha}\n"
        f"UV_SHA_MACOS_X64={sha}; UV_SHA_MACOS_ARM64={sha}\n"
        'if command -v sha256sum >/dev/null 2>&1; then SHA_CMD=sha256sum; else SHA_CMD="shasum -a 256"; fi\n'
        'err() { echo "err: $*" >&2; exit 1; }\n'
        f'_py_usable() {{ [ "$1" = {_MANAGED_PY} ]; }}\n'
        'PY=""\n'
        f"{function.group(0)}"
        'if _provision_python_via_uv; then echo "PY=$PY"; else echo "provision failed"; fi\n'
    )
    attempts = tmp_path / "attempts"
    env = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "KIROCREW_HOME": str(tmp_path / "data-home"),
        "ATTEMPTS": str(attempts),
        "UV_TARBALL": str(tarball),
        **extra_env,
    }
    result = run_bounded(["sh", str(harness)], env)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = attempts.read_text().splitlines() if attempts.exists() else []
    return result.stdout + result.stderr, recorded


def test_a_certificate_refusal_is_retried_on_the_system_store(tmp_path: Path) -> None:
    output, attempts = _provision(tmp_path, {})

    assert attempts == ["native=", "native=1"], output
    assert "Retrying the Python download with the system certificate store" in output
    assert f"PY={_MANAGED_PY}" in output


def test_every_install_attempt_skips_the_bin_link(tmp_path: Path) -> None:
    """``--no-bin`` on both tries: uv links nothing into ~/.local/bin, so it never
    warns mid-install that that directory is off PATH."""
    output, _attempts = _provision(tmp_path, {})

    args = (tmp_path / "attempts.args").read_text().splitlines()
    assert args == ["python install --no-bin cpython-3.12"] * 2, output


def test_a_user_set_uv_native_tls_is_not_overridden(tmp_path: Path) -> None:
    output, attempts = _provision(tmp_path, {"UV_NATIVE_TLS": "0"})

    assert attempts == ["native=0"], output
    assert "provision failed" in output

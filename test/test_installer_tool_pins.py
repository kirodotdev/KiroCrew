"""Bootstrap downloads are pinned and checksum-verified before they run.

* mise (``ensure-python.sh``, ``ensure-node.sh``, ``install.sh --mise``) comes
  from one pinned release checked against a recorded sha256
  (``scripts/lib/pinned-mise.sh``). No installer pipes a download into a shell;
  ``ensure-node.sh`` runs unprompted from gateway start and ``kirocrew update``,
  so this matters beyond an interactive install.
* ``cloud-install.sh`` and ``install.ps1`` install the AWS CLI and Session
  Manager plugin as root/admin, from versioned URLs and only when the file
  matches the sha256 recorded in ``packaging/pins/aws-tools.env``.

The behavioural checks run the real shell helpers against a stub ``curl``.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MISE_LIB = REPO_ROOT / "scripts" / "lib" / "pinned-mise.sh"
AWS_PINS = REPO_ROOT / "packaging" / "pins" / "aws-tools.env"
CLOUD_INSTALL = REPO_ROOT / "cloud-install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"
DOCKERFILE = REPO_ROOT / "src/kiro_crew/apps/builtins/aws_control/crew/runtime/Dockerfile"
SHELL_INSTALLERS = (
    "ensure-python.sh",
    "ensure-node.sh",
    "install.sh",
    "setup.sh",
    "cloud-install.sh",
    "minimal_install.sh",
)

_PIPE_TO_SHELL = re.compile(r"\b(?:curl|wget)\b[^#]*\|\s*(?:ba|z)?sh\b")
_SHA = re.compile(r"[0-9a-f]{64}")

posix_only = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell helpers"
)


def _code_lines(path: Path) -> list[str]:
    return [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    ]


def _env_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\s*([A-Z0-9_]+)=(\S+)\s*", line)
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


# ---------------------------------------------------------------- static ---


@pytest.mark.parametrize("name", SHELL_INSTALLERS)
def test_no_installer_pipes_a_download_into_a_shell(name: str) -> None:
    hits = [ln.strip() for ln in _code_lines(REPO_ROOT / name) if _PIPE_TO_SHELL.search(ln)]
    assert not hits, f"{name} pipes a download into a shell: {hits}"


@pytest.mark.parametrize("name", ["ensure-python.sh", "ensure-node.sh", "install.sh"])
def test_mise_installs_go_through_the_pinned_helper(name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert "scripts/lib/pinned-mise.sh" in text and "install_pinned_mise" in text


def test_mise_pin_is_one_version_with_six_checksums() -> None:
    text = MISE_LIB.read_text(encoding="utf-8")
    assert re.search(r'^MISE_PIN_VERSION="\d{4}\.\d+\.\d+"$', text, re.M)
    shas = re.findall(r'^MISE_PIN_SHA256_[A-Z0-9_]+="([0-9a-f]{64})"$', text, re.M)
    assert len(shas) == 6 and len(set(shas)) == 6


@pytest.mark.parametrize("path", [CLOUD_INSTALL, INSTALL_PS1], ids=lambda p: p.name)
def test_aws_tool_urls_are_versioned(path: Path) -> None:
    code = "\n".join(_code_lines(path))
    assert "/latest/" not in code, f"{path.name} still downloads a 'latest' artifact"
    assert not re.search(
        r"amazonaws\.com/AWSCLIV2\.(?:pkg|msi)", code
    ), "unversioned AWS CLI package URL"
    assert not re.search(
        r"awscli-exe-linux-(?:x86_64|aarch64)\.zip", code
    ), "unversioned AWS CLI zip URL"


def test_cloud_install_aws_steps_only_download_through_the_verifier() -> None:
    text = CLOUD_INSTALL.read_text(encoding="utf-8")
    steps = text[text.index('step 2 "AWS CLI"') : text.index("step 4 ")]
    raw = [
        ln.strip()
        for ln in steps.splitlines()
        if re.search(r"\bcurl\b", ln) and not ln.lstrip().startswith("#")
    ]
    assert not raw, f"AWS CLI / SSM steps call curl directly instead of fetch_verified: {raw}"
    assert steps.count("fetch_verified ") >= 5


def test_install_ps1_downloads_only_through_the_verifier() -> None:
    code = _code_lines(INSTALL_PS1)
    downloads = [ln.strip() for ln in code if "Invoke-WebRequest" in ln]
    assert downloads == ["Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing"]
    joined = "\n".join(code)
    assert joined.count("Get-VerifiedFile $") == 2
    assert "Get-FileHash" in joined


def test_aws_pins_are_complete_and_match_the_runtime_image() -> None:
    pins = _env_pins(AWS_PINS)
    shas = {k: v for k, v in pins.items() if "SHA256" in k}
    assert len(shas) == 11 and all(_SHA.fullmatch(v) for v in shas.values())
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins["AWSCLI_VERSION"])
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", pins["SSM_PLUGIN_VERSION"])
    docker = DOCKERFILE.read_text(encoding="utf-8")
    assert f"ARG AWSCLI_VERSION={pins['AWSCLI_VERSION']}" in docker
    assert f"ARG AWSCLI_SHA256_X86_64={pins['AWSCLI_SHA256_LINUX_X86_64']}" in docker
    assert f"ARG AWSCLI_SHA256_AARCH64={pins['AWSCLI_SHA256_LINUX_AARCH64']}" in docker
    # Every variable the two installers read exists in the pin file.
    used = set(
        re.findall(r"\$\{?((?:AWSCLI|SSM)_[A-Z0-9_]+)", CLOUD_INSTALL.read_text(encoding="utf-8"))
    )
    used |= set(
        re.findall(r'\$toolPins\["([A-Z0-9_]+)"\]', INSTALL_PS1.read_text(encoding="utf-8"))
    )
    assert used and used <= set(pins), f"read but not pinned: {sorted(used - set(pins))}"


# ------------------------------------------------------------ behavioural ---


def _stub_curl(bin_dir: Path, payload: Path) -> None:
    """A curl that writes *payload* to its ``-o`` target and touches a marker."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        'out=""\n'
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f'cp "{payload}" "$out"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)


def _mise_tarball(path: Path, marker: Path) -> str:
    """A tarball shaped like a mise release whose 'binary' would touch *marker*."""
    script = f"#!/bin/sh\ntouch '{marker}'\n".encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("mise/bin/mise")
        info.size = len(script)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(script))
    path.write_bytes(buf.getvalue())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_mise(
    tmp_path: Path, override_sha: str | None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    marker = tmp_path / "executed"
    payload = tmp_path / "payload.tar.gz"
    real_sha = _mise_tarball(payload, marker)
    _stub_curl(tmp_path / "bin", payload)
    dest = tmp_path / "home" / ".local" / "bin" / "mise"
    sha = real_sha if override_sha is None else override_sha
    overrides = "; ".join(
        f"MISE_PIN_SHA256_{k}={sha}"
        for k in (
            "LINUX_X64",
            "LINUX_ARM64",
            "LINUX_X64_MUSL",
            "LINUX_ARM64_MUSL",
            "MACOS_X64",
            "MACOS_ARM64",
        )
    )
    proc = subprocess.run(
        ["bash", "-c", f'. "$1"; {overrides}; install_pinned_mise', "bash", str(MISE_LIB)],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "MISE_INSTALL_PATH": str(dest),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert not marker.exists(), "the downloaded mise was executed by the installer"
    return proc, dest


@posix_only
def test_pinned_mise_refuses_a_checksum_mismatch(tmp_path: Path) -> None:
    proc, dest = _run_mise(tmp_path, "0" * 64)
    assert proc.returncode != 0
    assert not dest.exists()
    assert "Checksum mismatch" in proc.stdout


@posix_only
def test_pinned_mise_installs_a_matching_archive(tmp_path: Path) -> None:
    proc, dest = _run_mise(tmp_path, None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert dest.is_file() and os.access(dest, os.X_OK)


def _fetch_verified_source() -> str:
    text = CLOUD_INSTALL.read_text(encoding="utf-8")
    start = text.index("fetch_verified() {")
    end = text.index("\n}", start) + 2
    helpers = "\n".join(ln for ln in text.splitlines() if re.match(r"^(warn|has)\(\)", ln))
    return helpers + "\n" + text[start:end]


@posix_only
@pytest.mark.parametrize("matches", [True, False])
def test_cloud_install_fetch_verified_keeps_only_a_matching_file(
    tmp_path: Path, matches: bool
) -> None:
    payload = tmp_path / "artifact"
    payload.write_bytes(b"pretend installer")
    want = hashlib.sha256(payload.read_bytes()).hexdigest() if matches else "f" * 64
    _stub_curl(tmp_path / "bin", payload)
    out = tmp_path / "dl" / "pkg"
    out.parent.mkdir()
    proc = subprocess.run(
        [
            "bash",
            "-c",
            _fetch_verified_source() + '\nfetch_verified "https://example.invalid/x" "$1" "$2"',
            "bash",
            str(out),
            want,
        ],
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert (proc.returncode == 0) is matches, proc.stdout + proc.stderr
    assert out.exists() is matches

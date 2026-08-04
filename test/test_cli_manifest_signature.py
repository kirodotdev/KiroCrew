"""Signed CLI artifact-manifest and installer verification tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "signing" / "cli-manifest.py"
INSTALLER = ROOT / "cli.sh"
CLI_WORKFLOW = ROOT / ".github" / "workflows" / "publish-cli.yml"
PINNED_PUBLIC_KEY = ROOT / "packaging" / "signing" / "cli-manifest-public.pem"
VERSION = "1.2.3"
CHANNEL = "stable"
WHEEL_NAME = f"kirocrew-{VERSION}-py3-none-any.whl"
CDN_BASE = "https://fixtures.invalid"


@dataclass(frozen=True)
class SigningKey:
    private: Path
    public: Path
    key_id: str
    public_b64: str


@pytest.fixture(scope="module")
def test_key(tmp_path_factory: pytest.TempPathFactory) -> SigningKey:
    root = tmp_path_factory.mktemp("cli-manifest-key")
    private = root / "private.pem"
    public = root / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:3072",
            "-out",
            str(private),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(public), "-outform", "DER"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return SigningKey(
        private=private,
        public=public,
        key_id=f"sha256:{hashlib.sha256(der).hexdigest()}",
        public_b64=base64.b64encode(public.read_bytes()).decode("ascii"),
    )


def _run_helper(
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _write_canonical_json(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
    )


def _build_manifest(
    root: Path,
    key: SigningKey,
    wheel: Path,
    *,
    channel: str = CHANNEL,
    artifact_base: str = CDN_BASE,
) -> Path:
    payload = root / "payload.json"
    signature = root / "signature.bin"
    manifest = root / "cli-manifest.json"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel_url = f"{artifact_base.rstrip('/')}/cli/{channel}/{VERSION}/{WHEEL_NAME}"
    _run_helper(
        "payload",
        "--channel",
        channel,
        "--version",
        VERSION,
        "--wheel-url",
        wheel_url,
        "--sha256",
        digest,
        "--python-requires",
        ">=3.10",
        "--pub-date",
        "2026-08-01T00:00:00Z",
        "--public-key",
        str(key.public),
        "--output",
        str(payload),
    )
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(key.private),
            "-out",
            str(signature),
            str(payload),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _run_helper(
        "assemble",
        "--payload",
        str(payload),
        "--signature",
        str(signature),
        "--public-key",
        str(key.public),
        "--output",
        str(manifest),
    )
    return manifest


def test_helper_builds_a_canonical_independently_verifiable_manifest(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"signed wheel bytes")
    manifest_path = _build_manifest(tmp_path, test_key, wheel)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema"] == "kirocrew-cli-artifact-manifest-v1"
    assert manifest["algorithm"] == "RSASSA_PKCS1_V1_5_SHA_256"
    assert manifest["key_id"] == test_key.key_id
    assert manifest["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()

    signature = tmp_path / "decoded-signature.bin"
    signature.write_bytes(base64.b64decode(manifest.pop("signature"), validate=True))
    payload = tmp_path / "reconstructed-payload.json"
    _write_canonical_json(payload, manifest)
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(test_key.public),
            "-signature",
            str(signature),
            str(payload),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_helper_refuses_to_assemble_a_tampered_payload(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"original")
    manifest = _build_manifest(tmp_path, test_key, wheel)
    assert manifest.exists()

    payload = tmp_path / "payload.json"
    signature = tmp_path / "signature.bin"
    data = json.loads(payload.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    _write_canonical_json(payload, data)
    refused = _run_helper(
        "assemble",
        "--payload",
        str(payload),
        "--signature",
        str(signature),
        "--public-key",
        str(test_key.public),
        "--output",
        str(tmp_path / "refused.json"),
        check=False,
    )
    assert refused.returncode == 1
    assert "rejected" in refused.stderr
    assert not (tmp_path / "refused.json").exists()


def test_repository_public_key_is_explicitly_unconfigured_or_valid() -> None:
    result = _run_helper(
        "key-info",
        "--public-key",
        str(PINNED_PUBLIC_KEY),
        check=False,
    )
    if b"UNCONFIGURED" in PINNED_PUBLIC_KEY.read_bytes():
        assert result.returncode == 1
        assert "not configured" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["key_id"].startswith("sha256:")


def _installer_with_trust_root(tmp_path: Path, *, key_id: str, public_b64: str) -> Path:
    source = INSTALLER.read_text(encoding="utf-8")
    source, key_id_changes = re.subn(
        r'^CLI_MANIFEST_KEY_ID="[^"]*"$',
        f'CLI_MANIFEST_KEY_ID="{key_id}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    source, public_key_changes = re.subn(
        r'^CLI_MANIFEST_PUBLIC_KEY_B64="[^"]*"$',
        f'CLI_MANIFEST_PUBLIC_KEY_B64="{public_b64}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    assert key_id_changes == public_key_changes == 1
    script = tmp_path / "cli.sh"
    script.write_text(source, encoding="utf-8")
    script.chmod(0o755)
    return script


def _patched_installer(
    tmp_path: Path,
    key: SigningKey,
    *,
    key_id: str | None = None,
    public_b64: str | None = None,
) -> Path:
    return _installer_with_trust_root(
        tmp_path,
        key_id=key.key_id if key_id is None else key_id,
        public_b64=key.public_b64 if public_b64 is None else public_b64,
    )


def _write_fake_tools(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    tools = root / "tools"
    tools.mkdir()
    curl_marker = root / "curl-called"
    install_marker = root / "pipx-installed"
    curl = tools / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu
touch "$FAKE_CURL_MARKER"
out=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  https://fixtures.invalid/*) rel=${url#https://fixtures.invalid/} ;;
  *) echo "unexpected URL: $url" >&2; exit 9 ;;
esac
[ -n "$out" ] || exit 10
cp "$FAKE_CDN_ROOT/$rel" "$out"
""",
        encoding="utf-8",
    )
    pipx = tools / "pipx"
    pipx.write_text(
        """#!/bin/sh
set -eu
case "$1" in
  install) touch "$FAKE_INSTALL_MARKER" ;;
  environment) printf '%s\\n' "$HOME/.local/bin" ;;
  *) exit 11 ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    pipx.chmod(0o755)
    return tools, curl_marker, install_marker


def _stage_cdn(root: Path, manifest: Path, wheel: Path, *, include_feed: bool = True) -> Path:
    cdn = root / "cdn"
    version_dir = cdn / "cli" / CHANNEL / VERSION
    version_dir.mkdir(parents=True)
    shutil.copy2(wheel, version_dir / WHEEL_NAME)
    shutil.copy2(manifest, version_dir / "cli-manifest.json")
    if include_feed:
        feed_dir = cdn / "feed" / CHANNEL
        feed_dir.mkdir(parents=True)
        shutil.copy2(manifest, feed_dir / "latest-cli.json")
    return cdn


def _run_installer(
    script: Path,
    root: Path,
    cdn: Path,
    *args: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    tools, curl_marker, install_marker = _write_fake_tools(root)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tools}{os.pathsep}{env['PATH']}",
            "HOME": str(root / "home"),
            "KIROCREW_HOME": str(root / "data-home"),
            "FAKE_CDN_ROOT": str(cdn),
            "FAKE_CURL_MARKER": str(curl_marker),
            "FAKE_INSTALL_MARKER": str(install_marker),
        }
    )
    result = subprocess.run(
        ["sh", str(script), "--cdn", CDN_BASE, *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return result, curl_marker, install_marker


@pytest.mark.parametrize("pinned", [False, True])
def test_installer_verifies_signature_and_digest_before_installing(
    tmp_path: Path, test_key: SigningKey, pinned: bool
) -> None:
    case = tmp_path / ("pinned" if pinned else "channel")
    case.mkdir()
    wheel = case / WHEEL_NAME
    wheel.write_bytes(b"verified wheel")
    manifest = _build_manifest(case, test_key, wheel)
    cdn = _stage_cdn(case, manifest, wheel, include_feed=not pinned)
    script = _patched_installer(case, test_key)

    extra = ("--version", VERSION) if pinned else ()
    result, curl_marker, install_marker = _run_installer(script, case / "run", cdn, *extra)

    assert result.returncode == 0, result.stderr
    assert curl_marker.exists()
    assert install_marker.exists()
    assert "Verified signed manifest." in result.stdout
    assert "Verified SHA-256." in result.stdout


@pytest.mark.parametrize("pinned", [False, True])
def test_installer_refuses_when_signed_manifest_is_missing(
    tmp_path: Path, test_key: SigningKey, pinned: bool
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    manifest = _build_manifest(tmp_path, test_key, wheel)
    cdn = _stage_cdn(tmp_path, manifest, wheel, include_feed=False)
    if pinned:
        (cdn / "cli" / CHANNEL / VERSION / "cli-manifest.json").unlink()
    script = _patched_installer(tmp_path, test_key)

    extra = ("--version", VERSION) if pinned else ()
    result, _, install_marker = _run_installer(script, tmp_path / "run", cdn, *extra)

    assert result.returncode == 1
    assert "signed CLI manifest not found" in result.stderr
    assert not install_marker.exists()


def test_installer_refuses_corrupted_embedded_public_key_before_network(
    tmp_path: Path, test_key: SigningKey
) -> None:
    corrupted = base64.b64encode(b"not an RSA public key").decode("ascii")
    script = _patched_installer(tmp_path, test_key, public_b64=corrupted)

    result, curl_marker, install_marker = _run_installer(
        script, tmp_path / "run", tmp_path / "unused-cdn"
    )

    assert result.returncode == 1
    assert "embedded CLI manifest public key is invalid" in result.stderr
    assert not curl_marker.exists(), "a corrupted trust root must fail before network I/O"
    assert not install_marker.exists()


def test_installer_refuses_a_manifest_changed_after_signing(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"verified wheel")
    manifest_path = _build_manifest(tmp_path, test_key, wheel)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    cdn = _stage_cdn(tmp_path, manifest_path, wheel)
    script = _patched_installer(tmp_path, test_key)

    result, _, install_marker = _run_installer(script, tmp_path / "run", cdn)

    assert result.returncode == 1
    assert "manifest signature verification failed" in result.stderr
    assert not install_marker.exists()


def test_installer_refuses_wheel_bytes_that_do_not_match_signed_digest(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"signed wheel")
    manifest = _build_manifest(tmp_path, test_key, wheel)
    wheel.write_bytes(b"tampered wheel")
    cdn = _stage_cdn(tmp_path, manifest, wheel)
    script = _patched_installer(tmp_path, test_key)

    result, _, install_marker = _run_installer(script, tmp_path / "run", cdn)

    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not install_marker.exists()


def test_installer_refuses_unsigned_legacy_feed_without_fallback(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    legacy = {
        "channel": CHANNEL,
        "version": VERSION,
        "wheel_url": f"{CDN_BASE}/cli/{CHANNEL}/{VERSION}/{WHEEL_NAME}",
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "python_requires": ">=3.10",
        "pub_date": "2026-08-01T00:00:00Z",
    }
    manifest = tmp_path / "legacy.json"
    manifest.write_text(json.dumps(legacy), encoding="utf-8")
    cdn = _stage_cdn(tmp_path, manifest, wheel)
    script = _patched_installer(tmp_path, test_key)

    result, _, install_marker = _run_installer(script, tmp_path / "run", cdn)

    assert result.returncode == 1
    assert "malformed signed manifest" in result.stderr
    assert not install_marker.exists()


def test_installer_fails_before_network_when_trust_root_is_unconfigured(
    tmp_path: Path,
) -> None:
    script = _installer_with_trust_root(
        tmp_path,
        key_id="UNCONFIGURED",
        public_b64="UNCONFIGURED",
    )
    result, curl_marker, install_marker = _run_installer(
        script, tmp_path / "run", tmp_path / "unused-cdn"
    )

    assert result.returncode == 1
    assert "manifest signing trust root is not configured" in result.stderr
    assert not curl_marker.exists(), "unconfigured trust must fail before any network request"
    assert not install_marker.exists()


def test_installer_and_repository_public_key_are_one_fail_closed_contract() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    key_id = re.search(r'^CLI_MANIFEST_KEY_ID="([^"]+)"$', source, re.M)
    key_b64 = re.search(r'^CLI_MANIFEST_PUBLIC_KEY_B64="([^"]+)"$', source, re.M)
    assert key_id and key_b64

    public_bytes = PINNED_PUBLIC_KEY.read_bytes()
    if key_id.group(1) == "UNCONFIGURED":
        assert key_b64.group(1) == "UNCONFIGURED"
        assert b"UNCONFIGURED" in public_bytes
    else:
        assert key_id.group(1).startswith("sha256:")
        assert base64.b64decode(key_b64.group(1), validate=True) == public_bytes
        assert b"UNCONFIGURED" not in public_bytes


def test_kms_signer_requires_matching_non_exportable_key_and_verifies_output(
    tmp_path: Path, test_key: SigningKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"kms-signed wheel")
    payload = tmp_path / "payload.json"
    signature = tmp_path / "signature.bin"
    manifest = tmp_path / "manifest.json"
    _run_helper(
        "payload",
        "--channel",
        CHANNEL,
        "--version",
        VERSION,
        "--wheel-url",
        f"{CDN_BASE}/cli/{CHANNEL}/{VERSION}/{WHEEL_NAME}",
        "--sha256",
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "--python-requires",
        ">=3.10",
        "--pub-date",
        "2026-08-01T00:00:00Z",
        "--public-key",
        str(test_key.public),
        "--output",
        str(payload),
    )
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(test_key.private),
            "-out",
            str(signature),
            str(payload),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    public_der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", str(test_key.public), "-outform", "DER"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout

    spec = importlib.util.spec_from_file_location("cli_manifest_test_helper", HELPER)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    key_arn = "arn:aws:kms:us-west-2:000000000000:key/test"
    aws_calls: list[list[str]] = []

    def fake_aws_json(args: list[str]) -> dict[str, object]:
        aws_calls.append(args)
        if args[:2] == ["kms", "get-public-key"]:
            return {
                "KeyUsage": "SIGN_VERIFY",
                "KeySpec": "RSA_3072",
                "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"],
                "PublicKey": base64.b64encode(public_der).decode("ascii"),
            }
        if args[:2] == ["kms", "sign"]:
            return {
                "Signature": base64.b64encode(signature.read_bytes()).decode("ascii")
            }
        raise AssertionError(f"unexpected AWS CLI arguments: {args}")

    monkeypatch.setattr(helper, "_run_aws_json", fake_aws_json)
    helper._kms_sign_command(
        argparse.Namespace(
            payload=payload,
            key_arn=key_arn,
            public_key=test_key.public,
            output=manifest,
        )
    )

    assert [call[:2] for call in aws_calls] == [
        ["kms", "get-public-key"],
        ["kms", "sign"],
    ]
    assert json.loads(manifest.read_text(encoding="utf-8"))["key_id"] == test_key.key_id


def _workflow_steps() -> list[dict]:
    workflow = yaml.safe_load(CLI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["publish-cli"]["steps"]


def _workflow_step(name: str) -> dict:
    return next(step for step in _workflow_steps() if step.get("name") == name)


def test_publish_workflow_fails_closed_and_uses_non_exportable_kms_signing() -> None:
    steps = _workflow_steps()
    names = [step.get("name") for step in steps]
    guard = _workflow_step("Refuse partial CLI signing configuration")
    signer = _workflow_step("Build and sign CLI artifact manifest")
    publisher = _workflow_step("Publish wheel and signed channel manifest")

    assert names.index("Configure AWS credentials") < names.index(signer["name"])
    assert names.index(signer["name"]) < names.index(publisher["name"])
    assert guard["if"] == "env.HAS_PUBLISH_ROLE != env.HAS_MANIFEST_KEY"
    assert "kms-sign" in signer["run"]
    assert "CLI_MANIFEST_SIGNING_KEY_ARN" in CLI_WORKFLOW.read_text(encoding="utf-8")
    assert "PRIVATE_KEY" not in signer["run"]
    assert "AWS_SECRET_ACCESS_KEY" not in signer["run"]


def test_publish_workflow_uses_a_deterministic_manifest_publication_date() -> None:
    run = _workflow_step("Build and sign CLI artifact manifest")["run"]

    assert 'git show -s --format=%ct "$GITHUB_SHA"' in run
    assert 'date -u -d "@${SOURCE_DATE_EPOCH}"' in run
    assert '--pub-date "$PUB_DATE"' in run
    assert '--pub-date "$(date -u' not in run


def _verify_manifest(
    manifest: Path,
    key: SigningKey,
    *,
    expected_channel: str = CHANNEL,
    artifact_base: str = CDN_BASE,
) -> subprocess.CompletedProcess[str]:
    return _run_helper(
        "verify",
        "--manifest",
        str(manifest),
        "--public-key",
        str(key.public),
        "--expected-channel",
        expected_channel,
        "--artifact-base",
        artifact_base,
        check=False,
    )


def test_verify_accepts_a_signed_manifest_and_rejects_tampering(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel-bytes")
    manifest = _build_manifest(tmp_path, test_key, wheel)

    verified = _verify_manifest(manifest, test_key)
    assert verified.returncode == 0, verified.stderr

    # Tampered field: signature no longer covers the payload.
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = "9.9.9"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    refused = _verify_manifest(tampered, test_key)
    assert refused.returncode == 1

    # Missing signature: fail closed before any crypto.
    data = json.loads(manifest.read_text(encoding="utf-8"))
    del data["signature"]
    unsigned = tmp_path / "unsigned.json"
    unsigned.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    refused = _verify_manifest(unsigned, test_key)
    assert refused.returncode == 1
    assert "signature" in refused.stderr


def test_verify_refuses_a_valid_manifest_for_another_channel(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wrong-channel-wheel")
    manifest = _build_manifest(tmp_path, test_key, wheel, channel="nightly")

    refused = _verify_manifest(manifest, test_key, expected_channel=CHANNEL)

    assert refused.returncode == 1
    assert "channel does not match" in refused.stderr


def test_verify_refuses_a_valid_manifest_from_another_artifact_host(
    tmp_path: Path, test_key: SigningKey
) -> None:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wrong-host-wheel")
    manifest = _build_manifest(
        tmp_path, test_key, wheel, artifact_base="https://attacker.invalid"
    )

    refused = _verify_manifest(manifest, test_key)

    assert refused.returncode == 1
    assert "wheel URL does not match" in refused.stderr


def test_installer_publish_refuses_an_unconfigured_trust_root() -> None:
    """Merging the strict cli.sh must not brick the live curl one-liner.

    publish-installer.yml path-triggers on cli.sh pushes to main. Until KMS
    provisioning pins a real fingerprint, cli.sh is deliberately fail-closed
    (CLI_MANIFEST_KEY_ID=UNCONFIGURED refuses every install), so the publish
    lane must refuse to replace the live installer with it. This pins the
    guard's presence, its refusal semantics, and its position before any
    credentialed/upload step.
    """
    installer_workflow = ROOT / ".github" / "workflows" / "publish-installer.yml"
    workflow = yaml.safe_load(installer_workflow.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-installer"]["steps"]
    names = [step.get("name") for step in steps]

    guard = next(
        step
        for step in steps
        if step.get("name") == "Refuse to publish an unconfigured trust root"
    )
    assert "CLI_MANIFEST_KEY_ID" in guard["run"]
    assert "UNCONFIGURED" in guard["run"]
    assert "exit 1" in guard["run"]
    # A non-placeholder key id is not enough: it must be the SPKI-DER SHA-256
    # fingerprint of the exact public key embedded in cli.sh, or the published
    # installer rejects every feed before installation.
    assert "CLI_MANIFEST_PUBLIC_KEY_B64" in guard["run"]
    assert "base64 -d" in guard["run"]
    assert "openssl pkey -pubin" in guard["run"]
    assert "-outform DER" in guard["run"]
    assert "sha256sum" in guard["run"]
    assert 'if [ "$key_id" != "$derived_key_id" ]' in guard["run"]
    feed_gate = next(
        step
        for step in steps
        if step.get("name") == "Verify every live channel feed against the pinned key"
    )
    # A pinned key alone is not sufficient: every LIVE feed must verify with
    # the exact checks cli.sh performs, or publishing the strict installer
    # bricks that channel. Absent feeds (404) are skip-with-warning: they are
    # not installable by the current installer either.
    assert "cli-manifest.py verify" in feed_gate["run"]
    assert '--expected-channel "$channel"' in feed_gate["run"]
    assert '--artifact-base "${CDN_BASE}"' in feed_gate["run"]
    for channel in ("nightly", "insider", "stable"):
        assert channel in feed_gate["run"]
    assert "exit 1" in feed_gate["run"]
    upload_indexes = [
        index
        for index, step in enumerate(steps)
        if "aws" in str(step.get("run", "")) or "credentials" in str(step.get("uses", ""))
    ]
    assert upload_indexes, "publish lane must still publish"
    assert names.index(guard["name"]) < names.index(feed_gate["name"]) < min(upload_indexes)


def test_publish_workflow_writes_immutable_manifest_before_signed_feed() -> None:
    run = _workflow_step("Publish wheel and signed channel manifest")["run"]
    immutable = 'put_immutable "${PREFIX}/cli-manifest.json" "$MANIFEST_PATH"'
    feed = 'aws s3 cp "$MANIFEST_PATH" "s3://${BUCKET}/feed/${CHANNEL}/latest-cli.json"'

    assert immutable in run
    assert feed in run
    assert run.index(immutable) < run.index(feed)
    assert "cat > /tmp/latest-cli.json" not in run
    assert "--cache-control no-cache" in run


def test_installer_authenticates_before_reading_or_downloading_artifact() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    verify = executable.index("openssl dgst -sha256 -verify")
    parse_fields = executable.index("read_field()")
    wheel_download = executable.index("curl -fsS --proto '=https' \"$WHEEL_URL\"")
    assert verify < parse_fields < wheel_download
    assert "SHA256SUMS" not in executable, "strict installer must have no unsigned fallback"


def test_publish_workflow_gates_all_artifact_work_before_side_effects() -> None:
    steps = _workflow_steps()
    assert steps[0]["name"] == "Refuse partial CLI signing configuration"
    complete_config = "env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY"
    for name in (
        "Checkout manifest verifier contract",
        "Download wheel artifact",
        "Locate wheel and compute checksum",
    ):
        assert _workflow_step(name)["if"] == complete_config

    promote = (
        "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && inputs.promote }}"
    )
    fresh_build = (
        "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && !inputs.promote }}"
    )
    assert _workflow_step("Verify immutable promotion bundle")["if"] == promote
    assert _workflow_step("Attest wheel provenance")["if"] == fresh_build
    assert _workflow_step("Verify promoted wheel provenance")["if"] == promote


def test_installer_fetches_authenticated_urls_without_redirects() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    fetches = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("curl ") and '"$' in line
    ]

    assert len(fetches) == 2
    assert all("--proto '=https'" in line for line in fetches)
    assert all(re.search(r"(?:^|\s)-[^\s]*L", line) is None for line in fetches)
    manifest_fetch = next(line for line in fetches if '"$MANIFEST_URL"' in line)
    assert "--max-filesize 65536" in manifest_fetch

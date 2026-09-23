"""Tests for the feature-videos publishing tool (``scripts/feature-videos``).

The tool signs a release folder for the CDN. What matters is that what it
produces is what the runtime consumes: the runtime's own verifier accepts the
signature, its parser keeps every entry, and every limit the tool enforces is
the runtime's number. Signing goes through the CLI manifest signer's KMS flow,
with the AWS CLI replaced at that signer's runner seam by a fake that answers
``get-public-key`` and ``sign`` exactly as KMS would for a throwaway key.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import feature_video_fixture as fixture
import pytest

from kiro_crew import feature_videos_manifest as fvm
from kiro_crew.platform import feed_trust
from kiro_crew.tips_allowlist import TIP_DOC_ALLOWLIST

ROOT = Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "scripts" / "feature-videos"
PLACEHOLDER_CLIP = ROOT / "website" / "capture" / "assets" / "placeholder.mp4"
PLACEHOLDER_POSTER = ROOT / "website" / "capture" / "assets" / "placeholder.jpg"

#: A doc that is really in the tips allowlist.
ALLOWED_DOC = "monitor-loops.md"
KMS_ARN = "arn:aws:kms:us-west-2:000000000000:key/fixture"


def _load(name: str) -> Any:
    """Import one of the tool's modules by path (it is a script, not a package)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest_mod = _load("_manifest")
publish_mod = _load("publish")
verify_mod = _load("verify")
ManifestError = manifest_mod.ManifestError

CONSUMER_SOURCE = ROOT / "src" / "kiro_crew" / "feature_videos_manifest.py"
CACHE_SOURCE = ROOT / "src" / "kiro_crew" / "feature_videos_cache.py"


def _number_literal(node: ast.expr) -> int | float | None:
    """An int constant or a product/sum of them, e.g. ``64 * 1024``; else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left = _number_literal(node.left)
        right = _number_literal(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        return left + right if isinstance(node.op, ast.Add) else left - right
    return None


def _module_assignments(source: Path) -> dict[str, ast.expr]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out[target.id] = node.value
    return out


def _consumer_limits() -> dict[str, int | float]:
    """The runtime's module-level numeric limits, read from its source."""
    limits: dict[str, int | float] = {}
    for source in (CONSUMER_SOURCE, CACHE_SOURCE):
        for name, value in _module_assignments(source).items():
            literal = _number_literal(value)
            if literal is not None:
                limits[name] = literal
    return limits


@pytest.fixture(scope="module")
def key_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway RSA-3072 pair standing in for the release key. Module-scoped."""
    return fixture.mint_throwaway_key(tmp_path_factory.mktemp("feature-videos-key"))


@pytest.fixture(scope="module")
def second_key(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A second pair, for a signature or a KMS answer from the wrong key."""
    return fixture.mint_throwaway_key(tmp_path_factory.mktemp("feature-videos-second-key"))


def _entry(**overrides: Any) -> dict[str, Any]:
    entry = {
        "id": "monitor-loops",
        "feature": "monitor-loops",
        "title": "Let one session watch a pull request",
        "description": "A monitor loop re-injects your check instructions on an interval.",
        "doc": ALLOWED_DOC,
        "used_when": ["sel_event_seen:monitor_start"],
        "min_version": "",
        "duration_s": 22.0,
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def release_dir(tmp_path: Path) -> Path:
    """A release folder holding one real clip and its poster, ready to be signed."""
    folder = tmp_path / "release"
    folder.mkdir()
    shutil.copyfile(PLACEHOLDER_CLIP, folder / "monitor-loops.mp4")
    shutil.copyfile(PLACEHOLDER_POSTER, folder / "monitor-loops.jpg")
    return folder


@pytest.fixture()
def catalog(tmp_path: Path) -> Path:
    """The catalog describing that folder, kept outside it."""
    path = tmp_path / "catalog.json"
    _write_catalog(path, _entry())
    return path


def _write_catalog(path: Path, *entries: dict[str, Any]) -> None:
    path.write_text(json.dumps({"entries": list(entries)}, indent=2) + "\n", encoding="utf-8")


class _FakeKms:
    """Stands in for the AWS CLI at the signer's ``_run_aws_json`` seam.

    Answers ``kms get-public-key`` with *public*'s DER and ``kms sign`` with a
    real PKCS#1 v1.5 signature over the digest it is handed, made with
    *private* -- the same bytes KMS would return for that key.
    """

    def __init__(self, private: Path, public: Path, scratch: Path) -> None:
        self.private = private
        self.public = public
        self.scratch = scratch
        self.calls: list[str] = []

    def __call__(self, args: list[str]) -> dict[str, Any]:
        assert args[0] == "kms", args
        self.calls.append(args[1])
        openssl = fixture.openssl_or_skip()
        if args[1] == "get-public-key":
            der = subprocess.run(
                [openssl, "pkey", "-pubin", "-in", str(self.public), "-outform", "DER"],
                check=True,
                cwd=self.scratch,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout
            return {
                "KeyUsage": "SIGN_VERIFY",
                "KeySpec": "RSA_3072",
                "SigningAlgorithms": ["RSASSA_PKCS1_V1_5_SHA_256"],
                "PublicKey": base64.b64encode(der).decode("ascii"),
            }
        assert args[1] == "sign", args
        digest = base64.b64decode(args[args.index("--message") + 1], validate=True)
        digest_path = self.scratch / "digest.bin"
        digest_path.write_bytes(digest)
        signature = subprocess.run(
            [openssl, "pkeyutl", "-sign", "-inkey", str(self.private), "-in", str(digest_path)]
            + ["-pkeyopt", "digest:sha256"],
            check=True,
            cwd=self.scratch,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout
        return {"Signature": base64.b64encode(signature).decode("ascii")}


@pytest.fixture()
def kms(key_pair: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeKms:
    """The throwaway key pinned as the committed key, and KMS answering for it."""
    private, public = key_pair
    monkeypatch.setattr(publish_mod, "PUBLIC_KEY_PATH", public)
    fake = _FakeKms(private, public, tmp_path)
    monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", fake)
    return fake


def _publish(release_dir: Path, catalog: Path, *extra: str, release: str = "0.7.0") -> Path:
    exit_code = publish_mod.main(
        [
            "--catalog",
            str(catalog),
            "--release-dir",
            str(release_dir),
            "--cdn-host",
            "videos.example.com",
            "--release",
            release,
            "--kms-key-arn",
            KMS_ARN,
            *extra,
        ]
    )
    assert exit_code == 0
    return release_dir


def _read(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


def _rewrite(folder: Path, manifest: dict[str, Any]) -> None:
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sign_bytes(payload: bytes, private_key: Path, tmp_path: Path) -> str:
    path = tmp_path / "payload-to-sign.json"
    path.write_bytes(payload)
    signature = subprocess.run(
        [fixture.openssl_or_skip(), "dgst", "-sha256", "-sign", str(private_key), str(path)],
        check=True,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return base64.b64encode(signature).decode("ascii")


class TestProducedFolder:
    def test_the_folder_gains_only_a_manifest_and_the_media_is_untouched(
        self, release_dir: Path, catalog: Path, kms: _FakeKms
    ) -> None:
        before = {p.name: p.read_bytes() for p in release_dir.iterdir()}
        _publish(release_dir, catalog)
        after = {p.name: p.read_bytes() for p in release_dir.iterdir()}
        assert set(after) == set(before) | {"manifest.json"}
        for name, data in before.items():
            assert after[name] == data
        assert kms.calls == ["get-public-key", "sign"]

    def test_the_manifest_has_the_contract_shape(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, key_pair: tuple[Path, Path]
    ) -> None:
        manifest = _read(_publish(release_dir, catalog))
        assert set(manifest) == set(manifest_mod.REQUIRED_FIELDS) | {"signature"}
        assert manifest["schema"] == manifest_mod.SCHEMA
        assert manifest["release"] == "0.7.0"
        assert manifest["cdn_base"] == "https://videos.example.com/feature-videos/"
        assert manifest["key_id"] == manifest_mod.public_key_id(key_pair[1])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["generated_at"])
        (entry,) = manifest["entries"]
        assert entry["file"] == "monitor-loops.mp4" and entry["poster"] == "monitor-loops.jpg"
        assert entry["bytes"] == PLACEHOLDER_CLIP.stat().st_size
        assert entry["duration_s"] == 22.0
        assert verify_mod.verify_folder(release_dir, public_key=key_pair[1])["entries"] == 1

    def test_the_runtime_verifier_accepts_the_signature_and_refuses_one_changed_byte(
        self,
        release_dir: Path,
        catalog: Path,
        kms: _FakeKms,
        key_pair: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The only proof the canonical bytes agree with the consumer is its verdict."""
        manifest = _read(_publish(release_dir, catalog))
        fixture.pin_fixture_key(monkeypatch, key_pair[1])
        cap = manifest_mod.RUNTIME_LIMITS["max_payload_bytes"]
        assert feed_trust.verify_document_signature(manifest, max_payload_bytes=cap) is True
        manifest["entries"][0]["title"] = "A title nobody signed"
        assert feed_trust.verify_document_signature(manifest, max_payload_bytes=cap) is False

    def test_the_runtime_parser_keeps_the_entry_and_derives_the_uploaded_url(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The URL a dashboard fetches must be the object the upload plan puts there."""
        _publish(release_dir, catalog)
        out = capsys.readouterr().out
        prefix = "feature-videos/0.7.0/"
        assert f"s3://<BUCKET>/{prefix}" in out
        parsed = fvm.parse_manifest(_read(release_dir))
        assert parsed is not None and len(parsed.entries) == 1
        entry = parsed.entries[0]
        assert entry.bytes == PLACEHOLDER_CLIP.stat().st_size
        assert parsed.asset_url(entry.file) == f"https://videos.example.com/{prefix}{entry.file}"
        assert parsed.asset_url(entry.poster) == (
            f"https://videos.example.com/{prefix}{entry.poster}"
        )

    def test_the_upload_plan_is_printed_not_run_and_quoted_for_the_shell(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = tmp_path / "my release" / "0.7.0"
        publish_mod._print_upload_plan(folder, "0.7.0")
        out = capsys.readouterr().out
        quoted = shlex.quote(str(folder))
        assert quoted != str(folder)
        assert f"verify.py {quoted}" in out
        assert f"aws s3 sync --dryrun {quoted}/ s3://<BUCKET>/feature-videos/0.7.0/" in out
        assert f"aws s3 sync {quoted}/ s3://<BUCKET>/feature-videos/0.7.0/" in out
        assert "aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID>" in out
        assert "'/feature-videos/0.7.0/*'" in out
        assert f"sync {folder}/" not in out


class TestRulesMatchTheRuntime:
    def test_the_runtime_limits_are_the_consumer_s(self) -> None:
        """Every ceiling this tool enforces is the runtime's own number, read from its source."""
        limits = _consumer_limits()
        pairs = (
            ("max_payload_bytes", "_SIGNED_PAYLOAD_MAX_BYTES"),
            ("max_document_bytes", "_MANIFEST_MAX_BYTES"),
            ("max_entries", "_MAX_ENTRIES"),
            ("max_clip_bytes", "_MAX_ENTRY_BYTES"),
            ("max_poster_bytes", "MAX_POSTER_BYTES"),
            ("max_duration_s", "_MAX_DURATION_S"),
        )
        assert set(manifest_mod.RUNTIME_LIMITS) == {flag for flag, _ in pairs}
        for flag, constant in pairs:
            assert manifest_mod.RUNTIME_LIMITS[flag] == limits.get(constant), (flag, constant)

    def test_the_signing_plumbing_is_the_cli_signer_s(self) -> None:
        """One signer for the CLI feed and for videos: identity, not a copy."""
        signer = manifest_mod._signer
        assert signer.__file__ == str(ROOT / "packaging" / "signing" / "cli-manifest.py")
        assert manifest_mod.ManifestError is signer.ManifestError
        assert manifest_mod.public_key_id is signer.public_key_id
        assert manifest_mod.public_key_der is signer.public_key_der
        assert manifest_mod.run_openssl is signer.run_openssl
        assert manifest_mod.kms_sign_digest is signer.kms_sign_digest
        assert manifest_mod.canonical_bytes({"b": 1, "a": [{"y": 1, "x": 2}]}) == (
            signer.canonical_json({"a": [{"x": 2, "y": 1}], "b": 1})
        )

    def test_the_longest_id_still_fits_the_runtime_basename_bound(self) -> None:
        longest = "a" * manifest_mod.MAX_ID_CHARS
        assert manifest_mod.validate_slug(longest, where="test") == longest
        assert len(f"{longest}.mp4") == manifest_mod.RUNTIME_MAX_BASENAME_CHARS
        with pytest.raises(ManifestError, match="longer than"):
            manifest_mod.validate_slug(longest + "a", where="test")

    def test_a_signable_release_is_exactly_the_folder_name_the_runtime_asks_for(self) -> None:
        """The rule is the runtime's round trip: ``running_release`` must give the name back."""
        corpus = [
            "0.7.0", "0.0.0", "1.0.0", "10.20.30", "99999.99999.99999",
            "00.7.0", "0.07.0", "0.7.00", "007.7.7", "0.7.0rc1", "0.7.0-rc1",
            "0.7.0+build.1", "1", "1.2", "1.2.3.4", "123456.0.0", "0..1", "",
        ]  # fmt: skip
        runtime_re_node = _module_assignments(CONSUMER_SOURCE)["_RELEASE_RE"]
        assert isinstance(runtime_re_node, ast.Call) and runtime_re_node.args
        pattern = runtime_re_node.args[0]
        assert isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
        runtime_re = re.compile(pattern.value)
        for candidate in corpus:
            # Signable iff the runtime's own parser gives the name back unchanged
            # AND its manifest grammar admits it.
            round_trip = fvm.running_release(candidate) == candidate and bool(
                runtime_re.match(candidate)
            )
            if round_trip:
                assert manifest_mod.validate_release(candidate) == candidate
            else:
                with pytest.raises(ManifestError, match="three-component"):
                    manifest_mod.validate_release(candidate)
        for folder in fvm.release_candidates("0.7.3"):
            assert manifest_mod.validate_release(folder) == folder


class TestSigning:
    def test_a_kms_key_that_is_not_the_committed_one_is_refused_before_signing(
        self,
        release_dir: Path,
        catalog: Path,
        key_pair: tuple[Path, Path],
        second_key: tuple[Path, Path],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A mistyped ARN must not produce a folder every dashboard refuses."""
        monkeypatch.setattr(publish_mod, "PUBLIC_KEY_PATH", key_pair[1])
        fake = _FakeKms(second_key[0], second_key[1], tmp_path)
        monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", fake)
        with pytest.raises(ManifestError, match="does not match the committed public key"):
            _publish(release_dir, catalog)
        assert fake.calls == ["get-public-key"]
        assert not (release_dir / "manifest.json").exists()

    def test_a_signature_kms_returns_that_does_not_verify_is_refused(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The signature is checked against the committed key before anything is written."""
        real_sign = kms.__call__

        def bad_sign(args: list[str]) -> dict[str, Any]:
            response = real_sign(args)
            if args[1] == "sign":
                response["Signature"] = base64.b64encode(b"\x00" * 384).decode("ascii")
            return response

        monkeypatch.setattr(manifest_mod._signer, "_run_aws_json", bad_sign)
        with pytest.raises(ManifestError, match="does not verify"):
            _publish(release_dir, catalog)
        assert not (release_dir / "manifest.json").exists()

    def test_a_payload_over_the_runtime_s_cap_is_refused_before_signing(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_payload_bytes", 64)
        with pytest.raises(ManifestError, match="signed payload is .* over the runtime's 64"):
            _publish(release_dir, catalog)
        assert kms.calls == []

    def test_a_write_that_fails_part_way_leaves_no_half_release(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_open = open

        class _DiskFull:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def __enter__(self) -> "_DiskFull":
                return self

            def __exit__(self, *exc: Any) -> None:
                self._inner.close()

            def write(self, _data: bytes) -> int:
                raise OSError(28, "No space left on device")

        def failing_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(path, mode, *args, **kwargs)
            return _DiskFull(handle) if mode == "xb" else handle

        monkeypatch.setattr(publish_mod, "open", failing_open, raising=False)
        with pytest.raises(OSError):
            _publish(release_dir, catalog)
        assert not (release_dir / "manifest.json").exists()


class TestRefusals:
    """Each refusal names its reason; nothing is signed or written."""

    def _refused(self, release_dir: Path, catalog: Path, match: str) -> None:
        with pytest.raises(ManifestError, match=match):
            _publish(release_dir, catalog)
        assert not (release_dir / "manifest.json").exists()

    def test_catalog_entry_rules(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, tmp_path: Path
    ) -> None:
        cases = [
            (_entry(id="Monitor Loops"), "not a lowercase hyphenated slug"),
            (_entry(doc="internal-design-note.md"), "not in the tips doc allowlist"),
            (_entry(extra="field"), "unknown field"),
            (_entry(title=""), "must be non-empty text"),
            (_entry(title="line\nbreak"), "control characters"),
            (_entry(used_when=["", "x"]), "used_when must be a list"),
            (_entry(min_version="0.7.0-rc1"), "bare release"),
            (_entry(duration_s=float("inf")), "must be finite"),
            (_entry(duration_s=0.0004), "must be positive"),
            (_entry(duration_s=10**400), "out of range"),
            (_entry(duration_s=3600.001), "over the runtime's 3600 second ceiling"),
            (_entry(duration_s="22"), "must be a number"),
        ]
        for entry, match in cases:
            _write_catalog(catalog, entry)
            self._refused(release_dir, catalog, match)
        entry = _entry()
        del entry["duration_s"]
        _write_catalog(catalog, entry)
        self._refused(release_dir, catalog, "duration_s is required")
        _write_catalog(catalog, _entry(), _entry())
        self._refused(release_dir, catalog, "duplicate id")
        catalog.write_text('{"entries": [], "entries": []}', encoding="utf-8")
        self._refused(release_dir, catalog, "duplicate JSON key")
        catalog.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        self._refused(release_dir, catalog, "nested too deeply")
        catalog.write_bytes(b"x" * (publish_mod._MAX_CATALOG_BYTES + 1))
        self._refused(release_dir, catalog, "larger than")
        assert kms.calls == []

    def test_too_many_entries(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_entries", 1)
        _write_catalog(catalog, _entry(), _entry(id="second"))
        self._refused(release_dir, catalog, "over the runtime's 1 entry limit")

    def test_release_folder_rules(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, tmp_path: Path
    ) -> None:
        (release_dir / "notes.txt").write_text("x", encoding="utf-8")
        self._refused(release_dir, catalog, "file\\(s\\) the catalog does not name: notes.txt")
        (release_dir / "notes.txt").unlink()

        (release_dir / "monitor-loops.jpg").unlink()
        self._refused(release_dir, catalog, "is missing: monitor-loops.jpg")
        shutil.copyfile(PLACEHOLDER_POSTER, release_dir / "monitor-loops.jpg")

        (release_dir / "monitor-loops.mp4").unlink()
        (release_dir / "monitor-loops.mp4").symlink_to(PLACEHOLDER_CLIP)
        self._refused(release_dir, catalog, "is a symlink")
        (release_dir / "monitor-loops.mp4").unlink()
        (release_dir / "monitor-loops.mp4").write_bytes(b"")
        self._refused(release_dir, catalog, "monitor-loops.mp4 is empty")
        shutil.copyfile(PLACEHOLDER_CLIP, release_dir / "monitor-loops.mp4")

        link = tmp_path / "release-link"
        link.symlink_to(release_dir, target_is_directory=True)
        self._refused(link, catalog, "not a directory")
        self._refused(tmp_path / "absent", catalog, "not a directory")

        _publish(release_dir, catalog)
        with pytest.raises(ManifestError, match="already holds manifest.json"):
            _publish(release_dir, catalog)

    def test_media_over_the_runtime_s_caps(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_clip_bytes", 16)
        self._refused(
            release_dir, catalog, "monitor-loops.mp4 is .* over the runtime's 16 byte clip"
        )
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_clip_bytes", 64 * 1024 * 1024)
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_poster_bytes", 16)
        self._refused(
            release_dir, catalog, "monitor-loops.jpg is .* over the runtime's 16 byte poster"
        )

    def test_a_manifest_over_the_runtime_s_document_cap(
        self, release_dir: Path, catalog: Path, kms: _FakeKms, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(manifest_mod.RUNTIME_LIMITS, "max_document_bytes", 64)
        self._refused(release_dir, catalog, "manifest.json is .* over the runtime's 64")

    @pytest.mark.parametrize(
        "bad",
        [
            "videos.example.com/archive",
            "videos.example.com/",
            "videos.example.com?x=1",
            "user:secret@videos.example.com",
            "",
            "videos.example.com:abc",
            "videos.example.com:0",
            "[videos.example.com",
            "videos example.com",
            "videos_example.com",
            "-videos.example.com",
            "a" * 64 + ".example.com",
        ],
    )
    def test_a_cdn_host_that_is_not_a_bare_host_is_refused(self, bad: str) -> None:
        with pytest.raises(ManifestError, match="--cdn-host"):
            manifest_mod.validate_cdn_host(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "videos.example.com",
            "videos.example.com:8443",
            "localhost",
            "192.0.2.10",
            "[2001:db8::10]:8443",
        ],
    )
    def test_a_bare_host_is_kept_as_given(self, good: str) -> None:
        assert manifest_mod.validate_cdn_host(good) == good

    def test_a_release_that_is_not_major_minor_patch_is_refused_before_anything_is_read(
        self, release_dir: Path, catalog: Path, kms: _FakeKms
    ) -> None:
        with pytest.raises(ManifestError, match="three-component"):
            _publish(release_dir, catalog, release="1.2")
        assert kms.calls == []

    def test_cdn_base_rules(self) -> None:
        for bad, match in [
            ("http://videos.example.com/feature-videos/", "must be an https URL"),
            ("https://videos.example.com/feature-videos", "must end with a slash"),
            ("https://videos.example.com/feature-videos/?x=1", "no query string"),
            ("https://u:p@videos.example.com/feature-videos/", "no credentials"),
            ("https://videos.example.com//feature-videos/", "empty segment"),
            ("https://videos.example.com:abc/feature-videos/", "not a well-formed URL"),
            ("https://videos.example.com:0/feature-videos/", "port 0"),
            ("https://[videos.example.com/feature-videos/", "not a well-formed URL"),
        ]:
            with pytest.raises(ManifestError, match=match):
                manifest_mod.validate_cdn_base(bad)
        good = "https://videos.example.com:8443/feature-videos/"
        assert manifest_mod.validate_cdn_base(good) == good


class TestVerifyDetectsTampering:
    """``verify.py`` is the pre-upload check; every way a folder can differ from
    what was signed must be named."""

    def test_a_clean_folder_verifies_from_the_command_line(
        self,
        release_dir: Path,
        catalog: Path,
        kms: _FakeKms,
        key_pair: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _publish(release_dir, catalog)
        capsys.readouterr()
        assert verify_mod.main([str(release_dir), "--public-key", str(key_pair[1])]) == 0
        out = capsys.readouterr().out
        assert "release    0.7.0" in out
        assert f"signed by  {manifest_mod.public_key_id(key_pair[1])}" in out

    def test_every_kind_of_tampering_is_named(
        self,
        release_dir: Path,
        catalog: Path,
        kms: _FakeKms,
        key_pair: tuple[Path, Path],
        second_key: tuple[Path, Path],
        tmp_path: Path,
    ) -> None:
        public = key_pair[1]
        _publish(release_dir, catalog)
        good = _read(release_dir)
        clip = release_dir / "monitor-loops.mp4"
        clip_bytes = clip.read_bytes()

        def verify() -> Any:
            return verify_mod.verify_folder(release_dir, public_key=public)

        def expect(match: str) -> None:
            with pytest.raises(ManifestError, match=match):
                verify()

        # Media changed under a valid signature.
        clip.write_bytes(clip_bytes + b"\x00")
        expect("bytes hash to")
        clip.write_bytes(clip_bytes)

        # Manifest edited: the signature covers other bytes than these.
        edited = json.loads(json.dumps(good))
        edited["entries"][0]["title"] = "A title nobody signed"
        _rewrite(release_dir, edited)
        expect("does not verify")

        # Signed by another key, key_id naming that key, or the committed one.
        forged = json.loads(json.dumps(good))
        payload = manifest_mod.check_signable(manifest_mod.signed_payload(forged))
        forged["signature"] = _sign_bytes(payload, second_key[0], tmp_path)
        _rewrite(release_dir, forged)
        expect("does not verify")
        forged["key_id"] = manifest_mod.public_key_id(second_key[1])
        _rewrite(release_dir, forged)
        expect("names key_id")

        stripped = {k: v for k, v in good.items() if k != "signature"}
        _rewrite(release_dir, stripped)
        expect("missing its signature")

        added = {**good, "note": "hello"}
        _rewrite(release_dir, added)
        expect("unknown field")

        dropped = {k: v for k, v in good.items() if k != "generated_at"}
        _rewrite(release_dir, dropped)
        expect("missing field")

        wrong_schema = {**good, "schema": "other"}
        _rewrite(release_dir, wrong_schema)
        expect("unsupported schema")

        _rewrite(release_dir, good)
        assert verify()["entries"] == 1

        # Files the signature never covered, at the top and nested, and a link.
        (release_dir / "extra.js").write_text("x", encoding="utf-8")
        expect("unsigned path.*extra.js")
        (release_dir / "extra.js").unlink()
        (release_dir / "evil").mkdir()
        (release_dir / "evil" / "payload.js").write_text("x", encoding="utf-8")
        expect("unsigned path.*evil/")
        shutil.rmtree(release_dir / "evil")
        clip.unlink()
        clip.symlink_to(tmp_path / "somewhere-else.mp4")
        (tmp_path / "somewhere-else.mp4").write_bytes(clip_bytes)
        expect("is a symlink")
        clip.unlink()
        clip.write_bytes(clip_bytes)

        # A different release folder name than the manifest signs is caught by
        # the runtime's URL derivation, not here; but a cdn_base carrying the
        # release is.
        doubled = json.loads(json.dumps(good))
        doubled["cdn_base"] = "https://videos.example.com/feature-videos/0.7.0/"
        payload = manifest_mod.check_signable(manifest_mod.signed_payload(doubled))
        doubled["signature"] = _sign_bytes(payload, key_pair[0], tmp_path)
        _rewrite(release_dir, doubled)
        expect("does not end in /feature-videos/")

        assert TIP_DOC_ALLOWLIST  # the allowlist the publisher imported is the runtime's

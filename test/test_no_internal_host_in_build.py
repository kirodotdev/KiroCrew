"""The public build ships no internal host.

`scripts/verify_no_internal_host.py` scans the BUILT artifacts (the wheel in
`dist/` and the website `dist/`), not the source tree. `build.yml` (every PR)
and `build-wheel.yml` (release/nightly) run it against the real artifacts.

The script keeps its denylist as sha256 digests only, so these tests name no
internal host either: each planted fixture uses a public dummy host under the
reserved `.test` domain and injects that host's digest into the scan. A clean
build passes; a planted host, name or internal ks bundle fails it, and the
report names the file, line and digest, never the token.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kiro_crew.testing.wait import default_timeout

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "verify_no_internal_host.py"
_WHEEL = "kirocrew-0.0.0-py3-none-any.whl"

#: Public dummies standing in for the real (hashed) denylist.
_DOMAIN = "planted.example.test"
_NAME = "PlantedPackageName"
_MARKER = "plantedbuild"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def script() -> dict:
    """The script's live module namespace, loaded fresh for each test.

    Live (not ``runpy``'s copy), so a test that swaps ``EXCLUSIONS`` or
    ``HOST_DIGESTS`` changes what the script's functions read.
    """
    spec = importlib.util.spec_from_file_location("verify_no_internal_host", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def _scan(script: dict, dist: Path, web: Path) -> list[str]:
    return script["scan"](
        dist,
        web,
        hosts=frozenset({_sha(_DOMAIN)}),
        names=frozenset({_sha(_NAME)}),
        markers=frozenset({_sha(_MARKER)}),
    )


def _build(tmp_path: Path, wheel_files: dict, web_files: dict) -> tuple[Path, Path]:
    """Write a fake wheel and website dist, return (dist, website dist)."""
    dist = tmp_path / "dist"
    web = tmp_path / "website-dist"
    dist.mkdir()
    web.mkdir()
    with zipfile.ZipFile(dist / _WHEEL, "w") as whl:
        whl.writestr("kiro_crew/__init__.py", "VERSION = '0.0.0'\n")
        for name, body in wheel_files.items():
            whl.writestr(name, body)
    (web / "index.html").write_text("<html>https://kiro.dev</html>", encoding="utf-8")
    for name, body in web_files.items():
        path = web / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return dist, web


def _run(dist: Path, web: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(dist), str(web)],
        capture_output=True,
        encoding="utf-8",
        timeout=default_timeout(),
        check=False,
    )


def test_both_build_lanes_run_the_scan() -> None:
    """Every PR and every published wheel are scanned by the same script."""
    assert _SCRIPT.is_file()
    for lane in ("build.yml", "build-wheel.yml"):
        text = (_REPO_ROOT / ".github" / "workflows" / lane).read_text(encoding="utf-8")
        assert "scripts/verify_no_internal_host.py" in text, f"{lane} skips the host scan"


def test_the_denylist_is_digests_only(script: dict) -> None:
    for key, count in (("HOST_DIGESTS", 7), ("NAME_DIGESTS", 1), ("BUILD_MARKER_DIGESTS", 3)):
        digests = script[key]
        assert len(digests) == count, key
        assert all(len(d) == 64 and set(d) <= set("0123456789abcdef") for d in digests), key


def test_a_clean_build_passes(tmp_path: Path) -> None:
    dist, web = _build(
        tmp_path,
        {"kiro_crew/config.py": "URL = 'https://kiro.dev'\n"},
        {"assets/index-abc.js": "fetch('https://api.github.com')"},
    )
    result = _run(dist, web)
    assert result.returncode == 0, result.stderr
    assert "no internal host" in result.stdout


@pytest.mark.parametrize(
    "planted",
    [
        f"https://{_DOMAIN}/x",
        f"https://deep.sub.{_DOMAIN}/x",
        f"HTTPS://{_DOMAIN.upper()}:8443/",
        f"uvx {_NAME}",
        f"see {_DOMAIN}.",
        f"-{_DOMAIN}",
        "re.compile(r'[\\w.-]+\\." + _DOMAIN.replace(".", "\\.") + "')",
    ],
)
@pytest.mark.parametrize("where", ["wheel", "website"])
def test_a_planted_host_fails_with_file_line_and_digest(
    tmp_path: Path, script: dict, planted: str, where: str
) -> None:
    """The fixture that proves the scan fails when it should."""
    body = f"// header\nconst u = '{planted}'\n"
    if where == "wheel":
        dist, web = _build(tmp_path, {"kiro_crew/planted.js": body}, {})
        expected = f"{_WHEEL}!kiro_crew/planted.js:2: matches "
    else:
        dist, web = _build(tmp_path, {}, {"assets/planted-123.js": body})
        expected = "website dist: assets/planted-123.js:2: matches "
    hits = _scan(script, dist, web)
    assert len(hits) == 1, hits
    assert hits[0].startswith(expected), hits
    assert hits[0].endswith(_sha(_NAME) if _NAME in planted else _sha(_DOMAIN))
    assert "planted.example" not in hits[0].lower() and _NAME not in hits[0]


def test_a_lookalike_host_passes(tmp_path: Path, script: dict) -> None:
    """Only the domain and hosts under it match, not a host that merely ends alike."""
    dist, web = _build(tmp_path, {}, {"a.js": "https://notplanted.example.test.org/"})
    assert _scan(script, dist, web) == []


def test_a_host_inside_a_binary_file_is_found(tmp_path: Path, script: dict) -> None:
    dist, web = _build(tmp_path, {}, {"assets/blob.bin": b"\x00\xff" + _DOMAIN.encode() + b"\x00"})
    hits = _scan(script, dist, web)
    assert [h.split(":")[1] for h in hits] == [" assets/blob.bin"]


def test_every_exclusion_names_a_reason_and_only_known_digests(script: dict) -> None:
    """An exclusion is (path -> reason, digests it excuses); the reason names no host."""
    deny = script["_Denylist"](
        script["HOST_DIGESTS"], script["NAME_DIGESTS"], script["BUILD_MARKER_DIGESTS"]
    )
    for path, (reason, digests) in script["EXCLUSIONS"].items():
        assert "*" not in path, f"exclusion {path!r} is a glob; name the file"
        assert reason.strip(), f"exclusion {path!r} has no reason"
        assert script["_hits_in"](reason.encode(), deny) == [], "a reason names no host"
        assert digests and digests <= script["HOST_DIGESTS"] | script["NAME_DIGESTS"], path


def test_an_exclusion_excuses_only_its_own_digests_in_its_own_file(
    tmp_path: Path, script: dict
) -> None:
    other = "other.example.test"
    script["EXCLUSIONS"] = {"kiro_crew/detector.py": ("a detector", frozenset({_sha(_DOMAIN)}))}
    body = f"PATTERN = '{_DOMAIN}'\n"
    dist, web = _build(
        tmp_path,
        {
            "kiro_crew/detector.py": body + f"ALSO = '{other}'\n",
            "kiro_crew/elsewhere.py": body,
        },
        {},
    )
    hits = script["scan"](
        dist,
        web,
        hosts=frozenset({_sha(_DOMAIN), _sha(other)}),
        names=frozenset(),
        markers=frozenset(),
    )
    assert sorted(h.split(": matches ")[0] for h in hits) == [
        f"{_WHEEL}!kiro_crew/detector.py:2",
        f"{_WHEEL}!kiro_crew/elsewhere.py:1",
    ]


def test_main_exits_1_on_a_hit(tmp_path: Path, script: dict) -> None:
    """The exit code is the CI verdict; a hit must never exit 0."""
    script["HOST_DIGESTS"] = frozenset({_sha(_DOMAIN)})
    dist, web = _build(tmp_path, {"kiro_crew/planted.py": f"U = '{_DOMAIN}'\n"}, {})
    assert script["main"](["verify", str(dist), str(web)]) == 1


def _ks_bundle(source: dict | None) -> dict:
    files = {"kiro_crew/vendor/ks/ks.mjs": "export const run = () => 0\n"}
    if source is not None:
        files["kiro_crew/vendor/ks/SOURCE.json"] = json.dumps(source)
    return files


@pytest.mark.parametrize(
    "source",
    [
        {"package": "PlantedBuildCli", "version": "1.0"},
        {"repo": f"https://{_DOMAIN}/packages/KsCli", "commit": "abc"},
    ],
)
def test_a_ks_bundle_from_an_internal_build_fails(
    tmp_path: Path, script: dict, source: dict
) -> None:
    dist, web = _build(tmp_path, _ks_bundle(source), {})
    hits = _scan(script, dist, web)
    assert any("kiro_crew/vendor/ks/ks.mjs: SOURCE.json names an internal build" in h for h in hits)


def test_a_ks_bundle_without_provenance_fails(tmp_path: Path, script: dict) -> None:
    dist, web = _build(tmp_path, _ks_bundle(None), {})
    assert any("no SOURCE.json" in h for h in _scan(script, dist, web))


def test_a_ks_bundle_from_a_public_build_passes(tmp_path: Path, script: dict) -> None:
    source = {"repo": "github.com/kirodotdev/ks", "build": "public", "version": "1.0"}
    dist, web = _build(tmp_path, _ks_bundle(source), {})
    assert _scan(script, dist, web) == []


@pytest.mark.parametrize("missing", ["wheel", "website"])
def test_a_missing_artifact_is_an_error_not_a_pass(tmp_path: Path, missing: str) -> None:
    """Scanning nothing must never read as clean."""
    dist, web = _build(tmp_path, {}, {})
    if missing == "wheel":
        (dist / _WHEEL).unlink()
    else:
        for path in web.iterdir():
            path.unlink()
    result = _run(dist, web)
    assert result.returncode == 2
    assert "nothing to scan" in result.stderr

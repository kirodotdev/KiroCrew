"""Tests for ``container.supervisor.bundle.install_bundle``.

The failure this guards is the one the packaging contract was written around: a
bundle that is built, digested and handed to the task, then never read, so the
deployment serves a default agent while every gate is green. So the assertions
here are about WHERE each entry lands (the paths verified against the Kiro Crew
source) and that each refusal actually fires.

Every refusal test is a MUTATION test: it starts from a bundle the installer
accepts (proven by ``test_install_lays_the_bundle_out_where_kirocrew_reads``),
applies exactly ONE change, and asserts the matching refusal. A guard that never
fails is indistinguishable from one that cannot, so each guard is shown failing.

The digest the fixture stamps into the manifest is computed by an INDEPENDENT
reimplementation below, not by importing the installer's own ``_content_digest``:
a happy path that used the code under test to stamp what the code under test
checks would only prove the function is deterministic. This mirrors how the
source's ``verify_bundle.py`` restates the algorithm on purpose.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from container.common import ConfigError, Settings
from container.supervisor import bundle as bundle_mod


def make_settings(tmp_path: Path, *, crew_name: str = "frontdesk") -> Settings:
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    return Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home,
        crew_name=crew_name,
        backup_bucket=None,
        backup_prefix="",
        backup_interval_secs=30,
        bundle_dir=tmp_path / "crew-bundle",
    )


def _independent_digest(root: Path) -> str:
    """A second spelling of the producer's algorithm (crew_export/bundle.py:78).

    Restated rather than imported so the happy-path match is a genuine agreement
    between two implementations, exactly as verify_bundle.py does.
    """
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_bundle(tmp_path: Path, *, crew_name: str = "frontdesk") -> Path:
    """Write a four-entry bundle whose manifest digest is correct.

    Returns the bundle dir. The manifest is written LAST, after the digest is
    computed over the other files, matching the producer.
    """
    root = tmp_path / "crew-bundle"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.json").write_text(
        json.dumps({"name": crew_name, "prompt": "You are the front desk."}, indent=2),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    skills = root / "skills"
    (skills / "greet").mkdir(parents=True, exist_ok=True)
    (skills / "greet" / "SKILL.md").write_text("# Greet\nSay hello.\n", encoding="utf-8")

    digest = _independent_digest(root)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_version": 1,
                "crew_name": crew_name,
                "created_at": "2026-09-03T00:00:00+00:00",
                "digest": digest,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


# --- happy path: the bundle lands where Kiro Crew reads it ------------------


def test_install_lays_the_bundle_out_where_kirocrew_reads(tmp_path):
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "kiro" / "agents"

    payload = bundle_mod.install_bundle(settings, agents_dir=agents)

    # agent.json -> <kiro agents>/<crew>.json, byte-identical to the source.
    agent_dst = agents / "frontdesk.json"
    assert agent_dst.is_file()
    assert agent_dst.read_bytes() == (settings.bundle_dir / "agent.json").read_bytes()
    # mcp.json -> <data home>/mcp.json.
    assert (settings.data_home / "mcp.json").is_file()
    # skills/ -> <data home>/skills/ (tree preserved).
    assert (settings.data_home / "skills" / "greet" / "SKILL.md").is_file()
    # marker at the data-home root, with the three contract fields.
    marker = json.loads((settings.data_home / bundle_mod.INSTALLED_MARKER).read_text())
    assert marker["crew_name"] == "frontdesk"
    assert marker["bundle_digest"].startswith("sha256:")
    assert marker["installed_at"]
    assert payload == marker


def test_default_agents_dir_mirrors_kiro_home(tmp_path, monkeypatch):
    # Verified location: $KIRO_HOME/agents (config/paths.py:510 kiro_home), NOT
    # under the data home. With KIRO_HOME set, the agent dir follows it.
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "khome"))
    assert bundle_mod.default_kiro_agents_dir() == tmp_path / "khome" / "agents"
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "h"))
    assert bundle_mod.default_kiro_agents_dir() == tmp_path / "h" / ".kiro" / "agents"


# --- refusal 1: the bundle dir / an entry is missing (mutation: delete) -----


def test_refuses_a_missing_bundle_dir(tmp_path):
    settings = make_settings(tmp_path)  # no build_bundle -> dir absent
    with pytest.raises(ConfigError, match=r"bundle dir present"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


@pytest.mark.parametrize("entry", ["manifest.json", "agent.json", "mcp.json", "skills"])
def test_refuses_when_any_of_the_four_entries_is_missing(tmp_path, entry):
    # MUTATION: build a valid bundle, then remove exactly one required entry.
    root = build_bundle(tmp_path)
    target = root / entry
    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    settings = make_settings(tmp_path)
    with pytest.raises(ConfigError, match=r"entry present"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


# --- refusal 2: manifest crew_name != SMC_CREW_NAME -------------------------


def test_refuses_when_manifest_crew_name_disagrees_with_env(tmp_path):
    # MUTATION: the bundle names 'frontdesk' but the task is configured for
    # 'lawyer'. The image does not carry the crew this task serves.
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="lawyer")
    with pytest.raises(ConfigError, match=r"crew_name == SMC_CREW_NAME"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


def test_refuses_an_empty_crew_name(tmp_path):
    # MUTATION: SMC_CREW_NAME unset. "It started" must mean "the NAMED crew is
    # installed"; an unnamed crew cannot satisfy that.
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="")
    with pytest.raises(ConfigError, match=r"crew_name == SMC_CREW_NAME"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


# --- refusal 3: agent.json name != crew_name --------------------------------


def test_refuses_when_agent_name_disagrees_with_manifest(tmp_path):
    # MUTATION: rewrite agent.json's name AND restamp the digest, so ONLY the
    # name check can fire (a naive restamp-less edit would trip the digest guard
    # instead and prove nothing about this one).
    root = build_bundle(tmp_path, crew_name="frontdesk")
    (root / "agent.json").write_text(
        json.dumps({"name": "someone-else", "prompt": "hi"}, indent=2),
        encoding="utf-8",
    )
    man = json.loads((root / "manifest.json").read_text())
    man["digest"] = _independent_digest(root)
    (root / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    with pytest.raises(ConfigError, match=r"agent.json name == crew_name"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


# --- refusal 4: recomputed digest != manifest digest ------------------------


def test_refuses_when_content_does_not_match_the_manifest_digest(tmp_path):
    # MUTATION: change a skill file AFTER the manifest was stamped, so the
    # recomputed content digest no longer matches. Nothing else is wrong.
    root = build_bundle(tmp_path)
    (root / "skills" / "greet" / "SKILL.md").write_text(
        "# Greet\nSay hello, tampered.\n", encoding="utf-8"
    )
    settings = make_settings(tmp_path)
    with pytest.raises(ConfigError, match=r"content digest == manifest digest"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")


def test_nothing_is_installed_when_a_check_fails(tmp_path):
    # A refusal must leave the read paths untouched -- fail closed, not halfway.
    root = build_bundle(tmp_path)
    (root / "skills" / "greet" / "SKILL.md").write_text("tampered\n", encoding="utf-8")
    settings = make_settings(tmp_path)
    with pytest.raises(ConfigError):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "agents")
    assert not (settings.data_home / "mcp.json").exists()
    assert not (settings.data_home / "skills").exists()
    assert not (settings.data_home / bundle_mod.INSTALLED_MARKER).exists()

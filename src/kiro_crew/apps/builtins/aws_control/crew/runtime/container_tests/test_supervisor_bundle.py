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
import shutil
from pathlib import Path

import pytest
from container.common import ConfigError, Settings
from container.supervisor import bundle as bundle_mod


def make_settings(
    tmp_path: Path, *, crew_name: str = "frontdesk", control_secret: str | None = "s3cr3t"
) -> Settings:
    """Settings for an install test.

    ``control_secret`` defaults to a value because the install marker is authenticated
    with it, and a deployment that reaches the pruning path has one: without it the
    supervisor cannot sign the marker and a later install reads the record as untrusted.
    Tests of that case pass ``None`` explicitly.
    """
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    return Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=control_secret,
        data_home=data_home,
        config_dir=data_home,
        crew_name=crew_name,
        backup_bucket=None,
        backup_prefix="",
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


def build_bundle(
    tmp_path: Path, *, crew_name: str = "frontdesk", skills: dict[str, str] | None = None
) -> Path:
    """Write a four-entry bundle whose manifest digest is correct.

    Returns the bundle dir. The manifest is written LAST, after the digest is
    computed over the other files, matching the producer.

    ``skills`` maps a skill directory name to its ``SKILL.md`` body, so a test can
    build a SECOND bundle that drops one and drive a real upgrade. The skills tree is
    rebuilt from scratch each call for exactly that reason: a bundle that still carried
    a previous call's skill would make an upgrade test pass without upgrading anything.
    """
    root = tmp_path / "crew-bundle"
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.json").write_text(
        json.dumps({"name": crew_name, "prompt": "You are the front desk."}, indent=2),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    skills_tree = root / "skills"
    if skills_tree.exists():
        shutil.rmtree(skills_tree)
    for name, body in (skills or {"greet": "# Greet\nSay hello.\n"}).items():
        (skills_tree / name).mkdir(parents=True, exist_ok=True)
        (skills_tree / name / "SKILL.md").write_text(body, encoding="utf-8")
    skills_tree.mkdir(parents=True, exist_ok=True)

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
    # recomputed content digest does not match. Nothing else is wrong.
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


# --- pre-planted destination symlinks cannot redirect the unsandboxed install ----
#
# install_bundle runs at boot before any sandbox, as the image's own user. Under a mounted
# persistent data home (this image's Dockerfile provisions one), a sandboxed agent in a PRIOR
# task can plant a symlink at a bundle destination pointing outside the data home. A plain
# copyfile/copytree would follow it and overwrite the target. Each destination must refuse.


def _outside_target(tmp_path: Path) -> Path:
    """A file OUTSIDE the data home that a followed symlink would clobber."""
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("ORIGINAL", encoding="utf-8")
    return victim


def test_a_symlink_planted_at_mcp_dst_is_refused_not_followed(tmp_path):
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    victim = _outside_target(tmp_path)
    (settings.data_home / "mcp.json").symlink_to(victim)

    with pytest.raises(ConfigError, match="not a symlink"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")
    # The outside file was NOT overwritten through the link.
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


def test_a_symlink_planted_at_the_agent_dst_is_refused(tmp_path):
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    victim = _outside_target(tmp_path)
    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "frontdesk.json").symlink_to(victim)

    with pytest.raises(ConfigError, match="not a symlink"):
        bundle_mod.install_bundle(settings, agents_dir=agents)
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


def test_a_symlink_planted_at_skills_dst_is_refused(tmp_path):
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_home / "skills").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ConfigError, match="not a symlink"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")
    # Nothing was copied through the link into the outside directory.
    assert not (outside_dir / "greet").exists()


def test_a_bundle_upgrade_prunes_a_removed_skill(tmp_path):
    # A skill an earlier bundle installed and this one dropped must be GONE, or a
    # removed (possibly governance-relevant) skill stays active on a persistent volume.
    # Driven as two real installs, because the pruning decision reads what the previous
    # install RECORDED: fabricating a skill on disk with no marker is a different case,
    # covered by test_an_unrecorded_skill_is_left_alone.
    build_bundle(
        tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n", "obsolete": "# O\n"}
    )
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "kiro" / "agents"
    bundle_mod.install_bundle(settings, agents_dir=agents)
    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert (settings.data_home / "skills" / "greet" / "SKILL.md").is_file()
    assert not (settings.data_home / "skills" / "obsolete").exists()


def test_a_skill_the_crew_created_survives_a_reinstall(tmp_path):
    # The other half of the same rule, and the one a wholesale replace violates: the
    # install replaces the bundle's OWN files, and a skill the running crew produced is
    # not its to delete. A reinstall happens on every boot, so a wholesale prune loses
    # the crew's work on the first restart.
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "kiro" / "agents"
    bundle_mod.install_bundle(settings, agents_dir=agents)

    learned = settings.data_home / "skills" / "learned-by-doing"
    learned.mkdir(parents=True, exist_ok=True)
    (learned / "SKILL.md").write_text("# Learned\n", encoding="utf-8")

    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert (learned / "SKILL.md").read_text(encoding="utf-8") == "# Learned\n"
    assert (settings.data_home / "skills" / "greet" / "SKILL.md").is_file()


def test_a_crew_file_inside_a_dropped_skill_survives_its_pruning(tmp_path):
    # Pruning is per FILE, not per directory, which is what keeps this case right: the
    # bundle's own file goes and the crew's file in the same directory stays. Pruning by
    # directory name would take both.
    build_bundle(
        tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n", "obsolete": "# O\n"}
    )
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "kiro" / "agents"
    bundle_mod.install_bundle(settings, agents_dir=agents)
    notes = settings.data_home / "skills" / "obsolete" / "NOTES.md"
    notes.write_text("# mine\n", encoding="utf-8")

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert not (settings.data_home / "skills" / "obsolete" / "SKILL.md").exists()
    assert notes.read_text(encoding="utf-8") == "# mine\n"


def test_an_unrecorded_skill_is_left_alone(tmp_path):
    # No marker means the previous install's file list is unknown, so NOTHING is pruned.
    # A stale skill an operator can delete is a smaller harm than deleting a skill the
    # crew produced, and this is the case where the two cannot be told apart.
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    unknown = settings.data_home / "skills" / "arrived-somehow"
    unknown.mkdir(parents=True, exist_ok=True)
    (unknown / "SKILL.md").write_text("# Unknown\n", encoding="utf-8")

    bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")

    assert (unknown / "SKILL.md").read_text(encoding="utf-8") == "# Unknown\n"


def test_the_marker_records_the_bundle_managed_files(tmp_path):
    # The next install can only prune what this one recorded, so the record is the
    # mechanism rather than a log line. An empty or absent list means "prune nothing".
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")

    payload = json.loads(
        (settings.data_home / bundle_mod.INSTALLED_MARKER).read_text(encoding="utf-8")
    )
    assert payload["skill_files"] == ["greet/SKILL.md"]


def test_a_hand_edited_marker_prunes_nothing(tmp_path):
    # The marker is written by the install and read by the next one, so a corrupt or
    # hand-edited one must degrade to "cannot decide" rather than to a wide prune.
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    agents = tmp_path / "kiro" / "agents"
    bundle_mod.install_bundle(settings, agents_dir=agents)
    mine = settings.data_home / "skills" / "greet" / "MINE.md"
    mine.write_text("# mine\n", encoding="utf-8")
    (settings.data_home / bundle_mod.INSTALLED_MARKER).write_text("{not json", encoding="utf-8")

    bundle_mod.install_bundle(settings, agents_dir=agents)

    assert mine.read_text(encoding="utf-8") == "# mine\n"


def test_a_nested_symlink_in_the_old_skills_tree_is_removed_not_followed(tmp_path):
    # A symlink an agent planted inside the skills tree, at a path the BUNDLE owns, is
    # removed before the bundle's own file is written there -- unlinking a pointer
    # destroys no work, and the link's target is never opened. Refusing instead would
    # let a leftover link stop every future boot on a persistent volume.
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    victim = _outside_target(tmp_path)
    nested = settings.data_home / "skills" / "greet"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "SKILL.md").symlink_to(victim)

    bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")

    # The victim was never written through the link.
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"
    # The planted link is gone and the real bundle skill is in place.
    assert not (settings.data_home / "skills" / "greet" / "SKILL.md").is_symlink()
    assert (settings.data_home / "skills" / "greet" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Greet\nSay hello.\n"


def test_a_symlink_planted_at_the_marker_is_refused(tmp_path):
    build_bundle(tmp_path, crew_name="frontdesk")
    settings = make_settings(tmp_path, crew_name="frontdesk")
    victim = _outside_target(tmp_path)
    (settings.data_home / bundle_mod.INSTALLED_MARKER).symlink_to(victim)

    with pytest.raises(ConfigError, match="not a symlink"):
        bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"

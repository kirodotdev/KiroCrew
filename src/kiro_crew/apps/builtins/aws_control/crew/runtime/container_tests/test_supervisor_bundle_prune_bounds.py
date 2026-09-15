"""The prune is bounded by the code, not by what the marker says.

`install_bundle` prunes the skills an earlier bundle installed and this one dropped,
reading that set from `.smc-crew-installed.json` in the data home. The data home is
writable by the running crew, so the record is an INPUT: an absolute entry, a
non-normalised one, one carrying `..`, or a clean-looking one whose parent is a symlink
would each turn "delete the bundle's own file" into "delete a file of the entry's
choosing".

Narrower and unbounded is worse than broad and contained. The bound therefore lives in
two places: the record is refused as a whole when any entry is not a plain relative
path, and the deletion walks descriptors from the skills directory with symlinks
refused, so the directory verified is the directory deleted from and no re-resolution
happens in between.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import ConfigError
from container.supervisor import bundle as bundle_mod

from .test_supervisor_bundle import build_bundle, make_settings


def _install(settings, tmp_path: Path):
    return bundle_mod.install_bundle(settings, agents_dir=tmp_path / "kiro" / "agents")


def _seed(tmp_path: Path):
    """One completed install, so a second one has a record to prune from."""
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    _install(settings, tmp_path)
    return settings


def _record(settings, entries: list[str], *, sign: bool = True) -> None:
    """Rewrite the marker's recorded set, re-signing it as the supervisor would.

    Re-signing is what makes these path tests test the PATH rules. An unsigned rewrite is
    rejected by the marker's authentication before any entry is looked at, so it would
    prove only that authentication works -- which
    ``test_a_forged_marker_is_not_read`` covers separately. Pass ``sign=False`` to
    exercise that direction instead.
    """
    marker = settings.data_home / bundle_mod.INSTALLED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["skill_files"] = entries
    payload.pop("mac", None)
    if sign:
        payload["mac"] = bundle_mod._marker_mac(payload, settings.control_secret)
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_a_forged_marker_is_not_read(tmp_path: Path, caplog) -> None:
    """The premise, not another layer on top of it.

    The marker lives in the data home, which the crew's worker can write, so its
    contents are an input. It carries an authentication tag computed with
    ``SMC_CONTROL_SECRET`` -- the one value ``build_backend_env`` removes from the
    environment the worker inherits -- so a record the supervisor did not write is
    detectable, and an undetectable one cannot be produced by the party that could
    otherwise forge it.
    """
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n", "obsolete": "# O\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    _install(settings, tmp_path)
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n"})
    _record(settings, ["obsolete/SKILL.md"], sign=False)

    with caplog.at_level("ERROR"):
        _install(settings, tmp_path)

    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()
    assert "authentication tag" in caplog.text


def test_a_marker_with_a_wrong_tag_is_not_read(tmp_path: Path, caplog) -> None:
    """A tag that does not verify is louder than a missing one, and says which it was."""
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n", "obsolete": "# O\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    _install(settings, tmp_path)
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n"})
    marker = settings.data_home / bundle_mod.INSTALLED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["skill_files"] = ["obsolete/SKILL.md"]
    payload["mac"] = "00" * 32
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with caplog.at_level("ERROR"):
        _install(settings, tmp_path)

    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()
    assert "does not verify" in caplog.text


def test_without_a_control_secret_nothing_is_pruned(tmp_path: Path, caplog) -> None:
    """No key means no authentication, which means no pruning.

    The deployment, not this code, decides whether a control secret exists. When it does
    not, the honest answer is that the record cannot be told from a forgery, so it is not
    acted on -- and the install still proceeds, because refusing to boot over an
    unsignable bookkeeping file would be the worse failure.
    """
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n", "obsolete": "# O\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk", control_secret=None)
    _install(settings, tmp_path)
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n"})

    with caplog.at_level("WARNING"):
        _install(settings, tmp_path)

    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()
    assert "cannot be authenticated" in caplog.text


def test_a_regular_file_where_a_skill_directory_belongs_refuses_to_start(tmp_path: Path) -> None:
    """A boot loop is worse than a refusal, for the reason a crash is.

    A previous task can leave a regular file where this bundle ships a skill directory.
    ``mkdir`` raises ``FileExistsError`` there, and an uncaught one makes the supervisor
    crash, ECS restart the task, and the next boot hit the same file on the same volume --
    forever, spending the owner's money and never saying what is wrong. So it is a
    refusal that names the path.
    """
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    skills = settings.data_home / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "greet").write_text("a file where a directory belongs\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="not a directory") as exc:
        _install(settings, tmp_path)
    assert str(skills / "greet") in str(exc.value), "the refusal must name the path"


@pytest.mark.parametrize(
    "entry,because",
    [
        ("/etc/hostname", "it is absolute"),
        ("../../escaped.txt", "it has a '.', '..' or empty component"),
        ("greet/../../escaped.txt", "it is not normalised"),
        ("./greet/SKILL.md", "it is not normalised"),
        ("greet//SKILL.md", "it is not normalised"),
    ],
)
def test_a_malformed_entry_is_refused_and_names_itself(
    tmp_path: Path, caplog, entry: str, because: str
) -> None:
    """Each shape produces its own refusal, naming the entry and the reason.

    Refused rather than repaired: a silently sanitised path hides that the record is
    corrupt or that something is editing it, and this record sits in a tree the crew can
    write.
    """
    settings = _seed(tmp_path)
    victim = tmp_path / "escaped.txt"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    _record(settings, [entry])

    with caplog.at_level("ERROR"):
        _install(settings, tmp_path)

    assert entry in caplog.text, "the refusal must name the offending entry"
    assert because in caplog.text
    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_one_bad_entry_discards_the_whole_record(tmp_path: Path) -> None:
    """All-or-nothing, and deliberately so.

    A malformed entry means the record is corrupt or hostile, and the rest of it is no
    more trustworthy than the bad entry. So nothing is pruned -- including the entry
    beside it that looks fine -- and the install proceeds, leaving residue rather than
    deleting from a list something else has been editing.
    """
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n", "obsolete": "# O\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    _install(settings, tmp_path)
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n"})
    _record(settings, ["obsolete/SKILL.md", "/etc/hostname"])

    _install(settings, tmp_path)

    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()


def test_an_entry_whose_parent_is_a_symlink_is_refused(tmp_path: Path, caplog) -> None:
    """The case a string check cannot see.

    ``skills/away/SKILL.md`` carries no ``..`` and is perfectly normalised, so shape
    validation passes it. If ``away`` is a symlink, deleting by path deletes someone
    else's file. The descriptor walk refuses the component instead, and the resolved
    containment check is what gives the refusal a message an operator can read.
    """
    settings = _seed(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "SKILL.md"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    (settings.data_home / "skills" / "away").symlink_to(outside, target_is_directory=True)
    _record(settings, ["away/SKILL.md"])

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    with caplog.at_level("WARNING"):
        _install(settings, tmp_path)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n", "a file outside was deleted"
    assert "away/SKILL.md" in caplog.text
    assert "outside the skills directory" in caplog.text


def test_a_symlinked_entry_pointing_outside_is_left_alone(tmp_path: Path, caplog) -> None:
    """A link AT the recorded path, aimed outside, is caught by containment.

    Two guards can answer this and the resolved-containment one answers first, which is
    the better message: it says where the entry would have gone rather than only that it
    is not a plain file.
    """
    settings = _seed(tmp_path)
    victim = tmp_path / "linked-target.md"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    (settings.data_home / "skills" / "greet" / "GONE.md").symlink_to(victim)
    _record(settings, ["greet/GONE.md"])

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    with caplog.at_level("WARNING"):
        _install(settings, tmp_path)

    assert victim.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert "greet/GONE.md" in caplog.text
    assert "outside the skills directory" in caplog.text


def test_a_symlinked_entry_pointing_inside_is_left_alone(tmp_path: Path, caplog) -> None:
    """The case containment cannot answer, so ``lstat`` on the descriptor does.

    A link that stays inside the skills directory passes both the shape check and the
    resolved-containment check. It is still not the file this install put there, and
    unlinking it would delete the crew's link rather than a bundle file, so the leaf's
    own type is what decides -- read from the descriptor, about the link and not its
    target.
    """
    settings = _seed(tmp_path)
    inside = settings.data_home / "skills" / "greet" / "SKILL.md"
    (settings.data_home / "skills" / "greet" / "GONE.md").symlink_to(inside)
    _record(settings, ["greet/GONE.md"])

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# Greet\n"})
    with caplog.at_level("WARNING"):
        _install(settings, tmp_path)

    assert (settings.data_home / "skills" / "greet" / "GONE.md").is_symlink()
    assert inside.is_file(), "the link's target inside the tree survived"
    assert "no longer a plain file" in caplog.text


def test_a_well_formed_entry_inside_the_tree_is_still_pruned(tmp_path: Path) -> None:
    """Non-vacuity for every refusal above: the ordinary prune must still work.

    A bound that refused everything would make the tests above pass while quietly
    restoring the stale-skill hazard the prune exists to close.
    """
    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n", "obsolete": "# O\n"})
    settings = make_settings(tmp_path, crew_name="frontdesk")
    _install(settings, tmp_path)
    assert (settings.data_home / "skills" / "obsolete" / "SKILL.md").is_file()

    build_bundle(tmp_path, crew_name="frontdesk", skills={"greet": "# G\n"})
    _install(settings, tmp_path)

    assert not (settings.data_home / "skills" / "obsolete").exists()

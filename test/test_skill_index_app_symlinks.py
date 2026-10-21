"""App-provided skills must be visible to the skill scanner.

``apps.bridges._register_skills`` symlinks an app's skill directories into the
skills tree "so the skill scanner finds the skill". Those symlinks resolve into
the app's own tree, i.e. OUTSIDE the skills base, so a containment check written
against the RESOLVED path silently pruned every one of them: an installed app's
skills were absent from the pre-listed skills block AND from ``skill_search``,
while still being reachable only by absolute path.

These tests lock in both halves of the fix: a symlink into a trusted provider
root is enumerated, and a symlink to an arbitrary target is still refused.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
import kiro_crew.skills_discovery as skills_disc
from kiro_crew.skills import _iter_skill_files, _within_any
from kiro_crew.skills_builtin_provenance import _skill_script_index

# Creating a symlink on Windows needs elevation or Developer Mode, which CI
# runners do not have. The repo's established idiom is to skip such a test there
# (see test/test_agent_home_isolation.py). Applied per-class rather than to the
# whole module on purpose: the pure path-semantics tests below carry their weight
# precisely ON Windows, where separators and case-folding differ.
requires_symlinks = pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation needs elevation on Windows",
)


def _write_skill(directory: Path, body: str = "# Skill\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    skill_file = directory / "SKILL.md"
    skill_file.write_text(body, encoding="utf-8")
    return skill_file


def _packaged_app_skill() -> Path:
    """A skill dir that a built-in app ships INSIDE the installed package.

    Resolved rather than hardcoded per-test so drift fails with this message
    instead of a bare ``FileNotFoundError`` from ``os.symlink``.
    """
    skill = (
        Path(skills_mod.__file__).parent / "apps" / "builtins" / "dev_fleet" / "skills" / "pod-e2e"
    )
    assert skill.is_dir(), f"fixture drift: expected a built-in app skill at {skill}"
    return skill


@requires_symlinks
class TestBuiltinAppSkillsAreVisible:
    def test_symlink_into_the_package_tree_is_enumerated(self, tmp_path: Path) -> None:
        """A built-in app's skill lives under the installed package, not the skills base."""
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(_packaged_app_skill()), str(base / "pod-e2e"))

        found = dict(_iter_skill_files(base))
        assert "pod-e2e" in found, "app skill symlinked into the skills tree must be indexed"
        assert found["pod-e2e"].is_file()

    def test_double_registration_yields_one_key(self, tmp_path: Path) -> None:
        """bridges registers BOTH ``skills/<app>/<skill>`` and a flat ``skills/<skill>``.

        Both links resolve to one target, so the symlink-loop guard keeps exactly
        one. Loading by either name still works — ``load_skill`` reads the path
        directly and both symlinks are on disk regardless of which key is indexed.
        """
        packaged = _packaged_app_skill()
        base = tmp_path / "skills"
        (base / "dev-fleet").mkdir(parents=True)
        os.symlink(str(packaged), str(base / "dev-fleet" / "pod-e2e"))
        os.symlink(str(packaged), str(base / "pod-e2e"))

        names = [name for name, _ in _iter_skill_files(base)]
        assert (
            names.count("dev-fleet/pod-e2e") + names.count("pod-e2e") == 1
        ), f"one skill must yield one key, got {names}"

    def test_surviving_key_is_a_function_of_the_names_not_the_filesystem(
        self, tmp_path: Path
    ) -> None:
        """The sort buys DETERMINISM, not a namespacing preference.

        Which of the two registered keys survives depends on how the app
        directory name collates against the skill name — ``dev-fleet`` sorts
        before ``pod-e2e`` so the namespaced key wins, while ``zzz-app`` sorts
        after it so the flat key wins. Both are correct; the point is that the
        outcome is a pure function of the names rather than of ``scandir`` order.
        """
        packaged = _packaged_app_skill()

        early = tmp_path / "early"
        (early / "dev-fleet").mkdir(parents=True)
        os.symlink(str(packaged), str(early / "dev-fleet" / "pod-e2e"))
        os.symlink(str(packaged), str(early / "pod-e2e"))
        assert "dev-fleet/pod-e2e" in [n for n, _ in _iter_skill_files(early)]

        late = tmp_path / "late"
        (late / "zzz-app").mkdir(parents=True)
        os.symlink(str(packaged), str(late / "zzz-app" / "pod-e2e"))
        os.symlink(str(packaged), str(late / "pod-e2e"))
        assert "pod-e2e" in [n for n, _ in _iter_skill_files(late)]

        # Repeating a walk must give the identical key both times.
        assert [n for n, _ in _iter_skill_files(early)] == [n for n, _ in _iter_skill_files(early)]

    def test_symlink_into_the_installed_apps_dir_is_enumerated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An externally installed app keeps its skills under ``<data home>/apps``."""
        data_home = tmp_path / "home"
        _write_skill(data_home / "apps" / "my-app" / "skills" / "thing")
        monkeypatch.setattr(skills_mod, "config_dir", lambda: data_home)

        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(data_home / "apps" / "my-app" / "skills" / "thing"), str(base / "thing"))

        names = {name for name, _ in _iter_skill_files(base)}
        assert "thing" in names


@requires_symlinks
class TestUntrustedTargetsStillRefused:
    def test_symlink_to_an_arbitrary_directory_is_refused(self, tmp_path: Path) -> None:
        """An arbitrary target would admit unvetted SKILL.md prose into the context."""
        outsider = tmp_path / "elsewhere" / "evil"
        _write_skill(outsider, "# Injected\n")

        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "evil"))

        names = {name for name, _ in _iter_skill_files(base)}
        assert "evil" not in names, "a symlink outside every trusted root must stay pruned"

    def test_sensitive_target_is_refused_even_inside_a_trusted_root(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The credential-store guard runs AFTER the trusted-root check, not instead of it."""
        data_home = tmp_path / "home"
        secret_skill = data_home / "apps" / "my-app" / "skills" / "secretish"
        _write_skill(secret_skill)
        monkeypatch.setattr(skills_mod, "config_dir", lambda: data_home)

        resolved = os.path.realpath(secret_skill)
        # The walk asks the fence through the pre-resolved gate, with the
        # realpath it already computed; stub that gate, not is_sensitive_path.
        monkeypatch.setattr(skills_mod, "is_sensitive_resolved_path", lambda p: p == resolved)

        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(secret_skill), str(base / "secretish"))

        names = {name for name, _ in _iter_skill_files(base)}
        assert "secretish" not in names


@requires_symlinks
class TestOutsideRootLinkIsReported:
    """A refused out-of-root link in the skills base is reported, not silent.

    A user who links a skill folder into the skills directory from an outside
    repository has the folder dropped from the catalog by the containment gate.
    That refusal is a security boundary and stays. These tests lock in that the
    refused direct-child link emits one warning naming the target and
    ``skills.extra_paths`` as the supported fix.
    """

    @pytest.fixture(autouse=True)
    def _reset_dedup(self):
        # The warning dedups through the module's shared warn-once registry.
        # Tolerant of the attribute being absent so the red-first run on an
        # unfixed tree fails on the missing-warning assertion, not here.
        getattr(skills_disc, "_CHAIN_REFUSALS_WARNED", set()).clear()
        yield
        getattr(skills_disc, "_CHAIN_REFUSALS_WARNED", set()).clear()

    def test_outside_link_is_refused_and_warned(self, tmp_path: Path, caplog) -> None:
        outsider = tmp_path / "elsewhere" / "my-skill"
        _write_skill(outsider, "# Injected\n")
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "my-skill"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            names = {name for name, _ in _iter_skill_files(base, warn_outside_links=True)}

        assert "my-skill" not in names, "the containment boundary must still refuse it"
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        hits = [
            m
            for m in warnings
            if os.path.realpath(str(outsider)) in m and "skills.extra_paths" in m
        ]
        assert (
            len(hits) == 1
        ), f"expected one warning naming the target and skills.extra_paths, got {warnings}"

    def test_repeat_scans_warn_only_once(self, tmp_path: Path, caplog) -> None:
        outsider = tmp_path / "elsewhere" / "my-skill"
        _write_skill(outsider, "# Injected\n")
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "my-skill"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            _iter_skill_files(base, warn_outside_links=True)
            _iter_skill_files(base, warn_outside_links=True)
            _iter_skill_files(base, warn_outside_links=True)

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "skills.extra_paths" in r.getMessage()
        ]
        assert len(warnings) == 1, f"the warning must be deduped per process, got {warnings}"

    def test_a_non_catalog_walk_never_warns(self, tmp_path: Path, caplog) -> None:
        """The warning is opt-in: a walk that does not request it stays silent.

        Every caller but the catalog base walk (``kirocrew doctor``'s script
        index, provenance, selection, sync) walks with ``warn_outside_links``
        off, so a refused direct-child link is dropped silently there, matching
        the behaviour before this warning existed. Only the catalog can say a
        skill was dropped from the whole catalog, so only it warns.
        """
        outsider = tmp_path / "elsewhere" / "my-skill"
        _write_skill(outsider, "# Injected\n")
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "my-skill"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            names = {name for name, _ in _iter_skill_files(base)}

        assert "my-skill" not in names, "the containment boundary must still refuse it"
        assert not [
            r for r in caplog.records if "skills.extra_paths" in r.getMessage()
        ], "a walk that did not opt in must not emit the outside-root warning"

    def test_skill_script_index_walk_never_warns(self, tmp_path: Path, caplog) -> None:
        """The ``kirocrew doctor`` script-index walk is a non-catalog walk: silent.

        ``_skill_script_index`` walks the skills dir to map script assets and
        does not opt into the warning, so an out-of-root link in that dir is
        dropped without the "add it to skills.extra_paths" advice that only the
        catalog base walk emits.
        """
        outsider = tmp_path / "elsewhere" / "my-skill"
        _write_skill(outsider, "# Injected\n")
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "my-skill"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            _skill_script_index(base)

        assert not [
            r for r in caplog.records if "skills.extra_paths" in r.getMessage()
        ], "the doctor script-index walk must not emit the outside-root warning"

    def test_link_inside_an_allowed_root_is_admitted_without_warning(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A link whose target IS inside an allowed root loads and must not warn."""
        data_home = tmp_path / "home"
        _write_skill(data_home / "apps" / "my-app" / "skills" / "thing")
        monkeypatch.setattr(skills_mod, "config_dir", lambda: data_home)

        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(data_home / "apps" / "my-app" / "skills" / "thing"), str(base / "thing"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            names = {name for name, _ in _iter_skill_files(base, warn_outside_links=True)}

        assert "thing" in names
        assert not [
            r for r in caplog.records if "skills.extra_paths" in r.getMessage()
        ], "an admitted link must not emit the outside-root warning"

    def test_following_the_advice_stops_the_warning(self, tmp_path: Path, caplog) -> None:
        """A link whose target is under a configured extra-path root stops warning.

        The operator who adds the target's parent to ``skills.extra_paths`` (the
        fix the warning names) loads the skill through the separate extra-path
        walk, which the catalog passes to the base walk as ``extra_path_roots``.
        The base walk still refuses the link locally (containment is unchanged)
        and stays silent, so the advice actually silences the message.
        """
        outsider = tmp_path / "repo" / "my-skill"
        _write_skill(outsider, "# Injected\n")
        base = tmp_path / "skills"
        base.mkdir()
        os.symlink(str(outsider), str(base / "my-skill"))
        extra_root = os.path.realpath(str(tmp_path / "repo"))

        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            names = {
                name
                for name, _ in _iter_skill_files(
                    base, warn_outside_links=True, extra_path_roots=(extra_root,)
                )
            }

        assert "my-skill" not in names, "the base walk still refuses the link"
        assert not [
            r for r in caplog.records if "skills.extra_paths" in r.getMessage()
        ], "a link whose target is under an extra-path root must not warn"


class TestPreExistingBehaviourPreserved:
    def test_plain_in_base_skill_still_found(self, tmp_path: Path) -> None:
        base = tmp_path / "skills"
        _write_skill(base / "nested" / "regular")
        names = {name for name, _ in _iter_skill_files(base)}
        assert "nested/regular" in names

    def test_dot_directories_still_pruned(self, tmp_path: Path) -> None:
        base = tmp_path / "skills"
        _write_skill(base / "auto" / ".archive" / "old")
        _write_skill(base / "auto" / "live")
        names = {name for name, _ in _iter_skill_files(base)}
        assert "auto/live" in names
        assert not any(".archive" in n for n in names)

    @requires_symlinks
    def test_symlink_loop_is_pruned(self, tmp_path: Path) -> None:
        """A self-referential link must not spin the walk forever."""
        base = tmp_path / "skills"
        _write_skill(base / "real")
        os.symlink(str(base), str(base / "loop"))
        names = {name for name, _ in _iter_skill_files(base)}
        assert "real" in names

    def test_missing_base_returns_empty(self, tmp_path: Path) -> None:
        assert _iter_skill_files(tmp_path / "nope") == []


class TestWithinAny:
    def test_exact_root_matches(self, tmp_path: Path) -> None:
        assert _within_any(str(tmp_path), (str(tmp_path),))

    def test_descendant_matches(self, tmp_path: Path) -> None:
        assert _within_any(str(tmp_path / "a" / "b"), (str(tmp_path),))

    def test_sibling_does_not_match(self, tmp_path: Path) -> None:
        assert not _within_any(str(tmp_path / "a"), (str(tmp_path / "b"),))

    def test_no_roots_never_matches(self, tmp_path: Path) -> None:
        assert not _within_any(str(tmp_path), ())

    def test_prefix_lookalike_does_not_match(self, tmp_path: Path) -> None:
        """``/x/skills-evil`` is not under ``/x/skills`` despite the string prefix."""
        assert not _within_any(str(tmp_path / "skills-evil"), (str(tmp_path / "skills"),))

"""What a crew is told about merging and auto-merge.

A crew runs its forge's CLI from its own shell, so the app's refusal to arm GitLab
auto-merge never reaches it: the nudge is the only control. A live GitLab crew ran
``glab api -X PUT .../merge -f merge_when_pipeline_succeeds=true`` and GitLab merged
the MR when its pipeline passed, with no approval rule and no human. These tests pin
that ``may_arm_auto_merge`` (the crew's ``auto_merge``, never on GitLab, never without
a key) decides the one merge rule the crew is told.
"""

import tempfile
import unittest
from pathlib import Path

from issue_radar_crew_runtime_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    OWNER,
    REPO,
    _app_is_on,
    _crew,
    _isolated_crew_log,
    _private_sel_root_per_test,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime as cr
from kiro_crew.apps.builtins.issue_radar.backend import provider
from kiro_crew.apps.builtins.issue_radar.backend.errors import ProviderCliError

ARM = "auto-merge once it is green and mergeable"
NEVER_ARM = "or arm auto-merge"


def _key(forge: str) -> provider.RepoKey:
    return provider.key_from_parts(OWNER, REPO, forge, "forge.example")


class TestMayArmAutoMerge(unittest.TestCase):
    def test_github_and_azure_follow_the_toggle(self):
        for forge in (provider.GITHUB, provider.AZURE):
            with self.subTest(forge=forge):
                self.assertIs(cr.may_arm_auto_merge({"auto_merge": True}, _key(forge)), True)
                self.assertIs(cr.may_arm_auto_merge({"auto_merge": False}, _key(forge)), False)
                self.assertIs(cr.may_arm_auto_merge({}, _key(forge)), False)

    def test_gitlab_never_arms_whatever_the_toggle(self):
        """The crew record defaults ``auto_merge`` to True; on GitLab that must not
        reach the crew as permission, because arming there IS the merge call."""
        self.assertIs(cr.may_arm_auto_merge({"auto_merge": True}, _key(provider.GITLAB)), False)

    def test_no_key_never_arms(self):
        """A caller that lost the repo's forge must not default to one that arms."""
        self.assertIs(cr.may_arm_auto_merge({"auto_merge": True}, None), False)

    def test_gitlabs_own_client_refuses_to_arm_too(self):
        """The same rule, held by the app's own route: the two must agree."""
        with self.assertRaises(ProviderCliError):
            provider.client_for(_key(provider.GITLAB)).enable_auto_merge("o", "r", 1)


class TestCrewMergeRule(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _nudge(self, key: provider.RepoKey | None, **spec) -> str:
        # Crew names are unique per repo, and one test may build several crews.
        self._n = getattr(self, "_n", 0) + 1
        crew = _crew(self.root, name=f"Andromeda-{self._n}", **spec)
        return cr.compose_nudge(cr.build_snapshot(OWNER, REPO, crew, self.root, key))

    def test_a_gitlab_crew_is_told_never_to_arm_and_why(self):
        nudge = self._nudge(_key(provider.GITLAB), auto_merge=True)
        self.assertIn("never merge a MR yourself", nudge)
        self.assertIn(NEVER_ARM, nudge)
        self.assertIn("`merge_when_pipeline_succeeds` is a merge", nudge)
        self.assertIn("leave it for a person to merge", nudge)
        self.assertNotIn(ARM, nudge)

    def test_a_github_crew_with_auto_merge_on_may_arm_it(self):
        nudge = self._nudge(_key(provider.GITHUB), auto_merge=True)
        self.assertIn("arm GitHub's own auto-merge once it is green", nudge)
        self.assertIn("never merge a PR yourself", nudge)
        self.assertNotIn(NEVER_ARM, nudge)

    def test_a_github_crew_with_auto_merge_off_never_arms_it(self):
        """The editor toggle is the crew's only source for this rule."""
        nudge = self._nudge(_key(provider.GITHUB), auto_merge=False)
        self.assertIn("never merge a PR yourself", nudge)
        self.assertIn(NEVER_ARM, nudge)
        self.assertIn("leave it for a person to merge", nudge)
        self.assertNotIn(ARM, nudge)
        # The GitLab flag is GitLab's: naming it on GitHub names nothing there.
        self.assertNotIn("merge_when_pipeline_succeeds", nudge)

    def test_an_azure_crew_with_auto_merge_on_may_arm_it(self):
        self.assertIn(ARM, self._nudge(_key(provider.AZURE), auto_merge=True))

    def test_a_snapshot_built_without_a_key_leaves_the_merge_to_a_person(self):
        self.assertIn(NEVER_ARM, self._nudge(None, auto_merge=True))

    def test_a_snapshot_without_the_flag_leaves_the_merge_to_a_person(self):
        """A snapshot built before the field existed, or by hand, must not arm."""
        crew = _crew(self.root, name="Bode", auto_merge=True)
        snapshot = cr.build_snapshot(OWNER, REPO, crew, self.root, _key(provider.GITHUB))
        snapshot.pop("arm_auto_merge")
        self.assertIn(NEVER_ARM, cr.compose_nudge(snapshot))


if __name__ == "__main__":
    unittest.main()

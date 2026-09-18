"""A crew-home sandbox carve-out must be spelled every way the masks spell it.

The masks name one crew-home directory three ways: ``$HOME/.kiro/crew/<leaf>``,
``$HOME/.kirocrew/<leaf>`` (both joined from ``Path.home()``, which does not
resolve links) and the ``config_dir()`` path ``_relocated_crew_targets`` adds
(which does, because ``KIROCREW_HOME`` is ``resolve()``d). Under a symlinked
``$HOME`` -- ``/home/u -> /local/home/u``, the shape every Amazon cloud desktop
has -- those are DIFFERENT STRINGS for THE SAME directory.

``extra_visible_dirs`` lifts a mask lexically (``commonpath``, never
``realpath``: the builders run on the event loop). So a carve-out that names one
spelling lifts one entry, and the aliasing entry survives to bind an empty
directory straight back over the tree that was just exposed. The gateway then
stages a file the child cannot see, and the child reports it missing -- which is
how the AWS Control drive preview failed with

    s3:GetObject failed: [Errno 2] No such file or directory:
    '<data home>/aws-control-staging/drive-preview-XXXXXXX/object'

on a path the gateway had created with ``O_EXCL`` moments earlier.
"""

from __future__ import annotations

import json
import os
import re

import pytest

import kiro_crew.sandbox as sb
from kiro_crew import platform_compat

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="the launcher script and the symlinked home are POSIX mechanisms",
)

_STAGING_LEAF = "aws-control-staging"


def _hidden_dirs(mode: str = "standard", **kwargs) -> set[str]:
    script = sb._build_launcher_script(mode, **kwargs)
    match = re.search(r"SENSITIVE_DIRS = (\[.*?\])\n", script, re.S)
    assert match, "SENSITIVE_DIRS not found in the generated launcher script"
    return set(json.loads(match.group(1)))


@pytest.fixture()
def symlinked_home(tmp_path, monkeypatch):
    """A data home reached through every symlinked ``$HOME`` spelling.

    ``Path.home()`` keeps the link spelling and ``config_dir()`` returns the
    resolved one, while both ``$HOME``-joined mask roots resolve to that same
    directory. This is the production shape when the supported data-home aliases
    are present.
    """
    real = tmp_path / "real"
    data_home = real / ".kirocrew"
    data_home.mkdir(parents=True)
    current_parent = real / ".kiro"
    current_parent.mkdir()
    (current_parent / "crew").symlink_to(data_home, target_is_directory=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: link))
    monkeypatch.setattr(sb, "config_dir", lambda: data_home)
    return link, data_home


class TestTheSpellingSet:
    def test_a_data_home_path_gains_both_home_joined_spellings(self, symlinked_home):
        link, data_home = symlinked_home
        staged = data_home / _STAGING_LEAF / "drive-preview-abc123"

        spellings = sb.crew_home_visible_spellings(str(staged))

        # The caller's own spelling first, then one per crew-home prefix -- the
        # aliasing entries whose masks would otherwise survive the lift.
        assert set(spellings) == {
            str(staged),
            str(link / ".kiro" / "crew" / _STAGING_LEAF / "drive-preview-abc123"),
            str(link / ".kirocrew" / _STAGING_LEAF / "drive-preview-abc123"),
        }

    def test_the_reported_host_shape_lifts_its_one_alias_and_nothing_else(
        self, monkeypatch, tmp_path
    ):
        # The exact host that reported the bug: ``$HOME`` is a symlink, the data
        # home is reached as ``$HOME/.kirocrew``, and the OTHER crew-home prefix
        # does not exist at all. Pinned as one case because the fixture above
        # provisions both aliases and the two gate tests use a plain home, so the
        # shape that actually produced the ENOENT was covered only piecewise.
        real = tmp_path / "real"
        data_home = real / ".kirocrew"
        data_home.mkdir(parents=True)
        home = tmp_path / "link"
        home.symlink_to(real, target_is_directory=True)
        assert not (home / ".kiro").exists(), "this case needs the other prefix absent"
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(sb, "config_dir", lambda: data_home)
        staged = data_home / _STAGING_LEAF / "drive-preview-abc123"

        spellings = sb.crew_home_visible_spellings(str(staged))

        assert set(spellings) == {
            str(staged),
            str(home / ".kirocrew" / _STAGING_LEAF / "drive-preview-abc123"),
        }
        # And the whole point: no mask entry is an ANCESTOR of a granted
        # spelling, so nothing can be bound over the staged file. The absent
        # ``.kiro/crew`` prefix is still listed -- the launcher names both
        # unconditionally and skips what does not exist -- and it stays listed
        # here, which is what keeps a foreign home covered.
        hidden = _hidden_dirs(extra_visible_dirs=spellings)
        covering = {
            entry
            for entry in hidden
            for spelling in spellings
            if os.path.commonpath((entry, spelling)) == entry
        }
        assert covering == set(), sorted(covering)
        assert str(home / ".kiro" / "crew" / _STAGING_LEAF) in hidden

    def test_a_real_unrelated_crew_home_stays_masked(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        unrelated_root = home / ".kiro" / "crew"
        unrelated_root.mkdir(parents=True)
        data_home = tmp_path / "configured"
        data_home.mkdir()
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(sb, "config_dir", lambda: data_home)
        staged = data_home / _STAGING_LEAF / "drive-preview-abc123"
        unrelated_staged = unrelated_root / _STAGING_LEAF / "drive-preview-abc123"

        spellings = sb.crew_home_visible_spellings(str(staged))

        assert str(unrelated_staged) not in spellings
        hidden = _hidden_dirs(extra_visible_dirs=spellings)
        assert str(unrelated_root / _STAGING_LEAF) in hidden

    def test_a_nonexistent_crew_home_root_adds_no_spelling(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        data_home = tmp_path / "configured"
        data_home.mkdir()
        monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(sb, "config_dir", lambda: data_home)
        staged = data_home / _STAGING_LEAF / "drive-preview-abc123"

        spellings = sb.crew_home_visible_spellings(str(staged))

        assert spellings[0] == str(staged)
        assert (
            str(home / ".kiro" / "crew" / _STAGING_LEAF / "drive-preview-abc123") not in spellings
        )
        assert str(home / ".kirocrew" / _STAGING_LEAF / "drive-preview-abc123") not in spellings

    def test_a_path_outside_the_data_home_is_returned_unchanged(self, symlinked_home, tmp_path):
        # A workspace or repo carve-out has no crew-home spelling to add, and
        # inventing one would hand the spawn a directory nobody asked for.
        elsewhere = tmp_path / "workspace" / "repo"

        assert sb.crew_home_visible_spellings(str(elsewhere)) == (str(elsewhere),)

    def test_an_unresolvable_home_still_yields_the_callers_spelling(self, monkeypatch, tmp_path):
        # Degrades to the pre-existing behaviour instead of failing the spawn.
        def boom():
            raise OSError("no home")

        monkeypatch.setattr(sb, "config_dir", boom)
        target = tmp_path / "whatever"

        assert sb.crew_home_visible_spellings(str(target)) == (str(target),)


class TestTheLauncherStopsMaskingTheStagedDirectory:
    """The point of the spelling set: no surviving mask over the staged file."""

    def _staging_masks(self, hidden: set[str]) -> set[str]:
        return {path for path in hidden if _STAGING_LEAF in path}

    def test_every_spelling_is_masked_without_a_carve_out(self, symlinked_home):
        # The baseline the carve-out has to defeat: all three spellings are
        # masked, and two of them are the symlinked home's.
        masks = self._staging_masks(_hidden_dirs())

        assert len(masks) >= 2, masks

    def test_naming_only_the_resolved_spelling_leaves_the_aliases_masked(self, symlinked_home):
        # The regression itself. The resolved spelling is lifted; the two
        # ``$HOME``-joined ones name the SAME directory through the link and
        # keep their masks, so the staged file is hidden from the spawn anyway.
        _link, data_home = symlinked_home
        staged_dir = data_home / _STAGING_LEAF / "drive-preview-abc123"

        masks = self._staging_masks(_hidden_dirs(extra_visible_dirs=(str(staged_dir),)))

        assert masks, "expected the aliasing masks to survive a single-spelling carve-out"
        assert str(data_home / _STAGING_LEAF) not in masks

    def test_the_full_spelling_set_lifts_every_mask(self, symlinked_home):
        _link, data_home = symlinked_home
        staged_dir = data_home / _STAGING_LEAF / "drive-preview-abc123"

        masks = self._staging_masks(
            _hidden_dirs(extra_visible_dirs=sb.crew_home_visible_spellings(str(staged_dir)))
        )

        assert masks == set(), (
            "a mask still covers the staged directory, so the sandboxed AWS CLI would "
            f"report the object file missing: {sorted(masks)}"
        )

    def test_lifting_the_staging_leaf_leaves_the_other_crew_masks_alone(self, symlinked_home):
        # The carve-out is scoped: it cancels the entries that CONTAIN the
        # staged path and nothing else, so the rest of the governance tree
        # (token signing key, vault, policy cache) stays masked.
        _link, data_home = symlinked_home
        staged_dir = data_home / _STAGING_LEAF / "drive-preview-abc123"

        hidden = _hidden_dirs(extra_visible_dirs=sb.crew_home_visible_spellings(str(staged_dir)))

        for leaf in ("token_signing.key", ".vault", "policy_cache"):
            assert any(path.endswith(leaf) for path in hidden), leaf


class TestTheDrivePreviewUsesTheSpellingSet:
    def test_the_preview_grants_every_spelling_of_its_staging_directory(
        self, symlinked_home, monkeypatch
    ):
        """The producer side: the grant the AWS CLI spawn actually receives.

        Pinned here rather than in the storage suite because the defect is a
        property of the SPELLING, and the storage suite's staging seam points at
        a temp directory outside the data home, where every spelling collapses to
        one and the bug cannot appear.
        """
        from kiro_crew.apps.builtins.aws_control.backend import storage

        _link, data_home = symlinked_home
        staging_parent = data_home / _STAGING_LEAF
        staging_parent.mkdir(parents=True, exist_ok=True)
        captured: dict = {}

        def fake_checked(argv, profile, **kwargs):
            captured["kwargs"] = kwargs
            with open(argv[-1], "wb") as fh:
                fh.write(b"head")
            return json.dumps({"ContentRange": "bytes 0-3/4"})

        monkeypatch.setattr(storage, "_checked", fake_checked)
        monkeypatch.setattr(storage, "_preview_staging_parent", lambda: staging_parent)

        data, size = storage.get_object_head_bytes(
            "p",
            "us-east-1",
            "b",
            "drive",
            "a.txt",
            account="111122223333",
            max_bytes=1024,
        )

        assert (data, size) == (b"head", 4)
        granted = captured["kwargs"]["extra_visible_dirs"]
        # One grant per crew-home spelling of the per-call directory, and every
        # one of them names that directory -- never the staging root, and never
        # a sibling call's directory.
        assert len(granted) == 3, granted
        for path in granted:
            assert os.path.basename(path).startswith("drive-preview-"), path
            assert os.path.basename(os.path.dirname(path)) == _STAGING_LEAF, path

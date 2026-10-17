"""Regression tests for _resolve_skill_root edition-root resolution.

Edition-contributed skill roots now come from the CPP seam
``McpToolingProvider.extra_skills()`` (public Default ``[]``) rather than a
a hardcoded edition path; tests patch ``DefaultMcpToolingProvider.extra_skills``
to inject a root.
"""

from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers._shared as _shared
from kiro_crew.platform.defaults import DefaultMcpToolingProvider


class _FakeState:
    def __init__(self):
        self._slots = {}


def _no_extra_paths():
    """Mock that prevents real config from leaking extra_paths into tests."""
    raise FileNotFoundError("no config in test")


@pytest.fixture(autouse=True)
def _isolate_config():
    # Warm the platform context BEFORE patching KiroCrewConfig.load to raise —
    # otherwise current_context()'s lazy build (reached via the extra_skills
    # seam in _resolve_skill_root) would call the raising load() and degrade the
    # edition-root lookup to [].
    from kiro_crew.platform.context import current_context

    current_context()
    with patch.object(_shared.KiroCrewConfig, "load", side_effect=_no_extra_paths):
        yield


def _set_edition_roots(monkeypatch, *roots):
    """Patch the extra_skills() seam to expose *roots* as edition skill roots."""
    monkeypatch.setattr(
        DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots)
    )


def test_resolve_skill_root_resolves_edition_nested_key(tmp_path, monkeypatch):
    pkg_root = tmp_path / "package_skills"
    skill_dir = pkg_root / "Pkg" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# hi", encoding="utf-8")

    empty_kirocrew = tmp_path / "kirocrew_skills"
    empty_kirocrew.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_kirocrew)
    _set_edition_roots(monkeypatch, pkg_root)

    resolved = _shared._resolve_skill_root("Pkg/my-skill", _FakeState())
    assert resolved == skill_dir.resolve()


def test_resolve_skill_root_still_prefers_kirocrew_root(tmp_path, monkeypatch):
    mc_root = tmp_path / "kirocrew_skills"
    (mc_root / "local-skill").mkdir(parents=True)
    (mc_root / "local-skill" / "SKILL.md").write_text("# local", encoding="utf-8")
    pkg_root = tmp_path / "package_skills"
    pkg_root.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: mc_root)
    _set_edition_roots(monkeypatch, pkg_root)

    resolved = _shared._resolve_skill_root("local-skill", _FakeState())
    assert resolved == (mc_root / "local-skill").resolve()


def test_resolve_skill_root_rejects_traversal(tmp_path, monkeypatch):
    pkg_root = tmp_path / "package_skills"
    pkg_root.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "mc")
    _set_edition_roots(monkeypatch, pkg_root)

    assert _shared._resolve_skill_root("Pkg/../../etc", _FakeState()) is None
    assert _shared._resolve_skill_root("../etc", _FakeState()) is None
    assert _shared._resolve_skill_root("/etc/passwd", _FakeState()) is None


def test_resolve_skill_root_finds_skill_in_extra_paths(tmp_path, monkeypatch):
    extra_root = tmp_path / "extra_skills"
    (extra_root / "custom-skill").mkdir(parents=True)
    (extra_root / "custom-skill" / "SKILL.md").write_text("# custom", encoding="utf-8")

    empty_mc = tmp_path / "kirocrew_skills"
    empty_mc.mkdir()
    empty_pkg = tmp_path / "package_skills"
    empty_pkg.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_mc)
    _set_edition_roots(monkeypatch, empty_pkg)

    class _FakeConfig:
        class skills:  # noqa: N801
            extra_paths = [str(extra_root)]

    with patch.object(_shared.KiroCrewConfig, "load", return_value=_FakeConfig()):
        resolved = _shared._resolve_skill_root("custom-skill", _FakeState())
    assert resolved == (extra_root / "custom-skill").resolve()


def test_resolve_skill_root_rejects_tilde_prefix(tmp_path, monkeypatch):
    # ``~`` is not caught by the top-level guard (which only checks ``/``),
    # so the else-branch must reject it before probing.
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "mc")
    _set_edition_roots(monkeypatch, tmp_path / "pkg")

    assert _shared._resolve_skill_root("~", _FakeState()) is None
    assert _shared._resolve_skill_root("~root/.ssh", _FakeState()) is None


def test_resolve_skill_root_extra_paths_take_precedence_over_edition(tmp_path, monkeypatch):
    # Same skill name in BOTH an extra path and an edition root must resolve to
    # the extra path, matching SkillsLoader.load_skill() precedence
    # (kirocrew -> extra_paths -> edition roots).
    extra_root = tmp_path / "extra_skills"
    (extra_root / "dup-skill").mkdir(parents=True)
    (extra_root / "dup-skill" / "SKILL.md").write_text("# extra", encoding="utf-8")

    pkg_root = tmp_path / "package_skills"
    (pkg_root / "dup-skill").mkdir(parents=True)
    (pkg_root / "dup-skill" / "SKILL.md").write_text("# package", encoding="utf-8")

    empty_mc = tmp_path / "kirocrew_skills"
    empty_mc.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_mc)
    _set_edition_roots(monkeypatch, pkg_root)

    class _FakeConfig:
        class skills:  # noqa: N801
            extra_paths = [str(extra_root)]

    with patch.object(_shared.KiroCrewConfig, "load", return_value=_FakeConfig()):
        resolved = _shared._resolve_skill_root("dup-skill", _FakeState())
    assert resolved == (extra_root / "dup-skill").resolve()


# ── _match_package_row: exact key wins, ambiguous leaf refuses ──


def test_package_row_matched_by_exact_key():
    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [
        {"key": "package/SomePkg/shared-skill", "name": "shared-skill", "path": "/a/SKILL.md"},
        {"key": "package/shared-skill", "name": "shared-skill", "path": "/b/SKILL.md"},
    ]
    row = _match_package_row(rows, "package/shared-skill", "shared-skill")
    assert row is not None and row["path"] == "/b/SKILL.md"


def test_ambiguous_leaf_name_refuses_rather_than_serving_the_wrong_file(caplog):
    """A key that names neither file must not resolve to an arbitrary one.

    ``name`` is a LEAF comparison, so two rows can share it under different
    parents while the requested key matches no row's ``key`` at all. There is no
    correct pick in that case, and serving one anyway returns another skill's
    SKILL.md under a 200 — which a reader has no way to notice. Refusing is the
    only honest answer.
    """
    import logging

    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [
        {"key": "one/shared-skill", "name": "shared-skill", "path": "/a/SKILL.md"},
        {"key": "two/shared-skill", "name": "shared-skill", "path": "/b/SKILL.md"},
    ]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.prompts"):
        assert _match_package_row(rows, "package/shared-skill", "shared-skill") is None
    assert any("refusing to guess" in r.getMessage() for r in caplog.records)


def test_unique_leaf_name_still_matches_for_editions_that_key_differently():
    """An edition may key rows without the ``package/`` prefix.

    Dropping the leaf leg outright would break it, so the fallback stays — gated
    on being unambiguous.
    """
    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [{"key": "AIPowerUser/agent-builder", "name": "agent-builder", "path": "/x"}]
    row = _match_package_row(rows, "package/agent-builder", "agent-builder")
    assert row is not None and row["path"] == "/x"


def test_no_match_is_quiet_while_ambiguity_warns(caplog):
    """A plain miss must NOT log — only a genuine ambiguity does.

    Both cases return ``None``, so the return value alone cannot tell them apart.
    Warning on every miss would make the signal worthless: the dashboard requests
    keys that legitimately do not exist, and the log has to stay readable for the
    collision it is actually there to report.
    """
    import logging

    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [{"key": "package/other", "name": "other", "path": "/x"}]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.prompts"):
        assert _match_package_row(rows, "package/missing", "missing") is None
    assert caplog.records == [], [r.getMessage() for r in caplog.records]


def test_exact_relative_path_beats_a_nested_leaf_of_the_same_name(tmp_path, monkeypatch):
    """A ``package/<rel>`` key addresses ``<root>/<rel>``, not a same-named leaf.

    Both layouts are supported, so with ``<root>/shared-skill`` AND
    ``<root>/SomePkg/shared-skill`` present the key ``shared-skill`` must resolve to the
    first. Without a precedence order between the two patterns the answer is
    whichever the filesystem happens to yield — another skill's content served
    under a 200.
    """
    root = tmp_path / "package_skills"
    exact = root / "shared-skill"
    exact.mkdir(parents=True)
    (exact / "SKILL.md").write_text("# exact", encoding="utf-8")
    nested = root / "SomePkg" / "shared-skill"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# nested", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    assert _shared._resolve_package_skill_path("shared-skill") == exact / "SKILL.md"


def test_same_relative_path_in_two_roots_refuses_to_guess(tmp_path, monkeypatch, caplog):
    """Two packages bundling the same relative path is unaddressable, not a pick.

    For a ``packages/<Pkg>/<version>/skills`` layout the package name lives in
    the ROOT, so it is absent from the key and both files claim ``shared-skill``. This
    key grammar cannot express which one is meant, so there is no correct answer
    to return — and picking one serves the other package's content under a 200.
    Failing closed with a log is the only honest answer.
    """
    import logging

    root_a = tmp_path / "p1" / "skills"
    root_b = tmp_path / "p2" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers._shared"):
        assert _shared._resolve_package_skill_path("shared-skill") is None
    assert any("refusing to guess" in r.getMessage() for r in caplog.records)


def test_one_skill_reachable_through_two_roots_still_resolves(tmp_path, monkeypatch):
    """A symlink alias is NOT an ambiguity — only two distinct FILES are.

    An edition may advertise both a directory and a symlink into it, so the same
    SKILL.md is reachable twice. Comparing unresolved paths would read that as a
    collision and 404 a skill that exists.
    """
    real = tmp_path / "real_skills"
    (real / "shared-skill").mkdir(parents=True)
    (real / "shared-skill" / "SKILL.md").write_text("# one", encoding="utf-8")
    alias = tmp_path / "alias_skills"
    alias.symlink_to(real, target_is_directory=True)
    _set_edition_roots(monkeypatch, real, alias)

    resolved = _shared._resolve_package_skill_path("shared-skill")
    assert resolved is not None
    assert resolved.resolve() == (real / "shared-skill" / "SKILL.md").resolve()


def test_nested_leaf_still_resolves_when_unambiguous(tmp_path, monkeypatch):
    """The leaf layout an edition may key by must keep working."""
    root = tmp_path / "package_skills"
    nested = root / "Pkg" / "agent-builder"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("# nested", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    assert _shared._resolve_package_skill_path("agent-builder") == nested / "SKILL.md"


def test_symlink_loop_does_not_raise(tmp_path, monkeypatch):
    """A looping symlink must not 500 the request.

    ``Path.resolve()`` raises ``RuntimeError`` — NOT ``OSError`` — on a symlink
    loop (verified on 3.10 and 3.12), and ``glob`` yields a looping ``SKILL.md``
    because a literal pattern matches the dirent without following it. Catching
    only ``OSError`` let that escape as a 500 on a browser-triggered request.
    """
    root = tmp_path / "package_skills"
    looping = root / "loop"
    looping.mkdir(parents=True)
    # a -> b -> a, then SKILL.md -> a, so resolve() sees a cycle.
    (looping / "a").symlink_to("b")
    (looping / "b").symlink_to("a")
    (looping / "SKILL.md").symlink_to("a")
    good = root / "fine"
    good.mkdir()
    (good / "SKILL.md").write_text("# ok", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    # The unresolvable entry is skipped, not fatal.
    assert _shared._resolve_package_skill_path("loop") is None
    assert _shared._resolve_package_skill_path("fine") == good / "SKILL.md"


def test_symlink_loop_root_does_not_break_key_enumeration(tmp_path, monkeypatch):
    """Same for an advertised ROOT that is a symlink loop.

    The root still gets a ``package/`` key — an unresolvable root is left out of
    the dedupe comparison rather than dropped, so this stays a pure crash fix and
    keeps enumerating every root it is handed.
    """
    data_home = tmp_path / "kirocrew_skills"
    data_home.mkdir()
    loop_root = tmp_path / "loop_root"
    loop_root.symlink_to(tmp_path / "loop_other")
    (tmp_path / "loop_other").symlink_to(loop_root)

    monkeypatch.setattr(_shared, "skills_dir", lambda: data_home)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, loop_root)

    pairs = _shared._skill_key_roots(_FakeState())

    assert loop_root in [r for prefix, r in pairs if prefix == "package/"]


def test_canonical_root_never_answers_a_package_key(tmp_path, monkeypatch):
    """A ``package/`` request must not be served from a root the core owns.

    ``extra_skills()`` advertises ``~/.kiro/skills`` and the data home so the
    LOADER indexes them. Searching them here too lets ``package/foo`` return the
    user's OWN editable skill under a read-only package identity — and it makes
    resolution disagree with enumeration, which deliberately excludes those roots.
    """
    kiro_user = tmp_path / ".kiro" / "skills"
    (kiro_user / "foo").mkdir(parents=True)
    (kiro_user / "foo" / "SKILL.md").write_text("# user's own", encoding="utf-8")
    data_home = tmp_path / "kirocrew_skills"
    (data_home / "bar").mkdir(parents=True)
    (data_home / "bar" / "SKILL.md").write_text("# data home", encoding="utf-8")

    monkeypatch.setattr(_shared, "skills_dir", lambda: data_home)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, kiro_user, data_home)

    assert _shared._resolve_package_skill_path("foo") is None
    assert _shared._resolve_package_skill_path("bar") is None


def test_package_root_wins_over_a_canonical_root_with_the_same_leaf(tmp_path, monkeypatch):
    """The concrete collision: same leaf in a canonical root and a package root.

    The exact-relative-path tier would otherwise match the canonical root's copy
    and shadow the package skill the key actually names.
    """
    kiro_user = tmp_path / ".kiro" / "skills"
    (kiro_user / "shared-skill").mkdir(parents=True)
    (kiro_user / "shared-skill" / "SKILL.md").write_text("# user's own", encoding="utf-8")
    pkg_root = tmp_path / "package_skills"
    pkg_skill = pkg_root / "Pkg" / "shared-skill"
    pkg_skill.mkdir(parents=True)
    (pkg_skill / "SKILL.md").write_text("# package", encoding="utf-8")

    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, kiro_user, pkg_root)

    assert _shared._resolve_package_skill_path("shared-skill") == pkg_skill / "SKILL.md"


def test_enumeration_and_resolution_agree_on_package_territory(tmp_path, monkeypatch):
    """The invariant behind the shared helper.

    Every root the catalog offers under ``package/`` must be one the resolver
    searches, and vice versa. If the two lists drift, the catalog either offers a
    key the resolver refuses or the resolver answers from a root the catalog
    never listed.
    """
    kiro_user = tmp_path / ".kiro" / "skills"
    kiro_user.mkdir(parents=True)
    data_home = tmp_path / "kirocrew_skills"
    data_home.mkdir()
    pkg_root = tmp_path / "package_skills"
    pkg_root.mkdir()

    monkeypatch.setattr(_shared, "skills_dir", lambda: data_home)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, kiro_user, data_home, pkg_root)

    enumerated = [
        r.resolve() for prefix, r in _shared._skill_key_roots(_FakeState()) if prefix == "package/"
    ]
    searched = [r.resolve() for r in _shared._edition_package_roots()]

    assert enumerated == searched == [pkg_root.resolve()]


# ── _skill_key_roots: no ghost package/ keys ──


def test_edition_root_already_keyed_elsewhere_is_not_re_added_as_package(tmp_path, monkeypatch):
    """``extra_skills()`` advertises the data home and ``~/.kiro/skills`` too.

    The loader needs those roots indexed, but the core already keys them as
    unprefixed and ``kiro-user/``. Re-adding them under ``package/`` gives one
    file two catalog keys, and the ``package/`` one presents a user's OWN
    editable skill as a read-only package skill.
    """
    data_home = tmp_path / "kirocrew_skills"
    data_home.mkdir()
    kiro_user = tmp_path / ".kiro" / "skills"
    kiro_user.mkdir(parents=True)
    pkg_only = tmp_path / "package_skills"
    pkg_only.mkdir()

    monkeypatch.setattr(_shared, "skills_dir", lambda: data_home)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, pkg_only, data_home, kiro_user)

    pairs = _shared._skill_key_roots(_FakeState())

    package_roots = [r.resolve() for prefix, r in pairs if prefix == "package/"]
    assert package_roots == [pkg_only.resolve()]
    # And the roots the core owns are still enumerated under their own prefixes.
    assert data_home.resolve() in [r.resolve() for prefix, r in pairs if prefix == ""]
    assert kiro_user.resolve() in [r.resolve() for prefix, r in pairs if prefix == "kiro-user/"]

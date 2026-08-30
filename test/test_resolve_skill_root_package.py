"""Regression tests for _resolve_skill_root edition-root resolution.

Edition-contributed skill roots now come from the CPP seam
``McpToolingProvider.extra_skills()`` (public Default ``[]``) rather than a
a hardcoded edition path; tests patch ``DefaultMcpToolingProvider.extra_skills``
to inject a root.
"""

import os
import sys
from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers._shared as _shared

#: The qualifier grammar exists only where a read can serve a qualified key: the enumerator
#: refuses to mint one otherwise, rather than listing a row every read path would refuse.
from kiro_crew import pinned_fs as _pinned_fs_for_marks
from kiro_crew.platform.defaults import DefaultMcpToolingProvider

_needs_pinned_walk = pytest.mark.skipif(
    not _pinned_fs_for_marks.supports_pinned_walk(),
    reason="no descriptor walk, so no qualified key is minted to assert about",
)


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
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots))


def _key_safe(qualifier: str) -> bool:
    """Whether *qualifier* survives its own key's parse, asserted DIRECTLY.

    The derivation is a lowercase-hex digest, so this states the property the resolver
    depends on rather than routing through a production predicate: nothing here can be
    read as the key separator, as glob pattern syntax, as a traversal element, or as
    more than one path segment. Asserted here in the test rather than in production
    because the resolver needs no per-value filter -- it requires equality with
    a derived digest, and any value failing these checks equals no digest and so
    resolves to nothing on its own.
    """
    return bool(
        qualifier
        and _shared._SKILL_KEY_QUALIFIER_SEP not in qualifier
        and not any(c in qualifier for c in _shared._GLOB_CHARS)
        and ".." not in qualifier
        and "/" not in qualifier
        and "\\" not in qualifier
        and not qualifier.startswith((".", "~"))
    )


# ``:`` is a legal filename character on POSIX and reserved on Windows (NTFS reads
# it as an alternate-data-stream separator, so mkdir raises WinError 267). The
# behaviour these tests pin — a skill directory whose own name carries the key
# separator — is therefore unreachable on Windows, and the production branches that
# handle it simply never fire there.
_COLON_IN_FILENAME_OK = pytest.mark.skipif(
    sys.platform == "win32", reason="':' is reserved in Windows filenames"
)

# ``*`` is reserved on Windows for the same class of reason as ``:`` above: the Win32
# path syntax rejects it, so ``mkdir`` raises WinError 123 ("the filename, directory
# name, or volume label syntax is incorrect") before any assertion runs. On POSIX it is
# an ordinary filename character, which is exactly why a directory literally named
# ``**`` is a reachable phantom-row case worth pinning there — and why the production
# branch that omits such a rel simply never fires on Windows.
_GLOB_STAR_IN_FILENAME_OK = pytest.mark.skipif(
    sys.platform == "win32", reason="'*' is reserved in Windows filenames"
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


def test_two_rows_sharing_the_requested_key_refuse_rather_than_serving_the_first(caplog):
    """The exact-key leg was ungated, so a duplicate key served whichever row came first.

    The leaf leg below already refuses an ambiguous pick, but the key leg returned on the
    first match. Two rows listed under ONE key is exactly the collision this change exists
    for, and answering 200 with an arbitrary one of them is the reader-cannot-notice failure
    the leaf guard was written to prevent -- reached through the other leg.
    """
    import logging

    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [
        {"key": "package/shared-skill", "name": "shared-skill", "path": "/a/SKILL.md"},
        {"key": "package/shared-skill", "name": "shared-skill", "path": "/b/SKILL.md"},
    ]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.prompts"):
        assert _match_package_row(rows, "package/shared-skill", "shared-skill") is None
    assert any("refusing to guess" in r.getMessage() for r in caplog.records)

    # Non-vacuity: ONE row under that key still resolves, so the guard is about duplication
    # rather than the key leg having been disabled.
    solo = [{"key": "package/shared-skill", "name": "shared-skill", "path": "/b/SKILL.md"}]
    row = _match_package_row(solo, "package/shared-skill", "shared-skill")
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


# ── package/<qualifier>:<rel> — addressing one of several colliding copies ──


def _two_colliding_roots(tmp_path, rel="shared-skill"):
    """Two package roots that both bundle *rel*, as two bundles of one skill do."""
    root_a = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    root_b = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    for root, body in ((root_a, "# from PkgA"), (root_b, "# from PkgB")):
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(body, encoding="utf-8")
    return root_a, root_b


def test_split_package_skill_key_leaves_an_unqualified_key_untouched():
    """No separator means no qualifier — the pre-existing grammar, verbatim.

    This is what keeps the change additive: every key emitted before it existed
    still reaches the same code path with the same relative path.
    """
    assert _shared._split_package_skill_key("shared-skill") == (None, "shared-skill")
    assert _shared._split_package_skill_key("SomePkg/shared-skill") == (
        None,
        "SomePkg/shared-skill",
    )


def test_split_package_skill_key_ignores_a_half_empty_qualifier():
    """A stray separator degrades to "unqualified", never to an empty glob.

    ``:shared-skill`` with an empty qualifier would otherwise filter every
    candidate out, and ``shared-skill:`` would glob ``/SKILL.md`` off the root.
    """
    assert _shared._split_package_skill_key(":shared-skill") == (None, ":shared-skill")
    assert _shared._split_package_skill_key("shared-skill:") == (None, "shared-skill:")


def test_a_colon_named_directory_is_not_mistaken_for_a_qualifier():
    """A skill directory legitimately containing ``:`` must still key to its own path.

    Reserving the separator would otherwise 404 such a directory with no fallback: the key
    parsed as ``qualifier=<dir-prefix>`` and resolved to nothing. Only the minted SHAPE -- a
    fixed-width lowercase hex digest -- is read as a qualifier, so the reservation costs
    nothing to a pre-existing name.
    """
    width = _shared._ROOT_IDENTITY_DIGEST_BYTES * 2
    for literal in ("weird:name", "Notes:2026/SKILL.md", "a:b", "A" * width + ":tool"):
        assert _shared._split_package_skill_key(literal) == (None, literal), literal
    # Non-vacuity: a real minted qualifier still splits, so the guard did not disable the
    # grammar it protects.
    minted = "0" * width
    assert _shared._split_package_skill_key(f"{minted}:shared-skill") == (
        minted,
        "shared-skill",
    )


def test_qualifier_addresses_each_of_two_colliding_copies(tmp_path, monkeypatch):
    """The capability: one relative path bundled twice becomes addressable.

    Unqualified, this key names two distinct files and the resolver refuses (see
    ``test_same_relative_path_in_two_roots_refuses_to_guess``) — correctly, since
    the package name lives in the ROOT for a ``packages/<Pkg>/<event>/skills``
    layout and so cannot appear in the relative path. A qualifier supplies that
    missing root segment, and each copy resolves to its OWN file.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    assert (
        _shared._resolve_package_skill_path("shared-skill", qualifier=_q(root_a))
        == root_a / "shared-skill" / "SKILL.md"
    )
    assert (
        _shared._resolve_package_skill_path("shared-skill", qualifier=_q(root_b))
        == root_b / "shared-skill" / "SKILL.md"
    )


def test_qualifier_for_a_root_without_the_path_refuses_rather_than_substituting(
    tmp_path, monkeypatch
):
    """A qualifier is checked even when only ONE candidate exists.

    Skipping the check when a tier holds a single candidate would make
    ``package/PkgB:solo-skill`` serve ``PkgA``'s copy under a 200 — the same
    silent wrong-content failure the unqualified path fails closed to avoid.

    With one holder there is no collision, so enumeration mints the UNQUALIFIED
    key ``package/solo-skill`` and offers no qualified spelling at all. Resolution
    mirrors that: neither qualifier answers, and the unqualified key still does.
    Accepting ``PkgA:solo-skill`` here would be resolving a key the catalogue never
    listed, which is the surface a stale qualifier slides along (see
    ``test_a_stale_qualifier_never_slides_onto_another_roots_copy``).
    """
    root_a = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root_a / "solo-skill").mkdir(parents=True)
    (root_a / "solo-skill" / "SKILL.md").write_text("# only PkgA", encoding="utf-8")
    root_b = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    root_b.mkdir(parents=True)
    _set_edition_roots(monkeypatch, root_a, root_b)

    # The substitution this test exists for: PkgB must never be handed PkgA's file.
    assert _shared._resolve_package_skill_path("solo-skill", qualifier="PkgB") is None
    # And neither is PkgA's own name a key here, because none was minted.
    assert _shared._resolve_package_skill_path("solo-skill", qualifier="PkgA") is None
    # Positive control: the key that IS catalogued resolves, so the refusals above
    # are discriminating rather than the file being unreachable.
    assert _shared._resolve_package_skill_path("solo-skill") is not None


def test_a_stale_qualifier_never_slides_onto_another_roots_copy(tmp_path, monkeypatch):
    """The uninstall/re-install sequence: a stale key must refuse, not substitute.

    Mint ``package/PkgA:<rel>`` while ``PkgA``'s root is installed, uninstall it, then
    install a DIFFERENT root that also happens to carry a segment named ``PkgA``. A
    membership test (``qualifier in root.parts``) accepts the new root, so the stale
    key answers 200 with ANOTHER bundle's content -- and the catalogue meanwhile keys
    that root by whatever the derivation yields for it now, so ``/tree`` lists the file
    under a different key than the one ``detail`` answered.

    Resolution therefore re-derives the qualifier instead of testing membership: a
    qualified key resolves to the copy the catalogue would list it for, or to nothing.
    """
    rel = "shared-skill"

    def root_with(rel_dir: str, body: str):
        root = tmp_path / rel_dir
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(body, encoding="utf-8")
        return root

    r1 = root_with("x/PkgA/skills", "# PkgA content")
    r2 = root_with("x/PkgB/skills", "# PkgB content")

    # Phase 1: PkgA and PkgB collide, so 'PkgA' is the derived qualifier and the key
    # resolves to PkgA's own copy.
    _set_edition_roots(monkeypatch, r1, r2)
    stale = _q(r1)
    assert _shared._root_identity_token(r1) == stale
    assert _shared._resolve_package_skill_path(rel, None, stale) == r1 / rel / "SKILL.md"

    # Phase 2: PkgA's root is uninstalled; a different root carrying a 'PkgA' segment
    # arrives. The catalogue now keys that root by 'y', NOT by 'PkgA'.
    r3 = root_with("y/PkgA/deep/skills", "# SOMEONE ELSE's content")
    _set_edition_roots(monkeypatch, r2, r3)
    assert "PkgA" in r3.parts, "the sequence needs the new root to carry the segment"
    assert _shared._root_identity_token(r3) == _q(r3)

    # The stale key must refuse rather than serve r3's content under PkgA's name.
    assert _shared._resolve_package_skill_path(rel, None, stale) is None

    # Positive control: the key the catalogue DOES list resolves, so the refusal is
    # about the stale qualifier and not about r3 being unreachable.
    got = _shared._resolve_package_skill_path(rel, None, _q(r3))
    assert got == r3 / rel / "SKILL.md", got


@_COLON_IN_FILENAME_OK
def test_a_half_empty_qualified_key_is_refused(tmp_path, monkeypatch):
    """A remainder still carrying the reserved separator is not a qualified key.

    ``_split_package_skill_key`` degrades a half-empty pair (``:foo``, ``foo:``) to
    "unqualified", which leaves the separator inside the REL half -- and a rel carrying
    the separator is omitted by enumeration, so admitting it at resolve time opens a
    colon-named skill the catalogue never listed.

    The colon-named directories are created ON DISK deliberately: without them the glob
    finds nothing and the refusal would hold for the wrong reason, so the test would
    pass on the unfixed tree and prove nothing.
    """
    root = tmp_path / "packages" / "PkgA" / "skills"
    for name in ("plain-skill", ":plain-skill", "plain-skill:"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    for remainder in (":plain-skill", "plain-skill:"):
        qualifier, rel = _shared._split_package_skill_key(remainder)
        # The split degrades to unqualified and the separator survives in the rel.
        assert qualifier is None, remainder
        assert _shared._SKILL_KEY_QUALIFIER_SEP in rel, remainder
        # The file the unfixed resolver would have served really is there, so the
        # refusal below is the guard and not an empty directory.
        assert (root / rel / "SKILL.md").is_file(), remainder
        assert _shared._resolve_package_skill_path(rel, None, qualifier) is None, remainder

    # Positive control: the same root answers the well-formed key, so the refusals
    # above are about the separator rather than an unreachable root.
    assert _shared._resolve_package_skill_path("plain-skill", None, None) is not None


def test_a_cross_tier_match_is_not_counted_as_a_collision(tmp_path, monkeypatch):
    """Two roots at DIFFERENT tiers are two catalogue rels, not one collision.

    The resolver globs an exact tier (``<rel>/SKILL.md``) and a lenient nested tier
    (``*/<rel>/SKILL.md``). The fold's collision unit is the walked REL, so a root
    holding ``tool`` and a root holding ``Pkg/tool`` are two separate, non-colliding
    entries and the catalogue keys BOTH unqualified. Counting the nested tier when
    building the holder set saw them as two holders of ``tool`` and answered
    ``package/<q>:tool`` -- a key ``/tree`` never lists, so it 404s for a reader who
    follows it from anywhere else.
    """
    a = tmp_path / "PkgA" / "skills"
    (a / "tool").mkdir(parents=True)
    (a / "tool" / "SKILL.md").write_text("# A exact", encoding="utf-8")
    b = tmp_path / "PkgB" / "skills"
    (b / "Nested" / "tool").mkdir(parents=True)
    (b / "Nested" / "tool" / "SKILL.md").write_text("# B nested", encoding="utf-8")

    # The catalogue has no collision here: two distinct rels, both unqualified.
    catalog = _package_catalog(tmp_path, monkeypatch, a, b)
    assert set(catalog) == {"package/tool", "package/Nested/tool"}, sorted(catalog)

    _set_edition_roots(monkeypatch, a, b)
    # So no qualified spelling of ``tool`` may resolve...
    for qualifier in ("PkgA", "PkgB", "Nested"):
        got = _shared._resolve_package_skill_path("tool", qualifier=qualifier)
        assert got is None, f"{qualifier} -> {got}"
    # ...while both keys the catalogue DOES list still resolve, so the refusals above
    # are about the manufactured collision and not about the files being unreachable.
    assert _shared._resolve_package_skill_path("tool") == a / "tool" / "SKILL.md"
    assert _shared._resolve_package_skill_path("Nested/tool") == b / "Nested" / "tool" / "SKILL.md"


@_GLOB_STAR_IN_FILENAME_OK
def test_a_rel_the_resolver_refuses_is_omitted_from_the_catalog(tmp_path, monkeypatch):
    """Enumeration holds the rel to the resolver's own predicate.

    A directory literally named ``**`` is legal on POSIX and IS walked, so it was
    catalogued and then refused by the resolver, which rejects a glob wildcard -- the
    phantom-row shape (offered but unresolvable) this grammar exists to remove, and the
    one divergence class left after colon-carrying rels were omitted. An anchor, a
    drive or a traversal element cannot be a dirent, so they cannot reach here.
    """
    root = tmp_path / "PkgA" / "skills"
    for name in ("**", "plain-tool"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    catalog = _package_catalog(tmp_path, monkeypatch, root)

    # The wildcard-named rel is omitted...
    assert "package/**" not in catalog, sorted(catalog)
    # ...and the ordinary sibling under the same root is still catalogued, so the
    # omission is that rel and not the whole walk.
    assert catalog.get("package/plain-tool") == root / "plain-tool" / "SKILL.md"

    # The binding invariant, stated the way the headline claims it: every key the
    # catalogue offers resolves.
    _set_edition_roots(monkeypatch, root)
    for key in catalog:
        rel = key[len(_shared.PACKAGE_KEY_PREFIX) :]
        qualifier, rest = _shared._split_package_skill_key(rel)
        assert _shared._resolve_package_skill_path(rest, None, qualifier) is not None, key


def test_qualifier_narrows_and_can_never_widen_the_search(tmp_path, monkeypatch):
    """A qualifier cannot reach a root the unqualified call would not search.

    The exclusion of core-owned roots is a security property, not a convenience:
    ``package/foo`` must never answer with the user's OWN editable skill. Naming
    that root's segment as a qualifier must not buy access to it.
    """
    kiro_user = tmp_path / ".kiro" / "skills"
    (kiro_user / "foo").mkdir(parents=True)
    (kiro_user / "foo" / "SKILL.md").write_text("# user's own", encoding="utf-8")
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path)
    _set_edition_roots(monkeypatch, kiro_user)

    assert _shared._resolve_package_skill_path("foo") is None
    assert _shared._resolve_package_skill_path("foo", qualifier=".kiro") is None
    assert _shared._resolve_package_skill_path("foo", qualifier="skills") is None


def test_qualifier_matching_several_colliding_roots_still_refuses(tmp_path, monkeypatch, caplog):
    """An ambiguity a qualifier fails to break is still a refusal.

    A segment present in more than one colliding root narrows nothing, and a partial
    narrowing must not be rounded up to a pick. Since resolution now re-derives the
    qualifier per root, such a segment is not the derived qualifier of EITHER root
    (it is shared, so the derivation skips it), and the refusal happens BEFORE the
    glob rather than after two candidates come back — earlier, for a stronger
    reason, and with no "guess" left to make.
    """
    root_a = tmp_path / "bundles" / "PkgA" / "skills"
    root_b = tmp_path / "bundles" / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    assert _shared._resolve_package_skill_path("shared-skill", qualifier="bundles") is None
    # The shared segment is the derived qualifier of neither root, which is WHY it
    # narrows to nothing rather than to both.
    assert _shared._root_identity_token(root_a) == _q(root_a)
    assert _shared._root_identity_token(root_b) == _q(root_b)
    # Positive control: the derived qualifiers DO resolve, each to its own copy, so
    # the refusal above is about this segment and not a blanket failure.
    for root, qualifier in (
        (root_a, _q(root_a)),
        (root_b, _q(root_b)),
    ):
        got = _shared._resolve_package_skill_path("shared-skill", qualifier=qualifier)
        assert got == root / "shared-skill" / "SKILL.md", qualifier


def test_qualifier_holding_a_path_separator_cannot_match(tmp_path, monkeypatch):
    """A qualifier is one SEGMENT, so a multi-segment value fails closed.

    ``parts`` never contains a joined value, which is why the segment test needs
    no sanitising of its own: a qualifier carrying ``/`` (or ``..``, or ``~``)
    can only miss.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    assert _shared._resolve_package_skill_path("shared-skill", qualifier="PkgA/eventId-1") is None
    assert _shared._resolve_package_skill_path("shared-skill", qualifier="..") is None


@_needs_pinned_walk
def test_qualifier_naming_an_aliasing_root_is_not_eaten_by_deduplication(tmp_path, monkeypatch):
    """An aliasing root is not a separate COPY, so it gets no key of its own.

    Deduplication keeps one ``(root, file)`` pair per resolved FILE, and the fold
    applies it BEFORE deriving qualifiers -- so a root holding a symlink to another
    root's copy is not a second copy and the catalogue never emits a key for it. The
    resolver derives against the same deduplicated set, which is what keeps an EMITTED
    key from 404ing: were the alias retained here, the ``others`` set the resolver
    derives against would differ from the fold's, and a qualifier the catalogue
    published could come back as a different segment and refuse.

    Nothing becomes unreachable. The alias resolves to the SAME file as the surviving
    root, so that content is served under the key the catalogue does list.

    The layout is one an edition legitimately installs, and one
    :func:`_collect_skills_under` documents as supported: ``PkgAlias`` carries the
    skill as a SYMLINK to ``PkgReal``'s copy. Both roots survive
    :func:`_edition_package_roots` because the roots themselves resolve
    differently. ``PkgThird`` holds a genuinely different file.
    """
    real = tmp_path / "PkgReal" / "skills"
    (real / "shared-skill").mkdir(parents=True)
    (real / "shared-skill" / "SKILL.md").write_text("# real", encoding="utf-8")
    alias = tmp_path / "PkgAlias" / "skills"
    alias.mkdir(parents=True)
    (alias / "shared-skill").symlink_to(real / "shared-skill", target_is_directory=True)
    third = tmp_path / "PkgThird" / "skills"
    (third / "shared-skill").mkdir(parents=True)
    (third / "shared-skill" / "SKILL.md").write_text("# third", encoding="utf-8")

    # What the CATALOGUE actually emits decides what may resolve.
    catalog = _package_catalog(tmp_path, monkeypatch, real, alias, third)
    assert set(catalog) == {
        f"package/{_q(real)}:shared-skill",
        f"package/{_q(third)}:shared-skill",
    }, sorted(catalog)

    _set_edition_roots(monkeypatch, real, alias, third)

    # Every emitted key resolves, to its own file -- the property that breaks if the
    # resolver derives against a different set than the fold did.
    assert (
        _shared._resolve_package_skill_path("shared-skill", qualifier=_q(real))
        == real / "shared-skill" / "SKILL.md"
    )
    assert (
        _shared._resolve_package_skill_path("shared-skill", qualifier=_q(third))
        == third / "shared-skill" / "SKILL.md"
    )
    # The alias spelling is not a catalogued key, so it refuses rather than answering
    # under a name /tree never listed. Its content is not lost: it is the same file
    # PkgReal's key serves.
    assert _shared._resolve_package_skill_path("shared-skill", qualifier=_q(alias)) is None
    assert (alias / "shared-skill" / "SKILL.md").resolve() == (
        real / "shared-skill" / "SKILL.md"
    ).resolve()


def test_the_package_prefix_literal_lives_in_exactly_one_place():
    """The prefix constant exists to stop drift, so no module may respell it.

    A constant applied to only some of the sites that read the prefix buys nothing
    — the remaining literal is free to drift, which is the failure the constant was
    introduced to prevent. Two modules parse a ``package/`` key: ``_shared`` (the
    resolver and key enumeration) and ``prompts`` (the detail endpoint). Only the
    definition may spell it out.

    Matches the quoted string, not the double-backtick prose form, so a docstring
    describing the grammar is unaffected.
    """
    from pathlib import Path as _Path

    handlers = _Path(_shared.__file__).parent
    literal = '"package/"'
    spelled: list[str] = []
    for module in ("_shared.py", "prompts.py"):
        text = (handlers / module).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if literal in line:
                spelled.append(f"{module}:{lineno}: {line.strip()}")

    assert spelled == [
        f'_shared.py:{_prefix_definition_line()}: PACKAGE_KEY_PREFIX = "package/"',
    ], spelled


def _prefix_definition_line() -> int:
    """Line number of ``PACKAGE_KEY_PREFIX``'s definition, so the guard is not
    pinned to a line number that shifts on every unrelated edit above it."""
    from pathlib import Path as _Path

    text = (_Path(_shared.__file__).parent / "_shared.py").read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith("PACKAGE_KEY_PREFIX = "):
            return lineno
    raise AssertionError("PACKAGE_KEY_PREFIX definition not found in _shared.py")


def _rel_shadowing_a_root_name(tmp_path, monkeypatch):
    """Two roots bundling rel ``PkgA/foo`` — the rel's own first segment names a
    root. Returns ``(root_a, root_b)``."""
    root_a = tmp_path / "PkgA" / "skills"
    root_b = tmp_path / "PkgB" / "skills"
    for root, body in ((root_a, "# PkgA"), (root_b, "# PkgB")):
        (root / "PkgA" / "foo").mkdir(parents=True)
        (root / "PkgA" / "foo" / "SKILL.md").write_text(body, encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)
    return root_a, root_b


def test_a_rel_shadowing_a_root_name_resolves_to_its_own_copy(tmp_path, monkeypatch):
    """When the rel shadows a root name, BOTH copies are addressable and neither slides.

    A segment-derived qualifier cannot be derived when the only candidate is also a
    segment of the relative path (rel ``PkgA/foo`` under roots ``PkgA`` and ``PkgB``),
    because the key ``package/PkgA:PkgA/foo`` has one token meaning a root to the
    resolver and a directory to a reader. root_a would derive nothing, and the
    all-or-nothing rule would drop BOTH copies.

    A digest cannot be confused with a rel segment, so both are addressable. What must
    still hold is the fail-closed direction: a NON-canonical qualifier -- one naming a
    real segment of a real root -- must not resolve, because the catalogue never offered
    it and answering it is the surface a stale key slides along.
    """
    root_a, root_b = _rel_shadowing_a_root_name(tmp_path, monkeypatch)

    # Both derive now, and to different values.
    q_a, q_b = _shared._root_identity_token(root_a), _shared._root_identity_token(root_b)
    assert q_a is not None and q_b is not None and q_a != q_b

    # Each canonical qualifier serves its OWN copy, never the sibling's -- the
    # wrong-content shape this file fails closed against.
    for root, q in ((root_a, q_a), (root_b, q_b)):
        got = _shared._resolve_package_skill_path("PkgA/foo", qualifier=q)
        assert got == (root / "PkgA" / "foo" / "SKILL.md").resolve(), (root, got)

    # The segment spellings the old grammar would have used still resolve to nothing.
    assert _shared._resolve_package_skill_path("PkgA/foo", qualifier="PkgA") is None
    assert _shared._resolve_package_skill_path("PkgA/foo", qualifier="PkgB") is None
    # Positive control: both files genuinely exist, so the refusals above are about the
    # key grammar and not about a missing file.
    for root in (root_a, root_b):
        assert (root / "PkgA" / "foo" / "SKILL.md").is_file(), root


def test_qualifier_never_serves_another_roots_copy_when_its_own_is_gone(tmp_path, monkeypatch):
    """The wrong-content shape, which is worse than the refusal above.

    With only ``PkgB``'s copy left, exactly one candidate survives the glob and its
    ``parts`` still contain ``PkgA`` (from the rel), so the length-1 branch is taken
    and ``package/PkgA:PkgA/foo`` answers with ``PkgB``'s content under a 200. A
    reader who opened one bundle's skill and silently got another's has no way to
    notice — the failure this module fails closed everywhere else to avoid.
    """
    root_a, root_b = _rel_shadowing_a_root_name(tmp_path, monkeypatch)
    (root_a / "PkgA" / "foo" / "SKILL.md").unlink()

    resolved = _shared._resolve_package_skill_path("PkgA/foo", qualifier="PkgA")
    assert resolved is None, f"served {resolved} under PkgA's key"


@_needs_pinned_walk
def test_a_rel_shadowing_a_root_name_is_addressable_without_ambiguity(tmp_path, monkeypatch):
    """A rel that shadows a root's name costs the rows nothing, and stays unambiguous.

    With both roots bundling ``PkgA/tool``, a segment-derived qualifier would have
    ``PkgA`` as the only candidate for root_a -- absent from root_b, yet also a segment
    of the rel every copy carries. The key would read ``package/PkgA:PkgA/tool``, where
    the same token means a root to the resolver and a directory to a reader, so the
    derivation refused it and the all-or-nothing rule dropped BOTH copies: installed and
    invisible.

    A digest cannot collide with a rel segment, so the ambiguity is structurally absent
    rather than avoided by omitting. Both copies are addressable, and the ambiguous
    spelling is still not a key.
    """
    root_a = tmp_path / "PkgA" / "skills"
    root_b = tmp_path / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "PkgA" / "tool").mkdir(parents=True)
        (root / "PkgA" / "tool" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b)

    # The ambiguous spelling is absent, and the two digest keys are present instead.
    assert "package/PkgA:PkgA/tool" not in keys
    assert f"package/{_q(root_a)}:PkgA/tool" in keys, sorted(keys)
    assert f"package/{_q(root_b)}:PkgA/tool" in keys, sorted(keys)
    # And each resolves to its OWN copy, which is what makes the two keys distinct
    # rather than two spellings of one file.
    for key, skill_md in keys.items():
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve(), key
    assert len({str(v) for v in keys.values()}) == len(keys), keys


@_COLON_IN_FILENAME_OK
def test_a_stale_qualified_key_never_falls_back_onto_a_colon_named_skill(tmp_path, monkeypatch):
    """A qualified key that fails to resolve must 404, not find something else.

    Retrying the whole remainder verbatim looks harmless — it was there so a skill
    directory literally named ``foo:bar`` stayed reachable — but the remainder of a
    QUALIFIED key has exactly the same shape. So once ``PkgA`` stops carrying
    ``shared-skill``, ``package/PkgA:shared-skill`` falls through to a literal glob
    for ``PkgA:shared-skill``, and any OTHER root holding a directory by that name
    answers — serving a different bundle's content under ``PkgA``'s key.

    The two readings are indistinguishable from the key alone, so one of them has to
    lose. The separator is therefore RESERVED: there is no verbatim retry at all, and
    enumeration omits colon-carrying paths for the same reason, so the resolver agrees
    rather than guessing. Note this holds however the roots are arranged — the earlier
    root-set discriminator made the answer depend on which roots existed, which is
    exactly how a stale key could still be re-pointed.
    """
    gone = tmp_path / "PkgA" / "skills"
    gone.mkdir(parents=True)
    other = tmp_path / "PkgB" / "skills"
    (other / "PkgA:shared-skill").mkdir(parents=True)
    (other / "PkgA:shared-skill" / "SKILL.md").write_text("# PkgB's own", encoding="utf-8")
    _set_edition_roots(monkeypatch, gone, other)

    resolved = _shared._resolve_skill_root("package/PkgA:shared-skill", _FakeState())
    assert resolved is None, f"served {resolved} under PkgA's key"


@_COLON_IN_FILENAME_OK
def test_a_separator_carrying_path_is_omitted_when_a_root_carries_that_segment(
    tmp_path, monkeypatch, caplog
):
    """A separator-carrying path is omitted from the catalog, unconditionally.

    The separator is reserved, so ``package/foo:bar`` has exactly one reading — the
    qualified one — and a literal path of that name can never claim that key.
    Enumeration therefore omits such a path whether or not any root carries the
    leading segment. This case, where one does, is simply the one where listing it
    would have offered a key resolving to a DIFFERENT package's skill; the omission
    itself does not depend on that.
    """
    import logging

    contender = tmp_path / "foo" / "skills"
    (contender / "plain").mkdir(parents=True)
    (contender / "plain" / "SKILL.md").write_text("# plain", encoding="utf-8")
    other = tmp_path / "PkgOther" / "skills"
    (other / "foo:bar").mkdir(parents=True)
    (other / "foo:bar" / "SKILL.md").write_text("# colon", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers._shared"):
        keys = _package_catalog(tmp_path, monkeypatch, contender, other)

    assert list(keys) == ["package/plain"]
    assert any("separator" in r.getMessage() for r in caplog.records)
    # And the omitted key really is unreadable, so nothing is offered that 404s.
    assert _shared._resolve_skill_root("package/foo:bar", _FakeState()) is None


@pytest.mark.asyncio
async def test_detail_and_tree_agree_on_a_qualified_key(tmp_path, monkeypatch):
    """A qualified key must be OPENABLE, not just traversable.

    ``api_skill_detail`` does not go through :func:`_resolve_skill_root`; it matches a
    capability row by exact key, and its leaf fallback cannot match a qualified
    remainder. So a qualified row whose producer has not landed yet resolves its tree
    while detail 404s — the mirror image of the asymmetry this grammar exists to
    avoid, and worse than a clean omission because the skill looks present and
    unreadable. Detail now falls through to the same resolver the tree uses.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    # No capability manager rows exist, which is exactly today's state: nothing in
    # this repo emits a qualified key, so the row lookup cannot answer.
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: MagicMock(available=lambda: False))

    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    state = MagicMock(_slots={})
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.method = "GET"
    key = f"package/{_q(root_a)}:shared-skill"
    request.match_info = {"name": key}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    response = await _prompts.api_skill_detail(request)

    # The thesis is AGREEMENT between the two halves, not a status: where the platform
    # cannot pin, neither half may hand back another bundle's bytes.
    assert _shared._resolve_skill_root(key, _FakeState()) == (root_a / "shared-skill").resolve()
    if not _prompts.pinned_fs.supports_pinned_walk():
        assert response.status != 200, (
            "a platform that cannot pin must refuse to serve rather than answer 200 from "
            f"a name resolved after the check, got {response.status}"
        )
        assert (
            "# from PkgA" not in response.text
        ), "the file was served on a platform that cannot verify the root it came from"
        return
    assert response.status == 200, response.text
    assert "# from PkgA" in response.text


@pytest.mark.asyncio
@_COLON_IN_FILENAME_OK
async def test_detail_serves_the_package_copy_not_a_core_skill_of_that_literal_name(
    tmp_path, monkeypatch
):
    """Detail must answer a ``package/`` key from the package roots, like the tree does.

    ``load_skill`` joins the key onto a core root (``<dir>/<name>/SKILL.md``), so a
    core skill whose own relative path is literally ``package/<qualifier>:<rel>``
    answers first and the ``package/`` branches never run. Detail then serves that
    file while ``/tree`` resolves the packaged copy — one key naming two skills, and a
    spec written from the modal loads something the tree never showed.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: MagicMock(available=lambda: False))

    skills = MagicMock()
    key = f"package/{_q(root_a)}:shared-skill"
    # A core skill occupying the qualified key's exact string, as load_skill sees it.
    skills.load_skill = lambda name: ("# core impostor" if name == key else None)
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    state = MagicMock(_slots={})
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.method = "GET"
    request.match_info = {"name": key}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    response = await _prompts.api_skill_detail(request)

    # The core impostor must never win, on any platform -- that is this test's thesis and
    # it holds whether the packaged copy is then served or refused.
    assert "# core impostor" not in response.text
    # The tree half already answers from the package roots; detail must match it.
    assert _shared._resolve_skill_root(key, _FakeState()) == (root_a / "shared-skill").resolve()
    if _prompts.pinned_fs.supports_pinned_walk():
        assert "# from PkgA" in response.text
    else:
        assert response.status != 200, (
            "a platform that cannot pin must refuse the packaged copy rather than serve "
            f"it from a name resolved after the check, got {response.status}"
        )


@_needs_pinned_walk
def test_an_alias_beside_a_real_collision_emits_no_duplicate_row(tmp_path, monkeypatch):
    """Qualified rows are per DISTINCT file, so an alias must not add a second row.

    The two-distinct-files gate counts deduplicated files, but the loop that derives
    qualifiers iterates the RAW entries. A third root that is a symlink alias of one
    copy therefore earns its own qualified key pointing at a file another key already
    names — two catalog rows for one skill, which is the phantom-row shape inverted.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    # A THIRD distinct root whose entry is a symlink to PkgA's copy. Root-level
    # aliases are collapsed before the walk, so the alias has to be the skill
    # directory itself to reach the entries list at all.
    alias_root = tmp_path / "packages" / "PkgAlias" / "eventId-3" / "skills"
    alias_root.mkdir(parents=True)
    (alias_root / "shared-skill").symlink_to(root_a / "shared-skill", target_is_directory=True)
    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b, alias_root)

    values = [v.resolve() for v in keys.values()]
    assert len(values) == len(set(values)), f"duplicate file across keys: {keys}"
    assert len(keys) == 2, keys


@_COLON_IN_FILENAME_OK
def test_an_uninstalled_roots_stale_key_never_binds_to_another_package(tmp_path, monkeypatch):
    """Uninstalling a root must not re-point its keys at another package's skill.

    This is the hole the root-set discriminator left open, and it is distinct from the
    sibling test above where ``PkgA``'s root still EXISTS. Here ``PkgA`` is gone
    entirely — not advertised at all — so no installed root carries the segment
    ``PkgA``, the qualified reading was declared "structurally impossible", and the key
    fell back to a literal glob for ``PkgA:shared-skill``. ``PkgB`` holds a directory of
    exactly that name, so it answered: ``PkgA``'s key served ``PkgB``'s content.

    That is the whole hazard of deciding a key's meaning by the installed root set —
    the decision changes when roots come and go, and an uninstall is precisely when a
    key goes stale. With the separator reserved there is no second reading to fall back
    to, so the stale key fails closed.
    """
    other = tmp_path / "PkgB" / "skills"
    (other / "PkgA:shared-skill").mkdir(parents=True)
    (other / "PkgA:shared-skill" / "SKILL.md").write_text("# PkgB's own", encoding="utf-8")
    # PkgA is NOT advertised: its root is absent from the edition entirely.
    _set_edition_roots(monkeypatch, other)

    resolved = _shared._resolve_skill_root("package/PkgA:shared-skill", _FakeState())
    assert resolved is None, f"PkgA's stale key served {resolved}"

    # And the literal directory is not reachable under its own name either, so the
    # catalog cannot offer a key the resolver refuses.
    keys = _package_catalog(tmp_path, monkeypatch, other)
    assert "package/PkgA:shared-skill" not in keys, keys


@_COLON_IN_FILENAME_OK
def test_a_colon_named_skill_is_omitted_and_unresolvable(tmp_path, monkeypatch):
    """A skill whose own name carries the separator is omitted, and does not resolve.

    The separator is RESERVED, so ``package/foo:bar`` has exactly one reading — the
    qualified one — and a directory literally named ``foo:bar`` is unaddressable. The
    earlier design kept such a skill working by retrying the remainder verbatim when
    no installed root carried the segment, but that made a key's MEANING a function
    of the installed root set: uninstalling the root that carried the segment, or
    installing an unrelated bundle whose path happens to carry it, silently flipped an
    existing key between the two readings. Worse, the verbatim retry is what let a
    STALE qualified key fall through onto another bundle's directory of that literal
    name.

    Reserving the separator removes the ambiguity instead of adjudicating it. Both
    halves of the API must agree, so the catalog omits the key and the resolver
    refuses it — the same treatment already given to a collision no segment
    distinguishes.
    """
    root = tmp_path / "packages" / "PkgOnly" / "eventId-1" / "skills"
    (root / "foo:bar").mkdir(parents=True)
    (root / "foo:bar" / "SKILL.md").write_text("# colon named", encoding="utf-8")
    keys = _package_catalog(tmp_path, monkeypatch, root)

    assert "package/foo:bar" not in keys, keys
    assert _shared._resolve_skill_root("package/foo:bar", _FakeState()) is None


@pytest.mark.asyncio
async def test_package_detail_prefers_the_resolver_over_the_capability_row(tmp_path, monkeypatch):
    """A ``package/`` key whose row and resolver disagree is answered by NEITHER.

    The row branch once resolved a row's path and called ``Path.read_text`` straight
    inside the coroutine, so a large SKILL.md stalled the gateway loop; its read now
    runs on ``discovery_executor()`` like the resolver's. The row branch is retained
    as a FALLBACK for a row whose key is not its root-relative path.

    Serving the resolver's file here was a shadowing defect rather than a preference:
    the caller selected the row, so returning another root's bytes answers one identity
    with the other's content. The row is therefore consulted BEFORE the read, purely to
    detect the disagreement, and a decoy pointing elsewhere now fails closed.
    """
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgOnly" / "eventId-1" / "skills"
    (root / "shared-skill").mkdir(parents=True)
    (root / "shared-skill" / "SKILL.md").write_text("# from the package root", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    # A capability row that WOULD match this key exactly and points somewhere else.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "SKILL.md").write_text("# from the capability row", encoding="utf-8")
    mgr = MagicMock()
    mgr.available = lambda: True
    mgr.list_skills = AsyncMock(
        return_value=[{"key": "package/shared-skill", "path": str(decoy / "SKILL.md")}]
    )
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    state = MagicMock(_slots={})
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.method = "GET"
    request.match_info = {"name": "package/shared-skill"}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    response = await _prompts.api_skill_detail(request)

    assert "# from the capability row" not in response.text
    assert "# from the package root" not in response.text, "served under the row's identity"
    assert response.status == 404, response.text


@pytest.mark.asyncio
async def test_package_detail_opens_a_row_whose_key_is_not_its_rel_path(tmp_path, monkeypatch):
    """A listed skill keyed differently from its rel path must still open.

    The resolver globs a key's remainder against the installed roots, so it can only
    find a skill whose ROOT-RELATIVE PATH equals its key. An edition is free to list
    a row under some other key; for those the resolver returns ``None``, and if the
    exact-row lookup is absent the skill lists in the catalog and 404s when opened --
    present and unreadable.

    Here the file lives at ``<root>/actual-location/SKILL.md`` while the row is keyed
    ``package/listed-under-another-key``. Globbing that key finds nothing, so a 200
    proves the row fallback ran; the row must also actually have been listed.
    """
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgOnly" / "skills"
    (root / "actual-location").mkdir(parents=True)
    (root / "actual-location" / "SKILL.md").write_text("# the real body", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    key = "package/listed-under-another-key"
    # Precondition: the glob resolver genuinely cannot answer this key, so a 200
    # below cannot come from the resolver and must come from the row.
    assert _shared._resolve_skill_root(key, _FakeState()) is None

    mgr = MagicMock()
    mgr.available = lambda: True
    mgr.list_skills = AsyncMock(
        return_value=[{"key": key, "path": str(root / "actual-location" / "SKILL.md")}]
    )
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    state = MagicMock(_slots={})
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.method = "GET"
    request.match_info = {"name": key}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    response = await _prompts.api_skill_detail(request)

    assert mgr.list_skills.await_count == 1, "the exact-row fallback never ran"
    assert response.status == 200, response.text
    assert "# the real body" in response.text


@pytest.mark.asyncio
async def test_the_row_fallback_validates_the_path_off_the_event_loop(tmp_path, monkeypatch):
    """``validate_file_path`` must run on the pool, not on the coroutine.

    It canonicalizes the path, and on a network-mounted package root that call is itself
    the slow one — so validating here stalled the gateway even though the READ had already
    been offloaded. Asserted by thread identity rather than by timing, which would be
    flaky: the validation must observe a thread other than the one serving the request.
    """
    import asyncio as _asyncio
    import threading
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgOnly" / "skills"
    (root / "actual-location").mkdir(parents=True)
    (root / "actual-location" / "SKILL.md").write_text("# the real body", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    key = "package/listed-under-another-key"
    assert _shared._resolve_skill_root(key, _FakeState()) is None

    mgr = MagicMock()
    mgr.available = lambda: True
    mgr.list_skills = AsyncMock(
        return_value=[{"key": key, "path": str(root / "actual-location" / "SKILL.md")}]
    )
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)
    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    real_validate = _prompts.validate_file_path
    seen: list[int] = []

    def _recording_validate(raw):
        seen.append(threading.get_ident())
        return real_validate(raw)

    monkeypatch.setattr(_prompts, "validate_file_path", _recording_validate)

    state = MagicMock(_slots={})
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    request.method = "GET"
    request.match_info = {"name": key}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    loop_thread = threading.get_ident()
    response = await _prompts.api_skill_detail(request)

    # Vacuity guards: the fallback must have run and the validation must have been
    # reached, or the thread assertion below is satisfied by nothing.
    assert mgr.list_skills.await_count == 1, "the exact-row fallback never ran"
    assert response.status == 200, response.text
    assert seen, "validate_file_path was never called, so nothing was measured"
    assert _asyncio.get_running_loop() is not None
    assert (
        loop_thread not in seen
    ), "validate_file_path ran on the event-loop thread; its realpath must be offloaded"


@pytest.mark.asyncio
async def test_a_qualified_remainder_never_reaches_the_row_fallback(tmp_path, monkeypatch):
    """A key the resolver refused must 404, not be rescued by a colon-named row.

    The separator is reserved: the enumerator omits any rel carrying it, and the
    resolver refuses such a key, so ``/tree`` does not list it. The row fallback
    matches on whatever key an EDITION chose, though — so a row keyed
    ``package/Weird:Name`` was still served here, answering 200 for a key ``/tree``
    404s. That is the detail-versus-tree divergence the fallback's ordering was meant
    to avoid, reintroduced through the row path.

    Asserted on ``await_count`` rather than only the status, because a 404 could also
    come from the row simply not matching -- this pins that the inventory is never
    consulted at all for a qualified remainder.
    """
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgOnly" / "skills"
    root.mkdir(parents=True)
    _set_edition_roots(monkeypatch, root)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    key = "package/Weird:Name"
    # Precondition: the resolver genuinely cannot answer it, so the row is the only
    # thing that could have produced a 200.
    assert _shared._resolve_skill_root(key, _FakeState()) is None

    mgr = MagicMock()
    mgr.available = lambda: True
    mgr.list_skills = AsyncMock(
        return_value=[
            {"key": key, "name": "Weird:Name", "path": str(tmp_path / "any" / "SKILL.md")}
        ]
    )
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.method = "GET"
    request.match_info = {"name": key}
    request.headers = {}
    request.query = {}
    request.cookies = {}

    response = await _prompts.api_skill_detail(request)

    assert response.status == 404, response.text
    assert mgr.list_skills.await_count == 0, "a colon-named row was consulted"


def test_every_admitted_name_can_be_globbed_without_raising(tmp_path):
    """The predicate's job is that nothing it admits can make ``glob`` raise.

    That is the property, and it is host-relative: ``Path.glob`` rejects a pattern
    carrying a drive or a root, and what counts as a drive depends on the host. So the
    test asserts the consequence rather than a fixed verdict per name — a name legal
    here must be admitted AND globbable, and a non-relative one must be refused. On
    POSIX ``a:b`` and ``~:x`` are ordinary directory names; refusing them would strand
    the rows the enumerator creates for them.
    """
    always_refused = ("", "/x", "/:x", "../x", "..")
    for name in always_refused:
        assert _shared._names_a_relative_path(name) is False, name

    host_legal = ("shared-skill", "nested/shared-skill", "foo:bar", "a:b", "~:x")
    for name in host_legal:
        admitted = _shared._names_a_relative_path(name)
        if os.name == "nt":
            continue  # a drive-shaped name is correctly refused where drives exist
        assert admitted is True, name
        # The binding consequence: an admitted name must not make glob raise.
        list(tmp_path.glob(f"{name}/SKILL.md"))

    # Negative control: a refused name WOULD raise, so the guard is load-bearing
    # rather than decorative.
    with pytest.raises(NotImplementedError):
        list(tmp_path.glob("/:x/SKILL.md"))


def test_a_glob_wildcard_remainder_is_refused_rather_than_crashing(tmp_path, monkeypatch):
    """A caller-supplied glob metacharacter must 404, not raise and not match a sibling.

    ``package/Pkg:***`` reached the resolver's glob unvalidated, and ``Path.glob``
    raises ``ValueError: Invalid pattern: '**' can only be an entire path component``
    for a ``**`` that is not a whole component -- an unhandled exception in the
    handler, so HTTP 500 on a request the caller controls. The non-relative guard did
    not cover it: ``***`` carries no drive, no root and no ``..``, so it was admitted.

    Every OTHER spelling is a valid pattern but still not a NAME: ``**`` matches at any
    depth, ignoring the nest-depth bound, while ``*``, ``?`` and ``[`` match siblings --
    so one key resolves to whichever skill happens to match, or to several. An earlier
    revision admitted a single ``*`` or ``?`` to avoid stranding a row for a directory
    legally named that way on POSIX; ``_merge_package_walks`` now holds every walked rel
    to this same predicate, so the phantom row is impossible and the ambiguity is not
    worth carrying.
    """
    # Embedded -- invalid pattern, the crash. Everything else -- valid but a wildcard.
    for rel in ("***", "a**b", "deep/***/x", "**", "deep/**/x"):
        assert _shared._names_a_relative_path(rel) is False, rel
    for rel in ("star*name", "quer?name", "class[ab]", "nested/star*name"):
        assert _shared._names_a_relative_path(rel) is False, rel

    # Control: this is not a blanket refusal of punctuation. Characters glob does NOT
    # read as syntax stay admitted, so an ordinary POSIX name is unaffected.
    for rel in ("shared-skill", "nested/shared-skill", "under_score", "dot.name", "a+b"):
        assert _shared._names_a_relative_path(rel) is True, rel

    # The binding consequence, through the real resolver: the endpoint's path returns
    # None (its 404) instead of propagating an exception (its 500).
    root = tmp_path / "package_skills"
    (root / "Pkg" / "real-skill").mkdir(parents=True)
    (root / "Pkg" / "real-skill" / "SKILL.md").write_text("# real", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)
    for rel in ("***", "a**b", "real-skil?", "real-skil*"):
        assert _shared._resolve_package_skill_path(rel, None, None) is None, rel

    # Positive control: the same resolver still answers a well-formed remainder, so
    # the assertions above are not passing because everything now returns None.
    assert _shared._resolve_package_skill_path("Pkg/real-skill", None, None) is not None


@_needs_pinned_walk
def test_only_the_canonical_qualifier_resolves_not_any_unique_root_segment(tmp_path, monkeypatch):
    """A qualifier is the catalog-derived value, not any unique segment of the root.

    The documented layout is ``packages/<Pkg>/<eventId>/skills``, in which ``eventId-1``
    is a genuinely UNIQUE segment of one root -- so a membership contract would accept
    ``package/eventId-1:<rel>``. The catalogue does not offer that spelling: the
    derivation returns the root's identity digest, which is not a segment of the root at
    all. Answering
    ``eventId-1`` anyway would answer a key ``/tree`` never lists, and accepting any
    carried segment is exactly what would let a stale key bind to a different bundle once
    its own root was uninstalled.

    So the resolver accepts the canonical value only, and the docstring says so rather
    than promising "a segment the root carries".
    """
    a = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    b = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    for root in (a, b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root.parts[-3]}", encoding="utf-8")

    # What the catalogue emits is the contract.
    catalog = _package_catalog(tmp_path, monkeypatch, a, b)
    assert set(catalog) == {
        f"package/{_q(a)}:shared-skill",
        f"package/{_q(b)}:shared-skill",
    }, sorted(catalog)

    _set_edition_roots(monkeypatch, a, b)
    # The canonical qualifier resolves, to its own copy.
    assert (
        _shared._resolve_package_skill_path("shared-skill", qualifier=_q(a))
        == a / "shared-skill" / "SKILL.md"
    )
    # A unique-but-not-canonical segment does not, in either root's spelling.
    for qualifier in ("eventId-1", "eventId-2", "packages"):
        got = _shared._resolve_package_skill_path("shared-skill", qualifier=qualifier)
        assert got is None, f"{qualifier} -> {got}"


def test_a_sibling_matching_wildcard_key_cannot_resolve(tmp_path, monkeypatch):
    """``*`` and ``?`` in a key must not let one key open a DIFFERENT skill.

    Both are ordinary POSIX filename characters, so an earlier revision admitted them
    and only refused ``**``. But the resolver hands the remainder to ``Path.glob``, so
    ``tool?`` matches the sibling ``tools`` -- one key silently answering with another
    skill's content, under a 200. The enumerator holds each walked rel to the same
    predicate now, so refusing them cannot strand a catalogued row.

    The wildcards here match EXACTLY ONE directory on purpose. A pattern matching two
    would be refused by the ambiguity branch even without the predicate, so the test
    would pass on the unfixed tree and prove nothing -- the single match is what makes
    the wrong-content answer reachable.
    """
    root = tmp_path / "packages" / "PkgA" / "skills"
    for name in ("tools", "readme-skill"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    # Each of these matches ``tools`` and nothing else, so the unfixed resolver answered
    # 200 with that file for a key the catalogue never listed.
    for rel in ("tool?", "tool*", "tool[s]"):
        got = _shared._resolve_package_skill_path(rel, None, None)
        assert got is None, f"{rel} -> {got}"

    # Positive control: the literal names both resolve, so the refusals are about the
    # wildcards and not about the root being unreachable.
    for name in ("tools", "readme-skill"):
        assert _shared._resolve_package_skill_path(name, None, None) == root / name / "SKILL.md"


def test_a_skill_symlinked_out_of_its_root_is_refused(tmp_path, monkeypatch):
    """A skill dir symlinked OUTSIDE its root must not be served, or catalogued.

    ``Path.glob`` matches a symlinked directory's dirent, so ``<root>/leaked/SKILL.md``
    is yielded for a ``leaked`` that points anywhere on the filesystem. The path is
    LEXICALLY under the root, so neither the glob result nor a string-prefix test can
    see the escape -- the detail endpoint would then read a file outside the package
    territory. Containment is therefore asserted on the CANONICAL form of both sides.

    Every shape below escaped before the fix, including the UNQUALIFIED ones: the defect
    is in the tier globs, not in the qualified branch, so a fix confined to qualified
    lookup would have left three of these four reachable.

    The negative control is the same shape pointing INSIDE the root -- an edition
    advertising a directory and a symlink into it is legitimate and stays resolvable, so
    a test that passed by refusing symlinks wholesale would fail here.
    """
    outside = tmp_path / "outside"
    (outside / "stolen").mkdir(parents=True)
    (outside / "stolen" / "SKILL.md").write_text("# OUTSIDE", encoding="utf-8")

    # 1. Unqualified: the skill directory itself is the symlink.
    one = tmp_path / "packages" / "PkgA" / "skills"
    one.mkdir(parents=True)
    (one / "leaked").symlink_to(outside / "stolen", target_is_directory=True)
    _set_edition_roots(monkeypatch, one)
    assert _shared._resolve_package_skill_path("leaked") is None
    # ... and the catalogue does not list it either, so this is not a phantom row.
    assert _package_catalog(tmp_path, monkeypatch, one) == {}

    # 2. An INTERMEDIATE directory is the symlink, not the leaf.
    two = tmp_path / "two" / "PkgA" / "skills"
    two.mkdir(parents=True)
    (two / "hop").symlink_to(outside, target_is_directory=True)
    _set_edition_roots(monkeypatch, two)
    assert _shared._resolve_package_skill_path("hop/stolen") is None

    # 3. The NESTED (``*/<name>``) tier, a separate glob call site.
    three = tmp_path / "three" / "PkgA" / "skills"
    (three / "Pkg").mkdir(parents=True)
    (three / "Pkg" / "stolen").symlink_to(outside / "stolen", target_is_directory=True)
    _set_edition_roots(monkeypatch, three)
    assert _shared._resolve_package_skill_path("stolen") is None

    # 4. Qualified: the winning root's copy is the escaping symlink. The qualified key
    #    must not become a way around the check that the unqualified key hits.
    a = tmp_path / "q" / "PkgA" / "skills"
    b = tmp_path / "q" / "PkgB" / "skills"
    a.mkdir(parents=True)
    (b / "shared-skill").mkdir(parents=True)
    (b / "shared-skill" / "SKILL.md").write_text("# PkgB", encoding="utf-8")
    (a / "shared-skill").symlink_to(outside / "stolen", target_is_directory=True)
    _set_edition_roots(monkeypatch, a, b)
    assert _shared._resolve_package_skill_path("shared-skill", qualifier="PkgA") is None
    # PkgB's own copy is now the only holder, so it is keyed UNQUALIFIED -- which is
    # exactly the inverse property: the escaping copy is gone from both sides.
    assert set(_package_catalog(tmp_path, monkeypatch, a, b)) == {"package/shared-skill"}

    # NEGATIVE CONTROL: identical shape, pointing INSIDE the root.
    inside = tmp_path / "ok" / "PkgA" / "skills"
    (inside / "real-skill").mkdir(parents=True)
    (inside / "real-skill" / "SKILL.md").write_text("# INSIDE", encoding="utf-8")
    (inside / "alias").symlink_to(inside / "real-skill", target_is_directory=True)
    _set_edition_roots(monkeypatch, inside)
    got = _shared._resolve_package_skill_path("alias")
    assert got is not None, "an in-root symlink must still resolve"
    assert got.read_text(encoding="utf-8") == "# INSIDE"


@_needs_pinned_walk
def test_a_replacement_root_cannot_reuse_a_held_keys_qualifier(tmp_path, monkeypatch):
    """A REPLACED root must not re-derive a held key's qualifier and be written.

    The corrupting sequence, which a segment-only qualifier allowed:

    1. Two roots collide on ``tool``, so the catalogue offers a qualified key per copy
       and the agent editor holds one of them.
    2. That root is REPLACED -- uninstalled, and a different root installed that still
       carries the same distinguishing segment (``<...>/A/skills`` -> ``<...>/A/v2/skills``,
       an ordinary version bump).
    3. The editor PATCHes. :func:`apply_skill_mapping` resolves the held key against a
       FRESH catalog, the segment still derives, and the wrong file's ``skill://`` URI
       is persisted into the agent config -- silently, under a 200, and durably.

    Binding the qualifier to the root's identity makes step 3 impossible: the stale key
    resolves to nothing, and the write path already rejects the WHOLE request when any
    key is unknown, so no partial or wrong mapping lands.
    """
    rel = "tool"

    def root_with(sub: str, body: str):
        root = tmp_path / sub
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(body, encoding="utf-8")
        return root

    a_old = root_with("p/A/skills", "# ORIGINAL A -- what the editor was shown")
    b = root_with("p/B/skills", "# B")

    before = _package_catalog(tmp_path, monkeypatch, a_old, b)
    # ``is_relative_to`` and NOT a ``str().startswith(root + "/")`` prefix test: on
    # Windows ``str(path)`` yields backslashes, so a forward-slash prefix matches
    # nothing, the generator is empty and this line raises StopIteration -- a real
    # failure on the Windows shard that says nothing about the behaviour under test.
    held = next(k for k, v in before.items() if v.is_relative_to(a_old))
    editor_saw = before[held].resolve()

    # The replacement still carries the segment 'A', so a segment-only qualifier would
    # derive the SAME token for it -- that is what made the stale key re-bind.
    a_new = root_with("p/A/v2/skills", "# REPLACEMENT ROOT -- a DIFFERENT file")
    assert "A" in a_new.parts

    after = _package_catalog(tmp_path, monkeypatch, a_new, b)
    assert held not in after, "the held key must not survive the replacement"
    assert a_new.parts.count("A") == 1  # the segment really is still there

    # THE WRITE, exactly as the agent PATCH performs it.
    _set_edition_roots(monkeypatch, a_new, b)
    data: dict = {"resources": []}
    applied, unknown = _shared.apply_skill_mapping(
        data, tmp_path / "agent.json", _FakeState(), [held]
    )

    assert applied == [], applied
    assert unknown == [held], unknown
    assert data["resources"] == [], "nothing may be written when a key is unknown"

    # The specific corruption: no URI naming the replacement's file was persisted.
    wrong = str((a_new / rel / "SKILL.md").resolve())
    assert not any(wrong in str(r) for r in data["resources"])
    assert editor_saw != (a_new / rel / "SKILL.md").resolve()

    # Positive control: a CURRENT key from the same catalogue does write, so the
    # assertions above are about staleness and not about writes failing generally.
    live = next(k for k, v in after.items() if v.is_relative_to(a_new))
    fresh: dict = {"resources": []}
    applied2, unknown2 = _shared.apply_skill_mapping(
        fresh, tmp_path / "agent.json", _FakeState(), [live]
    )
    assert unknown2 == [], unknown2
    assert applied2 == [live]
    assert len(fresh["resources"]) == 1


def test_a_root_identity_token_is_stable_and_unique_per_root(tmp_path):
    """Same root -> same token across calls; different root -> different token.

    Stability is what makes a key usable at all: a token that changed between two
    enumerations would break every key on its own, and ``hash()`` would do exactly
    that, being salted per process. Uniqueness is what closes the replacement hazard.
    """
    one = tmp_path / "p" / "A" / "skills"
    two = tmp_path / "p" / "A" / "v2" / "skills"
    for d in (one, two):
        d.mkdir(parents=True)

    first = _shared._root_identity_token(one)
    assert first is not None
    assert first == _shared._root_identity_token(one), "must be stable across calls"
    assert first != _shared._root_identity_token(two), "must differ per root"

    # An alias reaching the SAME directory is the SAME identity, since the token is
    # taken from the canonical path -- an edition advertising both must not split in two.
    alias = tmp_path / "alias-root"
    alias.symlink_to(one, target_is_directory=True)
    assert _shared._root_identity_token(alias) == first

    # The token is one legal segment: no key separator, no glob metacharacter, and it
    # passes the resolver's own predicate, so a composed qualifier round-trips.
    assert _shared._SKILL_KEY_QUALIFIER_SEP not in first
    assert not any(c in first for c in _shared._GLOB_CHARS)
    assert _key_safe(first)


@pytest.mark.skipif(os.name == "nt", reason="Windows paths are text; no undecodable byte")
def test_a_root_named_with_undecodable_bytes_still_yields_a_token(tmp_path):
    """A POSIX root can be named with a byte the filesystem encoding cannot decode.

    Python surfaces such a byte as a LONE SURROGATE via surrogateescape, and
    ``str.encode("utf-8")`` REFUSES a lone surrogate — so taking the digest that way
    raised ``UnicodeEncodeError`` out of catalog enumeration for every install carrying
    one. A crash, not the fail-closed ``None`` the function documents: enumeration is
    reached from the skills catalog, so one such bundle took the whole listing down
    rather than dropping one row.

    ``os.fsencode`` reverses the same mapping, so the digest is taken over the root's
    real bytes. Asserted through ``_root_identity_token`` as well, because that is
    the caller the exception actually propagated through.
    """
    raw = os.path.join(os.fsencode(str(tmp_path)), b"pkg-\xff-bundle")
    try:
        os.mkdir(raw)
    except OSError as exc:
        # A UTF-8-enforcing filesystem (APFS) refuses the name outright, so a root carrying
        # an undecodable byte cannot exist there and there is nothing to guard against.
        pytest.skip(f"this filesystem will not store an undecodable name: {exc}")
    odd = _shared.Path(os.fsdecode(raw))
    assert odd.is_dir(), "fixture root was not created"
    # surrogateescape maps an undecodable byte 0x80-0xFF to U+DC80-U+DCFF. Asserted by
    # CODEPOINT rather than by a "\\udc" literal, which is a truncated escape in source.
    assert any(
        any(0xDC80 <= ord(ch) <= 0xDCFF for ch in part) for part in odd.parts
    ), "no surrogate present -- the fixture would not exercise the fix"

    token = _shared._root_identity_token(odd)
    assert token is not None
    assert _key_safe(token)

    # The caller must not raise either, and must still distinguish this root.
    plain = tmp_path / "pkg-plain-bundle"
    plain.mkdir()
    assert _shared._root_identity_token(plain) != token

    # POSITIVE CONTROL: an ordinary root still tokenises, so a pass above is not the
    # function having become a no-op that returns None for everything.
    assert _shared._root_identity_token(plain) is not None


@pytest.mark.skipif(
    sys.platform == "win32", reason="both fixture names use characters Windows reserves"
)
@pytest.mark.parametrize(
    ("rel", "marker"),
    [
        ("carries:colon", "reserved key qualifier separator"),
        ("glob*star", "not a relative path the resolver accepts"),
    ],
)
def test_an_omission_warning_names_the_absolute_file_path(
    tmp_path, monkeypatch, caplog, rel, marker
):
    """Every omission warning owes the ABSOLUTE path, not just the relative key.

    Once a rel is omitted the row is simply absent, so the log line is the only
    remediation surface there is — and the key alone is relative to a root the reader
    cannot infer. Two of the three warnings named only the relative key, which tells an
    operator that something was dropped without telling them where it is.

    Driven through the enumerator rather than the warning call, so it fails if the
    omission moves to a different branch.
    """
    import logging

    root = tmp_path / "packages" / "PkgA" / "skills"
    root.mkdir(parents=True)
    skill_md = root / rel / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# omitted", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=_shared.logger.name):
        keys = _package_catalog(tmp_path, monkeypatch, root)

    assert not any(rel in k for k in keys), f"{rel} should not be catalogued"
    messages = [r.getMessage() for r in caplog.records]
    hit = [m for m in messages if marker in m]
    assert hit, f"no warning carried {marker!r}; saw {messages}"
    assert str(skill_md) in hit[0], f"absolute path missing from warning: {hit[0]}"


def test_a_qualifier_the_resolver_would_refuse_is_never_returned(tmp_path):
    """Every derived qualifier must be one the resolver accepts, on hostile roots.

    An earlier spelling picked a path SEGMENT and rejected one only when it CARRIED the
    separator, so a segment the resolver refuses for a DIFFERENT reason -- a leading
    ``.`` or ``~``, or a traversal element -- was still returned, and the catalogue then
    offered ``package/<that>:<rel>`` whose qualifier fails
    key-safe by construction: offered and unresolvable.

    A digest cannot carry any of those, so the guarantee is now structural rather than
    filtered. Kept because the ROOTS are the hostile part: a root whose own name is
    ``..`` or ``~PkgA`` must still yield an acceptable qualifier.
    """
    for hostile in (".PkgA", "~PkgA", "..", "."):
        # The roots must EXIST: a root that cannot be stat'ed now refuses outright, and a
        # refusal would satisfy "no hostile name leaked" without testing the derivation.
        root = tmp_path / hostile / "skills"
        root.mkdir(parents=True, exist_ok=True)
        got = _shared._root_identity_token(root)
        assert got != hostile, f"returned {got!r}, which the resolver refuses"
        assert got is not None, hostile
        assert _key_safe(got), got
    # Positive control: two hostile roots still get DISTINCT qualifiers, so the
    # assertions above are not satisfied by some constant fallback.
    (tmp_path / "ctl-a" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ctl-b" / "skills").mkdir(parents=True, exist_ok=True)
    assert _shared._root_identity_token(
        tmp_path / "ctl-a" / "skills"
    ) != _shared._root_identity_token(tmp_path / "ctl-b" / "skills")


@_COLON_IN_FILENAME_OK
def test_a_single_character_before_a_colon_is_not_a_drive_off_windows():
    """``a:b`` is a legal relative name where drives do not exist, and must read as one.

    ``ntpath.splitdrive`` reports a drive for ANY single character before ``:``, so
    validating with Windows semantics refused ``a:b`` on every platform. On POSIX that
    is a perfectly ordinary directory name, and refusing it made the resolver reject a
    path the host can hold. Validate with HOST semantics instead: a drive is a drive
    where drives exist, and nowhere else.

    This is asserted against the predicate directly rather than through a colon-named
    skill, because the separator is now reserved — such a skill is omitted before the
    resolver's glob is ever reached, which would make a skill-level test vacuous while
    still passing.
    """
    is_windows = os.path.splitdrive("a:b")[0] != ""

    assert _shared._names_a_relative_path("a:b") is not is_windows
    # Controls: an unambiguously relative name is always accepted, and a rooted one
    # always refused, on every host.
    assert _shared._names_a_relative_path("plain/nested") is True
    assert _shared._names_a_relative_path("/rooted") is False


def test_a_core_row_under_the_reserved_prefix_is_pruned(tmp_path, monkeypatch):
    """A core skill cannot hold a ``package/`` key it cannot answer.

    Detail and tree now route a ``package/`` key exclusively to the package roots, so
    a core row whose own relative path is literally ``package/<rel>`` is catalogued
    by a lookup that will never reach it. Prune the reserved prefix out of the core
    walk before the package walks fold in, so the catalogue and the resolver agree.
    """
    core = tmp_path / "core_skills"
    (core / "package" / "foo").mkdir(parents=True)
    (core / "package" / "foo" / "SKILL.md").write_text("# core impostor", encoding="utf-8")
    (core / "plain").mkdir()
    (core / "plain" / "SKILL.md").write_text("# plain", encoding="utf-8")
    monkeypatch.setattr(_shared, "skills_dir", lambda: core)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch)

    catalog = _shared.enumerate_skill_catalog(_FakeState())

    # Positive control: the ordinary core skill IS catalogued, so a missing
    # package/ key below is the prune working rather than an unreachable fixture.
    assert any(k.endswith("plain") for k in catalog), sorted(catalog)
    offered = [k for k in catalog if k.startswith(_shared.PACKAGE_KEY_PREFIX)]
    assert offered == [], f"core rows offered under the reserved prefix: {offered}"


def test_the_prune_warning_names_the_stranded_files_absolute_path(tmp_path, monkeypatch, caplog):
    """The pruned file stays on disk, so the log must say WHERE it is.

    Dropping the row from the catalog makes the file invisible to the UI and to
    open-by-key, so this warning is the only surface naming its location. The key
    alone is not enough -- it is relative and the reader cannot know which core root
    it was joined onto. Emitting the absolute path is what makes the strand
    remediable by hand, which is the ONLY remediation surface: the API refuses every
    mutating verb on a ``package/`` key, stranded or not.
    """
    import logging

    core = tmp_path / "core_skills"
    (core / "package" / "foo").mkdir(parents=True)
    stranded = core / "package" / "foo" / "SKILL.md"
    stranded.write_text("# core impostor", encoding="utf-8")
    monkeypatch.setattr(_shared, "skills_dir", lambda: core)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers._shared"):
        _shared.enumerate_skill_catalog(_FakeState())

    pruned = [r for r in caplog.records if "reserved" in r.getMessage()]
    assert pruned, f"no prune warning logged: {[r.getMessage() for r in caplog.records]}"
    message = pruned[0].getMessage()
    assert str(stranded) in message, message
    # Negative control: an absolute path is strictly more than the key, so assert the
    # message is not merely the relative key that a reader cannot act on.
    assert message.count(str(stranded)) == 1, message


def test_a_qualifier_that_names_no_real_segment_fails_closed(tmp_path, monkeypatch):
    """A separator-only qualifier must narrow to nothing, not to everything.

    ``root.parts`` of an absolute path begins with ``"/"``, so a qualifier of ``"/"``
    is "present" in every root and the filter becomes a no-op — a qualified key then
    searches the whole territory and answers with a copy it never named. The contract
    is that a qualifier names a real path segment, so one that cannot be a segment
    must yield no roots at all.
    """
    root = tmp_path / "packages" / "PkgOnly" / "eventId-1" / "skills"
    (root / "shared-skill").mkdir(parents=True)
    (root / "shared-skill" / "SKILL.md").write_text("# solo", encoding="utf-8")
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch, root)

    # Coherence check: the unqualified key does resolve, so a None below is the filter
    # working rather than the fixture being unreachable.
    assert _shared._resolve_skill_root("package/shared-skill", _FakeState()) is not None
    for bogus in ("/", "..", "~"):
        key = f"package/{bogus}{_shared._SKILL_KEY_QUALIFIER_SEP}shared-skill"
        assert _shared._resolve_skill_root(key, _FakeState()) is None, key


def test_a_qualifier_is_always_key_safe_and_resolver_acceptable(tmp_path, monkeypatch):
    """A derived qualifier must survive its own key's parse, structurally.

    The hazard is a qualifier carrying the key separator: ``package/<qualifier>:<rel>``
    would then split at the qualifier's own colon, leaving a rel that names nothing --
    a key the catalog offers and the resolver cannot reach. An earlier spelling picked a
    human-legible PATH SEGMENT, so it had to filter candidates for this (a Windows drive
    anchor is exactly such a segment: ``PureWindowsPath("C:/x").parts[0]`` is ``"C:\\\\"``),
    and a root whose every candidate was rejected produced no qualifier at all.

    A digest cannot be unsafe, so the filter is gone rather than merely passing. Pinned
    on real roots that would have defeated the old segment rules -- one whose every
    segment is shared with the other, and one whose only distinguishing segment carries
    the separator -- because those are the shapes at risk of yielding ``None``.
    """
    shallow = tmp_path / "x" / "PkgA" / "skills"
    deep = tmp_path / "x" / "nested" / "PkgA" / "skills"
    for root in (shallow, deep):
        root.mkdir(parents=True)

    for root in (shallow, deep):
        q = _shared._root_identity_token(root)
        assert q is not None, root
        assert _shared._SKILL_KEY_QUALIFIER_SEP not in q, q
        assert not any(c in q for c in ("*", "?", "[")), q
        assert ".." not in q, q
        # AND the predicate the resolver itself applies to an incoming qualifier.
        assert _key_safe(q), q

    # Distinct roots, distinct qualifiers -- so the collision is addressable, which is
    # what the old segment rules could not promise for this pair.
    assert _shared._root_identity_token(shallow) != _shared._root_identity_token(deep)


def test_the_qualifier_is_stable_against_unrelated_bundle_changes(tmp_path):
    """The same root keys the same way no matter what else is installed.

    This is the property a segment-derived qualifier lacks: its segment was the
    first one absent from every OTHER colliding root, so installing or removing an
    unrelated bundle re-spelled the key of a root that had not moved. A key is a durable
    handle -- the editor holds one and the agent-config write path resolves one -- so a
    spelling that shifts underneath an untouched root is a defect even though each
    individual resolve was self-consistent.

    Derived from the root alone, so there is no set to be relative to. Asserted by
    deriving for one root while its NEIGHBOURS change around it.
    """
    subject = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    subject.mkdir(parents=True)
    first = _shared._root_identity_token(subject)
    assert first is not None

    # Install two unrelated bundles, one of which shares every segment of the subject
    # except its own -- the shape that would force the segment deeper or to None.
    for extra in ("PkgB/eventId-2", "PkgA/eventId-1/nested"):
        (tmp_path / "packages" / extra / "skills").mkdir(parents=True)
        assert _shared._root_identity_token(subject) == first, extra

    # And removing one does not move it either.
    (tmp_path / "packages" / "PkgB" / "eventId-2" / "skills").rmdir()
    assert _shared._root_identity_token(subject) == first


def test_the_identity_token_has_no_pure_path_fallback():
    """The deleted branch must not return: it was reachable only from a test.

    A behavioural test cannot catch its reintroduction, because re-adding the branch
    breaks nothing — it simply revives a production code path with no production
    caller. So this reads the source.
    """
    source = _shared.Path(_shared.__file__).read_text(encoding="utf-8")
    assert 'getattr(root, "resolve"' not in source, "the pure-path fallback is back"
    assert source.count("def _root_identity_token(") == 1
    # TWO named call sites: the qualifier MINT and the write-time REVALIDATION. A third
    # would mean the enumeration walk grew its own derivation, so the count stays exact.
    assert source.count("_root_identity_token(root)") == 2
    for owner in ("def _package_collision(", "def _qualified_entry_still_binds("):
        assert owner in source, f"the permitted caller {owner!r} is gone"


@_COLON_IN_FILENAME_OK
@_needs_pinned_walk
def test_roots_differing_only_by_a_colon_bearing_segment_are_addressable(tmp_path, monkeypatch):
    """The same rule end to end: a separator-bearing root name costs nothing now.

    ``C:`` is a legal POSIX directory name, so this is the drive-anchor shape reproduced
    on this platform. With a segment-derived qualifier the two roots would differ
    only in a segment carrying the separator, so no usable qualifier existed and BOTH
    copies were dropped with a log -- installed and invisible, the harm this grammar
    exists to remove, reintroduced by the legibility half.

    A digest is independent of the root's spelling, so both copies are addressable. What
    must still hold is that no key carries the raw segment: a key is parsed back at the
    FIRST separator, so ``package/C::shared-skill`` would split into qualifier ``C`` and
    a rel naming nothing.
    """
    root_c = tmp_path / "C:" / "skills"
    root_d = tmp_path / "D:" / "skills"
    for root in (root_c, root_d):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, root_c, root_d)

    assert keys == {
        f"package/{_q(root_c)}:shared-skill": root_c / "shared-skill" / "SKILL.md",
        f"package/{_q(root_d)}:shared-skill": root_d / "shared-skill" / "SKILL.md",
    }, keys
    # No key carries the roots' own separator-bearing names, and every key resolves to
    # its own copy -- so the grammar round-trips rather than merely being non-empty.
    for key, skill_md in keys.items():
        assert "C:" not in key and "D:" not in key, key
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve(), key


def test_resolve_skill_root_resolves_a_qualified_key(tmp_path, monkeypatch):
    """The routed entry point, which is what ``/tree`` and ``/file`` call.

    ``api_skill_detail`` reads the row's own ``path`` and does NOT come through
    here, so teaching only the detail path would leave a skill openable while its
    tree 404s — an asymmetry worse than the clean omission it replaces.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    assert _shared._resolve_skill_root("package/shared-skill", _FakeState()) is None
    assert (
        _shared._resolve_skill_root(f"package/{_q(root_a)}:shared-skill", _FakeState())
        == (root_a / "shared-skill").resolve()
    )
    assert (
        _shared._resolve_skill_root(f"package/{_q(root_b)}:shared-skill", _FakeState())
        == (root_b / "shared-skill").resolve()
    )


def test_resolve_skill_root_rejects_traversal_inside_a_qualified_key(tmp_path, monkeypatch):
    """The ``..`` rejection runs on the WHOLE key, before the split."""
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    assert _shared._resolve_skill_root("package/PkgA:../../etc", _FakeState()) is None
    assert _shared._resolve_skill_root("package/..:shared-skill", _FakeState()) is None


# ── enumeration: a colliding relative path is listed once per copy ──


def _q(root):
    """The qualifier production derives for *root*: its identity digest.

    A test cannot spell a digest literally without hardcoding one, which would pass for
    the wrong reason and break on any tmp path change. Composing it from the same
    function production uses keeps each test pinning the part it is ABOUT -- WHICH root
    a key resolves to -- rather than the digest's value.
    """
    token = _shared._root_identity_token(_shared.Path(root))
    assert token is not None, f"no identity token for {root}"
    return token


def _package_catalog(tmp_path, monkeypatch, *roots):
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch, *roots)
    catalog = _shared.enumerate_skill_catalog(_FakeState())
    return {k: v for k, v in catalog.items() if k.startswith("package/")}


@_needs_pinned_walk
def test_colliding_relative_path_is_enumerated_once_per_copy(tmp_path, monkeypatch):
    """Both copies get a key, and each key names its own file.

    ``setdefault`` would keep whichever root is walked first and drop the
    other, leaving one key the resolver refuses — a phantom row in the
    editor, and the other copy unreachable entirely.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b)

    assert keys == {
        f"package/{_q(root_a)}:shared-skill": root_a / "shared-skill" / "SKILL.md",
        f"package/{_q(root_b)}:shared-skill": root_b / "shared-skill" / "SKILL.md",
    }


def test_glob_pattern_characters_have_exactly_one_spelling():
    """One constant for "a pattern is not a name", not two that drift apart.

    Two spellings of the same three characters existed in this module for the same job —
    one for ``Path.glob`` when a key remainder is resolved, one for ``fnmatch`` when a
    ``skill://`` URI is inverted. Nothing made them agree, so adding a fourth character
    to one would silently leave the other accepting it: a rel refused at resolve time but
    still inverted from a URI, or the reverse. Collapsed to the pre-existing name.

    The definition must also PRECEDE both consumers. A module-level constant defined
    after a function that reads it still resolves at call time, so this cannot be caught
    by running the code — only by reading it.
    """
    source = _shared.Path(_shared.__file__).read_text(encoding="utf-8")

    assert "_GLOB_METACHARACTERS" not in source, "the duplicate spelling is back"
    assert source.count("\n_GLOB_CHARS = ") == 1, "more than one definition"

    lines = source.splitlines()
    definition = next(i for i, ln in enumerate(lines) if ln.startswith("_GLOB_CHARS = "))
    consumers = [
        i
        for i, ln in enumerate(lines)
        if "_GLOB_CHARS" in ln and not ln.startswith("_GLOB_CHARS =")
    ]
    assert len(consumers) == 2, consumers
    assert all(c > definition for c in consumers), (definition, consumers)

    # Negative control: the census can see a name that IS present, so a zero above is a
    # real absence rather than a pattern that never matches.
    assert "_GLOB_CHARS" in source

    # And the value is still the three characters both jobs need, with ``]`` excluded.
    assert set(_shared._GLOB_CHARS) == {"*", "?", "["}


@_needs_pinned_walk
def test_a_name_containing_dotdot_is_neither_catalogued_nor_resolved(tmp_path, monkeypatch):
    """``..`` INSIDE a component must be refused by BOTH sides, not just the resolver.

    The resolver's first gate tests ``".." in name`` — a SUBSTRING — so it refuses
    ``foo..bar`` outright. Enumeration used ``".." in host.parts``, a PARTS test, which
    admits it: the only part of ``foo..bar`` is ``"foo..bar"``, which is not ``".."``.
    The catalogue therefore listed a key the resolver could never reach, which is the
    phantom row this grammar exists to remove.

    Both shapes are covered, because ``..`` can enter the key from either half:

    * the REL carries it — ``package/foo..bar``; and
    * a root SEGMENT carries it, so the derived qualifier would — a root directory named
      ``pkg..v2`` composing ``package/pkg..v2-<digest>:<rel>``, refused as a whole by
      that same first gate.

    Asserted through the catalogue and the resolver rather than the predicates, so it
    fails if either side is loosened again. A plain colliding layout in the same fixture
    is the positive control: it must still be listed and still resolve, so a failure
    here is about ``..`` and not about the harness.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)

    # An uncollided rel, which is the POSITIVE CONTROL: it must survive both shapes
    # below, so their absence assertions cannot pass on an empty catalogue.
    (root_a / "only-a").mkdir()
    (root_a / "only-a" / "SKILL.md").write_text("# solo", encoding="utf-8")

    # Shape A: a rel whose component merely CONTAINS "..".
    (root_a / "foo..bar").mkdir()
    (root_a / "foo..bar" / "SKILL.md").write_text("# dotdot rel", encoding="utf-8")

    # Shape B: a root whose name carries "..". Under the old segment spelling this was
    # the sole candidate for that root, so refusing it left the collision unaddressable
    # and the all-or-nothing rule dropped it WHOLE. A digest carries no "..", so the
    # root is now addressable and the key still cannot smuggle a traversal element.
    dotted = tmp_path / "packages" / "pkg..v2" / "skills"
    (dotted / "shared-skill").mkdir(parents=True)
    (dotted / "shared-skill" / "SKILL.md").write_text("# dotdot root", encoding="utf-8")

    q_dotted = _shared._root_identity_token(dotted)
    assert q_dotted is not None, "a '..'-bearing root name blocked its own qualifier"
    assert ".." not in q_dotted, q_dotted

    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b, dotted)

    assert "package/foo..bar" not in keys, "a '..'-bearing rel was catalogued"
    assert not any(".." in k for k in keys), sorted(k for k in keys if ".." in k)

    # The '..'-named ROOT is addressable even though the '..'-bearing REL is not: the
    # two shapes are independent, and only the rel is a key the resolver must parse.
    assert f"package/{q_dotted}:shared-skill" in keys, sorted(keys)

    # The control survived, so the two absence assertions above mean something.
    assert "package/only-a" in keys, sorted(keys)

    # And the invariant still holds for whatever remains listed.
    for key, skill_md in keys.items():
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve(), key


def test_the_collision_set_is_computed_in_exactly_one_place():
    """Enumeration and resolution must SHARE the collision-set rule, not mirror it.

    The grammar's binding property is that resolution is the exact inverse of
    enumeration. Two copies of the rule agreeing today is not that guarantee -- it is a
    pair that drifts on the next edit to either one, and the drift is silent: a widened
    collision set makes a catalogued key stop resolving (a phantom row) while every
    existing test still passes. An earlier revision had exactly that, with the resolver
    counting the nested-leaf tier and skipping the dedupe.

    So this asserts the SHAPE rather than a behaviour: ``_package_collision`` is defined
    ONCE and both sides reach the rule only by calling it, so a future edit that
    re-inlines the derivation fails here instead of shipping a divergence.

    It deliberately does NOT census the derivation's internal spelling. That census
    failed red on any innocent rewrap of the comprehension while adding no coverage:
    the drift itself is pinned behaviourally by
    :func:`test_every_enumerated_package_key_resolves`, whose nested-leaf and
    symlink-alias roots were built for exactly this divergence. Measured by widening the
    resolver's collision set and confirming that test goes red.
    """
    source = _shared.Path(_shared.__file__).read_text(encoding="utf-8")

    # The shared helper exists once, and both consumers reach the rule through it.
    assert source.count("def _package_collision(") == 1
    assert source.count("_package_collision(") == 3, "expected 1 def + 2 call sites"

    # Negative control: the census can distinguish present from absent, so a zero
    # above would be a real absence rather than a pattern that never matches.
    assert source.count("_zzz_not_a_real_collision_helper(") == 0


def test_package_collision_reports_its_two_outcomes(tmp_path):
    """``_package_collision`` must separate no-collision from a real collision.

    Enumeration needs the distinction -- one mints the unqualified key, the other mints
    one qualified key per copy -- while resolution refuses the first. Collapsing them
    would make the fold either drop a perfectly good uncollided skill or mint a key no
    root answers to.
    """
    rel = "shared-skill"

    def root_with(sub: str, body: str):
        root = tmp_path / sub
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(body, encoding="utf-8")
        return root

    # 1. ONE distinct copy -> no qualifiers, and the caller keys it unqualified.
    solo = root_with("packages/PkgA/eventId-1/skills", "# solo")
    copies, qualifiers = _shared._package_collision([(solo, solo / rel / "SKILL.md")])
    assert len(copies) == 1
    assert qualifiers is None, "a single copy has no qualified spelling"

    # 2. TWO distinct copies -> one qualifier each, and they differ.
    other = root_with("packages/PkgB/eventId-2/skills", "# other")
    copies, qualifiers = _shared._package_collision(
        [(solo, solo / rel / "SKILL.md"), (other, other / rel / "SKILL.md")]
    )
    assert len(copies) == 2
    assert qualifiers == [_q(solo), _q(other)], qualifiers
    assert len(set(qualifiers)) == 2

    # 3. An ALIAS reaching copy 1's file is not a second copy, so the collision
    #    collapses back to case 1 rather than manufacturing a qualified key.
    alias = tmp_path / "packages" / "PkgAlias" / "eventId-3" / "skills"
    alias.mkdir(parents=True)
    (alias / rel).symlink_to(solo / rel, target_is_directory=True)
    copies, qualifiers = _shared._package_collision(
        [(solo, solo / rel / "SKILL.md"), (alias, alias / rel / "SKILL.md")]
    )
    assert len(copies) == 1, copies
    assert qualifiers is None

    # 4. The shape that could be a THIRD outcome: one root sharing every segment with
    #    the other, which no distinguishing segment could split, so the whole collision
    #    was dropped as unqualifiable. Digests differ regardless of shared spelling, so
    #    this is now an ordinary case 2 -- the omission branch survives only as a
    #    fail-closed backstop for a root that does not canonicalise.
    twin = root_with("twin/skills", "# twin")
    deeper = root_with("twin/nested/skills", "# deeper")
    copies, qualifiers = _shared._package_collision(
        [(twin, twin / rel / "SKILL.md"), (deeper, deeper / rel / "SKILL.md")]
    )
    assert len(copies) == 2, copies
    assert qualifiers == [_q(twin), _q(deeper)], qualifiers
    assert len(set(qualifiers)) == 2


def test_a_bundle_directory_named_like_a_file_keys_by_itself(tmp_path):
    """Which FIELD carries the location decides, so the row's SUFFIX must decide nothing.

    A skill directory is legal on POSIX under any name, ``notes.md`` included. Deciding
    file-ness from the suffix keys such a row by its PARENT, so the collision fold groups it
    under the wrong bundle and every colliding copy there stays listed under a key the
    resolver refuses -- a row that cannot be opened.
    """
    bundle = tmp_path / "pkg" / "notes.md"
    bundle.mkdir(parents=True)
    (bundle / "SKILL.md").write_text("# x", encoding="utf-8")

    assert _shared._row_dir_key({"dir": str(bundle)}) == str(bundle.resolve())
    # ``path`` names the file, so THIS shape is the one that steps up to the directory.
    assert _shared._row_dir_key({"path": str(bundle / "SKILL.md")}) == str(bundle.resolve())
    # Neither field present yields no key rather than a key derived from nothing.
    assert _shared._row_dir_key({}) is None


@_needs_pinned_walk
def test_every_enumerated_package_key_resolves(tmp_path, monkeypatch):
    """The invariant the qualifier exists to restore, asserted by RESOLVING.

    Asserting the ``package/`` prefix instead would pass vacuously under any key
    change — a false all-clear on exactly the property being changed. So each key
    is handed back to the resolver and compared to the file it was enumerated for.

    The fixture carries the two root shapes that make the invariant NON-TRIVIAL, because
    they are the two ways the resolver's collision set can drift from the enumerator's:

    * a NESTED-LEAF root (``<root>/<Pkg>/<rel>/SKILL.md``, the documented tier-2
      layout), which the resolver would see as a holder of ``<rel>`` if it counted the
      ``*/{name}`` tier — while the fold's collision unit is the walked rel, so it
      counts it as a separate, non-colliding entry; and
    * a SYMLINK-ALIAS root reaching another root's copy, which the fold collapses to one
      COPY and therefore keys unqualified; and
    * a root sharing EVERY segment with a sibling, the layout the description's claim about
      "no segment tells the colliding roots apart" is about. A segment rule cannot address
      it, so the whole collision was unqualifiable and the rel was omitted; a per-root
      digest keys it like any other, and asserting that HERE rather than only on the
      collision helper is what makes the invariant end-to-end over that layout.

    Either one, if admitted, widens the set the qualifier is derived against, so a
    segment that uniquely identified a holder at enumeration time stops being unique at
    resolve time and the derivation shifts deeper or to ``None`` — the enumerated key
    then matches no root and 404s. Without these two roots the test passes while the
    guarantee is false, since the remaining fixtures have disjoint distinguishing
    segments and so cannot expose the drift.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    (root_a / "only-a").mkdir()
    (root_a / "only-a" / "SKILL.md").write_text("# solo", encoding="utf-8")

    # A nested-leaf root whose PkgA segment collides with root_a's distinguishing one.
    nested = tmp_path / "packages" / "PkgA" / "eventId-3" / "skills"
    (nested / "PkgA" / "shared-skill").mkdir(parents=True)
    (nested / "PkgA" / "shared-skill" / "SKILL.md").write_text("# nested", encoding="utf-8")

    # An aliasing root reaching root_a's copy, again carrying a colliding segment.
    alias = tmp_path / "packages" / "PkgA" / "eventId-4" / "skills"
    alias.mkdir(parents=True)
    (alias / "shared-skill").symlink_to(root_a / "shared-skill", target_is_directory=True)

    # A root sharing EVERY segment with a sibling, so no distinguishing segment exists for
    # it at all. The helper's unit test pins this shape; this is the end-to-end half.
    twin = tmp_path / "twin" / "skills"
    deeper = tmp_path / "twin" / "nested" / "skills"
    for root, body in ((twin, "# twin"), (deeper, "# deeper")):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(body, encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b, nested, alias, twin, deeper)

    assert "package/only-a" in keys  # an uncollided path keeps its plain key
    # The fixture must actually exercise both shapes, or the assertions below are
    # satisfied by a catalogue that never saw them.
    assert any(
        v.is_relative_to(nested) for v in keys.values()
    ), "the nested-leaf root contributed no key"
    # The twin pair is the case under test only while twin holds no segment of its own,
    # so assert that instead of trusting the spelling to stay that way.
    assert not set(twin.parts) - set(deeper.parts), f"twin gained a unique segment: {twin}"
    for label, root in (("twin", twin), ("deeper", deeper)):
        assert any(
            v.is_relative_to(root) for v in keys.values()
        ), f"the {label} root contributed no key"
    assert len(keys) >= 6, sorted(keys)

    for key, skill_md in keys.items():
        resolved = _shared._resolve_skill_root(key, _FakeState())
        assert resolved == skill_md.parent.resolve(), f"{key} did not resolve to its own file"


@_needs_pinned_walk
def test_a_nested_leaf_sibling_leaves_every_qualified_key_resolvable(tmp_path, monkeypatch):
    """The three-root layout as REPORTED, pinned in the direction that can detect it.

    Reported mechanism: the nested root joins the qualified derivation set through the
    ``*/{name}/SKILL.md`` glob, carries the segment ``PkgA``, and so re-spells root1's
    qualifier deeper -- leaving an enumerated key that resolves to nothing.

    The FORWARD direction cannot see that. Measured: with the nested tier unioned into
    the qualified set, every assertion below the catalogue loop still passes, because the
    qualifier is a digest of each root's own canonical path -- widening the resolver's set
    cannot RE-SPELL a key the enumerator minted, so no enumerated key is lost and the
    reported symptom does not appear. What a wider set changes is which keys are ACCEPTED,
    so the withheld-key assertion at the end is the one that fails under the drift, and it
    is why this test asserts both directions on the reported three-root layout instead of
    only that the minted keys resolve.
    """
    root1 = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    root2 = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    root3 = tmp_path / "packages" / "PkgA" / "eventId-3" / "skills"
    for root, body in ((root1, "# one"), (root2, "# two")):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(body, encoding="utf-8")
    (root3 / "PkgA" / "shared-skill").mkdir(parents=True)
    (root3 / "PkgA" / "shared-skill" / "SKILL.md").write_text("# three", encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, root1, root2, root3)
    # The layout is the case under test only while the nested root is actually walked and
    # the collision is actually qualified, so assert both rather than trusting the shape.
    assert any(v.is_relative_to(root3) for v in keys.values()), "nested root contributed no key"
    assert (
        len([k for k in keys if _shared._SKILL_KEY_QUALIFIER_SEP in k]) == 2
    ), f"the collision was not keyed qualified: {sorted(keys)}"
    for key, skill_md in keys.items():
        assert (
            _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve()
        ), f"{key} enumerated but did not resolve to its own file"

    withheld = f"package/{_q(root3)}:shared-skill"
    # Non-vacuity: a spelling the catalogue really did not offer, or the refusal is empty.
    assert withheld not in keys, f"the catalogue DID mint {withheld}"
    assert (
        _shared._resolve_skill_root(withheld, _FakeState()) is None
    ), "the resolver accepted a qualified key the catalogue never minted for the nested root"


def test_an_alias_sibling_leaves_every_qualified_key_resolvable(tmp_path, monkeypatch):
    """The same report's second layout: an alias root, no nested tier involved.

    Reported mechanism: the enumerator collapses the alias by resolved identity and keys
    the survivors, while the resolver keeps the alias as a distinct holder and re-derives
    a different qualifier, so the minted key answers nothing.

    As above, the forward direction is blind to it -- measured: with the resolver's dedupe
    dropped, every minted key still resolves -- so the withheld-key refusal at the end is
    what pins this layout.
    """
    root1 = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    root2 = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    alias = tmp_path / "packages" / "PkgA" / "eventId-2" / "skills"
    for root, body in ((root1, "# one"), (root2, "# two")):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(body, encoding="utf-8")
    alias.mkdir(parents=True)
    (alias / "shared-skill").symlink_to(root1 / "shared-skill", target_is_directory=True)

    keys = _package_catalog(tmp_path, monkeypatch, root1, root2, alias)
    # The alias must really alias, or the dedupe under test never fires.
    assert (alias / "shared-skill" / "SKILL.md").resolve() == (
        root1 / "shared-skill" / "SKILL.md"
    ).resolve(), "the alias does not reach root1's copy"
    for key, skill_md in keys.items():
        assert (
            _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve()
        ), f"{key} enumerated but did not resolve to its own file"

    withheld = f"package/{_q(alias)}:shared-skill"
    # Non-vacuity: a spelling the catalogue really did not offer, or the refusal is empty.
    assert withheld not in keys, f"the catalogue DID mint {withheld}"
    assert (
        _shared._resolve_skill_root(withheld, _FakeState()) is None
    ), "the resolver accepted a qualified key the catalogue never minted for the alias root"


def test_no_qualified_key_the_catalog_withheld_resolves(tmp_path, monkeypatch):
    """The INVERSE direction, which the forward invariant cannot detect.

    The forward test asserts every enumerated key resolves. It passes under a resolver
    whose collision set is WIDER than the enumerator's, because the qualifier is a digest
    of each root's own canonical path: admitting extra holders cannot re-spell a key the
    enumerator did mint, so every enumerated key still resolves. What a wider set changes
    is which keys are ACCEPTED -- a qualifier for a root the fold never keyed becomes
    resolvable, and resolution stops being enumeration's inverse.

    Measured: unioning the ``*/{name}`` tier into the qualified collision set, and
    dropping the alias dedupe, are BOTH invisible to the forward test. They are visible
    here, which is why this direction is asserted separately rather than folded in.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)

    nested = tmp_path / "packages" / "PkgA" / "eventId-3" / "skills"
    (nested / "PkgA" / "shared-skill").mkdir(parents=True)
    (nested / "PkgA" / "shared-skill" / "SKILL.md").write_text("# nested", encoding="utf-8")

    alias = tmp_path / "packages" / "PkgA" / "eventId-4" / "skills"
    alias.mkdir(parents=True)
    (alias / "shared-skill").symlink_to(root_a / "shared-skill", target_is_directory=True)

    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b, nested, alias)

    withheld = {
        "nested-leaf root": f"package/{_q(nested)}:shared-skill",
        "symlink-alias root": f"package/{_q(alias)}:shared-skill",
    }
    # Non-vacuity: these must be spellings the catalogue really did NOT offer, or the
    # assertion below is satisfied by keys that were never candidates.
    for label, key in withheld.items():
        assert key not in keys, f"{label}: the catalogue DID offer {key}, so it is not withheld"

    for label, key in withheld.items():
        assert (
            _shared._resolve_skill_root(key, _FakeState()) is None
        ), f"{label}: the resolver accepted a qualified key the catalogue never minted"


def test_every_enumerated_core_key_resolves(tmp_path, monkeypatch):
    """The same invariant, for the THREE territories the qualifier does not key.

    :func:`_resolve_skill_root`'s first gate refuses ``".." in name`` as a SUBSTRING,
    before any prefix is dispatched, so it applies to ``""``, ``kiro-user/`` and
    ``kiro-workspace/`` exactly as it does to ``package/``. The catalogue walk applied
    no such filter, so a directory literally named ``foo..bar`` was offered under each
    of those three prefixes and then 404'd on open -- the phantom row this change
    exists to remove, left standing in the territories it did not key.

    ``..`` is the only divergence to close here, and deliberately the only one closed:
    core resolution joins the rel onto its root directly, so a glob-metacharacter name
    resolves there and filtering it would hide a skill that works today.
    """
    user_root = tmp_path / "home" / ".kiro" / "skills"
    data_root = tmp_path / "data_home"
    proj = tmp_path / "proj"
    ws_root = proj / ".kiro" / "skills"

    for root in (user_root, data_root, ws_root):
        # A working skill per root, so the absence asserted below cannot be satisfied
        # by a walk that never reached the root at all.
        (root / "good").mkdir(parents=True)
        (root / "good" / "SKILL.md").write_text("# fine", encoding="utf-8")
        (root / "foo..bar").mkdir()
        (root / "foo..bar" / "SKILL.md").write_text("# unresolvable", encoding="utf-8")
        # A leading ``~`` is refused by the UNPREFIXED branch only, so the same dirent is
        # a phantom row under ``""`` and a working skill under the other two.
        (root / "~tilde").mkdir()
        (root / "~tilde" / "SKILL.md").write_text("# top-level tilde", encoding="utf-8")
        (root / "outer" / "~nested").mkdir(parents=True)
        (root / "outer" / "~nested" / "SKILL.md").write_text("# nested", encoding="utf-8")

    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(_shared, "skills_dir", lambda: data_root)
    monkeypatch.setattr(_shared, "active_project_dir", lambda state, session_key="": proj)
    _set_edition_roots(monkeypatch)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    core = {k: v for k, v in catalog.items() if not k.startswith("package/")}

    # Vacuity guards: all three territories must actually be present in the catalogue,
    # or the invariant below is asserted over a set that never saw them.
    assert "good" in core, sorted(core)
    assert "kiro-user/good" in core, sorted(core)
    assert "kiro-workspace/good" in core, sorted(core)

    offered_dotdot = sorted(k for k in core if ".." in k)
    assert not offered_dotdot, f"catalogued but unresolvable: {offered_dotdot}"

    # The tilde guard must not widen: every spelling below resolves today, so omitting
    # any of them would hide a working skill.
    for still_offered in (
        "outer/~nested",
        "kiro-user/~tilde",
        "kiro-user/outer/~nested",
        "kiro-workspace/~tilde",
        "kiro-workspace/outer/~nested",
    ):
        assert still_offered in core, f"{still_offered} resolves and must stay listed"
    assert "~tilde" not in core, "an unprefixed top-level ~ name does not resolve"

    for key, skill_md in core.items():
        resolved = _shared._resolve_skill_root(key, _FakeState())
        assert resolved == skill_md.parent.resolve(), f"{key} did not resolve to its own file"


def _detail_req(method, name, state):
    """One ``api_skill_detail`` request double, shared by the stranded-file tests."""
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    r = MagicMock(spec=web.Request)
    r.app = {"state": state}
    r.method = method
    r.match_info = {"name": name}
    r.headers = {}
    r.query = {}
    r.cookies = {}
    r.json = AsyncMock(return_value={"content": "# edited"})
    return r


def test_the_omission_warning_names_the_cause_an_operator_can_act_on(tmp_path, monkeypatch, caplog):
    """The branch fires on an unidentifiable root, so the log must say so.

    Two distinct roots always canonicalise to distinct paths and so to distinct digests,
    which leaves a failed canonicalisation as the only way this branch is reached. An
    earlier wording blamed roots that "share every path segment" -- true of the retired
    segment-based qualifier, unreachable under a digest, and so a diagnosis that sends the
    reader to inspect paths that are not the problem.
    """
    import logging

    root_a, root_b = _two_colliding_roots(tmp_path)
    monkeypatch.setattr(_shared, "_root_identity_token", lambda _root: None)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers._shared"):
        keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b)

    assert "package/shared-skill" not in keys, keys
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "could not be canonicalised" in said, said
    assert "share every path segment" not in said, said
    # The remediation surface is the on-disk path, so it stays in the message.
    assert str(root_a / "shared-skill" / "SKILL.md") in said, said


def test_the_fold_is_handed_a_catalog_free_of_reserved_prefix_keys(tmp_path, monkeypatch, caplog):
    """The prune runs BEFORE the fold, which is what lets the fold assign blindly.

    ``_merge_package_walks`` writes every qualified key unconditionally. That is safe
    only because its sole caller has already removed every ``package/``-prefixed key
    from the catalog, so no core row can contest a qualified key inside the fold. The
    precondition belongs to the caller, so it is asserted against the caller: move the
    prune after the fold and this fails, while the fold itself still reads correctly.

    Uses a plain ``package/<rel>`` core row, so it is platform-independent — the
    contest staged by ``test_a_core_row_cannot_contest_a_qualified_key`` needs a
    colon-bearing directory and cannot exist on Windows.
    """
    core = tmp_path / "kirocrew_skills"
    reserved = core / "package" / "core-owned"
    reserved.mkdir(parents=True)
    (reserved / "SKILL.md").write_text("# core", encoding="utf-8")
    (core / "plain-core-skill").mkdir(parents=True)
    (core / "plain-core-skill" / "SKILL.md").write_text("# plain", encoding="utf-8")
    root_a, root_b = _two_colliding_roots(tmp_path)

    monkeypatch.setattr(_shared, "skills_dir", lambda: core)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch, root_a, root_b)

    handed: list[list[str]] = []
    real_merge = _shared._merge_package_walks

    def _spy(walks, catalog):
        handed.append(sorted(catalog))
        return real_merge(walks, catalog)

    monkeypatch.setattr(_shared, "_merge_package_walks", _spy)
    with caplog.at_level("WARNING"):
        catalog = _shared.enumerate_skill_catalog(_FakeState())

    assert handed, "the fold never ran, so its precondition was never observed"
    at_entry = handed[0]
    # Positive controls first: without these the assertion below passes vacuously on an
    # empty catalog or on a core walk that never keyed anything.
    assert any(
        not k.startswith(_shared.PACKAGE_KEY_PREFIX) for k in at_entry
    ), f"the core walk keyed nothing, so the check below proves nothing: {at_entry}"
    assert any(
        "reserved" in r.getMessage() for r in caplog.records
    ), "no prune warning, so the reserved row was never keyed and then removed"
    # The property this pins directly.
    assert [
        k for k in at_entry if k.startswith(_shared.PACKAGE_KEY_PREFIX)
    ] == [], f"the fold was handed reserved-prefix keys: {at_entry}"
    assert reserved / "SKILL.md" not in catalog.values()


@_COLON_IN_FILENAME_OK
@_needs_pinned_walk
def test_a_core_row_cannot_contest_a_qualified_key(tmp_path, monkeypatch, caplog):
    """A core skill at ``package/<qualifier>:<rel>`` loses the key outright.

    The unprefixed core walk keys such a skill byte-identically to the qualified key
    this fold wants for PkgA's copy, so the catalog would have named one file under a
    key the resolver answers with a DIFFERENT one: the editor lists the core skill
    while a spec written from that row loads the packaged one. The reserved-prefix
    prune settles it before the fold even runs — a ``package/`` key is answerable only
    from package roots, so a core row holding one is offered to nobody, and the
    qualified key then means what the resolver says it means.

    The contender's own directory name carries the separator, which NTFS reads as an
    alternate-data-stream marker, so the fixture cannot exist on Windows and the
    contest it stages is unreachable there. The prune itself is platform-independent
    and is covered on every platform by
    ``test_a_core_row_under_the_reserved_prefix_is_pruned``.
    """
    core = tmp_path / "kirocrew_skills"
    contender = core / "package" / f"PkgA{_shared._SKILL_KEY_QUALIFIER_SEP}shared-skill"
    contender.mkdir(parents=True)
    (contender / "SKILL.md").write_text("# core", encoding="utf-8")
    root_a, root_b = _two_colliding_roots(tmp_path)

    monkeypatch.setattr(_shared, "skills_dir", lambda: core)
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    _set_edition_roots(monkeypatch, root_a, root_b)
    with caplog.at_level("WARNING"):
        catalog = _shared.enumerate_skill_catalog(_FakeState())

    # The key is PkgA's, and the core file is nowhere in the catalogue.
    assert catalog[f"package/{_q(root_a)}:shared-skill"] == root_a / "shared-skill" / "SKILL.md"
    assert catalog[f"package/{_q(root_b)}:shared-skill"] == root_b / "shared-skill" / "SKILL.md"
    assert contender / "SKILL.md" not in catalog.values()
    assert any("reserved" in r.getMessage() for r in caplog.records)
    # The binding invariant: every offered key resolves to its own file.
    for key, skill_md in catalog.items():
        if not key.startswith("package/"):
            continue
        resolved = _shared._resolve_skill_root(key, _FakeState())
        assert resolved == skill_md.parent.resolve(), f"{key} did not resolve to its own file"


def test_symlink_alias_across_two_roots_keeps_the_plain_key(tmp_path, monkeypatch):
    """One file reachable twice is not a collision, so it is not qualified.

    An edition may advertise both a directory and a symlink into it. Qualifying
    that would split one skill into two keys and change a key that works today.
    """
    real = tmp_path / "real" / "skills"
    (real / "shared-skill").mkdir(parents=True)
    (real / "shared-skill" / "SKILL.md").write_text("# one", encoding="utf-8")
    alias = tmp_path / "alias_skills"
    alias.symlink_to(real, target_is_directory=True)
    keys = _package_catalog(tmp_path, monkeypatch, real, alias)

    assert list(keys) == ["package/shared-skill"]


@_needs_pinned_walk
def test_indistinguishable_roots_are_addressable_rather_than_dropped(tmp_path, monkeypatch):
    """No segment tells the roots apart, and that costs the rows nothing.

    One root is a path PREFIX of the other, so the shallower one has no segment the
    deeper one lacks. With a segment-derived qualifier neither remaining
    option worked -- a plain key names two distinct files, and a segment shared by both
    matches both -- so the whole collision was dropped and BOTH copies became invisible
    and 404 on open. That is precisely the pre-existing harm this change exists to
    remove, reintroduced by the legibility half of the qualifier.

    Digests differ between the two roots regardless of how much of their spelling is
    shared, so both copies are addressable.
    """
    outer = tmp_path / "skills"
    inner = outer / "nested" / "skills"
    for root in (outer, inner):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, outer, inner)

    # Both copies get a key, each naming its own file, and no unqualified key survives
    # for a rel two roots contest.
    assert "package/shared-skill" not in keys
    assert f"package/{_q(outer)}:shared-skill" in keys, sorted(keys)
    assert f"package/{_q(inner)}:shared-skill" in keys, sorted(keys)
    # Every offered key routes -- the invariant an omission branch could only buy by
    # offering nothing at all.
    for key, skill_md in keys.items():
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve(), key


def test_qualifier_is_chosen_by_segment_membership_not_by_diverging_index(tmp_path, monkeypatch):
    """The chosen qualifier must be absent from every colliding root's path.

    Picking the segment at the first index where the roots DIFFER looks equivalent
    and is not: ``PkgA`` differs from ``nested`` at that index while still
    occurring deeper in the sibling root, so a qualifier chosen that way would
    match BOTH candidates and the resolver would refuse a key enumeration had just
    offered. Membership rejects it instead, and the path is omitted — so whichever
    branch is taken, every offered key still resolves.
    """
    root_a = tmp_path / "x" / "PkgA" / "skills"
    root_b = tmp_path / "x" / "nested" / "PkgA" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text(f"# {root}", encoding="utf-8")
    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b)

    assert "package/PkgA:shared-skill" not in keys
    for key, skill_md in keys.items():
        assert _shared._resolve_skill_root(key, _FakeState()) == skill_md.parent.resolve(), key


def test_qualified_key_does_not_reach_the_leaf_fallback(tmp_path):
    """A qualified remainder matches no leaf, at the level of the helper itself.

    ``_match_package_row``'s leaf fallback compares the key remainder to a row's
    ``name``. For a qualified key that remainder is ``<qualifier>:<rel>``, which
    matches no leaf — and that is the wanted behaviour: a leaf match would serve
    whichever package happened to be the only row with that leaf, ignoring the
    qualifier that was the whole point of the key.

    This is a property of the helper in isolation. ``api_skill_detail`` never
    reaches it with a qualified remainder at all — it 404s first, because a row keyed
    with the reserved separator would answer 200 for a key ``/tree`` omits (see
    ``test_a_qualified_remainder_never_reaches_the_row_fallback``). The exact-key
    assertion below therefore pins the helper's contract, not a reachable endpoint
    path.
    """
    from kiro_crew.dashboard.handlers.prompts import _match_package_row

    rows = [{"key": "package/PkgA:shared-skill", "name": "shared-skill", "path": "/a/SKILL.md"}]
    row = _match_package_row(rows, "package/PkgA:shared-skill", "PkgA:shared-skill")
    assert row is not None and row["path"] == "/a/SKILL.md"
    assert _match_package_row(rows, "package/PkgB:shared-skill", "PkgB:shared-skill") is None


def _sel_spy(monkeypatch):
    """Capture the SEL seam ``_sel()`` late-binds to, so refusals can be asserted."""
    from unittest.mock import MagicMock

    m = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: m)
    return m


def test_the_qualifier_is_too_wide_to_grind_a_stale_key_onto_another_root(tmp_path):
    """A stale key must not be re-bindable by CHOOSING an install path that collides.

    The docstring's promise is that a different root cannot produce a given qualifier.
    That holds only while the digest is too wide to search: a narrow one is ground
    against, not merely collided with by accident, so the width IS the guarantee.
    """
    import hashlib
    import os

    from kiro_crew.dashboard.handlers import _shared

    base = tmp_path.resolve()

    # Mechanism control: at a deliberately narrow width the collision is findable in a
    # few hundred tries, which is what makes a narrow qualifier re-bindable at all.
    def _narrow(p):
        return hashlib.blake2b(os.fsencode(str(p)), digest_size=2).hexdigest()

    seen: dict[str, object] = {}
    ground: tuple[object, object] | None = None
    for i in range(20000):
        cand = base / f"bundle-{i}" / "skills"
        token = _narrow(cand)
        if token in seen:
            ground = (seen[token], cand)
            break
        seen[token] = cand
    assert ground is not None, "narrow-width grind found no collision; control is broken"

    first, second = ground
    assert _narrow(first) == _narrow(second), "control pair does not actually collide"

    for r in (first, second):
        r.mkdir(parents=True)

    # The shipped width must make that search infeasible rather than merely unlikely.
    bits = _shared._ROOT_IDENTITY_DIGEST_BYTES * 8
    assert bits >= 128, f"qualifier is {bits} bits, narrow enough to grind a rebinding"

    token = _shared._root_identity_token(first)
    assert token is not None
    assert len(token) * 4 >= 128, f"qualifier renders {len(token) * 4} bits"

    # And the pair that collided at the narrow width must NOT collide at the shipped one,
    # so the extra width is doing the separating rather than merely being present.
    assert _shared._root_identity_token(first) != _shared._root_identity_token(second)


@pytest.mark.asyncio
async def test_a_refused_row_path_denial_is_sel_recorded(tmp_path, monkeypatch):
    """A name ``validate_file_path`` rejects outright is the one KNOWABLE denial.

    It is the strongest operator signal on this surface -- a rejected name can mean
    filesystem probing -- so it must not be the one return that leaves no trace.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    spy = _sel_spy(monkeypatch)
    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: None)

    mgr = MagicMock()
    mgr.available.return_value = True
    mgr.list_skills = AsyncMock(return_value=[{"key": "package/row-skill", "path": "/x/SKILL.md"}])
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)
    monkeypatch.setattr(_prompts, "_match_package_row", lambda *_a, **_k: {"path": "/x/SKILL.md"})
    monkeypatch.setattr(_prompts, "validate_file_path", lambda _p: None)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/row-skill", MagicMock(_slots={}))
    )

    assert response.status == 403, response.text
    outcomes = [c.kwargs.get("outcome") for c in spy.log_tool_invocation.call_args_list]
    assert outcomes, "the refused row read emitted no SEL event"


@pytest.mark.asyncio
async def test_a_missing_package_file_is_not_reported_as_access_denied(tmp_path, monkeypatch):
    """The descriptor gate answers ``None`` for an absent file as well as a refused one.

    Reporting the absence as a denial makes the endpoint an oracle in the other
    direction and dilutes the signal a real refusal carries.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    (root / "present.md").write_text("HERE", encoding="utf-8")
    # A ``package/`` key resolves THROUGH an edition root, so the fixture registers one.
    _set_edition_roots(monkeypatch, root.parent)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: root)

    def _req(rel):
        r = MagicMock(spec=web.Request)
        r.app = {"state": MagicMock(_slots={})}
        r.match_info = {"name": "package/shared-skill"}
        r.query = {"path": rel}
        r.headers = {}
        return r

    absent = await _prompts.api_skill_file(_req("no-such-file.md"))

    assert absent.status != 403, f"an absent file was reported as a denial: {absent.text}"
    assert "access denied" not in (absent.text or "")

    # Positive control: a file that IS present still reads, so the branch above is
    # about absence rather than a handler that refuses every package read.
    served = await _prompts.api_skill_file(_req("present.md"))
    assert served.status == 200, served.text
    assert "HERE" in served.text


@pytest.mark.asyncio
async def test_a_row_is_not_served_with_another_roots_content(tmp_path, monkeypatch):
    """An exact row and the resolver can land on DIFFERENT files for one key.

    The resolver answers first, so where a row claims a key whose root-relative path
    also exists under another root, the other root's bytes were returned under the
    row's identity -- the caller is told it opened the row it selected. Serving the row
    instead would only swap which of the two lies, so neither is served.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    # The row's own file, keyed as ``tool`` although its rel path is ``vendor/tool``.
    row_root = tmp_path / "packages" / "PkgRow" / "eventId-1" / "skills"
    (row_root / "vendor" / "tool").mkdir(parents=True)
    (row_root / "vendor" / "tool" / "SKILL.md").write_text("ROW_BODY", encoding="utf-8")

    # A second root where ``tool`` IS the rel path, so the resolver answers from here.
    other = tmp_path / "packages" / "PkgOther" / "eventId-2" / "skills"
    (other / "tool").mkdir(parents=True)
    (other / "tool" / "SKILL.md").write_text("OTHER_ROOT_BODY", encoding="utf-8")

    _set_edition_roots(monkeypatch, row_root, other)

    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    row = {"key": "package/tool", "path": str(row_root / "vendor" / "tool" / "SKILL.md")}
    mgr = MagicMock()
    mgr.available.return_value = True
    mgr.list_skills = AsyncMock(return_value=[row])
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/tool", MagicMock(_slots={}))
    )

    body = response.text or ""
    assert "OTHER_ROOT_BODY" not in body, f"another root's content served as the row: {body}"
    assert response.status == 404, f"the shadowed read was answered: {response.status} {body}"

    # Positive control: with the second root gone AND the row naming the file the
    # resolver lands on, the same key still reads, so the 404 above is the disagreement.
    (other / "tool" / "SKILL.md").unlink()
    _set_edition_roots(monkeypatch, row_root)
    (row_root / "tool").mkdir()
    (row_root / "tool" / "SKILL.md").write_text("ROW_BODY", encoding="utf-8")
    row["path"] = str(row_root / "tool" / "SKILL.md")
    served = await _prompts.api_skill_detail(
        _detail_req("GET", "package/tool", MagicMock(_slots={}))
    )
    assert served.status == 200, served.text


@pytest.mark.asyncio
async def test_a_served_qualified_read_is_sel_recorded(tmp_path, monkeypatch):
    """Serving one of several copies is a selection, so the choice must be durable.

    Every refusal on this path already leaves a line, which made the SUCCESS the one
    outcome absent from SEL -- and it is the outcome that names WHICH copy's bytes went
    out. Without it an operator reading SEL sees the reads that were withheld and cannot
    tell whether the copy served was the one the key asked for.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    # A qualified read is served only through a DESCRIPTOR walk; a rename-proof pin covers
    # the root and its ancestors, never a traversed descendant, so it fails closed instead.
    if not _prompts.pinned_fs.supports_pinned_walk():
        pytest.skip("no descriptor walk on this platform, so a qualified read is refused")

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: MagicMock(available=lambda: False))

    spy = _sel_spy(monkeypatch)
    key = f"package/{_q(root_a)}:shared-skill"
    served = await _prompts.api_skill_detail(_detail_req("GET", key, MagicMock(_slots={})))

    assert served.status == 200, served.text
    assert "# from PkgA" in (served.text or ""), served.text
    ok_calls = [
        c
        for c in spy.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "ok" and c.kwargs.get("tool_name") == "api_skill_detail"
    ]
    assert ok_calls, [c.kwargs.get("outcome") for c in spy.log_tool_invocation.call_args_list]
    assert ok_calls[0].kwargs.get("metadata", {}).get("name") == key, ok_calls[0].kwargs

    spy.log_tool_invocation.reset_mock()
    (root_a / "shared-skill" / "SKILL.md").unlink()
    gone = await _prompts.api_skill_detail(_detail_req("GET", key, MagicMock(_slots={})))
    gone_outcomes = [c.kwargs.get("outcome") for c in spy.log_tool_invocation.call_args_list]
    assert gone.status == 404, gone.text
    assert "ok" not in gone_outcomes, gone_outcomes


@pytest.mark.asyncio
async def test_an_unvalidatable_row_path_is_refused_before_it_is_resolved(tmp_path, monkeypatch):
    """A row path is edition-supplied, so it is validated BEFORE being resolved.

    Resolving first follows whatever the path points at, which is the operation the
    validator exists to gate; a rejected path must therefore fail closed rather than be
    canonicalised and compared. The resolver can answer this key, so without the
    ordering the read is served with the validation skipped.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgOnly" / "eventId-1" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("ROOT_BODY", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    row = {"key": "package/tool", "path": str(root / "tool" / "SKILL.md")}
    mgr = MagicMock()
    mgr.available.return_value = True
    mgr.list_skills = AsyncMock(return_value=[row])
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    calls: list[str] = []

    def _refusing_validator(p: str) -> None:
        calls.append(p)
        return None

    monkeypatch.setattr(_prompts, "validate_file_path", _refusing_validator)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/tool", MagicMock(_slots={}))
    )

    assert calls, "the row path was never validated"
    assert "ROOT_BODY" not in (response.text or ""), "served despite a refused row path"
    assert response.status == 404, f"the refused path was answered: {response.status}"


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_the_reviewed_nested_leaf_layout_keeps_every_key_resolvable(tmp_path, monkeypatch):
    """Three roots where a nested-leaf copy carries a colliding PATH SEGMENT.

    Reported as a phantom row: the resolver was said to admit the nested-leaf tier into
    the set the qualifier is derived against, so a segment unique at enumeration time
    stopped being unique at resolve time and the enumerated key bound to no root. The
    qualifier is a per-root identity digest and the derivation set is the EXACT tier
    alone, so neither half of that mechanism survives -- pinned here per reported layout
    rather than by the shapes a fixture happens to reach.
    """
    root1 = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root1 / "shared-skill").mkdir(parents=True)
    (root1 / "shared-skill" / "SKILL.md").write_text("# root1", encoding="utf-8")

    root2 = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    (root2 / "shared-skill").mkdir(parents=True)
    (root2 / "shared-skill" / "SKILL.md").write_text("# root2", encoding="utf-8")

    root3 = tmp_path / "packages" / "PkgA" / "eventId-3" / "skills"
    (root3 / "PkgA" / "shared-skill").mkdir(parents=True)
    (root3 / "PkgA" / "shared-skill" / "SKILL.md").write_text("# root3", encoding="utf-8")

    keys = _package_catalog(tmp_path, monkeypatch, root1, root2, root3)

    sep = _shared._SKILL_KEY_QUALIFIER_SEP
    assert sum(1 for k in keys if sep in k) == 2, f"the collision minted no pair: {sorted(keys)}"
    assert any(
        v.is_relative_to(root3) for v in keys.values()
    ), "the nested-leaf root contributed no key"

    for key, skill_md in keys.items():
        resolved = _shared._resolve_skill_root(key, _FakeState())
        assert resolved == skill_md.parent.resolve(), f"{key} resolved to nothing"

    # The SEGMENT spelling the report expected is never minted, so it must not answer.
    assert _shared._resolve_package_skill_path("shared-skill", qualifier="PkgA") is None


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_the_reviewed_symlink_alias_layout_keeps_every_key_resolvable(tmp_path, monkeypatch):
    """The same claim with NO nested tier: root3's copy is a symlink to root1's.

    The enumerator collapses the alias by resolved identity. The report was that the
    resolver keeps it in its own set and re-derives a different qualifier, so the
    catalogued key answers nothing -- and that in one variant BOTH qualified keys of the
    collision do. A per-root digest cannot be re-spelled by set membership.
    """
    root1 = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root1 / "shared-skill").mkdir(parents=True)
    (root1 / "shared-skill" / "SKILL.md").write_text("# root1", encoding="utf-8")

    root2 = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    (root2 / "shared-skill").mkdir(parents=True)
    (root2 / "shared-skill" / "SKILL.md").write_text("# root2", encoding="utf-8")

    root3 = tmp_path / "packages" / "PkgA" / "eventId-2" / "skills"
    root3.mkdir(parents=True)
    (root3 / "shared-skill").symlink_to(root1 / "shared-skill", target_is_directory=True)

    keys = _package_catalog(tmp_path, monkeypatch, root1, root2, root3)

    sep = _shared._SKILL_KEY_QUALIFIER_SEP
    qualified = {k: v for k, v in keys.items() if sep in k}
    assert len(qualified) == 2, f"the alias was not collapsed to one pair: {sorted(keys)}"

    for key, skill_md in qualified.items():
        resolved = _shared._resolve_skill_root(key, _FakeState())
        assert resolved == skill_md.parent.resolve(), f"{key} resolved to nothing"


def test_a_key_deeper_than_the_walk_is_refused_rather_than_crashing(tmp_path, monkeypatch):
    """``Path.glob`` recurses per pattern component, so depth is a crash surface.

    A caller-supplied remainder reaches the resolver's glob verbatim, and at a few
    hundred components the recursion limit is hit inside pathlib -- a 500 rather than a
    404. The ceiling is the walk's own nesting bound, so refusing above it cannot strand
    an enumerated key: nothing deeper is mintable in the first place.
    """
    root = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "b" / "c" / "SKILL.md").write_text("# deepest mintable", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    ceiling = _shared._SKILL_NEST_DEPTH

    # The depth that crashes pathlib must be refused by the guard, not passed to it.
    deep = "/".join(f"d{i}" for i in range(500))
    assert _shared._resolve_package_skill_path(deep, qualifier=None) is None
    assert _shared._names_a_relative_path(deep) is False

    assert _shared._names_a_relative_path("/".join("x" * 1 for _ in range(ceiling))) is True
    assert _shared._names_a_relative_path("/".join("x" * 1 for _ in range(ceiling + 1))) is False

    # Positive control: the deepest key the walk CAN mint still resolves, so the ceiling
    # was placed at the enumerator's bound rather than below it.
    keys = _package_catalog(tmp_path, monkeypatch, root)
    minted = [k for k in keys if k.count("/") == ceiling]
    assert minted, f"the fixture minted nothing at the ceiling: {sorted(keys)}"
    for key in minted:
        assert _shared._resolve_skill_root(key, _FakeState()) == keys[key].parent.resolve()


@pytest.mark.asyncio
async def test_a_symlink_loop_row_path_is_refused_rather_than_crashing(tmp_path, monkeypatch):
    """A canonicalized row path can still carry a symlink loop.

    ``os.path.realpath`` gives up on a loop WITHOUT raising and returns the path
    unchanged, so the validator answers a non-None "canonical" string and the first call
    that actually walks the links is the comparison itself -- where a loop is a
    ``RuntimeError`` (an ``OSError`` on newer interpreters) and would leave the endpoint
    answering 500. The row is edition-supplied, so that is reachable from data.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("# real", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    # A two-hop directory loop, then a row pointing THROUGH it.
    (tmp_path / "loopa").symlink_to(tmp_path / "loopb", target_is_directory=True)
    (tmp_path / "loopb").symlink_to(tmp_path / "loopa", target_is_directory=True)
    looped = str(tmp_path / "loopa" / "SKILL.md")
    if os.name != "nt":
        # Windows' linked-ancestor gate refuses this shape before any comparison runs.
        assert _prompts.validate_file_path(looped) is not None, "the validator refused it outright"

    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)

    mgr = MagicMock()
    mgr.available.return_value = True
    mgr.list_skills = AsyncMock(return_value=[{"key": "package/tool", "path": looped}])
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/tool", MagicMock(_slots={}))
    )

    assert response.status == 404, f"the loop escaped as {response.status}: {response.text}"
    assert "# real" not in (response.text or ""), "served the resolver's file under the row"


@pytest.mark.asyncio
async def test_a_nul_in_the_path_query_is_refused_rather_than_crashing(tmp_path, monkeypatch):
    """``ValueError`` is not an ``OSError``, so a NUL escaped the read handler.

    An embedded NUL makes the C-level ``lstat`` raise ``ValueError`` rather than answering,
    and the reader guards caught only ``OSError`` -- so a caller-supplied ``path`` carrying
    one left the endpoint answering 500 instead of a refusal.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("ORDINARY", encoding="utf-8")
    _set_edition_roots(monkeypatch, root.parent)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: root)

    def _req(name, rel):
        r = MagicMock(spec=web.Request)
        r.app = {"state": MagicMock(_slots={})}
        r.match_info = {"name": name}
        r.query = {"path": rel}
        r.headers = {}
        return r

    for rel in ("\x00", "notes.md\x00", "a\x00b.md"):
        response = await _prompts.api_skill_file(_req("package/shared-skill", rel))
        assert response.status in (400, 404), f"{rel!r}: {response.status} {response.text}"

    # Positive control: an ordinary remainder in the same root still reads, so the loop
    # above is refusing the NUL rather than refusing every request.
    served = await _prompts.api_skill_file(_req("package/shared-skill", "notes.md"))
    assert served.status == 200, served.text
    assert "ORDINARY" in served.text


def test_a_bundle_replaced_at_the_same_path_cannot_re_derive_the_qualifier(tmp_path):
    """The canonical path alone is not an identity, so it must not be the whole basis.

    Uninstalling a bundle and installing another at the SAME path left the digest
    unchanged, so a key an editor still held resolved to the replacement's file and the
    write path persisted a skill the user never selected. Binding the root's device and
    inode makes the identity per-instance: the replacement is a different root, the stale
    key fails to resolve, and the write path rejects the whole request.
    """
    root = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root / "shared-skill").mkdir(parents=True)
    (root / "shared-skill" / "SKILL.md").write_text("# first bundle", encoding="utf-8")

    first = _shared._root_identity_token(root)
    assert first is not None, "the fixture root has no identity"
    assert _shared._root_identity_token(root) == first, "identity is unstable in place"

    # Uninstall, then install a DIFFERENT bundle at exactly the same path.
    import shutil

    shutil.rmtree(tmp_path / "packages" / "PkgA")
    (root / "shared-skill").mkdir(parents=True)
    (root / "shared-skill" / "SKILL.md").write_text("# second bundle", encoding="utf-8")

    second = _shared._root_identity_token(root)
    assert second is not None, "the replacement root has no identity"
    assert second != first, "a replacement bundle re-derived the replaced bundle's qualifier"


def test_a_recycled_inode_does_not_re_derive_the_replaced_bundles_qualifier(tmp_path):
    """A replacement bundle handed the SAME inode number must still get a new qualifier.

    An inode number is a reusable resource: uninstall a bundle and install another at the
    same path and the replacement can receive the identical ``st_ino``. Binding the
    qualifier to ``dev:ino`` alone therefore re-derives the REPLACED bundle's key, so a
    key an editor still holds resolves to the replacement's file and the write path
    persists a skill nobody selected. ``st_ctime_ns`` is what cannot be recycled with it.

    Recycling cannot be forced on demand, so the allocator is stood in for: the stat the
    token is taken over reports the replaced inode's own dev:ino with a later creation
    time, which is exactly the state a recycling allocator produces. The later time is
    constructed rather than measured, because this filesystem's timestamp granularity is
    coarse enough (~20ms on xfs here) that a real recreate lands in the same granule.
    """
    root = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("# first bundle", encoding="utf-8")

    before = root.stat()
    first = _shared._root_identity_token(root)
    assert first is not None

    class _Recycled:
        """The replaced inode's number, carrying the replacement's later creation time."""

        st_dev = before.st_dev
        st_ino = before.st_ino
        st_ctime_ns = before.st_ctime_ns + 1
        # Held EQUAL on purpose: the creation-time bump alone must discriminate, so this
        # cannot pass merely because the newer field moved too.
        st_mtime_ns = before.st_mtime_ns

    class _RecycledRoot(type(root)):
        """A root whose stat reports the recycled triple.

        A subclass rather than a patch of ``Path.stat``: patching that globally reaches
        pytest's own failure formatting and takes the run down with an INTERNALERROR
        instead of reporting the assertion.
        """

        def stat(self, *a, **k):
            return _Recycled()

    second = _shared._root_identity_token(_RecycledRoot(str(root)))

    assert second is not None
    assert second != first, "a recycled inode re-derived the replaced bundle's qualifier"


@pytest.mark.asyncio
async def test_a_stale_package_key_that_resolves_to_nothing_is_sel_recorded(tmp_path, monkeypatch):
    """The fail-closed stale-key 404 is an access decision and must leave a SEL line.

    A qualifier carries its root's identity, so a bundle replacement re-spells every key
    and a key an editor still holds resolves to nothing. That is the DESIGNED path, not a
    rare one, so an operator reconstructing what a session tried to open must see it. The
    resolver miss reported an empty refusal, which the caller reads as "nothing to
    record", and the shared 404 then answered with no audit at all.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("# tool", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    spy = _sel_spy(monkeypatch)
    skills = MagicMock()
    skills.load_skill = lambda _name: None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    # No row can answer either, so the shared 404 is the only outcome left.
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: MagicMock(available=lambda: False))

    stale = f"package/{'0' * 32}:tool"
    state = MagicMock(_slots={})
    response = await _prompts.api_skill_detail(_detail_req("GET", stale, state))

    assert response.status == 404, response.text
    outcomes = [
        c.kwargs.get("outcome") or (c.args[2] if len(c.args) > 2 else None)
        for c in spy.log_tool_invocation.call_args_list
    ]
    assert "not_found" in outcomes, f"the stale-key 404 left no SEL line: {outcomes}"


def test_a_qualified_key_never_answers_from_outside_the_checked_candidate_set(
    tmp_path, monkeypatch
):
    """A qualified key resolves to the file whose identity was checked, or to nothing.

    The qualifier is derived over the EXACT relative-path glob, so the shared tier loop's
    nested-leaf tier can only offer a file that was never in the derivation set. Answering
    a qualified key from there rebinds it: the caller selected one bundle's identity and
    received another file.

    It needs the two reads INTERLEAVED, which is why the obvious static fixture proves
    nothing: delete the exact copy up front and the root never enters the collision set at
    all, so the qualifier matches nothing and the resolver already answers None for an
    unrelated reason. The window is between the glob the identity check reads and the
    re-glob that followed it -- exactly what a bundle REPLACED at that path produces. It is
    staged here by handing the resolver a root whose exact copy is visible to the first
    glob and gone from the second, rather than by racing a real replacement.
    """
    root_a = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    (root_a / "shared-skill").mkdir(parents=True)
    (root_a / "shared-skill" / "SKILL.md").write_text("# root A exact", encoding="utf-8")
    # A nested leaf of the SAME name under the same root. The qualifier is never derived
    # over this tier, so it must not be answerable through a qualified key.
    (root_a / "PkgA" / "shared-skill").mkdir(parents=True)
    (root_a / "PkgA" / "shared-skill" / "SKILL.md").write_text("# root A nested", encoding="utf-8")

    root_b = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    (root_b / "shared-skill").mkdir(parents=True)
    (root_b / "shared-skill" / "SKILL.md").write_text("# root B exact", encoding="utf-8")

    _set_edition_roots(monkeypatch, root_a, root_b)
    q = _q(root_a)

    # Precondition: with both reads agreeing, the key resolves to root A's own copy.
    assert _shared._resolve_package_skill_path("shared-skill", qualifier=q) == (
        root_a / "shared-skill" / "SKILL.md"
    ), "precondition: the qualified key must resolve to the checked copy"

    reads: dict[tuple[str, str], int] = {}

    class _ReplacedAfterCheck(type(root_a)):
        """A root whose exact copy is visible once and gone on the next read.

        A subclass rather than a patch of ``Path.glob``: patching that globally reaches
        pytest's own collection and reporting.
        """

        def glob(self, pattern):
            key = (str(self), pattern)
            reads[key] = reads.get(key, 0) + 1
            if pattern == "shared-skill/SKILL.md" and reads[key] > 1:
                return iter(())
            return super().glob(pattern)

    _set_edition_roots(monkeypatch, _ReplacedAfterCheck(str(root_a)), root_b)
    # Patched at the PACKAGE-roots seam, not through ``extra_skills``: that path normalises
    # every root with ``Path(r)`` (:func:`_edition_skill_roots`), which strips the subclass.
    monkeypatch.setattr(
        _shared,
        "_edition_package_roots",
        lambda *_a, **_k: [_ReplacedAfterCheck(str(root_a)), root_b],
    )

    got = _shared._resolve_package_skill_path("shared-skill", qualifier=q)

    # At least ONE read, not two: the fix removes the second, so demanding it would fail
    # on the fixed code for the wrong reason.
    assert (
        reads.get((str(root_a), "shared-skill/SKILL.md"), 0) >= 1
    ), f"vacuous: the resolver never globbed the staged root ({reads})"
    assert (
        got != root_a / "PkgA" / "shared-skill" / "SKILL.md"
    ), "a qualified key answered from the nested tier, outside the checked candidate set"


@pytest.mark.asyncio
async def test_the_listing_does_not_enumerate_the_roots_on_the_event_loop(tmp_path, monkeypatch):
    """The qualification pass walks the skill roots, so it must not run on the event loop.

    ``/api/skills`` is browser-triggerable and the gateway runs ONE event loop, so a
    synchronous enumeration here stalls the heartbeat for as long as the walk takes --
    unbounded in the size of the installed catalog. Every sibling read in this handler
    already offloads, so the hazard is the inconsistency, not the walk itself.

    Asserted by WITNESSING THE THREAD the enumeration runs on, rather than by looking for
    a ``run_in_executor`` call: a source-shape assertion passes the moment the text
    appears and says nothing about what actually executed.
    """
    import threading
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a = tmp_path / "packages" / "PkgA" / "skills"
    root_b = tmp_path / "packages" / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    req = MagicMock(spec=web.Request)
    req.app = {"state": MagicMock(_slots={})}
    req.method = "GET"
    req.headers = {}
    req.query = {}
    req.cookies = {}

    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = _shared.enumerate_skill_catalog

    def _witness(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    # An unrelated gate: the app-slot isolation check has its own tests and would 404 this
    # request double before the walk under test ever runs.
    monkeypatch.setattr(_prompts, "_deny_foreign_app_skill_slot", lambda *a, **kw: None)
    # A REAL package row, so the walk under test is actually reached: the assembly's own
    # rows are mock objects, which the re-key skips and which no listing would carry.
    monkeypatch.setattr(
        _prompts,
        "collect_skills_blocking",
        lambda *a, **kw: [{"key": f"{_shared.PACKAGE_KEY_PREFIX}shared-skill", "name": "shared"}],
    )
    monkeypatch.setattr(_prompts, "enumerate_skill_catalog", _witness, raising=False)
    monkeypatch.setattr(_shared, "enumerate_skill_catalog", _witness)

    resp = await _prompts.api_skills(req)

    assert resp.status == 200, resp.text
    assert seen, "precondition: the listing never enumerated the roots at all"
    assert loop_thread not in seen, (
        "the root enumeration ran on the event loop thread, so a large catalog stalls the "
        f"gateway heartbeat: walked on {seen} and the loop is {loop_thread}"
    )


@_needs_pinned_walk
def test_the_qualifier_does_not_depend_on_which_roots_the_resolver_admits(tmp_path, monkeypatch):
    """The derivation must not shift when the resolver's candidate set is WIDER.

    Resolution is deliberately wider than enumeration -- it also accepts a bare leaf name
    through the nested tier -- so a derivation computed RELATIVE to the candidate set is a
    phantom-row generator: the segment that uniquely named a root among two holders names
    something else among three, and the enumerated key then binds to no root.

    Pinned separately from the invariant test above because it fixes the layout where the
    two readings differ MOST: roots spelled ``packages/<Pkg>/<event>/skills``, where a
    root's distinguishing segment (``PkgA``) and its parent (``eventId-1``) are both
    plausible qualifiers, plus a third root joining ONLY through the nested-leaf glob while
    itself carrying ``PkgA``, plus an alias root the enumerator folds away by resolved
    identity. A shallower fixture cannot separate the two readings.

    It holds because the qualifier is a digest of the root's OWN canonical path.
    """
    pkgs = tmp_path / "packages"
    root_a = pkgs / "PkgA" / "eventId-1" / "skills"
    root_b = pkgs / "PkgB" / "eventId-1" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    nested = pkgs / "PkgC" / "eventId-1" / "skills"
    (nested / "PkgA" / "shared-skill").mkdir(parents=True)
    (nested / "PkgA" / "shared-skill" / "SKILL.md").write_text("# nested", encoding="utf-8")
    alias = pkgs / "PkgA" / "eventId-2" / "skills"
    alias.mkdir(parents=True)
    (alias / "shared-skill").symlink_to(root_a / "shared-skill", target_is_directory=True)

    keys = _package_catalog(tmp_path, monkeypatch, root_a, root_b, nested, alias)
    state = _FakeState()

    sep = _shared._SKILL_KEY_QUALIFIER_SEP
    qualified = [k for k in keys if sep in k[len(_shared.PACKAGE_KEY_PREFIX) :]]
    assert qualified, f"VACUOUS: this layout minted no qualified key at all: {sorted(keys)}"
    assert any(
        v.is_relative_to(nested) for v in keys.values()
    ), "the nested-leaf root contributed no key, so the wider-set case is untested"

    unresolved = [k for k in keys if _shared._resolve_skill_root(k, state, "") is None]
    assert not unresolved, (
        f"enumerated keys that do not resolve: {unresolved} -- the derivation shifted "
        f"between enumeration and resolution; enumerated {sorted(keys)}"
    )


@_needs_pinned_walk
def test_a_colliding_package_row_lists_the_key_the_write_path_accepts(tmp_path, monkeypatch):
    """A listed key must be one the caller can actually write back.

    This is what the editor's recovery instruction depends on. Listing the
    UNQUALIFIED key for a colliding relative path breaks it at both ends: the write
    refuses that key as ambiguous, and the qualified spelling it WOULD accept appeared on
    no read surface, so re-picking from the list could never succeed no matter how many
    times the user tried.

    Asserted end to end rather than by string shape -- that the listed key resolves --
    because a key that merely LOOKS qualified proves nothing about addressability.
    """
    state = _FakeState()
    root_a = tmp_path / "packages" / "PkgA" / "skills"
    root_b = tmp_path / "packages" / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    rows = [
        {"key": "package/shared-skill", "name": "shared", "dir": str(root_a / "shared-skill")},
        {"key": "package/shared-skill", "name": "shared", "dir": str(root_b / "shared-skill")},
    ]
    listed = _shared.qualify_package_rows(rows, state)
    keys = [r["key"] for r in listed]

    assert len(set(keys)) == 2, f"both copies still list one unaddressable key: {keys}"
    catalog = _shared.enumerate_skill_catalog(state)
    for key in keys:
        assert key in catalog, f"a listed key is not in the catalog, so it cannot resolve: {key}"
    # The shared rows must not be rewritten in place: the assembly hands the same list
    # objects to every reader coalesced behind one scan.
    assert [r["key"] for r in rows] == ["package/shared-skill"] * 2, "mutated the shared rows"


def test_a_replaced_bundle_is_not_persisted_under_the_stale_key(tmp_path, monkeypatch):
    """A bundle replaced between the enumeration and the WRITE must not be persisted.

    The qualifier is minted while the catalog is enumerated, and the URI is written some
    time later. Replacing the bundle in that window re-binds the stale key to the
    replacement, so the ``skill://`` URI persisted into the agent config loads a skill
    nobody selected -- and keeps loading it, because agent config is durable. That is the
    residual the write path is supposed to close, and the check has to happen at the
    WRITE: an enumeration-time check cannot see a replacement that has not happened yet.

    The replacement is staged on the DERIVATION -- between ``enumerate_skill_catalog``
    returning and the persist reading it -- rather than on a clock or an allocator, so the
    window is exercised deterministically rather than raced.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    key = f"package/{_q(root_a)}:shared-skill"
    real_enumerate = _shared.enumerate_skill_catalog

    def _enumerate_then_replace(state, session_key=""):
        catalog = real_enumerate(state, session_key)
        # The bundle is REPLACED after its qualifier was minted: same path, new inode.
        import shutil

        shutil.rmtree(root_a / "shared-skill")
        (root_a / "shared-skill").mkdir()
        (root_a / "shared-skill" / "SKILL.md").write_text("# REPLACEMENT", encoding="utf-8")
        return catalog

    monkeypatch.setattr(_shared, "enumerate_skill_catalog", _enumerate_then_replace)

    data: dict = {"resources": []}
    applied, unknown = _shared.apply_skill_mapping(
        data, tmp_path / "agent.json", _FakeState(), [key]
    )

    assert unknown == [key], (
        "the stale key was accepted after its bundle was replaced, so the replacement's "
        f"URI is about to be persisted: applied={applied} unknown={unknown}"
    )
    assert not any(
        "shared-skill" in str(r) for r in data.get("resources") or []
    ), f"the replacement was written into the agent config anyway: {data.get('resources')}"


@_needs_pinned_walk
def test_the_read_endpoint_the_picker_consumes_mints_a_qualified_key(tmp_path, monkeypatch):
    """The consumer loop CLOSES here: the picker's own endpoint qualifies a colliding row.

    This replaces the tracker that asserted the opposite. While no read endpoint derived a
    qualifier, a colliding copy could not be SELECTED at all -- the picker offered the rows a
    read endpoint returned and those carried unqualified keys, which the write path refuses as
    ambiguous. So the addressability this change adds was reachable only by hand-writing a key.

    Asserted through the HANDLER rather than by reading its source: a source-text assertion
    passes on the spelling of whichever helper is called and says nothing about the rows a
    caller receives, which is the only thing the picker consumes.
    """
    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a, root_b = _two_colliding_roots(tmp_path)

    # POSITIVE CONTROL: the write path mints one for this very fixture, so a failure below
    # is the read surface's, not a fixture that never collided.
    minted = _package_catalog(tmp_path, monkeypatch, root_a, root_b)
    qualified = [k for k in minted if _shared._SKILL_KEY_QUALIFIER_SEP in k]
    assert qualified, (
        "precondition: the write path minted no qualified key for a colliding fixture, so "
        f"the assertion below would be vacuous: {sorted(minted)}"
    )

    rows = [
        {"key": "package/shared-skill", "name": "shared", "dir": str(root_a / "shared-skill")},
        {"key": "package/shared-skill", "name": "shared", "dir": str(root_b / "shared-skill")},
    ]
    listed = _shared.qualify_package_rows(rows, _FakeState())
    keys = [r["key"] for r in listed]

    assert len(set(keys)) == 2, f"the picker still receives one unaddressable key: {keys}"
    for key in keys:
        assert (
            _shared._SKILL_KEY_QUALIFIER_SEP in key[len(_shared.PACKAGE_KEY_PREFIX) :]
        ), f"a listed colliding row carries no qualifier, so it cannot be selected: {key}"
    assert (
        _prompts.qualify_package_rows is _shared.qualify_package_rows
    ), "the handler does not route its rows through the qualification pass"


def _swap_scene(tmp_path):
    """A root whose ANCESTOR is replaced by a link after the path was resolved."""
    import shutil

    live = tmp_path / "live"
    (live / "skills" / "my-skill").mkdir(parents=True)
    (live / "skills" / "my-skill" / "SKILL.md").write_text("# real", encoding="utf-8")
    attacker = tmp_path / "attacker"
    (attacker / "skills" / "my-skill").mkdir(parents=True)
    (attacker / "skills" / "my-skill" / "SKILL.md").write_text("# STOLEN", encoding="utf-8")
    # The resolver ran here: `basis` is the canonical path as it stood a moment ago.
    basis = (live / "skills").resolve(strict=True)
    shutil.rmtree(live)
    (tmp_path / "live").symlink_to(attacker, target_is_directory=True)
    return basis


def test_the_captured_root_is_the_one_the_key_names(tmp_path, monkeypatch):
    """The pinned root must be the key's OWN root, not a symlink target's ancestor.

    The capture climbed ``rel``'s depth from the RESOLVED skill directory, so where the
    copy is reached through a link the climb starts inside the target's tree and lands
    on the target's ancestor. The qualifier belongs to the root the key was minted for,
    so the pinned check then compares against a root that never minted it and every
    qualified tree/detail request on that copy 404s while the catalogue still lists it.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    # root_b's copy is reached through a link whose target is DEEPER inside root_b, so
    # climbing from the resolved path lands on the nested holder rather than on root_b.
    (root_b / "shared-skill" / "SKILL.md").unlink()
    (root_b / "shared-skill").rmdir()
    (root_b / "nest" / "actual").mkdir(parents=True)
    (root_b / "nest" / "actual" / "SKILL.md").write_text("# b", encoding="utf-8")
    (root_b / "shared-skill").symlink_to(root_b / "nest" / "actual", True)

    _set_edition_roots(monkeypatch, tmp_path, root_a, root_b)
    captured: list = []
    key = f"package/{_q(root_b)}:shared-skill"
    _shared._resolve_skill_root(key, _FakeState(), identity_out=captured)

    assert captured, f"{key} captured no pinned root at all"
    assert (
        captured[0][0] == root_b.resolve()
    ), f"captured {captured[0][0]} but the key names {root_b.resolve()}"


def test_the_picker_claims_no_bundle_origin_it_cannot_derive(tmp_path):
    """The picker must not render a bundle ORIGIN, because the key carries no name.

    ``GET /api/skills`` now re-keys a colliding package row onto its qualified spelling, so
    such a copy DOES reach the picker. What it does not carry is a bundle NAME: the qualifier
    is a digest of the root's canonical path, and the readable disambiguator is the package
    package field the row already carries -- the same field an origin span would have read,
    which is why a separate origin render would duplicate rather than add.
    """
    src = (
        _shared.Path(__file__).resolve().parents[1]
        / "website"
        / "src"
        / "components"
        / "AgentSkillsEditor.tsx"
    )
    text = src.read_text(encoding="utf-8")

    assert (
        "bundleOrigin" not in text
    ), "an origin derivation is back in the editor; the picker cannot supply the row it needs"
    # The readable-half fallback IS reachable and must stay: a mapped key whose copy is no
    # longer enumerated has no catalog row, so its rel half is all there is to render.
    assert "qualified?.rel" in text, "the readable-half fallback for a mapped key was lost"

    handler = (
        _shared.Path(__file__).resolve().parents[1]
        / "src"
        / "kiro_crew"
        / "dashboard"
        / "handlers"
        / "prompts.py"
    )
    body = handler.read_text(encoding="utf-8")
    at = body.index("async def api_skills(")
    nxt = body.index("\nasync def ", at + 1)
    assert "enumerate_skill_catalog" not in body[at:nxt], (
        "api_skills now enumerates, so the picker CAN offer a qualified key and the "
        "origin render is owed rather than forbidden"
    )


def test_the_stored_qualifier_is_the_keys_own_not_one_minted_while_resolving(tmp_path, monkeypatch):
    """What the read compares against must arrive with the REQUEST, not with the tree.

    A token minted during resolution records whatever is on disk at that instant, so a
    replacement completing just before the mint has its OWN identity stored as the thing to
    compare against -- and the read then approves precisely the substitution the check exists
    to catch. Storing the qualifier the key carried makes the comparison answer "is this
    still the bundle the caller asked for", which is the only question worth asking.

    Driven through the resolver so the assertion is about what it stores, not about a value
    a test hands it. The divergence is staged on the DERIVATION rather than by mutating the
    tree: without one the two forms are indistinguishable, since a freshly minted token
    equals the key's qualifier because validation just proved they match. A real in-place
    upgrade re-spells the token by moving ``st_ctime_ns``, but that clock is coarse enough to
    leave the digest unchanged across a fast delete-and-recreate, so a filesystem-staged
    version of this test passes or fails on timing.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    q = _q(root_a)

    real_token = _shared._root_identity_token
    real_resolve = _shared._resolve_package_skill_path
    REPLACEMENT = "f" * len(q)
    after_validation = {"yes": False}

    def _token(root):
        return REPLACEMENT if after_validation["yes"] else real_token(root)

    def _resolve_then_diverge(rel, canonical=None, qualifier=None):
        out = real_resolve(rel, canonical, qualifier)
        after_validation["yes"] = True
        return out

    monkeypatch.setattr(_shared, "_root_identity_token", _token)
    monkeypatch.setattr(_shared, "_resolve_package_skill_path", _resolve_then_diverge)

    checked: list = []
    got = _shared._resolve_skill_root(
        f"package/{q}:shared-skill", _FakeState(), identity_out=checked
    )

    assert got == (root_a / "shared-skill").resolve(), "precondition: the key must resolve"
    assert checked, "precondition: the resolver must have stored something to compare"
    assert after_validation["yes"] and REPLACEMENT != q, (
        "precondition: validation must have completed and the post-validation identity must "
        "differ, or this test cannot tell the two forms apart"
    )
    _stored_root, stored = checked[0]
    assert stored == q, (
        f"the resolver stored {stored!r} but the key carried {q!r}; a value minted while "
        "resolving lets a replacement supply its own identity as the comparison basis"
    )

    # An unqualified package key carries nothing to bind, so nothing may be stored for it.
    monkeypatch.setattr(_shared, "_root_identity_token", real_token)
    monkeypatch.setattr(_shared, "_resolve_package_skill_path", real_resolve)
    unqualified: list = []
    _shared._resolve_skill_root("package/only-a", _FakeState(), identity_out=unqualified)
    assert not unqualified, (
        "an unqualified key had an identity stored for it, which can only have been minted "
        "from the tree rather than carried by the request"
    )


@_needs_pinned_walk
def test_a_colliding_row_carrying_only_a_file_path_is_still_qualified(tmp_path, monkeypatch):
    """A row may name the ``SKILL.md`` FILE rather than the bundle directory.

    The collision fold keys by directory, so comparing the file path against it missed every
    time and such a row stayed on the unqualified key -- unselectable, which is the phantom
    row this change exists to remove. The sibling test above uses ``dir`` and so cannot
    detect it.
    """
    state = _FakeState()
    root_a = tmp_path / "packages" / "PkgA" / "skills"
    root_b = tmp_path / "packages" / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    # NO ``dir``: only the file path, which is the shape that never matched.
    rows = [
        {
            "key": "package/shared-skill",
            "name": "shared",
            "path": str(root / "shared-skill" / "SKILL.md"),
        }
        for root in (root_a, root_b)
    ]
    listed = _shared.qualify_package_rows(rows, state)
    keys = [r["key"] for r in listed]

    assert len(set(keys)) == 2, f"a path-only colliding row stayed unqualified: {keys}"
    catalog = _shared.enumerate_skill_catalog(state)
    for key in keys:
        assert key in catalog, f"a listed key is not in the catalog, so it cannot resolve: {key}"


@_needs_pinned_walk
def test_a_stale_qualified_row_is_omitted_rather_than_offered(tmp_path, monkeypatch):
    """A qualifier minted while a sibling copy existed must not survive its removal.

    Once the sibling is gone the rel is keyed UNQUALIFIED again, so the held qualifier is
    nobody's key. Passing the row through would let a PATCH persist that spelling and land on
    whichever copy survived -- a write against a skill the user never chose. Omission is the
    only answer that cannot mis-persist.
    """
    state = _FakeState()
    root_a = tmp_path / "packages" / "PkgA" / "skills"
    root_b = tmp_path / "packages" / "PkgB" / "skills"
    for root in (root_a, root_b):
        (root / "shared-skill").mkdir(parents=True)
        (root / "shared-skill" / "SKILL.md").write_text("# shared", encoding="utf-8")
    _set_edition_roots(monkeypatch, root_a, root_b)

    # Mint the qualified key while BOTH copies exist, exactly as a listing would have.
    minted = [k for k in _shared.enumerate_skill_catalog(state) if _q(root_a) in k]
    assert len(minted) == 1, f"precondition: no qualified key was minted for root_a: {minted}"
    held = minted[0]

    # The sibling is removed, so the rel is keyed unqualified and `held` is nobody's key.
    import shutil

    shutil.rmtree(root_b / "shared-skill")
    fresh = _shared.enumerate_skill_catalog(state)
    assert held not in fresh, f"precondition: the qualified key still exists: {held}"

    rows = [{"key": held, "name": "shared", "dir": str(root_a / "shared-skill")}]
    listed = _shared.qualify_package_rows(rows, state)
    keys = [r["key"] for r in listed]

    assert held not in keys, (
        "a stale qualified key was offered back to the caller, so a PATCH would persist a "
        f"spelling no catalog mints: {keys}"
    )


def test_a_row_predating_a_bundle_replacement_is_not_rekeyed(tmp_path, monkeypatch):
    """A bundle replaced while the catalog is assembled must not lend its key to the old row.

    The listing snapshot is taken before the fresh enumeration, so a replacement landing
    between the two leaves a row describing content absent at that path. The
    map is keyed by DIRECTORY, and the replacement occupies the same directory, so the stale
    row matched and received the replacement's qualifier -- a key naming somebody else's
    SKILL.md, which the write path would then persist.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    skill_dir = root_a / "shared-skill"
    row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    real_enumerate = _shared.enumerate_skill_catalog
    swapped: list[str] = []

    def _enumerate_after_replacing(state, session_key=""):
        # The replacement lands BETWEEN the row snapshot and the enumeration, which is the
        # window the finding names. Same path, different inode and content.
        if not swapped:
            (skill_dir / "SKILL.md").unlink()
            (skill_dir / "SKILL.md").write_text("# REPLACEMENT BUNDLE", encoding="utf-8")
            swapped.append("done")
        return real_enumerate(state, session_key)

    monkeypatch.setattr(_shared, "enumerate_skill_catalog", _enumerate_after_replacing)

    out = _shared.qualify_package_rows([row], _FakeState())

    assert swapped, "VACUOUS: the fixture never performed the replacement"
    offered = [str(r.get("key")) for r in out]
    qualified = [k for k in offered if _shared._SKILL_KEY_QUALIFIER_SEP in k]
    assert not qualified, (
        "a row predating the replacement was handed a qualified key for the bundle that "
        f"replaced it: {qualified}"
    )


class _StubLoader:
    """The two sources ``collect_skills_blocking`` reads besides the package rows."""

    def list_skills(self):
        return []

    def catalog_project_skills(self, project_dir):
        return []


def test_a_row_swapped_after_assembly_is_omitted(tmp_path, monkeypatch):
    """Identity must be captured where the row is ASSEMBLED, not re-stat'd at qualification.

    The sibling swap test moves the bundle between the fold's own snapshot and its
    enumeration. This is the EARLIER window, and the one a re-stat inside the fold cannot
    see: assembly hands back a row describing bundle A, the bundle becomes B, and only then
    does qualification run. Both of the fold's own reads then describe B and agree
    vacuously, so a row assembled from A is handed a key naming B -- and because the
    write-side re-derivation also finds B on disk and accepts it, a PATCH of that key
    persists a skill the operator never selected, durably and without an error surface.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    skill_dir = root_a / "shared-skill"
    package_row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    _shared.capture_package_row_identities([package_row])
    rows = _shared.collect_skills_blocking(_StubLoader(), [package_row], None)
    assembled = [r for r in rows if r.get("source") == "package"]
    assert len(assembled) == 1, f"assembly did not produce the package row: {rows}"

    before = _shared._skill_dir_identity(str(skill_dir.resolve()))
    (skill_dir / "SKILL.md").unlink()
    (skill_dir / "SKILL.md").write_text("# REPLACEMENT BUNDLE", encoding="utf-8")
    after = _shared._skill_dir_identity(str(skill_dir.resolve()))
    assert before is not None and before != after, "VACUOUS: the swap changed no identity"

    out = _shared.qualify_package_rows(assembled, _FakeState())

    qualified = [
        str(r.get("key"))
        for r in out
        if _shared._SKILL_KEY_QUALIFIER_SEP in str(r.get("key") or "")
    ]
    assert not qualified, (
        "a row assembled before the replacement was handed a qualified key naming the "
        f"bundle that replaced it: {qualified}"
    )


@_needs_pinned_walk
def test_an_assembled_row_with_no_swap_is_still_rekeyed(tmp_path, monkeypatch):
    """Non-vacuity for the test above: the same assembly path DOES re-key an untouched row.

    Without this, omitting every assembled row would satisfy the assertion above.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    skill_dir = root_a / "shared-skill"
    package_row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    _shared.capture_package_row_identities([package_row])
    rows = _shared.collect_skills_blocking(_StubLoader(), [package_row], None)
    assembled = [r for r in rows if r.get("source") == "package"]
    out = _shared.qualify_package_rows(assembled, _FakeState())

    keys = [str(r.get("key")) for r in out]
    assert len(keys) == 1, keys
    assert _shared._SKILL_KEY_QUALIFIER_SEP in keys[0], f"the row was not re-keyed at all: {keys}"


def test_the_captured_identity_never_reaches_the_listing(tmp_path, monkeypatch):
    """The capture is internal, so it must not appear in what the endpoint returns.

    It carries a device/inode/mtime token for a path on the host, and the rows the fold
    returns are serialized straight to the client.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    skill_dir = root_a / "shared-skill"
    package_row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    _shared.capture_package_row_identities([package_row])
    rows = _shared.collect_skills_blocking(_StubLoader(), [package_row], None)
    assembled = [r for r in rows if r.get("source") == "package"]
    # Non-vacuity: the field must really be on the row, or the absence below proves nothing.
    assert all(
        _shared._PACKAGE_ROW_IDENTITY_FIELD in r for r in assembled
    ), "assembly captured no identity, so this test cannot detect a leak"

    qualified_out = _shared.qualify_package_rows(assembled, _FakeState())
    leaked = [r for r in qualified_out if _shared._PACKAGE_ROW_IDENTITY_FIELD in r]
    assert not leaked, "the qualified path let the private capture field reach the listing"

    # The early returns bypass the fold entirely, so they need their own assertion.
    monkeypatch.setattr(_shared, "_edition_package_roots", lambda *a, **k: [])
    early_out = _shared.qualify_package_rows(assembled, _FakeState())
    leaked = [r for r in early_out if _shared._PACKAGE_ROW_IDENTITY_FIELD in r]
    assert not leaked, "the no-package-roots early return let the field reach the listing"


@_needs_pinned_walk
def test_an_unreplaced_colliding_row_is_still_rekeyed(tmp_path, monkeypatch):
    """Non-vacuity: with no swap the same row DOES get its qualified key.

    Without this the test above passes under a function that re-keys nothing at all.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    skill_dir = root_a / "shared-skill"
    row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    out = _shared.qualify_package_rows([row], _FakeState())

    keys = [str(r.get("key")) for r in out]
    assert len(keys) == 1, keys
    assert _shared._SKILL_KEY_QUALIFIER_SEP in keys[0], f"the row was not re-keyed at all: {keys}"


@_needs_pinned_walk
def test_a_replaced_row_omission_is_audited(tmp_path, monkeypatch):
    """The omission must reach the SEL, which is the only durable record that it happened.

    Dropping the row is a denial with no error surface, so an operator investigating a skill
    that silently stopped being offered has nothing to read unless the event is written. The
    seam patched here is ``kiro_crew.sel.sel`` and not the handlers alias, because the audit
    resolves the singleton through its own module at call time.
    """
    from unittest.mock import MagicMock

    import kiro_crew.sel as _sel_mod

    spy = MagicMock()
    monkeypatch.setattr(_sel_mod, "sel", lambda: spy)

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    skill_dir = root_a / "shared-skill"
    row = {
        "key": "package/shared-skill",
        "name": "shared-skill",
        "source": "package",
        "dir": str(skill_dir),
        "path": str(skill_dir / "SKILL.md"),
    }

    real_enumerate = _shared.enumerate_skill_catalog
    swapped: list[str] = []

    def _enumerate_after_replacing(state, session_key=""):
        if not swapped:
            (skill_dir / "SKILL.md").unlink()
            (skill_dir / "SKILL.md").write_text("# REPLACEMENT BUNDLE", encoding="utf-8")
            swapped.append("done")
        return real_enumerate(state, session_key)

    monkeypatch.setattr(_shared, "enumerate_skill_catalog", _enumerate_after_replacing)

    out = _shared.qualify_package_rows([row], _FakeState(), session_key="sess-1")

    assert swapped, "VACUOUS: the fixture never performed the replacement"
    assert not [
        k for k in (str(r.get("key")) for r in out) if _shared._SKILL_KEY_QUALIFIER_SEP in k
    ]
    calls = spy.log_tool_invocation.call_args_list
    assert calls, "no SEL event was emitted for the omitted replaced row"
    kwargs = calls[0].kwargs
    assert kwargs["outcome"] == "blocked", kwargs
    assert kwargs["session_key"] == "sess-1", kwargs
    # The detail must survive somewhere durable, and ``metadata`` is the field that accepts it.
    assert "package_row_bundle_replaced" in str(kwargs.get("metadata")), kwargs


@pytest.mark.asyncio
async def test_a_non_package_tree_still_lists_a_contained_symlink(tmp_path, monkeypatch):
    """Only ``package/`` keys pay the pinned walk's refusal of contained symlinks.

    The pinned walk drops every symlink, contained ones included, which is the right
    trade where a key names one of several colliding roots. The other three territories
    have no collision to disambiguate, so a link inside the skill folder is an ordinary
    listable file there and the endpoint's own contract says only ESCAPING links are
    omitted. Routing them through the pinned walk makes such a file vanish, and only on
    platforms that support the walk -- so the Windows fallback would disagree.
    """
    from unittest.mock import MagicMock

    from kiro_crew import pinned_fs as _pf
    from kiro_crew.dashboard.handlers import prompts as _prompts

    if not _pf.supports_pinned_tree_walk():
        pytest.skip("no pinned tree walk on this platform, so the branch is unreachable")

    root = tmp_path / "demo"
    root.mkdir()
    (root / "SKILL.md").write_text("# demo", encoding="utf-8")
    (root / "real.md").write_text("# target", encoding="utf-8")
    (root / "link.md").symlink_to(root / "real.md")

    def _resolve(name, state, session_key="", identity_out=None):
        # identity_out stays EMPTY: that is what a non-package key looks like here.
        return root

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    _sel_spy(monkeypatch)

    resp = await _prompts.api_skill_tree(_detail_req("GET", "kiro-user/demo", MagicMock(_slots={})))

    assert resp.status == 200, f"the tree refused a healthy non-package skill ({resp.status})"
    body = resp.text or ""
    assert "real.md" in body, "precondition: the walk listed nothing at all"
    assert "link.md" in body, "a contained symlink vanished from a non-package tree listing"


@pytest.mark.asyncio
async def test_a_hardlinked_package_skill_md_is_not_read_out(tmp_path, monkeypatch):
    """A hardlinked ``SKILL.md`` reached through a ``package/`` key must not be served.

    Canonicalising a hardlink yields the link's OWN path, so a containment check and a
    sensitive-name check both pass while the bytes read belong to whatever inode the
    link shares. Only a descriptor gate that refuses a multiply-linked inode stops it.

    The second half is the scope control: the same file under a SIBLING territory key
    still reads, because only ``package/`` keys are newly routed to this reader. Were
    the gate applied to every key, this branch would change behaviour the grammar
    never claimed, and that assertion is what fails if it ever is.
    """
    import os
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    secret = tmp_path / "credentials"
    secret.write_text("SENTINEL_HARDLINK_LEAK", encoding="utf-8")
    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    os.link(secret, root / "SKILL.md")
    assert (root / "SKILL.md").stat().st_nlink > 1, "fixture did not create a hardlink"

    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: root)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/shared-skill", MagicMock(_slots={}))
    )

    assert "SENTINEL_HARDLINK_LEAK" not in (response.text or "")

    sibling = await _prompts.api_skill_detail(
        _detail_req("GET", "kiro-user/shared-skill", MagicMock(_slots={}))
    )

    # The sibling territories keep the reader they already had, so this pins the gate's
    # SCOPE without asserting that what they serve is desirable: widening it answers 403.
    assert sibling.status == 200, f"sibling territory changed: {sibling.status}"


@pytest.mark.asyncio
async def test_package_keys_refuse_put_and_delete(monkeypatch):
    """An ANSWERED ``package/`` key is read-only, so a write cannot target a core copy.

    GET resolves such a key through the package roots, while ``update_skill`` and
    ``delete_skill`` join it onto a CORE root. Honouring a write would therefore edit
    or delete ``<core>/package/<rel>`` while GET went on serving the packaged file --
    one key naming two skills, with the write landing somewhere the reader was never
    shown. The endpoint refuses the mutation, and the loader must not be reached at
    all: reaching it is the bug, whatever it then returns.

    The key here is genuinely answerable, but the refusal does not depend on that:
    a key NOTHING answers is refused identically (see the stranded-file test below),
    so ``Allow`` is one fixed verb set rather than a per-resource contract.
    """
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    skills = MagicMock()
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    # A package root answers this key, which is what makes the write a divergence.
    monkeypatch.setattr(
        _prompts, "_resolve_skill_root", lambda *_a, **_k: _shared.Path("/pkg/root")
    )
    state = MagicMock(_slots={})

    def _req(method, name):
        r = MagicMock(spec=web.Request)
        r.app = {"state": state}
        r.method = method
        r.match_info = {"name": name}
        r.headers = {}
        r.query = {}
        r.cookies = {}
        r.json = AsyncMock(return_value={"content": "# edited"})
        return r

    for method in ("PUT", "DELETE"):
        response = await _prompts.api_skill_detail(_req(method, "package/some-skill"))
        assert response.status == 405, f"{method}: {response.status} {response.text}"
        assert "read-only" in response.text
        # An answered key really does not accept DELETE, so Allow names GET alone.
        assert response.headers.get("Allow") == "GET"

    assert skills.update_skill.call_count == 0, "PUT reached the core loader"
    assert skills.delete_skill.call_count == 0, "DELETE reached the core loader"

    # Positive control: without this the assertions above would also pass on a handler that
    # refused EVERY mutation. The control key is in neither reserved territory.
    skills.delete_skill.return_value = True
    allowed = await _prompts.api_skill_detail(_req("DELETE", "plain-skill"))
    assert allowed.status == 200, allowed.text
    assert skills.delete_skill.call_count == 1


@pytest.mark.asyncio
async def test_a_hardlinked_package_file_is_not_served_by_the_file_endpoint(tmp_path, monkeypatch):
    """The ``/file`` endpoint must refuse a hardlink the same way the detail read does.

    ``read_skill_file`` canonicalises to the link's OWN path, so containment and the
    by-name sensitive check both pass while the bytes belong to the shared inode. This
    endpoint became reachable for a ``package/`` key with the qualifier grammar.
    """
    import os
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    secret = tmp_path / "credentials"
    secret.write_text("SENTINEL_FILE_ENDPOINT_LEAK", encoding="utf-8")
    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    os.link(secret, root / "notes.md")
    assert (root / "notes.md").stat().st_nlink > 1, "fixture did not create a hardlink"

    _set_edition_roots(monkeypatch, root.parent)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: root)

    def _req(name, rel):
        r = MagicMock(spec=web.Request)
        r.app = {"state": MagicMock(_slots={})}
        r.match_info = {"name": name}
        r.query = {"path": rel}
        r.headers = {}
        return r

    response = await _prompts.api_skill_file(_req("package/shared-skill", "notes.md"))

    assert "SENTINEL_FILE_ENDPOINT_LEAK" not in (response.text or ""), "hardlinked bytes served"
    # 404 on EVERY platform: this key carries no qualifier, so the read took the by-name
    # branch that needs no pinning -- a capability limit is not a verdict it can reach.
    assert response.status == 404, f"the documented refusal code changed: {response.status}"
    assert "skill_read_unsupported" not in (response.text or ""), response.text

    # Positive control: an ordinary file in the same root is still served, so the refusal
    # above is the hardlink and not a handler that refuses every package read.
    (root / "plain.md").write_text("ORDINARY_BODY", encoding="utf-8")
    allowed = await _prompts.api_skill_file(_req("package/shared-skill", "plain.md"))
    assert allowed.status == 200, allowed.text
    assert "ORDINARY_BODY" in allowed.text

    # Sibling-territory control: a non-package key keeps the reader it already had, which
    # is the scoping the detail path asserts too.
    sib = tmp_path / "core" / "shared-skill"
    sib.mkdir(parents=True)
    os.link(secret, sib / "notes.md")
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: sib)
    base = await _prompts.api_skill_file(_req("shared-skill", "notes.md"))
    assert base.status == 200, f"sibling territory changed: {base.status}"


@pytest.mark.asyncio
async def test_an_unpinnable_platform_refuses_a_qualified_read_instead_of_crashing(
    tmp_path, monkeypatch
):
    """A platform without ``dir_fd`` support answers a refusal, never a 500.

    ``os.O_DIRECTORY`` is absent on Windows and ``openat`` raises NotImplementedError where
    ``dir_fd`` is unsupported. A qualified key is minted from a stat digest WITHOUT
    consulting either capability, so a colliding package edition makes this path reachable
    there -- and neither exception is an OSError, so both escaped as a 500.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("body", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((root.parent, "TOKEN"))
        return root

    def _absent(*_a, **_k):
        raise AttributeError("module 'os' has no attribute 'O_DIRECTORY'")

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts.pinned_fs, "open_in_pinned_parent", _absent)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:shared-skill"}
    request.query = {"path": "notes.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    assert response.status != 500, "an unpinnable platform crashed instead of refusing"
    assert response.status == 404, response.status


@pytest.mark.asyncio
async def test_a_qualified_read_cannot_traverse_out_of_its_skill_into_a_sibling(
    tmp_path, monkeypatch
):
    """``path=../<sibling>/<file>`` must be refused, not served.

    The qualified branch opens relative to a descriptor for the PACKAGE ROOT, which sits
    above the skill dir, and ``O_NOFOLLOW`` guards only the final component -- so the kernel
    resolves ``..`` against that descriptor and a sibling skill's file lands INSIDE the root
    the containment check was comparing against.
    """
    import os
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    basis = tmp_path / "pkg"
    skill = basis / "mine"
    sibling = basis / "sibling"
    skill.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mine", encoding="utf-8")
    (sibling / "private.md").write_text("SENTINEL_SIBLING_LEAK", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN"))
        return skill

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {"path": "../sibling/private.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    body = response.text or ""
    assert "SENTINEL_SIBLING_LEAK" not in body, f"sibling traversal served: {body[:200]}"
    assert response.status != 200, f"traversal answered 200: {body[:200]}"
    assert os.path.exists(sibling / "private.md"), "fixture vanished"


@pytest.mark.asyncio
async def test_a_qualified_read_refuses_a_symlinked_directory_that_leaves_the_skill(
    tmp_path, monkeypatch
):
    """A symlinked INTERMEDIATE component must not reach a sibling skill.

    This is the case the traversal-component check cannot see -- the path carries no ``..``
    -- and that ``O_NOFOLLOW`` cannot see either, because it guards only the FINAL
    component. Only anchoring containment on the skill dir rather than the package root
    above it refuses this, which is why the two halves of the fix are not redundant.
    """
    import os
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    basis = tmp_path / "pkg"
    skill = basis / "mine"
    sibling = basis / "sibling"
    skill.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (sibling / "private.md").write_text("SENTINEL_LINKED_SIBLING_LEAK", encoding="utf-8")
    os.symlink(sibling, skill / "hop", target_is_directory=True)

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN"))
        return skill

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {"path": "hop/private.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    body = response.text or ""
    assert "SENTINEL_LINKED_SIBLING_LEAK" not in body, f"linked sibling served: {body[:200]}"
    assert response.status != 200, f"linked traversal answered 200: {body[:200]}"


@pytest.mark.asyncio
async def test_detail_refuses_a_root_whose_descriptor_identity_no_longer_matches(
    tmp_path, monkeypatch
):
    """Detail must re-verify the recorded identity, as ``/file`` already does.

    Resolution records a token for the root it checked; the descriptor reports a different
    one, which is exactly what an in-place swap between resolve and read looks like. Reading
    the pathname back cannot see it, because the swapped path re-resolves to itself.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    basis = tmp_path / "pkg"
    skill = basis / "mine"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("SENTINEL_REPLACEMENT_CONTENT", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN_AT_RESOLUTION"))
        return skill

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN_AFTER_SWAP")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {}
    request.headers = {}

    response = await _prompts.api_skill_detail(request)
    body = response.text or ""
    assert "SENTINEL_REPLACEMENT_CONTENT" not in body, f"served a swapped root: {body[:200]}"


@pytest.mark.asyncio
async def test_tree_refuses_a_root_whose_descriptor_identity_no_longer_matches(
    tmp_path, monkeypatch
):
    """The tree must re-verify the recorded identity, as the read paths already do.

    Walking by pathname after resolution enumerates whatever the name points at NOW, so a
    root swapped for an out-of-root link between the two exposes directory metadata the
    caller was never entitled to.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    basis = tmp_path / "pkg"
    skill = basis / "mine"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mine", encoding="utf-8")
    (skill / "SENTINEL_TREE_ENTRY.md").write_text("x", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN_AT_RESOLUTION"))
        return skill

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN_AFTER_SWAP")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {}
    request.headers = {}

    response = await _prompts.api_skill_tree(request)
    body = response.text or ""
    assert "SENTINEL_TREE_ENTRY" not in body, f"enumerated a swapped root: {body[:200]}"


def test_no_qualified_key_is_minted_where_the_platform_cannot_pin(tmp_path, monkeypatch, caplog):
    """Where the platform has no descriptor walk, a qualified key would list and never open.

    Every read path refuses on that one condition, so minting there would put back the
    phantom row this change removes -- per platform instead of per collision. Minting and
    reading therefore decide capability with the SAME predicate; a name-locking pin covers
    the root and its ancestors only, so it cannot stand in for a traversed descendant.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    root_a, root_b = _two_colliding_roots(tmp_path)
    monkeypatch.setattr(_sh, "skills_dir", lambda: tmp_path / "empty_core")
    monkeypatch.setattr(_sh.Path, "home", lambda: tmp_path / "empty_home")
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_sh.pinned_fs, "supports_pinned_walk", lambda: False)

    with caplog.at_level("WARNING"):
        catalog = _sh.enumerate_skill_catalog(_FakeState())
    records = [r.getMessage() for r in caplog.records]

    qualified = [k for k in catalog if k.startswith("package/") and ":" in k]
    assert qualified == [], f"minted keys no read can answer: {qualified}"
    assert any("no descriptor walk" in r for r in records), records


@pytest.mark.asyncio
async def test_the_tree_walk_cannot_escape_a_root_swapped_after_the_identity_check(
    tmp_path, monkeypatch
):
    """A root swapped to an out-of-root link after the check must yield no entry from it.

    ``os.walk`` follows its own TOP argument even with ``followlinks=False``, and the escape
    re-check fires only for entries that are themselves symlinks -- so walking by pathname
    after the descriptor is closed leaks out-of-root names and sizes.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    basis = tmp_path / "pkg"
    basis.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SENTINEL_OUTSIDE.md").write_text("leak", encoding="utf-8")
    # The root the caller asked for IS the swap: a link out of the package root.
    swapped = basis / "mine"
    os.symlink(outside, swapped, target_is_directory=True)

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN"))
        return swapped

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {}
    request.headers = {}

    response = await _prompts.api_skill_tree(request)
    body = response.text or ""
    assert "SENTINEL_OUTSIDE" not in body, f"walked out of the pinned root: {body[:300]}"


@pytest.mark.asyncio
async def test_a_qualified_read_refuses_a_contained_symlinked_parent_component(
    tmp_path, monkeypatch
):
    """An intermediate component that is a symlink is refused even when it stays IN the root.

    Containment and the realpath check both pass here by construction -- the link's target is
    inside the skill root -- so neither can refuse it. Only descending every parent component
    with ``O_NOFOLLOW`` and opening the final name alone closes the swap window.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    # Only the descriptor walk serves, so without it a refusal of the symlinked parent is
    # indistinguishable from the refusal of everything.
    if not _prompts.pinned_fs.supports_pinned_walk():
        pytest.skip("no descriptor walk, so no served read to contrast the refusal against")

    basis = tmp_path / "pkg"
    skill = basis / "mine"
    real = skill / "real"
    real.mkdir(parents=True)
    (real / "page.md").write_text("SENTINEL_THROUGH_LINKED_PARENT", encoding="utf-8")
    os.symlink(real, skill / "hop", target_is_directory=True)

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((basis, "TOKEN"))
        return skill

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {"path": "hop/page.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    body = response.text or ""
    assert "SENTINEL_THROUGH_LINKED_PARENT" not in body, f"followed a linked parent: {body[:200]}"
    # Positive control on the fixture: the same file IS readable by its real parent, so the
    # refusal above is about the link rather than a path the endpoint cannot serve at all.
    request.query = {"path": "real/page.md"}
    ok = await _prompts.api_skill_file(request)
    assert "SENTINEL_THROUGH_LINKED_PARENT" in (
        ok.text or ""
    ), f"the real parent stopped working, so the test proves nothing: {(ok.text or '')[:200]}"


@pytest.mark.asyncio
async def test_a_rename_locking_platform_refuses_a_qualified_file_read(tmp_path, monkeypatch):
    """Where the pin locks the NAME but there is no ``dir_fd``, the read must be REFUSED.

    The pin covers the pinned directory and its ancestors, so it cannot vouch for a
    descendant swapped to a junction mid-read; serving by name would leave containment to a
    post-open path comparison that races the swap. The refusal must also stay a refusal: the
    descriptor descent raises ``NotImplementedError`` -- not an ``OSError`` -- which once
    escaped the branch handler, the executor await and the endpoint to answer 500.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("body", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((root.parent, "TOKEN"))
        return root

    def _no_dir_fd(*_a, **_k):
        raise NotImplementedError("dir_fd unavailable on this platform")

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")
    monkeypatch.setattr(_prompts.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(_prompts.pinned_fs, "descend_nofollow", _no_dir_fd)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:shared-skill"}
    request.query = {"path": "notes.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    assert response.status != 200, "a by-name descendant read was served"
    assert response.status != 500, f"the refusal escaped as an unhandled error: {response.status}"
    assert "body" not in (response.text or ""), "the file's bytes were served anyway"


@pytest.mark.asyncio
async def test_a_rename_locking_platform_still_refuses_a_hardlinked_qualified_read(
    tmp_path, monkeypatch
):
    """The by-name fallback must refuse a hardlink, exactly as the descriptor path does.

    Reading by name is forced where there is no ``dir_fd``, and a plain read judges only
    the resolved path -- which for a hardlink is its own in-root alias, so containment and
    the sensitive-name check both pass while the bytes belong to the shared inode.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    secret = tmp_path / "credentials"
    secret.write_text("SENTINEL_WINDOWS_QUALIFIED_LEAK", encoding="utf-8")
    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    os.link(secret, root / "notes.md")
    assert (root / "notes.md").stat().st_nlink > 1, "fixture did not create a hardlink"

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((root.parent, "TOKEN"))
        return root

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")
    monkeypatch.setattr(_prompts.pinned_fs, "supports_pinned_walk", lambda: False)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:shared-skill"}
    request.query = {"path": "notes.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    assert "SENTINEL_WINDOWS_QUALIFIED_LEAK" not in (
        response.text or ""
    ), "the by-name fallback served a hardlinked inode"


@pytest.mark.asyncio
async def test_a_failed_package_row_lookup_refuses_rather_than_skipping_the_crosscheck(
    tmp_path, monkeypatch
):
    """A row lookup that raises must not degrade into "serve with no identity check".

    ``row_path`` empty is also what "no row claims this key" looks like, so an exception
    that clears the list silently takes the same branch and the shadow comparison below is
    never reached.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root = tmp_path / "pkg" / "PkgA" / "shared-skill"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("# SENTINEL_UNVALIDATED_BODY", encoding="utf-8")

    class _Mgr:
        def available(self):
            return True

        async def list_skills(self):
            raise RuntimeError("capability manager unavailable")

    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: _Mgr())
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: root)

    response = await _prompts.api_skill_detail(
        _detail_req("GET", "package/shared-skill", MagicMock(_slots={}))
    )

    assert "SENTINEL_UNVALIDATED_BODY" not in (
        response.text or ""
    ), "a failed row lookup served the resolver's answer with no identity comparison"


@pytest.mark.asyncio
async def test_a_nul_in_the_path_query_is_refused_before_anything_resolves(monkeypatch):
    """The NUL must be refused by an explicit test, not by an exception it happens to raise.

    POSIX raises on an embedded NUL and Windows truncates at it, so a guard that relies on
    the exception refuses on one platform and serves on the other. Asserting the resolver
    is never reached is what pins the check ahead of the path rather than behind it.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    calls: list[str] = []

    def _resolve(*_a, **_k):
        calls.append("resolved")
        return None

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/shared-skill"}
    request.query = {"path": "notes.md\x00"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    assert response.status == 400, f"a NUL in the path was not refused: {response.status}"
    assert calls == [], "the NUL reached the resolver instead of being refused ahead of it"


@pytest.mark.asyncio
async def test_the_by_name_tree_walk_refuses_a_root_repointed_after_the_check(
    tmp_path, monkeypatch
):
    """Where there is no ``dir_fd`` the walk is by name, so the top must be re-resolved.

    ``os.walk`` follows the top it is handed and every entry it yields is contained by
    that top, so the escape is invisible to per-entry containment: the root moved, and
    containment is measured against where it moved TO.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SENTINEL_OUTSIDE.md").write_text("leak", encoding="utf-8")
    real = tmp_path / "pkg" / "mine"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("# mine", encoding="utf-8")

    def _resolve(name, state, session_key, identity_out=None):
        if identity_out is not None:
            identity_out.append((real.parent, "TOKEN"))
        # The swap lands AFTER the identity was recorded, which is the window under test.
        real.rename(tmp_path / "pkg" / "moved-away")
        (tmp_path / "pkg" / "mine").symlink_to(outside, target_is_directory=True)
        return real

    monkeypatch.setattr(_prompts, "_resolve_skill_root", _resolve)
    monkeypatch.setattr(_prompts, "_identity_token_from_stat", lambda *_a, **_k: "TOKEN")
    monkeypatch.setattr(_prompts.pinned_fs, "supports_pinned_walk", lambda: False)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/PkgA:mine"}
    request.query = {}
    request.headers = {}

    response = await _prompts.api_skill_tree(request)
    assert "SENTINEL_OUTSIDE" not in (
        response.text or ""
    ), f"the by-name walk followed a repointed root: {response.text}"


@pytest.mark.asyncio
async def test_package_identity_is_never_stat_on_the_event_loop(monkeypatch):
    """A ``resolve()``/``lstat()`` per package row must not run on the event loop.

    Package rows on slow or network storage make that walk unbounded in wall-clock terms,
    and every gateway request and heartbeat shares the thread it would hold. Asserting the
    THREAD is what distinguishes "moved to the pool" from "still inline but quick".
    """
    import threading
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import _shared as _sh
    from kiro_crew.dashboard.handlers import prompts as _prompts

    seen: list[str] = []

    class _Mgr:
        def available(self):
            return True

        async def list_skills(self):
            return [{"key": "package/x", "name": "x", "source": "package", "dir": "/tmp/x"}]

    monkeypatch.setattr(_prompts, "_capability_manager", lambda: _Mgr())

    def _record(rows):
        seen.append(threading.current_thread().name)

    # BOTH bindings, because the pre-fix call site referenced the name imported into the
    # handler module: patching one module only would intercept nothing and prove nothing.
    monkeypatch.setattr(_sh, "capture_package_row_identities", _record)
    if hasattr(_prompts, "capture_package_row_identities"):
        monkeypatch.setattr(_prompts, "capture_package_row_identities", _record)

    await _prompts._assemble_skills_catalog(MagicMock(), None)

    assert seen, "the capture never ran, so this test cannot detect where it ran"
    assert not any(
        n == "MainThread" for n in seen
    ), f"identity capture ran on the event loop thread: {seen}"


def test_package_identity_is_captured_before_any_other_walk_in_the_job():
    """Inside the job the stat comes FIRST, so no other walk widens the window before it.

    That ruling moved this onto the pool; keeping it ahead of the loader's own
    enumeration is what stops the stat drifting late, which is the property the earlier
    capture-window finding cared about.
    """
    from kiro_crew.dashboard.handlers import _shared as _sh

    order: list[str] = []

    class _Loader:
        def list_skills(self):
            order.append("loader")
            return []

    original = _sh.capture_package_row_identities
    try:
        _sh.capture_package_row_identities = lambda rows: order.append("capture")
        _sh.collect_skills_blocking(_Loader(), [], None)
    finally:
        _sh.capture_package_row_identities = original

    assert order[:2] == [
        "capture",
        "loader",
    ], f"the stat did not precede the job's other walks: {order}"


@pytest.mark.asyncio
async def test_a_nul_bearing_path_is_audited_not_just_refused(monkeypatch):
    """A refusal that leaves no SEL record is invisible to the probing it detects.

    Every other rejection on this handler is audited, and a percent-encoded NUL in the path
    query is filesystem probing by construction -- so the refusal has to reach the audit
    seam rather than return above it.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    spy = _sel_spy(monkeypatch)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: None)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/shared-skill"}
    request.query = {"path": "notes.md\x00"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)

    assert response.status == 400, f"the NUL was not refused: {response.status}"
    outcomes = [c.kwargs.get("outcome") for c in spy.log_tool_invocation.call_args_list]
    assert "blocked" in outcomes, f"the NUL refusal emitted no blocked audit event: {outcomes}"


@pytest.mark.asyncio
async def test_a_served_exact_row_read_is_sel_recorded(tmp_path, monkeypatch):
    """The row path's SUCCESS must leave the same record the resolver's success leaves.

    A row keyed by anything other than its root-relative path is unreachable through the
    resolver, so this fallback is its only read path. Auditing every refusal on it while
    letting the serve pass silently means the bytes that actually went out are the one
    outcome an operator cannot see.
    """
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    served_dir = tmp_path / "pkgA" / "skills" / "a-directory-that-is-not-the-key"
    served_dir.mkdir(parents=True)
    (served_dir / "SKILL.md").write_text("# from the exact row", encoding="utf-8")

    key = "package/keyed-by-its-edition"
    row = {
        "key": key,
        "name": "keyed-by-its-edition",
        "source": "package",
        "dir": str(served_dir),
        "path": str(served_dir / "SKILL.md"),
    }

    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    # The resolver globs the remainder against the roots, so it cannot reach this row.
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: None)
    mgr = MagicMock(available=lambda: True)
    mgr.list_skills = AsyncMock(return_value=[row])
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: mgr)

    spy = _sel_spy(monkeypatch)
    served = await _prompts.api_skill_detail(_detail_req("GET", key, MagicMock(_slots={})))

    assert served.status == 200, served.text
    assert "# from the exact row" in (
        served.text or ""
    ), "VACUOUS: the exact-row path never served this read"
    ok_calls = [
        c
        for c in spy.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "ok" and c.kwargs.get("tool_name") == "api_skill_detail"
    ]
    assert ok_calls, (
        "the exact-row serve left no SEL record: "
        f"{[c.kwargs.get('outcome') for c in spy.log_tool_invocation.call_args_list]}"
    )
    assert ok_calls[0].kwargs.get("metadata", {}).get("name") == key, ok_calls[0].kwargs


@pytest.mark.asyncio
async def test_a_rename_proof_pin_does_not_license_a_by_name_descendant_read(tmp_path, monkeypatch):
    """Holding a root does not hold its descendants, so a by-name walk is not authorised.

    The pin blocks a rename of the pinned directory and its ancestors. A descendant swapped
    to a junction between resolution and open is therefore outside it, leaving containment
    to a post-open path comparison -- a check that races the swap it is meant to catch. A
    platform with no descriptor walk must refuse instead of serving.
    """
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")
    skills = MagicMock()
    skills.load_skill.return_value = None
    monkeypatch.setattr(_prompts, "_get_skills", lambda _state: skills)
    monkeypatch.setattr(_prompts, "_capability_manager", lambda: MagicMock(available=lambda: False))

    # The platform under test: no descriptor walk, but a pin that locks the root's own name.
    monkeypatch.setattr(_prompts.pinned_fs, "supports_pinned_walk", lambda: False)

    key = f"package/{_q(root_a)}:shared-skill"
    served = await _prompts.api_skill_detail(_detail_req("GET", key, MagicMock(_slots={})))

    assert served.status != 200, (
        "a by-name descendant read was served where no descriptor walk exists: "
        f"{(served.text or '')[:120]}"
    )
    assert "# from PkgA" not in (served.text or ""), "the file's bytes were served anyway"


def test_the_catalog_never_lists_a_qualified_key_its_own_reads_refuse(tmp_path, monkeypatch):
    """Minting and reading must decide platform capability with ONE predicate.

    A key minted where no read can answer it is the listed-but-unopenable row this grammar
    exists to remove, so a platform whose reads refuse for want of a descriptor walk must
    not have keys minted for it either.
    """
    from kiro_crew.dashboard.handlers import prompts as _prompts

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_home")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_kiro_home")

    # The platform the readers refuse on: no descriptor walk, but a name-locking pin.
    monkeypatch.setattr(_shared.pinned_fs, "supports_pinned_walk", lambda: False)
    monkeypatch.setattr(_prompts.pinned_fs, "supports_pinned_walk", lambda: False)

    catalog = _shared.enumerate_skill_catalog(_FakeState())
    qualified = [k for k in catalog if _shared._SKILL_KEY_QUALIFIER_SEP in str(k)]
    assert (
        not qualified
    ), f"minted {len(qualified)} qualified key(s) every read path refuses: {qualified[:3]}"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_pinned_read_of_a_fifo_does_not_wait_for_a_writer(tmp_path):
    """A bounded worker pool makes a blocking open an availability defect, not a slow read.

    ``O_NOFOLLOW`` does not reject a FIFO, and the read runs on a fixed-size discovery
    executor whose futures cannot be cancelled once started, so an open that waits for a
    writer pins a worker permanently -- four of them take the pool down.
    """
    import os as _os
    import threading

    from kiro_crew.hooks import safe_read_file_bytes_nolink

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    fifo = skill_dir / "notes.md"
    _os.mkfifo(fifo)

    holder = _os.open(str(skill_dir), _os.O_RDONLY)
    done: list[object] = []

    def _read():
        try:
            done.append(
                safe_read_file_bytes_nolink(
                    str(fifo), within_root=str(skill_dir), dir_fd=holder, dir_fd_rel="notes.md"
                )
            )
        except OSError:
            done.append(None)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=5)
    try:
        assert (
            not t.is_alive()
        ), "the pinned open is still waiting for a FIFO writer, so the worker is wedged"
        assert done == [None], f"a FIFO was read as a regular file: {done!r}"
    finally:
        _os.close(holder)


def _reported_roots(tmp_path, monkeypatch, *, nested: bool):
    """Build a reported layout and point enumeration at its three roots."""
    import os

    def mk(p, body):
        p.mkdir(parents=True, exist_ok=True)
        (p / "SKILL.md").write_text(body, encoding="utf-8")

    r1 = tmp_path / "packages" / "PkgA" / "eventId-1" / "skills"
    r2 = tmp_path / "packages" / "PkgB" / "eventId-2" / "skills"
    mk(r1 / "shared-skill", "# from root1")
    mk(r2 / "shared-skill", "# from root2")

    if nested:
        r3 = tmp_path / "packages" / "PkgA" / "eventId-3" / "skills"
        mk(r3 / "PkgA" / "shared-skill", "# from root3 nested leaf")
    else:
        r3 = tmp_path / "packages" / "PkgA" / "eventId-2" / "skills"
        r3.mkdir(parents=True, exist_ok=True)
        os.symlink(str(r1 / "shared-skill"), str(r3 / "shared-skill"))

    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_core")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_home")
    _set_edition_roots(monkeypatch, r1, r2, r3)
    return r1, r2, r3


def _assert_every_package_key_resolves(catalog):
    """Every ``package/`` key must resolve to the directory it was enumerated for."""
    unresolved = []
    for key, path in sorted(catalog.items()):
        if not str(key).startswith("package/"):
            continue
        got = _shared._resolve_skill_root(str(key), _FakeState())
        want = _shared.Path(str(path)).parent.resolve()
        if got is None or _shared.Path(str(got)).resolve() != want:
            unresolved.append((str(key), str(got), str(want)))
    assert not unresolved, "enumerated keys that do not resolve to their own copy: " + repr(
        unresolved
    )


@_needs_pinned_walk
def test_a_nested_leaf_root_beside_a_collision_leaves_every_key_resolvable(tmp_path, monkeypatch):
    """A nested-leaf root must not widen the set the qualifier is derived against.

    The resolver reaches a leaf-name key through its ``*/{name}`` tier, so counting that tier
    as a HOLDER of the walked rel would add a root the fold never counted -- and a segment
    that identified a holder at enumeration time stops being unique, shifting the derivation
    deeper or to ``None`` so the enumerated key answers nothing.
    """
    _reported_roots(tmp_path, monkeypatch, nested=True)
    catalog = _shared.enumerate_skill_catalog(_FakeState())
    assert [k for k in catalog if str(k).startswith("package/")], "fixture enumerated nothing"
    _assert_every_package_key_resolves(catalog)


@_needs_pinned_walk
def test_a_symlink_alias_root_beside_a_collision_leaves_every_key_resolvable(tmp_path, monkeypatch):
    """A root whose copy is an alias of another's is ONE copy, so it cannot add a holder.

    The fold deduplicates by resolved file identity and keys the rel unqualified. A resolver
    that kept the alias as a separate holder would derive against a wider set than the one
    the key was minted from, and the key would answer nothing.
    """
    _reported_roots(tmp_path, monkeypatch, nested=False)
    catalog = _shared.enumerate_skill_catalog(_FakeState())
    assert [k for k in catalog if str(k).startswith("package/")], "fixture enumerated nothing"
    _assert_every_package_key_resolves(catalog)


def test_a_holders_qualifier_does_not_depend_on_who_else_is_in_the_set(tmp_path):
    """One root's qualifier must be the same whoever else holds the rel.

    A segment-derived qualifier is set-dependent: adding a holder that shares the chosen
    segment forces the derivation deeper or to nothing, so a key minted against the narrow
    set stops matching against the wider one. A per-root digest is set-independent, which is
    what makes an enumerated key resolvable no matter which tier the resolver reached. This
    asserts that property directly, because no end-to-end fixture can: widening the set
    changes nothing observable while the derivation stays per-root.
    """
    rel = "shared-skill"

    def root_with(where, body):
        r = tmp_path / where
        (r / rel).mkdir(parents=True)
        (r / rel / "SKILL.md").write_text(body, encoding="utf-8")
        return r

    a = root_with("packages/PkgA/eventId-1/skills", "# a")
    b = root_with("packages/PkgB/eventId-2/skills", "# b")
    # Shares PkgA with *a*: the segment a segment-rule would have picked to tell them apart.
    c = root_with("packages/PkgA/eventId-3/skills", "# c")

    def qualifier_for(root, others):
        entries = [(r, r / rel / "SKILL.md") for r in [root, *others]]
        copies, qualifiers = _shared._package_collision(entries)
        assert qualifiers is not None, "the fixture produced no qualified spelling"
        for (r, _f), q in zip(copies, qualifiers):
            if r == root:
                return q
        raise AssertionError(f"{root} vanished from its own collision set")

    narrow = qualifier_for(a, [b])
    wider = qualifier_for(a, [b, c])
    assert narrow == wider, (
        "a root's qualifier changed when another holder joined the set, so a key minted "
        f"against one set cannot resolve against the other: {narrow} != {wider}"
    )


@pytest.mark.skipif(not hasattr(os, "link"), reason="no hard links on this platform")
def test_enumeration_warns_when_a_package_skill_carries_extra_links(tmp_path, monkeypatch, caplog):
    """The read-time refusal is invisible until it bites, so enumeration states it first.

    A deduplicating install gives a legitimate bundle an inode it shares with a file outside
    the package root, which the reader refuses because it cannot tell that apart from an
    aliased secret. Without a warning at enumeration the skill simply lists and never opens.
    """
    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "shared-skill").mkdir(parents=True)
    md = root / "shared-skill" / "SKILL.md"
    md.write_text("# shared", encoding="utf-8")
    # A second name for the same inode, OUTSIDE the package root: the dedup-install shape.
    os.link(str(md), str(tmp_path / "content-store-copy.md"))

    other = tmp_path / "packages" / "PkgB" / "skills"
    (other / "shared-skill").mkdir(parents=True)
    (other / "shared-skill" / "SKILL.md").write_text("# other", encoding="utf-8")

    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "empty_core")
    monkeypatch.setattr(_shared.Path, "home", lambda: tmp_path / "empty_home")
    _set_edition_roots(monkeypatch, root, other)

    with caplog.at_level("WARNING"):
        _shared.enumerate_skill_catalog(_FakeState())

    said = [r.getMessage() for r in caplog.records if "hard link" in r.getMessage()]
    assert said, [r.getMessage()[:80] for r in caplog.records]
    assert "SKILL.md" in said[0], said[0]
    assert (
        "reflink=never" in said[0] or "copy rather than link" in said[0]
    ), f"the warning does not name the remedy: {said[0]}"


def test_the_fold_serves_no_package_key_that_names_two_rows(tmp_path, monkeypatch):
    """An omitted collision must not leave both rows under one key.

    Where no qualified key can be minted -- no qualifier available, or a platform with no
    descriptor-relative pinned walk -- the fold's re-keying finds nothing for either colliding
    directory and appends each row unchanged. Both then carry the same ``package/<rel>`` key,
    and a mapping write on that key binds whichever copy the resolver reaches first. The key
    names two files, so it is refused rather than served as one of them.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    # The condition under which the catalog omits a collision instead of qualifying it.
    monkeypatch.setattr(_shared.pinned_fs, "supports_pinned_walk", lambda: False)

    rows = []
    for root in (root_a, root_b):
        skill_dir = root / "shared-skill"
        rows.append(
            {
                "key": "package/shared-skill",
                "name": "shared-skill",
                "source": "package",
                "dir": str(skill_dir),
                "path": str(skill_dir / "SKILL.md"),
            }
        )
    _shared.capture_package_row_identities(rows)
    assert len({r["key"] for r in rows}) == 1, "VACUOUS: the two rows do not share a key"

    out = _shared.qualify_package_rows(rows, _FakeState())

    served = [str(r.get("key")) for r in out if str(r.get("key") or "").startswith("package/")]
    assert len(served) == len(
        set(served)
    ), f"one package key names two rows in the listing: {served}"
    assert "package/shared-skill" not in served, (
        "the ambiguous key is still served, so a write on it binds one copy by luck: " f"{served}"
    )


def test_a_hardlink_read_refusal_is_audited_under_its_own_reason(tmp_path, monkeypatch):
    """A deduplicating install is diagnosable, not just refused.

    The refusal is unchanged and the response stays a bare 404 -- separating a hardlink from a
    sensitive path in the reply would make the endpoint an oracle. The audit is server-side, so
    it can name the condition and the remedy where an operator will see them.
    """
    root = tmp_path / "edition" / "skills" / "solo"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("# solo", encoding="utf-8")
    origin = tmp_path / "store" / "NOTES.md"
    origin.parent.mkdir(parents=True)
    origin.write_text("# shared body", encoding="utf-8")
    os.link(origin, root / "NOTES.md")
    assert (root / "NOTES.md").stat().st_nlink > 1, "VACUOUS: the fixture is not a hardlink"

    seen = []

    class _Sel:
        def log_tool_invocation(self, **kw):
            seen.append(kw)

    import kiro_crew.sel as _sel_mod
    from kiro_crew.dashboard.handlers import prompts as _pr

    monkeypatch.setattr(_sel_mod, "sel", lambda: _Sel())
    _pr._audit_package_read_refusal(root / "NOTES.md")

    reasons = [k.get("metadata", {}).get("reason") for k in seen]
    assert "package_read_hardlink_refused" in reasons, f"no audit named the condition: {seen}"
    entry = next(k for k in seen if k["metadata"]["reason"] == "package_read_hardlink_refused")
    assert entry["outcome"] == "blocked", entry
    assert entry["metadata"]["links"] > 1, entry
    assert "reflink" in entry["metadata"]["remedy"], entry

    # Control: an ordinary single-link file inside the same root audits nothing, so the reason
    # above marks the deduplicating case rather than every refusal.
    seen.clear()
    (root / "PLAIN.md").write_text("# plain", encoding="utf-8")
    _pr._audit_package_read_refusal(root / "PLAIN.md")
    assert seen == [], f"a single-link path was audited as a hardlink refusal: {seen}"


@_needs_pinned_walk
def test_every_catalogued_package_key_opens_when_a_copy_is_symlinked(tmp_path, monkeypatch):
    """Membership and openability must agree, and a symlinked copy is the case that split them.

    Every package read opens the leaf with ``O_NOFOLLOW``, so a symlinked ``SKILL.md`` is
    refused however contained its target is. Catalogued anyway it is a listed key that 404s --
    the phantom row this grammar removes, reached from the other side. Two roots vendor the
    rel so the collision path is the one under test; one copy is a link.
    """
    import asyncio
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _pr

    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)

    real = root_a / "shared-skill" / "REAL.md"
    real.write_text("# BODY BEHIND THE LINK", encoding="utf-8")
    linked = root_a / "shared-skill" / "SKILL.md"
    linked.unlink()
    linked.symlink_to(real)
    assert linked.is_symlink(), "VACUOUS: the fixture is not a symlink"

    keys = [
        k
        for k in _shared.enumerate_skill_catalog(_FakeState(), None)
        if k.startswith(_shared.PACKAGE_KEY_PREFIX)
    ]
    assert keys, "VACUOUS: no package key was catalogued, so nothing is under test"

    def _req(name):
        r = MagicMock(spec=web.Request)
        r.app = {"state": MagicMock(_slots={})}
        r.match_info = {"name": name}
        r.query = {"path": "SKILL.md"}
        r.headers = {}
        return r

    unopenable = []
    for key in keys:
        response = asyncio.new_event_loop().run_until_complete(_pr.api_skill_file(_req(key)))
        if response.status != 200:
            unopenable.append((key, response.status))
    assert not unopenable, (
        "the catalog offers package keys the reader refuses, so membership and openability "
        f"disagree: {unopenable}"
    )


def test_aliased_rows_for_one_skill_are_not_read_as_an_ambiguous_key(tmp_path, monkeypatch):
    """One skill reached through two roots is not two skills.

    An edition may advertise both a directory and a symlink into it, so the same resolved file
    arrives as two rows under one key. Counting rows called that ambiguous and dropped the key,
    removing a skill from the listing that no reader could have mis-bound. Genuine ambiguity --
    two DISTINCT resolved skills under one key -- must still be refused, which the second half
    of this test pins.
    """
    root_a, root_b = _two_colliding_roots(tmp_path)
    _set_edition_roots(monkeypatch, root_a, root_b)
    skill_dir = root_a / "shared-skill"

    def _row(dir_path):
        return {
            "key": "package/shared-skill",
            "name": "shared-skill",
            "source": "package",
            "dir": str(dir_path),
            "path": str(dir_path / "SKILL.md"),
        }

    aliases = [_row(skill_dir), _row(skill_dir)]
    assert len({r["key"] for r in aliases}) == 1, "VACUOUS: the rows do not share a key"
    assert len({r["dir"] for r in aliases}) == 1, "VACUOUS: the rows are not aliases"

    kept = _shared._refuse_ambiguous_package_rows(aliases, "")
    served = [str(r.get("key")) for r in kept]
    assert served == [
        "package/shared-skill"
    ], f"aliases of one skill were dropped as an ambiguous key: {served}"

    # The other direction, so the fix did not simply stop refusing: two DISTINCT directories
    # under one key remain a genuine ambiguity and are still refused.
    genuine = [_row(skill_dir), _row(root_b / "shared-skill")]
    assert (
        _shared._refuse_ambiguous_package_rows(genuine, "") == []
    ), "two distinct skills under one key are no longer refused"


@_needs_pinned_walk
def test_a_package_row_no_longer_enumerated_is_omitted_rather_than_listed(tmp_path, monkeypatch):
    """A listed key must be openable, on the unqualified arm as well as the qualified one.

    A package skill whose SKILL.md is a symlink is dropped from the catalogue by the symlink
    floor, so no key -- qualified or not -- resolves it. The row describing it reaches the
    listing from a different source, and keeping it there offers a pick that always 404s.
    """
    state = _FakeState()
    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "linked-skill").mkdir(parents=True)
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# linked", encoding="utf-8")
    (root / "linked-skill" / "SKILL.md").symlink_to(outside)
    # A second, ordinary bundle: the omission must be surgical, not a blanket drop of the arm.
    (root / "plain-skill").mkdir(parents=True)
    (root / "plain-skill" / "SKILL.md").write_text("# plain", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    catalog = _shared.enumerate_skill_catalog(state)
    # Self-validating fixture: if the floor ever stops dropping this leaf the assertion below
    # would pass for the wrong reason, so prove the premise here.
    assert (
        "package/linked-skill" not in catalog
    ), f"fixture is wrong -- the symlinked leaf IS enumerated: {sorted(catalog)}"
    assert "package/plain-skill" in catalog, f"the ordinary bundle vanished: {sorted(catalog)}"

    rows = [
        {
            "key": "package/linked-skill",
            "name": "linked",
            "path": str(root / "linked-skill" / "SKILL.md"),
        },
        {
            "key": "package/plain-skill",
            "name": "plain",
            "path": str(root / "plain-skill" / "SKILL.md"),
        },
    ]
    keys = [r["key"] for r in _shared.qualify_package_rows(rows, state)]

    assert "package/plain-skill" in keys, f"an openable row was dropped: {keys}"
    assert (
        "package/linked-skill" not in keys
    ), f"a row nothing enumerates stayed in the listing, so its key 404s: {keys}"
    for key in keys:
        assert key in catalog, f"a listed key is not in the catalog, so it cannot resolve: {key}"


@_needs_pinned_walk
def test_a_package_row_keyed_unlike_the_catalog_survives_when_its_directory_resolves(
    tmp_path, monkeypatch
):
    """A row is unresolvable only when its DIRECTORY is unenumerated, never merely re-spelled.

    Membership was once tested by key, which drops a row whose key is not the catalogue's
    spelling for the same directory -- an edition-keyed row among them. Every read of that
    directory succeeds, so dropping the row hides an installed skill from the listing.
    """
    state = _FakeState()
    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "plain-skill").mkdir(parents=True)
    (root / "plain-skill" / "SKILL.md").write_text("# plain", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    catalog = _shared.enumerate_skill_catalog(state)
    resolved_dir = os.path.realpath(str(root / "plain-skill"))
    # The premise: this directory IS enumerated, under a key the row below does not carry.
    assert any(
        os.path.realpath(str(md.parent)) == resolved_dir for md in catalog.values()
    ), f"fixture is wrong -- the directory is not enumerated at all: {sorted(catalog)}"
    other_key = "package/renamed-skill"
    assert other_key not in catalog, "fixture is wrong -- the row's key must not be a catalog key"

    rows = [{"key": other_key, "name": "plain", "dir": str(root / "plain-skill")}]
    keys = [r["key"] for r in _shared.qualify_package_rows(rows, state)]

    assert keys == [other_key], (
        f"a row whose directory resolves was dropped because its key was spelled "
        f"differently: {keys}"
    )


@_needs_pinned_walk
def test_the_package_tree_omits_the_files_its_own_reads_refuse(tmp_path, monkeypatch):
    """List membership and openability must agree for an UNQUALIFIED package key too.

    The reader gates every ``package/`` key on aliasing, so a hardlinked or symlinked file
    that stays in the listing is a path whose every read 404s -- the asymmetry this grammar
    exists to remove, reintroduced one route over.
    """
    import os as _os

    root = tmp_path / "packages" / "PkgA" / "skills" / "tool"
    root.mkdir(parents=True)
    (root / "plain.md").write_text("ORDINARY", encoding="utf-8")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("ALIASED", encoding="utf-8")
    _os.link(outside, root / "aliased.md")
    (root / "linked.md").symlink_to(root / "plain.md")
    assert (root / "aliased.md").stat().st_nlink > 1, "fixture did not create a hardlink"

    listed = {e["path"] for e in _shared.list_skill_tree(root, omit_aliased=True)}
    assert "plain.md" in listed, f"an openable file was dropped: {sorted(listed)}"
    assert "aliased.md" not in listed, f"a file every read refuses stayed listed: {sorted(listed)}"
    assert "linked.md" not in listed, f"a symlinked leaf stayed listed: {sorted(listed)}"

    # Scoping control: a core skill's reads are NOT gated, so its listing must not shrink.
    ungated = {e["path"] for e in _shared.list_skill_tree(root)}
    assert "aliased.md" in ungated, f"the omission leaked into ungated callers: {sorted(ungated)}"


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_an_unqualified_package_tree_refuses_a_root_swapped_after_resolution(
    tmp_path, monkeypatch
):
    """The common non-colliding key must not enumerate whatever its root points at now.

    An unqualified key carries no qualifier, so the write-time revalidation has nothing to
    compare -- which is exactly why the listing has to come off a descriptor. Resolution
    hands back a real package path; the swap lands afterwards, so a by-name walk would
    enumerate the target's filenames and sizes under the caller's own key.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    secret_dir = tmp_path / "private"
    secret_dir.mkdir()
    (secret_dir / "id_rsa").write_text("SENTINEL_TREE_DISCLOSURE", encoding="utf-8")

    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("# tool", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    # Resolution answers with a real package path; the root is the attacker's by open time.
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: secret_dir)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/tool"}
    request.headers = {}
    request.query = {}

    response = await _prompts.api_skill_tree(request)
    body = response.text or ""

    assert "id_rsa" not in body, f"a swapped root's filenames were enumerated: {body[:200]}"
    assert response.status != 200, f"a swapped root answered 200: {body[:200]}"


@_needs_pinned_walk
def test_an_aliased_leaf_is_omitted_like_a_symlinked_one(tmp_path, monkeypatch):
    """The two unopenable shapes must be catalogued the same way: not at all.

    A reader refuses both a symlinked ``SKILL.md`` and one carrying a second hard link, so
    cataloguing either offers a key whose every read 404s. Omitting only the symlink left a
    deduplicating install (``cp -al``) listing rows none of its reads could answer.
    """
    import os as _os

    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "plain").mkdir(parents=True)
    (root / "plain" / "SKILL.md").write_text("# plain", encoding="utf-8")
    (root / "aliased").mkdir(parents=True)
    elsewhere = tmp_path / "store" / "content.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("# aliased", encoding="utf-8")
    _os.link(elsewhere, root / "aliased" / "SKILL.md")
    assert (root / "aliased" / "SKILL.md").stat().st_nlink > 1, "fixture made no hard link"
    _set_edition_roots(monkeypatch, root)

    catalog = _shared.enumerate_skill_catalog(_FakeState())

    assert "package/plain" in catalog, f"an openable copy was dropped: {sorted(catalog)}"
    assert (
        "package/aliased" not in catalog
    ), f"an aliased leaf stayed catalogued, so its key 404s: {sorted(catalog)}"


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_an_unqualified_package_read_refuses_a_root_swapped_after_resolution(
    tmp_path, monkeypatch
):
    """``within_root`` is the resolved root itself, so it cannot notice that root changing.

    An unqualified key mints no qualifier, so there is no token to revalidate -- which is why
    the read has to come off a descriptor whose inode is checked to still be package
    territory. Otherwise a root repointed after resolution has its own files served under
    the caller's key, and containment passes because the swapped root IS the root.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    secret_dir = tmp_path / "private"
    secret_dir.mkdir()
    (secret_dir / "secret.md").write_text("SENTINEL_UNQUALIFIED_READ_LEAK", encoding="utf-8")

    root = tmp_path / "packages" / "PkgA" / "skills"
    (root / "tool").mkdir(parents=True)
    (root / "tool" / "SKILL.md").write_text("# tool", encoding="utf-8")
    _set_edition_roots(monkeypatch, root)

    # Resolution answers with a real package path; by read time the root is the attacker's.
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: secret_dir)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/tool"}
    request.query = {"path": "secret.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)
    body = response.text or ""

    assert "SENTINEL_UNQUALIFIED_READ_LEAK" not in body, f"served a swapped root: {body[:200]}"
    assert response.status != 200, f"a swapped root answered 200: {body[:200]}"


@_needs_pinned_walk
@pytest.mark.asyncio
async def test_a_root_that_will_not_resolve_does_not_abort_a_package_read(tmp_path, monkeypatch):
    """One unusable advertised root must not take a request for a usable one down.

    ``Path.resolve`` signals a symlink loop as ``OSError`` on some platforms and as
    ``RuntimeError`` on others, so the territory check has to treat both as "this root is not
    the answer" rather than letting one abort the request. RuntimeError is raised here
    directly, because the platform this runs on reports a real loop as OSError and so cannot
    reach the other arm at all.
    """
    from unittest.mock import MagicMock

    from aiohttp import web

    from kiro_crew.dashboard.handlers import prompts as _prompts

    good = tmp_path / "packages" / "PkgA" / "skills"
    (good / "tool").mkdir(parents=True)
    (good / "tool" / "SKILL.md").write_text("# tool", encoding="utf-8")
    (good / "tool" / "notes.md").write_text("ORDINARY", encoding="utf-8")
    unusable = tmp_path / "unusable"
    unusable.mkdir()

    # Advertised FIRST, so it is reached before the root that does resolve.
    _set_edition_roots(monkeypatch, unusable, good)
    monkeypatch.setattr(_prompts, "_resolve_skill_root", lambda *_a, **_k: good / "tool")

    real_resolve = _shared.Path.resolve

    def _resolve(self, *a, **k):
        if str(self) == str(unusable):
            raise RuntimeError("symlink loop")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(_prompts.Path, "resolve", _resolve)

    request = MagicMock(spec=web.Request)
    request.app = {"state": MagicMock(_slots={})}
    request.match_info = {"name": "package/tool"}
    request.query = {"path": "notes.md"}
    request.headers = {}

    response = await _prompts.api_skill_file(request)

    assert response.status != 500, f"an unresolvable root crashed the read: {response.text}"
    assert "ORDINARY" in (
        response.text or ""
    ), f"the usable root's file was not served: {(response.text or '')[:200]}"

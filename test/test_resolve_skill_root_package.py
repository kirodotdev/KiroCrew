import os

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
    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: list(roots))


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

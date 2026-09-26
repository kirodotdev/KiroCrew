"""Global steering leaf links admitted by the host-bound ``steering.sources`` scope.

A symlink under ``~/.kiro/steering`` pointing at a file outside the home is the
one linked source the essentials collector may read, and only when the
policy ∩ host-profile ruleset names the CANONICAL target. Everything else the
collector already refuses stays refused: project steering links, template
resource links, directory links, non-regular targets, managed state and the
sensitive-path fence. Every tree here is a temporary fixture; nothing reads the
operator's home.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew import member_essential_context as essentials
from kiro_crew.member_essential_context import MemberEssentialContextError

pytestmark = requires_symlinks

_SCOPE = "steering.sources"
_SHARED_BODY = "Shared standard: every public API needs a changelog entry."


def _ruleset(*allow: str, mode: str = "allow", deny: tuple[str, ...] = ()):
    from kiro_crew.platform.governance import ScopedRuleset

    return ScopedRuleset(mode=mode, allow=tuple(allow), deny=deny, matcher="path")


def _ceiling(controls: dict | None):
    """A ceiling carrying *controls* verbatim, built without the policy parser.

    The parser's acceptance of the new ``steering`` key is its own test below;
    building the carrier directly keeps THIS boundary's verdict about the link,
    not about the document grammar.
    """
    from kiro_crew.platform.governance import BootControls, GovernanceCeiling

    if controls is None:
        return None
    return GovernanceCeiling(version=1, boot=BootControls(), controls=controls)


def _install(monkeypatch, controls: dict | None, profile=None, *, profile_raises=False) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform import governance_profiles as gp

    ceiling = _ceiling(controls)

    class _Ctx:
        governance = ceiling

    monkeypatch.setattr(pc, "current_context", lambda: _Ctx())
    if profile_raises:

        def _boom(*a, **k):
            raise RuntimeError("profile store unavailable")

        monkeypatch.setattr(gp, "resolve_active_scope", _boom)
    else:
        monkeypatch.setattr(gp, "resolve_active_scope", lambda *a, **k: profile)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake home with one real global steering file, plus an external standards dir."""
    root = Path(os.path.realpath(tmp_path))
    home = root / "home"
    steering = home / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (home / ".kiro" / "agents").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / ".kiro"))
    monkeypatch.setenv("KIROCREW_HOME", str(root / "crew-home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", home / ".kiro" / "agents")
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", home / ".kiro" / "agents")
    (steering / "local.md").write_text("Local guide: keep functions short.", encoding="utf-8")
    external = root / "external" / "standards"
    external.mkdir(parents=True)
    (external / "shared.md").write_text(_SHARED_BODY, encoding="utf-8")
    (steering / "shared.md").symlink_to(external / "shared.md")
    return SimpleNamespace(root=root, home=home, steering=steering, external=external)


_STEERING_GLOB = ".kiro/steering/**/*.md"


def _write_default_template(tree, name: str = "kirocrew", pattern: str = _STEERING_GLOB) -> Path:
    """The shipped default spec's shape: a global steering glob as a resource."""
    spec = tree.home / ".kiro" / "agents" / f"{name}.json"
    spec.write_text(
        json.dumps({"name": name, "prompt": "Task.", "resources": [f"file://{pattern}"]}),
        encoding="utf-8",
    )
    return spec


def _nest(tree) -> Path:
    """Move the leaf link one REAL directory down: ``steering/team/shared.md``."""
    (tree.steering / "shared.md").unlink()
    team = tree.steering / "team"
    team.mkdir()
    link = team / "shared.md"
    link.symlink_to(tree.external / "shared.md")
    return link


def _global_docs(**kw) -> dict[str, str]:
    return dict(essentials.documents_for_member("kirocrew", None, **kw))


# ── The regression: an explicitly approved external leaf is delivered ──────


def test_authorized_external_leaf_link_is_delivered_with_its_body(tree, monkeypatch):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    docs = _global_docs()
    assert docs[str(tree.steering / "local.md")] == "Local guide: keep functions short."
    # Labelled by its LOGICAL path (the link), read from its canonical target.
    assert docs[str(tree.steering / "shared.md")] == _SHARED_BODY
    assert str(tree.external / "shared.md") not in docs


def test_native_capture_path_delivers_the_same_authorized_leaf(tree, monkeypatch):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    docs = dict(essentials.kiro_launch_documents("kirocrew", None))
    assert docs[str(tree.steering / "shared.md")] == _SHARED_BODY


def test_policy_document_grammar_accepts_the_steering_sources_scope(tree):
    from kiro_crew.platform.governance import ScopedRuleset, parse_policy

    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "steering": {"sources": {"mode": "allow", "allow": [f"{tree.external}/*.md"]}},
        }
    )
    control = ceiling.get(_SCOPE)
    assert isinstance(control, ScopedRuleset)
    assert control.matcher == "path"
    assert control.permits(str(tree.external / "shared.md")).permitted


def _link(tree, name: str, target: Path) -> Path:
    link = tree.steering / name
    link.symlink_to(target)
    return link


def _assert_refused(match: str | None = None, **kw):
    with pytest.raises(MemberEssentialContextError, match=match):
        _global_docs(**kw)


# ── Default deny: nothing about the link carries trust ────────────────────


@pytest.mark.parametrize(
    "controls",
    [
        None,  # no ceiling at all (standalone default)
        {"tools": _ruleset("*")},  # a ceiling silent about steering.sources
        {_SCOPE: _ruleset("/nonexistent-approved-dir/*.md")},  # named, target not listed
    ],
    ids=["ungoverned", "scope-absent", "unapproved-target"],
)
def test_unapproved_or_ungoverned_leaf_link_refuses_the_build(tree, monkeypatch, controls):
    _install(monkeypatch, controls)
    _assert_refused("steering.sources")
    with pytest.raises(MemberEssentialContextError, match="steering.sources"):
        essentials.kiro_launch_documents("kirocrew", None)


def test_policy_and_host_profile_intersect(tree, monkeypatch):
    from kiro_crew.platform.governance import Profile

    policy = {_SCOPE: _ruleset(f"{tree.external}/*.md")}
    narrower = Profile(name="host", controls={_SCOPE: _ruleset(f"{tree.external}/other-*.md")})
    _install(monkeypatch, policy, narrower)
    _assert_refused("profile denies")
    same = Profile(name="host", controls={_SCOPE: _ruleset(f"{tree.external}/*.md")})
    _install(monkeypatch, policy, same)
    assert _global_docs()[str(tree.steering / "shared.md")] == _SHARED_BODY


def test_a_profile_alone_may_govern_the_scope_under_the_unchanged_algebra(tree, monkeypatch):
    from kiro_crew.platform.governance import Profile

    only_profile = Profile(name="host", controls={_SCOPE: _ruleset(f"{tree.external}/*.md")})
    _install(monkeypatch, {"tools": _ruleset("*")}, only_profile)
    assert _global_docs()[str(tree.steering / "shared.md")] == _SHARED_BODY


def test_deny_mode_is_an_open_set_the_operator_chose(tree, monkeypatch):
    outside = tree.root / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text("OUTSIDE", encoding="utf-8")
    _install(monkeypatch, {_SCOPE: _ruleset(mode="deny", deny=(f"{outside}/**",))})
    assert _global_docs()[str(tree.steering / "shared.md")] == _SHARED_BODY
    _link(tree, "leak.md", outside / "leak.md")
    _assert_refused("policy denies")


def test_governance_evaluation_failure_denies(tree, monkeypatch):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")}, profile_raises=True)
    _assert_refused("steering.sources")


def test_the_host_session_is_what_is_evaluated(tree, monkeypatch):
    from kiro_crew.platform import governance_profiles as gp

    seen: list[dict] = []
    real = gp.governance_permits

    def spy(scope, item, **kw):
        seen.append({"scope": scope, "item": item, **kw})
        return real(scope, item, **kw)

    monkeypatch.setattr(gp, "governance_permits", spy)
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    _global_docs()
    assert seen and all(
        s["scope"] == _SCOPE
        and s["item"] == str(tree.external / "shared.md")
        and s["session_key"] == gp.HOST_SESSION_KEY
        and s["fail_closed"] is True
        for s in seen
    )


# ── The canonical target is the subject; an alias spelling is not ─────────


def test_canonical_target_is_authorized_not_the_alias_spelling(tree, monkeypatch):
    alias = tree.root / "alias"
    alias.symlink_to(tree.external, target_is_directory=True)
    _link(tree, "via-alias.md", alias / "shared.md")
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    docs = _global_docs()
    assert docs[str(tree.steering / "via-alias.md")] == _SHARED_BODY
    _install(monkeypatch, {_SCOPE: _ruleset(f"{alias}/*.md")})
    _assert_refused("steering.sources")


# ── Broad approval never reaches sensitive, managed or non-regular targets ─

_BROAD = {_SCOPE: _ruleset("*")}


def test_sensitive_and_managed_targets_stay_refused_under_broad_approval(tree, monkeypatch):
    _install(monkeypatch, _BROAD)
    aws = tree.home / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\naws_secret_access_key = SECRET", encoding="utf-8")
    (tree.steering / "shared.md").unlink()
    _link(tree, "creds.md", aws / "credentials")
    _assert_refused("cannot be read safely")
    (tree.steering / "creds.md").unlink()
    managed = tree.root / "crew-home" / "memory"
    managed.mkdir(parents=True)
    (managed / "notes.md").write_text("MEMBER MEMORY", encoding="utf-8")
    _link(tree, "notes.md", managed / "notes.md")
    _assert_refused("managed memory/member state")


_READER_REFUSAL = "not a regular file, or no longer the approved file"


@pytest.mark.parametrize(
    "kind, reason",
    [
        ("directory", _READER_REFUSAL),
        ("root", _READER_REFUSAL),
        pytest.param(
            "fifo",
            _READER_REFUSAL,
            marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo unavailable"),
        ),
        ("dangling", _READER_REFUSAL),
        ("non-markdown", "linked document or directory"),
    ],
)
def test_non_regular_or_non_leaf_links_are_refused(tree, monkeypatch, kind, reason):
    _install(monkeypatch, _BROAD)
    if kind == "directory":
        _link(tree, "dir.md", tree.external)
    elif kind == "root":
        _link(tree, "root.md", Path(Path(tree.root).anchor))
    elif kind == "fifo":
        os.mkfifo(tree.root / "pipe")
        _link(tree, "pipe.md", tree.root / "pipe")
    elif kind == "dangling":
        _link(tree, "gone.md", tree.root / "external" / "missing.md")
    else:
        _link(tree, "notes.txt", tree.external / "shared.md")
    _assert_refused(reason)


def test_hardlinked_inodes_are_refused_on_both_sides(tree, monkeypatch):
    _install(monkeypatch, _BROAD)
    os.link(tree.external / "shared.md", tree.steering / "hard.md")
    _assert_refused("hard.md")
    (tree.steering / "hard.md").unlink()
    os.link(tree.external / "shared.md", tree.external / "twin.md")
    _assert_refused("hardlinked")


# ── Everything that is not a global steering leaf link is unchanged ────────


def test_project_steering_links_stay_refused_under_broad_approval(tree, monkeypatch):
    _install(monkeypatch, _BROAD)
    project = tree.root / "project"
    (project / ".kiro" / "steering").mkdir(parents=True)
    (project / ".kiro" / "steering" / "team.md").symlink_to(tree.external / "shared.md")
    with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
        essentials.documents_for_member("kirocrew", str(project))


def test_template_resource_links_stay_refused_under_broad_approval(tree, monkeypatch):
    _install(monkeypatch, _BROAD)
    (tree.home / ".kiro" / "agents" / "tmpl.json").write_text(
        json.dumps({"name": "tmpl", "prompt": "Task.", "resources": ["file://linked-guide.md"]}),
        encoding="utf-8",
    )
    (tree.home / "linked-guide.md").symlink_to(tree.external / "shared.md")
    with pytest.raises(MemberEssentialContextError, match="linked-guide.md"):
        essentials.documents_for_member("tmpl", None)


def test_real_files_and_global_steering_directory_links_behave_as_before(tree, monkeypatch):
    _install(monkeypatch, _BROAD)
    (tree.steering / "shared.md").unlink()
    assert _global_docs()[str(tree.steering / "local.md")].startswith("Local guide")
    (tree.steering / "nested").symlink_to(tree.external, target_is_directory=True)
    _assert_refused("linked document or directory")


# ── The logical spelling labels the document and drives its triggers ──────


def test_conditional_leaf_link_keeps_its_logical_label_and_trigger(tree, monkeypatch):
    (tree.external / "manual-shared.md").write_text(
        "---\ninclusion: manual\n---\nMANUAL BODY", encoding="utf-8"
    )
    logical = _link(tree, "team-manual.md", tree.external / "manual-shared.md")
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    assert not any("MANUAL BODY" in body for body in _global_docs().values())
    pointer = _global_docs(conditional_index=True)[f"{logical}#selection"]
    assert "CONDITIONAL GUIDE" in pointer and f"Read {logical}" in pointer
    assert "MANUAL BODY" not in pointer
    # The trigger is the LINK's stem, not the canonical file's.
    by_canonical_stem = _global_docs(conditional_index=True, trigger_text="use #manual-shared")
    assert str(logical) not in by_canonical_stem
    activated = _global_docs(conditional_index=True, trigger_text="use #team-manual")
    assert activated[str(logical)].endswith("MANUAL BODY")


# ── Limits still bind a linked target ─────────────────────────────────────


def test_oversized_linked_target_is_refused_not_truncated(tree, monkeypatch):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    (tree.external / "shared.md").write_bytes(b"x" * (essentials._MAX_SOURCE_BYTES + 1))
    _assert_refused(f"exceeds {essentials._MAX_SOURCE_BYTES} bytes")


def test_document_ceiling_counts_linked_leaves(tree, monkeypatch):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    for i in range(essentials._MAX_DOCUMENTS + 1):
        (tree.external / f"std-{i:03d}.md").write_text(f"standard {i}", encoding="utf-8")
        _link(tree, f"std-{i:03d}.md", tree.external / f"std-{i:03d}.md")
    _assert_refused("too many documents")


# ── A swap after approval cannot redirect the read ─────────────────────────


def _swap_before_read(monkeypatch, tree, swap) -> None:
    """Run *swap* between the authorization and the pinned open of the approved file."""
    canonical = str(tree.external / "shared.md")
    real = essentials.safe_read_file_bytes_nolink
    fired: list[str] = []

    def racing(raw, within_root=None, **kw):
        if raw == canonical and not fired:
            fired.append(raw)
            swap()
        return real(raw, within_root, **kw)

    monkeypatch.setattr(essentials, "safe_read_file_bytes_nolink", racing)


@pytest.fixture
def unapproved(tree):
    outside = tree.root / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET", encoding="utf-8")
    return outside / "secret.md"


def test_link_retargeted_after_approval_still_reads_only_the_approved_file(
    tree, monkeypatch, unapproved
):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})

    def retarget():
        (tree.steering / "shared.md").unlink()
        (tree.steering / "shared.md").symlink_to(unapproved)

    _swap_before_read(monkeypatch, tree, retarget)
    docs = _global_docs()
    assert docs[str(tree.steering / "shared.md")] == _SHARED_BODY
    assert not any("SECRET" in body for body in docs.values())


def test_canonical_leaf_swapped_into_an_unapproved_target_is_refused(tree, monkeypatch, unapproved):
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})

    def swap_leaf():
        (tree.external / "shared.md").rename(tree.external / "shared.md.bak")
        (tree.external / "shared.md").symlink_to(unapproved)

    _swap_before_read(monkeypatch, tree, swap_leaf)
    _assert_refused("no longer the approved file")


def test_canonical_leaf_swapped_into_an_approved_sibling_is_still_refused(tree, monkeypatch):
    """The pin is the exact file, not the parent: a sibling the PATTERN would admit is
    not the file that was authorized, so the read lands on nothing."""
    (tree.external / "sibling.md").write_text("SIBLING", encoding="utf-8")
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})

    def swap_to_sibling():
        (tree.external / "shared.md").rename(tree.external / "shared.md.bak")
        (tree.external / "shared.md").symlink_to(tree.external / "sibling.md")

    _swap_before_read(monkeypatch, tree, swap_to_sibling)
    with pytest.raises(MemberEssentialContextError, match="no longer the approved file") as excinfo:
        _global_docs()
    assert "SIBLING" not in str(excinfo.value)


def test_canonical_ancestor_swapped_after_approval_is_refused(tree, monkeypatch):
    other = tree.root / "other"
    other.mkdir()
    (other / "shared.md").write_text("OTHER TREE", encoding="utf-8")
    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})

    def swap_ancestor():
        tree.external.rename(tree.root / "external" / "standards-moved")
        tree.external.symlink_to(other, target_is_directory=True)

    _swap_before_read(monkeypatch, tree, swap_ancestor)
    _assert_refused("no longer the approved file")


# ── governance_permits: ordinary callers unchanged; absence denies by catalog metadata ─


class TestDenyWhenUngoverned:
    def test_ordinary_callers_keep_the_ungoverned_permit(self, monkeypatch):
        from kiro_crew.platform.governance_profiles import governance_permits

        _install(monkeypatch, None)
        assert governance_permits("tools", "anything").permitted
        _install(monkeypatch, {"apps": _ruleset("x")})
        assert governance_permits("tools", "anything").permitted

    def test_the_catalog_row_carries_the_flag_and_only_this_row(self):
        from kiro_crew.platform.governance import SCOPE_CATALOG, ScopeSpec

        assert ScopeSpec("ruleset").deny_when_ungoverned is False
        flagged = {name for name, spec in SCOPE_CATALOG.items() if spec.deny_when_ungoverned}
        assert flagged == {_SCOPE}

    def test_absence_from_both_levels_denies_through_governance_permits(self, monkeypatch):
        from kiro_crew.platform.governance import Profile
        from kiro_crew.platform.governance_profiles import governance_permits

        for controls, profile in (
            (None, None),
            ({"apps": _ruleset("x")}, None),
            (None, Profile(name="host", controls={"apps": _ruleset("x")})),
        ):
            _install(monkeypatch, controls, profile)
            decision = governance_permits(_SCOPE, "/any/file.md")
            assert decision.permitted is False
            assert decision.layer == "default" and decision.rule == "default"

    def test_resolve_itself_denies_so_policy_explain_agrees_with_the_collector(self):
        """``kirocrew policy explain`` calls ``resolve`` directly; it must not say ALLOWED."""
        from kiro_crew.platform.governance import Profile, resolve

        assert resolve(None, None, "tools", "anything").permitted
        denied = resolve(None, None, _SCOPE, "/any/file.md")
        assert denied.permitted is False and denied.layer == "default"
        silent_policy = _ceiling({"apps": _ruleset("x")})
        silent_profile = Profile(name="host", controls={"apps": _ruleset("x")})
        assert resolve(silent_policy, silent_profile, _SCOPE, "/any/file.md").permitted is False

    def test_either_explicit_tier_may_grant_while_the_other_is_silent(self):
        """Opt-in consumption default, not an enterprise deny floor: a host-controlled
        profile alone, or a policy alone, names the scope and the unchanged algebra
        decides."""
        from kiro_crew.platform.governance import Profile, resolve

        only_profile = Profile(name="host", controls={_SCOPE: _ruleset("/approved/*.md")})
        granted = resolve(None, only_profile, _SCOPE, "/approved/a.md")
        assert granted.permitted and granted.layer == "profile"
        assert resolve(None, only_profile, _SCOPE, "/elsewhere/a.md").permitted is False
        only_policy = _ceiling({_SCOPE: _ruleset("/approved/*.md")})
        granted = resolve(only_policy, None, _SCOPE, "/approved/a.md")
        assert granted.permitted and granted.rule == "rule2-intersect" and granted.layer == "policy"

    def test_explicit_empty_allow_at_the_policy_pins_closed(self):
        from kiro_crew.platform.governance import Profile, resolve

        pinned = _ceiling({_SCOPE: _ruleset(mode="allow")})
        wide_profile = Profile(name="host", controls={_SCOPE: _ruleset("*")})
        denied = resolve(pinned, wide_profile, _SCOPE, "/approved/a.md")
        assert denied.permitted is False and denied.layer == "policy"

    def test_evaluation_error_with_fail_closed_denies(self, monkeypatch):
        from kiro_crew.platform.governance_profiles import governance_permits

        _install(monkeypatch, {_SCOPE: _ruleset("*")}, profile_raises=True)
        decision = governance_permits(_SCOPE, "/x.md", fail_closed=True)
        assert decision.permitted is False

    def test_security_snapshot_does_not_call_the_absent_row_not_restricted(self, monkeypatch):
        from kiro_crew.dashboard.handlers import security as sec

        _install(monkeypatch, {"tools": _ruleset("*")})
        monkeypatch.setattr(sec, "current_context", lambda: SimpleNamespace(governance=None))
        monkeypatch.setattr(sec, "resolve_active_scope", lambda *a, **k: None)
        rows = {row["scope"]: row for row in sec.build_governance_policy_snapshot()["scopes"]}
        steering = rows[_SCOPE]
        assert steering["governed"] is False and steering["source"] == "ungoverned"
        assert steering["deny_when_ungoverned"] is True
        assert "deny_when_ungoverned" not in rows["tools"]


# ── No filesystem probe of a link before validate_file_path admits it ──────


class TestNoFollowOrdering:
    def test_leaf_candidate_predicate_is_purely_lexical(self, monkeypatch):
        def forbidden(*a, **k):
            raise AssertionError("candidate predicate touched the filesystem")

        for name in ("isdir", "isfile", "islink"):
            monkeypatch.setattr(os.path, name, forbidden)
        for name in ("is_dir", "is_file", "is_symlink"):
            monkeypatch.setattr(Path, name, forbidden)
        pieces = Path(".kiro/steering/**/*.md").parts
        assert essentials._leaf_link_candidate("shared.md", pieces, 3) is True
        assert essentials._leaf_link_candidate("shared.md", pieces, 2) is True
        assert essentials._leaf_link_candidate("notes.txt", pieces, 2) is False
        assert essentials._leaf_link_candidate("shared.md", pieces, 1) is False

    def test_validate_file_path_is_the_first_call_that_resolves_the_link(self, tree, monkeypatch):
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        link = tree.steering / "shared.md"
        target = tree.external / "shared.md"
        watched = {str(link), str(target)}
        validated: list[str] = []
        real_validate = essentials.validate_file_path

        def spy_validate(raw):
            validated.append(raw)
            return real_validate(raw)

        monkeypatch.setattr(essentials, "validate_file_path", spy_validate)

        def guard(original, label):
            def probe(path, *a, **k):
                if str(path) in watched and str(link) not in validated:
                    raise AssertionError(f"{label} followed {path} before validate_file_path")
                return original(path, *a, **k)

            return probe

        monkeypatch.setattr(os.path, "isdir", guard(os.path.isdir, "os.path.isdir"))
        monkeypatch.setattr(os.path, "isfile", guard(os.path.isfile, "os.path.isfile"))
        monkeypatch.setattr(Path, "is_file", guard(Path.is_file, "Path.is_file"))
        monkeypatch.setattr(Path, "is_dir", guard(Path.is_dir, "Path.is_dir"))
        monkeypatch.setattr(Path, "exists", guard(Path.exists, "Path.exists"))
        assert _global_docs()[str(link)] == _SHARED_BODY
        assert str(link) in validated

    def test_no_stat_of_a_link_whose_name_the_glob_never_selects(self, tree, monkeypatch):
        _install(monkeypatch, _BROAD)
        link = _link(tree, "notes.txt", tree.external / "shared.md")
        real_validate = essentials.validate_file_path

        def refuse_validate(raw):
            assert raw != str(link), "validate_file_path resolved a link the glob never selects"
            return real_validate(raw)

        monkeypatch.setattr(essentials, "validate_file_path", refuse_validate)

        def guard(original):
            def probe(path, *a, **k):
                assert str(path) != str(link), f"followed {path}"
                return original(path, *a, **k)

            return probe

        monkeypatch.setattr(os.path, "isdir", guard(os.path.isdir))
        monkeypatch.setattr(Path, "is_file", guard(Path.is_file))
        monkeypatch.setattr(Path, "is_dir", guard(Path.is_dir))
        _assert_refused("linked document or directory")

    def test_directory_link_is_never_descended(self, tree, monkeypatch):
        _install(monkeypatch, _BROAD)
        (tree.external / "inner.md").write_text("INNER", encoding="utf-8")
        _link(tree, "dir.md", tree.external)
        listed: list[str] = []
        real_scandir = os.scandir

        def spy(path=".", *a, **k):
            listed.append(str(path))
            return real_scandir(path, *a, **k)

        monkeypatch.setattr(os, "scandir", spy)
        with pytest.raises(MemberEssentialContextError):
            _global_docs()
        assert str(tree.external) not in listed
        assert str(tree.steering / "dir.md") not in listed

    def test_ancestors_are_screened_before_the_leaf_is_probed(self, tree, monkeypatch):
        """``lstat`` on a leaf below a linked ancestor traverses that ancestor -- on
        Windows, an outbound probe when its target is a share -- so the leaf's own
        link test must wait until every ancestor is known to be real."""
        (tree.steering / "via").symlink_to(tree.external, target_is_directory=True)
        below_link = tree.steering / "via" / "shared.md"
        clean = tree.steering / "shared.md"
        calls: list[tuple[str, str]] = []
        real_ancestor = essentials.first_linked_ancestor
        real_leaf = essentials.is_link_or_junction

        def ancestor_screen(path):
            calls.append(("ancestors", str(path)))
            return real_ancestor(path)

        def leaf_probe(path):
            assert ("ancestors", str(path)) in calls, f"probed {path} before its ancestors"
            calls.append(("leaf", str(path)))
            return real_leaf(path)

        monkeypatch.setattr(essentials, "first_linked_ancestor", ancestor_screen)
        monkeypatch.setattr(essentials, "is_link_or_junction", leaf_probe)
        assert essentials._is_global_steering_leaf_link(below_link) is False
        assert ("leaf", str(below_link)) not in calls
        assert essentials._is_global_steering_leaf_link(clean) is True
        assert calls.index(("ancestors", str(clean))) < calls.index(("leaf", str(clean)))
        with pytest.raises(MemberEssentialContextError):
            essentials._read(below_link, tree.home)
        assert ("leaf", str(below_link)) not in calls


# ── Every failure on the governed path is one MemberEssentialContextError ──


class TestErrorTranslation:
    def test_governance_composition_error_is_translated(self, tree, monkeypatch):
        from kiro_crew.platform import governance_profiles as gp
        from kiro_crew.platform.context import PlatformCompositionError

        def boom(*a, **k):
            raise PlatformCompositionError("unknown governed key")

        monkeypatch.setattr(gp, "governance_permits", boom)
        with pytest.raises(MemberEssentialContextError, match="unknown governed key"):
            _global_docs()
        with pytest.raises(MemberEssentialContextError, match="unknown governed key"):
            essentials.kiro_launch_documents("kirocrew", None)

    def test_os_error_during_admission_is_translated_not_skipped(self, tree, monkeypatch):
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        real_validate = essentials.validate_file_path

        def flaky(raw):
            if raw == str(tree.steering / "shared.md"):
                raise FileNotFoundError(2, "vanished", raw)
            return real_validate(raw)

        monkeypatch.setattr(essentials, "validate_file_path", flaky)
        with pytest.raises(MemberEssentialContextError, match="vanished"):
            _global_docs()

    def test_oversized_linked_target_names_the_bound_in_bytes_and_the_target(
        self, tree, monkeypatch
    ):
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        (tree.external / "shared.md").write_bytes(b"x" * (essentials._MAX_SOURCE_BYTES + 1))
        with pytest.raises(MemberEssentialContextError) as excinfo:
            _global_docs()
        message = str(excinfo.value)
        assert f"{essentials._MAX_SOURCE_BYTES} bytes" in message
        assert repr(str(tree.external / "shared.md")) in message
        assert message.count("Essential source") == 1


# ── A real host-bound profile grants, and revoking it takes effect ─────────


class TestProfileStoreHostBind:
    @pytest.fixture
    def store(self, tree, monkeypatch):
        from kiro_crew.platform import context as pc
        from kiro_crew.platform import governance_profiles as gp

        profiles = tree.root / "crew-home" / "profiles"
        profiles.mkdir(parents=True)
        monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
        gp.reset_store()
        monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=None))
        yield profiles
        gp.reset_store()

    @staticmethod
    def _bind_host(profiles: Path, *allow: str) -> None:
        (profiles / "host.json").write_text(
            json.dumps(
                {
                    "name": "host",
                    "bind": {"type": "surface", "id": "host"},
                    "steering": {"sources": {"mode": "allow", "allow": list(allow)}},
                }
            ),
            encoding="utf-8",
        )

    def test_host_profile_grants_and_a_rewritten_profile_revokes(self, tree, store):
        from kiro_crew.platform import governance_profiles as gp

        _assert_refused("steering.sources")
        self._bind_host(store, f"{tree.external}/*.md")
        gp.reset_store()
        assert _global_docs()[str(tree.steering / "shared.md")] == _SHARED_BODY
        self._bind_host(store, f"{tree.external}/other-*.md")
        gp.reset_store()
        _assert_refused("profile denies")

    def test_revocation_between_scan_and_read_refuses(self, tree, store, monkeypatch):
        from kiro_crew.platform import governance_profiles as gp

        self._bind_host(store, f"{tree.external}/*.md")
        gp.reset_store()
        real_matches = essentials._matches
        scanned: list[Path] = []

        def scan_then_revoke(root, pattern):
            found = real_matches(root, pattern)
            if not scanned and tree.steering / "shared.md" in found:
                scanned.extend(found)
                self._bind_host(store)
                gp.reset_store()
            return found

        monkeypatch.setattr(essentials, "_matches", scan_then_revoke)
        _assert_refused("profile denies")
        assert tree.steering / "shared.md" in scanned


# ── The shipped default template's own glob reaches the same admission ─────


class TestDefaultTemplateScan:
    def test_documents_for_member_dedupes_the_resource_scan_of_the_same_link(
        self, tree, monkeypatch
    ):
        _write_default_template(tree)
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        docs = essentials.documents_for_member("kirocrew", None)
        sources = [source for source, _ in docs]
        assert sources.count(str(tree.steering / "shared.md")) == 1
        assert dict(docs)[str(tree.steering / "shared.md")] == _SHARED_BODY

    def test_native_only_resource_scan_delivers_the_authorized_leaf(self, tree, monkeypatch):
        _write_default_template(tree)
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        native = dict(essentials.documents_for_member("kirocrew", None, native_only=True))
        assert native[str(tree.steering / "shared.md")] == _SHARED_BODY
        launch = dict(essentials.kiro_launch_documents("kirocrew", None))
        assert launch[str(tree.steering / "shared.md")] == _SHARED_BODY

    def test_projected_resources_with_cwd_at_home_deliver_the_leaf(self, tree, monkeypatch):
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        definition = {"resources": ["file://.kiro/steering/**/*.md"]}
        docs = essentials.projected_resource_documents(definition, str(tree.home))
        assert docs[str(tree.steering / "shared.md")] == _SHARED_BODY

    def test_project_equal_to_home_scans_the_global_tree_once(self, tree, monkeypatch):
        _write_default_template(tree)
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        docs = essentials.documents_for_member("kirocrew", str(tree.home))
        sources = [source for source, _ in docs]
        assert sources.count(str(tree.steering / "shared.md")) == 1

    def test_unapproved_link_still_fails_the_default_template_scan(self, tree, monkeypatch):
        _write_default_template(tree)
        _install(monkeypatch, {"tools": _ruleset("*")})
        with pytest.raises(MemberEssentialContextError, match="steering.sources"):
            essentials.documents_for_member("kirocrew", None, native_only=True)

    def test_a_project_cwd_link_gets_no_authority_from_the_same_glob(self, tree, monkeypatch):
        _install(monkeypatch, _BROAD)
        project = tree.root / "project"
        (project / ".kiro" / "steering").mkdir(parents=True)
        (project / ".kiro" / "steering" / "team.md").symlink_to(tree.external / "shared.md")
        definition = {"resources": ["file://.kiro/steering/**/*.md"]}
        with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
            essentials.projected_resource_documents(definition, str(project))

    def test_a_link_reached_through_a_linked_steering_subdirectory_is_refused(
        self, tree, monkeypatch
    ):
        _install(monkeypatch, _BROAD)
        (tree.steering / "via").symlink_to(tree.external, target_is_directory=True)
        _write_default_template(tree)
        (tree.home / ".kiro" / "agents" / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "prompt": "Task.",
                    "resources": ["file://.kiro/steering/via/shared.md"],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(MemberEssentialContextError):
            essentials.documents_for_member("kirocrew", None, native_only=True)


# ── One admission rule for a leaf, whichever glob component reaches it ─────

_SURFACES = ("default-template", "native-only", "kiro-launch", "projected")


def _docs_for(surface: str, tree, pattern: str) -> dict[str, str]:
    """Documents from one scanning surface with *pattern* as its declared resource."""
    if surface == "projected":
        return essentials.projected_resource_documents(
            {"resources": [f"file://{pattern}"]}, str(tree.home)
        )
    _write_default_template(tree, pattern=pattern)
    if surface == "native-only":
        return dict(essentials.documents_for_member("kirocrew", None, native_only=True))
    if surface == "kiro-launch":
        return dict(essentials.kiro_launch_documents("kirocrew", None))
    return dict(essentials.documents_for_member("kirocrew", None))


_LEAF_PATTERNS = [
    pytest.param(".kiro/steering/**/shared.md", False, id="literal-after-globstar"),
    pytest.param(".kiro/steering/**/shared.md", True, id="literal-after-globstar-nested"),
    pytest.param(".kiro/steering/*/shared.md", True, id="literal-after-star-nested"),
    pytest.param(".kiro/steering/team/shared.md", True, id="all-literal-nested"),
    pytest.param(_STEERING_GLOB, True, id="wildcard-final-nested"),
]


class TestLeafAdmissionIsOneRule:
    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize("pattern, nested", _LEAF_PATTERNS)
    def test_the_approved_leaf_is_delivered_by_every_component_shape(
        self, tree, monkeypatch, surface, pattern, nested
    ):
        link = _nest(tree) if nested else tree.steering / "shared.md"
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        docs = _docs_for(surface, tree, pattern)
        assert docs[str(link)] == _SHARED_BODY
        assert str(tree.external / "shared.md") not in docs

    def test_the_global_scan_delivers_a_leaf_below_a_real_subdirectory(self, tree, monkeypatch):
        link = _nest(tree)
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        assert _global_docs()[str(link)] == _SHARED_BODY
        assert dict(essentials.kiro_launch_documents("kirocrew", None))[str(link)] == _SHARED_BODY

    @pytest.mark.parametrize("surface", _SURFACES)
    @pytest.mark.parametrize("pattern, nested", _LEAF_PATTERNS)
    def test_a_literal_component_reaches_the_same_ruleset_not_a_blanket_refusal(
        self, tree, monkeypatch, surface, pattern, nested
    ):
        if nested:
            _nest(tree)
        _install(monkeypatch, {"tools": _ruleset("*")})
        with pytest.raises(MemberEssentialContextError, match="steering.sources"):
            _docs_for(surface, tree, pattern)

    def test_a_directory_link_named_by_a_literal_final_is_refused_unlisted(self, tree, monkeypatch):
        _install(monkeypatch, _BROAD)
        (tree.external / "inner.md").write_text("INNER", encoding="utf-8")
        (tree.steering / "shared.md").unlink()
        _link(tree, "dir.md", tree.external)
        _write_default_template(tree, pattern=".kiro/steering/**/dir.md")
        listed: list[str] = []
        real_scandir = os.scandir

        def spy(path=".", *a, **k):
            listed.append(str(path))
            return real_scandir(path, *a, **k)

        monkeypatch.setattr(os, "scandir", spy)
        with pytest.raises(MemberEssentialContextError, match=_READER_REFUSAL):
            essentials.documents_for_member("kirocrew", None, native_only=True)
        assert str(tree.external) not in listed

    def test_a_link_at_a_literal_directory_position_stays_refused(self, tree, monkeypatch):
        _install(monkeypatch, _BROAD)
        (tree.steering / "via").symlink_to(tree.external, target_is_directory=True)
        _write_default_template(tree, pattern=".kiro/steering/via/*.md")
        with pytest.raises(MemberEssentialContextError, match="linked document or directory"):
            essentials.documents_for_member("kirocrew", None, native_only=True)

    def test_an_admitted_leaf_is_captured_without_a_by_name_type_probe(self, tree, monkeypatch):
        _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
        link = tree.steering / "shared.md"

        def guard(original, label):
            def probe(path, *a, **k):
                assert str(path) != str(link), f"{label} probed the admitted link by name"
                return original(path, *a, **k)

            return probe

        monkeypatch.setattr(Path, "is_file", guard(Path.is_file, "Path.is_file"))
        monkeypatch.setattr(Path, "is_dir", guard(Path.is_dir, "Path.is_dir"))
        monkeypatch.setattr(os.path, "isfile", guard(os.path.isfile, "os.path.isfile"))
        docs = _docs_for("native-only", tree, ".kiro/steering/**/shared.md")
        assert docs[str(link)] == _SHARED_BODY

    def test_an_admitted_junction_is_neither_descended_nor_dropped(self, tree, monkeypatch):
        """A Windows junction reports ``is_dir(follow_symlinks=False)`` True. Once a
        link is admitted the walk must hand it to the pinned reader -- which
        refuses a directory -- and never list it or lose it on the way."""
        _install(monkeypatch, _BROAD)
        junction = tree.steering / "junction.md"
        junction.mkdir()
        (junction / "inner.md").write_text("INNER", encoding="utf-8")
        real_link = essentials.is_link_or_junction
        monkeypatch.setattr(
            essentials,
            "is_link_or_junction",
            lambda path: str(path) == str(junction) or real_link(path),
        )
        listed: list[str] = []
        real_scandir = os.scandir

        def spy(path=".", *a, **k):
            listed.append(str(path))
            return real_scandir(path, *a, **k)

        monkeypatch.setattr(os, "scandir", spy)
        with pytest.raises(MemberEssentialContextError, match=_READER_REFUSAL):
            _global_docs()
        assert str(junction) not in listed


@pytest.mark.parametrize("project_template", [False, True])
@pytest.mark.parametrize("native_only", [False, True])
def test_absolute_prompt_cannot_delegate_through_parent_components(
    tree, monkeypatch, project_template, native_only
):
    import json

    _install(monkeypatch, {_SCOPE: _ruleset(f"{tree.external}/*.md")})
    project = tree.root / "project"
    project.mkdir()
    (project / "planted.md").symlink_to(tree.external / "shared.md")
    agents = project / ".kiro" / "agents" if project_template else tree.home / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    prompt = tree.steering / ".." / ".." / ".." / "project" / "planted.md"
    (agents / "traversal-template.json").write_text(
        json.dumps({"name": "traversal-template", "prompt": f"file://{prompt}"}),
        encoding="utf-8",
    )
    with pytest.raises(MemberEssentialContextError, match="outside the admitted document root"):
        essentials.documents_for_member(
            "traversal-template",
            str(project) if project_template else None,
            native_only=native_only,
        )

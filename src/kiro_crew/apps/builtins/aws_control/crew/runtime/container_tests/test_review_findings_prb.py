"""The two findings the GPT lane raised on PR B once its adjudication could run.

Worth recording why they were UPHELD rather than downgraded: the adjudicator said both
rarity records were INCOMPLETE, because the deploy-time controls that would bound each risk
(``templates/crew.yaml``, ``scripts/smc-deploy.sh``) live in the track that deploys and are
absent from this checkout. That is a property of splitting the change, not of the risk, and
the right answer is not to argue the absent files -- it is to make this image hold on its
own. Both fixes below are checkable entirely inside this PR.

**F1, the customer turn route.** It forwards the caller's ``id`` and that id drives the
on-demand transcript fetch, so with a bucket configured one caller can name another's slot
and have that conversation restored onto this task's disk. This process cannot bind the id
to a principal: it has no caller identity to bind to, and a binding written against an
absent identity fails OPEN. So it answers the question it CAN answer -- has the deployment
vouched that one principal reaches this task -- and refuses to START on the pairing that
needs an answer it does not have. A security property the container cannot observe
arrives as a setting, defaulting to the safe value.

**F2, the crew name.** Every check ``install_bundle`` already made was an equality or a
digest. Those prove the manifest agrees with the spec and with the bytes, and none of them
constrains the SHAPE of the agreed name -- so a manifest and spec that both say
``../../x`` passed all four, and ``agents / f"{name}.json"`` then resolved outside
``agents`` and was written by ``copyfile`` as root, before the backend started. The
builder's ``_validated_crew_name`` is the same guard for the same reason; duplicated rather
than shared because this tree is image source the gateway must not import.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from container import common
from container.front.app import build_app
from container.supervisor import bundle as bundle_mod

from .test_supervisor_bundle import build_bundle, make_settings


def _with(settings: common.Settings, **changes) -> common.Settings:
    return settings.__class__(**{**settings.__dict__, **changes})


# ---------------------------------------------------------------------------
# F1: persistent memory needs a declared trust domain
# ---------------------------------------------------------------------------
def test_a_bucket_without_a_declared_trust_domain_refuses_to_start(tmp_path) -> None:
    """The refusal is at START, not per turn.

    A container that answers its port while silently declining to restore history looks
    healthy and is not, which is why ``require_api_key`` refuses at startup too. The
    message has to name the setting, because the operator's next action is to decide
    whether their deployment really is single-principal.
    """
    settings = _with(make_settings(tmp_path), backup_bucket="smc-bucket", single_principal=False)
    with pytest.raises(common.ConfigError) as caught:
        build_app(settings)
    assert "SMC_SINGLE_PRINCIPAL" in str(caught.value)


def test_a_bucket_with_a_declared_trust_domain_starts(tmp_path) -> None:
    """Non-vacuity: the refusal must not have taken persistent memory out entirely.

    Without this, raising unconditionally would satisfy the test above and delete the
    feature.
    """
    settings = _with(make_settings(tmp_path), backup_bucket="smc-bucket", single_principal=True)
    assert build_app(settings) is not None


def test_no_bucket_does_not_excuse_the_declaration(tmp_path) -> None:
    """A missing bucket is NOT a reason to skip the trust-domain declaration.

    Asserting the opposite here is what would make the gap look
    intentional. The first version of the guard read
    ``if settings.backup_bucket and not settings.single_principal``, and the bucket is the
    wrong condition: it decides whether transcripts are DURABLE, while the caller's ``id``
    decides which conversation the turn joins. With no bucket the fetch does nothing, but
    the id still selects a slot on the shared serializer and the backend still returns the
    live session behind it -- so a second caller reusing a first caller's id lands in that
    conversation, with nothing restored and nothing needed.

    Both values are checked so the refusal cannot quietly go back to depending on the
    bucket in either direction.
    """
    for bucket in (None, "smc-bucket"):
        settings = _with(make_settings(tmp_path), backup_bucket=bucket, single_principal=False)
        with pytest.raises(common.ConfigError) as caught:
            build_app(settings)
        assert "SMC_SINGLE_PRINCIPAL" in str(caught.value), f"bucket={bucket!r}"


def test_the_declaration_alone_is_what_lets_it_start(tmp_path) -> None:
    """Non-vacuity, and it pins that the bucket is irrelevant to this decision.

    With the declaration, both bucket settings start. Without it, neither does. That pair is
    the property: the guard is about who can reach the task, not about where transcripts go.
    """
    for bucket in (None, "smc-bucket"):
        settings = _with(make_settings(tmp_path), backup_bucket=bucket, single_principal=True)
        assert build_app(settings) is not None, f"bucket={bucket!r}"


def test_the_safe_default_is_not_claimed() -> None:
    """Silence must mean "not vouched for", because claiming is what unlocks the pairing.

    A default of True would make every hand-built Settings and every future deployment
    assert a property nobody checked, which is the failure direction that matters.
    """
    assert common.Settings.single_principal is False


def test_the_environment_reader_defaults_the_same_direction(monkeypatch) -> None:
    """An absent or unparseable variable must not read as a claim.

    Checked through the loader rather than the dataclass, because that is the path a real
    deployment takes and the two could drift.
    """
    for value in (None, "0", "false"):
        monkeypatch.delenv("SMC_SINGLE_PRINCIPAL", raising=False)
        if value is not None:
            monkeypatch.setenv("SMC_SINGLE_PRINCIPAL", value)
        assert common.load().single_principal is False, value
    monkeypatch.setenv("SMC_SINGLE_PRINCIPAL", "1")
    assert common.load().single_principal is True

    # An unparseable value REFUSES rather than defaulting, which is the loader's own
    # behaviour and stricter than this test first assumed. It is the right direction: a
    # typo in the variable governing this pairing should stop the container rather than
    # silently pick a posture nobody chose.
    monkeypatch.setenv("SMC_SINGLE_PRINCIPAL", "maybe")
    with pytest.raises(common.ConfigError):
        common.load()


# ---------------------------------------------------------------------------
# F2: a crew name is a name
# ---------------------------------------------------------------------------
def _bundle_for(tmp_path: pathlib.Path, crew_name: str) -> common.Settings:
    """Settings whose bundle's manifest, spec and digest all agree on *crew_name*.

    Agreeing is the point: the four checks that ran before this fix are equalities and a
    digest, so a hostile name that is consistent everywhere passes every one of them. Built
    on the suite's own ``build_bundle`` so the bundle shape stays in one place, then the
    name is rewritten in all three files and the digest recomputed.
    """
    build_bundle(tmp_path, crew_name="placeholder")
    settings = make_settings(tmp_path, crew_name=crew_name)
    d = settings.bundle_dir
    spec = json.loads((d / "agent.json").read_text(encoding="utf-8"))
    spec["name"] = crew_name
    (d / "agent.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["crew_name"] = crew_name
    manifest["digest"] = bundle_mod._content_digest(d)
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return settings


@pytest.mark.parametrize("name", ["../../escaped", "..", "a/b", "a\\b", "/abs", ""])
def test_a_crew_name_that_can_address_a_path_is_refused(name, tmp_path) -> None:
    """Refused before any copy, and the victim outside ``agents`` must be untouched."""
    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "kiro" / "escaped.json"
    victim.write_text("original\n", encoding="utf-8")
    settings = _bundle_for(tmp_path, name)

    with pytest.raises(common.ConfigError) as caught:
        bundle_mod.install_bundle(settings, agents_dir=agents)
    assert "crew name" in str(caught.value).lower() or "crew_name" in str(caught.value)
    assert victim.read_text(encoding="utf-8") == "original\n", "the copy escaped agents/"


def test_an_ordinary_crew_name_still_installs(tmp_path) -> None:
    """Non-vacuity: the guard must not have become a blanket refusal."""
    agents = tmp_path / "kiro" / "agents"
    settings = _bundle_for(tmp_path, "frontdesk")
    bundle_mod.install_bundle(settings, agents_dir=agents)
    assert (agents / "frontdesk.json").is_file()


def test_the_equality_checks_alone_would_have_let_it_through(tmp_path) -> None:
    """Why the shape check is needed rather than covered by what was already there.

    The fixture makes manifest, spec and digest agree on a traversal name, so the ONLY
    thing standing between that bundle and a write outside ``agents`` is the shape check --
    not one of the four checks that precede it.
    """
    settings = _bundle_for(tmp_path, "../../escaped")
    d = settings.bundle_dir
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    spec = json.loads((d / "agent.json").read_text(encoding="utf-8"))
    assert manifest["crew_name"] == spec["name"] == "../../escaped"
    assert manifest["digest"] == bundle_mod._content_digest(d)

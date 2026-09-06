"""The structural tag a model-entitlement rejection leaves on its error row.

``chat_runner._model_unentitled_meta`` decides from the exception's
``rejected_model`` / ``advertised`` attributes (set by ``_raise_acp_error``),
never from the prose, whether the terminal error row gets the
``model_unentitled`` kind the frontend turns into fix affordances.
"""

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat_runner import _model_unentitled_meta
from kiro_crew.dashboard.chat_utils import MODEL_UNENTITLED_KIND


def _exc(rejected, advertised):
    e = AcpError("boom", transient=False)
    e.rejected_model = rejected
    e.advertised = advertised
    return e


def test_rejected_id_absent_from_advertised_is_tagged():
    # Bare kind only — the same shape every TRANSIENT_RETRY_KIND append persists.
    # The rejected id and the served list already live in the row's prose.
    meta = _model_unentitled_meta(_exc("auto", ["gpt-5.6-sol", "glm-5"]))
    assert meta == {"kind": MODEL_UNENTITLED_KIND}


def test_rejected_id_present_in_advertised_is_a_capacity_blip_not_entitlement():
    # Same verdict _model_is_unentitled reaches: an advertised model that was
    # rejected is transient, so no fix affordance.
    assert _model_unentitled_meta(_exc("glm-5", ["gpt-5.6-sol", "glm-5"])) is None
    assert _model_unentitled_meta(_exc("GLM-5", ["glm-5"])) is None


def test_unknowable_without_advertised_list():
    assert _model_unentitled_meta(_exc("auto", [])) is None
    assert _model_unentitled_meta(_exc("auto", None)) is None


def test_untagged_exception_yields_none():
    assert _model_unentitled_meta(AcpError("plain")) is None
    assert _model_unentitled_meta(RuntimeError("x")) is None
    assert _model_unentitled_meta(_exc("", ["a"])) is None
    assert _model_unentitled_meta(_exc("   ", ["a"])) is None


def test_verdict_is_the_shared_predicate():
    # The helper defers to client.model_is_unusable, so the two cannot drift.
    from kiro_crew.acp.client import model_is_unusable

    for rejected, adv in [("auto", ["glm-5"]), ("glm-5", ["glm-5"]), ("auto", []), ("auto", None)]:
        expected = {"kind": MODEL_UNENTITLED_KIND} if model_is_unusable(rejected, adv) else None
        assert _model_unentitled_meta(_exc(rejected, adv)) == expected

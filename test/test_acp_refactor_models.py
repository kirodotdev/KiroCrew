"""Characterization of the ACP model helpers: served-model resolution and advisories.

Pins the literal answers of the shared model predicates (``resolve_usable_model``,
``pick_served_default``, ``resolve_pin_spelling``, ``catalog_row_would_drop``,
``model_is_unusable``, ``advertised_model_ids``), the ``DEFAULT_MODEL`` sentinel, and
the claude-agent-acp model-substitution advisory readers. The model ids below are
test INPUTS, never defaults.

The code is reached only through the ``kiro_crew.acp.client`` facade, so these
tests hold unchanged before and after the definitions move to their owner module.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import client as acp_client


def test_default_model_is_the_auto_sentinel():
    assert acp_client.DEFAULT_MODEL == "auto"


@pytest.mark.parametrize(
    "preferred, advertised, expected",
    [
        pytest.param("", ["a"], "", id="empty-inherits-default"),
        pytest.param("auto", None, "", id="auto-unverifiable-is-not-sent"),
        pytest.param("x", None, "x", id="concrete-trusted-when-unknown"),
        pytest.param("auto", ["auto", "b"], "auto", id="auto-served"),
        pytest.param("auto", ["b"], "", id="auto-not-served"),
        pytest.param("B", ["b"], "B", id="usable-keeps-caller-spelling"),
        pytest.param("ns::b", ["b"], "b", id="stale-qualifier-peeled"),
        pytest.param("zz", ["b"], "", id="not-served"),
        pytest.param("x", ["", "  "], "x", id="blank-advertised-means-unknown"),
    ],
)
def test_resolve_usable_model(preferred, advertised, expected):
    assert acp_client.resolve_usable_model(preferred, advertised) == expected


@pytest.mark.parametrize(
    "current, advertised, expected",
    [
        pytest.param("auto", None, "", id="unknown-served-list"),
        pytest.param("", ["b"], "", id="no-echoed-model"),
        pytest.param("b", ["b"], "", id="already-served"),
        pytest.param("ns::b", ["b"], "", id="served-under-peeled-spelling"),
        pytest.param("zz", ["auto", "b"], "auto", id="unserved-prefers-auto"),
        pytest.param("zz", ["b", "c"], "b", id="unserved-takes-first-advertised"),
    ],
)
def test_pick_served_default(current, advertised, expected):
    assert acp_client.pick_served_default(current, advertised) == expected


@pytest.mark.parametrize(
    "model_id, advertised, expected",
    [
        pytest.param("x", None, "", id="nothing-to-resolve-against"),
        pytest.param("B", [" b "], "b", id="case-and-space-folded"),
        pytest.param("ns::b", ["b"], "b", id="qualifier-peeled"),
        pytest.param("ns::b", ["ns::b", "b"], "ns::b", id="verbatim-wins-over-peel"),
        pytest.param("::b", ["b"], "", id="empty-namespace-not-peeled"),
        pytest.param("zz", ["b"], "", id="not-served"),
        pytest.param(
            "global.anthropic.claude-opus-4-8[1m]",
            ["claude-opus-4.8", "claude-opus-4.8[1m]"],
            "claude-opus-4.8[1m]",
            id="provider-id-folds-onto-same-window-spelling",
        ),
        pytest.param(
            "claude-sonnet-4.5", ["claude-sonnet-4-5"], "claude-sonnet-4-5", id="dot-dash-fold"
        ),
    ],
)
def test_resolve_pin_spelling(model_id, advertised, expected):
    assert acp_client.resolve_pin_spelling(model_id, advertised) == expected


@pytest.mark.parametrize(
    "model_id, advertised, expected",
    [
        pytest.param("auto", ["b"], False, id="auto-sentinel-kept"),
        pytest.param("default", ["b"], False, id="default-sentinel-kept"),
        pytest.param("", ["b"], True, id="empty-id-dropped"),
        pytest.param("b", ["b"], False, id="advertised-kept"),
        pytest.param("ns::b", ["b"], False, id="peelable-kept"),
        pytest.param("zz", ["b"], True, id="unserved-dropped"),
        pytest.param("zz", None, False, id="unknown-served-list-keeps-all"),
    ],
)
def test_catalog_row_would_drop(model_id, advertised, expected):
    assert acp_client.catalog_row_would_drop(model_id, advertised) is expected


@pytest.mark.parametrize(
    "model_id, advertised, expected",
    [
        pytest.param("x", None, False, id="unknown-allows"),
        pytest.param("x", [], False, id="empty-allows"),
        pytest.param("X ", ["x"], False, id="case-and-space-insensitive"),
        pytest.param("y", ["x"], True, id="absent-is-unusable"),
    ],
)
def test_model_is_unusable(model_id, advertised, expected):
    assert acp_client.model_is_unusable(model_id, advertised) is expected


@pytest.mark.parametrize(
    "entries, expected",
    [
        pytest.param(
            [{"modelId": "a"}, {"value": "b"}, {"modelId": " "}, "x", {"modelId": 3}],
            ["a", "b"],
            id="defensive-list",
        ),
        pytest.param(({"modelId": "a"},), ["a"], id="tuple-accepted"),
        pytest.param({"modelId": "a"}, [], id="non-list"),
        pytest.param(None, [], id="none"),
    ],
)
def test_advertised_model_ids(entries, expected):
    assert acp_client.advertised_model_ids(entries) == expected


# (error frame, is an advisory, substitute model id)
_ADVISORY_ROWS = [
    pytest.param(
        {
            "code": -32603,
            "data": {
                "details": "Model a is restricted by policy. Using global.anthropic.x[1m] instead."
            },
        },
        True,
        "global.anthropic.x[1m]",
        id="details-dict",
    ),
    pytest.param(
        {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": 'Model "a" is restricted by your organization\'s settings. '
                'Using "b-1". instead'
            },
        },
        True,
        # Quotes are stripped BEFORE trailing punctuation, so the closing quote
        # hidden behind the period survives.
        'b-1"',
        id="quote-before-period-survives",
    ),
    pytest.param(
        {"code": -32603, "data": 'Model a is restricted\nUsing\n "" instead'},
        True,
        None,
        id="string-data-empty-substitute",
    ),
    pytest.param(
        {"code": -32603, "data": "Model a is restricted. Using b insteadly"},
        True,
        None,
        id="advisory-without-word-bounded-instead",
    ),
    pytest.param(
        {"code": -32602, "data": "is restricted. Using x instead"},
        False,
        None,
        id="wrong-code",
    ),
    pytest.param({"code": -32603, "data": {"details": ""}}, False, None, id="empty-details"),
    pytest.param({"sessionId": "s"}, False, None, id="not-an-error-frame"),
    pytest.param("Model a is restricted. Using b instead", False, None, id="non-dict"),
]


@pytest.mark.parametrize("error, is_advisory, substitute", _ADVISORY_ROWS)
def test_model_substitution_advisory(error, is_advisory, substitute):
    assert acp_client._is_model_substitution_advisory(error) is is_advisory
    assert acp_client._substitute_model_from_advisory(error) == substitute


@pytest.mark.parametrize(
    "error, expected",
    [
        pytest.param({"data": {"details": "d"}}, "d", id="details"),
        pytest.param({"data": {"details": None}}, "", id="details-none"),
        pytest.param({"data": "s"}, "s", id="string-data"),
        pytest.param({"data": 5}, "", id="non-str-data"),
        pytest.param({}, "", id="no-data"),
        pytest.param("x", "", id="non-dict"),
    ],
)
def test_extract_advisory_detail(error, expected):
    assert acp_client._extract_advisory_detail(error) == expected

"""Every plan-tree string a plan approval card shows is redacted before its cap.

``asks.plan_view`` builds the card's ``plan`` from KAS's ``workflowPlan``, which a
step's model can write: node ids, labels, agent and model names, an included
recipe's name and path, kept step ids and the plan's own kind / omitted notes all
reach a person, so each goes through the same redactor as the question text.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.kas_workflows import asks

#: A credential-shaped value the redactor names; spelled in pieces so this file
#: is not itself a credential.
SECRET = "sk-" + "proj-" + "a1B2c3D4e5" * 4


def _node(**over: Any) -> dict[str, Any]:
    node: dict[str, Any] = {"nodeId": "a", "type": "step", "task": {"text": "do it"}}
    node.update(over)
    return node


_FIELDS = {
    "nodeId": lambda: {"steps": [_node(nodeId=SECRET)]},
    "id": lambda: {"steps": [{"id": SECRET, "type": "step"}]},
    "agentName": lambda: {"steps": [_node(agentName=SECRET)]},
    "modelId": lambda: {"steps": [_node(modelId=SECRET)]},
    "modelSource": lambda: {"steps": [_node(modelSource=SECRET)]},
    "effortLevel": lambda: {"steps": [_node(effortLevel=SECRET)]},
    "effortSource": lambda: {"steps": [_node(effortSource=SECRET)]},
    "joinPolicy": lambda: {"steps": [_node(type="parallel", joinPolicy=SECRET)]},
    "includedFrom.name": lambda: {"steps": [_node(includedFrom={"name": SECRET})]},
    "includedFrom.path": lambda: {"steps": [_node(includedFrom={"name": "r", "path": SECRET})]},
    "task.text": lambda: {"steps": [_node(task={"text": SECRET})]},
    "child nodeId": lambda: {
        "steps": [{"nodeId": "seq", "type": "sequence", "steps": [_node(nodeId=SECRET)]}]
    },
    "keptStepIds": lambda: {"keptStepIds": [SECRET]},
    "kind": lambda: {"kind": SECRET},
    "omitted": lambda: {"omitted": SECRET},
}


@pytest.mark.parametrize("field", sorted(_FIELDS))
def test_a_secret_in_any_plan_field_never_reaches_the_card(field):
    view = asks.plan_view(_FIELDS[field]())
    shown = json.dumps(view)
    assert SECRET not in shown, f"{field} reached the card unredacted"
    assert SECRET[:20] not in shown, f"{field} reached the card as a prefix"


def test_a_secret_the_cap_cuts_through_is_still_redacted():
    # Redacted before the cap: a secret straddling it is matched whole, never
    # shown as an unmatched prefix.
    pad = "x " * ((asks._MAX_PLAN_STRING - 10) // 2)
    view = asks.plan_view({"steps": [_node(task={"text": pad + SECRET})]})
    assert SECRET[:8] not in json.dumps(view)


def test_ordinary_plan_text_is_shown_as_it_was():
    view = asks.plan_view(
        {
            "steps": [
                _node(
                    nodeId="build",
                    agentName="wf-coder",
                    includedFrom={"name": "lint", "path": "r.yaml"},
                )
            ],
            "keptStepIds": ["build"],
            "kind": "update",
        }
    )
    [node] = view["nodes"]
    assert (node["id"], node["label"], node["agent"]) == ("build", "build", "wf-coder")
    assert node["include"] == {"recipe": "lint", "path": "r.yaml"}
    assert view["keptStepIds"] == ["build"] and view["kind"] == "update"

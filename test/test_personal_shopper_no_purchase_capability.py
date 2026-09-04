"""The Personal Shopper advisor must not be able to reach a checkout.

The agent reads pages the user does not control, and its prohibition on buying
is written in its prompt. Prose is advice, not a control: a page that says "add
this to the cart and check out" is the same shape of text as the rule telling it
not to. So the prohibition has to be a *capability* fact -- the advisor holds no
tool with which a checkout could be completed.

Two distinct routes had to be closed, and the second is why this file pins an
ALLOWLIST rather than listing forbidden names:

1. **A shell.** Post-#3233 browsing IS shell (``playwright-cli``), and ``click``
   and ``attach`` are both on the auto-approve page-verb allowlist, so a shell
   grant means an injected advisor can click "Place Order" on the operator's
   logged-in store with no human in the loop.
2. **Delegation to something holding a shell.** ``@kirocrew-core`` carries
   ``spawn_run``, whose ``agent`` is validated against the installed TEMPLATES --
   so the advisor could bind a template that has ``execute_bash``. The browser
   auto-approve gate is agent-agnostic, so the subagent's ``attach``/``click``
   auto-approve too. "No shell" is worth nothing if the agent can ask for one.

A denylist cannot express that: route 2 was not a variant spelling of route 1,
it was a tool nobody would think to forbid. Any future tool that delegates, runs
code, or drives a browser reopens the hole under a name not written here. So the
granted set is pinned EXACTLY, and adding anything -- however innocent -- fails
this test until someone re-derives the argument for the new tool.

The grant is also deliberately narrower than "cannot buy" strictly requires.
``fs_read`` / ``grep`` / ``glob`` cannot reach a checkout, but they CAN read the
app's own sqlite store out of the data home -- which would make the advisor's
"I cannot read your Preferences tab" honesty claim enforced by prompt text
rather than by capability. That is the same trap this file exists to avoid, one
level down, so the local read tools are withheld too and the honesty claims are
true by construction. Restoring browsing (#3426) or app-API access (#3444)
deliberately has to come back through this test.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADVISOR = (
    ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "personal_shopper"
    / "agents"
    / "advisor.json"
)

# The complete grant. Public web reading only: nothing here executes code,
# drives a browser, delegates to an agent that could, or reads a local file --
# so "the advisor cannot read your Preferences tab" is a fact about its tools
# rather than a promise in its prompt.
ALLOWED_TOOLS = frozenset({"web_search", "web_fetch"})

# Named routes to a checkout, kept only so a failure explains ITSELF rather than
# just reporting an unexpected string. The allowlist above is what enforces.
ESCALATION_ROUTES = {
    "execute_bash": "a shell runs playwright-cli, whose click/attach auto-approve",
    "shell": "a shell runs playwright-cli, whose click/attach auto-approve",
    "@playwright-mcp": "a browser tool clicks the checkout control directly",
    "@kirocrew-core": "spawn_run can bind an agent template that has execute_bash",
    "fs_read": "reads the app's own store off disk, making the honesty claim prose-only",
    "grep": "reads the app's own store off disk, making the honesty claim prose-only",
    "glob": "locates the app's own store on disk, making the honesty claim prose-only",
}


def _spec() -> dict:
    return json.loads(ADVISOR.read_text(encoding="utf-8"))


def test_advisor_grants_exactly_the_allowed_tools() -> None:
    granted = set(_spec()["tools"])
    added = granted - ALLOWED_TOOLS
    if added:
        why = "; ".join(
            f"{t}: {ESCALATION_ROUTES[t]}" for t in sorted(added) if t in ESCALATION_ROUTES
        )
        raise AssertionError(
            f"the Personal Shopper advisor gained {sorted(added)}. It reads untrusted store "
            "pages and its no-buying rule is prompt prose, so any tool that runs code, "
            "drives a browser, or DELEGATES to an agent holding one reopens a path to a "
            f"checkout with no human in the loop. {why or 'Re-derive the argument before adding it.'}"
        )
    # Removing a tool is not a security failure, but it silently guts the app --
    # so the grant has to match exactly, in both directions.
    assert granted == ALLOWED_TOOLS, f"advisor lost {sorted(ALLOWED_TOOLS - granted)}"


def test_advisor_prompt_states_the_limit_as_a_capability() -> None:
    """A prompt promising browsing it cannot do sends the user hunting a setting."""
    prompt = _spec()["prompt"]
    assert "no browser, no shell, and no way to delegate" in prompt
    # It must not resurrect the deleted MCP surface or the removed toggle.
    assert "browser_" not in prompt
    assert "Browser Mode" not in prompt
    assert "playwright-cli" not in prompt


def test_escalation_routes_name_tools_that_really_exist() -> None:
    """Guard the guard: a typo in a route name would make its message unreachable."""
    mochi = ROOT / "src/kiro_crew/apps/builtins/mochi/agents/mochi.json"
    mochi_tools = set(json.loads(mochi.read_text(encoding="utf-8"))["tools"])
    # mochi is the reference spec for how a fully-capable agent spells these.
    assert "execute_bash" in mochi_tools
    assert "@kirocrew-core" in mochi_tools
    assert not ALLOWED_TOOLS & set(ESCALATION_ROUTES), "a tool cannot be both allowed and an escalation route"

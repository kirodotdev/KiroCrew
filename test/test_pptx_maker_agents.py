"""The chat-mode agent names the frontend sends must be DECLARED agent names.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer``.

This guards a failure that is invisible at runtime: the value the page hands to
``createChatSlot`` is stored on the slot verbatim, and dispatch resolves it via
``config.loader.resolve_agent_bindings`` -> ``_materialized_kiro_agent``, whose
snapshot is keyed on each registered config's ``name`` field
(``_scan_materialized_agents``). An unknown value matches nothing there and
resolution FALLS BACK to the default agent instead of erroring — so a wrong
string opens a plain chat with none of this app's MCP tools or prompt, while
looking like it worked. The ``{app}--{agent}`` stem the registrar writes is only
the on-disk FILENAME, never a dispatchable identifier, and the slash form is a
display namespace; both are pinned rejected below because each has shipped as
this exact silent bug once already.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Anchored on the REPO ROOT, derived from this file, never on the CWD. A
# CWD-relative path resolves differently under `pytest -n auto` (each xdist worker
# can start elsewhere), which made these pass locally and fail with
# `FileNotFoundError` in the sharded run.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "pptx_maker"
_PAGE = _REPO_ROOT / "website" / "src" / "apps" / "pptx-maker" / "PptxMakerPage.tsx"


def _declared_agent_names() -> list[str]:
    """The ``name`` of every agent the manifest declares, in manifest order.

    Every shipped config must declare a ``name``: it is framework-owned
    (``bridges._FRAMEWORK_OWNED_AGENT_KEYS``) and refreshed from the template on
    every registration. Asserted here rather than subscripted so a nameless
    config fails with the reason — for such a config the registered filename
    STEM becomes the dispatchable identifier (``_scan_materialized_agents``'s
    fallback), and the negative guards below would need re-gating.
    """
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    names = []
    for rel in manifest.get("agents") or []:
        data = json.loads((_APP_DIR / rel).read_text(encoding="utf-8"))
        name = data.get("name")
        assert isinstance(name, str) and name, (
            f"{rel} declares no `name`; its registered filename stem would be the "
            "dispatchable identifier — update this test's guards before shipping that"
        )
        names.append(name)
    return names


def _page_chat_agents() -> list[str]:
    """The `CHAT_AGENTS` tuple as the page actually spells it."""
    if not _PAGE.is_file():
        # A python-only checkout (sdist, or a backend-only CI job) has no `website/`.
        # Skip rather than fail: the guard is about frontend/backend agreement and
        # there is no frontend to disagree with. Same posture as the e2e gate.
        pytest.skip("no website/ checkout — nothing to compare against")
    source = _PAGE.read_text(encoding="utf-8")
    block = re.search(r"const CHAT_AGENTS = \[(.*?)\] as const", source, re.S)
    assert block, "CHAT_AGENTS is no longer a literal tuple — update this test"
    return re.findall(r"'([^']+)'", block.group(1))


class TestChatAgentNamesResolve:
    def test_every_chat_agent_is_a_declared_name(self) -> None:
        """The `name` field is what `_scan_materialized_agents` makes dispatchable."""
        declared = set(_declared_agent_names())
        assert declared, "the manifest declares no agents — this guard is vacuous"
        for agent in _page_chat_agents():
            assert agent in declared, (
                f"{agent!r} is not the declared `name` of any shipped agent; dispatch "
                f"would fall back to the default agent silently. Declared: "
                f"{sorted(declared)}"
            )

    def test_the_page_does_not_use_the_slash_namespace(self) -> None:
        """The slash form is a display namespace, not a dispatchable name.

        Pinned separately because it fails soundlessly: a `pptx-maker/...` value
        opens a working chat with the wrong agent.
        """
        for agent in _page_chat_agents():
            assert "/" not in agent, f"{agent!r} uses the namespace form, not the declared name"

    def test_the_page_does_not_use_the_filename_stem(self) -> None:
        """The `{app}--{agent}` stem is the registered FILENAME, not a name.

        `_scan_materialized_agents` trusts each config's declared `name` and uses
        the stem only when a config declares none — a state `_declared_agent_names`
        asserts against for this app — so the double-hyphen spelling matches
        nothing and dispatch falls back to the default agent silently. Pinned
        separately so a reintroduction fails with the reason, not just "not found
        in the set".
        """
        for agent in _page_chat_agents():
            assert "--" not in agent, (
                f"{agent!r} is the on-disk filename stem, which dispatch cannot "
                "resolve — use the agent's declared `name`"
            )

    def test_all_three_chat_modes_are_present(self) -> None:
        """Spec, vibe and style are the three the UI offers; a dropped one would leave
        a mode button pointing at nothing."""
        agents = _page_chat_agents()
        assert len(agents) == 3, agents
        assert {a.rsplit("-", 1)[-1] for a in agents} == {"spec", "vibe", "style"}

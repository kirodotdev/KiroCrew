"""The chat-mode agent names the frontend sends must be the ones registered.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer``.

This guards a failure that is invisible at runtime: the value the page hands to
``createChatSlot`` reaches ``kiro-cli --agent``, which resolves it against the
filename ``bridges._safe_link_name`` wrote — and an unknown name makes ``--agent``
FALL BACK to the default agent instead of erroring. So a wrong string here opens a
plain chat with none of this app's MCP tools or prompt, while looking like it
worked. Derived from ``_safe_link_name`` rather than hard-coded, so the two cannot
drift apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.apps.bridges import _namespace, _safe_link_name

# Anchored on the REPO ROOT, derived from this file, never on the CWD. A
# CWD-relative path resolves differently under `pytest -n auto` (each xdist worker
# can start elsewhere), which made these pass locally and fail with
# `FileNotFoundError` in the sharded run.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_DIR = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins" / "pptx_maker"
_PAGE = _REPO_ROOT / "website" / "src" / "apps" / "pptx-maker" / "PptxMakerPage.tsx"


def _declared_agent_names() -> list[str]:
    """The ``name`` of every agent the manifest declares, in manifest order."""
    manifest = json.loads((_APP_DIR / "app.json").read_text(encoding="utf-8"))
    names = []
    for rel in manifest.get("agents") or []:
        names.append(json.loads((_APP_DIR / rel).read_text(encoding="utf-8"))["name"])
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
    def test_every_chat_agent_is_a_registered_link_name(self) -> None:
        """`{app}--{agent}`, which is what `--agent` looks up."""
        registered = {
            _safe_link_name(_namespace("pptx-maker", name)) for name in _declared_agent_names()
        }
        assert registered, "the manifest declares no agents — this guard is vacuous"
        for agent in _page_chat_agents():
            assert agent in registered, (
                f"{agent!r} is not a registered agent link name; `--agent` would fall "
                f"back to the default agent silently. Registered: {sorted(registered)}"
            )

    def test_the_page_does_not_use_the_slash_namespace(self) -> None:
        """The slash form is the namespace, NOT the filename — it matches nothing.

        Pinned separately because it is the exact mistake this fixed, and it fails
        soundlessly: a `pptx-maker/...` value opens a working chat with the wrong agent.
        """
        for agent in _page_chat_agents():
            assert "/" not in agent, f"{agent!r} uses the namespace form, not the link name"

    def test_all_three_chat_modes_are_present(self) -> None:
        """Spec, vibe and style are the three the UI offers; a dropped one would leave
        a mode button pointing at nothing."""
        agents = _page_chat_agents()
        assert len(agents) == 3, agents
        assert {a.rsplit("-", 1)[-1] for a in agents} == {"spec", "vibe", "style"}

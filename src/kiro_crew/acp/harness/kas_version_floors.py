"""The kiro-cli release floors a KAS relay spawn warns below, and their notices.

The agent-prompt floor moved here whole from :mod:`kiro_crew.acp.harness.kas`,
which imports the names it uses back, so that module stays the import path its
callers use.
"""

from __future__ import annotations

#: Oldest kiro-cli whose KAS applies a ``customAgents`` prompt: 2.33.1 ships KAS
#: 0.66.58 (``kas/worker-kit-live.jsonl``, ``kas/markdown-agent-live.jsonl``).
#: KAS 0.66.43 (kiro-cli 2.32.0) dropped the prompt of every custom agent.
KAS_AGENT_PROMPT_MIN_KIRO_CLI: tuple[int, int, int] = (2, 33, 1)


def kas_agent_prompt_issue(version: tuple[int, int, int] | None) -> str:
    """The upgrade notice for a relay-route KAS on kiro-cli *version*, or ``""``.

    Warn, never refuse: the chat still runs, without the agent's own prompt. An
    unknown version says nothing, as for the steer floor (``steer_version_issue``).
    """
    if version is None or version >= KAS_AGENT_PROMPT_MIN_KIRO_CLI:
        return ""
    installed, minimum = (".".join(map(str, v)) for v in (version, KAS_AGENT_PROMPT_MIN_KIRO_CLI))
    return (
        f"kiro-cli {installed} is older than {minimum}, so KAS drops this agent's own "
        "prompt and the chat runs without it. Update it: run 'kiro-cli update', "
        "then restart Kiro Crew."
    )


#: Oldest kiro-cli whose bundled KAS names Kiro Crew as the client: 3.4.0 ships KAS
#: 0.66.111 (kiro-cli ``package.json`` catalog at tag ``v3.4.0``). KAS before 0.66.94
#: does not know the ``kirocrew`` client name and treats the chat as an IDE client;
#: 3.2.0 (KAS 0.66.95) through 3.3.0 (KAS 0.66.102) name it as kiro-cli instead.
KAS_CLIENT_ORIGIN_MIN_KIRO_CLI: tuple[int, int, int] = (3, 4, 0)


def kas_client_origin_issue(version: tuple[int, int, int] | None) -> str:
    """The upgrade notice for a relay-route KAS that misnames its client, or ``""``.

    Warn, never refuse, like :func:`kas_agent_prompt_issue`: the chat still runs. An
    unknown version says nothing.
    """
    if version is None or version >= KAS_CLIENT_ORIGIN_MIN_KIRO_CLI:
        return ""
    installed, minimum = (".".join(map(str, v)) for v in (version, KAS_CLIENT_ORIGIN_MIN_KIRO_CLI))
    return (
        f"kiro-cli {installed} is older than {minimum}, so its KAS does not tell the "
        "Kiro service this chat is Kiro Crew, and the service may treat it as another "
        "client. The chat still runs. Update kiro-cli the way you installed it "
        "('kiro-cli update', 'toolbox update kiro-cli' or 'brew upgrade --cask kiro-cli'), "
        "then restart Kiro Crew."
    )

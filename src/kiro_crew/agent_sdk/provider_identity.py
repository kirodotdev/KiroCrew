"""Which provider a session is configured to run on.

This is the ``agent.provider`` axis: the config key an operator sets, whose
value reaches roughly a dozen branches that need to know "am I on Claude Code".
Those branches used to spell the value inline, which made the provider-specific
logic impossible to find by grep and impossible to change in one place.

Four different things are spelled ``"claude_code"`` in this codebase and only
the FIRST is this module's business. Consolidating the others into this one
would be wrong, so they are named here to keep the next reader from trying:

1. **This axis** -- the ``agent.provider`` config value, and the question
   "is the configured provider Claude Code". Owned here.

2. **The session-map provider label** -- ``PROVIDER_LABEL_CLAUDE`` in
   :mod:`kiro_crew.acp.types`. Same string, different job: it indexes resume
   compatibility, session-map persistence, and session-file cleanup routing.
   It has its own constant already; use that one when writing a session map.

3. **A model-registry index key** -- inside :mod:`kiro_crew.model_registry`,
   ``"claude_code"`` names a registry namespace, NOT a provider check. The
   module says so where it matters: *"Named for the registry key, NOT because
   windows are claude_code-only."* A model's context window is a property of
   the model, not of the provider serving it. Do not route those through the
   predicate below -- it would assert a provider identity the code is
   explicitly not testing.

4. **An onboarding import-source id** -- in :mod:`kiro_crew.onboarding_import`,
   ``"claude_code"`` identifies the Claude Code desktop app whose settings are
   being imported (``~/.claude``, ``CLAUDE_CONFIG_DIR``). It describes a
   foreign config tree on disk and is unrelated to what this process runs on.

(1) and (2) hold equal values today and are pinned equal by a test rather than
by an import, so a future divergence fails loudly instead of silently coupling
config vocabulary to session-map vocabulary.

Deliberately dependency-free: only stdlib, no ``kiro_crew`` imports, so this
module can never be the module that closes an import cycle.

Note what that does NOT buy. Importing ``kiro_crew.agent_sdk.provider_identity``
executes the parent package's ``__init__``, which loads ``backend_install`` and
``agent_sdk.drivers.acp`` -- naming the submodule does not skip them. What keeps
that cheap is that the chain is import-light rather than absent: ``acp_backends``
is stdlib-only, and ``drivers.acp`` defers every ``kiro_crew.acp`` import into a
function body, so ``kiro_crew.acp`` itself stays unloaded. That last property is
pinned by ``test_importing_this_module_does_not_load_the_acp_package``.
"""

from __future__ import annotations

#: ``agent.provider`` value selecting the Claude Code seam (claude-agent-acp).
#: Dormant in the public build, where the schema admits only ``PROVIDER_ACP``;
#: an edition re-registers it.
PROVIDER_CLAUDE_CODE = "claude_code"

#: ``agent.provider`` value selecting the ACP family (kiro-cli and KAS). The
#: default, and the only value the public build's config schema accepts.
PROVIDER_ACP = "acp"


def is_claude_code(provider: str | None) -> bool:
    """Whether *provider* names the Claude Code seam.

    Takes the raw ``agent.provider`` string (or a value already threaded down
    from it) so every caller asks the question the same way, whether it read
    config itself or received the value as an argument.

    A missing or empty value is NOT Claude Code: absent config means the
    default provider, so this answers False rather than guessing.
    """
    return provider == PROVIDER_CLAUDE_CODE

"""Provider labels: the backend identity key a session persists under.

Re-exported by :mod:`kiro_crew.acp.types`, which stays the import path callers use.
"""

from __future__ import annotations

from kiro_crew.acp_backends import (
    ACP_BACKEND_ANTIGRAVITY,
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)

# ── Provider labels ──
# The backend identity key persisted in the session map. It indexes three
# things, so every producer must agree on it: resume compatibility
# (detect_provider_switch), session-map persistence, and session-file cleanup
# routing. Defined here rather than in providers.acp because session.py needs
# the vocabulary and cannot import that module at module scope.
#
# An absent label means kiro-cli, which is the default backend.
PROVIDER_LABEL_DEFAULT = "acp"
PROVIDER_LABEL_CLAUDE = "claude_code"
PROVIDER_LABEL_KAS = "kas"
PROVIDER_LABEL_CODEX = "codex"
PROVIDER_LABEL_OPENCODE = "opencode"
PROVIDER_LABEL_PI = "pi"
PROVIDER_LABEL_GOOSE = "goose"
PROVIDER_LABEL_DEEPSEEK = "deepseek"
PROVIDER_LABEL_COPILOT = "copilot"
PROVIDER_LABEL_STRANDS = "strands"
PROVIDER_LABEL_ANTIGRAVITY = "antigravity"

#: Backend id -> its label. The mapping is what ``provider_label`` resolves
#: through, so a harness's label and the answer a session persists under are one
#: fact rather than a constant here and a branch in ``providers.acp``. Closed on
#: purpose: an id absent from it persists as a kiro session, which is why
#: ``test_harness_parity`` asserts the keys are exactly ``ACP_BACKENDS_KNOWN``.
#:
#: kiro-cli's own id is the empty string and its label is the DEFAULT, so it is a
#: row here like every other harness rather than the value a missing row falls
#: back to -- the fallback exists for an id this build does not know at all.
PROVIDER_LABEL_BY_BACKEND: dict = {
    ACP_BACKEND_KIRO: PROVIDER_LABEL_DEFAULT,
    ACP_BACKEND_KAS: PROVIDER_LABEL_KAS,
    ACP_BACKEND_STRANDS: PROVIDER_LABEL_STRANDS,
    ACP_BACKEND_CLAUDE: PROVIDER_LABEL_CLAUDE,
    ACP_BACKEND_CODEX: PROVIDER_LABEL_CODEX,
    ACP_BACKEND_OPENCODE: PROVIDER_LABEL_OPENCODE,
    ACP_BACKEND_PI: PROVIDER_LABEL_PI,
    ACP_BACKEND_GOOSE: PROVIDER_LABEL_GOOSE,
    ACP_BACKEND_DEEPSEEK: PROVIDER_LABEL_DEEPSEEK,
    ACP_BACKEND_COPILOT: PROVIDER_LABEL_COPILOT,
    ACP_BACKEND_ANTIGRAVITY: PROVIDER_LABEL_ANTIGRAVITY,
}

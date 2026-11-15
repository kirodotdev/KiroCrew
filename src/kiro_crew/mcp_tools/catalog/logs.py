"""Tool declarations of the ``logs`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.logs`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any


def schemas() -> list[dict[str, Any]]:
    """Descriptor for the kiro-cli log reader."""
    return [
        {
            "name": "kiro_cli_logs",
            "description": (
                "Read a REDACTED tail of kiro-cli's OWN protocol logs — the "
                "runtime you are driving — to diagnose a rejected or failed "
                "turn. Reads only the mcp/lsp log files, never the fenced "
                "identity/token store, and NOT the conversation-bearing sources "
                "(session transcripts, kiro-chat.log): neither is scoped to your "
                "session (even a kiro-cli process's own chat log holds every "
                "session that process serves), so returning them would hand "
                "another session's private conversation to yours. The output runs "
                "through the same credential + exfiltration-URL scrubbers the "
                "diagnostics bundle uses, so live tokens, Authorization headers "
                "and auth cookies are stripped before you see them. Use it when "
                "the backend rejected a turn, an ACP request errored, or you "
                "need first-hand evidence of what kiro-cli did. `tail` bounds "
                "the number of lines (default 200); `since` keeps lines at/after "
                'a leading-timestamp prefix like "2026-09-06T16:". Output is '
                "byte-capped per source AND bounded as a whole; when it does not "
                "all fit, the OLDEST lines are dropped and the newest are kept, "
                "with a note saying so. Returns the log text with one section "
                "per source, or a note when no logs exist."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tail": {
                        "type": "integer",
                        "description": (
                            "Max number of lines to return per source (default "
                            "200). The output is byte-capped independently, so a "
                            "large value cannot enlarge the response past the cap."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Keep only lines at or after this leading-timestamp "
                            "prefix (lexical match against the start of each log "
                            'line), e.g. "2026-09-06" or "2026-09-06T16:". '
                            "Continuation lines of a kept event ride along."
                        ),
                    },
                },
            },
        },
    ]

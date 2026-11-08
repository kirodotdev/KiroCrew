"""Boot notice for credentials stored as plain text in the config documents.

``config.json`` and ``config.local.json`` are readable by sandboxed agents (see
``sandbox_leaves``), so a channel token or the legacy ``hooks.webhook_token``
written there is readable by them too. The recommended homes -- channel settings
(vault) or ``.env`` -- are not. This module only names the offending key paths in
one WARNING at gateway boot: it never logs a value, never edits a file and never
changes how a credential is read.

The raw documents are read, not ``KiroCrewConfig.load()``: a load merges
environment variables, so a token kept in ``.env`` would be reported as if it sat
in the file.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("kiro_crew.config.loader")

#: The legacy bearer-only webhook token.
LEGACY_WEBHOOK_TOKEN_PATH = "hooks.webhook_token"

#: Channel credential key paths (each has a channel-settings / ``.env`` home)
#: plus the legacy webhook token. ``*`` matches one map key (a named
#: account). An explicit list rather than the schema's ``sensitive`` flag: that
#: flag also marks non-secret values the browser view hides (a model file name,
#: a hash, a telemetry URL) which can only live in ``config.json``, so naming
#: them here would be a warning the owner can never clear.
CREDENTIAL_PATHS: tuple[str, ...] = (
    "telegram.bot_token",
    "telegram.accounts.*.bot_token",
    "discord.bot_token",
    "webex.bot_token",
    "weixin.token",
    "teams.app_password",
    LEGACY_WEBHOOK_TOKEN_PATH,
)

#: Strong reference to the boot task (the loop holds tasks weakly).
_NOTICE_TASK: asyncio.Task[None] | None = None


def _is_plaintext(value: object) -> bool:
    """A non-empty string that is not a ``secret://`` vault reference."""
    return isinstance(value, str) and bool(value) and not value.startswith("secret://")


def _matches(node: object, parts: list[str], prefix: str, out: list[str]) -> None:
    if not parts:
        if _is_plaintext(node):
            out.append(prefix)
        return
    if not isinstance(node, dict):
        return
    head, rest = parts[0], parts[1:]
    keys = [k for k in node if isinstance(k, str)] if head == "*" else [head]
    for key in keys:
        if key in node:
            _matches(node[key], rest, f"{prefix}.{key}" if prefix else key, out)


def plaintext_credential_paths(doc: object) -> list[str]:
    """The key paths in one raw config document that hold a plaintext credential."""
    found: list[str] = []
    for pattern in CREDENTIAL_PATHS:
        _matches(doc, pattern.split("."), "", found)
    return found


def warn_plaintext_credentials() -> None:
    """Log one WARNING naming plaintext credential key paths in the config files.

    Blocking file reads: call it off the event loop. An unreadable or malformed
    document is skipped; config loading reports those itself.
    """
    from kiro_crew.config.loader import config_local_path, config_path, read_config_text

    findings: list[str] = []
    legacy = False
    for path in (config_path(), config_local_path()):
        try:
            doc = json.loads(read_config_text(path))
        except (OSError, ValueError):
            continue
        for key in plaintext_credential_paths(doc):
            legacy = legacy or key == LEGACY_WEBHOOK_TOKEN_PATH
            findings.append(f"{path.name}:{key}")
    if not findings:
        return
    advice = []
    if any(not f.endswith(":" + LEGACY_WEBHOOK_TOKEN_PATH) for f in findings):
        advice.append("Move channel tokens to the channel settings or .env.")
    if legacy:
        advice.append(
            "hooks.webhook_token is a legacy bearer-only token: create a signed "
            "source on the Webhooks page, then delete the key."
        )
    # The format string names no secret on purpose; the paths and advice are args.
    logger.warning(
        "Plain-text values at %s; config files are readable by sandboxed agents. %s",
        ", ".join(findings),
        " ".join(advice),
    )


def start_plaintext_credentials_notice() -> None:
    """Schedule the notice in the background so it never delays gateway readiness."""
    global _NOTICE_TASK

    async def _run() -> None:
        try:
            await asyncio.to_thread(warn_plaintext_credentials)
        except Exception:
            logger.debug("plaintext credential notice failed", exc_info=True)

    _NOTICE_TASK = asyncio.create_task(_run(), name="plaintext-credentials-notice")

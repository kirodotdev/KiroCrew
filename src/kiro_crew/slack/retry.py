"""Shared Slack DM-open retry.

ONE bounded-retry implementation for every DM sender (today: cron delivery
in the Slack gateway) so retryability classification and backoff cannot
drift between callers.  ``open_dm`` is the transient-failure-prone step
(rate limits, 5xx, network blips); ``post_message`` deliberately stays
single-shot at each call site to avoid duplicate DMs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

# Transient-failure classes worth retrying. AsyncWebClient rides on aiohttp,
# whose DNS/connector failures surface as aiohttp.ClientError subclasses (NOT
# ConnectionError) -- without them a transient connector blip abandons the
# DM after one attempt (GPT 5.6 PR #422 round 21).
_RETRYABLE_EXCEPTIONS = (
    SlackApiError,
    ConnectionError,
    TimeoutError,
    aiohttp.ClientError,
)


async def open_dm_with_retry(
    slack: Any,
    user_id: str,
    *,
    context: str = "",
    max_attempts: int = 3,
) -> Optional[str]:
    """Open a DM channel, retrying transient Slack API errors.

    Retries rate-limits (429), server errors (5xx), and network errors with a
    linear backoff; re-raises immediately on non-retryable client errors (4xx
    except 429) and on the final failed attempt.  ``context`` labels the WARN
    logs with the caller (e.g. ``"Cron 'daily-report'"``).
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await slack.open_dm(user_id)
        except _RETRYABLE_EXCEPTIONS as exc:
            retryable = (
                not isinstance(exc, SlackApiError)
                or exc.response.status_code == 429
                or exc.response.status_code >= 500
            )
            if not retryable:
                raise
            if attempt < max_attempts:
                logger.warning(
                    "%s: open_dm attempt %d/%d failed, retrying in %ds",
                    context or "open_dm",
                    attempt,
                    max_attempts,
                    attempt,
                    exc_info=True,
                )
                await asyncio.sleep(attempt)
            else:
                raise
    return None  # unreachable but satisfies type checker

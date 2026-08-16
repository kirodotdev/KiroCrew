"""Agent-assisted content fetch for Knowledge Library.

Fetches content from URLs using the dedicated URL-fetch LLM pool. The pool worker uses
whatever web/URL-fetch tool its backend exposes (configure the allowed tools
via KIROCREW_KNOWLEDGE_FETCH_TOOLS). If the backend has no fetch tool available,
the fetch fails gracefully and the caller falls back to local readers.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.knowledge.llm_pool import LLMPool

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 120.0

FETCH_PROMPT_TEMPLATE = (
    "Fetch the full text content from this URL using any web/URL-fetch tool you "
    "have available: {url}\n\n"
    "Return ONLY the raw document text, no commentary or formatting.\n"
    "If you cannot access the URL or encounter an error, respond with exactly: ERROR: <reason>"
)

# Patterns indicating the LLM returned an error instead of content
_ERROR_INDICATORS = (
    "ERROR:",
    "I don't have access",
    "I cannot access",
    "I'm unable to",
    "permission denied",
    "I don't have permission",
    "tool is not available",
    "not available in this",
)


async def fetch_url_content(url: str, pool: "LLMPool") -> str:
    """Fetch content from a URL using the dedicated URL-fetch pool.

    Acquires a worker from the caller-supplied URL-fetch pool, sends the fetch
    prompt, and releases the worker. The worker uses whatever URL-fetch tool its
    backend exposes (configurable via KIROCREW_KNOWLEDGE_FETCH_TOOLS); if none is
    available the fetch fails.

    Returns the fetched text content.
    Raises RuntimeError on failure.
    """
    logger.info("fetch_url_content: starting fetch for %s (pool provider=%s)", url, pool.provider_type)
    prompt = FETCH_PROMPT_TEMPLATE.format(url=url)
    response = await pool.send(prompt, timeout=FETCH_TIMEOUT)
    logger.info("fetch_url_content: got response length=%d for %s", len(response) if response else 0, url)
    if not response or not response.strip():
        raise RuntimeError(f"LLM pool returned empty content for {url}")
    content = response.strip()
    # Check if the response is an error message rather than actual content
    content_lower = content[:200].lower()
    for indicator in _ERROR_INDICATORS:
        if indicator.lower() in content_lower:
            redacted, _ = redact_exfiltration_urls(content[:300])
            redacted, _ = redact_credentials(redacted)
            logger.error("fetch_url_content: error indicator '%s' found in response: %s", indicator, redacted[:200])
            raise RuntimeError(f"Failed to fetch {url}: {redacted[:300]}")
    # Sanity check: real documents should have meaningful length
    if len(content) < 50:
        raise RuntimeError(f"Content too short for {url} ({len(content)} chars) -- likely an error")
    logger.info("fetch_url_content: success for %s (%d chars)", url, len(content))
    return content

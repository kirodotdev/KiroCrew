"""Navigation panel — LLM-powered link summary resolution."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

_LINK_SUMMARY_PROMPT = (
    "You are a link labeling agent. Given a list of URLs with their surrounding context from a chat conversation, "
    "generate a short descriptive label (3-8 words) for each URL.\n\n"
    "Rules:\n"
    "- Output one label per line, in the same order as the input\n"
    "- Each line should be ONLY the label text, nothing else\n"
    "- Be concise: 'Memory V2 Design Doc' not 'A document about the Memory V2 design'\n"
    "- For CRs: include the feature name, e.g. 'Nav panel link labels CR'\n"
    "- For tickets: include the topic, e.g. 'OmniScan Cognex deployment'\n"
    "- If context is insufficient, use the URL type + ID as label, e.g. 'Doc dVbcAXW3'\n\n"
    "{items}"
)


def _safe_url_for_prompt(url: str) -> str:
    """Strip query/fragment from URL before feeding to LLM to prevent prompt injection."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def _normalize_link(raw: object) -> dict[str, str]:
    """Coerce a raw link entry to ``{"url": str, "context": str}``.

    The nav panel posts arbitrary client JSON; a non-dict entry or a
    non-string ``url``/``context`` previously raised (TypeError/AttributeError)
    inside prompt construction and surfaced as a 500. Normalizing at this
    boundary keeps downstream logic string-only, and is index-aligned with the
    input so the positional ``summaries`` response still lines up per link.
    """
    if not isinstance(raw, dict):
        return {"url": "", "context": ""}
    url = raw.get("url", "")
    ctx = raw.get("context", "")
    return {
        "url": url if isinstance(url, str) else "",
        "context": ctx if isinstance(ctx, str) else "",
    }


def _build_link_summary_prompt(links: list[dict]) -> str:
    """Build prompt for batch link summary generation."""
    items: list[str] = []
    for i, link in enumerate(links):
        url = _safe_url_for_prompt(link.get("url", "")[:500])
        ctx = link.get("context", "").strip()[:300]
        ctx_part = f"\n  Context: {ctx}" if ctx else ""
        items.append(f"{i + 1}. URL: {url}{ctx_part}")
    return _LINK_SUMMARY_PROMPT.format(items="\n".join(items))


# "auto" = inherit the session's governed default (run_bg_oneliner skips the
# override for auto). A hardcoded model id 400s on accounts/partitions that do
# not serve it.
_LINK_SUMMARY_MODEL = "auto"


async def _resolve_link_summaries(state: DashboardState, links: list[dict]) -> list[str]:
    """Generate summaries for a batch of links using the background session."""
    prompt = _build_link_summary_prompt(links)
    # Link labeling is a trivial classification task — run on the cheapest model
    # via the shared background one-liner helper (denials SEL-logged as before).
    text = await run_bg_oneliner(
        state.sessions, prompt, model=_LINK_SUMMARY_MODEL, sel_source="chat_nav"
    )

    # Parse: one label per line
    lines = [re.sub(r'^\d{1,2}[.)]\s+', '', ln.strip()) for ln in text.strip().splitlines() if ln.strip()]
    # Redact each label
    results: list[str] = []
    for ln in lines:
        ln, redacted_url = redact_exfiltration_urls(ln)
        ln, redacted_cred = redact_credentials(ln)
        if redacted_url or redacted_cred:
            sel().log_tool_invocation(
                session_key=BACKGROUND_KEY, tool_name="llm_output_redaction",
                source="chat_nav", outcome="redacted",
                metadata={"redacted_url": bool(redacted_url), "redacted_cred": bool(redacted_cred)},
            )
        results.append(ln[:80])
    return results


async def api_chat_nav_resolve_links(request: web.Request) -> web.Response:
    """POST /api/chat/nav/resolve-links — batch resolve link summaries via LLM."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        # A valid-but-non-dict JSON body (array/scalar/string) would raise on
        # body.get() below; reject it as malformed rather than 500.
        return web.json_response({"error": "invalid request body"}, status=400)

    links = body.get("links", [])
    if not isinstance(links, list) or not links:
        return web.json_response({"error": "links array required"}, status=400)
    # Cap at 20 links per request, then normalize each entry to a string
    # url/context at this boundary — a non-dict entry or non-string field would
    # otherwise raise during prompt construction and surface as a spurious 500.
    links = [_normalize_link(x) for x in links[:20]]

    try:
        summaries = await _resolve_link_summaries(state, links)
    except Exception as exc:
        # Cosmetic link-label enrichment must never emit a 5xx: a resolver
        # failure (e.g. an LLM/provider error on the shared background session)
        # is a non-event — the frontend falls back to the raw-URL chip. Fail
        # soft to 200 with empty summaries, but keep the failure diagnosable
        # via the warning log and a SEL audit event (so fail-soft doesn't hide
        # a rising real-error rate).
        logger.warning("Link summary resolution failed: %s", type(exc).__name__, exc_info=True)
        try:
            sel().log_tool_invocation(
                session_key=BACKGROUND_KEY, tool_name="llm_link_summary",
                source="chat_nav", outcome="error", error=type(exc).__name__,
            )
        except Exception:
            # Best-effort audit: a SEL emit failure must not defeat fail-soft.
            logger.warning("Failed to emit SEL event for link summary failure", exc_info=True)
        summaries = []

    # Pad if the resolver returned fewer lines than expected (or failed soft).
    while len(summaries) < len(links):
        summaries.append("")

    return web.json_response({"summaries": summaries[:len(links)]})

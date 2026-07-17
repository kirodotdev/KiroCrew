"""Prompt optimizer endpoint — rewrites vague prompts before sending to agent."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

OPTIMIZER_SYSTEM = (
    "You transform vague prompts into specific, scoped instructions that produce the right "
    "result on the first try — eliminating wasted turns and context rot.\n\n"
    "Every message contains an <original_prompt> tag. Respond with ONLY the optimized "
    "prompt — no explanations, no wrapper text.\n\n"
    "## Rules (earlier rules win on conflict)\n\n"
    "1. NEVER change the user's intent, add requirements they didn't ask for, or invent "
    "specific values they left open.\n"
    "2. If the prompt is already specific, scoped, and actionable, return it unchanged "
    "— don't optimize for the sake of optimizing.\n"
    "3. When rewriting, add what the user skipped (only when relevant):\n"
    "   - Scope: what to read, check, or locate before acting.\n"
    "   - Constraints: what to preserve, avoid, or not change.\n"
    "   - Structure: break compound tasks into numbered steps.\n"
    "   - Uncertainty: \"if uncertain about X, state assumptions before proceeding.\"\n"
    "4. Replace hedging with direct verbs (\"maybe look at\" → \"examine\"). "
    "Do NOT replace intentionally open-ended quantities with arbitrary numbers.\n"
    "5. If the task modifies existing work without mentioning preservation, add "
    "\"preserve existing behavior unless explicitly asked to change it.\"\n"
    "6. Never exceed min(3× original length, 250 words).\n\n"
    "## Examples\n\n"
    "INPUT: \"fix the bug in auth\"\n"
    "OUTPUT: \"Locate the authentication code and fix the bug. Preserve existing "
    "behavior and ensure tests pass.\"\n\n"
    "INPUT: \"write up our launch plan\"\n"
    "OUTPUT: \"Write a launch plan covering timeline, milestones, risks, and rollback "
    "strategy. Keep it concise and actionable.\"\n\n"
    "INPUT: \"maybe clean up the service and also add retry logic and update the docs\"\n"
    "OUTPUT: \"Clean up the service and add retry logic:\n"
    "1. Identify and refactor unclear sections.\n"
    "2. Add retry with exponential backoff for transient failures.\n"
    "3. Update documentation to reflect changes.\n"
    "Preserve existing interfaces.\"\n\n"
    "INPUT: \"explore what's causing the latency spike\"\n"
    "OUTPUT: \"explore what's causing the latency spike\"\n"
)

# Prompts this short are never worth optimizing


async def handle_optimize(request: web.Request) -> web.Response:
    """POST /api/optimizer/optimize — rewrite a prompt using session context."""
    state: DashboardState = request.app["state"]
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    prompt = data.get("prompt", "").strip()
    context = data.get("context", "")

    if not prompt:
        return web.json_response({"optimized": prompt, "changed": False})

    # Build the user message with context
    parts = []
    if context:
        parts.append(f"<context>\n{context[-2000:]}\n</context>\n")
    parts.append(f"<original_prompt>\n{prompt}\n</original_prompt>")
    user_msg = "\n".join(parts)

    # Use a dedicated optimizer session to avoid semaphore contention
    # with title generation and folder categorization on BACKGROUND_KEY.
    optimizer_session_key = "_optimizer"
    full_prompt = f"[System: {OPTIMIZER_SYSTEM}]\n\n{user_msg}"

    try:
        async def _optimize() -> str:
            """Acquire session, stream, release — all under one timeout."""
            logger.debug("Optimizer: acquiring dedicated session")
            client, _is_new, _resumed = await state.sessions.get_or_create(
                optimizer_session_key, agent="kirocrew-lite"
            )
            logger.debug("Optimizer: session acquired, streaming")
            try:
                text = ""
                async for event in client.stream(full_prompt):
                    if event.kind == EVENT_TEXT_CHUNK:
                        text += event.text
                    elif event.kind == EVENT_PERMISSION_REQUEST:
                        await client.reject_tool(event.request_id)
                    elif event.kind == EVENT_COMPLETE:
                        break
                return text
            finally:
                logger.debug("Optimizer: releasing dedicated session")
                state.sessions.release(optimizer_session_key)

        text = await asyncio.wait_for(_optimize(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Optimizer timed out (30s) — kirocrew-lite may be unresponsive or overloaded")
        return web.json_response({"optimized": prompt, "changed": False})
    except Exception:
        logger.warning("Optimizer failed, returning original", exc_info=True)
        return web.json_response({"optimized": prompt, "changed": False})

    optimized = text.strip().strip('"').strip("'")
    if not optimized or optimized.upper() == "UNCHANGED":
        return web.json_response({"optimized": prompt, "changed": False})

    # Redact any exfiltration URLs or credentials from LLM output
    optimized, _ = redact_exfiltration_urls(optimized)
    optimized, _ = redact_credentials(optimized)

    changed = optimized.lower().strip() != prompt.lower().strip()
    if not changed:
        return web.json_response({"optimized": prompt, "changed": False})
    return web.json_response({"optimized": optimized, "changed": True})

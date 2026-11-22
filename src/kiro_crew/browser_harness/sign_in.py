"""Let the edition's credential provider finish a sign-in an agent's ``navigate`` landed on.

A site the provider's cookies do not sign in to sends the browser to
the edition's sign-in page. Some editions can finish that sign-in themselves, out
of band. Such a provider has the OPTIONAL method ``complete_sign_in(target, url)``
(:class:`~kiro_crew.platform.interfaces.BrowserCredentialProvider`): asked about the
URL the tab landed on, it answers a single-use URL that completes the sign-in, or
``None``. The public Default has no such method, so nothing here runs there.

**When.** Only after a ``navigate`` that committed, on an ``https`` page whose host
is in BOTH the provider's ``domains()`` and ``browser.credentials.domains``, while
the browser is Kiro's own Chromium and ``browser.credentials.enabled`` is on
(:func:`_scope`), and the person does not hold the tab.
Once per op: a completion that lands on a sign-in page again is not asked again.
It runs inside the op's own task, so a takeover cancels it like any op, and inside
the op's budget, which the route widens by ``budgets.SIGN_IN_WORST_S``.

**The URL is fenced.** ``https`` only, no userinfo, no control character, at most
the navigate cap, a host in both domain lists, and the route's own committed-URL
gate (``OpContext.url_allowed``). The landing is judged again by that gate.

**The URL never leaves.** The agent is told the final page as origin and path,
redacted; the live card's step names the host; the SEL row carries the provider's
name, both hosts and the outcome. A refusal names the host only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kiro_crew.browser_harness import credentials
from kiro_crew.browser_harness import ops as harness_ops
from kiro_crew.browser_harness.budgets import SIGN_IN_NAV_S, SIGN_IN_PROVIDER_S
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: The optional provider method, read with ``getattr``.
METHOD = "complete_sign_in"

_SKIP = object()

__all__ = ["METHOD", "fenced", "run_op"]


async def run_op(
    op: str, args: dict[str, Any], ctx: harness_ops.OpContext, supervisor: Any
) -> dict[str, Any]:
    """``ops.run_op``, then a sign-in completion when a ``navigate`` landed on one."""
    result = await harness_ops.run_op(op, args, ctx)
    if op != "navigate" or not isinstance(result, dict) or not result.get("ok"):
        return result
    return await _complete_if_offered(ctx, supervisor, result)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def fenced(
    url: Any,
    declared: Sequence[str],
    allowed: Sequence[str],
    url_allowed: Any,
) -> bool:
    """May the gateway open *url*, a provider's answer? See the module docstring."""
    if not isinstance(url, str) or not url or len(url) > harness_ops._URL_MAX:
        return False
    if "\\" in url or " " in url or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = _host(url)
    if parts.scheme != "https" or "@" in parts.netloc or not host:
        return False
    if not (
        credentials.domain_allowed(host, declared) and credentials.domain_allowed(host, allowed)
    ):
        return False
    try:
        return bool(url_allowed(url))
    except Exception:  # noqa: BLE001 - a gate that cannot answer refuses
        return False


def _shown(url: str) -> str:
    """*url* as the agent may see it: origin and path, redacted (URL redactor first)."""
    try:
        parts = urlsplit(url)
        text = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


async def _scope(supervisor: Any) -> tuple[Any, credentials.CredentialSettings, Any] | None:
    """``(provider, settings, target)`` for a completion, or ``None``: the cookies' opt-in.

    Kiro's own Chromium, an edition provider, ``browser.credentials.enabled`` on. Read
    through the supervisor's credential seam (``_handle``, ``_provider``,
    ``_credential_settings``) so the apply and this question always agree.
    """
    handle = getattr(supervisor, "_handle", None)
    choice = getattr(handle, "choice", None)
    if choice is None or choice.kind != "kiro_chromium":
        return None
    provider = supervisor._provider()
    if credentials.provider_name(provider) is None:
        return None
    settings = await asyncio.to_thread(supervisor._credential_settings, provider)
    if not settings.enabled:
        return None
    return provider, settings, credentials.Target(choice.kind, choice.headless, "sign_in")


def _ask(provider: Any, method: Any, target: Any, url: str) -> tuple[tuple[str, ...], Any]:
    """In a worker thread: the provider's domains, then its answer for *url*."""
    declared = credentials.normalize_declared_domains(provider.domains())
    if not credentials.domain_allowed(_host(url), declared):
        return declared, _SKIP
    return declared, method(target, url)


async def _complete_if_offered(
    ctx: harness_ops.OpContext, supervisor: Any, result: dict[str, Any]
) -> dict[str, Any]:
    landed = str(result.get("url") or "")
    host = _host(landed)
    if not landed.startswith("https://") or not host or await harness_ops._in_control(ctx):
        return result
    scope = await _scope(supervisor)
    if scope is None:
        return result
    provider, settings, target = scope
    method = getattr(provider, METHOD, None)
    if not callable(method) or not credentials.domain_allowed(host, settings.domains):
        return result
    name = credentials.provider_name(provider)
    try:
        declared, offered = await asyncio.wait_for(
            asyncio.to_thread(_ask, provider, method, target, landed), SIGN_IN_PROVIDER_S
        )
    except asyncio.TimeoutError:
        return _told(result, name, host, "", "provider_timeout")
    except Exception as exc:  # noqa: BLE001 - its text could carry a secret; reduced to a code
        logger.warning("browser sign-in: provider failed (%s)", type(exc).__name__)
        return _told(result, name, host, "", "provider_failed")
    if offered is _SKIP:
        return result
    if offered is None:
        return _told(result, name, host, "", "not_offered")
    if not isinstance(offered, str):
        return _told(result, name, host, "", "provider_bad_shape")
    if not fenced(offered, declared, settings.domains, ctx.url_allowed):
        return _told(result, name, host, "", "url_refused")
    to_host = _host(offered)
    if await harness_ops._in_control(ctx):
        return _told(result, name, host, to_host, "held")
    try:
        done = await asyncio.wait_for(
            harness_ops.navigate_to(ctx, offered, label=f"Finish sign-in on {to_host}"),
            SIGN_IN_NAV_S,
        )
    except asyncio.TimeoutError:
        done = {"ok": False, "code": "nav_timeout"}
    if done.get("ok"):
        final = str(done.get("url") or "")
        page = {"ok": True, "url": _shown(final), "title": done.get("title", "")}
        return _told(page, name, host, to_host, "completed")
    if done.get("code") == "redirect_refused":
        page = {"ok": True, "url": "about:blank", "title": ""}
        told = _told(page, name, host, to_host, "redirect_refused")
        told["sign_in"]["note"] = (
            f"finishing the sign-in on {to_host} landed on a page the browser may not "
            "open, so the tab was moved to about:blank"
        )
        return told
    page = {"ok": True, "url": "", "title": ""}
    told = _told(page, name, host, to_host, "nav_failed")
    told["sign_in"]["note"] = (
        f"finishing the sign-in on {to_host} did not load in time; take a snapshot to "
        "see where the page is"
    )
    return told


def _told(
    result: dict[str, Any], provider: str | None, host: str, to_host: str, outcome: str
) -> dict[str, Any]:
    """*result* with the ``sign_in`` outcome, and its one SEL row."""
    _audit(provider, host, to_host, outcome)
    return {**result, "sign_in": {"outcome": outcome, "provider": provider}}


def _audit(provider: str | None, host: str, to_host: str, outcome: str) -> None:
    """One SEL row per asked provider: its name, both hosts, the outcome. Never a URL."""

    def clean(text: str) -> str:
        return "".join(ch for ch in text if ch.isprintable() and ch != " ")[:253] or "-"

    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="gateway",
            operation="browser_harness.sign_in",
            outcome=outcome,
            source="browser_api",
            resources=f"provider={clean(provider or '')} from={clean(host)} to={clean(to_host)}",
        )
    except Exception:  # noqa: BLE001 - an audit failure never stops the browser
        logger.warning("browser sign-in: SEL write failed", exc_info=True)

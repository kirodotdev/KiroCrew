"""``js``, ``cdp``, ``http_get`` and ``iframe_target``: Browser Harness's own helpers as ops.

Each op calls the Harness helper of the same name as it is (``js(expression,
target_id)``, ``cdp(method, **params)``, ``http_get(url, headers, timeout)``) and
adds no browser feature of its own. The owner opened them with a card on every
call (the browser entry in ``docs/decisions/``):

* each call asks a person on its own card, which the op route opens and checks
  (``dashboard/handlers/browser_card_ask.py``), so it holds whatever the agent's
  spec or the chat's trust auto-approves; no site grant names these ops
  (``op_classes.card_only``);
* ``capabilities.browse`` is asked at the route and again on every helper call;
* the own-tab fence: a ``js`` ``target_id`` and a ``cdp`` ``params.targetId`` must
  be a tab of the caller's (``ctx.agent_targets()``, what ``tabs switch`` admits);
  ``js`` also takes an out-of-process iframe inside one of those tabs, the id
  ``iframe_target`` answers (:func:`_own_frames`). A named target's own URL must
  pass the navigate gate, which ``run_op`` asks only of the bound tab;
  Neither op takes a session id: no ledger says whose tab a session is on, so a
  call runs on the tab the op route bound, which is the caller's own;
* ``http_get`` asks the navigate gate for its URL in the shim and at the route
  (``browser_sites.browser_call_url``) and once more here. It is plain HTTP from the helper worker: the browser's
  cookies never ride along;
* one SEL row per call: the op, a ``cdp`` method or an ``http_get`` URL cut to
  scheme, host and path. Never the expression, params, headers or result;
* the result is JSON text, with credentials and exfiltration URLs redacted and
  fence look-alikes neutralized, cut at :data:`RESULT_CAP` characters.

``ops.py`` reaches this module by a call-time import.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kiro_crew.browser_harness import ops as _ops
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: The longest result text one call returns: what ``read`` returns, so the shim's
#: header and fences still fit its cap.
RESULT_CAP = _ops.READ_CONTENT_CAP
#: The longest ``js`` expression accepted.
EXPRESSION_MAX = _ops.TEXT_CAP
#: ``http_get``'s longest timeout in seconds, and its default. It stays under the
#: route's short op budget (``budgets.OP_TIMEOUT_S``), so a slow server is Harness's
#: timeout error and not the route's.
HTTP_TIMEOUT_MAX_S = 15.0
#: Most request headers one ``http_get`` sends, and the longest name or value.
HEADERS_MAX = 32
HEADER_CHARS_MAX = 8192
#: A CDP method name: ``Domain.method``.
_METHOD_RE = re.compile(r"^[A-Z][A-Za-z]{0,63}\.[a-z][A-Za-z]{0,63}$")
#: Keyword names ``helpers.cdp`` takes for itself; a param of that name would be
#: read as the helper's own argument, not sent to the browser.
_CDP_RESERVED = frozenset({"method", "session_id", "_response_timeout"})
#: CDP methods that hand the browser a path on this computer, to read
#: (``setFileInputFiles``, a drag's ``files``, ``loadNetworkResource`` of a ``file:``
#: URL) or to write (a download folder). The browser runs outside the agent's
#: sandbox, so these would reach files the agent cannot; ``upload`` is the checked
#: way to give a page a file.
_CDP_LOCAL_PATH_METHODS = frozenset(
    {
        "DOM.setFileInputFiles",
        "Input.dispatchDragEvent",
        "Network.loadNetworkResource",
        "Page.setDownloadBehavior",
        "Browser.setDownloadBehavior",
        # Tunnels: a command sent inside these is never judged by this list.
        "Target.sendMessageToTarget",
        "Target.exposeDevToolsProtocol",
    }
)

_NOT_AGENT_TAB = "only a tab you opened can be named; leave it out to run on the current tab"
#: Longest ``iframe_target`` substring accepted.
URL_SUBSTR_MAX = 2048


async def _browser_targets(ctx: _ops.OpContext) -> dict[str, dict[str, Any]]:
    """Every target in the browser, id to its ``Target.getTargets`` info."""
    got = await ctx.helper("cdp", method="Target.getTargets", params={})
    infos = got.get("targetInfos") if isinstance(got, dict) else None
    return {
        t["targetId"]: t
        for t in (infos if isinstance(infos, list) else [])
        if isinstance(t, dict) and isinstance(t.get("targetId"), str)
    }


def _frames_of(ctx: _ops.OpContext, by_id: dict[str, dict[str, Any]]) -> dict[str, str]:
    """The caller's out-of-process iframes in *by_id*, id to URL, in target order.

    Harness's ``iframe_target`` scans every target in the browser, the person's own
    tabs included; this keeps only an ``iframe`` target whose ``parentId`` chain
    (through other iframes) reaches a tab in ``ctx.agent_targets()``.
    """
    # Chromium's ``iframe`` TargetInfo names its owner in ``parentId`` (a target id),
    # beside ``parentFrameId``.
    tabs = set(ctx.agent_targets())
    out: dict[str, str] = {}
    for target_id, info in by_id.items():
        if info.get("type") != "iframe":
            continue
        parent, seen = info.get("parentId"), {target_id}
        while parent in by_id and by_id[parent].get("type") == "iframe" and parent not in seen:
            seen.add(parent)
            parent = by_id[parent].get("parentId")
        if parent in tabs:
            out[target_id] = str(info.get("url") or "")
    return out


async def _own_frames(ctx: _ops.OpContext) -> dict[str, str]:
    """:func:`_frames_of` the browser's current targets."""
    return _frames_of(ctx, await _browser_targets(ctx))


def _gated(ctx: _ops.OpContext, url: str) -> bool:
    """Whether a tab or frame on *url* passes the navigate gate (a blank page does)."""
    return url in ("", "about:blank") or ctx.url_allowed(url)


async def _refuse_named_target(ctx: _ops.OpContext, target: Any) -> dict[str, Any] | None:
    """Why ``js`` may not run on *target*, or ``None``.

    It must be one of the caller's tabs or frames, and still in the browser. Its
    URL passes the navigate gate: ``run_op`` judges only the current tab, and a
    background tab or popup can have moved to a loopback, private or link-local page.
    ``js`` asks again after the script, as ``run_op`` does for the bound tab.
    """
    if not isinstance(target, str):
        return _ops.refusal("not_agent_tab", f"js: {_NOT_AGENT_TAB}")
    by_id = await _browser_targets(ctx)
    tabs = {t for t in ctx.agent_targets() if t in by_id}
    if target not in tabs and target not in _frames_of(ctx, by_id):
        return _ops.refusal("not_agent_tab", f"js: {_NOT_AGENT_TAB}")
    url = str(by_id[target].get("url") or "")
    if _gated(ctx, url):
        return None
    from kiro_crew.browser_harness.recording import clean_url

    return _ops.refusal(
        "url_blocked",
        f"js: that tab or frame is on {clean_url(url)[:120]}, which is not a public "
        "http(s) URL other than this dashboard or a loopback origin the user listed (or, "
        "for an app's own session, not one of the pages the app may browse)",
    )


def _audit(op: str, resource: str) -> None:
    """SEL row: the op and its method or URL. Never the expression, params, headers or body."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="browser",
            operation=f"browser.{op}",
            outcome="invoked",
            source="browser",
            resources=resource,
        )
    except Exception:  # noqa: BLE001 - auditing never fails the op
        logger.debug("browser %s audit failed", op, exc_info=True)


def _answer(value: Any) -> dict[str, Any]:
    """The helper's return value as redacted, neutralized, capped text."""
    # Imported here: the MCP server loads ``ops`` before it answers the tool list,
    # and test_mcp_listing_import_budget caps what that list may load.
    from kiro_crew.context_assembly.markers import _neutralize_fence_markers

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    # Redacted whole before the cut, so a token at the cut is still caught.
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    text = _neutralize_fence_markers(text)
    out: dict[str, Any] = {"ok": True, "result": text[:RESULT_CAP]}
    if len(text) > RESULT_CAP:
        out["truncated"] = True
        out["total"] = len(text)
    return out


async def js(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``js {expression, target_id?}``: Harness ``js`` on the current tab or one of the caller's."""
    expression = args.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return _ops.refusal("bad_expression", "js needs an `expression` string")
    if len(expression) > EXPRESSION_MAX:
        return _ops.refusal("bad_expression", f"`expression` is over {EXPRESSION_MAX} characters")
    target = args.get("target_id")
    if target is not None:
        refused = await _refuse_named_target(ctx, target)
        if refused is not None:
            return refused
    await _ops._step(ctx, "Run a script on the page")
    _audit("js", "")
    value = await ctx.helper("js", expression=expression, target_id=target)
    if target is not None:
        # The target may have moved while the script ran; a target the script closed
        # is nobody's page now, so only a refused URL withholds the value.
        after = await _refuse_named_target(ctx, target)
        if after is not None and after.get("code") == "url_blocked":
            return after
    return _answer(value)


async def cdp(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``cdp {method, params?}``: one raw CDP command through Harness ``cdp``."""
    method = args.get("method")
    if not isinstance(method, str) or not _METHOD_RE.match(method):
        return _ops.refusal("bad_method", "cdp needs a `method` like Runtime.evaluate")
    if method in _CDP_LOCAL_PATH_METHODS:
        return _ops.refusal(
            "local_path",
            f"cdp {method} hands the browser a path on this computer; use the upload op "
            "to give a page a file",
        )
    params = args.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
        return _ops.refusal("bad_params", "`params` must be an object")
    reserved = sorted(_CDP_RESERVED & set(params))
    if reserved:
        return _ops.refusal("bad_params", f"`params` may not carry {', '.join(reserved)}")
    if args.get("session_id") is not None or "sessionId" in params:
        return _ops.refusal("not_agent_tab", "cdp runs on the current tab; it takes no session id")
    target = params.get("targetId")
    if target is not None and (not isinstance(target, str) or target not in ctx.agent_targets()):
        return _ops.refusal("not_agent_tab", f"cdp targetId: {_NOT_AGENT_TAB}")
    await _ops._step(ctx, f"Send {method} to the browser")
    _audit("cdp", f"method={method}")
    return _answer(await ctx.helper("cdp", method=method, params=params))


def _headers(raw: Any) -> dict[str, str] | None | str:
    """*raw* as a header map, ``None`` when absent, or the refusal text."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or len(raw) > HEADERS_MAX:
        return f"`headers` must be an object of at most {HEADERS_MAX} names"
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            return "each header name and value must be a string"
        if len(name) > HEADER_CHARS_MAX or len(value) > HEADER_CHARS_MAX:
            return f"a header name or value is over {HEADER_CHARS_MAX} characters"
        if any(c in "\r\n" for c in name + value):
            return "a header may not contain a line break"
    return dict(raw)


async def http_get(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``http_get {url, headers?, timeout?}``: plain HTTP GET, no browser.

    A redirect is not followed: the answer is its ``status`` and ``location``, and
    the next hop is another ``http_get``, judged and asked like this one.
    """
    from kiro_crew.browser_harness.recording import clean_url

    url = args.get("url")
    if not isinstance(url, str) or not url or len(url) > _ops._URL_MAX:
        return _ops.refusal("bad_url", "http_get needs a `url` string")
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")) or not ctx.url_allowed(url):
        return _ops.refusal("url_blocked", "http_get opens public http(s) URLs only")
    headers = _headers(args.get("headers"))
    if isinstance(headers, str):
        return _ops.refusal("bad_headers", headers)
    timeout = args.get("timeout", HTTP_TIMEOUT_MAX_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        return _ops.refusal("bad_timeout", "`timeout` must be a number of seconds")
    shown = clean_url(url)
    await _ops._step(ctx, f"Fetch {shown}")
    _audit("http_get", f"url={shown}")
    text = await ctx.helper(
        "http_get", url=url, headers=headers, timeout=float(min(timeout, HTTP_TIMEOUT_MAX_S))
    )
    return _answer(text)


async def iframe_target(ctx: _ops.OpContext, args: dict[str, Any]) -> dict[str, Any]:
    """``iframe_target {url_substr}``: Harness ``iframe_target``, held to the caller's tabs.

    Answers the first out-of-process iframe inside one of the caller's tabs whose
    URL contains ``url_substr`` and passes the navigate gate (``target_id``, for
    ``js``), or ``target_id: null``.
    A same-site frame has no target of its own; the snapshot's frame refs reach it.
    """
    from kiro_crew.browser_harness.recording import clean_url

    sub = args.get("url_substr")
    if not isinstance(sub, str) or len(sub) > URL_SUBSTR_MAX:
        return _ops.refusal("bad_url_substr", "iframe_target needs a `url_substr` string")
    for target_id, url in (await _own_frames(ctx)).items():
        if sub in url and _gated(ctx, url):
            return {"ok": True, "target_id": target_id, "url": clean_url(url)}
    return {"ok": True, "target_id": None}


__all__ = ["RESULT_CAP", "cdp", "http_get", "iframe_target", "js"]

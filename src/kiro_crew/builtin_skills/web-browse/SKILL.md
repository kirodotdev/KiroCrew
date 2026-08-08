---
name: web-browse
description: Render a REAL external web page in Kiro Crew's BUILT-IN Browser panel (the right-side embedded Chromium view) with browser_navigate. Use when the user wants to VIEW / verify / "show me" an actual website or public URL (not a local dev server — that's the web-preview skill). Showing a page is ungated; operating it or reading it back (click, type, snapshot, screenshot) needs the per-session "Let the agent act" consent.
triggers: open this page, show me this site, show me the page, view this url, render this page, look at this website, open in the browser, see what this page looks like, pull up this site, visit this url
---

# Web Browse — render a real page in the Browser panel

Kiro Crew's chat right-side **Browser** panel is a real embedded Chromium view.
When the user wants to *see* an actual external web page (a public site, a docs
page, a page they just deployed), open it with `browser_navigate` — the page
loads in the **built-in browser** and the panel surfaces itself automatically.

This is the **view** path. It is deliberately narrow: open the URL and show it,
nothing more. It does NOT require the per-session "Let the agent act" consent —
the user asking for a page IS the consent, exactly as if they had typed the URL
into the panel themselves.

## How the panel works (so you set expectations correctly)

The panel is normally a **native `WebContentsView`** owned by the Electron main
process and composited over the panel's rectangle: native paint, real events,
downloads, video. The user can click and type in it directly at any time — their
own input is never gated.

Two things follow from that:

- **You do not need a screenshot to make the page appear.** `browser_navigate`
  alone opens the built-in browser and the dashboard reveals the panel. Take a
  screenshot only when *you* need to look at the page.
- **A screenshot is not what the user sees.** They are watching the live view.

**Playwright is the FALLBACK, not the default.** When no native view can serve
the session — a remote gateway, a non-Electron host — the same `browser_*` tools
transparently fall back to an out-of-process Playwright browser whose frames are
streamed into the panel as a read-only mirror. That mirror is a degraded mode: no
real input channel, just painted frames. If you find yourself on it locally, that
is a bug worth reporting, not the intended path.

## What the "Let the agent act" consent actually governs

The per-session **Let the agent act** consent authorizes **you** to *operate* the
page or read it back. It has nothing to do with whether the built-in browser is
used, and it never gates the user's own clicking and typing in the panel.

| Class | Ops | Consent needed? |
|---|---|---|
| **View** | `browser_navigate`, `browser_navigate_back`, `browser_wait_for` | No |
| **Operate** | `browser_click`, `browser_type`, `browser_press_key`, `browser_hover`, `browser_select_option`, `browser_evaluate` | **Yes** |
| **Read back** | `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages` | **Yes** |

Reading the page back needs consent for a specific reason: the built-in browser
runs on a persistent profile holding sessions the user logged into **by hand**, so
a snapshot or screenshot lifts authenticated content into model context. Opening a
page for the user to look at does not require handing its contents to you.

An operate-class call without consent is refused with
`agent-act-not-authorized`. That is an authorization answer, not a transport
problem — do not retry it or route around it. Tell the user to grant it (the
Browser panel has a "Let the agent act" button), then drive it.

## Precondition — Playwright must be available (the guard)

The `browser_*` tool NAMES still come from the external `@playwright/mcp`
package, even when the ops are served natively, so it must be present for the
tools to exist at all.

- If the `browser_*` tools are **not** in your tool list, do NOT attempt this.
  Fall back to `web_fetch` to read the page, and tell the user:
  > "The built-in browser isn't set up. Run `kirocrew browse setup` — it writes
  >  the config, registers the proxy, and tells you if `@playwright/mcp` needs
  >  installing (`npm i -g @playwright/mcp`). Then restart the gateway
  >  (`kirocrew stop && kirocrew gateway`). For now, here's what I read from the
  >  page."
- Only proceed with the steps below when the `browser_*` tools are present.

## Steps

1. Confirm the URL is a valid, real `http(s)://` page (you can find/derive it
   from the conversation — you don't need the user to paste it). Only `http` and
   `https` are accepted; `file:`, `data:` and `javascript:` are refused by the
   same guard the user's own panel controls go through.
2. `browser_navigate` to it (use `waitUntil: "domcontentloaded"` for SPAs).
3. Tell the user it's showing in the Browser panel, in one line.
4. Do NOT reach for a screenshot to "prove" it opened — the user is watching the
   live view. Inspecting the page yourself (`browser_snapshot` /
   `browser_take_screenshot`) is a read-back and needs the "Let the agent act" consent.

## View vs. operate

- **View** (this skill): open a URL and show it.
- **Operate** (click, type, fill forms, multi-step navigation): drive the
  `browser_*` tools directly. They are present in your tool list whenever
  Browser Mode is on, and you decide when a task needs interaction versus a
  plain read. If the tools are absent, view the page with `web_fetch` and, if
  the user needs interaction, tell them to enable Browser Mode in Settings.

## Not this skill

- **Local dev / static server** (localhost, a site the user is building) →
  that's the `web-preview` skill (a loopback iframe), not Playwright. If you are
  checking a front-end change **you** just made on a loopback URL, that's the
  `web-verify` skill (navigate + screenshot + read the frame).
- **Just reading text** with no need to show the page → `web_fetch` is cheaper;
  only use the browser when the user wants to *see* the rendered page.

---
name: web-browse
description: Render a REAL external web page in KiroCrew's built-in Browser panel (the right-side panel), by opening it with the Playwright browser and screenshotting it so the live view streams into the panel. Use when the user wants to VIEW / verify / "show me" an actual website or public URL (not a local dev server — that's the web-preview skill). View-only: operating the page (clicking, typing, multi-step) requires the "Browser use" (Globe) toggle.
triggers: open this page, show me this site, show me the page, view this url, render this page, look at this website, open in the browser, see what this page looks like, pull up this site, visit this url
---

# Web Browse — render a real page in the Browser panel

KiroCrew's chat right-side **Browser** panel shows a live mirror of the
Playwright browser. When the user wants to *see* an actual external web page
(a public site, a docs page, a page they just deployed), open it with the
Playwright browser and take one screenshot — the frame streams into the panel
automatically (via the screencast), so the page appears next to the chat.

This is the **view** path. It is deliberately narrow: open the URL and show it,
nothing more. It does NOT require the user to turn on the "Browser use" (Globe)
toggle — this one screenshot is self-authorizing.

## How the panel works (so you set expectations correctly)

The panel is a **read-only live mirror**: a headless Chromium renders the page
out of view, each screenshot is streamed into the panel, and the panel paints
the latest frame. There is **no OS browser window** (headless) and **no input
channel from the panel back to the page** — clicking or typing in the panel
image does nothing. To actually *operate* the page, the user turns on **Browser
use** (the Globe), which authorizes *you* (the agent) to drive Playwright via
MCP tools; the panel still just shows screenshots of what you do.

## Precondition — Playwright must be available (the guard)

The Playwright browser tools (`browser_navigate`, `browser_take_screenshot`, …)
come from the external `@playwright/mcp` package, which may not be installed.

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
   from the conversation — you don't need the user to paste it).
2. `browser_navigate` to it (use `waitUntil: "domcontentloaded"` for SPAs).
3. `browser_take_screenshot` — this streams the frame into the Browser panel.
   (This is the one case where a screenshot is the point, so the usual
   "don't screenshot unless asked" rule does not apply here.)
4. Tell the user it's showing in the Browser panel, in one line.

## View vs. operate

- **View** (this skill): open a URL and show it. No Globe toggle needed.
- **Operate** (click, type, fill forms, multi-step navigation): that's the
  "Browser use" (Globe) toggle / `[BROWSE]` mode — it authorizes you to actively
  drive the browser across turns. If the user asks you to *interact* with a page
  that's only being viewed (Globe off), don't silently start operating: tell
  them to flip **Browser use** on (the panel also has an "Enable interaction"
  button that turns it on), then drive it.

## Not this skill

- **Local dev / static server** (localhost, a site the user is building) →
  that's the `web-preview` skill (a loopback iframe), not Playwright.
- **Just reading text** with no need to show the page → `web_fetch` is cheaper;
  only use the browser when the user wants to *see* the rendered page.

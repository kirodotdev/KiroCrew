---
name: web-browse
description: Render a REAL external web page in the built-in Browser panel (the right-side panel), by opening it with the Playwright browser and screenshotting it so the live view streams into the panel. Use when the user wants to VIEW / verify / "show me" an actual website or public URL (not a local dev server, that's the web-preview skill). This skill is narrow: it just renders and shows the page. To operate the page (clicking, typing, multi-step) drive the `browser_*` tools directly.
triggers: open this page, show me this site, show me the page, view this url, render this page, look at this website, open in the browser, see what this page looks like, pull up this site, visit this url
---

# Web Browse — render a real page in the Browser panel

KiroCrew's chat right-side **Browser** panel shows a live mirror of the
Playwright browser. When the user wants to *see* an actual external web page
(a public site, a docs page, a page they just deployed), open it with the
Playwright browser and take one screenshot — the frame streams into the panel
automatically (via the screencast), so the page appears next to the chat.

This is the **view** path. It is deliberately narrow: open the URL and show it,
nothing more. Operating the page is a separate concern handled directly through
the `browser_*` tools (see below).

## How the panel works (so you set expectations correctly)

The panel is a **read-only live mirror**: a headless Chromium renders the page
out of view, each screenshot is streamed into the panel, and the panel paints
the latest frame. There is **no OS browser window** (headless) and **no input
channel from the panel back to the page** — clicking or typing in the panel
image does nothing. To actually *operate* the page, you (the agent) drive
Playwright via the `browser_*` MCP tools; the panel still just shows
screenshots of what you do.

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
   (One frame is enough; the point is that the user sees the page.)
4. Tell the user it's showing in the Browser panel, in one line.

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

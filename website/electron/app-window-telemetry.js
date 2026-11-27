"use strict";

/**
 * What the desktop shell tells its own gateway about how an app's windows are used.
 *
 * One fact today: a press of one of an app's `desktop.shortcuts`, counted as
 * `kirocrew.app.opened{surface=shortcut}` through the owner-gated
 * `POST /api/telemetry/app-view` (`src/kiro_crew/dashboard/handlers/telemetry_app_view.py`).
 * The body names the app and nothing else about it: the gateway resolves the public id
 * and version itself (`private` for an app with none) and records nothing with telemetry
 * consent off. Fire and forget: the report is never awaited and never throws, so a slow
 * or offline gateway cannot delay or break the shortcut.
 */

const APP_VIEW_PATH = "/api/telemetry/app-view";

/**
 * Post one shortcut open for *app*.
 *
 * @param {{
 *   app: string,
 *   fetchImpl: typeof fetch,
 *   request: () => Promise<{url: string, headers: Record<string, string>} | null>,
 *   timeoutMs: number,
 * }} opts `request` resolves the authenticated URL and headers for `APP_VIEW_PATH`, or null.
 */
function reportShortcutOpen({ app, fetchImpl, request, timeoutMs }) {
  if (typeof fetchImpl !== "function") return;
  (async () => {
    const req = await request();
    if (!req) return;
    await fetchImpl(req.url, {
      method: "POST",
      headers: { ...req.headers, "Content-Type": "application/json" },
      body: JSON.stringify({ app, surface: "shortcut", event: "opened" }),
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
  })().catch(() => {});
}

module.exports = { APP_VIEW_PATH, reportShortcutOpen };

"use strict";

/**
 * Native consent for sharing this computer with a Remote Crew (issue 29949).
 *
 * A share asked for from the local dashboard is granted here, in the main
 * process, and nowhere else: the gateway serves no route that grants, so
 * nothing holding its internal secret or a dashboard session can share a
 * folder. The page names only which of its gateway's Remote Crew connections
 * to share with. This module then starts a short-lived consent-only desktop
 * host (`kirocrew connected-computer desktop-host`, on a stdio pipe only this
 * process holds), asks it what the native dialog shows (the crew name from the
 * gateway's registry, the local tools from this computer's MCP config), shows
 * the dialog, the OS folder picker and one yes/no per tool, and hands only the
 * person's answers back to that host, which writes the grant. A cancelled
 * dialog sends nothing, and the host ends when the share does.
 */

// Matches protocol.MAX_EXPOSED_MCP; the cross-language contract test pins parity.
const MAX_MCP_PROMPTS = 16;
// Matches desktop_host.MAX_INSTANCE_ID: a longer id names no connection.
const MAX_INSTANCE_ID = 128;
// The host's hello: it reads the switch and the keyring, nothing over the network.
const READY_TIMEOUT_MS = 15000;
const DESCRIBE_TIMEOUT_MS = 10000;
// One SSH enrollment, with ssh's own connect budget and the remote's answer.
const SHARE_TIMEOUT_MS = 180000;

/**
 * The native questions one share asks, in order: confirm the crew, pick the
 * folder in the OS picker, then one yes/no per offerable local tool server
 * (default No). Resolves `{root, mcp}`, or null when any step was cancelled.
 * The crew name and the tool list come from the caller's trusted source (this
 * computer's desktop host), never from a page.
 */
async function askNative(crewName, servers, deps) {
  const { dialog, window: parent } = deps;
  const show = (fn, opts) => (parent ? fn(parent, opts) : fn(opts));

  const first = await show(dialog.showMessageBox.bind(dialog), {
    type: "question",
    buttons: ["Choose folder…", "Cancel"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    message: "Let a Remote Crew work on this computer?",
    detail:
      `Crew: ${crewName}\n\n` +
      "It can read, write and run commands only inside the folder you choose next. " +
      "You can turn this off at any time.",
  });
  if (!first || first.response !== 0) return null;

  const picked = await show(dialog.showOpenDialog.bind(dialog), {
    title: "Folder the crew may use",
    buttonLabel: "Share this folder",
    properties: ["openDirectory", "createDirectory", "dontAddToRecent"],
  });
  if (!picked || picked.canceled || !Array.isArray(picked.filePaths) || picked.filePaths.length !== 1) {
    return null;
  }
  const root = picked.filePaths[0];

  const offerable = (Array.isArray(servers) ? servers : [])
    .filter((s) => s && typeof s.name === "string" && s.name && !s.refused)
    .slice(0, MAX_MCP_PROMPTS);
  const mcp = [];
  for (const server of offerable) {
    const answer = await show(dialog.showMessageBox.bind(dialog), {
      type: "question",
      buttons: ["Share", "Don't share"],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
      message: `Also share the local tool "${server.name}"?`,
      detail: "Its approvals and limits on this computer still apply.",
    });
    if (answer && answer.response === 0) mcp.push(server.name);
  }
  return { root, mcp };
}

/** Whether `value` can name one of the gateway's Remote Crew connections. */
function validInstanceId(value) {
  return typeof value === "string"
    && value.length >= 1
    && value.length <= MAX_INSTANCE_ID
    // No control characters: the host refuses them too.
    && [...value].every((ch) => ch >= " " && ch !== "\u007f");
}

// One dashboard share at a time: a second request while a dialog is open is refused.
let inFlight = false;

/**
 * Share this computer with the local gateway's Remote Crew `instanceId`: start
 * a consent-only host, describe, ask natively, share, stop the host. Resolves
 * `{ok: true, grant}` or `{ok: false, error}`; never rejects.
 *
 * @param {unknown} instanceId - the gateway registry id the page named
 * @param {object} deps
 * @param {object} deps.dialog - Electron's `dialog`
 * @param {object|null} deps.window - the parent BrowserWindow, or null
 * @param {() => object|null} deps.openHost - a fresh consent-only host
 *   (runtime/gateway/connected-computers-host.js), or null when none can run
 */
async function shareFromDashboard(instanceId, deps) {
  if (!validInstanceId(instanceId)) return { ok: false, error: "unknown_instance" };
  if (inFlight) return { ok: false, error: "busy" };
  const host = typeof deps.openHost === "function" ? deps.openHost() : null;
  if (!host) return { ok: false, error: "no_desktop_host" };
  inFlight = true;
  try {
    const state = await host.startAndWait({ timeoutMs: READY_TIMEOUT_MS });
    if (state !== "consent") {
      return { ok: false, error: state === "disabled" ? "connected_computers_disabled" : "no_desktop_host" };
    }
    if (!host.protection()) return { ok: false, error: "protection_unavailable" };
    const described = await host.request("describe", { instance_id: instanceId }, { timeoutMs: DESCRIBE_TIMEOUT_MS });
    if (!described || described.ok !== true || !described.instance || typeof described.instance.name !== "string") {
      return { ok: false, error: (described && described.code) || "describe_failed" };
    }
    const asked = await askNative(described.instance.name, described.mcp, deps);
    if (!asked) return { ok: false, error: "cancelled" };
    const reply = await host.request(
      "share",
      // The token this describe opened: the host spends exactly this consent.
      { instance_id: instanceId, consent: described.consent, root: asked.root, mcp: asked.mcp },
      { timeoutMs: SHARE_TIMEOUT_MS },
    );
    if (!reply || reply.ok !== true || !reply.grant) {
      return { ok: false, error: (reply && reply.code) || "share_failed" };
    }
    return { ok: true, grant: reply.grant };
  } finally {
    inFlight = false;
    host.stop();
  }
}

module.exports = {
  MAX_MCP_PROMPTS,
  MAX_INSTANCE_ID,
  SHARE_TIMEOUT_MS,
  askNative,
  shareFromDashboard,
  validInstanceId,
};

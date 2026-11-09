"use strict";

/**
 * The native menu's sharing actions for a window on a Remote Crew while no
 * local gateway runs (issue 29949, G1).
 *
 * Every step is native and every value has a trusted source:
 * - which crew: the window's own launch port, turned into the id of a
 *   connection THIS APP configured (`desktop-<port>`); the host refuses any id
 *   the app's own store did not name, so no page picks the target (B13);
 * - what the dialog says: read back from the host (the crew name from the app's
 *   store, the local tool list from this computer's MCP config);
 * - the folder: the OS picker; the tools: one native yes/no each (default No);
 * - the grant: written by the host with the local gateway's own share path,
 *   OS keyring and Off generation.
 * The local opt-in is not here: the preview is turned on only in a local
 * gateway's Settings > Developer > Feature Previews, and while it is off no
 * sharing item shows.
 * No page can reach any of this: there is no IPC channel for it, only menu
 * items. Off and revoke only narrow.
 */

const { askNative } = require("./connected-computers");

const DESCRIBE_TIMEOUT_MS = 10000;
// One SSH enrollment, with ssh's own connect budget and the remote's answer.
const SHARE_TIMEOUT_MS = 180000;
const STATUS_TIMEOUT_MS = 10000;
const REVOKE_TIMEOUT_MS = 30000;
// A message box holds a handful of buttons; more grants are listed, not offered.
const MAX_STATUS_BUTTONS = 6;

const ENGAGED = new Set(["running", "starting", "restarting", "elsewhere", "failed"]);

/**
 * Which sharing menu items show for a host state. Every item stays hidden
 * while sharing cannot work here: unsupported, stopped, or the local preview
 * switch off ("disabled").
 */
function menuVisibility(state) {
  const on = ENGAGED.has(state);
  return { share: on, status: on, off: on };
}

const NOT_RUNNING = {
  elsewhere: [
    "The Kiro Crew gateway on this computer manages sharing.",
    "Open its dashboard and use Settings > Connected computers.",
  ],
  failed: [
    "Sharing could not start on this computer.",
    "The details are in the app log. Quit and reopen the app to try again.",
  ],
  starting: ["Sharing is still starting.", "Try again in a moment."],
  restarting: ["Sharing is restarting.", "Try again in a moment."],
  unavailable: [
    "Sharing this computer is not available here.",
    "This system has no way to keep a shared folder fenced, so sharing stays off.",
  ],
  disabled: [
    "Sharing this computer is turned off.",
    "It is a feature preview that is off in this computer's Kiro Crew settings.",
  ],
  stopped: ["Sharing is not running.", "Quit and reopen the app to try again."],
};

function createDesktopSharing({
  dialog,
  host,
  getWindow = () => null,
  getTargetPort,
  log = () => {},
}) {
  function show(fn, opts) {
    const parent = getWindow();
    return parent ? fn.call(dialog, parent, opts) : fn.call(dialog, opts);
  }

  function notice(type, message, detail) {
    return show(dialog.showMessageBox, { type, buttons: ["OK"], defaultId: 0, noLink: true, message, detail });
  }

  async function explainNotRunning(state) {
    const [message, detail] = NOT_RUNNING[state] || NOT_RUNNING.stopped;
    await notice("info", message, detail);
    return { ok: false, code: state };
  }

  async function shareThisComputer() {
    const state = host.state();
    if (state !== "running") return explainNotRunning(state);
    if (!host.protection()) {
      await notice(
        "warning",
        "This computer cannot keep a sharing credential safe.",
        "Kiro Crew keeps it in the system keychain or keyring, and none is available. Nothing was shared.",
      );
      return { ok: false, code: "protection_unavailable" };
    }
    const instanceId = `desktop-${Number(getTargetPort())}`;
    const described = await host.request("describe", { instance_id: instanceId }, { timeoutMs: DESCRIBE_TIMEOUT_MS });
    if (!described || described.ok !== true || !described.instance) {
      const code = (described && described.code) || "describe_failed";
      if (code === "unknown_instance") {
        await notice(
          "info",
          "This window is not a Remote Crew this app reaches over SSH.",
          "Set its SSH host with Connection > Set Remote Host…, then share again.",
        );
      } else {
        await notice("error", "Could not prepare sharing.", (described && described.error) || code);
      }
      return { ok: false, code };
    }
    const asked = await askNative(described.instance.name, described.mcp, { dialog, window: getWindow() });
    if (!asked) return { ok: false, code: "cancelled" };
    const reply = await host.request(
      "share",
      // The token this describe opened: the host spends exactly this consent.
      { instance_id: instanceId, consent: described.consent, root: asked.root, mcp: asked.mcp },
      { timeoutMs: SHARE_TIMEOUT_MS },
    );
    if (reply && reply.ok === true && reply.grant) {
      await notice("info", `Shared with ${described.instance.name}.`, `Folder: ${reply.grant.root}`);
      return { ok: true, grant: reply.grant };
    }
    const code = (reply && reply.code) || "share_failed";
    if (code === "timeout") {
      await notice(
        "warning",
        "Sharing did not answer in time.",
        "It may still finish. Check Connection > Sharing Status before trying again.",
      );
    } else {
      await notice("error", "Nothing was shared.", (reply && reply.error) || code);
    }
    log(`connected computers: share refused (${code})`);
    return { ok: false, code };
  }

  async function showSharingStatus() {
    const state = host.state();
    if (state !== "running") return explainNotRunning(state);
    const reply = await host.request("status", {}, { timeoutMs: STATUS_TIMEOUT_MS });
    if (!reply || reply.ok !== true || !Array.isArray(reply.grants)) {
      await notice("error", "Could not read sharing status.", (reply && reply.code) || "status_failed");
      return { ok: false, code: (reply && reply.code) || "status_failed" };
    }
    const active = reply.grants.filter((g) => g && g.status !== "revoked" && g.state !== "revoked");
    if (active.length === 0) {
      await notice("info", "This computer is not shared with any Remote Crew.", "");
      return { ok: true, revoked: null };
    }
    const lines = active.map((g, i) => `#${i + 1}  ${g.gateway_label || g.instance_id}  ${g.root}  (${g.state})`);
    const offered = active.slice(0, MAX_STATUS_BUTTONS);
    const choice = await show(dialog.showMessageBox, {
      type: "info",
      buttons: ["Done", ...offered.map((_g, i) => `Stop #${i + 1}`)],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
      message: `Shared with ${active.length} Remote Crew${active.length === 1 ? "" : "s"}`,
      detail: lines.join("\n"),
    });
    const picked = choice && choice.response > 0 ? offered[choice.response - 1] : null;
    if (!picked) return { ok: true, revoked: null };
    const sure = await show(dialog.showMessageBox, {
      type: "warning",
      buttons: ["Stop Sharing", "Cancel"],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
      message: `Stop sharing ${picked.root}?`,
      detail: "Work running there stops now. The other shares are not changed.",
    });
    if (!sure || sure.response !== 0) return { ok: true, revoked: null };
    const ended = await host.request("revoke", { grant_id: picked.grant_id }, { timeoutMs: REVOKE_TIMEOUT_MS });
    if (ended && ended.ok === true) {
      await notice("info", "Stopped sharing.", picked.root);
      return { ok: true, revoked: ended.revoked };
    }
    await notice("error", "Could not stop sharing.", (ended && (ended.error || ended.code)) || "revoke_failed");
    return { ok: false, code: (ended && ended.code) || "revoke_failed" };
  }

  async function turnOffSharing() {
    const state = host.state();
    if (state !== "running") {
      if (state === "elsewhere") return explainNotRunning(state);
      // No host runs here, so no connector does either; the CLI writes the Off.
      await notice(
        "info",
        "Sharing is not running in this app.",
        "To turn off every share on this computer, run: kirocrew connected-computer off",
      );
      return { ok: false, code: state };
    }
    const sure = await show(dialog.showMessageBox, {
      type: "warning",
      buttons: ["Turn Off", "Cancel"],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
      message: "Turn off sharing with every Remote Crew?",
      detail: "Work running on this computer stops now. Each Remote Crew is told when it can be reached.",
    });
    if (!sure || sure.response !== 0) return { ok: false, code: "cancelled" };
    const reply = await host.request("off", {}, { timeoutMs: REVOKE_TIMEOUT_MS });
    if (reply && reply.ok === true) {
      const count = Array.isArray(reply.revoked) ? reply.revoked.length : 0;
      await notice("info", `Turned off ${count} share${count === 1 ? "" : "s"}.`, "");
      return { ok: true, revoked: reply.revoked || [] };
    }
    await notice(
      "error",
      "Could not confirm that sharing is off.",
      "Run: kirocrew connected-computer off",
    );
    return { ok: false, code: (reply && reply.code) || "off_failed" };
  }

  return Object.freeze({ shareThisComputer, showSharingStatus, turnOffSharing });
}

module.exports = {
  createDesktopSharing,
  menuVisibility,
  SHARE_TIMEOUT_MS,
  MAX_STATUS_BUTTONS,
};

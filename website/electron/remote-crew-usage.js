"use strict";

// What the remote Crews loaded in the main window cost this machine, for the
// Settings -> Remote Crew readout. Read-only: nothing here changes a pane.
//
// Two kinds of pane:
// - An SSH pane is an <iframe> in the dashboard on a loopback tunnel origin.
//   It is found by walking the dashboard's frames and keeping those whose
//   origin the renderer named. Chromium may host a cross-origin frame in its
//   own renderer process, or in the dashboard's: a frame on the dashboard's
//   own pid is counted as loaded but not measured (`sharedPanes`), since its
//   cost cannot be told apart from the dashboard's.
// - A relay pane is a WebContentsView of its own, whose pid comes from
//   relay-crew-window.js.
// The pids are then looked up in app.getAppMetrics().

const { purgeableOrigin } = require("./pane-asset-journal");

/** Most origins one request may name; the warm set is far smaller. */
const MAX_ORIGINS = 64;

/** The renderer's origin list, cleaned: loopback pane origins only, never the dashboard's own. */
function paneOrigins(origins, backendUrl) {
  const out = new Set();
  if (!Array.isArray(origins)) return out;
  for (const candidate of origins.slice(0, MAX_ORIGINS)) {
    const origin = purgeableOrigin(candidate, backendUrl);
    if (origin) out.add(origin);
  }
  return out;
}

function validPid(pid) {
  return Number.isInteger(pid) && pid > 0;
}

/**
 * The panes loaded now and the pids that carry them.
 * `dashboard` is the dashboard's webContents; `relayPids` the relay panes' pids.
 * Answers { panes, sharedPanes, pids: Set<number> }.
 */
function collectPaneProcesses({ dashboard, origins, backendUrl, relayPids = [] }) {
  const wanted = paneOrigins(origins, backendUrl);
  const pids = new Set();
  let panes = 0;
  let sharedPanes = 0;
  let frames = [];
  let ownPid = 0;
  try {
    if (dashboard && !(typeof dashboard.isDestroyed === "function" && dashboard.isDestroyed())) {
      ownPid = dashboard.mainFrame.osProcessId;
      frames = dashboard.mainFrame.framesInSubtree || [];
    }
  } catch {
    // The dashboard was torn down mid-read: no SSH panes to count.
    frames = [];
  }
  const seen = new Set();
  for (const frame of frames) {
    let origin;
    let pid;
    try {
      origin = frame.origin;
      pid = frame.osProcessId;
    } catch {
      continue;
    }
    // One pane per origin: a pane's own sub-frames share its origin.
    if (!wanted.has(origin) || seen.has(origin)) continue;
    seen.add(origin);
    panes += 1;
    if (!validPid(pid) || pid === ownPid) sharedPanes += 1;
    else pids.add(pid);
  }
  for (const pid of Array.isArray(relayPids) ? relayPids : []) {
    if (!validPid(pid)) continue;
    panes += 1;
    pids.add(pid);
  }
  return { panes, sharedPanes, pids };
}

/** Sum memory (MB) and CPU (%) over the collected pids from app.getAppMetrics(). */
function summarizeUsage(collected, metrics) {
  let kb = 0;
  let cpu = 0;
  for (const m of Array.isArray(metrics) ? metrics : []) {
    if (!m || !collected.pids.has(m.pid)) continue;
    kb += Number((m.memory && m.memory.workingSetSize) || 0);
    cpu += Number((m.cpu && m.cpu.percentCPUUsage) || 0);
  }
  return {
    panes: collected.panes,
    sharedPanes: collected.sharedPanes,
    memoryMb: Math.round(kb / 1024),
    cpuPercent: Math.round(cpu * 10) / 10,
  };
}

module.exports = { collectPaneProcesses, summarizeUsage, paneOrigins, MAX_ORIGINS };

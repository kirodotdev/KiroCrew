"use strict";

/**
 * Native consent for sharing this computer with a Remote Crew (issue 29949).
 *
 * The local dashboard page mints a single-use consent id at its own gateway
 * (owner, direct-local request, feature on). It hands ONLY that id to this
 * main-process module. Everything the person is asked about -- which crew,
 * which local tools -- is read back from this shell's own gateway with the
 * internal secret, so a page cannot change what the native dialog says. The
 * folder comes from the OS picker, never from the page. Every cancel tells the
 * gateway to drop the consent, so a cancelled dialog leaves nothing to redeem.
 */

const DESCRIBE_PATH = "/api/connected-computers/local/consent/describe";
const REDEEM_PATH = "/api/connected-computers/local/consent/redeem";
const CANCEL_PATH = "/api/connected-computers/local/consent/cancel";
// secrets.token_urlsafe(24) is 32 url-safe characters.
const CONSENT_RE = /^[A-Za-z0-9_-]{32}$/;
// Matches protocol.MAX_EXPOSED_MCP; the cross-language contract test pins parity.
const MAX_MCP_PROMPTS = 16;

async function post(deps, pathName, body) {
  const secret = typeof deps.getSecret === "function" ? deps.getSecret() : "";
  if (!secret) return { status: 0, body: null };
  try {
    const res = await deps.fetchFn(`${deps.backendUrl}${pathName}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Secret": secret },
      body: JSON.stringify(body),
    });
    let parsed = null;
    try {
      parsed = await res.json();
    } catch {
      parsed = null;
    }
    return { status: res ? res.status : 0, body: parsed };
  } catch {
    return { status: 0, body: null };
  }
}

function errorCode(reply, fallback) {
  const code = reply && reply.body && typeof reply.body.code === "string" ? reply.body.code : "";
  return code || fallback;
}

async function cancel(deps, consent) {
  await post(deps, CANCEL_PATH, { consent });
  return { ok: false, error: "cancelled" };
}

/**
 * The native questions one share asks, in order: confirm the crew, pick the
 * folder in the OS picker, then one yes/no per offerable local tool server
 * (default No). Resolves `{root, mcp}`, or null when any step was cancelled.
 * The crew name and the tool list come from the caller's trusted source (this
 * computer's gateway or host), never from a page.
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

/**
 * Show the native confirm, folder picker and per-tool ticks for one consent
 * id, then redeem it. Resolves `{ok: true, grant}` or `{ok: false, error}`;
 * never rejects.
 *
 * @param {unknown} consent - the opaque id the local page received
 * @param {object} deps
 * @param {object} deps.dialog - Electron's `dialog`
 * @param {object|null} deps.window - the parent BrowserWindow, or null
 * @param {string} deps.backendUrl - this shell's own local gateway URL
 * @param {() => string} deps.getSecret - the internal secret, re-read per call
 * @param {(url: string, init: object) => Promise<any>} deps.fetchFn
 */
async function confirmConsent(consent, deps) {
  if (typeof consent !== "string" || !CONSENT_RE.test(consent)) {
    return { ok: false, error: "invalid_consent" };
  }
  const secret = typeof deps.getSecret === "function" ? deps.getSecret() : "";
  if (!secret) return { ok: false, error: "no_local_gateway" };

  const described = await post(deps, DESCRIBE_PATH, { consent });
  const info = described.body;
  if (described.status !== 200 || !info || !info.instance || typeof info.instance.name !== "string") {
    return { ok: false, error: errorCode(described, "no_consent") };
  }
  const asked = await askNative(info.instance.name, info.mcp, deps);
  if (!asked) return cancel(deps, consent);
  const { root, mcp } = asked;

  const redeemed = await post(deps, REDEEM_PATH, { consent, root, mcp });
  if (redeemed.status !== 200 || !redeemed.body || !redeemed.body.grant) {
    return { ok: false, error: errorCode(redeemed, "redeem_failed") };
  }
  return { ok: true, grant: redeemed.body.grant };
}

module.exports = {
  DESCRIBE_PATH,
  REDEEM_PATH,
  CANCEL_PATH,
  CONSENT_RE,
  MAX_MCP_PROMPTS,
  confirmConsent,
  askNative,
};

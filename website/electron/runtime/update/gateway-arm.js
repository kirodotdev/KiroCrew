"use strict";

/**
 * Best-effort client for the gateway's own record of an armed update.
 *
 * The desktop main process is authoritative for the apply (it runs on quit,
 * after the gateway is gone), so everything here may fail without changing what
 * happens: a gateway that is down, slow or older than the contract just means
 * the status page learns about the arm one start later. Nothing here throws.
 *
 * Contract names are the core's: POST /api/update {"when": "next-start"} arms
 * and the status response carries update_armed_version (null while idle).
 *
 * A gateway older than that contract treats the same POST as "apply now" and
 * restarts, so the arm is sent only to a gateway whose status carries the
 * update_armed_version key. A status without the key is an older gateway, which
 * is not the same thing as a gateway holding nothing.
 */

const DEFAULT_TIMEOUT_MS = 3000;

function createGatewayArm({
  backendUrl,
  fetchGatewayAuth = null,
  cookieHeader = null,
  fetchImpl = typeof fetch === "function" ? fetch : null,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  log = console,
}) {
  // The arm poll runs every 30s, and every lookup of the credential chain is a
  // fresh mint (a local-secret link, or `ssh <host> kirocrew token` for a remote
  // crew) that takes one of the gateway's 50 link-nonce slots. One credential is
  // reused until the gateway refuses it, as crew-companion and mochi already do.
  let cachedAuth = null;
  async function resolveAuth(force) {
    if (!force && cachedAuth) return cachedAuth;
    let auth = null;
    try { auth = fetchGatewayAuth ? await fetchGatewayAuth() : null; } catch { auth = null; }
    cachedAuth = auth && auth.value ? auth : null;
    return cachedAuth;
  }

  async function request(method, path, body) {
    if (!backendUrl || typeof fetchImpl !== "function") return { reached: false };
    const res = await send(method, path, body, await resolveAuth(false));
    if (!res.reached || (res.status !== 401 && res.status !== 403)) return res;
    // A link token lapses after its short expiry: re-resolve once, never loop.
    return send(method, path, body, await resolveAuth(true));
  }

  async function send(method, path, body, auth) {
    if (!auth || !auth.value) return { reached: false };
    const headers = {};
    let url = `${backendUrl}${path}`;
    if (auth.viaCookie) {
      const cookie = cookieHeader ? cookieHeader(backendUrl, auth.value) : "";
      if (!cookie) {
        cachedAuth = null;
        return { reached: false };
      }
      headers.Cookie = cookie;
    } else {
      url += `${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(auth.value)}`;
    }
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      const res = await fetchImpl(url, {
        method,
        headers,
        redirect: "manual",
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      let json = null;
      try { json = await res.json(); } catch { json = null; }
      return { reached: true, status: res.status, json };
    } catch (err) {
      log.info(`[update] gateway ${method} ${path} unreachable: ${(err && err.message) || err}`);
      return { reached: false };
    }
  }

  /**
   * The version the gateway holds as armed, "" when it holds none, null when it
   * cannot be asked or does not speak the arm contract (no update_armed_version
   * key in its status).
   */
  async function armedRecord() {
    const res = await request("GET", "/api/status");
    if (!res.reached || res.status !== 200 || !res.json || typeof res.json !== "object") return null;
    if (!Object.prototype.hasOwnProperty.call(res.json, "update_armed_version")) return null;
    const v = res.json.update_armed_version;
    const at = res.json.update_armed_at;
    return { version: typeof v === "string" ? v : "", armedAt: typeof at === "number" && Number.isFinite(at) ? at : null };
  }

  async function armedVersion() {
    const rec = await armedRecord();
    return rec ? rec.version : null;
  }

  return {
    /**
     * Tell a running gateway the user armed an update. Nothing is posted unless
     * its status proves it knows the arm contract.
     * @param {string} [version] the release main armed, so a gateway that cannot
     *   see it records the right one.
     * @returns {Promise<boolean>} accepted
     */
    async arm(version) {
      if ((await armedVersion()) === null) return false;
      const body = { when: "next-start", ...(version ? { version: String(version) } : {}) };
      const res = await request("POST", "/api/update", body);
      return res.reached && res.status === 202;
    },
    /** Disarm the gateway's own record, only the one stamped `armedAt` when given. @returns {Promise<boolean>} accepted */
    async cancel(armedAt = null) {
      const body = { when: "cancel", ...(typeof armedAt === "number" ? { armed_at: armedAt } : {}) };
      const res = await request("POST", "/api/update", body);
      return res.reached && res.status === 200;
    },
    armedVersion,
    /** `{version, armedAt}` (seconds, null when the gateway sends none), or null as armedVersion. */
    armedRecord,
  };
}

module.exports = { createGatewayArm };

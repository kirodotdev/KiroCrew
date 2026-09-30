"use strict";

// Hashed /assets are cached `immutable`, so a build that only changes a header
// (a CSP rule) keeps the old file and its old header. Clear the HTTP cache and
// service-worker storage once per app+gateway version pair. Run once the gateway
// answers; `probe` reads its /api/health. No answer: do nothing, check next launch.
async function clearCacheOnUpgrade({ session, store, appVersion, probe, log }) {
  const health = await probe();
  if (!health) return false;
  const version = appVersion + "+" + (health.version || "unknown");
  const previous = store.get("lastCacheVersion", "");
  if (previous === version) return false;
  try {
    await session.clearCache();
    await session.clearStorageData({ storages: ["serviceworkers", "cachestorage"] });
    store.set("lastCacheVersion", version);
    log(`upgrade cache: cleared (${previous || "none"} -> ${version})`);
    return true;
  } catch (error) {
    // Never block boot on this; the pair stays unrecorded, so the next launch retries.
    log("upgrade cache: clear failed: " + (error && error.message));
    return false;
  }
}

module.exports = { clearCacheOnUpgrade };

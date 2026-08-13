/**
 * Read the first valid dashboard.url port from the given candidate homes, in
 * order. Returns null when no usable configured port exists.
 */
function findConfiguredDashboardPort(fs, path, homeCandidates) {
  for (const home of homeCandidates) {
    try {
      const cfg = JSON.parse(fs.readFileSync(path.join(home, "config.json"), "utf8"));
      let url = cfg && cfg.dashboard && cfg.dashboard.url;
      if (!url) continue;
      if (!url.includes("://")) url = "http://" + url;
      const port = parseInt(new URL(url).port, 10);
      if (Number.isInteger(port) && port > 0 && port <= 65535) return port;
    } catch {
      // Unreadable/malformed — try the next candidate home.
    }
  }
  return null;
}

module.exports = {
  findConfiguredDashboardPort,
};

"use strict";

function literalLoopbackUrl(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.protocol !== "http:") return "";
    if (url.hostname === "localhost" || url.hostname === "kirocrew.localhost") {
      url.hostname = "127.0.0.1";
    }
    if (url.hostname !== "127.0.0.1") return "";
    return url.origin;
  } catch {
    return "";
  }
}

async function requestLocalToken(http, backendUrl, secret) {
  const literalUrl = literalLoopbackUrl(backendUrl);
  if (!literalUrl) return "";
  return new Promise((resolve) => {
    const req = http.get(
      `${literalUrl}/api/token/local`,
      { headers: { "X-Local-Secret": secret }, timeout: 5000 },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          resolve("");
          return;
        }
        let data = "";
        res.on("error", () => resolve(""));
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try { resolve(JSON.parse(data).token || ""); } catch { resolve(""); }
        });
      },
    );
    req.on("error", () => resolve(""));
    req.on("timeout", () => { req.destroy(); resolve(""); });
  });
}

async function fetchLocalToken({ backendUrl, resolveHome, path, fs, http }) {
  let secret = "";
  try {
    const authoritativeHome = resolveHome();
    secret = fs.readFileSync(path.join(authoritativeHome, ".local_secret"), "utf8").trim();
  } catch {
    // A missing/unreadable authoritative secret is an ordinary token miss.
  }
  if (!secret) return "";
  return requestLocalToken(http, backendUrl, secret);
}

module.exports = { fetchLocalToken, literalLoopbackUrl };

const nodeFs = require("node:fs");
const nodeOs = require("node:os");
const path = require("node:path");

function parseLockTarget(target) {
  const dash = typeof target === "string" ? target.lastIndexOf("-") : -1;
  if (dash <= 0) return null;
  const host = target.slice(0, dash);
  const pidText = target.slice(dash + 1);
  if (!/^\d+$/.test(pidText)) return null;
  const pid = Number(pidText);
  return pid > 0 ? { host, pid } : null;
}

function probePid(pid, kill) {
  try {
    kill(pid, 0);
    return "is running";
  } catch (error) {
    if (error && error.code === "ESRCH") return "is not running";
    return "could not be probed (" + (error && error.code ? error.code : error) + ")";
  }
}

function describeSingletonLock({ userDataDir, fs = nodeFs, hostname = nodeOs.hostname(), kill = process.kill }) {
  try {
    let target;
    try {
      target = fs.readlinkSync(path.join(userDataDir, "SingletonLock"));
    } catch (error) {
      if (error && error.code === "ENOENT") return "no SingletonLock file";
      if (error && error.code === "EINVAL") return "SingletonLock is not a symlink";
      return "SingletonLock unreadable (" + (error && error.code ? error.code : error) + ")";
    }
    const owner = parseLockTarget(target);
    if (!owner) return "SingletonLock target " + JSON.stringify(target) + " is unparseable";
    const host = owner.host === hostname
      ? "host matches"
      : "host differs (this host is " + JSON.stringify(hostname) + ")";
    return "SingletonLock target " + JSON.stringify(target) + ", " + host
      + ", pid " + owner.pid + " on this machine " + probePid(owner.pid, kill);
  } catch (error) {
    return "SingletonLock could not be inspected (" + (error && error.message ? error.message : error) + ")";
  }
}

module.exports = { parseLockTarget, describeSingletonLock };

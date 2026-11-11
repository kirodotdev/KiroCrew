"use strict";

const nodeFs = require("fs");
const nodePath = require("path");
const { resolveHome } = require("../../home-dir");

const POLICY_FILE = "security_policy.json";

function hasText(value) {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * Whether the policy file pins an update COMMAND, which is what makes a policy
 * provider own this install's updates (kiro_crew/platform/update_provider.py
 * resolve_provider: a check or apply command, top level or per platform).
 * Source, channel and floor pins alone leave the install's own updater in charge.
 */
function pinsAProvider(policy) {
  const updates = policy && policy.updates;
  if (!updates || typeof updates !== "object") return false;
  if (hasText(updates.check_command) || hasText(updates.apply_command)) return true;
  const perPlatform = updates.platform_commands;
  if (!perPlatform || typeof perPlatform !== "object") return false;
  return Object.values(perPlatform).some(
    (entry) => entry && typeof entry === "object" && (hasText(entry.check_command) || hasText(entry.apply_command)),
  );
}

/**
 * Re-derive, from the policy file as it is NOW, whether a policy provider
 * governs this install. The verdict the dashboard recorded at the last run can
 * be stale: an admin may have installed a provider since.
 *
 * @returns {"governed"|"ungoverned"|"unknown"} no file is "ungoverned"; a file
 *   that cannot be read or parsed is "unknown", because it may hold a provider.
 */
function readPolicyGovernance({ home = resolveHome(), fs = nodeFs, path = nodePath } = {}) {
  let text;
  try {
    text = fs.readFileSync(path.join(home, POLICY_FILE), "utf8");
  } catch (err) {
    return err && err.code === "ENOENT" ? "ungoverned" : "unknown";
  }
  let policy;
  try { policy = JSON.parse(text); } catch { return "unknown"; }
  return pinsAProvider(policy) ? "governed" : "ungoverned";
}

module.exports = { readPolicyGovernance, POLICY_FILE };

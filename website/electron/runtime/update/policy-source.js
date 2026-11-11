"use strict";

const nodeFs = require("fs");
const nodePath = require("path");
const { resolveHome } = require("../../home-dir");

const POLICY_FILE = "security_policy.json";
const POLICY_URL_ENV = "KIROCREW_POLICY_URL";
const POLICY_INLINE_ENV = "KIROCREW_SECURITY_POLICY";

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
 *   that cannot be read or parsed, an externally delivered policy, or a declared
 *   distribution source is "unknown", because it may hold a provider.
 */
function readPolicyGovernance({ home = resolveHome(), fs = nodeFs, path = nodePath, env = process.env } = {}) {
  // A policy delivered from outside the data home (a URL or an inline document)
  // is not readable here and may hold a provider, so it counts as unknown.
  if (hasText(env && env[POLICY_URL_ENV]) || hasText(env && env[POLICY_INLINE_ENV])) return "unknown";
  let text;
  try {
    text = fs.readFileSync(path.join(home, POLICY_FILE), "utf8");
  } catch (err) {
    return err && err.code === "ENOENT" ? "ungoverned" : "unknown";
  }
  let policy;
  try { policy = JSON.parse(text); } catch { return "unknown"; }
  if (pinsAProvider(policy)) return "governed";
  // A file that names a distribution source hands the policy to that source, whose
  // content is not read here, so it may hold a provider.
  const distribution = policy && policy.distribution;
  if (distribution && typeof distribution === "object" && hasText(distribution.source)) return "unknown";
  return "ungoverned";
}

module.exports = { readPolicyGovernance, POLICY_FILE };

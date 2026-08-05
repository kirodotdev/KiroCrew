"use strict";

/**
 * Dock/taskbar badge count clamp (RFC notification bus Phase 4).
 *
 * The renderer pushes its unread notification count over IPC; anything that
 * isn't a positive finite number clears the badge (0). Values are floored and
 * capped so a buggy renderer can't set an absurd badge.
 */
const BADGE_MAX = 9999;

function clampBadgeCount(count) {
  const n = Number(count);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return Math.min(Math.floor(n), BADGE_MAX);
}

module.exports = { clampBadgeCount, BADGE_MAX };

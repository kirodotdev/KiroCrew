/**
 * Mirrors the backend's `_display_version` (handlers/updates.py) for version
 * strings that never cross the gateway (the Electron updater's own reports):
 * a promoted STABLE build keeps its soaked candidate's prerelease stamp in
 * the bytes (promotion never re-stamps), so fold the stamp to its clean base
 * for DISPLAY on the stable channel only. Keys on the FOLLOWED channel, same
 * as the backend, plus the same escape the backend applies via
 * `_channel_move_pending`: bytes the followed lane has never published are not
 * folded. Lenient like the backend's `running_release` rule — any SemVer
 * prerelease label folds onto its numeric base, because release.yml passes
 * every `v1.2.3-<label>` tag's label through unchanged.
 *
 * DISPLAY ONLY. Every functional reader keeps the raw string: the updater's
 * compare gate, `versionLooksPrerelease`, the arm target, and the update
 * popup's per-version snooze/skip keys. Gateway-reported versions should
 * prefer the backend-folded `*_display` sibling fields and use this only as
 * a fallback shape for desktop-local values.
 */
export function foldStableStamp(
  version: string,
  channel: string | null | undefined,
  runningAheadOfChannel?: boolean | null,
): string {
  if (channel !== 'stable') return version
  // These bytes are ahead of everything the stable lane publishes, so stable has
  // never shipped them and folding would invent a release: an insider
  // `0.5.0-insider.2` whose channel preference was flipped to stable rendered as
  // a clean `v0.5.0` that does not exist. UNKNOWN (undefined/null — no check has
  // completed) keeps folding, which is the promoted-stable case the fold exists
  // for and the overwhelmingly common one.
  if (runningAheadOfChannel === true) return version
  const m = /^([0-9]+(?:\.[0-9]+)*)-.+$/.exec(version.trim())
  return m ? m[1] : version
}

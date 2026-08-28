/**
 * Mirrors the backend's `_display_version` (handlers/updates.py) for version
 * strings that never cross the gateway (the Electron updater's own reports):
 * a promoted STABLE build keeps its soaked candidate's prerelease stamp in
 * the bytes (promotion never re-stamps), so fold the stamp to its clean base
 * for DISPLAY on the stable channel only. Keys on the FOLLOWED channel, same
 * as the backend. Lenient like the backend's `running_release` rule — any
 * SemVer prerelease label folds onto its numeric base, because release.yml
 * passes every `v1.2.3-<label>` tag's label through unchanged.
 *
 * DISPLAY ONLY. Every functional reader keeps the raw string: the updater's
 * compare gate, `versionLooksPrerelease`, the arm target, and the update
 * popup's per-version snooze/skip keys. Gateway-reported versions should
 * prefer the backend-folded `*_display` sibling fields and use this only as
 * a fallback shape for desktop-local values.
 */
export function foldStableStamp(version: string, channel: string | null | undefined): string {
  if (channel !== 'stable') return version
  const m = /^([0-9]+(?:\.[0-9]+)*)-.+$/.exec(version.trim())
  return m ? m[1] : version
}

/**
 * Deterministic gradient art for apps without hero/icon imagery.
 *
 * The gradient palette is a fixed 10 swatches; each app hashes
 * its name to a stable swatch so cards keep the same art across reloads and
 * installs. Swatch hex pairs are intentionally NOT theme variables — the art
 * reads as content (like a hero image), not chrome.
 */

const SWATCHES: [string, string][] = [
  ['#2e1f57', '#6d4aff'], // purple
  ['#4a1420', '#f0564f'], // red
  ['#0c3742', '#22d3ee'], // cyan
  ['#4a3410', '#f59e0b'], // amber
  ['#14294a', '#3b82f6'], // blue
  ['#0d3a28', '#10b981'], // green
  ['#451a35', '#ec4899'], // pink
  ['#232733', '#64748b'], // slate
  ['#1e2260', '#6366f1'], // indigo
  ['#0c3d38', '#2dd4bf'], // teal
]

/** Stable non-crypto string hash (djb2). */
function hash(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0
  return h
}

/** CSS ``background`` gradient value for an app, stable per name. */
export function gradientFor(name: string): string {
  const [from, to] = SWATCHES[hash(name) % SWATCHES.length]
  return `linear-gradient(135deg, ${from}, ${to})`
}
